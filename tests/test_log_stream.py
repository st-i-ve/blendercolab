from blendfleet.log_stream import parse_progress, is_end_of_log


def test_parses_a_real_sse_line():
    line = ('data: {"stream_name":"stdout","time":14.8,'
            '"data":"PROGRESS frame=1 done=1/10\n"}')
    assert parse_progress(line) == (1, 10)


def test_parses_later_frame():
    line = ('data: {"stream_name":"stdout","time":149.8,'
            '"data":"PROGRESS frame=10 done=10/10\n"}')
    assert parse_progress(line) == (10, 10)


def test_ignores_stderr_and_noise():
    assert parse_progress('data: {"stream_name":"stderr","data":"warning\n"}') is None
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
