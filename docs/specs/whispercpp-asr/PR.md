# PR — whisper.cpp ASR: fix the endpoint confidence signal, and measure turbo vs large-v3

## Summary

The endpoint ASR backend was losing `no_speech_prob`, and the repo's own music guard depends
on it. This branch fixes that, measures the two candidate models against real dub scripts on
the target hardware, and leaves a written record of what the change does and does not buy.

Three things land:

1. **The endpoint request asked for the wrong granularity.** `no_speech_prob` lives only on
   segments, and OpenRouter omits `segments` entirely when word granularity is the only one
   requested — so `_endpoint_request` rebuilt a single synthetic whole-file segment whose nsp
   is a hard `0.0`, and `repair.py`'s music guard passed every card. Fixed by asking for both
   granularities. whisper.cpp is unaffected (same output either way).
2. **The music guard is inert on turbo, on both backends, and has been since turbo became the
   default.** turbo's `no_speech_prob` is collapsed (`0.0` on CT2, `~1e-10` on whisper.cpp);
   `large-v3` reports a live one (median 0.54). `repair.nsp_is_alive()` now detects a collapsed
   series, logs it once per episode, and records `nsp_live` in `repair-summary.json`, so an
   inert guard stops being silent. Targeting is deliberately unchanged — which way to fail is
   an admission-policy call, coupled to the model decision below.
3. **The bakeoff can now measure an endpoint at all.** A new `endpoint:URL[@label]` entrant
   drives `generate.endpoint_transcribe` (production path, chunking included), plus a
   whisper.cpp contract fixture, an end-to-end test over a real socket, and `--metrics-only`
   so a report can leave the machine without carrying a dub script.

## Notable decisions

- **Bake off against the existing endpoint; build no image.** Recon found the target
  architecture already running on omarchy (llama-swap ships `whisper-server` +
  `ggml-large-v3-turbo.bin`, with flags already mirroring production faster-whisper). The
  custom CUDA image the handoff specified has no host that needs it: the Pascal/Turing cards it
  was sized for are gone, and the remaining fleet is one RTX 5060 plus Intel iGPUs.
- **The gate became a model A/B, not a config A/B.** large-v3 is what the maintainer chose to
  move to, so the run compares turbo against large-v3 on the same server.
- **`ASR_MODEL` doubles as the entrant label**, because llama-swap picks the entry by the
  request's `model` field — so for a router the label must be the router's entry name.
- **The default VAD backend is unchanged** even though it cannot run on a dev host; the tool
  now falls back and records what actually ran, rather than naming a backend that never
  executed.
- **Detection before policy** on the guard: the branch reports the collapse rather than
  choosing a new targeting rule, because that choice needs the model question answered first.

## Measured on the RTX 5060 (five episodes, four scored against embedded dub scripts)

| | turbo | large-v3 |
|---|---|---|
| WER (mean of four) | 20.43% | **18.01%** |
| Wall time | — | **1.46×** |
| `nsp_alive_frac` | 0.0005 | **0.166** |
| segments above nsp 0.5 | **0** | **12** |
| on-cue | 91.79% | 91.57% |
| median absolute onset error | 136.6 ms | 145.2 ms |
| blocklist artifacts | 0.48% of segments | 0.84% |

Full report and raw metrics: `docs/asr-bakeoff/whispercpp-2026-11-18.md`.

**The one counter-signal:** blocklist hits rise 9 → 15 (0.48% → 0.84%). Enumerated before
merge — the same family of broadcast-boilerplate hallucinations both models invent over
silence and music, emitted ~1.7× more often and every instance caught by the existing filter.
Not a new failure mode.

## Test plan

- `scripts/check.sh` — **5088 passed, 3 skipped** (Python 3.13 with the pinned deps, as CI runs).
  CI on this branch covers 3.11 and 3.13 plus the review-UI job.
- New tests: endpoint spec parsing (including `user@host` not read as a label), the segment
  rebuild and reflow-formula equality, the floor clamp, the router, `--metrics-only` stripping
  every transcript carrier while keeping the metrics, the whisper.cpp contract fixture through
  production's `_map_endpoint_doc`, the entrant end-to-end over a real socket, the two
  granularities in the request, the absolute-residual metric, and the VAD backend resolution.
- Manual: the bakeoff and pipeline runs were executed on the target card; nothing touches the
  live library (scratch copies with `OUTPUT_ROOT` redirects).

## Consequences to be aware of

- **Merging does not change any deployed behaviour by itself.** It carries the OpenRouter fix
  and the repair detection into the images on the next rebuild. The live llama-swap config is
  untouched except for an additive `large-v3` entry that no caller selects yet.
- **Adopting large-v3 in production is a separate step** and re-transcribes endpoint-made
  episodes once, because `decoder_identity()` records the model.
- `TRANSCRIBE_VERSION` was **not** bumped — no decoder-affecting change lands here.
