#!/usr/bin/env python3
"""Which part of a subtitle track is the opening or ending, not dialogue.

Two independent signals, used in that order:

  * **style** -- an ASS event styled ``OP``, ``Opening``, ``ED``, ``Ending``, or one of the
    per-language karaoke variants fansubbers build (``ED Romaji``, ``ED Kanji``,
    ``OP Theme``). This is the release telling us what the region is.
  * **position and density** -- with no styles (SRT, or a stripped ASS) an OP/ED is still
    visible as a run of cues much denser than the file's median, starting in the first fifth
    of the video or ending in the last fifth, and lasting the length of a song.

Measured against the artifacts in ``tests/fixtures/artifacts`` (2026-11-22), the style
signal is a *prior*, not a fact:

  * ``e86_s01e01_presentation.ass`` styles 1,260 karaoke events ``Opening`` and places them
    at 1326.2-1393.4 s of a 1,416.9 s episode -- the ending. A detector that trusts the
    name reports an opening 22 minutes late; this module reports the region with
    ``conflict=True`` and both readings, and the caller decides.
  * ``toradora_s01e24_presentation.ass`` styles the ending ``ED English`` / ``ED Kanji`` /
    ``ED Romaji`` -- three parallel per-language tracks, 1,214.9-1,345.5 s. That is a
    130.6 s region, so a 120 s ceiling (the obvious guess) would have rejected a real ED.
  * ``csm_s01e01_dubtitles.ass`` carries 251 ``Dubtitles`` events and no styles at all --
    the negative case: nothing here is an OP, and the density pass must not invent one.

Nothing is rewritten or dropped here: the region list is an answer for a consumer that
wants to treat songs differently (``song_spans`` already excludes them from the dialogue
checks; this module names the *slot* the songs sit in, which is what a lyric lookup or a
signs merge needs).

Stdlib only, and no import of ``common``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A style's *family* by prefix, because fansubbers build one style per language and per
# effect layer: "ED Romaji", "ED Kanji", "OP Theme", "Opening 2". Exact equality (the
# obvious port) misses every one of those -- it missed toradora's three ED styles.
_OP_STYLE = re.compile(r"^(?:op|opening)\b", re.IGNORECASE)
_ED_STYLE = re.compile(r"^(?:ed|ending)\b", re.IGNORECASE)

# Song lengths in the artifacts: e86's OP-as-ED is 67.2 s, toradora's ED is 130.6 s. A
# normal TV OP is 90 s and an ED 90-120 s, so the bounds have to be wider than the norm --
# these are the measured extremes with room either side, not the textbook 60-120 s.
MIN_OP = 45.0
MAX_OP = 150.0
MIN_ED = 60.0
MAX_ED = 180.0

# A karaoke line is a group of events sharing one window; they are one region, not 30. The
# gap is an instrumental break between lyric lines, not a lyric gap: toradora's ED is 111
# events whose largest gap is 3.45 s, so a 3 s join splits one song into two regions.
_STYLE_GAP = 6.0
# Position: an OP starts in the first fifth of the video (after a cold open), an ED ends in
# the last fifth. Both are checked against the video's own duration when it is known.
_HEAD_SHARE = 0.20
_TAIL_SHARE = 0.20

# Density pass: bins smaller than a cue, big enough that a typewriter line is one hit.
_BIN = 2.0
_MIN_REGION_EVENTS = 8
_DENSITY_RATIO = 3.0

EVENT_TIME = re.compile(r"^Dialogue:[^,]*,([^,]+),([^,]+),([^,]+),", re.MULTILINE)


@dataclass(frozen=True)
class Region:
    """One opening or ending region, with the evidence that produced it."""

    kind: str
    start: float
    end: float
    evidence: str
    conflict: bool = False
    position: str = ""

    @property
    def length(self):
        return self.end - self.start

    def describe(self):
        return {
            "kind": self.kind,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "length": round(self.length, 2),
            "evidence": self.evidence,
            "conflict": self.conflict,
            "position": self.position,
        }


def _clock(stamp):
    parts = stamp.strip().split(":")
    if len(parts) != 3:
        raise ValueError(stamp)
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def parse_events(text):
    """``[(start, end, style), ...]`` from ASS text; ``parse_srt_events`` for SRT.

    The style column is field 3 after ``Dialogue:`` in the common layout; a release with a
    different ``Format:`` order would need its own reader, and guessing there would be worse
    than the empty list this returns.
    """
    out = []
    for match in EVENT_TIME.finditer(text):
        try:
            start, end = _clock(match.group(1)), _clock(match.group(2))
        except ValueError:
            continue
        if end >= start:
            out.append((start, end, match.group(3).strip()))
    return out


_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def parse_srt_events(text):
    """``[(start, end, None), ...]`` from SRT/VTT: no styles to read, position only."""
    out = []
    for match in _SRT_TIME.finditer(text):
        g = match.groups()
        start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000.0
        end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000.0
        out.append((start, end, None))
    return out


def _family(style):
    if not style:
        return None
    if _OP_STYLE.match(style):
        return "op"
    if _ED_STYLE.match(style):
        return "ed"
    return None


def _bounds(kind):
    return (MIN_OP, MAX_OP) if kind == "op" else (MIN_ED, MAX_ED)


def _position_of(start, end, duration):
    """``head``, ``tail`` or ``middle`` -- the region's slot relative to the video."""
    if not duration:
        return ""
    if start <= _HEAD_SHARE * duration:
        return "head"
    if end >= (1.0 - _TAIL_SHARE) * duration:
        return "tail"
    return "middle"


def _style_regions(events, duration):
    by_family = {}
    for start, end, style in events:
        family = _family(style)
        if family is None:
            continue
        by_family.setdefault(family, []).append((start, end, style))
    regions = []
    for family, items in by_family.items():
        items.sort()
        groups = []
        for start, end, style in items:
            if groups and start - groups[-1][-1][1] <= _STYLE_GAP:
                groups[-1].append((start, end, style))
            else:
                groups.append([(start, end, style)])
        low, high = _bounds(family)
        for group in groups:
            start = min(item[0] for item in group)
            end = max(item[1] for item in group)
            if not low <= end - start <= high:
                continue
            position = _position_of(start, end, duration)
            # The name is the prior; the slot is the evidence. "Opening" in the last fifth
            # is either a reused OP or a misnamed style -- both are worth reporting, and
            # neither is worth silently relabelling.
            conflict = (family == "op" and position == "tail") or (family == "ed" and position == "head")
            styles = sorted({item[2] for item in group})
            evidence = "style:" + ",".join(styles)
            regions.append(Region(family, start, end, evidence, conflict, position))
    regions.sort(key=lambda region: region.start)
    return regions


def _density_regions(events, duration):
    """Unstyled *typeset* OP/ED by cue density, used only when the styles said nothing.

    This is a typesetting signal: e86's karaoke averages 19 events/s inside its song.
    A plain SRT has one cue per line of dialogue, so its OP is not denser than a talky
    scene and this pass correctly finds nothing -- ``song_spans`` is the tool for that
    case, because content is the only evidence a flat SRT carries.
    """
    if not events or not duration:
        return []
    bins = {}
    for start, _end, _style in events:
        bins[int(start // _BIN)] = bins.get(int(start // _BIN), 0) + 1
    if not bins:
        return []
    # The baseline is the whole timeline, empty bins included: a two-minute song in a
    # 24-minute episode occupies 35 of 720 bins, and a median over *occupied* bins would be
    # dragged up to the song's own density, which hides the song it was meant to find.
    span = int(duration // _BIN) + 1
    counts = sorted(bins.get(key, 0) for key in range(span))
    median = counts[len(counts) // 2] or 1
    loud = sorted(key for key, count in bins.items() if count >= _DENSITY_RATIO * median)
    if not loud:
        return []

    runs = []
    for key in loud:
        if runs and key - runs[-1][-1] <= 2:
            runs[-1].append(key)
        else:
            runs.append([key])

    regions = []
    for run in runs:
        start = run[0] * _BIN
        end = (run[-1] + 1) * _BIN
        held = sum(1 for s, _e, _st in events if start <= s < end)
        if held < _MIN_REGION_EVENTS:
            continue
        position = _position_of(start, end, duration)
        if position == "head" and MIN_OP <= end - start <= MAX_OP:
            regions.append(Region("op", start, end, f"density:{held}_events", False, position))
        elif position == "tail" and MIN_ED <= end - start <= MAX_ED:
            regions.append(Region("ed", start, end, f"density:{held}_events", False, position))
    return regions


def detect(events, duration=None):
    """Opening/ending regions in a cue list, one per kind, style evidence first.

    ``events`` is ``[(start, end, style)]`` from ``parse_events``/``parse_srt_events``.
    ``duration`` (the video's, not the track's) is optional; without it the position tests
    are skipped, so style regions are still reported and the density pass is not run.
    """
    found = _style_regions(events, duration)
    density = [] if duration is None else _density_regions(events, duration)
    kinds = {region.kind for region in found}
    found.extend(region for region in density if region.kind not in kinds)
    found.sort(key=lambda region: region.start)
    return found


def describe(events, duration=None):
    """Compact JSON-safe summary, for a run report or an audit line."""
    return [region.describe() for region in detect(events, duration)]
