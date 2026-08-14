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

from blendfleet import crash_log
from blendfleet.accounts import AccountStore
from blendfleet.assignment import estimate
from blendfleet.blender_versions import KNOWN_VERSIONS, validate_version
from blendfleet.fleet import _capped_stem, fingerprint_unreadable_entry
from blendfleet.instance_state import (GpuSnapshot, InstanceSnapshot,
                                       InstanceStore)
from blendfleet.kaggle_client import PENDING_STATES
from blendfleet.log_stream import stream_progress
from blendfleet.notebook_builder import RenderSettings
from blendfleet.platform_paths import log_dir, state_dir
from blendfleet.scenes import Scene, scenes_from_datasets
from blendfleet.settings import Settings
from blendfleet.ui.messages import explain

SECONDS_PER_FRAME_DEFAULT = 57.1     # measured: 1920x1080, 128spp, P100
POLL_INTERVAL_MS = 30_000            # real network calls: kernel status
LIVE_INTERVAL_MS = 2_000             # cheap: drain the in-memory queues

# How long the closing window waits for an in-flight worker before cutting
# it loose. Long enough for a Kaggle poll that is simply slow; short enough
# that closing the app never feels hung, since the wait blocks the UI
# thread. Past it, see _orphan.
_STOP_GRACE_MS = 5_000


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


# Workers that would not stop in time, kept alive on purpose. See _orphan.
_ORPHANED_WORKERS: list[_Worker] = []


def orphaned_workers() -> list[_Worker]:
    """Workers still running after stop() gave up waiting.

    Non-empty means this process cannot shut down through Python's normal
    finalisation without Qt aborting -- see web_main.main().
    """
    return list(_ORPHANED_WORKERS)


def _orphan(worker: _Worker) -> None:
    """Let a worker that will not stop outlive the app, instead of taking
    the app down with it.

    A poll is a Kaggle round-trip per account through kagglesdk, which
    exposes no timeout and no cancellation -- so "the user closed the
    window while a poll was in flight" is a thread that genuinely cannot
    be stopped, not a thread anyone forgot to join. Qt's answer to that is
    qFatal the moment it destroys the QThread, which is the abort this
    whole investigation started from.

    So the thread is cut loose instead: signals disconnected so it cannot
    call back into a half-torn-down Backend, unparented so Qt will not
    delete it as a child, and held here so Python will not collect it.
    Nothing destroys it, so ~QThread never runs, so there is no abort. The
    OS reclaims it when the process ends, moments later.
    """
    try:
        worker.succeeded.disconnect()
        worker.failed.disconnect()
        worker.finished.disconnect()
    except (RuntimeError, TypeError):
        pass                # nothing was connected, or C++ side already gone
    try:
        worker.setParent(None)
    except RuntimeError:
        pass
    _ORPHANED_WORKERS.append(worker)
    crash_log.record(
        f"a background worker did not stop within {_STOP_GRACE_MS}ms of the "
        "window closing (most likely a Kaggle request that had not "
        "answered yet). It has "
        "been cut loose rather than destroyed, so Qt will not abort; the "
        "process will exit without waiting for it.",
        critical=True)


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
    framePreview = Signal(str)      # one fetched frame, ready to show
    logLine = Signal(str, str)      # (message, tone)
    notification = Signal(str, str)
    healthChanged = Signal(str)
    busyChanged = Signal(str, bool)  # (action key, in flight)
    scenesChanged = Signal(str)     # {"scenes": [...], "errors": {label: why}}

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
        # Every _Worker whose run() has not returned yet.
        #
        # Separate from _workers because the two answer different
        # questions. _workers answers "is a call under this key already in
        # flight?" and is emptied by the succeeded/failed handler -- which
        # runs while run() is STILL ON THE STACK, since those signals are
        # emitted from inside run(). For the window between that emit and
        # run() returning, the thread is alive but no longer in _workers,
        # so stop() found nothing to wait for and Qt then destroyed a
        # running QThread. That is qFatal("QThread: Destroyed while thread
        # is still running") -> abort() -> the c0000409 the packaged app
        # died of. This set is emptied by `finished` instead, which Qt
        # emits only after run() has returned.
        self._running_workers: set[_Worker] = set()
        # What is on Kaggle right now, as far as this session knows:
        # {slug, blendName, sizeBytes, at}. Set only by syncDataset(), so
        # it is never a guess -- an empty value means we have not put this
        # scene up during this session, not that Kaggle has nothing.
        self._dataset: dict | None = None
        # label -> why, from the LAST prepare_dataset() call (syncDataset()
        # or a launch() that had to upload). None means no upload has run
        # this session -- never {}, which would read as "shared with
        # everyone" rather than "nothing recorded yet". Fleet.
        # unshared_accounts lives on a Fleet object this app rebuilds per
        # call and throws away, so it has to be copied out HERE, right
        # after the call that populated it, or it is lost the moment that
        # Fleet is garbage collected.
        self._unshared_accounts: dict[str, str] | None = None
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
        self._system_q: "queue.Queue[tuple[str, dict]]" = queue.Queue()
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
        self._running_workers.add(worker)
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
        # Connected BEFORE deleteLater so the set has already released the
        # worker by the time the C++ object goes; and holding the worker
        # in that set is also what keeps a strong Python reference to it
        # for the whole of run(), so it cannot be collected mid-flight.
        worker.finished.connect(lambda w=worker: self._running_workers.discard(w))
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

        Reads `load_jobs()` -- EVERY tracked job -- fresh off disk on every
        call, rather than a single cached FleetState. Several scenes can be
        rendering at once (Tasks 3-5), and a payload built from only the
        most recently launched or polled job would silently stop showing
        every OTHER one the moment a second render started. A fresh Fleet
        also means self.unreadable_jobs below is always this call's own
        read, never a stale one from whichever job happened to poll last.

        Deliberately includes `approximate: True` on the frame data. The
        notebook reports how many frames succeeded, not which, so a failed
        frame shifts every later cell for that account -- the UI has to be
        able to say so rather than presenting a green cell as proof.
        """
        fleet = self.fleet_factory(self.store.list())
        jobs = fleet.load_jobs()
        # "Most recent job" -- load()'s own definition -- kept as the
        # singular `job` key so the page (not ported to `jobs` until Task
        # 7) keeps working unchanged.
        newest = jobs[-1] if jobs else None

        # One entry per label, built from EVERY tracked job rather than
        # just `newest` -- an account rendering an older, still-active job
        # must still show that worker on its card. Jobs are visited oldest
        # first (load_jobs()'s own order), so a label that appears in more
        # than one job (finished once, launched again) ends up pointing at
        # its most recent job, which is the one still worth showing.
        worker_by_label = {}
        job_by_label = {}
        for job in jobs:
            for worker in job.workers:
                worker_by_label[worker.label] = worker
                job_by_label[worker.label] = job.job_id

        # The scene is uploaded ONCE, by one account, and every other
        # account reads it from there -- so exactly one instance is the
        # parent. Derived from the dataset slug ("<owner>/<name>") rather
        # than tracked separately, which means it cannot disagree with
        # the dataset actually being used.
        dataset_owner = (self._dataset or {}).get("slug", "").split("/", 1)[0]
        instances = []
        for account in self.store.list():
            worker = worker_by_label.get(account.label)
            instances.append({
                "label": account.label,
                "username": account.username,
                "verified": bool(account.verified),
                "revoked": bool(getattr(account, "revoked", False)),
                "owner": bool(account.username
                              and account.username == dataset_owner),
                # Which tracked job this account's worker belongs to, or
                # None for an idle account -- a real state, not a gap.
                "jobId": job_by_label.get(account.label),
                # Live only while a kernel runs; None means idle, which is
                # a real state and not an error.
                "worker": {
                    "state": worker.state,
                    "frames": list(worker.frames),
                    "framesDone": worker.frames_done,
                    "message": (worker.message
                                or self._failures.get(worker.label) or ""),
                    # Seconds this worker has been going, or took. Frozen
                    # once it finished -- the elapsed time of a completed
                    # render is a fact about the render, not about how
                    # long ago you ran it.
                    "elapsed": _elapsed(worker),
                    "finished": bool(worker.finished_at),
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
                "blend": newest.blend_name,
                "startFrame": newest.start_frame,
                "endFrame": newest.end_frame,
            } if newest else None,
            "jobs": [_job_payload(job) for job in jobs],
            "instances": instances,
            "dataset": self._dataset,
            # Sharing failures from the LAST upload only -- see
            # self._unshared_accounts' own comment in __init__. Explicit
            # about that scope here so the page cannot mistake a stale
            # report for something true of the CURRENT dataset.
            "unshared": {
                "accounts": self._unshared_accounts,
                "note": ("who the last scene upload could not be shared "
                         "with -- not a live check, and not necessarily "
                         "about the dataset in use right now"),
            } if self._unshared_accounts is not None else None,
            # Jobs whose record on disk could not be parsed at all. Never
            # erased (see Fleet.unreadable_jobs) because it may be the only
            # surviving trace of kernels still running and billing on
            # Kaggle that this app can no longer cancel or collect.
            "unreadableJobs": _unreadable_jobs_payload(fleet.unreadable_jobs),
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
            "ramUsed": slot["ramUsed"],
            "cpuPct": slot["cpuPct"],
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

    @Slot(result=str)
    def diagnostics(self) -> str:
        """Where this run's crash log lives.

        Surfaced in Settings because the alternative is reading a
        %APPDATA% path down a phone line to someone whose app just
        vanished -- which is exactly the situation this log exists for.
        """
        path = crash_log.current_log_path()
        return json.dumps({
            "logFile": str(path) if path is not None else "",
            "logDir": str(log_dir()),
        })

    @Slot(result=str)
    def blenderVersions(self) -> str:
        """The versions offered, and the one currently chosen.

        The list is a menu, not a gate -- an unlisted but well-formed
        version is accepted, because Blender releases far more often than
        this app does.
        """
        return json.dumps({"versions": list(KNOWN_VERSIONS),
                           "current": self.settings.blender_version})

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

    @Slot(int)
    @Slot(int, str)
    def previewFrame(self, frame: int, job_id: str = "") -> None:
        """Fetch ONE rendered frame and show it, without collecting the job.

        Looking at a frame should not mean choosing a folder and pulling
        forty megabytes of everybody's output. The notebook writes loose
        per-frame images alongside the archive, so exactly one file is
        downloaded -- about 2 MB for a 1080p PNG.

        Cached under the app's own state directory: clicking the same
        frame twice must not pay for it twice, and the cache is keyed by
        job id so a re-render of the same frame number is not served the
        previous run's picture.

        `job_id` (Task 7 fix round 1, IMPORTANT) scopes the search to ONE
        tracked job. Frame numbers are not unique across jobs -- two
        scenes both rendering frames 1-4 is the ordinary case -- and
        addressing by number alone used to resolve through `fleet.load()`
        ("the most recent job"), so clicking one scene's frame silently
        previewed a DIFFERENT scene's frame of the same number whenever
        that other scene happened to be the more recently launched one.
        An empty string falls back to that same "most recent" behaviour,
        unchanged, for any caller that still only knows a frame number.
        """
        fleet = self.fleet_factory(self.store.list())
        if job_id:
            state = next((j for j in fleet.load_jobs() if j.job_id == job_id),
                         None)
        else:
            state = fleet.load()
        if state is None:
            self.notification.emit(
                "There is no render job to preview a frame from.", "idle")
            return
        owner = next((w for w in state.workers if frame in w.frames), None)
        if owner is None:
            self.notification.emit(
                f"Frame {frame} was not assigned to any account in this job.",
                "idle")
            return
        account = next((a for a in self.store.list()
                        if a.label == owner.label
                        or a.username == owner.username), None)
        if account is None:
            self.notification.emit(
                f"Frame {frame} was rendered by {owner.username}, which is "
                "no longer a configured account — re-add it to preview or "
                "download that frame.", "offline")
            return

        cache = state_dir() / "previews" / state.job_id
        # The notebook names loose frames f_<4 digits>.<ext>; the extension
        # follows the render format, so both are tried rather than assuming
        # PNG and silently failing on a JPEG render.
        names = [f"f_{frame:04d}.png", f"f_{frame:04d}.jpg"]
        existing = next((cache / n for n in names if (cache / n).exists()), None)
        if existing is not None:
            self.framePreview.emit(json.dumps(
                {"frame": frame, "path": existing.as_uri(),
                 "label": owner.label}))
            return

        def work():
            client = fleet.client_factory(account.token)
            for name in names:
                got = client.fetch_one_output(owner.kernel_slug, name, cache)
                if got is not None:
                    return got
            return None

        def ok(path) -> None:
            if path is None:
                self.notification.emit(
                    f"Frame {frame} is not on Kaggle yet — {owner.username} "
                    "has not finished it, or the session's output has "
                    "already expired.", "idle")
                return
            self.framePreview.emit(json.dumps(
                {"frame": frame, "path": Path(path).as_uri(),
                 "label": owner.label}))

        self._start(f"preview:{frame}", work, f"Fetching frame {frame}", ok)

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

    @Slot()
    def scenes(self) -> None:
        """List every scene already on Kaggle -- across EVERY configured
        account, not just the first one.

        scenes.py's scenes_from_datasets() is a pure filter over whatever
        DatasetInfo values it is handed; it does not know which account
        anything came from, so every account's own list_datasets() is
        called here and the results are pooled BEFORE filtering/sorting --
        a scene can be owned by any configured account (Scene.owner
        exists precisely because of this).

        One account's own listing failing (revoked token, rate limit, a
        network blip) must never empty the WHOLE library -- the same
        discipline CollectReport.worker_errors already follows for
        collecting frames: that account's error is recorded in `errors`
        and every OTHER account's scenes are still returned. This is not
        best-effort by accident; a single `except` around the whole loop
        would let the one unreachable account hide every scene anyone
        else owns.
        """
        accounts = self.store.list()

        def work():
            try:
                client_factory = self.fleet_factory(accounts).client_factory
            except Exception as e:      # noqa: BLE001 -- turned into text
                # Could not even build a Fleet (e.g. a bad work_dir). Every
                # account fails identically, but each still gets its own
                # named entry rather than one bare exception aborting the
                # whole call and leaving `errors` looking empty.
                message = explain("Loading the scene library", e)
                return [], {a.label: message for a in accounts}
            datasets = []
            errors: dict[str, str] = {}
            for account in accounts:
                try:
                    datasets.extend(
                        client_factory(account.token).list_datasets())
                except Exception as e:      # noqa: BLE001
                    errors[account.label] = explain(
                        f"Listing {account.label}'s Kaggle datasets", e)
            return scenes_from_datasets(datasets), errors

        def ok(result) -> None:
            found, errors = result
            self.scenesChanged.emit(json.dumps({
                "scenes": [_scene_payload(s) for s in found],
                "errors": errors,
            }))

        self._start("scenes", work, "Loading the scene library", ok)

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
                   "minGpus": "min_gpus", "blenderVersion": "blender_version"}
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
        # Built here, on the UI thread, so `ok` below can still read
        # fleet.unshared_accounts once prepare_dataset() returns -- a
        # Fleet built and discarded INSIDE work() would take that dict
        # with it the moment the worker thread's closure goes out of
        # scope.
        fleet = self.fleet_factory(accounts)

        def work():
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
            # Copied out now -- see self._unshared_accounts' comment in
            # __init__ for why this is the only moment that is possible.
            self._unshared_accounts = dict(fleet.unshared_accounts)
            self.logLine.emit(f"dataset ready: {slug}", "active")
            self.notification.emit(f"Uploaded {blend.name} to {slug}", "active")
            self._emit_state()

        self._start("dataset", work, "Uploading the scene", ok)

    @Slot(str)
    def launch(self, options_json: str) -> None:
        """Start a render on chosen accounts (every FREE account when none
        are named).

        Refuses rather than guesses when the request cannot be honoured --
        no accounts, no file, a backwards frame range, an unknown label, or
        nobody free. The page shows the reason; it does not get to proceed
        with a default.
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
            blender_version=validate_version(
                options.get("blenderVersion") or self.settings.blender_version),
            min_gpus=self.settings.min_gpus)
        all_accounts = self.store.list()
        blend = self.blend
        # Built once, here, so both the free-accounts check below and
        # `ok` afterwards (which needs fleet.unshared_accounts) share the
        # exact same Fleet -- a second Fleet built inside work() would
        # re-read the same jobs file but throw away its own
        # unshared_accounts the moment that call returns.
        fleet = self.fleet_factory(all_accounts)

        # ABSENT "labels" used to mean "every configured account", because
        # only one job could ever be running. With several jobs possible
        # at once (Tasks 3-5), that default has to mean "whatever is free"
        # instead -- otherwise a second launch with nothing selected would
        # ask to render on an account the first launch is still using.
        #
        # `is None`, not `or` -- Fix round 1, Critical: an explicitly EMPTY
        # list ("labels": []) means every per-instance checkbox was
        # unticked (Task 7 adds exactly those), and `or` collapsed that
        # into the same case as the key being absent entirely, silently
        # widening "render on nobody" back out to "render on whatever is
        # free" -- the exact bug Fleet.launch's own `accounts=[]` guard
        # (fleet.py) exists to prevent, reopened one layer up because this
        # slot resolves accounts and calls fleet.launch() before that
        # guard ever sees the list. Both layers must agree: this refuses
        # up front, and Fleet.launch keeps its own refusal as the backstop
        # for every other caller.
        requested = options.get("labels")
        if requested is None:
            accounts = fleet.free_accounts()
            if not accounts:
                self.notification.emit(
                    "Every configured account is already rendering "
                    "something else. Choose specific accounts to render "
                    "on, wait for a job to finish, or cancel one first.",
                    "offline")
                return
        elif not requested:
            self.notification.emit(
                "No machines selected — tick at least one instance to "
                "render on.", "offline")
            return
        else:
            by_label = {a.label: a for a in all_accounts}
            unknown = [label for label in requested if label not in by_label]
            if unknown:
                self.notification.emit(
                    f"No account is configured with label(s): "
                    f"{', '.join(unknown)}. Nothing has been started -- "
                    "reselect accounts and try again.", "offline")
                return
            accounts = [by_label[label] for label in requested]

        owner = all_accounts[0].label

        # Reuse the dataset only when it is THIS scene. A slug left over
        # from a different .blend would render the wrong thing on somebody
        # else's quota, so the name has to match before we skip the
        # upload; fleet.launch verifies the content besides.
        prepared = (self._dataset["slug"]
                    if self._dataset
                    and self._dataset["blendName"] == blend.name else None)

        def work():
            return fleet.launch(
                blend, settings, start, end, dataset_slug=prepared,
                accounts=accounts,
                on_progress=lambda p: self.uploadProgress.emit(json.dumps({
                    "label": owner,
                    "stage": "uploading",
                    "uploaded": p.uploaded,
                    "total": p.total,
                })))

        def ok(state) -> None:
            self._last_state = state
            if prepared is None:
                # prepare_dataset() actually ran as part of THIS launch --
                # see self._unshared_accounts' comment in __init__ for why
                # this is the only moment it can be captured. Left alone
                # (not overwritten with an empty dict) when the dataset
                # was reused and no sharing was attempted this time.
                self._unshared_accounts = dict(fleet.unshared_accounts)
            self._start_streams(state)
            self.logLine.emit(
                f"render started on {len(accounts)} account(s)", "active")
            self.notification.emit("Render started", "active")
            self._emit_state()
            self.refreshQuota()

        self._start("launch", work, "Starting the render", ok)

    @Slot(str, str)
    def renderScene(self, slug: str, options_json: str) -> None:
        """Render a scene that is already on Kaggle -- no local .blend,
        and no re-upload.

        Goes straight through Fleet.launch_from_dataset, which confirms a
        .blend genuinely exists (by LISTING the dataset's real files --
        the scene library's own listing only ever GUESSES the filename,
        see Scene's own docstring) and RE-VERIFIES sharing for every
        account in this launch. Neither check is repeated or
        short-circuited here.

        Mirrors launch()'s own ABSENT-vs-EXPLICITLY-EMPTY contract for
        `options.labels` (see that slot's own long comment): absent means
        every free account, refused if none are free; an explicitly empty
        list means every checkbox was unticked on purpose and is refused
        outright, never silently widened back out to "everyone".
        """
        options = json.loads(options_json)
        start = int(options.get("startFrame", 1))
        end = int(options.get("endFrame", 1))
        if not self.store.list():
            self.notification.emit(
                "Add at least one Kaggle account before rendering.", "offline")
            return
        if end < start:
            self.notification.emit(
                f"End frame ({end}) is before the start frame ({start}).",
                "offline")
            return

        settings = RenderSettings(
            int(options.get("resX", 1920)), int(options.get("resY", 1080)),
            int(options.get("samples", 128)), options.get("format", "PNG"),
            blender_version=validate_version(
                options.get("blenderVersion") or self.settings.blender_version),
            min_gpus=self.settings.min_gpus)
        all_accounts = self.store.list()
        fleet = self.fleet_factory(all_accounts)

        # See launch()'s own comment on this exact pattern -- `is None`,
        # not `or`: an explicitly empty "labels": [] must never collapse
        # into "labels absent" and widen back out to every free account.
        requested = options.get("labels")
        if requested is None:
            accounts = fleet.free_accounts()
            if not accounts:
                self.notification.emit(
                    "Every configured account is already rendering "
                    "something else. Choose specific accounts to render "
                    "on, wait for a job to finish, or cancel one first.",
                    "offline")
                return
        elif not requested:
            self.notification.emit(
                "No machines selected — tick at least one instance to "
                "render on.", "offline")
            return
        else:
            by_label = {a.label: a for a in all_accounts}
            unknown = [label for label in requested if label not in by_label]
            if unknown:
                self.notification.emit(
                    f"No account is configured with label(s): "
                    f"{', '.join(unknown)}. Nothing has been started -- "
                    "reselect accounts and try again.", "offline")
                return
            accounts = [by_label[label] for label in requested]

        def work():
            return fleet.launch_from_dataset(slug, settings, start, end,
                                             accounts=accounts)

        def ok(state) -> None:
            self._last_state = state
            # Deliberately NOT copied into self._unshared_accounts, unlike
            # launch()'s own ok(): launch_from_dataset scopes sharing to
            # only the accounts in THIS render (its own docstring), so its
            # fleet.unshared_accounts is always {} on this path -- copying
            # that in would read as "the last upload reached everyone",
            # a positive claim nothing here actually checked for accounts
            # outside this render. Leaving it alone keeps whatever an
            # earlier prepare_dataset()/launch() call last recorded, which
            # is still an honest answer to "the last upload", just not
            # about this call.
            self._start_streams(state)
            self.logLine.emit(
                f"render started on {len(accounts)} account(s) from "
                f"{slug}", "active")
            self.notification.emit(f"Render started from {slug}", "active")
            self._emit_state()
            self.refreshQuota()

        self._start(f"launch-scene:{slug}", work, "Starting the render", ok)

    @Slot(str)
    def deleteScene(self, slug: str) -> None:
        """Permanently delete a Kaggle dataset -- using the OWNER's own
        token, never a friend's.

        Kaggle rejects a delete from any account other than the literal
        owner, even one this dataset has been explicitly shared with as a
        READER (see KaggleClient.delete_dataset's own docstring) -- read
        access and delete access are different permissions. The owner is
        resolved from `slug` itself ("owner/name"), the same way
        Fleet.launch_from_dataset resolves it, never guessed from fleet
        position -- a friend's token must never even be tried here.

        The irreversible confirmation -- naming the scene, its size, and
        that sharing accounts lose access -- happens in the page, before
        this is ever called; this slot trusts that already happened and
        does not ask again.

        Fix round 1 (Minor): refuses outright, like every other
        consequential action here (require_free's own pattern), while a
        tracked job still has a worker actively rendering FROM this
        dataset. Matched via `scene_key`, never via Scene.blend_name's own
        GUESSED filename (that class's docstring is explicit it may not
        be the real one): `stem` below is derived from `slug` the exact
        same way Fleet.launch_from_dataset derives it, so it can never
        disagree with the scene_key a job launched from THIS dataset was
        actually given.
        """
        accounts = self.store.list()
        owner_username = slug.split("/", 1)[0] if "/" in slug else ""
        owner = next((a for a in accounts if a.username == owner_username),
                     None)
        if owner is None:
            self.notification.emit(
                f"Cannot delete {slug!r} -- no configured account has the "
                f"Kaggle username {owner_username!r}, so BlendFleet has no "
                "token that could delete it. Nothing has been deleted. Add "
                "that account under Manage accounts…, or confirm its "
                "stored username matches what Kaggle reports (Instances -> "
                "Set username), then try again.", "offline")
            return

        dataset_name = slug.split("/", 1)[-1]
        stem = (dataset_name[: -len("-blend")]
                if dataset_name.endswith("-blend") else dataset_name)
        stem = _capped_stem(stem) or "scene"
        # PENDING_STATES, not ACTIVE_STATES: a kernel that has been pushed
        # but whose Kaggle session has not started yet reports
        # "not_started", which still holds this account exactly like
        # queued/running (see kaggle_client.PENDING_STATES,
        # Fleet.busy_labels()/require_free(), which this mirrors). This is
        # the one irreversible action here -- Kaggle has no trash for a
        # deleted dataset -- so it cannot afford the narrower predicate
        # that busy_labels()/require_free() themselves moved off of.
        rendering = sorted({
            w.label for job in self.fleet_factory(accounts).load_jobs()
            if job.scene_key == stem
            for w in job.workers if w.state in PENDING_STATES})
        if rendering:
            self.notification.emit(
                f"Cannot delete {slug!r} -- {', '.join(rendering)} "
                f"{'is' if len(rendering) == 1 else 'are'} still rendering "
                "it right now. Deleting a scene mid-render can break that "
                "render for every account using it. Nothing has been "
                "deleted -- wait for it to finish, cancel it, or use "
                "'Stop tracking job' if Kaggle refuses to cancel, then "
                "try deleting again.", "offline")
            return

        def work():
            self.fleet_factory([owner]).client_factory(
                owner.token).delete_dataset(slug)

        def ok(_result) -> None:
            self.logLine.emit(f"deleted {slug}", "warn")
            self.notification.emit(f"Deleted {slug} from Kaggle.", "idle")
            # The library must stop showing what is now gone rather than
            # waiting for the user to stumble onto it stale.
            self.scenes()

        self._start(f"delete-scene:{slug}", work, f"Deleting {slug}", ok)

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

        Fix round 2: mirrors launch()'s own ABSENT-vs-EXPLICITLY-EMPTY
        distinction (see that slot's own comment) instead of collapsing
        both into "start everyone" with a bare `if not labels`. A warm
        machine spends quota from the moment it starts, so an empty
        selection silently widened back out to the whole fleet is the
        same class of harm the launch() Critical fixed, not a lesser one
        just because it warms machines instead of rendering. An EMPTY
        STRING (no argument content at all -- today's "start every
        configured account" button's own call, unchanged) means absent;
        a non-empty string that decodes to `[]` means the caller
        explicitly asked for nobody and is refused.
        """
        requested = json.loads(labels_json) if labels_json else None
        if requested is None:
            labels = [a.label for a in self.store.list()]
        elif not requested:
            self.notification.emit(
                "No machines selected — tick at least one instance to "
                "start.", "offline")
            return
        else:
            labels = requested
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
        settings = RenderSettings(
            1920, 1080, 128,
            blender_version=validate_version(self.settings.blender_version),
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
        """Give work to machines that are already warm -- every warm
        machine, or a chosen subset of them.

        Publishes a job descriptor the running workers pick up on their
        next poll, instead of pushing new kernels. No setup cost, and no
        second session per account.

        Fix round 2: `options.get("labels")` used to be silently ignored
        -- a parameter that LOOKED respected (launch() reads the same key
        out of the same shape of `options`) but was not, which is worse
        than not having it, since a caller relying on it to scope a job
        would have it sent to every warm machine instead with no warning
        at all. Honoured now, with the same absent-vs-explicitly-empty
        distinction as launch()/startInstances(): absent means every warm
        machine (today's only caller, app.js's renderOptions(), never
        sends this key, so that caller is unaffected); an explicitly
        empty list refuses; a named machine that is not currently warm
        refuses by name rather than silently sending to fewer machines
        than asked.
        """
        options = json.loads(options_json)
        start = int(options.get("startFrame", 1))
        end = int(options.get("endFrame", 1))
        if end < start:
            self.notification.emit(
                f"End frame ({end}) is before the start frame ({start}).",
                "offline")
            return
        all_warm = [w.label for w in (self._last_state.workers
                                      if self._last_state else [])]
        if not all_warm:
            self.notification.emit(
                "No warm machines — start some first, or use Render across "
                "fleet to push a one-shot job.", "offline")
            return

        requested = options.get("labels")
        if requested is None:
            warm = all_warm
        elif not requested:
            self.notification.emit(
                "No machines selected — tick at least one warm instance "
                "to send this job to.", "offline")
            return
        else:
            cold_or_unknown = [label for label in requested
                               if label not in all_warm]
            if cold_or_unknown:
                self.notification.emit(
                    f"Not currently warm: {', '.join(cold_or_unknown)}. "
                    "Nothing has been sent -- start them first, or "
                    "reselect only warm machines.", "offline")
                return
            warm = requested

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

    @Slot(str)
    def cancelJob(self, job_id: str) -> None:
        """Stop every account rendering job `job_id`, leaving every OTHER
        tracked job's kernels running untouched -- the per-job counterpart
        to cancelAll(), built on Fleet.cancel_job() (must-fix 1).

        Fleet.cancel_job() was built in Task 5 for exactly this and had
        ZERO production callers until now: the page's per-job Cancel
        button was instead wired to one cancelInstance() call per account
        in the job, which only ever reaches Fleet.cancel_worker() ->
        load()'s single newest job. Cancelling any OLDER of two live
        scenes therefore cancelled nothing at all and reported
        "acct0 had already stopped." while that account's kernel kept
        running and billing -- the worst outcome this app can produce.
        Reports per-CancelResult outcomes exactly like cancelAll() above,
        so a cancel that silently failed for one account is never
        indistinguishable from one that actually stopped it.
        """
        accounts = self.store.list()

        def work():
            return self.fleet_factory(accounts).cancel_job(job_id)

        def ok(results) -> None:
            results = list(results or [])
            failed = [r for r in results if not r.ok]
            if not results:
                self.notification.emit("Nothing to cancel.", "idle")
            elif failed:
                detail = "; ".join(f"{r.label}: {r.error}" for r in failed)
                self.notification.emit(
                    f"{len(failed)} of {len(results)} did NOT stop and may "
                    f"still be spending quota — {detail}. Stop them by hand "
                    f"at kaggle.com.", "offline")
            else:
                self.notification.emit(
                    f"Cancelled {len(results)} account(s)", "idle")
            self.poll()

        self._start(f"cancel-job:{job_id}", work, "Cancelling the render", ok)

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

    @Slot(int, str)
    def forgetUnreadableJob(self, index: int, fingerprint: str) -> None:
        """Acknowledge ONE entry from `unreadableJobs`, so a warning the
        user has already resolved by hand at kaggle.com does not sit on
        the page forever with no way to clear it (Fix round 1,
        Important 3).

        `index` is that entry's position in the `unreadableJobs` list the
        payload just handed the page -- see _unreadable_jobs_payload()'s
        own docstring for why position, not job_id, is the stable key
        here. `fingerprint` is that SAME entry's `unreadableJobs[i].
        fingerprint` from that same payload (Fix round 2): save_jobs()
        writes parsed jobs first and unreadable entries last, so a
        DIFFERENT job going unreadable between the page being drawn and
        this being clicked can shift every later unreadable entry's
        position by one -- `index` alone could then silently forget the
        WRONG record. Fleet.forget_unreadable() refuses (via
        UnreadableJobChanged) when the fingerprint no longer matches
        whatever is actually at `index` right now, and that refusal
        reaches the page as a normal, named failure through the usual
        `_start`/`explain` path below, not a silent no-op.

        Exactly like forgetJob(), this is NOT a cancel: whatever the raw
        entry might have been tracking (if anything) keeps running on
        Kaggle and keeps spending quota. All this does is stop this app
        from being able to warn about it -- said here as plainly as
        forgetJob() already says it for a parsed job.
        """
        accounts = self.store.list()

        def work():
            return self.fleet_factory(accounts).forget_unreadable(
                index, fingerprint)

        def ok(entry) -> None:
            if entry is None:
                self.notification.emit(
                    "That record was already gone — nothing to forget.",
                    "idle")
            else:
                self.notification.emit(
                    "Stopped tracking that unreadable record. It was NOT "
                    "cancelled — if it named any kernels and they are "
                    "still running, they keep spending quota. Stop them "
                    "by hand at kaggle.com.", "warn")
                self.logLine.emit(
                    "stopped tracking an unreadable job record — not "
                    "cancelled", "warn")
            self._emit_state()

        self._start(f"forget-unreadable:{index}", work,
                    "Forgetting that record", ok)

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
    @Slot(str, str)
    def collect(self, label: str = "", job_id: str = "") -> None:
        """Download rendered frames from ONE job -- named explicitly by
        `job_id`, inferred from `label` (whichever job that account
        belongs to), or -- with neither given -- the most recent tracked
        job.

        Fix round 1 (Important 1+2, Minors 1+2): this used to load and
        collect from EVERY tracked job with no filter on state or age,
        merging their CollectReports with plain dict.update(). Neither
        half of that held up: `load_jobs()` is never pruned except one
        job at a time via forget_job(), so a fleet-wide button grew to
        re-download every job this app had EVER tracked, including ones
        still queued or running; and dict.update() silently drops one
        job's worker_errors behind another's for the same label (measured:
        an old job's "could not reach user_0" vanished behind a newer
        job's "token revoked"), which CollectReport.worker_errors' own
        docstring says must never happen. Scoping to exactly ONE job below
        removes both problems by construction rather than by merging
        better -- this is exactly `Fleet.load()`'s own pre-Task-6 answer
        (the newest job) when neither `label` nor `job_id` narrows it.
        `label` continues to search every tracked job, but takes the LAST
        (most recent) match, not the first (must-fix 4): the job list is
        append-only, so once an account has rendered twice, `label` names
        TWO jobs, not one -- the claim that "an account belongs to at
        most one job at a time, so that search is never ambiguous" stopped
        being true the moment a second job could be tracked at all, and
        searching oldest-first silently collected the OLDER scene's
        frames while the notification named the account as if nothing was
        wrong. `job_id` is wired to Task 7's per-job collect button, which
        is the right way to reach a specific older job on purpose -- a
        fleet-wide button should not guess.
        """
        from PySide6.QtWidgets import QFileDialog
        from blendfleet.collector import collect as collect_frames

        destination = QFileDialog.getExistingDirectory(None, "Save frames to")
        if not destination:
            return
        accounts = self.store.list()
        who = label or "the fleet"

        def work():
            fleet = self.fleet_factory(accounts)
            jobs = fleet.load_jobs()
            if job_id:
                job = next((j for j in jobs if j.job_id == job_id), None)
            elif label:
                # reversed(): the NEWEST job this label appears in, not
                # the oldest (must-fix 4) -- load_jobs() is oldest-first,
                # so a plain forward search over an append-only list
                # always finds the FIRST job an account ever rendered,
                # never the one it is rendering now.
                job = next((j for j in reversed(jobs)
                           if any(w.label == label for w in j.workers)),
                          None)
            else:
                job = jobs[-1] if jobs else None
            if job is None:
                return None
            return collect_frames(
                job, accounts, fleet.client_factory, Path(destination),
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
                        # Read directly for the same reason as the two
                        # above: a getattr default would turn a rename
                        # into a permanent, plausible-looking 0 B/s.
                        "rate": p.rate_bps,
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

        # Fix round 2: the busy key is BUTTON identity, not call identity.
        # `job_id` was folded into this key alongside `label`, but app.js
        # (btn-collect's own disable/re-enable, keyed by an EXACT match on
        # "collect:") only ever sends "" for both on the fleet-wide button
        # -- so the key it now saw was "collect::", which nothing in that
        # map matches, and the button never disabled while a collect ran.
        # A second click then re-opened the folder picker with the first
        # collect still in flight. `job_id` never needs to be part of this
        # key: collect() is scoped to one job either way, and the UI has
        # exactly one button per label (never per job), so `label` alone
        # is the right granularity -- exactly as it was before job_id
        # existed.
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
                        on_system=lambda r: self._system_q.put((label, r)),
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
            # Live system memory, distinct from ramTotal (which is the
            # machine's size, reported once). None until the first
            # SYSTEM line -- never 0, which would read as "no memory
            # in use" rather than "not measured yet".
            "ramUsed": None, "cpuPct": None,
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
                label, record = self._system_q.get_nowait()
            except queue.Empty:
                break
            slot = self._slot(label)
            # Bytes on the wire, GB at the edge -- the same discipline the
            # GPU rows already follow with MiB.
            slot["ramUsed"] = record.get("ram_used")
            slot["cpuPct"] = record.get("cpu_pct")
            if slot["ramTotal"] is None and record.get("ram_total"):
                slot["ramTotal"] = record["ram_total"] / (1024 ** 3)
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
        # _running_workers first, and it is the one that matters: see its
        # comment in __init__ for why _workers alone was empty at exactly
        # the moment this needed it not to be. _workers is still drained
        # too, so a worker that somehow never reached `finished` is not
        # skipped just because the set had let go of it.
        pending = list(self._running_workers)
        pending += [w for w in self._workers.values() if w not in self._running_workers]
        for worker in pending:
            try:
                if not worker.wait(_STOP_GRACE_MS):
                    _orphan(worker)
            except RuntimeError:
                pass            # already finished and deleted
        self._workers.clear()
        self._running_workers.clear()
        # A daemon thread still inside SSL when the process tears down is
        # what produces "Fatal Python error: Aborted" -- daemon=True hides
        # that, it does not prevent it.
        for thread in self._stream_threads:
            thread.join(timeout=3.0)
        self._stream_threads = [t for t in self._stream_threads if t.is_alive()]


def _elapsed(worker) -> float | None:
    """Seconds this worker has been running, or took in total.

    None when the start was never recorded -- a job launched by a build
    before started_at existed, whose state file has 0.0. Returning
    time.time() - 0.0 there would report a fifty-six year render, which
    is the kind of number that makes a user distrust every other number
    on the page.
    """
    started = getattr(worker, "started_at", 0.0) or 0.0
    if not started:
        return None
    finished = getattr(worker, "finished_at", 0.0) or 0.0
    return (finished or time.time()) - started


def _job_payload(job) -> dict:
    """One tracked job, for the `jobs` list.

    `elapsed`/`finished` mirror `_elapsed`'s own contract at the JOB level:
    frozen once every worker has reached a terminal state, rather than
    recomputed from "now" at display time, or a finished job would keep
    ageing every time the dashboard repainted.
    """
    finished = bool(job.workers) and all(w.finished_at for w in job.workers)
    started = getattr(job, "started_at", 0.0) or 0.0
    if not started:
        elapsed = None      # never recorded -- see _elapsed's own docstring
    elif finished:
        elapsed = max(w.finished_at for w in job.workers) - started
    else:
        elapsed = time.time() - started
    return {
        "jobId": job.job_id,
        "scene": job.scene_key,
        "blend": job.blend_name,
        "startFrame": job.start_frame,
        "endFrame": job.end_frame,
        "labels": [w.label for w in job.workers],
        "elapsed": elapsed,
        "finished": finished,
    }


def _unreadable_jobs_payload(raw_entries: list) -> list[dict]:
    """Job records `load_jobs()` could not parse, translated into
    something a person can act on.

    Never silently absent: a job this app cannot read may still have
    kernels running on Kaggle that it can no longer cancel or collect (see
    Fleet.unreadable_jobs). Names the kernels when the raw entry still has
    them to recover -- most likely for a per-job parse failure, where only
    one field was malformed -- and says plainly when it cannot, rather
    than guessing a slug that might not exist.

    `index` is this entry's position in `Fleet.unreadable_jobs` --
    forgetUnreadableJob()'s own key, since a job_id may be missing (a
    whole-file JSON failure has no fields to read at all) or, being only
    32 bits of uuid4, could collide; position is the one identifier that
    is always present and never ambiguous (Fix round 1, Critical fix's
    sibling problem, Important 3).

    `fingerprint` (Fix round 2) is forgetUnreadableJob()'s staleness
    guard: `index` alone can point at the WRONG entry by the time a
    click reaches the backend, if some OTHER job went unreadable in
    between and shifted every later position by one (save_jobs() writes
    unreadable entries last -- see UnreadableJobChanged's own docstring
    in fleet.py). The page must send this back unchanged alongside
    `index`; Fleet.forget_unreadable() refuses rather than guessing if it
    no longer matches what is actually at that position.

    Fix round 1, Minor 1: a per-job parse failure (as opposed to a
    whole-file one) usually still HAS `job_id`/`blend_name` -- discarding
    them made every such entry read as the identical generic sentence,
    even with several unreadable jobs on screen at once. Used here when
    present; a kernel slug is also turned into the actual kaggle.com/code
    URL, since that is the page the user has to open to act on it, not
    just the slug this app happens to store internally.
    """
    payload = []
    for index, entry in enumerate(raw_entries):
        kernels = []
        job_id = None
        blend_name = None
        if isinstance(entry, dict):
            job_id = entry.get("job_id")
            blend_name = entry.get("blend_name")
            for w in entry.get("workers") or []:
                if isinstance(w, dict):
                    slug = w.get("kernel_slug")
                    if slug:
                        kernels.append(slug)
        urls = [f"https://www.kaggle.com/code/{slug}" for slug in kernels]
        if blend_name and job_id:
            which = f"the job rendering {blend_name!r} ({job_id})"
        elif job_id:
            which = f"job {job_id}"
        else:
            which = "a tracked job"
        if kernels:
            message = (
                f"BlendFleet could not read the record for {which}, so it "
                "can no longer track, cancel, or collect it here. If any "
                f"of these kernels are still running, they keep spending "
                f"quota: {', '.join(urls)}. Check kaggle.com and stop "
                "them by hand, then use \"Forget this record\" to clear "
                "this warning.")
        else:
            message = (
                f"BlendFleet could not read the record for {which} at "
                "all, so it cannot say which kernels (if any) belong to "
                "it. If a render is still running, it will not show up "
                "here -- check kaggle.com for anything still active, "
                "then use \"Forget this record\" to clear this warning.")
        payload.append({
            "index": index, "jobId": job_id, "blend": blend_name,
            "kernels": kernels, "kernelUrls": urls, "message": message,
            "fingerprint": fingerprint_unreadable_entry(entry),
        })
    return payload


def _scene_payload(scene: Scene) -> dict:
    """One Scene, ready for the page.

    `updated` is an ISO string when Kaggle reported one, and exactly
    `None` when it did not (Scene.updated: datetime | None) -- never a
    default date standing in for "we don't know". `blendName` carries
    Scene.blend_name's own GUESSED filename verbatim: this payload must
    never dress it up as confirmed, because nothing on this path has
    listed the dataset's real files -- that only happens inside
    Fleet.launch_from_dataset, immediately before rendering it.
    """
    return {
        "slug": scene.slug,
        "name": scene.name,
        "owner": scene.owner,
        "sizeBytes": scene.size_bytes,
        "updated": scene.updated.isoformat() if scene.updated else None,
        "blendName": scene.blend_name,
    }


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
