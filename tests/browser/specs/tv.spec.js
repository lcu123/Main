// A television: the browser on a Samsung (Tizen), LG (webOS) or Fire TV, seen from the sofa,
// and a TV box that is handed the video by AirPlay or Chromecast. Runs on the TV projects.
//
// What a test machine cannot do: run the real Tizen, webOS or Silk engines (they are old
// Chromium builds, as far back as Chromium 53; the static checks cover what those lack), or
// send a real AirPlay or Cast stream. What is tested here is what those need from the page.

const {
  test, expect, checkGap, projectFacts,
  videoState, waitForMetadata, pressPlay, pressPause, waitForTime, press, textSizes, pictureInPlayer, insideFirstScreen,
} = require('../lib/harness');

test('with the remote in pointer mode: click the player, it plays; pick a star, send', async ({ page, clip, server }, testInfo) => {
  // Samsung, LG and Fire TV browsers start like this: the remote moves an arrow, OK is a click.
  await page.goto(clip.watch);
  await waitForMetadata(page);
  const how = await pressPlay(page, testInfo, { as: 'mouse', allowScript: false });
  expect(how, 'a click on the player did not start the video').not.toBe('');
  await waitForTime(page, 1);
  expect(await pressPause(page, testInfo, { as: 'mouse', allowScript: false })).not.toBe('');

  await press(page, testInfo, 'label[for="s5"]', { as: 'mouse' });
  await press(page, testInfo, '#score button[type=submit]', { as: 'mouse' });
  await expect(page.locator('#score .ok')).toHaveText('Thank you! You gave us 5 of 5 stars.');
  const state = await server.state(clip.token);
  expect(state.played).toBe(1);
  expect(state.score.stars).toBe(5);
});

test('the player can go full screen, so the video can fill the TV', async ({ page, clip }) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);
  const facts = await page.evaluate(() => {
    const v = document.getElementById('v');
    return {
      allowed: document.fullscreenEnabled || document.webkitFullscreenEnabled || false,
      controlsList: v.getAttribute('controlslist') || '',
    };
  });
  expect(facts.allowed, 'full screen is not allowed on this page').toBe(true);
  expect(facts.controlsList, 'the player must keep its full screen button').not.toContain('nofullscreen');
  // Two clicks on the picture is full screen in Chrome-family players, pointer mode included.
  const box = await page.locator('#v').boundingBox();
  await page.mouse.dblclick(box.x + box.width / 2, box.y + box.height / 3);
  await expect.poll(() => page.evaluate(() => !!document.fullscreenElement), { message: 'a double click did not take the player full screen' }).toBe(true);
  const filled = await page.evaluate(() => { const r = document.getElementById('v').getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height), window.innerWidth, window.innerHeight]; });
  expect(filled[1], 'in full screen the video should be as tall as the screen').toBeGreaterThanOrEqual(filled[3] - 2);
});

test('AirPlay and Chromecast: nothing on the page blocks them, and a TV box can fetch the video by itself', async ({ page, clip, server }) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);
  const facts = await page.evaluate(() => {
    const v = document.getElementById('v');
    return {
      airplay: v.getAttribute('x-webkit-airplay') || '',
      noRemote: v.hasAttribute('disableremoteplayback') || v.disableRemotePlayback === true,
      controlsList: v.getAttribute('controlslist') || '',
      src: v.getAttribute('src'),
    };
  });
  expect(facts.airplay, 'AirPlay must not be denied').not.toBe('deny');
  expect(facts.noRemote, 'remote playback (Cast, AirPlay) must not be switched off').toBe(false);
  expect(facts.controlsList).not.toContain('noremoteplayback');

  // The TV box gets the address and asks for it itself: no page, no cookies, no referrer.
  // The stand-in bucket has a throwaway certificate, so it is asked the way the box would
  // ask the real bucket, minus certificate checking.
  const first = await fetch(new URL(facts.src, clip.watch), { redirect: 'manual', headers: { Range: 'bytes=0-1' } });
  expect(first.status, 'the player address must redirect to the bucket for anyone holding the link').toBe(302);
  const location = first.headers.get('location');
  expect(location).toContain(server.info.bucket);
  const answer = await page.request.get(location, { headers: { Range: 'bytes=0-1023' }, ignoreHTTPSErrors: true });
  expect(answer.status()).toBe(206);
  expect(answer.headers()['content-type']).toBe('video/mp4');
  expect(answer.headers()['accept-ranges']).toBe('bytes');
  expect(answer.headers()['content-range']).toMatch(/^bytes 0-1023\/\d+$/);
});

test('from the sofa: the words are big enough to read at 3 metres', async ({ page, clip }, testInfo) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);
  // The 10-foot rule of thumb: text no smaller than 24 px on a 1080-line picture.
  const { height } = projectFacts(testInfo);
  const sizes = await textSizes(page);
  const small = sizes.map((t) => ({ ...t, at1080: (t.size / height) * 1080 })).filter((t) => t.at1080 < 24);
  checkGap(testInfo, 'tv-words-too-small',
    small.slice(0, 4).map((t) => `${t.where} "${t.text}" is ${t.size} px, as tall as ${t.at1080.toFixed(0)} px on a 1080-line screen`),
    'text is too small to read from the sofa');
});

test('from the sofa: the picture takes up most of the screen height', async ({ page, clip }, testInfo) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);
  const { width, height } = projectFacts(testInfo);
  const player = await pictureInPlayer(page);
  const share = player.picture.height / height;
  testInfo.annotations.push({ type: 'player size', description: `${Math.round(player.box.width)}x${Math.round(player.box.height)} px box, ${player.picture.width}x${player.picture.height} px picture: ${(share * 100).toFixed(0)}% of the screen height` });
  expect(player.wrong, 'the picture is squashed or cropped, or the player does not fill its column').toEqual([]);
  // 74% of the window on every TV size. A browser that reports the full 3840x2160 used to get
  // a 560 px column and a picture 43% of the screen tall; the column now widens with the window.
  expect(share, `the picture is only ${(share * 100).toFixed(0)}% of the screen height in a ${width}x${height} window`).toBeGreaterThanOrEqual(0.7);
  expect(await insideFirstScreen(page, '#v', 'the player'), 'the player must be whole on the first screen: a remote scrolls badly').toBe('');
  expect((await videoState(page)).error).toBe('');
});
