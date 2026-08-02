"""Live progress from Kaggle's SSE log stream.

Verified 2026-07-31: GetKernelSessionLogsStream emits Server-Sent Events while
the session runs. `kernels logs` and `kernels output` return nothing until the
kernel completes, so neither can drive a live progress bar.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Callable

# blendfleet/notebook_builder.py prints exactly:
#   f"PROGRESS frame={frame} ok={ok} secs={time.time()-t0:.1f} "
#   f"done={len(done)}/{len(FRAMES)}"
# The `.*?` bridges the ok=/secs= fields between frame= and done=.
PROGRESS_RE = re.compile(r"PROGRESS frame=(\d+) .*?done=(\d+)/(\d+)")


def is_end_of_log(line: str) -> bool:
    return "END_OF_LOG" in line


def parse_progress(line: str) -> tuple[int, int] | None:
    """Return (frames_done, total) from one SSE line, or None."""
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    try:
        # strict=False: some observed payloads carry a raw, unescaped control
        # character (e.g. a literal newline) inside the "data" string value,
        # which strict JSON rejects even though the value is otherwise fine.
        obj = json.loads(payload, strict=False)
    except (json.JSONDecodeError, ValueError):
        return None
    if obj.get("stream_name") != "stdout":
        return None
    m = PROGRESS_RE.search(obj.get("data", ""))
    if not m:
        return None
    return int(m.group(2)), int(m.group(3))


def stream_progress(token: str, user_name: str, kernel_slug: str,
                    on_progress: Callable[[int, int], None],
                    stop_event: threading.Event | None = None) -> None:
    """Block, calling on_progress(done, total) as lines arrive."""
    os.environ["KAGGLE_API_TOKEN"] = token
    from kagglesdk import KaggleClient
    from kagglesdk.kernels.types.kernels_api_service import (
        ApiGetKernelSessionLogsStreamRequest)

    req = ApiGetKernelSessionLogsStreamRequest()
    req.user_name = user_name
    req.kernel_slug = kernel_slug
    req.wait_for_logs_url_seconds = 30

    resp = KaggleClient().kernels.kernels_api_client.get_kernel_session_logs_stream(req)
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
