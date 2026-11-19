/** Pure helpers for the glossary editor page . */

/** @typedef {'names' | 'phrases' | 'hard_fixes'} GlossaryKey */
/** @typedef {'add' | 'update' | 'delete'} GlossaryOp */
/** @typedef {string | [string, string]} GlossaryRow */

// Mirrors review_glossary.py: control, format (zero-width, bidi), surrogate, private-use and line/paragraph separators.
const BAD_CHARS = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Zl}\p{Zp}]/u;
const VALID_KEYS = ['names', 'phrases', 'hard_fixes'];

/**
 * Rows whose term (or, for pairs, either side) contains the query, case-insensitively.
 * An empty query returns a copy of the whole list.
 * @param {GlossaryRow[]} list
 * @param {string} query
 * @returns {GlossaryRow[]}
 */
export function filterTerms(list, query) {
  const q = String(query ?? '')
    .trim()
    .toLowerCase();
  if (!q) return list.slice();
  return list.filter((item) => {
    if (Array.isArray(item))
      return String(item[0]).toLowerCase().includes(q) || String(item[1]).toLowerCase().includes(q);
    return String(item).toLowerCase().includes(q); // a hand-edited file can hold numbers or nulls
  });
}

/**
 * A new array sorted by term, case-insensitively; the input is never mutated.
 * @param {GlossaryRow[]} list
 * @returns {GlossaryRow[]}
 */
export function sortTerms(list) {
  return list.slice().sort((a, b) => {
    const ka = String(Array.isArray(a) ? a[0] : a);
    const kb = String(Array.isArray(b) ? b[0] : b);
    return ka.localeCompare(kb, undefined, { sensitivity: 'base' });
  });
}

/**
 * Mirror of the server validation limits; null when the term is usable.
 * `value` of null/undefined means add (names/phrases) or delete and is not checked.
 * @param {string} key
 * @param {unknown} term
 * @param {unknown} value
 * @returns {string | null}
 */
export function validateTerm(key, term, value) {
  if (!VALID_KEYS.includes(key)) return 'Unknown glossary section.';
  if (typeof term !== 'string') return 'Term must be text.';
  // Trim then NFC, like the server: length is counted on the composed form.
  const t = term.trim().normalize('NFC');
  if (!t) return 'Term must not be empty.';
  if (BAD_CHARS.test(t)) return 'Term must not contain control or invisible characters.';
  // Names and phrases are matched as text; a backslash only makes sense in a hard-fix replacement.
  if (key !== 'hard_fixes' && t.includes('\\')) return 'Term must not contain a backslash.';
  const max = key === 'names' ? 80 : 120;
  if (t.length > max) return `Term must be ${max} characters or fewer.`;
  if (key === 'phrases' && !t.includes(' ')) return 'Phrases need a space; single tokens belong in names.';
  if (value === null || value === undefined) return null;
  if (typeof value !== 'string') return 'Value must be text.';
  const v = value.trim().normalize('NFC');
  if (!v) return 'Value must not be empty.';
  if (BAD_CHARS.test(v)) return 'Value must not contain control or invisible characters.';
  if (key !== 'hard_fixes' && v.includes('\\')) return 'Value must not contain a backslash.';
  if (v.length > max) return `Value must be ${max} characters or fewer.`;
  if (key === 'phrases' && !v.includes(' ')) return 'Phrases need a space; single tokens belong in names.';
  if (key === 'hard_fixes' && v.toLowerCase() === t.toLowerCase())
    return 'Hard-fix key and value must differ.';
  return null;
}

/**
 * Whether the add form or the open edit row holds changes that are not saved yet.
 * @param {{ newTerm: string, newValue: string, editTerm: string | null, editDraft: string, editValue: string, saved: Record<string, string> }} s
 * @returns {boolean}
 */
export function isDirty(s) {
  if (s.newTerm.trim() !== '' || s.newValue.trim() !== '') return true;
  if (s.editTerm === null) return false;
  return s.editDraft !== s.editTerm || s.editValue !== (s.saved[s.editTerm] ?? '');
}

/**
 * The POST /api/glossary body. Delete ignores any value passed.
 * @param {string} show
 * @param {GlossaryOp} op
 * @param {GlossaryKey} key
 * @param {string} term
 * @param {string | null} value
 * @param {string} expected_version
 * @returns {{ show: string, op: GlossaryOp, key: GlossaryKey, term: string, value: string | null, expected_version: string }}
 */
export function buildOp(show, op, key, term, value, expected_version) {
  return { show, op, key, term, value: op === 'delete' ? null : (value ?? null), expected_version };
}

/**
 * One plain sentence for a glossary error response.
 * @param {number} status
 * @param {{ error?: unknown, version?: unknown } | null | undefined} payload
 * @returns {string}
 */
export function describeError(status, payload) {
  const raw = typeof payload?.error === 'string' ? payload.error.trim() : '';
  const detail = raw.replace(/\.+$/, '');
  if (status === 503) return 'glossary busy, try again';
  if (status === 409) return 'Version conflict — reloaded the latest version. Review and try again.';
  if (status === 404) return detail ? `Unknown show: ${detail}.` : 'Unknown show: no such glossary.';
  if (status === 400)
    return detail ? `Invalid request: ${detail}.` : 'Invalid request. Check the term and try again.';
  return detail ? `${detail}.` : `Request failed (${status}).`;
}

// --- the flagged review queue ---------------------------------------------------------------
// The server sends what glossary_acquire.review_items() and accept_effect() produced: `effect` is the
// CLI's own verdict on what accepting would do, so these helpers only label it -- they never re-decide.

/** @typedef {{ term: string, reason?: string, canonical?: string, effect?: string, variant_count?: number, canonical_count?: number, bound?: number, score?: number, context?: string[] }} ReviewItem */

// Fields the card already shows as its heading or as their own evidence row; never repeated as extras.
const EVIDENCE_FIELDS = new Set([
  'term',
  'reason',
  'canonical',
  'effect',
  'context',
  'variant_count',
  'canonical_count',
  'bound',
  'score',
]);

/**
 * Queue entries whose term or canonical contains the query, case-insensitively. Empty query = a copy.
 * @param {ReviewItem[]} items
 * @param {string} query
 * @returns {ReviewItem[]}
 */
export function filterReview(items, query) {
  const list = Array.isArray(items) ? items : [];
  const q = String(query ?? '')
    .trim()
    .toLowerCase();
  if (!q) return list.slice();
  return list.filter(
    (item) =>
      String(item?.term ?? '')
        .toLowerCase()
        .includes(q) ||
      String(item?.canonical ?? '')
        .toLowerCase()
        .includes(q),
  );
}

/**
 * The evidence a reviewer needs, as label/value rows: counts first, the transcript lines next, then
 * every other key the producer attached (bare/possessive/source/scope/...).
 * @param {ReviewItem} item
 * @returns {{ label: string, value: string }[]}
 */
export function evidenceRows(item) {
  /** @type {{ label: string, value: string }[]} */
  const rows = [];
  const it = /** @type {Record<string, any>} */ (item ?? {});
  const shown = (/** @type {unknown} */ value) => typeof value === 'number' && Number.isFinite(value);
  if (shown(it.variant_count)) rows.push({ label: 'seen', value: `${it.variant_count}×` });
  if (shown(it.canonical_count)) rows.push({ label: 'canonical', value: `${it.canonical_count}×` });
  if (shown(it.bound)) rows.push({ label: 'bound', value: it.bound.toFixed(3) });
  if (shown(it.score)) rows.push({ label: 'score', value: it.score.toFixed(3) });
  for (const line of Array.isArray(it.context) ? it.context : [])
    rows.push({ label: 'context', value: String(line) });
  for (const key of Object.keys(it).sort()) {
    if (EVIDENCE_FIELDS.has(key)) continue;
    const value = it[key];
    if (value === null || value === undefined || value === '') continue;
    rows.push({ label: key, value: typeof value === 'string' ? value : JSON.stringify(value) });
  }
  return rows;
}

/**
 * The one decision button this entry offers: what it does and the accept flag to POST.
 * A no-canonical entry has nothing to accept, so (like the CLI's y) it is answered accept=false.
 * @param {ReviewItem} item
 * @returns {{ accept: boolean, label: string }}
 */
export function reviewAction(item) {
  const effect = item?.effect;
  if (!item?.canonical) return { accept: false, label: 'Keep as correct' };
  if (effect === 'hard_fix') return { accept: true, label: 'Accept fix' };
  if (effect === 'additive') return { accept: true, label: 'Add as name' };
  return { accept: true, label: 'Accept' };
}

/**
 * What accepting will change, in the same words the CLI prompt uses. Empty when there is nothing to warn about.
 * @param {ReviewItem} item
 * @returns {string}
 */
export function reviewNote(item) {
  if (!item?.canonical) return '';
  if (item.effect === 'hard_fix') return 'Accepting rewrites every occurrence of this term in the show.';
  if (item.effect === 'additive') return 'Accepting adds the canonical spelling only; the spoken term stays.';
  return '';
}
