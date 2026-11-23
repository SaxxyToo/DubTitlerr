"""Seed and inspect store-backed internal artifacts in tests (P39).

Before P39 a test prepared an episode by writing ``<stem>.dubtitles.conf.json`` (and
friends) beside a fake media file. Those kinds now live only in the artifact store, so a
test seeds a row instead. These helpers keep the old call shape: pass the path the old
code used (``stem + CONF_SUFFIX``) and the kind, and the suffix is stripped to find the
store key. ``reset_connections()`` and ``ARTIFACT_STORE`` are handled by the autouse
fixture in ``conftest.py``.
"""

from __future__ import annotations

import os
import sqlite3

from dubtitlerr.core import artifact_store


def stem_for(path: str, kind: str) -> str:
    """The store key for an old beside-media path of ``kind``."""
    suffix = artifact_store.KIND_SUFFIXES[kind]
    path = str(path)
    return path[: -len(suffix)] if path.endswith(suffix) else path


def seed(path: str, kind: str, obj, meta: dict | None = None) -> None:
    """Put ``obj`` under the store row that ``path`` used to name."""
    artifact_store.put_json(stem_for(path, kind), kind, obj, meta=meta)


def conf(path: str, rows) -> None:
    seed(path, "conf", rows)


def words(path: str, doc) -> None:
    seed(path, "words", doc)


def qc(path: str, doc) -> None:
    seed(path, "qc", doc)


def cuts(path: str, doc) -> None:
    seed(path, "cuts", doc)


def seed_aged(path: str, kind: str, obj, at: float) -> None:
    """Seed a row whose ``updated_at`` is ``at`` (seconds).

    Parking is mtime-driven: a sidecar older than the done stamp is last version's output.
    A fresh write is always "now", so a test that needs the stale arm backdates the row.
    """
    stem = os.path.normpath(os.path.abspath(stem_for(path, kind)))
    artifact_store.put_json(stem, kind, obj)
    conn = sqlite3.connect(artifact_store.store_path())
    try:
        conn.execute("UPDATE artifacts SET updated_at=? WHERE stem=? AND kind=?", (at, stem, kind))
        conn.commit()
    finally:
        conn.close()


def load(path: str, kind: str):
    """Read the row ``path`` names; None when absent or unreadable."""
    try:
        return artifact_store.get_json(stem_for(path, kind), kind)
    except Exception:
        return None


def has(path: str, kind: str) -> bool:
    return artifact_store.exists(stem_for(path, kind), kind)


def drop(path: str, kind: str) -> bool:
    return artifact_store.delete(stem_for(path, kind), kind)
