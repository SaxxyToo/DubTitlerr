# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

A single expert operator — the deployment's owner — runs DubTitlerr end to end and makes every review decision. Multi-account support exists in the code, but there are no other regular human users in practice; the interface may assume expert knowledge, and onboarding for strangers is not a goal. Review happens on a desktop with headphones in a dark-first workspace, in focused passes: waveform audio, keyboard verbs, dense information. The same workspace is used from a phone: the panels stack in one column, in the same order, with touch-sized controls.

## Product Purpose

DubTitlerr transcribes English-dub audio into subtitles, repairs low-confidence lines with an LLM against a per-show glossary, and merges the result into a live media library as sidecar subtitle tracks. The review workspace is where the operator inspects every risky repair — original transcript, model proposal, reference track, and the dub audio in one view — and decides: keep the repair, keep the original, or write a correction. Nothing ships without those verdicts. Success means the library's subtitle tracks stay trusted end to end, and reviewing keeps pace with the pipeline.

## Positioning

Machine repair under human authority: every proposal is reviewable evidence, verdicts are staged and explicitly applied, and nothing is auto-approved. It is an operator's instrument, not a consumer product — speed and trust outrank presentation.

## Operating Context

- Runs in a homelab media stack (Plex/Jellyfin, *arr tooling, local LLM backends); the review workspace is served from the same container as the pipeline.
- Typical loop: sweeps transcribe and repair overnight; the operator opens the review inbox, decides flagged lines in a keyboard-driven pass (keep repair / keep original / write correction; J/K to move; Space to hear the dub), then applies verdicts, which queue a remux.
- The operator is fluent in subtitles, dubbing, .ass conventions, reference tracks and ASR confidence — none of it needs explaining in the UI.
- The workspace also carries the wider control surface (glossary review queue, run queue, settings, accounts, notifications and API surfaces); deciding repairs is its center of gravity.

## Capabilities and Constraints

- Single-page web app (Svelte 5 + TypeScript + Vite) served by the Python review server; the server-rendered classic page remains as the fallback when the workspace is not built, and for `?stem=` deep links. `/` redirects to `/review/` once the workspace is built.
- Accounts, roles, audit logging, and API keys exist and must keep working.
- Verdicts are staged, then applied; applying queues a remux — review actions reach the real library, so the UI must communicate consequence honestly and never imply automation it cannot ensure.
- Keyboard operability and WCAG AA contrast are standing commitments; CI runs svelte-check, unit tests, and Playwright browser tests including axe scans.
- Open decision: whether a light theme ever joins the dark-first default.

## Brand Commitments

- Name: **DubTitlerr**. The waveform/audio-lines icon concept is the starting point for the mark; everything else visual is open to replacement.

## Evidence on Hand

- An isolated fixture environment for design work: `web/tests/fixture_server.py` serves synthetic shows, flagged repairs, and real audio streams with no library access; screenshots of the current UI were captured during recon.
- No marketing assets, logos, testimonials, or claims exist — and none may be invented.

## Product Principles

1. Trust is the feature — every machine proposal appears with its evidence; only the human verdict ships.
2. Built for one expert — density, keyboard speed, and precision over hand-holding.
3. The operator's attention is scarce — surface what needs a decision; collapse what doesn't.
4. Reviewing must keep pace with the pipeline; anything that slows a decision pass is a defect.
5. Honest instruments — show real state without decoration or invented claims.

## Accessibility & Inclusion

- Keyboard-first operability (the flow already runs on single-key verbs), WCAG AA text contrast, and reduced-motion support; automated axe checks gate every page in CI.
