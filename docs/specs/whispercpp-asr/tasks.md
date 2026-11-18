# Tasks — whisper.cpp ASR sidecar

> Persistent memory between sessions. Read this + `spec.md` and check out the branch before
> anything else. Legend: `[ ]` pending · `[~]` in progress · `[x]` done.
>
> **Revised after recon (2026-11-18).** Phase 0's image build is removed: omarchy already runs the
> target architecture. The centre of gravity is now the **bakeoff against the existing endpoint**.
> Old task IDs (T0.4-T0.8, T1.6) are retired rather than renumbered, so the handoff stays
> cross-referencable.

**Branch:** `feat/whispercpp-asr-sidecar` (base: `main`)

Rules: each task ≤ ~1h with a verifiable done criterion; `scripts/check.sh` green before `[x]`;
1 task = 1 commit; never push or write to a host without the maintainer's approval.

## Phase 0 — Spec and ground truth

- [x] **T0.1** Write `spec.md` (this project, `docs/specs/whispercpp-asr/`) capturing the pivot —
  done when: it exists and the maintainer has approved it. *(written; awaiting approval)*
- [x] **T0.2** `gh pr list` — confirm no open PR touches Dockerfiles or `compose*.yaml` —
  done when: output pasted. *(empty 2026-11-18 — no open PRs at all)*
- [x] **T0.3** Pin the whisper.cpp release and verify every CMake option + server flag against
  that tag — done when: the grep output is recorded. *(pinned `v1.9.5`; all options and flags
  verified at the tag; `/health` semantics answered; backend load order confirmed)*
- [~] **T0.4** Recon the real fleet and the existing deployment — done when: hardware, drivers,
  the llama-swap image's backends and its `whisper-large-v3-turbo-subs` flags are recorded in the
  spec. *(done: see spec "Verified facts" and "Config gaps"; GTX 1050Ti/1060/2070S confirmed
  absent from the fleet)*

## Phase 1 — Bakeoff against the existing endpoint

- [ ] **T1.0** Config gaps — decide and apply `--logprob-thold -2.0` to the live
  `whisper-large-v3-turbo-subs` entry (and `--dtw large.v3.turbo` if the maintainer wants it
  measured). **Maintainer runs the write** to `~/stacks/ai/llama-swap.yaml` on omarchy; this task
  produces the exact edit and the recreate/verify commands — done when: he confirms the endpoint
  reports the new flags and a smoke transcription still returns `words`.
- [ ] **T1.1** Add an `endpoint:` entrant to `tools/asr_bakeoff.py` — e.g.
  `--models large-v3-turbo,endpoint:http://127.0.0.1:11434/v1/audio/transcriptions@whispercpp-turbo-f16`.
  Reuse `generate.endpoint_transcribe` so the bakeoff exercises production code (chunking
  included); keep GPU imports lazy; fill the same `segments`/`words`/`episodes` shape as
  `run_whisper` (`tools/asr_bakeoff.py:441`) — done when: unit tests pass against a fake server
  (`tests/fake_servers.py`) with the failing test written first, and `scripts/check.sh` is green.
- [ ] **T1.2** Make the bakeoff report metrics-only (a `--metrics-only` flag or a documented strip
  step) so no dub text can be committed — done when: a test shows `texts`/`words` are absent.
- [ ] **T1.3** Hand the maintainer the bakeoff commands for omarchy — current config vs proposed
  config, plus the wiki's stored figures as context. Entrants run sequentially; stop the asr
  container between a faster-whisper run and a whisper.cpp run so VRAM is free — done when: he
  returns the `--out` JSONs.
- [ ] **T1.4** *(optional, only if a LAN sidecar is adopted)* Measure upload format: the endpoint
  path re-encodes to 32 kbps mono mp3 for OpenRouter's 25 MB cap, which is needless for a loopback
  sidecar. Run the entrant with mp3 and with wav — done when: both WERs are in the report. If they
  match, drop the idea (YAGNI).
- [ ] **T1.5** Timing: generate the same ≥ 4 episodes with each config into **separate
  `OUTPUT_ROOT`s** (nothing touches the live library; check `common.out_for`) and run
  `tools/timing_compare.py` on each, for `ASR_WORD_TIMING=token` and `=dtw` once T2.3 lands —
  done when: on-cue fraction and median onset error are tabulated per config.
- [ ] **T1.7** Write `docs/asr-bakeoff/whispercpp-<date>.md` — metrics only, with a verdict against
  the spec's acceptance criteria — done when: the maintainer has read it. **Stop here if the gate
  fails.**

## Phase 2 — Pipeline changes (all opt-in, defaults unchanged)

Each item: failing test first, then code, then the registry entry (`settings_registry.py` via
`_wave(...)`), a `docs/wiki/Reference.md` row, and a CHANGELOG line under `## [Unreleased]`.

- [ ] **T2.1** Preflight health check — before the first transcription of a run in endpoint mode,
  `GET` `ASR_HEALTH_URL` (default derived only when clearly a whisper.cpp sidecar; unset = skip).
  Not ready after N seconds → fixed diagnostic and non-zero exit **before touching any episode**,
  mirroring the key-guard exit at `generate.py:1808-1814`. Treat any non-200 as not ready —
  the server answers `503 {"status":"loading model"}` mid-load — done when: tests cover ready,
  not-ready-then-ready, and never-ready.
- [ ] **T2.2** `ASR_UPLOAD_FORMAT` (`mp3` default | `wav`) — only if T1.4 showed a difference —
  done when: the test asserts content type and filename per format and mp3 stays the default.
- [ ] **T2.3** `ASR_WORD_TIMING=dtw` in `_map_endpoint_doc` — done when: tests cover token merge
  plus DTW, `t_dtw=-1` fallback, non-monotonic clamping, and the segment-end clamp.
- [ ] **T2.4** Endpoint identity tag — **maintainer approval required** (re-transcription) —
  done when: tests show the tag in `decoder_identity()` and that a mismatch blocks words reuse.
- [ ] **T2.5** Fallback endpoint (`ASR_FALLBACK_URL` / `_MODEL` / `_KEY_ENV`, empty = off).
  Transport errors only; the key guard applies independently; **whole-episode fallback, never
  mixed decoders within one transcript**; record the identity of the decoder that actually ran —
  done when: tests cover primary-ok, primary-down-fallback-ok, both-down, fallback-key-refused,
  and mixed-piece prevention.
- [ ] **T2.7** Update `.env.example`, `docs/wiki/Reference.md`,
  `docs/wiki/Choosing-an-ASR-Model.md`, `docs/AGENT-GUIDE.md`, `CHANGELOG.md` — done when:
  `scripts/check.sh` is green including `tests/test_public_repo_hygiene.py`.

## Phase 3 — Rollout (maintainer's hands)

- [ ] **T3.1** Draft the PR description (Summary / Notable Decisions / Test Plan), listing the
  re-transcription consequence and the bakeoff numbers. **Pause for approval.**
- [ ] **T3.2** Rollout runbook: back up the library (`docs/AGENT-GUIDE.md` §6), one show first,
  inspect the track and review queue, then widen. Rollback = the current llama-swap entry, which
  stays untouched until T1.0's change is deliberately applied.
- [ ] **T3.3** Retiring `Dockerfile.builder` / faster-whisper is **not in this project** —
  see F1 in `dubtitlerr-future-plans.md`, gated on weeks of clean runs.

## Closing (the _close_ phase — keep last)

- [ ] Evolve tests/CI to cover the changes — done when: pipeline green on 3.11 and 3.13.
- [ ] Push the branch to origin — done when: branch published. **Requires maintainer approval.**
- [ ] Draft the PR (Summary / Notable Decisions / Test Plan) and **pause for approval**.

## Done

*(move tasks here as they complete, preserving the done criterion)*

- **T0.2** — `gh pr list` empty, 2026-11-18.
- **T0.3** — pinned `v1.9.5`; all CMake options, server flags, `/health` semantics, DTW preset
  names and the backend load order verified at the tag.
- **T0.4** — fleet recon: omarchy RTX 5060 / driver 615.78.08 / sm_120; no NVIDIA on
  debiantop/saxserver/minisax (UHD 630 ×2); llama-swap image ships cpu+cuda backends only.
