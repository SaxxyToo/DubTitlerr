"""P32 review policy: reason tiers, auto-resolution, durable verdicts, audit and backfill.

The live baseline this targets (2026-11-21): 1,877 flagged entries, 1,542 of them english-word.
Every test here encodes one tier, one guardrail, or one persistence claim from the card.
"""

import json
import os

import pytest

from dubtitlerr.glossary import glossary_acquire as ga
from dubtitlerr.glossary import review_policy as rp

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "glossaries", "p32_live_pile.json")
TITLES = ["Fern", "Stark", "Frieren", "Aura", "Hero", "Land", "Serie"]


def base(**extra):
    g = {"show": "S", "names": [], "phrases": [], "hard_fixes": {}, "verified": []}
    g.update(extra)
    return g


# ---- tier 1: never queued, but only behind the guardrail ----------------------


def test_tier1_reasons_are_dismissed_and_counted_not_queued(tmp_path):
    g = base(
        flagged={
            "Magic": {"reason": "english-word"},
            "Northern": "below-floor",
            "Imperial": {"reason": "sentence-initial-only"},
        }
    )

    rep = rp.apply_policy(g, str(tmp_path / "g.json"), titles=TITLES)

    assert "flagged" not in g
    assert g["archived_flags"]["counts"] == {"below-floor": 1, "english-word": 1, "sentence-initial-only": 1}
    assert g["archived_flags"]["total"] == 3
    assert rep["tiers"]["dismiss"]["dismissed"] == 3
    assert rep["tiers"]["dismiss"]["protected"] == 0
    assert rep["tiers"]["queue"]["queued"] == 0


def test_an_english_word_that_is_a_wiki_canonical_is_protected_and_queued(tmp_path):
    """The Fern/Stark guardrail: a name the wiki has a page for is never auto-dismissed."""
    g = base(flagged={"Fern": {"reason": "english-word"}, "Aura": {"reason": "english-word"}})

    rep = rp.apply_policy(g, str(tmp_path / "g.json"), titles=TITLES)

    assert set(g["flagged"]) == {"Fern", "Aura"}
    assert rep["tiers"]["dismiss"]["protected"] == 2
    assert {q["term"]: q["why"] for q in rep["queued"]} == {"Fern": "wiki-title", "Aura": "wiki-title"}


def test_an_english_word_on_the_known_cast_or_a_hard_fix_is_protected(tmp_path):
    g = base(
        names=["Brook"],
        hard_fixes={"law": "Law"},
        flagged={
            "Brook": {"reason": "english-word"},
            "law": {"reason": "below-floor"},
            "Robin": {"reason": "sentence-initial-only"},
        },
        known=["Robin"],
    )

    rep = rp.apply_policy(g, str(tmp_path / "g.json"), titles=TITLES)

    assert set(g["flagged"]) == {"Brook", "law", "Robin"}
    assert rep["tiers"]["dismiss"]["protected"] == 3


def test_without_wiki_titles_a_guard_verdict_is_not_dismissed(tmp_path):
    """Fail-closed: the exact-title exemption cannot be applied, so uncertainty queues."""
    g = base(flagged={"Magic": {"reason": "english-word"}})

    rep = rp.apply_policy(g, str(tmp_path / "g.json"), titles=None)

    assert "Magic" in g["flagged"]
    assert rep["queued"][0]["why"] == "no-wiki-cache"


# ---- tier 2: apply the glossary canonical when corroborated -------------------


def test_a_respelling_corroborated_by_the_glossary_is_applied_as_a_hard_fix(tmp_path):
    g = base(names=["Zorro"], flagged={"Zoro": {"reason": "respelling-needs-review", "canonical": "Zorro"}})

    rep = rp.apply_policy(g, str(tmp_path / "g.json"), titles=["Zorro"])

    assert g["hard_fixes"] == {"Zoro": "Zorro"}
    assert g["acquired"]["Zoro"]["run"] == f"auto:{rp.POLICY_VERSION}"
    assert g["acquired"]["Zoro"]["reason"] == "auto-approved"
    assert "Zoro" not in g.get("flagged", {})
    assert g["review_rulings"]["zoro"]["decision"] == "apply"
    assert rep["tiers"]["apply"]["applied"] == 1


def test_an_uncorroborated_respelling_is_surfaced_not_applied(tmp_path):
    """A model-high respelling the glossary has never used is not trusted (the measured error class)."""
    g = base(names=[], flagged={"Raftel": {"reason": "respelling-needs-review", "canonical": "Ratel"}})

    rep = rp.apply_policy(g, str(tmp_path / "g.json"), titles=["Ratel"])

    assert "Raftel" in g["flagged"]
    assert "hard_fixes" not in g or g["hard_fixes"] == {}
    assert rep["queued"][0]["why"] == "uncorroborated-canonical"


def test_a_respelling_that_conflicts_with_a_curated_fix_is_surfaced(tmp_path):
    g = base(
        hard_fixes={"Zolo": "Zoro"},
        acquired={"Zolo": {"canonical": "Zoro", "run": "review"}},
        flagged={"Zolo": {"reason": "respelling-needs-review", "canonical": "Zorro"}},
    )

    rep = rp.apply_policy(g, str(tmp_path / "g.json"), titles=["Zorro", "Zoro"])

    assert g["hard_fixes"] == {"Zolo": "Zoro"}
    assert "Zolo" in g["flagged"]
    assert rep["queued"][0]["why"] == "curated-conflict"


def test_a_possessive_crossing_with_no_canonical_is_surfaced(tmp_path):
    g = base(flagged={"Vegapunk": {"reason": "possessive_floor_crossing", "bare": 1, "possessive": 4}})

    rep = rp.apply_policy(g, str(tmp_path / "g.json"), titles=TITLES)

    assert "Vegapunk" in g["flagged"]
    assert rep["queued"][0]["why"] == "no-canonical"


def test_a_possessive_variant_already_on_the_roster_is_dismissed(tmp_path):
    g = base(names=["Vegapunk"], flagged={"vegapunK": {"reason": "possessive_floor_crossing", "bare": 1}})

    rep = rp.apply_policy(g, str(tmp_path / "g.json"), titles=TITLES)

    assert "vegapunK" not in g.get("flagged", {})
    assert g["review_rulings"]["vegapunk"]["decision"] == "dismiss"
    assert rep["tiers"]["dismiss"]["dismissed"] == 1


# ---- tier 3 and unknown reasons: the true queue -------------------------------


@pytest.mark.parametrize(
    "reason",
    ["unseen-needs-evidence", "conflicts-with-curated-fix", "no-wiki-match", "transcript-new-term", "something-new"],
)
def test_the_true_queue_reasons_are_never_auto_resolved(reason, tmp_path):
    g = base(flagged={"Freeran": {"reason": reason, "canonical": "Frieren"}})

    rep = rp.apply_policy(g, str(tmp_path / "g.json"), titles=TITLES)

    assert g["flagged"]["Freeran"]["reason"] == reason
    assert rep["tiers"]["queue"]["queued"] == 1
    assert rep["archived"] == 0


def test_the_collapse_classes_stay_collapsed_without_a_ruling(tmp_path):
    """P24 behaviour is preserved: no-match/common-word are counts, not rulings and not queue entries."""
    g = base(flagged={"Nobody": "no-match", "Word": {"reason": "common-word", "canonical": "Word Long"}})

    rep = rp.apply_policy(g, str(tmp_path / "g.json"), titles=TITLES)

    assert "flagged" not in g
    assert g["archived_flags"]["counts"] == {"common-word": 1, "no-match": 1}
    assert "review_rulings" not in g
    assert rep["tiers"]["collapsed"] == 2


# ---- verdict persistence ------------------------------------------------------


def test_a_dismissed_variant_cannot_requeue_on_the_next_run(tmp_path):
    first = base(flagged={"Magic": {"reason": "english-word"}})
    rp.apply_policy(first, str(tmp_path / "g.json"), titles=TITLES)
    rulings = first["review_rulings"]

    fresh = base(flagged={"Magic": {"reason": "english-word"}}, review_rulings=rulings)
    rep = rp.apply_policy(fresh, str(tmp_path / "g.json"), titles=TITLES)

    assert "Magic" not in fresh.get("flagged", {})
    assert rep["tiers"]["ruled"] == 1
    assert rep["archived"] == 0


def test_an_operator_reject_writes_a_durable_ruling(tmp_path):
    g = base(flagged={"Smokey": {"reason": "unseen-needs-evidence", "canonical": "Smoker"}})

    out = ga.record_decision(g, "Smokey", accept=False)

    assert out["known"] == ["Smokey"]
    assert out["review_rulings"]["smokey"]["decision"] == "dismiss"
    assert out["review_rulings"]["smokey"]["by"] == "operator"

    fresh = base(flagged={"Smokey": {"reason": "unseen-needs-evidence"}}, review_rulings=out["review_rulings"])
    rp.apply_policy(fresh, str(tmp_path / "g.json"), titles=TITLES)
    assert "Smokey" not in fresh.get("flagged", {})


def test_an_operator_accept_writes_a_durable_apply_ruling():
    g = base(flagged={"Deccan": {"reason": "share-too-close", "canonical": "Decken"}})

    out = ga.record_decision(g, "Deccan", accept=True)

    assert out["review_rulings"]["deccan"]["decision"] == "apply"
    assert out["review_rulings"]["deccan"]["canonical"] == "Decken"
    assert out["review_rulings"]["deccan"]["by"] == "operator"


# ---- audit and undo -----------------------------------------------------------


def test_every_auto_decision_is_an_audit_record_with_the_p33_shape(tmp_path):
    path = str(tmp_path / "g.json")
    g = base(
        names=["Zorro"],
        flagged={
            "Magic": {"reason": "english-word"},
            "Zoro": {"reason": "respelling-needs-review", "canonical": "Zorro"},
        },
    )

    rep = rp.apply_policy(g, path, titles=["Zorro"])

    lines = [json.loads(ln) for ln in open(path + ".review-audit.jsonl", encoding="utf-8").read().splitlines()]
    assert [rec["decision"] for rec in lines] == ["dismiss", "apply"]
    rec = lines[1]
    assert set(rec) == {"at", "term", "reason", "decision", "tier", "canonical", "applied", "guard", "by", "policy", "show"}
    assert rec["term"] == "Zoro" and rec["canonical"] == "Zorro" and rec["applied"] == "hard_fix"
    assert rec["by"] == "auto" and rec["policy"] == rp.POLICY_VERSION
    assert rep["decisions"] == lines


def test_an_auto_apply_is_undoable(tmp_path):
    g = base(names=["Zorro"], flagged={"Zoro": {"reason": "respelling-needs-review", "canonical": "Zorro"}})
    rp.apply_policy(g, str(tmp_path / "g.json"), titles=["Zorro"])

    out = rp.undo(g, "Zoro")

    assert "Zoro" not in out.get("hard_fixes", {})
    assert "Zoro" not in out.get("acquired", {})
    assert "zoro" not in out.get("review_rulings", {})


def test_undo_never_removes_a_hard_fix_a_human_owns():
    g = base(
        hard_fixes={"Zolo": "Zoro"},
        acquired={"Zolo": {"canonical": "Zoro", "run": "review"}},
        review_rulings={"zolo": {"decision": "apply", "canonical": "Zoro"}},
    )

    out = rp.undo(g, "Zolo")

    assert out["hard_fixes"] == {"Zolo": "Zoro"}
    assert "zolo" not in out.get("review_rulings", {})


# ---- backfill sweep -----------------------------------------------------------


def test_the_backfill_sweep_drains_the_live_pile_to_exceptions(tmp_path, monkeypatch):
    from dubtitlerr.glossary import glossary_audit, glossary_verify

    show = "Frieren Beyond Journey's End (2023) {tvdb-424536}"
    cache = glossary_audit.cache_path(show)
    os.makedirs(glossary_verify.CACHE_DIR, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"titles": TITLES, "api": "https://x/api.php"}))
    path = tmp_path / "frieren.json"
    path.write_bytes(open(FIXTURE, "rb").read())

    rep = rp.sweep([str(path)])

    doc = json.loads(path.read_text())
    assert rep["files"][0]["archived"] >= 5
    # Every pure-noise class is gone; the genuine decisions remain, plus the protected names.
    remaining = {t: (m if isinstance(m, str) else m.get("reason")) for t, m in (doc.get("flagged") or {}).items()}
    assert "English" not in remaining  # english-word, no wiki title
    assert "Journey" not in remaining
    assert remaining.get("Fern") == "english-word"  # protected by the wiki title
    assert remaining.get("Raftel") == "respelling-needs-review"  # uncorroborated
    assert remaining.get("Freeran") == "unseen-needs-evidence"
    assert doc["review_rulings"]["english"]["decision"] == "dismiss"
    assert doc["hard_fixes"]["Freeren"] == "Frieren"  # corroborated respelling applied
    # Idempotent: the second sweep changes nothing.
    before = path.read_bytes()
    again = rp.sweep([str(path)])
    assert path.read_bytes() == before
    assert again["files"][0]["archived"] == 0


def test_the_measured_baseline_distribution_drains_to_exceptions(tmp_path):
    """The card's own measurement: 1,877 entries -> 1,787 tier-1 dismissals, ~90 exceptions."""
    counts = {
        "english-word": 1542,
        "below-floor": 216,
        "sentence-initial-only": 29,
        "respelling-needs-review": 45,
        "possessive_floor_crossing": 20,
        "unseen-needs-evidence": 12,
        "conflicts-with-curated-fix": 6,
        "no-wiki-match": 3,
        "common-word": 3,
        "transcript-new-term": 1,
    }
    flagged = {}
    for reason, n in counts.items():
        for i in range(n):
            term = f"Noise{i}{reason.split('-')[0].title()}"
            meta = {"reason": reason}
            if reason in ("respelling-needs-review",):
                meta["canonical"] = f"Canon{i}"
            flagged[term] = meta
    g = base(flagged=flagged)

    rep = rp.apply_policy(g, str(tmp_path / "g.json"), titles=["SomeWikiPage"])

    assert rep["tiers"]["dismiss"]["dismissed"] == 1787
    assert rep["tiers"]["collapsed"] == 3
    assert rep["archived"] == 1790
    assert len(g.get("flagged") or {}) == 87
    reasons = [m["reason"] for m in g["flagged"].values()]
    assert "english-word" not in reasons and "below-floor" not in reasons and "sentence-initial-only" not in reasons
    assert len(g["review_rulings"]) == 1787


def test_a_dry_run_leaves_a_real_glossary_byte_identical(tmp_path):
    """Byte-real input: the shipped One Pace glossary. A dry run never touches the file."""
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "glossaries", "One Pace.json")
    path = tmp_path / "One Pace.json"
    path.write_bytes(open(src, "rb").read())
    before = path.read_bytes()

    rep = rp.sweep([str(path)], dry_run=True)

    assert path.read_bytes() == before
    assert rep["files"][0]["archived"] == 2, "its two candidate-less no-match entries collapse"
