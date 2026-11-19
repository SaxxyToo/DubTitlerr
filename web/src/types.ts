export type Verdict = 'accept' | 'reject' | 'correct' | 'force';
export type Risk = 'words' | 'substitution' | 'punctuation';
export interface Episode {
  stem: string;
  name: string;
  season: string;
  show: string;
  pending: number;
  admitted: number;
  refused: number;
  oldest_pending_at?: string | null;
}
export interface Entry {
  id: string;
  index: number;
  original_text: string;
  proposed_text: string;
  stage: string;
  reason: string;
  risk: Risk;
  reference?: string;
  // R5. Recorded by repair.py (R2) on new queue entries; absent on legacy ones, where
  // `ref_class` is the normalised label the page chips and filters on.
  ref_role?: 'dub' | 'translation' | null;
  ref_provenance?: string | null;
  ref_track_title?: string | null;
  ref_class?: 'dub' | 'translation' | 'unanchored' | 'unclassified' | null;
  starts: number[];
  context: { before: string[]; after: string[] }[];
  offered: Verdict[];
  permanent: boolean;
  avg_logprob?: number;
}
export interface Shared extends Omit<
  Entry,
  'index' | 'starts' | 'context' | 'stage' | 'reason' | 'permanent'
> {
  show: string;
  episodes: number;
  pair: number;
  // null when the episodes carrying this pair disagree about their anchor.
  ref_class: 'dub' | 'translation' | 'unanchored' | 'unclassified' | null;
}
export interface Draft {
  verdict: Verdict;
  text: string;
  note: string;
}
export interface SaveResult {
  saved: number;
  error?: string;
  warning?: string;
  errors?: { index?: number; id?: string; error: string }[];
}
