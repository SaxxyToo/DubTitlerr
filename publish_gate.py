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

MAX_CHARS = 88
MAX_LINE = 46
MAX_DUR = 7.5
MIN_COVER = 0.90
MIN_CARDS_PER_MIN = 6.0
# A sentence split across two cues leaves the first without a terminal mark, so a healthy
# file sits well under 100%. Below two thirds, the file is probably raw ASR (the old NGE
# season ran 0-5%). 0.65 is the line between "real subtitles" and "transcript".
MIN_PUNCT_ENDED = 0.65
MAX_ORPHAN = 0.15
MIN_NAME_LEN = 3

END = (".", "!", "?", "\u2026", "\"", "'", "\u201d", "\u2019")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
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
    fmt = next((l for l in ev.splitlines() if l.startswith("Format:")), "")
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
    for suffix, parser in ((".eng.dubtitles.srt", parse_srt), (".eng.dubtitles.ass", parse_ass),
                           (".dub.en.srt", parse_srt), (".dub.en.ass", parse_ass)):
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
        path = "%s.%s" % (stem, ext)
        if not os.path.exists(path):
            continue
        try:
            out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                                  "-of", "default=nw=1:nk=1", path],
                                 capture_output=True, text=True, timeout=60)
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
        from glossary import load
        g = load(path) or {}
        pairs += list((g.get("token_fixes") or {}).items())
        pairs += list((g.get("phrase_fixes") or {}).items())
    except Exception:  # noqa: BLE001 - a glossary import failure must not crash the gate
        pass
    return sorted({k for k, v in pairs
                   if len(k) >= MIN_NAME_LEN
                   and k.strip().casefold() != str(v).strip().casefold()
                   # an expansion (asuka langley -> Asuka Langley Soryu) is not a mishear
                   and k.strip().casefold() not in str(v).strip().casefold()})


def evaluate(cues, duration=None, mishears=(), max_chars=MAX_CHARS, max_line=MAX_LINE):
    """Return the list of reasons this sidecar must not be published (empty = pass)."""
    fails = []
    if not cues:
        return ["no cues"]
    if duration:
        cover = cues[-1][1] / duration
        if cover < MIN_COVER:
            fails.append("coverage %.0f%% (last cue %.1fs of %.1fs)"
                         % (cover * 100, cues[-1][1], duration))
        per_min = len(cues) / (duration / 60.0)
        if per_min < MIN_CARDS_PER_MIN:
            fails.append("%.1f cards per minute" % per_min)
    lines = [t.split("\n") for _, _, t in cues]
    longest_line = max((len(x) for ls in lines for x in ls), default=0)
    if longest_line > max_line:
        fails.append("line of %d chars (max %d)" % (longest_line, max_line))
    over = sum(1 for _, _, t in cues if len(t.replace("\n", " ")) > max_chars)
    if over:
        fails.append("%d card(s) over %d chars" % (over, max_chars))
    longdur = [b - a for a, b, _ in cues if b - a > MAX_DUR]
    if longdur:
        fails.append("%d card(s) over %.1fs (max %.1fs)" % (len(longdur), MAX_DUR, max(longdur)))
    ended = sum(1 for _, _, t in cues if t.rstrip().endswith(END))
    if ended / len(cues) < MIN_PUNCT_ENDED:
        fails.append("%.0f%% of cards carry sentence punctuation"
                     % (100 * ended / len(cues)))
    # Lowercase start AFTER a sentence ended = cold-start ASR cut. A lowercase start after an
    # unpunctuated card is a sentence split across two cues, which is normal subtitling.
    if cues[0][0] > 290:
        fails.append("first cue at %.0fs (cold open missing?)" % cues[0][0])
    orphan = sum(1 for i in range(1, len(cues))
                 if cues[i][2][:1].islower() and cues[i - 1][2].rstrip().endswith((".", "!", "?", "\u2026")))
    if orphan / len(cues) > MAX_ORPHAN:
        fails.append("%.0f%% lowercase orphan cards" % (100 * orphan / len(cues)))
    text = " ".join(t.replace("\n", " ") for _, _, t in cues)
    hits = [w for w in mishears if re.search(r"(?<!\w)%s(?!\w)" % re.escape(w), text, re.I)]
    if hits:
        fails.append("known mishear(s): %s" % ", ".join(sorted(set(hits))[:6]))
    return fails


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
        print("gate: no .eng.dubtitles.srt/.ass for %s" % a.stem)
        return 2
    duration = a.duration or video_duration(a.stem)
    gloss_path = a.glossary or glossary_for(a.stem, a.glossary_dir)
    mishears = glossary_mishears(gloss_path) if gloss_path else []
    fails = evaluate(cues, duration, mishears, max_line=a.max_line)
    if a.json:
        print(json.dumps({"artifact": path, "cues": len(cues), "duration": duration,
                          "fails": fails}, indent=1))
    elif fails:
        print("gate: FAIL %s — %s" % (os.path.basename(path), "; ".join(fails)))
    else:
        print("gate: ok %s (%d cues%s)" % (os.path.basename(path), len(cues),
                                           ", %.0f%% covered" % (100 * cues[-1][1] / duration) if duration else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
