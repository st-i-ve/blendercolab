from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

SETUP_SCRIPT = '''
import os, bpy
s = bpy.context.scene
s.render.resolution_x = int(os.environ["BR_RES_X"])
s.render.resolution_y = int(os.environ["BR_RES_Y"])
s.render.resolution_percentage = 100
s.render.image_settings.file_format = os.environ["BR_FORMAT"]
s.render.filepath = os.environ["BR_OUTPUT"]
s.render.engine = "CYCLES"
s.cycles.samples = int(os.environ["BR_SAMPLES"])
prefs = bpy.context.preferences.addons["cycles"].preferences
chosen = None
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
    print("[setup] " + chosen + " -> " +
          str([d.name for d in prefs.devices if d.use]))
else:
    s.cycles.device = "CPU"
    print("[setup] *** NO GPU BACKEND -> CPU (very slow) ***")
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
    blender_version: str = "5.2.0"
    # 0 = no minimum-hardware gate (today's behaviour). Kaggle's GPU
    # allocation is not guaranteed even with a valid machine_shape request
    # (see docs/machine-shape-findings.md) -- a caller that genuinely needs
    # >=N GPUs sets this so the generated notebook's PREFLIGHT gate stops
    # the session before Blender is downloaded, rather than discovering the
    # shortfall only from a slow render.
    min_gpus: int = 0


def _code(src: str) -> dict:
    return {"cell_type": "code", "execution_count": None, "metadata": {},
            "outputs": [], "source": src.strip("\n").splitlines(keepends=True)}


def build(frames: list[int], settings: RenderSettings, dataset_slug: str,
          out_dir: Path, kernel_slug: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    c1 = f'''
import os, subprocess, psutil
FRAMES = {frames!r}
RES_X, RES_Y = {settings.resolution_x}, {settings.resolution_y}
SAMPLES, FMT = {settings.samples}, {settings.file_format!r}
BLENDER_VERSION = {settings.blender_version!r}
MIN_GPUS = {settings.min_gpus!r}

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
print(f"PREFLIGHT gpus={{len(gpu_names)}} "
      f"gpu_names={{'|'.join(gpu_names) if gpu_names else 'none'}} "
      f"cpu={{cpu_count}} ram={{ram_total:.1f}}", flush=True)

if len(gpu_names) < MIN_GPUS:
    # Minimum-hardware gate: fail loudly and stop HERE, before Blender is
    # downloaded or the .blend is even walked for -- a wrong machine costs
    # seconds, not the whole session's quota.
    print(f"PREFLIGHT_FAIL requires >={{MIN_GPUS}} GPU(s), got "
          f"{{len(gpu_names)}}: {{gpu_names}}", flush=True)
    raise SystemExit(
        f"minimum hardware not met: requires >={{MIN_GPUS}} GPU(s), got "
        f"{{len(gpu_names)}} ({{gpu_names}})")

print(f"CPU {{cpu_count}} cores | RAM {{ram_total:.1f}} GB")
print(gpu_listing)

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

    c2 = '''
import os, subprocess, time
V = BLENDER_VERSION
S = ".".join(V.split(".")[:2])
T = f"blender-{V}-linux-x64.tar.xz"
URL = f"https://download.blender.org/release/Blender{S}/{T}"
BBIN = f"/kaggle/tmp/blender-{V}-linux-x64/blender"
os.makedirs("/kaggle/tmp", exist_ok=True)

def sh(cmd):
    p = subprocess.run(cmd, shell=True, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(p.stdout.rstrip()[-1500:])
    return p.returncode

if not os.path.exists(BBIN):
    t0 = time.time()
    assert sh(f"wget -q -O /kaggle/tmp/{T} '{URL}'") == 0, "blender download failed"
    assert sh(f"tar -xf /kaggle/tmp/{T} -C /kaggle/tmp") == 0, "extract failed"
    os.remove(f"/kaggle/tmp/{T}")
    print(f"blender ready in {time.time()-t0:.0f}s")
sh(f"{BBIN} --version | head -2")
'''

    c3 = f'''
open("/kaggle/working/render_setup.py", "w").write({SETUP_SCRIPT!r})
print("wrote render_setup.py")
'''

    c_telemetry = '''
import subprocess, threading

def _telemetry_loop(stop_event, interval=5):
    # Runs on a background thread so a telemetry hiccup can never abort a
    # render frame. Per-GPU lines only -- never aggregated, since Kaggle's
    # GPU allocation isn't guaranteed (a GPU request once returned a single
    # P100 instead of the expected T4 x2), so each card must be independently
    # visible.
    while not stop_event.is_set():
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
            "BR_OUTPUT": f"{{OUT}}/f_"}})

done, failed, _archived = [], [], set()
for frame in FRAMES:
    t0 = time.time()
    p = subprocess.run([BBIN, blend, "-b", "-noaudio", "-P",
                        "/kaggle/working/render_setup.py", "-f", str(frame)],
                       env=env, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    ok = p.returncode == 0
    (done if ok else failed).append(frame)
    if ok:
        # Appended right after THIS frame succeeds, not batched to the
        # end of the loop -- see the ARCHIVE comment above for why.
        for f in glob.glob(f"{{OUT}}/*"):
            name = os.path.basename(f)
            if name in _archived:
                continue
            with zipfile.ZipFile(ARCHIVE, "a", zipfile.ZIP_STORED) as zf:
                zf.write(f, arcname=name)
            _archived.add(name)
    # PROGRESS lines are what the desktop app parses out of the log stream.
    print(f"PROGRESS frame={{frame}} ok={{ok}} secs={{time.time()-t0:.1f}} "
          f"done={{len(done)}}/{{len(FRAMES)}}", flush=True)
    if not ok:
        print(p.stdout[-2000:])

print("DONE", sorted(done), "FAILED", sorted(failed))
for f in sorted(glob.glob(f"{{OUT}}/*")):
    print(f"  {{os.path.basename(f)}} {{os.path.getsize(f)//1024}} KB")
_telemetry_stop.set()
'''

    nb = {"cells": [_code(c1), _code(c2), _code(c3), _code(c_telemetry), _code(c4)],
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
        "machine_shape": MACHINE_SHAPE,
        "enable_internet": True,
        "dataset_sources": [dataset_slug],
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }, indent=2), encoding="utf-8")
    return nb_path
