# whisper.cpp sidecar — turbo vs large-v3 (2026-11-18)

Evidence for the spec at `docs/specs/whispercpp-asr/spec.md`. **Metrics only** — the raw
report this was built from had its transcripts stripped by `--metrics-only` before it left
the machine, because a full report carries a commercial dub's script.

## What ran

`tools/asr_bakeoff.py` driving the **live omarchy endpoint** (`http://omarchy:11434`),
through production's own `generate.endpoint_transcribe` — silence-chunking and the 32 kbps
mp3 re-encode included. Two entrants, the same server, different `model` names:

| Entrant | Server config |
|---|---|
| `whisper-large-v3-turbo-subs` | `ggml-large-v3-turbo.bin`, bs 7 / bo 7 / nth 0.9, current production |
| `whisper-large-v3-subs` | `ggml-large-v3.bin`, the same flags **plus `--logprob-thold -2.0`** |

Hardware: **RTX 5060**, driver 615.78.08, sm_120. Entrants run strictly sequentially.

## The reference is a real dub script, not a proxy

Four of the five episodes carry an embedded subtitle track that is the **English dub
script** — the library labels them (`Dub`, `English (Dubtitle)`, `English (SDH Dubtitle)`)
and 170 episodes across it have one. So the WER below is a genuine ground-truth number. The
fifth (Attack on Titan S01E01) has no such track; it covers the fansub axis and reports no
WER. This matters: a sub track would measure dub-vs-sub localisation, not transcription.

**The spread** (AGENTS.md asks for one with a fansub track and one without, one glossary
acquired and one only mined): Edgerunners (Netflix, official subs only), Death Note, Vinland
Saga, FMA Brotherhood (long-runner, official), plus AOT (fansub groups: MTBB / Commie /
Coalgirls / FFF). Note a real constraint this exposed: **no episode that has a dub reference
also has a fansub track** — the two axes do not overlap in this library, so the fansub
episode cannot carry a WER number.

## Results

| Episode | turbo WER | large-v3 WER | Δ | turbo wall | large-v3 wall | ratio |
|---|---|---|---|---|---|---|
| Cyberpunk Edgerunners S01E01 | 16.66% | **16.21%** | −0.45 | 42.8 s | 63.6 s | 1.49× |
| Death Note S01E01 | **10.12%** | 10.24% | +0.12 | 30.7 s | 52.3 s | 1.70× |
| Vinland Saga S01E01 | 25.37% | **21.71%** | −3.66 | 37.0 s | 58.1 s | 1.57× |
| FMA Brotherhood S01E02 | 29.56% | **23.87%** | −5.69 | 42.4 s | 67.9 s | 1.60× |
| Attack on Titan S01E01 | — | — | — | 58.0 s | 63.2 s | 1.09× |
| **mean of the four** | **20.43%** | **18.01%** | **−2.42** | 3.5 min | 5.1 min | **1.46×** |

large-v3 is better on three of four, by a mean of **2.42 pp**, and worse on Death Note by
**0.12 pp** — inside the +0.5 pp tolerance. Cross-entrant agreement 10.90%, i.e. the two
models genuinely disagree on about a tenth of the words; they are not near-duplicates.

## The thing the change is actually for

| | turbo | large-v3 |
|---|---|---|
| `nsp_alive_frac` | **0.0005** | **0.166** |
| `nsp_max` | 1.9e-05 | **0.9248** |
| segments with nsp > 0.5 | **0** | **12** |

turbo cannot clear the guard on any segment: `0.05%` alive, nothing above 0.5. large-v3
reaches 12 segments. `repair.py`'s music guard (`no_speech_prob > NSP_MAX`) moves from
provably inert to firing — that is the point of the change, and it is a measurement, not an
inference. The stricter `music_rule_would_fire` (needs nsp > 0.95 **and** lp < −2.0 in one
segment) stays at 0 for both, because large-v3's peak is 0.9248.

## Timing pass (T1.5b) — criterion 4

`tools/timing_compare.py` over both configs' generated output (staged into one tree per
config: symlinked videos plus that config's sidecars, so the tool can pair them). GPU-free.
Four episodes analysed per config; one has no usable reference track.

| Metric | turbo | large-v3 | Δ |
|---|---|---|---|
| `pct_cards_on_cue` | 91.79% | **91.57%** | −0.22 pp |
| `kept_in_gap` | 124 | 128 | +4 |
| `in_gap_speech` | 103 | 98 | −5 |
| `in_gap_silent` | 21 | 30 | +9 |
| `false_in_gap_rate` | 6.82% | **6.45%** | −0.37 pp |

**Criterion 4 passes on the half that can be measured**: on-cue fraction is 0.22 pp lower,
well inside the 2 pp tolerance, and `false_in_gap_rate` — the share of kept cards landing in a
subtitle gap with no speech in it, i.e. hallucinated into silence — is slightly *better* on
large-v3.

**Two caveats, both about the criterion rather than the result:**

1. **The criterion's second half cannot be evaluated.** It asks for "median absolute onset
   error ≤ +50 ms", and `timing_compare` does not produce that metric — it fits a RANSAC
   offset+drift and reports the on-cue classification, with no median onset-error field in its
   report. Either the criterion should drop that clause or the tool should grow the field; it
   has been left unevaluated rather than guessed at.
2. **The default VAD backend is unusable here.** With the shipped default (`webrtcvad`) every
   in-gap card errors (`in_gap_vad_error: 124`) and `false_in_gap_rate` is `null`, because
   `webrtcvad` never installs — no wheel for this venv, and its best-effort install in the
   image fails too. The numbers above use `--vad ffmpeg-silencedetect`, which works. Worth
   noting for anyone reading a `timing_compare` report that shows `null`: that is what it means.

## Verdict against the spec's acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | WER ≤ current + 0.5 pp | **PASS** — better on 3/4, mean −2.42 pp, worst +0.12 pp |
| 2 | Historical context | WER 10–30%, in the range the wiki's CT2 table reports (18–21%) |
| 4 | Timing: on-cue ≥ −2 pp | **PASS** — 91.57% vs 91.79%, −0.22 pp; `false_in_gap_rate` also slightly better (6.45% vs 6.82%). The criterion's second clause (median onset error) is not produced by the tool and is left unevaluated |
| 3 | Pipeline QC | **Mixed** — see the pipeline section; layout counters improve, one hallucination signal rises |
| 5 | Confidence calibration | **Reported, not gated** — nsp is on a different scale by design (that is the finding); evaluated on word `probability` |
| 6 | Hallucination gate | **FLAGGED** — blocklist hits 15 (large-v3) vs 9 (turbo), i.e. 0.84% vs 0.48% of segments. Not "comparable"; unexplained |
| 8 | Wall time | **1.46×** — inside the 1.5× flag threshold, barely |

**Not measured, and therefore not passed:**
- **Criterion 3 (pipeline QC)** — `flagged`, `over_cps`, `dropped_hallucination` and
  `collapsed_runs` come from real pipeline runs into a separate `OUTPUT_ROOT` (T1.5). This
  run measured transcription, not the pipeline's own counters.
- **Criterion 4 (timing)** — needs `tools/timing_compare.py` over generated episodes (T1.5).
- **Criterion 7 (VRAM)** — not applicable to an endpoint entrant; the model lives on the
  serving host, and reporting this process's VRAM would be meaningless.

---

## Pipeline pass (T1.5) — criterion 3

The same five episodes run through **`generate.py` itself** (not just the transcriber), each
config into its own `OUTPUT_ROOT`, on scratch copies of the media so nothing touched the live
library. Counters are summed from the per-episode `dubtitles.qc.json` plus the run
`lastrun.json`. `collapsed_runs` and `dropped_hallucination` live only in `lastrun.json`.

| Counter | turbo | large-v3 | Δ | vs the 10% rule |
|---|---|---|---|---|
| `collapsed_runs` | 10 | **0** | −100% | **better** |
| `merged_backward` | 112 | 95 | −15% | better |
| `over_cps` | 423 | 378 | −11% | better |
| `violations` | 432 | 386 | −11% | better |
| `cards_after` | 1918 | 1875 | −2% | same |
| `restore_runs_sent` | 60 | 64 | +7% | same |
| `flagged` | 127 | 140 | +10.2% | **borderline** |
| `low_conf` | 91 | 132 | **+45%** | **expected, not a defect** |
| `dropped_hallucination` | 28 | 45 | **+61%** | **REGRESS** |
| `orphan_candidates` | 2 | 6 | **+200%** | **REGRESS** |

**Criterion 3 does not pass as written.** Three counters move the wrong way, two of them far
outside tolerance.

`low_conf` deserves its own reading: it is
`avg_logprob < -0.8 or no_speech_prob > 0.6`, and large-v3 is the config whose
`no_speech_prob` is *alive*. So this counter rising is the guard starting to work, not a
regression — the two configs are not measuring the same thing any more. That is the finding
from the bakeoff showing up again downstream.

**A correction to how these counters were first grouped here.** Reading the code rather
than trusting the names:

- `flagged` comes from `hallucination.flag_reason`, whose only trigger is
  `avg_logprob < LP_FLAG`. The nsp-gated variant was **deleted** (ADR 0002). So `flagged` is a
  **confidence-threshold count on the derived logprob**, not hallucination evidence — and it
  is the same measurement as `low_conf`, not an independent witness.
- `low_conf` likewise thresholds the derived logprob (plus nsp, which only large-v3 has).
- `orphan_candidates` is a **layout** counter (quarantined structural orphans), unrelated to
  hallucination. `over_cps`, `violations` and `merged_backward` are layout too — and those all
  improve.

So there is **one** hallucination signal here, not three: `dropped_hallucination`, which comes
from `hallucination.BLOCKLIST` matching known hallucination patterns. It rises 28 → 45 (+61%),
and the bakeoff's `blocklist_hits` independently rises 9 → 15 on the same episodes. **Two
instruments, one signal, same direction.**

**What that signal actually is** (checked 2026-11-18): the blocklist is 17 patterns —
`amara.org`, `thank you for watching`, `thanks for your support`, `please subscribe`,
`subtitles by`, `captions by`, `translated by`, `copyright`, `www.` — i.e. the **classic whisper
hallucination-artifact family**, the boilerplate whisper invents when it decodes silence or
music. So `dropped_hallucination` is a real hallucination count, not a proxy.

As a rate: **1.46% of cards** on turbo (28 / 1918) against **2.40%** on large-v3 (45 / 1875).
Small in absolute terms, ~1.6× in relative terms, consistent across both instruments.

**Which patterns fired — answered 2026-11-18** by re-running the same five episodes through
the bakeoff with `--metrics-only` omitted, so the per-segment texts were on hand to match
against. The report stayed in scratch and was not committed.

| Pattern | turbo | large-v3 |
|---|---|---|
| `we.?ll be right back` | 6 | 9 |
| `transcri(bed\|ption) by` | 2 | 0 |
| `see you (in the )?next (video\|time)` | 1 | 2 |
| `to be continued` | 0 | 3 |
| `thank you for watching` | 0 | 1 |
| **total** | **9** | **15** |

**The answer: the same failure mode, modestly more often — not a new one.** Both models invent
the same family of broadcast/credits boilerplate over silence and music transitions
("we'll be right back", "to be continued", "thanks for watching", "transcribed by"). large-v3
emits them about 1.7× as often (9 → 15 segments; 0.48% vs 0.84% of segments) and trades a couple
of phrases for others, but every one is caught and dropped by the existing filter. There is no
evidence of a worse or different failure mode.

Read against WER, this is coherent: large-v3 is more willing to emit plausible-sounding text in
a gap, which is the same behaviour that earns it a better WER on real dialogue. The filter is
what keeps that from reaching a subtitle.

One episode drives a lot of it: **Death Note S01E01 has `flagged` 11 → 27 (+145%)** — and it
is also the one episode where large-v3's WER was marginally *worse* (+0.12 pp). It is the
episode to hand-read before trusting the change.

### On wall-clock in the pipeline pass

`lastrun.json` reports turbo 875.6 s and large-v3 299.1 s for their five episodes. **This
contradicts the bakeoff's 1.46× and is not trustworthy.** The server's own request log shows
per-piece timings that are similar for both configs (turbo 4–23 s, large-v3 8–22 s), so the
difference is in the gaps *between* episodes, not in transcription — something outside the
decoder dominated that number and I could not account for it from the logs available. The
bakeoff's per-episode wall times, measured back-to-back under the same conditions and
instrumented directly, remain the reliable speed figure: **1.46×**.

## Open item worth naming

The blocklist delta is the one result that argues *against* large-v3 and it is unexplained: 9 → 15
in the bakeoff, 28 → 45 in the pipeline pass — two instruments, one signal, the same direction.
Note this is a *text* match, so a confidence-calibration story does not account for it. It could be more segments in the 10–30% WER band carrying
recognisable hallucination patterns, or a real regression. It should be understood before a
rollout, and it is the strongest argument for running T1.5's pipeline QC pass.
