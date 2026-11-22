import test from 'node:test';
import assert from 'node:assert/strict';
import {
  auditSummary,
  classRows,
  decisionLabel,
  decisionReason,
  decisionTitle,
  decisionTone,
  isUndoable,
  queueHeadline,
  relativeTime,
} from '../src/lib/audit.js';

test('decisionLabel and decisionTone cover every decision the server emits', () => {
  assert.equal(decisionLabel('dismiss'), 'Dismissed');
  assert.equal(decisionLabel('apply'), 'Applied');
  assert.equal(decisionLabel('expire'), 'Expired');
  assert.equal(decisionLabel('queue-bound'), 'Capped');
  assert.equal(decisionLabel('restore'), 'Restored');
  // an unknown decision must never read as success
  assert.equal(decisionTone('mystery'), 'muted');
  assert.equal(decisionTone('apply'), 'ok');
});

test('auditSummary counts what happened, omitting zeros', () => {
  assert.equal(auditSummary({ dismiss: 12, apply: 3, expire: 0 }), '3 applied · 12 dismissed');
  assert.equal(auditSummary({}), 'No automatic decisions recorded yet');
  assert.equal(auditSummary({ apply: 1, 'custom-class': 2 }), '1 applied · 2 custom-class');
});

test('relativeTime says how old a decision is, and nothing when unreadable', () => {
  const now = 1_800_000_000;
  assert.equal(relativeTime(now - 30, now), 'just now');
  assert.equal(relativeTime(now - 300, now), '5 min ago');
  assert.equal(relativeTime(now - 7200, now), '2 h ago');
  assert.equal(relativeTime(now - 3 * 86400, now), '3 d ago');
  assert.equal(relativeTime(null, now), '');
  assert.equal(relativeTime(NaN, now), '');
});

test('a row says what it decided and why', () => {
  const applied = {
    decision: 'apply',
    detail: { applied: 'hard_fix' },
    term: 'Frieren',
    reason: 'respelling-needs-review',
  };
  assert.equal(decisionTitle(applied), 'Frieren');
  assert.equal(decisionReason(applied), 'Wrote hard_fix');
  const expired = {
    decision: 'expire',
    detail: {},
    guard: 'queue-expiry',
    term: '',
    reason: 'unseen-needs-evidence',
  };
  assert.equal(decisionReason(expired), 'Guard: queue-expiry');
  assert.equal(decisionTitle(expired), 'unseen-needs-evidence');
  const queue = {
    decision: 'expiry',
    detail: {},
    original_text: 'the long line',
    reason: 'repair/decision_unfittable',
  };
  assert.equal(decisionReason(queue), 'the long line');
});

test('only a reversible row offers undo', () => {
  assert.equal(isUndoable({ undoable: true }), true);
  assert.equal(isUndoable({ undoable: false }), false);
  assert.equal(isUndoable({}), false);
});

test('classRows sorts the queue health biggest first', () => {
  const rows = classRows({
    glossary: { by_class: { 'unseen-needs-evidence': 4, 'english-word': 12 } },
  });
  assert.deepEqual(rows, [
    { cls: 'english-word', count: 12 },
    { cls: 'unseen-needs-evidence', count: 4 },
  ]);
  assert.deepEqual(classRows(null), []);
});

test('queueHeadline states the policy the numbers actually follow', () => {
  const h = queueHeadline({ glossary: { queued: 7, expired: 41 }, expiry_days: 14, max: 200 });
  assert.equal(h.queued, 7);
  assert.equal(h.expired, 41);
  assert.match(h.note, /14 days/);
  assert.match(h.note, /capped at 200/);
});
