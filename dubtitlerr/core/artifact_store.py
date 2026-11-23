#!/usr/bin/env python3
"""The central store for per-episode INTERNAL artifacts (P39).

The media tree is a delivery surface: players and users read it, and on this install it is
NFS-backed, so every extra entry costs scan, sweep and backup metadata traffic. The pipeline
used to keep 11-13 ``.dubtitles.*`` sidecars per episode beside the media -- stage records,
the transcript, qc counters, repair audits, unresolved-queue evidence. Measured 2026-11-22:
~8,477 files across ~700 episodes, 316 MB, for ~700 episodes of actual content.

This module moves those artifacts into ONE SQLite file in the config volume
(``$CONFIG_DIR/artifacts.db``), keyed by episode stem and artifact kind. One file, no
service, already inside the backed-up config volume.

What stays on the media tree, deliberately:

  * the media itself;
  * the delivered subtitles (``.eng.dubtitles.ass`` / ``.eng.dubtitles.srt``, and whatever
    name ``SIDECAR_SUFFIX`` gives them) -- Jellyfin and users read those;
  * nothing else. Not even the ``.dubtitles.done`` idempotency stamp: the store owns it
    (``common.write_stamp``/``read_stamp``), and its recorded size+mtime still answers
    "does this stamp describe this file?".

Stdlib only (sqlite3), on purpose: the shell loops' CLIs import pipeline helpers from a
minimal app directory with no pysubs2, and this module is on that path. Do not add a
``common`` import here.

Layout::

    artifacts(stem TEXT, kind TEXT, payload BLOB, meta TEXT, updated_at REAL,
              PRIMARY KEY (stem, kind))

``stem`` is the episode path without its extension (exactly what callers already pass to
``common.out_for``). ``meta`` is JSON for facts a reader needs without parsing the payload
(the done stamp's ``size``/``mtime`` live in the payload verbatim; ``meta`` also records the
source mtime and byte count for the migration).

CLI::

    python3 -m dubtitlerr.core.artifact_store sweep  ROOT [--apply]
    python3 -m dubtitlerr.core.artifact_store import ROOT [--apply]
    python3 -m dubtitlerr.core.artifact_store export [ROOT] [--apply]
    python3 -m dubtitlerr.core.artifact_store census ROOT
    python3 -m dubtitlerr.core.artifact_store stems  --kind K [--root R]
    python3 -m dubtitlerr.core.artifact_store has    --stem S --kind K
    python3 -m dubtitlerr.core.artifact_store get    --stem S --kind K
    python3 -m dubtitlerr.core.artifact_store put    --stem S --kind K  (payload on stdin)

Every mutating subcommand is DRY RUN unless ``--apply`` is passed. Sweep and import are the
two destructive ones (they delete files from the media tree and, for import, take a copy
into the store first).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time

DB_FILENAME = "artifacts.db"
DB_MODE = 0o600
SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    stem       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    BLOB NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    PRIMARY KEY (stem, kind)
);
CREATE INDEX IF NOT EXISTS artifacts_kind ON artifacts (kind);
"""

# kind -> filename suffix. Longest suffix wins on import (``.unresolved.counters.json``
# before ``.unresolved.jsonl`` is irrelevant -- different tails -- but ``repair-summary.json``
# vs ``repair.csv`` and the ``.jsonl`` family all need exact matching, not startswith).
KIND_SUFFIXES = {
    "done": ".dubtitles.done",
    "stages": ".dubtitles.stages.json",
    "words": ".dubtitles.words.json",
    "cuts": ".dubtitles.cuts.json",
    "qc": ".dubtitles.qc.json",
    "conf": ".dubtitles.conf.json",
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
SUFFIX_KINDS = {suffix: kind for kind, suffix in KIND_SUFFIXES.items()}

# A parked artifact is the previous payload of a live kind, kept in the store under
# ``<kind>.parked`` while a rebuild is in flight. Parking exists for the reason the old
# ``<name>.stale`` rename did: a replacement that fails must leave the previous generation
# one step away from being served again. The live row is deleted so nothing reads the
# superseded payload while the rebuild runs, and unpark() restores it verbatim.
PARK_SUFFIX = ".parked"

# Artifact classes the sweep removes outright. ``stale``/``bak`` are parked copies from a
# superseded run (a replacement was written, or the operator moved them aside); ``mux.log``
# is a one-line post-mux note whose content mux's log already carries; ``mux-recovery`` is a
# marker a human resolves, and a resolved marker is junk. None of them is ever imported.
JUNK_KINDS = ("stale", "bak", "mux.log", "mux-recovery")

# Suffixes that belong beside media and must never be imported, swept or rekeyed.
DELIVERABLE_MARKERS = (".eng.dubtitles.ass", ".eng.dubtitles.srt", ".dubtitles.ass", ".dubtitles.srt")

_LOCK = threading.Lock()
# One connection per (store path, thread): sqlite connections are thread-affine, and the
# review server answers requests on a thread pool while the pipeline writes from its own
# process. Sharing one handle across threads raises ProgrammingError; a per-thread handle
# keeps every caller on the same store without a global write lock.
_CONNS: dict[tuple[str, int], sqlite3.Connection] = {}


def config_dir(environ=None) -> str:
    environ = os.environ if environ is None else environ
    return environ.get("CONFIG_DIR") or "/config"


def store_path(environ=None) -> str:
    """Where the store lives: ``$ARTIFACT_STORE``, else ``$CONFIG_DIR/artifacts.db``."""
    environ = os.environ if environ is None else environ
    explicit = (environ.get("ARTIFACT_STORE") or "").strip()
    if explicit:
        return explicit
    return os.path.join(config_dir(environ), DB_FILENAME)


def _norm(stem: str) -> str:
    """One canonical key shape for reads and writes: absolute, normalized, no trailing /."""
    return os.path.normpath(os.path.abspath(stem))


def _connect(path: str | None = None) -> sqlite3.Connection:
    path = path or store_path()
    key = (path, threading.get_ident())
    with _LOCK:
        conn = _CONNS.get(key)
        if conn is not None:
            return conn
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, mode=0o700, exist_ok=True)
        created = not os.path.exists(path)
        conn = sqlite3.connect(path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
        conn.commit()
        if created:
            try:
                os.chmod(path, DB_MODE)
            except OSError:
                pass
        _CONNS[key] = conn
        return conn


def reset_connections() -> None:
    """Close cached connections (tests switch ``ARTIFACT_STORE`` between cases)."""
    with _LOCK:
        for conn in _CONNS.values():
            try:
                conn.close()
            except Exception:
                pass
        _CONNS.clear()


def _encode(data) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        return data.encode("utf-8")
    raise TypeError(f"artifact payload must be str or bytes, not {type(data).__name__}")


def _decode_text(raw: bytes):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --- writes -----------------------------------------------------------------------


def _base_kind(kind: str):
    """The live kind behind ``kind``, or None.

    Accepts every kind in ``KIND_SUFFIXES`` and its ``<kind>.parked`` form. Parked kinds are
    store-only: they have no filename suffix, so import/export/sweep never see them.
    """
    if kind in KIND_SUFFIXES:
        return kind
    if kind.endswith(PARK_SUFFIX):
        base = kind[: -len(PARK_SUFFIX)]
        if base in KIND_SUFFIXES:
            return base
    return None


def _require_kind(kind: str) -> None:
    if _base_kind(kind) is None:
        raise ValueError(f"unknown artifact kind {kind!r}")


def parked_kind(kind: str) -> str:
    """The store kind holding ``kind``'s parked payload."""
    _require_kind(kind)
    return kind + PARK_SUFFIX


def put(stem: str, kind: str, data, meta: dict | None = None, path: str | None = None) -> None:
    """Insert or replace one artifact. ``meta`` keys are MERGED into the stored document, so
    a writer can refresh payload and provenance without dropping what import recorded."""
    _require_kind(kind)
    conn = _connect(path)
    key = _norm(stem)
    doc = dict(meta or {})
    try:
        conn.execute("BEGIN IMMEDIATE")  # one writer at a time: review server and pipeline both write
        if doc:
            row = conn.execute("SELECT meta FROM artifacts WHERE stem=? AND kind=?", (key, kind)).fetchone()
            if row:
                try:
                    previous = json.loads(row[0])
                except ValueError:
                    previous = {}
                if isinstance(previous, dict):
                    previous.update(doc)
                    doc = previous
        conn.execute(
            "INSERT INTO artifacts (stem, kind, payload, meta, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(stem, kind) DO UPDATE SET payload=excluded.payload, meta=excluded.meta, "
            "updated_at=excluded.updated_at",
            (key, kind, _encode(data), json.dumps(doc, sort_keys=True), time.time()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def put_text(stem: str, kind: str, text: str, meta: dict | None = None, path: str | None = None) -> None:
    put(stem, kind, text, meta=meta, path=path)


def put_json(stem: str, kind: str, obj, meta: dict | None = None, path: str | None = None) -> None:
    put(stem, kind, json.dumps(obj), meta=meta, path=path)


def append_jsonl(stem: str, kind: str, obj, path: str | None = None) -> None:
    """Append one JSON line atomically (read-modify-write under the store's own transaction).

    The old on-disk log was one append; the store has no append, so the whole row is rewritten.
    That is fine at this size (P33 caps the queue at a few hundred lines) and keeps the
    ``items()`` contract -- one JSON object per line, torn tails impossible.
    """
    conn = _connect(path)
    key = _norm(stem)
    try:
        conn.execute("BEGIN IMMEDIATE")  # the read-modify-write must not interleave with another writer
        row = conn.execute("SELECT payload, meta FROM artifacts WHERE stem=? AND kind=?", (key, kind)).fetchone()
        body = row[0] if row else b""
        try:
            meta = json.loads(row[1]) if row else {}
        except ValueError:
            meta = {}
        line = json.dumps(obj, ensure_ascii=False)
        new = body + (b"\n" if body and not body.endswith(b"\n") else b"") + line.encode("utf-8") + b"\n"
        conn.execute(
            "INSERT INTO artifacts (stem, kind, payload, meta, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(stem, kind) DO UPDATE SET payload=excluded.payload, meta=excluded.meta, "
            "updated_at=excluded.updated_at",
            (key, kind, new, json.dumps(meta, sort_keys=True), time.time()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def write_jsonl(stem: str, kind: str, rows, path: str | None = None) -> None:
    """Replace one artifact with ``rows`` as JSONL: one object per line, in order.

    The whole-record rewrite pairs with :func:`append_jsonl` for the kinds whose payload is a
    line-oriented log (the unresolved queue and its audit). Empty ``rows`` stores an empty
    payload, exactly like the empty file the file-based writer left behind.
    """
    lines = [json.dumps(row, ensure_ascii=False) for row in rows or [] if isinstance(row, dict)]
    put_text(stem, kind, "".join(line + "\n" for line in lines), path=path)


def touch(stem: str, kind: str, when: float | None = None, path: str | None = None) -> bool:
    """Set one artifact's ``updated_at`` (its mtime for readers that ask for age).

    False when the row is absent. Used by the staleness tests and by any caller that must
    age a record without rewriting its payload.
    """
    conn = _connect(path)
    with conn:
        cur = conn.execute(
            "UPDATE artifacts SET updated_at=? WHERE stem=? AND kind=?",
            (time.time() if when is None else float(when), _norm(stem), kind),
        )
    return cur.rowcount > 0


def delete(stem: str, kind: str, path: str | None = None) -> bool:
    """Remove one artifact; False when it was not there."""
    conn = _connect(path)
    with conn:
        cur = conn.execute("DELETE FROM artifacts WHERE stem=? AND kind=?", (_norm(stem), kind))
    return cur.rowcount > 0


def delete_kind(kind: str, path: str | None = None) -> int:
    conn = _connect(path)
    with conn:
        cur = conn.execute("DELETE FROM artifacts WHERE kind=?", (kind,))
    return cur.rowcount


def park(stem: str, kind: str, path: str | None = None) -> bool:
    """Move the live payload of ``kind`` aside as ``<kind>.parked``. False when absent.

    This is the store form of the old ``<name>.stale`` rename: the live row goes away so no
    reader serves the superseded generation while a rebuild runs, and the payload survives
    one call away. A failed rebuild therefore still leaves the previous artifact recoverable
    with ``unpark`` (and, unlike a file rename, nothing lands beside the media).
    """
    raw = get(stem, kind, path=path)
    if raw is None:
        return False
    previous = meta(stem, kind, path=path)
    put(stem, parked_kind(kind), raw, meta={**previous, "parked_from": kind, "parked_at": time.time()}, path=path)
    delete(stem, kind, path=path)
    return True


def unpark(stem: str, kind: str, path: str | None = None) -> bool:
    """Restore ``<kind>.parked`` as the live ``kind``. False when nothing was parked."""
    raw = get(stem, parked_kind(kind), path=path)
    if raw is None:
        return False
    previous = meta(stem, parked_kind(kind), path=path)
    for key in ("parked_from", "parked_at"):
        previous.pop(key, None)
    put(stem, kind, raw, meta=previous, path=path)
    delete(stem, parked_kind(kind), path=path)
    return True


def has_parked(stem: str, kind: str, path: str | None = None) -> bool:
    return exists(stem, parked_kind(kind), path=path)


# --- reads ------------------------------------------------------------------------


def get(stem: str, kind: str, path: str | None = None) -> bytes | None:
    conn = _connect(path)
    row = conn.execute("SELECT payload FROM artifacts WHERE stem=? AND kind=?", (_norm(stem), kind)).fetchone()
    return None if row is None else bytes(row[0])


def get_text(stem: str, kind: str, path: str | None = None) -> str | None:
    raw = get(stem, kind, path=path)
    return None if raw is None else _decode_text(raw)


def get_json(stem: str, kind: str, path: str | None = None):
    """Parsed payload or None. A corrupt payload reads as absent, never as an exception: this
    sits in the hot path of stage and review readers, which must fail open."""
    raw = get(stem, kind, path=path)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def read_jsonl(stem: str, kind: str, path: str | None = None) -> list:
    """Every well-formed object line, in order. A corrupt line is skipped, like the file
    reader it replaces -- a torn append costs the last entry, not the queue."""
    text = get_text(stem, kind, path=path)
    if not text:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def exists(stem: str, kind: str, path: str | None = None) -> bool:
    conn = _connect(path)
    row = conn.execute("SELECT 1 FROM artifacts WHERE stem=? AND kind=?", (_norm(stem), kind)).fetchone()
    return row is not None


def meta(stem: str, kind: str, path: str | None = None) -> dict:
    conn = _connect(path)
    row = conn.execute("SELECT meta FROM artifacts WHERE stem=? AND kind=?", (_norm(stem), kind)).fetchone()
    if not row:
        return {}
    try:
        doc = json.loads(row[0])
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def mtime(stem: str, kind: str, path: str | None = None) -> float | None:
    conn = _connect(path)
    row = conn.execute("SELECT updated_at FROM artifacts WHERE stem=? AND kind=?", (_norm(stem), kind)).fetchone()
    return None if row is None else float(row[0])


def kinds(stem: str, path: str | None = None) -> list:
    conn = _connect(path)
    rows = conn.execute("SELECT kind FROM artifacts WHERE stem=? ORDER BY kind", (_norm(stem),)).fetchall()
    return [r[0] for r in rows]


def stems(root: str | None = None, kind: str | None = None, path: str | None = None) -> list:
    """Distinct episode stems, optionally limited to one artifact kind and one tree.

    ``root`` is a directory prefix; matching is on the normalized absolute path, so it works
    for the media root the stages use and for a test tmpdir.
    """
    conn = _connect(path)
    sql = "SELECT DISTINCT stem FROM artifacts"
    where, args = [], []
    if kind:
        where.append("kind=?")
        args.append(kind)
    if root:
        base = _norm(root)
        where.append("(stem=? OR stem LIKE ?)")
        args += [base, base + os.sep + "%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY stem"
    return [r[0] for r in conn.execute(sql, args)]


def stats(root: str | None = None, path: str | None = None) -> dict:
    """Row counts and payload bytes per kind, optionally limited to one tree."""
    conn = _connect(path)
    sql = "SELECT kind, COUNT(*), COALESCE(SUM(LENGTH(payload)), 0) FROM artifacts"
    args = []
    if root:
        base = _norm(root)
        sql += " WHERE (stem=? OR stem LIKE ?)"
        args = [base, base + os.sep + "%"]
    sql += " GROUP BY kind ORDER BY kind"
    rows = conn.execute(sql, args).fetchall()
    return {"kinds": {r[0]: {"count": r[1], "bytes": r[2]} for r in rows}, "rows": sum(r[1] for r in rows)}


# --- suffix <-> kind --------------------------------------------------------------


def kind_for_suffix(suffix: str):
    """Artifact kind for a filename suffix, or None for a deliverable/unknown file."""
    return SUFFIX_KINDS.get(suffix)


def suffix_for_kind(kind: str) -> str:
    return KIND_SUFFIXES[kind]


def split_artifact_name(name: str):
    """``(stem_suffixless_path, kind)`` for an internal artifact filename, else ``(None, None)``.

    Matching is by exact suffix, longest first, so ``x.dubtitles.unresolved.audit.jsonl`` is
    not mistaken for the ``unresolved`` queue and ``x.dubtitles.conf.json.bak`` (junk) is not
    imported. Deliverables are excluded before the lookup.
    """
    for marker in DELIVERABLE_MARKERS:
        if name.endswith(marker):
            return None, None
    for suffix in sorted(SUFFIX_KINDS, key=len, reverse=True):
        if name.endswith(suffix):
            return name[: -len(suffix)], SUFFIX_KINDS[suffix]
    return None, None


def is_junk_name(name: str) -> bool:
    """True for a sidecar the sweep owns: parked copies, mux notes, resolved recovery markers.

    Also covers the operator-parked crash marker ``<stem>.dubtitles.fail.archived-<date>``:
    a .fail marker is retried by deleting it, so an archived one is only a record nobody
    reads, and the owner's rule is that the media tree carries nothing but the media and its
    subtitles.
    """
    if ".dubtitles." not in name:
        return False
    if ".dubtitles.fail.archived-" in name:
        return True
    return name.endswith((".stale", ".bak", ".mux.log", ".mux-recovery"))


# --- migration --------------------------------------------------------------------


def _walk_artifacts(root: str, include_junk: bool = False):
    """Yield ``(path, stem, kind)`` for every recognized internal artifact under ``root``."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if is_junk_name(name) and not include_junk:
                continue
            stem_tail, kind = split_artifact_name(name)
            if kind is None or stem_tail is None:
                continue
            if stem_tail:
                yield os.path.join(dirpath, name), _norm(os.path.join(dirpath, stem_tail)), kind


def import_tree(root: str, apply: bool = False, path: str | None = None) -> dict:
    """Copy every internal artifact under ``root`` into the store.

    Idempotent: a file whose payload and size/mtime already match is left alone, so a second
    run imports nothing. With ``apply`` the file is removed only AFTER its row is committed --
    a crash mid-run leaves a duplicate (harmless, next run reconciles), never a lost artifact.
    """
    plan = []
    inserted = updated = unchanged = failed = 0
    for filepath, stem, kind in _walk_artifacts(root):
        try:
            st = os.stat(filepath)
            with open(filepath, "rb") as fh:
                payload = fh.read()
        except OSError as exc:
            failed += 1
            plan.append({"path": filepath, "kind": kind, "action": "failed", "error": str(exc)})
            continue
        conn = _connect(path)
        row = conn.execute("SELECT payload, meta FROM artifacts WHERE stem=? AND kind=?", (stem, kind)).fetchone()
        same = False
        if row is not None:
            prev_meta = {}
            try:
                prev_meta = json.loads(row[1]) or {}
            except ValueError:
                prev_meta = {}
            same = (
                bytes(row[0]) == payload
                and prev_meta.get("size") == st.st_size
                and abs(float(prev_meta.get("mtime") or 0) - st.st_mtime) < 0.001
            )
        if same:
            unchanged += 1
            plan.append({"path": filepath, "kind": kind, "action": "unchanged"})
            if apply:
                _remove_quietly(filepath)
            continue
        action = "updated" if row is not None else "inserted"
        plan.append({"path": filepath, "kind": kind, "action": action, "bytes": len(payload)})
        if not apply:
            if row is None:
                inserted += 1
            else:
                updated += 1
            continue
        try:
            put(stem, kind, payload, meta={"mtime": st.st_mtime, "size": st.st_size, "imported_from": filepath}, path=path)
            _remove_quietly(filepath)
            if row is None:
                inserted += 1
            else:
                updated += 1
        except (OSError, sqlite3.Error, ValueError) as exc:
            failed += 1
            plan[-1]["action"] = "failed"
            plan[-1]["error"] = str(exc)
    return {
        "root": _norm(root),
        "applied": bool(apply),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "failed": failed,
        "total": len(plan),
        "items": plan,
    }


def export_tree(root: str | None = None, apply: bool = False, path: str | None = None, kind: str | None = None) -> dict:
    """Write store rows back out as files, reconstructing the original paths.

    The reversibility belt: after an import, ``export --apply`` restores every artifact
    byte-for-byte at ``<stem><suffix>``. Rows are never deleted by export.
    """
    from dubtitlerr.core.atomic_file import atomic_write_bytes

    rows = []
    conn = _connect(path)
    sql = "SELECT stem, kind, payload, meta FROM artifacts"
    where, args = [], []
    if root:
        base = _norm(root)
        where.append("(stem=? OR stem LIKE ?)")
        args += [base, base + os.sep + "%"]
    if kind:
        where.append("kind=?")
        args.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY stem, kind"
    for stem, row_kind, payload, meta_raw in conn.execute(sql, args):
        if row_kind not in KIND_SUFFIXES:
            # Parked rows (``<kind>.parked``) are store-only bookkeeping; there is no filename
            # to reconstruct, so export leaves them in place.
            continue
        suffix = suffix_for_kind(row_kind)
        target = stem + suffix
        rows.append({"path": target, "kind": row_kind, "bytes": len(payload)})
        if not apply:
            continue
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            atomic_write_bytes(target, bytes(payload))
            try:
                doc = json.loads(meta_raw)
            except ValueError:
                doc = {}
            if isinstance(doc, dict) and doc.get("mtime"):
                os.utime(target, (float(doc["mtime"]), float(doc["mtime"])))
        except OSError as exc:
            rows[-1]["error"] = str(exc)
    return {"applied": bool(apply), "exported": len(rows), "items": rows}


def sweep_tree(root: str, apply: bool = False, path: str | None = None) -> dict:
    """Remove the junk classes from the media tree (and any rows the store holds for them).

    Patterns: ``*.stale`` (parked replacement copies), ``*.bak`` (including
    ``*.pre-gate-fix.bak``), ``*.dubtitles.mux.log`` (one-line mux notes) and
    ``*.dubtitles.mux-recovery`` (a marker whose incident a human already resolved).
    Deliverables are untouchable.
    """
    junk = []
    freed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not is_junk_name(name):
                continue
            filepath = os.path.join(dirpath, name)
            if any(filepath.endswith(m) for m in DELIVERABLE_MARKERS):
                continue
            try:
                size = os.stat(filepath).st_size
            except OSError:
                size = 0
            junk.append({"path": filepath, "bytes": size, "class": _junk_class(name)})
            freed += size
            if apply:
                _remove_quietly(filepath)
    removed_rows = 0
    if apply:
        for kind in JUNK_KINDS:
            removed_rows += delete_kind(kind, path=path)
    return {
        "root": _norm(root),
        "applied": bool(apply),
        "files": len(junk),
        "bytes": freed,
        "store_rows": removed_rows,
        "items": junk,
    }


def _junk_class(name: str) -> str:
    for marker, label in (
        (".dubtitles.fail.archived-", "archived"),
        (".stale", "stale"),
        (".mux-recovery", "mux-recovery"),
        (".mux.log", "mux.log"),
        (".bak", "bak"),
    ):
        if marker in name:
            return label
    return "junk"


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"warning: could not remove {path}: {exc}", file=sys.stderr)


# --- CLI --------------------------------------------------------------------------


def _print_plan(result: dict, verb: str) -> None:
    items = result.get("items") or []
    if verb == "sweep":
        by_class = {}
        for item in items:
            by_class.setdefault(item["class"], 0)
            by_class[item["class"]] += 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(by_class.items())) or "nothing"
        print(f"sweep {result['root']}: {result['files']} file(s), {result['bytes']} bytes ({summary})")
        if not result["applied"] and result["files"]:
            print("dry run: pass --apply to delete")
    elif verb == "import":
        print(
            f"import {result['root']}: {result['total']} artifact(s) -- "
            f"inserted={result['inserted']} updated={result['updated']} "
            f"unchanged={result['unchanged']} failed={result['failed']}"
        )
        if not result["applied"] and result["total"]:
            print("dry run: pass --apply to write the store and remove migrated files")
    elif verb == "export":
        print(f"export: {result['exported']} artifact(s)")
        if not result["applied"] and result["exported"]:
            print("dry run: pass --apply to write the files")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Central store for per-episode internal artifacts.")
    ap.add_argument("--store", default=None, help="override the store path (default $ARTIFACT_STORE or $CONFIG_DIR/artifacts.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, help_text in (
        ("sweep", "remove junk sidecars (.stale/.bak/.mux.log/.mux-recovery) from a tree"),
        ("import", "copy internal artifacts under a tree into the store"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("root")
        p.add_argument("--apply", action="store_true", help="actually change things (default: dry run)")

    p = sub.add_parser("export", help="write store artifacts back out as files")
    p.add_argument("root", nargs="?", default=None)
    p.add_argument("--kind", default=None)
    p.add_argument("--apply", action="store_true")

    p = sub.add_parser("census", help="count artifacts in a tree and in the store")
    p.add_argument("root")

    p = sub.add_parser("stems", help="print stems holding one artifact kind")
    p.add_argument("--kind", required=True)
    p.add_argument("--root", default=None)

    p = sub.add_parser("has", help="exit 0 when an artifact exists")
    p.add_argument("--stem", required=True)
    p.add_argument("--kind", required=True)

    p = sub.add_parser("get", help="write one artifact payload to stdout")
    p.add_argument("--stem", required=True)
    p.add_argument("--kind", required=True)

    p = sub.add_parser("put", help="store one artifact payload from stdin")
    p.add_argument("--stem", required=True)
    p.add_argument("--kind", required=True)

    args = ap.parse_args(argv)
    store = args.store

    if args.cmd == "sweep":
        result = sweep_tree(args.root, apply=args.apply, path=store)
        _print_plan(result, "sweep")
        return 0
    if args.cmd == "import":
        result = import_tree(args.root, apply=args.apply, path=store)
        _print_plan(result, "import")
        return 1 if result["failed"] else 0
    if args.cmd == "export":
        result = export_tree(args.root, apply=args.apply, path=store, kind=args.kind)
        _print_plan(result, "export")
        return 0
    if args.cmd == "census":
        tree = {}
        files = 0
        for _path_item, _stem, kind in _walk_artifacts(args.root):
            files += 1
            tree[kind] = tree.get(kind, 0) + 1
        print(json.dumps({"media_tree": {"files": files, "kinds": tree}, "store": stats(path=store)}, indent=2, sort_keys=True))
        return 0
    if args.cmd == "stems":
        for stem in stems(root=args.root, kind=args.kind, path=store):
            print(stem)
        return 0
    if args.cmd == "has":
        return 0 if exists(args.stem, args.kind, path=store) else 1
    if args.cmd == "get":
        raw = get(args.stem, args.kind, path=store)
        if raw is None:
            return 1
        sys.stdout.buffer.write(raw)
        return 0
    if args.cmd == "put":
        put(args.stem, args.kind, sys.stdin.buffer.read(), path=store)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
