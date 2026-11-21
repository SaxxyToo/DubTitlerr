#!/usr/bin/env python3
"""One deterministic repair attempt for an episode the publish gate refused.

`publish_gate.evaluate` is the last word on what ships, but until now a refusal was a
dead end: `merge_pass.sh` printed `gate: FAIL`, printed `skip mux`, and the episode sat
unmuxed with no stamp and nothing consuming the failure. Most refusals are *layout*
faults the pipeline itself introduced while assembling the file -- a card left far longer
than the screen allows, or a line that was never wrapped -- and both are repairable from
the file's own text and word timings, with no model and no re-transcription.

What this module will and will not do:

  * **It never invents a word.** Cards are re-wrapped (`reflow.wrap_balance`) or split at
    an existing word/sentence/clause boundary (`card_split.find_legal_split`). The word
    sequence of a split card is byte-identical across its halves. The ONE exception is the
    CORRECTION LANE below, which rewrites a token only where the show's own glossary
    already says the correct spelling (`glossary.correct`'s curated hard_fixes / names --
    the same rules the pipeline applies to fresh text, never a model's guess).
  * **A correctable refusal heals in-app (P24).** A non-layout give-up (``known
    mishear(s): ...``) used to park straight away, even though the glossary that flagged
    the term already carried the fix for it -- the product was assembled before that fix
    landed. Before giving up, each cue's parsed TEXT is run through
    ``glossary.correct()`` (per cue, per physical line, so layout and timing never move):
    if anything changed, the file is re-gated in memory and written only if it now passes.
    An uncorrectable finding still parks, and a partial correction is never written.
  * **It only moves timing to split.** A split takes the parent card's span, removes one
    `reflow.MIN_GAP` from it, and divides the remainder between the halves -- by the
    card's own ASR word timings when they line up 1:1, otherwise by character share.
    Nothing else in the file is re-timed.
  * **It does not ship a partial fix.** The rewritten file is parsed and re-gated in
    memory before it is written; if any gate finding survives, nothing is written and the
    episode stays refused. The merge pass re-runs `publish_gate` on the file it finds.
  * **It only touches layout faults.** Coverage, punctuation, cold-open, cards-per-minute
    and known-mishear findings are a different failure class -- fixing those needs the
    transcript or a model, so they are reported as unsupported rather than attempted.
  * **A card with no word in it is not dialogue.** An over-duration card whose text is
    symbol-only (`♪`, `♪♪♪`, ...) is a music marker the dub ASR emitted with no words
    attached: trim-to-speech has no word data and split/wrap has no boundary, so the card
    used to be reported unfixable forever. Such a card is dropped instead -- it removes no
    spoken word, and the fansub/song path owns that span -- and the drop is counted in the
    audit document (`music_cues_dropped`).
  * **An already-passing file is a no-op.** No audit entry, no stage record, no byte
    written -- so a healthy episode costs the merge pass nothing.
  * **A give-up is PARKED, not retried every pass.** When the attempt ends `unfixable`,
    `unsupported` or `still-failing`, `<stem>.dubtitles.parked.json` records the refusal and
    the ATTEMPT STAMP that produced it (transcribe_version + text_version +
    ``GATE_FIX_EPOCH``). Ordinary merge passes skip a parked episode; it is retried only
    when the stamp no longer matches the running code (a deployed fix heals it exactly
    once) or when a run request for the show was created after the park. Bump
    ``GATE_FIX_EPOCH`` for every change to this module's behaviour: the tier versions
    cannot see a gate_fix improvement (the P16 no-text-bump precedent), so without the
    epoch a parked episode would never be retried by a new fix.

Fault classes it can repair, all under the gate's own budget
(``publish_gate.MAX_DUR`` / ``MAX_CHARS`` / ``MAX_LINE``):

  * ``line of N chars`` and ``N card(s) over 88 chars`` -- re-wrap within the card, or
    split into two cards when no single word boundary fits the line budget. A line the
    ASR emitted as ONE run of one letter, with at most two stray characters (the shipped
    57-character ``'A' + 'H'*56`` scream, a long laugh) has no boundary at all: it gets an
    even line break instead, which changes no character -- and still refuses if the card's
    TOTAL is over ``MAX_CHARS``. An alternating run (``Kekekekeke...``) is not a run and
    stays refused.
  * ``N card(s) over 7.5s`` -- split the card, recursively, until each piece is inside
    the duration budget (a 16.9 s card needs three pieces, not two).

Audit trail: every attempt is appended to ``<stem>.dubtitles.gate-fix.json`` with the
cards it touched, their before/after duration and length, and the attempt outcome, plus a
one-line ``summary``. The stage record ``gate_fix`` in ``.dubtitles.stages.json`` carries
the outcome the merge pass' census reads (``ok`` or ``refused``).

CLI:
  python3 -m dubtitlerr.pipeline.gate_fix STEM [--dry-run] [--json] [--duration S]
  python3 -m dubtitlerr.pipeline.gate_fix STEM --parked [--show NAME]   # exit 0 = skip, 1 = not parked, 3 = unusable
  python3 -m dubtitlerr.pipeline.gate_fix STEM --unpark                  # retry this pass

Exit codes (the merge pass branches on these):
  0  the episode's subtitle now passes the gate (fixed, or it already did)
  1  still refused; this exact refusal was already escalated, so do not re-notify
  3  still refused (or not a layout fault at all); this is new -> escalate now
  2  the CLI could not evaluate the episode (bad arguments, unreadable artifact)

``--parked`` answers a different question, so its codes are separate: 0 = the park stands
(skip the stem), 1 = no park, or one that no longer stands (merge_pass retries it), 3 = the
park record itself is unusable. 3 exists because 1 used to mean both "not parked" and "the
check crashed", and the merge pass read any non-zero as a stale verdict: it unparked the
episode and deleted a record it could not read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter

from dubtitlerr.core.atomic_file import MODE_KEEP, atomic_write
from dubtitlerr.glossary import glossary as glossary_text
from dubtitlerr.pipeline import card_split, publish_gate, reflow

AUDIT_SUFFIX = ".dubtitles.gate-fix.json"
PARK_SUFFIX = ".dubtitles.parked.json"
BACKUP_SUFFIX = ".pre-gate-fix.bak"
MAX_AUDIT_ATTEMPTS = 20

# GATE_FIX_EPOCH: the "which behaviour gave up here" stamp on a parked episode.
#
# Bump this whenever anything in this module changes what it will fix, drop, or report.
# Neither TRANSCRIBE_VERSION nor TEXT_VERSION can see such a change: a gate_fix fix does
# not alter how the text was derived from the words (the P16 no-text-bump precedent), so a
# parked episode would keep its old verdict and a newly deployed repair would never reach
# it. The attempt stamp is compared against today's values on every pass, so a bump heals
# every parked episode exactly once and then parks whatever still cannot be fixed.
#
# History (append one entry per bump, oldest first):
#   1 (2026-11-20): introduced with parking. Same release adds the music-symbol drop
#     (over-duration cards whose text has no word character at all), so the four episodes
#     the 2026-11-20 campaign left churning heal on their first pass under this epoch.
#   2 (2026-11-21): the correction lane. A non-layout give-up now re-applies the show's own
#     curated corrections to the stored text before parking, so the nine Frieren episodes
#     parked with `known mishear(s)` heal on their first pass under this epoch. The attempt
#     stamp also carries a glossary fingerprint now, so a glossary change retries that
#     show's parked episodes once (exactly what heals them) without an operator unpark.
GATE_FIX_EPOCH = 2

# Outcomes that are PARKED: a deterministic give-up on this exact artifact. `cannot-evaluate`
# is deliberately absent -- an unreadable artifact can be transient, so it keeps retrying.
PARKED_OUTCOMES = ("unfixable", "unsupported", "still-failing")

# The gate's budget is the target; a fix that only satisfies a laxer one would be refused
# again by the very check that sent us here.
GATE_MAX_CHARS = publish_gate.MAX_CHARS
GATE_MAX_LINE = publish_gate.MAX_LINE
GATE_MAX_DUR = publish_gate.MAX_DUR

# A run is one repeated character; this many stray characters are still a run. FMA:B
# S01E41's scream ships as 'A' + 'H'*56 -- the normal English spelling of a scream, one
# leading 'A' then the held letter -- and a break inserted anywhere in it removes no
# character.
RUN_STRAYS = 2

# `publish_gate.evaluate` reports faults as prose. These are the ones a layout repair can
# address; anything else is a different failure class and must not be attempted.
_LAYOUT_FAIL = re.compile(
    r"^(?:line of \d+ chars"
    r"|\d+ card\(s\) over \d+ chars"
    r"|\d+ card\(s\) over [\d.]+s)"
)

OUTCOME_FIXED = "fixed"
OUTCOME_CORRECTED = "corrected"
OUTCOME_NOOP = "no-op"
OUTCOME_UNSUPPORTED = "unsupported"
OUTCOME_UNFIXABLE = "unfixable"
OUTCOME_STILL_FAILING = "still-failing"
OUTCOME_CANNOT_EVALUATE = "cannot-evaluate"

PASSED = (OUTCOME_FIXED, OUTCOME_CORRECTED, OUTCOME_NOOP)


# --- fault classification -----------------------------------------------------------


def split_fails(fails):
    """(layout, other) halves of a gate finding list."""
    layout, other = [], []
    for f in fails:
        (layout if _LAYOUT_FAIL.match(f) else other).append(f)
    return layout, other


def _flat(text):
    """A card's words, with both the raw ``\\N`` and a real newline read as whitespace."""
    return re.sub(r"\s+", " ", text.replace("\\N", " ")).strip()


def _line_len(text):
    return max((len(ln) for ln in text.replace("\\N", "\n").split("\n")), default=0)


def cue_faults(text, dur, max_line=GATE_MAX_LINE):
    """Which layout constraints this ONE card violates (empty list = displayable)."""
    out = []
    flat = _flat(text)
    if len(flat) > GATE_MAX_CHARS:
        out.append(f"{len(flat)} chars")
    if _line_len(text) > max_line:
        out.append(f"{_line_len(text)}-char line")
    if dur > GATE_MAX_DUR + reflow.EPS:
        out.append(f"{dur:.2f}s card")
    return out


def is_music_marker(text):
    """True when a card carries no word character at all -- a music marker, not dialogue.

    The dub ASR emits bare music marks (``♪``, ``♪♪♪``) as their own cards, and Blue Eye
    Samurai S01E02 ships a 29.9 s ``♪`` and a 37.0 s ``♪♪♪``. Neither trim-to-speech (there
    are no words at all) nor split/wrap (there is no boundary) can normalise one, so the
    card was reported unfixable on every pass. A card with no letter and no digit says
    nothing: dropping it removes no word, and the fansub/song path owns the span.

    Deliberately only symbols/whitespace: a number ("42") is content, and so is any word.
    """
    flat = _flat(text)
    return bool(flat) and not any(ch.isalnum() for ch in flat)


# --- one card ------------------------------------------------------------------------


def _wrap(text):
    """Lay a card's text out inside the line budget. Not one character changes.

    A line the ASR emitted as ONE token that is a run of one letter -- the shipped
    57-character ``'A' + 'H'*56`` scream, a long laugh, with at most two stray characters
    anywhere in it -- has no word boundary to wrap at, so :func:`reflow.wrap_balance` hands
    it straight back and the card is unshippable. A break inserted into such a run is pure
    layout: strip the breaks and the string is byte-identical to what came in, which is the
    same invariant every other re-wrap here honours. See :func:`_wrap_run` for the conditions.
    """
    flat = _flat(text)
    run = _wrap_run(flat)
    return run if run is not None else reflow.wrap_balance(flat)


def _run_counts(text):
    """Counter of the token's case-folded characters."""
    return Counter(c.casefold() for c in text)


def _is_run(text):
    """One repeated character, with at most ``RUN_STRAYS`` strays elsewhere.

    ``'A' + 'H' * 56`` (the shipped FMA:B S01E41 scream) is a run: one 'A', 56 H's.
    ``'Kekekekeke...'`` is not -- it alternates, so its minority is half the token and
    belongs to the word-repair path, not a layout break.
    """
    counts = _run_counts(text)
    if len(counts) == 1:
        return True
    return len(counts) == 2 and min(counts.values()) <= RUN_STRAYS


def _wrap_run(text, max_line=GATE_MAX_LINE):
    """Break a single run of one repeated character into equal lines, or None.

    Only for the case no word-boundary wrap can reach: ONE token (no whitespace) that is a
    run (:func:`_is_run`) and longer than the line budget. The split is even (the two lines
    differ by at most one character), so a scream reads as two balanced lines rather than a
    full line and a stub.

    It deliberately does not consider the card's total length: a 446-character run is
    wrap-able by this function and STILL refused by the gate on ``MAX_CHARS``, so making
    it the caller's (``cue_faults``) finding keeps this a pure layout helper.
    """
    text = _flat(text)
    if len(text) <= max_line or " " in text:
        return None
    if not _is_run(text):
        return None
    parts = -(-len(text) // max_line)  # ceil: how many lines the run needs
    size = -(-len(text) // parts)  # ceil: even lines, every one inside the budget
    return "\n".join(text[i:i + size] for i in range(0, len(text), size))


def _own_speech(words, text):
    """(first start, last end) of the earliest run of ASR words that spells this card's text.

    A card can outlive its own words for a reason that is not a layout fault: the word
    stream repeated the sentence (an ASR loop), so a card timed from the first word to the
    last repetition spans seconds of speech its text only says once. Matching the text
    against the window finds the utterance that actually belongs to the card. Returns None
    when no such run exists (the card is not a repetition artifact).
    """
    if not words:
        return None
    tokens = [t.casefold() for t in _flat(text).split()]
    norm = [str(w.get("text", "")).strip().casefold() for w in words]
    n = len(tokens)
    if not n or n > len(norm):
        return None
    for i in range(len(norm) - n + 1):
        if norm[i:i + n] != tokens:
            continue
        run = [w for w in words[i:i + n]
               if w.get("start") is not None and w.get("end") is not None]
        if not run:
            return None
        return min(w["start"] for w in run), max(w["end"] for w in run)
    return None


def _trim_to_speech(text, start, end, words, next_start):
    """Where the card should end if its own speech ends sooner than the card does.

    Only the END moves, only into silence, and only when the card's text really is spoken
    inside the budget -- a card whose text genuinely runs nine seconds is split instead.
    The result can never run into the next card.
    """
    span = _own_speech(words, text)
    if span is None:
        return None
    spoken_start, spoken_end = span
    if spoken_start - start > reflow.GAP_MAX:
        return None  # the words start long after the card does: not the same fault
    if spoken_end - start > GATE_MAX_DUR - reflow.EPS:
        return None  # the text itself needs more than one card -> split
    target = min(end, spoken_end + reflow.LINGER)
    if next_start is not None:
        target = min(target, next_start - reflow.MIN_GAP)
    if target - start < reflow.MIN_DUR:
        return None  # clamped behind the next card or bellow MIN_DUR: not a trim
    if end - target < 0.1:
        return None
    return target


def _pieces(text, start, end, words, next_start=None, is_last=False, depth=0):
    """Make ``text`` shown over [start, end] a legal card, or several.

    Returns a list of ``{start, end, text}`` or ``None`` when no legal repair exists.
    Ranked candidate boundaries and the timing math come from ``card_split``, which is
    where the pipeline's one definition of "a legal cut" lives; the gate's duration
    budget is enforced here because 7.5 s is not part of that profile.
    """
    wrapped = _wrap(text)
    dur = end - start
    faults = cue_faults(wrapped, dur)
    if not faults:
        piece = {"start": start, "end": end, "text": wrapped}
        if _wrap_run(_flat(text)) is not None:
            # The break inside this card came from the run wrap, not from a word boundary.
            piece["run_wrap"] = True
        return [piece]
    if depth > 8:  # a legal cut that leaves an illegal card, eight times over: stop
        return None

    # 1. An over-duration card that is not too long to read: end it where its own speech
    #    ends. That is the repetition/stretch case, and trimming silence keeps the text
    #    whole instead of chopping one sentence into one-word cards.
    if words and not is_last and all("s card" in f for f in faults):
        target = _trim_to_speech(text, start, end, words, next_start)
        if target is not None:
            return [{"start": start, "end": target, "text": wrapped, "trimmed": True}]

    # 2. Otherwise split at the best available boundary, recursively.
    pair = card_split.find_legal_split(_flat(text), start, end, words)
    if pair is None:
        return None
    out = []
    for half in pair:
        sub_words = (card_split.card_words({"words": words}, half["start"], half["end"])
                     if words else None)
        pieces = _pieces(half["text"], half["start"], half["end"], sub_words,
                         next_start if half is pair[-1] else pair[1]["start"],
                         is_last and half is pair[-1], depth + 1)
        if pieces is None:
            return None
        out.extend(pieces)
    return out


def _why_unsplittable(text):
    flat = _flat(text)
    if " " not in flat:
        run = _wrap_run(flat)
        if run is not None:
            counts = _run_counts(flat)
            shape = (f"{len(flat)} identical chars" if len(counts) == 1 else
                     f"{len(flat)} chars, one letter repeated {max(counts.values())} times")
            wrapped = " ".join(run.split("\n"))
            if len(wrapped) > GATE_MAX_CHARS:
                return (f"single run of {shape}: a line break fits the "
                        f"{GATE_MAX_LINE}-char line, but the card's total "
                        f"({len(wrapped)} with the break) is over the gate's "
                        f"{GATE_MAX_CHARS}-char budget, so it cannot be made displayable "
                        f"without changing words")
            return (f"single run of {shape}: the line break fits, but "
                    f"the card is still over the {GATE_MAX_DUR:.1f}s budget and a repair "
                    f"may not change words (splitting a single run across cards is not a "
                    f"layout repair)")
        return (f"single unbroken token of {len(flat)} chars: there is no word boundary to "
                f"wrap or split at, and a repair may not change words")
    return (f"no word/sentence/clause boundary leaves two cards inside the gate's budget "
            f"({GATE_MAX_CHARS} chars, {GATE_MAX_LINE}/line, {GATE_MAX_DUR:.1f}s)")


def fix_cues(cues, words_doc=None, max_line=GATE_MAX_LINE):
    """Apply the layout repair to a cue list.

    ``cues`` is a list of ``(start, end, text)`` with real newlines. Returns
    ``(new_cues, actions, origin)``: the rewritten cue list, one record per card touched
    with its before/after metrics, and -- per output cue -- the index of the card it came
    from. An untouched card keeps its original bytes; a split card contributes several
    output cues with the same origin. ``(None, reason, None)`` when a card cannot be
    normalized.
    """
    new_cues, actions, origin = [], [], []
    for index, (start, end, text) in enumerate(cues):
        flat = _flat(text)
        dur = end - start
        if not cue_faults(text, dur, max_line):
            new_cues.append((start, end, text))
            origin.append(index)
            continue
        # A music marker that outlives the screen budget is not a layout fault and has no
        # words to keep: drop it through the song/music policy. Never wrap or split-bail.
        if is_music_marker(flat) and dur > GATE_MAX_DUR + reflow.EPS:
            actions.append({
                "index": index,
                "action": "drop-music",
                "before": {"start": round(start, 3), "end": round(end, 3),
                           "dur": round(dur, 3), "chars": len(flat),
                           "longest": _line_len(text)},
                "after": [],
            })
            continue
        words = card_split.card_words(words_doc, start, end) if words_doc else None
        next_start = cues[index + 1][0] if index + 1 < len(cues) else None
        pieces = _pieces(text, start, end, words, next_start,
                         is_last=index == len(cues) - 1)
        if pieces is None:
            return None, {"index": index, "start": round(start, 3), "end": round(end, 3),
                          "dur": round(dur, 3), "chars": len(flat),
                          "longest": _line_len(text),
                          "reason": _why_unsplittable(text)}, None
        if pieces[0].get("trimmed"):
            action = "trim-end"
        elif len(pieces) > 1:
            action = "split"
        elif pieces[0].get("run_wrap"):
            action = "wrap-run"
        else:
            action = "rewrap"
        actions.append({
            "index": index,
            "action": action,
            "before": {"start": round(start, 3), "end": round(end, 3),
                       "dur": round(dur, 3), "chars": len(flat),
                       "longest": _line_len(text)},
            "after": [{"start": round(p["start"], 3), "end": round(p["end"], 3),
                       "dur": round(p["end"] - p["start"], 3),
                       "chars": len(_flat(p["text"])),
                       "lines": len(p["text"].split("\n")),
                       "longest": _line_len(p["text"])} for p in pieces],
        })
        new_cues.extend((p["start"], p["end"], p["text"]) for p in pieces)
        origin.extend([index] * len(pieces))
    return new_cues, actions, origin


# --- the correction lane (P24) -------------------------------------------------------


def correct_cues(cues, gloss):
    """Re-apply the show's own curated corrections to a cue list's TEXT.

    ``gloss`` is a dict from ``glossary.load`` (empty means no-op). Each cue is corrected
    one physical line at a time: a card's break is layout the pipeline chose, and
    ``glossary.correct`` works a line at a time, so the break count never moves. Timing is
    copied through untouched -- this lane changes words, never when they are shown.

    Returns ``(cues, corrections)``; ``corrections`` names every card that changed, with
    the text before/after, so the audit says exactly what was re-applied.
    """
    names = gloss.get("names") or []
    token_fixes = gloss.get("token_fixes") or {}
    phrase_fixes = gloss.get("phrase_fixes") or {}
    if not (names or token_fixes or phrase_fixes):
        return cues, []
    rules = {"names": names, "token_fixes": token_fixes, "phrase_fixes": phrase_fixes}
    out, corrections = [], []
    for index, (start, end, text) in enumerate(cues):
        lines, changed = [], 0
        for line in text.split("\n"):
            fixed, n = glossary_text.correct(line, rules)
            lines.append(fixed)
            changed += n
        if not changed:
            out.append((start, end, text))
            continue
        new_text = "\n".join(lines)
        out.append((start, end, new_text))
        corrections.append({"index": index, "changes": changed,
                            "before": text, "after": new_text})
    return out, corrections


def _corrections_summary(corrections, limit=3):
    """One line naming what the glossary fixed, before -> after (first few cards)."""
    parts = []
    for c in corrections[:limit]:
        parts.append(f"card #{c['index']} {c['before']!r} -> {c['after']!r}")
    more = len(corrections) - len(parts)
    return ("re-applied the glossary's corrections to "
            f"{len(corrections)} card(s): " + "; ".join(parts)
            + (f"; +{more} more" if more > 0 else ""))


def _parse_rendered(path, text):
    """Parse a rendered sidecar the same way the gate does."""
    if path.endswith(".srt"):
        return publish_gate.parse_srt(text)
    styles = set(re.findall(r"^Style:\s*([^,]+)", text, re.M))
    return publish_gate.parse_ass(text, style="Dubtitles" if "Dubtitles" in styles else None)


def _write_product(path, new_text):
    """Replace the product in place, after keeping one pre-fix copy."""
    _backup(path)
    atomic_write(path, lambda f: f.write(new_text), mode=MODE_KEEP)


# --- reading and writing the artifact ------------------------------------------------


def _fmt_ass(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t - h * 3600) // 60)
    s = t - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _fmt_srt(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t - h * 3600) // 60)
    s = t - h * 3600 - m * 60
    ms = int(round((s - int(s)) * 1000))
    if ms == 1000:
        ms, s = 0, s + 1
        if int(s) == 60:
            s, m = 0, m + 1
            if m == 60:
                m, h = 0, h + 1
    return f"{h:02d}:{m:02d}:{int(s):02d},{ms:03d}"


_ASS_TAGS = re.compile(r"^((?:\{[^}]*\})*)")


def _ass_events(lines):
    """Index every scored Dialogue line: its line number, its prefix fields, its text.

    Mirrors ``publish_gate.load_cues``: on a styled sidecar the scored dialogue is the
    ``Dubtitles`` style; signs, lyrics and title cards are left exactly where they are.
    """
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "[Events]":
            start = i
            break
    if start is None:
        return []
    fmt = next((ln for ln in lines[start:] if ln.startswith("Format:")), "")
    fields = [f.strip() for f in fmt[len("Format:"):].split(",")] if fmt else []
    nf = max(1, len(fields))
    styles = set(re.findall(r"^Style:\s*([^,]+)", "\n".join(lines), re.M))
    style = "Dubtitles" if "Dubtitles" in styles else None
    events = []
    for i in range(start, len(lines)):
        line = lines[i]
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", nf - 1)
        if len(parts) < nf:
            continue
        if style and parts[3].strip() != style:
            continue
        events.append({"line": i, "prefix": parts[:nf - 1], "text": parts[-1]})
    return events


def render_ass(text, cues, origin, touched, dropped=frozenset()):
    """Rewrite only the cards the repair touched, in place, preserving every other byte.

    Editing lines rather than re-serialising through a subtitle library is deliberate: a
    re-serialise loses the file's own override tags and can move sign/song events, which
    this pipeline promises not to do.

    ``dropped`` names source cards removed from the output entirely (a music marker); their
    Dialogue lines are deleted and every other line keeps its bytes.
    """
    lines = text.splitlines()
    events = _ass_events(lines)
    if len(events) != len({*origin} | set(dropped)):
        raise ValueError(f"{len(events)} dialogue events but "
                         f"{len({*origin} | set(dropped))} source cards")
    groups = {}
    for k, (start, end, body) in enumerate(cues):
        groups.setdefault(origin[k], []).append((start, end, body))
    replacement = {}
    for i, ev in enumerate(events):
        if i in dropped:
            replacement[ev["line"]] = None  # marker: this Dialogue line is removed
            continue
        if i not in touched:
            continue
        tags = _ASS_TAGS.match(ev["text"]).group(1)
        out = []
        for start, end, body in groups[i]:
            prefix = list(ev["prefix"])
            prefix[1] = _fmt_ass(start)
            prefix[2] = _fmt_ass(end)
            out.append(",".join(prefix) + "," + tags + body.replace("\n", "\\N"))
        replacement[ev["line"]] = "\n".join(out)
    out = []
    for i, line in enumerate(lines):
        if i in replacement:
            if replacement[i] is not None:
                out.append(replacement[i])
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _srt_blocks(text):
    norm = text.replace("\r\n", "\n")
    blocks = []
    for raw in re.split(r"\n\s*\n", norm.strip()):
        lines = raw.splitlines()
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        a, b = lines[1].split("-->")
        blocks.append({"start": publish_gate._srt_time(a.strip()),
                       "end": publish_gate._srt_time(b.strip()),
                       "body": "\n".join(lines[2:])})
    return blocks


def render_srt(text, cues, origin, touched, dropped=frozenset()):
    blocks = _srt_blocks(text)
    if len(blocks) != len({*origin} | set(dropped)):
        raise ValueError(f"{len(blocks)} srt blocks but "
                         f"{len({*origin} | set(dropped))} source cards")
    out = []
    for i, (start, end, body) in enumerate(cues, 1):
        if origin[i - 1] not in touched:
            body = blocks[origin[i - 1]]["body"]
        out.append(f"{i}\n{_fmt_srt(start)} --> {_fmt_srt(end)}\n{body}")
    return "\n\n".join(out) + ("\n" if text.endswith("\n") else "")


def load_artifact(stem):
    """The file the gate scored: (path, cues, renderer) or (None, [], None)."""
    path, cues = publish_gate.load_cues(stem)
    if not path:
        return None, [], None
    text = open(path, encoding="utf-8-sig", errors="replace").read()
    if path.endswith(".srt"):
        return path, cues, lambda new, org, touched, dropped=frozenset(): render_srt(
            text, new, org, touched, dropped)
    return path, cues, lambda new, org, touched, dropped=frozenset(): render_ass(
        text, new, org, touched, dropped)


# --- audit ---------------------------------------------------------------------------


def _audit_path(stem):
    # `common` imports pysubs2 (see core/atomic_file.py's docstring): only the OUTPUT_ROOT
    # redirect and the stage record need it, so both are resolved where they are used and
    # the fixer still runs -- and dry-runs -- in an environment the gate itself can run in.
    try:
        from dubtitlerr.core import common
        return common.out_for(stem + AUDIT_SUFFIX)
    except ImportError:
        return stem + AUDIT_SUFFIX


def read_audit(stem):
    try:
        with open(_audit_path(stem)) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_audit(stem, doc):
    atomic_write(_audit_path(stem), lambda f: json.dump(doc, f, indent=1))


def _signature(fails, artifact):
    """What makes two refusals 'the same' for escalation purposes."""
    blob = (artifact or "") + "\0" + "\0".join(sorted(fails))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# --- parked episodes ------------------------------------------------------------------
#
# A give-up is deterministic: the same artifact, words and code produce the same refusal
# forever. Re-running it every merge pass (every MERGE_INTERVAL) burns the episode's slot
# and proves nothing -- Michael, on four episodes churning every ~10 minutes: "no point in
# it constantly trying over and over again." So the verdict is persisted WITH the code that
# produced it, and the pass skips it until one of two things changes:
#
#   * the ATTEMPT STAMP (tier versions + GATE_FIX_EPOCH) stops matching what is running --
#     a deployed fix retries the episode exactly once, and it either heals or re-parks
#     under the new stamp; or
#   * a run request for the show was created AFTER the park -- an explicit "try this again"
#     beats the parked verdict once, and the re-park is newer than the request, so a second
#     retry needs a second request.
#
# The record lives beside the artifact (`<stem>.dubtitles.parked.json`), so it travels with
# the episode on the same disk branch the rest of the pipeline writes to.


def _park_path(stem):
    try:
        from dubtitlerr.core import common
        return common.out_for(stem + PARK_SUFFIX)
    except ImportError:
        return stem + PARK_SUFFIX


def _glossary_fingerprint(stem):
    """What the correction lane/gate would read from this episode's glossary, hashed.

    Only the fields that decide a correction or a gate finding: names and the hard_fixes
    split (token/phrase). A glossary edit is exactly what heals a parked episode, so it has
    to move the attempt stamp -- otherwise the park would outlive its own fix. Empty string
    when there is no glossary (nothing can change, so nothing should retry).
    """
    try:
        path = publish_gate.glossary_for(stem)
        gloss = glossary_text.load(path) if path else {}
    except Exception:  # noqa: BLE001 - a glossary that cannot load must not break the stamp
        return ""
    if not gloss:
        return ""
    blob = json.dumps({"names": sorted(gloss.get("names") or []),
                       "token": sorted((gloss.get("token_fixes") or {}).items()),
                       "phrase": sorted((gloss.get("phrase_fixes") or {}).items())},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def attempt_stamp(stem=None):
    """The runtime values that decide whether a parked verdict is still authoritative.

    ``stem`` adds the glossary fingerprint, so a glossary change re-attempts that show's
    parked episodes exactly once (P17's retry discipline, extended to the glossary). The
    no-argument form is the version-only stamp: that is all a park record from before the
    glossary field carries.
    """
    from dubtitlerr.core import common
    stamp = {"transcribe_version": common.TRANSCRIBE_VERSION,
             "text_version": common.TEXT_VERSION,
             "gate_fix_epoch": GATE_FIX_EPOCH}
    if stem is not None:
        stamp["glossary"] = _glossary_fingerprint(stem)
    return stamp


def read_park(stem):
    try:
        with open(_park_path(stem)) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


class ParkRecordError(ValueError):
    """A park file claims a give-up but its fields cannot be trusted (a hand edit, a bad clock)."""


def validate_park(doc):
    """Raise ParkRecordError unless a parked document carries the fields that are read from it.

    Read strictly on purpose: the alternative -- letting a bad ``at`` raise ValueError inside a
    caller that reads any exception as "the verdict no longer stands" -- silently unparked the
    episode and deleted the record. A record nobody can vouch for stays parked until a human
    looks at it.

    The required stamp keys are the version/epoch triple, which every park record has always
    carried. The glossary fingerprint is COMPARED, not required: a record written before it
    existed is merely stale (a retry heals or re-parks it), never unusable.
    """
    at = doc.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0:
        raise ParkRecordError(f"park record has no usable 'at' timestamp: {at!r}")
    stamp, now = doc.get("attempt_stamp"), attempt_stamp()
    # Typed against TODAY's stamp, not against int: if a version ever becomes a string the
    # validator follows it, so a legitimate record is never called unusable.
    if not isinstance(stamp, dict) or not all(
            k in stamp and type(stamp[k]) is type(now[k]) for k in now):
        raise ParkRecordError(f"park record has no usable attempt stamp: {stamp!r}")


def _write_park(stem, result, at):
    doc = {
        "parked": True,
        "at": at,
        "outcome": result["outcome"],
        "summary": result["summary"],
        "fails": result.get("fails_after") or result.get("fails", []),
        "attempt_stamp": attempt_stamp(stem),
    }
    atomic_write(_park_path(stem), lambda f: json.dump(doc, f, indent=1))


def clear_park(stem):
    """Drop a parked verdict (healed, or about to be re-attempted). Never raises."""
    try:
        os.remove(_park_path(stem))
    except OSError:
        pass


def _requests_dir():
    return os.environ.get("REQUESTS_DIR") or os.path.join(
        os.environ.get("CONFIG_DIR", "/config"), "requests")


def request_since(show, when):
    """True if a queued/finished run request for ``show`` was created after ``when``.

    A request file's mtime is its creation time (a pending -> done move is a rename), so
    "newer than the park" is exactly "somebody asked again after this episode gave up".
    Read-only, and a broken/unreadable queue means no request: the parked verdict stands.
    """
    if not show:
        return False
    rdir = _requests_dir()
    for d in (rdir, os.path.join(rdir, "done")):
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for entry in entries:
            if (not entry.is_file() or entry.name.startswith(".")
                    or not entry.name.endswith((".json", ".running"))):
                continue
            try:
                if entry.stat().st_mtime <= when:
                    continue
                with open(entry.path, encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and doc.get("show") == show:
                return True
    return False


def parked_state(stem, show=None):
    """``(parked, note)`` -- is this episode's give-up still authoritative?

    ``parked`` True means an ordinary pass must not re-attempt it. False with a non-empty
    note means a parked verdict exists but is stale (a code/version change or a newer run
    request), i.e. retry this pass.
    """
    doc = read_park(stem)
    if not doc.get("parked"):
        return False, ""
    validate_park(doc)
    stamp = doc.get("attempt_stamp")
    if stamp != attempt_stamp(stem):
        return False, "the attempt stamp changed since the park (a fix or version bump)"
    if request_since(show, float(doc.get("at") or 0)):
        return False, "a run request was created after the park"
    return True, (f"gate_fix gave up ({doc.get('outcome')}): {doc.get('summary', '')}"
                  + " — retries on a version/epoch change or a new run request")


# --- one attempt ---------------------------------------------------------------------


def _fixed_summary(actions):
    """One line naming what was done to which card, before -> after."""
    parts = []
    for a in actions:
        before = a["before"]["dur"]
        if a["action"] == "trim-end":
            parts.append(f"card #{a['index']} ended at its own speech "
                         f"({before:.2f}s -> {a['after'][0]['dur']:.2f}s)")
        elif a["action"] == "drop-music":
            parts.append(f"card #{a['index']} dropped (music marker over "
                         f"{GATE_MAX_DUR:.1f}s, {before:.1f}s, no words)")
        elif a["action"] == "split":
            parts.append(f"card #{a['index']} split into {len(a['after'])} "
                         f"(longest {max(x['dur'] for x in a['after']):.2f}s)")
        elif a["action"] == "wrap-run":
            parts.append(f"card #{a['index']} repeated-character run wrapped onto "
                         f"{a['after'][0]['lines']} lines "
                         f"(longest {a['after'][0]['longest']} chars)")
        else:
            parts.append(f"card #{a['index']} re-wrapped")
    return "; ".join(parts)


def attempt(stem, duration=None, max_line=GATE_MAX_LINE, dry_run=False):
    """Run the bounded repair once. Returns a result dict; writes only when it fixes.

    The returned ``code`` is the CLI's exit code (see the module docstring).
    """
    path, cues, render = load_artifact(stem)
    result = {"artifact": path, "code": 1, "outcome": OUTCOME_STILL_FAILING,
              "summary": "", "fails": [], "fails_after": [], "fixes": []}
    if not path:
        result.update(outcome=OUTCOME_CANNOT_EVALUATE,
                      summary=f"no subtitle artifact for {os.path.basename(stem)}"
                              f" (the gate cannot evaluate it)")
        return _finish(stem, result, dry_run, parked=True)

    dur = duration if duration is not None else publish_gate.video_duration(stem)
    gloss_path = publish_gate.glossary_for(stem)
    mishears = publish_gate.glossary_mishears(gloss_path) if gloss_path else []
    gloss = glossary_text.load(gloss_path) if gloss_path else {}
    before = publish_gate.evaluate(cues, dur, mishears, max_line=max_line)
    result["fails"] = before
    if not before:
        result.update(code=0, outcome=OUTCOME_NOOP,
                      summary=f"{os.path.basename(path)} already passes the gate")
        if not dry_run:
            clear_park(stem)  # a healed episode must not stay parked
        return result  # NO-OP: no audit entry, no stage record, no bytes touched

    # P24: the correction lane. A product assembled before a glossary fix landed still
    # spells the mishear the glossary NOW knows how to fix, and that used to park forever.
    # Re-apply the show's own curated corrections to the stored text first; the in-memory
    # re-gate below decides whether that heals the file, so a partial correction is never
    # written. Timing and line breaks are carried through untouched (see correct_cues).
    cues, corrections = correct_cues(cues, gloss)
    corrected = {c["index"] for c in corrections}
    after = before
    if corrections:
        result["corrections"] = corrections
        corrected_text = render(cues, range(len(cues)), corrected)
        after = publish_gate.evaluate(_parse_rendered(path, corrected_text), dur, mishears,
                                      max_line=max_line)
        result["fails_after"] = after
        if not after:
            result.update(code=0, outcome=OUTCOME_CORRECTED,
                          summary=_corrections_summary(corrections) + " — gate passes")
            if not dry_run:
                _write_product(path, corrected_text)
                _record(stem, result, parked=False, failures_before=before)
            return result

    _layout, other = split_fails(after)
    music = [i for i, (start, end, text) in enumerate(cues)
             if is_music_marker(_flat(text)) and end - start > GATE_MAX_DUR + reflow.EPS]
    # A non-layout finding that the correction lane could not heal is still not attempted --
    # UNLESS the file also carries music markers to drop. A marker is not dialogue, and
    # removing it is not a transcript repair; it can also lift the punctuation ratio
    # honestly (Blue Eye S01E06: 59.4% before its three over-duration '♪♪' cards, 60.2%
    # after). The in-memory re-gate decides: if any finding survives, nothing is written.
    if other and not music:
        summary = ("not a layout fault (needs the transcript or a model): "
                   + "; ".join(other))
        if corrections:
            summary = (_corrections_summary(corrections)
                       + " — the gate still refuses: " + "; ".join(other))
        result.update(outcome=OUTCOME_UNSUPPORTED, summary=summary)
        return _finish(stem, result, dry_run, parked=True)

    words_doc = None
    words_path = stem + ".dubtitles.words.json"
    if os.path.exists(words_path):
        try:
            with open(words_path) as f:
                words_doc = json.load(f)
        except (OSError, ValueError):
            words_doc = None

    new_cues, actions, origin = fix_cues(cues, words_doc, max_line=max_line)
    if new_cues is None:
        result.update(outcome=OUTCOME_UNFIXABLE, fix=actions,
                      summary=(f"card #{actions['index']} cannot be normalized: "
                               + actions["reason"]))
        return _finish(stem, result, dry_run, parked=True)
    result["fixes"] = actions

    # Re-gate IN MEMORY before a byte is written: a partial fix must never ship. A card the
    # correction lane rewrote is repainted too, even when the layout left it alone.
    touched = {a["index"] for a in actions} | corrected
    dropped = {a["index"] for a in actions if a["action"] == "drop-music"}
    new_text = render(new_cues, origin, touched, dropped)
    after_cues = _parse_rendered(path, new_text)
    after = publish_gate.evaluate(after_cues, dur, mishears, max_line=max_line)
    result["fails_after"] = after
    if after:
        result.update(outcome=OUTCOME_STILL_FAILING,
                      summary="fixed the layout but the gate still refuses: "
                              + "; ".join(after))
        return _finish(stem, result, dry_run, parked=True)

    summary = _fixed_summary(actions) + " — gate passes"
    if corrections:
        summary = _corrections_summary(corrections) + "; " + summary
    result.update(code=0, outcome=OUTCOME_FIXED, summary=summary)
    if not dry_run:
        _write_product(path, new_text)
        _record(stem, result, parked=False, failures_before=before)
    return result


def _finish(stem, result, dry_run, parked):
    """Attach the exit code and (when not a dry run) persist the attempt."""
    if dry_run:
        result["code"] = 1 if parked else 0
        return result
    _record(stem, result, parked=parked)
    return result


def _backup(path):
    """One pre-fix copy, refreshed per attempt: the artifact is a product, not scratch."""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as src:
            text = src.read()
        atomic_write(path + BACKUP_SUFFIX, lambda f: f.write(text), mode=MODE_KEEP)
    except OSError:
        pass  # a backup that cannot land must not block the fix itself


def _record(stem, result, parked, failures_before=None):
    """Append the attempt to the audit file, write the stage record, set the exit code.

    "Parked" means a deterministic give-up. It is persisted as
    ``<stem>.dubtitles.parked.json`` with the attempt stamp that produced it, so ordinary
    passes skip the episode instead of re-attempting it; see :func:`parked_state`. The
    escalation block still remembers the signature of the last refusal it surfaced, so a
    standing refusal is notified once and then recorded as already-known (exit 1).
    """
    doc = read_audit(stem)
    attempts = doc.get("attempts") if isinstance(doc.get("attempts"), list) else []
    fails_before = (failures_before if failures_before is not None
                    else result["fails"])
    entry = {"at": time.time(), "outcome": result["outcome"],
             "summary": result["summary"], "artifact": result.get("artifact"),
             "fails_before": fails_before, "fails_after": result.get("fails_after", []),
             "fixes": result.get("fixes", [])}
    if result.get("corrections"):
        entry["corrections"] = result["corrections"]
    if result.get("fix"):
        entry["unfixable"] = result["fix"]
    attempts.append(entry)
    doc["attempts"] = attempts[-MAX_AUDIT_ATTEMPTS:]
    doc["artifact"] = result.get("artifact")
    dropped = sum(1 for a in entry["fixes"] if a.get("action") == "drop-music")
    if dropped:
        doc["music_cues_dropped"] = int(doc.get("music_cues_dropped") or 0) + dropped

    sig = _signature(result.get("fails_after") or fails_before,
                     result.get("artifact"))
    previous = (doc.get("escalation") or {}).get("signature")
    if parked:
        if previous == sig:
            result["code"] = 1  # known refusal: keep the log line, skip the notification
        else:
            doc["escalation"] = {"signature": sig, "at": entry["at"],
                                 "reason": result["summary"]}
            result["code"] = 3
    else:
        result["code"] = 0 if result["outcome"] in PASSED else 1
    # Persist (or drop) the parked verdict. A give-up is remembered WITH the stamp that
    # produced it; anything else clears it, because this pass just re-examined the file.
    if parked and result["outcome"] in PARKED_OUTCOMES:
        try:
            _write_park(stem, result, entry["at"])
        except OSError:
            pass  # the audit + stage record still record the refusal
    else:
        clear_park(stem)
    _write_audit(stem, doc)
    outcome = "ok" if result["outcome"] in PASSED else "refused"
    try:
        from dubtitlerr.core import common
        common.write_stage(stem, "gate_fix", outcome,
                           f"{result['outcome']}: {result['summary']}"[:300])
    except (ImportError, OSError, ValueError):
        pass


# --- CLI -----------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("stem", help="path stem without .eng.dubtitles.<ext>")
    ap.add_argument("--duration", type=float, default=None,
                    help="container duration in seconds (default: ffprobe the video)")
    ap.add_argument("--max-line", type=int, default=GATE_MAX_LINE)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be fixed; write nothing at all")
    ap.add_argument("--parked", action="store_true",
                    help="exit 0 if this episode is parked and the verdict still stands; "
                         "print the reason. Writes nothing.")
    ap.add_argument("--unpark", action="store_true",
                    help="drop the parked verdict so the next attempt runs now")
    ap.add_argument("--show", default=None,
                    help="--parked: show name, to honour a run request created after the park")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.parked:
        try:
            parked, note = parked_state(a.stem, show=a.show)
        except ParkRecordError as exc:  # exit 3: "cannot tell" -- never "not parked"
            print(f"gate_fix: {os.path.basename(a.stem)}: {exc}")
            return 3
        if note:
            print(note)
        return 0 if parked else 1
    if a.unpark:
        clear_park(a.stem)
        return 0
    try:
        result = attempt(a.stem, duration=a.duration, max_line=a.max_line,
                         dry_run=a.dry_run)
    except (OSError, ValueError) as exc:
        print(f"gate_fix: cannot evaluate {os.path.basename(a.stem)}: {exc}")
        return 2
    if a.json:
        print(json.dumps({k: v for k, v in result.items() if k != "code"}, indent=1))
    else:
        if result["outcome"] == OUTCOME_FIXED and a.dry_run:
            head = "gate_fix: would fix"
        else:
            head = "gate_fix:"
        print(f"{head} {os.path.basename(a.stem)} — {result['outcome']}: "
              f"{result['summary']}")
    return result["code"]


if __name__ == "__main__":
    sys.exit(main())
