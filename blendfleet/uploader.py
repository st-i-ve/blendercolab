"""Reliable, visible, resumable upload of a single blob to a Kaggle/GCS
resumable-upload session.

WHY this module exists: the installed `kaggle` package's own upload path
(`kaggle_api_extended.py`, `_upload_blob`/`upload_complete`) PUTs a whole
file in one request behind a `Retry(total=10)` that cannot replay an
already-consumed file stream, then gives up and **returns None** -- the
file is silently dropped. The subsequent dataset-version call then goes
out with an empty file list and Kaggle answers 400 "Please upload at
least one file", three layers away from the actual cause. See
blendfleet.kaggle_client._raise_dataset_upload_error for where that 400
gets translated back into something actionable, and
docs/upload-concurrency-findings.md for why this module does NOT chunk.

Task 0's live experiment against the real endpoint (a GCS JSON API
resumable-upload session) settled the design:
- Out-of-order Content-Range PUTs are rejected: the server aborts the
  TCP connection outright, zero bytes committed.
- Concurrent range PUTs are rejected: one in-flight request per session;
  competing requests get their connections killed.
- Chunking measured SLOWER than one whole-file PUT (0.59 MB/s vs
  0.95 MB/s for an 8 MB-chunked vs single-PUT 30 MB transfer) -- chunking
  buys smaller retry granularity at a real throughput cost, and buys no
  parallelism since the server refuses concurrent ranges anyway.

So: one PUT for the whole remaining range, every time. What was actually
broken -- invisible progress, a retry that can't resume, and a silent
None on failure -- is what this module fixes:
- Progress comes from an instrumented file reader that fires on_progress
  every `progress_interval` bytes *read*, not from splitting the request.
- On failure, `committed_offset` asks the server what it already has
  (GCS commits progressively as bytes arrive) and the retry resumes from
  there with a `Content-Range` header covering only the remainder.
- Retries use exponential backoff with jitter.
- Giving up raises `UploadError` carrying the last status and body --
  never returns None.

No `lanes`/concurrency parameter: Task 0 proved concurrency within one
upload session is rejected by the server, so a parameter implying
parallelism here would misdescribe the API. Concurrency (several lanes)
belongs across accounts, each with its own independent session.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class UploadError(Exception):
    """Raised when an upload cannot be completed after retrying.

    Carries the last HTTP status and response body the server gave, so a
    caller several layers up (see kaggle_client._raise_dataset_upload_error
    for the shape of that problem) never has to guess why a file didn't
    make it -- the transport never gets a chance to swallow the failure
    into a bare `None`.
    """

    def __init__(self, message: str, status: int | None, body: str):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class UploadProgress:
    """One tick of upload progress, reported via `on_progress`.

    `uploaded` and `total` are absolute byte offsets into the whole file
    (not relative to the current attempt), so `uploaded` always ends
    exactly at `total` on success regardless of how many retries or
    resumes happened along the way. `resumed_from` is 0 on a fresh
    (non-resumed) attempt and the server's committed offset once a retry
    has resumed past a failure, so a UI can say "resumed from 12 MB".
    """

    uploaded: int
    total: int
    rate_bps: float
    retries: int
    resumed_from: int


class _Response(Protocol):
    status_code: int
    headers: dict


class Transport(Protocol):
    """What upload_file/committed_offset need from an HTTP client.

    Deliberately minimal so tests can fake it with zero network: one
    `put(url, data, headers)` method returning something with
    `.status_code`/`.headers`/`.text`, plus a `.token` attribute -- the
    blob token the caller ultimately wants back. (The real Kaggle blob
    token comes from the session-start response, not the PUT response
    body, so the transport -- which owns the session -- is what carries
    it here rather than this module trying to parse it out of a GCS
    finalize response.)
    """

    token: str

    def put(self, url: str, data, headers: dict) -> _Response: ...


def _body_of(response) -> str:
    text = getattr(response, "text", None)
    if text:
        return text
    return getattr(response, "body", "") or ""


def _status_of(response) -> int | None:
    return getattr(response, "status_code", None)


class _InstrumentedReader:
    """Wraps an open file positioned at `start`, calling `on_progress`
    every `progress_interval` bytes actually read (not scheduled -- read),
    so progress reflects bytes genuinely handed to the transport. This is
    how progress is reported without splitting the PUT into chunks.
    """

    def __init__(self, fp, start: int, total: int, progress_interval: int,
                 on_progress: Callable[[UploadProgress], None] | None,
                 resumed_from: int, retries: int):
        self._fp = fp
        self._total = total
        self._progress_interval = max(1, progress_interval)
        self._on_progress = on_progress
        self._resumed_from = resumed_from
        self._retries = retries
        self._uploaded = start
        self._since_tick = 0
        self._attempt_start = time.monotonic()

    def read(self, size: int = -1) -> bytes:
        # File-like contract: size None/negative means "read everything";
        # size == 0 means "read nothing" (must NOT fall through to
        # read-everything, which `if size` alone would do since 0 is falsy).
        chunk = self._fp.read() if size is None or size < 0 else self._fp.read(size)
        if chunk:
            n = len(chunk)
            self._uploaded += n
            self._since_tick += n
            if self._on_progress is not None and (
                self._since_tick >= self._progress_interval
                or self._uploaded >= self._total
            ):
                self._emit()
        return chunk

    def _emit(self) -> None:
        elapsed = max(time.monotonic() - self._attempt_start, 1e-9)
        sent_this_attempt = self._uploaded - self._resumed_from
        rate_bps = sent_this_attempt / elapsed
        self._on_progress(UploadProgress(
            uploaded=self._uploaded, total=self._total, rate_bps=rate_bps,
            retries=self._retries, resumed_from=self._resumed_from))
        self._since_tick = 0


def committed_offset(session_url: str, total: int, transport: Transport) -> int:
    """Ask the server how many bytes of `total` it already has.

    Uses the standard GCS resumable-upload status-probe idiom: a
    zero-length PUT with `Content-Range: bytes */{total}`. A 200/201
    means the object is already fully finalized (return `total`); a 308
    carries a `Range: bytes=0-N` header naming the last committed byte
    (inclusive), so the committed offset is N + 1; no Range header at all
    means nothing has been received yet.
    """
    if total == 0:
        return 0  # nothing to probe; an empty file has no bytes to commit
    headers = {"Content-Range": f"bytes */{total}", "Content-Length": "0"}
    response = transport.put(session_url, data=b"", headers=headers)
    status = _status_of(response)
    if status in (200, 201):
        return total
    if status == 308:
        range_header = (response.headers or {}).get("Range")
        if not range_header:
            return 0
        try:
            upper = int(range_header.rsplit("-", 1)[-1])
        except ValueError:
            return 0
        return upper + 1
    raise UploadError(
        f"could not determine committed offset: server returned "
        f"status={status!r} body={_body_of(response)!r}",
        status=status, body=_body_of(response))


def _backoff_delay(attempt: int, rand_fn: Callable[[], float]) -> float:
    """Exponential backoff with jitter. `attempt` is 1 for the first
    retry, 2 for the second, etc. Capped so a flaky connection doesn't
    end up waiting minutes between attempts."""
    base = 0.5
    cap = 20.0
    delay = min(cap, base * (2 ** (attempt - 1)))
    return delay + delay * rand_fn()


def upload_file(path, session_url: str, transport: Transport,
                 on_progress: Callable[[UploadProgress], None] | None = None,
                 max_retries: int = 6, progress_interval: int = 1 << 20,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 rand_fn: Callable[[], float] = random.random) -> str:
    """Upload `path` to `session_url` and return the blob token.

    One PUT for the whole remaining range every attempt -- Task 0 showed
    chunking is both rejected when out of order/concurrent and slower
    even when done correctly in order, so there is nothing to gain by
    splitting the request. `on_progress` (if given) is called from the
    instrumented reader as bytes are actually read, roughly every
    `progress_interval` bytes.

    On failure, the server's committed offset is queried and the next
    attempt resumes from there with a `Content-Range` header covering
    only the remainder -- never the whole file again. Retries use
    exponential backoff with jitter. After `max_retries` failed retries,
    raises `UploadError` naming the last status and body; this function
    never returns None.
    """
    path = Path(path)
    total = path.stat().st_size
    start = 0
    resumed_from = 0
    retries = 0
    last_status: int | None = None
    last_body = ""

    while True:
        try:
            with path.open("rb") as fp:
                fp.seek(start)
                headers = {}
                if start > 0:
                    headers["Content-Range"] = f"bytes {start}-{total - 1}/{total}"
                    headers["Content-Length"] = str(total - start)
                else:
                    headers["Content-Length"] = str(total)
                reader = _InstrumentedReader(
                    fp, start=start, total=total,
                    progress_interval=progress_interval, on_progress=on_progress,
                    resumed_from=resumed_from, retries=retries)
                response = transport.put(session_url, data=reader, headers=headers)
            status = _status_of(response)
            if status in (200, 201):
                return transport.token
            last_status, last_body = status, _body_of(response)
        except UploadError:
            raise
        except Exception as exc:  # connection dropped mid-stream, etc.
            last_status, last_body = None, str(exc)

        retries += 1
        if retries > max_retries:
            raise UploadError(
                f"upload failed after {max_retries} retries "
                f"(last status={last_status!r}, body={last_body!r})",
                status=last_status, body=last_body)

        offset = committed_offset(session_url, total, transport)
        if offset > start:
            resumed_from = offset
        start = offset
        if start >= total:
            return transport.token  # already fully committed server-side

        sleep_fn(_backoff_delay(retries, rand_fn))
