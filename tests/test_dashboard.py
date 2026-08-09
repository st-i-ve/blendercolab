import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

import blendfleet.platform_paths as pp
import blendfleet.ui.dashboard as dashboard_mod
from blendfleet.accounts import Account, AccountStore
from blendfleet.fleet import Fleet, FleetState, WorkerState
from blendfleet.instance_state import GpuSnapshot, InstanceSnapshot, InstanceStore
from blendfleet.kaggle_client import KaggleError, KernelStatus, Quota
from blendfleet.notebook_builder import RenderSettings
from blendfleet.ui.dashboard import Dashboard


@pytest.fixture(autouse=True)
def tmp_cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


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
    for _ in range(20):
        QCoreApplication.processEvents()


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
    for worker in (dash._poll_worker, dash._quota_worker,
                   dash._cancel_worker, dash._collect_worker):
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


def stub_message_boxes(monkeypatch):
    calls = {"warning": [], "critical": [], "information": []}
    for kind in calls:
        monkeypatch.setattr(
            f"blendfleet.ui.dashboard.QMessageBox.{kind}",
            lambda parent, title, message, k=kind: calls[k].append((title, message)))
    monkeypatch.setattr(
        "blendfleet.ui.dashboard.QMessageBox.question",
        lambda *a, **kw: dashboard_mod.QMessageBox.StandardButton.Yes)
    return calls


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

    def __init__(self, token, state="running"):
        self.token = token
        self.state = state
        self.sdk = FakeSdk()
        self._sdk_factory = lambda tok: self.sdk

    def whoami(self):
        return "user_" + self.token[-1]

    def dataset_exists(self, slug):
        return False

    def dataset_reachable(self, slug):
        return True

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

    def dataset_version(self, folder, message, on_progress=None):
        self.dataset_create(folder, on_progress=on_progress)

    def push_kernel(self, folder):
        pass

    def status(self, slug):
        return KernelStatus(state=self.state)

    def cancel(self, slug):
        return True

    def quota(self):
        return Quota(0, 21600, "2026-08-01", source="api")

    def fetch_output(self, slug, dest):
        return []


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
    assert dash.rail_rows_layout.count() == 3
    assert dash.poll_status_label.text() == ""
    dash.close()


def test_rail_shows_one_row_per_account(qapp, tmp_path):
    dash = make_dashboard(qapp, tmp_path, n=3)
    assert dash.rail_rows_layout.count() == 3
    dash.close()


# ---------------- InstanceCard wiring (Task 4) ----------------

def test_rail_shows_one_instance_card_per_account_starting_idle(qapp, tmp_path):
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
                             on_hardware=None):
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


def test_launch_failure_shows_friendly_message_not_raw_exception(qapp, tmp_path, monkeypatch):
    calls = stub_message_boxes(monkeypatch)
    FakeClient.fail_upload = True
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


def test_launch_with_no_accounts_shows_actionable_warning(qapp, tmp_path, monkeypatch):
    calls = stub_message_boxes(monkeypatch)
    dash = make_dashboard(qapp, tmp_path, store=AccountStore())
    dash.blend = tmp_path / "x.blend"
    dash._launch()
    assert calls["warning"]
    title, message = calls["warning"][0]
    assert "add" in message.lower()
    dash.close()


def test_launch_with_bad_frame_range_shows_actionable_warning(qapp, tmp_path, monkeypatch):
    calls = stub_message_boxes(monkeypatch)
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
                             on_hardware=None):
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


def test_the_filmstrip_tells_the_user_its_completed_cells_are_approximate(qapp, tmp_path):
    """charts.frame_done can only approximate which frames are done (the
    notebook reports a count of successes, not a list), so the UI has to
    say so -- a caveat that lives only in a docstring is invisible to the
    person reading a green cell as proof the frame exists."""
    dash = make_dashboard(qapp, tmp_path)
    labels = [w.text() for w in dash.findChildren(dashboard_mod.QLabel)]
    filmstrip_headers = [t for t in labels if "Filmstrip" in t]
    assert filmstrip_headers, "the filmstrip header label went missing"
    header = filmstrip_headers[0]
    assert "approximate" in header.lower()
    assert "failed frame" in header.lower(), \
        "say WHY it is approximate, not just that it is"
