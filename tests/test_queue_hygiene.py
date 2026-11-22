"""P33 hygiene sweep: one writer that expires, caps and reports every queue as a digest."""

import json
import time

from dubtitlerr.core import queue_hygiene, unresolved

DAY = 86400.0
NOW = 1_800_000_000.0


def _gloss_dir(tmp_path, n_flags, age_days=20):
    d = tmp_path / "glossaries"
    d.mkdir()
    doc = {
        "show": "Show",
        "flagged": {f"T{i}": {"reason": "unseen-needs-evidence", "first_seen": NOW - age_days * DAY} for i in range(n_flags)},
    }
    (d / "Show.json").write_text(json.dumps(doc))
    return d


def test_a_700_item_pile_drains_itself_through_the_sweep(tmp_path):
    """The card's acceptance case, end to end: over the cap, the sweep caps it and audits it."""
    gloss = _gloss_dir(tmp_path, 700, age_days=1)
    media = tmp_path / "media"
    media.mkdir()
    digest = queue_hygiene.run(str(gloss), [str(media)], now=NOW)
    assert digest["counts"]["queued"] == queue_policy_max()
    # the 500 dropped by the cap are "bounded", not "expired": two different actions, one each
    assert digest["counts"]["bounded"] == 700 - queue_policy_max()
    assert digest["counts"]["expired"] == 0
    doc = json.loads((gloss / "Show.json").read_text())
    assert len(doc["flagged"]) == queue_policy_max()
    audit = [json.loads(line) for line in open(gloss / "Show.json.review-audit.jsonl", encoding="utf-8")]
    assert len(audit) == 700 - queue_policy_max()
    assert {r["decision"] for r in audit} == {"dismiss"}
    assert {r["guard"] for r in audit} == {"queue-expiry"}


def test_a_stale_pile_expires_within_the_widow_without_needing_the_cap(tmp_path):
    gloss = _gloss_dir(tmp_path, 40, age_days=20)
    media = tmp_path / "media"
    media.mkdir()
    digest = queue_hygiene.run(str(gloss), [str(media)], now=NOW)
    assert digest["counts"]["queued"] == 0
    assert digest["counts"]["expired"] == 40


def queue_policy_max():
    from dubtitlerr.core import queue_policy

    return queue_policy.QUEUE_MAX


def test_the_digest_self_reports_the_class_it_could_not_keep(tmp_path):
    gloss = _gloss_dir(tmp_path, 260)
    media = tmp_path / "media"
    media.mkdir()
    digest = queue_hygiene.run(str(gloss), [str(media)], now=NOW)
    assert digest["report"][0]["class"] == "unseen-needs-evidence"
    assert digest["detail"]["classes"][0]["escalate"] is True
    assert digest["escalate"] is True


def test_a_dry_run_reports_without_writing(tmp_path):
    gloss = _gloss_dir(tmp_path, 5)
    media = tmp_path / "media"
    media.mkdir()
    before = (gloss / "Show.json").read_text()
    digest = queue_hygiene.run(str(gloss), [str(media)], now=NOW, dry_run=True)
    assert digest["counts"]["expired"] == 5
    assert (gloss / "Show.json").read_text() == before
    assert not (gloss / "Show.json.review-audit.jsonl").exists()


def test_an_episode_sidecar_is_expired_by_the_same_sweep(tmp_path):
    gloss = _gloss_dir(tmp_path, 0)
    media = tmp_path / "media"
    season = media / "Show" / "Season 01"
    season.mkdir(parents=True)
    stem = str(season / "Show - S01E01")
    unresolved.record(stem, "repair", "decision_unfittable", original_text="too long")
    doc = unresolved.items(stem)
    doc[0]["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 20 * DAY))
    unresolved._rewrite(stem, doc)

    digest = queue_hygiene.run(str(gloss), [str(media)], now=NOW)
    assert digest["counts"]["expired"] == 1
    assert unresolved.pending(stem) == []
    assert unresolved.audit(stem)[0]["kind"] == "expiry"


def test_an_unreadable_glossary_does_not_stop_the_sweep(tmp_path):
    gloss = _gloss_dir(tmp_path, 1, age_days=1)
    (gloss / "Broken.json").write_text("{not json")
    media = tmp_path / "media"
    media.mkdir()
    digest = queue_hygiene.run(str(gloss), [str(media)], now=NOW)
    assert digest["glossaries"]["errors"]
    assert len(json.loads((gloss / "Show.json").read_text())["flagged"]) == 1


def test_the_digest_is_one_event_and_escalates_only_exception_classes(monkeypatch, tmp_path):
    from dubtitlerr.core import notify

    sent = []

    def fake_emit(event, summary, counts=None, loop=None, detail=None):
        sent.append({"event": event, "summary": summary, "counts": counts, "detail": detail})
        return []

    monkeypatch.setattr(notify, "emit", fake_emit)
    digest = queue_hygiene.run(
        str(_gloss_dir(tmp_path, 0)),
        [str(tmp_path / "media")],
        now=NOW,
        dry_run=True,
    )
    digest["detail"]["classes"] = [
        {"class": "repair/verdict_owed", "count": 3, "escalate": True},
        {"class": "repair/rejected_guard", "count": 400, "escalate": False},
    ]
    queue_hygiene.emit(digest)
    assert [s["event"] for s in sent] == ["review.digest", "review.pending"]
    assert sent[0]["counts"] == digest["counts"]
    assert sent[1]["detail"]["classes"][0]["class"] == "repair/verdict_owed"


def test_no_exception_class_means_no_second_notification(monkeypatch, tmp_path):
    from dubtitlerr.core import notify

    sent = []
    monkeypatch.setattr(notify, "emit", lambda event, summary, **kw: sent.append(event) or [])
    digest = queue_hygiene.run(str(_gloss_dir(tmp_path, 0)), [str(tmp_path / "media")], now=NOW, dry_run=True)
    digest["detail"]["classes"] = [{"class": "repair/rejected_guard", "count": 400, "escalate": False}]
    queue_hygiene.emit(digest)
    assert sent == ["review.digest"]


def test_the_digest_event_is_declared_by_notify():
    from dubtitlerr.core import notify

    assert "review.digest" in notify.EVENTS
    assert "review.digest" in notify.EVENT_TITLES
    assert "review.digest" in notify.EVENT_COLORS
    assert notify.EVENT_VIEWS["review.digest"] == "audit"


def test_a_dry_run_counts_bounded_and_expired_separately(tmp_path):
    """A capped pile reports 'bounded', not 'expired': the digest must not merge two actions."""
    gloss = _gloss_dir(tmp_path, 0)
    media = tmp_path / "media"
    season = media / "Show" / "Season 01"
    season.mkdir(parents=True)
    stem = str(season / "Show - S01E01")
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - DAY))  # fresh: the cap acts, not the age rule
    entry = {"stage": "repair", "reason": "verdict_owed", "resolved": False, "ts": ts}
    unresolved._rewrite(stem, [dict(entry) for _ in range(unresolved.QUEUE_MAX + 5)])

    digest = queue_hygiene.run(str(gloss), [str(media)], now=NOW, dry_run=True)

    assert digest["counts"]["bounded"] == 5
    assert digest["counts"]["expired"] == 0
