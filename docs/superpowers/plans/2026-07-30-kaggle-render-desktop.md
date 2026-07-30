# Kaggle Render Desktop App Implementation Plan

> **SUPERSEDED 2026-07-30 by `2026-07-30-blendfleet-desktop.md`.**
> Three of this plan's Global Constraints were disproved by testing against a
> live Kaggle account: auth does not use `kaggle.json`, cancellation IS possible
> via the SDK, and GPU quota IS exposed by the API. Do not execute this plan.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Windows desktop app that submits Blender renders to Kaggle and monitors them, with no browser involved.

**Architecture:** Five layers with one-way dependencies — `kaggle_client` and `drive_client` wrap external tools, `notebook_builder` generates the notebook to push, `job` orchestrates a single render and persists its state, and `ui` talks only to `job`. Nothing in `ui` imports the Kaggle API. All external calls go through subprocess or the official Kaggle Python client so they can be stubbed in tests.

**Tech Stack:** Python 3.11+, PySide6 (GUI), `kaggle` (official client), rclone (external binary), pytest, PyInstaller.

## Global Constraints

- Python 3.11 or newer.
- Windows is the only supported target for v1. Paths use `pathlib`, never string concatenation.
- Kaggle auth reuses `~/.kaggle/kaggle.json`. The app never invents its own credential store for Kaggle.
- Google Drive credentials live in a private Kaggle dataset named `render-creds`, never in a Kaggle Secret. Secrets cannot be set via API (Kaggle/kaggle-api#582).
- **The app can start renders but cannot stop them.** There is no API to stop a kernel (Kaggle/kaggle-api#388). No Cancel button may appear in the UI at any point.
- **Never display a weekly GPU quota figure.** No API endpoint exposes it. Link to `https://www.kaggle.com/settings` instead.
- Kaggle concurrent limits: 2 GPU sessions, 5 CPU sessions.
- `kaggle kernels push` always triggers a run — it is never a no-op, even with identical content.
- No network calls in unit tests. Every external call is injected and stubbed.
- Every module gets a matching test file under `tests/`.

---

## File Structure

```
app/
  __init__.py
  config.py             AppConfig: paths, slugs, persisted settings
  kaggle_client.py      wraps the kaggle CLI/API: dataset version, kernel push, status
  notebook_builder.py   renders the CONFIG cell into a copy of the template notebook
  drive_client.py       rclone: count finished frames in the output folder
  job.py                RenderJob: build -> upload -> push -> poll; persists state
  ui/
    __init__.py
    main_window.py      the single window
    job_view.py         status panel for the active job
  templates/
    render_template.ipynb   copied from ../../kaggle_blender_render.ipynb
tests/
  test_config.py
  test_kaggle_client.py
  test_notebook_builder.py
  test_drive_client.py
  test_job.py
pyproject.toml
```

Rationale: `kaggle_client` and `drive_client` are the only modules that touch the outside world, so they are the only ones needing subprocess stubs. `job` holds all orchestration and is the sole module the UI depends on, which keeps the UI thin enough to be worth not unit-testing.

---

### Task 1: Project scaffold and config

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces: `AppConfig` dataclass with fields `kaggle_username: str`, `project_slug: str`, `creds_slug: str`, `kernel_slug: str`, `drive_remote: str`, `drive_output_dir: str`, and `AppConfig.state_dir() -> Path`, `AppConfig.load() -> AppConfig`, `AppConfig.save() -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import json
from pathlib import Path
import pytest
from app.config import AppConfig


def test_slugs_derive_from_username():
    cfg = AppConfig(kaggle_username="st-i-ve", project_name="malia")
    assert cfg.project_slug == "st-i-ve/malia"
    assert cfg.creds_slug == "st-i-ve/render-creds"
    assert cfg.kernel_slug == "st-i-ve/blendercolab-render"


def test_state_dir_is_under_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    cfg = AppConfig(kaggle_username="st-i-ve", project_name="malia")
    assert cfg.state_dir() == tmp_path / "blendercolab-render"
    assert cfg.state_dir().is_dir()


def test_save_then_load_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    cfg = AppConfig(kaggle_username="st-i-ve", project_name="malia",
                    drive_remote="gdrive")
    cfg.save()
    loaded = AppConfig.load()
    assert loaded.kaggle_username == "st-i-ve"
    assert loaded.project_name == "malia"
    assert loaded.drive_remote == "gdrive"


def test_load_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert AppConfig.load() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 3: Write pyproject.toml**

```toml
[project]
name = "blendercolab-render"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "PySide6>=6.6",
    "kaggle>=1.6",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-qt>=4.4"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 4: Write minimal implementation**

```python
# app/__init__.py
```

```python
# app/config.py
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

CONFIG_FILENAME = "config.json"


@dataclass
class AppConfig:
    kaggle_username: str
    project_name: str
    drive_remote: str = "gdrive"
    drive_output_root: str = "Blender/render_results"

    @property
    def project_slug(self) -> str:
        return f"{self.kaggle_username}/{self.project_name}"

    @property
    def creds_slug(self) -> str:
        return f"{self.kaggle_username}/render-creds"

    @property
    def kernel_slug(self) -> str:
        return f"{self.kaggle_username}/blendercolab-render"

    @property
    def drive_output_dir(self) -> str:
        return f"{self.drive_remote}:{self.drive_output_root}/{self.project_name}"

    def state_dir(self) -> Path:
        base = Path(os.environ["APPDATA"]) / "blendercolab-render"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def save(self) -> None:
        path = self.state_dir() / CONFIG_FILENAME
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "AppConfig | None":
        base = Path(os.environ["APPDATA"]) / "blendercolab-render"
        path = base / CONFIG_FILENAME
        if not path.exists():
            return None
        return cls(**json.loads(path.read_text(encoding="utf-8")))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS, 4 tests

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml app/ tests/test_config.py
git commit -m "feat: project scaffold and AppConfig"
```

---

### Task 2: Kaggle client wrapper

**Files:**
- Create: `app/kaggle_client.py`
- Test: `tests/test_kaggle_client.py`

**Interfaces:**
- Consumes: nothing from Task 1
- Produces:
  - `KaggleClient(runner: Callable[[list[str]], CompletedProcess] = ...)`
  - `KaggleClient.whoami() -> str`
  - `KaggleClient.dataset_version(folder: Path, slug: str, message: str) -> None`
  - `KaggleClient.dataset_create(folder: Path, slug: str, title: str) -> None`
  - `KaggleClient.kernel_push(folder: Path) -> None`
  - `KaggleClient.kernel_status(slug: str) -> KernelStatus`
  - `KernelStatus` dataclass: `state: str`, `message: str`; `state` is one of `"queued" | "running" | "complete" | "error"`
  - `KaggleError(Exception)`

Note: injecting `runner` is what makes this testable without a network or a token.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kaggle_client.py
import json
import subprocess
from pathlib import Path
import pytest
from app.kaggle_client import KaggleClient, KaggleError, KernelStatus


def fake_runner(result_stdout="", returncode=0, stderr=""):
    calls = []

    def run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode,
                                           stdout=result_stdout, stderr=stderr)
    run.calls = calls
    return run


def test_whoami_returns_username():
    r = fake_runner('{"username": "st-i-ve"}')
    assert KaggleClient(runner=r).whoami() == "st-i-ve"


def test_dataset_version_invokes_correct_command(tmp_path):
    r = fake_runner()
    KaggleClient(runner=r).dataset_version(tmp_path, "st-i-ve/malia", "lighting tweak")
    argv = r.calls[0]
    assert argv[:3] == ["kaggle", "datasets", "version"]
    assert "-m" in argv and "lighting tweak" in argv
    assert str(tmp_path) in argv


def test_kernel_push_invokes_correct_command(tmp_path):
    r = fake_runner()
    KaggleClient(runner=r).kernel_push(tmp_path)
    assert r.calls[0][:3] == ["kaggle", "kernels", "push"]


def test_kernel_status_parses_running():
    r = fake_runner('{"status": "running", "failureMessage": null}')
    st = KaggleClient(runner=r).kernel_status("st-i-ve/blendercolab-render")
    assert st.state == "running"


def test_kernel_status_parses_error_with_message():
    r = fake_runner('{"status": "error", "failureMessage": "boom"}')
    st = KaggleClient(runner=r).kernel_status("st-i-ve/x")
    assert st.state == "error"
    assert st.message == "boom"


def test_nonzero_exit_raises_kaggle_error():
    r = fake_runner(returncode=1, stderr="401 Unauthorized")
    with pytest.raises(KaggleError, match="401 Unauthorized"):
        KaggleClient(runner=r).kernel_push(Path("."))


def test_client_never_exposes_a_stop_method():
    # There is no API to stop a kernel (Kaggle/kaggle-api#388).
    # Asserting absence keeps a future contributor from inventing one.
    assert not hasattr(KaggleClient, "kernel_stop")
    assert not hasattr(KaggleClient, "cancel")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_kaggle_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.kaggle_client'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/kaggle_client.py
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

Runner = Callable[[list[str]], subprocess.CompletedProcess]


class KaggleError(Exception):
    """A kaggle CLI invocation failed."""


@dataclass
class KernelStatus:
    state: str
    message: str = ""


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


class KaggleClient:
    def __init__(self, runner: Runner = _default_runner) -> None:
        self._run = runner

    def _exec(self, argv: list[str]) -> str:
        proc = self._run(argv)
        if proc.returncode != 0:
            raise KaggleError((proc.stderr or proc.stdout or "").strip())
        return proc.stdout

    def whoami(self) -> str:
        out = self._exec(["kaggle", "config", "view", "--csv"])
        try:
            return json.loads(out)["username"]
        except (json.JSONDecodeError, KeyError):
            raise KaggleError(f"could not parse username from: {out!r}")

    def dataset_create(self, folder: Path, slug: str, title: str) -> None:
        self._exec(["kaggle", "datasets", "create", "-p", str(folder), "-u"])

    def dataset_version(self, folder: Path, slug: str, message: str) -> None:
        self._exec(["kaggle", "datasets", "version",
                    "-p", str(folder), "-m", message, "-d"])

    def kernel_push(self, folder: Path) -> None:
        # Always triggers a run -- never a no-op, even for identical content.
        self._exec(["kaggle", "kernels", "push", "-p", str(folder)])

    def kernel_status(self, slug: str) -> KernelStatus:
        out = self._exec(["kaggle", "kernels", "status", slug])
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            raise KaggleError(f"could not parse status from: {out!r}")
        return KernelStatus(state=data.get("status", "unknown"),
                            message=data.get("failureMessage") or "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_kaggle_client.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add app/kaggle_client.py tests/test_kaggle_client.py
git commit -m "feat: Kaggle CLI wrapper with injected runner"
```

---

### Task 3: Notebook builder

**Files:**
- Create: `app/notebook_builder.py`
- Create: `app/templates/render_template.ipynb` (copy of `kaggle_blender_render.ipynb`)
- Test: `tests/test_notebook_builder.py`

**Interfaces:**
- Consumes: `AppConfig` from Task 1
- Produces:
  - `RenderSettings` dataclass: `start_frame: int`, `end_frame: int`, `resolution_x: int`, `resolution_y: int`, `samples: int`, `file_format: str`, `blender_version: str = "5.2.0"`
  - `build_notebook(template: Path, cfg: AppConfig, settings: RenderSettings, out_dir: Path) -> Path`
  - `build_kernel_metadata(cfg: AppConfig, out_dir: Path) -> Path`

The CONFIG cell is identified by its marker comment `# ============================== CONFIG`. Replacing the whole cell (rather than patching lines) keeps the pushed notebook self-describing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notebook_builder.py
import json
from pathlib import Path
import pytest
from app.config import AppConfig
from app.notebook_builder import (RenderSettings, build_notebook,
                                  build_kernel_metadata)


@pytest.fixture
def template(tmp_path):
    nb = {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["# title\n"]},
            {"cell_type": "code", "execution_count": None, "metadata": {},
             "outputs": [],
             "source": ["# ============================== CONFIG ====\n",
                        "BLENDER_VERSION = \"4.2.0\"\n",
                        "START_FRAME = 999\n"]},
            {"cell_type": "code", "execution_count": None, "metadata": {},
             "outputs": [], "source": ["print('render')\n"]},
        ],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }
    p = tmp_path / "template.ipynb"
    p.write_text(json.dumps(nb), encoding="utf-8")
    return p


@pytest.fixture
def cfg():
    return AppConfig(kaggle_username="st-i-ve", project_name="malia")


def test_config_cell_is_replaced(template, cfg, tmp_path):
    s = RenderSettings(start_frame=1, end_frame=120, resolution_x=1920,
                       resolution_y=1080, samples=512, file_format="PNG")
    out = build_notebook(template, cfg, s, tmp_path / "out")
    nb = json.loads(out.read_text(encoding="utf-8"))
    src = "".join(nb["cells"][1]["source"])
    assert "START_FRAME  = 1" in src
    assert "END_FRAME    = 120" in src
    assert "999" not in src


def test_other_cells_untouched(template, cfg, tmp_path):
    s = RenderSettings(1, 1, 100, 100, 16, "PNG")
    out = build_notebook(template, cfg, s, tmp_path / "out")
    nb = json.loads(out.read_text(encoding="utf-8"))
    assert "".join(nb["cells"][2]["source"]) == "print('render')\n"
    assert nb["cells"][0]["cell_type"] == "markdown"


def test_generated_notebook_is_valid_python(template, cfg, tmp_path):
    import ast
    s = RenderSettings(5, 10, 800, 600, 64, "JPEG")
    out = build_notebook(template, cfg, s, tmp_path / "out")
    nb = json.loads(out.read_text(encoding="utf-8"))
    ast.parse("".join(nb["cells"][1]["source"]))


def test_missing_config_marker_raises(tmp_path, cfg):
    nb = {"cells": [{"cell_type": "code", "execution_count": None,
                     "metadata": {}, "outputs": [], "source": ["x = 1\n"]}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    p = tmp_path / "bad.ipynb"
    p.write_text(json.dumps(nb), encoding="utf-8")
    s = RenderSettings(1, 1, 100, 100, 16, "PNG")
    with pytest.raises(ValueError, match="CONFIG"):
        build_notebook(p, cfg, s, tmp_path / "out")


def test_kernel_metadata_has_gpu_internet_and_both_datasets(cfg, tmp_path):
    path = build_kernel_metadata(cfg, tmp_path)
    meta = json.loads(path.read_text(encoding="utf-8"))
    assert meta["enable_gpu"] is True
    assert meta["enable_internet"] is True
    assert meta["is_private"] is True
    assert meta["id"] == "st-i-ve/blendercolab-render"
    assert set(meta["dataset_sources"]) == {"st-i-ve/render-creds", "st-i-ve/malia"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_notebook_builder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.notebook_builder'`

- [ ] **Step 3: Copy the template notebook**

```bash
mkdir -p app/templates
cp kaggle_blender_render.ipynb app/templates/render_template.ipynb
```

- [ ] **Step 4: Write minimal implementation**

```python
# app/notebook_builder.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.config import AppConfig

CONFIG_MARKER = "# ============================== CONFIG"


@dataclass
class RenderSettings:
    start_frame: int
    end_frame: int
    resolution_x: int
    resolution_y: int
    samples: int
    file_format: str
    blender_version: str = "5.2.0"


def _config_source(cfg: AppConfig, s: RenderSettings) -> str:
    return f'''{CONFIG_MARKER} ==============================
# Generated by blendercolab-render. Edits here are overwritten on the next push.

BLENDER_VERSION = "{s.blender_version}"

DRIVE_REMOTE   = "{cfg.drive_remote}"
PROJECT_FOLDER = "{cfg.project_name}"
BLEND_FILE     = "{cfg.project_name}"
OUTPUT_NAME    = "{cfg.project_name}"

DRIVE_PROJECT_DIR = f"{{DRIVE_REMOTE}}:Blender/{{PROJECT_FOLDER}}"
DRIVE_OUTPUT_DIR  = "{cfg.drive_output_dir}"

START_FRAME  = {s.start_frame}
END_FRAME    = {s.end_frame}
RESOLUTION_X = {s.resolution_x}
RESOLUTION_Y = {s.resolution_y}
SAMPLES      = {s.samples}
FPS          = 24
FILE_FORMAT  = "{s.file_format}"

MAX_RUNTIME_HOURS = 8.5
SKIP_EXISTING     = True
DEVICE            = "OPTIX"

SERIES       = ".".join(BLENDER_VERSION.split(".")[:2])
TARBALL      = f"blender-{{BLENDER_VERSION}}-linux-x64.tar.xz"
BLENDER_URL  = f"https://download.blender.org/release/Blender{{SERIES}}/{{TARBALL}}"
BLENDER_DIR  = f"/kaggle/tmp/blender-{{BLENDER_VERSION}}-linux-x64"
BLENDER_BIN  = f"{{BLENDER_DIR}}/blender"
WORK_DIR     = "/kaggle/tmp/work"
PROJECT_DIR  = f"{{WORK_DIR}}/project"
RENDER_DIR   = f"{{WORK_DIR}}/frames"

import os, subprocess
for d in (WORK_DIR, PROJECT_DIR, RENDER_DIR):
    os.makedirs(d, exist_ok=True)

def sh(cmd, check=True):
    p = subprocess.run(cmd, shell=True, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.stdout:
        print(p.stdout.rstrip())
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed ({{p.returncode}}): {{cmd}}")
    return p.returncode

print(f"Blender  : {{BLENDER_VERSION}}")
print(f"Frames   : {{START_FRAME}}..{{END_FRAME}}")
'''


def build_notebook(template: Path, cfg: AppConfig,
                   settings: RenderSettings, out_dir: Path) -> Path:
    nb = json.loads(template.read_text(encoding="utf-8"))

    target = None
    for cell in nb["cells"]:
        if cell["cell_type"] == "code" and \
                "".join(cell["source"]).lstrip().startswith(CONFIG_MARKER):
            target = cell
            break
    if target is None:
        raise ValueError(
            f"template has no cell starting with {CONFIG_MARKER!r}")

    target["source"] = _config_source(cfg, settings).splitlines(keepends=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "render.ipynb"
    out.write_text(json.dumps(nb, indent=1), encoding="utf-8")
    return out


def build_kernel_metadata(cfg: AppConfig, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": cfg.kernel_slug,
        "title": "blendercolab render",
        "code_file": "render.ipynb",
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True,
        "dataset_sources": [cfg.creds_slug, cfg.project_slug],
        "competition_sources": [],
        "kernel_sources": [],
    }
    path = out_dir / "kernel-metadata.json"
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_notebook_builder.py -v`
Expected: PASS, 5 tests

- [ ] **Step 6: Commit**

```bash
git add app/notebook_builder.py app/templates/ tests/test_notebook_builder.py
git commit -m "feat: generate notebook and kernel metadata from render settings"
```

---

### Task 4: Drive client for frame counting

**Files:**
- Create: `app/drive_client.py`
- Test: `tests/test_drive_client.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `DriveClient(runner: Callable[[list[str]], CompletedProcess] = ...)`
  - `DriveClient.count_frames(output_dir: str, prefix: str, ext: str) -> int | None` — returns `None` when rclone is unreachable, never raises

Returning `None` rather than raising matters: a Drive hiccup must degrade progress display, not fail the job.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drive_client.py
import subprocess
from app.drive_client import DriveClient


def fake_runner(stdout="", returncode=0):
    def run(argv):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")
    return run


def test_counts_matching_frames():
    listing = "malia_0001.png\nmalia_0002.png\nmalia_0003.png\n"
    c = DriveClient(runner=fake_runner(listing))
    assert c.count_frames("gdrive:Blender/render_results/malia", "malia", ".png") == 3


def test_ignores_non_matching_files():
    listing = "malia_0001.png\nnotes.txt\nthumbs/\nmalia_0002.png\n"
    c = DriveClient(runner=fake_runner(listing))
    assert c.count_frames("gdrive:x", "malia", ".png") == 2


def test_wrong_extension_not_counted():
    listing = "malia_0001.jpg\nmalia_0002.jpg\n"
    c = DriveClient(runner=fake_runner(listing))
    assert c.count_frames("gdrive:x", "malia", ".png") == 0


def test_returns_none_when_rclone_fails():
    c = DriveClient(runner=fake_runner("", returncode=1))
    assert c.count_frames("gdrive:x", "malia", ".png") is None


def test_returns_none_when_rclone_missing():
    def boom(argv):
        raise FileNotFoundError("rclone")
    assert DriveClient(runner=boom).count_frames("gdrive:x", "m", ".png") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_drive_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.drive_client'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/drive_client.py
from __future__ import annotations

import re
import subprocess
from typing import Callable

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


class DriveClient:
    def __init__(self, runner: Runner = _default_runner) -> None:
        self._run = runner

    def count_frames(self, output_dir: str, prefix: str, ext: str) -> int | None:
        """Number of finished frames on Drive, or None if Drive is unreachable.

        Never raises: a Drive problem must degrade the progress display, not
        fail the render job.
        """
        try:
            proc = self._run(["rclone", "lsf", output_dir])
        except OSError:
            return None
        if proc.returncode != 0:
            return None

        pattern = re.compile(rf"^{re.escape(prefix)}_\d+{re.escape(ext)}$")
        return sum(1 for line in proc.stdout.splitlines()
                   if pattern.match(line.strip()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_drive_client.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add app/drive_client.py tests/test_drive_client.py
git commit -m "feat: count finished frames on Drive via rclone"
```

---

### Task 5: Render job orchestration

**Files:**
- Create: `app/job.py`
- Test: `tests/test_job.py`

**Interfaces:**
- Consumes: `AppConfig` (Task 1), `KaggleClient`/`KaggleError`/`KernelStatus` (Task 2), `RenderSettings`/`build_notebook`/`build_kernel_metadata` (Task 3), `DriveClient` (Task 4)
- Produces:
  - `JobState` dataclass: `submitted_at: str`, `kernel_slug: str`, `total_frames: int`, `state: str`, `message: str`
  - `RenderJob(cfg, kaggle, drive, template_path, on_change: Callable[[JobState], None] | None)`
  - `RenderJob.submit(blend_folder: Path, settings: RenderSettings) -> JobState`
  - `RenderJob.poll() -> JobState`
  - `RenderJob.active_job() -> JobState | None` — reads persisted state
  - `ConcurrentJobError(Exception)` — raised by `submit` when a job is already active

`submit` must refuse when a previous job is still active, because Kaggle allows only 2 concurrent GPU sessions and the app has no way to stop one.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_job.py
import json
from pathlib import Path
import pytest

from app.config import AppConfig
from app.kaggle_client import KernelStatus, KaggleError
from app.notebook_builder import RenderSettings
from app.job import RenderJob, ConcurrentJobError


class FakeKaggle:
    def __init__(self, status="running"):
        self.status = status
        self.pushed = 0
        self.versioned = []

    def dataset_version(self, folder, slug, message):
        self.versioned.append(slug)

    def kernel_push(self, folder):
        self.pushed += 1

    def kernel_status(self, slug):
        return KernelStatus(state=self.status)


class FakeDrive:
    def __init__(self, count=0):
        self.count = count

    def count_frames(self, output_dir, prefix, ext):
        return self.count


@pytest.fixture
def template(tmp_path):
    nb = {"cells": [{"cell_type": "code", "execution_count": None,
                     "metadata": {}, "outputs": [],
                     "source": ["# ============================== CONFIG ==\n"]}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    p = tmp_path / "t.ipynb"
    p.write_text(json.dumps(nb), encoding="utf-8")
    return p


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return AppConfig(kaggle_username="st-i-ve", project_name="malia")


def test_submit_versions_dataset_then_pushes(cfg, template, tmp_path):
    k = FakeKaggle()
    job = RenderJob(cfg, k, FakeDrive(), template)
    st = job.submit(tmp_path, RenderSettings(1, 10, 100, 100, 16, "PNG"))
    assert "st-i-ve/malia" in k.versioned
    assert k.pushed == 1
    assert st.total_frames == 10
    assert st.state == "queued"


def test_submit_refuses_while_a_job_is_active(cfg, template, tmp_path):
    k = FakeKaggle(status="running")
    job = RenderJob(cfg, k, FakeDrive(), template)
    job.submit(tmp_path, RenderSettings(1, 5, 100, 100, 16, "PNG"))
    with pytest.raises(ConcurrentJobError):
        job.submit(tmp_path, RenderSettings(1, 5, 100, 100, 16, "PNG"))
    assert k.pushed == 1          # the second push never happened


def test_submit_allowed_again_once_complete(cfg, template, tmp_path):
    k = FakeKaggle(status="complete")
    job = RenderJob(cfg, k, FakeDrive(), template)
    job.submit(tmp_path, RenderSettings(1, 5, 100, 100, 16, "PNG"))
    job.poll()
    job.submit(tmp_path, RenderSettings(1, 5, 100, 100, 16, "PNG"))
    assert k.pushed == 2


def test_poll_reports_frames_done(cfg, template, tmp_path):
    job = RenderJob(cfg, FakeKaggle(), FakeDrive(count=7), template)
    job.submit(tmp_path, RenderSettings(1, 20, 100, 100, 16, "PNG"))
    st = job.poll()
    assert st.frames_done == 7
    assert st.total_frames == 20


def test_poll_survives_drive_outage(cfg, template, tmp_path):
    job = RenderJob(cfg, FakeKaggle(), FakeDrive(count=None), template)
    job.submit(tmp_path, RenderSettings(1, 20, 100, 100, 16, "PNG"))
    st = job.poll()
    assert st.frames_done is None
    assert st.state == "running"      # job unaffected


def test_state_persists_across_instances(cfg, template, tmp_path):
    job = RenderJob(cfg, FakeKaggle(), FakeDrive(), template)
    job.submit(tmp_path, RenderSettings(1, 42, 100, 100, 16, "PNG"))
    fresh = RenderJob(cfg, FakeKaggle(), FakeDrive(), template)
    assert fresh.active_job().total_frames == 42


def test_job_has_no_cancel_method():
    # No API exists to stop a kernel (Kaggle/kaggle-api#388).
    assert not hasattr(RenderJob, "cancel")
    assert not hasattr(RenderJob, "stop")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_job.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.job'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/job.py
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.config import AppConfig
from app.notebook_builder import (RenderSettings, build_notebook,
                                  build_kernel_metadata)

STATE_FILENAME = "active_job.json"
ACTIVE_STATES = {"queued", "running"}


class ConcurrentJobError(Exception):
    """A job is already active and the app cannot stop it."""


@dataclass
class JobState:
    submitted_at: str
    kernel_slug: str
    total_frames: int
    state: str = "queued"
    message: str = ""
    frames_done: int | None = None


class RenderJob:
    def __init__(self, cfg: AppConfig, kaggle, drive, template_path: Path,
                 on_change: Callable[[JobState], None] | None = None) -> None:
        self.cfg = cfg
        self.kaggle = kaggle
        self.drive = drive
        self.template_path = Path(template_path)
        self.on_change = on_change

    # --- persistence -------------------------------------------------
    def _state_path(self) -> Path:
        return self.cfg.state_dir() / STATE_FILENAME

    def _write(self, st: JobState) -> None:
        self._state_path().write_text(json.dumps(asdict(st), indent=2),
                                      encoding="utf-8")
        if self.on_change:
            self.on_change(st)

    def active_job(self) -> JobState | None:
        p = self._state_path()
        if not p.exists():
            return None
        return JobState(**json.loads(p.read_text(encoding="utf-8")))

    # --- operations --------------------------------------------------
    def submit(self, blend_folder: Path, settings: RenderSettings) -> JobState:
        existing = self.active_job()
        if existing and existing.state in ACTIVE_STATES:
            raise ConcurrentJobError(
                f"a render is already {existing.state}. Kaggle allows only 2 "
                f"concurrent GPU sessions and this app cannot stop a run -- "
                f"stop it at https://www.kaggle.com/ (Active Events) first."
            )

        build_dir = self.cfg.state_dir() / "build"
        build_notebook(self.template_path, self.cfg, settings, build_dir)
        build_kernel_metadata(self.cfg, build_dir)

        self.kaggle.dataset_version(blend_folder, self.cfg.project_slug,
                                    f"frames {settings.start_frame}-{settings.end_frame}")
        self.kaggle.kernel_push(build_dir)

        st = JobState(
            submitted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            kernel_slug=self.cfg.kernel_slug,
            total_frames=settings.end_frame - settings.start_frame + 1,
            state="queued",
        )
        self._write(st)
        return st

    def poll(self) -> JobState | None:
        st = self.active_job()
        if st is None:
            return None

        status = self.kaggle.kernel_status(st.kernel_slug)
        st.state = status.state
        st.message = status.message
        st.frames_done = self.drive.count_frames(
            self.cfg.drive_output_dir, self.cfg.project_name, ".png")

        self._write(st)
        return st
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_job.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Run the whole suite**

Run: `pytest -v`
Expected: PASS, 28 tests across 5 files

- [ ] **Step 6: Commit**

```bash
git add app/job.py tests/test_job.py
git commit -m "feat: render job orchestration with concurrency guard"
```

---

### Task 6: Notebook reads credentials from the dataset

**Files:**
- Modify: `kaggle_blender_render.ipynb` (the rclone cell)
- Modify: `app/templates/render_template.ipynb` (same change)

**Interfaces:**
- Consumes: nothing
- Produces: notebook that works both standalone (Secret) and app-driven (dataset)

The notebook currently reads `RCLONE_CONF` from `kaggle_secrets`. The app cannot set Secrets, so it must also accept `/kaggle/input/render-creds/rclone.conf`. Dataset first, Secret as fallback, so the notebook keeps working when opened by hand in a browser.

- [ ] **Step 1: Replace the credential-loading block**

Find this in the rclone cell of both notebooks:

```python
try:
    from kaggle_secrets import UserSecretsClient
    conf = UserSecretsClient().get_secret("RCLONE_CONF")
except Exception as e:
    raise SystemExit(...)
```

Replace with:

```python
# Credentials come from one of two places:
#   1. /kaggle/input/render-creds/rclone.conf  -- used by the desktop app,
#      because Kaggle Secrets CANNOT be set through the API (kaggle-api#582)
#   2. the RCLONE_CONF Secret -- used when you open this notebook by hand
CREDS_DATASET = "/kaggle/input/render-creds/rclone.conf"
conf = None

if os.path.exists(CREDS_DATASET):
    conf = open(CREDS_DATASET).read()
    print(f"Using credentials from {CREDS_DATASET}")
else:
    try:
        from kaggle_secrets import UserSecretsClient
        conf = UserSecretsClient().get_secret("RCLONE_CONF")
        print("Using credentials from the RCLONE_CONF secret")
    except Exception as e:
        raise SystemExit(
            "No Drive credentials found. Either:\n"
            f"  - attach the render-creds dataset (provides {CREDS_DATASET}), or\n"
            "  - Add-ons -> Secrets -> add RCLONE_CONF with the output of\n"
            "    `rclone config show gdrive` from your own machine.\n"
            f"Underlying error: {e}"
        )
```

- [ ] **Step 2: Verify both notebooks still parse**

Run:
```bash
python -c "
import json, ast
for p in ['kaggle_blender_render.ipynb', 'app/templates/render_template.ipynb']:
    nb = json.load(open(p, encoding='utf-8'))
    for i, c in enumerate(nb['cells']):
        if c['cell_type'] != 'code':
            continue
        src = ''.join(c['source'])
        body = '\n'.join('pass' if l.startswith('%') else l for l in src.split('\n'))
        ast.parse(body)
    print(p, 'OK')
"
```
Expected: both print `OK`

- [ ] **Step 3: Commit**

```bash
git add kaggle_blender_render.ipynb app/templates/render_template.ipynb
git commit -m "feat: read Drive credentials from dataset with Secret fallback"
```

---

### Task 7: Main window

**Files:**
- Create: `app/ui/__init__.py`
- Create: `app/ui/main_window.py`
- Create: `app/__main__.py`

**Interfaces:**
- Consumes: `AppConfig` (Task 1), `RenderJob`/`JobState`/`ConcurrentJobError` (Task 5), `RenderSettings` (Task 3)
- Produces: `MainWindow(QMainWindow)`, `main() -> int`

No unit tests for the UI: it holds no logic worth asserting, and every branch it can reach is already covered in `job.py`. Verification is the manual checklist in Step 4.

- [ ] **Step 1: Write the window**

```python
# app/ui/__init__.py
```

```python
# app/ui/main_window.py
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QFormLayout, QHBoxLayout,
    QLineEdit, QSpinBox, QComboBox, QPushButton, QLabel, QFileDialog,
    QMessageBox, QProgressBar,
)

from app.config import AppConfig
from app.job import RenderJob, JobState, ConcurrentJobError
from app.notebook_builder import RenderSettings

ACTIVE_EVENTS_URL = "https://www.kaggle.com/discussions"
KAGGLE_SETTINGS_URL = "https://www.kaggle.com/settings"


class MainWindow(QMainWindow):
    def __init__(self, cfg: AppConfig, job: RenderJob) -> None:
        super().__init__()
        self.cfg = cfg
        self.job = job
        self.blend_folder: Path | None = None

        self.setWindowTitle("blendercolab render")
        self.resize(520, 420)

        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)

        layout.addWidget(QLabel(f"<b>Kaggle:</b> {cfg.kaggle_username}"))

        form = QFormLayout()
        self.folder_label = QLabel("<i>none selected</i>")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_folder)
        row = QHBoxLayout()
        row.addWidget(self.folder_label, 1)
        row.addWidget(browse)
        holder = QWidget()
        holder.setLayout(row)
        form.addRow("Project folder", holder)

        self.start = QSpinBox(); self.start.setRange(1, 100000); self.start.setValue(1)
        self.end = QSpinBox(); self.end.setRange(1, 100000); self.end.setValue(1)
        self.res_x = QSpinBox(); self.res_x.setRange(64, 8192); self.res_x.setValue(1920)
        self.res_y = QSpinBox(); self.res_y.setRange(64, 8192); self.res_y.setValue(1080)
        self.samples = QSpinBox(); self.samples.setRange(1, 16384); self.samples.setValue(512)
        self.fmt = QComboBox(); self.fmt.addItems(["PNG", "JPEG"])

        form.addRow("Start frame", self.start)
        form.addRow("End frame", self.end)
        form.addRow("Width", self.res_x)
        form.addRow("Height", self.res_y)
        form.addRow("Samples", self.samples)
        form.addRow("Format", self.fmt)
        layout.addLayout(form)

        self.render_btn = QPushButton("RENDER")
        self.render_btn.clicked.connect(self._submit)
        layout.addWidget(self.render_btn)

        self.status = QLabel("Idle")
        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        layout.addWidget(self.status)
        layout.addWidget(self.bar)

        # There is deliberately NO cancel button: no API can stop a kernel
        # (Kaggle/kaggle-api#388). Send the user to Kaggle instead.
        self.stop_link = QPushButton("Stop a run on Kaggle (opens browser)")
        self.stop_link.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(ACTIVE_EVENTS_URL)))
        layout.addWidget(self.stop_link)

        quota = QLabel(
            f'<a href="{KAGGLE_SETTINGS_URL}">Check your GPU quota on Kaggle</a> '
            "— not available through the API")
        quota.setOpenExternalLinks(True)
        quota.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(quota)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(30_000)
        self._refresh(self.job.active_job())

    def _pick_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Select project folder")
        if chosen:
            self.blend_folder = Path(chosen)
            self.folder_label.setText(self.blend_folder.name)

    def _submit(self) -> None:
        if self.blend_folder is None:
            QMessageBox.warning(self, "No project", "Select a project folder first.")
            return
        if self.end.value() < self.start.value():
            QMessageBox.warning(self, "Bad range", "End frame is before start frame.")
            return

        settings = RenderSettings(
            start_frame=self.start.value(), end_frame=self.end.value(),
            resolution_x=self.res_x.value(), resolution_y=self.res_y.value(),
            samples=self.samples.value(), file_format=self.fmt.currentText())

        self.render_btn.setEnabled(False)
        try:
            self._refresh(self.job.submit(self.blend_folder, settings))
        except ConcurrentJobError as e:
            QMessageBox.warning(self, "Already running", str(e))
        except Exception as e:
            QMessageBox.critical(self, "Submit failed", str(e))
        finally:
            self.render_btn.setEnabled(True)

    def _poll(self) -> None:
        try:
            self._refresh(self.job.poll())
        except Exception as e:
            self.status.setText(f"Status check failed: {e}")

    def _refresh(self, st: JobState | None) -> None:
        if st is None:
            self.status.setText("Idle")
            self.bar.setValue(0)
            return

        if st.frames_done is None:
            self.bar.setFormat("frame count unavailable")
            self.bar.setValue(0)
        else:
            self.bar.setMaximum(max(st.total_frames, 1))
            self.bar.setValue(st.frames_done)
            self.bar.setFormat(f"%v / %m frames")

        text = f"{st.state}  (submitted {st.submitted_at})"
        if st.message:
            text += f"\n{st.message}"
        self.status.setText(text)
```

```python
# app/__main__.py
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QInputDialog

from app.config import AppConfig
from app.kaggle_client import KaggleClient
from app.drive_client import DriveClient
from app.job import RenderJob
from app.ui.main_window import MainWindow

TEMPLATE = Path(__file__).parent / "templates" / "render_template.ipynb"


def main() -> int:
    qt = QApplication(sys.argv)

    cfg = AppConfig.load()
    if cfg is None:
        kaggle = KaggleClient()
        try:
            username = kaggle.whoami()
        except Exception:
            username, ok = QInputDialog.getText(
                None, "Kaggle username",
                "Could not read ~/.kaggle/kaggle.json.\n"
                "Create a token at kaggle.com -> Account -> Create New API Token,\n"
                "then enter your username:")
            if not ok or not username:
                return 1
        project, ok = QInputDialog.getText(
            None, "Project", "Project name (the .blend and dataset name):")
        if not ok or not project:
            return 1
        cfg = AppConfig(kaggle_username=username, project_name=project)
        cfg.save()

    job = RenderJob(cfg, KaggleClient(), DriveClient(), TEMPLATE)
    win = MainWindow(cfg, job)
    win.show()
    return qt.exec()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run the app**

Run: `python -m app`
Expected: window opens, no traceback

- [ ] **Step 3: Run the full test suite (nothing regressed)**

Run: `pytest -v`
Expected: PASS, 28 tests

- [ ] **Step 4: Manual verification checklist**

- [ ] Window opens and shows the Kaggle username
- [ ] "Browse…" selects a folder and the name appears
- [ ] RENDER with no folder selected shows a warning, does not submit
- [ ] End frame < start frame shows a warning, does not submit
- [ ] **No Cancel button exists anywhere in the UI**
- [ ] **No GPU quota number is displayed** — only a link to Kaggle settings
- [ ] "Stop a run on Kaggle" opens a browser

- [ ] **Step 5: Commit**

```bash
git add app/ui/ app/__main__.py
git commit -m "feat: main window with submit and monitoring"
```

---

### Task 8: Package as a Windows executable

**Files:**
- Create: `build.ps1`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: everything above
- Produces: `dist/blendercolab-render.exe`

- [ ] **Step 1: Write the build script**

```powershell
# build.ps1 -- produces dist/blendercolab-render.exe
$ErrorActionPreference = "Stop"

python -m pip install --upgrade pyinstaller

pyinstaller --noconfirm --onefile --windowed `
    --name blendercolab-render `
    --add-data "app/templates/render_template.ipynb;app/templates" `
    app/__main__.py

Write-Host ""
Write-Host "Built dist/blendercolab-render.exe"
Write-Host "Expect roughly 80-150 MB: PySide6 is bundled whole."
```

- [ ] **Step 2: Make the template path work when frozen**

Modify `app/__main__.py`, replacing the `TEMPLATE` line:

```python
import sys
from pathlib import Path

def _template_path() -> Path:
    # PyInstaller unpacks --add-data into sys._MEIPASS at runtime
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent))
    candidate = base / "app" / "templates" / "render_template.ipynb"
    if candidate.exists():
        return candidate
    return Path(__file__).parent / "templates" / "render_template.ipynb"

TEMPLATE = _template_path()
```

- [ ] **Step 3: Ignore build artefacts**

Append to `.gitignore`:

```
build/
dist/
*.spec
__pycache__/
.pytest_cache/
```

- [ ] **Step 4: Build and verify**

Run: `powershell -ExecutionPolicy Bypass -File build.ps1`
Expected: `dist/blendercolab-render.exe` exists

Run: `dist\blendercolab-render.exe`
Expected: the window opens with no console and no traceback

- [ ] **Step 5: Commit**

```bash
git add build.ps1 .gitignore app/__main__.py
git commit -m "feat: package as a single Windows executable"
```

---

## First-run setup (document, do not automate)

These are one-time manual steps. The app assumes they are done.

1. **Kaggle token** — kaggle.com → Account → Create New API Token → save `kaggle.json` to `%USERPROFILE%\.kaggle\`.
2. **rclone remote** — on your own machine, `rclone config` → new remote named `gdrive`, type `drive`, with your own `client_id`/`client_secret` from Google Cloud Console.
3. **Credentials dataset** — one time:
   ```bash
   mkdir render-creds
   rclone config show gdrive > render-creds/rclone.conf
   kaggle datasets init -p render-creds
   # edit render-creds/dataset-metadata.json: set title and id, keep it private
   kaggle datasets create -p render-creds
   ```
4. **Project dataset** — put the `.blend` and its textures in a folder, then
   `kaggle datasets init -p <folder>` and `kaggle datasets create -p <folder>`.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| `kaggle_client.py` | Task 2 |
| `notebook_builder.py` | Task 3 |
| `drive_client.py` | Task 4 |
| `job.py` | Task 5 |
| `ui/` | Task 7 |
| Notebook parameterisation via CONFIG cell | Task 3 |
| `kernel-metadata.json` generation | Task 3 |
| Credentials via private dataset | Task 6 + first-run setup |
| Notebook reads dataset with Secret fallback | Task 6 |
| Progress via `rclone lsf` | Task 4, wired in Task 5 |
| Poll intervals | Task 7 (30 s timer) |
| No Cancel button | Task 7 Step 4 checklist; asserted in Tasks 2 and 5 |
| No quota display | Task 7 Step 4 checklist |
| Concurrency guard | Task 5 |
| Error handling table | Tasks 2, 4, 5, 7 |
| PyInstaller packaging | Task 8 |

**Deviation from spec:** the spec listed a separate `ui/job_view.py`. Folded into
`main_window.py` — v1's status panel is three widgets, and splitting it would create a
file with no independent responsibility. Revisit if the UI grows.

**Simplification:** the spec described backoff from 30 s to 60 s after ten idle minutes.
Task 7 uses a flat 30 s timer. Backoff is worth adding when there is evidence polling
cost matters; adding it now is speculative.

**Type consistency:** `JobState.frames_done` is `int | None` throughout — produced as
`int | None` by `DriveClient.count_frames` (Task 4), stored in Task 5, and rendered with
an explicit `None` branch in Task 7. `KernelStatus.state` is a plain `str` in Tasks 2 and
5, compared against the `ACTIVE_STATES` set defined once in `job.py`.

**Placeholder scan:** no TBDs. Every code step contains runnable code; every test step
contains real assertions.
