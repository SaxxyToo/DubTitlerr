import test from 'node:test';
import assert from 'node:assert/strict';
import {
  CLEAR,
  blankNotification,
  describeNotificationResult,
  isDirty,
  notificationTestable,
  notificationsBody,
  notificationsChanged,
  secretFieldFor,
  toggleTrigger,
  withNotificationKind,
} from '../src/lib/settingsModel.js';

const RULES = {
  kinds: ['discord', 'ntfy', 'webhook'],
  triggers: ['run.requested', 'sweep.completed', 'merge.completed', 'pass.failed', 'review.pending', 'episode.delivered'],
  fields: {
    discord: { visible: ['link_url'], secret: ['url'] },
    ntfy: { visible: ['server', 'topic'], secret: ['token'] },
    webhook: { visible: [], secret: ['url'] },
  },
};
const LOADED = [
  { name: 'chat', kind: 'discord', enabled: true, triggers: ['sweep.completed'], url: { set: true } },
  {
    name: 'phone',
    kind: 'ntfy',
    enabled: false,
    triggers: ['pass.failed'],
    server: 'https://ntfy.sh',
    topic: 'dub',
    token: { set: false },
  },
];

test('a new notification is Discord, enabled, with every trigger except the per-episode one', () => {
  const blank = blankNotification(RULES);
  assert.equal(blank.kind, 'discord');
  assert.equal(blank.enabled, true);
  assert.deepEqual(blank.triggers, ['run.requested', 'sweep.completed', 'merge.completed', 'pass.failed', 'review.pending']);
  assert.deepEqual(blank.url, { set: false });
  assert.equal(blank.link_url, '');
});

test('each kind has exactly one write-only secret field', () => {
  assert.equal(secretFieldFor('discord'), 'url');
  assert.equal(secretFieldFor('webhook'), 'url');
  assert.equal(secretFieldFor('ntfy'), 'token');
});

test('switching kind drops the old kind field and reports no stored secret', () => {
  const ntfy = withNotificationKind({ ...LOADED[1], triggers: ['pass.failed'] }, 'discord');
  assert.equal(ntfy.kind, 'discord');
  assert.equal('server' in ntfy, false);
  assert.equal('token' in ntfy, false);
  assert.deepEqual(ntfy.url, { set: false });
  assert.equal(ntfy.link_url, '');
  assert.equal(ntfy.enabled, false);
});

test('the body sends visible fields always and a secret only when it was touched', () => {
  const body = notificationsBody([
    { ...LOADED[0] },
    { ...LOADED[1], secret: 'tok-123' },
    { ...LOADED[1], name: 'phone2', secret: CLEAR },
  ]);
  assert.deepEqual(body[0], { name: 'chat', kind: 'discord', enabled: true, triggers: ['sweep.completed'] });
  assert.equal(body[1].token, 'tok-123');
  assert.equal(body[1].server, 'https://ntfy.sh');
  assert.deepEqual(body[2].token, { clear: true });
  assert.equal('secret' in body[1], false);
  assert.equal('url' in body[0], false, 'the stored URL marker is never echoed back');
});

test('a discord link_url travels in the body and reads as a change and an untestable edit', () => {
  const withLink = { ...LOADED[0], link_url: 'https://dub.example.test/review' };
  assert.equal(notificationsBody([withLink])[0].link_url, 'https://dub.example.test/review');
  assert.equal(notificationsChanged(LOADED, [withLink, LOADED[1]]), true);
  assert.equal(notificationTestable(withLink, LOADED.map((p) => ({ ...p }))), false);
  assert.equal(notificationTestable({ ...LOADED[0] }, LOADED.map((p) => ({ ...p }))), true);
});

test('changed sees an edited field, a typed secret or a removed provider', () => {
  const same = LOADED.map((p) => ({ ...p }));
  assert.equal(notificationsChanged(LOADED, same), false);
  assert.equal(notificationsChanged(LOADED, [{ ...LOADED[0], enabled: false }, LOADED[1]]), true);
  assert.equal(notificationsChanged(LOADED, [{ ...LOADED[0], triggers: ['merge.completed'] }, LOADED[1]]), true);
  assert.equal(notificationsChanged(LOADED, [{ ...LOADED[0], secret: 'x' }, LOADED[1]]), true);
  assert.equal(notificationsChanged(LOADED, [LOADED[0]]), true);
});

test('a provider is testable only as the server holds it', () => {
  const saved = LOADED.map((p) => ({ ...p }));
  assert.equal(notificationTestable({ ...LOADED[1] }, saved), true);
  assert.equal(notificationTestable({ ...LOADED[1], topic: 'other' }, saved), false);
  assert.equal(notificationTestable({ ...LOADED[1], kind: 'discord' }, saved), false);
  assert.equal(notificationTestable({ ...LOADED[1], secret: 'typed' }, saved), false);
  assert.equal(notificationTestable({ name: 'new', kind: 'discord' }, saved), false);
});

test('triggers toggle without mutating the list', () => {
  const on = ['sweep.completed'];
  assert.deepEqual(toggleTrigger(on, 'pass.failed'), ['sweep.completed', 'pass.failed']);
  assert.deepEqual(toggleTrigger(on, 'sweep.completed'), []);
  assert.deepEqual(on, ['sweep.completed']);
});

test('a test result reads as one plain sentence', () => {
  assert.equal(describeNotificationResult({ ok: true, error: '' }), 'Test notification sent.');
  assert.equal(describeNotificationResult({ ok: false, error: 'HTTP 404' }), 'The test failed: HTTP 404');
  assert.equal(describeNotificationResult({ ok: false, error: '' }), 'The test failed.');
  assert.equal(describeNotificationResult(null), 'The test failed.');
});

test('notification changes count as an unsaved edit', () => {
  assert.equal(isDirty({}, null, null, null), false);
  assert.equal(isDirty({}, null, null, [LOADED[0]]), true);
});
