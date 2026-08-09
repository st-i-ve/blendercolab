<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo/mark-white.png">
  <img src="assets/logo/mark-black.png" alt="BlendFleet" width="128">
</picture>

# BlendFleet

**Render Blender animations across a fleet of free Kaggle GPUs — yours and your friends'.**

[![Windows](https://img.shields.io/badge/Windows-desktop_app-F5792A?style=for-the-badge&labelColor=16181D)](#install)
[![Linux](https://img.shields.io/badge/Linux-build_script-4A96F0?style=for-the-badge&labelColor=16181D)](#install)
[![Tests](https://img.shields.io/badge/tests-480_passing-3DBF7A?style=for-the-badge&labelColor=16181D)](#developing)
[![Python](https://img.shields.io/badge/Python-3.11+-9B7AE8?style=for-the-badge&labelColor=16181D)](#developing)

</div>

---

## What it does

One Blender project. Several Kaggle accounts. BlendFleet splits the frame range
across them, uploads the `.blend` **once**, shares it with everyone
automatically, and streams the renders back into a single folder.

Frames are assigned by **stride** — worker 1 takes frames 1, 4, 7…, worker 2
takes 2, 5, 8… — so a friend dropping out leaves gaps spread evenly through the
animation instead of one missing chunk, and whatever finished is still usable.

<div align="center">

|  | |
|---|---|
| 🎞️ **Frame filmstrip** | every frame as a cell, tinted by *which account rendered it* |
| 📤 **Resumable uploads** | a break at 90% resumes at 90%, with live speed and ETA |
| 🔗 **Automatic sharing** | one upload; friends granted read access over the API |
| 📊 **Per-GPU telemetry** | utilisation, VRAM and temperature per physical GPU, live |
| 🎨 **Five accent colours** | applied instantly, no restart |

</div>

---

## Install

Grab `blendfleet.exe` from the build, or build it yourself:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install PySide6 kaggle pyinstaller
powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1
```

```bash
# Linux
python3 -m venv .venv && .venv/bin/python -m pip install PySide6 kaggle pyinstaller
./packaging/build_linux.sh
```

The build scripts refuse to run against the wrong interpreter and fail on a
suspiciously small binary — PyInstaller freezes whatever environment it runs
*in*, and a build missing PySide6 still reports success.

## Setup

1. Each person generates a Kaggle API token at
   [kaggle.com/settings](https://www.kaggle.com/settings) → **API** →
   *Create New Token*.
2. Add them in **Manage accounts**. Each is verified against Kaggle on the spot —
   a bad token is rejected, not stored.
3. Pick a `.blend`, set the frame range, press render.

> **A Kaggle API token grants full access to that account.** Only accept tokens
> from people who understand that. They can revoke one at any time from the same
> page.

---

## Things worth knowing

These are measured against the live API, not assumed.

**Kaggle has no idle instances.** A session exists only while a kernel runs, so
there is nothing to poll between renders. Instance cards show live quota,
last-known hardware *labelled with its age*, and live GPU load **only while that
account is actually rendering**. An idle card reads as idle rather than showing
a stale number in a live-looking dial.

**Allocation is not guaranteed.** A GPU request has returned a single Tesla P100
when T4 ×2 was expected. Nothing assumes a GPU count or model.

**The pre-launch check compares file size, not content.** Kaggle's API exposes no
hash or checksum for dataset files — verified against the SDK — so the app says
"size matches" and never claims an integrity check it cannot perform.

**Uploads are one request, not chunked.** Kaggle's endpoint is a GCS resumable
session: out-of-order and concurrent ranges are both rejected, and chunking
measured *slower* (0.59 vs 0.95 MB/s). So it sends one full-speed request with
real progress and resume-from-committed-offset instead.

**Reference timing:** 1920×1080 at 128 samples renders in **57.1 s/frame** on a
Tesla P100. A 250-frame animation is roughly 4 GPU-hours solo, or ~1.3 h each
across three accounts.

---

## Developing

```bash
QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/ -q
```

The suite runs entirely offline. `tests/conftest.py` installs autouse guards that
fail a test which opens a socket, leaks a thread, or raises an unstubbed modal
dialog — each one is there because that exact failure once made the suite hang or
crash non-deterministically instead of failing honestly.

```
blendfleet/
  uploader.py         resumable upload with progress + resume
  sharing.py          collaborator grants via the dataset metadata API
  fleet.py            stride assignment, launch, poll, cancel
  log_stream.py       SSE: progress, telemetry, hardware banner
  instance_state.py   last-known hardware per account
  notebook_builder.py generates the Kaggle notebook that does the rendering
  ui/                 dashboard, instance cards, filmstrip, settings, theme
```

---

## Accent colours

<div align="center">

<img src="assets/logo/mark-orange.png" width="52"> <img src="assets/logo/mark-green.png" width="52"> <img src="assets/logo/mark-purple.png" width="52"> <img src="assets/logo/mark-blue.png" width="52"> <img src="assets/logo/mark-red.png" width="52">

`#F5792A` &nbsp;&nbsp; `#3DBF7A` &nbsp;&nbsp; `#9B7AE8` &nbsp;&nbsp; `#4A96F0` &nbsp;&nbsp; `#E85454`

</div>

All five clear WCAG 4.5:1 against the app background. Warnings stay amber
independently of your choice — including when the accent *is* red — so an error
never blends into ordinary chrome.

---

## Also here

- **`kaggle_blender_render.ipynb`** — the standalone notebook this grew out of.
  Runs a render on Kaggle without the desktop app.
- **`collabrendertest.ipynb`** — the original Google Colab notebook. Superseded:
  Colab needed the tab open and died after ~90 minutes.

## Credits

Type is [Roboto](https://fonts.google.com/specimen/Roboto) (Apache-2.0), icons are
[Lucide](https://lucide.dev) (ISC). Licences are vendored beside the assets.
