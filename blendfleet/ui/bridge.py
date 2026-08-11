"""The one seam between the web UI and the Python that does the work.

Everything the page can ask for, and everything it gets told, passes
through the Backend object below. That is the point: the UI is HTML/CSS/JS
in a QWebEngineView, but nothing in it may invent state. `render-farm
(7).html` -- the design this port follows -- is a SIMULATION, with its own
fake `instances[]`, its own `tickRender()`, its own generated frame
images. Every one of those has to be replaced by a call through here, or
the app goes back to showing numbers nobody measured.

CONTRACT
  - JS -> Python is a @Slot. Slots return JSON strings, not QVariant maps:
    an explicit `json.dumps` at the boundary means the shape of every
    payload is written down in one place and cannot drift silently as a
    dataclass gains a field.
  - Python -> JS is a Signal carrying a JSON string, for the same reason.
  - Nothing here blocks. Every call that touches the network runs on a
    QThread and answers with a signal, because a slot invoked from the
    page runs on the UI thread and a blocking one freezes the render, not
    just the widget.

Honesty rules carried over from the Qt UI, which this must not lose:
  - Quota is what the API says right now, never a promise.
  - Cached hardware is labelled with its age; Kaggle reallocates.
  - There is no idle instance to poll -- live telemetry exists only while
    a kernel runs.
  - Frames "done" is a COUNT the notebook reports, not a list, so the
    frame grid is approximate and says so.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from blendfleet.accounts import AccountStore
from blendfleet.assignment import estimate
from blendfleet.instance_state import InstanceStore
from blendfleet.notebook_builder import RenderSettings
from blendfleet.settings import Settings
from blendfleet.ui.messages import explain

SECONDS_PER_FRAME_DEFAULT = 57.1     # measured: 1920x1080, 128spp, P100


class _Worker(QThread):
    """One off-thread call, answered on the UI thread.

    The same shape as the Qt UI's _CallWorker: `action` is a gerund phrase
    used to build a friendly message via ui.messages.explain if `fn`
    raises, so the page never sees a raw traceback.
    """

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], object], action: str, parent=None) -> None:
        super().__init__(parent)
        self._fn = fn
        self._action = action

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as e:      # noqa: BLE001 -- turned into a message
            self.failed.emit(explain(self._action, e))
        else:
            self.succeeded.emit(result)


class Backend(QObject):
    """Registered on the QWebChannel as `backend`.

    The page does `new QWebChannel(qt.webChannelTransport, ch => {
    window.backend = ch.objects.backend })` and from then on calls slots
    and connects to the signals below.
    """

    # ---- Python -> JS -------------------------------------------------
    stateChanged = Signal(str)      # the whole fleet state, as JSON
    accountsChanged = Signal(str)
    settingsChanged = Signal(str)
    telemetry = Signal(str)         # one GPU sample
    uploadProgress = Signal(str)
    downloadProgress = Signal(str)
    logLine = Signal(str, str)      # (message, tone)
    notification = Signal(str, str)
    healthChanged = Signal(str)
    busyChanged = Signal(str, bool)  # (action key, in flight)

    def __init__(self, store: AccountStore, fleet_factory, verifier,
                 settings: Settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.fleet_factory = fleet_factory
        self.verifier = verifier
        self.settings = settings
        self.instance_store = InstanceStore.load()
        self.blend: Path | None = None
        self._last_state = None
        self._quota: dict[str, str] = {}
        self._failures: dict[str, str] = {}
        self._workers: dict[str, _Worker] = {}
        self._last_poll_ms: int | None = None
        self._last_poll_at: str | None = None
        self._online = True

    # ---- helpers ------------------------------------------------------
    def _start(self, key: str, fn, action: str, on_ok, on_fail=None) -> bool:
        """Run `fn` off-thread under `key`, skipping if one is in flight.

        Skipping rather than queueing is deliberate and matches the Qt UI:
        a 30s poll timer firing again before a slow poll returns must not
        stack a second network call on top of the first.
        """
        if key in self._workers:
            return False
        worker = _Worker(fn, action, self)
        self._workers[key] = worker
        self.busyChanged.emit(key, True)

        def done(result) -> None:
            self._workers.pop(key, None)
            self.busyChanged.emit(key, False)
            on_ok(result)

        def failed(message: str) -> None:
            self._workers.pop(key, None)
            self.busyChanged.emit(key, False)
            if on_fail is not None:
                on_fail(message)
            else:
                self.notification.emit(message, "offline")
                self.logLine.emit(message, "offline")

        worker.succeeded.connect(done)
        worker.failed.connect(failed)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        return True

    def _accounts_payload(self) -> list[dict]:
        return [{"label": a.label, "username": a.username,
                 "verified": bool(a.verified)} for a in self.store.list()]

    def _state_payload(self) -> dict:
        """Everything the page needs to draw the fleet, in one object.

        ACCOUNT-first, with a worker attached when there is one -- not
        worker-first with accounts alongside. Kaggle has no idle instances:
        a session exists only while a kernel runs, so between renders there
        is no worker at all, and an account is the only durable unit. A
        worker-keyed payload silently drops quota and last-known hardware
        for every idle account, which is most of them most of the time.

        Deliberately includes `approximate: True` on the frame data. The
        notebook reports how many frames succeeded, not which, so a failed
        frame shifts every later cell for that account -- the UI has to be
        able to say so rather than presenting a green cell as proof.
        """
        state = self._last_state
        by_label = {w.label: w for w in (state.workers if state else [])}
        instances = []
        for account in self.store.list():
            worker = by_label.get(account.label)
            instances.append({
                "label": account.label,
                "username": account.username,
                "verified": bool(account.verified),
                # Live only while a kernel runs; None means idle, which is
                # a real state and not an error.
                "worker": {
                    "state": worker.state,
                    "frames": list(worker.frames),
                    "framesDone": worker.frames_done,
                    "message": (worker.message
                                or self._failures.get(worker.label) or ""),
                } if worker is not None else None,
                # Live, cheap to poll, and never a promise.
                "quota": self._quota.get(account.label, ""),
                # Last-KNOWN, always carrying its age.
                "hardware": _snapshot_payload(
                    self.instance_store.get(account.label)),
            })
        return {
            "job": {
                "blend": state.blend_name,
                "startFrame": state.start_frame,
                "endFrame": state.end_frame,
            } if state else None,
            "instances": instances,
            "approximate": True,
        }

    def _emit_state(self) -> None:
        self.stateChanged.emit(json.dumps(self._state_payload()))

    # ---- JS -> Python: reads ------------------------------------------
    @Slot(result=str)
    def accounts(self) -> str:
        return json.dumps(self._accounts_payload())

    @Slot(result=str)
    def state(self) -> str:
        return json.dumps(self._state_payload())

    @Slot(result=str)
    def preferences(self) -> str:
        return json.dumps({
            "accent": self.settings.accent,
            "theme": self.settings.theme,
            "translucent": self.settings.translucent,
            "minGpus": self.settings.min_gpus,
            "fullscreen": self.settings.fullscreen,
        })

    @Slot(int, int, result=str)
    def estimateRender(self, start_frame: int, end_frame: int) -> str:
        """Hours per account for a frame range, with the measurement the
        figure came from -- the page must show the caveat, not just the
        number."""
        accounts = max(len(self.store.list()), 1)
        frames = max(end_frame - start_frame + 1, 0)
        hours = estimate(frames, SECONDS_PER_FRAME_DEFAULT, accounts)
        return json.dumps({
            "frames": frames,
            "accounts": accounts,
            "hours": hours,
            "basis": (f"at {SECONDS_PER_FRAME_DEFAULT:.0f}s/frame measured "
                      "on a P100 at 1920x1080/128spp — your scene will differ"),
        })

    @Slot(result=str)
    def health(self) -> str:
        """Sourced from the poll this app already runs -- never a synthetic
        ping. There is no packet-loss figure because nothing here measures
        packet loss, and a row permanently reading 0% would be a decoration
        pretending to be an instrument."""
        reachable = sum(1 for v in self._quota.values() if v != "unavailable")
        return json.dumps({
            "online": self._online,
            "lastPollMs": self._last_poll_ms,
            "lastPollAt": self._last_poll_at,
            "accountsReachable": reachable,
            "accountsTotal": len(self.store.list()),
        })

    # ---- JS -> Python: writes -----------------------------------------
    @Slot(str, str)
    def setPreference(self, key: str, value: str) -> None:
        """One setter for every preference, taking strings.

        JSON-decoded rather than trusted: the page sends "true"/"2" and
        Settings' own __post_init__ is what validates, so a malformed value
        from a page bug lands on the same defensive path as a hand-edited
        settings.json.
        """
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = value
        mapping = {"accent": "accent", "theme": "theme",
                   "translucent": "translucent", "minGpus": "min_gpus"}
        field = mapping.get(key)
        if field is None:
            return
        setattr(self.settings, field, decoded)
        self.settings.__post_init__()       # re-validate, never trust the page
        self.settings.save()
        self.settingsChanged.emit(self.preferences())

    @Slot()
    def poll(self) -> None:
        """Refresh every worker's kernel status. Cheap to call from the
        page; skipped if one is already in flight."""
        accounts = self.store.list()
        started = time.monotonic()

        def work():
            return self.fleet_factory(accounts).poll()

        def ok(state) -> None:
            self._last_poll_ms = int((time.monotonic() - started) * 1000)
            self._last_poll_at = time.strftime("%H:%M:%S")
            if state:
                self._last_state = state
            if not self._online:
                self._online = True
                self.logLine.emit("reconnected to Kaggle", "active")
            self._emit_state()
            self.healthChanged.emit(self.health())

        def fail(message: str) -> None:
            self._last_poll_ms = int((time.monotonic() - started) * 1000)
            if self._online:
                self._online = False
                self.logLine.emit("lost contact with Kaggle", "offline")
            self.notification.emit(message, "offline")
            self.healthChanged.emit(self.health())

        self._start("poll", work, "Checking render status", ok, fail)

    @Slot()
    def refreshQuota(self) -> None:
        """Best-effort per-account quota. A single account's fetch failing
        (rate limit, revoked token) only ever downgrades that figure to
        "unavailable" -- it must never break the page or block a render."""
        accounts = self.store.list()

        def work() -> dict[str, str]:
            try:
                client_factory = self.fleet_factory(accounts).client_factory
            except Exception:
                client_factory = None
            result: dict[str, str] = {}
            for acct in accounts:
                if client_factory is None:
                    result[acct.label] = "unavailable"
                    continue
                try:
                    q = client_factory(acct.token).quota()
                    result[acct.label] = (f"{q.used_seconds / 3600.0:.1f} / "
                                          f"{q.total_seconds / 3600.0:.1f} h")
                except Exception:
                    result[acct.label] = "unavailable"
            return result

        def ok(result: dict) -> None:
            self._quota.update(result)
            self._emit_state()
            self.healthChanged.emit(self.health())

        self._start("quota", work, "Refreshing quota", ok, lambda _m: None)

    def stop(self) -> None:
        """Wait for whatever is in flight. Called from the host window's
        closeEvent: a QThread still running when Qt destroys its QObject
        is the same class of bug the Qt UI documents at length."""
        for worker in list(self._workers.values()):
            try:
                if worker.isRunning():
                    worker.wait(5000)
            except RuntimeError:
                pass            # already finished and deleted
        self._workers.clear()


def _snapshot_payload(snapshot) -> dict | None:
    """Last-known hardware, ALWAYS with its age.

    Kaggle's allocation varies run to run -- a P100 last time does not mean
    a P100 next time -- so a hardware line without an age is a claim this
    app is not entitled to make.
    """
    if snapshot is None:
        return None
    return {
        "gpus": [{"index": g.index, "memTotal": g.mem_total, "model": g.model}
                 for g in snapshot.gpus],
        "cpuCount": snapshot.cpu_count,
        "ramTotal": snapshot.ram_total,
        "observedAt": snapshot.observed_at,
        "ageSeconds": max(time.time() - snapshot.observed_at, 0.0),
    }
