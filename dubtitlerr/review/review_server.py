#!/usr/bin/env python3
"""[S-7] The review server: rule on the repairs `accept_repair` admitted.

`accept_repair` states its acceptance bar in its own docstring and then says plainly that
nothing below it enforces that -- `factory -> needle` and `VIVRA card -> Vivi card` pass
every gate. An accepted repair is therefore a decision no code has checked, and this is
where the one reviewer who can check it does so, without the verdict having to travel back
through a markdown diff.

Stdlib http.server, no new dependency. Every route is a thin call into decisions.py,
unresolved.py and review_apply.py; nothing durable is decided here.

SECURITY. `container_run.sh` runs as root so `generate.py` can chown into the media tree.
[S-8] puts this server in that same process tree, and its write routes rewrite subtitles and
force re-muxes. A downstream user on host networking would therefore be exposing an
unauthenticated root-owned endpoint, so:

  REVIEW_TOKEN unset       -> a token is GENERATED, persisted 0600, and printed once.
  REVIEW_TOKEN= (empty)    -> treated exactly like unset: a token is still generated.
  REVIEW_TOKEN=<value>     -> that token, and it wins over any persisted one.
  REVIEW_AUTH=off          -> the ONLY way to disable auth. It is the operator's decision
                              about their own network, independent of REVIEW_TOKEN.

Auth is gated on REVIEW_AUTH, never on REVIEW_TOKEN's value: a blank token left by
accident (a stray REVIEW_TOKEN= line, or an unset Compose variable rendered empty) must
not silently open a root-owned write endpoint. Read routes are never gated
-- they expose only what is already on the operator's disk -- and every write route is.

Episode identity NEVER comes from the client. A stem is accepted only if it appears in the
set this process discovered by walking MERGE_ROOTS; anything else is refused. The stems are
file paths, so trusting one from a request would be a path traversal into any file this
root process can read or overwrite.

Env:
  REVIEW_PORT    default 8842
  REVIEW_TOKEN   see above
  DECISIONS_DIR  default /config/decisions   (the token lives beside it)
  MERGE_ROOTS    default /data/Media/Anime Library  (colon list; the only stems accepted)
  REVIEW_USERNAME, REVIEW_PASSWORD, REVIEW_PASSWORD_FILE
                 imported once as the first admin account when no account exists yet
                 (review_auth.import_env_admin); not read again afterwards.
"""

import datetime
import hashlib
import http.cookies
import http.server
import json
import math
import os
import re
import secrets
import string
import subprocess
import threading
import time
from pathlib import Path

from dubtitlerr.core import artifact_store, common, decisions, media_select, unresolved
from dubtitlerr.core.common import log
from dubtitlerr.core.decisions import show_for
from dubtitlerr.review import (
    review_api_v1,
    review_apply,
    review_audit,
    review_auth,
    review_glossary,
    review_requests,
    review_status,
    review_webhooks,
)
from dubtitlerr.settings import settings_api

review_auth.register_routes(settings_api.ROLE_ENTRIES)

REVIEW_PORT = int(os.environ.get("REVIEW_PORT", "8842"))
UI_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"
_AUDIO_SLOTS = threading.BoundedSemaphore(2)
AUDIO_MAX_SECONDS = 20


# Beside DECISIONS_DIR rather than inside it: that directory is the artifact a user may
# later publish or sync, and a credential must not ride along with it.
def token_dir_for(decisions_dir: str) -> str:
    """Where the token lives: beside DECISIONS_DIR, never inside it and never at /.

    Beside, because DECISIONS_DIR is the artifact a user may later publish or sync, and a
    credential must not ride along with it. Never at the filesystem root, because
    os.path.dirname("/decisions") is "/" -- truthy, so an `or "/config"` fallback silently
    does not fire and the credential is written to /review_token."""
    parent = os.path.dirname((decisions_dir or "").rstrip("/"))
    return parent if parent not in ("", "/") else "/config"


TOKEN_DIR = token_dir_for(os.environ.get("DECISIONS_DIR", "/config/decisions"))
ROOTS = os.environ.get("MERGE_ROOTS", "/data/Media/Anime Library").split(":")
# Read routes are ungated by design, and each one resolved a stem by walking the whole media
# root. On a 20,000-episode library over CIFS that is a full recursive walk per
# unauthenticated request. Cached with a SHORT ttl rather than forever, so an episode
# generated mid-session still appears without restarting the container.
STEMS_TTL = float(os.environ.get("REVIEW_STEMS_TTL", "30"))
# ...but a TTL that expires faster than the walk it is hiding is not a cache at all. Measured
# 2026-08-28: 297s over 989 episodes against a 30s TTL, so every request re-walked the whole
# tree and the cache had never once been hit. The cost is a property of someone else's
# filesystem, so the floor stays configurable and the ceiling is derived from what the walk
# actually took. A list this expensive to rebuild is allowed to be an hour stale; a fresh
# episode still appears within that, and immediately on restart.
STEMS_TTL_FACTOR = float(os.environ.get("REVIEW_STEMS_TTL_FACTOR", "20"))
_STEMS_CACHE = None  # (expiry, stems, filled_at): filled_at is the walk START, time.monotonic(), for /api/status's cache_age_s
# One episode's open queue, filled by the same pass that discovers the episodes and dropped
# when that pass is redone. The index reads every episode's queue artifact AND its conf.json --
# ~200s across the live library, on top of the walk.
#
# NOT validated per request. The first version stat-ed both files each time to check
# freshness and that measured 176s warm: 989 episodes x 2 stats x ~90ms, almost exactly the
# read it was avoiding. On a mount like this ANY per-episode touch costs the same, so
# validation cannot be cheaper than the thing validated. The cache is therefore tied to the
# walk's lifetime, and a WRITE drops the one stem it wrote -- the writer knows which, and
# dropping everything would make each save cost a full walk exactly when the reviewer is
# working.
#
# Deliberately holds the entries BEFORE the decisions store is consulted. The store is one
# small file read per request, so a verdict shows up everywhere immediately.
_QUEUE_CACHE: dict = {}
# stem -> queue artifact updated_at as ISO 8601 UTC (None when unreadable). Filled and dropped with _QUEUE_CACHE: a stat
# on every index call is exactly the touch that cache exists to avoid.
_QUEUE_MTIME: dict = {}
# A miss sentinel for the two readers below, and it has to be an object rather than None:
# ``_QUEUE_MTIME`` stores None for an unreadable sidecar, so ``.get(stem)`` returning None
# cannot tell a cached "unreadable" from "not cached yet". Reading through a single ``.get``
# also closes the window the old check-then-subscript left open -- ``known_stems()`` clears
# both dicts, and this server is a ThreadingHTTPServer, so one request could clear the cache
# between another request's membership test and its subscript (KeyError, surfacing as a 500).
_CACHE_MISS = object()
CONF_SUFFIX = ".dubtitles.conf.json"


# 0.0.0.0 because the container is the point -- the operator reaches this from their LAN.
# That is exactly why an unset REVIEW_TOKEN generates one instead of meaning "open".
REVIEW_BIND = os.environ.get("REVIEW_BIND", "0.0.0.0")
# [F-4] How many requests may be in flight. The Handler.timeout below bounds how LONG one
# unauthenticated request can hold a worker; this bounds HOW MANY. ThreadingHTTPServer
# spawns a daemon thread per connection with no cap of its own, so without this a LAN client
# opens many at once and each dies after the deadline rather than never -- an indefinite pin
# becomes a sustained churn. A review is one person clicking buttons; 16 is generous.
MAX_CONCURRENT = int(os.environ.get("REVIEW_MAX_CONCURRENT", "16"))
# The token minted this process. review_auth.key_matches() calls resolve_token() per request, so without
# this a persistence failure would mint a FRESH random token every time -- none of them the
# one printed at startup -- and every write would 401 forever, including for the operator
# holding the logged token. Failing closed is right; failing closed against yourself is not.
_GENERATED = None

# Which verdicts the reviewer may give, per queue entry. Not cosmetic: `accept` on an entry
# the gate REFUSED would be a `force` with no distinct record, and counting forces is the
# only way anyone will later learn how often the gate was wrong.
OFFERED = {
    ("repair_applied", "accepted"): ("accept", "reject", "correct"),
    ("repair", "rejected_guard"): ("force", "reject", "correct"),
    ("repair", "rejected_name_invented"): ("force", "reject", "correct"),
    ("repair", "rejected_name_part"): ("force", "reject", "correct"),
    ("repair", "decision_unfittable"): ("correct", "reject"),
    # An approval the model orphaned by proposing different wording for the same line
    # (usually a REPAIR_MODEL change). The reviewer is being asked to rule again, on the
    # NEW proposal, so this is the same choice they had the first time -- `force` included,
    # because the new wording may trip a gate the old wording did not.
    ("repair", "verdict_stale_proposal"): ("accept", "force", "reject", "correct"),
}
DEFAULT_OFFERED = ("reject", "correct")


def auth_required(token_dir: str = "") -> bool:
    """False ONLY when REVIEW_AUTH=off. An empty REVIEW_TOKEN is treated as unset."""
    return os.environ.get("REVIEW_AUTH", "") != "off"


def resolve_token(token_dir: str = "") -> str:
    """The token: the env var, else the persisted one, else a fresh one written 0600.

    Persisted so a restart does not lock the operator out of their own queue, and 0600
    because the media tree this process writes to is deliberately group-writable."""
    global _GENERATED
    d = token_dir or TOKEN_DIR
    env = os.environ.get("REVIEW_TOKEN")
    if env:
        return env
    path = os.path.join(d, "review_token")
    try:
        existing = open(path, encoding="utf-8").read().strip()  # noqa: SIM115
        if existing:
            return existing
    except OSError:
        # No token yet: fall through and generate one.
        pass
    if _GENERATED:
        return _GENERATED  # persistence failed earlier; keep the one already printed
    tok = secrets.token_urlsafe(32)
    _GENERATED = tok
    try:
        os.makedirs(d, exist_ok=True)
        # 0600 from the moment it exists, through common.atomic_write -- the atomic idiom
        # the other state writers share. A direct O_TRUNC write
        # leaves a window where the file exists and is empty, and a reader in that window
        # regenerates a second token that only one of the two writers will keep.
        common.atomic_write(path, lambda f: f.write(tok + "\n"), mode=0o600)
        log(f"review server: generated an access token, stored {path} (0600)")
        log(f"review server: token = {tok}")  # printed ONCE, at startup, to the operator's own log
    except OSError as e:
        log(f"review server: could not persist a token ({e}); it holds for this process only")
        log(f"review server: token = {tok}")
    return tok


review_auth.set_token_fallback(lambda: resolve_token())  # until a key file exists, the 0.2.x review token IS the API key


def announce_token(token_dir: str = "", bind: str = "") -> None:
    """Say WHERE the token is on every start; say what it IS only when it is new.

    resolve_token prints the value at the moment it generates one, and never again -- so an
    operator returning after the container log had rotated had no way to find it short of
    knowing the docker exec incantation. A path and a command are not a credential.

    Also warns, loudly, on the one combination the module docstring calls "the operator's
    decision about their own network" but the README never mentions: REVIEW_AUTH=off on
    the default 0.0.0.0 bind. An empty REVIEW_TOKEN is no longer enough to trigger this --
    only REVIEW_AUTH=off does -- so an operator who finds it in a forum thread (the natural
    answer to "my token never arrived, I restarted before I saw it") should still see the
    risk in their own logs, not only in source they may never read."""
    d = token_dir or TOKEN_DIR
    if not auth_required(d) and (bind or REVIEW_BIND) == "0.0.0.0":
        log(
            "review server: WARNING — REVIEW_AUTH=off (auth disabled) and REVIEW_BIND is "
            "0.0.0.0: every write route is open to anything that can reach this port. "
            "Unset REVIEW_AUTH, or set REVIEW_BIND to a host-only address, unless this "
            "network is one you fully trust."
        )
    if "REVIEW_TOKEN" in os.environ:
        log("review server: using REVIEW_TOKEN from the environment")
        return
    path = os.path.join(d, "review_token")
    log(f"review server: token file {path} (0600, root) — read it with:")
    log("review server:   docker exec <container> cat " + path)


# A request target after whitespace -- origin-form (/path) or absolute-form (scheme://host/path) -- and the query that
# follows it, whatever the method token (PROPFIND, lowercase get, M-SEARCH...).
_REQUEST_LINE_QUERY = re.compile(r'(\s(?:[A-Za-z][A-Za-z0-9+.-]*://[^\s"?/]*)?/[^\s"?]*)\?[^\s"]*')
# Write routes added with the webui whose body must be JSON: garbage is a 400 there. The inbox routes keep their old
# lenient reading (unparseable becomes an empty body).
_JSON_REQUIRED = frozenset(
    (
        ("POST", "/api/requests"),
        ("PUT", "/api/schedule"),
        ("PUT", "/api/settings"),
        ("POST", "/api/settings/test"),
        ("POST", "/api/settings/reset"),
        ("POST", "/api/glossary"),
        ("POST", "/api/glossary/wiki-test"),
        ("POST", "/api/users"),
        ("PUT", "/api/me/password"),
    )
)
# (method, path) -> handler(body) for routes the role table marks admin; route_full has already checked the role. Tests
# register a probe here.
ADMIN_ROUTES: dict = {}


def _anime_root():
    """ANIME_ROOT from the environment at call time (None lets review_requests apply its own default)."""
    return os.environ.get("ANIME_ROOT") or None


# Builder-affecting writes (admin role, enforced by route_full before these run).
ADMIN_ROUTES[("POST", "/api/requests")] = lambda body: review_requests.handle(
    "POST", "/api/requests", body, anime_root=_anime_root()
)
ADMIN_ROUTES[("POST", "/api/glossary/wiki-test")] = review_glossary.wiki_test
ADMIN_ROUTES[("PUT", "/api/schedule")] = lambda body: review_requests.handle(
    "PUT", "/api/schedule", body, anime_root=_anime_root()
)


def _stems_ttl(elapsed: float) -> float:
    """How long the stem list stays valid, given what discovering it cost."""
    return max(STEMS_TTL, elapsed * STEMS_TTL_FACTOR)


def open_entries(stem: str) -> list:
    """This episode's primary, still-live queue entries. Read once per walk, then held.

    NOT filtered by the decisions store. That file is small, is read once per request, and
    changes on every save -- so filtering stays live and a verdict is reflected everywhere
    the moment it lands. Callers apply unresolved.undecided themselves."""
    cached = _QUEUE_CACHE.get(stem, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached
    entries = unresolved.live_only(stem, unresolved.pending(stem, primary_only=True))
    _QUEUE_CACHE[stem] = entries
    return entries


def forget(stem: str) -> None:
    """Drop one episode's cached queue, after writing to it."""
    _QUEUE_CACHE.pop(stem, None)
    _QUEUE_MTIME.pop(stem, None)


def known_stems() -> list:
    """Every episode stem this process can see, discovered by walking MERGE_ROOTS.

    The allow-list for every route. A stem is a FILE PATH, so honouring one from a request
    would let any caller read or overwrite anything this root process can reach."""
    global _STEMS_CACHE
    now = time.monotonic()
    # An EXPIRY, not a stamp: how long this list stays good depends on what it cost, and the
    # caller cannot know that before the walk.
    if _STEMS_CACHE and now < _STEMS_CACHE[0]:
        return _STEMS_CACHE[1]
    out = []
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        real_root = os.path.realpath(root)
        # The conf is a store row now (P39), so the episodes come from the store rather than
        # a walk. The containment check still applies: a stem is a FILE PATH and this list
        # is the allow-list every route trusts. os.walk used to list symlinked FILES in a
        # group-writable tree; a store key is no different, so a stem that realpath-resolves
        # outside the root is dropped here too.
        for stem in artifact_store.stems(root, kind="conf"):
            if not os.path.realpath(stem).startswith(real_root + os.sep):
                continue
            out.append(stem)
    stems = sorted(out)
    # Filled together, dropped together. Nothing else expires a queue entry, so a re-repaired
    # episode would otherwise keep its old queue until the container restarted.
    _QUEUE_CACHE.clear()
    _QUEUE_MTIME.clear()
    # One tuple, one assignment: a reader never sees a list paired with another fill's timestamp. ``now`` is the
    # walk START -- the list describes the tree as the walk saw it from then on, not as of when it finished.
    _STEMS_CACHE = (time.monotonic() + _stems_ttl(time.monotonic() - now), stems, now)
    return stems


def _resolve(stem: str):
    """The stem, only if it is one we already knew about. Never trust the client's path."""
    return stem if stem in known_stems() else None


def _store_for(stem: str, cache: dict) -> dict:
    """This episode's show store, loaded once per show per request.

    The index walks the whole library -- 294 episodes on the live one -- and a store load
    per episode would be 294 reads of a handful of files. Read at CALL time, not bound as a
    default, for decisions.load's reason: DECISIONS_DIR is captured in its signature at
    import, so the mount cannot be redirected after this module loads unless passed through."""
    show = show_for(stem)
    if show not in cache:
        cache[show] = decisions.load(show, decisions.DECISIONS_DIR) if show else {}
    return cache[show]


def queue_written_at(stem: str):
    """When this episode's queue was last written in the store, ISO 8601 UTC, or None: the fallback for entries that carry no
    ts. Cached per walk, like the entries it describes."""
    cached = _QUEUE_MTIME.get(stem, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached
    try:
        updated_at = artifact_store.mtime(stem, unresolved.KIND)
        when = datetime.datetime.fromtimestamp(updated_at, datetime.timezone.utc) if updated_at else None
        value = when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None
    except Exception:
        value = None
    _QUEUE_MTIME[stem] = value
    return value


def handle_index() -> dict:
    """Every episode with something pending, split by what kind of question it is.

    ADMITTED and REFUSED are not the same job and one number for both misleads. An admitted
    repair is a change nothing checked the meaning of -- it SHIPPED, and `factory -> needle`
    passes every gate; that is the reason this whole loop exists. A refusal means the ASR
    text shipped, which is the safe outcome, and reviewing it asks the audit question of
    whether the guard was too strict. Measured on the live library 2026-08-27: 8,662 pending
    items, every one of them a refusal and none an admitted repair -- a single count read as
    thousands of the urgent kind."""
    out, stores = [], {}
    for stem in known_stems():
        live = open_entries(stem)
        # A verdict settles the LINE, show-wide, not the one queue row that raised it. The
        # opening song is 24 episodes of the same question; without this the count here
        # keeps promising work that the episode page no longer has.
        live = unresolved.undecided(live, _store_for(stem, stores))
        if not live:
            continue
        admitted = sum(1 for e in live if e.get("stage") == "repair_applied")
        season = os.path.basename(os.path.dirname(stem))
        # Entries written before they were stamped have no ts; the sidecar's mtime stands in only when none of these has one.
        stamps = [e["ts"] for e in live if isinstance(e.get("ts"), str) and e["ts"]]
        oldest_at = min(stamps) if stamps else queue_written_at(stem)
        out.append(
            {
                "stem": stem,
                "name": os.path.basename(stem),
                # Path-derived for DISPLAY only. decisions.show_for is the identity the store
                # and the gate key on, but it needs a glossary to resolve and returns "" when
                # there is none -- a page that silently dropped those episodes would be worse
                # than one whose grouping label is occasionally a directory name.
                "season": season,
                "show": os.path.basename(os.path.dirname(os.path.dirname(stem))),
                "pending": len(live),
                "admitted": admitted,
                "refused": len(live) - admitted,
                "oldest_pending_at": oldest_at,
            }
        )
    # Most admitted first. With everything at 0 the flat list had no meaningful order at all,
    # so the episode that gains an accepted repair has to surface without being hunted for.
    out.sort(key=lambda e: (-e["admitted"], -e["refused"], e["name"]))
    return {"episodes": out}


# Leading/trailing only. `don't` and `it's` are ONE word each; stripping punctuation inside
# them would split most contractions and file them as word changes -- the exact 78% this
# ordering exists to push down the page.
_TRIM = string.punctuation + "\u2014\u2013\u2026\u201c\u201d\u00ab\u00bb\u00a1\u00bf"


def _words(text: str) -> list:
    """The word sequence, with punctuation and case discarded.

    The apostrophe fold is decisions.key's, for decisions.key's reason: U+2019 and U+0027 are
    two renderings of one character and English dub dialogue is mostly contractions."""
    folded = (text or "").replace(chr(0x2019), chr(0x27)).lower()
    # A DASH separates, a HYPHEN does not. This stage's commonest single act is turning a
    # run-on into sentences, and `it--that's` becoming `it. That's` adds a token on one side
    # only; filed as a word change, pure punctuation would sort to the top of the queue. The
    # hyphen is left alone because Flame-Flame, non-stop and CP-0 are one word each.
    for sep in ("\u2014", "\u2013", "\u2026"):
        folded = folded.replace(sep, " ")
    return [w for w in (t.strip(_TRIM) for t in folded.split()) if w]


# Worst first. `words` is where the flame-flame deletion lives -- ratio 0.88 inside a 0.6-1.5
# band, shorter so fits_card passes, no new token for invents_name to see: accept_repair
# cannot detect it, and on an unanchored card neither can anything downstream.
RISK_ORDER = {"words": 0, "substitution": 1, "punctuation": 2}


def risk_class(original: str, proposed: str) -> str:
    """What the repair actually DID, as the axis the queue is ordered on.

    Measured over the 682 admitted repairs of the 2026-08-28 run: 529 punctuation, 85
    substitution, 68 words. Every regression in the owner's 45-line read changed a word;
    none of the 529 could. This is NOT a safety gate -- accept_repair already ran and
    admitted all of these -- it is the reading order that puts the reviewer's attention
    where a mistake is still possible."""
    a, b = _words(original), _words(proposed)
    if a == b:
        return "punctuation"
    return "substitution" if len(a) == len(b) else "words"


def _triage_key(e: dict) -> tuple:
    """Stage, then risk, then position in the episode.

    STAGE OUTRANKS RISK, and that is the point: an admitted repair SHIPPED with nothing
    checking its meaning, while a refusal means the ASR text shipped -- the safe outcome --
    and asks the separate audit question of whether the guard was too strict. Ordering the
    two together by risk would make the reviewer change jobs line by line. Time last, so a
    bucket reads in episode order and the reviewer scrubs forwards."""
    return (
        0 if e.get("stage") == "repair_applied" else 1,
        RISK_ORDER.get(e.get("risk", ""), 1),
        (e.get("starts") or [float("inf")])[0],
        e["index"],
    )


# R5: the reference a repair was anchored on, folded into one class the page can chip and
# filter on. r2 records `ref_role`/`ref_provenance`/`ref_track_title` on new entries only;
# the 1,917 items already open when it shipped carry none of them, so the split is:
#
#   dub / translation  the entry names the role of the track its reference came from
#   unanchored         no reference at all -- the same condition as `permanent` below
#   unclassified       there WAS a reference but no recorded role (every legacy entry)
#
# "unclassified" is deliberately not folded into "unanchored": they ask the reviewer
# different questions. An unanchored card cannot be repaired again without a guess; a
# legacy anchored one can, we simply cannot say from which track.
REF_CLASSES = ("dub", "translation", "unanchored", "unclassified")


def ref_class(entry: dict) -> str:
    """Which kind of anchor this entry's repair used, from R2's per-entry fields.

    Only dub and translation reach here: reftracks never selects a signs track and `ours`
    is never a reference, so a recorded role outside those two cannot name an anchor and
    falls through to the reference-text test like a legacy entry."""
    role = str(entry.get("ref_role") or "").strip().lower()
    if role in ("dub", "translation"):
        return role
    return "unanchored" if not (entry.get("reference") or "").strip() else "unclassified"


def entry_id(entry: dict) -> str:
    """Identity of the exact question, independent of its position in a rewritten queue."""
    fields = [entry.get(k, "") for k in ("stage", "reason", "original_text", "proposed_text")]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()[:24]


def _decorate(stem: str, e: dict, index: int, starts: dict, context: dict | None = None) -> dict:
    """One queue entry as the page needs it: its own index, what may be done to it, whether
    doing it is reversible, what the repair changed, where in the episode to hear it, and
    the cards either side of it."""
    d = dict(e)
    d["index"] = index
    d["id"] = entry_id(e)
    d["risk"] = risk_class(e.get("original_text", ""), e.get("proposed_text", ""))
    # DERIVED, not stored: repair.py records no timing on an accepted repair. Every card the
    # line appears on, because duplicate-pair suppression makes repeats a single entry.
    d["starts"] = starts.get(decisions.key(e.get("original_text", "")), [])
    # A card is not a sentence: reflow splits on duration and line length, so a queued line
    # routinely starts or ends mid-clause and cannot be judged on its own. Same key as
    # `starts`, one entry per occurrence, so context and seek target stay paired.
    d["context"] = (context or {}).get(decisions.key(e.get("original_text", "")), [])
    d["offered"] = list(OFFERED.get((str(e.get("stage", "")), str(e.get("reason", ""))), DEFAULT_OFFERED))
    # R5. Normalised, so a consumer never has to tell "absent" from "empty" -- and the whole
    # ref block is present on every entry, legacy ones included, as nulls. `ref_class` is the
    # only field the page filters on; it is always one of REF_CLASSES.
    d["ref_role"] = e.get("ref_role") or None
    d["ref_provenance"] = e.get("ref_provenance") or None
    d["ref_track_title"] = e.get("ref_track_title") or None
    d["ref_class"] = ref_class(e)
    # No reference means nothing downstream can repair this card again -- a bad force here
    # is permanent. Every S31 card is unanchored, so this is the common case, not an edge.
    d["permanent"] = not (e.get("reference") or "").strip()
    return d


def _dedupe_identical(entries: list) -> list:
    """Collapse entries that are the SAME recorded event to one, keeping the first.

    unresolved.record() is a deliberate O(1) append (its own docstring: the array version
    was O(n^2) I/O on a path that fires ~86x per episode), not an upsert -- so calling
    repair.py again on an episode still holding open queue entries (a glossary
    improvement, a model swap -- exactly the rerun this pipeline is supposed to support)
    appends a second identical line rather than updating the first. Safe to collapse for
    DISPLAY only: unresolved.undecided() already treats a text pair as settled everywhere
    once ANY one occurrence is decided, so the redundant siblings need no separate
    resolution of their own -- they vanish with the one row a reviewer actually answers.

    Also fixes a real bug this masked: items.index(e) cannot tell identical dicts apart,
    so every duplicate row was already secretly resolving the SAME (first) index -- a
    reviewer deciding what looked like row 2 of 4 was actually deciding row 1."""
    seen: set = set()
    out = []
    for e in entries:
        key = (e.get("stage"), e.get("reason"), e.get("original_text"), e.get("proposed_text"))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def handle_episode(stem: str, all_reasons: bool = False) -> dict:
    """One episode's queue. Primary view by default; the full unresolved walk on request."""
    ep = _resolve(stem)
    if ep is None:
        return {"error": "unknown episode"}
    items = unresolved.items(ep)
    # Entries orphaned by a re-transcription describe text this episode no longer contains.
    # Nothing will re-queue them so nothing will ever resolve them; the mux gate has ignored
    # them since [S-6] and the page was still listing them.
    wanted = open_entries(ep) if not all_reasons else unresolved.live_only(ep, unresolved.pending(ep))
    # Settled elsewhere. Decided once on the episode it first appeared in, a shared line is
    # not a question here -- the same rule mux.held_for_review has applied since [S-6].
    seen = len(wanted)
    wanted = unresolved.undecided(wanted, _store_for(ep, {}))
    wanted = _dedupe_identical(wanted)
    starts = unresolved.card_starts(ep)
    context = unresolved.card_context(ep)
    entries = [_decorate(ep, e, items.index(e), starts, context) for e in wanted]
    # Sorted for the READER only: index still addresses the jsonl row, so a verdict posted
    # from the page lands on the same entry whatever order it was displayed in.
    entries.sort(key=_triage_key)
    # Named, not silently dropped -- the same reason the index's toggle names the backlog it
    # conceals. A queue that quietly shrank would read as work lost, not work already done.
    return {"stem": ep, "name": os.path.basename(ep), "entries": entries, "settled_elsewhere": seen - len(wanted)}


# One writer at a time. Both writes below are read-modify-write over a WHOLE file -- the
# decisions store and the queue jsonl -- and ThreadingHTTPServer serves each connection on
# its own thread, so two open tabs (or one reviewer double-clicking) could interleave and
# lose each other's verdicts with no error anywhere. A review is one person clicking, so the
# contention this serialises is real but tiny.
_WRITE_LOCK = threading.Lock()


def shared_id(show: str, key_orig: str, key_prop: str) -> str:
    """A stable handle for one shared (show, original, proposed) pair: a hash of exactly what one stored verdict settles. Unlike
    a list offset it names the same pair in every tab and on every reload, so a stale page cannot land on a different pair."""
    return hashlib.sha256(json.dumps([show, key_orig, key_prop]).encode()).hexdigest()[:16]


def handle_shared() -> dict:
    """Every still-open line that appears in MORE than one episode, listed once.

    The opening song is the same repair in 24 episodes and the closing song in 23. Read
    per episode that is 23 further decisions carrying no information, and they are mixed in
    with the lines that only exist there. Measured on the live library 2026-08-28: 665 open
    admitted entries against 487 distinct text pairs.

    Grouped on decisions.key of BOTH texts -- the store's own identity -- so what collapses
    here is exactly what one stored verdict settles. It does NOT collapse the same sung line
    transcribed two ways (`our minds will never give up` and `our mods will never give up`
    are 7 episodes each); different ASR text is a different proposal, and a reviewer reading
    one has not read the other.

    Only `repair_applied`. A guard refusal left the ASR text in place and asks the separate
    audit question; batching those across episodes would hide which release they came from."""
    groups: dict = {}
    stores: dict = {}
    for stem in known_stems():
        show = show_for(stem)
        if not show:
            continue  # a decision has nowhere to go without one, so there is nothing to offer
        # The unfiltered walk on purpose: `repair_applied` entries left the default view in
        # the 2026-11-27 owner decision, and this cross-episode batching is their surface.
        for e in unresolved.undecided(unresolved.live_only(stem, unresolved.pending(stem)), _store_for(stem, stores)):
            if e.get("stage") != "repair_applied":
                continue
            orig, prop = e.get("original_text", ""), e.get("proposed_text", "")
            g = groups.setdefault(
                (show, decisions.key(orig), decisions.key(prop)),
                {"stems": set(), "e": e, "classes": set()},
            )
            g["stems"].add(stem)
            # R5: a shared pair can be anchored differently in different episodes (the same
            # line, a different release's tracks). Report a class only when they agree; a
            # single value taken from whichever episode was walked first would be a claim
            # about all of them that nothing checked.
            g["classes"].add(ref_class(e))
    rows = []
    for (show, _ko, _kp), g in groups.items():
        if len(g["stems"]) < 2:
            continue  # a line in one episode is that episode's question, not a shared one
        e = g["e"]
        rows.append(
            {
                "id": shared_id(show, _ko, _kp),
                "show": show,
                "original_text": e.get("original_text", ""),
                "proposed_text": e.get("proposed_text", ""),
                "episodes": len(g["stems"]),
                "risk": risk_class(e.get("original_text", ""), e.get("proposed_text", "")),
                # None when the copies disagree (see the loop above).
                "ref_class": next(iter(g["classes"])) if len(g["classes"]) == 1 else None,
                "offered": list(OFFERED[("repair_applied", "accepted")]),
            }
        )
    # Deterministic display order (most-repeated first clears the most work per decision). `pair` is only the row's position in
    # THIS rendering, for the page's sort tie-break: it moves whenever another row disappears, so nothing is ever written by it.
    # What a verdict names is `id`, the hash of the exact text pair, which does not move.
    rows.sort(key=lambda r: (-r["episodes"], RISK_ORDER.get(r["risk"], 1), r["show"], r["original_text"]))
    for i, r in enumerate(rows):
        r["pair"] = i
    return {"pairs": rows}


def handle_shared_decide(verdicts: list) -> dict:
    """Record verdicts from the shared list. One store write per show.

    NO QUEUE FILE IS TOUCHED, and that is the design rather than an omission: the verdict is
    show-wide, and unresolved.undecided already hides a settled line wherever it appears --
    including in mux.held_for_review, which has trusted the verdict over the queue flag since
    [S-6]. Marking 24 episodes' rows resolved would be 24 rewrites to reach a state the
    pipeline already agrees on.

    The client sends the stable `id` of a pair this module issued, never the text and never a list offset. A client is not a trust
    boundary, and accepting raw text here would let it write a decision for any line in the show -- including one nobody was ever
    shown. An offset is no better: it names whatever row sits there NOW, so a stale tab would approve a different pair. The id is
    looked up in the list rebuilt UNDER the write lock, so a pair that was decided meanwhile (or no longer shared) is refused as
    stale instead of being written."""
    errors: list = []
    by_show: dict = {}
    with _WRITE_LOCK:
        rows = {r["id"]: r for r in handle_shared()["pairs"]}
        for d in verdicts if isinstance(verdicts, list) else []:
            if not isinstance(d, dict):
                errors.append({"id": None, "error": "not a verdict"})
                continue
            sid, verdict = d.get("id"), str(d.get("verdict", ""))
            if not isinstance(sid, str) or sid not in rows:
                errors.append(
                    {
                        "id": sid if isinstance(sid, str) else None,
                        "error": "stale or unknown shared line: it was decided meanwhile or the page is out of date; reload",
                    }
                )
                continue
            r = rows[sid]
            if verdict not in r["offered"]:
                errors.append({"id": sid, "error": "verdict not offered for this entry"})
                continue
            if verdict == "correct" and decisions.is_blank(d.get("text", "")):
                errors.append({"id": sid, "error": "a correction needs text; the line stays on the page"})
                continue
            by_show.setdefault(r["show"], []).append((r, verdict, str(d.get("text", ""))))
        saved, ddir = 0, decisions.DECISIONS_DIR
        for show, wanted in by_show.items():
            with decisions.locked(show, ddir):
                store = decisions.load(show, ddir)
                for r, verdict, text in wanted:
                    store = decisions.record(store, r["original_text"], r["proposed_text"], verdict, text=text)
                if not decisions.save(store, show, ddir):
                    # Named per show, not swallowed: with two shows on the page one store
                    # can fail while the other lands, and the reviewer must know which.
                    errors.append({"id": None, "error": f"the decisions for {show} could not be saved"})
                    continue
                saved += len(wanted)
    return {"saved": saved, "errors": errors}


def handle_decide_batch(stem: str, verdicts: list) -> dict:
    """Record many verdicts and take their lines out of the queue, in ONE pass.

    The page hands back a whole episode at once. Done one at a time this was a store load, a
    store save and a queue rewrite PER VERDICT, every one of them a round trip over the media
    mount, with a full page reload between each.

    BOTH writes matter and they are not interchangeable: the decision is what stops repair.py
    re-applying and re-queueing the line, the resolved flag is what empties the queue. Sprint
    006 found the [S-6] gate holding an episode forever when only one of the two happened.

    PARTIAL SUCCESS IS REPORTED, not swallowed. A malformed twentieth verdict must not cost
    the reviewer the nineteen good ones, and a verdict that vanished silently is the exact
    failure this loop exists to prevent -- so refused items come back named, with their
    index, and the caller can show them."""
    ep = _resolve(stem)
    if ep is None:
        return {"error": "unknown episode"}
    show = show_for(ep)
    if not show:
        return {"error": "cannot resolve a show for this episode"}
    errors: list = []
    with _WRITE_LOCK:
        items = unresolved.items(ep)
        # Read at CALL time, not bound as a default: decisions.load/save capture
        # DECISIONS_DIR in their signatures at import, so the mount cannot be changed (or
        # pointed at a test directory) after this module loads unless it is passed through.
        ddir = decisions.DECISIONS_DIR
        updates: list = []
        with decisions.locked(show, ddir):
            store = decisions.load(show, ddir)
            for d in verdicts if isinstance(verdicts, list) else []:
                if not isinstance(d, dict):
                    errors.append({"index": None, "error": "not a verdict"})
                    continue
                index, verdict = d.get("index"), str(d.get("verdict", ""))
                # bool is an int in Python, and `True` would index item 1 of somebody else's queue.
                if isinstance(index, bool) or not isinstance(index, int) or not (0 <= index < len(items)):
                    errors.append({"index": index, "error": "no such entry"})
                    continue
                e = items[index]
                # New workspace sends an identity as well as the legacy index. Never let a stale tab approve a new row.
                if "id" in d and (
                    d["id"] != entry_id(e)
                    or e.get("resolved")
                    or not unresolved.undecided([e], store)
                    or not unresolved.live_only(ep, [e])
                ):
                    errors.append({"index": index, "error": "stale review entry; reload this episode"})
                    continue
                # Enforced here, not merely rendered: a client is not a trust boundary, and
                # `force` on an accepted entry would be an unlabelled bypass of the gate.
                if verdict not in OFFERED.get((str(e.get("stage", "")), str(e.get("reason", ""))), DEFAULT_OFFERED):
                    errors.append({"index": index, "error": "verdict not offered for this entry"})
                    continue
                text, note = str(d.get("text", "")), str(d.get("note", ""))
                if verdict == "correct" and decisions.is_blank(text):
                    # Named and left pending: decisions.record would drop it silently, and a queue entry resolved for a verdict
                    # that does not exist releases an opted-in review hold with no human decision behind it.
                    errors.append({"index": index, "error": "a correction needs text; the entry stays pending"})
                    continue
                orig, prop = e.get("original_text", ""), e.get("proposed_text", "")
                store = decisions.record(store, orig, prop, verdict, text=text, note=note)
                stored = decisions.lookup(store, orig, prop)
                if stored is None:
                    # Counted as saved only once the verdict is in the store (record() refuses an unusable pair or verdict).
                    errors.append({"index": index, "error": "the verdict could not be stored; the entry stays pending"})
                    continue
                updates.append((index, stored.get("verdict") != "reject", note))
            if not updates:
                return {"saved": 0, "show": show, "errors": errors, "queue_cleared": False}
            if not decisions.save(store, show, ddir):
                # Reported as NOT saved rather than swallowed: a review that silently
                # discards the human's decisions is worse than one that errors, because
                # they believe they are settled. Nothing is cleared from the queue either,
                # so they stay reviewable.
                return {"error": "the decisions could not be saved", "errors": errors}
        # The verdicts are durable from here. The queue flags are a SECOND file and the two
        # cannot be made atomic across them -- but the report can be honest. The gate is
        # unaffected either way: mux trusts the durable verdict, not this flag.
        cleared = unresolved.resolve_many(ep, updates)
        forget(ep)
    out = {"saved": len(updates), "show": show, "errors": errors, "queue_cleared": bool(cleared)}
    if not cleared:
        out["warning"] = "verdicts saved, but the queue could not be cleared — they will still be listed"
    return out


def handle_decide(stem: str, index: int, verdict: str, text: str = "", note: str = "") -> dict:
    """One verdict, as a batch of one. The --review CLI and the single-entry tests keep this
    shape; there is deliberately no second implementation of the two writes behind it."""
    res = handle_decide_batch(stem, [{"index": index, "verdict": verdict, "text": text, "note": note}])
    if res.get("error"):
        return {"error": res["error"]}
    if res.get("errors"):
        return {"error": res["errors"][0]["error"]}
    out = {"saved": True, "verdict": verdict, "show": res["show"], "queue_cleared": res["queue_cleared"]}
    if res.get("warning"):
        out["warning"] = res["warning"]
    return out


def handle_apply(stem: str) -> dict:
    """Push this episode's stored decisions into the video, via [S-5]."""
    ep = _resolve(stem)
    if ep is None:
        return {"error": "unknown episode"}
    store, _ = decisions.decisions_for(ep)
    forget(ep)  # apply_episode drops the stamp and rewrites sidecars; the cached queue is stale
    return review_apply.apply_episode(ep, store, apply=True)


# --- HTTP ----------------------------------------------------------------------------
# route() is the entire request surface and is a pure function of (method, path, body,
# token), so every test drives it directly and no socket is ever opened in the suite.


# Routes answered without a token, besides the /review document and its assets. EXPLICIT, so adding one is a visible edit and
# a test (tests/test_review_login.py) compares this list with the code. Everything else under /api needs the token.
OPEN_ROUTES = (("GET", "/healthz"), ("GET", "/api/auth-mode"), ("POST", "/api/login"), ("POST", "/api/setup"))
_SMALL_JSON_ROUTES = ("/api/login", "/api/setup")
_FORM_TYPES = ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data")  # what a cross-site <form> can send
LOGIN_MAX_BODY = 4096  # a username and a password; the generic POST cap (1 MiB) is far too generous for an unauthenticated route


class LoginThrottle:
    """Failure counters for /api/login, in memory, with an injectable clock.

    Two tables, because they defend different things:

    * **addresses** -- after ``max_fail`` consecutive failures inside ``window`` seconds the
      client is locked out for ``lockout`` seconds (429 with Retry-After; the credentials are
      not even compared). At most ``cap`` addresses are tracked and a flood of distinct
      addresses evicts the OLDEST, so memory is bounded and a real client only ever loses its
      OWN record.
    * **usernames** -- a name being guessed at from many addresses must not be locked out: an
      IPv6 /64 holds enough addresses to keep a 429 on the account's owner forever, which is a
      denial of service, not a defence. A name is only SLOWED: from ``max_fail`` failures on,
      every login for it pauses ``pause`` seconds, doubling per extra failure up to
      ``pause_cap``. The table is separate from the address one, and a record that has
      accumulated failures is never evicted by a flood of addresses or of fresh names, so a
      diverse-guess attack can neither reset nor remove the counter of the name it targets.

    A success forgets the client and the username's counter.
    """

    def __init__(
        self,
        clock=time.monotonic,
        sleeper=time.sleep,
        max_fail=5,
        window=300.0,
        lockout=60.0,
        cap=1024,
        user_cap=1024,
        pause=0.5,
        pause_cap=5.0,
    ):
        from collections import OrderedDict

        self.clock, self.max_fail, self.window, self.lockout = clock, max_fail, window, lockout
        self.cap, self.user_cap, self.pause, self.pause_cap = cap, user_cap, pause, pause_cap
        self._sleeper = sleeper
        self._seen: OrderedDict[str, dict] = OrderedDict()
        self._users: dict[str, dict] = {}
        self._lock = threading.Lock()

    def __len__(self):
        return len(self._seen)

    def tracked(self, addr: str) -> bool:
        return addr in self._seen

    def retry_after(self, addr: str) -> int:
        """Seconds until ``addr`` may try again; 0 when it may try now."""
        with self._lock:
            rec = self._seen.get(addr)
            wait = (rec["locked_until"] - self.clock()) if rec else 0
            return max(0, math.ceil(wait)) if wait > 0 else 0

    def failure(self, addr: str) -> None:
        with self._lock:
            now = self.clock()
            rec = self._seen.pop(addr, None)
            if rec is None or now - rec["first"] > self.window:
                rec = {"fails": 0, "first": now, "locked_until": 0.0}
            rec["fails"] += 1
            if rec["fails"] >= self.max_fail:
                rec["locked_until"], rec["fails"], rec["first"] = now + self.lockout, 0, now
            self._seen[addr] = rec  # re-inserted last: the newest
            while len(self._seen) > self.cap:
                self._seen.popitem(last=False)

    def success(self, addr: str) -> None:
        with self._lock:
            self._seen.pop(addr, None)

    # --- usernames: slowed, never locked ------------------------------------------------

    def user_pause(self, key: str) -> float:
        """Seconds a login for ``key`` should wait; 0 below the failure threshold."""
        with self._lock:
            rec = self._users.get(key)
            if rec is None or self.clock() - rec["first"] > self.window:
                return 0.0
            extra = rec["fails"] - self.max_fail
            return min(self.pause_cap, self.pause * 2**extra) if extra >= 0 else 0.0

    def user_failure(self, key: str) -> None:
        with self._lock:
            now = self.clock()
            self._prune_users(now)
            rec = self._users.get(key)
            if rec is None or now - rec["first"] > self.window:
                rec = {"fails": 0, "first": now}
                if not self._remember_user(key, rec):
                    return  # every slot is held by a name that is being attacked
            rec["fails"] = min(rec["fails"] + 1, self.max_fail + 16)
            self._users[key] = rec

    def user_success(self, key: str) -> None:
        with self._lock:
            self._users.pop(key, None)

    def sleep(self, seconds: float) -> None:
        """Wait the pause; injectable so tests record it instead of sleeping."""
        self._sleeper(seconds)

    def _prune_users(self, now: float) -> None:
        for key in [k for k, r in self._users.items() if now - r["first"] > self.window]:
            self._users.pop(key, None)

    def _remember_user(self, key: str, rec: dict) -> bool:
        if key in self._users or len(self._users) < self.user_cap:
            self._users[key] = rec
            return True
        # Only a name with no accumulated failures is evictable: the point of this table is
        # that a flood can neither reset nor remove the counter of the name under attack.
        for old, r in list(self._users.items()):
            if r["fails"] < self.max_fail:
                del self._users[old]
                self._users[key] = rec
                return True
        return False


_THROTTLE = LoginThrottle()


def workspace_asset(path: str) -> tuple:
    """Serve only the inert SPA document and Vite's flat asset directory, never arbitrary files."""
    if path in ("/review", "/review/", "/review/index.html", "/review/setup", "/review/login"):
        target = UI_DIST / "index.html"
    elif re.fullmatch(r"/review/assets/[A-Za-z0-9_.-]+\.(?:js|css|woff2|svg)", path):
        target = UI_DIST / "assets" / path.rsplit("/", 1)[1]
    else:
        return 404, {"error": "no such asset"}
    if not target.resolve().is_relative_to(UI_DIST.resolve()):
        return 404, {"error": "no such asset"}
    try:
        return 200, target.read_bytes()
    except OSError:
        return 503 if target.name == "index.html" else 404, {
            "error": "Review workspace not built. Run npm ci && npm run build in web/."
        }


def preview_audio(body: dict) -> tuple:
    """Short, on-demand English audio evidence. Cue bounds come from the known episode, never the client."""
    stem = _resolve(str(body.get("stem", "")))
    if stem is None:
        return 404, {"error": "unknown episode"}
    try:
        index, occurrence = int(body.get("index", "")), int(body.get("occurrence", "0"))
        if index < 0 or occurrence < 0:
            raise ValueError
    except (TypeError, ValueError):
        return 400, {"error": "invalid parameter: index and occurrence must be non-negative integers"}
    try:
        entries = unresolved.items(stem)
        entry = entries[index]
        if body.get("id") != entry_id(entry):
            return 409, {"error": "stale review entry; reload this episode"}
        conf_path = Path(stem)
        if not any(conf_path.resolve().is_relative_to(Path(root).resolve()) for root in ROOTS):
            return 404, {"error": "episode evidence unavailable"}
        cards = common.read_conf(stem)
        if not isinstance(cards, list) or not isinstance(entry, dict):
            raise ValueError
        matches = [
            c
            for c in cards
            if isinstance(c, dict) and decisions.key(c.get("text", "")) == decisions.key(entry.get("original_text", ""))
        ]
        card = matches[occurrence]
        start, end = float(card["start"]), float(card["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return 400, {"error": "no playable card for this occurrence"}
    video = common.find_video(stem)
    if not video or not any(Path(video).resolve().is_relative_to(Path(root).resolve()) for root in ROOTS):
        return 404, {"error": "episode media unavailable"}
    if not _AUDIO_SLOTS.acquire(blocking=False):
        return 429, {"error": "audio preview busy; try again shortly"}
    try:
        # The SAME decision the pipeline makes (core/media_select.py). The preview used to
        # accept English tags only, so an `und` track titled "English Dub" -- the shape the
        # pipeline itself transcribes -- 422'd here while the episode it came from played.
        streams = media_select.probe_audio_streams(video, timeout=10)
        pick = media_select.select_english_audio(streams)
        if not pick.ok:
            return 422, {"error": f"no English dialogue audio track ({pick.reason})"}
        offset = max(0, start - 1.5)
        duration = min(AUDIO_MAX_SECONDS, end + 1.5 - offset)
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-ss",
                str(offset),
                "-i",
                video,
                "-map",
                f"0:{pick.index}",
                "-t",
                str(duration),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-f",
                "wav",
                "pipe:1",
            ],
            capture_output=True,
            timeout=30,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        if not result.stdout.startswith(b"RIFF") or len(result.stdout) <= 44:
            return 502, {"error": "audio extraction produced no clip"}
        return 200, result.stdout
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, media_select.MediaProbeError):
        return 502, {"error": "audio preview unavailable; check the media and ffmpeg"}
    finally:
        _AUDIO_SLOTS.release()


def route(method: str, path: str, body: dict, token, client: str = "", headers: dict | None = None) -> tuple:
    """(status, payload) for one request; route_full is the same call with the response headers (a Set-Cookie) as a third item."""
    return route_full(method, path, body, token, client, headers)[:2]


def route_full(method: str, path: str, body: dict, token, client: str = "", headers: dict | None = None) -> tuple:
    """(status, payload, extra_headers). ``token`` is the pre-accounts calling convention and counts as X-Api-Key;
    ``headers`` carry the Cookie, X-Api-Key and CSRF header. review_auth.decide runs first, on the bare path (no query
    string): no handler is reached by a caller its route's role does not allow. ``client`` is the peer address,
    used only to throttle /api/login."""
    bare = path.partition("?")[0]
    hdrs = dict(headers or {})
    if token and not review_auth.header(hdrs, review_auth.API_KEY_HEADER):
        hdrs[review_auth.API_KEY_HEADER] = token
    decision = review_auth.decide(method, bare, hdrs)
    if decision.status is not None:
        return decision.status, {"error": decision.error}, {}
    result = _route(method, path, bare, body, decision, client, hdrs)
    status, payload, extra = result if len(result) == 3 else (*result, {})
    p = decision.principal
    if p is not None and p.kind == "session" and p.refresh and status < 400 and "Set-Cookie" not in extra:
        extra = {
            **extra,
            "Set-Cookie": review_auth.session_cookie(p.sid, True, review_auth.cookie_secure(hdrs)),
        }  # sliding 30 days
        review_auth.sessions().cookie_sent(p.sid)
    return status, payload, extra


def _route(method: str, path: str, bare: str, body: dict, decision, client: str, headers: dict | None) -> tuple:
    """The old route() body: (status, payload) or (status, payload, extra_headers). ``headers`` are read by the webhook
    routes (their own X-Webhook-Token)."""
    if method == "GET" and (path == "/review" or path.startswith("/review/")):
        return workspace_asset(path)
    if method == "GET" and path == "/healthz":
        return handle_healthz()
    if method == "GET" and path == "/api/auth-mode":
        return (
            200,
            {
                "setup_required": review_auth.setup_required(),
                "accounts_ok": review_auth.accounts_ok(),
            },
        )
    if method == "POST" and bare == "/api/setup":
        return review_auth.handle_setup(body)
    if method == "POST" and bare == "/api/login":
        return review_auth.handle_login(body, client, _THROTTLE, headers)
    if method == "POST" and bare == "/api/logout":
        return review_auth.handle_logout(decision.principal, headers)
    if method == "GET" and bare == "/api/me":
        return review_auth.handle_me(decision.principal)
    if method == "POST" and bare in review_webhooks.WEBHOOK_ROUTES:
        # Sonarr/Radarr cannot send the review token: these routes carry their own WEBHOOK_TOKEN, checked inside
        # handle(). They are deliberately NOT in OPEN_ROUTES, which stays the three pinned entries.
        return review_webhooks.handle(
            method,
            path,
            body,
            headers=headers,
            create=review_requests.create,
            list_requests=review_requests.list_requests,
            anime_root=_anime_root(),
        )
    admin_handler = ADMIN_ROUTES.get((method, bare))
    if admin_handler is not None:
        return (400, {"error": "invalid JSON"}) if body is None else admin_handler(body)
    if bare in ("/api/apikey", "/api/apikey/reset"):
        return review_auth.handle_apikey(method, bare)
    if body is None:  # only the strict JSON routes produce None (see Handler._with_body)
        return 400, {"error": "invalid JSON"}
    if bare == "/api/users" or bare.startswith("/api/users/"):
        return review_auth.handle_users(method, bare, body, decision.principal)
    if method == "PUT" and bare == "/api/me/password":
        return review_auth.handle_change_password(body, decision.principal, client, _THROTTLE, headers)
    if method == "GET" and path == "/api/audio":
        return preview_audio(body)
    if method == "GET" and path == "/api/episodes":
        return 200, handle_index()
    if method == "GET" and path == "/api/shows":
        return 200, {"shows": review_requests.list_shows(_anime_root())}
    if method == "GET" and path == "/api/episode":
        return 200, handle_episode(str(body.get("stem", "")), all_reasons=bool(body.get("all")))
    if method == "POST" and path == "/api/decide":
        # Two shapes on one route, so there is ONE authorised write path for a verdict
        # rather than two that could drift apart. The page sends the batch; is the
        # single-verdict form the tests and any scripted caller still use.
        if isinstance(body.get("decisions"), list):
            return 200, handle_decide_batch(str(body.get("stem", "")), body["decisions"])
        idx = body.get("index")
        return 200, handle_decide(
            str(body.get("stem", "")),
            idx if isinstance(idx, int) else -1,
            str(body.get("verdict", "")),
            text=str(body.get("text", "")),
            note=str(body.get("note", "")),
        )
    if method == "GET" and path == "/api/shared":
        return 200, handle_shared()
    # P33 audit-first: what the app decided, why, and the one-tap undo. The pending queues are
    # still here (/api/episode, /api/glossary) but they are the exception list now, not the home.
    if method == "GET" and path == "/api/audit":
        try:
            limit = int(body.get("limit") or review_audit.DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = review_audit.DEFAULT_LIMIT
        return 200, review_audit.feed(limit=limit, show=str(body.get("show") or ""))
    if method == "POST" and path == "/api/audit/undo":
        return review_audit.undo(str(body.get("id") or ""))
    if method == "GET" and path == "/api/queues":
        return 200, review_audit.queues()
    if method == "POST" and path == "/api/shared":
        d = body.get("decisions")
        return 200, handle_shared_decide(d if isinstance(d, list) else [])
    if method == "POST" and path == "/api/apply":
        return 200, handle_apply(str(body.get("stem", "")))
    if method == "GET" and bare in ("/api/requests", "/api/schedule"):
        return review_requests.handle("GET", bare, body, anime_root=_anime_root())
    if bare in (
        "/api/settings",
        "/api/settings/schema",
        "/api/settings/reset",
        "/api/settings/test",
        "/api/settings/models",
        "/api/system/restart",
        "/api/system/processing",
        "/api/system/start",
        "/api/system/pause",
        "/api/system/stop",
    ):
        p = decision.principal
        return settings_api.handle(method, bare, body, who=f"{p.kind}:{p.name}" if p and p.name else "admin")
    if path == "/api/status" and method == "GET":
        # ONE read of the cache global: list and fill time always come from the same fill.
        snapshot = _STEMS_CACHE
        return review_status.handle(method, path, body, stems_cache=snapshot, open_entries=open_entries, now=time.monotonic())
    if path == "/api/glossary" and method in ("GET", "POST"):
        p = decision.principal
        who = f"{p.kind}:{p.name}" if p and p.name else ("token" if auth_required() else "anonymous")
        return review_glossary.handle(method, path, body, operator=who, role=decision.role or "user")
    # The versioned, frozen contract for agents and scripts. Additive: the classic /api/* routes
    # above are untouched and are still what the web UI calls. Auth already ran in route_full, so a
    # /api/v1 path this module does not serve was judged by the role table (unlisted = admin).
    if bare == review_api_v1.API_PREFIX or bare.startswith(review_api_v1.API_PREFIX + "/"):
        p = decision.principal
        who = f"{p.kind}:{p.name}" if p and p.name else ("token" if auth_required() else "anonymous")
        return review_api_v1.handle(
            method,
            bare,
            body,
            snapshot=_STEMS_CACHE,
            anime_root=_anime_root(),
            health=handle_healthz,
            who=who,
        )
    return 404, {"error": "no such route"}


def _js(value: str) -> str:
    """A value as a JS string literal, safe inside a <script> block.

    NOT html.escape: that is for HTML text, and running a file path through it turns
    `Tom & Jerry` into `Tom &amp; Jerry`, which the page then posts back as a stem no
    `_resolve()` will recognise -- every verdict on that show refused, with no explanation
    the reviewer can act on. json.dumps produces the correct literal; `<` is escaped on top
    of it because json.dumps does not touch `/`, so any value containing `</script>` would
    otherwise close the block early."""
    return json.dumps(value).replace("<", "\\u003c")


# Mobile Safari lays out at 980px and scales the result down to fit unless the page declares a
# viewport, so a page without one renders at roughly a third scale on a phone and reads as
# broken -- which is how these pages were reported on 2026-11-21 (adjudicated from a phone
# against omarchy's :8842). Desktop stays the shape of this tool; this only stops the shrink.
_VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'


# Shared by both pages, so the shared-lines list and an episode queue cannot drift apart
# visually -- the risk colours in particular have to mean the same thing in both.
_CSS = (
    "body{font:14px system-ui;max-width:52em;margin:2em auto}"
    "li{margin:1em 0;border-left:3px solid #ccc;padding-left:.8em}"
    ".o{color:#900}.p{color:#060}small{color:#666}"
    # The cards either side of the current line, so a reviewer can tell a whole sentence
    # from a fragment of one. Muted so it reads as background, not a repeat of the .o/.p
    # pair below it -- but `.ctx b` (the current line, bolded) stays full-strength so the
    # one line actually under review is not muted along with its neighbours.
    ".ctx{color:#888;font-size:92%;margin-bottom:.2em}.ctx b{color:#111;font-size:105%;font-weight:600}"
    "details{margin:.4em 0}summary{cursor:pointer}details.season{margin-left:1.4em}"
    "li.ep{border:0;margin:.25em 0}.adm{color:#060;font-weight:600}.ref{color:#888;font-size:90%}"
    "#filter{padding:.3em}"
    "#bar{position:sticky;bottom:0;background:#fff;border-top:2px solid #333;padding:.6em 0;margin-top:1em}"
    "#bar button{padding:.4em .8em;margin-right:.5em}#tally{color:#888}"
    "label:has(input[type=radio]){margin-right:.9em;cursor:pointer}"
    "button.clear{margin-left:.4em;font-size:.85em;padding:0 .45em;opacity:.5;cursor:pointer}"
    "button.clear:hover{opacity:1}"
    "li.rwords{border-left-color:#c00}li.rsubstitution{border-left-color:#e90}"
    "li.rpunctuation{border-left-color:#ccc}"
    # Narrow screens. The viewport above stops the shrink-to-fit; this is what a phone then sees,
    # and at 390px the fixed 14px/52em desktop shape is still the wrong shape. 16px also stops
    # iOS zooming the page when a text input takes focus. The sticky bar is bottom-anchored --
    # exactly where iOS floats its URL pill -- so it has to clear the home indicator itself
    # (viewport-fit=cover is what makes env() non-zero there).
    "html{-webkit-text-size-adjust:100%}"
    "@media (max-width:640px){"
    "body{font-size:16px;max-width:none;margin:.8em}"
    "li{margin:.9em 0}"
    "#filter{width:100%;box-sizing:border-box;padding:.5em}"
    "label:has(input[type=radio]){display:block;margin:0;padding:.45em 0}"
    "input[type=radio]{width:1.25em;height:1.25em;vertical-align:-.25em}"
    "#bar{padding:.7em 0 calc(.7em + env(safe-area-inset-bottom))}"
    "#bar button{padding:.8em 1em}"
    "}"
)


def handle_healthz() -> tuple:
    """Return 200 OK or 503 Service Unavailable based on heartbeat conditions.

    Response shape: {"ok": True} on 200, {"ok": False, "reasons": [...]} on 503.
    Never raises, never includes a filesystem path."""
    hb = common.read_heartbeat()
    if hb is None:
        return 503, {"ok": False, "reasons": ["no heartbeat yet"]}

    now = time.time()
    try:
        last_sweep_end = None if hb.get("last_sweep_end") is None else float(hb["last_sweep_end"])
    except (TypeError, ValueError):
        # A document we wrote is numeric; a hand-edited or corrupt one must fail closed as
        # "no sweep has completed yet", never raise out of a probe.
        last_sweep_end = None
    rescan_interval = int(os.environ.get("RESCAN_INTERVAL", "21600"))
    stale_after = common.STALE_SWEEP_MULTIPLIER * rescan_interval
    reasons = []

    if last_sweep_end is None:
        reasons.append("stale: no sweep has completed yet")
    elif (now - last_sweep_end) > stale_after:
        reasons.append(f"stale: last sweep ended {now - last_sweep_end:.0f}s ago (threshold {stale_after}s)")

    if not hb.get("roots_readable", True):
        reasons.append("roots_readable is false")

    if not hb.get("order_file_present", True):
        reasons.append("order_file_present is false")

    if reasons:
        return 503, {"ok": False, "reasons": reasons}
    return 200, {"ok": True}


class Handler(http.server.BaseHTTPRequestHandler):
    # BaseHTTPRequestHandler.timeout is None, and socketserver.StreamRequestHandler.setup()
    # only calls connection.settimeout() when it is not None -- so without this rfile.read()
    # blocks with no deadline. The 1MB cap bounds MEMORY per request and nothing else: a
    # client declaring a legal length and dripping a byte a minute pins a worker thread
    # BEFORE review_auth.decide() is reached, and ThreadingHTTPServer spawns those threads uncapped.
    # 30s is long for a review POST (a verdict is a few hundred bytes) and short for a drip.
    timeout = 30

    def _client(self) -> str:
        """The peer address (throttle key); empty when the transport has none."""
        return (getattr(self, "client_address", None) or ("",))[0]

    def _send(self, status, payload, ctype="application/json", extra=None):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        # No framing, no sniffing, no referrer: the page renders text this pipeline did not
        # author, and the token must not ride out on a Referer header.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if status == 429 and isinstance(payload, dict) and payload.get("retry_after"):
            self.send_header("Retry-After", str(int(payload["retry_after"])))
        if self.path.split("?", 1)[0].startswith("/review"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
                "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
            )
        for name, value in (extra or {}).items():
            for v in value if isinstance(value, list) else [value]:  # a login sends two Set-Cookie lines
                self.send_header(name, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse

        u = urlparse(self.path)
        # One UI: the review workspace. The server-rendered pages are gone; their old URLs keep
        # working as redirects so a bookmark cannot land on a surface that no longer exists.
        if u.path in ("/", "/index.html"):
            return self._send(302, b"", "text/html; charset=utf-8", {"Location": "/review/"})
        if u.path == "/shared":
            return self._send(302, b"", "text/html; charset=utf-8", {"Location": "/review/#/shared"})
        if u.path == "/healthz":
            status, payload = handle_healthz()
            return self._send(status, payload)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        status, payload, extra = route_full(
            "GET",
            u.path,
            q,
            None,
            client=self._client(),
            headers=dict(self.headers.items()),
        )
        ctype = "application/json"
        if isinstance(payload, bytes):
            if u.path == "/api/audio":
                ctype = "audio/wav"
            else:
                ctype = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".woff2": "font/woff2"}.get(
                    Path(u.path).suffix, "text/html; charset=utf-8"
                )
        self._send(status, payload, ctype, extra)

    def do_POST(self):
        self._with_body("POST")

    def do_PUT(self):
        self._with_body("PUT")

    def do_DELETE(self):
        self._with_body("DELETE")

    def _with_body(self, method: str):
        # The body is read BEFORE review_auth.decide() runs, so this bound is what stands between an
        # unauthenticated caller and this root process's memory. int("-1") does NOT raise, so
        # a declared length of -1 slipped past a `> cap` test and rfile.read(-1) then read to
        # EOF -- unbounded, which is exactly what the check existed to prevent. Anything not
        # a non-negative integer is refused without reading a byte.
        raw = self.headers.get("Content-Length")
        if method == "DELETE" and raw is None:
            raw = "0"  # a DELETE normally has no body
        try:
            n = int(raw or "")
        except (TypeError, ValueError):
            return self._send(411, {"error": "a valid Content-Length is required"})
        if n < 0:
            return self._send(411, {"error": "a valid Content-Length is required"})
        if n > 1 << 20:
            return self._send(413, {"error": "body too large"})
        if self.path.split("?", 1)[0] in _SMALL_JSON_ROUTES:
            # Unauthenticated, so stricter than the other POSTs: JSON only, and a few KiB at most.
            if n > LOGIN_MAX_BODY:
                return self._send(413, {"error": "body too large"})
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                return self._send(415, {"error": "application/json is required"})
        path_only = self.path.split("?", 1)[0]
        is_webhook = method == "POST" and path_only in review_webhooks.WEBHOOK_ROUTES
        if n > 0 and not is_webhook and self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() in _FORM_TYPES:
            return self._send(415, {"error": "application/json is required"})
        if is_webhook:
            # The webhook handler wants the RAW bytes: it checks its token first, then the 256 KiB cap, then parses,
            # so a bad JSON body is a 400 (not a silent {}) and an oversize one a 413. Read at most cap + 1 bytes.
            body = self.rfile.read(min(n, review_webhooks.BODY_CAP + 1))
        else:
            strict = (method, path_only) in _JSON_REQUIRED
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = None if strict else {}
            if body is not None and not isinstance(body, dict):
                body = None if strict else {}  # None reaches route(), which answers 400 once the caller is authorised
        status, payload, extra = route_full(
            method,
            self.path,
            body,
            None,
            client=self._client(),
            headers=dict(self.headers.items()),
        )
        self._send(status, payload, extra=extra)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - the base class names it this
        # Never log a query string: the webhook routes accept ?token=, and a token in the request line would reach the log.
        log("review server: " + _REQUEST_LINE_QUERY.sub(r"\1?[query hidden]", format % args))


class BoundedHTTPServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer with a ceiling on requests in flight.

    Over the ceiling the connection is CLOSED rather than queued: an unbounded accept queue
    is the same resource exhaustion with an extra step, and a reviewer would rather see a
    dropped connection than a page that hangs. The slot is released in
    process_request_thread's finally, which is the only place that runs for both the served
    and the errored path -- releasing in process_request would return the slot before the
    work it is guarding had started."""

    daemon_threads = True

    def __init__(self, *a, **k):
        self._slots = threading.Semaphore(MAX_CONCURRENT)
        super().__init__(*a, **k)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            log(f"review server: refusing {client_address[0]} — {MAX_CONCURRENT} requests already in flight")
            self.close_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def warm_cache() -> None:
    """Walk the library and read every queue, so the first request does not have to.

    The walk is unavoidable once per cache lifetime; WHO waits for it is a choice. Measured
    on the live library, a cold index took over 900s -- and the reviewer was the one sitting
    in front of it, after every deploy. The container has those minutes anyway.

    NEVER RAISES. This runs on a background thread while the server is already accepting
    connections, and a media tree that is not mounted yet must delay the first page rather
    than take the process down."""
    try:
        t = time.monotonic()
        stems = known_stems()
        for stem in stems:
            open_entries(stem)
        log(f"review server: cache warm — {len(stems)} episodes in {time.monotonic() - t:.0f}s")
    except Exception as exc:  # noqa: BLE001 -- a warmer must not be able to end the process
        log(f"review server: cache warm failed ({exc}) — the first request will pay for it")


# How long before the CURRENT cache's own tracked expiry to fire the next warm. Small on
# purpose: it only has to beat the gap between expiry and the next re-warm, not the walk
# itself -- a request landing in THAT gap is the exact cold-cache hit this whole mechanism
# exists to prevent.
_WARM_MARGIN_S = 5.0


def _next_warm_delay(stems_cache, now: float) -> float:
    """Seconds until the next re-warm should run, timed to land just BEFORE the cache
    warm_cache() just built would expire -- not after. Reported live 2026-09-01: warm_cache
    only ever ran once, at startup; measured on the real 968-episode library the stems TTL
    (STEMS_TTL_FACTOR=20 against a 16.82s walk) expired in ~336s, and every request landing
    after that paid the full cold cost again (measured live: 115-157s), not just the very
    first request after a deploy. Falls back to STEMS_TTL when there is no cache to read an
    expiry from -- the walk failed, or this is the very first call.

    Floored at STEMS_TTL, not a fixed 1s -- also found live: _stems_ttl() scales the cache's
    OWN lifetime to how long its walk took, so a walk that happens to land fast (real NFS
    latency varies, especially while merge_pass is concurrently muxing multi-GB files on the
    same mount) gets assigned a short TTL, which schedules another re-warm soon, which if
    ALSO fast keeps compounding -- observed live as a burst of "cache warm ... in 0s" lines
    seconds apart. STEMS_TTL is already this codebase's own answer to "the shortest sane
    cache lifetime" (see its definition); reusing it here prevents the feedback loop without
    inventing a second constant that could drift from the first."""
    expiry = stems_cache[0] if stems_cache else now + STEMS_TTL
    return max(STEMS_TTL, expiry - now - _WARM_MARGIN_S)


def _warm_cache_forever() -> None:
    """Keep the cache perpetually warm, not just once at startup.

    A single warm_cache() call goes stale the moment _STEMS_CACHE's own TTL expires, and
    nothing was re-warming it after that -- so the FIRST request after every ~5-6 minute
    window (on the real library) paid the full cold-cache cost `warm_cache`'s own docstring
    says a reviewer should never have to. NEVER RAISES past one iteration: warm_cache()
    already swallows its own failures, so a transient mount hiccup delays the next warm
    rather than killing this thread."""
    while True:
        warm_cache()
        time.sleep(_next_warm_delay(_STEMS_CACHE, time.monotonic()))


def startup_auth() -> None:
    """Auth on: refuse to start without scrypt, import REVIEW_USERNAME/REVIEW_PASSWORD once, seed the API key from the
    review token. Auth off (REVIEW_AUTH=off): nothing to prepare, but say so loudly."""
    if not auth_required():
        log("review server: WARNING auth is off: admin writes are open to anyone who can reach this port")
        return
    if os.environ.get("REVIEW_ADMIN_TOKEN") or os.environ.get("REVIEW_ADMIN_TOKEN_FILE"):
        log("review server: REVIEW_ADMIN_TOKEN is retired and ignored; admins sign in with an account or use the API key")
    if not review_auth.scrypt_available():
        log("review server: hashlib.scrypt is not available in this Python/OpenSSL build; refusing to start with auth on")
        raise SystemExit(2)
    review_auth.import_env_admin()
    review_auth.init_api_key()
    review_auth.dummy_hash()  # built now, so the first login for an unknown name costs one scrypt like every other


def serve(port: int = 0):
    # Checked before ANYTHING else, including resolve_token() (which would otherwise
    # mint/print a token nobody needs). A fail-closed opt-in for an unattended deploy:
    # REQUIRE_TOKEN=1 means "never start wide open", so a REVIEW_AUTH=off left behind
    # in an old .env is caught at start, not discovered later by whoever finds the
    # port open.
    if os.environ.get("REQUIRE_TOKEN") == "1" and not auth_required():
        log("review server: REQUIRE_TOKEN=1 but auth is disabled (REVIEW_AUTH=off)")
        raise SystemExit(2)
    resolve_token()  # generates and prints the VALUE on first start, before anything is served
    announce_token()  # and on every start, says where to find it
    startup_auth()
    srv = BoundedHTTPServer((REVIEW_BIND, port or REVIEW_PORT), Handler)
    log(f"review server: listening on {REVIEW_BIND}:{port or REVIEW_PORT}")
    # Started BEFORE serve_forever and as a daemon: the port must open immediately either
    # way, and a warm that is still running must not hold up a shutdown.
    threading.Thread(target=_warm_cache_forever, name="warm", daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    serve()
