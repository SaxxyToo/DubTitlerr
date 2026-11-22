# DubTitlerr

**Dubtitles for anime: subtitles that say what the English dub actually says.**

Most anime already ships an English subtitle track, but it is a translation of the _Japanese_
audio — different wording, sometimes different meaning. Watching a dub with it on gives you
two scripts at once. DubTitlerr transcribes the dub audio itself, corrects the proper nouns,
and writes the result back into the file as a subtitle track your player picks up.

It runs as one container, per episode, incrementally.

> **This is a beta.** It is validated end to end on One Pace, Sword Art Online and Digimon. Other shows
> work and are also where the bugs are found. Version stamps may be invalidated without
> notice: pin your image tag.
> If you have pulled a newer image but are not ready to re-transcribe, stop the container until you are.

---

## Start here

| I want to…                                  | Go to                                                                                     |
| ------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Get it running on one show, start to finish | **[Your first show](Your-First-Show.md)**                                                 |
| Solve one specific problem                  | **[How-to guides](How-To-Guides.md)**                                                     |
| Look up a variable, a filename, a schema    | **[Reference](Reference.md)**                                                             |
| Understand why it behaves the way it does   | **[Why it works this way](Why-It-Works-This-Way.md)**                                     |
| Pick a transcription model for your GPU     | **[Choosing an ASR model](Choosing-an-ASR-Model.md)**                                     |
| Pick the model that repairs the lines       | **[Choosing a repair model](Choosing-a-repair-model.md)**                                 |
| See the numbers behind the model choice     | **[ASR bakeoff data](ASR-bakeoff-data.md)**                                                |
| Read a design decision before reversing it  | **[Design decisions](Design-decisions.md)**                                                |
| Point an AI agent at the project            | **[Agent notes](https://github.com/SaxxyToo/DubTitlerr/blob/main/AGENTS.md)**              |

---

## What it does, per episode

1. **Transcribe** the English dub audio with Whisper, then reflow it into readable cards —
   sentence-split, at most two lines, timed to the spoken onset.
2. **Correct names** against a per-show glossary that is mined from your own files and
   checked against the show's wiki.
3. **Repair** the low-confidence and name-suspect lines with a small language model, local or a hosted API,
   anchored on the existing subtitle track where you have one.
4. **Gate** the result — drop hallucinated music and silence lines, collapse runaway
   repetition, and flag what it is unsure about for you to read.
5. **Merge** the on-screen signs and song lyrics back in.
6. **Mux** the finished track into the file with its original fonts, so signs render in their
   real typeface.

The last step is a choice. By default the track is muxed into the episode; `OUTPUT_MODE=sidecar`
writes `<episode>.dub.en.ass` beside the video instead and never opens the video for writing, which
is the safe mode for seeded or hardlinked files.

Then it stops and waits for you. The lines it was unsure about are queued at
**http://localhost:8842**, and a verdict you give there applies to that line everywhere in
the show.

---

## What it does not do

- It does not translate. It transcribes English audio.
- It does not proofread. The review queue holds the lines the pipeline **doubted** — the ones
  it was confident about were never shown to anyone, and some are wrong.
- It does not re-mux an episode because you saved a verdict. That is a separate button, on
  purpose.

---

## Requirements

- **Transcription** takes one of two paths. Either an NVIDIA card with CUDA — 6 GB works, see
  [choosing a quantisation](How-To-Guides.md#choose-a-quantisation-for-your-card) — or an
  OpenAI-compatible `/audio/transcriptions` endpoint, which needs no GPU, no model and no torch at
  all. The lite image exists for the second path.
- **Docker.** The NVIDIA container runtime is needed only for the GPU path.
- An OpenAI-compatible endpoint for repair: a small GGUF model you serve yourself, or a hosted
  API with a key (see [Repair with a hosted model](How-To-Guides.md#repair-with-a-hosted-model)).
- A library with English dub audio, laid out as `Show Name (Year)/Season 01/…S01E01…`.

**Image:** built from this repository. `Dockerfile.builder` is the standalone full build (Whisper
model baked in, review UI built); `Dockerfile.lite` is endpoint-only and under 1 GB. There is no
third "fork overlay" image: both images ship this repository's code.

---

## Roadmap

- **Arr API and poster library view** - connect directly to Sonarr and Radarr APIs to present a poster grid with status bars, replacing filesystem walks and letting you set a show's wiki URL before its glossary exists.
- **Run a show again from the Runs page** - a show that already ran is skipped. Queueing it again
  will ask whether you want to run it again or cancel, so you don't redo one by mistake. Run again
  redoes only that show and season. This helps after a new version changes the output.
- **Season ranges in run requests** - a request on the Runs page targets one season or the whole
  show. It should also take a range of seasons, so queueing a show's early arcs is one request
  instead of one per season.
- **Remove a queued request** - queued requests can be added but not withdrawn yet. The Runs page
  should let you remove one before it starts.
- **What the current run is doing** - a status section on the Runs page showing the show, episode
  and stage being worked on right now.
- **Wiki scope for more shows** - the scoped wiki lookup is built and tested live against Naruto,
  Pokémon and two one-season shows. What is left is trying it on each show's real library and
  writing the `wiki_scope` block for the shows people actually run.
- **Reference lines from a published subtitle file** - repair anchors on the English subtitle track
  embedded in the video. Dub re-cuts made from another release carry none, so repair works from the
  glossary alone. The idea is to match each dub line, by its words, to a published English subtitle
  file for the same scene, allowing the timing offset to change partway through an episode. A
  first blind test on three episodes of one arc preferred the referenced output over the
  glossary-only output on 34 of 65 contested lines, against 7 the other way. It needs a wider test
  and a spot-check before it is built.
- **Community glossary repo** - [XenaRathon/DubTitlerr-glossaries](https://github.com/XenaRathon/DubTitlerr-glossaries)
  is the shared repository of per-show glossaries. Planned for v1.0.0 or a later 1.x release:
  downloading glossaries from it and uploading yours back, from inside the app. Downloads will
  never overwrite your own fixes or a wiki you set. Today it is a `git clone` into your
  `GLOSSARY_DIR` and a `git pull` to update, which `dubtitlerr/core/decisions.py` already documents as the
  intended flow.

  **This repository's `glossaries/` directory is a snapshot**, used by the tests and by the
  examples in these docs. The pipeline reads whatever is mounted at `GLOSSARY_DIR` and never
  the copy in its own source tree, so the shared repository is the one to install.
