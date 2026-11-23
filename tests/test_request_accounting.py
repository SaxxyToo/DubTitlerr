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
            # The request in _run() executes on the endpoint backend, so a delivered episode
            # is stamped with THAT decoder identity. Stamping it with the host's default
            # (local) one would read model-stale under P12 and the episode would be neither
            # delivered nor untouched -- a different test than this one.
            saved = os.environ.get("ASR_BACKEND")
            os.environ["ASR_BACKEND"] = "endpoint"
            try:
                common.write_stamp(str(v)[:-4] + common.STAMP_SUFFIX, str(v), mode="sidecar")
            finally:
                if saved is None:
                    os.environ.pop("ASR_BACKEND", None)
                else:
                    os.environ["ASR_BACKEND"] = saved
            (tmp_path / f"Show - S01E{i:02d}.dub.en.ass").write_text("old")
            conf = tmp_path / f"Show - S01E{i:02d}.dubtitles.conf.json"
            conf.write_text("[]")
            old = time.time() - 86400
            os.utime(conf, (old, old))
        eps.append(str(v))
    return eps


def _run(tmp_path, monkeypatch, *, force, process=None, since=None, force_env=None, text_seen=None):
    report = tmp_path / "report.json"
    monkeypatch.setattr(common, "OUTPUT_ROOT", "")
    monkeypatch.setattr(generate, "out_for", lambda p: p)
    monkeypatch.setenv("SIDECAR_SUFFIX", ".dub.en")
    monkeypatch.setenv("RUN_REPORT", str(report))
    monkeypatch.setenv("REQUEST_SINCE", str(since if since is not None else time.time() - 1))
    monkeypatch.setenv("FORCE_REDO", force_env if force_env is not None else ("1" if force else ""))
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

    def fake_process_text(v):
        if text_seen is not None:
            text_seen.append(os.path.basename(v))
        open(v[:-4] + ".dubtitles.conf.json", "w").write("[]")
        open(v[:-4] + ".eng.dubtitles.srt", "w").write("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        return "ok"

    monkeypatch.setattr(generate, "process", fake_process)
    monkeypatch.setattr(generate, "process_text", fake_process_text)
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


def _give_words(tmp_path, n, monkeypatch):
    """A usable words.json for episodes 1..n -- the transcript the text tier replays."""
    monkeypatch.setattr(generate, "ASR_BACKEND", "endpoint")  # same identity read_words checks
    words = [{"start": 0.0, "end": 1.5, "word": "hi", "prob": 0.9, "text": "Hi."}]
    for i in range(1, n + 1):
        generate.write_words(str(tmp_path / f"Show - S01E{i:02d}"), words, [], 2.0, initial_prompt=generate.INITIAL_PROMPT)


def test_a_forced_rebuild_replays_text_from_cached_words(tmp_path, monkeypatch):
    """force is "start over", not "re-transcribe": with a usable words.json the text tier
    re-runs and the decoder is never called, so a rebuild costs CPU minutes, not GPU hours."""
    _show(tmp_path, 2, delivered=True)
    _give_words(tmp_path, 2, monkeypatch)
    text_seen = []

    calls, rep = _run(tmp_path, monkeypatch, force=True, text_seen=text_seen)

    assert calls == [], "usable words.json must keep the decoder out of a forced rebuild"
    assert text_seen == ["Show - S01E01.mkv", "Show - S01E02.mkv"]
    assert (rep["scope"], rep["rebuilt"]) == (2, 2)
    assert rr.verdict(rep, 0) == (True, None)
    # The replay READS words.json, so force must leave it in place...
    words = tmp_path / f"Show - S01E01{common.WORDS_SUFFIX}"
    assert words.exists() and not words.with_name(words.name + ".stale").exists()
    # ...but the delivery stamp is parked, which is what makes the merge pass re-mux.
    assert (tmp_path / f"Show - S01E01{common.STAMP_SUFFIX}.stale").exists()


def test_a_forced_rebuild_only_transcribes_the_episodes_without_words(tmp_path, monkeypatch):
    _show(tmp_path, 2, delivered=True)
    _give_words(tmp_path, 1, monkeypatch)  # only E01 has a cache
    text_seen = []

    calls, rep = _run(tmp_path, monkeypatch, force=True, text_seen=text_seen)

    assert text_seen == ["Show - S01E01.mkv"]
    assert calls == ["Show - S01E02.mkv"]
    assert (rep["scope"], rep["rebuilt"]) == (2, 2)


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


def _force_redo_from_request_file(tmp_path, monkeypatch, capsys, *, force):
    """FORCE_REDO as gen_loop.sh derives it: request file -> `next` -> `unpack`.

    gen_loop.sh runs exactly this chain and puts whatever unpack prints into generate's
    environment. A field `next` drops is a field generate never sees, whatever generate
    itself does with FORCE_REDO.
    """
    req_dir = tmp_path / "requests"
    req_dir.mkdir()
    req_id = "20261122T100000.000000Z-abcd1234"
    (req_dir / f"{req_id}.json").write_text(
        json.dumps(
            {
                "id": req_id,
                "show": "Show",
                "generate_only_re": "",
                "require_eng": None,
                "force": force,
                "status": "pending",
            }
        )
    )
    monkeypatch.setattr(sys, "argv", ["review_requests.py", "next", str(req_dir)])
    rr.main()
    req_json = capsys.readouterr().out.strip()
    monkeypatch.setattr(sys, "argv", ["review_requests.py", "unpack", req_json])
    rr.main()
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("FORCE_REDO=")]
    assert lines, "unpack must always print FORCE_REDO so gen_loop can clear it"
    return lines[0].split("=", 1)[1]


def test_a_forced_request_reaches_generate_through_the_cli_chain(tmp_path, monkeypatch, capsys):
    """The 2026-11-22 failure, end to end: force=true -> next -> unpack -> generate.

    Every episode is at the current epoch (delivered); only force makes the run rebuild
    them. Measured 2026-11-22 on v0.4.19: the request was created with force=true and the
    run printed `todo=0` -> "nothing to transcribe", because `next` never carried the field.
    """
    _show(tmp_path, 3, delivered=True)
    force_redo = _force_redo_from_request_file(tmp_path, monkeypatch, capsys, force=True)
    assert force_redo == "1", "next/unpack must carry force through to generate"

    calls, rep = _run(tmp_path, monkeypatch, force=False, force_env=force_redo)
    assert len(calls) == 3, "a forced request must rebuild the delivered episodes"
    assert rep["forced"] is True
    assert (rep["scope"], rep["rebuilt"]) == (3, 3)
    assert rr.verdict(rep, 0) == (True, None)
    assert "forced=1 rebuilt=3" in capsys.readouterr().out


def test_an_unforced_request_through_the_chain_leaves_current_files_alone(tmp_path, monkeypatch, capsys):
    """Control: the same chain without force must still no-op on delivered episodes."""
    _show(tmp_path, 3, delivered=True)
    force_redo = _force_redo_from_request_file(tmp_path, monkeypatch, capsys, force=False)
    assert force_redo == ""

    calls, rep = _run(tmp_path, monkeypatch, force=False, force_env=force_redo)
    assert calls == []
    assert (rep["delivered"], rep["rebuilt"]) == (3, 0)
    assert "forced=0 rebuilt=0" in capsys.readouterr().out


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
    assert not ok and why is not None and "2 of 3" in why and "no-eng-dub" in why
    assert "sub-only release; no English audio track" in why


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
