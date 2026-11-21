"""One decision about which audio track is the English dub.

Two call sites ask that question about the same files, and they used to answer it
differently:

* the pipeline -- :func:`dubtitlerr.pipeline.generate.eng_audio_index` (tag first, then an
  English-evidence title, then the opt-in lone-untagged track);
* the review server's audio preview (English tags only).

The preview therefore 422'd "no English dialogue audio track" on an episode the pipeline
had just produced from an ``und`` track titled "English Dub" -- the review tool disagreeing
with the artefact it was reviewing. Every rule about tags, titles and commentary exclusion
lives here now; call sites probe, call :func:`select_english_audio`, and act on the result.

The env knobs stay environment reads at call time (``UND_AUDIO_AS_DUB``, ``REQUIRE_ENG``),
so a stored setting still takes effect on the next show without a restart, and the preview
honours exactly what the pipeline will do.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

# Tags that assert English audio. `eng` is ISO 639-2/B; `en` shows up in hand-remuxed files.
ENGLISH_TAGS = ("eng", "en")
# Tags that assert nothing about the language. A title has to carry the evidence instead.
UNKNOWN_TAGS = ("", "und")

# `reason` values on Selection. Call sites log or map them; the strings are also the
# vocabulary the parity tests assert, so keep them stable.
REASON_ENG_TAG = "eng-tag"
REASON_TITLE = "title"
REASON_UND_AS_DUB = "und-as-dub"
REASON_FIRST_STREAM = "first-stream"
REASON_NO_AUDIO = "no-audio"
REASON_NO_ENGLISH = "no-english-evidence"

# A title is evidence only when it names ENGLISH AUDIO. Some muxes leave the audio language
# tag empty or `und` but keep "English 5.1" / "English Dub" in the title; without that the
# episode is skipped as a sub-only release.
#
# The accept condition is an explicit English token. "dub"/"dubbed" is supporting evidence
# only: it never tells us WHICH language the dub is in, so a bare-dub title is accepted only
# when it names no other language. An external review (2026-11-20, v0.4.11) showed the old
# single regex accepting "Spanish Dub" and, first-match-wins, choosing it over "English Dub"
# while English ASR transcribed the Spanish track; "Japanese (Eng subs) stereo" was picked
# for the same reason. Both paths produced subtitles that could pass the gates silently.
ENGLISH_TITLE = re.compile(r"\beng(?:lish)?\b", re.I)
DUB_TITLE = re.compile(r"\bdub(?:bed)?\b", re.I)

# A title that names subtitles describes the subtitle mux, not the audio behind it, so
# "English subs" is not evidence that a track is the English dub.
SUBTITLE_TITLE = re.compile(r"\b(?:subs?|subbed|subtitles?|subtitled|closed captions?|cc)\b", re.I)

# A commentary track is never the dub, whatever its tag or title says.
COMMENTARY_TITLE = re.compile(r"\bcommentar(?:y|ies)\b", re.I)

# Titles that name another language are evidence AGAINST this being the English dub. Kept as
# one maintainable tuple: add a language here, not another regex at a call site. Covers the
# tag vocabulary measured across 173 files of the anime library on 2026-11-20 (jpn/eng/spa
# tags with "Japanese (Korean Names)" style titles) plus the usual dub markets.
OTHER_LANGUAGE_NAMES = (
    "arabic",
    "bengali",
    "cantonese",
    "castilian",
    "chinese",
    "czech",
    "danish",
    "dutch",
    "filipino",
    "finnish",
    "french",
    "farsi",
    "german",
    "greek",
    "hebrew",
    "hindi",
    "hungarian",
    "indonesian",
    "italian",
    "japanese",
    "korean",
    "malay",
    "mandarin",
    "norwegian",
    "persian",
    "polish",
    "portuguese",
    "romanian",
    "russian",
    "spanish",
    "swedish",
    "tagalog",
    "tamil",
    "telugu",
    "thai",
    "turkish",
    "ukrainian",
    "urdu",
    "vietnamese",
)
OTHER_LANGUAGE_TITLE = re.compile(r"\b(?:" + "|".join(OTHER_LANGUAGE_NAMES) + r")\b", re.I)


class MediaProbeError(Exception):
    """ffprobe could not report the file's streams (missing file, no ffprobe, bad JSON)."""


@dataclass(frozen=True)
class Selection:
    """The chosen audio stream index (or None) and why.

    ``title`` is the chosen stream's title, for a log line that says what was picked.
    ``reason`` is one of the REASON_* constants. ``detail`` is the human-readable evidence
    (why a title was accepted) and ``skipped`` names other title candidates that lost, so a
    wrong pick is visible in lastrun rather than silent.
    """

    index: int | None
    reason: str
    title: str = ""
    detail: str = ""
    skipped: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.index is not None


def probe_audio_streams(video: str, timeout: float) -> list[dict]:
    """Audio streams of ``video`` as ffprobe reports them (index + language/title tags).

    Raises :class:`MediaProbeError` so a caller can distinguish "the file could not be
    probed" (a 502 for the preview, a skip for the pipeline) from "no English track"
    (the preview's 422).
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index:stream_tags=language,title",
                "-of",
                "json",
                video,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise MediaProbeError(f"ffprobe failed: {e}") from e
    if getattr(result, "returncode", 0) != 0:
        raise MediaProbeError(f"ffprobe exited {result.returncode}: {(result.stderr or '').strip()}")
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except (AttributeError, ValueError) as e:
        raise MediaProbeError(f"ffprobe returned unreadable JSON: {e}") from e
    return streams if isinstance(streams, list) else []


def audio_streams(streams) -> list[dict]:
    """The audio entries of an ffprobe stream list.

    ``-select_streams a`` output carries no ``codec_type``; a full ``-show_streams`` list
    does. Both are accepted, so a caller can pass whichever probe it already ran.
    """
    return [s for s in streams if isinstance(s, dict) and s.get("codec_type", "audio") == "audio"]


def stream_lang(stream: dict) -> str:
    return ((stream.get("tags") or {}).get("language") or "").lower()


def stream_title(stream: dict) -> str:
    return (stream.get("tags") or {}).get("title") or ""


def is_commentary(title: str) -> bool:
    return COMMENTARY_TITLE.search(title or "") is not None


def title_english_evidence(title: str) -> tuple[bool, str]:
    """(accept, reason) for an untagged audio stream's title.

    Explicit English audio wins; a bare "dub"/"dubbed" is accepted only when the title names
    no other language and does not describe subtitles. Everything else is refused, which
    leaves the stream to the opt-in lone-untagged path or a sub-only skip.
    """
    if not title or not title.strip():
        return False, "no title"
    other = OTHER_LANGUAGE_TITLE.search(title)
    if other:
        return False, f"title names another language ({other.group(0)!r})"
    names_subtitles = SUBTITLE_TITLE.search(title) is not None
    if ENGLISH_TITLE.search(title):
        if names_subtitles and not DUB_TITLE.search(title):
            return False, "title names subtitles, not audio"
        return True, "title names English audio"
    if DUB_TITLE.search(title):
        if names_subtitles:
            return False, "title names subtitles, not audio"
        return True, "bare dub title naming no other language"
    return False, "title names no English audio or dub"


def und_as_dub() -> bool:
    """UND_AUDIO_AS_DUB: treat a LONE untagged audio track as the English dub.

    Read per call, like every other env knob, so a stored setting takes effect on the next
    show without a restart. Default off: a single untagged track can just as easily be a
    Japanese-only release.
    """
    return os.environ.get("UND_AUDIO_AS_DUB", "") not in ("", "0")


def require_eng() -> bool:
    """REQUIRE_ENG: refuse to guess when no track carries English evidence (default on)."""
    return os.environ.get("REQUIRE_ENG", "1") == "1"


def select_english_audio(streams, *, und_override=None, require_override=None) -> Selection:
    """Pick the English-dub audio stream out of an ffprobe stream list.

    Order of evidence, strongest first:
      1. an ``eng``/``en`` tag on a non-commentary track;
      2. an ``und``/untagged non-commentary track whose TITLE names English audio;
      3. the only audio track, when it is untagged and UND_AUDIO_AS_DUB is on;
      4. the first audio track, when REQUIRE_ENG is off (a pre-filtered English collection).

    The keyword overrides exist for tests; production callers omit them and get the env.
    """
    und = und_as_dub() if und_override is None else und_override
    req = require_eng() if require_override is None else require_override
    audio = audio_streams(streams)

    tagged = [s for s in audio if stream_lang(s) in ENGLISH_TAGS and not is_commentary(stream_title(s))]
    if tagged:
        return Selection(tagged[0]["index"], REASON_ENG_TAG, stream_title(tagged[0]))

    titled = []
    for stream in audio:
        if stream_lang(stream) not in UNKNOWN_TAGS:
            continue
        title = stream_title(stream)
        if is_commentary(title):
            continue
        accepted, why = title_english_evidence(title)
        if accepted:
            titled.append((stream, title, why))
    if titled:
        # Several title matches: the explicitly-English one wins, whatever stream order says
        # (a bare "Dub" ahead of "English Dub" must not win by position).
        explicit = [c for c in titled if ENGLISH_TITLE.search(c[1])]
        chosen, title, why = (explicit or titled)[0]
        skipped = tuple(f"index {s['index']} {t!r}" for s, t, _ in titled if s is not chosen)
        return Selection(chosen["index"], REASON_TITLE, title, why, skipped)

    if und and len(audio) == 1 and stream_lang(audio[0]) in UNKNOWN_TAGS and not is_commentary(stream_title(audio[0])):
        return Selection(audio[0]["index"], REASON_UND_AS_DUB, stream_title(audio[0]))

    if not audio:
        return Selection(None, REASON_NO_AUDIO)
    if not req:
        return Selection(audio[0]["index"], REASON_FIRST_STREAM, stream_title(audio[0]))
    return Selection(None, REASON_NO_ENGLISH)
