# Upload Pipeline, Auto-Sharing, Telemetry and UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Supersedes `2026-08-02-upload-pipeline.md`, which stated that dataset sharing was impossible via the API. **That was wrong** — it was concluded from RPC *names* without reading what the request bodies carry. Sharing is automatable and proven; see Measured Facts.

**Goal:** Make the 63 MB upload reliable and visible, share one upload with friends automatically, show live per-GPU telemetry from running kernels, and reskin the app in a dark Discord-like shell with friendly messages.

**Architecture:** `blendfleet/uploader.py` drives Kaggle's resumable blob protocol directly — bounded chunks, own retry/backoff, progress callback — replacing `KaggleApi.upload_files`. `blendfleet/sharing.py` grants collaborators via `update_dataset_metadata`. The render notebook emits telemetry on the existing SSE channel. The UI becomes a dark shell with a left account rail and live charts.

**Tech Stack:** Python 3.11+, `kagglesdk`, `requests`, PySide6 (QtCharts for graphs), pytest.

## Global Constraints

- Python 3.11+; all paths via `pathlib`.
- **No OS branching outside `blendfleet/platform_paths.py`** — a grep guard enforces this; keep it passing.
- **No network in unit tests.** Transports and clients are injected.
- **Never write `os.environ["KAGGLE_API_TOKEN"]` outside the lock-guarded `_with_env_token`.** A prior Critical had threads authenticating as each other.
- **Nothing may fail silently.** This branch exists because a swallowed `None` surfaced as a 400 three layers away.
- **All network I/O runs off the UI thread.** Uploads are the longest calls in the app.
- Every module gets a matching test file under `tests/`.
- No `Co-Authored-By` trailers on commits.

## Measured facts

| Fact | Evidence |
|---|---|
| Kaggle PUTs the **entire file in one request** | `kaggle_api_extended.upload_complete()` |
| Its `Retry(total=10)` cannot replay a consumed stream | urllib3 semantics; body is a file reader |
| `_upload_blob` gives up and returns `None`, dropping the file | `_upload_blob()`; yields `400 "Please upload at least one file"` |
| Progress goes to `tqdm` on stdout — invisible in a `console=False` exe | `upload_complete()` |
| **Sharing IS automatable** via `ApiUpdateDatasetMetadataRequest.settings.collaborators` | **proven live 2026-08-03**: added `dansbecker` as READER to a private dataset, read it back, removed it |
| `update_dataset_metadata` **replaces** the whole settings object | omitting `is_private` would silently make a project public; omitting licenses errors |
| It returns `{"errors": [...]}` with **HTTP 200** | success must be read from that array, not the status code |
| `CollaboratorType` = READER, WRITER, OWNER, ADMIN | `users_enums` |
| SSE log streaming works live; `kernels logs`/`output` return nothing until COMPLETE | proven 2026-07-31 |
| `flush=True` is mandatory on any notebook line the app parses | else Python buffers and nothing streams |
| Measured render: 1920×1080, 128 spp → **57.1 s/frame on a Tesla P100** | live run |

---

### Task 0: Settle whether chunks can fly concurrently

**Files:** none committed except a findings note. This is an experiment, not a feature.

The user wants a "highway with several lanes" — e.g. a 100 MB file as 10 MB × 5 **in flight at once**. Resumable protocols normally track a single committed offset and reject out-of-order ranges. **Nothing else in this plan may be built on the assumption that they don't.**

- [ ] **Step 1** — start a real resumable upload session against Kaggle for a ~30 MB throwaway file, using `KAGGLE_API_TOKEN`.
- [ ] **Step 2** — PUT a **middle** range first (e.g. bytes 10 MB–20 MB) before byte 0. Record the exact status and body.
- [ ] **Step 3** — PUT two non-adjacent ranges **concurrently** from two threads. Record both responses.
- [ ] **Step 4** — write findings to `docs/upload-concurrency-findings.md`: does the endpoint accept out-of-order ranges? Concurrent ones? What is the observed throughput of sequential 8 MB chunks vs one big PUT?
- [ ] **Step 5** — delete the throwaway dataset and say so. Commit only the findings file.

**Route the outcome:**
- **Out-of-order accepted →** Task 1 implements true parallel lanes.
- **Rejected (expected) →** Task 1 implements sequential bounded chunks, and the "lanes" become concurrency **across accounts** in per-account mode. Say so plainly in the UI rather than implying parallelism that isn't happening.

---

### Task 1: Reliable, visible, resumable uploader

**Files:** create `blendfleet/uploader.py`, `tests/test_uploader.py`

**REVISED BY TASK 0's MEASUREMENTS. Read these before designing anything:**
- Out-of-order ranges are **rejected** — the server aborts the connection, zero bytes committed.
- Concurrent ranges are **rejected** — one in-flight request per session; the others are killed.
- **Chunking is SLOWER**: 8 MB sequential chunks ran 0.59 MB/s vs 0.95 MB/s for one whole-file PUT. Chunking costs ~40% throughput and buys only a smaller retry unit.

So do **not** chunk by default. Keep the single fast PUT and fix what is actually broken about it: invisible progress, ineffective retry, and silent failure.

**Produces:**
- `UploadProgress(uploaded, total, rate_bps, retries, resumed_from)`
- `upload_file(path, session_url, transport, on_progress=None, max_retries=6, progress_interval=1<<20) -> str` returning the blob token
- `UploadError(Exception)` carrying last status and body
- `committed_offset(session_url, total, transport) -> int` — what the server already holds

Must hold:
- **One PUT for the whole remaining range** — full throughput.
- Progress comes from an **instrumented file reader** that fires `on_progress` every `progress_interval` bytes. This is how progress is reported without splitting the request.
- On failure: query `committed_offset`, resume from there with `Content-Range`, retry with exponential backoff **and jitter**. A break at 90% resumes at 90%.
- **Raises on giving up. Never returns None.** The silent `None` is why the user saw a 400 three layers from the cause.
- Transport injected; tests do zero network.

- [ ] **Step 1: failing test** — `FakeTransport` capturing the body and headers. A 20 MB file uploads in ONE PUT; `on_progress` fires repeatedly with monotonically increasing `uploaded` ending exactly at `total`; the returned token is the transport's.
- [ ] **Step 2: run it, confirm it fails**
- [ ] **Step 3: implement**
- [ ] **Step 4: resume tests** — transport fails at 12 MB of 20 MB, then reports `committed_offset == 12 MB`. Assert the retry sends `Content-Range: bytes 12582912-20971519/20971520`, that only the remaining bytes are re-sent (NOT the whole file), and that `resumed_from` is reported so the UI can say "resumed from 12 MB".
- [ ] **Step 5: give-up test** — a transport failing forever raises `UploadError` after `max_retries`, and the message names the last status and body. Assert it never returns None.
- [ ] **Step 6: edge cases** — empty file, single-byte file, a server reporting an offset equal to total (already complete: return the token without re-uploading).
- [ ] **Step 7: full suite + commit**

**Do NOT add a `lanes` parameter.** Task 0 proved concurrency within a session is impossible; a parameter implying otherwise would be a lie in the API. Concurrency belongs across accounts, and in shared mode there is only one upload anyway.

### Task 2: Use the uploader; never submit an empty version

**Files:** modify `blendfleet/kaggle_client.py`, `blendfleet/dataset_sync.py`; extend their tests

- [ ] **Step 1: failing test** — `sync_blend` whose upload yields no token raises a clear local error and **does not call** `dataset_create_version`. Assert the API method was never invoked.
- [ ] **Step 2: implement the preflight**
- [ ] **Step 3: wire `upload_file` in**, threading `on_progress` through `dataset_create`/`dataset_version` (default `None`).
- [ ] **Step 4** — existing 116 tests still pass; the 400 error-message tests must not regress.
- [ ] **Step 5: live check** — upload a ~20 MB throwaway to a scratch dataset, confirm chunked progress and completion, report chunk count and wall time, delete the scratch dataset and say so.
- [ ] **Step 6: commit**

---

### Task 3: Automatic private sharing

**Files:** create `blendfleet/sharing.py`, `tests/test_sharing.py`; modify `blendfleet/fleet.py`

Replaces the manual step entirely. One upload to the owner's account; every friend granted READER programmatically; all kernels reference the owner's slug.

**Two traps, both measured — a test must pin each:**
1. `update_dataset_metadata` **replaces** the settings object. Send title, `is_private=True` and exactly one license every time. **Omitting `is_private` would publish someone's private work.**
2. It returns `{"errors": [...]}` with **HTTP 200**. Success means that array is empty.

**Produces:** `grant_readers(client, owner, slug, usernames, current_settings) -> None`, `list_collaborators(client, owner, slug) -> list[tuple[str, str]]`

- [ ] **Step 1: failing tests** — granting sends `is_private=True` and exactly one license; a response with a non-empty `errors` array raises rather than being treated as success; existing collaborators are preserved, not clobbered.
- [ ] **Step 2: implement**
- [ ] **Step 3: wire into `fleet.launch`** — shared mode uploads once, grants each account's username READER, and all workers use the owner slug. Assert in a test that exactly one upload happens for N accounts.
- [ ] **Step 4: verify access before launching** — a friend not yet granted fails at kernel run time with an opaque error. Check reachability per account up front and refuse with a message naming who lacks access.
- [ ] **Step 5: live check** — grant and revoke against a throwaway dataset, confirm by reading back. Leave the account as found; report what you touched.
- [ ] **Step 6: commit**

---

### Task 4: Live GPU telemetry

**Files:** modify `blendfleet/notebook_builder.py`, `blendfleet/log_stream.py`; create `tests/test_telemetry.py`

The notebook already runs `nvidia-smi` and the SSE channel already carries `PROGRESS` lines. Emit telemetry on the same channel: **per GPU**, not aggregated.

Emit every ~5 s during rendering:
`TELEMETRY gpu=0 util=87 mem_used=6144 mem_total=15360 temp=71 power=58 flush=True`

- [ ] **Step 1: failing test** — `parse_telemetry` returns a per-GPU record from a real SSE line; ignores `PROGRESS` lines and malformed input; handles **two GPUs reporting independently**.
- [ ] **Step 2: implement the parser** alongside `parse_progress`, same conventions.
- [ ] **Step 3: emit from the notebook** — a background thread sampling `nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw`. **`flush=True` is mandatory** or nothing streams. It must not interfere with the render, and must degrade silently if `nvidia-smi` is absent (CPU sessions).
- [ ] **Step 4: verify the generated notebook still parses** (`ast.parse` every code cell) and the existing notebook tests pass.
- [ ] **Step 5: commit**

---

### Task 5: Discord-style UI with charts

**Files:** create `blendfleet/ui/theme.py`, `blendfleet/ui/charts.py`, `blendfleet/ui/upload_view.py`; modify `dashboard.py`, `setup_dialog.py`

Dark shell, left rail of accounts, main pane with live charts. **Do not ship a half-restyled app** — one coherent theme applied everywhere, including dialogs.

**Load the `frontend-design` skill before starting this task.** Match its guidance on making deliberate choices rather than defaults.

Required views:
- **Left rail** — accounts with status dot (verified ✓ / unverified ✗ / checking …). **Never colour alone**: symbol plus label, and amber rather than red for failure, since red/green is the most common colour-blindness pair.
- **Upload view** — per-account bars with bytes, %, speed, ETA, retry count, and resumed-from. The user must be able to tell **slow** from **stuck**; that is the entire point.
- **GPU panel** — per-GPU utilisation and memory sparklines from Task 4, one row per physical GPU.
- **Render progress** — frames done/total, per account.

Message rewrite: every user-facing string states **what happened, why, and what to do next**. Current example of the target register, already shipped: *"the .blend file did not finish uploading to Kaggle, so the request was submitted with no file attached… retry the render."* Never surface a bare status code.

- [ ] **Step 1** — pure formatting helpers (`format_bytes`, `format_rate`, `format_eta`) with unit tests including zero-elapsed and zero-rate, so ETA never shows `inf` or divides by zero.
- [ ] **Step 2** — `theme.py`: one stylesheet, one palette, applied at `QApplication` level so no widget is left unstyled.
- [ ] **Step 3** — the upload view, fed by signals from a worker thread.
- [ ] **Step 4** — GPU charts fed from the telemetry stream.
- [ ] **Step 5** — audit every user-facing string; rewrite any that leak a status code or a Python exception.
- [ ] **Step 6** — offscreen verification of each view through its states, including failure. Report exactly what you ran.
- [ ] **Step 7** — full suite + commit

---

### Task 6: Rebuild and verify the exe

- [ ] **Step 1** — `powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1`. It refuses the wrong interpreter and fails on a suspiciously small binary; do not weaken those guards.
- [ ] **Step 2** — confirm the bundle contains PySide6/kaggle/kagglesdk by scanning `build/blendfleet/blendfleet.pkg`, **not** by launching the app — Kaggle imports are lazy, so a window opening proves nothing.
- [ ] **Step 3** — report size and counts.

---

## Out of scope

- Public-dataset mode — rejected: it would make the `.blend` permanently downloadable.
- Rewriting non-upload network calls to be async. Uploads are the longest and are covered here; the rest remains a known follow-up.

## Self-Review

**Coverage:** breakage → Tasks 0–2; visual guide → Tasks 3 and 5; automatic sharing → Task 3; GPU states → Tasks 4–5; Discord UI and friendly messages → Task 5; shippable artefact → Task 6.

**Sequencing is load-bearing.** Task 0 gates Task 1's design. Task 4 gates Task 5's GPU panel. Do not build a chart for data that is not being emitted yet.

**The riskiest remaining assumption:** that Kaggle accepts arbitrary `Content-Range` boundaries at all. Their code only ever sends one range, so multi-chunk behaviour is inferred. Task 0 settles it; if mid-file ranges are rejected outright, the fallback is whole-file PUTs with our own retry, backoff and progress — which still fixes silent failure and invisibility, just not the retry-unit size.
