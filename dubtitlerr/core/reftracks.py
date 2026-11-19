r"""Classify a video's embedded subtitle tracks as candidate repair *references*.

A repair reference is text the dub pipeline may consult to fix a garbled ASR line.
Two axes decide whether a track can serve that role, and they are independent:

* **provenance** — ``ours`` when the track name is exactly ``common.TRACK_NAME``
  ("Dubtitles"), i.e. a track this pipeline muxed itself; ``native`` otherwise
  (shipped by the release, predates our ops). ``ours`` is never a reference: it is
  the artifact, not an independent witness. The existing exclusion in
  ``common.eng_sub_tracks()`` stays exactly as it is; this module repeats the same
  predicate (``common.is_our_track``) so the two cannot drift, and reports the
  track instead of silently dropping it.
* **role** — ``dub`` (captions of the English dub itself; a *strong* witness after
  dub-verification because it carries the dub's actual wording), ``translation``
  (a separate localized translation; a disambiguation anchor only, because dub and
  sub word things differently), or ``signs`` (signs/songs/forced — not dialogue,
  never a repair reference).

Role rule
---------
The title is the prior, the content is the evidence:

===========================  ============  ===================
title evidence               role          provisional?
===========================  ============  ===================
``common.SIGNS_TITLE``       signs         no — a track that
matches ("Signs & Songs",                  names itself as
"S&S", "Forced", "Songs")                  signs is signs
an explicit dub marker       dub           no
(``dub``, ``dubtitle``,
``dubbed``)
SDH / CC / dialog(ue) only   dub           yes — "English
(e.g. "English (SDH)",                     (SDH)" or
"Dialog - ENG",                            "English / ASS
"English / ASS (SDH)")                     (SDH)" may be the
                                           dub's captions or a
                                           translated SDH
any other title              translation   yes — nothing
(``Full Subtitles [GJM]``,                 names the dub
``English``)                               axis, so the
                                           role is a prior
empty title                  translation   yes — nothing
                                           to go on
===========================  ============  ===================

When ``sample_text`` (the episode's dub ASR transcript) is supplied every
non-``ours`` non-``signs`` track is confirmed by word-bigram overlap: the share of
the track's distinct adjacent word pairs that also occur in the sample. A
dub-caption track repeats the dub's phrasing, so its pairs mostly match; an
independent translation paraphrases the same scene and reuses almost none of them.
Single words are useless here — paraphrases share most of their vocabulary — which
is why the measure is bigrams. Measured against each episode's own dub sidecar:
DAN DA DAN dub captions 0.70 vs translation 0.17, Death Note 0.78 vs 0.20-0.26,
Vinland 0.87 vs 0.42, JJK S3 0.91 vs 0.55 (borderline). ``OVERLAP_MIN`` is the
split.

The measurement decides the role, with one asymmetry: an explicit dub title
(``Dub``, ``Dubtitle``) is only overturned by a clearly low score
(``OVERLAP_KEEP_MIN``), because ASR spelling drift costs a genuine dub-caption
track recall while never teaching a paraphrase the dub's word pairs. Provisional
titles flip at ``OVERLAP_MIN``. ``role_confirmed`` records whether the title was
explicit or the content was measured; ``role_overlap`` holds the score (``None``
when unmeasured). Without ``sample_text`` the role is the title guess and
``role_confirmed`` is False for the provisional rows above.

Reading
-------
``ass``/``ssa``/``subrip`` tracks are extracted with ``-c:s copy`` into a scratch
file (``common.extract_sub``, which retries as a re-encode when the copy fails);
``mov_text`` cannot be copied into an srt container, so it is transcoded
(``-c:s srt``) first. Parsing goes through pysubs2 and keeps spoken dialogue only,
giving ``(start_s, end_s, text)`` spans like ``common.dialogue_intervals()``.

The dialogue filter here is deliberately NOT ``common.is_dialogue_event``: that
predicate rejects any style whose name contains ``title`` or ``note``, which is
right for fansub tracks but deletes real dub-caption tracks whose style is called
``Dubtitle`` (Vinland) or ``Death Note Dialogue`` (Death Note) — every event, i.e.
the exact tracks this module exists to find. Signs/songs are excluded by style
name (sign|song|karaoke|lyric|romaji|kashi|insert|credit, plus the shared OP/ED
token rule), by karaoke and drawing tags, and by ``\pos``/``\move``; top-positioned
``\an`` dialogue is kept. Bitmap codecs (PGS, VobSub) are not text and are ignored;
a per-stream extraction/parse failure skips that track and is never fatal.

Only streams in ``sub_langs`` (default ``common.SUB_LANGS``) are considered:
references are English text by definition elsewhere in the pipeline.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass

import pysubs2

from dubtitlerr.core.common import (
    DROP_STYLE,
    KARAOKE,
    SIGNS_TITLE,
    SONG_FAMILY_STYLE,
    SUB_LANGS,
    extract_sub,
    is_our_track,
    log,
    stream_title,
)

PROVENANCE_NATIVE = "native"
PROVENANCE_OURS = "ours"

ROLE_DUB = "dub"
ROLE_TRANSLATION = "translation"
ROLE_SIGNS = "signs"

# Title markers, deliberately narrow. A dub caption track calls itself one of these;
# SDH/CC/dialog merely says "captions of speech", which is why it is only provisional.
DUB_TITLE = re.compile(r"\bdub(?:bed|titles?)?\b", re.I)
CAPTION_TITLE = re.compile(r"\bsdh\b|\bcc\b|\bdialog(?:ue)?\b", re.I)

# ffprobe codec_name -> scratch extension for the text codecs pysubs2 can read.
TEXT_CODECS = {"ass": ".ass", "ssa": ".ass", "subrip": ".srt", "mov_text": ".srt"}

# Word-bigram overlap at or above which a track's phrasing counts as the dub's.
# Measured against each episode's dub sidecar: dub captions 0.62-1.00, translations
# 0.14-0.55 (see the module docstring; one untitled JJK S3 track sits at 0.548,
# deliberately left on the translation side of the split).
OVERLAP_MIN = 0.55
# An explicit Dub/Dubtitle title is only overturned below this: measurement must be
# decisive to beat a title that names the axis.
OVERLAP_KEEP_MIN = 0.30

# Below this many distinct sample words the score is noise (a stub transcript, a
# single card), so the title prior stands rather than flipping the role.
MIN_SAMPLE_TOKENS = 40

_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")

# Styles that are signs/songs rather than spoken dialogue. Narrower than
# common.DIALOGUE_EXCLUDE_STYLE on purpose: "title" and "note" are not here because
# "Dubtitle" and "Death Note Dialogue" are dialogue style names in this library.
SIGN_STYLE = re.compile(r"sign|song|karaoke|lyric|romaji|kashi|insert|credit", re.I)
# Vector drawing in an event (typeset shapes) and hard positioning (sign placement);
# top/bottom \an alignment is ordinary dialogue placement and stays.
DRAWING = re.compile(r"\\p[1-9]")
POSITIONED_SIGN = re.compile(r"\\pos\(|\\move\(")

# Function words every English sentence shares; leaving them in would make unrelated
# translations look like the dub. Names, slang and contractions stay (they carry the
# signal, and ASR keeps them).
STOPWORDS = frozenset(
    """a an the and or but if of to in on at for with from by as is are was were be been
    being it its this that these those i you he she they we me him her them us my your his
    her their our not no do does did so what who how why when where there here just then
    than too very can could will would should may might must have has had up down out
    about into over after before all any some""".split()
)


@dataclass(frozen=True)
class Event:
    """One plain dialogue cue: seconds from the start of the video plus its text."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Track:
    """One embedded subtitle track and its classification.

    ``events`` is the parsed dialogue (signs/karaoke/positioned cues excluded), in
    file order. ``role_confirmed`` is True only when the title was explicit or the
    overlap measurement decided; see the module docstring.
    """

    index: int
    codec: str
    lang: str
    title: str
    provenance: str
    role: str
    role_confirmed: bool
    role_overlap: float | None
    events: tuple[Event, ...]

    @property
    def is_reference(self) -> bool:
        """Usable as a repair reference: release-native dialogue (not ours, not signs)."""
        return self.provenance == PROVENANCE_NATIVE and self.role != ROLE_SIGNS


def _probe(video: str) -> list[dict]:
    """Subtitle streams as ffprobe reports them; [] on any probe failure."""
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_entries",
                "stream=index,codec_name:stream_tags=language,title",
                "-of",
                "json",
                video,
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=90,
        )
        return json.loads(r.stdout or "{}").get("streams", [])
    except Exception as e:  # noqa: BLE001 - a failed probe means no references, as before
        log("reftracks: ffprobe failed:", video, e)
        return []


def _extract_mov_text(video: str, idx: int, out: str) -> bool:
    """mov_text cannot be ``-c:s copy``-ed into srt, so re-encode it as subrip."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", video, "-map", f"0:{idx}", "-c:s", "srt", out],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=180,
    )
    return os.path.exists(out) and os.path.getsize(out) > 0


def _load_subs(video: str, idx: int, codec: str) -> pysubs2.SSAFile | None:
    """Extract + parse one stream in a scratch dir; None on any failure (never fatal)."""
    ext = TEXT_CODECS[codec]
    try:
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "s" + ext)
            if codec == "mov_text":
                ok = _extract_mov_text(video, idx, out)
            else:
                ok = extract_sub(video, idx, out)
            if not ok:
                return None
            return pysubs2.load(out, encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - one bad stream must not hide the others
        log("reftracks: skipping unreadable stream", idx, "of", video, e)
        return None


def _is_dialogue_event(ev: pysubs2.SSAEvent, txt: str) -> bool:
    """Spoken dialogue, by a rule tuned for reference tracks (see module docstring).

    Excludes comments, signs/songs by style name (the shared OP/ED token rule too),
    karaoke and drawing tags, and ``\\pos``/``\\move`` sign placement. Keeps top/bottom
    ``\\an`` alignment, which dialogue tracks use routinely.
    """
    if ev.is_comment or not txt:
        return False
    raw = ev.text
    if KARAOKE.search(raw) or DRAWING.search(raw) or POSITIONED_SIGN.search(raw):
        return False
    style = ev.style or ""
    if SIGN_STYLE.search(style) or DROP_STYLE.search(style) or SONG_FAMILY_STYLE.search(style):
        return False
    return True


def _dialogue_events(subs: pysubs2.SSAFile) -> tuple[Event, ...]:
    out = []
    for ev in subs.events:
        txt = ev.plaintext.strip()
        if _is_dialogue_event(ev, txt):
            out.append(Event(ev.start / 1000.0, ev.end / 1000.0, txt))
    return tuple(out)


def content_tokens(text: str) -> set[str]:
    """Distinct lowercase content words: punctuation stripped, contractions kept,
    possessives folded to their base ("Mira's" -> "mira"), function words dropped.
    Shared by the scorer and its tests."""
    out = set()
    for tok in _TOKEN.findall(text.casefold().replace("\u2019", "'")):
        if tok.endswith("'s"):
            tok = tok[:-2]
        if tok and tok not in STOPWORDS:
            out.add(tok)
    return out


def content_bigrams(text: str) -> set[tuple[str, str]]:
    """Distinct word bigrams over content tokens, in order, across the whole text.

    Single-word overlap cannot tell a dub caption from a translation of the same
    scene: paraphrases share most content words. Adjacent word pairs survive only
    when the wording itself matches, which is the signal this classifier needs.
    """
    toks = [t for t in _TOKEN.findall(text.casefold().replace("\u2019", "'"))]
    toks = [t[:-2] if t.endswith("'s") else t for t in toks]
    toks = [t for t in toks if t and t not in STOPWORDS]
    return set(zip(toks, toks[1:]))


def overlap_score(events: tuple[Event, ...] | list[Event], sample_text: str) -> float | None:
    """Share of the track's distinct content bigrams that also occur in ``sample_text``.

    ``None`` when either side is too thin to measure (fewer than two track words, or
    fewer than MIN_SAMPLE_TOKENS distinct sample words) — the caller keeps the title role.
    """
    if len(content_tokens(sample_text)) < MIN_SAMPLE_TOKENS:
        return None
    track = content_bigrams(" ".join(ev.text for ev in events))
    if not track:
        return None
    sample = content_bigrams(sample_text)
    return len(track & sample) / len(track)


def title_role(title: str) -> tuple[str, bool]:
    """``(role, confirmed)`` from a track title alone.

    ``confirmed`` is False when the title is only a prior rather than evidence: SDH/CC/
    dialog-only titles (they say "captions of speech", not which axis), and any title
    that names neither axis — including empty, "English" and "Full Subtitles [GJM]"
    (a translation prior, but nothing rules out dub captions). Signs and an explicit
    dub marker are the only self-evident titles.
    """
    t = (title or "").strip()
    if SIGNS_TITLE.search(t):
        return ROLE_SIGNS, True
    if DUB_TITLE.search(t):
        return ROLE_DUB, True
    if CAPTION_TITLE.search(t):
        return ROLE_DUB, False
    return ROLE_TRANSLATION, False


def classify(video: str, sample_text: str | None = None, sub_langs=None) -> list[Track]:
    """Classify every text subtitle track of ``video`` that could be a reference.

    Returns one :class:`Track` per accepted-language text stream (including ``ours``
    and ``signs``, which callers filter on ``is_reference``), in ffprobe order. A
    stream that fails to extract or parse is skipped. ``sample_text`` is the dub ASR
    transcript: when given, its word-bigram overlap confirms or overrides the role.
    """
    langs = SUB_LANGS if sub_langs is None else sub_langs
    tracks: list[Track] = []
    for st in _probe(video):
        codec = st.get("codec_name") or ""
        if codec not in TEXT_CODECS:
            continue
        lang = ((st.get("tags") or {}).get("language", "") or "").lower()
        if lang not in langs:
            continue
        title = stream_title(st)
        subs = _load_subs(video, st["index"], codec)
        if subs is None:
            continue
        events = _dialogue_events(subs)
        provenance = PROVENANCE_OURS if is_our_track(title) else PROVENANCE_NATIVE
        role, confirmed = title_role(title)
        overlap = None
        if sample_text is not None and provenance == PROVENANCE_NATIVE and role != ROLE_SIGNS:
            overlap = overlap_score(events, sample_text)
            if overlap is not None:
                if confirmed and role == ROLE_DUB:
                    # explicit Dub/Dubtitle title: only a decisive low score overturns it
                    role = ROLE_DUB if overlap >= OVERLAP_KEEP_MIN else ROLE_TRANSLATION
                else:
                    role = ROLE_DUB if overlap >= OVERLAP_MIN else ROLE_TRANSLATION
                confirmed = True
        tracks.append(
            Track(
                index=st["index"],
                codec=codec,
                lang=lang,
                title=title,
                provenance=provenance,
                role=role,
                role_confirmed=confirmed,
                role_overlap=overlap,
                events=events,
            )
        )
    return tracks


def references(video: str, sample_text: str | None = None, sub_langs=None) -> list[Track]:
    """Just the tracks usable as repair references (see :attr:`Track.is_reference`)."""
    return [t for t in classify(video, sample_text=sample_text, sub_langs=sub_langs) if t.is_reference]
