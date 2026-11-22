"""P29: card structure + timing from word timestamps.

Fixtures are the live library's own bytes (86 S01E01 delivered sidecar + its
words.json, Toradora S01E24 the same, JJK S01E19 a sourced track with NO words.json),
because the defects this stage exists to fix were measured on them: `'Gotta say,'` cut
off its own sentence, a lone `'As'` on screen for seven seconds, 87 back-to-back <80 ms
gaps.
"""

import json
import os
import shutil

from dubtitlerr.pipeline import presentation, publish_gate

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "artifacts")

E86_ASS = os.path.join(ART, "e86_s01e01_presentation.ass")
E86_WORDS = os.path.join(ART, "e86_s01e01_presentation.words.json")
TORA_ASS = os.path.join(ART, "toradora_s01e24_presentation.ass")
TORA_WORDS = os.path.join(ART, "toradora_s01e24_presentation.words.json")
JJK_ASS = os.path.join(ART, "jjk_s01e19_sourced.ass")


def _cards(path):
    _lines, events, _fields = presentation.read_ass(path)
    return [{"start": e["start"], "end": e["end"], "text": e["text"]} for e in events]


def _words(path):
    with open(path) as f:
        return json.load(f)["words"]


def _quality(cards):
    short = [c for c in cards if len(presentation.tokens(c["text"])) <= 2]
    frag = 0
    orphan = 0
    for i, c in enumerate(cards):
        if len(presentation.tokens(c["text"])) > 2 or presentation.is_terminal(c["text"]):
            continue
        if i + 1 < len(cards) and cards[i + 1]["start"] - c["end"] <= 0.5:
            frag += 1
            orphan += 1
    gaps = [cards[i + 1]["start"] - cards[i]["end"] for i in range(len(cards) - 1)]
    durs = [c["end"] - c["start"] for c in cards]
    return {
        "cards": len(cards),
        "short2w": len(short),
        "orphan": orphan,
        "frag": frag,
        "disprop": sum(1 for c in short if c["end"] - c["start"] >= 2.5),
        "under1": sum(1 for d in durs if d < 1.0),
        "churn": sum(1 for g in gaps if g < 0.08),
        "maxdur": max(durs) if durs else 0.0,
    }


def _flat_tokens(texts):
    # The turn dash is a display MARK, not a word: it is stripped on both sides of the
    # comparison (this is the same hook P31's invariant uses).
    return [t for txt in texts for t in presentation.content_tokens(txt)]


def test_86_s01e01_is_rebuilt_into_sentence_units_without_churn():
    cards = _cards(E86_ASS)
    out, report = presentation.restructure(cards, _words(E86_WORDS), None)
    before, after = _quality(cards), _quality(out)

    assert report["words_preserved"] is True
    assert _flat_tokens([c["text"] for c in out]) == _flat_tokens([c["text"] for c in cards])
    # The measured defects, each one gone.
    assert after["churn"] == 0  # was 87 gaps < 80 ms
    assert after["orphan"] == 0  # was 23 single-word/partial cards
    assert after["under1"] <= 3  # was 17 cards under 1.0 s
    assert after["maxdur"] <= presentation.MAX_DISPLAY + presentation.reflow.EPS
    assert after["frag"] < before["frag"]
    assert after["disprop"] < before["disprop"]
    assert after["short2w"] < before["short2w"]
    # `'Gotta say,'` no longer ships as its own card: the sentence it starts is one unit
    # and the forced split lands on the clause, not eleven characters in.
    texts = [presentation.flatten(c["text"]) for c in out]
    assert "Gotta say," not in texts
    assert not any(t.startswith("though, with that many") for t in texts)
    assert any(t.startswith("Gotta say, though, with that many of them,") for t in texts)
    # The stretched word that made a lone `'As'` sit on screen for 7.4 s is re-spaced
    # inside its own segment, so no card is that single word any more.
    assert "As" not in texts
    assert report["stretched_segments"] > 0


def test_86_s01e01_never_changes_a_word():
    cards = _cards(E86_ASS)
    out, _report = presentation.restructure(cards, _words(E86_WORDS), None)
    assert _flat_tokens([c["text"] for c in out]) == _flat_tokens([c["text"] for c in cards])


def test_toradora_s01e24_is_equal_or_better_on_every_axis():
    cards = _cards(TORA_ASS)
    out, report = presentation.restructure(cards, _words(TORA_WORDS), None)
    before, after = _quality(cards), _quality(out)
    assert report["words_preserved"] is True
    for key in ("frag", "disprop", "short2w", "under1", "churn", "maxdur"):
        assert after[key] <= before[key], f"{key}: {before[key]} -> {after[key]}"
    assert after["orphan"] == 0


def test_sourced_track_without_words_is_structure_only_and_fail_closed():
    """JJK S01E19 has no words.json: cards may join, nothing is re-timed from invented
    audio, and the churn gap -- a pure display property -- is still removed. Turns need
    measured pauses, so a no-words pass inserts NO dashes."""
    cards = _cards(JJK_ASS)
    out, report = presentation.restructure(cards, None, None)
    before, after = _quality(cards), _quality(out)
    assert report["words_preserved"] is True
    assert report["turn_dashes"] == 0
    assert not any(presentation.turn_marked(c["text"]) for c in out)
    assert after["churn"] == 0
    assert after["orphan"] == 0
    assert after["cards"] <= before["cards"]
    assert after["frag"] < before["frag"]
    # Timing is never re-derived in this mode: a merged card spans its members and a
    # split only subdivides that span, so every boundary stays inside the original range.
    assert min(c["start"] for c in out) >= min(c["start"] for c in cards) - 1e-6
    assert max(c["end"] for c in out) <= max(c["end"] for c in cards) + 1e-6
    assert [c["start"] for c in out] == sorted(c["start"] for c in out)


# --- P30: speaker turns -------------------------------------------------------------


def _run(*specs):
    """Word run from (text, start, end, seg) tuples."""
    return [{"text": t, "start": a, "end": b, "seg": s} for t, a, b, s in specs]


def test_86_s01e01_marks_the_two_speaker_blocks_with_dashes():
    """The merged question/answer blocks get the fansub `- ` convention; nothing else."""
    cards = _cards(E86_ASS)
    out, report = presentation.restructure(cards, _words(E86_WORDS), None)
    marked = [c["text"] for c in out if presentation.turn_marked(c["text"])]

    assert report["turn_dashes"] == len(marked) == 3
    assert "- Not the other way around?\n- Correct." in marked
    assert "- Gather round.\n- Morning." in marked
    assert "- I want them!\n- Hey, calm down." in marked
    for text in marked:
        lines = text.split("\n")
        assert len(lines) == 2  # the classic two-line block
        assert all(ln.startswith(presentation.TURN_PREFIX) for ln in lines)
        assert all(len(ln) <= presentation.MAX_LINE for ln in lines)
    # Zero dashes where there is no turn: a single speaker's sentence pair is not one,
    # however many sentence boundaries it holds.
    assert "Back to business. We can't find a single handler to take this post." not in marked

    # Every marked card really does hold a sentence boundary inside it; a card without
    # one can never be dashed.
    def has_inner_boundary(text):
        toks = presentation.tokens(text)
        return any(presentation.is_terminal(" ".join(toks[:k])) for k in range(1, len(toks)))

    assert not any(presentation.turn_marked(c["text"]) for c in out if not has_inner_boundary(c["text"]))
    # The invariant still holds with the marks: strip the dashes and the word sequence
    # is byte-identical to the delivered file's.
    assert _flat_tokens([c["text"] for c in out]) == _flat_tokens([c["text"] for c in cards])


def test_toradora_s01e24_marks_the_rapid_ping_pong_blocks():
    cards = _cards(TORA_ASS)
    out, report = presentation.restructure(cards, _words(TORA_WORDS), None)
    marked = [c["text"] for c in out if presentation.turn_marked(c["text"])]

    assert report["turn_dashes"] == len(marked) == 6
    assert "- And your breath stinks.\n- Shut up!" in marked
    assert "- Happy now?\n- Later!" in marked
    assert "- So cool, don't you think?\n- Mm-hmm." in marked
    assert "- Don't you think?\n- Yeah!" in marked
    assert "- So I can love you.\n- Ryuuji." in marked
    assert "- You hear that? She's laughing!\n- Yeah, I know." in marked
    # `'God, Mom. So childish.'` is a pause under TURN_PAUSE: no dash, fail closed.
    assert "God, Mom. So childish." not in marked
    assert _flat_tokens([c["text"] for c in out]) == _flat_tokens([c["text"] for c in cards])


def _seam(left: list[str], right: list[str], pause: float, right_seg: int = 1) -> list[dict]:
    """A word run for ``left | right`` with ``pause`` seconds of silence at the seam."""
    run: list[dict] = []
    t = 0.0
    for word in left:
        run.append({"text": " " + word, "start": t, "end": t + 0.2, "seg": 0})
        t += 0.2
    t += pause
    for word in right:
        run.append({"text": " " + word, "start": t, "end": t + 0.2, "seg": right_seg})
        t += 0.2
    return run


def test_detect_turn_is_fail_closed_on_every_uncertain_signal():
    """One missing piece of evidence and the card keeps its single line."""
    # Positive: a pause, a segment seam, two complete sentences, two lines.
    got = presentation.detect_turn("Where is he? Gone.", _seam(["Where", "is", "he?"], ["Gone."], 0.4), 2.0)
    assert got is not None and got[0] == "- Where is he?\n- Gone." and round(got[1], 3) == 0.4

    def no(mark, text, run, display=4.0):
        assert presentation.detect_turn(text, run, display) is None, mark

    no("pause under TURN_PAUSE", "Where is he? Gone.", _seam(["Where", "is", "he?"], ["Gone."], 0.2))
    no("same whisper segment", "Where is he? Gone.", _seam(["Where", "is", "he?"], ["Gone."], 0.4, right_seg=0))
    no("lowercase continuation", "Where is he? gone.", _seam(["Where", "is", "he?"], ["gone."], 0.4))
    no("right side is not a sentence", "Where is he? Gone", _seam(["Where", "is", "he?"], ["Gone"], 0.4))
    no("two single-word sides", "Pleiades. Processor.", _seam(["Pleiades."], ["Processor."], 0.44))
    long_right = ["Gone."] * 6 + ["Right", "then."]
    no(
        "right side would need a third line",
        "Where is he? " + " ".join(long_right),
        _seam(["Where", "is", "he?"], long_right, 0.4),
    )
    # Two qualifying seams are three speakers: not a two-line card.
    three = _seam(["Where", "is", "he?"], ["Gone", "now."], 0.4)
    t = three[-1]["end"] + 0.4
    for word in ("Right", "then."):
        three.append({"text": " " + word, "start": t, "end": t + 0.2, "seg": 2})
        t += 0.2
    no("three speakers", "Where is he? Gone now. Right then.", three)


def test_a_sourced_track_keeps_its_own_markers_verbatim_and_gets_no_new_ones():
    cards = [
        {"start": 1.0, "end": 3.0, "text": "- What?\\N- Yeah."},
        {"start": 3.05, "end": 4.5, "text": "It's me."},
    ]
    words = _run((" It's", 3.1, 3.3, 0), (" me.", 3.3, 3.5, 0))
    out, report = presentation.restructure(cards, words, None)

    assert report["sourced_turn_cards"] == 1
    assert report["turn_dashes"] == 0
    assert out[0]["text"] == "- What?\n- Yeah."  # both markers, both lines, untouched
    assert out[1]["text"] == "It's me."  # never merged into the sourced block
    assert presentation.content_tokens("- What?\n- Yeah.") == ["What?", "Yeah."]


def test_dashes_land_in_the_file_and_survive_the_gate(tmp_path):
    stem = str(tmp_path / "Show" / "ep01")
    os.makedirs(os.path.dirname(stem), exist_ok=True)
    shutil.copy(TORA_ASS, stem + ".dub.en.ass")
    shutil.copy(TORA_WORDS, stem + ".dubtitles.words.json")

    report, changed = presentation.restructure_file(stem + ".dub.en.ass", {"words": _words(TORA_WORDS)}, None)
    assert changed is True and report["turn_dashes"] == 6
    text = open(stem + ".dub.en.ass", encoding="utf-8-sig").read()
    assert "\\N- " in text
    assert publish_gate.evaluate(publish_gate.parse_ass(text, style="Dubtitles"), None) == []
    # A second pass sees the markers as authorship: it preserves them and adds none.
    again, _ = presentation.restructure_file(stem + ".dub.en.ass", {"words": _words(TORA_WORDS)}, None)
    assert again["sourced_turn_cards"] == 6 and again["turn_dashes"] == 0


def test_an_unmapped_card_is_a_barrier(tmp_path):
    """A card whose tokens match no words is left exactly where it is."""
    cards = [
        {"start": 1.0, "end": 2.0, "text": "Hello there."},
        {"start": 2.05, "end": 3.0, "text": "zzz qqq"},
        {"start": 3.05, "end": 4.0, "text": "how are you"},
    ]
    words = [
        {"text": " Hello", "start": 1.0, "end": 1.4, "seg": 0},
        {"text": " there.", "start": 1.4, "end": 2.0, "seg": 0},
        {"text": " how", "start": 3.05, "end": 3.3, "seg": 1},
        {"text": " are", "start": 3.3, "end": 3.6, "seg": 1},
        {"text": " you", "start": 3.6, "end": 4.0, "seg": 1},
    ]
    out, _report = presentation.restructure(cards, words, None)
    texts = [presentation.flatten(c["text"]) for c in out]
    assert "zzz qqq" in texts
    assert not any(t.startswith("Hello there. zzz") for t in texts)


def test_a_rebuild_that_would_change_words_is_refused(monkeypatch):
    cards = _cards(E86_ASS)[:20]
    real = presentation._unit_cards

    def corrupted(unit, ranges, words):
        cards_out, info = real(unit, ranges, words)
        if cards_out:
            cards_out[0] = {**cards_out[0], "text": cards_out[0]["text"] + " edited"}
        return cards_out, info

    monkeypatch.setattr(presentation, "_unit_cards", corrupted)
    out, report = presentation.restructure(cards, _words(E86_WORDS), None)
    assert report["words_preserved"] is False
    assert _flat_tokens([c["text"] for c in out]) == _flat_tokens([c["text"] for c in cards])
    assert [c["start"] for c in out] == [c["start"] for c in cards]


def test_file_rewrite_touches_only_the_dialogue_cards(tmp_path):
    stem = str(tmp_path / "Show" / "ep01")
    os.makedirs(os.path.dirname(stem), exist_ok=True)
    ass = stem + ".dub.en.ass"
    shutil.copy(E86_ASS, ass)
    shutil.copy(E86_WORDS, stem + ".dubtitles.words.json")

    report, changed = presentation.restructure_file(ass, {"words": _words(E86_WORDS)}, None)
    assert changed is True and report["outcome"] == "rebuilt"

    def other(lines):
        return [ln for ln in lines if not (ln.startswith("Dialogue:") and "," + "Dubtitles" + "," in ln)]

    def dialogue(lines):
        return [ln for ln in lines if ln.startswith("Dialogue:") and ",Dubtitles," in ln]

    before = open(E86_ASS, encoding="utf-8").read().splitlines()
    after = open(ass, encoding="utf-8").read().splitlines()
    assert other(before) == other(after)  # styles, signs, lyrics: byte-identical
    assert len(dialogue(after)) == report["cards_after"] < len(dialogue(before))
    assert publish_gate.evaluate(publish_gate.parse_ass(open(ass, encoding="utf-8-sig").read(), style="Dubtitles"), None) == []

    # A second pass does not have to be a literal no-op (it re-derives layout from the
    # words, and the first pass moved the display windows the second one sees), but it
    # must stay a rebuild of the SAME words with the same guarantees -- the epoch stamp,
    # not convergence, is what makes the in-app re-pass exactly once.
    again, changed_again = presentation.restructure_file(ass, {"words": _words(E86_WORDS)}, None)
    assert again["words_preserved"] is True
    final = open(ass, encoding="utf-8").read().splitlines()
    assert _flat_tokens([e["text"] for e in presentation.read_ass(ass)[1]]) == _flat_tokens(
        [e["text"] for e in presentation.read_ass(E86_ASS)[1]]
    )
    assert len(dialogue(final)) <= int(1.05 * report["cards_after"])


def test_epoch_stamp_makes_the_repass_exactly_once(tmp_path):
    stem = str(tmp_path / "Show" / "ep01")
    os.makedirs(os.path.dirname(stem), exist_ok=True)
    shutil.copy(TORA_ASS, stem + ".dub.en.ass")
    shutil.copy(TORA_WORDS, stem + ".dubtitles.words.json")
    open(stem + ".dubtitles.done", "w").write("{}")

    assert presentation.is_stale(stem) is True
    first = presentation.apply_episode(stem)
    assert first["outcome"] == "rebuilt"
    assert presentation.is_stale(stem) is False
    second = presentation.apply_episode(stem)
    assert second["outcome"] == "no-op"
    assert json.load(open(stem + presentation.STAMP_SUFFIX))["epoch"] == presentation.PRESENTATION_EPOCH

    sweep = presentation.sweep([str(tmp_path)])
    assert sweep["scanned"] == 1 and sweep["stale"] == 0


def test_a_gate_refusal_restores_the_original_bytes(tmp_path, monkeypatch):
    stem = str(tmp_path / "Show" / "ep01")
    os.makedirs(os.path.dirname(stem), exist_ok=True)
    shutil.copy(TORA_ASS, stem + ".dub.en.ass")
    shutil.copy(TORA_WORDS, stem + ".dubtitles.words.json")
    original = open(stem + ".dub.en.ass", encoding="utf-8").read()

    monkeypatch.setattr(publish_gate, "evaluate", lambda *a, **k: ["line of 200 chars (max 46)"])
    report = presentation.apply_episode(stem)
    assert report["outcome"] == "regressed"
    assert open(stem + ".dub.en.ass", encoding="utf-8").read() == original
    assert json.load(open(stem + presentation.STAMP_SUFFIX))["outcome"] == "regressed"
    assert presentation.is_stale(stem) is False  # a deterministic refusal does not churn
