#!/usr/bin/env python3
"""Record and verify the payloads the P39 import moves into the artifact store.

The migration deletes the files it imports, so the only way to prove "nothing was lost" is to
hash the media tree BEFORE the import and compare the store's payloads AFTER it.

    # before (read-only; host paths, run wherever the tree is)
    python3 tools/p39_verify.py record --root /mnt/tdarr-media/anime-tv --out pre.json

    # after (host paths in the manifest -> container stems in the store)
    python3 tools/p39_verify.py verify --manifest pre.json \\
        --media-root /mnt/tdarr-media/anime-tv --stem-root /media/anime-tv --sample 25

``verify`` compares sha256 and size for a deterministic random sample of >= ``--sample``
distinct episodes per artifact kind (``--all`` checks every row). A missing or differently
sized payload is a failure; the exit code is non-zero if anything fails.

The stem mapping matters: the store is keyed by the path the pipeline uses, i.e. the container's
``/media/anime-tv/...``, never the host's ``/mnt/tdarr-media/anime-tv/...``. An import run from
the host records host stems, and every reader then misses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys

from dubtitlerr.core import artifact_store


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def cmd_record(args) -> int:
    items = []
    for dirpath, _dirs, names in os.walk(args.root):
        for name in names:
            if ".dubtitles." not in name:
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
                digest = _sha256_file(path)
            except OSError as exc:
                print(f"ERROR {path}: {exc}", file=sys.stderr)
                continue
            items.append({"path": path, "size": st.st_size, "mtime": st.st_mtime, "sha256": digest})
    items.sort(key=lambda r: r["path"])
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"root": os.path.abspath(args.root), "files": len(items), "items": items}, fh, indent=0)
    total = sum(i["size"] for i in items)
    print(json.dumps({"root": os.path.abspath(args.root), "files": len(items), "bytes": total, "out": args.out}))
    return 0


def _stem_for(item_path: str, media_root: str, stem_root: str):
    """Store stem for a recorded path, or (None, None) when it is not an importable artifact."""
    path = os.path.abspath(item_path)
    root = os.path.abspath(media_root)
    if root != path and not path.startswith(root + os.sep):
        return None, None
    tail = path[len(root) :]
    name = os.path.basename(tail)
    stem_tail, kind = artifact_store.split_artifact_name(name)
    if not stem_tail or kind is None:
        return None, None  # deliverable or unknown suffix: never imported
    return stem_root.rstrip("/") + os.path.join(os.path.dirname(tail), stem_tail), kind


def cmd_verify(args) -> int:
    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    items = manifest.get("items") or []

    by_kind: dict[str, dict[str, dict]] = {}
    skipped = 0
    for item in items:
        stem, kind = _stem_for(item["path"], args.media_root, args.stem_root)
        if stem is None or kind is None:
            skipped += 1
            continue
        by_kind.setdefault(kind, {})[stem] = item

    rng = random.Random(0)  # deterministic: the same manifest always checks the same rows
    checked = 0
    failures = []
    for kind in sorted(by_kind):
        stems = sorted(by_kind[kind])
        chosen = stems if args.all else rng.sample(stems, min(len(stems), args.sample))
        for stem in chosen:
            item = by_kind[kind][stem]
            payload = artifact_store.get(stem, kind, path=args.store)
            checked += 1
            if payload is None:
                failures.append({"stem": stem, "kind": kind, "why": "absent from the store"})
            elif hashlib.sha256(bytes(payload)).hexdigest() != item["sha256"]:
                failures.append({"stem": stem, "kind": kind, "why": "sha256 differs"})
            elif len(payload) != item["size"]:
                failures.append({"stem": stem, "kind": kind, "why": f"size {len(payload)} != {item['size']}"})
        print(f"{kind}: {len(chosen)} episode(s) checked of {len(stems)}")

    print(
        json.dumps(
            {
                "checked": checked,
                "deliverables_or_unknown_skipped": skipped,
                "failures": len(failures),
                "store": args.store or artifact_store.store_path(),
            },
            indent=2,
        )
    )
    for failure in failures[:20]:
        print(json.dumps(failure), file=sys.stderr)
    return 0 if not failures else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record or verify the payloads the P39 import moves.")
    ap.add_argument("--store", default=None, help="store path (default $ARTIFACT_STORE or $CONFIG_DIR/artifacts.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="hash every *.dubtitles.* file under a tree")
    rec.add_argument("--root", required=True)
    rec.add_argument("--out", required=True)
    rec.set_defaults(func=cmd_record)

    ver = sub.add_parser("verify", help="compare the store's rows with a recorded manifest")
    ver.add_argument("--manifest", required=True)
    ver.add_argument("--media-root", required=True, help="root the manifest paths were recorded under")
    ver.add_argument("--stem-root", required=True, help="root the store is keyed by (the container's media root)")
    ver.add_argument("--sample", type=int, default=25, help="distinct episodes per kind to check (default 25)")
    ver.add_argument("--all", action="store_true", help="check every row instead of a sample")
    ver.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
