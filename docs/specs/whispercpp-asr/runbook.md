# Runbook — whisper.cpp ASR bakeoff

> Operational steps for T1.0 (close the config gaps) and T1.3 (run the bakeoff).
> Everything here is the maintainer's to run. Verified 2026-11-18.

## Where things run

| Need | Where | Notes |
|---|---|---|
| The media (`*.mkv`) | **debiantop** `/mnt/saxro-media/anime-tv/`, **minisax** `/mnt/saxserver-media`, **omarchy** `/mnt/tdarr-media` | the same library, reachable from all three |
| The ASR endpoint | **omarchy**, `http://omarchy:11434/v1/audio/transcriptions` | llama-swap listens `0.0.0.0:11434`. Verified from debiantop: `/health` → 200, and a real 60 s transcription returned 11 segments / 65 words in 2.6 s on a warm model |
| The bakeoff tool | **debiantop** `/root/workspace/repos/DubTitlerr` | has the branch, the venv and the endpoints — **run it here.** Omarchy's clone is stale (`9cc99de`) with no venv, so running there would need the branch carried over first |

**Run the bakeoff on debiantop.** It needs the media, the repo and a route to the endpoint,
and debiantop has all three; omarchy has the media and the endpoint but not this branch.

## T1.0 — add a large-v3 entry beside the live one

The maintainer chose to move the sidecar to `large-v3` (2026-11-18): turbo's
`no_speech_prob` is collapsed on **every** backend (see the spec's finding), and `large-v3`
measured better WER in the wiki's own table (18.93% vs 20.76%). So the A/B is no longer a
flag difference — it is **turbo vs large-v3**, which is the change actually being
considered.

Config: `~/stacks/ai/llama-swap.yaml` on omarchy, mounted to
`/etc/llama-swap/config/config.yaml`. **It hot-reloads on save** — `/proc/1/cmdline` shows
`-watch-config`, and the logs read `reloading configuration` → `configuration reloaded`.
No restart.

**1. Download the model** into the host-side model dir (`/home/saxxy/ai-models` →
`/models` in the container). Verified sizes: `ggml-large-v3.bin` **2.88 GB**,
`ggml-large-v3-q5_0.bin` **1.01 GB** (a cheaper middle option worth testing — quantisation
is its own bakeoff axis). Omarchy has ~906 GB free, so size is not a constraint.

```bash
cd ~/ai-models
curl -fL --retry 3 -o ggml-large-v3.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin
ls -la ggml-large-v3.bin     # expect ~2.88 GB / 3093095424 bytes
```

**2. Back up, then add the entry** after the existing `whisper-large-v3-turbo-subs` block.
Do **not** edit the existing entry — it is what production runs, and it stays that way
until the gate has evidence.

```bash
cd ~/stacks/ai && cp llama-swap.yaml llama-swap.yaml.bak-$(date +%Y%m%d)
```

```yaml
  whisper-large-v3-subs:
    cmd: |
      ${whisper}
      --model /models/ggml-large-v3.bin
      --inference-path /v1/audio/transcriptions
      --tmp-dir /tmp
      --max-context 0
      --beam-size 7
      --best-of 7
      --no-speech-thold 0.9
      --logprob-thold -2.0
    proxy: "http://127.0.0.1:${PORT}"
    ttl: 900
    capabilities:
      in: [audio]
      out: [text]
```

`--logprob-thold -2.0` matches production faster-whisper's `log_prob_threshold`; the
existing turbo entry leaves the server default of `-1.00`. Take both differences or the A/B
cannot say which one moved the numbers.

**3. Add it to the `audio` group** (near line 145). The router only routes to group members,
and an entry outside the group loads and is never reachable — which looks like a silent
failure:

```yaml
        audio:
          ...
          - whisper-large-v3-turbo-subs
          - whisper-large-v3-subs            # <- add
```

**4. Verify** (save the file first so the watcher reloads):

```bash
# the live entry is untouched and still answers
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:11434/health
# the new entry answers, and -- the first thing to check -- whether its nsp is ALIVE
curl -s -F file=@<a short wav> -F response_format=verbose_json \
     -F model=whisper-large-v3-subs \
     http://127.0.0.1:11434/v1/audio/transcriptions | head -c 400
```

The point of the whole change is the `no_speech_prob` values in that reply. turbo returns
`~1e-10`; `large-v3` should return a range that reaches the `0.0-0.9` territory the guard
expects. **The wiki's large-v3 evidence is from CT2, not whisper.cpp** — whisper.cpp's
large-v3 nsp is unverified until this reply, or the bakeoff's `nsp_alive_frac`, says
otherwise. Check it before believing the fix worked.

## Prerequisites for T1.3

1. **The reference episode + SRT** — still unanswered (spec Open question 1). Without it the
   run produces QC and timing numbers but **no WER**, the gate's first criterion.
2. **The `-subs-v2` entry** (T1.0) — the A/B needs both entries to exist.
3. Nothing else: debiantop already has the branch, the venv, ffmpeg/ffprobe and endpoint
   access. No push and no host writes are required to *run* the bakeoff.

## T1.3 — the bakeoff

Run on **debiantop**, from the repo root. Both entrants are the *same* endpoint with different
`model` values: llama-swap picks the entry by the request's `model` field, so **the label
after `@` must be the router's entry name**.

```bash
cd /root/workspace/repos/DubTitlerr            # debiantop

EP="/mnt/saxro-media/anime-tv/Cyberpunk Edgerunners (2022) [tvdbid-384541]/Season 01/Cyberpunk Edgerunners (2022) - S01E01 - 001 - Let You Down [NF][WEBDL-1080p v2][EAC3 5.1][JA+EN][x264 8bit]-FLUX.mkv"
REF=/root/workspace/dubtitlerr-audit/bakeoff-refs/edgerunners-s01e01-dub.srt

.venv/bin/python tools/asr_bakeoff.py "$EP" \
  --models "endpoint:http://omarchy:11434/v1/audio/transcriptions@whisper-large-v3-turbo-subs,endpoint:http://omarchy:11434/v1/audio/transcriptions@whisper-large-v3-subs" \
  --ref "$(basename "$EP")=$REF" \
  --out /tmp/bakeoff-aot-e1-$(date +%Y%m%d).json \
  --metrics-only
```

**About that reference.** This one is a genuine **dub script**, not a sub: the episode
carries an embedded track titled `Dub` (stream 6), and 170 episodes across the library
carry one. Verified on extraction: 329 cues, 2246 words, last cue at 1345 s. The wiki's
original reference was the same lucky discovery (a track mislabelled SDH that was really
the dub script) — this library simply labels them properly. So the WER here is a real
ground-truth number, not a cross-translation proxy.

- `--metrics-only` is **required for any report that leaves the machine**: the full report
  carries a commercial dub's script, and this repo does not commit transcripts.
- **The `--ref` key must be the episode's full basename, extension included.** The scorer
  matches on `os.path.basename(video)`, so `--ref "S01E05=..."` matches nothing and the run
  silently produces no WER. Copy the basename from the video path.
- Entrants run strictly sequentially, which is what the tool is built around.
- Nothing touches the live library: this tool only reads the episode and writes its own
  report. (T1.5's episode generation is the step that needs separate `OUTPUT_ROOT`s.)

Read the result with:

```bash
.venv/bin/python tools/asr_bakeoff.py --report-only --out /tmp/bakeoff-config-ab-<date>.json
```

The numbers to compare against the spec's acceptance criteria are per entrant:
`wer_vs_ref` (gate 1), `flagged` / `over_cps` / `dropped_hallucination` from the pipeline
(gate 3), `nsp_median` / `logprob_p05` / `music_rule_would_fire` (gate 5), and the
per-episode `wall_s` (gate 8).

**Note on `logprob_source`:** the endpoint entrants derive `avg_logprob` from word
probabilities (reflow's formula) because the endpoint carries no per-segment logprob. Every
report entry says so. Comparing that column against a faster-whisper entrant's
`native-segment` value is comparing two measurements — the spec's gate 5 is therefore
evaluated on `no_speech_prob` and word `probability` distributions, with segment logprob
reported for context only.
