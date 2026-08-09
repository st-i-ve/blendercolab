import json

from blendfleet.log_stream import (is_end_of_log, parse_hardware_banner,
                                   parse_progress, parse_telemetry,
                                   stream_progress)


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


# --------------------------------------------------------------------------
# parse_hardware_banner: notebook_builder.py's first cell prints
#   CPU {n} cores | RAM {x.x} GB
#   <nvidia-smi --query-gpu=name,memory.total --format=csv,noheader output>
# once per run, on the same stdout the SSE stream carries.
# --------------------------------------------------------------------------

def _sse(data: str) -> str:
    return 'data: {"stream_name":"stdout","data":' + json.dumps(data) + '}'


def test_parses_the_real_cpu_ram_banner_line():
    line = _sse("CPU 4 cores | RAM 31.3 GB\n")
    assert parse_hardware_banner(line) == {
        "kind": "cpu_ram", "cpu_count": 4, "ram_total": 31.3}


def test_parses_one_gpu_row_from_the_nvidia_smi_listing():
    # Kaggle handed back a single P100 where T4 x2 was requested -- a real
    # observed case, not a hypothetical.
    line = _sse("Tesla P100-PCIE-16GB, 16280 MiB\n")
    assert parse_hardware_banner(line) == {
        "kind": "gpu", "model": "Tesla P100-PCIE-16GB", "mem_total": 16280}


def test_parses_two_gpu_rows_from_the_nvidia_smi_listing_independently():
    # nvidia-smi's multi-line stdout is forwarded as separate lines, one
    # per physical GPU -- same convention as TELEMETRY.
    first = _sse("Tesla T4, 15360 MiB\n")
    second = _sse("Tesla T4, 15360 MiB\n")
    assert parse_hardware_banner(first) == {
        "kind": "gpu", "model": "Tesla T4", "mem_total": 15360}
    assert parse_hardware_banner(second) == {
        "kind": "gpu", "model": "Tesla T4", "mem_total": 15360}


def test_hardware_banner_parser_ignores_progress_and_telemetry_lines():
    progress_line = _sse("PROGRESS frame=1 ok=True secs=1.0 done=1/2\n")
    telemetry_line = _sse(
        "TELEMETRY gpu=0 util=87 mem_used=6144 mem_total=15360 "
        "temp=71 power=58\n")
    assert parse_hardware_banner(progress_line) is None
    assert parse_hardware_banner(telemetry_line) is None


def test_progress_and_telemetry_parsers_ignore_hardware_banner_lines():
    cpu_ram_line = _sse("CPU 4 cores | RAM 31.3 GB\n")
    gpu_line = _sse("Tesla T4, 15360 MiB\n")
    assert parse_progress(cpu_ram_line) is None
    assert parse_progress(gpu_line) is None
    assert parse_telemetry(cpu_ram_line) is None
    assert parse_telemetry(gpu_line) is None


def test_hardware_banner_ignores_stderr_and_noise():
    assert parse_hardware_banner(
        'data: {"stream_name":"stderr","data":"CPU 4 cores | RAM 31.3 GB\\n"}'
    ) is None
    assert parse_hardware_banner("") is None
    assert parse_hardware_banner("event: ping") is None
    assert parse_hardware_banner("not json at all") is None


def test_hardware_banner_ignores_malformed_json():
    assert parse_hardware_banner('data: {"stream_name":') is None


def test_hardware_banner_returns_none_for_a_partial_cpu_ram_line():
    # Truncated mid-line -- must not raise, must not guess.
    assert parse_hardware_banner(_sse("CPU 4 cores\n")) is None
    assert parse_hardware_banner(_sse("CPU four cores | RAM 31.3 GB\n")) is None
    assert parse_hardware_banner(_sse("RAM 31.3 GB\n")) is None


def test_hardware_banner_returns_none_for_unrelated_stdout():
    assert parse_hardware_banner(_sse("BLEND = /kaggle/input/x/y.blend\n")) is None
    assert parse_hardware_banner(_sse("FRAMES = [1, 2, 3]\n")) is None
    assert parse_hardware_banner(_sse("\n")) is None


class _FakeHardwareSdkClient:
    """Same shape as _FakeSdkClient above, plus the hardware banner lines
    ahead of PROGRESS/TELEMETRY -- matching the real notebook's cell order
    (hardware banner cell runs first)."""

    def __init__(self, api_token=None, **kw):
        lines = [
            _sse("CPU 4 cores | RAM 31.3 GB\n"),
            _sse("Tesla P100-PCIE-16GB, 16280 MiB\n"),
            _sse("PROGRESS frame=1 ok=True secs=1.0 done=1/2\n"),
            _sse("TELEMETRY gpu=0 util=87 mem_used=6144 mem_total=15360 "
                "temp=71 power=58\n"),
            "data: END_OF_LOG",
        ]

        class _ApiClient:
            @staticmethod
            def get_kernel_session_logs_stream(req):
                return _FakeStreamResponse(lines)

        class _Kernels:
            kernels_api_client = _ApiClient()

        self.kernels = _Kernels()


def test_stream_progress_reports_hardware_banner_alongside_progress_and_telemetry(monkeypatch):
    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", _FakeHardwareSdkClient)

    progress_calls = []
    telemetry_calls = []
    hardware_calls = []
    stream_progress(
        "KGAT_" + "a" * 32, "user0", "user0/kernel",
        on_progress=lambda done, total: progress_calls.append((done, total)),
        on_telemetry=telemetry_calls.append,
        on_hardware=hardware_calls.append)

    assert progress_calls == [(1, 2)]
    assert [t["gpu"] for t in telemetry_calls] == [0]
    assert hardware_calls == [
        {"kind": "cpu_ram", "cpu_count": 4, "ram_total": 31.3},
        {"kind": "gpu", "model": "Tesla P100-PCIE-16GB", "mem_total": 16280},
    ]


def test_stream_progress_without_on_hardware_ignores_hardware_lines(monkeypatch):
    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", _FakeHardwareSdkClient)

    progress_calls = []
    # Default on_hardware=None must not raise on hardware-banner lines.
    stream_progress(
        "KGAT_" + "a" * 32, "user0", "user0/kernel",
        on_progress=lambda done, total: progress_calls.append((done, total)))
    assert progress_calls == [(1, 2)]
