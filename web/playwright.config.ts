import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  fullyParallel: false,
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: 'http://127.0.0.1:18842',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : {},
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } } },
    { name: 'mobile', use: { ...devices['iPhone 13'], defaultBrowserType: 'chromium' } },
  ],
  webServer: {
    command: 'python3 tests/fixture_server.py',
    // The fixture binds only after it has seeded its media and glossary files, so answering here
    // means it is already serving real data: no test can start against a half-built fixture. Its
    // `[WebServer] BrokenPipeError` noise came from clients hanging up mid-response (handled in
    // tests/fixture_server.py), never from this server dying -- job 136719511316 logged one and
    // then served 258 more tests.
    url: 'http://127.0.0.1:18842/review',
    reuseExistingServer: false,
    timeout: 30000,
  },
  // No retries, deliberately. The suite's one recurring flake was an axe scan sampling a 150 ms
  // colour fade on a button that had just been enabled; the scan now runs with reduced motion
  // (tests/browser/helpers.ts), which removes the race rather than hiding it. Retrying here would
  // mask the next real contrast regression, and a passed-on-retry nobody reads is not a signal.
});
