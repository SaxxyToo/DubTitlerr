"""publish_gate: a sidecar that is not a dubtitle must not pass.

These tests are the shape of the November 2026 NGE failure: 100% unpunctuated cards,
`Oscar` for Asuka, and an E23 that stopped at 11:26 of a 24:30 episode while every stage
recorded "ok".
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import publish_gate as g


def srt(cues):
    out = []
    for i, (a, b, text) in enumerate(cues, 1):
        out.append("%d\n00:00:%02d,000 --> 00:00:%02d,000\n%s\n" % (i, int(a), int(b), text))
    return "\n".join(out)


GOOD = [
    (1, 4, "The mitosis process in the left arm\nof Unit 2 has cleared the requirement."),
    (5, 8, "Development of necrosis is\ncurrently under 0.45%."),
    (9, 12, "The Evas were created from the angel\nknown as Adam."),
    (13, 16, "I hate the Evas. Is our revenge worth this?"),
    (17, 20, "Major Katsuragi! They've started\nconstruction on all the Evas."),
]


def test_a_clean_sidecar_passes():
    path, cues = "x", g.parse_srt(srt(GOOD))
    assert g.evaluate(cues, duration=21) == []


def test_unpunctuated_cards_fail():
    cues = g.parse_srt(srt([(1, 4, "The direct cause you mean was the mental breakdown."),
                            (5, 8, "the result of the contact yes"),
                            (9, 12, "that doll as if it were her real daughter")]))
    fails = g.evaluate(cues, duration=13)
    assert any("sentence punctuation" in f for f in fails)
    assert any("orphan" in f for f in fails)


def test_a_truncated_sidecar_fails_coverage():
    cues = g.parse_srt(srt([(1, 4, "An Angel is approaching."), (5, 8, "We will continue this later.")]))
    fails = g.evaluate(cues, duration=1452)
    assert any("coverage" in f for f in fails)


def test_a_known_mishear_fails_even_when_readable():
    cues = g.parse_srt(srt(GOOD + [(21, 24, "Oscar's sync ratio has dropped eight points.")]))
    fails = g.evaluate(cues, duration=25, mishears=["oscar", "asakura", "sink ratio"])
    assert any("mishear" in f for f in fails)


def test_a_long_line_fails():
    cues = g.parse_srt(srt([(1, 6, "All the Evas were created from the angel known as Adam and we are employing the same forces")]))
    fails = g.evaluate(cues, duration=7)
    assert any("chars" in f for f in fails)


def test_an_overlong_card_fails():
    cues = g.parse_srt(srt([(1, 11, "We will continue this later."), (12, 14, "Yes.")]))
    fails = g.evaluate(cues, duration=15)
    assert any("over 7.5s" in f for f in fails)


def test_no_cues_fails():
    assert g.evaluate([], duration=1400) == ["no cues"]


def test_case_only_fixes_and_expansions_are_not_mishears(tmp_path):
    """`ikari -> Ikari` is normalisation and `asuka langley -> Asuka Langley Soryu` is an
    expansion; neither means the ASR heard a wrong word."""
    p = tmp_path / "show.json"
    p.write_text(json.dumps({"hard_fixes": {
        "ikari": "Ikari",
        "at field": "AT Field",
        "asuka langley": "Asuka Langley Soryu",
        "oscar": "Asuka",
        "sink ratio": "sync ratio",
        "ava": "Eva",
    }}))
    words = g.glossary_mishears(str(p))
    assert "oscar" in words
    assert "sink ratio" in words
    assert "ava" in words
    assert "ikari" not in words
    assert "at field" not in words
    assert "asuka langley" not in words


def test_ass_parses_line_breaks_and_ignores_drawing_commands():
    ass = """[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:04.00,Dubtitles,,0,0,0,,The Evas were created\\Nfrom the angel known as Adam.
Dialogue: 0,0:00:05.00,0:00:08.00,Signs,,0,0,0,,{\\pos(10,10)}TITLE CARD
"""
    cues = g.parse_ass(ass, style="Dubtitles")
    assert len(cues) == 1
    assert cues[0][2] == "The Evas were created\nfrom the angel known as Adam."


def test_cli_exit_codes(tmp_path):
    stem = str(tmp_path / "ep")
    (tmp_path / "ep.eng.dubtitles.srt").write_text(srt(GOOD))
    assert g.main([stem, "--duration", "21"]) == 0
    (tmp_path / "ep.eng.dubtitles.srt").write_text(srt([(1, 4, "no punctuation here at all")]))
    assert g.main([stem, "--duration", "1452"]) == 1


def test_sentence_splits_are_not_punctuation_failures():
    """7 of 10 cards end a sentence; the other three are the first half of a split sentence.
    That is normal subtitling and must pass the floor."""
    cues = g.parse_srt(srt([
        (1, 3, "The Evas were created from the"),
        (4, 6, "angel known as Adam."),
        (7, 9, "I hate the Evas."),
        (10, 12, "Is our revenge worth this?"),
        (13, 15, "Major Katsuragi said the"),
        (16, 18, "construction is behind schedule."),
        (19, 21, "We have lost two Evas."),
        (22, 24, "Two more are damaged."),
        (25, 27, "The reserve force is"),
        (28, 30, "not ready yet."),
    ]))
    assert g.evaluate(cues, duration=30) == []


def test_a_raw_transcript_fails_the_punctuation_floor():
    """The old NGE shape: every card unpunctuated and lowercase."""
    cues = g.parse_srt(srt([
        (1, 4, "the direct cause you mean was the mental breakdown"),
        (5, 8, "the result of the contact yes"),
        (9, 12, "oscar sink ratio has dropped an entire eight points"),
    ]))
    fails = g.evaluate(cues, duration=13, mishears=["oscar", "sink ratio"])
    assert any("sentence punctuation" in f for f in fails)
    assert any("mishear" in f for f in fails)
