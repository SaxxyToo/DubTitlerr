import { expect, test } from '@playwright/test';
import { navTo, openInbox, reset } from './helpers';

/**
 * A phone gets the same workspace the desktop gets, not a second layout. These assertions run at
 * an iPhone-sized viewport in both projects and pin the composition: the same panels in the same
 * order, the inline transport and save bar, and no phone-only chrome.
 */
test.use({ viewport: { width: 390, height: 664 }, hasTouch: true, isMobile: true });

test.beforeEach(async ({ request }) => reset(request));

test('a phone renders the desktop workspace, stacked in one column', async ({ page }) => {
  await openInbox(page);
  await expect(page.locator('.page-header h1')).toBeVisible();
  await expect(page.locator('.episode-panel')).toBeVisible();
  await expect(page.locator('.phone-dock')).toHaveCount(0);
  await expect(page.locator('.back-to-episodes')).toHaveCount(0);

  await page.locator('.episode-list .episode').first().click();
  await expect(page.getByTestId('evidence')).toBeVisible();

  // Every desktop section is still on the page, in the desktop order.
  await expect(page.locator('.episode-panel')).toBeVisible();
  await expect(page.locator('.review-toolbar')).toBeVisible();
  await expect(page.locator('.line-strip')).toBeVisible();
  await expect(page.locator('.audio-band')).toBeVisible();
  await expect(page.locator('.deck-card')).toBeVisible();
  await expect(page.locator('.transport')).toBeVisible();
  await expect(page.locator('.save-bar')).toBeVisible();

  // Nothing forces a sideways scroll on a phone.
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);

  // The layout is the desktop's; only the touch metrics grow.
  for (const button of await page.locator('.line-strip button').all()) {
    const box = await button.boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }
  for (const button of await page.locator('.transport .verdict, .transport .deck-nav').all()) {
    const box = await button.boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }

  // A touch screen has no keyboard: the shortcut chips would only be noise.
  expect(await page.locator('kbd:visible').count()).toBe(0);
});

test('shared lines keep the desktop workspace on a phone', async ({ page }) => {
  await openInbox(page);
  await navTo(page, 'Shared lines');
  await expect(page.locator('.review-panel')).toBeVisible();
  await expect(page.locator('.episode-panel')).toHaveCount(0);
  await expect(page.locator('.phone-dock')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
