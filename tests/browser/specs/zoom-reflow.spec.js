// Zoom. A customer who cannot read small print zooms: Ctrl and + on a desktop (200% of a
// 1280x720 window lays the page out in 640x360 CSS px; 400% in 320x180), or "Page Zoom" in
// Safari's settings on an iPhone (150% of a 390 px wide phone is 260 px; 200% is 195 px).
// WCAG 1.4.10 asks that at 320 px nothing needs sideways scrolling. Runs on the desktop engines.

const {
  test, expect, checkGap, sidewaysScroll, waitForMetadata, pressPlay, waitForTime, press, shot, videoState,
} = require('../lib/harness');

const SIZES = [
  // name                                  width height scale  what must still work
  ['desktop at 200% (640x360)',             640, 360, 2],
  ['desktop at 400% (320x180)',             320, 180, 4],
  ['iPhone page zoom 150% (260 px wide)',   260, 443, 3],
  ['iPhone page zoom 200% (195 px wide)',   195, 332, 3],
];

for (const [name, width, height, scale] of SIZES) {
  test.describe(name, () => {
    test.use({ viewport: { width, height }, deviceScaleFactor: scale, hasTouch: false });

    test('nothing scrolls sideways, the words wrap, the form still works', async ({ page, clip, server }, testInfo) => {
      await page.goto(clip.watch);
      await waitForMetadata(page);
      await shot(page, testInfo, `zoom-${width}x${height}`);

      const sideways = await sidewaysScroll(page);
      // (the stars: see 'stars-wider-than-the-screen' in lib/known-gaps.js; asked here about THIS window's width)
      checkGap(testInfo, 'stars-wider-than-the-screen', sideways, 'the page scrolls sideways', { width });

      // No text is cut off: every block of words is as wide as its box allows, and no wider.
      const clipped = await page.evaluate(() => [...document.querySelectorAll('h1, h2, p, summary, .btn, footer')]
        .filter((el) => el.getBoundingClientRect().width > 0 && el.scrollWidth > el.clientWidth + 1)
        .map((el) => `${el.tagName.toLowerCase()} "${el.textContent.trim().slice(0, 30)}" needs ${el.scrollWidth} px in a ${el.clientWidth} px box`));
      expect.soft(clipped, 'text is cut off').toEqual([]);

      // The player must stay big enough to see and to press.
      const box = await page.locator('#v').boundingBox();
      testInfo.annotations.push({ type: 'player size', description: `${Math.round(box.width)}x${Math.round(box.height)} px in a ${width}x${height} window` });
      const tooSmall = box.width >= 120 ? '' : `the player is ${Math.round(box.width)}x${Math.round(box.height)} px`;
      if (height < 250) checkGap(testInfo, 'player-shrinks-to-nothing-when-zoomed', tooSmall, 'the player is too small to watch or press');
      else expect.soft(tooSmall, 'the player is too small to watch or press').toBe('');

      // Play, score: still possible.
      expect(await pressPlay(page, testInfo)).not.toBe('');
      await waitForTime(page, 0.5);
      expect((await videoState(page)).error).toBe('');
      await press(page, testInfo, 'label[for="s4"]');
      await press(page, testInfo, '#score button[type=submit]');
      await expect(page.locator('#score .ok')).toContainText('4 of 5 stars');
      expect((await server.state(clip.token)).score.stars).toBe(4);
    });
  });
}
