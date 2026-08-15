"""Live progress from Kaggle's SSE log stream.

Verified 2026-07-31: GetKernelSessionLogsStream emits Server-Sent Events while
the session runs. `kernels logs` and `kernels output` return nothing until the
kernel completes, so neither can drive a live progress bar.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable

from blendfleet import crash_log
from blendfleet.kaggle_http import install_request_timeout

# blendfleet/notebook_builder.py prints exactly:
#   f"PROGRESS frame={frame} ok={ok} secs={time.time()-t0:.1f} "
#   f"done={len(done)}/{len(FRAMES)}"
# The `.*?` bridges the ok=/secs= fields between frame= and done=.
PROGRESS_RE = re.compile(r"PROGRESS frame=(\d+) .*?done=(\d+)/(\d+)")


def _tokenless(text: str, token: str) -> str:
    """`text` with this stream's own API token reduced to a fragment.

    Every line this module writes to the diagnostic log quotes an
    exception from the Kaggle SDK, which is free to echo back the request
    it was given -- and that log is a file the user is asked to send on
    when something goes wrong. Mirrors kaggle_client._mask's fragment so a
    masked token reads the same wherever it appears.
    """
    if token and len(token) > 12 and token in text:
        return text.replace(token, f"{token[:9]}…")
    return text

# blendfleet/notebook_builder.py's background telemetry thread prints exactly:
#   f"TELEMETRY gpu={idx} util={util} mem_used={mem_used} "
#   f"mem_total={mem_total} temp={temp} power={power_val}"
# One line per physical GPU -- never aggregated. power_val is "NA" when
# nvidia-smi reports power.draw as "[N/A]" (some cards don't expose it).
TELEMETRY_RE = re.compile(
    r"TELEMETRY gpu=(\d+) util=(\d+) mem_used=(\d+) mem_total=(\d+) "
    r"temp=(\d+) power=(NA|[\d.]+)"
)

# System RAM and CPU, sampled on the same tick as the GPUs. A separate
# marker from TELEMETRY on purpose: TELEMETRY_RE requires gpu=, and
# widening it to make one regex serve both would make every consumer
# guess which kind of record it had been handed.
SYSTEM_RE = re.compile(
    r"SYSTEM ram_used=(\d+) ram_total=(\d+) cpu_pct=(\d+)"
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

# blendfleet/notebook_builder.py's first cell prints exactly ONE PREFLIGHT
# line, before anything else in the whole notebook -- before the CPU/RAM +
# nvidia-smi hardware banner above, and before the next cell even starts
# downloading Blender:
#   f"PREFLIGHT gpus={len(gpu_names)} "
#   f"gpu_names={'|'.join(gpu_names) if gpu_names else 'none'} "
#   f"cpu={cpu_count} ram={ram_total:.1f}"
# Unlike TELEMETRY/the hardware banner, this is never one-per-GPU: it is
# the single fact the desktop app needs, seconds after the kernel starts,
# to decide whether to keep going or stop -- so gpu_names is a single
# '|'-joined field ("Tesla T4|Tesla T4"), not separate lines to reassemble.
PREFLIGHT_RE = re.compile(
    r"PREFLIGHT gpus=(\d+) gpu_names=(.*?) cpu=(\d+) ram=([\d.]+)"
)


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


# Re-exported under its original private name so the call site below, and
# the tests that monkeypatch it there, keep working. kaggle_client.py needs
# the identical helper, and one shared implementation is the point.
_install_request_timeout = install_request_timeout


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


def parse_system(line: str) -> dict | None:
    """Return a system RAM/CPU sample from one SSE line, or None.

    Bytes as reported, not gigabytes: the conversion is a presentation
    choice, and rounding here would make the only live memory reading
    this app has lossy before anything could use it.
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
    m = SYSTEM_RE.search(obj.get("data", ""))
    if not m:
        return None
    return {
        "ram_used": int(m.group(1)),
        "ram_total": int(m.group(2)),
        "cpu_pct": int(m.group(3)),
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


def parse_preflight(line: str) -> dict | None:
    """Return one record from the notebook's PREFLIGHT line, or None.

    Same gate as parse_progress/parse_telemetry/parse_hardware_banner: only
    a `data:` SSE line whose payload is valid JSON with
    stream_name == "stdout" is even considered.

    Returns {"gpu_count": int, "gpu_names": list[str], "cpu_count": int,
    "ram_total": float}. gpu_names is [] for a CPU-only session (the
    notebook prints the literal "none" for that case, never an empty
    string, so a truncated/malformed line can't be mistaken for one).
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
    m = PREFLIGHT_RE.search(obj.get("data", ""))
    if not m:
        return None
    names_raw = m.group(2)
    gpu_names = [] if names_raw == "none" else names_raw.split("|")
    return {
        "gpu_count": int(m.group(1)),
        "gpu_names": gpu_names,
        "cpu_count": int(m.group(3)),
        "ram_total": float(m.group(4)),
    }


def stream_progress(token: str, user_name: str, kernel_slug: str,
                    on_progress: Callable[[int, int], None],
                    stop_event: threading.Event | None = None,
                    on_telemetry: Callable[[dict], None] | None = None,
                    on_hardware: Callable[[dict], None] | None = None,
                    on_preflight: Callable[[dict], None] | None = None,
                    on_system: Callable[[dict], None] | None = None,
                    max_reconnects: int = 5,
                    sleep: Callable[[float], None] = time.sleep) -> None:
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

    `on_preflight`, if given, is called with the parsed dict (see
    parse_preflight) for the single PREFLIGHT line the notebook prints
    before anything else -- before the hardware banner above, and before
    the next cell even starts downloading Blender. This is how a caller
    finds out real hardware within seconds of the kernel starting, not
    only once telemetry/the hardware banner arrive later.

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

    # Lines already dispatched, across every connection this call makes.
    # Kaggle replays the log from the top on each new stream, so a
    # reconnect re-delivers everything already seen -- skipping by count
    # keeps one PROGRESS line to one on_progress call, and stops replayed
    # TELEMETRY from briefly showing a GPU's state from five minutes ago.
    seen_lines = 0
    attempt = 0

    while True:
        if stop_event is not None and stop_event.is_set():
            return

        try:
            client = KaggleClient(api_token=token)
            _install_request_timeout(
                client, (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS))
            resp = client.kernels.kernels_api_client \
                .get_kernel_session_logs_stream(req)
        except Exception as e:  # noqa: BLE001
            if stop_event is not None and stop_event.is_set():
                return
            attempt += 1
            if attempt > max_reconnects:
                raise
            # Retried, not swallowed -- but every retry until the last one
            # used to be invisible, so "it took four minutes to show any
            # progress" and "Kaggle was refusing the stream outright" read
            # identically. Bounded by max_reconnects, so this cannot flood.
            crash_log.record(_tokenless(
                f"log stream for {user_name}/{kernel_slug}: could not open "
                f"it (attempt {attempt} of {max_reconnects}), retrying. "
                f"{type(e).__name__}: {e}", token))
            sleep(min(2 ** (attempt - 1), 8))
            continue

        finished = threading.Event()
        closer: threading.Thread | None = None
        if stop_event is not None:
            closer = threading.Thread(
                target=_close_when_stopped, args=(resp, stop_event, finished),
                name="blendfleet-log-stream-closer", daemon=True)
            closer.start()

        progressed = False
        try:
            index = 0
            for raw in resp.iter_lines(decode_unicode=True):
                if stop_event is not None and stop_event.is_set():
                    return
                # Counted before anything else, blank lines included, so
                # the index means the same thing on every connection.
                index += 1
                if index <= seen_lines:
                    continue
                seen_lines = index
                progressed = True
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
                if on_system is not None:
                    sys_record = parse_system(raw)
                    if sys_record:
                        on_system(sys_record)
                        continue
                if on_preflight is not None:
                    pf_record = parse_preflight(raw)
                    if pf_record:
                        on_preflight(pf_record)
                        continue
                if on_hardware is not None:
                    hw_record = parse_hardware_banner(raw)
                    if hw_record:
                        on_hardware(hw_record)
        except Exception as e:  # noqa: BLE001
            # A dropped stream is not a failed render. Measured on a real
            # 15-frame run (2026-08-11): ChunkedEncodingError at frame 3,
            # after which the app showed 3/15 for seven minutes while the
            # kernel quietly finished all fifteen. Reconnect instead.
            if stop_event is not None and stop_event.is_set():
                return
            # Recorded on the way past. When the reconnect works nobody is
            # told anything at all -- correctly, since the render is fine --
            # so a stream dropping repeatedly looks exactly like a slow
            # render from the outside, and this line is the only thing that
            # can separate them afterwards.
            crash_log.record(_tokenless(
                f"log stream for {user_name}/{kernel_slug}: dropped mid-"
                f"stream after {seen_lines} line(s) (attempt {attempt} of "
                f"{max_reconnects}). {type(e).__name__}: {e}", token))
            if attempt >= max_reconnects and not progressed:
                raise
        finally:
            # Order matters: release the closer first so it cannot outlive
            # this iteration, then drop the connection, then make sure the
            # closer really is gone -- a stream thread that has "finished"
            # while quietly leaving a helper behind is the same leak in a
            # smaller costume.
            finished.set()
            try:
                resp.close()
            except Exception:       # noqa: BLE001
                pass
            if closer is not None:
                closer.join(timeout=STOP_POLL_SECONDS * 8)

        if stop_event is not None and stop_event.is_set():
            return
        # Reaching here means the stream ended WITHOUT the end-of-log
        # marker -- either an error above, or a body that simply stopped.
        # A reconnect that delivered new lines is progress, so the budget
        # resets; one that delivered nothing new counts against it, which
        # is what stops a finished-but-unmarked log looping forever.
        attempt = 0 if progressed else attempt + 1
        if attempt > max_reconnects:
            return
        sleep(min(2 ** (max(attempt, 1) - 1), 8))
