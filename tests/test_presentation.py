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
    return [t for txt in texts for t in presentation.tokens(txt.replace("\n", " "))]


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
    audio, and the churn gap -- a pure display property -- is still removed."""
    cards = _cards(JJK_ASS)
    out, report = presentation.restructure(cards, None, None)
    before, after = _quality(cards), _quality(out)
    assert report["words_preserved"] is True
    assert after["churn"] == 0
    assert after["orphan"] == 0
    assert after["cards"] <= before["cards"]
    assert after["frag"] < before["frag"]
    # Timing is never re-derived in this mode: a merged card spans its members and a
    # split only subdivides that span, so every boundary stays inside the original range.
    assert min(c["start"] for c in out) >= min(c["start"] for c in cards) - 1e-6
    assert max(c["end"] for c in out) <= max(c["end"] for c in cards) + 1e-6
    assert [c["start"] for c in out] == sorted(c["start"] for c in out)


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
