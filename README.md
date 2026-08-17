<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo/mark-white.png">
  <img src="assets/logo/mark-black.png" alt="BlendFleet" width="128">
</picture>

# BlendFleet

**Render Blender animations across a fleet of free Kaggle GPUs — yours and your friends'.**

[![Windows](https://img.shields.io/badge/Windows-desktop_app-F5792A?style=for-the-badge&labelColor=16181D)](#install)
[![Linux](https://img.shields.io/badge/Linux-build_script-4A96F0?style=for-the-badge&labelColor=16181D)](#install)
[![Tests](https://img.shields.io/badge/tests-1391_passing-3DBF7A?style=for-the-badge&labelColor=16181D)](#developing)
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
| 🎞️ **Frame grid** | every frame as a cell, or as thumbnails you can scroll |
| 🖼️ **Frame preview** | open any finished frame full size, without collecting the job |
| 📤 **Resumable uploads** | a break at 90% resumes at 90%, with live speed and ETA |
| 🔗 **Automatic sharing** | one upload; friends granted read access over the API |
| 📊 **Per-GPU telemetry** | utilisation and VRAM per physical GPU, live |
| 🌙 **Themes that switch as one** | light, black, or frosted glass over your desktop |
| 🎨 **Eight accents, four typefaces** | applied instantly, no restart |
| 🔕 **Keeps running** | close mid-render and it follows the job from the tray |

</div>

---

## Install

There are **two builds of the same app**, sharing one backend:

| | | |
|---|---|---|
| **`blendfleetweb.exe`** | the current UI — Qt window, HTML dashboard inside | recommended |
| `blendfleet.exe` | the original all-Qt UI | still builds, no longer developed |

Both are *onedir* builds: the `.exe` sits beside an `_internal/` folder and needs
it, so move or shortcut the whole `dist/blendfleetweb/` directory rather than the
exe alone.

There is also an **Electron shell** in progress (`electron/`), running the
same dashboard against the same Python backend over a pipe rather than
through Qt. It builds for Windows, Linux and macOS from one place. See
*The Electron shell* below.

### Build it

```powershell
# Windows — from a clean checkout
python -m venv .venv
.venv\Scripts\python.exe -m pip install PySide6 kaggle pyinstaller
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean packaging/blendfleetweb.spec
# -> dist/blendfleetweb/blendfleetweb.exe
```

```bash
# Linux
python3 -m venv .venv && .venv/bin/python -m pip install PySide6 kaggle pyinstaller
.venv/bin/python -m PyInstaller --noconfirm --clean packaging/blendfleetweb.spec
```

`packaging/build_windows.ps1` and `build_linux.sh` build the **Qt** app
(`blendfleet.spec`) with extra safety checks — they refuse to run against the
wrong interpreter and fail on a suspiciously small binary, because PyInstaller
freezes whatever environment it runs *in* and a build missing PySide6 still
reports success. The web build is the PyInstaller line above; run it against the
project's own `.venv` for the same reason.

## Setup

1. Each person generates a Kaggle API token at
   [kaggle.com/settings](https://www.kaggle.com/settings) → **API** →
   *Create New Token*.
2. Add them under **Instances → Add account** (**Verify & add**). Each token is
   checked against Kaggle on the spot — one that cannot render is rejected rather
   than stored, because an account that sits in the fleet looking merely idle is
   worse than an absent one.
3. Pick a `.blend`, set the frame range, press **Render across fleet**.

While it runs, the dashboard shows a card per account and a grid of every frame;
switch that grid to thumbnails to see the frames themselves, or click one to open
it full size. Frames stay on Kaggle until you press **Collect frames**, which
packs them into a single zip.

Closing the window mid-render offers to keep BlendFleet running in the
notification area, so it keeps following the job and can still collect it. The
render is on Kaggle either way — quitting never cancels it, it only stops this
app watching.

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

**The machine a run got costs nothing to ask.** A kernel's metadata carries
`machine_shape`, so a card can say "this run's machine: Tesla T4" from a
metadata request instead of a one-minute hardware probe. It names a *type*, not
a count — `NvidiaTeslaT4` is what Kaggle calls a machine that turns out to hold
two T4s — so the nvidia-smi banner from inside the run stays the source for how
many cards there were, and keeps its age.

**Sessions can outlive the app's memory of them.** A job forgotten, or the app
killed between pushing a kernel and writing its state file, leaves a render
spending quota with nothing on the dashboard to say so. **Instances → Stray
sessions** lists the live ones and offers to stop them. It matches only kernels
BlendFleet named (`blendfleet-worker-<8 hex>`, `<stem>-render-<8 hex>`), because
the button next to each one cancels it.

**A notebook can be edited underneath a render.** Every generated cell carries a
`BLENDFLEET-NOTEBOOK contract=N` marker. When a finished render's frame count
cannot be read, the app checks whether the notebook on Kaggle still prints the
lines it parses, so "the log was unreadable" and "the notebook was changed" stop
looking identical.

**The base image is pinned per kernel** (`docker_image_pinning_type: original`),
so a rerun cannot silently land on a new CUDA driver. It does not pin *across*
jobs — each render is a new kernel. That part is possible and not done yet:
`kernel-metadata.json` also accepts an explicit `docker_image` digest, which
would hold every render on one image, but a digest hardcoded in the source would
rot the day Kaggle retires it with nothing in the UI to explain why renders
stopped starting. It wants a setting.

**Three session controls in Settings, all sent on the push:**

- **Machine to ask for** — T4 ×2 or P100. T4 gets two cards and Cycles splits a
  frame across both; P100 is one card with its memory undivided. T4 remains the
  default because it was measured faster. TPU is not offered: Cycles cannot use
  one, so it would only be a way to spend a session producing nothing.
- **Stop a session after** *N* minutes — 0 leaves Kaggle's own limit, which is
  hours. This is the difference between a hung render costing minutes and costing
  most of an account's week.
- **Pin the base image** to an exact digest, for a project that needs every
  render on one CUDA driver. Empty is the right answer unless you have a reason.

An invalid `machine_shape` is accepted by Kaggle at push time with **no error**
and silently gives a single P100, so that one is whitelisted in Python and the
page is *sent* the list rather than keeping its own.

**Still unused:** `delete_kernel`, for housekeeping old notebooks.

---

## Developing

### Run it from source

No build needed — this is the same app the exe wraps:

```bash
.venv/Scripts/python.exe -m blendfleet.web_main    # the web UI (current)
.venv/Scripts/python.exe -m blendfleet             # the all-Qt UI
```

### Run the tests

```bash
.venv/Scripts/python.exe -m pip install pytest
QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/ -q
```

1500 tests, entirely offline — including the browser ones, which drive the real
page in a real QtWebEngine. `tests/conftest.py` installs autouse guards that fail
a test which opens a socket, leaks a thread, or raises an unstubbed modal dialog:
each is there because that exact failure once made the suite hang or crash
non-deterministically instead of failing honestly.

> **The exit code is pytest's, deliberately.** QtWebEngine's browser thread dies
> with an access violation while Python finalises, *after* the summary is
> printed — measured on Qt 6.11.1, and identically on commits that predate any
> of this app's threading work, so it is upstream teardown. Left alone it makes
> every run exit 139, green or not. `pytest_unconfigure` in `conftest.py` runs
> this app's own shutdown work, flushes, then exits with pytest's verdict. The
> crash still prints; it just no longer decides whether the suite passed.

### The Electron shell

`electron/` is a second shell over the same core: the Qt window is
replaced by an Electron one, and QWebChannel by a headless Python
sidecar speaking one JSON object per line over stdio.

Being a real desktop shell, it does the things a page cannot:

- **keeps the machine awake while work is in flight** — held against the
  same `busyChanged` keys the dashboard uses, released the moment nothing
  is running. A laptop that suspends mid-upload loses the upload.
- **polls the moment the machine wakes**, instead of showing readings from
  before the lid closed until the 30-second timer next fires.
- **puts render progress on the taskbar** (and a count badge), which is the
  point when the window is hidden in the tray.
- **raises an OS notification only when the window is hidden** — a toast
  duplicating one already on screen is noise.
- **jump list and recent `.blend` files** on the taskbar right-click.
- **follows the desktop's light/dark setting** when the theme is set to
  System, resolved by the page through `prefers-color-scheme` and by the
  window through `nativeTheme`, so the two cannot disagree.

```bash
cd electron && npm install     # Electron itself, ~150 MB
npm start                      # runs against blendfleet/web/ and .venv
npm run backend                # freeze the sidecar -> dist/backend/
npm run dist                   # package -> dist/electron/
```

Two things make this affordable rather than a rewrite. The render core
imports no PySide6 — Qt only ever appears in the entry points and `ui/`
— so the sidecar is the same `fleet.py`, `uploader.py` and
`log_stream.py` the Qt build uses. And the dashboard talks to exactly one
object, `backend`, so `electron/preload.js` rebuilds that object's shape
on the other side of the pipe and `blendfleet/web/` runs **unchanged**:
byte for byte the same files both shells serve. Everything
Electron-specific (the accent ground, the window's drag region) is
injected from `electron/shell.css`.

The sidecar is worth its own line: **30 MB frozen, with no Qt in it at
all**, which is what `packaging/blendfleet-backend.spec` excluding
PySide6 buys. `blendfleet/design.py` is what makes that possible — it
holds the accent, theme and font *names* so `Settings` can validate them
without importing the design system.

`dist/blendfleetweb/` is never touched by any of this; the Electron
output has its own tree. Linux and macOS artifacts come from
`.github/workflows/electron.yml`, because PyInstaller cannot
cross-compile: each platform's sidecar has to be frozen on that platform.

### Building the Electron app

```bash
cd electron
npm install
npm run backend          # freezes the Python sidecar -> dist/backend/
npm run pack:offline     # assembles dist/fleet-electron/
```

`dist/fleet-electron/` then holds `BlendFleet.exe` with `backend/` beside
it, shaped like `dist/blendfleetweb/` — double-click it and the app runs
against the real fleet, no install step.

> **Installer status.** There is a working packaged app; there is not yet
> an installer. `npm run dist` (electron-builder, NSIS) gets as far as
> `packaging platform=win32 electron=43.4.0`, reports the cached Electron
> zip at 100%, then makes one more HTTPS request and sits on it for its
> full 600-second timeout. That is with the Electron zip, winCodeSign and
> NSIS caches primed by hand, with `--dir` (which skips NSIS entirely) and
> with `CSC_IDENTITY_AUTO_DISCOVERY=false`. The blocker is the network,
> not the config.
>
> `npm run pack:offline` exists because of that: `electron/pack-offline.js`
> does the copying electron-builder would have done, from files already on
> disk. No installer, no asar, Windows only — but a real double-clickable
> folder. Installers for all three platforms come from
> `.github/workflows/electron.yml`, which is also the only way to build
> the Linux and macOS sidecars, since PyInstaller cannot cross-compile.

### Changing the UI without rebuilding

The dashboard is `blendfleet/web/{index.html,app.css,app.js}` — plain files, no
bundler, no build step. Because the exe is a *onedir* build and those ship as
data, an edit reaches an installed copy by being copied over:

```bash
cp blendfleet/web/*.{html,css,js} dist/blendfleetweb/_internal/blendfleet/web/
```

Relaunch and the change is there. **A rebuild is required** for anything else —
Python, a new bundled font, a new accent (the list lives in `ui/theme.py`, and
`Settings` rejects a name it does not know), or a new preference key (mapped in
`bridge.setPreference`).

```
blendfleet/
  design.py           the accent/theme/font NAMES, with no Qt attached
  rpc/                the headless backend: session, protocol, sidecar
  uploader.py         resumable upload with progress + resume
  sharing.py          collaborator grants via the dataset metadata API
  fleet.py            stride assignment, launch, poll, cancel
  log_stream.py       SSE: progress, telemetry, hardware banner
  instance_state.py   last-known hardware per account
  notebook_builder.py generates the Kaggle notebook that does the rendering
  settings.py         the persisted preferences, each guarded against a bad file
  web/                the dashboard: index.html, app.css, app.js
  ui/web_host.py      the window around it — tray, context menu, background ground
  ui/bridge.py        every call the page can make, and every signal it receives
  ui/theme.py         accents, themes, typefaces, and the Qt stylesheet
electron/             the Electron shell: window, tray, dialogs, IPC shim
```

---

## Looks

Eight accents and four typefaces, in **Settings → Appearance**, applied instantly
and remembered — window chrome included, so the shell never ends up in a
different colour or face from the page inside it.

| accent | | accent | |
|---|---|---|---|
| orange | `#E8935A` | dark orange | `#C2703A` |
| blue | `#5A9BD8` | dark red | `#A2464B` |
| green | `#4DB690` | slate | `#5A6BA8` |
| purple | `#9B7FD4` | | |
| red | `#D4708F` | | |

Every one is measured, not eyeballed: `tests/test_theme.py` computes WCAG
contrast for each accent's text colour on both themes, and for the label that
sits *on* the accent fill. That label is picked per accent from the measurement
rather than fixed — black on the six paler fills, white on dark red and slate,
where black measures 3.15:1 and white 5.98:1. Warnings stay amber independently
of your choice, including when the accent *is* red, so an error never blends
into ordinary chrome.

Typefaces are Heebo (the base), Inter, Arimo and Oswald. Each ships twice — a
variable TTF for Qt, a woff2 latin subset for the page — because the app must
render its own text without a network.

---

## Also here

- **`kaggle_blender_render.ipynb`** — the standalone notebook this grew out of.
  Runs a render on Kaggle without the desktop app.
- **`collabrendertest.ipynb`** — the original Google Colab notebook. Superseded:
  Colab needed the tab open and died after ~90 minutes.

## Credits

Type is [Heebo](https://fonts.google.com/specimen/Heebo),
[Inter](https://fonts.google.com/specimen/Inter),
[Arimo](https://fonts.google.com/specimen/Arimo) and
[Oswald](https://fonts.google.com/specimen/Oswald) (all OFL-1.1), with
[Roboto Mono](https://fonts.google.com/specimen/Roboto+Mono) (Apache-2.0) as the
fallback; icons are [Lucide](https://lucide.dev) (ISC). Licences are vendored
beside the assets in `assets/fonts/LICENSE.md`.
