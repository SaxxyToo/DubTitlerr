# ASR bakeoff data

Reference. The numbers behind [Choosing an ASR model](Choosing-an-ASR-Model.md) — every entrant
that was measured, on which card, and what it cost in VRAM and wall time.

**Metrics only.** The harness (`tools/asr_bakeoff.py`) also captures each entrant's full
transcript. That is a commercial dub's script, so a report destined to leave the machine is run
with `--metrics-only`, which strips the transcripts and the release tags in each episode filename.
Word error rate, peak VRAM, load time, wall time and the confidence distributions are what is kept,
and they are all that is published here.

---

## The 2026-09-04 bakeoff — CTranslate2 stack, two GPU generations

Three episodes, chosen for range: a normal 24-minute episode, a second-show sanity check, and a
142-minute movie with heavy music and mixed calm/action audio.

`large-v3` and `large-v3-turbo` ran `int8`; every other entrant ran `fp16`, which is the only
precision its stack supports (see the asymmetry note below). Word error rate is against a real
reference — the movie's "SDH" subtitle track turned out to be the professional English dub script,
so one episode carries genuine ground truth rather than cross-model agreement.

| Model | Precision | GTX 1060 (6 GB, Pascal) | RTX 2070 Super (8 GB, Turing) | Peak VRAM | Load |
| --- | --- | ---: | ---: | ---: | ---: |
| **large-v3** | int8 | **18.93%** | **18.25%** | 1645 / 2888 MiB | 73.6 / 22.0 s |
| large-v3-turbo | int8 | 20.76% | 21.71% | 941 / 3245 MiB | 38.2 / 7.3 s |
| Qwen3-ASR-1.7B | fp16 | 23.69% | 23.47% | 4599 / 5440 MiB | 79.8 / 82.5 s |
| parakeet-tdt-0.6b-v3 | fp16 | 28.55% | crashed every attempt | 4949 MiB (1060) | 64.0 s |
| parakeet-tdt_ctc-110m | fp16 | 29.04% | crashed every attempt | 4587 MiB (1060) | 28.3 s |
| Qwen3-ASR-0.6B | fp16 | 29.11% | 29.11% | 2065 / 2707 MiB | 51.5 / 14.9 s |
| canary-180m-flash | fp16 | 72.72% | 73.30% | 5133 / 3956 MiB | 34.0 / 23.1 s |
| canary-1b-v2 | fp16 | OOM at load | loaded; transcription broken | 7843 MiB (2070S) | 3200.4 s |
| canary-qwen-2.5b | — | not run — no word timestamps | not run | — | — |
| large-v3-turbo | float16 | did not load — Pascal has no efficient fp16 | n/a | — | — |

Wall time per episode, the two cards that ran everything:

| Entrant | GTX 1060 (3 episodes) | RTX 2070 Super (3 episodes) |
| --- | ---: | ---: |
| large-v3-turbo (int8) | 88.8 / 186.0 / 616.2 s | 93.9 / 115.3 / 557.0 s |
| large-v3 (int8) | 187.6 / 395.3 / 1132.6 s | 113.0 / 205.2 / 593.2 s |
| Qwen3-ASR-1.7B (fp16) | 674.3 / 1250.2 / 4753.6 s | 209.7 / 418.3 / 1407.3 s |
| Qwen3-ASR-0.6B (fp16) | 629.0 / 1236.9 / 4435.7 s | 214.3 / 395.3 / 1423.2 s |
| canary-180m-flash (fp16) | 97.5 / 200.0 / 684.1 s | 47.8 / 101.5 / 350.2 s |

**Why the precision column is not a fair fight.** Neither NeMo nor Qwen3-ASR has a working int8
inference path — NeMo's route runs through a separate TensorRT Model Optimizer export days away
from a same-day bakeoff, and the `torch.quantization` route is a documented compatibility dead end
against these architectures. So every non-whisper entrant ran at fp16, roughly double int8's cost
for a model of the same size. WER is comparable across precisions; peak VRAM is not.

**Cross-entrant agreement** (how much two entrants' text differs — a disagreement measure, not an
accuracy one): large-v3-turbo vs large-v3, 12.04% on the 1060 and 12.04% on the 2070 Super. Qwen3-ASR
1.7B vs its 0.6B sibling, 14.90% / 14.90%. canary-180m-flash disagrees with everything by 63–153%,
which is what a repetition-collapse failure looks like as a number.

---

## The 2026-11-18 bakeoff — whisper.cpp over an HTTP endpoint, RTX 5060

Two entrants behind one `llama-swap` instance, selected by the request's `model` field, driven
through production's own `generate.endpoint_transcribe` — silence-chunking and the 32 kbps mp3
re-encode included. Hardware: RTX 5060 (sm_120), driver 615.78.08. Entrants ran strictly
sequentially. This is an **endpoint** entrant, so there is no VRAM number for it: the model lives on
the serving host, and this process's VRAM would be meaningless.

| Entrant | Server config |
| --- | --- |
| `whisper-large-v3-turbo` | `ggml-large-v3-turbo.bin`, beam 7 / best-of 7 / no-speech-thold 0.9 |
| `whisper-large-v3` | `ggml-large-v3.bin`, the same flags plus `--logprob-thold -2.0` |

Five episodes, and four of them carry an embedded subtitle track that is the **English dub script**
(the library labels them `Dub`, `English (Dubtitle)`, `English (SDH Dubtitle)`), so the WER below is
ground truth, not agreement. The fifth (Attack on Titan S01E01) covers the fansub axis and reports no
WER. Note the constraint this exposed: **no episode that has a dub reference also has a fansub
track** — the two axes do not overlap in this library.

| Episode | turbo WER | large-v3 WER | Δ | turbo wall | large-v3 wall | ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Cyberpunk Edgerunners S01E01 | 16.66% | **16.21%** | −0.45 | 42.8 s | 63.6 s | 1.49× |
| Death Note S01E01 | **10.12%** | 10.24% | +0.12 | 30.7 s | 52.3 s | 1.70× |
| Vinland Saga S01E01 | 25.37% | **21.71%** | −3.66 | 37.0 s | 58.1 s | 1.57× |
| FMA Brotherhood S01E02 | 29.56% | **23.87%** | −5.69 | 42.4 s | 67.9 s | 1.60× |
| Attack on Titan S01E01 | — | — | — | 58.0 s | 63.2 s | 1.09× |
| **mean of the four** | **20.43%** | **18.01%** | **−2.42** | 3.5 min | 5.1 min | **1.46×** |

Cross-entrant agreement 10.90% — the two models genuinely disagree on about a tenth of the words,
so they are not near-duplicates of each other.

### The confidence signal, which is the reason this run happened

| | turbo | large-v3 |
| --- | ---: | ---: |
| `nsp_alive_frac` | **0.0005** | **0.166** |
| `nsp_max` | 1.9e-05 | **0.9248** |
| segments with `no_speech_prob` > 0.5 | **0** | **12** |
| `blocklist_hits` | 9 (0.48% of segments) | 15 (0.84%) |
| `logprob_median` | −0.041 | −0.037 |
| segments | 1866 | 1791 |

`large-v3-turbo` cannot clear the music guard on any segment; `large-v3` reaches 12. That single
fact is why the endpoint request was changed to ask for **both** word and segment granularity —
`no_speech_prob` lives only on segments, and an endpoint that is asked for words alone omits it
entirely. See [Why it works this way](Why-It-Works-This-Way.md) for what the guard does with it.

### Pipeline counters on the same five episodes

The same episodes were then run through `generate.py` itself (not just the transcriber), each config
into its own `OUTPUT_ROOT`, on scratch copies so nothing touched a live library.

| Counter | turbo | large-v3 | Δ |
| --- | ---: | ---: | ---: |
| `collapsed_runs` | 10 | **0** | −100% |
| `merged_backward` | 112 | 95 | −15% |
| `over_cps` | 423 | 378 | −11% |
| `violations` | 432 | 386 | −11% |
| `cards_after` | 1918 | 1875 | −2% |
| `restore_runs_sent` | 60 | 64 | +7% |
| `flagged` | 127 | 140 | +10.2% |
| `low_conf` | 91 | 132 | +45% (expected — that counter reads `no_speech_prob`, which only large-v3 has alive) |
| `dropped_hallucination` | 28 | 45 | **+61%** |
| `orphan_candidates` | 2 | 6 | **+200%** (a layout counter, unrelated to hallucination) |

Reading the hallucination signal honestly: `flagged` and `low_conf` both threshold the *derived
logprob*, so they are one measurement, not two independent witnesses. The one real hallucination
instrument is `dropped_hallucination`, from `hallucination.BLOCKLIST` — and it rises 28 → 45
(1.46% → 2.40% of cards), agreeing with the bakeoff's own `blocklist_hits` rising 9 → 15. **Two
instruments, one signal, same direction.**

Matching the patterns by name (a scratch re-run with the transcripts on hand, nothing committed):
both models invent the same family of broadcast/credits boilerplate over silence and music — "we'll
be right back", "to be continued", "thank you for watching", "transcribed by". large-v3 emits them
about 1.7× as often and trades a couple of phrases for others, but **every instance is caught and
dropped by the existing filter**. Same failure mode, more often — not a new one.

Read against WER this is coherent: large-v3 is more willing to emit plausible-sounding text in a
gap, which is the same behaviour that earns it a better WER on real dialogue. The filter is what
keeps that from reaching a subtitle.

### Timing

`tools/timing_compare.py`, GPU-free, four episodes per config. "On cue" is the share of cards whose
timing lands on a subtitle cue; absolute onset error is the median |residual| over inlier pairs.

| Metric | turbo | large-v3 |
| --- | ---: | ---: |
| `pct_cards_on_cue` | **91.79%** | 91.57% |
| `false_in_gap_rate` | 6.82% | **6.45%** |
| median absolute onset error | **136.6 ms** | 145.2 ms |

The two trade places per episode, which is why the median and the aggregate are the numbers to read.
The ~140 ms absolute figure is a property of the method — nearest-onset pairing of card boundaries
against cue boundaries — and is essentially the same for both configs.

### Speed, and one number that is not trustworthy

The bakeoff's instrumented wall times give **1.46×**. The pipeline pass's own `lastrun.json` reported
turbo 875.6 s against large-v3 299.1 s, which contradicts it and is **not** trustworthy: the server's
request log shows per-piece timings that are similar for both configs (turbo 4–23 s, large-v3 8–22 s),
so the difference lay in the gaps *between* episodes rather than in transcription, and nothing in the
logs accounted for it. Trust the instrumented figure.

---

## Reading these tables

- **A better WER is not automatically a better subtitle.** The one number that argues against
  large-v3 is its hallucination rate, and it is unexplained.
- **Wall time is per-episode and single-stream.** Vendor throughput figures that assume large-batch
  server concurrency (Qwen3-ASR's, for instance) do not describe this workload, which is one episode
  at a time — that gap is most of why Qwen3-ASR measured ~7× slower than Whisper.
- **`no_speech_prob` is not comparable across models.** turbo reports a collapsed zero, CT2 `large-v3`
  reports a live one. Any counter built on it measures something different on each.
