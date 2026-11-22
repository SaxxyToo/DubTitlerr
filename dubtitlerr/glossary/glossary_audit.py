#!/usr/bin/env python3
"""Names-hygiene: REPORT junk names a legacy intake left in a glossary, and (--fix) REMOVE them.

Why this exists (P22). Glossary VERIFY/ACQUIRE already CLASSIFY a bad term -- `flagged` holds it as english-word,
sentence-initial-only, common-word, no-match or below-floor -- but nothing ever removed the matching entry from
`names`, and the guards that stop a NEW junk write cannot see a file written before they existed. Frieren was the
outlier: 26 words of the single wiki title "Continental Magic Association" and a pile of generic words sat in
`names` while their flags sat right next to them.

Two detector classes, both cross-checked against the wiki cache:

  flagged        the entry is flagged with a junk reason (JUNK_FLAG_REASONS) and is still in `names`
  ordinary-word  the entry is a capitalised ordinary English word with no exact wiki title -- the wave-2 class the
                 Frieren cleanup found with NO flag at all (legacy intake bypassed the common-word guard)

The exact-wiki-title check is what keeps this from eating real names: "Fern", "Aura", "Hero" and "Land" are ordinary
words AND exact wiki pages, so they are protected. "Continental" is an ordinary word and only a FRAGMENT of a wiki
title, so it is reported. A lowercase entry is never reported by the ordinary-word class: `mana`/`mage` in a curated
names list are a deliberate whisper-prompt vocabulary, not a mis-acquired proper noun.

ACTING (P24). `--fix` removes the reported entries from `names` and writes `<glossary>.cleanup.json` beside the file
with what moved and why; the sweep runs this right after VERIFY, so the loop needs no human step. Without cached wiki
titles NOTHING is removed: the exact-title exemption cannot be applied, so entries stay under `unevaluated` and the
stage reports them instead of guessing. The wiki is never fetched here -- VERIFY has just refreshed the cache.

    python3 -m dubtitlerr.glossary.glossary_audit <glossary.json> [--titles FILE] [--fix]

Exit code 0 = nothing left to fix (clean, or clean after --fix), 1 = junk remains (reported; without --fix nothing is
written, with --fix there was no wiki to decide on), 2 = the glossary could not be read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from dubtitlerr.glossary import glossary, glossary_verify

# Flag reasons that mean "this term is not a name". Everything else in `flagged` is a REAL review item the human
# queue must keep (respelling-needs-review, unseen-needs-evidence, conflicts-with-curated-fix, ...), so it is
# deliberately absent here: a respelling flag says the term exists but is spelled wrong, not that it is junk.
# One list, shared with the verifier's bounded flag storage: an entry archived there as junk is exactly one this
# audit may remove, so the two must never drift.
JUNK_FLAG_REASONS = glossary_verify.ARCHIVED_FLAG_REASONS

MAX_NEAR_TITLES = 3
MAX_CLEANUP_RUNS = 20


def cache_path(show_key: str) -> str:
    """The wiki-cache file glossary_verify.fetch_titles would use for ``show_key`` (same naming, by construction)."""
    return os.path.join(glossary_verify.CACHE_DIR, re.sub(r"[^A-Za-z0-9]+", "_", show_key) + ".json")


def cached_titles(*show_keys: str) -> tuple[list[str], str]:
    """Cached ns=0 titles for the first of ``show_keys`` that has a cache file, plus the wiki api recorded there.

    No network: an absent or unreadable cache returns ([], ""). Several keys are tried because the same glossary can
    be keyed by its ``show`` value or by its file name depending on which stage fetched the cache."""
    for key in show_keys:
        if not key:
            continue
        try:
            doc = json.load(open(cache_path(key)))
        except (OSError, ValueError):
            continue
        titles = doc.get("titles")
        if isinstance(titles, list) and titles:
            return [t for t in titles if isinstance(t, str)], str(doc.get("api") or "")
    return [], ""


def flags_by_term(flagged) -> dict[str, list[str]]:
    """{lowercased term: [reasons]} from a glossary's ``flagged`` map, tolerating the legacy bare-string form."""
    out: dict[str, list[str]] = {}
    if not isinstance(flagged, dict):
        return out
    for term, record in flagged.items():
        if not isinstance(term, str) or not term.strip():
            continue
        reason = record.get("reason") if isinstance(record, dict) else record
        out.setdefault(term.strip().lower(), []).append(str(reason or ""))
    return out


def looks_like_proper_noun(name: str) -> bool:
    """True when the first letter of the entry is uppercase. Junk intake produces capitalised words; a lowercase
    entry in `names` is a curated term (whisper prompt vocabulary), not a harvested name."""
    core = (name or "").strip()
    for ch in core:
        if ch.isalpha():
            return ch.isupper()
    return False


def near_titles(name: str, titles: list[str]) -> list[str]:
    """Wiki titles CONTAINING the entry as a whole word, shortest first -- the evidence for a title fragment.

    'Continental' -> 'Continental Magic Association'. Purely diagnostic; the caller decides nothing from it."""
    pattern = re.compile(r"(?<![A-Za-z])" + re.escape(name.strip()) + r"(?![A-Za-z])", re.IGNORECASE)
    hits = [t for t in titles if pattern.search(t)]
    return sorted(hits, key=lambda t: (len(t), t))[:MAX_NEAR_TITLES]


def audit_gloss(gloss: dict, titles: list[str] | None) -> dict:
    """Audit one glossary dict against ``titles`` (the wiki's ns=0 page titles). Pure; never mutates ``gloss``.

    ``titles`` None or empty means the wiki is unknown: junk-class entries are then listed under ``unevaluated``
    with the reason, because the exact-title exemption cannot be applied and a partial proxy would report real
    names."""
    names = [n for n in (gloss.get("names") or []) if isinstance(n, str) and n.strip()]
    flags = flags_by_term(gloss.get("flagged"))
    title_set = {t.strip().lower() for t in (titles or []) if isinstance(t, str) and t.strip()}
    have_titles = bool(title_set)

    junk: list[dict] = []
    unevaluated: list[dict] = []
    for name in names:
        low = name.strip().lower()
        junk_reasons = [r for r in flags.get(low, []) if r in JUNK_FLAG_REASONS]
        classes = []
        if junk_reasons:
            classes.append("flagged")
        if looks_like_proper_noun(name) and glossary.is_ordinary_word(name):
            classes.append("ordinary-word")
        if not classes:
            continue
        entry = {
            "name": name,
            "classes": classes,
            "flag": junk_reasons[0] if junk_reasons else None,
        }
        if not have_titles:
            unevaluated.append({**entry, "why": "no cached wiki titles"})
            continue
        if low in title_set:
            continue  # an exact wiki page protects the name, however ordinary the word looks
        junk.append({**entry, "near_titles": near_titles(name, titles or [])})

    rep = {
        "show": gloss.get("show") or "",
        "names": len(names),
        "flagged": len(flags),
        "titles": len(title_set),
        "junk": junk,
        "junk_count": len(junk),
    }
    if unevaluated:
        rep["unevaluated"] = unevaluated
    if not have_titles:
        rep["note"] = (
            "no cached wiki titles: the exact-title exemption could not be applied, so junk-class entries are "
            "listed under 'unevaluated' (run glossary verify, or pass --titles)"
        )
    return rep


def audit(gloss_path: str, titles: list[str] | None = None) -> dict:
    """Audit the glossary file at ``gloss_path``; returns the report. Read-only, never raises for bad content."""
    try:
        doc = json.load(open(gloss_path))
    except (OSError, ValueError) as e:
        return {"note": f"load-failed: {e}", "junk": [], "junk_count": 0}
    if not isinstance(doc, dict):
        return {"note": "load-failed: glossary is not a JSON object", "junk": [], "junk_count": 0}
    wiki = str(doc.get("wiki") or "")
    if titles is None:
        show_keys = [str(doc.get("show") or ""), os.path.splitext(os.path.basename(gloss_path))[0]]
        titles, cached_api = cached_titles(*show_keys)
        wiki = wiki or cached_api
    rep = audit_gloss(doc, titles)
    rep["glossary"] = os.path.abspath(gloss_path)
    rep["wiki"] = wiki
    return rep


def cleanup_path(gloss_path: str) -> str:
    """The audit log of what the acting mode removed, beside the glossary."""
    return gloss_path + ".cleanup.json"


def apply_cleanup(gloss: dict, junk: list) -> tuple[dict, list]:
    """Remove the audit's junk entries from ``names`` (in place). Returns (gloss, removed names).

    Only entries the audit actually reported are removed -- it already applied the exact-wiki-title
    exemption, so a real name the wiki has a page for can never be in this list. Keyed
    case-insensitively on the stripped entry.
    """
    drop = {str(j.get("name", "")).strip().lower() for j in junk if str(j.get("name", "")).strip()}
    if not drop:
        return gloss, []
    removed = [n for n in (gloss.get("names") or []) if isinstance(n, str) and n.strip().lower() in drop]
    gloss["names"] = [n for n in (gloss.get("names") or []) if not (isinstance(n, str) and n.strip().lower() in drop)]
    return gloss, removed


def _write_cleanup(path: str, run: dict) -> str:
    """Append ``run`` to the cleanup log (newest 20 kept), atomically. Returns the path."""
    doc = {"runs": []}
    try:
        loaded = json.load(open(path))
        if isinstance(loaded, dict) and isinstance(loaded.get("runs"), list):
            doc = loaded
    except (OSError, ValueError):
        # A missing or corrupt cleanup log just starts a fresh one.
        pass
    doc["runs"] = (doc["runs"] + [run])[-MAX_CLEANUP_RUNS:]
    from dubtitlerr.glossary.glossary_acquire import _write_json  # call time: acquire imports verify

    _write_json(path, doc)
    return path


def fix(gloss_path: str, titles: list[str] | None = None) -> dict:
    """Audit the glossary and REMOVE what the audit reported; log it. Never raises for bad content.

    Returns the audit report plus ``cleanup`` (``{"removed": n, "log": path}``). With no cached
    wiki titles nothing is removed -- the exact-title exemption is the protection, and a guess
    must never delete a name.
    """
    rep = audit(gloss_path, titles)
    rep.setdefault("cleanup", {"removed": 0, "log": None})
    if rep.get("junk_count"):
        try:
            doc = json.load(open(gloss_path))
        except (OSError, ValueError) as e:
            rep["cleanup"] = {"removed": 0, "log": None, "note": f"load-failed: {e}"}
            return rep
        if not isinstance(doc, dict):
            return rep
        before = len(doc.get("names") or [])
        doc, removed = apply_cleanup(doc, rep["junk"])
        if removed:
            try:
                from dubtitlerr.glossary.glossary_acquire import _write_json

                _write_json(gloss_path, doc)
            except OSError as e:
                rep["cleanup"] = {"removed": 0, "log": None, "note": f"write-failed: {e}"}
                return rep
            log = _write_cleanup(
                cleanup_path(gloss_path),
                {
                    "at": round(time.time(), 3),
                    "show": rep.get("show", ""),
                    "wiki": rep.get("wiki", ""),
                    "titles": rep.get("titles", 0),
                    "names_before": before,
                    "names_after": len(doc.get("names") or []),
                    "removed_count": len(removed),
                    "removed": rep["junk"],
                },
            )
            rep["cleanup"] = {"removed": len(removed), "log": log}
        rep["names"] = len(doc.get("names") or [])
    return rep


def main() -> None:
    ap = argparse.ArgumentParser(description="Report (and with --fix remove) junk names in a DubTitlerr glossary.")
    ap.add_argument("glossary", help="path to <show>.json")
    ap.add_argument("--titles", metavar="FILE", help="JSON list of wiki titles to check instead of the cache")
    ap.add_argument("--fix", action="store_true", help="remove the reported entries from `names` (logs .cleanup.json)")
    a = ap.parse_args()
    titles = None
    if a.titles:
        try:
            doc = json.load(open(a.titles))
            titles = doc.get("titles") if isinstance(doc, dict) else doc
        except (OSError, ValueError) as e:
            print(json.dumps({"note": f"titles-unreadable: {e}"}), flush=True)
            sys.exit(2)
        if not isinstance(titles, list):
            print(json.dumps({"note": "titles-unreadable: expected a JSON list"}), flush=True)
            sys.exit(2)
    rep = fix(a.glossary, titles) if a.fix else audit(a.glossary, titles)
    print(json.dumps(rep, ensure_ascii=False), flush=True)
    if rep.get("note", "").startswith("load-failed"):
        sys.exit(2)
    if a.fix and (rep.get("cleanup") or {}).get("removed"):
        sys.exit(0)  # the junk that was reported is gone: the stage is clean
    sys.exit(1 if (rep.get("junk_count") or rep.get("unevaluated")) else 0)


if __name__ == "__main__":
    main()
