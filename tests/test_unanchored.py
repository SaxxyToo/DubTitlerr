"""The per-show no-reference record the review UI turns into a one-click switch."""

import json
from pathlib import Path

from dubtitlerr.core import unanchored

SHOW = "Frieren Beyond Journeys End (2023) [tvdbid-424536]"


def test_record_then_read_aggregates_one_show(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert unanchored.record(SHOW, "/lib/Frieren/Season 01/S01E01.mkv", 12) is True
    assert unanchored.record(SHOW, "/lib/Frieren/Season 01/S01E02.mkv", 9) is True
    got = unanchored.read(SHOW)
    assert got["episodes"] == 2
    assert got["targets"] == 12
    assert got["last_episode"] == "S01E02.mkv"
    assert got["last_at"].endswith("Z")


def test_read_dedupes_a_repeated_episode(tmp_path, monkeypatch):
    # A repair re-run on the same episode must not look like a second affected episode.
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    unanchored.record(SHOW, "S01E01.mkv", 4)
    unanchored.record(SHOW, "S01E01.mkv", 6)
    got = unanchored.read(SHOW)
    assert got["episodes"] == 1 and got["targets"] == 6


def test_other_shows_and_corrupt_lines_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    unanchored.record(SHOW, "S01E01.mkv", 4)
    unanchored.record("Another Show", "S01E01.mkv", 40)
    with open(unanchored.path(), "a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert unanchored.read(SHOW)["episodes"] == 1
    assert unanchored.read("Another Show")["targets"] == 40


def test_an_absent_show_reads_empty_without_creating_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert unanchored.read(SHOW) == {}
    assert not Path(unanchored.path()).exists()


def test_a_blank_show_is_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert unanchored.record("", "S01E01.mkv", 4) is False
    assert not Path(unanchored.path()).exists()


def test_the_record_is_jsonl_with_the_episode_stem(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    unanchored.record(SHOW, "/lib/Show/Season 01/S01E01.mkv", 7)
    line = Path(unanchored.path()).read_text().splitlines()[0]
    entry = json.loads(line)
    assert entry["show"] == SHOW and entry["episode"] == "S01E01.mkv" and entry["targets"] == 7
