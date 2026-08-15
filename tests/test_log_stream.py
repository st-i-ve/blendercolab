import json

from blendfleet.log_stream import (is_end_of_log, parse_hardware_banner,
                                   parse_preflight, parse_progress,
                                   parse_telemetry, stream_progress)


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


# --------------------------------------------------------------------------
# parse_preflight: notebook_builder.py's first cell prints exactly ONE
# PREFLIGHT line, before anything else -- before the hardware banner above,
# and before the next cell downloads Blender:
#   f"PREFLIGHT gpus={len(gpu_names)} gpu_names={...} cpu={cpu_count} "
#   f"ram={ram_total:.1f}"
# --------------------------------------------------------------------------

def test_parses_the_real_preflight_line_with_two_gpus():
    line = _sse("PREFLIGHT gpus=2 gpu_names=Tesla T4|Tesla T4 cpu=4 ram=31.3\n")
    assert parse_preflight(line) == {
        "gpu_count": 2, "gpu_names": ["Tesla T4", "Tesla T4"],
        "cpu_count": 4, "ram_total": 31.3}


def test_parses_a_preflight_line_with_no_gpus():
    # CPU-only session: the notebook prints the literal "none", never an
    # empty gpu_names field.
    line = _sse("PREFLIGHT gpus=0 gpu_names=none cpu=4 ram=31.3\n")
    assert parse_preflight(line) == {
        "gpu_count": 0, "gpu_names": [], "cpu_count": 4, "ram_total": 31.3}


def test_parses_a_single_gpu_preflight_line():
    line = _sse("PREFLIGHT gpus=1 gpu_names=Tesla P100-PCIE-16GB cpu=4 ram=31.3\n")
    assert parse_preflight(line) == {
        "gpu_count": 1, "gpu_names": ["Tesla P100-PCIE-16GB"],
        "cpu_count": 4, "ram_total": 31.3}


def test_preflight_ignores_stderr_and_noise():
    assert parse_preflight(
        'data: {"stream_name":"stderr","data":"PREFLIGHT gpus=1 '
        'gpu_names=Tesla T4 cpu=4 ram=31.3\\n"}') is None
    assert parse_preflight("") is None
    assert parse_preflight("event: ping") is None
    assert parse_preflight("not json at all") is None


def test_preflight_ignores_malformed_json():
    assert parse_preflight('data: {"stream_name":') is None


def test_preflight_parser_ignores_progress_telemetry_and_hardware_lines():
    progress_line = _sse("PROGRESS frame=1 ok=True secs=1.0 done=1/2\n")
    telemetry_line = _sse(
        "TELEMETRY gpu=0 util=87 mem_used=6144 mem_total=15360 "
        "temp=71 power=58\n")
    cpu_ram_line = _sse("CPU 4 cores | RAM 31.3 GB\n")
    gpu_line = _sse("Tesla T4, 15360 MiB\n")
    assert parse_preflight(progress_line) is None
    assert parse_preflight(telemetry_line) is None
    assert parse_preflight(cpu_ram_line) is None
    assert parse_preflight(gpu_line) is None


def test_other_parsers_ignore_preflight_lines():
    line = _sse("PREFLIGHT gpus=2 gpu_names=Tesla T4|Tesla T4 cpu=4 ram=31.3\n")
    assert parse_progress(line) is None
    assert parse_telemetry(line) is None
    assert parse_hardware_banner(line) is None


class _FakePreflightSdkClient:
    """Same shape as _FakeHardwareSdkClient above, plus the PREFLIGHT line
    ahead of everything else -- matching the real notebook's cell order
    (PREFLIGHT prints before the hardware banner, which prints before
    PROGRESS/TELEMETRY)."""

    def __init__(self, api_token=None, **kw):
        lines = [
            _sse("PREFLIGHT gpus=2 gpu_names=Tesla T4|Tesla T4 cpu=4 ram=31.3\n"),
            _sse("CPU 4 cores | RAM 31.3 GB\n"),
            _sse("Tesla T4, 15360 MiB\n"),
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


def test_stream_progress_reports_preflight_alongside_everything_else(monkeypatch):
    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", _FakePreflightSdkClient)

    progress_calls = []
    telemetry_calls = []
    hardware_calls = []
    preflight_calls = []
    stream_progress(
        "KGAT_" + "a" * 32, "user0", "user0/kernel",
        on_progress=lambda done, total: progress_calls.append((done, total)),
        on_telemetry=telemetry_calls.append,
        on_hardware=hardware_calls.append,
        on_preflight=preflight_calls.append)

    assert preflight_calls == [{
        "gpu_count": 2, "gpu_names": ["Tesla T4", "Tesla T4"],
        "cpu_count": 4, "ram_total": 31.3}]
    assert hardware_calls == [
        {"kind": "cpu_ram", "cpu_count": 4, "ram_total": 31.3},
        {"kind": "gpu", "model": "Tesla T4", "mem_total": 15360}]
    assert progress_calls == [(1, 2)]
    assert [t["gpu"] for t in telemetry_calls] == [0]


def test_stream_progress_without_on_preflight_ignores_preflight_lines(monkeypatch):
    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", _FakePreflightSdkClient)

    progress_calls = []
    # Default on_preflight=None must not raise on a PREFLIGHT line, and
    # must not swallow the lines that come after it.
    stream_progress(
        "KGAT_" + "a" * 32, "user0", "user0/kernel",
        on_progress=lambda done, total: progress_calls.append((done, total)))
    assert progress_calls == [(1, 2)]


# --------------------------------------------------------------------------
# Live frame previews.
#
# "the preview works after all the renders are complete, can we make it
# work when the frame is available? I might want to see what was happening
# mid render without stopping the process." (2026-08-15)
#
# `kernels output` returns NOTHING until a session ends -- this module's
# own docstring, and a real reading on 2026-08-15 where Kaggle listed 0
# output files for a kernel that was mid-render. So a frame cannot be
# fetched while the render runs; the notebook pushes a small JPEG down
# this same stream instead, chunked across several lines because nothing
# documents how long a single captured log line may be.
# --------------------------------------------------------------------------

def _thumb_line(frame, part, parts, nbytes, data):
    return _sse(f"THUMB frame={frame} part={part}/{parts} "
                f"bytes={nbytes} {data}\n")


def _b64_of(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode("ascii")


def test_parses_one_chunk_of_a_preview():
    from blendfleet.log_stream import parse_thumbnail_part

    assert parse_thumbnail_part(_thumb_line(7, 2, 3, 9000, "QUJD")) == {
        "frame": 7, "part": 2, "parts": 3, "bytes": 9000, "data": "QUJD"}


def test_a_chunk_parser_ignores_stderr_noise_and_malformed_json():
    from blendfleet.log_stream import parse_thumbnail_part

    assert parse_thumbnail_part(
        'data: {"stream_name":"stderr","data":"THUMB frame=1 part=1/1 '
        'bytes=3 QUJD\\n"}') is None
    assert parse_thumbnail_part("") is None
    assert parse_thumbnail_part("event: ping") is None
    assert parse_thumbnail_part("not json at all") is None
    assert parse_thumbnail_part('data: {"stream_name":') is None


def test_a_chunk_outside_its_own_set_is_refused():
    """part=0/3 or part=4/3 is a mangled line, not a preview -- taking it
    would let a nonsense index sit in the assembler forever."""
    from blendfleet.log_stream import parse_thumbnail_part

    assert parse_thumbnail_part(_thumb_line(1, 0, 3, 10, "QQ==")) is None
    assert parse_thumbnail_part(_thumb_line(1, 4, 3, 10, "QQ==")) is None
    assert parse_thumbnail_part(_thumb_line(1, 1, 0, 10, "QQ==")) is None


def test_the_other_parsers_ignore_a_thumbnail_line():
    """Every marker owns its own regex. A THUMB line reaching the hardware
    banner parser would be read as an nvidia-smi row."""
    line = _thumb_line(1, 1, 1, 3, "QUJD")
    assert parse_progress(line) is None
    assert parse_telemetry(line) is None
    assert parse_hardware_banner(line) is None
    assert parse_preflight(line) is None


def test_a_thumbnail_parser_ignores_every_other_marker():
    from blendfleet.log_stream import parse_thumbnail_part

    for line in (_sse("PROGRESS frame=1 ok=True secs=1.0 done=1/2\n"),
                 _sse("TELEMETRY gpu=0 util=87 mem_used=6144 "
                      "mem_total=15360 temp=71 power=58\n"),
                 _sse("SYSTEM ram_used=1 ram_total=2 cpu_pct=3\n"),
                 _sse("Tesla T4, 15360 MiB\n")):
        assert parse_thumbnail_part(line) is None


def test_a_chunked_preview_is_reassembled_into_the_original_bytes():
    from blendfleet.log_stream import ThumbnailAssembler, parse_thumbnail_part

    raw = bytes(range(256)) * 12          # 3072 bytes -> 4096 base64 chars
    b64 = _b64_of(raw)
    chunks = [b64[i:i + 1500] for i in range(0, len(b64), 1500)]
    asm = ThumbnailAssembler()
    got = None
    for i, chunk in enumerate(chunks, 1):
        part = parse_thumbnail_part(
            _thumb_line(4, i, len(chunks), len(raw), chunk))
        got = asm.add(part)
        if i < len(chunks):
            assert got is None, "a half-arrived preview must not be emitted"
    assert got == {"frame": 4, "jpeg_b64": b64, "bytes": len(raw)}


def test_a_set_missing_a_part_is_discarded_not_emitted():
    """A frame whose middle chunk never arrived must produce NOTHING. Half
    a JPEG shown as the frame would read as a rendering fault in a render
    that is perfectly healthy."""
    from blendfleet.log_stream import ThumbnailAssembler, parse_thumbnail_part

    raw = b"\xff\xd8" + b"x" * 3000
    b64 = _b64_of(raw)
    chunks = [b64[i:i + 1200] for i in range(0, len(b64), 1200)]
    asm = ThumbnailAssembler()
    results = []
    for i, chunk in enumerate(chunks, 1):
        if i == 2:
            continue            # the line Kaggle dropped
        results.append(asm.add(parse_thumbnail_part(
            _thumb_line(9, i, len(chunks), len(raw), chunk))))
    assert results and all(r is None for r in results)


def test_an_unfinished_set_is_dropped_when_the_next_frame_starts():
    """Previews arrive in frame order down one stdout, so a part of frame
    N+1 is proof frame N's set will never complete. Frame N+1 must not
    inherit frame N's leftovers."""
    from blendfleet.log_stream import ThumbnailAssembler, parse_thumbnail_part

    raw = b"hello world" * 4
    b64 = _b64_of(raw)
    asm = ThumbnailAssembler()
    # Frame 1 announces two parts and sends only the first.
    assert asm.add(parse_thumbnail_part(
        _thumb_line(1, 1, 2, len(raw), b64[:8]))) is None
    # Frame 2 arrives whole.
    got = asm.add(parse_thumbnail_part(
        _thumb_line(2, 1, 1, len(raw), b64)))
    assert got == {"frame": 2, "jpeg_b64": b64, "bytes": len(raw)}


def test_a_truncated_chunk_is_caught_by_the_declared_byte_count():
    """The exact risk chunking exists for: a line that survived but was
    clipped. Every line carries the total decoded size, so the pieces not
    adding up to it is detectable rather than decoded into rubbish."""
    from blendfleet.log_stream import ThumbnailAssembler, parse_thumbnail_part

    raw = b"\x00\x11\x22\x33" * 64        # 256 bytes
    b64 = _b64_of(raw)
    asm = ThumbnailAssembler()
    # One line, declaring 256 bytes, carrying only two thirds of them.
    clipped = b64[:(len(b64) // 4) * 4 - 40]
    assert asm.add(parse_thumbnail_part(
        _thumb_line(3, 1, 1, len(raw), clipped))) is None


def test_a_chunk_that_is_not_valid_base64_is_discarded():
    from blendfleet.log_stream import ThumbnailAssembler, parse_thumbnail_part

    asm = ThumbnailAssembler()
    # Three characters cannot be a whole base64 group; the set decodes to
    # nothing usable and must not be handed on.
    assert asm.add(parse_thumbnail_part(_thumb_line(5, 1, 1, 3, "QUJ"))) is None


class _FakeThumbnailSdkClient:
    """A stream carrying one preview split over three lines, in among the
    ordinary progress traffic -- the real shape, where a THUMB set is
    interrupted by nothing but is surrounded by PROGRESS/TELEMETRY."""

    RAW = bytes(range(256)) * 6            # 1536 bytes

    def __init__(self, api_token=None, **kw):
        b64 = _b64_of(self.RAW)
        third = len(b64) // 3 + 1
        chunks = [b64[i:i + third] for i in range(0, len(b64), third)]
        lines = [_sse("PROGRESS frame=1 ok=True secs=1.0 done=1/2\n")]
        lines += [_thumb_line(1, i, len(chunks), len(self.RAW), c)
                  for i, c in enumerate(chunks, 1)]
        lines += [
            _sse("TELEMETRY gpu=0 util=87 mem_used=6144 mem_total=15360 "
                 "temp=71 power=58\n"),
            _sse("PROGRESS frame=2 ok=True secs=1.0 done=2/2\n"),
            "data: END_OF_LOG",
        ]

        class _ApiClient:
            @staticmethod
            def get_kernel_session_logs_stream(req):
                return _FakeStreamResponse(lines)

        class _Kernels:
            kernels_api_client = _ApiClient()

        self.kernels = _Kernels()


def test_stream_progress_hands_over_one_whole_preview(monkeypatch):
    import base64

    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", _FakeThumbnailSdkClient)

    progress_calls = []
    thumbs = []
    stream_progress(
        "KGAT_" + "a" * 32, "user0", "user0/kernel",
        on_progress=lambda done, total: progress_calls.append((done, total)),
        on_telemetry=lambda r: None,
        on_thumbnail=thumbs.append)

    assert progress_calls == [(1, 2), (2, 2)], \
        "previews must not swallow the progress lines around them"
    assert len(thumbs) == 1, "three lines, one picture"
    assert thumbs[0]["frame"] == 1
    assert base64.b64decode(thumbs[0]["jpeg_b64"]) == \
        _FakeThumbnailSdkClient.RAW


def test_stream_progress_without_on_thumbnail_ignores_preview_lines(monkeypatch):
    """Nobody watching means nothing reassembled -- and, crucially, the
    lines after the preview still arrive."""
    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", _FakeThumbnailSdkClient)

    progress_calls = []
    stream_progress(
        "KGAT_" + "a" * 32, "user0", "user0/kernel",
        on_progress=lambda done, total: progress_calls.append((done, total)))
    assert progress_calls == [(1, 2), (2, 2)]
