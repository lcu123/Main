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

  await button.click();
  await expect(page).toHaveURL(server.info.watch);
  await waitForMetadata(page);
});
