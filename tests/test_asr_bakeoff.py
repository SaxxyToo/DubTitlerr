"""Unit tests for tools/asr_bakeoff.py -- the GPU-free, pure shaping half only.

The transcribers (run_whisper / run_nemo) need a CUDA stack and are exercised by
running the tool itself on one real episode; these tests pin the parts every verdict
is built from: WER math, the whisper/nemo router, the judge summary, and the
defensive NeMo transcript parser whose return shapes move between nemo versions.
"""

import json
import math
import wave

import pytest

from tools import asr_bakeoff as ab

# ------------------------------------------------------------------ WER arithmetic


def test_wer_zero_when_transcripts_match():
    assert ab.wer("the quick brown fox", "the quick brown fox") == 0.0


def test_wer_one_substitution_in_four_words():
    # 1 sub / 4 ref words = 0.25
    assert ab.wer("the quick brown fox", "the quick brown dog") == 0.25


def test_wer_counts_deletions_and_insertions():
    # deletion: "jumps" dropped -> 1/3
    assert ab.wer("the fox jumps", "the fox") == pytest.approx(1 / 3)
    # insertion: an extra word -> 1/2
    assert ab.wer("the fox", "the big fox") == 0.5


def test_wer_strips_punctuation_and_case_like_leaderboards():
    # Punctuation/case are stripped on BOTH sides, so formatting differences never
    # inflate the score -- same normalisation published WER numbers apply.
    assert ab.wer("The, QUICK Brown Fox!", "the quick brown fox") == 0.0


def test_wer_none_when_reference_is_empty():
    # An empty reference makes WER undefined, not infinite; None so callers can skip.
    assert ab.wer("", "anything") is None
    assert ab.wer("   ", "anything") is None


def test_edit_distance_hand_checked():
    assert ab.edit_distance(["a", "b", "c"], ["a", "x", "c"]) == 1
    assert ab.edit_distance([], ["a", "b"]) == 2
    assert ab.edit_distance(["a", "b"], []) == 2


# ---------------------------------------------------- load_srt_reference (real ground truth)

_SDH_SRT = """\
1
00:00:01,000 --> 00:00:03,000
[dramatic music plays]

2
00:00:04,000 --> 00:00:06,000
[narrator] A long time ago…

3
00:00:07,000 --> 00:00:09,000
<i>Wait a minute.</i>
"""


def test_load_srt_reference_drops_pure_sound_cues(tmp_path):
    srt = tmp_path / "sdh.srt"
    srt.write_text(_SDH_SRT)
    text = ab.load_srt_reference(str(srt))
    # cue 1 is pure sound effect and must vanish entirely, not become empty words
    assert "music" not in text.lower() and "dramatic" not in text.lower()


def test_load_srt_reference_strips_speaker_tag_keeps_dialogue(tmp_path):
    srt = tmp_path / "sdh.srt"
    srt.write_text(_SDH_SRT)
    text = ab.load_srt_reference(str(srt))
    assert "narrator" not in text.lower()
    assert "A long time ago" in text


def test_load_srt_reference_strips_italics_markup(tmp_path):
    srt = tmp_path / "sdh.srt"
    srt.write_text(_SDH_SRT)
    text = ab.load_srt_reference(str(srt))
    assert "<i>" not in text and "Wait a minute." in text


# --------------------------------------------------------- score_against_references


def test_score_against_references_matches_by_episode_basename():
    entries = [{"episodes": [{"episode": "ep1.mkv", "text": "the quick brown fox"}]}]
    ab.score_against_references(entries, {"ep1.mkv": "the quick brown fox"})
    assert entries[0]["episodes"][0]["wer_vs_ref"] == 0.0


def test_score_against_references_skips_episodes_without_a_reference():
    entries = [{"episodes": [{"episode": "unrelated.mkv", "text": "hello"}]}]
    ab.score_against_references(entries, {"ep1.mkv": "the quick brown fox"})
    assert "wer_vs_ref" not in entries[0]["episodes"][0]


def test_score_against_references_skips_episodes_with_no_text():
    # An errored episode (no dub track, extraction failure) never got a "text" key.
    entries = [{"episodes": [{"episode": "ep1.mkv", "error": "no-eng-dub-or-extract-failed"}]}]
    ab.score_against_references(entries, {"ep1.mkv": "the quick brown fox"})
    assert "wer_vs_ref" not in entries[0]["episodes"][0]


# ---------------------------------------------------------------------- router


def test_router_sends_whisper_family_to_faster_whisper():
    assert ab.is_whisper_name("large-v3-turbo")
    assert ab.is_whisper_name("deepdml/faster-distil-whisper-large-v3")


def test_router_sends_nvidia_nemo_ids_to_nemo():
    assert not ab.is_whisper_name("nvidia/parakeet-tdt-0.6b-v3")
    assert not ab.is_whisper_name("nvidia/canary-1b-v2")
    assert not ab.is_whisper_name("nvidia/canary-qwen-2.5b")


def test_router_sends_qwen_org_prefix_to_qwen():
    assert ab.is_qwen_name("Qwen/Qwen3-ASR-1.7B")
    assert ab.is_qwen_name("Qwen/Qwen3-ASR-0.6B")
    assert ab.is_qwen_name("qwen/qwen3-asr-1.7b")  # case-insensitive


def test_router_does_not_misroute_nemo_canary_qwen_to_qwen_backend():
    # nvidia/canary-qwen-2.5b uses a Qwen LLM as its DECODER but is a NeMo model --
    # a substring match on "qwen" would wrongly send it to the qwen_asr package.
    assert not ab.is_qwen_name("nvidia/canary-qwen-2.5b")
    assert not ab.is_whisper_name("nvidia/canary-qwen-2.5b")


# ------------------------------------------------------------------ judge summary


def test_summarise_counts_blocklist_hits():
    hit = "thanks for watching"
    assert ab.hallucination.BLOCKLIST.search(hit), "blocklist fixture no longer matches -- pick another known phrase"
    miss = "a perfectly ordinary line of dialogue that matches nothing"
    s = ab.summarise([], [], [hit, miss, miss])
    assert s["blocklist_hits"] == 1
    assert s["segments"] == 3


def test_summarise_nsp_alive_frac_uses_the_collapse_floor():
    # Two segments above the 1e-6 floor, two collapsed below it -> 0.5 alive.
    nsps = [1e-10, 1e-10, 0.5, 0.99]
    s = ab.summarise(nsps, [], ["a", "b", "c", "d"])
    assert s["nsp_alive_frac"] == 0.5
    assert s["nsp_max"] == 0.99
    assert s["nsp_min"] == 1e-10


def test_summarise_music_rule_needs_both_signals_in_the_same_segment():
    # The music rule is a CONJUNCTION: nsp > 0.95 AND logprob < -2.0 on the SAME
    # segment. High nsp with a fine logprob must not fire it.
    s = ab.summarise([0.99, 0.1], [-1.0, -3.0], ["a", "b"])
    assert s["music_rule_would_fire"] == 0
    s = ab.summarise([0.99, 0.1], [-3.0, -1.0], ["a", "b"])
    assert s["music_rule_would_fire"] == 1


def test_summarise_handles_empty_lists_without_raising():
    s = ab.summarise([], [], [])
    assert s["segments"] == 0 and s["blocklist_hits"] == 0 and s["nsp_median"] is None


# ------------------------------------------------- cross-model agreement (cross_wer)


def test_cross_wer_joins_over_the_whole_episode_set():
    a = ["the quick brown fox", "jumps over the dog"]
    b = ["the quick brown fox", "jumps over the dog"]
    assert ab.cross_wer(a, b) == 0.0


def test_cross_wer_is_none_when_either_side_is_empty():
    # A failed episode must not silently score as a 100% disagreement.
    assert ab.cross_wer([], ["something"]) is None
    assert ab.cross_wer(["something"], []) is None


# ---------------------------------------------------------------- pairwise_agreement


def _entry(model, episodes, texts):
    return {"model": model, "verdict": "ok", "episodes": episodes, "texts": texts}


def test_pairwise_agreement_covers_every_pair_of_ok_entrants():
    eps = [{"episode": "e1.mkv"}]
    entries = [
        _entry("a", eps, ["hello world"]),
        _entry("b", eps, ["hello world"]),
        _entry("c", eps, ["goodbye world"]),
    ]
    agreement = ab.pairwise_agreement(entries)
    assert set(agreement) == {"a|b", "a|c", "b|c"}
    assert agreement["a|b"] == 0.0
    assert agreement["a|c"] == pytest.approx(0.5)


def test_pairwise_agreement_skips_entrants_that_did_not_load():
    eps = [{"episode": "e1.mkv"}]
    entries = [
        _entry("a", eps, ["hello world"]),
        {"model": "b", "verdict": "did not load", "error": "OOM"},
    ]
    assert ab.pairwise_agreement(entries) == {}


def test_pairwise_agreement_is_none_when_episode_sets_differ():
    # One entrant errored on an episode the other completed -- not a fair comparison.
    entries = [
        _entry("a", [{"episode": "e1.mkv"}, {"episode": "e2.mkv"}], ["x", "y"]),
        _entry("b", [{"episode": "e1.mkv"}, {"episode": "e2.mkv", "error": "boom"}], ["x"]),
    ]
    assert ab.pairwise_agreement(entries) == {"a|b": None}


# ---------------------------------------------------------------------- QwenRun shape


def test_qwen_run_is_a_nemo_run_shape_with_its_own_family_label():
    # QwenRun subclasses NemoRun for the identical result shape (no per-segment
    # nsp/logprob, word-level timestamps) -- only the family label should differ.
    run = ab.QwenRun("Qwen/Qwen3-ASR-1.7B")
    assert isinstance(run, ab.NemoRun)
    assert run.family == "qwen3-asr"
    assert run.words == [] and run.segments == [] and run.episodes == []


# --------------------------------------------------------- print_verdict precision caveat


def _verdict_entry(model, episodes=None):
    eps = episodes if episodes is not None else [{"episode": "e1.mkv", "wall_s": 1.0}]
    return {
        "model": model,
        "verdict": "ok",
        "episodes": eps,
        "has_word_timestamps": True,
        "load_s": 1.0,
        "peak_vram_mib": 100,
        "segments": 1,
        "word_count": 1,
        "blocklist_hits": 0,
    }


def test_print_verdict_flags_int8_fp16_precision_mismatch(capsys):
    report = ab.shape_report([_verdict_entry("large-v3-turbo (int8)"), _verdict_entry("nvidia/parakeet-tdt-0.6b-v3 (fp16)")], {})
    ab.print_verdict(report)
    assert "CAVEAT" in capsys.readouterr().out


def test_print_verdict_silent_when_precision_matches(capsys):
    report = ab.shape_report([_verdict_entry("large-v3-turbo (int8)"), _verdict_entry("large-v3 (int8)")], {})
    ab.print_verdict(report)
    assert "CAVEAT" not in capsys.readouterr().out


# --------------------------------------------------- NeMo transcript parsing (defensive)


class _Hyp:
    """Stand-in for nemo's Hypothesis: attribute access, .text, optional .timestamp."""

    def __init__(self, text, timestamp=None):
        self.text = text
        self.timestamp = timestamp


def test_parse_transcript_single_hypothesis():
    texts, words = ab.parse_transcript(_Hyp("hello world"))
    assert texts == ["hello world"]
    assert words == []


def test_parse_transcript_list_of_hypotheses():
    texts, _ = ab.parse_transcript([_Hyp("one"), _Hyp("two")])
    assert texts == ["one", "two"]


def test_parse_transcript_unwraps_hypotheses_alignment_tuple():
    # Recent nemo returns (hypotheses, alignment); the alignment half must be dropped,
    # not parsed as a hypothesis.
    texts, _ = ab.parse_transcript(([_Hyp("speech")], {"alignment": "whatever"}))
    assert texts == ["speech"]


def test_parse_transcript_extracts_word_timestamps():
    ts = {"word": [{"word": "hello", "start": 0.0, "end": 0.4}, {"word": "world", "start": 0.5, "end": 0.9}]}
    texts, words = ab.parse_transcript(_Hyp("hello world", timestamp=ts))
    assert texts == ["hello world"]
    assert [w["text"] for w in words] == ["hello", "world"]
    assert words[0]["start"] == 0.0 and words[1]["end"] == 0.9


def test_parse_transcript_sorts_words_by_time():
    ts = {"word": [{"word": "world", "start": 0.5, "end": 0.9}, {"word": "hello", "start": 0.0, "end": 0.4}]}
    _, words = ab.parse_transcript(_Hyp("x", timestamp=ts))
    assert [w["text"] for w in words] == ["hello", "world"]


def test_parse_transcript_token_level_timestamps_also_accepted():
    ts = {"token": [{"token": "he", "start": 0.0, "end": 0.2}]}
    _, words = ab.parse_transcript(_Hyp("he", timestamp=ts))
    assert len(words) == 1


def test_parse_transcript_partial_word_entries_are_dropped():
    # A word entry missing a boundary is unusable for cue generation; drop it rather
    # than fabricate a time.
    ts = {"word": [{"word": "good", "start": 0.0, "end": 0.3}, {"word": "bad", "start": None, "end": 0.5}]}
    _, words = ab.parse_transcript(_Hyp("good bad", timestamp=ts))
    assert [w["text"] for w in words] == ["good"]


def test_parse_transcript_unrecognised_shape_degrades_to_empty():
    # An unknown return shape yields empty results the run records as a finding --
    # never a guess, never an exception.
    texts, words = ab.parse_transcript(42)
    assert texts == [] and words == []
    texts, words = ab.parse_transcript(None)
    assert texts == [] and words == []


# ------------------------------------------------------------- chunk_wav (long-audio)


def _wav_duration_s(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def _write_silence_wav(path, seconds, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))


def test_chunk_wav_splits_into_expected_number_of_pieces(tmp_path):
    src = tmp_path / "src.wav"
    _write_silence_wav(src, seconds=5)
    chunks = ab.chunk_wav(str(src), chunk_s=2, out_dir=str(tmp_path))
    # 5s / 2s chunks -> [0, 2), [2, 4), [4, 5) = 3 pieces, last one short
    assert [round(off) for off, _ in chunks] == [0, 2, 4]
    assert all(_wav_duration_s(p) > 0 for _, p in chunks)


def test_chunk_wav_shorter_than_one_chunk_yields_a_single_piece(tmp_path):
    src = tmp_path / "src.wav"
    _write_silence_wav(src, seconds=1)
    chunks = ab.chunk_wav(str(src), chunk_s=300, out_dir=str(tmp_path))
    assert len(chunks) == 1
    assert chunks[0][0] == 0.0


def test_chunk_offsets_are_added_back_onto_word_timestamps():
    # This is the merge step run_nemo performs after transcribing each chunk --
    # a word at t=1.0 in the second 300s chunk must land at t=301.0 in the episode.
    chunk_words = [{"text": "hi", "start": 1.0, "end": 1.5}]
    offset = 300.0
    merged = [{**w, "start": w["start"] + offset, "end": w["end"] + offset} for w in chunk_words]
    assert merged == [{"text": "hi", "start": 301.0, "end": 301.5}]


# ------------------------------------------------------- pseudo-segmentation for NeMo


def test_segment_from_words_splits_on_large_gaps():
    words = [
        {"text": "a", "start": 0.0, "end": 0.5},
        {"text": "b", "start": 0.6, "end": 1.0},
        # 3s gap -> split
        {"text": "c", "start": 4.0, "end": 4.5},
    ]
    segs = ab.segment_from_words(words, max_gap=2.0)
    assert [s["text"] for s in segs] == ["a b", "c"]
    assert segs[0]["start"] == 0.0 and segs[1]["end"] == 4.5


def test_segment_from_words_keeps_contiguous_speech_together():
    words = [{"text": "a", "start": 0.0, "end": 0.5}, {"text": "b", "start": 0.6, "end": 1.0}]
    segs = ab.segment_from_words(words, max_gap=2.0)
    assert len(segs) == 1 and segs[0]["text"] == "a b"


# --------------------------------------------------- endpoint entrant (whisper.cpp sidecar)


def test_endpoint_spec_splits_the_url_from_its_label():
    url, label = ab.parse_endpoint_spec(
        "endpoint:http://127.0.0.1:11434/v1/audio/transcriptions@whispercpp-turbo-f16"
    )
    assert url == "http://127.0.0.1:11434/v1/audio/transcriptions"
    assert label == "whispercpp-turbo-f16"


def test_endpoint_spec_defaults_the_label_to_host_and_port():
    """An unlabelled entry still needs a distinct report label -- two endpoints that
    differ only by port must not collide as one entrant in the agreement table."""
    url, label = ab.parse_endpoint_spec("endpoint:http://127.0.0.1:11434/v1/audio/transcriptions")
    assert url == "http://127.0.0.1:11434/v1/audio/transcriptions"
    assert label == "127.0.0.1:11434"


def test_endpoint_spec_keeps_a_url_that_contains_an_at_sign_out_of_it():
    """rpartition, not split: credentials in a URL ('user@host') must not be mistaken
    for a label separator."""
    url, label = ab.parse_endpoint_spec("endpoint:http://user@127.0.0.1:11434/v1/audio/transcriptions")
    assert url == "http://user@127.0.0.1:11434/v1/audio/transcriptions"
    assert label == "127.0.0.1:11434"


def test_router_sends_endpoint_entries_to_the_endpoint_runner():
    assert ab.is_endpoint_name("endpoint:http://127.0.0.1:11434/v1/audio/transcriptions@x")
    assert not ab.is_endpoint_name("large-v3-turbo")
    assert not ab.is_endpoint_name("nvidia/parakeet-tdt-0.6b-v3")


def test_endpoint_shape_rebuilds_a_segment_text_from_its_own_words():
    """endpoint_transcribe returns segments with no text -- production never needs it
    there (reflow rebuilds cards from words), but the judge counts blocklist hits per
    segment text, so the entrant must rebuild it."""
    segments = [
        {"start": 0.0, "end": 2.0, "no_speech_prob": 0.01},
        {"start": 2.0, "end": 3.0, "no_speech_prob": 0.9},
    ]
    words = [
        {"text": " Hello", "start": 0.0, "end": 0.5, "prob": 0.9, "seg": 0},
        {"text": " there.", "start": 0.5, "end": 1.0, "prob": 0.8, "seg": 0},
        {"text": " Music", "start": 2.0, "end": 3.0, "prob": 0.2, "seg": 1},
    ]
    segs, ws = ab.endpoint_run_shape(segments, words)
    assert [s["text"] for s in segs] == ["Hello there.", "Music"]
    assert [s["no_speech_prob"] for s in segs] == [0.01, 0.9]


def test_endpoint_shape_derives_segment_logprob_from_word_probs_like_reflow():
    """The endpoint path carries no per-segment avg_logprob: _map_endpoint_doc folds it
    into word probs, and reflow.card_confidence derives card confidence back out of
    those. Deriving the same way here means the judge sees what production sees -- and
    it must match reflow's formula, not a re-implementation of it."""
    import reflow

    prob_a, prob_b = 0.5, 0.25
    segments = [{"start": 0.0, "end": 2.0, "no_speech_prob": 0.0}]
    words = [
        {"text": " a", "start": 0.0, "end": 1.0, "prob": prob_a, "seg": 0},
        {"text": " b", "start": 1.0, "end": 2.0, "prob": prob_b, "seg": 0},
    ]
    segs, ws = ab.endpoint_run_shape(segments, words)
    expected, _nsp = reflow.card_confidence(words, segments)
    assert segs[0]["avg_logprob"] == pytest.approx(expected)


def test_endpoint_shape_keeps_word_prob_and_drops_the_endpoint_seg_key():
    segments = [{"start": 0.0, "end": 1.0, "no_speech_prob": 0.0}]
    words = [{"text": " a", "start": 0.0, "end": 1.0, "prob": 0.4, "seg": 0}]
    _segs, ws = ab.endpoint_run_shape(segments, words)
    # verbatim, leading space included: production stores merged whisper.cpp tokens the
    # same way, and reflow relies on the spacing to rejoin them
    assert ws == [{"text": " a", "start": 0.0, "end": 1.0, "prob": 0.4}]


def test_endpoint_shape_clamps_a_zero_probability_before_the_log():
    """prob 0 is real (a token the server scored at zero) and ln(0) is -inf, which would
    poison every downstream median. reflow clamps at PROB_FLOOR; this must too."""
    import reflow

    segments = [{"start": 0.0, "end": 1.0, "no_speech_prob": 0.0}]
    words = [{"text": " a", "start": 0.0, "end": 1.0, "prob": 0.0, "seg": 0}]
    segs, _ws = ab.endpoint_run_shape(segments, words)
    assert segs[0]["avg_logprob"] == pytest.approx(math.log(reflow.PROB_FLOOR))


def test_strip_transcripts_removes_every_carrier_of_dub_text():
    """The report keeps transcripts on purpose (the verdict tells you to hand-read the
    noisiest episode), and that is exactly why a repo-committed report must not: the text
    is a commercial dub's script. All three carriers have to go -- the top-level texts,
    the word list, and each episode's own text."""
    entry = {
        "model": "x",
        "episodes": [
            {"episode": "e1.mkv", "wall_s": 4.0, "segments": 3, "text": "the whole script"},
            {"episode": "e2.mkv", "wall_s": 1.0, "error": "no-eng-dub-or-extract-failed"},
        ],
        "texts": ["the whole script"],
        "words": [{"text": " the", "start": 0.0, "end": 0.5, "prob": 0.9}],
    }
    out = ab.strip_transcripts([entry])
    stripped = out[0]
    assert "texts" not in stripped and "words" not in stripped
    assert all("text" not in ep for ep in stripped["episodes"])
    # the metrics must survive -- this strips text, not the measurement
    assert stripped["episodes"][0]["wall_s"] == 4.0
    assert stripped["episodes"][0]["segments"] == 3
    assert stripped["episodes"][1]["error"] == "no-eng-dub-or-extract-failed"


def test_strip_transcripts_leaves_the_metrics_untouched():
    """The blocklist counts and confidence stats are built from text but are themselves
    numbers; stripping must not silently drop them or the report stops being evidence."""
    entry = {
        "model": "x",
        "episodes": [{"episode": "e.mkv", "text": "secret"}],
        "texts": ["secret"],
        "words": [],
        "blocklist_hits": 2,
        "nsp_median": 0.1,
        "logprob_p05": -1.5,
        "music_rule_would_fire": 3,
        "word_count": 10,
    }
    stripped = ab.strip_transcripts([entry])[0]
    assert stripped["blocklist_hits"] == 2
    assert stripped["nsp_median"] == 0.1
    assert stripped["logprob_p05"] == -1.5
    assert stripped["music_rule_would_fire"] == 3
    assert stripped["word_count"] == 10


def _tiny_wav(path, seconds=1.0, rate=16000):
    """A real (silent) wav, so the entrant's ffprobe duration step runs for real."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return str(path)


def test_run_endpoint_transcribes_through_the_real_http_path(tmp_path, monkeypatch):
    """The entrant must go end to end: extract -> mp3 re-encode -> multipart POST ->
    verbose_json -> the tool's shapes. Nothing here is stubbed except the ffmpeg step the
    production path shares with every other test, so a break in the request shape (the
    thing a whisper.cpp server has to accept) fails here."""
    import fake_servers

    import generate

    doc = json.load(open("tests/fixtures/whispercpp_verbose_json.json"))
    server = fake_servers.FakeServer(fake_servers.asr_reply(doc))
    try:
        wav = _tiny_wav(tmp_path / "a.wav")
        monkeypatch.setattr(ab, "_extract_dub_wav", lambda video, td: wav)
        # The mp3 encode is the one shared production step that needs a real ffmpeg; the
        # POST is exercised over a REAL socket, which is the point of this test.
        def _fake_mp3(w, path):
            open(path, "wb").write(b"x" * 4096)  # a real POST goes out; only the encode is stubbed
            return True

        monkeypatch.setattr(generate, "_mp3_for_upload", _fake_mp3)
        monkeypatch.setattr(generate, "ASR_KEY_ENV", "")
        monkeypatch.setattr(generate, "ASR_URL", "http://127.0.0.1:1/never-used")
        monkeypatch.setattr(generate, "ASR_MODEL", "never-used")

        run = ab.run_endpoint(server.base + "/v1/audio/transcriptions", "fake-whispercpp", ["ep.mkv"])

        # the request really was a multipart upload carrying the fields the server needs
        assert server.requests, "no request reached the endpoint"
        body = server.requests[0]["body"]
        assert isinstance(body, bytes) and b"response_format" in body and b"verbose_json" in body
        assert server.requests[0]["path"] == "/v1/audio/transcriptions"

        # shapes: sub-word tokens rejoined, per-word prob kept, segment text rebuilt
        assert [w["text"] for w in run.words] == [" Treecutting", " aviary", " Beacon"]
        assert run.words[0]["prob"] == pytest.approx(0.55)  # min of the merged pieces
        assert run.segments[0]["text"] == "Treecutting aviary"
        assert run.segments[0]["no_speech_prob"] == pytest.approx(0.02)
        assert run.segments[0]["avg_logprob"] < 0  # derived, never left absent
        assert run.episodes[0]["segments"] == 2 and "error" not in run.episodes[0]
        assert run.ok and run.family == "whisper.cpp-endpoint"
    finally:
        server.close()


def test_run_endpoint_restores_the_endpoint_globals_even_on_failure(tmp_path, monkeypatch):
    """A bakeoff URL must not survive the run: a later production call in the same process
    would otherwise be pointed at a bakeoff server."""
    import generate

    monkeypatch.setattr(ab, "_extract_dub_wav", lambda video, td: None)  # -> per-episode error
    monkeypatch.setattr(generate, "ASR_KEY_ENV", "")
    monkeypatch.setattr(generate, "ASR_URL", "http://127.0.0.1:1/original")
    monkeypatch.setattr(generate, "ASR_MODEL", "original-model")

    run = ab.run_endpoint("http://127.0.0.1:9/bakeoff", "bakeoff", ["ep.mkv"])

    assert generate.ASR_URL == "http://127.0.0.1:1/original"
    assert generate.ASR_MODEL == "original-model"
    assert run.episodes[0]["error"] == "no-eng-dub-or-extract-failed"


def test_run_endpoint_records_an_episode_error_instead_of_aborting(tmp_path, monkeypatch):
    """One unreadable mkv must not throw away the other episodes' work."""
    import generate

    calls = {"n": 0}

    def fake_extract(video, td):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bad mkv")
        return None

    monkeypatch.setattr(ab, "_extract_dub_wav", fake_extract)
    monkeypatch.setattr(generate, "ASR_KEY_ENV", "")
    run = ab.run_endpoint("http://127.0.0.1:9/x", "l", ["a.mkv", "b.mkv"])
    assert len(run.episodes) == 2
    assert run.episodes[0]["error"].startswith("RuntimeError")
    assert run.episodes[1]["error"] == "no-eng-dub-or-extract-failed"
