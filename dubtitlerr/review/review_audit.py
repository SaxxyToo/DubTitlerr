#!/usr/bin/env python3
"""P33 audit-first surface: what the app decided, why, and one-tap undo.

Until now the review workspace opened on a pending gate: a list of questions the pipeline
could not settle, with no record of the decisions it HAD taken. That is backwards. Most of
the daily volume is decisions the app already made (P32 dismisses noise, tier-2 applies a
corroborated respelling, P33 expires a stale item); the exceptions are few. This module is
the feed of those auto decisions and the undo that makes them safe:

    GET  /api/audit          recent auto decisions, newest first, with their evidence
    POST /api/audit/undo     reverse one, restoring the prior state and recording the reversal

Two sources, one vocabulary:

  * the glossary's ``<show>.json.review-audit.jsonl`` (P32 records: dismiss/apply/expire),
  * each episode's ``<stem>.dubtitles.unresolved.audit.jsonl`` (P33 expiry/queue-bound).

Both write the P32 record shape, so the feed does not care which stage wrote a record. Undo
is dispatched by kind and every reversal appends its own audit line -- an undo is itself a
decision, and the next reader must see it.

An id is derived from the record's own content (kind, target, at), never a list offset: a
stale tab must never undo a different decision than the one it showed.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os

from dubtitlerr.core import decisions, queue_policy, unresolved
from dubtitlerr.glossary import review_policy

AUDIT_SUFFIX = ".json.review-audit.jsonl"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def glossary_dir() -> str:
    return os.environ.get("GLOSSARY_DIR") or getattr(decisions, "GLOSSARY_DIR", "/config/glossaries")


def media_roots() -> list:
    roots = []
    for name in ("MEDIA_ROOT", "ANIME_ROOT", "DUB_MEDIA_ROOT"):
        value = (os.environ.get(name) or "").strip()
        if value:
            roots.append(value)
            break
    return roots or ["/media"]


def _audit_files(limit: int) -> list:
    out = []
    try:
        out += sorted(glob.glob(os.path.join(glossary_dir(), "*" + AUDIT_SUFFIX)))[:limit]
    except OSError:
        pass
    for root in media_roots():
        try:
            out += sorted(glob.glob(os.path.join(root, "**", "*" + unresolved.AUDIT_SUFFIX), recursive=True))[:limit]
        except OSError:
            pass
    return out


def item_id(kind: str, target: str, at, extra: str = "") -> str:
    return hashlib.sha256(json.dumps([kind, target, at, extra]).encode()).hexdigest()[:16]


def _at(rec: dict) -> float:
    value = rec.get("at")
    try:
        return float(value)
    except (TypeError, ValueError):
        return queue_policy.parse_time(rec.get("at_iso")) or 0.0


def _gloss_item(rec: dict) -> dict:
    term = str(rec.get("term") or "")
    show = str(rec.get("show") or "")
    decision = str(rec.get("decision") or "")
    guard = rec.get("guard")
    expired = guard == "queue-expiry" or decision == "expire"
    return {
        "id": item_id("glossary", f"{show}|{term}", _at(rec), decision),
        "kind": "glossary",
        "decision": "expire" if expired else decision,
        "at": _at(rec),
        "show": show,
        "term": term,
        "reason": rec.get("reason"),
        "guard": guard,
        "applied": rec.get("applied"),
        "by": rec.get("by") or "auto",
        "undoable": rec.get("by") != "operator" and decision in ("dismiss", "apply", "expire"),
        "detail": rec,
    }


def _queue_item(rec: dict, stem: str) -> dict:
    kind = str(rec.get("kind") or "")
    return {
        "id": item_id("queue", f"{stem}|{rec.get('index')}", _at(rec), kind),
        "kind": "queue",
        "decision": kind,
        "at": _at(rec),
        "show": os.path.basename(os.path.dirname(os.path.dirname(stem))),
        "stem": stem,
        "index": rec.get("index"),
        "reason": f"{rec.get('stage', '')}/{rec.get('reason', '')}",
        "original_text": rec.get("original_text"),
        "days": rec.get("days"),
        "by": "auto" if kind != "undo-expiry" else "operator",
        "undoable": kind in ("expiry", "queue-bound"),
        "detail": rec,
    }


def feed(limit: int = DEFAULT_LIMIT, show: str = "") -> dict:
    """Recent auto decisions across every queue, newest first. Fails open with []."""
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    items: list = []
    for path in _audit_files(limit):
        if path.endswith(AUDIT_SUFFIX):
            gloss_path = path[: -len(AUDIT_SUFFIX)]
            try:
                with open(gloss_path, encoding="utf-8") as f:
                    gloss_show = str((json.load(f) or {}).get("show") or "")
            except (OSError, ValueError, TypeError, AttributeError):
                gloss_show = ""
            for rec in queue_policy.audit_read(path, limit):
                if isinstance(rec, dict) and rec.get("term"):
                    item = _gloss_item(rec)
                    item["show"] = item["show"] or gloss_show
                    items.append(item)
        elif path.endswith(unresolved.AUDIT_SUFFIX):
            stem = path[: -len(unresolved.AUDIT_SUFFIX)]
            for rec in queue_policy.audit_read(path, limit):
                if isinstance(rec, dict):
                    items.append(_queue_item(rec, stem))
    if show:
        wanted = show.strip().casefold()
        items = [i for i in items if str(i.get("show") or "").casefold() == wanted]
    items.sort(key=lambda i: i["at"], reverse=True)
    counts: dict = {}
    for i in items:
        counts[i["decision"]] = counts.get(i["decision"], 0) + 1
    return {
        "decisions": items[:limit],
        "counts": dict(sorted(counts.items())),
        "undoable": sum(1 for i in items if i["undoable"]),
        "policy": queue_policy.POLICY_VERSION,
    }


def _find(item_id_: str, show: str = "") -> dict | None:
    for item in feed(MAX_LIMIT, show)["decisions"]:
        if item["id"] == item_id_:
            return item
    return None


def _write_gloss(path: str, doc: dict) -> None:
    review_policy._write(path, doc)


def _undo_glossary(item: dict) -> tuple:
    term = item.get("term") or ""
    path = os.path.join(glossary_dir(), f"{item.get('show')}.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        return 404, {"error": f"glossary unreadable: {e}"}
    if not isinstance(doc, dict):
        return 422, {"error": "glossary is not an object"}
    restored = None
    if item.get("decision") == "expire":
        restored = review_policy.undo_flag(doc, term)
        if restored is None:
            return 409, {"error": "that dismissal is no longer in the undo window"}
    else:
        # P32's auto dismiss/apply: drop the ruling and re-queue the flag when the archive
        # still holds its evidence.
        doc = review_policy.undo(doc, term)
        for rec in reversed(_archived_records(path)):
            if review_policy._fold(rec.get("term")) == review_policy._fold(term):
                meta = {k: v for k, v in rec.items() if k not in ("term", "at")}
                doc.setdefault("flagged", {})[str(rec.get("term"))] = meta
                restored = {"term": rec.get("term")}
                break
    _write_gloss(path, doc)
    review_policy._append_audit(
        path,
        [
            review_policy.decision_record(
                term,
                str(item.get("reason") or ""),
                "restore",
                item.get("guard"),
                queue_policy.parse_time(item.get("at")),
                str(item.get("show") or ""),
                by="operator",
            )
        ],
    )
    return 200, {"ok": True, "restored": restored, "id": item["id"]}


def _archived_records(gloss_path: str) -> list:
    out = []
    try:
        with open(gloss_path + ".archived.jsonl", encoding="utf-8") as f:
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
    return out


def _undo_queue(item: dict) -> tuple:
    stem = str(item.get("stem") or "")
    try:
        index = int(item.get("index"))
    except (TypeError, ValueError):
        return 422, {"error": "not an expiry record"}
    if unresolved.undo_expire(stem, index):
        return 200, {"ok": True, "restored": {"stem": stem, "index": index}, "id": item["id"]}
    return 409, {"error": "that entry is no longer expired (already restored or rewritten)"}


def undo(item_id_: str) -> tuple:
    """Reverse one auto decision: (status, payload). Restores the prior state and audits it."""
    if not isinstance(item_id_, str) or not item_id_.strip():
        return 400, {"error": "id required"}
    item = _find(item_id_.strip())
    if item is None:
        return 404, {"error": "no such decision (or it aged out of the audit)"}
    if not item["undoable"]:
        return 409, {"error": "that decision is not reversible from here"}
    if item["kind"] == "glossary":
        return _undo_glossary(item)
    if item["kind"] == "queue":
        return _undo_queue(item)
    return 409, {"error": "unknown decision source"}


def queues() -> dict:
    """Queue health for the audit view: what is open, by class, with the self-report.

    Every number here is derived from the same counters that bound the queues, so the page
    cannot show a pile the pipeline does not have."""
    out = {"policy": queue_policy.POLICY_VERSION, "expiry_days": queue_policy.EXPIRY_DAYS, "max": queue_policy.QUEUE_MAX}
    gloss = {"queued": 0, "expired": 0, "by_class": {}, "report": []}
    for path in _sorted_glossaries():
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(doc, dict):
            continue
        flagged = doc.get("flagged") if isinstance(doc.get("flagged"), dict) else {}
        by_class: dict = {}
        for meta in flagged.values():
            reason = str((meta or {}).get("reason") if isinstance(meta, dict) else meta or "")
            by_class[reason] = by_class.get(reason, 0) + 1
        gloss["queued"] += len(flagged)
        archived = doc.get("archived_flags") if isinstance(doc.get("archived_flags"), dict) else {}
        gloss["expired"] += int(archived.get("expired") or 0)
        for reason, n in by_class.items():
            gloss["by_class"][reason] = gloss["by_class"].get(reason, 0) + n
    gloss["by_class"] = dict(sorted(gloss["by_class"].items()))
    gloss["report"] = queue_policy.report(gloss["by_class"])
    out["glossary"] = gloss
    return out


def _sorted_glossaries() -> list:
    try:
        names = sorted(os.listdir(glossary_dir()))
    except OSError:
        return []
    return [os.path.join(glossary_dir(), n) for n in names if n.endswith(".json") and not review_policy.SIDECAR.search(n)]
