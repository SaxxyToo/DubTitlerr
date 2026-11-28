# Choosing a repair model

Understanding-oriented. `REPAIR_MODEL` is the small instruct model that rewrites the lines the
pipeline was unsure about. It is a separate decision from [Choosing an ASR model](Choosing-an-ASR-Model.md),
and from [choosing a quantisation](How-To-Guides.md#choose-a-quantisation-for-your-card), which is
about fitting whatever you pick into the VRAM you have left.

If you want a value looked up rather than argued, see [Reference](Reference.md#repair).

---

## What the job actually is, because it changes the answer

The repair stage does not rewrite subtitles. It is handed a line the pipeline doubted — low
confidence, or a proper noun that appears in neither the glossary nor the original — plus the lines
either side for context, and it proposes a correction. Three properties of that job decide which
models are suitable:

1. **The task is dominated by names.** Long-tail proper nouns and transliterated Japanese names are
   where the errors are. A model that is strong on general reasoning but weak on rare-name recall
   will lose to a smaller model that is better at it.
2. **The output must be constrained.** A model that rewrites a line it was not asked to touch is
   worse than one that returns nothing, because a wrong "improvement" ships. The prompt asks for a
   minimal edit; the acceptance gate downstream rejects one that does not fit.
3. **It runs one line at a time, on a card that is probably also doing something else.** Throughput
   at batch scale is irrelevant. Latency per request and resident VRAM are what matter.

## What this project runs

`qwen3-4b-instruct`, served by `llama-server`, at `Q6_K`. The instruction-tuned variant has no
thinking mode, so it needs no `enable_thinking` flag, and it returns clean completions without one
(verified 2026-09-02). If you swap in a thinking model — `qwen3-4b`, or any `qwen3.5-*` — the server
**must** run with `--jinja` and thinking must be turned off, or the model spends its whole token
budget on reasoning and returns empty content, which the pipeline reads as "no repair" and tells you
nothing. See [Your first show](Your-First-Show.md#step-1--serve-the-repair-model) for the measured
version of that failure.

## What the landscape looks like now (November 2026)

The 3B–8B instruct class has moved a long way since `qwen3-4b-instruct` shipped. All of these are
Apache 2.0 and all of them handle a 4–8K-token prompt (a whole subtitle file plus a glossary) without
strain — the native contexts are 128K to 262K, far past anything this pipeline sends.

| Model | Released | Size | Notes |
| --- | --- | --- | --- |
| `Qwen3-4B-Instruct-2507` | Jul 2025 | 4B dense | The direct refresh of what you run today. 262K context. |
| `Qwen3.5-4B` | Feb 2026 | 4B dense | IFEval 89.8, MMLU-Pro 79.1 on the model card. 262K context. Vision-capable, and its 248K-token padded embedding table makes GGUFs larger than the parameter count suggests. |
| `Qwen3.5-9B` | Feb 2026 | 9B dense | IFEval 91.5, MMLU-Pro 82.5. The step up if you have ~9 GB free. |
| `Gemma 4 E4B` | Mar 2026 | 4.5B effective | Apache 2.0, unlike Gemma 3. Google's own table: 4.5 GB at Q4_0. 128K context, 262K vocab. |
| `Granite 4.2 8B` | 2026 | 8B dense | 128K context; IBM lists structured JSON output as a first-class capability. |
| `Ministral 3 8B` | Dec 2025 | 8B dense | Explicitly tuned for edge deployment. |
| `Phi-4-mini-instruct` | Feb 2025 | 3.8B | MIT. A reasonable CPU-budget choice. |
| `SmolLM3` | Jul 2025 | 3B | Apache 2.0. For very tight CPU budgets. |

The figures above are vendor or third-party claims from the cited release dates, not measurements
taken here. What has *not* moved: nothing in this class is meaningfully better at the specific thing
this pipeline needs than the Qwen line, and the Qwen tokenizer's handling of transliterated Japanese
names is a real advantage for this job.

## What the research says about the names specifically

This is the part worth reading before changing anything. Several papers attack the same problem —
recovering a proper noun the recogniser mangled — and they converge on the same answer: **put the
candidate spellings in the prompt.** A small model cannot invent a rare transliteration it has never
seen, but it can pick from a list.

- Giving the LLM a dictionary of phonetically and graphemically similar candidate names beats plain
  instruction prompting on cross-lingual proper nouns (DiP-ASR, EMNLP 2024 Findings).
- Supplying phonetic and semantic candidate matches from a local glossary cut named-entity error
  rate by up to 30% relative (deployed in Taiwan News, June 2025).
- Retrieval-style entity correction reports 33–39% relative error reduction on rare entities.
- RECOVER (2026) constrains the model to *entity-only* find/replace edits, with deterministic
  guardrails and a second critic pass — which is close to what this pipeline does.

Two consequences for your prompt, both already the house pattern: enumerate the glossary candidates
rather than asking the model to recall them, and constrain the edit to entity swaps and punctuation
so the model cannot rewrite a line it was not asked about.

## The constrained-output trap

Every serving stack here can force schema-valid JSON, and it is tempting to lean on that. A
15,000-generation study published in 2026 found hard schema decoding raised output validity from
61.5% to **100%** while answer accuracy *fell* from 19.7% to **11.0%** — wrong-but-valid outputs rose
from 49.5% to 88.9%. Other work measures 3.6–8.2× latency overhead from hard constraints, and finds
the semantic gap widens as models get smaller.

So: use `response_format` as a parse-safety net, keep the schema minimal (the edited line, and
whether anything changed), and let the deterministic acceptance gate do the judging. Never let the
grammar force the model to "answer" a line it should have left alone.

## Serving notes

- **`llama-server`** is `--jinja` by default now; the chat template is read from the GGUF's metadata.
  Two traps remain: a third-party GGUF with a stripped or wrong template silently breaks roles and
  tool calling, and the JSON schema you pass to `response_format` is **not** injected into the prompt
  — describe the expected structure in the prompt yourself.
- **Ollama** takes `format: "json"` or a schema, and exposes the same through `response_format` on its
  OpenAI-compatible route. No raw grammar control.
- **vLLM** is the right answer if you ever batch many files; it is overkill for one line at a time.

## Recommendation

**Stay on the Qwen line.** Move to `Qwen3.5-4B` at `Q5_K_M` or `Q6_K` when convenient — it is a
straight upgrade on the same footprint — or stay on `Qwen3-4B-Instruct-2507` and lose nothing
important. Prefer the higher quantisation: correct-name output is long-tail knowledge, which is
exactly what low-bit quantisation damages first. Budget roughly 3–4 GB of weights plus under 1 GB of
KV cache at an 8K prompt.

Only reach for `Gemma 4 E4B` or `Granite 4.2 8B` if you have a specific reason — an official quant
table you want to follow, or structured output you would rather not prompt for. Neither is a
measured winner here, because nobody has run them through this pipeline yet.

## Measured standings (2026-11-27)

Everything above this section is vendor claims. These are the project's own numbers: the anchored
Chainsaw Man S01 bench (406 judged lines per round), every saved proposal re-judged by the
acceptance gate this codebase ships, then split by edit kind —

- **glyph** — case/punctuation-only (renders near-identically),
- **contra** — contraction expansion (`don't` → `do not`),
- **borrow** — imported a word the fansub reference carries,
- **edit** — a real repair. **This is the comparable column.** Raw "admitted" counts rank
  punctuation churn first and misled an earlier round; split them.

`tools/repair_standings.py` reproduces the table from any round's saved outputs (counts only —
no subtitle text is printed or written).

### Local models, production prompt

| model | edit | borrow | note |
| --- | ---: | ---: | --- |
| `z5-granite-4.0-h-1b` (Q5-class) | **34** | 22 | the bench leader; keeps names; heavy glyph churn |
| `cand-gemma-3-1b` | 30 | 15 | name-strong; heavy contraction churn |
| `exaone-4.0-1.2b` | 28 | 26 | borrow-heavy |
| `falcon-h1-1.5b` | 24 | 33 | borrow-heavier still |
| `aion-2.3b` | 13 | 21 | needs a patched llama.cpp — no upstream arch support |
| `qwen3.5:4b` | 10 | 26 | the production default; see the end-to-end note below |

Two measured notes on top of the table:

- **Quantisation: no upgrade.** The leader at Q5-class posts 34 edits; Q6_K 24 and Q8_0 28.
  More bits did not buy more (or safer) work — the live Q5-class file stays.
- **The fansub reference in the prompt is net-positive.** Hiding it collapses borrowing for every
  model but also drops name fixes; keep it, and let the gate police the copies (measured
  2026-11-27).

### Hosted models, capable prompt (round 7, re-scored)

| model (OpenRouter) | edit | borrow | note |
| --- | ---: | ---: | --- |
| `llama-4-scout` | **65** | 76 | most active; most risk (hand-read found wrong-name swaps) |
| `qwen3.7-flash` | 45 | 49 | 25 empty errors — needs reasoning-budget handling |
| `gpt-oss-120b` | 36 | 43 | cleanest hand-read of the hosted set |
| `gpt-5-nano` | 29 | 34 | clean runner |
| `claude-haiku-4.5` | 23 | 64 | copies the reference heavily |

For scale: under the same capable prompt the local models manage 20 (`cand-gemma-3-1b`) and 14
(`qwen3.5:4b`) edits, so the hosted leaders genuinely out-work them — at heavier borrow and with
hand-judge caveats. The earlier rounds where hosted models "lost" failed on three mechanical
shapes, not quality: outputs too verbose for a subtitle line (`length_ratio` refusals), the
fansub treated as the answer (`ref_borrow`), and reasoning models truncating to empty replies.

### What is not settled

- **End-to-end, the delivered artifact decides.** The 2026-11-25 season A/B kept `qwen3.5:4b`
  as the default over `cand-gemma-3-1b` despite the bench gap — gemma's bench activity did not
  survive the end-to-end name check (corruptions the mechanical gate cannot see). A
  `z5-granite-4.0-h-1b` end-to-end A/B is the standing next test.
- **One show so far.** Every number above is Chainsaw Man S01. A second show is the next
  confirmation round.
- **The review queue is load-bearing.** Whole classes (wrong-name swaps, tone flips) pass the
  mechanical gate by design; the standings measure edit volume and type, not truth against the
  dub.

## Sources

Model cards and release notes: [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507),
[Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B), [Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B),
[Gemma 4](https://ai.google.dev/gemma/docs/core/model_card_4/),
[Granite 4.2](https://www.ibm.com/granite/docs/models/granite4-2),
[Mistral models](https://docs.mistral.ai/models).
Serving: [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md),
[llama.cpp grammars](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md),
[Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs),
[vLLM structured outputs](https://docs.vllm.ai/en/stable/features/structured_outputs/).
Name correction: [DiP-ASR](https://aclanthology.org/2024.findings-emnlp.399.pdf),
[candidate-list revision](https://arxiv.org/abs/2506.10779),
[retrieval-style entity correction](https://arxiv.org/abs/2409.06062),
[RECOVER](https://arxiv.org/abs/2603.16411).
Constrained decoding: [the constraint tax](https://arxiv.org/abs/2605.26128),
[structural vs semantic](https://arxiv.org/abs/2609.23742),
[latency](https://arxiv.org/abs/2605.02363).
Long context: [RULER](https://arxiv.org/abs/2404.06654), [OneRuler](https://arxiv.org/abs/2503.01996),
[quantisation and long-context retrieval](https://arxiv.org/abs/2505.20276).
