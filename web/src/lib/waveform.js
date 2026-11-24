// Deterministic dub-audio waveform: dense vertical strokes from an amplitude envelope.
//
// One generator for both drawings: the live Audio Deck (App.svelte) and the frozen hero capture
// (ReviewInbox.svelte, which passes its own pixel-measured width and height). Keeping one copy is
// what stops the two shapes drifting apart; the call sites keep their own dimensions.

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
