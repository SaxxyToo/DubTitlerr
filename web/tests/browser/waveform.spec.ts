import { expect, test, type Page } from '@playwright/test';

// The Audio Deck used to draw a waveform that was pure decoration: the playhead only moved four
// times a second and nothing could seek it. These checks pin the behaviour the operator asked for —
// the playhead follows the clip, and a click or drag on the wave lands where it was aimed.

async function openClip(page: Page): Promise<void> {
  await page.goto('/review');
  await page.getByLabel('Username').fill('reviewer');
  await page.getByLabel('Password').fill('browser-fixture-password');
  await page.getByLabel('Password').press('Enter');
  // P33: the workspace opens on Recent decisions; the review inbox is one click from its header.
  await page
    .locator('.page-actions')
    .getByRole('button', { name: /Review inbox/ })
    .click();
  await page.getByLabel('Search episodes').fill('Neon Harbor');
  await page.getByRole('button', { name: /Neon Harbor.*S01E01/ }).click();
  await page.getByLabel('Search review lines').fill('factory');
  await expect(page.locator('ins')).toContainText('needle');
}

/** 0..1 position of the playhead along the band's trace span. */
async function playheadFraction(page: Page): Promise<number> {
  return page.evaluate(() => {
    const band = document.querySelector('.audio-band')!.getBoundingClientRect();
    const head = document.querySelector('.band-playhead')!.getBoundingClientRect();
    const start = band.left + band.width * 0.04;
    const span = band.width * 0.92;
    return (head.left + head.width / 2 - start) / span;
  });
}

/** 0..1 position of the audio element's own clock. */
async function audioFraction(page: Page): Promise<number> {
  return page.locator('audio').evaluate((el: HTMLAudioElement) => el.currentTime / el.duration);
}

async function ready(page: Page): Promise<number> {
  await expect
    .poll(() => page.locator('audio').evaluate((el: HTMLAudioElement) => el.duration))
    .toBeGreaterThan(1);
  return page.locator('audio').evaluate((el: HTMLAudioElement) => el.duration);
}

test('the waveform playhead follows the audio and the wave seeks where it is clicked', async ({
  page,
  request,
}) => {
  test.setTimeout(90_000);
  const reset = await request.post('/__test/reset', {
    headers: { 'X-Api-Key': 'browser-fixture-token' },
  });
  expect(reset.ok()).toBe(true);
  await openClip(page);

  const seek = page.getByTestId('band-wave-seek');
  // On the single-column layout the deck sits below the episode list; click where it actually is.
  await seek.scrollIntoViewIfNeeded();
  const box = (await seek.boundingBox())!;
  expect(box.width).toBeGreaterThan(100);
  // The seek surface and the playhead start at the trace's own left edge, not the band's: a click
  // at the start of the waveform is time zero, and the playhead is drawn there when it is 0.
  const waveBox = (await page.locator('.band-wave').boundingBox())!;
  const headBox = (await page.locator('.band-playhead').boundingBox())!;
  expect(Math.abs(box.x - waveBox.x)).toBeLessThan(1.5);
  expect(Math.abs(box.width - waveBox.width)).toBeLessThan(1.5);
  expect(Math.abs(box.height - waveBox.height)).toBeLessThan(1.5);
  expect(Math.abs(headBox.x + headBox.width / 2 - waveBox.x)).toBeLessThan(1.5);
  const bandBefore = (await page.locator('.audio-band').boundingBox())!;

  // A click before anything is loaded must still land: the clip is fetched and the seek applied on
  // metadata, rather than silently starting playback from zero.
  await page.mouse.click(box.x + box.width * 0.5, box.y + box.height / 2);
  const duration = await ready(page);
  expect(await audioFraction(page)).toBeCloseTo(0.5, 1);

  // A drag scrubs: down at 20%, up at 70% leaves the playhead and the element at 70%.
  await page.mouse.move(box.x + box.width * 0.2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.4, box.y + box.height / 2, { steps: 3 });
  await page.mouse.move(box.x + box.width * 0.7, box.y + box.height / 2, { steps: 3 });
  await page.mouse.up();
  expect(await audioFraction(page)).toBeCloseTo(0.7, 1);
  expect(await playheadFraction(page)).toBeCloseTo(0.7, 1);

  // The band does not resize when the clip appears or moves: no layout shift.
  const bandAfter = (await page.locator('.audio-band').boundingBox())!;
  expect(Math.round(bandAfter.height)).toBe(Math.round(bandBefore.height));
  expect(Math.round(bandAfter.width)).toBe(Math.round(bandBefore.width));

  // While it plays, the playhead is drawn where the audio clock is — sampled twice, a few hundred
  // milliseconds apart, to prove it tracks rather than snapping once. 100 ms on this 5 s clip is
  // 0.02 of the span.
  await page.getByRole('button', { name: /Listen to dub/ }).click();
  await expect.poll(() => page.locator('audio').evaluate((el: HTMLAudioElement) => el.paused)).toBe(false);
  for (let i = 0; i < 2; i++) {
    await page.waitForTimeout(400);
    const [head, clock] = [await playheadFraction(page), await audioFraction(page)];
    expect(head).toBeCloseTo(clock, 1);
    expect(Math.abs(head - clock) * duration).toBeLessThan(0.1);
  }

  // The playhead actually moved while playing, and seeking by click parks playback there.
  expect(await playheadFraction(page)).toBeGreaterThan(0.7);
  await page.getByRole('button', { name: /Listen to dub/ }).click(); // pause
  await expect.poll(() => page.locator('audio').evaluate((el: HTMLAudioElement) => el.paused)).toBe(true);
  const moved = await audioFraction(page);
  await page.mouse.click(box.x + box.width * 0.1, box.y + box.height / 2);
  expect(await audioFraction(page)).toBeCloseTo(0.1, 1);
  expect(await playheadFraction(page)).toBeCloseTo(0.1, 1);
  expect(moved).toBeGreaterThan(0.7);
});
