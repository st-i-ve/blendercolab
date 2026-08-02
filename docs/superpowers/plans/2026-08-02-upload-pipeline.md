# Upload Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make uploading a 63 MB `.blend` to Kaggle reliable, visible, and — where the user opts in — done once instead of once per account.

**Architecture:** A `blendfleet/uploader.py` that drives Kaggle's resumable blob protocol directly in bounded chunks with its own retry/backoff and a progress callback, replacing reliance on `KaggleApi.upload_files`. `dataset_sync` gains a preflight that refuses to submit a version RPC unless a file token actually exists. The dashboard gains a per-account upload view fed from a worker thread. A shared-dataset mode lets one upload serve several accounts.

**Tech Stack:** Python 3.11+, `kagglesdk` blob RPCs, `requests`, PySide6, pytest.

## Global Constraints

- Python 3.11+; all paths via `pathlib`.
- **No OS branching outside `blendfleet/platform_paths.py`.** Task 11 of the previous plan added a grep guard; keep it passing.
- **No network in unit tests.** The uploader takes an injected transport.
- **Never write `os.environ["KAGGLE_API_TOKEN"]` outside the existing lock-guarded `_with_env_token`.** A prior Critical had threads authenticating as each other; the fix is load-bearing.
- **Nothing may fail silently.** This whole branch exists because a swallowed `None` produced a 400 three layers away.
- Every module gets a matching test file under `tests/`.
- No `Co-Authored-By` trailers on commits.

## Measured facts (2026-08-02)

| Fact | Evidence |
|---|---|
| Kaggle uploads the **entire file in ONE `session.put(url, data=reader)`** | `kaggle_api_extended.upload_complete()` |
| Its `Retry(total=10, backoff_factor=0.5)` cannot replay a consumed stream | urllib3 semantics; the body is a file reader |
| `_upload_blob` gives up after `MAX_UPLOAD_RESUME_ATTEMPTS` and returns `None` | `kaggle_api_extended._upload_blob()` |
| A dropped file yields `400 {"message":"Please upload at least one file"}` | captured live |
| A real interrupted upload leaves `upload_complete: false` in the resumable cache | found on the user's disk for their 66,116,606-byte file |
| Progress goes to `tqdm` on stdout — invisible in a `console=False` exe | `upload_complete()` |
| **No collaborator/sharing RPC exists** in `DatasetApiService` | full RPC listing: create, version, delete, download, get, list, update-metadata, upload-file |
| Resume offset is queried via `_resume_upload` before continuing | `upload_complete(resume=True)` |

**Consequence:** chunking is not about parallelism within a file. Resumable protocols hand out sequential ranges. Chunking matters because it **bounds the retry unit** — an 8 MB chunk that fails costs 8 MB, not 63 — and because it is the only way to report real progress.

---

### Task 1: Chunked resumable uploader

**Files:** create `blendfleet/uploader.py`, `tests/test_uploader.py`

**Produces:**
- `UploadProgress(uploaded: int, total: int, chunk_index: int, chunk_count: int, retries: int)`
- `upload_file(path, start_url, transport, on_progress=None, chunk_size=8*1024*1024, max_retries=5, resume_offset=0) -> None`
- `UploadError(Exception)` — carries the last HTTP status and body
- `probe_offset(url, total, transport) -> int` — how many bytes the server already has

Behaviour that must hold:
- Uploads sequential `Content-Range: bytes {start}-{end}/{total}` PUTs of at most `chunk_size`.
- Each chunk retries independently with exponential backoff and jitter; a chunk failure never restarts the whole file.
- On resume, calls `probe_offset` first and skips what the server already has.
- Calls `on_progress` after every chunk — this is what the UI renders.
- **Raises `UploadError` on giving up. Never returns None, never returns silently.**
- The transport is injected so tests use a fake with zero network.

- [ ] **Step 1: failing test** — `tests/test_uploader.py` with a `FakeTransport` recording `Content-Range` headers. Assert: a 20 MB file at 8 MB chunks issues 3 PUTs with correct, contiguous, non-overlapping ranges covering exactly `0..total-1`; `on_progress` fires 3 times with monotonically increasing `uploaded` ending exactly at `total`.
- [ ] **Step 2: run it, confirm ModuleNotFoundError**
- [ ] **Step 3: implement `blendfleet/uploader.py`**
- [ ] **Step 4: retry tests** — a transport failing chunk 2 twice then succeeding completes with `retries == 2` and re-sends **only** chunk 2 (assert chunk 1 was PUT exactly once). A transport failing chunk 2 forever raises `UploadError` after `max_retries`, and the message names the chunk and the last status.
- [ ] **Step 5: resume test** — `resume_offset=8 MB` on a 20 MB file starts at byte 8388608, not 0, and reported progress accounts for the skipped bytes.
- [ ] **Step 6: edge cases** — empty file, file smaller than one chunk, file exactly one chunk. Assert no zero-length PUT is issued.
- [ ] **Step 7: full suite + commit**

---

### Task 2: Use the uploader, and never submit an empty version

**Files:** modify `blendfleet/kaggle_client.py`, `blendfleet/dataset_sync.py`; extend their tests

The root cause of the user's 400 was submitting a version RPC whose file list was empty. Fix it at two levels: upload reliably, and refuse to submit when the upload did not produce a token.

- [ ] **Step 1: failing test** — `sync_blend` with a stub whose upload yields no token must raise a clear local error and **must not call** `dataset_create_version`. Assert the API method was never invoked.
- [ ] **Step 2: implement the preflight** in `dataset_sync`/`kaggle_client`.
- [ ] **Step 3: wire `upload_file` in**, threading an `on_progress` callback out through `dataset_create`/`dataset_version` (default `None` so existing callers are unaffected).
- [ ] **Step 4: verify the existing 116 tests still pass** — the previous branch's error-message tests for the 400 case must not regress.
- [ ] **Step 5: live check** — with `KAGGLE_API_TOKEN`, upload a ~20 MB throwaway file to a scratch dataset (`bf-upload-test`) and confirm chunked progress and completion. Report the observed chunk count and wall time. Delete the scratch dataset afterwards and say so.
- [ ] **Step 6: commit**

---

### Task 3: Upload progress view

**Files:** create `blendfleet/ui/upload_view.py`; modify `blendfleet/ui/dashboard.py`; `tests/test_upload_view.py`

Per-account rows with bytes, percentage, speed and ETA, plus a total. Resumed uploads show what was already done. The user must be able to tell **slow** from **stuck** — that is the whole point.

```
Uploading remember.blend (63.1 MB)

 you       [########--]  48.2/63.1 MB  2.1 MB/s  0:07
 friend-a  [###-------]  19.4/63.1 MB  0.9 MB/s  0:48
 friend-b  [##########] done (resumed from 41 MB)

            total 130.7/189.3 MB   retry 2/5 on friend-a
```

- [ ] **Step 1** — pure formatting helpers first (`format_rate`, `format_eta`, `format_bytes`) with unit tests, including zero-elapsed and zero-rate cases so ETA never divides by zero or shows `inf`.
- [ ] **Step 2** — the widget, fed by signals. **Uploads MUST run on a worker thread**; the existing dashboard already freezes on network calls and this is the longest call in the app. Reuse the `QThread` pattern from `setup_dialog._VerifyWorker`.
- [ ] **Step 3** — surface retries visibly. A silent retry looks identical to a stall.
- [ ] **Step 4** — offscreen verification driving the widget through progress → retry → done → failure. Report what you ran.
- [ ] **Step 5** — full suite + commit

---

### Task 4: Shared-dataset mode

**Files:** modify `blendfleet/fleet.py`, `blendfleet/accounts.py` or config; tests

**The user chose:** upload once to their own account, share that dataset by hand on kaggle.com with each friend, and have every account's kernel reference the owner's slug. Cuts a 3-account job from 189 MB to 63 MB.

There is **no API for sharing** — verified against the full `DatasetApiService` RPC list. The manual step is unavoidable; the app's job is to make it obvious and to verify it worked.

- [ ] **Step 1: failing test** — in shared mode, `launch` uploads **once** (assert exactly one `dataset_create`/`dataset_version` across all accounts) and every worker's `kernel-metadata.json` lists the **owner's** slug.
- [ ] **Step 2: implement** a per-project mode flag, defaulting to today's per-account behaviour so nothing breaks for existing users.
- [ ] **Step 3: verify access before launching.** A friend who has not been granted access will fail at kernel run time with an opaque error. Check reachability from each account up front (`dataset_exists` against the owner slug using that account's token) and refuse to launch with a message naming exactly who still needs sharing and the URL to do it.
- [ ] **Step 4: setup guidance in the UI** — the manual sharing step, stated plainly, with the dataset URL.
- [ ] **Step 5** — full suite + commit

---

### Task 5: Rebuild and verify the exe

- [ ] **Step 1** — `powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1`. The script now refuses to build with the wrong interpreter and fails on a suspiciously small binary; do not weaken those guards.
- [ ] **Step 2** — confirm the bundle really contains PySide6/kaggle/kagglesdk by scanning `build/blendfleet/blendfleet.pkg`, **not** by launching the app. Kaggle imports are lazy, so a window opening proves nothing.
- [ ] **Step 3** — report the size and the counts.

---

## Out of scope

- Parallel chunks **within** one file. Resumable protocols hand out sequential ranges; concurrency belongs across accounts, and in shared mode there is only one upload anyway.
- Public-dataset mode. Considered and rejected by the user: it would make the `.blend` permanently downloadable.
- Moving all other network I/O off the UI thread. Still a known follow-up; this branch only guarantees it for uploads, which are the longest calls.

## Self-Review

**Coverage:** breakage → Tasks 1+2; visual guide → Task 3; one-upload-for-many-accounts → Task 4; shippable artefact → Task 5.

**The riskiest assumption**, flagged deliberately: that Kaggle's blob endpoint accepts arbitrary `Content-Range` chunk boundaries. `upload_complete` only ever sends one range, so multi-chunk behaviour is **inferred, not observed**. Task 2 Step 5 is the live test that settles it. If the endpoint rejects mid-file ranges, fall back to whole-file PUTs with our own retry/backoff and progress — which still fixes the silent-failure and invisibility problems, just not the retry-unit size. **Do not build Task 3 or 4 before Task 2 Step 5 has passed.**
