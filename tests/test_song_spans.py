"""song_spans: recognising a sung romaji span, and *not* recognising terse English.

The two failure directions both have real bytes: Frieren S02E01's ending song is seven
cards of pseudo-romaji the English ASR produced from Japanese singing, and 86 Eighty-Six
S01E01's military radio chatter ("Confirming ID.", "Synchronization target.") is terse
English with no function word in it at all. A detector that only asks "is there an English
word here?" swallows both, and the second one hides name-bearing dialogue -- exactly where
a mishear lives.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.core import song_spans
from dubtitlerr.pipeline import publish_gate

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "artifacts")


def _cues(name, style="Dubtitles"):
    text = open(os.path.join(FIXTURES, name), encoding="utf-8-sig", errors="replace").read()
    if name.endswith(".ass"):
        return publish_gate.parse_ass(text, style=style)
    return publish_gate.parse_srt(text)


# --- what a lyric looks like ----------------------------------------------------------


def test_a_romaji_line_is_lyric_shaped():
    for line in (
        "Osoresu ni susume yo.",
        "Furikaereba itsumo.",
        "Anata ga warateta.",
        "Dokogi demo ikeru hane wo.",
        "michi ni ite mo dokka.",
    ):
        assert song_spans.is_lyric_shaped(line), line


def test_a_lyric_line_with_an_english_particle_is_covered_by_its_region():
    """ "Anata ni sazuketa no donna yami mo." has "no" in it, which is English too.

    A single token can be both, so card-level classification cannot settle it -- the region
    does: the line sits inside the ending-song span and is handled with it.
    """
    cues = _cues("frieren_s02e01_song.dubtitles.ass")
    line = [c for c in cues if c[2] == "Anata ni sazuketa no donna yami mo."]
    assert line, "fixture changed"
    assert not song_spans.is_lyric_shaped(line[0][2])
    assert any(s <= line[0][0] < e for s, e in song_spans.spans(cues))


def test_terse_english_dialogue_is_not_lyric_shaped():
    """These are 86 Eighty-Six's and Cyberpunk's real lines: no function word, all English."""
    for line in (
        "Confirming ID.",
        "Synchronization target.",
        "Weak Kingsley.",
        "Major Vladilena Milizé.",
        "Pleiades. Processor.",
        "*crickets chirping*",
        "Pararate. Activate.",
    ):
        assert not song_spans.is_lyric_shaped(line), line


def test_one_word_and_number_only_cards_are_never_lyrics():
    assert not song_spans.is_lyric_shaped("Emily,")
    assert not song_spans.is_lyric_shaped("42")
    assert not song_spans.is_lyric_shaped("*chirping*")


def test_a_card_with_an_english_function_word_is_dialogue():
    assert not song_spans.is_lyric_shaped("Anata ni sazuketa no")  # "no" is English too
    assert not song_spans.is_lyric_shaped("Holy brave.")  # "brave" spells a br cluster


# --- what a song span looks like ------------------------------------------------------


def test_the_live_frieren_ending_song_is_one_region_from_first_to_last_lyric():
    """Real bytes: the ED runs 22:22-23:29 and is cut as two clusters of romaji cards."""
    cues = _cues("frieren_s02e01_song.dubtitles.ass")
    found = song_spans.spans(cues)
    assert [(round(s, 2), round(e, 2)) for s, e in found] == [(1342.05, 1351.45), (1361.37, 1408.99)]
    assert [d["cards"] for d in song_spans.describe(cues)] == [2, 13]


def test_the_english_adaptation_lines_inside_the_span_are_covered():
    """ "You gave me holy brave." sits between two romaji lines; the span covers it."""
    cues = _cues("frieren_s02e01_song.dubtitles.ass")
    inside = [t for s, e, t in cues if 1388.35 <= s < 1408.99]
    assert "You gave me holy brave." in inside
    assert "Furikaereba itsumo." in inside
    idx = song_spans.indexes(cues)
    texts = {i: cues[i][2] for i in idx}
    assert "You gave me holy brave." in texts.values()


def test_the_live_86_and_cyberpunk_files_have_no_song_span():
    """No false positives: terse English dialogue is left alone in both directions."""
    assert song_spans.spans(_cues("e86_s01e01_dubtitles.ass")) == []
    assert song_spans.spans(_cues("cyberpunk_s02e01_dubtitles.srt")) == []


def test_one_lyric_shaped_card_is_not_a_span():
    cues = [(1.0, 2.0, "Take the tram."), (3.0, 4.0, "Osoresu ni susume yo."), (5.0, 6.0, "And back again.")]
    assert song_spans.is_lyric_shaped(cues[1][2])
    assert song_spans.spans(cues) == []


def test_lyrics_further_apart_than_the_merge_gap_are_separate_groups():
    cues = [(1.0, 2.0, "Osoresu ni susume yo."), (2.0 + song_spans.SONG_GAP + 1.0, 20.0, "Furikaereba itsumo.")]
    assert song_spans.spans(cues) == []


def test_a_region_longer_than_the_span_cap_is_not_a_song():
    """A chained run of lyric cards is capped: three minutes is not a song, it is a misfire."""
    chained = [(float(i * 5), float(i * 5) + 4.0, "Osoresu ni susume yo.") for i in range(40)]
    pad = [(1000.0 + i, 1001.0 + i, "And then we went home.") for i in range(90)]
    assert song_spans.spans(chained + pad) == []
    song = chained[:10] + pad
    assert song_spans.spans(song), "the same shape under the cap is still a song"


def test_a_region_covering_a_quarter_of_the_file_is_not_a_song():
    """A majority-foreign file is a broken transcript, not an episode with an OP/ED.

    Excluding it from the dialogue checks would hand the gate a free pass, so the span
    budget is a share of the file: 40 lyric cards among 71 cards is not a song.
    """
    block = [(float(i), float(i) + 1.0, "Linya, linya") for i in range(40)]
    dialogue = [(100.0 + i, 101.0 + i, "Ends here.") for i in range(30)]
    cues = dialogue + block + [(200.0, 203.0, "Furikaereba itsumo.")]
    assert song_spans.spans(cues) == []
