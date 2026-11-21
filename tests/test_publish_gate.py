"""publish_gate: a sidecar that is not a dubtitle must not pass.

These tests are the shape of the November 2026 NGE failure: 100% unpunctuated cards,
`Oscar` for Asuka, and an E23 that stopped at 11:26 of a 24:30 episode while every stage
recorded "ok".
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.pipeline import publish_gate as g


def srt(cues):
    out = []
    for i, (a, b, text) in enumerate(cues, 1):
        out.append(f"{i}\n00:00:{int(a):02d},000 --> 00:00:{int(b):02d},000\n{text}\n")
    return "\n".join(out)


GOOD = [
    (1, 4, "The mitosis process in the left arm\nof Unit 2 has cleared the requirement."),
    (5, 8, "Development of necrosis is\ncurrently under 0.45%."),
    (9, 12, "The Evas were created from the angel\nknown as Adam."),
    (13, 16, "I hate the Evas. Is our revenge worth this?"),
    (17, 20, "Major Katsuragi! They've started\nconstruction on all the Evas."),
]


def test_a_clean_sidecar_passes():
    cues = g.parse_srt(srt(GOOD))
    assert g.evaluate(cues, duration=21) == []


def test_unpunctuated_cards_fail():
    cues = g.parse_srt(
        srt(
            [
                (1, 4, "The direct cause you mean was the mental breakdown."),
                (5, 8, "the result of the contact yes"),
                (9, 12, "that doll as if it were her real daughter"),
            ]
        )
    )
    fails = g.evaluate(cues, duration=13)
    assert any("end with punctuation" in f for f in fails)
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
    cues = g.parse_srt(
        srt([(1, 6, "All the Evas were created from the angel known as Adam and we are employing the same forces")])
    )
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
    p.write_text(
        json.dumps(
            {
                "hard_fixes": {
                    "ikari": "Ikari",
                    "at field": "AT Field",
                    "asuka langley": "Asuka Langley Soryu",
                    "oscar": "Asuka",
                    "sink ratio": "sync ratio",
                    "ava": "Eva",
                }
            }
        )
    )
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
    cues = g.parse_srt(
        srt(
            [
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
            ]
        )
    )
    assert g.evaluate(cues, duration=30) == []


def test_a_raw_transcript_fails_the_punctuation_floor():
    """The old NGE shape: every card unpunctuated and lowercase."""
    cues = g.parse_srt(
        srt(
            [
                (1, 4, "the direct cause you mean was the mental breakdown"),
                (5, 8, "the result of the contact yes"),
                (9, 12, "oscar sink ratio has dropped an entire eight points"),
            ]
        )
    )
    fails = g.evaluate(cues, duration=13, mishears=["oscar", "sink ratio"])
    assert any("end with punctuation" in f for f in fails)
    assert any("mishear" in f for f in fails)


def test_a_card_cut_at_a_comma_is_punctuated_text():
    """A card ending on a comma continues a sentence; it is not a bare transcript line.

    86 Eighty-Six S01E01 is cut this way throughout ("Gotta say," / "anyone out"), and
    counting only terminal marks read those cards as raw ASR and parked the episode.
    """
    cues = g.parse_srt(
        srt(
            [
                (1, 3, "Gotta say,"),
                (4, 6, "anyone out by day"),
                (7, 9, "can see the clear skies --"),
                (10, 12, "or not."),
            ]
        )
    )
    assert g.evaluate(cues, None) == [], g.evaluate(cues, None)


# --- the punctuation floor is a minimum, and it admits real split-heavy subtitles -----
#
# Measured 2026-11-21 over all 1,063 dubtitle artifacts in the live library, counting a card
# as punctuated when it ends with any punctuation mark (terminal or clause): the raw-era
# band tops out at 27.1% and the lowest real sidecar is 86 Eighty-Six S01E01 at 60.9%;
# Chainsaw Man S01E01 is 68.1%. The floor is a MINIMUM that separates subtitles from
# transcripts, not a quality bar -- a split sentence leaves its first card on a comma.

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "artifacts")


def test_the_live_chainsaw_man_episode_passes_the_punctuation_floor():
    """Real bytes: 171 of 251 cards end on punctuation. The floor is a minimum, not a bar
    every card must clear -- a split sentence leaves its first card bare by design."""
    text = open(os.path.join(FIXTURES, "csm_s01e01_dubtitles.ass"), encoding="utf-8-sig", errors="replace").read()
    cues = g.parse_ass(text, style="Dubtitles")
    ended = sum(1 for _, _, t in cues if t.rstrip().endswith(g.PUNCT))
    assert (len(cues), ended) == (251, 171)
    assert g.evaluate(cues, None) == [], g.evaluate(cues, None)


def test_the_live_86_eighty_six_episode_passes_legitimately():
    """The 2026-11-21 park: 198 of 325 cards (60.9%) end on punctuation, 13.4 marks per 100
    words, and a full regeneration reproduces it -- the text genuinely reads that way.

    Its only remaining finding is the layout one for the 58-character scream, which
    gate_fix wraps; the punctuation floor must not refuse the file at all.
    """
    text = open(os.path.join(FIXTURES, "e86_s01e01_dubtitles.ass"), encoding="utf-8-sig", errors="replace").read()
    cues = g.parse_ass(text, style="Dubtitles")
    ended = sum(1 for _, _, t in cues if t.rstrip().endswith(g.PUNCT))
    assert (len(cues), ended) == (325, 198)
    fails = g.evaluate(cues, None)
    assert not any("punctuation" in f for f in fails), fails
    assert fails == ["line of 58 chars (max 46)"], fails


def test_a_file_just_under_the_floor_still_fails():
    """Boundary: 50 of 100 ends pass, 49 of 100 fail. One card is the margin."""

    def cues_with(ended):
        return [(1.0 + i, 2.0 + i, "Ends here." if i < ended else "No terminal here") for i in range(100)]

    assert g.evaluate(cues_with(50), None) == [], g.evaluate(cues_with(50), None)
    fails = g.evaluate(cues_with(49), None)
    assert any("end with punctuation" in f for f in fails), fails


def test_the_live_zero_percent_raw_file_still_fails_the_floor():
    """Negative control: the 0.0%-era file must not be admitted by the lower line."""
    text = open(os.path.join(FIXTURES, "death_note_s01e10_raw.srt"), encoding="utf-8-sig", errors="replace").read()
    cues = g.parse_srt(text)
    ended = sum(1 for _, _, t in cues if t.rstrip().endswith(g.PUNCT))
    assert (len(cues), ended) == (262, 0)
    fails = g.evaluate(cues, None)
    assert any("0% of cards end with punctuation" in f for f in fails), fails


# --- song spans are not English dialogue ----------------------------------------------


def test_a_romaji_lyric_cannot_be_a_known_mishear():
    """Frieren S02E01's real ending: the show glossary carries "Furikaereba -> Frieren", so
    the lyric line "Furikaereba itsumo." was reported as a known mishear forever."""
    text = open(os.path.join(FIXTURES, "frieren_s02e01_song.dubtitles.ass"), encoding="utf-8-sig", errors="replace").read()
    cues = g.parse_ass(text, style="Dubtitles")
    assert "Furikaereba itsumo." in [t for _, _, t in cues]
    assert g.evaluate(cues, None, mishears=["Furikaereba", "Verbal"]) == []


def test_a_dialogue_mishear_outside_the_song_span_still_fails():
    """Negative control: excluding lyrics must not blind the mishear guard on speech."""
    cues = g.parse_srt(
        srt(
            [
                (1, 3, "Take the tram to the station."),
                (4, 6, "Furikaereba itsumo."),
                (7, 9, "Osoresu ni susume yo."),
                (10, 12, "Verbal said Furikaereba, apparently."),
            ]
        )
    )
    fails = g.evaluate(cues, None, mishears=["Furikaereba", "Verbal"])
    assert fails and fails[0].startswith("known mishear"), fails


def test_a_published_sidecar_is_found_by_its_delivery_name(tmp_path):
    """`stem.dub.en.ass` is what the library actually carries; the gate must audit it, not
    only the pipeline's internal `stem.eng.dubtitles.ass`."""
    stem = str(tmp_path / "ep")
    (tmp_path / "ep.dub.en.ass").write_text(
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:05.80,Dubtitles,,0,0,0,,The Evas were created.\\NFrom Adam.\n"
    )
    assert g.main([stem, "--duration", "6"]) == 0


def test_signs_and_lyrics_are_not_counted_as_dialogue(tmp_path):
    """A styled sidecar carries sign cues that run longer than any dialogue card. Counting
    them fails a good file (and did, on E22's 8.1 s title card)."""
    stem = str(tmp_path / "ep")
    (tmp_path / "ep.dub.en.ass").write_text(
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Style: Dubtitles,Arial,60,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2.8,0,2,60,60,49,1\n"
        "Dialogue: 0,0:00:01.00,0:00:05.80,Dubtitles,,0,0,0,,The Evas were created.\\NFrom Adam.\n"
        "Dialogue: 0,0:00:06.00,0:00:14.00,Signs,,0,0,0,,TITLE CARD THAT RUNS LONG\n"
    )
    assert g.main([stem, "--duration", "6"]) == 0
