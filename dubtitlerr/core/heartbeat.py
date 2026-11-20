#!/usr/bin/env python3
"""The builder heartbeat: one JSON document the running loops write and every health reader reads.

Who reads it (all of them, one document, so they cannot disagree):

* ``review_server.handle_healthz`` -- ``GET /healthz`` (public) and, through
  ``review_api_v1.handle_health``, ``GET /api/v1/health``.
* ``review_api_v1._loop_state`` -- ``loop.state`` in ``GET /api/v1/system/status``:
  ``running`` (a sweep is in progress), ``stalled`` (see the staleness rule below),
  ``stopping`` (soft stop requested), ``idle``, or ``unknown`` when the document is missing.
* ``review_status._read_header`` -- ``builder_heartbeat_age_s`` on the Dashboard, taken from the
  file's mtime (which is why an idle loop must keep touching it) and ``current_show``.

Who writes it, and when (all through ``dubtitlerr.core.heartbeat`` on the command line):

* ``gen_loop.sh``: ``sweep-start`` before a sweep's work, ``show`` before each show / run
  request, ``sweep-end`` when the show loop finishes, and ``ping`` on every idle poll so the
  file stays fresh while the loop waits for the next sweep.
* ``merge_pass.sh``: ``merge-start`` when a pass starts, ``merge-end`` when it ends. Merge owns
  its own ``merge_*`` fields and never touches the sweep fields the readers judge.

Staleness rule (one constant, shared with the readers): a heartbeat whose ``last_sweep_end`` is
older than ``STALE_SWEEP_MULTIPLIER * RESCAN_INTERVAL`` is stale. ``RESCAN_INTERVAL`` is the
loop's own idle period (default 21600 s), so three missed rescans is the point at which the same
document turns ``/healthz`` into a 503 and ``loop.state`` into ``stalled``. A document with no
``last_sweep_end`` at all (a container that has never completed a sweep) is unhealthy, not
merely idle -- the reader's reason string is "stale: no sweep has completed yet".

Stdlib only, deliberately: this module is the one the shell loops call, and they run from a
minimal app directory that has no pysubs2 (see atomic_file.py). The write is best-effort by
contract -- a heartbeat is observability, so it must never block or fail a loop, exactly like
``core/notify.py``. ``write()`` reports failure with a False instead of raising, and the CLI
always exits 0.
"""

import argparse
import json
import os
import sys
import time

from dubtitlerr.core.atomic_file import atomic_write

DEFAULT_PATH = "/config/heartbeat.json"

# ``last_sweep_end`` may be this many RESCAN_INTERVALs old before a reader calls the builder
# unhealthy. Shared by /healthz and loop.state so a sweep that stopped reporting can never read
# as healthy on one route and stalled on the other.
STALE_SWEEP_MULTIPLIER = 3
DEFAULT_RESCAN_INTERVAL = 21600


def heartbeat_path(explicit=None):
    """The document's path: an explicit argument, else $HEARTBEAT_PATH, else the default.

    Read at CALL time, not import time: HEARTBEAT_PATH is a registered setting the loops apply
    at every boundary, and the tests point it at a tmp_path.
    """
    return explicit or os.environ.get("HEARTBEAT_PATH") or DEFAULT_PATH


def read(path=None):
    """The heartbeat document as a dict, or None when it is missing or not a JSON object."""
    try:
        with open(heartbeat_path(path)) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def write(path=None, **fields):
    """Merge ``fields`` into the document atomically. Returns True when the write landed.

    Merge, not replace: the generate loop and the merge pass write different fields to the same
    file, and a later writer must not drop an earlier writer's. A write that cannot land (a
    missing config volume, a read-only mount) returns False and leaves the previous document
    intact -- never raises OSError, so no caller has to defend against it.
    """
    target = heartbeat_path(path)
    doc = read(target) or {}
    doc.update(fields)
    return atomic_write(target, lambda f: json.dump(doc, f, sort_keys=True), mode=0o600, quiet=True)


def stale_after_seconds(rescan_interval=None):
    """Seconds after ``last_sweep_end`` at which a reader considers the builder stale."""
    raw = rescan_interval if rescan_interval is not None else os.environ.get("RESCAN_INTERVAL", DEFAULT_RESCAN_INTERVAL)
    try:
        rescan = int(raw)
    except (TypeError, ValueError):
        rescan = DEFAULT_RESCAN_INTERVAL
    return STALE_SWEEP_MULTIPLIER * rescan


def _readable_dir(path):
    """True when ``path`` is a directory this process can list -- what a sweep needs."""
    return bool(path) and os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)


def _fields(args):
    """The document fields one CLI event writes."""
    now = round(time.time(), 3)
    loop = args.loop or ("merge" if args.event.startswith("merge-") else "generate")
    fields = {"last_beat": now, "beat_by": loop}
    if args.event == "sweep-start":
        fields["last_sweep_start"] = now
        # A new sweep is not working on the previous sweep's show any more (the readers report
        # whatever current_show says until the first `show` event of this sweep).
        fields["current_show"] = args.show
        if args.root:
            fields["roots_readable"] = _readable_dir(args.root)
    elif args.event == "show":
        fields["current_show"] = args.show
    elif args.event == "sweep-end":
        fields["last_sweep_end"] = now
        fields["current_show"] = None
        if args.root:
            fields["roots_readable"] = _readable_dir(args.root)
        # With --order omitted the sweep ran without an order file (LIBRARY_SWEEP off: only
        # queued requests ran), so nothing gated on it and True is the honest value.
        fields["order_file_present"] = os.path.isfile(args.order) if args.order else True
        if args.shows is not None:
            fields["sweep_shows"] = args.shows
        if args.failed is not None:
            fields["sweep_failed"] = args.failed
    elif args.event == "merge-start":
        fields["merge_last_start"] = now
    elif args.event == "merge-end":
        fields["merge_last_end"] = now
        if args.delivered is not None:
            fields["merge_delivered"] = args.delivered
        if args.failed is not None:
            fields["merge_failed"] = args.failed
    return fields


def _parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m dubtitlerr.core.heartbeat",
        description="Write the builder heartbeat the health and status routes read.",
    )
    parser.add_argument("event", choices=["sweep-start", "show", "sweep-end", "merge-start", "merge-end", "ping"])
    parser.add_argument("--show", "--name", dest="show", help="sweep-start/show: the show this loop is working on")
    parser.add_argument("--root", help="sweep-start/sweep-end: the media root, recorded as roots_readable")
    parser.add_argument("--order", help="sweep-end: the order file the sweep read, recorded as order_file_present")
    parser.add_argument("--shows", type=int, help="sweep-end: shows this sweep ran")
    parser.add_argument("--failed", type=int, help="sweep-end/merge-end: failures this pass recorded")
    parser.add_argument("--delivered", type=int, help="merge-end: episodes this pass delivered")
    parser.add_argument("--loop", choices=["generate", "merge"], help="who is reporting (default: from the event)")
    return parser


def main(argv=None):
    """CLI entry point. Always returns 0: a heartbeat must never fail the loop that called it."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit:
        return 0
    try:
        write(**_fields(args))
    except Exception:  # noqa: BLE001 -- observability must never take a loop down
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
