# Plan — whisper.cpp ASR sidecar

> `spec.md` approved 2026-11-18 (maintainer: "go for it"). This plan reflects the pivot:
> the bakeoff runs against the **existing omarchy endpoint**, and no image is built.

## Branch and delivery

- **Branch:** `feat/whispercpp-asr-sidecar` (base: `main`; this repo is single-branch, so
  `main` is the only base).
- **PR slicing:** 2 PRs.
  1. **Bakeoff instrument** — T1.1, T1.2 (the endpoint entrant + metrics-only reports).
     Nothing changes for a running instance; it only adds a way to measure.
  2. **Opt-in pipeline changes** — T2.1, T2.2, T2.3, T2.4, T2.5, T2.7, with the bakeoff
     verdict attached. Every change is off by default.

  Rationale: the instrument must be reviewable on its own, because the *numbers it
  produces* are the evidence the second PR depends on.

## Technical approach

Extend `tools/asr_bakeoff.py` with an `endpoint:` entrant that calls
`generate.endpoint_transcribe` — the production path, chunking and mp3 re-encode included —
so the measurement describes what a real run does rather than an idealised direct POST. The
endpoint's segment shape carries no text and no per-segment logprob, so the entrant rebuilds
text from each segment's words and derives `avg_logprob` with **reflow's own formula**
(`mean ln(max(prob, PROB_FLOOR))`, importing `PROB_FLOOR` rather than restating it), which is
also how production derives card confidence for both backends. That keeps the judge's inputs
identical to production's and the two logprob columns honestly labelled
(`logprob_source: native-segment | derived-from-word-probs`).

Phase 2 changes stay opt-in behind settings, each with a failing test first and a
`settings_registry.py` wave entry, exactly as the handoff's §6 specifies.

## Affected files (by layer)

| Layer | File | Change |
| ----- | ---- | ------ |
| Tool / router | `tools/asr_bakeoff.py` | `is_endpoint_name`, `parse_endpoint_spec`, `endpoint_run_shape`, `run_endpoint`; dispatch; `--models` help; `logprob_source` on each entry |
| Tool / reporting | `tools/asr_bakeoff.py` | `--metrics-only` (T1.2) so no dub text can leave the tool |
| Tests | `tests/test_asr_bakeoff.py` | spec parsing (incl. `user@host` not mistaken for a label), shape rebuild, reflow-formula equality, floor clamp, routing |
| Tests | `tests/fake_servers.py` | a multipart-capable ASR fake — the existing `FakeServer` JSON-parses every body and cannot serve a form upload (T1.1 follow-on) |
| Pipeline (opt-in) | `generate.py` | T2.1 preflight; T2.3 DTW mapping in `_map_endpoint_doc`; T2.5 fallback; T2.4 identity tag |
| Settings | `settings_registry.py` | one `_wave(...)` entry per new variable |
| Docs | `docs/wiki/Reference.md`, `docs/wiki/Choosing-an-ASR-Model.md`, `docs/AGENT-GUIDE.md`, `CHANGELOG.md`, `.env.example` | T2.7 |
| Evidence | `docs/asr-bakeoff/whispercpp-<date>.md` | T1.7 report — metrics only |

## Risks and mitigation

| Risk | Mitigation |
| ---- | ---------- |
| The entrant silently measures a different code path than production | It calls `generate.endpoint_transcribe` directly, with the endpoint globals swapped and restored in a `finally` |
| Derived logprob read as if it were native | `logprob_source` is recorded on every entry and compared columns are labelled |
| A bakeoff URL leaks into the process for later runs | Globals restored in `finally`; covered by a test |
| Dub text reaches the repo through a report | T1.2 `--metrics-only`; the existing hygiene tests plus a test that `texts`/`words` are absent |
| Changing the live server's flags before evidence exists | T1.0 adds a SECOND llama-swap entry (`-subs-v2`) and leaves the running one untouched; promotion happens only if the gate passes |
| Adding a key to the endpoint identity re-transcribes the library | T2.4 is explicitly gated on maintainer approval; nothing lands without it |
| A flaky endpoint makes the entrant look slow | Per-episode errors are recorded on the episode (the tool's existing rule); the run continues |

## Rollback and reversibility

- **Instrument (PR 1):** reverting the PR is enough. It adds a tool path and tests; no
  runtime behaviour changes, and no instance reads it.
- **Phase 2 (PR 2):** every change is behind a setting whose default preserves today's
  behaviour (`ASR_UPLOAD_FORMAT=mp3`, fallback off when `ASR_FALLBACK_URL` is empty,
  DTW/tag off). Reverting the PR, or unsetting the variable, returns the previous behaviour.
- **The one irreversible item** is T2.4: adding a key to `decoder_identity()` invalidates
  stored words for endpoint-transcribed episodes and re-transcribes them. That is why it is
  gated on explicit approval and shipped last. It is flagged as a risk rather than assumed.
- **Nothing in this plan touches the live library.** T1.5 writes to separate `OUTPUT_ROOT`s;
  T1.0's edit is the maintainer's to apply, on a second entry that changes nothing until a
  caller selects it.
