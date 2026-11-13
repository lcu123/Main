// How heavy the watch page is before the video: on a weak signal every kilobyte is a wait.
// The budget is what the page weighed on 2026-11-13 plus 25%. If a change pushes it over,
// either slim the change or raise the number here on purpose.

const { test, expect, waitForMetadata } = require('../lib/harness');

// Measured 2026-11-13, bytes as sent with no compression:
//   the watch page HTML (inline CSS included)   5,730  (varies by a few bytes with the address)
//   /static/watch.js                              611
//   /static/rubik.woff2                        35,324
//   /static/logo.png                           11,921
//   /static/favicon.png                         8,788
const TODAY = { html: 5730, 'watch.js': 611, 'rubik.woff2': 35324, 'logo.png': 11921, 'favicon.png': 8788 };
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
    expect.soft(bytes, `${name} is ${bytes} bytes; the budget is ${BUDGET[name]} (it was ${TODAY[name]} on 2026-11-13)`).toBeLessThanOrEqual(BUDGET[name]);
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
