// Deterministic dub-audio waveform: dense vertical strokes from an amplitude envelope.
//
// One generator for both drawings: the live Audio Deck (App.svelte) and the frozen hero capture
// (ReviewInbox.svelte, which passes its own pixel-measured width and height). Keeping one copy is
// what stops the two shapes drifting apart; the call sites keep their own dimensions.
//
// The trace is inset from the band's edges by WAVE_INSET percent on each side. The playhead and the
// seek surface share that span through waveFractionAt()/waveLeft(), so a click lands under the
// stroke it looks like it is on.

/** Percent of the band the trace is inset from each side (matches `.band-wave`, app.css). */
export const WAVE_INSET = 4;

/**
 * @param {number} w width
 * @param {number} h height
 * @param {number} n number of sample columns
 * @returns {string} an SVG path `d` attribute
 */
export function waveTrace(w, h, n) {
  const mid = h / 2;
  let d = '';
  for (let i = 0; i <= n; i++) {
    const t = i / n;
    const x = t * w;
    const env =
      0.16 + 0.74 * Math.abs(Math.sin(t * 8.3 + 0.4)) * (0.5 + 0.5 * Math.abs(Math.sin(t * 1.9 + 0.8)));
    const j1 = 0.42 + 0.58 * Math.abs(Math.sin(i * 12.9898 + 4.1));
    const j2 = 0.42 + 0.58 * Math.abs(Math.sin(i * 7.233 + 1.7));
    const top = mid - env * j1 * mid * 0.98;
    const bot = mid + env * j2 * mid * 0.98;
    d += `M${x.toFixed(1)} ${top.toFixed(1)}L${x.toFixed(1)} ${bot.toFixed(1)}`;
  }
  return d;
}

/**
 * A pointer x (client pixels) as a 0..1 position along the trace span. Values outside the span
 * clamp to the near end, so a drag that leaves the band still lands on an edge instead of wrapping.
 * @param {number} clientX
 * @param {number} left band's left edge, client pixels
 * @param {number} width band's width in pixels
 * @returns {number}
 */
export function waveFractionAt(clientX, left, width) {
  const span = (width * (100 - WAVE_INSET * 2)) / 100;
  if (!(span > 0)) return 0;
  const t = (clientX - (left + (width * WAVE_INSET) / 100)) / span;
  return Math.min(1, Math.max(0, t));
}

/**
 * The playhead's `left` for a 0..1 position, as a CSS percentage of the band. Both the playhead and
 * the seek surface start at WAVE_INSET and span the trace, never the band's full width.
 * @param {number} fraction
 * @returns {string}
 */
export function waveLeft(fraction) {
  const t = Number.isFinite(fraction) ? Math.min(1, Math.max(0, fraction)) : 0;
  return `${(WAVE_INSET + (100 - WAVE_INSET * 2) * t).toFixed(3)}%`;
}
