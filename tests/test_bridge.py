"""The web UI's only source of truth is the Backend payload, so the shape
of that payload -- and its honesty guarantees -- are what these cover.

The page cannot be trusted to enforce any of this: it is HTML that anyone
can edit, and the design it was ported from shipped a full simulation. If
a guarantee matters, it has to hold here.
"""
import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from blendfleet.accounts import Account, AccountStore
from blendfleet.fleet import Fleet, FleetState, WorkerState
from blendfleet.instance_state import GpuSnapshot, InstanceSnapshot
from blendfleet.kaggle_client import Quota
from blendfleet.settings import Settings
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
    backend._last_state = FleetState(
        job_id="job", blend_name="scene.blend", start_frame=1, end_frame=8,
        workers=[WorkerState(label="acct1", username="user_1",
                             kernel_slug="user_1/k", frames=[1, 3, 5],
                             state="running", frames_done=2)])
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
                   on_progress=None, dataset_slug=None):
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
                   on_progress=None, dataset_slug=None):
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
                            usernames=None, on_stage=None):
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
                            usernames=None, on_stage=None):
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


def _preview_backend(tmp_path, monkeypatch, frames_by_label):
    """A Backend with a saved job, whose fleet hands out PreviewClients."""
    import blendfleet.ui.bridge as bridge_mod
    monkeypatch.setattr(bridge_mod, "state_dir", lambda: tmp_path / "state")

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
    backend = _preview_backend(tmp_path, monkeypatch,
                               {"a": [1, 3, 5], "b": [2, 4, 6]})
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
    backend = _preview_backend(tmp_path, monkeypatch,
                               {"a": [1, 3, 5], "b": [2, 4, 6]})
    backend.previewFrame(1)
    _settle(backend)
    _LIVE_BACKENDS.remove(backend)

    names = [name for _t, _s, name in PreviewClient.calls]
    assert names == ["f_0001.png"], names


def test_a_second_look_at_the_same_frame_is_served_from_cache(
        qapp, tmp_path, monkeypatch):
    PreviewClient.calls = []
    backend = _preview_backend(tmp_path, monkeypatch, {"a": [1, 2]})
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
    backend = _preview_backend(tmp_path, monkeypatch, {"a": [1, 2]})
    notes = []
    backend.notification.connect(lambda m, t: notes.append(m))
    backend.previewFrame(99)
    _LIVE_BACKENDS.remove(backend)
    assert notes and "not assigned" in notes[0]
