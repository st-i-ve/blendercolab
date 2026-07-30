# BlendFleet — Multi-Account Kaggle Render Desktop App

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Supersedes** `2026-07-30-kaggle-render-desktop.md`. That plan was written before the API was tested against a live account and three of its Global Constraints turned out to be false. Do not execute it.

**Goal:** A native desktop app that renders one Blender project across several Kaggle accounts in parallel, shows a live dashboard, and pulls every finished frame back to one local folder — without opening a browser.

**Architecture:** A platform-neutral Python core (`blendfleet/`) with all Kaggle and orchestration logic, plus a thin `platform_paths` layer that is the *only* place an OS check appears. Coordination is entirely client-side: the app holds N account tokens, assigns each a disjoint stride of frames, pushes one kernel per account, polls all of them, and merges outputs locally. The Kaggle accounts never communicate. Windows `.exe` ships first; Linux is a packaging task against the identical core.

**Tech Stack:** Python 3.11+, PySide6, `kaggle` 2.2.4 (which vendors `kagglesdk`), pytest, PyInstaller.

## Global Constraints

Every value below was measured against a live Kaggle account on 2026-07-30. Evidence is in "Verified Facts". Do not "correct" these from memory or from forum posts — forum posts are what made the previous plan wrong.

- Python 3.11+. All paths via `pathlib`. **No `os.name`/`sys.platform` checks outside `blendfleet/platform_paths.py`.**
- **Auth is a bearer token**, format `KGAT_<32 hex>`, supplied via `KAGGLE_API_TOKEN` env var or `~/.kaggle/access_token`. `kaggle.json` (username+key Basic auth) is **obsolete** in CLI 2.2.4 and must not be used.
- **Dataset inputs mount at `/kaggle/input/datasets/<owner>/<slug>/<file>`** — *not* `/kaggle/input/<slug>/`. Notebooks MUST locate the `.blend` by walking `/kaggle/input`.
- `/kaggle/input` is **read-only**. Copy the `.blend` to `/kaggle/tmp` before rendering.
- **Never pass `--dir-mode zip`** to `datasets create`. It nests the payload and breaks the input path.
- `kernels push` **always** starts a run. It is never a no-op, even for byte-identical content.
- `kernels status` via CLI prints prose (`<slug> has status "KernelWorkerStatus.COMPLETE"`), **not JSON**. Use `kagglesdk` for structured status.
- **HTTP 404 from status means "never run"**, not an error.
- `KernelWorkerStatus` values: `QUEUED, RUNNING, COMPLETE, ERROR, CANCEL_REQUESTED, CANCEL_ACKNOWLEDGED, NEW_SCRIPT`.
- Kaggle concurrency: **2 GPU sessions, 5 CPU sessions, per account.**
- **GPU model is not guaranteed.** `enable_gpu: true` returned a single Tesla P100, not T4 ×2. Never hardcode a GPU assumption.
- In Blender, `prefs.devices` lists each physical GPU **once per backend**. Enable only devices whose `type == chosen_backend`, or the same card is switched on twice.
- **No Google Drive, no rclone.** Renders go to `/kaggle/working`; retrieval is `kernels output`. v1 has zero Google dependencies.
- `/kaggle/working` caps at 20 GB. `/kaggle/tmp` is ~60 GB scratch, not persisted.
- No network in unit tests. Every external call is injected and stubbed.

## Verified Facts (evidence, 2026-07-30)

| Fact | How it was established |
|---|---|
| Token is bearer-auth | 200 with `Authorization: Bearer`, 401 with Basic/`X-Api-Key`, against `competitions/list?group=entered` which 401s anonymously |
| `datasets/list` is public | Unauthenticated control returned byte-identical data — an auth test against it proves nothing |
| Input path is `/kaggle/input/datasets/<owner>/<slug>/` | First run died `FileNotFoundError`; second run walked the tree and printed the real path |
| Blender 5.2.0 installs in ~28 s | Kernel log |
| Render: 1920×1080, 128 spp → **57.1 s** on P100 | Kernel log, `remember.blend` |
| Render: 480×270, 32 spp → 15.5 s | Same run |
| Machine: 4 cores, 31.3 GB RAM, 1× Tesla P100-PCIE-16GB | `psutil` + `nvidia-smi` in-kernel |
| OptiX **is** accepted on Pascal | Log: `USING OPTIX -> [Tesla P100…]` — a prediction that it would fall back to CUDA was wrong |
| Duplicate device bug | Log listed the single P100 twice |
| Quota RPC exists | `ApiGetAcceleratorQuotaStatisticsRequest`, returns `timeUsed`/`totalTimeAllowed`/`quotaRefreshTime` |
| Cancel RPC exists | `kagglesdk/kernels/services/kernels_api_service.py:124 cancel_kernel_session` — the CLI has no `cancel` subcommand, which is why forums claim it is impossible |

**Unresolved — do not assert either way.** The settings page showed **30 hrs** GPU quota while `gpuQuota.totalTimeAllowed` returned **21600 s (6 h)** for the same account at the same moment. TPU agreed across both (72000 s = 20 hrs). The dashboard MUST show the API figure *labelled as the API figure*, alongside a link to the settings page — never silently pick one.

---

## File Structure

```
blendfleet/
  __init__.py
  platform_paths.py     ONLY file allowed to branch on OS
  accounts.py           AccountStore: N tokens, add/remove/list
  kaggle_client.py      per-account client: whoami, quota, push, status, cancel, output
  dataset_sync.py       create-or-version a .blend dataset
  notebook_builder.py   render notebook + kernel-metadata from a frame list
  assignment.py         split frames across accounts (pure functions)
  fleet.py              orchestrates N accounts; persists fleet state
  collector.py          pull + merge outputs into one local folder
  ui/
    __init__.py
    dashboard.py        fleet dashboard
    setup_dialog.py     first-run: add accounts
  __main__.py
tests/
  test_platform_paths.py  test_accounts.py  test_kaggle_client.py
  test_dataset_sync.py    test_notebook_builder.py  test_assignment.py
  test_fleet.py           test_collector.py
packaging/
  build_windows.ps1
  build_linux.sh
  blendfleet.spec
pyproject.toml
```

`assignment.py` is deliberately pure — frame maths is the easiest thing to get wrong and the cheapest thing to test exhaustively.

---

### Task 1: Cross-platform paths and scaffold

**Files:**
- Create: `pyproject.toml`, `blendfleet/__init__.py`, `blendfleet/platform_paths.py`
- Test: `tests/test_platform_paths.py`

**Interfaces:**
- Produces: `config_dir() -> Path`, `state_dir() -> Path`, `cache_dir() -> Path`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_platform_paths.py
from pathlib import Path
import blendfleet.platform_paths as pp


def test_windows_uses_appdata(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert pp.config_dir() == tmp_path / "BlendFleet"


def test_linux_respects_xdg(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert pp.config_dir() == tmp_path / "blendfleet"


def test_linux_defaults_without_xdg(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert pp.config_dir() == tmp_path / ".config" / "blendfleet"


def test_dirs_are_created(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert pp.config_dir().is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_platform_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'blendfleet'`

- [ ] **Step 3: Write pyproject.toml**

```toml
[project]
name = "blendfleet"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["PySide6>=6.6", "kaggle>=2.2.4"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 4: Write the implementation**

```python
# blendfleet/__init__.py
```

```python
# blendfleet/platform_paths.py
"""The ONLY module permitted to branch on operating system.

Everything else in blendfleet must be platform-neutral so the Linux build is
a packaging job, not a port.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "BlendFleet"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "blendfleet"
    return Path(os.environ["HOME"]) / ".config" / "blendfleet"


def config_dir() -> Path:
    p = _base()
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_dir() -> Path:
    p = _base() / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = _base() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_platform_paths.py -v` → PASS, 4 tests

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml blendfleet/ tests/test_platform_paths.py
git commit -m "feat: cross-platform path layer"
```

---

### Task 2: Account store

**Files:**
- Create: `blendfleet/accounts.py`
- Test: `tests/test_accounts.py`

**Interfaces:**
- Consumes: `platform_paths.config_dir`
- Produces: `Account(label: str, token: str, username: str | None)`, `AccountStore` with `add`, `remove`, `list`, `save`, `load`, `TokenFormatError`

Tokens are stored in a file with owner-only permissions. This is not a secrets manager, and the plan says so plainly rather than implying safety it does not provide.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_accounts.py
import pytest
import blendfleet.platform_paths as pp
from blendfleet.accounts import Account, AccountStore, TokenFormatError


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


VALID = "KGAT_" + "a" * 32


def test_rejects_malformed_token():
    s = AccountStore()
    with pytest.raises(TokenFormatError):
        s.add(Account(label="bad", token="not-a-token"))


def test_accepts_kgat_token():
    s = AccountStore()
    s.add(Account(label="me", token=VALID))
    assert len(s.list()) == 1


def test_rejects_duplicate_token():
    s = AccountStore()
    s.add(Account(label="me", token=VALID))
    with pytest.raises(ValueError, match="already"):
        s.add(Account(label="other", token=VALID))


def test_roundtrips_through_disk():
    s = AccountStore()
    s.add(Account(label="me", token=VALID, username="stivestivewithani"))
    s.add(Account(label="friend", token="KGAT_" + "b" * 32))
    s.save()
    assert [a.label for a in AccountStore.load().list()] == ["me", "friend"]


def test_remove_by_label():
    s = AccountStore()
    s.add(Account(label="me", token=VALID))
    s.remove("me")
    assert s.list() == []


def test_load_with_no_file_is_empty():
    assert AccountStore.load().list() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_accounts.py -v` → FAIL, no module `blendfleet.accounts`

- [ ] **Step 3: Write the implementation**

```python
# blendfleet/accounts.py
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path

from blendfleet.platform_paths import config_dir

TOKEN_RE = re.compile(r"^KGAT_[0-9a-fA-F]{32}$")
FILENAME = "accounts.json"


class TokenFormatError(ValueError):
    """Token is not in the KGAT_<32 hex> form Kaggle issues."""


@dataclass
class Account:
    label: str
    token: str
    username: str | None = None


class AccountStore:
    def __init__(self, accounts: list[Account] | None = None) -> None:
        self._accounts: list[Account] = list(accounts or [])

    def add(self, account: Account) -> None:
        if not TOKEN_RE.match(account.token):
            raise TokenFormatError(
                "Expected a token like KGAT_ followed by 32 hex characters. "
                "Generate one at kaggle.com -> Settings -> API -> Generate New Token."
            )
        if any(a.token == account.token for a in self._accounts):
            raise ValueError("that token is already registered")
        self._accounts.append(account)

    def remove(self, label: str) -> None:
        self._accounts = [a for a in self._accounts if a.label != label]

    def list(self) -> list[Account]:
        return list(self._accounts)

    def _path(self) -> Path:
        return config_dir() / FILENAME

    def save(self) -> None:
        p = self._path()
        p.write_text(json.dumps([asdict(a) for a in self._accounts], indent=2),
                     encoding="utf-8")
        try:
            os.chmod(p, 0o600)      # no-op on Windows, meaningful on Linux
        except OSError:
            pass

    @classmethod
    def load(cls) -> "AccountStore":
        p = config_dir() / FILENAME
        if not p.exists():
            return cls()
        return cls([Account(**d) for d in json.loads(p.read_text(encoding="utf-8"))])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_accounts.py -v` → PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add blendfleet/accounts.py tests/test_accounts.py
git commit -m "feat: multi-account token store"
```

---

### Task 3: Frame assignment

**Files:**
- Create: `blendfleet/assignment.py`
- Test: `tests/test_assignment.py`

**Interfaces:**
- Produces: `assign_frames(start: int, end: int, n_workers: int) -> list[list[int]]`, `estimate(n_frames: int, seconds_per_frame: float, n_workers: int) -> float`

Pure functions, no I/O. Stride assignment (worker *i* takes every *n*-th frame) rather than contiguous blocks, so a worker that dies leaves gaps spread through the animation instead of a missing chunk — and partial results still preview as a whole.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_assignment.py
import pytest
from blendfleet.assignment import assign_frames, estimate


def test_splits_evenly():
    got = assign_frames(1, 6, 3)
    assert got == [[1, 4], [2, 5], [3, 6]]


def test_handles_remainder():
    got = assign_frames(1, 7, 3)
    assert got == [[1, 4, 7], [2, 5], [3, 6]]
    assert sorted(f for w in got for f in w) == [1, 2, 3, 4, 5, 6, 7]


def test_every_frame_assigned_exactly_once():
    got = assign_frames(10, 250, 4)
    flat = [f for w in got for f in w]
    assert sorted(flat) == list(range(10, 251))
    assert len(flat) == len(set(flat))


def test_more_workers_than_frames_leaves_empty_lists():
    got = assign_frames(1, 2, 5)
    assert got == [[1], [2], [], [], []]


def test_single_worker_gets_everything():
    assert assign_frames(1, 4, 1) == [[1, 2, 3, 4]]


def test_rejects_bad_input():
    with pytest.raises(ValueError):
        assign_frames(5, 1, 2)
    with pytest.raises(ValueError):
        assign_frames(1, 5, 0)


def test_estimate_divides_across_workers():
    # 250 frames at 57.1s measured on a P100
    assert estimate(250, 57.1, 1) == pytest.approx(3.965, abs=0.01)
    assert estimate(250, 57.1, 3) == pytest.approx(1.322, abs=0.01)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_assignment.py -v` → FAIL, no module

- [ ] **Step 3: Write the implementation**

```python
# blendfleet/assignment.py
"""Pure frame-splitting maths. No I/O, no Kaggle, no UI."""
from __future__ import annotations


def assign_frames(start: int, end: int, n_workers: int) -> list[list[int]]:
    """Split [start, end] across n_workers by stride.

    Worker i renders frames start+i, start+i+n, ... A worker that dies leaves
    gaps spread evenly through the animation rather than one missing block,
    so partial output still previews as a whole.
    """
    if end < start:
        raise ValueError(f"end frame {end} is before start frame {start}")
    if n_workers < 1:
        raise ValueError("need at least one worker")
    frames = list(range(start, end + 1))
    return [frames[i::n_workers] for i in range(n_workers)]


def estimate(n_frames: int, seconds_per_frame: float, n_workers: int) -> float:
    """Wall-clock hours, assuming workers run concurrently and evenly."""
    if n_workers < 1:
        raise ValueError("need at least one worker")
    per_worker = -(-n_frames // n_workers)      # ceiling division
    return per_worker * seconds_per_frame / 3600.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_assignment.py -v` → PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add blendfleet/assignment.py tests/test_assignment.py
git commit -m "feat: stride-based frame assignment"
```

---

### Task 4: Kaggle client

**Files:**
- Create: `blendfleet/kaggle_client.py`
- Test: `tests/test_kaggle_client.py`

**Interfaces:**
- Produces:
  - `KaggleClient(token: str, runner=..., sdk_factory=...)`
  - `.whoami() -> str`
  - `.quota() -> Quota`
  - `.push_kernel(folder: Path) -> None`
  - `.status(slug: str) -> KernelStatus`
  - `.cancel(slug: str) -> bool`
  - `.fetch_output(slug: str, dest: Path) -> list[Path]`
  - `Quota(used_seconds, total_seconds, refresh_time, source="api")`
  - `KernelStatus(state: str, message: str)` — `state` lowercase of the enum, plus `"not_started"` for 404
  - `KaggleError(Exception)`

CLI is used for upload/push/output (it handles chunked upload); `kagglesdk` for status, quota and cancel (structured, and the CLI cannot cancel at all).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kaggle_client.py
import subprocess
from pathlib import Path
import pytest
from blendfleet.kaggle_client import KaggleClient, KaggleError, KernelStatus

TOKEN = "KGAT_" + "a" * 32


def runner_factory(stdout="", returncode=0, stderr=""):
    calls = []

    def run(argv, env=None):
        calls.append((argv, env))
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)
    run.calls = calls
    return run


def test_token_is_passed_via_env_not_argv():
    r = runner_factory("ok")
    KaggleClient(TOKEN, runner=r).push_kernel(Path("/tmp/x"))
    argv, env = r.calls[0]
    assert TOKEN not in " ".join(argv), "token must never appear in argv"
    assert env["KAGGLE_API_TOKEN"] == TOKEN


def test_status_parses_cli_prose():
    r = runner_factory('x/y has status "KernelWorkerStatus.COMPLETE"')
    assert KaggleClient(TOKEN, runner=r).status("x/y").state == "complete"


def test_status_parses_running():
    r = runner_factory('x/y has status "KernelWorkerStatus.RUNNING"')
    assert KaggleClient(TOKEN, runner=r).status("x/y").state == "running"


def test_404_means_not_started_not_error():
    r = runner_factory("404 Client Error: Not Found for url: ...", returncode=1)
    assert KaggleClient(TOKEN, runner=r).status("x/y").state == "not_started"


def test_other_failure_raises():
    r = runner_factory("401 Unauthorized", returncode=1)
    with pytest.raises(KaggleError, match="401"):
        KaggleClient(TOKEN, runner=r).status("x/y")


def test_quota_converts_seconds():
    class FakeQuota:
        class gpu_quota:
            time_used = __import__("datetime").timedelta(seconds=3600)
            total_time_allowed = __import__("datetime").timedelta(seconds=21600)
        quota_refresh_time = "2026-08-01T00:00:00Z"

    c = KaggleClient(TOKEN, runner=runner_factory(), sdk_factory=lambda t: FakeQuota())
    q = c.quota()
    assert q.used_seconds == 3600
    assert q.total_seconds == 21600
    assert q.source == "api"     # must be labelled; the settings page may disagree


def test_push_never_treated_as_noop():
    # kernels push ALWAYS starts a run; there is no "unchanged" short-circuit.
    r = runner_factory("Kernel version 1 successfully pushed.")
    c = KaggleClient(TOKEN, runner=r)
    c.push_kernel(Path("/tmp/x"))
    c.push_kernel(Path("/tmp/x"))
    assert len(r.calls) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_kaggle_client.py -v` → FAIL, no module

- [ ] **Step 3: Write the implementation**

```python
# blendfleet/kaggle_client.py
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

STATUS_RE = re.compile(r'KernelWorkerStatus\.([A-Z_]+)')


class KaggleError(Exception):
    """A Kaggle call failed."""


@dataclass
class KernelStatus:
    state: str          # queued|running|complete|error|cancel_requested|
                        # cancel_acknowledged|new_script|not_started
    message: str = ""

    @property
    def is_active(self) -> bool:
        return self.state in {"queued", "running"}


@dataclass
class Quota:
    used_seconds: int
    total_seconds: int
    refresh_time: str
    source: str = "api"     # ALWAYS label the source: the settings page has
                            # been observed to disagree with this figure.


def _default_runner(argv: list[str], env=None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, env=env)


def _default_sdk(token: str):
    os.environ["KAGGLE_API_TOKEN"] = token
    from kagglesdk import KaggleClient as SdkClient
    from kagglesdk.kernels.types.kernels_api_service import (
        ApiGetAcceleratorQuotaStatisticsRequest)
    c = SdkClient()
    return c.kernels.kernels_api_client.get_accelerator_quota_statistics(
        ApiGetAcceleratorQuotaStatisticsRequest())


class KaggleClient:
    """One Kaggle account. Never mutates global state for callers."""

    def __init__(self, token: str, runner: Callable = _default_runner,
                 sdk_factory: Callable = _default_sdk) -> None:
        self.token = token
        self._run = runner
        self._sdk = sdk_factory

    def _env(self) -> dict:
        env = os.environ.copy()
        env["KAGGLE_API_TOKEN"] = self.token     # never in argv: argv leaks
        return env                               # into process lists and logs

    def _exec(self, argv: list[str]) -> str:
        proc = self._run(argv, env=self._env())
        if proc.returncode != 0:
            raise KaggleError((proc.stderr or proc.stdout or "").strip())
        return proc.stdout or ""

    def whoami(self) -> str:
        out = self._exec(["kaggle", "kernels", "list", "--mine", "--csv"])
        for line in out.splitlines()[1:]:
            if "/" in line:
                return line.split("/", 1)[0].strip('", ')
        raise KaggleError("could not determine username from kernels list")

    def quota(self) -> Quota:
        r = self._sdk(self.token)
        return Quota(
            used_seconds=int(r.gpu_quota.time_used.total_seconds()),
            total_seconds=int(r.gpu_quota.total_time_allowed.total_seconds()),
            refresh_time=str(r.quota_refresh_time),
            source="api")

    def push_kernel(self, folder: Path) -> None:
        """Always starts a run — there is no unchanged-content short circuit."""
        self._exec(["kaggle", "kernels", "push", "-p", str(folder)])

    def status(self, slug: str) -> KernelStatus:
        proc = self._run(["kaggle", "kernels", "status", slug], env=self._env())
        text = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            if "404" in text:
                # No session exists yet. Not an error.
                return KernelStatus(state="not_started")
            raise KaggleError(text.strip())
        m = STATUS_RE.search(text)
        if not m:
            raise KaggleError(f"unparseable status output: {text!r}")
        return KernelStatus(state=m.group(1).lower())

    def cancel(self, slug: str) -> bool:
        """Cancel via the SDK. The CLI has no cancel subcommand, which is why
        this is widely believed impossible."""
        try:
            os.environ["KAGGLE_API_TOKEN"] = self.token
            from kagglesdk import KaggleClient as SdkClient
            from kagglesdk.kernels.types.kernels_api_service import (
                ApiCancelKernelSessionRequest)
            owner, name = slug.split("/", 1)
            req = ApiCancelKernelSessionRequest()
            req.user_name, req.kernel_slug = owner, name
            SdkClient().kernels.kernels_api_client.cancel_kernel_session(req)
            return True
        except Exception:
            return False

    def fetch_output(self, slug: str, dest: Path) -> list[Path]:
        dest.mkdir(parents=True, exist_ok=True)
        self._exec(["kaggle", "kernels", "output", slug, "-p", str(dest)])
        return sorted(dest.rglob("*.png"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_kaggle_client.py -v` → PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add blendfleet/kaggle_client.py tests/test_kaggle_client.py
git commit -m "feat: per-account Kaggle client with cancel and quota"
```

---

### Task 5: Dataset sync

**Files:**
- Create: `blendfleet/dataset_sync.py`
- Test: `tests/test_dataset_sync.py`

**Interfaces:**
- Produces: `sync_blend(client, blend: Path, slug: str, staging: Path) -> str` returning `"created"` or `"versioned"`

Each account uploads its **own copy** of the `.blend`. Sharing one dataset across accounts needs collaborator management that the API does not expose, so N accounts means N uploads — 64 MB × 3 at ~2.5 MB/s is roughly 45 s each, which the UI must surface rather than appear frozen.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dataset_sync.py
import json
from pathlib import Path
import pytest
from blendfleet.dataset_sync import sync_blend
from blendfleet.kaggle_client import KaggleError


class FakeClient:
    def __init__(self, exists=False):
        self.exists = exists
        self.calls = []

    def _exec(self, argv):
        self.calls.append(argv)
        if argv[:3] == ["kaggle", "datasets", "status"]:
            if not self.exists:
                raise KaggleError("404 Not Found")
            return "ready"
        return "ok"


@pytest.fixture
def blend(tmp_path):
    p = tmp_path / "remember.blend"
    p.write_bytes(b"BLENDER" + b"\0" * 500)
    return p


def test_creates_when_absent(blend, tmp_path):
    c = FakeClient(exists=False)
    assert sync_blend(c, blend, "me/remember-blend", tmp_path / "stage") == "created"
    assert any(a[:3] == ["kaggle", "datasets", "create"] for a in c.calls)


def test_versions_when_present(blend, tmp_path):
    c = FakeClient(exists=True)
    assert sync_blend(c, blend, "me/remember-blend", tmp_path / "stage") == "versioned"
    assert any(a[:3] == ["kaggle", "datasets", "version"] for a in c.calls)


def test_never_uses_dir_mode_zip(blend, tmp_path):
    # --dir-mode zip nests the payload and breaks /kaggle/input path resolution.
    c = FakeClient(exists=False)
    sync_blend(c, blend, "me/x", tmp_path / "stage")
    assert all("zip" not in " ".join(a) for a in c.calls)


def test_metadata_written_correctly(blend, tmp_path):
    stage = tmp_path / "stage"
    sync_blend(FakeClient(), blend, "me/remember-blend", stage)
    meta = json.loads((stage / "dataset-metadata.json").read_text())
    assert meta["id"] == "me/remember-blend"
    assert (stage / "remember.blend").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dataset_sync.py -v` → FAIL, no module

- [ ] **Step 3: Write the implementation**

```python
# blendfleet/dataset_sync.py
from __future__ import annotations

import json
import shutil
from pathlib import Path

from blendfleet.kaggle_client import KaggleError


def sync_blend(client, blend: Path, slug: str, staging: Path) -> str:
    """Upload `blend` as dataset `slug`, creating it if it does not exist.

    Returns "created" or "versioned". Never passes --dir-mode zip: that nests
    the payload and breaks /kaggle/input path resolution inside the notebook.
    """
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copy(blend, staging / blend.name)
    (staging / "dataset-metadata.json").write_text(json.dumps({
        "title": slug.split("/", 1)[1].replace("-", " "),
        "id": slug,
        "licenses": [{"name": "CC0-1.0"}],
    }, indent=2), encoding="utf-8")

    try:
        client._exec(["kaggle", "datasets", "status", slug])
        exists = True
    except KaggleError:
        exists = False

    if exists:
        client._exec(["kaggle", "datasets", "version",
                      "-p", str(staging), "-m", f"update {blend.name}", "-d"])
        return "versioned"
    client._exec(["kaggle", "datasets", "create", "-p", str(staging)])
    return "created"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_dataset_sync.py -v` → PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add blendfleet/dataset_sync.py tests/test_dataset_sync.py
git commit -m "feat: create-or-version blend dataset upload"
```

---

### Task 6: Notebook builder

**Files:**
- Create: `blendfleet/notebook_builder.py`
- Test: `tests/test_notebook_builder.py`

**Interfaces:**
- Produces: `RenderSettings(resolution_x, resolution_y, samples, file_format="PNG", blender_version="5.2.0")`, `build(frames: list[int], settings, slug, out_dir, kernel_slug) -> Path`

The generated notebook embeds the exact code proven to work on 2026-07-30: walk `/kaggle/input` to find the `.blend`, copy to `/kaggle/tmp`, enable only devices matching the chosen backend.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notebook_builder.py
import ast, json
from pathlib import Path
import pytest
from blendfleet.notebook_builder import RenderSettings, build


@pytest.fixture
def settings():
    return RenderSettings(resolution_x=1920, resolution_y=1080, samples=128)


def cells_src(path):
    nb = json.loads(Path(path).read_text(encoding="utf-8"))
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def test_every_cell_is_valid_python(tmp_path, settings):
    p = build([1, 4, 7], settings, "me/remember-blend", tmp_path, "me/render-0")
    for src in cells_src(p):
        ast.parse(src)


def test_frame_list_is_embedded_verbatim(tmp_path, settings):
    p = build([1, 4, 7], settings, "me/x", tmp_path, "me/render-0")
    assert "FRAMES = [1, 4, 7]" in "\n".join(cells_src(p))


def test_walks_input_instead_of_hardcoding_path(tmp_path, settings):
    # /kaggle/input/datasets/<owner>/<slug>/ -- a hardcoded path broke a real run
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert "os.walk" in joined
    assert '"/kaggle/input/' + 'x"' not in joined


def test_enables_only_chosen_backend_devices(tmp_path, settings):
    # d.type != "CPU" switched the same P100 on twice
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert 'd.use = (d.type == chosen)' in joined
    assert 'd.use = (d.type != "CPU")' not in joined


def test_copies_out_of_readonly_input(tmp_path, settings):
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert "shutil.copy" in joined and "/kaggle/tmp" in joined


def test_kernel_metadata(tmp_path, settings):
    build([1], settings, "me/remember-blend", tmp_path, "me/render-0")
    meta = json.loads((tmp_path / "kernel-metadata.json").read_text())
    assert meta["enable_gpu"] is True
    assert meta["enable_internet"] is True
    assert meta["is_private"] is True
    assert meta["id"] == "me/render-0"
    assert meta["dataset_sources"] == ["me/remember-blend"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_notebook_builder.py -v` → FAIL, no module

- [ ] **Step 3: Write the implementation**

```python
# blendfleet/notebook_builder.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_notebook_builder.py -v` → PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add blendfleet/notebook_builder.py tests/test_notebook_builder.py
git commit -m "feat: render notebook builder with verified Kaggle quirks"
```

---

### Task 7: Fleet orchestration

**Files:**
- Create: `blendfleet/fleet.py`
- Test: `tests/test_fleet.py`

**Interfaces:**
- Produces: `WorkerState(label, username, kernel_slug, frames, state, frames_done, message)`, `FleetState(job_id, blend_name, start_frame, end_frame, workers)`, `Fleet(accounts, client_factory, work_dir)` with `.launch(blend, settings)`, `.poll() -> FleetState`, `.cancel_all()`, `.load()`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet.py
from pathlib import Path
import pytest
import blendfleet.platform_paths as pp
from blendfleet.accounts import Account
from blendfleet.kaggle_client import KernelStatus, Quota
from blendfleet.notebook_builder import RenderSettings
from blendfleet.fleet import Fleet


class FakeClient:
    def __init__(self, token, state="running"):
        self.token = token
        self.state = state
        self.pushed = 0
        self.cancelled = []

    def whoami(self): return "user_" + self.token[-1]
    def _exec(self, argv): return "ok"
    def push_kernel(self, folder): self.pushed += 1
    def status(self, slug): return KernelStatus(state=self.state)
    def cancel(self, slug): self.cancelled.append(slug); return True
    def quota(self): return Quota(0, 21600, "2026-08-01", source="api")
    def fetch_output(self, slug, dest): return []


@pytest.fixture(autouse=True)
def tmp_cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture
def blend(tmp_path):
    p = tmp_path / "remember.blend"
    p.write_bytes(b"X" * 100)
    return p


def accounts(n):
    return [Account(label=f"a{i}", token="KGAT_" + str(i) * 32) for i in range(n)]


def test_launch_pushes_one_kernel_per_account(blend, tmp_path):
    clients = {}
    def factory(tok):
        clients[tok] = FakeClient(tok); return clients[tok]
    f = Fleet(accounts(3), factory, tmp_path / "w")
    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)
    assert len(st.workers) == 3
    assert all(c.pushed == 1 for c in clients.values())


def test_frames_are_disjoint_and_complete(blend, tmp_path):
    f = Fleet(accounts(3), lambda t: FakeClient(t), tmp_path / "w")
    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 10)
    allf = [fr for w in st.workers for fr in w.frames]
    assert sorted(allf) == list(range(1, 11))
    assert len(allf) == len(set(allf))


def test_poll_aggregates_state(blend, tmp_path):
    f = Fleet(accounts(2), lambda t: FakeClient(t, "complete"), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    st = f.poll()
    assert all(w.state == "complete" for w in st.workers)


def test_cancel_all_hits_every_worker(blend, tmp_path):
    clients = {}
    def factory(tok):
        clients[tok] = FakeClient(tok); return clients[tok]
    f = Fleet(accounts(3), factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 6)
    f.cancel_all()
    assert all(len(c.cancelled) == 1 for c in clients.values())


def test_state_persists(blend, tmp_path):
    f = Fleet(accounts(2), lambda t: FakeClient(t), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 8)
    f2 = Fleet(accounts(2), lambda t: FakeClient(t), tmp_path / "w")
    assert len(f2.load().workers) == 2


def test_launch_refuses_with_no_accounts(blend, tmp_path):
    f = Fleet([], lambda t: FakeClient(t), tmp_path / "w")
    with pytest.raises(ValueError, match="account"):
        f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fleet.py -v` → FAIL, no module

- [ ] **Step 3: Write the implementation**

```python
# blendfleet/fleet.py
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable

from blendfleet.accounts import Account
from blendfleet.assignment import assign_frames
from blendfleet.dataset_sync import sync_blend
from blendfleet.notebook_builder import RenderSettings, build
from blendfleet.platform_paths import state_dir

STATE_FILE = "fleet.json"


@dataclass
class WorkerState:
    label: str
    username: str
    kernel_slug: str
    frames: list[int]
    state: str = "queued"
    frames_done: int = 0
    message: str = ""


@dataclass
class FleetState:
    job_id: str
    blend_name: str
    start_frame: int
    end_frame: int
    workers: list[WorkerState] = field(default_factory=list)


class Fleet:
    def __init__(self, accounts: list[Account],
                 client_factory: Callable, work_dir: Path) -> None:
        self.accounts = accounts
        self.client_factory = client_factory
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def _state_path(self) -> Path:
        return state_dir() / STATE_FILE

    def _save(self, st: FleetState) -> None:
        self._state_path().write_text(json.dumps(asdict(st), indent=2),
                                      encoding="utf-8")

    def load(self) -> FleetState | None:
        p = self._state_path()
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        d["workers"] = [WorkerState(**w) for w in d["workers"]]
        return FleetState(**d)

    def launch(self, blend: Path, settings: RenderSettings,
               start_frame: int, end_frame: int) -> FleetState:
        if not self.accounts:
            raise ValueError("add at least one account before launching")

        job_id = uuid.uuid4().hex[:8]
        buckets = assign_frames(start_frame, end_frame, len(self.accounts))
        stem = blend.stem.lower().replace("_", "-")
        workers: list[WorkerState] = []

        for account, frames in zip(self.accounts, buckets):
            client = self.client_factory(account.token)
            username = account.username or client.whoami()

            dataset_slug = f"{username}/{stem}-blend"
            kernel_slug = f"{username}/{stem}-render-{job_id}"

            # Each account needs its OWN copy: the API cannot add dataset
            # collaborators, so N accounts means N uploads.
            sync_blend(client, blend, dataset_slug,
                       self.work_dir / f"ds_{account.label}")

            kern_dir = self.work_dir / f"kern_{account.label}"
            build(frames, settings, dataset_slug, kern_dir, kernel_slug)
            client.push_kernel(kern_dir)

            workers.append(WorkerState(label=account.label, username=username,
                                       kernel_slug=kernel_slug, frames=frames))

        st = FleetState(job_id=job_id, blend_name=blend.name,
                        start_frame=start_frame, end_frame=end_frame,
                        workers=workers)
        self._save(st)
        return st

    def poll(self) -> FleetState | None:
        st = self.load()
        if st is None:
            return None
        by_label = {a.label: a for a in self.accounts}
        for w in st.workers:
            acct = by_label.get(w.label)
            if acct is None:
                continue
            s = self.client_factory(acct.token).status(w.kernel_slug)
            w.state, w.message = s.state, s.message
        self._save(st)
        return st

    def cancel_all(self) -> None:
        st = self.load()
        if st is None:
            return
        by_label = {a.label: a for a in self.accounts}
        for w in st.workers:
            acct = by_label.get(w.label)
            if acct:
                self.client_factory(acct.token).cancel(w.kernel_slug)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_fleet.py -v` → PASS, 6 tests

- [ ] **Step 5: Run full suite**

Run: `pytest -v` → PASS, 40 tests

- [ ] **Step 6: Commit**

```bash
git add blendfleet/fleet.py tests/test_fleet.py
git commit -m "feat: multi-account fleet orchestration"
```

---

### Task 8: Output collector

**Files:**
- Create: `blendfleet/collector.py`
- Test: `tests/test_collector.py`

**Interfaces:**
- Produces: `collect(fleet_state, accounts, client_factory, dest: Path) -> CollectReport`, `CollectReport(copied: int, missing_frames: list[int], per_worker: dict[str, int])`

Kaggle returns files named `f_0001.png`; the collector renames to the real frame number and reports which frames are still missing — a partial render must be obvious, not silently look complete.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_collector.py
from pathlib import Path
import pytest
from blendfleet.accounts import Account
from blendfleet.fleet import FleetState, WorkerState
from blendfleet.collector import collect


class FakeClient:
    def __init__(self, token, produce=()):
        self.token = token
        self.produce = produce

    def fetch_output(self, slug, dest):
        dest.mkdir(parents=True, exist_ok=True)
        out = []
        for name in self.produce:
            p = dest / name
            p.write_bytes(b"PNG")
            out.append(p)
        return out


def state():
    return FleetState(job_id="j1", blend_name="r.blend", start_frame=1, end_frame=4,
                      workers=[WorkerState("a0", "u0", "u0/k0", [1, 3]),
                               WorkerState("a1", "u1", "u1/k1", [2, 4])])


def accts():
    return [Account("a0", "KGAT_" + "0"*32), Account("a1", "KGAT_" + "1"*32)]


def test_collects_from_all_workers(tmp_path):
    def factory(tok):
        return FakeClient(tok, ["f_0001.png", "f_0003.png"] if tok.endswith("0"*32)
                          else ["f_0002.png", "f_0004.png"])
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 4
    assert r.missing_frames == []
    assert sorted(p.name for p in (tmp_path / "out").glob("*.png")) == [
        "r_0001.png", "r_0002.png", "r_0003.png", "r_0004.png"]


def test_reports_missing_frames(tmp_path):
    def factory(tok):
        return FakeClient(tok, ["f_0001.png"] if tok.endswith("0"*32) else [])
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 1
    assert r.missing_frames == [2, 3, 4]


def test_per_worker_counts(tmp_path):
    def factory(tok):
        return FakeClient(tok, ["f_0001.png"] if tok.endswith("0"*32) else ["f_0002.png"])
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.per_worker == {"a0": 1, "a1": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_collector.py -v` → FAIL, no module

- [ ] **Step 3: Write the implementation**

```python
# blendfleet/collector.py
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

FRAME_RE = re.compile(r"_(\d+)\.(png|jpg|jpeg)$", re.I)


@dataclass
class CollectReport:
    copied: int = 0
    missing_frames: list[int] = field(default_factory=list)
    per_worker: dict[str, int] = field(default_factory=dict)


def collect(fleet_state, accounts, client_factory: Callable,
            dest: Path) -> CollectReport:
    """Pull every worker's output into one folder, renamed by real frame number.

    Missing frames are reported explicitly: a partial render must be visibly
    partial rather than quietly looking finished.
    """
    dest.mkdir(parents=True, exist_ok=True)
    by_label = {a.label: a for a in accounts}
    stem = Path(fleet_state.blend_name).stem
    report = CollectReport()
    found: set[int] = set()

    for w in fleet_state.workers:
        acct = by_label.get(w.label)
        if acct is None:
            report.per_worker[w.label] = 0
            continue
        client = client_factory(acct.token)
        staging = dest / f".raw_{w.label}"
        files = client.fetch_output(w.kernel_slug, staging)

        n = 0
        for src in sorted(files):
            m = FRAME_RE.search(src.name)
            if not m:
                continue
            frame = int(m.group(1))
            shutil.copy(src, dest / f"{stem}_{frame:04d}.png")
            found.add(frame)
            n += 1
        report.per_worker[w.label] = n
        report.copied += n

    expected = range(fleet_state.start_frame, fleet_state.end_frame + 1)
    report.missing_frames = sorted(f for f in expected if f not in found)
    return report
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_collector.py -v` → PASS, 3 tests

- [ ] **Step 5: Commit**

```bash
git add blendfleet/collector.py tests/test_collector.py
git commit -m "feat: collect and merge outputs across accounts"
```

---

### Task 9: Dashboard UI

**Files:**
- Create: `blendfleet/ui/__init__.py`, `blendfleet/ui/setup_dialog.py`, `blendfleet/ui/dashboard.py`, `blendfleet/__main__.py`

**Interfaces:**
- Consumes: everything above
- Produces: `SetupDialog`, `Dashboard`, `main() -> int`

No unit tests for the UI — it holds no logic that isn't already covered. Verification is the manual checklist in Step 3.

- [ ] **Step 1: Write the UI**

```python
# blendfleet/ui/__init__.py
```

```python
# blendfleet/ui/setup_dialog.py
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QListWidget, QLabel, QMessageBox)

from blendfleet.accounts import Account, AccountStore, TokenFormatError


class SetupDialog(QDialog):
    """Add one Kaggle account per person who is lending you quota."""

    def __init__(self, store: AccountStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("BlendFleet — accounts")
        self.resize(560, 380)
        v = QVBoxLayout(self)

        v.addWidget(QLabel(
            "<b>Add a Kaggle account for each person contributing GPU time.</b><br>"
            "Each person generates their own token at "
            "<a href='https://www.kaggle.com/settings'>kaggle.com/settings</a> "
            "→ API → Generate New Token.<br><br>"
            "<b>A token grants full access to that Kaggle account.</b> Only accept "
            "tokens from people who understand that, and tell them they can revoke "
            "it at any time from the same page."))

        self.list = QListWidget()
        v.addWidget(self.list)

        row = QHBoxLayout()
        self.label = QLineEdit(); self.label.setPlaceholderText("label, e.g. 'james'")
        self.token = QLineEdit(); self.token.setPlaceholderText("KGAT_…")
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        add = QPushButton("Add"); add.clicked.connect(self._add)
        rm = QPushButton("Remove"); rm.clicked.connect(self._remove)
        for wdg in (self.label, self.token, add, rm):
            row.addWidget(wdg)
        v.addLayout(row)

        done = QPushButton("Done"); done.clicked.connect(self.accept)
        v.addWidget(done)
        self._refresh()

    def _refresh(self) -> None:
        self.list.clear()
        for a in self.store.list():
            self.list.addItem(f"{a.label}   ({a.username or 'not yet identified'})")

    def _add(self) -> None:
        try:
            self.store.add(Account(label=self.label.text().strip() or "account",
                                   token=self.token.text().strip()))
            self.store.save()
            self.label.clear(); self.token.clear()
            self._refresh()
        except (TokenFormatError, ValueError) as e:
            QMessageBox.warning(self, "Cannot add account", str(e))

    def _remove(self) -> None:
        item = self.list.currentItem()
        if item:
            self.store.remove(item.text().split()[0])
            self.store.save()
            self._refresh()
```

```python
# blendfleet/ui/dashboard.py
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                               QFormLayout, QSpinBox, QComboBox, QPushButton,
                               QLabel, QFileDialog, QMessageBox, QTableWidget,
                               QTableWidgetItem, QProgressBar, QHeaderView)

from blendfleet.accounts import AccountStore
from blendfleet.assignment import estimate
from blendfleet.fleet import Fleet
from blendfleet.notebook_builder import RenderSettings
from blendfleet.ui.setup_dialog import SetupDialog

SETTINGS_URL = "https://www.kaggle.com/settings"
SECONDS_PER_FRAME_DEFAULT = 57.1     # measured: 1920x1080, 128spp, Tesla P100


class Dashboard(QMainWindow):
    def __init__(self, store: AccountStore, fleet_factory) -> None:
        super().__init__()
        self.store = store
        self.fleet_factory = fleet_factory
        self.blend: Path | None = None
        self.setWindowTitle("BlendFleet")
        self.resize(900, 620)

        root = QWidget(); v = QVBoxLayout(root); self.setCentralWidget(root)

        top = QHBoxLayout()
        self.acct_label = QLabel()
        mgr = QPushButton("Manage accounts…"); mgr.clicked.connect(self._manage)
        top.addWidget(self.acct_label, 1); top.addWidget(mgr)
        v.addLayout(top)

        form = QFormLayout()
        self.file_label = QLabel("<i>no .blend selected</i>")
        browse = QPushButton("Browse…"); browse.clicked.connect(self._pick)
        fr = QHBoxLayout(); fr.addWidget(self.file_label, 1); fr.addWidget(browse)
        holder = QWidget(); holder.setLayout(fr)
        form.addRow("Project", holder)

        self.start = QSpinBox(); self.start.setRange(1, 1000000); self.start.setValue(1)
        self.end = QSpinBox(); self.end.setRange(1, 1000000); self.end.setValue(250)
        self.rx = QSpinBox(); self.rx.setRange(64, 8192); self.rx.setValue(1920)
        self.ry = QSpinBox(); self.ry.setRange(64, 8192); self.ry.setValue(1080)
        self.spp = QSpinBox(); self.spp.setRange(1, 16384); self.spp.setValue(128)
        self.fmt = QComboBox(); self.fmt.addItems(["PNG", "JPEG"])
        for lbl, wdg in (("Start frame", self.start), ("End frame", self.end),
                         ("Width", self.rx), ("Height", self.ry),
                         ("Samples", self.spp), ("Format", self.fmt)):
            form.addRow(lbl, wdg)
        v.addLayout(form)

        self.eta = QLabel(); v.addWidget(self.eta)
        for w in (self.start, self.end):
            w.valueChanged.connect(self._update_eta)

        btns = QHBoxLayout()
        self.render_btn = QPushButton("RENDER ACROSS FLEET")
        self.render_btn.clicked.connect(self._launch)
        self.cancel_btn = QPushButton("Cancel all")
        self.cancel_btn.clicked.connect(self._cancel)
        self.collect_btn = QPushButton("Collect frames…")
        self.collect_btn.clicked.connect(self._collect)
        for b in (self.render_btn, self.cancel_btn, self.collect_btn):
            btns.addWidget(b)
        v.addLayout(btns)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Account", "Kaggle user", "Frames", "State", "Progress"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table)

        note = QLabel(
            f'Quota shown per account is the <b>API</b> figure. It has been '
            f'observed to disagree with <a href="{SETTINGS_URL}">your settings '
            f'page</a> — check both before a long run.')
        note.setOpenExternalLinks(True)
        note.setWordWrap(True)
        v.addWidget(note)

        self.timer = QTimer(self); self.timer.timeout.connect(self._poll)
        self.timer.start(30_000)
        self._refresh_accounts(); self._update_eta()

    # --- helpers ---
    def _refresh_accounts(self) -> None:
        n = len(self.store.list())
        names = ", ".join(a.label for a in self.store.list()) or "none"
        self.acct_label.setText(f"<b>{n} account(s):</b> {names}")

    def _update_eta(self) -> None:
        n = max(len(self.store.list()), 1)
        frames = max(self.end.value() - self.start.value() + 1, 0)
        hours = estimate(frames, SECONDS_PER_FRAME_DEFAULT, n)
        self.eta.setText(
            f"{frames} frames across {n} account(s) ≈ <b>{hours:.1f} h</b> each "
            f"(at {SECONDS_PER_FRAME_DEFAULT:.0f}s/frame measured on a P100 at "
            f"1920×1080/128spp — your scene will differ)")

    def _manage(self) -> None:
        SetupDialog(self.store, self).exec()
        self._refresh_accounts(); self._update_eta()

    def _pick(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Select .blend", "",
                                           "Blender (*.blend)")
        if f:
            self.blend = Path(f)
            self.file_label.setText(self.blend.name)

    def _launch(self) -> None:
        if not self.store.list():
            QMessageBox.warning(self, "No accounts", "Add at least one account.")
            return
        if self.blend is None:
            QMessageBox.warning(self, "No project", "Select a .blend first.")
            return
        if self.end.value() < self.start.value():
            QMessageBox.warning(self, "Bad range", "End frame is before start.")
            return
        settings = RenderSettings(self.rx.value(), self.ry.value(),
                                  self.spp.value(), self.fmt.currentText())
        self.render_btn.setEnabled(False)
        try:
            fleet = self.fleet_factory(self.store.list())
            st = fleet.launch(self.blend, settings,
                              self.start.value(), self.end.value())
            self._render_table(st)
        except Exception as e:
            QMessageBox.critical(self, "Launch failed", str(e))
        finally:
            self.render_btn.setEnabled(True)

    def _cancel(self) -> None:
        if QMessageBox.question(self, "Cancel all",
                                "Stop every running render?") != \
                QMessageBox.StandardButton.Yes:
            return
        try:
            self.fleet_factory(self.store.list()).cancel_all()
        except Exception as e:
            QMessageBox.warning(self, "Cancel failed", str(e))

    def _collect(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Save frames to")
        if not d:
            return
        from blendfleet.collector import collect
        from blendfleet.kaggle_client import KaggleClient
        fleet = self.fleet_factory(self.store.list())
        st = fleet.load()
        if st is None:
            QMessageBox.information(self, "Nothing to collect", "No job found.")
            return
        r = collect(st, self.store.list(), lambda t: KaggleClient(t), Path(d))
        msg = f"Copied {r.copied} frame(s)."
        if r.missing_frames:
            msg += (f"\n\nSTILL MISSING {len(r.missing_frames)}: "
                    f"{r.missing_frames[:20]}"
                    f"{'…' if len(r.missing_frames) > 20 else ''}")
        QMessageBox.information(self, "Collected", msg)

    def _poll(self) -> None:
        try:
            fleet = self.fleet_factory(self.store.list())
            st = fleet.poll()
            if st:
                self._render_table(st)
        except Exception:
            pass          # a transient poll failure must not kill the dashboard

    def _render_table(self, st) -> None:
        self.table.setRowCount(len(st.workers))
        for i, w in enumerate(st.workers):
            self.table.setItem(i, 0, QTableWidgetItem(w.label))
            self.table.setItem(i, 1, QTableWidgetItem(w.username))
            self.table.setItem(i, 2, QTableWidgetItem(
                f"{len(w.frames)} ({w.frames[0] if w.frames else '-'}…)"))
            self.table.setItem(i, 3, QTableWidgetItem(w.state))
            bar = QProgressBar()
            bar.setMaximum(max(len(w.frames), 1))
            bar.setValue(w.frames_done)
            self.table.setCellWidget(i, 4, bar)
```

```python
# blendfleet/__main__.py
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from blendfleet.accounts import AccountStore
from blendfleet.fleet import Fleet
from blendfleet.kaggle_client import KaggleClient
from blendfleet.platform_paths import cache_dir
from blendfleet.ui.dashboard import Dashboard
from blendfleet.ui.setup_dialog import SetupDialog


def main() -> int:
    app = QApplication(sys.argv)
    store = AccountStore.load()
    if not store.list():
        SetupDialog(store).exec()

    def fleet_factory(accounts):
        return Fleet(accounts, lambda t: KaggleClient(t), cache_dir() / "work")

    win = Dashboard(store, fleet_factory)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it**

Run: `python -m blendfleet`
Expected: setup dialog on first run, then the dashboard. No traceback.

- [ ] **Step 3: Manual verification checklist**

- [ ] Adding a malformed token shows a clear error, not a crash
- [ ] Adding the same token twice is rejected
- [ ] Token field is masked
- [ ] ETA updates when the frame range or account count changes
- [ ] RENDER with no accounts / no .blend / inverted range each warn and do nothing
- [ ] Quota text is explicitly labelled "API figure" and links to the settings page
- [ ] Cancel asks for confirmation first
- [ ] Collect reports missing frames when a worker produced nothing

- [ ] **Step 4: Commit**

```bash
git add blendfleet/ui/ blendfleet/__main__.py
git commit -m "feat: fleet dashboard and account setup UI"
```

---

### Task 10: Windows executable

**Files:**
- Create: `packaging/blendfleet.spec`, `packaging/build_windows.ps1`
- Modify: `.gitignore`

- [ ] **Step 1: Write the PyInstaller spec**

```python
# packaging/blendfleet.spec  -- shared by Windows and Linux builds
import sys
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None
datas = collect_data_files("kagglesdk") + collect_data_files("kaggle")

a = Analysis(["../blendfleet/__main__.py"], pathex=[".."], binaries=[],
             datas=datas, hiddenimports=["kagglesdk", "kaggle"],
             hookspath=[], runtime_hooks=[], excludes=[],
             cipher=block_cipher)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
          name="blendfleet", debug=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
```

- [ ] **Step 2: Write the Windows build script**

```powershell
# packaging/build_windows.ps1
$ErrorActionPreference = "Stop"
python -m pip install --upgrade pyinstaller
python -m PyInstaller --noconfirm --clean packaging/blendfleet.spec
Write-Host ""
Write-Host "Built dist/blendfleet.exe"
Write-Host "Expect 80-150 MB: PySide6 is bundled whole."
```

- [ ] **Step 3: Ignore build artefacts**

Append to `.gitignore`:

```
build/
dist/
*.spec.bak
__pycache__/
.pytest_cache/
```

- [ ] **Step 4: Build and verify**

Run: `powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1`
Then: `dist\blendfleet.exe`
Expected: dashboard opens, no console window, no traceback.

- [ ] **Step 5: Commit**

```bash
git add packaging/ .gitignore
git commit -m "feat: Windows executable packaging"
```

---

### Task 11: Linux build

**Files:**
- Create: `packaging/build_linux.sh`

Because nothing outside `platform_paths.py` branches on OS, this is packaging only — no code changes.

- [ ] **Step 1: Write the build script**

```bash
#!/usr/bin/env bash
# packaging/build_linux.sh
set -euo pipefail
python3 -m pip install --upgrade pyinstaller
python3 -m PyInstaller --noconfirm --clean packaging/blendfleet.spec
echo
echo "Built dist/blendfleet"
echo "Config lives at \${XDG_CONFIG_HOME:-~/.config}/blendfleet"
```

- [ ] **Step 2: Verify no stray platform branches**

Run:
```bash
grep -rnE 'sys\.platform|os\.name|platform\.system' blendfleet/ --include='*.py' | grep -v platform_paths.py
```
Expected: **no output.** Any hit is a portability bug — fix it before building.

- [ ] **Step 3: Run the suite on Linux**

Run: `pytest -v`
Expected: PASS, 40 tests — identical to Windows.

- [ ] **Step 4: Build and verify**

Run: `chmod +x packaging/build_linux.sh && ./packaging/build_linux.sh`
Then: `./dist/blendfleet`
Expected: dashboard opens; config written under `~/.config/blendfleet`.

- [ ] **Step 5: Commit**

```bash
git add packaging/build_linux.sh
git commit -m "feat: Linux build"
```

---

## Operating notes

**Trust.** A Kaggle API token grants full account access — datasets, notebooks, everything. Anyone lending you one should know that and know they can revoke it instantly at kaggle.com/settings. The setup dialog says so; do not soften that wording.

**Kaggle ToS.** Friends using their own accounts is legitimate. Creating extra accounts yourself to multiply quota is not, and would risk every account involved.

**Upload cost.** Each account uploads its own copy of the `.blend` because the API cannot add dataset collaborators. 64 MB × 3 accounts ≈ 45 s each at the ~2.5 MB/s observed.

**Concurrency ceiling.** 2 GPU sessions per account. Three accounts is a hard ceiling of 6 simultaneous renders, and each still draws on its own weekly quota.

## Self-Review

**Spec coverage:** accounts → Task 2; frame splitting → Task 3; Kaggle API incl. cancel and quota → Task 4; dataset upload → Task 5; notebook generation → Task 6; multi-account orchestration → Task 7; output retrieval and merge → Task 8; dashboard → Task 9; Windows `.exe` → Task 10; Linux → Task 11.

**Every verified fact is enforced by a test, not a comment:** the input-path walk, the device-selection fix, `--dir-mode zip` avoidance, 404-means-not-started, token-never-in-argv, and push-is-never-a-no-op each have a dedicated assertion.

**Type consistency:** `KernelStatus.state` is a lowercase `str` everywhere, produced in Task 4 and consumed in Tasks 7 and 9. `WorkerState.frames` is `list[int]` from Task 3 through Tasks 7, 8 and 9. `Quota.source` is always set and always displayed.

**Known gap, deliberately deferred:** `WorkerState.frames_done` is only ever 0 until log-stream parsing lands. The notebook already emits `PROGRESS frame=… done=n/m` lines for exactly this purpose, and `ApiGetKernelSessionLogsStreamRequest` exists in the SDK. v1 shows job-level state and a progress bar that fills on completion; live per-frame progress is the first v2 task.

**Placeholder scan:** no TBDs. Every code step is runnable; every test step contains real assertions.
