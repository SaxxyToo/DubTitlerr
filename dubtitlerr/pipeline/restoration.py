#!/usr/bin/env python3
"""Presentation restoration: punctuation, capitalisation, spacing, house style.

WHAT THIS OWNS
--------------
``presentation`` (P29/P30) decides which words share a card and when that card is
on screen. It deliberately does not touch punctuation or case: those belong here.
The measured result of that split was a library whose *structure* was fixed and
whose *text* was not -- 86 S01E01 sat at 65.2% terminal punctuation and 66.3%
capitalised cards after the structural re-pass, with defects like
``'... your whole crew tomorrow ,'`` (space before the comma), ``'*Screaming*'``
(the source's asterisk sound description) and lowercase sentence starts.

THE OWNER'S RULE, ENFORCED MECHANICALLY
---------------------------------------
Punctuation, capitalisation and spacing may be **added or fixed**.
**Word sequences never change, are never added to, and never lose a word.**

That is not a prompt instruction the model is trusted to follow. :func:`word_tokens`
reduces a line to its ordered alphanumeric tokens -- ASS override blocks, line
escapes, P30's ``- `` turn marker, every punctuation character, case and whitespace
run removed -- and :func:`invariant_ok` compares the two sequences exactly. A
restoration that would alter one token is rejected and the original card is kept,
per card, before anything is written. :func:`restore_cards` then re-checks the whole
card list and refuses the entire pass if even one card drifted; :func:`apply_file`
re-checks the file's dialogue before replacing bytes. Any word-level diff is a hard
failure, never a warning.

HOUSE STYLE
-----------
Proposed here, and fixture-tested against the live artifact:

  * ellipsis      one character ``…``, no spaces: ``Wait...`` -> ``Wait…``
  * quotes        curly ``“ ”`` (straight quotes only where the model wrote them;
                  apostrophes inside words stay apostrophes)
  * sound         parenthesised, not asterisks: ``*Screaming*`` -> ``(Screaming)``
  * spacing       no space before ``, . ; : ! ? …``, one space after sentence
                  punctuation, single spaces between words
  * dashes        a spaced ``--`` becomes an em dash ``—``

Parentheses were chosen over italics because the assembled .ass carries signs
through unchanged and an italic override tag inside a dialogue card would have to
survive wrapping, the publish gate's character counts and the review UI. A
parenthesised description is plain text everywhere, which is also how the sampled
86 E01 line already reads once the asterisks are gone.

WHAT IS SENT, AND WHAT IS NOT
-----------------------------
Only **deficient** cards are sent. "Deficient" is a measured property of the card
and its neighbours, not a model judgement:

  * ``terminal``  a SENTENCE-FINAL card (no next card, or the next starts a new
    sentence) that ends with no mark at all. A card ending in ``,`` is a deliberate
    continuation and is not touched.
  * ``caps``      a SENTENCE-INITIAL card whose first letter is lowercase.
  * ``spacing``   whitespace before punctuation, doubled spaces, a space after an
    opening quote.
  * ``style``     ``*...*`` or a literal ``...``.
  * ``runon``     seven or more words with no internal sentence or clause mark at
    all -- the "no punctuation anywhere" ASR run this pass exists to repair.

A well-formed card is validated (its words are compared before and after) and
never rewritten. A card carrying ASS override tags is a sign/overlay and is left
verbatim: its bytes are presentation, not prose. Cards without an alphanumeric
character (music notes) are skipped for the same reason.

Spacing and style defects are fixed deterministically first -- they need no model
and are identical every run. The model is asked only for what is left. Two axes are
then finished deterministically whatever the model did, because the acceptance bar
must not depend on a 4B model answering: a sentence-final card with no mark at all
gets a period (before any closing quote), and a sentence-initial card's first letter
is uppercased. Both are punctuation/case-only, re-checked by the same invariant.

FAIL-CLOSED
-----------
An unreachable model, an empty answer, an unparseable numbered line or a rejected
guard leaves the card exactly as it was. ``apply_file`` writes through
``presentation._render`` (only the Dubtitles events change), then re-runs the
publish gate on the bytes it wrote and restores the previous file on a refusal.
An episode whose model was unreachable is NOT stamped, so the next pass retries it;
the sweep stops after two consecutive unreachable episodes rather than timing out
against every episode in the library.

IN-APP RE-PASS
--------------
``RESTORATION_EPOCH`` is the behaviour stamp, the same discipline as
``presentation.PRESENTATION_EPOCH`` and ``gate_fix.GATE_FIX_EPOCH``: bump it for
every change to what this module does. A delivered episode carries
``<stem>.dubtitles.restoration.json`` recording the epoch, the outcome, the counters
and the file's fingerprint; ``--sweep`` (run by ``merge_pass.sh`` after the
presentation sweep) restores any delivered episode whose stamp is missing, older,
or predates the file on disk. ``RESTORATION_REPASS=0`` disables it.

CLI
---
  python3 -m dubtitlerr.pipeline.restoration STEM [--dry-run] [--json] [--force]
  python3 -m dubtitlerr.pipeline.restoration --stale STEM     # 0 = stale, 1 = current, 3 = unusable
  python3 -m dubtitlerr.pipeline.restoration --sweep ROOT...  # every delivered file under the roots
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time

from dubtitlerr.core.atomic_file import MODE_KEEP, atomic_write
from dubtitlerr.pipeline import presentation, punctuation

RESTORATION_EPOCH = 1  # 1 = punctuation/caps/spacing/house style (P31)
STAMP_SUFFIX = ".dubtitles.restoration.json"
MAX_BATCH = 12  # cards per model call
MAX_REJECT_EVENTS = 20
ELLIPSIS = "\u2026"

RESTORATION_PASS = os.environ.get("RESTORATION_PASS", "1")

_TERM = ".!?\u2026"
_CLAUSE = ",;:—–"
_TRAILING = "\"'”’)]}*"
_ASS_TAG = re.compile(r"\{[^}]*\}")
_LINE_BREAK = re.compile(r"(?:\\N|\\n|\n)")
_TURN = re.compile(r"^\s*-\s+")
_ALNUM = re.compile(r"[^0-9A-Za-z\u00C0-\u024F]+")
_REPLY_LINE = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)[.)])\s*(.+?)\s*$")


# --- the invariant ------------------------------------------------------------------


def word_tokens(text: str) -> tuple[str, ...]:
    """The line's word sequence: case, punctuation, spacing, ASS tags and turn dashes gone.

    This is the mechanical form of the owner's rule. Alphanumeric runs only, in order,
    casefolded -- so ``"tempo—you"`` is two tokens (the model may punctuate a pause),
    ``"dont"`` and ``"don't"`` are one token (an apostrophe is punctuation), and an ASS
    override block contributes nothing.
    """
    s = _ASS_TAG.sub(" ", str(text or ""))
    s = _LINE_BREAK.sub(" ", s)
    # An apostrophe is punctuation INSIDE a word: removing it must not split the token,
    # or "dont" and "don't" would compare as different words.
    s = re.sub(r"['\u2018\u2019\u02BC]", "", s)
    return tuple(t.casefold() for t in _ALNUM.sub(" ", s).split())


def invariant_ok(before: str, after: str) -> bool:
    """True only when ``after`` says exactly the same words, in the same order."""
    return word_tokens(before) == word_tokens(after)


# --- text helpers -------------------------------------------------------------------


def split_lines(text: str) -> list[str]:
    """Display lines. Cards in memory use ``\\n``; raw .ass text uses ``\\N``."""
    return _LINE_BREAK.split(str(text or ""))


def line_marked(line: str) -> bool:
    return bool(_TURN.match(line))


def markerless(line: str) -> str:
    return _TURN.sub("", line, count=1)


def flatten(text: str) -> str:
    return re.sub(r"\s+", " ", " ".join(split_lines(text))).strip()


def first_alpha(text: str) -> str:
    for ch in flatten(_ASS_TAG.sub(" ", str(text or ""))):
        if ch.isalpha():
            return ch
    return ""


def _body(text: str) -> str:
    t = flatten(text).rstrip()
    while t and t[-1] in _TRAILING:
        t = t[:-1].rstrip()
    return t


def ends_terminal(text: str) -> bool:
    return bool(_body(text)) and _body(text)[-1] in _TERM


def _ends_clause(text: str) -> bool:
    return bool(_body(text)) and _body(text)[-1] in _CLAUSE


def is_styled(text: str) -> bool:
    """True when the card carries ASS override tags: a sign/overlay, never rewritten."""
    return bool(_ASS_TAG.search(str(text or "")))


def is_sentence_final(cards: list[dict], i: int) -> bool:
    """A sentence ends at this card: the next card starts a new one (or there is none)."""
    return i + 1 >= len(cards) or first_alpha(cards[i + 1]["text"]).isupper()


def is_sentence_start(cards: list[dict], i: int) -> bool:
    """A new sentence begins here: first card, after a terminal card, or after a turn."""
    if i == 0 or line_marked(split_lines(cards[i]["text"])[0]):
        return True
    return ends_terminal(cards[i - 1]["text"])


# --- house style --------------------------------------------------------------------


def _curly_quotes(t: str) -> str:
    out = []
    prev = ""
    for ch in t:
        if ch == '"':
            out.append("“" if (not prev or prev.isspace() or prev in "([{—") else "”")
        elif ch == "'":
            nxt = t[len(out) + 1] if len(out) + 1 < len(t) else ""
            if prev.isalnum() and (nxt.isalnum() or nxt == ""):
                out.append("'")  # a contraction apostrophe, not a quote
            else:
                out.append("‘" if (not prev or prev.isspace() or prev in "([{—") else "’")
        else:
            out.append(ch)
        prev = out[-1]
    return "".join(out)


def house_style(text: str) -> str:
    """Apply the house style to one card, preserving its word sequence and line layout."""
    raw = str(text or "")
    # Sound descriptions can span a display line break ("*crickets\nchirping*"). Convert
    # them on the whole card, then drop any stray asterisk: a dialogue card never ships
    # one. The words are untouched, so the invariant still holds.
    raw = re.sub(r"\*([^*]+)\*", lambda m: "(" + re.sub(r"(?:\\N|\\n|\n)", " ", m.group(1)) + ")", raw, flags=re.S)
    raw = raw.replace("*", "")
    lines = []
    for line in split_lines(raw):
        marked = line_marked(line)
        body = markerless(line)
        body = _curly_quotes(body)
        body = re.sub(r"\*([^*\n]+)\*", r"(\1)", body)
        body = re.sub(r"\s*\.{3,}\s*", ELLIPSIS, body)
        body = re.sub(r"\s+--\s+", " — ", body)
        body = re.sub(r"[ \t]+", " ", body).strip()
        body = re.sub(r"\(\s*\)", "", body).strip()
        body = re.sub(r"([,;:!?\u2026])(?=[^\s\d])", r"\1 ", body)
        body = re.sub(r"([.?!])(?=[A-Z])", r"\1 ", body)
        body = re.sub(r"\s+([,.;:!?\u2026”’])", r"\1", body)
        body = re.sub(r"([“‘])\s+", r"\1", body)
        lines.append(("- " if marked else "") + body)
    return "\n".join(lines)


# --- deficiency ---------------------------------------------------------------------


def _words(text: str) -> list[str]:
    return flatten(text).split()


def _spacing_bad(text: str) -> bool:
    flat = flatten(text)
    if re.search(r"\s+[,.;:!?\u2026]", flat) or "  " in flat:
        return True
    if re.search(r"(?:^|[\s(\[—])[“\"‘]\s+", flat):
        return True
    return False


def _style_bad(flat: str) -> bool:
    return "*" in flat or "..." in flat


def _runon(flat: str) -> bool:
    return len(_words(flat)) >= 7 and not re.search(r"[,.;:!?\u2026—–]", flat)


def deficiency(cards: list[dict], i: int) -> list[str]:
    """Reasons this card needs restoration; empty means it is well formed."""
    text = cards[i]["text"]
    if is_styled(text):
        return []
    flat = flatten(text)
    if not any(ch.isalnum() for ch in flat):
        return []  # music notes, pure glyphs
    reasons = []
    if _spacing_bad(text):
        reasons.append("spacing")
    if _style_bad(flat):
        reasons.append("style")
    if is_sentence_start(cards, i) and first_alpha(text).islower():
        reasons.append("caps")
    if is_sentence_final(cards, i) and not ends_terminal(text) and not _ends_clause(text):
        reasons.append("terminal")
    if _runon(flat):
        reasons.append("runon")
    return reasons


def findings(cards: list[dict]) -> dict[int, list[str]]:
    out = {}
    for i in range(len(cards)):
        reasons = deficiency(cards, i)
        if reasons:
            out[i] = reasons
    return out


# --- layout -------------------------------------------------------------------------


def _line_groups(text: str) -> list[tuple[bool, int]]:
    """(turn-marked, WORD count) per display line, with the marker excluded.

    Counted with :func:`word_tokens`, not whitespace: a card that arrived with a
    stray space before its punctuation (``'Stop !'``) has two whitespace tokens but
    one word, and the restored line legitimately has one of each.
    """
    return [(line_marked(line), len(word_tokens(markerless(line)))) for line in split_lines(text)]


def relayout(original: str, restored: str) -> str | None:
    """Lay ``restored`` back onto ``original``'s line breaks and turn markers.

    ``None`` means the layout cannot be reconstructed without violating the card's
    shape (a word count moved) or the publish ceilings -- caller refuses the restore.
    """
    groups = _line_groups(original)
    toks = restored.split()
    if sum(len(word_tokens(t)) for t in toks) != sum(n for _marked, n in groups):
        return None
    out, at = [], 0
    for marked, n in groups:
        line: list[str] = []
        got = 0
        while at < len(toks) and (got < n or not line):
            tok = toks[at]
            line.append(tok)
            got += len(word_tokens(tok))
            at += 1
            if got >= n:
                break
        out.append(("- " if marked else "") + " ".join(line))
    if at != len(toks):
        return None
    text = "\n".join(out)
    if max((len(ln) for ln in out), default=0) > presentation.HARD_LINE:
        return None
    if len(flatten(text)) > presentation.HARD_CHARS:
        return None
    return text


def _fits(text: str) -> bool:
    lines = split_lines(text)
    return max((len(ln) for ln in lines), default=0) <= presentation.HARD_LINE and len(flatten(text)) <= presentation.HARD_CHARS


def capitalize_first(text: str) -> str:
    """Uppercase the first letter of the card (of every turn line), layout untouched."""
    out = []
    for k, line in enumerate(split_lines(text)):
        marked = line_marked(line)
        body = markerless(line)
        if marked or k == 0:
            for j, ch in enumerate(body):
                if ch.isalpha():
                    body = body[:j] + ch.upper() + body[j + 1 :]
                    break
        out.append(("- " if marked else "") + body)
    return "\n".join(out)


def finish_sentence(text: str) -> str:
    """Put a full stop on a sentence-final card that has no mark at all.

    The conservative default when neither the model nor the source supplied a mark:
    a declarative period, placed before any closing quote/bracket. Punctuation only;
    the word sequence is unchanged, which the caller re-checks.
    """
    lines = split_lines(text)
    last = lines[-1]
    body = markerless(last).rstrip()
    at = len(body)
    while at > 0 and body[at - 1] in _TRAILING:
        at -= 1
    lines[-1] = ("- " if line_marked(last) else "") + body[:at] + "." + body[at:]
    return "\n".join(lines)


# --- the model ----------------------------------------------------------------------


def _ask(prompt: str, n_words: int) -> str:
    """One restoration call through the repair lane's own transport.

    Uses the punctuation pass's resolved backend/model (RESTORE_* -> REPAIR_*) so the
    same one-model policy serves both: this is the same text task, one stage later.
    """
    if RESTORATION_PASS == "0" or punctuation.RESTORE_PROBLEM:
        return ""
    return punctuation._ask(prompt, n_words)


def build_prompt(cards: list[dict], batch: list[int]) -> str:
    def line(i: int) -> str:
        return flatten(cards[i]["text"])

    body = []
    if batch and batch[0] > 0:
        body.append(f"context before: {line(batch[0] - 1)}")
    for i in batch:
        body.append(f"[{i}] {line(i)}")
    if batch and batch[-1] + 1 < len(cards):
        body.append(f"context after: {line(batch[-1] + 1)}")
    return (
        "Restore the presentation of these English subtitle cards. Change ONLY punctuation, "
        "capitalisation, spacing and the house style.\n"
        "Rules:\n"
        "- Return the SAME WORDS in the SAME ORDER. Never add, remove, replace, reorder, expand "
        "or translate a word. An apostrophe inside a word is punctuation and is allowed.\n"
        "- Capitalise the first word of every sentence that starts a card.\n"
        "- End a sentence with . ! or ?. If a card continues the sentence into the next card, "
        "end it with a comma or no mark -- do not force a full stop.\n"
        "- Add conservative internal punctuation where a run of words needs it.\n"
        "- House style: ellipsis is the single character … ; quotation marks are curly “ ” ; "
        "a sound description is parenthesised like (Screaming) ; no space before , . ; : ! ?\n"
        "- context lines are for reference only; never return them.\n"
        "- Answer every numbered card exactly once, as [N] corrected text, one per line. "
        "No commentary.\n\n" + "\n".join(body)
    )


def parse_reply(reply: str, wanted: set[int]) -> dict[int, str]:
    out = {}
    for raw in (reply or "").splitlines():
        m = _REPLY_LINE.match(raw)
        if not m:
            continue
        idx = int(m.group(1) or m.group(2))
        if idx in wanted:
            out[idx] = m.group(3)
    return out


def _count_punct(text: str) -> int:
    return sum(1 for ch in text if not ch.isalnum() and not ch.isspace())


def _tally(report: dict, before: str, after: str) -> None:
    if first_alpha(before).islower() and first_alpha(after).isupper():
        report["caps_fixed"] += 1
    if flatten(before) != flatten(after) and re.sub(r"\s+", "", before) == re.sub(r"\s+", "", after):
        report["spacing_fixed"] += 1
    report["punct_added"] += max(0, _count_punct(after) - _count_punct(before))
    if _style_bad(flatten(before)) and not _style_bad(flatten(after)):
        report["style_fixed"] += 1


# --- restoration --------------------------------------------------------------------


def restore_cards(cards: list[dict], ask=None, stem: str | None = None) -> tuple[list[dict], dict]:
    """Restore the deficient cards in place (copy) and return the report.

    Timings and the card count never change. ``ask(prompt, n_words)`` is injectable so
    tests drive a deterministic model; the default is the repair lane.
    """
    ask = _ask if ask is None else ask
    new = [dict(c) for c in cards]
    report = {
        "cards_checked": len(cards),
        "cards_styled_skipped": sum(1 for c in cards if is_styled(c["text"])),
        "cards_deficient": 0,
        "cards_restored": 0,
        "cards_restored_style": 0,
        "cards_restored_lm": 0,
        "cards_finished_caps": 0,
        "cards_finished_terminal": 0,
        "punct_added": 0,
        "caps_fixed": 0,
        "spacing_fixed": 0,
        "style_fixed": 0,
        "rejected_guard": 0,
        "layout_rejected": 0,
        "llm_empty": 0,
        "llm_missing": 0,
        "invariant_violations": 0,
        "words_preserved": True,
    }
    pending: list[int] = []
    rejected_events = 0

    for i, card in enumerate(cards):
        reasons = deficiency(cards, i)
        if not reasons:
            continue
        report["cards_deficient"] += 1
        styled = house_style(card["text"])
        if styled != card["text"] and invariant_ok(card["text"], styled):
            laid = relayout(card["text"], styled)
            if laid is not None:
                new[i]["text"] = laid
                report["cards_restored"] += 1
                report["cards_restored_style"] += 1
                _tally(report, card["text"], laid)
        if deficiency(new, i):
            pending.append(i)

    for start in range(0, len(pending), MAX_BATCH):
        batch = pending[start : start + MAX_BATCH]
        n_words = sum(len(word_tokens(new[i]["text"])) for i in batch)
        reply = ask(build_prompt(new, batch), n_words)
        if not reply:
            report["llm_empty"] += 1
            if stem:
                from dubtitlerr.core import unresolved

                unresolved.record(
                    stem,
                    "restoration",
                    "llm_empty",
                    original_text=flatten(new[batch[0]]["text"])[:300],
                    cards=[batch[0], batch[-1] + 1],
                    words=n_words,
                )
            continue
        parsed = parse_reply(reply, set(batch))
        for i in batch:
            candidate = parsed.get(i)
            if candidate is None:
                report["llm_missing"] += 1
                continue
            candidate = house_style(candidate)
            if not invariant_ok(new[i]["text"], candidate):
                report["rejected_guard"] += 1
                if stem:
                    from dubtitlerr.core import unresolved

                    unresolved.record(
                        stem,
                        "restoration",
                        "rejected_guard",
                        original_text=flatten(new[i]["text"])[:300],
                        proposed_text=flatten(candidate)[:300],
                        cards=[i, i + 1],
                        words=len(word_tokens(new[i]["text"])),
                        **unresolved.dismissal(new[i]["text"], candidate),
                    )
                continue
            laid = relayout(new[i]["text"], candidate)
            if laid is None:
                report["layout_rejected"] += 1
                continue
            if laid != new[i]["text"]:
                _tally(report, new[i]["text"], laid)
                new[i]["text"] = laid
                report["cards_restored"] += 1
                report["cards_restored_lm"] += 1
                if rejected_events < MAX_REJECT_EVENTS:
                    rejected_events += 1  # bounded event budget, shared with the reject path

    # Mechanical finalisation: the two presentation axes the acceptance measures must
    # not depend on the model answering. Punctuation is added only where the sentence
    # ends and the card has no mark at all; the first letter of a sentence start is
    # uppercased. Both are word-preserving by construction and re-checked below.
    for i in range(len(new)):
        if is_sentence_start(new, i):
            capped = capitalize_first(new[i]["text"])
            if capped != new[i]["text"] and invariant_ok(new[i]["text"], capped) and _fits(capped):
                _tally(report, new[i]["text"], capped)
                new[i]["text"] = capped
                report["cards_restored"] += 1
                report["cards_finished_caps"] += 1
        if is_sentence_final(new, i) and not ends_terminal(new[i]["text"]) and not _ends_clause(new[i]["text"]):
            finished = finish_sentence(new[i]["text"])
            if invariant_ok(new[i]["text"], finished) and _fits(finished):
                _tally(report, new[i]["text"], finished)
                new[i]["text"] = finished
                report["cards_restored"] += 1
                report["cards_finished_terminal"] += 1

    # Belt to the per-card guard's braces: if ANY card's word sequence moved, the whole
    # pass is refused and the caller gets the original list. This must be impossible; a
    # failure here is a bug, not a quality trade.
    for i in range(len(cards)):
        if not invariant_ok(cards[i]["text"], new[i]["text"]):
            report["invariant_violations"] += 1
    if report["invariant_violations"]:
        report["words_preserved"] = False
        return [dict(c) for c in cards], report
    joined_before = " ".join(" ".join(word_tokens(c["text"])) for c in cards)
    joined_after = " ".join(" ".join(word_tokens(c["text"])) for c in new)
    if joined_before != joined_after:  # reordering across cards
        report["invariant_violations"] += 1
        report["words_preserved"] = False
        return [dict(c) for c in cards], report
    report["deficient_remaining"] = len(findings(new))
    return new, report


# --- file apply ---------------------------------------------------------------------


def _fingerprint(path: str) -> dict:
    try:
        st = os.stat(path)
    except OSError:
        return {}
    try:
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        digest = ""
    return {"size": st.st_size, "mtime": round(st.st_mtime, 3), "sha256": digest}


def restorable_path(stem: str) -> str | None:
    return presentation.episode_path(stem)


def apply_file(path: str, stem: str | None = None, dry_run: bool = False, ask=None) -> tuple[dict, bool]:
    """Restore one subtitle file; writes only when the result differs and is safe."""
    lines, events, fields = presentation.read_ass(path) if path.endswith(".ass") else presentation.read_srt(path)
    if not events:
        return {"skipped": "no dialogue events"}, False
    cards = [{"start": e["start"], "end": e["end"], "text": e["text"]} for e in events]
    new_cards, report = restore_cards(cards, ask=ask, stem=stem)
    report["cards_before"] = len(cards)
    report["cards_after"] = len(new_cards)
    if not report.get("words_preserved", True):
        report["outcome"] = "invariant-violated"
        report["skipped"] = "word sequence would change"
        return report, False
    changed = sum(1 for a, b in zip(new_cards, cards) if a["text"] != b["text"])
    report["changed_cards"] = changed
    remaining = report.get("deficient_remaining", 0)
    if not changed:
        report["outcome"] = "clean" if not remaining else "llm-unavailable"
        return report, False
    if dry_run:
        report["outcome"] = "would-restore"
        return report, False
    text = presentation._render(path, lines, events, fields, new_cards)
    atomic_write(path, lambda f: f.write(text), mode=MODE_KEEP)
    # Deterministic spacing/style fixes are safe to keep; model work still outstanding
    # means the caller must not stamp this as done.
    report["outcome"] = "partial" if (remaining and report["llm_empty"]) else "restored"
    return report, True


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
    """True when this delivered episode has not been restored under this epoch/file."""
    stamp = read_stamp(stem)
    if stamp.get("epoch") != RESTORATION_EPOCH:
        return True
    path = stamp.get("path")
    if not path:
        return True
    current = _fingerprint(path)
    return bool(current) and current.get("sha256") != stamp.get("sha256")


def _write_stamp(stem: str, path: str, report: dict, outcome: str) -> None:
    doc = {
        "epoch": RESTORATION_EPOCH,
        "at": time.time(),
        "outcome": outcome,
        "path": path,
        "counters": {k: v for k, v in report.items() if isinstance(v, int)},
        **_fingerprint(path),
    }
    atomic_write(_stamp_path(stem), lambda f: json.dump(doc, f, indent=1))


def apply_episode(stem: str, dry_run: bool = False, force: bool = False, ask=None) -> dict:
    """One in-app re-pass for a delivered episode: structure, restore, re-gate, stamp."""
    if not dry_run and os.environ.get("PRESENTATION_REPASS", "1") != "0" and presentation.is_stale(stem):
        # The sentence units are this stage's input: make sure the file has its P29
        # pass before restoring text on top of it (also covers a new delivery that the
        # delivered-file sweep never saw).
        presentation.apply_episode(stem)
    path = restorable_path(stem)
    if not path:
        return {"outcome": "no-artifact"}
    if not force and not dry_run and not is_stale(stem):
        return {"outcome": "no-op", "stamp_current": True, "path": os.path.basename(path)}
    backup = None
    if not dry_run:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                backup = f.read()
        except OSError:
            backup = None
    from dubtitlerr.pipeline import publish_gate

    try:
        duration = publish_gate.video_duration(stem)
    except Exception:  # noqa: BLE001 - a missing duration only drops the tail clamp
        duration = None
    # The verdict this episode arrived with. Restoring the previous bytes is only correct
    # when this pass makes the gate WORSE; a refusal the input already carried belongs to
    # the stage downstream that repairs it, not to a rollback that discards the text work.
    fails_before = publish_gate.evaluate(publish_gate.load_cues(stem)[1], duration) if backup is not None else []
    report, changed = apply_file(path, stem=stem, dry_run=dry_run, ask=ask)
    report["path"] = os.path.basename(path)
    if not changed:
        report.setdefault("outcome", "clean")
        # Do not stamp an episode whose model was unreachable or whose deficiency is
        # still outstanding: retrying it next pass is the point.
        if not dry_run and report["outcome"] not in ("llm-unavailable", "partial"):
            _write_stamp(stem, path, report, report["outcome"])
        return report

    fails = publish_gate.evaluate(publish_gate.load_cues(stem)[1], duration)
    worse = publish_gate.regressions(fails_before, fails)
    if worse:
        if backup is not None:
            atomic_write(path, lambda f: f.write(backup), mode=MODE_KEEP)
        report["outcome"] = "regressed"
        report["fails"] = worse
        report["pre_existing_fails"] = fails_before
        if not dry_run:
            _write_stamp(stem, path, report, "regressed")
        return report
    if fails:
        report["pre_existing_fails"] = fails
    if report.get("outcome") == "partial":
        return report  # written, not stamped: the next pass retries the model work
    report["outcome"] = "restored"
    if not dry_run:
        _write_stamp(stem, path, report, "restored")
    return report


def sweep(roots: list[str], dry_run: bool = False, limit: int | None = None, ask=None) -> dict:
    """Restore every delivered episode under ``roots`` whose stamp is stale."""
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
    out = {"scanned": len(ordered), "stale": 0, "restored": 0, "noop": 0, "skipped": 0, "failed": 0, "results": []}
    unreachable = 0
    for stem in ordered:
        if not is_stale(stem):
            continue
        out["stale"] += 1
        try:
            rep = apply_episode(stem, dry_run=dry_run, ask=ask)
        except Exception as e:  # noqa: BLE001 - one bad episode must not stop the sweep
            out["failed"] += 1
            out["results"].append({"stem": os.path.basename(stem), "outcome": f"error: {e}"})
            continue
        oc = rep.get("outcome")
        if oc == "restored":
            out["restored"] += 1
        elif oc in ("clean", "no-op", "would-restore"):
            out["noop"] += 1
        else:
            out["skipped"] += 1
        if oc == "llm-unavailable":
            unreachable += 1
            if unreachable >= 2:
                out["results"].append({"stem": os.path.basename(stem), "outcome": oc})
                out["aborted"] = "model unreachable: retried next pass"
                break
        else:
            unreachable = 0
        out["results"].append({"stem": os.path.basename(stem), "outcome": oc, "counters": rep})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Restore punctuation, capitalisation, spacing and house style.")
    ap.add_argument("stem", nargs="?", help="episode stem (no extension)")
    ap.add_argument("--stale", action="store_true", help="0 = stale, 1 = current, 3 = unusable")
    ap.add_argument("--sweep", nargs="*", metavar="ROOT", help="restore every stale delivered episode under these roots")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-run even when the epoch stamp is current")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)

    if args.sweep is not None:
        roots = args.sweep or ([args.stem] if args.stem else [])
        if not roots:
            print("restoration: --sweep needs at least one root", file=sys.stderr)
            return 2
        result = sweep(roots, dry_run=args.dry_run, limit=args.limit)
        if args.json:
            print(json.dumps(result, indent=1))
        else:
            extra = f" aborted={result['aborted']}" if result.get("aborted") else ""
            print(
                f"restoration: scanned={result['scanned']} stale={result['stale']} "
                f"restored={result['restored']} noop={result['noop']} skipped={result['skipped']} "
                f"failed={result['failed']}{extra}"
            )
        return 0
    if not args.stem:
        ap.print_help()
        return 2
    if args.stale:
        if not restorable_path(args.stem):
            return 3
        print("stale" if is_stale(args.stem) else "current")
        return 0 if is_stale(args.stem) else 1
    report = apply_episode(args.stem, dry_run=args.dry_run, force=args.force)
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(
            f"restoration: {report.get('outcome')} cards={report.get('cards_before')} "
            f"deficient={report.get('cards_deficient')} restored={report.get('cards_restored')} "
            f"caps={report.get('caps_fixed')} punct={report.get('punct_added')} "
            f"spacing={report.get('spacing_fixed')} style={report.get('style_fixed')} "
            f"rejected={report.get('rejected_guard')}"
        )
    return 0 if report.get("outcome") not in ("invariant-violated", "regressed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
