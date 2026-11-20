"""Phase 3 wiring: the shell loops and the review server report what they already know.

Every harness here replaces ``dubtitlerr/core/notify.py`` in the app directory with a recorder
that logs its argv, so the assertions are about the real CLI invocation the script makes (event,
summary, loop, counts, detail) -- no test reaches the network. The scripts' real notify module is
exercised by tests/test_notify.py, and copied unchanged into the other minimal app-dir harnesses.

Two properties are guarded throughout: an event is emitted at pass boundaries only (never mid
episode), and a notification that fails can never change a script's exit code or its stage records.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Recorder standing in for dubtitlerr/core/notify.py. It logs argv and can pretend to be subscribed
# to episode.delivered (the per-pass gate) or to be broken (the never-change-the-exit-code rule).
_NOTIFY_STUB = """\
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["NOTIFYLOG"], "a") as f:
    f.write(json.dumps(argv) + "\\n")
if argv[:1] == ["subscribed"]:
    sys.exit(0 if os.environ.get("NOTIFY_SUBSCRIBED") == "1" else 1)
sys.exit(int(os.environ.get("NOTIFY_EXIT", "0")))
"""

_STAGE_STUB = """\
import os, sys
sys.path.insert(0, {repo!r})
from dubtitlerr.core import common
common.OUTPUT_ROOT = ""
name = {name!r}
if "--delivered" in sys.argv:
    sys.exit(0 if os.path.exists(sys.argv[sys.argv.index("--delivered") + 1] + ".dubtitles.done") else 1)
arg = sys.argv[-1]
stem = arg
for suffix in (".dubtitles.conf.json", ".eng.dubtitles.srt", ".mkv", ".mp4"):
    if arg.endswith(suffix):
        stem = arg[: -len(suffix)]
        break
with open(os.environ["CALLLOG"], "a") as f:
    f.write(name + " " + os.path.basename(stem) + "\\n")
if os.environ.get("STUB_FAIL") == name:
    common.write_stage(stem, name, "crashed")
    sys.exit(1)
common.write_stage(stem, name, "ok")
if name == "signs":
    open(stem + ".eng.dubtitles.ass", "w").close()
    os.remove(stem + ".eng.dubtitles.srt")
if name == "mux":
    open(stem + ".dubtitles.done", "w").close()
"""


def _package_skeleton(app):
    for pkg in ("", "core", "glossary", "pipeline", "review"):
        (app / "dubtitlerr" / pkg).mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "dubtitlerr" / pkg / "__init__.py", app / "dubtitlerr" / pkg / "__init__.py")
    (app / "shell").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "shell" / "lib.sh", app / "shell" / "lib.sh")


def _notifications(log_path):
    """Every notify CLI invocation recorded, as {command, event, summary, loop, counts, detail}."""
    if not log_path.exists():
        return []
    calls = []
    for line in log_path.read_text().splitlines():
        argv = json.loads(line)
        call = {"command": argv[0] if argv else "", "event": "", "summary": "", "loop": "", "counts": None, "detail": None}
        for flag in ("--event", "--summary", "--loop"):
            if flag in argv:
                call[flag[2:]] = argv[argv.index(flag) + 1]
        for flag in ("--counts", "--detail"):
            if flag in argv:
                call[flag[2:]] = json.loads(argv[argv.index(flag) + 1])
        calls.append(call)
    return calls


def _events(log_path):
    return [call["event"] for call in _notifications(log_path) if call["command"] == "send"]


def _call(log_path, event):
    return next(call for call in _notifications(log_path) if call["command"] == "send" and call["event"] == event)


def _base_env():
    return {k: v for k, v in os.environ.items() if k not in ("LIBRARY_SWEEP", "WATCH_QUEUE_WINDOW_DAYS", "REQUIRE_ENG")}


# --- gen_loop.sh -----------------------------------------------------------------------------------------------------------


def _run_gen_loop(tmp_path, generate_body, *shows, unresolved=None, **extra_env):
    app = tmp_path / "app"
    _package_skeleton(app)
    shutil.copy(REPO / "dubtitlerr" / "review" / "review_requests.py", app / "dubtitlerr" / "review" / "review_requests.py")
    shutil.copy(REPO / "dubtitlerr" / "core" / "atomic_file.py", app / "dubtitlerr" / "core" / "atomic_file.py")
    (app / "dubtitlerr" / "core" / "notify.py").write_text(_NOTIFY_STUB)
    (app / "dubtitlerr" / "glossary" / "mine_glossary.py").write_text("")
    (app / "dubtitlerr" / "pipeline" / "generate.py").write_text(generate_body)
    anime = tmp_path / "anime"
    for show in shows:
        (anime / show).mkdir(parents=True)
    for show, text in (unresolved or {}).items():
        (anime / show / "ep01.dubtitles.unresolved.jsonl").write_text(text)
    order = tmp_path / "order.txt"
    order.write_text("".join(f"{show}\n" for show in shows))
    requests_dir = tmp_path / "config" / "requests"
    requests_dir.mkdir(parents=True)
    env = _base_env()
    env.update(
        APP_DIR=str(app),
        ANIME_ROOT=str(anime),
        ANIME_ORDER=str(order),
        CONFIG_DIR=str(tmp_path / "config"),
        REQUESTS_DIR=str(requests_dir),
        GLOSSARY_DIR=str(tmp_path / "gloss"),
        STOP_FLAG=str(tmp_path / "stop"),
        RESCAN_INTERVAL="3600",
        NOTIFYLOG=str(tmp_path / "notify.log"),
    )
    env.update(extra_env)
    proc = subprocess.run(["sh", str(REPO / "gen_loop.sh")], env=env, cwd=str(app), capture_output=True, text=True, timeout=60)
    return proc, tmp_path / "notify.log", anime


_GENERATE_OK = """\
import os, pathlib
pathlib.Path(os.environ["STOP_FLAG"]).touch()
"""


def test_a_sweep_reports_sweep_completed_with_the_counts_it_already_has(tmp_path):
    proc, log, _ = _run_gen_loop(tmp_path, _GENERATE_OK, "Frieren")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _events(log) == ["sweep.completed"], proc.stdout + proc.stderr
    call = _call(log, "sweep.completed")
    assert call["loop"] == "generate"
    assert call["counts"] == {"shows": 1, "failed": 0, "review_pending": 0}
    assert "1 show(s), 0 failed, 0 review item(s) pending" in call["summary"]


def test_review_pending_is_reported_only_when_the_show_has_open_items(tmp_path):
    pending = "\n".join(
        [
            json.dumps({"stage": "repair", "reason": "rejected_guard", "original_text": "a", "proposed_text": "b"}),
            json.dumps({"stage": "repair", "reason": "no_reference", "resolved": True}),
            json.dumps({"stage": "repair", "reason": "rejected_guard", "original_text": "c", "proposed_text": "d"}),
            "{torn final line",
        ]
    )
    proc, log, anime = _run_gen_loop(tmp_path, _GENERATE_OK, "Frieren", unresolved={"Frieren": pending + "\n"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    events = _events(log)
    assert events == ["sweep.completed", "review.pending"], events
    assert _call(log, "sweep.completed")["counts"]["review_pending"] == 2
    pending_call = _call(log, "review.pending")
    assert pending_call["counts"] == {"pending": 2}
    assert pending_call["loop"] == "generate"


def test_no_review_pending_event_when_every_entry_is_resolved(tmp_path):
    resolved = json.dumps({"stage": "repair", "reason": "no_reference", "resolved": True}) + "\n"
    proc, log, _ = _run_gen_loop(tmp_path, _GENERATE_OK, "Frieren", unresolved={"Frieren": resolved})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _events(log) == ["sweep.completed"]


def test_a_stalled_generate_reports_pass_failed_and_still_ends_the_sweep(tmp_path):
    stalled = """\
import os, pathlib, sys
pathlib.Path(os.environ["STOP_FLAG"]).touch()  # end the sweep after this show, as a real sweep run does
sys.exit(1)
"""
    proc, log, _ = _run_gen_loop(tmp_path, stalled, "Frieren")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "GENERATE stalled on Frieren" in proc.stdout
    events = _events(log)
    assert events == ["pass.failed", "sweep.completed"], events
    failed = _call(log, "pass.failed")
    assert failed["loop"] == "generate" and failed["detail"]["stage"] == "generate"
    assert "Frieren" in failed["summary"]
    assert _call(log, "sweep.completed")["counts"]["failed"] == 1


def test_a_broken_notify_never_changes_the_loops_exit_code(tmp_path):
    proc, log, _ = _run_gen_loop(tmp_path, _GENERATE_OK, "Frieren", NOTIFY_EXIT="3")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SWEEP COMPLETE" in proc.stdout


# --- merge_pass.sh ---------------------------------------------------------------------------------------------------------


def _fake_bin(tmp_path, name):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / name).write_text("#!/bin/sh\nexit 0\n")
    (bindir / name).chmod(0o755)
    return bindir


def _run_merge_pass(tmp_path, *, drop=None, **extra_env):
    app = tmp_path / "app"
    app.mkdir()
    shutil.copytree(REPO / "dubtitlerr", app / "dubtitlerr")
    for name, script in (("repair", "repair.py"), ("signs", "dub_signs_merge.py"), ("mux", "mux.py")):
        (app / "dubtitlerr" / "pipeline" / script).write_text(_STAGE_STUB.format(repo=str(REPO), name=name))
    (app / "dubtitlerr" / "core" / "notify.py").write_text(_NOTIFY_STUB)
    (app / "shell").mkdir()
    (app / "shell" / "lib.sh").write_text((REPO / "shell" / "lib.sh").read_text())
    if drop:
        (app / "dubtitlerr" / "pipeline" / drop).unlink()
    library = tmp_path / "library"
    stem = library / "Show" / "ep01"
    stem.parent.mkdir(parents=True)
    (stem.parent / "ep01.mkv").write_text("")
    (stem.parent / "ep01.eng.dubtitles.srt").write_text("")
    bindir = _fake_bin(tmp_path, "ffmpeg")
    _fake_bin(tmp_path, "mkvmerge")
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    env.update(
        MERGE_ROOTS=str(library),
        APP_DIR=str(app),
        PYTHONPATH=str(app),
        CONFIG_DIR=str(tmp_path / "config"),
        PUBLISH_GATE="0",
        CALLLOG=str(tmp_path / "calls.log"),
        NOTIFYLOG=str(tmp_path / "notify.log"),
    )
    env.update(extra_env)
    res = subprocess.run(
        ["/bin/sh", str(REPO / "merge_pass.sh")], env=env, cwd=str(library), capture_output=True, text=True, timeout=120
    )
    return res, tmp_path / "notify.log", str(stem)


def test_a_pass_end_reports_merge_completed_and_skips_per_episode_events_unless_subscribed(tmp_path):
    res, log, _ = _run_merge_pass(tmp_path, NOTIFY_SUBSCRIBED="0")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "MERGE PASS COMPLETE" in res.stdout
    assert _events(log) == ["merge.completed"], _events(log)
    call = _call(log, "merge.completed")
    assert call["loop"] == "merge"
    assert call["counts"] == {"delivered": 1, "failed": 0, "total_done": 1}
    assert call["summary"] == "merge pass: 1 delivered, 0 failed, 1 total done"


def test_a_subscriber_gets_one_episode_delivered_event_per_delivery(tmp_path):
    res, log, _ = _run_merge_pass(tmp_path, NOTIFY_SUBSCRIBED="1")
    assert res.returncode == 0, res.stdout + res.stderr
    events = _events(log)
    assert events == ["episode.delivered", "merge.completed"], events
    delivered = _call(log, "episode.delivered")
    assert delivered["summary"] == "delivered ep01"
    assert delivered["detail"] == {"mode": "mux"}
    assert delivered["loop"] == "merge"


def test_an_unsubscribed_pass_asks_the_gate_exactly_once_and_never_per_episode(tmp_path):
    res, log, _ = _run_merge_pass(tmp_path, NOTIFY_SUBSCRIBED="0")
    assert res.returncode == 0, res.stdout + res.stderr
    commands = [call["command"] for call in _notifications(log)]
    assert commands.count("subscribed") == 1, commands


def test_a_fatal_preflight_reports_pass_failed_with_stage_context(tmp_path):
    res, log, _ = _run_merge_pass(tmp_path, drop="dub_signs_merge.py")
    assert res.returncode != 0
    assert "FATAL:" in res.stdout
    assert _events(log) == ["pass.failed"], _events(log)
    call = _call(log, "pass.failed")
    assert call["loop"] == "merge"
    assert call["detail"]["stage"] == "preflight"
    assert "dub_signs_merge.py" in call["summary"]


def test_a_broken_notify_never_changes_the_pass_outcome(tmp_path):
    res, log, _ = _run_merge_pass(tmp_path, NOTIFY_EXIT="3")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "MERGE PASS COMPLETE" in res.stdout


# --- container_run.sh ------------------------------------------------------------------------------------------------------


def _run_container(tmp_path, notify_exit="0"):
    app = tmp_path / "app"
    app.mkdir()
    for pkg in ("", "core", "review"):
        (app / "dubtitlerr" / pkg).mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "dubtitlerr" / pkg / "__init__.py", app / "dubtitlerr" / pkg / "__init__.py")
    (app / "dubtitlerr" / "core" / "notify.py").write_text(_NOTIFY_STUB)
    (app / "dubtitlerr" / "review" / "review_server.py").write_text("import sys\nsys.exit(0)\n")
    (app / "shell").mkdir()
    (app / "shell" / "lib.sh").write_text((REPO / "shell" / "lib.sh").read_text())
    (app / "merge_pass.sh").write_text("#!/bin/sh\nexit 0\n")
    (app / "gen_loop.sh").write_text("#!/bin/sh\nexit 42\n")  # the container's own failure fence
    env = _base_env()
    env.update(
        APP_DIR=str(app),
        STOP_FLAG=str(tmp_path / "stop"),
        CONFIG_DIR=str(tmp_path / "config"),
        MERGE_INTERVAL="600",
        REVIEW_RESTART="600",
        NOTIFYLOG=str(tmp_path / "notify.log"),
        NOTIFY_EXIT=notify_exit,
    )
    res = subprocess.run(
        ["sh", str(REPO / "container_run.sh")], env=env, cwd=str(app), capture_output=True, text=True, timeout=60
    )
    return res, tmp_path / "notify.log"


def test_a_dying_generate_loop_reports_pass_failed_before_the_container_exits(tmp_path):
    res, log = _run_container(tmp_path)
    assert res.returncode == 42, res.stdout + res.stderr
    assert _events(log) == ["pass.failed"], _events(log)
    call = _call(log, "pass.failed")
    assert call["loop"] == "generate"
    assert call["detail"] == {"stage": "generate", "reason": "loop-exited"}
    assert "rc=42" in call["summary"]


def test_a_broken_notify_never_changes_the_containers_exit_code(tmp_path):
    res, log = _run_container(tmp_path, notify_exit="3")
    assert res.returncode == 42, res.stdout + res.stderr
