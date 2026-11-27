"""dubtitlerr.core.binaries: the boot check that refuses when a media binary is absent.

The module's behavior is pinned here; the container wiring (refuse before any loop starts) is
pinned in tests/test_container_run_binaries.py.
"""

from dubtitlerr.core import binaries


def test_missing_names_only_the_absent_ones(monkeypatch):
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None if name == "mkvmerge" else f"/usr/bin/{name}")
    assert binaries.missing() == ["mkvmerge"]


def test_nothing_missing_when_all_resolve(monkeypatch):
    monkeypatch.setattr(binaries.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert binaries.missing() == []


def test_main_is_silent_and_zero_when_all_present(monkeypatch, capsys):
    monkeypatch.setattr(binaries.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert binaries.main() == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_main_names_every_missing_binary_and_exits_one(monkeypatch, capsys):
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None)
    assert binaries.main() == 1
    err = capsys.readouterr().err
    assert "FATAL" in err
    for name in ("ffmpeg", "ffprobe", "mkvmerge"):
        assert name in err


def test_the_required_set_is_the_three_pipeline_binaries():
    assert binaries.REQUIRED == ("ffmpeg", "ffprobe", "mkvmerge")
    # bsdtar stays out: refs/acquire.py guards it per-feature (.rar reference archives only),
    # so requiring it at boot would refuse installs that never touch the feature.
    assert "bsdtar" not in binaries.REQUIRED
