// Processing state: the words the Settings panel shows for the persisted running / paused /
// stopped state. Pure, so node:test can cover the wording without a browser.

/** @typedef {{state?: string, by?: string | null, at?: string | null, default?: boolean, error?: string}} Processing */

export const PROCESSING_STATES = /** @type {const} */ (['running', 'paused', 'stopped']);

/** @type {Record<'running' | 'paused' | 'stopped', string>} */
const LABELS = {
  running: 'Running',
  paused: 'Paused',
  stopped: 'Stopped',
};

/**
 * The label, tone and who/when line for one state read. An unknown/missing state is shown as
 * Paused because that is what the server does with it (safe direction: do not touch the library).
 * @param {Processing | null | undefined} info
 */
export function describeProcessing(info) {
  const raw = String(info?.state ?? '');
  const known = PROCESSING_STATES.find((value) => value === raw);
  const state = /** @type {'running' | 'paused' | 'stopped'} */ (known ?? 'paused');
  const who = info?.by ? String(info.by) : '';
  const at = info?.at ? String(info.at) : '';
  let detail;
  if (state === 'running') {
    detail = 'Both loops process the library at their normal cadence.';
    if (who && at) detail += ` Last changed by ${who} at ${at}.`;
  } else if (state === 'stopped') {
    detail = 'The current unit finishes, then the container drains and stops. Start after it comes back.';
    if (who && at) detail += ` Last changed by ${who} at ${at}.`;
  } else if (info?.default || !info) {
    detail =
      'No explicit state yet, so the app boots idle: the review UI and API are up, and nothing touches the library.';
  } else {
    detail = 'Each loop finishes the unit in flight, then holds at its boundary. The UI and API stay up.';
    if (who && at) detail += ` Last changed by ${who} at ${at}.`;
  }
  if (info?.error)
    detail += ` (The state file could not be read: ${info.error}. Paused is the safe default.)`;
  return { state, label: LABELS[state], detail };
}

/**
 * The classic API route for a control. Kept in one place so the panel and any future caller
 * cannot drift onto a path the server does not serve.
 * @param {'start' | 'pause' | 'stop'} action
 */
export function processingActionPath(action) {
  return `/api/system/${action}`;
}
