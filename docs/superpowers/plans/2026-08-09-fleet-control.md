# Fleet Control — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Six things the user hit in real use — per-instance cancel, actually using both GPUs, seeing *why* an instance failed, per-instance download with progress, downloading one archive instead of hundreds of files, and checking hardware before committing a session to a render.

**Architecture:** A preflight gate inside the render notebook (same session, before Blender is fetched), archive-on-completion in the kernel, per-worker cancel/collect in `fleet.py`, and failure detail pulled from the kernel log.

**Tech Stack:** Python 3.11+, PySide6, kagglesdk, pytest.

## Global Constraints

- Python 3.11+; `pathlib`. No OS branching outside `blendfleet/platform_paths.py`.
- **No network in unit tests.** `tests/conftest.py` autouse guards block sockets and fail on leaked threads — **do not weaken them**; a live-calling test once aborted ~15% of runs.
- **All network I/O off the UI thread**, via the existing `_CallWorker` pattern.
- **Never write `os.environ["KAGGLE_API_TOKEN"]`** outside the lock-guarded `_with_env_token`.
- Status is never colour-alone: symbol + word, amber not red, in every accent.
- Qt objects torn down deterministically in tests, never left to GC.
- **Archive format is ZIP, not RAR.** RAR's compressor is proprietary — there is no license-free way to *create* one, and Python has no stdlib writer. ZIP is in the stdlib on both sides (kernel and app), so it needs no dependency in either. For already-compressed PNGs, ZIP `STORED` is near-instant and the win is one download instead of hundreds, not byte savings.
- No `Co-Authored-By` trailers on commits.

## Established facts

| Fact | Evidence |
|---|---|
| `enable_gpu` is **DEPRECATED**; the field is `machine_shape` | `ApiSaveKernelRequest` docstring in the installed SDK |
| We currently set only `enable_gpu`, never `machine_shape` | `notebook_builder.py` kernel-metadata |
| A GPU request returned a **single Tesla P100** | live run, 2026-07-30 |
| `kernels_status` carries `failure_message` | live probe |
| Kaggle concurrency: 2 GPU / 5 CPU sessions per account | measured |
| Cancel works via `cancel_kernel_session` (CLI has no such command) | live |
| `flush=True` is mandatory on any notebook line the app parses | measured |
| SSE streaming is the only live log source | measured |

---

### Task 0: What machine shapes exist, and do we get two GPUs?

**Experiment, not a feature.** Nothing else in this plan may assume an answer.

The user believes an instance has 2×16 GB GPUs but their render uses one. Two candidate causes, and they need separating before any code changes:
- **(a)** Kaggle is only giving one GPU, because we set the deprecated `enable_gpu` and never `machine_shape`.
- **(b)** Kaggle gives two, but Cycles is only using one.

- [ ] **Step 1** — find the valid `machine_shape` values. The docstring names only `Tpu1VmV38`; look for others in the SDK, and try plausible ones against a real push. Record exactly what is accepted and what is rejected.
- [ ] **Step 2** — push a **CPU-cheap probe** that prints `nvidia-smi -L`, the full `nvidia-smi` table, CPU count and RAM, under each shape that pushes successfully. Record what hardware each shape actually yields.
- [ ] **Step 3** — if any shape yields 2 GPUs, run a **short real Blender render** on it and capture `nvidia-smi` utilisation for **both** GPUs during the render, plus Blender's own `[setup]` line listing enabled devices. This is what separates (a) from (b).
- [ ] **Step 4** — if two GPUs are present but only one is busy, investigate Cycles: are both enabled in preferences, and does a single frame actually split across devices? Note that Cycles splits *within* a frame; at very low resolution/samples the second GPU may finish nothing measurable, so use a workload big enough to be conclusive.
- [ ] **Step 5** — write `docs/machine-shape-findings.md`: valid shapes, hardware each yields, whether both GPUs are used, and the recommended default. Delete throwaway datasets/kernels and say what is left.

**Route the outcome:** Task 1 sets `machine_shape` to whatever actually yields the best hardware; if no shape yields 2 GPUs for this account tier, say so plainly in the UI rather than implying two.

---

### Task 1: Request the right machine, and prove both GPUs work

**Files:** `blendfleet/notebook_builder.py`, `tests/test_notebook_builder.py`, possibly `blendfleet/settings.py`

- [ ] **Step 1: failing test** — generated `kernel-metadata.json` carries `machine_shape` (the value Task 0 established) and no longer relies on the deprecated `enable_gpu` alone.
- [ ] **Step 2: implement**, keeping `enable_gpu` too if Kaggle still honours it, since dropping it on an older backend would silently produce a CPU session.
- [ ] **Step 3** — the notebook's `[setup]` line must report **every** enabled device with its name, so a single-GPU session is visible in the log rather than inferred from slow renders.
- [ ] **Step 4** — if Task 0 found a Cycles-side cause, fix it here and pin it with a test on the generated setup script.
- [ ] **Step 5** — full suite + commit

---

### Task 2: Preflight the session, then render or stop

**Files:** `blendfleet/notebook_builder.py`, `blendfleet/log_stream.py`, `blendfleet/ui/`, tests

The user's idea, and it is a good one: **the kernel is going to start anyway**, so report the hardware in the first seconds of the session — before Blender is downloaded — and then either continue into the render or end the session. That gets real hardware facts without a separate probe, and without wasting a long session on the wrong machine.

- [ ] **Step 1: failing test** — a `PREFLIGHT` line is emitted with GPU names/count, CPU count and RAM, **before** the Blender download begins, and `parse_preflight` in `log_stream.py` turns it into a record. `flush=True` is mandatory or nothing streams.
- [ ] **Step 2: implement** the preflight block at the very top of the generated notebook.
- [ ] **Step 3: minimum-hardware gate** — if the session does not meet a configured minimum (e.g. "requires a GPU", "requires ≥2 GPUs"), the notebook prints why and **exits before downloading Blender or the .blend**, so a wrong machine costs seconds, not the whole session.
- [ ] **Step 4** — surface preflight in the instance card the moment it arrives: the card stops being "starting…" and shows real hardware within seconds of launch.
- [ ] **Step 5** — full suite + commit

---

### Task 3: Per-instance cancel

**Files:** `blendfleet/fleet.py`, `blendfleet/ui/dashboard.py`, tests

Today cancel is all-or-nothing. The user wants to stop one instance — e.g. one friend's machine is slow or they need their quota back — without killing the fleet.

- [ ] **Step 1: failing test** — `cancel_worker(label)` cancels exactly that worker and **leaves the others running** (assert the other accounts' cancel was never called). Cancelling an already-finished worker is a no-op, not an error.
- [ ] **Step 2: implement**, reusing the existing per-account failure reporting — a cancel that fails must say so, because its whole purpose is stopping someone's quota draining.
- [ ] **Step 3** — per-card cancel control in the UI, confirming before it fires, disabled while in flight, and **never shown for a worker that is not running**.
- [ ] **Step 4** — full suite + commit

---

### Task 4: Show why an instance failed

**Files:** `blendfleet/kaggle_client.py`, `blendfleet/fleet.py`, `blendfleet/ui/`, tests

"Error" with no reason is the complaint. `kernels_status` already returns `failure_message`, and the kernel log holds the actual traceback — a Blender crash, an OOM, a missing file.

- [ ] **Step 1: failing test** — a failed worker surfaces `failure_message` when present; when it is empty, the **tail of the kernel log** is fetched and shown instead. Assert the log fetch happens only for failed workers, never on the polling path for healthy ones.
- [ ] **Step 2: implement.** Log retrieval is a network call — it goes through `_CallWorker`, off the UI thread, and is fetched **once per failure**, not on every poll.
- [ ] **Step 3** — the card shows a one-line cause with a way to see the full log. Distinguish the common cases in plain language: out of memory, Blender crashed, file missing, session timed out, quota exhausted.
- [ ] **Step 4** — full suite + commit

---

### Task 5: Archive the frames in the kernel

**Files:** `blendfleet/notebook_builder.py`, `blendfleet/collector.py`, tests

Downloading 250 PNGs individually is slow and fragile. Zip them in the kernel; download one file; extract locally.

- [ ] **Step 1: failing test** — the generated notebook zips `/kaggle/working/frames` into a single archive named per worker, and the collector extracts it and reports the same `missing_frames` as before. **The missing-frame guarantee must survive the format change** — a partial render must still read as partial.
- [ ] **Step 2: implement.** Use `zipfile` with `ZIP_STORED` for PNG/JPEG (already compressed; deflate costs time for ~nothing). Write the archive **as frames complete or at the end**, but never in a way that loses frames if the session is cut short — if the archive is only written at the end, a timeout loses everything, so keep the loose frames as fallback and prefer the archive when present.
- [ ] **Step 3** — collector handles: archive present, archive absent (fall back to loose files), archive corrupt (report it, do not crash).
- [ ] **Step 4** — full suite + commit

---

### Task 6: Per-instance download with progress

**Files:** `blendfleet/collector.py`, `blendfleet/kaggle_client.py`, `blendfleet/ui/`, tests

- [ ] **Step 1: failing test** — `collect` can target a single worker by label; downloading reports progress; a failure on one instance does not abort the others.
- [ ] **Step 2: implement** download progress the same way the uploader does it — an instrumented reader, not a chunked request, since chunking measured slower on the upload side and there is no reason to expect otherwise here.
- [ ] **Step 3** — per-card download control plus a fleet-wide one; progress shown per instance with bytes, rate and ETA, reusing the existing `formatting` helpers.
- [ ] **Step 4** — full suite + commit

---

### Task 7: Rebuild and verify the exe

- [ ] **Step 1** — `powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1`. Its guards refuse the wrong interpreter and fail on a small binary; do not weaken them.
- [ ] **Step 2** — verify the bundle by scanning `build/blendfleet/blendfleet.pkg`, **not** by launching the app: Kaggle imports are lazy, so a window opening proves nothing.
- [ ] **Step 3** — report size and module counts.

---

## Out of scope

- RAR. Proprietary compressor, no license-free writer, no stdlib support. ZIP achieves the actual goal — one download instead of hundreds.
- Cancelling an individual **GPU** within one session. Kaggle's unit of control is the session; there is no API to release one GPU and keep the other. Per-instance cancel is the real equivalent.

## Self-Review

**Coverage:** per-instance cancel → Task 3; two GPUs → Tasks 0–1; failure reasons → Task 4; per-instance download + progress → Task 6; archive → Task 5; preflight-then-continue → Task 2.

**Sequencing is load-bearing.** Task 0 gates Task 1 — do not set a `machine_shape` before knowing which values are valid and what they yield. Task 5 gates Task 6: the collector must know whether it is fetching an archive or loose files before download progress is wired to it.

**The likeliest quiet failure** is Task 5 losing frames. Today every frame is uploaded as it completes, so a timeout keeps everything finished so far. An archive written only at the end would throw that away on the exact runs where it matters most — a long render that gets cut off. The fallback path is not optional.
