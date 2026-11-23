#!/usr/bin/env python3
"""Offline acceptance replay for the R3 role-aware repair gates.

Re-judges the proposals the live repair stage already REFUSED, using the stored
``.dubtitles.unresolved.jsonl`` entries as the only input: original text, proposed text, the
reference window that entry recorded, and the card duration recovered from the sibling
``.dubtitles.conf.json``. No model is called, no pipeline stage runs, and nothing is written.

Why a replay: the R3 change (native dub-caption references vouch for the words a repair adds)
is a relaxation, and a relaxation is only safe if you can see exactly which refusals it flips
and that it flips nothing else. The queue is the honest corpus -- every pair here was
produced by a real model on a real episode -- and each entry's stored ``reference`` is the
material the gate actually saw.

Roles: entries from a native dub-caption show are re-judged with ``ref_role='dub'`` (per the
design's DDD acceptance cases); every other show is re-judged with ``ref_role='translation'``,
which must produce the SAME verdict as the classic gates. A non-zero ``regressions`` column
means the role-aware path changed a translation-role decision and the diff is not safe.

The table prints COUNTS ONLY. Subtitle text is never written to stdout, a file, or a commit.

Usage:
    python3 tools/gate_replay.py "/mnt/tdarr-media/anime-tv" --dub-show "DAN DA DAN"
    python3 tools/gate_replay.py path/to/episode.dubtitles.unresolved.jsonl --verbose

Exit status: 0 when every show replayed, 1 when a translation-role regression was found.
"""

import argparse
import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.core import (
    common,  # noqa: E402
    decisions,  # noqa: E402
)
from dubtitlerr.pipeline import repair  # noqa: E402

JUDGED_REASONS = ("rejected_guard", "rejected_name_invented", "rejected_name_part")
DEFAULT_DURATION = 6.0  # only for an entry whose conf.json lost the card; counted, never silent


def _stems(root):
    """Every episode stem under ``root`` that has an unresolved queue file."""
    if os.path.isfile(root):
        name = os.path.basename(root)
        return [root[: -len(".dubtitles.unresolved.jsonl")]] if name.endswith(".dubtitles.unresolved.jsonl") else []
    out = []
    for path in sorted(glob.glob(os.path.join(root, "**", "*.dubtitles.unresolved.jsonl"), recursive=True)):
        out.append(path[: -len(".dubtitles.unresolved.jsonl")])
    return out


def _durations(stem):
    """Map ``decisions.key(original_text)`` -> card duration in seconds, from the stored conf.

    A queue entry carries no timing (unresolved.card_starts derives it the same way), so the
    card the proposal was judged on is found by text identity. Missing/duplicate cards simply
    fall out of the map; the caller counts those and uses DEFAULT_DURATION.
    """
    out = {}
    cards = common.read_conf(stem)
    if cards is None:
        return out
    for c in cards:
        if not isinstance(c, dict):
            continue
        try:
            dur = float(c["end"]) - float(c["start"])
        except (KeyError, TypeError, ValueError):
            continue
        out.setdefault(decisions.key(str(c.get("text") or "")), dur)
    return out


def _show_of(stem):
    """The show directory a queue file belongs to (one level above ``Season NN``), or its basename."""
    season = os.path.dirname(stem)
    if os.path.basename(season).startswith("Season"):
        return os.path.basename(os.path.dirname(season))
    return os.path.basename(season)


def _role_for(show, dub_shows):
    return "dub" if any(s.lower() in show.lower() for s in dub_shows) else "translation"


def replay(stems, dub_shows, gloss=None):
    """Count classic vs role-aware verdicts per show over every stored refusal.

    Returns (rows, flips, notes): ``rows`` is per-show counters, ``flips`` the classic-guard ->
    outcome crosstab for proposals the R3 path newly admits, ``notes`` collects inputs that
    were unusable (no texts, no duration) so the counts can be read honestly.
    """
    rows = collections.OrderedDict()
    flips = collections.Counter()
    notes = collections.Counter()
    for stem in stems:
        show = _show_of(stem)
        row = rows.setdefault(show, collections.Counter())
        durations = _durations(stem)
        role = _role_for(show, dub_shows)
        for e in _entries(stem):
            row["entries"] += 1
            if e.get("stage") != "repair" or e.get("reason") not in JUDGED_REASONS:
                continue
            if not e.get("original_text") or not e.get("proposed_text"):
                notes["no_text_pair"] += 1
                continue
            row["judged"] += 1
            stored_role = e.get("ref_role")
            entry_role = stored_role or role
            dur = durations.get(decisions.key(str(e["original_text"])))
            if dur is None:
                notes["no_duration"] += 1
                dur = DEFAULT_DURATION
            ref = str(e.get("reference") or "")
            klass = repair.rejecting_guard(str(e["original_text"]), str(e["proposed_text"]), ref, dur, gloss, ref_role=None)
            new = repair.rejecting_guard(str(e["original_text"]), str(e["proposed_text"]), ref, dur, gloss, ref_role=entry_role)
            row["classic_ok"] += klass is None
            row["new_ok"] += new is None
            if klass is not None and new is None:
                row["newly_accepted"] += 1
                flips[(entry_role, klass)] += 1
            if klass is None and new is not None:
                row["newly_rejected"] += 1
                flips[(entry_role, f"ACCEPTED->{new}")] += 1
            if entry_role != "dub" and klass != new:
                row["regressions"] += 1
    return rows, flips, notes


def _entries(stem):
    path = stem + ".dubtitles.unresolved.jsonl"
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue  # torn final line, same rule as unresolved.items()
                if isinstance(e, dict):
                    yield e
    except OSError:
        return


def _gloss_for(stem):
    """The show's glossary when one is deployed, else the empty glossary.

    The name guards read it, so replaying with the wrong one would misreport ``invents_name``.
    GLOSSARY_DIR is honoured; a missing file is not fatal (repair.gl() semantics: no names).
    """
    from dubtitlerr.glossary import glossary

    show = _show_of(stem)
    path = os.path.join(repair.GLOSSARY_DIR, show + ".json")
    try:
        return glossary.load(path)
    except Exception:  # noqa: BLE001 - a replay is observability, never a failure
        return glossary.load_dict({"names": [], "hard_fixes": {}})


def table(rows):
    head = f"{'show':44} {'entries':>8} {'judged':>7} {'classic':>8} {'r3':>6} {'+accept':>8} {'+reject':>8} {'regress':>8}"
    lines = [head, "-" * len(head)]
    for show, r in rows.items():
        lines.append(
            f"{show[:44]:44} {r['entries']:>8} {r['judged']:>7} {r['classic_ok']:>8} {r['new_ok']:>6}"
            f" {r['newly_accepted']:>8} {r['newly_rejected']:>8} {r['regressions']:>8}"
        )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Offline acceptance replay for the R3 role-aware repair gates.")
    ap.add_argument("roots", nargs="+", help="library root, show directory, or one .dubtitles.unresolved.jsonl")
    ap.add_argument("--dub-show", action="append", default=None, help="show substring judged as ref_role=dub (repeatable)")
    ap.add_argument("--verbose", action="store_true", help="also print the classic-guard crosstab and unusable-input counts")
    a = ap.parse_args(argv)

    dub_shows = a.dub_show or ["DAN DA DAN"]
    stems = [s for root in a.roots for s in _stems(root)]
    if not stems:
        print("no .dubtitles.unresolved.jsonl found under: " + ", ".join(a.roots))
        return 2

    # One glossary per show: the guards read it, and a replay on the wrong one lies about
    # invents_name. Loaded once per show directory rather than once (or N times) per episode.
    by_show = {}
    for stem in stems:
        by_show.setdefault(_show_of(stem), _gloss_for(stem))

    rows, flips, notes = collections.OrderedDict(), collections.Counter(), collections.Counter()
    for show in by_show:
        if not os.path.exists(os.path.join(repair.GLOSSARY_DIR, show + ".json")):
            notes["no_glossary"] += 1  # counted, so an empty-glossary replay is never read as deployed
    for show, gloss in by_show.items():
        show_rows, show_flips, show_notes = replay([s for s in stems if _show_of(s) == show], dub_shows, gloss)
        rows.update(show_rows)
        flips.update(show_flips)
        notes.update(show_notes)

    print(table(rows))
    if a.verbose:
        print("\nnewly accepted, by (role, classic guard):")
        for (role, guard), n in sorted(flips.items()):
            print(f"  {role:11} {guard:20} {n:>5}")
        if notes:
            print("\nunusable inputs (excluded or defaulted):")
            for k, n in sorted(notes.items()):
                print(f"  {k:20} {n:>5}")

    regressions = sum(r["regressions"] for r in rows.values())
    if regressions:
        print(f"\nREGRESSION: {regressions} translation-role verdict(s) changed -- the R3 diff is not safe")
        return 1
    print("\ntranslation-role verdicts unchanged: 0 regressions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
