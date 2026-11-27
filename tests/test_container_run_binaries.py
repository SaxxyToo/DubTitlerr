"""Boot refuses when a media binary is missing: container_run.sh checks before any loop starts.

The check runs before the up banner and before any loop starts, and it uses shell builtins only
-- no python, no app package -- exactly like the flock claim, because a misbuilt app must still
boot while a missing ffmpeg/ffprobe/mkvmerge must not wait for the first episode to fail. The
block is driven under /bin/sh with a controlled PATH, so the outcome does not depend on what
the test host has installed (CI installs ffmpeg but not mkvtoolnix).
"""

import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = open(os.path.join(REPO, "container_run.sh")).read()


def block():
    start = SCRIPT.index("# >>> media binaries")
    end = SCRIPT.index("# <<< media binaries")
    return SCRIPT[start:end]


def run_boot(tmp_path, provide):
    bins = tmp_path / "bin"
    bins.mkdir(exist_ok=True)
    for name in provide:
        stub = bins / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    script = f"set -u\n{block()}\necho started\n"
    return subprocess.run(["/bin/sh", "-c", script], env={"PATH": str(bins)}, capture_output=True, text=True, timeout=30)


def test_boot_refuses_and_names_the_missing_binary(tmp_path):
    res = run_boot(tmp_path, provide=("ffmpeg", "mkvmerge"))
    assert res.returncode == 1
    assert "started" not in res.stdout.splitlines()
    assert "missing media binaries: ffprobe --" in res.stdout


def test_boot_names_every_binary_when_none_are_present(tmp_path):
    res = run_boot(tmp_path, provide=())
    assert res.returncode == 1
    assert "missing media binaries: ffmpeg ffprobe mkvmerge --" in res.stdout


def test_boot_proceeds_when_all_three_resolve(tmp_path):
    res = run_boot(tmp_path, provide=("ffmpeg", "ffprobe", "mkvmerge"))
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.splitlines()[-1] == "started"


def test_the_check_needs_no_python_or_app_package():
    # The design point, pinned: the block must survive the minimal app-dir harnesses and a
    # misbuilt image, so no executable line may delegate to the package the way other boot
    # steps do. (The comment explains the rule; only code lines count.)
    code = [line for line in block().splitlines() if not line.lstrip().startswith("#")]
    assert not any("python" in line for line in code)


def test_the_check_sits_before_the_up_banner_and_every_loop():
    assert SCRIPT.index("# >>> media binaries") < SCRIPT.index("==== dubtitle-builder up")
    assert SCRIPT.index("# >>> media binaries") < SCRIPT.index("merge loop in the background")
    assert SCRIPT.index("# >>> media binaries") < SCRIPT.index('sh "$APP_DIR/gen_loop.sh"')
