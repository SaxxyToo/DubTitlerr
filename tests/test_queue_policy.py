"""P33 queue hygiene: the app owns its backlog -- bounds, 14-day expiry, class self-report, bounded audit."""

import json
import time

from dubtitlerr.core import queue_policy as qp


def _iso(days_ago: float, now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days_ago * 86400))


NOW = 1_800_000_000.0


def _entry(stage="repair", reason="rejected_guard", days_ago=0.0, **fields):
    e = {"stage": stage, "reason": reason, "ts": _iso(days_ago, NOW), "resolved": False}
    e.update(fields)
    return e


# --- expiry -------------------------------------------------------------------------------


def test_expiry_dismisses_an_item_older_than_the_window_and_keeps_the_evidence():
    fresh = _entry(days_ago=1, original_text="fresh")
    stale = _entry(days_ago=15, original_text="stale")
    kept, expired = qp.expire([fresh, stale], now=NOW)
    assert [e["original_text"] for e in kept] == ["fresh"]
    assert [e["original_text"] for e in expired] == ["stale"]
    # reversible: the record keeps every field it had, plus what happened to it
    assert expired[0]["expired"] is True
    assert expired[0]["expired_at"] == _iso(0, NOW)
    assert expired[0]["expired_days"] == 15
    assert expired[0]["original_text"] == "stale"


def test_expiry_leaves_a_resolved_or_already_expired_item_alone():
    done = _entry(days_ago=30, resolved=True)
    gone = _entry(days_ago=30, expired=True)
    kept, expired = qp.expire([done, gone], now=NOW)
    assert len(kept) == 2 and expired == []


def test_an_item_with_no_readable_time_is_stamped_and_kept_never_dropped():
    """A queue entry is evidence. An unknown age must cost one sweep, not the entry."""
    e = {"stage": "repair", "reason": "rejected_guard", "resolved": False}
    kept, expired = qp.expire([e], now=NOW)
    assert expired == [] and kept[0]["ts"] == _iso(0, NOW)


def test_expiry_accepts_an_epoch_timestamp_too():
    e = _entry()
    e["ts"] = NOW - 20 * 86400
    _kept, expired = qp.expire([e], now=NOW)
    assert len(expired) == 1


def test_a_700_item_pile_drains_itself_to_the_bound_and_the_rest_expire_later():
    """The acceptance case: a pile can never again sit at 700."""
    pile = [_entry(days_ago=i / 24, original_text=f"line {i}") for i in range(700)]
    kept, dropped = qp.bound(pile, now=NOW)
    assert len(kept) == qp.QUEUE_MAX
    assert len(dropped) == 700 - qp.QUEUE_MAX
    # the oldest went first (file order, so the last lines), all reversibly audited
    assert dropped[-1]["original_text"] == "line 699"
    assert {e["original_text"] for e in dropped} == {f"line {i}" for i in range(200, 700)}
    assert all(e["expired"] for e in dropped)
    # and the remainder still expires on its own 14-day clock
    kept2, later = qp.expire(kept, now=NOW + 15 * 86400)
    assert kept2 == [] and len(later) == qp.QUEUE_MAX


def test_unexpire_restores_the_prior_state():
    stale = _entry(days_ago=20, original_text="misjudged")
    _kept, expired = qp.expire([stale], now=NOW)
    assert qp.unexpire(expired[0]) is True
    for key in ("expired", "expired_at", "expired_days"):
        assert key not in expired[0]


# --- self-report --------------------------------------------------------------------------


def test_a_class_over_the_alert_threshold_self_reports_with_a_proposal():
    counts = {"repair/no_reference": 6601, "repair/rejected_guard": 3}
    report = qp.report(counts)
    assert [r["class"] for r in report] == ["repair/no_reference"]
    assert report[0]["count"] == 6601
    assert report[0]["proposal"]  # a real resolution proposal, not just the number


def test_counts_only_include_open_items_by_class():
    items = [
        _entry(reason="no_reference"),
        _entry(reason="no_reference"),
        _entry(reason="rejected_guard"),
        _entry(reason="no_reference", resolved=True),
    ]
    assert qp.counts(items) == {"repair/no_reference": 2, "repair/rejected_guard": 1}


# --- bounded audit ------------------------------------------------------------------------


def test_audit_is_a_bounded_ring_not_a_growing_log(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    for i in range(30):
        qp.audit_append(path, [{"at": i, "kind": "expiry", "n": i}], max_records=10)
    lines = [json.loads(line) for line in open(path, encoding="utf-8")]
    assert len(lines) == 10
    assert lines[-1]["n"] == 29  # newest kept, oldest dropped
    assert qp.audit_read(path)[0]["n"] == 29  # newest first


def test_audit_append_never_raises_on_an_unwritable_path(tmp_path):
    assert qp.audit_append(str(tmp_path / "nope" / "audit.jsonl"), [{"at": 1}]) is False


# --- digest -------------------------------------------------------------------------------


def test_digest_is_one_summary_with_exception_classes_called_out():
    d = qp.digest(expired=12, queued=4, by_class={"repair/verdict_owed": 2, "repair/rejected_guard": 2})
    assert "expired" in d["summary"]
    assert d["counts"] == {"expired": 12, "queued": 4, "bounded": 0}
    assert [c["class"] for c in d["detail"]["classes"]] == ["repair/verdict_owed"]
    assert d["detail"]["classes"][0]["escalate"] is True
