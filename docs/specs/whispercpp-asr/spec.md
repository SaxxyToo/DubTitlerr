# Spec — whisper.cpp ASR sidecar

> Derivative of the 2026-11-18 handoff `whispercpp-asr-handoff.md`, revised after recon against
> `main` at `0e6abaf` and the live hosts. Where this document and the handoff disagree, this one
> wins: the handoff was written against `dc01304` and against a hardware fleet that no longer
> exists.

## Context and problem

DubTitlerr's full image is ~13 GB because it bakes a faster-whisper/CTranslate2 model. The
endpoint backend already exists and speaks OpenAI-compatible `/v1/audio/transcriptions`, so the
pipeline does not care who answers. The goal is to run transcription on a small whisper.cpp server
instead: local first (GPU before CPU), accuracy no worse than today, endpoints still supported.

**Recon changed the shape of this project.** The intended architecture is *already running* on
omarchy: `llama-swap` (`ghcr.io/mostlygeek/llama-swap:unified-cuda13`) ships `whisper-server` and
`ggml-large-v3-turbo.bin`, and its `whisper-large-v3-turbo-subs` entry already mirrors production
faster-whisper's decode parameters. There is nothing to build for the primary host. The work is
therefore a **bakeoff against the existing endpoint plus two config gaps**, not an image build.

**Hardware (verified 2026-11-18, not the handoff's table):**

| Host | GPU | Driver | Notes |
|---|---|---|---|
| omarchy | RTX 5060, sm_120 | 615.78.08 (CUDA UMD 13.4) | the ASR host; already serves whisper.cpp |
| saxserver | Intel UHD 630 | — | no NVIDIA driver |
| minisax | Intel UHD 630 | — | no NVIDIA driver |
| debiantop | (no NVIDIA driver) | — | control plane |

The GTX 1050 Ti / 1060 / RTX 2070 Super named in the handoff §2.7 exist **only in repo history**
(`docs/asr-bakeoff/*.json`, the "3500g node" comment at `generate.py:97`). They are gone. The
handoff's §5 gate — "on both the GTX 1060 and the RTX 2070 Super" — is therefore unexecutable and
is rewritten below.

## Acceptance criteria (verifiable)

All measured on **omarchy / RTX 5060**. The Intel iGPUs are *fallback* targets only and are not
gated in this project (see Out of scope).

1. **Same-machine A/B against today's production path.** whisper.cpp on omarchy with the proposed
   config vs the *current* `whisper-large-v3-turbo-subs` entry, on the same episodes. WER must be
   ≤ the current config's WER + 0.5 percentage points. (This measures the config change, not the
   engine change — see Decisions taken for why.)
2. **Historical context.** Report WER against the wiki's stored faster-whisper figures
   (`docs/wiki/Choosing-an-ASR-Model.md`: large-v3 int8 18.93% / 18.25%, turbo int8 20.76% /
   21.71%) for reference, labelled as cross-machine and not a gate.
3. **Pipeline QC.** Across ≥ 4 episodes covering the AGENTS.md spread (with/without a fansub
   track; glossary acquired vs mined only; never One Pace alone), `flagged`, `over_cps`,
   `dropped_hallucination` and `collapsed_runs` from `lastrun.json` do not regress by more than 10%
   versus the current config.
4. **Timing.** `tools/timing_compare.py` gives an on-cue fraction ≥ the current config's minus 2
   percentage points, and median absolute onset error ≤ the current config's + 50 ms. Run it for
   both word-timing modes (token vs DTW).
5. **Confidence calibration.** The `avg_logprob` / `no_speech_prob` / word `probability`
   distributions select a similar share of cards (within ±20% relative) under the thresholds at
   `generate.py:934`. If they do not, **stop and report** — retuning thresholds is admission
   policy and is the maintainer's call, not this project's.
6. **Hallucination gate.** The Japanese-script drop and the blocklist fire at comparable rates.
7. **Resource.** Peak VRAM fits the 5060's 8 GB at beam 7 with the chosen model.
8. **Wall time.** Seconds per episode reported for both configs. No hard gate; a slowdown over
   1.5× is flagged to the maintainer.

## Out of scope (explicit)

- Building `Dockerfile.asr` or any custom CUDA/Vulkan image. Deferred until an Intel-iGPU host
  actually needs to serve ASR.
- The multi-arch CUDA `61;75;86;89;120` portability work. Its only motivation was Pascal + Turing
  hosts, which no longer exist.
- Retiring `Dockerfile.builder` / faster-whisper. That is F1 in `dubtitlerr-future-plans.md`.
- Replacing the omarchy llama-swap deployment. It is the reference deployment.
- AMD/Intel GPU support (handoff F7), Rust worker (F3), multi-GPU pool (F10).
- Changing the default `ASR_BACKEND`. It stays `local` until the maintainer signs off.

## Data contracts

- **Inputs:** unchanged. `ASR_BACKEND=endpoint`, `ASR_URL`, `ASR_MODEL`, `ASR_KEY_ENV`,
  `ASR_TIMEOUT` (`generate.py:122-129`).
- **Outputs:** unchanged. `_map_endpoint_doc` (`generate.py:1491`) already reads whisper.cpp's
  sub-word `words[]` shape, per-word `start`/`end`/`probability`, and per-segment `avg_logprob` /
  `no_speech_prob`. It ignores `t_dtw`.
- **Schemas affected:** `<stem>.dubtitles.words.json` identity via `decoder_identity()`
  (`generate.py:209`). Any new key re-transcribes every endpoint-transcribed episode once —
  maintainer approval required.
- **External dependencies:** the omarchy llama-swap whisper endpoint
  (`http://127.0.0.1:11434/...`, OpenAI-compatible, keyless because the key guard in
  `common.openai_endpoint_problem` (`common.py:854`) refuses plain-http keys off loopback).

## Edge cases and failure modes

| Case | Expected behavior |
|---|---|
| Sidecar down before a sweep | Preflight health check exits non-zero before touching an episode (T2.1); today each episode burns a retry cycle and writes a `crash.json` |
| `/health` mid-load | Reports `503 {"status":"loading model"}` — treat "not 200" as not ready, do not parse the body |
| Server restarted mid-sweep | Existing behavior holds: a transport error raises the original error, the episode is retried next sweep, and it is never retired as no-speech |
| Config changes decode, identity unchanged | Silent staleness — the reason T2.4/T2.5 and F6 exist |
| `t_dtw == -1` (DTW off or unavailable) | Fall back to the token `start` |
| Non-monotonic DTW timestamps | Clamp; last word of a segment ends at the segment end |

## Decisions taken

| Decision | Rejected alternative | Why |
|---|---|---|
| Bake off against the existing llama-swap endpoint; build no image | Build `Dockerfile.asr` per handoff §4.1 | The target architecture already runs on omarchy with decode params already mirroring production. A custom image has no host that needs it. Maintainer decision 2026-11-18. |
| Rewrite the §5 gate for the RTX 5060; Intel iGPU is fallback only | Keep the 1060/2070S gate | Those cards no longer exist. Maintainer confirmed 2026-11-18. |
| Gate is a **same-machine A/B of configs**, not engines | Engine A/B (faster-whisper vs whisper.cpp on one card) | No faster-whisper-capable NVIDIA card exists any more, and faster-whisper cannot run on the 5060 at all (CTranslate2/cuBLAS `CUBLAS_STATUS_NOT_SUPPORTED`). omarchy's production path is *already* whisper.cpp. |
| Spec lives at `docs/specs/whispercpp-asr/` | `specs/whispercpp-asr/` per handoff T0.1 | `specs/` was retired as process residue on 2026-11-18; `docs/` holds live design documentation (`docs/adr/` is the precedent). Maintainer decision. |
| Pin whisper.cpp **`v1.9.5`** | `master@6680804` (handoff §0/§4.1) | master is 1.9.6-dev and untagged; §4.1 itself says never build master for production. All flags/options re-verified at the tag. |

## Verified facts (against `v1.9.5`, and against the live hosts)

- All CMake options the build needs exist at `v1.9.5`: `BUILD_SHARED_LIBS`, `GGML_BACKEND_DL`,
  `GGML_CPU_ALL_VARIANTS`, `GGML_NATIVE`, `GGML_CUDA`, `GGML_VULKAN`, `WHISPER_BUILD_SERVER`.
- All server flags exist at the tag: `-bs -bo -wt -sow -dtw -ng -fa -nfa -nth -lpt -mc
  --inference-path --convert --vad --tmp-dir`.
- Backend load order at the tag: blas → zendnn → cann → **cuda** → hip → metal → rpc → sycl →
  **vulkan** → virtgpu → opencl → hexagon → musa → openvino → **cpu (last)**. The
  CUDA → Vulkan → CPU chain holds.
- `/health`: `200 {"status":"ok"}` when ready, `503 {"status":"loading model"}` while loading.
- `--dtw large.v3.turbo` is a valid preset (`WHISPER_AHEADS_LARGE_V3_TURBO`); an unknown preset
  exits with `error: unknown DTW preset` at startup — fail-fast, no silent fallback.
- The llama-swap image ships **`libggml-base`, `libggml-cpu`, `libggml-cuda` only (ggml 0.29.0) —
  no Vulkan backend.** It cannot serve the Intel iGPU hosts.
- `--flash-attn` defaults **true** in the llama-swap build.

## Finding: the music guard is inert for turbo — on BOTH backends (not an endpoint issue)

Measured 2026-11-18 from the repo's own stored bakeoff artifacts (`docs/asr-bakeoff/*.json`,
two physical cards) and from the live omarchy endpoint:

| Model / backend | `nsp_alive_frac` | nsp median | nsp max |
|---|---|---|---|
| **large-v3-turbo (int8, CT2 — production default)** | **0.0** | **0.0** | **0.0** |
| large-v3 (int8, CT2) | 1.0 | 0.542 | 0.961 |
| whisper.cpp turbo (live endpoint) | — | ~1e-10 | 1.5e-08 |

`no_speech_prob` is present on the endpoint (`2.2e-10`, `4.6e-12`, `1.5e-08`) — present and
meaningless, not missing. But the same collapse holds for CT2's turbo, which is the model the
local path ships: **the guard has been inert since turbo became the default**, not since
endpoint mode arrived. The knowledge was already in the repo (`NSP_ALIVE_FLOOR`'s comment:
"Turbo's known nsp collapse, verified across two CT2 conversions") but only in the bakeoff
tool, never in the stage that depends on the signal.

What that means concretely at `repair.py:323`:

```
if c.get("no_speech_prob", 1.0) > NSP_MAX:   # NSP_MAX = 0.5
    return False                             # "not speech" -> do not repair
```

On a collapsed series this passes every card, so `is_target` sends music cards to the repair
LLM. The direction of the defect is opposite to "repairs are being skipped": it is that the
one guard which screens out music **does not screen anything out**. `generate.py:934`'s
`low_conf` has the same dead half, but that one is only a counter.

**Landed (safe half):** `repair.nsp_is_alive()` detects a collapsed series, logs it once per
episode, and records `nsp_live` in the repair summary, so "targets: 144" can be read for what
it is. Targeting is deliberately unchanged — which way to fail is policy, and it is coupled to
the model decision below.

**Open decision (maintainer):** the two coherent options are
1. **keep turbo** — accept that music is not screened, and let the downstream protections
   (blocklist, the Japanese-script drop, the review queue) carry it; or
2. **move the sidecar to large-v3** — restores a live nsp (median 0.54) *and* measured better
   WER than turbo in `docs/wiki/Choosing-an-ASR-Model.md` (18.93% vs 20.76% on the 1060,
   18.25% vs 21.71% on the 2070S), at the cost of speed and a ~3 GB model download.

Option 2 also answers spec Open question 7. It is a model choice, so it is the maintainer's.

**This revises acceptance criterion 5**: comparing `no_speech_prob` distributions across
models measures whether the signal is alive, not whether transcription is accurate. The
criterion is evaluated on word `probability` (which both families produce reliably) with nsp
reported for context only.

## Measured on the RTX 5060 (2026-11-18) — the actual target hardware

Ran through the real endpoint path (`tools/asr_bakeoff.py`, EdgeRunners S01E01, a 25.6-min
episode, against that episode's embedded **`Dub`** track — a genuine dub script: 329 cues,
2246 words):

| Entrant | wall | segments | words | WER vs dub script | nsp alive | nsp max | segs nsp>0.5 |
|---|---|---|---|---|---|---|---|
| `whisper-large-v3-turbo-subs` | **42.3 s** | 393 | 2309 | 16.66% | **0.25%** | 1.9e-05 | **0** |
| `whisper-large-v3-subs` | **63.1 s** | 337 | 2243 | **16.21%** | **19.6%** | **0.925** | **10** |

Agreement between the two: 12.71%.

**Speed: 1.49× — +20.8 s per 26-minute episode.** (The 1060/2070S figures elsewhere in this
spec are CT2 on hardware nobody owns any more; these two rows are the measured ones.)

**The music guard:** turbo produces **zero** segments above 0.5 — the guard provably cannot
fire. large-v3 produces 10, and its `maybe_silence_would_fire` counter moves off zero for the
first time. That is the change's real payoff, and it is measured, not inferred.

**Accuracy:** large-v3 is better, but only by **0.45 pp** on this episode — far narrower than
the 1.83 pp gap the CT2-era table shows (18.93 vs 20.76). Worth stating plainly: the case for
large-v3 here is mostly **the guard**, with accuracy a secondary gain.

**Residual:** one episode is one episode. The gate wants ≥4 covering the AGENTS.md spread
(with/without a fansub track; glossary acquired vs mined only), so these rows open the gate
rather than close it.

## Config gaps (existing endpoint vs production faster-whisper)

| Parameter | Production faster-whisper (`generate.py:1587`) | Live llama-swap entry | Action |
|---|---|---|---|
| beam | `beam_size=7` | `--beam-size 7` | ✔ already matches |
| best-of | `best_of=7` | `--best-of 7` | ✔ already matches |
| no-speech | `no_speech_threshold=0.9` | `--no-speech-thold 0.9` | ✔ already matches |
| context | `condition_on_previous_text=False` | `--max-context 0` | ✔ already matches |
| **logprob** | `log_prob_threshold=-2.0` | *(unset → default `-1.00`)* | **gap — T1.0** |
| **word timing** | `word_timestamps=True` | *(no `--dtw`)* | **gap — T1.0, gated on T1.5** |

## Open questions (maintainer decisions)

1. **Reference episodes for the gate.** Which episode(s) and which reference SRT? (The handoff's
   §9 Q2 asked for the `--ref EPISODE_BASENAME=SRT_PATH` used for the wiki table.)
2. **`--logprob-thold -2.0`** — apply it to the live `-subs` entry, or bake it off first? It is a
   write to `~/stacks/ai/llama-swap.yaml` on omarchy.
3. **`TRANSCRIBE_VERSION` bump** — yes or no for this change set (`common.py:183-187`).
4. **DTW** — if T1.5 shows DTW timing wins, adopt `ASR_WORD_TIMING=dtw` plus `--dtw` on the server?
5. **Endpoint identity tag** (handoff T2.4) — OK to add a server-config tag knowing it
   re-transcribes every endpoint-made episode once?
6. **Fallback** — if a future fallback (OpenRouter) is used, re-do those episodes locally later,
   or keep them?
7. **Model default** — ANSWERED 2026-11-18: move to `large-v3` (maintainer). Measured on
   the 5060: +1.49× wall, +0.45 pp WER, and the only one of the two whose `no_speech_prob`
   the music guard can use.
8. **Upstream** — PR to `XenaRathon/DubTitlerr`, or fork-only?
9. **Intel iGPU hosts** — is a Vulkan/CPU ASR image ever wanted for saxserver/minisax, or do they
   stay on OpenRouter endpoints permanently?

## Constraints

- Follow `AGENTS.md` and `docs/AGENT-GUIDE.md` §4.
- Never push, merge, deploy or write to a host without explicit approval. Deploys are the
  maintainer's to run.
- Never commit dub transcripts; bakeoff output must be metrics-only.
- No Tailscale `100.x` addresses in docs or configs.
