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

## Verdict against the spec's acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | WER ≤ current + 0.5 pp | **PASS** — better on 3/4, mean −2.42 pp, worst +0.12 pp |
| 2 | Historical context | WER 10–30%, in the range the wiki's CT2 table reports (18–21%) |
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

## Open item worth naming

The blocklist delta (9 → 15 across the same episodes) is the one result that argues *against*
large-v3, and it is unexplained. It could be more segments in the 10–30% WER band carrying
recognisable hallucination patterns, or a real regression. It should be understood before a
rollout, and it is the strongest argument for running T1.5's pipeline QC pass.
