// The email the customer gets: one button. Real mail apps (Gmail, Outlook, Apple Mail, Yahoo)
// each rewrite a message in their own way and cannot be run here; this opens the exact HTML
// the server sent in each engine at this project's screen size, and follows the button.

const { test, expect, sidewaysScroll, waitForMetadata } = require('../lib/harness');

test('the email shows one big button, fits the screen, and the button opens the video', async ({ page, server }, testInfo) => {
  const token = server.info.watch.split('/w/')[1];
  await page.goto(server.emailUrl(token));
  const button = page.locator('a', { hasText: 'Watch your video' });
  await expect(button).toBeVisible();
  await expect(button).toHaveAttribute('href', server.info.watch);
  const box = await button.boundingBox();
  expect(box.height, 'the button must be easy to hit').toBeGreaterThanOrEqual(44);
  expect(box.width).toBeGreaterThanOrEqual(200);
  expect(await sidewaysScroll(page), 'the email scrolls sideways').toEqual([]);

  const logo = await page.locator('img').first().evaluate((img) => ({ ok: img.complete && img.naturalWidth > 0, alt: img.alt, src: img.src }));
  expect(logo.ok, `the logo in the email did not load from ${logo.src}`).toBe(true);
  expect(logo.alt).toBe(server.info.brand);
  await expect(page.locator('a', { hasText: 'Stop these video emails' })).toHaveAttribute('href', /\/u\/[0-9a-f]{32}$/);
  await expect(page.locator('a[href^="tel:"]')).toHaveText('(916) 555-0100');

  // Mail apps strip <style> and <script>; the email must not need either. Gmail cuts a message off at 102 KB.
  const html = await (await page.request.get(server.emailUrl(token))).text();
  expect(html).not.toMatch(/<script|<style|<link/i);
  expect(Buffer.byteLength(html)).toBeLessThan(102_000);

  // The triangle on the button is U+25BA. U+25B6, the "play" sign, is drawn as a colour emoji
  // by iPhones and Android phones: a blue square in the middle of the purple button.
  expect(await button.textContent()).toContain('\u25BA');
  expect(html, 'the button must not carry U+25B6, which phones turn into an emoji').not.toMatch(/&#9654;|\u25B6/);

  // Classic Outlook (Word's engine; no test machine runs it) ignores padding on a link and
  // max-width on a table. What can be checked here is that the two things it needs are in
  // the message: the colour and Outlook's own padding on the CELL round the link, and a
  // fixed-width table just for Outlook, inside conditional comments every other app skips.
  const cell = button.locator('xpath=ancestor::td[1]');
  await expect(cell).toHaveAttribute('bgcolor', '#6E20B9');
  expect(await cell.getAttribute('style'), 'the cell needs mso-padding-alt, or Outlook draws a button with no room round its words').toMatch(/mso-padding-alt:\s*18px 20px/);
  expect(html, 'the fixed-width table for Outlook is missing').toMatch(/<!--\[if mso\]><table[^>]*width="520"/);
  expect((html.match(/<!--\[if mso\]>/g) || []).length, 'each Outlook-only opening needs its closing').toBe(2);
  expect(html).toMatch(/<!--\[if mso\]><\/td><\/tr><\/table><!\[endif\]-->/);
  // The whole coloured cell is as tall as the link in it: no dead strip a finger could hit without opening the video.
  const cellBox = await cell.boundingBox();
  expect(Math.abs(cellBox.height - box.height), 'the link should fill its coloured cell').toBeLessThanOrEqual(2);

  await button.click();
  await expect(page).toHaveURL(server.info.watch);
  await waitForMetadata(page);
});
