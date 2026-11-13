// Keys only: a desktop keyboard (Tab, Shift+Tab, Enter, Space, arrows) and a TV remote
// (arrows and OK). Runs on the desktop engines and on the TV projects.
//
// A TV remote is emulated with Chromium's own "spatial navigation": an arrow moves the focus
// to the nearest control in that direction, OK presses it. That is what a TV browser does in
// its focus mode. (Samsung's, LG's and Fire TV's browsers start in pointer mode instead: the
// remote moves an arrow on screen. tv.spec.js covers that.)

const {
  test, expect, projectFacts, checkGap, knownToFail,
  videoState, waitForMetadata, pressPlay, isPlaying, isPaused, waitForTime,
  focusByArrows, focusedIs, pixelsAround, pixelsChanged,
} = require('../lib/harness');

const isRemote = (testInfo) => projectFacts(testInfo).input === 'dpad';

/** Tab until `selector` has the focus. Returns how many presses it took. */
async function tabTo(page, selector, { max = 30, key = 'Tab' } = {}) {
  for (let presses = 1; presses <= max; presses += 1) {
    await page.keyboard.press(key);
    if (await focusedIs(page, selector)) return presses;
  }
  throw new Error(`${key} never reached ${selector} in ${max} presses`);
}

const reach = (page, testInfo, selector) => (isRemote(testInfo) ? focusByArrows(page, selector) : tabTo(page, selector));

/** What has the focus, in the customer's words. */
function focusName(page) {
  return page.evaluate(() => {
    const a = document.activeElement;
    if (!a || a === document.body) return '';
    if (a.id === 'v') return 'player';
    if (a.type === 'radio') return 'stars';
    if (a.tagName === 'LABEL') return 'stars';
    if (a.tagName === 'TEXTAREA') return 'comment';
    if (a.tagName === 'SUMMARY') return 'not my home';
    return (a.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 30);
  });
}

test('the focus can reach the player, the stars, the comment box and Send, in that order', async ({ page, clip }, testInfo) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);
  const key = isRemote(testInfo) ? 'ArrowDown' : 'Tab';
  const stops = [];
  for (let i = 0; i < 40; i += 1) {
    await page.keyboard.press(key);
    await page.waitForTimeout(40);
    const name = await focusName(page);
    if (name && stops[stops.length - 1] !== name) stops.push(name);
    if (name === 'Support') break;
  }
  testInfo.annotations.push({ type: 'focus order', description: stops.join(' > ') });
  // Safari's player is not always a Tab stop of its own (that is Safari's choice, not the page's);
  // the page's own controls must be, in order, everywhere.
  const engine = projectFacts(testInfo).engine;
  const wanted = engine === 'webkit' ? ['stars', 'comment', 'Send'] : ['player', 'stars', 'comment', 'Send'];
  expect(stops.filter((s) => wanted.includes(s)), `the focus went: ${stops.join(' > ')}`).toEqual(wanted);
  // Safari on a Mac only tabs to links when the user has turned that on, so links are not required there.
  if (projectFacts(testInfo).engine !== 'webkit') {
    for (const link of ['Call our office', 'not my home']) expect(stops, `the focus never reached "${link}"`).toContain(link);
    expect(stops.indexOf('Send')).toBeLessThan(stops.indexOf('Call our office'));
  }

  if (!isRemote(testInfo)) {
    await tabTo(page, '#score button[type=submit]', { key: 'Shift+Tab' });
    await page.keyboard.press('Shift+Tab');
    expect(await focusName(page), 'Shift+Tab from Send should go back to the comment box').toBe('comment');
  }
});

test('whatever has the focus is visibly outlined', async ({ page, clip }, testInfo) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);
  const stops = [
    ['the player', '#v', '#v'],
    ['a star', 'label[for="s5"]', 'label[for="s5"]'],
    ['the comment box', 'textarea', 'textarea'],
    ['Send', '#score button[type=submit]', '#score button[type=submit]'],
    ['Call our office', 'a.btn.ghost', 'a.btn.ghost'],
    ['This is not my home', 'details summary', 'details summary'],
  ];
  const faint = [];
  const engine = projectFacts(testInfo).engine;
  for (const [name, focusOn, lookAt] of stops) {
    if (engine === 'webkit' && ['Call our office', 'This is not my home'].includes(name) && !isRemote(testInfo)) continue;
    // The ring round the player is the browser's own; only Chromium's has been seen. The rest are the page's.
    if (engine !== 'chromium' && name === 'the player') continue;
    await reach(page, testInfo, focusOn);
    const withFocus = await pixelsAround(page, lookAt);
    await page.evaluate(() => document.activeElement.blur());
    const without = PNGof(await page.screenshot({ clip: withFocus.clip, animations: 'disabled', caret: 'hide' }));
    const changed = pixelsChanged(withFocus.png, without);
    // A ring of the outline's thickness all the way round the control, in CSS px.
    const around = 2 * (withFocus.box.width + withFocus.box.height) * withFocus.scale * withFocus.scale;
    const thickness = changed / around;
    testInfo.annotations.push({ type: 'focus ring', description: `${name}: about ${thickness.toFixed(1)} px thick (${changed} pixels changed)` });
    if (thickness < 0.8) faint.push(`${name}: only ${changed} pixels change when it has the focus (about ${thickness.toFixed(1)} px all round)`);
    // Put the focus back where it was for the next stop.
    await page.locator(focusOn).first().evaluate((el) => (el.tagName === 'LABEL' ? document.getElementById(el.htmlFor) : el).focus());
  }
  expect(faint, 'a control shows no clear outline when it has the focus').toEqual([]);
});

function PNGof(buffer) {
  return require('pngjs').PNG.sync.read(buffer);
}

test('the player works from the keys: play, pause, seek', async ({ page, clip, server }, testInfo) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);

  if (isRemote(testInfo)) {
    // The remote: arrows put the focus on the player, OK should start it.
    await knownToFail(testInfo, 'tv-remote-ok-does-not-play', async () => {
      await focusByArrows(page, '#v');
      await page.keyboard.press('Enter');
      expect(await isPlaying(page, 1500), 'OK on the focused player did not start the video').toBe(true);
    });
    return;
  }

  // Chrome, Edge and Firefox: Tab to the player, space bar plays and pauses, the right arrow moves on.
  // Safari's player has its own keys and is not always a Tab stop: there, whatever happens is reported.
  await knownToFail(testInfo, 'safari-player-keys', async () => {
    await tabTo(page, '#v');
    await page.keyboard.press('Space');
    expect(await isPlaying(page, 1500), 'the space bar on the focused player did not start the video').toBe(true);
    await waitForTime(page, 0.8);
    await page.keyboard.press('Space');
    expect(await isPaused(page, 1500), 'the space bar did not pause the video').toBe(true);

    const before = (await videoState(page)).time;
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(500);
    const after = (await videoState(page)).time;
    testInfo.annotations.push({ type: 'right arrow on the player', description: `moved ${(after - before).toFixed(1)} s` });
    expect(after, 'the right arrow did not move the video forward').toBeGreaterThan(before + 0.01);

    await page.keyboard.press('Space');
    expect(await isPlaying(page, 1500), 'the space bar did not start the video again').toBe(true);
    await expect.poll(async () => (await server.state(clip.token)).played).toBe(1);
  });
});

test('the stars work from the keys, and the arrows go the way they point', async ({ page, clip, server }, testInfo) => {
  await page.goto(clip.watch);

  if (isRemote(testInfo)) {
    await focusByArrows(page, 'label[for="s3"]');
    await page.keyboard.press('Enter');
    await expect(page.locator('#s3')).toBeChecked();
    await focusByArrows(page, 'label[for="s4"]'); // one press to the right
    await page.keyboard.press('Enter');
    await expect(page.locator('#s4')).toBeChecked();
  } else {
    await tabTo(page, 'label[for="s5"]'); // Tab lands on the group
    await page.keyboard.press('Space');
    const first = await page.evaluate(() => (document.querySelector('.stars input:checked') || {}).value);
    expect(first, 'Space on the stars should pick one').toBeTruthy();

    // From 3 stars, the right arrow should give 4 and the left arrow 2: the stars are drawn left to right.
    await page.locator('#s3').focus();
    await page.keyboard.press('Space');
    await expect(page.locator('#s3')).toBeChecked();
    await page.keyboard.press('ArrowRight');
    const afterRight = Number(await page.evaluate(() => document.querySelector('.stars input:checked').value));
    checkGap(testInfo, 'star-arrows-are-backwards', afterRight === 4 ? '' : `from 3 stars the right arrow gave ${afterRight}`, 'the right arrow did not move one star to the right');
    await page.locator('#s4').focus();
    await page.keyboard.press('Space');
  }

  await reach(page, testInfo, 'textarea');
  await page.keyboard.type('Thanks Pat');
  await reach(page, testInfo, '#score button[type=submit]');
  await page.keyboard.press('Enter');
  await expect(page.locator('#score .ok')).toHaveText('Thank you! You gave us 4 of 5 stars.');
  expect((await server.state(clip.token)).score).toEqual({ stars: 4, comment: 'Thanks Pat' });
});

test('"This is not my home" opens and confirms from the keys', async ({ page, clip, server }, testInfo) => {
  test.skip(projectFacts(testInfo).engine === 'webkit' && !isRemote(testInfo), 'Safari on a Mac does not tab to a <summary> unless the user has turned on "Press Tab to highlight each item"');
  await page.goto(clip.watch);
  await reach(page, testInfo, 'details summary');
  await page.keyboard.press('Enter');
  await expect(page.locator('details button[type=submit]')).toBeVisible();
  await reach(page, testInfo, 'details button[type=submit]');
  await page.keyboard.press('Enter');
  await expect(page.locator('h1')).toHaveText('Thank you');
  expect((await server.state(clip.token)).state).toBe('pulled');
});
