# Design decisions

Understanding-oriented. The two decisions that are expensive to rediscover, recorded because the
code that carried them no longer explains itself. Each one names the measurement that forced it, so
a future change can be argued against the evidence rather than against a comment.

If you are about to reverse one of these, read the measurement first. Both were made after an
earlier instinct turned out to be wrong.

---

## 1. Idempotency is keyed on two version tiers, not one

**Accepted 2026-08-24.** The constants are `TRANSCRIBE_VERSION` and `TEXT_VERSION` in `dubtitlerr/core/common.py`.

### The problem

A single `PIPELINE_VERSION` made every change equal. A glossary correction that alters only what a
caption *says* invalidated the library exactly as hard as a decoder change, forcing full
re-transcription — GPU-days on a small card. The 2026-08-21 architectural review ranked it the
largest recurring cost in the system.

Two facts sharpened it:

- The raw Whisper word list is not persisted — `generate.py` asks for `word_timestamps=True` and the
  words die after reflow — so changes at the word layer (punctuation, card splitting) genuinely
  cannot be re-run without the GPU.
- `tools/reapply_glossary.py` already re-applied a glossary from `conf.json` with no GPU, no LLM and
  no re-transcription. The cheapest and most frequent case was already solved; nothing recorded that
  it had happened, and nothing ran it automatically.

A live measurement that day found 3,889 videos against 813 stamps — 576 at v4, 236 at v2, 1 at v3 —
so the library had been sitting several versions deep in inconsistency without anything reporting it.

### The decision

Idempotency is keyed on **two** constants, adopted at 4 and 5 so the 576 live v4 stamps read as
transcribe-fresh and text-stale and migrate at watch-gated pace.

The boundary rule: everything computed from `(words, segments, conf.json, fansub, LLM)` is **text**;
everything computed from audio or the decoder is **transcription**. The one genuinely undecidable
case — an operator changing decoder-affecting configuration — has no mechanical signal and is
carried by documentation in both constants' docstrings.

Rejected alternatives:

- **Per-stage versioning**, one constant per stage with a dependency graph. More precise, and the
  precision is not the problem: a wrong edge silently skips a stage that needed to run, which is the
  exact bug class this work exists to remove.
- **One global version plus a manual `--reuse-transcript` flag.** Zero design risk, but it puts the
  invariant in the operator's head. Two silent-degradation bugs in the same review were
  "configuration that looked applied and was not"; this would have been a third by construction.
- **Hashing the glossary file to decide transcription staleness.** Intuitive and wrong: the glossary
  reaches the decoder by exactly one route, `initial_prompt`, while `mine_glossary.py` appends
  `hard_fixes` on every sweep of a watched show. Hashing would re-queue a whole show for the GPU on
  edits that changed nothing about the decoder input. Classification compares the stored prompt
  string instead.

### What it costs

Easier: a glossary or punctuation fix ships for CPU-minutes instead of GPU-hours, which makes
correctness work cheap enough to do. A per-tier staleness count tells you how far behind the part of
the library you are not watching has fallen.

Harder, and paid deliberately: a `words.json` sidecar per episode, roughly 200–300 KB plus segment
records, carrying per-segment `no_speech_prob` and `audio_duration` because neither is recoverable
from the word list. It has to follow the `out_for()` write / raw-path read convention exactly — get
that half-right and the cache misses silently and every episode re-transcribes forever. Two constants
must now be kept honest by hand where one was before, and getting a classification wrong means an
episode is quietly never re-transcribed. The `words_missing` and per-tier counters exist so that
failure is visible within one sweep rather than one month.

This does not converge the library to a uniform state. `watch_queue.py` deliberately never queues
unwatched shows, so the honest invariant is that the library converges **ahead of the viewer**, with
the residual readable as a number.

---

## 2. The `no_speech_prob`-gated hallucination rules are deleted, not tuned

**Accepted 2026-08-24.** See also [ASR bakeoff data](ASR-bakeoff-data.md) for the confidence
measurements this rests on.

### The problem

`hallucination.drop_reason` had three rules and `flag_reason` had two. Two of the five were gated on
`no_speech_prob`:

- `music` — `nsp > 0.95` **and** `avg_logprob < -2.0` → drop the card
- `maybe_silence` — `nsp > 0.5` → keep but mark suspect

`music` caught **zero cards across 859 episodes and 353,879 cards**. On 2026-08-21 that was read as a
bug and the threshold was loosened to 0.90 — then reverted the same day, once a labelled set existed
to judge it against:

```
labels: 207 certain hallucinations (blocklist-matching) vs 57,572 real cards, 136 episodes
nsp separates them (0.796 vs 0.330, AUC 0.929); avg_logprob too (0.913)

nsp>0.70 lp<-0.3 -> recall 82.6%, precision 18.5%, 5.54 false drops per episode
nsp>0.80 lp<-0.3 -> recall 54.6%, precision 19.8%, 3.37
nsp>0.90 lp<-2.0 -> recall  2.9%, precision 19.4%, 0.18   <- the loosened setting
nsp>0.95 lp<-2.0 -> recall  0.0%, precision  0.0%, 0.00   <- as shipped
```

Precision peaks near 20%: four of every five drops would be real dialogue, and a caption that never
covers its line is lost content — the worse failure. So an inert rule was judged strictly better than
any reachable setting, and the rule was kept deliberately unreachable with a comment forbidding a
"fix".

Two further facts changed that.

First, `large-v3-turbo` — the model in production — returns `no_speech_prob` of **exactly 0.0**,
median and max, across 263 segments, with 0 of 267 segments carrying a live value in the production
path. Neither rule can evaluate to true on any input the pipeline actually sees.

Second, and decisively: `large-v3` **does not fix this**. Measured on the same episode, same beam,
card to itself, it produces a live nsp distribution (median 0.397, max 0.963, 6 segments over 0.95)
— and `music_rule_would_fire` is still **0** at beams 3, 4 and 7. None of the segments clearing
`nsp > 0.95` also has `avg_logprob < -2.0`; large-v3's worst 5% of segments sit at −0.445 and the
rule wants −2.0. The two conditions do not co-occur on this content.

### The decision

Delete both rules rather than tune them.

The rule is not dead because the decoder collapsed a signal, which a model change could repair. It is
dead because the conjunction it asks for does not occur in this material, and because every reachable
relaxation of it destroys more real dialogue than it saves. Those are permanent properties of the
content and the measurement, not of the model.

Rejected alternatives:

- **Keep them inert, as before.** Right while "the model collapsed nsp" was the explanation, because
  a better model would have revived them. That explanation is now falsified: a model with a
  perfectly live nsp still never fires the rule. What remains is a rule that cannot fire, evaluated
  on every card, forever.
- **Switch to `large-v3` to revive them.** It buys nothing for the gate — the conjunction stays
  unmet — and costs throughput at matched beam with confidence a wash.
- **Loosen the thresholds.** Refused on the same evidence as 2026-08-21. Nothing measured since has
  improved the precision ceiling.

`low_conf` (`avg_logprob < -0.6`), `blocklist` and `repetition` are **kept**: all three fire on real
content.

### What it costs

Easier: five gated rules become three, and every remaining one demonstrably fires. The liveness
counters stop reporting two rules that evaluate on every card and can never activate — noise in
exactly the instrument built to detect dead rules. `drop_reason` no longer reads `no_speech_prob` at
all, so the gate no longer depends on a field the production decoder does not populate.

Harder, and accepted: the pipeline has no music/silence drop defence. In practice it never had one —
the rule caught zero cards in its entire life — so nothing is lost that was ever gained. `BLOCKLIST`
and `is_repetition` remain the live defences, as they already were.

If a future decoder produces an nsp/logprob pair that *does* co-occur, revisit this from the
measurement above rather than by re-adding the rule from memory: the precision ceiling near 20% is
the thing to beat, not the threshold.

Historical `conf.json` sidecars still carry `flag: "maybe_silence"` from past runs, so
`tools/timing_compare.py` keeps its bucket for that key. Deleting the rule does not retroactively
clean the data it produced.
