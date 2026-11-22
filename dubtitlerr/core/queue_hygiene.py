#!/usr/bin/env python3
"""P33 hygiene sweep: expire, cap and report every queue the app owns.

Run at the end of a generate pass (and safely by hand). It is the ONE writer that persists the
P33 policy: the read paths apply the same rules in memory, but only this sweep records them
as expiry decisions in the audit.

  python3 -m dubtitlerr.core.queue_hygiene [--glossary-dir DIR] [--media-root DIR]
                                           [--days N] [--limit N] [--dry-run] [--json]

What it does per queue:

  glossary  stamp a flag's first sighting; dismiss flags older than the window; cap the queue;
            write one P32-shaped audit record per dismissal to ``<glossary>.review-audit.jsonl``
  episodes  expire/cap each ``<stem>.dubtitles.unresolved.jsonl`` through ``unresolved.enforce``
            (which writes its own bounded audit and counters)

Output is a digest: exact counts per class, the self-report for any class over the alert
threshold, and whether anything needs the owner. ``--json`` is what ``gen_loop.sh`` reads.

Nothing here raises for one bad file: a sweep that aborted on the first unreadable glossary
would leave every later queue unbounded, which is the failure it exists to prevent.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from dubtitlerr.core import queue_policy, unresolved
from dubtitlerr.glossary import review_policy

DEFAULT_GLOSSARY_DIR = "/config/glossaries"
DEFAULT_MEDIA_ROOT = "/media"
SIDECAR = review_policy.SIDECAR


def sweep_glossaries(directory: str, now, days: float, limit: int, dry_run: bool) -> dict:
    out = {"files": 0, "queued": 0, "expired": 0, "bounded": 0, "by_class": {}, "errors": []}
    try:
        names = sorted(os.listdir(directory))
    except OSError as e:
        out["errors"].append(f"dir-unreadable: {e}")
        return out
    for name in names:
        if not name.endswith(".json") or SIDECAR.search(name):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            out["errors"].append(f"{name}: {e}")
            continue
        if not isinstance(doc, dict):
            out["errors"].append(f"{name}: not a JSON object")
            continue
        out["files"] += 1
        rep = review_policy.expire_flags(doc, None if dry_run else path, now=now, days=days, limit=limit)
        out["queued"] += rep["queued"]
        # ``rep["expired"]`` holds the age-expired AND the cap-dropped entries (``bounded`` is a
        # subset), so counting both as-is would report 500 items twice. The digest speaks in
        # actions: age expiry and cap-bound are different ones.
        out["expired"] += len(rep["expired"]) - len(rep["bounded"])
        out["bounded"] += len(rep["bounded"])
        for rec in rep["expired"]:
            reason = str(rec.get("reason") or "")
            out["by_class"][reason] = out["by_class"].get(reason, 0) + 1
        if rep["expired"] and not dry_run:
            try:
                review_policy._write(path, doc)
            except OSError as e:
                out["errors"].append(f"{name}: write-failed: {e}")
                continue
            review_policy._append_audit(
                path,
                [
                    review_policy.decision_record(
                        rec.get("term"),
                        rec.get("reason"),
                        "dismiss",
                        "queue-expiry",
                        now,
                        str(doc.get("show") or ""),
                        expired_days=rec.get("expired_days"),
                    )
                    for rec in rep["expired"]
                ],
            )
    out["by_class"] = dict(sorted(out["by_class"].items()))
    return out


def sweep_episodes(root: str, now, days: float, limit: int, dry_run: bool) -> dict:
    out = {"files": 0, "expired": 0, "bounded": 0, "by_class": {}, "errors": []}
    stems = []
    for path in glob.iglob(os.path.join(root, "**", "*" + unresolved.SUFFIX), recursive=True):
        stems.append(path[: -len(unresolved.SUFFIX)])
    for stem in sorted(stems):
        out["files"] += 1
        try:
            if dry_run:
                doc = unresolved.items(stem)
                _kept, expired = queue_policy.expire(doc, now=now, days=days)
                _kept, bounded = queue_policy.bound(doc, now=now, limit=limit)
                gone = {id(e) for e in expired}
                bounded = [e for e in bounded if id(e) not in gone]
                out["expired"] += len(expired)
                out["bounded"] += len(bounded)
                by_class = {}
                for e in [*expired, *bounded]:
                    cls = queue_policy.class_of(e)
                    by_class[cls] = by_class.get(cls, 0) + 1
            else:
                by_class = {}
                for e in unresolved.items(stem):
                    if queue_policy.is_open(e) and (queue_policy.age_days(e, now) or 0) > days:
                        cls = queue_policy.class_of(e)
                        by_class[cls] = by_class.get(cls, 0) + 1
                rep = unresolved.enforce(stem, now=now, days=days, limit=limit)
                out["expired"] += rep["expired"]
                out["bounded"] += rep["bounded"]
            for cls, n in by_class.items():
                out["by_class"][cls] = out["by_class"].get(cls, 0) + n
        except Exception as e:  # noqa: BLE001 -- one bad sidecar must not stop the sweep
            out["errors"].append(f"{os.path.basename(stem)}: {e}")
    out["by_class"] = dict(sorted(out["by_class"].items()))
    return out


def run(
    glossary_dir: str,
    media_roots: list,
    now=None,
    days: float = queue_policy.EXPIRY_DAYS,
    limit: int = queue_policy.QUEUE_MAX,
    dry_run: bool = False,
) -> dict:
    now = now or __import__("time").time()
    gloss = sweep_glossaries(glossary_dir, now, days, limit, dry_run)
    episodes = {"files": 0, "expired": 0, "bounded": 0, "by_class": {}, "errors": []}
    for root in media_roots:
        rep = sweep_episodes(root, now, days, limit, dry_run)
        for key in ("files", "expired", "bounded"):
            episodes[key] += rep[key]
        for cls, n in rep["by_class"].items():
            episodes["by_class"][cls] = episodes["by_class"].get(cls, 0) + n
        episodes["errors"] += rep["errors"]
    by_class = dict(gloss["by_class"])
    for cls, n in episodes["by_class"].items():
        by_class[cls] = by_class.get(cls, 0) + n
    digest = queue_policy.digest(
        expired=gloss["expired"] + episodes["expired"],
        queued=gloss["queued"],
        by_class=by_class,
        bounded=gloss["bounded"] + episodes["bounded"],
    )
    digest.update(
        glossaries=gloss,
        episodes=episodes,
        dry_run=dry_run,
        report=queue_policy.report(by_class),
    )
    return digest


def emit(digest: dict) -> list:
    """Deliver ONE batched digest; escalate only the exception classes.

    This is the whole notification policy in one function: a class that historically needed
    the owner (P32 tier 3, a human-owed subtitle verdict) gets its own `review.pending`
    event; every other class reports on the digest cadence and never nags per item."""
    from dubtitlerr.core import notify

    results = notify.emit(
        "review.digest",
        digest["summary"],
        counts=digest["counts"],
        loop="generate",
        detail={"classes": digest["detail"]["classes"], "by_class": digest["detail"]["by_class"]},
    )
    exceptions = [c for c in digest["detail"]["classes"] if c["escalate"]]
    if exceptions:
        results += notify.emit(
            "review.pending",
            f"{len(exceptions)} exception class(es) need a decision: "
            + ", ".join(f"{c['class']} ({c['count']})" for c in exceptions),
            counts={c["class"]: c["count"] for c in exceptions},
            loop="generate",
            detail={"classes": exceptions},
        )
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P33 queue hygiene: expire, cap and report every queue.")
    ap.add_argument("--glossary-dir", default=os.environ.get("GLOSSARY_DIR") or DEFAULT_GLOSSARY_DIR)
    ap.add_argument("--media-root", action="append", default=[], help="media root to walk (repeatable)")
    ap.add_argument("--days", type=float, default=queue_policy.EXPIRY_DAYS)
    ap.add_argument("--limit", type=int, default=queue_policy.QUEUE_MAX)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="one JSON digest on stdout (the gen_loop shape)")
    ap.add_argument(
        "--notify",
        action="store_true",
        help="deliver the digest (and escalate only exception classes) through the configured providers",
    )
    a = ap.parse_args(argv)
    roots = a.media_root or [os.environ.get("MEDIA_ROOT") or DEFAULT_MEDIA_ROOT]
    digest = run(a.glossary_dir, roots, days=a.days, limit=a.limit, dry_run=a.dry_run)
    if a.notify:
        emit(digest)
    if a.json:
        print(json.dumps(digest, ensure_ascii=False), flush=True)
    else:
        print(digest["summary"], flush=True)
        for rep in digest["report"]:
            print(f"  {rep['class']}: {rep['count']} -- {rep['proposal']}", flush=True)
        for err in digest["glossaries"]["errors"] + digest["episodes"]["errors"]:
            print(f"  WARNING {err}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
