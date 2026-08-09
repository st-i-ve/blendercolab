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

# blendfleet/notebook_builder.py's first cell prints, once per run, to the
# same stdout the SSE stream carries:
#   print(f"CPU {psutil.cpu_count(logical=True)} cores | RAM {vm.total/2**30:.1f} GB")
#   print(<nvidia-smi --query-gpu=name,memory.total --format=csv,noheader output>)
# The second print's argument is itself multi-line -- one row per physical
# GPU, e.g. "Tesla T4, 15360 MiB" -- and Kaggle's log capture forwards it as
# separate stdout lines, so each GPU row arrives as its own SSE frame, just
# like TELEMETRY's one-line-per-GPU convention. The GPU count is never
# fixed (a GPU request has come back a single P100 instead of the T4 x2
# that was asked for), so the number of GPU rows genuinely varies.
HARDWARE_CPU_RAM_RE = re.compile(r"CPU (\d+) cores \| RAM ([\d.]+) GB")
HARDWARE_GPU_RE = re.compile(r"^(.+?),\s*(\d+)\s*MiB$")


# How long a stream thread may sit inside the network stack with no way to
# notice stop_event. kagglesdk passes no timeout at all to requests
# (kaggle_http_client.py: `self._session.send(http_request, **settings)`,
# where settings comes from merge_environment_settings and never carries
# one), so without this a thread blocked in connect/TLS-handshake/read
# waits forever -- which is precisely the "closeEvent cannot wait it out"
# failure: N daemon threads still mid-SSL while Qt tears the window down.
#
# The read timeout is generous on purpose. The kernel's own telemetry
# thread prints a TELEMETRY line every 5s (notebook_builder.py) and the
# request itself may be held open for up to wait_for_logs_url_seconds (30)
# before the first byte, so 120s is far longer than any healthy gap while
# still bounding a dead connection.
CONNECT_TIMEOUT_SECONDS = 20.0
READ_TIMEOUT_SECONDS = 120.0

# How often the closer thread re-checks stop_event. Small: this is the
# latency between "the user closed the window" and "the blocked socket is
# torn down", and closeEvent waits on it.
STOP_POLL_SECONDS = 0.25


def _install_request_timeout(client, timeout) -> bool:
    """Give `client`'s requests.Session a default timeout.

    kagglesdk exposes no timeout parameter anywhere, so the only injection
    point is the Session it builds internally. Best-effort by design: if a
    future kagglesdk reshuffles its internals this returns False and the
    stream still runs (just without the backstop) rather than taking the
    dashboard down over a private attribute.
    """
    try:
        http = client.http_client()
        http._init_session()
        session = http._session
        if session is None:
            return False
        original_send = session.send

        def send(request, **kwargs):
            kwargs.setdefault("timeout", timeout)
            return original_send(request, **kwargs)

        session.send = send
        return True
    except Exception:      # noqa: BLE001 -- a missing internal is not fatal
        return False


def _close_when_stopped(resp, stop_event: threading.Event,
                        finished: threading.Event) -> None:
    """Close `resp` as soon as `stop_event` is set.

    stop_event used to be checked only BETWEEN received lines, so a thread
    parked in a blocking socket read never saw it -- the stream was
    effectively unstoppable and closeEvent had nothing it could wait for.
    Closing the response from here makes the blocked read raise, which
    unwinds the streaming thread immediately.
    """
    while not finished.is_set():
        if stop_event.wait(STOP_POLL_SECONDS):
            try:
                resp.close()
            except Exception:   # noqa: BLE001 -- already tearing down
                pass
            return


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


def parse_hardware_banner(line: str) -> dict | None:
    """Return one record from the notebook's first-cell hardware banner, or None.

    Two distinct shapes come out of the same banner, on separate stdout
    lines (see the comment above HARDWARE_CPU_RAM_RE):
      - {"kind": "cpu_ram", "cpu_count": int, "ram_total": float}
      - {"kind": "gpu", "model": str, "mem_total": int}  -- one per GPU row

    Same gate as parse_progress/parse_telemetry: only a `data:` SSE line
    whose payload is valid JSON with stream_name == "stdout" is even
    considered, so stderr, malformed JSON, and non-`data:` lines never
    reach the regexes below. PROGRESS/TELEMETRY lines are also plain
    stdout text on the same stream, so they are explicitly excluded before
    the GPU-row regex gets a chance at them, rather than trusting the two
    shapes to never collide by accident.
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
    data = obj.get("data", "")
    m = HARDWARE_CPU_RAM_RE.search(data)
    if m:
        return {"kind": "cpu_ram", "cpu_count": int(m.group(1)),
                "ram_total": float(m.group(2))}
    if PROGRESS_RE.search(data) or TELEMETRY_RE.search(data):
        return None
    m = HARDWARE_GPU_RE.match(data.strip())
    if not m:
        return None
    model = m.group(1).strip()
    if not model:
        return None
    return {"kind": "gpu", "model": model, "mem_total": int(m.group(2))}


def stream_progress(token: str, user_name: str, kernel_slug: str,
                    on_progress: Callable[[int, int], None],
                    stop_event: threading.Event | None = None,
                    on_telemetry: Callable[[dict], None] | None = None,
                    on_hardware: Callable[[dict], None] | None = None) -> None:
    """Block, calling on_progress(done, total) as lines arrive.

    `on_telemetry`, if given, is called with the parsed dict (see
    parse_telemetry) for every TELEMETRY line on the same stream -- this is
    the only source of live per-GPU utilisation/memory: it rides the exact
    same SSE connection as frame progress, so a GPU panel does not need a
    second stream of its own.

    `on_hardware`, if given, is called with the parsed dict (see
    parse_hardware_banner) for every hardware-banner line on the same
    stream -- the notebook's first cell prints CPU count, total RAM, and
    the nvidia-smi GPU listing exactly once per run, and this is the only
    way to see that text: no new network call, same SSE connection.

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

    # Cheapest possible stop: never open a connection at all if the caller
    # has already asked everything to unwind (window closed between the
    # thread being started and it getting scheduled).
    if stop_event is not None and stop_event.is_set():
        return

    client = KaggleClient(api_token=token)
    _install_request_timeout(client,
                             (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS))
    resp = client.kernels.kernels_api_client.get_kernel_session_logs_stream(req)

    finished = threading.Event()
    closer: threading.Thread | None = None
    if stop_event is not None:
        closer = threading.Thread(
            target=_close_when_stopped, args=(resp, stop_event, finished),
            name="blendfleet-log-stream-closer", daemon=True)
        closer.start()

    try:
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
                    continue
            if on_hardware is not None:
                hw_record = parse_hardware_banner(raw)
                if hw_record:
                    on_hardware(hw_record)
    finally:
        # Order matters: release the closer first so it cannot outlive this
        # call, then drop the connection, then make sure the closer really
        # is gone before returning -- a stream thread that has "finished"
        # while quietly leaving a helper behind is the same leak in a
        # smaller costume.
        finished.set()
        try:
            resp.close()
        except Exception:       # noqa: BLE001
            pass
        if closer is not None:
            closer.join(timeout=STOP_POLL_SECONDS * 8)
