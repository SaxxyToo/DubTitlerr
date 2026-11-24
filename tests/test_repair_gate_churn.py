"""The gate must not ship churn: glyph-only no-ops, deleted content, or short reference borrows.

Measured on the 2026-11-23 Chainsaw Man S01 bake-off (406 anchored lines, 12 models, every model
judged by this same ``accept_repair``) by replaying its saved per-line outputs:

* 6 of the 1041 shipped repairs were straight<->curly apostrophe swaps (all ministral-3-8b) --
  the report's larger "apostrophe churn" shares are surface-only edits as a group (punctuation
  and case included), which this gate keeps admitting on purpose;
* ``It's not like I was asking to`` -> ``It's not like I was asking to be rich`` (the tail exists
  only in the fansub line) was recorded ``admitted=True`` by the gate (cand-phi3-grammar and
  r2-phi-3.5-mini, saved per-line results);
* clause deletions the length band cannot see shipped too (the round-1 hand-judge).

The fixtures that carry a ``ref``/duration are the saved bake-off rows byte-for-byte. The wanted
fixes in the same sample -- split words, duplicate collapse, casing, name order -- must keep
shipping, and they are pinned here beside the refusals.
"""

import pytest

from dubtitlerr.glossary import glossary
from dubtitlerr.pipeline import repair


def gl(names=None, phrases=None):
    return glossary.load_dict({"names": names or [], "phrases": phrases or [], "hard_fixes": {}})


GL = gl(names=["Zoro"])


# ---- glyph-only no-ops -------------------------------------------------------


@pytest.mark.parametrize(
    "orig,new",
    [
        ("You're hurt.", "You\u2019re hurt."),  # the measured churn: straight -> curly
        ("You\u2019re hurt.", "You're hurt."),  # and back
        ('He said "stop".', "He said \u201cstop\u201d."),  # quote glyphs
        ("It`s here.", "It's here."),  # backtick apostrophe
        ("You\u2019re hurt.", "You\u2019re hurt."),  # identical is still nothing
    ],
)
def test_a_glyph_only_proposal_is_refused_as_unchanged(orig, new):
    assert repair.rejecting_guard(orig, new, "", 6.0, GL) == "unchanged"
    assert not repair.accept_repair(orig, new, "", 6.0, GL)


def test_the_punct_case_flag_does_not_revive_glyph_churn(monkeypatch):
    """The relaxed punctuation/case path folds the curly apostrophe itself, so without an
    explicit check it would call a glyph swap a punctuation addition and admit it."""
    monkeypatch.setenv("REPAIR_ACCEPT_PUNCT_CASE_ONLY", "1")
    assert repair.rejecting_guard("You're hurt.", "You\u2019re hurt.", "", 6.0, GL) == "unchanged"


def test_fold_glyphs_touches_only_the_lookalikes():
    assert repair.fold_glyphs("You\u2019re \u201clate\u201d, aren't") == "You're \"late\", aren't"
    assert repair.fold_glyphs(None) == ""


# ---- content-word drops ------------------------------------------------------


@pytest.mark.parametrize(
    "orig,new",
    [
        # the ticket's three measured drops; the first two orig/ref are saved bake-off rows
        ("Her face is so priceless.", "Her face is priceless."),
        ("Let's take turns guarding Arai and Kobeni.", "Let's guard Arai and Kobeni."),
        ("I'll keep that one in mind for you, too.", "I'll keep that in mind for you."),
        # a condensed replace is a drop too, not a one-for-one substitution
        ("We'll take turns watching him closely.", "We'll watch him closely."),
    ],
)
def test_a_content_word_dropped_with_no_reference_support_is_refused(orig, new):
    assert repair.rejecting_guard(orig, new, "", 6.0, GL) == "content_drop"
    assert not repair.accept_repair(orig, new, "", 6.0, GL)


def test_a_dropped_content_word_the_reference_carries_is_left_to_the_model():
    """The reference attests the words, so dropping them is the model's judgement on the audio,
    and the guard must not fire."""
    orig, new = "Let's take turns guarding Arai and Kobeni.", "Let's guard Arai and Kobeni."
    assert repair.rejecting_guard(orig, new, "Let's take turns guarding Arai and Kobeni.", 6.0, GL) is None
    assert repair.accept_repair(orig, new, "Let's take turns guarding Arai and Kobeni.", 6.0, GL)


def test_a_one_for_one_name_fix_is_not_a_drop():
    """``Spondum`` -> ``Spandam`` trades one content word for another; nothing is lost."""
    assert repair.dropped_content_words("Spondum drew his blade.", "Spandam drew his blade.", "") == []
    assert repair.accept_repair(
        "Spondum drew his blade.", "Spandam drew his blade.", "Spandam drew his blade, sneering.", 6.0, gl(["Spandam"])
    )


def test_dropped_content_words_reports_the_collapsed_side():
    assert repair.dropped_content_words("Let's take turns guarding.", "Let's guard.", "") == ["take", "turns", "guarding"]


def test_a_substitution_that_trades_a_content_word_for_another_is_not_a_drop():
    """``What you ain't`` -> ``What aren't you?`` loses one content word and gains another: the
    line's information is intact, so it is a correction, not a deleted clause."""
    assert repair.dropped_content_words("What you ain't", "What aren't you?", "") == []
    assert repair.dropped_content_words("Aki's still a punk, so train him up.", "Aki's still weak, so train him up.", "") == []


def test_a_respelled_join_is_not_a_drop():
    """A split word rejoined under a different spelling keeps its material: the fragments are
    absorbed into the joined word, so the line does not lose content."""
    orig = "Once the gun devil's dead and buried, I'll get lots of smoo cheroos."
    new = "Once the Gun Devil's dead and buried, I'll get lots of smooches."
    assert repair.dropped_content_words(orig, new, "") == []


def test_a_dropped_word_absent_from_the_reference_still_fires_with_a_reference_present():
    """The reference is what separates a judgement from a deletion: ``so`` is neither in it nor
    an ASR fragment, so the intensifier leaving is the meaning change."""
    assert repair.dropped_content_words("Her face is so priceless.", "Her face is priceless.", "That face!") == ["so"]


def test_a_fragment_absorbed_into_a_word_is_not_a_drop():
    assert repair._absorbed("shoun", ["shounen"])
    assert repair._absorbed("en", ["shounen"])
    assert repair._absorbed("thats", ["that's"])
    assert not repair._absorbed("guarding", ["guard"])


def test_the_content_drop_guard_is_not_applied_to_a_dub_transcript():
    """Under role=dub the reference IS the audio's transcript, so a word it lacks is likelier ASR
    noise than a deletion; the other guards still apply."""
    orig = "This is a long line of dialogue that the card really does need to keep."
    assert repair.dropped_content_words(orig, "hi there", "what are you doing")
    assert repair.rejecting_guard(orig, "hi there", "hi there", 6.0, GL, ref_role="dub") == "length_ratio"


# ---- reference borrowing, phrase level ---------------------------------------


def test_a_two_word_tail_borrowed_from_the_reference_is_refused():
    """The saved bake-off row (S01E01, dur 6.457 s), recorded admitted=True before this guard:
    the tail ``be rich`` exists only in the fansub line the model was shown."""
    orig, new, ref = (
        "It's not like I was asking to",
        "It's not like I was asking to be rich.",
        "It's not like I wanted to be rich.",
    )
    assert repair.borrowed_phrase_from_ref(orig, new, ref) == ["be", "rich"]
    assert repair.rejecting_guard(orig, new, ref, 6.457, GL) == "ref_borrow"
    assert not repair.accept_repair(orig, new, ref, 6.457, GL)


def test_the_same_tail_is_attestation_when_the_reference_is_a_dub_transcript():
    """A dub caption is a transcript of the audio: the words it carries are evidence, not a leak."""
    orig, new, ref = (
        "It's not like I was asking to",
        "It's not like I was asking to be rich.",
        "It's not like I wanted to be rich.",
    )
    assert repair.accept_repair(orig, new, ref, 6.457, GL, ref_role="dub")


def test_one_added_reference_word_is_still_a_disambiguation_aid():
    """The case the reference exists to serve: one misheard token corrected from it."""
    orig, new, ref = "Spondum drew his blade.", "Spandam drew his blade.", "Spandam drew his blade, sneering."
    assert repair.borrowed_phrase_from_ref(orig, new, ref) == []
    assert repair.accept_repair(orig, new, ref, 6.0, gl(["Spandam"]))


def test_a_contiguous_borrow_trips_the_phrase_guard_and_the_count_together():
    """The phrase guard is additive, not a replacement: a three-word lift goes through the
    count limit as it always did, and the run itself is visible to the phrase guard."""
    orig, new, ref = "Enough of that, idiots!", "enough of that, hold it brats!", "hold it brats"
    assert repair.borrowed_phrase_from_ref(orig, new, ref) == ["hold", "it", "brats"]
    assert len(repair.borrowed_from_ref(orig, new, ref)) >= repair.MAX_REF_BORROW
    assert repair.rejecting_guard(orig, new, ref, 6.0, GL) == "ref_borrow"


def test_a_two_word_bigram_in_the_middle_of_a_line_is_not_a_borrow():
    """``gonna`` -> ``going to`` matches the reference's own "going to", and so does every other
    ordinary expansion. A mid-line two-word match is not evidence of copying."""
    orig = "This time I'm gonna cut him right down the middle."
    new = "This time I'm going to cut him right down the middle."
    ref = "I'm going to cut him right in two."
    assert repair.borrowed_phrase_from_ref(orig, new, ref) == []
    assert repair.accept_repair(orig, new, ref, 6.0, GL)


def test_a_contraction_expanded_at_the_line_start_is_not_a_borrow():
    """``We've`` -> ``We have`` at the head matches the reference's "We have", but the model only
    expanded the contraction the line already carried."""
    orig = "We've got a secret weapon down in the basement."
    new = "We have a secret weapon down in the basement."
    ref = "We have a secret weapon in the basement."
    assert repair.borrowed_phrase_from_ref(orig, new, ref) == []
    assert repair.accept_repair(orig, new, ref, 6.0, GL)


# ---- the wanted fixes must keep shipping -------------------------------------


@pytest.mark.parametrize(
    "orig,new,ref,gloss",
    [
        # split words
        ("th warted", "warted", "", gl()),
        ("tim id", "timid", "", gl()),
        ("shoun en", "shounen", "", gl()),
        # a duplicate collapsed
        ("You've You've got to go.", "You've got to go.", "", gl()),
        # a dropped apostrophe restored
        ("thats right.", "that's right.", "", gl()),
        # a name re-spaced, with the ASR line as the reference
        (
            "Vanderdecken and Vanderdecken are here.",
            "Van der Decken and Van der Decken are here.",
            "Vanderdecken and Vanderdecken are here.",
            gl(phrases=["Van der Decken"]),
        ),
        ("Takashi Inoue, wait!", "Inoue Takashi, wait!", "Inoue Takashi, wait!", gl()),
        # casing + a name fix in one proposal
        ("i saw zorro today", "I saw Zoro today.", "I saw Zoro today.", gl(names=["Zoro"])),
    ],
)
def test_the_wanted_asr_garbage_fixes_still_ship(orig, new, ref, gloss):
    assert repair.rejecting_guard(orig, new, ref, 6.0, gloss) is None
    assert repair.accept_repair(orig, new, ref, 6.0, gloss)
