import { diffWordsWithSpace } from 'diff';

export const riskLabels = {
  words: 'Words added / dropped',
  substitution: 'Word substitution',
  punctuation: 'Punctuation / case',
};
export const verdictLabels = {
  accept: 'Keep repair',
  reject: 'Keep original',
  correct: 'Write correction',
  force: 'Override guard',
};
/**
 * R5: what a repair was anchored on. `ref_class` comes from the server, but the fallback is
 * derived here too so a page talking to an older server still labels every row: a recorded
 * role wins, then the presence of reference TEXT (there was an anchor, we just cannot name
 * its role -- every entry queued before R2), and otherwise no anchor at all.
 */
export type RefClass = 'dub' | 'translation' | 'unanchored' | 'unclassified';
export const refClassLabels: Record<RefClass, string> = {
  dub: 'Dub-anchored',
  translation: 'Translation-anchored',
  unanchored: 'Unanchored',
  unclassified: 'Unclassified',
};
export const refClassHints: Record<RefClass, string> = {
  dub: "Checked against the release's own dub captions, so this wording is near-certain.",
  translation: 'Anchored on a separate translation: a strong hint, but the dub words it differently.',
  unanchored: 'No independent subtitle reference. Nothing downstream can repair this line again.',
  unclassified: 'Queued before reference roles were recorded. The anchor exists; its role is unknown.',
};
// The badge tones app.css already styles, so the chips need no new colour vocabulary:
// near-certain reads as ok, a judgement call as info, no anchor as warn, unknown as muted.
export const refClassTones: Record<RefClass, string> = {
  dub: 'ok',
  translation: 'info',
  unanchored: 'warn',
  unclassified: 'muted',
};
// The sentence under the reference text. An unclassified entry knows it HAS a reference but
// not which track it came from, so it must not be described as a different translation.
export const refClassNotes: Record<RefClass, string> = {
  dub: 'Captions of the dub itself: the closest available record of the dub wording. Follow the audio.',
  translation: 'A different translation, not the answer. Follow the dub audio.',
  unanchored: 'No subtitle reference was kept for this line.',
  unclassified:
    'From a subtitle track queued before its role was recorded. Treat it as a reference: follow the dub audio.',
};
export function refNote(entry: {
  ref_class?: string | null;
  ref_role?: string | null;
  reference?: string | null;
}): string {
  return refClassNotes[refClassOf(entry)];
}
export function refClassOf(entry: {
  ref_class?: string | null;
  ref_role?: string | null;
  reference?: string | null;
}): RefClass {
  const given = entry.ref_class;
  if (given === 'dub' || given === 'translation' || given === 'unanchored' || given === 'unclassified')
    return given;
  const role = (entry.ref_role ?? '').trim().toLowerCase();
  if (role === 'dub' || role === 'translation') return role;
  return (entry.reference ?? '').trim() ? 'unclassified' : 'unanchored';
}
/** The label as a chip shows it: role, plus provenance when the server recorded one. */
export function refChip(entry: {
  ref_class?: string | null;
  ref_role?: string | null;
  reference?: string | null;
  ref_provenance?: string | null;
  ref_track_title?: string | null;
}) {
  const kind = refClassOf(entry);
  const provenance = (entry.ref_provenance ?? '').trim();
  const named = kind === 'dub' || kind === 'translation';
  const label = named && provenance ? `${refClassLabels[kind]} · ${provenance}` : refClassLabels[kind];
  const track = (entry.ref_track_title ?? '').trim();
  return {
    kind,
    label,
    tone: refClassTones[kind],
    title: refClassHints[kind] + (track ? ` (track: ${track})` : ''),
  };
}
export function timecode(seconds: number) {
  const s = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  const mm = Math.floor(s / 60) % 60;
  const ss = String(s % 60).padStart(2, '0');
  return s >= 3600
    ? `${Math.floor(s / 3600)}:${String(mm).padStart(2, '0')}:${ss}`
    : `${Math.floor(s / 60)}:${ss}`;
}
/** Index of the next row after `from` that has no staged verdict; `from` itself when none follows (no wrap). */
export function nextOpenIndex(ids: string[], isStaged: (id: string) => boolean, from: number) {
  for (let i = from + 1; i < ids.length; i++) if (!isStaged(ids[i])) return i;
  return from;
}
export function diffParts(original: string, proposed: string, side: 'original' | 'proposed') {
  return diffWordsWithSpace(original, proposed).filter((p) => (side === 'original' ? !p.added : !p.removed));
}
export function blank(text: string) {
  return !/[^\s\u00ad\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]/u.test(text);
}
/**
 * The risk mix of a queue, in the order the classic review page prints it: counts, not just
 * filters, so an episode whose work is all punctuation reads differently before you start.
 */
export function riskSummary(rows: { risk: string }[]): string {
  const order = ['words', 'substitution', 'punctuation'] as const;
  const labels: Record<string, [string, string]> = {
    words: ['word', 'words'],
    substitution: ['substitution', 'substitutions'],
    punctuation: ['punctuation', 'punctuation'],
  };
  return order
    .map((k) => {
      const n = rows.filter((r) => r.risk === k).length;
      const [one, many] = labels[k];
      return `${n} ${n === 1 ? one : many}`;
    })
    .join(' · ');
}
