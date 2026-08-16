"""The Qt-free adapter, and whether it still tells the truth.

blendfleet/rpc/session.py is a port of ui/bridge.py with Qt taken out, so
the dashboard can run under an Electron shell that talks to a headless
Python sidecar. The port is deliberate duplication -- bridge.py is not
edited, because the Qt build is what is being used for real renders --
and duplication drifts.

So the tests that matter here are PARITY tests: for the same seeded
fleet, the two adapters must produce the same payload, byte for byte. A
difference is either a port bug or a change somebody made to one and not
the other, and both are worth failing over.

The rest covers what the port genuinely changed: dialogs it can no longer
open, and the events it now delivers through Emitters.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from blendfleet.accounts import Account, AccountStore
from blendfleet.fleet import Fleet, FleetState, WorkerState
from blendfleet.rpc.session import Session
from blendfleet.settings import Settings
from blendfleet.ui.bridge import Backend

from test_bridge import FakeClient


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _store(n=2):
    return AccountStore([
        Account(label=f"acct{i}", token=f"KGAT_{i:032x}",
                username=f"user_{i}", verified=(i != 1))
        for i in range(n)])


def _adapters(tmp_path, n=2):
    """One of each, on their own copy of the same fleet.

    Separate directories on purpose: both adapters poll and write state,
    and a shared directory would let one's writes answer the other's
    read, which would make a drifted payload look identical.
    """
    made = []
    for name in ("qt", "rpc"):
        root = tmp_path / name
        root.mkdir()
        store = _store(n)
        factory = (lambda accounts, root=root: Fleet(
            accounts, lambda t: FakeClient(t), root / "w"))
        made.append((store, factory, root))
    qt = Backend(made[0][0], made[0][1], lambda t: "someone", Settings())
    rpc = Session(made[1][0], made[1][1], lambda t: "someone", Settings())
    return qt, rpc, made


def _seed(factory, store, root, workers):
    factory(store.list()).save_jobs([FleetState(
        job_id="job", blend_name="waydown.blend", start_frame=1, end_frame=8,
        workers=workers)])


def _stop(*adapters):
    for adapter in adapters:
        adapter.stop()


# ---------------- parity ----------------

def test_an_idle_fleet_reads_the_same_through_both_adapters(qapp, tmp_path):
    qt, rpc, _ = _adapters(tmp_path)
    try:
        assert json.loads(rpc.state()) == json.loads(qt.state())
        assert json.loads(rpc.accounts()) == json.loads(qt.accounts())
        assert json.loads(rpc.preferences()) == json.loads(qt.preferences())
    finally:
        _stop(qt, rpc)


def test_a_running_render_reads_the_same_through_both_adapters(qapp, tmp_path):
    """The payload that carries every honesty rule worth having: which
    account is live, how many frames it claims, and how old that claim
    is."""
    qt, rpc, made = _adapters(tmp_path)
    workers = [
        WorkerState(label="acct0", username="user_0", kernel_slug="user_0/k",
                    frames=[1, 3, 5], state="running", frames_done=2),
        WorkerState(label="acct1", username="user_1", kernel_slug="user_1/k",
                    frames=[2, 4, 6], state="complete", frames_done=3),
    ]
    try:
        for store, factory, root in made:
            _seed(factory, store, root, workers)
        left, right = json.loads(qt.state()), json.loads(rpc.state())
        assert right == left
        # And not vacuously: the payload has to actually carry the render.
        by_label = {i["label"]: i for i in right["instances"]}
        assert by_label["acct0"]["worker"]["state"] == "running"
        assert by_label["acct0"]["worker"]["framesDone"] == 2
    finally:
        _stop(qt, rpc)


def test_a_stopped_render_with_an_unreadable_count_reads_the_same(qapp,
                                                                  tmp_path):
    """The subtlest payload in the app: a render that ended without its
    log being readable, where the count on disk is a floor and the page
    is required to say so rather than draw a bar."""
    qt, rpc, made = _adapters(tmp_path)
    workers = [WorkerState(label="acct0", username="user_0",
                           kernel_slug="user_0/k", frames=[1, 2],
                           state="complete", frames_done=1,
                           frames_done_at=0.0)]
    try:
        for store, factory, root in made:
            _seed(factory, store, root, workers)
        assert json.loads(rpc.state()) == json.loads(qt.state())
    finally:
        _stop(qt, rpc)


def test_live_renders_agrees_across_adapters(qapp, tmp_path):
    """What the window asks before it closes."""
    qt, rpc, made = _adapters(tmp_path)
    workers = [WorkerState(label="acct0", username="user_0",
                           kernel_slug="user_0/k", frames=[1, 2],
                           state="running")]
    try:
        for store, factory, root in made:
            _seed(factory, store, root, workers)
        assert rpc.live_renders() == qt.live_renders()
        assert rpc.live_renders()["accounts"] == 1
    finally:
        _stop(qt, rpc)


def _contract(adapter_class):
    """The methods a page could call on this adapter.

    Qt's own QObject API is not part of the contract, and neither are the
    Signals -- which are class attributes on the Qt side and instance
    Emitters on this one, so they are compared separately below.
    """
    import PySide6.QtCore
    qobject = {n for n in dir(PySide6.QtCore.QObject) if not n.startswith("_")}
    signals = {"stateChanged", "accountsChanged", "settingsChanged",
               "telemetry", "uploadProgress", "downloadProgress",
               "framePreview", "logLine", "notification", "healthChanged",
               "busyChanged", "scenesChanged", "outputsChanged",
               "collectFinished"}
    return {n for n in dir(adapter_class)
            if not n.startswith("_")
            and callable(getattr(adapter_class, n))} - qobject - signals


def test_both_adapters_offer_the_same_methods_to_the_page():
    """A method the page calls and the sidecar cannot answer is a dead
    button."""
    missing = _contract(Backend) - _contract(Session) - {"pickBlend"}
    assert not missing, f"the sidecar cannot answer: {sorted(missing)}"
    assert "setBlend" in _contract(Session)
    assert "pickBlend" not in _contract(Session), (
        "a headless sidecar has no window to parent a file dialog to -- "
        "the shell shows it and calls setBlend")


def test_both_adapters_emit_the_same_events(qapp, tmp_path):
    """Every signal the page connects to has to exist on the sidecar, by
    the same name, or the handler is simply never called -- which looks
    like a render that stopped reporting rather than like a bug."""
    qt, rpc, _ = _adapters(tmp_path)
    try:
        for name in ("stateChanged", "accountsChanged", "settingsChanged",
                     "telemetry", "uploadProgress", "downloadProgress",
                     "framePreview", "logLine", "notification",
                     "healthChanged", "busyChanged", "scenesChanged",
                     "outputsChanged", "collectFinished"):
            assert hasattr(qt, name), name
            event = getattr(rpc, name, None)
            assert event is not None, f"the sidecar never emits {name}"
            assert hasattr(event, "connect") and hasattr(event, "emit"), name
    finally:
        _stop(qt, rpc)


# ---------------- what the port changed on purpose ----------------

def test_setting_the_blend_takes_a_path_instead_of_opening_a_dialog(qapp,
                                                                    tmp_path):
    blend = tmp_path / "waydown.blend"
    blend.write_bytes(b"BLENDER")
    _, rpc, _ = _adapters(tmp_path)
    try:
        answer = json.loads(rpc.setBlend(str(blend)))
        assert answer["name"] == "waydown.blend"
        assert answer["path"] == str(blend)
    finally:
        rpc.stop()


def test_a_dismissed_chooser_leaves_the_previous_choice_alone(qapp, tmp_path):
    """An empty path is a cancelled dialog, and cancelling has never
    meant "forget what I picked before"."""
    blend = tmp_path / "waydown.blend"
    blend.write_bytes(b"BLENDER")
    _, rpc, _ = _adapters(tmp_path)
    try:
        rpc.setBlend(str(blend))
        assert json.loads(rpc.setBlend(""))["name"] == "waydown.blend"
    finally:
        rpc.stop()


def test_a_collect_with_nowhere_to_put_the_frames_does_not_start(qapp,
                                                                 tmp_path):
    """The destination is the shell's question to ask. No answer means
    the chooser was dismissed."""
    _, rpc, _ = _adapters(tmp_path)
    busy = []
    try:
        rpc.busyChanged.connect(lambda key, state: busy.append((key, state)))
        rpc.collect("", "", "")
        assert busy == [], "a collect started with no destination"
    finally:
        rpc.stop()


def test_events_reach_a_plain_handler(qapp, tmp_path):
    """The whole point of the Emitter: no Qt, same connect()."""
    _, rpc, _ = _adapters(tmp_path)
    seen = []
    try:
        rpc.notification.connect(lambda message, tone: seen.append((message,
                                                                    tone)))
        rpc.previewFrame(7, "no-such-job")
        assert seen, "nothing was emitted"
        assert seen[0][1] in {"idle", "offline"}
    finally:
        rpc.stop()


def test_stopping_twice_is_harmless(qapp, tmp_path):
    """stop() runs when the pipe closes, and a pipe can close while a
    stop is already under way."""
    _, rpc, _ = _adapters(tmp_path)
    rpc.stop()
    rpc.stop()
