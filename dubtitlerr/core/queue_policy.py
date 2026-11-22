#!/usr/bin/env python3
"""Queue hygiene (P33): the app owns its backlog.

Review queues are evidence, but an unbounded queue is not evidence -- it is a pile nobody
reads. Measured on the live library 2026-11-21: 700 pending glossary entries and a 12,840-line
`.unresolved.jsonl` in twelve hours, 93% of it machine noise the P32 tiers already classify.
This module is the policy that keeps any queue bounded and every automatic decision visible:

  * every open item expires after ``EXPIRY_DAYS`` with no action -- dismissed, audited, and
    reversible (``unexpire``), so an unattended pile drains itself instead of growing;
  * a queue is capped at ``QUEUE_MAX`` items, oldest first, so it can never again sit at 700;
  * a class crossing ``ALERT_CLASS`` self-reports with a resolution proposal instead of
    waiting for a person to notice the pile;
  * the audit of every automatic action is a bounded ring (newest ``AUDIT_MAX``), not a
    second growing log.

Deliberately stdlib-only: the shell loops and the review server both import this, and the
module must not drag pysubs2 in through ``common``. Atomic writes are implemented here for
the same reason (``common.atomic_write`` imports pysubs2).

Expiry is fail-closed on evidence and fail-open on age: an unreadable/missing timestamp is
stamped with ``now`` and KEPT for one window, because dropping an item whose age nobody can
prove is how a queue loses the question it existed to ask.
"""

from __future__ import annotations

import calendar
import json
import os
import time

POLICY_VERSION = "p33-v1"

EXPIRY_DAYS = 14
QUEUE_MAX = 200
ALERT_CLASS = 50
AUDIT_MAX = 500
# Evidence lines kept per class per day, beside the exact counters. Enough to judge a class,
# far below its volume (no_reference alone was 6,601 in twelve hours).
SAMPLE_CAP = 25

# Classes whose presence has historically meant "a person must look": P32's tier-3 reasons
# (never auto-resolved) plus the subtitle runner's human-only reasons. Everything else reports
# on the digest cadence and never notifies per item.
ESCALATE_REASONS = frozenset(
    {
        "unseen-needs-evidence",
        "conflicts-with-curated-fix",
        "no-wiki-match",
        "decision_unfittable",
        "verdict_unfittable",
        "verdict_owed",
    }
)

# What each over-threshold class is telling the owner, in the owner's terms.
_PROPOSALS = {
    "repair/no_reference": "the release has no usable fansub: one fact about the release, not N cards -- "
    "leave it counted, or set NO_REFERENCE_POLICY=skip to stop recording it",
    "repair/llm_empty": "the repair backend is failing: check the endpoint before reading the queue",
    "repair/rejected_guard": "the guard is refusing proposals it should accept: sample the evidence, then widen the rule",
    "repair/rejected_name_invented": "the name guard is working as designed: counted, not queued",
    "repair_applied/accepted": "accepted repairs are the audit trail, not a question -- sample, never queue",
    "repair/decision_unfittable": "a human verdict cannot be rendered on its card: shorten the line (C1 holds timing)",
}


def _now(now=None) -> float:
    return time.time() if now is None else float(now)


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_time(value) -> float | None:
    """Epoch seconds from a float/int epoch or the queue's ISO-8601 `...Z` stamp; None if unreadable."""
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return float(calendar.timegm(time.strptime(text, fmt)))
            except ValueError:
                continue
        try:
            return float(text)
        except ValueError:
            return None
    return None


def age_days(entry: dict, now=None) -> float | None:
    at = parse_time((entry or {}).get("ts") if (entry or {}).get("ts") is not None else (entry or {}).get("at"))
    if at is None:
        return None
    return max(0.0, (_now(now) - at) / 86400.0)


def is_open(entry: dict) -> bool:
    return not (entry or {}).get("resolved") and not (entry or {}).get("expired")


def stamp_missing(entry: dict, now=None) -> bool:
    """Give an entry with no readable time a first-seen stamp. True when it was stamped."""
    if parse_time((entry or {}).get("ts")) is not None:
        return False
    entry["ts"] = iso(_now(now))
    entry["ts_backfilled"] = True
    return True


def expire(items, now=None, days: float = EXPIRY_DAYS):
    """(kept, expired) -- open items older than ``days`` are marked dismissed-but-reversible.

    The expired records keep every field they had plus ``expired``/``expired_at``/``expired_days``,
    which is what ``unexpire`` reverses and the audit feed shows.
    """
    now = _now(now)
    kept, expired = [], []
    for e in items or []:
        if not isinstance(e, dict) or not is_open(e):
            kept.append(e)
            continue
        stamp_missing(e, now)
        age = age_days(e, now) or 0.0
        if age > days:
            e["expired"] = True
            e["expired_at"] = iso(now)
            e["expired_days"] = int(age)
            expired.append(e)
        else:
            kept.append(e)
    return kept, expired


def unexpire(entry: dict) -> bool:
    """Reverse one expiry. True when the entry had been expired."""
    if not (entry or {}).get("expired"):
        return False
    for key in ("expired", "expired_at", "expired_days"):
        entry.pop(key, None)
    return True


def bound(items, now=None, limit: int = QUEUE_MAX):
    """(kept, expired) -- the queue never holds more than ``limit`` open items; oldest goes first."""
    now = _now(now)
    open_items = [e for e in (items or []) if isinstance(e, dict) and is_open(e)]
    for e in open_items:
        stamp_missing(e, now)
    if len(open_items) <= limit:
        return list(items or []), []
    oldest = sorted(open_items, key=lambda e: parse_time(e.get("ts")) or now)[: len(open_items) - limit]
    drop = {id(e) for e in oldest}
    kept, expired = [], []
    for e in items or []:
        if isinstance(e, dict) and id(e) in drop:
            e["expired"] = True
            e["expired_at"] = iso(now)
            e["expired_days"] = int(age_days(e, now) or 0)
            e["expired_reason"] = "queue-bound"
            expired.append(e)
        else:
            kept.append(e)
    return kept, expired


def class_of(entry: dict) -> str:
    return f"{entry.get('stage', '')}/{entry.get('reason', '')}"


def counts(items) -> dict:
    """Open items per (stage, reason), the classes a self-report and a digest speak in."""
    out: dict = {}
    for e in items or []:
        if isinstance(e, dict) and is_open(e):
            out[class_of(e)] = out.get(class_of(e), 0) + 1
    return dict(sorted(out.items()))


def proposal_for(cls: str, count: int) -> str:
    stage_reason = cls.split("/", 1)
    reason = stage_reason[1] if len(stage_reason) == 2 else cls
    if cls in _PROPOSALS:
        return _PROPOSALS[cls]
    if reason in _PROPOSALS:
        return _PROPOSALS[reason]
    if reason in ESCALATE_REASONS:
        return "this class needs a person: inspect a sample, then rule on the pattern (P32 tier table)"
    return "sample the evidence; if the class is noise, add it to P32's tier table so it stops queuing"


def report(counts_by_class: dict, alert: int = ALERT_CLASS) -> list:
    """Classes over ``alert``, each with the resolution proposal -- the pile's self-report."""
    out = [
        {"class": cls, "count": n, "proposal": proposal_for(cls, n)} for cls, n in (counts_by_class or {}).items() if n >= alert
    ]
    out.sort(key=lambda r: (-r["count"], r["class"]))
    return out


def escalate_class(cls: str) -> bool:
    reason = cls.split("/", 1)[1] if "/" in cls else cls
    return reason in ESCALATE_REASONS


def digest(expired: int = 0, queued: int = 0, by_class: dict | None = None, bounded: int = 0) -> dict:
    """One batched report. Exception classes are called out; everything else is a count."""
    by_class = dict(sorted((by_class or {}).items()))
    classes = [
        {"class": cls, "count": n, "escalate": escalate_class(cls)}
        for cls, n in by_class.items()
        if escalate_class(cls) or n >= ALERT_CLASS
    ]
    parts = [f"{queued} queued", f"{expired} expired"]
    if bounded:
        parts.append(f"{bounded} bounded")
    return {
        "summary": "review digest: " + ", ".join(parts),
        "counts": {"expired": expired, "queued": queued, "bounded": bounded},
        "detail": {"classes": classes, "by_class": by_class},
        "escalate": any(c["escalate"] for c in classes),
        "policy": POLICY_VERSION,
        "at": iso(time.time()),
    }


# --- bounded audit ------------------------------------------------------------------------


def audit_append(path: str, records, max_records: int = AUDIT_MAX) -> bool:
    """Append to a bounded JSONL ring: newest ``max_records`` survive, older lines are dropped.

    The queue's audit is evidence too, so it must not become the next 12,840-line log. A
    rewrite happens only when the cap is crossed, and through a temp file + os.replace so a
    killed write leaves the previous audit intact. Never raises: observability.
    """
    records = [r for r in (records or []) if isinstance(r, dict)]
    if not records:
        return False
    try:
        lines: list = []
        try:
            with open(path, encoding="utf-8") as f:
                lines = [line for line in f if line.strip()]
        except OSError:
            lines = []
        lines.extend(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
        if len(lines) > max_records:
            lines = lines[-max_records:]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def audit_read(path: str, limit: int = 50) -> list:
    """Newest-first audit records, skipping torn lines. [] when absent/unreadable."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    out.reverse()
    return out[: max(0, int(limit))]


def audit_record(kind: str, **fields) -> dict:
    rec = {"at": time.time(), "at_iso": iso(time.time()), "kind": kind, "policy": POLICY_VERSION}
    rec.update(fields)
    return rec
