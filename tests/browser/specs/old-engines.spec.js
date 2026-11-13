// What an OLD browser makes of the page: iOS 12 to 14 Safari (iPhone 5s, 6, 7, 8), the
// Chromium 53 to 85 inside 2017-2021 Samsung and LG TVs, old Samsung Internet.
//
// Those engines cannot be installed on a test machine. What CAN be done is to take away, from
// a modern engine, exactly the CSS they do not understand (the list comes from
// static/compat.spec.js) and look at the page that is left:
//     gap on a flex row      ignored before iOS 14.5, Safari 14.1, Chromium 84
//     :focus-visible         the whole rule is dropped before Safari 15.4, Chromium 86
//     aspect-ratio           ignored before iOS 15, Chromium 88
//     font-display           ignored before Chromium 60
// It is an approximation of their LAYOUT, not of their video player or their JavaScript
// (static/es5.spec.js covers the script).

const {
  test, expect, checkGap, projectFacts, sidewaysScroll, insideFirstScreen, waitForMetadata, pressPlay, waitForTime,
  press, focusByArrows, shot, pixelsAround, pixelsChanged, videoState,
} = require('../lib/harness');
const { PNG } = require('pngjs');

// The page's own <style> is rewritten in place as soon as the document is parsed. (Rewriting
// the HTML on the wire instead makes Chromium treat the page as coming from outside this
// machine, and it then refuses the page's requests to the local stand-in bucket.)
async function open(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => {
    const style = document.querySelector('style');
    style.textContent = style.textContent
      .replace(/([;{])gap:[^;}]+/g, '$1')
      .replace(/\n[^\n{}]*:focus-visible[^{}]*\{[^}]*\}/g, '\n')
      .replace(/([;{])aspect-ratio:[^;}]+/g, '$1')
      .replace(/([;{])font-display:[^;}]+/g, '$1');
  });
}

test('old engine: the page still fits, the video plays, a score is saved', async ({ page, clip, server }, testInfo) => {
  await open(page, clip.watch);
  const css = await page.evaluate(() => document.querySelector('style').textContent);
  expect(css, 'the old-engine stand-in did not take the newer CSS away').not.toMatch(/gap:|:focus-visible|aspect-ratio|font-display/);
  await waitForMetadata(page);
  await shot(page, testInfo, 'as-an-old-engine');
  expect.soft(await sidewaysScroll(page), 'the page scrolls sideways').toEqual([]);
  const { height } = projectFacts(testInfo);
  if (height >= 331) expect.soft(await insideFirstScreen(page, '#v', 'the player')).toBe('');
  const box = await page.locator('#v').boundingBox();
  expect.soft(box.width / box.height, 'without aspect-ratio the player must still take the clip\'s 9:16 shape once it has loaded').toBeGreaterThan(0.54);
  expect.soft(box.width / box.height).toBeLessThan(0.58);

  expect(await pressPlay(page, testInfo)).not.toBe('');
  await waitForTime(page, 1);
  expect((await videoState(page)).error).toBe('');
  await press(page, testInfo, 'label[for="s4"]');
  await press(page, testInfo, '#score button[type=submit]');
  await expect(page.locator('#score .ok')).toContainText('4 of 5 stars');
  expect((await server.state(clip.token)).score.stars).toBe(4);
});

test('old engine: the logo does not touch the heading', async ({ page, clip }, testInfo) => {
  await open(page, clip.watch);
  const space = await page.evaluate(() => {
    const logo = document.querySelector('header.top img').getBoundingClientRect();
    const heading = document.querySelector('header.top h1').getBoundingClientRect();
    return Math.round(heading.left - logo.right);
  });
  checkGap(testInfo, 'old-engines-logo-touches-heading', space >= 8 ? '' : `${space} px between the logo and the heading`, 'the logo touches the heading');
});

test('old engine: the star that has the focus is outlined', async ({ page, clip }, testInfo) => {
  const { input } = projectFacts(testInfo);
  test.skip(input === 'touch', 'a finger has no focus to show');
  await open(page, clip.watch);
  if (input === 'dpad') await focusByArrows(page, 'label[for="s5"]');
  else {
    for (let i = 0; i < 20; i += 1) {
      await page.keyboard.press('Tab');
      if (await page.evaluate(() => document.activeElement && document.activeElement.type === 'radio')) break;
    }
  }
  const focused = await page.evaluate(() => { const a = document.activeElement; return a && (a.htmlFor || a.id); });
  expect(focused, 'the keys never reached the stars').toMatch(/^s[1-5]$/);
  const withFocus = await pixelsAround(page, `label[for="${focused}"]`);
  await page.evaluate(() => document.activeElement.blur());
  const without = PNG.sync.read(await page.screenshot({ clip: withFocus.clip, animations: 'disabled', caret: 'hide' }));
  const changed = pixelsChanged(withFocus.png, without);
  checkGap(testInfo, 'old-engines-star-focus-is-invisible', changed > 200 ? '' : `only ${changed} pixels change when a star has the focus`, 'a focused star shows no outline');
});

test('old engine: before the video has loaded the player is a small box, not a tall one', async ({ page, server }, testInfo) => {
  // Hold the bucket back so the moment before the video's details arrive can be measured.
  const clip = await server.newVideo({ first_byte_ms: 4000 });
  await open(page, clip.watch);
  const before = await page.locator('#v').boundingBox();
  const scoreBefore = (await page.locator('#score').boundingBox()).y;
  await waitForMetadata(page);
  const after = await page.locator('#v').boundingBox();
  const scoreAfter = (await page.locator('#score').boundingBox()).y;
  testInfo.annotations.push({ type: 'old engine', description: `player ${Math.round(before.width)}x${Math.round(before.height)} px before the video's details arrive, ${Math.round(after.width)}x${Math.round(after.height)} after; the score form moves down ${Math.round(scoreAfter - scoreBefore)} px` });
  checkGap(testInfo, 'old-engines-page-jumps-when-video-loads', Math.abs(scoreAfter - scoreBefore) <= 4 ? '' : `the score form jumps ${Math.round(scoreAfter - scoreBefore)} px when the video's details arrive`,
    'the page jumps when the video loads');
});
