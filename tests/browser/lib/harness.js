// What every spec shares: a fresh video per test, an ear on the page for errors, and the
// handful of things a customer does (press play, pick a star, press a button) done the way
// that project's customer does them: a finger, a mouse, or a TV remote.

const fs = require('fs');
const path = require('path');
const base = require('@playwright/test');
const { expect } = base;
const GAPS = require('./known-gaps');

const PORT = Number(process.env.BROWSER_TEST_PORT || 19300);
const CONTROL = `http://127.0.0.1:${PORT}/__test`;
const SHOTS = path.join(__dirname, '..', 'screenshots');

async function control(pathAndQuery, options) {
  const response = await fetch(CONTROL + pathAndQuery, options);
  if (!response.ok) throw new Error(`serve.py ${pathAndQuery}: ${response.status} ${await response.text()}`);
  return response.json();
}

// Console lines that are the browser reporting an HTTP status we asked for on purpose
// (a dead link answers 410, an unknown one 404), not something wrong with the page.
const EXPECTED_STATUS_NOISE = /Failed to load resource: the server responded with a status of (404|410)/;

const test = base.test.extend({
  // 'mouse' | 'touch' | 'dpad': set per project in playwright.config.js
  input: ['mouse', { option: true }],

  server: async ({}, use) => {
    const info = await control('/info');
    await use({
      info,
      /** Film a brand new video. Options: see serve.py (file, throttle, first_byte_ms, link_seconds, address, pulled). */
      newVideo: (options = {}) => control('/video?' + new URLSearchParams(options), { method: 'POST' }),
      /** What the server and the bucket recorded for one link. */
      state: (token) => control('/state?token=' + token),
      expire: (token) => control('/expire?token=' + token, { method: 'POST' }),
      /** The office takes the video down, now. */
      pull: (token) => control('/pull?token=' + token, { method: 'POST' }),
      /**
       * The bucket stops sending this video in mid-answer (a dropped signal), or lets it flow
       * again. Needs throttle=. With `from` (a byte count) only the file from that byte on is
       * held back, so the signal drops at the same place in every browser.
       */
      stall: (token, on = true, from = 0) => control(`/stall?token=${token}&on=${on ? 1 : 0}${from ? `&from=${from}` : ''}`, { method: 'POST' }),
      /** An address of this test's own for which the REAL rate limiter is on (see serve.py). Send it as X-Forwarded-For. */
      limitedAddress: () => `198.18.${1 + Math.floor(Math.random() * 250)}.${1 + Math.floor(Math.random() * 250)}`,
      emailUrl: (token) => `${CONTROL}/email?token=${token}`,
    });
  },

  // A fresh, live video for this test alone.
  clip: async ({ server }, use) => {
    await use(await server.newVideo());
  },

  // Everything that went wrong on the page while the test ran. Tests end with
  // `expect(problems.list()).toEqual([])` so that the failure message names the problem.
  problems: async ({ page }, use, testInfo) => {
    const found = [];
    const allowed = [EXPECTED_STATUS_NOISE];
    // Playwright's WebKit on a Mac draws Safari's own player. At three device pixels per CSS
    // pixel (the iPhone 13, 15 and 17 rows) that player asks for "placard" pictures its Mac
    // build does not carry at that density, and logs "Button failed to load ... placard ...
    // [MacOSLayoutTraits ...]". It is the test browser's own player, not the page: the rows
    // at two pixels, the same rows on Linux, and real Mobile Safari on those phones in the
    // simulator log nothing. (The page's CSP does allow blob: pictures; that was not it.)
    if (projectFacts(testInfo).engine === 'webkit' && process.platform === 'darwin') {
      allowed.push(/^Button failed to load, iconName = [\w-]*placard, layoutTraits = \[MacOSLayoutTraits/);
    }
    page.on('pageerror', (error) => {
      if (allowed.some((pattern) => pattern.test(error.message))) return; // only what a test has named, with its reason
      found.push(`uncaught error: ${error.message}`);
    });
    page.on('console', (message) => {
      if (message.type() !== 'error') return;
      const text = message.text();
      if (allowed.some((pattern) => pattern.test(text))) return;
      found.push(`console error: ${text}`);
    });
    page.on('response', (response) => {
      const url = new URL(response.url());
      if (url.pathname.startsWith('/static/') && response.status() >= 400) {
        found.push(`${response.status()} for ${url.pathname}`);
      }
    });
    page.on('requestfailed', (request) => {
      const url = new URL(request.url());
      const failure = (request.failure() || {}).errorText || '';
      // A media request the browser itself cancels (it has enough) is not a failure.
      if (url.pathname.startsWith('/static/')) found.push(`request failed: ${url.pathname} ${failure}`);
    });
    // Anything the Content-Security-Policy blocked, in every engine.
    await page.addInitScript(() => {
      window.__csp = [];
      document.addEventListener('securitypolicyviolation', (e) => {
        window.__csp.push(`${e.violatedDirective} blocked ${e.blockedURI}`);
      });
    });
    await use({
      allow: (pattern) => allowed.push(pattern),
      list: async () => {
        const csp = await page.evaluate(() => window.__csp || []).catch(() => []);
        return [...found, ...csp.map((line) => `CSP: ${line}`)];
      },
    });
  },
});

// ---------------------------------------------------------------------------------------------
// Known gaps: things the page gets wrong today, each listed once in known-gaps.js
// ---------------------------------------------------------------------------------------------

function projectFacts(testInfo) {
  const use = testInfo.project.use;
  return {
    project: testInfo.project.name,
    engine: use.browserName || use.defaultBrowserType || 'chromium',
    input: use.input || 'mouse',
    width: (use.viewport || {}).width || 0,
    height: (use.viewport || {}).height || 0,
    bucket: process.env.BROWSER_TEST_BUCKET || 'https', // 'https': another origin, as in production; or 'same-origin'
  };
}

/** true: listed for this project. 'unverified': listed, but never run on this engine. false: not listed. */
function gapVerdict(testInfo, id, facts = {}) {
  const gap = GAPS.find((g) => g.id === id);
  if (!gap) throw new Error(`known-gaps.js has no entry "${id}"`);
  return { gap, verdict: gap.where({ ...projectFacts(testInfo), ...facts }) };
}

function gapFor(testInfo, id) {
  const { gap, verdict } = gapVerdict(testInfo, id);
  return verdict ? gap : null;
}

function noteUnverified(testInfo, id, gap, happened, detail) {
  const { engine } = projectFacts(testInfo);
  testInfo.annotations.push({
    type: 'to confirm',
    description: `${id}: on ${engine} it ${happened ? 'HAPPENS' : 'does NOT happen'}${detail ? ` (${detail})` : ''}. `
      + `Set this engine to ${happened ? 'true' : 'false'} in lib/known-gaps.js. [${gap.what}]`,
  });
}

/**
 * One check that may be a known gap. `wrong` is what the page did wrong ('' or [] when it is
 * right). On a project where the gap is listed, the check passes only while the page is still
 * wrong, and says so in the report; the day the page is fixed it fails and asks for the
 * entry to be deleted. Everywhere else, anything wrong fails as usual. Where the entry is
 * 'unverified' for this engine, or 'varies' (it depends on the device, not on the page alone),
 * either outcome passes and the report says which one it was. `facts` overrides what the
 * entry is asked about, for a test that sets its own window size.
 */
function checkGap(testInfo, id, wrong, message, facts = {}) {
  const { gap, verdict } = gapVerdict(testInfo, id, facts);
  const isWrong = Array.isArray(wrong) ? wrong.length > 0 : !!wrong;
  const detail = Array.isArray(wrong) ? wrong.join('; ') : String(wrong || '');
  if (verdict === 'varies') {
    // Depends on something outside the page (the device's own fonts): report what this machine showed.
    testInfo.annotations.push({ type: 'varies by device', description: `${id}: here it ${isWrong ? `HAPPENS (${detail})` : 'does not happen'}. [${gap.what}]` });
  } else if (verdict === 'unverified') {
    noteUnverified(testInfo, id, gap, isWrong, detail);
  } else if (verdict) {
    testInfo.annotations.push({ type: 'known gap', description: `${id}: ${gap.what}${detail ? ` [seen: ${detail}]` : ''}` });
    expect.soft(isWrong, `"${id}" is listed in lib/known-gaps.js for this project, but the page now gets it right. Delete the entry.`).toBe(true);
  } else {
    expect.soft(detail, message).toBe('');
  }
}

/**
 * A whole behaviour that fails today. Where the gap is listed, the test is marked with
 * test.fail(): it is reported as an expected failure, and the day the page is fixed it turns
 * into an error ("expected to fail, but passed"). Where the entry is 'unverified' for this
 * engine, the body runs and either outcome is reported without failing the run. `body` must
 * use hard expects (not expect.soft) for what the gap is about.
 */
async function knownToFail(testInfo, id, body) {
  const { gap, verdict } = gapVerdict(testInfo, id);
  if (verdict === 'unverified') {
    let failure = '';
    try {
      await body();
    } catch (error) {
      failure = String(error.message || error).split('\n')[0].replace(/\u001b\[[0-9;]*m/g, '');
    }
    noteUnverified(testInfo, id, gap, !!failure, failure);
    return;
  }
  if (verdict) {
    testInfo.annotations.push({ type: 'known gap', description: `${id}: ${gap.what}` });
    base.test.fail(true, `${id}: ${gap.what}`);
  }
  await body();
}

// ---------------------------------------------------------------------------------------------
// The player
// ---------------------------------------------------------------------------------------------

function videoState(page) {
  return page.evaluate(() => {
    const v = document.getElementById('v');
    if (!v) return null;
    const buffered = [];
    for (let i = 0; i < v.buffered.length; i += 1) buffered.push([+v.buffered.start(i).toFixed(2), +v.buffered.end(i).toFixed(2)]);
    return {
      paused: v.paused, ended: v.ended, seeking: v.seeking, muted: v.muted,
      time: v.currentTime, duration: v.duration, width: v.videoWidth, height: v.videoHeight,
      ready: v.readyState, network: v.networkState, error: v.error ? `${v.error.code} ${v.error.message || ''}`.trim() : '',
      buffered, noteShown: !document.getElementById('verr').hidden,
      // '' | 'failed' ("The video did not load. Press here to try again.") | 'trying' ("Trying again...",
      // while a try by hand runs) | 'slow' ("Your signal is weak...")
      note: document.getElementById('verr').hidden ? '' : (/did not load/.test(document.getElementById('verr').textContent) ? 'failed'
        : (/Trying again/.test(document.getElementById('verr').textContent) ? 'trying' : 'slow')),
      canPlay: v.canPlayType('video/mp4; codecs="avc1.640028, mp4a.40.2"'),
    };
  });
}

/** The metadata came through the 302 to the other origin: the player knows its length and size. */
async function waitForMetadata(page, timeout = 20_000) {
  await expect.poll(async () => {
    const s = await videoState(page);
    // canPlayType "" means this browser BUILD has no H.264/AAC decoder: a fault of the test machine, not of the page.
    const codec = s.canPlay ? '' : ' (this browser build says it cannot play H.264/AAC at all: canPlayType is "")';
    return s.error ? `error ${s.error}${codec}` : (s.ready >= 1 && s.duration > 0 && s.width > 0 ? 'ready' : `readyState ${s.ready}${codec}`);
  }, { message: 'the video never loaded its metadata through the redirect to the bucket', timeout }).toBe('ready');
  return videoState(page);
}

async function isPlaying(page, wait = 900) {
  const deadline = Date.now() + wait;
  for (;;) {
    const s = await videoState(page);
    if (s && !s.paused && !s.ended) return true;
    if (Date.now() > deadline) return false;
    await page.waitForTimeout(100);
  }
}

async function isPaused(page, wait = 900) {
  const deadline = Date.now() + wait;
  for (;;) {
    const s = await videoState(page);
    if (s && s.paused) return true;
    if (Date.now() > deadline) return false;
    await page.waitForTimeout(100);
  }
}

/** Where each engine draws the play/pause button of its own controls, from the player's box. */
function playButtonSpots(box, engine) {
  const centre = { name: 'the middle of the player', x: box.x + box.width / 2, y: box.y + box.height / 2 };
  const chromium = { name: 'the play button, bottom left', x: box.x + 25, y: box.y + box.height - 48 };
  const low = { name: 'the play button, bottom left', x: box.x + 22, y: box.y + box.height - 21 };
  if (engine === 'chromium') return [chromium, centre, low];
  return [centre, low, chromium];
}

/**
 * Press play (or pause) the way this project's customer does, and say how it was done.
 * touch: taps. mouse: clicks, then the space bar. dpad: move the focus to the player, press OK.
 * `allowScript` lets a last resort call video.play() itself; the answer then says 'script',
 * and tests that are about the customer's own hands must not accept that.
 */
async function toggle(page, testInfo, want, { allowScript = true, as = '' } = {}) {
  const { engine } = projectFacts(testInfo);
  const input = as || projectFacts(testInfo).input;
  const video = page.locator('#v');
  await video.scrollIntoViewIfNeeded();
  // "It took": the player has changed its mind about being paused. After the END of the clip
  // an engine may need a moment to go back to the start, and `ended` stays true meanwhile; a
  // second tap in that moment would pause it again (that race failed one run in CI on
  // WebKit). So a tap is only repeated somewhere else when the first one changed nothing.
  const took = async (wait = 900) => {
    const deadline = Date.now() + wait;
    for (;;) {
      const s = await videoState(page);
      if (s && (want === 'play' ? !s.paused : s.paused)) return true;
      if (Date.now() > deadline) return false;
      await page.waitForTimeout(100);
    }
  };
  // "It is done": really playing (not paused, not sitting at the end), or really paused.
  const done = async (wait = 8000) => {
    const deadline = Date.now() + wait;
    for (;;) {
      const s = await videoState(page);
      if (s && (want === 'play' ? (!s.paused && !s.ended) : s.paused)) return true;
      // It was started and something stopped it again (an error, another hand): not done.
      if (s && want === 'play' && s.paused && !s.seeking) return false;
      if (Date.now() > deadline) return false;
      await page.waitForTimeout(100);
    }
  };

  if (input === 'dpad') {
    await focusByArrows(page, '#v');
    await page.keyboard.press('Enter');
    if (await took() && await done()) return 'remote: OK on the player';
  } else {
    const box = await video.boundingBox();
    for (const spot of playButtonSpots(box, engine)) {
      if (input === 'touch') await page.touchscreen.tap(spot.x, spot.y);
      else await page.mouse.click(spot.x, spot.y);
      if (await took()) {
        if (await done()) return `${input === 'touch' ? 'tap' : 'click'}: ${spot.name}`;
        break; // it took and then stopped: pressing more places would only toggle it back and forth
      }
    }
    if (input === 'mouse' && !(await took(0))) {
      await video.focus();
      await page.keyboard.press('Space');
      if (await took() && await done()) return 'keyboard: space bar on the player';
    }
  }
  if (await done(0)) return 'a press that took late';
  if (!allowScript) return '';
  await page.evaluate((w) => {
    const v = document.getElementById('v');
    if (w === 'play') { v.muted = true; return v.play().catch(() => {}); }
    v.pause();
    return undefined;
  }, want);
  return (await done(4000)) ? 'script' : '';
}

const pressPlay = (page, testInfo, options) => toggle(page, testInfo, 'play', options);
const pressPause = (page, testInfo, options) => toggle(page, testInfo, 'pause', options);

/** The clip really plays: time moves past `seconds` and a frame has real size. */
async function waitForTime(page, seconds, timeout = 20_000) {
  await expect.poll(async () => {
    const s = await videoState(page);
    return s.error ? `error ${s.error}` : (s.time >= seconds || s.ended ? 'there' : `at ${s.time.toFixed(2)} s, readyState ${s.ready}`);
  }, { message: `the video did not play past ${seconds} s`, timeout }).toBe('there');
}

/**
 * Keep a list of everything the yellow note under the player says from now on, with the
 * second it said it: a note that shows for a moment and goes again is still something the
 * customer saw. Call after the page has loaded; read with notesShown().
 */
async function watchNote(page) {
  await page.evaluate(() => {
    if (window.__notes) return;
    window.__notes = [];
    const box = document.getElementById('verr');
    const start = Date.now();
    const look = () => {
      const now = box.hidden ? '' : box.textContent.trim();
      const last = window.__notes.length ? window.__notes[window.__notes.length - 1].text : '';
      if (now !== last) window.__notes.push({ at: (Date.now() - start) / 1000, text: now });
    };
    new MutationObserver(look).observe(box, { attributes: true, childList: true, characterData: true, subtree: true });
    look();
    // And count how often the page loads the video's source afresh from now on.
    window.__reloads = 0;
    document.getElementById('v').addEventListener('loadstart', () => { window.__reloads += 1; });
  });
}

/**
 * How many times the page has loaded the video's source afresh since watchNote(). This is the
 * page's own doing (watch.js asking for a new link), whatever the engine: Firefox and Safari
 * ask /w/<token>/video again for every piece of the file by themselves, so the server's count
 * of requests says nothing about it there.
 */
function sourceReloads(page) {
  return page.evaluate(() => window.__reloads || 0);
}

/** Every text the note has shown since watchNote(), in order (the empty string is "hidden"). */
function notesShown(page) {
  return page.evaluate(() => (window.__notes || []).filter((n) => n.text).map((n) => n.text));
}

/**
 * The player's box and the picture inside it. The <video> element is as wide as the column so
 * that the browser's own control bar has room for its buttons; the picture (9:16) sits in the
 * middle of it, letterboxed by the browser. `wrong` lists whatever would squash or crop it.
 */
function pictureInPlayer(page) {
  return page.evaluate(() => {
    const v = document.getElementById('v');
    const r = v.getBoundingClientRect();
    const frame = v.parentElement.getBoundingClientRect();
    const fit = getComputedStyle(v).objectFit;
    const wrong = [];
    if (!['contain', 'scale-down'].includes(fit)) wrong.push(`object-fit is "${fit}": the picture would be ${fit === 'cover' ? 'cropped' : 'stretched'} to fill the box`);
    if (!v.videoWidth || !v.videoHeight) wrong.push('the video has no picture size yet');
    const shape = v.videoWidth / v.videoHeight;
    if (v.videoWidth && Math.abs(shape - 9 / 16) > 0.01) wrong.push(`the clip is ${v.videoWidth}x${v.videoHeight}, not 9:16`);
    // The picture as the browser draws it: as big as fits in the box, shape kept.
    const scale = Math.min(r.width / (v.videoWidth || 9), r.height / (v.videoHeight || 16));
    const picture = { width: (v.videoWidth || 9) * scale, height: (v.videoHeight || 16) * scale };
    if (picture.width > r.width + 0.5 || picture.height > r.height + 0.5) wrong.push('the picture does not fit inside the player');
    if (Math.abs(r.width - frame.width) > 1 || Math.abs(r.left - frame.left) > 1) wrong.push(`the player is ${Math.round(r.width)} px wide in a ${Math.round(frame.width)} px column: its control bar loses that room`);
    if (Math.abs(r.height - frame.height) > 1) wrong.push(`the player (${Math.round(r.height)} px) and its black frame (${Math.round(frame.height)} px) are not the same height`);
    return {
      box: { x: r.left, y: r.top, width: r.width, height: r.height },
      column: frame.width,
      picture: { width: Math.round(picture.width), height: Math.round(picture.height) },
      fit,
      wrong,
    };
  });
}

// ---------------------------------------------------------------------------------------------
// Hands: finger, mouse, remote
// ---------------------------------------------------------------------------------------------

function focusedIs(page, selector) {
  return page.evaluate((sel) => {
    const target = document.querySelector(sel);
    const active = document.activeElement;
    if (!target || !active) return false;
    if (active === target) return true;
    // A star's label and its hidden radio button are one control to the customer.
    if (target.tagName === 'LABEL' && active.id && target.htmlFor === active.id) return true;
    if (active.tagName === 'LABEL' && target.id && active.htmlFor === target.id) return true;
    return false;
  }, selector);
}

/**
 * The key that moves the focus to the NEXT control for a keyboard user of this browser.
 * Safari on a Mac moves only between text boxes with a plain Tab unless the user has turned on
 * "Press Tab to highlight each item on a webpage"; Option+Tab is the key that reaches every
 * control (buttons, links, radio buttons), and it is what a Safari keyboard user presses. That
 * is Safari's long-standing default, not something the page decides. WebKit on Linux and
 * every other engine use the plain Tab.
 */
function tabKey(testInfo, { back = false } = {}) {
  const option = projectFacts(testInfo).engine === 'webkit' && process.platform === 'darwin';
  return `${option ? 'Alt+' : ''}${back ? 'Shift+' : ''}Tab`;
}

/** Tab (or Shift+Tab) until `selector` has the focus. Returns how many presses it took. */
async function tabTo(page, testInfo, selector, { max = 30, back = false } = {}) {
  const key = tabKey(testInfo, { back });
  for (let presses = 1; presses <= max; presses += 1) {
    await page.keyboard.press(key);
    if (await focusedIs(page, selector)) return presses;
  }
  throw new Error(`${key} never reached ${selector} in ${max} presses`);
}

/** A TV remote: press arrows until the focus is on `selector`. Fails if it cannot be reached. */
async function focusByArrows(page, selector, { max = 40 } = {}) {
  if (await focusedIs(page, selector)) return 0;
  const where = async () => page.evaluate((sel) => {
    const t = document.querySelector(sel).getBoundingClientRect();
    const a = document.activeElement && document.activeElement !== document.body ? document.activeElement.getBoundingClientRect() : null;
    return { target: { x: t.x + t.width / 2, y: t.y + t.height / 2 }, active: a ? { x: a.x + a.width / 2, y: a.y + a.height / 2 } : null };
  }, selector);
  for (let presses = 1; presses <= max; presses += 1) {
    const { target, active } = await where();
    let key = 'ArrowDown';
    if (active) {
      const dx = target.x - active.x;
      const dy = target.y - active.y;
      if (Math.abs(dy) > 12) key = dy > 0 ? 'ArrowDown' : 'ArrowUp';
      else key = dx > 0 ? 'ArrowRight' : 'ArrowLeft';
    }
    await page.keyboard.press(key);
    await page.waitForTimeout(60);
    if (await focusedIs(page, selector)) return presses;
  }
  throw new Error(`the remote's arrows never reached ${selector} in ${max} presses`);
}

/** Press a button, link, star or summary the way this project's customer does. */
async function press(page, testInfo, selector, { as = '' } = {}) {
  const input = as || projectFacts(testInfo).input;
  const target = page.locator(selector);
  if (input === 'dpad') {
    await focusByArrows(page, selector);
    await page.keyboard.press('Enter');
  } else if (input === 'touch') {
    await target.tap();
  } else {
    await target.click();
  }
}

// ---------------------------------------------------------------------------------------------
// Layout checks, all measured in the page
// ---------------------------------------------------------------------------------------------

/** '' when nothing makes the page scroll sideways, else what sticks out. */
function sidewaysScroll(page) {
  return page.evaluate(() => {
    const doc = document.documentElement;
    const wide = Math.max(doc.scrollWidth, document.body.scrollWidth);
    const out = [];
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (r.right > doc.clientWidth + 0.5 || r.left < -0.5) {
        const name = el.tagName.toLowerCase() + (el.id ? `#${el.id}` : '') + (el.htmlFor ? `[for=${el.htmlFor}]` : '');
        out.push(`${name} spans ${Math.round(r.left)}..${Math.round(r.right)} px of ${doc.clientWidth}`);
      }
    }
    if (wide <= doc.clientWidth && !out.length) return [];
    return out.length ? out : [`the page is ${wide} px wide in a ${doc.clientWidth} px window`];
  });
}

/** Every control a finger could aim at, with its size. Links inside a sentence are marked inline. */
function tapTargets(page) {
  return page.evaluate(() => {
    const out = [];
    const seen = new Set();
    const controls = document.querySelectorAll('a[href], button, summary, label[for], textarea, select, input:not([type=hidden])');
    for (const el of controls) {
      const style = getComputedStyle(el);
      if (style.visibility === 'hidden' || style.display === 'none') continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      // The radio buttons behind the stars are 1 px and invisible on purpose: the label is the target.
      if (el.tagName === 'INPUT' && el.type === 'radio' && document.querySelector(`label[for="${el.id}"]`)) continue;
      if (el.closest('details:not([open])') && el.tagName !== 'SUMMARY') continue;
      // A link in the middle of a sentence is as tall as the sentence (WCAG 2.5.8 excuses those).
      const inline = el.tagName === 'A' && style.display === 'inline' && !!el.parentElement
        && [...el.parentElement.childNodes].some((n) => n.nodeType === 3 && n.nodeValue.trim().length > 1);
      const text = (el.getAttribute('aria-label') || el.textContent || el.getAttribute('placeholder') || '').trim().replace(/\s+/g, ' ').slice(0, 40);
      const name = `${el.tagName.toLowerCase()} "${text}"`;
      if (seen.has(name)) continue;
      seen.add(name);
      out.push({ name, tag: el.tagName.toLowerCase(), width: Math.round(r.width * 10) / 10, height: Math.round(r.height * 10) / 10, inline });
    }
    return out;
  });
}

/** Every piece of visible text with its size in CSS px. */
function textSizes(page) {
  return page.evaluate(() => {
    const out = new Map();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.nodeValue.trim();
      if (!text) continue;
      const el = node.parentElement;
      if (!el || ['SCRIPT', 'STYLE', 'VIDEO', 'NOSCRIPT'].includes(el.tagName)) continue;
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (el.closest('details:not([open])') && !el.closest('summary')) continue;
      const size = parseFloat(style.fontSize);
      const where = el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? `.${el.className.split(' ')[0]}` : '');
      const key = `${where} ${size}`;
      if (!out.has(key)) out.set(key, { where, size, text: text.replace(/\s+/g, ' ').slice(0, 40) });
    }
    return [...out.values()];
  });
}

/** Is the whole of `selector` inside the first screen, with no scrolling? '' or what is not. */
async function insideFirstScreen(page, selector, name) {
  return page.evaluate(([sel, label]) => {
    const el = document.querySelector(sel);
    if (!el) return `${label} is not on the page`;
    const r = el.getBoundingClientRect();
    const h = window.innerHeight;
    const w = document.documentElement.clientWidth;
    const top = r.top + window.scrollY;
    const bottom = r.bottom + window.scrollY;
    if (bottom > h + 0.5) return `${label} ends ${Math.round(bottom - h)} px below the first screen (${Math.round(top)}..${Math.round(bottom)} px of ${h})`;
    if (r.left < -0.5 || r.right > w + 0.5) return `${label} is cut off at the side (${Math.round(r.left)}..${Math.round(r.right)} px of ${w})`;
    return '';
  }, [selector, name]);
}

/** A full-page picture of the page as this project shows it, named by project. */
async function shot(page, testInfo, suffix = '') {
  fs.mkdirSync(SHOTS, { recursive: true });
  // A frame of the clip in the picture, not the browser's "loading" ring, when there is a player.
  const hasPlayer = await page.waitForFunction(() => { const v = document.getElementById('v'); return !v ? 'none' : (v.readyState >= 2 && !v.seeking ? 'ready' : false); }, null, { timeout: 5000 })
    .then((handle) => handle.jsonValue()).catch(() => 'slow');
  if (hasPlayer !== 'none') await page.waitForTimeout(1200); // Chrome's ring fades out a moment after the frame is there
  const preview = process.env.ALL_ROWS_ON ? `PREVIEW-on-${process.env.ALL_ROWS_ON}--` : '';
  const name = preview + testInfo.project.name.replace(/[^A-Za-z0-9]+/g, '-').replace(/^-|-$/g, '') + (suffix ? `--${suffix}` : '');
  const file = path.join(SHOTS, `${name}.png`);
  await page.screenshot({ path: file, fullPage: true, animations: 'disabled' });
  return file;
}

// ---------------------------------------------------------------------------------------------
// Pixels: what the customer actually sees (forced colours and dark filters do not show in
// computed styles, only in the picture)
// ---------------------------------------------------------------------------------------------

const { PNG } = require('pngjs');

const colourDistance = (p, q) => Math.abs(p[0] - q[0]) + Math.abs(p[1] - q[1]) + Math.abs(p[2] - q[2]);

/** A picture of the area around one element, as { width, height, data } (RGBA). */
async function pixelsAround(page, selector, pad = 8) {
  const target = page.locator(selector).first();
  await target.scrollIntoViewIfNeeded();
  const box = await target.boundingBox();
  const size = page.viewportSize();
  const x = Math.max(0, Math.floor(box.x - pad));
  const y = Math.max(0, Math.floor(box.y - pad));
  const clip = { x, y, width: Math.min(size.width - x, Math.ceil(box.width + 2 * pad)), height: Math.min(size.height - y, Math.ceil(box.height + 2 * pad)) };
  const png = PNG.sync.read(await page.screenshot({ clip, animations: 'disabled', caret: 'hide' }));
  return { png, clip, box, scale: png.width / clip.width };
}

/** How many pixels differ between two pictures of the same area. */
function pixelsChanged(a, b, tolerance = 24) {
  let changed = 0;
  const n = Math.min(a.data.length, b.data.length);
  for (let i = 0; i < n; i += 4) {
    if (Math.abs(a.data[i] - b.data[i]) + Math.abs(a.data[i + 1] - b.data[i + 1]) + Math.abs(a.data[i + 2] - b.data[i + 2]) > tolerance) changed += 1;
  }
  return changed;
}

/** The two colours a picture is mostly made of: [background, ink], each [r, g, b]. */
function twoColours(png) {
  const counts = new Map();
  for (let i = 0; i < png.data.length; i += 4) {
    const key = ((png.data[i] >> 2) << 12) | ((png.data[i + 1] >> 2) << 6) | (png.data[i + 2] >> 2); // 6 bits a channel
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const rgb = (key) => [((key >> 12) & 63) << 2, ((key >> 6) & 63) << 2, (key & 63) << 2];
  const far = (p, q) => Math.abs(p[0] - q[0]) + Math.abs(p[1] - q[1]) + Math.abs(p[2] - q[2]);
  const sorted = [...counts.entries()].sort((p, q) => q[1] - p[1]).map(([key]) => rgb(key));
  const background = sorted[0];
  // The ink is the colour furthest from the background among the common ones (edges are blends).
  const ink = sorted.slice(1, 40).sort((p, q) => far(q, background) - far(p, background))[0] || background;
  return [background, ink];
}

/** The colour of a small square of the page around (x, y) in CSS px: the middle value of each channel. */
async function colourAt(page, x, y, half = 2) {
  const png = PNG.sync.read(await page.screenshot({ clip: { x: x - half, y: y - half, width: 2 * half, height: 2 * half }, animations: 'disabled', caret: 'hide' }));
  const channel = (offset) => {
    const values = [];
    for (let i = offset; i < png.data.length; i += 4) values.push(png.data[i]);
    values.sort((a, b) => a - b);
    return values[values.length >> 1];
  };
  return [channel(0), channel(1), channel(2)];
}

/**
 * The colour of a line drawn on a picture: the most common colour that is clearly neither of
 * `not` (the fill and the card behind it). null when there is no such line.
 */
function lineColour(png, not, least = 90) {
  const counts = new Map();
  for (let i = 0; i < png.data.length; i += 4) {
    const c = [png.data[i], png.data[i + 1], png.data[i + 2]];
    if (not.some((n) => colourDistance(c, n) < least)) continue;
    const key = ((c[0] >> 3) << 10) | ((c[1] >> 3) << 5) | (c[2] >> 3);
    const seen = counts.get(key) || { n: 0, sum: [0, 0, 0] };
    seen.n += 1; seen.sum[0] += c[0]; seen.sum[1] += c[1]; seen.sum[2] += c[2];
    counts.set(key, seen);
  }
  const best = [...counts.values()].sort((a, b) => b.n - a.n)[0];
  // A few stray blended pixels are not a line: ask for at least half a percent of the picture.
  if (!best || best.n < (png.data.length / 4) * 0.005) return null;
  return best.sum.map((v) => Math.round(v / best.n));
}

function luminance([r, g, b]) {
  const f = (c) => { const v = c / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

/** WCAG contrast ratio between two [r, g, b] colours. */
function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (hi + 0.05) / (lo + 0.05);
}


module.exports = {
  pixelsAround, pixelsChanged, twoColours, contrast, colourDistance, colourAt, lineColour, knownToFail, gapVerdict,
  test, expect, control, CONTROL, PORT,
  projectFacts, gapFor, checkGap,
  videoState, waitForMetadata, pressPlay, pressPause, waitForTime, isPlaying, isPaused,
  watchNote, notesShown, sourceReloads, pictureInPlayer,
  press, focusByArrows, focusedIs, tabKey, tabTo,
  sidewaysScroll, tapTargets, textSizes, insideFirstScreen, shot,
};
