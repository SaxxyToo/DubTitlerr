"""Tests for dubtitlerr.core.reftracks -- the repair-reference track classifier.

Everything here is synthetic: hand-written .ass/.srt with invented lines, muxed
into tiny videos by ffmpeg. No commercial subtitle text is copied anywhere. The
media-dependent tests skip when ffmpeg is unavailable; CI installs it.
"""

import shutil
import subprocess

import pysubs2
import pytest

from dubtitlerr.core import common, reftracks

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")

# Invented lines. The dub wording and the translation deliberately share names
# ("Mira", "Harrow") but almost no ordinary words, which is how the real library
# behaves and what the overlap check keys on.
DUB_LINES = [
    "The gate was open before we got here.",
    "Mira, hand me the second lantern.",
    "I told you that trail was washed out.",
    "Nobody walks away from Harrow and keeps a clean name.",
    "Keep your voice down, the guards are still awake.",
    "We leave at first light, whether the storm breaks or not.",
    "That map is worthless if the river moved again.",
    "Storm lanterns burn quicker than the oil clerk admits.",
    "Harrow's ledger lists every debt except the ones we owe.",
    "Wait until the watchtower bell rings twice, then run.",
    "My brother stitched that sail before the winter took him.",
]
TRANS_LINES = [
    "Someone unlocked this gate before our arrival.",
    "Pass the other lantern, Mira.",
    "I warned you the path had been eroded.",
    "Anyone who crosses Harrow carries a stain forever.",
    "Lower your voice; the sentries have not slept.",
    "Dawn decides our departure, storm or clear skies.",
    "A chart means nothing once the river shifts.",
    "These storm lamps consume fuel faster than the merchant claims.",
    "Every obligation appears in Harrow's books but those we bear.",
    "Remain quiet until the tower bell sounds twice, then flee.",
    "That canvas was sewn by my brother before winter claimed him.",
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


def _ass(lines, style="Default"):
    body = "".join(
        f"Dialogue: 0,0:00:{i * 2:02d}.00,0:00:{i * 2 + 1:02d}.00,{style},,0,0,0,,{line}\n"
        for i, line in enumerate(lines)
    )
    return ASS_HEADER + body


def _srt(lines):
    out = []
    for i, line in enumerate(lines, 1):
        out.append(f"{i}\n00:00:{i * 2 - 2:02d},000 --> 00:00:{i * 2 - 1:02d},000\n{line}\n")
    return "\n".join(out)


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _mux(dst, subs, codec="copy"):
    """1-second black video with the given (path, language, title) subtitle tracks."""
    args = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1"]
    for path, _lang, _title in subs:
        args += ["-i", path]
    args += ["-map", "0:v"]
    for i in range(len(subs)):
        args += ["-map", f"{i + 1}:0"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-c:s", codec]
    for i, (_path, lang, title) in enumerate(subs):
        args += [f"-metadata:s:s:{i}", f"language={lang}"]
        if title:
            args += [f"-metadata:s:s:{i}", f"title={title}"]
        else:
            # ffmpeg rejects an empty metadata value; drop the title tag instead.
            pass
    args += ["-y", str(dst)]
    subprocess.run(args, check=True, capture_output=True)


@pytest.fixture(scope="module")
def mixed_mkv(tmp_path_factory):
    """ass dub-caption + ass translation + our own marker track, in that order."""
    d = tmp_path_factory.mktemp("reftracks_mkv")
    dub = _write(d, "dub.ass", _ass(DUB_LINES))
    trans = _write(d, "trans.ass", _ass(TRANS_LINES))
    ours = _write(d, "ours.ass", _ass(DUB_LINES))
    video = d / "ep.mkv"
    _mux(
        video,
        [
            (dub, "eng", "English (Dubtitle)"),
            (trans, "eng", "Full Subtitles [GJM]"),
            (ours, "eng", common.TRACK_NAME),
            (trans, "ger", "Deutsch"),
        ],
    )
    return str(video)


@pytest.fixture(scope="module")
def subrip_mkv(tmp_path_factory):
    d = tmp_path_factory.mktemp("reftracks_srt")
    dub = _write(d, "dub.srt", _srt(DUB_LINES))
    trans = _write(d, "trans.srt", _srt(TRANS_LINES))
    video = d / "ep-srt.mkv"
    _mux(video, [(dub, "eng", "Dub"), (trans, "eng", "English")])
    return str(video)


@pytest.fixture(scope="module")
def mov_text_mp4(tmp_path_factory):
    d = tmp_path_factory.mktemp("reftracks_mov")
    dub = _write(d, "dub.srt", _srt(DUB_LINES))
    video = d / "ep.mp4"
    _mux(video, [(dub, "eng", "Dub")], codec="mov_text")
    return str(video)


# --- title -> role -----------------------------------------------------------


@pytest.mark.parametrize(
    "title,role,confirmed",
    [
        ("Dub", reftracks.ROLE_DUB, True),
        ("Dubtitle (SDH)", reftracks.ROLE_DUB, True),
        ("English (SDH Dubtitle)", reftracks.ROLE_DUB, True),
        ("English (Dubtitle)", reftracks.ROLE_DUB, True),
        ("English / ASS (Dub/SDH)", reftracks.ROLE_DUB, True),
        ("Dubbed", reftracks.ROLE_DUB, True),
        # ambiguous: says captions of speech, not which axis -> provisional dub
        ("English (SDH)", reftracks.ROLE_DUB, False),
        ("Dialog - ENG", reftracks.ROLE_DUB, False),
        ("English / ASS (SDH)", reftracks.ROLE_DUB, False),
        ("CC", reftracks.ROLE_DUB, False),
        # signs wins even when the title also says dub
        ("English / ASS (Signs)", reftracks.ROLE_SIGNS, True),
        ("English (Signs & Songs)", reftracks.ROLE_SIGNS, True),
        ("Dub (Forced)", reftracks.ROLE_SIGNS, True),
        ("Signs/Songs [GJM]", reftracks.ROLE_SIGNS, True),
        # everything else is a translation prior (nothing names the dub axis)
        ("Full Subtitles [GJM]", reftracks.ROLE_TRANSLATION, False),
        ("English (Netflix)", reftracks.ROLE_TRANSLATION, False),
        # nothing to go on
        ("", reftracks.ROLE_TRANSLATION, False),
    ],
)
def test_title_role_mapping(title, role, confirmed):
    assert reftracks.title_role(title) == (role, confirmed)


def test_content_tokens_drops_function_words_keeps_names_and_contractions():
    toks = reftracks.content_tokens("I don't know what Mira's lantern is for.")
    assert {"don't", "know", "mira", "lantern"} <= toks
    assert not ({"i", "what", "is", "for"} & toks)


def test_content_bigrams_require_the_same_wording_not_just_the_same_words():
    """A paraphrase reuses content words but almost no adjacent pairs; that is the
    whole reason the role check measures bigrams, not unigrams."""
    dub = reftracks.content_bigrams("Pass me the second lantern now")
    same_words = reftracks.content_bigrams("lantern second the me pass now")
    paraphrase = reftracks.content_bigrams("Hand over the other lamp at once")
    assert dub & same_words == set()  # same vocabulary, different order
    assert dub & paraphrase == set()


def test_overlap_score_is_none_for_thin_sample():
    events = (reftracks.Event(0.0, 1.0, "short line here"),)
    assert reftracks.overlap_score(events, "too few words to measure") is None


def test_overlap_score_is_none_without_track_dialogue():
    sample = " ".join(f"word{i}" for i in range(60))
    assert reftracks.overlap_score((), sample) is None


# --- real extraction: ass, subrip, mov_text ----------------------------------


@needs_ffmpeg
def test_classify_reads_ass_tracks_and_marks_ours(mixed_mkv):
    tracks = reftracks.classify(mixed_mkv)
    # de/en only; the German track is filtered out by SUB_LANGS
    assert [t.lang for t in tracks] == ["eng", "eng", "eng"]
    assert [t.codec for t in tracks] == ["ass", "ass", "ass"]
    assert [t.title for t in tracks] == ["English (Dubtitle)", "Full Subtitles [GJM]", common.TRACK_NAME]
    assert [t.role for t in tracks] == [reftracks.ROLE_DUB, reftracks.ROLE_TRANSLATION, reftracks.ROLE_DUB]
    # the first track's dialogue parsed (start_s, end_s, text)
    assert tracks[0].events[0].start == 0.0
    assert tracks[0].events[0].end == 1.0
    assert tracks[0].events[0].text == DUB_LINES[0]
    assert len(tracks[0].events) == len(DUB_LINES)


@needs_ffmpeg
def test_our_marker_is_provenance_ours_and_never_a_reference(mixed_mkv):
    tracks = reftracks.classify(mixed_mkv)
    ours = [t for t in tracks if t.title == common.TRACK_NAME]
    assert len(ours) == 1
    assert ours[0].provenance == reftracks.PROVENANCE_OURS
    assert ours[0].is_reference is False
    assert common.TRACK_NAME not in [t.title for t in reftracks.references(mixed_mkv)]
    # native dialogue tracks stay references
    assert [t.title for t in reftracks.references(mixed_mkv)] == ["English (Dubtitle)", "Full Subtitles [GJM]"]


@needs_ffmpeg
def test_classify_reads_subrip_tracks(subrip_mkv):
    tracks = reftracks.classify(subrip_mkv)
    assert [t.codec for t in tracks] == ["subrip", "subrip"]
    assert [t.role for t in tracks] == [reftracks.ROLE_DUB, reftracks.ROLE_TRANSLATION]  # "Dub" vs "English"
    assert len(tracks[0].events) == len(DUB_LINES)
    assert tracks[0].events[0].text == DUB_LINES[0]


@needs_ffmpeg
def test_classify_reads_mov_text_by_transcoding(mov_text_mp4):
    """mov_text cannot be copy-extracted into srt; the transcode path must still parse.

    mp4 keeps no ``title`` tag for this stream, so the role is the untitled prior
    (provisional translation) -- what matters here is the dialogue events arriving."""
    tracks = reftracks.classify(mov_text_mp4)
    assert [t.codec for t in tracks] == ["mov_text"]
    assert tracks[0].title == ""
    assert tracks[0].role == reftracks.ROLE_TRANSLATION
    assert tracks[0].role_confirmed is False
    assert len(tracks[0].events) == len(DUB_LINES)
    assert tracks[0].events[0].text == DUB_LINES[0]


@pytest.fixture(scope="module")
def mislabeled_srt(tmp_path_factory):
    """Two tracks whose titles disagree with their content, in both directions."""
    d = tmp_path_factory.mktemp("reftracks_mislabeled")
    dub = _write(d, "dub.srt", _srt(DUB_LINES))
    trans = _write(d, "trans.srt", _srt(TRANS_LINES))
    video = d / "ep-mislabeled.mkv"
    _mux(video, [(dub, "eng", "English (SDH)"), (trans, "eng", "Dubtitle (SDH)")])
    return str(video)


# --- overlap confirmation ----------------------------------------------------


@needs_ffmpeg
def test_dub_sample_confirms_dub_and_keeps_translation_provisional_roles(mixed_mkv):
    tracks = reftracks.classify(mixed_mkv, sample_text=" ".join(DUB_LINES))
    dub, trans = tracks[0], tracks[1]
    assert dub.role == reftracks.ROLE_DUB and dub.role_confirmed is True
    assert dub.role_overlap == pytest.approx(1.0)
    assert trans.role == reftracks.ROLE_TRANSLATION and trans.role_confirmed is True
    assert trans.role_overlap is not None and trans.role_overlap < reftracks.OVERLAP_MIN


@needs_ffmpeg
def test_overlap_overrides_a_wrong_title_prior(mislabeled_srt):
    """Measurement wins over the title, both ways: a provisional-dub title holding
    the dub's wording is confirmed dub, and an explicit "Dubtitle" title holding a
    different translation is corrected to translation."""
    tracks = reftracks.classify(mislabeled_srt, sample_text=" ".join(DUB_LINES))
    assert [t.title for t in tracks] == ["English (SDH)", "Dubtitle (SDH)"]
    assert tracks[0].role == reftracks.ROLE_DUB and tracks[0].role_overlap == pytest.approx(1.0)
    assert tracks[1].role == reftracks.ROLE_TRANSLATION
    assert tracks[1].role_overlap is not None and tracks[1].role_overlap < reftracks.OVERLAP_MIN


@needs_ffmpeg
def test_title_prior_holds_without_sample_text(mislabeled_srt):
    tracks = reftracks.classify(mislabeled_srt)
    assert [(t.role, t.role_confirmed, t.role_overlap) for t in tracks] == [
        (reftracks.ROLE_DUB, False, None),  # "English (SDH)" -> provisional dub
        (reftracks.ROLE_DUB, True, None),  # explicit "Dubtitle (SDH)"
    ]


@needs_ffmpeg
def test_thin_sample_text_leaves_the_title_prior_alone(subrip_mkv):
    tracks = reftracks.classify(subrip_mkv, sample_text="only a few words")
    assert [t.role_confirmed for t in tracks] == [True, False]  # "Dub" explicit, "English" prior
    assert [t.role_overlap for t in tracks] == [None, None]


# --- per-stream failure tolerance -------------------------------------------


def test_a_failing_stream_is_skipped_not_fatal(monkeypatch):
    """One unreadable stream must not hide the others: parse failure -> skip."""
    monkeypatch.setattr(
        reftracks,
        "_probe",
        lambda video: [
            {"index": 10, "codec_name": "ass", "tags": {"language": "eng", "title": "Broken"}},
            {"index": 11, "codec_name": "ass", "tags": {"language": "eng", "title": "English (Dubtitle)"}},
            {"index": 12, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}},
        ],
    )
    good = pysubs2.SSAFile()
    good.events = [pysubs2.SSAEvent(text="A line of dialogue.", start=0, end=1000, style="Default")]

    def fake_extract(video, idx, out):
        if idx == 10:
            with open(out, "wb") as f:
                f.write(b"\x00\x01not a subtitle file")
            return True
        good.save(out)
        return True

    monkeypatch.setattr(reftracks, "extract_sub", fake_extract)
    tracks = reftracks.classify("fake.mkv")
    assert [t.index for t in tracks] == [11]
    assert tracks[0].events[0].text == "A line of dialogue."


def test_probe_failure_yields_no_tracks(monkeypatch):
    monkeypatch.setattr(reftracks, "_probe", lambda video: [])
    assert reftracks.classify("missing.mkv") == []


# --- dialogue filter (real-library styles that common.is_dialogue_event drops) ---


def _ssa(text, style="Default", comment=False):
    return pysubs2.SSAEvent(
        text=text, style=style, start=0, end=1000, type="Comment" if comment else "Dialogue"
    )


@pytest.mark.parametrize("style", ["Dubtitle", "Death Note Dialogue", "Default", "Funi-Bottom", "Main"])
def test_dialogue_filter_keeps_dub_caption_style_names(style):
    """common.DIALOGUE_EXCLUDE_STYLE rejects any style containing "title"/"note", which
    would empty the Vinland ("Dubtitle") and Death Note ("Death Note Dialogue") tracks."""
    ev = _ssa("The gate was open.", style=style)
    assert reftracks._is_dialogue_event(ev, ev.plaintext.strip()) is True


@pytest.mark.parametrize(
    "text,style",
    [
        ("A sign line.", "Signs_jtb"),
        ("A song line.", "Song"),
        ("Opening lyric.", "OP"),
        (r"{\k20}ka ra o ke", "Default"),
        (r"{\p1}m 0 0 l 10 0 10 10", "Default"),
        (r"{\pos(300,20)}A sign line.", "Default"),
        ("A line.", "Warning"),
    ],
)
def test_dialogue_filter_drops_signs_songs_karaoke_and_drawings(text, style):
    ev = _ssa(text, style=style)
    assert reftracks._is_dialogue_event(ev, ev.plaintext.strip()) is False


def test_dialogue_filter_drops_comments_and_empty_events():
    ev = _ssa("A line.", comment=True)
    assert reftracks._is_dialogue_event(ev, ev.plaintext.strip()) is False
    empty = _ssa("", style="Default")
    assert reftracks._is_dialogue_event(empty, "") is False
