// What if a video ever arrives with its index (the "moov atom") at the END of the file
// instead of the front? The iPhone app exports faststart files; this proves what a customer
// gets if one day it does not (an app update, a re-encode, a file fixed by hand).

const {
  test, expect, projectFacts, videoState, waitForMetadata, pressPlay, waitForTime,
} = require('../lib/harness');

/**
 * Open a watch page and time how long the player needs to know the video: from the first byte
 * of the page, NOT from the page's "load" event. A browser may hold "load" back for as long
 * as its request for the video is still running (WebKit does, and its one request is for the
 * whole file), and the customer does not wait for "load": the words, the stars and the player
 * are on screen long before it.
 */
async function timeToMetadata(page, server, options) {
  const clip = await server.newVideo(options);
  const started = Date.now();
  const marks = { load: 0 };
  const onLoad = () => { if (page.url().startsWith(clip.watch)) marks.load = Date.now() - started; };
  page.on('load', onLoad);
  await page.goto(clip.watch, { waitUntil: 'commit' });
  await waitForMetadata(page, 45_000);
  const ms = Date.now() - started;
  page.off('load', onLoad);
  return { clip, ms, loadMs: marks.load };
}

test('a file with its index at the end still plays', async ({ page, server }, testInfo) => {
  const { clip } = await timeToMetadata(page, server, { file: 'moov-at-end' });
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await waitForTime(page, 1.2);
  await page.evaluate(() => { document.getElementById('v').currentTime = 6.5; });
  await expect.poll(async () => (await videoState(page)).ended, { message: 'the video did not reach its end', timeout: 20_000 }).toBe(true);
  expect((await videoState(page)).error).toBe('');
  const state = await server.state(clip.token);
  testInfo.annotations.push({ type: 'byte ranges asked of the bucket', description: state.bucket.map((r) => r.range || 'whole file').join(', ') });
});

test('on a slow connection: a faststart file is ready early, and an index at the end is found without the whole file first', async ({ page, server }, testInfo) => {
  // A full-size file at 300 KB a second, index at the front, then at the end, in this same browser.
  const RATE = 300000;
  const whole = Math.round((server.info.files.stretched.bytes / RATE) * 1000); // 9 s: what the whole file takes
  const front = await timeToMetadata(page, server, { file: 'stretched', throttle: RATE });
  const frontAsked = (await server.state(front.clip.token)).bucket.map((r) => r.range || 'whole file');
  const end = await timeToMetadata(page, server, { file: 'stretched-moov-at-end', throttle: RATE });
  const state = await server.state(end.clip.token);
  const endAsked = state.bucket.map((r) => r.range || 'whole file');
  const size = server.info.files['stretched-moov-at-end'].bytes;
  const wentForTheTail = state.bucket.some((r) => { const m = /^bytes=(\d+)-/.exec(r.range || ''); return m && Number(m[1]) > size * 0.9; });
  const line = `player ready after ${front.ms} ms with the index at the front (bucket was asked: ${frontAsked.join(', ')}; `
    + `"load" ${front.loadMs ? `fired after ${front.loadMs} ms` : 'had not fired yet'}), ${end.ms} ms with it at the end `
    + `(${wentForTheTail ? 'the browser asked for the end of the file separately' : 'the browser read the file from the start until it found the index'}: `
    + `${endAsked.join(', ')}); the whole file takes ${whole} ms at this speed`;
  testInfo.annotations.push({ type: 'faststart', description: line });
  if (projectFacts(testInfo).engine === 'webkit') console.log(`[${testInfo.project.name}] ${line}`); // into the CI log: see known-gaps.js

  // Every engine: the player must not need longer than the whole file plus a margin, either way round.
  expect(front.ms, `with the index at the front the player took too long to get ready (the whole file takes ${whole} ms)`).toBeLessThan(whole + 8000);
  expect(end.ms, 'with the index at the end the player took too long to get ready').toBeLessThan(whole + 11_000);

  // What faststart is for: the player is ready after the index and the first frames, long
  // before the file is in, and never later than the same file with its index at the end.
  // Chromium and Firefox do that (under 2.5 s for both files in the first CI run). The limit
  // is half the time the whole file takes, so it is a statement about THIS browser on THIS
  // connection and not a number tuned on Chromium.
  const late = [];
  if (front.ms > whole / 2) late.push(`a faststart file was only ready after ${front.ms} ms; the whole file takes ${whole} ms`);
  if (front.ms > end.ms + 1500) late.push(`faststart (${front.ms} ms) was slower than the same file with its index at the end (${end.ms} ms)`);
  // (Safari's engine was in doubt after the first CI run, which had also waited for the
  // page's "load" event. Measured without that wait it does the same as the others, on Linux
  // and on Apple's own video stack on a Mac: runs of 2026-11-14.)
  expect.soft(late.join('; '), 'on a slow connection the player waited for far more than the index').toBe('');

  expect(await pressPlay(page, testInfo)).not.toBe('');
  await waitForTime(page, 0.5, 40_000);
  expect((await videoState(page)).error).toBe('');
});
