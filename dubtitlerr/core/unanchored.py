#!/usr/bin/env python3
"""Per-show record of episodes whose repair had nothing to anchor on.

The pipeline already warns in the log when a release carries no English subtitle track
for the Japanese audio: every repair target is skipped and names stay as ASR heard them.
A log line nobody is watching is not a fix, so the same event is appended here and the
review UI offers the per-show ``unanchored_repair`` switch.

Append-only JSONL, the house idiom for incremental stage state: one line per affected
episode, no read-modify-write race between the generate loop and a merge pass, and a
killed run keeps what it wrote. ``read`` dedupes by episode, so re-running repair on the
same episode does not inflate the count.

Stdlib only: repair.py runs in the full image, but the review API must be able to read
this without dragging the pipeline in.
"""

from __future__ import annotations

import datetime
import json
import os

FILENAME = "unanchored.jsonl"


def _dir(environ=None):
    environ = os.environ if environ is None else environ
    return environ.get("CONFIG_DIR") or "/config"


def path(environ=None):
    return os.path.join(_dir(environ), FILENAME)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(show, episode, targets, environ=None):
    """Append one affected episode. Returns False for a missing show name, never raises OSError."""
    show = (show or "").strip()
    if not show:
        return False
    entry = {
        "show": show,
        "episode": os.path.basename(episode or ""),
        "targets": int(targets),
        "at": _now(),
    }
    os.makedirs(_dir(environ), exist_ok=True)
    with open(path(environ), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


def read(show, environ=None):
    """Aggregate one show's records, or ``{}`` when it has none.

    ``episodes`` counts distinct episode stems; ``targets`` is the largest single-episode
    target count (the number the operator is being asked to trade away); ``last_at`` and
    ``last_episode`` describe the most recent record in file order.
    """
    episodes = set()
    targets = 0
    last_at = ""
    last_episode = ""
    try:
        with open(path(environ), encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a torn tail line is not the operator's problem
                if not isinstance(entry, dict) or entry.get("show") != show:
                    continue
                episode = entry.get("episode") or ""
                if episode:
                    episodes.add(episode)
                try:
                    targets = max(targets, int(entry.get("targets") or 0))
                except (TypeError, ValueError):
                    pass
                last_at = entry.get("at") or last_at
                last_episode = episode or last_episode
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    if not episodes:
        return {}
    return {"episodes": len(episodes), "targets": targets, "last_at": last_at, "last_episode": last_episode}
