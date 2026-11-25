"""Cue-flow style guards: a proposal that is well-formed but breaks the delivered house
style is refused under its own label, so the run report shows the class.

The three classes (A/B of the delivered artifacts, 2026-11-25):

- ``style:continuation-break`` -- a sentence-ending period added where the next cue
  continues the same sentence (lowercase first word).
- ``style:tone-flattened``    -- an ASR '?' or '!' replaced by a plain period.
- ``style:continuation-cap``  -- a capital added to a card-initial word where the
  previous cue does not finish a sentence.

Each has a true positive and a true negative: a period at a real sentence end (the next
cue starts a sentence) still passes, a kept tone mark passes, a capital at a sentence
start passes, and all of them pass when ``REPAIR_GUARD_CUE_STYLE=0``.
"""

import pytest
import repair_ref_stub
import store_seed

from dubtitlerr.core import artifact_store, unresolved
from dubtitlerr.glossary import glossary
from dubtitlerr.pipeline import repair

GL = glossary.load_dict({"names": ["Meowy", "Chainsaw Man"], "phrases": ["Straw Hat Pirates"]})


@pytest.fixture(autouse=True)
def ordinary_words(monkeypatch):
    """A word list that is present, whatever this machine has: `hawks` is an ordinary
    word, so a mid-sentence `Hawks` is a style cap rather than a name."""
    monkeypatch.setattr(glossary, "_ORDINARY", {"hawks", "war", "water", "young", "matter", "american"})
    monkeypatch.setattr(glossary, "_ORDINARY_STATUS", "ok")


# ---- 1. a period added where the next cue continues the sentence ----


def test_a_period_added_before_a_lowercase_continuation_is_refused():
    orig = "The American matter has got the Soviet war"
    assert (
        repair.rejecting_guard(orig, orig + ".", "", 6.0, GL, next_text="hawks crowing louder by the day")
        == "style:continuation-break"
    )
    assert not repair.accept_repair(orig, orig + ".", "", 6.0, GL, next_text="hawks crowing louder by the day")


def test_a_comma_turned_into_a_period_before_a_lowercase_continuation_is_refused():
    orig = "if you ask me,"
    assert (
        repair.rejecting_guard(orig, "if you ask me.", "", 6.0, GL, next_text="then we should go") == "style:continuation-break"
    )


def test_a_leading_quote_does_not_hide_a_lowercase_continuation():
    orig = "The American matter has got the Soviet war"
    assert repair.rejecting_guard(orig, orig + ".", "", 6.0, GL, next_text='"hawks crowing louder"') == "style:continuation-break"


def test_a_period_at_a_real_sentence_end_passes():
    """The next cue starts a new sentence, so the period is correct and must ship."""
    orig = "The American matter has got the Soviet war"
    assert repair.rejecting_guard(orig, orig + ".", "", 6.0, GL, next_text="The military use of devils") is None
    assert repair.accept_repair(orig, orig + ".", "", 6.0, GL, next_text="The military use of devils")


def test_without_a_next_cue_the_ending_is_not_judged():
    orig = "The American matter has got the Soviet war"
    assert repair.rejecting_guard(orig, orig + ".", "", 6.0, GL, next_text="") is None


# ---- 2. a tone mark replaced by a period ----


@pytest.mark.parametrize(
    "orig,new",
    [
        ("You're quitting already?", "You're quitting already."),
        ("Water!", "Water."),
        ("That's young!", "That's young."),
    ],
)
def test_an_interrogative_or_emphatic_mark_flattened_to_a_period_is_refused(orig, new):
    assert repair.rejecting_guard(orig, new, "", 6.0, GL) == "style:tone-flattened"
    assert not repair.accept_repair(orig, new, "", 6.0, GL)


def test_keeping_the_tone_mark_passes():
    assert repair.rejecting_guard("Water!", "Water!", "", 6.0, GL) == "unchanged"  # no proposal
    assert repair.rejecting_guard("So you're quitting?", "So, you're quitting!", "", 6.0, GL) is None


def test_a_period_where_the_asr_had_no_tone_passes():
    """`Water` -> `Water.` at a clause end is not a tone flatten: nothing carried a tone."""
    assert repair.rejecting_guard("Water", "Water.", "", 6.0, GL, next_text="Then we leave") is None


# ---- 3. a capital added at a continuation ----


def test_a_card_initial_capital_after_an_unfinished_cue_is_refused():
    orig = "hawks crowing louder by the day"
    assert (
        repair.rejecting_guard(orig, "Hawks crowing louder by the day", "", 6.0, GL, prev_text="The Soviet war")
        == "style:continuation-cap"
    )


def test_a_card_initial_capital_after_a_finished_cue_ships(monkeypatch):
    """The previous card ends a sentence, so the capital is correct."""
    monkeypatch.setenv("REPAIR_ACCEPT_PUNCT_CASE_ONLY", "1")
    assert (
        repair.rejecting_guard("hawks crowing louder", "Hawks crowing louder", "", 6.0, GL, prev_text='"The Soviet war."') is None
    )
    assert repair.accept_repair("hawks crowing louder", "Hawks crowing louder", "", 6.0, GL, prev_text='"The Soviet war."')


def test_a_glossary_name_is_not_a_catchy_cap(monkeypatch):
    monkeypatch.setenv("REPAIR_ACCEPT_PUNCT_CASE_ONLY", "1")
    assert repair.rejecting_guard("meowy is here", "Meowy is here", "", 6.0, GL, prev_text="and") is None


def test_without_prev_text_the_capital_is_not_judged():
    assert repair.rejecting_guard("hawks crowing", "Hawks crowing", "", 6.0, GL) != "style:continuation-cap"


# ---- 4. the flag turns all three off ----


@pytest.mark.parametrize(
    "orig,new,kwargs",
    [
        ("The American matter has got the Soviet war", "The American matter has got the Soviet war.", {"next_text": "hawks"}),
        ("Water!", "Water.", {}),
        ("hawks crowing", "Hawks crowing", {"prev_text": "The Soviet war"}),
    ],
)
def test_the_kill_switch_disables_every_cue_style_guard(monkeypatch, orig, new, kwargs):
    monkeypatch.setenv("REPAIR_GUARD_CUE_STYLE", "0")
    assert repair.rejecting_guard(orig, new, "", 6.0, GL, **kwargs) not in {
        "style:continuation-break",
        "style:tone-flattened",
        "style:continuation-cap",
    }


# ---- 5. the run summary counts the classes, and the queue names the guard ----


def run_episode(tmp_path, monkeypatch, cards, answers, name="ep", dur=6.0):
    """repair.process over ``cards`` (each ``dur`` s); the LLM answers ``answers`` in order."""
    sub = tmp_path / name
    sub.mkdir()
    stem = str(sub / "ep")
    conf = [
        {"start": k * dur, "end": (k + 1) * dur, "text": t, "avg_logprob": -0.6, "no_speech_prob": 0.1}
        for k, t in enumerate(cards)
    ]
    open(stem + repair.SRT_SUFFIX, "w").close()
    store_seed.conf(stem + repair.CONF_SUFFIX, conf)
    monkeypatch.setattr(repair, "find_video", lambda s: str(sub / "ep.mkv"))
    monkeypatch.setattr(repair, "glossary_for", lambda video: glossary.load_dict({}))
    monkeypatch.setattr(
        repair,
        "reference_tracks",
        repair_ref_stub.stub([(k * dur, (k + 1) * dur, "the official sub") for k in range(len(cards))]),
    )
    it = iter(answers)
    monkeypatch.setattr(repair, "llm", lambda prompt, model=None: next(it))
    repair.process(stem + repair.CONF_SUFFIX)
    return artifact_store.get_json(stem, "repair-summary"), unresolved.items(stem)


def test_the_summary_counts_each_class_and_a_legit_period_still_ships(tmp_path, monkeypatch):
    cards = [
        "The American matter has got the Soviet war",  # next cue continues lowercase -> break
        "hawks crowing louder by the day",  # next cue starts a sentence -> the period ships
        "Water!",  # tone flattened
        "Then we leave",  # untouched
        "war hawks crowing",  # card-initial cap after an unfinished cue
    ]
    answers = [
        "The American matter has got the Soviet war.",
        "hawks crowing louder by the day.",
        "Water.",
        "Then we leave",
        "War hawks crowing",
    ]
    summary, items = run_episode(tmp_path, monkeypatch, cards, answers)
    assert summary["rejected_style_continuation_break"] == 1
    assert summary["rejected_style_tone_flattened"] == 1
    assert summary["rejected_style_continuation_cap"] == 1
    assert summary["rejected_guard"] == 3
    assert summary["repaired"] == 1  # the legit sentence-ending period on the second card
    assert summary["targets"] == 5
    guards = [e["guard"] for e in items if e["reason"] == "rejected_guard"]
    assert guards == ["style:continuation-break", "style:tone-flattened", "style:continuation-cap"]


def test_the_summary_drops_the_counters_when_the_guards_are_off(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIR_GUARD_CUE_STYLE", "0")
    summary, _ = run_episode(
        tmp_path,
        monkeypatch,
        ["The American matter has got the Soviet war"],
        ["The American matter has got the Soviet war."],
    )
    assert "rejected_style_continuation_break" not in summary
    assert summary["repaired"] == 1  # the ordinary gate admits it, exactly as before this guard
