"""Processing state: running / paused / stopped, persisted under CONFIG_DIR.

The state is one tiny JSON file. Missing means *paused* for the application (container_run.sh
materialises it at boot), while a stage invoked directly outside the container keeps running
when no file exists -- those shell stages are not the app's boot path. A corrupt or unknown
file is always paused: the safe direction is to touch the library as little as possible.

The loops hold at their existing boundaries (gen: between shows/sweeps; merge: between stems;
glossary verify: between terms) without killing the unit in flight. Stop is today's soft stop:
the persisted `stopped` word plus STOP_FLAG, so the container drains and exits.
"""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from dubtitlerr.core import processing_state as ps

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO, "shell", "lib.sh")
CONTAINER_RUN = os.path.join(REPO, "container_run.sh")
GEN_LOOP = os.path.join(REPO, "gen_loop.sh")
MERGE_PASS = os.path.join(REPO, "merge_pass.sh")


# --- module: read / write / defaults ---------------------------------------------------------


def test_a_missing_file_reads_as_paused(tmp_path):
    info = ps.read({"CONFIG_DIR": str(tmp_path)})
    assert info["state"] == "paused"
    assert info["default"] is True
    assert info["by"] is None and info["at"] is None


def test_each_state_round_trips_with_who_and_when(tmp_path, monkeypatch):
    environ = {"CONFIG_DIR": str(tmp_path)}
    for state in ("running", "paused", "stopped"):
        info = ps.write(state, by="user:alice", environ=environ)
        assert info["state"] == state and info["by"] == "user:alice"
        assert info["at"] and info["default"] is False
        assert json.loads((tmp_path / "processing.json").read_text())["state"] == state
        assert ps.read(environ)["state"] == state


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[]",
        '{"state": "exploded"}',
        '{"state": 7}',
    ],
)
def test_a_corrupt_or_unknown_file_is_paused_not_running(tmp_path, raw):
    (tmp_path / "processing.json").write_text(raw)
    assert ps.read({"CONFIG_DIR": str(tmp_path)})["state"] == "paused"


def test_writing_an_unknown_state_is_refused_without_touching_the_file(tmp_path):
    environ = {"CONFIG_DIR": str(tmp_path)}
    ps.write("running", by="boot", environ=environ)
    with pytest.raises(ValueError):
        ps.write("exploded", by="user:alice", environ=environ)
    assert ps.read(environ)["state"] == "running"


def test_start_clears_a_stale_stop_flag_and_stop_writes_both(tmp_path):
    flag = tmp_path / "stop"
    flag.write_text("")
    environ = {"CONFIG_DIR": str(tmp_path), "STOP_FLAG": str(flag)}
    status, payload = ps.apply("start", "user:alice", environ)
    assert status == 202 and payload["state"] == "running"
    assert not flag.exists(), "Start must clear a stale soft-stop flag"
    status, payload = ps.apply("stop", "user:alice", environ)
    assert status == 202 and payload["state"] == "stopped" and flag.exists()
    assert ps.read(environ)["by"] == "user:alice"


def test_pause_neither_touches_the_stop_flag_nor_the_library(tmp_path):
    flag = tmp_path / "stop"
    environ = {"CONFIG_DIR": str(tmp_path), "STOP_FLAG": str(flag)}
    status, payload = ps.apply("pause", "user:alice", environ)
    assert status == 202 and payload["state"] == "paused"
    assert not flag.exists()


# --- CLI: usable with the container down -----------------------------------------------------


def _cli(tmp_path, *args, **env):
    full = {k: v for k, v in os.environ.items() if k != "CONFIG_DIR"}
    full.update({"CONFIG_DIR": str(tmp_path), "PYTHONPATH": REPO, **env})
    return subprocess.run(
        [sys.executable, "-m", "dubtitlerr.core.processing_state", *args],
        env=full,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_prints_the_current_state_and_defaults_to_paused(tmp_path):
    res = _cli(tmp_path, "show")
    assert res.returncode == 0 and res.stdout.strip() == "paused", res.stderr


def test_cli_sets_paused_before_a_recreate(tmp_path):
    res = _cli(tmp_path, "pause", "--by", "deploy")
    assert res.returncode == 0 and res.stdout.strip() == "paused", res.stderr
    info = ps.read({"CONFIG_DIR": str(tmp_path)})
    assert info["state"] == "paused" and info["by"] == "deploy"


def test_cli_stop_writes_the_state_and_the_flag_when_asked(tmp_path):
    flag = tmp_path / "stop"
    res = _cli(tmp_path, "stop", "--stop-flag", str(flag), "--by", "operator")
    assert res.returncode == 0 and flag.exists()
    assert ps.read({"CONFIG_DIR": str(tmp_path)})["state"] == "stopped"


# --- shell helper: the loops' view ------------------------------------------------------------


def _sh(script, **env):
    e = {k: v for k, v in os.environ.items() if k not in ("STOP_FLAG", "PROCESSING_STATE_FILE", "CONFIG_DIR")}
    e.update(env)
    return subprocess.run(["sh", "-c", f". {LIB}; {script}"], env=e, capture_output=True, text=True, timeout=30)


def test_shell_reads_each_state(tmp_path):
    f = tmp_path / "processing.json"
    for state in ("running", "paused", "stopped"):
        f.write_text(json.dumps({"state": state, "by": "x", "at": "2026-11-19T00:00:00Z"}))
        assert _sh("processing_state", PROCESSING_STATE_FILE=str(f)).stdout.strip() == state


def test_shell_treats_a_corrupt_file_as_paused_and_a_missing_one_as_running_for_direct_runs(tmp_path):
    f = tmp_path / "processing.json"
    f.write_text("{oops")
    assert _sh("processing_state", PROCESSING_STATE_FILE=str(f)).stdout.strip() == "paused"
    assert _sh("processing_state", PROCESSING_STATE_FILE=str(tmp_path / "absent.json")).stdout.strip() == "running"


def test_shell_hold_while_paused_returns_at_once_when_running(tmp_path):
    f = tmp_path / "processing.json"
    f.write_text('{"state": "running"}')
    t0 = time.monotonic()
    res = _sh("hold_while_paused; echo rc=$?", PROCESSING_STATE_FILE=str(f), PROCESSING_POLL_INTERVAL="1")
    assert res.returncode == 0 and "rc=0" in res.stdout
    assert time.monotonic() - t0 < 2


def test_shell_hold_while_paused_logs_once_and_resumes_when_started(tmp_path):
    f = tmp_path / "processing.json"
    f.write_text('{"state": "paused"}')
    e = {k: v for k, v in os.environ.items() if k not in ("STOP_FLAG", "PROCESSING_STATE_FILE", "CONFIG_DIR")}
    e.update({"PROCESSING_STATE_FILE": str(f), "PROCESSING_POLL_INTERVAL": "1"})
    proc = subprocess.Popen(
        ["sh", "-c", f". {LIB}; hold_while_paused; echo resumed"],
        env=e,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(1.2)
        f.write_text('{"state": "running"}')
        out, _ = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    assert "processing paused — holding" in out, out
    assert "resumed" in out


def test_shell_hold_while_paused_returns_nonzero_on_the_soft_stop(tmp_path):
    f = tmp_path / "processing.json"
    flag = tmp_path / "stop"
    f.write_text('{"state": "paused"}')
    flag.write_text("")
    res = _sh("hold_while_paused; echo rc=$?", PROCESSING_STATE_FILE=str(f), STOP_FLAG=str(flag), PROCESSING_POLL_INTERVAL="1")
    assert "rc=1" in res.stdout


# --- container boot: paused by default, review server always up -------------------------------

_STUB_MERGE = """#!/bin/sh
. "$APP_DIR/shell/lib.sh"
hold_while_paused || exit 0
echo "merge_pass ran" >> "$CALLLOG"
while :; do
	if [ -e "$STOP_FLAG" ]; then exit 0; fi
	sleep 0.2
done
"""
_STUB_GEN = """#!/bin/sh
. "$APP_DIR/shell/lib.sh"
hold_while_paused || exit 0
echo "gen_loop started" >> "$CALLLOG"
while :; do
	if [ -e "$STOP_FLAG" ]; then exit 0; fi
	if [ -e "$GEN_DIE" ]; then exit 7; fi
	sleep 0.2
done
"""
_STUB_REVIEW = """import time
open(__import__("os").environ["CALLLOG"], "a").write("review started\\n")
while True:
    time.sleep(0.2)
"""


def _boot(tmp_path, state=None):
    app = tmp_path / "app"
    app.mkdir()
    (app / "merge_pass.sh").write_text(_STUB_MERGE)
    (app / "gen_loop.sh").write_text(_STUB_GEN)
    (app / "shell").mkdir()
    (app / "shell" / "lib.sh").write_text(open(LIB).read())
    for pkg in ("", "review"):
        (app / "dubtitlerr" / pkg).mkdir(parents=True, exist_ok=True)
        (app / "dubtitlerr" / pkg / "__init__.py").write_text("")
    (app / "dubtitlerr" / "review" / "review_server.py").write_text(_STUB_REVIEW)
    config = tmp_path / "config"
    config.mkdir()
    if state is not None:
        (config / "processing.json").write_text(json.dumps({"state": state, "by": "test", "at": "2026-11-19T00:00:00Z"}))
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        **os.environ,
        "APP_DIR": str(app),
        "CONFIG_DIR": str(config),
        "STOP_FLAG": str(tmp_path / "stop.flag"),
        "CALLLOG": str(log),
        "GEN_DIE": str(tmp_path / "die"),
        "MERGE_INTERVAL": "1",
        "REVIEW_RESTART": "1",
        "RESCAN_INTERVAL": "1",
        "PROCESSING_POLL_INTERVAL": "1",
        "PYTHONPATH": str(app),
    }
    proc = subprocess.Popen(
        ["sh", CONTAINER_RUN],
        cwd=str(app),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    return proc, log, config


def _wait_for(path, text, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if text in path.read_text():
            return True
        time.sleep(0.1)
    return False


def _reap(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=10)


def test_boot_with_no_state_materialises_paused_and_processes_nothing(tmp_path):
    proc, log, config = _boot(tmp_path)
    try:
        assert _wait_for(log, "review started")
        time.sleep(1.5)
        assert (config / "processing.json").exists()
        assert json.loads((config / "processing.json").read_text())["state"] == "paused"
        assert "gen_loop started" not in log.read_text()
        assert "merge_pass ran" not in log.read_text()
        # Start from the UI/API is a state flip; the loops pick it up at their boundary.
        (config / "processing.json").write_text(json.dumps({"state": "running", "by": "user:alice", "at": "x"}))
        assert _wait_for(log, "gen_loop started"), log.read_text()
        assert _wait_for(log, "merge_pass ran"), log.read_text()
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
        assert proc.returncode == 0 and "soft stop complete" in out, out
    finally:
        _reap(proc)


def test_boot_with_a_stored_running_state_starts_the_loops(tmp_path):
    proc, log, _ = _boot(tmp_path, state="running")
    try:
        assert _wait_for(log, "gen_loop started") and _wait_for(log, "merge_pass ran") and _wait_for(log, "review started")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=20)
        finally:
            _reap(proc)


def test_boot_with_a_stored_stopped_state_keeps_the_ui_up_and_does_no_work(tmp_path):
    proc, log, _ = _boot(tmp_path, state="stopped")
    try:
        assert _wait_for(log, "review started")
        time.sleep(1.5)
        assert "gen_loop started" not in log.read_text()
        assert "merge_pass ran" not in log.read_text()
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=20)
        finally:
            _reap(proc)


# --- gen loop actually holds before the first show, then resumes ------------------------------


def _gen_stub_app(tmp_path):
    app = tmp_path / "app"
    (app / "shell").mkdir(parents=True)
    (app / "shell" / "lib.sh").write_text(open(LIB).read())
    for pkg in ("", "glossary", "pipeline", "core", "review"):
        (app / "dubtitlerr" / pkg).mkdir(parents=True, exist_ok=True)
        (app / "dubtitlerr" / pkg / "__init__.py").write_text("")
    (app / "dubtitlerr" / "glossary" / "mine_glossary.py").write_text("")
    (app / "dubtitlerr" / "pipeline" / "generate.py").write_text(
        "import os, pathlib\n"
        'open(os.environ["CALLLOG"], "a").write("generate " + os.environ["SHOW_NAME"] + "\\n")\n'
        "pathlib.Path(os.environ['STOP_FLAG']).touch()\n"
    )
    anime = tmp_path / "anime"
    (anime / "Show1").mkdir(parents=True)
    order = tmp_path / "order.txt"
    order.write_text("Show1\n")
    log = tmp_path / "calls.log"
    log.write_text("")
    config = tmp_path / "config"
    config.mkdir()
    (config / "processing.json").write_text('{"state": "paused", "by": "test", "at": "x"}')
    env = {
        **os.environ,
        "APP_DIR": str(app),
        "CONFIG_DIR": str(config),
        "ANIME_ORDER": str(order),
        "ANIME_ROOT": str(anime),
        "GLOSSARY_DIR": str(tmp_path / "gloss"),
        "REQUESTS_DIR": str(config / "requests"),
        "STOP_FLAG": str(tmp_path / "stop.flag"),
        "CALLLOG": str(log),
        "RESCAN_INTERVAL": "1",
        "PROCESSING_POLL_INTERVAL": "1",
        "PYTHONPATH": str(app),
    }
    (tmp_path / "gloss").mkdir()
    return env, log, config


def test_gen_loop_holds_while_paused_and_starts_after_a_start_flip(tmp_path):
    env, log, config = _gen_stub_app(tmp_path)
    proc = subprocess.Popen(
        ["sh", GEN_LOOP], env=env, cwd=env["APP_DIR"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        time.sleep(2.0)
        assert log.read_text().strip() == "", "the paused loop must not touch the library"
        (config / "processing.json").write_text('{"state": "running", "by": "user:alice", "at": "x"}')
        out, _ = proc.communicate(timeout=30)
        assert proc.returncode == 0, out
        assert "processing paused — holding" in out, out
        assert "generate Show1" in log.read_text(), log.read_text()
    finally:
        if proc.poll() is None:
            proc.kill()
