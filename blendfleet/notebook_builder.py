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


@dataclass
class RenderSettings:
    resolution_x: int
    resolution_y: int
    samples: int
    file_format: str = "PNG"
    blender_version: str = "5.2.0"


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
SAMPLES, FMT = {settings.samples}, "{settings.file_format}"
BLENDER_VERSION = "{settings.blender_version}"

vm = psutil.virtual_memory()
print(f"CPU {{psutil.cpu_count(logical=True)}} cores | RAM {{vm.total/2**30:.1f}} GB")
print(subprocess.run("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader",
                     shell=True, capture_output=True, text=True).stdout.strip())

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

    c4 = '''
import os, glob, time, shutil, subprocess
WORK = "/kaggle/tmp/work"
os.makedirs(WORK, exist_ok=True)
blend = f"{WORK}/scene.blend"
shutil.copy(BLEND, blend)          # /kaggle/input is READ-ONLY
OUT = "/kaggle/working/frames"
os.makedirs(OUT, exist_ok=True)

env = os.environ.copy()
env.update({"BR_RES_X": str(RES_X), "BR_RES_Y": str(RES_Y),
            "BR_SAMPLES": str(SAMPLES), "BR_FORMAT": FMT,
            "BR_OUTPUT": f"{OUT}/f_"})

done, failed = [], []
for frame in FRAMES:
    t0 = time.time()
    p = subprocess.run([BBIN, blend, "-b", "-noaudio", "-P",
                        "/kaggle/working/render_setup.py", "-f", str(frame)],
                       env=env, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    ok = p.returncode == 0
    (done if ok else failed).append(frame)
    # PROGRESS lines are what the desktop app parses out of the log stream.
    print(f"PROGRESS frame={frame} ok={ok} secs={time.time()-t0:.1f} "
          f"done={len(done)}/{len(FRAMES)}", flush=True)
    if not ok:
        print(p.stdout[-2000:])

print("DONE", sorted(done), "FAILED", sorted(failed))
for f in sorted(glob.glob(f"{OUT}/*")):
    print(f"  {os.path.basename(f)} {os.path.getsize(f)//1024} KB")
'''

    nb = {"cells": [_code(c1), _code(c2), _code(c3), _code(c4)],
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
        "enable_internet": True,
        "dataset_sources": [dataset_slug],
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }, indent=2), encoding="utf-8")
    return nb_path
