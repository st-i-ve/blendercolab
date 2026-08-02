from blendfleet.log_stream import parse_telemetry


def test_parses_a_real_sse_telemetry_line():
    # \\n (not \n): a real SSE payload carries the newline as a proper JSON
    # escape sequence, not a raw control character embedded in the string.
    line = ('data: {"stream_name":"stdout","time":30.1,'
            '"data":"TELEMETRY gpu=0 util=87 mem_used=6144 mem_total=15360 '
            'temp=71 power=58\\n"}')
    assert parse_telemetry(line) == {
        "gpu": 0, "util": 87, "mem_used": 6144, "mem_total": 15360,
        "temp": 71, "power": 58.0,
    }


def test_ignores_progress_lines():
    line = ('data: {"stream_name":"stdout","time":14.8,'
            '"data":"PROGRESS frame=1 ok=True secs=12.3 done=1/10\\n"}')
    assert parse_telemetry(line) is None


def test_ignores_stderr_and_noise():
    assert parse_telemetry(
        'data: {"stream_name":"stderr","data":"warning\\n"}') is None
    assert parse_telemetry("") is None
    assert parse_telemetry("event: ping") is None
    assert parse_telemetry("not json at all") is None


def test_ignores_malformed_json():
    assert parse_telemetry('data: {"stream_name":') is None


def test_two_gpus_report_independently():
    line0 = ('data: {"stream_name":"stdout","time":30.1,'
             '"data":"TELEMETRY gpu=0 util=87 mem_used=6144 mem_total=15360 '
             'temp=71 power=58\\n"}')
    line1 = ('data: {"stream_name":"stdout","time":30.2,'
             '"data":"TELEMETRY gpu=1 util=12 mem_used=1024 mem_total=15360 '
             'temp=45 power=22\\n"}')
    rec0 = parse_telemetry(line0)
    rec1 = parse_telemetry(line1)
    assert rec0["gpu"] == 0 and rec0["util"] == 87 and rec0["mem_used"] == 6144
    assert rec1["gpu"] == 1 and rec1["util"] == 12 and rec1["mem_used"] == 1024
    # each record stands alone -- neither is mutated/aggregated by the other
    assert rec0 != rec1


def test_handles_power_na_without_crashing():
    # power.draw comes back as "[N/A]" from nvidia-smi on some cards; the
    # notebook's sampler substitutes the literal token "NA" for the printed
    # line, and parse_telemetry must not raise on it.
    line = ('data: {"stream_name":"stdout","time":30.1,'
            '"data":"TELEMETRY gpu=0 util=87 mem_used=6144 mem_total=15360 '
            'temp=71 power=NA\\n"}')
    assert parse_telemetry(line) == {
        "gpu": 0, "util": 87, "mem_used": 6144, "mem_total": 15360,
        "temp": 71, "power": None,
    }
