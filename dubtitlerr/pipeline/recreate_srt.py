#!/usr/bin/env python3
"""Rebuild <stem>.eng.dubtitles.srt from the stored conf card list (kind ``conf``) — used
when the srt was already consumed by a (buggy) assemble and we want to re-assemble without
re-transcribing. Pass stems (or legacy conf.json paths) as args.  Built with help of Claude (Anthropic)."""

import os
import sys

from dubtitlerr.core.common import read_conf, ts_srt
from dubtitlerr.pipeline import reflow

CONF_SUFFIX = ".dubtitles.conf.json"


def recreate(target: str) -> str | None:
    """Rebuild one srt from its stored conf. Returns the srt path, or None if it already
    exists or the episode has no conf. Split out of the argv loop so the wrap below is
    testable: importing this module used to execute the loop against pytest's own sys.argv."""
    stem = target[: -len(CONF_SUFFIX)] if target.endswith(CONF_SUFFIX) else target
    srt = stem + ".eng.dubtitles.srt"
    if os.path.exists(srt):
        return None
    d = read_conf(stem)
    if d is None:
        return None
    # conf stores text FLATTENED (generate.py replaces '\n' with ' '), so re-wrap
    # here -- same defect, same fix as repair.py's srt rewrite. Without it every episode
    # recreated this way ships as unwrapped single lines.
    with open(srt, "w") as f:
        for i, c in enumerate(d, 1):
            f.write(f"{i}\n{ts_srt(c['start'])} --> {ts_srt(c['end'])}\n{reflow.wrap_balance(c['text'])}\n\n")
    return srt


if __name__ == "__main__":
    for target in sys.argv[1:]:
        out = recreate(target)
        if out:
            print("recreated", os.path.basename(out))
