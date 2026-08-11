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
