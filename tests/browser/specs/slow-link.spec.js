// A slow connection: a phone with one bar, hotel Wi-Fi, a TV at the far end of the house.
// Three ways to be slow, because no single one works in every engine:
//   * the bucket itself trickles the video (serve.py: throttle, first_byte_ms): every engine;
//   * the page's own files are held back with route delays: every engine;
//   * the whole connection is throttled through Chrome's own network emulation: Chromium only.

const {
  test, expect, checkGap, projectFacts, videoState, waitForMetadata, pressPlay, waitForTime, press,
} = require('../lib/harness');

const hold = (ms) => async (route) => { await new Promise((r) => setTimeout(r, ms)); await route.continue(); };

test('a slow bucket: the page is usable at once, the video starts when it can, no false alarm', async ({ page, server, problems }, testInfo) => {
  // 60 KB a second for a file that needs 340 KB a second: a real visit video on a poor 3G signal.
  const clip = await server.newVideo({ file: 'stretched', throttle: 60000, first_byte_ms: 3000 });
  const started = Date.now();
  await page.goto(clip.watch, { waitUntil: 'domcontentloaded' });
  // The page does not wait for the video: heading, stars and Send are there straight away.
  await expect(page.locator('h1')).toBeVisible();
  await expect(page.locator('#score button[type=submit]')).toBeVisible();
  const usable = Date.now() - started;
  testInfo.annotations.push({ type: 'slow bucket', description: `page usable after ${usable} ms` });
  expect(usable, 'the words and the score form must not wait for the video').toBeLessThan(2500);
  expect((await videoState(page)).ready, 'the bucket has not sent a byte yet').toBe(0);
  const formBefore = (await page.locator('#score').boundingBox()).y;

  await waitForMetadata(page, 30_000);
  // The player keeps its place while the video is on its way, so the form below does not jump under a finger.
  const formAfter = (await page.locator('#score').boundingBox()).y;
  const jump = Math.round(Math.abs(formAfter - formBefore));
  checkGap(testInfo, 'page-jumps-when-video-loads', jump <= 4 ? '' : `the score form moves ${jump} px when the video's details arrive`, 'the page jumps when the video loads');
  testInfo.annotations.push({ type: 'slow bucket', description: `player knew the video's length after ${Date.now() - started} ms` });
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await waitForTime(page, 0.5, 40_000);
  testInfo.annotations.push({ type: 'slow bucket', description: `half a second had played after ${Date.now() - started} ms` });
  const s = await videoState(page);
  expect(s.error).toBe('');
  expect(s.noteShown, 'slow is not broken: the "did not load" note must stay hidden').toBe(false);
  expect(await problems.list()).toEqual([]);
});

test('slow page files: words show before the font arrives, and the player works before the script', async ({ page, clip, server }, testInfo) => {
  await page.route('**/static/rubik.woff2*', hold(2500));
  await page.route('**/static/logo.png*', hold(2500));
  await page.route('**/static/watch.js*', hold(9000)); // long enough that play is pressed first in every engine
  await page.goto(clip.watch, { waitUntil: 'commit' });
  // font-display:swap: the heading is readable in the fallback font while Rubik is still on its way.
  await expect(page.locator('h1')).toBeVisible({ timeout: 2000 });
  const early = await page.evaluate(() => ({
    width: document.querySelector('h1').getBoundingClientRect().width,
    rubik: document.fonts.check('700 26px Rubik'),
    logoSpace: document.querySelector('header.top img').getBoundingClientRect().width,
  }));
  expect(early.width, 'the heading has no text while the font loads').toBeGreaterThan(50);
  expect(early.logoSpace, 'the logo keeps its place while it loads, so nothing jumps').toBe(58);

  // The customer presses play before watch.js has arrived.
  await waitForMetadata(page);
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await waitForTime(page, 0.5);
  expect((await server.state(clip.token)).hits.played, 'watch.js is still held back, so nothing can have been posted yet').toBe(0);
  await page.waitForLoadState('load', { timeout: 30_000 });
  await page.waitForTimeout(1500);
  expect((await videoState(page)).time, 'the video should have been playing all along').toBeGreaterThan(0.5);
  const state = await server.state(clip.token);
  checkGap(testInfo, 'play-before-script-is-not-counted', state.played === 1 ? '' : `the video is playing but the server recorded ${state.played} plays`,
    'a play that started before watch.js loaded was never reported');
});

test('Chrome on a throttled connection (400 ms delay, 500 kbit/s): loads, plays', async ({ page, clip, problems }, testInfo) => {
  test.skip(projectFacts(testInfo).engine !== 'chromium', 'network throttling through the browser itself exists only in Chromium');
  const session = await page.context().newCDPSession(page);
  await session.send('Network.enable');
  await session.send('Network.emulateNetworkConditions', { offline: false, latency: 400, downloadThroughput: 62500, uploadThroughput: 31250 });
  const started = Date.now();
  await page.goto(clip.watch);
  await waitForMetadata(page, 30_000);
  testInfo.annotations.push({ type: 'throttled Chrome', description: `page and video metadata after ${Date.now() - started} ms` });
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await waitForTime(page, 1, 40_000);
  expect((await videoState(page)).noteShown).toBe(false);
  await press(page, testInfo, 'label[for="s5"]');
  await press(page, testInfo, '#score button[type=submit]');
  await expect(page.locator('#score .ok')).toContainText('5 of 5 stars', { timeout: 20_000 });
  expect(await problems.list()).toEqual([]);
});
