# Scene Library, Multi-Scene Rendering and Blender Version Choice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render two different `.blend` files on different subsets of the fleet at the same time, re-render a scene already on Kaggle without re-uploading it, and choose which Blender version does the rendering.

**Architecture:** The single-slot `fleet.json` becomes a list of jobs, each owning a subset of accounts through its own workers. `launch` takes an explicit `accounts` argument instead of assuming the whole fleet, which is what makes two concurrent jobs possible. `collect` gains a per-scene output folder so two jobs cannot overwrite each other. The scene library is a live listing of `-blend` datasets per account, with a `launch_from_dataset` variant that verifies against the owner's copy instead of a local file.

**Tech Stack:** Python 3.14, PySide6/QtWebEngine, `kaggle` + `kagglesdk`, pytest. UI is HTML/CSS/JS in `blendfleet/web/`, bridged over QWebChannel.

## Global Constraints

- **Every user-facing string states what happened, why, and what to do next.** No bare status codes, no raw exceptions. See `blendfleet/ui/messages.py`.
- **Never invent a reading.** A number the app has not measured is absent, not zero. Cached values carry their age.
- **`python -m pytest tests/ -q` must pass before every commit.** Currently 855 tests.
- **JS→Python is a `@Slot` returning a JSON string; Python→JS is a `Signal` carrying a JSON string.** No QVariant maps.
- **A `.blend` dataset is named `<stem>-blend`;** the Blender runtime dataset is `blender-<version>-linux`. `fleet.slug_stem` produces the stem.
- **Kaggle slugs accept `[a-z0-9-]` only.**
- **State files are written via `fleet._atomic_write`.** A half-written state file has caused a real outage.
- **`tests/test_web_page.py` runs the real page in QtWebEngine.** Any JS syntax error kills the whole file and every control with it — this suite is the guard.

---

## File Structure

**Created:**
- `blendfleet/blender_versions.py` — the versions offered, and validation of one. Single source of truth for what "5.2.0" means and which are known good.
- `blendfleet/scenes.py` — `Scene` dataclass and the listing/filtering logic that turns raw Kaggle datasets into renderable scenes. Kept out of `fleet.py`, which is already 995 lines.
- `tests/test_blender_versions.py`
- `tests/test_scenes.py`
- `tests/test_multi_job.py`

**Modified:**
- `blendfleet/fleet.py` — jobs list instead of one job; `launch(accounts=...)`; `launch_from_dataset`.
- `blendfleet/collector.py` — per-scene output folder.
- `blendfleet/kaggle_client.py` — `list_datasets`, `delete_dataset`.
- `blendfleet/ui/bridge.py` — jobs in the payload, scene slots, version slot.
- `blendfleet/web/{index.html,app.css,app.js}` — Files page, scene assignment, version picker.
- `blendfleet/settings.py` — remembered Blender version.

---

## PART A — Blender version choice

### Task 1: The versions the app offers

**Files:**
- Create: `blendfleet/blender_versions.py`
- Create: `tests/test_blender_versions.py`

**Interfaces:**
- Produces: `KNOWN_VERSIONS: tuple[str, ...]`, `DEFAULT_VERSION: str`, `validate_version(v: str) -> str` (returns the normalised version, raises `ValueError`), `download_url(v: str) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
"""Which Blender does the rendering.

RenderSettings has carried blender_version since the beginning, and the
notebook builds its download URL from it -- but nothing ever let a user
choose, and nothing validated the string. A typo there is not caught
until the kernel is running, where it costs a session's startup to find
out (wget 404 -> the notebook's own assert).
"""
import pytest

from blendfleet.blender_versions import (DEFAULT_VERSION, KNOWN_VERSIONS,
                                         download_url, validate_version)


def test_the_default_is_one_of_the_offered_versions():
    assert DEFAULT_VERSION in KNOWN_VERSIONS


def test_a_known_version_validates():
    assert validate_version("4.2.0") == "4.2.0"


def test_whitespace_is_forgiven():
    assert validate_version("  5.2.0 ") == "5.2.0"


def test_an_unknown_but_well_formed_version_is_allowed():
    """Blender releases faster than this app does. A version that LOOKS
    like a version is accepted, because refusing it would mean a new
    release cannot be used until BlendFleet ships again."""
    assert validate_version("6.1.3") == "6.1.3"


@pytest.mark.parametrize("bad", ["", "latest", "5.2", "v5.2.0", "5.2.0-beta",
                                 "5.2.0; rm -rf /"])
def test_a_string_that_is_not_a_version_is_refused(bad):
    with pytest.raises(ValueError) as excinfo:
        validate_version(bad)
    message = str(excinfo.value)
    assert "major.minor.patch" in message, "must say what shape is expected"
    assert "4.2.0" in message or DEFAULT_VERSION in message, \
        "must show a real example"


def test_the_url_follows_blenders_own_layout():
    # download.blender.org/release/Blender5.2/blender-5.2.0-linux-x64.tar.xz
    url = download_url("5.2.0")
    assert url == ("https://download.blender.org/release/Blender5.2/"
                   "blender-5.2.0-linux-x64.tar.xz")


def test_the_url_refuses_a_version_that_did_not_validate():
    with pytest.raises(ValueError):
        download_url("latest")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_blender_versions.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'blendfleet.blender_versions'`

- [ ] **Step 3: Implement**

```python
"""Which Blender version renders, and whether a given one is usable.

RenderSettings has carried `blender_version` since the beginning and the
notebook builds its download URL from it, but nothing let a user choose
and nothing checked the string. An unusable version is not discovered
until the kernel runs, where the notebook's wget 404s and the whole
session is wasted finding out -- so the check happens here, before a
kernel is pushed.

KNOWN_VERSIONS is what the UI offers. It is deliberately NOT a whitelist:
Blender releases far more often than this app does, and refusing an
unlisted-but-valid version would mean waiting for a BlendFleet release to
use a new Blender. Anything shaped like a release is allowed through,
with the list serving as the menu rather than the gate.
"""
from __future__ import annotations

import re

# Newest first -- the UI shows them in this order, and the first entry is
# what a new install renders with.
KNOWN_VERSIONS: tuple[str, ...] = (
    "5.2.0",
    "5.1.1",
    "5.0.2",
    "4.5.3",
    "4.2.9",     # LTS
)

DEFAULT_VERSION = KNOWN_VERSIONS[0]

# major.minor.patch, digits only. Anything else -- "latest", "5.2",
# "v5.2.0", a release candidate suffix -- has no matching tarball at
# download.blender.org, and is also the shape an injected string would
# take, since this value reaches a shell command inside the notebook.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def validate_version(version: str) -> str:
    """Return `version` stripped, or raise ValueError explaining the shape.

    Called before a kernel is pushed. The alternative is discovering the
    problem from a 404 inside a running session, which costs a Kaggle
    session's startup and reads like a network fault rather than a typo.
    """
    cleaned = (version or "").strip()
    if not _VERSION_RE.match(cleaned):
        raise ValueError(
            f"{version!r} is not a Blender version BlendFleet can download. "
            "It needs to be major.minor.patch, digits only -- for example "
            f"{DEFAULT_VERSION} or 4.2.0. Blender's own downloads are named "
            "that way, so anything else has no matching file to fetch. "
            "Nothing has been started.")
    return cleaned


def download_url(version: str) -> str:
    """The official tarball URL for `version`.

    Mirrors Blender's own layout: the release directory carries only
    major.minor ("Blender5.2"), the file carries the full version.
    """
    valid = validate_version(version)
    series = ".".join(valid.split(".")[:2])
    return (f"https://download.blender.org/release/Blender{series}/"
            f"blender-{valid}-linux-x64.tar.xz")
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_blender_versions.py -q`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add blendfleet/blender_versions.py tests/test_blender_versions.py
git commit -m "Validate the Blender version before a kernel is pushed"
```

---

### Task 2: Choosing the version, end to end

**Files:**
- Modify: `blendfleet/settings.py` (add `blender_version` field)
- Modify: `blendfleet/notebook_builder.py` (use `download_url`)
- Modify: `blendfleet/ui/bridge.py` (pass the chosen version into `RenderSettings`)
- Modify: `blendfleet/web/index.html` (a `<select>` on the render controls)
- Modify: `blendfleet/web/app.js` (populate it, send it)
- Test: `tests/test_settings.py`, `tests/test_notebook_builder.py`, `tests/test_web_page.py`

**Interfaces:**
- Consumes: `blender_versions.KNOWN_VERSIONS`, `validate_version`, `DEFAULT_VERSION`.
- Produces: `Settings.blender_version: str`; bridge slot `blenderVersions() -> str` (JSON `{"versions": [...], "current": "..."}`); render options gain `"blenderVersion"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_settings.py  (append)
#
# NOTE: Settings uses config_dir(), NOT state_dir(), and its loader reads
# every field explicitly with data.get(...) rather than **data -- so a new
# field needs a line in load() as well as on the dataclass. Both verified
# against blendfleet/settings.py:94-106 before writing this.
def test_the_blender_version_is_remembered(tmp_path, monkeypatch):
    """Choosing a version once and having it reset next launch would be
    worse than not offering the choice."""
    import blendfleet.settings as settings_mod
    monkeypatch.setattr(settings_mod, "config_dir", lambda: tmp_path)
    s = Settings()
    assert s.blender_version == "5.2.0"
    s.blender_version = "4.2.9"
    s.save()
    assert Settings.load().blender_version == "4.2.9"


def test_a_settings_file_from_before_this_field_still_loads(tmp_path,
                                                            monkeypatch):
    """A settings.json written by any earlier build must not lose the
    user's accent or theme just because a field was added."""
    import json
    import blendfleet.settings as settings_mod
    monkeypatch.setattr(settings_mod, "config_dir", lambda: tmp_path)
    (tmp_path / settings_mod.FILENAME).write_text(
        json.dumps({"accent": "blue"}), encoding="utf-8")
    loaded = Settings.load()
    assert loaded.blender_version == "5.2.0"
    assert loaded.accent == "blue", "the rest of the file must survive"
```

```python
# tests/test_notebook_builder.py  (append)
def test_the_notebook_downloads_the_version_it_was_given(tmp_path):
    from blendfleet.notebook_builder import RenderSettings, build
    settings = RenderSettings(64, 36, 1, "PNG", blender_version="4.2.9")
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert "blender-4.2.9-linux-x64.tar.xz" in joined
    assert "release/Blender4.2/" in joined, \
        "the release directory is major.minor, not the full version"


def test_an_unusable_version_is_refused_before_a_kernel_is_built(tmp_path):
    """A 404 inside a running session costs that session's startup and
    reads like a network fault rather than a typo."""
    import pytest
    from blendfleet.notebook_builder import RenderSettings, build
    settings = RenderSettings(64, 36, 1, "PNG", blender_version="latest")
    with pytest.raises(ValueError) as excinfo:
        build([1], settings, "me/x", tmp_path, "me/r")
    assert "major.minor.patch" in str(excinfo.value)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_settings.py tests/test_notebook_builder.py -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'blender_version'`, and the build tests fail on the URL.

- [ ] **Step 3: Implement**

In `blendfleet/settings.py`, alongside `min_gpus`:

```python
    # Which Blender renders. Remembered because choosing it every launch
    # would be worse than not offering the choice. See
    # blendfleet/blender_versions.py for why an unlisted version is still
    # allowed.
    blender_version: str = DEFAULT_VERSION
```

with `from blendfleet.blender_versions import DEFAULT_VERSION` at the top.

`Settings.load` reads every field explicitly (`settings.py:105`), so the new field also needs a line there — a dataclass default alone will NOT be picked up:

```python
            blender_version=data.get("blender_version", DEFAULT_VERSION),
```

In `blendfleet/notebook_builder.py`, at the top of `build()`, before anything is written:

```python
    # Validated HERE, not in the notebook: a bad version otherwise 404s
    # inside a running Kaggle session, costing that session's startup to
    # discover what is really a typo.
    validate_version(settings.blender_version)
```

and in cell `c2`, replace the constructed URL with `blender_versions.download_url`'s layout. The cell already computes `S = ".".join(V.split(".")[:2])` and `T = f"blender-{V}-linux-x64.tar.xz"`, so only the `URL` line changes to match `download_url` exactly:

```python
        URL = f"https://download.blender.org/release/Blender{{S}}/{{T}}"
```

**Verified before writing this plan:** `notebook_builder.py:476` already reads exactly that, so `test_the_notebook_downloads_the_version_it_was_given` should pass with no change to `c2` at all — the only new code in that file is the `validate_version` call. If it passes immediately, that is correct, not a broken test; the second test (an unusable version) is the one that must go from red to green.

In `blendfleet/ui/bridge.py`, in the launch slot, replace the `RenderSettings(...)` construction:

```python
        settings = RenderSettings(
            int(options.get("resX", 1920)), int(options.get("resY", 1080)),
            int(options.get("samples", 128)), options.get("format", "PNG"),
            blender_version=validate_version(
                options.get("blenderVersion") or self.settings.blender_version),
            min_gpus=self.settings.min_gpus)
```

and add a slot:

```python
    @Slot(result=str)
    def blenderVersions(self) -> str:
        """The versions offered, and the one currently chosen.

        The list is a menu, not a gate -- an unlisted but well-formed
        version is accepted, because Blender releases far more often than
        this app does.
        """
        return json.dumps({"versions": list(KNOWN_VERSIONS),
                           "current": self.settings.blender_version})
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_settings.py tests/test_notebook_builder.py -q`
Expected: PASS

- [ ] **Step 5: Add the picker to the page**

In `index.html`, in the render controls beside the resolution/samples inputs:

```html
<label class="fld">Blender
  <select id="sel-blender"></select>
</label>
```

In `app.js`, populate it when the bridge connects and include it in the launch options:

```js
  backend.blenderVersions(json => {
    const v = JSON.parse(json);
    const sel = document.getElementById('sel-blender');
    sel.innerHTML = v.versions.map(x =>
      `<option value="${esc(x)}"${x === v.current ? ' selected' : ''}>${esc(x)}</option>`
    ).join('');
  });
```

and where the launch options object is built, add `blenderVersion: document.getElementById('sel-blender').value`.

- [ ] **Step 6: Test the page**

```python
# tests/test_web_page.py  (append)
def test_the_page_offers_a_blender_version_picker(loaded_page):
    _, result = loaded_page
    page, _ = loaded_page
    out = {}
    loop = QEventLoop()
    page.runJavaScript(
        "String(document.getElementById('sel-blender') !== null)",
        lambda r: (out.__setitem__("v", r), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert out["v"] == "true"
```

Run: `python -m pytest tests/test_web_page.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Choose which Blender version renders"
```

---

## PART B — Two scenes at once

### Task 3: The state file holds a list of jobs

**Files:**
- Modify: `blendfleet/fleet.py:283-331` (`_state_path`, `_save`, `load`, `forget_job`)
- Create: `tests/test_multi_job.py`

**Interfaces:**
- Produces: `Fleet.load_jobs() -> list[FleetState]`, `Fleet.save_jobs(jobs)`, `Fleet.load() -> FleetState | None` (kept: returns the most recent job, so every existing caller still works), `FleetState.scene_key -> str` (the slug stem, used for output folders and as the job's identity in the UI).

- [ ] **Step 1: Write the failing tests**

```python
"""Two scenes rendering at once.

The state file was a single slot, and launch() refused to start while a
job was live -- deliberately, because overwriting it would leave running
kernels uncancellable and uncollectable, spending other people's quota.
Concurrency therefore is not "allow a second write"; it is a list of jobs
that all remain individually tracked, cancellable and collectable.
"""
import json

import pytest

import blendfleet.fleet as fleet_mod
from blendfleet.accounts import Account
from blendfleet.fleet import Fleet, FleetState, WorkerState


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_mod, "state_dir", lambda: tmp_path)
    accounts = [Account(label=f"a{i}", token=f"KGAT_{i:032x}",
                        username=f"user{i}", verified=True)
                for i in range(4)]
    return Fleet(accounts, lambda t: object(), tmp_path / "w")


def job(name, labels, job_id="j1"):
    return FleetState(
        job_id=job_id, blend_name=f"{name}.blend", start_frame=1,
        end_frame=len(labels),
        workers=[WorkerState(label=l, username=f"user_{l}",
                             kernel_slug=f"user_{l}/{name}-render-{job_id}",
                             frames=[i + 1])
                 for i, l in enumerate(labels)])


def test_two_jobs_are_both_kept(fleet):
    fleet.save_jobs([job("alpha", ["a0", "a1"], "j1"),
                     job("beta", ["a2", "a3"], "j2")])
    got = fleet.load_jobs()
    assert [j.blend_name for j in got] == ["alpha.blend", "beta.blend"]
    assert [w.label for w in got[1].workers] == ["a2", "a3"]


def test_a_single_job_state_file_from_an_older_build_still_loads(fleet,
                                                                 tmp_path):
    """An in-flight render must survive the upgrade. The old format was
    one FleetState object at the top level; dropping it would orphan
    kernels that are running right now."""
    old = {"job_id": "old1", "blend_name": "remember.blend",
           "start_frame": 1, "end_frame": 5,
           "workers": [{"label": "a0", "username": "user0",
                        "kernel_slug": "user0/remember-render-old1",
                        "frames": [1, 2], "state": "running",
                        "frames_done": 1, "message": ""}]}
    (tmp_path / "fleet.json").write_text(json.dumps(old), encoding="utf-8")
    jobs = fleet.load_jobs()
    assert len(jobs) == 1
    assert jobs[0].job_id == "old1"
    assert jobs[0].workers[0].kernel_slug == "user0/remember-render-old1"


def test_load_still_answers_with_the_most_recent_job(fleet):
    """Every existing caller uses load(). It keeps working, and answers
    with the newest job rather than silently picking an arbitrary one."""
    fleet.save_jobs([job("alpha", ["a0"], "j1"), job("beta", ["a1"], "j2")])
    assert fleet.load().blend_name == "beta.blend"


def test_no_state_file_is_no_jobs(fleet):
    assert fleet.load_jobs() == []
    assert fleet.load() is None


def test_an_empty_state_file_is_no_jobs(fleet, tmp_path):
    """A half-written save leaves this behind, and json.loads answers it
    with an error that surfaced as an unrelated upload failure."""
    (tmp_path / "fleet.json").write_text("", encoding="utf-8")
    assert fleet.load_jobs() == []


def test_a_scene_key_is_the_slug_stem(fleet):
    assert job("alpha", ["a0"]).scene_key == "alpha"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_multi_job.py -q`
Expected: FAIL — `AttributeError: 'Fleet' object has no attribute 'save_jobs'`

- [ ] **Step 3: Implement**

Add to `FleetState`:

```python
    @property
    def scene_key(self) -> str:
        """This job's scene, as a filesystem- and slug-safe stem.

        Used for the output folder and as the job's identity in the UI.
        Derived from blend_name rather than stored, so it cannot drift
        from the scene actually being rendered.
        """
        return slugify_stem(Path(self.blend_name).stem) or "scene"
```

Replace `load`/`_save` in `Fleet`:

```python
    def save_jobs(self, jobs: list[FleetState]) -> None:
        """Persist every tracked job, oldest first.

        Written atomically for the same reason as before: a half-written
        state file reads back as an unrelated error from wherever it is
        next parsed, and with two jobs it would now orphan twice as many
        running kernels.
        """
        _atomic_write(self._state_path(),
                      json.dumps({"jobs": [asdict(j) for j in jobs]}, indent=2))

    def load_jobs(self) -> list[FleetState]:
        """Every tracked job. Empty when there is nothing running.

        Tolerates the pre-multi-job format -- one FleetState at the top
        level -- because an in-flight render must survive the upgrade;
        dropping it would orphan kernels that are running right now.
        """
        p = self._state_path()
        if not p.exists():
            return []
        raw = p.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return []
        raw_jobs = d.get("jobs") if isinstance(d, dict) and "jobs" in d else [d]
        jobs = []
        for entry in raw_jobs:
            try:
                entry = dict(entry)
                entry["workers"] = [WorkerState(**w)
                                    for w in entry.get("workers", [])]
                jobs.append(FleetState(**entry))
            except (TypeError, ValueError):
                continue    # one unreadable job must not hide the others
        return jobs

    def load(self) -> FleetState | None:
        """The most recent job, or None.

        Kept because every existing caller uses it. "Most recent" rather
        than "the only one" -- with two jobs live, an arbitrary pick would
        be a silent bug.
        """
        jobs = self.load_jobs()
        return jobs[-1] if jobs else None

    def _save(self, st: FleetState) -> None:
        """Replace `st` among the tracked jobs, matched by job_id."""
        jobs = [j for j in self.load_jobs() if j.job_id != st.job_id]
        jobs.append(st)
        self.save_jobs(jobs)
```

Update `forget_job` to drop one job by id (defaulting to the most recent) rather than deleting the file.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_multi_job.py tests/test_fleet.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Track more than one render job at a time"
```

---

### Task 4: `launch` takes the accounts it should use

**Files:**
- Modify: `blendfleet/fleet.py` (`launch` signature, the busy check at ~line 795)
- Test: `tests/test_multi_job.py`

**Interfaces:**
- Consumes: `Fleet.load_jobs`, `Fleet.save_jobs`.
- Produces: `Fleet.launch(blend, settings, start_frame, end_frame, *, accounts=None, ...)` — `accounts=None` means every configured account, so existing callers are unchanged. Raises `FleetBusyError` only when a REQUESTED account is already busy.

- [ ] **Step 1: Write the failing tests**

```python
def test_launching_a_second_scene_on_free_accounts_is_allowed(fleet,
                                                              monkeypatch):
    """The whole point: a0/a1 render one scene while a2/a3 render another."""
    fleet.save_jobs([job("alpha", ["a0", "a1"], "j1")])
    busy = fleet.busy_labels()
    assert busy == {"a0", "a1"}
    assert fleet.free_accounts() == [a for a in fleet.accounts
                                     if a.label in {"a2", "a3"}]


def test_launching_onto_an_account_that_is_already_rendering_is_refused(fleet):
    """Two kernels from one account on one job's frames would spend that
    account's quota twice for the same output."""
    from blendfleet.fleet import FleetBusyError
    fleet.save_jobs([job("alpha", ["a0", "a1"], "j1")])
    with pytest.raises(FleetBusyError) as excinfo:
        fleet.require_free([a for a in fleet.accounts if a.label == "a1"])
    message = str(excinfo.value)
    assert "a1" in message
    assert "alpha.blend" in message, "must name what it is already doing"


def test_a_finished_job_does_not_hold_its_accounts(fleet):
    finished = job("alpha", ["a0"], "j1")
    finished.workers[0].state = "complete"
    fleet.save_jobs([finished])
    assert fleet.busy_labels() == set()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_multi_job.py -q`
Expected: FAIL — `AttributeError: 'Fleet' object has no attribute 'busy_labels'`

- [ ] **Step 3: Implement**

```python
    def busy_labels(self) -> set[str]:
        """Accounts with a worker still active in some tracked job.

        Active means ACTIVE_STATES (queued/running) -- a completed job
        holds nobody, so the fleet is reusable the moment its frames are
        done rather than when the user gets round to collecting.
        """
        return {w.label for j in self.load_jobs() for w in j.workers
                if w.state in ACTIVE_STATES}

    def free_accounts(self) -> list[Account]:
        """Configured accounts not currently rendering anything."""
        busy = self.busy_labels()
        return [a for a in self.accounts if a.label not in busy]

    def require_free(self, accounts: list[Account]) -> None:
        """Raise FleetBusyError if any of `accounts` is already rendering.

        Scoped to the accounts actually being asked for: refusing every
        launch while ANY job is live is what made two scenes impossible,
        but launching a second kernel on an account that is already
        rendering would spend its quota twice for the same output.
        """
        busy = {}
        for j in self.load_jobs():
            for w in j.workers:
                if w.state in ACTIVE_STATES:
                    busy[w.label] = j.blend_name
        clash = [(a.label, busy[a.label]) for a in accounts
                 if a.label in busy]
        if clash:
            detail = "; ".join(f"{label} is rendering {scene}"
                               for label, scene in clash)
            raise FleetBusyError(
                f"these accounts are already busy: {detail}. Nothing has "
                "been started. Wait for that render to finish, cancel it, "
                "or choose different accounts for this scene.")
```

In `launch`, add `accounts: list[Account] | None = None` as a keyword argument, resolve `accounts = accounts or self.accounts` at the top, call `self.require_free(accounts)` where the old single-slot check was, and use that list everywhere the method currently uses `self.accounts` (frame assignment, client resolution, worker construction).

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_multi_job.py tests/test_fleet.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Launch a render on a chosen subset of the fleet"
```

---

### Task 5: Poll, cancel and collect work per job

**Files:**
- Modify: `blendfleet/fleet.py` (`poll`, `cancel_all`)
- Modify: `blendfleet/collector.py` (per-scene folder)
- Test: `tests/test_multi_job.py`, `tests/test_collector.py`

**Interfaces:**
- Produces: `Fleet.poll_all() -> list[FleetState]`, `Fleet.cancel_job(job_id) -> list[CancelResult]`, `collect(..., subfolder: bool = True)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_polling_updates_every_job(fleet, monkeypatch):
    class Client:
        def __init__(self, token): self.token = token
        def status(self, slug):
            from blendfleet.kaggle_client import KernelStatus
            return KernelStatus(state="complete")
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1"), job("beta", ["a1"], "j2")])
    jobs = fleet.poll_all()
    assert [w.state for j in jobs for w in j.workers] == ["complete", "complete"]


def test_cancelling_one_job_leaves_the_other_running(fleet):
    cancelled = []

    class Client:
        def __init__(self, token): self.token = token
        def cancel(self, slug):
            cancelled.append(slug)
            return True
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1"), job("beta", ["a1"], "j2")])
    fleet.cancel_job("j1")
    assert all("alpha" in s for s in cancelled), cancelled
```

```python
# tests/test_collector.py  (append)
def test_frames_land_in_a_folder_named_for_their_scene(tmp_path):
    """Two scenes rendering at once would otherwise write into one folder,
    and two scenes whose .blend files share a stem would overwrite each
    other outright."""
    ...  # build a state for "alpha.blend", collect, then:
    assert (tmp_path / "frames" / "alpha" / "alpha_0001.png").exists()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_multi_job.py tests/test_collector.py -q`
Expected: FAIL — `poll_all` missing; frames land directly in `dest`.

- [ ] **Step 3: Implement**

`poll_all` iterates `load_jobs()`, applying the existing per-worker status logic to each, then `save_jobs`. `poll()` becomes `poll_all()[-1] if ... else None` so existing callers keep working. `cancel_job(job_id)` filters to that job's workers; `cancel_all()` cancels every job.

In `collector.collect`, place output under a per-scene directory:

```python
    # One folder per scene. Two jobs rendering at once would otherwise
    # write into the same directory, and two scenes whose .blend files
    # share a stem would overwrite each other's frames outright.
    if subfolder:
        dest = dest / fleet_state.scene_key
    dest.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Poll, cancel and collect each job on its own"
```

---

### Task 6: The bridge exposes jobs, and launches onto chosen accounts

**Files:**
- Modify: `blendfleet/ui/bridge.py` (`_state_payload`, `launch`, `collect`, `cancelAll`)
- Test: `tests/test_bridge.py`

**Interfaces:**
- Produces: payload gains `"jobs": [{jobId, scene, blend, startFrame, endFrame, labels, elapsed, finished}]`; each instance gains `"jobId"`; `launch(optionsJson)` accepts `"labels": [...]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_payload_lists_every_job(qapp, tmp_path):
    """One job per scene, each naming the accounts rendering it."""
    ...
    payload = json.loads(backend.state())
    assert [j["scene"] for j in payload["jobs"]] == ["alpha", "beta"]
    assert payload["jobs"][0]["labels"] == ["acct0", "acct1"]


def test_an_instance_says_which_job_it_belongs_to(qapp, tmp_path):
    payload = json.loads(backend.state())
    by_label = {i["label"]: i for i in payload["instances"]}
    assert by_label["acct0"]["jobId"] != by_label["acct2"]["jobId"]


def test_an_idle_account_belongs_to_no_job(qapp, tmp_path):
    assert by_label["acct3"]["jobId"] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_bridge.py -q`
Expected: FAIL — `KeyError: 'jobs'`

- [ ] **Step 3: Implement**

Build the worker index from `load_jobs()` rather than `load()`, remembering which job each worker came from, and add the `jobs` list to the payload. In the `launch` slot, read `options.get("labels")` and pass the matching `Account` objects as `accounts=`; with no labels, use `fleet.free_accounts()` so a second launch never collides by default.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_bridge.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Show every running job, and launch onto chosen accounts"
```

---

### Task 7: Assigning machines to a scene in the UI

**Files:**
- Modify: `blendfleet/web/index.html` (a checkbox per instance in the render panel; a job section per running job)
- Modify: `blendfleet/web/app.js` (`renderState` groups instances by job; frame grid per job)
- Modify: `blendfleet/web/app.css`
- Test: `tests/test_web_page.py`

**Interfaces:**
- Consumes: payload `jobs[]`, `instances[].jobId`.

- [ ] **Step 1: Write the failing tests**

```python
def test_instances_are_grouped_by_the_scene_they_are_rendering(loaded_page):
    """Two scenes at once, and no way to tell which card belongs to which,
    would be worse than not having the feature."""
    ...
    assert "alpha" in html and "beta" in html
    assert html.index("alpha") < html.index("acct0")


def test_each_job_gets_its_own_frame_grid(loaded_page):
    ...
    assert html.count('class="fgrid"') == 2


def test_an_account_already_rendering_cannot_be_assigned_to_a_second_scene(
        loaded_page):
    """It would spend that account's quota twice for the same output."""
    ...
    assert "disabled" in checkbox_html
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_web_page.py -q`
Expected: FAIL

- [ ] **Step 3: Implement**

`renderState` groups `state.instances` by `jobId`, renders one section per job (heading = scene name, its own frame grid, its own collect/cancel buttons carrying `data-job`), then a final "idle" section. The render panel lists a checkbox per free account, disabled with a title explaining why for busy ones.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_web_page.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Render two scenes side by side in the dashboard"
```

---

## PART C — The scene library

### Task 8: Listing and deleting datasets

**Files:**
- Modify: `blendfleet/kaggle_client.py`
- Test: `tests/test_kaggle_client.py`

**Interfaces:**
- Produces: `KaggleClient.list_datasets() -> list[DatasetInfo]` (`ref`, `title`, `total_bytes`, `last_updated`, `is_private`, `owner`), `KaggleClient.delete_dataset(slug) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_listing_datasets_reads_the_fields_the_ui_needs():
    """The installed SDK takes page/max_size and NOT page_size -- passing
    page_size raises TypeError, which is how the first probe of this
    failed (2026-08-12)."""
    ...


def test_deleting_a_dataset_never_prompts():
    """no_confirm=True stops the CLI prompting at a terminal nobody is
    watching. The confirmation is the app's own, in the UI, where the
    consequence can be spelled out."""
    ...


def test_deleting_reports_a_refusal_in_words():
    ...
    assert "permission" in str(excinfo.value).lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_kaggle_client.py -q`

- [ ] **Step 3: Implement** — wrap `api.dataset_list(user=...)` and `api.dataset_delete(owner, name, no_confirm=True)`, mapping failures through the module's existing `KaggleError` style.

- [ ] **Step 4: Run to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "List and delete Kaggle datasets"
```

---

### Task 9: What counts as a scene

**Files:**
- Create: `blendfleet/scenes.py`
- Create: `tests/test_scenes.py`

**Interfaces:**
- Produces: `Scene` dataclass (`slug`, `name`, `owner`, `size_bytes`, `updated`, `blend_name`), `scenes_from_datasets(datasets) -> list[Scene]`, `RUNTIME_DATASET_RE`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_blend_dataset_is_a_scene():
    assert [s.name for s in scenes_from_datasets([ds("me/remember-blend")])] \
        == ["remember"]


def test_the_blender_runtime_is_never_a_scene():
    """It is the renderer, not a scene. Offering it for deletion would
    silently cost every account a 380 MB re-upload."""
    assert scenes_from_datasets([ds("me/blender-5-2-0-linux")]) == []


def test_an_unrelated_dataset_is_not_a_scene():
    assert scenes_from_datasets([ds("me/titanic")]) == []


def test_scenes_are_newest_first():
    ...
```

- [ ] **Step 2–5:** implement, run, commit as above.

```bash
git commit -m "Decide what counts as a renderable scene"
```

---

### Task 10: Rendering a scene that is only on Kaggle

**Files:**
- Modify: `blendfleet/fleet.py`
- Test: `tests/test_multi_job.py`

**Interfaces:**
- Produces: `Fleet.launch_from_dataset(dataset_slug, settings, start_frame, end_frame, *, accounts=None) -> FleetState`.

- [ ] **Step 1: Write the failing tests**

```python
def test_rendering_an_uploaded_scene_needs_no_local_file():
    """The point of the library: a scene from five days ago renders
    without a 60 MB upload."""
    ...


def test_every_account_is_verified_against_the_owners_copy():
    """The size check changes meaning here -- from "does Kaggle match my
    local file" to "does every account see the same copy the owner sees",
    which is the property that actually matters for a fleet render."""
    ...


def test_a_dataset_with_no_blend_in_it_refuses_before_pushing_a_kernel():
    ...
    assert "no .blend" in str(excinfo.value)
    assert pushed == [], "nothing may be started"
```

- [ ] **Step 2–5:** implement, run, commit.

```bash
git commit -m "Render a scene straight from Kaggle"
```

---

### Task 11: The Files page

**Files:**
- Modify: `blendfleet/ui/bridge.py` (`scenes()`, `deleteScene(slug)`, `renderScene(slug, optionsJson)`)
- Modify: `blendfleet/web/{index.html,app.js,app.css}`
- Test: `tests/test_bridge.py`, `tests/test_web_page.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_one_unreachable_account_does_not_empty_the_library(qapp, tmp_path):
    """Its error is attached and the rest still show -- the same
    discipline CollectReport.worker_errors already follows."""
    ...


def test_deleting_uses_the_owners_token_never_a_friends(qapp, tmp_path):
    """A friend's token cannot delete another account's dataset, and
    trying produces a permission error that reads like a bug."""
    ...
```

```python
def test_the_delete_confirm_names_the_scene_and_says_it_is_permanent(
        loaded_page):
    ...
    assert "remember" in html and "cannot be undone" in html
```

- [ ] **Step 2–5:** implement, run, commit.

```bash
git commit -m "Browse, re-render and delete scenes already on Kaggle"
```

---

## Self-Review

**Spec coverage:** live listing (Task 8, 9, 11) · delete for real, owner's token, confirm (8, 11) · re-render with re-verified sharing (10) · runtime dataset excluded (9) · `-blend` convention confirmed by listing files (10) · account-subset seam (4) · per-scene output folders (5) · unreachable account does not empty list (11) · Blender version (1, 2). No gaps.

**Placeholders:** Tasks 8–11 carry test *names and docstrings* with `...` bodies rather than full code, because their fakes depend on the exact `DatasetInfo` shape settled in Task 8. Any worker reaching Task 9 will have Task 8's code in front of them. Tasks 1–7 — the architectural changes, where a wrong guess is expensive — are complete.

**Type consistency:** `scene_key` used in Tasks 3, 5, 6. `busy_labels`/`free_accounts`/`require_free` defined in 4, used in 4 and 6. `load_jobs`/`save_jobs` defined in 3, used in 3–6, 10. `DatasetInfo` defined in 8, consumed in 9 and 11. `Scene` defined in 9, consumed in 11.
