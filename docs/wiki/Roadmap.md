# Roadmap

> The fork's plan, deliberately deeper than the upstream wiki roadmap: every upstream
> item appears here with a status line for this fork, and everything else is
> accumulated from the program ledger, the running deployment, and the review queue.
>
> Release rhythm: **batched**. Merges accumulate on `main`; a release is cut when the
> owner decides the moment. One version carries the batch. Rollback is the previous
> immutable GHCR tag.

---

## Where this deployment is today

- **App:** v0.4.23 live on saxserver (`ghcr.io/saxxytoo/dubtitlerr:v0.4.23-lite`),
  healthy, routed at `dubtitlerr.nalasden.cc` / `.saxlab.dev`, config + library store
  migrated (358 MB artifacts.db, 881 processed stems).
- **Pipeline:** the generation wave over the library is complete; sweeps run clean.
  Parked: two restoration-stale episodes (Eighty Six S01E09, Afro Samurai S01E02 —
  fixed by the unreleased scope fix below); the AoT S01E11/E16 pair (audio-gap,
  owner verdict pending).
- **Gaps in coverage:** Apothecary Diaries S03E06 (fillable now; dual-audio release
  exists) and S03E07 (dub not aired yet).
- **Review:** 753 episodes carry flagged triage items on the review surface
  (heaviest: Cyberpunk S02E01, Hell's Paradise S02, FMA:B S01E41, DDD S58-59).
- **LLM stack:** llama-swap on omarchy is the single LLM host for all stages
  (`qwen3.5:4b` repair/restore/verify, context tightened to fit the shared 8 GB card).

---

## Now — in flight

| Item | State |
| --- | --- |
| **Restore-lane scope fix** (PR #124) — `RESTORE_*` rows never reached the generate-scope pass, so restore could not call its model | PR open, green-ish; merge on sight; **required for the two stale episodes** |
| **Registry scope audit fix** (branch `370eded`) — five generate-read rows were merge-scoped; scan test pins the family | Branch verified; push + PR + batch |
| **Waveform follows audio** (`t_c2a8f2cf`) — real-time playhead + click-to-seek | In build |
| **Decision-model seat** — automated adjudication (glossary decisions) is unconfigured; default would be a hosted free tier with no key on this host | **Pick the winner from the r2/r3 candidates; wire it; verify against the review queue** |
| **Library fill: Apothecary S03E06** | Queue the request when convenient |

---

## Next — candidates for upcoming batches

1. **Settings deep-keys** — the 11 deliberately held-back rows gain meaning with use:
   `ANIME_ORDER`, `DECISIONS_DIR`, `REQUESTS_DIR`, `SCHEDULE_FILE`, `HEARTBEAT_PATH`,
   `RESCAN_INTERVAL`, `GLOSSARY_LOCK_WAIT`, `WIKI_ALLOW_HTTP`, `WIKI_HOST_ALLOW`,
   `ACQUIRE_NO_CACHE`, `VERIFY_NO_JOURNAL`. Each needs per-key rationale before it
   graduates from read-only, exactly like wave 2.
2. **Review-queue triage program** — work the 753 flagged items to zero, heaviest shows
   first; the queue is the product's honest edge, and clearing it retires the
   "not sure" surface.
3. **Ops retirement** — decommission the stopped omarchy rollback container (keep image),
   retire/repoint omarchy's config-backup timer to the saxserver instance.
4. **Upstream ports with a live need** — season ranges in requests and request removal
   (see table below); both are cheap ports whenever the Runs page next gets attention.
5. **Community glossaries** — adopt the upstream `DubTitlerr-glossaries` repo into
   `GLOSSARY_DIR` for the shows that have entries.
6. **Arr link (scoped)** — read-only Sonarr/Radarr consumption: dub-availability
   detection (feeds the fillable-gap workflow directly), a coverage view joining arr
   statistics with the artifacts store, and wiki-URL setup before a glossary exists.
   Design the surface (a Shows view vs. additions to the Runs page) before building.

---

## Adapted from the upstream roadmap — status per item

| Upstream item | Status here |
| --- | --- |
| **Arr API and poster library view** | ⭐ **Worth a look (owner, 2026-11-23).** Live probe: Sonarr 148 series (145 with files), Radarr 782 movies (758) — and every episodeFile carries **`mediaInfo.audioLanguages`** (`jpn/eng`, `eng/eng/jpn/jpn`, …) plus subtitle-language lists. Concrete uses for this fork: **dub-availability detection** (jpn-only files are exactly the "fillable gap" class — e.g. 2 such files in Apothecary), **coverage view** (join arr statistics with the artifacts store — 881/918 at a glance), setting a show's wiki URL before its glossary exists, and poster art in the review surface. Caveats: mediaInfo is as-of-the-last-arr-scan (stale after external edits — AoT S02E07 still shows its pre-EAC3-fix streams); scope = read-only API consumption, no writes. |
| **Run a show again from the Runs page** | Half-buried: `force` in a run request already carries through the queue into generate (P36), which is the plumbing the feature needs. The UI affordance ("this already ran — run again or cancel?") is not built. |
| **Season ranges in run requests** | Not built here. Low pressure (the queue is drained and new seasons self-queue), but a cheap port if wanted. |
| **Remove a queued request** | Not built here. Same assessment. |
| **What the current run is doing** | Largely present (`review_status.py` feeds live state; the UI shows what's processing). Extend only if the Runs page needs a richer stage view. |
| **Wiki scope for more shows** | Mechanism built and in use (per-show wiki override + `wiki_scope`). The remaining work is content: write scope blocks for our specific library shows as they come up. |
| **Reference lines from a published subtitle file** | Strong candidate for this fork — it targets exactly our AoT-class problem (dub re-cuts with no embedded English track get glossary-only repair). Upstream's blind test (34–7 preference over glossary-only on contested lines) needs the wider test they describe before building. |
| **Community glossary repo** | Adopt the clone/pull flow upstream documents; local adoption is a config action, not code. |

---

## Model & accuracy track

- **ASR:** running `large-v3-sub`-class models via llama-swap. The bake-off data and
  model guidance live in `docs/wiki/ASR-bakeoff-data.md` and
  `Choosing-an-ASR-Model.md`; upstream's whisper-turbo + beam-size experiments are the
  obvious next test if transcription quality ever needs revisiting.
- **Repair:** `qwen3.5:4b` today. Candidates (per upstream's research and our own
  roster): the r2/r3 quantised variants tested for the decision seat. Any change walks
  the same bake-off procedure the wiki documents.
- **Decision/adjudicator:** unconfigured (see Now). Upstream's default is a hosted
  free-tier adjudicator ("Jev"); on this host that is keyless-unreachable, so a local
  candidate should take the seat.
- **Watch item — hosted ASR retirement:** `openai/whisper-1` (the hosted-API path's
  default) retires 2027-02-26, and no disclosed successor returns word timestamps.
  Only relevant if a hosted ASR path is ever adopted; endpoint ASR here is unaffected.

---

## Hard problems / watch items

- **Multi-audio targets:** episodes whose dub is a re-cut of a different release carry
  no anchor track; repair quality then rests on the glossary alone. Affects the parked
  AoT pair and appears whenever a show is sourced from a non-standard release.
- **Single-GPU contention:** all LLM work shares one 8 GB desktop card with the ASR
  models; context/quantisation budgets are load-bearing (the reason the repair model
  runs at 8k/q8 rather than 32k).
- **Glossary growth:** community glossary adoption adds names without adding local
  review; the glossary audit tooling is the guard, and it should run after bulk pulls.

---

## Deliberately not doing (and why)

- **External scripts or cron agents driving the pipeline** — the application owns its
  own work; operational tooling is limited to deploys, verification and backups.
- **Per-PR releases** — merges are free and invisible; releases are deliberate batches.
- **A second, mobile-specific UI** — one responsive surface, sized properly for touch.
- **A stale static manifest as a "search" or catalogue** — anything served must be
  generated or fetched for what it is.

---

## Release & delivery mechanics (for reference)

1. Work lands on `main` via PR with the full gate green.
2. Owner calls the moment; the release PR bumps `__version__`, merges, and the annotated
   tag is pushed → pipeline builds both image variants and publishes the release page.
3. Images are verified by pulling them and reading the version/revision labels baked
   into the config blob — never by trusting the pipeline's own exit code.
4. Deployment = template image bump in the Unraid GUI (Apply), followed by health,
   route and boot-banner verification plus a live stage call.
