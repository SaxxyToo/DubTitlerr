#!/usr/bin/env python3
"""Atomic file replacement: write a temp file beside the target, chmod it, os.replace over the
target. The previous file survives any failure and no temp is left behind.

Stdlib only, deliberately: this is the one helper the shell loops' CLIs need at IMPORT time, and
they run from a minimal app directory with no pysubs2 (see settings_store._log and the harnesses
in tests/test_gen_loop_*.py). common.py imports pysubs2, so anything those CLIs need on the import
path has to live in a module with no third-party imports -- the same reason glossary_lock.py sits
beside it rather than inside common.py. common re-exports all of this, so `common.atomic_write`
and `from common import SIDECAR_MODE` keep working.
"""

import os
import tempfile

# Sidecars must be GROUP-WRITABLE. 2026-08-21: every sidecar shipped 0644, so only the
# creating uid could ever overwrite one. That is invisible while the container runs as root
# (root bypasses the check) and breaks the moment any other member of the media group writes
# -- the 3200g node reaches the library over CIFS as r520smb (uid 1001, gid 100 users), hit
# "Permission denied" re-writing an existing .dubtitles.done, and re-muxed the same episode
# on every sweep because the stamp could never land. 8,701 sidecars library-wide were 0644.
SIDECAR_MODE = 0o664

# ``mode`` sentinel: reuse the target's current permission bits instead of installing a fixed
# set. A target that does not exist yet gets SIDECAR_MODE.
MODE_KEEP = "keep"


def atomic_write(path, render, mode=SIDECAR_MODE, newline=None, fsync=False, quiet=False):
    """Write ``path`` through a temp file in the same directory plus os.replace -- the
    discipline qc.write and glossary_acquire._write_json already follow.

    process() clears the in-flight .dubtitles.fail marker as soon as transcription
    finishes, BEFORE the srt and conf are written, so a plain open(path, "w") that dies
    mid-loop leaves a TRUNCATED file with no marker behind it: the default SKIP_IF_SRT=1
    already-srt guard reads that as a finished episode on the next sweep and mux embeds a
    cut-off subtitle. Same rule as the stale-sidecar parking fix one function away --
    never drop known-good output before the replacement exists. os.replace either swaps
    or does nothing, and a failure leaves neither a partial target nor a temp file.

    The exception is deliberately NOT swallowed (unlike qc.write's): the srt and conf are
    the episode's product, not observability, and a run that lost them must not report ok.

    ``newline`` is passed to the file object (csv output needs ``newline=""``).

    ``mode`` is the permission bits installed on the replacement: an int, or MODE_KEEP to
    reuse the target's current bits (a target that does not exist yet gets SIDECAR_MODE).
    It defaults to common.SIDECAR_MODE (0664) because mkstemp creates 0600, which would
    strip group/other read from every file we ship; sidecars must stay GROUP-WRITABLE --
    0644 meant only the creating uid could ever overwrite one, which broke every non-root
    writer (see the SIDECAR_MODE comment above).

    ``fsync`` also flushes the containing directory after the rename, making the rename
    itself durable. Set it where a lost write is a lost account or setting (the config
    volume), not for per-episode sidecars the next sweep rewrites anyway.

    ``quiet`` returns False instead of raising when the write cannot LAND -- an OSError or
    a ValueError -- for callers that own a verdict path and need a False they can report
    (decisions.save, unresolved._rewrite). A TypeError (a payload that is not serialisable)
    is a programming error in the caller and still raises: silently returning False would
    make the lost verdict look like a disk problem. The default is to raise: an episode's
    product must not go missing silently.

    Returns True on success. Either way the previous file survives and no temp remains.
"""
    return _atomic_replace(path, render, mode, fsync, quiet, binary=False, newline=newline)


def atomic_write_bytes(path, data, mode=SIDECAR_MODE, fsync=False, quiet=False):
    """atomic_write for a bytes payload (settings_store, review_glossary's glossary edits).

    Same guarantees: temp file beside the target, chmod, os.replace; the old file survives
    any failure and no temp is left behind. ``data`` may be str, encoded UTF-8."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return _atomic_replace(path, lambda f: f.write(data), mode, fsync, quiet, binary=True, newline=None)


def _resolve_mode(path, mode):
    """MODE_KEEP -> the target's current permission bits (a new target gets SIDECAR_MODE);
    anything else is the mode to install verbatim."""
    if mode != MODE_KEEP:
        return mode
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return SIDECAR_MODE


def _fsync_dir(d):
    """Flush the directory entry, so the rename survives a power loss. Best-effort: a
    filesystem that refuses it must not turn a finished write into an error."""
    try:
        dfd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _atomic_replace(path, write, mode, fsync, quiet, binary, newline):
    """The one atomic-replace engine both public writers run on.

    mkstemp beside the target (same filesystem, so the rename is atomic), let ``write``
    fill the fd, install ``mode``, os.replace over the target. On any Exception the temp is
    unlinked: ``quiet`` callers get a False, everyone else the original traceback."""
    d = os.path.dirname(path) or "."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
        kwargs = {} if binary else {"encoding": "utf-8", "newline": newline}
        with os.fdopen(fd, "wb" if binary else "w", **kwargs) as f:
            write(f)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.chmod(tmp, _resolve_mode(path, mode))
        os.replace(tmp, path)
        tmp = None
        if fsync:
            _fsync_dir(d)
        return True
    except (OSError, ValueError):
        if quiet:
            return False
        raise
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
