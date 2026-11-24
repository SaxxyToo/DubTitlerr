import test from 'node:test';
import assert from 'node:assert/strict';
import {
  formatAge,
  formatAgo,
  formatCount,
  formatTime,
  formatWhen,
  relativeTime,
} from '../src/lib/format.js';

test('formatAge renders seconds, minutes, hours, and unknown', () => {
  assert.equal(formatAge(14.2), '14 s');
  assert.equal(formatAge(0), '0 s');
  assert.equal(formatAge(59.9), '59 s');
  assert.equal(formatAge(180), '3 min');
  assert.equal(formatAge(7200), '2 h');
  assert.equal(formatAge(null), 'unknown');
  assert.equal(formatAge(undefined), 'unknown');
  assert.equal(formatAge(NaN), 'unknown');
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

test('formatAgo uses the same words as relativeTime, with an em dash for junk', () => {
  const now = 1_800_000_000;
  assert.equal(formatAgo(now - 30, now), 'just now');
  assert.equal(formatAgo(now - 7200, now), '2 h ago');
  assert.equal(formatAgo(null, now), '—');
  assert.equal(formatAgo(NaN, now), '—');
  // a future stamp (clock skew) is shown as the absolute time, not a negative age
  assert.equal(formatAgo(now + 600, now), formatTime(now + 600));
});

test('formatWhen reads ISO time and carries a timezone', () => {
  const rendered = formatWhen('2026-10-12T14:00:00+00:00');
  assert.ok(rendered.includes('2026'));
  // A tz hint, so a bare "5:01 PM" never sits next to the schedule form's explicit IANA zone.
  assert.match(rendered, /[A-Za-z]{2,}$/);
  assert.equal(formatWhen(null), '—');
  assert.equal(formatWhen(''), '—');
  assert.equal(formatWhen('not a date'), '—');
});

test('formatTime reads epoch seconds and keeps the same shape as formatWhen', () => {
  assert.equal(formatTime(null), '—');
  assert.equal(formatTime(NaN), '—');
  const stamp = Date.UTC(2026, 9, 12, 14, 0, 0) / 1000;
  assert.equal(formatTime(stamp), formatWhen('2026-10-12T14:00:00Z'));
});

test('formatCount groups thousands', () => {
  assert.equal(formatCount(7), '7');
  assert.equal(formatCount(1234), (1234).toLocaleString());
  assert.ok(formatCount(1234567).replace(/\D/g, '').length >= 7);
});
