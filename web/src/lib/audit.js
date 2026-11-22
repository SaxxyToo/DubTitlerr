// Audit-first surface helpers (P33). Pure, so node:test can import them without a browser.
//
// The review workspace used to open on a pending gate with no record of what the app had
// already decided. The audit view is the default now: most of the daily volume is the app's
// own decisions (P32 dismisses noise, applies a corroborated respelling; P33 expires a stale
// item), and the pending queues are the exception list. These helpers keep the wording and the
// counting in one place so the page and its tests cannot disagree.

/** @typedef {{ id: string, kind: string, decision: string, at: number, show?: string, term?: string, reason?: string, guard?: string | null, original_text?: string, detail?: Record<string, any>, undoable?: boolean, stem?: string }} AuditItem */

/** @type {Record<string, string>} */
export const DECISION_LABELS = {
  dismiss: 'Dismissed',
  apply: 'Applied',
  expire: 'Expired',
  'queue-bound': 'Capped',
  restore: 'Restored',
};

/** Tone tokens app.css already styles; an unknown decision reads as muted, never as success. @type {Record<string, string>} */
export const DECISION_TONES = {
  apply: 'ok',
  dismiss: 'info',
  expire: 'muted',
  'queue-bound': 'warn',
  restore: 'info',
};

/** @param {string | null | undefined} decision */
export function decisionLabel(decision) {
  return DECISION_LABELS[String(decision ?? '')] ?? String(decision ?? 'Decision');
}

/** @param {string | null | undefined} decision */
export function decisionTone(decision) {
  return DECISION_TONES[String(decision ?? '')] ?? 'muted';
}

/** "12 dismissed · 3 applied · 1 expired", in a stable order with zero counts omitted. @param {Record<string, number> | null | undefined} counts */
export function auditSummary(counts) {
  const order = ['apply', 'dismiss', 'expire', 'queue-bound', 'restore'];
  const parts = [];
  for (const key of order) {
    const n = Number(counts?.[key] ?? 0);
    if (n > 0) parts.push(`${n} ${decisionLabel(key).toLowerCase()}`);
  }
  for (const [key, value] of Object.entries(counts ?? {})) {
    if (!order.includes(key) && Number(value) > 0) parts.push(`${Number(value)} ${String(key)}`);
  }
  return parts.length ? parts.join(' · ') : 'No automatic decisions recorded yet';
}

/** "2 h ago" from an epoch-seconds stamp; "just now" under a minute; '' when unreadable. @param {number} at @param {number} [now] */
export function relativeTime(at, now = Date.now() / 1000) {
  const t = Number(at);
  if (!Number.isFinite(t) || t <= 0) return '';
  const s = Math.max(0, now - t);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

/** The one-line "why" for a feed row: what the policy guarded against, or what it applied. @param {AuditItem} item */
export function decisionReason(item) {
  const detail = item?.detail ?? {};
  if (item?.decision === 'apply') return detail.applied ? `Wrote ${detail.applied}` : 'Applied';
  if (item?.guard) return `Guard: ${item.guard}`;
  if (item?.term) return `Term: ${item.term}`;
  if (item?.original_text) return item.original_text;
  return item?.reason ?? '';
}

/** What a row is about, in a reviewer's words. @param {AuditItem} item */
export function decisionTitle(item) {
  if (item?.term) return item.term;
  if (item?.reason) return item.reason;
  if (item?.stem) return item.stem.split('/').pop() ?? item.stem;
  return 'Queue item';
}

/** @param {AuditItem} item */
export function isUndoable(item) {
  return item?.undoable === true;
}

/** Queue health rows, biggest first; `escalate` marks the classes that needed the owner. @param {any} queues */
export function classRows(queues) {
  const byClass = queues?.glossary?.by_class ?? {};
  return Object.entries(byClass)
    .map(([cls, count]) => ({ cls, count: Number(count) || 0 }))
    .sort((a, b) => b.count - a.count || a.cls.localeCompare(b.cls));
}

/** The whole queue section's claim, backed by the numbers the server returned. @param {any} queues */
export function queueHeadline(queues) {
  const q = queues?.glossary ?? {};
  const queued = Number(q.queued ?? 0);
  const expired = Number(q.expired ?? 0);
  const days = Number(queues?.expiry_days ?? 14);
  const max = Number(queues?.max ?? 0);
  return {
    queued,
    expired,
    max,
    note: `Anything untouched for ${days} days is dismissed automatically; a queue is capped at ${max}. ${expired} dismissed so far.`,
  };
}
