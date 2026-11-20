#!/usr/bin/env python3
"""The persisted processing state: ``running``, ``paused`` or ``stopped``.

One tiny JSON file in the config volume (``$CONFIG_DIR/processing.json``) survives a
container recreate. It exists so the app can be UP -- review UI, API, operators -- without
touching the media library, and so Start / Pause / Stop are explicit operator acts rather
than something a boot happens to do.

The file holds who changed the state and when, in the same spirit as the settings audit log:

    {"state": "paused", "by": "user:alice", "at": "2026-11-19T18:02:11Z"}

Semantics:

* ``running`` -- both loops work at their normal cadence.
* ``paused``  -- each loop finishes the unit in flight (show, sweep, stem, term) and holds
                 at its existing boundary, polling every PROCESSING_POLL_INTERVAL seconds.
                 Nothing in flight is killed; the UI and API stay up.
* ``stopped`` -- the existing soft stop: the loops drain and exit, the container ends, and
                 its restart policy brings the UI back. At that next boot ``stopped`` holds
                 the loops idle until someone presses Start. ``docker stop`` keeps working
                 exactly as before (it writes no state; it is a container operation).

A missing file means **paused** for the application: container_run.sh calls ``init`` at
boot, which materialises ``paused`` before any loop starts. Stage binaries invoked directly
outside the container (tests, a manual repair run) keep today's behaviour when no file
exists -- there is no app boot to honor. A corrupt file is always read as paused: the safe
direction is to leave the library alone.

Stdlib only, deliberately: container_run.sh and the shell loops' CLIs run from a minimal
app directory with no pysubs2 (see atomic_file.py's docstring for the same rule).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

from dubtitlerr.core.atomic_file import atomic_write_bytes

STATES = ("running", "paused", "stopped")
DEFAULT_STATE = "paused"
FILENAME = "processing.json"


def config_dir(environ=None):
    environ = os.environ if environ is None else environ
    return environ.get("CONFIG_DIR") or "/config"


def state_path(environ=None):
    environ = os.environ if environ is None else environ
    return environ.get("PROCESSING_STATE_FILE") or os.path.join(config_dir(environ), FILENAME)


def _log(message):
    # stderr, so a CLI that prints just the state word on stdout stays scriptable while the
    # audit line still lands in the container log.
    print(message, file=sys.stderr)


def _iso_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(environ=None):
    """The current state as ``{state, by, at, default, error}``. Never raises.

    ``default`` is true when there is no usable file, so a caller can tell an operator's
    explicit ``paused`` from the boot-time default. Either way the state is ``paused``.
    """
    path = state_path(environ)
    try:
        with open(path, "rb") as f:
            raw = f.read(64 * 1024 + 1)
    except FileNotFoundError:
        return {"state": DEFAULT_STATE, "by": None, "at": None, "default": True, "missing": True, "error": ""}
    except OSError as e:
        return {
            "state": DEFAULT_STATE,
            "by": None,
            "at": None,
            "default": True,
            "missing": False,
            "error": f"unreadable ({e.__class__.__name__})",
        }
    if len(raw) > 64 * 1024:
        return {"state": DEFAULT_STATE, "by": None, "at": None, "default": True, "missing": False, "error": "larger than 64 KiB"}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"state": DEFAULT_STATE, "by": None, "at": None, "default": True, "missing": False, "error": "not valid JSON"}
    if not isinstance(data, dict):
        return {"state": DEFAULT_STATE, "by": None, "at": None, "default": True, "missing": False, "error": "unexpected shape"}
    state = data.get("state")
    if state not in STATES:
        return {
            "state": DEFAULT_STATE,
            "by": None,
            "at": None,
            "default": True,
            "missing": False,
            "error": f"unknown state {state!r}",
        }
    by = data.get("by") if isinstance(data.get("by"), str) else None
    at = data.get("at") if isinstance(data.get("at"), str) else None
    return {"state": state, "by": by, "at": at, "default": False, "missing": False, "error": ""}


def describe(environ=None):
    """The read shape the UI and API return (identical to read)."""
    return read(environ)


def write(state, by="unknown", environ=None):
    """Persist ``state`` atomically with who/when. Raises ValueError for an unknown state,
    OSError when the config volume refuses the write."""
    if state not in STATES:
        raise ValueError(f"unknown processing state: {state!r}")
    path = state_path(environ)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = json.dumps({"state": state, "by": by, "at": _iso_now()}, sort_keys=True) + "\n"
    atomic_write_bytes(path, payload, mode=0o664, fsync=True)
    _log(f"review server: processing state changed by {by}: {state}")
    return describe(environ)


def _touch_flag(flag):
    with open(flag, "a"):
        os.utime(flag, None)


def apply(action, who="admin", environ=None):
    """Apply ``start`` / ``pause`` / ``stop``. Returns ``(status, payload)`` for the API.

    202 is deliberate for all three: the state is persisted immediately, but a running
    loop picks it up at its next boundary, not synchronously inside this request.
    """
    environ = os.environ if environ is None else environ
    mapping = {"start": "running", "pause": "paused", "stop": "stopped"}
    state = mapping.get(action)
    if state is None:
        return 400, {"ok": False, "error": f"unknown processing action: {action!r}"}
    try:
        info = write(state, by=who, environ=environ)
    except (OSError, ValueError) as e:
        return 500, {"ok": False, "error": f"could not persist the processing state ({e.__class__.__name__})"}
    flag = environ.get("STOP_FLAG") or "/tmp/dubtitlerr.stop"
    if state == "stopped":
        try:
            _touch_flag(flag)
        except OSError as e:
            return 500, {
                "ok": False,
                "state": state,
                "error": f"state saved but could not write the stop flag ({e.__class__.__name__})",
            }
    elif state == "running":
        try:
            os.unlink(flag)
        except OSError:
            pass  # absent is the normal case
    return 202, {"ok": True, **info}


# --- CLI: the host-side way to set the state with the container down --------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read or set the DubTitlerr processing state.")
    parser.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=("show", "init", "start", "pause", "stop"),
        help="show (default), init (write paused only when absent), start, pause, stop",
    )
    parser.add_argument("--by", default="cli", help="who is changing the state (recorded in the file)")
    parser.add_argument("--config-dir", default=None, help="override CONFIG_DIR")
    parser.add_argument("--stop-flag", default=None, help="override STOP_FLAG for stop/start")
    parser.add_argument("--quiet", action="store_true", help="print nothing, only set the state")
    args = parser.parse_args(argv)

    environ = dict(os.environ)
    if args.config_dir:
        environ["CONFIG_DIR"] = args.config_dir
    if args.stop_flag:
        environ["STOP_FLAG"] = args.stop_flag

    if args.action in ("show", "init"):
        if args.action == "init" and not os.path.exists(state_path(environ)):
            write("paused", by=args.by, environ=environ)
        info = describe(environ)
        if not args.quiet:
            print(info["state"])
        return 0

    status, payload = apply(args.action, who=args.by, environ=environ)
    if status >= 400:
        _log(payload.get("error") or "failed")
        return 1
    if not args.quiet:
        print(payload["state"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
