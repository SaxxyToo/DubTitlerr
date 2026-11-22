import AxeBuilder from '@axe-core/playwright';
import { expect, type APIRequestContext, type Page } from '@playwright/test';

/** What AxeBuilder.analyze() resolves to, without importing axe-core (a transitive dependency). */
export type AxeResults = Awaited<ReturnType<AxeBuilder['analyze']>>;

export const USERNAME = 'reviewer';
export const PASSWORD = 'browser-fixture-password';
export const API_KEY = 'browser-fixture-token';

/** Put the fixture back to its starting state: decisions, queues, glossaries and run requests. */
export async function reset(request: APIRequestContext) {
  const res = await request.post('/__test/reset', { headers: { 'X-Api-Key': API_KEY } });
  expect(res.ok()).toBe(true);
}

/** Fill the sign-in form that is already on screen and submit it. */
export async function signIn(page: Page, username = USERNAME, password = PASSWORD) {
  // exact: a bare 'Password' also matches 'New password', 'Current password' and the rest on the
  // Settings page, so a sign-in that ran before that page was gone failed as a strict-mode violation
  // (CI, 2026-11-18: resolved to 6 elements). An exact label matches nothing there, so fill() waits
  // for the sign-in form itself. signOut() cannot wait for it instead: an edited page intercepts
  // the sign-out with the Unsaved changes dialog, and a spec tests exactly that.
  await page.getByLabel('Username', { exact: true }).fill(username);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
}

/** Open a hash route signed out, then sign in (a deep link keeps its hash through login). */
export async function openSignedIn(page: Page, hash = '') {
  await page.goto('/review' + hash);
  await signIn(page);
  // The sidebar is on screen signed out too; the account menu only renders once the session cookie is set and the workspace loaded.
  await expect(page.getByRole('button', { name: 'Account menu' })).toBeVisible();
}

/**
 * Open the review inbox explicitly.
 *
 * Since P33 the workspace opens on Recent decisions (`#/audit`); the inbox is the exception
 * list, not the landing page. A spec that drives the episode queue has to ask for it, and
 * asking here keeps one definition of "the inbox is open" instead of a bare heading check
 * scattered through the suite.
 */
export async function openInbox(page: Page) {
  await openSignedIn(page, '#/inbox');
  await expect(page.getByRole('heading', { name: 'Review inbox', level: 1 })).toBeVisible();
}

/** Open the account menu (unless it is already open) and sign out. */
export async function signOut(page: Page) {
  const menu = page.getByRole('button', { name: 'Account menu' });
  if ((await menu.getAttribute('aria-expanded')) !== 'true') await menu.click();
  await page.getByRole('button', { name: 'Sign out' }).click();
}

/** The sidebar button for a view, opening the mobile drawer first when it is collapsed. */
export async function navTo(page: Page, name: string | RegExp) {
  const toggle = page.getByRole('button', { name: 'Toggle navigation' });
  if (await toggle.isVisible()) await toggle.click();
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name }).click();
}

/** WCAG 2.x A/AA scan of the whole page, expecting zero violations. */
export async function expectNoAxeViolations(page: Page) {
  const results = await scanAccessibility(page);
  expect(results.violations).toEqual([]);
}

/**
 * WCAG 2.x A/AA scan of the whole page, with motion switched off first.
 *
 * The app honours `@media (prefers-reduced-motion: reduce)` (src/app.css), which drops every
 * transition and shortens every animation, so a scan reads resting colours rather than a frame
 * caught in the middle of one. Scanning live motion made this suite flaky for real: the review
 * workspace's Save button is disabled until a choice is staged, and enabling it fades
 * `--surface` (#2a292a) to `--accent` (#e79b32) over 150 ms (`transition: background 0.15s`,
 * src/app.css). For roughly the first 60 ms of that fade the button renders a brown in between
 * (axe measured #976b2f, 3.68:1 against the #1c1a17 label) and every intermediate below ~#b67e30
 * fails the 4.5:1 rule. Whether axe sampled inside that window depended on machine load, so the
 * same assertion passed and failed on identical code (CI 2026-11-21, review.spec.ts on both
 * viewports, jobs 136719511316 and 136728162440).
 *
 * Switching motion off does not weaken coverage: axe judges a rendered state, WCAG's contrast
 * rule is not about transient animation frames, and reduced motion is a state real users run in,
 * so the scan still exercises the same DOM, markup and palette it always did.
 */
export async function scanAccessibility(page: Page): Promise<AxeResults> {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  // The media switch is applied on the next style recalc; a frame of the old colour can still be
  // in flight, so let two frames pass before axe samples computed styles.
  await page.evaluate(
    () => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))),
  );
  return new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
}
