#!/usr/bin/env python3
"""Repair-bench standings: re-judge a saved round with the live acceptance gate, split by edit kind.

Usage:
    python3 tools/repair_standings.py RESULTS_DIR --glossary GLOSSARY.json [--dur 6.0] [--json]

RESULTS_DIR holds one saved JSON per episode per model, as the round runners write them:
either ``<Ep>.json`` (one model per directory) or ``<Ep>.<model>.json`` (models mixed in one
directory). Rows are the runner's dicts (``orig``, ``out``, ``ref``, ...). No model is called
and nothing is written; every saved proposal is re-judged offline by the live
``repair.rejecting_guard`` -- the same function production ships.

Why the split. The gate cannot judge meaning, so raw "admitted" counts rank punctuation churn
first (measured 2026-11-27: the leading local model topped "admitted" partly on period deletions
the gate permits). Every comparison since the round-5 analysis reports by edit kind instead:

    glyph  - case/punctuation-only (renders near-identically)
    contra - contraction expansion ("don't" -> "do not")
    borrow - imported a word the fansub reference carries
    edit   - a real repair (the comparable column)

Output is COUNTS ONLY: the corpus is a commercial dub's script, so subtitle text is never
printed to stdout, written to a file, or committed. Columns: rows, err, adm, glyph, contra,
borrow, edit.

Exit status: 0 on success, 2 when no results were found or a file failed to parse.
"""

import argparse
import collections
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.glossary import glossary as glossary_mod  # noqa: E402
from dubtitlerr.pipeline import repair  # noqa: E402

WORD = re.compile(r"[a-z']+")
PUNCT_STRIP = re.compile(r"[^a-z0-9]")
CONTRA = [
    (re.compile(r"n't\b"), " not"),
    (re.compile(r"'re\b"), " are"),
    (re.compile(r"'ve\b"), " have"),
    (re.compile(r"'ll\b"), " will"),
    (re.compile(r"'m\b"), " am"),
    (re.compile(r"'d\b"), " would"),
    (re.compile(r"'s\b"), " is"),
]


def _words(s):
    return WORD.findall((s or "").lower())


def _glyph_form(s):
    return PUNCT_STRIP.sub("", (s or "").lower())


def _contra_form(s):
    t = (s or "").lower()
    for pat, rep in CONTRA:
        t = pat.sub(rep, t)
    return PUNCT_STRIP.sub("", t)


def classify(orig, new, ref):
    """The edit kind of an admitted, changed proposal (see the module docstring)."""
    if _glyph_form(orig) == _glyph_form(new):
        return "glyph"
    if _contra_form(orig) == _contra_form(new):
        return "contra"
    had, in_ref = set(_words(orig)), set(_words(ref))
    added = [w for w in _words(new) if w not in had]
    if any(w in in_ref for w in added):
        return "borrow"
    return "edit"


def _rows(path):
    """The list of proposal dicts in one saved episode file, or [] when absent/unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    for value in data.values():
        if isinstance(value, list):
            return value
    return []


def _model_of(path):
    """The model a saved file belongs to: ``<Ep>.<model>.json`` names it; ``<Ep>.json`` does not.

    Model names may themselves contain dots (``qwen3.5:4b``), so the name is everything between
    the FIRST and the LAST dot, not the first segment after position 0.
    """
    name = os.path.basename(path)
    if name.count(".") >= 2:
        return name.split(".", 1)[1].rsplit(".", 1)[0]
    return os.path.basename(os.path.dirname(path))


def score_results(results_dir, gloss, dur):
    """Per-model counters for one results directory (see the module docstring for the columns)."""
    agg = collections.defaultdict(collections.Counter)
    files = sorted(glob.glob(os.path.join(results_dir, "*.json")))
    if not files:
        return None
    for path in files:
        rows = _rows(path)
        if rows is None:
            return None
        model = _model_of(path)
        for row in rows:
            orig = (row.get("orig") or "").strip()
            new = (row.get("out") or "").strip()
            ref = (row.get("ref") or "").strip()
            if not orig:
                continue
            counts = agg[model]
            counts["rows"] += 1
            if not new or "<ERROR" in new or "<EMPTY>" in new:
                counts["err"] += 1
                continue
            if repair.rejecting_guard(orig, new, ref, dur, gloss) is not None:
                continue
            if new == orig:
                continue
            counts["adm"] += 1
            counts[classify(orig, new, ref)] += 1
    return agg


def main(argv=None):
    ap = argparse.ArgumentParser(description="Repair-bench standings, split by edit kind.")
    ap.add_argument("results_dir", help="directory of saved per-episode round JSON files")
    ap.add_argument("--glossary", required=True, help="glossary JSON used for the round")
    ap.add_argument("--dur", type=float, default=6.0, help="card duration for re-judging (rows carry none)")
    ap.add_argument("--json", action="store_true", help="print machine-readable counters instead of a table")
    args = ap.parse_args(argv)

    with open(args.glossary, encoding="utf-8") as handle:
        gloss = glossary_mod.load_dict(json.load(handle))
    agg = score_results(args.results_dir, gloss, args.dur)
    if agg is None:
        print("no usable results under", args.results_dir, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({m: dict(c) for m, c in sorted(agg.items())}, indent=2, sort_keys=True))
        return 0
    print(f"{'model':44s} {'rows':>4s} {'err':>4s} {'adm':>4s} {'glyph':>5s} {'contra':>6s} {'borrow':>6s} {'edit':>5s}")
    for model, counts in sorted(agg.items(), key=lambda kv: -kv[1].get("edit", 0)):
        print(
            f"{model:44s} {counts['rows']:4d} {counts['err']:4d} {counts['adm']:4d} {counts['glyph']:5d} "
            f"{counts['contra']:6d} {counts['borrow']:6d} {counts['edit']:5d}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
