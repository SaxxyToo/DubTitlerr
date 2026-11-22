#!/usr/bin/env python3
"""Rebuild a subtitle file's card STRUCTURE and TIMING from its own words.

What this stage owns
--------------------
Cards used to be whatever the ASR emitted: a sentence cut in half at a segment
boundary (`'Gotta say,'` / `'though, with that many of them, ...'`), a stray
`'As'` left alone on screen for seven seconds because its one word carried a
nineteen-second DTW duration, back-to-back cards two frames apart. Nothing in
the pipeline owned sentence structure or display rhythm -- ``reflow`` segmented
whatever the words looked like, and ``repair`` edited text one card at a time.

This module is that owner. It takes the cards a file already shows and the
episode's own ``.dubtitles.words.json`` timeline, and rebuilds the presentation:

  * **Sentence units.** Adjacent cards that continue one sentence are joined
    (the previous card has no terminal mark and the next starts lowercase, or
    the two are literally adjacent words, or the previous card is a 1-2 word
    runt). A unit is split only where its TEXT cannot be one card (the 84-char
    target / 88-char gate ceiling); the cut lands on a sentence end, else a
    clause mark, else the most balanced word boundary -- never at the largest
    pause, which is what cut `'Gotta say,'` off. A slow or long SPAN is not a
    reason to split: the display is clamped to `MAX_DISPLAY` and the rest of
    that speech is left untracked, because `'It'` + `'hurts...'` is a fragment.
  * **Orphans.** A card of one or two words that does not end a sentence is
    absorbed into the sentence it belongs to. A complete one-word line
    (`'Yes.'`) stays a card: it is a line, not a fragment.
  * **Timing from word timestamps.** The word list's durations are used, but not
    trusted blindly: a word stretched past ``STRETCH_WORD_MAX`` (whisper's DTW
    pins a segment's first word to the segment start and lets one token swallow
    the rest of the utterance) makes that segment's words re-spaced across the
    segment's own span, in proportion to token length. Display starts sit
    ``LEAD_IN`` before the first spoken word, end ``LINGER`` after the last,
    reach ``MIN_DISPLAY`` where the gap to the next card allows it, never exceed
    ``MAX_DISPLAY``, and always leave ``MIN_GAP`` -- so back-to-back <80 ms
    "churn" gaps cannot be produced.
  * **Speaker turns.** A card that holds TWO utterances with a real pause between
    them is a merged ping-pong block (`'Where is he?'` / `'Gone.'`). Where the
    evidence is unambiguous it is written as the fansub two-line block -- each
    speaker's line prefixed with ``- `` -- so a reader can tell the mouths apart.
    A card that is one word on each side (`'Pleiades.'` / `'Processor.'`) is left
    alone: one speaker listing two names sounds exactly the same. The dash is a
    MARK, never a word: :func:`content_tokens` strips it, and the word invariant is
    checked on the stripped sequence, so P31's comparison stays mechanical.
  * **Nothing else.** No word is added, removed, reordered, re-spelled or
    re-cased. Punctuation and capitalisation belong to the restoration stage
    (P31). The invariant is enforced mechanically:
    :func:`restructure` refuses (returns the input untouched) unless the
    whitespace-split token sequence of the output equals the input's, and the
    file writer re-checks it before replacing anything on disk.

Fail-closed
-----------
Uncertainty produces the ORIGINAL card, never a guess:

  * a card whose tokens cannot be aligned to the word stream is a barrier -- it is
    never merged, never split, never re-timed, and no unit crosses it;
  * a turn is only marked where a pause, a segment boundary, two complete sentences
    and a two-line layout all agree; everything else keeps its single line;
  * a card that already carries a turn marker is kept VERBATIM and is a barrier --
    a sourced track's own attribution is never re-derived or overwritten;
  * a layout that would need an unpublishable card (over the gate's 88-char /
    46-char-line / 7.5 s ceilings) is abandoned and that unit keeps its cards;
  * no usable ``words.json`` means structure-only: cards may be joined where the
    join is unambiguous, and nothing is re-timed from invented audio positions.
    Turns need word timing, so a structure-only pass inserts no dashes at all.

In-app re-pass
--------------
``PRESENTATION_EPOCH`` is the behaviour stamp, the same discipline as
``gate_fix.GATE_FIX_EPOCH``: bump it for every change to what this module does.
A delivered episode carries ``<stem>.dubtitles.presentation.json`` recording the
epoch it was rebuilt under; ``--sweep`` (run by ``merge_pass.sh`` before its
delivered-file skip) rebuilds any delivered episode whose stamp is missing or
older -- exactly once per epoch, with no operator step and no separate script.

CLI
---
  python3 -m dubtitlerr.pipeline.presentation STEM [--dry-run] [--json]
  python3 -m dubtitlerr.pipeline.presentation --stale STEM      # 0 = stale, 1 = current, 3 = unusable
  python3 -m dubtitlerr.pipeline.presentation --sweep ROOT...   # every delivered file under the roots
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time

from dubtitlerr.core.atomic_file import MODE_KEEP, atomic_write
from dubtitlerr.pipeline import reflow

# --- the display profile ------------------------------------------------------------
# The reflow profile is the TARGET; publish_gate's looser numbers (88 chars, 46-char
# line, 7.5 s) are the hard ceiling we refuse to cross. A plan that cannot reach the
# target but stays inside the ceiling is still an improvement and is kept.
MAX_LINE = reflow.MAX_LINE  # 42
MAX_CHARS = reflow.MAX_CHARS  # 84
MAX_CPS = reflow.MAX_CPS  # 17.0
MIN_DUR = reflow.MIN_DUR  # 0.83
MAX_DISPLAY = reflow.MAX_DUR  # 7.0
MIN_GAP = reflow.MIN_GAP  # 0.083 -- ~2 frames at 24 fps
LEAD_IN = reflow.LEAD_IN  # 0.12
LINGER = reflow.LINGER  # 0.30

# The gate's own ceilings: a card past any of these is refused at publish, so a plan
# that would produce one is abandoned instead.
HARD_CHARS = 88
HARD_LINE = 46
HARD_DUR = 7.5

MIN_DISPLAY = 1.0  # the floor this stage aims for "where achievable"
MERGE_PAUSE = reflow.GAP_MAX  # 0.5 s -- never join cards across a real pause
MAX_UNIT_SPAN = 21.0  # a "sentence" longer than this is several utterances
ORPHAN_WORDS = 2  # a 1-2 word card that does not end a sentence is a fragment
STRETCH_WORD_MAX = 1.5  # a single word claiming longer than this is a DTW artifact
# Below this many visible characters per second the WORD TIMES are not describing the
# speech: a segment that claims 20 seconds for six words is a song/chant or a broken
# DTW, and cutting inside it on word boundaries produces one-word cards. Such a span is
# laid out by BALANCED word count instead, which keeps every card readable.
MIN_TRUST_CPS = 4.0
MAX_RECORDS = 200  # audit detail cap per episode (counters are never capped)

# Speaker turns (P30). A dash is a MARK, never a word -- see content_tokens().
TURN_PREFIX = "- "
TURN_PAUSE = 0.30  # a real pause BETWEEN two speakers, inside one card
TURN_LINE_MAX = MAX_LINE - len(TURN_PREFIX)  # 40: a turn line must fit ONE display line

SENT_END = ".!?…"
CLAUSE = ",;:"
_TRAILING_QUOTES = "\"'”’)]}*"
_TURN_LINE = re.compile(r"^\s*-\s+")

PRESENTATION_EPOCH = 2  # 2 = speaker-turn dashes (P30)
STAMP_SUFFIX = ".dubtitles.presentation.json"


# --- text helpers -------------------------------------------------------------------


def flatten(text: str) -> str:
    """One card's text as a single line: ``\\N``, real newlines and runs of blanks."""
    return re.sub(r"\s+", " ", (text or "").replace("\\N", " ").replace("\\n", " ")).strip()


def tokens(text: str) -> list[str]:
    return flatten(text).split()


def turn_marked(text: str) -> bool:
    """True when this card already carries the fansub ``- `` speaker marker."""
    raw = str(text or "").replace("\\N", "\n").replace("\\n", "\n")
    return any(_TURN_LINE.match(line) for line in raw.split("\n"))


def content_tokens(text: str) -> list[str]:
    """Tokens with the leading speaker marker removed, line by line.

    The P31 invariant hook: the dash is a MARK, not a word, so the word-sequence
    comparison (and the "my rebuild changed no word" check) must be made on the text
    with the marker stripped. Both sides of that comparison go through here, which
    also makes a sourced track's own markers symmetric instead of a token difference.
    """
    out: list[str] = []
    raw = str(text or "").replace("\\N", "\n").replace("\\n", "\n")
    for line in raw.split("\n"):
        toks = line.split()
        if toks and toks[0] == "-":
            toks = toks[1:]
        out.extend(toks)
    return out


def _norm(token: str) -> str:
    """A token reduced to what the word stream and the card text can agree on."""
    return re.sub(r"[^0-9a-z']", "", token.casefold())


def is_terminal(text: str) -> bool:
    """True when this text ends a sentence, ignoring trailing quotes/brackets."""
    t = flatten(text)
    while t and t[-1] in _TRAILING_QUOTES:
        t = t[:-1].rstrip()
    return bool(t) and t[-1] in SENT_END


def _ends_clause(text: str) -> bool:
    t = flatten(text)
    while t and t[-1] in _TRAILING_QUOTES:
        t = t[:-1].rstrip()
    return bool(t) and t[-1] in CLAUSE


def _first_letter(text: str) -> str:
    for ch in flatten(text):
        if ch.isalpha():
            return ch
    return ""


def _wrappable(text: str) -> bool:
    return all(len(ln) <= MAX_LINE for ln in reflow.wrap_balance(text).split("\n"))


def _wrap_card(text: str) -> str:
    """Wrap one card, respecting a turn break the turn detector already placed.

    ``wrap_balance`` would re-flow a two-line turn card as one balanced paragraph and
    throw the attribution away, so each line of an already-broken card is wrapped on
    its own; the break survives because it is the boundary between two speakers.
    """
    if "\n" not in text:
        return reflow.wrap_balance(text)
    return "\n".join(reflow.wrap_balance(line) for line in text.split("\n") if line.strip())


def _repeat_run(text: str) -> bool:
    """True when the card's letters are one character repeated (`'Nooooooo…'`).

    A repeated run is what the ASR does to a scream: the same utterance is split across
    cards by a line break or a stunned pause, and neither half is a word that can be
    aligned. It is the only unmapped text this stage is willing to join across a
    barrier, because the repetition itself proves the two halves are one vocalisation.
    """
    letters = [ch.casefold() for ch in flatten(text) if ch.isalpha()]
    if len(letters) < 2:
        return False
    # A leading consonant is normal (`'Nooooooo'`); the test is whether ONE character
    # dominates the card, which a real two-word line (`'zzz qqq'`, 50/50) cannot do.
    top = max(letters.count(ch) for ch in set(letters))
    return top / len(letters) >= 0.75


def _hard_faults(text: str, dur: float | None = None) -> list[str]:
    """Faults that make a card unpublishable (the gate would refuse the file).

    Duration is only a fault when it is KNOWN to be the display duration; a long
    SPEECH span is not, because the display is clamped to ``MAX_DISPLAY`` by
    :func:`_time_cards` (one card may show part of a slow line and then leave the
    screen blank until the next unit, which is better than splitting a sentence
    into fragments just because the voice is slow).
    """
    flat = flatten(text)
    out = []
    if len(flat) > HARD_CHARS:
        out.append("chars")
    if max((len(ln) for ln in reflow.wrap_balance(flat).split("\n")), default=0) > HARD_LINE:
        out.append("line")
    if dur is not None and dur > HARD_DUR + reflow.EPS:
        out.append("dur")
    return out


# --- the word timeline --------------------------------------------------------------


def repair_word_times(words: list[dict]) -> tuple[list[dict], dict]:
    """Re-space the words of any segment one stretched token has swallowed.

    whisper's word DTW occasionally pins a segment's first word to the segment start
    and gives a later word a start far after it, so ONE token claims most of the
    utterance. Live on 86 S01E01: the word ``As`` spans 93.14 -> 112.62 s (19.5 s),
    which is why a card reading ``As`` was on screen for 7.4 s and the rest of its
    sentence arrived twelve seconds later as a separate card.

    A segment with any word longer than ``STRETCH_WORD_MAX`` and at least three words
    is re-spaced across its OWN [first start, last end] span, in proportion to token
    length. Nothing outside that span moves, so a genuine pause BETWEEN segments is
    untouched -- that is the boundary the pipeline already trusts.
    """
    out = [dict(w) for w in words]
    stats = {"stretched_segments": 0, "stretched_words": 0}
    groups: dict[object, list[int]] = {}
    for i, w in enumerate(out):
        if w.get("start") is None or w.get("end") is None:
            continue
        groups.setdefault(w.get("seg", 0), []).append(i)
    for idxs in groups.values():
        if len(idxs) < 3:
            continue
        durs = [out[i]["end"] - out[i]["start"] for i in idxs]
        if not any(d > STRETCH_WORD_MAX for d in durs):
            continue
        lo, hi = out[idxs[0]]["start"], out[idxs[-1]]["end"]
        if hi <= lo:
            continue
        weights = [len(_norm(out[i].get("text", ""))) + 1.2 for i in idxs]
        total = sum(weights)
        if total <= 0:
            continue
        t = lo
        for i, wt in zip(idxs, weights):
            span = (hi - lo) * wt / total
            out[i]["start"] = t
            out[i]["end"] = t + span
            t += span
        stats["stretched_segments"] += 1
        stats["stretched_words"] += sum(1 for d in durs if d > STRETCH_WORD_MAX)
    return out, stats


# --- alignment: which words a card says ---------------------------------------------


def align(cards: list[dict], words: list[dict]) -> list[tuple[int, int] | None]:
    """For each card, the (first, last) index into ``words`` its text covers, or None.

    The delivered text is the word stream after glossary fixes and repair edits, so it
    is compared token by token over the WHOLE file (a per-card time window misaligns
    the moment one card's display timing was extended). Measured on 86 S01E01: 2,166
    word tokens vs 2,118 delivered, 98.3% identical, every difference a substitution.

    A card maps only when every one of its tokens maps and the mapped indices are
    monotonic -- anything else is None, and an unmapped card is a barrier: no unit
    crosses it and it is never re-timed.
    """
    if not words:
        return [None] * len(cards)
    word_norm = [_norm(w.get("text", "")) for w in words]
    w_idx = [i for i, n in enumerate(word_norm) if n]
    w_seq = [word_norm[i] for i in w_idx]

    flat: list[str] = []
    counts: list[int] = []
    for c in cards:
        n = 0
        for t in tokens(c.get("text", "")):
            nt = _norm(t)
            if nt:
                flat.append(nt)
                n += 1
        counts.append(n)
    if not flat or not w_seq:
        return [None] * len(cards)

    sm = difflib.SequenceMatcher(None, w_seq, flat, autojunk=False)
    mapped: list[int | None] = [None] * len(flat)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal" or (op == "replace" and (i2 - i1) == (j2 - j1)):
            for k in range(i2 - i1):
                mapped[j1 + k] = w_idx[i1 + k]
    out: list[tuple[int, int] | None] = []
    pos = 0
    for n in counts:
        if n == 0:
            out.append(None)
            continue
        chunk = mapped[pos : pos + n]
        pos += n
        if any(x is None for x in chunk) or any(chunk[k] > chunk[k + 1] for k in range(len(chunk) - 1)):
            out.append(None)
            continue
        first, last = chunk[0], chunk[-1]
        # Both are ints, not Optional: the guard above already refused any chunk holding a None or
        # an out-of-order pair. Narrowed with an explicit test rather than an `assert`, which
        # `python -O` strips -- the same hazard the settings registry was hardened for.
        if first is None or last is None:  # pragma: no cover - the guard above refuses any None in the chunk
            out.append(None)
            continue
        out.append((first, last))
    return out


def _speech(card: dict, rng: tuple[int, int] | None, words: list[dict] | None) -> tuple[float, float]:
    """The audio span this card speaks: its own words when known, else its display."""
    if words is not None and rng is not None:
        run = words[rng[0] : rng[1] + 1]
        if run:
            return run[0]["start"], run[-1]["end"]
    return card["speech_start"], card["speech_end"]


# How far the words a card CLAIMS may sit outside the card's own display window. The
# display was extended by up to ~2 s for readability, so a couple of seconds is normal
# drift; ten is evidence the tokens matched a different line.
RANGE_SLACK = 30.0


def validate_ranges(
    cards: list[dict], ranges: list[tuple[int, int] | None], words: list[dict] | None
) -> list[tuple[int, int] | None]:
    """Drop any mapping whose words sit nowhere near the card that claims them.

    A token diff over a whole episode is reliable but not magic: a line the repair lane
    rewrote twice can match the wrong occurrence. A mapping that would move a card's
    speech window seconds away from where the card is displayed is a bad mapping, and
    treating it as None keeps that card a barrier -- the fail-closed direction.
    """
    if not words:
        return ranges
    out: list[tuple[int, int] | None] = []
    for card, rng in zip(cards, ranges):
        if rng is None:
            out.append(None)
            continue
        run = words[rng[0] : rng[1] + 1]
        if not run:
            out.append(None)
            continue
        lo, hi = run[0]["start"], run[-1]["end"]
        if lo < card["speech_start"] - RANGE_SLACK or hi > card["speech_end"] + RANGE_SLACK:
            out.append(None)
            continue
        out.append(rng)
    return out


# --- unit assembly ------------------------------------------------------------------


def _continues(prev: dict, nxt: dict, prev_rng, nxt_rng, speech_gap: float) -> bool:
    """True when ``nxt`` continues the sentence ``prev`` started.

    Only signals that cannot be a new sentence are accepted: the previous card has no
    terminal mark AND one of

      * the next card begins in lowercase -- it cannot start a sentence;
      * the two cards hold literally adjacent word indices -- one utterance cut in
        half by the segmenter (`'Handler 1 to'` / `'Undertaker.'`);
      * the previous card is a 1-2 word runt -- a fragment by construction, and
        absorbing it is the point (`'As'` / `'anyone out'`). A runt is joined when the
        two DISPLAY cards are back-to-back even if the word times claim a pause
        between them: those timings are exactly what produced the seven-second
        `'As'`, so they are not evidence against a visible fragment.

    A card that ends a sentence never absorbs the next one, so `'Yes.'` + `'No.'`
    stay two cards however close together they are.
    """
    if is_terminal(prev.get("text", "")):
        return False
    if len(tokens(prev.get("text", ""))) <= ORPHAN_WORDS:
        # Either evidence of continuity will do: the cards are back-to-back on screen,
        # or their words are contiguous in the audio. The broken sidecar and the broken
        # DTW each lie in one of those two ways, never in both.
        return min(nxt["start"] - prev["end"], speech_gap) <= MERGE_PAUSE + reflow.EPS
    if speech_gap > MERGE_PAUSE + reflow.EPS:
        return False
    if _first_letter(nxt.get("text", "")).islower():
        return True
    return prev_rng is not None and nxt_rng is not None and nxt_rng[0] - prev_rng[1] <= 1


def _join_text(parts: list[str]) -> str:
    return " ".join(p for p in (flatten(x) for x in parts) if p)


def _join_short(prev: dict, nxt: dict, prev_speech: tuple[float, float], nxt_speech: tuple[float, float], gap: float) -> bool:
    """Join two BACK-TO-BACK cards when at least one cannot be read on its own.

    A card spoken for less than ``MIN_DISPLAY`` whose successor starts on the next
    frame cannot be held on screen long enough to read: the two are one visual beat.
    This is the churn rule -- it is the only path that joins across a terminal mark,
    and only when the pair is literally contiguous (< ``MIN_GAP`` apart) and the
    merged card still fits the display budget.
    """
    if gap >= MIN_GAP - reflow.EPS:
        return False
    if prev_speech[1] - prev_speech[0] >= MIN_DISPLAY and nxt_speech[1] - nxt_speech[0] >= MIN_DISPLAY:
        return False
    merged = _join_text([prev["text"], nxt["text"]])
    span = nxt_speech[1] - prev_speech[0]
    return len(merged) <= MAX_CHARS and span <= MAX_DISPLAY and _wrappable(merged)


# --- layout: one unit -> 1..n display cards -----------------------------------------


def _cut_score(toks: list[str], k: int) -> tuple:
    """Rank a cut before ``toks[k]``: sentence end, then clause mark, then balance.

    The old overflow splitter cut at the LARGEST PAUSE, which is what turned one
    sentence into `'Gotta say,'` plus the rest: the 0.47 s breath after a comma is the
    biggest gap in the card and it sits eleven characters in. Balance is measured on
    characters, with a penalty for a half under three words so a stub is never chosen.
    """
    left, right = " ".join(toks[:k]), " ".join(toks[k:])
    prev = toks[k - 1].rstrip()
    rank = 0 if prev.endswith(tuple(SENT_END)) else (1 if prev.endswith(tuple(CLAUSE)) else 2)
    stub = 60 if min(k, len(toks) - k) < 3 else 0
    return (rank, stub, abs(len(left) - len(right)), k)


def _split_text(
    text: str, speech: tuple[float, float], word_run: list[dict] | None
) -> list[tuple[str, tuple[float, float], list[dict] | None]]:
    """Break one unit's text into display cards, recursively, at ranked cuts.

    ``word_run`` (the unit's own words, in order) lets a cut land on a real audio
    boundary; a unit whose tokens and words do not line up 1:1 falls back to dividing
    the span by character share. A piece that already fits the display budget is
    returned as-is -- the recursion stops there, which is what keeps a sentence from
    being shredded into one card per word.
    """
    toks = tokens(text)
    dur = speech[1] - speech[0]
    if len(toks) < 2:
        return [(text, speech, word_run)]
    if len(text) <= MAX_CHARS and _wrappable(text):
        # The text fits ONE card. A long or slow span is not a reason to cut a
        # sentence in half: the display is clamped to MAX_DISPLAY, so a slow line
        # shows what it can and leaves the rest of its speech untracked rather than
        # shipping `'It'` / `'hurts...'` or `'Handler One'` / `'to'` / `'all'`.
        return [(text, speech, word_run)]
    # A span too slow to be real speech gets balanced word counts, not word-time cuts:
    # its own timestamps would carve single words onto the screen for seconds each.
    balanced_only = dur > 0 and reflow.card_cps(text, dur) < MIN_TRUST_CPS
    aligned = word_run is not None and len(word_run) == len(toks)
    min_half = 3 if len(toks) >= 8 else (2 if len(toks) >= 3 else 1)
    if balanced_only:
        k = min_half + (len(toks) - 2 * min_half) // 2
        k = max(min_half, min(k, len(toks) - min_half))
        left, right = " ".join(toks[:k]), " ".join(toks[k:])
        share = len(left) / (len(left) + len(right))
        mid = speech[0] + dur * share
        lr = word_run[:k] if aligned else None
        rr = word_run[k:] if aligned else None
        return _split_text(left, (speech[0], mid), lr) + _split_text(right, (mid, speech[1]), rr)
    for k in sorted(range(1, len(toks)), key=lambda k: _cut_score(toks, k)):
        if min(k, len(toks) - k) < min_half:
            continue  # a cut that leaves a stub is never preferred
        left, right = " ".join(toks[:k]), " ".join(toks[k:])
        share = len(left) / (len(left) + len(right))
        candidates = []
        if aligned:
            candidates.append(word_run[k - 1]["end"] - speech[0])  # type: ignore[index]
        candidates.append(dur * share)
        for d1 in candidates:
            d2 = dur - d1
            if d1 < MIN_GAP or d2 < MIN_GAP:
                continue
            if _hard_faults(left, d1) or _hard_faults(right, d2):
                continue
            mid = speech[0] + d1
            lr = word_run[:k] if aligned else None
            rr = word_run[k:] if aligned else None
            return [(left, (speech[0], mid), lr)] + _split_text(right, (mid, speech[1]), rr)
    # No cut satisfies the display budget: split at the word midpoint and let both
    # halves recurse (a leaf that still does not fit is caught by _unit_cards, which
    # fails closed to the original cards). The fallback divides the span by character
    # share -- cut times from the word stream were already tried and rejected above.
    k = len(toks) // 2
    left, right = " ".join(toks[:k]), " ".join(toks[k:])
    share = len(left) / (len(left) + len(right))
    mid = speech[0] + dur * share
    lr = word_run[:k] if aligned else None
    rr = word_run[k:] if aligned else None
    return _split_text(left, (speech[0], mid), lr) + _split_text(right, (mid, speech[1]), rr)


def _unit_cards(unit: list[dict], ranges: list[tuple[int, int] | None], words: list[dict] | None) -> tuple[list[dict], dict]:
    """Display cards for one sentence unit, plus this unit's contribution to the audit."""
    info = {"splits": 0, "clause_splits": 0, "refused": False}
    if len(unit) == 1 and unit[0].get("sourced_turn"):
        # A sourced track's own attribution is a deliverable, not a guess: the card is
        # kept verbatim (both lines, both markers) and never merged, split or wrapped.
        c = unit[0]
        s0, e0 = _speech(c, ranges[0], words)
        return [{"text": c["text"], "speech_start": s0, "speech_end": e0, "verbatim": True}], info
    text = _join_text([c["text"] for c in unit])
    speech = (_speech(unit[0], ranges[0], words)[0], _speech(unit[-1], ranges[-1], words)[1])
    run = None
    if words is not None and ranges[0] is not None and ranges[-1] is not None:
        run = words[ranges[0][0] : ranges[-1][1] + 1]
    # A unit splits when its TEXT cannot be displayed as one card. A long speech span
    # is handled by clamping the display (see _time_cards), never by cutting the
    # sentence: `'It'` + `'hurts...'` was the old span rule, and it is a fragment.
    needs_split = len(text) > MAX_CHARS or not _wrappable(text)
    pieces = _split_text(text, speech, run) if needs_split else [(text, speech, run)]
    cards = []
    for piece, pspeech, prun in pieces:
        # A piece past the hard ceiling is the one fault this stage cannot repair
        # without changing words: fail closed to the original cards. Duration is not
        # checked here -- the display is clamped below MAX_DISPLAY, under the gate's.
        if _hard_faults(piece):
            info["refused"] = True
            return [dict(c) for c in unit], info
        cards.append({"text": piece, "speech_start": pspeech[0], "speech_end": pspeech[1], "_run": prun})
    if len(cards) > 1:
        info["splits"] = len(cards) - 1
        info["clause_splits"] = sum(1 for c in cards[:-1] if _ends_clause(c["text"]))
    return cards, info


# --- timing -------------------------------------------------------------------------


def _time_cards(cards: list[dict], audio_duration: float | None) -> tuple[list[dict], dict]:
    """Assign display times from speech spans: lead-in, linger, floor, gap, ceiling.

    Starts are placed first (speech onset minus ``LEAD_IN``), then each end is the
    last thing that has to move: ``LINGER`` past the last word, raised to
    ``MIN_DISPLAY``, capped at ``MAX_DISPLAY``, at the media's end, and at
    ``MIN_GAP`` before the NEXT card's start -- which is what makes a <80 ms churn gap
    impossible. A card that cannot reach ``MIN_DISPLAY`` because its neighbour starts
    immediately is counted (``min_duration_clamps``) instead of pushing text forward.
    """
    stats = {"min_duration_clamps": 0, "churn_clamps": 0, "lead_clamps": 0}
    n = len(cards)
    for c in cards:
        c["start"] = max(0.0, c["speech_start"] - LEAD_IN)

    def place_end(i: int) -> float:
        c = cards[i]
        end = max(c["speech_end"] + LINGER, c["start"] + MIN_DISPLAY)
        end = min(end, c["start"] + MAX_DISPLAY)
        if audio_duration is not None:
            end = min(end, audio_duration)
        if i + 1 < n:
            ceiling = max(cards[i + 1]["start"], c["start"] + MIN_GAP) - MIN_GAP
            if end > ceiling:
                end = ceiling
                stats["churn_clamps"] += 1
        return end

    for i, c in enumerate(cards):
        c["end"] = place_end(i)
        if c["end"] - c["start"] < MIN_DISPLAY - reflow.EPS:
            stats["min_duration_clamps"] += 1
    # A card whose speech starts on the same frame as its predecessor's end would
    # otherwise be cut by the gap rule; move the LATER card, never the earlier one.
    for i in range(1, n):
        floor = cards[i - 1]["end"] + MIN_GAP
        if cards[i]["start"] < floor - reflow.EPS:
            cards[i]["start"] = floor
            stats["lead_clamps"] += 1
            cards[i]["end"] = place_end(i)
    return cards, stats


# --- speaker turns ------------------------------------------------------------------


def detect_turn(text: str, word_run: list[dict] | None, display: float) -> tuple[str, float] | None:
    """Return ``(two-line dash-prefixed text, pause at the seam)`` for a two-speaker card.

    Every one of these must hold at the seam, or the card is left exactly as it is:

      * the card's tokens map 1:1 onto its own words -- a pause has to be MEASURABLE;
      * the first utterance ENDS a sentence and the second starts a capital or an
        opening quote -- a lowercase continuation is one sentence, not a reply;
      * a real pause mid-card (``TURN_PAUSE``) AND a whisper segment boundary, i.e.
        the ASR itself heard a break rather than a DTW wobble;
      * both sides are complete sentences (terminal marks on both ends);
      * each side fits ONE display line including its ``- `` marker, so the card is
        the classic two-line block rather than a three-line wall;
      * exactly ONE seam qualifies -- three speakers cannot be a two-line card;
      * both sides are not one word each. Two one-word utterances around a pause are
        as likely one speaker listing (`'Pleiades.'` / `'Processor.'`); a wrong
        attribution reads worse than no attribution, so it is withheld.

    The dashes are a display mark and never part of a word -- :func:`content_tokens`
    strips them, which is what keeps the word-sequence invariant mechanical.
    """
    toks = tokens(text)
    if len(toks) < 2 or not word_run or len(word_run) != len(toks):
        return None
    seams: list[int] = []
    for k in range(1, len(toks)):
        left, right = " ".join(toks[:k]), " ".join(toks[k:])
        if not is_terminal(left) or not is_terminal(right):
            continue
        if not (right[:1].isupper() or right[:1] in "\"'“‘"):
            continue
        if word_run[k].get("seg", 0) == word_run[k - 1].get("seg", 0):
            continue
        gap = word_run[k]["start"] - word_run[k - 1]["end"]
        if gap < TURN_PAUSE - reflow.EPS:
            continue
        if len(left) > TURN_LINE_MAX or len(right) > TURN_LINE_MAX:
            continue
        if k == 1 and len(toks) == 2:
            continue  # both sides one word each: indistinguishable from one speaker listing
        marks = f"{TURN_PREFIX}{left}\n{TURN_PREFIX}{right}"
        if _hard_faults(marks):
            continue
        if reflow.card_cps(marks, max(display, reflow.MIN_DUR)) > MAX_CPS + reflow.EPS:
            continue
        seams.append(k)
    if len(seams) != 1:
        return None
    k = seams[0]
    gap = word_run[k]["start"] - word_run[k - 1]["end"]
    return f"{TURN_PREFIX}{' '.join(toks[:k])}\n{TURN_PREFIX}{' '.join(toks[k:])}", gap


def _apply_turns(cards: list[dict], report: dict) -> None:
    """Mark speaker turns on already-timed cards; counters and audit go to ``report``.

    Runs AFTER timing because the readability guard needs the display duration, and a
    dash adds two visible characters per line. Text-only: no time moves, so the
    timing counters describe exactly the same layout with or without the dashes.
    """
    for c in cards:
        run = c.pop("_run", None)
        if c.get("verbatim") or turn_marked(c["text"]):
            continue
        found = detect_turn(c["text"], run, c["end"] - c["start"])
        if not found:
            continue
        marked, gap = found
        c["text"] = marked
        report["turn_dashes"] += 1
        left, right = (ln[len(TURN_PREFIX) :] for ln in marked.split("\n"))
        report.setdefault("turn_records", []).append(
            {"at": round(c["start"], 2), "pause": round(gap, 2), "turn": f"{left} | {right}"}
        )


# --- the engine ---------------------------------------------------------------------


def restructure(
    cards: list[dict],
    words: list[dict] | None = None,
    audio_duration: float | None = None,
) -> tuple[list[dict], dict]:
    """Rebuild card structure and timing; returns (new cards, report).

    ``cards`` are dicts with ``start``, ``end`` and ``text`` (a delivered file's own
    cues, or reflow's cards -- both shapes work; ``source_start``/``source_end`` are
    honoured when present). ``words`` is the episode's word list, or None for a
    structure-only pass. Every returned card carries ``start``, ``end`` and ``text``;
    the token sequence of the whole list is guaranteed identical to the input's.
    """
    report = {
        "cards_before": len(cards),
        "cards_after": 0,
        "fragments_merged": 0,
        "orphans_merged": 0,
        "churn_merges": 0,
        "structural_splits": 0,
        "clause_splits": 0,
        "min_duration_clamps": 0,
        "churn_clamps": 0,
        "lead_clamps": 0,
        "unmapped_cards": 0,
        "refused_units": 0,
        "stretched_segments": 0,
        "stretched_words": 0,
        "turn_dashes": 0,
        "sourced_turn_cards": 0,
        "words_preserved": True,
    }
    if not cards:
        return [], report

    norm: list[dict] = []
    for original in cards:
        c = dict(original)
        if not c.get("speech_start"):
            c["speech_start"] = c.get("source_start", c["start"])
        if not c.get("speech_end"):
            c["speech_end"] = c.get("source_end", c["end"])
        if turn_marked(c.get("text", "")):
            # A source track's own `- ` markers are authorship, not layout: keep the
            # card verbatim (lines included) and make it a barrier, so no join, split,
            # wrap or new dash can touch what the source already decided.
            c["sourced_turn"] = True
            c["text"] = str(c["text"]).replace("\\N", "\n").replace("\\n", "\n").strip()
            report["sourced_turn_cards"] += 1
        else:
            c["text"] = flatten(c["text"])
        norm.append(c)

    repaired = words
    if words:
        repaired, wstats = repair_word_times(words)
        report.update(wstats)
    ranges = align(norm, repaired) if repaired else [None] * len(norm)
    ranges = validate_ranges(norm, ranges, repaired)
    report["unmapped_cards"] = sum(1 for r in ranges if r is None)

    before_tokens = [t for c in norm for t in content_tokens(c["text"])]

    # --- assemble sentence units ---
    units: list[tuple[list[dict], list]] = []
    cur, cur_ranges = [norm[0]], [ranges[0]]
    for i in range(1, len(norm)):
        prev, nxt = norm[i - 1], norm[i]
        prev_speech = _speech(prev, ranges[i - 1], repaired)
        nxt_speech = _speech(nxt, ranges[i], repaired)
        gap = nxt_speech[0] - prev_speech[1]
        unit_start = _speech(cur[0], cur_ranges[0], repaired)[0]
        # An unmapped card is a BARRIER when a word list is available: nothing joins
        # across it, because nothing can prove the two sides are one utterance. The one
        # exception is a REPEATED-RUN fragment (`'Nooooooo…'` broken across two cards by
        # a line break): the repetition itself proves one vocalisation, so the unit is
        # joined on display evidence and timed from its cue bounds.
        usable = repaired is None or (ranges[i - 1] is not None and ranges[i] is not None)
        runt_bridge = (
            not usable
            and not prev.get("sourced_turn")
            and not nxt.get("sourced_turn")
            and _repeat_run(prev["text"])
            and nxt["start"] - prev["end"] <= MERGE_PAUSE + reflow.EPS
            and len(_join_text([prev["text"], nxt["text"]])) <= MAX_CHARS
        )
        same_unit = nxt_speech[1] - unit_start <= MAX_UNIT_SPAN and (
            (
                usable
                and not prev.get("sourced_turn")
                and not nxt.get("sourced_turn")
                and (
                    (not is_terminal(prev["text"]) and _continues(prev, nxt, ranges[i - 1], ranges[i], gap))
                    or _join_short(prev, nxt, prev_speech, nxt_speech, gap)
                )
            )
            or runt_bridge
        )
        if same_unit:
            cur.append(nxt)
            cur_ranges.append(ranges[i])
            report["fragments_merged"] += 1
            if len(tokens(prev["text"])) <= ORPHAN_WORDS:
                report["orphans_merged"] += 1
            if gap < reflow.MIN_GAP:
                report["churn_merges"] += 1
        else:
            units.append((cur, cur_ranges))
            cur, cur_ranges = [nxt], [ranges[i]]
    units.append((cur, cur_ranges))

    # --- lay each unit out, then time the result ---
    laid: list[dict] = []
    records: list[dict] = []
    for unit, uranges in units:
        cards_out, info = _unit_cards(unit, uranges, repaired)
        if info["refused"]:
            report["refused_units"] += 1
            for c, r in zip(unit, uranges):
                s0, e0 = _speech(c, r, repaired)
                laid.append({"text": flatten(c["text"]), "speech_start": s0, "speech_end": e0})
            continue
        report["structural_splits"] += info["splits"]
        report["clause_splits"] += info["clause_splits"]
        if len(unit) > 1 or info["splits"]:
            records.append({"merged": len(unit), "split": info["splits"], "text": _join_text([c["text"] for c in unit])[:160]})
        laid.extend(cards_out)

    if repaired:
        laid, tstats = _time_cards(laid, audio_duration)
        for k, v in tstats.items():
            report[k] += v
    else:
        # Structure-only: the cue bounds ARE the display times. A merged card spans its
        # members; nothing is re-timed and nothing is invented. The one exception is the
        # churn gap: a card ending a few milliseconds before its neighbour still reads
        # as a flash, so its end is pulled back inside MIN_GAP -- a sub-80 ms trim, the
        # same ceiling every other stage already applies.
        for c in laid:
            c["start"] = c["speech_start"]
            # A joined card's span can exceed the gate's duration ceiling even though it
            # is the right text: hold it as long as allowed, never past it.
            c["end"] = min(c["speech_end"], c["start"] + MAX_DISPLAY)
        for i in range(len(laid) - 1):
            ceiling = laid[i + 1]["start"] - MIN_GAP
            if ceiling >= laid[i]["start"] + MIN_DUR - reflow.EPS and laid[i]["end"] > ceiling:
                laid[i]["end"] = ceiling
                report["churn_clamps"] += 1

    # Speaker turns last: they only add marks, and the readability guard needs the
    # display duration that timing just decided.
    _apply_turns(laid, report)
    records.extend(report.pop("turn_records", []))

    out = [
        {
            "start": c["start"],
            "end": c["end"],
            "text": c["text"] if c.get("verbatim") else _wrap_card(c["text"]),
        }
        for c in laid
    ]

    after_tokens = [t for c in out for t in content_tokens(c["text"])]
    report["cards_after"] = len(out)
    if before_tokens != after_tokens:
        # The invariant is the owner's rule ("words never change"). A rebuild that
        # would break it is not a rebuild -- return the input, unchanged.
        report["words_preserved"] = False
        report["refused_units"] += 1
        return [{"start": c["start"], "end": c["end"], "text": c["text"]} for c in norm], report
    report["records"] = records[:MAX_RECORDS]
    return out, report


# --- file I/O -----------------------------------------------------------------------

_ASS_DIALOGUE = re.compile(r"^Dialogue:")
_ASS_FORMAT = re.compile(r"^Format:\s*(.*)$")


def _ass_time(s: str) -> float:
    h, m, rest = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def _ass_ts(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _srt_time(s: str) -> float:
    h, m, rest = s.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def _srt_ts(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def read_ass(path: str) -> tuple[list[str], list[dict], list[str]]:
    """(lines, dialogue events of the Dubtitles style, event field names).

    Read WITHOUT re-serialising: the writer replaces only those events, so every other
    byte of the file -- styles, signs, lyrics, fonts, override tags -- is preserved.
    """
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        lines = f.read().splitlines()
    section = ""
    fields: list[str] = []
    events: list[dict] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            continue
        if section != "[Events]":
            continue
        m = _ASS_FORMAT.match(line)
        if m and not fields:
            fields = [x.strip() for x in m.group(1).split(",")]
            continue
        if not _ASS_DIALOGUE.match(line) or not fields:
            continue
        payload = line.split(":", 1)[1].split(",", len(fields) - 1)
        if len(payload) < len(fields):
            continue
        rec = dict(zip(fields, payload))
        style = rec.get("Style", "").strip()
        if style != "Dubtitles":
            continue
        events.append(
            {
                "line": i,
                "fields": payload,
                "style": style,
                "start": _ass_time(rec["Start"].strip()),
                "end": _ass_time(rec["End"].strip()),
                "text": rec.get("Text", ""),
            }
        )
    return lines, events, fields


def read_srt(path: str) -> tuple[list[str], list[dict], list[str]]:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        lines = f.read().splitlines()
    events: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.isdigit() and i + 1 < len(lines) and "-->" in lines[i + 1]:
            a, b = (x.strip() for x in lines[i + 1].split("-->"))
            j = i + 2
            body = []
            while j < len(lines) and lines[j].strip():
                body.append(lines[j])
                j += 1
            events.append({"line": i, "end_line": j + 1, "start": _srt_time(a), "end": _srt_time(b), "text": "\n".join(body)})
            i = j + 1
        else:
            i += 1
    return lines, events, []


def _render(path: str, lines: list[str], events: list[dict], fields: list[str], cards: list[dict]) -> str:
    """The file's text with ONLY its dialogue events replaced, in place."""
    drop = {e["line"] for e in events}
    if path.endswith(".ass"):
        at = sum(1 for i in range(events[0]["line"]) if i not in drop)
        new_lines = []
        for c in cards:
            payload = list(events[0]["fields"])
            payload[fields.index("Start")] = _ass_ts(c["start"])
            payload[fields.index("End")] = _ass_ts(c["end"])
            payload[fields.index("Text")] = c["text"].replace("\n", "\\N")
            new_lines.append("Dialogue:" + ",".join(payload))
    else:
        first, last = events[0]["line"], events[-1]["end_line"]
        at = first
        drop = set(range(first, last))
        new_lines = []
        for n, c in enumerate(cards, 1):
            new_lines += [str(n), f"{_srt_ts(c['start'])} --> {_srt_ts(c['end'])}", c["text"], ""]
    kept = [ln for i, ln in enumerate(lines) if i not in drop]
    return "\n".join(kept[:at] + new_lines + kept[at:]) + "\n"


def _load_words(stem: str) -> dict | None:
    from dubtitlerr.core import common

    try:
        doc = common.read_words(stem)
    except Exception:  # noqa: BLE001 - a missing/unreadable word list is structure-only
        return None
    if not doc or not doc.get("words"):
        return None
    return doc


def episode_path(stem: str) -> str | None:
    """The subtitle file to rebuild: the pipeline artifact, else the published sidecar.

    Same resolution order as ``publish_gate.load_cues`` -- the gate must score the file
    this stage rewrites, not a different one -- plus the configured sidecar suffix, which
    is a deployment setting (``SIDECAR_SUFFIX``) rather than a fixed name.
    """
    from dubtitlerr.pipeline import mux, publish_gate

    path, _ = publish_gate.load_cues(stem)
    if path:
        return path
    for suffix in (mux.SIDECAR_SUFFIX, ".dub.en"):
        for ext in (".ass", ".srt"):
            candidate = stem + suffix + ext
            if os.path.exists(candidate):
                return candidate
    return None


def restructure_file(path: str, words_doc: dict | None, audio_duration: float | None, dry_run: bool = False) -> tuple[dict, bool]:
    """Rebuild one subtitle file; writes only when the result differs and is safe."""
    lines, events, fields = read_ass(path) if path.endswith(".ass") else read_srt(path)
    if not events:
        return {"skipped": "no dialogue events"}, False
    cards = [{"start": e["start"], "end": e["end"], "text": e["text"]} for e in events]
    new_cards, report = restructure(cards, (words_doc or {}).get("words"), audio_duration)
    if not report.get("words_preserved", True):
        report["skipped"] = "word sequence would change"
        return report, False
    unchanged = len(new_cards) == len(cards) and all(
        abs(a["start"] - b["start"]) < 0.001 and abs(a["end"] - b["end"]) < 0.001 and flatten(a["text"]) == flatten(b["text"])
        for a, b in zip(new_cards, cards)
    )
    report["changed_cards"] = sum(
        1
        for a, b in zip(new_cards, cards)
        if abs(a["start"] - b["start"]) >= 0.001 or abs(a["end"] - b["end"]) >= 0.001 or flatten(a["text"]) != flatten(b["text"])
    )
    report["cards_in_file_before"] = len(cards)
    report["cards_in_file_after"] = len(new_cards)
    if unchanged:
        report["outcome"] = "no-op"
        return report, False
    if dry_run:
        report["outcome"] = "would-rebuild"
        return report, False
    text = _render(path, lines, events, fields, new_cards)
    atomic_write(path, lambda f: f.write(text), mode=MODE_KEEP)
    report["outcome"] = "rebuilt"
    return report, True


# --- epoch --------------------------------------------------------------------------


def _stamp_path(stem: str) -> str:
    from dubtitlerr.core import common

    return common.out_for(stem + STAMP_SUFFIX)


def read_stamp(stem: str) -> dict:
    try:
        with open(_stamp_path(stem)) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def is_stale(stem: str) -> bool:
    """True when this delivered episode has not been rebuilt under this epoch."""
    return read_stamp(stem).get("epoch") != PRESENTATION_EPOCH


def _write_stamp(stem: str, report: dict, outcome: str) -> None:
    doc = {
        "epoch": PRESENTATION_EPOCH,
        "at": time.time(),
        "outcome": outcome,
        "cards_before": report.get("cards_before"),
        "cards_after": report.get("cards_after"),
        "counters": {k: v for k, v in report.items() if isinstance(v, int) and k not in ("cards_before", "cards_after")},
    }
    atomic_write(_stamp_path(stem), lambda f: json.dump(doc, f, indent=1))


def apply_episode(stem: str, dry_run: bool = False, force: bool = False) -> dict:
    """One in-app re-pass for a delivered episode: rebuild, re-gate, stamp.

    A stamp at the current epoch means this episode has already had its pass, so the
    call is a no-op unless ``force`` -- "exactly one re-pass per change" is enforced
    here, not by hoping nobody calls it twice.
    """
    path = episode_path(stem)
    if not path:
        return {"outcome": "no-artifact"}
    if not force and not dry_run and not is_stale(stem):
        return {"outcome": "no-op", "stamp_current": True, "path": os.path.basename(path)}
    words_doc = _load_words(stem)
    try:
        from dubtitlerr.pipeline import publish_gate

        duration = publish_gate.video_duration(stem)
    except Exception:  # noqa: BLE001 - a missing duration only drops the tail clamp
        duration = None
    backup = None
    if not dry_run:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                backup = f.read()
        except OSError:
            backup = None
    report, changed = restructure_file(path, words_doc, duration, dry_run=dry_run)
    report["path"] = os.path.basename(path)
    report["words_source"] = "words.json" if words_doc else "cues-only"
    if not changed:
        report.setdefault("outcome", "no-op")
        if not dry_run:
            _write_stamp(stem, report, report["outcome"])
        return report
    # Fail-closed: re-gate the file we just wrote. A refusal means the rebuild made the
    # file unpublishable, so the previous bytes go back and the epoch is not advanced.
    from dubtitlerr.pipeline import publish_gate

    fails = publish_gate.evaluate(publish_gate.load_cues(stem)[1], duration)
    if fails:
        if backup is not None:
            atomic_write(path, lambda f: f.write(backup), mode=MODE_KEEP)
        report["outcome"] = "regressed"
        report["fails"] = fails
        if not dry_run:
            # Stamp the attempt: a rebuild that the gate refuses is deterministic, and
            # retrying it every merge pass is the churn this stamp exists to prevent.
            _write_stamp(stem, report, "regressed")
        return report
    report["outcome"] = "rebuilt"
    if not dry_run:
        _write_stamp(stem, report, "rebuilt")
    return report


def sweep(roots: list[str], dry_run: bool = False, limit: int | None = None) -> dict:
    """Rebuild every delivered episode under ``roots`` whose epoch stamp is stale."""
    from dubtitlerr.core import common

    stems = set()
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.endswith(".dubtitles.done"):
                    stems.add(common.out_for(os.path.join(dirpath, name[: -len(".dubtitles.done")])))
    ordered = sorted(stems)
    if limit:
        ordered = ordered[:limit]
    out = {"scanned": len(ordered), "stale": 0, "rebuilt": 0, "noop": 0, "skipped": 0, "failed": 0, "results": []}
    for stem in ordered:
        if not is_stale(stem):
            continue
        out["stale"] += 1
        try:
            rep = apply_episode(stem, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001 - one bad episode must not stop the sweep
            out["failed"] += 1
            out["results"].append({"stem": os.path.basename(stem), "outcome": f"error: {e}"})
            continue
        oc = rep.get("outcome")
        if oc == "rebuilt":
            out["rebuilt"] += 1
        elif oc in ("no-op", "would-rebuild"):
            out["noop"] += 1
        else:
            out["skipped"] += 1
        out["results"].append({"stem": os.path.basename(stem), "outcome": oc, "counters": rep})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild card structure and timing from word timestamps.")
    ap.add_argument("stem", nargs="?", help="episode stem (no extension)")
    ap.add_argument("--stale", action="store_true", help="0 = stale, 1 = current, 3 = unusable")
    ap.add_argument("--sweep", nargs="*", metavar="ROOT", help="rebuild every stale delivered episode under these roots")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    if args.sweep is not None:
        roots = args.sweep or ([args.stem] if args.stem else [])
        if not roots:
            print("presentation: --sweep needs at least one root", file=sys.stderr)
            return 2
        result = sweep(roots, dry_run=args.dry_run, limit=args.limit)
        if args.json:
            print(json.dumps(result, indent=1))
        else:
            print(
                f"presentation: scanned={result['scanned']} stale={result['stale']} "
                f"rebuilt={result['rebuilt']} no-op={result['noop']} skipped={result['skipped']} failed={result['failed']}"
            )
            for r in result["results"]:
                if r["outcome"] not in ("no-op",):
                    print(f"  {r['outcome']}: {r['stem']}")
        return 0
    if not args.stem:
        ap.print_help()
        return 2
    if args.stale:
        if not episode_path(args.stem):
            print("no subtitle artifact", file=sys.stderr)
            return 3
        stale = is_stale(args.stem)
        print(f"stale={stale}")
        return 0 if stale else 1
    report = apply_episode(args.stem, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        print(
            f"presentation: {report.get('outcome')} cards {report.get('cards_before')}->{report.get('cards_after')} "
            f"merged={report.get('fragments_merged')} orphans={report.get('orphans_merged')} "
            f"churn={report.get('churn_merges')} splits={report.get('structural_splits')} "
            f"min-dur-clamps={report.get('min_duration_clamps')}"
        )
        for f in report.get("fails", []):
            print(f"  gate: {f}")
    return 0 if report.get("outcome") in ("rebuilt", "no-op", "would-rebuild") else 3


if __name__ == "__main__":
    sys.exit(main())
