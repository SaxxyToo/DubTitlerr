# Contributing

Thanks for looking. DubTitlerr transcribes English-dub audio into subtitles, repairs them with a
language model, merges the signs/songs track, and writes the result into a media library. A bad
change can burn hours of GPU time or leave an episode's subtitle track worse than before, so the
bar is deliberate: small changes, real evidence, no silent behaviour changes.

This project is a fork of [XenaRathon/DubTitlerr](https://github.com/XenaRathon/DubTitlerr) and is
GPL-3.0. Keep the upstream credit.

## Setup

- **Python 3.11 or newer.** `uv` is what the checks use; `pip install -e ".[dev]"` works too.
- **ffmpeg and mkvtoolnix** if you are exercising the pipeline (the images bake both in).
- **Docker** only if you are testing the images or the shell loops under dash.
- The repair stage calls an OpenAI-compatible chat endpoint; tests mock every model call and need
  none.

```sh
uv venv && . .venv/bin/activate
uv pip install -e ".[dev]"
```

## Before you change code

Read [`AGENTS.md`](AGENTS.md). It is the working contract: atomic writes, stage outcomes,
incremental persistence, version stamps, and the ask-first list.

**Ask the maintainer first** when a change touches card timing, transcript provenance,
output-version migration, glossary admission policy, or anything that replaces media in place.
Those decide what ships into a library.

Write the failing test first. Tests mock HTTP and model calls and use fake credentials.

## Checks

```sh
scripts/check.sh          # ruff + the full Python suite
scripts/check.sh --dash   # also the shell loops under dash (needs Docker)
```

Both must finish clean before a PR. CI runs the same gate on Python 3.11 and 3.13, plus the review
UI's svelte-check, build, unit tests and Playwright suite.

If you touched the web UI:

```sh
cd web
npm ci
npm run check
npm run build
npm test
npx playwright test
```

## Where things live

| Path | What |
| --- | --- |
| `dubtitlerr/core/` | Shared leaves: atomic file, settings state, reference tracks, decisions |
| `dubtitlerr/pipeline/` | generate → repair → hallucination → reflow/punctuation → signs merge → mux/publish |
| `dubtitlerr/glossary/` | Mining, acquisition, verification, locking |
| `dubtitlerr/review/` | Web server, API v1, glossary editor, run requests |
| `dubtitlerr/settings/` | Registry, store, connections, notifications |
| `web/` | Svelte review UI |
| `gen_loop.sh`, `merge_pass.sh`, `container_run.sh` | Supervisors; the most fragile surface |
| `docs/wiki/` | Tutorial, how-to, reference and rationale pages |

## Pull requests

- Conventional Commits with a scope where it fits: `fix:`, `feat:`, `docs:`, `test(web):`, `chore:`.
- One concern per PR. A version bump is its own PR.
- Fill in the PR template; "tests pass" needs the command and its result.
- Do not commit secrets, config volumes, media, or the `graphify-out/` cache.
- A merge is not a release. Tags are cut deliberately from `main`; see
  [Cut a release](docs/wiki/How-To-Guides.md#cut-a-release).

## Reporting a problem

Include the release (`python3 -m dubtitlerr --version`), the stage outcome
(`<stem>.dubtitles.stages.json`), and the relevant log lines. A publish-gate or stage failure
without its detail is not reproducible.
