#!/usr/bin/env python3
"""Does a candidate reference subtitle belong to *this* video?

A reference the pipeline did not receive from the release -- a fansub Japanese track
pulled for a show that shipped none -- is untrusted input. The release's own embedded
track is a witness because it shipped with the video; a downloaded one may be for another
cut, another episode or another season, and nothing in its filename says so. Measured on
Sublarr's reference library: a track for the same video agrees with another language's
track on 85-100 % of line starts (both follow the same speech), a track for another cut
finds 3-28 % at any offset, and a delayed track finds the 85 % at one constant offset
that can simply be undone.

That is the whole method, and it needs no audio, no alignment engine and no subtitle
parser: two tracks of the same video start their lines at the same moments. This module
answers three questions from cue times alone:

  * **agreement** -- what share of the candidate's line starts have a witness line start
    within ``TOLERANCE`` seconds at the best global offset, and what is that offset?
  * **overrun** -- do more cues end past ``duration + max(10 %, 120 s)`` than a stray late
    event explains? A Blu-ray double episode used against a TV-length video does.
  * **coverage** -- does the cue span cover at least ``MIN_COVERAGE`` of the video? Skipped
    for forced/signs tracks, which legitimately cover a few scenes.

Measured here on this repo's own artifacts (``tests/fixtures/artifacts``, 2026-11-22, 28
cross-episode pairs): the worst share between tracks of *different* episodes was 0.60, and
every pair at or above 0.40 was a fixture slice of 5-12 cues -- among tracks long enough to
be a reference (``MIN_CUES``) the worst was 0.32. The same track against itself, shifted by
up to 37 s, scored 1.00 with a lift of +0.94 and recovered the shift exactly. So
``MIN_SHARE = 0.60`` plus ``MIN_LIFT = 0.10`` sits clear of every real negative on this box,
``MIN_CUES`` keeps a stub from passing on coincidence, and ``STRONG_SHARE = 0.85`` names the
case where the candidate is provably the same cut.

Stdlib only, and no import of ``common``: the acquisition path runs under the shell-loop
harness's curated package slice, which has no ``pysubs2``. Cue times are read from the
text with a regex rather than parsed into events -- a 20 MB typeset ASS is scanned once
and never materialised, which is all this question needs.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

# SRT and WebVTT: HH:MM:SS,mmm --> HH:MM:SS,mmm (VTT uses '.', SRT ',' -- both appear).
_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})")
# ASS/SSA: the two time fields after the layer, before the style.
_ASS_TIME = re.compile(r"^Dialogue:[^,]*,([^,]+),([^,]+),", re.MULTILINE)

TOLERANCE = 0.3
# A delay worth undoing is minutes, not hundreds of seconds; beyond this the candidate is
# a different cut and the offset search should not be talked into finding one.
MAX_OFFSET = 90.0
_HIST_BIN = 0.1
# How many candidate starts the offset search looks at. Cross-correlation cost is
# samples x witness cues in the window; 400 x ~220 is instant on a dense 1,700-cue track
# and still gives a peak four times the noise floor.
_OFFSET_SAMPLES = 400

MIN_SHARE = 0.60
STRONG_SHARE = 0.85
# A dense track (1.2 cues/s) scores ~0.72 at *any* offset simply because a witness start
# is never far away, so share alone admits a different episode of the same show. What
# separates a real alignment is that it beats that floor by a wide margin.
MIN_LIFT = 0.10
# A handful of cues can coincide by luck: the 5-cue and 12-cue slices in this repo's
# fixtures scored 0.60 against unrelated episodes. A track that short cannot repair a
# transcript anyway, so it is not a reference whether it agrees or not.
MIN_CUES = 20
MIN_COVERAGE = 0.50
# A late cue is a stray event; a late *region* is another cut. 10 % of an episode or two
# minutes, whichever is larger, is the allowance a normal track needs for a trailing sign.
OVERRUN_SLACK_SHARE = 0.10
OVERRUN_SLACK_SECONDS = 120.0


@dataclass(frozen=True)
class Fit:
    """The agreement between a witness track and a candidate track.

    ``offset`` is how much later the candidate runs than the witness: a candidate delayed
    by 6 s reports ``+6.0``, and ``align()`` subtracts it.
    """

    share: float
    offset: float
    witness_cues: int
    candidate_cues: int
    witness_span: tuple[float, float] = (0.0, 0.0)
    candidate_span: tuple[float, float] = (0.0, 0.0)
    chance: float = 0.0

    @property
    def lift(self):
        """How far the peak beats the same measurement at an arbitrary offset."""
        return self.share - self.chance

    @property
    def usable(self):
        """True when the candidate may be consulted as a reference at all."""
        return (
            self.candidate_cues >= MIN_CUES
            and self.witness_cues >= MIN_CUES
            and self.share >= MIN_SHARE
            and self.lift >= MIN_LIFT
        )

    @property
    def same_cut(self):
        """True when the agreement is strong enough to call it the same cut, not a lookalike."""
        return self.usable and self.share >= STRONG_SHARE

    def describe(self):
        return {
            "share": round(self.share, 3),
            "offset": round(self.offset, 3),
            "chance": round(self.chance, 3),
            "lift": round(self.lift, 3),
            "witness_cues": self.witness_cues,
            "candidate_cues": self.candidate_cues,
            "usable": self.usable,
            "same_cut": self.same_cut,
        }


def _clock(h, m, s, ms="0"):
    # SRT splits milliseconds into their own field; ASS folds them into the seconds
    # ("0:00:01.04"), so the seconds field may already be fractional.
    return int(h) * 3600 + int(m) * 60 + float(s) + int(ms) / 1000.0


def parse_cues(text):
    """``[(start, end), ...]`` from SRT/VTT/ASS text, in file order.

    Reads both formats because a reference arrives as either: Jimaku and Kitsunekko
    ship ASS, most Western providers ship SRT. An ASS file with a ``Format:`` column
    order other than the common one still parses -- only the two time fields are used,
    and they are always fields 2 and 3 after ``Dialogue:``.
    """
    out = []
    for m in _SRT_TIME.finditer(text):
        g = m.groups()
        out.append((_clock(*g[:4]), _clock(*g[4:])))
    if out:
        return out
    for m in _ASS_TIME.finditer(text):
        times = []
        for stamp in m.groups():
            parts = stamp.strip().split(":")
            if len(parts) != 3:
                times = None
                break
            try:
                times.append(_clock(*parts))
            except ValueError:
                times = None
                break
        if times and len(times) == 2 and times[1] >= times[0]:
            out.append((times[0], times[1]))
    return out


def shift(cues, offset):
    """Move every cue by ``offset`` seconds (negative moves earlier)."""
    return [(start + offset, end + offset) for start, end in cues]


def _share_at(sorted_starts, starts, offset, tolerance):
    """The share of ``starts`` that find a witness start within ``tolerance`` of
    ``value - offset``.

    The subtraction is the direction the caller undoes: the candidate runs ``offset``
    seconds later than the witness, so the witness's line for it sits at ``value - offset``.
    """
    if not starts:
        return 0.0
    hits = 0
    for value in starts:
        target = value - offset
        lo = bisect.bisect_left(sorted_starts, target - tolerance)
        hi = bisect.bisect_right(sorted_starts, target + tolerance)
        if hi > lo:
            hits += 1
    return hits / len(starts)


def _best_offset(witness_starts, candidate_starts, max_offset):
    """The offset with the most support, by cross-correlation of the two start lists.

    A nearest-start histogram is not enough on a dense track: at 1.2 cues/s every
    candidate start has *some* witness start within a second, so "nearest difference"
    describes the gaps, not the alignment. Counting every pair inside the search window
    and looking for the one bin (plus its two neighbours, so a true offset cannot split
    across bins) with a real peak is what survives that density.
    """
    if len(candidate_starts) > _OFFSET_SAMPLES:
        stride = max(1, len(candidate_starts) // _OFFSET_SAMPLES)
        sample = candidate_starts[::stride]
    else:
        sample = candidate_starts

    bins = {}
    for value in sample:
        lo = bisect.bisect_left(witness_starts, value - max_offset)
        hi = bisect.bisect_right(witness_starts, value + max_offset)
        for i in range(lo, hi):
            key = int(round((value - witness_starts[i]) / _HIST_BIN))
            bins[key] = bins.get(key, 0) + 1
    if not bins:
        return 0.0

    # Evaluate the three-bin window around every occupied bin that has mass; adjacency
    # catches a peak that straddles two bins because of a 0.05 s stamp difference.
    best_key, best_mass = 0, -1
    for key in bins:
        mass = bins.get(key - 1, 0) + bins.get(key, 0) + bins.get(key + 1, 0)
        if mass > best_mass:
            best_key, best_mass = key, mass
    return best_key * _HIST_BIN


def agreement(witness, candidate, tolerance=TOLERANCE, max_offset=MAX_OFFSET):
    """How well ``candidate``'s line starts match ``witness``'s, at the best offset.

    Both arguments are ``[(start, end), ...]``. ``chance`` is the same share measured
    ``4 * MAX_OFFSET`` seconds away from the peak -- outside any delay worth undoing, so
    it measures what this pair scores by density alone. A real alignment beats it.
    """
    witness_starts = sorted(start for start, _ in witness)
    candidate_starts = sorted(start for start, _ in candidate)
    if not witness_starts or not candidate_starts:
        return Fit(0.0, 0.0, len(witness_starts), len(candidate_starts))

    offset = _best_offset(witness_starts, candidate_starts, max_offset)
    share = _share_at(witness_starts, candidate_starts, offset, tolerance)
    probe = 4 * max_offset
    chance = (
        _share_at(witness_starts, candidate_starts, offset + probe, tolerance)
        + _share_at(witness_starts, candidate_starts, offset - probe, tolerance)
    ) / 2

    span_a = (witness[0][0], max(end for _, end in witness))
    span_b = (candidate[0][0], max(end for _, end in candidate))
    return Fit(share, offset, len(witness_starts), len(candidate_starts), span_a, span_b, chance)


def defects(cues, duration, forced=False):
    """Structural reasons this track cannot belong to a video of ``duration`` seconds.

    Empty when the track is plausible. ``forced`` (signs/songs) skips the coverage test
    but never the overrun test: signs also end when the video ends.
    """
    if not cues:
        return ["empty"]
    if not duration or duration <= 0:
        return []

    slack = max(OVERRUN_SLACK_SHARE * duration, OVERRUN_SLACK_SECONDS)
    late = sum(1 for _, end in cues if end > duration + slack)
    out = []
    # One trailing sign over the credits is normal; a run of them is a different cut.
    if late > max(3, int(0.01 * len(cues))):
        out.append(f"overrun:{late}_cues_past_{duration + slack:.0f}s")

    start = min(start for start, _ in cues)
    end = max(end for _, end in cues)
    if not forced and end - start < MIN_COVERAGE * duration:
        out.append(f"coverage:{(end - start) / duration:.2f}_of_video")
    return out


def describe(cues):
    """Compact JSON-safe summary of a cue list, for a run report or an audit line."""
    if not cues:
        return {"cues": 0, "start": 0.0, "end": 0.0}
    return {
        "cues": len(cues),
        "start": round(min(start for start, _ in cues), 2),
        "end": round(max(end for _, end in cues), 2),
    }


@dataclass
class Verdict:
    """Everything the acquisition path needs to decide about one candidate file."""

    ok: bool
    reasons: list = field(default_factory=list)
    fit: Fit | None = None
    defects: list = field(default_factory=list)

    def describe(self):
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "defects": list(self.defects),
            "fit": self.fit.describe() if self.fit else None,
        }


def check(candidate, *, duration=None, witness=None, forced=False):
    """The full admission test for one downloaded candidate reference.

    Both structural defects and agreement are reported; ``ok`` is the conjunction. A
    candidate is admitted with no witness only when its structure is clean and
    ``duration`` is known -- an untimed track cannot be trusted to repair anything, which
    is the point of asking. A recovered delay is reported but does not block: the caller
    can either undo it (``align``) or use the track's absolute times as they are.
    """
    failures = defects(candidate, duration, forced) if duration else ([] if candidate else ["empty"])
    fit = agreement(witness, candidate) if witness is not None and candidate else None
    reasons = list(failures)
    blocking = list(failures)
    if fit is not None and not fit.usable:
        reason = f"disagrees:{fit.share:.2f}_at_{fit.offset:+.1f}s"
        reasons.append(reason)
        blocking.append(reason)
    elif fit is not None and abs(fit.offset) >= 0.5:
        reasons.append(f"offset:{fit.offset:+.1f}s")
    return Verdict(not blocking, reasons, fit, failures)


def align(candidate, witness):
    """``candidate`` moved onto ``witness``'s clock, or the input when it does not fit.

    The offset is undone, never invented: a track that fails ``MIN_SHARE`` is returned
    unchanged so the caller's admission test is what rejects it.
    """
    fit = agreement(witness, candidate)
    if not fit.usable or abs(fit.offset) < 0.05:
        return candidate, fit
    return shift(candidate, -fit.offset), fit
