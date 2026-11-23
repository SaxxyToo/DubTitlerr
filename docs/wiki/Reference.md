# Reference

Information-oriented. Values, shapes and filenames, looked up rather than read.

Every default below is the value in the code, not the value in a compose example. Where a
default differs between modules, that is stated rather than smoothed over.

---

## Container

**Image:** pulled from GHCR, built by the release workflow from this repository
(`ghcr.io/saxxytoo/dubtitlerr:<vX.Y.Z>` for the full build omarchy runs; `:<vX.Y.Z>-lite` for the
endpoint-only image)

**Tags:** the release's git tag (`vX.Y.Z`), annotated, on the merge commit in `main`. Pushing it
runs `.github/workflows/release.yml`: the full CI gate re-runs, then both images publish in
parallel — `ghcr.io/saxxytoo/dubtitlerr:vX.Y.Z` and `:latest` (builder),
`:vX.Y.Z-lite` and `:latest-lite` (lite). Hosts pull instead of building; the moving `:latest`
follows the newest release, never `main`. A locally date-suffixed tag
(`dubtitlerr:rollback-2026-11-19`) is the rollback slot for a same-version rebuild — see
[Cut a release](How-To-Guides.md#cut-a-release).

**Entrypoint:** `container_run.sh`, which starts three loops in one container:

| Loop     | Script             | Cadence                                   | Work                                              |
| -------- | ------------------ | ----------------------------------------- | ------------------------------------------------- |
| Generate | `gen_loop.sh`      | continuous, then `RESCAN_INTERVAL` idle   | GPU: mine, acquire, verify, transcribe            |
| Merge    | `merge_pass.sh`    | every `MERGE_INTERVAL`                    | CPU + LLM: repair, merge signs, mux, Plex refresh |
| Review   | `dubtitlerr/review/review_server.py` | restarts after `REVIEW_RESTART` s on exit | HTTP review UI                                    |

**Stage order within `gen_loop.sh`:** `dubtitlerr.pipeline.watch_queue` → `dubtitlerr.glossary.mine_glossary` →
`dubtitlerr.glossary.glossary_acquire` → `dubtitlerr.glossary.glossary_verify` → `dubtitlerr.pipeline.generate`

**Stage order within `merge_pass.sh`:** `dubtitlerr.pipeline.repair` → `dubtitlerr.pipeline.dub_signs_merge` →
`dubtitlerr.pipeline.mux` → `dubtitlerr.pipeline.plex_refresh`

Script stems are shorthand for the pipeline package: `generate.py` is `dubtitlerr/pipeline/generate.py`,
`common.py` is `dubtitlerr/core/common.py`, `glossary_*` live in `dubtitlerr/glossary/`, `review_*` in
`dubtitlerr/review/`, `settings_*` in `dubtitlerr/settings/`. The shells drive stages as
`python3 -m dubtitlerr.<sub>.<module>`.

`mine_glossary.py --bootstrap` is the one manual exception to that order: it re-mines a show's
already-delivered episodes to create a first glossary, and gen_loop never passes it. See
[Bootstrap a glossary for a show that shipped without one](How-To-Guides.md#bootstrap-a-glossary-for-a-show-that-shipped-without-one).

A failed repair or signs stage skips mux for that episode. The episode is retried on the next
pass. See [Stage outcomes](#stage-outcomes).

**Stopping:** the entrypoint traps SIGTERM and SIGINT and does not forward them to the work. It
creates the stop flag file (`STOP_FLAG`, default `/tmp/dubtitlerr.stop`) and prints
`soft stop requested`. While the flag exists, no loop starts a new show, episode, glossary step
or mux. The unit already running finishes, then the container prints `soft stop complete` and
exits 0. The flag is deleted at startup, so a stale flag never stops a fresh run. What the fresh
run then does is decided by the persisted processing state (below), not by a hard-coded boot.
`docker stop` sends SIGKILL once its `-t` grace period is over (10 seconds by default), so set
`-t` above your longest single unit of work. Inside the glossary steps, `mine_glossary.py`,
`glossary_acquire.py` and `glossary_verify.py` also check the flag between episodes, terms and
escalation pairs. Releases up to 0.2.2 check it only before a glossary step starts, so an
acquire step runs to its end or to `ACQUIRE_TIMEOUT`.

The container runs as **root** so the pipeline can chown into a bind-mounted media tree:
`python3 -m dubtitlerr.pipeline.generate` writes each sidecar as `MEDIA_UID:MEDIA_GID`, and
`chown` needs `CAP_CHOWN`. The two supported images are `Dockerfile.builder` (local Whisper,
review UI, ~13 GB) and `Dockerfile.lite` (endpoint ASR only, under 1 GB); `Dockerfile.builder`
is the recommended full build.

---

## Version

**Source of truth:** `dubtitlerr/__init__.py`, the `__version__` literal. One string, one file.
`pyproject.toml` declares `dynamic = ["version"]` and reads that attribute; `uv.lock` stores no
version for the project; `tests/test_version.py` fails if any surface below stops agreeing with it.

| Surface | What it shows |
| --- | --- |
| `python3 -m dubtitlerr --version` | `dubtitlerr X.Y.Z` — on the host, in the image, no network |
| Container boot log (`docker logs`) | `==== dubtitle-builder up dubtitlerr X.Y.Z …` and `#### gen_loop up dubtitlerr X.Y.Z …` |
| `GET /api/v1/system/status` → `version.app` | `"X.Y.Z"` (always a string; the field is no longer null) |
| Review page sidebar footer | `DubTitlerr X.Y.Z`, fetched from that route after sign-in |
| `docker inspect <image>` | `org.opencontainers.image.version`, `…revision` (git sha), `…title`, `…source` |

`version.transcribe_version` and `version.text_version` are a different thing: the pipeline's
output-contract stamps from `dubtitlerr/core/common.py`, which decide whether a change re-processes
already-processed episodes. A release that bumps either of them says so in its notes.

**Docker build arguments:** `DUBTITLERR_VERSION` (default `dev`) and `VCS_REF` (default `unknown`).
Both images take them; no image hardcodes the version, because a literal in a Dockerfile would
survive a bump and label the wrong release. The release workflow passes both from the tag and the
checkout (`version` = the tag minus its `v`, `revision` = the short sha), so a published image
labels itself correctly with no build command to remember.

**Cutting a release:** semver per [Cut a release](How-To-Guides.md#cut-a-release); the tag is
annotated (`vX.Y.Z`) and points at the merge commit on `main`.

---

## Staleness

An episode is **done** only when its `.dubtitles.done` stamp matches the file on disk (size and
mtime), is current in both output tiers, and was produced by the decoder running today. Everything
else is stale and is regenerated the next time the pipeline reaches it.

| Signal in the stamp | Current value comes from | Stale when |
| --- | --- | --- |
| `transcribe_version` | `common.TRANSCRIBE_VERSION` | behind — audio → words changed |
| `text_version` | `common.TEXT_VERSION` | behind — anything after the word list changed |
| `asr_backend` + `asr_model` | `common.decoder_stamp_identity()` (`ASR_BACKEND`, then `ASR_MODEL` for the endpoint backend or `WHISPER_MODEL` for the local one) | different from today's, **or absent** |

A **model change is not a version bump.** Switching `WHISPER_MODEL` or `ASR_MODEL` changes the
transcript without changing the code, and the tier numbers cannot see it, so the decoder identity is
recorded in every new stamp and compared directly. A different model marks the episode stale in
**both** tiers — new words invalidate everything derived from them — which is the same shape as a
`TRANSCRIBE_VERSION` bump. Neither constant is raised for this: a bump would mark the whole library
stale at once, and a model change is an operator action, not a release.

**Grandfathering.** A stamp with no `asr_model`/`asr_backend` was written before the model was
recorded, i.e. by a decoder that is very likely not the one running now. It reads **stale**, not
"unknown, so assume fine" — that population is the gap this check exists to close (~200 episodes at
introduction). Moving back to a model an episode already recorded makes it current again: the
comparison is against the recorded identity, not a one-way ratchet.

**No bursts.** Staleness only marks the episode; nothing rebuilds it here. The generate loop walks
the watch-queue order file, so a library-wide model change re-transcribes the shows actually being
watched, one sweep at a time. A watched show does regenerate in full, because a subtitle track's
value is in the episodes ahead of the viewer. To rebuild a single episode or show on demand, use a
review request (`generate` scope) — a full-library rebuild is never automatic.

`stamp_valid()` is still exactly "nothing stale", so every existing caller keeps its meaning:
generate skips a stamped episode, mux treats it as delivered, `tools/export_subtitles.py` exports it.
The one behaviour worth knowing: on an install that predates the model field, those callers will see
the whole library as stale until each episode is regenerated once.

---

## Processing state and controls

The app can be **up without touching the library**. That is the boot default: starting the
container brings up the review UI and API, and processing waits for an explicit Start.

The state is one tiny JSON file in the config volume, so it survives a recreate:

`$CONFIG_DIR/processing.json` (`PROCESSING_STATE_FILE` overrides the path):

```json
{"state": "paused", "by": "user:alice", "at": "2026-11-19T18:02:11Z"}
```

| State     | What the loops do                                                                                                                                                    |
| --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `running` | Work at the normal cadence.                                                                                                                                          |
| `paused`  | Finish the unit in flight (show, sweep, stem, term), then hold at the boundary, polling every `PROCESSING_POLL_INTERVAL`. UI and API stay up. Nothing is killed.       |
| `stopped` | The existing soft stop: persist `stopped`, write `STOP_FLAG`, drain the unit, exit the container. With a restart policy the UI returns idle; Start resumes.            |

**Boot honors the file.** `container_run.sh` writes `paused` only when the file is missing (by
`boot`), then starts the loops. A stored `running` behaves like earlier releases; `paused` and
`stopped` start no sweep and no merge pass but keep the review server. A corrupt or unknown file
is read as `paused`. A missing file is `paused` for the app; a stage run directly outside the
container with no state file keeps working, because there is no app boot to honor.

**Controls.** Settings → General shows the current state, who changed it and when, with Start /
Pause / Stop for an admin. The same transitions are available as API routes:

| Method | Path                     | Role   | Effect                                            |
| ------ | ------------------------ | ------ | ------------------------------------------------- |
| GET    | `/api/system/processing` | `user` | Current state, `by`, `at`, and whether it is the boot default |
| POST   | `/api/system/start`      | `admin` | Persist `running`; clear a stale `STOP_FLAG`; 202 |
| POST   | `/api/system/pause`      | `admin` | Persist `paused`; 202                              |
| POST   | `/api/system/stop`       | `admin` | Persist `stopped`; write `STOP_FLAG`; 202         |

The `/api/v1` command endpoint carries the additive commands `system.start`, `system.pause` and
`system.stop`; `GET /api/v1/system/status` reports the same `processing` object.

**Host-side, with the container down.** The deploy flow should set `paused` before a recreate:

```sh
python3 -m dubtitlerr.core.processing_state pause --config-dir /path/to/config --by deploy
python3 -m dubtitlerr.core.processing_state show --config-dir /path/to/config
```

`init` writes `paused` only when no file exists; `start`, `pause` and `stop` are the same
transitions as the API. `--stop-flag PATH` lets `stop` also write the flag outside the container.

**Boundary points.** Generate holds before a sweep, before each show and while idling; merge
holds before each stem; glossary verification holds before each term. A request that was
running when a stop arrived is re-queued exactly as before.

Notifications for state changes are deliberately not emitted: the state file and the Settings
panel are the reporting surface, and a notification failure must never be able to affect the
control path.

---

## Per-episode files

Only three things belong beside the video: the media itself, the delivered subtitle
(`.eng.dubtitles.srt` / `.eng.dubtitles.ass`, and whatever `SIDECAR_SUFFIX` names), and
nothing else. Everything else in the table below is an **internal artifact**: it is moving to
the central store described under [Internal artifact store](#internal-artifact-store-p39), and
a kind stays in this table only until its writer and every reader are migrated in the same
change. The merged `.ass` keeps the same `...eng.dubtitles.*` name, so it still shows up as the Dubtitles track with signs included. If the sidecar came from subgen with `SKIP_IF_EXTERNAL_SUBTITLES_EXIST`, replacing the `.srt` with the merged `.ass` does not trigger a re-transcribe loop.

| Suffix                           | Written by                                                   | Read by                                      | Contents                                                                                                            |
| -------------------------------- | ------------------------------------------------------------ | -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `.eng.dubtitles.srt`             | `generate.py`, `repair.py`, `review_apply.py`                | `dub_signs_merge.py`, `mux.py`               | The dialogue transcript from the dub audio                                                                          |
| `.eng.dubtitles.ass`             | `dub_signs_merge.py`, `review_apply.py`                      | `mux.py`                                     | Dialogue plus signs, songs and credits                                                                              |
| `.dubtitles.conf.json`           | `generate.py`, `repair.py`                                   | `repair.py`, `glossary_acquire.py`, `mux.py` | Per-cue `avg_logprob` and `no_speech_prob`                                                                          |
| `.dubtitles.words.json`          | `generate.py`                                                | `generate.py`                                | Per-word confidences and audio duration, plus the decoder identity (`backend`, `model`) it was produced under; lets a text-tier re-run skip the GPU |
| `.dubtitles.done`                | `common.py`, `mux.py`                                        | `generate.py`, `mux.py`                      | Completion stamp: muxed size, mtime, tier versions, decoder identity (`asr_backend`, `asr_model`)                    |
| `.dubtitles.fail`                | `generate.py`                                                | `generate.py`, `review_requests.py`          | Poison marker while an episode is in flight / after a hard crash. Generate refuses the episode and a request containing it fails; see [Crash-marked episodes](#crash-marked-episodes) |
| `.dubtitles.stages.json`         | `repair.py`, `dub_signs_merge.py`, `mux.py`, `merge_pass.sh` | `merge_pass.sh`, `mux.py`                    | One outcome per stage (repair, signs, mux). See [Stage outcomes](#stage-outcomes)                                   |
| `.dubtitles.mux-recovery`        | `mux.py`                                                     | `mux.py`, the `deploy/` close unit           | Written when a failed cross-device copy may have damaged the episode. While it exists, mux refuses that episode     |
| `.muxtmp.mkv.recovered`          | `mux.py`                                                     | you                                          | The complete muxed file that failed copy kept. The marker names its path                                            |
| `.dubtitles.crash.json`          | `generate.py`                                                | `review_requests.py`                         | Exception type, message and time from that crash; read by the `crash-marked` listing          |
| `.dubtitles.qc.json`             | `generate.py`                                                | `mux.py`                                     | Quality counters, cps quantiles, layout violations, and duration flags (schema version 5: overlong and drip_chain)  |
| `.dubtitles.repair.csv`          | `repair.py`                                                  | -                                            | Audit trail: original, repaired, reference, latency                                                                 |
| `.dubtitles.repair-summary.json` | `repair.py`                                                  | `mux.py`                                     | Targets, repaired, skipped, latency, model, rules, and `anchor_mode`                                                |
| `.dubtitles.unresolved.jsonl`    | `unresolved.py`                                              | `unresolved.py`, `review_server.py`          | Rejections awaiting human triage (the review queue)                                                                 |
| `.dubtitles.mux.log`             | `mux.py`                                                     | -                                            | Tracks dropped, defaults set                                                                                        |

### Internal artifact store (P39)

Every `.dubtitles.*` file in the table above (not the `.eng.dubtitles.*` deliverables) is an
internal artifact. They live in one SQLite database in the config volume:

    $CONFIG_DIR/artifacts.db          (mode 0600, WAL; $ARTIFACT_STORE overrides the path)

Schema: `artifacts(stem, kind, payload, meta, updated_at)`, primary key `(stem, kind)`. `stem`
is the episode path without its extension — the same key the pipeline already uses for
`common.out_for` — and `kind` is the suffix class (`conf`, `words`, `stages`, `done`,
`unresolved`, …). The database is part of the config volume, so it is included in whatever
backs that volume up. There is no service to run and no port to open.

Why: the media tree used to carry 11–13 artifacts per episode (~8,477 files, 316 MB, measured
2026-11-22). It is NFS-backed, so each entry also costs scan, sweep and backup metadata
traffic. The store keeps the same information with one file in an already-persisted volume.

One CLI, `python3 -m dubtitlerr.core.artifact_store`, owns storage and migration. Every
mutating subcommand is a **dry run unless `--apply` is passed**:

| Subcommand | Purpose |
| ---------- | ------- |
| `sweep ROOT` | Remove the orphan classes from the media tree: `*.stale`, `*.bak` (including `*.pre-gate-fix.bak`), `*.dubtitles.mux.log`, `*.dubtitles.mux-recovery`, `*.dubtitles.fail.archived-*`. Never touches a deliverable |
| `import ROOT` | Copy every internal artifact under `ROOT` into the store, then remove the file. Idempotent: a second run imports nothing. A crash mid-run can only duplicate, never lose |
| `export [ROOT]` | Write store rows back out as `<stem><suffix>` files, byte for byte. The reversibility belt; rows are kept |
| `census ROOT` | Count artifacts in a tree and in the store |
| `stems --kind K [--root R]` | Print the stems holding one kind — the shell loops' replacement for `find -name` |
| `has / get / put` | Existence check, payload to stdout, payload from stdin for scripts |

`import --apply` is the point of no return for files; run `export --apply` to undo it. Run
`sweep` before `import`, so junk is deleted rather than migrated.

A `.stale` suffix is appended to prior-version output during a version upgrade, e.g.
`.eng.dubtitles.srt.stale`. The `.dubtitles.done` stamp is the only already-done signal: an embedded Dubtitles track alone does not count, so episodes muxed before stamps existed regenerate on the first sweep. Run `scripts/migrate_write_v1_stamps.py` first (dry run by default, `--apply` to write); it writes sidecar stamps only, never touches media, and never overwrites an existing stamp.

Per **show** rather than per episode: `.lastrun.json` and the glossary's
`.acquire-cache.json`.

Beside each glossary: `<glossary>.json.lock` (an advisory lock; the file is harmless to delete),
`<glossary>.json.review-pending` (answers a human gave while a stage held the lock, applied at the next `--review`)
and `<glossary>.json.review-pending.bad` (a pending file that did not parse; kept, never trusted).

### Crash-marked episodes

`generate.py` writes `<stem>.dubtitles.fail` before it starts transcribing an episode and removes it
once that episode's words are written. A process killed hard — a redeploy, a restart, a power cut —
leaves the marker behind, and every later pass refuses that episode (`skip-prior-crash`). Stopping
the container softly no longer leaves one (0.2.2 and later), so a marker left by a kill means the
episode needs one recovery step.

A request whose scope contains a marked episode is filed **failed**, never "done with a gap": the
episode has no transcript, and calling the request done would hide that. Its message names the
blocked episodes and the remedy, and `crash-marked` is the listing the message points at:

```sh
python3 -m dubtitlerr.review.review_requests crash-marked [LIBRARY_ROOT]
python3 -m dubtitlerr.review.review_requests clear-crash [--root LIBRARY_ROOT] EPISODE [EPISODE ...]
```

`crash-marked` walks the library once, prints one `<stem>` per marker (tab, then the exception from
`<stem>.dubtitles.crash.json` when generate recorded one) and writes nothing. `clear-crash` removes
the marker for each episode it is given — the stem, the video path, or the marker path all name the
same marker — and keeps the crash record as history. `LIBRARY_ROOT` defaults to `ANIME_ROOT`;
removal is confined to that root and only ever unlinks a `.dubtitles.fail` file, so a mistyped path
cannot delete media. After clearing, the next request (or the next sweep) processes the episode
normally.

### Review queue entries

`.dubtitles.unresolved.jsonl` holds one JSON object per line. Entries stay in the file after they
are resolved; the review view filters them. Keys: `stage`, `reason`, `original_text`,
`proposed_text`, `avg_logprob`, `resolved`, and since this release `guard` (the check that refused
a repair) and, for dismissed entries, `dismissed` and `changes`. A repair entry whose card had an
anchored reference also carries `ref_role`, `ref_provenance` and `ref_track_title`; its
`reference` holds at most 300 characters. See [Repair references](#repair-references).

| Reason                   | Stage       | Meaning                                                                                                                                                                                    |
| ------------------------ | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `no_reference`           | repair      | No fansub anchor overlapped the card. Not in the primary view                                                                                                                              |
| `rejected_guard`         | repair      | The model proposed an edit and `accept_repair` refused it. `guard` names the check: `length_ratio`, `fits_card`, `invents_name`, `vouched_name`, `ref_borrow`, `terminal_punct` and others |
| `rejected_name_invented` | repair      | The model substituted a proper noun that is in neither the glossary nor the original                                                                                                       |
| `rejected_name_part`     | repair      | The repair added or dropped part of a multi-word name the line already had a piece of                                                                                                      |
| `decision_unfittable`    | repair      | A human verdict whose text does not fit the card. The transcript text stands and the reviewer is told                                                                                      |
| `llm_empty`              | repair      | The model answered with nothing                                                                                                                                                            |
| `rejected_guard`         | punctuation | The punctuation-restore stage's own guard refused a proposal                                                                                                                               |

A rejected repair that changes more than `REPAIR_AUTO_DISMISS_WORDS` words carries
`"dismissed": true` and a `changes` count. The review page's default view and
`unresolved.pending(primary_only=True)` skip it; the all-reasons view and the command-line `--all`
show it. A stored human verdict is looked up before any guard runs, so dismissing never overrides one.

---

## Stage outcomes

`<stem>.dubtitles.stages.json` holds one record per stage (`repair`, `signs`, `mux`). Each
record has an `outcome` and a `detail`. For example, an episode that mux refused because of a
recovery marker has this mux record (the other stages are left out):

```json
{"mux": {"outcome": "crashed", "detail": "recovery-pending"}}
```

A stage writes its record when it returns. `merge_pass.sh` clears a stage's record before it
runs that stage again, so an old failure never stands in for a new result. A run that dies
mid-stage leaves a `crashed` record.

| Outcome                                                                                   | Counts as | Meaning                                                                                                           |
| ----------------------------------------------------------------------------------------- | --------- | ----------------------------------------------------------------------------------------------------------------- |
| `ok`                                                                                      | passed    | The stage finished                                                                                                |
| `no-video`                                                                                | passed    | There is no media file for the stem                                                                               |
| `no-reference`                                                                            | passed    | Older records only. Nothing writes it any more                                                                    |
| `refused`                                                                                 | failed    | Repair declined to overwrite repairs that already shipped. The `.srt` on disk is raw speech recognition           |
| `stopped`                                                                                 | failed    | Repair saw a soft stop between two targets and quit before it rewrote the `.srt`. The next pass runs repair again |
| `crashed`, `timeout`, `unwritable`, `backend-unreachable`, `extract-error`, `build-error` | failed    | The stage failed for the reason in its name                                                                       |

A failed outcome blocks mux for that episode. The merge pass prints a `skip mux:` line for each
episode it holds back and reports `MERGE PASS INCOMPLETE` at the end. That includes an episode
repair stopped for a soft stop: the next pass clears the `stopped` record and repairs it again.

**The publish gate, and what happens after it refuses.** Before mux the merge pass runs
`dubtitlerr.pipeline.publish_gate` on the assembled sidecar. It refuses a file that does not cover
the episode, is not punctuated as text, does not fit the screen (more than 46 characters on a line,
more than 88 characters a card, or more than 7.5 seconds per card), or still contains a name the
show's glossary calls a mishear. A refusal is not a dead end: `dubtitlerr.pipeline.gate_fix` makes one
deterministic, model-free repair attempt — re-wrap an over-long line, split a card at a word,
sentence or clause boundary, or end an over-duration card where its own spoken words end — then
the gate is re-run on the file it wrote. If it passes, delivery continues normally. If it does not,
the episode is parked exactly as before (mux stage `refused`, a `skip mux:` line, counted in
`MERGE PASS INCOMPLETE`) and an `episode.refused` notification names the finding, once per distinct
refusal. The attempt is recorded in `<stem>.dubtitles.gate-fix.json` (cards touched, before/after
duration and length, outcome) and as the `gate_fix` stage record; an episode that already passes is
not touched at all. `GATE_FIX=0` disables just the attempt.

A line the ASR emitted as one run of a single letter — a real 57-character `'A' + 'H'×56`
scream, a long laugh — has no word boundary to wrap or split at, so it gets a **line break**
(`\N` in `.ass`, a newline in `.srt`) instead, at an even split inside the 46-character budget.
A couple of stray **letters** in the token (the scream's leading `A`, two closing letters) do not
disqualify it, and neither does punctuation: Berserk S01E16 ships `'A' + 'H'×57 + '!'` and its
exclamation mark rides on the run rather than competing with it. That is pure layout: strip the
breaks and the card's text is byte-identical, and its timing does not move. The audit records it as
`action: "wrap-run"`. A token that carries a **word in front of a long run** wraps the same way:
Demon Slayer S01E19 ships `'DOWN' + 'N'×48` (52 characters, four distinct letters, so the stray
count reads it as a word), and any token holding a run of at least 20 identical characters in a
row breaks inside that run — that card's 49-N run splits 27 / 25 characters — so the word stays
whole on the first line. The break cannot rescue a card whose **total** is over 88 characters (a
446-character run is over even wrapped), and a token that *alternates* rather than repeats
(`Kekekekeke…`, whose minority is half the token) is not a run — both of those refusals stand.

A card whose whole text is **one token** has no boundary at all, so when its duration is the only
fault there is nothing for a wrap or a split to do. Two shapes still resolve without touching a
character. A word the stream repeats — the card is a repetition artifact — ends where its **first**
utterance ends, and that now holds even when the card's text and the word stream disagree on the
spelling: the glossary renames a character after the words were written, as in Toradora S01E18,
where `Ryuuji?` is displayed over 13.4 s while the words say `Ryuji?` five times in a row. A card
with nothing to trim to — Cowboy Bebop S01E20 shipped `Hello?` over 19.1 s with no word stream at
all, and Toradora S01E24 shipped `Ryu!` across the whole 27.5 s OP — has its display **clamped to
the gate's own 7.5 s budget**. That is a documented duration exception, not a layout repair:
everything the viewer must read is on screen from the card's first frame, no character and no start
time moves, the clamp never reaches the next card, and the gate still judges the file. The audit
records `action: "clamp-duration"` with that reason. A repeated-character run is deliberately not
clamped: it keeps its own break-then-refuse path.

Faults that need a transcript edit rather than a layout one — coverage, punctuation, a cold open, a
single unbroken token that is not a run — are reported as unsupported rather than attempted. A
**known mishear** is the exception: the product may have been assembled before the glossary's own fix
landed, so `gate_fix` re-applies the show's `glossary.correct()` rules to the stored cues' parsed text
(one physical line at a time, so breaks and timing never move), re-gates the result in memory and
writes it only if it now passes. The audit records it as `outcome: "corrected"` with the before/after
text per card. An uncorrectable mishear, or a partial correction that leaves another finding, still
parks. The park's attempt stamp carries a glossary fingerprint, so editing the glossary retries that
show's parked episodes exactly once — the same discipline `GATE_FIX_EPOCH` and the version bumps
already follow.

**Song spans are not dialogue.** A dub re-uses the Japanese OP/ED, and the English ASR renders
sung Japanese as pseudo-romaji with an occasional English adaptation line (`Osoresu ni susume yo.`
/ `Holy brave.` / `Furikaereba itsumo.`). `dubtitlerr.core.song_spans` recognises such a region —
two or more cards that hold no recognised English word *and* whose tokens are all romaji-shaped,
grouped when they start within 8 s of each other — and the gate leaves the span out of its
punctuation, layout and mishear checks; `gate_fix` leaves its cards' bytes alone. So the ending-song
romaji can no longer be reported as a known mishear (a show glossary legitimately carries a
`Furikaereba -> Frieren` rule admitted from dialogue), and the lyric lines are not measured as
subtitles. The span is conservative by construction: a terse English line with no function word
("Confirming ID.", "Weak Kingsley.") is *not* a lyric because its consonant clusters are unspellable
in romaji; a lone romaji-looking card is still dialogue; and a region covering more than a quarter
of the file's cards, or running longer than 180 s, is treated as a broken transcript rather than a
song. Nothing is deleted or rewritten: the release's own lyric and sign events stay where the merge
put them, and a styled `.ass` still ships them.

The punctuation floor is a **minimum, not a quality bar**: a card counts as punctuated when it ends
with any punctuation mark, clauses included, because a subtitle cut at a comma is still punctuated
text continuing a sentence. It sits at 50% of cards, well under the real sidecars measured
2026-11-21 (the lowest is 60.9%) and well above the unpunctuated raw-ASR band (which tops out at
27.1%, the 0.0% Death Note control included).

The over-duration trim ends a card where **its own** first utterance ends. A word may start up to a
second before the card (the assembler places the card boundary at the segment boundary and the word
at its onset), so the trim still finds the run it was written for. The trade-off is deliberate and
documented: when the word stream repeats the line later, those later utterances were never part of
the card's text and stay unsubbed rather than being duplicated onto a card that never displayed them.

**A collapsed repetition run is the same trade-off with one more step.** The hallucination pass
folds four or more consecutive identical one-word cards into one card spanning the whole run
(`hallucination.collapse_runs`: first start, last end). A short exclamation therefore ships as a
single card over its entire run — 86 Eighty-Six S01E22's `Princess!` over 8.35 s (six utterances,
the first a zero-length word), Berserk S01E06's `Griffith!` over 14.55 s (four shouts, the first
0.52 s). Ending the card at its first utterance would leave it shorter than the 0.83 s minimum
display time, which is why that path gave up and parked both episodes. Such a card now ends at the
end of its **contiguous repeat** instead — the occurrences of its exact words spaced no more than a
pause (0.5 s) apart, capped at the 7.5 s budget. The text still appears once (a repair may not
duplicate words), the caption covers the cry it describes, and a repetition separated by real
silence (Berserk's last shout, 10 s later) stays outside the chain and unsubbed. A card whose
first utterance is short and *not* repeated is still not repairable: it parks.

`llm-empty` is a name in the code's outcome list, but repair never writes it. When a model
answers a target with nothing, repair counts it as `llm_empty` in the repair summary, queues the
line for review, and the stage still records `ok`.

---

## Repair references

A **reference** is native release text the repair stage may consult for one card. Two independent
axes decide whether a track can serve that role; both are classified in
`dubtitlerr/core/reftracks.py`.

| Axis       | Values                        | Rule                                                                                                                                       |
| ---------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Provenance | `native`, `ours`              | `ours` is a track whose title is exactly `common.TRACK_NAME` (`"Dubtitles"`), the marker this pipeline muxes itself. `is_reference` requires `native` and a role other than `signs`, so our own output is never a reference |
| Role       | `dub`, `translation`, `signs` | `dub` is captions of the English dub itself; `translation` is a separate localisation; `signs` is signs/songs/forced and is never dialogue |

Only English tracks are considered (`SUB_LANGS`). `ass`, `ssa`, `subrip` and `mov_text` are read;
bitmap codecs (PGS, VobSub) are not text and are ignored.

### Role classification

The title is the prior; the content is the evidence. `title_role` (`reftracks.py:320`) maps a
title to a role. When the episode's ASR text is supplied, word-bigram overlap (`classify`,
`reftracks.py:339`) confirms or overrides it.

| Title evidence                                         | Role          | Confirmed from the title? |
| ------------------------------------------------------ | ------------- | ------------------------- |
| `common.SIGNS_TITLE` match (`Signs & Songs`, `S&S`, `Forced`, `Songs`) | `signs`       | yes                       |
| explicit dub marker (`dub`, `dubtitle`, `dubbed`)      | `dub`         | yes                       |
| `SDH` / `CC` / `dialog(ue)` only                       | `dub`         | no — provisional          |
| any other title, or an empty title                     | `translation` | no — provisional          |

Measured word-bigram overlap decides the provisional rows:

| Constant           | Value  | Effect                                                                                       |
| ------------------ | ------ | -------------------------------------------------------------------------------------------- |
| `OVERLAP_MIN`      | `0.55` | A provisional title becomes `dub` at or above this, `translation` below it                    |
| `OVERLAP_KEEP_MIN` | `0.30` | An explicit dub title is only overturned below this, so measurement must be decisive to beat it |
| `MIN_SAMPLE_TOKENS`| `40`   | Below this many distinct sample words the score is noise and the title prior stands           |

`role_confirmed` records whether the role came from an explicit title or from measurement;
`role_overlap` holds the score (`null` when unmeasured). A classifier failure is never an episode
failure: the episode is treated as unanchored (`repair.py:537`).

### Selection, per card

Repair picks **one** reference track per card from those whose dialogue cues overlap the card's
audio window (`select_reference`, `repair.py:553`). The order is fixed:

1. role, `dub` before `translation`;
2. the track holding the most overlapping characters;
3. the lowest stream index.

Only that one track's overlapping cues are joined, capped at `REF_WINDOW_CAP` = `300` characters
(`repair.py:528`). There is no cross-track join: a dub caption and a fansub line are never
concatenated into one reference.

`REPAIR_REF_CLASSIC=1` restores the pre-classification merged pool (`overlap_ref` over every English subtitle
stream) exactly. It is a rollback and A/B switch, not a stored setting; the classifier is not
called on that path, and the summary reports `anchor_mode: "unclassified"`.

### What each role does to the gates

`accept_repair` and `rejecting_guard` (`repair.py:1195`) take the recorded `ref_role`. For
`translation`, and for a reference with no recorded role, every gate behaves exactly as measured
before this change.

For `dub`, a proposal whose **every added word** appears in that card's dub-caption window is
**all-vouched** (`dub_vouches`, `repair.py:722`). That changes three checks:

| Check                | Classic                        | All-vouched, `ref_role=dub`                                                          |
| -------------------- | ------------------------------ | ------------------------------------------------------------------------------------ |
| `ref_borrow`         | `MAX_REF_BORROW` borrowed words refuse | Skipped — the same words are attestation, not leakage                        |
| `length_ratio` upper | `LEN_RATIO_MAX` = `1.5`        | `REPAIR_GATE_DUB_VOUCHED_MAX_RATIO` = `3.0`                                          |
| `invents_name`       | A fabricated proper noun refuses | A gained name core that appears in the dub window is exempt (`dub_name_exemption`, `repair.py:709`) |

Everything else still applies, unchanged for both roles: `empty`, `unchanged`, `terminal_punct`,
`name_part`, `vouched_name`, the `LEN_RATIO_MIN` lower bound and `fits_card`. If **any** added
word is absent from the dub window, the proposal falls back to the classic gates unchanged.
`REPAIR_GATE_DUB_VOUCHED=0` restores classic semantics everywhere; like `REPAIR_REF_CLASSIC` it is
a rollback switch, not a stored setting.

### Recording

| Where                                   | Fields                                                                                                                                  |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Queue entry (`.dubtitles.unresolved.jsonl`) | `ref_role`, `ref_provenance`, `ref_track_title` (absent or null when the card had no anchored reference); `reference` holds at most 300 characters |
| `repair-summary.json`                   | `anchor_mode`: `native-dub`, `native-translation`, `mixed`, `none`, or `unclassified` (rollback path only)                              |
| Review server entry                     | the three fields above plus a normalised `ref_class`: `dub`, `translation`, `unanchored`, `unclassified`                                |
| Shared pairs (`/api/shared`)            | `ref_class` only when every episode in the pair agrees; otherwise `null`                                                                |

`ref_class` is `unanchored` only when the entry has no reference text at all. An entry that has
reference text but no recorded role — every entry queued before this change — is `unclassified`,
which is a different question: the anchor exists, its role is unknown. Older queue entries without
these fields still read.

### External reference acquisition (Jimaku)

A release that ships no subtitle track has nothing to classify, which is also the case
`glossary_acquire` documents: no mineable track means no names. `dubtitlerr/core/refs/` fills that
gap by fetching one Japanese track from [Jimaku](https://jimaku.cc) (the Kitsunekko successor,
keyed to AniList IDs) and **refusing it unless it provably belongs to this video**. An acquired
track has `translation` provenance, never `dub`: it is the original-language wording, which is a
disambiguation anchor, not the dub's text.

| Step | Module | What it decides |
| --- | --- | --- |
| entry | `refs/match.py` | Is this the series, and the season? `Season 2`, `II`, `第2期`; a later cour (`Part 2`, 第2クール) never matches |
| file | `refs/match.py` | Does the file name state **this** episode? `S01E05`, `1x05`, `- 05`, `第5話`, `05v2`, `01-04`; a season pack names no episode and is never used |
| encoding | `core/subtitle_encoding.py` | BOM, then strict UTF-8, then the language's codecs (`ja`: cp932/Shift-JIS/EUC-JP/ISO-2022-JP), then scored legacy codepages |
| admission | `core/subtitle_fit.py` | Overrun, coverage, and — when a witness exists — line-start agreement at the best offset |

Admission constants, measured against this repo's own artifacts (28 cross-episode pairs):

| Constant | Value | Meaning |
| --- | --- | --- |
| `MIN_SHARE` | `0.60` | Share of the candidate's line starts that find a witness start within 0.3 s at the best offset |
| `MIN_LIFT` | `0.10` | How far that share must beat the same measurement at an arbitrary offset (a dense track scores ~0.72 by density alone) |
| `MIN_CUES` | `20` | A shorter track can coincide by luck; the 5- and 12-cue fixture slices scored 0.60 against unrelated episodes |
| `MAX_OFFSET` | `90` s | Beyond this the candidate is another cut, and the search must not be talked into finding an offset |
| `OVER_RUN` | 10 % or 120 s | Cues ending past this are a different cut, not a stray sign |

The lane is a module CLI, not a loop stage — nothing is muxed and no glossary entry is admitted from
it:

```bash
JIMAKU_API_KEY=... python3 -m dubtitlerr.core.refs.acquire \
  --anilist-id 154587 --season 1 --episode 5 --video /media/.../Show.S01E05.mkv --out /tmp/refs
```

Exit status `0` means a reference was admitted, `1` means none passed (reasons on stdout as JSON),
`2` is a usage or key error. Downloads are https-only and restricted to `jimaku.cc` hosts. `.zip`
members are read with the stdlib; `.rar` needs `bsdtar` from `libarchive-tools`, which both images
carry — without it the archive is reported as unextractable rather than silently skipped.

---

## Environment variables

### Paths and roots

| Var              | Default                     | Meaning                                            |
| ---------------- | --------------------------- | -------------------------------------------------- |
| `MEDIA_ROOT`     | `/media`                    | Media mount                                        |
| `OUTPUT_ROOT`    | _(empty)_                   | Write sidecars elsewhere than beside the video     |
| `ANIME_ROOT`     | `/media/Anime Library`      | Root the generate loop walks                       |
| `MERGE_ROOTS`    | `/data/Media/Anime Library` | Roots the merge pass walks (colon-separated)       |
| `MUX_ROOTS`      | `/data/Media/Anime Library` | Roots `mux.py` walks                               |
| `ANIME_ORDER`    | `/config/anime_order.txt`   | Show order file; if missing, the builder queues the library tree |
| `GLOSSARY_DIR`   | `/config/glossaries`        | Per-show glossaries                                |
| `DECISIONS_DIR`  | `/config/decisions`         | Per-show verdict stores                            |
| `WIKI_CACHE_DIR` | `/config/wiki_cache`        | Cached Fandom wiki responses                       |
| `MODEL_DIR`      | `/subgen/models`            | Whisper model directory                            |
| `HARDLINK_ROOTS` | _(unset)_                   | Roots to search when preserving download hardlinks |

> **`ANIME_ROOT` and `MERGE_ROOTS` have different defaults.** The generate loop defaults to
> `/media/Anime Library` and the merge pass to `/data/Media/Anime Library`. Set both
> explicitly; do not rely on either default lining up with the other.

### Transcription

| Var                                  | Default          | Meaning                                                                                        |
| ------------------------------------ | ---------------- | ---------------------------------------------------------------------------------------------- |
| `WHISPER_MODEL`                      | `large-v3-turbo` | Whisper model. `large-v3` scores higher; see [Choosing an ASR model](Choosing-an-ASR-Model.md) |
| `COMPUTE_TYPE`                       | `int8`           | Precision. On Pascal cards `float16` does not load at all — this is not a quality knob there   |
| `WHISPER_BEAM_SIZE`                  | `7`              | Beam width                                                                                     |
| `REQUIRE_ENG`                        | `1`              | Skip episodes with no English audio track                                                      |
| `UND_AUDIO_AS_DUB`                   | `0`              | Treat a file's only audio track as the English dub when it has no language tag. Off by default; a Japanese-only file would be transcribed as English |
| `SKIP_IF_SRT`                        | `1`              | Skip an episode that already has a dubtitle sidecar                                            |
| `SHOW_NAME`                          | _(empty)_        | Override the show name derived from the path                                                   |
| `GLOSSARY_FILE`                      | _(empty)_        | Override the glossary resolved from the show                                                   |
| `FFMPEG_TIMEOUT` / `FFPROBE_TIMEOUT` | `1800` / `60`    | Seconds                                                                                        |

### ASR backend

`ASR_BACKEND` decides where transcription happens. Everything downstream — reflow, glossary
correction, repair, the hallucination gate, the signs merge — is identical on both paths, so this is
the only place the choice shows up. [Choosing an ASR model](Choosing-an-ASR-Model.md) is about which
model to point an endpoint at.

| Var                                  | Default                                            | Meaning                                                                                                                                                                                    |
| ------------------------------------ | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `ASR_BACKEND`                        | `local`                                            | `local` runs Whisper inside this container; `endpoint` posts the audio to an OpenAI-compatible `/audio/transcriptions` URL and needs no local model, no torch and no CUDA                      |
| `ASR_URL`                            | `https://openrouter.ai/api/v1/audio/transcriptions` | Full `/audio/transcriptions` URL, used when the backend is `endpoint`                                                                                                                      |
| `ASR_MODEL`                          | `openai/whisper-1`                                 | Model name sent to the endpoint — for a router, this must be the router's entry name. **Read the shutdown note below before relying on the default**                                        |
| `ASR_KEY_ENV`                        | `OPENROUTER_API_KEY`                               | Name of the environment variable that holds the key, and the only one read. Empty sends no key. A key is sent only over https, or plain http to loopback, whatever this is set to           |
| `ASR_TIMEOUT`                        | `900`                                              | Per-request timeout, seconds                                                                                                                                                               |
| `ASR_REFERER`                        | this fork's URL                                    | `HTTP-Referer`, sent to OpenRouter only; other endpoints ignore it                                                                                                                         |
| `ASR_DEVICE`                         | `cuda`                                             | Device faster-whisper runs on. `local` backend only; ignored in endpoint mode                                                                                                              |
| `OUTPUT_MODE`                        | `mux`                                              | `mux` embeds the subtitle as a Dubtitles track and rewrites the video; `sidecar` leaves it beside the video and never opens the video for writing                                          |
| `SIDECAR_SUFFIX`                     | `.eng.dubtitles`                                   | Sidecar mode only. The text between the episode name and the extension, e.g. `.dub.en` gives `Episode.dub.en.srt`                                                                          |
| `ADOPT_DUB_EN`                       | `false`                                            | Feed an existing `.dub.en.srt` / `.ass` from another pipeline through repair and signs merge. Off, the pass never moves files it did not produce                                           |

> **The `ASR_URL` and `ASR_MODEL` defaults point at a service being withdrawn.** `openai/whisper-1`
> shuts down on **2027-02-26**, and no replacement returns word-level timestamps — the
> `timestamp_granularities[]` parameter that provides them is supported by `whisper-1` alone. Point
> the endpoint at a server you run, or at a provider that returns both word timings and a live
> `no_speech_prob`; [Choosing an ASR model](Choosing-an-ASR-Model.md#hosted-endpoints-and-a-date-worth-knowing)
> compares the hosted options field by field.

### Repair

| Var                                  | Default                                            | Meaning                                                                                                                                                                                                                                                                                                             |
| ------------------------------------ | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `REPAIR_MODEL`                       | `qwen3-4b-instruct`                                | Repair model                                                                                                                                                                                                                                                                                                        |
| `REPAIR_BACKEND`                     | `llamacpp`                                         | `llamacpp`, `ollama` or `openai` (any OpenAI-compatible chat API; see the rows below)                                                                                                                                                                                                                               |
| `REPAIR_LLAMACPP_URL`                | `http://127.0.0.1:8090/v1/chat/completions`        | llama.cpp chat endpoint. The server behind it **must** be started with `--jinja`, or `chat_template_kwargs.enable_thinking=false` is silently ignored and a thinking-capable model burns its whole budget on reasoning and returns empty content                                                                    |
| `OLLAMA_URL`                         | `http://127.0.0.1:11434/api/generate`              | Ollama endpoint                                                                                                                                                                                                                                                                                                     |
| `REPAIR_MODEL_SECONDARY`             | _(same as primary)_                                | Second-opinion model for name changes                                                                                                                                                                                                                                                                               |
| `REPAIR_UNANCHORED`                  | `1` (deployed 2026-09-06 on the reference install) | Global override; per-show glossary field preferred for new installs (see How-To-Guides.md)                                                                                                                                                                                                                          |
| `DECISIONS_APPLY`                    | `1`                                                | Apply stored human verdicts during repair                                                                                                                                                                                                                                                                           |
| `LOGPROB_MIN`                        | `-0.4`                                             | Below this average logprob, a line is a repair target                                                                                                                                                                                                                                                               |
| `NSP_MAX`                            | `0.5`                                              | Above this no-speech probability, a line is skipped                                                                                                                                                                                                                                                                 |
| `LEN_RATIO_MIN` / `LEN_RATIO_MAX`    | `0.6` / `1.5`                                      | Accepted length change                                                                                                                                                                                                                                                                                              |
| `MAX_REF_BORROW`                     | `3`                                                | Words a repair may take from the reference                                                                                                                                                                                                                                                                          |
| `REPAIR_REF_CLASSIC`                 | _(unset)_                                          | `1` restores the classic merged reference pool instead of one classified track per card. Rollback and A/B only: the classifier is not called and `anchor_mode` reports `unclassified`. Not a stored setting                                                                                                         |
| `REPAIR_GATE_DUB_VOUCHED`            | `1`                                                | `0` restores the classic gates everywhere, so a native dub-caption reference no longer vouches for added words. Rollback switch only, not a stored setting. See [Repair references](#repair-references)                                                                                                             |
| `REPAIR_GATE_DUB_VOUCHED_MAX_RATIO`  | `3.0`                                              | Longest repair ratio allowed when a native dub-caption reference attests every added word. The lower bound is still `LEN_RATIO_MIN`, and `fits_card` still decides displayability                                                                                                                                   |
| `REPAIR_PHONETIC_MIN`                | `0.75`                                             | Phonetic similarity floor for a name correction                                                                                                                                                                                                                                                                     |
| `REPAIR_TIMEOUT_CONNECT` / `_READ`   | `10` / `120`                                       | Seconds. `_READ` applies to each socket read, not to the whole request; see `REPAIR_REQUEST_DEADLINE`                                                                                                                                                                                                               |
| `REPAIR_OPENAI_URL`                  | _(unset)_                                          | Full chat-completions URL of an OpenAI-compatible API. Used when `REPAIR_BACKEND=openai`                                                                                                                                                                                                                            |
| `REPAIR_OPENAI_KEY_ENV`              | _(unset)_                                          | The **name** of the environment variable that holds the API key. The key is read at call time, a trailing CR or LF is stripped, and its value is never logged                                                                                                                                                       |
| `REPAIR_MAX_TOKENS`                  | `1024`                                             | `max_tokens` for the `openai` backend. A reasoning model that spends all of it thinking returns empty content, which repair counts as the backend being unreachable. A value that is not a number logs a warning and uses the default                                                                               |
| `REPAIR_EXTRA_BODY`                  | _(unset)_                                          | JSON object merged into the request body, for provider options such as `{"enable_thinking": false}`. It cannot replace `model` or `messages`. A value that is not a JSON object makes every call fail as unreachable                                                                                                |
| `REPAIR_OPENAI_RETRIES`              | `2`                                                | Retries after HTTP 429 or 5xx, so three attempts in all. Each waits the `Retry-After` value (at most 30 s), or 2 s and then 4 s when there is none                                                                                                                                                                  |
| `REPAIR_RETRY_SLEEP_BUDGET`          | `600`                                              | Seconds of retry waiting allowed per episode. Once spent, each remaining target gets one attempt                                                                                                                                                                                                                    |
| `REPAIR_REQUEST_DEADLINE`            | `300`                                              | Seconds one HTTP request may take in total, connect plus response (minimum 10). A server that trickles bytes defeats `REPAIR_TIMEOUT_READ`, so this caps the whole request                                                                                                                                          |
| `REPAIR_EPISODE_DEADLINE`            | `3600`                                             | Seconds repair may spend on one episode; `0` turns it off. Past it, the episode is refused like an outage. The slowest of 1,631 production episodes took 897 s                                                                                                                                                      |
| `REPAIR_UNREACHABLE_REFUSE_PCT`      | `10`                                               | Refuse an episode when the backend failed this percent (1 to 100) or more of the targets that reached it                                                                                                                                                                                                            |
| `REPAIR_MAX_CONSECUTIVE_UNREACHABLE` | `5`                                                | This many failures in a row count as an outage and end the episode                                                                                                                                                                                                                                                  |
| `REPAIR_TRANSPORT_RETRIES`           | `2` (0 to 5)                                       | Retries on the `openai` backend after a failure that is known to happen before the request is sent: connection refused, DNS, connect timeout. A read timeout or a dropped connection after sending is never retried, because the provider may already have the request. `0` turns it off                            |
| `REPAIR_KEEP_NAME_PARTS`             | `1`                                                | A name said alone stays alone, and a full name stays full. Repair refuses a proposal that adds or drops a name part next to a name the line already has, and adds one sentence about it to the prompt when the glossary lists names. `0` turns both off. See [stored verdicts](#stored-verdicts-and-prompt-changes) |
| `REPAIR_AUTO_DISMISS_WORDS`          | `6`                                                | A repair a guard refused that changes more than this many words is still written to the queue file, marked `dismissed`, but the primary review view skips it. `off` turns it off. Words added plus words dropped are counted, so re-spacing a known phrase counts as no change                                      |
| `REPAIR_ACCEPT_PUNCT_CASE_ONLY`      | _(off)_                                            | `1` lets a proposal that only **adds** a `.` or `,` (and makes a safe case change) skip the "one more visible character" layout check. It never accepts removed or changed punctuation. Added commas can still change meaning, so audit a sample before you rely on it                                              |
| `REPAIR_GUARD_TERMINAL_PUNCT`        | _(off)_                                            | `1` refuses a proposal that adds or removes a terminal `?`, swaps `!` and `?`, or turns a trailing ellipsis into `.` or `!`. On 451 real pairs it refused 21, and most of those were corrections, so it is off                                                                                                      |

**When repair refuses an episode.** Repair leaves the `.srt` as it was, records
`backend-unreachable`, and retries the episode on a later pass when:

- the backend failed `REPAIR_UNREACHABLE_REFUSE_PCT` percent or more of the targets that reached it,
- `REPAIR_MAX_CONSECUTIVE_UNREACHABLE` failures came in a row, or
- the episode ran past `REPAIR_EPISODE_DEADLINE`.

A failure is a connection error, a timeout, HTTP 401, 403, 408, 429 or 5xx, a missing or
malformed key, or (on the `openai` backend) an empty reply that hit the token limit. HTTP 400,
404, 409, 413 and 422 mean the model rejected that one request: the line keeps its transcript text
and is queued like an empty reply, with no retry. If every request of an episode is rejected that
way, the episode is refused, because the model id or the URL is probably wrong. These rules apply
to every backend, not only `openai`.

#### Stored verdicts and prompt changes

A human verdict is keyed on the pair of original line and proposed text. Anything that changes
what the model proposes can therefore leave an approved verdict that no longer matches. The line
is then recorded as `verdict_stale_proposal` and the new proposal is judged on its own merits.
Rejected verdicts are untouched, and an approved verdict that still matches keeps applying.

`REPAIR_KEEP_NAME_PARTS=1` adds a sentence to the repair prompt whenever the glossary lists names,
so the first repair of an already-reviewed episode can produce proposals that differ from the ones
you approved. It does not make a stamped episode repair again: the `.dubtitles.done` stamp holds
version numbers and never the prompt. Only an episode repaired again for another reason sees the
new wording. Set `REPAIR_KEEP_NAME_PARTS=0` to restore the old prompt.

### Punctuation restore

| Var                   | Default                            | Meaning                                    |
| --------------------- | ---------------------------------- | ------------------------------------------ |
| `RESTORE_PUNCTUATION` | `1`                                | Enable the punctuation-restore stage       |
| `RESTORE_MODEL`       | _(falls back to `REPAIR_MODEL`)_   | Model for this stage                       |
| `RESTORE_BACKEND`     | _(falls back to `REPAIR_BACKEND`)_ | Backend for this stage                     |
| `RESTORE_MIN_RUN`     | `2`                                | Minimum run of unpunctuated cues to act on |
| `RESTORE_MAX_TOKENS`  | `2048`                             | Generation cap                             |

### Glossary mining, acquisition and verification

| Var                  | Default                            | Meaning                                                                                                  |
| -------------------- | ---------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `MINE_MIN_COUNT`     | `3`                                | Occurrences before a mined term is kept                                                                  |
| `ACQUIRE`            | `1`                                | Run the acquisition step (`gen_loop.sh`)                                                                 |
| `ACQUIRE_APPLY`      | `True`                             | Apply acquired proper nouns by default; explicit 0, false, no, off, or dry-run enables dry-run mode      |
| `ACQUIRE_MIN_COUNT`  | `3`                                | Occurrences before a candidate is considered                                                             |
| `ACQUIRE_MIN_SHARE`  | `0.80`                             | Share of occurrences that must agree                                                                     |
| `ACQUIRE_MIN_SIM`    | `0.72`                             | Similarity floor for a candidate match                                                                   |
| `ACQUIRE_UNSEEN_SIM` | `0.98`                             | Similarity above which a term counts as already known                                                    |
| `ACQUIRE_GROWTH_MAX` | `2`                                | Cap on glossary growth per pass                                                                          |
| `ACQUIRE_NO_CACHE`   | _(unset)_                          | Bypass the acquire cache on dry runs                                                                     |
| `VERIFY_MODEL`       | `qwen3:8b`                         | Adjudication model for wiki verification                                                                 |
| `VERIFY_BACKEND`     | _(unset)_                          | Backend for verification and acquisition. See [Decision backend](#decision-backend) for what unset means |
| `VERIFY_WORKERS`     | `4`                                | Concurrent wiki lookups                                                                                  |
| `WIKI_HTTP_TIMEOUT`  | `20`                               | Seconds                                                                                                  |
| `WIKI_CACHE_TTL`     | `2592000`                          | 30 days, in seconds                                                                                      |
| `WORDLIST_PATH`      | `/usr/share/dict/american-english` | The real-English-word gate                                                                               |

### Glossary write guards and the review queue

Verify turns a model's pick into one of three outcomes: a name written to the glossary, a flag
for a human, or a record that nobody sees. These variables choose which. The first two guards
were measured on 163 blind-judged writes from a rebuild of the One Pace glossary: unguarded, the
Jev decision backend wrote 87 expansions of which 45% were right; with both guards it wrote 29,
86% right, and sent 58 to the queue.

| Var                               | Default | Meaning                                                                                                                                                                                                                                                                                                                    |
| --------------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `VERIFY_WRITE_WHOLE_WORD`         | `1`     | An expansion (`Zoro` → `Roronoa Zoro`) is written only when the term is a whole word of the title and has at least two letters. Anything else is flagged `respelling-needs-review`. `0` restores the older substring rule, which wrote `Usop` → `Usopp` and `Burn` → `Coburn`                                              |
| `VERIFY_SKIP_COMMON_WORDS`        | `1`     | A one-word term that `WORDLIST_PATH` spells in lowercase (`castle`, `government`) is flagged `common-word` instead of written. Names the list capitalises (`Hancock`) are not affected. If the list cannot be read, the guard falls back to a 228-word list, logs it once, and the verify report shows `wordlist: missing` |
| `VERIFY_COMMON_WORD_ARC_RESCUE`   | _(off)_ | `1` lifts a common-word block when the title is on an arc page. On the audit set it let 58 of 99 wrong writes back in, because ordinary words sit on arc pages, so it stays off                                                                                                                                            |
| `VERIFY_QUEUE_NO_MATCH`           | `0`     | A term with no wiki candidate is recorded in `verified` and counted (`no_match` in the report) but not flagged. `1` flags each one. On the One Pace rebuild this removed 578 of 687 queue items, none of which carried a decision                                                                                          |
| `VERIFY_QUEUE_BLOCKED_EXPANSIONS` | `1`     | `0` records an expansion a guard blocked, when the title has **more tokens** than the term, without queuing it. A same-length respelling such as `Usop` → `Usopp` is always queued, because it is a spelling fix and not an added name part                                                                                |
| `VERIFY_REFUSE_EXIT`              | _(off)_ | `1` makes a refused `--tag-arcs`, verify or acquire run exit 2 instead of 0. `gen_loop.sh` treats any non-zero exit except 124 as "continuing", so inside the loop it only shows up in the log                                                                                                                             |

Two settings undo each other's work. `VERIFY_ARC_GATE=1` together with
`VERIFY_QUEUE_BLOCKED_EXPANSIONS=0` hides every name that misses an arc page from every human.
Setting `REPAIR_AUTO_DISMISS_WORDS` below 4 hides fixes a human would have accepted.

### Decision backend

With `VERIFY_BACKEND=decision`, verify asks a decision model to pick among the wiki candidates
instead of asking a chat model to write JSON. Each call sends the **show name, the term and the
candidate titles** to the provider; no dialogue leaves the machine. Acquire stays on the local
model unless you set `ACQUIRE_BACKEND=decision`.

| Var                             | Default                                                                               | Meaning                                                                                                                                                                                                                                                                     |
| ------------------------------- | ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `VERIFY_BACKEND`                | _(unset)_                                                                             | `llamacpp`, `ollama`, `openai` or `decision`. Unset follows `REPAIR_BACKEND` only when that is `llamacpp` or `ollama`; with `REPAIR_BACKEND=openai` the stage **refuses** until you set it                                                                                  |
| `ACQUIRE_BACKEND`               | _(unset)_                                                                             | Backend for acquire's LLM calls. Unset, acquire follows `VERIFY_BACKEND`, except that when that is `decision` it uses the local backend: the decision model scored 47% against the local model's 72% on the blind acquire pairs. `ACQUIRE_BACKEND=decision` opts acquire in |
| `VERIFY_DECISION_MODEL`         | `inception/mercury-decide:free`                                                       | Decision model. The maintainer's install uses `typesafe/jev` through CommandCode, which is paid. Span models are refused: they score one candidate at a time                                                                                                                |
| `VERIFY_DECISION_URL`           | _(the provider's endpoint)_                                                           | Override the endpoint. It must be https and on the provider's host                                                                                                                                                                                                          |
| `VERIFY_DECISION_KEY_ENV`       | `OPENROUTER_JEV_API_KEY` for OpenRouter models, `COMMANDCODE_API_KEY` for CommandCode | The **name** of the variable that holds the key, never the key. Read at call time, never logged                                                                                                                                                                             |
| `VERIFY_DECISION_TAU`           | `0.5` (0 to 1)                                                                        | A pick at or above this confidence is `high` and may be written; below it, it is flagged                                                                                                                                                                                    |
| `ACQUIRE_DECISION_TAU`          | `0.8` (0.5 to 1)                                                                      | The same threshold for acquire's same-entity question                                                                                                                                                                                                                       |
| `VERIFY_DECISION_FALLBACK`      | `none`                                                                                | `none`: a failed call leaves the term pending for the next sweep. `local`: the local model decides instead                                                                                                                                                                  |
| `VERIFY_DECISION_LOCAL_BACKEND` | `REPAIR_BACKEND` if that is `llamacpp` or `ollama`, else `ollama`                     | The local backend the fallback uses                                                                                                                                                                                                                                         |
| `VERIFY_DECISION_BREAKER`       | `5` (0 to 1000)                                                                       | After this many failed terms in a row the provider is not called again this run. `0` turns the breaker off                                                                                                                                                                  |
| `VERIFY_DECISION_WORKERS`       | `1` (1 to 16)                                                                         | Concurrent decisions. One keeps a rate limit from multiplying                                                                                                                                                                                                               |
| `VERIFY_DECISION_RETRIES`       | `0` (0 to 5)                                                                          | Retries after a 429 or 5xx inside one call. The next sweep retries anyway                                                                                                                                                                                                   |
| `VERIFY_OPENAI_URL`             | _(unset)_                                                                             | Chat-completions URL for `VERIFY_BACKEND=openai`                                                                                                                                                                                                                            |
| `VERIFY_OPENAI_KEY_ENV`         | _(unset, no default name)_                                                            | The name of the variable that holds that key                                                                                                                                                                                                                                |
| `VERIFY_OPENAI_EXTRA_BODY`      | _(unset)_                                                                             | JSON object merged into the request body                                                                                                                                                                                                                                    |

At the end of a run verify prints one line, for example
`decision backend: model=typesafe/jev calls=986 ok=985 failures=1 fallbacks=0 pending=1`.

### Arc tags and wiki scope

The arc stage fetches the wiki pages for each arc (or, for shows with no arc names, a scope you
configure) and stores the titles in the glossary. Names are then tagged with their arcs, which
puts the current arc's names first in the repair prompt, and the titles can gate verify's writes.
See [Glossary file](#glossary-file) for the stored keys.

| Var                 | Default                     | Meaning                                                                                                                                                                                                                                                             |
| ------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GLOSSARY_ARC_TAGS` | `0` (read by `gen_loop.sh`) | `1` runs the `--tag-arcs` pass for each show, between acquire and verify                                                                                                                                                                                  |
| `ARC_TAGS_TIMEOUT`  | `600`                       | Seconds one `--tag-arcs` run may take                                                                                                                                                                                                                               |
| `ARC_MIN_TITLES`    | `10`                        | An arc with fewer wiki titles counts as missing. The smallest real One Piece arc has 38                                                                                                                                                                             |
| `VERIFY_ARC_GATE`   | _(off)_                     | `1` flags a would-be write whose title is on no arc page (`not-in-arc-pages`). It does nothing, and says so once, unless every arc of the show has stored titles with finished redirects. On the audit set it blocked 45 of 119 wrong writes and 8 of 42 right ones |
| `WIKI_HOST_ALLOW`   | _(unset)_                   | Legacy, no longer consulted: `wiki_scope` accepts any public https host whose URL ends in `api.php` (P22). Kept so an existing config still validates                                                                                                               |
| `WIKI_ALLOW_HTTP`   | _(unset)_                   | `1` lets `glossary_verify` fetch an `http://` wiki, for tests. `wiki_scope` is always https                                                                                                                                                                         |

Every wiki fetch is https-only. A redirect to another scheme is refused before the request is made.

### Glossary locks

| Var                              | Default | Meaning                                                                                                                                                            |
| -------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `GLOSSARY_LOCK_WAIT`             | `5`     | Seconds an unattended stage waits for a busy glossary. After that it logs `glossary busy: <stage> skipped` and exits 0, so the nightly loop never waits on a human |
| `GLOSSARY_LOCK_WAIT_INTERACTIVE` | `600`   | Seconds a human's `--review`, `--revert` or `--prune-no-match` waits for the final write. On a timeout the answers are saved and applied at the next `--review`    |

### Mux

| Var                       | Default                  | Meaning                                                                                           |
| ------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------- |
| `KEEP_LANGS`              | `eng,en,dut,nld,nl,und,` | Audio and subtitle languages kept                                                                 |
| `SUB_LANGS`               | `eng,en,und,`            | Subtitle languages considered as a reference                                                      |
| `MIN_FREE_GB`             | `5`                      | Skip a mux below this free space rather than failing                                              |
| `DUR_TOL`                 | `2`                      | Seconds of duration mismatch tolerated                                                            |
| `DUB_SUFFIX`              | `.eng.dubtitles.srt`     | Sidecar the merge step looks for                                                                  |
| `REVIEW_GATE_SHOWS`       | _(empty)_                | Shows whose episodes wait for review before muxing                                                |
| `REVIEW_GATE_STALE_DAYS`  | `7`                      | A hold older than this is reported loudly and **stays held**. It buys a log line, never a release |
| `MEDIA_UID` / `MEDIA_GID` | `1000` / `100`           | Ownership set on written files                                                                    |

The muxed `Dubtitles` track is appended last and flagged default, so the relative order of the other subtitle tracks is unchanged while players pick it up through the default flag. An MP4 or M4V source is removed only after the remuxed MKV verifies and the stamp writes. If the stamp write itself fails, the new MKV is rolled back and the original file is left untouched, so a failed run always leaves something complete on disk. Plex reads `.ass` sidecars and renders styling and positioning on direct-play clients; transcoding clients burn them in.

### Review server

| Var                     | Default                | Meaning                                                                                                                                           |
| ----------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `REVIEW_PORT`           | `8842`                 | Listen port. Editable in Settings > General; requires container restart                                                                           |
| `REVIEW_BIND`           | `0.0.0.0`              | Listen address. Editable in Settings > General; requires container restart                                                                        |
| `REVIEW_TOKEN`          | _(retired for web UI)_ | Retired for browser access. On initial startup without an API key file, an existing value is imported once into `/config/apikey` and then retired |
| `REVIEW_AUTH`           | _(unset)_              | Set to `off` to disable authentication entirely (see Authentication)                                                                              |
| `REVIEW_MAX_CONCURRENT` | `16`                   | Concurrent request cap                                                                                                                            |
| `REVIEW_STEMS_TTL`      | `30`                   | Seconds an episode listing is cached                                                                                                              |
| `REVIEW_RESTART`        | `15`                   | Seconds before restarting the server after an exit                                                                                                |

### Ordering and watch queue

| Var                                        | Default                     | Meaning                                                                                                                                                                                                                   |
| ------------------------------------------ | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LIBRARY_SWEEP`                            | `True`                      | When True (default), loops sweep the whole library. When False, generator processes only queued requests and merge pass covers shows with requests pending, running, or finished in the last 7 days (Settings > Schedule) |
| `SEASON_START`                             | `0`                         | Global watch-order start season                                                                                                                                                                                           |
| `SEASON_PRIORITY_FILE`                     | _(unset)_                   | Per-show start seasons                                                                                                                                                                                                    |
| `WATCH_QUEUE_WINDOW_DAYS`                  | _(unset: step skipped)_     | Days of watch history to consider                                                                                                                                                                                         |
| `WATCH_QUEUE_PIN`                          | _(unset)_                   | Always queue this show first, watched or not                                                                                                                                                                              |
| `WATCHSTATE_URL` / `WATCHSTATE_API_KEY`    | _(empty)_                   | WatchState source                                                                                                                                                                                                         |
| `JELLYFIN_URL` / `JELLYFIN_API_KEY` / `JELLYFIN_USER` | _(empty)_ / _(empty)_ / _(empty: every user)_ | Jellyfin direct watch source; an empty user aggregates all users                                                                                                                                        |
| `PLEX_URL` / `PLEX_TOKEN` / `PLEX_SECTION` | _(empty)_ / _(empty)_ / `7` | Plex refresh and watch source                                                                                                                                                                                             |
| `PLEX_PATH`                                | _(empty)_                   | Path prefix Plex sees, if it differs                                                                                                                                                                                      |
| `JIMAKU_API_KEY`                           | _(empty: acquisition off)_  | Free key from jimaku.cc; used by `dubtitlerr.core.refs.acquire` to fetch a Japanese reference track for a show whose release ships none                                                                                   |

**How a watched show finds its folder:** by the show's TVDB id first — WatchState's
`parent.guid_tvdb`, the `tvdb://` Guid on the Plex show, or Jellyfin's `ProviderIds.Tvdb` —
against the `{tvdb-N}` in the folder name. Without an id it falls back to the title: exact,
then with the `(YYYY)` kept, then with it stripped, then case/punctuation-insensitive. A
title two folders share (e.g. `JoJo's Bizarre Adventure` for both the 1993 and 2012 series)
is reported as unmatched, never guessed — so name your folders `Title (YYYY) {tvdb-N}` and
the id settles it.

**No media server, no order file.** On the first sweep the generate loop builds
`anime_order.txt` from the top level of `ANIME_ROOT`: every folder holding a video becomes a
show, newest episode first. It never overwrites an existing file, so a hand-written list or a
watch-queue result stays authoritative. This is the fresh-install bootstrap, not a watch-order
replacement — once Plex, WatchState or Jellyfin is configured, that source owns the queue.

### Loop cadence

| Var                                        | Default                | Meaning                                                                                                                                                  |
| ------------------------------------------ | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MERGE_INTERVAL`                           | `600`                  | Seconds between merge passes                                                                                                                             |
| `MERGE_WINDOW`                             | _(empty, always)_      | Hours a merge sweep may run, `HH:MM-HH:MM`. May cross midnight. The end is exclusive. Set it when the repair backend is only up part of the day          |
| `STOP_FLAG`                                | `/tmp/dubtitlerr.stop` | File the entrypoint creates on SIGTERM/SIGINT or an API Stop. While it exists, loops stop starting work. Deleted at startup. Unset (manual runs), nothing ever stops |
| `PROCESSING_STATE_FILE`                    | `$CONFIG_DIR/processing.json` | Persisted `running` / `paused` / `stopped` state, with who and when. Created as `paused` at boot when absent |
| `PROCESSING_POLL_INTERVAL`                 | `15`                   | Seconds a paused loop sleeps between boundary checks                                                                                                    |
| `RESCAN_INTERVAL`                          | `21600`                | Idle seconds after a full generate sweep                                                                                                                 |
| `ACQUIRE_TIMEOUT`                          | `1800`                 | Seconds                                                                                                                                                  |
| `VERIFY_TIMEOUT`                           | `1200`                 | Seconds                                                                                                                                                  |
| `AUDIT_TIMEOUT`                            | `300`                  | Seconds the per-show names-hygiene audit may take (read-only; a timeout just logs)                                                                       |
| `LLM_TIMEOUT_CONNECT` / `LLM_TIMEOUT_READ` | `10` / `120`           | Seconds                                                                                                                                                  |

---

## Settings pages

Every environment variable the pipeline reads has a row under **Settings** in the web UI, in nine categories: General, Library, Transcription, Glossary, Repair, Connections, Output, Schedule and Connect. `#/settings` shows one card per category. `#/settings/<category>` shows the rows of that category.

**Connect** carries more than rows. Besides `PLEX_URL`, `PLEX_TOKEN`, `PLEX_SECTION`, `WATCHSTATE_URL`, `WATCHSTATE_API_KEY`, `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `JELLYFIN_USER`, `PLEX_PATH` and the webhook access rows, it holds the **Notifications** panel. Notification providers are a user-defined list stored beside Connections and have no environment variable; see [Notifications](#notifications).

The first-wave rows are editable. Every other row shows its value and why it cannot be changed here: "Set in the container environment" for the values the container fixes, "Set through a Connection", "Retired: no longer read", or "edit in a later release". For a signed-in user who is not an admin, the rows that would be editable are read-only and say "Only an admin can change settings". **Show Advanced** in the toolbar reveals the rows that rarely change. The bootstrap values always list, under General.

Where a value comes from, in order: what you saved in the UI (**Set here**), else the container's environment (**From the container**), else the built-in default (**Default**). A saved value that hides an environment value reads "Overrides the container value". Resetting a row removes your saved value, so the container value or the default applies again.

Saved values live in `$CONFIG_DIR/settings.json` (values, Connections and stage choices) and `$CONFIG_DIR/secrets.json` (the secrets, mode 0600). Each save keeps one `.prev` copy of each file and writes one log line that names the keys and never a secret value.

| A row says       | The change applies                                                                                                                                                                                                                                                                        |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| nothing special  | at the next show, sweep or merge pass, never in the middle of an episode                                                                                                                                                                                                                  |
| Restart required | after the builder restarts. The rows you can change that need one are the web port and address, the merge interval and window, the webhook token and path mappings. The read-only rows carry the same badge, for example the sign-in values, the container paths and the tool thresholds. |

After a save that changed a restart-only row the page offers **Restart Now** and **Restart Later**. Restart Now asks the builder to finish the episode in flight and stop. Restart Later leaves a "Restart pending" banner until the next start. The banner appears whenever a saved restart-only value is not running yet. The container comes back only with a restart policy of `unless-stopped` or `always`, and the supplied `compose.yaml` sets `unless-stopped`.

**Connections** are named LLM servers: a kind (llama.cpp, Ollama or OpenAI-compatible), a URL, a model, a second model for repair, an extra request body and connect and read timeouts. Each stage (repair, restore, verify, decision, acquire) picks one. A stage with no Connection keeps the container's own settings. Only an admin can save them or run Test.

Rules a saved Connection obeys:

- The URL must not carry a query or a fragment. Tokens belong in a secret field.
- Addresses in 169.254.0.0/16 and fe80::/10 are refused, and so are the cloud metadata addresses 100.100.100.200 and fd00:ec2::254. Loopback and LAN addresses are fine.
- An API key is write-only. You can set it or clear it, and nothing ever shows it again. It is 8 to 512 characters on one line. Only an OpenAI-compatible Connection holds one, and it is only sent over https, or to a loopback host.
- The extra request body is write-only too. The page shows Set or Not set and a Replace body field. Only an OpenAI-compatible Connection carries one.
- Changing a URL or a kind clears the stored key unless you enter the key in the same save.
- The decision stage accepts only the hosts the decision client calls: openrouter.ai, api.commandcode.ai, or loopback.
- **Test** asks the server to probe a saved Connection with its own timeouts.

Secrets (the Plex token, the WatchState and Jellyfin keys, the webhook token, Connection keys and notification credentials) are entered only by a signed-in admin. Until login is enabled the page shows "available after login is enabled" and a plain set or not set.

The first start after the upgrade imports the keys an existing `runtime.env` carried: `REPAIR_MODEL`, `VERIFY_BACKEND`, `VERIFY_DECISION_MODEL`, `REQUIRE_ENG` and `ACQUIRE_APPLY`. A `REPAIR_PROFILE` choice becomes the repair stage. The old `RUNTIME_VERIFY_BACKENDS` and `RUNTIME_DECISION_MODELS` opt-in filter still applies to what is imported. The file is renamed `runtime.env.migrated`, or an empty one is written so the import runs once. Under its own `profiles.migrated` marker, each `REPAIR_PROFILES` entry becomes a Connection. `REPAIR_PROFILES`, `RUNTIME_VERIFY_BACKENDS` and `RUNTIME_DECISION_MODELS` are retired: nothing reads them after that, and General lists them while they are still set.

### Notifications

The **Notifications** panel under Settings > Connect (`#/settings/connect`) is a list of outbound providers. Each one is a destination: a name, a kind, a `triggers` list, and that kind's fields. An admin saves the list and runs Test; a signed-in non-admin sees the providers read-only. Resetting the Connect category clears the list.

Providers live in `$CONFIG_DIR/settings.json` under `notifications` (visible fields) and `$CONFIG_DIR/secrets.json` under `notifications.<name>` (the write-only parts, mode 0600). `dubtitlerr/core/notify.py` reads exactly those keys, so the panel and the sender cannot disagree about the file layout. There is no environment variable and no config file of its own: an install with no providers is a silent no-op.

**Kinds and fields.**

| Kind      | Visible fields     | Write-only secret | Sends |
| --------- | ------------------ | ----------------- | ----- |
| `discord` | `link_url` (optional) | `url` (webhook) | `POST` one embed per event (below) to the webhook URL |
| `ntfy`    | `server`, `topic`  | `token` (optional)| `POST server/topic` with the summary as the body, `Title: <event>`, `Priority: 5` on `pass.failed` and `default` otherwise, `Authorization: Bearer <token>` when a token is set |
| `webhook` | _(none)_           | `url`             | `POST` the JSON envelope (below) verbatim |

| Field      | Applies to     | Rule |
| ---------- | -------------- | ---- |
| `name`     | all            | letters, digits, `.`, `_`, `-`; up to 64 characters; unique |
| `kind`     | all            | `discord`, `ntfy` or `webhook` |
| `enabled`  | all            | `false` stores the provider and never sends to it |
| `triggers` | all            | a list of event names from the table below; empty means nothing is sent |
| `server`   | `ntfy`         | required; an http(s) URL passing the guards below |
| `topic`    | `ntfy`         | required; letters, digits, `_`, `-`; up to 64 characters |
| `url`      | `discord`, `webhook` | required; http(s), one line, 1 to 512 characters, passing the guards below |
| `token`    | `ntfy`         | optional; one line without spaces or control characters, 1 to 512 characters |
| `link_url` | `discord`      | optional; an http(s) URL passing the guards below. Absent (or unusable, if edited by hand) means the embed carries no link |

The webhook envelope is `{"event", "at", "host", "loop", "summary", "counts", "detail"}`. It has no `version` field: the repo exposes no runtime application version, so inventing one would give a consumer nothing it could check.

**The Discord embed.** One embed per event: the event's title, a severity colour (green for a completed sweep, merge or delivery; blurple for `run.requested`; amber for `review.pending` and `episode.refused`; red for `pass.failed`), the summary as its description, the `counts` (and scalar `detail` entries) as inline fields, a footer naming the host and loop, and a UTC timestamp. Every string is clipped to Discord's per-embed limits, and `allowed_mentions` stays empty so a summary can never ping a role. With `link_url` set, the embed's title also links to the review-workspace view the event belongs to — `#/runs` for `run.requested`, `#/inbox` for `review.pending`, `#/dashboard` otherwise. The link names a view, not an episode: the workspace's hash router addresses views only. The `ntfy` and `webhook` kinds are unchanged.

**Triggers.**

| Event               | Sent when | Carries |
| ------------------- | --------- | ------- |
| `run.requested`     | a run request is created from the web UI or the API (in-process, by the review server) | summary naming the show |
| `sweep.completed`   | a generation sweep ends | `shows`, `failed`, `review_pending` |
| `merge.completed`   | a merge pass ends | `delivered`, `failed`, `total_done` |
| `pass.failed`       | a loop or stage failure fence: a stalled generate pass, a merge pass that cannot start (missing `ffmpeg`, `mkvmerge`, `pysubs2` or library root), or the generate loop exiting | `detail`: `{"stage", "reason"}` |
| `review.pending`    | a generation sweep ends with unresolved review items (count > 0) | `pending` |
| `episode.delivered` | a merge pass delivers an episode. Off by default in the panel, because it is one message per episode | `detail`: `{"mode": "mux"}` or `"sidecar"` |
| `episode.refused` | a merge pass parks an episode its publish gate refused, after the one deterministic repair attempt. Opt in per provider in the panel: a parked episode is a thing a human has to see | `detail`: `{"stage": "publish-gate"}` |

A provider receives only the events its `triggers` list names. For `episode.delivered` and
`episode.refused` the merge pass asks once per pass whether any enabled provider subscribes
(`python3 -m dubtitlerr.core.notify subscribed`), so an unsubscribed install pays no per-episode
subprocess. A refusal is escalated once per distinct finding: re-attempting the same un-normalizable
file on the next pass keeps the merge log line but does not repeat the message.

**Delivery semantics.**

- One request per provider per event, with a single 10-second timeout: urllib exposes one timeout covering connect and reads, so connect cannot be bounded separately from reads.
- A timeout, a network error or a 5xx retries **once**. A 4xx does not retry.
- Delivery never raises and never fails or stalls a run: the loops call it with `|| true`, the review server calls it in-process around a request that is already written, and the CLI exits 0 even when every delivery failed. A failure is one log line naming the provider, the event and the outcome.
- **Secrets never reach a log.** No message contains a webhook URL, a token, or the exception text; the log records only the outcome (`ok`, `HTTP <status>`, `timeout`, `network error`).
- URL safety mirrors Connections: http(s) only, no user name or password in the URL, and a refusal for link-local (`169.254.0.0/16`, `fe80::/10`) and cloud metadata addresses however they are spelled. Loopback and LAN hosts are allowed. A redirect is followed only into the same safe URL space.
- An unusable `settings.json` or `secrets.json`, an unknown kind, or a missing secret is logged and skipped; nothing else in the pipeline changes.

**Test button.** Save the provider first, then click Test. The page enables the button only when the provider exists on the server and its kind, server and topic match what is stored, with no unsaved secret typed into the row — Test exercises the saved configuration, not a draft. The page calls `POST /api/settings/test` with `{"notification": "<name>"}` (admin), which reaches the same code path as the CLI and sends a `notification.test` event to that provider regardless of `enabled` or its triggers. Discord and webhook providers hold their URL as a write-only secret, so the URL must be entered and saved before the button can work.

The same sender is available without the UI:

```sh
python3 -m dubtitlerr.core.notify test  --name <provider>
python3 -m dubtitlerr.core.notify send  --event sweep.completed --summary "..." --counts '{"shows": 12}'
python3 -m dubtitlerr.core.notify subscribed --event episode.delivered   # exit 0 when some provider subscribes
```

---

## Glossary file

`$GLOSSARY_DIR/<Show folder name>.json`. Keys not listed here are ignored on load. The wiki-verifier is reusable on its own: it makes any mined or community-submitted glossary as accurate as a hand-curated one.

```json
{
  "show": "One Piece",
  "wiki": "https://onepiece.fandom.com/api.php",
  "initial_prompt": "This is One Piece. Spell names correctly: Luffy, Zoro, ...",
  "unanchored_repair": false,
  "names": ["Luffy", "Zoro", "Spandam"],
  "phrases": ["Enies Lobby", "Water 7"],
  "hard_fixes": { "ruffy": "Luffy", "spondum": "Spandam" },
  "verified": ["Luffy", "Zoro"],
  "flagged": { "Raftel": { "reason": "respelling-needs-review", "canonical": "Ratel" } }
}
```

| Key                  | Type   | Meaning                                                                                                                                                                   |
| -------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `show`               | string | Display name, used in prompts                                                                                                                                             |
| `wiki`               | string | MediaWiki API URL override (editable on the Glossary page with admin Test verification; empty auto-detects)                                                               |
| `initial_prompt`     | string | Passed to Whisper. **Measured to make no difference** to output; kept for compatibility                                                                                   |
| `unanchored_repair`  | bool   | `true` when your copies of this show carry no English subtitles for the Japanese audio                                                                                    |
| `names`              | list   | Proper nouns                                                                                                                                                              |
| `phrases`            | list   | Multi-word terms                                                                                                                                                          |
| `hard_fixes`         | object | Exact replacements. A key containing a space is matched as a phrase, otherwise as a token. Keys are lowercased on load                                                    |
| `verified`           | list   | Verifier bookkeeping                                                                                                                                                      |
| `flagged`            | object | Terms a human still has to decide, or that the verifier could not confirm. Each value is a reason string or an object `{"reason", "canonical", ...}`. See the table below |
| `known`              | list   | Terms a human marked correct as they stand                                                                                                                                |
| `acquired`           | object | Provenance of each name acquisition or a human review wrote                                                                                                               |
| `review_rulings`     | object | Durable review verdicts, keyed by the casefolded term: `{decision: "dismiss"\|"apply", reason, canonical, by: "auto"\|"operator", policy}`. A dismissed variant never re-queues |
| `review_policy`      | object | Last per-tier counter snapshot written by the review policy (P33 reads this)                                                                                              |
| `arc_titles`         | object | Arc name (or `scope:<rung>:<season>`) to the wiki titles stored for it. Written by `--tag-arcs`                                                                           |
| `arc_coverage`       | object | Per arc: how many titles, whether redirects finished, whether it is waived or stale. The gate trusts this                                                                 |
| `arc_titles_missing` | list   | Arcs whose fetch came back empty, partial or under `ARC_MIN_TITLES`. While it is non-empty the arc gate does nothing                                                      |
| `arc_tags`           | object | Lower-cased name to the arcs it belongs to                                                                                                                                |
| `arc_aliases`        | object | `{"Water Seven": "Water 7"}`: the wiki page to fetch for a season whose arc has another name. You write it                                                                |
| `arc_waived`         | list   | Seasons you accept as covered with no wiki titles, such as seasons that are not arcs. You write it                                                                        |
| `wiki_scope`         | object | Scope for a show with no arc names. See [Wiki scope](#wiki-scope)                                                                                                         |

**Flag reasons.** What a human sees in `python3 -m dubtitlerr.glossary.glossary_acquire --review`:

| Reason                      | What it asks                                                                                                    | `y` does                            |
| --------------------------- | --------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| `respelling-needs-review`   | The model picked a different spelling, or a same-length expansion the whole-word guard refused                  | Writes the fix                      |
| `common-word`               | An ordinary one-word term the model wants to expand                                                             | Writes the expansion to `phrases`   |
| `not-in-arc-pages`          | The title is on no arc page (only with `VERIFY_ARC_GATE=1`)                                                     | Writes the expansion to `phrases`   |
| `expansion-guarded`         | An expansion a guard blocked, shown only when `VERIFY_QUEUE_BLOCKED_EXPANSIONS=1` and the title has more tokens | Writes the expansion to `phrases`   |
| `low-confidence`            | The model had a candidate and was not sure                                                                      | Accepts the candidate               |
| `possessive_floor_crossing` | A possessive form that crossed the mining floor                                                                 | Keeps both forms                    |
| `unseen-needs-evidence`     | A real spelling difference the transcript never corroborates                                                    | Accepts the candidate               |
| `conflicts-with-curated-fix`| The proposal disagrees with a fix a person owns                                                                 | Overrides the curated fix           |
| `no-wiki-match`             | A tier-B candidate from a different lookup, not on this wiki's title list                                       | Accepts the candidate               |
| `no-match`                  | No candidate. Not queued unless `VERIFY_QUEUE_NO_MATCH=1`                                                       | Marks the term correct as it stands |

`--review` lists decisions first, in that order, and collapses the no-match terms into one line;
`--review --all` lists them too, and `--review --prune-no-match` removes the stored ones.
An answer is applied only to the proposal the human saw: if the term was flagged again with a
different canonical before the answer could be written, the answer is skipped and logged.

### Wiki scope

A show whose seasons carry no arc names (Naruto, Pokémon, a one-season show) gets its spelling
universe from a `wiki_scope` block in its glossary. `--tag-arcs` resolves one scope per library
season, or one for the whole show, and stores only **complete** results: a failed request, a
partial listing or an unparseable table stores nothing. The library files need an `SxxExx` tag in
their names for the season and episode scopes.

| Rung          | Reads                                                                                                                                       | Use for                                           |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| `arc_index`   | An index page with one table per part (arc, chapters, episodes). Library episodes are mapped to the table's numbering with `season_offsets` | Naruto, Naruto Shippuden                          |
| `season_page` | One wiki page per season, with episode codes and episode titles dropped                                                                     | Pokémon (`S01` to `S25` on Bulbapedia)            |
| `whole_wiki`  | Every page of the wiki, with chapter, volume and episode pages dropped                                                                      | One-season shows (Marriagetoxin, Chainsmoker Cat) |

```json
{
  "wiki_scope": {
    "rung": "arc_index",
    "wiki": "https://naruto.fandom.com/api.php",
    "index_page": "Plot of Naruto",
    "part": "Part II",
    "season_offsets": { "2": 32, "3": 53 }
  }
}
```

A whole-wiki scope tags no names and is left out of the arc gate's universe, because it contains
every page verify could propose. Do not rely on a wiki's `Category:Characters`: Chainsmoker Cat's
has one member of 33. The full key list is in the docstring of `wiki_scope.py`.

The `wiki` URL is not Fandom-only: any public https MediaWiki `api.php` works, Wikipedia included
(`https://en.wikipedia.org/w/api.php`). Automatic probing still suggests `*.fandom.com` candidates
only; a plain MediaWiki is an explicit per-show choice. A URL that is not an `api.php` endpoint, a
non-https URL, or an internal/link-local host is refused before any request.

### Names-hygiene audit

`glossary_audit` cross-references a glossary's `names` against its own `flagged` map and the wiki
cache, and **acts on** entries that are junk-classified yet still listed as names: a flag reason
(`english-word`, `sentence-initial-only`, `common-word`, `no-match`, `below-floor`)
or a capitalised ordinary English word with no exact wiki page. An exact wiki title protects a name
however ordinary it looks (`Fern`, `Hero`, `Land`), and a lowercase entry (`mana`, `mage`) is treated
as curated prompt vocabulary, never junk. It runs in the nightly sweep right after VERIFY with
`--fix`, which removes what it reported and logs the removal to `<glossary>.cleanup.json` (newest 20
runs); a second sweep finds nothing and writes no new log entry. Without a cached wiki index nothing
is removed: the entries are listed as `unevaluated` rather than guessed, because the exact-title
check is what protects real cast. By hand:

```sh
python3 -m dubtitlerr.glossary.glossary_audit "/config/glossaries/<Show>.json" [--titles FILE] [--fix]
```

Exit `0` = nothing left to fix (clean, or clean after `--fix`), `1` = junk reported but not removed
(`--fix` had no wiki to decide on, or the report-only mode was used), `2` = the glossary could not be
read.

### Review policy: tiers, durable verdicts and backfill

VERIFY and ACQUIRE re-classify the same unsettled terms every sweep, so guard verdicts used to pile
up as review entries (Frieren: ~666 of 798; the live library reached 1,877, 82% of them
`english-word`). The review policy now resolves each generated verdict by tier, at generation time:

| Tier | Reasons | What happens |
| ---- | ------- | ------------ |
| 1 | `english-word`, `below-floor`, `sentence-initial-only` | Dismissed into `archived_flags` counts + `.archived.jsonl`. **Guardrail:** only when the variant matches no exact wiki title, no cast term (`names`/`phrases`/`known`/`acquired`) and no `hard_fix`; without cached wiki titles the verdict is queued (fail-closed) |
| 2 | `respelling-needs-review`, `possessive_floor_crossing` | The correction is applied as a `hard_fix` only when the canonical is already a glossary canonical (names/phrases/fix value), the effect is a spelling fix, and no curated fix disagrees. Anything else is queued |
| 3 | `unseen-needs-evidence`, `conflicts-with-curated-fix`, `no-wiki-match`, and every unknown reason | Queued: the only entries that need a person |
| — | `common-word`, `no-match` | Collapsed to counts (P24); neither carries a decision |

Every dismissal/approval (auto or operator) writes a durable `review_rulings` entry, so the same
variant+reason cannot re-enter the queue on a later sweep. Auto decisions also append a JSON line to
`<glossary>.review-audit.jsonl` with the shape shared with the P33 audit view:

```json
{"at": 1795319493.2, "term": "Magic", "reason": "english-word", "decision": "dismiss",
 "tier": 1, "canonical": "Magic Knight", "applied": null, "guard": null,
 "by": "auto", "policy": "p32-v1", "show": "Frieren ..."}
```

`undo` removes an auto ruling and the fix only if the policy wrote it (a human-owned `run: review`
fix is never touched). Drain an existing pile with the backfill tool; it reads each show's cached
wiki titles and leaves the file byte-identical when nothing is due:

```sh
python3 -m dubtitlerr.glossary.review_policy "/config/glossaries/<Show>.json" [--titles FILE] [--dry-run]
python3 -m dubtitlerr.glossary.review_policy --dir /config/glossaries --dry-run   # whole library
python3 -m dubtitlerr.glossary.review_policy "/config/glossaries/<Show>.json" --undo <Term>
```

A bulk rewrite first copies the glossary to `<file>.bak-p32-<UTC stamp>`. Exit `0` = drained or
nothing due, `1` = a load/write failure, `2` = bad arguments.

### Queue hygiene: the app owns its backlog (P33)

P32 stops the noise at the source; the queues that remain are still bounded and self-reporting, and
every automatic decision is inspectable and reversible from the workspace.

| Rule | Value | What it does |
| ---- | ----- | ------------ |
| Expiry | 14 days | An open review item with no action is dismissed, audited and reversible (`undo`). Applied at read, persisted by the sweep |
| Bound | 200 per queue | Oldest open items are expired first, so a pile can never sit at 700. Sampled evidence is capped at 25 lines per class per day |
| Self-report | class ≥ 50 | The sweep reports each class over threshold with a resolution proposal instead of waiting for someone to notice |
| Audit | 500 lines per file | A bounded ring: newest records survive, so the audit cannot become the next growing log |

The review workspace opens on **Recent decisions** (`#/audit`): what the app decided, why, and a
one-tap undo. The pending queues (`#/inbox`, `/api/episode`) are the exception list. Undo restores
the prior state and records the reversal (`decision: "restore"`, `by: "operator"`):

```sh
python3 -m dubtitlerr.core.queue_hygiene [--glossary-dir DIR] [--media-root DIR] [--dry-run] [--json] [--notify]
```

The sweep runs at the end of every generate pass. Notifications are batched: one `review.digest`
event per sweep, and `review.pending` fires only for the exception classes (P32 tier 3 plus
`decision_unfittable`/`verdict_owed`) — there are no per-item notifications.

`.unresolved.jsonl` is now counters + sampled evidence, not an append-only log: every record bumps
`<stem>.dubtitles.unresolved.counters.json` (per day and per class), while evidence lines are kept
only up to the daily sample cap. `unresolved.metrics(stem)` returns the per-day aggregates, and
queue expiry/undo events go to `<stem>.dubtitles.unresolved.audit.jsonl`.

### Bounded flag storage

Guard verdicts still land in `archived_flags` as counts by class
(`{"counts": {"english-word": 412, "below-floor": 180, ...}, "total": 666}`), recomputed on each
write and therefore stable across reruns, while their full entries are appended (deduplicated by
term+reason) to `<glossary>.archived.jsonl`. Entry-form `flagged` is kept for the classes a human
actually decides: `respelling-needs-review`, `low-confidence`, `unseen-needs-evidence`,
`conflicts-with-curated-fix`, `possessive_floor_crossing`, `transcript-new-term`, `no-wiki-match`
(and acquire's `share-too-close`/`not-in-arc-pages`/`expansion-guarded`).

`glossary_verify` can be run by hand:

```sh
python3 -m dubtitlerr.glossary.glossary_verify "/config/glossaries/<Show>.json" [--wiki URL] [--force]
```

---

## Decision store

`$DECISIONS_DIR/<Show folder name>.json`. One store per show, intended to be committed to
git.

An entry is keyed on the **normalised text pair**, never on episode or line number:

```json
{
  "orig": "dothamingo's coming",
  "proposed": "doflamingo's coming",
  "verdict": "correct",
  "text": "Doflamingo's coming.",
  "run": "review",
  "at": 1756600000.0
}
```

**Verdicts:** `accept`, `reject`, `correct`, `force`.

`force` admits a repair the mechanical gate refused. It overrides the judgement checks but
never the card-fit check, because card timing is immutable.

**Normalisation for matching:** case and runs of whitespace are folded; the curly apostrophe
`'` is folded to `'`. **Punctuation is otherwise kept** — it is part of a line's identity.

---

## Review server routes

The review server is started by `container_run.sh` and listens on `REVIEW_BIND` and `REVIEW_PORT` (default `0.0.0.0:8842`).
Access is controlled by `ROUTE_ROLES`. Every route requires a specific role: `public`, `user`, or `admin`. Unlisted routes fail closed and require `admin`.

| Method | Path                      | Role      | Purpose                                                                  |
| ------ | ------------------------- | --------- | ------------------------------------------------------------------------ |
| GET    | `/healthz`                | `public`  | Health probe: 200/503 from the builder heartbeat (see Heartbeat and health) |
| GET    | `/api/auth-mode`          | `public`  | Return setup_required and accounts_ok status                             |
| POST   | `/api/login`              | `public`  | Authenticate with username and password; sets session cookie             |
| POST   | `/api/setup`              | `public`  | Create initial administrator account (409 if already set up)             |
| GET    | `/review`, `/review/*`    | `public`  | Single-page application assets and review workspace                      |
| POST   | `/api/webhook/sonarr`     | `public`* | Sonarr webhook receiver (*requires `WEBHOOK_TOKEN` header or query)      |
| POST   | `/api/webhook/radarr`     | `public`* | Radarr webhook receiver (*requires `WEBHOOK_TOKEN` header or query)      |
| GET    | `/`, `/index.html`        | `public`  | Redirects to `/review/`; the server-rendered page is gone                |
| GET    | `/shared`                 | `public`  | Redirects to `/review/#/shared`                                          |
| GET    | `/api/audio`              | `user`    | Audio dialogue clip playback for cue review                              |
| GET    | `/api/episodes`           | `user`    | List episodes with pending review items                                  |
| GET    | `/api/shows`              | `user`    | List library shows                                                       |
| GET    | `/api/episode`            | `user`    | One episode's queue in decision order                                    |
| POST   | `/api/decide`             | `user`    | Record human verdicts on episode cues                                    |
| GET    | `/api/shared`             | `user`    | Repair pairs occurring across multiple episodes                          |
| POST   | `/api/shared`             | `user`    | Record verdicts on shared lines                                          |
| POST   | `/api/apply`              | `user`    | Rewrite subtitle and drop done stamp for re-mux                          |
| GET    | `/api/requests`           | `user`    | List queued and recent run requests                                      |
| GET    | `/api/schedule`           | `user`    | Read schedule window configuration                                       |
| GET    | `/api/settings`           | `user`    | Read effective settings values                                           |
| GET    | `/api/settings/schema`    | `user`    | Read settings registry schema metadata                                   |
| GET    | `/api/status`             | `user`    | Read builder status, active sweeps, and heartbeat                        |
| GET    | `/api/glossary`           | `user`    | List glossary shows or read one show glossary                            |
| POST   | `/api/glossary`           | `user`*   | Add, update, or delete glossary terms (*editing `wiki` requires `admin`) |
| GET    | `/api/me`                 | `user`    | Return authenticated username and role                                   |
| PUT    | `/api/me/password`        | `user`    | Self-service password change for currently signed-in user                |
| POST   | `/api/logout`             | `user`    | End session and revoke session token                                     |
| POST   | `/api/glossary/wiki-test` | `admin`   | Probe MediaWiki siteinfo for a wiki URL override                         |
| POST   | `/api/requests`           | `admin`   | Enqueue a new run request                                                |
| PUT    | `/api/schedule`           | `admin`   | Update schedule window settings                                          |
| PUT    | `/api/settings`           | `admin`   | Save setting values, connections, and stage assignments                  |
| POST   | `/api/settings/reset`     | `admin`   | Reset settings to environment or built-in defaults                       |
| POST   | `/api/settings/test`      | `admin`   | Test an LLM connection endpoint                                          |
| POST   | `/api/system/restart`     | `admin`   | Trigger a soft stop and restart of the builder container                 |
| GET    | `/api/system/processing`  | `user`    | Read the persisted processing state, who changed it and when            |
| POST   | `/api/system/start`       | `admin`   | Start processing: persist `running` and clear a stale stop flag          |
| POST   | `/api/system/pause`       | `admin`   | Pause after the unit in flight: persist `paused`                         |
| POST   | `/api/system/stop`        | `admin`   | Soft stop: persist `stopped`, write the stop flag, drain, exit           |
| GET    | `/api/apikey`             | `admin`   | Retrieve current administrator API key                                   |
| POST   | `/api/apikey/reset`       | `admin`   | Rotate administrator API key                                             |
| GET    | `/api/users`              | `admin`   | List user accounts and roles                                             |
| POST   | `/api/users`              | `admin`   | Create a new user account                                                |
| PUT    | `/api/users/*`            | `admin`   | Update a user's role or password                                         |
| DELETE | `/api/users/*`            | `admin`   | Delete a user account                                                    |
| GET    | `/api/v1/system/status`   | `user`    | Version, uptime, loop state, queue depth, library cache and health       |
| GET    | `/api/v1/health`          | `user`    | The `/healthz` check with a JSON body                                    |
| GET    | `/api/v1/queue`           | `user`    | Queued and running run requests, with counts                             |
| GET    | `/api/v1/history`         | `user`    | Recent deliveries and stage outcomes, paged                              |
| GET    | `/api/v1/library`         | `user`    | Shows in the library                                                     |
| POST   | `/api/v1/command`         | `admin`   | `{"name", "params"}`: `run.request`, `system.restart`, `system.start`, `system.pause`, `system.stop`, `notification.test` |
| GET    | `/api/v1/notifications`   | `user`    | List notification providers (secrets reported as set or not set)         |
| POST   | `/api/v1/notifications`   | `admin`   | Create a notification provider                                           |
| GET    | `/api/v1/notification/*`  | `user`    | Read one notification provider                                           |
| PUT    | `/api/v1/notification/*`  | `admin`   | Update one notification provider                                         |
| DELETE | `/api/v1/notification/*`  | `admin`   | Delete one notification provider                                         |
| GET    | `/api/v1/settings`        | `user`    | Effective settings, read-only, secrets masked                            |
| GET    | `/api/v1/apikey/info`     | `user`    | API key rotation state, never the key                                    |
| GET    | `/api/v1/openapi.json`    | `user`    | The OpenAPI document for this surface                                    |

### Heartbeat and health

`/healthz`, `/api/v1/health`, `loop.state` in `/api/v1/system/status` and the Dashboard's
"Builder heartbeat" figure all read **one** JSON document, `$HEARTBEAT_PATH` (default
`/config/heartbeat.json`). The running loops write it, so an install that is doing work always has
a fresh one:

| Writer          | When                                              | Fields it sets                                                                                          |
| --------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `gen_loop.sh`   | at the start of every sweep                        | `last_sweep_start`, `roots_readable`, `current_show` (cleared)                                           |
| `gen_loop.sh`   | before each show, and before each run request      | `current_show`                                                                                           |
| `gen_loop.sh`   | when the sweep's show loop finishes                | `last_sweep_end`, `current_show` (cleared), `roots_readable`, `order_file_present`, `sweep_shows`, `sweep_failed` |
| `gen_loop.sh`   | on every idle poll while waiting for a rescan      | `last_beat` (liveness only)                                                                              |
| `merge_pass.sh` | when a merge pass starts, and when it ends         | `merge_last_start`, `merge_last_end`, `merge_delivered`, `merge_failed`                                   |

Every write merges into the existing document (so the two loops never drop each other's fields),
is atomic, and is best-effort: a heartbeat that cannot be written is never allowed to fail or delay
a sweep, the same rule runtime notifications follow. A misbuilt image without the writer module
still runs — it just has no heartbeat, so `/healthz` stays at `503`.

**What the readers decide.** `RESCAN_INTERVAL` (default 21600 s, six hours) is the loop's idle
period between sweeps; a heartbeat is **stale** once `last_sweep_end` is older than three idle
periods (`STALE_SWEEP_MULTIPLIER × RESCAN_INTERVAL` — 18 hours by default). One constant, in
`dubtitlerr/core/heartbeat.py`, so the two routes cannot disagree:

| Heartbeat state                                             | `/healthz` and `/api/v1/health`                    | `loop.state` |
| ----------------------------------------------------------- | -------------------------------------------------- | ------------ |
| no document at all                                          | `503`, reason `no heartbeat yet`                   | `unknown`    |
| a sweep is in progress (no `last_sweep_end` yet)             | `503`, reason `stale: no sweep has completed yet`  | `running`    |
| `last_sweep_end` within `3 × RESCAN_INTERVAL`                | `200`, `{"ok": true}`                              | `idle`       |
| `last_sweep_end` older than that                             | `503`, reason `stale: last sweep ended Ns ago`     | `stalled`    |
| `roots_readable` / `order_file_present` is false             | `503`, with that reason                            | reported in `loop` |
| the soft-stop flag exists                                    | unchanged                                          | `stopping`   |

A container that has not yet **completed** a sweep is unhealthy on purpose: at this granularity an
in-progress sweep cannot be told from one wedged mid-episode, and that is the failure the probe
exists to catch. An install with `LIBRARY_SWEEP=off` reports `order_file_present: true` at sweep
end, because no order file gated its request-only run. `current_show` is what the Dashboard shows
while a sweep works on a show.

The container healthcheck in `compose.yaml` is a TCP connect to the review port, not a `/healthz`
request, so Docker can read healthy while `/healthz` is `503`. If that distinction matters, point
your own monitor at `/healthz` (public) or `/api/v1/health` (API key).

### `/api/v1`: the external contract

`/api/v1` is the **frozen, additive** contract for external consumers: agents, scripts and future
tools. The classic `/api/*` routes above remain the web UI's backend and move with it; nothing in v1
renames, moves or changes them. The two surfaces share one role table and one auth path.

- **Roles.** Every v1 read requires `user`; every v1 write, including `POST /api/v1/command`,
  requires `admin`. A v1 path the module does not serve still fails closed at the role table, so it
  needs `admin` rather than falling open.
- **Auth.** `X-Api-Key` is an admin credential throughout; a valid key also exempts a request from
  the browser CSRF header, so an agent sends the key and a JSON body and nothing else.
- **No stored secret is returned.** A notification reports its URL or token as `{"set": true}`;
  `GET /api/v1/settings` reuses the masked Settings shape; `GET /api/v1/apikey/info` returns rotation
  state only.
- **Commands.** `run.request` takes the same body as `POST /api/requests` (`show` required;
  `season`, `episode_re`, `require_eng`, `webhook`, `force` optional) and returns that route's status
  code through the envelope `{"name", "ok", "result"}`, including the 409 for an identical pending
  request. `force: true` means *start over*: every in-scope episode is rebuilt from its cached ASR
  words (the stored `.dubtitles.words.json`; no re-transcription unless the words are missing),
  overriding the freshness stamps, and the run's summary line reports `forced=1 rebuilt=N`. A forced
  request that rebuilds nothing is filed as failed. `system.restart` writes the soft-stop flag. `system.start`, `system.pause` and
  `system.stop` persist the same processing state the Settings panel controls (start clears a
  stale stop flag; stop also writes it). `notification.test` needs `params.name`.
- **History paging.** `limit` defaults to 25 and must be 1 to 200; `offset` is 0 or more. A value out
  of range is a 400 rather than a clamp. The first call after a library walk reads up to two small
  sidecars per episode, capped at 5,000 episodes; past the cap the response carries
  `truncated: true`.
- **Deliberately absent:** removing a queued request, scoped or multiple API keys, config writes
  beyond notifications, and an event stream.
- **The spec.** The static `dubtitlerr/review/openapi.json` is served at `/api/v1/openapi.json`; each
  operation carries `x-dubtitlerr-role`. `tests/test_review_api_v1_openapi.py` compares the spec with
  the live role table and fails when a path or a role drifts, so the document is never a stale
  description of the surface.

### Authentication and security

Authentication uses role-based access control with two roles: `admin` and `user`.

- **User sessions:** Browsers authenticate against scrypt password hashes in `$CONFIG_DIR/users.json`. A successful login sets an HttpOnly, SameSite=Strict session cookie backed by `$CONFIG_DIR/sessions.json`.
- **CSRF protection:** Browser state-changing requests (POST, PUT, DELETE) must provide an `X-DubTitlerr-Request: 1` header. Origin headers are validated against Host and X-Forwarded-Host headers.
- **Admin API key:** Automation scripts send the `X-Api-Key` header instead of browser cookies. The key is stored with mode 0600 at `$CONFIG_DIR/apikey`. `X-Review-Token` is accepted as a deprecated alias for the API key.
- **First-run setup:** When no accounts exist, unauthenticated requests to protected routes return 409, directing browsers to `/review/setup`. Setting `REVIEW_USERNAME` and `REVIEW_PASSWORD` (or `REVIEW_PASSWORD_FILE`) automatically creates the initial admin account at first boot.
- **Disabling authentication:** Setting `REVIEW_AUTH=off` disables role checks on all routes and treats all requests as administrator. Only use this on a private, trusted network.
- **Audit logging:** Actions that modify settings, connections, accounts, glossaries, or trigger container restarts write log lines that explicitly name the acting user or API key principal.
