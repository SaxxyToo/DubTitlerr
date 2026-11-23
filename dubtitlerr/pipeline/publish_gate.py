#!/usr/bin/env python3
"""Refuse to publish a dubtitle sidecar that is not one.

The merge pass used to publish whatever it assembled: Neon Genesis Evangelion shipped 25
episodes with 90-100% unpunctuated cards, `Oscar` for Asuka 28 times, and E23 as a 127-cue
file ending at 11:26 of a 24:30 episode. `.dubtitles.done` said "ok" for all of them.

This gate runs on the assembled dialogue before mux and fails closed. It answers four
questions that a human reviewer asks in the first ten seconds:

  * does it cover the episode?          last cue end / container duration >= 90%
  * is it read as text?                 terminal punctuation, lowercase orphans
  * does it fit the screen?             <= 2 x 42 chars per card, <= 7.5 s per card
  * does it use the show's names?       no `hard_fixes` key from the show glossary

Pure stdlib for parsing; ffprobe is only needed for the duration (skipped with --duration).

Exit codes: 0 pass, 1 below threshold, 2 cannot evaluate (missing artifact).
"""

import argparse
import json
import os
import re
import subprocess
import sys

from dubtitlerr.core import song_spans

MAX_CHARS = 88
MAX_LINE = 46
MAX_DUR = 7.5
MIN_COVER = 0.90
MIN_CARDS_PER_MIN = 6.0
# A sentence split across two cues leaves the first without a TERMINAL mark, but it is
# still punctuated text: it ends at the clause it was cut on, and that is what separates a
# subtitle from a transcript. Counting only terminal marks mixed the two questions
# together -- a show whose cards are cut mid-sentence looked exactly like raw ASR.
#
# Measured 2026-11-21 over every dubtitle artifact in the live library (1,063 files):
# counting a card as punctuated when it ends with ANY punctuation mark, the unpunctuated
# band tops out at 27.1% (the raw-era FMA:B and Apothecary S02 files, which also carry
# 0.0-8.9 punctuation marks per 100 words) and the lowest real sidecar is 86 Eighty-Six
# S01E01 at 60.9% (325 cards; 13.4 marks/100 words). 0.50 sits under the real floor with
# a 1.8x margin and above the raw band with a 1.8x margin. Every raw-era file, including
# the 0.0% Death Note S01E10 control, fails by more than 30 points.
MIN_PUNCT_ENDED = 0.50
MAX_ORPHAN = 0.15
MIN_NAME_LEN = 3

# Sentence-terminal marks: what the orphan rule and "does this card end a sentence" read.
END = (".", "!", "?", "\u2026", '"', "'", "\u201d", "\u2019")
# Any punctuation a card can legitimately end on, clause marks included -- a card cut at a
# comma is punctuated text continuing a sentence (see MIN_PUNCT_ENDED).
PUNCT = END + (",", ";", ":", "\u2013", "\u2014")
GLOSSARY_DIR = os.environ.get("GLOSSARY_DIR", "/config/glossaries")


def _srt_time(s):
    h, m, rest = s.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def _ass_time(s):
    h, m, rest = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def parse_srt(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        a, b = lines[1].split("-->")
        body = "\n".join(re.sub(r"<[^>]+>|\{[^}]*\}", "", x).strip() for x in lines[2:] if x.strip())
        cues.append((_srt_time(a.strip()), _srt_time(b.strip()), body))
    return cues


def parse_ass(text, style=None):
    cues = []
    head, _, ev = text.partition("[Events]")
    fmt = next((ln for ln in ev.splitlines() if ln.startswith("Format:")), "")
    nf = max(1, len(fmt.split(",")))
    for line in ev.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        p = line.split(",", nf - 1)
        if len(p) < nf:
            continue
        if style and p[3].strip() != style:
            continue
        body = re.sub(r"\{[^}]*\}", "", p[-1]).replace("\\N", "\n").strip()
        cues.append((_ass_time(p[1]), _ass_time(p[2]), body))
    return cues


def load_cues(stem):
    """The pipeline's own artifact first, then the published sidecar.

    merge_pass gates `stem.eng.dubtitles.<ext>`; auditing what is already in the library
    means reading `stem.dub.en.<ext>`, so both names resolve here.
    """
    for suffix, parser in (
        (".eng.dubtitles.srt", parse_srt),
        (".eng.dubtitles.ass", parse_ass),
        (".dub.en.srt", parse_srt),
        (".dub.en.ass", parse_ass),
    ):
        path = stem + suffix
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8-sig", errors="replace").read()
        if parser is parse_ass:
            # A styled sidecar also carries signs, lyrics and title cards. Those are not
            # dialogue: they run long, they have no sentence punctuation, and counting them
            # fails a perfectly good file.
            styles = set(re.findall(r"^Style:\s*([^,]+)", text, re.M))
            return path, parse_ass(text, style="Dubtitles" if "Dubtitles" in styles else None)
        return path, parser(text)
    return None, []


def video_duration(stem):
    for ext in ("mkv", "mp4", "m4v"):
        path = f"{stem}.{ext}"
        if not os.path.exists(path):
            continue
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return float(out.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return None


def glossary_mishears(path):
    """Wrong *words* this show has already decided on.

    A fix that only changes case (`ikari` -> `Ikari`) is a normalisation, not a mishear:
    the token is already the right name and the fixer will case it. Only keys whose
    replacement is a different word (`oscar` -> `Asuka`) are evidence of an ASR error.
    """
    try:
        doc = json.load(open(path))
    except (OSError, ValueError):
        return []
    pairs = list((doc.get("hard_fixes") or {}).items())
    try:
        from dubtitlerr.glossary.glossary import load

        g = load(path) or {}
        pairs += list((g.get("token_fixes") or {}).items())
        pairs += list((g.get("phrase_fixes") or {}).items())
    except Exception:  # noqa: BLE001 - a glossary import failure must not crash the gate
        pass
    return sorted(
        {
            k
            for k, v in pairs
            if len(k) >= MIN_NAME_LEN
            and k.strip().casefold() != str(v).strip().casefold()
            # an expansion (asuka langley -> Asuka Langley Soryu) is not a mishear
            and k.strip().casefold() not in str(v).strip().casefold()
        }
    )


def evaluate(cues, duration=None, mishears=(), max_chars=MAX_CHARS, max_line=MAX_LINE):
    """Return the list of reasons this sidecar must not be published (empty = pass).

    Song spans are not dialogue. A dub re-uses the Japanese OP/ED, and the English ASR
    renders the sung audio as pseudo-romaji with the odd English adaptation line
    (``core/song_spans``). Those cards carry no English to check, so they are left out of
    the punctuation, layout and mishear checks rather than being counted as failures --
    the voice-orchestra lyric region parked Frieren S02E01 forever otherwise.
    """
    fails = []
    if not cues:
        return ["no cues"]
    if duration:
        cover = cues[-1][1] / duration
        if cover < MIN_COVER:
            fails.append(f"coverage {cover * 100:.0f}% (last cue {cues[-1][1]:.1f}s of {duration:.1f}s)")
        per_min = len(cues) / (duration / 60.0)
        if per_min < MIN_CARDS_PER_MIN:
            fails.append(f"{per_min:.1f} cards per minute")
    song = song_spans.indexes(cues)
    dialogue = [c for i, c in enumerate(cues) if i not in song]
    if not dialogue:
        return fails + ["no dialogue cards (the whole file reads as a song)"]
    lines = [t.split("\n") for _, _, t in dialogue]
    longest_line = max((len(x) for ls in lines for x in ls), default=0)
    if longest_line > max_line:
        fails.append(f"line of {longest_line} chars (max {max_line})")
    over = sum(1 for _, _, t in dialogue if len(t.replace("\n", " ")) > max_chars)
    if over:
        fails.append(f"{over} card(s) over {max_chars} chars")
    longdur = [b - a for a, b, _ in dialogue if b - a > MAX_DUR]
    if longdur:
        fails.append(f"{len(longdur)} card(s) over {MAX_DUR:.1f}s (max {max(longdur):.1f}s)")
    ended = sum(1 for _, _, t in dialogue if t.rstrip().endswith(PUNCT))
    if ended / len(dialogue) < MIN_PUNCT_ENDED:
        fails.append(f"{100 * ended / len(dialogue):.0f}% of cards end with punctuation")
    # Lowercase start AFTER a sentence ended = cold-start ASR cut. A lowercase start after an
    # unpunctuated card is a sentence split across two cues, which is normal subtitling.
    if dialogue[0][0] > 290:
        fails.append(f"first cue at {dialogue[0][0]:.0f}s (cold open missing?)")
    orphan = sum(
        1
        for i in range(1, len(dialogue))
        if dialogue[i][2][:1].islower() and dialogue[i - 1][2].rstrip().endswith((".", "!", "?", "\u2026"))
    )
    if orphan / len(dialogue) > MAX_ORPHAN:
        fails.append(f"{100 * orphan / len(dialogue):.0f}% lowercase orphan cards")
    text = " ".join(t.replace("\n", " ") for _, _, t in dialogue)
    hits = [w for w in mishears if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text, re.I)]
    if hits:
        fails.append(f"known mishear(s): {', '.join(sorted(set(hits))[:6])}")
    return fails


# --- "worse than it was", not "not perfect" ---------------------------------------
# The re-pass stages (presentation, restoration) used to restore the previous bytes
# whenever the rebuilt file failed this gate AT ALL. One defect the input already carried
# then discarded every other improvement in the episode: JJK S01E12's 57-char scream card
# (a single line the later gate_fix stage wraps) threw away a 268 -> 215 card rebuild and
# 107 restored cards, and the episode shipped the untouched raw track. A stage's job is
# to not make the gate WORSE, so a revert needs a NEW failure category or a worse value in
# one the input already failed. `evaluate()`'s messages are the vocabulary; this table is
# how they are compared.
#   direction +1: a larger number is better (more coverage, more punctuation)
#   direction -1: a smaller number is better (shorter lines, fewer over-long cards)
#   direction  0: presence alone is the failure; already present is not a regression
_FAIL_RULES = (
    (re.compile(r"coverage ([0-9.]+)%"), "coverage", +1),
    (re.compile(r"([0-9.]+) cards per minute"), "cards per minute", +1),
    (re.compile(r"line of (\d+) chars"), "line length", -1),
    (re.compile(r"(\d+) card\(s\) over \d+ chars"), "card chars", -1),
    (re.compile(r"(\d+) card\(s\) over [0-9.]+s"), "card duration", -1),
    (re.compile(r"(\d+)% of cards end with punctuation"), "punctuation", +1),
    (re.compile(r"first cue at ([0-9.]+)s"), "first cue", -1),
    (re.compile(r"(\d+)% lowercase orphan cards"), "lowercase orphans", -1),
    (re.compile(r"known mishear"), "known mishear", -1),
    (re.compile(r"^no cues$|^no dialogue cards"), "no dialogue", 0),
)


def _finding(msg):
    """``(category, value, direction)`` for one :func:`evaluate` message."""
    for rx, category, direction in _FAIL_RULES:
        m = rx.search(msg)
        if not m:
            continue
        if category == "known mishear":
            return category, float(msg.count(",") + 1), direction  # 'a, b' = two hits
        return category, float(m.group(1)) if m.groups() else 0.0, direction
    return f"unclassified: {msg}", 0.0, 0


def fail_findings(fails):
    """``{category: (value, direction)}`` for a list of :func:`evaluate` refusals.

    A message with no rule is kept verbatim (category ``unclassified: ...``) and treated as
    direction 0, so an unrecognised refusal can never be silently tolerated: it counts as
    already present only when the exact same message was there before.
    """
    out = {}
    for msg in fails:
        category, value, direction = _finding(msg)
        out[category] = (value, direction)
    return out


def regressions(before_fails, after_fails):
    """The refusals in ``after_fails`` that are NEW or WORSE than ``before_fails``.

    Empty means the rebuild did not make the gate worse -- possibly still failing, but no
    failure it did not already inherit. That is the only condition under which a re-pass
    may overwrite the bytes it was given.
    """
    was = fail_findings(before_fails)
    worse = []
    for msg in after_fails:
        category, value, direction = _finding(msg)
        if category not in was:
            worse.append(msg)
            continue
        was_value, _ = was[category]
        if (direction > 0 and value < was_value) or (direction < 0 and value > was_value):
            worse.append(msg)
    return worse


def glossary_for(stem, gloss_dir=None):
    """The show glossary for an episode: first ancestor dir whose name has a <Show>.json."""
    gloss_dir = gloss_dir or GLOSSARY_DIR
    d = os.path.dirname(os.path.abspath(stem))
    while d and d != os.path.dirname(d):
        gp = os.path.join(gloss_dir, os.path.basename(d) + ".json")
        if os.path.exists(gp):
            return gp
        d = os.path.dirname(d)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("stem", help="path stem without .eng.dubtitles.<ext>")
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--glossary", default=None)
    ap.add_argument("--glossary-dir", default=None)
    ap.add_argument("--max-line", type=int, default=MAX_LINE)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    path, cues = load_cues(a.stem)
    if not path:
        print(f"gate: no .eng.dubtitles.srt/.ass for {a.stem}")
        return 2
    duration = a.duration or video_duration(a.stem)
    gloss_path = a.glossary or glossary_for(a.stem, a.glossary_dir)
    mishears = glossary_mishears(gloss_path) if gloss_path else []
    fails = evaluate(cues, duration, mishears, max_line=a.max_line)
    songs = song_spans.describe(cues)
    if a.json:
        print(
            json.dumps({"artifact": path, "cues": len(cues), "duration": duration, "song_spans": songs, "fails": fails}, indent=1)
        )
    elif fails:
        print(f"gate: FAIL {os.path.basename(path)} — {'; '.join(fails)}")
    else:
        covered = f", {100 * cues[-1][1] / duration:.0f}% covered" if duration else ""
        sung = f", {sum(s['cards'] for s in songs)} song card(s) excluded" if songs else ""
        print(f"gate: ok {os.path.basename(path)} ({len(cues)} cues{covered}{sung})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
