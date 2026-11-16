// Script switched off in the browser. The page is plain HTML on purpose: the player is the
// browser's own, the score and "not my home" are ordinary forms. Only the "played" ping
// needs script.

const {
  test, expect, knownToFail, videoState, waitForMetadata, pressPlay, waitForTime, press,
} = require('../lib/harness');

test.use({ javaScriptEnabled: false });

test('with script off the video still plays from the customer\'s own tap or click', async ({ page, clip, server }, testInfo) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);
  // Safari draws its player controls with script of its own; with script off it may draw none.
  await knownToFail(testInfo, 'script-off-player-cannot-be-started', async () => {
    const how = await pressPlay(page, testInfo, { allowScript: false });
    expect(how, 'with script off, no tap, click or key started the video').not.toBe('');
    await waitForTime(page, 1);
  });
  expect((await videoState(page)).error).toBe('');
  // Nobody is told: the "played" ping is the one thing on the page that is script.
  const state = await server.state(clip.token);
  testInfo.annotations.push({ type: 'script off', description: `the server recorded ${state.played} plays: a customer with script off is never counted as having watched` });
  expect(state.hits.played).toBe(0);
});

test('with script off a score is still sent and saved', async ({ page, clip, server }, testInfo) => {
  await page.goto(clip.watch);
  await press(page, testInfo, 'label[for="s3"]');
  await press(page, testInfo, '#score button[type=submit]');
  await expect(page.locator('#score .ok')).toHaveText('Thank you! You gave us 3 of 5 stars.');
  expect((await server.state(clip.token)).score).toEqual({ stars: 3, comment: '' });
});

test('with script off "This is not my home" still takes the video down', async ({ page, clip, server }, testInfo) => {
  await page.goto(clip.watch);
  await press(page, testInfo, 'details summary');
  await press(page, testInfo, 'details button[type=submit]');
  await expect(page.locator('h1')).toHaveText('Thank you');
  expect((await server.state(clip.token)).state).toBe('pulled');
});

test('with script off the "did not load" note stays out of the way', async ({ page, clip }) => {
  await page.goto(clip.watch);
  await expect(page.locator('#verr')).toBeHidden();
});

test('with script off a link under the player opens the video by itself', async ({ page, clip, server }, testInfo) => {
  // No script means no retry and, in Safari, no buttons on the player at all. This link is
  // the way out: the video in the browser's own full-window player. It only exists with
  // script off (it is inside <noscript>).
  await page.goto(clip.watch);
  const link = page.getByRole('link', { name: 'Open the video by itself' });
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute('href', `/w/${clip.token}/video`);
  const box = await link.boundingBox();
  const player = await page.locator('#v').boundingBox();
  expect(box.y, 'the link belongs right under the player').toBeGreaterThanOrEqual(player.y + player.height - 1);
  expect(box.y - (player.y + player.height), 'the link belongs right under the player').toBeLessThan(80);
  const size = await link.evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
  expect(size).toBeGreaterThanOrEqual(16);
  expect(box.height, `the link is ${Math.round(box.height)} px tall: smaller than a finger needs`).toBeGreaterThanOrEqual(44);

  // The address is the player's own: it sends the browser on to the bucket with a fresh link.
  const hop = await page.request.get(clip.video, { maxRedirects: 0 });
  expect(hop.status()).toBe(302);
  expect(hop.headers().location).toContain(server.info.bucket);
  const before = (await server.state(clip.token)).hits.video;
  const video = page.waitForResponse((r) => r.url().startsWith(server.info.bucket), { timeout: 20_000 });
  await link.click({ noWaitAfter: true });
  const answer = await video;
  expect([200, 206], `the bucket answered ${answer.status()} to the browser opening the video by itself`).toContain(answer.status());
  expect(answer.headers()['content-type']).toBe('video/mp4');
  expect((await server.state(clip.token)).hits.video, 'the link should have gone through /w/<token>/video').toBeGreaterThan(before);
});
