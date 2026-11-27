#!/usr/bin/env python3
"""The media binaries the pipeline shells out to -- checked once, at boot.

ffmpeg, ffprobe and mkvmerge are resolved through PATH at every call site (generate.py, mux.py,
reftracks.py, media_select.py, ...). In the supported image they are baked in (Dockerfile.lite
installs ffmpeg + mkvtoolnix), so the only ways one goes missing are a misbuilt image or a PATH
that never had it -- and then every episode fails on its own, one at a time, instead of one
clear refusal before any loop starts. ``container_run.sh`` runs this module at container boot
for exactly that reason: a missing binary is a CONFIGURATION error, and it is cheapest before
the loops have done anything.

``bsdtar`` (refs/acquire.py) is deliberately NOT required here: it guards itself with
``shutil.which`` and its absence only refuses .rar reference archives, which is a per-feature
refusal by design.

CLI: ``python3 -m dubtitlerr.core.binaries`` -- exits 0 when all are present, else 1 with the
missing names on stderr. Stdlib only, deliberately: the shell loops call this the same way they
call ``core/heartbeat.py`` and ``core/notify.py``.
"""

import shutil
import sys

REQUIRED = ("ffmpeg", "ffprobe", "mkvmerge")


def missing() -> list[str]:
    """The required binaries that do not resolve from PATH right now."""
    return [name for name in REQUIRED if shutil.which(name) is None]


def main() -> int:
    missing_now = missing()
    if not missing_now:
        return 0
    print(
        "FATAL: missing media binaries: "
        + ", ".join(missing_now)
        + " -- the image must provide them (Dockerfile.lite installs ffmpeg + mkvtoolnix); refusing to start",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
