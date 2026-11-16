// The pages around the watch page, on every project: a dead link, a link nobody ever sent,
// unsubscribe, privacy, terms and support.

const { test, expect, sidewaysScroll, tapTargets, textSizes, insideFirstScreen, press, shot } = require('../lib/harness');

const CALL = 'Call our office (916) 555-0100';

async function expectReadable(page, { infoPage = false } = {}) {
  expect.soft(await sidewaysScroll(page), 'the page scrolls sideways').toEqual([]);
  // On the long text pages, a phone or email link inside a sentence is as tall as the sentence;
  // only stand-alone controls must be 44 px there.
  const small = (await tapTargets(page)).filter((t) => !(infoPage && t.inline) && (t.width < 44 || t.height < 44));
  expect.soft(small.map((t) => `${t.name} is ${t.width}x${t.height} px`), 'a control is smaller than 44x44 px').toEqual([]);
  const tiny = (await textSizes(page)).filter((t) => t.size < 16).map((t) => `${t.where} "${t.text}" is ${t.size} px`);
  expect.soft(tiny, 'text is smaller than 16 px').toEqual([]);
}

for (const [name, which, status] of [['a video that was taken down', 'dead', 410], ['a link nobody ever sent', 'unknown', 404]]) {
  test(`${name}: a kind message and the office number, no player`, async ({ page, server, problems }, testInfo) => {
    const response = await page.goto(server.info[which]);
    expect(response.status()).toBe(status);
    expect(response.headers()['cache-control']).toBe('no-store');
    await expect(page.locator('h1')).toHaveText('This video is no longer available.');
    await expect(page.locator('main')).toContainText('If you think this is a mistake, please call our office.');
    await expect(page.locator('video')).toHaveCount(0);

    // The number is written on the button: on a desktop, a tablet or a TV a tel: link starts
    // no call, and "Call our office" alone left the customer with nothing to dial.
    const call = page.locator('main a.btn');
    await expect(call).toHaveText(CALL);
    await expect(call).toHaveAttribute('href', `tel:${server.info.officePhone}`);
    expect(await insideFirstScreen(page, 'main a.btn', 'the "Call our office" button')).toBe('');
    await expect(page.locator('footer a[href^="tel:"]')).toHaveText('(916) 555-0100');
    await expectReadable(page);
    if (which === 'dead') await shot(page, testInfo, 'dead-link');
    expect(await problems.list()).toEqual([]);
  });
}

test('unsubscribe: one clear question, one button, and it is done; opening the link again does not ask twice', async ({ page, clip, problems }, testInfo) => {
  await page.goto(clip.unsubscribe);
  await expect(page.locator('h1')).toHaveText('Stop video emails?');
  await expect(page.locator('main')).toContainText('Your service does not change.');
  expect(await insideFirstScreen(page, 'form button', 'the button')).toBe('');
  await expectReadable(page);

  await press(page, testInfo, 'form button[type=submit]');
  await expect(page.locator('h1')).toHaveText('Done');
  await expect(page.locator('main')).toContainText('You will not get video emails from us anymore.');

  // A week later the customer taps "Stop these video emails" in an old email again. Asking
  // "Stop video emails?" a second time reads as if the first answer had been lost.
  const again = await page.goto(clip.unsubscribe);
  expect(again.status()).toBe(200);
  await expect(page.locator('h1')).toHaveText('Done');
  await expect(page.locator('main')).toContainText('You will not get video emails from us anymore.');
  await expect(page.locator('form')).toHaveCount(0);
  await expectReadable(page);
  expect(await problems.list()).toEqual([]);
});

for (const [kind, heading] of [['privacy', 'Privacy Policy'], ['terms', /Terms/], ['support', /Support|Help/]]) {
  test(`${kind} page opens, reads without sideways scroll, and links back to the office`, async ({ page, server, problems }) => {
    const response = await page.goto(server.info[kind]);
    expect(response.status()).toBe(200);
    await expect(page.locator('h1')).toHaveText(heading);
    await expect(page.locator('footer a[href^="tel:"]')).toHaveText('(916) 555-0100');
    if (kind !== 'support') await expect(page.locator('main')).toContainText('Last updated');
    await expectReadable(page, { infoPage: true });
    expect(await problems.list()).toEqual([]);
  });
}

test('the support page tells a customer what the watch page really offers', async ({ page, server }) => {
  // The note under a video that will not load is a button that says "try again". The advice
  // here used to be "tap play again", which does nothing on a player that has failed.
  await page.goto(server.info.support);
  await expect(page.locator('main')).toContainText('press “try again” under it');
  await expect(page.locator('main')).not.toContainText('tap play again');
  await expect(page.locator('main')).toContainText('This is not my home');
});

test('a link opened too many times in a minute says "wait a minute", not "no longer available"', async ({ page, server, problems }) => {
  // The real limiter: 120 page opens a minute from one address (a family's Wi-Fi, an office,
  // a mail gateway that opens every link). This test has an address of its own, so it slows
  // nobody else down (serve.py: clients from 198.18.0.0/15 are always limited for real).
  const address = server.limitedAddress();
  const from = { 'X-Forwarded-For': address };
  const clip = await server.newVideo();
  const opens = await Promise.all(Array.from({ length: 120 }, () => page.request.get(clip.watch, { headers: from }).then((r) => r.status())));
  expect(opens.filter((s) => s !== 200), 'the first 120 opens in a minute must all be answered').toEqual([]);

  await page.setExtraHTTPHeaders(from);
  problems.allow(/status of 429/);
  const response = await page.goto(clip.watch);
  expect(response.status()).toBe(429);
  const wait = Number(response.headers()['retry-after']);
  expect(wait, 'browsers and gateways are told when to come back: the real wait, about a minute here').toBeGreaterThanOrEqual(40);
  expect(wait).toBeLessThanOrEqual(61);
  expect(response.headers()['cache-control']).toBe('no-store');
  // The video is fine. Telling this customer it "is no longer available" sends them to the
  // phone, or makes them give up, over a one-minute wait.
  await expect(page.locator('h1')).toHaveText('Please wait a minute');
  await expect(page).toHaveTitle(/Please wait a minute/);
  // It is the address that is counted, not the link: a neighbour on the same connection who
  // has never opened theirs must not read that "this link" was opened many times.
  await expect(page.locator('main')).toContainText('Too many visits have come from your internet connection. Wait a minute, then open your link again.');
  await expect(page.locator('body')).not.toContainText('This link has been opened');
  await expect(page.locator('body')).not.toContainText('no longer available');
  await expect(page.locator('video')).toHaveCount(0);
  await expect(page.locator('main a.btn')).toHaveText(CALL);
  await expectReadable(page);

  // Nobody else is affected: another address opens the same link at once.
  const other = await page.request.get(clip.watch, { headers: { 'X-Forwarded-For': server.limitedAddress() } });
  expect(other.status()).toBe(200);
  expect(await problems.list()).toEqual([]);
});

test('a player asking for the video many times does not use up the page opens', async ({ page, server }) => {
  // One viewing is several requests for /video (Safari and Firefox ask again for every piece,
  // and a seek asks again). They are counted apart from page opens (600 a minute against 120):
  // counted together, a household watching three videos could be locked out of opening a fourth.
  const address = server.limitedAddress();
  const from = { 'X-Forwarded-For': address };
  const clip = await server.newVideo();
  const pieces = await Promise.all(Array.from({ length: 130 }, () => page.request.get(clip.video, { headers: { ...from, Range: 'bytes=0-1' }, maxRedirects: 0 }).then((r) => r.status())));
  expect(pieces.filter((s) => s !== 302), '130 requests from the player must all be sent on to the bucket').toEqual([]);

  await page.setExtraHTTPHeaders(from);
  const response = await page.goto(clip.watch);
  expect(response.status(), 'after 130 player requests the page itself must still open').toBe(200);
  await expect(page.locator('#v')).toHaveCount(1);
  const played = await page.request.post(`${clip.watch}/played`, { headers: from });
  expect(played.status(), 'and "played" must still be accepted').toBeLessThan(300);
});
