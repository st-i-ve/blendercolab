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

import warnings
import zipfile
from pathlib import Path

import pytest

from blendfleet import kaggle_client, log_stream
from blendfleet.kaggle_http import install_request_timeout
from blendfleet.collector import collect
from blendfleet.downloader import IncompleteDownload, fetch_files
from blendfleet.fleet import FleetState


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
        # collect() scopes its staging folders by job id so two jobs
        # collected into one chosen folder cannot share one.
        self.job_id = "j-transient"

    @property
    def scene_key(self):
        # Delegates to the real FleetState.scene_key (rather than a
        # second, hand-copied slugify) so this fake can never drift from
        # what collect() actually derives its per-scene subfolder from.
        return FleetState.scene_key.fget(self)


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
    state = State([worker])
    report = collect(state, [Account("stive")], lambda tok: client, dest,
                     sleep=lambda s: None)
    # collect() now leaves one zip in `dest` -- the truncated 5-byte
    # leftover must not be inside it either.
    assert report.archive_path == dest / f"{state.scene_key}.zip"
    with zipfile.ZipFile(report.archive_path) as zf:
        for name in zf.namelist():
            assert zf.read(name) == b"PNG-real", f"{name} is a partial file"


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


# --------------------------------------------------------------------------
# A worker whose account can no longer be matched
#
# collect() used to skip it in silence: per_worker=0, nothing in
# worker_errors, its frames in missing_frames. On 2026-08-12 that made a
# 25-frame render across five accounts look like "two accounts did not
# render any" -- both had in fact finished every frame, and both sets were
# sitting on Kaggle the whole time.
# --------------------------------------------------------------------------

class NamedWorker(Worker):
    def __init__(self, label, username, frames):
        super().__init__(label, frames)
        self.username = username


class NamedAccount(Account):
    def __init__(self, label, username=None):
        super().__init__(label)
        self.username = username


class GoodClient:
    def __init__(self, frames):
        self.frames = frames

    def fetch_output(self, slug, dest):
        out = Path(dest)
        out.mkdir(parents=True, exist_ok=True)
        written = []
        for f in self.frames:
            p = out / f"f_{f:04d}.png"
            p.write_bytes(b"PNG-real")
            written.append(p)
        return written


def test_an_unmatched_worker_is_explained_never_silently_skipped(tmp_path):
    worker = NamedWorker("worpstudios", "worpstudios", [4, 9, 14])
    report = collect(State([worker]), [NamedAccount("someone-else", "other")],
                     lambda tok: GoodClient([4, 9, 14]), tmp_path / "frames",
                     sleep=lambda s: None)
    assert "worpstudios" in report.worker_errors, \
        "an unmatched worker must be reported, not skipped in silence"
    message = report.worker_errors["worpstudios"]
    # What happened, why, and what to do -- this app's rule for every
    # user-facing string.
    assert "3 frame(s)" in message
    assert "still on Kaggle" in message
    assert "Re-add that account" in message
    assert "worpstudios" in message, "must name the Kaggle username to re-add"


def test_renaming_an_account_mid_job_still_collects_its_frames(tmp_path):
    # The label is the user's editable nickname; the username is the
    # account's real identity. Renaming "worp" to "studio-2" while a job
    # is running must not orphan that job's frames.
    worker = NamedWorker("worp", "worpstudios", [1, 2, 3])
    account = NamedAccount("studio-2", "worpstudios")
    report = collect(State([worker]), [account],
                     lambda tok: GoodClient([1, 2, 3]), tmp_path / "frames",
                     sleep=lambda s: None)
    assert report.copied == 3
    assert report.worker_errors == {}
    assert report.missing_frames == []


def test_one_unmatched_worker_does_not_stop_the_others(tmp_path):
    good = NamedWorker("stive", "stivestivewithani", [1, 2])
    orphan = NamedWorker("gone", "worpstudios", [3])
    state = State([good, orphan])
    state.end_frame = 3
    report = collect(state, [NamedAccount("stive", "stivestivewithani")],
                     lambda tok: GoodClient([1, 2]), tmp_path / "frames",
                     sleep=lambda s: None)
    assert report.copied == 2, "the matched worker's frames still arrive"
    assert "gone" in report.worker_errors
    assert report.missing_frames == [3]


# --------------------------------------------------------------------------
# kaggle_client: a call that can block forever is a thread that cannot stop
# --------------------------------------------------------------------------
#
# Backend.stop() gives an in-flight worker 5 seconds and then cuts it
# loose, which is what stopped the packaged app aborting with "QThread:
# Destroyed while thread is still running". But a worker could not be
# stopped in the first place because kagglesdk sets no timeout anywhere, so
# a poll parked in connect/TLS/read waited forever. These tests pin the
# backstop that makes cutting a thread loose the rare case again.

class FakeSession:
    """Enough of requests.Session for the helper: a .send to wrap."""

    def __init__(self):
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(kwargs)
        return "response"


class FakeHttpClient:
    def __init__(self):
        self._session = None

    def _init_session(self):
        if self._session is None:
            self._session = FakeSession()


class FakeSdkClient:
    """Shaped like kagglesdk.KaggleClient as far as the helper reaches."""

    def __init__(self):
        self._http = FakeHttpClient()

    def http_client(self):
        return self._http


def sent_timeout(client) -> object:
    """The timeout the wrapped session would actually put on the wire."""
    session = client.http_client()._session
    session.send(object())
    return session.sent[-1].get("timeout")


def test_the_sdk_client_type_gets_a_timeout(monkeypatch):
    """_default_sdk_factory's kagglesdk.KaggleClient is used by quota(),
    cancel(), the blob-upload session and dataset listing."""
    built = FakeSdkClient()
    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", lambda api_token: built)
    client = kaggle_client._default_sdk_factory("KGAT_" + "a" * 32)
    assert sent_timeout(client) == kaggle_client.REQUEST_TIMEOUT


def test_the_kaggle_api_type_gets_a_timeout_on_every_call_it_builds():
    """KaggleApi holds no session of its own -- it builds a FRESH kagglesdk
    client per network call via build_kaggle_client(). Patching the object
    once would cover nothing, so the factory itself is the seam, and every
    client it hands back afterwards must arrive already bounded."""
    class FakeApi:
        def __init__(self):
            self.built = []

        def build_kaggle_client(self):
            c = FakeSdkClient()
            self.built.append(c)
            return c

    api = FakeApi()
    assert kaggle_client._install_api_timeout(
        api, kaggle_client.REQUEST_TIMEOUT) is True

    first, second = api.build_kaggle_client(), api.build_kaggle_client()
    assert sent_timeout(first) == kaggle_client.REQUEST_TIMEOUT
    assert sent_timeout(second) == kaggle_client.REQUEST_TIMEOUT, \
        "a per-call factory must bound EVERY client, not just the first"


def test_a_client_that_already_has_a_timeout_keeps_it():
    """setdefault, not overwrite: a caller passing its own timeout for one
    specific call must win over the blanket default."""
    client = FakeSdkClient()
    install_request_timeout(client, kaggle_client.REQUEST_TIMEOUT)
    session = client.http_client()._session
    session.send(object(), timeout=(1.0, 2.0))
    assert session.sent[-1]["timeout"] == (1.0, 2.0)


@pytest.mark.parametrize("fake", [
    object(),                                   # nothing at all
    type("NoHttp", (), {})(),                   # no http_client
    type("BadHttp", (), {"http_client": lambda self: None})(),
])
def test_a_client_without_the_internals_degrades_instead_of_raising(fake):
    """Best-effort by design. Tests inject fake factories everywhere, and a
    future kagglesdk that reshuffles its privates must cost the backstop,
    never the whole app."""
    assert install_request_timeout(fake, (5.0, 30.0)) is False


@pytest.mark.parametrize("fake", [
    object(),
    type("NotCallable", (), {"build_kaggle_client": None})(),
])
def test_an_api_without_the_factory_degrades_instead_of_raising(fake):
    assert kaggle_client._install_api_timeout(fake, (5.0, 30.0)) is False


def test_a_fake_api_survives_the_timeout_install_unchanged():
    """The whole existing suite injects api_factory doubles. Installing the
    timeout must be invisible to them -- no raise, no warning, no
    attribute appearing that a strict double would reject."""
    class StrictDouble:
        __slots__ = ()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert kaggle_client._install_api_timeout(
            StrictDouble(), kaggle_client.REQUEST_TIMEOUT) is False
        assert install_request_timeout(
            StrictDouble(), kaggle_client.REQUEST_TIMEOUT) is False


def test_bulk_transfers_get_a_longer_read_timeout_than_control_calls():
    """A read timeout is a gap BETWEEN bytes, so it does not cap a long
    transfer -- but it does bound the request-SEND phase, and it must also
    cover the far end's think time after the last byte of a 400 MB upload.
    Using the control-plane value there would abort uploads that work."""
    assert (kaggle_client.TRANSFER_READ_TIMEOUT_SECONDS
            > kaggle_client.READ_TIMEOUT_SECONDS)
    assert kaggle_client.CONNECT_TIMEOUT_SECONDS > 0
    # Comfortably above Kaggle's slowest control-plane RPC, and above the
    # 30s poll interval so a slow-but-alive poll is not called a failure.
    assert kaggle_client.READ_TIMEOUT_SECONDS > 30


def test_both_real_transports_pass_a_timeout(monkeypatch):
    """uploader.py/downloader.py take an injected Transport and do no HTTP
    of their own -- the only real `requests` calls are these two, so an
    unbounded PUT/GET here would leave a stuck transfer just as
    unstoppable as an unbounded poll."""
    calls = {}
    monkeypatch.setattr(kaggle_client.requests, "put",
                        lambda url, **kw: calls.setdefault("put", kw))
    monkeypatch.setattr(kaggle_client.requests, "get",
                        lambda url, **kw: calls.setdefault("get", kw))

    kaggle_client._RequestsPutTransport(token="t").put("u", b"", {})
    kaggle_client._RequestsGetTransport().get("u")

    assert calls["put"]["timeout"] == kaggle_client.TRANSFER_TIMEOUT
    assert calls["get"]["timeout"] == kaggle_client.TRANSFER_TIMEOUT
    assert calls["get"]["stream"] is True, "streaming must survive the change"


def test_there_is_exactly_one_implementation_of_the_helper():
    """log_stream and kaggle_client both need it. Two copies would be two
    things to fix the day kagglesdk renames an internal."""
    assert log_stream._install_request_timeout is install_request_timeout
