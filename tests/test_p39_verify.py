"""tools/p39_verify.py: the record -> verify pass that proves the import lost nothing.

The dangerous half is the stem mapping: the manifest records host paths, the store is keyed by
the container's media root, and a wrong mapping reads as "every payload missing" against a
perfectly good store.
"""

from __future__ import annotations

import json

from dubtitlerr.core import artifact_store
from tools import p39_verify


def _seed_tree(root):
    root.mkdir(parents=True, exist_ok=True)
    ep = root / "Show" / "Season 01" / "Show - S01E01"
    ep.parent.mkdir(parents=True)
    files = {
        f"{ep}.dubtitles.conf.json": b"conf\n",
        f"{ep}.dubtitles.done": b"done\n",
        f"{ep}.eng.dubtitles.ass": b"a deliverable, never imported\n",
    }
    for path, payload in files.items():
        with open(path, "wb") as fh:
            fh.write(payload)
    return files


def test_record_then_verify_maps_host_paths_onto_container_stems(tmp_path, capsys):
    media_dir = tmp_path / "media"
    files = _seed_tree(media_dir)
    media = str(media_dir)
    manifest = str(tmp_path / "manifest.json")
    assert p39_verify.main(["record", "--root", media, "--out", manifest]) == 0
    with open(manifest, encoding="utf-8") as fh:
        recorded = json.load(fh)
    assert recorded["files"] == len(files)

    store = str(tmp_path / "store.db")
    for path, payload in files.items():
        name = path.rsplit("/", 1)[-1]
        stem_tail, kind = artifact_store.split_artifact_name(name)
        if kind is None or stem_tail is None:
            continue  # the deliverable
        stem = "/media/anime-tv/Show/Season 01/" + stem_tail
        artifact_store.put(stem, kind, payload, path=store)
    artifact_store.reset_connections()

    rc = p39_verify.main(
        [
            "--store",
            store,
            "verify",
            "--manifest",
            manifest,
            "--media-root",
            media,
            "--stem-root",
            "/media/anime-tv",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert '"deliverables_or_unknown_skipped": 1' in captured.out
    assert '"failures": 0' in captured.out


def test_verify_fails_on_a_payload_that_changed(tmp_path, capsys):
    media_dir = tmp_path / "media"
    _seed_tree(media_dir)
    media = str(media_dir)
    manifest = str(tmp_path / "manifest.json")
    assert p39_verify.main(["record", "--root", media, "--out", manifest]) == 0

    store = str(tmp_path / "store.db")
    artifact_store.put("/media/anime-tv/Show/Season 01/Show - S01E01", "conf", b"conf\n", path=store)
    artifact_store.put("/media/anime-tv/Show/Season 01/Show - S01E01", "done", b"different\n", path=store)
    artifact_store.reset_connections()

    rc = p39_verify.main(
        [
            "--store",
            store,
            "verify",
            "--manifest",
            manifest,
            "--media-root",
            media,
            "--stem-root",
            "/media/anime-tv",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert '"failures": 1' in captured.out
    assert "sha256 differs" in captured.err
