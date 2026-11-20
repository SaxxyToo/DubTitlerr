#!/usr/bin/env python3
"""Arm #5: non-dialogue boilerplate / credit cue filter (companion to hallucination.py).

Whisper occasionally writes text nobody spoke: video-platform boilerplate
("Thanks for watching!"), release credits ("Subtitles by the Amara.org community",
"Original Script: Kitsune-HentaiBot [...]"), and whole disclaimer blocks. The four
existing arms are all repetition detectors -- repeated n-grams, repeated cards, repeated
single-token cards, repeated intra-token units -- and this text is UNIQUE, so nothing
loops and no existing arm can see it. Afro Samurai S01E01 shipped four cues of it before
any dialogue ("This video is a derivative work of the Touhou Project", ...).

Two deliberate restrictions keep this arm from eating real dialogue:

1. A cue matches only when its WHOLE text is the template, punctuation aside. The library
   contains a trailing cue "Thanks for watching him." (Demon Slayer S04E01, the last cue
   of the episode) and mid-scene dialogue "Others support us silently." (FMA:B S01E18).
   A substring match would drop the first and flag the second; an anchored match touches
   neither. Anchoring is the false-positive guard, not a tidiness preference.

2. Only a LEADING run (before the first non-matching cue) or a TRAILING run (after the
   last) is dropped -- the positions a hallucinated intro/outro can occupy. A mid-file
   match is counted and audited, never dropped: mid-file boilerplate text has been seen
   as a real subtitle credit at cue 4 of 400 (Vinland Saga S01E13), and dropping credits
   from the middle of an episode is a mux-grade change this filter must not make on a
   template's say-so.

The template list lives here, once. tools/artifact_sweep.py imports it so the offline
sweep and the pipeline cannot disagree about what boilerplate is.

Pure stdlib, deterministic. Card dicts are {start, end, text, ...}; extra keys are
carried through untouched. Built with help of Claude (Anthropic).
"""

from __future__ import annotations

import re

# Full-cue templates, case-insensitive. Each is anchored by normalize() + fullmatch(),
# so it must match the complete cue text and nothing else. Keep a template only if the
# whole line can never be spoken dub dialogue:
#
#   "Thanks for watching him." is dialogue -- the template is "thanks for watching",
#   which does not full-match the longer line. "See you next time" is deliberately
#   absent although the blocklist carries it: people say it, and a trailing "See you
#   next time!" is a plausible real sign-off.
TEMPLATES = (
    # intro/outro boilerplate
    r"this video is a derivative work of the \w+ project",
    r"i'?m sorry for the (poor quality|lack of subtitles)",
    r"i hope you enjoy the video",
    r"enjoy the (video|episode)",
    r"thanks? for watching",
    r"thank you for watching",
    r"(please|like and) subscribe",
    r"don'?t forget to subscribe",
    r"subscribe to (my|our|the) channel",
    # subtitle credits
    r"subtitles by .+",
    r"captions? by .+",
    r"transcri(bed|ption) by .+",
    r"translated by .+",
    r"original script: .+",
    r"amara\.org",
    # bare URLs, which are never spoken
    r"(www\.|https?://)\S+",
)

PATTERN = re.compile("|".join(f"(?:{t})" for t in TEMPLATES), re.I)

# A run longer than this is not the intro/outro shape this arm knows how to remove. Twelve
# consecutive matching cues is a LOOP (arms #1-#4 territory) or a template that has drifted
# too broad; either way, dropping it here would be a mux-grade decision made by a filter
# that has no business making it. Counted and left in place.
MAX_RUN = 6

_EDGE = re.compile(r"^[^a-z0-9]+|[^a-z0-9]+$")


def normalize(text: str) -> str:
    """Fold a cue to its matchable core: whitespace collapsed, casefolded, edge
    punctuation removed.

    Only the EDGES: "Original Script: ..." keeps its colon, and the internal
    apostrophe in "I'm" survives so the template's ``i'?m`` reads naturally. Interior
    punctuation is left alone deliberately -- stripping it would make "Thanks, for
    watching" match, which is a spoken sentence shape.
    """
    folded = " ".join((text or "").split()).casefold()
    return _EDGE.sub("", folded)


def is_boilerplate(text: str) -> bool:
    """True when the WHOLE cue is a known non-dialogue boilerplate/credit template."""
    return bool(PATTERN.fullmatch(normalize(text)))


def drop_indices(cards: list[dict], rec=None) -> tuple[set[int], list[dict]]:
    """Indices of the LEADING and TRAILING boilerplate runs, plus one audit record per
    matched cue.

    Audit records are ``{index, start, end, text, run, dropped}`` with ``run`` one of
    ``leading`` / ``trailing`` / ``midfile``. ``dropped`` is False for every mid-file
    match and for a run refused by MAX_RUN -- the record says what was seen and what was
    done, not only what was removed.

    ``rec`` (a qc.Recorder or None) gets ``dropped_boilerplate`` and
    ``boilerplate_midfile`` counters and one event per drop. Counters are optional so
    tools/ and tests can call this bare.
    """
    flags = [is_boilerplate(c.get("text", "")) for c in cards]
    n = len(cards)
    lead = 0
    while lead < n and flags[lead]:
        lead += 1
    trail = 0
    while trail < n - lead and flags[n - 1 - trail]:
        trail += 1
    # Never empty the episode: all-matching means the template is wrong about this file,
    # not that the file has no dialogue. Refuse both runs and let the warnings show.
    if lead + trail >= n:
        lead = trail = 0
    if lead > MAX_RUN:
        lead = 0
    if trail > MAX_RUN:
        trail = 0
    drops = set(range(lead)) | set(range(n - trail, n))

    audits: list[dict] = []
    mid = 0
    for index, c in enumerate(cards):
        if not flags[index]:
            continue
        if index in drops:
            run = "leading" if index < lead else "trailing"
        else:
            run = "midfile"
            mid += 1
        audit = {
            "index": index,
            "start": c.get("start"),
            "end": c.get("end"),
            "text": " ".join(c.get("text", "").split()),
            "run": run,
            "dropped": index in drops,
        }
        audits.append(audit)
        if rec is not None:
            if audit["dropped"]:
                # a dropped cue exists nowhere else in the sidecar: name it, and name it
                # in the priority tier so a common event class cannot crowd it out.
                rec.event(
                    priority=True,
                    reason="boilerplate",
                    card_index=index,
                    start=c.get("start"),
                    end=c.get("end"),
                    text=audit["text"],
                    run=run,
                )
            else:
                rec.event(reason="boilerplate", card_index=index, start=c.get("start"),
                          end=c.get("end"), text=audit["text"], run=run)
    if rec is not None:
        rec.count("dropped_boilerplate", len(drops))
        if mid:
            rec.count("boilerplate_midfile", mid)
    return drops, audits
