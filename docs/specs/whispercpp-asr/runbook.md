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

## T1.0 — close the config gaps (a second entry; the live one untouched)

Config: `~/stacks/ai/llama-swap.yaml` on omarchy, mounted to
`/etc/llama-swap/config/config.yaml`. **It hot-reloads on save** —
`/proc/1/cmdline` shows `-watch-config`, and the logs read
`reloading configuration` → `configuration reloaded`. No restart needed.

1. Back up, following the convention already in that directory:

   ```bash
   cd ~/stacks/ai
   cp llama-swap.yaml llama-swap.yaml.bak-$(date +%Y%m%d)
   ```

2. Add a **second** entry directly after the existing
   `whisper-large-v3-turbo-subs` block (currently lines 95-107). Do **not** edit the
   existing entry: it is what production runs, and it stays that way until the gate has
   evidence.

   ```yaml
     whisper-large-v3-turbo-subs-v2:
       cmd: |
         ${whisper}
         --model /models/ggml-large-v3-turbo.bin
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

   `--logprob-thold -2.0` is the gap: production faster-whisper runs
   `log_prob_threshold=-2.0` and the live entry leaves the server default of `-1.00`.

3. **Add it to the `audio` group** (near line 145). The router only routes to group
   members — without this the entry loads and is never reachable, which looks like a
   silent failure:

   ```yaml
           audio:
             ...
             - whisper-large-v3-turbo-subs
             - whisper-large-v3-turbo-subs-v2      # <- add
   ```

4. Verify (both must work; save the file first so the watcher reloads):

   ```bash
   # the live entry is unchanged
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:11434/health
   # the new entry answers and returns words
   curl -s -F file=@<any short wav> -F response_format=verbose_json \
        -F model=whisper-large-v3-turbo-subs-v2 \
        http://127.0.0.1:11434/v1/audio/transcriptions | head -c 400
   ```

   Confirm the reply has `segments[].words` with `probability`, and `avg_logprob` /
   `no_speech_prob` per segment.

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
cd /root/workspace/repos/DubTitlerr        # debiantop

.venv/bin/python tools/asr_bakeoff.py \
  "/mnt/saxro-media/anime-tv/<show>/Season 01/<episode>.mkv" \
  --models "endpoint:http://omarchy:11434/v1/audio/transcriptions@whisper-large-v3-turbo-subs,endpoint:http://omarchy:11434/v1/audio/transcriptions@whisper-large-v3-turbo-subs-v2" \
  --ref "$(basename "<the same episode>.mkv")=<path to the reference SRT>" \
  --out /tmp/bakeoff-config-ab-$(date +%Y%m%d).json \
  --metrics-only
```

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
