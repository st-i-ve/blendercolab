# Upload concurrency findings

**Date:** 2026-08-02/03
**Question:** Can a single Kaggle resumable upload session accept out-of-order or concurrent range PUTs, so a 100 MB file could be sent as 10 MB × 5 lanes in flight at once?

**Answer: No.** The endpoint is a Google Cloud Storage (GCS) JSON API resumable upload session, and it enforces the standard GCS contract: a single committed offset, strictly sequential, one request in flight at a time. This was verified empirically, not inferred from documentation alone.

## How the session was obtained

`kagglesdk.KaggleClient(api_token=...).blobs.blob_api_client.start_blob_upload(ApiStartBlobUploadRequest(type=ApiBlobType.DATASET, name=..., content_length=..., last_modified_epoch_seconds=...))` returns an `ApiStartBlobUploadResponse` with a `create_url` such as:

```
https://www.googleapis.com/upload/storage/v1/b/kaggle-data-sets/o?uploadType=resumable&upload_id=...
```

This confirms the upload target is literally the GCS resumable-upload endpoint (bucket `kaggle-data-sets`), not a Kaggle-custom protocol. No dataset was ever created via `dataset_create_new` — only blob-upload sessions were opened directly, so this never touched `remember-blend` or created any visible dataset (`bf-lane-test` was never needed).

Test payload: 30 MB (31,457,280 bytes) of local random bytes, `os.urandom`, never a real project file.

## 1. Sequential baseline (8 MB chunks, in order, fresh session)

Four `PUT`s with `Content-Range: bytes {start}-{end}/31457280`, in order 0→8M→16M→24M→30M:

| chunk | status | `Range` response header |
|---|---|---|
| 0–8388607 | 308 | `bytes=0-8388607` |
| 8388608–16777215 | 308 | `bytes=0-16777215` |
| 16777216–25165823 | 308 | `bytes=0-25165823` |
| 25165824–31457279 (final) | **200** | body = full GCS object JSON |

Final response body confirmed `"size": "31457280"` (exact match to source file) and `timeFinalized` present — i.e., this was verified as a genuine finalized, correctly-sized object, not just a 200 status taken on faith.

Total time: 50.89 s → **0.59 MB/s**.

## 2. Out-of-order (fresh session)

`PUT` bytes 8,388,608–16,777,215 (the second chunk) **before** byte 0, on a session with nothing committed yet.

Result (reproduced twice, on two different fresh sessions): the server does **not** return a clean 4xx — it **aborts the TCP connection outright** (`ConnectionResetError [WinError 10054]` / `ConnectionAbortedError [WinError 10053]`, "connection forcibly closed"/"aborted by the software in your host machine"). No HTTP status, no body.

A subsequent status-probe (`Content-Range: bytes */31457280`) on the same session showed `Range: (none)` — i.e., **zero bytes were committed** by the rejected out-of-order attempt.

Sending the actually-correct next chunk (bytes 0–8,388,607) immediately after, on the *same* session, succeeded normally (308, `Range: bytes=0-8388607`), proving the session itself was healthy and the abort was specifically a reaction to the out-of-order offset, not a fluke/expired session.

**Out-of-order ranges: rejected — yes, confirmed, via hard connection abort rather than an HTTP error code.**

## 3. Concurrent (fresh session, two threads, non-adjacent ranges)

Two threads simultaneously `PUT` bytes 0–8,388,607 ("A", the valid next range) and bytes 16,777,216–25,165,823 ("B", non-adjacent), same session.

Run 1: **both** requests were aborted mid-flight (`ConnectionResetError`/`ConnectionAbortedError`). Status probe afterward: `Range: (none)` — nothing committed.

Run 2 (fresh session, repeated for reproducibility): A succeeded (308, `Range: bytes=0-8388607`, confirmed committed by a follow-up status probe), B was aborted (`ConnectionAbortedError`).

Across both runs, the pattern is consistent: the server tolerates only **one in-flight request per session**. A second concurrent request against the same `upload_id` causes at least the non-sequential one (and sometimes both) to have its connection forcibly killed, never committed. There is no observed case where two ranges landed and were both accepted concurrently.

**Concurrent range PUTs to the same session: rejected — yes, confirmed.**

## 4. Throughput: sequential chunks vs one big PUT

Same fresh 30 MB payload, same network conditions, run back-to-back:

| method | time | throughput |
|---|---|---|
| Sequential 8 MB chunks (4 requests) | 50.89 s | **0.59 MB/s** |
| Single whole-file PUT (1 request) | 31.73 s | **0.95 MB/s** |

Both were verified to produce a correctly finalized object of exactly 31,457,280 bytes (same `md5Hash: OS3f3kck8yU4pIYr2LVLRw==` in both GCS response bodies — i.e., byte-identical content, not just matching size).

In this measurement, chunking was **~1.6x slower** than one big PUT — chunking adds per-request round-trip/TLS overhead without buying any concurrency, since concurrency is rejected by the server. This is a single run on one network path, not a statistically rigorous benchmark, but the direction is unambiguous: chunking here has a real cost and no counterbalancing parallelism benefit.

## Answers

- **Does the endpoint accept out-of-order ranges?** No. Confirmed: the connection is forcibly aborted and zero bytes are committed.
- **Does it accept concurrent range PUTs?** No. Confirmed: only one in-flight request per session survives; competing requests get their connections killed and are never committed.
- **Sequential-chunk throughput vs one big PUT:** chunked was slower in this test (0.59 MB/s vs 0.95 MB/s) — chunking added latency overhead with no offsetting parallelism.

## Recommendation

**Do not build true parallel lanes within a single upload session — the server rejects it.** The "several lanes" experience the user wants has to come from **concurrency across accounts** (or, if truly necessary, sequential bounded chunks per account for resumability/retry granularity, run one-at-a-time per session), not from splitting one file's bytes across simultaneous in-flight PUTs to the same session. Task 1 should implement sequential bounded chunks per file (useful for resuming a broken upload without restarting from byte 0) and get its "multiple lanes" throughput from running several *independent* per-account sessions concurrently — and the UI should say so plainly rather than implying single-file parallelism that does not exist.

## Account hygiene

No dataset was created (throwaway or otherwise) — only direct blob-upload sessions via `start_blob_upload`, which is how `_upload_blob` in `kaggle_api_extended.py` itself works before any dataset is created. `remember-blend` was never touched. `bf-lane-test` was not created because it wasn't needed for these tests.

**Left behind:** several completed/partial blob objects under the Kaggle-internal `inbox/24357697/...` path in the `kaggle-data-sets` GCS bucket (two fully-finalized 30 MB test objects from the sequential and single-PUT runs, plus a few partially-committed sessions from the out-of-order/concurrency tests). These are not attached to any dataset, are not visible or listed anywhere in the account/dataset UI, and there is no public API to delete an orphaned blob upload directly — Kaggle's own `_upload_blob` code path has the same property (a blob isn't a dataset until `dataset_create_new`/`dataset_create_version` references its token). They are expected to be garbage-collected by Kaggle's own inbox-cleanup process since no dataset ever referenced their tokens. No real project files were involved; all payloads were local `os.urandom` data.
