"""Pure formatting helpers for the numeric/machine data shown in the UI.

No Qt, no I/O: these render the monospace byte counts, rates, frame
numbers and ETAs used by upload_view.py and charts.py, and are unit
tested in complete isolation from any widget. Every helper is defensive
about the zero/negative edge cases a live upload or an idle GPU produces
constantly (0 bytes transferred yet, 0 B/s while stalled, 0 elapsed
seconds on the very first tick) -- none of them may raise, divide by
zero, or print "inf".
"""
from __future__ import annotations

_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_bytes(n: float) -> str:
    """Human-scaled byte count, e.g. "63.1 MB".

    Negative input (should never happen, but a clamp is cheap insurance)
    is treated as zero rather than printing a negative size. Whole bytes
    are shown as an integer count with no decimal ("512 B"); anything at
    or above 1 KB gets one decimal place.
    """
    value = max(float(n), 0.0)
    unit_i = 0
    while value >= 1024 and unit_i < len(_UNITS) - 1:
        value /= 1024
        unit_i += 1
    if unit_i == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {_UNITS[unit_i]}"


def format_rate(bytes_per_second: float) -> str:
    """e.g. "2.1 MB/s".

    A rate that is zero or negative reads as "stalled", never "0.0 B/s" --
    a slow-but-moving upload must look visibly different from one that
    has stopped, which is the entire point of showing a rate at all.
    """
    if bytes_per_second is None or bytes_per_second <= 0:
        return "stalled"
    return f"{format_bytes(bytes_per_second)}/s"


def format_eta(uploaded: float, total: float, rate_bps: float) -> str:
    """Estimated time remaining to reach `total` at `rate_bps`.

    Never divides by zero and never prints "inf":
    - nothing left to send (uploaded >= total, including the zero-elapsed
      instant right after an upload starts from a full resume) -> "done"
    - a zero or negative rate (stalled, or the very first tick before any
      throughput has been measured) -> "unknown", not a bogus huge number
    """
    remaining = total - uploaded
    if remaining <= 0:
        return "done"
    if rate_bps is None or rate_bps <= 0:
        return "unknown"
    seconds = remaining / rate_bps
    return _format_duration(seconds)


def _format_duration(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        minutes, secs = divmod(int(seconds), 60)
        return f"{minutes}m {secs:02d}s"
    hours, rem = divmod(int(seconds), 3600)
    minutes, _ = divmod(rem, 60)
    return f"{hours}h {minutes:02d}m"
