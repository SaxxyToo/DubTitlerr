# ASR bakeoff data

The raw per-entrant numbers behind [Choosing an ASR model](../wiki/Choosing-an-ASR-Model.md) and the
synthesis in [ASR bakeoff data](../wiki/ASR-bakeoff-data.md). Written by `tools/asr_bakeoff.py`.

| File | Card | Run |
| --- | --- | --- |
| `gtx1060-6gb.json` | GTX 1060, 6 GB — Pascal | 2026-09-04 |
| `rtx2070s-8gb.json` | RTX 2070 Super, 8 GB — Turing | 2026-09-04 |
| `whispercpp-2026-11-18.json` | RTX 5060, over an HTTP endpoint | 2026-11-18 |

**Metrics only.** The harness also captures every entrant's full transcript, which is a commercial
dub's script. A report destined to leave the machine is produced with `--metrics-only`, which strips
the transcripts and the release tags in each episode filename. Word error rate, peak VRAM, load and
wall time, and the confidence distributions are what remain, and they are all that is kept here.
Re-run the harness locally if you need the transcripts.
