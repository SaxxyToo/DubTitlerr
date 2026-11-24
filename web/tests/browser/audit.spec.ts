import { expect, test } from '@playwright/test';
import { expectNoAxeViolations, openInbox, openSignedIn, reset } from './helpers';

// P33: the review workspace opens on what the app DECIDED (Recent decisions, `#/audit`), not on
// the pending inbox. These assertions pin that default explicitly: every other spec that drives
// the episode queue asks for `#/inbox` through openInbox(), so if the landing page silently flipped
// back to the inbox the queue specs would still pass -- only this file would fail.
test.beforeEach(async ({ request }) => reset(request));

test('the bare workspace lands on Recent decisions, not on the pending inbox', async ({ page }) => {
  await openSignedIn(page, '');
  await expect(page.getByRole('heading', { name: 'Recent decisions', level: 1 })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Review inbox', level: 1 })).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Recent auto decisions' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Queue health' })).toBeVisible();
  // A bare /review names no view, and the app does not rewrite the URL; the view rendered is the audit.
  expect(new URL(page.url()).hash).toBe('');
  await expectNoAxeViolations(page);
});

test('the empty landing explains itself: zero decisions is a designed state, not a blank page', async ({
  page,
}) => {
  // A brand-new install: the pipeline has decided nothing yet, so the feed and the queues are empty.
  const json = (body: object) => ({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
  await page.route('**/api/audit**', (route) =>
    route.fulfill(json({ decisions: [], counts: {}, undoable: 0, policy: 'p33-v1' })),
  );
  await page.route('**/api/queues', (route) =>
    route.fulfill(
      json({
        policy: 'p33-v1',
        expiry_days: 14,
        max: 500,
        glossary: { queued: 0, expired: 0, by_class: {}, report: [] },
      }),
    ),
  );
  await openSignedIn(page, '');
  await expect(page.getByRole('heading', { name: 'Recent decisions', level: 1 })).toBeVisible();
  await expect(page.getByText('No automatic decisions recorded yet')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'No automatic decisions yet' })).toBeVisible();
  await expect(page.getByText('Nothing is queued. That is the normal state now.')).toBeVisible();
  await expectNoAxeViolations(page);
});

test('Review inbox is the way to the pending queue, and Back returns to the audit', async ({ page }) => {
  await openSignedIn(page, '');
  await page.locator('.page-actions').getByRole('button', { name: /Review inbox/ }).click();
  await expect(page.getByRole('heading', { name: 'Review inbox', level: 1 })).toBeVisible();
  expect(new URL(page.url()).hash).toBe('#/inbox');
  await page.goBack();
  await expect(page.getByRole('heading', { name: 'Recent decisions', level: 1 })).toBeVisible();
});

test('a deep link to the inbox is still the inbox', async ({ page }) => {
  // openInbox is the suite's shared door into the queue; assert it rather than assume it.
  await openInbox(page);
  expect(new URL(page.url()).hash).toBe('#/inbox');
  await expect(page.getByRole('heading', { name: 'Review inbox', level: 1 })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Recent decisions', level: 1 })).toHaveCount(0);
});
