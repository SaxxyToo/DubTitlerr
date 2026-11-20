"""gate_fix: one deterministic, model-free repair attempt for a gate refusal.

The shape under test is the live one: the publish gate refuses a file that is *almost* a
dubtitle -- one card left running 16 s over silence, one line never wrapped -- and the merge
pass otherwise parks it and forgets it. Every test here asserts the two properties that make
the attempt safe to run automatically: the words never change, and nothing is written unless
the result passes the gate.

The padding cards are load-bearing: the gate also requires 65% of cards to carry sentence
punctuation, so a five-cue fixture cannot absorb a split the way a 400-cue episode can.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

LONG_LINE = ("The mitosis process in the left arm of Unit 2 has cleared every "
             "requirement we set for it now")


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
    before = [ln for ln in read(stem + ".eng.dubtitles.ass").splitlines()
              if ln.startswith("Dialogue:")]
    touched = [ln for ln in before if "mitosis" in ln]
    gate_fix.attempt(stem)
    after = [ln for ln in read(stem + ".eng.dubtitles.ass").splitlines()
             if ln.startswith("Dialogue:")]
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
    words = [{"text": w, "start": 100.0 + i * 0.31, "end": 100.31 + i * 0.31, "prob": 0.9}
             for i, w in enumerate(tokens)]
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
    sentence = ("I am not going to let go of my freedom and that is the end of it "
                "for now and ever")
    tokens = sentence.split()
    per = 14.0 / len(tokens)
    words = [{"text": w, "start": 100.0 + i * per, "end": 100.0 + (i + 1) * per,
              "prob": 0.9} for i, w in enumerate(tokens)]
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
    cues = [(1.0, 3.0, "the direct cause you mean was the mental breakdown"),
            (4.0, 6.0, "the result of the contact yes"),
            (7.0, 9.0, "that doll as if it were her real daughter")]
    stem = write_episode(tmp_path, cues)
    before = read(stem + ".eng.dubtitles.ass")

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_UNSUPPORTED, result
    assert "sentence punctuation" in result["summary"]
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
    words = [{"text": w, "start": 100.0 + i * 0.3, "end": 100.3 + i * 0.3, "prob": 0.9}
             for i, w in enumerate(sentence.split())]
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
        f"{i}\n00:{int(s) // 60:02d}:{int(s) % 60:02d},000 --> "
        f"00:{int(e) // 60:02d}:{int(e) % 60:02d},000\n{t}\n\n"
        for i, (s, e, t) in enumerate(cards, 1))
    (d / "ep01.eng.dubtitles.srt").write_text(body)
    assert gate_fails(stem)

    result = gate_fix.attempt(stem)

    assert result["outcome"] == gate_fix.OUTCOME_FIXED, result
    assert gate_fails(stem) == []
    assert words_of(stem) == [w for _, _, t in cards for w in t.split()]


# --- fault classification -----------------------------------------------------------


def test_split_fails_separates_layout_from_everything_else():
    layout, other = gate_fix.split_fails([
        "1 card(s) over 7.5s (max 16.9s)",
        "line of 446 chars (max 46)",
        "2 card(s) over 88 chars",
        "coverage 41% (last cue 600.0s of 1450.0s)",
        "12% of cards carry sentence punctuation",
        "known mishear(s): oscar",
    ])
    assert len(layout) == 3
    assert len(other) == 3


def test_cue_faults_is_the_gates_own_budget():
    assert gate_fix.cue_faults("short line", 3.0) == []
    assert gate_fix.cue_faults("x" * 89, 3.0) == ["89 chars", "89-char line"]
    assert gate_fix.cue_faults("x", 8.0) == ["8.00s card"]
    assert gate_fix.cue_faults("Unseen\n" + "y" * 50, 3.0), "second line too long"
