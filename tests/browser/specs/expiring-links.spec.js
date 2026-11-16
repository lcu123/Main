// The bucket link the player is redirected to lives 15 minutes in production. Here it lives
// 4 seconds (serve.py: link_seconds), on a file with the bit rate of a real visit video, so
// that a customer who opens the page, puts the phone down and comes back is 6 seconds away
// instead of 16 minutes.
//
// The server signs a fresh link every time /w/<token>/video is asked. Firefox and Safari ask
// that address again for every piece of the file, so they never hold a dead link. Chromium
// (Chrome, Edge, Samsung Internet, Android mail apps) remembers where the redirect pointed
// and asks the BUCKET again; when that link has died, watch.js must notice and ask our own
// address for a new one, and the picture must carry on from the same second. Without that,
// such a customer plays what was buffered when the page opened (two or three seconds) and
// then watches a spinner for ever.
//
// How the bucket words its refusal decides how fast Chromium notices: see "The refusal" at
// the top of serve.py. A real bucket's refusal is hidden from the player, and Chromium then
// asks the dead link again for half a minute before it reports an error. The page does not
// wait for that: it knows how long a link lives (data-life on the player, which serve.py
// sets to match these 4-second links), and when the player has been stuck for a second and
// a half on a link past its life, it asks for a new one.

const {
  test, expect, videoState, waitForMetadata, pressPlay, pressPause, waitForTime, watchNote, notesShown, sourceReloads,
} = require('../lib/harness');

const LIFE = 4;
const expired = (page) => page.waitForTimeout((LIFE + 2) * 1000);
// Left to itself Chromium asks a dead link 30 more times, over about 28 seconds, before it
// tells the page. The tests wait that long so that a page which has lost its own check fails
// on "not promptly", with the numbers, and not on a time-out.
const PATIENCE = 60_000;
const PROMPT = 8000; // longer than this with a frozen picture and the customer has given up

const refusals = (state) => state.bucket.filter((r) => r.status === 403).length;

/** Watch the player until the clip ends: did it, how long did the picture stand still on the way, and where? */
async function watchToTheEnd(page, limit = PATIENCE) {
  const started = Date.now();
  let at = -1;
  let moved = Date.now();
  let still = 0;
  let stuckAt = 0;
  for (;;) {
    const s = await videoState(page);
    if (s.time !== at) { at = s.time; moved = Date.now(); }
    if (Date.now() - moved > still) { still = Date.now() - moved; stuckAt = s.time; }
    if (s.ended || Date.now() - started > limit) return { ended: s.ended, still, stuckAt, took: Date.now() - started, s };
    await page.waitForTimeout(150);
  }
}

/** The parts of the clip this player has shown since its source was last loaded: [[from, to], ...]. */
const shown = (page) => page.evaluate(() => { const v = document.getElementById('v'); const out = []; for (let i = 0; i < v.played.length; i += 1) out.push([+v.played.start(i).toFixed(2), +v.played.end(i).toFixed(2)]); return out; });

/** What every recovery must end with, whatever the engine did on the way. Returns what was slow about it. */
async function expectRecovered(page, server, clip, testInfo, seen) {
  const state = await server.state(clip.token);
  const notes = await notesShown(page);
  testInfo.annotations.push({
    type: 'after the link expired',
    description: `the clip ${seen.ended ? 'ended' : 'did NOT end'} after ${(seen.took / 1000).toFixed(1)} s; the picture stood still for at most ${(seen.still / 1000).toFixed(1)} s; `
      + `the page asked /video ${state.hits.video} time(s); the bucket refused ${refusals(state)} request(s); notes shown: ${notes.join(' | ') || 'none'}`,
  });
  expect(seen.ended, `the video did not play to its end after the link expired (stuck at ${seen.s.time.toFixed(1)} of ${seen.s.duration.toFixed(0)} s, error "${seen.s.error}", note "${seen.s.note}")`).toBe(true);
  expect(seen.s.error).toBe('');
  expect(notes.filter((n) => /did not load/.test(n)), 'the page gave up ("The video did not load") on a video it could have fetched again').toEqual([]);
  expect(seen.s.noteShown, 'a note is still showing after the video has played').toBe(false);
  if (refusals(state)) {
    // The browser held a dead link: the page must have gone back to the server for a new one,
    // once or twice, not in a loop.
    expect(state.hits.video, 'the bucket refused the player but the page never asked the server for a fresh link').toBeGreaterThanOrEqual(2);
    const reloads = await sourceReloads(page);
    expect(reloads, 'the bucket refused the player but the page never loaded the video afresh').toBeGreaterThanOrEqual(1);
    expect(reloads, 'the page asked for a fresh link again and again').toBeLessThanOrEqual(2);
  } else {
    // Nothing was refused: this browser asks our own address for each piece of the file
    // (Firefox, Safari) or already had what it needed. No fresh link was called for. One is
    // allowed all the same: a player stuck for a second and a half on a link past its life
    // gets a new one whatever the reason, and on a busy test machine a slow answer looks the same.
    expect(await sourceReloads(page), 'nothing was refused, yet the page loaded the video afresh more than once').toBeLessThanOrEqual(1);
    expect(state.hits.video, 'far too many requests for one viewing (the server allows 600 a minute)').toBeLessThan(100);
  }
  await expect.poll(async () => (await server.state(clip.token)).played, { message: 'the server was never told "played"' }).toBe(1);
  expect((await server.state(clip.token)).hits.played, 'a recovery must not report a second viewing').toBe(1);

  const slow = [];
  if (seen.still > PROMPT) slow.push(`the picture stood still for ${(seen.still / 1000).toFixed(0)} s before it carried on`);
  if (notes.some((n) => /signal/.test(n))) slow.push('the customer was told "Your signal is weak or has dropped" although the signal was fine');
  return slow;
}

test('open the page, come back after the link has expired, press play: it recovers by itself and the whole video plays', async ({ page, server }, testInfo) => {
  test.setTimeout(150_000);
  const clip = await server.newVideo({ file: 'stretched', link_seconds: LIFE });
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await watchNote(page);
  await expired(page);

  expect(await pressPlay(page, testInfo)).not.toBe('');
  const seen = await watchToTheEnd(page);
  const slow = await expectRecovered(page, server, clip, testInfo, seen);
  if ((await sourceReloads(page)) > 0 && seen.stuckAt > 1) {
    // The page loaded the source again part-way through: what it has shown since must begin
    // where the picture froze, not at zero. (The customer does not sit through the start twice.)
    const parts = await shown(page);
    expect(parts.filter(([from]) => from < seen.stuckAt - 0.6), `after the fresh link the video started again from the beginning (it froze at ${seen.stuckAt.toFixed(1)} s and then showed ${JSON.stringify(parts)})`).toEqual([]);
  }
  expect(slow.join('; '), 'the video came back, but not promptly').toBe('');
});

test('after the link has expired, skip ahead while paused: the player gets there without starting by itself, then plays on', async ({ page, server }, testInfo) => {
  test.setTimeout(150_000);
  const clip = await server.newVideo({ file: 'stretched', link_seconds: LIFE });
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await watchNote(page);
  await expired(page);

  // The customer drags the slider to near the end, past anything the browser has. It needs
  // more of the file, and the link it holds is dead.
  const dragged = Date.now();
  await page.evaluate(() => { document.getElementById('v').currentTime = 6.5; });
  let startedByItself = '';
  await expect.poll(async () => {
    const s = await videoState(page);
    if (!s.paused) startedByItself = `the video started playing by itself at ${s.time.toFixed(1)} s while the customer had it paused`;
    return s.error ? `error ${s.error}` : (!s.seeking && s.ready >= 2 ? 'there' : `still fetching (readyState ${s.ready})`);
  }, { message: 'the player never got to the place the customer skipped to', timeout: PATIENCE, intervals: [200] }).toBe('there');
  const took = Date.now() - dragged;
  // The page fetched a fresh link for a PAUSED player: it must stay paused, at the same place.
  expect(startedByItself).toBe('');
  const resting = await videoState(page);
  expect(resting.paused, 'the video must wait for the customer to press play').toBe(true);
  expect(resting.time, 'the player lost the place the customer skipped to').toBeGreaterThan(6);
  expect(await shown(page), 'nothing should have played yet').toEqual([]);

  expect(await pressPlay(page, testInfo)).not.toBe('');
  const seen = await watchToTheEnd(page, 20_000);
  const slow = await expectRecovered(page, server, clip, testInfo, seen);
  const parts = await shown(page);
  expect(parts.filter(([from]) => from < 6), `the video went back to the start instead of carrying on from 6.5 s (it showed ${JSON.stringify(parts)})`).toEqual([]);
  if (took > PROMPT) slow.push(`the skip took ${(took / 1000).toFixed(0)} s to arrive`);
  expect(slow.join('; '), 'the player got there, but not promptly').toBe('');
});

test('play, pause, come back after the link has expired, press play: it carries on from the same second', async ({ page, server }, testInfo) => {
  // On a connection that was never cut, Chromium has the whole file by the time of the pause
  // and asks the bucket for nothing more; Firefox and Safari ask our own address and get a
  // fresh link. Either way: no wait, no note, no second "played", and not from the start.
  const clip = await server.newVideo({ file: 'stretched', link_seconds: LIFE });
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await watchNote(page);
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await waitForTime(page, 0.8);
  expect(await pressPause(page, testInfo)).not.toBe('');
  const pausedAt = (await videoState(page)).time;
  await expired(page);
  expect((await videoState(page)).time, 'the picture moved while paused').toBeCloseTo(pausedAt, 1);

  expect(await pressPlay(page, testInfo)).not.toBe('');
  const seen = await watchToTheEnd(page, 30_000);
  const slow = await expectRecovered(page, server, clip, testInfo, seen);
  expect(slow, 'after a pause the video must carry on at once').toEqual([]);
  expect(await notesShown(page), 'no note should show').toEqual([]);
  if ((await sourceReloads(page)) > 0) {
    // The page loaded the source again: what it has shown since must begin at the pause, not at zero.
    const parts = await shown(page);
    expect(parts.filter(([from]) => from < pausedAt - 0.6), `after a reload the video did not carry on from the pause at ${pausedAt.toFixed(1)} s (it showed ${JSON.stringify(parts)})`).toEqual([]);
  }
});

test('when the browser does tell the player it was refused, the fresh link is fetched at once and nothing shows', async ({ page, server }, testInfo) => {
  // The same expired link, with a refusal Chromium passes on to the player (serve.py:
  // refusal=visible). This is the page's own recovery with the browser's waiting taken away:
  // under a second, no note. It is also what a bucket on the page's own origin gives.
  const clip = await server.newVideo({ file: 'stretched', link_seconds: LIFE, refusal: 'visible' });
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await watchNote(page);
  await expired(page);

  expect(await pressPlay(page, testInfo)).not.toBe('');
  const seen = await watchToTheEnd(page, 30_000);
  const slow = await expectRecovered(page, server, clip, testInfo, seen);
  expect(slow, 'with the refusal in plain sight the recovery must be immediate').toEqual([]);
  expect(seen.still, 'the picture should barely pause while the fresh link is fetched').toBeLessThan(3000);
  expect(await notesShown(page), 'no note at all should show').toEqual([]);
});

test('a link that is still good plays the same file with no refusals (the control)', async ({ page, server }, testInfo) => {
  const clip = await server.newVideo({ file: 'stretched' });
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await watchNote(page);
  await page.waitForTimeout(1500);
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await page.evaluate(() => { document.getElementById('v').currentTime = 6.5; });
  await expect.poll(async () => (await videoState(page)).ended, { message: 'the video did not reach its end', timeout: 20_000 }).toBe(true);
  const state = await server.state(clip.token);
  expect(refusals(state)).toBe(0);
  expect(await notesShown(page), 'a video that plays must show no note').toEqual([]);
  const ranges = state.bucket.map((r) => r.range || 'whole file');
  testInfo.annotations.push({ type: 'byte ranges asked of the bucket', description: ranges.join(', ') });
  expect(state.bucket.some((r) => r.status === 206), 'a file this size should be fetched with byte ranges').toBe(true);
});
