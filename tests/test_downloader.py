"""Tests for blendfleet.downloader: reliable, visible download of Kaggle
kernel-output files, mirroring blendfleet.uploader's design (see task-6
brief: "download progress the same way the uploader does it -- an
instrumented reader, not a chunked request, since chunking measured
slower on the upload side and there is no reason to expect otherwise
here").

No network: FakeTransport below never opens a socket -- it fakes exactly
what `requests.get(url, stream=True)` would hand back: `.headers` and an
`.iter_content(chunk_size)` generator.
"""
from __future__ import annotations

from blendfleet.downloader import DownloadProgress, fetch_files

MB = 1 << 20


class FakeResponse:
    def __init__(self, body: bytes, headers=None, chunk_size=64 * 1024):
        self.body = body
        self.headers = headers if headers is not None else {
            "Content-Length": str(len(body))}
        self._chunk_size = chunk_size

    def iter_content(self, chunk_size=None):
        size = chunk_size or self._chunk_size
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]


class FakeTransport:
    def __init__(self, responses: dict[str, FakeResponse]):
        self._responses = responses
        self.urls_fetched: list[str] = []

    def get(self, url: str):
        self.urls_fetched.append(url)
        return self._responses[url]


def _bytes(n: int, fill: int = 7) -> bytes:
    return bytes([fill % 256]) * n


def test_downloads_a_single_file_and_writes_its_content(tmp_path):
    body = _bytes(3 * MB, fill=65)
    transport = FakeTransport({"https://x/a": FakeResponse(body)})
    dest = tmp_path / "out" / "a.zip"

    fetch_files([("https://x/a", dest)], transport)

    assert dest.read_bytes() == body


def test_progress_fires_repeatedly_and_ends_exactly_at_total(tmp_path):
    body = _bytes(5 * MB, fill=1)
    transport = FakeTransport({"https://x/a": FakeResponse(body)})
    events: list[DownloadProgress] = []

    fetch_files([("https://x/a", tmp_path / "a.bin")], transport,
                on_progress=events.append, progress_interval=MB)

    assert len(events) > 2, "progress must fire more than once for a multi-MB file"
    assert events[-1].downloaded == len(body)
    assert events[-1].total == len(body)
    values = [e.downloaded for e in events]
    assert all(a <= b for a, b in zip(values, values[1:])), "must never regress"


def test_progress_is_cumulative_and_monotonic_across_multiple_files(tmp_path):
    body_a = _bytes(2 * MB, fill=1)
    body_b = _bytes(2 * MB, fill=2)
    transport = FakeTransport({
        "https://x/a": FakeResponse(body_a),
        "https://x/b": FakeResponse(body_b),
    })
    events: list[DownloadProgress] = []

    fetch_files([("https://x/a", tmp_path / "a.bin"),
                ("https://x/b", tmp_path / "b.bin")],
               transport, on_progress=events.append, progress_interval=512 * 1024)

    total = len(body_a) + len(body_b)
    assert events[-1].downloaded == total
    assert events[-1].total == total
    values = [e.downloaded for e in events]
    assert all(a <= b for a, b in zip(values, values[1:])), \
        "downloaded must never go backwards across a file boundary"
    assert (tmp_path / "a.bin").read_bytes() == body_a
    assert (tmp_path / "b.bin").read_bytes() == body_b


def test_creates_missing_destination_directories(tmp_path):
    transport = FakeTransport({"https://x/a": FakeResponse(b"hello")})
    dest = tmp_path / "nested" / "dir" / "a.bin"

    fetch_files([("https://x/a", dest)], transport)

    assert dest.read_bytes() == b"hello"


def test_no_files_is_a_safe_no_op(tmp_path):
    events: list[DownloadProgress] = []
    fetch_files([], FakeTransport({}), on_progress=events.append)
    assert events == []


def test_missing_content_length_does_not_crash_and_reports_zero_total(tmp_path):
    """A server that omits Content-Length must not raise or divide by
    zero -- formatting.py's own helpers already treat total<=downloaded
    as "done", so total=0 here is safe, not a bug to work around."""
    transport = FakeTransport({
        "https://x/a": FakeResponse(b"abc", headers={})})
    events: list[DownloadProgress] = []

    fetch_files([("https://x/a", tmp_path / "a.bin")], transport,
                on_progress=events.append)

    assert (tmp_path / "a.bin").read_bytes() == b"abc"
    assert events[-1].downloaded == 3


# ---------------------------------------------------------------- review --
# Code review finding: fetch_files used to open EVERY file's GET up front
# (`[(transport.get(url), path) for url, path in files]`) before reading or
# writing any of them -- for the no-archive fallback (hundreds of loose
# frames) that opens hundreds of simultaneous connections before a single
# byte reaches disk, and contradicts this module's own stated one-request-
# at-a-time design (mirroring uploader.py). Fixed to open the NEXT file's
# connection only once the previous one's response has been fully read and
# closed.

class _OpenTracker:
    def __init__(self):
        self.open_count = 0
        self.max_open = 0


class _TrackedResponse(FakeResponse):
    def __init__(self, body: bytes, tracker: _OpenTracker):
        super().__init__(body)
        self._tracker = tracker

    def close(self) -> None:
        self._tracker.open_count -= 1


class TrackingTransport:
    def __init__(self, bodies: dict[str, bytes], tracker: _OpenTracker):
        self._bodies = bodies
        self._tracker = tracker

    def get(self, url: str):
        self._tracker.open_count += 1
        self._tracker.max_open = max(self._tracker.max_open, self._tracker.open_count)
        return _TrackedResponse(self._bodies[url], self._tracker)


def test_fetches_one_file_at_a_time_never_more_than_one_open_connection(tmp_path):
    tracker = _OpenTracker()
    transport = TrackingTransport(
        {"https://x/a": _bytes(1024), "https://x/b": _bytes(1024),
         "https://x/c": _bytes(1024)}, tracker)

    fetch_files([("https://x/a", tmp_path / "a.bin"),
                ("https://x/b", tmp_path / "b.bin"),
                ("https://x/c", tmp_path / "c.bin")], transport)

    assert tracker.max_open == 1, (
        "at most one connection should ever be open at once -- one GET at "
        "a time, exactly like uploader.py's one-PUT-at-a-time design")


def test_progress_never_regresses_even_with_a_defensive_high_water_clamp(tmp_path):
    """Same discipline as uploader._ProgressReporter: on_progress is fed
    through a high-water-mark clamp so nothing this module ever does
    (now, or if resume is added later) can make the reported number step
    backwards."""
    from blendfleet.downloader import _ProgressReporter

    seen = []
    reporter = _ProgressReporter(seen.append)
    reporter.report(100, 1000, 10.0)
    reporter.report(50, 1000, 10.0)   # a lower local reading
    reporter.report(200, 1000, 10.0)
    values = [e.downloaded for e in seen]
    assert values == [100, 100, 200]
