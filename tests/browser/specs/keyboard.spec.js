// Keys only: a desktop keyboard (Tab, Shift+Tab, Enter, Space, arrows) and a TV remote
// (arrows, OK and the play/pause keys). Runs on the desktop engines and on the TV projects.
//
// A TV remote is emulated with Chromium's own "spatial navigation": an arrow moves the focus
// to the nearest control in that direction, OK presses it. That is what a TV browser does in
// its focus mode. (Samsung's, LG's and Fire TV's browsers start in pointer mode instead: the
// remote moves an arrow on screen. tv.spec.js covers that.)
//
// Safari on a Mac: a plain Tab stops only on text boxes unless the user has changed a
// setting; Option+Tab is the key that reaches every control. The tests press what a Safari
// keyboard user presses (tabKey in lib/harness.js) and expect the same results as elsewhere.

const {
  test, expect, projectFacts, knownToFail,
  videoState, waitForMetadata, isPlaying, isPaused, waitForTime,
  focusByArrows, focusedIs, tabKey, tabTo, pixelsAround, pixelsChanged,
} = require('../lib/harness');
const { PNG } = require('pngjs');

const isRemote = (testInfo) => projectFacts(testInfo).input === 'dpad';
const safariOnLinux = (testInfo) => projectFacts(testInfo).engine === 'webkit' && process.platform !== 'darwin';

const reach = (page, testInfo, selector) => (isRemote(testInfo) ? focusByArrows(page, selector) : tabTo(page, testInfo, selector));

/** What has the focus, in the customer's words. */
function focusName(page) {
  return page.evaluate(() => {
    const a = document.activeElement;
    if (!a || a === document.body) return '';
    if (a.id === 'v') return 'player';
    if (a.id === 'verr') return 'note';
    if (a.type === 'radio') return 'stars';
    if (a.tagName === 'LABEL') return 'stars';
    if (a.tagName === 'TEXTAREA') return 'comment';
    if (a.tagName === 'SUMMARY') return 'not my home';
    if (a.matches('a.btn')) return 'Call our office';
    return (a.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 30);
  });
}

const stars = (page) => page.evaluate(() => Number((document.querySelector('.stars input:checked') || { value: 0 }).value));

/** Tab (or the remote's arrows) onto the row of stars, whichever star the browser lands on. Returns that star's selector. */
async function reachStars(page, testInfo) {
  if (isRemote(testInfo)) { await focusByArrows(page, 'label[for="s5"]'); return 'label[for="s5"]'; }
  for (let presses = 0; presses < 30; presses += 1) {
    await page.keyboard.press(tabKey(testInfo));
    const id = await page.evaluate(() => (document.activeElement && document.activeElement.type === 'radio' ? document.activeElement.id : ''));
    if (id) return `label[for="${id}"]`;
  }
  throw new Error(`${tabKey(testInfo)} never reached the stars in 30 presses`);
}

/** How thick the outline round `selector` is when it has the focus, from two pictures: with and without. */
async function ringThickness(page, selector) {
  const withFocus = await pixelsAround(page, selector);
  await page.evaluate(() => document.activeElement.blur());
  const without = PNG.sync.read(await page.screenshot({ clip: withFocus.clip, animations: 'disabled', caret: 'hide' }));
  const changed = pixelsChanged(withFocus.png, without);
  // A ring of the outline's thickness all the way round the control, in CSS px.
  const around = 2 * (withFocus.box.width + withFocus.box.height) * withFocus.scale * withFocus.scale;
  return { changed, thickness: changed / around };
}

test('the focus can reach the player, the stars, the comment box and Send, in that order', async ({ page, clip }, testInfo) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);
  const key = isRemote(testInfo) ? 'ArrowDown' : tabKey(testInfo);
  const stops = [];
  for (let i = 0; i < 40; i += 1) {
    await page.keyboard.press(key);
    await page.waitForTimeout(40);
    const name = await focusName(page);
    if (name && stops[stops.length - 1] !== name) stops.push(name);
    if (name === 'Support') break;
  }
  testInfo.annotations.push({ type: 'focus order', description: `${key}: ${stops.join(' > ')}` });
  // Safari's player is not always a stop of its own (that is Safari's choice, not the page's);
  // the page's own controls must be, in order, everywhere.
  const engine = projectFacts(testInfo).engine;
  const wanted = engine === 'webkit' ? ['stars', 'comment', 'Send'] : ['player', 'stars', 'comment', 'Send'];
  expect(stops.filter((s) => wanted.includes(s)), `the focus went: ${stops.join(' > ')}`).toEqual(wanted);
  // The links and "not my home". (WebKit as built for Linux has its own idea of which keys stop on links; Safari is the Mac run.)
  if (!safariOnLinux(testInfo)) {
    for (const link of ['Call our office', 'not my home']) expect(stops, `the focus never reached "${link}": it went ${stops.join(' > ')}`).toContain(link);
    expect(stops.indexOf('Send')).toBeLessThan(stops.indexOf('Call our office'));
  }

  if (!isRemote(testInfo)) {
    await tabTo(page, testInfo, '#score button[type=submit]', { back: true });
    await page.keyboard.press(tabKey(testInfo, { back: true }));
    expect(await focusName(page), 'Shift+Tab from Send should go back to the comment box').toBe('comment');
  }
});

test('whatever has the focus is visibly outlined', async ({ page, clip }, testInfo) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);
  const stops = [
    ['the player', '#v'],
    ['a star', 'label[for="s5"]'],
    ['the comment box', 'textarea'],
    ['Send', '#score button[type=submit]'],
    ['Call our office', 'a.btn.ghost'],
    ['This is not my home', 'details summary'],
  ];
  const faint = [];
  const engine = projectFacts(testInfo).engine;
  for (const [name, listed] of stops) {
    if (safariOnLinux(testInfo) && ['Call our office', 'This is not my home'].includes(name)) continue;
    let selector = listed;
    try {
      if (name === 'a star') selector = await reachStars(page, testInfo);
      else await (isRemote(testInfo) ? focusByArrows(page, selector) : tabTo(page, testInfo, selector, { max: name === 'the player' ? 12 : 30 }));
    } catch (error) {
      // Safari's player may not be a stop for the keys at all: that is 'safari-player-keys', not a missing outline.
      if (engine === 'webkit' && name === 'the player') { testInfo.annotations.push({ type: 'focus ring', description: 'the player: the keys did not reach it in this browser' }); continue; }
      throw error;
    }
    const { changed, thickness } = await ringThickness(page, selector);
    testInfo.annotations.push({ type: 'focus ring', description: `${name}: about ${thickness.toFixed(1)} px thick (${changed} pixels changed)` });
    if (thickness < 0.8) faint.push(`${name}: only ${changed} pixels change when it has the focus (about ${thickness.toFixed(1)} px all round)`);
    // The player's ring is the page's own (5 px, yellow, inside the black box) so that it shows
    // on a TV from the sofa; the browser's own thin ring could not be seen there.
    if (name === 'the player' && engine === 'chromium') expect.soft(thickness, 'the ring round the focused player should be the page\'s thick one').toBeGreaterThan(3);
    // Put the focus back where it was for the next stop.
    await page.locator(selector).first().evaluate((el) => (el.tagName === 'LABEL' ? document.getElementById(el.htmlFor) : el).focus());
  }
  expect(faint, 'a control shows no clear outline when it has the focus').toEqual([]);
});

test('the player works from the keys: play, pause, seek', async ({ page, clip, server }, testInfo) => {
  await page.goto(clip.watch);
  await waitForMetadata(page);

  if (isRemote(testInfo)) {
    // The remote: arrows put the focus on the player, OK starts it, OK pauses it. A TV browser
    // in focus mode ignores OK on a <video>; watch.js is what makes it work. Without it a
    // customer on the sofa could look at the first frame and nothing else.
    await focusByArrows(page, '#v');
    await page.keyboard.press('Enter');
    expect(await isPlaying(page, 2500), 'OK on the focused player did not start the video').toBe(true);
    await waitForTime(page, 0.6);
    await page.keyboard.press('Enter');
    expect(await isPaused(page, 2500), 'OK on the playing video did not pause it').toBe(true);
    const stoppedAt = (await videoState(page)).time;
    await page.waitForTimeout(400);
    expect((await videoState(page)).time, 'the picture kept moving after OK').toBeCloseTo(stoppedAt, 1);
    await page.keyboard.press('Enter');
    expect(await isPlaying(page, 2500), 'OK did not start the video again').toBe(true);
    expect(await focusedIs(page, '#v'), 'the focus should stay on the player').toBe(true);
    await expect.poll(async () => (await server.state(clip.token)).played).toBe(1);
    expect((await server.state(clip.token)).hits.played, 'three presses of OK are one viewing').toBe(1);
    return;
  }

  // Chrome, Edge and Firefox: Tab to the player, space bar plays and pauses, the right arrow moves on.
  // Safari's player has its own keys: there this is a known gap.
  await knownToFail(testInfo, 'safari-player-keys', async () => {
    await tabTo(page, testInfo, '#v');
    await page.keyboard.press('Space');
    expect(await isPlaying(page, 1500), 'the space bar on the focused player did not start the video').toBe(true);
    await waitForTime(page, 0.8);
    await page.keyboard.press('Space');
    expect(await isPaused(page, 1500), 'the space bar did not pause the video').toBe(true);

    // Let the paused picture come to rest first: a player may still step back to the last
    // frame it drew for a moment after "paused" turns true.
    let before = (await videoState(page)).time;
    await expect.poll(async () => { const was = before; before = (await videoState(page)).time; return before === was; }, { message: 'the paused video never came to rest', intervals: [200] }).toBe(true);
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

test('Enter on the focused player starts and pauses it', async ({ page, clip }, testInfo) => {
  test.skip(isRemote(testInfo), 'on the TV rows OK is Enter: the test above');
  // The same key a TV remote sends, from a keyboard. Desktop Chrome starts a focused video on
  // Enter by itself, and watch.js does too (for TVs, which do not): the script takes the key
  // for itself, so the video toggles ONCE. If both acted, the two would cancel out.
  await page.goto(clip.watch);
  await waitForMetadata(page);
  await knownToFail(testInfo, 'enter-on-the-player', async () => {
    await tabTo(page, testInfo, '#v', { max: 12 });
    await page.keyboard.press('Enter');
    expect(await isPlaying(page, 2000), 'Enter on the focused player did not start the video').toBe(true);
    await waitForTime(page, 0.5);
    await page.keyboard.press('Enter');
    expect(await isPaused(page, 2000), 'Enter on the playing video did not pause it').toBe(true);
  });
});

test('the play and pause keys of a remote work wherever the focus is', async ({ page, clip, server }, testInfo) => {
  test.skip(projectFacts(testInfo).engine !== 'chromium', 'these are the key codes of TV remotes, and TV browsers are Chromium; what Firefox and Safari do with a keyboard\'s media keys is theirs');
  await page.goto(clip.watch);
  await waitForMetadata(page);
  // The focus is somewhere else on the page: a star.
  if (isRemote(testInfo)) await focusByArrows(page, 'label[for="s3"]');
  else await page.locator('#s3').focus();

  // Through the browser's own input, with the key codes real remotes send:
  // 415 play, 19 pause, 413 stop, 179 and 10252 (Samsung) play/pause.
  const session = await page.context().newCDPSession(page);
  const remoteKey = async (code, name) => {
    await session.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', windowsVirtualKeyCode: code, nativeVirtualKeyCode: code, key: name });
    await session.send('Input.dispatchKeyEvent', { type: 'keyUp', windowsVirtualKeyCode: code, nativeVirtualKeyCode: code, key: name });
  };
  await remoteKey(415, 'MediaPlay');
  expect(await isPlaying(page, 2500), 'the remote\'s PLAY key (415) did not start the video').toBe(true);
  await waitForTime(page, 0.4);
  await remoteKey(415, 'MediaPlay');
  expect(await isPlaying(page, 600), 'PLAY on a playing video must leave it playing').toBe(true);
  await remoteKey(19, 'Pause');
  expect(await isPaused(page, 2500), 'the remote\'s PAUSE key (19) did not pause the video').toBe(true);
  await remoteKey(19, 'Pause');
  await page.waitForTimeout(300);
  expect((await videoState(page)).paused, 'PAUSE on a paused video must leave it paused').toBe(true);
  await remoteKey(179, 'MediaPlayPause');
  expect(await isPlaying(page, 2500), 'the PLAY/PAUSE key (179) did not start the video').toBe(true);
  await remoteKey(179, 'MediaPlayPause');
  expect(await isPaused(page, 2500), 'the PLAY/PAUSE key (179) did not pause the video').toBe(true);
  await remoteKey(10252, 'MediaPlayPause');
  expect(await isPlaying(page, 2500), 'Samsung\'s PLAY/PAUSE key (10252) did not start the video').toBe(true);
  await remoteKey(413, 'MediaStop');
  expect(await isPaused(page, 2500), 'the STOP key (413) did not pause the video').toBe(true);
  // The keys did not disturb what the customer was doing, and it is one viewing.
  expect(await stars(page), 'a media key must not pick a star').toBe(0);
  await expect.poll(async () => (await server.state(clip.token)).played).toBe(1);
  expect((await server.state(clip.token)).hits.played).toBe(1);
});

test('the stars work from the keys, and the arrows go the way they point', async ({ page, clip, server }, testInfo) => {
  await page.goto(clip.watch);
  const sent = async () => (await server.state(clip.token)).hits.score;

  if (isRemote(testInfo)) {
    // Before anything is picked the arrows only move the focus along the row.
    await focusByArrows(page, 'label[for="s3"]');
    expect(await stars(page), 'moving the focus onto a star must not pick it').toBe(0);
    await page.keyboard.press('Enter');
    await expect(page.locator('#s3')).toBeChecked();
    expect(await sent(), 'OK on a star sent the form').toBe(0);
    // Once one is picked, Right means more and Left means fewer: the way the stars are drawn.
    await page.keyboard.press('ArrowRight');
    expect(await stars(page), 'from 3 stars the right arrow should give 4').toBe(4);
    await page.keyboard.press('ArrowLeft');
    await page.keyboard.press('ArrowLeft');
    expect(await stars(page), 'from 4 stars two presses of the left arrow should give 2').toBe(2);
    // The row must not hold the remote: Up goes back to the player, Down goes on to the
    // comment box. (On a small TV window the first presses scroll the page; a few are allowed.)
    const leave = async (key) => {
      for (let presses = 0; presses < 6 && (await focusName(page)) === 'stars'; presses += 1) {
        await page.keyboard.press(key);
        await page.waitForTimeout(80);
      }
      return focusName(page);
    };
    expect(await leave('ArrowUp'), 'the Up arrow could not leave the stars').toBe('player');
    expect(await stars(page), 'leaving the row upwards changed the score').toBe(2);
    await focusByArrows(page, 'label[for="s4"]');
    await page.keyboard.press('Enter');
    await expect(page.locator('#s4')).toBeChecked();
    expect(await leave('ArrowDown'), 'the Down arrow could not leave the stars').toBe('comment');
    expect(await stars(page), 'leaving the row downwards changed the score').toBe(4);
  } else {
    await reachStars(page, testInfo); // Tab lands on the group
    await page.keyboard.press('Space');
    expect(await stars(page), 'Space on the stars should pick one').toBeGreaterThan(0);

    // From 3 stars: Right is more, Left is fewer, and neither end wraps round to the other.
    // (The radio buttons are written 5 to 1 and drawn right to left, so that picking one fills
    // those before it with no script; left alone, the browser's arrows then run backwards.)
    await page.locator('#s3').focus();
    await page.keyboard.press('Space');
    await expect(page.locator('#s3')).toBeChecked();
    await page.keyboard.press('ArrowRight');
    expect(await stars(page), 'from 3 stars the right arrow should give 4').toBe(4);
    expect(await focusedIs(page, 'label[for="s4"]'), 'the focus should move with the pick').toBe(true);
    await page.keyboard.press('ArrowRight');
    expect(await stars(page)).toBe(5);
    await page.keyboard.press('ArrowRight');
    expect(await stars(page), 'the right arrow on 5 stars must stay on 5, not wrap round to 1').toBe(5);
    for (let n = 4; n >= 1; n -= 1) {
      await page.keyboard.press('ArrowLeft');
      expect(await stars(page), `the left arrow should give ${n}`).toBe(n);
    }
    await page.keyboard.press('ArrowLeft');
    expect(await stars(page), 'the left arrow on 1 star must stay on 1, not wrap round to 5').toBe(1);

    // Enter on a star picks it. It must not send the form: a customer who presses Enter to
    // choose would have sent a score before reaching the comment box.
    await page.locator('#s2').focus(); // as after Shift+Tab into the group: focused, not picked
    expect(await stars(page)).toBe(1);
    await page.keyboard.press('Enter');
    expect(await stars(page), 'Enter on a star should pick it').toBe(2);
    await page.waitForTimeout(300);
    expect(await sent(), 'Enter on a star sent the form').toBe(0);
    await expect(page.locator('#score form'), 'Enter on a star left the form').toHaveCount(1);
    await page.keyboard.press('ArrowRight');
    await page.keyboard.press('ArrowRight');
    expect(await stars(page)).toBe(4);
  }

  await reach(page, testInfo, 'textarea');
  await page.keyboard.type('Thanks Pat');
  await reach(page, testInfo, '#score button[type=submit]');
  await page.keyboard.press('Enter');
  await expect(page.locator('#score .ok')).toHaveText('Thank you! You gave us 4 of 5 stars.');
  expect((await server.state(clip.token)).score).toEqual({ stars: 4, comment: 'Thanks Pat' });
  expect(await sent(), 'the form is sent once, by Send').toBe(1);
});

test('"This is not my home" opens and confirms from the keys', async ({ page, clip, server }, testInfo) => {
  test.skip(safariOnLinux(testInfo), 'WebKit as built for Linux does not stop on a <summary> with Tab; Safari is the Mac run (Option+Tab)');
  await page.goto(clip.watch);
  await reach(page, testInfo, 'details summary');
  await page.keyboard.press('Enter');
  await expect(page.locator('details button[type=submit]')).toBeVisible();
  await reach(page, testInfo, 'details button[type=submit]');
  await page.keyboard.press('Enter');
  await expect(page.locator('h1')).toHaveText('Thank you');
  expect((await server.state(clip.token)).state).toBe('pulled');
});

test('the "try again" note can be reached and pressed with the keys', async ({ page, server, problems }, testInfo) => {
  // A video taken down as the page opened: the note is showing (recovery.spec.js). It used to
  // be a <div>: nothing a keyboard or a remote could land on. As a button, the keys reach it,
  // Enter (OK on a remote) tries once more, and a second press opens the page again.
  const clip = await server.newVideo();
  let asked = 0;
  let pulled = false;
  await page.route('**/w/*/video', async (route) => {
    asked += 1;
    if (!pulled) { pulled = true; await server.pull(clip.token); }
    await route.continue();
  });
  problems.allow(/status of 410/);
  await page.goto(clip.watch);
  await expect(page.locator('#verr')).toBeVisible();
  await page.waitForTimeout(1000);
  const before = asked;

  await reach(page, testInfo, '#verr');
  expect(await focusName(page)).toBe('note');
  const { changed, thickness } = await ringThickness(page, '#verr');
  testInfo.annotations.push({ type: 'focus ring', description: `the note: about ${thickness.toFixed(1)} px thick (${changed} pixels changed)` });
  expect(thickness, `the note shows no clear outline when it has the focus (${changed} pixels changed)`).toBeGreaterThan(0.8);
  await page.locator('#verr').focus();

  await page.keyboard.press('Enter');
  await expect.poll(() => asked, { message: 'Enter on the note did not try again' }).toBe(before + 1);
  await expect(page.locator('#verr'), 'the try failed again: the note must be back').toBeVisible();
  await expect(page.locator('#v'), 'one press must not leave the page').toHaveCount(1);
  // A second press within a second of the first is taken for a double tap (recovery.spec.js),
  // so this customer waits a moment, as people do.
  await page.waitForTimeout(1100);
  // The note is ready for the second press only if it still has the focus.
  // (The note stays where it is while the try runs, so the focus has nowhere else to go.)
  const where = await focusName(page);
  expect(where, `after Enter on the note the focus is on ${where ? `"${where}"` : 'nothing'}: the note lost the focus when it was pressed`).toBe('note');

  // The second press: the space bar on a keyboard, OK again on a remote.
  await page.keyboard.press(isRemote(testInfo) ? 'Enter' : 'Space');
  await expect(page.locator('h1')).toHaveText('This video is no longer available.');
  await expect(page.locator('video')).toHaveCount(0);
  expect(await problems.list()).toEqual([]);
});

test('Enter (OK on a remote) on a player that has failed tries again, as a press on the note does', async ({ page, clip, server, problems }, testInfo) => {
  // On a TV the focus is on the player when it dies, and OK is the key under the thumb. It
  // used to do nothing there: the customer had to find the note with the arrows first.
  const { engine } = projectFacts(testInfo);
  let asked = 0;
  await page.route('**/w/*/video', (route) => { asked += 1; return route.abort('connectionfailed'); });
  problems.allow(/Failed to load resource/);
  await page.goto(clip.watch);
  await expect(page.locator('#verr')).toHaveText('The video did not load. Press here to try again.');
  await page.waitForTimeout(600);
  const before = asked;
  await page.locator('#v').focus();
  await page.keyboard.press('Enter');
  await page.waitForTimeout(900);
  const line = `${engine}: Enter on the failed player asked for the video ${asked - before} more time(s)`;
  testInfo.annotations.push({ type: 'Enter on a failed player', description: line });
  if (engine === 'chromium') {
    expect(asked - before, 'Enter on the failed player did not try again').toBe(1);
  } else {
    console.log(`[${testInfo.project.name}] ${line}`); // into the CI log: only Chromium has been run
    expect(asked - before, 'one press must never start a round of tries').toBeLessThanOrEqual(2);
  }
  await expect(page.locator('#verr'), 'the try failed again: the note must be back').toHaveText('The video did not load. Press here to try again.');
  expect((await server.state(clip.token)).played, 'a key on a dead player is not a viewing').toBe(0);
  expect(await problems.list()).toEqual([]);
});

test('Alt+Left on a star is left to the browser (it is the Back key), and the score stays', async ({ page, clip }, testInfo) => {
  test.skip(isRemote(testInfo), 'a TV remote has no Alt key');
  const { engine } = projectFacts(testInfo);
  await page.goto(clip.watch);
  await page.locator('label[for="s3"]').click();
  await page.locator('#s3').focus();
  expect(await stars(page)).toBe(3);
  // The page turns Left and Right around on the stars (they are drawn right to left). With
  // Alt held it used to do that too: the score dropped to 2 and the browser did not go back.
  await page.keyboard.press('Alt+ArrowLeft');
  await page.waitForTimeout(150);
  await expect(page.locator('#v'), 'the page should still be here: there is nothing to go back to in this test').toHaveCount(1);
  const after = await stars(page);
  if (engine === 'chromium') {
    expect(after, 'Alt+Left changed the score').toBe(3);
    await page.keyboard.press('Control+ArrowRight');
    expect(await stars(page), 'Ctrl+Right changed the score').toBe(3);
    await page.keyboard.press('ArrowRight');
    expect(await stars(page), 'a plain Right should still give one more').toBe(4);
  } else {
    const line = `${engine}: after Alt+Left on the third star the score is ${after} (what this browser itself does with the key)`;
    testInfo.annotations.push({ type: 'Alt+Left on a star', description: line });
    console.log(`[${testInfo.project.name}] ${line}`);
  }
});

