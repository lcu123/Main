// The watch page, on every project in playwright.config.js: every engine, every phone, the
// iPads, the desktop sizes and the TVs. Each test films its own fresh video, so they all run
// at the same time without touching each other.

const {
  test, expect, checkGap, projectFacts,
  videoState, waitForMetadata, pressPlay, pressPause, waitForTime, pictureInPlayer,
  press, sidewaysScroll, tapTargets, textSizes, insideFirstScreen, shot,
} = require('../lib/harness');

const LONG_ADDRESS = '12345 North Whispering Pines Boulevard, Apartment 204';
const CALL = 'Call our office (916) 555-0100';

test('opens with nothing wrong: no errors, the logo, the font, the private-page headers', async ({ page, clip, problems }, testInfo) => {
  const response = await page.goto(clip.watch);
  expect(response.status()).toBe(200);
  await expect(page).toHaveTitle(/Your service video/);
  await expect(page.locator('h1')).toContainText(`A video from ${clip.tech} at ${clip.address}`);

  const headers = response.headers();
  const csp = headers['content-security-policy'];
  expect(csp, 'the page must keep its Content-Security-Policy').toContain("default-src 'none'");
  // Safari's own player draws its AirPlay and picture-in-picture cards from blob: addresses.
  // Without blob: here every iPhone 13 and later logged "Button failed to load" for each of
  // them (first CI run, WebKit on macOS) and the cards were blank.
  expect(csp, "img-src must allow blob: for Safari's own player icons").toMatch(/img-src[^;]*\bblob:/);
  expect(csp, 'and nothing wider than that').toMatch(/img-src 'self' data: blob:;/);
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

  expect.soft(await sidewaysScroll(page), 'the page scrolls sideways').toEqual([]);
  expect.soft(await insideFirstScreen(page, 'header.top h1', 'the heading')).toBe('');
  // The player's own controls sit along the bottom edge of the <video> box.
  checkGap(testInfo, 'player-below-first-screen', await insideFirstScreen(page, '#v', 'the player'), 'the player is not all in the first screen');

  // The player is a black box as wide as the column, and the 9:16 picture sits in the middle
  // of it. The box is NOT 9:16 itself: a box as narrow as the picture leaves the browser's
  // control bar no room, and on a phone on its side (a 150 px wide picture) the full-screen
  // and sound buttons dropped out of it.
  const { width, height } = projectFacts(testInfo);
  const player = await pictureInPlayer(page);
  testInfo.annotations.push({
    type: 'player size',
    description: `${Math.round(player.box.width)}x${Math.round(player.box.height)} px box, ${player.picture.width}x${player.picture.height} px picture, in a ${width}x${height} window`,
  });
  expect.soft(player.wrong, 'the picture is squashed or cropped, or the player does not fill its column').toEqual([]);
  expect.soft(player.picture.width, 'the picture is too narrow to watch').toBeGreaterThanOrEqual(120);
  // Room for the control bar: play, the time, sound, full screen and the menu need about 280 px.
  expect.soft(player.box.width, 'the player is too narrow for its own buttons').toBeGreaterThanOrEqual(Math.min(280, player.column));
  expect.soft(player.column, 'the column itself should be at least 280 px on every device in the table').toBeGreaterThanOrEqual(280);

  // Five stars, side by side, inside the card.
  const stars = await page.evaluate(() => {
    const card = document.getElementById('score').getBoundingClientRect();
    return [1, 2, 3, 4, 5].map((n) => { const r = document.querySelector(`label[for="s${n}"]`).getBoundingClientRect(); return { n, left: r.left - card.left, right: card.right - r.right, width: r.width, height: r.height, top: r.top }; });
  });
  testInfo.annotations.push({ type: 'stars', description: `each star is ${Math.round(stars[0].width)}x${Math.round(stars[0].height)} px here` });
  expect.soft(stars.filter((s) => s.left < 0 || s.right < 0).map((s) => `star ${s.n} sticks out of the card`), 'a star is outside the card').toEqual([]);
  expect.soft(new Set(stars.map((s) => Math.round(s.top))).size, 'the five stars should be on one line').toBe(1);
  expect.soft(stars.every((s, i) => i === 0 || s.left > stars[i - 1].left), 'the stars should read 1 to 5 from left to right').toBe(true);
  expect(await problems.list()).toEqual([]);
});

test('a long address wraps instead of breaking the page', async ({ page, server }, testInfo) => {
  const clip = await server.newVideo({ address: LONG_ADDRESS });
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await expect(page.locator('h1')).toContainText(LONG_ADDRESS);
  expect.soft(await sidewaysScroll(page), 'the page scrolls sideways').toEqual([]);
  expect.soft(await insideFirstScreen(page, 'header.top h1', 'the heading')).toBe('');
  const { height } = projectFacts(testInfo);
  // Whichever of the two is listed for this window is the one that must still be true.
  const gap = height < 331 ? 'player-below-first-screen' : 'long-address-pushes-player-down';
  checkGap(testInfo, gap, await insideFirstScreen(page, '#v', 'the player'), 'the player is not all in the first screen');
  await shot(page, testInfo, 'long-address');
});

test('an address with no spaces in it is broken across lines, not pushed off the screen', async ({ page, server }) => {
  // One long "word" (a unit written without spaces, a pasted map link). Without
  // overflow-wrap on the heading it keeps one line and the whole page scrolls sideways.
  const clip = await server.newVideo({ address: '1234NorthWhisperingPinesBoulevardApartment204BuildingC' });
  await page.goto(clip.watch);
  await waitForMetadata(page);
  expect(await sidewaysScroll(page), 'the page scrolls sideways').toEqual([]);
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

  // Play again from the start. First wait until the player has settled at its end (paused, not
  // seeking): only then is "press play" one clear action with one clear result.
  await expect.poll(async () => { const s = await videoState(page); return s.paused && !s.seeking; }, { message: 'the player did not come to rest at the end' }).toBe(true);
  expect(await pressPlay(page, testInfo), 'pressing play after the end did not start the video again').not.toBe('');
  await expect.poll(async () => { const s = await videoState(page); return s.error ? `error ${s.error}` : (!s.ended && s.time > 0.3 && s.time < 6 ? 'playing again' : `at ${s.time.toFixed(2)} s, ${s.paused ? 'paused' : 'not paused'}${s.ended ? ', ended' : ''}`); },
    { message: 'after the end, the video did not play again from the start', timeout: 15_000 }).toBe('playing again');

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
  // The real server allows 120 page opens and 600 player requests a minute per address; one viewing must stay far below both.
  expect(state.hits.all, 'one viewing made too many requests to the server').toBeLessThan(40);
  expect(await problems.list()).toEqual([]);
});

test('a score: pick a star, send, see the thank-you; it is saved once and stays', async ({ page, clip, server, problems }, testInfo) => {
  await page.goto(clip.watch);
  await expect(page.locator('#score h2')).toHaveText('How did we do?');

  await press(page, testInfo, 'label[for="s4"]');
  await expect(page.locator('#s4')).toBeChecked();
  // The customer sees it: stars 1 to 4 are filled, star 5 is not. (What "filled" looks like is
  // measured in pixels in colour-schemes.spec.js; here, that the right ones change.)
  // (The label's colour is what the drawing is filled with: fill:currentColor.)
  const fills = await page.evaluate(() => [1, 2, 3, 4, 5].map((n) => getComputedStyle(document.querySelector(`label[for="s${n}"]`)).color));
  expect(new Set(fills.slice(0, 4)).size, 'stars 1 to 4 should share one fill').toBe(1);
  expect(fills[4], 'star 5 must not look picked').not.toBe(fills[0]);

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
  // kept the old form anyway would let the customer send a second score over the first.
  // (Chromium, Firefox and WebKit all ask again: first CI run.)
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
  expect.soft(await sidewaysScroll(page), 'the page scrolls sideways with "not my home" open').toEqual([]);

  await press(page, testInfo, 'details button[type=submit]');
  await expect(page.locator('h1')).toHaveText('Thank you');
  await expect(page.locator('main')).toContainText('We took this video down');
  await expect(page.locator('main a.btn')).toHaveAttribute('href', `tel:${server.info.officePhone}`);

  let state = await server.state(clip.token);
  expect(state.state).toBe('pulled');
  expect(state.watchable).toBe(false);
  expect(state.notMyHome).toBe(1);
  expect(state.hits.notMyHome).toBe(1);

  // The customer refreshes the thank-you page and lets the browser send the form again:
  // the same kind answer, and it is not counted twice.
  const resent = await page.request.post(`${clip.watch}/not-my-home`);
  expect(resent.status()).toBe(200);
  expect(await resent.text()).toContain('We took this video down');
  state = await server.state(clip.token);
  expect(state.notMyHome, 'sending the form again must not report "not my home" twice').toBe(1);

  // Later they come back to that same address (from the history, or by pressing Enter in the
  // address bar). That is a plain GET of the address the form posts to. It used to answer
  // "405 Method Not Allowed" in bare text; it must end on the styled page for a video that is gone.
  problems.allow(/status of 410/);
  const back = await page.goto(`${clip.watch}/not-my-home`);
  expect(new URL(page.url()).pathname, 'the form\'s address must send a visitor on to the link\'s own page').toBe(`/w/${clip.token}`);
  expect(back.status()).toBe(410);
  await expect(page.locator('h1')).toHaveText('This video is no longer available.');
  await expect(page.locator('main a.btn'), 'the styled page, with the office number').toHaveText(CALL);
  await expect(page.locator('header.top img, main img').first(), 'the styled page carries the logo').toBeVisible();

  // The link is dead from now on, and so is the address the player used.
  const again = await page.goto(clip.watch);
  expect(again.status()).toBe(410);
  await expect(page.locator('h1')).toHaveText('This video is no longer available.');
  await expect(page.locator('video')).toHaveCount(0);
  const stream = await page.request.get(clip.video, { maxRedirects: 0 });
  expect(stream.status(), 'the player address must stop redirecting to the bucket').toBe(410);
  expect(await problems.list()).toEqual([]);
});

test('opening the "not my home" address by itself takes nothing down', async ({ page, clip, server }) => {
  // Mail scanners, link previews and "open in new tab" send GETs to every address they find.
  // A GET must never pull a video: it goes to the watch page, and the video stays up.
  const response = await page.goto(`${clip.watch}/not-my-home`);
  expect(response.status()).toBe(200);
  expect(new URL(page.url()).pathname).toBe(`/w/${clip.token}`);
  await expect(page.locator('#v')).toHaveCount(1);
  await waitForMetadata(page);
  const hop = await page.request.get(`${clip.watch}/not-my-home`, { maxRedirects: 0 });
  expect(hop.status(), 'a redirect that turns the request into a GET of the watch page').toBe(303);
  expect(hop.headers().location).toBe(`/w/${clip.token}`);
  const state = await server.state(clip.token);
  expect(state.watchable, 'a GET took the video down').toBe(true);
  expect(state.notMyHome).toBe(0);
  expect(state.events).not.toContain('not_my_home');
});

test('every control is big enough for a finger (44x44) and no text is under 16 px', async ({ page, clip, server }) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await page.locator('details summary').evaluate((el) => { el.parentElement.open = true; });

  const targets = await tapTargets(page);
  const names = targets.map((t) => t.name).join(' | ');
  for (const wanted of ['label "1 star"', 'label "5 stars"', 'button "Send"', `a "${CALL}"`, 'summary "This is not my home"', 'button "Yes, this is not my home"', 'a "Privacy"']) {
    expect(names, `the check did not find ${wanted}`).toContain(wanted);
  }
  const small = targets.filter((t) => !t.inline && (t.width < 44 || t.height < 44)).map((t) => `${t.name} is ${t.width}x${t.height} px`);
  expect.soft(small, 'a control is smaller than 44x44 px').toEqual([]);

  const tiny = (await textSizes(page)).filter((t) => t.size < 16).map((t) => `${t.where} "${t.text}" is ${t.size} px`);
  expect.soft(tiny, 'text is smaller than 16 px').toEqual([]);

  // The office number is one tap away, twice, and it can be READ both times: on a desktop, a
  // tablet or a TV, where a tap on a tel: link starts no call, "Call our office" alone told
  // the customer nothing.
  const call = page.locator('a.btn.ghost');
  await expect(call).toHaveText(CALL);
  await expect(call).toHaveAttribute('href', `tel:${server.info.officePhone}`);
  const number = await call.locator('.nb').evaluate((el) => ({ text: el.textContent, lines: el.getClientRects().length, inside: el.getBoundingClientRect().right <= el.parentElement.getBoundingClientRect().right + 0.5 }));
  expect(number.text).toBe('(916) 555-0100');
  expect.soft(number.lines, 'the number must stay on one line, not break in the middle').toBe(1);
  expect.soft(number.inside, 'the number must stay inside the button').toBe(true);
  await expect(page.locator('footer a[href^="tel:"]')).toHaveText('(916) 555-0100');
});

test('the stars are named for a screen reader by words inside their labels, and the note sits where it will be read out', async ({ page, clip }) => {
  await page.goto(clip.watch);
  // The name comes from text in the label itself, hidden from sight. (aria-label on a <label>
  // is not allowed, and a browser may ignore it: the stars would then be five nameless buttons.)
  for (const [n, name] of [[5, '5 stars'], [4, '4 stars'], [3, '3 stars'], [2, '2 stars'], [1, '1 star']]) {
    await expect(page.getByRole('radio', { name, exact: true }), `no radio button is named "${name}"`).toHaveCount(1);
    const label = page.locator(`label[for="s${n}"]`);
    expect(await label.getAttribute('aria-label'), 'aria-label does not belong on a <label>').toBeNull();
    expect((await label.textContent()).trim()).toBe(name);
    const words = await label.locator('.vh').boundingBox();
    expect(words.width * words.height, `the words "${name}" should not be seen`).toBeLessThanOrEqual(4);
  }
  // The "did not load" note is in a region that is always on the page, so a screen reader
  // says it the moment it appears, wherever the customer is on the page.
  const region = await page.locator('#verr').evaluate((el) => ({ role: el.parentElement.getAttribute('role'), shown: !el.parentElement.hidden, height: el.parentElement.getBoundingClientRect().height }));
  expect(region.role, 'the note must sit inside role="status"').toBe('status');
  expect(region.shown).toBe(true);
  expect(region.height, 'the empty region must take no room on a page whose video is fine').toBe(0);
});

test('on a very tall screen the video page widens for the player, and the pages of words keep their reading width', async ({ page, clip, server }, testInfo) => {
  test.skip(projectFacts(testInfo).input !== 'mouse', 'a monitor turned upright, or a 4K screen at full scale: desktop rows');
  await page.setViewportSize({ width: 1440, height: 2560 });
  await page.goto(server.info.privacy);
  const words = await page.locator('.wrap').evaluate((el) => el.getBoundingClientRect().width);
  expect(words, 'the privacy page stretched with the height of the screen: lines of 130 characters').toBeLessThanOrEqual(560);
  await page.goto(clip.watch);
  const watch = await page.locator('.wrap').evaluate((el) => el.getBoundingClientRect().width);
  expect(watch, 'the watch page should use a tall screen for a bigger player').toBeGreaterThan(900);
  expect(await sidewaysScroll(page)).toEqual([]);
});

