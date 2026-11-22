import { expect, test } from '@playwright/test';
import { USERNAME, expectNoAxeViolations, openSignedIn, reset, signIn, signOut } from './helpers';

test.beforeEach(async ({ request }) => reset(request));

test('signed out, the workspace asks for a username and password and has no token field', async ({
  page,
}) => {
  await page.goto('/review');
  await expect(page.getByRole('heading', { name: 'Open your review workspace' })).toBeVisible();
  await expect(page.getByLabel('Username')).toBeVisible();
  await expect(page.getByLabel('Password')).toHaveAttribute('autocomplete', 'current-password');
  await expect(page.getByLabel('Access token')).toHaveCount(0);
  await expectNoAxeViolations(page);
});

test('a wrong password is one plain sentence, the right one opens the workspace and a reload stays signed in', async ({
  page,
}) => {
  await page.goto('/review');
  await signIn(page, USERNAME, 'not the password');
  await expect(page.getByRole('alert')).toContainText('wrong username or password');
  await signIn(page);
  // P33: a signed-in workspace opens on Recent decisions (the audit), not on the pending inbox.
  await expect(page.getByRole('heading', { name: 'Recent decisions', level: 1 })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Recent decisions', level: 1 })).toBeVisible();
});

test('the session is an HttpOnly cookie the page cannot read, and no token is kept in storage', async ({
  page,
  context,
}) => {
  await openSignedIn(page);
  const stored = await page.evaluate(() => ({
    cookie: document.cookie,
    session: Object.keys(sessionStorage),
    local: Object.keys(localStorage),
  }));
  expect(stored).toEqual({ cookie: '', session: [], local: [] });
  const cookie = (await context.cookies()).find((c) => c.name === 'dubtitlerr_session');
  expect(cookie?.httpOnly).toBe(true);
  expect(cookie?.sameSite).toBe('Strict');
});

test('a session the server no longer knows sends the page back to the sign-in form and keeps the route', async ({
  page,
  context,
}) => {
  await openSignedIn(page, '#/glossary');
  await context.clearCookies();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Open your review workspace' })).toBeVisible();
  expect(new URL(page.url()).hash).toBe('#/glossary');
});

test('requests carry the CSRF header on writes and never a token header', async ({ page }) => {
  const seen: { method: string; headers: Record<string, string> }[] = [];
  page.on('request', (r) => {
    if (r.url().includes('/api/')) seen.push({ method: r.method(), headers: r.headers() });
  });
  await openSignedIn(page);
  await signOut(page);
  await expect(page.getByRole('heading', { name: 'Open your review workspace' })).toBeVisible();
  const writes = seen.filter((r) => r.method !== 'GET');
  expect(writes.length).toBeGreaterThanOrEqual(2); // the login and the logout
  for (const w of writes) expect(w.headers['x-dubtitlerr-request']).toBe('1');
  for (const r of seen) {
    expect(r.headers['x-review-token']).toBeUndefined();
    expect(r.headers['x-review-admin-token']).toBeUndefined();
  }
});

test('first run: the setup form creates the admin, checks the confirm field and signs in', async ({
  page,
}) => {
  let created: unknown;
  const json = (body: object, status = 200) => ({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
  await page.route('**/api/me', (route) => route.fulfill(json({ error: 'setup required' }, 409)));
  await page.route('**/api/setup', (route) => {
    created = route.request().postDataJSON();
    return route.fulfill(json({ username: 'boss', role: 'admin' }));
  });
  await page.route('**/api/login', (route) => route.fulfill(json({ username: 'boss', role: 'admin' })));
  await page.route('**/api/episodes', (route) => route.fulfill(json({ episodes: [] })));
  // The first admin lands on Recent decisions, which reads the audit feed and the queue health.
  // Both are empty on a brand-new install -- that empty landing is asserted below, deliberately:
  // audit-first is the default even before the pipeline has decided anything.
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
  // No mock session cookie exists, so the real status call would 401 and flip the page back to
  // login while the test is still asserting the freshly created account.
  await page.route('**/api/v1/system/status', (route) =>
    route.fulfill(json({ ok: true, version: { app: 'test' } })),
  );
  await page.goto('/review');
  await expect(page.getByRole('heading', { name: 'Create the first admin account' })).toBeVisible();
  await expectNoAxeViolations(page);
  await page.getByLabel('Username').fill('boss');
  await page.getByLabel('Password', { exact: true }).fill('a long enough pass');
  await page.getByLabel('Confirm password').fill('a different pass!');
  await page.getByRole('button', { name: 'Create account' }).click();
  await expect(page.getByRole('alert')).toContainText('Passwords do not match');
  expect(created).toBeUndefined();
  await page.getByLabel('Confirm password').fill('a long enough pass');
  await page.getByRole('button', { name: 'Create account' }).click();
  await expect(page.getByRole('heading', { name: 'Recent decisions', level: 1 })).toBeVisible();
  // Nothing has been decided yet: the landing explains the empty feed instead of showing a blank page.
  await expect(page.getByRole('heading', { name: 'No automatic decisions yet' })).toBeVisible();
  expect(created).toEqual({ username: 'boss', password: 'a long enough pass' });
});

test('setup that loses the race lands on the sign-in form with a sentence', async ({ page }) => {
  const json = (body: object, status: number) => ({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
  await page.route('**/api/me', (route) => route.fulfill(json({ error: 'setup required' }, 409)));
  await page.route('**/api/setup', (route) => route.fulfill(json({ error: 'already set up' }, 409)));
  await page.goto('/review');
  await page.getByLabel('Username').fill('boss');
  await page.getByLabel('Password', { exact: true }).fill('a long enough pass');
  await page.getByLabel('Confirm password').fill('a long enough pass');
  await page.getByRole('button', { name: 'Create account' }).click();
  await expect(page.getByRole('heading', { name: 'Open your review workspace' })).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('An account was created while this page was open');
});

test('a reset forgets earlier failed sign-ins, so a lockout never leaks into the next test', async ({
  page,
  request,
}) => {
  for (let i = 0; i < 5; i++) {
    const res = await request.post('/api/login', { data: { username: USERNAME, password: 'wrong' } });
    expect(res.status()).toBe(401);
  }
  expect(
    (await request.post('/api/login', { data: { username: USERNAME, password: 'wrong' } })).status(),
  ).toBe(429);
  await reset(request);
  await page.goto('/review');
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Recent decisions', level: 1 })).toBeVisible();
});
