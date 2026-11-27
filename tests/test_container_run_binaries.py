"""Boot refuses when a media binary is missing: container_run.sh runs `python3 -m dubtitlerr.core.binaries` first.

The check runs before the up banner and before any loop starts. The block is driven under `sh`
with a stub python3, so the wiring (refuse, say why, start nothing) is pinned without depending
on the test host's PATH -- CI has no mkvmerge, and the dev machine must not be assumed either.
The module's own behavior is pinned in tests/test_binaries.py.
"""

import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = open(os.path.join(REPO, "container_run.sh")).read()


def block():
    start = SCRIPT.index("# >>> media binaries")
    end = SCRIPT.index("# <<< media binaries")
    return SCRIPT[start:end]


def run_boot(tmp_path, stub_rc):
    """The block, then "started": what a boot that got past the check would go on to do.

    The stub python3 shadows the real one via PATH order; `sh` and the shell builtins still
    resolve from the inherited PATH.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "python3"
    stub.write_text(f'#!/bin/sh\necho "FATAL: missing media binaries: ffmpeg, ffprobe, mkvmerge" >&2\nexit {stub_rc}\n')
    stub.chmod(0o755)
    script = f"set -u\n{block()}\necho started\n"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
    return subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, timeout=30)


def test_boot_refuses_before_starting_when_the_check_fails(tmp_path):
    res = run_boot(tmp_path, 1)
    assert res.returncode == 1
    assert "started" not in res.stdout.splitlines()
    assert "refusing to start" in res.stdout
    assert "missing media binaries" in res.stderr


def test_boot_proceeds_when_the_check_passes(tmp_path):
    res = run_boot(tmp_path, 0)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.splitlines() == ["started"]


def test_the_check_sits_before_the_up_banner_and_every_loop():
    assert SCRIPT.index("# >>> media binaries") < SCRIPT.index("==== dubtitle-builder up")
    assert SCRIPT.index("# >>> media binaries") < SCRIPT.index("merge loop in the background")
    assert SCRIPT.index("# >>> media binaries") < SCRIPT.index('sh "$APP_DIR/gen_loop.sh"')
