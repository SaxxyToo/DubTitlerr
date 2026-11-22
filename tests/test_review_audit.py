"""P33 audit-first surface: the feed of automatic decisions and the undo that makes them safe."""

import json
import time

import pytest

from dubtitlerr.core import decisions, unresolved
from dubtitlerr.glossary import review_policy as rp
from dubtitlerr.review import review_audit

DAY = 86400.0


@pytest.fixture
def env(tmp_path, monkeypatch):
    gloss = tmp_path / "glossaries"
    gloss.mkdir()
    media = tmp_path / "media"
    (media / "Show" / "Season 01").mkdir(parents=True)
    monkeypatch.setenv("GLOSSARY_DIR", str(gloss))
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setattr(decisions, "GLOSSARY_DIR", str(gloss))
    return gloss, media


def _write(path, doc):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f)


def _audit(path, records):
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _expired_gloss(gloss, term="Term1", reason="unseen-needs-evidence"):
    path = str(gloss / "Show.json")
    doc = {"show": "Show", "flagged": {term: {"reason": reason, "first_seen": time.time() - 20 * DAY}}}
    _write(path, doc)
    rep = rp.expire_flags(doc, path)
    _write(path, doc)
    _audit(
        path + ".review-audit.jsonl",
        [rp.decision_record(term, reason, "dismiss", "queue-expiry", time.time() - 1, "Show")],
    )
    return path, doc, rep


def test_feed_merges_both_sources_newest_first(env):
    gloss, media = env
    _expired_gloss(gloss)
    stem = str(media / "Show" / "Season 01" / "Show - S01E01")
    unresolved.record(stem, "repair", "rejected_guard", original_text="line")
    doc = unresolved.items(stem)
    doc[0]["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20 * DAY))
    unresolved._rewrite(stem, doc)
    unresolved.expire(stem)

    feed = review_audit.feed()
    kinds = {d["kind"] for d in feed["decisions"]}
    assert kinds == {"glossary", "queue"}
    assert feed["decisions"] == sorted(feed["decisions"], key=lambda d: d["at"], reverse=True)
    assert feed["undoable"] == 2
    assert len({d["id"] for d in feed["decisions"]}) == 2


def test_undo_a_glossary_expiry_restores_the_flag_and_records_the_reversal(env):
    gloss, _media = env
    path, _doc, _rep = _expired_gloss(gloss)
    item = review_audit.feed()["decisions"][0]

    status, payload = review_audit.undo(item["id"])
    assert status == 200 and payload["restored"] is not None

    with open(path, encoding="utf-8") as f:
        restored = json.load(f)
    assert "Term1" in restored["flagged"]
    assert restored["flagged"]["Term1"]["first_seen"] > time.time() - 60  # a fresh window

    audit = [json.loads(line) for line in open(path + ".review-audit.jsonl", encoding="utf-8")]
    assert audit[-1]["decision"] == "restore" and audit[-1]["by"] == "operator"

    # the same id cannot be undone twice
    assert review_audit.undo(item["id"])[0] == 409


def test_undo_a_p32_auto_dismiss_restores_the_queued_flag(env):
    gloss, _media = env
    path = str(gloss / "Show.json")
    _write(path, {"show": "Show"})
    _audit(path + ".archived.jsonl", [{"at": time.time() - 100, "term": "Fern", "reason": "english-word"}])
    _audit(
        path + ".review-audit.jsonl",
        [rp.decision_record("Fern", "english-word", "dismiss", None, time.time() - 60, "Show")],
    )
    item = review_audit.feed()["decisions"][0]
    assert item["undoable"] is True and item["decision"] == "dismiss"

    status, _payload = review_audit.undo(item["id"])
    assert status == 200
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["flagged"]["Fern"]["reason"] == "english-word"


def test_undo_a_queue_expiry_restores_the_entry_and_audits_it(env):
    _gloss, media = env
    stem = str(media / "Show" / "Season 01" / "Show - S01E01")
    unresolved.record(stem, "repair", "decision_unfittable", original_text="too long")
    doc = unresolved.items(stem)
    doc[0]["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20 * DAY))
    unresolved._rewrite(stem, doc)
    unresolved.expire(stem)
    assert unresolved.pending(stem) == []

    item = review_audit.feed()["decisions"][0]
    status, _payload = review_audit.undo(item["id"])
    assert status == 200
    assert [e["original_text"] for e in unresolved.pending(stem)] == ["too long"]
    assert unresolved.audit(stem)[0]["kind"] == "undo-expiry"


def test_undo_rejects_an_unknown_or_missing_id(env):
    assert review_audit.undo("deadbeefdeadbeef")[0] == 404
    assert review_audit.undo("")[0] == 400


def test_an_operator_record_is_never_offered_for_undo(env):
    gloss, _media = env
    path = str(gloss / "Show.json")
    _write(path, {"show": "Show", "flagged": {}})
    _audit(
        path + ".review-audit.jsonl",
        [rp.decision_record("Term", "english-word", "dismiss", None, time.time(), "Show", by="operator")],
    )
    item = review_audit.feed()["decisions"][0]
    assert item["undoable"] is False
    assert review_audit.undo(item["id"])[0] == 409


def test_queues_self_reports_a_pile_by_class(env):
    gloss, _media = env
    _write(
        gloss / "Show.json",
        {"show": "Show", "flagged": {f"T{i}": {"reason": "unseen-needs-evidence"} for i in range(60)}},
    )
    q = review_audit.queues()
    assert q["glossary"]["queued"] == 60
    assert q["glossary"]["report"][0]["class"] == "unseen-needs-evidence"
    assert q["glossary"]["report"][0]["proposal"]


def test_feed_and_queues_fail_open_on_an_empty_config(env):
    assert review_audit.feed()["decisions"] == []
    assert review_audit.queues()["glossary"]["queued"] == 0


# --- the routes the workspace actually calls ----------------------------------------------


def test_the_audit_routes_are_registered_for_a_reviewer():
    from dubtitlerr.review import review_auth

    assert review_auth.ROUTE_ROLES[("GET", "/api/audit")] == "user"
    assert review_auth.ROUTE_ROLES[("POST", "/api/audit/undo")] == "user"
    assert review_auth.ROUTE_ROLES[("GET", "/api/queues")] == "user"


def test_route_serves_the_feed_and_the_undo_is_real(env, monkeypatch):
    from dubtitlerr.review import review_server as rs

    _gloss, media = env
    monkeypatch.setenv("REVIEW_AUTH", "off")
    monkeypatch.setattr(rs, "ROOTS", [str(media)])
    monkeypatch.setattr(rs, "_STEMS_CACHE", None)
    monkeypatch.setattr(rs, "_QUEUE_CACHE", {})
    path, _doc, _rep = _expired_gloss(_gloss)

    status, payload = rs.route("GET", "/api/audit", {}, None)
    assert status == 200
    item = payload["decisions"][0]
    assert item["undoable"] is True

    status, payload = rs.route("POST", "/api/audit/undo", {"id": item["id"]}, None, headers={"X-DubTitlerr-Request": "1"})
    assert status == 200 and payload["ok"] is True
    with open(path, encoding="utf-8") as f:
        assert "Term1" in json.load(f)["flagged"]

    status, payload = rs.route("GET", "/api/queues", {}, None)
    assert status == 200 and payload["glossary"]["queued"] == 1
