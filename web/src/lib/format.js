// Date and age formatting in one place: the dashboard, the audit feed, the runs table and the CLI
// views all read the same helper, so a timestamp never shows two different shapes on one page.
//
// Every absolute time carries a short timezone name, because the schedule form works in explicit
// IANA zones and a bare "5:01 PM" next to "17:01 America/Chicago" reads as a contradiction.

// dateStyle/timeStyle cannot be combined with timeZoneName (ECMA-402), so the fields are explicit.
/** @type {Intl.DateTimeFormatOptions} */
const ABSOLUTE = {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  timeZoneName: 'short',
};

/**
 * Local date and time from an epoch-seconds stamp; '—' when unreadable.
 * @param {number | null | undefined} seconds
 * @returns {string}
 */
export function formatTime(seconds) {
  return typeof seconds === 'number' && Number.isFinite(seconds)
    ? new Date(seconds * 1000).toLocaleString(undefined, ABSOLUTE)
    : '—';
}

/**
 * Local date and time from an ISO string; '—' when missing or unreadable.
 * @param {unknown} iso
 * @returns {string}
 */
export function formatWhen(iso) {
  if (typeof iso !== 'string' || iso === '') return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString(undefined, ABSOLUTE);
}

/**
 * A heartbeat/cache age in seconds as "14 s", "3 min" or "2 h"; anything missing as "unknown".
 * @param {unknown} seconds
 * @returns {string}
 */
export function formatAge(seconds) {
  const s = typeof seconds === 'number' ? seconds : NaN;
  if (!Number.isFinite(s) || s < 0) return 'unknown';
  if (s < 60) return `${Math.floor(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  return `${Math.floor(s / 3600)} h`;
}

/**
 * "just now" / "2 h ago" from an epoch-seconds stamp; '' when unreadable.
 * @param {number} at
 * @param {number} [now] epoch seconds, defaults to this moment
 * @returns {string}
 */
export function relativeTime(at, now = Date.now() / 1000) {
  const t = Number(at);
  if (!Number.isFinite(t) || t <= 0) return '';
  const s = Math.max(0, now - t);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

/**
 * An "… ago" line for a timestamp; '—' when unreadable and the full time when the stamp is in
 * the future (clock skew).
 * @param {number | null | undefined} at epoch seconds
 * @param {number} [now] epoch seconds, defaults to this moment
 * @returns {string}
 */
export function formatAgo(at, now = Date.now() / 1000) {
  if (typeof at !== 'number' || !Number.isFinite(at)) return '—';
  if (at > now) return formatTime(at); // clock skew: an age cannot be negative
  return relativeTime(at, now) || '—';
}

/**
 * A count with locale grouping, so 1234 reads "1,234" everywhere.
 * @param {number} n
 * @returns {string}
 */
export function formatCount(n) {
  return Number(n).toLocaleString();
}
