import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import SIGNAL, QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

import blendfleet.platform_paths as pp
import blendfleet.ui.dashboard as dashboard_mod
import blendfleet.ui.theme as theme
from blendfleet.accounts import Account, AccountStore
from blendfleet.fleet import Fleet, FleetState, WorkerState
from blendfleet.instance_state import GpuSnapshot, InstanceSnapshot, InstanceStore
from blendfleet.kaggle_client import KaggleError, KernelStatus, Quota
from blendfleet.notebook_builder import RenderSettings
from blendfleet.settings import Settings
from blendfleet.ui.dashboard import Dashboard
from blendfleet.ui.sidebar import Sidebar
from blendfleet.ui.theme import ACCENTS


@pytest.fixture(autouse=True)
def tmp_cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_active_accent(qapp):
    """test_switching_accent_live_repaints_the_brand_mark_without_restart
    below calls theme.apply() with every non-default accent -- see
    test_theme.py's fixture of the same name for why that process-global
    state must not leak into other test modules run later in the same
    session.

    Only actually calls theme.apply() -- which restyles the WHOLE
    QApplication, including every widget any earlier test in this
    session left alive under the one shared QApplication -- when a test
    genuinely changed the accent. Measured: calling it unconditionally on
    every test's teardown (this file, test_instance_card.py and
    test_settings_view.py all did) made test_dashboard.py alone take 75s
    for 36 tests instead of a few seconds, growing call over call, and
    made a full-suite run look like a hang rather than a slow pass. Only
    a handful of tests in this file ever change the accent; the other
    ~30+ do not need this at all.
    """
    original = theme._active_accent_name
    yield
    if theme._active_accent_name != original:
        theme._active_accent_name = original
        theme.apply(qapp, original)


@pytest.fixture(autouse=True)
def stub_stream_progress(monkeypatch):
    """No test may reach Kaggle's live SSE log stream.

    Dashboard._start_progress_threads spawns one thread per worker straight
    into log_stream.stream_progress, which opens a real HTTPS connection.
    test_launch_success_updates_upload_filmstrip_and_table did not stub it,
    so it did exactly that -- with fake KGAT_000… tokens -- and the threads
    were never joined, outliving the module and dying inside
    ssl.do_handshake while a later module ran (2 of 14 clean runs aborted).

    Autouse rather than per-test: the leak was one missing stub away, and
    "remember to stub it" is not a property a merge gate can rely on. A
    test that wants to observe the streaming wiring overrides this with its
    own fake (see
    test_start_progress_threads_feeds_live_progress_and_telemetry).
    """
    def no_stream(token, user_name, kernel_slug, on_progress,
                  stop_event=None, on_telemetry=None, on_hardware=None):
        return None

    monkeypatch.setattr(dashboard_mod, "stream_progress", no_stream)
    return no_stream


# Every Dashboard a test builds, torn down deterministically below. A
# Dashboard owns QThreads and QTimers as Qt children; left to Python's
# garbage collector it gets destroyed at an arbitrary later allocation --
# possibly in the middle of an unrelated test -- and a C++ QThread
# destroyed while its thread is still running aborts the process outright.
_LIVE_DASHBOARDS: list = []


@pytest.fixture(autouse=True)
def close_dashboards(qapp):
    yield
    while _LIVE_DASHBOARDS:
        dash = _LIVE_DASHBOARDS.pop()
        dash.close()      # stops timers, signals the SSE threads, joins workers
        settle(dash)
        dash.deleteLater()
    # deleteLater is only honoured while events are being processed; without
    # this the C++ objects would still be alive and back on the GC's terms.
    #
    # processEvents() alone is not enough, though it looks like it should
    # be: measured directly (no test framework involved) that 30+ calls to
    # plain processEvents() after deleteLater() leave every widget of a
    # closed, deleteLater()'d Dashboard still in QApplication.allWidgets()
    # -- Qt does not fold DeferredDelete into a manual processEvents() pass
    # the way it does for a real app.exec() loop. The explicit
    # sendPostedEvents(None, DeferredDelete) call below is what actually
    # flushes it; without it every Dashboard built by an earlier test in
    # this module survives (as real, live QWidgets) for the rest of the
    # session, and each one makes theme.apply()'s app.setStyleSheet() --
    # which restyles every widget currently in the QApplication, not just
    # the one that changed -- slower for every subsequent accent switch.
    # This is what actually explained test_dashboard.py's runtime, not (or
    # not only) the theme_signal disconnect leak fixed alongside this.
    for _ in range(20):
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def pump(worker, timeout=2000) -> None:
    assert worker is not None
    assert worker.wait(timeout), "worker did not finish in time"
    for _ in range(10):
        QCoreApplication.processEvents()


def settle(dash, timeout=2000) -> None:
    """Wait for whichever background _CallWorker(s) are in flight (e.g.
    the quota refresh Dashboard.__init__ kicks off) and drain their
    queued cross-thread signals, so a test sees a settled state instead
    of racing a background thread. Also used as teardown: closing the
    dashboard runs the same wait via closeEvent, so no QThread is ever
    left running (and possibly garbage-collected mid-flight) once a test
    function returns.
    """
    workers = [dash._poll_worker, dash._quota_worker,
               dash._cancel_worker, dash._collect_worker]
    # Task 3/4/6: per-label worker dicts -- more than one can legitimately
    # be in flight at once (two different cards' cancels, two different
    # accounts' failure-log fetches, or two different cards' downloads).
    workers += list(dash._instance_cancel_workers.values())
    workers += list(dash._log_fetch_workers.values())
    workers += list(dash._instance_download_workers.values())
    for worker in workers:
        if worker is None:
            continue
        try:
            assert worker.wait(timeout), "background worker did not finish in time"
        except RuntimeError:
            # The worker finished and its deleteLater() was processed, so
            # the C++ QThread is already gone -- which is the state this
            # was waiting for. Same case Dashboard.closeEvent handles.
            pass
    for _ in range(10):
        QCoreApplication.processEvents()


def wait_until(condition, timeout=2.0) -> None:
    """Poll `condition` (a no-arg callable) until it is true, pumping the
    Qt event loop between checks, rather than a fixed processEvents()
    count. Window-state changes (showFullScreen()/showMaximized(), and
    the child visibility that follows from them) apply synchronously in
    isolation, but were observed to occasionally lag by a tick or two
    under the offscreen QPA platform after many windows have already
    been created and torn down earlier in the same test session --
    exactly the kind of eventual-consistency settle() above exists to
    wait out for background workers.
    """
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            assert condition(), "condition was never satisfied in time"
        QCoreApplication.processEvents()
        time.sleep(0.01)


# stub_message_boxes now lives in tests/conftest.py as a fixture -- the
# sanctioned opt-in for a test that legitimately expects a dialog, paired
# with the autouse no_unstubbed_dialogs guard that fails loudly (instead of
# hanging) on any QMessageBox this module doesn't explicitly stub.


class FakeSdk:
    def __init__(self):
        self.datasets = type("D", (), {
            "dataset_api_client": type("C", (), {
                "get_dataset_metadata": lambda self, req: type(
                    "R", (), {"info": type("I", (), {
                        "title": "", "licenses": [], "collaborators": []})()})(),
                "update_dataset_metadata": lambda self, req: type(
                    "R", (), {"errors": []})(),
            })()})()


class FakeClient:
    """Network-free stand-in for KaggleClient, in the spirit of
    tests/test_fleet.py's FakeClient -- but this one also emits a fake
    UploadProgress tick, so the dashboard's launch-worker -> upload_view
    wiring can be exercised end-to-end without a real Kaggle upload."""

    fail_upload = False
    # Class-level, like fail_upload: a dataset lives on Kaggle, not inside
    # one client, so an upload by the owner is visible to every account.
    uploaded = False

    def __init__(self, token, state="running", message=""):
        self.token = token
        self.state = state
        self.message = message
        self.cancelled: list[str] = []
        self.sdk = FakeSdk()
        self._sdk_factory = lambda tok: self.sdk

    def whoami(self):
        return "user_" + self.token[-1]

    def dataset_exists(self, slug):
        return False

    def dataset_reachable(self, slug):
        return True

    def dataset_file_size(self, slug, filename):
        # Task 5: fleet.launch() now confirms every account's visible copy
        # of the .blend matches the local file's size before pushing any
        # kernel. Every blend this module's launch-path tests actually
        # write is 100 bytes (see the `blend.write_bytes(b"x" * 100)` calls
        # below) -- this suite exercises the dashboard's wiring, not Task
        # 5's staleness detection itself (see tests/test_fleet.py for
        # that), so it must match rather than spuriously fail launch.
        #
        # None BEFORE anything is uploaded, though: prepare_dataset now
        # asks Kaggle whether the scene is already there and skips the
        # upload if it is. A fake that answers "already there" from the
        # first call makes every launch skip the upload -- which silently
        # disabled the test that forces an upload FAILURE.
        if not FakeClient.uploaded:
            return None
        return 100

    def dataset_create(self, folder, on_progress=None):
        if on_progress:
            from blendfleet.uploader import UploadProgress
            on_progress(UploadProgress(uploaded=50, total=100,
                                       rate_bps=10.0, retries=0,
                                       resumed_from=0))
        if FakeClient.fail_upload:
            raise KaggleError(
                "Dataset creation failed: the .blend file did not finish "
                "uploading to Kaggle -- retry the render.")
        FakeClient.uploaded = True

    def dataset_version(self, folder, message, on_progress=None):
        self.dataset_create(folder, on_progress=on_progress)

    def push_kernel(self, folder, timeout_seconds=0):
        pass

    def status(self, slug):
        return KernelStatus(state=self.state, message=self.message)

    def cancel(self, slug):
        self.cancelled.append(slug)
        return True

    def quota(self):
        return Quota(0, 21600, "2026-08-01", source="api")

    def fetch_output(self, slug, dest):
        return []

    def fetch_log_tail(self, slug, dest, max_lines=200):
        return ""


class RefusingCancelClient(FakeClient):
    """cancel() returns False, exactly as the real client does on error."""
    def cancel(self, slug):
        self.cancelled.append(slug)
        return False


class RaisingCancelClient(FakeClient):
    """cancel() raises, as if the network call or auth blew up."""
    def cancel(self, slug):
        raise RuntimeError("boom")


def make_store(n=3):
    store = AccountStore()
    for i in range(n):
        store.add(Account(label=f"acct{i}", token="KGAT_" + str(i) * 32,
                          username=f"user_{i}", verified=True))
    return store


def make_dashboard(qapp, tmp_path, n=3, store=None):
    """Build a Dashboard and register it for deterministic teardown.

    Everything goes through here (rather than constructing Dashboard
    inline) so no instance can escape close_dashboards' cleanup.
    """
    store = make_store(n) if store is None else store

    def fleet_factory(accounts):
        return Fleet(accounts, lambda tok: FakeClient(tok), tmp_path / "w")

    dash = Dashboard(store, fleet_factory, verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)   # let __init__'s initial quota-refresh worker finish
    return dash


# ---------------- empty state ----------------

def test_empty_state_shows_no_frames_and_placeholders(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path)
    assert dash.filmstrip.total_frames == 0
    assert dash.filmstrip_caption.text() == "no frames yet"
    assert not dash.upload_view._rows
    assert dash.gpu_panel.gpu_count == 0
    assert dash.instances_layout.count() == 3
    assert dash.poll_status_label.text() == ""
    dash.close()


def test_dashboard_page_shows_one_card_slot_per_account(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path, n=3)
    assert dash.instances_layout.count() == 3
    dash.close()


# ---------------- InstanceCard wiring (Task 4) ----------------

def test_dashboard_page_shows_one_instance_card_per_account_starting_idle(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path, n=3)
    assert set(dash._instance_cards) == {"acct0", "acct1", "acct2"}
    for card in dash._instance_cards.values():
        assert card.status_word.text() == "idle"
        assert card.idle_container.isHidden() is False
        assert card.live_container.isHidden() is True
    dash.close()


def test_quota_cache_flows_into_instance_cards(qapp, tmp_path):
    """make_dashboard() already waits out __init__'s initial quota-refresh
    worker -- FakeClient.quota() returns Quota(0, 21600, ...), i.e. 6h
    total, 0 used."""
    dash = make_dashboard(qapp, tmp_path, n=1)
    card = dash._instance_cards["acct0"]
    assert card.quota_value.text() == "0.0 / 6.0 h"
    assert card.quota_marker.text() == "live"
    dash.close()


def test_last_known_hardware_flows_into_the_idle_instance_card(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path, n=1)
    dash.instance_store.record("acct0", InstanceSnapshot(
        username="user_0",
        gpus=[GpuSnapshot(index=0, mem_total=16280,
                          model="Tesla P100-PCIE-16GB")],
        cpu_count=4, ram_total=31.3, observed_at=time.time() - 7200))
    dash._refresh_views()

    card = dash._instance_cards["acct0"]
    assert "2h ago" in card.last_run_value.text()
    assert "Tesla P100-PCIE-16GB" in card.last_run_value.text()
    assert "4 vCPU" in card.last_run_value.text()
    assert "31.3 GB" in card.last_run_value.text()
    dash.close()


def test_live_telemetry_flows_into_the_matching_instance_card_only(qapp, tmp_path):
    """Two accounts, telemetry for only one -- the OTHER account's card
    must show nothing live, per-account, never aggregated."""
    dash = make_dashboard(qapp, tmp_path, n=2)
    dash._last_state = FleetState(
        job_id="job", blend_name="x.blend", start_frame=1, end_frame=2,
        workers=[
            WorkerState(label="acct0", username="user_0",
                       kernel_slug="user_0/k0", frames=[1, 2], state="running"),
            WorkerState(label="acct1", username="user_1",
                       kernel_slug="user_1/k1", frames=[1, 2], state="running"),
        ])
    dash._refresh_views()

    dash._telemetry_queue.put(("acct0", {
        "gpu": 0, "util": 87, "mem_used": 6144, "mem_total": 15360,
        "temp": 71, "power": 58.0}))
    dash._live_tick()

    assert 0 in dash._instance_cards["acct0"]._gpu_rows
    assert dash._instance_cards["acct1"]._gpu_rows == {}
    dash.close()


# --------- PREFLIGHT: real hardware within seconds, before TELEMETRY -----

def test_preflight_flows_into_the_matching_instance_card_only(qapp, tmp_path):
    """PREFLIGHT arrives before Blender is even downloaded -- well before
    a worker could plausibly be "running" -- so a merely queued worker
    must already see it, and per-account, never the other account's."""
    dash = make_dashboard(qapp, tmp_path, n=2)
    dash._last_state = FleetState(
        job_id="job", blend_name="x.blend", start_frame=1, end_frame=2,
        workers=[
            WorkerState(label="acct0", username="user_0",
                       kernel_slug="user_0/k0", frames=[1, 2], state="queued"),
            WorkerState(label="acct1", username="user_1",
                       kernel_slug="user_1/k1", frames=[1, 2], state="queued"),
        ])
    dash._refresh_views()

    dash._preflight_queue.put(("acct0", {
        "gpu_count": 2, "gpu_names": ["Tesla T4", "Tesla T4"],
        "cpu_count": 4, "ram_total": 31.3}))
    dash._live_tick()

    card0 = dash._instance_cards["acct0"]
    card1 = dash._instance_cards["acct1"]
    assert "2x Tesla T4" in card0.preflight_label.text()
    assert card0.live_container.isHidden() is False
    assert card1.preflight_label.text() == ""
    dash.close()


def test_preflight_cleared_at_the_start_of_a_new_render(qapp, tmp_path):
    """A card must never open showing the PREVIOUS run's PREFLIGHT text."""
    dash = make_dashboard(qapp, tmp_path, n=1)
    dash._last_state = FleetState(
        job_id="job", blend_name="x.blend", start_frame=1, end_frame=1,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/k0", frames=[1],
                             state="queued")])
    dash._refresh_views()
    dash._preflight_queue.put(("acct0", {
        "gpu_count": 1, "gpu_names": ["Tesla T4"],
        "cpu_count": 4, "ram_total": 31.3}))
    dash._live_tick()
    assert "Tesla T4" in dash._instance_cards["acct0"].preflight_label.text()

    dash._start_progress_threads(FleetState("job2", "x.blend", 1, 1, []))
    assert dash._instance_cards["acct0"].preflight_label.text() == ""
    dash.close()


def test_launch_success_reflects_in_the_instance_card_status(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path, n=1)
    blend = tmp_path / "remember.blend"
    blend.write_bytes(b"x" * 100)
    dash.blend = blend
    dash.start.setValue(1)
    dash.end.setValue(4)
    dash._launch()
    pump(dash._launch_worker)

    card = dash._instance_cards["acct0"]
    # fleet.WorkerState defaults every freshly-launched worker to "queued"
    # -- Kaggle has not been polled yet, so the card must say exactly that,
    # not jump straight to "rendering".
    assert card.status_word.text() == "queued"
    dash.close()


def test_telemetry_arriving_while_still_queued_shows_a_live_card(qapp, tmp_path, monkeypatch):
    """The same poll/telemetry race blendfleet/ui/instance_card.py's
    InstanceCard docstring describes, exercised end-to-end: dashboard.py's
    30s status poll has not run yet (the worker is still "queued"), but a
    telemetry sample has already arrived on the SSE stream -- the card's
    live body must reflect that immediately, not wait for the next poll."""
    def fake_stream_progress(token, user_name, kernel_slug, on_progress,
                             stop_event=None, on_telemetry=None,
                             on_hardware=None, on_preflight=None):
        on_progress(1, 4)
        if on_telemetry:
            on_telemetry({"gpu": 0, "util": 50, "mem_used": 100,
                         "mem_total": 200, "temp": 60, "power": 10.0})

    monkeypatch.setattr(dashboard_mod, "stream_progress", fake_stream_progress)

    dash = make_dashboard(qapp, tmp_path, n=1)
    blend = tmp_path / "remember.blend"
    blend.write_bytes(b"x" * 100)
    dash.blend = blend
    dash.start.setValue(1)
    dash.end.setValue(4)
    dash._launch()
    pump(dash._launch_worker)

    deadline = time.monotonic() + 2.0
    while not dash._live_progress and time.monotonic() < deadline:
        time.sleep(0.01)
    dash._live_tick()

    card = dash._instance_cards["acct0"]
    assert card.status_word.text() == "queued"        # poll hasn't run yet
    assert card.live_container.isHidden() is False    # telemetry already is
    assert 0 in card._gpu_rows
    dash.close()


# ---------------- launch success: in-progress -> complete ----------------

def test_launch_success_updates_upload_filmstrip_and_table(qapp, tmp_path):
    monkeypatch_targets = []
    FakeClient.fail_upload = False
    FakeClient.uploaded = False
    dash = make_dashboard(qapp, tmp_path)
    blend = tmp_path / "remember.blend"
    blend.write_bytes(b"x" * 100)
    dash.blend = blend
    dash.start.setValue(1)
    dash.end.setValue(9)

    dash._launch()
    assert dash.render_btn.text() == "Starting…"
    assert not dash.render_btn.isEnabled()
    # the owner's row exists immediately, even before the worker finishes
    assert "acct0" in dash.upload_view._rows

    pump(dash._launch_worker)

    assert dash.render_btn.isEnabled()
    assert dash.render_btn.text() == "RENDER ACROSS FLEET"
    assert dash.upload_view._rows["acct0"].state == "complete"
    assert dash._last_state is not None
    assert len(dash._last_state.workers) == 3
    assert dash.filmstrip.total_frames == 9
    assert dash.table.rowCount() == 3
    dash.close()


def test_launch_failure_shows_friendly_message_not_raw_exception(qapp, tmp_path, stub_message_boxes):
    calls = stub_message_boxes
    FakeClient.fail_upload = True
    FakeClient.uploaded = False
    try:
        dash = make_dashboard(qapp, tmp_path)
        blend = tmp_path / "remember.blend"
        blend.write_bytes(b"x" * 100)
        dash.blend = blend
        dash.start.setValue(1)
        dash.end.setValue(9)

        dash._launch()
        pump(dash._launch_worker)

        assert dash.render_btn.isEnabled()
        assert dash.upload_view._rows["acct0"].state == "failed"
        assert calls["critical"], "expected a critical dialog on launch failure"
        title, message = calls["critical"][0]
        assert "did not finish uploading" in message
        assert "retry the render" in message
        # never a bare traceback/exception repr
        assert "Traceback" not in message
        dash.close()
    finally:
        FakeClient.fail_upload = False
    FakeClient.uploaded = False


def test_launch_with_no_accounts_shows_actionable_warning(qapp, tmp_path, stub_message_boxes):
    calls = stub_message_boxes
    dash = make_dashboard(qapp, tmp_path, store=AccountStore())
    dash.blend = tmp_path / "x.blend"
    dash._launch()
    assert calls["warning"]
    title, message = calls["warning"][0]
    assert "add" in message.lower()
    dash.close()


def test_launch_with_bad_frame_range_shows_actionable_warning(qapp, tmp_path, stub_message_boxes):
    calls = stub_message_boxes
    dash = make_dashboard(qapp, tmp_path)
    blend = tmp_path / "remember.blend"
    blend.write_bytes(b"x" * 10)
    dash.blend = blend
    dash.start.setValue(10)
    dash.end.setValue(1)
    dash._launch()
    assert calls["warning"]
    assert "end frame" in calls["warning"][0][1].lower()
    dash.close()


# ---------------- GPU telemetry drains onto the UI thread ----------------

def test_live_tick_drains_queued_telemetry_into_gpu_panel(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path)
    dash._telemetry_queue.put(("acct0", {
        "gpu": 0, "util": 87, "mem_used": 6144, "mem_total": 15360,
        "temp": 71, "power": 58.0}))
    dash._telemetry_queue.put(("acct0", {
        "gpu": 1, "util": 12, "mem_used": 1024, "mem_total": 15360,
        "temp": 45, "power": None}))
    dash._live_tick()
    assert dash.gpu_panel.gpu_count == 2
    dash.close()


def test_start_progress_threads_feeds_live_progress_and_telemetry(qapp, tmp_path, monkeypatch):
    def fake_stream_progress(token, user_name, kernel_slug, on_progress,
                             stop_event=None, on_telemetry=None,
                             on_hardware=None, on_preflight=None):
        on_progress(2, 3)
        if on_telemetry:
            on_telemetry({"gpu": 0, "util": 50, "mem_used": 100,
                         "mem_total": 200, "temp": 60, "power": 10.0})

    monkeypatch.setattr(dashboard_mod, "stream_progress", fake_stream_progress)

    dash = make_dashboard(qapp, tmp_path)
    blend = tmp_path / "remember.blend"
    blend.write_bytes(b"x" * 100)
    dash.blend = blend
    dash.start.setValue(1)
    dash.end.setValue(9)
    dash._launch()
    pump(dash._launch_worker)

    # background threads are daemon threads started by _start_progress_threads;
    # give them a moment to run the (now instantaneous) fake stream.
    deadline = time.monotonic() + 2.0
    while not dash._live_progress and time.monotonic() < deadline:
        time.sleep(0.01)

    assert dash._live_progress   # at least one worker reported live progress
    dash._live_tick()
    assert dash.gpu_panel.gpu_count >= 1
    dash.close()


# ---------------- last-known instance hardware (Task 3) ----------------

def test_live_tick_records_instance_snapshot_from_telemetry(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path)
    dash._telemetry_queue.put(("acct0", {
        "gpu": 0, "util": 87, "mem_used": 6144, "mem_total": 15360,
        "temp": 71, "power": 58.0}))
    dash._telemetry_queue.put(("acct0", {
        "gpu": 1, "util": 12, "mem_used": 1024, "mem_total": 15360,
        "temp": 45, "power": None}))
    dash._live_tick()

    snap = dash.instance_store.get("acct0")
    assert snap is not None
    assert snap.username == "user_0"   # make_store's Account.username
    assert snap.gpus == [GpuSnapshot(index=0, mem_total=15360),
                         GpuSnapshot(index=1, mem_total=15360)]
    # No source for these through the telemetry wiring -- left None, not
    # guessed.
    assert snap.cpu_count is None
    assert snap.ram_total is None
    dash.close()


def test_instance_snapshot_is_persisted_to_disk(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path)
    dash._telemetry_queue.put(("acct0", {
        "gpu": 0, "util": 50, "mem_used": 100, "mem_total": 200,
        "temp": 60, "power": 10.0}))
    dash._live_tick()
    dash.close()

    reloaded = InstanceStore.load()
    assert reloaded.get("acct0").gpus == [GpuSnapshot(index=0, mem_total=200)]


def test_instance_snapshot_recorded_once_per_run_not_per_sample(qapp, tmp_path):
    """Persisting to disk on every telemetry sample would be wasteful --
    the account's snapshot for the current run is written once, on the
    first telemetry seen for it, not re-written as later samples for the
    same GPU keep arriving every ~5s."""
    dash = make_dashboard(qapp, tmp_path)
    dash._telemetry_queue.put(("acct0", {
        "gpu": 0, "util": 50, "mem_used": 100, "mem_total": 200,
        "temp": 60, "power": 10.0}))
    dash._live_tick()
    first = dash.instance_store.get("acct0")
    assert first is not None

    # A later sample for the SAME run must not move observed_at forward or
    # otherwise re-record.
    dash._telemetry_queue.put(("acct0", {
        "gpu": 0, "util": 99, "mem_used": 150, "mem_total": 200,
        "temp": 65, "power": 12.0}))
    dash._live_tick()
    second = dash.instance_store.get("acct0")
    assert second.observed_at == first.observed_at
    dash.close()


def test_instance_snapshot_re_recorded_on_next_render(qapp, tmp_path):
    """Kaggle's allocation varies between runs, so the once-per-run guard
    must reset when a NEW render starts, not stay latched forever."""
    dash = make_dashboard(qapp, tmp_path)
    dash._telemetry_queue.put(("acct0", {
        "gpu": 0, "util": 50, "mem_used": 100, "mem_total": 200,
        "temp": 60, "power": 10.0}))
    dash._live_tick()
    first = dash.instance_store.get("acct0")

    time.sleep(0.01)   # observed_at must move forward on the next record
    dash._start_progress_threads(FleetState("job", "b.blend", 1, 1, []))
    dash._telemetry_queue.put(("acct0", {
        "gpu": 0, "util": 20, "mem_used": 80, "mem_total": 200,
        "temp": 55, "power": 8.0}))
    dash._live_tick()
    second = dash.instance_store.get("acct0")
    assert second.observed_at > first.observed_at
    dash.close()


def test_account_with_no_history_has_no_snapshot(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path)
    assert dash.instance_store.get("acct0") is None
    dash.close()


# --------- hardware banner (cpu/ram/GPU model) rides the same stream ---------

def test_live_tick_records_hardware_banner_fields_end_to_end(qapp, tmp_path):
    """The notebook's first cell prints its hardware banner before the
    render loop's TELEMETRY lines start -- exercised here in that same
    order -- and _record_instance_snapshot must fold both queues into one
    InstanceSnapshot with cpu_count, ram_total, and per-GPU model filled
    in, not left None."""
    dash = make_dashboard(qapp, tmp_path)
    dash._hardware_queue.put(("acct0", {
        "kind": "cpu_ram", "cpu_count": 4, "ram_total": 31.3}))
    dash._hardware_queue.put(("acct0", {
        "kind": "gpu", "model": "Tesla P100-PCIE-16GB", "mem_total": 16280}))
    dash._telemetry_queue.put(("acct0", {
        "gpu": 0, "util": 87, "mem_used": 6144, "mem_total": 16280,
        "temp": 71, "power": 58.0}))
    dash._live_tick()

    snap = dash.instance_store.get("acct0")
    assert snap is not None
    assert snap.cpu_count == 4
    assert snap.ram_total == 31.3
    assert snap.gpus == [GpuSnapshot(index=0, mem_total=16280,
                                     model="Tesla P100-PCIE-16GB")]
    dash.close()


def test_live_tick_matches_multiple_gpu_models_to_telemetry_indices_by_position(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path)
    dash._hardware_queue.put(("acct0", {
        "kind": "cpu_ram", "cpu_count": 4, "ram_total": 31.3}))
    dash._hardware_queue.put(("acct0", {
        "kind": "gpu", "model": "Tesla T4", "mem_total": 15360}))
    dash._hardware_queue.put(("acct0", {
        "kind": "gpu", "model": "Tesla T4", "mem_total": 15360}))
    dash._telemetry_queue.put(("acct0", {
        "gpu": 0, "util": 50, "mem_used": 100, "mem_total": 15360,
        "temp": 60, "power": 10.0}))
    dash._telemetry_queue.put(("acct0", {
        "gpu": 1, "util": 12, "mem_used": 80, "mem_total": 15360,
        "temp": 55, "power": 8.0}))
    dash._live_tick()

    snap = dash.instance_store.get("acct0")
    assert snap.gpus == [GpuSnapshot(index=0, mem_total=15360, model="Tesla T4"),
                         GpuSnapshot(index=1, mem_total=15360, model="Tesla T4")]
    dash.close()


def test_snapshot_still_records_with_none_hardware_fields_when_banner_never_arrives(qapp, tmp_path):
    """No hardware-banner lines this run (e.g. the stream dropped before
    the first cell finished) -- cpu_count/ram_total/model stay None rather
    than blocking the telemetry-driven snapshot entirely."""
    dash = make_dashboard(qapp, tmp_path)
    dash._telemetry_queue.put(("acct0", {
        "gpu": 0, "util": 50, "mem_used": 100, "mem_total": 200,
        "temp": 60, "power": 10.0}))
    dash._live_tick()

    snap = dash.instance_store.get("acct0")
    assert snap.cpu_count is None
    assert snap.ram_total is None
    assert snap.gpus == [GpuSnapshot(index=0, mem_total=200, model=None)]
    dash.close()


# ---------------- full screen: maximised default, F11, a way out ---------

def test_dashboard_loads_its_own_settings_when_none_given(qapp, tmp_path):
    """Every existing test in this module constructs Dashboard with no
    settings= -- this is the fallback the Task 6 brief's "persisted"
    requirement depends on staying test-compatible."""
    dash = make_dashboard(qapp, tmp_path, n=1)
    assert dash.settings.accent == "orange"
    assert dash.settings.fullscreen is False
    assert dash.settings.min_gpus == 1
    dash.close()


def test_launch_wires_settings_min_gpus_into_render_settings(
        qapp, tmp_path, monkeypatch):
    """Final review IMPORTANT 2: the desktop app's one production
    RenderSettings() construction (dashboard.py's _launch) must actually
    pass the user's configured minimum-GPU gate through, not silently
    leave every launch at RenderSettings' own default of 0 (no gate at
    all, which is what made the PREFLIGHT SystemExit gate unreachable)."""
    from blendfleet.fleet import Fleet

    captured = {}
    original_launch = Fleet.launch

    def spy_launch(self, blend, settings, *args, **kwargs):
        captured["settings"] = settings
        return original_launch(self, blend, settings, *args, **kwargs)

    monkeypatch.setattr(Fleet, "launch", spy_launch)

    settings = Settings(min_gpus=3)
    dash = Dashboard(make_store(1), lambda accounts: Fleet(
        accounts, lambda tok: FakeClient(tok), tmp_path / "w"),
        verifier=lambda t: "someone", settings=settings)
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)

    blend = tmp_path / "remember.blend"
    blend.write_bytes(b"x" * 100)
    dash.blend = blend
    dash.start.setValue(1)
    dash.end.setValue(4)
    dash._launch()
    pump(dash._launch_worker)

    assert captured["settings"].min_gpus == 3
    dash.close()


def test_toggle_fullscreen_enters_and_persists(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path, n=1)
    assert dash.isFullScreen() is False
    assert dash.exit_fullscreen_btn.isVisible() is False

    dash._toggle_fullscreen()
    wait_until(lambda: dash.isFullScreen())
    wait_until(lambda: dash.exit_fullscreen_btn.isVisible())
    assert dash.settings.fullscreen is True

    reloaded = Settings.load()
    assert reloaded.fullscreen is True
    dash.close()


def test_toggle_fullscreen_again_leaves_full_screen_and_hides_the_exit_button(
        qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path, n=1)
    dash._toggle_fullscreen()
    wait_until(lambda: dash.isFullScreen())
    dash._toggle_fullscreen()
    wait_until(lambda: not dash.isFullScreen())
    wait_until(lambda: not dash.exit_fullscreen_btn.isVisible())
    assert dash.settings.fullscreen is False
    dash.close()


def test_escape_exits_full_screen(qapp, tmp_path):
    """The exit_fullscreen_btn is the primary visible way out, but Esc is
    the other conventional one -- never zero ways back out of a real
    full-screen toggle."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    dash = make_dashboard(qapp, tmp_path, n=1)
    dash._toggle_fullscreen()
    wait_until(lambda: dash.isFullScreen())

    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                      Qt.KeyboardModifier.NoModifier)
    dash.keyPressEvent(event)
    wait_until(lambda: not dash.isFullScreen())
    dash.close()


def test_escape_does_nothing_when_not_full_screen(qapp, tmp_path):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    dash = make_dashboard(qapp, tmp_path, n=1)
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                      Qt.KeyboardModifier.NoModifier)
    dash.keyPressEvent(event)   # must not raise, must not enter full screen
    assert dash.isFullScreen() is False
    dash.close()


def test_show_at_startup_maximises_by_default(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path, n=1)
    dash.show_at_startup()
    wait_until(lambda: dash.isMaximized())
    assert dash.isFullScreen() is False
    dash.close()


def test_show_at_startup_honours_a_stored_fullscreen_preference(qapp, tmp_path):
    settings = Settings(fullscreen=True)
    dash = Dashboard(make_store(1), lambda accounts: Fleet(
        accounts, lambda tok: FakeClient(tok), tmp_path / "w"),
        verifier=lambda t: "someone", settings=settings)
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash.show_at_startup()
    wait_until(lambda: dash.isFullScreen())
    wait_until(lambda: dash.exit_fullscreen_btn.isVisible())
    dash.close()


# ------------- content column width (Task 6 visual-pass fix) -------------
# The bug: outer.addWidget(content, 0) in _build_main handed 100% of
# surplus width to the two flanking addStretch(1) spacers regardless of
# content's maximumWidth, so content sat at a MEASURED constant ~453px
# whether the window was 1920 or 2560px wide -- a prior report claimed
# this layout worked without ever querying a width, and a reviewer
# disproved it in one measurement. This test measures, it does not
# describe: it is the thing that would have caught the original bug and
# must keep catching any regression back to it.

def test_content_column_grows_with_window_but_never_exceeds_the_cap(
        qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path, n=1)
    dash.show()
    widths = {}
    for w in (1280, 1920, 2560):
        dash.resize(w, 900)
        for _ in range(30):
            QCoreApplication.processEvents()
        widths[w] = dash.content_column.width()
    dash.close()

    assert widths[1280] < widths[2560], (
        "content_column must grow as the window widens from 1280 to "
        f"2560px -- measured widths: {widths}. A constant width here is "
        "exactly the stretch-factor-0 bug this test guards against.")
    assert widths[1920] <= dashboard_mod.MAX_CONTENT_WIDTH
    assert widths[2560] <= dashboard_mod.MAX_CONTENT_WIDTH, (
        f"content_column exceeded MAX_CONTENT_WIDTH "
        f"({dashboard_mod.MAX_CONTENT_WIDTH}px) at 2560px window width: "
        f"{widths[2560]}px")


# ---------------- navigation shell ----------------

def test_every_sidebar_destination_has_a_page(qapp, tmp_path):
    """Sidebar.PAGES and Dashboard._pages must agree exactly: a nav item
    with no page behind it is a dead button, and a page with no nav item is
    unreachable."""
    dash = make_dashboard(qapp, tmp_path, n=1)
    assert set(dash._pages) == {key for key, _, _ in Sidebar.PAGES}
    assert set(dash.sidebar.buttons) == set(dash._pages)
    assert set(dashboard_mod.PAGE_TITLES) == set(dash._pages)
    dash.close()


@pytest.mark.parametrize("page", ["files", "instances", "logs", "settings",
                                  "dashboard"])
def test_clicking_a_nav_item_switches_page_and_title(qapp, tmp_path, page):
    dash = make_dashboard(qapp, tmp_path, n=1)
    dash.sidebar.buttons[page].click()
    assert dash.current_page == page
    assert dash.pages.currentWidget() is dash._pages[page]
    assert dash.page_title.text() == dashboard_mod.PAGE_TITLES[page]
    # Exactly one nav item is ever marked active.
    active = [k for k, b in dash.sidebar.buttons.items() if b.isChecked()]
    assert active == [page]
    dash.close()


def test_dashboard_opens_on_the_dashboard_page(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path, n=1)
    assert dash.current_page == "dashboard"
    assert dash.pages.currentWidget() is dash._pages["dashboard"]
    dash.close()


def test_nav_counts_report_accounts_and_project(qapp, tmp_path):
    """The pills exist so you do not have to change page to find out
    whether anything is there. Settings has nothing countable and must show
    no pill at all rather than a permanent 0."""
    dash = make_dashboard(qapp, tmp_path, n=3)
    assert dash.sidebar.buttons["instances"].pill.text() == "3"
    assert dash.sidebar.buttons["files"].pill.text() == "0"
    dash.blend = tmp_path / "scene.blend"
    dash._refresh_views()
    assert dash.sidebar.buttons["files"].pill.text() == "1"
    assert dash.sidebar.buttons["settings"].pill.text() == ""
    dash.close()


def test_poll_status_banner_lives_outside_the_page_stack(qapp, tmp_path):
    """"Kaggle is unreachable" is true on every page, so it must not be
    parented into any single one of them."""
    dash = make_dashboard(qapp, tmp_path, n=1)
    ancestors = set()
    w = dash.poll_status_label.parentWidget()
    while w is not None:
        ancestors.add(w)
        w = w.parentWidget()
    assert not (ancestors & set(dash._pages.values()))
    dash.close()


# ---------------- settings, now a page rather than a modal ----------------

def test_settings_nav_item_switches_to_the_settings_page(qapp, tmp_path):
    """Settings used to be a modal opened from a gear in the rail. It is a
    page now, showing the same SettingsPanel -- an accent picker that covers
    up the thing whose colour it changes is the wrong shape for the job."""
    dash = make_dashboard(qapp, tmp_path, n=1)
    dash.settings_btn.click()
    assert dash.current_page == "settings"
    assert dash.pages.currentWidget() is dash._pages["settings"]
    assert dash.settings_panel.settings is dash.settings
    dash.close()


def test_open_settings_shows_the_settings_page(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path, n=1)
    dash._open_settings()
    assert dash.current_page == "settings"
    dash.close()


# ---------------- accent: live, without a restart (Task 6) ----------------
# THE bug the Task 6 brief calls out by name: rendering with the red accent
# selected used to produce zero red pixels anywhere, because dashboard.py
# captured `ACCENT` at ITS OWN import time (`from ... import ACCENT`) and
# never looked at it again. This proves the fix end-to-end through
# Dashboard's real brand-mark pixmap, not just a stylesheet string.

def _image_has_color(image, hex_color: str) -> bool:
    from PySide6.QtGui import QColor
    target = QColor(hex_color)
    for y in range(image.height()):
        for x in range(image.width()):
            px = image.pixelColor(x, y)
            if px.alpha() > 0 and (px.red(), px.green(), px.blue()) == \
                    (target.red(), target.green(), target.blue()):
                return True
    return False


@pytest.mark.parametrize("name", ["orange", "green", "purple", "blue", "red"])
def test_switching_accent_live_repaints_the_brand_mark_without_restart(
        qapp, tmp_path, name):
    dash = make_dashboard(qapp, tmp_path, n=1)
    theme.apply(qapp, name)   # e.g. what a settings swatch click does
    # The brand mark moved into the navigation sidebar, which now owns its
    # own theme_signal connection and repaints itself -- the requirement is
    # unchanged, only its address is.
    image = dash.sidebar.brand_mark.pixmap().toImage()
    assert _image_has_color(image, ACCENTS[name].base), (
        f"the {name!r} accent does not appear in the brand mark after a "
        "live switch -- the sidebar must repaint chrome it painted with an "
        "explicit accent colour, not just leave it as it was at __init__")
    dash.close()


def test_switching_accent_live_also_repaints_every_instance_card(
        qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path, n=1)
    dash._last_state = FleetState(
        job_id="job", blend_name="x.blend", start_frame=1, end_frame=2,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/k0", frames=[1, 2],
                             state="running")])
    dash._refresh_views()

    theme.apply(qapp, "red")
    image = dash._instance_cards["acct0"].status_icon.pixmap().toImage()
    assert _image_has_color(image, ACCENTS["red"].base)
    dash.close()


def test_closing_many_dashboards_does_not_leak_theme_signal_connections(
        qapp, tmp_path):
    """Regression test for the signal-lifetime leak fixed in Task 6.

    theme.theme_signal is a process-global QObject that outlives every
    Dashboard/InstanceCard connected to it. closeEvent used to try
    `disconnect(bound_method)` wrapped in `except (RuntimeError,
    TypeError)`, on the theory that a redundant disconnect "just emits a
    RuntimeWarning ... not an error worth stopping for" -- but PySide6's
    disconnect() does not raise on a redundant disconnect, so the except
    clause never fired and never could, and every leftover connection
    made theme.apply()/theme_signal.changed.emit() a little slower for
    every later accent change (measured: 94 warnings and a 55s ->
    expected ~10s tests/test_dashboard.py runtime before this fix).

    A count of "Failed to disconnect" warnings would pass again the
    moment PySide6 changes that message. What actually matters, and what
    this asserts, is the property the warning was only a symptom of: the
    number of live receivers on theme_signal.changed must return to its
    starting point once every Dashboard/InstanceCard this test opened has
    been closed and torn down -- not grow with each one, the way it would
    if disconnecting were unreliable.
    """
    baseline = theme.theme_signal.receivers(SIGNAL("changed()"))
    for _ in range(5):
        dash = make_dashboard(qapp, tmp_path, n=3)
        dash.close()
        settle(dash)
        dash.deleteLater()
    for _ in range(20):
        QCoreApplication.processEvents()

    after = theme.theme_signal.receivers(SIGNAL("changed()"))
    assert after == baseline, (
        f"theme_signal.changed had {baseline} receiver(s) before this test "
        f"opened and closed 5 Dashboards (3 InstanceCards each) and "
        f"{after} after -- closing a Dashboard must remove its own and "
        "every one of its cards' connections, not leave them for Qt's "
        "eventual (and here, unreliable) auto-disconnect-on-destroy.")


def test_the_filmstrip_tells_the_user_its_completed_cells_are_approximate(qapp, tmp_path):
    """charts.frame_done can only approximate which frames are done (the
    notebook reports a count of successes, not a list), so the UI has to
    say so -- a caveat that lives only in a docstring is invisible to the
    person reading a green cell as proof the frame exists."""
    dash = make_dashboard(qapp, tmp_path)
    labels = [w.text() for w in dash.findChildren(dashboard_mod.QLabel)]
    # The caveat is its own label under the "Filmstrip" section header, so
    # match on the caveat itself rather than on the section title.
    caveats = [t for t in labels if "approximate" in t.lower()]
    assert caveats, "the filmstrip's approximate-cells caveat went missing"
    caveat = caveats[0].lower()
    assert "failed frame" in caveat, \
        "say WHY it is approximate, not just that it is"
    assert "collect frames" in caveat, \
        "point at the authoritative list, not just at the problem"


# ---------------------------------------------------------------------------
# Task 3: per-instance cancel, end to end through Dashboard._cancel_instance.
# A real Fleet.launch() is used (not just dash._last_state) so
# Fleet.cancel_worker -- built fresh inside the worker closure -- has an
# actual job on disk to load and cancel.
# ---------------------------------------------------------------------------

def _seed_job(tmp_path, make_client, n=2):
    """Launch a real n-account job through a real Fleet and poll it once,
    so a freshly constructed Dashboard's fleet_factory(...) has real,
    on-disk state to load/cancel/poll further -- not merely an in-memory
    dash._last_state.

    `make_client(token, account)` builds each account's fake client, given
    the ACCOUNT too (not just the token) so a test can assign a fixed
    state per account deterministically, by label -- never by call order
    or call count: every action below (launch, poll, and later each
    cancel_worker/poll call from Dashboard) calls the factory again for
    its own fresh Fleet/client, so anything order-dependent breaks the
    moment more than one action runs.

    Returns (store, factory, fleet_state).
    """
    store = make_store(n)
    by_token = {a.token: a for a in store.list()}

    def factory(tok):
        return make_client(tok, by_token[tok])

    blend = tmp_path / "remember.blend"
    blend.write_bytes(b"x" * 100)
    seed = Fleet(store.list(), factory, tmp_path / "w")
    seed.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    st = seed.poll()  # each client's own .status() decides its worker's state
    return store, factory, st


def test_cancel_button_visible_for_both_queued_and_running(qapp, tmp_path):
    """Review fix (Task 3 spec defect): a queued kernel already holds a
    GPU session slot and Fleet.cancel_worker() cancels it correctly, so
    the per-instance Cancel button must be offered for queued workers
    too, not only running ones."""
    clients = {}
    def make_client(tok, acct):
        state = "running" if acct.label == "acct0" else "queued"
        clients[tok] = FakeClient(tok, state)
        return clients[tok]

    store, factory, st = _seed_job(tmp_path, make_client)

    def fleet_factory(accounts):
        return Fleet(accounts, factory, tmp_path / "w")

    dash = Dashboard(store, fleet_factory, verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    running_worker = next(w for w in st.workers if w.state == "running")
    queued_worker = next(w for w in st.workers if w.state == "queued")
    assert dash._instance_cards[running_worker.label].cancel_btn.isHidden() is False
    assert dash._instance_cards[queued_worker.label].cancel_btn.isHidden() is False
    dash.close()


def test_cancel_instance_confirms_and_cancels_only_that_account(
        qapp, tmp_path, stub_message_boxes):
    clients = {}
    def make_client(tok, acct):
        clients[tok] = FakeClient(tok)
        return clients[tok]

    store, factory, st = _seed_job(tmp_path, make_client)

    def fleet_factory(accounts):
        return Fleet(accounts, factory, tmp_path / "w")

    dash = Dashboard(store, fleet_factory, verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    target = st.workers[0]
    other = st.workers[1]

    dash._cancel_instance(target.label)
    worker = dash._instance_cancel_workers.get(target.label)
    pump(worker)
    settle(dash)

    target_token = next(a.token for a in store.list() if a.label == target.label)
    other_token = next(a.token for a in store.list() if a.label == other.label)
    assert clients[target_token].cancelled == [target.kernel_slug]
    # the whole point: the OTHER account's cancel() must never be called.
    assert clients[other_token].cancelled == []
    assert stub_message_boxes["information"], "no confirmation dialog was shown"
    dash.close()


def test_cancel_instance_declined_confirmation_cancels_nothing(
        qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **kw: QMessageBox.StandardButton.No)

    clients = {}
    def make_client(tok, acct):
        clients[tok] = FakeClient(tok)
        return clients[tok]

    store, factory, st = _seed_job(tmp_path, make_client)

    def fleet_factory(accounts):
        return Fleet(accounts, factory, tmp_path / "w")

    dash = Dashboard(store, fleet_factory, verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    dash._cancel_instance(st.workers[0].label)

    assert dash._instance_cancel_workers == {}
    assert all(c.cancelled == [] for c in clients.values())
    dash.close()


def test_cancel_instance_disables_button_while_in_flight_and_reenables_on_success(
        qapp, tmp_path, stub_message_boxes):
    clients = {}
    def make_client(tok, acct):
        clients[tok] = FakeClient(tok)
        return clients[tok]

    store, factory, st = _seed_job(tmp_path, make_client, n=1)

    def fleet_factory(accounts):
        return Fleet(accounts, factory, tmp_path / "w")

    dash = Dashboard(store, fleet_factory, verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    label = st.workers[0].label
    card = dash._instance_cards[label]

    dash._cancel_instance(label)
    # Disabling happens synchronously, before the worker thread is even
    # scheduled -- must already be true the instant _cancel_instance returns.
    assert card.cancel_btn.isEnabled() is False
    assert card.cancel_btn.text() == "Cancelling…"

    pump(dash._instance_cancel_workers[label])
    settle(dash)

    assert card.cancel_btn.isEnabled() is True
    assert card.cancel_btn.text() == "Cancel"
    dash.close()


def test_cancel_instance_failure_warns_and_reenables_the_button(
        qapp, tmp_path, stub_message_boxes):
    def make_client(tok, acct):
        return RefusingCancelClient(tok)

    store, factory, st = _seed_job(tmp_path, make_client, n=1)

    def fleet_factory(accounts):
        return Fleet(accounts, factory, tmp_path / "w")

    dash = Dashboard(store, fleet_factory, verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    label = st.workers[0].label
    card = dash._instance_cards[label]

    dash._cancel_instance(label)
    pump(dash._instance_cancel_workers[label])
    settle(dash)

    assert card.cancel_btn.isEnabled() is True
    assert stub_message_boxes["warning"], \
        "a failed cancel must say so, never look like a silent success"
    dash.close()


def test_cancel_instance_on_an_already_finished_worker_says_nothing_to_cancel(
        qapp, tmp_path, stub_message_boxes):
    """Simulates the race the brief describes: the button fired while the
    card still thought the worker was running, but by the time the actual
    network call happens the job has already finished."""
    def make_client(tok, acct):
        return FakeClient(tok, "complete")

    store, factory, st = _seed_job(tmp_path, make_client, n=1)

    def fleet_factory(accounts):
        return Fleet(accounts, factory, tmp_path / "w")

    dash = Dashboard(store, fleet_factory, verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)

    dash._cancel_instance(st.workers[0].label)
    pump(dash._instance_cancel_workers[st.workers[0].label])
    settle(dash)

    titles = [title for title, _ in stub_message_boxes["information"]] or         [e.text() for e in dash.event_log._entries]
    assert any("Nothing to cancel" in t for t in titles)
    dash.close()


# ---------------------------------------------------------------------------
# Task 6: per-instance download (with progress), and the fleet-wide
# "Collect frames..." button rewired onto the same progress-reporting path.
# ---------------------------------------------------------------------------

class DownloadingClient(FakeClient):
    """fetch_output writes fake frame files into `dest`; fetch_output_with_
    progress reports one DownloadProgress tick first -- so Task 6's
    download-progress wiring can be exercised end to end with zero
    network."""

    def __init__(self, token, state="complete", frame_names=("f_0001.png",)):
        super().__init__(token, state)
        self.frame_names = frame_names

    def fetch_output(self, slug, dest):
        dest.mkdir(parents=True, exist_ok=True)
        out = []
        for name in self.frame_names:
            p = dest / name
            p.write_bytes(b"PNG")
            out.append(p)
        return out

    def fetch_output_with_progress(self, slug, dest, on_progress=None):
        if on_progress is not None:
            from blendfleet.downloader import DownloadProgress
            on_progress(DownloadProgress(downloaded=3, total=3, rate_bps=1.0))
        return self.fetch_output(slug, dest)


class BoomFetchClient(FakeClient):
    def fetch_output(self, slug, dest):
        raise RuntimeError("network died mid-download")

    def fetch_output_with_progress(self, slug, dest, on_progress=None):
        raise RuntimeError("network died mid-download")


def test_download_instance_downloads_only_that_worker(
        qapp, tmp_path, monkeypatch, stub_message_boxes):
    clients = {}

    def make_client(tok, acct):
        clients[tok] = DownloadingClient(
            tok, frame_names=(f"f_000{acct.label[-1]}.png",))
        return clients[tok]

    store, factory, st = _seed_job(tmp_path, make_client, n=2)
    dest = tmp_path / "downloaded"
    monkeypatch.setattr(dashboard_mod.QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: str(dest))

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    target = st.workers[0]
    dash._download_instance(target.label)
    pump(dash._instance_download_workers[target.label])
    settle(dash)

    assert dest.exists()
    assert dash.event_log._entries, "the collect result was never reported"
    dash.close()


def test_download_instance_shows_progress_on_that_cards_own_line(
        qapp, tmp_path, monkeypatch, stub_message_boxes):
    def make_client(tok, acct):
        return DownloadingClient(tok)

    store, factory, st = _seed_job(tmp_path, make_client, n=1)
    monkeypatch.setattr(dashboard_mod.QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: str(tmp_path / "downloaded"))

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    label = st.workers[0].label
    card = dash._instance_cards[label]
    seen_progress = []
    card.set_download_progress = (lambda p, orig=card.set_download_progress:
                                  (seen_progress.append(p), orig(p)))

    dash._download_instance(label)
    pump(dash._instance_download_workers[label])
    settle(dash)

    assert any(p is not None for p in seen_progress), \
        "the card's own progress line was never updated"
    # cleared again once the download finishes
    assert card.download_progress_label.isHidden() is True
    dash.close()


def test_download_instance_disables_button_while_in_flight_and_reenables(
        qapp, tmp_path, monkeypatch, stub_message_boxes):
    def make_client(tok, acct):
        return DownloadingClient(tok)

    store, factory, st = _seed_job(tmp_path, make_client, n=1)
    monkeypatch.setattr(dashboard_mod.QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: str(tmp_path / "downloaded"))

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    label = st.workers[0].label
    card = dash._instance_cards[label]

    dash._download_instance(label)
    assert card.download_btn.isEnabled() is False
    assert card.download_btn.text() == "Downloading…"

    pump(dash._instance_download_workers[label])
    settle(dash)

    assert card.download_btn.isEnabled() is True
    assert card.download_btn.text() == "Download"
    dash.close()


def test_download_instance_no_folder_chosen_does_nothing(
        qapp, tmp_path, monkeypatch):
    def make_client(tok, acct):
        return DownloadingClient(tok)

    store, factory, st = _seed_job(tmp_path, make_client, n=1)
    monkeypatch.setattr(dashboard_mod.QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: "")   # user cancelled the dialog

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    dash._download_instance(st.workers[0].label)
    assert dash._instance_download_workers == {}
    dash.close()


def test_download_instance_failure_on_one_worker_leaves_the_button_usable_again(
        qapp, tmp_path, monkeypatch, stub_message_boxes):
    def make_client(tok, acct):
        return BoomFetchClient(tok)

    store, factory, st = _seed_job(tmp_path, make_client, n=1)
    monkeypatch.setattr(dashboard_mod.QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: str(tmp_path / "downloaded"))

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    label = st.workers[0].label
    card = dash._instance_cards[label]

    dash._download_instance(label)
    # A worker whose fetch never succeeds now takes ~3s to give up:
    # collect() retries a dropped download before reporting one, because a
    # transient socket used to lose a whole finished render (see
    # tests/test_transient_failures.py). The UI behaviour asserted below is
    # unchanged -- only the wait is.
    pump(dash._instance_download_workers[label], timeout=15000)
    settle(dash)

    # collect() itself never raises on a fetch failure (Task 6: one
    # worker's failure must not abort the others) -- it comes back as a
    # CollectReport whose worker_errors names this account, so the button
    # must still re-enable exactly as on a clean success.
    assert card.download_btn.isEnabled() is True
    assert card.download_btn.text() == "Download"
    assert stub_message_boxes["warning"] or stub_message_boxes["information"]
    dash.close()


def test_collect_fleet_wide_downloads_every_worker_and_routes_progress_per_card(
        qapp, tmp_path, monkeypatch, stub_message_boxes):
    clients = {}

    def make_client(tok, acct):
        clients[tok] = DownloadingClient(
            tok, frame_names=(f"f_000{acct.label[-1]}.png",))
        return clients[tok]

    store, factory, st = _seed_job(tmp_path, make_client, n=2)
    dest = tmp_path / "downloaded"
    monkeypatch.setattr(dashboard_mod.QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: str(dest))

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    dash._collect()
    pump(dash._collect_worker)
    settle(dash)

    assert dest.exists()
    for w in st.workers:
        assert dash._instance_cards[w.label].download_progress_label.isHidden() is True
    # A clean collect reports through a toast and the fleet log rather than
    # a modal the user has to dismiss to carry on watching the render it
    # just finished. It must still REPORT -- silence would be worse than a
    # dialog.
    # Reported three ways by Dashboard.notify: a toast now, a fleet-log
    # line, and a notification-panel entry for anyone who was not looking.
    assert any("Collected" in e.text() for e in dash.event_log._entries), \
        "the collect result never reached the fleet log"
    assert dash.notif_panel.unread == 1
    dash.close()


def test_collect_fleet_wide_with_a_worker_error_warns_not_informs(
        qapp, tmp_path, monkeypatch, stub_message_boxes):
    """Final review Minor: a CollectReport.worker_errors used to surface
    under the same success-toned "Frames collected" information dialog on
    the fleet-wide path, while the per-instance path
    (_show_download_instance_result) correctly warns for the identical
    condition. The two must agree -- errors warn, on both paths."""
    def make_client(tok, acct):
        if acct.label == "acct0":
            return BoomFetchClient(tok)
        return DownloadingClient(tok)

    store, factory, st = _seed_job(tmp_path, make_client, n=2)
    monkeypatch.setattr(dashboard_mod.QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: str(tmp_path / "downloaded"))

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    dash._collect()
    # acct0's fetch never succeeds, and collect() now retries a dropped
    # download before reporting one -- ~3s, not instant. See the note in
    # the per-instance failure test above.
    pump(dash._collect_worker, timeout=15000)
    settle(dash)

    assert stub_message_boxes["warning"], \
        "a worker_errors report must warn, never look like a clean collect"
    titles = [title for title, _ in stub_message_boxes["information"]] or         [e.text() for e in dash.event_log._entries]
    assert "Frames collected" not in titles
    dash.close()


def test_collect_fleet_wide_message_names_the_actual_zip(
        qapp, tmp_path, monkeypatch, stub_message_boxes):
    """This dialog is the ONLY place the app says where a render's output
    went. collect() now leaves exactly one file -- <scene>.zip -- in the
    folder the user picked, and picks the final name itself (it will not
    overwrite one already there), so the dialog must quote the path off
    the report rather than reconstruct it."""
    def make_client(tok, acct):
        if acct.label == "acct0":
            return BoomFetchClient(tok)
        return DownloadingClient(tok)

    store, factory, st = _seed_job(tmp_path, make_client, n=2)
    dest = tmp_path / "downloaded"
    monkeypatch.setattr(dashboard_mod.QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: str(dest))

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    dash._collect()
    pump(dash._collect_worker, timeout=15000)
    settle(dash)

    assert stub_message_boxes["warning"], "expected a worker_errors warning"
    _, message = stub_message_boxes["warning"][-1]
    # _seed_job launches "remember.blend" -- collect()'s own scene_key.
    expected = str(dest / "remember.zip")
    assert expected in message, (
        f"the dialog must name the zip that was actually written: {message!r}")
    assert (dest / "remember.zip").is_file()
    assert [p.name for p in dest.iterdir()] == ["remember.zip"], (
        "the destination must be left holding the zip and nothing else")
    dash.close()


# ---------------------------------------------------------------------------
# Review findings on the Task 6 download wiring above.
# ---------------------------------------------------------------------------

class _CollectReportStub:
    """The fields _describe_collect_result reads."""

    def __init__(self, copied=0, missing_frames=(), archive_errors=(),
                 worker_errors=None, archive_path=None, wanted_name=""):
        self.copied = copied
        self.missing_frames = list(missing_frames)
        self.archive_errors = list(archive_errors)
        self.worker_errors = worker_errors or {}
        self.archive_path = archive_path
        self.wanted_name = wanted_name


def test_collect_result_message_never_doubles_the_word_to(
        qapp, tmp_path, monkeypatch, stub_message_boxes):
    """Review finding: _describe_collect_result's own template already
    ends "... {who} to {dest}.", but the fleet-wide call site passed
    who="to" and the per-instance one passed who=f"from {who} to" --
    both produced a doubled "to to" in the user-facing dialog."""
    def make_client(tok, acct):
        return DownloadingClient(tok)

    store, factory, st = _seed_job(tmp_path, make_client, n=1)
    monkeypatch.setattr(dashboard_mod.QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: str(tmp_path / "downloaded"))

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    # A successful collect reports through a toast and the fleet log now,
    # not a modal -- so the wording is asserted on the builder both paths
    # share, which is where the doubled "to to" actually came from.
    report = _CollectReportStub(copied=2, archive_path=tmp_path / "remember.zip")
    fleet_message = dash._describe_collect_result(
        report, source_phrase="", folder=str(tmp_path))
    assert "to to" not in fleet_message, fleet_message
    assert str(tmp_path / "remember.zip") in fleet_message, fleet_message
    instance_message = dash._describe_collect_result(
        report, source_phrase=" from acct0", folder=str(tmp_path))
    assert "to to" not in instance_message, instance_message
    assert "from acct0" in instance_message, instance_message
    assert "  " not in instance_message, instance_message

    # Nothing collected: no zip was written, so the message must not name
    # one -- it says so, and points at the folder it did not write to.
    nothing = dash._describe_collect_result(
        _CollectReportStub(copied=0), source_phrase="", folder=str(tmp_path))
    assert "no zip was written" in nothing, nothing
    assert ".zip\n" not in nothing, nothing

    dash._collect()
    pump(dash._collect_worker)
    settle(dash)
    dash.close()


def test_fleet_wide_collect_finishing_does_not_clobber_a_concurrent_instance_download(
        qapp, tmp_path, monkeypatch, stub_message_boxes):
    """Review finding: the fleet-wide "Collect frames..." button and a
    per-card "Download" are not mutually exclusive. When the fleet-wide
    one finishes, it used to blank EVERY card's progress line -- including
    one whose own, unrelated per-instance download was still running."""
    from blendfleet.downloader import DownloadProgress

    def make_client(tok, acct):
        return DownloadingClient(tok)

    store, factory, st = _seed_job(tmp_path, make_client, n=2)
    monkeypatch.setattr(dashboard_mod.QFileDialog, "getExistingDirectory",
                        lambda *a, **kw: str(tmp_path / "downloaded"))

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)
    dash._last_state = st
    dash._refresh_views()

    other_label = st.workers[1].label
    other_card = dash._instance_cards[other_label]
    other_card.set_download_progress(DownloadProgress(downloaded=1, total=2, rate_bps=1.0))
    # Simulate other_label's own per-instance download still being in
    # flight -- a plain marker is enough since _clear_all_download_progress
    # only needs to consult the KEYS of this dict, never call into it.
    dash._instance_download_workers[other_label] = object()

    dash._collect()
    pump(dash._collect_worker)

    assert other_card.download_progress_label.isHidden() is False, (
        "the fleet-wide collect finishing must not blank a card whose OWN "
        "per-instance download is still running")
    # Drop the placeholder before settle()/close() try to .wait() it as if
    # it were a real _DownloadWorker.
    dash._instance_download_workers.pop(other_label, None)
    dash.close()


# ---------------------------------------------------------------------------
# Task 4: "it just says error" -- surfacing failure_message, or the fetched
# log tail when it is empty, and ONLY for a failed worker.
# ---------------------------------------------------------------------------

# Both tests below count only the FAILURE-log fetch. poll_all() also reads a
# terminal worker's log tail exactly once, for its final frame count
# (Fleet._final_frame_count), and that call is told apart by its destination
# -- "finallog_<label>" versus fetch_failure_log()'s "log_<label>". These
# tests are about the dashboard never going to the network to explain a
# failure Kaggle already explained, which is unchanged.
def _is_failure_log(dest) -> bool:
    return "finallog" not in str(dest)


def test_poll_shows_failure_message_without_fetching_a_log_when_present(
        qapp, tmp_path):
    fetch_calls: list[str] = []

    def make_client(tok, acct):
        class TrackedClient(FakeClient):
            def fetch_log_tail(self, slug, dest, max_lines=200):
                if _is_failure_log(dest):
                    fetch_calls.append(slug)
                return "should never be requested"
        return TrackedClient(
            tok, "error", message="CUDA out of memory: tried to allocate 2GB")

    store, factory, st = _seed_job(tmp_path, make_client, n=1)
    label = st.workers[0].label

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)

    dash._poll()
    pump(dash._poll_worker)
    settle(dash)

    assert fetch_calls == [], \
        "failure_message was already present -- the log must never be fetched"
    assert dash._log_fetch_workers == {}
    card = dash._instance_cards[label]
    assert card.failure_label.isHidden() is False
    assert "memory" in card._failure_detail.lower()
    dash.close()


def test_poll_fetches_the_log_once_for_a_failure_with_no_message(qapp, tmp_path):
    fetch_calls: list[str] = []

    def make_client(tok, acct):
        class TrackedClient(FakeClient):
            def fetch_log_tail(self, slug, dest, max_lines=200):
                if _is_failure_log(dest):
                    fetch_calls.append(slug)
                return "Fatal Python error: Segmentation fault"
        return TrackedClient(tok, "error")

    store, factory, st = _seed_job(tmp_path, make_client, n=1)
    label = st.workers[0].label

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)

    dash._poll()
    pump(dash._poll_worker)
    settle(dash)   # also waits out the log-fetch worker _maybe_fetch_... started

    assert len(fetch_calls) == 1
    card = dash._instance_cards[label]
    assert card.failure_label.isHidden() is False
    assert "crashed" in card._failure_detail.lower() or \
        "Blender crashed" in card.failure_label.text()

    # A second poll tick, worker still "error" -- must NOT re-fetch.
    dash._poll()
    pump(dash._poll_worker)
    settle(dash)
    assert len(fetch_calls) == 1, "once per failure, not once per poll tick"
    dash.close()


def test_poll_never_fetches_a_log_for_a_healthy_worker(qapp, tmp_path):
    fetch_calls: list[str] = []

    def make_client(tok, acct):
        class TrackedClient(FakeClient):
            def fetch_log_tail(self, slug, dest, max_lines=200):
                fetch_calls.append(slug)
                return "should never be requested"
        return TrackedClient(tok, "running")

    store, factory, st = _seed_job(tmp_path, make_client, n=1)
    label = st.workers[0].label

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)

    dash._poll()
    pump(dash._poll_worker)
    settle(dash)

    assert fetch_calls == []
    assert dash._log_fetch_workers == {}
    card = dash._instance_cards[label]
    # failure_row, not failure_label: the row carries the alert icon
    # alongside the text and is what set_failure shows and hides.
    assert card.failure_row.isHidden() is True
    dash.close()


def test_failure_cache_is_cleared_when_a_new_render_starts(qapp, tmp_path):
    def make_client(tok, acct):
        class TrackedClient(FakeClient):
            def fetch_log_tail(self, slug, dest, max_lines=200):
                return "Fatal Python error: Segmentation fault"
        return TrackedClient(tok, "error")

    store, factory, st = _seed_job(tmp_path, make_client, n=1)
    label = st.workers[0].label

    dash = Dashboard(store, lambda accounts: Fleet(accounts, factory, tmp_path / "w"),
                     verifier=lambda t: "someone")
    _LIVE_DASHBOARDS.append(dash)
    settle(dash)

    dash._poll()
    pump(dash._poll_worker)
    settle(dash)
    assert dash._failure_logs.get(label)

    dash._start_progress_threads(FleetState("job2", "x.blend", 1, 1, []))
    assert label not in dash._failure_logs
    assert dash._instance_cards[label].failure_label.text() == ""
    dash.close()
