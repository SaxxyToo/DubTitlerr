#!/usr/bin/env python3
"""Review policy (P32): the reason -> tier table, auto-resolution, durable verdicts, audit.

Review noise exists because every sweep re-derives guard verdicts (`english-word`, `below-floor`,
`sentence-initial-only`) and re-queues them beside the handful of genuine judgement calls. On the
live library that was 1,877 queued entries, 1,787 of them machine noise, and the same class re-minted
after every cleanup. This module stops that at the source.

Tiers (fail-closed: an unlisted reason or an unmet guardrail is QUEUED, never resolved):

  tier 1  dismiss, never queued   english-word, below-floor, sentence-initial-only
          GUARDRAIL: only when the variant matches no wiki title, no glossary cast term
          (names/phrases/known/acquired), no hard_fix -- and only when wiki titles are known.
          Fern/Stark/Aura are ordinary words AND wiki pages, so they stay in the queue.
  tier 2  apply, else surface     respelling-needs-review, possessive_floor_crossing
          A correction is auto-applied only when the flag carries a canonical, the canonical is
          already a glossary canonical (corroboration -- a bare model-high spelling is the class
          that measured >50% wrong), the effect is a spelling hard_fix, and no curated fix
          disagrees. Everything else is surfaced.
  tier 3  queue                   unseen-needs-evidence, conflicts-with-curated-fix,
                                  no-wiki-match, and every unknown reason.

`no-match` / `common-word` keep their P24 behaviour: archived as counts, no human decision to make.

Verdicts are durable. Every dismissal/approval (auto or operator) writes a ruling into the
glossary under ``review_rulings`` keyed by the casefolded term, so the same variant+reason can never
re-enter the queue. The per-reason tier is the pattern-level rule; the ruling is the per-show one.

Every auto decision is an audit record appended to ``<glossary>.review-audit.jsonl`` and returned in
the report. Shape shared with the P33 audit view (one JSON object per line)::

    {"at", "term", "reason", "decision", "tier", "canonical", "applied",
     "guard", "by", "policy", "show"}

  decision  "dismiss" | "apply"
  applied   "hard_fix" when a correction was written, else null
  guard     why a resolution was refused ("wiki-title", "curated-conflict", ...), else null
  by        "auto" | "operator"
  policy    POLICY_VERSION

`undo(gloss, term)` removes an auto ruling and the fix only if this policy wrote it; a human-owned
fix (run == "review") is never touched.

Backfill: ``python3 -m dubtitlerr.glossary.review_policy GLOSSARY... [--dir DIR] [--dry-run]``
drains an existing pile per the tiers above, reading the show's cached wiki titles. With no cache the
tier-1 guardrail cannot be applied, so those entries stay queued (fail-closed) and the report says so.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from dubtitlerr.core import queue_policy

POLICY_VERSION = "p32-v1"

DISMISS = "dismiss"
APPLY = "apply"
QUEUE = "queue"

TIER1 = 1
TIER2 = 2
TIER3 = 3

REASON_TIERS = {
    "english-word": (TIER1, DISMISS),
    "below-floor": (TIER1, DISMISS),
    "sentence-initial-only": (TIER1, DISMISS),
    "respelling-needs-review": (TIER2, APPLY),
    "possessive_floor_crossing": (TIER2, APPLY),
    "unseen-needs-evidence": (TIER3, QUEUE),
    "conflicts-with-curated-fix": (TIER3, QUEUE),
    "no-wiki-match": (TIER3, QUEUE),
}
# P24's collapse classes: nothing a person can accept or reject, so counts instead of entries.
COLLAPSE_REASONS = frozenset({"common-word", "no-match"})
SIDECAR = re.compile(r"\\.(acquire-cache|lastrun|journal|cleanup)\\.json$|\\.review-pending|\\.bak-")


def _fold(value) -> str:
    return str(value or "").strip().casefold()


def reason_of(meta) -> str:
    """The reason of a `flagged` entry, tolerating the legacy bare-string form."""
    return meta if isinstance(meta, str) else str((meta or {}).get("reason") or "")


def classify(reason: str) -> str:
    """DISMISS / APPLY / QUEUE for one reason; anything unlisted is QUEUE (fail-closed)."""
    return REASON_TIERS.get(reason, (TIER3, QUEUE))[1]


def tier_of(reason: str) -> int:
    return REASON_TIERS.get(reason, (TIER3, QUEUE))[0]


def _fixes(gloss) -> dict:
    return gloss.get("hard_fixes") or {}


def _curated_value(gloss, term: str):
    """The hard_fix value a person owns for `term`, else None (mirrors glossary_acquire._is_curated)."""
    fixes = _fixes(gloss)
    key = _fold(term)
    fk = next((k for k in fixes if _fold(k) == key), None)
    if fk is None:
        return None
    acquired = gloss.get("acquired") or {}
    ak = next((k for k in acquired if _fold(k) == key), None)
    if ak is None:
        return fixes[fk]
    meta = acquired[ak] or {}
    if meta.get("run") == "review" or fixes[fk] != meta.get("canonical"):
        return fixes[fk]
    return None


def protection(term: str, gloss: dict, titles=None):
    """The guardrail label that keeps `term` out of tier-1 auto-dismissal, else None.

    Exact wiki title > glossary cast (names/phrases) > known > acquired > hard_fix key/value.
    `verified` is deliberately absent: it contains the term itself, so it proves nothing.
    """
    key = _fold(term)
    if titles:
        title_keys = (
            titles if isinstance(titles, (set, frozenset)) else {_fold(t) for t in titles if isinstance(t, str) and t.strip()}
        )
        if key in title_keys:
            return "wiki-title"
    for field in ("names", "phrases"):
        if any(_fold(n) == key for n in (gloss.get(field) or []) if isinstance(n, str)):
            return "glossary-" + field
    if any(_fold(k) == key for k in (gloss.get("known") or []) if isinstance(k, str)):
        return "known"
    if any(_fold(k) == key for k in (gloss.get("acquired") or {})):
        return "acquired"
    fixes = _fixes(gloss)
    if any(_fold(k) == key for k in fixes):
        return "hard-fix"
    if any(_fold(v) == key for v in fixes.values() if isinstance(v, str)):
        return "hard-fix-value"
    return None


def _known_canonicals(gloss) -> set[str]:
    """Every spelling the glossary already treats as canonical (for tier-2 corroboration).

    Deliberately excludes `known` and acquired KEYS: those are variants a person ruled on,
    not established spellings. A canonical must come from names/phrases or a fix value.
    """
    out = set()
    for field in ("names", "phrases"):
        out.update(_fold(n) for n in (gloss.get(field) or []) if isinstance(n, str))
    acquired = gloss.get("acquired") or {}
    out.update(_fold((m or {}).get("canonical")) for m in acquired.values() if isinstance(m, dict))
    fixes = _fixes(gloss)
    out.update(_fold(v) for v in fixes.values() if isinstance(v, str))
    out.discard("")
    return out


def _tier2(term: str, meta: dict, gloss: dict):
    """(action, applied, guard) for one tier-2 entry. Never invents a correction."""
    canon = str(meta.get("canonical") or "").strip()
    if canon:
        curated = _curated_value(gloss, term)
        if curated is not None:
            if _fold(curated) == _fold(canon):
                return APPLY, None, "already-curated"  # already what the person wants: drop the flag
            return QUEUE, None, "curated-conflict"
        if _fold(canon) not in _known_canonicals(gloss):
            return QUEUE, None, "uncorroborated-canonical"
        # Call time: glossary_acquire imports this module transitively through verify.
        from dubtitlerr.glossary.glossary_acquire import accept_effect

        if accept_effect(term, meta) != "hard_fix":
            return QUEUE, None, "not-a-spelling-fix"
        return APPLY, "hard_fix", None
    # A crossing with no candidate: only safe when the term is already on the roster.
    if protection(term, gloss, None):
        return DISMISS, None, "already-known"
    return QUEUE, None, "no-canonical"


def _apply_hard_fix(gloss: dict, term: str, canon: str, meta: dict) -> None:
    """Write the correction the way record_decision's human hard_fix branch does, marked auto."""
    key = _fold(term)
    fixes = gloss.setdefault("hard_fixes", {})
    for k in [k for k in fixes if _fold(k) == key]:
        fixes.pop(k)
    fixes[term] = canon
    acquired = gloss.setdefault("acquired", {})
    for k in [k for k in acquired if _fold(k) == key]:
        acquired.pop(k)
    acquired[term] = {
        "canonical": canon,
        "count": meta.get("variant_count", 0),
        "canonical_count": meta.get("canonical_count", 0),
        "score": meta.get("score", 0.0),
        "bound": meta.get("bound", 0.0),
        "reason": "auto-approved",
        "run": f"auto:{POLICY_VERSION}",
    }
    known = [k for k in (gloss.get("known") or []) if _fold(k) != key]
    if known:
        gloss["known"] = known
    else:
        gloss.pop("known", None)


def _ruling(reason: str, decision: str, canonical: str, at: float) -> dict:
    return {
        "reason": reason,
        "decision": decision,
        "canonical": canonical,
        "at": round(at, 3),
        "by": "auto",
        "policy": POLICY_VERSION,
    }


def _record(term, reason, decision, tier, canonical, applied, guard, at, show) -> dict:
    return {
        "at": round(at, 3),
        "term": term,
        "reason": reason,
        "decision": decision,
        "tier": tier,
        "canonical": canonical,
        "applied": applied,
        "guard": guard,
        "by": "auto",
        "policy": POLICY_VERSION,
        "show": show,
    }


def audit_path(gloss_path: str) -> str:
    return gloss_path + ".review-audit.jsonl"


def _append_audit(gloss_path: str, records: list) -> None:
    """Append this glossary's decision records to a BOUNDED ring (newest ``AUDIT_MAX``).

    P33: an audit is evidence, and evidence that grows without limit is the next 12,840-line
    log. The decision *history* survives as the glossary's own ``archived_flags`` counters;
    this file is the bounded window the UI can inspect and undo. Never raises: the glossary
    write is the durable state, and a lost audit line must not fail it.
    """
    if not records:
        return
    queue_policy.audit_append(audit_path(gloss_path), records, max_records=queue_policy.AUDIT_MAX)


def apply_policy(gloss: dict, gloss_path: str | None = None, titles=None, now=None) -> dict:
    """Resolve one glossary's queue by policy. Mutates ``gloss`` in place; returns the report.

    ``titles`` is the show's wiki title list. Without it tier-1 entries are left queued
    (guard "no-wiki-cache"), because the exact-title exemption cannot be applied.
    """
    now = time.time() if now is None else now
    show = str(gloss.get("show") or "")
    raw_flagged = gloss.get("flagged")
    if raw_flagged is not None and not isinstance(raw_flagged, dict):
        # A malformed queue is left untouched: silently dropping it would lose evidence.
        return {
            "archived": 0,
            "counts": {},
            "flagged": 0,
            "tiers": {
                "dismiss": {"dismissed": 0, "protected": 0},
                "apply": {"applied": 0, "queued": 0},
                "queue": {"queued": 0},
                "ruled": 0,
                "collapsed": 0,
            },
            "queued": [],
            "decisions": [],
            "note": "flagged-not-a-map",
        }
    flagged = dict(raw_flagged or {})
    rulings = dict(gloss.get("review_rulings") or {}) if isinstance(gloss.get("review_rulings"), dict) else {}
    title_keys = frozenset(_fold(t) for t in titles if isinstance(t, str) and t.strip()) if titles else None
    have_titles = bool(title_keys)

    archived, counts, decisions, queued = [], {}, [], []
    tiers = {
        "dismiss": {"dismissed": 0, "protected": 0},
        "apply": {"applied": 0, "queued": 0},
        "queue": {"queued": 0},
        "ruled": 0,
        "collapsed": 0,
    }

    for term in sorted(flagged):
        meta = flagged[term]
        reason = reason_of(meta)
        record = meta if isinstance(meta, dict) else {"reason": meta}
        canon = str(record.get("canonical") or "").strip()
        key = _fold(term)

        prior = rulings.get(key)
        if isinstance(prior, dict) and prior.get("decision") in (DISMISS, APPLY):
            if prior["decision"] == APPLY and canon and _fold(canon) not in _known_canonicals(gloss):
                _apply_hard_fix(gloss, term, canon, record)
            flagged.pop(term, None)
            tiers["ruled"] += 1
            continue

        if reason in COLLAPSE_REASONS:
            counts[reason] = counts.get(reason, 0) + 1
            archived.append({**record, "term": term, "reason": reason})
            flagged.pop(term, None)
            tiers["collapsed"] += 1
            continue

        action = classify(reason)
        if action == QUEUE:
            tiers["queue"]["queued"] += 1
            queued.append({"term": term, "reason": reason, "why": "queue"})
            continue

        if action == DISMISS:
            guard = protection(term, gloss, title_keys)
            if guard is None and not have_titles:
                guard = "no-wiki-cache"
            if guard:
                tiers["dismiss"]["protected"] += 1
                queued.append({"term": term, "reason": reason, "why": guard})
                continue
            rulings[key] = _ruling(reason, DISMISS, canon, now)
            counts[reason] = counts.get(reason, 0) + 1
            archived.append({**record, "term": term, "reason": reason})
            flagged.pop(term, None)
            decisions.append(_record(term, reason, DISMISS, TIER1, canon, None, None, now, show))
            tiers["dismiss"]["dismissed"] += 1
            continue

        # tier 2
        outcome, applied, guard = _tier2(term, record, gloss)
        if outcome == DISMISS:
            rulings[key] = _ruling(reason, DISMISS, canon, now)
            counts[reason] = counts.get(reason, 0) + 1
            archived.append({**record, "term": term, "reason": reason})
            flagged.pop(term, None)
            decisions.append(_record(term, reason, DISMISS, TIER2, canon, None, guard, now, show))
            tiers["dismiss"]["dismissed"] += 1
            continue
        if outcome != APPLY:
            tiers["apply"]["queued"] += 1
            queued.append({"term": term, "reason": reason, "why": guard})
            continue
        if applied == "hard_fix":
            _apply_hard_fix(gloss, term, canon, record)
        rulings[key] = _ruling(reason, APPLY, canon, now)
        flagged.pop(term, None)
        decisions.append(_record(term, reason, APPLY, TIER2, canon, applied, guard, now, show))
        tiers["apply"]["applied"] += 1

    if decisions or archived:
        # P33 reads the last per-tier counts here; only written when something was decided so a
        # no-op sweep leaves the file byte-identical.
        gloss["review_policy"] = {"version": POLICY_VERSION, "at": round(now, 3), "tiers": tiers}
    if flagged:
        gloss["flagged"] = flagged
    else:
        gloss.pop("flagged", None)
    if rulings:
        gloss["review_rulings"] = rulings
    if archived:
        gloss["archived_flags"] = {"counts": dict(sorted(counts.items())), "total": len(archived)}
        if gloss_path:
            # Call time: glossary_verify imports this module.
            from dubtitlerr.glossary import glossary_verify

            glossary_verify._append_archived(gloss_path + ".archived.jsonl", archived)
    if gloss_path:
        _append_audit(gloss_path, decisions)

    return {
        "archived": len(archived),
        "counts": dict(sorted(counts.items())),
        "flagged": len(flagged),
        "tiers": tiers,
        "queued": queued,
        "decisions": decisions,
    }


def undo(gloss: dict, term: str) -> dict:
    """Remove an AUTO decision for ``term``: its ruling, and a fix this policy wrote.

    A human-owned fix / acquired record (run == "review") is never touched -- undo reverses the
    machine, not the operator.
    """
    g = json.loads(json.dumps(gloss))
    key = _fold(term)
    rulings = g.get("review_rulings") or {}
    ruling = rulings.pop(key, None) if isinstance(rulings, dict) else None
    if ruling and ruling.get("decision") == APPLY:
        acquired = g.get("acquired") or {}
        ak = next((k for k in acquired if _fold(k) == key), None)
        rec = acquired.get(ak) or {} if ak is not None else {}
        if str(rec.get("run", "")).startswith("auto:"):
            fixes = g.get("hard_fixes") or {}
            fk = next((k for k in fixes if _fold(k) == key), None)
            if fk is not None and fixes[fk] == rec.get("canonical"):
                fixes.pop(fk)
            acquired.pop(ak, None)
    for field in ("review_rulings", "acquired", "hard_fixes"):
        if field in g and not g[field]:
            g.pop(field, None)
    return g


def cached_titles(gloss: dict, gloss_path: str):
    """Cached wiki titles for this glossary, or [] (no network here: VERIFY refreshed the cache)."""
    from dubtitlerr.glossary import glossary_audit

    show = str(gloss.get("show") or "")
    stem = os.path.splitext(os.path.basename(gloss_path))[0]
    titles, _api = glossary_audit.cached_titles(show, stem)
    return titles


def _write(gloss_path: str, gloss: dict) -> None:
    from dubtitlerr.glossary.glossary_acquire import _write_json

    _write_json(gloss_path, gloss)


def sweep(paths: list, titles=None, dry_run=False) -> dict:
    """Backfill: apply the policy to each glossary file. Returns {"files": [report...], totals...}."""
    reports = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            reports.append({"glossary": path, "note": f"load-failed: {e}", "archived": 0})
            continue
        if not isinstance(doc, dict):
            reports.append({"glossary": path, "note": "load-failed: not a JSON object", "archived": 0})
            continue
        show_titles = titles if titles is not None else cached_titles(doc, path)
        rep = apply_policy(doc, None if dry_run else path, titles=show_titles)
        changed = bool(rep["archived"] or rep["tiers"]["apply"]["applied"] or rep["tiers"]["ruled"])
        if changed and not dry_run:
            # One undo copy before a bulk rewrite: the archive log and per-term --undo are the
            # fine-grained history, this is the "put the file back" escape hatch.
            try:
                import datetime
                import shutil

                stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                shutil.copy2(path, f"{path}.bak-p32-{stamp}")
            except OSError:
                # The `.bak` is operator insurance: a failed copy must not block the policy run.
                pass
            try:
                _write(path, doc)
            except OSError as e:
                rep["note"] = f"write-failed: {e}"
                changed = False
        rep.update(glossary=path, show=str(doc.get("show") or ""), titles=len(show_titles or []), dry_run=dry_run)
        reports.append(rep)
    return {
        "files": reports,
        "archived": sum(r.get("archived", 0) for r in reports),
        "applied": sum(r.get("tiers", {}).get("apply", {}).get("applied", 0) for r in reports),
        "queued": sum(r.get("flagged", 0) for r in reports),
        "dry_run": dry_run,
    }


def _load_titles(path):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    titles = doc.get("titles") if isinstance(doc, dict) else doc
    if not isinstance(titles, list):
        raise ValueError("expected a JSON list of titles")
    return [t for t in titles if isinstance(t, str)]


def _expand(args) -> list:
    paths = list(args.glossary)
    for d in args.dir:
        try:
            names = sorted(os.listdir(d))
        except OSError as e:
            print(json.dumps({"note": f"dir-unreadable: {e}"}), flush=True)
            sys.exit(2)
        for name in names:
            if name.endswith(".json") and not SIDECAR.search(name):
                paths.append(os.path.join(d, name))
    return paths


def main() -> None:
    ap = argparse.ArgumentParser(description="P32 review policy: drain queue noise per reason tier, keep durable verdicts.")
    ap.add_argument("glossary", nargs="*", help="path(s) to <show>.json")
    ap.add_argument("--dir", action="append", default=[], help="also sweep every *.json in this glossary directory")
    ap.add_argument("--titles", metavar="FILE", help="JSON list (or {titles:[...]}) used instead of the show's wiki cache")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument("--undo", metavar="TERM", help="remove one auto decision (needs exactly one glossary)")
    a = ap.parse_args()
    titles = None
    if a.titles:
        try:
            titles = _load_titles(a.titles)
        except (OSError, ValueError) as e:
            print(json.dumps({"note": f"titles-unreadable: {e}"}), flush=True)
            sys.exit(2)
    if a.undo:
        if len(a.glossary) != 1:
            print(json.dumps({"note": "--undo needs exactly one glossary path"}), flush=True)
            sys.exit(2)
        path = a.glossary[0]
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            print(json.dumps({"note": f"load-failed: {e}"}), flush=True)
            sys.exit(2)
        out = undo(doc, a.undo)
        if not a.dry_run:
            _write(path, out)
        print(json.dumps({"glossary": path, "undo": a.undo, "dry_run": a.dry_run}), flush=True)
        return
    paths = _expand(a)
    if not paths:
        print(json.dumps({"note": "no glossary given"}), flush=True)
        sys.exit(2)
    rep = sweep(paths, titles, a.dry_run)
    print(json.dumps(rep, ensure_ascii=False), flush=True)
    sys.exit(1 if any(r.get("note", "").startswith(("load-failed", "write-failed")) for r in rep["files"]) else 0)


if __name__ == "__main__":
    main()


# --- P33: the glossary queue obeys the same foreign policy as every other queue -------------
# P32 resolved noise at generation; this drains what is already queued. Both use the same
# decision-record shape, so one audit feed and one undo path serve the whole system.

EXPIRY_DAYS = 14
MAX_FLAGS = 200
UNDO_WINDOW = 50  # dismissed entries kept in-document for undo; the JSONL holds the rest


def _flag_meta(meta) -> dict:
    """The stored flagged entry as a dict, keeping a legacy bare-string reason."""
    if isinstance(meta, dict):
        return dict(meta)
    return {"reason": str(meta or "")}


def _first_seen(rec: dict, now: float) -> float:
    """Epoch seconds of a flag's first sighting; restamps ``now`` when absent or unparseable.

    Tolerated on purpose: ``first_seen`` is stored data and can be a hand-edited ISO string.
    A sweep that raised on one such glossary would leave every later queue unbounded, which is
    exactly the failure this sweep exists to prevent. An unreadable age is restamped rather
    than trusted, so it costs one window instead of deciding the question.
    """
    value = rec.get("first_seen")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    parsed = queue_policy.parse_time(value)
    if parsed is None:
        parsed = now
        rec["first_seen"] = now
    return parsed


def expire_flags(gloss: dict, gloss_path: str | None = None, now=None, days: float = EXPIRY_DAYS, limit: int = MAX_FLAGS) -> dict:
    """Stamp unknown-age flags, dismiss the stale, and cap the queue. Mutates ``gloss``.

    A flagged entry carries no timestamp, so the first sweep that sees it stamps ``first_seen``
    and keeps it; only a later sweep can expire it. That is deliberate: an unknown age must
    cost one window, never the question. Dismissed entries move to ``archived_flags`` with
    their evidence and can be restored by ``undo_flag``.
    """
    now = time.time() if now is None else float(now)
    flagged = gloss.get("flagged")
    if not isinstance(flagged, dict):
        return {"expired": [], "bounded": [], "queued": len(flagged or {}) if isinstance(flagged, list) else 0}
    expired, kept = [], {}
    for term, meta in flagged.items():
        rec = _flag_meta(meta)
        if not rec.get("first_seen"):
            rec["first_seen"] = now
            kept[term] = rec
            continue
        age_days = (now - _first_seen(rec, now)) / 86400.0
        if age_days > days:
            rec.update(expired=True, expired_at=now, expired_days=int(age_days), reversible=True)
            expired.append({"term": term, **rec})
        else:
            kept[term] = rec
    bounded = []
    if len(kept) > limit:
        oldest = sorted(kept.items(), key=lambda kv: _first_seen(kv[1], now))[: len(kept) - limit]
        for term, rec in oldest:
            rec.update(expired=True, expired_at=now, expired_days=int((now - _first_seen(rec, now)) / 86400.0))
            rec["expired_reason"] = "queue-bound"
            expired.append({"term": term, **rec})
            bounded.append({"term": term, "reason": rec.get("reason")})
            kept.pop(term, None)
    if kept:
        gloss["flagged"] = kept
    else:
        gloss.pop("flagged", None)
    if expired:
        arch = gloss.get("archived_flags") if isinstance(gloss.get("archived_flags"), dict) else {}
        counts = dict(arch.get("counts") or {})
        for rec in expired:
            reason = str(rec.get("reason") or "")
            counts[reason] = int(counts.get(reason) or 0) + 1
        # `recent` is the bounded undo window, not a history: the full evidence goes to the
        # archived JSONL (the audit trail), so this list cannot become the next growing pile.
        recent = [r for r in (arch.get("recent") or []) if isinstance(r, dict)] + expired
        gloss["archived_flags"] = {
            "counts": dict(sorted(counts.items())),
            "total": int(arch.get("total") or 0) + len(expired),
            "expired": int(arch.get("expired") or 0) + len(expired),
            "at": round(now, 3),
            "recent": recent[-UNDO_WINDOW:],
        }
        gloss["queue_policy"] = {
            "version": "p33-v1",
            "at": round(now, 3),
            "days": days,
            "limit": limit,
            "queued": len(kept),
        }
        if gloss_path:
            # Full evidence to the archive log; `recent` above is only the bounded undo window.
            try:
                from dubtitlerr.glossary import glossary_verify

                glossary_verify._append_archived(
                    gloss_path + ".archived.jsonl",
                    [
                        {
                            "term": r.get("term"),
                            "reason": r.get("reason"),
                            **{k: v for k, v in r.items() if k not in ("term", "reason")},
                        }
                        for r in expired
                    ],
                )
            except OSError:
                # Archive evidence is advisory: a failed append leaves the ruling itself in the glossary.
                pass
    return {"expired": expired, "bounded": bounded, "queued": len(kept)}


def undo_flag(gloss: dict, term: str) -> dict | None:
    """Restore one auto-dismissed flag. Returns the restored entry, or None if it was not expired.

    The restored entry is given a fresh ``first_seen`` so the next sweep does not immediately
    re-expire it -- an undo is a decision, and a decision deserves a full window."""
    flagged = gloss.get("flagged")
    flagged = dict(flagged) if isinstance(flagged, dict) else {}
    key = _fold(term)
    if any(_fold(t) == key for t in flagged):
        return None
    archived = gloss.get("archived_flags")
    if not isinstance(archived, dict):
        return None
    recent = [r for r in (archived.get("recent") or []) if isinstance(r, dict)]
    idx = next((i for i, r in enumerate(recent) if _fold(r.get("term")) == key), None)
    if idx is None:
        return None
    rec = dict(recent.pop(idx))
    name = str(rec.pop("term"))
    for field in ("expired", "expired_at", "expired_days", "expired_reason", "reversible"):
        rec.pop(field, None)
    rec["first_seen"] = time.time()
    rec["restored_at"] = rec["first_seen"]
    flagged[name] = rec
    gloss["flagged"] = flagged
    archived["total"] = max(0, int(archived.get("total") or 0) - 1)
    archived["expired"] = max(0, int(archived.get("expired") or 0) - 1)
    archived["recent"] = recent
    gloss["archived_flags"] = archived
    return {"term": name, **rec}


def decision_record(term, reason, decision, guard, now, show, **extra) -> dict:
    """One audit record in the P32 shape -- the single decision vocabulary every reader uses."""
    rec = {
        "at": round(float(now), 3),
        "term": term,
        "reason": reason,
        "decision": decision,
        "tier": 0,
        "canonical": None,
        "applied": None,
        "guard": guard,
        "by": "auto",
        "policy": "p33-v1",
        "show": show,
    }
    rec.update(extra)
    return rec
