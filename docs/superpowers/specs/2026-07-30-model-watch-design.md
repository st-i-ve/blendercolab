# Model Watch — keeping the Frappe AI design doc current

**Date:** 2026-07-30
**Target:** `docs/frappe-ai-assistant/frappe-ai-assistant-design.html`

## Problem

The design doc's model tables (§4.1 candidates, §4.5 tier ladder, §7.5 OCR) are
hand-written snapshots. They went stale without anyone noticing: `baidu/Unlimited-OCR`
was released 2026-06-19 and is now the most-liked OCR model on Hugging Face, and the
doc does not mention it.

Two different needs are tangled in "keep it current":

- **Metadata** — what exists, how popular, released when. A public API answers this.
- **Judgement** — does it fit a 16 GB T4, an open license, vLLM serving, and our
  OCR / NL→SQL goals? No API answers this. Unlimited-OCR is the proof: the API says
  "#1 by likes"; it does not say "needs a custom Docker image and has unproven
  Turing support."

The design covers both, with separate mechanisms.

## Part 1 — Live panel (new §4.6)

A self-contained section appended after §4.5, inside the existing HTML. Inline JS,
no dependencies, no API key, no build step.

### Queries

Two calls, both public and CORS-enabled:

```
GET https://huggingface.co/api/models?search=OCR&sort=likes&direction=-1&limit=30
GET https://huggingface.co/api/models?pipeline_tag=image-text-to-text&sort=likes&direction=-1&limit=30
```

**Sort by `likes`, not `createdAt`.** Verified 2026-07-30: date-sorting
`image-text-to-text` returns near-pure noise — LoRA adapters, DPO checkpoints,
auto-dated junk repos — and does not surface real releases at all. Like-sorting the
`search=OCR` lane puts Unlimited-OCR at rank 1 and returns genuine models throughout.

### Available fields

Confirmed present on each object: `id`, `likes`, `downloads`, `tags`, `pipeline_tag`,
`library_name`, `createdAt`, `private`, `_id`, `modelId`.

**Not available:** parameter count, and no `lastModified`. The panel therefore
displays id / likes / downloads / created date only. Parameter counts stay in the
hand-curated tables where a human put them.

### Rendering

- Merge both lanes, dedupe by `id`, drop `private: true`.
- Drop derivative noise by id regex:
  `/uncensored|abliterated|crack|gguf|lora|dpo|imatrix|-i1-/i`.
- Sort by `createdAt` descending; badge anything under 90 days old as **NEW**.
- Show a `last checked: <local time>` line.

### Failure path

This matters more than the happy path. The doc is opened over `file://` and a PDF
copy sits beside it.

The HTML ships a hand-written snapshot table visible in the markup. The JS *replaces*
it on success. On any failure — offline, CORS, CSP, PDF export — the reader sees the
snapshot plus `couldn't reach Hugging Face — snapshot from <date>`. It must never
render an empty box or a hanging spinner.

### Explicit non-goal

The panel reports popularity and recency. It cannot say a model is good, will fit a
T4, or is servable. That is Part 2's job.

## Part 2 — Weekly agent

A scheduled routine, weekly, with a fixed brief.

### Gate

A candidate is added only if it clears all four:

1. Open license (MIT / Apache-2.0 / similar — no research-only or gated weights)
2. Fits 16 GB VRAM at some published quantization
3. Servable by vLLM, or by a documented alternative the runbook can adopt
4. Relevant to OCR or NL→SQL

### Writes

- Survivors get a row appended to §4.5 (ladder) or §7.5 (OCR), in the existing
  column format, including known caveats.
- Every run appends one dated line to an append-only **"Model watch log"** subsection
  in the §12 Appendix — *including* "nothing worth adding," so a silent week is
  distinguishable from a broken cron.
- Additions only. The agent never edits or deletes existing rows.
- One git commit per run, so each week is a reviewable diff.

## Part 3 — Seeding

`baidu/Unlimited-OCR` is added to §7.5 by hand as part of this work:

| Field | Value |
|---|---|
| Repo | `baidu/Unlimited-OCR` |
| Released | 2026-06-19 (HF), announced 2026-06-22 |
| Size | 3B total, MoE, ~500M active |
| License | MIT |
| Lineage | continue-trained from DeepSeek-OCR |
| Benchmark | 93.23 OmniDocBench v1.5 / 93.92 v1.6 |
| Context | 32K, flat KV cache via Reference Sliding Window Attention |
| VRAM | ≥8 GB BF16 |

Both caveats ship with it:

- **Not in a pip wheel.** vLLM support (28 Jun 2026) ships only via
  `vllm/vllm-openai:unlimited-ocr` and needs
  `--logits_processors vllm.model_executor.models.unlimited_ocr:NGramPerReqLogitsProcessor`,
  `--no-enable-prefix-caching`, `--mm-processor-cache-gb 0`, `--trust-remote-code`.
  The Studio's pip vLLM 0.25.1 will not serve it. **Open question: whether Lightning
  Studios permit Docker.**
- **T4 (sm75) support unverified.** The recipe documents an `fa3` attention backend,
  which is Hopper-class. Nobody documents graceful degradation on Turing. Treat as a
  scorecard experiment, not a given.

Per-request args: `vllm_xargs: {ngram_size: 35, window_size: 128}` — `1024` for
multi-page PDF — plus `skip_special_tokens: false`.

## Version control

`blendercolab/` is a git repo on `main` (remote `github.com/st-i-ve/blendercolab`),
but `docs/` was entirely untracked. Committed as baseline in `8cc758c` so the doc has
history to diff and revert against before anything automated edits it. Not pushed —
the remote is public and that is the owner's call.

## Out of scope

- Auto-editing §4.1 (narrative prose, not a table)
- Any change to the runbook's serving steps beyond the §7.5 row
- Benchmarking any new model; the agent reports, the harness scores
