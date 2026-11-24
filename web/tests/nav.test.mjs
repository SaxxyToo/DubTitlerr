import test from 'node:test';
import assert from 'node:assert/strict';
import { VIEW_LABELS } from '../src/lib/nav.js';
import { VIEWS } from '../src/lib/route.js';

test('the nav labels cover exactly the views the router knows, in sidebar order', () => {
  assert.deepEqual(Object.keys(VIEW_LABELS), [...VIEWS]);
});

test('every view has a non-empty label', () => {
  for (const [key, label] of Object.entries(VIEW_LABELS)) {
    assert.ok(label.trim().length > 0, `${key} needs a label`);
  }
});
