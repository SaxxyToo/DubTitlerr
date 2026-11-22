#!/usr/bin/env python3
"""ADDITIVE glossary mining. For one show: load its existing dictionary (if any), mine the
NEW (not-yet-dubtitled) episodes' embedded English subtitles for recurring proper nouns
(character/place names — the official spellings), and ADD any new ones to the dictionary.
Never rebuilds from scratch: curated names, hard_fixes and a curated initial_prompt are
preserved; mining only appends. Runs BEFORE generate (in gen_loop) so the grown dictionary
applies to the very episodes being transcribed, and grows again whenever new episodes appear.

BOOTSTRAP (`--bootstrap`): the one deliberate exception to the per-episode skip below. A show
can be generated and delivered with no glossary at all (DAN DA DAN, 2026-11-19: mining
admitted nothing, no request file existed, both episodes show `glossary_version: none`), and
delivery closes the mining window for good — so nothing ever creates the file. `--bootstrap`
mines DELIVERED episodes too, but only for a show that has NO glossary file; with one present
it refuses (exit 2) and reads nothing. Same gates (deny-list, MINE_MIN_COUNT, lowercase share,
mid-sentence) and still purely additive. Bounded to the most recent episodes that actually carry
a mineable English track (DEFAULT_BOOTSTRAP_LIMIT, or `--bootstrap-limit`), so a rescue run cannot
re-read a whole show: a file whose newest releases have no such track costs a probe, not an
extraction. It writes only the glossary file, never the media tree.

CPU only (ffmpeg + pysubs2). Env: GLOSSARY_DIR (default /config/glossaries),
MINE_MIN_COUNT (a name must recur >= this across the new episodes, default 3).
Built with help of Claude (Anthropic).
"""

import argparse
import contextlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections.abc import Collection

import pysubs2

from dubtitlerr.core.common import (
    NON_DIALOGUE_STYLE,
    SIGNS_TITLE,
    STAMP_SUFFIX,
    is_our_track,
    load_extras,
    stop_requested,
    stream_title,
)

EXTRA_DIRS = load_extras()  # data/extras.txt is the source (see common.load_extras)

GLOSS_DIR = os.environ.get("GLOSSARY_DIR", "/config/glossaries")
MIN_COUNT = int(os.environ.get("MINE_MIN_COUNT", "3"))
# Bootstrap's bound: the most recent N episode files, by path name. A rescue run exists to
# create a first glossary cheaply; walking a 400-episode show at ~37 s each is the sweep
# this bound prevents. Overridable per run with --bootstrap-limit.
DEFAULT_BOOTSTRAP_LIMIT = 12
# Lowercase-share gate (see admit): ordinary words show up lowercase in the same text.
# Measured over five shows: real names sit at 0.0, the highest real word-name is Merry at 0.10;
# plain junk runs 0.62 (School) to 0.95 (Life). 0.6 does NOT clear every ordinary word: Triple .55,
# Door .56, Mayor .54, Editor .52, Pirates .51, Knight/Giant/Dose/Quick .50 stay admitted on purpose
# (Door is half of "Heaven's Door", Mayor may be a name). Series terms Stand/Fruit/Straw/Sword are
# dropped by design. Guard: at least 3 lowercase sightings.
MAX_LOWER_SHARE = 0.6
MIN_LOWER_EVIDENCE = 3
MIN_SHARE_FLOOR = 0.3  # a lower MINE_MAX_LOWER_SHARE would drop real names (Heaven .25): ignored
SKIP_FILE_RE = re.compile(r"\bNC(ED|OP|BD)\b|-\s*scene\b|creditless", re.I)
# words that are capitalized for position/grammar, not proper nouns — never mine these.
# V2 C8: sourced from data/common_proper_noun_deny.txt (one word per line, # comments
# allowed) so the deny-list can be tuned without a code change; falls back to this exact
# inline set if the file is missing/unreadable (backward-compatible).
_COMMON_FALLBACK = """the a an and or but of to in on at is was are were be been being have has had do does did
will would can could should shall may might must i you he she it we they me him her us them my your his
her its our their this that these those with from for not no nor yes all out up down here there then now
what who whom whose when where why how which while because if so as than too very just only even still
oh ah hey hmm well yeah yes okay ok hello goodbye please thank thanks sorry sir madam mister missus doctor
get got go going gone come came see saw seen know knew known think thought say said tell told want need
make made take took give gave find found let look looked good bad great little big small new old one two
three four five six seven eight nine ten first last next every some any many much more most right left
wait stop help yes mom dad mother father brother sister friend everyone someone something nothing
today tomorrow yesterday day night morning time year hand way thing people man woman boy girl
master lady lord king queen captain general doctor mister""".split()


def _load_common(path: str = "data/common_proper_noun_deny.txt") -> set:
    try:
        with open(path, encoding="utf-8") as f:
            words = {ln.strip().lower() for ln in f if ln.strip() and not ln.startswith("#")}
    except OSError:
        words = set()
    return words or set(_COMMON_FALLBACK)


COMMON = _load_common()


# A backstop against a wedged ffmpeg, not a budget. 120s was not enough: the miner walks
# every episode of a show back to back over a network mount, and a cold or contended pass is
# far slower than any single warm measurement suggests. Tunable like MINE_MIN_COUNT.
EXTRACT_TIMEOUT = 600.0


def _extract_timeout() -> float:
    """Read at CALL time so the env var can be set for one run without a restart."""
    try:
        return float(os.environ.get("MINE_EXTRACT_TIMEOUT", EXTRACT_TIMEOUT))
    except ValueError:
        return EXTRACT_TIMEOUT


def _episode_timeout() -> float:
    """MINE_EPISODE_TIMEOUT (default 180s): how long one episode may take before it is skipped."""
    try:
        return float(os.environ.get("MINE_EPISODE_TIMEOUT", "180"))
    except ValueError:
        return 180.0


@contextlib.contextmanager
def _deadline(seconds: float, what: str):
    """Hard-abort one episode's work after ``seconds``.

    SIGALRM interrupts blocking syscalls, so a wedged ffmpeg, a stalled media mount or a
    wait on a lock cannot stop the sweep. Observed 2026-11-17: a mine run sat 17 minutes
    at 0% CPU holding the glossary lock, the queued run never reached generation, and
    nothing in the log said which episode it was on. A skip is a per-episode loss; a
    wedge is the whole queue.
    """
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _boom(_signum, _frame):
        raise TimeoutError(f"{what} exceeded {seconds:.0f}s")

    previous = signal.signal(signal.SIGALRM, _boom)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def eng_sub_text(video):
    """Return plaintext of the video's English (or und) ASS/SSA/SRT subtitle, or ''.

    Our own previously-muxed dubtitle (common.is_our_track) is excluded: this selector
    bypasses common.eng_sub_streams(), so it needs the same guard, otherwise a
    regeneration would re-mine last version's spellings out of its own output and
    reinforce its errors into the glossary. No fallback — a file whose only English sub
    is our dubtitle mines nothing."""
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_entries",
                "stream=index,codec_name:stream_tags=language,title",
                "-of",
                "json",
                # NO -nostdin. That is an ffmpeg option; ffprobe has no such flag and takes
                # the NEXT argument -- the video path -- as its value, then exits 1 with an
                # empty stdout. json.loads("") raised straight into the handler below, so
                # this returned "" for every file on every show, and the miner's summary
                # ("0 new ep(s), no new terms") read exactly like a normal no-op. Found
                # 2026-08-28 on Sword Art Online: two English fansub tracks per episode,
                # nothing mined, no glossary -- and with no glossary decisions.show_for
                # returns "", so the show gets no decision store and the review page refuses
                # every verdict. stdin=DEVNULL below is what actually keeps ffprobe off the
                # terminal; the flag was never doing that job.
                video,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        streams = json.loads(r.stdout).get("streams", [])
    except Exception as exc:
        # SAID, not swallowed. "" is the right answer for a release with no fansub track,
        # and that is common -- but a broken probe must not be indistinguishable from it.
        # Silence is how a wrong argument list ran against the whole library unnoticed.
        print(f"mine: could not read subtitle streams from {video}: {exc}")
        return ""
    cand = [
        s
        for s in streams
        if ((s.get("tags") or {}).get("language") or "").lower() in ("eng", "en", "und", "")
        and s.get("codec_name") in ("ass", "ssa", "subrip")
        and not is_our_track(stream_title(s))
        # A SIGNS/SONGS track is not a script, and the first English track is not
        # necessarily the dialogue one. Measured on Sword Art Online E01: track 2 is
        # "S&S English" -- 3,233 events, 2,142 of them Opening-Romaji karaoke, plus signs,
        # UI text and "Next Episode" cards, and almost no dialogue -- while the real script
        # is track 3. Mining the first one admitted 418 "names" including Above, Again,
        # Address and Episode, which then became words the Whisper prompt was told to spell
        # correctly. SIGNS_TITLE is mux's own test for this, reused so the two cannot
        # disagree; it matches "S&S" and "Forced" alike.
        and not SIGNS_TITLE.search(stream_title(s) or "")
    ]
    # No fallback to a signs track, for the same reason there is none to our own dubtitle:
    # junk in the glossary becomes junk in the prompt and junk in glossary.correct's name
    # list, applied to every future episode. Mining nothing is the better failure.
    if not cand:
        return ""
    idx = cand[0]["index"]
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "s.ass")
        try:
            # NO `-c:s copy`, deliberately, and NOT common.extract_sub which tries that
            # first. Measured 2026-08-28 on one 352MB AV1 episode over the media mount:
            # `-c:s copy` took 51.7s and the plain path 0.8s, for a byte-identical 586,559
            # byte result. 60x, the wrong way round, on the shape of file this walks 25 of.
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-nostdin", "-i", video, "-map", f"0:{idx}", out],
                capture_output=True,
                timeout=_extract_timeout(),
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:
            # A per-episode failure is a per-episode loss. This call was outside any try, so
            # a TimeoutExpired propagated out of main() and killed the whole run -- throwing
            # away every term mined from the episodes before it (E11 of Sword Art Online,
            # 2026-08-28, taking the ten already mined with it). Reported for the same reason
            # the probe failure is: a show that mines nothing and a show that could not be
            # read must not look the same.
            print(f"mine: could not extract subtitles from {video}: {exc}")
            return ""
        if not os.path.exists(out):
            return ""
        try:
            subs = pysubs2.load(out)
        except Exception:
            return ""
        # Skipping the furniture INSIDE the track, not just the wrong track. See
        # common.NON_DIALOGUE_STYLE for the measurement and for why this is a heuristic.
        return "\n".join(ev.plaintext for ev in subs if not ev.is_comment and not NON_DIALOGUE_STYLE.search(ev.style or ""))


REVIEW_REASON = "possessive_floor_crossing"
# A trailing possessive: ASCII apostrophe or U+2019, then "s". chr(0x2019) is BUILT, never
# typed as a literal -- a curly apostrophe pasted into this repo's source has been silently
# normalised to ASCII before, which disables the guard while leaving a passing-looking test.
_POSS_RE = re.compile("['" + chr(0x2019) + "]s$")


def _fold(w):
    """Strip a trailing possessive 's / U+2019s. Every other word comes back unchanged,
    including internal-apostrophe names (Kin'emon, D'Arby) and a bare trailing quote."""
    return _POSS_RE.sub("", w)


_announced = set()  # overrides already printed this process: one line each, never per word


def _env_num(name, default, cast, low, high):
    """Read at CALL time so the env var can be set for one run without a restart. A value that
    does not parse, is not finite, or falls outside [low, high] gives the default. The first
    time a non-default value is in effect, one line says so (a typo'd override that silently
    changes what is admitted is otherwise invisible in the logs)."""
    try:
        v = cast(os.environ.get(name, default))
        ok = math.isfinite(v) and low <= v <= high
    except (ValueError, TypeError, ArithmeticError):  # ArithmeticError: OverflowError on a huge int
        return default
    if not ok:
        return default
    if v != default and (name, v) not in _announced:
        _announced.add((name, v))
        print(f"mine: {name}={v} (default {default})")
    return v


def mine_text(text, bare, poss, midsentence, forms=None, lower=None):
    """Count capitalized proper-noun candidates into TWO lanes and track which appear
    MID-sentence (not just sentence-initial, where any word is capitalized).

    `bare` counts the plain form, `poss` the possessive one. Before D5 the ^[A-Z][a-z]{3,}$
    test ran against a core that still carried its `'s`, so `Brownbeard's`/`Vegapunk's` matched
    nothing and were counted as NEITHER form -- the evidence was split and then discarded.

    Possessives are never merged into `bare` and never added to `midsentence`. Both gates on
    the auto-append lane (the count floor and the mid-sentence requirement) therefore see
    exactly what they saw before this change, which is what makes the fold safe without an
    English-dictionary gate: possessive evidence can REINFORCE a candidate but can never
    ORIGINATE one. admit() reads the second lane.

    `forms` (optional) collects the SURFACE spellings behind each folded key --
    {folded: {surface: count}} -- so a consumer can show a reviewer the evidence a term
    was escalated on rather than a bare number. Purely observational: no admission rule
    reads it, and it exists here rather than in the caller so there is exactly one
    tokeniser and one fold in the repo (D3a needs the forms; task 13).

    `lower` (optional) counts the all-lowercase tokens the candidate regex would accept in
    capitalised form ({"like": n}) -- the evidence admit() uses to tell an ordinary word
    ("Like") from a name ("Zoro", which the text never writes lowercase). Observational
    only, and off by default."""
    for sent in re.split(r"[.!?…]+|\n", text):
        words = re.findall(r"[A-Za-z][A-Za-z'’\-]{2,}", sent)
        for i, w in enumerate(words):
            core = w.strip("'’-")
            folded = _fold(core)
            if not re.match(r"^[A-Z][a-z]{3,}$", folded):
                if lower is not None and re.match(r"^[a-z]{4,}$", folded):  # "school's" counts for "school"
                    lower[folded] = lower.get(folded, 0) + 1
                continue
            if forms is not None:
                seen = forms.setdefault(folded, {})
                seen[core] = seen.get(core, 0) + 1
            if folded != core:  # possessive: reinforces, never originates
                poss[folded] = poss.get(folded, 0) + 1
                continue
            bare[core] = bare.get(core, 0) + 1
            if i > 0:  # not the first word of the sentence
                midsentence.add(core)


def admit(
    bare, poss, midsentence, min_count=None, common=None, existing: Collection = frozenset(), lower=None, dropped=None
):
    """Two-lane admission -> (new_names, review_queue).

        bare >= min_count                              -> auto-append (unchanged behaviour)
        bare <  min_count and bare + poss >= min_count  -> review queue, REVIEW_REASON

    Possessive evidence may raise a candidate into VISIBILITY; it may never raise one into
    the glossary unattended. Deliberately NO English-dictionary gate on this path: 13 of this
    show's 81 glossary names are dictionary words (Brook, Robin and Chopper are Straw Hats),
    so a gate would make 16% of the cast permanently unmineable -- trading a false positive
    for a systematic false negative on the names that matter most.

    The mid-sentence requirement gates BOTH lanes, as it always has: without it every
    sentence-initial word would queue for review.

    `lower` (from mine_text) adds the lowercase-share gate on BOTH lanes: a candidate whose
    lowercase form is lower / (lower + bare + possessive) >= MINE_MAX_LOWER_SHARE, with at least
    MINE_MIN_LOWER_EVIDENCE lowercase sightings, is an ordinary word and is skipped (and
    appended to `dropped` when given). This is evidence from the text, not a dictionary:
    Brook is kept as long as the text only ever writes it capitalised."""
    min_count = MIN_COUNT if min_count is None else min_count
    common = COMMON if common is None else common
    new, queue = [], {}
    max_share = _env_num("MINE_MAX_LOWER_SHARE", MAX_LOWER_SHARE, float, MIN_SHARE_FLOOR, 1)
    min_evidence = _env_num("MINE_MIN_LOWER_EVIDENCE", MIN_LOWER_EVIDENCE, int, 1, math.inf)
    for t in sorted(midsentence):
        if t.lower() in common or t.lower() in existing:
            continue
        b, p = bare.get(t, 0), poss.get(t, 0)
        if b + p < min_count:
            continue
        lo = lower.get(t.lower(), 0) if lower else 0
        if lo >= min_evidence and lo / (lo + b + p) >= max_share:
            if dropped is not None:
                dropped.append(t)
            continue
        if b >= min_count:
            new.append(t)
        else:
            queue[t] = {"reason": REVIEW_REASON, "bare": b, "possessive": p}
    return new, queue


def mine(text, min_count=None, common=None, existing: Collection = frozenset()):
    """mine_text + admit over one block of text -> (new_names, review_queue)."""
    bare, poss, mid = {}, {}, set()
    lower = {}
    mine_text(text, bare, poss, mid, lower=lower)
    return admit(bare, poss, mid, min_count, common, existing, lower)


def queue_for_review(cfg, queue):
    """Record the crossing lane in the glossary's `flagged` review queue -> newly-added terms.

    Never overwrites an entry another producer (glossary_verify, glossary_acquire, or a human
    decision) wrote for the same term: the crossing lane is the weakest evidence in the file
    and must not displace a stronger reason already in front of a reviewer. Its own entries
    are refreshed, since each sweep re-counts only the not-yet-dubtitled episodes."""
    flagged = cfg.get("flagged") or {}
    added = []
    for term, meta in queue.items():
        cur = flagged.get(term)
        if isinstance(cur, str):
            cur = {"reason": cur}
        if cur and cur.get("reason") != REVIEW_REASON:
            continue
        if cur is None:
            added.append(term)
        flagged[term] = meta
    if flagged:
        cfg["flagged"] = flagged
    return sorted(added)


def _bootstrap_limit(explicit: int | str | None = None) -> int:
    """How many episodes a bootstrap may read: the flag's value, else DEFAULT_BOOTSTRAP_LIMIT.
    A value that is not positive gives the default -- an unbounded bootstrap is the one
    failure this bound exists to prevent."""
    if explicit is None:
        return DEFAULT_BOOTSTRAP_LIMIT
    try:
        value = int(explicit)
    except (TypeError, ValueError):
        return DEFAULT_BOOTSTRAP_LIMIT
    return value if value > 0 else DEFAULT_BOOTSTRAP_LIMIT


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="mine_glossary.py",
        description="ADDITIVE glossary mining for one show: append recurring proper nouns from "
        "the embedded English subtitles of its not-yet-dubtitled episodes.",
    )
    parser.add_argument("show_dir")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="create the FIRST glossary for a show that was delivered without one: mines "
        "delivered episodes too. Refused when the show already has a glossary.",
    )
    parser.add_argument(
        "--bootstrap-limit",
        type=int,
        default=None,
        help=f"most recent episodes a bootstrap may read (default {DEFAULT_BOOTSTRAP_LIMIT})",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    show_dir = args.show_dir.rstrip("/")
    show = os.path.basename(show_dir)
    gpath = os.path.join(GLOSS_DIR, show + ".json")
    from dubtitlerr.glossary import glossary_lock

    with glossary_lock.locked(gpath, "mine") as ok:  # the whole read-count-write: a busy glossary skips this sweep's mining
        if not ok:
            return 0
        if args.bootstrap and os.path.exists(gpath):
            # The gate, checked inside the lock so a concurrent writer cannot slip between the
            # test and the read. Bootstrap exists to create the first glossary; on a show that
            # has one, the ordinary window (new episodes only) is what should apply.
            print(
                f"mine[{show}]: bootstrap refused: {gpath} already exists.\n"
                "  Bootstrap only creates the FIRST glossary; this show has one, so its "
                "normal mining window (new episodes only) applies. Nothing was read or written."
            )
            return 2
        _mine(show_dir, show, gpath, bootstrap=args.bootstrap, bootstrap_limit=_bootstrap_limit(args.bootstrap_limit))
    return 0


def _mine(show_dir, show, gpath, bootstrap=False, bootstrap_limit=DEFAULT_BOOTSTRAP_LIMIT):
    cfg = {"show": show, "initial_prompt": "", "names": [], "hard_fixes": {}}
    if os.path.exists(gpath):
        try:
            cfg.update(json.load(open(gpath)))
        except Exception as e:
            print("mine: bad glossary, starting fresh:", e)
    existing = {n.lower() for n in cfg.get("names", [])}

    counter, poss, mid, lower = {}, {}, set(), {}
    mined_eps = 0
    read_eps = 0
    # A request's scope (gen_loop passes the request's GENERATE_ONLY_RE): mine only the episodes
    # the run is about. Measured 2026-11-18: a one-season request mined 38 episodes to serve 28.
    # An invalid pattern mines nothing -- generate.py refuses it too, and the whole show would be
    # exactly the out-of-scope work this exists to prevent.
    only = os.environ.get("GENERATE_ONLY_RE", "")
    try:
        only_rx = re.compile(only) if only else None
    except re.error as e:
        print(f"mine[{show}]: invalid GENERATE_ONLY_RE {only!r} ({e}); mining nothing")
        return

    episodes = []
    for dp, dns, fs in os.walk(show_dir):
        dns[:] = [d for d in dns if d.lower() not in EXTRA_DIRS]
        for fn in fs:
            if not fn.lower().endswith((".mkv", ".mp4")) or SKIP_FILE_RE.search(fn):
                continue
            if only_rx is not None and not only_rx.search(fn):
                continue
            episodes.append(os.path.join(dp, fn))
    if bootstrap:
        # "Most recent" = last by path name, which is episode order for the SxxExx naming every
        # media server uses (and season order across the season folders). Newest-first, because
        # the bound counts episodes that actually carry something to mine: DAN DA DAN's newest
        # 12 files (all of S2) hold no English subtitle track at all, and a plain "last 12
        # files" bound would have mined nothing while looking like a completed run -- measured
        # 2026-11-19. A file with no mineable track costs one ffprobe and no extraction.
        # Sorted ONLY here: the ordinary path keeps os.walk's order untouched, so a bootstrap
        # run cannot change what a normal sweep admits.
        episodes.sort(reverse=True)
        print(
            f"mine[{show}]: BOOTSTRAP -- no glossary for this show, mining delivered episodes too "
            f"(up to {bootstrap_limit} that carry an English subtitle track)"
        )

    for path in episodes:
        if bootstrap and mined_eps >= bootstrap_limit:
            print(f"mine[{show}]: bootstrap bound reached ({bootstrap_limit} mineable episode(s))")
            break
        fn = os.path.basename(path)
        stem = os.path.splitext(path)[0]
        # only NEW episodes -> each episode mined exactly once, additively. "Not new" is a
        # pipeline transcript OR a delivery stamp: delivery removes the transcript (mux deletes
        # it, sidecar mode renames it to SIDECAR_SUFFIX), so without the stamp check every
        # delivered episode read as new and was re-mined on every run.
        #
        # BOOTSTRAP is the single gated exception: a show generated with no glossary at all has
        # nothing to re-mine INTO (no file exists), so the loop this skip prevents cannot start.
        # The gate is the refusal in main() -- with a glossary present, --bootstrap exits 2 --
        # so this branch is only ever reached for a show that has none.
        if not bootstrap and any(
            os.path.exists(stem + suf) for suf in (".eng.dubtitles.ass", ".eng.dubtitles.srt", STAMP_SUFFIX)
        ):
            continue
        # Soft stop (SIGTERM at window close). Between episodes is the safe point: nothing
        # is persisted until the whole walk is counted, so leave WITHOUT writing -- a
        # glossary admitted from half the episodes would apply MIN_COUNT to a partial
        # tally. The unmined episodes still have no dubtitle, so the next sweep re-reads them.
        if stop_requested():
            print(f"stop requested: leaving mine after {read_eps} episodes")
            return
        read_eps += 1
        print(f"mine[{show}]: {read_eps} {fn}", flush=True)
        try:
            with _deadline(_episode_timeout(), f"mining {fn}"):
                txt = eng_sub_text(path)
        except TimeoutError as exc:
            print(f"mine[{show}]: skipped {fn}: {exc}", flush=True)
            continue
        if txt:
            mine_text(txt, counter, poss, mid, lower=lower)
            mined_eps += 1

    dropped = []
    new, queue = admit(counter, poss, mid, MIN_COUNT, COMMON, existing, lower, dropped)
    if dropped:
        print(f"mine[{show}]: lowercase gate dropped {len(dropped)} ordinary word(s) -> {dropped[:15]}")
    queued = queue_for_review(cfg, queue)  # D5: the crossing lane goes to a human, never auto-appended
    if cfg.get("flagged"):
        # P32: resolve the crossing lane by policy before writing -- a durable verdict (or an
        # already-canonical term) must not reach the queue. Fail-open: a policy error leaves
        # the flags in place, which is the safe direction (the queue is bounded by P33).
        try:
            from dubtitlerr.glossary import review_policy

            review_policy.apply_policy(cfg, gpath if os.path.exists(gpath) else None)
        except Exception as e:  # noqa: BLE001 -- never lose a queued term to a policy crash
            print(f"mine[{show}]: review policy skipped: {type(e).__name__}: {e}")
        still = cfg.get("flagged") or {}
        queued = [t for t in queued if any(str(k).lower() == t.lower() for k in still)]
    if not new and not queued:
        print(f"mine[{show}]: {mined_eps} new ep(s), no new terms (dict has {len(existing)})")
        return
    cfg["names"] = cfg.get("names", []) + new
    # build a prompt from the top names only if there is no curated one
    if not cfg.get("initial_prompt"):
        top = sorted(cfg["names"], key=lambda n: -counter.get(n, 0))[:30]
        title = re.sub(r"\s*\{tvdb-\d+\}|\s*\(\d{4}\)", "", show)
        cfg["initial_prompt"] = f"This is {title}, a Japanese anime (English dub). Spell names correctly: " + ", ".join(top) + "."
    os.makedirs(GLOSS_DIR, exist_ok=True)
    with open(gpath, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")  # POSIX line: prettier flags a glossary without it
    review = f" | {len(queued)} queued for review: {queued[:5]}" if queued else ""
    print(f"mine[{show}]: +{len(new)} terms from {mined_eps} new ep(s) -> {new[:15]}{review}")


if __name__ == "__main__":
    sys.exit(main())
