// Zoom. A customer who cannot read small print zooms: Ctrl and + on a desktop (200% of a
// 1280x720 window lays the page out in 640x360 CSS px; 400% in 320x180), or "Page Zoom" in
// Safari's settings on an iPhone (150% of a 390 px wide phone is 260 px; 200% is 195 px).
// WCAG 1.4.10 asks that at 320 px nothing needs sideways scrolling. Runs on the desktop engines.

const {
  test, expect, sidewaysScroll, waitForMetadata, pressPlay, waitForTime, press, shot, videoState, pictureInPlayer,
} = require('../lib/harness');

const SIZES = [
  // name                                  width height scale
  ['desktop at 200% (640x360)',             640, 360, 2],
  ['desktop at 400% (320x180)',             320, 180, 4],
  ['iPhone page zoom 150% (260 px wide)',   260, 443, 3],
  ['iPhone page zoom 200% (195 px wide)',   195, 332, 3],
];

for (const [name, width, height, scale] of SIZES) {
  test.describe(name, () => {
    test.use({ viewport: { width, height }, deviceScaleFactor: scale, hasTouch: false });

    test('nothing scrolls sideways, the words wrap, the player and the five stars keep their room, the form still works', async ({ page, clip, server }, testInfo) => {
      await page.goto(clip.watch);
      await waitForMetadata(page);
      await shot(page, testInfo, `zoom-${width}x${height}`);

      // Sideways scroll, and words cut off: every block of words is as wide as its box allows, and no wider.
      // That includes the office number on its button: in the 195 px window it wraps inside the
      // button (.nb is an inline block, not "never wrap").
      const sideways = await sidewaysScroll(page);
      const clipped = await page.evaluate(() => [...document.querySelectorAll('h1, h2, p, summary, .btn, footer')]
        .filter((el) => el.getBoundingClientRect().width > 0 && el.scrollWidth > el.clientWidth + 1)
        .map((el) => `${el.tagName.toLowerCase()} "${el.textContent.trim().slice(0, 30)}" needs ${el.scrollWidth} px in a ${el.clientWidth} px box`));
      expect.soft(sideways, 'the page scrolls sideways').toEqual([]);
      expect.soft(clipped, 'text is cut off').toEqual([]);
      const number = await page.locator('main a.btn .nb').evaluate((el) => {
        const n = el.getBoundingClientRect();
        const b = el.closest('a').getBoundingClientRect();
        return { text: el.textContent, out: Math.round(Math.max(n.right - b.right, b.left - n.left, n.bottom - b.bottom)) };
      });
      expect.soft(number.out, `the office number ${number.text} sticks out of its button by ${number.out} px`).toBeLessThanOrEqual(0);

      // The player. It used to be capped at 74% of the window HEIGHT and keep a 9:16 box: in the
      // 180 px tall window of a desktop at 400% that was a 75x133 px stamp. Now the box is the
      // full column wide and never under 240 px tall, and the picture sits whole inside it.
      const player = await pictureInPlayer(page);
      testInfo.annotations.push({ type: 'player size', description: `${Math.round(player.box.width)}x${Math.round(player.box.height)} px box, ${player.picture.width}x${player.picture.height} px picture, in a ${width}x${height} window` });
      expect.soft(player.wrong, 'the picture is squashed or cropped, or the player does not fill its column').toEqual([]);
      expect.soft(player.box.height, 'the player must not shrink with the window: it should scroll instead').toBeGreaterThanOrEqual(240);
      expect.soft(player.picture.width, 'the picture is too small to watch').toBeGreaterThanOrEqual(120);
      // Room for the browser's own control bar (play, time, sound, full screen): 280 px, or the whole column where the window is narrower.
      expect.soft(player.box.width, 'the player is too narrow for its own buttons').toBeGreaterThanOrEqual(Math.min(280, player.column));
      expect.soft(player.column, 'the column should use the whole window but its 16 px margins').toBeGreaterThanOrEqual(Math.min(528, width - 32) - 1);

      // The five stars: all inside the card, side by side, each at least 44 px tall, and as wide
      // as a fifth of the room allows (44 px or more where the card has it). The typed stars
      // of before were 45 to 56 px wide whatever the window: at these sizes the first and the
      // last hung off the screen, and at 195 px the 1-star could not be reached at all.
      const row = await page.evaluate(() => {
        const card = document.getElementById('score');
        const box = card.getBoundingClientRect();
        const style = getComputedStyle(card);
        const inner = box.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
        const stars = [1, 2, 3, 4, 5].map((n) => {
          const r = document.querySelector(`label[for="s${n}"]`).getBoundingClientRect();
          const drawing = document.querySelector(`label[for="s${n}"] svg`).getBoundingClientRect();
          return { n, left: r.left, right: r.right, width: r.width, height: r.height, drawing: drawing.width };
        });
        return { card: { left: box.left, right: box.right, inner }, stars, window: document.documentElement.clientWidth };
      });
      testInfo.annotations.push({ type: 'stars', description: `each star is ${Math.round(row.stars[0].width)}x${Math.round(row.stars[0].height)} px (the drawing ${Math.round(row.stars[0].drawing)} px) in a card with ${Math.round(row.card.inner)} px of room` });
      const wrong = [];
      for (const [i, s] of row.stars.entries()) {
        if (s.left < row.card.left - 0.5 || s.right > row.card.right + 0.5) wrong.push(`star ${s.n} is outside the card (${Math.round(s.left)}..${Math.round(s.right)} px, the card is ${Math.round(row.card.left)}..${Math.round(row.card.right)})`);
        if (s.left < -0.5 || s.right > row.window + 0.5) wrong.push(`star ${s.n} is off the screen`);
        if (i > 0 && s.left < row.stars[i - 1].right - 0.5) wrong.push(`star ${s.n} overlaps star ${row.stars[i - 1].n}`);
        if (s.height < 44) wrong.push(`star ${s.n} is ${Math.round(s.height)} px tall`);
        if (s.width < Math.min(44, row.card.inner / 5) - 0.6) wrong.push(`star ${s.n} is ${Math.round(s.width)} px wide although the card has room for ${Math.round(Math.min(44, row.card.inner / 5))}`);
        if (s.drawing < 16) wrong.push(`the drawing of star ${s.n} is only ${Math.round(s.drawing)} px`);
      }
      expect.soft(wrong, 'the five stars do not fit, or are too small').toEqual([]);

      // Play, score: still possible.
      expect(await pressPlay(page, testInfo)).not.toBe('');
      await waitForTime(page, 0.5);
      expect((await videoState(page)).error).toBe('');
      await press(page, testInfo, 'label[for="s1"]'); // the star that used to be out of reach
      await expect(page.locator('#s1')).toBeChecked();
      await press(page, testInfo, 'label[for="s4"]');
      await press(page, testInfo, '#score button[type=submit]');
      await expect(page.locator('#score .ok')).toContainText('4 of 5 stars');
      expect((await server.state(clip.token)).score.stars).toBe(4);
    });
  });
}
