"""The unresolved queue lives in the artifact store (P39-B4).

The three queue artifacts -- `unresolved`, `unresolved.counters`, `unresolved.audit` -- were
files beside the media. These tests pin the store round-trip, the reader parity with the old
file bytes, ordering, corruption tolerance, and the two invariants the module has always had:
`record` never raises, and a resolution survives a process restart. No test here looks for a
beside-media file; post-migration there is none.
"""

import json
import time

from dubtitlerr.core import artifact_store, queue_policy, unresolved

DAY = 86400.0


def _stem(tmp_path):
    return str(tmp_path / "Show" / "Season 01" / "Show - S01E01")


def _legacy_read(payload: bytes) -> list:
    """The reader this module used before P39: one JSON object per line, torn line skipped."""
    out = []
    for raw in payload.decode("utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def test_record_writes_one_unresolved_row_and_no_beside_media_file(tmp_path):
    stem = _stem(tmp_path)
    assert unresolved.record(stem, "repair", "rejected_guard", original_text="a", proposed_text="b") is True

    assert [e["proposed_text"] for e in unresolved.items(stem)] == ["b"]
    assert artifact_store.exists(stem, unresolved.KIND)
    assert not (tmp_path / "Show" / "Season 01" / "Show - S01E01.dubtitles.unresolved.jsonl").exists()


def test_record_writes_counters_alongside_the_queue(tmp_path):
    stem = _stem(tmp_path)
    unresolved.record(stem, "repair", "rejected_guard", original_text="a", proposed_text="b")

    counters = artifact_store.get_json(stem, unresolved.COUNTER_KIND)
    assert counters["counts"]["repair/rejected_guard"] == 1
    assert counters["days"]  # one day bucket holds today's count
    assert unresolved.metrics(stem)["queue"]["pending"] == 1


def test_expire_writes_the_bounded_audit_kind(tmp_path):
    stem = _stem(tmp_path)
    unresolved.record(stem, "repair", "decision_unfittable", original_text="too long")
    doc = unresolved.items(stem)
    doc[0]["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20 * DAY))
    unresolved._rewrite(stem, doc)

    assert unresolved.expire(stem)["expired"] == 1
    assert unresolved.pending(stem) == []
    assert artifact_store.exists(stem, unresolved.AUDIT_KIND)
    assert unresolved.audit(stem)[0]["kind"] == "expiry"


def test_reader_parity_store_payload_and_old_file_bytes(tmp_path):
    """The store reader and the old file reader agree on the same bytes, corrupt line included."""
    stem = _stem(tmp_path)
    payload = (
        json.dumps({"stage": "repair", "reason": "rejected_guard", "original_text": "a", "proposed_text": "b"})
        + "\n{torn final line\n"
        + json.dumps({"stage": "repair_applied", "reason": "accepted", "original_text": "c", "proposed_text": "d"})
        + "\n"
    )
    artifact_store.put_text(stem, unresolved.KIND, payload)

    assert unresolved.items(stem) == _legacy_read(payload.encode("utf-8"))


def test_append_keeps_recording_order(tmp_path):
    stem = _stem(tmp_path)
    for i in range(5):
        unresolved.record(stem, "repair", "rejected_guard", original_text=f"line {i}", proposed_text=f"fix {i}")

    assert [e["original_text"] for e in unresolved.items(stem)] == [f"line {i}" for i in range(5)]


def test_corrupt_line_is_skipped_not_fatal(tmp_path):
    stem = _stem(tmp_path)
    artifact_store.put_text(stem, unresolved.KIND, '{"stage": "repair"}\nnot json\n{"stage": "punctuation"}\n')

    assert [e["stage"] for e in unresolved.items(stem)] == ["repair", "punctuation"]


def test_corrupt_counters_read_as_a_fresh_document(tmp_path):
    stem = _stem(tmp_path)
    artifact_store.put_text(stem, unresolved.COUNTER_KIND, "{not json")

    assert unresolved.record(stem, "repair", "rejected_guard", original_text="a", proposed_text="b") is True
    assert artifact_store.get_json(stem, unresolved.COUNTER_KIND)["counts"]["repair/rejected_guard"] == 1


def test_resolution_and_counters_survive_a_process_restart(tmp_path):
    stem = _stem(tmp_path)
    unresolved.record(stem, "repair_applied", "accepted", original_text="a line", proposed_text="fix")
    assert unresolved.resolve(stem, 0, accept=True, note="checked") is True

    artifact_store.reset_connections()  # a fresh process opens a fresh handle

    (entry,) = unresolved.items(stem)
    assert entry["resolved"] is True and entry["accepted"] is True and entry["note"] == "checked"
    assert artifact_store.get_json(stem, unresolved.COUNTER_KIND)["counts"]["repair_applied/accepted"] == 1


def test_undo_expire_restores_the_entry_and_audits_it(tmp_path):
    stem = _stem(tmp_path)
    unresolved.record(stem, "repair", "decision_unfittable", original_text="too long")
    doc = unresolved.items(stem)
    doc[0]["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20 * DAY))
    unresolved._rewrite(stem, doc)
    unresolved.expire(stem)
    assert unresolved.pending(stem) == []

    assert unresolved.undo_expire(stem, 0) is True
    assert [e["original_text"] for e in unresolved.pending(stem)] == ["too long"]
    assert unresolved.audit(stem)[0]["kind"] == "undo-expiry"


def test_the_counters_keep_the_trimmed_shape(tmp_path):
    stem = _stem(tmp_path)
    old = {"counts": {"repair/rejected_guard": 4}, "days": {}, "sampled": {}, "sample_day": {}}
    for i in range(queue_policy.EXPIRY_DAYS + 5):
        old["days"][f"2026-01-{i + 1:02d}"] = {"repair/rejected_guard": 1}
        old["sample_day"][f"2026-01-{i + 1:02d}"] = {"repair/rejected_guard": 1}
    artifact_store.put_json(stem, unresolved.COUNTER_KIND, old)

    unresolved.record(stem, "repair", "rejected_guard", original_text="a", proposed_text="b")

    doc = artifact_store.get_json(stem, unresolved.COUNTER_KIND)
    assert len(doc["days"]) == queue_policy.EXPIRY_DAYS + 1
    assert doc["counts"]["repair/rejected_guard"] == 5  # lifetime totals are not trimmed


def test_record_never_raises_when_the_store_is_unavailable(tmp_path, monkeypatch):
    stem = _stem(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(artifact_store, "_connect", boom)
    assert unresolved.record(stem, "repair", "rejected_guard", original_text="a", proposed_text="b") is False
    assert unresolved.items(stem) == []
    assert unresolved.pending(stem) == []


def test_the_cli_discovers_stems_from_the_store(tmp_path, capsys):
    stem = _stem(tmp_path)
    unresolved.record(stem, "repair", "rejected_guard", original_text="a", proposed_text="b")

    unresolved.main([str(tmp_path)])

    out = capsys.readouterr().out
    assert "Show - S01E01" in out and "1 pending" in out
