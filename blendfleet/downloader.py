"""Reliable, visible download of Kaggle kernel-output files.

WHY this module exists: kaggle_api_extended.KaggleApi.kernels_output() (the
CLI wrapper's own download path) does `requests.get(item.url, stream=True)`
and then `download_response.content` -- the whole body is pulled into
memory in one shot, with nowhere for a progress callback to hook in. Task 6
wants real per-instance download progress ("i want to see the download
process"), so blendfleet.kaggle_client.KaggleClient.fetch_output_with_progress
bypasses that call and streams each file itself, through this module.

The design deliberately mirrors blendfleet/uploader.py:
- One GET per file, read start to finish -- no Range-chunked sub-requests.
  Task 0 measured chunking SLOWER than one whole-file PUT on the upload
  side (0.59 vs 0.95 MB/s); there is no reason to expect the download side
  would behave differently, so this does not invent chunking either.
- Progress comes from bytes actually read off the response body (an
  instrumented iteration over `response.iter_content(...)`), not from
  splitting the request.
- `on_progress` is driven through a high-water-mark reporter, exactly
  `uploader._ProgressReporter`'s own discipline: "progress must never
  regress" is a general rule this app holds to, not something unique to
  resumable uploads -- so the clamp is applied here too even though a
  plain, non-resumable download has no path to a real regression today.
- `downloaded`/`total` accumulate ACROSS every file in one `fetch_files()`
  call, not per file: after Task 5 archives the render output into one
  zip, a caller normally passes exactly one (url, dest) pair, but the
  fallback (no archive -> many loose frames) still gets one continuous,
  monotonically climbing progress figure instead of a bar that resets to
  zero every file.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

# Read granularity for iter_content(). Independent of progress_interval
# (which governs how often on_progress fires, not how much is read per
# iteration) -- this only bounds how much of one file sits in memory
# between writes.
_READ_CHUNK = 256 * 1024


@dataclass
class DownloadProgress:
    """One tick of download progress, reported via `on_progress`.

    `downloaded` and `total` are absolute byte counts across every file in
    the current `fetch_files()` call (see the module docstring), and
    `downloaded` is a high-water mark: it never decreases, mirroring
    `blendfleet.uploader.UploadProgress`'s own contract.
    """

    downloaded: int
    total: int
    rate_bps: float


class _Response(Protocol):
    headers: dict

    def iter_content(self, chunk_size: int) -> Iterable[bytes]: ...


class Transport(Protocol):
    """What fetch_files needs from an HTTP client: one `get(url)` call
    returning something with `.headers` and a streaming `.iter_content`,
    exactly `requests.Response`'s own shape when called with stream=True --
    deliberately minimal so tests can fake it with zero network."""

    def get(self, url: str) -> _Response: ...


class _ProgressReporter:
    """High-water-mark progress -- the download-side twin of
    `uploader._ProgressReporter`. Shared across every file in one
    `fetch_files()` call so `downloaded` only ever climbs.
    """

    def __init__(self, on_progress: Callable[[DownloadProgress], None] | None):
        self._on_progress = on_progress
        self.high_water = 0

    def report(self, downloaded: int, total: int, rate_bps: float) -> None:
        if self._on_progress is None:
            return
        reported = max(downloaded, self.high_water)
        self.high_water = reported
        self._on_progress(DownloadProgress(reported, total, rate_bps))


def _content_length(response: _Response) -> int:
    """The response's declared size, or 0 when absent/unparseable -- never
    raises. A caller must never divide by this without the same
    zero-is-safe treatment blendfleet.ui.formatting's helpers already give
    a zero/unknown total."""
    try:
        return int((getattr(response, "headers", None) or {}).get(
            "Content-Length", 0))
    except (TypeError, ValueError):
        return 0


def fetch_files(files: list[tuple[str, Path]], transport: Transport,
                on_progress: Callable[[DownloadProgress], None] | None = None,
                progress_interval: int = 1 << 20) -> None:
    """Download every (url, dest_path) pair in `files`, one GET each, in
    order. `dest_path`'s parent directories are created as needed.

    `on_progress`, if given, is called roughly every `progress_interval`
    bytes actually written to disk, with a running total across every file
    in `files` -- see the module docstring for why that is cumulative
    rather than per file.
    """
    responses = [(transport.get(url), path) for url, path in files]
    total = sum(_content_length(response) for response, _ in responses)
    reporter = _ProgressReporter(on_progress)
    downloaded = 0
    since_tick = 0
    start = time.monotonic()

    for response, dest in responses:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            for chunk in response.iter_content(_READ_CHUNK):
                if not chunk:
                    continue
                f.write(chunk)
                n = len(chunk)
                downloaded += n
                since_tick += n
                if since_tick >= progress_interval or downloaded >= total:
                    elapsed = max(time.monotonic() - start, 1e-9)
                    reporter.report(downloaded, total, downloaded / elapsed)
                    since_tick = 0
