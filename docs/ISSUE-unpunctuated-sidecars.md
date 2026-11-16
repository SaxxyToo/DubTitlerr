# ISSUE: a sidecar can be published with no punctuation, wrong names, or half an episode

**Status: open (2026-11-15).** Found while reviewing Neon Genesis Evangelion E22/E23 after
Michael reported the dubtitles as "really bad" and the character names still wrong across
the whole series.

## What shipped

Neon Genesis Evangelion, 26 episodes in `anime-tv/Neon Genesis Evangelion (1995) [tvdbid-70350]`.
Every one of the 25 episodes built by the batch generator has the same signature:

| symptom | measured |
| --- | --- |
| cards with no terminal punctuation | 90-100% per episode (E22: 668/675) |
| cards starting lowercase mid-sentence | 35-209 per episode |
| `Oscar` for **Asuka** | 28 occurrences |
| `Ava` / `Avas` for **Eva** / **Evas** | 21 occurrences |
| `sink ratio` for **sync ratio** | 5 occurrences |
| `Asakura` for **Asuka** | 5 occurrences |
| E22 glued two speakers into one card | e.g. `You're the only one I love, Kaji. Well / that's quite an honor. I mean it, Kaji!` |
| E23 coverage | 127 cues, last at **11:26** of a 24:30 episode (40%), and the text is a truncated fansub translation, not a dubtitle |

E22's raw text, as published:

```
the direct cause you mean was the mental breakdown the result of the contact yes
Oscar sink ratio has dropped an entire eight points don't think about
that doll as if it were her real daughter I think it's because of the
```

`.dubtitles.done` and `stages.json` both say `"outcome": "ok"` for these episodes.

## Root causes

### 1. Cards are built from word tokens, and word tokens carry no punctuation

`gen_dubtitles.py:95 build_cards(words)` groups `w["word"]` tokens and joins them with
`_join()`. Every ASR in use punctuates `segment.text` and leaves `words[].word` bare:

```json
{"start": 0.0, "end": 6.26, "text": " The mitosis process in the left arm of Unit 2 has cleared the numerical requirement. Code 3."}
words: ["The","mitosis","process","in","the","left","arm","of","Unit","2","has","cleared","the","numerical","requirement","Code","3"]
```

So the pipeline throws away every full stop it was given. `reflow.reflow(words, segments)`
has the same shape — `segment` is typed `{"start","end","no_speech_prob"}` with no text — and
`punctuation.py` exists precisely to put punctuation back with an LLM. The batch generator
never calls it.

**Fix:** attach `segment.text` punctuation to the word tokens before reflow. The token
sequences match after stripping punctuation, and whisper's word timings drift by at most a
token across segment boundaries, so a `difflib.SequenceMatcher` alignment is enough. This is
deterministic, costs nothing, and needs no LLM. Measured on E22: 351/358 segments, 664/3160
word tokens punctuated; unpunctuated cards fell from 100% to 5.8%.

### 2. `CHUNK = 60` makes every ASR window start cold

`gen_dubtitles.py:134`. A 60-second window begins mid-sentence, so the first segment comes
back lowercase and unpunctuated even when the model punctuates correctly. It was set to 60 s
to stop a 5-minute chunk containing the OP from making whisper loop on the song — the workaround
for one failure became a whole-library quality ceiling.

**Fix:** 240-300 s chunks with `vad_filter=false`. Measured on E22: a 400 s request returned
93 punctuated segments and no loop.

### 3. The glossary never ran on the batch path

`glossary.correct()` is applied by the merge pass; `gen_dubtitles.py` writes
`.eng.dubtitles.srt` and never calls it. The NGE glossary file existed the whole time — it
just has no `Oscar`, `Asakura` or `sink ratio` rule, and nothing checked the output for names
it did carry.

### 4. Adoption is ungated

`ADOPT_DUB_EN=1` feeds a foreign `.dub.en.srt/.ass` through repair → signs → mux. Nothing
compares the result to the episode: no coverage check, no cue-length check, no
punctuation/case check, no name check. E23 was published at 40% coverage with
`"outcome": "ok"`.

**Fix:** a publish gate before mux. Refuse to write the sidecar (and `dubtitles.done`) when:

* last cue end < 90% of container duration
* a known mishear name appears (`Oscar`, `Ava`, `Asakura`, `sink ratio`, …) — from the glossary's `hard_fixes`
* a line exceeds 2×42 characters, or a card exceeds 7 s, or cards < 6 per minute of runtime
* more than 15% of cards are lowercase orphans (start lowercase, previous card takes no terminal punctuation)

### 5. A cheaper-ASR fallback silently degrades the whole deliverable

`srt_to_ass.py` (used when the merge pass has no template) writes one bare style and no
signs. E23's sidecar is `Trebuchet MS,64`, while the other 25 episodes in the season carry the
fansub template with `Default/BG/Signs/Japanese Lyrics/English Lyrics/Title Cards2/Fancy Signs`
and a `Dubtitles` style. A fallback that cannot preserve the template should refuse and say so,
not publish a visually different file.

## Operational note: OpenRouter whisper-1 cost reservation

`POST /api/v1/audio/transcriptions` with `timestamp_granularities[]=word` reserves the
model's *maximum* cost (1080 min × $0.006 ≈ $6.48) against the key, regardless of the clip
length. A 20-second clip returns `402 weight_exceeds_budget` on a key with $6.36 left.
Segment-only requests reserve far less and pass. Request `segment` granularity, derive cards
from segments, and chunk at 300 s; add `word` only when sub-segment timing is genuinely
needed.

## Reproduction

```bash
# the published E22 card
grep -c 'Dialogue: 0,[^,]*,Dubtitles' "…S01E22….dub.en.ass"   # 675
# how many of them end in . ! ? …
python3 - <<'EOF'
import re
txt = open("…S01E22….dub.en.ass").read()
cues = [re.sub(r"\{[^}]*\}", "", l.split(",", 9)[9]).strip()
        for l in txt.split("[Events]")[1].splitlines() if l.startswith("Dialogue:")]
print(sum(1 for c in cues if c[-1] not in ".!?…"), "of", len(cues), "unpunctuated")
EOF
```
