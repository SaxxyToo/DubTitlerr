#!/usr/bin/env python3
"""Queue a whole library from the folder tree when no media server is configured.

The watch queue answers "what is live?" from Plex, WatchState or Jellyfin. A fresh
install has none of those, and the old fallback was a hand-written anime_order.txt.
This is that fallback: every top-level folder under ANIME_ROOT that holds a video
becomes a show, ordered by its newest episode so a newly added arc reaches the builder
first.

It refuses to overwrite a non-empty order file. A hand-written list, or one the watch
queue just produced, is an operator decision; this is only the bootstrap.

Stdlib only, deliberately: gen_loop.sh invokes it from the minimal app dir with no
pysubs2 (see atomic_file.py's and media_types.py's docstrings for the same rule).
"""

from __future__ import annotations

import argparse
import os
import sys

from dubtitlerr.core.atomic_file import atomic_write_bytes
from dubtitlerr.core.media_types import VIDEO_EXTS

# Directories no library should queue as a show. Case-insensitive.
IGNORE_DIRS = {"@eadir", "#recycle", "lost+found", "system volume information"}


def _newest_video(show_dir):
    """The mtime of the show's newest video, or 0.0 when it holds none."""
    newest = 0.0
    for dp, dns, files in os.walk(show_dir):
        dns[:] = [d for d in dns if not d.startswith(".") and d.lower() not in IGNORE_DIRS]
        for fn in files:
            if not fn.lower().endswith(VIDEO_EXTS):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dp, fn)))
            except OSError:
                continue
    return newest


def scan(root):
    """``[(show name, newest video mtime)]`` newest first, then name.

    Top-level directories only: the library layout is ``Show (Year)/Season NN/``. A
    directory with no video anywhere below it is skipped rather than queued for an
    empty sweep.
    """
    shows = []
    for entry in os.scandir(root):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name.lower() in IGNORE_DIRS:
            continue
        newest = _newest_video(entry.path)
        if newest:
            shows.append((entry.name, newest))
    shows.sort(key=lambda s: (-s[1], s[0].lower()))
    return shows


def main(argv=None):
    args_list = sys.argv[1:] if argv is None else list(argv)
    ap = argparse.ArgumentParser(description="Queue a whole library from the folder tree.")
    ap.add_argument("--root", default=os.environ.get("ANIME_ROOT", "/media/Anime Library"))
    ap.add_argument("--out", default=os.environ.get("ANIME_ORDER", "/config/anime_order.txt"))
    ap.add_argument("--force", action="store_true", help="overwrite a non-empty order file")
    ap.add_argument("--dry-run", action="store_true", help="print the queue, write nothing")
    args = ap.parse_args(args_list)

    if not os.path.isdir(args.root):
        print(f"library_queue: root not found: {args.root}", file=sys.stderr)
        return 1
    try:
        has_content = os.path.getsize(args.out) > 0
    except OSError:
        has_content = False
    if has_content and not args.force and not args.dry_run:
        print(f"library_queue: {args.out} already has shows; leaving it alone (use --force)", file=sys.stderr)
        return 1

    try:
        shows = scan(args.root)
    except OSError as e:
        print(f"library_queue: cannot read {args.root}: {e}", file=sys.stderr)
        return 1
    names = [name for name, _ in shows]
    if not names:
        print(f"library_queue: no shows with video under {args.root}", file=sys.stderr)
        return 1
    if args.dry_run:
        print("\n".join(names))
        return 0

    parent = os.path.dirname(args.out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    atomic_write_bytes(args.out, "\n".join(names) + "\n", mode=0o644)
    print(f"library_queue: {len(names)} show(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
