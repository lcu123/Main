// How heavy the watch page is before the video: on a weak signal every kilobyte is a wait.
// The budget is what the page weighs today plus 25%. If a change pushes it over, either slim
// the change or raise the number here on purpose, and say why.

const { test, expect, waitForMetadata } = require('../lib/harness');

// Bytes as sent with no compression.
//                                              2026-11-13 a.m.   p.m. (fix round)   evening (second round)
//   the watch page HTML (inline CSS included)        5,730            9,175            10,224     (a few bytes more or less with the address)
//   /static/watch.js                                   611            5,728            10,238
//   /static/rubik.woff2                             35,324           35,324            35,324
//   /static/logo.png                                11,921           11,921            11,921
//   /static/favicon.png                              8,788            8,788             8,788
// What the 8.5 KB bought: the five stars are drawings in the page (they shrink, they are
// hollow or filled, no device font is involved); the player has a box an old browser can
// reserve; the script fetches a fresh link when the old one has died, stops after a bounded
// number of tries, says so in a note that is a button, says when the signal is weak, and
// makes a TV remote's OK, play and pause keys and the arrows on the stars work. The font and
// the two pictures did not change and keep their old limits.
// What the second round's 5 KB bought, nearly all of it in the script: in Chrome a page left
// open past the life of its video link gets a new link in two seconds, not thirty; the place
// is kept through a failed retry; the video carries on by itself when the signal is back and
// never swaps the page for the browser's "no internet" page; a dead player leaves full screen
// and brings its note into view; the note keeps the focus of a keyboard or a remote; a
// dropped "played" message is sent again; a double tap is one press. Half the script's bytes
// are its comments; it is served as written, with no build step, on purpose.
const TODAY = { html: 10224, 'watch.js': 10238, 'rubik.woff2': 35324, 'logo.png': 11921, 'favicon.png': 8788 };
const BUDGET = Object.fromEntries(Object.entries(TODAY).map(([name, bytes]) => [name, Math.round(bytes * 1.25)]));
const TOTAL_BUDGET = Math.round(Object.values(TODAY).reduce((a, b) => a + b, 0) * 1.25);

test('the page without the video stays inside its byte budget', async ({ request, clip }, testInfo) => {
  const html = await (await request.get(clip.watch)).text();
  const version = (/watch\.js\?v=([^"]+)"/.exec(html) || [])[1];
  expect(version, 'the page should load /static/watch.js?v=...').toBeTruthy();
  const sizes = { html: Buffer.byteLength(html) };
  for (const name of ['watch.js', 'rubik.woff2', 'logo.png', 'favicon.png']) {
    const response = await request.get(`/static/${name}`);
    expect(response.status(), `/static/${name}`).toBe(200);
    expect(response.headers()['cache-control'], `/static/${name} should be kept by the browser`).toContain('max-age=2592000');
    sizes[name] = (await response.body()).length;
  }
  const total = Object.values(sizes).reduce((a, b) => a + b, 0);
  testInfo.annotations.push({ type: 'page weight', description: `${JSON.stringify(sizes)} total ${total} bytes (budget ${TOTAL_BUDGET})` });
  for (const [name, bytes] of Object.entries(sizes)) {
    expect.soft(bytes, `${name} is ${bytes} bytes; the budget is ${BUDGET[name]} (it was ${TODAY[name]} when the budget was set)`).toBeLessThanOrEqual(BUDGET[name]);
  }
  expect(total, `the page weighs ${total} bytes without the video; the budget is ${TOTAL_BUDGET}`).toBeLessThanOrEqual(TOTAL_BUDGET);
});

test('this browser fetches each page file once, and nothing from anywhere else', async ({ page, clip, server }, testInfo) => {
  const asked = [];
  page.on('request', (request) => asked.push(request.url()));
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(500);

  const base = new URL(clip.watch).origin;
  const bucket = new URL(server.info.bucket).origin;
  const paths = asked.filter((u) => u.startsWith(base)).map((u) => new URL(u).pathname.replace(clip.token, 'TOKEN'));
  const count = (p) => paths.filter((x) => x === p).length;
  testInfo.annotations.push({ type: 'requests', description: paths.join(', ') });
  expect(count('/w/TOKEN'), 'the page itself').toBe(1);
  // A preload that does not match the @font-face rule makes a browser download the font twice.
  expect(count('/static/rubik.woff2'), 'the font should be fetched exactly once').toBe(1);
  expect(count('/static/watch.js')).toBe(1);
  expect(count('/static/logo.png')).toBe(1);
  expect(count('/static/favicon.png'), 'the icon is fetched at most once').toBeLessThanOrEqual(1);
  const elsewhere = asked.filter((u) => !u.startsWith(base) && !u.startsWith(bucket) && !u.startsWith('data:') && !u.startsWith('blob:'));
  expect(elsewhere, 'the page asked a third party for something').toEqual([]);
});
