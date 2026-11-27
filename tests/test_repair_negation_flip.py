"""The negation-polarity guard: a repair that reverses a line's truth conditions is refused.

Found by the 2026-11-27 replay of the saved CSM rounds (14 model runs): 'It's futile.' ->
'It's not futile.' (z5-granite) and three more of the shape shipped; every candidate refusal
was hand-classified and this file pins the classes. The refusal is symmetric and gated by
REPAIR_GUARD_NEGATION (default on).
"""

from dubtitlerr.glossary import glossary
from dubtitlerr.pipeline import repair

GL = glossary.load_dict({"names": ["Kaido", "Meowy"], "phrases": []})


def test_gaining_negation_is_refused():
    # the measured corruption: admitted by every guard before this one
    assert repair.rejecting_guard("It's futile.", "It's not futile.", "", 3.0, GL) == "negation_flip"


def test_dropping_negation_is_refused():
    assert repair.rejecting_guard("Life's not fair.", "Life's is fair.", "", 3.0, GL) == "negation_flip"


def test_contraction_expansion_keeps_the_polarity():
    assert repair.rejecting_guard("I ain't going.", "I'm not going.", "", 3.0, GL) is None
    assert repair.rejecting_guard("It isn't over.", "It is not over.", "", 3.0, GL) is None


def test_a_spaced_contraction_is_joined_before_the_check():
    # 7 of the 22 candidate refusals in the replay were exactly this shape; re-spacing one
    # word must not read as a gain or drop of negation, whichever side the space lands on
    assert not repair.flips_negation("you don 't even know", "You don't even know.")
    assert not repair.flips_negation("you don' t even know", "You don't even know.")
    assert repair.rejecting_guard("you don 't even know", "You don't even know.", "", 3.0, GL) is None


def test_flips_negation_reads_both_directions():
    assert repair.flips_negation("It's futile.", "It's not futile.")
    assert repair.flips_negation("Never mind.", "Mind.")
    assert not repair.flips_negation("No, no, no.", "No, no!")


def test_an_ordinary_repair_passes():
    assert repair.rejecting_guard("we have to go", "We have to go.", "", 3.0, GL) is None


def test_the_kill_switch(monkeypatch):
    monkeypatch.setenv("REPAIR_GUARD_NEGATION", "0")
    assert repair.accept_repair("It's futile.", "It's not futile.", "", 3.0, GL)
