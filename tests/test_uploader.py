"""Tests for blendfleet.uploader: reliable, visible, resumable upload of a
single blob to a Kaggle/GCS resumable-upload session.

Task 0's live experiment settled the design (see docs/upload-concurrency-
findings.md): out-of-order and concurrent range PUTs are rejected outright
by the server, and chunking measured slower than one whole-file PUT. So
these tests deliberately assert ONE PUT per attempt (the whole remaining
range), not chunked sub-requests -- and a resumed attempt must send only
the bytes the server doesn't have yet, never the whole file again.

No network: FakeTransport below never opens a socket. It fakes the two
observed server behaviors (308 "Resume Incomplete" with a Range header
during a status probe, 200/201 on a completed PUT) and the two client-
visible failure modes from Task 0 (a raised exception mid-stream, or a
non-2xx/308 status).
"""
from __future__ import annotations

import pytest

from blendfleet.uploader import (
    UploadError,
    UploadProgress,
    committed_offset,
    upload_file,
)

MB = 1 << 20


class FakeResponse:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _is_probe(headers) -> bool:
    return headers.get("Content-Range", "").startswith("bytes */")


class FakeTransport:
    """Records every PUT. Distinguishes an upload PUT (has a real body, or
    is the fresh whole-file attempt) from a status-probe PUT (the GCS
    "bytes */total" idiom used by committed_offset) purely by header shape,
    exactly like the real server would see them.

    `put_plan` is a list of callables/results consumed in order, one per
    *upload* PUT (not probes): each is either an exception instance to
    raise, or a FakeResponse to return. Exhausting the list repeats the
    last entry.

    `probe_plan` is a list of ints (committed offsets) consumed in order
    for each *probe* PUT; exhausting it repeats the last entry.
    """

    def __init__(self, token="tok-abc123", put_plan=None, probe_plan=None,
                 read_chunk=256 * 1024):
        self.token = token
        self._put_plan = list(put_plan) if put_plan else [FakeResponse(200)]
        self._probe_plan = list(probe_plan) if probe_plan is not None else []
        self._read_chunk = read_chunk
        self.calls = []       # dicts: url, headers, body (upload PUTs only)
        self.probe_calls = []  # list of headers dicts

    def _next(self, plan):
        if len(plan) > 1:
            return plan.pop(0)
        return plan[0]

    def put(self, url, data, headers):
        if _is_probe(headers):
            self.probe_calls.append(dict(headers))
            total = int(headers["Content-Range"].split("/")[1])
            offset = self._next(self._probe_plan) if self._probe_plan else 0
            if offset >= total:
                return FakeResponse(200)
            range_header = {} if offset == 0 else {"Range": f"bytes=0-{offset - 1}"}
            return FakeResponse(308, headers=range_header)

        # Upload PUT: consume the reader like a real streaming HTTP client
        # would (multiple .read() calls), so on_progress must fire more
        # than once for a file bigger than one chunk.
        body = bytearray()
        result = self._next(self._put_plan)
        while True:
            chunk = data.read(self._read_chunk)
            if not chunk:
                break
            body.extend(chunk)
            if isinstance(result, _RaiseAfter) and len(body) >= result.after_bytes:
                self.calls.append({"url": url, "headers": dict(headers),
                                    "body": bytes(body)})
                raise result.exc
        self.calls.append({"url": url, "headers": dict(headers), "body": bytes(body)})
        if isinstance(result, Exception):
            raise result
        return result


class _RaiseAfter:
    """Plan entry: simulate a connection dying after `after_bytes` of this
    PUT's body have been streamed out (Task 0's observed failure mode --
    the server aborts mid-flight, some bytes committed, no clean status)."""

    def __init__(self, after_bytes, exc):
        self.after_bytes = after_bytes
        self.exc = exc


def _periodic_bytes(size: int) -> bytes:
    """Same content as bytes(i % 251 for i in range(size)), computed by
    tiling a 251-byte period instead of a size-iteration Python loop --
    identical bytes, just fast enough for a 20 MB test file."""
    period = bytes(range(251))
    reps = size // 251 + 1
    return (period * reps)[:size]


def make_file(tmp_path, size, fill=None):
    p = tmp_path / "big.blend"
    p.write_bytes(fill if fill is not None else _periodic_bytes(size))
    return p


# ---------------------------------------------------------------- Step 1 --

def test_whole_file_uploads_in_one_put(tmp_path):
    size = 20 * MB
    f = make_file(tmp_path, size)
    transport = FakeTransport(token="tok-20mb")
    events: list[UploadProgress] = []

    token = upload_file(f, "https://upload.example/session", transport,
                         on_progress=events.append)

    assert token == "tok-20mb"
    assert len(transport.calls) == 1, "must be exactly one PUT for the whole file"
    assert transport.calls[0]["body"] == f.read_bytes()
    assert not transport.probe_calls, "no probe needed when the first PUT succeeds"


def test_progress_fires_repeatedly_and_ends_exactly_at_total(tmp_path):
    size = 20 * MB
    f = make_file(tmp_path, size)
    transport = FakeTransport()
    events: list[UploadProgress] = []

    upload_file(f, "https://upload.example/session", transport,
                on_progress=events.append, progress_interval=MB)

    assert len(events) > 5, "progress must fire repeatedly, not once at the end"
    uploaded_values = [e.uploaded for e in events]
    assert uploaded_values == sorted(uploaded_values), "must be monotonically increasing"
    assert all(a < b for a, b in zip(uploaded_values, uploaded_values[1:])), \
        "must be STRICTLY increasing"
    assert events[-1].uploaded == size
    assert all(e.total == size for e in events)


# ---------------------------------------------------------------- Step 4 --

def test_resume_sends_content_range_header_for_remainder(tmp_path):
    size = 20 * MB
    twelve_mb = 12 * MB
    content = _periodic_bytes(size)
    f = make_file(tmp_path, size, fill=content)

    transport = FakeTransport(
        token="tok-resumed",
        put_plan=[_RaiseAfter(twelve_mb, ConnectionError("connection reset")),
                  FakeResponse(200)],
        probe_plan=[twelve_mb],
    )

    token = upload_file(f, "https://upload.example/session", transport,
                         progress_interval=MB, sleep_fn=lambda _seconds: None)

    assert token == "tok-resumed"
    assert len(transport.calls) == 2, "one failed attempt, one resumed attempt"
    second = transport.calls[1]
    assert second["headers"]["Content-Range"] == \
        f"bytes {twelve_mb}-{size - 1}/{size}"
    assert second["headers"]["Content-Length"] == str(size - twelve_mb)


def test_resume_resends_only_the_remaining_bytes_not_the_whole_file(tmp_path):
    """The entire point of resume: a naive implementation that reopens the
    file at offset 0 and just re-PUTs everything would still pass a weaker
    'it eventually succeeds' test. This asserts the second PUT's body is
    EXACTLY the tail the server didn't have, both in length and content."""
    size = 20 * MB
    twelve_mb = 12 * MB
    content = _periodic_bytes(size)
    f = make_file(tmp_path, size, fill=content)

    transport = FakeTransport(
        put_plan=[_RaiseAfter(twelve_mb, ConnectionError("connection reset")),
                  FakeResponse(200)],
        probe_plan=[twelve_mb],
    )

    upload_file(f, "https://upload.example/session", transport,
                progress_interval=MB, sleep_fn=lambda _seconds: None)

    second_body = transport.calls[1]["body"]
    assert len(second_body) == size - twelve_mb, \
        "resume must send only the remainder, not the whole file"
    assert second_body == content[twelve_mb:], \
        "resumed bytes must be exactly the file's tail, not a re-read from 0"


def test_resumed_from_reported_on_the_resumed_attempt(tmp_path):
    size = 20 * MB
    twelve_mb = 12 * MB
    f = make_file(tmp_path, size)

    transport = FakeTransport(
        put_plan=[_RaiseAfter(twelve_mb, ConnectionError("boom")),
                  FakeResponse(200)],
        probe_plan=[twelve_mb],
    )
    events: list[UploadProgress] = []

    upload_file(f, "https://upload.example/session", transport,
                on_progress=events.append, progress_interval=MB,
                sleep_fn=lambda _seconds: None)

    first_attempt_events = [e for e in events if e.resumed_from == 0]
    resumed_events = [e for e in events if e.resumed_from == twelve_mb]
    assert first_attempt_events, "the failed first attempt should have reported progress too"
    assert resumed_events, "resumed_from must be reported on the resumed attempt"
    assert resumed_events[-1].uploaded == size
    # retries must have advanced by the time the resumed attempt runs
    assert resumed_events[-1].retries >= 1


def test_progress_never_regresses_when_local_reads_outrun_committed_offset(tmp_path):
    """Fix round 1, Finding 1: on_progress fires from bytes locally handed
    to read(), not from bytes the server has actually committed -- a real
    socket hands data to the OS/TCP buffer well ahead of what GCS
    acknowledges. Here the transport locally accepts 15 MB before the
    connection dies, but the server only committed 12 MB, so the resumed
    attempt's reader starts at 12 MB -- *below* a value already reported.
    The old `FakeTransport(_RaiseAfter(after_bytes,...), probe_plan=[...])`
    always had after_bytes == the probed offset, so no existing test
    (before this fix) could ever construct this divergence."""
    size = 20 * MB
    fifteen_mb = 15 * MB
    twelve_mb = 12 * MB
    content = _periodic_bytes(size)
    f = make_file(tmp_path, size, fill=content)

    transport = FakeTransport(
        token="tok-clamped",
        put_plan=[_RaiseAfter(fifteen_mb, ConnectionError("connection reset")),
                  FakeResponse(200)],
        probe_plan=[twelve_mb],
    )
    events: list[UploadProgress] = []

    token = upload_file(f, "https://upload.example/session", transport,
                         on_progress=events.append, progress_interval=MB,
                         sleep_fn=lambda _seconds: None)

    assert token == "tok-clamped"
    uploaded_values = [e.uploaded for e in events]
    assert all(b >= a for a, b in zip(uploaded_values, uploaded_values[1:])), \
        "uploaded must never go backwards, even when a resume starts lower locally"
    assert events[-1].uploaded == size

    resumed_events = [e for e in events if e.resumed_from == twelve_mb]
    assert resumed_events, "the resumed attempt must still report progress"
    assert resumed_events[0].uploaded >= fifteen_mb, (
        "the resumed attempt's first tick must be clamped to the earlier "
        "15 MB high-water mark, not dip down to the 12 MB committed offset"
    )


# ---------------------------------------------------------------- Step 5 --

def test_gives_up_after_max_retries_and_raises_upload_error(tmp_path):
    size = 5 * MB
    f = make_file(tmp_path, size)

    always_fails = FakeResponse(500, text="internal error, try again")
    transport = FakeTransport(put_plan=[always_fails], probe_plan=[0])

    with pytest.raises(UploadError) as exc_info:
        upload_file(f, "https://upload.example/session", transport,
                    max_retries=3, sleep_fn=lambda _seconds: None)

    message = str(exc_info.value)
    assert "500" in message
    assert "internal error, try again" in message
    assert exc_info.value.status == 500
    assert exc_info.value.body == "internal error, try again"


def test_upload_file_never_returns_none_on_failure(tmp_path):
    """The bug this task fixes: kaggle's _upload_blob silently returns None
    after exhausting its retries, so a 400 shows up three layers away with
    no file attached and no explanation. This must always raise instead."""
    size = 1 * MB
    f = make_file(tmp_path, size)
    transport = FakeTransport(put_plan=[FakeResponse(503, text="unavailable")],
                              probe_plan=[0])

    result = None
    raised = None
    try:
        result = upload_file(f, "https://upload.example/session", transport,
                             max_retries=1, sleep_fn=lambda _seconds: None)
    except UploadError as e:
        raised = e

    assert result is None
    assert raised is not None, "must raise UploadError rather than returning None"


# ---------------------------------------------------------------- Step 6 --

def test_empty_file_uploads_successfully(tmp_path):
    f = make_file(tmp_path, 0, fill=b"")
    transport = FakeTransport(token="tok-empty")

    token = upload_file(f, "https://upload.example/session", transport)

    assert token == "tok-empty"
    assert len(transport.calls) == 1
    assert transport.calls[0]["body"] == b""


def test_single_byte_file_uploads_successfully(tmp_path):
    f = make_file(tmp_path, 1, fill=b"X")
    transport = FakeTransport(token="tok-1b")
    events: list[UploadProgress] = []

    token = upload_file(f, "https://upload.example/session", transport,
                         on_progress=events.append)

    assert token == "tok-1b"
    assert transport.calls[0]["body"] == b"X"
    assert events[-1].uploaded == 1
    assert events[-1].total == 1


def test_offset_equal_to_total_means_already_complete_no_reupload(tmp_path):
    """The server can report the file already fully committed even though
    the client-side PUT looked like it failed (e.g. the response was lost
    after the bytes landed). Resuming must short-circuit rather than
    re-sending anything."""
    size = 3 * MB
    f = make_file(tmp_path, size)
    transport = FakeTransport(
        token="tok-already-done",
        put_plan=[ConnectionError("response lost")],
        probe_plan=[size],  # server already has everything
    )

    token = upload_file(f, "https://upload.example/session", transport,
                         sleep_fn=lambda _seconds: None)

    assert token == "tok-already-done"
    assert len(transport.calls) == 1, "must not re-upload once offset == total"
    assert len(transport.probe_calls) == 1


# -------------------------------------------------------- instrumented reader --

def test_reader_read_zero_returns_empty_not_the_whole_remainder(tmp_path):
    """File-like contract: read(0) means 'read nothing'. A naive `if size`
    check treats 0 as falsy and falls through to read-everything, which
    would silently dump the whole remaining file into a single 'chunk'."""
    from blendfleet.uploader import _InstrumentedReader, _ProgressReporter

    f = make_file(tmp_path, 10, fill=b"0123456789")
    with f.open("rb") as fp:
        reader = _InstrumentedReader(fp, start=0, total=10, progress_interval=1024,
                                      reporter=_ProgressReporter(None),
                                      resumed_from=0, retries=0)
        assert reader.read(0) == b""
        assert reader.read() == b"0123456789"


# ------------------------------------------------------------ committed_offset --

def test_committed_offset_zero_when_nothing_received(tmp_path):
    transport = FakeTransport(probe_plan=[0])
    assert committed_offset("https://upload.example/session", 10 * MB, transport) == 0


def test_committed_offset_parses_range_header(tmp_path):
    transport = FakeTransport(probe_plan=[12 * MB])
    got = committed_offset("https://upload.example/session", 20 * MB, transport)
    assert got == 12 * MB


def test_committed_offset_returns_total_when_already_finalized(tmp_path):
    transport = FakeTransport(probe_plan=[20 * MB])
    got = committed_offset("https://upload.example/session", 20 * MB, transport)
    assert got == 20 * MB


def test_committed_offset_empty_file_is_zero_without_probing(tmp_path):
    transport = FakeTransport()
    got = committed_offset("https://upload.example/session", 0, transport)
    assert got == 0
    assert not transport.probe_calls, "no server round trip needed for a 0-byte file"
