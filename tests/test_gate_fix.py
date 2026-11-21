"""gate_fix: one deterministic, model-free repair attempt for a gate refusal.

The shape under test is the live one: the publish gate refuses a file that is *almost* a
dubtitle -- one card left running 16 s over silence, one line never wrapped -- and the merge
pass otherwise parks it and forgets it. Every test here asserts the two properties that make
the attempt safe to run automatically: the words never change, and nothing is written unless
the result passes the gate.

The padding cards are load-bearing: the gate also requires 60% of cards to carry sentence
punctuation, so a five-cue fixture cannot absorb a split the way a 400-cue episode can.
"""

import json
import os
import shutil
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.core.atomic_file import atomic_write
from dubtitlerr.pipeline import gate_fix, publish_gate

ASS_HEAD = (
    """[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, """
    """StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,24,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0.5,2,10,10,18,1
Style: Signs,Arial,20,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0.5,8,20,20,20,1
Style: Dubtitles,Arial,24,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0.5,2,10,10,18,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
)

PAD = 14  # punctuated cards around the fault, so a split cannot sink the punctuation ratio


def _t(sec):
    h = int(sec // 3600)
    m = int((sec - h * 3600) // 60)
    s = sec - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_text(cues, style="Dubtitles"):
    lines = [ASS_HEAD]
    for start, end, body in cues:
        lines.append(f"Dialogue: 0,{_t(start)},{_t(end)},{style},,0,0,0,,{body}")
    return "\n".join(lines) + "\n"


def padded(fault_start, fault_end, body):
    """A file made of punctuated padding cards with one faulty card in the middle."""
    cues = [(float(i), float(i) + 1.0, f"Line number {i} is spoken here.") for i in range(PAD)]
    cues.append((fault_start, fault_end, body))
    tail = fault_end + 10.0
    cues += [(tail + i, tail + i + 1.0, f"And line {i} again.") for i in range(PAD)]
    return cues


def write_episode(tmp_path, cues, words=None, stem="ep01"):
    d = tmp_path / "Show"
    d.mkdir(parents=True, exist_ok=True)
    stem_path = str(d / stem)
    (d / f"{stem}.eng.dubtitles.ass").write_text(ass_text(cues))
    if words is not None:
        (d / f"{stem}.dubtitles.words.json").write_text(json.dumps({"words": words}))
    return stem_path


def gate_fails(stem):
    path, cues = publish_gate.load_cues(stem)
    return publish_gate.evaluate(cues, None)


def words_of(stem):
    path, cues = publish_gate.load_cues(stem)
    return [w for _, _, t in cues for w in t.replace("\n", " ").split()]


def read(path):
    return open(path, encoding="utf-8-sig").read()


def cue_index(stem, text):
    _, cues = publish_gate.load_cues(stem)
    return [i for i, (_, _, t) in enumerate(cues) if text in t]


# --- the two fault classes the live refusals showed ---------------------------------

LONG_LINE = "The mitosis process in the left arm of Unit 2 has cleared every requirement we set for it now"


def test_an_over_chars_card_is_split_without_touching_a_word(tmp_path):
    cues = padded(100.0, 107.0, LONG_LINE)
    stem = write_episode(tmp_path, cues)
    assert any("chars" in f for f in gate_fails(stem)), gate_fails(stem)
    before_words = [w for _, _, t in cues for w in t.split()]

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert gate_fails(stem) == []
    assert words_of(stem) == before_words


def test_every_other_line_is_left_byte_identical(tmp_path):
    """Only the faulty card's own Dialogue line may change; every other line is verbatim."""
    cues = padded(100.0, 107.0, LONG_LINE)
    stem = write_episode(tmp_path, cues)
    before = [ln for ln in read(stem + ".eng.dubtitles.ass").splitlines() if ln.startswith("Dialogue:")]
    touched = [ln for ln in before if "mitosis" in ln]
    gate_fix.attempt(stem)
    after = [ln for ln in read(stem + ".eng.dubtitles.ass").splitlines() if ln.startswith("Dialogue:")]
    untouched_before = [ln for ln in before if "mitosis" not in ln]
    # every untouched line survives, in order, byte for byte
    it = iter(after)
    assert all(any(ln == cand for cand in it) for ln in untouched_before)
    assert len(touched) == 1
    assert len(after) > len(before), "the faulty card was split, not just re-timed"


def test_an_over_duration_card_is_ended_where_its_own_speech_ends(tmp_path):
    """A card left running 16 s over silence: the text says it once, in 2.5 s."""
    sentence = "I'm not going to let go of my freedom."
    cues = padded(100.0, 116.94, sentence)
    tokens = sentence.split()
    words = [{"text": w, "start": 100.0 + i * 0.31, "end": 100.31 + i * 0.31, "prob": 0.9} for i, w in enumerate(tokens)]
    stem = write_episode(tmp_path, cues, words=words)
    assert gate_fails(stem) == ["1 card(s) over 7.5s (max 16.9s)"], gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert result["fixes"][0]["action"] == "trim-end"
    assert gate_fails(stem) == []
    _, fixed = publish_gate.load_cues(stem)
    idx = result["fixes"][0]["index"]
    assert fixed[idx][2] == sentence
    assert 102.7 < fixed[idx][1] < 103.5, fixed[idx]
    assert words_of(stem) == [w for _, _, t in cues for w in t.split()]


def test_an_over_duration_card_whose_speech_really_runs_long_is_split(tmp_path):
    """A card whose own words span 14 s is not a linger fault: it becomes several cards."""
    sentence = "I am not going to let go of my freedom and that is the end of it for now and ever"
    tokens = sentence.split()
    per = 14.0 / len(tokens)
    words = [{"text": w, "start": 100.0 + i * per, "end": 100.0 + (i + 1) * per, "prob": 0.9} for i, w in enumerate(tokens)]
    cues = padded(100.0, 114.0, sentence)
    stem = write_episode(tmp_path, cues, words=words)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert result["fixes"][0]["action"] == "split", result["fixes"]
    assert gate_fails(stem) == []
    assert words_of(stem) == [w for _, _, t in cues for w in t.split()]


def test_a_single_unbroken_token_is_reported_unfixable_and_nothing_is_written(tmp_path):
    """Chainsaw Man S01E07's real refusal: one 446-character 'Kekeke...' token."""
    blob = "Kek" * 149
    cues = padded(100.0, 101.32, blob)
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNFIXABLE, result
    assert result["code"] == 3
    assert "single unbroken token" in result["summary"]
    assert read(stem + ".eng.dubtitles.ass") == before
    assert not os.path.exists(stem + ".eng.dubtitles.ass.pre-gate-fix.bak")
    audit = json.load(open(stem + ".dubtitles.gate-fix.json"))
    assert audit["attempts"][-1]["outcome"] == gate_fix.OUTCOME_UNFIXABLE


def test_a_non_layout_refusal_is_not_attempted(tmp_path):
    """Punctuation is a transcript fault; a layout repair may not pretend to fix it."""
    cues = [
        (1.0, 3.0, "the direct cause you mean was the mental breakdown"),
        (4.0, 6.0, "the result of the contact yes"),
        (7.0, 9.0, "that doll as if it were her real daughter"),
    ]
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNSUPPORTED, result
    assert "end with punctuation" in result["summary"]
    assert read(stem + ".eng.dubtitles.ass") == before


def test_a_passing_file_is_a_no_op_and_leaves_no_trace(tmp_path):
    cues = [(float(i), float(i) + 1.0, f"Line number {i} is spoken here.") for i in range(8)]
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")
    mtime = os.path.getmtime(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_NOOP
    assert result["code"] == 0
    assert read(stem + ".eng.dubtitles.ass") == before
    assert os.path.getmtime(stem + ".eng.dubtitles.ass") == mtime
    assert not os.path.exists(stem + ".dubtitles.gate-fix.json")
    assert not os.path.exists(stem + ".dubtitles.stages.json")


def test_re_running_on_the_fixed_file_changes_nothing(tmp_path):
    sentence = "I'm not going to let go of my freedom."
    cues = padded(100.0, 116.94, sentence)
    words = [{"text": w, "start": 100.0 + i * 0.3, "end": 100.3 + i * 0.3, "prob": 0.9} for i, w in enumerate(sentence.split())]
    stem = write_episode(tmp_path, cues, words=words)

    first = gate_fix.attempt(stem)
    fixed_bytes = read(stem + ".eng.dubtitles.ass")
    second = gate_fix.attempt(stem)

    assert first["outcome"] == gate_fix.OUTCOME_FIXED
    assert second["outcome"] == gate_fix.OUTCOME_NOOP
    assert read(stem + ".eng.dubtitles.ass") == fixed_bytes
    audit = json.load(open(stem + ".dubtitles.gate-fix.json"))
    assert len(audit["attempts"]) == 1, "the no-op run must not append an attempt"


def test_the_attempt_is_recorded_as_a_stage_and_in_the_audit_trail(tmp_path):
    sentence = "I'm not going to let go of my freedom."
    stems = padded(100.0, 116.94, sentence)
    stem = write_episode(tmp_path, stems)

    gate_fix.attempt(stem)

    from dubtitlerr.core import common

    assert common.read_stages(stem)["gate_fix"]["outcome"] == "ok"
    audit = json.load(open(stem + ".dubtitles.gate-fix.json"))
    attempt = audit["attempts"][-1]
    assert attempt["fails_before"] == ["1 card(s) over 7.5s (max 16.9s)"]
    assert attempt["fails_after"] == []
    assert attempt["fixes"][0]["action"] == "split"
    assert attempt["fixes"][0]["before"]["dur"] == 16.94
    assert all(a["dur"] <= 7.5 for a in attempt["fixes"][0]["after"])


def test_the_backup_is_the_original_file(tmp_path):
    sentence = "I'm not going to let go of my freedom."
    cues = padded(100.0, 116.94, sentence)
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")

    gate_fix.attempt(stem)

    assert read(stem + ".eng.dubtitles.ass.pre-gate-fix.bak") == before


# --- the same repair on an .srt artifact (the mp4 delivery path) ---------------------


def test_an_over_chars_srt_card_is_split(tmp_path):
    d = tmp_path / "Show"
    d.mkdir(parents=True)
    stem = str(d / "ep01")
    cards = [(float(i), float(i) + 1.0, f"Line number {i} is spoken here.") for i in range(PAD)]
    cards.append((100.0, 107.0, LONG_LINE))
    body = "".join(
        f"{i}\n00:{int(s) // 60:02d}:{int(s) % 60:02d},000 --> 00:{int(e) // 60:02d}:{int(e) % 60:02d},000\n{t}\n\n"
        for i, (s, e, t) in enumerate(cards, 1)
    )
    (d / "ep01.eng.dubtitles.srt").write_text(body)
    assert gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert gate_fails(stem) == []
    assert words_of(stem) == [w for _, _, t in cards for w in t.split()]


# --- repeated-character runs: a scream has no word boundary to wrap at ---------------

SCREAM = "A" * 57  # the uniform scream the P13 fixture used: one token, 7.1 s, 57 chars
LIVE_SCREAM = "A" + "H" * 56  # what FMA:B S01E41 actually ships: 57 chars, {'a', 'h'}
KEKE = "Ke" * 223  # Chainsaw Man S01E07's token: 446 chars, period 2, no uniform run
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "fmab_s01e41_abyss.ass")


def dialogue_lines(stem):
    return [ln for ln in read(stem + ".eng.dubtitles.ass").splitlines() if ln.startswith("Dialogue:")]


def test_a_repeated_character_run_is_wrapped_without_touching_a_character(tmp_path):
    """A 57-char scream is one token with no boundary: insert a line break, change no char."""
    cues = padded(100.0, 107.1, SCREAM)
    stem = write_episode(tmp_path, cues)
    assert "line of 57 chars (max 46)" in gate_fails(stem), gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert result["fixes"][0]["action"] == "wrap-run", result["fixes"]
    assert gate_fails(stem) == []
    # the invariant: strip the break and the card's content is byte-identical
    line = next(ln for ln in dialogue_lines(stem) if "A" * 10 in ln)
    body = line.rsplit(",", 1)[-1]
    assert body.replace("\\N", "") == SCREAM
    # and every rendered line now fits the gate's own budget
    assert len(body.split("\\N")) == 2
    assert all(len(ln) <= publish_gate.MAX_LINE for ln in body.split("\\N"))


def test_the_live_scream_shape_is_wrapped_without_touching_a_character(tmp_path):
    """FMA:B S01E41's real token is 'A' + 56 H's -- one stray, not a uniform run."""
    cues = padded(100.0, 107.14, LIVE_SCREAM)
    stem = write_episode(tmp_path, cues)
    assert "line of 57 chars (max 46)" in gate_fails(stem), gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert result["fixes"][0]["action"] == "wrap-run", result["fixes"]
    assert gate_fails(stem) == []
    line = next(ln for ln in dialogue_lines(stem) if "AHHH" in ln)
    body = line.rsplit(",", 1)[-1]
    assert body.replace("\\N", "") == LIVE_SCREAM
    first, second = body.split("\\N")
    assert abs(len(first) - len(second)) <= 1, (first, second)
    assert all(len(ln) <= publish_gate.MAX_LINE for ln in body.split("\\N"))


def test_the_live_fmab_s01e41_artifact_is_fixed_in_place(tmp_path):
    """The shipped E41 sidecar, byte for byte: only the scream's Dialogue line changes."""
    d = tmp_path / "Show"
    d.mkdir(parents=True)
    stem = str(d / "ep41")
    shutil.copyfile(FIXTURE, stem + ".eng.dubtitles.ass")
    before = read(stem + ".eng.dubtitles.ass")
    assert before.count(LIVE_SCREAM) == 1
    assert "line of 57 chars (max 46)" in gate_fails(stem), gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert result["fixes"][0]["action"] == "wrap-run", result["fixes"]
    assert gate_fails(stem) == []
    after = read(stem + ".eng.dubtitles.ass")
    body = next(ln for ln in after.splitlines() if "AHHH" in ln).rsplit(",", 1)[-1]
    assert body.replace("\\N", "") == LIVE_SCREAM
    assert all(len(ln) <= publish_gate.MAX_LINE for ln in body.split("\\N"))
    # every other line, including the sign/song events' positions, is untouched
    old, new = before.splitlines(), after.splitlines()
    assert len(old) == len(new)
    changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
    assert len(changed) == 1, changed


def test_a_period_two_run_is_still_refused_unchanged(tmp_path):
    """'Kekekekeke...' has two characters but no tiny minority: it stays unfixable."""
    cues = padded(100.0, 101.32, KEKE)
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNFIXABLE, result
    assert "single unbroken token" in result["summary"], result["summary"]
    assert read(stem + ".eng.dubtitles.ass") == before
    assert not os.path.exists(stem + ".eng.dubtitles.ass.pre-gate-fix.bak")


def test_the_run_is_split_evenly_and_re_running_is_a_no_op(tmp_path):
    cues = padded(100.0, 107.1, SCREAM)
    stem = write_episode(tmp_path, cues)

    gate_fix.attempt(stem)
    line = next(ln for ln in dialogue_lines(stem) if "A" * 10 in ln)
    first, second = line.rsplit(",", 1)[-1].split("\\N")
    assert abs(len(first) - len(second)) <= 1, (first, second)
    fixed_bytes = read(stem + ".eng.dubtitles.ass")

    second_run = gate_fix.attempt(stem)

    assert second_run["outcome"] == gate_fix.OUTCOME_NOOP
    assert read(stem + ".eng.dubtitles.ass") == fixed_bytes


def test_a_wrapped_run_keeps_the_card_timing(tmp_path):
    """A layout break moves no speech: start and end are the card's originals."""
    cues = padded(100.0, 107.1, SCREAM)
    stem = write_episode(tmp_path, cues)
    _, before = publish_gate.load_cues(stem)
    idx = next(i for i, (_, _, t) in enumerate(before) if t == SCREAM)

    gate_fix.attempt(stem)

    _, after = publish_gate.load_cues(stem)
    assert after[idx][0] == before[idx][0]
    assert after[idx][1] == before[idx][1]
    assert after[idx][2].replace("\n", "") == SCREAM


def test_a_normal_multi_word_line_still_takes_the_word_boundary_path(tmp_path):
    """The run wrap must never reach ordinary prose: words stay whole."""
    cues = padded(100.0, 107.0, LONG_LINE)
    stem = write_episode(tmp_path, cues)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert result["fixes"][0]["action"] == "split", result["fixes"]
    _, after = publish_gate.load_cues(stem)
    for _, _, t in after:
        for ln in t.split("\n"):
            assert ln == ln.strip()
    # every word of the original still appears, in order, unbroken
    assert words_of(stem) == [w for _, _, t in cues for w in t.split()]


def test_a_run_longer_than_the_card_budget_still_refuses(tmp_path):
    """446 chars: wrap-able, but the TOTAL is over MAX_CHARS, so the card stays refused."""
    blob = "A" * 446
    cues = padded(100.0, 101.32, blob)
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNFIXABLE, result
    assert "446 identical chars" in result["summary"], result["summary"]
    assert "over the gate's 88-char budget" in result["summary"], result["summary"]
    assert result["code"] == 1 or result["code"] == 3
    assert read(stem + ".eng.dubtitles.ass") == before
    assert not os.path.exists(stem + ".eng.dubtitles.ass.pre-gate-fix.bak")


def test_a_run_at_the_char_boundary_still_refuses(tmp_path):
    """88 chars fits the card budget alone, but the break makes it 89: fail closed."""
    blob = "A" * 88
    cues = padded(100.0, 101.32, blob)
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNFIXABLE, result
    assert "89 with the break" in result["summary"], result["summary"]
    assert read(stem + ".eng.dubtitles.ass") == before


def test_a_run_that_also_runs_too_long_says_so_rather_than_blaming_the_break(tmp_path):
    """57 A's over 9 s: the break fits, the duration does not — and it must say that."""
    cues = padded(100.0, 109.0, SCREAM)
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNFIXABLE, result
    assert "7.5s budget" in result["summary"], result["summary"]
    assert read(stem + ".eng.dubtitles.ass") == before


def test_an_srt_scream_is_wrapped_with_a_real_line_break(tmp_path):
    """The mp4 delivery path: same repair, the break is an SRT newline inside the block."""
    d = tmp_path / "Show"
    d.mkdir(parents=True)
    stem = str(d / "ep01")
    cards = [(float(i), float(i) + 1.0, f"Line number {i} is spoken here.") for i in range(PAD)]
    cards.append((100.0, 107.1, SCREAM))
    body = "".join(
        f"{i}\n00:{int(s) // 60:02d}:{int(s) % 60:02d},000 --> 00:{int(e) // 60:02d}:{int(e) % 60:02d},000\n{t}\n\n"
        for i, (s, e, t) in enumerate(cards, 1)
    )
    (d / "ep01.eng.dubtitles.srt").write_text(body)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert gate_fails(stem) == []
    text = read(stem + ".eng.dubtitles.srt")
    block = next(b for b in text.split("\n\n") if "AAAA" in b)
    wrapped = block.split("\n", 2)[2]
    assert wrapped.replace("\n", "") == SCREAM
    assert all(len(ln) <= publish_gate.MAX_LINE for ln in wrapped.split("\n"))


def test_wrap_run_declines_everything_but_one_run_with_a_tiny_minority():
    assert gate_fix._wrap_run("A" * 57) == "A" * 29 + "\n" + "A" * 28
    assert gate_fix._wrap_run("a" * 57) == "a" * 29 + "\n" + "a" * 28
    assert gate_fix._wrap_run(LIVE_SCREAM) == LIVE_SCREAM[:29] + "\n" + LIVE_SCREAM[29:]
    assert gate_fix._wrap_run("A" * 46) is None, "a line that already fits is left alone"
    assert gate_fix._wrap_run("AHHH no") is None, "more than one token"
    assert gate_fix._wrap_run(SCREAM + "\n" + SCREAM) is None, "already two lines"


def test_wrap_run_minority_boundary_is_two_stray_characters():
    """Two distinct letters are fine when the stray one appears at most twice."""
    two_strays = "A" + "H" * 55 + "A"  # 57 chars, 'a' x2
    three_strays = "A" + "H" * 53 + "AAA"  # 57 chars, 'a' x3
    assert len(two_strays) == len(three_strays) == 57
    assert gate_fix._wrap_run(two_strays) is not None
    assert gate_fix._wrap_run(three_strays) is None
    assert gate_fix._wrap_run(KEKE) is None, "period-2 run: the minority is large"
    assert gate_fix._wrap_run("Kek" * 30) is None, "three distinct characters"


def test_why_unsplittable_stays_accurate_for_a_near_run(tmp_path):
    """A mixed run must not be called 'identical chars', and the over-budget arm must say so."""
    near_blob = "A" + "H" * 446  # 447 chars: wrap-able, but over the 88-char card budget
    cues = padded(100.0, 101.32, near_blob)
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNFIXABLE, result
    assert "447" in result["summary"], result["summary"]
    assert "identical chars" not in result["summary"], result["summary"]
    assert "over the gate's 88-char budget" in result["summary"], result["summary"]
    assert read(stem + ".eng.dubtitles.ass") == before


def test_why_unsplittable_blames_duration_when_the_break_would_fit(tmp_path):
    """57 chars over 9 s: the near-run wraps, the duration does not — say that, not 'identical'."""
    cues = padded(100.0, 109.0, LIVE_SCREAM)
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNFIXABLE, result
    assert "57" in result["summary"], result["summary"]
    assert "7.5s budget" in result["summary"], result["summary"]
    assert "identical chars" not in result["summary"], result["summary"]
    assert read(stem + ".eng.dubtitles.ass") == before


# --- fault classification -----------------------------------------------------------


def test_split_fails_separates_layout_from_everything_else():
    layout, other = gate_fix.split_fails(
        [
            "1 card(s) over 7.5s (max 16.9s)",
            "line of 446 chars (max 46)",
            "2 card(s) over 88 chars",
            "coverage 41% (last cue 600.0s of 1450.0s)",
            "12% of cards end with punctuation",
            "known mishear(s): oscar",
        ]
    )
    assert len(layout) == 3
    assert len(other) == 3


def test_cue_faults_is_the_gates_own_budget():
    assert gate_fix.cue_faults("short line", 3.0) == []
    assert gate_fix.cue_faults("x" * 89, 3.0) == ["89 chars", "89-char line"]
    assert gate_fix.cue_faults("x", 8.0) == ["8.00s card"]
    assert gate_fix.cue_faults("Unseen\n" + "y" * 50, 3.0), "second line too long"


# --- music-symbol cards: a marker is not dialogue -------------------------------------

MUSIC = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "artifacts", "blue_eye_s01e02_music.srt"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "artifacts", "blue_eye_s01e04_music.srt"),
)


def _copy_episode(tmp_path, fixture, stem="ep01"):
    d = tmp_path / "Show"
    d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture, str(d / f"{stem}.eng.dubtitles.srt"))
    return str(d / stem)


def test_is_music_marker_only_accepts_text_with_no_word_character():
    assert gate_fix.is_music_marker("♪")
    assert gate_fix.is_music_marker("♪♪♪")
    assert gate_fix.is_music_marker("♪ ♪")
    assert gate_fix.is_music_marker("...")
    assert not gate_fix.is_music_marker("42"), "a number is content"
    assert not gate_fix.is_music_marker("♪ oh ♪"), "a word makes it dialogue"
    assert not gate_fix.is_music_marker("")


def test_the_live_blue_eye_files_pass_once_their_music_cards_are_dropped(tmp_path):
    """The real 2026-11-20 refusals: E02's 29.9 s '♪' + 37.0 s '♪♪♪', E04's 37.0 s '♪♪♪'.

    Real bytes from the library: the gate refuses each on the over-duration finding, and
    the drop must remove exactly those cards and nothing else.
    """
    expected = ((MUSIC[0], 2), (MUSIC[1], 1))
    for i, (fixture, drops) in enumerate(expected):
        stem = _copy_episode(tmp_path / f"case{i}", fixture)
        _, before = publish_gate.load_cues(stem)
        assert gate_fails(stem) and "card(s) over 7.5s" in gate_fails(stem)[0], gate_fails(stem)

        result = gate_fix.attempt(stem)

        assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
        assert gate_fails(stem) == [], gate_fails(stem)
        _, after = publish_gate.load_cues(stem)
        assert len(after) == len(before) - drops
        gone = [c for c in before if c not in after]
        assert len(gone) == drops
        assert all(not any(ch.isalnum() for ch in t) and (e - s) > 7.5 for s, e, t in gone)
        assert [a["action"] for a in result["fixes"]] == ["drop-music"] * drops
        audit = json.load(open(stem + ".dubtitles.gate-fix.json"))
        assert audit["music_cues_dropped"] == drops, audit
        assert "dropped" in audit["attempts"][-1]["summary"]


def test_a_music_card_inside_the_duration_budget_is_left_alone(tmp_path):
    """A short '♪♪' is harmless and ships; only an over-duration marker is dropped."""
    cues = [(float(i), float(i) + 1.0, f"Line number {i} is spoken here.") for i in range(PAD)]
    cues.append((50.0, 52.0, "♪♪"))
    cues += [(60.0 + i, 61.0 + i, f"And line {i} again.") for i in range(PAD)]
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")
    assert gate_fails(stem) == [], gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_NOOP, result
    assert read(stem + ".eng.dubtitles.ass") == before


def test_an_over_duration_music_marker_is_dropped_through_the_song_policy(tmp_path):
    """Two markers over the screen budget, in a file with nothing else wrong."""
    cues = [(float(i), float(i) + 1.0, f"Line number {i} is spoken here.") for i in range(PAD)]
    cues.append((100.0, 129.94, "♪"))
    cues.append((130.0, 167.0, "♪♪♪"))
    cues += [(180.0 + i, 181.0 + i, f"And line {i} again.") for i in range(PAD)]
    stem = write_episode(tmp_path, cues)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert [a["action"] for a in result["fixes"]] == ["drop-music", "drop-music"]
    assert gate_fails(stem) == []
    bodies = [t for _, _, t in publish_gate.load_cues(stem)[1]]
    assert "♪" not in bodies and "♪♪♪" not in bodies
    assert "Line number 0 is spoken here." in bodies


def test_a_music_drop_can_lift_a_file_over_the_punctuation_floor(tmp_path):
    """Blue Eye S01E06's shape: just under the floor, with two over-duration '♪♪' cards.

    The punctuation finding is not a layout fault and is normally not attempted -- but when
    the file also carries music markers, dropping them removes non-dialogue cards and the
    honest ratio is recomputed. 50/102 is 49%, 50/100 after the drop is 50% and clears the
    floor; the in-memory re-gate is what decides, so a file that still fails writes nothing.
    """
    cues = [(float(i), float(i) + 1.0, "Ends here.") for i in range(50)]
    cues += [(100.0 + i, 101.0 + i, "No terminal here") for i in range(50)]
    cues.append((200.0, 237.0, "♪♪"))
    cues.append((240.0, 277.0, "♪♪"))
    assert len(cues) == 102
    stem = write_episode(tmp_path, cues)
    fails = gate_fails(stem)
    assert any("card(s) over 7.5s" in f for f in fails), fails
    assert any("end with punctuation" in f for f in fails), fails

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert [a["action"] for a in result["fixes"]] == ["drop-music", "drop-music"]
    assert gate_fails(stem) == []


def test_a_music_drop_that_leaves_an_unfixable_card_writes_nothing(tmp_path):
    """Drop candidates do not license a partial fix: one unfixable card scopes the pass out."""
    cues = [(float(i), float(i) + 1.0, "Ends here.") for i in range(60)]
    cues += [(100.0, 137.0, "♪♪")]
    cues += [(150.0, 151.32, "Kek" * 149)]
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNFIXABLE, result
    assert read(stem + ".eng.dubtitles.ass") == before


def test_an_ass_music_marker_is_deleted_from_its_dialogue_line(tmp_path):
    """The mkv path: the marker's Dialogue line is removed and every other line is intact."""
    cues = [(float(i), float(i) + 1.0, f"Line number {i} is spoken here.") for i in range(PAD)]
    cues.append((100.0, 137.0, "♪♪♪"))
    cues += [(150.0 + i, 151.0 + i, f"And line {i} again.") for i in range(PAD)]
    stem = write_episode(tmp_path, cues)
    before = dialogue_lines(stem)
    assert len(before) == len(cues)
    assert gate_fails(stem) == ["1 card(s) over 7.5s (max 37.0s)"], gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    after = dialogue_lines(stem)
    assert len(after) == len(before) - 1
    assert not any("♪" in ln for ln in after)
    assert gate_fails(stem) == []


# --- a give-up is parked, not retried every pass --------------------------------------


def test_a_give_up_is_parked_with_the_code_that_produced_it(tmp_path):
    blob = "Kek" * 149
    stem = write_episode(tmp_path, padded(100.0, 101.32, blob))

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNFIXABLE
    doc = gate_fix.read_park(stem)
    assert doc["parked"] is True, doc
    assert doc["outcome"] == gate_fix.OUTCOME_UNFIXABLE
    assert doc["fails"], "the parked record keeps the refusal it stands for"
    assert doc["attempt_stamp"] == gate_fix.attempt_stamp(stem)
    assert doc["attempt_stamp"]["gate_fix_epoch"] == gate_fix.GATE_FIX_EPOCH
    assert gate_fix.parked_state(stem)[0] is True


def test_a_park_stops_standing_when_the_gate_fix_epoch_moves(tmp_path, monkeypatch):
    """A deployed gate_fix change retries parked episodes exactly once."""
    stem = write_episode(tmp_path, padded(100.0, 101.32, "Kek" * 149))
    gate_fix.attempt(stem)
    assert gate_fix.parked_state(stem)[0] is True

    monkeypatch.setattr(gate_fix, "GATE_FIX_EPOCH", gate_fix.GATE_FIX_EPOCH + 1)

    parked, note = gate_fix.parked_state(stem)
    assert parked is False
    assert "attempt stamp" in note


def test_a_park_stops_standing_only_for_a_request_created_after_it(tmp_path, monkeypatch):
    stem = write_episode(tmp_path, padded(100.0, 101.32, "Kek" * 149))
    gate_fix.attempt(stem)
    rdir = tmp_path / "requests"
    rdir.mkdir()
    monkeypatch.setenv("REQUESTS_DIR", str(rdir))
    now = time.time()

    old = rdir / "old.json"
    old.write_text(json.dumps({"show": "Show"}))
    os.utime(old, (now - 3600, now - 3600))
    assert gate_fix.parked_state(stem, show="Show")[0] is True, "an older request changes nothing"

    new = rdir / "new.json"
    new.write_text(json.dumps({"show": "Show"}))
    os.utime(new, (now + 120, now + 120))
    parked, note = gate_fix.parked_state(stem, show="Show")
    assert parked is False, "an explicit run request retries it"
    assert "run request" in note

    assert gate_fix.parked_state(stem, show="Another Show")[0] is True


def test_an_unusable_park_record_is_an_error_never_a_stale_verdict(tmp_path):
    """A hand-edited or half-written park file used to raise ValueError, which merge_pass read as
    "the verdict no longer stands": it unparked the episode and dropped the record. A record it
    cannot trust must stay parked and be reported (exit 3), not silently deleted."""
    stem = write_episode(tmp_path, padded(100.0, 101.32, "Kek" * 149))
    park_path = gate_fix._park_path(stem)
    atomic_write(
        park_path,
        lambda f: f.write(
            json.dumps(
                {
                    "parked": True,
                    "at": "yesterday",
                    "outcome": "unfixable",
                    "summary": "old",
                    "attempt_stamp": gate_fix.attempt_stamp(),
                }
            )
        ),
    )

    with pytest.raises(gate_fix.ParkRecordError):
        gate_fix.parked_state(stem)
    assert gate_fix.main([stem, "--parked"]) == 3
    assert os.path.exists(park_path), "the park is kept for a human, not dropped"


def test_a_park_record_without_a_usable_attempt_stamp_is_an_error_too(tmp_path):
    stem = write_episode(tmp_path, padded(100.0, 101.32, "Kek" * 149))
    park_path = gate_fix._park_path(stem)
    atomic_write(park_path, lambda f: f.write(json.dumps({"parked": True, "at": 1.0, "outcome": "unfixable", "summary": "old"})))

    with pytest.raises(gate_fix.ParkRecordError):
        gate_fix.parked_state(stem)
    assert gate_fix.main([stem, "--parked"]) == 3


def test_the_parked_cli_keeps_its_two_happy_exit_codes(tmp_path):
    stem = write_episode(tmp_path, padded(100.0, 101.32, "Kek" * 149))
    assert gate_fix.main([stem, "--parked"]) == 1, "not parked: merge_pass takes the normal path"
    gate_fix.attempt(stem)
    assert gate_fix.main([stem, "--parked"]) == 0, "parked and the verdict stands: skip the stem"


def test_a_fixed_episode_drops_its_parked_record(tmp_path):
    sentence = "I'm not going to let go of my freedom."
    cues = padded(100.0, 116.94, sentence)
    words = [
        {"text": w, "start": 100.0 + i * 0.31, "end": 100.31 + i * 0.31, "prob": 0.9} for i, w in enumerate(sentence.split())
    ]
    stem = write_episode(tmp_path, cues, words=words)
    park_path = gate_fix._park_path(stem)
    atomic_write(
        park_path,
        lambda f: f.write(
            json.dumps(
                {"parked": True, "at": 1.0, "outcome": "unfixable", "summary": "old", "attempt_stamp": gate_fix.attempt_stamp()}
            )
        ),
    )

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert not os.path.exists(park_path), "a healed episode must not stay parked"


# --- the correction lane (P24): the glossary heals what it flagged --------------------


def write_glossary(tmp_path, monkeypatch, hard_fixes=None, names=None, show="Show"):
    """A fixture glossary where ``glossary_for`` will find it, plus its path."""
    gdir = tmp_path / "glossaries"
    gdir.mkdir(exist_ok=True)
    path = gdir / f"{show}.json"
    path.write_text(json.dumps({"show": show, "names": names or [], "hard_fixes": hard_fixes or {}}))
    monkeypatch.setattr(publish_gate, "GLOSSARY_DIR", str(gdir))
    return path


def mishear_fails(stem):
    """The gate's findings as gate_fix sees them: its own glossary's mishear list included."""
    path, cues = publish_gate.load_cues(stem)
    mishears = publish_gate.glossary_mishears(publish_gate.glossary_for(stem))
    return [f for f in publish_gate.evaluate(cues, None, mishears) if f.startswith("known mishear")]


def test_a_parked_mishear_heals_by_reapplying_the_glossary(tmp_path, monkeypatch):
    """Frieren S01E17: `Zine's very generous notes` -> `Sein's ...` (real case, 2026-11-21).

    The product was assembled before the hard_fix existed, so the gate's own mishear list
    flagged a word the same glossary already knows how to correct. The lane re-applies it
    and the file passes -- without a model, a re-transcribe or an operator script.
    """
    stem = write_episode(tmp_path, padded(100.0, 104.0, "Zine's very generous notes."))
    write_glossary(tmp_path, monkeypatch, hard_fixes={"zine": "Sein"}, names=["Frieren", "Fern", "Sein"])
    assert mishear_fails(stem) == ["known mishear(s): zine"], mishear_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_CORRECTED, result
    assert gate_fails(stem) == [], gate_fails(stem)
    bodies = [t for _, _, t in publish_gate.load_cues(stem)[1]]
    assert "Sein's very generous notes." in bodies
    assert not any("Zine" in t for t in bodies)
    assert result["corrections"][0]["before"] == "Zine's very generous notes."
    assert result["corrections"][0]["after"] == "Sein's very generous notes."
    audit = json.load(open(stem + ".dubtitles.gate-fix.json"))
    assert audit["attempts"][-1]["corrections"] == result["corrections"]


def test_a_linebreak_glued_mishear_is_corrected_mid_cue(tmp_path, monkeypatch):
    """Frieren S01E17: `and\\NFreeran's` -- the mishear sits after a break, mid-cue.

    Correcting a cue is done one physical line at a time, so the break stays where the
    layout put it and only the token changes.
    """
    stem = write_episode(tmp_path, padded(100.0, 105.0, "and\\NFreeran's patience ran out."))
    write_glossary(tmp_path, monkeypatch, hard_fixes={"freeran": "Fern"}, names=["Frieren", "Fern"])
    assert mishear_fails(stem), mishear_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_CORRECTED, result
    bodies = [t for _, _, t in publish_gate.load_cues(stem)[1]]
    assert "and\nFern's patience ran out." in bodies
    assert "Freeran's" not in " ".join(bodies)


def test_a_possessive_mishear_heals_through_the_curated_fix(tmp_path, monkeypatch):
    """Frieren S01E26: `Zinza's replica` -> `Sense's replica`."""
    stem = write_episode(tmp_path, padded(100.0, 103.0, "Zinza's replica is perfect."))
    write_glossary(tmp_path, monkeypatch, hard_fixes={"zinza": "Sense"}, names=["Frieren", "Sense"])

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_CORRECTED, result
    bodies = [t for _, _, t in publish_gate.load_cues(stem)[1]]
    assert "Sense's replica is perfect." in bodies


def test_the_correction_lane_never_moves_timing_or_structure(tmp_path, monkeypatch):
    """Words may change; the card count, spans and break count must not."""
    cues = padded(100.0, 104.0, "Zine's very generous notes.")
    cues[3] = (cues[3][0], cues[3][1], "and\\NFreeran's patience ran out.")
    stem = write_episode(tmp_path, cues)
    write_glossary(tmp_path, monkeypatch, hard_fixes={"zine": "Sein", "freeran": "Fern"}, names=["Frieren", "Fern", "Sein"])
    _, before = publish_gate.load_cues(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_CORRECTED, result
    _, after = publish_gate.load_cues(stem)
    assert [(s, e) for s, e, _ in after] == [(s, e) for s, e, _ in before]
    assert [t.count("\n") for _, _, t in after] == [t.count("\n") for _, _, t in before]


def test_a_dry_run_reports_the_correction_and_writes_nothing(tmp_path, monkeypatch):
    stem = write_episode(tmp_path, padded(100.0, 104.0, "Zine's very generous notes."))
    write_glossary(tmp_path, monkeypatch, hard_fixes={"zine": "Sein"}, names=["Sein"])
    before = read(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem, dry_run=True)

    assert result["outcome"] == gate_fix.OUTCOME_CORRECTED, result
    assert result["code"] == 0
    assert read(stem + ".eng.dubtitles.ass") == before
    assert not os.path.exists(stem + gate_fix.BACKUP_SUFFIX)


def test_an_uncorrectable_refusal_still_parks(tmp_path, monkeypatch):
    """`Furikaereba itsumo.` -- a JP lyric and an unpunctuated block the glossary cannot fix.

    Nothing changed, so the give-up is still a give-up: the product is untouched and the
    park record stands.
    """
    cues = [(float(i), float(i) + 1.0, "Ends here.") for i in range(30)]
    cues += [(100.0 + i, 101.0 + i, "Linya, linya") for i in range(40)]
    cues += [(200.0, 203.0, "Furikaereba itsumo.")]
    stem = write_episode(tmp_path, cues)
    write_glossary(tmp_path, monkeypatch, hard_fixes={}, names=["Linea", "Frieren"])
    before = read(stem + ".eng.dubtitles.ass")
    assert any("end with punctuation" in f for f in gate_fails(stem)), gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNSUPPORTED, result
    assert read(stem + ".eng.dubtitles.ass") == before
    assert gate_fix.parked_state(stem)[0] is True


def test_a_partial_correction_is_never_written(tmp_path, monkeypatch):
    """A healed mishear plus a still-unfixable finding writes nothing at all."""
    cues = [(float(i), float(i) + 1.0, "Ends here.") for i in range(30)]
    cues += [(100.0 + i, 101.0 + i, "No terminal here") for i in range(40)]
    cues.append((200.0, 204.0, "Zine's very generous notes."))
    stem = write_episode(tmp_path, cues)
    write_glossary(tmp_path, monkeypatch, hard_fixes={"zine": "Sein"}, names=["Sein"])
    before = read(stem + ".eng.dubtitles.ass")
    assert any("end with punctuation" in f for f in gate_fails(stem)), gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNSUPPORTED, result
    assert "corrections" in result, "the lane ran; it just did not heal the file"
    assert read(stem + ".eng.dubtitles.ass") == before
    assert gate_fix.parked_state(stem)[0] is True


def test_a_glossary_change_unparks_a_parked_episode(tmp_path, monkeypatch):
    """The stamp carries the glossary fingerprint: editing the glossary retries the park.

    That edit is exactly what heals a `known mishear(s)` refusal, so a park that outlived
    its own fix would have to wait for a human (or a run request) -- the operator step P24
    removes.
    """
    stem = write_episode(tmp_path, padded(100.0, 104.0, "Zine's very generous notes."))
    gloss_path = write_glossary(tmp_path, monkeypatch, hard_fixes={"zinza": "Sense"}, names=["Sein"])
    # Stand in for the record an older pass wrote: a real give-up, stamped against THIS
    # glossary. (No plain-text gate finding can park this file until the fix lands, so the
    # record is written the way _write_park writes it.)
    atomic_write(
        gate_fix._park_path(stem),
        lambda f: f.write(
            json.dumps(
                {
                    "parked": True,
                    "at": time.time(),
                    "outcome": gate_fix.OUTCOME_UNSUPPORTED,
                    "summary": "known mishear(s): zine",
                    "fails": ["known mishear(s): zine"],
                    "attempt_stamp": gate_fix.attempt_stamp(stem),
                }
            )
        ),
    )
    assert gate_fix.parked_state(stem)[0] is True

    gloss_path.write_text(json.dumps({"show": "Show", "names": ["Sein"], "hard_fixes": {"zinza": "Sense", "zine": "Sein"}}))

    parked, note = gate_fix.parked_state(stem)
    assert parked is False, "a glossary edit is the fix; the park must not outlive it"
    assert "attempt stamp" in note

    result = gate_fix.attempt(stem)
    assert result["outcome"] == gate_fix.OUTCOME_CORRECTED, result
    assert gate_fix.parked_state(stem)[0] is False


# --- the three 2026-11-21 parks, on the library's own bytes ---------------------------
#
# Each of these files was parked on a real episode and could not be resolved by code:
# 86 Eighty-Six S01E01 (punctuation + a 58-character scream), Cyberpunk S02E01 (a 22.1 s
# two-word card with no boundary to split at), Frieren S02E01 (romaji ending-song lyrics
# reported as a known mishear). They are the exact artifacts, trimmed only where a file is
# a byproduct (the Cyberpunk word list is its 340-460 s window, verbatim).

LIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "artifacts")


def _live_episode(tmp_path, fixture, words=None):
    d = tmp_path / "Show"
    d.mkdir(parents=True, exist_ok=True)
    ext = ".ass" if fixture.endswith(".ass") else ".srt"
    dest = str(d / f"ep01.eng.dubtitles{ext}")
    shutil.copyfile(os.path.join(LIVE, fixture), dest)
    if words:
        shutil.copyfile(os.path.join(LIVE, words), str(d / "ep01.dubtitles.words.json"))
    return str(d / "ep01")


def test_the_live_86_eighty_six_scream_is_wrapped_and_the_episode_ships(tmp_path):
    """The park: `line of 58 chars` plus a punctuation refusal. The punctuation measure was
    wrong (it counted only terminal marks on a file cut at commas); the line is a
    repeated-character run the gate_fix wrap exists for. Both now resolve without a model.
    """
    stem = _live_episode(tmp_path, "e86_s01e01_dubtitles.ass")
    assert gate_fails(stem) == ["line of 58 chars (max 46)"], gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert [a["action"] for a in result["fixes"]] == ["wrap-run"]
    assert gate_fails(stem) == []
    _, after = publish_gate.load_cues(stem)
    scream = [t for _, _, t in after if t.startswith("Noooo")]
    assert len(scream) == 1
    assert "\n" in scream[0], "the break is a line break, not a split card"
    assert scream[0].replace("\n", "") == "No" + "o" * 56, scream[0]
    assert max(len(ln) for ln in scream[0].split("\n")) <= publish_gate.MAX_LINE


def test_the_live_cyberpunk_over_duration_card_is_ended_at_its_own_speech(tmp_path):
    """Card #101, "I need you." displayed 22.13 s: the word stream says it four times.

    There is no boundary to split at (two words) and a 22 s card is not shippable, so the
    documented trade-off applies -- the card ends where its own first utterance ends. Text
    unchanged; the later repetitions the card never carried stay unsubbed rather than
    duplicating a word.
    """
    stem = _live_episode(tmp_path, "cyberpunk_s02e01_dubtitles.srt", words="cyberpunk_s02e01_words.json")
    before_words = words_of(stem)
    assert gate_fails(stem) == ["1 card(s) over 7.5s (max 22.1s)"], gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert [a["action"] for a in result["fixes"]] == ["trim-end"]
    fix = result["fixes"][0]
    assert (round(fix["before"]["start"], 2), round(fix["before"]["dur"], 2)) == (395.7, 22.13)
    assert round(fix["after"][0]["end"], 2) == 398.3, fix
    assert gate_fails(stem) == []
    assert words_of(stem) == before_words, "a trim may not change a word"


def test_the_live_frieren_song_episode_is_a_no_op(tmp_path):
    """The only refusal was the ending-song romaji read as a known mishear; the file ships."""
    stem = _live_episode(tmp_path, "frieren_s02e01_song.dubtitles.ass")
    path = stem + ".eng.dubtitles.ass"
    before = read(path)
    assert gate_fails(stem) == [], gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_NOOP, result
    assert read(path) == before


def test_a_song_span_card_is_left_alone_even_when_it_breaks_a_budget(tmp_path):
    """A romaji lyric over 7.5 s is not a layout fault to fix: the song owns that span."""
    cues = [(float(i), float(i) + 1.0, f"Line number {i} is spoken here.") for i in range(PAD)]
    cues.append((100.0, 120.0, "Osoresu ni susume yo."))
    cues.append((120.5, 140.5, "Furikaereba itsumo."))
    cues += [(150.0 + i, 151.0 + i, f"And line {i} again.") for i in range(PAD)]
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")
    assert gate_fails(stem) == [], gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_NOOP, result
    assert read(stem + ".eng.dubtitles.ass") == before


def test_a_lone_foreign_card_is_dialogue_and_still_has_to_fit(tmp_path):
    """One romaji-looking card is not a span, so the gate still judges its layout."""
    cues = [(float(i), float(i) + 1.0, f"Line number {i} is spoken here.") for i in range(PAD)]
    cues.append((100.0, 120.0, "Osoresu ni susume yo."))
    cues += [(150.0 + i, 151.0 + i, f"And line {i} again.") for i in range(PAD)]
    stem = write_episode(tmp_path, cues)
    assert any("over 7.5s" in f for f in gate_fails(stem)), gate_fails(stem)


def test_the_correction_lane_never_rewrites_a_lyric(tmp_path, monkeypatch):
    """The deployed lane did exactly that: `Furikaereba itsumo.` -> `Frieren itsumo.`

    A glossary rule is admitted from dialogue, and a lyric can match one by accident
    (Frieren S02E01's glossary carries `Furikaereba -> Frieren`). The lane must correct
    the dialogue card that needs it and leave the song span's bytes exactly as they are.
    """
    cues = [(float(i), float(i) + 1.0, f"Line number {i} is spoken here.") for i in range(PAD)]
    cues += [
        (100.0, 103.0, "Zine's very generous notes."),
        (103.5, 106.5, "Furikaereba itsumo."),
        (107.0, 110.0, "Osoresu ni susume yo."),
    ]
    cues += [(150.0 + i, 151.0 + i, f"And line {i} again.") for i in range(PAD)]
    stem = write_episode(tmp_path, cues)
    write_glossary(tmp_path, monkeypatch, hard_fixes={"zine": "Sein", "furikaereba": "Frieren"}, names=["Frieren", "Sein"])
    assert mishear_fails(stem) == ["known mishear(s): zine"], mishear_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_CORRECTED, result
    bodies = [t for _, _, t in publish_gate.load_cues(stem)[1]]
    assert "Sein's very generous notes." in bodies
    assert "Furikaereba itsumo." in bodies, "the lyric keeps the release's own words"
    assert "Frieren itsumo." not in bodies
    assert [c["index"] for c in result["corrections"]] == [PAD]


# --- P26: a collapse artifact whose first repetition is too short to read --------------
#
# `hallucination.collapse_runs` folds a run of >= 4 identical one-word cards into one card
# (first start, last end). When the word is a short exclamation the card spans the whole
# run -- 8.35 s for 86 S01E22's "Princess!", 14.55 s for Berserk S01E06's "Griffith!" --
# and the first repetition alone is 0.0 s / 0.52 s, under reflow.MIN_DUR. The trim-to-
# first-utterance path then gave up and both episodes parked. The two fixtures are the
# live artifacts' own header and Dialogue lines over the fault's time window, with the
# real word stream for that window (verbatim; only the window is cut).


def test_the_live_86_s01e22_princess_card_is_ended_at_its_repeated_run(tmp_path):
    """#15: `Princess!` displayed 8.35 s; the ASR says it six times, first word 0.0 s."""
    stem = _live_episode(tmp_path, "e86_s01e22_princess.dubtitles.ass", words="e86_s01e22_princess_words.json")
    before_words = words_of(stem)
    assert gate_fails(stem) == ["1 card(s) over 7.5s (max 8.3s)"], gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert [a["action"] for a in result["fixes"]] == ["trim-end"]
    fix = result["fixes"][0]
    assert (round(fix["before"]["start"], 2), round(fix["before"]["dur"], 2)) == (90.5, 8.35)
    # the contiguous run ends at 94.22 (the next repetition is 1.36 s later), plus LINGER
    assert round(fix["after"][0]["end"], 2) == 94.52, fix
    assert fix["after"][0]["dur"] <= publish_gate.MAX_DUR
    assert gate_fails(stem) == []
    assert words_of(stem) == before_words, "a trim may not change a word"
    assert "Princess!" in [t for _, _, t in publish_gate.load_cues(stem)[1]]


def test_the_live_berserk_s01e06_griffith_card_is_ended_at_its_repeated_run(tmp_path):
    """#241: `Griffith!` displayed 14.55 s; the last shout 10 s later stays outside."""
    stem = _live_episode(tmp_path, "berserk_s01e06_griffith.dubtitles.ass", words="berserk_s01e06_griffith_words.json")
    before_words = words_of(stem)
    assert gate_fails(stem) == ["1 card(s) over 7.5s (max 14.5s)"], gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert [a["action"] for a in result["fixes"]] == ["trim-end"]
    fix = result["fixes"][0]
    assert (round(fix["before"]["start"], 2), round(fix["before"]["dur"], 2)) == (1270.81, 14.55)
    # the three contiguous shouts end at 1274.85, plus LINGER; the 1284.53 one is a
    # separate utterance and must not drag the card back over the budget
    assert round(fix["after"][0]["end"], 2) == 1275.15, fix
    assert gate_fails(stem) == []
    assert words_of(stem) == before_words


def test_a_short_first_utterance_with_no_repeat_still_parks(tmp_path):
    """No repetition to fall back to: the trim cannot make a readable card, so it parks."""
    words = [{"text": " Yes.", "start": 100.0, "end": 100.2, "prob": 0.9}]
    cues = padded(100.0, 108.0, "Yes.")
    stem = write_episode(tmp_path, cues, words=words)
    before = read(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNFIXABLE, result
    assert "single unbroken token" in result["summary"], result["summary"]
    assert read(stem + ".eng.dubtitles.ass") == before


def test_a_repeated_run_longer_than_the_budget_is_clamped_to_it(tmp_path):
    """Ten contiguous shouts span 10 s; the card ends inside 7.5 s, still displayable."""
    words = [{"text": " No!", "start": 100.0 + i, "end": 101.0 + i, "prob": 0.9} for i in range(10)]
    cues = padded(100.0, 112.0, "No!")
    stem = write_episode(tmp_path, cues, words=words)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert [a["action"] for a in result["fixes"]] == ["trim-end"]
    assert gate_fails(stem) == []
    assert result["fixes"][0]["after"][0]["dur"] <= publish_gate.MAX_DUR
