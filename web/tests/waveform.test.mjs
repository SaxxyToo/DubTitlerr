import test from 'node:test';
import assert from 'node:assert/strict';
import { WAVE_INSET, waveFractionAt, waveLeft, waveTrace } from '../src/lib/waveform.js';

// The playhead and the seek surface have to describe the same span, or a click lands a few percent
// away from the stroke it is on. These are the pure halves of that contract; the browser suite
// covers the element wiring.

test('waveFractionAt maps the trace span, not the whole band', () => {
  const left = 100;
  const width = 1000;
  // The trace starts WAVE_INSET percent in from each edge of the band.
  assert.equal(waveFractionAt(left + (width * WAVE_INSET) / 100, left, width), 0);
  assert.equal(waveFractionAt(left + width / 2, left, width), 0.5);
  assert.equal(waveFractionAt(left + (width * (100 - WAVE_INSET)) / 100, left, width), 1);
});

test('waveFractionAt clamps a drag that leaves the band', () => {
  assert.equal(waveFractionAt(-500, 100, 1000), 0);
  assert.equal(waveFractionAt(5000, 100, 1000), 1);
});

test('waveFractionAt survives a zero-width band', () => {
  assert.equal(waveFractionAt(42, 0, 0), 0);
});

test('waveLeft is the inverse of waveFractionAt and stays inside the inset span', () => {
  assert.equal(waveLeft(0), `${WAVE_INSET.toFixed(3)}%`);
  assert.equal(waveLeft(0.5), '50.000%');
  assert.equal(waveLeft(1), `${(100 - WAVE_INSET).toFixed(3)}%`);
  // A not-yet-decoded duration must not push the playhead outside the trace.
  assert.equal(waveLeft(Number.NaN), `${WAVE_INSET.toFixed(3)}%`);
  assert.equal(waveLeft(2), waveLeft(1));
  const left = 100;
  const width = 1000;
  for (const fraction of [0, 0.25, 0.5, 0.75, 1]) {
    const percent = Number(waveLeft(fraction).replace('%', ''));
    const clientX = left + (width * percent) / 100;
    assert.ok(Math.abs(waveFractionAt(clientX, left, width) - fraction) < 1e-9);
  }
});

test('waveTrace keeps one generator for every call site', () => {
  const trace = waveTrace(925, 290, 330);
  // One stroke per sample column, plus the closing column.
  assert.equal(trace.match(/M/g).length, 331);
  assert.ok(trace.startsWith('M0.0 '));
  // Deterministic: the hero capture and the live deck must keep drawing the same shape.
  assert.equal(trace, waveTrace(925, 290, 330));
});
