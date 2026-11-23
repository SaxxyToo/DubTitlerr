"""One track decision, two call sites: the preview and the pipeline must agree.

The external review (2026-11-20) found them disagreeing on an episode the pipeline itself
had produced: `und` audio titled "English Dub" is picked by `generate.eng_audio_index`, but
the review server's audio preview accepted English TAGS only and answered 422 "no English
dialogue audio track". The rules now live in `dubtitlerr/core/media_select.py`, and this
module proves both call sites answer with it.

The table is driven by the committed ffprobe JSON fixtures (real stream shapes from the
review) and covers the five cases acceptance names: eng-tagged, und+title, dual-audio,
commentary, and zxx.
"""

import json
import pathlib
import threading
from types import SimpleNamespace

import pytest
import store_seed

from dubtitlerr.core import media_select, unresolved
from dubtitlerr.pipeline import generate
from dubtitlerr.review import review_server as rs

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ffprobe"

# (fixture, expected stream index, expected reason).
#   eng_tagged_dual_audio  -- eng-tagged  (JPN + ENG + SPA mux)
#   und_english_dub_only   -- und + title (the shape finding #2 is about)
#   eng_commentary_plus_dub-- commentary excluded, the other eng track wins
#   und_zxx_and_untagged   -- zxx (no linguistic content) is never English evidence
#   the rest keep the P19 title rules pinned.
CASES = [
    ("eng_tagged_dual_audio", 2, media_select.REASON_ENG_TAG),
    ("und_english_dub_only", 2, media_select.REASON_TITLE),
    ("eng_commentary_plus_dub", 2, media_select.REASON_ENG_TAG),
    ("und_real_release_titles", 2, media_select.REASON_TITLE),
    ("und_plain_dub", 2, media_select.REASON_TITLE),
    ("und_spanish_and_english_dub", 2, media_select.REASON_TITLE),
    ("und_spanish_dub", None, media_select.REASON_NO_ENGLISH),
    ("und_german_dubbed", None, media_select.REASON_NO_ENGLISH),
    ("und_japanese_eng_subs_stereo", None, media_select.REASON_NO_ENGLISH),
    ("und_zxx_and_untagged", None, media_select.REASON_NO_ENGLISH),
]

# The acceptance cases that must also agree through the review server's preview.
PARITY = {
    "eng_tagged_dual_audio",
    "und_english_dub_only",
    "eng_commentary_plus_dub",
    "und_zxx_and_untagged",
}


def _streams(fixture):
    return json.loads((FIXTURES / f"{fixture}.json").read_text())["streams"]


@pytest.fixture(autouse=True)
def _clean_knobs(monkeypatch):
    monkeypatch.delenv("UND_AUDIO_AS_DUB", raising=False)
    monkeypatch.setenv("REQUIRE_ENG", "1")


@pytest.mark.parametrize("fixture, expected, reason", CASES)
def test_selector_picks_the_reviewed_track(fixture, expected, reason):
    pick = media_select.select_english_audio(_streams(fixture))
    assert (pick.index, pick.reason) == (expected, reason)


def test_a_bare_dub_title_does_not_beat_an_explicit_english_one_by_position():
    streams = [
        {"index": 1, "tags": {"language": "und", "title": "Dub"}},
        {"index": 2, "tags": {"language": "und", "title": "English Dub"}},
    ]
    pick = media_select.select_english_audio(streams)
    assert pick.index == 2
    assert pick.skipped == ("index 1 'Dub'",)


def test_the_lone_untagged_opt_in_still_needs_one_untagged_track():
    lone = [{"index": 4, "tags": {"language": "und", "title": "2.0"}}]
    assert media_select.select_english_audio(lone, und_override=False).index is None
    assert media_select.select_english_audio(lone, und_override=True).index == 4
    two = lone + [{"index": 5, "tags": {"language": "", "title": "5.1"}}]
    assert media_select.select_english_audio(two, und_override=True).index is None


def test_a_lone_commentary_track_is_never_taken_as_the_dub():
    commentary = [{"index": 1, "tags": {"language": "und", "title": "English Commentary"}}]
    assert media_select.select_english_audio(commentary, und_override=True).index is None


def test_require_eng_off_is_the_blunt_override():
    streams = [{"index": 3, "tags": {"language": "jpn", "title": "Japanese 2.0"}}]
    pick = media_select.select_english_audio(streams, require_override=False)
    assert (pick.index, pick.reason) == (3, media_select.REASON_FIRST_STREAM)


def test_no_audio_streams_is_reported_as_such():
    pick = media_select.select_english_audio([{"index": 0, "codec_type": "video"}])
    assert (pick.index, pick.reason) == (None, media_select.REASON_NO_AUDIO)


# --- preview <-> pipeline parity ---------------------------------------------------------


@pytest.fixture
def clip(tmp_path, monkeypatch):
    """The minimum preview_audio needs: a known stem, a card, a review entry and media."""
    stem = str(tmp_path / "episode")
    pathlib.Path(stem + ".mkv").write_bytes(b"fixture")
    store_seed.conf(stem + rs.CONF_SUFFIX, [{"text": "Hello.", "start": 3.0, "end": 200.0}])
    unresolved.record(stem, "repair_applied", "accepted", original_text="Hello.", proposed_text="Hello!")
    row = unresolved.items(stem)[0]
    monkeypatch.setattr(rs, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(rs, "_resolve", lambda value: stem if value == stem else None)
    monkeypatch.setattr(rs, "_AUDIO_SLOTS", threading.BoundedSemaphore(2))
    return {"stem": stem, "index": "0", "id": rs.entry_id(row)}


def _fake_ffprobe(fixture, maps):
    payload = (FIXTURES / f"{fixture}.json").read_text()

    def run(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return SimpleNamespace(stdout=payload, returncode=0, stderr="")
        maps.append(cmd[cmd.index("-map") + 1])
        return SimpleNamespace(stdout=b"RIFF" + bytes(80), returncode=0, stderr="")

    return run


@pytest.mark.parametrize("fixture", [f for f, _, _ in CASES if f in PARITY])
def test_preview_and_pipeline_agree_on_the_same_file(fixture, clip, monkeypatch):
    expected = next(index for name, index, _ in CASES if name == fixture)
    maps = []
    # One patch reaches both: media_select, generate and review_server all use the
    # `subprocess` module object.
    monkeypatch.setattr(media_select.subprocess, "run", _fake_ffprobe(fixture, maps))

    status, body = rs.preview_audio(clip)
    pipeline = generate.eng_audio_index(clip["stem"] + ".mkv")

    assert pipeline == expected
    if expected is None:
        assert status == 422 and "English" in body["error"]
        assert maps == [], "no clip should be extracted when no track qualifies"
    else:
        assert status == 200 and body.startswith(b"RIFF")
        assert maps == [f"0:{expected}"], "the preview must extract the pipeline's track"


def test_preview_reports_a_probe_failure_as_502_not_422(clip, monkeypatch):
    def fail(cmd, **kwargs):
        raise OSError("no ffprobe")

    monkeypatch.setattr(media_select.subprocess, "run", fail)
    status, body = rs.preview_audio(clip)
    assert status == 502
    assert "unavailable" in body["error"]
