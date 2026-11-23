#!/usr/bin/env python3
"""Offline sweep for whisper boilerplate/credits in already-delivered sidecars.

Arm #5 (dubtitlerr/pipeline/boilerplate.py) keeps future output clean; this tool is the
deterministic cleanup pass for sidecars that shipped before it existed. A 2026-11-20
library sweep found 135 files in this class: ~80 "Thanks for watching" tails (old-era
deliveries), ~50 credit header lines ("Original Script: ...", "Subtitles by the Amara.org
community"), and one four-cue Touhou disclaimer block in Afro Samurai S01E01.

Modes:
  scan ROOT          walk for sidecars, one TSV row per matching file: file, hits, first, last
  fix FILE [FILE..]  (SRT only) drop a LEADING or TRAILING run of boilerplate cues, keeping
                     a backup first. Mid-file matches are reported, never auto-dropped.

The templates come from the pipeline module -- one list, so the sweep and the pipeline
cannot disagree about what boilerplate is. A file with no matches produces no scan row.
Nothing is removed from the middle of an episode, and nothing is removed at all without
a `.pre-artifact-fix-<ts>` backup beside it.

Usage:
  python3 tools/artifact_sweep.py scan /path/to/library > hits.tsv
  python3 tools/artifact_sweep.py fix --dry-run /path/to/library/*.dub.en.srt
  python3 tools/artifact_sweep.py fix /path/to/library/*.dub.en.srt
"""

import argparse
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.pipeline import boilerplate  # noqa: E402

# The delivered sidecar names this pipeline writes. Backups and parked copies are skipped
# by name, so a sweep over a directory that already holds one cannot re-report it.
SIDECAR_SUFFIXES = (".dub.en.srt", ".dub.en.ass", ".eng.dubtitles.srt")

DEFAULT_AUDIT = "/tmp/artifact-fix-audit.jsonl"


def is_sidecar(path):
    base = os.path.basename(path)
    if ".bak" in base or ".pre-" in base or ".stale" in base:
        return False
    return base.endswith(SIDECAR_SUFFIXES)


def parse_srt(text):
    """Cues as {idx, ts, text}; the seed implementation's parser, kept byte-faithful.

    Cue text is joined with a single space: the filter matches whole cues, and a template
    split across two display lines ("This video is a derivative" / "work of the Touhou
    Project.") is still one cue.
    """
    blocks = re.split(r"\n\s*\n", text.strip())
    cues = []
    for b in blocks:
        lines = b.strip().split("\n")
        if len(lines) >= 3 and "-->" in lines[1]:
            cues.append({"idx": lines[0].strip(), "ts": lines[1].strip(), "text": " ".join(lines[2:]).strip()})
    return cues


def render_srt(cues):
    """Renumber 1..N, keep every timestamp verbatim."""
    out = []
    for n, c in enumerate(cues, 1):
        out.append(f"{n}\n{c['ts']}\n{c['text']}\n")
    return "\n".join(out)


def srt_hits(path):
    cues = parse_srt(open(path, encoding="utf-8", errors="replace").read())  # noqa: SIM115
    return [c["text"] for c in cues if boilerplate.is_boilerplate(c["text"])]


def ass_hits(path):
    """.ass is scanned line by line, not as events.

    The credits we are looking for live in the .ass HEADER that pysubs2 writes back out
    ("Original Script: Kitsune-HentaiBot [...]"), not in dialogue events, so an
    event-level scan would miss every one of them. Line matching cannot see a split
    multi-line event, but it also cannot produce a false drop: nothing here is fixed
    in .ass.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        return [ln.strip() for ln in fh if boilerplate.is_boilerplate(ln)]


def hits_for(path):
    return srt_hits(path) if path.endswith(".srt") else ass_hits(path)


def scan(root):
    rows = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            if not is_sidecar(path):
                continue
            try:
                hits = hits_for(path)
            except OSError:
                continue
            if hits:
                rows.append((path, len(hits), hits[0][:80], hits[-1][:80]))
    for path, n, first, last in rows:
        print(f"{path}\t{n}\t{first}\t{last}")
    print(f"## SCAN: {len(rows)} files with matches; {len(rows)} rows", file=sys.stderr)
    return rows


def fix_one(path, dry_run=False, audit_path=DEFAULT_AUDIT):
    """Drop the leading/trailing boilerplate runs from one SRT. Returns the audit dict.

    The decision is boilerplate.drop_indices(), the same function the pipeline calls, so
    a sweep cannot remove a cue the pipeline would have kept. Returns None for an
    unparseable file; the caller prints the reason.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        cues = parse_srt(fh.read())
    if not cues:
        return None
    drops, audits = boilerplate.drop_indices(cues)
    if not drops:
        mid = [a["text"][:60] for a in audits]
        print(f"OK (no head/tail artifacts): {path}" + (f" [mid hits not touched: {mid}]" if mid else ""))
        return {"file": path, "dropped_cues": 0, "kept": len(cues), "mid_hits_not_touched": mid}
    kept = [c for i, c in enumerate(cues) if i not in drops]
    dropped = [a["text"][:60] for a in audits if a["dropped"]]
    mid = [a["text"][:60] for a in audits if a["run"] == "midfile"]
    audit = {
        "file": path,
        "dropped_cues": len(drops),
        "kept": len(kept),
        "dropped_texts": dropped,
        "mid_hits_not_touched": mid,
    }
    if dry_run:
        print(json.dumps({**audit, "dry_run": True}))
        return audit
    bak = path + ".pre-artifact-fix-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_srt(kept) + "\n")
    audit["backup"] = bak
    print(json.dumps(audit))
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(audit) + "\n")
    return audit


def fix(paths, dry_run=False, audit_path=DEFAULT_AUDIT):
    for path in paths:
        if path.endswith(".ass"):
            print(f"SKIP (ass is never auto-fixed; scan reports it): {path}")
            continue
        try:
            result = fix_one(path, dry_run=dry_run, audit_path=audit_path)
        except OSError as e:
            print(f"SKIP ({e}): {path}")
            continue
        if result is None:
            print(f"SKIP (not parseable srt): {path}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Offline sweep for whisper boilerplate/credits in delivered sidecars.")
    ap.add_argument("mode", choices=["scan", "fix"])
    ap.add_argument("paths", nargs="+", help="scan: one ROOT directory; fix: SRT files")
    ap.add_argument("--dry-run", action="store_true", help="fix: report, write nothing")
    ap.add_argument("--audit", default=DEFAULT_AUDIT, help=f"fix: JSONL audit path (default {DEFAULT_AUDIT})")
    args = ap.parse_args(argv)
    if args.mode == "scan":
        scan(args.paths[0])
    else:
        fix(args.paths, dry_run=args.dry_run, audit_path=args.audit)


if __name__ == "__main__":
    main()
