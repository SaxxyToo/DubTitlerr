"""Tests for tools/references_report.py -- the read-only reference-axis / route audit.

Synthetic media only: invented subtitle lines muxed into tiny clips by ffmpeg. PGS has no
encoder in ffmpeg, so the bitmap case is covered two ways: the classifier-level test feeds a
``hdmv_pgs_subtitle`` stream (the codec filter is what must hold), and the end-to-end tree
includes an episode with no subtitle streams at all, which takes the same
"no native text" path through the report.

The read-only contract is asserted too: the JSON report is written outside the scanned root,
a report path inside the root is refused, and the scanned tree is byte-for-byte unchanged.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

import tools.references_report as rr
from dubtitlerr.core import common, reftracks

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DUB_LINES = [
    "The gate was open before we got here.",
    "Mira, hand me the second lantern.",
    "Nobody walks away from Harrow and keeps a clean name.",
    "Keep your voice down, the guards are still awake.",
    "We leave at first light, whether the storm breaks or not.",
]
TRANS_LINES = [
    "Someone unlocked this gate before our arrival.",
    "Pass the other lantern, Mira.",
    "Anyone who crosses Harrow carries a stain forever.",
    "Lower your voice; the sentries have not slept.",
    "Dawn decides our departure, storm or clear skies.",
]

ASS_HEADER = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "PlayResX: 640\n"
    "PlayResY: 480\n"
    "\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
    "Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,"
    "10,10,10,1\n"
    "\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)


def _ass(lines):
    body = "".join(
        f"Dialogue: 0,0:00:{i * 2:02d}.00,0:00:{i * 2 + 1:02d}.00,Default,,0,0,0,,{line}\n"
        for i, line in enumerate(lines)
    )
    return ASS_HEADER + body


def _srt(lines):
    return "\n".join(
        f"{i}\n00:00:{i * 2 - 2:02d},000 --> 00:00:{i * 2 - 1:02d},000\n{line}\n"
        for i, line in enumerate(lines, 1)
    )


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _mux(dst, subs, *, audio=True):
    """1-second black clip with the given (path, lang, title) subtitle tracks, + sine audio."""
    args = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    for path, _lang, _title in subs:
        args += ["-i", path]
    args += ["-map", "0:v"]
    if audio:
        args += ["-map", "1:a"]
    first_sub = 2 if audio else 1
    for i in range(len(subs)):
        args += ["-map", f"{first_sub + i}:0"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "copy"]
    for i, (_path, lang, title) in enumerate(subs):
        args += [f"-metadata:s:s:{i}", f"language={lang}"]
        if title:
            args += [f"-metadata:s:s:{i}", f"title={title}"]
    args += [str(dst)]
    subprocess.run(args, check=True, capture_output=True)


def _episode(root, show, name, tmp_path, subs, *, audio=True):
    d = root / show / "Season 01"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / (name + ".mkv")
    _mux(dst, subs, audio=audio)
    return str(dst)


# --- axes / route vocabulary --------------------------------------------------


def _track(role, provenance=reftracks.PROVENANCE_NATIVE, events=1, codec="ass"):
    ev = tuple(reftracks.Event(float(i), float(i) + 1, "x") for i in range(events))
    return reftracks.Track(
        index=2, codec=codec, lang="eng", title="t", provenance=provenance,
        role=role, role_confirmed=True, role_overlap=None, events=ev,
    )


@pytest.mark.parametrize(
    "tracks,axes",
    [
        ([_track(reftracks.ROLE_DUB)], "native-dub"),
        ([_track(reftracks.ROLE_TRANSLATION)], "native-translation"),
        ([_track(reftracks.ROLE_DUB), _track(reftracks.ROLE_TRANSLATION, codec="subrip")], "both"),
        ([], "none"),
        # a dub-titled track with no dialogue is not dub captions
        ([_track(reftracks.ROLE_DUB, events=0)], "none"),
        # signs are never a reference
        ([_track(reftracks.ROLE_SIGNS)], "none"),
        # our own output is the artifact, not a witness
        ([_track(reftracks.ROLE_DUB, provenance=reftracks.PROVENANCE_OURS)], "none"),
        # subrip dub captions DO count (the 37 blind spots R1 fixed)
        ([_track(reftracks.ROLE_DUB, codec="subrip")], "native-dub"),
    ],
)
def test_axes_of(tracks, axes):
    assert rr.axes_of(tracks) == axes


def test_route_sourced_when_native_dub_present():
    route, reason = rr.route_of([_track(reftracks.ROLE_DUB, codec="subrip")])
    assert route == "sourced"
    assert "subrip" in reason


def test_route_generate_from_translation_only():
    route, _ = rr.route_of([_track(reftracks.ROLE_TRANSLATION)])
    assert route == "generate"


def test_route_generate_with_no_reference_and_unanchored_flag_noted():
    route, reason = rr.route_of([], unanchored_ok=True)
    assert route == "generate"
    assert "unanchored_repair" in reason


def test_route_unfillable_without_audio():
    route, reason = rr.route_of([_track(reftracks.ROLE_TRANSLATION)], audio_ok=False)
    assert route == "unfillable"
    assert "audio" in reason


def test_pgs_only_is_no_text_track(monkeypatch):
    """Bitmap codecs are ignored by the classifier -- a PGS-only episode has no reference."""
    monkeypatch.setattr(
        reftracks, "_probe",
        lambda video: [{"index": 2, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}}],
    )
    assert reftracks.classify("whatever.mkv") == []
    assert rr.axes_of([]) == "none"


def test_pgs_only_episode_reports_none_but_lists_the_stream(monkeypatch, tmp_path):
    """The metadata report must keep the bitmap stream visible (PGS-only != no subtitles)."""
    monkeypatch.setattr(
        rr, "probe_subtitle_streams",
        lambda video: [{"index": 2, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng", "title": ""}}],
    )
    monkeypatch.setattr(rr, "probe_audio", lambda video: True)
    ep = rr.episode_report("placeholder.mkv", str(tmp_path), str(tmp_path / "gloss"), sample=True)
    assert ep["axes"] == "none" and ep["route"] == "generate"
    assert [s["codec"] for s in ep["subtitle_streams"]] == ["hdmv_pgs_subtitle"]
    assert ep["tracks"] == []


# --- sidecar / stamp inventory ------------------------------------------------


def test_sidecar_files_reports_house_and_artifact_names(tmp_path):
    stem = str(tmp_path / "ep")
    (tmp_path / "ep.dub.en.srt").write_text("1\n", encoding="utf-8")
    (tmp_path / "ep.eng.dubtitles.ass").write_text("[Events]\n", encoding="utf-8")
    found = rr.sidecar_files(stem, suffixes=[".dub.en.ass", ".dub.en.srt", ".eng.dubtitles.ass", ".eng.dubtitles.srt"])
    assert found == ["ep.dub.en.srt", "ep.eng.dubtitles.ass"]


def test_stamp_state_current_then_stale(tmp_path):
    video = tmp_path / "ep.mkv"
    video.write_bytes(b"video-bytes")
    stem = str(tmp_path / "ep")
    common.write_stamp(stem + common.STAMP_SUFFIX, str(video))
    cur = rr.stamp_state(stem, str(video))
    assert cur["present"] and cur["current"] and cur["stale_tiers"] == []

    # a stamp one text tier behind, still describing this file
    st = video.stat()
    (tmp_path / ("ep" + common.STAMP_SUFFIX)).write_text(
        json.dumps({
            "muxed": True,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "transcribe_version": common.TRANSCRIBE_VERSION,
            "text_version": common.TEXT_VERSION - 1,
        }),
        encoding="utf-8",
    )
    stale = rr.stamp_state(stem, str(video))
    assert stale["present"] and not stale["current"] and stale["stale_tiers"] == ["text"]


def test_missing_stamp_reads_stale_but_absent(tmp_path):
    video = tmp_path / "ep.mkv"
    video.write_bytes(b"x")
    state = rr.stamp_state(str(tmp_path / "ep"), str(video))
    assert state["present"] is False and state["stale_tiers"] == ["transcribe", "text"]


# --- end-to-end synthetic library ---------------------------------------------


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    """Six shows covering every axis/route/marker case in the card."""
    d = tmp_path_factory.mktemp("refreport_lib")
    root = d / "media"
    root.mkdir()
    work = d / "src"
    work.mkdir()

    dub_srt = _write(work, "dub.srt", _srt(DUB_LINES))
    dub_ass = _write(work, "dub.ass", _ass(DUB_LINES))
    trans_ass = _write(work, "trans.ass", _ass(TRANS_LINES))
    ours_ass = _write(work, "ours.ass", _ass(DUB_LINES))

    alpha = _episode(root, "Alpha Dub Show (2020) [tvdbid-1]", "Alpha - S01E01", work, [(dub_srt, "eng", "Dub")])
    _episode(root, "Bravo Trans Show (2021) [tvdbid-2]", "Bravo - S01E01", work, [(trans_ass, "eng", "Full Subtitles [GJM]")])
    _episode(
        root, "Charlie Both Show (2022) [tvdbid-3]", "Charlie - S01E01", work,
        [(dub_ass, "eng", "English (Dubtitle)"), (trans_ass, "eng", "English")],
    )
    _episode(root, "Delta Ours Show (2023) [tvdbid-4]", "Delta - S01E01", work, [(ours_ass, "eng", common.TRACK_NAME)])
    _episode(root, "Echo Silent Show (2024) [tvdbid-5]", "Echo - S01E01", work, [])
    _episode(root, "Foxtrot Mute Show (2025) [tvdbid-6]", "Foxtrot - S01E01", work, [], audio=False)

    # Alpha already delivered: house sidecar + a matching current stamp
    stem = os.path.splitext(alpha)[0]
    (root / "Alpha Dub Show (2020) [tvdbid-1]" / "Season 01" / "Alpha - S01E01.dub.en.srt").write_text("1\n", encoding="utf-8")
    common.write_stamp(stem + common.STAMP_SUFFIX, alpha)

    out = d / "report.json"
    return root, out, d


def _run(root, out, *extra):
    cmd = [sys.executable, os.path.join(REPO, "tools", "references_report.py"),
           "--root", str(root), "--json", str(out), "--jobs", "2", *extra]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)


def _show(report, name):
    return next(s for s in report["shows"] if s["show"].startswith(name))


@needs_ffmpeg
def test_report_end_to_end(library):
    root, out, _ = library
    proc = _run(root, out, "--verify-content")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))

    assert doc["schema"] == rr.SCHEMA
    assert doc["mode"] == "content-verified"
    assert doc["totals"]["shows"] == 6
    assert doc["totals"]["episodes"] == 6

    alpha = _show(doc, "Alpha")
    ep = alpha["items"][0]
    assert ep["axes"] == "native-dub"
    assert ep["route"] == "sourced"
    assert ep["tracks"][0]["codec"] == "subrip" and ep["tracks"][0]["role"] == "dub"
    assert ep["sidecar"] == {"present": True, "files": ["Alpha - S01E01.dub.en.srt"]}
    assert ep["stamp"]["current"] is True

    bravo = _show(doc, "Bravo")["items"][0]
    assert bravo["axes"] == "native-translation" and bravo["route"] == "generate"
    assert bravo["tracks"][0]["role"] == "translation"

    charlie = _show(doc, "Charlie")["items"][0]
    assert charlie["axes"] == "both" and charlie["route"] == "sourced"

    delta = _show(doc, "Delta")["items"][0]
    assert delta["axes"] == "none" and delta["ours_embedded"] == 1 and delta["route"] == "generate"

    echo = _show(doc, "Echo")["items"][0]
    assert echo["axes"] == "none" and echo["route"] == "generate" and echo["audio_ok"] is True

    foxtrot = _show(doc, "Foxtrot")["items"][0]
    assert foxtrot["axes"] == "none" and foxtrot["route"] == "unfillable"
    assert "audio" in foxtrot["route_reason"]

    # counts only in the per-show summary: no subtitle text, no ASR text in the document
    blob = out.read_text(encoding="utf-8")
    assert "hand me the second lantern" not in blob
    assert "Someone unlocked this gate" not in blob


@needs_ffmpeg
def test_report_metadata_mode_uses_titles_without_extraction(library):
    """Default depth: ffprobe-only inventory; the axes and routes must match the verified run."""
    root, out, _ = library
    proc = _run(root, out)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["mode"] == "metadata"

    alpha = _show(doc, "Alpha")["items"][0]
    assert alpha["axes"] == "native-dub" and alpha["route"] == "sourced"
    assert alpha["tracks"][0]["role"] == "dub" and alpha["tracks"][0]["dialogue_events"] is None
    assert alpha["subtitle_streams"][0]["codec"] == "subrip"
    assert _show(doc, "Alpha")["dub_codecs"] == {"subrip": 1}

    charlie = _show(doc, "Charlie")["items"][0]
    assert charlie["axes"] == "both" and charlie["route"] == "sourced"

    delta = _show(doc, "Delta")["items"][0]
    assert delta["axes"] == "none" and delta["ours_embedded"] == 1

    foxtrot = _show(doc, "Foxtrot")["items"][0]
    assert foxtrot["route"] == "unfillable"

    totals = doc["totals"]
    assert totals["routes"] == {"sourced": 2, "generate": 3, "unfillable": 1}


@needs_ffmpeg
def test_scan_does_not_write_under_the_root(library):
    root, out, d = library

    def snapshot(base):
        return sorted((p, os.stat(p).st_mtime_ns) for p, _dirs, files in os.walk(base) for p in
                      [os.path.join(p, f) for f in files])

    before = snapshot(root)
    proc = _run(root, out, "--verify-content")
    assert proc.returncode == 0, proc.stderr
    assert snapshot(root) == before


@needs_ffmpeg
def test_refuses_report_path_inside_root(library):
    root, _out, _d = library
    bad = root / "report.json"
    proc = _run(root, bad)
    assert proc.returncode == 2
    assert "refusing" in proc.stderr
    assert not bad.exists()


@needs_ffmpeg
def test_show_and_limit_filters(library):
    root, out, _d = library
    proc = _run(root, out, "--show", "Charlie", "--limit", "1")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert [s["show"].split(" ")[0] for s in doc["shows"]] == ["Charlie"]
    assert doc["totals"]["episodes"] == 1
