"""P39-B5 parity: marker kinds decide the same from a store row as from the old file.

Before P39 each marker was a file beside the media: ``.dubtitles.fail`` stopped generate,
``.dubtitles.no-speech`` retired an episode, ``.dubtitles.crash.json`` gave the triage listing
its detail, ``.dubtitles.parked.json`` blocked a gate_fix re-attempt, and
``.dubtitles.mux-recovery`` made mux refuse an episode. Those files no longer exist; these
tests seed the equivalent store row and pin the SAME decision, plus the fact that nothing is
written beside the media.
"""

from __future__ import annotations

import json
import os

import store_seed

from dubtitlerr.core import artifact_store
from dubtitlerr.pipeline import gate_fix, generate, mux
from dubtitlerr.review import review_requests as rr


def _episode(tmp_path):
    d = tmp_path / "Show"
    d.mkdir()
    v = d / "ep01.mkv"
    v.write_bytes(b"video")
    return v, str(d / "ep01")


def test_a_fail_row_blocks_generate_and_names_the_episode_in_the_scope(tmp_path, monkeypatch):
    v, stem = _episode(tmp_path)
    artifact_store.put(stem, "fail", "")

    assert generate.process(str(v)) == "skip-prior-crash"
    rep = generate.scope_report([str(v)], since=0, statuses={})
    assert rep["not_done"] == {"prior crash (fail marker)": 1}
    assert rep["crash_marked"] == [str(v)]
    # The marker is a row, not a file: a fresh run never recreates the old sidecar.
    assert not os.path.exists(stem + ".dubtitles.fail")


def test_crash_marked_reads_the_row_and_clear_crash_deletes_it(tmp_path):
    v, stem = _episode(tmp_path)
    artifact_store.put(stem, "fail", "")
    artifact_store.put_json(stem, "crash", {"exc_type": "RuntimeError", "msg": "CUDA OOM"})

    [entry] = rr.crash_marked(str(tmp_path))
    assert entry["stem"] == stem
    assert entry["crash"] == {"exc_type": "RuntimeError", "msg": "CUDA OOM"}
    assert "clear-crash" in entry["clear"]

    assert rr.clear_crash(str(v), str(tmp_path)) == stem
    assert not artifact_store.exists(stem, "fail")
    assert artifact_store.exists(stem, "crash"), "history survives the blocker"


def test_a_no_speech_row_retires_the_episode_and_shows_in_the_scope(tmp_path, monkeypatch):
    v, stem = _episode(tmp_path)
    artifact_store.put_json(stem, "no-speech", {"attempts": 3, "exc_type": "EndpointAsrError"})

    assert generate.process(str(v)) == "no-speech"
    rep = generate.scope_report([str(v)], since=0, statuses={})
    assert rep["not_done"] == {"endpoint gave up (no-speech marker)": 1}
    assert not os.path.exists(stem + ".dubtitles.no-speech")


def test_a_parked_row_still_blocks_the_re_attempt_with_the_same_evidence(tmp_path):
    _v, stem = _episode(tmp_path)
    doc = {
        "parked": True,
        "at": 1.0,
        "outcome": gate_fix.OUTCOME_UNFIXABLE,
        "summary": "single unbroken token",
        "fails": ["single unbroken token"],
        "attempt_stamp": gate_fix.attempt_stamp(stem),
    }
    artifact_store.put_text(stem, "parked", json.dumps(doc))

    parked, note = gate_fix.parked_state(stem)
    assert parked is True, note
    assert gate_fix.read_park(stem)["fails"] == ["single unbroken token"]
    assert not os.path.exists(stem + ".dubtitles.parked.json")

    gate_fix.clear_park(stem)
    assert not artifact_store.exists(stem, "parked")


def test_a_mux_recovery_row_makes_mux_refuse_the_episode(tmp_path, monkeypatch):
    v, stem = _episode(tmp_path)
    (tmp_path / "Show" / "ep01.eng.dubtitles.ass").write_text("subs")
    artifact_store.put_json(stem, "mux-recovery", {"kept": str(tmp_path / "ep.muxtmp.mkv.recovered")})

    assert mux.process(str(v), apply=False) == "recovery-pending"
    assert artifact_store.exists(stem, "mux-recovery")
    assert not os.path.exists(stem + ".dubtitles.mux-recovery")

    # A human resolving it deletes the row; then mux treats the episode normally again.
    artifact_store.delete(stem, "mux-recovery")
    assert not artifact_store.exists(stem, "mux-recovery")


def test_store_seed_reaches_the_same_marker_rows_by_old_suffix(tmp_path):
    """The seeding helper keys the same rows a pipeline writer would, by the old file path."""
    v, stem = _episode(tmp_path)
    store_seed.seed(stem + ".dubtitles.fail", "fail", "")
    store_seed.seed(stem + ".dubtitles.crash.json", "crash", {"exc_type": "X"})
    assert store_seed.has(stem + ".dubtitles.fail", "fail")
    assert store_seed.load(stem + ".dubtitles.crash.json", "crash") == {"exc_type": "X"}
    assert generate.process(str(v)) == "skip-prior-crash"
