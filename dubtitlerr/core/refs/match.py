#!/usr/bin/env python3
"""Which Jimaku entry and file belong to the episode in front of us.

Jimaku's name search is deliberately loose: "Frieren" also returns Dragon Ball, and an
entry lists every file of every episode (Sublarr measured 440 files for one Mushoku Tensei
episode before it added this selection). Handing all of that to the decoder would spend the
run on the wrong show, and picking the first name match would pick season 3.

So two questions, answered separately:

  * **the entry** -- is this the queried series, and is it the queried season? Season is
    read from the title (``Season 2``, ``2nd Season``, ``II``, ``第2期``) because Jimaku
    entries are per-season; an entry naming no season is a season-1 entry.
  * **the file** -- does its name state this episode? Parsed without ``guessit`` (a
    dependency this repo does not carry): ``S01E05``, ``1x05``, ``- 05``, ``E05``,
    ``第5話``, ``05v2``, and ranges like ``01-04``.

A split cour ("Part 2", "Cour 2", 第2クール) renumbers its own episodes, so it is never the
episode the query means unless the query says so.

The matcher never widens a query to make it fit: a file that names no episode is not a
candidate for an episode. ``select`` exists to return *fewer* files, not more.

Stdlib only, no ``guessit``, no import of ``common``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_ROMAN = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "ⅱ": 2, "ⅲ": 3, "ⅳ": 4, "ⅴ": 5}
_SEASON_PATTERNS = (
    re.compile(r"\bseason\s*(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+season\b", re.IGNORECASE),
    re.compile(r"第\s*(\d{1,2})\s*期"),
)
_ROMAN_RE = re.compile(r"(?:^|[\s:])(ii|iii|iv|v|vi|ⅱ|ⅲ|ⅳ|ⅴ)(?=$|[\s:～~])", re.IGNORECASE)
# A split cour of one season: same season, its own episode numbering.
_PART_RE = re.compile(r"\b(?:part|cour)\s*([2-9])\b|第\s*([2-9])\s*クール", re.IGNORECASE)
_MARKERS_RE = re.compile(
    r"\bseason\s*\d{1,2}\b|\b\d{1,2}(?:st|nd|rd|th)\s+season\b|第\s*\d{1,2}\s*期"
    r"|\b(?:part|cour)\s*\d\b|(?:^|(?<=[\s:]))(?:ii|iii|iv|v|vi|ⅱ|ⅲ|ⅳ|ⅴ)(?=$|[\s:～~])",
    re.IGNORECASE,
)

# --- episode numbers, the shapes real subtitle files carry ----------------------------
_PATTERNS = (
    # S01E05 / s1e5 / S01.E05 / S01E05-E06
    re.compile(r"\bS(\d{1,2})[ ._-]*E(\d{1,4})(?:[ ._-]*E?(\d{1,4}))?\b", re.IGNORECASE),
    # 1x05
    re.compile(r"\b(\d{1,2})x(\d{1,4})\b"),
    # " - 05", " - E05", "[05]"
    re.compile(r"(?:^|[\s\-_\[(])E?(\d{1,4})(?:v\d)?(?=[\s\-_\])]|\.|$)"),
    # 第05話 / 第5話
    re.compile(r"第\s*(\d{1,4})\s*[話话集]"),
    # Episode 5 / Ep. 5
    re.compile(r"\b(?:episode|ep|e)\s*\.?\s*(\d{1,4})\b", re.IGNORECASE),
)
_RANGE = re.compile(r"\b(\d{1,4})\s*[-~–]\s*(\d{1,4})\b")
_SEASON_ONLY = re.compile(r"\bS(\d{1,2})\b", re.IGNORECASE)

_LANGUAGE_TAGS = {
    "ja": "ja", "jp": "ja", "jpn": "ja", "japanese": "ja", "日本語": "ja",
    "en": "en", "eng": "en", "english": "en",
    "de": "de", "deu": "de", "ger": "de", "german": "de",
    "fr": "fr", "fra": "fr", "fre": "fr", "french": "fr",
    "es": "es", "spa": "es", "spanish": "es",
    "zh": "zh", "chi": "zh", "chs": "zh", "cht": "zh", "chinese": "zh",
    "pt": "pt", "por": "pt", "russian": "ru", "ru": "ru", "rus": "ru",
}  # fmt: skip
_TAG_GROUP_RE = re.compile(r"\[([^\]]+)\]|(?<=[._])([a-z]{2,8}(?:-[a-z]{2,8})*)(?=[._\[])")

SUBTITLE_EXTS = (".ass", ".srt", ".ssa", ".vtt")
ARCHIVE_EXTS = (".zip", ".rar")


def normalise(text):
    """Case- and punctuation-insensitive form of a title, for comparing two of them."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _names(entry):
    return [name for name in (entry.get("name"), entry.get("english_name"), entry.get("japanese_name")) if name]


def entry_season(entry):
    """The season an entry's title names (``II``, ``Season 2``, ``第2期``), or ``None``."""
    for name in _names(entry):
        text = unicodedata.normalize("NFKC", name)  # "Ⅱ" -> "II", full-width digits
        for pattern in _SEASON_PATTERNS:
            found = pattern.search(text)
            if found:
                return int(found.group(1))
        roman = _ROMAN_RE.search(text)
        if roman:
            return _ROMAN[roman.group(1).lower()]
    return None


def is_later_part(entry):
    """True for the second (or later) cour of a season: ``Part 2``, ``Cour 2``, 第2クール."""
    return any(_PART_RE.search(name) for name in _names(entry))


def bare_title(name):
    """A title with its season/cour markers removed, so seasons compare as one series."""
    return normalise(_MARKERS_RE.sub(" ", name or ""))


def matches_title(entry, titles):
    """True when the entry's title, minus season markers, is one of ``titles``."""
    wanted = [normalise(title) for title in titles if title]
    for name in _names(entry):
        bare = bare_title(name)
        if bare and any(bare.startswith(title) or title.startswith(bare) for title in wanted):
            return True
    return False


def select_entries(entries, *, titles=(), season=None, absolute=False):
    """The entries for this series and season, best first.

    Returns ``entries`` unchanged when no entry carries the title (a differently-localised
    name would otherwise lose every result), so a loose search still finds something. A
    season-1 query keeps season-less entries, because that is what they mean.
    """
    titled = [entry for entry in entries if matches_title(entry, titles)] if titles else list(entries)
    if not titled:
        return list(entries)
    if season is None or absolute:
        return titled
    same = [entry for entry in titled if entry_season(entry) == season]
    unmarked = [entry for entry in titled if entry_season(entry) is None]
    if season == 1:
        same = same + unmarked
    chosen = same or unmarked
    return sorted(chosen, key=is_later_part)


@dataclass(frozen=True)
class EpisodeName:
    """What a subtitle file name says about the episode it holds."""

    season: int | None
    episodes: tuple
    part: int | None

    def describe(self):
        return {"season": self.season, "episodes": list(self.episodes), "part": self.part}


def file_episode(filename):
    """``(season, episodes, part)`` stated by a subtitle file name; empty when it states none."""
    stem = unicodedata.normalize("NFKC", filename or "")
    stem = re.sub(r"\.(?:ass|srt|ssa|vtt|zip|rar)$", "", stem, flags=re.IGNORECASE)

    part = None
    part_match = _PART_RE.search(stem)
    if part_match:
        part = int(part_match.group(1) or part_match.group(2))

    season = None
    season_match = _SEASON_ONLY.search(stem)
    if season_match:
        season = int(season_match.group(1))

    for pattern in _PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue
        groups = [group for group in match.groups() if group is not None]
        if pattern is _PATTERNS[0]:
            season = int(match.group(1))
            episodes = [int(match.group(2))]
            if match.group(3):
                episodes += list(range(int(match.group(2)) + 1, int(match.group(3)) + 1))
            return EpisodeName(season, tuple(episodes), part)
        if pattern is _PATTERNS[1]:
            return EpisodeName(int(match.group(1)), (int(match.group(2)),), part)
        number = int(groups[0])
        if number <= 0:
            continue
        # "Show - 05-06" or "01-04": a range, not a single episode.
        span = _RANGE.search(stem)
        if span and int(span.group(1)) == number:
            low, high = int(span.group(1)), int(span.group(2))
            if 0 < low < high <= low + 12:
                return EpisodeName(season, tuple(range(low, high + 1)), part)
        return EpisodeName(season, (number,), part)
    return EpisodeName(season, (), part)


def file_matches_episode(filename, *, episode, season=None, absolute=False):
    """True when the file name states exactly this episode.

    A multi-episode file only fits when its whole set is the queried one; a stated season
    must be the queried season, unless the number is absolute (absolute numbering carries no
    season). A file that states no episode never matches -- guessing there is how a season
    pack gets used as one episode's witness.
    """
    named = file_episode(filename)
    if not named.episodes or episode not in named.episodes:
        return False
    if len(named.episodes) > 1 and set(named.episodes) != {episode}:
        return False
    if named.season is not None and not absolute and season is not None and named.season != season:
        return False
    return True


def language_from_tags(filename):
    """The one language a file name's tags state; ``None`` when none or several.

    Jimaku tags files ``.ja.srt``, ``[JPN]``, ``[CHS, JPN]`` or ``.ja-en``. A file tagged
    with two languages is not a clean track in either, so it counts as unmarked.
    """
    stem = (filename or "").rsplit(".", 1)[0] + "."
    found = set()
    for bracket, dotted in _TAG_GROUP_RE.findall(stem):
        parts = re.split(r"[,\s/+]+", bracket or dotted)
        tags = {_LANGUAGE_TAGS.get(part.lower()) for part in parts if part}
        if bracket and None in tags:
            continue  # "[1080p]", "[Web]": a bracket that is not a language list
        found |= {tag for tag in tags if tag}
    return found.pop() if len(found) == 1 else None


def is_subtitle(name):
    return (name or "").lower().endswith(SUBTITLE_EXTS)


def is_archive(name):
    return (name or "").lower().endswith(ARCHIVE_EXTS)


def select_files(files, *, episode, season=None, absolute=False, language=None):
    """The files for this episode, best first: the asked language before unmarked, then ASS.

    ``files`` is ``[{"name": str, "url": str, ...}, ...]`` as Jimaku's files endpoint returns
    them. An unmarked file is a candidate because Jimaku's archive is old and inconsistent;
    a file tagged for another language is not.
    """
    wanted = [file for file in files if is_subtitle(file.get("name", "")) or is_archive(file.get("name", ""))]
    matched = [
        file for file in wanted if file_matches_episode(file.get("name", ""), episode=episode, season=season, absolute=absolute)
    ]
    if not matched:
        return []

    def rank(file):
        name = file.get("name", "")
        stated = language_from_tags(name)
        lang_rank = 0 if (language and stated == language) else 1 if stated is None else 2
        fmt_rank = 0 if name.lower().endswith(".ass") else 1 if name.lower().endswith((".srt", ".ssa", ".vtt")) else 2
        return (lang_rank, fmt_rank, name)

    if language:
        matched = [file for file in matched if language_from_tags(file.get("name", "")) in (None, language)]
    return sorted(matched, key=rank)
