// When the video will not load, or stops arriving. watch.js tries again by itself a
// bounded number of times, then shows ONE yellow note under the player; the note is a
// button. Runs on the 'deep' rows.
//
//   a failure           two tries in a row without asking, never more than six on one page;
//                       then the note "The video did not load. Press here to try again."
//   the note, pressed   once: one more try. Twice in a row: the page is opened again (a video
//                       that was taken down then shows the "no longer available" page).
//                       While that one try runs the note stays where it is and says
//                       "Trying again...", so nothing slides up under a second tap and a
//                       keyboard or a remote keeps its place. A double tap is one press.
//   the note, seen      where the player fills the screen (a phone on its side) the note is
//                       scrolled into view; a player in full screen leaves full screen.
//   a weak signal       the picture has not moved for 6 s while playing: the same box says
//                       "Your signal is weak or has dropped. The video carries on by itself
//                       when it is back." It goes away when the picture moves.
//   the signal is back  the browser says "online": the page tries by itself, from the same
//                       second. While the browser says it is offline a second press does
//                       not open the page again (that would show the browser's own
//                       "no internet" page in place of ours).
//
// What a customer got before: a note that said "tap play again", which did nothing on a
// failed player, and a spinner with no words at all when the signal dropped.

const {
  test, expect, projectFacts, videoState, waitForMetadata, pressPlay, watchNote, notesShown, sourceReloads,
  press, pixelsAround, twoColours, contrast,
} = require('../lib/harness');

const FAILED = 'The video did not load. Press here to try again.';
const TRYING = 'Trying again...';
const SLOW = 'Your signal is weak or has dropped. The video carries on by itself when it is back.';
const note = (page) => page.locator('#verr');

/** How many times the page has asked the server for the video, counted in the browser. */
function countVideoRequests(page) {
  const seen = { n: 0 };
  page.on('request', (request) => { if (/\/w\/[0-9a-f]{32}\/video$/.test(new URL(request.url()).pathname)) seen.n += 1; });
  return seen;
}

/** The count has stopped growing: no loop. Returns the count. */
async function settled(page, seen, quiet = 3000) {
  let last = seen.n;
  let since = Date.now();
  while (Date.now() - since < quiet) {
    await page.waitForTimeout(200);
    if (seen.n !== last) { last = seen.n; since = Date.now(); }
    if (seen.n > 20) throw new Error(`the page keeps asking for the video: ${seen.n} requests and counting`);
  }
  return seen.n;
}

test('a video taken down as the page opens: two tries, then the note; a press tries once more; a second press opens the "no longer available" page', async ({ page, server, problems }, testInfo) => {
  const { engine } = projectFacts(testInfo);
  const clip = await server.newVideo();
  const asked = countVideoRequests(page);
  // The office pulls the video in the moment between the page and the player's first request.
  // From then on the real server answers 410 for the video, and for the page.
  let pulled = false;
  await page.route('**/w/*/video', async (route) => {
    if (!pulled) { pulled = true; await server.pull(clip.token); }
    await route.continue();
  });
  problems.allow(/status of 410/);
  // Firefox's own play button calls play() on the failed player and lets the refusal go
  // unhandled; that is the browser's doing, not the page's (watch.js catches its own).
  problems.allow(/media resource indicated by the src attribute .* was not suitable/);
  await page.goto(clip.watch);

  // It tried, and it stopped trying.
  await expect(note(page), 'a video that cannot load must end at the note').toBeVisible();
  await expect(note(page)).toHaveText(FAILED);
  const afterLoad = await settled(page, asked);
  testInfo.annotations.push({ type: 'retries', description: `${afterLoad} requests for the video before the note (the first and ${afterLoad - 1} automatic tries)` });
  // The first request and two tries. Engines that ask twice per try may count more, never past the hard stop of six tries.
  if (engine === 'chromium') expect(afterLoad, 'one request and two automatic tries').toBe(3);
  expect(afterLoad).toBeGreaterThanOrEqual(2);
  expect(afterLoad, 'more than six automatic tries').toBeLessThanOrEqual(13);
  expect((await videoState(page)).error, 'the player should be in its failed state').not.toBe('');

  // The note is a real button a finger, a mouse, a keyboard and a TV remote can all press.
  const facts = await note(page).evaluate((el) => {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return { tag: el.tagName, type: el.type, height: r.height, width: r.width, column: el.parentElement.getBoundingClientRect().width, size: parseFloat(style.fontSize), cursor: style.cursor, tab: el.tabIndex };
  });
  expect(facts.tag, 'the note must be a <button>: a <div> cannot be reached by keys or a remote').toBe('BUTTON');
  expect(facts.type, 'type=button: inside or near a form it must never send the form').toBe('button');
  expect(facts.tab).toBe(0);
  expect(facts.height, 'the note is too short for a finger').toBeGreaterThanOrEqual(44);
  expect(facts.width, 'the note should be as wide as the player').toBeGreaterThanOrEqual(facts.column - 40);
  expect(facts.size, 'the note\'s words are too small').toBeGreaterThanOrEqual(16);
  const [ground, ink] = twoColours((await pixelsAround(page, '#verr', -4)).png);
  expect(contrast(ground, ink), `the note's words are hard to read (rgb(${ink}) on rgb(${ground}))`).toBeGreaterThanOrEqual(4.5);

  // A tap on the dead player does nothing: no request, and it is not a viewing.
  expect(await pressPlay(page, testInfo, { allowScript: false }), 'a failed player should not start').toBe('');
  expect(await settled(page, asked, 1200), 'tapping a failed player must not ask the server again').toBe(afterLoad);
  let state = await server.state(clip.token);
  expect(state.played, 'a tap on a player that failed was counted as a viewing').toBe(0);
  expect(state.hits.played, 'a tap on a player that failed told the server "played"').toBe(0);

  // First press of the note: one more try, by hand.
  await press(page, testInfo, '#verr');
  await expect.poll(() => asked.n, { message: 'pressing the note did not try again' }).toBeGreaterThan(afterLoad);
  const afterPress = await settled(page, asked, 1500);
  expect(afterPress - afterLoad, 'one press of the note is one more try, not a new round of retries').toBeLessThanOrEqual(engine === 'chromium' ? 1 : 2);
  await expect(note(page), 'the try failed again: the note must be back').toBeVisible();
  await expect(note(page)).toHaveText(FAILED);
  await expect(page.locator('#v'), 'the first press must not leave the page').toHaveCount(1);

  // Second press in a row: open the page again. For a video that was taken down that is the
  // page that says so, with the office number, instead of a note that can never succeed.
  await press(page, testInfo, '#verr');
  await expect(page.locator('h1')).toHaveText('This video is no longer available.');
  await expect(page.locator('video')).toHaveCount(0);
  await expect(page.locator('main a.btn')).toHaveText('Call our office (916) 555-0100');
  state = await server.state(clip.token);
  expect(state.played, 'a video nobody saw must not be counted as watched').toBe(0);
  expect(await problems.list()).toEqual([]);
});

test('a file that breaks off part-way: it plays to the break, tries twice, shows the note, and does not loop', async ({ page, server, problems }, testInfo) => {
  const { engine } = projectFacts(testInfo);
  // The last 45% of the file is missing; its index still promises all 8 seconds.
  const clip = await server.newVideo({ file: 'cut-short' });
  const asked = countVideoRequests(page);
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await watchNote(page);
  expect(await pressPlay(page, testInfo)).not.toBe('');

  // Each engine meets the break in its own way: Chromium reports a decoding error (the note);
  // another may stop at the break as if the clip had ended, or wait for the missing part (the
  // weak-signal note). Each of those tells the customer something. A loop does not.
  let outcome = '';
  await expect.poll(async () => {
    const s = await videoState(page);
    outcome = s.note === 'failed' ? 'the note' : (s.ended ? 'an early end' : (s.note === 'slow' ? 'a wait, with the weak-signal note' : ''));
    return outcome;
  }, { message: 'the damaged video neither ended nor showed a note', timeout: 40_000 }).not.toBe('');
  const total = await settled(page, asked);
  const s = await videoState(page);
  const line = `${engine}: a file cut short gives ${outcome} at ${s.time.toFixed(1)} s after ${total} request(s) for the video`;
  testInfo.annotations.push({ type: 'a file cut short', description: line });
  if (engine !== 'chromium') console.log(`[${testInfo.project.name}] ${line}`); // into the CI log: only Chromium has been run
  expect(total, 'the page asked for the damaged video again and again').toBeLessThanOrEqual(13);
  if (engine === 'chromium') {
    expect(outcome, 'Chromium reports the break as an error, so the note must show').toBe('the note');
    expect(total, 'one request and two automatic tries').toBe(3);
  }
  await expect.poll(async () => (await server.state(clip.token)).played, { message: 'the customer did press play and saw the first half: that is a viewing' }).toBe(1);

  if (outcome === 'the note') {
    await expect(note(page)).toHaveText(FAILED);
    expect(s.time, 'the tries should have kept the place, not gone back to the start').toBeGreaterThan(2.5);
    // One press, one try; it fails at the same place and the note comes back. Still no loop.
    await press(page, testInfo, '#verr');
    await expect.poll(() => asked.n, { message: 'pressing the note did not try again' }).toBeGreaterThan(total);
    const afterPress = await settled(page, asked);
    expect(afterPress - total, 'one press of the note is one more try').toBeLessThanOrEqual(engine === 'chromium' ? 1 : 2);
    await expect(note(page)).toBeVisible();
    await expect(note(page)).toHaveText(FAILED);
  }
  expect((await server.state(clip.token)).hits.played, 'the tries must not report more viewings').toBe(1);
  expect(await problems.list()).toEqual([]);
});

test('a video that keeps failing after it has moved on: the page stops at six tries', async ({ page, clip }, testInfo) => {
  // A real player cannot be made to fail seven times, each after three seconds of progress,
  // in a test of reasonable length. So the failures here are "error" events sent to the
  // player by the test, and the progress is a skip forward. What is counted is real: the
  // requests watch.js makes for the video. Without the hard stop, a connection that fails
  // every few seconds would have the page fetching the video for as long as it stays open.
  const { engine } = projectFacts(testInfo);
  const asked = countVideoRequests(page);
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await watchNote(page);
  if (engine === 'chromium') expect(asked.n).toBe(1);
  const fail = () => page.evaluate(() => document.getElementById('v').dispatchEvent(new Event('error')));
  const moveOn = async () => {
    // The fresh source is in, then the picture moves more than 3 s past where it failed.
    await expect.poll(async () => (await videoState(page)).ready, { message: 'the fresh source never loaded' }).toBeGreaterThanOrEqual(1);
    await page.evaluate(() => { document.getElementById('v').currentTime = 5; });
    await expect.poll(async () => { const s = await videoState(page); return !s.seeking && s.time > 4.5; }).toBe(true);
    await page.evaluate(() => { document.getElementById('v').currentTime = 0.5; });
    await expect.poll(async () => { const s = await videoState(page); return !s.seeking && s.time < 1; }).toBe(true);
  };
  for (let tries = 1; tries <= 6; tries += 1) {
    await moveOn();
    await fail();
    // The source is loaded afresh once per failure (counted in the page, the same in every engine).
    await expect.poll(() => sourceReloads(page), { message: `failure ${tries} should have been tried again` }).toBe(tries);
    await expect(note(page), `after ${tries} tries with progress in between there is still no reason for the note`).toBeHidden();
  }
  await moveOn();
  const before = asked.n;
  await fail(); // the seventh
  await expect(note(page), 'six tries are spent: the seventh failure must show the note').toBeVisible();
  await expect(note(page)).toHaveText(FAILED);
  expect(await settled(page, asked, 1500), 'a seventh automatic try asked the server for the video again').toBe(before);
  expect(await sourceReloads(page), 'a seventh automatic try').toBe(6);
  if (engine === 'chromium') expect(asked.n, 'the first request and six tries').toBe(7);
});

test('a video this customer\'s connection cannot fetch: taps and "try again" are not viewings', async ({ page, clip, server, problems }, testInfo) => {
  // The video is fine on the server; the requests for it die on the way (a filter at work,
  // a dead spot). The office must not see "watched" for a customer who saw a yellow note.
  await page.route('**/w/*/video', (route) => route.abort('connectionfailed'));
  problems.allow(/Failed to load resource/);
  problems.allow(/media resource indicated by the src attribute .* was not suitable/); // Firefox's own play button, as above
  await page.goto(clip.watch);
  await expect(note(page)).toBeVisible();
  await expect(note(page)).toHaveText(FAILED);

  expect(await pressPlay(page, testInfo, { allowScript: false })).toBe('');
  await page.waitForTimeout(800);
  let state = await server.state(clip.token);
  expect(state.hits.played, 'a tap on a player that failed told the server "played"').toBe(0);

  // A browser may still announce "play" and "playing" for a tap on a failed player (Chromium
  // does when the player failed after it had loaded). Those announcements must not count as
  // a viewing, and must not take the note away while nothing plays.
  await page.evaluate(() => { const v = document.getElementById('v'); v.dispatchEvent(new Event('play')); v.dispatchEvent(new Event('playing')); });
  await page.waitForTimeout(600);
  state = await server.state(clip.token);
  expect(state.hits.played, '"play" from a failed player told the server "played"').toBe(0);
  await expect(note(page), '"playing" from a failed player took the note away').toBeVisible();
  await expect(note(page)).toHaveText(FAILED);

  await press(page, testInfo, '#verr');
  await expect(note(page), 'the try failed again: the note must be back').toBeVisible();
  await page.waitForTimeout(800);
  state = await server.state(clip.token);
  // A press of "try again" starts the player; with nothing loaded that is not a viewing either.
  expect(state.played, `after one press of "try again" on a video that never loaded, the server recorded ${state.played} viewing(s)`).toBe(0);
});

test.describe('a weak signal', () => {
  // The bucket trickles the file a little faster than it plays (it needs 340 KB a second), and
  // can be told to stop in mid-answer.
  const film = (server) => server.newVideo({ file: 'stretched', throttle: 420000 });
  // The signal drops at this byte of the file: the index and about two and a half seconds of
  // picture arrive, and nothing after them. The place is fixed before the page opens, so it
  // is the same in every browser. (Cutting the signal once play had started was a race that
  // Safari won: it asks for the whole file at once, and on a slow test machine had all of it.)
  const DROPS_AT = 900_000;

  /** Open the page with the signal set to drop part-way into the file, and press play. Returns when the picture has stopped, with the time it stopped. */
  async function playThenStall(page, server, clip, testInfo) {
    await server.stall(clip.token, true, DROPS_AT);
    await page.goto(clip.watch);
    await waitForMetadata(page, 30_000);
    await watchNote(page);
    expect(await pressPlay(page, testInfo)).not.toBe('');
    // The picture runs on for as long as the browser has something left, then stops.
    let at = -1;
    let since = Date.now();
    await expect.poll(async () => {
      const s = await videoState(page);
      if (s.time !== at) { at = s.time; since = Date.now(); }
      return s.ended ? 'the clip ended although the bucket held back most of the file' : (Date.now() - since >= 700 && s.time > 0.3 ? 'stopped' : 'still moving');
    }, { message: 'the picture never stopped although the bucket stopped sending', timeout: 20_000, intervals: [150] }).toBe('stopped');
    return since;
  }

  test('the picture stops for more than 6 seconds: the note says the signal is weak, and goes when the picture moves again', async ({ page, server }, testInfo) => {
    const clip = await film(server);
    const stopped = await playThenStall(page, server, clip, testInfo);
    await expect(note(page), 'six seconds of a frozen picture and not a word to the customer').toBeVisible({ timeout: 12_000 });
    const waited = Date.now() - stopped;
    await expect(note(page)).toHaveText(SLOW);
    expect(waited, 'the note came too soon to be about a weak signal').toBeGreaterThan(4500);
    const s = await videoState(page);
    expect(s.error, 'a weak signal is not a failure').toBe('');
    expect(s.paused, 'the video should still be trying to play').toBe(false);

    await server.stall(clip.token, false);
    await expect(note(page), 'the picture is moving again but the note still says the signal is weak').toBeHidden({ timeout: 10_000 });
    await expect.poll(async () => (await videoState(page)).ended, { message: 'the video did not carry on by itself', timeout: 30_000 }).toBe(true);
    const state = await server.state(clip.token);
    // It waited; it did not throw the link away and start again.
    expect(await sourceReloads(page), 'a weak signal must not make the page fetch the video afresh').toBe(0);
    expect(state.hits.played).toBe(1);
    expect((await notesShown(page)).filter((n) => n !== SLOW), 'only the weak-signal note should ever have shown').toEqual([]);
  });

  test('a hiccup of two seconds shows nothing', async ({ page, server }, testInfo) => {
    const clip = await film(server);
    const stopped = await playThenStall(page, server, clip, testInfo);
    await page.waitForTimeout(Math.max(0, 2000 - (Date.now() - stopped)));
    await server.stall(clip.token, false);
    await expect.poll(async () => (await videoState(page)).ended, { message: 'the video did not carry on', timeout: 30_000 }).toBe(true);
    // More than six seconds have passed since the stop (the rest of the clip is that long).
    expect(Date.now() - stopped).toBeGreaterThan(6500);
    expect(await notesShown(page), 'a two-second hiccup must not alarm the customer').toEqual([]);
    expect(await sourceReloads(page), 'a hiccup must not make the page fetch the video afresh').toBe(0);
  });

  test('pressing the weak-signal note only puts it away', async ({ page, server }, testInfo) => {
    const clip = await film(server);
    await playThenStall(page, server, clip, testInfo);
    await expect(note(page)).toBeVisible({ timeout: 12_000 });
    await expect(note(page)).toHaveText(SLOW);
    await press(page, testInfo, '#verr');
    await expect(note(page)).toBeHidden();
    await page.waitForTimeout(1000);
    // No reload of the page, no new request for the video: the player is still waiting on the same link.
    await expect(page.locator('#v')).toHaveCount(1);
    expect((await videoState(page)).error).toBe('');
    expect(await sourceReloads(page), 'pressing the weak-signal note must not fetch the video afresh').toBe(0);
    expect((await server.state(clip.token)).hits.page, 'pressing the weak-signal note must not open the page again').toBe(1);
    await server.stall(clip.token, false);
    await expect.poll(async () => (await videoState(page)).ended, { message: 'the video did not carry on', timeout: 30_000 }).toBe(true);
  });
});

test.describe('the "try again" note, pressed', () => {
  /** Every request for the video fails until `open()` is called; then they go through, after `delay` ms. */
  async function blockTheVideo(page, delay = 0) {
    const gate = { open: false, asked: 0 };
    await page.route('**/w/*/video', async (route) => {
      gate.asked += 1;
      if (!gate.open) { await route.abort('connectionfailed'); return; }
      if (delay) await new Promise((done) => setTimeout(done, delay));
      await route.continue();
    });
    return gate;
  }
  const pageLoads = (server, clip) => server.state(clip.token).then((state) => state.hits.page);

  test('a double tap is one press: one try, the page is not opened again, no star is picked', async ({ page, clip, server, problems }, testInfo) => {
    const gate = await blockTheVideo(page);
    problems.allow(/Failed to load resource/);
    await page.goto(clip.watch);
    await expect(note(page)).toHaveText(FAILED);
    const before = gate.asked;
    // Two presses a few milliseconds apart, as a double tap or a double click sends them. The
    // second used to count as "the second press in a row", which opens the page again.
    await note(page).evaluate((el) => { el.click(); el.click(); });
    await expect(note(page), 'the try failed: the note must be back').toHaveText(FAILED);
    await page.waitForTimeout(1200);
    expect(gate.asked - before, 'a double tap must be one more try').toBeLessThanOrEqual(projectFacts(testInfo).engine === 'chromium' ? 1 : 2);
    expect(gate.asked - before).toBeGreaterThanOrEqual(1);
    expect(await pageLoads(server, clip), 'a double tap opened the page again').toBe(1);
    expect(await page.evaluate(() => document.querySelectorAll('.stars input:checked').length), 'a double tap picked a star').toBe(0);
    expect(await problems.list()).toEqual([]);
  });

  test('while the one try runs the note stays where it is and says so; when the video is back it plays and the note goes', async ({ page, clip, server, problems }, testInfo) => {
    const gate = await blockTheVideo(page, 1500);
    problems.allow(/Failed to load resource/);
    await page.goto(clip.watch);
    await expect(note(page)).toHaveText(FAILED);
    const place = await note(page).boundingBox();
    gate.open = true; // the connection is back; the next answer takes a second and a half
    await press(page, testInfo, '#verr');
    // The note used to vanish at the press: the score card slid up under the finger, and a
    // second tap landed on a star.
    await expect(note(page), 'the note should say that it is trying').toHaveText(TRYING);
    const during = await note(page).boundingBox();
    expect(Math.abs(during.y - place.y), 'the note moved while the try was running').toBeLessThanOrEqual(1);
    expect(await page.evaluate(() => { const b = document.getElementById('verr'); const r = b.getBoundingClientRect(); return document.elementFromPoint(r.left + r.width / 2, r.top + r.height * 0.8) === b; }),
      'something else is now where the lower part of the note was').toBe(true);
    await expect.poll(async () => { const s = await videoState(page); return !s.paused && s.time > 0.3 && !s.error; }, { message: 'the video did not play after the try', timeout: 15_000 }).toBe(true);
    await expect(note(page), 'the video plays but the note is still showing').toBeHidden();
    await expect.poll(async () => (await server.state(clip.token)).played, { message: 'a video that did play after "try again" is a viewing' }).toBe(1);
    expect(await pageLoads(server, clip)).toBe(1);
    expect(await problems.list()).toEqual([]);
  });

  test('with the phone on its side the note is brought into view; a video that is fine never scrolls the page', async ({ page, server, problems }) => {
    // 740x360: an Android phone on its side. The player fills the first screen, so a note
    // under it is below the edge, and a failed player with no words looks simply dead.
    await page.setViewportSize({ width: 740, height: 360 });
    const fine = await server.newVideo();
    await page.goto(fine.watch);
    await waitForMetadata(page);
    await page.waitForTimeout(700);
    expect(await page.evaluate(() => window.scrollY), 'a page whose video is fine scrolled by itself').toBe(0);

    const clip = await server.newVideo();
    await blockTheVideo(page);
    problems.allow(/Failed to load resource/);
    await page.goto(clip.watch);
    await expect(note(page)).toHaveText(FAILED);
    await expect.poll(() => page.evaluate(() => { const r = document.getElementById('verr').getBoundingClientRect(); return r.top >= 0 && r.bottom <= window.innerHeight + 1; }),
      { message: 'the note is not whole on the screen: the customer sees a dead player and nothing else', timeout: 5000 }).toBe(true);
    const player = await page.evaluate(() => { const r = document.getElementById('v').getBoundingClientRect(); return Math.round(Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0)); });
    expect(player, 'the player should still be on the screen above the note').toBeGreaterThan(150);
    expect(await problems.list()).toEqual([]);
  });

  test('a player that dies in full screen leaves full screen, so its note can be seen', async ({ page, clip, problems }, testInfo) => {
    const { engine, input } = projectFacts(testInfo);
    test.skip(engine !== 'chromium' || input !== 'mouse', 'full screen is entered here the desktop Chromium way; the iPhone player and TVs are on the go-live list for a real device');
    await page.goto(clip.watch);
    await waitForMetadata(page);
    // Full screen needs a real press: hang the request on the next click on the heading.
    await page.evaluate(() => document.querySelector('h1').addEventListener('click', () => document.getElementById('v').requestFullscreen(), { once: true }));
    await page.locator('h1').click();
    await expect.poll(() => page.evaluate(() => document.fullscreenElement && document.fullscreenElement.id)).toBe('v');
    // Now the video dies for good.
    await blockTheVideo(page);
    problems.allow(/Failed to load resource/);
    await page.evaluate(() => { const v = document.getElementById('v'); v.src = v.getAttribute('src').split('#')[0] + '#t=0.2'; v.load(); });
    await expect(note(page)).toHaveText(FAILED);
    await expect.poll(() => page.evaluate(() => !!document.fullscreenElement), { message: 'the dead player is still in full screen, on top of its note' }).toBe(false);
    await expect.poll(() => page.evaluate(() => { const r = document.getElementById('verr').getBoundingClientRect(); return r.top >= 0 && r.bottom <= window.innerHeight + 1; }),
      { message: 'the note is not on the screen after leaving full screen' }).toBe(true);
    expect(await problems.list()).toEqual([]);
  });

  test('a "played" message that does not arrive is sent again by itself, and counted once', async ({ page, clip, server, problems }, testInfo) => {
    // One dropped request used to leave a customer who watched the whole clip marked "sent".
    let posts = 0;
    await page.route('**/w/*/played', async (route) => {
      posts += 1;
      if (posts === 1) await route.fulfill({ status: 500, body: '' });
      else await route.continue();
    });
    problems.allow(/status of 500/);
    await page.goto(clip.watch);
    await waitForMetadata(page);
    expect(await pressPlay(page, testInfo)).not.toBe('');
    await expect.poll(async () => (await server.state(clip.token)).played, { message: 'the server never learned that the video was watched', timeout: 20_000 }).toBe(1);
    await page.waitForTimeout(1000);
    expect((await server.state(clip.token)).hits.played, 'the message should arrive once').toBe(1);
    testInfo.annotations.push({ type: 'played', description: `${posts} "played" message(s) left the page; the first was answered 500` });
    expect(await problems.list()).toEqual([]);
  });
});

test('the signal drops for good, then comes back: two presses stay on the page, and the video carries on by itself from the same second', async ({ page, server, context, problems }, testInfo) => {
  test.skip(projectFacts(testInfo).engine !== 'chromium', 'the browser is told it is offline the Chromium way');
  test.setTimeout(150_000);
  const clip = await server.newVideo({ file: 'stretched' });
  problems.allow(/Failed to load resource|ERR_INTERNET_DISCONNECTED/);
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await watchNote(page);
  await page.waitForTimeout(1000); // the browser fetches a little of the file ahead of play
  await context.setOffline(true);
  expect(await pressPlay(page, testInfo)).not.toBe('');
  // The player shows what it had (up to two or three seconds), waits, and in the end gives up.
  let reached = 0; // how far the picture got before it stopped
  await expect.poll(async () => {
    const s = await videoState(page);
    reached = Math.max(reached, s.time);
    return s.note;
  }, { message: 'with no connection the page must end at the note', timeout: 75_000, intervals: [200] }).toBe('failed');
  await expect(note(page)).toHaveText(FAILED);
  expect(await page.evaluate(() => navigator.onLine), 'the browser should say it is offline').toBe(false);
  // The place it froze at is kept in the address it last tried (#t=...). A failed load reads
  // zero seconds, and the next try used to start from there.
  const froze = Number((await page.evaluate(() => document.getElementById('v').getAttribute('src'))).split('#t=')[1]);
  testInfo.annotations.push({ type: 'signal lost', description: `the picture got to ${reached.toFixed(1)} s; the tries kept #t=${froze}` });
  expect(froze, `the tries forgot where the picture was (it had got to ${reached.toFixed(1)} s) and went back to the start`).toBeGreaterThan(reached - 0.6);

  // Two presses, more than a second apart. Offline, the second one must not open the page
  // again: the customer would get the browser's own "no internet" page instead of ours.
  await press(page, testInfo, '#verr');
  await expect(note(page)).toHaveText(FAILED);
  await page.waitForTimeout(1200);
  await press(page, testInfo, '#verr');
  await expect(note(page)).toHaveText(FAILED);
  await page.waitForTimeout(600);
  expect(page.url(), 'the watch page was swapped for the browser\'s error page').toBe(clip.watch);
  await expect(page.locator('#v')).toHaveCount(1);

  // The signal is back. (Offline emulation does not announce it, as a real browser does.)
  await context.setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await expect.poll(async () => { const s = await videoState(page); return !s.error && !s.paused && s.ready >= 3; }, { message: 'the video did not carry on by itself when the signal came back', timeout: 20_000 }).toBe(true);
  await expect(note(page)).toBeHidden();
  const s = await videoState(page);
  expect(s.time, `the video started again from the beginning (it froze at ${froze} s)`).toBeGreaterThan(froze - 0.6);
  await expect.poll(async () => (await videoState(page)).ended, { message: 'the video did not reach its end', timeout: 30_000 }).toBe(true);
  expect((await server.state(clip.token)).hits.page, 'the page was opened again').toBe(1);
  await expect.poll(async () => (await server.state(clip.token)).played).toBe(1);
});

