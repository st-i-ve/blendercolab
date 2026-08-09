from blendfleet.ui.formatting import format_bytes, format_eta, format_rate

MB = 1 << 20
GB = 1 << 30


# ---------------- format_bytes ----------------

def test_zero_bytes():
    assert format_bytes(0) == "0 B"


def test_negative_bytes_clamped_to_zero():
    assert format_bytes(-5) == "0 B"


def test_whole_bytes_have_no_decimal():
    assert format_bytes(512) == "512 B"


def test_kilobytes():
    assert format_bytes(2048) == "2.0 KB"


def test_megabytes():
    assert format_bytes(int(63.1 * MB)) == "63.1 MB"


def test_gigabytes():
    assert format_bytes(int(2.5 * GB)) == "2.5 GB"


def test_terabytes_is_the_top_unit_and_does_not_overflow():
    assert format_bytes(1 << 50).endswith("TB")


# ---------------- format_rate ----------------

def test_positive_rate():
    assert format_rate(2.1 * MB) == "2.1 MB/s"


def test_zero_rate_reads_as_stalled_not_zero_bps():
    assert format_rate(0) == "stalled"


def test_negative_rate_reads_as_stalled():
    assert format_rate(-100) == "stalled"


def test_none_rate_reads_as_stalled():
    assert format_rate(None) == "stalled"


# ---------------- format_eta ----------------

def test_eta_zero_rate_returns_unknown_never_inf_or_raises():
    # This is the "zero-elapsed" case: a rate computed as bytes/elapsed
    # with elapsed == 0 (the very first progress tick) comes out as 0,
    # and format_eta must treat that as "we cannot estimate yet", not
    # attempt remaining/0.
    assert format_eta(0, 100 * MB, 0) == "unknown"


def test_eta_negative_rate_also_returns_unknown():
    assert format_eta(0, 100 * MB, -1) == "unknown"


def test_eta_nothing_uploaded_yet_with_zero_elapsed_time():
    # Right at upload start: 0 bytes sent, 0 elapsed time -> rate is 0.
    assert format_eta(uploaded=0, total=10 * MB, rate_bps=0) == "unknown"


def test_eta_already_complete_is_done():
    assert format_eta(10 * MB, 10 * MB, 0) == "done"


def test_eta_uploaded_past_total_is_done_not_negative():
    assert format_eta(11 * MB, 10 * MB, 5 * MB) == "done"


def test_eta_seconds_under_a_minute():
    assert format_eta(0, 30, 1) == "30s"


def test_eta_minutes_and_seconds():
    assert format_eta(0, 125, 1) == "2m 05s"


def test_eta_hours_and_minutes():
    assert format_eta(0, 3 * 3600 + 5 * 60, 1) == "3h 05m"


def test_eta_never_divides_by_zero_total_equal_uploaded_zero():
    assert format_eta(0, 0, 0) == "done"
