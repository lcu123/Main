// Phones and computers set to dark, Windows "contrast themes" (forced colours), and the
// "darken every website" switch in Samsung Internet and Chrome on Android. The page declares
// one light design; these tests look at what the customer actually sees, in pixels, because
// forced colours and darkening filters do not show up in computed styles.

const {
  test, expect, checkGap, projectFacts, press,
  pixelsAround, twoColours, contrast, colourDistance,
} = require('../lib/harness');

/** Pick 3 stars, then report the ink colour of star 2 (selected) and star 5 (not), and the heading's contrast. */
async function look(page, testInfo, clip) {
  await page.goto(clip.watch);
  await page.evaluate(() => document.fonts.ready);
  await press(page, testInfo, 'label[for="s3"]');
  await expect(page.locator('#s3')).toBeChecked();
  // Move the pointer and the focus away so neither hover nor the focus ring colours a star.
  await page.mouse.move(2, 2);
  await page.evaluate(() => document.activeElement && document.activeElement.blur());
  const selected = twoColours((await pixelsAround(page, 'label[for="s2"]', -6)).png);
  const unselected = twoColours((await pixelsAround(page, 'label[for="s5"]', -6)).png);
  const heading = twoColours((await pixelsAround(page, '#score h2', 0)).png);
  const send = twoColours((await pixelsAround(page, '#score button[type=submit]', -4)).png);
  return {
    starsDiffer: colourDistance(selected[1], unselected[1]),
    selected: selected[1], unselected: unselected[1], card: unselected[0],
    headingContrast: contrast(heading[0], heading[1]),
    sendContrast: contrast(send[0], send[1]),
  };
}

function report(testInfo, name, seen) {
  testInfo.annotations.push({
    type: 'colours',
    description: `${name}: selected star rgb(${seen.selected}), unselected rgb(${seen.unselected}), `
      + `heading contrast ${seen.headingContrast.toFixed(1)}:1, Send button contrast ${seen.sendContrast.toFixed(1)}:1`,
  });
}

test.describe('the device is set to dark', () => {
  test.use({ colorScheme: 'dark' });
  test('the page stays readable and a picked star still looks picked', async ({ page, clip }, testInfo) => {
    const seen = await look(page, testInfo, clip);
    report(testInfo, 'dark', seen);
    expect(seen.starsDiffer, 'a selected star and an unselected star look the same').toBeGreaterThan(90);
    expect(seen.headingContrast, 'the heading is hard to read').toBeGreaterThanOrEqual(4.5);
    expect(seen.sendContrast, 'the Send button is hard to read').toBeGreaterThanOrEqual(4.5);
  });
});

test.describe('forced colours (Windows contrast themes)', () => {
  test.use({ forcedColors: 'active' });
  test('the page stays readable and a picked star still looks picked', async ({ page, clip }, testInfo) => {
    await page.goto(clip.watch);
    const active = await page.evaluate(() => matchMedia('(forced-colors: active)').matches);
    test.skip(!active, 'this engine has no forced-colours mode to test (Safari has none)');
    const seen = await look(page, testInfo, clip);
    report(testInfo, 'forced colours', seen);
    expect.soft(seen.headingContrast, 'the heading is hard to read').toBeGreaterThanOrEqual(4.5);
    expect.soft(seen.sendContrast, 'the Send button is hard to read').toBeGreaterThanOrEqual(4.5);
    checkGap(testInfo, 'forced-colours-stars-look-the-same',
      seen.starsDiffer > 90 ? '' : `selected star rgb(${seen.selected}) and unselected star rgb(${seen.unselected}) look the same`,
      'a selected star and an unselected star look the same');
  });
});

test.describe('the browser darkens every website (Samsung Internet dark mode, Chrome "dark theme for sites")', () => {
  test('the page stays readable and a picked star still looks picked', async ({ page, clip }, testInfo) => {
    test.skip(projectFacts(testInfo).engine !== 'chromium', 'only Chromium browsers have this switch');
    const session = await page.context().newCDPSession(page);
    await session.send('Emulation.setAutoDarkModeOverride', { enabled: true });
    const seen = await look(page, testInfo, clip);
    report(testInfo, 'auto-darkened', seen);
    expect(seen.starsDiffer, 'a selected star and an unselected star look the same').toBeGreaterThan(90);
    expect(seen.headingContrast, 'the heading is hard to read').toBeGreaterThanOrEqual(4.5);
    expect(seen.sendContrast, 'the Send button is hard to read').toBeGreaterThanOrEqual(4.5);
  });
});

test('light, as designed: the control for the three checks above', async ({ page, clip }, testInfo) => {
  const seen = await look(page, testInfo, clip);
  report(testInfo, 'light', seen);
  expect(seen.starsDiffer).toBeGreaterThan(90);
  expect(seen.headingContrast).toBeGreaterThanOrEqual(4.5);
  expect(seen.sendContrast).toBeGreaterThanOrEqual(4.5);

  // Colour must not be the only difference (cataracts, colour blindness, a phone in bright sun):
  // a picked star should also be clearly darker or lighter than an unpicked one, and an empty
  // star should stand out from the card it sits on (3:1, the WCAG figure for controls).
  const pickedVsNot = contrast(seen.selected, seen.unselected);
  const emptyVsCard = contrast(seen.unselected, seen.card);
  const faint = [];
  if (pickedVsNot < 3) faint.push(`picked and unpicked stars differ ${pickedVsNot.toFixed(2)}:1 in brightness (hue only)`);
  if (emptyVsCard < 3) faint.push(`an empty star is ${emptyVsCard.toFixed(2)}:1 against the card`);
  checkGap(testInfo, 'stars-are-faint-and-differ-by-hue-only', faint, 'the stars are hard to see');
});
