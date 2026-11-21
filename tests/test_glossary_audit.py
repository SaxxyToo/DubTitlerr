"""Names-hygiene audit (P22 Part B): reports junk names, never fixes them.

The fixture bytes are the LIVE Frieren glossary's post-cleanup evidence, as recorded on the P22 card: the junk set
(two waves), the flag reasons each entry carried, the 8 protected ordinary-word names WITH exact wiki pages, and the
single wiki title "Continental Magic Association" the fragment names came from. No test here depends on the host word
list: the ordinary-word set is pinned explicitly, as test_common_word_guard does.
"""

import json
import os

import pytest

from dubtitlerr.glossary import glossary
from dubtitlerr.glossary import glossary_audit as ga
from dubtitlerr.glossary import glossary_verify as gv

ORDINARY = {
    "continental",
    "association",
    "magic",
    "birds",
    "blade",
    "creates",
    "kills",
    "northern",
    "imperial",
    "aura",
    "aureole",
    "fern",
    "hero",
    "land",
    "monk",
    "sense",
    "stark",
    "mana",
    "mage",
    "demon",
}


@pytest.fixture(autouse=True)
def pinned_wordlist(monkeypatch):
    monkeypatch.setattr(glossary, "_ORDINARY", set(ORDINARY))
    monkeypatch.setattr(glossary, "_ORDINARY_STATUS", "ok")


# The live file's names after both cleanup waves, plus the junk that was removed (so the audit has something to find),
# plus the protected ordinary words and the lowercase prompt vocabulary.
NAMES = [
    "Frieren",
    "Fern",
    "Stark",
    "Himmel",
    "Heiter",
    "Eisen",
    "Flamme",
    "Serie",
    "Aura",
    "Sein",
    "Ubel",
    "Denken",
    "Wirbel",
    "Methode",
    "mana",
    "mage",
    "demon",
    # wave 1/2 junk that the audit must still catch if a file still carries it
    "Continental",
    "Association",
    "Magic",
    "Birds",
    "Blade",
    "Creates",
    "Kills",
    "Northern",
    "Imperial",
    "Aureole",
    "Hero",
    "Land",
    "Monk",
    "Sense",
]

FLAGGED = {
    # Continental was NOT flagged in the live file -- exactly why wave 1's flag-only rule missed it.
    "Association": {"reason": "english-word"},
    "Magic": {"reason": "english-word"},
    "Northern": {"reason": "below-floor"},
    "Imperial": {"reason": "sentence-initial-only"},
    "Birds": {"reason": "respelling-needs-review", "canonical": "Iris"},
    "Blade": {"reason": "respelling-needs-review", "canonical": "Land"},
    "Creates": {"reason": "respelling-needs-review", "canonical": "Clematis"},
    "Berry": {"reason": "english-word"},  # flagged but NOT in names: nothing to report
    "Freeran": {"reason": "unseen-needs-evidence"},  # a real review item, never junk
}

# The wiki cache: 568 titles in the live file; the ones that matter here are the exact pages that protect ordinary
# words, and the one title whose WORDS produced the fragment names.
TITLES = [
    "Frieren",
    "Fern",
    "Stark",
    "Himmel",
    "Heiter",
    "Eisen",
    "Flamme",
    "Serie",
    "Aura",
    "Sein",
    "Ubel",
    "Denken",
    "Wirbel",
    "Methode",
    "Aureole",
    "Hero",
    "Land",
    "Monk",
    "Sense",
    "Continental Magic Association",
    "Frieren: Beyond Journey's End",
]


def live_gloss():
    return {
        "show": "Frieren: Beyond Journey's End",
        "names": list(NAMES),
        "phrases": [],
        "flagged": dict(FLAGGED),
        "wiki": "https://frieren-beyond-journeys-end.fandom.com/api.php",
    }


def report():
    return ga.audit_gloss(live_gloss(), TITLES)


def by_name(rep):
    return {e["name"]: e for e in rep["junk"]}


# ---- the two detector classes ------------------------------------------------


def test_flag_cross_ref_reports_a_junk_classified_name():
    junk = by_name(report())
    assert junk["Association"]["classes"] == ["flagged", "ordinary-word"]
    assert junk["Association"]["flag"] == "english-word"
    assert junk["Northern"]["flag"] == "below-floor"
    assert junk["Imperial"]["flag"] == "sentence-initial-only"


def test_wave_two_ordinary_word_with_no_flag_is_still_reported():
    """The class wave 1's rule missed: no flag at all, but a capitalised ordinary word with no exact wiki title."""
    junk = by_name(report())
    assert junk["Continental"]["flag"] is None
    assert junk["Continental"]["classes"] == ["ordinary-word"]
    assert junk["Kills"]["classes"] == ["ordinary-word"]


def test_a_title_fragment_carries_the_title_it_came_from_as_evidence():
    entry = by_name(report())["Continental"]
    assert entry["near_titles"] == ["Continental Magic Association"]


def test_a_respelling_flag_is_never_reported_as_the_flagged_class():
    """'Birds'/'Blade'/'Creates' are respelling candidates -- a real review item, not junk. They still surface through
    the ordinary-word class (which is why wave 2 removed the NAME and kept the flag)."""
    junk = by_name(report())
    for name, canonical in (("Birds", "Iris"), ("Blade", "Land"), ("Creates", "Clematis")):
        assert junk[name]["classes"] == ["ordinary-word"]
        assert junk[name]["flag"] is None
        assert FLAGGED[name]["canonical"] == canonical  # the flag itself is untouched by the audit
    assert "Freeran" not in junk


# ---- protection: an exact wiki title is the exemption ------------------------


@pytest.mark.parametrize("name", ["Fern", "Aura", "Aureole", "Hero", "Land", "Monk", "Sense", "Stark"])
def test_an_ordinary_word_with_an_exact_wiki_page_is_not_reported(name):
    assert name not in by_name(report())


def test_lowercase_prompt_vocabulary_is_not_reported():
    junk = by_name(report())
    for term in ("mana", "mage", "demon"):
        assert term not in junk


def test_a_flagged_name_missing_from_names_is_not_reported():
    assert "Berry" not in by_name(report())


def test_the_legacy_bare_string_flag_form_is_read():
    """Older files hold `"Kills": "english-word"` rather than a record; the audit must read both."""
    rep = ga.audit_gloss({"show": "X", "names": ["Kills"], "flagged": {"Kills": "english-word"}}, TITLES)
    assert by_name(rep)["Kills"]["flag"] == "english-word"


def test_a_clean_glossary_reports_nothing():
    rep = ga.audit_gloss({"show": "Frieren", "names": ["Frieren", "Fern"], "flagged": {}}, TITLES)
    assert rep["junk"] == [] and rep["junk_count"] == 0


# ---- unknown wiki: downgrade, never guess ------------------------------------


def test_no_wiki_titles_lists_entries_as_unevaluated_instead_of_junk():
    rep = ga.audit_gloss(live_gloss(), [])
    assert rep["junk"] == [] and rep["junk_count"] == 0
    assert {e["name"] for e in rep["unevaluated"]} >= {"Continental", "Association"}
    assert all(e["why"] == "no cached wiki titles" for e in rep["unevaluated"])
    assert "no cached wiki titles" in rep["note"]


# ---- file path + cache -------------------------------------------------------


def write_gloss(tmp_path, **extra):
    path = tmp_path / "Frieren Beyond Journey's End (2023) {tvdbid-424536}.json"
    path.write_text(json.dumps({**live_gloss(), **extra}))
    return path


def test_audit_reads_the_wiki_cache_by_show_key(tmp_path, monkeypatch):
    monkeypatch.setattr(gv, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "Frieren_Beyond_Journey_s_End.json").write_text(
        json.dumps({"api": "https://frieren.fandom.com/api.php", "titles": TITLES})
    )
    path = write_gloss(tmp_path)
    before = path.read_bytes()
    rep = ga.audit(str(path))
    assert rep["junk_count"] >= 1
    assert rep["titles"] == len(TITLES)
    assert rep["wiki"] == "https://frieren-beyond-journeys-end.fandom.com/api.php"
    assert path.read_bytes() == before  # read-only: the glossary is untouched


def test_audit_without_a_cache_downgrades(tmp_path, monkeypatch):
    monkeypatch.setattr(gv, "CACHE_DIR", str(tmp_path / "no-cache"))
    rep = ga.audit(str(write_gloss(tmp_path)))
    assert rep["junk"] == [] and rep["unevaluated"]
    assert "no cached wiki titles" in rep["note"]


def test_audit_reports_a_load_failure_without_raising(tmp_path):
    rep = ga.audit(str(tmp_path / "missing.json"))
    assert rep["note"].startswith("load-failed")


# ---- CLI ---------------------------------------------------------------------


def run_cli(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["glossary_audit", *argv])
    with pytest.raises(SystemExit) as exc:
        ga.main()
    return exc.value.code


def test_cli_exits_1_when_junk_is_reported(tmp_path, monkeypatch, capsys):
    titles = tmp_path / "titles.json"
    titles.write_text(json.dumps(TITLES))
    code = run_cli(monkeypatch, [str(write_gloss(tmp_path)), "--titles", str(titles)])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["junk_count"] >= 1


def test_cli_exits_0_on_a_clean_glossary(tmp_path, monkeypatch, capsys):
    titles = tmp_path / "titles.json"
    titles.write_text(json.dumps(TITLES))
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps({"show": "Frieren", "names": ["Frieren", "Fern"], "flagged": {}}))
    assert run_cli(monkeypatch, [str(clean), "--titles", str(titles)]) == 0
    assert json.loads(capsys.readouterr().out)["junk_count"] == 0


def test_cli_exits_2_on_an_unreadable_glossary(tmp_path, monkeypatch):
    assert run_cli(monkeypatch, [str(tmp_path / "missing.json")]) == 2


def test_the_nightly_sweep_runs_the_audit():
    """Part B is wired into the sweep, not just available: prep_show audits each show right after VERIFY."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "gen_loop.sh"), encoding="utf-8").read()
    assert "#### GLOSSARY AUDIT" in text
    assert "dubtitlerr.glossary.glossary_audit" in text
    assert text.index("#### VERIFY") < text.index("#### GLOSSARY AUDIT")
