// Phones and computers set to dark, Windows "contrast themes" (forced colours), and the
// "darken every website" switch in Samsung Internet and Chrome on Android. The page declares
// one light design; these tests look at what the customer actually sees, in pixels, because
// forced colours and darkening filters do not show up in computed styles.
//
// The stars are drawings: hollow (the card shows through a violet outline) when not picked,
// filled when picked. So "picked" does not rest on a hue: it is a shape with real contrast,
// for a colour-blind customer, in greyscale, and on a phone in the sun. The old stars were a
// pale grey glyph and a gold glyph of the same brightness (1.03:1), and in forced colours
// both were drawn in the one text colour.

const {
  test, expect, projectFacts, press,
  pixelsAround, twoColours, contrast, colourAt, lineColour,
} = require('../lib/harness');

const rgb = (c) => (c ? `rgb(${c.join(',')})` : 'none');

/** Pick 3 stars, then measure: the fill of a picked star (2) and of one that is not (5), its outline, the card, the heading and Send. */
async function look(page, testInfo, clip) {
  await page.goto(clip.watch);
  await page.evaluate(() => document.fonts.ready);
  await press(page, testInfo, 'label[for="s3"]');
  await expect(page.locator('#s3')).toBeChecked();
  // Move the pointer and the focus away so neither hover nor the focus ring colours a star.
  await page.mouse.move(2, 2);
  await page.evaluate(() => document.activeElement && document.activeElement.blur());
  await page.locator('.stars').scrollIntoViewIfNeeded();
  const spots = await page.evaluate(() => {
    // The middle of the star's body (a little below the centre of its box).
    const body = (n) => { const r = document.querySelector(`label[for="s${n}"] svg`).getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height * 0.55 }; };
    const card = document.getElementById('score').getBoundingClientRect();
    const row = document.querySelector('.stars').getBoundingClientRect();
    return { picked: body(2), empty: body(5), card: { x: card.left + 8, y: row.top + row.height / 2 } };
  });
  const pickedFill = await colourAt(page, spots.picked.x, spots.picked.y);
  const emptyFill = await colourAt(page, spots.empty.x, spots.empty.y);
  const card = await colourAt(page, spots.card.x, spots.card.y);
  const outline = lineColour((await pixelsAround(page, 'label[for="s5"]', -2)).png, [emptyFill, card]);
  const heading = twoColours((await pixelsAround(page, '#score h2', 0)).png);
  const send = twoColours((await pixelsAround(page, '#score button[type=submit]', -4)).png);
  return {
    pickedFill, emptyFill, card, outline,
    pickedVsEmpty: contrast(pickedFill, emptyFill),
    emptyVsCard: contrast(emptyFill, card),
    outlineVsCard: outline ? contrast(outline, card) : 1,
    headingContrast: contrast(heading[0], heading[1]),
    sendContrast: contrast(send[0], send[1]),
  };
}

/** What is wrong with the stars as seen, if anything: 3:1 is the WCAG figure for the parts of a control. */
function starFaults(seen) {
  const faults = [];
  if (!seen.outline) faults.push(`a star that is not picked has no outline: it is ${rgb(seen.emptyFill)} on a ${rgb(seen.card)} card`);
  else if (seen.outlineVsCard < 3) faults.push(`the outline of a star that is not picked is ${seen.outlineVsCard.toFixed(2)}:1 against the card (${rgb(seen.outline)} on ${rgb(seen.card)})`);
  // Hollow means the card shows through (or a fill as good as the card's own colour). A star
  // that is filled with something else looks picked, whatever its colour.
  if (seen.emptyVsCard > 1.5) faults.push(`a star that is not picked is not hollow: it is filled ${rgb(seen.emptyFill)} on a ${rgb(seen.card)} card (${seen.emptyVsCard.toFixed(1)}:1), so every star looks filled`);
  if (seen.pickedVsEmpty < 3) faults.push(`a picked star (${rgb(seen.pickedFill)}) and one that is not (${rgb(seen.emptyFill)}) differ only ${seen.pickedVsEmpty.toFixed(2)}:1`);
  return faults;
}

function report(testInfo, name, seen) {
  testInfo.annotations.push({
    type: 'colours',
    description: `${name}: picked star ${rgb(seen.pickedFill)}, not picked ${rgb(seen.emptyFill)} with outline ${rgb(seen.outline)} on a ${rgb(seen.card)} card `
      + `(picked against not picked ${seen.pickedVsEmpty.toFixed(1)}:1, outline against card ${seen.outlineVsCard.toFixed(1)}:1); `
      + `heading ${seen.headingContrast.toFixed(1)}:1, Send ${seen.sendContrast.toFixed(1)}:1`,
  });
}

/** Send the score and measure the thank-you block: its words against its own background. */
async function thankYou(page, testInfo, name) {
  await press(page, testInfo, '#score button[type=submit]');
  const block = page.locator('#score .ok');
  await expect(block).toContainText('You gave us 3 of 5 stars.');
  await page.mouse.move(2, 2);
  await block.scrollIntoViewIfNeeded();
  const [ground, ink] = twoColours((await pixelsAround(page, '#score .ok', -8)).png);
  const ratio = contrast(ground, ink);
  testInfo.annotations.push({ type: 'colours', description: `${name}: the thank-you reads ${rgb(ink)} on ${rgb(ground)}, ${ratio.toFixed(1)}:1` });
  return ratio;
}

test('light, as designed: the control for the checks below', async ({ page, clip }, testInfo) => {
  const seen = await look(page, testInfo, clip);
  report(testInfo, 'light', seen);
  expect(starFaults(seen), 'the stars are hard to see or to tell apart').toEqual([]);
  expect(seen.headingContrast).toBeGreaterThanOrEqual(4.5);
  expect(seen.sendContrast).toBeGreaterThanOrEqual(4.5);
  // As designed: a violet line round a star that is not picked, the same violet all through one that is.
  expect(contrast(seen.pickedFill, seen.card), 'a picked star against the card').toBeGreaterThanOrEqual(3);
  expect(contrast(seen.pickedFill, seen.outline), 'the fill of a picked star should be the colour of the outline').toBeLessThan(1.2);
  expect(await thankYou(page, testInfo, 'light'), 'the thank-you is hard to read').toBeGreaterThanOrEqual(4.5);
});

test.describe('the device is set to dark', () => {
  test.use({ colorScheme: 'dark' });
  test('the page stays readable, a picked star still looks picked, the thank-you can be read', async ({ page, clip }, testInfo) => {
    const seen = await look(page, testInfo, clip);
    report(testInfo, 'dark', seen);
    expect(starFaults(seen), 'the stars are hard to see or to tell apart').toEqual([]);
    expect(seen.headingContrast, 'the heading is hard to read').toBeGreaterThanOrEqual(4.5);
    expect(seen.sendContrast, 'the Send button is hard to read').toBeGreaterThanOrEqual(4.5);
    expect(await thankYou(page, testInfo, 'dark'), 'the thank-you is hard to read').toBeGreaterThanOrEqual(4.5);
  });
});

test.describe('forced colours (Windows contrast themes)', () => {
  test.use({ forcedColors: 'active' });
  test('the page stays readable, a picked star still looks picked, the thank-you can be read', async ({ page, clip }, testInfo) => {
    await page.goto(clip.watch);
    const active = await page.evaluate(() => matchMedia('(forced-colors: active)').matches);
    test.skip(!active, 'this engine has no forced-colours mode to test (Safari has none)');
    const seen = await look(page, testInfo, clip);
    report(testInfo, 'forced colours', seen);
    expect.soft(seen.headingContrast, 'the heading is hard to read').toBeGreaterThanOrEqual(4.5);
    expect.soft(seen.sendContrast, 'the Send button is hard to read').toBeGreaterThanOrEqual(4.5);
    // The theme takes every colour away; the page hands the stars the theme's own: an outline
    // in the text colour, filled with the background when not picked and with the text colour
    // when picked. (Filled with the highlight colour it measured 2.94:1 against "not picked"
    // in Firefox: too close. Text against background is the strongest pair a theme has.)
    expect.soft(starFaults(seen).join('; '), 'in forced colours the stars are hard to see or to tell apart').toBe('');
    // A button with no border is a patch of text in forced colours: the page gives .btn one.
    const border = await page.locator('#score button[type=submit]').evaluate((el) => parseFloat(getComputedStyle(el).borderTopWidth));
    expect.soft(border, 'in forced colours a button needs a border to look like a button').toBeGreaterThanOrEqual(2);
    expect.soft(await thankYou(page, testInfo, 'forced colours'), 'the thank-you is hard to read').toBeGreaterThanOrEqual(4.5);
  });
});

test.describe('the browser darkens every website (Samsung Internet dark mode, Chrome "dark theme for sites")', () => {
  test('the page stays readable, a picked star still looks picked, the thank-you can be read', async ({ page, clip }, testInfo) => {
    test.skip(projectFacts(testInfo).engine !== 'chromium', 'only Chromium browsers have this switch');
    const session = await page.context().newCDPSession(page);
    await session.send('Emulation.setAutoDarkModeOverride', { enabled: true });
    const seen = await look(page, testInfo, clip);
    report(testInfo, 'auto-darkened', seen);
    // An unpicked star has no fill at all (color:transparent), so the darkening has nothing to turn white.
    expect.soft(starFaults(seen).join('; '), 'once the browser has darkened the page the stars are hard to tell apart').toBe('');
    expect.soft(seen.outlineVsCard, 'the stars themselves must stand out from the darkened card').toBeGreaterThanOrEqual(3);
    expect.soft(seen.headingContrast, 'the heading is hard to read').toBeGreaterThanOrEqual(4.5);
    expect.soft(seen.sendContrast, 'the Send button is hard to read').toBeGreaterThanOrEqual(4.5);
    // The darkening turns black words white. On the old lime block that was white on lime,
    // 1.5:1: the customer could not read their own thank-you. The block is white with a lime
    // frame now, which the browser darkens together with its words.
    expect(await thankYou(page, testInfo, 'auto-darkened'), 'the thank-you cannot be read once the browser has darkened the page').toBeGreaterThanOrEqual(4.5);
  });
});
