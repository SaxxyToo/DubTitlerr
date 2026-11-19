"""The flagged review queue through the web API: GET lists it, POST decides it exactly as the CLI does.

The CLI is the contract: every accept/reject assertion below is checked against
``glossary_acquire.record_decision`` on the same input, so the web surface can never drift into
its own adjudication rules. GLOSSARY_DIR is always a tmp_path -- these tests never see /config.
"""

import hashlib
import json

import pytest

from dubtitlerr.core import decisions
from dubtitlerr.glossary import glossary_acquire, glossary_lock
from dubtitlerr.review import review_glossary

SHOW = "Frieren"

# One entry per shape a producer writes: an object with full evidence (acquire), a bare string
# (glossary_verify), a no-canonical no-match (tier B) and an additive candidate.
FLAGGED = {
    "Usop": {
        "reason": "respelling-needs-review",
        "canonical": "Usopp",
        "variant_count": 9,
        "canonical_count": 3,
        "score": 0.91,
        "bound": 0.72,
        "context": ["Usop, wait!", "That is Usop."],
        "source": "transcript",
        "settled_target": None,
        "episode_count": 4,
        "scope": 12,
        "admission_method": None,
    },
    "Straw": {
        "reason": "common-word",
        "canonical": "Straw Hat Pirates",
        "variant_count": 5,
        "canonical_count": 5,
        "score": 0.8,
        "bound": 0.6,
        "context": [],
        "source": "transcript",
        "scope": 12,
    },
    "Dwarfs": "low-confidence",
    "Frier": {
        "reason": "no-wiki-match",
        "canonical": "",
        "variant_count": 7,
        "canonical_count": 0,
        "bound": 0.0,
        "context": ["Frier is late."],
        "source": "transcript",
        "scope": 12,
    },
}

BASE = {
    "show": SHOW,
    "initial_prompt": "This is Frieren.",
    "names": ["Frieren"],
    "phrases": [],
    "hard_fixes": {"frierin": "Frieren"},
    "verified": ["Frieren"],
    "flagged": FLAGGED,
    "acquired": {},
    "known": [],
    "_curated": True,
}


@pytest.fixture
def gdir(tmp_path, monkeypatch):
    d = tmp_path / "glossaries"
    d.mkdir()
    monkeypatch.setattr(decisions, "GLOSSARY_DIR", str(d))
    monkeypatch.setenv("GLOSSARY_LOCK_WAIT", "0")
    return d


def _put(gdir, data=None, indent=2, newline=True):
    p = gdir / f"{SHOW}.json"
    p.write_text(json.dumps(BASE if data is None else data, indent=indent, ensure_ascii=False) + ("\n" if newline else ""))
    return p


def _version(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _disk(path):
    return json.loads(path.read_text())


def _review(path, **kw):
    body = {"show": SHOW, "op": "review", "term": "Usop", "accept": True, "expected_version": _version(path)}
    body.update(kw)
    return review_glossary.handle("POST", "/api/glossary", body)


def _post_edit(path, **kw):
    """A plain term edit, for tests that only need to prove the editor still works on a file."""
    body = {"show": SHOW, "op": "add", "key": "names", "term": "Fern", "expected_version": _version(path)}
    body.update(kw)
    return review_glossary.handle("POST", "/api/glossary", body)


# --- GET: the queue, normalised -------------------------------------------------------------


def test_get_lists_the_queue_with_a_total_and_normalises_both_entry_shapes(gdir):
    _put(gdir)
    status, payload = review_glossary.handle("GET", "/api/glossary", {"show": SHOW})

    assert status == 200
    assert payload["flagged_total"] == 4
    assert [i["term"] for i in payload["flagged"]] == ["Dwarfs", "Frier", "Straw", "Usop"]

    bare = payload["flagged"][0]
    assert bare == {
        "term": "Dwarfs",
        "reason": "low-confidence",
        "canonical": "",
        "variant_count": 0,
        "canonical_count": 0,
        "bound": 0.0,
        "context": [],
        "effect": "none",
    }


def test_get_keeps_every_evidence_key_the_producer_attached(gdir):
    _put(gdir)
    _, payload = review_glossary.handle("GET", "/api/glossary", {"show": SHOW})
    usop = next(i for i in payload["flagged"] if i["term"] == "Usop")

    assert usop["variant_count"] == 9
    assert usop["canonical_count"] == 3
    assert usop["bound"] == 0.72
    assert usop["score"] == 0.91
    assert usop["context"] == ["Usop, wait!", "That is Usop."]
    assert usop["source"] == "transcript"
    assert usop["episode_count"] == 4
    assert usop["scope"] == 12
    assert "settled_target" in usop and usop["settled_target"] is None
    assert "admission_method" in usop


def test_get_reports_what_accepting_would_do_using_the_cli_rule(gdir):
    """`effect` comes from glossary_acquire.accept_effect -- the one place the CLI and the web share."""
    _put(gdir)
    _, payload = review_glossary.handle("GET", "/api/glossary", {"show": SHOW})
    effects = {i["term"]: i["effect"] for i in payload["flagged"]}

    assert effects == {"Usop": "hard_fix", "Straw": "additive", "Dwarfs": "none", "Frier": "none"}
    for item in payload["flagged"]:
        assert item["effect"] == glossary_acquire.accept_effect(item["term"], FLAGGED[item["term"]])


def test_get_without_a_queue_is_empty_not_missing(gdir):
    _put(gdir, data={**BASE, "flagged": None})
    status, payload = review_glossary.handle("GET", "/api/glossary", {"show": SHOW})
    assert status == 200 and payload["flagged"] == [] and payload["flagged_total"] == 0


def test_an_empty_list_queue_reads_as_empty_and_stays_editable(gdir):
    """A hand-edited or older file may spell 'nothing queued' as []; that is not corruption."""
    p = _put(gdir, data={**BASE, "flagged": []})
    status, payload = review_glossary.handle("GET", "/api/glossary", {"show": SHOW})
    assert status == 200 and payload["flagged"] == [] and payload["flagged_total"] == 0
    assert _post_edit(p)[0] == 200, "the term editor still works on such a file"


def test_a_flagged_list_is_corrupt_not_silently_rewritten(gdir):
    """record_decision would coerce a wrong-shaped `flagged` to {} and drop it: refuse instead."""
    _put(gdir, data={**BASE, "flagged": [{"term": "Usop"}]})
    before = (gdir / f"{SHOW}.json").read_bytes()
    assert review_glossary.handle("GET", "/api/glossary", {"show": SHOW}) == (500, {"error": "glossary corrupt"})
    assert _review(gdir / f"{SHOW}.json") == (500, {"error": "glossary corrupt"})
    assert (gdir / f"{SHOW}.json").read_bytes() == before


# --- POST: accept / reject, through the CLI's own decision function -------------------------


def test_accept_a_respelling_writes_the_hard_fix_and_clears_the_flag(gdir):
    p = _put(gdir)
    status, payload = _review(p)

    assert status == 200, payload
    disk = _disk(p)
    assert disk["hard_fixes"]["Usop"] == "Usopp"
    assert disk["acquired"]["Usop"]["run"] == "review"
    assert "Usop" not in disk["flagged"]
    assert payload["flagged_total"] == 3 and payload["ok"] is True
    assert payload["backup"].startswith(f"{SHOW}.json.bak-")


def test_accept_an_expansion_adds_the_canonical_and_leaves_the_spoken_term_alone(gdir):
    p = _put(gdir)
    status, payload = _review(p, term="Straw")

    assert status == 200, payload
    disk = _disk(p)
    assert disk["phrases"] == ["Straw Hat Pirates"]
    assert "Straw" not in disk["hard_fixes"], "a multi-word canonical is additive, never a token rewrite"
    assert "Straw" not in disk["flagged"]


def test_reject_drops_the_term_to_known_and_records_it_so_the_sweep_never_reproposes(gdir):
    p = _put(gdir)
    status, payload = _review(p, accept=False)

    assert status == 200, payload
    disk = _disk(p)
    assert "Usop" in disk["known"]
    assert "Usop" not in disk["flagged"]
    assert "Usop" not in disk["hard_fixes"]
    # the term a human rejected is settled for the unattended sweep
    assert "usop" in glossary_acquire._settled(disk)


def test_reject_a_bare_string_entry_works_the_same_way(gdir):
    p = _put(gdir)
    assert _review(p, term="Dwarfs", accept=False)[0] == 200
    assert "Dwarfs" in _disk(p)["known"]
    assert "Dwarfs" not in _disk(p)["flagged"]


@pytest.mark.parametrize(
    "term, accept",
    [
        ("Usop", True),
        ("Usop", False),
        ("Straw", True),
        ("Straw", False),
        ("Dwarfs", True),
        ("Dwarfs", False),
        ("Frier", True),
        ("Frier", False),
    ],
)
def test_every_decision_is_byte_for_byte_the_cli_decision(gdir, term, accept):
    p = _put(gdir)
    assert _review(p, term=term, accept=accept)[0] == 200
    assert _disk(p) == glossary_acquire.record_decision(BASE, term, accept=accept)


def test_a_no_canonical_entry_answered_accept_is_known_exactly_as_the_cli_does(gdir):
    """The CLI's y on a no-canonical line calls record_decision(accept=False); parity is what matters."""
    p = _put(gdir)
    assert _review(p, term="Frier", accept=True)[0] == 200
    assert "Frier" in _disk(p)["known"]
    assert _disk(p) == glossary_acquire.record_decision(BASE, "Frier", accept=True)


def test_the_term_matches_case_insensitively(gdir):
    p = _put(gdir)
    assert _review(p, term="USOP")[0] == 200
    assert "Usop" not in _disk(p)["flagged"]


def test_a_changed_canonical_is_409_and_writes_nothing(gdir):
    """The CLI skips an answer whose proposal changed; the web refuses it so nobody approves a guess they never saw."""
    p = _put(gdir)
    before = p.read_bytes()
    status, payload = _review(p, canonical="Someone Else")

    assert status == 409 and "changed" in payload["error"]
    assert p.read_bytes() == before
    assert [f for f in gdir.iterdir() if ".bak-" in f.name] == []


def test_the_same_canonical_the_human_saw_is_accepted(gdir):
    p = _put(gdir)
    assert _review(p, canonical="Usopp")[0] == 200


def test_a_term_that_is_no_longer_flagged_is_409(gdir):
    p = _put(gdir)
    before = p.read_bytes()
    status, payload = _review(p, term="Ghost")
    assert status == 409 and "pending" in payload["error"]
    assert p.read_bytes() == before


def test_a_stale_expected_version_is_409_and_writes_nothing(gdir):
    p = _put(gdir)
    before = p.read_bytes()
    status, payload = _review(p, expected_version="0" * 64)
    assert status == 409
    assert payload == {"error": "version conflict", "version": hashlib.sha256(before).hexdigest()}
    assert p.read_bytes() == before
    assert [f for f in gdir.iterdir() if ".bak-" in f.name] == []


@pytest.mark.parametrize("accept", ["true", "false", 1, 0, None])
def test_accept_must_be_a_real_boolean(gdir, accept):
    p = _put(gdir)
    before = p.read_bytes()
    assert _review(p, accept=accept)[0] == 400
    assert p.read_bytes() == before


@pytest.mark.parametrize("term", ["", "   ", 5, None, "bad\x00name", "x" * 81])
def test_an_invalid_term_is_400(gdir, term):
    p = _put(gdir)
    before = p.read_bytes()
    assert _review(p, term=term)[0] == 400
    assert p.read_bytes() == before


def test_busy_glossary_is_503_with_no_write(gdir):
    p = _put(gdir)
    before = p.read_bytes()
    with glossary_lock.locked(str(p), "verify") as held:
        assert held is True
        status, payload = _review(p)
    assert (status, payload) == (503, {"error": "glossary busy"})
    assert p.read_bytes() == before
    assert [f for f in gdir.iterdir() if ".bak-" in f.name] == []


def test_the_write_holds_the_webui_lock_and_keeps_the_file_style(gdir):
    p = _put(gdir, indent=4, newline=False)
    assert _review(p)[0] == 200
    text = p.read_text()
    assert text.startswith('{\n    "show"') and not text.endswith("\n")


def test_machine_keys_outside_the_decision_survive(gdir):
    p = _put(gdir)
    assert _review(p)[0] == 200
    disk = _disk(p)
    assert disk["initial_prompt"] == BASE["initial_prompt"]
    assert disk["verified"] == BASE["verified"]
    assert disk["_curated"] is True
    assert disk["names"] == BASE["names"]


def test_a_review_logs_one_audit_line(gdir, capsys):
    p = _put(gdir)
    review_glossary.handle(
        "POST",
        "/api/glossary",
        {"show": SHOW, "op": "review", "term": "Usop", "accept": True, "expected_version": _version(p)},
        operator="token",
    )
    (line,) = [ln for ln in capsys.readouterr().out.splitlines() if "glossary review" in ln]
    assert "operator=token" in line and f"show={json.dumps(SHOW)}" in line
    assert "op=accept" in line and 'term="Usop"' in line
    assert "backup=" in line


def test_a_refused_review_logs_nothing(gdir, capsys):
    p = _put(gdir)
    _review(p, expected_version="0" * 64)
    assert "glossary review" not in capsys.readouterr().out


def test_a_review_is_user_level_not_admin(gdir):
    p = _put(gdir)
    body = {"show": SHOW, "op": "review", "term": "Usop", "accept": True, "expected_version": _version(p)}
    assert review_glossary.handle("POST", "/api/glossary", body, operator="u", role="user")[0] == 200


def test_get_after_a_decision_shows_the_shrunk_queue(gdir):
    p = _put(gdir)
    assert _review(p)[0] == 200
    _, payload = review_glossary.handle("GET", "/api/glossary", {"show": SHOW})
    assert payload["flagged_total"] == 3
    assert "Usop" not in [i["term"] for i in payload["flagged"]]
