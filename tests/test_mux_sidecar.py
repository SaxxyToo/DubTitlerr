"""OUTPUT_MODE=sidecar: publish the assembled subtitle beside the video, touch no video.

The mux stage is the only place the pipeline rewrites a media file. Sidecar mode ends the
pipeline one step earlier, so these tests pin the two things that make that safe:
the video's bytes/mtime never move, and the idempotency stamp records the delivery MODE so
switching back to mux re-muxes instead of reading the sidecar stamp as already done.
"""

import json
import os

import common
import mux

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"


def _video(tmp_path, name="ep01.mkv"):
    v = tmp_path / "Show" / name
    v.parent.mkdir(parents=True, exist_ok=True)
    v.write_bytes(b"video" * 1000)
    return str(v)


def _stamp(stem):
    return common.read_stamp(stem + mux.STAMP_SUFFIX)


def test_sidecar_mode_renames_artifact_and_never_touches_video(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "OUTPUT_ROOT", "")
    monkeypatch.setattr(mux, "OUTPUT_MODE", "sidecar")
    monkeypatch.setattr(mux, "SIDECAR_SUFFIX", ".dub.en")
    v = _video(tmp_path)
    stem = os.path.splitext(v)[0]
    with open(stem + mux.SRT_SUFFIX, "w", encoding="utf-8") as f:
        f.write(SRT)
    before = os.stat(v)

    assert mux.process(v, apply=True) == "sidecar"

    # The artifact is published under the house name and the pipeline copy is gone.
    assert os.path.exists(stem + ".dub.en.srt")
    assert not os.path.exists(stem + mux.SRT_SUFFIX)
    # The video is byte-for-byte and timestamp-for-timestamp the same file.
    after = os.stat(v)
    assert (after.st_size, after.st_mtime) == (before.st_size, before.st_mtime)
    # The stamp records the delivery mode, so mux mode will not treat it as done.
    stamp = _stamp(stem)
    assert stamp["mode"] == "sidecar"
    assert common.stamp_mode(stamp) == "sidecar"
    assert mux.already_delivered(stamp, v, "sidecar")
    assert not mux.already_delivered(stamp, v, "mux")


def test_sidecar_mode_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "OUTPUT_ROOT", "")
    monkeypatch.setattr(mux, "OUTPUT_MODE", "sidecar")
    v = _video(tmp_path)
    stem = os.path.splitext(v)[0]
    with open(stem + mux.SRT_SUFFIX, "w", encoding="utf-8") as f:
        f.write(SRT)
    assert mux.process(v, apply=True) == "sidecar"
    assert mux.process(v, apply=True) == "already-sidecar"


def test_sidecar_keeps_the_assembled_ass_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "OUTPUT_ROOT", "")
    monkeypatch.setattr(mux, "OUTPUT_MODE", "sidecar")
    monkeypatch.setattr(mux, "SIDECAR_SUFFIX", ".dub.en")
    v = _video(tmp_path)
    stem = os.path.splitext(v)[0]
    with open(stem + mux.ASS_SUFFIX, "w", encoding="utf-8") as f:
        f.write("[Script Info]\n")

    assert mux.process(v, apply=True) == "sidecar"

    # The merged artifact stays .ass: converting it to .srt would drop sign positioning.
    assert os.path.exists(stem + ".dub.en.ass")
    assert _stamp(stem)["stages"]["signs_merge"] is True


def test_sidecar_refuses_a_dialogue_only_fallback_after_a_signs_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "OUTPUT_ROOT", "")
    monkeypatch.setattr(mux, "OUTPUT_MODE", "sidecar")
    v = _video(tmp_path)
    stem = os.path.splitext(v)[0]
    with open(stem + mux.SRT_SUFFIX, "w", encoding="utf-8") as f:
        f.write(SRT)
    common.write_stage(stem, "signs", "build-error", "boom")

    assert mux.process(v, apply=True) == "signs-regression-refused"
    assert not os.path.exists(stem + mux.STAMP_SUFFIX)


def test_dry_run_plans_without_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "OUTPUT_ROOT", "")
    monkeypatch.setattr(mux, "OUTPUT_MODE", "sidecar")
    monkeypatch.setattr(mux, "SIDECAR_SUFFIX", ".dub.en")
    v = _video(tmp_path)
    stem = os.path.splitext(v)[0]
    with open(stem + mux.SRT_SUFFIX, "w", encoding="utf-8") as f:
        f.write(SRT)

    assert mux.process(v, apply=False) == "plan"
    assert os.path.exists(stem + mux.SRT_SUFFIX)
    assert not os.path.exists(stem + ".dub.en.srt")
    assert not os.path.exists(stem + mux.STAMP_SUFFIX)


def test_keep_all_subs_keeps_a_mislabeled_english_track():
    """KEEP_LANGS deletes tracks; MUX_KEEP_ALL_SUBS is the opt-out. Our own old Dubtitles
    track is still dropped so a re-mux replaces rather than duplicates it."""
    mislabeled = {"properties": {"language": "enm", "track_name": ""}}
    ours = {"properties": {"language": "eng", "track_name": mux.TRACK_NAME}}
    assert mux.keep_sub(mislabeled, {"eng", "en"}) is False  # upstream default
    mux.KEEP_ALL_SUBS = True
    try:
        assert mux.keep_sub(mislabeled, {"eng", "en"}) is True
        assert mux.keep_sub(ours, {"eng", "en"}) is False
    finally:
        mux.KEEP_ALL_SUBS = False


def test_build_cmd_keeps_the_mislabeled_track_when_asked(monkeypatch):
    info = {"tracks": [{"id": 2, "type": "subtitles", "properties": {"language": "enm", "track_name": ""}}]}
    _, dropped = mux.build_cmd(info, "ep.mkv", "ep.eng.dubtitles.ass", "out.mkv")
    assert dropped == ["sub:enm"]
    monkeypatch.setattr(mux, "KEEP_ALL_SUBS", True)
    cmd, dropped = mux.build_cmd(info, "ep.mkv", "ep.eng.dubtitles.ass", "out.mkv")
    assert dropped == []
    assert cmd[cmd.index("-s") + 1] == "2"


def test_sidecar_preserves_a_foreign_file_at_the_destination(tmp_path, monkeypatch):
    """The dubsub pipeline also writes .dub.en.srt. Sidecar mode must not silently replace
    a subtitle this pipeline did not put there."""
    monkeypatch.setattr(common, "OUTPUT_ROOT", "")
    monkeypatch.setattr(mux, "OUTPUT_MODE", "sidecar")
    monkeypatch.setattr(mux, "SIDECAR_SUFFIX", ".dub.en")
    v = _video(tmp_path)
    stem = os.path.splitext(v)[0]
    with open(stem + mux.SRT_SUFFIX, "w", encoding="utf-8") as f:
        f.write(SRT)
    foreign = stem + ".dub.en.srt"
    with open(foreign, "w", encoding="utf-8") as f:
        f.write("somebody else's subtitle\n")

    assert mux.process(v, apply=True) == "sidecar"

    assert open(foreign + ".bak", encoding="utf-8").read() == "somebody else's subtitle\n"
    assert "Hello" in open(foreign, encoding="utf-8").read()


def test_sidecar_does_not_back_up_its_own_previous_output(tmp_path, monkeypatch):
    """A stale-VERSION stamp (the version-bump path) is still our own file: no .bak."""
    monkeypatch.setattr(common, "OUTPUT_ROOT", "")
    monkeypatch.setattr(mux, "OUTPUT_MODE", "sidecar")
    monkeypatch.setattr(mux, "SIDECAR_SUFFIX", ".dub.en")
    v = _video(tmp_path)
    stem = os.path.splitext(v)[0]
    with open(stem + mux.SRT_SUFFIX, "w", encoding="utf-8") as f:
        f.write(SRT)
    assert mux.process(v, apply=True) == "sidecar"

    # A version bump makes stamp_valid false but the stamp still describes this video.
    stamp_path = stem + mux.STAMP_SUFFIX
    stamp = common.read_stamp(stamp_path)
    assert stamp is not None
    stamp["text_version"] = 0
    common.atomic_write(stamp_path, lambda f: json.dump(stamp, f))
    with open(stem + mux.SRT_SUFFIX, "w", encoding="utf-8") as f:
        f.write("second run\n")

    assert mux.process(v, apply=True) == "sidecar"
    assert not os.path.exists(stem + ".dub.en.srt.bak")


def test_stamp_without_a_mode_reads_as_mux():
    """Every stamp written before OUTPUT_MODE existed meant an embedded track."""
    assert common.stamp_mode({"muxed": True}) == "mux"
    assert common.stamp_mode({}) == "mux"
    assert common.stamp_mode(None) == "mux"
