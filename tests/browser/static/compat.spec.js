// Which CSS and browser features do the customer pages use that some browser on the support
// list does not have? Two sources:
//   1. doiuse (caniuse.com data) over every line of CSS the customer pages carry;
//   2. USES below: features doiuse cannot see (HTML attributes, @font-face descriptors,
//      properties caniuse has no entry for), each found by a pattern in the source.
//
// Every feature that comes out unsupported or partly supported on the list is PRINTED with the
// browsers it fails on (and written to report/compat.txt). Each must be in
// compat-allowlist.json with a reason. So:
//   * a NEW unsupported feature fails the build until someone decides it is acceptable;
//   * an entry that no longer applies fails too, so the allow-list cannot go stale.

const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const postcss = require('postcss');
const doiuse = require('doiuse');
const browserslist = require('browserslist');
const lite = require('caniuse-lite');
const { customerCss, CUSTOMER_TEMPLATES, template, scripts } = require('./sources');

// The support list: iOS back to 12 (iPhone 5s/6, iPad Air 1), Chromium back to 53 (LG webOS 4
// TVs; Samsung 2017 TVs are 56), Samsung Internet back to 7, desktop Safari back to 12, Firefox ESR.
const BROWSERS = ['ios_saf >= 12', 'chrome >= 53', 'samsung >= 7', 'firefox esr', 'safari >= 12'];
const ALLOW_FILE = path.join(__dirname, 'compat-allowlist.json');

const NAMES = { chrome: 'Chrome', ios_saf: 'Safari on iOS', safari: 'Safari', samsung: 'Samsung Internet', firefox: 'Firefox' };

// Features doiuse cannot detect. `find` is looked for in the customer templates, CSS and
// scripts. `caniuse` names the caniuse.com feature; where caniuse has none, `since` gives the
// first version with support (from MDN's compatibility tables).
const USES = [
  { id: 'aspect-ratio', title: 'CSS aspect-ratio property', find: /aspect-ratio\s*:/, since: { chrome: 88, safari: 15, ios_saf: 15, samsung: 15, firefox: 89 } },
  { id: 'variable-fonts', title: 'Variable fonts (one font file, font-weight:300 900)', find: /font-weight\s*:\s*\d+\s+\d+/, caniuse: 'variable-fonts' },
  { id: 'woff2', title: 'WOFF 2.0 fonts', find: /\.woff2/, caniuse: 'woff2' },
  { id: 'css-variables', title: 'CSS variables (var(--x))', find: /var\(--/, caniuse: 'css-variables' },
  { id: 'flexbox', title: 'Flexbox', find: /display\s*:\s*flex/, caniuse: 'flexbox' },
  { id: 'viewport-units', title: 'Viewport units (vh)', find: /\d(vh|vw)\b/, caniuse: 'viewport-units' },
  { id: 'text-size-adjust', title: 'CSS text-size-adjust', find: /text-size-adjust/, caniuse: 'text-size-adjust' },
  { id: 'details', title: '<details> and <summary>', find: /<details/, caniuse: 'details' },
  { id: 'hidden', title: 'hidden attribute', find: /\shidden[\s>]/, caniuse: 'hidden' },
  { id: 'form-validation', title: 'Form validation (required)', find: /\srequired[\s>]/, caniuse: 'form-validation' },
  { id: 'link-rel-preload', title: '<link rel=preload>', find: /rel="preload"/, caniuse: 'link-rel-preload' },
  { id: 'video', title: '<video>', find: /<video/, caniuse: 'video' },
  { id: 'mpeg4', title: 'MP4 / H.264 video', find: /<video/, caniuse: 'mpeg4' },
  { id: 'aac', title: 'AAC audio', find: /<video/, caniuse: 'aac' },
  { id: 'media-fragments', title: 'Media fragments (#t=0.1 picks the first frame shown)', find: /#t=\d/, caniuse: 'media-fragments' },
  { id: 'playsinline', title: 'playsinline (video plays in the page on an iPhone)', find: /playsinline/, since: { chrome: 1, safari: 1, ios_saf: 10, samsung: 1, firefox: 1 } },
  { id: 'fetch', title: 'fetch()', find: /\bfetch\(/, caniuse: 'fetch' },
  { id: 'referrer-policy', title: 'Referrer Policy', find: /name="referrer"/, caniuse: 'referrer-policy' },
  // Added with the fix round of 2026-11-13 (the drawn stars, the old-engine player box, contrast themes):
  { id: 'css-featurequeries', title: '@supports (CSS feature queries)', find: /@supports\s*\(/, caniuse: 'css-featurequeries' },
  { id: 'forced-colors', title: '@media (forced-colors) and the system colours Canvas, CanvasText, Highlight', find: /forced-colors/, since: { chrome: 89, safari: 16, ios_saf: 16, samsung: 15, firefox: 89 } },
  { id: 'svg-html5', title: 'Inline SVG in HTML (the stars)', find: /<svg\b/, caniuse: 'svg-html5' },
  { id: 'wordwrap', title: 'overflow-wrap: break-word', find: /overflow-wrap\s*:/, caniuse: 'wordwrap' },
  { id: 'css-sel3', title: 'CSS3 selectors (:not(), :checked, ~)', find: /:not\(|:checked/, caniuse: 'css-sel3' },
  { id: 'outline', title: 'CSS outline and outline-offset (the focus rings)', find: /outline-offset\s*:/, caniuse: 'outline' },
];

function versionOf(text) {
  return parseFloat(String(text).split('-')[0]);
}

/** { missing: {browser: [versions]}, partial: {...} } for one feature over the support list. */
function support(list, use) {
  const out = { missing: {}, partial: {} };
  const stats = use.caniuse ? lite.feature(require(`caniuse-lite/data/features/${use.caniuse}`)).stats : null;
  for (const entry of list) {
    const [browser, version] = entry.split(' ');
    let flag;
    if (stats) flag = ((stats[browser] || {})[version] || 'u')[0];
    else flag = versionOf(version) >= use.since[browser] ? 'y' : 'n';
    if (flag === 'y') continue;
    const bucket = flag === 'n' || flag === 'u' ? out.missing : out.partial;
    (bucket[browser] = bucket[browser] || []).push(version);
  }
  return out;
}

function ranges(byBrowser) {
  return Object.entries(byBrowser).map(([browser, versions]) => {
    const sorted = versions.slice().sort((a, b) => versionOf(a) - versionOf(b));
    const first = sorted[0].split('-')[0];
    const last = sorted[sorted.length - 1].split('-').pop();
    return `${NAMES[browser] || browser} ${first === last ? first : `${first} to ${last}`}`;
  }).join('; ');
}

async function audit() {
  const list = browserslist(BROWSERS);
  const found = new Map(); // id -> { title, missing, partial, where:Set }

  // 1. doiuse over the CSS.
  for (const part of customerCss()) {
    await postcss([doiuse({
      browsers: BROWSERS,
      onFeatureUsage: (usage) => {
        const id = usage.feature;
        if (!found.has(id)) found.set(id, { title: usage.featureData.title, missing: usage.featureData.missing || '', partial: usage.featureData.partial || '', where: new Set(), source: 'doiuse' });
        found.get(id).where.add(`${part.from}: ${String(usage.usage).split('\n')[0].slice(0, 70)}`);
      },
    })]).process(part.css, { from: undefined });
  }

  // 2. What doiuse cannot see.
  const haystack = [...CUSTOMER_TEMPLATES.map((name) => ({ name, text: template(name) })), ...scripts().map((s) => ({ name: `static/${s.name}`, text: s.code }))];
  for (const use of USES) {
    const hits = haystack.filter((h) => use.find.test(h.text)).map((h) => h.name);
    if (!hits.length || found.has(use.id)) continue;
    const s = support(list, use);
    if (!Object.keys(s.missing).length && !Object.keys(s.partial).length) continue;
    found.set(use.id, { title: use.title, missing: ranges(s.missing), partial: ranges(s.partial), where: new Set(hits), source: use.caniuse ? 'caniuse' : 'MDN' });
  }
  return { list, found };
}

test('every feature the customer pages use that an old browser lacks is known and has a reason', async ({}, testInfo) => {
  const { list, found } = await audit();
  const allow = JSON.parse(fs.readFileSync(ALLOW_FILE, 'utf8'));
  delete allow['//'];

  const lines = [`Support list: ${BROWSERS.join(', ')} (${list.length} browser versions)`, ''];
  const unexpected = [];
  for (const [id, f] of [...found.entries()].sort()) {
    const known = allow[id];
    lines.push(`${known ? 'expected for now' : 'NEW            '}  ${id}: ${f.title}`);
    if (f.missing) lines.push(`    not supported by: ${f.missing}`);
    if (f.partial) lines.push(`    partly supported by: ${f.partial}`);
    lines.push(`    used in: ${[...f.where].slice(0, 4).join(' | ')}`);
    if (known) lines.push(`    what the customer gets: ${known.effect}`, `    why it is accepted: ${known.reason}`);
    lines.push('');
    if (!known) unexpected.push(`${id} (${f.title}) is not supported by: ${f.missing || '-'}${f.partial ? `; partly: ${f.partial}` : ''}`);
  }
  const stale = Object.keys(allow).filter((id) => !found.has(id));
  const text = lines.join('\n');
  console.log(`\n${text}`);
  fs.mkdirSync(path.join(__dirname, '..', 'report'), { recursive: true });
  fs.writeFileSync(path.join(__dirname, '..', 'report', 'compat.txt'), text);
  await testInfo.attach('compat.txt', { body: text, contentType: 'text/plain' });

  expect(unexpected, 'a feature the pages use is not supported on the whole list and is not in static/compat-allowlist.json. '
    + 'Decide what an old browser shows instead, then add it there with a reason').toEqual([]);
  expect(stale, 'static/compat-allowlist.json lists features the pages no longer use, or that the whole list now supports. Delete them').toEqual([]);
});

test('the allow-list gives every entry an effect and a reason', () => {
  const allow = JSON.parse(fs.readFileSync(ALLOW_FILE, 'utf8'));
  delete allow['//'];
  for (const [id, entry] of Object.entries(allow)) {
    expect(typeof entry.effect === 'string' && entry.effect.length > 20, `${id}: say what the customer on an old browser gets ("effect")`).toBe(true);
    expect(typeof entry.reason === 'string' && entry.reason.length > 20, `${id}: say why that is acceptable for now ("reason")`).toBe(true);
  }
});
