"""A request is only `done` when its scope shows the work on disk.

Measured 2026-11-18: a forced season request finished "done, error: null" in nine minutes
having produced zero subtitles. The force fix (9ffa1de) parked the delivery markers inside
force_park_sidecars(), but that only runs for episodes already on the to-do list, and
needs_work() dropped every delivered episode first: todo=0 again. And gen_loop marked a
request done on generate's exit code alone, which says the process ran, not that anything
was built. The decision now comes from a scope report generate writes from the files on disk.
"""

import json
import os
import sys
import time
import types

import pytest


def _stub_faster_whisper():
    if "faster_whisper" not in sys.modules:
        fake = types.ModuleType("faster_whisper")

        class _Unused:
            def __init__(self, *a, **kw):
                raise AssertionError("WhisperModel must never be instantiated here")

        fake.WhisperModel = _Unused
        sys.modules["faster_whisper"] = fake


_stub_faster_whisper()
from dubtitlerr.core import common  # noqa: E402
from dubtitlerr.pipeline import generate  # noqa: E402
from dubtitlerr.review import review_requests as rr  # noqa: E402


def _show(tmp_path, n=3, delivered=True):
    eps = []
    for i in range(1, n + 1):
        v = tmp_path / f"Show - S01E{i:02d}.mkv"
        v.write_text("video")
        if delivered:
            common.write_stamp(str(v)[:-4] + common.STAMP_SUFFIX, str(v), mode="sidecar")
            (tmp_path / f"Show - S01E{i:02d}.dub.en.ass").write_text("old")
            conf = tmp_path / f"Show - S01E{i:02d}.dubtitles.conf.json"
            conf.write_text("[]")
            old = time.time() - 86400
            os.utime(conf, (old, old))
        eps.append(str(v))
    return eps


def _run(tmp_path, monkeypatch, *, force, process=None, since=None):
    report = tmp_path / "report.json"
    monkeypatch.setattr(common, "OUTPUT_ROOT", "")
    monkeypatch.setattr(generate, "out_for", lambda p: p)
    monkeypatch.setenv("SIDECAR_SUFFIX", ".dub.en")
    monkeypatch.setenv("RUN_REPORT", str(report))
    monkeypatch.setenv("REQUEST_SINCE", str(since if since is not None else time.time() - 1))
    monkeypatch.setenv("FORCE_REDO", "1" if force else "")
    monkeypatch.setenv("ASR_BACKEND", "endpoint")
    monkeypatch.setattr(generate, "ASR_BACKEND", "endpoint")
    monkeypatch.setattr(generate, "endpoint_key_problem", lambda: "")
    monkeypatch.setattr(generate, "load_glossary", lambda: None)
    monkeypatch.setattr(generate, "GLOSS_DIR", str(tmp_path / "gloss"))
    calls = []

    def fake_process(v):
        calls.append(os.path.basename(v))
        if process:
            return process(v)
        open(v[:-4] + ".dubtitles.conf.json", "w").write("[]")
        open(v[:-4] + ".eng.dubtitles.srt", "w").write("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        return "ok"

    monkeypatch.setattr(generate, "process", fake_process)
    monkeypatch.setattr(sys, "argv", ["generate.py", "--root", str(tmp_path)])
    generate.main()
    return calls, json.loads(report.read_text())


# --- generate: force reaches delivered episodes, once per request ---------------------------


def test_force_rebuilds_episodes_that_are_already_delivered(tmp_path, monkeypatch):
    _show(tmp_path, 3, delivered=True)
    calls, rep = _run(tmp_path, monkeypatch, force=True)
    assert len(calls) == 3, "a forced request must reach delivered episodes"
    assert (rep["scope"], rep["rebuilt"]) == (3, 3)
    assert rr.verdict(rep, 0) == (True, None)


def test_a_resumed_forced_request_does_not_rebuild_an_episode_twice(tmp_path, monkeypatch):
    # gen_loop re-runs generate after a crash; the request's start time is the same across
    # those passes, so an episode rebuilt in pass one is not parked and rebuilt again.
    _show(tmp_path, 3, delivered=True)
    since = time.time() - 1
    _run(tmp_path, monkeypatch, force=True, since=since)
    calls, rep = _run(tmp_path, monkeypatch, force=True, since=since)
    assert calls == []
    assert rep["rebuilt"] == 3 and rr.verdict(rep, 0) == (True, None)


def test_without_force_a_delivered_show_is_untouched_and_done(tmp_path, monkeypatch):
    _show(tmp_path, 3, delivered=True)
    calls, rep = _run(tmp_path, monkeypatch, force=False)
    assert calls == []
    assert (rep["delivered"], rep["rebuilt"]) == (3, 0)
    assert rr.verdict(rep, 0) == (True, None)


def test_the_report_is_written_when_there_is_nothing_to_do(tmp_path, monkeypatch):
    _show(tmp_path, 2, delivered=True)
    _calls, rep = _run(tmp_path, monkeypatch, force=False)
    assert rep["scope"] == 2


def test_episodes_that_could_not_be_built_are_counted_by_reason(tmp_path, monkeypatch):
    _show(tmp_path, 3, delivered=False)
    status = {"S01E01": "ok", "S01E02": "no-eng-dub", "S01E03": "extract-failed"}

    def process(v):
        s = next(st for k, st in status.items() if k in v)
        if s == "ok":
            open(v[:-4] + ".dubtitles.conf.json", "w").write("[]")
        return s

    _calls, rep = _run(tmp_path, monkeypatch, force=False, process=process)
    assert rep["rebuilt"] == 1
    assert rep["not_done"] == {"no-eng-dub": 1, "extract-failed": 1}
    ok, why = rr.verdict(rep, 0)
    assert not ok and "2 of 3" in why and "no-eng-dub" in why


# --- review_requests.verdict: the decision, from the report alone ----------------------------


def test_verdict_refuses_a_forced_run_that_rebuilt_nothing():
    rep = {"scope": 28, "forced": True, "rebuilt": 0, "delivered": 28, "awaiting_merge": 0, "not_done": {}}
    ok, why = rr.verdict(rep, 0)
    assert not ok and "rebuilt nothing" in why and "28" in why


def test_verdict_refuses_an_empty_scope():
    ok, why = rr.verdict({"scope": 0, "forced": False, "rebuilt": 0, "delivered": 0, "awaiting_merge": 0, "not_done": {}}, 0)
    assert not ok and "no episode" in why


def test_verdict_fails_closed_without_a_report():
    ok, why = rr.verdict(None, 0)
    assert not ok and "report" in why


def test_verdict_keeps_the_exit_code_failure():
    rep = {"scope": 3, "forced": False, "rebuilt": 3, "delivered": 0, "awaiting_merge": 0, "not_done": {}}
    ok, why = rr.verdict(rep, 3)
    assert not ok and "rc=3" in why


def test_episodes_awaiting_the_merge_pass_count_as_done():
    rep = {"scope": 4, "forced": False, "rebuilt": 1, "delivered": 1, "awaiting_merge": 2, "not_done": {}}
    assert rr.verdict(rep, 0) == (True, None)


def test_finish_attaches_the_report(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"id": "r", "status": "running"}))
    rep = {"scope": 1, "forced": False, "rebuilt": 1, "delivered": 0, "awaiting_merge": 0, "not_done": {}}
    dest = rr.finish(str(p), ok=True, report=rep)
    assert json.load(open(dest))["report"] == rep


@pytest.mark.parametrize("rc", ["0", "3"])
def test_finish_report_cli_decides_and_files_the_request(tmp_path, monkeypatch, rc):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"id": "r", "status": "running"}))
    rep = tmp_path / "rep.json"
    rep.write_text(json.dumps({"scope": 2, "forced": True, "rebuilt": 0, "delivered": 2, "awaiting_merge": 0, "not_done": {}}))
    monkeypatch.setattr(sys, "argv", ["review_requests.py", "finish-report", str(p), str(rep), rc])
    rr.main()
    failed = json.load(open(tmp_path / "failed" / "r.json"))
    assert failed["status"] == "failed" and failed["report"]["rebuilt"] == 0
