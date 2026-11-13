// The watch page, on every project in playwright.config.js: every engine, every phone, the
// iPads, the desktop sizes and the TVs. Each test films its own fresh video, so they all run
// at the same time without touching each other.

const {
  test, expect, checkGap, knownToFail, projectFacts,
  videoState, waitForMetadata, pressPlay, pressPause, waitForTime,
  press, sidewaysScroll, tapTargets, textSizes, insideFirstScreen, shot,
} = require('../lib/harness');

const LONG_ADDRESS = '12345 North Whispering Pines Boulevard, Apartment 204';

/** Which font this machine drew the star with (the star is not in the page's own font). Chromium can say. */
async function starFont(page, testInfo) {
  if (projectFacts(testInfo).engine !== 'chromium') return '';
  try {
    const session = await page.context().newCDPSession(page);
    await session.send('DOM.enable');
    await session.send('CSS.enable');
    const { root } = await session.send('DOM.getDocument');
    const { nodeId } = await session.send('DOM.querySelector', { nodeId: root.nodeId, selector: 'label[for="s1"]' });
    const { fonts } = await session.send('CSS.getPlatformFontsForNode', { nodeId });
    await session.detach();
    return `, drawn with ${fonts.map((f) => `${f.familyName}${f.isCustomFont ? ' (the page\'s font)' : ' (a font of this machine)'}`).join(' + ')}`;
  } catch (error) {
    return '';
  }
}

test('opens with nothing wrong: no errors, the logo, the font, the private-page headers', async ({ page, clip, problems }, testInfo) => {
  const response = await page.goto(clip.watch);
  expect(response.status()).toBe(200);
  await expect(page).toHaveTitle(/Your service video/);
  await expect(page.locator('h1')).toContainText(`A video from ${clip.tech} at ${clip.address}`);

  const headers = response.headers();
  expect(headers['content-security-policy'], 'the page must keep its Content-Security-Policy').toContain("default-src 'none'");
  expect(headers['cache-control']).toBe('no-store');
  expect(headers['x-robots-tag']).toContain('noindex');
  expect(headers['referrer-policy']).toBe('no-referrer');
  expect(headers['x-frame-options']).toBe('DENY');

  await waitForMetadata(page);
  const loaded = await page.evaluate(async () => {
    await document.fonts.ready;
    const logo = document.querySelector('header.top img');
    return { logo: logo.complete && logo.naturalWidth > 0, font: document.fonts.check('700 26px Rubik') };
  });
  expect(loaded.logo, 'the logo did not load').toBe(true);
  expect(loaded.font, 'the Rubik font did not load').toBe(true);

  await shot(page, testInfo); // screenshots/<project>.png: one picture of the page per project
  expect(await problems.list()).toEqual([]);
});

test('fits the screen: no sideways scroll; heading, player and controls in the first screen', async ({ page, clip, problems }, testInfo) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);

  checkGap(testInfo, 'stars-wider-than-the-screen', await sidewaysScroll(page), 'the page scrolls sideways');
  expect.soft(await insideFirstScreen(page, 'header.top h1', 'the heading')).toBe('');
  // The player's own controls sit along the bottom edge of the <video> box.
  checkGap(testInfo, 'player-below-first-screen', await insideFirstScreen(page, '#v', 'the player'), 'the player is not all in the first screen');

  const star = await page.locator('label[for="s1"]').boundingBox();
  testInfo.annotations.push({ type: 'stars', description: `each star is ${Math.round(star.width)} px wide here${await starFont(page, testInfo)}` });

  const box = await page.locator('#v').boundingBox();
  const { width, height } = projectFacts(testInfo);
  testInfo.annotations.push({ type: 'player size', description: `${Math.round(box.width)}x${Math.round(box.height)} px in a ${width}x${height} window` });
  expect.soft(box.width, 'the player is too narrow to watch').toBeGreaterThanOrEqual(120);
  // Portrait video: the box keeps the clip's 9:16 shape, so nothing is squashed or cropped.
  expect.soft(box.width / box.height).toBeGreaterThan(0.54);
  expect.soft(box.width / box.height).toBeLessThan(0.58);
  expect(await problems.list()).toEqual([]);
});

test('a long address wraps instead of breaking the page', async ({ page, server }, testInfo) => {
  const clip = await server.newVideo({ address: LONG_ADDRESS });
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await expect(page.locator('h1')).toContainText(LONG_ADDRESS);
  checkGap(testInfo, 'stars-wider-than-the-screen', await sidewaysScroll(page), 'the page scrolls sideways');
  expect.soft(await insideFirstScreen(page, 'header.top h1', 'the heading')).toBe('');
  const { height } = projectFacts(testInfo);
  // Whichever of the two is listed for this window is the one that must still be true.
  const gap = height < 331 ? 'player-below-first-screen' : 'long-address-pushes-player-down';
  checkGap(testInfo, gap, await insideFirstScreen(page, '#v', 'the player'), 'the player is not all in the first screen');
  await shot(page, testInfo, 'long-address');
});

test('the video plays, pauses, seeks and plays again, and the server is told "played" once', async ({ page, clip, server, problems }, testInfo) => {
  await page.goto(clip.watch);
  const meta = await waitForMetadata(page);
  expect(meta.duration).toBeGreaterThan(7);
  expect(meta.duration).toBeLessThan(9);
  expect([meta.width, meta.height], 'a frame with real size').toEqual([720, 1280]);
  expect((await server.state(clip.token)).played, 'opening the page must not count as watched').toBe(0);

  // Play.
  const how = await pressPlay(page, testInfo);
  testInfo.annotations.push({ type: 'play pressed by', description: how || 'nothing worked' });
  expect(how, 'nothing started the video: not a tap, a click, a key or even script').not.toBe('');
  await waitForTime(page, 1.2);
  expect((await videoState(page)).error).toBe('');

  // Pause: time stops.
  const paused = await pressPause(page, testInfo);
  testInfo.annotations.push({ type: 'pause pressed by', description: paused || 'nothing worked' });
  expect(paused).not.toBe('');
  const stoppedAt = (await videoState(page)).time;
  await page.waitForTimeout(400);
  expect((await videoState(page)).time).toBeCloseTo(stoppedAt, 1);

  // Seek near the end (the bucket must answer byte ranges), then play to the end.
  await page.evaluate(() => { document.getElementById('v').currentTime = 6.6; });
  await expect.poll(async () => (await videoState(page)).seeking, { message: 'the seek never finished' }).toBe(false);
  expect((await videoState(page)).time).toBeGreaterThan(6);
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await expect.poll(async () => (await videoState(page)).ended, { message: 'the video did not reach its end', timeout: 20_000 }).toBe(true);

  // Play again from the start.
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await expect.poll(async () => { const s = await videoState(page); return !s.ended && s.time > 0.3 && s.time < 5; },
    { message: 'pressing play after the end did not start the video again' }).toBe(true);

  const after = await videoState(page);
  expect(after.error).toBe('');
  expect(after.noteShown, 'the "did not load" note must stay hidden when the video plays').toBe(false);

  // What the server saw.
  await expect.poll(async () => (await server.state(clip.token)).played, { message: 'the server was never told "played"' }).toBe(1);
  const state = await server.state(clip.token);
  expect(state.hits.played, 'play, pause, seek and replay must tell the server exactly once').toBe(1);
  expect(state.viewCount).toBe(1);
  expect(state.bucket.length, 'the browser never reached the bucket through the redirect').toBeGreaterThan(0);
  expect(state.bucket.filter((r) => r.status >= 400), 'the bucket refused a request').toEqual([]);
  testInfo.annotations.push({ type: 'requests', description: `page ${state.hits.page}, /video ${state.hits.video}, bucket ${state.bucket.map((r) => r.range || 'whole file').join(', ')}` });
  // The real server allows 120 hits a minute per address; one viewing must stay far below it.
  expect(state.hits.all, 'one viewing made too many requests to the server').toBeLessThan(40);
  expect(await problems.list()).toEqual([]);
});

test('a score: pick a star, send, see the thank-you; it is saved once and stays', async ({ page, clip, server, problems }, testInfo) => {
  await page.goto(clip.watch);
  await expect(page.locator('#score h2')).toHaveText('How did we do?');

  await press(page, testInfo, 'label[for="s4"]');
  await expect(page.locator('#s4')).toBeChecked();
  // The customer sees it: stars 1 to 4 turn gold, star 5 does not.
  const colours = await page.evaluate(() => [1, 2, 3, 4, 5].map((n) => getComputedStyle(document.querySelector(`label[for="s${n}"]`)).color));
  expect(new Set(colours.slice(0, 4)).size, 'stars 1 to 4 should share one colour').toBe(1);
  expect(colours[4], 'star 5 must not look selected').not.toBe(colours[0]);

  await press(page, testInfo, '#score button[type=submit]');
  await expect(page.locator('#score .ok')).toHaveText('Thank you! You gave us 4 of 5 stars.');
  expect(page.url()).toContain('?thanks=1#score');
  await expect(page.locator('#score .ok'), 'after sending, the thank-you must be on screen').toBeInViewport();
  await expect(page.locator('#score form')).toHaveCount(0);

  let state = await server.state(clip.token);
  expect(state.score).toEqual({ stars: 4, comment: '' });
  expect(state.hits.score, 'one press of Send must post once').toBe(1);
  expect(state.scoresSaved).toBe(1);

  // Opening the link again later: the score is there and there is nothing left to send.
  await page.goto(clip.watch);
  await expect(page.locator('#score .ok')).toHaveText('You gave us 4 of 5 stars.');
  await expect(page.locator('#score input, #score button')).toHaveCount(0);
  state = await server.state(clip.token);
  expect(state.scoresSaved).toBe(1);
  expect(await problems.list()).toEqual([]);
});

test('after sending, going Back does not let a second score replace the first', async ({ page, clip, server }, testInfo) => {
  await page.goto(clip.watch);
  await press(page, testInfo, 'label[for="s5"]');
  await press(page, testInfo, '#score button[type=submit]');
  await expect(page.locator('#score .ok')).toContainText('5 of 5 stars');

  await page.goBack();
  await page.waitForLoadState('load');
  // Either the browser asked the server again (the form is gone), or it showed the copy it kept.
  // The page is sent with Cache-Control: no-store so that it is asked again; a browser that
  // keeps the old form anyway (its back/forward cache) lets the customer send a second score.
  await knownToFail(testInfo, 'back-then-send-replaces-the-score', async () => {
    const formIsBack = await page.locator('#score button[type=submit]').isVisible().catch(() => false);
    testInfo.annotations.push({ type: 'after Back', description: formIsBack ? 'the old form was shown again' : 'the saved score was shown, no form' });
    if (formIsBack) {
      await press(page, testInfo, 'label[for="s1"]');
      await press(page, testInfo, '#score button[type=submit]');
      await page.waitForLoadState('load');
    }
    const state = await server.state(clip.token);
    expect(state.score.stars, 'a second send after Back changed the score the customer already gave').toBe(5);
  });
});

test('Send with no star picked sends nothing', async ({ page, clip, server }, testInfo) => {
  await page.goto(clip.watch);
  await press(page, testInfo, '#score button[type=submit]');
  await page.waitForTimeout(300);
  await expect(page.locator('#score h2')).toHaveText('How did we do?');
  const state = await server.state(clip.token);
  expect(state.hits.score, 'the browser must stop a send with no star').toBe(0);
  expect(state.score).toBeNull();
});

test('"This is not my home" opens, and confirming takes the video down for good', async ({ page, clip, server, problems }, testInfo) => {
  await page.goto(clip.watch);
  const confirm = page.locator('details button[type=submit]');
  await expect(confirm).toBeHidden();

  await press(page, testInfo, 'details summary');
  await expect(confirm).toBeVisible();
  await expect(page.locator('details p')).toContainText('we will take it down right away');
  checkGap(testInfo, 'stars-wider-than-the-screen', await sidewaysScroll(page), 'the page scrolls sideways with "not my home" open');

  await press(page, testInfo, 'details button[type=submit]');
  await expect(page.locator('h1')).toHaveText('Thank you');
  await expect(page.locator('main')).toContainText('We took this video down');
  await expect(page.locator('main a.btn')).toHaveAttribute('href', `tel:${server.info.officePhone}`);

  const state = await server.state(clip.token);
  expect(state.state).toBe('pulled');
  expect(state.watchable).toBe(false);
  expect(state.notMyHome).toBe(1);
  expect(state.hits.notMyHome).toBe(1);

  // The link is dead from now on, and so is the address the player used.
  problems.allow(/status of 410/);
  const again = await page.goto(clip.watch);
  expect(again.status()).toBe(410);
  await expect(page.locator('h1')).toHaveText('This video is no longer available.');
  await expect(page.locator('video')).toHaveCount(0);
  const stream = await page.request.get(clip.video, { maxRedirects: 0 });
  expect(stream.status(), 'the player address must stop redirecting to the bucket').toBe(410);
  expect(await problems.list()).toEqual([]);
});

test('every control is big enough for a finger (44x44) and no text is under 16 px', async ({ page, clip, server }, testInfo) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await page.locator('details summary').evaluate((el) => { el.parentElement.open = true; });

  const targets = await tapTargets(page);
  const names = targets.map((t) => t.name).join(' | ');
  for (const wanted of ['label "1 star"', 'label "5 stars"', 'button "Send"', 'a "Call our office"', 'summary "This is not my home"', 'button "Yes, this is not my home"', 'a "Privacy"']) {
    expect(names, `the check did not find ${wanted}`).toContain(wanted);
  }
  const small = targets.filter((t) => !t.inline && (t.width < 44 || t.height < 44));
  const summary = small.filter((t) => t.tag === 'summary').map((t) => `${t.name} is ${t.width}x${t.height} px`);
  const others = small.filter((t) => t.tag !== 'summary').map((t) => `${t.name} is ${t.width}x${t.height} px`);
  checkGap(testInfo, 'not-my-home-is-41px-tall', summary, 'a control is smaller than 44x44 px');
  expect.soft(others, 'a control is smaller than 44x44 px').toEqual([]);

  const text = await textSizes(page);
  const tiny = text.filter((t) => t.size < 16);
  const date = tiny.filter((t) => t.where === 'small').map((t) => `${t.where} "${t.text}" is ${t.size} px`);
  const rest = tiny.filter((t) => t.where !== 'small').map((t) => `${t.where} "${t.text}" is ${t.size} px`);
  checkGap(testInfo, 'date-is-15px', date, 'text is smaller than 16 px');
  expect.soft(rest, 'text is smaller than 16 px').toEqual([]);

  // The office number is one tap away, twice: the big button and the footer.
  await expect(page.locator('a.btn.ghost')).toHaveText('Call our office');
  await expect(page.locator('a.btn.ghost')).toHaveAttribute('href', `tel:${server.info.officePhone}`);
  await expect(page.locator('footer a[href^="tel:"]')).toHaveText('(916) 555-0100');
});
