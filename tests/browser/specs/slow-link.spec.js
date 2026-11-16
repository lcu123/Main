// A slow connection: a phone with one bar, hotel Wi-Fi, a TV at the far end of the house.
// Three ways to be slow, because no single one works in every engine:
//   * the bucket itself trickles the video (serve.py: throttle, first_byte_ms): every engine;
//   * the page's own files are held back with route delays: every engine;
//   * the whole connection is throttled through Chrome's own network emulation: Chromium only.

const {
  test, expect, projectFacts, videoState, waitForMetadata, pressPlay, waitForTime, press,
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
  expect(Math.round(Math.abs(formAfter - formBefore)), 'the score form moved when the video\'s details arrived: the page jumped under the customer\'s finger').toBeLessThanOrEqual(4);
  testInfo.annotations.push({ type: 'slow bucket', description: `player knew the video's length after ${Date.now() - started} ms` });
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await waitForTime(page, 0.5, 40_000);
  testInfo.annotations.push({ type: 'slow bucket', description: `half a second had played after ${Date.now() - started} ms` });
  const s = await videoState(page);
  expect(s.error).toBe('');
  expect(s.noteShown, 'slow is not broken: the "did not load" note must stay hidden').toBe(false);
  expect(await problems.list()).toEqual([]);
});

test('slow page files: words show before the font arrives, and a play pressed before the script arrives is still counted', async ({ page, clip, server }, testInfo) => {
  let letTheScriptThrough;
  const scriptHeld = new Promise((resolve) => { letTheScriptThrough = resolve; });
  await page.route('**/static/rubik.woff2*', hold(2500));
  await page.route('**/static/logo.png*', hold(2500));
  // watch.js stays on its way until this test has pressed play: the same order in every engine.
  await page.route('**/static/watch.js*', async (route) => { await scriptHeld; await route.continue(); });
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

  // The player is the browser's own: it works before watch.js has arrived, and the customer presses play.
  await waitForMetadata(page);
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await waitForTime(page, 0.5);
  expect((await server.state(clip.token)).hits.played, 'watch.js is still held back, so nothing can have been posted yet').toBe(0);

  // Now the script arrives and finds a video that is already playing. That customer is
  // watching: without the check at the end of watch.js the server was never told, and the
  // office saw "not watched" for everyone on a slow connection who pressed play quickly.
  letTheScriptThrough();
  await page.waitForLoadState('load', { timeout: 30_000 });
  await expect.poll(async () => (await server.state(clip.token)).played, { message: 'a play that started before watch.js arrived was never reported' }).toBe(1);
  const s = await videoState(page);
  expect(s.error).toBe('');
  expect(s.time, 'the video should have been playing all along').toBeGreaterThan(0.5);
  await page.waitForTimeout(1200);
  expect((await server.state(clip.token)).hits.played, 'and it must be told once, not once per event').toBe(1);
});

test.describe('a tall window (a tablet held upright)', () => {
  // The first CI run saw the old player grow when the video's details arrived on an iPad in
  // WebKit, pushing the score form down. The box is now fixed by the page's own CSS (the full
  // column wide, 9:16 or 74% of the window tall, whichever is smaller), in every engine.
  test.use({ viewport: { width: 810, height: 1080 } });
  test('the player keeps its place and size while the video is on its way', async ({ page, server }) => {
    const clip = await server.newVideo({ first_byte_ms: 3000 });
    await page.goto(clip.watch, { waitUntil: 'domcontentloaded' });
    expect((await videoState(page)).ready, 'the bucket has not sent a byte yet').toBe(0);
    const before = { player: await page.locator('#v').boundingBox(), form: await page.locator('#score').boundingBox() };
    await waitForMetadata(page, 30_000);
    const after = { player: await page.locator('#v').boundingBox(), form: await page.locator('#score').boundingBox() };
    expect(Math.abs(after.form.y - before.form.y), 'the score form moved when the video\'s details arrived').toBeLessThanOrEqual(4);
    expect(Math.abs(after.player.width - before.player.width), 'the player changed width when the video\'s details arrived').toBeLessThanOrEqual(2);
    expect(Math.abs(after.player.height - before.player.height), 'the player changed height when the video\'s details arrived').toBeLessThanOrEqual(2);
    expect(before.player.height, 'before the video arrives the player must already be a tall box, not a 150 px band').toBeGreaterThan(300);
  });
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
