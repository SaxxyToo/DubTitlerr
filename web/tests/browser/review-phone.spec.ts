import { expect, test, type Page } from '@playwright/test';
import { navTo, openInbox, reset } from './helpers';

/**
 * The phone composition: episodes are one screen, the open line is the next, and the verdicts
 * live in a fixed dock within thumb reach. These assertions run at an iPhone-sized viewport in
 * both projects, so the desktop two-column layout is never what is being measured here.
 */
test.use({ viewport: { width: 390, height: 664 }, hasTouch: true, isMobile: true });

test.beforeEach(async ({ request }) => reset(request));

async function openFirstEpisode(page: Page) {
  await openInbox(page);
  await page.locator('.episode-list .episode').first().click();
  await expect(page.locator('.review-panel')).toBeVisible();
  await expect(page.locator('.review-title h2')).not.toHaveText('Choose an episode');
}

test('the phone inbox and editor are separate screens', async ({ page }) => {
  await openInbox(page);
  await expect(page.locator('.episode-panel')).toBeVisible();
  await expect(page.locator('.review-panel')).toBeHidden();

  await page.locator('.episode-list .episode').first().click();
  await expect(page.locator('.episode-panel')).toBeHidden();
  await expect(page.locator('.review-panel')).toBeVisible();

  await page.getByRole('button', { name: 'Episodes', exact: true }).click();
  await expect(page.locator('.episode-panel')).toBeVisible();
  await expect(page.locator('.review-panel')).toBeHidden();
  // Focus returns to the episode that was open, so the list continues where it left off.
  await expect(page.locator('.episode.selected')).toBeFocused();
});

test('the verdict dock is in reach and the line text fits above it', async ({ page }) => {
  await openFirstEpisode(page);

  const dock = page.locator('.phone-dock');
  await expect(dock).toBeVisible();
  const dockBox = await dock.boundingBox();
  expect(dockBox!.y + dockBox!.height).toBeLessThanOrEqual(664);
  expect(dockBox!.y).toBeGreaterThan(664 * 0.6); // pinned low, not floating mid-screen

  // The point of the composition: the proposed text is readable before any scroll.
  const repair = page.locator('.deck-repair');
  const repairBox = await repair.boundingBox();
  expect(repairBox!.y + repairBox!.height).toBeLessThanOrEqual(dockBox!.y + 1);
  await expect(repair).toBeInViewport();

  // Fingers, not a cursor.
  for (const verdict of await page.locator('.dock-verdicts .verdict').all()) {
    const box = await verdict.boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }
  for (const button of await page.locator('.dock-nav button').all()) {
    const box = await button.boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }

  // A touch screen has no keyboard: no shortcut chips anywhere.
  expect(await page.locator('kbd:visible').count()).toBe(0);
  // Nothing may force a horizontal scroll on a phone.
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test('a staged verdict advances the line and leaves the dock in place', async ({ page }) => {
  await openFirstEpisode(page);
  await expect(page.locator('.dock-status')).toContainText('Line 1 of');
  const save = page.locator('.dock-save');
  await expect(save).toBeDisabled();

  await page.locator('.dock-verdicts .verdict').first().click();
  await expect(page.locator('.dock-status')).toContainText('Line 2 of');
  await expect(save).toBeEnabled();
  await expect(save).toContainText('1 choice');
  await expect(page.locator('.phone-dock')).toBeVisible();
});

test('the line filters stay one tap away', async ({ page }) => {
  await openFirstEpisode(page);
  await expect(page.locator('.review-toolbar')).toBeHidden();
  await page.getByRole('button', { name: 'Review filters' }).click();
  await expect(page.locator('.review-toolbar')).toBeVisible();
  await expect(page.getByLabel('Search review lines')).toBeVisible();
  await page.getByRole('button', { name: 'Review filters' }).click();
  await expect(page.locator('.review-toolbar')).toBeHidden();
});

test('shared lines keep their editor and dock on a phone', async ({ page }) => {
  await openInbox(page);
  await navTo(page, 'Shared lines');
  await expect(page.locator('.review-panel')).toBeVisible();
  await expect(page.locator('.episode-panel')).toHaveCount(0);
  await expect(page.locator('.phone-dock')).toBeVisible();
});
