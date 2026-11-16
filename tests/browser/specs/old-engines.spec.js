// What an OLD browser makes of the page: iOS 12 to 14 Safari (iPhone 5s, 6, 7, 8), the
// Chromium 53 to 85 inside 2017-2021 Samsung and LG TVs, old Samsung Internet.
//
// Those engines cannot be installed on a test machine. What CAN be done is to take away, from
// a modern engine, exactly the CSS they do not understand (the list comes from
// static/compat.spec.js) and look at the page that is left:
//     @supports (aspect-ratio:1/1){...}  the whole block is skipped before iOS 15, Chromium 88
//     max(...) and min(...)              the declaration is dropped before iOS 11.3 (min/max
//                                        with units: 13.4), Chromium 79: an earlier plain
//                                        declaration of the same property then applies
//     :focus-visible                     the whole RULE is dropped before Safari 15.4, Chromium 86,
//                                        and that includes ":focus:not(:focus-visible){outline:0}",
//                                        the rule that takes the ring away again for a mouse
//     @media (forced-colors:active)      skipped before Chromium 89, Safari 16
//     gap on a flex row                  ignored before iOS 14.5, Chromium 84 (the page no longer uses it)
//     font-display                       ignored before Chromium 60
// It is an approximation of their LAYOUT, not of their video player or their JavaScript
// (static/es5.spec.js covers the script).
//
// What the page promises such a browser (the comments in watch.html and _base.html):
//   * the player is a box of fixed height (74% of the window) from the first moment, so the
//     page does not jump when the video's size arrives;
//   * the logo keeps its distance from the heading by a margin, not by gap;
//   * a focused star, button or link is outlined by a plain :focus rule.

const {
  test, expect, projectFacts, sidewaysScroll, insideFirstScreen, waitForMetadata, pressPlay, waitForTime,
  press, focusByArrows, tabKey, shot, pixelsAround, pixelsChanged, videoState, pictureInPlayer,
} = require('../lib/harness');
const { PNG } = require('pngjs');

const NEWER_CSS = /@supports|aspect-ratio|\bmax\(|\bmin\(|:focus-visible|forced-colors|[;{]gap:|font-display/;

/** Take out of one stylesheet's text what an old engine would not apply. */
function asAnOldEngine(css) {
  /** Remove every "@name (...) { ... }" block, braces matched. */
  const dropBlocks = (text, opener) => {
    let out = '';
    let i = 0;
    for (;;) {
      const at = text.indexOf(opener, i);
      if (at < 0) return out + text.slice(i);
      out += text.slice(i, at);
      let depth = 0;
      let j = text.indexOf('{', at);
      if (j < 0) return out;
      for (; j < text.length; j += 1) {
        if (text[j] === '{') depth += 1;
        if (text[j] === '}') { depth -= 1; if (depth === 0) break; }
      }
      i = j + 1;
    }
  };
  let text = css.replace(/\/\*[\s\S]*?\*\//g, ''); // comments mention the newer features by name
  text = dropBlocks(text, '@supports');
  text = dropBlocks(text, '@media (forced-colors');
  return text
    .replace(/(^|\})([^{}]*:focus-visible[^{}]*)\{[^{}]*\}/g, '$1') // a rule with a selector it cannot read is dropped whole
    .replace(/([;{])[a-z-]+:[^;{}]*\b(?:max|min)\([^;{}]*(?=[;}])/g, '$1') // a value it cannot read drops that one declaration
    .replace(/([;{])gap:[^;}]+/g, '$1')
    .replace(/([;{])font-display:[^;}]+/g, '$1');
}

// The page's own <style> is rewritten in place as soon as the document is parsed. (Rewriting
// the HTML on the wire instead makes Chromium treat the page as coming from outside this
// machine, and it then refuses the page's requests to the local stand-in bucket.)
async function open(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  const css = await page.evaluate(() => document.querySelector('style').textContent);
  const stripped = asAnOldEngine(css);
  await page.evaluate((text) => { document.querySelector('style').textContent = text; }, stripped);
  return stripped;
}

test('the old-engine stand-in takes away everything an old engine would not apply, and only that', async ({ page, clip }) => {
  const before = await (await page.request.get(clip.watch)).text();
  expect(before, 'the page no longer uses any of the newer CSS: this spec has nothing left to stand in for').toMatch(NEWER_CSS);
  const css = await open(page, clip.watch);
  expect(css, 'the stand-in left newer CSS in the page').not.toMatch(NEWER_CSS);
  // What must still be there, because old engines DO apply it.
  for (const kept of [
    '.player video{display:block;width:100%;height:74vh;background:#000}', // the fixed box
    '.wrap{max-width:560px;', // the plain column width, without the max() after it
    'header.top img{width:58px;height:58px;flex:none;margin-right:12px}',
    '.stars input:focus+label{outline:3px solid #932FFB;border-radius:8px}',
    '.btn:focus,footer a:focus,summary:focus{outline:4px solid #932FFB;outline-offset:3px}',
    '.player video:focus{outline:5px solid #FFF515;outline-offset:-5px}',
    '#verr[hidden]{display:none}',
  ]) expect(css, `the stand-in removed a rule old engines understand: ${kept}`).toContain(kept);
  const applied = await page.evaluate(() => {
    const v = getComputedStyle(document.getElementById('v'));
    return { height: v.height, wrap: getComputedStyle(document.querySelector('.wrap')).maxWidth, window: window.innerHeight };
  });
  // (The width="720" height="1280" attributes still give a modern engine a ratio to fall back
  // on; with a width AND a height set by the page's CSS it changes nothing, as on an old engine.)
  expect(parseFloat(applied.height), 'the player should be the fixed 74% of the window').toBeCloseTo(applied.window * 0.74, 0);
  expect(applied.wrap).toBe('560px');
});

test('old engine: the page still fits, the picture is whole, the video plays, a score is saved', async ({ page, clip, server }, testInfo) => {
  await open(page, clip.watch);
  await waitForMetadata(page);
  await shot(page, testInfo, 'as-an-old-engine');
  expect.soft(await sidewaysScroll(page), 'the page scrolls sideways').toEqual([]);
  const { height } = projectFacts(testInfo);
  if (height >= 331) expect.soft(await insideFirstScreen(page, '#v', 'the player')).toBe('');
  // Without aspect-ratio the box is 74% of the window tall whatever its width; the picture
  // must sit inside it whole (the browser's default, object-fit: contain), never stretched to it.
  const player = await pictureInPlayer(page);
  testInfo.annotations.push({ type: 'old engine', description: `player ${Math.round(player.box.width)}x${Math.round(player.box.height)} px, picture ${player.picture.width}x${player.picture.height} px` });
  expect.soft(player.wrong, 'the picture is squashed or cropped, or the player does not fill its column').toEqual([]);
  expect.soft(player.box.height, 'the player should be 74% of the window tall').toBeCloseTo(height * 0.74, 0);
  expect.soft(player.picture.width, 'the picture is too small to watch').toBeGreaterThanOrEqual(120);

  expect(await pressPlay(page, testInfo)).not.toBe('');
  await waitForTime(page, 1);
  expect((await videoState(page)).error).toBe('');
  await press(page, testInfo, 'label[for="s4"]');
  await press(page, testInfo, '#score button[type=submit]');
  await expect(page.locator('#score .ok')).toContainText('4 of 5 stars');
  expect((await server.state(clip.token)).score.stars).toBe(4);
});

test('old engine: the logo does not touch the heading', async ({ page, clip }) => {
  // The space used to come from "gap", which iPhones before iOS 14.5 and TVs before 2021
  // ignore: the logo sat right against the first letter of the heading.
  await open(page, clip.watch);
  const space = await page.evaluate(() => {
    const logo = document.querySelector('header.top img').getBoundingClientRect();
    const heading = document.querySelector('header.top h1').getBoundingClientRect();
    return Math.round(heading.left - logo.right);
  });
  expect(space, 'px between the logo and the heading').toBeGreaterThanOrEqual(8);
});

test('old engine: the star that has the focus is outlined', async ({ page, clip }, testInfo) => {
  // The radio buttons behind the stars are invisible on purpose; the outline is drawn on the
  // star's label. When the only rule that drew it needed :focus-visible, an old engine drew
  // nothing: a keyboard user could not see which star they were on.
  const { input } = projectFacts(testInfo);
  test.skip(input === 'touch', 'a finger has no focus to show');
  await open(page, clip.watch);
  if (input === 'dpad') await focusByArrows(page, 'label[for="s5"]');
  else {
    for (let i = 0; i < 20; i += 1) {
      await page.keyboard.press(tabKey(testInfo));
      if (await page.evaluate(() => document.activeElement && document.activeElement.type === 'radio')) break;
    }
  }
  const focused = await page.evaluate(() => { const a = document.activeElement; return a && (a.htmlFor || a.id); });
  expect(focused, 'the keys never reached the stars').toMatch(/^s[1-5]$/);
  const withFocus = await pixelsAround(page, `label[for="${focused}"]`);
  await page.evaluate(() => document.activeElement.blur());
  const without = PNG.sync.read(await page.screenshot({ clip: withFocus.clip, animations: 'disabled', caret: 'hide' }));
  const changed = pixelsChanged(withFocus.png, without);
  const around = 2 * (withFocus.box.width + withFocus.box.height) * withFocus.scale * withFocus.scale;
  expect(changed / around, `a focused star shows no outline: only ${changed} pixels change when it has the focus`).toBeGreaterThan(0.8);
});

test('old engine: the player\'s place is kept before the video has loaded, so nothing jumps', async ({ page, server }, testInfo) => {
  // Hold the bucket back so the moment before the video's details arrive can be measured.
  // Without aspect-ratio and without a fixed height the player was a 300x150 band at first,
  // then grew by 340 to 650 px and pushed the score form down under the customer's finger.
  const clip = await server.newVideo({ first_byte_ms: 4000 });
  await open(page, clip.watch);
  expect((await videoState(page)).ready, 'the bucket has not sent a byte yet').toBe(0);
  const before = await page.locator('#v').boundingBox();
  const scoreBefore = (await page.locator('#score').boundingBox()).y;
  await waitForMetadata(page);
  const after = await page.locator('#v').boundingBox();
  const scoreAfter = (await page.locator('#score').boundingBox()).y;
  testInfo.annotations.push({ type: 'old engine', description: `player ${Math.round(before.width)}x${Math.round(before.height)} px before the video's details arrive, ${Math.round(after.width)}x${Math.round(after.height)} after; the score form moves ${Math.round(scoreAfter - scoreBefore)} px` });
  expect(Math.abs(scoreAfter - scoreBefore), 'the score form jumped when the video\'s details arrived').toBeLessThanOrEqual(4);
  expect(Math.abs(after.height - before.height), 'the player changed height when the video\'s details arrived').toBeLessThanOrEqual(2);
  expect(Math.abs(after.width - before.width), 'the player changed width when the video\'s details arrived').toBeLessThanOrEqual(2);
});
