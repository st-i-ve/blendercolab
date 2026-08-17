from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from blendfleet.blender_versions import (DEFAULT_VERSION, download_url,
                                          validate_version)

# THE SCENE IS LOADED ONCE, AND EVERY FRAME IS RENDERED INSIDE THIS ONE
# PROCESS.
#
# This script used to do setup only, and the notebook drove frames by
# running Blender once per frame with -f N. That works, but it pays
# Blender's startup, CUDA/OptiX context creation and a full parse of the
# .blend FOR EVERY FRAME. Measured on a real 15-frame render of a 66 MB
# scene (2026-08-11): 35s per frame of which the GPUs were busy for only
# ~25% of the wall clock -- both T4s sat idle while frame N+1's copy of
# Blender re-read the same file frame N had just finished with.
#
# So the loop lives here now, on the far side of that one-time cost.
# Blender's own -f accepts a frame list and would also load once, but then
# Blender owns the loop: per-frame PROGRESS would have to be scraped out
# of its stdout, and one bad frame would kill every frame after it.
# Looping in Python keeps the exact PROGRESS format log_stream.py parses,
# and keeps a failed frame to ONE failed frame.
#
# LIVE PREVIEWS RIDE THE LOG STREAM, BECAUSE NOTHING ELSE CAN CARRY THEM.
#
# Kaggle's kernel-output API returns NOTHING until a session ends -- see
# blendfleet/log_stream.py's module docstring, and the real 2026-08-15
# reading where fetch_one_output listed 0 files for a kernel that was
# mid-render. So an image of a finished frame cannot be fetched while the
# render runs; it has to be PUSHED, and stdout is the only channel out of
# a running kernel this app can read.
#
# Sizes are set here rather than in the generated source so the parser
# contract and the byte budget are stated in one place:
#
#   THUMB_WIDTH_PX  320  -> 320x180 for a 16:9 render. Measured on a
#                           1920x1080 test image: 4.3 kB at quality 45,
#                           4.7 kB at 60, 6.3 kB at 75. 60 is the middle
#                           of that, ~5 kB of JPEG -> ~6.7 kB of base64,
#                           so a 100-frame render adds well under a
#                           megabyte of log text in total.
#   THUMB_CHUNK_CHARS 3000 -> ~3 lines for a typical frame. A single log
#                           line carrying the whole payload is the risk
#                           this splits: Kaggle's log capture is not
#                           documented to carry tens of kilobytes on one
#                           line, and a silently truncated line would
#                           decode to a corrupt image. Each line carries
#                           part=i/n and the total decoded byte count, so
#                           a missing or clipped part is DETECTED and the
#                           whole set discarded (log_stream.
#                           ThumbnailAssembler) rather than shown.
#   THUMB_MAX_BYTES 32768 -> the ceiling one frame may spend. Past it the
#                           preview is dropped and says so, so a
#                           pathological frame cannot bury the PROGRESS
#                           lines the progress bar depends on.
#
# These are DEFAULTS the running kernel can override through BR_THUMB_*,
# and previews as a whole are switched by BR_THUMBS -- a user on a slow
# link, watching over a phone tether, pays for every one of these bytes.
THUMB_WIDTH_PX = 320
THUMB_QUALITY = 60
THUMB_CHUNK_CHARS = 3000
THUMB_MAX_BYTES = 32768

# The numbers above are what the notebook PUTS IN THE ENVIRONMENT (see
# the BR_THUMB_* entries in the render and worker cells). SETUP_SCRIPT is
# a plain string, not an f-string -- it is full of f-strings of its own --
# so the same values appear once more as its env fallbacks, and
# test_notebook_builder.py asserts the two agree rather than trusting
# them to.
SETUP_SCRIPT = '''
import base64, io, os, sys, time, traceback, bpy

# Set by the notebook immediately before launching Blender, so the gap
# between it and now is exactly what the old per-frame loop was paying
# over and over: process start, addon registration, and the .blend parse.
_t_launch = float(os.environ.get("BR_T0") or 0)
_t_ready = time.time()

s = bpy.context.scene
s.render.resolution_x = int(os.environ["BR_RES_X"])
s.render.resolution_y = int(os.environ["BR_RES_Y"])
s.render.resolution_percentage = 100
s.render.image_settings.file_format = os.environ["BR_FORMAT"]
s.render.engine = "CYCLES"
s.cycles.samples = int(os.environ["BR_SAMPLES"])

# Keeps BVH and geometry resident BETWEEN frames -- which only means
# anything now that consecutive frames share a process. Costs RAM/VRAM
# (measured headroom: the 66 MB test scene used 2.7 GB of each T4's 15 GB),
# so it is a flag rather than a certainty.
if os.environ.get("BR_PERSISTENT", "1") == "1":
    try:
        s.render.use_persistent_data = True
    except AttributeError:
        pass        # older Blender: not fatal, just slower

prefs = bpy.context.preferences.addons["cycles"].preferences
chosen = None
# OPTIX first, deliberately: it uses the RT cores CUDA leaves idle on the
# Turing-class T4s Kaggle allocates. Falls back to CUDA when the driver or
# build has no OptiX, which is why this is a loop and not an assignment.
for backend in ("OPTIX", "CUDA"):
    try:
        prefs.compute_device_type = backend
        prefs.refresh_devices()
        if any(d.type == backend for d in prefs.devices):
            chosen = backend
            break
    except TypeError:
        continue
if chosen:
    # Enable ONLY devices of the chosen backend. prefs.devices lists each
    # physical GPU once per backend, so a looser filter switches the same
    # card on twice (observed on a Kaggle P100, 2026-07-30).
    for d in prefs.devices:
        d.use = (d.type == chosen)
    s.cycles.device = "GPU"
    enabled = [d.name for d in prefs.devices if d.use]
    # Printed with an explicit count: "which backend" and "how many cards"
    # are the two questions a slow render raises, and both were previously
    # invisible -- the notebook captured Blender's stdout and threw it away
    # unless the process failed, so nobody could tell OptiX from CUDA, or
    # two GPUs from one.
    print(f"[setup] backend={chosen} gpus={len(enabled)} devices={enabled}",
          flush=True)
else:
    s.cycles.device = "CPU"
    print("[setup] *** NO GPU BACKEND -> CPU (very slow) ***", flush=True)

if _t_launch:
    print(f"[setup] startup+scene load {_t_ready - _t_launch:.1f}s", flush=True)

# THE PER-FRAME WORK THAT WAS NOT ON THE GPU.
#
# Path tracing runs on both T4s, and then Cycles denoises and composites
# the frame on the CPU -- of which a Kaggle session has four cores.
# Measured 2026-08-12 on the scene this app was tested with:
# denoiser=OPENIMAGEDENOISE, denoising_use_gpu=False, compositor nodes
# enabled, and both GPUs idle for ~77% of every 23s frame.
#
# OIDN on the GPU is the SAME denoiser with the same weights, so the
# output image does not change -- only which processor does the work.
# Both assignments are guarded: these attribute names have moved between
# Blender versions, and a missing one must cost a slower render, never
# the render itself.
if os.environ.get("BR_POST_GPU", "1") == "1":
    _moved = []
    if getattr(s.cycles, "use_denoising", False):
        try:
            s.cycles.denoising_use_gpu = True
            _moved.append(f"denoise({s.cycles.denoiser})")
        except (AttributeError, TypeError) as e:
            print(f"[setup] could not move denoising to the GPU: {e}",
                  flush=True)
    try:
        s.render.compositor_device = "GPU"
        _moved.append("compositor")
    except (AttributeError, TypeError):
        pass        # no compositor device choice in this Blender
    print(f"[setup] on GPU: {', '.join(_moved) or 'nothing to move'}",
          flush=True)
else:
    print("[setup] post-processing left on the CPU (BR_POST_GPU=0)",
          flush=True)

# LIVE FRAME PREVIEWS.
#
# Guarded exactly as the telemetry cell guards psutil: Pillow is on every
# Kaggle image today, but an import that can raise must cost the preview
# and NEVER the render. A missing Pillow leaves THUMBS_ON False and the
# render proceeds untouched.
THUMBS_ON = os.environ.get("BR_THUMBS", "1") == "1"
THUMB_W = int(os.environ.get("BR_THUMB_W") or 320)
THUMB_Q = int(os.environ.get("BR_THUMB_Q") or 60)
THUMB_CHUNK = int(os.environ.get("BR_THUMB_CHUNK") or 3000)
THUMB_MAX = int(os.environ.get("BR_THUMB_MAX") or 32768)
_thumb_timed = [False]
try:
    from PIL import Image as _Img
except Exception as _thumb_err:
    _Img = None
    if THUMBS_ON:
        print("[setup] live frame previews off: Pillow could not be "
              f"imported here ({type(_thumb_err).__name__}: {_thumb_err}). "
              "The render is unaffected -- frames are still written and "
              "collected as normal, there is just nothing to look at until "
              "the session ends.", flush=True)
if THUMBS_ON and _Img is not None:
    print(f"[setup] live frame previews on: one {THUMB_W}px JPEG per "
          "finished frame, sent down this log. Set BR_THUMBS=0 to turn "
          "them off if the extra log traffic is unwelcome.", flush=True)
elif not THUMBS_ON:
    print("[setup] live frame previews off (BR_THUMBS=0). Frames will only "
          "be viewable once the session ends and its output is released.",
          flush=True)


def _rendered_path(prefix):
    """Where write_still actually put the frame, or None.

    write_still appends the format's own extension to render.filepath, and
    that mapping (PNG -> .png, OPEN_EXR -> .exr, ...) lives in Blender, not
    here -- so it is ASKED for, and only guessed at by matching the prefix
    if this Blender has no file_extension property.
    """
    ext = getattr(s.render, "file_extension", "") or ""
    if ext and os.path.exists(prefix + ext):
        return prefix + ext
    import glob
    hits = sorted(glob.glob(prefix + ".*"))
    return hits[0] if hits else None


def _emit_thumb(frame, prefix):
    """Print one finished frame as chunked base64 JPEG on stdout.

    CHUNKED, with part=i/n and the total decoded byte count on every line,
    because this payload has to survive Kaggle's log capture and nothing
    documents how long a single captured line may be. A truncated or
    dropped part is therefore detectable by the reader
    (log_stream.ThumbnailAssembler), which discards the whole set -- half a
    JPEG must never be shown as though it were the frame.

    Every failure path here is swallowed and NARRATED, never raised: this
    runs inside the render loop, and a preview is worth nothing next to
    the frame it previews.
    """
    if not (THUMBS_ON and _Img is not None):
        return
    # _t0, not t0: the render loop below has its own t0 holding the frame's
    # start time, and a name collision there would be measured and printed
    # as the frame's own duration.
    _t0 = time.time()
    try:
        path = _rendered_path(prefix)
        if path is None:
            print(f"[setup] frame {frame} preview skipped: the rendered "
                  f"file was not found at {prefix}.* -- the frame itself "
                  "may still have been written elsewhere.", flush=True)
            return
        im = _Img.open(path)
        # A no-op for PNG; for a JPEG render it decodes straight to a
        # reduced size, which is most of this function's cost.
        try:
            im.draft("RGB", (THUMB_W, THUMB_W))
        except Exception:
            pass
        im = im.convert("RGB")      # JPEG cannot carry PNG's alpha channel
        im.thumbnail((THUMB_W, THUMB_W), _Img.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=THUMB_Q, optimize=True)
        raw = buf.getvalue()
        if len(raw) > THUMB_MAX:
            # One retry at a quality nobody would mistake for the render,
            # rather than either dropping a busy frame or letting it
            # flood the log the PROGRESS lines share.
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=30, optimize=True)
            raw = buf.getvalue()
        if len(raw) > THUMB_MAX:
            print(f"[setup] frame {frame} preview skipped: {len(raw)} bytes "
                  f"is over the {THUMB_MAX}-byte budget one preview may "
                  "spend on this log. The frame rendered fine.", flush=True)
            return
        b64 = base64.b64encode(raw).decode("ascii")
        parts = [b64[i:i + THUMB_CHUNK]
                 for i in range(0, len(b64), THUMB_CHUNK)] or [""]
        # EXACT format parsed by log_stream.THUMB_RE.
        for i, part in enumerate(parts, 1):
            print(f"THUMB frame={frame} part={i}/{len(parts)} "
                  f"bytes={len(raw)} {part}", flush=True)
        if not _thumb_timed[0]:
            _thumb_timed[0] = True
            print(f"[setup] first frame preview cost {time.time()-_t0:.2f}s "
                  f"and {len(raw)} bytes in {len(parts)} log line(s); every "
                  "later frame costs about the same.", flush=True)
    except Exception as e:
        print(f"[setup] frame {frame} preview failed, render unaffected: "
              f"{type(e).__name__}: {e}", flush=True)

FRAMES = [int(x) for x in os.environ["BR_FRAMES"].split(",") if x.strip()]
OUT = os.environ["BR_OUTPUT"]
done, failed = [], []
for frame in FRAMES:
    t0 = time.time()
    s.frame_set(frame)
    # -f used to append the frame number for us. write_still does not: it
    # writes scene.render.filepath verbatim (plus the format's extension),
    # so the number has to be part of the path or every frame would
    # overwrite the last one.
    s.render.filepath = f"{OUT}{frame:04d}"
    try:
        bpy.ops.render.render(write_still=True)
        ok = True
    except Exception:
        ok = False
        print(f"[frame {frame}] FAILED", flush=True)
        traceback.print_exc()
        sys.stdout.flush()
    # Only for a frame that actually rendered, and BEFORE its PROGRESS
    # line: a preview for a failed frame would be a picture of whatever
    # the last run left on disk, presented as this frame.
    if ok:
        _emit_thumb(frame, s.render.filepath)
    (done if ok else failed).append(frame)
    # EXACT format parsed by log_stream.PROGRESS_RE -- frame=, ok=, secs=,
    # done=N/M. Echoed verbatim by the notebook, so the app sees the same
    # lines it always has.
    print(f"PROGRESS frame={frame} ok={ok} secs={time.time()-t0:.1f} "
          f"done={len(done)}/{len(FRAMES)}", flush=True)
print(f"[setup] batch finished done={sorted(done)} failed={sorted(failed)}",
      flush=True)
'''


# Confirmed live (docs/machine-shape-findings.md, 2026-08-10) against the
# app's own KaggleClient.push_kernel: "NvidiaTeslaT4" is the ONLY string of
# the three valid `machine_shape` values that yields 2 GPUs (2x Tesla T4,
# 15360 MiB each) -- "NvidiaTeslaP100" and omitting the field both yield a
# single P100. kagglesdk ships no enum for this (see the findings doc,
# section 1); the only source is a docstring, so a typo here has nothing
# to catch it at import time. Kaggle's own kernels_push accepts an INVALID
# machine_shape with NO error and silently falls back to a single P100 --
# indistinguishable from success in the push response -- so this constant
# is pinned by an exact-string test (test_notebook_builder.py) rather than
# trusted to eyeball review.
MACHINE_SHAPE = "NvidiaTeslaT4"

# Extension of the Task 5 per-worker output archive -- single source of
# truth for both where it's written (below, into the generated notebook's
# ARCHIVE literal) and where it's later recognised (collector.py's
# _resolve_frame_sources, kaggle_client.py's _OUTPUT_SUFFIXES). Used to be
# defined separately in each of those three places; a future format change
# updated in only one or two of them would have silently stopped the third
# from seeing the archive at all.
ARCHIVE_SUFFIX = ".zip"


@dataclass
class RenderSettings:
    resolution_x: int
    resolution_y: int
    samples: int
    file_format: str = "PNG"
    blender_version: str = DEFAULT_VERSION
    # 0 = no minimum-hardware gate (today's behaviour). Kaggle's GPU
    # allocation is not guaranteed even with a valid machine_shape request
    # (see docs/machine-shape-findings.md) -- a caller that genuinely needs
    # >=N GPUs sets this so the generated notebook's PREFLIGHT gate stops
    # the session before Blender is downloaded, rather than discovering the
    # shortfall only from a slow render.
    min_gpus: int = 0
    # WHICH MACHINE TO ASK KAGGLE FOR, and for how long, and on which base
    # image. Session-level rather than render-level -- none of the three
    # appears inside the notebook -- but they belong to the same decision a
    # caller makes when it starts a render, and carrying them here means
    # every push path picks them up without a second argument threaded
    # through Fleet. See design.MACHINE_SHAPES for why the string is exact,
    # and KaggleClient.push_kernel for what the timeout does.
    machine_shape: str = MACHINE_SHAPE
    session_timeout_seconds: int = 0
    docker_image: str = ""
    # Cycles does per-frame POST-processing on the CPU by default -- the
    # denoiser and the compositor both. Measured on a real session
    # (2026-08-12): the scene ships denoiser=OPENIMAGEDENOISE with
    # denoising_use_gpu=False, and a Kaggle box has FOUR cpu cores next to
    # two Tesla T4s. Both T4s read idle for ~77% of a 23s frame, which is
    # what that buys.
    #
    # True moves both onto the GPU. Measured on the same 15 frames, same
    # scene, same 1920x1080 / 64 samples, 2x T4:
    #
    #                     total    per frame   GPU duty cycle
    #   CPU post         360.3s      22.9s        23%
    #   GPU post         115.8s       6.7s      79-88%
    #
    # 3.1x, and the image is the same image: max difference 1/255 on any
    # channel, no pixel differing by more than 2 -- the same magnitude as
    # two frames of this static scene from a single run, i.e. inside
    # Cycles' own sampling noise. Same OIDN, same weights, different
    # processor.
    #
    # A flag rather than unconditional because it costs VRAM (the measured
    # scene used 2.7 GB of each card's 15 GB), and a far larger scene
    # could want it off.
    post_on_gpu: bool = True
    # A small JPEG of every finished frame, pushed down the log stream so
    # a render can be watched WHILE it runs. Kaggle releases a session's
    # output only once that session ends, so without this there is nothing
    # to look at until the whole render is over -- which is the entire
    # reason it exists.
    #
    # On by default because "what is it doing right now" is the question
    # this app is for, and the cost is small (about 5 kB per frame, under
    # a megabyte across a 100-frame render). Off is a real choice, not a
    # hedge: the bytes travel over the same connection everything else
    # does, and someone watching a render over a phone tether pays for
    # them. The generated notebook also honours BR_THUMBS at runtime, so
    # this can be overridden without rebuilding.
    live_previews: bool = True


# WHAT MAKES A NOTEBOOK ON KAGGLE THIS APP'S, AND WHOSE VERSION.
#
# The app reads a render's progress by parsing the notebook's own stdout --
# `PROGRESS frame=... ok=... secs=...`, the hardware banner, TELEMETRY. That
# is a contract between two programs, and the one on Kaggle can change
# without this one knowing: somebody opens the notebook in the browser and
# edits it, or an older BlendFleet pushed it and a newer one is reading it.
# Either way the render appears to run and report nothing, and the app
# currently has no way to say which.
#
# So every cell it generates carries this line, and `notebook_drift` below
# is what compares it. Bump CONTRACT whenever the log format the app parses
# changes -- that is what makes "built by a different version" detectable
# rather than merely suspected.
NOTEBOOK_CONTRACT = 1
NOTEBOOK_MARKER = f"BLENDFLEET-NOTEBOOK contract={NOTEBOOK_CONTRACT}"

_MARKER_RE = re.compile(r"BLENDFLEET-NOTEBOOK contract=(\d+)")


def _code(src: str) -> dict:
    # Marked here rather than in each template: this is the one place every
    # generated cell passes through, so a template added later cannot
    # forget it.
    src = f"# {NOTEBOOK_MARKER}\n" + src.strip("\n")
    return {"cell_type": "code", "execution_count": None, "metadata": {},
            "outputs": [], "source": src.splitlines(keepends=True)}


def notebook_drift(source: str) -> str:
    """Why the notebook on Kaggle cannot be trusted to report, or "".

    Given the source Kaggle holds for a kernel, answers in one sentence
    fit to show a user. Every answer is about what this app can no longer
    rely on -- never a guess about who changed it or why.

    Deliberately narrow: an edit that leaves the marker and the PROGRESS
    line intact is NOT reported, because the app can still read such a
    notebook, and warning about edits that do not matter would teach the
    user to ignore the warning that does.
    """
    if not source:
        # Absence of evidence. Kaggle did not hand over a source, which is
        # not the same as a notebook that has been changed.
        return ""
    found = _MARKER_RE.search(source)
    if found is None:
        return ("The notebook on Kaggle is not one BlendFleet built — it "
                "looks edited, so its progress lines cannot be relied on.")
    if int(found.group(1)) != NOTEBOOK_CONTRACT:
        return (f"The notebook on Kaggle was built by a different version "
                f"of BlendFleet (contract {found.group(1)}, this app reads "
                f"{NOTEBOOK_CONTRACT}) — what it reports may not match what "
                "this app expects.")
    if "PROGRESS frame=" not in source:
        return ("The notebook on Kaggle no longer prints the progress lines "
                "this app reads — frame counts from it cannot be trusted.")
    return ""


# ---------------------------------------------------------------------------
# WARM WORKERS, AND THE ONE THING THAT MAKES THEM AWKWARD.
#
# A pushed Kaggle kernel is a BATCH job: it runs every cell top to bottom
# and the session ends when the last one does. Nothing can talk to a
# running kernel -- there is no API for "send this session a command". So a
# machine that comes up, waits, and renders when told has to find out about
# the job by ASKING, not by being told: it polls.
#
# What it polls is a "job file" the app publishes as a new version of a
# small control dataset. Reading that from inside the kernel needs the
# Kaggle API, which needs a token -- and that is the awkward part, so it is
# stated plainly rather than buried:
#
#   The token embedded in a worker notebook is THAT SAME ACCOUNT'S OWN
#   token. Nobody's credentials travel to anybody else's machine: the
#   kernel pushed to a friend's account carries only the friend's token,
#   running on the friend's own session. The kernel is private
#   (is_private=True below), and the token is read from a variable that is
#   never printed or written to /kaggle/working.
#
# It is still a token sitting in a notebook, and a kernel accidentally made
# public would expose it. Warm mode is therefore opt-in per launch, never
# the default, and the app tells the user what it is doing.
#
# THE OTHER COST, which is not technical: a session bills GPU quota by
# wall-clock, not by compute. A machine waiting for work spends a friend's
# 30h/week at exactly the same rate as one rendering. IDLE_TIMEOUT_S is
# what bounds that, and the worker shuts ITSELF down -- not the app -- so
# an app that crashes or a laptop that sleeps cannot strand a session
# quietly eating quota.
# ---------------------------------------------------------------------------
IDLE_TIMEOUT_S = 600            # 10 minutes, the user's own figure
JOB_POLL_SECONDS = 10
# Kaggle caps a session well before this, but a worker that has been up for
# eleven hours has stopped being "warm" and started being a leak.
MAX_WORKER_LIFETIME_S = 10 * 3600


# WHAT HARDWARE DID THIS SESSION ACTUALLY GET?
#
# Shared verbatim between a render's first cell and the standalone
# hardware probe, because these exact strings are a contract with
# log_stream.parse_preflight / parse_hardware_banner. A probe that
# re-implemented the format would drift from the parser the day either
# one changed, and the failure would be silent: the app would simply
# report no hardware.
#
# Why a probe exists at all: Kaggle's allocation is a lottery. The same
# account asked for the same machine_shape minutes apart got 2x T4 once
# and no GPU at all the next time (docs/machine-shape-findings.md, and
# again on 2026-08-12 when two sessions were cancelled and a third
# queued). Checking costs about a minute; discovering it from a render
# costs the render.
HARDWARE_REPORT = '''
cpu_count = psutil.cpu_count(logical=True)
ram_total = psutil.virtual_memory().total / 2**30
gpu_listing = subprocess.run(
    "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader",
    shell=True, capture_output=True, text=True).stdout.strip()
gpu_names = [row.split(",", 1)[0].strip()
            for row in gpu_listing.splitlines() if row.strip()]

# PREFLIGHT: the kernel is starting regardless, so report the REAL
# hardware in the first seconds -- before the next cell downloads
# Blender, let alone before the .blend is touched -- rather than only
# finding out from a slow render or a wasted whole session. One line,
# not one-per-GPU like the CPU/RAM + nvidia-smi lines below: this is the
# single fact the desktop app needs to decide "keep going or stop" the
# moment the kernel starts. Flushing stdout immediately is mandatory
# here: without it, nothing reaches the live log stream until the kernel
# exits (same reason PROGRESS/TELEMETRY flush explicitly further down).
print(f"PREFLIGHT gpus={len(gpu_names)} "
      f"gpu_names={'|'.join(gpu_names) if gpu_names else 'none'} "
      f"cpu={cpu_count} ram={ram_total:.1f}", flush=True)

print(f"CPU {cpu_count} cores | RAM {ram_total:.1f} GB", flush=True)
print(gpu_listing, flush=True)
'''


def build_probe(out_dir: Path, kernel_slug: str) -> Path:
    """A notebook that reports its hardware and stops. Nothing else.

    No dataset, no Blender, no .blend -- so it starts in seconds, costs
    about a minute of quota, and cannot fail for any reason except the one
    being tested. That matters: the render path's first cell walks
    /kaggle/input for a .blend and ASSERTS it found one, which a probe
    with no dataset attached would trip on immediately.

    It also checks DNS, because the two failures travel together: a
    session that gets no GPU is usually the same session that gets no
    outbound network (measured 2026-08-11 -- "Temporary failure in name
    resolution", which is what made every early render die downloading
    Blender). Knowing both from one probe is what tells the user whether
    to retry or to attach the Blender dataset.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cell = f'''
import socket, subprocess, psutil
{HARDWARE_REPORT}
try:
    socket.getaddrinfo("download.blender.org", 443)
    print("PROBE_DNS ok", flush=True)
except Exception as e:
    print(f"PROBE_DNS failed {{type(e).__name__}}: {{e}}", flush=True)
print("PROBE_DONE", flush=True)
'''
    nb = {"cells": [_code(cell)],
          "metadata": {"kernelspec": {"display_name": "Python 3",
                                      "language": "python",
                                      "name": "python3"},
                       "language_info": {"name": "python"}},
          "nbformat": 4, "nbformat_minor": 5}
    (out_dir / "render.ipynb").write_text(json.dumps(nb, indent=1),
                                          encoding="utf-8")
    (out_dir / "kernel-metadata.json").write_text(json.dumps({
        "id": kernel_slug,
        "title": kernel_slug.split("/", 1)[1].replace("-", " "),
        "code_file": "render.ipynb",
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True,
        # The whole point is to learn what THIS shape actually yields, so
        # it must ask for exactly what a render asks for. A probe that
        # requested something different would answer a question nobody
        # asked.
        "machine_shape": MACHINE_SHAPE,
        # Hold the base image this kernel was created with, instead of
        # taking whatever is newest each time it runs. Kaggle rolls its
        # images without asking, and the CUDA driver in one is what
        # Blender's GPU rendering actually runs against -- a kernel that
        # rendered fine and then behaves differently on a rerun, with the
        # same notebook and the same scene, is a day lost to looking in the
        # wrong place.
        #
        # WHAT IT DOES NOT BUY, so nobody expects it to: this pins a kernel
        # to ITS OWN original image, and a render creates a new kernel per
        # job, so two jobs started a month apart can still get different
        # images.
        #
        # PINNING ACROSS JOBS IS POSSIBLE, and deliberately not done here
        # yet. `ApiSaveKernelRequest` takes an explicit `docker_image`
        # (a `gcr.io/kaggle-images/python@sha256:...` digest) and
        # kernels_push reads it straight out of this file -- so naming one
        # digest would hold every render on it for as long as Kaggle keeps
        # that image. What is missing is a way for a USER to choose and
        # update it: hardcoding a digest here would silently rot the day
        # Kaggle retires it, with no UI to say why a render stopped
        # starting. Worth doing with a setting, not with a constant.
        "docker_image_pinning_type": "original",
        "dataset_sources": [],
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }, indent=2), encoding="utf-8")
    return out_dir / "render.ipynb"


def build(frames: list[int], settings: RenderSettings, dataset_slug: str,
          out_dir: Path, kernel_slug: str, *, mode: str = "render",
          control_slug: str | None = None, token: str | None = None,
          worker_label: str | None = None,
          blender_slug: str | None = None) -> Path:
    """Write the notebook and its kernel-metadata.json.

    `mode="render"` is the one-shot job: render `frames`, then the session
    ends. `mode="worker"` is a warm machine: identical setup, then a wait
    loop that polls `control_slug` for work and shuts itself down after
    IDLE_TIMEOUT_S with nothing to do.

    Warm mode needs `token` (that account's OWN token -- see the note
    above), `control_slug` and `worker_label`; it refuses rather than
    producing a notebook that would come up and wait forever for a job it
    has no way to hear about.
    """
    # Validated AND NORMALISED here, not in the notebook: a bad version
    # otherwise 404s inside a running Kaggle session, costing that
    # session's startup to discover what is really a typo. The return
    # value is assigned back rather than discarded: c1 embeds
    # settings.blender_version verbatim (BLENDER_VERSION = ...repr...) and
    # c2's fallback URL is built from the same value via download_url() --
    # if those two ever read something different (e.g. an un-stripped
    # " 4.2.9 "), the download URL and the extracted tarball name would
    # disagree, and BLENDER_VERSION would land in the notebook un-stripped.
    settings.blender_version = validate_version(settings.blender_version)
    if mode not in ("render", "worker"):
        raise ValueError(f"unknown notebook mode {mode!r}")
    # Kaggle mounts a dataset at /kaggle/input/<name>, without the owner
    # prefix, so the notebook needs the name and the metadata needs the
    # full slug.
    blender_dataset_name = (blender_slug.split("/", 1)[-1]
                            if blender_slug else "")
    if mode == "worker" and not (control_slug and token and worker_label):
        raise ValueError(
            "worker mode needs control_slug, token and worker_label -- "
            "without all three the machine would start, spend quota and "
            "never be able to learn about a job")
    out_dir.mkdir(parents=True, exist_ok=True)

    c1 = f'''
import os, subprocess, psutil
FRAMES = {frames!r}
RES_X, RES_Y = {settings.resolution_x}, {settings.resolution_y}
SAMPLES, FMT = {settings.samples}, {settings.file_format!r}
BLENDER_VERSION = {settings.blender_version!r}
MIN_GPUS = {settings.min_gpus!r}
POST_GPU = {settings.post_on_gpu!r}
LIVE_PREVIEWS = {settings.live_previews!r}

{HARDWARE_REPORT}
if len(gpu_names) < MIN_GPUS:
    # Minimum-hardware gate: fail loudly and stop HERE, before Blender is
    # downloaded or the .blend is even walked for -- a wrong machine costs
    # seconds, not the whole session's quota.
    print(f"PREFLIGHT_FAIL requires >={{MIN_GPUS}} GPU(s), got "
          f"{{len(gpu_names)}}: {{gpu_names}}", flush=True)
    raise SystemExit(
        f"minimum hardware not met: requires >={{MIN_GPUS}} GPU(s), got "
        f"{{len(gpu_names)}} ({{gpu_names}})")

# Datasets mount at /kaggle/input/datasets/<owner>/<slug>/<file>, NOT
# /kaggle/input/<slug>/. Walk instead of assuming -- a hardcoded path
# failed a real run on 2026-07-30.
BLEND = None
for root, _dirs, files in os.walk("/kaggle/input"):
    for f in files:
        if f.endswith(".blend"):
            BLEND = os.path.join(root, f)
print("BLEND =", BLEND)
assert BLEND, "no .blend found under /kaggle/input"
print("FRAMES =", FRAMES)
'''

    # Blender comes from an ATTACHED DATASET, not from the internet.
    #
    # A Kaggle session has no outbound network unless the account is
    # phone-verified -- enable_internet is accepted and then silently
    # ignored. Measured on a real session: DNS does not resolve at all
    # ("Temporary failure in name resolution"), so the old `wget` of the
    # release tarball failed every single time, ~41s in, and every render
    # this app ever started died in this cell without producing a frame.
    #
    # An attached dataset needs no network, works on an unverified account
    # -- which matters when the fleet is friends' accounts, since otherwise
    # every one of them would have to phone-verify -- and starts in seconds
    # instead of minutes. The wget path is kept only as a fallback for a
    # caller that has no Blender dataset to attach.
    c2 = f'''
import os, glob, subprocess, time
V = BLENDER_VERSION
T = f"blender-{{V}}-linux-x64.tar.xz"
BBIN = f"/kaggle/tmp/blender-{{V}}-linux-x64/blender"
BLENDER_DATASET = {blender_dataset_name!r}
os.makedirs("/kaggle/tmp", exist_ok=True)

def sh(cmd):
    p = subprocess.run(cmd, shell=True, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(p.stdout.rstrip()[-1500:])
    return p.returncode

if not os.path.exists(BBIN):
    t0 = time.time()
    tarball = None
    if BLENDER_DATASET:
        # Searched across the WHOLE of /kaggle/input, with no assumption
        # about where a dataset lands. Measured: Kaggle mounts them at
        # /kaggle/input/datasets/<owner>/<name>/, not /kaggle/input/<name>
        # -- guessing the second cost a whole render, silently, by falling
        # through to a download that cannot work. The .blend in cell 1 is
        # found by walking /kaggle/input for the same reason.
        hits = sorted(glob.glob("/kaggle/input/**/blender-*.tar.xz",
                                recursive=True))
        if not hits:
            hits = sorted(glob.glob("/kaggle/input/**/*.tar.xz",
                                    recursive=True))
        if hits:
            tarball = hits[0]
            print("blender from dataset:", tarball, flush=True)
        else:
            print("BLENDER DATASET WAS ATTACHED BUT NO TARBALL WAS FOUND "
                  "anywhere under /kaggle/input:", flush=True)
            for entry in sorted(glob.glob("/kaggle/input/**", recursive=True))[:40]:
                print("   ", entry, flush=True)
    if tarball is None:
        # No dataset: fall back to downloading, which only works on a
        # phone-verified account. Says so, so a failure here is not a
        # mystery. The URL is baked in at BUILD time via
        # blender_versions.download_url, matching the same
        # major.minor/full-version layout T (above) builds at runtime from
        # V -- pinned in one place rather than reconstructed a second time
        # from a runtime string split.
        URL = {download_url(settings.blender_version)!r}
        print("no blender dataset attached -- downloading, which requires "
              "this Kaggle account to be phone-verified for internet access",
              flush=True)
        assert sh(f"wget -q -O /kaggle/tmp/{{T}} '{{URL}}'") == 0, (
            "blender download failed -- this session has no internet. "
            "Attach a Blender dataset, or phone-verify this Kaggle account.")
        tarball = f"/kaggle/tmp/{{T}}"
    assert sh(f"tar -xf '{{tarball}}' -C /kaggle/tmp") == 0, "extract failed"
    if tarball.startswith("/kaggle/tmp"):
        os.remove(tarball)
    print(f"blender ready in {{time.time()-t0:.0f}}s", flush=True)
sh(f"{{BBIN}} --version | head -2")
'''

    c3 = f'''
open("/kaggle/working/render_setup.py", "w").write({SETUP_SCRIPT!r})
print("wrote render_setup.py")
'''

    # Shared by the one-shot render cell and the warm worker, so the two
    # cannot drift apart in how frames are actually run -- the same reason
    # render_setup.py is shared. Progress numbers are re-emitted HERE
    # rather than echoed from Blender: a fallback child renders a single
    # frame and so believes it is "1/1", which would make the app's
    # progress bar jump to 100% on the first recovered frame.
    c_runner = '''
import collections, glob, os, subprocess, time, zipfile

def _archive_new(out_prefix, archive, archived):
    """Append newly-written frames to the archive, once each.

    Called after every frame rather than at the end: the archive is an
    optimisation for collect(), and a kernel killed mid-run should still
    have every frame it finished. The loose files in /kaggle/working
    remain the fallback, so a failure to archive is reported and ignored,
    never raised.
    """
    if not archive:
        return
    for path in sorted(glob.glob(out_prefix + "*")):
        name = os.path.basename(path)
        if name in archived:
            continue
        try:
            with zipfile.ZipFile(archive, "a", zipfile.ZIP_STORED) as zf:
                zf.write(path, arcname=name)
            archived.add(name)
        except Exception as e:
            print(f"ARCHIVE skipped {name}: {type(e).__name__}", flush=True)


def _run_blender(frames, blend, env, on_frame):
    """One Blender process rendering the whole of `frames`.

    Returns (returncode, tail). `on_frame(frame, ok, secs)` fires as each
    frame's PROGRESS line arrives, so the caller can archive and report
    without waiting for the process to exit.
    """
    env = dict(env)
    env["BR_FRAMES"] = ",".join(str(f) for f in frames)
    env["BR_T0"] = repr(time.time())
    p = subprocess.Popen(
        [BBIN, blend, "-b", "-noaudio", "-P",
         "/kaggle/working/render_setup.py"],
        env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, bufsize=1)
    tail = collections.deque(maxlen=80)
    for line in p.stdout:
        line = line.rstrip("\\n")
        tail.append(line)
        if line.startswith("PROGRESS frame="):
            fields = dict(kv.split("=", 1)
                          for kv in line.split()[1:] if "=" in kv)
            try:
                frame = int(fields.get("frame", ""))
            except ValueError:
                continue
            try:
                secs = float(fields.get("secs", "0"))
            except ValueError:
                secs = 0.0
            on_frame(frame, fields.get("ok") == "True", secs)
        elif line.startswith("THUMB frame="):
            # Forwarded VERBATIM, never re-formatted: these lines are a
            # byte-for-byte contract with log_stream.THUMB_RE, and each
            # one carries part=i/n plus the total byte count that lets the
            # reader tell a complete preview from a clipped one. Unlike
            # PROGRESS -- which is re-emitted here so a fallback child's
            # "1/1" cannot reach the app -- a preview means the same thing
            # whichever process produced it.
            print(line, flush=True)
        elif line.startswith("[setup]") or line.startswith("[frame "):
            print(line, flush=True)     # backend, device count, load time
    p.wait()
    return p.returncode, list(tail)


def render_frames(frames, blend, env, out_prefix, archive):
    """Render `frames` in ONE process, per-frame only for what it drops.

    A crash takes the whole batch down with it, not just the frame that
    caused it -- so anything the batch never reported is retried one
    process at a time. That is the old behaviour, reached only on failure:
    worst case this matches what it replaced, best case it skips a scene
    reload per frame.
    """
    done, failed, archived = [], [], set()
    total = len(frames)

    def on_frame(frame, ok, secs):
        (done if ok else failed).append(frame)
        _archive_new(out_prefix, archive, archived)
        print(f"PROGRESS frame={frame} ok={ok} secs={secs:.1f} "
              f"done={len(done)}/{total}", flush=True)

    t0 = time.time()
    rc, tail = _run_blender(frames, blend, env, on_frame)
    if rc != 0:
        print(f"BATCH exited rc={rc} after {time.time()-t0:.1f}s", flush=True)
        for line in tail[-25:]:
            print("   ", line, flush=True)

    seen = set(done) | set(failed)
    remaining = [f for f in frames if f not in seen]
    if remaining:
        print(f"FALLBACK one process per frame for {remaining}", flush=True)
        for frame in remaining:
            before = len(done) + len(failed)
            rc2, tail2 = _run_blender([frame], blend, env, on_frame)
            if len(done) + len(failed) == before:
                # Died without even reporting its own frame.
                failed.append(frame)
                print(f"PROGRESS frame={frame} ok=False secs=0.0 "
                      f"done={len(done)}/{total}", flush=True)
                for line in tail2[-25:]:
                    print("   ", line, flush=True)
    return sorted(set(done)), sorted(set(failed))

print("[runner] batched render helper ready")
'''

    c_telemetry = '''
import subprocess, threading
# Guarded, not a plain import: this cell must stay importable on any
# machine. Kaggle ships psutil (cell 1 already uses it), but a telemetry
# dependency that can raise at import time would take the whole render
# down for the sake of a memory reading -- the same reason the sampler
# runs on its own thread.
try:
    import psutil
except Exception:
    psutil = None

def _telemetry_loop(stop_event, interval=5):
    # Runs on a background thread so a telemetry hiccup can never abort a
    # render frame. Per-GPU lines only -- never aggregated, since Kaggle's
    # GPU allocation isn't guaranteed (a GPU request once returned a single
    # P100 instead of the expected T4 x2), so each card must be independently
    # visible.
    while not stop_event.is_set():
        # System RAM, sampled on the SAME tick as the GPUs. Only the
        # total was ever reported (once, in the hardware banner), so a
        # session could be minutes from being killed for memory and the
        # dashboard showed a reassuring "31.3 GB RAM" the whole way down.
        # Its own try/except: psutil failing must cost the RAM line, not
        # the GPU lines underneath it.
        try:
            if psutil is not None:
                vm = psutil.virtual_memory()
                print(f"SYSTEM ram_used={vm.used} ram_total={vm.total} "
                      f"cpu_pct={psutil.cpu_percent(interval=None):.0f}",
                      flush=True)
        except Exception:
            pass
        try:
            p = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=index,utilization.gpu,memory.used,memory.total,"
                 "temperature.gpu,power.draw",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            return  # CPU-only session: no nvidia-smi -- stop quietly
        except Exception:
            stop_event.wait(interval)
            continue
        if p.returncode == 0:
            for row in p.stdout.strip().splitlines():
                parts = [x.strip() for x in row.split(",")]
                if len(parts) != 6:
                    continue
                idx, util, mem_used, mem_total, temp, power = parts
                try:
                    power_val = f"{float(power):.0f}"
                except ValueError:
                    power_val = "NA"  # power.draw reported as "[N/A]"
                print(f"TELEMETRY gpu={idx} util={util} mem_used={mem_used} "
                      f"mem_total={mem_total} temp={temp} power={power_val}",
                      flush=True)
        stop_event.wait(interval)

_telemetry_stop = threading.Event()
_telemetry_thread = threading.Thread(target=_telemetry_loop,
                                     args=(_telemetry_stop,), daemon=True)
_telemetry_thread.start()
print("[telemetry] background GPU sampler started")
'''

    # Task 5: the whole point is ONE download instead of hundreds, not
    # squeezing extra bytes out of already-compressed PNG/JPEG -- hence
    # ZIP_STORED, never ZIP_DEFLATED. Named from THIS kernel's own slug so
    # collect() can tell one worker's archive apart from another's --
    # which needs the WHOLE slug, owner included: kernel_slug is
    # "<username>/<stem>-render-<job_id>", and username is the only part
    # that differs between workers in the same job (stem and job_id are
    # fleet-wide). Slicing that off with .split("/", 1)[1], as this used
    # to, left every worker's archive_name identical despite the comment
    # here claiming otherwise. "/" cannot appear in a filename, so it is
    # replaced rather than kept literally.
    archive_name = kernel_slug.replace("/", "-")

    c4 = f'''
import os, glob, time, shutil, subprocess, zipfile
WORK = "/kaggle/tmp/work"
os.makedirs(WORK, exist_ok=True)
blend = f"{{WORK}}/scene.blend"
shutil.copy(BLEND, blend)          # /kaggle/input is READ-ONLY
OUT = "/kaggle/working/frames"
os.makedirs(OUT, exist_ok=True)
# ONE archive, in ADDITION to the loose files in OUT -- never a
# replacement for them. If the kernel is killed before this is even
# opened, the loose files (uploaded to /kaggle/working exactly as
# before) are still there for collect() to fall back to.
ARCHIVE = "/kaggle/working/{archive_name}{ARCHIVE_SUFFIX}"

env = os.environ.copy()
env.update({{"BR_RES_X": str(RES_X), "BR_RES_Y": str(RES_Y),
            "BR_SAMPLES": str(SAMPLES), "BR_FORMAT": FMT,
            "BR_POST_GPU": "1" if POST_GPU else "0",
            # Live previews, and the byte budget one may spend. Read back
            # out of the kernel's OWN environment first, so a session can
            # be told to stop sending them without rebuilding the
            # notebook; LIVE_PREVIEWS is what the app asked for at build
            # time and is the default when nothing overrides it.
            "BR_THUMBS": os.environ.get(
                "BR_THUMBS", "1" if LIVE_PREVIEWS else "0"),
            "BR_THUMB_W": os.environ.get("BR_THUMB_W", "{THUMB_WIDTH_PX}"),
            "BR_THUMB_Q": os.environ.get("BR_THUMB_Q", "{THUMB_QUALITY}"),
            "BR_THUMB_CHUNK": os.environ.get(
                "BR_THUMB_CHUNK", "{THUMB_CHUNK_CHARS}"),
            "BR_THUMB_MAX": os.environ.get("BR_THUMB_MAX", "{THUMB_MAX_BYTES}"),
            "BR_OUTPUT": f"{{OUT}}/f_"}})

# ONE Blender process for every frame -- see SETUP_SCRIPT's header for the
# measurement that motivated it. PROGRESS lines still arrive one per
# frame, from render_frames.
_t_render = time.time()
done, failed = render_frames(FRAMES, blend, env, f"{{OUT}}/f_", ARCHIVE)
_elapsed = time.time() - _t_render
print(f"[batch] {{len(done)}}/{{len(FRAMES)}} frames in {{_elapsed:.1f}}s "
      f"({{_elapsed/max(len(done), 1):.1f}}s per rendered frame)", flush=True)

print("DONE", sorted(done), "FAILED", sorted(failed))
for f in sorted(glob.glob(f"{{OUT}}/*")):
    print(f"  {{os.path.basename(f)}} {{os.path.getsize(f)//1024}} KB")
_telemetry_stop.set()
'''

    # Warm mode swaps the render cell for a wait loop. Everything before it
    # -- preflight, Blender, render_setup.py, telemetry -- is IDENTICAL, so
    # a warm worker and a one-shot render cannot drift apart in setup: the
    # thing that renders the frames is the same render_setup.py either way.
    c_worker = f'''
import os, json, glob, shutil, subprocess, time, zipfile
IDLE_TIMEOUT_S = {IDLE_TIMEOUT_S}
POLL_S = {JOB_POLL_SECONDS}
MAX_LIFETIME_S = {MAX_WORKER_LIFETIME_S}
CONTROL = {control_slug!r}
WORKER_LABEL = {worker_label!r}
# This account's OWN token, for reading the control dataset only. Never
# printed, never written to /kaggle/working. See the note in
# blendfleet/notebook_builder.py.
os.environ["KAGGLE_API_TOKEN"] = {token!r}

WORK = "/kaggle/tmp/work"
CTL = "/kaggle/tmp/ctl"
os.makedirs(WORK, exist_ok=True)
os.makedirs(CTL, exist_ok=True)
OUT = "/kaggle/working/frames"
os.makedirs(OUT, exist_ok=True)

print("WORKER ready, waiting for a job", flush=True)

def fetch_job():
    """The newest job descriptor, or None. Never raises: a control dataset
    that is briefly unreachable must not kill a warm machine -- it just
    means there is no news this tick."""
    try:
        subprocess.run(
            ["kaggle", "datasets", "download", "-d", CONTROL,
             "-p", CTL, "--force", "--unzip"],
            check=True, capture_output=True, timeout=120)
        with open(os.path.join(CTL, "job.json")) as fh:
            return json.load(fh)
    except Exception as e:
        print("JOBPOLL unavailable:", type(e).__name__, flush=True)
        return None

started = time.time()
last_activity = time.time()
seen_job = None

while True:
    if time.time() - started > MAX_LIFETIME_S:
        print("WORKER max lifetime reached, shutting down", flush=True)
        break
    idle_for = time.time() - last_activity
    if idle_for > IDLE_TIMEOUT_S:
        print(f"WORKER idle {{int(idle_for)}}s, shutting down", flush=True)
        break

    job = fetch_job()
    # A job is ours if it names us, or names nobody (a fleet-wide job).
    mine = (job and job.get("id") != seen_job
            and WORKER_LABEL in (job.get("workers") or [WORKER_LABEL]))
    if not mine:
        print(f"WORKER idle {{int(idle_for)}}s", flush=True)
        time.sleep(POLL_S)
        continue

    seen_job = job["id"]
    frames = job.get("frames") or []
    print(f"JOB {{seen_job}} frames={{len(frames)}}", flush=True)
    blend = f"{{WORK}}/scene.blend"
    shutil.copy(BLEND, blend)          # /kaggle/input is READ-ONLY
    env = os.environ.copy()
    env.update({{"BR_RES_X": str(job.get("resX", RES_X)),
                "BR_RES_Y": str(job.get("resY", RES_Y)),
                "BR_SAMPLES": str(job.get("samples", SAMPLES)),
                "BR_FORMAT": job.get("format", FMT),
                "BR_POST_GPU": "1" if job.get("postGpu", POST_GPU) else "0",
                # Same switches as a one-shot render, for the same reason
                # the runner is shared: a warm worker that quietly stopped
                # sending previews would look like a render that had
                # stalled.
                "BR_THUMBS": os.environ.get(
                    "BR_THUMBS",
                    "1" if job.get("livePreviews", LIVE_PREVIEWS) else "0"),
                "BR_THUMB_W": os.environ.get("BR_THUMB_W", "{THUMB_WIDTH_PX}"),
                "BR_THUMB_Q": os.environ.get("BR_THUMB_Q", "{THUMB_QUALITY}"),
                "BR_THUMB_CHUNK": os.environ.get(
                    "BR_THUMB_CHUNK", "{THUMB_CHUNK_CHARS}"),
                "BR_THUMB_MAX": os.environ.get(
                    "BR_THUMB_MAX", "{THUMB_MAX_BYTES}"),
                "BR_OUTPUT": f"{{OUT}}/f_"}})
    # Same batched runner as a one-shot render -- and the same PROGRESS
    # format. The warm worker used to print "PROGRESS 3/9", which
    # log_stream.PROGRESS_RE (which requires frame=) never matched, so a
    # warm job's progress was invisible to the app.
    done, failed = render_frames(frames, blend, env, f"{{OUT}}/f_", None)
    print("DONE", sorted(done), "FAILED", sorted(failed), flush=True)
    # The clock restarts from the END of the work, not its start: a job
    # that took an hour must not count as an hour of idling.
    last_activity = time.time()

_telemetry_stop.set()
print("WORKER stopped", flush=True)
'''

    render_cell = c_worker if mode == "worker" else c4
    nb = {"cells": [_code(c1), _code(c2), _code(c3), _code(c_runner),
                    _code(c_telemetry), _code(render_cell)],
          "metadata": {"kernelspec": {"display_name": "Python 3",
                                      "language": "python", "name": "python3"},
                       "language_info": {"name": "python"}},
          "nbformat": 4, "nbformat_minor": 5}
    nb_path = out_dir / "render.ipynb"
    nb_path.write_text(json.dumps(nb, indent=1), encoding="utf-8")

    (out_dir / "kernel-metadata.json").write_text(json.dumps({
        "id": kernel_slug,
        "title": kernel_slug.split("/", 1)[1].replace("-", " "),
        "code_file": "render.ipynb",
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": True,
        # DEPRECATED per kagglesdk's own docstring, but kept alongside
        # machine_shape: a backend that has not adopted machine_shape yet
        # would otherwise silently fall back to a CPU-only session if this
        # were dropped (docs/machine-shape-findings.md, section 2).
        #
        # The caller's choice now, defaulting to the same MACHINE_SHAPE it
        # was hardcoded to: two T4s suit throughput, one P100 suits a scene
        # that needs its memory undivided.
        "machine_shape": settings.machine_shape or MACHINE_SHAPE,
        # An exact base image, when one has been named. Empty means "the
        # current one", and the pinning type below then holds THAT for this
        # kernel's life -- so an unset value is still pinned, just not
        # across jobs.
        **({"docker_image": settings.docker_image}
           if settings.docker_image else {}),
        # Hold the base image this kernel was created with, instead of
        # taking whatever is newest each time it runs. Kaggle rolls its
        # images without asking, and the CUDA driver in one is what
        # Blender's GPU rendering actually runs against -- a kernel that
        # rendered fine and then behaves differently on a rerun, with the
        # same notebook and the same scene, is a day lost to looking in the
        # wrong place.
        #
        # WHAT IT DOES NOT BUY, so nobody expects it to: this pins a kernel
        # to ITS OWN original image, and a render creates a new kernel per
        # job, so two jobs started a month apart can still get different
        # images.
        #
        # PINNING ACROSS JOBS IS POSSIBLE, and deliberately not done here
        # yet. `ApiSaveKernelRequest` takes an explicit `docker_image`
        # (a `gcr.io/kaggle-images/python@sha256:...` digest) and
        # kernels_push reads it straight out of this file -- so naming one
        # digest would hold every render on it for as long as Kaggle keeps
        # that image. What is missing is a way for a USER to choose and
        # update it: hardcoding a digest here would silently rot the day
        # Kaggle retires it, with no UI to say why a render stopped
        # starting. Worth doing with a setting, not with a constant.
        "docker_image_pinning_type": "original",
        "enable_internet": True,
        # The scene, plus Blender itself when it is being shipped as a
        # dataset rather than downloaded. The CONTROL dataset is
        # deliberately NOT attached: an attached dataset is pinned at
        # session start, so a worker would never see a new version of it --
        # which is the entire mechanism. That one is fetched over the API.
        "dataset_sources": ([dataset_slug, blender_slug] if blender_slug
                            else [dataset_slug]),
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }, indent=2), encoding="utf-8")
    return nb_path
