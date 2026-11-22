# DubTitlerr — agent notes

DubTitlerr transcribes English-dub anime audio into subtitles, repairs them with an LLM, merges the signs/songs track, and muxes the result into a live media library. Treat it as production media infrastructure: a bad run burns hours of GPU time or leaves an episode's subtitle track worse than before.

`README.md` is the human entry point. This file holds the working rules for an agent changing the code.

## Working rules

- **Layout:** the pipeline lives in the `dubtitlerr/` package (`core/`, `pipeline/`, `glossary/`, `review/`, `settings/`); the shells at the repo root drive stages as `python3 -m dubtitlerr.<sub>.<module>`. There are no top-level pipeline scripts.

- **Commits** follow Conventional Commits with a scope where one fits: `fix:`, `feat:`, `docs:`, `test(web):`, `chore:`.
- **Checks before pushing:** `scripts/check.sh` runs ruff then the full suite; `scripts/check.sh --dash` also runs the shell loops under dash; `scripts/check.sh --cov` adds branch coverage. All finish clean before a PR. CI also runs CodeQL, dependency review, shellcheck, a Dockerfile.lite build and the documentation link check.
- **Write the failing test first.** Tests mock HTTP and model calls and use fake credentials.
- **Validate on a spread of shows**, never one: one with a fansub track and one without, one whose glossary came from acquisition and one that was only mined. One Pace is the least representative title, because it holds nearly all the curated `hard_fixes`.
- **One worker per library.** Nothing in the code locks across workers. Two workers on one library race on `<stem>.muxtmp.mkv` and re-transcribe each other's episodes. Building a real claim/lock is open work; a change that assumes a lock exists is a bug.
- **Persist incrementally** in any expensive stage: write per item or per batch (append-only JSONL is the house idiom; see `dubtitlerr/core/unresolved.py`), so a killed run keeps the work it finished. A longer timeout can unblock a run, but the fix is incremental writes.
- **Write product sidecars through `common.atomic_write`** (temporary file plus `os.replace`), so a failed write leaves the previous output intact.
- **Record each stage result through `common.write_stage`** with a name from `common.STAGE_OUTCOMES`; a failed outcome blocks mux.
- **`TRANSCRIBE_VERSION` and `TEXT_VERSION`** in `dubtitlerr/core/common.py` decide whether a change reaches already-processed episodes; read their history before deciding on a bump.
- **Glossaries are a shipped deliverable.** `glossaries/*.json` are pre-seeded dictionaries for everyone who clones the repo. The glossary a deployment actually uses lives in its config volume and drifts from the repo copy, so check the deployed one when debugging a live library.
- **LLM calls speak the OpenAI-compatible REST API**, so hosted endpoints and local servers (llama.cpp, Ollama) stay drop-in replacements for each other. A `503 Loading model` from the repair backend means the model is still warming up; check the backend before treating it as a pipeline regression.
- **`.ass` output keeps its override tags** (`\pos`, `\an`, `\fad`), and sign/song events stay anchored where the source placed them.
- **No secret enters the tree.** Config files, command-line arguments, logs, fixtures and commits hold only the environment variable's name.
- **Ask the maintainer first** when a change touches card timing, transcript provenance, output-version migration, glossary admission policy, or anything that replaces media in place. These decide what ships into a library.
