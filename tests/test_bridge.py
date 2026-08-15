"""The web UI's only source of truth is the Backend payload, so the shape
of that payload -- and its honesty guarantees -- are what these cover.

The page cannot be trusted to enforce any of this: it is HTML that anyone
can edit, and the design it was ported from shipped a full simulation. If
a guarantee matters, it has to hold here.
"""
import json
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog

from blendfleet.accounts import Account, AccountStore
from blendfleet.fleet import Fleet, FleetState, WorkerState
from blendfleet.instance_state import GpuSnapshot, InstanceSnapshot
from blendfleet.kaggle_client import (DatasetInfo, KaggleError, KernelStatus,
                                      Quota)
from blendfleet.settings import Settings
from blendfleet.ui import bridge as bridge_mod
from blendfleet.ui.bridge import Backend


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeClient:
    def __init__(self, token, label=None):
        self.token = token

    def quota(self):
        return Quota(7200, 108000, "soon", "api")


# Every Backend a test builds, kept alive until that test ends.
#
# A Backend parents its worker QThreads to itself. If Python collects the
# Backend while one of those threads still has a deleteLater queued, the
# NEXT processEvents() -- in a later test entirely -- walks freed memory
# and Qt aborts the process rather than raising. Holding a reference and
# tearing down deterministically is the same fix tests/test_dashboard.py
# uses for Dashboards.
_LIVE_BACKENDS = []


def _settle(backend):
    """Join the worker threads and flush what their completion queued.

    wait() alone is not enough: the succeeded/failed connections are
    queued across threads, so without pumping the loop _workers never
    empties and stop() has nothing to wait on. Pumped again AFTER stop()
    so every deleteLater lands while the Backend that owns those objects
    is still alive.
    """
    for worker in list(backend._workers.values()):
        worker.wait(5000)
    for _ in range(30):
        QApplication.processEvents()
    backend.stop()
    for _ in range(30):
        QApplication.processEvents()


@pytest.fixture(autouse=True)
def _close_backends(qapp):
    yield
    while _LIVE_BACKENDS:
        _settle(_LIVE_BACKENDS.pop())


def make_backend(tmp_path, n=2, settings=None):
    store = AccountStore([
        Account(label=f"acct{i}", token=f"KGAT_{i:032x}",
                username=f"user_{i}", verified=(i != 1))
        for i in range(n)])
    factory = lambda accounts: Fleet(  # noqa: E731
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    backend = Backend(store, factory, lambda t: "someone",
                      settings or Settings())
    _LIVE_BACKENDS.append(backend)
    return backend


# ---------------- the payload is ACCOUNT-first ----------------

def test_every_account_appears_even_with_no_render_running(qapp, tmp_path):
    """Kaggle has no idle instances: between renders there is no worker at
    all. A worker-keyed payload would drop every idle account, which is
    most of them most of the time."""
    backend = make_backend(tmp_path, n=3)
    payload = json.loads(backend.state())
    assert [i["label"] for i in payload["instances"]] == \
        ["acct0", "acct1", "acct2"]
    assert all(i["worker"] is None for i in payload["instances"])


def test_idle_is_a_state_not_a_missing_reading(qapp, tmp_path):
    backend = make_backend(tmp_path, n=1)
    instance = json.loads(backend.state())["instances"][0]
    assert instance["worker"] is None
    assert "quota" in instance and "hardware" in instance


def test_quota_and_hardware_survive_having_no_worker(qapp, tmp_path):
    """The bug this file was written after: quota and last-known hardware
    belong to the ACCOUNT, and vanished for every account that was not
    currently rendering."""
    backend = make_backend(tmp_path, n=1)
    backend._quota["acct0"] = "2.0 / 30.0 h"
    backend.instance_store.record("acct0", InstanceSnapshot(
        username="user_0",
        gpus=[GpuSnapshot(index=0, mem_total=16280, model="Tesla P100")],
        cpu_count=4, ram_total=31.3, observed_at=time.time() - 7200))

    instance = json.loads(backend.state())["instances"][0]
    assert instance["worker"] is None
    assert instance["quota"] == "2.0 / 30.0 h"
    assert instance["hardware"]["gpus"][0]["model"] == "Tesla P100"


def test_hardware_always_carries_its_age(qapp, tmp_path):
    """Kaggle reallocates between runs -- a P100 last time does not mean a
    P100 next time. Hardware without an age is a claim this app is not
    entitled to make, so the page must be unable to render one."""
    backend = make_backend(tmp_path, n=1)
    backend.instance_store.record("acct0", InstanceSnapshot(
        username="user_0", gpus=[], cpu_count=4, ram_total=31.3,
        observed_at=time.time() - 3600))
    hardware = json.loads(backend.state())["instances"][0]["hardware"]
    assert "ageSeconds" in hardware
    assert hardware["ageSeconds"] >= 3500


def test_frame_counts_are_flagged_approximate(qapp, tmp_path):
    """The notebook reports how many frames succeeded, not which, so a
    failed frame shifts every later cell for that account. The payload
    says so explicitly rather than leaving the page to assume."""
    backend = make_backend(tmp_path, n=1)
    assert json.loads(backend.state())["approximate"] is True


def test_a_running_worker_is_attached_to_its_account(qapp, tmp_path):
    backend = make_backend(tmp_path, n=2)
    # _state_payload() now reads every tracked job fresh off disk (see its
    # own docstring), rather than a single cached FleetState -- so the job
    # has to actually be SAVED through a Fleet, not just poked onto the
    # Backend in memory.
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([FleetState(
        job_id="job", blend_name="scene.blend", start_frame=1, end_frame=8,
        workers=[WorkerState(label="acct1", username="user_1",
                             kernel_slug="user_1/k", frames=[1, 3, 5],
                             state="running", frames_done=2)])])
    payload = json.loads(backend.state())
    by_label = {i["label"]: i for i in payload["instances"]}
    assert by_label["acct0"]["worker"] is None
    assert by_label["acct1"]["worker"]["state"] == "running"
    assert by_label["acct1"]["worker"]["framesDone"] == 2
    assert payload["job"]["blend"] == "scene.blend"


def test_verified_flag_reaches_the_page(qapp, tmp_path):
    """An unverified account cannot render at all, so the card has to be
    able to say so up front rather than showing it as merely idle."""
    backend = make_backend(tmp_path, n=2)
    by_label = {i["label"]: i for i in json.loads(backend.state())["instances"]}
    assert by_label["acct0"]["verified"] is True
    assert by_label["acct1"]["verified"] is False


# ---------------- preferences round-trip -------------------------------

def test_preferences_expose_theme_accent_and_translucency(qapp, tmp_path):
    backend = make_backend(tmp_path, settings=Settings(accent="blue",
                                                        theme="dark"))
    prefs = json.loads(backend.preferences())
    assert prefs["accent"] == "blue"
    assert prefs["theme"] == "dark"
    assert prefs["translucent"] is False


def test_a_malformed_preference_from_the_page_falls_back(qapp, tmp_path):
    """The page is HTML: it can send anything. setPreference re-runs
    Settings' own validation rather than trusting it, so a page bug lands
    on the same defensive path as a hand-edited settings.json."""
    settings = Settings()
    backend = make_backend(tmp_path, settings=settings)
    backend.setPreference("theme", json.dumps("solarized"))
    assert settings.theme == "light"
    backend.setPreference("accent", json.dumps(["not", "a", "colour"]))
    assert settings.accent == "orange"


def test_the_chosen_blender_version_round_trips_through_the_bridge(qapp, tmp_path):
    """test_the_blender_version_is_remembered (test_settings.py) exercises
    Settings directly and never the UI path, so it can pass while the page
    never actually sends the choice anywhere. This drives it the way the
    page does: through setPreference, then back out through the same
    blenderVersions() slot the picker reads on load."""
    settings = Settings()
    backend = make_backend(tmp_path, settings=settings)
    assert json.loads(backend.blenderVersions())["current"] == "5.2.0"

    backend.setPreference("blenderVersion", json.dumps("4.2.9"))

    assert settings.blender_version == "4.2.9"
    assert json.loads(backend.blenderVersions())["current"] == "4.2.9"
    # setPreference's own save() is what makes it durable across a
    # relaunch -- test_the_blender_version_is_remembered (test_settings.py)
    # already pins that disk round trip at the Settings layer; this test's
    # job is the wiring on top of it: that the page's setPreference call
    # actually reaches settings.blender_version and is reflected back out
    # through blenderVersions().


def test_a_malformed_blender_version_from_the_page_falls_back(qapp, tmp_path):
    """Same defensive path as every other preference: the page is HTML
    that anyone can edit, so a bad value must fall back rather than
    bricking the app or reaching a Kaggle 404 later."""
    settings = Settings()
    backend = make_backend(tmp_path, settings=settings)
    backend.setPreference("blenderVersion", json.dumps("latest"))
    assert settings.blender_version == "5.2.0"


def test_an_unknown_preference_key_is_ignored_not_set(qapp, tmp_path):
    settings = Settings()
    backend = make_backend(tmp_path, settings=settings)
    backend.setPreference("windowOpacity", json.dumps(0.5))
    assert not hasattr(settings, "windowOpacity")


# ---------------- health is measured, not invented ----------------------

def test_health_reports_nothing_measured_before_the_first_poll(qapp, tmp_path):
    backend = make_backend(tmp_path, n=2)
    health = json.loads(backend.health())
    assert health["lastPollMs"] is None
    assert health["lastPollAt"] is None
    assert health["accountsTotal"] == 2
    assert health["accountsReachable"] == 0


def test_health_has_no_packet_loss_field(qapp, tmp_path):
    """The reference design shows a packet-loss row. Nothing in this app
    measures packet loss, and a row permanently reading 0% would be a
    decoration pretending to be an instrument."""
    assert "packetLoss" not in json.loads(make_backend(tmp_path).health())


def test_estimate_carries_the_measurement_it_came_from(qapp, tmp_path):
    """The ETA is an extrapolation from one measured scene. The page must
    be able to show that caveat, so it travels with the number."""
    backend = make_backend(tmp_path, n=2)
    result = json.loads(backend.estimateRender(1, 100))
    assert result["frames"] == 100
    assert result["accounts"] == 2
    assert result["hours"] > 0
    assert "P100" in result["basis"]


# ---------------- the dataset is a step of its own ----------------------

def test_state_says_nothing_is_uploaded_before_a_sync(qapp, tmp_path):
    """"Not uploaded this session" is a different claim from "not on
    Kaggle": the app only knows what it put there itself."""
    payload = json.loads(make_backend(tmp_path).state())
    assert payload["dataset"] is None
    assert payload["blend"] is None


def test_a_synced_dataset_is_reused_when_the_scene_matches(qapp, tmp_path):
    """Re-uploading a scene already on Kaggle is the most expensive thing
    this app can do for no reason."""
    seen = {}

    class RecordingFleet(Fleet):
        def launch(self, blend, settings, start_frame, end_frame,
                   on_progress=None, dataset_slug=None, blender_slug=None,
                   *, accounts=None):
            seen["slug"] = dataset_slug
            return FleetState(job_id="j", blend_name=blend.name,
                              start_frame=start_frame, end_frame=end_frame,
                              workers=[])

    backend = make_backend(tmp_path)
    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x" * 32)
    backend.blend = blend
    backend._dataset = {"slug": "owner/scene-blend", "blendName": "scene.blend",
                        "sizeBytes": 32, "at": "10:00:00"}

    backend.launch(json.dumps({"startFrame": 1, "endFrame": 4}))
    _settle(backend)
    assert seen["slug"] == "owner/scene-blend"


def test_a_dataset_for_a_different_scene_is_not_reused(qapp, tmp_path):
    """A slug left over from another .blend would render the WRONG SCENE
    on somebody else's quota. The name has to match before the upload is
    skipped."""
    seen = {}

    class RecordingFleet(Fleet):
        def launch(self, blend, settings, start_frame, end_frame,
                   on_progress=None, dataset_slug=None, blender_slug=None,
                   *, accounts=None):
            seen["slug"] = dataset_slug
            return FleetState(job_id="j", blend_name=blend.name,
                              start_frame=start_frame, end_frame=end_frame,
                              workers=[])

    backend = make_backend(tmp_path)
    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    blend = tmp_path / "other.blend"
    blend.write_bytes(b"x" * 32)
    backend.blend = blend
    backend._dataset = {"slug": "owner/scene-blend", "blendName": "scene.blend",
                        "sizeBytes": 32, "at": "10:00:00"}

    backend.launch(json.dumps({"startFrame": 1, "endFrame": 4}))
    _settle(backend)
    assert seen["slug"] is None, "reused a dataset built from a different scene"


def test_sync_refuses_without_a_scene_rather_than_guessing(qapp, tmp_path):
    backend = make_backend(tmp_path)
    messages = []
    backend.notification.connect(lambda m, t: messages.append((m, t)))
    backend.syncDataset()
    assert messages and messages[0][1] == "offline"
    assert "dataset" not in backend._workers


# ---------------------------------------------------------------------------
# Live GPU / RAM. These assert against the ACTUAL keys log_stream's parsers
# emit, because the payload is hand-written against them and a renamed key
# would show up as an empty card rather than as an error anywhere.
# ---------------------------------------------------------------------------

def test_telemetry_reaches_the_payload_as_one_row_per_gpu(qapp, tmp_path):
    """Never aggregated: an average across two cards hides one of them
    sitting idle, which is exactly what you need to see."""
    backend = make_backend(tmp_path, n=1)
    for gpu, util in ((0, 91), (1, 12)):
        backend._telemetry_q.put(("acct0", {
            "gpu": gpu, "util": util, "mem_used": 4096, "mem_total": 15360,
            "temp": 61, "power": 70.0}))
    backend._live_tick()

    live = json.loads(backend.state())["instances"][0]["live"]
    assert [g["index"] for g in live["gpus"]] == [0, 1]
    assert [g["util"] for g in live["gpus"]] == [91, 12]
    assert live["gpus"][0]["memUsed"] == 4096
    assert live["gpus"][0]["memTotal"] == 15360


def test_the_keys_match_what_log_stream_actually_parses(qapp, tmp_path):
    """Parses a real TELEMETRY line rather than a hand-built dict, so a
    rename in log_stream.parse_telemetry fails HERE instead of silently
    emptying the card."""
    from blendfleet.log_stream import parse_telemetry

    line = ('data: {"stream_name": "stdout", "data": '
            '"TELEMETRY gpu=0 util=77 mem_used=5000 mem_total=15360'
            ' temp=63 power=71.5"}')
    record = parse_telemetry(line)
    assert record is not None, "the sample line no longer parses"

    backend = make_backend(tmp_path, n=1)
    backend._telemetry_q.put(("acct0", record))
    backend._live_tick()

    gpu = json.loads(backend.state())["instances"][0]["live"]["gpus"][0]
    assert gpu["util"] == 77
    assert gpu["memTotal"] == 15360


def test_cpu_and_ram_from_the_hardware_banner_reach_the_payload(qapp, tmp_path):
    backend = make_backend(tmp_path, n=1)
    backend._hardware_q.put(("acct0", {
        "kind": "cpu_ram", "cpu_count": 4, "ram_total": 31.3}))
    backend._live_tick()

    live = json.loads(backend.state())["instances"][0]["live"]
    assert live["cpuCount"] == 4
    assert live["ramTotal"] == 31.3


def test_preflight_reports_the_hardware_this_session_actually_got(qapp, tmp_path):
    """Distinct from the cached "last known" line: Kaggle reallocates, so
    the two can legitimately disagree and both are shown."""
    backend = make_backend(tmp_path, n=1)
    backend._preflight_q.put(("acct0", {
        "gpu_count": 2, "gpu_names": ["Tesla T4", "Tesla T4"],
        "cpu_count": 4, "ram_total": 31.3}))
    backend._live_tick()

    live = json.loads(backend.state())["instances"][0]["live"]
    assert live["preflight"]["gpu_names"] == ["Tesla T4", "Tesla T4"]


def test_phase_advances_with_what_has_actually_arrived(qapp, tmp_path):
    """The 30s poll can only say queued/running -- Kaggle returns no logs
    until a kernel completes. Phase is the only thing that can say
    "installing Blender", so it must track the evidence."""
    backend = make_backend(tmp_path, n=1)

    backend._preflight_q.put(("acct0", {"gpu_count": 1, "gpu_names": ["P100"],
                                        "cpu_count": 4, "ram_total": 31.3}))
    backend._live_tick()
    assert json.loads(backend.state())["instances"][0]["live"]["phase"] == \
        "checking hardware"

    backend._progress_q.put(("acct0", 7, 15))
    backend._live_tick()
    assert "7/15" in json.loads(backend.state())["instances"][0]["live"]["phase"]


def test_no_live_block_at_all_before_anything_streams(qapp, tmp_path):
    """Between renders there is no session to poll. "No live data" is the
    honest answer -- not last run's numbers presented as current."""
    backend = make_backend(tmp_path, n=1)
    assert json.loads(backend.state())["instances"][0]["live"] is None


# ---------------------------------------------------------------------------
# Upload progress. Every tick reported "0 of 0" because the payload read
# sent_bytes/total_bytes while uploader.UploadProgress calls them uploaded
# and total -- a typo that getattr defaults absorbed silently instead of
# raising. These read the REAL dataclasses so a rename cannot do it again.
# ---------------------------------------------------------------------------

def test_upload_progress_carries_real_byte_counts(qapp, tmp_path):
    from blendfleet.uploader import UploadProgress

    backend = make_backend(tmp_path, n=1)
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x" * 64)
    backend.blend = blend

    seen = []
    backend.uploadProgress.connect(lambda j: seen.append(json.loads(j)))

    class ProgressFleet(Fleet):
        def prepare_dataset(self, blend, on_progress=None, *, clients=None,
                            usernames=None, on_stage=None, required=None):
            on_stage("uploading", "me/scene-blend")
            on_progress(UploadProgress(uploaded=32, total=64,
                                       rate_bps=1024.0, retries=0,
                                       resumed_from=0))
            on_stage("sharing", "friend_1")
            on_stage("ready", "me/scene-blend")
            return "me/scene-blend"

    backend.fleet_factory = lambda accounts: ProgressFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    backend.syncDataset()
    _settle(backend)

    byte_ticks = [s for s in seen if "uploaded" in s]
    assert byte_ticks, f"no byte progress reached the page; saw {seen}"
    assert byte_ticks[0]["uploaded"] == 32
    assert byte_ticks[0]["total"] == 64


def test_every_upload_stage_is_reported_not_just_the_bytes(qapp, tmp_path):
    """The byte counter stops moving during verify, share and
    verify-access. Without a stage name, all three look like "stuck"."""
    from blendfleet.uploader import UploadProgress

    backend = make_backend(tmp_path, n=1)
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x" * 8)
    backend.blend = blend
    seen = []
    backend.uploadProgress.connect(lambda j: seen.append(json.loads(j)))

    class StagedFleet(Fleet):
        def prepare_dataset(self, blend, on_progress=None, *, clients=None,
                            usernames=None, on_stage=None, required=None):
            for key in ("uploading", "verifying", "sharing",
                        "verifying-access", "ready"):
                on_stage(key, "detail")
            return "me/scene-blend"

    backend.fleet_factory = lambda accounts: StagedFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    backend.syncDataset()
    _settle(backend)

    stages = [s["stage"] for s in seen]
    assert stages == ["uploading", "verifying", "sharing",
                      "verifying-access", "ready"], stages


def test_download_progress_carries_real_byte_counts(qapp, tmp_path):
    """The same typo existed on the download side."""
    from blendfleet.downloader import DownloadProgress

    progress = DownloadProgress(downloaded=10, total=20, rate_bps=512.0)
    assert progress.downloaded == 10 and progress.total == 20


# ---------------- previewing one frame ----------------
#
# "can we see one rendered image in our app instead of having to download
# it" (2026-08-12). Exactly one file is fetched, from the account that
# actually rendered that frame.

class PreviewClient(FakeClient):
    """Records what was asked for, and hands back a file that exists."""

    calls: list = []

    def __init__(self, token, label=None):
        super().__init__(token, label)
        self.token = token

    def fetch_one_output(self, slug, filename, dest):
        PreviewClient.calls.append((self.token, slug, filename))
        if not filename.endswith(".png"):
            return None          # the JPEG probe, on a PNG render
        from pathlib import Path
        Path(dest).mkdir(parents=True, exist_ok=True)
        path = Path(dest) / filename
        path.write_bytes(b"\x89PNG fake")
        return path


def _preview_backend(tmp_path, frames_by_label):
    """A Backend with a saved job, whose fleet hands out PreviewClients.

    Preview caching (bridge.py's only state_dir() call site) just needs a
    writable, per-test directory -- conftest.py's autouse redirect_app_dirs
    already gives bridge_mod.state_dir() exactly that, so this no longer
    needs its own override.
    """
    store = AccountStore([
        Account(label=label, token=f"KGAT_{i:032x}", username=f"user_{label}",
                verified=True)
        for i, label in enumerate(frames_by_label)])
    fleet_dir = tmp_path / "w"

    def factory(accounts):
        fleet = Fleet(accounts, lambda t: PreviewClient(t), fleet_dir)
        fleet.load = lambda: FleetState(
            job_id="job1", blend_name="remember.blend",
            start_frame=1, end_frame=6,
            workers=[WorkerState(label=label, username=f"user_{label}",
                                 kernel_slug=f"user_{label}/remember-render-1",
                                 frames=frames, state="complete",
                                 frames_done=len(frames))
                     for label, frames in frames_by_label.items()])
        return fleet

    backend = Backend(store, factory, lambda t: "someone", Settings())
    _LIVE_BACKENDS.append(backend)
    return backend


def test_a_preview_fetches_one_file_from_the_account_that_rendered_it(
        qapp, tmp_path, monkeypatch):
    PreviewClient.calls = []
    backend = _preview_backend(tmp_path, {"a": [1, 3, 5], "b": [2, 4, 6]})
    seen = []
    backend.framePreview.connect(lambda j: seen.append(json.loads(j)))

    backend.previewFrame(4)          # b's frame
    _settle(backend)
    _LIVE_BACKENDS.remove(backend)

    assert seen, "no preview was emitted"
    assert seen[0]["frame"] == 4
    assert seen[0]["label"] == "b"
    slugs = {slug for _tok, slug, _name in PreviewClient.calls}
    assert slugs == {"user_b/remember-render-1"}, \
        "must ask the account that rendered the frame, not the first one"
    assert any(name == "f_0004.png" for _t, _s, name in PreviewClient.calls)


def test_a_preview_never_downloads_the_whole_job(qapp, tmp_path, monkeypatch):
    """The point of the feature: one file, not everyone's output."""
    PreviewClient.calls = []
    backend = _preview_backend(tmp_path, {"a": [1, 3, 5], "b": [2, 4, 6]})
    backend.previewFrame(1)
    _settle(backend)
    _LIVE_BACKENDS.remove(backend)

    names = [name for _t, _s, name in PreviewClient.calls]
    assert names == ["f_0001.png"], names


def test_a_second_look_at_the_same_frame_is_served_from_cache(
        qapp, tmp_path, monkeypatch):
    PreviewClient.calls = []
    backend = _preview_backend(tmp_path, {"a": [1, 2]})
    seen = []
    backend.framePreview.connect(lambda j: seen.append(json.loads(j)))

    backend.previewFrame(1)
    _settle(backend)
    before = len(PreviewClient.calls)
    backend.previewFrame(1)          # again
    _LIVE_BACKENDS.remove(backend)

    assert len(PreviewClient.calls) == before, \
        "clicking the same frame twice must not pay for it twice"
    assert len(seen) == 2, "but it must still be shown the second time"


def test_a_frame_nobody_was_assigned_says_so(qapp, tmp_path, monkeypatch):
    backend = _preview_backend(tmp_path, {"a": [1, 2]})
    notes = []
    backend.notification.connect(lambda m, t: notes.append(m))
    backend.previewFrame(99)
    # Settled BEFORE being dropped from the list, exactly like the two
    # tests below that do the same thing. Removing it without settling it
    # leaves a Backend nobody ever calls stop() on, so its 30-second poll
    # timer keeps firing for the rest of the session -- and a poll that
    # lands in a LATER test builds a Fleet with redirect_app_dirs no longer
    # in effect, i.e. writes to the user's REAL fleet.json (caught by
    # conftest's guard_real_app_dir_untouched).
    _settle(backend)
    _LIVE_BACKENDS.remove(backend)
    assert notes and "not assigned" in notes[0]


def test_preview_frame_is_scoped_to_the_named_job(qapp, tmp_path):
    """Task 7 fix round 1, IMPORTANT: two jobs both rendering frame 2 --
    without a job id, previewFrame() resolved through fleet.load(), "the
    most recent job", so asking for scene ALPHA's frame 2 could silently
    fetch scene BETA's frame 2 instead whenever beta happened to be the
    more recently launched of the two. job_id must pick the job it
    actually names, never by recency."""
    PreviewClient.calls = []
    store = AccountStore([
        Account(label="acct-alpha", token=f"KGAT_{0:032x}",
                username="user_alpha", verified=True),
        Account(label="acct-beta", token=f"KGAT_{1:032x}",
                username="user_beta", verified=True),
    ])
    fleet_dir = tmp_path / "w"
    factory = lambda accounts: Fleet(  # noqa: E731
        accounts, lambda t: PreviewClient(t), fleet_dir)
    factory(store.list()).save_jobs([
        FleetState(job_id="job-alpha", blend_name="alpha.blend",
                   start_frame=1, end_frame=4,
                   workers=[WorkerState(
                       label="acct-alpha", username="user_alpha",
                       kernel_slug="user_alpha/alpha-render-1",
                       frames=[1, 2, 3, 4], state="complete",
                       frames_done=4)]),
        FleetState(job_id="job-beta", blend_name="beta.blend",
                   start_frame=1, end_frame=4,
                   workers=[WorkerState(
                       label="acct-beta", username="user_beta",
                       kernel_slug="user_beta/beta-render-1",
                       frames=[1, 2, 3, 4], state="complete",
                       frames_done=4)]),
    ])

    backend = Backend(store, factory, lambda t: "someone", Settings())
    _LIVE_BACKENDS.append(backend)
    seen = []
    backend.framePreview.connect(lambda j: seen.append(json.loads(j)))

    backend.previewFrame(2, "job-alpha")
    _settle(backend)
    backend.previewFrame(2, "job-beta")
    _settle(backend)
    _LIVE_BACKENDS.remove(backend)

    assert [s["label"] for s in seen] == ["acct-alpha", "acct-beta"], seen
    slugs = {slug for _tok, slug, _name in PreviewClient.calls}
    assert "user_alpha/alpha-render-1" in slugs
    assert "user_beta/beta-render-1" in slugs


def test_preview_frame_falls_back_to_the_most_recent_job_when_unscoped(
        qapp, tmp_path):
    """An empty job id (a caller that only knows a frame number) must keep
    working exactly as before -- resolving through fleet.load()'s own
    "most recent" answer, unchanged."""
    backend = _preview_backend(tmp_path, {"a": [1, 3, 5], "b": [2, 4, 6]})
    seen = []
    backend.framePreview.connect(lambda j: seen.append(json.loads(j)))
    backend.previewFrame(4)          # no job_id at all
    _settle(backend)
    _LIVE_BACKENDS.remove(backend)
    assert seen and seen[0]["label"] == "b"


# ---------------------------------------------------------------------------
# Several jobs at once (Task 6). The payload used to be built from a single
# cached FleetState -- one job, full stop -- so a second concurrent scene
# had nowhere to appear at all.
# ---------------------------------------------------------------------------

def _two_job_backend(tmp_path):
    """4 accounts, two jobs already tracked on disk: "alpha" rendering on
    acct0+acct1, "beta" on acct2 alone. acct3 is rendering nothing."""
    backend = make_backend(tmp_path, n=4)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([
        FleetState(job_id="job-alpha", blend_name="alpha.blend",
                   start_frame=1, end_frame=6,
                   workers=[
                       WorkerState(label="acct0", username="user_0",
                                  kernel_slug="user_0/alpha-render-1",
                                  frames=[1, 2, 3], state="running"),
                       WorkerState(label="acct1", username="user_1",
                                  kernel_slug="user_1/alpha-render-1",
                                  frames=[4, 5, 6], state="running"),
                   ]),
        FleetState(job_id="job-beta", blend_name="beta.blend",
                   start_frame=1, end_frame=5,
                   workers=[
                       WorkerState(label="acct2", username="user_2",
                                  kernel_slug="user_2/beta-render-1",
                                  frames=[1, 2, 3, 4, 5], state="running"),
                   ]),
    ])
    return backend


def test_the_payload_lists_every_job(qapp, tmp_path):
    """One job per scene, each naming the accounts rendering it."""
    backend = _two_job_backend(tmp_path)
    payload = json.loads(backend.state())
    assert [j["scene"] for j in payload["jobs"]] == ["alpha", "beta"]
    assert payload["jobs"][0]["labels"] == ["acct0", "acct1"]
    assert payload["jobs"][1]["labels"] == ["acct2"]


def test_an_instance_says_which_job_it_belongs_to(qapp, tmp_path):
    backend = _two_job_backend(tmp_path)
    payload = json.loads(backend.state())
    by_label = {i["label"]: i for i in payload["instances"]}
    assert by_label["acct0"]["jobId"] != by_label["acct2"]["jobId"]
    assert by_label["acct0"]["jobId"] == by_label["acct1"]["jobId"]


def test_an_idle_account_belongs_to_no_job(qapp, tmp_path):
    backend = _two_job_backend(tmp_path)
    payload = json.loads(backend.state())
    by_label = {i["label"]: i for i in payload["instances"]}
    assert by_label["acct3"]["jobId"] is None
    assert by_label["acct3"]["worker"] is None


# ---------------------------------------------------------------------------
# Jobs this app can no longer read at all (beyond the brief, per the task's
# own instructions): a broken record may still be a kernel running and
# billing on Kaggle that nothing here can cancel or collect any more.
# ---------------------------------------------------------------------------

def test_an_unreadable_job_names_its_kernels_and_points_at_kaggle(
        qapp, tmp_path):
    backend = make_backend(tmp_path, n=1)
    fleet = backend.fleet_factory(backend.store.list())
    # A field FleetState(**parsed) does not accept makes load_jobs() give
    # up on this entry -- and preserve it VERBATIM on unreadable_jobs
    # rather than dropping it (see Fleet.load_jobs()'s own docstring).
    fleet._state_path().write_text(json.dumps({"jobs": [{
        "job_id": "bad", "blend_name": "x.blend",
        "workers": [{"label": "acct9", "username": "u9",
                     "kernel_slug": "u9/x-render-1", "frames": [1]}],
        "not_a_real_field": 1,
    }]}), encoding="utf-8")

    entries = json.loads(backend.state())["unreadableJobs"]
    assert len(entries) == 1
    assert entries[0]["kernels"] == ["u9/x-render-1"]
    assert "kaggle.com" in entries[0]["message"]
    # Fix round 1, Minor 1: job_id/blend_name are read from the raw entry
    # when present, and a kernel slug becomes the actual page to open.
    assert entries[0]["jobId"] == "bad"
    assert entries[0]["blend"] == "x.blend"
    assert entries[0]["kernelUrls"] == \
        ["https://www.kaggle.com/code/u9/x-render-1"]
    assert "x.blend" in entries[0]["message"]


def test_two_unreadable_jobs_get_two_different_messages(qapp, tmp_path):
    """Fix round 1, Minor 1: discarding job_id/blend_name made every
    unreadable entry read as the identical generic sentence -- with more
    than one on screen at once there was no way to tell them apart."""
    backend = make_backend(tmp_path, n=1)
    fleet = backend.fleet_factory(backend.store.list())
    fleet._state_path().write_text(json.dumps({"jobs": [
        {"job_id": "bad-1", "blend_name": "one.blend", "workers": [],
         "not_a_real_field": 1},
        {"job_id": "bad-2", "blend_name": "two.blend", "workers": [],
         "not_a_real_field": 1},
    ]}), encoding="utf-8")

    entries = json.loads(backend.state())["unreadableJobs"]
    assert len(entries) == 2
    assert entries[0]["message"] != entries[1]["message"]
    assert entries[0]["index"] == 0 and entries[1]["index"] == 1


def test_a_totally_unparseable_state_file_says_so_without_a_kernel_list(
        qapp, tmp_path):
    """The raw text itself couldn't be read as JSON at all -- there is
    nothing here to recover a kernel slug from, and the message must not
    invent one."""
    backend = make_backend(tmp_path, n=1)
    fleet = backend.fleet_factory(backend.store.list())
    fleet._state_path().write_text("{not json at all", encoding="utf-8")

    entries = json.loads(backend.state())["unreadableJobs"]
    assert len(entries) == 1
    assert entries[0]["kernels"] == []
    assert "kaggle.com" in entries[0]["message"]


def test_no_unreadable_jobs_when_the_state_file_is_clean(qapp, tmp_path):
    backend = _two_job_backend(tmp_path)
    assert json.loads(backend.state())["unreadableJobs"] == []


def test_forgetting_an_unreadable_job_clears_it_from_the_payload(
        qapp, tmp_path):
    """Fix round 1, Important 3: an unreadable entry used to have no way
    to be dismissed at all -- save_jobs() re-writes it verbatim on every
    save, and forgetJob() only ever pops a PARSED job by position, never
    reaching self.unreadable_jobs. Once the user has gone and dealt with
    it by hand at kaggle.com, forgetUnreadableJob() must be able to clear
    the warning."""
    backend = make_backend(tmp_path, n=1)
    fleet = backend.fleet_factory(backend.store.list())
    fleet._state_path().write_text(json.dumps({"jobs": [{
        "job_id": "bad", "blend_name": "x.blend",
        "workers": [{"label": "acct9", "username": "u9",
                     "kernel_slug": "u9/x-render-1", "frames": [1]}],
        "not_a_real_field": 1,
    }]}), encoding="utf-8")
    entries = json.loads(backend.state())["unreadableJobs"]
    assert len(entries) == 1

    backend.forgetUnreadableJob(0, entries[0]["fingerprint"])
    _settle(backend)

    assert json.loads(backend.state())["unreadableJobs"] == []
    # A save-then-load round trip (poll(), _save(), ...) must not bring
    # the forgotten entry back -- see save_jobs()'s own "carries it
    # through on every write" contract, which this call has to survive
    # having actually removed the entry from.
    fleet2 = backend.fleet_factory(backend.store.list())
    fleet2.load_jobs()
    assert fleet2.unreadable_jobs == []


def test_forgetting_an_unreadable_job_does_not_touch_a_real_one(
        qapp, tmp_path):
    """The two lists (parsed jobs, unreadable entries) are independent --
    forgetting index 0 of one must never remove the other."""
    backend = _two_job_backend(tmp_path)
    fleet = backend.fleet_factory(backend.store.list())
    # Append one unreadable entry alongside the two good jobs already
    # seeded by _two_job_backend().
    raw = json.loads(fleet._state_path().read_text(encoding="utf-8"))
    raw["jobs"].append({"job_id": "bad", "blend_name": "x.blend",
                        "workers": [], "not_a_real_field": 1})
    fleet._state_path().write_text(json.dumps(raw), encoding="utf-8")

    before = json.loads(backend.state())
    assert len(before["jobs"]) == 2
    assert len(before["unreadableJobs"]) == 1

    backend.forgetUnreadableJob(0, before["unreadableJobs"][0]["fingerprint"])
    _settle(backend)

    after = json.loads(backend.state())
    assert [j["scene"] for j in after["jobs"]] == ["alpha", "beta"]
    assert after["unreadableJobs"] == []


def test_forgetting_an_already_gone_unreadable_index_says_so(
        qapp, tmp_path):
    backend = make_backend(tmp_path, n=1)
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))
    backend.forgetUnreadableJob(0, "")   # nothing tracked at all
    _settle(backend)
    assert notes and notes[0][1] == "idle"


def test_forgetting_an_unreadable_job_refuses_when_the_list_has_changed(
        qapp, tmp_path):
    """Fix round 2: save_jobs() writes unreadable entries LAST, so a
    DIFFERENT job going unreadable between the page reading its payload
    and the user clicking "forget" can shift every later unreadable
    entry's position by one. Forgetting by `index` alone would then
    silently drop the WRONG record -- along with the one kernel slug the
    user actually needed. A stale fingerprint must refuse, not guess."""
    backend = make_backend(tmp_path, n=1)
    fleet = backend.fleet_factory(backend.store.list())
    fleet._state_path().write_text(json.dumps({"jobs": [
        {"job_id": "first", "blend_name": "a.blend", "workers": [],
         "not_a_real_field": 1},
    ]}), encoding="utf-8")
    entries = json.loads(backend.state())["unreadableJobs"]
    stale_fingerprint = entries[0]["fingerprint"]

    # A SECOND job goes unreadable before the user clicks "forget" --
    # inserted at position 0, pushing "first" to position 1.
    raw = json.loads(fleet._state_path().read_text(encoding="utf-8"))
    raw["jobs"].insert(0, {"job_id": "second", "blend_name": "b.blend",
                           "workers": [], "not_a_real_field": 1})
    fleet._state_path().write_text(json.dumps(raw), encoding="utf-8")

    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))
    backend.forgetUnreadableJob(0, stale_fingerprint)   # still index 0
    _settle(backend)

    assert notes and notes[0][1] == "offline"
    assert "changed" in notes[0][0].lower()
    # Nothing was dropped -- both records must still be there.
    still_there = json.loads(backend.state())["unreadableJobs"]
    assert {e["jobId"] for e in still_there} == {"first", "second"}


# ---------------------------------------------------------------------------
# unshared_accounts (beyond the brief): Fleet.unshared_accounts lives on a
# Fleet object this app builds and discards per call, and only ever
# describes the LAST upload -- both facts have to survive into the payload.
# ---------------------------------------------------------------------------

def test_unshared_accounts_reach_the_payload_as_last_upload_info(
        qapp, tmp_path):
    backend = make_backend(tmp_path, n=1)
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x" * 8)
    backend.blend = blend

    class UnshareableFleet(Fleet):
        def prepare_dataset(self, blend, on_progress=None, *, clients=None,
                            usernames=None, on_stage=None, required=None):
            self.unshared_accounts = {
                "friend_1": "could not be reached to share the scene with"}
            return "me/scene-blend"

    backend.fleet_factory = lambda accounts: UnshareableFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    backend.syncDataset()
    _settle(backend)

    unshared = json.loads(backend.state())["unshared"]
    assert unshared["accounts"] == {
        "friend_1": "could not be reached to share the scene with"}
    # Its meaning has to be explicit -- the page must not be able to
    # present this as a live, current-dataset check.
    assert "last" in unshared["note"] or "not" in unshared["note"]


def test_no_unshared_block_before_any_upload_this_session(qapp, tmp_path):
    backend = make_backend(tmp_path, n=1)
    assert json.loads(backend.state())["unshared"] is None


# ---------------------------------------------------------------------------
# launch() onto chosen accounts (the brief's own interface change).
# ---------------------------------------------------------------------------

def _account_workers_fleet():
    class RecordingFleet(Fleet):
        def launch(self, blend, settings, start_frame, end_frame,
                   on_progress=None, dataset_slug=None, blender_slug=None,
                   *, accounts=None):
            RecordingFleet.seen_labels = [a.label for a in accounts]
            return FleetState(
                job_id="j", blend_name=blend.name,
                start_frame=start_frame, end_frame=end_frame,
                workers=[WorkerState(label=a.label, username=a.label,
                                     kernel_slug=f"{a.label}/k", frames=[1])
                         for a in accounts])
    return RecordingFleet


def test_launch_uses_only_the_chosen_labels(qapp, tmp_path):
    RecordingFleet = _account_workers_fleet()
    backend = make_backend(tmp_path, n=3)
    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x" * 8)
    backend.blend = blend

    backend.launch(json.dumps({"startFrame": 1, "endFrame": 4,
                              "labels": ["acct1"]}))
    _settle(backend)
    assert RecordingFleet.seen_labels == ["acct1"]


def test_launch_with_no_labels_only_uses_free_accounts(qapp, tmp_path):
    """A second launch with nothing selected must never ask to render on
    an account a first launch is still using."""
    backend = make_backend(tmp_path, n=2)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([FleetState(
        job_id="job1", blend_name="other.blend", start_frame=1, end_frame=5,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/other-render-1",
                             frames=[1, 2], state="running")])])

    RecordingFleet = _account_workers_fleet()
    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x" * 8)
    backend.blend = blend

    backend.launch(json.dumps({"startFrame": 1, "endFrame": 4}))
    _settle(backend)
    assert RecordingFleet.seen_labels == ["acct1"]


def test_launch_refuses_an_explicitly_empty_label_list(qapp, tmp_path):
    """Fix round 1, Critical: `"labels": []` means every per-instance
    checkbox was unticked -- a caller explicitly asking for nobody -- and
    is a DIFFERENT request from the key being absent entirely (which means
    "whatever is free"). `or` used to collapse the two, so this launched
    on every free account instead of refusing -- exactly the widening
    Fleet.launch's own `accounts=[]` guard (fleet.py) exists to prevent,
    reopened one layer up because this slot resolves accounts and calls
    fleet.launch() before that guard ever runs.
    """
    RecordingFleet = _account_workers_fleet()
    backend = make_backend(tmp_path, n=3)
    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x" * 8)
    backend.blend = blend
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.launch(json.dumps({"startFrame": 1, "endFrame": 4,
                              "labels": []}))
    _settle(backend)

    assert getattr(RecordingFleet, "seen_labels", None) is None, \
        "must not launch on ANY account when labels is explicitly empty"
    assert notes and notes[0][1] == "offline"
    assert "launch" not in backend._workers


def test_launch_refuses_an_unknown_label(qapp, tmp_path):
    backend = make_backend(tmp_path, n=2)
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x" * 8)
    backend.blend = blend
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.launch(json.dumps({"startFrame": 1, "endFrame": 4,
                              "labels": ["ghost"]}))
    assert notes and notes[0][1] == "offline"
    assert "ghost" in notes[0][0]
    assert "launch" not in backend._workers


def test_launch_refuses_when_every_account_is_already_busy(qapp, tmp_path):
    backend = make_backend(tmp_path, n=1)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([FleetState(
        job_id="job1", blend_name="other.blend", start_frame=1, end_frame=5,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/other-render-1",
                             frames=[1, 2], state="running")])])
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x" * 8)
    backend.blend = blend
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.launch(json.dumps({"startFrame": 1, "endFrame": 4}))
    assert notes and notes[0][1] == "offline"
    assert "busy" in notes[0][0].lower() or "rendering" in notes[0][0].lower()
    assert "launch" not in backend._workers


# ---------------------------------------------------------------------------
# Must-fix 1: cancelJob(job_id) -- Fleet.cancel_job() was built in Task 5
# for exactly this and had ZERO production callers until now. The page's
# per-job Cancel button used to loop cancelInstance() (-> Fleet.cancel_worker()
# -> load()'s single newest job) once per account, so cancelling the OLDER
# of two live jobs cancelled nothing and reported "already stopped" while
# that job's kernels kept running and billing.
# ---------------------------------------------------------------------------

def _two_job_backend_for_cancel(tmp_path):
    """acct0 renders job-old (still running); acct1 renders job-new
    (also still running) -- two LIVE jobs at once, the exact shape
    per-job Cancel exists for."""
    backend = make_backend(tmp_path, n=2)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([
        FleetState(job_id="job-old", blend_name="alpha.blend",
                  start_frame=1, end_frame=3,
                  workers=[WorkerState(label="acct0", username="user_0",
                                       kernel_slug="user_0/alpha-render-1",
                                       frames=[1, 2, 3], state="running")]),
        FleetState(job_id="job-new", blend_name="beta.blend",
                  start_frame=1, end_frame=2,
                  workers=[WorkerState(label="acct1", username="user_1",
                                       kernel_slug="user_1/beta-render-1",
                                       frames=[1, 2], state="running")]),
    ])
    return backend


def test_cancel_job_stops_only_that_jobs_own_accounts(qapp, tmp_path):
    """Cancelling job-old (the OLDER of two live jobs) must reach acct0's
    kernel and must never touch acct1's still-running job-new -- exactly
    the case where the old cancelInstance()-loop wiring found nothing."""
    backend = _two_job_backend_for_cancel(tmp_path)
    cancelled = []

    class RecordingClient:
        def __init__(self, token):
            self.token = token
        def cancel(self, slug):
            cancelled.append(slug)
            return True
        def status(self, slug):
            from blendfleet.kaggle_client import KernelStatus
            return KernelStatus(state="running")

    fleet = backend.fleet_factory(backend.store.list())
    fleet.client_factory = RecordingClient
    backend.fleet_factory = lambda accounts: fleet

    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.cancelJob("job-old")
    _settle(backend)

    assert cancelled == ["user_0/alpha-render-1"], (
        f"must cancel exactly job-old's own kernel, not job-new's: {cancelled}")
    assert notes and "1" in notes[0][0] and "Cancelled" in notes[0][0]


def test_cancel_job_reports_a_failed_cancel_by_name(qapp, tmp_path):
    """A silently-failed per-job cancel is the same worst-case outcome as
    a silently-failed cancelAll() -- must be named, never swallowed."""
    backend = _two_job_backend_for_cancel(tmp_path)

    class RefusingClient:
        def __init__(self, token):
            self.token = token
        def cancel(self, slug):
            return False
        def status(self, slug):
            from blendfleet.kaggle_client import KernelStatus
            return KernelStatus(state="running")

    fleet = backend.fleet_factory(backend.store.list())
    fleet.client_factory = RefusingClient
    backend.fleet_factory = lambda accounts: fleet

    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.cancelJob("job-old")
    _settle(backend)

    assert notes
    assert notes[0][1] == "offline"
    assert "acct0" in notes[0][0]


# ---------------------------------------------------------------------------
# collect() -- scoped to exactly ONE job (Fix round 1, Important 1+2):
# `load_jobs()` is never pruned except one job at a time via forget_job(),
# so collecting from EVERY tracked job with no filter grows unbounded and
# re-downloads jobs that finished long ago; merging their reports with
# dict.update() also silently drops one job's worker_errors behind
# another's for the same label. Scoping to one job at a time removes both
# problems by construction.
# ---------------------------------------------------------------------------

def _two_job_collect_backend(tmp_path):
    """acct0 finished "job-a" (older); acct1 finished "job-b" (newer)."""
    backend = make_backend(tmp_path, n=2)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([
        FleetState(job_id="job-a", blend_name="alpha.blend",
                   start_frame=1, end_frame=3,
                   workers=[WorkerState(label="acct0", username="user_0",
                                        kernel_slug="user_0/alpha-render-1",
                                        frames=[1, 2, 3], state="complete")]),
        FleetState(job_id="job-b", blend_name="beta.blend",
                   start_frame=1, end_frame=2,
                   workers=[WorkerState(label="acct1", username="user_1",
                                        kernel_slug="user_1/beta-render-1",
                                        frames=[1, 2], state="complete")]),
    ])
    return backend


def _stub_collect_frames(monkeypatch, tmp_path, seen_job_ids):
    import blendfleet.collector as collector_mod
    from blendfleet.collector import CollectReport

    def fake_collect(state, accounts, client_factory, dest, *,
                     worker_label=None, on_progress=None):
        seen_job_ids.append(state.job_id)
        return CollectReport(copied=len(state.workers[0].frames))

    monkeypatch.setattr(collector_mod, "collect", fake_collect)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: str(tmp_path / "out"))


def test_collect_with_no_label_only_reaches_the_most_recent_job(
        qapp, tmp_path, monkeypatch):
    """With neither `label` nor `job_id` given, exactly ONE job is
    collected -- the most recent -- matching `Fleet.load()`'s own answer
    from before several jobs could be tracked at once. Never every job
    this app has ever tracked."""
    backend = _two_job_collect_backend(tmp_path)
    seen_job_ids = []
    _stub_collect_frames(monkeypatch, tmp_path, seen_job_ids)

    backend.collect("")
    _settle(backend)

    assert seen_job_ids == ["job-b"], \
        "must collect only the most recent job, not every tracked job"


def test_collect_with_a_label_finds_its_job_even_if_not_the_newest(
        qapp, tmp_path, monkeypatch):
    """A label search is never ambiguous (an account renders in at most
    one job at a time) and is unaffected by this fix -- it must keep
    finding an OLDER job's worker, not just the newest job's."""
    backend = _two_job_collect_backend(tmp_path)
    seen_job_ids = []
    _stub_collect_frames(monkeypatch, tmp_path, seen_job_ids)

    backend.collect("acct0")            # acct0 is in the OLDER job
    _settle(backend)

    assert seen_job_ids == ["job-a"]


def test_collect_with_a_label_in_two_jobs_reaches_the_newer_one(
        qapp, tmp_path, monkeypatch):
    """Must-fix 4: `label` used to take the FIRST match over an
    oldest-first list (`load_jobs()`'s own order), so once an account had
    rendered twice, every per-instance Download button silently collected
    the OLDER job's frames forever -- the docstring's own justification
    ("an account belongs to at most one job at a time") stopped being
    true the moment the job list became append-only. The test above
    (disjoint labels) cannot see this at all; this one puts acct0 in
    BOTH jobs."""
    backend = make_backend(tmp_path, n=2)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([
        FleetState(job_id="job-old", blend_name="alpha.blend",
                  start_frame=1, end_frame=3,
                  workers=[WorkerState(label="acct0", username="user_0",
                                       kernel_slug="user_0/alpha-render-1",
                                       frames=[1, 2, 3], state="complete")]),
        FleetState(job_id="job-new", blend_name="beta.blend",
                  start_frame=1, end_frame=2,
                  workers=[WorkerState(label="acct0", username="user_0",
                                       kernel_slug="user_0/beta-render-1",
                                       frames=[1, 2], state="complete")]),
    ])
    seen_job_ids = []
    _stub_collect_frames(monkeypatch, tmp_path, seen_job_ids)

    backend.collect("acct0")
    _settle(backend)

    assert seen_job_ids == ["job-new"], (
        "acct0 rendered twice -- Download must reach the NEWER job "
        f"(job-new), not the older one it silently found instead: {seen_job_ids}")


def test_collect_with_an_explicit_job_id_reaches_that_job(
        qapp, tmp_path, monkeypatch):
    """Task 7's per-job collect button will carry a job id directly --
    the right way to reach a specific older job, rather than a fleet-wide
    button guessing at every job it has ever tracked."""
    backend = _two_job_collect_backend(tmp_path)
    seen_job_ids = []
    _stub_collect_frames(monkeypatch, tmp_path, seen_job_ids)

    backend.collect("", "job-a")
    _settle(backend)

    assert seen_job_ids == ["job-a"]


def test_collect_does_not_leak_worker_errors_between_jobs(
        qapp, tmp_path, monkeypatch):
    """The bug a fleet-wide merge produced: dict.update() let a newer
    job's worker_errors for one label silently replace an older job's,
    for a DIFFERENT label, the moment both jobs were collected in one
    call. Scoped to one job at a time, this cannot happen -- collecting
    the older job on its own must still report ITS OWN error."""
    import blendfleet.collector as collector_mod
    from blendfleet.collector import CollectReport

    backend = _two_job_collect_backend(tmp_path)

    def fake_collect(state, accounts, client_factory, dest, *,
                     worker_label=None, on_progress=None):
        if state.job_id == "job-a":
            return CollectReport(
                worker_errors={"acct0": "could not reach acct0"})
        return CollectReport(worker_errors={"acct1": "token revoked"})

    monkeypatch.setattr(collector_mod, "collect", fake_collect)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: str(tmp_path / "out"))

    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))
    backend.collect("", "job-a")
    _settle(backend)

    assert notes and "could not reach acct0" in notes[0][0]


# ---------------------------------------------------------------------------
# Fix round 2: the collect() busy key is BUTTON identity, not call
# identity. app.js matches it by an EXACT string ("collect:" for the
# fleet-wide button); folding job_id into the key broke that match.
# ---------------------------------------------------------------------------

def test_collect_busy_key_for_the_fleet_wide_button_is_exactly_collect(
        qapp, tmp_path, monkeypatch):
    backend = _two_job_collect_backend(tmp_path)
    seen_job_ids = []
    _stub_collect_frames(monkeypatch, tmp_path, seen_job_ids)
    keys = []
    backend.busyChanged.connect(lambda key, busy: keys.append(key))

    backend.collect("")
    _settle(backend)

    assert "collect:" in keys, \
        f"app.js matches this key by exact string 'collect:'; saw {keys}"


def test_collect_busy_key_for_one_account_carries_only_its_label(
        qapp, tmp_path, monkeypatch):
    backend = _two_job_collect_backend(tmp_path)
    seen_job_ids = []
    _stub_collect_frames(monkeypatch, tmp_path, seen_job_ids)
    keys = []
    backend.busyChanged.connect(lambda key, busy: keys.append(key))

    backend.collect("acct0")
    _settle(backend)

    assert "collect:acct0" in keys, \
        f"job_id must not be folded into the busy key; saw {keys}"


# ---------------------------------------------------------------------------
# Fix round 2: startInstances() had the SAME absent-vs-explicitly-empty
# collapse the launch() Critical fixed. A warm machine spends quota from
# the moment it starts, so this is the same class of harm.
# ---------------------------------------------------------------------------

def test_start_instances_with_no_argument_starts_every_account(
        qapp, tmp_path):
    """Today's "Start all" button's own call (an empty STRING) must keep
    meaning "every configured account", unchanged."""
    backend = make_backend(tmp_path, n=2)
    backend._dataset = {"slug": "owner/scene-blend", "blendName": "scene.blend",
                        "sizeBytes": 8, "at": "10:00:00"}
    seen = {}

    class RecordingFleet(Fleet):
        def start_workers(self, labels, settings, dataset_slug,
                          blender_slug=None):
            seen["labels"] = list(labels)
            return FleetState(job_id="j", blend_name="", start_frame=0,
                              end_frame=0,
                              workers=[WorkerState(label=l, username=l,
                                                   kernel_slug=f"{l}/k",
                                                   frames=[])
                                       for l in labels])

    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")

    backend.startInstances("")
    _settle(backend)

    assert sorted(seen["labels"]) == ["acct0", "acct1"]


def test_start_instances_refuses_an_explicitly_empty_label_list(
        qapp, tmp_path):
    """`"[]"` means every per-instance checkbox was unticked -- the same
    request `launch()`'s Critical fix already refuses, not "start
    everyone". A warm machine spends quota from the moment it starts, so
    silently widening this is the same class of harm."""
    backend = make_backend(tmp_path, n=2)
    backend._dataset = {"slug": "owner/scene-blend", "blendName": "scene.blend",
                        "sizeBytes": 8, "at": "10:00:00"}
    seen = {}

    class RecordingFleet(Fleet):
        def start_workers(self, labels, settings, dataset_slug,
                          blender_slug=None):
            seen["labels"] = list(labels)
            return FleetState(job_id="j", blend_name="", start_frame=0,
                              end_frame=0, workers=[])

    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.startInstances(json.dumps([]))
    _settle(backend)

    assert "labels" not in seen, \
        "must not start ANY machine when the selection is explicitly empty"
    assert notes and notes[0][1] == "offline"
    assert "start" not in backend._workers


def test_start_instances_with_named_labels_starts_only_those(
        qapp, tmp_path):
    backend = make_backend(tmp_path, n=3)
    backend._dataset = {"slug": "owner/scene-blend", "blendName": "scene.blend",
                        "sizeBytes": 8, "at": "10:00:00"}
    seen = {}

    class RecordingFleet(Fleet):
        def start_workers(self, labels, settings, dataset_slug,
                          blender_slug=None):
            seen["labels"] = list(labels)
            return FleetState(job_id="j", blend_name="", start_frame=0,
                              end_frame=0,
                              workers=[WorkerState(label=l, username=l,
                                                   kernel_slug=f"{l}/k",
                                                   frames=[])
                                       for l in labels])

    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")

    backend.startInstances(json.dumps(["acct1"]))
    _settle(backend)

    assert seen["labels"] == ["acct1"]


# ---------------------------------------------------------------------------
# Fix round 2: sendJob() silently ignored `options["labels"]` -- a
# parameter that looked respected (launch() reads the identical key out
# of the identical shape of `options`) but was not.
# ---------------------------------------------------------------------------

def _warm_backend(tmp_path, labels):
    backend = make_backend(tmp_path, n=len(labels))
    backend._last_state = FleetState(
        job_id="warm", blend_name="", start_frame=0, end_frame=0,
        workers=[WorkerState(label=l, username=l, kernel_slug=f"{l}/k",
                             frames=[]) for l in labels])
    return backend


def test_send_job_with_no_labels_reaches_every_warm_machine(qapp, tmp_path):
    backend = _warm_backend(tmp_path, ["acct0", "acct1"])
    published = []

    class RecordingFleet(Fleet):
        def publish_job(self, job):
            published.append(dict(job))

    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")

    backend.sendJob(json.dumps({"startFrame": 1, "endFrame": 4}))
    _settle(backend)

    assert {j["workers"][0] for j in published} == {"acct0", "acct1"}


def test_send_job_honours_an_explicit_label_selection(qapp, tmp_path):
    """The Minor this round's review flagged: `labels` must actually be
    respected, not silently ignored while looking like it is (launch()
    reads the exact same key out of the exact same `options` shape)."""
    backend = _warm_backend(tmp_path, ["acct0", "acct1"])
    published = []

    class RecordingFleet(Fleet):
        def publish_job(self, job):
            published.append(dict(job))

    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")

    backend.sendJob(json.dumps({"startFrame": 1, "endFrame": 4,
                               "labels": ["acct0"]}))
    _settle(backend)

    assert {j["workers"][0] for j in published} == {"acct0"}


def test_send_job_refuses_an_explicitly_empty_label_list(qapp, tmp_path):
    backend = _warm_backend(tmp_path, ["acct0", "acct1"])
    published = []

    class RecordingFleet(Fleet):
        def publish_job(self, job):
            published.append(dict(job))

    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.sendJob(json.dumps({"startFrame": 1, "endFrame": 4,
                               "labels": []}))
    _settle(backend)

    assert not published
    assert notes and notes[0][1] == "offline"


def test_send_job_refuses_a_label_that_is_not_currently_warm(qapp, tmp_path):
    backend = _warm_backend(tmp_path, ["acct0", "acct1"])
    published = []

    class RecordingFleet(Fleet):
        def publish_job(self, job):
            published.append(dict(job))

    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.sendJob(json.dumps({"startFrame": 1, "endFrame": 4,
                               "labels": ["acct0", "ghost"]}))
    _settle(backend)

    assert not published
    assert notes and notes[0][1] == "offline"
    assert "ghost" in notes[0][0]


# ---------------------------------------------------------------------------
# The scene library: scenes(), renderScene(), deleteScene() (Task 11).
#
# scenes_from_datasets() (blendfleet/scenes.py) is a pure filter with no
# idea which account anything came from -- it is handed a flat list of
# DatasetInfo. Pooling every configured account's own list_datasets()
# before handing them to it is this bridge's own job, and so is making
# sure one account's failure never hides everyone else's scenes.
# ---------------------------------------------------------------------------

class SceneListingClient(FakeClient):
    """A FakeClient whose list_datasets() answers per-account, keyed by
    the token it was constructed with (make_backend gives every fake
    account a distinct token) -- an Exception value is raised instead of
    returned, so one entry can simulate an unreachable account."""

    def __init__(self, token, datasets_by_token, label=None):
        super().__init__(token)
        self._by_token = datasets_by_token

    def list_datasets(self):
        entry = self._by_token.get(self.token, [])
        if isinstance(entry, Exception):
            raise entry
        return entry


def _dataset(owner, stem, size=1000, updated=None):
    return DatasetInfo(ref=f"{owner}/{stem}-blend", title=f"{stem}-blend",
                       total_bytes=size, last_updated=updated,
                       is_private=True, owner=owner)


def test_one_unreachable_account_does_not_empty_the_library(qapp, tmp_path):
    """Its error is attached and the rest still show -- the same
    discipline CollectReport.worker_errors already follows."""
    backend = make_backend(tmp_path, n=3)
    accounts = backend.store.list()
    by_token = {
        accounts[0].token: [_dataset("user_0", "remember")],
        accounts[1].token: KaggleError("could not list datasets: rate limited"),
        accounts[2].token: [_dataset("user_2", "another")],
    }
    backend.fleet_factory = lambda accts: Fleet(
        accts, lambda t: SceneListingClient(t, by_token), tmp_path / "w")

    payloads = []
    backend.scenesChanged.connect(lambda j: payloads.append(json.loads(j)))
    backend.scenes()
    _settle(backend)

    assert len(payloads) == 1
    payload = payloads[0]
    names = {s["name"] for s in payload["scenes"]}
    # acct1's failure must not have hidden acct0's or acct2's own scenes.
    assert names == {"remember", "another"}
    assert "acct1" in payload["errors"]
    assert "rate limited" in payload["errors"]["acct1"]


def test_scenes_payload_never_invents_an_update_date(qapp, tmp_path):
    """Scene.updated is datetime | None -- an undated scene must reach
    the page as `null`, never as some default timestamp standing in for
    'we don't know'."""
    backend = make_backend(tmp_path, n=1)
    accounts = backend.store.list()
    by_token = {accounts[0].token: [_dataset("user_0", "remember", size=999)]}
    backend.fleet_factory = lambda accts: Fleet(
        accts, lambda t: SceneListingClient(t, by_token), tmp_path / "w")

    payloads = []
    backend.scenesChanged.connect(lambda j: payloads.append(json.loads(j)))
    backend.scenes()
    _settle(backend)

    scene = payloads[0]["scenes"][0]
    assert scene["updated"] is None
    assert scene["owner"] == "user_0"
    assert scene["sizeBytes"] == 999
    # A GUESS, carried through verbatim -- never dressed up as confirmed.
    assert scene["blendName"] == "remember.blend"


def test_deleting_uses_the_owners_token_never_a_friends(qapp, tmp_path):
    """A friend's token cannot delete another account's dataset, and
    trying produces a permission error that reads like a bug."""
    backend = make_backend(tmp_path, n=2)
    accounts = backend.store.list()   # user_0 (owner), user_1 (friend)
    calls = []

    class DeleteClient(FakeClient):
        def delete_dataset(self, slug):
            calls.append(self.token)
            if self.token != accounts[0].token:
                # What Kaggle itself does to a non-owner's token -- see
                # KaggleClient.delete_dataset's own docstring. Only
                # reachable here if the bridge picked the wrong account.
                raise KaggleError(
                    f"could not delete dataset {slug!r}: Kaggle refused "
                    "with a permission error. Only the dataset's OWNER "
                    "can delete it -- a collaborator's token is refused "
                    "even with full read access.")

    backend.fleet_factory = lambda accts: Fleet(
        accts, lambda t: DeleteClient(t), tmp_path / "w")
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.deleteScene("user_0/remember-blend")
    _settle(backend)

    assert calls == [accounts[0].token], \
        "must call delete_dataset with the OWNER's token, never a friend's"
    assert notes and notes[0][1] == "idle"
    assert "Deleted" in notes[0][0]


def test_deleting_refuses_while_a_tracked_job_is_still_rendering_it(
        qapp, tmp_path):
    """Fix round 1, Minor: every other consequential action here goes
    through a require_free-style guard (launch(), renderScene(), ...) --
    deleteScene() previously never checked at all, even though deleting a
    dataset out from under an active render can break that render for
    every account using it."""
    backend = make_backend(tmp_path, n=1)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([FleetState(
        job_id="job1", blend_name="remember.blend",
        start_frame=1, end_frame=4,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/remember-render-1",
                             frames=[1, 2], state="running")])])
    calls = []

    class DeleteClient(FakeClient):
        def delete_dataset(self, slug):
            calls.append(slug)

    backend.fleet_factory = lambda accts: Fleet(
        accts, lambda t: DeleteClient(t), tmp_path / "w")
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.deleteScene("user_0/remember-blend")

    assert calls == [], \
        "must not delete while a tracked job is actively rendering it"
    assert notes and notes[0][1] == "offline"
    assert "acct0" in notes[0][0]
    assert "delete-scene:user_0/remember-blend" not in backend._workers


def test_deleting_refuses_while_a_tracked_job_is_not_started_yet(
        qapp, tmp_path):
    """A kernel Kaggle has accepted but not yet started a session for
    reports "not_started" (KaggleClient.status()), not a synonym for
    "finished" -- it still holds this account exactly like queued/running
    (see kaggle_client.PENDING_STATES, fleet.busy_labels()/require_free()).
    deleteScene() previously checked ACTIVE_STATES only, so a scene whose
    only worker was still "not_started" read as not-rendering and its
    Kaggle dataset -- which Kaggle has no trash for -- could be deleted out
    from under a render that had not even started yet."""
    backend = make_backend(tmp_path, n=1)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([FleetState(
        job_id="job1", blend_name="remember.blend",
        start_frame=1, end_frame=4,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/remember-render-1",
                             frames=[1, 2], state="not_started")])])
    calls = []

    class DeleteClient(FakeClient):
        def delete_dataset(self, slug):
            calls.append(slug)

    backend.fleet_factory = lambda accts: Fleet(
        accts, lambda t: DeleteClient(t), tmp_path / "w")
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.deleteScene("user_0/remember-blend")

    assert calls == [], \
        "must not delete while a tracked job's worker is still not_started"
    assert notes and notes[0][1] == "offline"
    assert "acct0" in notes[0][0]
    assert "delete-scene:user_0/remember-blend" not in backend._workers


def test_deleting_is_not_blocked_by_a_finished_job_for_the_same_scene(
        qapp, tmp_path):
    """A completed job holds nobody -- Fleet.busy_labels()'s own contract
    -- so it must not block deleting the scene it rendered."""
    backend = make_backend(tmp_path, n=1)
    accounts = backend.store.list()
    fleet = backend.fleet_factory(accounts)
    fleet.save_jobs([FleetState(
        job_id="job1", blend_name="remember.blend",
        start_frame=1, end_frame=4,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/remember-render-1",
                             frames=[1, 2], state="complete",
                             finished_at=1.0)])])
    calls = []

    class DeleteClient(FakeClient):
        def delete_dataset(self, slug):
            calls.append(slug)

    backend.fleet_factory = lambda accts: Fleet(
        accts, lambda t: DeleteClient(t), tmp_path / "w")

    backend.deleteScene("user_0/remember-blend")
    _settle(backend)

    assert calls == ["user_0/remember-blend"]


def test_deleting_refuses_when_no_configured_account_owns_it(qapp, tmp_path):
    """Never guessed -- Scene.owner (here, the slug's own owner segment)
    is what decides whose token to use, and if nobody configured matches
    it this must refuse rather than try some other account's token."""
    backend = make_backend(tmp_path, n=1)
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.deleteScene("somebody_else/remember-blend")

    assert notes and notes[0][1] == "offline"
    assert "somebody_else" in notes[0][0]
    assert "delete-scene:somebody_else/remember-blend" not in backend._workers


def test_deleting_surfaces_kaggles_real_permission_error(qapp, tmp_path):
    """Even when the owner IS resolved correctly, Kaggle itself may still
    refuse (e.g. the account's own token was revoked) -- that failure must
    reach the user verbatim, not as a generic 'something went wrong'."""
    backend = make_backend(tmp_path, n=1)

    class RefusingClient(FakeClient):
        def delete_dataset(self, slug):
            raise KaggleError(
                f"could not delete dataset {slug!r}: Kaggle refused with "
                "a permission error. Only the dataset's OWNER can delete "
                "it.")

    backend.fleet_factory = lambda accts: Fleet(
        accts, lambda t: RefusingClient(t), tmp_path / "w")
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.deleteScene("user_0/remember-blend")
    _settle(backend)

    assert notes and notes[0][1] == "offline"
    assert "OWNER" in notes[0][0]


def test_deleting_refreshes_the_library_so_the_gone_scene_stops_showing(
        qapp, tmp_path):
    backend = make_backend(tmp_path, n=1)
    accounts = backend.store.list()
    list_calls = []

    class DeleteAndListClient(FakeClient):
        def delete_dataset(self, slug):
            pass

        def list_datasets(self):
            list_calls.append(self.token)
            return []

    backend.fleet_factory = lambda accts: Fleet(
        accts, lambda t: DeleteAndListClient(t), tmp_path / "w")

    backend.deleteScene(f"{accounts[0].username}/remember-blend")
    _settle(backend)

    assert list_calls == [accounts[0].token]


# ---------------------------------------------------------------------------
# renderScene(): a scene that is already on Kaggle, rendered with NO local
# .blend and no re-upload -- must go through Fleet.launch_from_dataset, and
# must not bypass it.
# ---------------------------------------------------------------------------

def _account_workers_fleet_from_dataset():
    class RecordingFleet(Fleet):
        def launch_from_dataset(self, dataset_slug, settings, start_frame,
                                end_frame, *, accounts=None):
            RecordingFleet.seen_slug = dataset_slug
            RecordingFleet.seen_labels = [a.label for a in accounts]
            return FleetState(
                job_id="j", blend_name="remember.blend",
                start_frame=start_frame, end_frame=end_frame,
                workers=[WorkerState(label=a.label, username=a.label,
                                     kernel_slug=f"{a.label}/k", frames=[1])
                         for a in accounts])
    return RecordingFleet


def test_render_scene_goes_through_launch_from_dataset(qapp, tmp_path):
    """No local .blend is ever set on the backend here -- proving this
    path needs none."""
    RecordingFleet = _account_workers_fleet_from_dataset()
    backend = make_backend(tmp_path, n=2)
    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    assert backend.blend is None

    backend.renderScene("user_0/remember-blend",
                        json.dumps({"startFrame": 1, "endFrame": 4}))
    _settle(backend)

    assert RecordingFleet.seen_slug == "user_0/remember-blend"
    assert set(RecordingFleet.seen_labels) == {"acct0", "acct1"}


def test_render_scene_with_no_labels_only_uses_free_accounts(qapp, tmp_path):
    backend = make_backend(tmp_path, n=2)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([FleetState(
        job_id="job1", blend_name="other.blend", start_frame=1, end_frame=5,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/other-render-1",
                             frames=[1, 2], state="running")])])

    RecordingFleet = _account_workers_fleet_from_dataset()
    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")

    backend.renderScene("user_0/remember-blend",
                        json.dumps({"startFrame": 1, "endFrame": 4}))
    _settle(backend)

    assert RecordingFleet.seen_labels == ["acct1"]


def test_render_scene_refuses_when_every_account_is_already_busy(
        qapp, tmp_path):
    backend = make_backend(tmp_path, n=1)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([FleetState(
        job_id="job1", blend_name="other.blend", start_frame=1, end_frame=5,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/other-render-1",
                             frames=[1, 2], state="running")])])
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.renderScene("user_0/remember-blend",
                        json.dumps({"startFrame": 1, "endFrame": 4}))

    assert notes and notes[0][1] == "offline"
    assert "busy" in notes[0][0].lower() or "rendering" in notes[0][0].lower()
    assert "launch-scene:user_0/remember-blend" not in backend._workers


def test_render_scene_refuses_an_explicitly_empty_label_list(qapp, tmp_path):
    """Same Critical-fix discipline as launch(): `"labels": []` must never
    be widened back out to every free account."""
    RecordingFleet = _account_workers_fleet_from_dataset()
    backend = make_backend(tmp_path, n=2)
    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.renderScene("user_0/remember-blend",
                        json.dumps({"startFrame": 1, "endFrame": 4,
                                   "labels": []}))
    _settle(backend)

    assert getattr(RecordingFleet, "seen_labels", None) is None, \
        "must not render on ANY account when labels is explicitly empty"
    assert notes and notes[0][1] == "offline"


def test_render_scene_refuses_an_unknown_label(qapp, tmp_path):
    backend = make_backend(tmp_path, n=2)
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    backend.renderScene("user_0/remember-blend",
                        json.dumps({"startFrame": 1, "endFrame": 4,
                                   "labels": ["ghost"]}))

    assert notes and notes[0][1] == "offline"
    assert "ghost" in notes[0][0]


def test_render_scene_does_not_overwrite_the_last_uploads_sharing_report(
        qapp, tmp_path):
    """launch_from_dataset scopes sharing to only the accounts in THIS
    render, so its fleet.unshared_accounts is always {} on this path --
    copying that into self._unshared_accounts would read as a positive
    "shared with everyone" claim about accounts this call never checked.
    Whatever the last real upload recorded must be left alone."""
    RecordingFleet = _account_workers_fleet_from_dataset()
    backend = make_backend(tmp_path, n=1)
    backend._unshared_accounts = {"friend_1": "could not be reached"}
    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")

    backend.renderScene("acct0/remember-blend",
                        json.dumps({"startFrame": 1, "endFrame": 2}))
    _settle(backend)

    assert backend._unshared_accounts == {"friend_1": "could not be reached"}


# ---------------- the QThread destructor abort (crash log, 2026-08-14) -----
#
# The packaged app aborted three times with BEX64 / c0000409 sub-code 7 in
# Qt6Core.dll and no message. Once blendfleet/crash_log.py was installing a
# Qt message handler, the very first run wrote the reason:
#
#     Qt FATAL: QThread: Destroyed while thread '' is still running
#
# _Worker emits succeeded/failed from INSIDE run(), and the handler for
# those signals pops the worker out of _workers immediately. Between that
# emit and run() actually returning, the thread is alive and untracked --
# so stop() waited for nothing, Qt destroyed a running QThread, and
# ~QThread called qFatal. These pin the tracking, not the abort: provoking
# the real one would take the test process down with it.

def test_a_worker_stays_tracked_until_run_has_actually_returned(qapp, tmp_path):
    """The exact window the crash lived in: succeeded has been delivered
    and _workers is already empty, but run() has not returned yet."""
    backend = make_backend(tmp_path, n=1)
    released = threading.Event()
    seen_during_handler = {}

    def slow_tail():
        released.wait(5.0)
        return "done"

    def on_ok(_result):
        # Runs on the UI thread from inside run(): this is the moment the
        # old code stopped tracking the still-running thread.
        seen_during_handler["workers"] = dict(backend._workers)
        seen_during_handler["running"] = set(backend._running_workers)

    backend._start("probe", slow_tail, "probing", on_ok)
    worker = backend._workers["probe"]
    released.set()
    worker.wait(5000)
    for _ in range(30):
        QApplication.processEvents()

    assert seen_during_handler, "the success handler never ran"
    assert seen_during_handler["workers"] == {}, (
        "precondition: _workers is emptied by the handler -- if this ever "
        "stops being true, this test is no longer covering the real bug")
    assert worker in seen_during_handler["running"], (
        "the worker was untracked while its run() was still on the stack. "
        "That is what let Qt destroy a running QThread and abort.")


def test_finished_is_what_untracks_a_worker(qapp, tmp_path):
    backend = make_backend(tmp_path, n=1)
    backend._start("probe", lambda: "done", "probing", lambda _r: None)
    worker = backend._workers.get("probe") or next(iter(backend._running_workers))
    worker.wait(5000)
    for _ in range(30):
        QApplication.processEvents()

    assert backend._running_workers == set(), (
        "`finished` must release the worker, or the set grows for the "
        "lifetime of the app")


def test_stop_waits_for_a_worker_that_is_no_longer_in_workers(qapp, tmp_path):
    """stop() is called from the host window's closeEvent. By then every
    handler has long since emptied _workers, which is precisely why the
    old stop() had nothing to wait on."""
    backend = make_backend(tmp_path, n=1)
    entered = threading.Event()

    def body():
        entered.set()
        time.sleep(0.2)
        return "done"

    backend._start("probe", body, "probing", lambda _r: None)
    worker = backend._workers["probe"]
    assert entered.wait(5.0), "the worker never started"
    backend._workers.clear()        # what the succeeded handler does

    backend.stop()

    assert worker.isFinished(), (
        "stop() returned while the QThread was still running -- Qt would "
        "abort when it destroyed it")


def test_stop_clears_its_tracking_so_it_can_be_called_twice(qapp, tmp_path):
    backend = make_backend(tmp_path, n=1)
    backend._start("probe", lambda: "done", "probing", lambda _r: None)
    backend.stop()
    assert backend._running_workers == set()
    backend.stop()      # closeEvent can fire more than once; must not raise


# ---------------- a worker that will NOT stop (crash log, run 2) ----------
#
# The tracking fix above was necessary but not sufficient. The next run
# logged both halves of what was left:
#
#     21:40:09.318 a background worker did not stop within 5s ...
#     21:40:09.437 Qt FATAL: QThread: Destroyed while thread '' is still running
#
# poll() is a Kaggle round-trip per account through kagglesdk, which
# exposes no timeout and no cancellation. Closing the window during an
# in-flight poll is therefore a thread that CANNOT be stopped -- and the
# poll timer fires every 30s, so during a render there is almost always
# one in flight. stop() now cuts such a worker loose instead of letting Qt
# destroy it.

@pytest.fixture(autouse=True)
def _no_orphans_leak_between_tests():
    """_ORPHANED_WORKERS is module state that deliberately never empties in
    production. A test that fills it must not leave it filled, or a later
    test reads another test's orphan."""
    yield
    for worker in bridge_mod._ORPHANED_WORKERS:
        worker.wait(5000)
    bridge_mod._ORPHANED_WORKERS.clear()


def _unstoppable(backend, release):
    """Start a worker that blocks until `release` is set, as an
    uncancellable network call does."""
    entered = threading.Event()

    def body():
        entered.set()
        release.wait(30.0)
        return "eventually"

    backend._start("probe", body, "probing", lambda _r: None)
    assert entered.wait(5.0), "the worker never started"
    return backend._workers["probe"]


def test_stop_cuts_loose_a_worker_that_will_not_stop(qapp, tmp_path,
                                                     monkeypatch):
    """Qt aborts the instant it destroys a running QThread, and a Kaggle
    request that has not answered cannot be cancelled. Cutting the thread
    loose is what turns that abort into an ordinary exit."""
    monkeypatch.setattr(bridge_mod, "_STOP_GRACE_MS", 200)
    backend = make_backend(tmp_path, n=1)
    release = threading.Event()
    worker = _unstoppable(backend, release)
    try:
        backend.stop()

        assert worker in bridge_mod.orphaned_workers(), (
            "a worker that outlasted stop() must be cut loose; leaving it "
            "parented to the Backend is the qFatal abort")
        assert worker.parent() is None, (
            "still a child of the Backend -- Qt would delete it during "
            "teardown and abort")
    finally:
        release.set()
        worker.wait(5000)


def test_an_orphaned_worker_cannot_call_back_into_the_backend(qapp, tmp_path,
                                                              monkeypatch):
    """Its signals fire while the window is already tearing down, into
    handlers that touch timers and emit on a half-dead object."""
    monkeypatch.setattr(bridge_mod, "_STOP_GRACE_MS", 200)
    backend = make_backend(tmp_path, n=1)
    release = threading.Event()
    worker = _unstoppable(backend, release)
    try:
        backend.stop()
        release.set()
        worker.wait(5000)
        for _ in range(30):
            QApplication.processEvents()

        assert backend._workers == {}
        assert backend._running_workers == set()
    finally:
        release.set()
        worker.wait(5000)


def test_a_worker_that_stops_in_time_is_never_orphaned(qapp, tmp_path):
    """The normal path must stay normal: nothing is leaked just because
    the app closed."""
    backend = make_backend(tmp_path, n=1)
    backend._start("probe", lambda: "done", "probing", lambda _r: None)
    backend.stop()
    assert bridge_mod.orphaned_workers() == []


# ---------------- a standalone Upload requires only the OWNER -------------
#
# Field report: the first Upload of a scene failed with a 403 from Kaggle's
# ListDatasetFiles for a friend account, and pressing Upload again -- with
# nothing else changed -- succeeded. Omitting `required` makes EVERY
# configured account mandatory, so a friend whose READER grant had not
# finished propagating failed the whole upload. An upload starts nothing
# and costs no quota, so only the owner is genuinely required here; the
# strict, all-accounts treatment belongs to launch(), which is about to
# push kernels.

def test_a_standalone_upload_requires_only_the_owner(qapp, tmp_path):
    backend = make_backend(tmp_path, n=3)
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x" * 8)
    backend.blend = blend
    captured = {}

    class RecordingFleet(Fleet):
        def prepare_dataset(self, blend, on_progress=None, *, clients=None,
                            usernames=None, on_stage=None, required=None):
            captured["required"] = required
            return "me/scene-blend"

    backend.fleet_factory = lambda accounts: RecordingFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    backend.syncDataset()
    _settle(backend)

    assert captured["required"] is not None, (
        "None means 'every account is mandatory' -- which is what made a "
        "friend's propagation delay fail the whole upload")
    assert [a.label for a in captured["required"]] == ["acct0"]


def test_an_upload_that_could_not_share_says_so(qapp, tmp_path):
    """Making a propagation delay non-fatal must not make it invisible --
    the user would render on an account that cannot see the scene."""
    backend = make_backend(tmp_path, n=2)
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x" * 8)
    backend.blend = blend
    notes = []
    backend.notification.connect(lambda m, t: notes.append(m))

    class PartlySharedFleet(Fleet):
        def prepare_dataset(self, blend, on_progress=None, *, clients=None,
                            usernames=None, on_stage=None, required=None):
            self.unshared_accounts = {"acct1": "grant has not propagated"}
            return "me/scene-blend"

    backend.fleet_factory = lambda accounts: PartlySharedFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    backend.syncDataset()
    _settle(backend)

    assert notes, "the upload said nothing at all"
    message = notes[-1]
    assert "acct1" in message, "the account left unshared must be named"
    assert "does not need repeating" in message, (
        "the user must be told the upload itself succeeded, or they will "
        "re-upload the whole .blend for nothing")


def test_a_fully_shared_upload_still_reports_plain_success(qapp, tmp_path):
    backend = make_backend(tmp_path, n=2)
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"x" * 8)
    backend.blend = blend
    notes = []
    backend.notification.connect(lambda m, t: notes.append((m, t)))

    class CleanFleet(Fleet):
        def prepare_dataset(self, blend, on_progress=None, *, clients=None,
                            usernames=None, on_stage=None, required=None):
            return "me/scene-blend"

    backend.fleet_factory = lambda accounts: CleanFleet(
        accounts, lambda t: FakeClient(t), tmp_path / "w")
    backend.syncDataset()
    _settle(backend)

    assert notes[-1] == ("Uploaded scene.blend to me/scene-blend", "active")


# ---------------------------------------------------------------------------
# user-facing failures must reach the diagnostic log
#
# A sharing failure was reported from the field and %APPDATA%\BlendFleet\
# logs had nothing about it at all: the log only ever carried Qt messages
# and crashes, while this app's own errors lived in a toast that fades.
# ---------------------------------------------------------------------------

@pytest.fixture
def diagnostic_log(tmp_path):
    """A real crash_log for one test, then process-global state back.

    install() rebinds sys.excepthook, threading.excepthook, Qt's message
    handler and faulthandler's target fd -- the same reasoning (and the
    same restore) as tests/test_crash_log.py's own fixture.
    """
    import faulthandler
    import sys as _sys

    from blendfleet import crash_log

    saved_excepthook = _sys.excepthook
    saved_thread_hook = threading.excepthook
    saved_faulthandler = faulthandler.is_enabled()
    path = crash_log.install(tmp_path / "logs")
    yield path
    crash_log.shutdown()
    _sys.excepthook = saved_excepthook
    threading.excepthook = saved_thread_hook
    from PySide6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(None)
    if saved_faulthandler:
        faulthandler.enable()
    else:
        faulthandler.disable()


def _log_text(path):
    return path.read_text(encoding="utf-8", errors="replace")


def test_an_error_notification_is_recorded(qapp, tmp_path, diagnostic_log):
    backend = make_backend(tmp_path, n=1)
    backend.notification.emit("Choose a .blend file first.", "offline")

    assert "Choose a .blend file first." in _log_text(diagnostic_log)


def test_an_error_tone_is_recorded_prominently(qapp, tmp_path, monkeypatch,
                                               diagnostic_log):
    """"offline" is this app's error tone. Those lines have to survive the
    log's routine-message cap, or the fault gets dropped and the chatter
    around it kept."""
    seen = []
    monkeypatch.setattr(bridge_mod.crash_log, "record",
                        lambda message, critical=False:
                        seen.append((message, critical)))
    backend = make_backend(tmp_path, n=1)

    backend.notification.emit("Kaggle refused the upload.", "offline")
    backend.logLine.emit("dataset ready: me/scene-blend", "active")

    assert ("notification [offline] Kaggle refused the upload.", True) in seen
    assert ("log [active] dataset ready: me/scene-blend", False) in seen


def test_the_same_message_repeating_does_not_bury_the_log(
        qapp, tmp_path, diagnostic_log):
    """The 30-second poll failing while the network is down emits the
    identical sentence every 30 seconds."""
    backend = make_backend(tmp_path, n=1)
    for _ in range(20):
        backend.notification.emit("lost contact with Kaggle", "offline")

    assert _log_text(diagnostic_log).count("lost contact with Kaggle") == 1


def test_a_failed_call_records_the_exception_behind_the_friendly_sentence(
        qapp, tmp_path, diagnostic_log):
    """explain() is right for the page and useless for a support request:
    "Kaggle could not be reached" covers a DNS failure, a 403 and a bug in
    this app equally well."""
    backend = make_backend(tmp_path, n=1)

    def work():
        raise KaggleError("403 Forbidden from ListDatasetFiles")

    backend._start("probe", work, "Uploading the scene", lambda _r: None)
    _settle(backend)

    body = _log_text(diagnostic_log)
    assert "KaggleError: 403 Forbidden from ListDatasetFiles" in body
    assert "Traceback (most recent call last)" in body
    assert "Uploading the scene" in body
    assert "notification [offline]" in body, (
        "the sentence the user actually saw must be in the log too -- it is "
        "what they will quote back")


def test_no_account_token_ever_reaches_the_diagnostic_log(qapp, tmp_path,
                                                          diagnostic_log):
    backend = make_backend(tmp_path, n=2)
    token = backend.store.list()[0].token

    def work():
        raise KaggleError(f"401 Unauthorized (token={token})")

    backend._start("probe", work, "Checking render status", lambda _r: None)
    _settle(backend)

    body = _log_text(diagnostic_log)
    assert "401 Unauthorized" in body
    assert token not in body, (
        "a diagnostic log carrying a live token turns a support request "
        "into a credential rotation")
    assert token[:9] + "…" in body


def test_a_dead_log_stream_leaves_a_trace(qapp, tmp_path, monkeypatch,
                                          diagnostic_log):
    """Swallowing this keeps the app alive, correctly -- but it is also
    exactly what "the progress bar froze at 3/15 and nothing said why"
    looks like from the outside."""
    def explode(*a, **kw):
        raise RuntimeError("ChunkedEncodingError: connection broken")

    monkeypatch.setattr(bridge_mod, "stream_progress", explode)
    backend = make_backend(tmp_path, n=1)
    state = FleetState(job_id="j1", blend_name="scene.blend",
                       start_frame=1, end_frame=4, workers=[
                           WorkerState(label="acct0", username="user_0",
                                       kernel_slug="user_0/scene-render-abc",
                                       frames=[1, 2], state="running")])

    backend._start_streams(state)
    for thread in backend._stream_threads:
        thread.join(timeout=5)

    body = _log_text(diagnostic_log)
    assert "the live log stream for acct0" in body
    assert "ChunkedEncodingError: connection broken" in body
    assert "Traceback (most recent call last)" in body


def test_an_unavailable_quota_says_why_in_the_log(qapp, tmp_path,
                                                  diagnostic_log):
    """"unavailable" on a card is what a rate limit AND a revoked token
    both look like."""
    class DeadClient(FakeClient):
        def quota(self):
            raise KaggleError("429 Too Many Requests")

    backend = make_backend(tmp_path, n=1)
    backend.fleet_factory = lambda accounts: Fleet(
        accounts, lambda t: DeadClient(t), tmp_path / "w")

    backend.refreshQuota()
    _settle(backend)

    body = _log_text(diagnostic_log)
    assert "acct0 reads 'unavailable'" in body
    assert "429 Too Many Requests" in body


# ---------------------------------------------------------------------------
# Surviving a restart.
#
# Reported from the field: "once it closes and when I try to open it again
# the render progress disappears, all we see is loading". The renders were
# running on Kaggle the whole time -- the app had simply stopped looking at
# them. Two independent gaps: nothing re-attached the log streams when the
# app started (they were only ever started by a launch's success callback),
# and the one progress number that IS written to disk, frames_done, was
# only ever set by the live stream, so it never moved either.
# ---------------------------------------------------------------------------

class _StubStream:
    """A stand-in for log_stream.stream_progress that never touches the
    network and never returns until the test lets it.

    Blocking is the point: a resumed stream is only "reconnecting" while
    its thread is alive, and a stub that returned immediately would race
    the assertion. release() is called by every test that uses it, so no
    thread outlives its test (see conftest.no_leaked_threads).
    """

    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.finish = threading.Event()

    def __call__(self, token, user_name, kernel_slug, on_progress,
                 stop_event=None, **kwargs):
        self.calls.append((token, user_name, kernel_slug))
        self.started.set()
        self.finish.wait(10)

    def release(self, backend):
        self.finish.set()
        for thread in backend._stream_threads:
            thread.join(timeout=5)


class _KaggleSaysClient(FakeClient):
    """A Kaggle that answers the startup status check with a fixed state.

    FakeClient has no status() at all, which poll_all() treats as one more
    unreachable worker -- correct, and exactly what the "Kaggle could not
    be asked" tests want, but useless for the cases that need Kaggle to
    actually answer.
    """

    def __init__(self, token, state="running", error=None):
        super().__init__(token)
        self._state = state
        self._error = error

    def status(self, slug):
        if self._error is not None:
            raise self._error
        return KernelStatus(self._state, "")


# "not given", distinct from kaggle_says=None ("Kaggle cannot be reached").
_UNSET = object()


def _running_job_backend(tmp_path, state="running", frames_done=0,
                         frames_done_at=0.0, n=2, kaggle_says=_UNSET,
                         status_error=None):
    """One tracked job already on disk, as a restarted app would find it.

    `kaggle_says` is what the startup check's poll gets back, which is a
    DIFFERENT question from `state` (what the file on disk says) -- the
    whole point of that check is that the two can disagree after the app
    has been shut for a while. It defaults to agreeing with the file, so a
    test that does not care reads as "nothing changed while it was closed".
    Pass `kaggle_says=None` to leave the fleet using FakeClient, i.e. a
    Kaggle that cannot be reached at all.
    """
    backend = make_backend(tmp_path, n=n)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([FleetState(
        job_id="job-1", blend_name="scene.blend", start_frame=1, end_frame=6,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/scene-render-1",
                             frames=[1, 2, 3, 4, 5, 6], state=state,
                             frames_done=frames_done,
                             frames_done_at=frames_done_at,
                             started_at=time.time() - 900)])])
    if kaggle_says is _UNSET:
        kaggle_says = state
    if kaggle_says is not None or status_error is not None:
        backend.fleet_factory = lambda accounts: Fleet(
            accounts,
            lambda t: _KaggleSaysClient(t, kaggle_says, status_error),
            tmp_path / "w")
    return backend


def _ready(backend):
    """backend.ready(), including the Kaggle check it now runs off-thread.

    ready() no longer resumes anything on the spot: it asks Kaggle what is
    still running first, on a worker thread, and only then decides. Tests
    have to let that round trip finish, which means pumping the loop --
    the succeeded/failed connections are queued across threads.
    """
    backend.ready()
    worker = backend._workers.get("startupCheck")
    if worker is not None:
        worker.wait(5000)
    for _ in range(30):
        QApplication.processEvents()


def _collect_notifications(backend):
    """Every (message, tone) the backend emits from here on."""
    seen = []
    backend.notification.connect(lambda m, t: seen.append((m, t)))
    return seen


def test_a_render_still_running_gets_its_stream_back_when_the_app_reopens(
        qapp, tmp_path, monkeypatch):
    """The gap the user actually hit. Nothing called _start_streams at
    startup, so a job still rendering on Kaggle got no SSE stream at all
    after a restart: no phase, no frame counter, no telemetry."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path)

    _ready(backend)
    assert stub.started.wait(5), "no stream was started for a running render"
    stub.release(backend)

    assert stub.calls == [("KGAT_" + "0" * 32, "user_0", "scene-render-1")]


def test_a_worker_kaggle_has_not_started_yet_is_still_resumed(
        qapp, tmp_path, monkeypatch):
    """PENDING_STATES, not ACTIVE_STATES. A kernel pushed moments before
    the app closed reports "not_started" -- it is about to render, and it
    is exactly the case where the user has seen no progress at all."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path, state="not_started")

    _ready(backend)
    assert stub.started.wait(5)
    stub.release(backend)


@pytest.mark.parametrize("state", ["complete", "error", "cancel_acknowledged"])
def test_a_finished_render_is_never_re_streamed(qapp, tmp_path, monkeypatch,
                                                state):
    """Replaying a finished kernel's log would rebuild a "rendering 6/6"
    phase for a job that ended hours ago, and costs a Kaggle connection
    per account to learn nothing."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path, state=state)

    _ready(backend)

    assert not stub.started.is_set()
    assert backend._stream_threads == []
    assert json.loads(backend.state())["instances"][0]["reconnecting"] is False


def test_a_worker_already_being_streamed_does_not_get_a_second_stream(
        qapp, tmp_path, monkeypatch):
    """Two streams on one kernel double every PROGRESS line it reports and
    open a second Kaggle connection for no gain."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path)

    _ready(backend)
    assert stub.started.wait(5)
    _ready(backend)                     # a second connect, or a page reload
    backend._resume_streams()           # and the resume itself, again
    stub.release(backend)

    assert len(stub.calls) == 1
    assert len(backend._stream_threads) == 1


def test_a_reconnecting_card_says_so_and_does_not_claim_a_live_reading(
        qapp, tmp_path, monkeypatch):
    """The window between resuming a stream and it replaying anything. The
    saved frame count is shown -- it is the best thing known -- but it is
    flagged as saved, with its age, and `live` stays null."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path, frames_done=4,
                                   frames_done_at=time.time() - 600)

    _ready(backend)
    assert stub.started.wait(5)
    instance = json.loads(backend.state())["instances"][0]
    stub.release(backend)

    assert instance["reconnecting"] is True
    assert instance["live"] is None, "a persisted count is not a live reading"
    assert instance["worker"]["framesDone"] == 4
    assert 500 < instance["worker"]["framesDoneAge"] < 700


def test_reconnecting_stops_the_moment_the_stream_reports(
        qapp, tmp_path, monkeypatch):
    """"Catching up" is only true while it is catching up. Once anything
    live arrives the card shows the live reading instead."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path, frames_done=4)

    _ready(backend)
    assert stub.started.wait(5)
    backend._progress_q.put(("acct0", 5, 6))
    backend._live_tick()
    instance = json.loads(backend.state())["instances"][0]
    stub.release(backend)

    assert instance["reconnecting"] is False
    assert instance["live"]["framesDone"] == 5


def test_a_frame_count_that_was_never_saved_has_no_age(qapp, tmp_path):
    """Absent is not zero. A worker whose count predates this field (or
    that has not finished a frame yet) must not claim it was measured now."""
    backend = _running_job_backend(tmp_path)
    worker = json.loads(backend.state())["instances"][0]["worker"]
    assert worker["framesDone"] == 0
    assert worker["framesDoneAge"] is None


def _finished_worker_backend(tmp_path, **worker_kw):
    """One tracked job whose worker has STOPPED, as the payload sees it."""
    backend = make_backend(tmp_path, n=1)
    fleet = backend.fleet_factory(backend.store.list())
    fields = dict(label="acct0", username="user_0",
                  kernel_slug="user_0/scene-render-1", frames=[1, 2],
                  state="complete", frames_done=1,
                  frames_done_at=time.time() - 3600,
                  started_at=time.time() - 900,
                  finished_at=time.time() - 600)
    fields.update(worker_kw)
    fleet.save_jobs([FleetState(
        job_id="job-1", blend_name="scene.blend", start_frame=1, end_frame=2,
        workers=[WorkerState(**fields)])])
    return backend


def test_a_finished_count_read_from_the_log_is_labelled_final(qapp, tmp_path):
    """Not "saved 1h ago": it is the render's own last word, read from its
    kernel log once it stopped, and the card must not present it as a
    cached live reading."""
    backend = _finished_worker_backend(tmp_path, frames_done=2,
                                       final_count_checked=True,
                                       final_count_known=True)
    worker = json.loads(backend.state())["instances"][0]["worker"]
    assert worker["framesDone"] == 2
    assert worker["framesDoneSource"] == "final"


def test_a_finished_worker_whose_log_could_not_be_read_reads_as_unknown(
        qapp, tmp_path):
    """The stale 1 is still in the file -- it is a floor from before the
    render ended -- but the payload must NOT let the page show it as a
    count, and must not replace it with a zero either."""
    backend = _finished_worker_backend(tmp_path, frames_done=1,
                                       final_count_checked=True,
                                       final_count_known=False)
    worker = json.loads(backend.state())["instances"][0]["worker"]
    assert worker["framesDoneSource"] == "unknown"


def test_a_running_workers_saved_count_is_still_labelled_saved(qapp, tmp_path):
    """Unchanged for a render that has NOT stopped: the saved-with-its-age
    treatment is right there, and this must not turn into "unknown"."""
    backend = _running_job_backend(tmp_path, frames_done=3,
                                   frames_done_at=time.time() - 600)
    worker = json.loads(backend.state())["instances"][0]["worker"]
    assert worker["framesDoneSource"] == "saved"


def test_a_worker_nothing_has_ever_counted_says_so(qapp, tmp_path):
    backend = _running_job_backend(tmp_path)
    worker = json.loads(backend.state())["instances"][0]["worker"]
    assert worker["framesDoneSource"] == "none"


def test_an_account_with_no_stream_is_not_reported_as_reconnecting(
        qapp, tmp_path):
    backend = _running_job_backend(tmp_path)
    by_label = {i["label"]: i
                for i in json.loads(backend.state())["instances"]}
    assert by_label["acct1"]["reconnecting"] is False


# ---------------------------------------------------------------------------
# Reopening the app AFTER the renders have already finished.
#
# Reported from the field: "Reconnecting to 5 render(s) still running on
# Kaggle ... but still everything is stuck". They were not still running.
# ready() resumed streams straight from the state FILE, which holds whatever
# was true when the app last closed -- nothing had asked Kaggle since. So
# five finished kernels each got an SSE stream that had no progress left to
# send, the app announced five renders that did not exist, and the cards sat
# there. The check below is the fix: ask first, then decide, then say what
# was actually found.
# ---------------------------------------------------------------------------

def test_a_render_that_finished_while_the_app_was_closed_gets_no_stream(
        qapp, tmp_path, monkeypatch):
    """The file says running; Kaggle says complete. Kaggle wins -- opening a
    stream on a finished kernel replays a log that can never advance."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path, state="running",
                                   kaggle_says="complete")

    _ready(backend)

    assert not stub.started.is_set(), "a finished kernel was streamed anyway"
    assert backend._stream_threads == []


def test_a_render_that_finished_while_closed_shows_as_finished_not_reconnecting(
        qapp, tmp_path, monkeypatch):
    """The card the user was actually looking at. Once the check has run,
    it must carry the finished state, the time it took, and the frame
    count -- not a reconnection that is never coming."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path, state="running", frames_done=6,
                                   kaggle_says="complete")

    _ready(backend)
    instance = json.loads(backend.state())["instances"][0]

    assert instance["worker"]["state"] == "complete"
    assert instance["worker"]["finished"] is True
    assert instance["worker"]["elapsed"] > 0
    assert instance["worker"]["framesDone"] == 6
    assert instance["reconnecting"] is False


def test_a_render_that_finished_while_closed_says_where_the_frames_are(
        qapp, tmp_path, monkeypatch):
    """Someone reopening the app needs the useful fact, not just "done":
    the frames are still sitting on Kaggle until they are collected."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path, state="running",
                                   kaggle_says="complete")
    seen = _collect_notifications(backend)

    _ready(backend)

    answers = [m for m, _t in seen if "have finished" in m]
    assert answers, f"nothing said the renders had finished: {seen}"
    assert "acct0" in answers[-1]
    assert "Collect frames" in answers[-1]
    assert "still running" not in answers[-1]


def test_a_render_kaggle_confirms_is_running_is_streamed_and_announced(
        qapp, tmp_path, monkeypatch):
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path, state="running",
                                   kaggle_says="running")
    seen = _collect_notifications(backend)

    _ready(backend)
    assert stub.started.wait(5)
    stub.release(backend)

    answers = [m for m, _t in seen if "still running" in m]
    assert answers, f"the resume was never announced: {seen}"
    assert "Kaggle says 1 render(s) are still running (acct0)" in answers[-1]


def test_a_mixed_fleet_is_reported_as_both(qapp, tmp_path, monkeypatch):
    """Some finished overnight and some did not. One sentence has to be
    true of both halves, and neither half may be rounded into the other."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = make_backend(tmp_path, n=2)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([FleetState(
        job_id="job-1", blend_name="scene.blend", start_frame=1, end_frame=6,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/k0", frames=[1, 2, 3],
                             state="running", started_at=time.time() - 900),
                 WorkerState(label="acct1", username="user_1",
                             kernel_slug="user_1/k1", frames=[4, 5, 6],
                             state="running",
                             started_at=time.time() - 900)])])

    # acct0 is done, acct1 is still going.
    def client(token):
        done = token.endswith("0" * 32)
        return _KaggleSaysClient(token, "complete" if done else "running")
    backend.fleet_factory = lambda accounts: Fleet(accounts, client,
                                                   tmp_path / "w")
    seen = _collect_notifications(backend)

    _ready(backend)
    assert stub.started.wait(5)
    stub.release(backend)

    answer = [m for m, _t in seen if "Checking with Kaggle" not in m][-1]
    assert "still running (acct1)" in answer
    assert "have finished (acct0)" in answer
    assert "All 1 render(s)" not in answer, (
        "'all' is only true when nothing is still going")
    assert stub.calls == [("KGAT_" + "1".rjust(32, "0"), "user_1", "k1")]


def test_the_startup_check_says_what_it_is_doing_before_it_answers(
        qapp, tmp_path, monkeypatch):
    """The check costs a network call per account. Saying nothing until it
    returns is how a fleet of stale cards reads as a stuck app."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path)
    seen = _collect_notifications(backend)

    backend.ready()
    assert seen, "nothing was said while the check was in flight"
    assert "Checking with Kaggle" in seen[0][0]
    assert "may be out of date" in seen[0][0]

    _ready(backend)
    stub.release(backend)


def test_an_unreachable_kaggle_says_the_view_may_be_stale(
        qapp, tmp_path, monkeypatch):
    """Never silently present the state file as current. If the check
    failed, the app says so and says what that means."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(
        tmp_path, state="running",
        status_error=KaggleError("503 Service Unavailable"))
    seen = _collect_notifications(backend)

    _ready(backend)

    answers = [(m, t) for m, t in seen if "could not be asked" in m]
    assert answers, f"an unreachable Kaggle was never reported: {seen}"
    message, tone = answers[-1]
    assert "503 Service Unavailable" in message
    assert "may be out of date" in message
    assert tone == "offline"


def test_an_unreachable_kaggle_opens_no_stream(qapp, tmp_path, monkeypatch):
    """A worker nobody could ask about is not a worker known to be running.
    Streaming it is how the app ends up claiming a live reading it does not
    have."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(
        tmp_path, state="running",
        status_error=KaggleError("503 Service Unavailable"))

    _ready(backend)

    assert not stub.started.is_set()
    assert json.loads(backend.state())["instances"][0]["reconnecting"] is False


def test_nothing_pending_asks_kaggle_nothing_and_says_nothing(
        qapp, tmp_path, monkeypatch):
    """A fleet whose jobs were all finished before the app closed has
    nothing to check -- and a startup toast about it would be noise."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path, state="complete")
    seen = _collect_notifications(backend)

    _ready(backend)

    assert seen == []
    assert not stub.started.is_set()


def test_the_reconnecting_flag_clears_when_the_stream_thread_ends(
        qapp, tmp_path, monkeypatch):
    """A stream that ended without ever reporting -- a replayed log that hit
    END_OF_LOG, or a connection that gave up -- must not leave the card
    promising a reconnection that is never coming."""
    stub = _StubStream()
    monkeypatch.setattr(bridge_mod, "stream_progress", stub)
    backend = _running_job_backend(tmp_path)

    _ready(backend)
    assert stub.started.wait(5)
    assert json.loads(backend.state())["instances"][0]["reconnecting"] is True

    stub.release(backend)       # the stream thread returns

    assert json.loads(backend.state())["instances"][0]["reconnecting"] is False


def test_a_finished_workers_replayed_readings_are_not_shown_as_live(
        qapp, tmp_path):
    """The other half of "everything is stuck". `_live` is only ever cleared
    by a new launch, so a session that ended left its last phase and GPU
    bars on the card for ever -- and a resumed stream REPLAYS a finished
    kernel's whole log, rebuilding them from scratch. They describe a
    machine that no longer exists."""
    backend = _running_job_backend(tmp_path, state="complete")
    fleet = backend.fleet_factory(backend.store.list())
    jobs = fleet.load_jobs()
    jobs[0].workers[0].finished_at = time.time()
    fleet.save_jobs(jobs)
    slot = backend._slot("acct0")
    slot["phase"] = "rendering · 6/6 frames"
    slot["framesDone"], slot["framesTotal"] = 6, 6
    slot["gpus"][0] = {"index": 0, "util": 91, "memUsed": 4096,
                       "memTotal": 15360}
    slot["ramUsed"] = 8 * 1024 ** 3
    slot["cpuPct"] = 70

    live = json.loads(backend.state())["instances"][0]["live"]

    assert live["phase"] == ""
    assert live["gpus"] == []
    assert live["ramUsed"] is None
    assert live["cpuPct"] is None
    # What the session really did is still a fact about it, and is kept.
    assert live["framesDone"] == 6


def test_the_thirty_second_poll_turns_a_stale_running_card_into_a_finished_one(
        qapp, tmp_path):
    """The routine poll already corrected the state FILE. Nothing carried
    that through to the card, because the replayed live phase outranked it
    -- the same bug in a different hat."""
    backend = _running_job_backend(tmp_path, state="running",
                                   kaggle_says="complete")
    slot = backend._slot("acct0")
    slot["phase"] = "rendering · 6/6 frames"
    slot["framesDone"], slot["framesTotal"] = 6, 6

    backend.poll()
    for worker in list(backend._workers.values()):
        worker.wait(5000)
    for _ in range(30):
        QApplication.processEvents()

    instance = json.loads(backend.state())["instances"][0]
    assert instance["worker"]["state"] == "complete"
    assert instance["worker"]["finished"] is True
    assert instance["live"]["phase"] == ""


def test_the_live_tick_writes_the_frame_count_through_to_disk(qapp, tmp_path):
    """Kaggle's status API reports no frame count, so poll can never learn
    one. If the live stream does not persist it, nothing does, and a
    restart is back to showing nothing."""
    backend = _running_job_backend(tmp_path)

    backend._progress_q.put(("acct0", 3, 6))
    backend._live_tick()

    reloaded = backend.fleet_factory(backend.store.list()).load_jobs()
    assert reloaded[0].workers[0].frames_done == 3
    assert reloaded[0].workers[0].frames_done_at > 0


def test_a_frame_count_that_has_not_moved_is_not_written_again(
        qapp, tmp_path, monkeypatch):
    """The live tick fires every 2 seconds and a save is a read-merge-write
    of the whole jobs file. Only a CHANGED count may touch the disk, or a
    progress bar becomes a write storm."""
    backend = _running_job_backend(tmp_path)
    writes = []
    real_save = Fleet.save_jobs

    def counted(self, jobs):
        writes.append(1)
        return real_save(self, jobs)

    monkeypatch.setattr(Fleet, "save_jobs", counted)

    backend._progress_q.put(("acct0", 3, 6))
    backend._live_tick()
    for _ in range(5):                  # idle ticks: nothing on any queue
        backend._live_tick()
    backend._progress_q.put(("acct0", 3, 6))    # a replayed line, same count
    backend._live_tick()

    assert len(writes) == 1, f"{len(writes)} writes for one finished frame"


def test_one_write_covers_every_account_that_moved_in_the_same_tick(
        qapp, tmp_path, monkeypatch):
    """Four accounts each finishing a frame in the same 2-second window is
    one file write, not four."""
    backend = make_backend(tmp_path, n=3)
    fleet = backend.fleet_factory(backend.store.list())
    fleet.save_jobs([FleetState(
        job_id="job-1", blend_name="scene.blend", start_frame=1, end_frame=9,
        workers=[WorkerState(label=f"acct{i}", username=f"user_{i}",
                             kernel_slug=f"user_{i}/scene-render-1",
                             frames=[1, 2, 3], state="running")
                 for i in range(3)])])
    writes = []
    real_save = Fleet.save_jobs

    def counted(self, jobs):
        writes.append(1)
        return real_save(self, jobs)

    monkeypatch.setattr(Fleet, "save_jobs", counted)

    for i in range(3):
        backend._progress_q.put((f"acct{i}", 2, 3))
    backend._live_tick()

    assert len(writes) == 1
    reloaded = backend.fleet_factory(backend.store.list()).load_jobs()
    assert [w.frames_done for w in reloaded[0].workers] == [2, 2, 2]


def test_telemetry_alone_never_touches_the_jobs_file(qapp, tmp_path,
                                                     monkeypatch):
    """GPU load, system RAM and the phase string are live-only readings
    that a resumed stream rebuilds by replaying the log. Persisting them
    would turn a 2-second tick into a 2-second disk write."""
    backend = _running_job_backend(tmp_path)
    writes = []
    monkeypatch.setattr(Fleet, "save_jobs",
                        lambda self, jobs: writes.append(1))

    backend._telemetry_q.put(("acct0", {"gpu": 0, "util": 91}))
    backend._system_q.put(("acct0", {"ram_used": 1, "cpu_pct": 4}))
    backend._live_tick()

    assert writes == []


# ---------------------------------------------------------------------------
# Live frame previews.
#
# Kaggle releases a kernel's output only once its session ends, so a
# mid-render frame cannot be fetched -- the notebook pushes a small JPEG
# down the log stream and log_stream reassembles it on a STREAM THREAD.
# Everything below is about what happens after that: nothing touches a Qt
# object off the UI thread, and a hundred-frame render must not accumulate
# a hundred images.
# ---------------------------------------------------------------------------

def _thumb(frame, jpeg=b"\xff\xd8fake-jpeg"):
    import base64
    return {"frame": frame,
            "jpeg_b64": base64.b64encode(jpeg).decode("ascii"),
            "bytes": len(jpeg)}


def test_a_live_preview_reaches_the_payload_as_a_drawable_image(qapp, tmp_path):
    backend = make_backend(tmp_path, n=1)
    backend._thumb_q.put(("acct0", _thumb(7)))
    backend._live_tick()

    live = json.loads(backend.state())["instances"][0]["live"]
    assert live["thumb"]["frame"] == 7
    # A data URL, not a file path: the page draws it directly, and no
    # frame is written to the user's disk to be looked at once.
    assert live["thumb"]["dataUrl"].startswith("data:image/jpeg;base64,")


def test_the_keys_match_what_log_stream_actually_hands_over(qapp, tmp_path):
    """Built from a real reassembled preview rather than a hand-made dict,
    so a rename in log_stream fails HERE instead of silently emptying the
    tile."""
    import base64

    from blendfleet.log_stream import ThumbnailAssembler, parse_thumbnail_part

    raw = b"\xff\xd8" + bytes(range(200))
    b64 = base64.b64encode(raw).decode("ascii")
    line = ('data: {"stream_name":"stdout","data":"THUMB frame=4 part=1/1 '
            f'bytes={len(raw)} {b64}"' + "}")
    record = ThumbnailAssembler().add(parse_thumbnail_part(line))
    assert record is not None, "the sample line no longer reassembles"

    backend = make_backend(tmp_path, n=1)
    backend._thumb_q.put(("acct0", record))
    backend._live_tick()

    thumb = json.loads(backend.state())["instances"][0]["live"]["thumb"]
    assert thumb["frame"] == 4
    assert thumb["dataUrl"].endswith(b64)


def test_only_the_newest_preview_per_account_is_kept(qapp, tmp_path):
    """A 100-frame render must not hold 100 images. Each one is ~5 kB of
    JPEG that is re-serialised into the payload on every tick, and the
    question this answers -- what is it doing right now -- only the newest
    frame can answer."""
    backend = make_backend(tmp_path, n=1)
    for frame in range(1, 101):
        backend._thumb_q.put(("acct0", _thumb(frame)))
    backend._live_tick()

    live = backend._live["acct0"]
    assert live["thumb"]["frame"] == 100
    # Not a list, not a dict of frames: one slot, holding one picture.
    assert isinstance(live["thumb"], dict)
    assert set(live["thumb"]) == {"frame", "dataUrl", "bytes"}


def test_each_account_keeps_its_own_newest_preview(qapp, tmp_path):
    """Accounts render different stripes of the same scene, so one
    account's preview must never overwrite another's."""
    backend = make_backend(tmp_path, n=2)
    backend._thumb_q.put(("acct0", _thumb(3)))
    backend._thumb_q.put(("acct1", _thumb(11)))
    backend._live_tick()

    instances = json.loads(backend.state())["instances"]
    by_label = {i["label"]: i for i in instances}
    assert by_label["acct0"]["live"]["thumb"]["frame"] == 3
    assert by_label["acct1"]["live"]["thumb"]["frame"] == 11


def test_an_account_with_no_preview_yet_shows_nothing(qapp, tmp_path):
    """Never a placeholder. An empty frame-shaped box would imply a frame
    had rendered, which is the one thing this must not say."""
    backend = make_backend(tmp_path, n=1)
    backend._progress_q.put(("acct0", 1, 4))
    backend._live_tick()

    live = json.loads(backend.state())["instances"][0]["live"]
    assert live["thumb"] is None


def test_a_preview_is_evidence_the_render_has_started(qapp, tmp_path):
    """A preview only exists because a frame finished inside a running
    Blender, so it says at least as much as telemetry does."""
    backend = make_backend(tmp_path, n=1)
    backend._thumb_q.put(("acct0", _thumb(1)))
    backend._live_tick()
    assert json.loads(backend.state())["instances"][0]["live"]["phase"] \
        == "rendering"


def test_a_preview_never_touches_the_payload_from_the_stream_thread(qapp,
                                                                    tmp_path):
    """The discipline that stopped this app crashing: a stream thread puts
    on a queue and nothing else. Asserted by pushing from a real thread and
    checking the payload is untouched until the UI thread ticks."""
    backend = make_backend(tmp_path, n=1)

    thread = threading.Thread(
        target=lambda: backend._thumb_q.put(("acct0", _thumb(2))))
    thread.start()
    thread.join()

    assert json.loads(backend.state())["instances"][0]["live"] is None
    backend._live_tick()
    assert json.loads(backend.state())["instances"][0]["live"]["thumb"][
        "frame"] == 2


# ---------------------------------------------------------------------------
# The renders / packed outputs list on the Files page.
#
# "i believe files should have a view that displays the packed outputs --
# ie we have the render for waydown existing in the instances, press
# download to download all of them -- and we can have other pre-existing
# outputs too for previous projects." (2026-08-15.)
#
# The list is the jobs THIS app rendered (Fleet.load_jobs(), append-only),
# so a render the user remembers doing cannot silently vanish from it --
# not even once Kaggle has deleted the frames behind it. That case is
# LISTED AND LABELLED, which is the whole reason `availability` has four
# values rather than being a boolean.
# ---------------------------------------------------------------------------

def _outputs_backend(tmp_path, jobs, n=2):
    backend = make_backend(tmp_path, n=n)
    backend.fleet_factory(backend.store.list()).save_jobs(jobs)
    return backend


def _output_job(job_id, blend, *, labels=("acct0",), state="complete",
                finished_at=1.0, started_at=1000.0, end_frame=4,
                final_count_known=False, frames_done=0):
    return FleetState(
        job_id=job_id, blend_name=blend, start_frame=1, end_frame=end_frame,
        started_at=started_at,
        workers=[WorkerState(label=label,
                             username=f"user_{label[-1]}",
                             kernel_slug=f"user_{label[-1]}/{job_id}-render",
                             frames=[1, 2], state=state,
                             frames_done=frames_done,
                             final_count_known=final_count_known,
                             final_count_checked=True,
                             finished_at=finished_at)
                 for label in labels])


class _OutputListingClient(FakeClient):
    """A Kaggle whose per-kernel output listing is scripted by slug.

    A value that is an Exception is RAISED, so "Kaggle says there is
    nothing" and "this app could not ask" stay distinguishable -- which is
    the difference between a render being gone and being unknown.
    """

    by_slug: dict = {}

    def list_output_files(self, slug):
        answer = self.by_slug.get(slug, [])
        if isinstance(answer, Exception):
            raise answer
        return answer


def _listing_factory(tmp_path, by_slug):
    client = type("_Scripted", (_OutputListingClient,), {"by_slug": by_slug})
    return lambda accounts: Fleet(accounts, lambda t: client(t), tmp_path / "w")


def _outputs(backend):
    """Drive outputs() and return the emitted list."""
    seen = []
    backend.outputsChanged.connect(lambda j: seen.append(json.loads(j)))
    backend.outputs()
    assert seen, "outputs() emitted nothing"
    return seen[-1]["outputs"]


def test_the_outputs_list_is_every_tracked_job_newest_first(qapp, tmp_path):
    """load_jobs() is append-only and oldest-first. The render somebody
    wants to download is overwhelmingly the one that just finished, so the
    list is reversed -- and EVERY tracked job is in it, not just the
    newest, because "previous projects" is exactly what was asked for."""
    backend = _outputs_backend(tmp_path, [
        _output_job("job-old", "old-project.blend"),
        _output_job("job-mid", "another.blend", labels=("acct1",)),
        _output_job("job-new", "waydown.blend"),
    ])
    rows = _outputs(backend)
    assert [r["jobId"] for r in rows] == ["job-new", "job-mid", "job-old"]
    assert [r["scene"] for r in rows] == ["waydown", "another", "old-project"]


def test_the_outputs_list_answers_without_touching_the_network(qapp, tmp_path):
    """It is what the Files page draws with the moment it opens. FakeClient
    has no list_output_files at all, so an implementation that reached for
    Kaggle here would blow up rather than quietly get slower."""
    backend = _outputs_backend(tmp_path, [_output_job("job-1", "waydown.blend")])
    assert len(_outputs(backend)) == 1


def test_an_unchecked_render_claims_neither_available_nor_gone(qapp, tmp_path):
    backend = _outputs_backend(tmp_path, [_output_job("job-1", "waydown.blend")])
    row = _outputs(backend)[0]
    assert row["availability"] == "unchecked"
    assert row["checkedAgeSeconds"] is None
    assert row["availableAccounts"] == 0


def test_a_render_kaggle_has_deleted_is_still_listed_and_marked_gone(
        qapp, tmp_path):
    """Kaggle expires kernel output. The render DID happen, so the row
    stays -- what changes is that it says there is nothing left."""
    backend = _outputs_backend(
        tmp_path, [_output_job("job-1", "waydown.blend", labels=("acct0",))])
    backend.fleet_factory = _listing_factory(
        tmp_path, {"user_0/job-1-render": []})

    backend.checkOutputs()
    _settle(backend)

    row = _outputs(backend)[0]
    assert row["scene"] == "waydown", "a deleted render must stay listed"
    assert row["availability"] == "gone"
    assert row["checkedAccounts"] == 1 and row["availableAccounts"] == 0


def test_a_render_kaggle_still_has_is_marked_available(qapp, tmp_path):
    backend = _outputs_backend(
        tmp_path, [_output_job("job-1", "waydown.blend", labels=("acct0",))])
    backend.fleet_factory = _listing_factory(
        tmp_path, {"user_0/job-1-render": ["waydown.zip", "waydown_0001.png"]})

    backend.checkOutputs()
    _settle(backend)

    row = _outputs(backend)[0]
    assert row["availability"] == "available"
    assert row["availableAccounts"] == 1
    assert row["checkedAgeSeconds"] is not None


def test_an_account_that_could_not_be_asked_is_unknown_never_gone(
        qapp, tmp_path):
    """Kaggle no longer having it, and this app being unable to ask, are
    different sentences. Only the first may ever be shown as deleted."""
    backend = _outputs_backend(
        tmp_path, [_output_job("job-1", "waydown.blend", labels=("acct0",))])
    backend.fleet_factory = _listing_factory(
        tmp_path,
        {"user_0/job-1-render": KaggleError("could not reach Kaggle")})

    backend.checkOutputs()
    _settle(backend)

    row = _outputs(backend)[0]
    assert row["availability"] == "unknown"
    assert "acct0" in row["availabilityErrors"]
    assert "Kaggle" in row["availabilityErrors"]["acct0"]


def test_one_account_still_holding_frames_makes_the_render_available(
        qapp, tmp_path):
    """A partly-expired render is still worth downloading, and the row
    says how much of it answered."""
    backend = _outputs_backend(
        tmp_path,
        [_output_job("job-1", "waydown.blend", labels=("acct0", "acct1"))])
    backend.fleet_factory = _listing_factory(tmp_path, {
        "user_0/job-1-render": ["waydown.zip"],
        "user_1/job-1-render": [],
    })

    backend.checkOutputs()
    _settle(backend)

    row = _outputs(backend)[0]
    assert row["availability"] == "available"
    assert row["availableAccounts"] == 1 and row["workerCount"] == 2


def test_a_half_answered_check_is_unknown_not_gone(qapp, tmp_path):
    """One account said nothing was there and the other could not be asked
    at all. That is not proof Kaggle deleted the render."""
    backend = _outputs_backend(
        tmp_path,
        [_output_job("job-1", "waydown.blend", labels=("acct0", "acct1"))])
    backend.fleet_factory = _listing_factory(tmp_path, {
        "user_0/job-1-render": [],
        "user_1/job-1-render": KaggleError("rate limited"),
    })

    backend.checkOutputs()
    _settle(backend)

    assert _outputs(backend)[0]["availability"] == "unknown"


def test_an_availability_answer_survives_a_later_plain_refresh(qapp, tmp_path):
    """outputs() must not flick a checked row back to unchecked -- it is
    called again whenever the list is redrawn."""
    backend = _outputs_backend(
        tmp_path, [_output_job("job-1", "waydown.blend", labels=("acct0",))])
    backend.fleet_factory = _listing_factory(
        tmp_path, {"user_0/job-1-render": ["waydown.zip"]})
    backend.checkOutputs()
    _settle(backend)

    assert _outputs(backend)[0]["availability"] == "available"


def test_a_render_never_claims_a_frame_count_it_cannot_confirm(qapp, tmp_path):
    """frames_done off a live stream is a floor from some moment before
    the render ended, not a total (see _frames_done_source). The row shows
    the frame RANGE, which is a fact, and refuses the done count until
    every account own kernel log has been read back for it."""
    backend = _outputs_backend(tmp_path, [
        _output_job("job-guess", "guess.blend", frames_done=3),
        _output_job("job-final", "final.blend", frames_done=4,
                    final_count_known=True),
    ])
    rows = {r["jobId"]: r for r in _outputs(backend)}
    assert rows["job-guess"]["framesDoneKnown"] is False
    assert rows["job-final"]["framesDoneKnown"] is True
    assert rows["job-final"]["framesDone"] == 4
    # The range is stated either way.
    assert rows["job-guess"]["frameCount"] == 4


def test_a_render_with_no_recorded_start_says_so_rather_than_1970(
        qapp, tmp_path):
    backend = _outputs_backend(
        tmp_path, [_output_job("job-1", "waydown.blend", started_at=0.0)])
    assert _outputs(backend)[0]["ageSeconds"] is None


def test_an_unfinished_render_is_listed_and_not_called_finished(
        qapp, tmp_path):
    backend = _outputs_backend(tmp_path, [
        _output_job("job-1", "waydown.blend", state="running", finished_at=0.0),
    ])
    row = _outputs(backend)[0]
    assert row["finished"] is False
    assert row["accounts"] == ["acct0"]


# ---- downloading one render from the Files page ---------------------------

def test_a_files_download_gets_its_own_busy_key(qapp, tmp_path, monkeypatch):
    """Keyed per RENDER, because that is what the button is one of. The
    fleet-wide button exact-match "collect:" and the per-instance
    "collect:<label>" keys must keep matching exactly as before, which is
    why this is its own prefix and not a third segment of that key."""
    backend = _two_job_collect_backend(tmp_path)
    seen_job_ids = []
    _stub_collect_frames(monkeypatch, tmp_path, seen_job_ids)
    keys = []
    backend.busyChanged.connect(lambda k, b: keys.append((k, b)))

    backend.collect("", "job-a")
    _settle(backend)

    assert seen_job_ids == ["job-a"]
    assert ("collect-job:job-a", True) in keys
    assert ("collect-job:job-a", False) in keys
    assert not any(k.startswith("collect:") for k, _ in keys)


def test_the_fleet_wide_button_keeps_its_exact_key(qapp, tmp_path,
                                                   monkeypatch):
    backend = _two_job_collect_backend(tmp_path)
    _stub_collect_frames(monkeypatch, tmp_path, [])
    keys = []
    backend.busyChanged.connect(lambda k, b: keys.append((k, b)))

    backend.collect("")
    _settle(backend)

    assert ("collect:", True) in keys and ("collect:", False) in keys


def _progress_collect(monkeypatch, tmp_path, ticks, report=None):
    """Stub collector.collect so it emits the given progress ticks."""
    import blendfleet.collector as collector_mod
    from blendfleet.collector import CollectReport

    def fake_collect(state, accounts, client_factory, dest, *,
                     worker_label=None, on_progress=None):
        for label, downloaded, total in ticks:
            on_progress(label, type("P", (), {
                "downloaded": downloaded, "total": total,
                "rate_bps": 1024})())
        return report if report is not None else CollectReport(copied=3)

    monkeypatch.setattr(collector_mod, "collect", fake_collect)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: str(tmp_path / "out"))


def test_download_progress_says_which_render_and_how_many_accounts(
        qapp, tmp_path, monkeypatch):
    """Carried on the EXISTING downloadProgress signal -- the instance
    cards still read `label` -- with just enough added for the page to add
    the per-account ticks up into one figure for the render. The account
    COUNT has to come from here: a page counting whoever has reported so
    far would read as complete the moment the first account finished."""
    backend = _two_job_collect_backend(tmp_path)
    _progress_collect(monkeypatch, tmp_path, [("acct0", 500, 1000)])
    ticks = []
    backend.downloadProgress.connect(lambda j: ticks.append(json.loads(j)))

    backend.collect("", "job-a")
    _settle(backend)

    assert ticks and ticks[0]["jobId"] == "job-a"
    assert ticks[0]["label"] == "acct0"
    assert ticks[0]["jobWorkers"] == 1
    assert ticks[0]["downloaded"] == 500 and ticks[0]["total"] == 1000


def test_a_per_instance_download_reports_one_account_not_the_whole_job(
        qapp, tmp_path, monkeypatch):
    """collector.collect filters to that one worker, so the roll-up must
    be told to expect one -- otherwise the job bar waits for accounts this
    download was never going to fetch from."""
    backend = _two_job_collect_backend(tmp_path)
    _progress_collect(monkeypatch, tmp_path, [("acct0", 10, 20)])
    ticks = []
    backend.downloadProgress.connect(lambda j: ticks.append(json.loads(j)))

    backend.collect("acct0")
    _settle(backend)

    assert ticks[0]["jobWorkers"] == 1


def test_a_finished_download_tells_the_row_where_the_zip_went(
        qapp, tmp_path, monkeypatch):
    """The row that started it says where the file is, rather than the
    path living only in a toast that fades."""
    from blendfleet.collector import CollectReport
    backend = _two_job_collect_backend(tmp_path)
    archive = tmp_path / "out" / "alpha.zip"
    _progress_collect(monkeypatch, tmp_path, [("acct0", 10, 10)],
                      report=CollectReport(copied=3, archive_path=archive))
    done = []
    backend.collectFinished.connect(lambda j: done.append(json.loads(j)))

    backend.collect("", "job-a")
    _settle(backend)

    assert done and done[0]["jobId"] == "job-a"
    assert done[0]["archivePath"] == str(archive)
    assert done[0]["copied"] == 3
    assert "alpha.zip" in done[0]["message"]


def test_a_download_that_wrote_no_zip_does_not_name_one(qapp, tmp_path,
                                                        monkeypatch):
    """collect() writes no zip when nothing came back. Absent, not zero --
    naming a file that is not there sends the user hunting for it."""
    from blendfleet.collector import CollectReport
    backend = _two_job_collect_backend(tmp_path)
    _progress_collect(monkeypatch, tmp_path, [],
                      report=CollectReport(copied=0))
    done = []
    backend.collectFinished.connect(lambda j: done.append(json.loads(j)))

    backend.collect("", "job-a")
    _settle(backend)

    assert done and done[0]["archivePath"] == ""
    assert done[0]["destination"] == str(tmp_path / "out")
