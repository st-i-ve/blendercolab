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
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from blendfleet.accounts import AccountStore
from blendfleet.assignment import estimate
from blendfleet.instance_state import (GpuSnapshot, InstanceSnapshot,
                                       InstanceStore)
from blendfleet.log_stream import stream_progress
from blendfleet.notebook_builder import RenderSettings
from blendfleet.settings import Settings
from blendfleet.ui.messages import explain

SECONDS_PER_FRAME_DEFAULT = 57.1     # measured: 1920x1080, 128spp, P100
POLL_INTERVAL_MS = 30_000            # real network calls: kernel status
LIVE_INTERVAL_MS = 2_000             # cheap: drain the in-memory queues


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
        # What is on Kaggle right now, as far as this session knows:
        # {slug, blendName, sizeBytes, at}. Set only by syncDataset(), so
        # it is never a guess -- an empty value means we have not put this
        # scene up during this session, not that Kaggle has nothing.
        self._dataset: dict | None = None
        self._last_poll_ms: int | None = None
        self._last_poll_at: str | None = None
        self._online = True

        # ---- live telemetry --------------------------------------------
        # `kernels logs`/`kernels output` return NOTHING until a kernel is
        # COMPLETE, so a 30-second status poll can only ever say "queued"
        # or "running" -- it cannot say "installing Blender" or "frame 7 of
        # 15". The SSE log stream is the only source of live progress, live
        # per-GPU telemetry and the hardware a session actually got.
        #
        # One daemon thread per worker fills these queues; _live_tick
        # drains them on the UI thread. Nothing touches the payload from a
        # stream thread.
        self._stop = threading.Event()
        self._stream_threads: list[threading.Thread] = []
        self._progress_q: "queue.Queue[tuple[str, int, int]]" = queue.Queue()
        self._telemetry_q: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._hardware_q: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        # Notifications raised BY a stream thread (a hardware check
        # that could not be watched). Queued like everything else so
        # the signal is emitted on the UI thread, never from the
        # thread that noticed.
        self._notify_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._preflight_q: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        # label -> what we have seen live this run. Cleared per launch.
        self._live: dict[str, dict] = {}

        # A status poll is infrequent and costs a network call per account;
        # the live drain is cheap and purely in-memory. Two timers, two
        # rates -- the Qt UI's own split, for the same reasons.
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self.poll)
        self._poll_timer.start(POLL_INTERVAL_MS)
        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._live_tick)
        self._live_timer.start(LIVE_INTERVAL_MS)

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
                 "verified": bool(a.verified),
                 # Distinct from not-verified: Kaggle has REJECTED this
                 # token, so the page says "replace it" rather than
                 # "check it". Never set by a network failure.
                 "revoked": bool(getattr(a, "revoked", False))}
                for a in self.store.list()]

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
                "revoked": bool(getattr(account, "revoked", False)),
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
                # What the log stream has seen THIS run: the phase, live
                # per-GPU utilisation and memory, and the hardware the
                # session actually got. None until a stream reports --
                # never a cached value dressed up as live.
                "live": self._live_payload(account.label),
            })
        return {
            "job": {
                "blend": state.blend_name,
                "startFrame": state.start_frame,
                "endFrame": state.end_frame,
            } if state else None,
            "instances": instances,
            "dataset": self._dataset,
            "blend": {"path": str(self.blend), "name": self.blend.name}
                     if self.blend else None,
            "approximate": True,
        }

    def _live_payload(self, label: str) -> dict | None:
        """What the SSE stream has reported for `label` this run.

        None when nothing has arrived. That is the honest answer between
        renders: there is no idle session to poll, so "no live data" is a
        state, not a gap to paper over with the last run's numbers.
        """
        slot = self._live.get(label)
        if not slot:
            return None
        return {
            "phase": slot["phase"],
            "framesDone": slot["framesDone"],
            "framesTotal": slot["framesTotal"],
            "gpus": [slot["gpus"][k] for k in sorted(slot["gpus"])],
            "cpuCount": slot["cpuCount"],
            "ramTotal": slot["ramTotal"],
            "preflight": slot["preflight"],
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
            "sound": self.settings.sound,
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
                   "translucent": "translucent", "sound": "sound",
                   "minGpus": "min_gpus"}
        field = mapping.get(key)
        if field is None:
            return
        setattr(self.settings, field, decoded)
        self.settings.__post_init__()       # re-validate, never trust the page
        self.settings.save()
        # The page restyles itself from its own CSS variables, but the
        # WINDOW around it is Qt -- the title bar, the shell background
        # behind the view, the Mica/dark-chrome flags, and any dialog
        # (SetupDialog, QFileDialog) opened later. Saving the preference
        # without applying it leaves all of that on the old theme, which
        # is exactly the "background does not change" bug. theme.apply()
        # also fires theme_signal, which is what WebHost listens to.
        if field in ("theme", "accent"):
            from PySide6.QtWidgets import QApplication
            from blendfleet.ui import theme as theme_module
            app = QApplication.instance()
            if app is not None:
                theme_module.apply(app, self.settings.accent,
                                   self.settings.theme)
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

    # ---- JS -> Python: actions ----------------------------------------
    @Slot(result=str)
    def pickBlend(self) -> str:
        """Open the OS file chooser and remember the choice.

        A native dialog rather than an <input type=file>: the page is not
        given filesystem access, and the app needs a real path to upload,
        not a sandboxed File object.
        """
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            None, "Select .blend", "", "Blender (*.blend)")
        if path:
            self.blend = Path(path)
        return json.dumps({"path": str(self.blend) if self.blend else "",
                           "name": self.blend.name if self.blend else ""})

    @Slot()
    def syncDataset(self) -> None:
        """Upload the chosen .blend to Kaggle as a dataset, on its own.

        Costs no GPU quota -- a dataset upload is not a session -- so this
        is safe to run whenever, and separating it means the slowest and
        most failure-prone step is no longer able to take a whole render
        attempt down with it. Once it has run, launch() reuses the result
        instead of uploading the same scene again.
        """
        if self.blend is None:
            self.notification.emit("Choose a .blend file first.", "offline")
            return
        if not self.store.list():
            self.notification.emit(
                "Add at least one Kaggle account first.", "offline")
            return
        accounts = self.store.list()
        blend = self.blend
        owner = accounts[0].label

        def work():
            fleet = self.fleet_factory(accounts)
            return fleet.prepare_dataset(
                blend,
                # uploader.UploadProgress calls them `uploaded` and
                # `total`. Reading sent_bytes/total_bytes through getattr
                # defaults meant every tick reported 0 of 0 and the bar
                # never moved -- a typo that a default silently absorbed.
                on_progress=lambda p: self.uploadProgress.emit(json.dumps({
                    "label": owner,
                    "stage": "uploading",
                    "uploaded": p.uploaded,
                    "total": p.total,
                })),
                on_stage=lambda key, detail: self.uploadProgress.emit(
                    json.dumps({"label": owner, "stage": key,
                                "detail": detail})))

        def ok(slug) -> None:
            self._dataset = {
                "slug": slug,
                "blendName": blend.name,
                "sizeBytes": blend.stat().st_size,
                "at": time.strftime("%H:%M:%S"),
            }
            self.logLine.emit(f"dataset ready: {slug}", "active")
            self.notification.emit(f"Uploaded {blend.name} to {slug}", "active")
            self._emit_state()

        self._start("dataset", work, "Uploading the scene", ok)

    @Slot(str)
    def launch(self, options_json: str) -> None:
        """Start a render across every account.

        Refuses rather than guesses when the request cannot be honoured --
        no accounts, no file, or a backwards frame range. The page shows
        the reason; it does not get to proceed with a default.
        """
        options = json.loads(options_json)
        start = int(options.get("startFrame", 1))
        end = int(options.get("endFrame", 1))
        if not self.store.list():
            self.notification.emit(
                "Add at least one Kaggle account before rendering.", "offline")
            return
        if self.blend is None:
            self.notification.emit("Choose a .blend file first.", "offline")
            return
        if end < start:
            self.notification.emit(
                f"End frame ({end}) is before the start frame ({start}).",
                "offline")
            return

        settings = RenderSettings(
            int(options.get("resX", 1920)), int(options.get("resY", 1080)),
            int(options.get("samples", 128)), options.get("format", "PNG"),
            min_gpus=self.settings.min_gpus)
        accounts = self.store.list()
        blend = self.blend
        owner = accounts[0].label

        # Reuse the dataset only when it is THIS scene. A slug left over
        # from a different .blend would render the wrong thing on somebody
        # else's quota, so the name has to match before we skip the
        # upload; fleet.launch verifies the content besides.
        prepared = (self._dataset["slug"]
                    if self._dataset
                    and self._dataset["blendName"] == blend.name else None)

        def work():
            return self.fleet_factory(accounts).launch(
                blend, settings, start, end, dataset_slug=prepared,
                on_progress=lambda p: self.uploadProgress.emit(json.dumps({
                    "label": owner,
                    "stage": "uploading",
                    "uploaded": p.uploaded,
                    "total": p.total,
                })))

        def ok(state) -> None:
            self._last_state = state
            self._start_streams(state)
            self.logLine.emit(
                f"render started on {len(accounts)} account(s)", "active")
            self.notification.emit("Render started", "active")
            self._emit_state()
            self.refreshQuota()

        self._start("launch", work, "Starting the render", ok)

    @Slot(str)
    def startInstances(self, labels_json: str) -> None:
        """Bring machines up warm, without giving them work yet.

        Requires an uploaded scene: a warm worker attaches the dataset at
        session start, so there is nothing to warm up around until one
        exists. Refusing here is much cheaper than starting machines that
        would have to be thrown away and restarted once the upload lands.

        Every machine started is spending quota from this moment. The
        worker's own idle timeout is what bounds that -- see
        notebook_builder.IDLE_TIMEOUT_S -- and it shuts itself down rather
        than relying on this app still being here.
        """
        labels = json.loads(labels_json or "[]")
        if not labels:
            labels = [a.label for a in self.store.list()]
        if not labels:
            self.notification.emit("No accounts to start.", "offline")
            return
        if self._dataset is None:
            self.notification.emit(
                "Upload a scene first — a warm machine attaches the dataset "
                "when it starts, so there is nothing to warm up around yet.",
                "offline")
            return

        accounts = self.store.list()
        slug = self._dataset["slug"]
        settings = RenderSettings(1920, 1080, 128,
                                  min_gpus=self.settings.min_gpus)

        def work():
            return self.fleet_factory(accounts).start_workers(
                labels, settings, slug)

        def ok(state) -> None:
            self._last_state = state
            self._start_streams(state)
            self.logLine.emit(
                f"started {len(labels)} machine(s) warm — they are spending "
                "quota while they wait", "warn")
            self.notification.emit(
                f"Starting {len(labels)} machine(s). They report their "
                "hardware as they come up, and shut themselves down after "
                "10 idle minutes.", "idle")
            self._emit_state()
            self.poll()

        self._start("start", work, "Starting machines", ok)

    @Slot(str)
    def sendJob(self, options_json: str) -> None:
        """Give work to machines that are already warm.

        Publishes a job descriptor the running workers pick up on their
        next poll, instead of pushing new kernels. No setup cost, and no
        second session per account.
        """
        options = json.loads(options_json)
        start = int(options.get("startFrame", 1))
        end = int(options.get("endFrame", 1))
        if end < start:
            self.notification.emit(
                f"End frame ({end}) is before the start frame ({start}).",
                "offline")
            return
        warm = [w.label for w in (self._last_state.workers
                                  if self._last_state else [])]
        if not warm:
            self.notification.emit(
                "No warm machines — start some first, or use Render across "
                "fleet to push a one-shot job.", "offline")
            return

        from blendfleet.assignment import assign_frames
        buckets = assign_frames(start, end, len(warm))
        job = {
            "id": uuid.uuid4().hex[:8],
            "workers": warm,
            "frames": [],
            "resX": int(options.get("resX", 1920)),
            "resY": int(options.get("resY", 1080)),
            "samples": int(options.get("samples", 128)),
            "format": options.get("format", "PNG"),
        }
        # One descriptor per worker: a machine renders only its own stride,
        # exactly as a one-shot job splits them.
        per_worker = {label: frames for label, frames in zip(warm, buckets)}
        accounts = self.store.list()

        def work():
            fleet = self.fleet_factory(accounts)
            # Published one job at a time, keyed to the worker it is for --
            # a single shared descriptor cannot carry a different frame
            # list per machine.
            for label, frames in per_worker.items():
                fleet.publish_job(dict(job, workers=[label], frames=frames))
            return len(per_worker)

        def ok(count) -> None:
            self.logLine.emit(
                f"sent job {job['id']} to {count} warm machine(s)", "active")
            self.notification.emit(
                f"Sent {end - start + 1} frames to {count} warm machine(s)",
                "active")
            self.poll()

        self._start("job", work, "Sending the job", ok)

    @Slot()
    def cancelAll(self) -> None:
        accounts = self.store.list()

        def work():
            return self.fleet_factory(accounts).cancel_all()

        def ok(results) -> None:
            results = list(results or [])
            failed = [r for r in results if not r.ok]
            if not results:
                self.notification.emit("Nothing to cancel.", "idle")
            elif failed:
                # A silently-failed cancel is the worst outcome in this
                # app: the user believes the render stopped while it keeps
                # draining a friend's weekly GPU quota. Name them.
                detail = "; ".join(f"{r.label}: {r.error}" for r in failed)
                self.notification.emit(
                    f"{len(failed)} of {len(results)} did NOT stop and may "
                    f"still be spending quota — {detail}. Stop them by hand "
                    f"at kaggle.com.", "offline")
            else:
                self.notification.emit(
                    f"Cancelled {len(results)} account(s)", "idle")
            self.poll()

        self._start("cancel", work, "Cancelling the render", ok)

    @Slot()
    def forgetJob(self) -> None:
        """Stop tracking a job this app can no longer control.

        For the deadlock where Kaggle reports a kernel as active but
        refuses to cancel it: cancel cannot clear it, and launch keeps
        refusing because a job is still running. This unwedges the app.

        It does NOT stop anything. Whatever is running on Kaggle keeps
        running and keeps spending quota -- and this app will no longer be
        able to cancel it or collect its frames. The message says exactly
        that, because a button that quietly abandons somebody else's
        running GPU session while sounding like a cancel would be the worst
        kind of lie this app could tell.
        """
        accounts = self.store.list()

        def work():
            return self.fleet_factory(accounts).forget_job()

        def ok(workers) -> None:
            self._last_state = None
            self._live = {}
            if not workers:
                self.notification.emit("There was no tracked job.", "idle")
            else:
                names = ", ".join(
                    f"{w.label} ({w.kernel_slug})" for w in workers)
                self.notification.emit(
                    f"Stopped tracking {len(workers)} kernel(s). They are "
                    f"NOT cancelled — if still running they keep spending "
                    f"quota, and this app can no longer stop or collect "
                    f"them: {names}. Stop them by hand at kaggle.com.",
                    "offline")
                self.logLine.emit(
                    f"stopped tracking {len(workers)} kernel(s) — not "
                    "cancelled", "warn")
            self._emit_state()

        self._start("forget", work, "Forgetting the job", ok)

    @Slot(str)
    def cancelInstance(self, label: str) -> None:
        """Stop ONE account's session.

        Kaggle's unit of control is the session, not a GPU within it, so
        this stops that account's whole session -- the wording the page
        shows says exactly that rather than implying a GPU can be released.
        """
        accounts = self.store.list()

        def work():
            return self.fleet_factory(accounts).cancel_worker(label)

        def ok(result) -> None:
            if result is None:
                self.notification.emit(f"{label} had already stopped.", "idle")
            elif result.ok:
                self.notification.emit(f"Cancelled {label}", "idle")
            else:
                self.notification.emit(
                    f"{label} did NOT stop and may still be spending quota: "
                    f"{result.error}", "offline")
            self.poll()

        self._start(f"cancel:{label}", work, f"Cancelling {label}", ok)

    @Slot(str)
    def collect(self, label: str = "") -> None:
        """Download rendered frames -- the whole fleet, or one account."""
        from PySide6.QtWidgets import QFileDialog
        from blendfleet.collector import collect as collect_frames

        destination = QFileDialog.getExistingDirectory(None, "Save frames to")
        if not destination:
            return
        accounts = self.store.list()
        who = label or "the fleet"

        def work():
            fleet = self.fleet_factory(accounts)
            state = fleet.load()
            if state is None:
                return None
            return collect_frames(
                state, accounts, fleet.client_factory, Path(destination),
                worker_label=label or None,
                # downloader.DownloadProgress calls them `downloaded` and
                # `total` -- the same mistake as the upload side, which a
                # getattr default turned into a permanent 0 of 0 instead of
                # an error. Read directly so a rename fails loudly.
                on_progress=lambda lbl, p: self.downloadProgress.emit(
                    json.dumps({
                        "label": lbl,
                        "downloaded": p.downloaded,
                        "total": p.total,
                    })))

        def ok(report) -> None:
            if report is None:
                self.notification.emit(
                    "No render job found — start a render first.", "idle")
                return
            message = f"Collected {report.copied} frame(s) from {who}"
            if report.missing_frames:
                # Never presented as a complete set when it is not one.
                message += (f" — {len(report.missing_frames)} still missing "
                            "(not rendered yet, or that account failed)")
            tone = "offline" if report.worker_errors else "active"
            if report.worker_errors:
                detail = "; ".join(f"{k}: {v}"
                                   for k, v in report.worker_errors.items())
                message += f". Could not reach: {detail}"
            self.notification.emit(message, tone)

        self._start(f"collect:{label}", work,
                    f"Collecting frames from {who}", ok)

    @Slot(str, str)
    def addAccount(self, label: str, token: str) -> None:
        """Add and VERIFY an account.

        Verification is a real network call, so it runs off-thread like
        everything else -- and an account that fails to verify is not
        added, because one that cannot render is worse than absent: it
        would sit in the fleet looking merely idle.
        """
        from blendfleet.accounts import Account

        account = Account(label=label.strip(), token=token.strip())
        try:
            self.store.validate(account)
        except Exception as e:      # noqa: BLE001 -- shown to the user
            self.notification.emit(str(e), "offline")
            return

        def work():
            return self.verifier(account.token)

        def ok(username) -> None:
            account.username = username
            account.verified = True
            self.store.add(account)
            self.store.save()
            self.logLine.emit(f"added account {account.label}", "active")
            if username:
                self.notification.emit(
                    f"Added {account.label} ({username})", "active")
            else:
                # The token is valid -- verification passed -- but Kaggle
                # had nothing owned by this account to read the handle
                # from. Said HERE, where it is fixable in one field,
                # rather than discovered later as a failed render.
                self.notification.emit(
                    f"Added {account.label}, but Kaggle did not reveal its "
                    "username -- the account owns no notebook or dataset to "
                    "read it from. Enter it on the Instances page, or this "
                    "account cannot render.", "warn")
            self._emit_state()
            self.refreshQuota()

        self._start(f"verify:{label}", work, f"Verifying {label}", ok)

    @Slot(str, str)
    def setUsername(self, label: str, username: str) -> None:
        """Set an account's Kaggle handle by hand.

        Kaggle exposes no "who am I" endpoint: the handle is recovered
        from the owner prefix of something the account owns. An account
        that has never created a notebook OR a dataset has nothing to read
        it from -- and that is a perfectly ordinary account, especially for
        a friend lending quota who has never written a notebook. Without
        this, such an account is added successfully and then fails every
        render with "could not determine username", which is a dead end.
        """
        username = username.strip().lstrip("@")
        if not username:
            self.notification.emit("Enter a Kaggle username.", "offline")
            return
        for account in self.store.list():
            if account.label == label:
                account.username = username
                self.store.save()
                self.logLine.emit(
                    f"{label}: username set to {username}", "active")
                self._emit_state()
                # Cross-checked against Kaggle where possible, off-thread.
                # A wrong handle here is not harmless: it surfaces much
                # later as "collaborator usernames don't exist", after an
                # upload has already been spent.
                self._verify_username(account, username)
                return
        self.notification.emit(f"No account labelled {label}.", "offline")

    def _verify_username(self, account, claimed: str) -> None:
        token = account.token

        def work():
            return self.fleet_factory([account]).client_factory(token).whoami()

        def ok(actual) -> None:
            if actual and actual != claimed:
                self.notification.emit(
                    f"Kaggle says that account is {actual!r}, not "
                    f"{claimed!r}. Sharing would fail with that name.",
                    "offline")
            else:
                self.notification.emit(
                    f"{account.label} is {claimed}", "active")

        def cannot_check(_message: str) -> None:
            # The usual reason is the one manual entry exists for: the
            # account owns nothing Kaggle can read a handle from. Not being
            # able to confirm is not the same as being wrong.
            self.notification.emit(
                f"{account.label} is {claimed} — Kaggle could not confirm "
                "it (this account owns nothing to read a handle from), so "
                "it will be used as typed.", "warn")

        self._start(f"whoami:{account.label}", work,
                    f"Checking {account.label}'s username", ok, cannot_check)

    @Slot(str)
    def removeAccount(self, label: str) -> None:
        self.store.remove(label)
        self.store.save()
        self.logLine.emit(f"removed account {label}", "warn")
        self._emit_state()

    # ---- live streaming ------------------------------------------------
    def _record_hardware(self, label: str, preflight: dict) -> None:
        """Keep what a PREFLIGHT line said, with the time it said it.

        `observed_at` is the whole honesty mechanism (see
        instance_state.InstanceSnapshot): Kaggle reallocates, so a stored
        snapshot is only ever "what this account got at this time", never
        what it will get next. Saved immediately -- an app that is closed
        before the next save would otherwise lose the observation it just
        spent a minute of quota to make.

        Never raises: failing to CACHE hardware must not break the run
        that reported it.
        """
        try:
            account = next((a for a in self.store.list()
                            if a.label == label), None)
            models = [m for m in (preflight.get("gpu_names") or []) if m]
            snapshot = InstanceSnapshot(
                username=(account.username if account else None),
                # A probe has no TELEMETRY lines, so the physical index is
                # not known here; position is the honest stand-in, and
                # mem_total stays 0 rather than being invented.
                gpus=[GpuSnapshot(index=i, mem_total=0, model=model)
                      for i, model in enumerate(models)],
                cpu_count=preflight.get("cpu"),
                ram_total=preflight.get("ram"),
                observed_at=time.time())
            self.instance_store.record(label, snapshot)
            self.instance_store.save()
        except Exception as e:      # noqa: BLE001
            self.logLine.emit(
                f"could not save {label}'s hardware reading: {e}", "warn")

    @Slot(str)
    def checkHardware(self, label: str) -> None:
        """What would Kaggle actually give this account right now?

        Kaggle's allocation is a lottery, not a setting -- the same account
        got 2x Tesla T4 one minute and no GPU the next. This spends about a
        minute of quota to find out, instead of a scene upload and a
        render.

        Answered through the same log stream a render uses, so the reading
        lands in exactly the same place on the page (see Fleet.
        check_hardware and notebook_builder.HARDWARE_REPORT -- the probe
        prints the identical PREFLIGHT format on purpose).
        """
        accounts = self.store.list()
        account = next((a for a in accounts if a.label == label), None)
        if account is None:
            self.notification.emit(
                f"There is no account called {label} to check.", "offline")
            return

        def work():
            return self.fleet_factory(accounts).check_hardware(label)

        def ok(slug: str) -> None:
            self.logLine.emit(
                f"{label}: asking Kaggle for a machine to see what it "
                "gives — about a minute", "active")
            self._stream_probe(account, str(slug))

        self._start(f"hwcheck:{label}", work, "Checking the hardware", ok)

    def _stream_probe(self, account, kernel_slug: str) -> None:
        """Stream one hardware probe until it reports, then stop.

        Uses the render path's own stream_progress, so a probe benefits
        from the same reconnect-on-drop behaviour -- and its PREFLIGHT line
        arrives on the same queue, which is why nothing downstream needs to
        know a probe happened at all.
        """
        self._stream_threads = [t for t in self._stream_threads if t.is_alive()]
        label = account.label

        def run():
            try:
                stream_progress(
                    account.token, kernel_slug.split("/", 1)[0],
                    kernel_slug.split("/", 1)[1],
                    lambda done, total: None,      # a probe renders nothing
                    on_hardware=lambda r: self._hardware_q.put((label, r)),
                    on_preflight=lambda r: self._preflight_q.put((label, r)))
            except Exception as e:      # noqa: BLE001
                # A probe that could not be watched is not a failed
                # account -- say what happened and leave it at that.
                self._notify_q.put((
                    f"Could not read {label}'s hardware check: {e}", "warn"))

        thread = threading.Thread(
            target=run, name=f"blendfleet-hwcheck-{label}", daemon=True)
        self._stream_threads.append(thread)
        thread.start()

    def _start_streams(self, state) -> None:
        """One SSE log stream per worker, for as long as it runs.

        Workers are matched to accounts by LABEL, never by position:
        zip(accounts, workers) mispairs the moment the two lists stop
        lining up, and streaming a kernel with the wrong person's token is
        both a privacy leak and a stream that simply 403s.
        """
        self._live = {}
        self._stream_threads = [t for t in self._stream_threads if t.is_alive()]
        by_label = {a.label: a for a in self.store.list()}
        for worker in (state.workers if state else []):
            account = by_label.get(worker.label)
            if account is None:
                continue        # removed mid-launch: no token, so no stream

            def run(account=account, worker=worker):
                label = account.label

                def progress(done, total):
                    self._progress_q.put((label, done, total))

                try:
                    stream_progress(
                        account.token, worker.username,
                        worker.kernel_slug.split("/", 1)[1], progress,
                        self._stop,
                        on_telemetry=lambda r: self._telemetry_q.put((label, r)),
                        on_hardware=lambda r: self._hardware_q.put((label, r)),
                        on_preflight=lambda r: self._preflight_q.put((label, r)))
                except Exception:
                    pass        # a dead stream must never kill the render

            thread = threading.Thread(target=run, daemon=True,
                                      name=f"blendfleet-stream-{worker.label}")
            self._stream_threads.append(thread)
            thread.start()

    def _slot(self, label: str) -> dict:
        return self._live.setdefault(label, {
            "phase": "", "framesDone": 0, "framesTotal": 0,
            "gpus": {}, "cpuCount": None, "ramTotal": None, "preflight": None,
        })

    def _live_tick(self) -> None:
        """Drain what the stream threads collected, on the UI thread.

        Bounded per tick so a flood of telemetry cannot starve the loop.
        """
        changed = False
        for _ in range(200):
            try:
                label, done, total = self._progress_q.get_nowait()
            except queue.Empty:
                break
            slot = self._slot(label)
            slot["framesDone"], slot["framesTotal"] = done, total
            slot["phase"] = f"rendering · {done}/{total} frames"
            changed = True
        for _ in range(200):
            try:
                label, record = self._telemetry_q.get_nowait()
            except queue.Empty:
                break
            slot = self._slot(label)
            slot["gpus"][record["gpu"]] = {
                "index": record["gpu"],
                "util": record.get("util"),
                "memUsed": record.get("mem_used"),
                "memTotal": record.get("mem_total"),
            }
            # Telemetry only exists while Blender is running, so its
            # arrival is itself evidence the setup finished.
            if not slot["phase"]:
                slot["phase"] = "rendering"
            changed = True
        for _ in range(200):
            try:
                label, record = self._hardware_q.get_nowait()
            except queue.Empty:
                break
            slot = self._slot(label)
            if record.get("kind") == "cpu_ram":
                slot["cpuCount"] = record.get("cpu_count")
                slot["ramTotal"] = record.get("ram_total")
            # The hardware banner is cell 1; Blender is downloaded in cell
            # 2. Seeing the banner but no telemetry yet means setup.
            if not slot["phase"]:
                slot["phase"] = "installing Blender"
            changed = True
        for _ in range(200):
            try:
                label, record = self._preflight_q.get_nowait()
            except queue.Empty:
                break
            slot = self._slot(label)
            slot["preflight"] = record
            if not slot["phase"]:
                slot["phase"] = "checking hardware"
            # Persisted, not just shown live. Nothing wrote instance_store
            # in this UI, so the "last known hardware" line -- which
            # _state_payload has always read from it -- was permanently
            # empty; every observation was lost the moment the stream
            # ended. PREFLIGHT is the right thing to keep: it is the one
            # line that reports what Kaggle actually allocated.
            self._record_hardware(label, record)
            changed = True
        for _ in range(50):
            try:
                message, tone = self._notify_q.get_nowait()
            except queue.Empty:
                break
            self.notification.emit(message, tone)
        if changed:
            self._emit_state()

    def stop(self) -> None:
        """Wait for whatever is in flight, streams included. Called from the host window's
        closeEvent: a QThread still running when Qt destroys its QObject
        is the same class of bug the Qt UI documents at length."""
        self._poll_timer.stop()
        self._live_timer.stop()
        self._stop.set()        # tells the SSE threads to unwind
        for worker in list(self._workers.values()):
            try:
                if worker.isRunning():
                    worker.wait(5000)
            except RuntimeError:
                pass            # already finished and deleted
        self._workers.clear()
        # A daemon thread still inside SSL when the process tears down is
        # what produces "Fatal Python error: Aborted" -- daemon=True hides
        # that, it does not prevent it.
        for thread in self._stream_threads:
            thread.join(timeout=3.0)
        self._stream_threads = [t for t in self._stream_threads if t.is_alive()]


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
