import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Every generate.py call is logged as "SHOW|GENERATE_ONLY_RE". A request run optionally stops the loop (STOP_AFTER_REQUEST);
# a sweep run always does, so a loop that sweeps ends after its first show.
_STUB_GENERATE = """\
import os, pathlib
with open(os.environ["CALLLOG"], "a") as f:
    f.write(os.environ.get("SHOW_NAME", "") + "|" + os.environ.get("GENERATE_ONLY_RE", "") + "\\n")
if os.environ.get("GENERATE_ONLY_RE"):
    if os.environ.get("STOP_AFTER_REQUEST"):
        pathlib.Path(os.environ["STOP_FLAG"]).touch()
else:
    pathlib.Path(os.environ["STOP_FLAG"]).touch()
"""


def _run_loop(tmp_path, sweep, with_order=True, **extra):
    app = tmp_path / "app"
    (app / "shell").mkdir(parents=True)
    shutil.copy(REPO / "shell" / "lib.sh", app / "shell" / "lib.sh")
    # The app dir carries a package skeleton: gen_loop.sh drives every stage as
    # `python3 -m dubtitlerr.<pkg>.<mod>`. Only the files the loop's CLIs need are copied,
    # so this minimal app dir stays pysubs2-free (a stray module-scope `common` import fails).
    for pkg in ("", "core", "glossary", "pipeline", "review"):
        (app / "dubtitlerr" / pkg).mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "dubtitlerr" / pkg / "__init__.py", app / "dubtitlerr" / pkg / "__init__.py")
    shutil.copy(REPO / "dubtitlerr" / "review" / "review_requests.py", app / "dubtitlerr" / "review" / "review_requests.py")
    # review_requests now shares the atomic-write primitive, which is stdlib-only so this
    # minimal app dir stays pysubs2-free.
    shutil.copy(REPO / "dubtitlerr" / "core" / "atomic_file.py", app / "dubtitlerr" / "core" / "atomic_file.py")
    # review_requests reads the review queue from the artifact store (P39-B4); stdlib-only.
    shutil.copy(REPO / "dubtitlerr" / "core" / "artifact_store.py", app / "dubtitlerr" / "core" / "artifact_store.py")
    # The no-media-server bootstrap: gen_loop.sh runs this CLI when the order file is
    # missing, and it must import from the same pysubs2-free skeleton.
    shutil.copy(REPO / "dubtitlerr" / "core" / "media_types.py", app / "dubtitlerr" / "core" / "media_types.py")
    shutil.copy(REPO / "dubtitlerr" / "pipeline" / "library_queue.py", app / "dubtitlerr" / "pipeline" / "library_queue.py")
    # The loop reports at pass boundaries through the notify CLI. notify must import from this
    # pysubs2-free skeleton: a module-scope common/pysubs2 import here would fail every test.
    shutil.copy(REPO / "dubtitlerr" / "core" / "notify.py", app / "dubtitlerr" / "core" / "notify.py")
    (app / "dubtitlerr" / "glossary" / "mine_glossary.py").write_text("")
    (app / "dubtitlerr" / "pipeline" / "generate.py").write_text(_STUB_GENERATE)
    (app / "dubtitlerr" / "pipeline" / "watch_queue.py").write_text(
        "import os; open(os.environ['CALLLOG'] + '.watch', 'w').close()\n"
    )
    anime = tmp_path / "anime"
    (anime / "Frieren").mkdir(parents=True)
    (anime / "Frieren" / "S01E01.mkv").write_bytes(b"x")
    (anime / "Other").mkdir()
    order = tmp_path / "order.txt"
    if with_order:
        order.write_text("Other\n")
    requests_dir = tmp_path / "config" / "requests"
    requests_dir.mkdir(parents=True)
    (requests_dir / "20261012T100000Z-11111111.json").write_text(
        json.dumps(
            {
                "id": "20261012T100000Z-11111111",
                "show": "Frieren",
                "generate_only_re": r"Frieren.*S01E01",
                "status": "pending",
            }
        )
    )
    env = {k: v for k, v in os.environ.items() if k not in ("REQUIRE_ENG", "WATCH_QUEUE_WINDOW_DAYS", "LIBRARY_SWEEP")}
    env.update(
        APP_DIR=str(app),
        ANIME_ROOT=str(anime),
        ANIME_ORDER=str(order),
        CONFIG_DIR=str(tmp_path / "config"),
        REQUESTS_DIR=str(requests_dir),
        GLOSSARY_DIR=str(tmp_path / "gloss"),
        STOP_FLAG=str(tmp_path / "stop"),
        CALLLOG=str(tmp_path / "calls.log"),
        RESCAN_INTERVAL="3600",
        WATCH_QUEUE_WINDOW_DAYS="7",
    )
    if sweep is not None:
        env["LIBRARY_SWEEP"] = sweep
    env.update(extra)
    proc = subprocess.run(
        ["sh", str(REPO / "gen_loop.sh")], env=env, cwd=env["APP_DIR"], capture_output=True, text=True, timeout=30
    )
    log = tmp_path / "calls.log"
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls, requests_dir


def test_sweep_off_runs_the_queued_request_and_nothing_from_the_order_file(tmp_path):
    # Mutation caught: the order-file loop or the watch queue still runs with LIBRARY_SWEEP off (repair tokens spent unasked).
    proc, calls, _ = _run_loop(tmp_path, "false", STOP_AFTER_REQUEST="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls == [r"Frieren|Frieren.*S01E01"]
    assert not (tmp_path / "calls.log.watch").exists()


def test_sweep_off_with_no_order_file_still_serves_requests_without_the_idle_300(tmp_path):
    # Mutation caught: the 'no order file' idle (300 s) fires before the request is drained.
    proc, calls, _ = _run_loop(tmp_path, "0", with_order=False, STOP_AFTER_REQUEST="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls == [r"Frieren|Frieren.*S01E01"]
    assert "no order file" not in proc.stdout


def test_sweep_off_spellings_are_case_and_space_tolerant(tmp_path):
    proc, calls, _ = _run_loop(tmp_path, " Off ", STOP_AFTER_REQUEST="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls == [r"Frieren|Frieren.*S01E01"]


def test_sweep_on_runs_the_request_then_the_order_file(tmp_path):
    # Guard: today's behaviour. Passes before the change too.
    proc, calls, _ = _run_loop(tmp_path, "true")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls == [r"Frieren|Frieren.*S01E01", "Other|"]
    assert (tmp_path / "calls.log.watch").exists()


def test_sweep_unset_keeps_sweeping(tmp_path):
    proc, calls, _ = _run_loop(tmp_path, None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls[-1] == "Other|"


def test_a_missing_order_file_is_built_from_the_library_tree(tmp_path):
    # The no-media-server install: no order file, no watch queue answer. The loop must
    # queue what exists instead of idling forever. Mutation caught: the sweep never
    # starts, or library_queue overwrites a list the operator wrote.
    proc, calls, _ = _run_loop(tmp_path, "true", with_order=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls == [r"Frieren|Frieren.*S01E01", "Frieren|"]
    assert (tmp_path / "order.txt").read_text() == "Frieren\n"


def test_the_library_queue_does_not_touch_an_existing_order_file(tmp_path):
    # Guard: a hand-written (or watch-queue) order file stays authoritative.
    proc, calls, _ = _run_loop(tmp_path, "true")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / "order.txt").read_text() == "Other\n"
    assert "Other|" in calls
