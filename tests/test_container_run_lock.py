"""One worker per library: a second container on the same /config refuses to start.

Nothing in the pipeline locks across workers, so two workers on one library race on
`<stem>.muxtmp.mkv` and re-transcribe each other's episodes (AGENTS.md). container_run.sh takes
a container-level flock on the config volume; these tests drive the real block under `sh` and
simulate the second start by holding the same lock from the test process.
"""

import fcntl
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = open(os.path.join(REPO, "container_run.sh")).read()


def block():
    start = SCRIPT.index("# >>> single worker")
    end = SCRIPT.index("# <<< single worker")
    return SCRIPT[start:end]


def start(tmp_path, env=None):
    """The block, then "started": what a boot that got past the claim would go on to do."""
    script = f"set -u\n{block()}\necho started\n"
    full = {**os.environ, "CONFIG_DIR": str(tmp_path), **(env or {})}
    return subprocess.run(["sh", "-c", script], env=full, capture_output=True, text=True, timeout=30)


def hold(path):
    """A worker that is already running: an exclusive flock on the same file."""
    f = open(path, "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def test_the_first_worker_starts_and_leaves_the_lock_behind(tmp_path):
    res = start(tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.splitlines() == ["started"]
    assert (tmp_path / "worker.lock").exists(), "the claim is a file on the config volume"


def test_a_second_worker_refuses_to_start_and_says_why(tmp_path):
    holder = hold(tmp_path / "worker.lock")
    try:
        res = start(tmp_path)
    finally:
        holder.close()
    assert res.returncode == 1
    assert "started" not in res.stdout, "the loops must not run"
    assert "another worker already holds" in res.stdout
    assert os.path.join(str(tmp_path), "worker.lock") in res.stdout


def test_the_lock_is_released_with_its_holder_so_a_restart_needs_no_cleanup(tmp_path):
    holder = hold(tmp_path / "worker.lock")
    assert start(tmp_path).returncode == 1
    holder.close()  # the crashed/stopped worker is gone: the kernel drops the flock
    again = start(tmp_path)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "started" in again.stdout


def test_the_claim_is_taken_before_any_loop_starts(tmp_path):
    assert SCRIPT.index("# >>> single worker") < SCRIPT.index("merge loop in the background")
    assert SCRIPT.index("# >>> single worker") < SCRIPT.index('sh "$APP_DIR/gen_loop.sh"')
    assert "worker.lock" in block() and "$CONFIG_DIR/worker.lock" in block(), (
        "the lock lives on the config volume, so two containers sharing /config contend for it"
    )
