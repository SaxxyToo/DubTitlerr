import test from 'node:test';
import assert from 'node:assert/strict';
import { describeProcessing, processingActionPath } from '../src/lib/processing.js';

test('every state gets a human label and an explanation', () => {
  assert.equal(describeProcessing({ state: 'running' }).label, 'Running');
  assert.equal(describeProcessing({ state: 'paused' }).label, 'Paused');
  assert.equal(describeProcessing({ state: 'stopped' }).label, 'Stopped');
  for (const state of ['running', 'paused', 'stopped']) {
    assert.match(describeProcessing({ state }).detail, /library|holds|drains/);
  }
});

test('who and when are shown when the file records them', () => {
  const view = describeProcessing({ state: 'paused', by: 'user:alice', at: '2026-11-19T18:02:11Z' });
  assert.match(view.detail, /user:alice/);
  assert.match(view.detail, /2026-11-19T18:02:11Z/);
});

test('a missing state reads as the paused boot default, not as running', () => {
  const view = describeProcessing(null);
  assert.equal(view.state, 'paused');
  assert.match(view.detail, /boots idle/);
});

test('an unknown or corrupt state is shown as paused and says why', () => {
  const view = describeProcessing({ state: 'exploded', error: 'not valid JSON' });
  assert.equal(view.state, 'paused');
  assert.match(view.detail, /not valid JSON/);
});

test('the control paths are the classic system routes', () => {
  assert.equal(processingActionPath('start'), '/api/system/start');
  assert.equal(processingActionPath('pause'), '/api/system/pause');
  assert.equal(processingActionPath('stop'), '/api/system/stop');
});
