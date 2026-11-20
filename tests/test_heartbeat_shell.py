"""gen_loop.sh and merge_pass.sh are the heartbeat's writers, and the v0.4.0 bug was that nothing
called the writer at all. These drive the real shell loops end to end (dash in CI via
`scripts/check.sh --dash`) against a minimal pysubs2-free app dir, then read the file back through
the readers the review server actually serves.

Two shapes are pinned here:
  * the loops DO write -- sweep start / per show / sweep end, and a merge pass's own fields;
  * a misbuilt image (no heartbeat module) still completes the sweep: a reporting failure must
    never wedge a loop, exactly as runtime notifications must not.
"""

import json
import os
import shutil
import stat
import subprocess

import pytest

from dubtitlerr.core import common
from dubtitlerr.core import heartbeat as heartbeat_doc
from dubtitlerr.review import review_api_v1
from dubtitlerr.review import review_server as rs

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Snapshots the heartbeat the loop had while this show was running, then stops the loop (a sweep
# run always does, so the loop ends after its first show). The snapshot is what a review server
# polling /healthz would have seen at that moment.
_STUB_GENERATE = """\
import os, pathlib
src = os.environ.get("HEARTBEAT_PATH")
if src and os.path.exists(src):
    pathlib.Path(os.environ["HB_SNAPSHOT"]).write_text(open(src).read())
pathlib.Path(os.environ["STOP_FLAG"]).touch()
"""


def _skeleton(tmp_path, with_heartbeat=True):
    app = tmp_path / "app"
    (app / "shell").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "shell", "lib.sh"), app / "shell" / "lib.sh")
    for pkg in ("", "core", "glossary", "pipeline", "review"):
        (app / "dubtitlerr" / pkg).mkdir(parents=True, exist_ok=True)
        shutil.copy(os.path.join(REPO, "dubtitlerr", pkg, "__init__.py"), app / "dubtitlerr" / pkg / "__init__.py")
    for mod in ("review/review_requests.py", "core/atomic_file.py", "core/notify.py"):
        shutil.copy(os.path.join(REPO, "dubtitlerr", mod), app / "dubtitlerr" / mod)
    if with_heartbeat:
        shutil.copy(os.path.join(REPO, "dubtitlerr", "core", "heartbeat.py"), app / "dubtitlerr" / "core" / "heartbeat.py")
    for mod in ("pipeline/repair.py", "pipeline/dub_signs_merge.py", "pipeline/mux.py", "pipeline/publish_gate.py"):
        (app / "dubtitlerr" / mod).write_text("")
    (app / "dubtitlerr" / "glossary" / "mine_glossary.py").write_text("")
    (app / "dubtitlerr" / "pipeline" / "generate.py").write_text(_STUB_GENERATE)
    return app


def _gen_env(tmp_path, app, with_order=True, with_request=False):
    anime = tmp_path / "anime"
    (anime / "Frieren").mkdir(parents=True)
    order = tmp_path / "order.txt"
    if with_order:
        order.write_text("Frieren\n")
    config = tmp_path / "config"
    (config / "requests").mkdir(parents=True)
    if with_request:
        (config / "requests" / "20261012T100000Z-11111111.json").write_text(
            json.dumps({"id": "20261012T100000Z-11111111", "show": "Frieren", "generate_only_re": "Frieren", "status": "pending"})
        )
    env = dict(os.environ)
    for key in ("REQUIRE_ENG", "WATCH_QUEUE_WINDOW_DAYS", "LIBRARY_SWEEP", "HEARTBEAT_PATH", "RESCAN_INTERVAL"):
        env.pop(key, None)
    env.update(
        APP_DIR=str(app),
        ANIME_ROOT=str(anime),
        ANIME_ORDER=str(order),
        CONFIG_DIR=str(config),
        REQUESTS_DIR=str(config / "requests"),
        GLOSSARY_DIR=str(tmp_path / "gloss"),
        STOP_FLAG=str(tmp_path / "stop"),
        HB_SNAPSHOT=str(tmp_path / "hb-during-show.json"),
        HEARTBEAT_PATH=str(config / "heartbeat.json"),
        RESCAN_INTERVAL="3600",
    )
    return env


def _run_gen_loop(tmp_path, with_heartbeat=True, with_order=True, with_request=False):
    app = _skeleton(tmp_path, with_heartbeat=with_heartbeat)
    env = _gen_env(tmp_path, app, with_order=with_order, with_request=with_request)
    proc = subprocess.run(
        ["sh", os.path.join(REPO, "gen_loop.sh")], env=env, cwd=env["APP_DIR"], capture_output=True, text=True, timeout=60
    )
    return proc, env


def _hb(env):
    path = os.path.join(env["CONFIG_DIR"], "heartbeat.json")
    with open(path) as f:
        return json.load(f)


def test_gen_loop_writes_sweep_start_show_and_sweep_end(tmp_path, monkeypatch):
    proc, env = _run_gen_loop(tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    during = json.load(open(env["HB_SNAPSHOT"]))
    assert during["current_show"] == "Frieren", "the show being transcribed must be in the document"
    assert during["last_sweep_start"] > 0

    doc = _hb(env)
    assert doc["last_sweep_end"] >= doc["last_sweep_start"]
    assert doc["current_show"] is None, "a finished sweep is not working on a show"
    assert doc["roots_readable"] is True
    assert doc["order_file_present"] is True
    assert doc["sweep_shows"] == 1
    assert doc["beat_by"] == "generate"

    # ...and the readers agree, which is the whole point of the file: v0.4.0 answered 503 forever.
    monkeypatch.setattr(common, "HEARTBEAT_PATH", os.path.join(env["CONFIG_DIR"], "heartbeat.json"))
    assert rs.handle_healthz() == (200, {"ok": True})
    state = review_api_v1.handle_system_status(health=rs.handle_healthz)[1]["loop"]["state"]
    assert state == "idle"


def test_a_sweep_off_install_records_no_missing_order_file(tmp_path, monkeypatch):
    """LIBRARY_SWEEP off runs only queued requests (ORDER=/dev/null): recording that as a missing
    order file would make /healthz permanently 503 on a request-only install."""
    app = _skeleton(tmp_path)
    env = _gen_env(tmp_path, app, with_order=False, with_request=True)
    env["LIBRARY_SWEEP"] = "0"
    proc = subprocess.run(
        ["sh", os.path.join(REPO, "gen_loop.sh")], env=env, cwd=env["APP_DIR"], capture_output=True, text=True, timeout=60
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    doc = _hb(env)
    assert doc["last_sweep_end"] >= doc["last_sweep_start"]
    assert doc["order_file_present"] is True
    monkeypatch.setattr(common, "HEARTBEAT_PATH", os.path.join(env["CONFIG_DIR"], "heartbeat.json"))
    assert rs.handle_healthz() == (200, {"ok": True})


def test_a_misbuilt_image_without_the_heartbeat_module_still_completes_the_sweep(tmp_path):
    """The never-wedge rule: a missing module is a reporting failure, not a loop failure."""
    proc, env = _run_gen_loop(tmp_path, with_heartbeat=False)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SWEEP COMPLETE" in proc.stdout
    assert not os.path.exists(os.path.join(env["CONFIG_DIR"], "heartbeat.json"))


def _fake_bin(tmp_path, name):
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    p = bindir / name
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(bindir)


def test_merge_pass_writes_its_own_fields_without_touching_the_sweep_fields(tmp_path):
    app = _skeleton(tmp_path)
    root = tmp_path / "library"
    root.mkdir()
    config = tmp_path / "config"
    config.mkdir()

    # A sweep has already reported: the pass must merge next to it, not replace it.
    heartbeat_path = str(config / "heartbeat.json")
    assert heartbeat_doc.write(heartbeat_path, last_sweep_start=1000.0, last_sweep_end=2000.0, current_show=None)

    env = {k: v for k, v in os.environ.items() if k not in ("HEARTBEAT_PATH", "CONFIG_DIR")}
    env["PATH"] = os.pathsep.join([_fake_bin(tmp_path, "ffmpeg"), _fake_bin(tmp_path, "mkvmerge"), env["PATH"]])
    env.update(
        APP_DIR=str(app),
        MERGE_ROOTS=str(root),
        CONFIG_DIR=str(config),
        HEARTBEAT_PATH=heartbeat_path,
        PUBLISH_GATE="0",
        STOP_FLAG=str(tmp_path / "stop"),
    )
    proc = subprocess.run(
        ["sh", os.path.join(REPO, "merge_pass.sh")], env=env, cwd=str(root), capture_output=True, text=True, timeout=60
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    doc = json.load(open(heartbeat_path))
    assert doc["merge_last_end"] >= doc["merge_last_start"] > 0
    assert doc["merge_delivered"] == 0 and doc["merge_failed"] == 0
    assert doc["last_sweep_end"] == 2000.0, "a merge pass must not fake a sweep end"
    assert doc["last_sweep_start"] == 1000.0


@pytest.mark.parametrize("script", ["gen_loop.sh", "merge_pass.sh"])
def test_the_scripts_never_let_a_heartbeat_failure_abort_them(script):
    """Every call site must be failure-swallowed: one `|| true` per invocation, no bare call."""
    body = open(os.path.join(REPO, script)).read()
    calls = [ln.strip() for ln in body.splitlines() if "dubtitlerr.core.heartbeat" in ln and "python3 -m" in ln]
    assert calls, f"{script} does not call the heartbeat writer at all"
    for line in calls:
        assert line.endswith("|| true"), f"{script}: unguarded heartbeat call: {line}"
