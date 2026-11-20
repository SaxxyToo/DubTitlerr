"""``python3 -m dubtitlerr --version``: the release this checkout (or image) is running.

The pipeline stages are separate modules (``python3 -m dubtitlerr.pipeline.generate`` ...), so
there was no single host-side command that answered "which version is this?" -- the entrypoint
answers it for a container, and this answers it for a checkout, without importing the pipeline
and without touching a wire.
"""

import sys

from dubtitlerr import __version__


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv in (["--version"], ["-V"], ["version"]):
        print(f"dubtitlerr {__version__}")
        return 0
    print(f"dubtitlerr {__version__}\n\nusage: python3 -m dubtitlerr --version", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
