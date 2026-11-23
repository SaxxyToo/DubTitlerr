"""P31: presentation restoration — punctuation, capitalisation, spacing.

Owner-sanctioned rule: punctuation/capitalisation/spacing may be ADDED or FIXED;
the word sequence never changes, is never added to, and never loses a word.

The fixtures are the live library's own bytes: 86 S01E01 delivered sidecar plus its
words.json (the worst measured episode: 65.2% terminal / 66.3% caps over all cards,
92.7% / 94.4% over sentence-final cards after P29), Toradora S01E24, and JJK S01E19
(the sourced track P31 must not degrade). The restored fixture was produced by a live
run of this stage against the repair model.
"""

import json
import os
import re
import shutil

import store_seed

from dubtitlerr.core import artifact_store
from dubtitlerr.pipeline import presentation, publish_gate, restoration

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "artifacts")

E86_ASS = os.path.join(ART, "e86_s01e01_presentation.ass")
E86_WORDS = os.path.join(ART, "e86_s01e01_presentation.words.json")
E86_RESTORED = os.path.join(ART, "e86_s01e01_restored.ass")
TORA_ASS = os.path.join(ART, "toradora_s01e24_presentation.ass")
TORA_WORDS = os.path.join(ART, "toradora_s01e24_presentation.words.json")
JJK_ASS = os.path.join(ART, "jjk_s01e19_sourced.ass")
JJK_E12_ASS = os.path.join(ART, "jjk_s01e12_live_slice.ass")
JJK_E12_WORDS = os.path.join(ART, "jjk_s01e12_live_slice.words.json")


# --- the invariant ------------------------------------------------------------------


def test_word_tokens_strip_punctuation_case_spacing_tags_and_turn_dashes():
    assert restoration.word_tokens("  - Don't  stop, ME! ") == ("dont", "stop", "me")
    # A dash the model writes for a pause separates tokens instead of gluing them.
    assert restoration.word_tokens("tempo—you") == ("tempo", "you")
    assert restoration.word_tokens("Stop!Stop") == ("stop", "stop")
    # ASS override blocks and line escapes are presentation, not words.
    assert restoration.word_tokens("{\\an8\\pos(1,2)}May 13th,\\NStellar Year 2148") == (
        "may",
        "13th",
        "stellar",
        "year",
        "2148",
    )
    assert restoration.word_tokens("Milizé") == ("milizé",)


def test_invariant_is_mechanical_not_judgemental():
    assert restoration.invariant_ok("Hello, world", "HELLO world!")
    assert restoration.invariant_ok("I dont know", "I don't know.")
    assert restoration.invariant_ok("Wait... what?", "Wait… what?")
    assert restoration.invariant_ok("Hello there", "Hello, there")  # punctuation may be added
    assert not restoration.invariant_ok("Hello there", "Hello there, friend")
    assert not restoration.invariant_ok("Hello there", "Hello")


# --- house style --------------------------------------------------------------------


def test_house_style_sound_description_ellipsis_quotes_and_spacing():
    # *Screaming* is the anomaly sampled in 86 E01; the house uses parentheses.
    assert restoration.house_style("*Screaming*") == "(Screaming)"
    # A sound description split across a display line break is still one description.
    assert restoration.house_style("*crickets\nchirping* *crack*") == "(crickets chirping) (crack)"
    assert restoration.house_style("Wait... I can't") == "Wait… I can't"
    assert restoration.house_style("tomorrow ,") == "tomorrow,"
    assert restoration.house_style("Stop !") == "Stop!"
    assert restoration.house_style('He said "go"') == "He said “go”"
    assert restoration.house_style("too  many   spaces") == "too many spaces"
    # House style never changes words: the invariant holds by construction.
    for before, after in (
        ("*Screaming*", restoration.house_style("*Screaming*")),
        ('He said "go"', restoration.house_style('He said "go"')),
        ("Wait... I can't", restoration.house_style("Wait... I can't")),
    ):
        assert restoration.invariant_ok(before, after)


# --- deficiency detection -----------------------------------------------------------


def _cards(*texts):
    return [{"start": float(i), "end": float(i) + 0.9, "text": t} for i, t in enumerate(texts)]


def test_only_sentence_level_defects_are_flagged():
    cards = _cards(
        "Gotta say, though, with that many of them,",  # clause end: sentence continues
        "I don't think any of you are coming back alive.",  # well formed
        "The clear skies might not complete your whole crew tomorrow ,",  # spacing + terminal
        "*Screaming*",  # style anomaly
    )
    found = restoration.findings(cards)
    assert 0 not in found  # a comma-ended card is a continuation, not a defect
    assert 1 not in found  # well-formed sourced text is validated, not rewritten
    assert "spacing" in found[2]  # the measured 'tomorrow ,' defect
    assert "style" in found[3]  # '*Screaming*'


def test_lowercase_only_counts_at_a_sentence_start():
    cards = _cards("Stop!", "we have to move,", "and keep moving")
    found = restoration.findings(cards)
    assert "caps" in found[1]  # previous card ends a sentence
    assert "caps" not in found[2]  # continuation of a sentence: lowercase is correct


def test_styled_cards_and_non_words_are_never_rewritten():
    cards = _cards("{\\i1}SIGN TEXT{\\i0}", "♪ ♪", "")
    assert restoration.findings(cards) == {}


# --- restoration --------------------------------------------------------------------


def test_a_corrected_line_is_applied_and_counted():
    cards = _cards("Stop !", "we have to go")
    asked = []

    def ask(prompt, n_words):
        asked.append(prompt)
        return "[0] Stop!\n[1] We have to go."

    new, report = restoration.restore_cards(cards, ask=ask)
    assert [c["text"] for c in new] == ["Stop!", "We have to go."]
    assert [c["start"] for c in new] == [c["start"] for c in cards]
    assert [c["end"] for c in new] == [c["end"] for c in cards]
    assert report["words_preserved"] is True
    assert report["spacing_fixed"] == 1
    assert report["caps_fixed"] == 1
    assert report["punct_added"] >= 1
    assert report["cards_restored"] == 2
    assert asked and "[1] we have to go" in asked[0]
    assert "context before: Stop!" in asked[0]  # the spacing fix needed no model call


def test_a_word_change_is_a_hard_rejection_and_the_card_keeps_its_bytes():
    cards = _cards("we have to go")
    new, report = restoration.restore_cards(cards, ask=lambda p, n: "[0] We gotta go.")
    assert "gotta" not in new[0]["text"]  # the model's invented word never lands
    assert restoration.word_tokens(new[0]["text"]) == ("we", "have", "to", "go")
    assert report["rejected_guard"] == 1
    assert report["words_preserved"] is True


def test_a_missing_or_empty_answer_never_costs_a_card():
    cards = _cards("we have to go")
    new, report = restoration.restore_cards(cards, ask=lambda p, n: "")
    assert restoration.word_tokens(new[0]["text"]) == ("we", "have", "to", "go")
    assert report["llm_empty"] == 1
    assert report["cards_restored_lm"] == 0


def test_a_well_formed_card_is_never_sent_to_the_model():
    cards = _cards("I don't think any of you are coming back alive.")

    def ask(prompt, n_words):
        raise AssertionError("no deficient line should reach the model")

    new, report = restoration.restore_cards(cards, ask=ask)
    assert new[0]["text"] == cards[0]["text"]
    assert report["cards_deficient"] == 0


def test_line_layout_and_turn_markers_survive_restoration():
    cards = _cards("hello there\n- my friend")
    new, _report = restoration.restore_cards(cards, ask=lambda p, n: "[0] Hello there. My friend.")
    assert new[0]["text"] == "Hello there.\n- My friend."


def test_a_restoration_that_would_overrun_the_card_is_refused():
    # Two legal lines, one presentation character from the hard ceiling: the terminal
    # mark that would push the file past publish_gate's 88-char limit is refused.
    original = "A" * 46 + "\n" + "b" * 41
    cards = _cards(original)
    new, report = restoration.restore_cards(cards, ask=lambda p, n: "[0] " + original.replace("\n", " ") + ".")
    assert new[0]["text"] == original
    assert report["layout_rejected"] == 1


# --- epoch / file apply -------------------------------------------------------------


def _stem(tmp_path):
    stem = str(tmp_path / "Show" / "ep01")
    os.makedirs(os.path.dirname(stem), exist_ok=True)
    return stem


def _fake_model(prompt, n_words):
    """A deterministic stand-in for the repair model: capitalise and end the sentence.

    It answers exactly the numbered cards it is given, changes no word, and is enough
    to prove the plumbing: only deficient cards are sent, layout is rebuilt, counters
    move, the file is stamped once.
    """
    out = []
    for line in prompt.splitlines():
        m = re.match(r"^\[(\d+)\]\s*(.+)$", line)
        if not m:
            continue
        text = m.group(2)
        text = text[:1].upper() + text[1:] if text[:1].isalpha() else text
        if not restoration.ends_terminal(text) and not re.search(r"[,;:—–]\s*$", text):
            text += "."
        out.append(f"[{m.group(1)}] {text}")
    return "\n".join(out)


def test_repass_stamps_a_delivered_episode_once(tmp_path, monkeypatch):
    stem = _stem(tmp_path)
    shutil.copy(E86_ASS, stem + ".eng.dubtitles.ass")
    store_seed.words(stem + ".dubtitles.words.json", json.load(open(E86_WORDS)))
    artifact_store.put_json(stem, "done", {})

    monkeypatch.setattr(restoration, "_ask", _fake_model)
    first = restoration.apply_episode(stem)
    assert first["outcome"] == "restored", first
    assert restoration.is_stale(stem) is False
    stamp = artifact_store.get_json(stem, "restoration")
    assert stamp["epoch"] == restoration.RESTORATION_EPOCH

    delivered = open(stem + ".eng.dubtitles.ass", encoding="utf-8").read()
    second = restoration.apply_episode(stem)
    assert second["outcome"] == "no-op"
    assert open(stem + ".eng.dubtitles.ass", encoding="utf-8").read() == delivered


def test_an_unreachable_model_keeps_the_bytes_safe_and_leaves_the_pass_unstamped(tmp_path, monkeypatch):
    stem = _stem(tmp_path)
    shutil.copy(E86_ASS, stem + ".eng.dubtitles.ass")
    store_seed.words(stem + ".dubtitles.words.json", json.load(open(E86_WORDS)))

    monkeypatch.setattr(restoration, "_ask", lambda p, n: "")
    report = restoration.apply_episode(stem, force=True)
    # Deterministic spacing/style repairs are safe and kept; the model work is not
    # stamped as done, so the next pass retries it.
    assert report["outcome"] == "partial", report
    assert not artifact_store.exists(stem, "restoration")
    assert report["words_preserved"] is True


def test_a_gate_refusal_restores_the_original_bytes(tmp_path, monkeypatch):
    stem = _stem(tmp_path)
    shutil.copy(E86_ASS, stem + ".eng.dubtitles.ass")
    store_seed.words(stem + ".dubtitles.words.json", json.load(open(E86_WORDS)))
    original = open(stem + ".eng.dubtitles.ass", encoding="utf-8").read()

    monkeypatch.setenv("PRESENTATION_REPASS", "0")
    monkeypatch.setattr(restoration, "_ask", _fake_model)
    # ONLY the restored bytes refuse (the input was clean): a pass that makes the file
    # unpublishable must go back.
    calls = []

    def fake_evaluate(cues, duration=None, **_kw):
        calls.append(len(cues))
        return ["line of 200 chars (max 46)"] if len(calls) > 1 else []

    monkeypatch.setattr(publish_gate, "evaluate", fake_evaluate)
    report = restoration.apply_episode(stem)
    assert report["outcome"] == "regressed"
    assert open(stem + ".eng.dubtitles.ass", encoding="utf-8").read() == original
    assert restoration.is_stale(stem) is False


def test_a_refusal_the_input_already_carried_keeps_the_restoration(tmp_path, monkeypatch):
    """E12's shape one stage later: the text work is kept because the 57-char card was
    already there when this pass started. The live stamp recorded 107 restored cards
    thrown away by a refusal this stage never introduced."""
    stem = _stem(tmp_path)
    shutil.copy(JJK_E12_ASS, stem + ".eng.dubtitles.ass")
    store_seed.words(stem + ".dubtitles.words.json", json.load(open(JJK_E12_WORDS)))
    monkeypatch.setenv("PRESENTATION_REPASS", "0")
    monkeypatch.setattr(restoration, "_ask", lambda prompt, n: "")

    fails_before = publish_gate.evaluate(publish_gate.load_cues(stem)[1])
    assert fails_before == ["line of 57 chars (max 46)"], fails_before
    report = restoration.apply_episode(stem, force=True)
    assert report["outcome"] != "regressed", report
    assert report["pre_existing_fails"] == fails_before
    assert report["changed_cards"] > 0
    assert publish_gate.evaluate(publish_gate.load_cues(stem)[1]) == fails_before


# --- live bytes ---------------------------------------------------------------------


def _p29_cards(ass, words_path=None):
    _lines, events, _fields = presentation.read_ass(ass)
    cards = [{"start": e["start"], "end": e["end"], "text": e["text"]} for e in events]
    words = None
    if words_path:
        with open(words_path) as f:
            doc = json.load(f)
        words, duration = doc["words"], doc.get("audio_duration")
    else:
        duration = None
    out, _report = presentation.restructure(cards, words, duration)
    return out


def _metric(cards):
    texts = [c["text"] for c in cards]
    return {
        "terminal": sum(
            1 for i, t in enumerate(texts) if restoration.is_sentence_final(cards, i) and restoration.ends_terminal(t)
        ),
        "terminal_eligible": sum(1 for i in range(len(texts)) if restoration.is_sentence_final(cards, i)),
        "caps": sum(
            1 for i, t in enumerate(texts) if restoration.is_sentence_start(cards, i) and restoration.first_alpha(t).isupper()
        ),
        "caps_eligible": sum(1 for i in range(len(texts)) if restoration.is_sentence_start(cards, i)),
    }


def test_the_live_restored_86_e01_artifact_clears_the_bar_and_keeps_every_word():
    source = _p29_cards(E86_ASS, E86_WORDS)
    # The restored fixture is the final published file; read its cards verbatim.
    _lines, events, _fields = presentation.read_ass(E86_RESTORED)
    restored = [{"start": e["start"], "end": e["end"], "text": e["text"]} for e in events]

    assert [restoration.word_tokens(c["text"]) for c in restored] == [restoration.word_tokens(c["text"]) for c in source]
    assert "".join(f" {restoration.word_tokens(c['text'])}" for c in restored) == "".join(
        f" {restoration.word_tokens(c['text'])}" for c in source
    )

    m = _metric(restored)
    assert m["terminal"] / m["terminal_eligible"] >= 0.95, m
    assert m["caps"] / m["caps_eligible"] >= 0.95, m
    # The sampled defects are gone.
    texts = [presentation.flatten(c["text"]) for c in restored]
    assert not any("*" in t for t in texts)
    assert not any(" ," in t for t in texts)


def test_the_sourced_set_is_not_degraded():
    before = _p29_cards(JJK_ASS)
    # The model is only asked about deficient lines; a clear answer fixes them and the
    # invariant protects every word. Here the answer is "no change", the worst case.
    after, report = restoration.restore_cards(before, ask=lambda p, n: "")
    assert report["words_preserved"] is True
    assert [restoration.word_tokens(c["text"]) for c in after] == [restoration.word_tokens(c["text"]) for c in before]
    m = _metric(after)
    assert m["terminal"] / m["terminal_eligible"] >= 0.95, m
    assert m["caps"] / m["caps_eligible"] >= 0.95, m


def test_every_fixture_keeps_its_word_sequence_and_no_axis_degrades():
    """The invariant is checked across the whole spread, not one show: the worst
    generated episode, a second generated one, and the sourced track."""
    for ass, words_path in ((E86_ASS, E86_WORDS), (TORA_ASS, TORA_WORDS), (JJK_ASS, None)):
        before = _p29_cards(ass, words_path)
        after, report = restoration.restore_cards(before, ask=_fake_model)
        assert report["words_preserved"] is True, ass
        assert report["invariant_violations"] == 0, ass
        assert [restoration.word_tokens(c["text"]) for c in after] == [restoration.word_tokens(c["text"]) for c in before], ass
        b, a = _metric(before), _metric(after)
        assert a["terminal"] / a["terminal_eligible"] >= b["terminal"] / b["terminal_eligible"], ass
        assert a["caps"] / a["caps_eligible"] >= b["caps"] / b["caps_eligible"], ass
