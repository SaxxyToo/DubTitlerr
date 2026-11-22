"""refs.match: which Jimaku entry and file belong to the episode in front of us.

The names in these tests are the shapes real files carry -- fansub releases with a bare
absolute number, Sonarr-style SxxEyy, a multi-episode special, a Japanese 第N話, a season
pack volume, and Jimaku's own language tags. The matcher's job is to say no to almost all of
them, so the negative cases carry as much weight as the positive ones.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.core.refs import match


def _entry(name, english=None, japanese=None, anilist_id=1):
    return {"id": anilist_id, "name": name, "english_name": english, "japanese_name": japanese}


# --- entry season and cour ------------------------------------------------------------


def test_entry_season_shapes():
    assert match.entry_season(_entry("Frieren: Beyond Journey's End Season 2")) == 2
    assert match.entry_season(_entry("Mushoku Tensei II: Isekai Ittara Honki Dasu")) == 2
    assert match.entry_season(_entry("進撃の巨人 第2期")) == 2
    assert match.entry_season(_entry("Frieren")) is None


def test_later_part_is_not_the_episode_we_want():
    assert match.is_later_part(_entry("Mushoku Tensei: Part 2"))
    assert match.is_later_part(_entry("Some Show Cour 2"))
    assert not match.is_later_part(_entry("Frieren Season 2"))


def test_bare_title_drops_the_markers():
    assert match.bare_title("Frieren: Beyond Journey's End Season 2") == "frieren beyond journey s end"
    assert match.matches_title(_entry("Frieren: Beyond Journey's End Season 2"), ["Frieren: Beyond Journey's End"])


def test_select_entries_filters_the_season():
    entries = [
        _entry("Frieren", anilist_id=1),
        _entry("Frieren Season 2", anilist_id=2),
        _entry("Dragon Ball Daima", anilist_id=3),
    ]
    titles = ["Frieren"]
    assert [entry["id"] for entry in match.select_entries(entries, titles=titles, season=2)] == [2]
    assert [entry["id"] for entry in match.select_entries(entries, titles=titles, season=1)] == [1]


def test_select_entries_keeps_everything_when_the_title_matches_nothing():
    entries = [_entry("Frieren", anilist_id=1)]
    # A differently-localised title must not lose the results we did find.
    assert match.select_entries(entries, titles=["Frieren: Beyond Journey's End"], season=1) == entries


# --- file names -----------------------------------------------------------------------


def test_file_episode_shapes():
    assert match.file_episode("[SubsPlease] Frieren - 05 (1080p) [ABCD].ass").episodes == (5,)
    assert match.file_episode("Frieren.S01E05.ja.srt") == match.EpisodeName(1, (5,), None)
    assert match.file_episode("Show 1x05.ass") == match.EpisodeName(1, (5,), None)
    assert match.file_episode("第05話.ass").episodes == (5,)
    assert match.file_episode("Show - 05v2.ass").episodes == (5,)


def test_a_multi_episode_file_names_the_whole_set():
    named = match.file_episode("Show - 01-04 [TVSP].ass")
    assert named.episodes == (1, 2, 3, 4)


def test_a_season_pack_states_no_episode():
    assert match.file_episode("Frieren.S01.BluRay.1080p.ass").episodes == ()
    assert match.file_episode("Frieren_BD_Vol1.ass").episodes == ()


def test_file_matches_episode_negative_cases():
    assert not match.file_matches_episode("Show.S01E06.ja.srt", episode=5, season=1)
    assert not match.file_matches_episode("Show - 01-04.ass", episode=2, season=1)  # a partial hit is not this episode
    assert not match.file_matches_episode("Frieren.S02E05.ass", episode=5, season=1)
    assert not match.file_matches_episode("Frieren.S01.BluRay.ass", episode=5, season=1)


def test_absolute_numbering_ignores_the_absent_season():
    # "Frieren - 28" is S2E28 absolute; the file states no season at all.
    assert match.file_matches_episode("Frieren - 28.ass", episode=28, season=2, absolute=True)


# --- language tags --------------------------------------------------------------------


def test_language_from_tags():
    assert match.language_from_tags("Show.S01E05.ja.srt") == "ja"
    assert match.language_from_tags("[JPN] Show - 05.ass") == "ja"
    assert match.language_from_tags("[CHS, JPN] Show - 05.ass") is None
    assert match.language_from_tags("Show.1080p.WEB.ass") is None


def test_select_files_prefers_the_asked_language_then_ass():
    files = [
        {"name": "Show - 05.en.srt", "url": "https://jimaku.cc/a"},
        {"name": "Show - 05.srt", "url": "https://jimaku.cc/b"},
        {"name": "Show - 05.ja.ass", "url": "https://jimaku.cc/c"},
        {"name": "Show - 06.ja.ass", "url": "https://jimaku.cc/d"},
    ]
    chosen = match.select_files(files, episode=5, season=1, language="ja")
    assert [file["url"] for file in chosen] == ["https://jimaku.cc/c", "https://jimaku.cc/b"]


def test_select_files_refuses_an_episode_it_cannot_name():
    files = [{"name": "Show Season 1 (Batch).zip", "url": "https://jimaku.cc/x"}]
    assert match.select_files(files, episode=5, season=1, language="ja") == []
