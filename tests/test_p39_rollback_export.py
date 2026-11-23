"""The P39 rollback recipe: ``export --apply`` reproduces every payload byte-for-byte.

After ``artifact_store import --apply`` the internal artifacts exist only in the store, so the
one way back is ``export --apply``. This test rehearses that exact recipe on a scratch store:
seed a row per kind under a container-style stem, remap the stem prefix onto a scratch tree the
way an operator would (a store copy, never the live one), export, and compare bytes.
"""

from __future__ import annotations

import sqlite3

from dubtitlerr.core import artifact_store

MEDIA_ROOT = "/media/anime-tv"
STEM = f"{MEDIA_ROOT}/Show/Season 01/Show - S01E01"

KINDS = {
    "conf": ".dubtitles.conf.json",
    "words": ".dubtitles.words.json",
    "qc": ".dubtitles.qc.json",
    "cuts": ".dubtitles.cuts.json",
    "done": ".dubtitles.done",
    "stages": ".dubtitles.stages.json",
    "repair.csv": ".dubtitles.repair.csv",
    "repair-summary": ".dubtitles.repair-summary.json",
    "unresolved": ".dubtitles.unresolved.jsonl",
    "unresolved.counters": ".dubtitles.unresolved.counters.json",
    "unresolved.audit": ".dubtitles.unresolved.audit.jsonl",
    "gate-fix": ".dubtitles.gate-fix.json",
    "parked": ".dubtitles.parked.json",
    "restoration": ".dubtitles.restoration.json",
    "presentation": ".dubtitles.presentation.json",
    "fail": ".dubtitles.fail",
    "crash": ".dubtitles.crash.json",
    "no-speech": ".dubtitles.no-speech",
    "mux.log": ".dubtitles.mux.log",
    "mux-recovery": ".dubtitles.mux-recovery",
}


def test_export_apply_reproduces_every_kind_byte_for_byte(tmp_path):
    store = str(tmp_path / "dev.db")
    payloads = {}
    for kind in KINDS:
        payload = f"payload for kind={kind}\n".encode()
        payloads[kind] = payload
        artifact_store.put(STEM, kind, payload, path=store)

    # A parked-only row has no filename to reconstruct and must not break the export.
    artifact_store.put(STEM, artifact_store.parked_kind("conf"), b"parked\n", path=store)

    rehearsal = str(tmp_path / "rehearsal.db")
    with sqlite3.connect(store) as conn, sqlite3.connect(rehearsal) as copy:
        conn.backup(copy)
    scratch_root = str(tmp_path / "restored")
    with sqlite3.connect(rehearsal) as conn:
        remapped = conn.execute("UPDATE artifacts SET stem = replace(stem, ?, ?)", (MEDIA_ROOT, scratch_root)).rowcount
    assert remapped == len(KINDS) + 1

    artifact_store.reset_connections()
    result = artifact_store.export_tree(scratch_root, apply=True, path=rehearsal)
    artifact_store.reset_connections()

    exported = {row["kind"]: row for row in result["items"]}
    assert set(exported) == set(KINDS)
    for kind, suffix in KINDS.items():
        target = f"{scratch_root}/Show/Season 01/Show - S01E01{suffix}"
        with open(target, "rb") as fh:
            assert fh.read() == payloads[kind], kind
        assert exported[kind]["path"] == target
