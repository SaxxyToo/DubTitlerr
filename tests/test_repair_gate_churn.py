"""The gate must not ship churn: glyph-only no-ops, deleted content, short reference borrows, or a
reply that discards the line entirely and answers with something else (a prompt fragment included).

Measured on the 2026-11-23 Chainsaw Man S01 bake-off (406 anchored lines, 12 models, every model
judged by this same ``accept_repair``) by replaying its saved per-line outputs:

* 6 of the 1041 shipped repairs were straight<->curly apostrophe swaps (all ministral-3-8b) --
  the report's larger "apostrophe churn" shares are surface-only edits as a group (punctuation
  and case included), which this gate keeps admitting on purpose;
* ``It's not like I was asking to`` -> ``It's not like I was asking to be rich`` (the tail exists
  only in the fansub line) was recorded ``admitted=True`` by the gate (cand-phi3-grammar and
  r2-phi-3.5-mini, saved per-line results);
* clause deletions the length band cannot see shipped too (the round-1 hand-judge).

The round-4 replay (2026-11-24) added the replaced-line class: a model answering with unrelated
text -- SmolLM-FT-CoEdIT returned a sentence of its own prompt on nearly every line -- gained as
many words as it dropped, so ``dropped_content_words`` (which weighs drops against gains) admitted
every one. See the replaced-line section at the end of this file.

The fixtures that carry a ``ref``/duration are the saved bake-off rows byte-for-byte. The wanted
fixes in the same sample -- split words, duplicate collapse, casing, name order -- must keep
shipping, and they are pinned here beside the refusals.
"""

import csv
import io

import pytest
import repair_ref_stub
import store_seed

from dubtitlerr.core import artifact_store, unresolved
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


def test_a_truncated_word_is_not_a_compensating_gain():
    """``drunker`` -> ``drunk`` hands back a HEAD of the dropped word, not a substitute for it: the
    drop stands and the guard fires. Hand-judge fixture (2026-11-26, r6 CSM S01 replay, byte-for-byte):
    the one live truncation that sailed through when a gain of any shape counted as compensation."""
    orig, new = "But I'll do it when I'm drunker.", "But I'll do it when I'm drunk"
    assert repair.dropped_content_words(orig, new, "") == ["drunker"]
    assert repair.rejecting_guard(orig, new, "", 6.0, GL) == "content_drop"
    assert not repair.accept_repair(orig, new, "", 6.0, GL)


def test_the_mid_word_cut_the_same_hand_judge_flagged_is_refused():
    """``Tayo`` -> ``T``: the fragment filter (under ``MIN_CONTENT_DROP`` and not a word) already
    refuses it; pinned here as the measured row beside the truncation clause above."""
    assert (
        repair.rejecting_guard("You're getting on my nerves, Tayo!", "You're getting on my nerves, T", "", 6.0, GL)
        == "content_drop"
    )


def test_a_one_letter_clipping_is_a_contraction_not_a_truncation():
    """``coming`` -> ``comin'`` sheds one letter -- ordinary colloquial dub style, not a cut word,
    and it must keep shipping; only two or more letters of loss are a truncation."""
    assert repair.dropped_content_words("It's coming right at us!", "It's comin' right at us!", "") == []
    assert repair.rejecting_guard("It's coming right at us!", "It's comin' right at us!", "", 6.0, GL) is None


def test_a_capitalised_truncation_shaped_name_fix_still_ships():
    """``Poway`` -> ``Pow`` is the one real r6 row the truncation clause could misread: a capitalised
    source word is proper-noun territory, and the name-swap replay pinned that content_drop must not
    over-match it (Reference.md, '0 on content_drop'). Byte-for-byte from the replay -- both qwen and
    granite replied with the same corrected row."""
    orig = "Smoky Moe is asking for me and Poway, so we're gonna bounce ."
    new = "Smoky Moe is asking for me and Pow, so we're gonna bounce."
    ref = "Makima wants to see me and Pow, so we're leaving."
    assert repair.dropped_content_words(orig, new, ref) == []
    assert repair.rejecting_guard(orig, new, ref, 6.0, GL) is None
    assert repair.accept_repair(orig, new, ref, 6.0, GL)


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


# ---- a reply that replaces the line instead of repairing it ------------------
#
# Measured 2026-11-24 by replaying every saved row of the round-4 bake-off through the chain with
# this guard disabled and enabled: of the 2295 rows the CURRENT chain admits, 108 are refused by
# this guard and nothing else -- 104 of them SmolLM-FT-CoEdIT (135M) parroting a sentence of its
# own prompt, and 4 where a model answered with unrelated text. Read by hand, every one is a
# refusal; there is no false positive in the sample. The fixtures below are those rows.
#
# WHICH GUARD RECORDS the refusal can depend on the word list: this suite deliberately runs without
# the production list (conftest points WORDLIST_PATH at an empty file), so is_english() falls back
# to a 228-word bundle and a gained capitalised word like 'Ghost' counts as an invented name. Both
# guards refuse the row either way. The assertions below therefore pin what THIS guard owns -- its
# predicate -- for every row, and pin its recorded NAME only for the rows that gain no capitalised
# token, so no name guard can claim them first in either environment.

REPLACED_ROWS = [
    # the prompt fragment, on nearly every line that model answered (S01E01)
    (
        "Though his worthless dead dad accrued it, Denji's the one stuck with the bill.",
        "Return ONLY the corrected line - no quotes, no notes, no rule text.",
        "He's repaying his debt to us. Well, really it's the debt his piece-of-shit father left when he died.",
    ),
    # and three from models that are not the parroting one: unrelated text, not a repair
    (
        "It's just a pathetic attempt at self-comfort.",
        "Nothing lives in your heart, so spare me.",
        "is nothing but a pitiful attempt at self-consolation.",
    ),
    ("You're better than you were before.", "Ghost, kill this guy.", "You're fighting better than last time."),
    (
        "Soon I'll get my hookup night with Ms.",
        "So now I can kill all kinds of devils.",
        "And that'll help me get together with Makima.",
    ),
]


@pytest.mark.parametrize("orig,new,ref", REPLACED_ROWS)
def test_a_reply_that_discards_the_line_is_refused(orig, new, ref):
    assert repair.replaced_line(orig, new)
    assert not repair.accept_repair(orig, new, ref, 6.0, GL)


@pytest.mark.parametrize("orig,new,ref", [REPLACED_ROWS[1], REPLACED_ROWS[3]])
def test_the_replaced_guard_is_the_recorded_reason(orig, new, ref):
    """'replaced' is what the review queue records for these two -- neither reply gains a
    capitalised token, so no name guard competes for them -- which is what shows the guard is
    wired into the chain and not merely a predicate sitting beside it."""
    assert repair.rejecting_guard(orig, new, ref, 6.0, GL) == "replaced"


def test_a_line_made_of_one_repeated_word_is_out_of_scope():
    """``Woah, woah, woah, woah!`` -> ``Whoa, whoa, whoa, whoa!`` is the same interjection
    respelled -- seven models in the replay, every one a real if trivial spelling fix. A line of
    one repeated word carries no proposition whose loss could be detected, so this guard stays out
    of it. (Some word lists make 'Whoa' an invented name and refuse the row anyway; that is a
    different guard's call, and not pinned here.)"""
    assert not repair.replaced_line("Woah, woah, woah, woah!", "Whoa, whoa, whoa, whoa!")


def test_a_short_line_replaced_by_a_constant_string_is_refused():
    """The round-5 sweep's find: a model answered the single string `Thinking Process:` (its own
    reasoning header) on line after line. Every row it got admitted on replaced a SHORT line, and
    REPLACED_MIN_WORDS was 3, so the guard could not fire. These are the measured rows.

    Which guard RECORDS the refusal depends on the word list -- with the suite's fallback bundle
    'Thinking' reads as a proper name and `invents_name` claims the row first; with the production
    list it is an ordinary word and this guard is what fires. Both refuse it, so what is pinned
    here is the predicate, which is what this change owns."""
    for orig in ("Where's the exit?", "Here's an idea.", "What's the rush, crazy?"):
        assert repair.replaced_line(orig, "Thinking Process:"), orig
        assert not repair.accept_repair(orig, "Thinking Process:", "", 6.0, GL), orig


def test_a_single_content_word_line_stays_out_of_the_short_line_rule():
    """Lowering the floor to 2 must not reach a line whose whole content is one word -- that is the
    substitution shape the guard has always excluded, and `What's that?` is the measured partner of
    the rows above (same model, same constant reply, one content word)."""
    assert not repair.replaced_line("What you ain't", "What aren't you?")
    assert not repair.replaced_line("What's that?", "Thinking Process:")
    assert not repair.replaced_line("th warted", "warted")
    assert not repair.replaced_line("tim id", "timid")


def test_a_two_word_line_must_lose_BOTH_words_to_be_refused():
    """The other direction of the same floor: a two-content-word line whose reply keeps one of them
    is a substitution, not a replacement."""
    assert not repair.replaced_line("Spondum drew", "Spandam drew")  # one word kept
    assert repair.replaced_line("Spondum drew", "Nothing lives here")  # both gone


@pytest.mark.parametrize(
    "orig,new",
    [
        ("th warted", "warted"),
        ("tim id", "timid"),
        ("shoun en", "shounen"),
        ("You've You've got to go.", "You've got to go."),
        ("thats right.", "that's right."),
        ("i saw zorro today", "I saw Zoro today."),
        ("Spondum drew his blade.", "Spandam drew his blade."),
        ("Vanderdecken and Vanderdecken are here.", "Van der Decken and Van der Decken are here."),
    ],
)
def test_the_wanted_fixes_are_outside_the_replacement_guard(orig, new):
    assert not repair.replaced_line(orig, new)


def test_kept_content_words_absorbs_a_rejoined_fragment():
    kept = repair.kept_content_words("th warted my plans", "thwarted my plans")
    assert kept["warted"] == 1  # absorbed into the word it was joined into


def test_kept_content_words_counts_a_duplicate_collapse_as_one_lost_word():
    kept = repair.kept_content_words("You've You've got to go.", "You've got to go.")
    assert kept["you've"] == 1  # not two kept ones: one of the pair went


def test_a_fully_dub_vouched_reply_is_exempt_from_the_replacement_guard():
    """Under role=dub the reference is a transcript of the audio, so a reply whose added words it
    attests is evidence -- the same exemption ref_borrow takes."""
    orig = "The old man walked to the market slowly."
    new = "He ran home quickly, and he was happy."
    assert repair.replaced_line(orig, new)
    assert repair.rejecting_guard(orig, new, new, 6.0, GL, ref_role="dub") is None


# ---- a proper-noun spelling fix must keep shipping ---------------------------
#
# Chainsaw Man S01E03/E04, byte-for-byte from the LIVE instance (2026-11-24): the card texts and
# durations are the stored ``conf`` rows, the references are the windows the repair stage sent,
# the four replies are what qwen3.5:4b and gemma-3-1b actually returned at temperature 0.
#
# Reported as "the #121 gate now refuses Meowie -> Meowy; the Nov 22 build shipped Meowy". It does
# not. The gate admits the swap, with a reference and without one (the first three tests), and the
# live run recorded the same admissions (``I hate them, too, since one took my pet cat, Meowie,
# away.`` -> ``...Meowy, away.`` was in that episode's repair.csv, and the delivered sidecar
# carried it). What it refuses is the LINE the name arrives on -- a reply that drops a word
# (content_drop) or hands back the fansub's own sentence (ref_borrow) -- and the name goes with
# it. Those two refusals are pinned below so the distinction cannot quietly change: the fix that
# is allowed is the name ALONE, which is what an unusable reply has to be cut down to.
#
# Replayed over every one-token name-swap refusal the store holds (276 rows, 23 shows, 2026-11-24):
# 0 are content_drop, 202 fits_card, 25 invents_name, 7 length_ratio, 4 ref_borrow -- the
# content-word guard this gate was suspected of is not what refuses them.

CSM = gl(names=["Meowy", "Makima", "Denji", "Power"])
FANSUB_MEOWY = "And I hate devils too. Because one of them kidnapped my pet, Meowy."

MEOWIE_SWAP_ROWS = [
    # (orig, new, ref, dur) -- card 199.12-202.96 s on E03
    (
        "I hate them, too, since one took my pet cat, Meowie, away.",
        "I hate them, too, since one took my pet cat, Meowy, away.",
        FANSUB_MEOWY,
        3.84,
    ),
    # the same swap with no reference at all: a no-ref episode must still be able to fix a name
    # the glossary lists (refusal here would be the vouched_name guard over-reaching)
    (
        "I hate them, too, since one took my pet cat, Meowie, away.",
        "I hate them, too, since one took my pet cat, Meowy, away.",
        "",
        3.84,
    ),
    # the reply that shipped on the live run: the name fix riding a genuine split fix
    # (card 204.336-209.12 s)
    (
        "Before I was able to save Meowie, Makima caught me and th warted my plans.",
        "Before I was able to save Meowy, Makima caught me and thwarted my plans.",
        "Before I could Meowy get back, Makima caught me.",
        4.784,
    ),
    # a NAME-ONLY reply on the line whose whole-line copy was refused (card 209.46-213.6 s):
    # this is the shape that must keep working when the model offers nothing else
    (
        "Meowie may already be dead, but still, I can't give up.",
        "Meowy may already be dead, but still, I can't give up.",
        "Meowy might already be dead, but I cannot give up hope.",
        4.14,
    ),
]


@pytest.mark.parametrize("orig,new,ref,dur", MEOWIE_SWAP_ROWS)
def test_a_one_token_glossary_name_fix_is_admitted(orig, new, ref, dur):
    assert repair.rejecting_guard(orig, new, ref, dur, CSM, ref_role="translation") is None
    assert repair.accept_repair(orig, new, ref, dur, CSM, ref_role="translation")


def test_the_name_fix_is_not_what_the_gate_refuses_on_the_dead_line():
    """The documented refusal path for the live rows where the name did NOT ship: the names are
    identical in both refusals, so a refusal here is about the line, never the spelling."""
    orig = "Meowie may already be dead, but still, I can't give up."
    # gemma-3-1b handed back the fansub's sentence, name included
    fansub_line = "Meowy might already be dead, but I cannot give up hope."
    assert repair.rejecting_guard(orig, fansub_line, fansub_line, 4.14, CSM, ref_role="translation") == "ref_borrow"
    # gemma-3-1b elided 'away' to bring the line closer to the fansub's wording
    elided = "I hate them, too, since one took my pet cat, Meowy."
    assert (
        repair.rejecting_guard(
            "I hate them, too, since one took my pet cat, Meowie, away.", elided, FANSUB_MEOWY, 3.84, CSM, ref_role="translation"
        )
        == "content_drop"
    )
    # ...while the name-only cut-down of each is admitted. That is the only shape that can carry a
    # name out of a reply the gate refuses; the stage now cuts the reply down to it and offers the
    # cut-down to the same gate as its own candidate (see the salvage section at the end of this
    # file), rather than leaving the card on the old spelling until a later run proposes the name.
    assert repair.accept_repair(
        orig, "Meowy may already be dead, but still, I can't give up.", "", 4.14, CSM, ref_role="translation"
    )
    assert repair.accept_repair(
        "I hate them, too, since one took my pet cat, Meowie, away.",
        "I hate them, too, since one took my pet cat, Meowy, away.",
        "",
        3.84,
        CSM,
        ref_role="translation",
    )


# ---- salvaging a name fix out of a refused reply ------------------------------
#
# The passenger case (FINDINGS.md §6): a reply refused for its LINE can carry a correct
# glossary-name spelling fix down with it, and the card keeps the old spelling until another run
# proposes the name alone (4 of the 8 `Meowie` occurrences in CSM S01E03). The stage now cuts the
# refused reply down to just the same-length, glossary-name word replacement and offers that to
# the SAME gate as its own candidate -- every guard still judges it, it queues as an ordinary
# proposal, and no guard is loosened. These fixtures are the two live refusal rows from above.

SALVAGE_DROP_ORIG = "I hate them, too, since one took my pet cat, Meowie, away."
SALVAGE_DEAD_ORIG = "Meowie may already be dead, but still, I can't give up."

SALVAGE_ROWS = [
    # (orig, refused reply, ref, dur, guard, the cut-down that must be offered)
    # the reply elided 'away' -> content_drop; the cut-down restores both the name and the word
    (
        SALVAGE_DROP_ORIG,
        "I hate them, too, since one took my pet cat, Meowy.",
        FANSUB_MEOWY,
        3.84,
        "content_drop",
        "I hate them, too, since one took my pet cat, Meowy, away.",
    ),
    # the reply handed back the fansub's sentence -> ref_borrow; same single-name cut-down
    (
        SALVAGE_DEAD_ORIG,
        "Meowy might already be dead, but I cannot give up hope.",
        "Meowy might already be dead, but I cannot give up hope.",
        4.14,
        "ref_borrow",
        "Meowy may already be dead, but still, I can't give up.",
    ),
]


@pytest.mark.parametrize("orig,reply,ref,dur,guard,cut", SALVAGE_ROWS)
def test_a_refused_reply_is_cut_down_to_the_name_fix_it_carried(orig, reply, ref, dur, guard, cut):
    assert repair.rejecting_guard(orig, reply, ref, dur, CSM, ref_role="translation") == guard
    assert repair.salvage_name_fixes(orig, reply, CSM) == cut
    # The cut-down is a CANDIDATE, not an acceptance: the same gate judges it, and here it passes.
    assert repair.accept_repair(orig, cut, ref, dur, CSM, ref_role="translation")


@pytest.mark.parametrize(
    "orig,reply",
    [
        # a same-length swap whose destination is not a listed name: nothing to salvage
        (SALVAGE_DROP_ORIG, "I hate them, too, since one took my pet cat, Muffy, away."),
        # the spelling is unchanged and a word was ADDED: an insert never becomes a candidate
        (SALVAGE_DROP_ORIG, "I hate them, too, since one took my pet cat, Meowie, away, okay."),
        # the reply drops the name instead of fixing it
        (SALVAGE_DROP_ORIG, "I hate them, too, since one took my pet cat away."),
        # a whole-line fansub rewrite that does not carry the listed spelling
        ("Both Meowie and the devil are within.", "Both Muffy and the devil are in there."),
    ],
)
def test_salvage_yields_nothing_without_a_listed_name_replacement(orig, reply):
    assert repair.salvage_name_fixes(orig, reply, CSM) == ""


def _run_salvage(tmp_path, monkeypatch, orig, reply, dur):
    """Drive one card through process() with a canned reply and a reference carrying the fansub."""
    stem = str(tmp_path / "ep_salvage")
    open(stem + repair.SRT_SUFFIX, "w").close()
    store_seed.conf(
        stem + repair.CONF_SUFFIX,
        [{"start": 0.0, "end": dur, "text": orig, "avg_logprob": -0.6, "no_speech_prob": 0.1}],
    )
    monkeypatch.setattr(repair, "find_video", lambda s: str(tmp_path / "ep_salvage.mkv"))
    monkeypatch.setattr(repair, "glossary_for", lambda video: CSM)
    monkeypatch.setattr(repair, "reference_tracks", repair_ref_stub.stub([(0.0, dur, FANSUB_MEOWY)]))
    monkeypatch.setattr(repair, "llm", lambda prompt, model=None: reply)
    assert repair.process(stem + repair.CONF_SUFFIX) == "repaired"
    return stem


def test_a_refused_reply_ships_its_name_fix_as_an_ordinary_proposal(tmp_path, monkeypatch):
    stem = _run_salvage(tmp_path, monkeypatch, SALVAGE_DROP_ORIG, "I hate them, too, since one took my pet cat, Meowy.", 3.84)

    shipped = open(stem + repair.SRT_SUFFIX).read()
    assert "Meowy, away." in shipped and "Meowie" not in shipped

    summary = artifact_store.get_json(stem, "repair-summary")
    assert summary["repaired"] == 1 and summary["salvaged"] == 1
    assert summary["rejected_guard"] == 0  # counted once, as the repair that shipped

    rows = list(csv.reader(io.StringIO(artifact_store.get_text(stem, "repair.csv"), newline="")))
    assert rows[1][0] == SALVAGE_DROP_ORIG
    assert rows[1][1] == "I hate them, too, since one took my pet cat, Meowy, away."

    entries = unresolved.items(stem)
    # The raw reply's refusal stays recorded (it is why the reply was cut), and the cut-down is
    # queued as the line's ordinary question, flagged so a reviewer can see where it came from.
    assert any(e["reason"] == "rejected_guard" and e.get("proposed_text", "").endswith("Meowy.") for e in entries)
    (queued,) = [e for e in entries if e["stage"] == "repair_applied"]
    assert queued["proposed_text"] == "I hate them, too, since one took my pet cat, Meowy, away."
    assert queued.get("salvaged") is True


def test_a_refused_reply_without_a_name_fix_stays_refused(tmp_path, monkeypatch):
    reply = "I hate them, too, since one took my pet cat, Muffy, away."
    stem = _run_salvage(tmp_path, monkeypatch, SALVAGE_DROP_ORIG, reply, 3.84)
    summary = artifact_store.get_json(stem, "repair-summary")
    assert summary["repaired"] == 0 and summary["salvaged"] == 0 and summary["rejected_guard"] == 1
    assert "Meowie" in open(stem + repair.SRT_SUFFIX).read()
