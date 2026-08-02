"""Live progress from Kaggle's SSE log stream.

Verified 2026-07-31: GetKernelSessionLogsStream emits Server-Sent Events while
the session runs. `kernels logs` and `kernels output` return nothing until the
kernel completes, so neither can drive a live progress bar.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Callable

# blendfleet/notebook_builder.py prints exactly:
#   f"PROGRESS frame={frame} ok={ok} secs={time.time()-t0:.1f} "
#   f"done={len(done)}/{len(FRAMES)}"
# The `.*?` bridges the ok=/secs= fields between frame= and done=.
PROGRESS_RE = re.compile(r"PROGRESS frame=(\d+) .*?done=(\d+)/(\d+)")

# blendfleet/notebook_builder.py's background telemetry thread prints exactly:
#   f"TELEMETRY gpu={idx} util={util} mem_used={mem_used} "
#   f"mem_total={mem_total} temp={temp} power={power_val}"
# One line per physical GPU -- never aggregated. power_val is "NA" when
# nvidia-smi reports power.draw as "[N/A]" (some cards don't expose it).
TELEMETRY_RE = re.compile(
    r"TELEMETRY gpu=(\d+) util=(\d+) mem_used=(\d+) mem_total=(\d+) "
    r"temp=(\d+) power=(NA|[\d.]+)"
)


def is_end_of_log(line: str) -> bool:
    return "END_OF_LOG" in line


def parse_progress(line: str) -> tuple[int, int] | None:
    """Return (frames_done, total) from one SSE line, or None."""
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if obj.get("stream_name") != "stdout":
        return None
    m = PROGRESS_RE.search(obj.get("data", ""))
    if not m:
        return None
    return int(m.group(2)), int(m.group(3))


def parse_telemetry(line: str) -> dict | None:
    """Return a per-GPU telemetry record from one SSE line, or None.

    Never aggregates: each GPU's TELEMETRY line yields its own record, keyed
    by "gpu" (the physical GPU index), so two GPUs reporting independently
    produce two independent dicts.
    """
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if obj.get("stream_name") != "stdout":
        return None
    m = TELEMETRY_RE.search(obj.get("data", ""))
    if not m:
        return None
    power_raw = m.group(6)
    power = None if power_raw == "NA" else float(power_raw)
    return {
        "gpu": int(m.group(1)),
        "util": int(m.group(2)),
        "mem_used": int(m.group(3)),
        "mem_total": int(m.group(4)),
        "temp": int(m.group(5)),
        "power": power,
    }


def stream_progress(token: str, user_name: str, kernel_slug: str,
                    on_progress: Callable[[int, int], None],
                    stop_event: threading.Event | None = None,
                    on_telemetry: Callable[[dict], None] | None = None) -> None:
    """Block, calling on_progress(done, total) as lines arrive.

    `on_telemetry`, if given, is called with the parsed dict (see
    parse_telemetry) for every TELEMETRY line on the same stream -- this is
    the only source of live per-GPU utilisation/memory: it rides the exact
    same SSE connection as frame progress, so a GPU panel does not need a
    second stream of its own.

    The token is passed to KaggleClient explicitly and NEVER through
    os.environ: the dashboard starts one of these threads per account
    back-to-back, and a process-global written here was read back after an
    import plus an HTTPS client construction -- long enough that most
    threads authenticated with another account's token and then asked for a
    private log stream they had no right to.
    """
    from kagglesdk import KaggleClient
    from kagglesdk.kernels.types.kernels_api_service import (
        ApiGetKernelSessionLogsStreamRequest)

    req = ApiGetKernelSessionLogsStreamRequest()
    req.user_name = user_name
    req.kernel_slug = kernel_slug
    req.wait_for_logs_url_seconds = 30

    client = KaggleClient(api_token=token)
    resp = client.kernels.kernels_api_client.get_kernel_session_logs_stream(req)
    for raw in resp.iter_lines(decode_unicode=True):
        if stop_event is not None and stop_event.is_set():
            return
        if not raw:
            continue
        if is_end_of_log(raw):
            return
        got = parse_progress(raw)
        if got:
            on_progress(*got)
            continue
        if on_telemetry is not None:
            record = parse_telemetry(raw)
            if record:
                on_telemetry(record)
