import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildOp,
  describeError,
  evidenceRows,
  filterReview,
  filterTerms,
  isDirty,
  reviewAction,
  reviewNote,
  sortTerms,
  validateTerm,
} from '../src/lib/glossary.js';
import { ApiError } from '../src/lib/api.js';

test('filterTerms returns a copy of the whole list when the query is empty', () => {
  const list = ['Fixer', 'Needle'];
  const out = filterTerms(list, '');
  assert.deepEqual(out, list);
  assert.notEqual(out, list);
});

test('filterTerms matches case-insensitively on substrings', () => {
  assert.deepEqual(filterTerms(['Fixer Upper', 'Needle', 'fixation'], 'FIX'), ['Fixer Upper', 'fixation']);
  assert.deepEqual(filterTerms(['Fixer', 'Needle'], 'zzz'), []);
});

test('filterTerms matches hard_fixes entries on either side of the pair', () => {
  const entries = [
    ['asce', 'ace'],
    ['marimo', 'marimo'],
  ];
  assert.deepEqual(filterTerms(entries, 'asc'), [['asce', 'ace']]);
  assert.deepEqual(filterTerms(entries, 'ACE'), [['asce', 'ace']]);
  assert.deepEqual(filterTerms(entries, ''), entries);
});

test('sortTerms orders case-insensitively without mutating', () => {
  const list = ['Zulu', 'apple', 'Fixer'];
  const out = sortTerms(list);
  assert.deepEqual(out, ['apple', 'Fixer', 'Zulu']);
  assert.deepEqual(list, ['Zulu', 'apple', 'Fixer']);
  assert.deepEqual(
    sortTerms([
      ['zulu', 'z'],
      ['Apple', 'a'],
    ]),
    [
      ['Apple', 'a'],
      ['zulu', 'z'],
    ],
  );
});

test('validateTerm accepts good names and phrases', () => {
  assert.equal(validateTerm('names', 'Fixer Upper', null), null);
  assert.equal(validateTerm('phrases', 'Fixer Upper', null), null);
  assert.equal(validateTerm('names', 'Fixer', 'Fixed'), null);
  assert.equal(validateTerm('hard_fixes', 'asce', 'ace'), null);
  assert.equal(validateTerm('hard_fixes', 'marimo', null), null);
});

test('validateTerm rejects empty, non-string, and control-character terms', () => {
  for (const bad of ['', '   ', null, 5, 'has\x07bell']) {
    assert.ok(typeof validateTerm('names', bad, null) === 'string', JSON.stringify(bad));
  }
});

test('validateTerm enforces the 80/120 limits and the phrases space rule', () => {
  assert.ok(validateTerm('names', 'x'.repeat(81), null));
  assert.equal(validateTerm('names', 'x'.repeat(80), null), null);
  assert.ok(validateTerm('phrases', 'x'.repeat(121), null));
  assert.ok(validateTerm('phrases', 'SingleToken', null), 'single tokens belong in names');
  assert.equal(validateTerm('phrases', 'x'.repeat(118) + ' y', null), null);
  assert.ok(validateTerm('hard_fixes', 'x'.repeat(121), 'y'));
});

test('validateTerm rejects hard_fixes keys equal to their value and unknown keys', () => {
  assert.ok(validateTerm('hard_fixes', 'Ace', 'aCE'), 'key unequal to value case-insensitively');
  assert.ok(validateTerm('nope', 'term', null));
});

test('buildOp returns the POST body with expected_version', () => {
  assert.deepEqual(buildOp('One Pace', 'add', 'names', 'Fixer', null, 'v1'), {
    show: 'One Pace',
    op: 'add',
    key: 'names',
    term: 'Fixer',
    value: null,
    expected_version: 'v1',
  });
  assert.deepEqual(buildOp('One Pace', 'update', 'names', 'Fixer', 'Fixed', 'v1').value, 'Fixed');
  assert.deepEqual(buildOp('One Pace', 'update', 'hard_fixes', 'asce', 'ace', 'v9'), {
    show: 'One Pace',
    op: 'update',
    key: 'hard_fixes',
    term: 'asce',
    value: 'ace',
    expected_version: 'v9',
  });
  const del = buildOp('One Pace', 'delete', 'phrases', 'old phrase', 'ignored', 'v2');
  assert.equal(del.term, 'old phrase');
  assert.equal(del.value, null);
});

test('describeError says busy, conflict, unknown show, and invalid in plain sentences', () => {
  assert.equal(describeError(503, {}), 'Glossary busy; try again.');
  assert.match(describeError(409, { version: 'abc' }), /conflict/i);
  assert.match(describeError(404, { error: 'no such glossary' }), /no such glossary/);
  assert.match(describeError(400, { error: 'too long' }), /too long/);
  assert.equal(typeof describeError(500, {}), 'string');
});

// Client validation mirrors review_glossary.py: NFC-normalize, then refuse control, format, surrogate,
// private-use and line/paragraph separator characters. A plain space and NBSP (Zs) stay allowed.
test('validateTerm measures length after NFC, like the server', () => {
  const decomposed = 'e\u0301'.repeat(41); // 82 UTF-16 units decomposed, 41 once composed
  assert.equal(decomposed.length, 82);
  assert.equal(validateTerm('names', decomposed, null), null, 'composed it is 41 characters');
  assert.ok(validateTerm('names', 'e\u0301'.repeat(81), null), 'still too long once composed');
  assert.equal(validateTerm('hard_fixes', 'caf\u00e9', 'Cafe\u0301s'), null);
});

test('validateTerm refuses invisible and separator characters in terms and values', () => {
  const bad = {
    ZWSP: '\u200b',
    RLO: '\u202e',
    BOM: '\ufeff',
    LS: '\u2028',
    PS: '\u2029',
    private: '\ue000',
    surrogate: '\ud800',
    NEL: '\u0085',
    DEL: '\u007f',
  };
  for (const [name, ch] of Object.entries(bad)) {
    assert.ok(validateTerm('names', `Na${ch}mi`, null), `term ${name}`);
    assert.ok(validateTerm('hard_fixes', 'zorro', `Zo${ch}ro`), `value ${name}`);
  }
});

test('validateTerm still allows ordinary spaces and no-break spaces', () => {
  assert.equal(validateTerm('phrases', 'Going Merry', null), null);
  assert.equal(validateTerm('names', 'Monkey\u00a0D', null), null);
});

test('isDirty is true only when the add form or the open edit row differs from what is saved', () => {
  const clean = { newTerm: '', newValue: '', editTerm: null, editDraft: '', editValue: '', saved: {} };
  assert.equal(isDirty(clean), false);
  assert.equal(isDirty({ ...clean, newTerm: '  ' }), false, 'whitespace is not an edit');
  assert.equal(isDirty({ ...clean, newTerm: 'Nami' }), true);
  assert.equal(isDirty({ ...clean, newValue: 'x' }), true);
  assert.equal(isDirty({ ...clean, editTerm: 'Zoro', editDraft: 'Zoro' }), false, 'opened but unchanged');
  assert.equal(isDirty({ ...clean, editTerm: 'Zoro', editDraft: 'Roronoa' }), true);
  assert.equal(
    isDirty({ ...clean, editTerm: 'lufi', editDraft: 'lufi', editValue: 'Luffy', saved: { lufi: 'Luffy' } }),
    false,
  );
  assert.equal(
    isDirty({
      ...clean,
      editTerm: 'lufi',
      editDraft: 'lufi',
      editValue: 'Luffy-san',
      saved: { lufi: 'Luffy' },
    }),
    true,
  );
});

test('an ApiError keeps its HTTP status and payload so describeError can use them', () => {
  const cases = [
    [503, { error: 'glossary busy' }, /busy/],
    [409, { error: 'version conflict', version: 'abc' }, /conflict/i],
    [404, { error: 'no such glossary' }, /no such glossary/],
    [400, { error: 'term already exists' }, /already exists/],
  ];
  for (const [status, payload, expected] of cases) {
    const exc = new ApiError(payload.error, status, payload);
    assert.ok(exc instanceof Error);
    assert.equal(exc.status, status);
    assert.deepEqual(exc.payload, payload);
    assert.match(describeError(exc.status, exc.payload), expected);
  }
});

test('validateTerm refuses a backslash in names and phrases but allows it in hard-fix values', () => {
  assert.ok(validateTerm('names', 'AC\\DC', null));
  assert.ok(validateTerm('names', 'Zoro', 'Zo\\ro'));
  assert.ok(validateTerm('phrases', 'AC\\DC band', null));
  assert.equal(validateTerm('hard_fixes', 'acdc band', 'AC\\DC'), null);
});

test('filterTerms and sortTerms tolerate duplicates and non-string entries from a hand-edited file', () => {
  const messy = ['Luffy', 'Luffy', 5, null, 'zoro'];
  assert.deepEqual(filterTerms(messy, 'luf'), ['Luffy', 'Luffy']);
  assert.equal(filterTerms(messy, '5').length, 1, 'a number is matched by its text');
  const sorted = sortTerms(messy);
  assert.equal(sorted.length, 5);
  assert.deepEqual(messy, ['Luffy', 'Luffy', 5, null, 'zoro'], 'the input is not mutated');
});

// --- the flagged terms ---------------------------------------------------------------

const ITEM = {
  term: 'Usop',
  reason: 'respelling-needs-review',
  canonical: 'Usopp',
  variant_count: 9,
  canonical_count: 3,
  bound: 0.72,
  score: 0.91,
  context: ['Usop, wait!'],
  source: 'transcript',
  scope: 12,
  effect: 'hard_fix',
};

test('filterReview matches the term or the canonical, case-insensitively', () => {
  const items = [ITEM, { term: 'Straw', reason: 'common-word', canonical: 'Straw Hat Pirates' }];
  assert.deepEqual(filterReview(items, 'usop'), [ITEM]);
  assert.deepEqual(filterReview(items, 'PIRATES'), [items[1]]);
  assert.deepEqual(filterReview(items, 'zzz'), []);
  const copy = filterReview(items, '');
  assert.deepEqual(copy, items);
  assert.notEqual(copy, items, 'the caller gets a copy');
});

test('filterReview tolerates hand-edited non-string fields', () => {
  assert.deepEqual(filterReview([{ term: 5, canonical: null }], '5').length, 1);
  assert.deepEqual(filterReview([{ term: 'x' }], 'y'), []);
});

test('evidenceRows leads with the counts and never repeats the reason or canonical', () => {
  const rows = evidenceRows(ITEM);
  const labels = rows.map((r) => r.label);
  assert.deepEqual(labels.slice(0, 4), ['seen', 'canonical', 'bound', 'score']);
  assert.equal(rows[0].value, '9×');
  assert.equal(rows[1].value, '3×');
  assert.equal(rows[2].value, '0.720');
  assert.equal(rows[3].value, '0.910');
  assert.ok(!labels.includes('reason') && !labels.includes('canonical spelling'));
  assert.ok(labels.includes('context'), 'the transcript line the flag was raised on is shown');
});

test('evidenceRows carries every extra evidence key the producer attached', () => {
  const labels = evidenceRows(ITEM).map((r) => r.label);
  for (const key of ['source', 'scope']) assert.ok(labels.includes(key), key);
  const bare = evidenceRows({ term: 'Dwarfs', reason: 'low-confidence', canonical: '', bare: 'Dwarfs', possessive: "Dwarfs'" });
  assert.deepEqual(
    bare.filter((r) => r.label === 'bare' || r.label === 'possessive').map((r) => r.value),
    ['Dwarfs', "Dwarfs'"],
  );
});

test('reviewAction says what the button does, in the CLI’s terms', () => {
  assert.deepEqual(reviewAction({ ...ITEM, effect: 'hard_fix' }), { accept: true, label: 'Accept fix' });
  assert.deepEqual(reviewAction({ ...ITEM, effect: 'additive' }), { accept: true, label: 'Add as name' });
  assert.deepEqual(reviewAction({ ...ITEM, effect: 'refused' }), { accept: true, label: 'Accept' });
  assert.deepEqual(reviewAction({ term: 'Frier', reason: 'no-wiki-match', canonical: '', effect: 'none' }), {
    accept: false,
    label: 'Keep as correct',
  });
});

test('reviewNote warns only where accepting rewrites or adds something', () => {
  assert.match(reviewNote({ ...ITEM, effect: 'hard_fix' }), /every occurrence/i);
  assert.match(reviewNote({ ...ITEM, effect: 'additive' }), /adds the canonical/i);
  assert.equal(reviewNote({ ...ITEM, effect: 'refused' }), '');
  assert.equal(reviewNote({ term: 'Frier', canonical: '', effect: 'none' }), '');
});
