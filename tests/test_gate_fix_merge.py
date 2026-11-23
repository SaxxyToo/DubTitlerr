"""The merge pass's refusal path: fix, re-gate, deliver — or park and say so.

Before this, `gate: FAIL` printed `skip mux`, recorded `refused` and nothing consumed the
failure: the episode sat unmuxed for days with no stamp and no message. These tests drive
the real `merge_pass.sh` against a synthetic episode whose sidecar the real publish gate
refuses, with the stage modules stubbed so the assertions are about the wiring (was gate_fix
run? did the re-gate pass? did mux run? was the refusal surfaced exactly once?).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_NOTIFY_STUB = """\
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["NOTIFYLOG"], "a") as f:
    f.write(json.dumps(argv) + "\\n")
if argv[:1] == ["subscribed"]:
    sys.exit(0 if os.environ.get("NOTIFY_SUBSCRIBED") == "1" else 1)
sys.exit(0)
"""

_STAGE_STUB = """\
import os, sys
sys.path.insert(0, {repo!r})
from dubtitlerr.core import artifact_store, common
common.OUTPUT_ROOT = ""
name = {name!r}
if "--delivered" in sys.argv:
    sys.exit(0 if artifact_store.exists(sys.argv[sys.argv.index("--delivered") + 1], "done") else 1)
arg = sys.argv[-1]
stem = arg
for suffix in (".dubtitles.conf.json", ".eng.dubtitles.srt", ".mkv", ".mp4"):
    if arg.endswith(suffix):
        stem = arg[: -len(suffix)]
        break
with open(os.environ["CALLLOG"], "a") as f:
    f.write(name + " " + os.path.basename(stem) + "\\n")
common.write_stage(stem, name, "ok")
if name == "mux":
    artifact_store.put_json(stem, "done", {{}})
"""

ASS_HEAD = (
    """[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, """
    """StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Dubtitles,Arial,24,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0.5,2,10,10,18,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
)

PAD = 14
SENTENCE = "I'm not going to let go of my freedom."


def _t(sec):
    return f"{int(sec // 3600)}:{int((sec % 3600) // 60):02d}:{sec % 60:05.2f}"


def _ass(cues):
    return ASS_HEAD + "\n".join(
        f"Dialogue: 0,{_t(s)},{_t(e)},Dubtitles,,0,0,0,,{t}" for s, e, t in cues) + "\n"


def _padded(body, start=100.0, end=116.94):
    cues = [(float(i), float(i) + 1.0, f"Line number {i} is spoken here.") for i in range(PAD)]
    cues.append((start, end, body))
    cues += [(end + 10 + i, end + 11 + i, f"And line {i} again.") for i in range(PAD)]
    return cues


def _words(sentence, start=100.0, step=0.31):
    return {"words": [{"text": w, "start": start + i * step, "end": start + 0.3 + i * step,
                       "prob": 0.9} for i, w in enumerate(sentence.split())]}


def _app(tmp_path):
    app = tmp_path / "app"
    if app.exists():
        return app  # the second pass over the same episode reuses the same app + library
    app.mkdir(parents=True)
    shutil.copytree(REPO / "dubtitlerr", app / "dubtitlerr")
    for name, script in (("repair", "repair.py"), ("signs", "dub_signs_merge.py"),
                         ("mux", "mux.py")):
        (app / "dubtitlerr" / "pipeline" / script).write_text(
            _STAGE_STUB.format(repo=str(REPO), name=name))
    (app / "dubtitlerr" / "core" / "notify.py").write_text(_NOTIFY_STUB)
    (app / "shell").mkdir()
    (app / "shell" / "lib.sh").write_text((REPO / "shell" / "lib.sh").read_text())
    return app


def _fake_bin(tmp_path):
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    for name in ("ffmpeg", "mkvmerge"):
        p = bindir / name
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    return bindir


def _run(tmp_path, cues, words=None, **extra_env):
    app = _app(tmp_path)
    _STORE["path"] = str(tmp_path / "config" / "artifacts.db")
    library = tmp_path / "library"
    stem = library / "Show" / "ep01"
    stem.parent.mkdir(parents=True, exist_ok=True)
    (stem.parent / "ep01.mkv").write_text("")
    (stem.parent / "ep01.eng.dubtitles.ass").write_text(_ass(cues))
    if words is not None:
        (stem.parent / "ep01.dubtitles.words.json").write_text(json.dumps(words))
    (tmp_path / "calls.log").write_text("")
    (tmp_path / "notify.log").write_text("")  # each pass reports only its own notifications
    env = dict(os.environ)
    env["PATH"] = str(_fake_bin(tmp_path)) + os.pathsep + env["PATH"]
    env.update(MERGE_ROOTS=str(library), APP_DIR=str(app), PYTHONPATH=str(app),
               CONFIG_DIR=str(tmp_path / "config"),
               ARTIFACT_STORE=str(tmp_path / "config" / "artifacts.db"), PUBLISH_GATE="1",
               # These tests supply deliberately unpresentable text and drive gate_fix:
               # P31's restoration would repair the same defects first, so isolate it.
               RESTORATION_REPASS="0",
               CALLLOG=str(tmp_path / "calls.log"), NOTIFYLOG=str(tmp_path / "notify.log"))
    env.update(extra_env)
    res = subprocess.run(["/bin/sh", str(REPO / "merge_pass.sh")], env=env,
                         cwd=str(library), capture_output=True, text=True, timeout=120)
    calls = [ln for ln in (tmp_path / "calls.log").read_text().split("\n") if ln]
    log = tmp_path / "notify.log"
    sent = []
    if log.exists():
        sent = [json.loads(ln) for ln in log.read_text().splitlines()
                if json.loads(ln)[:1] == ["send"]]
    return res, calls, sent, str(stem)


def _refusals(sent):
    return [s for s in sent if s[1:3] == ["--event", "episode.refused"]]


_STORE = {"path": None}


def _stages(stem):
    """Stage records from the child's store (its CONFIG_DIR artifacts.db)."""
    from dubtitlerr.core import artifact_store

    return artifact_store.get_json(stem, "stages", path=_STORE["path"]) or {}


def _has_done(stem):
    from dubtitlerr.core import artifact_store

    return artifact_store.exists(stem, "done", path=_STORE["path"])


def test_a_fixable_refusal_is_normalized_and_delivered(tmp_path):
    res, calls, sent, stem = _run(tmp_path, _padded(SENTENCE), words=_words(SENTENCE))
    output = res.stdout + res.stderr
    assert "gate: fail -> gate_fix normalized" in output, output
    assert calls == ["mux ep01"], (calls, output)
    assert _has_done(stem)
    assert _stages(stem)["gate_fix"]["outcome"] == "ok", _stages(stem)
    assert _refusals(sent) == [], "a fixed episode is not a refusal"


def test_the_fixed_file_is_what_got_muxed(tmp_path):
    """The re-gate runs on the file, so the card that reaches mux is a legal one."""
    res, calls, _, stem = _run(tmp_path, _padded(SENTENCE), words=_words(SENTENCE),
                              NOTIFY_SUBSCRIBED="1")
    text = open(stem + ".eng.dubtitles.ass", encoding="utf-8-sig").read()
    assert "0:01:43" in text or "0:01:42" in text, "the 16.94 s card was ended near its speech"
    assert _has_done(stem), res.stdout


def _park(stem):
    try:
        return json.load(open(stem + ".dubtitles.parked.json"))
    except (OSError, ValueError):
        return {}


def _audit_attempts(stem):
    try:
        return len(json.load(open(stem + ".dubtitles.gate-fix.json"))["attempts"])
    except (OSError, ValueError, KeyError):
        return 0


def test_an_unfixable_refusal_is_parked_surfaced_once_and_never_retried(tmp_path):
    blob = "Kek" * 149
    res, calls, sent, stem = _run(tmp_path, _padded(blob, end=101.32), NOTIFY_SUBSCRIBED="1")
    output = res.stdout + res.stderr
    assert "skip mux: publish gate failed" in output, output
    assert "single unbroken token" in output, output
    assert calls == [], "mux must not run for a refused episode"
    assert _stages(stem)["mux"]["outcome"] == "refused", _stages(stem)
    refused = _refusals(sent)
    assert len(refused) == 1, sent
    assert "parked ep01" in refused[0][refused[0].index("--summary") + 1]
    assert _park(stem)["parked"] is True, _park(stem)
    assert _audit_attempts(stem) == 1

    # Every later ordinary pass SKIPS it: no gate_fix invocation, no new audit attempt, no
    # repeated notification. Michael: "no point in it constantly trying over and over again."
    for _ in range(2):
        res2, calls2, sent2, _ = _run(tmp_path, _padded(blob, end=101.32), NOTIFY_SUBSCRIBED="1")
        assert "skip: parked ep01" in res2.stdout, res2.stdout
        assert "skip mux: publish gate failed" not in res2.stdout, res2.stdout
        assert calls2 == []
        assert _refusals(sent2) == [], sent2
        assert _audit_attempts(stem) == 1, "a parked episode must not be re-attempted"
        assert "1 episode(s) parked" in res2.stdout, res2.stdout


def test_a_changed_attempt_stamp_retries_the_parked_episode_exactly_once(tmp_path):
    """A deployed gate_fix change heals parked episodes once, without timer retries."""
    blob = "Kek" * 149
    _run(tmp_path, _padded(blob, end=101.32), NOTIFY_SUBSCRIBED="0")
    stem = str(tmp_path / "library" / "Show" / "ep01")
    doc = _park(stem)
    assert doc["attempt_stamp"]["gate_fix_epoch"] >= 1
    assert _audit_attempts(stem) == 1

    doc["attempt_stamp"]["gate_fix_epoch"] -= 1  # what an older release parked it with
    (tmp_path / "library" / "Show" / "ep01.dubtitles.parked.json").write_text(json.dumps(doc))

    res, calls, sent, _ = _run(tmp_path, _padded(blob, end=101.32), NOTIFY_SUBSCRIBED="1")
    assert "retry: parked ep01" in res.stdout, res.stdout
    assert "attempt stamp changed" in res.stdout, res.stdout
    assert calls == []
    assert _audit_attempts(stem) == 2, "the stale verdict bought exactly one new attempt"
    assert _park(stem)["attempt_stamp"] == doc["attempt_stamp"] | {
        "gate_fix_epoch": doc["attempt_stamp"]["gate_fix_epoch"] + 1}
    # Older signature: the same refusal is not re-notified.
    assert _refusals(sent) == [], sent

    # ...and the pass after that skips again.
    res2, calls2, _, _ = _run(tmp_path, _padded(blob, end=101.32), NOTIFY_SUBSCRIBED="1")
    assert "skip: parked ep01" in res2.stdout, res2.stdout
    assert _audit_attempts(stem) == 2, res2.stdout


def test_a_run_request_created_after_the_park_retries_it_once(tmp_path):
    blob = "Kek" * 149
    _run(tmp_path, _padded(blob, end=101.32), NOTIFY_SUBSCRIBED="0")
    stem = str(tmp_path / "library" / "Show" / "ep01")
    assert _audit_attempts(stem) == 1
    rdir = tmp_path / "requests"
    rdir.mkdir()
    (rdir / "req.json").write_text(json.dumps({"show": "Show"}))

    res, calls, _, _ = _run(tmp_path, _padded(blob, end=101.32), NOTIFY_SUBSCRIBED="0",
                            REQUESTS_DIR=str(rdir))
    assert "retry: parked ep01" in res.stdout, res.stdout
    assert "run request" in res.stdout, res.stdout
    assert calls == []
    assert _audit_attempts(stem) == 2, res.stdout
    assert _park(stem)["at"] > (rdir / "req.json").stat().st_mtime

    # The re-park is newer than the request: the next pass skips without another request.
    res2, _, _, _ = _run(tmp_path, _padded(blob, end=101.32), NOTIFY_SUBSCRIBED="0",
                         REQUESTS_DIR=str(rdir))
    assert "skip: parked ep01" in res2.stdout, res2.stdout
    assert _audit_attempts(stem) == 2, res2.stdout



def test_an_unusable_park_record_keeps_the_park_and_is_reported_as_an_error(tmp_path):
    """`gate_fix --parked` used to exit 1 for BOTH "not parked" and "the record is broken", so a
    park file with a hand-edited `at` was read as a stale verdict, unparked and deleted. Exit 3
    says "cannot tell": keep the record, spend no attempt, and make it visible."""
    blob = "Kek" * 149
    _run(tmp_path, _padded(blob, end=101.32), NOTIFY_SUBSCRIBED="0")
    stem = str(tmp_path / "library" / "Show" / "ep01")
    park = tmp_path / "library" / "Show" / "ep01.dubtitles.parked.json"
    doc = _park(stem)
    doc["at"] = "yesterday"
    park.write_text(json.dumps(doc))
    before = park.read_text()

    res, calls, _, _ = _run(tmp_path, _padded(blob, end=101.32), NOTIFY_SUBSCRIBED="0")
    assert "error: parked ep01" in res.stdout, res.stdout
    assert "park record has no usable 'at'" in res.stdout, res.stdout
    assert "skip mux: publish gate failed" not in res.stdout, "the gate was not re-run on it"
    assert calls == []
    assert _audit_attempts(stem) == 1, "no attempt was spent on a record it could not trust"
    assert park.read_text() == before, "the park is kept exactly as it was"
    assert "MERGE PASS INCOMPLETE" in res.stdout, "an unusable park record must be visible, not quietly parked"


def test_no_subscriber_means_no_event_but_the_park_still_happens(tmp_path):
    blob = "Kek" * 149
    res, calls, sent, stem = _run(tmp_path, _padded(blob, end=101.32), NOTIFY_SUBSCRIBED="0")
    assert "skip mux: publish gate failed" in res.stdout, res.stdout
    assert _refusals(sent) == [], sent
    assert _stages(stem)["mux"]["outcome"] == "refused"


def test_a_passing_episode_never_reaches_gate_fix(tmp_path):
    """Regression: the new step must be invisible to a healthy episode."""
    cues = [(float(i), float(i) + 1.0, f"Line number {i} is spoken here.") for i in range(8)]
    res, calls, sent, stem = _run(tmp_path, cues)
    assert calls == ["mux ep01"], (calls, res.stdout)
    assert "gate_fix" not in res.stdout, res.stdout
    assert "gate_fix" not in _stages(stem), _stages(stem)
    assert not os.path.exists(stem + ".dubtitles.gate-fix.json")


def test_gate_fix_off_restores_the_old_park_behaviour(tmp_path):
    res, calls, sent, stem = _run(tmp_path, _padded(SENTENCE), words=_words(SENTENCE),
                                  GATE_FIX="0", NOTIFY_SUBSCRIBED="1")
    assert calls == [], (calls, res.stdout)
    assert "skip mux: publish gate failed" in res.stdout
    assert _stages(stem)["mux"]["outcome"] == "refused"
    assert not os.path.exists(stem + ".dubtitles.gate-fix.json")
    # With no fixer there is no signature to dedupe on, so the park still escalates.
    assert len(_refusals(sent)) == 1, sent


def test_a_missing_gate_fix_module_stops_the_pass_before_the_loop(tmp_path):
    app = _app(tmp_path)
    (app / "dubtitlerr" / "pipeline" / "gate_fix.py").unlink()
    library = tmp_path / "library"
    (library / "Show").mkdir(parents=True)
    (library / "Show" / "ep01.mkv").write_text("")
    (library / "Show" / "ep01.eng.dubtitles.ass").write_text(_ass(_padded(SENTENCE)))
    env = dict(os.environ)
    env["PATH"] = str(_fake_bin(tmp_path)) + os.pathsep + env["PATH"]
    env.update(MERGE_ROOTS=str(library), APP_DIR=str(app), PYTHONPATH=str(app),
               CONFIG_DIR=str(tmp_path / "config"), PUBLISH_GATE="1")
    res = subprocess.run(["/bin/sh", str(REPO / "merge_pass.sh")], env=env,
                         cwd=str(library), capture_output=True, text=True, timeout=120)
    assert res.returncode != 0
    assert "gate_fix.py" in res.stdout + res.stderr
    assert "MERGE PASS COMPLETE" not in res.stdout
