"""Surviving a connection that drops mid-operation.

Not hypothetical: on 2026-08-11/12 one machine's link broke three
separate live runs, each time AFTER the GPU work was finished and paid
for.

  * a 15-frame render reported ZERO frames collected, because the output
    download was truncated (IncompleteRead, 25 MB of 36 MB); calling
    collect again recovered all 15
  * the log stream died at frame 3 (ChunkedEncodingError) and never
    reconnected, so the app showed 3/15 for seven minutes while the
    kernel quietly finished every frame
  * a benchmark script died on a transient poll error while its kernel
    kept running

The rule these encode: work already rendered must not be lost to a
socket, and a dropped stream is not a failed render.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from blendfleet import log_stream
from blendfleet.collector import collect
from blendfleet.downloader import IncompleteDownload, fetch_files


# --------------------------------------------------------------------------
# downloader.fetch_files
# --------------------------------------------------------------------------

class FlakyResponse:
    """Yields `chunks`, then optionally raises -- a body that stops early."""

    def __init__(self, chunks, declared, raise_at_end=None):
        self.headers = {"Content-Length": str(declared)}
        self._chunks = chunks
        self._raise = raise_at_end
        self.closed = False

    def iter_content(self, chunk_size):
        for c in self._chunks:
            yield c
        if self._raise:
            raise self._raise

    def close(self):
        self.closed = True


class FlakyTransport:
    def __init__(self, responses):
        self._responses = list(responses)
        self.gets = 0

    def get(self, url):
        self.gets += 1
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_a_truncated_download_is_retried_and_recovered(tmp_path):
    dest = tmp_path / "out.zip"
    transport = FlakyTransport([
        FlakyResponse([b"x" * 10], declared=20),      # short: 10 of 20
        FlakyResponse([b"y" * 20], declared=20),      # complete
    ])
    fetch_files([("u", dest)], transport, sleep=lambda s: None)
    assert dest.read_bytes() == b"y" * 20
    assert transport.gets == 2


def test_a_short_read_is_never_left_on_disk_as_a_complete_file(tmp_path):
    # The failure that cost 15 frames: the truncated zip was written, and
    # then would not open. A partial file must be deleted, not kept.
    dest = tmp_path / "out.zip"
    transport = FlakyTransport([
        FlakyResponse([b"x" * 5], declared=99) for _ in range(4)])
    with pytest.raises(IncompleteDownload):
        fetch_files([("u", dest)], transport, attempts=4, sleep=lambda s: None)
    assert not dest.exists()


def test_a_dropped_connection_is_retried(tmp_path):
    dest = tmp_path / "out.zip"
    transport = FlakyTransport([
        FlakyResponse([b"a" * 4], 8, raise_at_end=OSError("connection reset")),
        FlakyResponse([b"b" * 8], 8),
    ])
    fetch_files([("u", dest)], transport, sleep=lambda s: None)
    assert dest.read_bytes() == b"b" * 8


def test_progress_never_goes_backwards_across_a_retry(tmp_path):
    seen = []
    transport = FlakyTransport([
        FlakyResponse([b"x" * 8], 16, raise_at_end=OSError("reset")),
        FlakyResponse([b"y" * 16], 16),
    ])
    fetch_files([("u", tmp_path / "f")], transport,
                on_progress=lambda p: seen.append(p.downloaded),
                progress_interval=1, sleep=lambda s: None)
    assert seen == sorted(seen), f"progress regressed: {seen}"


def test_retries_are_bounded_and_the_error_surfaces(tmp_path):
    transport = FlakyTransport([OSError("down")] * 3)
    with pytest.raises(OSError):
        fetch_files([("u", tmp_path / "f")], transport, attempts=3,
                    sleep=lambda s: None)
    assert transport.gets == 3


# --------------------------------------------------------------------------
# collector.collect
# --------------------------------------------------------------------------

class Worker:
    def __init__(self, label, frames):
        self.label = label
        self.username = label
        self.frames = frames
        self.kernel_slug = f"{label}/render-1"
        self.state = "complete"


class State:
    def __init__(self, workers):
        self.workers = workers
        self.start_frame = 1
        self.end_frame = len(workers[0].frames)
        self.blend_name = "remember.blend"   # collect names frames from it


class Account:
    def __init__(self, label):
        self.label = label
        self.token = "KGAT_" + label


class FlakyClient:
    """Fails `failures` times, then writes the frames it was asked for."""

    def __init__(self, failures, frames):
        self.failures = failures
        self.frames = frames
        self.calls = 0

    def fetch_output(self, slug, dest):
        self.calls += 1
        if self.calls <= self.failures:
            # A real truncated fetch leaves rubbish behind before failing.
            Path(dest).mkdir(parents=True, exist_ok=True)
            (Path(dest) / "f_0001.png").write_bytes(b"trunc")
            raise OSError("Connection broken: IncompleteRead")
        out = Path(dest)
        out.mkdir(parents=True, exist_ok=True)
        written = []
        for f in self.frames:
            p = out / f"f_{f:04d}.png"
            p.write_bytes(b"PNG-real")
            written.append(p)
        return written


def test_collect_retries_a_dropped_download_and_gets_every_frame(tmp_path):
    worker = Worker("stive", [1, 2, 3])
    client = FlakyClient(failures=1, frames=[1, 2, 3])
    report = collect(State([worker]), [Account("stive")],
                     lambda tok: client, tmp_path / "frames",
                     sleep=lambda s: None)
    assert report.copied == 3
    assert report.missing_frames == []
    assert report.worker_errors == {}
    assert client.calls == 2


def test_a_failed_attempts_leftovers_are_never_collected(tmp_path):
    # The first attempt leaves a truncated f_0001.png. If staging were not
    # cleared, that 5-byte file would be copied out and counted as frame 1
    # -- a "collected" frame that is actually corrupt.
    dest = tmp_path / "frames"
    worker = Worker("stive", [1, 2, 3])
    client = FlakyClient(failures=1, frames=[1, 2, 3])
    collect(State([worker]), [Account("stive")], lambda tok: client, dest,
            sleep=lambda s: None)
    for f in dest.iterdir():
        assert f.read_bytes() == b"PNG-real", f"{f.name} is a partial file"


def test_collect_still_reports_a_worker_that_never_recovers(tmp_path):
    worker = Worker("stive", [1, 2])
    client = FlakyClient(failures=99, frames=[1, 2])
    report = collect(State([worker]), [Account("stive")],
                     lambda tok: client, tmp_path / "frames",
                     sleep=lambda s: None)
    assert report.copied == 0
    assert "stive" in report.worker_errors
    assert report.missing_frames == [1, 2]


# --------------------------------------------------------------------------
# log_stream.stream_progress
# --------------------------------------------------------------------------

class FakeStream:
    def __init__(self, lines, raise_at_end=None):
        self._lines = lines
        self._raise = raise_at_end

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            yield line
        if self._raise:
            raise self._raise

    def close(self):
        pass


def install_fake_kaggle(monkeypatch, streams):
    """Make stream_progress open each of `streams` in turn."""
    opened = {"n": 0}

    class FakeApiClient:
        def get_kernel_session_logs_stream(self, req):
            i = opened["n"]
            opened["n"] += 1
            s = streams[min(i, len(streams) - 1)]
            if isinstance(s, Exception):
                raise s
            return s

    class FakeKernels:
        kernels_api_client = FakeApiClient()

    class FakeClient:
        def __init__(self, api_token=None):
            self.kernels = FakeKernels()

    import sys
    import types
    mod = types.ModuleType("kagglesdk")
    mod.KaggleClient = FakeClient
    svc = types.ModuleType("kagglesdk.kernels.types.kernels_api_service")

    class Req:
        pass
    svc.ApiGetKernelSessionLogsStreamRequest = Req
    monkeypatch.setitem(sys.modules, "kagglesdk", mod)
    monkeypatch.setitem(sys.modules, "kagglesdk.kernels", types.ModuleType("k"))
    monkeypatch.setitem(sys.modules, "kagglesdk.kernels.types",
                        types.ModuleType("kt"))
    monkeypatch.setitem(sys.modules,
                        "kagglesdk.kernels.types.kernels_api_service", svc)
    monkeypatch.setattr(log_stream, "_install_request_timeout",
                        lambda *a, **k: None)
    return opened


def progress_line(frame, done, total):
    """One real SSE frame, exactly as Kaggle sends it."""
    return ('data: {"stream_name":"stdout","data":"PROGRESS frame=%d ok=True '
            'secs=1.0 done=%d/%d"}' % (frame, done, total))


END_OF_LOG = 'data: {"stream_name":"stdout","data":"END_OF_LOG"}'


def test_a_dropped_stream_reconnects_and_keeps_reporting(monkeypatch):
    # The exact 2026-08-11 failure: the connection dies at frame 3 of 15.
    opened = install_fake_kaggle(monkeypatch, [
        FakeStream([progress_line(1, 1, 15), progress_line(2, 2, 15),
                    progress_line(3, 3, 15)],
                   raise_at_end=OSError("Response ended prematurely")),
        # Kaggle replays from the top on the new connection.
        FakeStream([progress_line(1, 1, 15), progress_line(2, 2, 15),
                    progress_line(3, 3, 15), progress_line(4, 4, 15),
                    progress_line(15, 15, 15), END_OF_LOG]),
    ])
    seen = []
    log_stream.stream_progress("KGAT_x", "me", "k", lambda d, t: seen.append(d),
                               sleep=lambda s: None)
    assert opened["n"] == 2, "must have reconnected"
    assert seen[-1] == 15, "must reach the end after reconnecting"


def test_replayed_lines_are_not_reported_twice(monkeypatch):
    install_fake_kaggle(monkeypatch, [
        FakeStream([progress_line(1, 1, 3), progress_line(2, 2, 3)],
                   raise_at_end=OSError("dropped")),
        FakeStream([progress_line(1, 1, 3), progress_line(2, 2, 3),
                    progress_line(3, 3, 3), END_OF_LOG]),
    ])
    seen = []
    log_stream.stream_progress("KGAT_x", "me", "k", lambda d, t: seen.append(d),
                               sleep=lambda s: None)
    assert seen == [1, 2, 3], f"replayed lines were re-reported: {seen}"


def test_reconnecting_stops_when_nothing_new_arrives(monkeypatch):
    # A log that ends without the end-of-log marker must not reconnect
    # forever -- each attempt that brings nothing new counts against the
    # budget.
    opened = install_fake_kaggle(monkeypatch, [
        FakeStream([progress_line(1, 1, 3)]) for _ in range(20)])
    log_stream.stream_progress("KGAT_x", "me", "k", lambda d, t: None,
                               max_reconnects=3, sleep=lambda s: None)
    assert opened["n"] <= 5, f"reconnected {opened['n']} times"


def test_a_stop_event_still_ends_the_stream_promptly(monkeypatch):
    import threading
    install_fake_kaggle(monkeypatch, [
        FakeStream([progress_line(1, 1, 3)], raise_at_end=OSError("drop"))])
    stop = threading.Event()
    stop.set()
    log_stream.stream_progress("KGAT_x", "me", "k", lambda d, t: None,
                               stop_event=stop, sleep=lambda s: None)
