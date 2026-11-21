import test from 'node:test';
import assert from 'node:assert/strict';
import { blank, diffParts, nextOpenIndex, refChip, refClassOf, refNote, riskSummary, timecode } from '../src/review.ts';

test('diff preserves the exact original and proposed words including untrusted HTML', () => {
  const original = 'A <script>factory</script> is here.';
  const proposed = 'A needle is here.';
  assert.equal(
    diffParts(original, proposed, 'original')
      .map((p) => p.value)
      .join(''),
    original,
  );
  assert.equal(
    diffParts(original, proposed, 'proposed')
      .map((p) => p.value)
      .join(''),
    proposed,
  );
  assert.ok(diffParts(original, proposed, 'original').some((p) => p.removed));
  assert.ok(diffParts(original, proposed, 'proposed').some((p) => p.added));
});
test('invisible-only corrections are not usable', () => {
  for (const value of ['', '  \n\t', '\u200b\u2060\ufeff']) assert.equal(blank(value), true);
  assert.equal(blank('We need a factory.'), false);
});
test('timestamps are useful seek targets', () => {
  assert.equal(timecode(125.9), '2:05');
  assert.equal(timecode(0), '0:00');
});
test('timecode shows hours from 3600 s and survives bad input', () => {
  assert.equal(timecode(3600), '1:00:00');
  assert.equal(timecode(4500), '1:15:00');
  assert.equal(timecode(3599.9), '59:59');
  assert.equal(timecode(59.9), '0:59');
  assert.equal(timecode(-5), '0:00');
  assert.equal(timecode(NaN), '0:00');
  assert.equal(timecode(Infinity), '0:00');
});
test('nextOpenIndex moves to the next unstaged row and stays put at the end', () => {
  const ids = ['a', 'b', 'c', 'd'];
  const staged = (set) => (id) => set.has(id);
  assert.equal(nextOpenIndex(ids, staged(new Set(['a'])), 0), 1);
  assert.equal(nextOpenIndex(ids, staged(new Set(['a', 'b'])), 0), 2);
  assert.equal(nextOpenIndex(ids, staged(new Set(['a', 'c'])), 0), 1);
  assert.equal(nextOpenIndex(ids, staged(new Set(['c'])), 1), 3, 'skips a later staged row');
  assert.equal(nextOpenIndex(ids, staged(new Set(['c', 'd'])), 2), 2, 'nothing open after: stay');
  assert.equal(nextOpenIndex(ids, staged(new Set(['a'])), 3), 3, 'no wrap to an open row before');
  assert.equal(nextOpenIndex([], staged(new Set()), -1), -1);
});
test('a reference class comes from the server when present and is derived when not', () => {
  assert.equal(refClassOf({ ref_class: 'dub' }), 'dub');
  assert.equal(refClassOf({ ref_class: 'unanchored' }), 'unanchored');
  // The server's normalised value is the answer, even if a stale role disagrees with it.
  assert.equal(refClassOf({ ref_class: 'translation', ref_role: 'dub' }), 'translation');
  // An older server sends no ref_class: the recorded role still names the class.
  assert.equal(refClassOf({ ref_role: 'translation', reference: 'line' }), 'translation');
  assert.equal(refClassOf({ ref_role: 'dub', reference: 'line' }), 'dub');
});
test('legacy entries render as unclassified, not unanchored', () => {
  // Everything queued before the per-card role recording: an anchor exists, its role does not.
  assert.equal(refClassOf({ reference: 'the fansub line' }), 'unclassified');
  assert.equal(refClassOf({ reference: '  ' }), 'unanchored');
  assert.equal(refClassOf({}), 'unanchored');
  const legacy = refChip({ reference: 'the fansub line' });
  assert.equal(legacy.label, 'Unclassified');
  assert.equal(legacy.tone, 'muted');
});
test('a chip names the role, its provenance, and the track behind it', () => {
  const dub = refChip({ ref_class: 'dub', ref_provenance: 'native', ref_track_title: 'English Dub' });
  assert.equal(dub.label, 'Dub-anchored · native');
  assert.equal(dub.tone, 'ok');
  assert.match(dub.title, /dub captions/);
  assert.match(dub.title, /English Dub/);
  const translation = refChip({ ref_class: 'translation', ref_provenance: 'native' });
  assert.equal(translation.label, 'Translation-anchored · native');
  assert.equal(translation.tone, 'info');
  // No provenance recorded: never render a trailing separator.
  assert.equal(refChip({ ref_class: 'dub' }).label, 'Dub-anchored');
  assert.equal(refChip({}).label, 'Unanchored');
  assert.equal(refChip({}).tone, 'warn');
});
test('the note under the reference never claims a role the entry does not have', () => {
  // Dub copy says these are the dub's own words; a translation is not.
  assert.match(refNote({ ref_class: 'dub' }), /dub itself/);
  assert.match(refNote({ ref_class: 'translation' }), /not the answer/);
  // A pre-R2 entry has a reference but no recorded role: it must not be called a translation.
  assert.match(refNote({ reference: 'the fansub line' }), /before its role was recorded/);
  assert.doesNotMatch(refNote({ reference: 'the fansub line' }), /different translation/);
  assert.match(refNote({}), /No subtitle reference/);
});
test('the risk summary counts every row in the queue, in a fixed order', () => {
  const rows = [
    { risk: 'punctuation' },
    { risk: 'words' },
    { risk: 'words' },
    { risk: 'substitution' },
  ];
  assert.equal(riskSummary(rows), '2 words · 1 substitution · 1 punctuation');
  // An empty queue still names the three classes, so the line never reads as partial.
  assert.equal(riskSummary([]), '0 words · 0 substitutions · 0 punctuation');
});
