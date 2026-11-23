#!/usr/bin/env python3
"""Gold dubtitle GENERATION — transcribe an anime's English-dub audio into a
time-coded subtitle, biased toward correct franchise spellings and with per-segment
confidence captured for a downstream repair pass.

Runs in the subgen CUDA image (mccloud/subgen:2026.06.2) so it inherits the exact
faster-whisper 1.2.1 / ctranslate2 4.8.0 stack that already works on the 1060
(Pascal) + driver 550 — no new CUDA surface. Only extra dep is none (uses ffmpeg +
faster_whisper already present).

Per video:
  1. pick the English audio stream (by language tag) and extract 16k mono wav,
  2. faster-whisper (large-v3-turbo, or large-v3 on 6GB+ cards -- see WHISPER_MODEL below),
     task=transcribe (English dub -> English text),
     word_timestamps + vad_filter + initial_prompt glossary,
  3. conservative name-correction sweep against the franchise glossary,
  4. write <stem>.eng.dubtitles.srt and the conf card list (store kind ``conf``; segment
     confidences: start,end,avg_logprob,no_speech_prob) for the repair stage.

Usage:
  python3 generate.py /media/.../Episode.mkv [more.mkv ...]   # explicit files
  python3 generate.py --root "/media/Anime Library/One Pace/Season 15"  # walk dir

Env:
  WHISPER_MODEL   default large-v3-turbo  (in the container this is set FOR you by the image --
                  Dockerfile.builder bakes a model and exports its name as this var, so the
                  default below only applies to a bare checkout. See the MODEL comment.)
  COMPUTE_TYPE    default int8  (Pascal-friendly, fits 6GB; try float16 for max quality)
  MODEL_DIR       default /subgen/models  (reuse subgen's downloaded model)
  WHISPER_AUDIO_FILTER  default highpass=f=80,compand=... (V2 A8; "" disables it, the
                  pre-A8 ffmpeg command)
  FFMPEG_TIMEOUT  default 1800  (seconds; the wav decode in extract_wav. Raised from
                  600 -- a 556 MB episode failed at 600s on the measured NAS read
                  rate. Raise it further on a slower mount; a timeout here fails
                  the episode)
  FFPROBE_TIMEOUT default 60   (seconds; both ffprobe calls -- audio-stream pick and
                  duration. The stream pick reads the same remote file as the decode)
  MEDIA_UID/GID   default 1000/100
  GLOSSARY_DIR    default /config/glossaries  (V2 C1: where <show>.lastrun.json is written,
                  same dir mine_glossary.py/repair.py use for the show's glossary itself)
Built with help of Claude (Anthropic).
"""

import bisect
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time

try:  # local ASR only: the lite (endpoint-backed) image ships no torch/ctranslate2
    from faster_whisper import WhisperModel
except ImportError:  # pragma: no cover - endpoint-only deployments
    WhisperModel = None

from dubtitlerr.core import artifact_store, common, media_select
from dubtitlerr.core.common import (
    STAMP_SUFFIX,
    TRANSCRIBE_VERSION,
    VIDEO_EXTS,
    WORDS_SCHEMA_VERSION,
    atomic_write,
    load_extras,
    out_for,
    read_stamp,
    read_words,
    stale_tiers,
    stale_version_stamp,
    stamp_valid,
    ts_srt,
)
from dubtitlerr.glossary import glossary
from dubtitlerr.pipeline import boilerplate, hallucination, ordering, punctuation, qc, reflow

EXTRA_DIRS = load_extras()  # data/extras.txt is the source (see common.load_extras)
# V2 C1: where per-show run summaries (<show>.lastrun.json) live -- same GLOSSARY_DIR
# convention as mine_glossary.py/repair.py, not the per-run GLOSSARY_FILE.
GLOSS_DIR = os.environ.get("GLOSSARY_DIR", "/config/glossaries")

# V2 A9 resolved, and the answer is per-GPU rather than global -- so this is a fallback,
# not really "the" model. Dockerfile.builder's WHISPER_MODEL build-arg bakes one model and
# exports the same name as a container ENV, which wins over this default; the value here
# only decides what a bare checkout (or a container run with the ENV cleared) loads.
#
# large-v3-turbo is the fallback because it fits EVERY card this runs on at the default
# beam_size=7, and because it is what the production image has been built with since the
# 1050ti swap -- the default now names the artifact that actually ships. large-v3 does not
# fit the 3500g node's 4GB 1050ti: benched on a real episode it OOM'd at beam 7 and only
# fit forced down to greedy, where it came out WORSE (flagged=76, over_cps=111) than turbo
# at the full beam (flagged=35, over_cps=98, peak 1405 MiB). It still fits the 6GB 1060 at
# beam 7, which is what the build-arg is for. Turbo is safe to default to because its known
# quality regression is on *translation*, and REQUIRE_ENG=1 means this pipeline only ever
# transcribes English audio to English text -- it never translates.
#
# NOT a TRANSCRIBE_VERSION bump. common.py names the whisper model as decoder-affecting and
# says nothing detects it mechanically -- but the library's 576 v4 stamps were produced by
# THIS decoder (the image has been turbo-built since the swap), so they are transcribe-fresh
# already. Bumping would burn ~2 GPU-days to record a bookkeeping change, which is the exact
# trade common.py's 4/7 adoption note declines. A downstream install that built with the old
# large-v3 default and then rebuilds DOES change decoder without a bump; the drift test in
# tests/test_dockerfile_copy.py cannot see that, and it is recorded here as the known gap.
MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
COMPUTE = os.environ.get("COMPUTE_TYPE", "int8")
# cuda is what the image ships for; cpu and auto exist so a card-less test box can run the same code (settings: ASR_DEVICE).
ASR_DEVICE = os.environ.get("ASR_DEVICE", "cuda")
MODEL_DIR = os.environ.get("MODEL_DIR", "/subgen/models")

# --- ASR backend -------------------------------------------------------------------
# "local"    : faster-whisper in-process; the model is baked into the full image.
# "endpoint" : an OpenAI-compatible /audio/transcriptions endpoint (OpenRouter, a
#              llama-swap whisper, the Nous gateway ...). No model, no torch, no GPU --
#              this is what the lite image runs.
ASR_BACKEND = os.environ.get("ASR_BACKEND", "local").strip().lower()
ASR_URL = os.environ.get("ASR_URL", "https://openrouter.ai/api/v1/audio/transcriptions").strip()
ASR_MODEL = os.environ.get("ASR_MODEL", "openai/whisper-1").strip()
ASR_KEY_ENV = os.environ.get("ASR_KEY_ENV", "OPENROUTER_API_KEY").strip()
ASR_TIMEOUT = int(os.environ.get("ASR_TIMEOUT", "900"))
ENDPOINT_MAX_ATTEMPTS = int(os.environ.get("ENDPOINT_MAX_ATTEMPTS", "3"))
FAIL_KIND = "fail"
CRASH_KIND = "crash"
NO_SPEECH_KIND = "no-speech"
ASR_REFERER = os.environ.get("ASR_REFERER", "https://github.com/SaxxyToo/dubtitlerr").strip()


class EndpointConfigError(Exception):
    """The endpoint backend is configured so that it must not be called (a key bound for a
    clear-text URL). Neither a GPU failure nor an endpoint failure: it must not poison an
    episode or count toward retiring one as no-speech."""


class EndpointNoSpeech(Exception):
    """A well-formed transcription with nothing in it: the piece of audio held no speech.

    Raised only for a response that IS a transcription document (it carries "text" or
    "segments") and is empty -- never for an error body, which stays an EndpointAsrError.
    A chunked file skips such a piece; a whole file still fails (see endpoint_transcribe)."""


class EndpointAsrError(Exception):
    """An endpoint transcription failed in a way that must not poison the episode.

    Deliberately NOT a RuntimeError: the caller treats RuntimeError as a CUDA-context
    failure -- it keeps the fail poison row (skip-prior-crash on every later
    sweep) and exits the process. An endpoint answering 200 with an error body, a
    truncated upload or a stretch with no speech is a per-episode retry, not a broken
    GPU context. Raising this takes the non-poison path: marker cleared, crash record
    written, episode retried on the next sweep.
    """


# V2 A8: optional pre-transcription audio cleanup (default highpass + dynamic-range
# compand, tuned for noisy/quiet anime dub tracks). Empty string ("") disables it
# entirely (the old, pre-A8 ffmpeg command) -- set WHISPER_AUDIO_FILTER="" to opt out.
AUDIO_FILTER = os.environ.get(
    "WHISPER_AUDIO_FILTER", "highpass=f=80,compand=attacks=0.001:decays=0.2:points=-80/-80|-30/-15|0/-3|20/-3"
)
# ffmpeg/ffprobe wall-clock limits. Both were literals until a full decode timed out
# on a slow NFS mount with no way to raise the ceiling short of editing this file. The
# probe budget is the tighter of the two and reads the SAME remote file the decode does,
# so it is overridable for the same reason.
FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "1800"))
FFPROBE_TIMEOUT = int(os.environ.get("FFPROBE_TIMEOUT", "60"))
AUDIO_START_THRESHOLD = 0.05  # ignore codec pre-skip and sub-frame timestamp noise
UID = int(os.environ.get("MEDIA_UID", "1000"))
GID = int(os.environ.get("MEDIA_GID", "100"))
SUFFIX = ".eng.dubtitles.srt"
WMODEL = None  # the WhisperModel, lazily loaded in main() once there's work to do

# --- Per-show glossary (optional) ---------------------------------------------------
# Name correction is OPT-IN per show (GLOSSARY_FILE), so One Piece's spellings can never
# leak onto another show. The tiered correction itself lives in glossary.py (C1). With no
# file, GLOSS is empty (correct() is a no-op) and the prompt is a neutral one from SHOW_NAME.
GLOSS = glossary.load("")
INITIAL_PROMPT = ""


def load_glossary():
    global GLOSS, INITIAL_PROMPT
    show = os.environ.get("SHOW_NAME", "")
    gloss_file = os.environ.get("GLOSSARY_FILE", "")
    GLOSS = glossary.load(gloss_file)
    show = show or GLOSS.get("show", "")
    if not show and gloss_file:
        # Glossaries are named <Show>.json, so the file name is the show's own name. A run
        # that reaches here without SHOW_NAME (a hand-invoked generate.py, a request that
        # lost the variable) used to transcribe with no glossary at all and report
        # names=0 — the one failure mode that quietly ships wrong names, which is the
        # defect the glossary exists to prevent.
        show = os.path.splitext(os.path.basename(gloss_file))[0]
    # ONE derivation, in glossary.prompt_for: the text tier compares a stored prompt
    # against it to decide whether a glossary edit needs the GPU. A second copy here
    # would drift, and the drift would read as "the prompt changed" on every episode.
    INITIAL_PROMPT = glossary.prompt_for(GLOSS, show)
    print(
        f"glossary: show={show!r} names={len(GLOSS['names'])} "
        f"fixes={len(GLOSS['token_fixes']) + len(GLOSS['phrase_fixes'])} "
        f"prompt={'custom' if GLOSS['initial_prompt'] else 'neutral'}",
        flush=True,
    )


def decoder_identity() -> dict:
    """What today's process would use to transcribe."""
    if ASR_BACKEND == "endpoint":
        # backend + model only: the URL is transport, not identity -- repointing ASR_URL
        # (same model, different address) must not invalidate sidecars (owner decision,
        # 2026-11-20: keep the semantic name, drop the address).
        return {
            "backend": "endpoint",
            "model": ASR_MODEL,
            "initial_prompt": INITIAL_PROMPT,
        }
    # Deliberately unchanged from before the endpoint backend existed: this dict is the
    # words.json identity, so adding a key here invalidates every existing transcript and
    # forces a full re-transcription of the library.
    return {
        "model": MODEL,
        "initial_prompt": INITIAL_PROMPT,
        "compute_type": COMPUTE,
        "beam_size": int(os.environ.get("WHISPER_BEAM_SIZE", "7")),
    }


# Plex "local extras" subfolders + creditless/scene clips — never real episodes, often
# mismatched junk from the scraper, and a frequent source of malformed-clip crashes. The
# --root walk prunes these so a library run only ever transcribes actual episodes.
SKIP_FILE_RE = re.compile(r"\bNCED\b|\bNCOP\b|\bNCBD\b|-\s*scene\b|creditless", re.I)


def log(*a):
    print(*a, flush=True)


# V2 C1: per-show run summary. process() updates this in place on its "ok" (success)
# path only; main() reads it right after each call to accumulate per-show totals for
# glossaries/<show>.lastrun.json. A module-level accumulator (rather than widening
# process()'s return type) keeps every existing "process() returns a status string"
# call site/test unchanged -- see WMODEL above for the same lazy-module-global pattern.
_LAST_STATS: dict = {}
# Sibling accumulator for the three words_* qc.Recorder counters (words_missing,
# words_version_mismatch, words_reused). Populated on EVERY episode process()/
# process_text() touches, not only the "ok" ones _LAST_STATS covers, because a
# words.json write failure or a config mismatch matters to the operator even when
# the episode itself completes.
_LAST_WORDS_STATS: dict = {}


def _note_words_stats(rec):
    """Snapshot this episode's words_* counters so main() can add them into
    lastrun.json regardless of the episode's overall status."""
    _LAST_WORDS_STATS.clear()
    _LAST_WORDS_STATS.update({k: rec.counters.get(k, 0) for k in ("words_missing", "words_version_mismatch", "words_reused")})


def _model_version() -> str:
    """faster_whisper's package version, for the lastrun.json audit trail. Reads the
    already-imported module from sys.modules (real or the tests' stub) rather than
    importing it again, so this stays a no-op in the CPU-only dev/test environment."""
    fw = sys.modules.get("faster_whisper")
    return getattr(fw, "__version__", "unknown") if fw is not None else "unknown"


def _glossary_version() -> str:
    """Short content hash of the active GLOSSARY_FILE (so lastrun.json records exactly
    which glossary revision produced a run) -- 'none' if no glossary file is configured."""
    path = os.environ.get("GLOSSARY_FILE", "")
    if not path or not os.path.exists(path):
        return "none"
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return "none"


def _no_glossary_warning(show, episode) -> int:
    """One line per episode generated with no glossary; 1 when it fired, else 0.

    A show can reach generate with no glossary at all: DAN DA DAN did on 2026-11-19
    (`glossary_version: none` in lastrun.json) and the run looked entirely normal. Nothing
    downstream says so either -- the decoder prompt carries no cast list, glossary.correct()
    is a no-op and repair has no name anchors -- so the failure mode is "shipped with wrong
    names and no one knew". The line names the show and the episode; main() counts it into
    lastrun.json as `no_glossary` and prints the total at the end of the run.

    TODO(notify): route this through dubtitlerr.core.notify once the loops emit a stage/event
    for it (the notify core itself is merged; generate.py has no event path of its own yet).
    Log-only for now, deliberately: a run must never fail or slow down over a notification.
    """
    if _glossary_version() != "none":
        return 0
    log(f"WARNING: generated without glossary (no name protection): {show} / {episode}")
    return 1


# One audio-track decision, shared with the review server's audio preview: see
# dubtitlerr/core/media_select.py. Both call sites used to carry their own rules, and the
# preview -- English tags only -- disagreed with the pipeline on `und` tracks titled
# "English Dub", 422ing an episode the pipeline had just produced.
def eng_audio_index(video):
    try:
        streams = media_select.probe_audio_streams(video, timeout=FFPROBE_TIMEOUT)
    except media_select.MediaProbeError as e:
        log("ffprobe failed", video, e)
        return None
    pick = media_select.select_english_audio(streams)
    if pick.reason == media_select.REASON_TITLE:
        skipped = f"; not preferred: {', '.join(pick.skipped)}" if pick.skipped else ""
        log(
            f"no English audio tag; using the title-identified English track "
            f"(index {pick.index}, title {pick.title!r}; {pick.detail}{skipped})"
        )
    elif pick.reason == media_select.REASON_UND_AS_DUB:
        log(f"no English audio tag; using the only untagged audio track (index {pick.index}) — UND_AUDIO_AS_DUB is on")
    return pick.index


def media_duration(path):
    """Duration of ``path`` in seconds via ffprobe, or None when it cannot be measured.
    None means "unbounded" to reflow.time_cards(): a probe failure must never fail an
    episode, and unbounded is exactly the pre-existing behavior. Called on the EXTRACTED
    WAV, not the container -- whisper's timestamps come out on the wav's timeline, and that
    is the timeline time_cards()'s end-of-audio guard has to compare against.

    When the audio stream is delayed, _apply_audio_start_offset moves the words AND this
    duration onto the video timeline together, so the two stay comparable."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        dur = float(json.loads(r.stdout)["format"]["duration"])
    except Exception as e:
        log("ffprobe duration failed", path, e)
        return None
    return dur if dur > 0 else None


def audio_start_time(video, idx):
    """Start time of the selected audio stream in the container timeline, or None.

    ``idx`` is the global stream index returned by ``eng_audio_index``. Filtering the
    probe result by that index is important when a file contains multiple audio tracks:
    the stream that is probed must be the stream that ``extract_wav`` maps.
    """
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index,start_time",
                "-of",
                "json",
                video,
            ],
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        streams = json.loads(r.stdout).get("streams", [])
        stream = next((s for s in streams if s.get("index") == idx), None)
        if stream is None:
            return None
        return float(stream["start_time"])
    except Exception as e:
        log("ffprobe audio start failed", video, e)
        return None


def _apply_audio_start_offset(words, segments, audio_duration, offset):
    """Move Whisper's audio-relative timestamps onto the video timeline.

    Only a POSITIVE offset is applied. A negative container start means the audio begins
    before video zero, so correcting would shift cues below zero -- and ts_srt has no guard
    for that: ts_srt(-0.5) renders "-1:59:59,500", which corrupts the file silently rather
    than failing. Correcting buys nothing there either, since a cue before the video starts
    cannot be rendered however it is written. The measured population already contains
    negative starts (SAO S01E02-E24 at -7ms); those sit below the threshold and never reach
    this branch, but nothing guarantees the next release will."""
    if offset is None or abs(offset) <= AUDIO_START_THRESHOLD:
        return audio_duration
    if offset < 0:
        log(f"audio start offset: branch=refused-negative offset={offset:+.3f}s (would shift cues before video zero)")
        return audio_duration
    for item in words:
        item["start"] += offset
        item["end"] += offset
    for segment in segments:
        segment["start"] += offset
        segment["end"] += offset
    if audio_duration is not None:
        audio_duration += offset
    log(f"audio start offset: branch=corrected offset={offset:+.3f}s")
    return audio_duration


def extract_wav(video, idx, wav):
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        video,
        "-map",
        f"0:{idx}",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
    ]
    if AUDIO_FILTER:  # V2 A8: empty WHISPER_AUDIO_FILTER = no filter (pre-A8 behavior)
        cmd += ["-af", AUDIO_FILTER]
    cmd.append(wav)
    r = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        # A failing ffmpeg can leave a wav well past the size check below, cut off partway:
        # transcribing it would ship a subtitle for only part of the audio.
        try:
            os.remove(wav)
        except OSError:
            # Best effort: the wav may be gone already, and a failed cleanup must not mask the ffmpeg failure reported next.
            pass
        return False
    return os.path.exists(wav) and os.path.getsize(wav) > 1000


def _card_word_probs(card, words, rec=None):
    """Per-word linear probabilities for one card (V2 A6), joined by time overlap
    against the full per-episode word list built in process()'s transcribe loop.
    reflow.Card doesn't retain which whisper words it was built from, so this
    re-derives the association here rather than threading it through reflow.py.

    Joined on the SOURCE window (C6), never the display one. A forward steal moves
    display timing -- starts later, ends past the old cap -- so a displaced card's
    display window can be entirely disjoint from its own audio while covering its
    neighbour's. Joining on display timing therefore hands repair.has_low_prob_word()
    the wrong card's confidences: a false negative on the card holding the mis-heard
    word and a false positive on the runt that stole the time. Same repoint, and the
    same pre-C6 fallback, as repair.overlap_ref(). The audio a card describes never moves.

    Must be called with the card's PRE-collapse boundaries (i.e. inside the per-card
    loop below, before hallucination.collapse_runs()): a later run-collapse keeps
    run[0]'s text verbatim, so computing word_probs against that same card's own
    source window -- rather than re-querying after the merge widens the window to
    cover the whole repeated run -- keeps the list aligned with the text it
    actually describes."""
    # S-6: a 1-2 word card whose source span exceeds MAX_DUR carries a whisper
    # timestamp already proven wrong, and a window that wide inherits the NEIGHBOURS'
    # probabilities -- measured at 20 of 401 gated cards, one of which would have been
    # flagged for repair purely on borrowed evidence. Return nothing rather than
    # substituting the display window: on 99% of gated cards display == source exactly,
    # so a fallback reproduces the window just declared implausible.
    if hallucination.bad_source_window(card, rec=rec):
        return []
    a = card.get("source_start", card["start"])
    b = card.get("source_end", card["end"])
    return [round(w["prob"], 3) for w in words if w["end"] > a and w["start"] < b]


QC_SUFFIX = ".dubtitles.qc.json"
CONF_SUFFIX = ".dubtitles.conf.json"
STALE_SUFFIX = ".stale"  # parked DELIVERABLE copies, not deleted -- see park_stale_sidecars
# The transcript/diagnostics cluster used to be sidecars. It is now store rows (P39):
#   conf  -- per-cue confidences repair.py reads
#   qc    -- quality counters
#   words -- the decoder's word list a text-tier replay reads
# Parking any of them is artifact_store.park(); nothing is renamed beside the media.
STORE_SIDECAR_KINDS = ("conf", "qc", "words")
# What still lives beside the media and is parked with a .stale rename: the deliverables.
FILE_SIDECAR_SUFFIXES = (".eng.dubtitles.ass", ".eng.dubtitles.srt")
# Kept as the name the file-parking code and its tests use. A words.json left behind by a
# superseded pipeline WOULD otherwise be read by the cached text-tier path, which is why it
# is parked with the rest -- in the store now.
SIDECAR_SUFFIXES = FILE_SIDECAR_SUFFIXES


def _words_decoder_identity(initial_prompt: str) -> dict:
    """The decoder fields a words.json RECORDS, from the same definition read_words compares.

    Sourcing them from decoder_identity() is what makes a sidecar reusable by the process
    that wrote it. Recording MODEL directly stored WHISPER_MODEL on EVERY backend, while an
    endpoint install compares ASR_MODEL: every endpoint sidecar therefore failed its own
    reuse check (words_config_mismatch), the text tier never replayed, and each TEXT_VERSION
    bump cost GPU on exactly the installs the tier split exists to keep off it.

    ``initial_prompt`` comes from the argument, not the identity: it is the prompt actually
    handed to the decoder for this run, which is the value a replay must reproduce.

    ``backend`` is recorded on BOTH backends even though the local identity dict carries
    only model/initial_prompt/compute_type/beam_size. The local dict itself stays frozen --
    adding a key there would count every one of the library's existing sidecars as
    words_config_unknown -- but a sidecar that says which backend wrote it lets a later
    endpoint-configured process reject a local transcript instead of reusing it because the
    two model names happen to collide. It is not compared under the local identity, so no
    existing sidecar changes status.
    """
    ident = dict(decoder_identity())
    ident.setdefault("backend", ASR_BACKEND)
    ident["initial_prompt"] = initial_prompt
    return ident


def write_words(stem, words, segments, audio_duration, initial_prompt=""):
    """Persist the word list so a TEXT-tier change can re-run without the GPU.

    Written at exactly one point: AFTER punctuation.restore() has mutated word["text"]
    in place, and BEFORE reflow splits anything. That is what makes a replay free of the
    punctuation LLM call while still reproducing the same cards.

    ``segments`` is persisted alongside, and is NOT redundant: card_confidence() reads
    each card's no_speech_prob from the SEGMENT list (reflow.py), and that value exists
    nowhere on a word. A sidecar without it would replay every card at nsp 0.0, silently
    disabling the music drop rule and the maybe_silence flag on the cheap path.
    ``audio_duration`` likewise -- time_cards() clamps the tail against it and raises
    CascadeInfeasible from it; it is measured from the wav, which is long gone by replay.

    ``initial_prompt`` is stored as the STRING, not as a hash of the glossary file: the
    prompt is the only glossary-derived input the decoder ever sees, so comparing it is
    what distinguishes a glossary edit that needs the GPU from one that does not.

    The decoder fields (model, plus the backend marker on the endpoint backend) come from
    _words_decoder_identity(), i.e. from the SAME decoder_identity() read_words compares.
    They used to be written from MODEL unconditionally, which made every endpoint sidecar
    fail its own reuse check -- see _words_decoder_identity's docstring.

    The words are stored as generate holds them -- pre-reflow-transform. reflow's
    _normalize/_clamp_to_segments/_dejitter are pure and cost microseconds, and
    _card_word_probs() joins against this same untransformed list, so storing the
    post-transform words would make the replay diverge from the original run for no gain.

    P39: the document is a store row (kind ``words``) in the config-volume SQLite store, not
    a file beside the video. ``common.read_words`` reads the same row. A store failure
    propagates, exactly as the atomic file write did: a run that lost the words must not
    report ok (see the caller's "episode stays GPU-only" log line)."""
    doc = {
        "schema_version": WORDS_SCHEMA_VERSION,
        "transcribe_version": TRANSCRIBE_VERSION,
        **_words_decoder_identity(initial_prompt),
        "audio_duration": audio_duration,
        "segments": segments,
        "words": words,
    }
    artifact_store.put_json(stem, "words", doc)


def park_stale_sidecars(stem):
    """Move the sidecars LEFT BEHIND by a superseded pipeline version (see
    common.stale_version_stamp) out of the way. They are last version's output, not
    pending work: left in place they'd make the skips in process() return
    "already-ass"/"already-srt" forever while mux re-embedded that same old subtitle and
    stamped it as current -- and a leftover qc.json would aggregate as this version's
    measurement.

    They are RENAMED to <name>.stale, not deleted. This ran before the already-srt guard
    and before transcription, so on a version bump under the DEFAULT SKIP_IF_SRT=1 the
    previous srt and conf were destroyed and only THEN could reflow raise
    CascadeInfeasible -- leaving no srt, no conf and a permanent .fail marker that
    retires the episode until an operator clears it by hand. Deleting output before
    knowing a replacement exists is the destructive half of a swap done in the wrong
    order. Parked files are invisible to every consumer (mux, the assemble pass and the
    stall detector all match on exact suffixes), so nothing muxes last version's content
    while they sit there, and a failed replacement is one rename from being undone.
    drop_parked_sidecars() clears them once this run has written its own.

    A sidecar counts as a leftover only if it PREDATES the stamp. The run that wrote the
    stamp wrote its sidecars first and deleted them just after stamping, so anything older
    than the stamp belongs to that finished run. Anything NEWER is this regeneration's own
    fresh work: the stamp only advances when mux succeeds, so a re-transcribed sidecar sits
    beside a still-stale stamp for at least one MERGE_INTERVAL — and indefinitely if the
    mux keeps failing (skip-no-room, verify-*). Deleting that would re-run Whisper on every
    resume pass, and since gen_loop.sh's stall detector counts .srt files, the deletions
    would read as "no progress" and abandon the show mid-regeneration.

    The store rows use ``artifact_store.mtime`` (the row's updated_at, refreshed on every
    write) as the same kind of evidence; the file deliverables keep their filesystem mtime.
    """
    try:
        stamp_mtime = artifact_store.mtime(stem, "done")
    except Exception:  # noqa: BLE001 - an unreadable store must not abort the sweep
        stamp_mtime = None
    if stamp_mtime is None:
        return  # no stamp -> nothing is attributable to it
    for kind in STORE_SIDECAR_KINDS:
        try:
            written = artifact_store.mtime(stem, kind)
            if written is None or written > stamp_mtime:
                continue  # absent, or newer than the stamp -> this run's work
            if artifact_store.park(stem, kind):
                log("  parked stale-version artifact", kind)
        except Exception:  # noqa: BLE001 - parking is opportunistic; a store hiccup must not abort the sweep
            pass
    for suff in FILE_SIDECAR_SUFFIXES:
        p = stem + suff
        try:
            if os.path.getmtime(p) > stamp_mtime:
                continue  # newer than the stamp -> this run's work
            os.replace(p, p + STALE_SUFFIX)
            log("  parked stale-version sidecar", os.path.basename(p))
        except OSError:
            # Parking is opportunistic: a sidecar that cannot be renamed is left where it is and the run continues.
            pass


def parked_sidecars(stem):
    """Names of this episode's parked previous output, sorted. Empty when there is none."""
    names = [os.path.basename(stem + s + STALE_SUFFIX) for s in FILE_SIDECAR_SUFFIXES if os.path.exists(stem + s + STALE_SUFFIX)]
    names += [f"{kind} (store)" for kind in STORE_SIDECAR_KINDS if artifact_store.has_parked(stem, kind)]
    return sorted(names)


def drop_parked_sidecars(stem):
    """Called once this run has written its own srt, conf, qc and words: the parked copies
    were insurance against a failed replacement, and the replacement landed."""
    for kind in STORE_SIDECAR_KINDS:
        try:
            artifact_store.delete(stem, artifact_store.parked_kind(kind))
        except Exception:  # noqa: BLE001 - an unreachable parked copy is harmless; cleanup must never fail the run
            pass
    for suff in FILE_SIDECAR_SUFFIXES:
        try:
            os.remove(stem + suff + STALE_SUFFIX)
        except OSError:
            # Best effort: an unreachable parked copy is harmless, and cleanup must never fail the run.
            pass


def _card_faults(text, dur):
    """Every profile breach of one finished card, or [] when it is clean. B2 + C4a:
    THE single predicate behind both the sidecar's `violations` counter and the console
    line's `violations=` -- those were two independent implementations, and the console
    one (hardcoded 7.001/2/42, no floor, no EPS on cps) is the check that was supposed
    to catch 730 short cards and structurally could not.

    Layout comes from reflow.layout_faults, the single definition of the display
    profile. The duration floor and ceiling are TIMING, not layout, so they are added
    here rather than pushed into a profile that repair.py also consumes: repair may not
    change a card's duration, so a duration fault is not something it can be judged on."""
    faults = reflow.layout_faults(text, dur)
    if reflow.is_short(dur):
        faults.append("under_min_dur")
    if dur > reflow.MAX_DUR + reflow.EPS:
        faults.append("over_max_dur")
    return faults


def _record_before(rec, cards, merges):
    """The "before" half of the sidecar's before/after pairs, measured on reflow's
    output plus its merge log -- i.e. on the state the timing passes were handed.

    ``cards_before`` is the GROUP count the timing layer saw: every merge record absorbs
    exactly one group, so len(cards) + len(merges) reconstructs it. ``cards_after``
    counts what shipped, so the pair spans every pass that can change the card count --
    and a retired episode's sidecar is no longer indistinguishable from a flawless one.

    ``ordinary_under_min_dur_before`` counts the runts the timing layer had to fix, in
    three disjoint parts: every ABSORBED group (short by definition -- _merge_fits gates
    on is_short and an orphan never merges backward); every merge TARGET that was itself
    short before it grew (`target_was_short`, captured at merge time because a merged
    span covers both groups and nothing downstream can recover it); and every surviving
    card whose SOURCE span -- the spoken duration, which no display pass moves -- is
    still short. Omitting the middle term undercounted by ~10% and always downward,
    which flatters exactly the before/after comparison this metric exists to support."""
    rec.count("cards_before", len(cards) + len(merges))
    # merge_runts() counts the original short non-orphan groups at entry and stamps the
    # total on every record; with no merges nothing moved, so the survivors' SOURCE spans
    # (which no display pass touches) are the same population.
    rec.count(
        "ordinary_under_min_dur_before",
        merges[0]["short_groups_before"]
        if merges
        else sum(1 for c in cards if not c.get("orphan") and reflow.is_short(c["source_end"] - c["source_start"])),
    )


def _record_qc(rec, cards):
    """Fold the finished cards into the QC recorder. Validates every FLOOR as well as
    every ceiling -- the omission that hid 730 short cards.

    Takes the CARDS, not (start, end, text) rows: the rows discard the orphan flag, and
    a quarantined orphan that stays short must land in its own counter rather than
    breaking the ordinary_under_min_dur_after == 0 acceptance assertion it is exempt
    from. Muxing a short orphan is an explicit decision; muxing a short ordinary card
    is the defect."""
    rec.record_duration_flags(cards)
    for c in cards:
        a, b, t = c["start"], c["end"], c["text"]
        dur = b - a
        rec.observe("cps", reflow.card_cps(t, dur))
        # required_extension = how much longer this card would have to be displayed to
        # read at MAX_CPS. Signed and observed for EVERY card, so the quantiles describe
        # the whole population (negative == reading slack): this is the quantity the
        # deferred cps-stealing decision consumes, and a bare over_cps count cannot
        # supply it. Zero would be indistinguishable from "no card needed extension".
        rec.observe("required_extension", len(t.replace("\n", " ")) / reflow.MAX_CPS - dur)
        faults = _card_faults(t, dur)
        if "under_min_dur" in faults:
            rec.count("orphan_under_min_dur_after" if c.get("orphan") else "ordinary_under_min_dur_after")
        if "over_cps" in faults:
            rec.count("over_cps")
        if "over_line_len" in faults:
            rec.count("over_line_len")
        # over_chars: two LEGAL 42-char lines can still exceed MAX_CHARS, so this fault
        # passes every per-line check and had no counter behind its event at all.
        if "over_chars" in faults:
            rec.count("over_chars")

        if faults:
            rec.count("violations")


def _layout_faults(text, dur):
    """Which profile constraints ``text`` violates at ``dur`` seconds; an empty list means valid.
    Line lengths are integer character counts, so only the cps comparison needs EPS."""
    return reflow.layout_faults(text, dur)


def _revalidate_after_correction(rec, cards):
    """C7: re-wrap each card's corrected text through reflow.wrap_balance -- the SAME
    function reflow() already used, so generation has exactly one wrapping algorithm --
    then validate the whole profile (line count, line length, and cps at the card's
    actual duration) on the RESULT. Mutates cards in place.

    Order matters: this runs after collapse_runs (which moves a collapsed card's end,
    hence its cps) and before srt/conf are written, so the text validated is the text
    written. Correcting per line preserved the pre-correction break; nothing re-checked
    the profile afterwards.

    The trigger is MEASURED invalidity, never a growth proxy. Wrapping feasibility
    depends on where word boundaries fall, not on total length: a length-neutral
    substitution can redistribute characters until no split satisfies both lines (an
    84-char card whose boundaries land at 20/40/60 has none), and +2 characters on a
    0.83s card adds ~2.4 cps, enough to cross 17 cps by itself.

    An invalid card KEEPS its correction -- the right name beats the layout profile --
    and records a layout_exception event. No splitter is built: splitting needs
    re-timing, which would put layout downstream of timing and give two layout
    algorithms that can disagree.

    Roughly 1% of cards are unwrappable with no correction involved (82-84 chars with no
    word boundary near the midpoint, so wrap_balance falls through to its over-long
    fallback). Those are reported as events with caused_by_correction=False, and are
    already counted by _record_qc's over_line_len/over_cps; the layout_exceptions COUNTER
    is C7's revisit trigger (post_glossary_layout_invalid) and counts only what the
    correction broke."""
    for c in cards:
        dur = c["end"] - c["start"]
        c["text"] = text = reflow.wrap_balance(c["text"].replace("\n", " "))
        reasons = _layout_faults(text, dur)
        if not reasons:
            continue
        before = c.get("pre_correction_text", text)
        pre = _layout_faults(reflow.wrap_balance(before.replace("\n", " ")), dur)
        caused = bool(set(reasons) - set(pre))
        if caused:
            rec.count("layout_exceptions")
        lines = text.split("\n")
        flat = text.replace("\n", " ")
        # priority: a correction-introduced fault exists in no counter and cannot be
        # reconstructed, so it must survive the event cap. Pre-existing faults are
        # already counted losslessly by _record_qc and described by the cps quantiles.
        rec.event(
            priority=caused,
            reason="layout_exception",
            start=round(c["start"], 3),
            end=round(c["end"], 3),
            text=flat,
            layout_exception_reason=reasons,
            pre_existing_reason=pre,
            caused_by_correction=caused,
            line_count=len(lines),
            line_lengths=[len(ln) for ln in lines],
            max_line_length=max(len(ln) for ln in lines),
            visible_chars=len(flat),
            cps=round(reflow.card_cps(text, dur), 2),
        )


# The implementation lives in common.atomic_write (stamp and repair writes use it too); this
# name stays so existing callers and tests keep working.
_atomic_write = atomic_write


def _write_qc(rec, stem):
    """Build and write the qc artifact. Observability only: a write failure is logged, never
    fatal (see qc.write's docstring), so this is safe on the failure path too.

    P39: the document is a store row (kind ``qc``), the same shape qc.write emitted."""
    show = os.environ.get("SHOW_NAME", "") or GLOSS.get("show", "") or "unknown_show"
    doc = rec.build(
        show=show, episode=os.path.basename(stem), stem=stem, glossary_sha=_glossary_version(), pipeline_version=_model_version()
    )
    try:
        artifact_store.put_json(stem, "qc", doc)
    except Exception as e:  # noqa: BLE001 - a store failure must not fail an episode that generated correctly
        log(f"  qc artifact write failed for {os.path.basename(stem)}: {e}")


MAX_CASCADE_EVENTS = 25  # per episode: the worst displacements, not one per moved card


def _record_cascades(rec, cards, cascades):
    """Fold time_cards()'s per-cascade records into the recorder. The records are
    positional over the PRE-filter card list -- reflow() emits exactly one card per group
    -- so a displaced/shortened index there addresses cards[i] here. Counters count CARDS
    (B1), so overlapping cascades (one can reach into the next one's span) are unioned
    rather than summed; cascade_depth is per CASCADE, one observation each.

    B1 promises three answers: "counters answer how many; quantiles answer how bad;
    events answer which ones". The third one needs an event per affected card, because
    neither a count of 431 nor a p99 displacement can name a card anyone can go and look
    at. ONE event per card carrying an effect LIST, never one per effect: the counters
    keep displaced and shortened_by_neighbour deliberately separate, and the same cascade
    routinely does both to the same card."""
    displaced, shortened = set(), set()
    hops, dur_before = {}, {}
    for r in cascades:
        if r["unfixable"]:  # the tail clamp: nothing left to steal from
            rec.count("unfixable_runts")
            continue
        rec.count("stolen")  # the runt at r["index"] took the time
        rec.observe("cascade_depth", r["hops"])
        displaced.update(r["displaced"])
        shortened.update(r["shortened"])
        for i in r["displaced"]:  # first cascade to touch a card saw its true
            dur_before.setdefault(i, r["dur_before"].get(i))  # "before"; cascades run in order
            hops[i] = max(hops.get(i, 0), r["hops"])
    rec.count("displaced", len(displaced))
    rec.count("shortened_by_neighbour", len(shortened))
    moved = {i for i in displaced | shortened if i < len(cards)}
    disp = {i: cards[i]["start"] - cards[i]["source_start"] for i in moved}
    for i in sorted(displaced & moved):
        rec.observe("displacement", disp[i])
    # WORST N ONLY. qc.MAX_EVENTS is 500 and Recorder.event() keeps the FIRST ones, so an
    # event per moved card (431 on a measured episode) would evict the rare classes that
    # exist in no counter at all. The quantiles above already carry the whole
    # distribution; the events only have to name the offenders. Not priority=True either:
    # that tier is reserved for correction-introduced layout exceptions.
    for i in sorted(moved, key=lambda i: (-disp[i], i))[:MAX_CASCADE_EVENTS]:
        c, d0 = cards[i], dur_before.get(i)
        rec.event(
            reason="cascade_shift",
            card_index=i,
            effects=(["displaced"] if i in displaced else []) + (["shortened"] if i in shortened else []),
            start=round(c["start"], 3),
            end=round(c["end"], 3),
            # the audio window is the durable identity: card_index is positional
            # over the pre-filter list, and hallucination dropping renumbers it.
            source_start=round(c["source_start"], 3),
            source_end=round(c["source_end"], 3),
            displacement=round(disp[i], 3),
            hops=hops.get(i, 0),
            dur_before=None if d0 is None else round(d0, 3),
            dur_after=round(c["end"] - c["start"], 3),
        )


def _cascade_infeasible(stem, exc):
    """A2b (strict): the card list cannot satisfy the A5 temporal invariants, so the
    episode is structurally unfixable. No srt/conf/ass is written and nothing is muxed.
    The poison marker goes back down -- process() already cleared the in-flight one after
    transcription -- so the skip-prior-crash path retires this episode instead of letting
    every sweep re-fail it; main() must never see the exception, because its
    non-RuntimeError branch REMOVES the marker and schedules exactly that retry loop.
    The QC sidecar is still written: a failed episode is when the evidence matters most."""
    try:
        artifact_store.put(stem, FAIL_KIND, "")
    except Exception:  # noqa: BLE001 - best effort: an unwritable marker only means the episode re-runs
        # If the marker cannot be written the episode simply re-runs; the caller's exception must still propagate.
        pass
    rec = qc.Recorder()
    rec.count("cascade_infeasible")
    # What survived: on a version bump this episode's previous srt/conf are parked
    # rather than deleted, so "poisoned" is recoverable rather than terminal. Recorded
    # here because the sidecar is the only durable account a retired episode gets.
    parked = parked_sidecars(stem)
    rec.event(
        reason="cascade_infeasible",
        card_index=exc.index,
        requested_shift=exc.requested,
        applied_shift=exc.applied,
        residual_shift=exc.residual,
        audio_duration=exc.audio_duration,
        retained_prior_output=parked,
    )
    _write_qc(rec, stem)
    log(
        f"  cascade infeasible at card {exc.index}: {exc.residual:.3f}s of a {exc.requested:.3f}s "
        f"steal will not fit before {exc.audio_duration}s -- no subtitle written, episode poisoned"
    )
    if parked:
        log(f"  previous output kept as {', '.join(parked)} -- drop the .stale suffix to recover it")
    return "cascade-infeasible"


def text_stages(stem, words, segments, audio_duration, rec):
    """Everything downstream of the word list: reflow, glossary correction, the
    hallucination gate, layout re-validation, and the srt/conf write.

    Extracted so a CACHED re-run and a fresh transcription execute the SAME code. Two
    copies would drift, and the drift would be a replay that quietly produces different
    cards than the run it claims to reproduce -- undetectable without comparing two full
    runs of the same episode. Everything above this line is the transcribe tier; this is
    the text tier, and it costs CPU minutes."""
    # A1: reflow whisper's words into clean, well-timed cards. C1: name-correct each card.
    # B1: drop near-certain hallucinations, flag the suspect, collapse runaway repeat runs.
    merge_log, cascade_log = [], []
    try:
        cut_list = scene_cuts(_video_for(stem)) if _snap_enabled() else []
        cards = reflow.reflow(
            words, segments, merge_log=merge_log, audio_duration=audio_duration, cascade_log=cascade_log, cuts=cut_list
        )
    except reflow.CascadeInfeasible as e:
        return _cascade_infeasible(stem, e)
    kept, fixes, dropped = [], 0, 0
    known = [*GLOSS["names"], *GLOSS.get("phrases", [])]
    # a word looped across many short cards is invisible to the per-card rules; glossary names and phrases are exempt
    loops, trims = hallucination.repeat_loops(cards, known=known)
    # a real sentence that ends in the loop word keeps its real words
    if trims:
        cards = [{**c, "text": trims[idx]} if idx in trims else c for idx, c in enumerate(cards)]
    # arm #4: ONE token looping inside itself (the 446-char CSM cackle) -- reduce it to a
    # readable ~6 repeats before anything else reads the text: the gate refuses the raw
    # token on line length, and wrapping has no boundary to break it on. Text only; the
    # card keeps its timing.
    cards, token_runs = hallucination.reduce_token_runs(cards, known=known)
    for r in token_runs:
        log(
            f"  token-run reduced at {float(r['start'] or 0.0):.3f}s: "
            f"{r['repeats']}x{r['period']}-char run -> {r['after']!r} ({len(r['before'])} chars)"
        )
    rec.count("token_runs_reduced", len(token_runs))
    # Arm #5: unique non-dialogue boilerplate/credit text, which no repetition arm can see.
    # Position, not text, is the safety property: only a LEADING or TRAILING run is
    # removed; a mid-file match is counted and audited and stays. Runs are computed on the
    # pre-drop list, so their indices line up with `loops` above.
    boiler_drops, boiler_audits = boilerplate.drop_indices(cards, rec=rec)
    for a in boiler_audits:
        if a["dropped"]:
            log(f"  boilerplate {a['run']} cue dropped at {float(a['start'] or 0.0):.3f}s: {a['text']!r}")
        else:
            log(f"  boilerplate mid-file cue KEPT at {float(a['start'] or 0.0):.3f}s: {a['text']!r}")
    for idx, c in enumerate(cards):
        # repeat-loop / boilerplate / blocklist / repetition -> drop
        if idx in loops or idx in boiler_drops or hallucination.drop_reason(c, rec=rec):
            dropped += 1
            continue
        lines, n = [], 0
        for ln in c["text"].split("\n"):  # correct per line so the wrap is preserved
            fixed, k = glossary.correct(ln, GLOSS)
            lines.append(fixed)
            n += k
        fixes += n
        kc = dict(c)
        kc["text"] = "\n".join(lines)
        kc["pre_correction_text"] = c["text"]  # C7 tells a broken layout from an inherited one
        kc["flag"] = hallucination.flag_reason(c, rec=rec)  # weaker single signal -> kept but marked
        kc["word_probs"] = _card_word_probs(c, words, rec=rec)  # V2 A6: per-word confidence for repair
        kept.append(kc)
    collapsed = hallucination.collapse_runs(kept)
    # C7: layout was decided before the glossary rewrote the text -- re-wrap and
    # re-validate the corrected cards before anything is written. (`rec` was opened before
    # the restoration pass so its counters land in this same sidecar.)
    _revalidate_after_correction(rec, collapsed)
    rows = [(c["start"], c["end"], c["text"]) for c in collapsed]
    conf = []
    for c in collapsed:
        row = {
            "start": round(c["start"], 3),
            "end": round(c["end"], 3),
            # C6: the audio evidence window, kept separate from the display timing a
            # forward steal may have moved. repair.py selects its fansub reference on
            # THIS pair; sidecars written before C6 simply lack it and fall back.
            "source_start": round(c["source_start"], 3),
            "source_end": round(c["source_end"], 3),
            "avg_logprob": round(c["avg_logprob"], 3),
            "no_speech_prob": round(c["no_speech_prob"], 3),
            "text": c["text"].replace("\n", " "),
        }
        if c.get("flag"):
            row["flag"] = c["flag"]
        if c.get("word_probs"):
            row["word_probs"] = c["word_probs"]  # optional/backward-compat (V2 A6/A7)
        conf.append(row)
    srt = out_for(stem + SUFFIX)

    def _render_srt(f):
        for i, (a, b, t) in enumerate(rows, 1):
            f.write(f"{i}\n{ts_srt(a)} --> {ts_srt(b)}\n{t}\n\n")

    _atomic_write(srt, _render_srt)  # both atomic: the in-flight marker is
    # P39: the conf card list is a store row, not a file. Store writes are transactional, so
    # the crash-atomicity the old temp-file write provided comes from sqlite itself.
    artifact_store.put_json(stem, "conf", conf)  # already gone by here (see helper)
    try:
        os.chown(srt, UID, GID)
    except OSError as e:
        log(f"chown failed for {srt}: {e}")
    # QC sidecar: observability only -- a write failure is logged, never fatal, since the
    # episode already generated correctly (see qc.write's docstring).
    _record_qc(rec, collapsed)
    rec.count("cards_after", len(rows))
    _record_before(rec, cards, merge_log)
    # Deferred from Task 5: orphan candidates are quarantined, not fixed -- count them
    # separately from merges, and never bump orphan_candidates_fixed (nothing here fixes
    # one). merged_backward comes from merge_runts()'s own records, not re-derived.
    rec.count("orphan_candidates", sum(1 for c in cards if c.get("orphan")))
    rec.count("merged_backward", len(merge_log))
    _record_cascades(rec, cards, cascade_log)
    low = sum(1 for c in conf if c["avg_logprob"] < -0.8 or c["no_speech_prob"] > 0.6)
    flagged = sum(1 for c in conf if c.get("flag"))
    rec.count("low_conf", low)
    rec.count("flagged", flagged)  # both were logged and
    _write_qc(rec, stem)  # then thrown away
    # Only now is the replacement COMPLETE -- srt, conf AND sidecar. Dropping the parked
    # copies before the sidecar exists would leave a window where neither generation's
    # qc.json is on disk.
    drop_parked_sidecars(stem)
    max_dur = max((b - a for a, b, _ in rows), default=0.0)
    faults = [_card_faults(t, b - a) for a, b, t in rows]  # B2: the SAME predicate the
    over_cps = sum(1 for f in faults if "over_cps" in f)  # sidecar counts, so the number
    bad = sum(1 for f in faults if f)  # an operator reads cannot differ
    collapsed_n = len(kept) - len(collapsed)
    log(
        f"  cards={len(rows)} name-fixes={fixes} dropped-hallucination={dropped} "
        f"dropped-boilerplate={len(boiler_drops)} "
        f"collapsed={collapsed_n} token-runs={len(token_runs)} flagged={flagged} low-conf={low} "
        f"max_dur={max_dur:.1f}s over_cps={over_cps} violations={bad} "
        f"meanlp={sum(c['avg_logprob'] for c in conf) / max(1, len(conf)):.2f}"
    )
    _LAST_STATS.clear()  # V2 C1: this episode's contribution to the show's lastrun.json
    _LAST_STATS.update(
        {
            "cards_written": len(rows),
            "dropped_hallucination": dropped,
            "dropped_boilerplate": len(boiler_drops),
            "collapsed_runs": collapsed_n,
            "token_runs_reduced": len(token_runs),
            "flagged": flagged,
        }
    )
    return "ok"


def _tier_for_file(v):
    stem = os.path.splitext(v)[0]
    if artifact_store.exists(stem, FAIL_KIND):
        return None  # poison marker wins, exactly as in needs_work()
    stale = stale_tiers(read_stamp(stem + STAMP_SUFFIX), v)
    if not stale:
        return None
    if "transcribe" in stale:
        return "transcribe"
    if read_words(stem, expect=decoder_identity()) is not None:
        return "text"
    return "transcribe"


def partition_todo(files):
    """Split outstanding work by TIER: ``(transcribe_todo, text_todo)``.

    An episode whose transcript is current and whose words.json is usable needs only the
    text stages, which cost CPU minutes. Without this split a TEXT_VERSION bump sends
    every one of those episodes back through the decoder -- for the live library that is
    576 episodes and roughly two GPU-days, which is precisely the cost the tier split
    exists to remove.

    An episode that is text-stale but has NO usable words.json goes to the transcribe
    queue: it transcribes once and gains a sidecar. That is a migration, not a bump, and
    it is far better than being skipped forever for want of a cache."""
    transcribe_todo, text_todo = [], []
    for v in files:
        tier = _tier_for_file(v)
        if tier == "transcribe":
            transcribe_todo.append(v)
        elif tier == "text":
            text_todo.append(v)
    return transcribe_todo, text_todo


def process_text(video):
    """Re-run the TEXT tier from the persisted word list -- no GPU, no punctuation LLM.

    Runs the identical text_stages() a fresh transcription runs, so a replay cannot
    quietly diverge from the run it claims to reproduce. Writes a new srt and conf;
    merge_pass then re-muxes the episode, because its stamp is text-stale."""
    stem = os.path.splitext(video)[0]
    rec = qc.Recorder()
    doc = read_words(stem, rec=rec, expect=decoder_identity())
    if doc is None:
        _note_words_stats(rec)
        return "no-words"
    _note_words_stats(rec)
    return text_stages(
        stem,
        doc["words"],
        doc.get("segments") or [],
        doc.get("audio_duration"),
        rec,
    )


def _asr_key():
    """Key for the endpoint backend: the variable NAMED by ASR_KEY_ENV, and nothing else.

    No fallback names. A fallback made ASR_KEY_ENV="" ("this endpoint is keyless") pick up
    whatever OPENROUTER_API_KEY / OPENAI_API_KEY the environment happened to hold and send it
    to ASR_URL -- possibly a plain-http box on the LAN."""
    return os.environ.get(ASR_KEY_ENV, "").strip() if ASR_KEY_ENV else ""


def endpoint_key_problem():
    """Why the endpoint key may not be sent to ASR_URL, or "" when it may (or no key is set).

    The same rule the glossary and restore stages apply (common.openai_endpoint_problem): https,
    or plain http to loopback only, and no credentials in the URL."""
    if not _asr_key():
        return ""
    problem = common.openai_endpoint_problem(ASR_URL)
    return f"{problem} (ASR_URL={ASR_URL!r}; set ASR_KEY_ENV= for a keyless endpoint)" if problem else ""


def _mp3_for_upload(wav, path):
    """Endpoints cap the upload (OpenRouter 25 MB); a 24-min 16 kHz wav is ~46 MB."""
    r = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            wav,
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "32k",
            path,
        ],
        capture_output=True,
        timeout=FFMPEG_TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        return False
    return os.path.exists(path) and os.path.getsize(path) > 1024


def _post_multipart(url, fields, filename, payload, content_type, headers, timeout):
    import urllib.request

    boundary = "----dubtitlerr" + os.urandom(8).hex()
    chunks = []
    for name, value in fields:
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    chunks.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    chunks.append(payload)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


CHUNK_TARGET = 600.0  # seconds of audio per request when the file is long
CHUNK_SLACK = 90.0  # how far from a target a silence may sit and still be preferred
CHUNK_MIN = 30.0  # never emit a piece shorter than this


SCENE_THRESHOLD = 0.30
CUTS_VERSION = 1


def _force_redo():
    return os.environ.get("FORCE_REDO", "").strip().lower() not in ("", "0", "false", "no", "off")


def _request_since():
    """REQUEST_SINCE: epoch seconds when the request being served started (gen_loop sets it
    once per request, so it is the same across the resume passes after a crash). None when
    generate runs outside a request."""
    raw = os.environ.get("REQUEST_SINCE", "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _rebuilt_since(stem, since):
    """True when this episode's transcript record was written at or after ``since``.

    Both tiers write the ``conf`` store row on success and nothing else does, so the row's
    ``updated_at`` (``artifact_store.mtime``) is the evidence that THIS request rebuilt the
    episode -- across resume passes, and whatever the in-process counters of any one pass say.
    """
    if since is None:
        return False
    try:
        written = artifact_store.mtime(stem, "conf")
    except Exception:  # noqa: BLE001 - an unreadable store reads as "not rebuilt by this request"
        return False
    return written is not None and written >= since


def scope_report(files, since, statuses):
    """What a request's scope looks like on disk after this pass.

    Every in-scope episode lands in exactly one bucket: rebuilt by this request, delivered
    (a current stamp, untouched), awaiting the merge pass (a transcript from an earlier run
    that the merge loop has not published yet), or not done -- counted by reason. The
    request's done/failed decision is made from this (review_requests.verdict), never from
    generate's exit code alone: measured 2026-11-18, a forced request finished "done" in nine
    minutes having produced nothing."""
    rep = {
        "scope": len(files),
        "forced": _force_redo(),
        "since": since,
        "rebuilt": 0,
        "delivered": 0,
        "awaiting_merge": 0,
        # The episodes a .dubtitles.fail marker blocked, in full: review_requests.verdict
        # turns this into the failed request's remedy ("clear-crash <episode>"), and the
        # listing here is the only place that names them for that message.
        "crash_marked": [],
    }
    not_done = {}
    for v in files:
        stem = os.path.splitext(v)[0]
        if _rebuilt_since(stem, since):
            rep["rebuilt"] += 1
            continue
        if stamp_valid(read_stamp(stem + STAMP_SUFFIX), v):
            key = "delivered"
        elif os.path.exists(stem + ".eng.dubtitles.ass") or os.path.exists(stem + ".eng.dubtitles.srt"):
            key = "awaiting_merge"
        else:
            key = None
        if key and not (rep["forced"] and key == "delivered"):
            rep[key] += 1
            continue
        if key == "delivered":
            reason = "still delivered from an earlier run"
        elif artifact_store.exists(stem, FAIL_KIND):
            reason = "prior crash (fail marker)"
            rep["crash_marked"].append(v)
        elif artifact_store.exists(stem, NO_SPEECH_KIND):
            reason = "endpoint gave up (no-speech marker)"
        else:
            reason = statuses.get(v) or "not attempted"
        not_done[reason] = not_done.get(reason, 0) + 1
    rep["not_done"] = not_done
    return rep


def _write_run_report(rep):
    """RUN_REPORT: where gen_loop wants the scope report for the request it is serving."""
    path = os.environ.get("RUN_REPORT", "").strip()
    if not path:
        return
    try:
        _atomic_write(path, lambda f: json.dump(rep, f, indent=2))
    except OSError as e:
        log("  run report write failed:", e)


def _log_scope(rep):
    """One log line carrying the evidence the run report holds: what force asked for and
    what the disk shows. `forced=1 rebuilt=N` is how an operator tells a real rebuild from
    the 2026-11-22 no-op, which looked like a clean run in the old summary."""
    not_done = sum((rep.get("not_done") or {}).values())
    line = (
        f"scope: forced={int(bool(rep.get('forced')))} rebuilt={rep.get('rebuilt', 0)} "
        f"delivered={rep.get('delivered', 0)} awaiting_merge={rep.get('awaiting_merge', 0)}"
    )
    if not_done:
        line += f" not_done={not_done}"
    log(line)


def _forced_partition(todo):
    """Split a forced rebuild by what the disk can serve: ``(transcribe_todo, text_todo)``.

    force means "start over", not "re-transcribe". The stored word list IS the decoder's
    output, and every stage a rebuild exists to re-run (reflow, glossary correction, the
    hallucination gates, layout re-validation, the srt/conf write) sits below it. So an
    episode with a usable words.json replays through the text tier and costs CPU minutes;
    only one without usable words goes back through the decoder. That is the same saving
    partition_todo makes for a version bump, and a forced request has no reason to pay
    GPU-days for audio it has already decoded.
    """
    transcribe_todo, text_todo = [], []
    for v in todo:
        stem = os.path.splitext(v)[0]
        if read_words(stem, expect=decoder_identity()) is not None:
            text_todo.append(v)
        else:
            transcribe_todo.append(v)
    return transcribe_todo, text_todo


def force_park_sidecars(stem, keep_words=False):
    """FORCE_REDO: park an episode's delivered and intermediate sidecars so this run
    rebuilds them.

    The same parking park_stale_sidecars uses, for the same reason: a failed rebuild is one
    unpark away from being undone, and drop_parked_sidecars() clears the parked copy once the
    replacement is written. This is the scoped alternative to bumping TEXT_VERSION --
    regenerate one show from a request instead of re-running the library, and without an
    operator moving files aside by hand.

    Store rows (conf/qc/words) park into the store; the .ass/.srt deliverables rename to
    ``.stale`` beside the media because that is where a deliverable must be to be delivered.
    """
    prefix = os.environ.get("SIDECAR_SUFFIX", ".dub.en")
    # keep_words: a text-tier replay READS the words row, so parking it would turn a cheap
    # replay into a re-transcription -- the opposite of what force means (see
    # _forced_partition).
    for kind in STORE_SIDECAR_KINDS:
        if keep_words and kind == "words":
            continue
        artifact_store.park(stem, kind)
    paths = [stem + s for s in FILE_SIDECAR_SUFFIXES]
    paths += [stem + prefix + ext for ext in (".ass", ".srt")]
    # The delivery MARKERS matter more than the artefacts: process() decides "already-muxed"
    # from the stored `done` artifact and the merge pass decides from the stage record, so
    # parking only the files leaves the episode delivered and the run reports success with
    # nothing to do -- measured 2026-11-18: a forced season request finished "done" in nine
    # minutes having produced zero subtitles.
    #
    # The two markers are store rows, not files: they are DELETED here rather than parked.
    # The old rename-to-`.stale` gave failed replacements a one-rename undo; a stamp has
    # nothing to undo -- it is derived from the video's size+mtime and the tier versions,
    # and the next successful mux rewrites it. Parking it would only add a stale row.
    artifact_store.delete(stem, "done")
    artifact_store.delete(stem, "stages")
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            os.replace(path, path + STALE_SUFFIX)
        except OSError:
            # Best effort: a stamp that cannot be parked is replaced in place by this run's own.
            pass


def _clear_failure_markers(stem):
    """A successful episode supersedes its failure markers.

    Without this, a crash.json from an earlier attempt (or a no-speech marker from
    before the endpoint key was fixed) survives the episode that fixed it: the retry
    counter never resets, and anyone triaging sees a failure on an episode that
    delivered.
    """
    for kind in (CRASH_KIND, NO_SPEECH_KIND):
        try:
            artifact_store.delete(stem, kind)
        except Exception:  # noqa: BLE001
            # Both markers are advisory; one that cannot be removed is retried next pass rather than failing a run that delivered.
            pass


def _video_for(stem):
    for ext in ("mkv", "mp4", "m4v"):
        if os.path.exists(f"{stem}.{ext}"):
            return f"{stem}.{ext}"
    return None


def _snap_enabled():
    return os.environ.get("SNAP_TO_CUTS", "1").strip().lower() not in ("0", "false", "no", "off")


def scene_cuts(video):
    """Shot-change timestamps for a video, cached in the store.

    Decoding 1080p for scene detection costs ~80 s an episode whatever the scale
    (measured: 80.0 s full, 83.7 s at 320px -- the decode dominates), so the answer is
    cached as the ``cuts`` artifact kind and reused by every later text-tier run.
    """
    if not video:
        return []
    stem = os.path.splitext(video)[0]
    doc = artifact_store.get_json(stem, "cuts")
    if isinstance(doc, dict) and doc.get("version") == CUTS_VERSION and float(doc.get("threshold", -1)) == SCENE_THRESHOLD:
        return [float(x) for x in doc.get("cuts") or []]
    try:
        r = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "info",
                "-i",
                video,
                "-filter:v",
                f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
                "-f",
                "null",
                "-",
            ],
            # Bounded like any other full decode: ~80 s for an episode, ~10 min for a three-hour film.
            # On a timeout the episode is simply not snapped, and nothing is cached.
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        cuts = sorted({round(float(m), 3) for m in re.findall(r"pts_time:([0-9.]+)", getattr(r, "stderr", "") or "")})
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"warning: scene detection failed for {os.path.basename(video)}: {exc}")
        return []
    try:
        artifact_store.put_json(stem, "cuts", {"version": CUTS_VERSION, "threshold": SCENE_THRESHOLD, "cuts": cuts})
    except Exception:  # noqa: BLE001 - the cache is an optimisation only: the cuts are already in hand, so a failed write does not change this run
        pass
    return cuts


def _wav_duration(path):
    """Seconds of audio in a file, or 0.0 when ffprobe cannot say."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        return float(r.stdout.strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return 0.0


def _detect_silences(path):
    """(start, end) of every silent stretch in a file, from ffmpeg's own detector."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "info", "-i", path, "-af", "silencedetect=noise=-35dB:d=0.35", "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        out = r.stderr
    except (subprocess.SubprocessError, OSError):
        return []
    starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[0-9.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", out)]
    return [(s, e) for s, e in zip(starts, ends) if e > s]


def plan_chunks(duration, silences, target=CHUNK_TARGET, slack=CHUNK_SLACK, minimum=CHUNK_MIN):
    """Where to cut a long file into request-sized pieces. Pure, so the policy is testable.

    Cuts land in real silence whenever one sits within ``slack`` of the target: cutting
    mid-word makes the model invent an ending for the left piece and a beginning for the
    right one, and both survive into the subtitle. A film of any length is covered — the
    targets keep stepping by ``target`` until the end, so three hours is the same problem
    as two.
    """
    if duration <= 0:
        return []
    if duration <= target + slack:
        return [(0.0, duration)]
    cuts = []
    t = target
    while t < duration - minimum:
        chosen = None
        for start, end in silences:
            mid = (start + end) / 2.0
            if abs(mid - t) <= slack and (chosen is None or abs(mid - t) < abs(chosen - t)):
                chosen = mid
        cut = t if chosen is None else chosen
        if not cuts or cut - cuts[-1] >= minimum:
            cuts.append(cut)
        t += target
    bounds = [0.0, *cuts, duration]
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a > 0]


def _cut_chunk(wav, start, end, out_path):
    """Write [start, end) of ``wav`` to ``out_path`` without re-encoding."""
    # Re-encode to PCM rather than stream-copy. Measured on a real episode: `-c copy`
    # on a 16 kHz wav exits 0 and writes a 138-byte header with no audio at all, so the
    # chunk is silently empty and the endpoint gets nothing to transcribe.
    r = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{end - start:.3f}",
            "-i",
            wav,
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            out_path,
        ],
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        log(f"warning: cutting {start:.0f}-{end:.0f}s failed: {(r.stderr or '').strip()[:200]}")
        return False
    return os.path.exists(out_path) and os.path.getsize(out_path) > 1024


ENDPOINT_RETRIES = 2  # extra tries after a 429, a 5xx or a dropped connection (the glossary stages' policy)
_retry_sleep = time.sleep  # the retry loop's own wait, so a test can observe it alone


def _retry_wait(retry_after, attempt):
    """Seconds before retry ``attempt`` (0-based): a finite Retry-After of 0 or more, capped at
    30 s; anything else backs off 2 s then 4 s. The same rule as repair._retry_wait."""
    try:
        wait = float(retry_after)
        if math.isfinite(wait) and wait >= 0:
            return min(wait, 30.0)
    except (TypeError, ValueError):
        # An absent or non-numeric Retry-After falls through to the 2 s/4 s backoff: the header is untrusted input.
        pass
    return 2.0 * 2**attempt


def _post_with_retries(fields, filename, payload, headers):
    """POST one piece of audio, retrying what is transient: HTTP 429, any 5xx, and a connection
    that dropped or timed out. A 4xx other than 429 is the request's own fault and is raised at
    once. When the retries run out the ORIGINAL error is raised -- not EndpointAsrError, so a
    backend outage never counts toward retiring an episode as no-speech."""
    import urllib.error

    for attempt in range(ENDPOINT_RETRIES + 1):
        try:
            return _post_multipart(ASR_URL, fields, filename, payload, "audio/mpeg", headers, ASR_TIMEOUT)
        except urllib.error.HTTPError as e:
            if not (e.code == 429 or e.code >= 500) or attempt == ENDPOINT_RETRIES or common.stop_requested():
                raise
            wait = _retry_wait((e.headers or {}).get("Retry-After"), attempt)
            log(f"  asr retry {attempt + 1}/{ENDPOINT_RETRIES} after HTTP {e.code} (waiting {wait:.0f}s)")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == ENDPOINT_RETRIES or common.stop_requested():
                raise
            wait = _retry_wait(None, attempt)
            log(f"  asr retry {attempt + 1}/{ENDPOINT_RETRIES} after {type(e).__name__} (waiting {wait:.0f}s)")
        _retry_sleep(wait)
    raise AssertionError("unreachable")  # the loop returns or raises


def _endpoint_request(wav, fields, headers):
    """One POST: the mapped (segments, words) for a single piece of audio."""
    mp3 = wav + ".mp3"
    if not _mp3_for_upload(wav, mp3):
        raise EndpointAsrError("could not build the upload mp3 from the extracted wav")
    try:
        with open(mp3, "rb") as fh:
            payload = fh.read()
        _status, raw = _post_with_retries(fields, os.path.basename(mp3), payload, headers)
        doc = json.loads(raw)
    finally:
        try:
            os.remove(mp3)
        except OSError:
            # Best effort: a temp clip that cannot be deleted is picked up by the stale-file sweep.
            pass
    segs = doc.get("segments") or []
    if not segs and doc.get("text"):
        segs = [{"start": 0.0, "end": float(doc.get("duration") or 0.0), "text": doc["text"]}]
    if not segs:
        if isinstance(doc, dict) and "error" not in doc and ("segments" in doc or "text" in doc):
            raise EndpointNoSpeech(f"no speech in {os.path.basename(wav)}")
        raise EndpointAsrError(f"endpoint returned no segments: {raw[:300]}")
    return _map_endpoint_doc(segs, doc.get("words") or [])


def endpoint_transcribe(wav):
    """Transcribe via an OpenAI-compatible endpoint; return the local decoder's shapes.

    (segments: [{start,end,no_speech_prob}], words: [{text,start,end,prob,seg}])

    A long file is cut into request-sized pieces at silence (see plan_chunks) and the
    times are offset back onto the whole file. Two reasons, both hard limits rather than
    preferences: the upload cap (32 kbps mp3 puts a two-hour film at ~29 MB, past
    OpenRouter's 25 MB) and the endpoint's own request timeout on hours of audio.
    """
    problem = endpoint_key_problem()
    if problem:
        raise EndpointConfigError(f"refusing to send the endpoint key: {problem}")
    key = _asr_key()
    if not key:
        # Keyless endpoints exist (a llama-swap whisper on the LAN). Don't refuse here:
        # an endpoint that wants a key answers 401 and the error carries that.
        log(f"warning: ASR_BACKEND=endpoint with no key in ${ASR_KEY_ENV} — sending none")
    fields = [
        ("model", ASR_MODEL),
        ("language", "en"),
        ("response_format", "verbose_json"),
        # BOTH granularities, deliberately. no_speech_prob lives only on segments, and
        # OpenRouter omits `segments` ENTIRELY when word granularity is the only one asked
        # for -- measured 2026-11-18 against openai/whisper-1: verbose_json alone returns
        # segments (no_speech_prob 0.279), verbose_json + word returns none at all. The old
        # single-granularity request therefore cost the repair stage its music guard on
        # every OpenRouter run, and left the endpoint path rebuilding one synthetic
        # whole-file segment whose nsp is 0.0. whisper.cpp ignores the field and returns
        # the same segments and words either way, so this is safe on both flavours.
        ("timestamp_granularities[]", "word"),
        ("timestamp_granularities[]", "segment"),
    ]
    if INITIAL_PROMPT:
        fields.append(("prompt", INITIAL_PROMPT))
    headers = {"Authorization": "Bearer " + key} if key else {}
    if key and "openrouter.ai" in ASR_URL:
        headers["HTTP-Referer"] = ASR_REFERER
        headers["X-Title"] = "DubTitlerr"

    duration = _wav_duration(wav)
    silences = _detect_silences(wav) if duration > CHUNK_TARGET + CHUNK_SLACK else []
    pieces = plan_chunks(duration, silences)
    if not pieces:
        pieces = [(0.0, duration)]
    if len(pieces) == 1:
        try:
            return _endpoint_request(wav, fields, headers)
        except EndpointNoSpeech as e:
            raise EndpointAsrError(f"endpoint returned no segments: {e}") from e

    log(f"asr: {duration / 60.0:.1f} min file -> {len(pieces)} pieces")
    segments, words = [], []
    for i, (start, end) in enumerate(pieces):
        chunk = f"{wav}.part{i:02d}.wav"
        if not _cut_chunk(wav, start, end, chunk):
            raise EndpointAsrError(f"could not cut piece {i} ({start:.0f}-{end:.0f}s) for the endpoint")
        try:
            segs, ws = _endpoint_request(chunk, fields, headers)
        except EndpointNoSpeech:
            # A ~10-minute piece with nothing said in it (silence, an instrumental stretch) is
            # an empty piece, not a failed episode. Failing here retried the whole episode and
            # retired it as no-speech after three attempts.
            log(f"asr: piece {i} ({start:.0f}-{end:.0f}s) holds no speech")
            continue
        finally:
            try:
                os.remove(chunk)
            except OSError:
                # Best effort: the chunk dir is swept on the next run, and a failed delete must not mask the ASR error.
                pass
        base = len(segments)
        for s in segs:
            s["start"] += start
            s["end"] += start
        for w in ws:
            w["start"] += start
            w["end"] += start
            w["seg"] += base
        segments.extend(segs)
        words.extend(ws)
    if not segments:
        raise EndpointAsrError(f"endpoint returned no speech in any of {len(pieces)} pieces")
    return segments, words


# whisper.cpp's control tokens, if a server is built to emit them ("[_BEG_]", "[_TT_150]").
_SPECIAL_TOKEN = re.compile(r"^\s*(?:\[_[A-Z]+_?\d*\]|<\|[^|]*\|>)\s*$")


def _seg_conf(seg):
    """A segment's own confidence as a word probability: exp(avg_logprob), the local path's
    fallback for a segment without word timestamps. None when the endpoint sends no avg_logprob."""
    lp = seg.get("avg_logprob")
    try:
        return min(1.0, math.exp(float(lp))) if lp is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _map_endpoint_doc(segs, top_words):
    """Map a verbose_json document onto the local decoder's shapes.

    Three shapes are in the wild:
      * words nested in each segment, already whole (faster-whisper-compatible servers);
      * words nested in each segment as TOKENS (whisper.cpp's server: " Tree","cut","ting"),
        where a leading space marks a word start -- merged back into words here, because
        reflow strips and space-joins each word and would ship "Tree cut ting";
      * whole words at the TOP level of the document (OpenAI's documented shape), assigned to
        the segment whose span holds their start.
    A word with no probability takes its segment's exp(avg_logprob), as the local path does,
    never a flat 1.0: card confidence is what selects repair targets and drives the
    hallucination gate."""
    nested = [s.get("words") or [] for s in segs]
    if not any(nested) and top_words:
        nested = [[] for _ in segs]
        starts = [float(s.get("start") or 0.0) for s in segs]
        for w in top_words:
            t = float(w.get("start") or 0.0)
            i = max(0, bisect.bisect_right(starts, t) - 1)
            nested[i].append(w)
    every = [str(w.get("word") or w.get("text") or "") for ws in nested for w in ws]
    token_mode = any(t[:1].isspace() for t in every) and any(t and not t[:1].isspace() for t in every[1:])
    segments, words = [], []
    for si, s in enumerate(segs):
        start, end = float(s.get("start") or 0.0), float(s.get("end") or 0.0)
        segments.append({"start": start, "end": end, "no_speech_prob": float(s.get("no_speech_prob") or 0.0)})
        conf = _seg_conf(s)
        fallback = conf if conf is not None else 1.0
        mine = []
        for w in nested[si]:
            text = str(w.get("word") or w.get("text") or "")
            if not text or _SPECIAL_TOKEN.match(text):
                continue
            p = w.get("probability")
            prob = float(p) if isinstance(p, (int, float)) and p > 0 else fallback
            ws, we = float(w.get("start", start)), float(w.get("end", end))
            if token_mode and mine and not text[:1].isspace():
                prev = mine[-1]
                prev["text"] += text
                prev["end"] = max(prev["end"], we)
                prev["prob"] = min(prev["prob"], prob)
                continue
            mine.append({"text": text, "start": ws, "end": we, "prob": prob, "seg": si})
        if not mine:
            mine = [{"text": s.get("text") or "", "start": start, "end": end, "prob": fallback, "seg": si}]
        words.extend(mine)
    return segments, words


def process(video):
    stem = os.path.splitext(video)[0]
    # The version-aware stamp (common.stamp_valid) is the ONLY "already muxed" guard.
    # The old SKIP_IF_MUXED ffprobe backstop is retired: an embedded Dubtitles track no
    # longer means "done", because mux.py now drops-and-replaces that track, so a
    # version bump must be able to regenerate an already-dubbed episode.
    stamp = read_stamp(stem + STAMP_SUFFIX)
    if stamp_valid(stamp, video):  # muxed, current version -> skip
        return "already-muxed"
    # Our own superseded output -> its leftover sidecars are stale too. Skipped for a
    # poison-marked file: that one is never transcribed, so discarding its sidecars would
    # be pure destruction (mux would then have nothing to embed until the marker is
    # cleared by hand).
    if stale_version_stamp(stamp, video) and not artifact_store.exists(stem, FAIL_KIND):
        park_stale_sidecars(stem)
    if os.path.exists(stem + ".eng.dubtitles.ass"):  # assembled already -> skip (idempotent)
        return "already-ass"
    if os.environ.get("SKIP_IF_SRT", "1") == "1" and os.path.exists(stem + ".eng.dubtitles.srt"):
        return "already-srt"  # generated, awaiting (a retry of) assemble
    if artifact_store.exists(stem, FAIL_KIND):  # a previous attempt hard-crashed on this
        return "skip-prior-crash"  # marker -> skip it (clear it in the store to retry)
    if artifact_store.exists(stem, NO_SPEECH_KIND):  # gave up: the endpoint reported no speech
        return "no-speech"  # clear the marker to retry
    idx = eng_audio_index(video)
    if idx is None:
        return "no-eng-dub"  # sub-only release (or no audio) -> skip
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "a.wav")
        if not extract_wav(video, idx, wav):
            return "extract-failed"
        audio_duration = media_duration(wav)  # measured while the wav still exists

        try:
            artifact_store.put(stem, FAIL_KIND, "")  # mark in-flight (a segfault here leaves the
        except Exception:  # noqa: BLE001
            pass  # marker, so a resume skips this poison file)
        if ASR_BACKEND == "endpoint":
            segments, words = endpoint_transcribe(wav)
        else:
            if WMODEL is None:
                raise RuntimeError(
                    "ASR_BACKEND=local but faster_whisper is not installed (use the full image, or set ASR_BACKEND=endpoint)"
                )
            beam_size = int(os.environ.get("WHISPER_BEAM_SIZE", "7"))
            segs, _info = WMODEL.transcribe(
                wav,
                language="en",
                task="transcribe",
                beam_size=beam_size,
                best_of=beam_size,
                word_timestamps=True,
                vad_filter=False,
                condition_on_previous_text=False,
                no_speech_threshold=0.9,
                log_prob_threshold=-2.0,  # max coverage: VAD was removing
                initial_prompt=INITIAL_PROMPT,
            )  # music-masked dialogue (the 18-20min
            # Buster Call scene) before whisper saw it -> big gaps. VAD off + loose thresholds keep it;
            # B1 + the LLM repair clean the resulting silence/music hallucinations (tuning deferred).
            # condition_on_previous_text=False is now also FORCED BY VRAM, not only by the
            # collapse below. Measured 2026-08-20 on this box (1060, 6GB, large-v3, beam 7):
            # True OOMs -- "CUDA failed with error out of memory" -- in a fresh process with the
            # GPU otherwise idle at 121MiB. True grows the decoder prompt with the previous
            # segment's text, and there is no headroom for it here. Fitting it would mean
            # beam_size=1 or large-v3-turbo, both of which cost accuracy elsewhere.
            # Consequence: Whisper decodes each segment cold, so segments that begin mid-sentence
            # come back lowercase and unpunctuated -- 73% punctuated / 14% lowercase-start at the
            # segment level on S30E06. punctuation.restore() puts that back BEFORE reflow splits
            # the words (it sees the whole run in both directions -- more context than this flag
            # would have given whisper); repair.py is too late, the cards are cut by then.
            # condition_on_previous_text=False: with True, hard/music-masked stretches collapse into
            # one mega-segment (e.g. a 139s "segment" over the 18-20min mark of One Pace S19E16) that
            # reflow then renders as a long gap — real dialogue lost. False keeps segments discrete and
            # recovers that dialogue (faster-whisper's recommended anti-collapse setting). The glossary
            # initial_prompt still biases names; C1 correction + the LLM repair restore cross-line context.
            # (hallucination_silence_threshold also removed — it skipped real speech.)
            # Consume the (lazy) generator while the wav still exists, adapting whisper's
            # objects to the plain dicts reflow expects: one word dict per word (with its
            # source segment index), plus a per-segment record for no_speech_prob.
            words, segments = [], []
            for si, s in enumerate(segs):
                segments.append({"start": s.start, "end": s.end, "no_speech_prob": s.no_speech_prob})
                sw = s.words or []
                if sw:
                    for w in sw:
                        words.append(
                            {
                                "text": w.word,
                                "start": w.start,
                                "end": w.end,
                                "prob": getattr(w, "probability", 1.0) or 1.0,
                                "seg": si,
                            }
                        )
                else:  # no word timestamps -> whole segment as one "word"
                    words.append(
                        {"text": s.text, "start": s.start, "end": s.end, "prob": min(1.0, math.exp(s.avg_logprob)), "seg": si}
                    )
        audio_duration = _apply_audio_start_offset(words, segments, audio_duration, audio_start_time(video, idx))
    try:
        artifact_store.delete(stem, FAIL_KIND)  # transcription finished -> clear in-flight mark
    except Exception:  # noqa: BLE001
        # Best effort: a marker that cannot be cleared ages out via the stale-file sweep.
        pass
    # A0: restore sentence punctuation to the WORD LIST before anything splits it. This has
    # to happen here and not in repair.py: repair edits card text, but by then the cards are
    # already split and timed, so the text would read better while the boundaries stayed
    # wrong (reflow has nothing to split on in an unpunctuated run and cuts on character
    # balance instead -- mid-phrase, across speaker changes). Any failure is a no-op; see
    # punctuation.restore's docstring.
    rec = qc.Recorder()
    punctuation.restore(words, segments, rec, stem=stem)
    # S-2: persist the word list HERE -- after restore() has mutated word["text"] in
    # place, before reflow splits anything -- so a text-tier re-run replays these exact
    # inputs with no GPU and no punctuation LLM call. Never fatal: the transcription
    # output is already committed by this point, so a failed sidecar write costs the
    # episode its cheap path, not its run. It is COUNTED, because an uncacheable episode
    # that says nothing looks identical to one that simply needed transcribing.
    try:
        write_words(stem, words, segments, audio_duration, initial_prompt=INITIAL_PROMPT)
    except Exception as e:
        rec.count("words_missing")
        log("words.json write failed (episode stays GPU-only):", stem, e)
    return text_stages(stem, words, segments, audio_duration, rec)


def build_lastrun(show, elapsed_s, episodes_total, transcribed, totals, census, words_totals=None, no_glossary=0):
    """The per-show run summary written to glossaries/<show>.lastrun.json.

    One builder rather than a dict literal plus a parallel list of field names: the
    per-tier staleness counts only earn their keep if something actually reports them,
    and two declarations of "what lastrun contains" would drift until one of them lied.
    Tests assert against this function, so the file and the assertion cannot disagree.

    `no_glossary` is the count of episodes this run generated with no glossary at all --
    the DAN DA DAN case: `glossary_version: none` alone did not say the episodes had
    shipped without name protection, and nothing counted them."""
    words_totals = words_totals or {}
    return {
        "show": show,
        "elapsed_s": elapsed_s,
        "episodes_total": episodes_total,
        "episodes_transcribed": transcribed,
        "cards_written": totals["cards_written"],
        "dropped_hallucination": totals["dropped_hallucination"],
        "dropped_boilerplate": totals.get("dropped_boilerplate", 0),
        "collapsed_runs": totals["collapsed_runs"],
        "token_runs_reduced": totals["token_runs_reduced"],
        "flagged": totals["flagged"],
        # GPU-hours vs CPU-minutes: reported separately or the split means nothing.
        "transcribe_stale": census["transcribe_stale"],
        "text_stale": census["text_stale"],
        "words_missing": words_totals.get("words_missing", 0),
        "words_version_mismatch": words_totals.get("words_version_mismatch", 0),
        "words_reused": words_totals.get("words_reused", 0),
        "no_glossary": no_glossary,
        "model": decoder_identity()["model"],  # the ACTUAL decoder (ASR_MODEL on endpoint), not the local default
        "model_version": _model_version(),
        "glossary_version": _glossary_version(),
    }


def _staleness_census(files):
    """How many of ``files`` are behind in each tier, counted independently.

    Separate counts because the two costs are not comparable: transcribe-stale is
    GPU-hours, text-stale is CPU-minutes. A single combined "stale" number would hide
    exactly the distinction the tier split exists to expose. A transcribe-stale episode
    is text-stale too (new words invalidate everything derived from them), so text_stale
    is always >= transcribe_stale.

    Never raises: a corrupt or hand-edited stamp counts as fully stale, because this runs
    over a whole library and one bad sidecar must not abort the sweep."""
    census = {"transcribe_stale": 0, "text_stale": 0}
    for v in files:
        stem = os.path.splitext(v)[0]
        stale = stale_tiers(read_stamp(stem + STAMP_SUFFIX), v)
        if "transcribe" in stale:
            census["transcribe_stale"] += 1
        if "text" in stale:
            census["text_stale"] += 1
    return census


def filter_only_re(files, pattern):
    """Filter files whose basename matches regex pattern. Fail closed on invalid regex."""
    if not pattern:
        return files
    try:
        rx = re.compile(pattern)
    except re.error as e:
        log(f"invalid GENERATE_ONLY_RE regex {pattern!r}: {e}")
        sys.exit(2)
    total = len(files)
    kept = [f for f in files if rx.search(os.path.basename(f))]
    log(f"GENERATE_ONLY_RE: kept {len(kept)} of {total} files")
    return kept


def main():
    args = sys.argv[1:]
    files = []
    if args and args[0] == "--root":
        for dp, dns, fs in os.walk(args[1]):
            dns[:] = [d for d in dns if d.lower() not in EXTRA_DIRS]  # prune extras dirs
            for fn in fs:
                if fn.lower().endswith(VIDEO_EXTS) and not SKIP_FILE_RE.search(fn):
                    files.append(os.path.join(dp, fn))
        only_re = os.environ.get("GENERATE_ONLY_RE", "")
        if only_re:
            files = filter_only_re(files, only_re)
        # Watch-order priority: process seasons >= a per-show start season first (the arc
        # the viewer is about to watch), then earlier ones. Absent config -> plain sort.
        files = ordering.order_files(files, ordering.read_start(os.environ.get("SHOW_NAME", "")))
    else:
        files = args
    load_glossary()
    # One derivation, used by the per-episode glossary warning and by lastrun.json.
    show = os.environ.get("SHOW_NAME", "") or GLOSS.get("show", "") or "unknown_show"

    # Cheap pre-filter (stat only, no ffprobe/model): drop files already done so a perpetual
    # re-scan doesn't pay the ~40s model load when there's nothing new to transcribe.
    since = _request_since()
    forced = _force_redo()

    def needs_work(v):
        stem = os.path.splitext(v)[0]
        # Read here, not closed over: tests extract this function from main() and call it alone.
        if _force_redo():
            # A forced request reaches delivered episodes too -- the stamp check below is
            # exactly what it overrides -- but only once: an episode this request already
            # rebuilt (in an earlier resume pass) is not parked and rebuilt again. A poison
            # marker still wins; it is reported, not silently skipped.
            return not artifact_store.exists(stem, FAIL_KIND) and not _rebuilt_since(stem, _request_since())
        stamp = read_stamp(stem + STAMP_SUFFIX)
        if stamp_valid(stamp, v):
            return False  # muxed at the current version -> done
        if artifact_store.exists(stem, FAIL_KIND):
            return False  # poison marker wins
        # Superseded output: its leftover sidecars are stale, so the checks below must not
        # read them as "done" -- process() discards them and re-transcribes.
        if stale_version_stamp(stamp, v):
            return True
        if os.path.exists(stem + ".eng.dubtitles.ass"):
            return False
        if os.environ.get("SKIP_IF_SRT", "1") == "1" and os.path.exists(stem + ".eng.dubtitles.srt"):
            return False
        return True

    todo = [v for v in files if needs_work(v)]
    # Split what needs doing by TIER. needs_work() stays the gate (it also honours the
    # pending-sidecar and poison-marker skips); this only decides which queue the
    # survivors land in.
    # Forced: a full rebuild of everything in scope. partition_todo would drop a delivered
    # episode (its stamp is current in both tiers until force_park_sidecars moves it aside),
    # so the split comes from what the disk can serve: a usable words.json replays in the
    # text tier, anything else needs the decoder.
    transcribe_todo, text_todo = _forced_partition(todo) if forced else partition_todo(todo)
    statuses = {}
    # Counted over EVERY file, not just todo: the question this answers is "how far
    # behind is the part of the library I am not watching", and that is invisible if it
    # only counts what is already queued.
    census = _staleness_census(files)
    log(
        f"model={MODEL} compute={COMPUTE} require_eng={os.environ.get('REQUIRE_ENG', '1')} "
        f"files={len(files)} todo={len(todo)} "
        f"transcribe={len(transcribe_todo)} text={len(text_todo)} "
        f"transcribe_stale={census['transcribe_stale']} text_stale={census['text_stale']}"
    )
    if not todo:
        log("nothing to transcribe (all done) — skipping model load")
        empty_report = scope_report(files, since, statuses)
        _write_run_report(empty_report)
        _log_scope(empty_report)
        return
    # The model is loaded ONLY when something actually needs the decoder. A text-only
    # sweep otherwise paid a ~40s GPU model load to perform zero transcription, which
    # would have made the cheap tier quietly not cheap.
    if transcribe_todo and common.stop_requested():
        # the loops below break on their first check; do not pay the load for nothing
        log("stop requested: not loading the model")
    elif transcribe_todo and ASR_BACKEND == "endpoint":
        problem = endpoint_key_problem()
        if problem:
            # A configuration error, so refuse the whole run before any episode is touched
            # rather than failing each one in turn.
            log(f"FATAL: refusing to send the endpoint key: {problem}")
            sys.exit(2)
        log(f"ASR backend=endpoint model={ASR_MODEL} url={ASR_URL} — no local model load")
    elif transcribe_todo:
        globals()["WMODEL"] = WhisperModel(MODEL, device=ASR_DEVICE, compute_type=COMPUTE, download_root=MODEL_DIR)
    else:
        log("text-tier work only — skipping the model load")
    t0 = time.monotonic()  # V2 C1: per-show run summary
    transcribed = 0
    no_glossary = 0  # episodes generated with no glossary at all (see _no_glossary_warning)
    totals = {"cards_written": 0, "dropped_hallucination": 0, "collapsed_runs": 0, "token_runs_reduced": 0, "flagged": 0}
    # Text tier first: it is CPU-minutes per episode, so the cheap wins land before the
    # GPU queue starts consuming the night.
    for v in text_todo:
        if common.stop_requested():
            log("stop requested: not starting", os.path.basename(v))
            break
        log("→", os.path.basename(v), "(text)")
        no_glossary += _no_glossary_warning(show, os.path.basename(v))
        if _force_redo():
            # Park the delivered output and its markers so this replay's srt/conf replace
            # them and the merge pass re-muxes. keep_words: the replay needs words.json.
            force_park_sidecars(os.path.splitext(v)[0], keep_words=True)
        try:
            statuses[v] = process_text(v)
            log("  ", statuses[v])
        except Exception as e:
            statuses[v] = f"error: {type(e).__name__}"
            log("  ERROR", type(e).__name__, e)  # one bad sidecar must not abort the show
    for v in transcribe_todo:
        if common.stop_requested():
            log("stop requested: not starting", os.path.basename(v))
            break
        log("→", os.path.basename(v))
        no_glossary += _no_glossary_warning(show, os.path.basename(v))
        if _force_redo():
            force_park_sidecars(os.path.splitext(v)[0])
        try:
            status = process(v)  # one bad episode must not abort the show
            statuses[v] = status
            log("  ", status)
            if status == "ok":
                transcribed += 1
                _clear_failure_markers(os.path.splitext(v)[0])
                for k in totals:
                    totals[k] += _LAST_STATS.get(k, 0)
        except Exception as e:
            statuses[v] = f"error: {type(e).__name__}"
            log("  ERROR", type(e).__name__, e)
            # V2 C15: gate on the exception TYPE, not a substring match on "cuda" in the
            # message/stacktrace. faster-whisper/ctranslate2 raise RuntimeError for real
            # GPU errors (OOM, device ordinal, cuBLAS) -- the old `"cuda" in str(e).lower()`
            # check also fired on a plain ValueError/ZeroDivisionError that merely mentions
            # "cuda" somewhere in its text, which would falsely poison (and exit-3) on a bug
            # that has nothing to do with the GPU context.
            if isinstance(e, RuntimeError):
                # A CUDA OOM/device error poisons the context — every later file would also
                # fail and get falsely marked. Exit so the loop relauncher restarts with a
                # fresh context; the OOM'd file keeps its .fail (skipped on resume), the rest
                # transcribe cleanly. (Usually means another process grabbed the GPU.)
                log("  CUDA/GPU error (RuntimeError) -> exiting to rebuild a clean GPU context (show resumes on restart)")
                sys.exit(3)
            # Non-RuntimeError: NOT a GPU error -> don't poison the episode. Clear the .fail
            # marker so the next sweep retries it, and persist a small JSON record of what
            # happened (V2 C15's retry log) for later triage.
            stem = os.path.splitext(v)[0]
            attempts = 1
            if isinstance(e, EndpointAsrError):
                doc = artifact_store.get_json(stem, CRASH_KIND)
                if isinstance(doc, dict):
                    try:
                        attempts = int(doc.get("attempts", 0)) + 1
                    except (TypeError, ValueError):
                        attempts = 1
            try:
                artifact_store.delete(stem, FAIL_KIND)
            except Exception:  # noqa: BLE001
                # Best effort: the marker is advisory -- the crash counter below is what gates the next attempt.
                pass
            if isinstance(e, EndpointAsrError) and attempts >= ENDPOINT_MAX_ATTEMPTS:
                # Consecutive endpoint failures: stop retrying and record a terminal state
                # for this episode. No empty sidecar is written -- the census keeps showing
                # it as a gap, which is the truth.
                try:
                    artifact_store.put_text(
                        stem,
                        NO_SPEECH_KIND,
                        json.dumps(
                            {"path": v, "attempts": attempts, "exc_type": type(e).__name__, "msg": str(e), "time": time.time()}
                        ),
                    )
                except Exception:  # noqa: BLE001
                    # Evidence only: the episode still retires through the marker/counter path if the sidecar cannot be written.
                    pass
                log(f"  endpoint failed {attempts}x -> marked no-speech, no further retries: {os.path.basename(v)}")
            else:
                try:
                    artifact_store.put_text(
                        stem,
                        CRASH_KIND,
                        json.dumps(
                            {"path": v, "exc_type": type(e).__name__, "msg": str(e), "time": time.time(), "attempts": attempts}
                        ),
                    )
                except Exception:  # noqa: BLE001
                    # A crash record that cannot be written must not turn a handled failure into an unhandled one.
                    pass
    # V2 C1: per-show run summary (glossaries/<show>.lastrun.json) -- one file per --root
    # invocation, since SHOW_NAME/GLOSSARY_FILE are per-run env (see load_glossary()).
    lastrun = build_lastrun(
        show, round(time.monotonic() - t0, 1), len(todo), transcribed, totals, census, no_glossary=no_glossary
    )
    report = scope_report(files, since, statuses)
    _write_run_report(report)
    _log_scope(report)
    if no_glossary:
        log(
            f"generated without glossary: {no_glossary} episode(s) — no name protection "
            f"(glossary_version: none); see the WARNING lines above"
        )
    if isinstance(lastrun, dict):
        lastrun["scope"] = report  # "done" carries its evidence: what was asked for vs what exists
    try:
        os.makedirs(GLOSS_DIR, exist_ok=True)
        with open(os.path.join(GLOSS_DIR, show + ".lastrun.json"), "w") as f:
            json.dump(lastrun, f, indent=2)
    except OSError as e:
        log("  lastrun.json write failed:", e)


if __name__ == "__main__":
    main()
