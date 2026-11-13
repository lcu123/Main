// LIVE mode: a real watch link on the real server, the real bucket, read-only.
//
//     LIVE_WATCH_URL=https://.../w/<token> npx playwright test
//
// When LIVE_WATCH_URL is set this is the ONLY spec that runs, on every project. It opens the
// page, checks it fits, and plays the video. It never presses Send, never opens "This is not
// my home", never posts anything. The one request that is not a GET is the page's own
// "played" ping when the video starts, exactly as for a customer: the office will see this
// video as watched.

const {
  test, expect, videoState, waitForMetadata, pressPlay, waitForTime, sidewaysScroll, insideFirstScreen, shot,
} = require('../lib/harness');

const URL_UNDER_TEST = process.env.LIVE_WATCH_URL || '';

test.skip(!URL_UNDER_TEST, 'set LIVE_WATCH_URL to run the live checks');

test('LIVE: the real page opens with nothing wrong and fits the screen', async ({ page, problems }, testInfo) => {
  const posts = [];
  page.on('request', (request) => { if (request.method() !== 'GET') posts.push(`${request.method()} ${new URL(request.url()).pathname.replace(/[0-9a-f]{32}/, 'TOKEN')}`); });
  const response = await page.goto(URL_UNDER_TEST);
  expect(response.status(), 'the live link did not open: is the video still up?').toBe(200);
  expect(response.headers()['content-security-policy']).toContain("default-src 'none'");
  expect(response.headers()['cache-control']).toBe('no-store');
  await expect(page.locator('h1')).toContainText('A video from');
  await waitForMetadata(page, 30_000);
  expect.soft(await sidewaysScroll(page), 'the page scrolls sideways').toEqual([]);
  expect.soft(await insideFirstScreen(page, 'header.top h1', 'the heading')).toBe('');
  await shot(page, testInfo, 'LIVE');
  expect(posts, 'opening the page must post nothing').toEqual([]);
  expect(await problems.list()).toEqual([]);
});

test('LIVE: the real video plays through the real bucket', async ({ page, problems }, testInfo) => {
  const posts = [];
  const media = [];
  page.on('request', (request) => { if (request.method() !== 'GET') posts.push(`${request.method()} ${new URL(request.url()).pathname.replace(/[0-9a-f]{32}/, 'TOKEN')}`); });
  page.on('response', (response) => {
    const type = response.headers()['content-type'] || '';
    if (type.startsWith('video/') || response.url().includes('/video')) media.push(`${response.status()} ${new URL(response.url()).host}`);
  });
  await page.goto(URL_UNDER_TEST);
  const meta = await waitForMetadata(page, 30_000);
  expect(meta.width).toBeGreaterThan(0);
  expect(meta.height).toBeGreaterThan(0);
  testInfo.annotations.push({ type: 'live video', description: `${meta.width}x${meta.height}, ${meta.duration.toFixed(1)} s` });

  const how = await pressPlay(page, testInfo);
  testInfo.annotations.push({ type: 'play pressed by', description: how || 'nothing worked' });
  expect(how, 'nothing started the live video').not.toBe('');
  await waitForTime(page, 1.5, 40_000);
  const s = await videoState(page);
  expect(s.error).toBe('');
  expect(s.noteShown).toBe(false);
  testInfo.annotations.push({ type: 'live media responses', description: media.join(', ') });

  // Read-only: nothing but the page's own "played" ping.
  expect(posts.filter((p) => p !== 'POST /w/TOKEN/played'), 'live mode must not post anything but the "played" ping').toEqual([]);
  expect(await problems.list()).toEqual([]);
});
