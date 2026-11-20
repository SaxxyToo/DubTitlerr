#!/usr/bin/env python3
"""Hallucination confidence-gate for dubtitle cards (B1).

Whisper occasionally invents text over music/silence, loops an n-gram within a line, or
repeats a whole line across many cards. This module classifies reflow cards (post A1 + C1)
into DROP (near-certain garbage) vs FLAG (kept, but suspect) and collapses runaway repeat
runs. Conservative by design: a single weak signal only flags — it never deletes a line.

Pure stdlib, deterministic, runs in the subgen image. Card dicts are
{start, end, text, avg_logprob, no_speech_prob}.  Built with help of Claude (Anthropic).
"""

from __future__ import annotations

import re
from collections import Counter

from dubtitlerr.pipeline import reflow

# The two no_speech_prob-gated rules that used to live here -- `music`
# (nsp > 0.95 AND avg_logprob < -2.0 -> drop) and `maybe_silence` (nsp > 0.5 -> flag) --
# were DELETED on 2026-08-24, not tuned. `music` caught 0 of 353,879 cards, and stays at 0
# even on large-v3, whose nsp is live: none of its segments clearing 0.95 also has a
# logprob below -2.0. The conjunction does not occur in this material, so no model change
# revives it, and every reachable relaxation destroys more real dialogue than it saves.
# The full measurement -- including the precision table that forced the 2026-08-21 revert
# -- is preserved in docs/wiki/Design-decisions.md.
# Do not re-add either rule from intuition: the ~20% precision ceiling is the thing to
# beat, not the threshold.
# FLAG thresholds (a single weaker signal -> keep but mark suspect)
LP_FLAG = -0.6
# repetition
REPEAT_MIN_TOKENS = 6  # below this a card is too short to call a loop (protects emphasis)
REPEAT_COVERAGE = 0.6  # a repeated word/1-3-gram covering >= this fraction of the card
RUN_COLLAPSE = 4  # collapse a run of >= this many near-identical consecutive cards
# a run of >= LOOP_MIN_CARDS consecutive cards that are each only one token, >= LOOP_MIN_TOKENS tokens in all
LOOP_MIN_CARDS = 5
LOOP_MIN_TOKENS = 6
# Arm #4: ONE token that repeats a short pattern inside itself, so no card/count-based rule
# can see it. Measured on Chainsaw Man S01E07: a 1.32 s cackle Whisper wrote as a single
# 446-character token, which the publish gate refuses on line length and no other arm can
# repair. reflow._collapse_repeats catches the clean word-level shape; this catches the
# card-level survivor (a token carrying punctuation, or a segment-level fallback "word").
TOKEN_RUN_MAX_PERIOD = 4  # the repeated unit is at most this many characters
TOKEN_RUN_MIN_REPEATS = 15  # ...and repeats at least this many times
TOKEN_RUN_KEEP_REPEATS = 6  # reduce to this many repeats + an ellipsis
TOKEN_RUN_MIN_LEN = 30  # below this length a repeated unit is a scream or a stutter, not a loop

# Known whisper hallucination phrases (music/credits/UGC boilerplate). Conservative —
# only phrases that are never real dub dialogue. V2 C8: sourced from
# data/hallucination_blocklist.txt (one regex alternative per line, # comments allowed)
# so it can be tuned without a code change; falls back to this exact inline pattern if
# the file is missing/unreadable (backward-compatible, e.g. a dev checkout without data/).
_BLOCKLIST_PATTERN_FALLBACK = (
    r"amara\.org|thank you for watching[.!]*\s*$|thanks for watching[.!]*\s*$|thanks for your support[.!]*\s*$|"
    r"please subscribe|subscribe to (the|our|my) channel|like and subscribe|"
    r"see you (in the )?next (video|time)|subtitles by|captions? by|transcri(bed|ption) by|"
    r"translated by|copyright|www\.|http|to be continued|we.?ll be right back"
)


def _load_blocklist(path: str = "data/hallucination_blocklist.txt") -> re.Pattern:
    try:
        with open(path, encoding="utf-8") as f:
            alts = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError:
        alts = []
    return re.compile("|".join(alts) if alts else _BLOCKLIST_PATTERN_FALLBACK, re.I)


BLOCKLIST = _load_blocklist()


def is_repetition(text: str) -> bool:
    """True if the card is dominated by a repeated word or short n-gram (a within-line loop)."""
    toks = re.findall(r"[a-z0-9']+", text.lower())
    n = len(toks)
    if n < REPEAT_MIN_TOKENS:
        return False
    if Counter(toks).most_common(1)[0][1] / n >= REPEAT_COVERAGE:  # one word dominates
        return True
    for k in (3, 2):  # a 2-3-gram loops
        grams = [tuple(toks[i : i + k]) for i in range(n - k + 1)]
        top = Counter(grams).most_common(1)[0][1]
        if top >= 3 and top * k / n >= REPEAT_COVERAGE:
            return True
    return False


def _tick(rec, rule: str, activated: bool) -> None:
    """Record that ``rule`` was EVALUATED, and separately whether it FIRED.

    `evaluated > 0` with `activated == 0` across a season is the dead-rule signal this
    codebase did not have: `music` sat inert through 353,879 cards and every episode still
    reported success, because zero activation is not an error. `rec` is optional so tools/
    and tests can call these bare.

    Short-circuiting is deliberate and meaningful: a rule that returns early leaves the
    later rules UN-evaluated, and the counters should say so rather than implying they ran
    and declined."""
    if rec is None:
        return
    rec.count(f"rule_{rule}_evaluated")
    # ALWAYS create the activated counter, incrementing by 0 when it did not fire. A missing
    # key is indistinguishable from "never reached", which is the exact ambiguity this
    # function exists to remove -- an absent counter would rebuild the invisibility one
    # level down.
    rec.count(f"rule_{rule}_activated", 1 if activated else 0)


# A card of at most this many words whose SOURCE window is longer than reflow.MAX_DUR
# carries a word timestamp whisper got wrong. Scoped deliberately: a long span across
# several words is ordinary dialogue.
BAD_WINDOW_MAX_WORDS = 2


def bad_source_window(card: dict, rec=None) -> bool:
    """True when this card's ``source_*`` window is a whisper timestamp already proven
    implausible -- a one- or two-word card whose source span exceeds reflow.MAX_DUR.

    Measured in the VAD hang-trim investigation: single function words pinned at the 7s
    ceiling ('disobeys', 'it'), and 20 of 401 gated cards carrying MORE word
    probabilities than they have words, because a 7-second window swallows the
    neighbours'. The window is not merely a display problem: two stages select evidence
    with it, and both were trusting a value known to be wrong.

    Returns False when either ``source_*`` key is ABSENT rather than defaulting to the
    display window. The VAD design records two of its own measurements invalidated by
    exactly that -- a ``.get(..., card["start"])`` fallback guaranteeing the answer it
    was asked to test. No window means no evidence that the window is bad.

    This is observability, not recovery: the counters are the point. A caller that finds
    a bad window should use NOTHING, not a substitute."""
    ss, se = card.get("source_start"), card.get("source_end")
    hit = (
        ss is not None
        and se is not None
        and len(card.get("text", "").split()) <= BAD_WINDOW_MAX_WORDS
        and (se - ss) > reflow.MAX_DUR
    )
    _tick(rec, "source_window", hit)
    return hit


# Hiragana, katakana, CJK ideographs (plus ext-A) and halfwidth katakana. NOT a confidence
# threshold: the dub track is English BY CONSTRUCTION -- whisper is transcribing an English
# dub -- so a card carrying Japanese script is not a low-confidence English line, it is
# whisper falling back to the Japanese it heard under a song it could not transcribe.
# A dub says a Japanese NAME in romaji, never in kana, so this does not collide with real
# dialogue.
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9d]")


def has_cjk(text: str) -> bool:
    """Japanese script in a line that is supposed to be English."""
    return bool(CJK.search(text or ""))


def drop_reason(card: dict, rec=None) -> str | None:
    """'blocklist' | 'cjk_in_english_dub' | 'repetition' | None — near-certain garbage only.
    The nsp-gated 'music' rule was deleted 2026-08-24 -- see ADR 0002.

    ADR 0002's warning is about re-adding a TUNED THRESHOLD from intuition, and it stands.
    `cjk_in_english_dub` is not one: it has no threshold to tune and no precision/recall
    trade-off to get wrong. Either the line contains Japanese script or it does not, and in
    an English dub it should not. Measured 2026-09-02 on MARRIAGETOXIN S01E02, whose release
    captions no song lyrics at all and so cannot be helped by the signs-track song-span
    drop: it removes 10 of the 15 hallucinated OP cards and touches no dialogue."""
    text = card.get("text", "")
    hit = bool(BLOCKLIST.search(text))
    _tick(rec, "blocklist", hit)
    if hit:
        return "blocklist"
    hit = has_cjk(text)
    _tick(rec, "cjk_in_english_dub", hit)
    if hit:
        return "cjk_in_english_dub"
    hit = is_repetition(text)
    _tick(rec, "repetition", hit)
    if hit:
        return "repetition"
    return None


def flag_reason(card: dict, rec=None) -> str | None:
    """A weaker single-signal suspicion for a KEPT card ('low_conf' | None). The
    nsp-gated 'maybe_silence' was deleted 2026-08-24 -- see ADR 0002."""
    hit = card.get("avg_logprob", 0.0) < LP_FLAG
    _tick(rec, "low_conf", hit)
    if hit:
        return "low_conf"
    return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def collapse_runs(cards: list[dict]) -> list[dict]:
    """Collapse runs of >= RUN_COLLAPSE near-identical consecutive cards into one (first
    start, last end). Shorter repeats are left untouched."""
    out: list[dict] = []
    i = 0
    while i < len(cards):
        j = i + 1
        key = _norm(cards[i]["text"])
        while j < len(cards) and _norm(cards[j]["text"]) == key:
            j += 1
        run = cards[i:j]
        if len(run) >= RUN_COLLAPSE:
            merged = dict(run[0])
            merged["end"] = run[-1]["end"]
            out.append(merged)
        else:
            out.extend(run)
        i = j
    return out


def repeat_loops(cards: list[dict], known=()) -> tuple[set[int], dict[int, str]]:
    """Find a word whisper looped across many short cards ('Xanamon!' x15 in 18 s). Reflow
    leaves these as one- and two-word cards that neither collapse_runs (needs identical cards)
    nor is_repetition (needs >= REPEAT_MIN_TOKENS per card) can see.

    A loop is a maximal run of consecutive cards that each hold only one token T (repeated any
    number of times, case and punctuation ignored), with >= LOOP_MIN_CARDS cards and
    >= LOOP_MIN_TOKENS tokens. T in ``known`` (the glossary's names and phrases, matched per
    token) is real and never a loop. Returns (indices to drop, {index: text}) where the second
    is the card just before a loop whose TRAILING words are T: its real words stay and the loop
    words are cut. Nothing else is touched."""
    names = {t for k in known for t in re.findall(r"[a-z0-9']+", str(k).lower())}
    toks = [re.findall(r"[a-z0-9']+", c.get("text", "").lower()) for c in cards]
    drops: set[int] = set()
    trims: dict[int, str] = {}
    i = 0
    while i < len(cards):
        word = toks[i][0] if toks[i] and len(set(toks[i])) == 1 else None
        j = i + 1
        while word and j < len(cards) and toks[j] and set(toks[j]) == {word}:
            j += 1
        if word and word not in names and j - i >= LOOP_MIN_CARDS and sum(len(t) for t in toks[i:j]) >= LOOP_MIN_TOKENS:
            drops.update(range(i, j))
            if i and toks[i - 1] and toks[i - 1][-1] == word:
                text = cards[i - 1]["text"]
                cut = re.sub(rf"(?:[\W_]*\b{re.escape(word)}\b)+[\W_]*$", "", text, flags=re.I)
                if cut != text and re.search(r"[a-z0-9]", cut, re.I):
                    # the sentence's own final mark went with the loop words; keep what preceded them
                    trims[i - 1] = _restore_end(text, cut)
        i = j
    return drops, trims


def _restore_end(text: str, cut: str) -> str:
    """Re-attach the terminal punctuation that closed the last real word ('Let's go! Xanamon!' -> 'Let's go!')."""
    tail = text[len(cut) :]
    m = re.match(r"[^\w\s]+", tail)
    return cut + (m.group(0) if m else "")


_NONSPACE = re.compile(r"\S+")
_TOKEN_EDGE = re.compile(r"^([^\w]*)(\w+)([^\w]*)$")


def _token_run(core: str) -> tuple[int, int] | None:
    """(period, repeats) when ``core`` is one short unit repeated many times, else None.

    The SMALLEST period wins: 'AAAAAA' is 'A' x 6, not 'AAA' x 2, and the kept prefix has to
    be whole units of the thing whisper actually looped. The comparison is case-insensitive
    so 'Ke' + 'ke' x N is one run, exactly as whisper emits a cackle's first syllable
    capitalised.
    """
    n = len(core)
    if n < TOKEN_RUN_MIN_LEN:
        return None
    low = core.lower()
    for period in range(1, TOKEN_RUN_MAX_PERIOD + 1):
        if n % period:
            continue
        repeats = n // period
        if repeats >= TOKEN_RUN_MIN_REPEATS and low == low[:period] * repeats:
            return period, repeats
    return None


def _reduced_token(token: str, names: set[str]) -> tuple[str, int, int] | None:
    """Reduce one whitespace token, or None when it is not a run / must be exempted.

    Only the alphanumeric core is examined; surrounding punctuation and quotes are kept
    verbatim so a reduced '!'-terminated or quoted token still reads as the same card.
    A core (or its repeated unit) matching a glossary name is real however often whisper
    repeats it -- the same exemption repeat_loops applies per card.
    """
    edge = _TOKEN_EDGE.fullmatch(token)
    if edge is None:
        return None
    prefix, core, suffix = edge.groups()
    run = _token_run(core)
    if run is None:
        return None
    period, repeats = run
    if core.lower() in names or core[:period].lower() in names:
        return None
    return f"{prefix}{core[: period * TOKEN_RUN_KEEP_REPEATS]}\u2026{suffix}", period, repeats


def reduce_token_runs(cards: list[dict], known=()) -> tuple[list[dict], list[dict]]:
    """Arm #4: reduce a token that repeats a short pattern inside itself ('Kekekekeke x223',
    446 chars in 1.32 s) to a readable ~6 repeats plus an ellipsis.

    The three existing arms all need something this shape does not have: collapse_runs needs
    a run of identical CARDS, repeat_loops a run of single-token CARDS, is_repetition many
    TOKENS per card. Here there is one card, one token, and the loop is inside the token --
    so the gate refuses the card on line length and gate_fix correctly finds no boundary.

    Text only: start/end are untouched (the cackle still occupies its 1.3 s). T in ``known``
    (the glossary's names and phrases) is exempt, matched per token like repeat_loops.
    Returns (cards, reductions) where each reduction records the audit trail
    {index, start, end, before, after, period, repeats}. Cards are copied, never mutated.
    """
    names = {t for k in known for t in re.findall(r"[a-z0-9']+", str(k).lower())}
    out: list[dict] = []
    reductions: list[dict] = []
    for index, c in enumerate(cards):
        text = c.get("text", "")
        seen: list[tuple[int, int]] = []

        def repl(m, seen=seen):
            hit = _reduced_token(m.group(0), names)
            if hit is None:
                return m.group(0)
            replacement, period, repeats = hit
            seen.append((period, repeats))
            return replacement

        reduced = _NONSPACE.sub(repl, text)
        if reduced == text:
            out.append(c)
            continue
        out.append({**c, "text": reduced})
        period, repeats = seen[0]
        reductions.append(
            {
                "index": index,
                "start": c.get("start"),
                "end": c.get("end"),
                "before": text,
                "after": reduced,
                "period": period,
                "repeats": repeats,
            }
        )
    return out, reductions
