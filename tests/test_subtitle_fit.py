"""subtitle_fit: may a downloaded track be trusted as a reference for this video?

The numbers in the module docstring come from this fixture set, so the tests assert the
same two directions on the same bytes: a real track agrees with itself (and with itself
moved), and two real tracks for *different* episodes agree on almost nothing. The
cross-episode pair used here measured 12.0 % -- with 24.7 % the worst of the nine pairs
this box has -- so the 0.60 floor is not a guess.

The overrun/coverage cases are constructed rather than captured: no artifact in the tree
is the wrong cut of the episode it sits next to, which is exactly the file the check
exists to refuse.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.core import subtitle_fit

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "artifacts")


def _cues(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8-sig") as fh:
        return subtitle_fit.parse_cues(fh.read())


# --- reading the two shapes a reference arrives in -----------------------------------


def test_parse_ass_artifact():
    cues = _cues("frieren_s02e01_song.dubtitles.ass")
    assert len(cues) > 300
    assert cues[0][0] > 0
    assert all(end > start for start, end in cues)


def test_parse_srt_artifact():
    cues = _cues("cyberpunk_s02e01_dubtitles.srt")
    assert len(cues) > 300
    assert all(end > start for start, end in cues)


def test_parse_ignores_non_dialogue_ass_events():
    text = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:03.50,Dubtitles,Hello.\n"
        "Comment: 0,0:00:05.00,0:00:06.00,Dubtitles,not a cue\n"
        "Dialogue: 0,0:00:07.00,0:00:09.00,Dubtitles,Again.\n"
    )
    assert subtitle_fit.parse_cues(text) == [(1.0, 3.5), (7.0, 9.0)]


# --- agreement: the positive direction ------------------------------------------------


def test_a_track_agrees_with_itself():
    cues = _cues("csm_s01e01_dubtitles.ass")
    fit = subtitle_fit.agreement(cues, cues)
    assert fit.share == 1.0
    assert fit.offset == 0.0
    assert fit.usable and fit.same_cut


def test_a_shifted_track_is_recovered_exactly():
    cues = _cues("toradora_s01e18_ryuuji.dubtitles.ass")
    for seconds in (0.4, 6.0, -11.5, 37.0):
        fit = subtitle_fit.agreement(cues, subtitle_fit.shift(cues, seconds))
        assert fit.share == 1.0
        assert abs(fit.offset - seconds) < 0.01


def test_align_undoes_the_delay():
    witness = _cues("e86_s01e01_dubtitles.ass")
    moved = subtitle_fit.shift(witness, 6.0)
    aligned, fit = subtitle_fit.align(moved, witness)
    assert abs(fit.offset - 6.0) < 0.01
    assert abs(aligned[0][0] - witness[0][0]) < 0.01
    assert subtitle_fit.agreement(witness, aligned).offset == 0.0


# --- agreement: the negative direction ------------------------------------------------


def test_different_episodes_disagree():
    left = _cues("frieren_s02e01_song.dubtitles.ass")
    right = _cues("csm_s01e01_dubtitles.ass")
    fit = subtitle_fit.agreement(left, right)
    # 12.0 % measured on these two artifacts; 24.7 % was the worst of nine real pairs.
    assert fit.share < subtitle_fit.MIN_SHARE / 2
    assert not fit.usable


def test_align_leaves_a_disagreeing_track_alone():
    left = _cues("frieren_s02e01_song.dubtitles.ass")
    right = _cues("csm_s01e01_dubtitles.ass")
    out, fit = subtitle_fit.align(right, left)
    assert out == right
    assert not fit.usable


def test_empty_input_is_never_usable():
    fit = subtitle_fit.agreement([], [])
    assert fit.share == 0.0 and not fit.usable


def test_a_stub_cannot_pass_on_coincidence():
    # The 5-cue slice is the trap: it measured share 0.60 against a 1,702-cue episode of
    # another show purely because three of its five starts landed on something.
    witness = _cues("e86_s01e01_dubtitles.ass")
    stub = _cues("toradora_s01e24_ryu.dubtitles.ass")
    assert len(stub) < subtitle_fit.MIN_CUES
    fit = subtitle_fit.agreement(witness, stub)
    assert not fit.usable
    assert not fit.same_cut


# --- structure: overrun and coverage --------------------------------------------------


def _cues_spanning(start, end, step, count):
    return [(start + i * step, start + i * step + step * 0.8) for i in range(count)]


def test_overrun_flags_a_double_length_episode():
    # A 2,738 s track against a 1,420 s TV episode: the incident this check exists for.
    cues = _cues_spanning(1.0, 2738.0, 4.0, 680)
    found = subtitle_fit.defects(cues, duration=1420.0)
    assert any(item.startswith("overrun:") for item in found)


def test_a_real_track_has_no_structural_defect():
    cues = _cues("e86_s01e01_dubtitles.ass")
    assert subtitle_fit.defects(cues, duration=cues[-1][1] + 5) == []


def test_coverage_flags_a_fragment():
    cues = [(1.0, 2.0), (10.0, 11.0), (20.0, 21.0)]
    assert any(item.startswith("coverage:") for item in subtitle_fit.defects(cues, duration=1420.0))
    assert subtitle_fit.defects(cues, duration=1420.0, forced=True) == []


def test_unknown_duration_skips_structure():
    cues = [(1.0, 2.0)]
    assert subtitle_fit.defects(cues, duration=None) == []
    assert subtitle_fit.defects([], duration=None) == ["empty"]


# --- the admission verdict ------------------------------------------------------------


def test_check_admits_the_same_cut():
    witness = _cues("e86_s01e01_dubtitles.ass")
    verdict = subtitle_fit.check(subtitle_fit.shift(witness, 4.0), duration=witness[-1][1] + 5, witness=witness)
    assert verdict.ok
    assert "offset:+4.0s" in verdict.reasons


def test_check_refuses_another_episode():
    witness = _cues("frieren_s02e01_song.dubtitles.ass")
    other = _cues("csm_s01e01_dubtitles.ass")
    verdict = subtitle_fit.check(other, duration=1440.0, witness=witness)
    assert not verdict.ok
    assert any(item.startswith("disagrees:") for item in verdict.reasons)


def test_check_describe_is_json_safe():
    witness = _cues("csm_s01e01_dubtitles.ass")
    payload = subtitle_fit.check(witness, duration=1440.0, witness=witness).describe()
    assert payload["ok"] is True
    assert payload["fit"]["share"] == 1.0
