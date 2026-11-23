#!/usr/bin/env python3
"""Per-episode UNRESOLVED queue — the human rung of the deterministic → LLM → human ladder,
for the subtitle path.

`glossary_acquire.py` already implements that ladder properly: candidates the deterministic
gates cannot settle become `flagged`, and `--review` walks them with their evidence attached.
The subtitle path had no equivalent. Repair increments `skipped_no_ref` and `rejected` and
continues; punctuation restoration records `restore_empty` and leaves the old text in place.
Each is a *safe* fallback, and each is invisible: `common.llm_chat()` returns "" on every
transport failure, so a dead endpoint produces a clean-looking run with a slightly higher
counter. The actual ladder was:

    rules -> bounded LLM -> keep old text / increment a counter

This module supplies the missing arrow. It records WHAT could not be settled, WHY, and the
evidence a human needs to settle it — so a model refusal, a dead endpoint and a missing
reference stop being the same silent no-op.

It also records what WAS settled without anyone checking the meaning. `repair.accept_repair`
states the acceptance bar in its own docstring and then says plainly that nothing below it
enforces that: `factory -> needle` and `VIVRA card -> Vivi card` both pass every gate. An
accepted repair is therefore a decision no code has checked, and the `repair_applied` stage
is where it waits for someone who can. Measured over 45 of them (2026-08-27): 4 outright
regressions and 5 more needing correction -- 36 of 45 clean, none of the nine reachable by
any check in this pipeline.

PER-STAGE, not one flat queue: the triage action differs by stage. A repair rejection needs
the fansub reference checked; a punctuation failure needs the run read; they are not
interchangeable, and a flat queue destroys the signal that says which to do.

Deliberately NOT covered yet: the hallucination `flag`. `maybe_silence` fires on 67% of real
cards (measured over 20,292), so queuing it would bury the operator in ~13,000 entries per
20,000 cards. A queue nobody can face is worse than no queue.

That deferral is tied to a specific future item: the liveness counters showed `music` AND
`repetition` both activate ZERO times, while `maybe_silence` activates on two thirds of
everything. All three are hallucination-gate rules that need re-thresholding together, and
the queue's hallucination stage should be added in that same pass — the right threshold is
what decides whether the entries are worth a human's attention at all.

Contract, mirroring qc.write: this is OBSERVABILITY. It must never raise and never fail an
episode that otherwise generated correctly. Every entry point returns a bool.
"""

import json
import os
import re
import time

from dubtitlerr.core import artifact_store, decisions, queue_policy
from dubtitlerr.core.common import out_for  # conf reads only; the conf kind moves to the store in P39-B2

# Imported as a NAME so the CLI resolves a show exactly the way the store and the mux gate
# do. decisions.py imports stdlib plus common, and common imports nothing from this repo
# (it is the leaf), so this cannot cycle back through here.
from dubtitlerr.core.decisions import show_for

# Inner artifacts of the review queue (P39). They used to be three files beside the media --
# ``.dubtitles.unresolved.jsonl``, ``.dubtitles.unresolved.counters.json`` and
# ``.dubtitles.unresolved.audit.jsonl`` -- and now live only in the central artifact store,
# keyed by episode stem. One JSON object per line for the queue and its audit, so the
# ``items()`` contract and the P33 bound are exactly what they were.
KIND = "unresolved"
COUNTER_KIND = "unresolved.counters"
AUDIT_KIND = "unresolved.audit"
SAMPLE_CAP = queue_policy.SAMPLE_CAP
QUEUE_MAX = queue_policy.QUEUE_MAX
EXPIRY_DAYS = queue_policy.EXPIRY_DAYS
# A reason a HUMAN owes stays in the log whatever its class volume: it is the reviewer's own
# verdict coming back refused, and sampling it away would hide exactly the loss it records.
ALWAYS_EVIDENCE = frozenset(
    {("repair", "decision_unfittable"), ("repair", "verdict_unfittable"), ("repair", "verdict_owed")}
)

# Reasons, per stage. Kept as a module constant so the --review CLI and the call sites cannot
# drift apart, and so a typo'd reason is visible rather than silently creating a new bucket.
REASONS = {
    "repair": (
        "no_reference",  # no fansub anchor overlapped this card's source window
        "rejected_guard",  # the model proposed an edit; accept_repair() refused it
        "rejected_name_invented",  # the model substituted a proper noun that is in
        # neither the glossary nor the original -- the phonetic name guard refused it
        "rejected_name_part",  # the repair added or removed a part of a multi-word glossary
        # name the line already had a piece of (REPAIR_KEEP_NAME_PARTS, on unless set to 0)
        "decision_unfittable",  # [S-4] a HUMAN verdict whose text cannot be displayed on
        # this card. C1 holds timing immutable, so the ASR text stands -- and the reviewer
        # is told, because a decision that disappears silently is the failure this whole
        # loop exists to prevent. The only reason here that is about the human, not the model.
        "llm_empty",
    ),  # the backend returned nothing (transport failure or timeout)
    "punctuation": (
        "llm_empty",  # ditto -- a dead endpoint looks exactly like "no change"
        "rejected_guard",
    ),  # accept_restoration() found the model rewrote words
    "repair_applied": ("accepted",),  # accept_repair ADMITTED this repair -- see the docstring
}

# The queue a reviewer opens by default. Keyed on (stage, reason), not reason alone, because
# "rejected_guard" belongs to BOTH `repair` and `punctuation`.
#
# Two different reasons for what is left out, and they must not be confused:
#
#   NOT ACTIONABLE per line. `no_reference` is mostly "this release has no fansub" -- true,
#   and one fact about the release rather than N facts about N cards. `llm_empty` is a dead
#   endpoint, likewise one fact about the run. Measured: ~25 primary entries per episode
#   against ~86 recorded, and this module already refuses to queue the hallucination flag on
#   the same grounds -- "a queue nobody can face is worse than no queue."
#
#   NO VERDICT TO GIVE. `punctuation`/`rejected_guard` records `original_text` AND
#   `proposed_text` (punctuation.py:290-296) -- the identical two-text shape as the repair
#   rejection that IS included, and just as judgeable by reading them. An earlier version of
#   this comment claimed otherwise; that was false and an adversarial review caught it. The
#   real bar is that a reviewer's verdict has nowhere to go. accept_restoration() is not a
#   judgement gate like accept_repair(): it is word-identity (punctuation.py:134-144), so a
#   rejection means the model CHANGED A WORD, and applying it anyway breaks _apply()'s stated
#   precondition that "past the guard the correspondence is exact" -- tokens would misalign
#   across the run. Of the four verdicts only `reject` is implementable; `accept`/`force`
#   are not, and `correct` only for text of identical token count. Restoration also runs on
#   the word list BEFORE reflow (generate.py:944-951) while repair runs on cards after it,
#   so [S-5]'s card write-back cannot apply a punctuation decision at all -- that needs a
#   TEXT-tier replay.
#
#   OWNER DECISION 2026-08-27: widening waits until there is a way to ACT on a punctuation
#   review, not merely display one. Until then those entries stay reachable through the
#   unfiltered walk and the --review CLI. The per-episode volume has never been measured;
#   the only sample is the 7 live rejections behind _split_dashes (punctuation.py:80-85),
#   3 of which were false rejections from a guard bug -- found from the QC event channel,
#   which already carries both texts, not from this queue.
PRIMARY = (
    ("repair_applied", "accepted"),
    ("repair", "rejected_guard"),
    ("repair", "rejected_name_invented"),
    ("repair", "rejected_name_part"),
    # [S-4]. In PRIMARY because it is the reviewer's OWN decision coming back refused: left
    # out of the default view, their verdict would vanish silently, which is precisely what
    # the entry was written to prevent. Unlike the punctuation entries held out above, this
    # one is actionable per line -- the answer is shorter text.
    ("repair", "decision_unfittable"),
)


def items(stem: str) -> list:
    """Every entry, resolved and pending. Returns [] rather than raising.

    JSONL, one entry per line, held in the artifact store. A corrupt line is skipped, so a
    torn or hand-edited payload costs that entry rather than the queue."""
    try:
        return artifact_store.read_jsonl(stem, KIND)
    except Exception:
        # Observability, not a dependency: an unreadable store reads as an empty queue.
        return []


_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


def _load_counters(stem: str) -> dict:
    try:
        doc = artifact_store.get_json(stem, COUNTER_KIND)
    except Exception:
        doc = None
    if isinstance(doc, dict):
        for key in ("counts", "days", "sampled", "sample_day"):
            if not isinstance(doc.get(key), dict):
                doc[key] = {}
        return doc
    # A missing or corrupt state doc falls through to the fresh document built here.
    return {"counts": {}, "days": {}, "sampled": {}, "sample_day": {}}


def _trim_counters(doc: dict, days: int = EXPIRY_DAYS + 1) -> None:
    """Keep only the last `days` days of per-day buckets; the lifetime totals stay."""
    keep = sorted(doc.get("days") or {})[-days:]
    doc["days"] = {d: doc["days"][d] for d in keep}
    keep_sd = sorted(doc.get("sample_day") or {})[-days:]
    doc["sample_day"] = {d: doc["sample_day"][d] for d in keep_sd}


def _save_counters(stem: str, doc: dict) -> bool:
    try:
        artifact_store.put_json(stem, COUNTER_KIND, doc)
        return True
    except Exception:
        # Same contract as the file writer: a counter that cannot be stored is a False
        # return, never an exception on an episode that otherwise generated.
        return False


def _count_and_sample(stem: str, stage: str, reason: str, day: str) -> tuple[bool, bool]:
    """Count every record; say whether THIS one keeps its evidence.

    A class is counted in the per-day and lifetime aggregate always. Evidence lines are
    sampled at ``SAMPLE_CAP`` per class per day, except for the reasons a human owes."""
    cls = f"{stage}/{reason}"
    doc = _load_counters(stem)
    doc["counts"][cls] = int(doc["counts"].get(cls) or 0) + 1
    day_counts = doc["days"].setdefault(day, {})
    day_counts[cls] = int(day_counts.get(cls) or 0) + 1
    kept = int(doc["sample_day"].setdefault(day, {}).get(cls) or 0)
    keep = (stage, reason) in ALWAYS_EVIDENCE or kept < SAMPLE_CAP
    if keep:
        doc["sample_day"][day][cls] = kept + 1
        doc["sampled"][cls] = int(doc["sampled"].get(cls) or 0) + 1
    _trim_counters(doc)
    return _save_counters(stem, doc), keep


def metrics(stem: str) -> dict:
    """Counters + bounded evidence, per day: how much the pipeline did, and how much drift.

    The point of the counters is that a 12,840-line log is no longer the only record of what
    happened. ``queue`` carries what is still open, so a page can show drift without reading
    the pile. Fails open: an absent stem is an empty report, never an exception."""
    doc = _load_counters(stem)
    entries = items(stem)
    open_items = [e for e in entries if not e.get("resolved") and not e.get("expired")]
    return {
        "counts": dict(sorted(doc.get("counts", {}).items())),
        "days": {d: dict(sorted(v.items())) for d, v in sorted(doc.get("days", {}).items())},
        "sampled": dict(sorted(doc.get("sampled", {}).items())),
        "queue": {
            "pending": len(open_items),
            "expired": sum(1 for e in entries if e.get("expired")),
            "evidence": len(entries),
            "by_class": queue_policy.counts(open_items),
        },
    }


def audit(stem: str, limit: int = 50) -> list:
    """Newest-first queue audit (expiry, undo), bounded by queue_policy.AUDIT_MAX."""
    try:
        return queue_policy.audit_records_read(artifact_store.read_jsonl(stem, AUDIT_KIND), limit)
    except Exception:
        return []


def _audit(stem: str, records: list) -> bool:
    """Append to the bounded audit ring in the store. Never raises."""
    records = [r for r in (records or []) if isinstance(r, dict)]
    if not records:
        return False
    try:
        existing = artifact_store.read_jsonl(stem, AUDIT_KIND)
        artifact_store.write_jsonl(stem, AUDIT_KIND, queue_policy.audit_records_append(existing, records))
        return True
    except Exception:
        return False


def expire(stem: str, now=None, days: float = EXPIRY_DAYS) -> dict:
    """Dismiss open entries older than ``days`` -- audited and reversible (``undo_expire``).

    Never deletes: the evidence stays in the log (marked) so the decision is inspectable and
    an undo can restore the prior state. Idempotent, and a no-op leaves the file untouched."""
    doc = items(stem)
    _kept, expired = queue_policy.expire(doc, now=now, days=days)
    if not expired:
        return {"expired": 0, "bounded": 0}
    # Index by identity, not ``list.index``: two entries can be equal dicts, and an audit record
    # that names the wrong duplicate would let an undo restore the wrong question.
    pos = {id(e): i for i, e in enumerate(doc)}
    _rewrite(stem, doc)
    at = time.time()
    _audit(
        stem,
        [
            queue_policy.audit_record(
                "expiry",
                index=pos[id(e)],
                stage=e.get("stage"),
                reason=e.get("reason"),
                days=e.get("expired_days"),
                original_text=str(e.get("original_text", ""))[:240],
                at=at,
            )
            for e in expired
        ],
    )
    return {"expired": len(expired), "bounded": 0}


def enforce(stem: str, now=None, days: float = EXPIRY_DAYS, limit: int = QUEUE_MAX) -> dict:
    """Expiry + hard cap in ONE rewrite: the queue a sweep leaves behind is bounded."""
    doc = items(stem)
    _kept, expired = queue_policy.expire(doc, now=now, days=days)
    _kept, bounded = queue_policy.bound(doc, now=now, limit=limit)
    bounded = [e for e in bounded if e not in expired]
    if not expired and not bounded:
        return {"expired": 0, "bounded": 0}
    pos = {id(e): i for i, e in enumerate(doc)}
    _rewrite(stem, doc)
    at = time.time()
    recs = [
        queue_policy.audit_record(
            kind,
            index=pos[id(e)],
            stage=e.get("stage"),
            reason=e.get("reason"),
            days=e.get("expired_days"),
            original_text=str(e.get("original_text", ""))[:240],
            at=at,
        )
        for kind, group in (("expiry", expired), ("queue-bound", bounded))
        for e in group
    ]
    _audit(stem, recs)
    return {"expired": len(expired), "bounded": len(bounded)}


def undo_expire(stem: str, index: int) -> bool:
    """Put one expired entry back in the queue and record the reversal.

    The entry gets a fresh timestamp: an undo is a decision, so the restored question deserves
    a full window before the age rule can retire it again."""
    doc = items(stem)
    if not (0 <= index < len(doc)):
        return False
    if not queue_policy.unexpire(doc[index]):
        return False
    doc[index]["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc[index]["expiry_undone_at"] = doc[index]["ts"]
    if not _rewrite(stem, doc):
        return False
    e = doc[index]
    _audit(
        stem,
        [
            queue_policy.audit_record(
                "undo-expiry",
                index=index,
                stage=e.get("stage"),
                reason=e.get("reason"),
                original_text=str(e.get("original_text", ""))[:240],
            )
        ],
    )
    return True


def _join_known_phrases(text: str, phrases) -> str:
    """``text`` with every occurrence of a listed multi-word phrase written as one word ('Van der Decken' -> 'vanderdecken'), so a
    re-spacing of a known phrase ('Vanderdecken' <-> 'Van der Decken') is the same word on both sides."""
    for phrase in sorted(phrases or (), key=len, reverse=True):
        words = _WORD.findall(phrase.replace("\u2019", "'").lower())
        if len(words) > 1:
            text = re.sub(r"(?<!\w)" + r"[\W_]+".join(map(re.escape, words)) + r"(?!\w)", "".join(words), text)
    return text


def word_changes(original: str, proposed: str, phrases=()) -> int:
    """How many words a proposal changed: words added plus words dropped, as a multiset difference over lower-cased words with
    punctuation stripped. ONE definition for every caller (repair, punctuation, the tests): case and punctuation alone are 0;
    letters with accents are letters; a curly apostrophe is a straight one; order does not count. A respelled or split word is a
    drop plus its additions ('Mobydick' -> 'Moby Dick' is 3), unless ``phrases`` lists 'Moby Dick': re-spacing a KNOWN phrase is
    0, because it is the correction the glossary asks for, not a large rewrite."""
    import collections

    def words(text: str) -> collections.Counter:
        text = _join_known_phrases((text or "").replace("\u2019", "'").lower(), phrases)
        return collections.Counter(_WORD.findall(text))

    a, b = words(original), words(proposed)
    return sum((b - a).values()) + sum((a - b).values())


_WARNED_THRESHOLDS: set = set()
DEFAULT_DISMISS_WORDS = 6


def auto_dismiss_threshold():
    """REPAIR_AUTO_DISMISS_WORDS as an int, or None (off). Unset or empty: DEFAULT_DISMISS_WORDS (6). 'off' (any case) turns it
    off. Any other value that is not a whole number >= 0 is off too, and one that is set but unusable ('abc', '4.0', '-1') is
    logged once so it is not mistaken for 'on'. 0 means any word change dismisses."""
    raw = (os.environ.get("REPAIR_AUTO_DISMISS_WORDS") or "").strip()
    if not raw:
        return DEFAULT_DISMISS_WORDS
    if raw.lower() == "off":
        return None
    if raw.isdecimal():
        return int(raw)
    if raw and raw not in _WARNED_THRESHOLDS:
        _WARNED_THRESHOLDS.add(raw)
        print(f"  REPAIR_AUTO_DISMISS_WORDS={raw!r} is not a whole number >= 0: auto-dismiss is OFF", flush=True)
    return None


def dismissal(original: str, proposed: str, phrases=()) -> dict:
    """The fields that mark a rejected proposal as dismissed from the review queue: {} while the rule is off or the change is not
    larger than the threshold. Spread into the record() call, so the entry is ALWAYS written (the audit trail), only marked."""
    limit = auto_dismiss_threshold()
    if limit is None:
        return {}
    n = word_changes(original, proposed, phrases)
    return {"dismissed": "large_change", "changes": n} if n > limit else {}


DISMISSIBLE = ("rejected_guard", "rejected_name_invented", "rejected_name_part")


def _is_dismissed(entry: dict) -> bool:
    """Only a rejected proposal can be dismissed as a large change: a decision the human owes (decision_unfittable, a stale
    verdict) stays visible whatever fields it carries."""
    return bool(entry.get("dismissed")) and entry.get("reason") in DISMISSIBLE


def pending(stem: str, primary_only: bool = False) -> list:
    """Unresolved entries; with ``primary_only``, only those worth a human's attention (and not dismissed as a large change).

    The filter lives here, not in the caller, so the review UI and the --review CLI cannot
    disagree about what "the queue" means -- the same reason REASONS is a module constant.

    P33: the age rule is applied HERE too, not only when a sweep persists it. An entry past
    ``EXPIRY_DAYS`` is no longer a question -- the sweep marks it expired and audits it, and
    until that sweep runs the page must not present it as pending. Applying the rule at read
    keeps the queue bounded even between sweeps, without a write on a GET route."""
    out = [e for e in items(stem) if not e.get("resolved") and not e.get("expired")]
    out = [e for e in out if (queue_policy.age_days(e) or 0.0) <= EXPIRY_DAYS]
    if primary_only:
        out = [e for e in out if (e.get("stage"), e.get("reason")) in PRIMARY and not _is_dismissed(e)]
    return out


CONF_SUFFIX = ".dubtitles.conf.json"


def live_only(stem: str, entries: list) -> list:
    """The entries still describing a line this episode actually contains.

    A re-transcription replaces conf.json, and every queue entry written against the old
    text is then about something that no longer exists: nothing will re-queue it, so nothing
    will ever resolve it. Measured on the live library 2026-08-27 -- 6,364 One Pace entries
    orphaned this way, against 0 that still matched.

    FAILS OPEN, and deliberately opposite to the mux gate. Without conf.json neither can
    tell an orphan from a live entry, and there the cost of guessing wrong is shipping an
    unreviewed repair, so it holds everything; here the cost is hiding a question a human
    could have answered, so it shows everything. Same ignorance, opposite safe direction.

    Shared with mux.held_for_review rather than reimplemented -- two answers to "is this
    entry still live" would drift, and the drift would be invisible until a reviewer was
    shown a queue the gate disagreed with."""
    try:
        with open(out_for(stem + CONF_SUFFIX), encoding="utf-8") as f:
            live = {decisions.key(c.get("text", "")) for c in json.load(f) if isinstance(c, dict)}
    except (OSError, ValueError, TypeError):
        return list(entries)
    return [e for e in entries if decisions.key(e.get("original_text", "")) in live]


def card_starts(stem: str) -> dict:
    """Every line this episode contains, mapped to the card start times it appears at.

    A queue entry carries no timing: repair.py records original_text and proposed_text, and
    an accepted repair records neither source_start nor source_end. So the timestamp a
    reviewer needs to scrub to the line and hear it is DERIVED here from conf.json rather
    than stored -- which is why the 682 entries already queued get one with no backfill, and
    why a re-transcription cannot leave a stale time behind.

    A LIST per line, not one time. The same sentence can be on several cards, and repair.py's
    duplicate-pair suppression makes those ONE queue entry -- so the reviewer is owed all of
    the places it is, not an arbitrary first.

    CARD start, not source_start: the reviewer is seeking to a card they will read on screen,
    not to the window the anchor logic searched.

    Keyed on decisions.key, the same identity live_only matches on, so the two cannot
    disagree about which entry describes which card. Returns {} rather than raising."""
    out: dict = {}
    try:
        with open(out_for(stem + CONF_SUFFIX), encoding="utf-8") as f:
            cards = json.load(f)
        for c in cards:
            if not isinstance(c, dict):
                continue
            out.setdefault(decisions.key(c.get("text", "")), []).append(float(c.get("start", 0.0)))
    except (OSError, ValueError, TypeError):
        return {}
    return out


def card_context(stem: str, width: int = 1) -> dict:
    """Every line mapped to the cards either side of it, per occurrence.

    A queue entry shows ONE card's text, and a card is not a sentence: reflow splits on
    duration and line length, so a line routinely starts or ends mid-clause. A reviewer
    judging whether `factory -> needle` is right has to see where the sentence begins and
    ends, and the queue entry alone cannot tell them.

    Per OCCURRENCE, not per line, for card_starts' reason: the same sentence can be on
    several cards and repair.py's duplicate-pair suppression makes those ONE queue entry, so
    the reviewer is owed the context of each place it appears rather than an arbitrary
    first. `start` is carried on each occurrence so the page can pair context with the
    matching seek target without re-deriving the join.

    At the episode edges the missing side is EMPTY, never padded and never wrapped -- the
    first card showing the last card of the episode as its predecessor would be a confident
    lie about what the reviewer is reading.

    Keyed on decisions.key, the same identity card_starts and live_only use, so the three
    cannot disagree about which entry describes which card. Returns {} rather than raising:
    observability never fails an episode."""
    out: dict = {}
    try:
        with open(out_for(stem + CONF_SUFFIX), encoding="utf-8") as f:
            cards = json.load(f)
        rows = [c for c in cards if isinstance(c, dict)]
        texts = [str(c.get("text", "")) for c in rows]
        for i, c in enumerate(rows):
            out.setdefault(decisions.key(texts[i]), []).append(
                {
                    "start": float(c.get("start", 0.0)),
                    # max(0, ...) rather than i-width: a negative slice start reaches the END
                    # of the list, which is exactly the wrap this must not do.
                    "before": texts[max(0, i - width) : i],
                    "after": texts[i + 1 : i + 1 + width],
                }
            )
    except (OSError, ValueError, TypeError):
        return {}
    return out


def undecided(entries: list, store: dict) -> list:
    """The entries no stored verdict already settles.

    A line with a stored verdict IS settled, whatever its queue flag says: the verdict is
    what stops repair.py re-applying and re-queueing it, while `resolved` is only what a
    reviewer's own write set. mux.held_for_review has trusted the verdict over the flag
    since [S-6]; the review page never got the same rule, so a reviewer who judged the
    opening song on E01 was asked again on every other episode it appears in. Measured on
    the live library 2026-08-28: 665 pending admitted entries against 487 distinct text
    pairs -- 178 of them (27%) a question already answered.

    Here rather than in either caller, and for live_only's reason: two answers to "is this
    entry still open" would drift, and the drift would be invisible until a reviewer was
    shown a queue the gate disagreed with.

    FAILS OPEN on an absent store, like live_only: with nothing to check against, showing a
    settled question costs a moment and hiding a live one costs the review."""
    if not store:
        return list(entries)
    # Indexed once, not scanned per entry. decisions.lookup is a linear walk of the store,
    # and the review page runs this over every open entry in the library -- 8,600 of them on
    # 2026-08-28 -- against a store that grows with every verdict the reviewer gives. Same
    # key as lookup, BOTH sides: a verdict on one proposal must not settle a different
    # proposal for the same line, including the one that fixes it.
    decided = {(d.get("orig"), d.get("proposed")) for d in store.get("decisions", [])}
    return [
        e
        for e in entries
        if (decisions.key(e.get("original_text", "")), decisions.key(e.get("proposed_text", ""))) not in decided
    ]


def _rewrite(stem: str, doc: list) -> bool:
    """Replace the whole queue payload, used by resolve()/expire(). One store transaction."""
    try:
        artifact_store.write_jsonl(stem, KIND, doc)
        return True
    except Exception:
        return False


def record(stem: str, stage: str, reason: str, **fields) -> bool:
    """Append one unresolved case. Extra fields are the evidence for triage — for repair that
    is original_text/proposed_text/source_start/source_end/avg_logprob; for punctuation the
    run's text. `proposed_text` matters most: today a rejected repair discards what the model
    said, which is exactly what a human needs to judge whether the guard was right.

    P33: EVERY call increments the day/class counters; the evidence line itself is written
    only while that class is under its daily sample cap (the human-owed reasons excepted).
    That is what turns a 12,840-line-per-12h log into a bounded sample with exact totals."""
    entry = {"stage": stage, "reason": reason, "resolved": False, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    entry.update({k: v for k, v in fields.items() if v is not None})
    day = entry["ts"][:10]
    try:
        counted, keep = _count_and_sample(stem, stage, reason, day)
        if not keep:
            return counted
        # The store's append is one read-modify-write under its own transaction, so a torn
        # tail cannot happen and two writers cannot lose each other's line. The queue is
        # bounded by P33, so rewriting the row is cheap at this size.
        artifact_store.append_jsonl(stem, KIND, entry)
        return True
    except Exception:
        # The module's contract: observability never fails an episode that otherwise
        # generated. Any store failure here is a False return, never a traceback.
        return False


def resolve(stem: str, index: int, accept: bool, note: str = "") -> bool:
    """Mark one entry reviewed. The entry KEEPS its evidence: this file is the audit trail,
    not a worklist that shrinks. Mirrors glossary_acquire.record_decision, where a human
    rejection is itself durable information (it stops the next sweep re-proposing it)."""
    doc = items(stem)
    if not (0 <= index < len(doc)):
        return False
    doc[index]["resolved"] = True
    doc[index]["accepted"] = bool(accept)
    if note:
        doc[index]["note"] = note
    return _rewrite(stem, doc)


def reopen(stem: str, index: int) -> bool:
    """Make a resolved entry pending again (the model proposed the same pair once more after it was superseded). The evidence
    stays; the old resolution is dropped and counted in `reopened`, so the audit trail shows it happened."""
    doc = items(stem)
    if not (0 <= index < len(doc)):
        return False
    e = doc[index]
    e["resolved"] = False
    e.pop("accepted", None)
    e.pop("note", None)
    e["reopened"] = int(e.get("reopened") or 0) + 1
    return _rewrite(stem, doc)


def resolve_many(stem: str, updates: list) -> bool:
    """Mark several entries reviewed in ONE rewrite. `updates` is [(index, accept, note)].

    resolve() re-reads and re-writes the whole file per call. That is fine for the --review
    CLI, which settles one line at a time, and wrong for the review page, where a reviewer
    hands back thirty verdicts at once: thirty rewrites over CIFS, and -- because the server
    is threaded -- thirty read-modify-write windows in which two open tabs lose each other's
    entries.

    ALL OR NOTHING on a bad index. A caller that miscounted would otherwise land some
    verdicts and not others, with nothing telling the reviewer which."""
    doc = items(stem)
    if any(not (0 <= i < len(doc)) for i, _a, _n in updates):
        return False
    for index, accept, note in updates:
        doc[index]["resolved"] = True
        doc[index]["accepted"] = bool(accept)
        if note:
            doc[index]["note"] = note
    return _rewrite(stem, doc)


# --- review CLI ---------------------------------------------------------------------------
# Mirrors glossary_acquire.py's --review: walk what the automation could not settle, show the
# evidence that stage's triage actually needs, record the decision. Unified walk, stage-keyed
# storage -- one pass for the operator, but a repair entry still arrives with its reference
# and a punctuation entry with its run.

_EVIDENCE = {
    # what a human needs to see to settle each reason, in the order it helps
    "no_reference": ("original_text", "source_start", "source_end", "avg_logprob"),
    "rejected_guard": ("original_text", "proposed_text", "reference", "avg_logprob", "words"),
    "rejected_name_invented": ("original_text", "proposed_text", "reference", "avg_logprob", "words"),
    "rejected_name_part": ("original_text", "proposed_text", "reference", "avg_logprob", "words"),
    "llm_empty": ("original_text", "segments", "words"),
    # The repair was APPLIED. The reviewer's whole job is comparing these two texts.
    "accepted": ("original_text", "proposed_text", "avg_logprob"),
    # The reviewer's own verdict, refused on timing. They need both texts to shorten it.
    "decision_unfittable": ("original_text", "proposed_text", "avg_logprob"),
}


def _render(i: int, e: dict) -> str:
    head = f"[{i}] {e['stage']}/{e['reason']}"
    lines = [head, "-" * len(head)]
    for k in _EVIDENCE.get(e["reason"], ()):
        if k in e:
            lines.append(f"  {k:14} {e[k]}")
    for k, v in e.items():  # anything not in the template, so nothing hides
        if k not in ("stage", "reason", "resolved", "accepted", "note") and k not in _EVIDENCE.get(e["reason"], ()):
            lines.append(f"  {k:14} {v}")
    return "\n".join(lines)


# What a CLI answer MEANS, per stage. "keep as-is" is about the CARD, not the proposal, and
# the card shows different things depending on why the entry was queued:
#
#   repair_applied/accepted -- the repair SHIPPED, so the card shows it. Keeping it as-is
#       endorses the repair (`accept`); needing fixing rejects it.
#   repair/rejected_guard, rejected_name_invented, rejected_name_part -- the repair was REFUSED, so the card
#       shows the ASR text. Keeping it as-is endorses the refusal (`reject` of the
#       proposal); needing fixing says the ASR is wrong and the refused proposal should
#       stand, which is `force`.
#
# Recording `reject` for a "needs fixing" on both stages would silently invert the
# reviewer's meaning on half of them.
_CLI_VERDICT = {
    ("repair_applied", "accepted"): {"k": "accept", "f": "reject"},
    ("repair", "rejected_guard"): {"k": "reject", "f": "force"},
    ("repair", "rejected_name_invented"): {"k": "reject", "f": "force"},
    ("repair", "rejected_name_part"): {"k": "reject", "f": "force"},
    ("repair", "decision_unfittable"): {"k": "reject", "f": "reject"},
}


def _record_verdict(stem: str, e: dict, ans: str, note: str) -> bool:
    """Persist the CLI's answer as a real verdict. True when it is safely stored.

    Returns True for an entry there is nothing to store -- `no_reference` and `llm_empty`
    carry no proposal, so they have no pair to key on and were never decisions in the first
    place. False means a verdict was WANTED and could not be written, which must stop the
    entry being marked resolved: a review that silently discards the human's decision is
    worse than one that errors, because the human believes the line is settled."""
    verdict = _CLI_VERDICT.get((str(e.get("stage", "")), str(e.get("reason", ""))), {}).get(ans)
    if not verdict or not e.get("proposed_text"):
        return True
    show = show_for(stem)
    if not show:
        print("  NOT SAVED: cannot resolve a show for this episode — check GLOSSARY_DIR")
        return False
    ddir = decisions.DECISIONS_DIR
    with decisions.locked(show, ddir):
        store = decisions.record(decisions.load(show, ddir), e.get("original_text", ""), e["proposed_text"], verdict, note=note)
        if not decisions.save(store, show, ddir):
            print(f"  NOT SAVED: the verdict for this line could not be written to {ddir}")
            return False
    print(f"  recorded {verdict} for {show}")
    return True


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="Review subtitle-quality cases the pipeline could not settle.")
    ap.add_argument("target", help="an episode stem, or a directory to walk")
    ap.add_argument("--review", action="store_true", help="interactive walk (default: list pending and exit)")
    ap.add_argument("--all", action="store_true", help="also list entries dismissed as large changes (REPAIR_AUTO_DISMISS_WORDS)")
    a = ap.parse_args(argv)

    # The queue lives in the store now, so discovery asks the store for the stems holding an
    # ``unresolved`` row under the target directory. A target that is itself a stem wins.
    try:
        stems = [a.target] if artifact_store.exists(a.target, KIND) else artifact_store.stems(root=a.target, kind=KIND)
    except Exception:
        stems = []
    total = 0
    for stem in stems:
        todo = pending(stem)
        dismissed = [e for e in todo if e.get("dismissed")]
        if not a.all:
            todo = [e for e in todo if not e.get("dismissed")]
        if not todo:
            continue
        print(f"\n=== {os.path.basename(stem)}  ({len(todo)} pending) ===")
        if dismissed and not a.all:
            print(f"  ({len(dismissed)} dismissed as large changes: --all shows them)")
        for e in todo:
            idx = items(stem).index(e)
            print(_render(idx, e))
            total += 1
            if not a.review:
                continue
            try:
                ans = input("  keep as-is [k] / needs fixing [f] / skip [s] ? ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  stopped.")
                return 0
            if ans in ("k", "f"):
                note = input("  note (optional): ").strip()
                # The VERDICT first, then the flag. The verdict is what repair.py consults
                # and what the mux gate treats as the hold's authority; the flag only empties
                # this queue. Marking the entry resolved on an unsaved verdict would hide the
                # loss from the next walk, which is the one place it would still be visible.
                if _record_verdict(stem, e, ans, note):
                    resolve(stem, idx, accept=(ans == "k"), note=note)
    print(f"\n{total} pending across {len(stems)} episode(s)." if total else "\nNothing pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
