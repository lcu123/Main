// The pages around the watch page, on every project: a dead link, a link nobody ever sent,
// unsubscribe, privacy, terms and support.

const { test, expect, checkGap, sidewaysScroll, tapTargets, textSizes, insideFirstScreen, press, shot } = require('../lib/harness');

async function expectReadable(page, testInfo, { infoPage = false, lastUpdated = false } = {}) {
  expect.soft(await sidewaysScroll(page), 'the page scrolls sideways').toEqual([]);
  // On the long text pages, a phone or email link inside a sentence is as tall as the sentence;
  // only stand-alone controls must be 44 px there.
  const small = (await tapTargets(page)).filter((t) => !(infoPage && t.inline) && (t.width < 44 || t.height < 44));
  expect.soft(small.map((t) => `${t.name} is ${t.width}x${t.height} px`), 'a control is smaller than 44x44 px').toEqual([]);
  const tiny = (await textSizes(page)).filter((t) => t.size < 16).map((t) => `${t.where} "${t.text}" is ${t.size} px`);
  if (lastUpdated) {
    checkGap(testInfo, 'last-updated-is-15px', tiny.filter((t) => t.includes('Last updated')), 'text is smaller than 16 px');
    expect.soft(tiny.filter((t) => !t.includes('Last updated')), 'text is smaller than 16 px').toEqual([]);
  } else {
    expect.soft(tiny, 'text is smaller than 16 px').toEqual([]);
  }
}

for (const [name, which, status] of [['a video that was taken down', 'dead', 410], ['a link nobody ever sent', 'unknown', 404]]) {
  test(`${name}: a kind message and the office number, no player`, async ({ page, server, problems }, testInfo) => {
    const response = await page.goto(server.info[which]);
    expect(response.status()).toBe(status);
    expect(response.headers()['cache-control']).toBe('no-store');
    await expect(page.locator('h1')).toHaveText('This video is no longer available.');
    await expect(page.locator('main')).toContainText('If you think this is a mistake, please call our office.');
    await expect(page.locator('video')).toHaveCount(0);

    const call = page.locator('main a.btn');
    await expect(call).toHaveText('Call our office');
    await expect(call).toHaveAttribute('href', `tel:${server.info.officePhone}`);
    expect(await insideFirstScreen(page, 'main a.btn', 'the "Call our office" button')).toBe('');
    await expect(page.locator('footer a[href^="tel:"]')).toHaveText('(916) 555-0100');
    await expectReadable(page, testInfo);
    if (which === 'dead') await shot(page, testInfo, 'dead-link');
    expect(await problems.list()).toEqual([]);
  });
}

test('unsubscribe: one clear question, one button, and it is done', async ({ page, clip, problems }, testInfo) => {
  await page.goto(clip.unsubscribe);
  await expect(page.locator('h1')).toHaveText('Stop video emails?');
  await expect(page.locator('main')).toContainText('Your service does not change.');
  expect(await insideFirstScreen(page, 'form button', 'the button')).toBe('');
  await expectReadable(page, testInfo);

  await press(page, testInfo, 'form button[type=submit]');
  await expect(page.locator('h1')).toHaveText('Done');
  await expect(page.locator('main')).toContainText('You will not get video emails from us anymore.');
  expect(await problems.list()).toEqual([]);
});

for (const [kind, heading] of [['privacy', 'Privacy Policy'], ['terms', /Terms/], ['support', /Support|Help/]]) {
  test(`${kind} page opens, reads without sideways scroll, and links back to the office`, async ({ page, server, problems }, testInfo) => {
    const response = await page.goto(server.info[kind]);
    expect(response.status()).toBe(200);
    await expect(page.locator('h1')).toHaveText(heading);
    await expect(page.locator('footer a[href^="tel:"]')).toHaveText('(916) 555-0100');
    await expectReadable(page, testInfo, { infoPage: true, lastUpdated: kind !== 'support' });
    expect(await problems.list()).toEqual([]);
  });
}
