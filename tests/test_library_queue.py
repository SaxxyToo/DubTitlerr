"""The folder-tree bootstrap for a fresh install (no Plex/WatchState/Jellyfin)."""

import os

from dubtitlerr.pipeline import library_queue


def _show(root, name, files=("S01E01.mkv",), mtime=None):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for i, fn in enumerate(files):
        sub = d / "Season 01"
        sub.mkdir(exist_ok=True)
        p = sub / fn
        p.write_bytes(b"x")
        if mtime is not None:
            os.utime(p, (mtime + i, mtime + i))
    return d


def test_scan_lists_only_directories_that_hold_a_video(tmp_path):
    _show(tmp_path, "Frieren", mtime=1000)
    (tmp_path / "Empty Show").mkdir()
    (tmp_path / "Frieren.nfo").write_text("not a show")
    assert [n for n, _ in library_queue.scan(tmp_path)] == ["Frieren"]


def test_scan_orders_by_newest_episode_then_name(tmp_path):
    _show(tmp_path, "Old Show", mtime=1000)
    _show(tmp_path, "New Show", mtime=3000)
    _show(tmp_path, "Same A", mtime=2000)
    _show(tmp_path, "Same B", mtime=2000)
    assert [n for n, _ in library_queue.scan(tmp_path)] == ["New Show", "Same A", "Same B", "Old Show"]


def test_scan_skips_hidden_and_system_directories(tmp_path):
    _show(tmp_path, "Frieren", mtime=1000)
    for name in (".hidden", "@eaDir", "#recycle", "lost+found"):
        _show(tmp_path, name, mtime=9000)
    assert [n for n, _ in library_queue.scan(tmp_path)] == ["Frieren"]


def test_scan_ignores_a_video_at_the_root(tmp_path):
    (tmp_path / "loose.mkv").write_bytes(b"x")
    assert library_queue.scan(tmp_path) == []


def test_main_writes_one_show_per_line(tmp_path, capsys):
    anime = tmp_path / "anime"
    _show(anime, "Frieren", mtime=2000)
    _show(anime, "Bebop", mtime=1000)
    out = tmp_path / "config" / "anime_order.txt"
    assert library_queue.main(["--root", str(anime), "--out", str(out)]) == 0
    assert out.read_text().splitlines() == ["Frieren", "Bebop"]
    assert "2 show(s)" in capsys.readouterr().out


def test_main_refuses_to_clobber_a_hand_written_order_file(tmp_path, capsys):
    anime = tmp_path / "anime"
    _show(anime, "Frieren")
    out = tmp_path / "anime_order.txt"
    out.write_text("My Chosen Show\n")
    assert library_queue.main(["--root", str(anime), "--out", str(out)]) == 1
    assert out.read_text() == "My Chosen Show\n"
    assert "already has shows" in capsys.readouterr().err


def test_force_replaces_a_non_empty_order_file(tmp_path):
    anime = tmp_path / "anime"
    _show(anime, "Frieren", mtime=1000)
    out = tmp_path / "anime_order.txt"
    out.write_text("Stale\n")
    assert library_queue.main(["--root", str(anime), "--out", str(out), "--force"]) == 0
    assert out.read_text() == "Frieren\n"


def test_no_shows_writes_nothing_and_fails(tmp_path, capsys):
    anime = tmp_path / "anime"
    anime.mkdir()
    (anime / "Empty").mkdir()
    out = tmp_path / "anime_order.txt"
    assert library_queue.main(["--root", str(anime), "--out", str(out)]) == 1
    assert not out.exists()
    assert "no shows with video" in capsys.readouterr().err


def test_dry_run_prints_but_writes_nothing(tmp_path, capsys):
    anime = tmp_path / "anime"
    _show(anime, "Frieren")
    out = tmp_path / "anime_order.txt"
    assert library_queue.main(["--root", str(anime), "--out", str(out), "--dry-run"]) == 0
    assert not out.exists()
    assert capsys.readouterr().out.strip() == "Frieren"


def test_a_season_subfolder_is_not_a_second_show(tmp_path):
    anime = tmp_path / "anime"
    d = anime / "Frieren"
    (d / "Season 01").mkdir(parents=True)
    (d / "Season 02").mkdir()
    (d / "Season 01" / "S01E01.mkv").write_bytes(b"x")
    (d / "Season 02" / "S02E01.mkv").write_bytes(b"x")
    assert [n for n, _ in library_queue.scan(anime)] == ["Frieren"]


def test_dry_run_ignores_an_existing_order_file(tmp_path, capsys):
    # A dry run inspects the tree; it must not care what is already at --out.
    anime = tmp_path / "anime"
    _show(anime, "Frieren")
    out = tmp_path / "anime_order.txt"
    out.write_text("Hand-written\n")
    assert library_queue.main(["--root", str(anime), "--out", str(out), "--dry-run"]) == 0
    assert out.read_text() == "Hand-written\n"
    assert capsys.readouterr().out.strip() == "Frieren"
