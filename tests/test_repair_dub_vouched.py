"""R3: role-aware repair gates.

A native dub-caption track is a transcript of the audio this subtitle accompanies, so a word
the repair ADDS that appears there is attestation -- the opposite of the same word appearing in
a *different translation*, which is borrowing. These tests pin both halves: the three measured
DAN DA DAN refusals must be admitted under role=dub, and every translation-role verdict must
stay exactly what the classic gates returned.
"""


import pytest
import repair_ref_stub
import store_seed

from dubtitlerr.core import unresolved
from dubtitlerr.glossary import glossary
from dubtitlerr.pipeline import repair

DUB = "dub"
TRANSLATION = "translation"


def gl(names=None, phrases=None):
    return glossary.load_dict({"names": names or [], "phrases": phrases or [], "hard_fixes": {}})


# The two DDD S01E01 refusals the native dub track fully vindicates, with the duration their
# live card actually had (conf.json: 2.57s and 2.10s). Both are admitted under role=dub.
DDD_CASES = [
    (
        "for-your-information",
        "for your",
        "For your information,",
        "For your information, the curse is real and it is coming for you.",
        2.57,
        "length_ratio",
    ),
    (
        "lookin",
        "What are you looking at, doofus?",
        "What're you lookin' at, doofus?",
        "What're you lookin' at, doofus? Get out of here.",
        2.10,
        "invents_name",
    ),
]

# The third measured case, at its REAL card: 22 chars in 1.34s is already 16.4 cps, and the
# dub-vouched repair is 27 chars = 20.1 cps. fits_card is deliberately unchanged, so the name
# guard no longer refuses it and the CARD does. See the test below -- R3 must not make an
# unreadable line shippable just because a track attests the words.
MOMO = (
    "momo-ayase",
    "Miss Momoa, that's it.",
    "Miss Momo Ayase, that's it.",
    "Miss Momo Ayase, that's it. I knew it.",
    1.34,
    "invents_name",
)


@pytest.mark.parametrize("_name,orig,new,ref,dur,_guard", DDD_CASES, ids=[c[0] for c in DDD_CASES])
def test_an_all_vouched_dub_repair_is_accepted(_name, orig, new, ref, dur, _guard):
    assert repair.rejecting_guard(orig, new, ref, dur, gl(), ref_role=DUB) is None
    assert repair.accept_repair(orig, new, ref, dur, gl(), ref_role=DUB)


@pytest.mark.parametrize("_name,orig,new,ref,dur,guard", DDD_CASES, ids=[c[0] for c in DDD_CASES])
def test_the_same_repair_is_still_refused_under_a_translation_reference(_name, orig, new, ref, dur, guard):
    assert repair.rejecting_guard(orig, new, ref, dur, gl(), ref_role=TRANSLATION) == guard
    assert repair.rejecting_guard(orig, new, ref, dur, gl()) == guard  # unclassified/legacy: classic


@pytest.mark.parametrize("_name,orig,new,ref,dur,guard", DDD_CASES + [MOMO], ids=[c[0] for c in DDD_CASES + [MOMO]])
def test_the_rollback_switch_restores_classic_semantics(monkeypatch, _name, orig, new, ref, dur, guard):
    monkeypatch.setenv("REPAIR_GATE_DUB_VOUCHED", "0")
    assert repair.dub_vouches(orig, new, ref, DUB) is False
    assert repair.rejecting_guard(orig, new, ref, dur, gl(), ref_role=DUB) == guard


def test_the_momo_case_is_vouched_but_its_card_cannot_hold_it():
    """The third measured case: the name guard no longer refuses it, and fits_card -- which R3
    leaves exactly as it was -- does. 27 characters in 1.34s is 20.1 cps against a 17 cps
    ceiling, and the original card had no slack to spend (22 chars = 16.4 cps)."""
    _name, orig, new, ref, dur, _guard = MOMO
    assert repair.dub_vouches(orig, new, ref, DUB)
    assert repair.rejecting_guard(orig, new, ref, dur, gl(), ref_role=DUB) == "fits_card"
    assert repair.rejecting_guard(orig, new, ref, dur, gl(), ref_role=TRANSLATION) == "invents_name"


def test_the_momo_case_is_admitted_when_the_card_can_actually_show_it():
    """Proof the remaining refusal is display geometry, not gate semantics: on a card long
    enough to read the line, the same dub-vouched repair is admitted."""
    _name, orig, new, ref, _dur, _guard = MOMO
    assert repair.rejecting_guard(orig, new, ref, 2.10, gl(), ref_role=DUB) is None
    assert repair.rejecting_guard(orig, new, ref, 2.10, gl(), ref_role=TRANSLATION) == "invents_name"


def test_a_dub_reference_vouches_only_words_it_actually_carries():
    """One added word the window lacks is enough to fall back to the classic gates."""
    orig, new = "for your", "For your information, honestly."
    ref = "For your information, the curse is real."
    assert not repair.dub_vouches(orig, new, ref, DUB)
    assert repair.rejecting_guard(orig, new, ref, 2.5, gl(), ref_role=DUB) == "length_ratio"


def test_dub_vouches_needs_an_added_word():
    """Nothing ADDED is nothing vouched: the rule speaks only about words the repair adds."""
    assert repair.dub_vouches("for your", "for your", "for your information", DUB) is False
    assert repair.dub_vouches("Miss Momoa, that's it.", "Miss, that's it.", "Momo Ayase", DUB) is False
    # A respelled name IS an added whole token ('momo'), so the dub window can vouch for it --
    # the same limit borrowed_from_ref has, and why the name guards still judge substitutions.
    assert repair.dub_vouches("Miss Momoa", "Miss Momo", "Momo is here", DUB)


def test_the_generous_cap_is_still_a_cap():
    orig, new = "hi", "hi there my friend buddy, the whole long line"
    ref = new  # every added word is attested -- only the ratio ceiling can stop it
    assert len(new) / len(orig) > repair.DUB_VOUCHED_MAX_RATIO
    assert repair.rejecting_guard(orig, new, ref, 6.0, gl(), ref_role=DUB) == "length_ratio"


def test_an_all_vouched_repair_still_cannot_collapse_the_line():
    orig = "This is a long line of dialogue that the card really does need to keep."
    new = "hi there"
    assert repair.dub_vouches(orig, new, "hi there", DUB)
    assert repair.rejecting_guard(orig, new, "hi there", 6.0, gl(), ref_role=DUB) == "length_ratio"


def test_ref_borrow_is_skipped_only_for_an_all_vouched_dub_repair():
    """The deliberate inversion: the same lift is a leak from a translation and evidence from
    the dub. Both directions pinned, so neither silently becomes the other."""
    orig, new, ref = "Enough of that, idiots!", "enough of that, hold it brats!", "hold it brats"
    assert len(repair.borrowed_from_ref(orig, new, ref)) >= repair.MAX_REF_BORROW
    assert repair.rejecting_guard(orig, new, ref, 6.0, gl(), ref_role=TRANSLATION) == "ref_borrow"
    assert repair.rejecting_guard(orig, new, ref, 6.0, gl(), ref_role=DUB) is None


def test_the_invents_name_exemption_is_dub_only_and_per_core():
    orig, new = "Miss Momoa, that's it.", "Miss Momo Ayase, that's it."
    assert repair.invents_name(orig, new, gl())
    assert not repair.invents_name(orig, new, gl(), repair.dub_name_exemption("Momo Ayase is here", DUB))
    assert repair.dub_name_exemption("Momo Ayase is here", TRANSLATION) is None


# Pairs drawn from the guard's measured territory, with the classic verdict each one had.
# The translation-role pass must reproduce every one of them -- that is the regression count.
SAMPLE = [
    ("Enough of that, idiots!", "enough of that, hold it brats!", "hold it brats", 6.0, "ref_borrow"),
    ("for your", "For your information,", "For your information, sir.", 2.5, "length_ratio"),
    ("Miss Momoa, that's it.", "Miss Momo Ayase, that's it.", "Miss Momo Ayase, that's it.", 1.34, "invents_name"),
    ("Spondum drew his blade.", "Spandam drew his blade.", "Spandam drew his blade, sneering.", 6.0, "invents_name"),
    ("He's just a reindeer with a blue nose, that's all.", "He's just a reindeer with a blue nose. That's all.", "", 6.0, None),
    ("a" * 40, "b" * 58, "", 3.0, "fits_card"),
    ("jester", "Dester", "the jester laughed", 6.0, "invents_name"),
]


def test_translation_role_reproduces_every_classic_verdict():
    for orig, new, ref, dur, guard in SAMPLE:
        classic = repair.rejecting_guard(orig, new, ref, dur, gl())
        translation = repair.rejecting_guard(orig, new, ref, dur, gl(), ref_role=TRANSLATION)
        assert classic == guard
        assert translation == classic
    moved = [repair.rejecting_guard(o, n, r, d, gl(), ref_role=TRANSLATION) for o, n, r, d, _ in SAMPLE]
    assert moved.count(None) == 1 and len(moved) == 7  # one admitted repair, six refusals, unchanged


def test_dub_role_only_moves_the_vouched_pairs():
    """The relaxation's blast radius, as a counter: under a dub reference only the four
    proposals the window attests change verdict (three become admissible, the Momo one moves
    from the name guard to the card gate); nothing else moves."""
    moved = []
    for o, n, r, d, g in SAMPLE:
        got = repair.rejecting_guard(o, n, r, d, gl(), ref_role=DUB)
        if got != g:
            moved.append((g, got))
    assert moved == [("ref_borrow", None), ("length_ratio", None), ("invents_name", "fits_card"), ("invents_name", None)]
    assert all(
        repair.dub_vouches(o, n, r, DUB)
        for o, n, r, d, g in SAMPLE
        if repair.rejecting_guard(o, n, r, d, gl(), ref_role=DUB) != g
    )


def _write_conf(conf_path, srt_path, rows):
    open(srt_path, "w").close()
    store_seed.conf(conf_path, rows)


def _episode(tmp_path, monkeypatch, stem, role, cue_text, out):
    conf_path = stem + repair.CONF_SUFFIX
    _write_conf(
        conf_path,
        stem + repair.SRT_SUFFIX,
        [{"start": 0.0, "end": 1.34, "text": "What are you looking at, doofus?", "avg_logprob": -0.6, "no_speech_prob": 0.1}],
    )
    g = gl()
    monkeypatch.setattr(repair, "find_video", lambda s: str(tmp_path / "ep_r3.mkv"))
    monkeypatch.setattr(repair, "glossary_for", lambda video: g)
    monkeypatch.setattr(repair, "reference_tracks", repair_ref_stub.stub([(0.0, 1.34, cue_text)], role=role))
    monkeypatch.setattr(repair, "llm", lambda prompt, model=None: out)
    return conf_path


def test_process_applies_the_repair_when_the_dub_track_vouches_it(tmp_path, monkeypatch):
    """End-to-end plumbing: the role R2 records reaches the gate, so the DDD repair ships."""
    stem = str(tmp_path / "ep_r3_dub")
    conf_path = _episode(tmp_path, monkeypatch, stem, DUB, "What're you lookin' at, doofus?", "What're you lookin' at, doofus?")
    assert repair.process(conf_path) == "repaired"
    applied = [e for e in unresolved.items(stem) if e.get("stage") == "repair_applied"]
    assert [e["ref_role"] for e in applied] == [DUB]


def test_process_still_queues_the_same_repair_under_a_translation_track(tmp_path, monkeypatch):
    stem = str(tmp_path / "ep_r3_trans")
    line = "What're you lookin' at, doofus?"
    conf_path = _episode(tmp_path, monkeypatch, stem, TRANSLATION, line, line)
    assert repair.process(conf_path) == "repaired"
    reasons = [e.get("reason") for e in unresolved.items(stem)]
    assert reasons == ["rejected_name_invented"]

