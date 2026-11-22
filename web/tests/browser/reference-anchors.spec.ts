import { expect, test } from '@playwright/test';
import { openSignedIn, reset, scanAccessibility } from './helpers';

// R5: the reviewer has to be able to tell a dub-caption anchor (the dub's own words, a
// near-certain repair) from a translation anchor (a judgement call) from no anchor at all.
// The fixture holds one of each plus a pre-R2 row, which renders "Unclassified".
test('the queue labels what each repair was anchored on and filters by it', async ({
  page,
  request,
}, testInfo) => {
  await reset(request);
  const errors: string[] = [];
  page.on('pageerror', (err) => errors.push(err.message));
  await openSignedIn(page);
  await expect(page.getByRole('heading', { name: 'Review inbox', level: 1 })).toBeVisible();
  await page.getByLabel('Search episodes').fill('Neon Harbor');
  await page.getByRole('button', { name: /Neon Harbor.*S01E01/ }).click();
  await expect(page.getByTestId('evidence')).toBeVisible();

  const lines = page.getByLabel('Search review lines');
  const anchors = page.getByLabel('Filter by reference anchor');
  const chip = page.getByTestId('evidence').locator('[data-ref-class]');

  await expect(anchors).toHaveValue('all');
  await expect(page.getByTestId('evidence')).toContainText(/Line \d+ of 4/);

  // A dub-caption anchor: role + provenance, with the source track named.
  await lines.fill('factory');
  await expect(chip).toHaveAttribute('data-ref-class', 'dub');
  await expect(chip).toHaveText('Dub-anchored · native');
  await expect(chip).toHaveAttribute('title', /track: English Dub/);
  await expect(page.getByTestId('evidence')).toContainText('Captions of the dub itself');
  const accessibility = await scanAccessibility(page);
  expect(accessibility.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath('review-anchor-dub.png'), fullPage: true });

  // A translation anchor: a different axis, labelled as such, and not called the dub's words.
  await lines.fill('harbor');
  await expect(chip).toHaveAttribute('data-ref-class', 'translation');
  await expect(chip).toHaveText('Translation-anchored · native');
  await expect(page.getByTestId('evidence')).toContainText('A different translation, not the answer');

  // A pre-R2 row: it HAS a reference, so it is unclassified, not unanchored.
  await lines.fill('compass');
  await expect(chip).toHaveAttribute('data-ref-class', 'unclassified');
  await expect(chip).toHaveText('Unclassified');
  // A pre-R2 anchor is NOT a translation: the note must not call it one.
  await expect(page.getByTestId('evidence')).toContainText('before its role was recorded');

  // The filter works on the same classes.
  await lines.fill('');
  await anchors.selectOption('dub');
  await expect(page.getByTestId('evidence')).toContainText('Line 1 of 1');
  await expect(chip).toHaveText('Dub-anchored · native');
  await anchors.selectOption('translation');
  await expect(page.getByTestId('evidence')).toContainText('Line 1 of 1');
  await expect(chip).toHaveText('Translation-anchored · native');
  await anchors.selectOption('unclassified');
  await expect(page.getByTestId('evidence')).toContainText('Line 1 of 2');
  await expect(chip).toHaveText('Unclassified');
  await page.screenshot({ path: testInfo.outputPath('review-anchor-unclassified.png'), fullPage: true });
  // This episode has an anchor on every card, so the unanchored filter is honestly empty.
  await anchors.selectOption('unanchored');
  await expect(page.getByRole('heading', { name: 'No matching changes' })).toBeVisible();
  await anchors.selectOption('all');
  await expect(page.getByTestId('evidence')).toContainText(/Line \d+ of 4/);

  // A card with no reference at all. S02E04 holds only a refused entry, so the inbox hides it
  // until the include-refused toggle is on.
  await page.getByRole('checkbox', { name: /Include episodes where every repair was turned down/ }).check();
  await page.getByLabel('Search episodes').fill('Paper Moon');
  await page.getByRole('button', { name: /Paper Moon.*S02E04/ }).click();
  await expect(page.getByTestId('evidence')).toBeVisible();
  await expect(chip).toHaveAttribute('data-ref-class', 'unanchored');
  await expect(chip).toHaveText('Unanchored');
  await anchors.selectOption('unanchored');
  await expect(page.getByTestId('evidence')).toContainText('Line 1 of 1');

  expect(errors).toEqual([]);
});
