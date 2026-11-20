"""The heartbeat writer: one document, written by the loops, read by every health surface.

v0.4.0 shipped the readers with no writer at all, so /healthz and /api/v1/health answered 503
"no heartbeat yet" forever and loop.state could only ever be "unknown". These tests pin the
writer (dubtitlerr/core/heartbeat.py + common.heartbeat) to the shape and the staleness rule the
readers already implement, end to end: CLI write -> common.read_heartbeat -> /healthz and
loop.state.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dubtitlerr.core import common, heartbeat
from dubtitlerr.review import review_api_v1
from dubtitlerr.review import review_server as rs

REPO = Path(__file__).resolve().parent.parent


def _cli(path, *args):
    """Run the writer the shell loops run: `python3 -m dubtitlerr.core.heartbeat ...`."""
    env = dict(os.environ, HEARTBEAT_PATH=str(path), PYTHONPATH=str(REPO))
    return subprocess.run(
        [sys.executable, "-m", "dubtitlerr.core.heartbeat", *args],
        env=env,
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _loop_state():
    return review_api_v1.handle_system_status(health=rs.handle_healthz)[1]["loop"]["state"]


def test_cli_sweep_start_show_and_end_are_a_round_trip(tmp_path, monkeypatch):
    """The three events gen_loop.sh emits, read back by the readers that ship with them."""
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(path))
    order = tmp_path / "order.txt"
    order.write_text("Frieren\n")

    assert _cli(path, "sweep-start", "--root", str(tmp_path)).returncode == 0
    doc = common.read_heartbeat()
    assert doc["current_show"] is None, "a sweep start names no show yet"
    assert doc["roots_readable"] is True
    assert doc["beat_by"] == "generate"
    start = doc["last_sweep_start"]

    assert _cli(path, "show", "--name", "Frieren").returncode == 0
    assert common.read_heartbeat()["current_show"] == "Frieren"
    # A sweep in progress is "running", not "unknown" -- the exact v0.4.0 degradation.
    assert common.read_heartbeat()["last_sweep_start"] > 0
    assert "last_sweep_end" not in common.read_heartbeat(), "no sweep has completed in this document"
    assert _loop_state() == "running"

    end = ("sweep-end", "--root", str(tmp_path), "--order", str(order), "--shows", "4", "--failed", "1")
    assert _cli(path, *end).returncode == 0
    doc = common.read_heartbeat()
    assert doc["last_sweep_end"] >= start
    assert doc["current_show"] is None, "a finished sweep is not working on a show"
    assert doc["order_file_present"] is True
    assert doc["sweep_shows"] == 4 and doc["sweep_failed"] == 1

    assert rs.handle_healthz() == (200, {"ok": True})
    assert rs.handle_healthz()[1]["ok"] is True
    assert _loop_state() == "idle"


def test_missing_order_file_is_reported_by_healthz(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(path))

    _cli(path, "sweep-end", "--root", str(tmp_path), "--order", str(tmp_path / "does-not-exist.txt"))

    status, payload = rs.handle_healthz()
    assert status == 503
    assert "order_file_present is false" in payload["reasons"]


def test_unreadable_root_is_reported_by_healthz(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(path))

    _cli(path, "sweep-end", "--root", str(tmp_path / "no-such-root"))

    status, payload = rs.handle_healthz()
    assert status == 503
    assert "roots_readable is false" in payload["reasons"]


def test_sweep_end_without_an_order_file_records_it_as_present(tmp_path, monkeypatch):
    """LIBRARY_SWEEP off: only queued requests ran, so no order file gated the sweep. Recording
    False here would turn /healthz into a permanent 503 on a request-only install."""
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(path))

    _cli(path, "sweep-end", "--root", str(tmp_path))

    assert common.read_heartbeat()["order_file_present"] is True
    assert rs.handle_healthz() == (200, {"ok": True})


def test_ping_keeps_the_sweep_fields_it_did_not_touch(tmp_path, monkeypatch):
    """The idle wait pings to keep the file (and so the Dashboard's age) fresh; it must not
    rewrite last_sweep_end and fake a sweep."""
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(path))
    _cli(path, "sweep-end", "--root", str(tmp_path))
    before = common.read_heartbeat()

    _cli(path, "ping")

    after = common.read_heartbeat()
    assert after["last_sweep_end"] == before["last_sweep_end"]
    assert after["last_beat"] > before["last_beat"]
    assert after["beat_by"] == "generate"


def test_merge_fields_do_not_clobber_the_generate_fields(tmp_path, monkeypatch):
    """Two writes merge into one document: a merge pass must not erase the sweep timestamps the
    health readers judge, and must not look like a sweep of its own."""
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(path))
    _cli(path, "sweep-start", "--root", str(tmp_path))
    _cli(path, "sweep-end", "--root", str(tmp_path))
    sweep = common.read_heartbeat()

    _cli(path, "merge-start")
    _cli(path, "merge-end", "--delivered", "3", "--failed", "1")
    doc = common.read_heartbeat()

    assert doc["last_sweep_end"] == sweep["last_sweep_end"]
    assert doc["last_sweep_start"] == sweep["last_sweep_start"]
    assert doc["merge_last_end"] >= doc["merge_last_start"]
    assert doc["merge_delivered"] == 3 and doc["merge_failed"] == 1
    assert rs.handle_healthz() == (200, {"ok": True}), "a merge pass changes no health verdict"


def test_an_unwritable_heartbeat_path_is_silent_and_exits_zero(tmp_path):
    """A read-only config volume, or a misbuilt image with no module at all, must never fail the
    loop that reported -- the same rule notifications live under."""
    path = tmp_path / "missing-dir" / "heartbeat.json"

    proc = _cli(path, "sweep-end", "--root", str(tmp_path))

    assert proc.returncode == 0
    assert proc.stderr == "", "a failed heartbeat write must not spam the loop's log"
    assert not path.exists()
    assert heartbeat.write(path, last_sweep_end=1.0) is False


def test_a_malformed_invocation_still_exits_zero(tmp_path):
    path = tmp_path / "heartbeat.json"

    assert _cli(path, "sweep-end", "--shows", "many").returncode == 0
    assert "usage:" in _cli(path, "nonsense").stderr


def test_healthz_keeps_503_without_a_document(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(tmp_path / "does-not-exist.json"))

    assert rs.handle_healthz() == (503, {"ok": False, "reasons": ["no heartbeat yet"]})
    assert review_api_v1.handle_system_status(health=rs.handle_healthz)[1]["loop"]["state"] == "unknown"


def test_staleness_boundary_is_one_constant_shared_by_healthz_and_loop_state(tmp_path, monkeypatch):
    """Both routes must flip on the same second, or a stopped sweep reads healthy on one and
    stalled on the other."""
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))
    monkeypatch.setenv("RESCAN_INTERVAL", "100")
    assert heartbeat.stale_after_seconds() == common.STALE_SWEEP_MULTIPLIER * 100 == 300
    now = time.time()

    common.heartbeat(last_sweep_start=now - 1000, last_sweep_end=now - 299)
    assert rs.handle_healthz()[0] == 200 and _loop_state() == "idle"

    common.heartbeat(last_sweep_end=now - 301)
    assert rs.handle_healthz()[0] == 503 and _loop_state() == "stalled"


def test_the_writer_module_imports_no_third_party_package():
    """The shell loops call this from a minimal app dir with no pysubs2 (like notify.py and
    atomic_file.py); importing it must not drag the pipeline in."""
    probe = "import sys, dubtitlerr.core.heartbeat as hb; print(sorted(m for m in ('pysubs2', 'jellyfish') if m in sys.modules))"
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        env=dict(os.environ, PYTHONPATH=str(REPO)),
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.stdout.strip() == "[]", proc.stdout + proc.stderr


def test_document_is_plain_json_the_readers_can_parse(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(path))
    _cli(path, "sweep-start", "--root", str(tmp_path))
    _cli(path, "sweep-end", "--root", str(tmp_path))

    doc = json.loads(path.read_text())
    for key in ("last_sweep_start", "last_sweep_end", "last_beat"):
        assert isinstance(doc[key], int | float)
