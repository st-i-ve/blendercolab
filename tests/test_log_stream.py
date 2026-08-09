from blendfleet.log_stream import parse_progress, is_end_of_log, stream_progress


def test_parses_a_real_sse_line():
    # \\n (not \n): a real SSE payload carries the newline as a proper JSON
    # escape sequence, not a raw control character embedded in the string.
    line = ('data: {"stream_name":"stdout","time":14.8,'
            '"data":"PROGRESS frame=1 done=1/10\\n"}')
    assert parse_progress(line) == (1, 10)


def test_parses_later_frame():
    line = ('data: {"stream_name":"stdout","time":149.8,'
            '"data":"PROGRESS frame=10 done=10/10\\n"}')
    assert parse_progress(line) == (10, 10)


def test_ignores_stderr_and_noise():
    assert parse_progress('data: {"stream_name":"stderr","data":"warning\\n"}') is None
    assert parse_progress("") is None
    assert parse_progress("event: ping") is None
    assert parse_progress("not json at all") is None


def test_ignores_malformed_json():
    assert parse_progress('data: {"stream_name":') is None


def test_detects_end_sentinel():
    assert is_end_of_log("data: END_OF_LOG")
    assert not is_end_of_log("data: PROGRESS frame=1 done=1/2")


def test_parses_real_notebook_line_shape():
    # Exact shape emitted by blendfleet/notebook_builder.py's PROGRESS print:
    # f"PROGRESS frame={frame} ok={ok} secs={...:.1f} done={i}/{len(FRAMES)}"
    line = ('data: {"stream_name":"stdout","time":14.808170317,'
            '"data":"PROGRESS frame=1 ok=True secs=12.3 done=1/10\\n"}')
    assert parse_progress(line) == (1, 10)


# --------------------------------------------------------------------------
# stream_progress: on_telemetry rides the same SSE connection as progress
# --------------------------------------------------------------------------

class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


class _FakeSdkClient:
    def __init__(self, api_token=None, **kw):
        lines = [
            'data: {"stream_name":"stdout","data":'
            '"PROGRESS frame=1 ok=True secs=1.0 done=1/2\\n"}',
            'data: {"stream_name":"stdout","data":'
            '"TELEMETRY gpu=0 util=87 mem_used=6144 mem_total=15360 '
            'temp=71 power=58\\n"}',
            'data: {"stream_name":"stdout","data":'
            '"TELEMETRY gpu=1 util=12 mem_used=1024 mem_total=15360 '
            'temp=45 power=NA\\n"}',
            "data: END_OF_LOG",
        ]

        class _ApiClient:
            @staticmethod
            def get_kernel_session_logs_stream(req):
                return _FakeStreamResponse(lines)

        class _Kernels:
            kernels_api_client = _ApiClient()

        self.kernels = _Kernels()


def test_stream_progress_reports_telemetry_alongside_progress(monkeypatch):
    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", _FakeSdkClient)

    progress_calls = []
    telemetry_calls = []
    stream_progress(
        "KGAT_" + "a" * 32, "user0", "user0/kernel",
        on_progress=lambda done, total: progress_calls.append((done, total)),
        on_telemetry=telemetry_calls.append)

    assert progress_calls == [(1, 2)]
    assert [t["gpu"] for t in telemetry_calls] == [0, 1]
    assert telemetry_calls[0]["util"] == 87
    assert telemetry_calls[1]["power"] is None


def test_stream_progress_without_on_telemetry_ignores_telemetry_lines(monkeypatch):
    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", _FakeSdkClient)

    progress_calls = []
    # Default on_telemetry=None must not raise on TELEMETRY lines.
    stream_progress(
        "KGAT_" + "a" * 32, "user0", "user0/kernel",
        on_progress=lambda done, total: progress_calls.append((done, total)))
    assert progress_calls == [(1, 2)]
