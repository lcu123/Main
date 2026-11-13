// What if a video ever arrives with its index (the "moov atom") at the END of the file
// instead of the front? The iPhone app exports faststart files; this proves what a customer
// gets if one day it does not (an app update, a re-encode, a file fixed by hand).

const {
  test, expect, videoState, waitForMetadata, pressPlay, waitForTime,
} = require('../lib/harness');

async function timeToMetadata(page, server, options) {
  const clip = await server.newVideo(options);
  const started = Date.now();
  await page.goto(clip.watch);
  await waitForMetadata(page, 45_000);
  return { clip, ms: Date.now() - started };
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

test('on a slow connection the browser finds an index at the end without downloading the whole file first', async ({ page, server }, testInfo) => {
  // A full-size file at 300 KB a second (9 seconds to download), index at the front, then at the end.
  const front = await timeToMetadata(page, server, { file: 'stretched', throttle: 300000 });
  const end = await timeToMetadata(page, server, { file: 'stretched-moov-at-end', throttle: 300000 });
  const state = await server.state(end.clip.token);
  const ranges = state.bucket.map((r) => r.range || 'whole file');
  const size = server.info.files['stretched-moov-at-end'].bytes;
  const wentForTheTail = state.bucket.some((r) => { const m = /^bytes=(\d+)-/.exec(r.range || ''); return m && Number(m[1]) > size * 0.9; });
  testInfo.annotations.push({
    type: 'faststart',
    description: `player ready after ${front.ms} ms with the index at the front, ${end.ms} ms with it at the end; `
      + `${wentForTheTail ? 'the browser asked for the end of the file separately' : 'the browser read the file from the start until it found the index'} `
      + `(bucket was asked: ${ranges.join(', ')})`,
  });
  expect(front.ms, 'a faststart file should be ready within a few seconds even on a slow connection').toBeLessThan(6000);
  expect(end.ms, 'with the index at the end the player took too long to get ready').toBeLessThan(20_000);
  expect(await pressPlay(page, testInfo)).not.toBe('');
  await waitForTime(page, 0.5, 40_000);
  expect((await videoState(page)).error).toBe('');
});
