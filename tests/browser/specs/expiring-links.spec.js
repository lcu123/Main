// The bucket link the player is redirected to lives 15 minutes in production. Here it lives
// 4 seconds (serve.py: link_seconds), on a file with the bit rate of a real visit video, so
// that a customer who opens the page, puts the phone down and comes back is 6 seconds away
// instead of 16 minutes.
//
// The server signs a fresh link every time /w/<token>/video is asked. That only helps if the
// browser asks THAT address again. What each engine does is what these tests find out.

const {
  test, expect, knownToFail, checkGap, videoState, waitForMetadata, pressPlay, pressPause, waitForTime,
} = require('../lib/harness');

const LIFE = 4;
const expired = (page) => page.waitForTimeout((LIFE + 2) * 1000);

function refusals(state) {
  return state.bucket.filter((r) => r.status === 403).length;
}

async function describe(page, server, clip, testInfo) {
  const s = await videoState(page);
  const state = await server.state(clip.token);
  testInfo.annotations.push({
    type: 'after the link expired',
    description: `time ${s.time.toFixed(1)} of ${s.duration.toFixed(0)} s, ${s.seeking ? 'stuck seeking, ' : ''}${s.paused ? 'paused' : 'not paused'}, `
      + `error "${s.error}", note ${s.noteShown ? 'shown' : 'hidden'}; the page asked /video ${state.hits.video} time(s), `
      + `the bucket refused ${refusals(state)} request(s)`,
  });
  return { s, state };
}

test('open the page, come back after the link has expired, press play: the whole video plays', async ({ page, server }, testInfo) => {
  const clip = await server.newVideo({ file: 'stretched', link_seconds: LIFE });
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await expired(page);

  await knownToFail(testInfo, 'expired-link-stalls', async () => {
    expect(await pressPlay(page, testInfo)).not.toBe('');
    try {
      await expect.poll(async () => (await videoState(page)).ended, { message: 'the video did not play to its end after the link expired', timeout: 15_000 }).toBe(true);
    } finally {
      await describe(page, server, clip, testInfo);
    }
  });
});

test('play, pause, come back after the link has expired, seek and play on: it still plays', async ({ page, server }, testInfo) => {
  const clip = await server.newVideo({ file: 'stretched', link_seconds: LIFE });
  await page.goto(clip.watch);
  await waitForMetadata(page);
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await waitForTime(page, 0.8);
  expect(await pressPause(page, testInfo)).not.toBe('');
  await expired(page);

  await knownToFail(testInfo, 'expired-link-seek-after-pause', async () => {
    try {
      await page.evaluate(() => { document.getElementById('v').currentTime = 7; });
      await expect.poll(async () => { const s = await videoState(page); return !s.seeking && s.ready >= 2; }, { message: 'a seek after the link expired never finished', timeout: 15_000 }).toBe(true);
      expect(await pressPlay(page, testInfo)).not.toBe('');
      await expect.poll(async () => (await videoState(page)).ended, { message: 'the video did not play on to its end', timeout: 15_000 }).toBe(true);
      // And again from the start.
      await page.evaluate(() => { document.getElementById('v').currentTime = 0; });
      expect(await pressPlay(page, testInfo)).not.toBe('');
      await waitForTime(page, 1.5);
    } finally {
      await describe(page, server, clip, testInfo);
    }
  });
});

test('when the video cannot go on, the customer is told', async ({ page, server }, testInfo) => {
  // Whatever the cause (here: every link for this video is dead), a customer looking at a
  // player that will never move again should get the page's "did not load" note.
  const clip = await server.newVideo({ file: 'stretched', link_seconds: LIFE });
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await expired(page);
  await pressPlay(page, testInfo);
  await page.evaluate(() => { document.getElementById('v').currentTime = 7; });
  await page.waitForTimeout(6000);
  const { s, state } = await describe(page, server, clip, testInfo);
  test.skip(refusals(state) === 0, 'this engine asked the server for a fresh link, so nothing was refused and there is nothing to tell the customer');
  const stuck = s.seeking || s.ready < 2 || !!s.error;
  expect(stuck, 'the bucket refused the player, so the video should not be playing').toBe(true);
  checkGap(testInfo, 'stall-shows-no-message', s.noteShown ? '' : `the player is stuck (${s.seeking ? 'seeking forever' : `readyState ${s.ready}`}, error "${s.error}") and the "did not load" note is hidden`,
    'the customer is shown nothing when the video cannot go on');
});

test('a link that is still good plays the same file with no refusals (the control)', async ({ page, server }, testInfo) => {
  const clip = await server.newVideo({ file: 'stretched' });
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await page.waitForTimeout(1500);
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await page.evaluate(() => { document.getElementById('v').currentTime = 6.5; });
  await expect.poll(async () => (await videoState(page)).ended, { message: 'the video did not reach its end', timeout: 20_000 }).toBe(true);
  const state = await server.state(clip.token);
  expect(refusals(state)).toBe(0);
  const ranges = state.bucket.map((r) => r.range || 'whole file');
  testInfo.annotations.push({ type: 'byte ranges asked of the bucket', description: ranges.join(', ') });
  expect(state.bucket.some((r) => r.status === 206), 'a file this size should be fetched with byte ranges').toBe(true);
});
