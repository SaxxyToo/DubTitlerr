# Design decisions

Understanding-oriented. The decisions that are expensive to rediscover, recorded because the code
that carried them no longer explains itself. Each one names the evidence that forced it, or says
plainly where evidence is still owed, so a future change can be argued against the record rather
than against a comment.

If you are about to reverse one of these, read the evidence first. The first two were made after an
earlier instinct turned out to be wrong. The rest were made against a measured gap, and the
Jellyfin entry is marked as a reason rather than a measurement.

---

## 1. Idempotency is keyed on two version tiers, not one

**Accepted 2026-08-24.** The constants are `TRANSCRIBE_VERSION` and `TEXT_VERSION` in `dubtitlerr/core/common.py`.

### The problem

A single `PIPELINE_VERSION` made every change equal. A glossary correction that alters only what a
caption *says* invalidated the library exactly as hard as a decoder change, forcing full
re-transcription — GPU-days on a small card. The 2026-08-21 architectural review ranked it the
largest recurring cost in the system.

Two facts sharpened it:

- The raw Whisper word list is not persisted — `generate.py` asks for `word_timestamps=True` and the
  words die after reflow — so changes at the word layer (punctuation, card splitting) genuinely
  cannot be re-run without the GPU.
- `tools/reapply_glossary.py` already re-applied a glossary from `conf.json` with no GPU, no LLM and
  no re-transcription. The cheapest and most frequent case was already solved; nothing recorded that
  it had happened, and nothing ran it automatically.

A live measurement that day found 3,889 videos against 813 stamps — 576 at v4, 236 at v2, 1 at v3 —
so the library had been sitting several versions deep in inconsistency without anything reporting it.

### The decision

Idempotency is keyed on **two** constants, adopted at 4 and 5 so the 576 live v4 stamps read as
transcribe-fresh and text-stale and migrate at watch-gated pace.

The boundary rule: everything computed from `(words, segments, conf.json, fansub, LLM)` is **text**;
everything computed from audio or the decoder is **transcription**. The one genuinely undecidable
case — an operator changing decoder-affecting configuration — has no mechanical signal and is
carried by documentation in both constants' docstrings.

Rejected alternatives:

- **Per-stage versioning**, one constant per stage with a dependency graph. More precise, and the
  precision is not the problem: a wrong edge silently skips a stage that needed to run, which is the
  exact bug class this work exists to remove.
- **One global version plus a manual `--reuse-transcript` flag.** Zero design risk, but it puts the
  invariant in the operator's head. Two silent-degradation bugs in the same review were
  "configuration that looked applied and was not"; this would have been a third by construction.
- **Hashing the glossary file to decide transcription staleness.** Intuitive and wrong: the glossary
  reaches the decoder by exactly one route, `initial_prompt`, while `mine_glossary.py` appends
  `hard_fixes` on every sweep of a watched show. Hashing would re-queue a whole show for the GPU on
  edits that changed nothing about the decoder input. Classification compares the stored prompt
  string instead.

### What it costs

Easier: a glossary or punctuation fix ships for CPU-minutes instead of GPU-hours, which makes
correctness work cheap enough to do. A per-tier staleness count tells you how far behind the part of
the library you are not watching has fallen.

Harder, and paid deliberately: a `words.json` sidecar per episode, roughly 200–300 KB plus segment
records, carrying per-segment `no_speech_prob` and `audio_duration` because neither is recoverable
from the word list. It has to follow the `out_for()` write / raw-path read convention exactly — get
that half-right and the cache misses silently and every episode re-transcribes forever. Two constants
must now be kept honest by hand where one was before, and getting a classification wrong means an
episode is quietly never re-transcribed. The `words_missing` and per-tier counters exist so that
failure is visible within one sweep rather than one month.

A `words.json` records the decoder identity it was produced under — the same `decoder_identity()`
dict the reader compares against, so a sidecar is always reusable by the process that wrote it. The
local backend's four fields (model, initial_prompt, compute_type, beam_size) are frozen: the existing
sidecars were written against exactly those and a new key there would count them all as unknown. The
endpoint backend records backend/model/initial_prompt instead. Writing the *local* model name on
every backend — which is what the first schema-2 sidecars did — made every endpoint sidecar fail its
own reuse check, so each TEXT_VERSION bump re-transcribed on the GPU on precisely the installations
the tier split exists to keep off it.

This does not converge the library to a uniform state. `watch_queue.py` deliberately never queues
unwatched shows, so the honest invariant is that the library converges **ahead of the viewer**, with
the residual readable as a number.

---

## 2. The `no_speech_prob`-gated hallucination rules are deleted, not tuned

**Accepted 2026-08-24.** See also [ASR bakeoff data](ASR-bakeoff-data.md) for the confidence
measurements this rests on.

### The problem

`hallucination.drop_reason` had three rules and `flag_reason` had two. Two of the five were gated on
`no_speech_prob`:

- `music` — `nsp > 0.95` **and** `avg_logprob < -2.0` → drop the card
- `maybe_silence` — `nsp > 0.5` → keep but mark suspect

`music` caught **zero cards across 859 episodes and 353,879 cards**. On 2026-08-21 that was read as a
bug and the threshold was loosened to 0.90 — then reverted the same day, once a labelled set existed
to judge it against:

```
labels: 207 certain hallucinations (blocklist-matching) vs 57,572 real cards, 136 episodes
nsp separates them (0.796 vs 0.330, AUC 0.929); avg_logprob too (0.913)

nsp>0.70 lp<-0.3 -> recall 82.6%, precision 18.5%, 5.54 false drops per episode
nsp>0.80 lp<-0.3 -> recall 54.6%, precision 19.8%, 3.37
nsp>0.90 lp<-2.0 -> recall  2.9%, precision 19.4%, 0.18   <- the loosened setting
nsp>0.95 lp<-2.0 -> recall  0.0%, precision  0.0%, 0.00   <- as shipped
```

Precision peaks near 20%: four of every five drops would be real dialogue, and a caption that never
covers its line is lost content — the worse failure. So an inert rule was judged strictly better than
any reachable setting, and the rule was kept deliberately unreachable with a comment forbidding a
"fix".

Two further facts changed that.

First, `large-v3-turbo` — the model in production — returns `no_speech_prob` of **exactly 0.0**,
median and max, across 263 segments, with 0 of 267 segments carrying a live value in the production
path. Neither rule can evaluate to true on any input the pipeline actually sees.

Second, and decisively: `large-v3` **does not fix this**. Measured on the same episode, same beam,
card to itself, it produces a live nsp distribution (median 0.397, max 0.963, 6 segments over 0.95)
— and `music_rule_would_fire` is still **0** at beams 3, 4 and 7. None of the segments clearing
`nsp > 0.95` also has `avg_logprob < -2.0`; large-v3's worst 5% of segments sit at −0.445 and the
rule wants −2.0. The two conditions do not co-occur on this content.

### The decision

Delete both rules rather than tune them.

The rule is not dead because the decoder collapsed a signal, which a model change could repair. It is
dead because the conjunction it asks for does not occur in this material, and because every reachable
relaxation of it destroys more real dialogue than it saves. Those are permanent properties of the
content and the measurement, not of the model.

Rejected alternatives:

- **Keep them inert, as before.** Right while "the model collapsed nsp" was the explanation, because
  a better model would have revived them. That explanation is now falsified: a model with a
  perfectly live nsp still never fires the rule. What remains is a rule that cannot fire, evaluated
  on every card, forever.
- **Switch to `large-v3` to revive them.** It buys nothing for the gate — the conjunction stays
  unmet — and costs throughput at matched beam with confidence a wash.
- **Loosen the thresholds.** Refused on the same evidence as 2026-08-21. Nothing measured since has
  improved the precision ceiling.

`low_conf` (`avg_logprob < -0.6`), `blocklist` and `repetition` are **kept**: all three fire on real
content.

### What it costs

Easier: five gated rules become three, and every remaining one demonstrably fires. The liveness
counters stop reporting two rules that evaluate on every card and can never activate — noise in
exactly the instrument built to detect dead rules. `drop_reason` no longer reads `no_speech_prob` at
all, so the gate no longer depends on a field the production decoder does not populate.

Harder, and accepted: the pipeline has no music/silence drop defence. In practice it never had one —
the rule caught zero cards in its entire life — so nothing is lost that was ever gained. `BLOCKLIST`
and `is_repetition` remain the live defences, as they already were.

If a future decoder produces an nsp/logprob pair that *does* co-occur, revisit this from the
measurement above rather than by re-adding the rule from memory: the precision ceiling near 20% is
the thing to beat, not the threshold.

Historical `conf.json` sidecars still carry `flag: "maybe_silence"` from past runs, so
`tools/timing_compare.py` keeps its bucket for that key. Deleting the rule does not retroactively
clean the data it produced.

---

## 3. `/api/v1` is a frozen, additive contract beside the UI's backend

**Accepted 2026-11-19.** The code is `dubtitlerr/review/review_api_v1.py` and the static
`dubtitlerr/review/openapi.json`.

### The problem

The review server already had role-based access control, an admin API key in `X-Api-Key`, and audit
logging on every mutating action. What it did not have was a contract. The `/api/*` surface is the
web UI's backend: it moves when the UI moves, its shapes are whatever the current page needs, and
nothing promises stability. The intended consumer — an AI agent driving DubTitlerr — would have been
building on a surface that changes without notice, and there was no machine-readable description of
it at all, so every client would re-derive the routes from source.

### The decision

A versioned namespace, `/api/v1`, is the frozen contract for external consumers. The classic
`/api/*` routes stay exactly as they are; v1 is dispatched from the same router and shares the same
role table and auth path.

- Reads are `user`, writes are `admin`. A v1 path the module does not serve is not public: it falls
  through to the role table's fail-closed default and needs `admin`.
- `X-Api-Key` is an admin credential, and a valid key exempts the request from the browser CSRF
  header, so an agent sends a key and a JSON body.
- Writes are a closed set: `run.request`, `system.restart`, `notification.test`. Supporting
  resources are the ones that already existed — status, health, queue, history, library, settings,
  notification CRUD, API-key rotation state.
- `openapi.json` is a static file served at `/api/v1/openapi.json`, with `x-dubtitlerr-role` on every
  operation. `tests/test_review_api_v1_openapi.py` compares it with the live route table, so the
  document cannot silently describe a surface that no longer exists.
- No v1 route returns a stored secret. A notification reports `{"set": true}`; settings reuse the
  masked read; the key route reports rotation state only.

Rejected alternatives:

- **Freeze the classic `/api/*` surface instead.** Cheap in the short term and wrong in the long
  term: it is the UI's private backend, and freezing it would either stop the UI from evolving or
  break the promise on the first redesign.
- **Add routes to `/api/*` without a version.** Indistinguishable from the UI's backend to any
  consumer, so there is no way to know which routes are safe to depend on.
- **Generate the OpenAPI document from the handlers.** Tempting, and it would remove one hand-kept
  file. It also couples the contract to handler internals, which is the opposite of a frozen shape;
  the consistency test buys the drift protection without that coupling.
- **Include queue removal in v1.** Deliberately left out: it is upstream issue #45 territory ("remove
  a queued request before it starts"), and inventing it here would create a fork-only API surface
  that upstream has no way to honour.

### What it costs

Easier: an agent has one stable, self-describing surface, and the wiki can point at a spec instead of
restating fourteen endpoints in prose. The key that already existed now buys something defined.

Harder, and accepted: two surfaces to keep working. The spec is hand-maintained, and the test guards
route and role drift, not field-level schema drift between an operation and its handler — a renamed
response field is caught by a client, not by CI. `GET /api/v1/history` also has a real first-call cost:
it reads up to two small sidecars per episode on the media mount, once per library walk, capped at
5,000 episodes with `truncated: true` past the cap. And because reads are `user`+, `/api/v1/health`
and `/api/v1/apikey/info` are visible to a plain reviewer, unlike the public `/healthz` and the
admin-only classic `/api/apikey`; the key route returns rotation state only, never the key.

---

## 4. Notifications are a Connect-style provider list, and their failure is not a run failure

**Accepted 2026-11-19.** The code is `dubtitlerr/core/notify.py` for the sender,
`dubtitlerr/settings/settings_notifications.py` for the validation, and the emitters at the pass
boundaries in `gen_loop.sh`, `merge_pass.sh` and `container_run.sh`.

### The problem

Nothing in the pipeline pushed events anywhere. The run was only observable by reading logs or
reloading the review UI, which is the wrong shape for an unattended overnight builder: a failed merge
pass and a finished sweep looked the same until someone looked.

The naive fix — one environment variable per destination, or a single "post to this URL" hook —
multiplies configuration paths and loses the thing the UI already does well. Connections had already
established the pattern for a user-defined list with write-only secrets, per-item validation, a Test
button, and URL safety guards. A second, bespoke mechanism for outbound destinations would be a second
set of rules to keep honest.

### The decision

A notification is a provider: `{name, kind, enabled, triggers[]}` plus its kind's fields, stored with
the same settings machinery as Connections (`settings.json` for visible fields, `secrets.json` mode
0600 for write-only ones). Kinds are a registry — `discord`, `ntfy`, `webhook` today, so another kind
is an addition, not a rewrite. Each provider chooses its own triggers from a fixed event set:
`run.requested`, `sweep.completed`, `merge.completed`, `pass.failed`, `review.pending`,
`episode.delivered` (off by default in the panel, because it is one message per episode) and
`episode.refused` (a parked episode, one message per distinct refusal). Both per-episode events are
opt-in: a provider sends only the triggers its own list names, so an install that wants to hear
about a parked episode ticks `episode.refused` in the panel.

Delivery is deliberately boring and bounded: one `POST` per provider per event through `urllib`, one
10-second timeout, one retry after a timeout, a network error or a 5xx, never a retry after a 4xx.
Every failure is logged as one line naming the provider, the event and the outcome, and swallowed.
The loops call the sender with `|| true`; the review server emits in-process *after* a request is
already written. A notification can fail; a run cannot fail because a notification failed.

The sender is a stdlib-only leaf, like `atomic_file.py` and `glossary_lock.py`, because the shell
loops import it from a minimal app directory that does not ship `pysubs2`. For the same reason its
URL guard is a duplicate of `settings_connections.host_problem` rather than an import.

Rejected alternatives:

- **A single generic webhook.** One URL, one JSON body, no per-destination formatting. It is less
  code, and it makes the common case (a phone notification) somebody else's problem.
- **A persistent delivery queue with retries.** It turns notifications into pipeline state: a
  destination that is down would produce a backlog to reason about, and a bug there could stall a
  pass. The event is a status line, not a ledger; one retry and a log line is the right weight.
- **Fail the pass when a configured provider cannot receive.** The notification is a report about
  work that already happened. Discarding that work because the report bounced is the worse failure.
- **A `version` field in the envelope.** The repo exposes no runtime application version — pyproject's
  number is packaging metadata — so any value there would be a claim a consumer could not check.

### What it costs

Easier: one place to add a destination, one validation path shared with Settings (including the rule
that changing a kind or an ntfy server drops the stored secret unless a new one arrives in the same
save), and a Test button that exercises exactly the code path a real event uses.

Harder, and accepted: the 10-second timeout is one number, because `urllib` exposes a single timeout
covering connect and reads — connect cannot be bounded separately from reads. A delivery failure is
visible only as a log line; there is no inbox of failed notifications to replay. The secret-never-logged
rule depends on the log line staying outcome-only, which is why the renderer catches every exception
and records its type, never its text.

---

## 5. The watch queue is the union of three incomplete sources, read under a tri-state rule

**Accepted 2026-08-21 by measurement; Jellyfin added 2026-11-19.** The code is
`dubtitlerr/pipeline/watch_queue.py`.

### The problem

The queue's only question is *is this show live?*, and it originally answered it from WatchState plus
Plex as though the two were interchangeable views of the same fact. The 2026-08-21 measurement showed
they are not, and that each one alone returns a confident, correctly-sorted, **incomplete** list:

- **WatchState only** left One Pace 40.0 days stale. Playback was on Jellyfin, and WatchState syncs
  the watched flag into Plex without bumping Plex's show-level `lastViewedAt`.
- **Plex only** missed shows that WatchState was not configured for: SPY x FAMILY, Serial Experiments
  Lain, Mashle and SK8 the Infinity. WatchState covered one Plex account, so other Plex users were
  invisible to it.

Neither source is a superset, so "prefer the better one" has no correct answer. The second problem is
an outage: a source that cannot be reached also returns nothing, and a queue rebuilt from "nothing
watched" silently narrows the sweep.

### The decision

The queue is the **union** of independent sources, and each source is read under a tri-state rule:
reachable-with-data, reachable-empty, unreachable. An empty answer and an unreachable source are
different facts, and only one of them is evidence.

- If a **configured** source cannot be read, the order file is left alone. A stale queue is safe; a
  queue narrowed by an outage is not.
- Jellyfin became a third direct source — the server both WatchState and Plex are downstream of. Its
  rows are additive: with neither `JELLYFIN_URL` nor `JELLYFIN_API_KEY` set it is simply not a source,
  so every existing deployment keeps working unconfigured. A half-configured Jellyfin is a mistake,
  not an absent source, and refuses.
- Sources are matched to folders by TVDB id first (`parent.guid_tvdb`, the Plex `tvdb://` Guid,
  Jellyfin's `ProviderIds.Tvdb`) against `{tvdb-N}` in the folder name, and only then by title, with a
  shared title reported as unmatched rather than guessed.
- Pagination walks **every page**. A partial first page is not an answer; Jellyfin hitting its page
  backstop raises `Unreachable` instead of returning a slice.
- The report line names all three sources, so a source that is absent is visible rather than implied.

Rejected alternatives:

- **Treat an unreachable source as empty.** This is the silent-narrowing bug in one line, and it is
  the failure the tri-state rule exists to prevent.
- **Make Jellyfin required like WatchState and Plex.** That would refuse every existing deployment
  until it configured a server, for a source that is additive.
- **Pick one authoritative source.** Falsified by the two measurements above: whichever one is
  chosen, the other holds shows it cannot see.
- **Match by title alone.** A title two folders share (`JoJo's Bizarre Adventure`, 1993 and 2012) has
  no correct answer, so it is reported unfound and the id settles it.

### What it costs

Easier: the queue no longer depends on what a sync layer managed to copy, and an outage degrades into
a stale-but-safe queue instead of a quietly shorter one. The three-source line in the report makes the
difference visible on every run.

Harder, and accepted: three network dependencies sit on the generate path, and each must be reachable
or the write is refused. The Jellyfin entry carries a debt the other two do not: **its justification
is a reason, not a measurement.** No live comparison of Jellyfin against the WatchState ∪ Plex union
has been recorded yet, and two behaviours — whether `Filters=IsPlayed,IsResumable` is applied as OR by
the deployed server, and whether `GET /Users` succeeds with the configured API key — are confirmed
only against mocked HTTP. `TODO(deploy)` in the module docstring records exactly what the deploy phase
must measure before this section can cite a number.

---

## 6. Role-aware gate semantics for dub-caption references

**Accepted 2026-11-19.** The switch is `REPAIR_GATE_DUB_VOUCHED` and the cap is
`REPAIR_GATE_DUB_VOUCHED_MAX_RATIO`, both in `repair.py`. The acceptance evidence is
`tools/gate_replay.py`.

### The problem

Every repair guard was built on one assumption: the reference is **a different translation of
this moment**. `ref_borrow` counts the words a proposal takes from it and refuses three or more,
because a model that copies the reference is writing the fansub's line, not the dub's. That is
correct when the track paraphrases the scene.

It is backwards when the track **transcribes the dub**. Once repair preferred native dub captions
over translations, the guards began refusing repairs that were exactly right: the words they
counted as leakage were the dub's own words.

Three misfires were verified on DAN DA DAN, each refused by a guard the dub captions should have
satisfied:

| Case                                          | Guard that refused it | What made it correct                                  |
| --------------------------------------------- | --------------------- | ----------------------------------------------------- |
| A line the model lengthened from 8 to 21 characters | `length_ratio` (ratio 2.6, above the 1.5 band) | Every added word appears in the dub captions |
| A contraction the model restored               | `invents_name`        | The word appears in the dub captions                   |
| A character's full name the model spelled out  | `invents_name`        | The gained name core appears in the dub captions       |

None of the three is a fabrication. All three were refused by a rule that could not tell a
transcript from a translation.

### The decision

For `ref_role='dub'` only, a proposal whose **every added word** appears in that card's
dub-caption window is **all-vouched**:

- `ref_borrow` is replaced by the positive signal — the added words are attestation, not leakage,
  so the borrowing count is not applied;
- the upper `length_ratio` band becomes `REPAIR_GATE_DUB_VOUCHED_MAX_RATIO` (`3.0`) instead of
  `LEN_RATIO_MAX` (`1.5`); the lower bound stays `LEN_RATIO_MIN`;
- a gained proper-noun core that appears in the dub window is exempt from `invents_name`.

Every other guard is unchanged and still runs: `empty`, `unchanged`, `terminal_punct`,
`name_part`, `vouched_name`, `fits_card`. If any added word is **absent** from the dub window the
proposal falls back to the classic gates exactly. `translation` and an unrecorded role keep the
classic gates, so every show measured before this decision behaves as it did.
`REPAIR_GATE_DUB_VOUCHED=0` restores classic semantics everywhere.

Rejected alternatives:

- **Trust a dub-caption reference outright.** It would admit any length and any added name, which
  removes the only mechanical check between a correct borrow and a hallucinated continuation. The
  all-vouched rule keeps the requirement that the *specific* added words be attested.
- **Leave the gates alone and let a human `force` each case.** This is what the queue already
  does, and it is why the three cases were visible at all. But it puts a whole class of correct
  repair into permanent human review, on every dub-caption show, forever.
- **Widen the length band globally.** It would relax translation-role cards too, where the
  measured 84.1% reference-import rate says the band is doing real work.

### The evidence

The acceptance replay re-judged proposals the live repair stage had **already refused**, using
only the stored queue entries and their recorded reference windows — no model, no pipeline run,
nothing written. On the live library's queue:

| Show                  | Cards re-judged | Newly admissible | Regressions |
| --------------------- | --------------- | ---------------- | ----------- |
| DAN DA DAN (dub role) | 40              | 10 (8 `invents_name`, 2 `length_ratio`) | n/a |
| Chainsaw Man (translation role) | 142   | 0                | 0           |
| Frieren (translation role)      | 418   | 0                | 0           |

A translation-role regression — any card whose verdict the role-aware path changed — would have
failed the replay. None occurred. The dub-role gain is confined to the show with native dub
captions, which is the point.

Two limits on that evidence, stated rather than smoothed over:

- **Two of the three cases are admitted end to end; the third is not.** The full-name case now
  passes the name guard that was refusing it, but its real card is 1.34 s and the corrected line
  is 20.1 characters per second against a 17 cps limit, so `fits_card` still refuses it.
  `fits_card` was deliberately left unchanged: card timing comes from the audio, and a repair that
  does not fit is a different problem from a repair the reference misjudged. The acceptance target
  for this change named all three; the full gate path admits two of them.
- The replay ran the name guards with an **empty glossary**, because the deployed glossary volume
  was not reachable from the replay host. An empty glossary makes `invents_name` stricter, so the
  refusal counts are, if anything, pessimistic; the regression count is unaffected because both
  sides of that comparison saw the same empty glossary.

### What it costs

Easier: a correct borrow from dub captions is no longer refused for being a borrow, and a
correction that legitimately lengthens a line has room. Ten live refusals on one show became
admissible, with no measurable change anywhere else.

Harder, accepted:

- The dub-role path is **more permissive** than the classic one. It leans on the human review
  stage for the plausibly-wrong, exactly as the classic gates do, and it leans slightly less on
  the mechanical checks. The all-vouched requirement is what keeps that bounded.
- `fits_card` still binds. A correct repair can be refused for want of room, and the third
  verified case shows it. Splitting a card to make room is still out of scope.
- There are now **two rollback switches** — `REPAIR_REF_CLASSIC` and `REPAIR_GATE_DUB_VOUCHED` —
  and neither is a stored setting, so turning one off is a container environment change rather
  than a UI toggle. That is deliberate: they exist for A/B replays and emergencies, not tuning.
- The evidence is one show with a native dub-caption track plus two regression controls. A library
  where every show has dub captions would exercise this path far more; re-run `gate_replay.py`
  there before assuming the ratio cap is generous enough.

---

## 7. Processing is an explicit persisted state, and boot defaults to paused

### The problem

The container's entrypoint did three things at once: start the generate sweep, run the first
merge pass, and start the review server. There was no "up but idle" state. Anyone who started the
container to look at the review queue, or to run an operator task, immediately spent GPU time and
repair calls on the library. The soft stop was the only brake, and it ended the whole container
including the UI. A flag on disk was deleted at boot, so the container could not remember that a
human had asked it to stay idle.

### The decision

Processing has three explicit states — `running`, `paused`, `stopped` — persisted in one tiny
atomic JSON file under the config volume with the actor and time. The boot path materialises
`paused` when the file is missing, before any loop starts, and every loop honors it at its
existing boundary. `paused` and `stopped` both leave the review server up and start no sweep or
merge pass.

- The loops do not get a second stop mechanism. Pause reuses the same boundary checks the soft
  stop already had (gen: between shows/sweeps; merge: between stems; glossary verify: between
  terms) and holds there instead of exiting. A stop that arrives during a hold returns the same
  drain path as today.
- Stop is still the soft stop: persist `stopped`, write `STOP_FLAG`, finish the unit, exit. The
  restart policy brings the UI back to the persisted `stopped`, which holds until Start.
  `docker stop` is a container operation and deliberately writes no state.
- Start, Pause and Stop are one implementation behind the Settings panel, the classic routes and
  the `/api/v1` commands, so the three surfaces cannot drift. A host-side CLI writes the same
  file with the container down, for the deploy flow's "set paused before recreate".
- A corrupt or unknown file reads as `paused`, not `running`: the safe direction is to leave the
  library alone. A missing file is paused for the app; a stage invoked directly outside the
  container with no file keeps running, because there is no app boot there to honor.
- `system.start` / `system.pause` / `system.stop` are additive `/api/v1` commands. `system.restart`
  remains what it was; nothing is renamed.

Rejected alternatives:

- **A boot toggle on the container** (`AUTOSTART=0`). It cannot be changed at runtime, does not
  survive a recreate the way a config-volume file does, and gives no place to record who asked.
- **Pause by deleting/keeping `STOP_FLAG`.** The flag means "drain and exit"; overloading it would
  make pause end the container, which is the exact behaviour this decision removes.
- **Stop the container to stop work.** That was the only option before, and it takes the UI and
  API down with the GPU work.
- **Make `stopped` mean "exit and stay down".** A container that stays down cannot show its UI or
  accept Start from an operator. It drains, exits, and the restart policy returns it idle.

### What it costs

Easier: an operator can start the app, browse the queue, change settings or answer review items
without spending a single repair token; and a deploy can recreate the container without a race
against the first sweep.

Harder, and accepted: a pause is honored at the next boundary, not immediately inside a show or
stem, so a long show keeps one unit of GPU work in flight (the same contract as the soft stop).
The state is one more small file to keep writable in the config volume; when it is not, the API
reports a 500 and the app keeps the previous behavior instead of silently processing.

## 8. Boilerplate/credit removal is whole-cue and position-gated

Whisper writes text nobody spoke: platform boilerplate ("Thanks for watching!"), release credits
("Subtitles by the Amara.org community", "Original Script: Kitsune-HentaiBot [...]") and
disclaimer blocks. The four repetition arms cannot see it, because the text is unique and nothing
loops. Afro Samurai S01E01 shipped four such cues before the first spoken line. Arm #5
(`dubtitlerr/pipeline/boilerplate.py`) removes them.

Two properties are the decision, not the implementation detail:

- **Whole-cue match.** A cue is boilerplate only when its entire text equals a template, edge
  punctuation aside. The delivered library contains a trailing cue "Thanks for watching him."
  (Demon Slayer S04E01 cue 292, real dialogue) and mid-scene "Others support us silently."
  (FMA:B S01E18 cue 108). A substring filter deletes the first and flags the second; an anchored
  one touches neither. The same anchoring was applied to the blocklist's
  "thanks for watching"/"thank you for watching"/"thanks for your support" alternatives, which
  were bare substrings and would have deleted that Demon Slayer line on the next regeneration.
- **Leading/trailing runs only.** Only the run before the first non-matching cue and the run after
  the last are dropped. A mid-file match is counted (`boilerplate_midfile`) and audited, never
  dropped: Vinland Saga S01E13 carries an Amara credit at cue 4 of 400, and removing credits from
  the middle of an episode is a mux-grade edit a filter should not make on a template's say-so.
  A run longer than `MAX_RUN` (six) is refused for the same reason -- that shape is a loop, which
  the repetition arms own, or a template that has drifted too broad.

The template list lives once, in the module, and `tools/artifact_sweep.py` imports it, so the
offline sweep for already-delivered sidecars and the pipeline cannot disagree about what
boilerplate is. A VAD speech check was considered and skipped: it is not wired into the
text-tier stages, and whole-cue matching on unique phrases already keeps precision high.

### What it costs

Harder, and accepted: a boilerplate variant not in the template list survives (conservative by
construction), and a credit rendered mid-episode is reported but left for an operator. Both are
cheaper than deleting a line of dialogue, which no counter would ever reveal.
