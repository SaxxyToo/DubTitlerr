"""P39-B2 parity: conf/words/qc/cuts read the same from the artifact store as they did
from the files they used to be.

Each test seeds the store with the EXACT bytes the old sidecar held (``put_text``) and
compares what a consumer derives from that row against the derivation computed from the
same bytes parsed as JSON -- which was the old reader's entire job. The file-shaped
document is the oracle; the store row is what ships.

Covers the acceptance criterion that ``repair``, ``glossary_acquire``, ``recreate_srt`` and
``review_apply`` reach the same conf decisions from the store as from the old file bytes, and
that a text-tier replay still reads its word list (and its scene-cut cache) without the
decoder.
"""

import json
import os

import repair_ref_stub

from dubtitlerr.core import artifact_store, common, decisions, unresolved
from dubtitlerr.glossary import glossary as _glossary
from dubtitlerr.glossary import glossary_acquire
from dubtitlerr.pipeline import generate, mux, recreate_srt, reflow, repair
from dubtitlerr.review import review_apply

# A conf document with one clear card, two low-confidence ones and one out-of-speech row --
# enough for repair.is_target(), review_apply's verdict matching and the unresolved readers
# to each make a different decision per row.
CARDS = [
    {"start": 0.0, "end": 2.0, "text": "I saw spondum", "avg_logprob": -1.2, "no_speech_prob": 0.05},
    {"start": 2.0, "end": 4.0, "text": "the ship sailed", "avg_logprob": -0.1, "no_speech_prob": 0.05},
    {"start": 4.0, "end": 6.0, "text": "He used hockey", "avg_logprob": -1.4, "no_speech_prob": 0.02},
    {"start": 6.0, "end": 8.0, "text": "grbled nnsense here", "avg_logprob": -1.4, "no_speech_prob": 0.30},
]
CONF_BYTES = json.dumps(CARDS)  # exactly what generate.py's json.dump left in the sidecar

WORDS_BYTES = json.dumps(
    {
        "schema_version": common.WORDS_SCHEMA_VERSION,
        "transcribe_version": common.TRANSCRIBE_VERSION,
        "initial_prompt": "",
        "audio_duration": 8.0,
        "segments": [],
        "words": [{"text": "hi", "start": 0.0, "end": 1.0, "prob": 0.9, "seg": 0}],
    }
)


def _legacy_conf(stem):
    """Seed the conf row with the old sidecar's bytes; return what the old reader saw."""
    artifact_store.put_text(stem, "conf", CONF_BYTES)
    return json.loads(CONF_BYTES)


def test_every_kind_serves_the_document_its_file_held(tmp_path):
    """The transport changed, the documents did not: each reader returns the parsed bytes."""
    stem = str(tmp_path / "ep")

    rows = _legacy_conf(stem)
    assert common.read_conf(stem) == rows
    assert common.conf_rows(stem) == CARDS

    artifact_store.put_text(stem, "words", WORDS_BYTES)
    assert common.read_words(stem) == json.loads(WORDS_BYTES)

    qc_bytes = json.dumps({"counters": {"restore_runs_sent": 3}})
    artifact_store.put_text(stem, "qc", qc_bytes)
    assert artifact_store.get_json(stem, "qc") == json.loads(qc_bytes)
    assert mux._stages_ran(stem, stem + ".eng.dubtitles.ass")["punctuation"] is True


def test_the_cuts_cache_is_served_from_the_store_without_decoding(tmp_path, monkeypatch):
    """The cache exists to keep an ~80 s decode out of every text-tier run; a seeded row
    must answer instead of the decoder."""
    video = tmp_path / "ep.mkv"
    video.write_bytes(b"x")
    stem = os.path.splitext(str(video))[0]
    artifact_store.put_text(
        stem, "cuts", json.dumps({"version": generate.CUTS_VERSION, "threshold": generate.SCENE_THRESHOLD, "cuts": [1.5, 9.25]})
    )

    def boom(*a, **k):
        raise AssertionError("a cached cuts row must not run scene detection")

    monkeypatch.setattr(generate.subprocess, "run", boom)
    assert generate.scene_cuts(str(video)) == [1.5, 9.25]


def test_repair_sends_the_same_targets_the_old_bytes_mark(tmp_path, monkeypatch):
    """repair.py's whole conf consumption is `is_target` over the rows. The cards it hands
    the model from the store row must be the cards the old bytes marked."""
    stem = str(tmp_path / "ep")
    rows = _legacy_conf(stem)
    open(stem + repair.SRT_SUFFIX, "w").close()
    gloss = _glossary.load_dict({"show": "S", "hard_fixes": {"hockey": "haki"}})
    ivals = [(r["start"], r["end"], "the official sub line") for r in rows]
    monkeypatch.setattr(repair, "find_video", lambda s: stem + ".mkv")
    monkeypatch.setattr(repair, "glossary_for", lambda video: gloss)
    monkeypatch.setattr(repair, "reference_tracks", repair_ref_stub.stub(ivals))
    prompts = []
    monkeypatch.setattr(repair, "llm", lambda prompt, model=None: prompts.append(prompt) or "")

    repair.process(stem)

    sent = "\n".join(prompts)
    targets = [r["text"] for r in rows if repair.is_target(r, gloss)]
    others = [r["text"] for r in rows if not repair.is_target(r, gloss)]
    assert targets, "fixture proves nothing if no row is a target"
    for text in targets:
        assert f"ASR line: {text}" in sent, f"a target row was not sent: {text}"
    for text in others:
        assert f"ASR line: {text}" not in sent, f"a non-target row was sent: {text}"


def test_recreate_srt_renders_the_same_text_from_the_store_as_from_the_bytes(tmp_path):
    """The srt is rebuilt from the card list; the rendering must be identical whether the
    list came from json.load(file) or the store row."""
    stem = str(tmp_path / "ep")
    rows = _legacy_conf(stem)
    expected = "".join(
        f"{i}\n{common.ts_srt(r['start'])} --> {common.ts_srt(r['end'])}\n{reflow.wrap_balance(r['text'])}\n\n"
        for i, r in enumerate(rows, 1)
    )

    out = recreate_srt.recreate(stem)

    assert out is not None
    assert out == stem + ".eng.dubtitles.srt"
    assert open(out, encoding="utf-8").read() == expected


def test_review_apply_matches_verdicts_against_the_stored_conf_the_same_way(tmp_path):
    """Eligibility is decided by matching a verdict's original_text against the conf rows.
    The store row must match exactly the rows the old bytes held."""
    stem = str(tmp_path / "ep")
    rows = _legacy_conf(stem)
    store = decisions.record({}, rows[2]["text"], "He used Haki", "accept")

    res = review_apply.apply_episode(stem, store)

    assert res["cards"] == len(rows)
    assert res["changed"] == sum(1 for r in rows if decisions.for_orig(store, r["text"]))


def test_glossary_acquire_reads_the_same_dialogue_text(tmp_path):
    """Tier-B/Tier-C pair mining joins all conf text newline-separated; the store row must
    produce exactly the string the file produced."""
    stem = str(tmp_path / "ep")
    rows = _legacy_conf(stem)
    assert glossary_acquire._conf_text(stem) == "\n".join(r["text"] for r in rows)


def test_the_unresolved_readers_answer_from_the_store_like_from_the_bytes(tmp_path):
    """live_only / card_starts / card_context key off decisions.key(text) and the row's
    start. Same bytes, same answers."""
    stem = str(tmp_path / "ep")
    rows = _legacy_conf(stem)
    live = {decisions.key(r["text"]) for r in rows}
    entries = [
        {"stage": "repair_applied", "original_text": "  I saw   SPONDUM "},
        {"stage": "repair_applied", "original_text": "a line from an older transcript"},
    ]

    assert [e["original_text"] for e in unresolved.live_only(stem, entries)] == [entries[0]["original_text"]]
    assert unresolved.card_starts(stem) == {
        decisions.key(r["text"]): [r["start"]] for r in rows if decisions.key(r["text"]) in live
    }
    ctx = unresolved.card_context(stem, width=1)
    assert ctx[decisions.key("the ship sailed")][0]["before"] == ["I saw spondum"]
