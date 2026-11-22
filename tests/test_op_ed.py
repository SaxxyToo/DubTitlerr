"""op_ed: naming the opening/ending slot without trusting the style name.

Three real artifacts drive the assertions:

  * e86's presentation file styles its *ending* karaoke ``Opening`` (1,326-1,393 s of a
    1,417 s episode), so the region must come back with ``conflict`` and the tail position;
  * toradora's ending is three per-language styles (``ED English``/``ED Kanji``/``ED
    Romaji``) spanning 130.6 s -- an exact-match style regex misses it and a 120 s ceiling
    rejects it;
  * csm's dubtitles have no styles at all and no songs, and the density pass must stay quiet.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.pipeline import op_ed

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "artifacts")


def _events(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8-sig", errors="replace") as fh:
        return op_ed.parse_events(fh.read())


def _duration(events):
    return max(end for _start, end, _style in events)


# --- style evidence on real artifacts -------------------------------------------------


def test_styled_ending_is_found_and_its_length_survives():
    events = _events("toradora_s01e24_presentation.ass")
    regions = op_ed.detect(events, duration=_duration(events))
    endings = [region for region in regions if region.kind == "ed"]
    assert len(endings) == 1
    ending = endings[0]
    assert abs(ending.start - 1214.94) < 0.01
    assert abs(ending.end - 1345.47) < 0.01
    assert ending.length > 120  # the ceiling a naive port would impose
    assert ending.position == "tail"
    assert not ending.conflict
    assert "ED Romaji" in ending.evidence


def test_a_misnamed_style_is_reported_not_relabelled():
    events = _events("e86_s01e01_presentation.ass")
    regions = op_ed.detect(events, duration=_duration(events))
    assert len(regions) == 1
    region = regions[0]
    assert region.kind == "op"  # the style says Opening; that is the prior
    assert region.position == "tail"  # the slot says ending; that is the evidence
    assert region.conflict
    assert region.start > 1300


def test_styles_beat_density_when_both_could_answer():
    events = _events("toradora_s01e24_presentation.ass")
    regions = op_ed.detect(events, duration=_duration(events))
    assert all("style:" in region.evidence for region in regions)


# --- the negative case ----------------------------------------------------------------


def test_plain_dubtitles_produce_no_region():
    events = _events("csm_s01e01_dubtitles.ass")
    assert op_ed.detect(events, duration=_duration(events)) == []


def test_no_duration_means_no_density_guess():
    events = [(1.0, 2.0, None)] * 30
    assert op_ed.detect(events) == []


# --- the density pass -----------------------------------------------------------------


def test_dense_typeset_run_at_the_head_is_an_opening():
    # Typeset density, not dialogue density: four events a second inside the song, one
    # every 4 s everywhere else -- the shape an unstyled karaoke ASS actually has.
    song = [(20.0 + i * 0.25, 20.25 + i * 0.25, None) for i in range(280)]
    dialogue = [(400.0 + 4 * i, 402.0 + 4 * i, None) for i in range(30)]
    regions = op_ed.detect(song + dialogue, duration=1440.0)
    assert [region.kind for region in regions] == ["op"]
    assert regions[0].evidence.startswith("density:")


def test_a_flat_srt_op_is_not_guessed_from_density():
    # One cue per line is dialogue density; position alone must not manufacture a region.
    events = [(4.0 * i, 4.0 * i + 2.0, None) for i in range(300)]
    assert op_ed.detect(events, duration=1440.0) == []


# --- parsing --------------------------------------------------------------------------


def test_parse_srt_events():
    text = "1\n00:00:01,000 --> 00:00:03,000\nHello.\n\n2\n00:00:07,500 --> 00:00:09,250\nAgain.\n"
    assert op_ed.parse_srt_events(text) == [(1.0, 3.0, None), (7.5, 9.25, None)]


def test_describe_is_json_safe():
    events = _events("toradora_s01e24_presentation.ass")
    payload = op_ed.describe(events, duration=_duration(events))
    assert payload[0]["kind"] == "ed"
    assert isinstance(payload[0]["length"], float)
