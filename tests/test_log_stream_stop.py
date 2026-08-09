"""stop_event must actually stop the stream, and the stream must time out.

`self._stop` used to be checked ONLY between received lines, so a thread
parked in the request, the TLS handshake or a socket read never saw it.
closeEvent could not wait such a thread out, and closing the window during
a live render left N threads mid-SSL while Qt tore the window down -- the
production twin of the abort that made this test suite non-deterministic.

kagglesdk also passes no timeout to requests anywhere (kaggle_http_client.py
sends with `self._session.send(http_request, **settings)`, and `settings`
comes from merge_environment_settings, which never carries one), so a thread
blocked in connect/TLS had nothing bounding it either.
"""
from __future__ import annotations

import threading
import time

from blendfleet.log_stream import (CONNECT_TIMEOUT_SECONDS,
                                   READ_TIMEOUT_SECONDS,
                                   _install_request_timeout, stream_progress)

TOKEN = "KGAT_" + "a" * 32
PROGRESS_LINE = ('data: {"stream_name":"stdout","data":'
                 '"PROGRESS frame=1 ok=True secs=1.0 done=1/2\\n"}')


class _BlockingStreamResponse:
    """Delivers one line, then blocks forever -- exactly like a socket read
    waiting on a server that has gone quiet. Only close() unblocks it."""

    def __init__(self) -> None:
        self.closed = threading.Event()
        self.close_calls = 0

    def iter_lines(self, decode_unicode=True):
        yield PROGRESS_LINE
        # Bounded generously so a broken implementation fails the test
        # rather than hanging the whole suite.
        self.closed.wait(20)
        return

    def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


class _BlockingSdkClient:
    """A KaggleClient whose log stream blocks. `last_response` is the
    response handed out, so a test can assert it was really closed."""

    last_response: "_BlockingStreamResponse | None" = None

    def __init__(self, api_token=None, **kw) -> None:
        response = _BlockingStreamResponse()
        _BlockingSdkClient.last_response = response

        class _ApiClient:
            @staticmethod
            def get_kernel_session_logs_stream(req):
                return response

        class _Kernels:
            kernels_api_client = _ApiClient()

        self.kernels = _Kernels()


def _run_in_thread(fn):
    error: dict = {}

    def target():
        try:
            fn()
        except BaseException as e:      # noqa: BLE001 -- reported to the test
            error["exc"] = e

    thread = threading.Thread(target=target, name="test-stream")
    thread.start()
    return thread, error


def test_a_stream_blocked_in_a_read_is_stopped_by_the_stop_event(monkeypatch):
    """The core fix: a thread inside a blocking read must still unwind when
    stop_event is set, because the response is closed underneath it."""
    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", _BlockingSdkClient)

    progressed = threading.Event()
    stop = threading.Event()

    thread, error = _run_in_thread(lambda: stream_progress(
        TOKEN, "user0", "user0/kernel",
        on_progress=lambda done, total: progressed.set(),
        stop_event=stop))

    assert progressed.wait(5), "the stream never delivered its first line"
    # It is now parked in the blocking read, where a stop_event checked
    # between lines can never reach it.
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive(), \
        "stop_event did not stop a stream blocked inside a read"
    assert "exc" not in error, error.get("exc")
    assert _BlockingSdkClient.last_response.close_calls >= 1, \
        "the response must actually be closed, not merely flagged"


def test_a_stopped_stream_leaves_no_helper_thread_behind(monkeypatch):
    """The closer thread must not outlive the stream it closes -- a
    'finished' stream that quietly leaves a helper running is the same leak
    in a smaller costume."""
    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", _BlockingSdkClient)

    before = {t.ident for t in threading.enumerate()}
    progressed = threading.Event()
    stop = threading.Event()

    thread, _ = _run_in_thread(lambda: stream_progress(
        TOKEN, "user0", "user0/kernel",
        on_progress=lambda done, total: progressed.set(),
        stop_event=stop))
    assert progressed.wait(5)
    stop.set()
    thread.join(timeout=10)

    extra: list = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        extra = [t for t in threading.enumerate()
                 if t.ident not in before and t.is_alive()]
        if not extra:
            break
        time.sleep(0.05)
    assert not extra, f"threads left running: {[t.name for t in extra]}"


def test_a_stream_already_stopped_never_opens_a_connection(monkeypatch):
    """Closing the window between a thread being started and it being
    scheduled must not still cost a connection to kaggle.com."""
    import kagglesdk

    constructed: list = []

    class _Recorder(_BlockingSdkClient):
        def __init__(self, api_token=None, **kw):
            constructed.append(api_token)
            super().__init__(api_token=api_token, **kw)

    monkeypatch.setattr(kagglesdk, "KaggleClient", _Recorder)

    stop = threading.Event()
    stop.set()
    stream_progress(TOKEN, "user0", "user0/kernel",
                    on_progress=lambda done, total: None, stop_event=stop)
    assert constructed == [], "a stopped stream must not build a client at all"


class _FiniteResponse:
    def __init__(self, lines) -> None:
        self._lines = list(lines)
        self.close_calls = 0

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    def close(self) -> None:
        self.close_calls += 1


def test_the_response_is_closed_even_on_a_normal_end_of_log(monkeypatch):
    """Not only on stop: a stream that ends cleanly must still release its
    connection rather than leaving it to the garbage collector."""
    import kagglesdk

    holder: dict = {}

    class _Client:
        def __init__(self, api_token=None, **kw):
            response = _FiniteResponse([PROGRESS_LINE, "data: END_OF_LOG"])
            holder["response"] = response

            class _ApiClient:
                @staticmethod
                def get_kernel_session_logs_stream(req):
                    return response

            class _Kernels:
                kernels_api_client = _ApiClient()

            self.kernels = _Kernels()

    monkeypatch.setattr(kagglesdk, "KaggleClient", _Client)
    seen: list = []
    stream_progress(TOKEN, "user0", "user0/kernel",
                    on_progress=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 2)]
    assert holder["response"].close_calls >= 1


# ------------------------------------------------------------- timeouts --

class _FakeSession:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, request, **kwargs):
        self.sent.append(kwargs)
        return "response"


class _FakeHttpClient:
    def __init__(self) -> None:
        self._session = None

    def _init_session(self):
        if self._session is None:
            self._session = _FakeSession()
        return self._session


class _FakeClientWithHttp:
    def __init__(self) -> None:
        self._http = _FakeHttpClient()

    def http_client(self):
        return self._http


def test_a_timeout_is_installed_on_the_session_kagglesdk_never_sets_one():
    client = _FakeClientWithHttp()
    assert _install_request_timeout(client, (5.0, 30.0)) is True

    session = client.http_client()._session
    session.send("request")
    assert session.sent == [{"timeout": (5.0, 30.0)}]


def test_an_explicit_timeout_is_not_overridden():
    client = _FakeClientWithHttp()
    _install_request_timeout(client, (5.0, 30.0))
    session = client.http_client()._session
    session.send("request", timeout=1.0)
    assert session.sent == [{"timeout": 1.0}]


def test_installing_a_timeout_degrades_instead_of_raising():
    """If a future kagglesdk reshuffles its internals, live progress may be
    lost -- the dashboard must not be."""
    class Opaque:
        pass

    assert _install_request_timeout(Opaque(), (5.0, 30.0)) is False


def test_the_read_timeout_allows_for_a_long_gap_between_log_lines():
    """The kernel prints TELEMETRY every 5s and the request may be held for
    wait_for_logs_url_seconds=30 before the first byte, so a read timeout
    below that would kill perfectly healthy streams."""
    assert READ_TIMEOUT_SECONDS > 30
    assert CONNECT_TIMEOUT_SECONDS > 0
