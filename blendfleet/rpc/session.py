"""The page's whole backend, without Qt.

PORTED FROM ui/bridge.py, deliberately and visibly: same method names,
same payload keys, same honesty rules. What changed is only how it
answers -- Emitters instead of Signals, threads instead of QThread, a
sleeping timer instead of QTimer -- because this runs headless beside an
Electron shell, talking newline-JSON over a pipe (see rpc/protocol.py).

WHY A COPY AND NOT A SHARED BASE. Sharing would mean editing bridge.py,
and bridge.py is what the app currently being used for real renders
depends on. A subtle threading regression there is worse than visible,
temporary duplication here -- see
docs/superpowers/specs/2026-08-16-electron-shell-design.md.
tests/test_rpc_session.py asserts the two produce byte-identical
payloads, which is what stops them drifting while both exist. They
collapse into one adapter when the Qt shell retires.

Everything below -- every comment explaining why a reading is labelled
the way it is, why a bar is not drawn, why a count carries its age -- is
bridge.py's, and stays true here.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Callable

from blendfleet.rpc.emitter import Emitter, RepeatingTimer, Worker

from blendfleet import crash_log
from blendfleet.accounts import AccountStore
from blendfleet.assignment import estimate
from blendfleet.blender_versions import KNOWN_VERSIONS, validate_version
from blendfleet.fleet import (_capped_stem, _tokenless,
                              fingerprint_unreadable_entry)
from blendfleet.instance_state import (GpuSnapshot, InstanceSnapshot,
                                       InstanceStore)
from blendfleet.kaggle_client import PENDING_STATES, TERMINAL_STATES
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


# The off-thread call, from rpc.emitter. Named _Worker here because
# _orphan and stop() below are bridge.py's, unchanged, and refer to it
# by that name.
_Worker = Worker


# Workers that would not stop in time, kept alive on purpose. See _orphan.
_ORPHANED_WORKERS: list[_Worker] = []


def orphaned_workers() -> list[_Worker]:
    """Workers still running after stop() gave up waiting.

    Non-empty means a Kaggle request outlived the shell. Nothing here
    has to be done about it -- these are daemon threads -- but the
    sidecar records it, because a poll that never returned is worth
    knowing about when a user reports a hang.
    """
    return list(_ORPHANED_WORKERS)


def _orphan(worker: _Worker) -> None:
    """Let a worker that will not stop outlive the app, instead of taking
    the app down with it.

    A poll is a Kaggle round-trip per account through kagglesdk, which
    exposes no timeout and no cancellation -- so "the shell went away
    while a poll was in flight" is a thread that genuinely cannot be
    stopped, not a thread anyone forgot to join.

    So it is cut loose: handlers disconnected so it cannot call back into
    a half-torn-down Session, and held here so nothing collects it
    mid-request. It is a daemon thread and cannot keep the process alive;
    the OS reclaims it when the process ends, moments later. (The Qt
    adapter has to do more -- destroying a running QThread is a qFatal
    abort -- which is why that version of this says more.)
    """
    try:
        worker.succeeded.disconnect()
        worker.failed.disconnect()
        worker.finished.disconnect()
    except (RuntimeError, TypeError):
        pass                # nothing was connected, or C++ side already gone
    _ORPHANED_WORKERS.append(worker)
    crash_log.record(
        f"a background worker did not stop within {_STOP_GRACE_MS}ms of the "
        "window closing (most likely a Kaggle request that had not "
        "answered yet). It has "
        "been cut loose rather than destroyed, so Qt will not abort; the "
        "process will exit without waiting for it.",
        critical=True)


class Session:
    """Everything the page can ask for, and everything it is told.

    The Qt adapter is registered on a QWebChannel and the page reaches it
    as `backend`. This one is reached the same way from the page's point
    of view, but the route is different: rpc.__main__ dispatches a line of
    JSON to a method here, and electron/preload.js rebuilds the same
    `window.backend` shape on the other end. The page cannot tell them
    apart, which is the whole design.
    """

    # ---- Python -> JS -------------------------------------------------












    # Every render THIS app has tracked, newest first, each carrying what
    # is known about whether Kaggle still has its output. Emitted twice per
    # refresh on purpose: once instantly from disk (every row "unchecked"),
    # then again once the background availability pass has answered. See
    # outputs() / checkOutputs().

    # One finished collect, so the row that started it can say where the
    # zip landed. Separate from downloadProgress because it is a RESULT,
    # not a reading -- progress keeps flowing on the existing channel.


    def __init__(self, store: AccountStore, fleet_factory, verifier,
                 settings: Settings, parent=None) -> None:
        # Every event the page can receive. Instance attributes rather
        # than class ones (Qt's Signals were class-level descriptors), so
        # two Sessions in one process -- which is what the test suite
        # does -- cannot share handlers.
        self.stateChanged = Emitter("stateChanged")  # the whole fleet state, as JSON
        self.accountsChanged = Emitter("accountsChanged")
        self.settingsChanged = Emitter("settingsChanged")
        self.telemetry = Emitter("telemetry")  # one GPU sample
        self.uploadProgress = Emitter("uploadProgress")
        self.downloadProgress = Emitter("downloadProgress")
        self.framePreview = Emitter("framePreview")  # one fetched frame, ready to show
        self.logLine = Emitter("logLine")  # (message, tone)
        self.notification = Emitter("notification")
        self.healthChanged = Emitter("healthChanged")
        self.busyChanged = Emitter("busyChanged")  # (action key, in flight)
        self.scenesChanged = Emitter("scenesChanged")  # {"scenes": [...], "errors": {label: why}}
        self.outputsChanged = Emitter("outputsChanged")
        self.collectFinished = Emitter("collectFinished")
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
        # job_id -> what checkOutputs() last learned about that render's
        # output on Kaggle. A job absent from here has NOT been checked,
        # which is a third state the outputs list has to show as itself:
        # neither "still there" nor "gone". Never pre-filled with a
        # default, because a default here would be a fabricated reading.
        self._output_availability: dict[str, dict] = {}

        # Everything the user is told, kept where it can be read back.
        #
        # A sharing failure was reported from the field and there was
        # nothing about it anywhere in %APPDATA%\BlendFleet\logs -- the log
        # only ever carried Qt messages and crashes, while the app's own
        # errors lived in a toast that fades after a few seconds. Connected
        # to the signals rather than added at each emit site so a message
        # added later cannot forget to do this.
        #
        # Bound methods, NOT lambdas, and unlike the Qt adapter these
        # DO make the Session reference itself: an Emitter holds its
        # handlers in a plain list, where PySide holds a QObject
        # receiver's bound method weakly. So a Session is freed by a
        # collection pass rather than the moment its last reference goes.
        # That is survivable precisely because it cannot be silent --
        # stop() cancels the timers explicitly, and the sidecar calls it
        # when the pipe closes, so an abandoned Session cannot go on
        # polling Kaggle and rewriting fleet.json while nobody watches.
        self._last_user_message: tuple[str, str] | None = None
        self.notification.connect(self._note_notification)
        self.logLine.connect(self._note_log_line)

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
        # Complete live frame previews, reassembled from the chunked THUMB
        # lines by log_stream.ThumbnailAssembler on the stream thread.
        # Queued like everything else: a QWebChannel payload built from a
        # stream thread is the crash class this app spent a release
        # getting rid of.
        self._thumb_q: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        # Notifications raised BY a stream thread (a hardware check
        # that could not be watched). Queued like everything else so
        # the signal is emitted on the UI thread, never from the
        # thread that noticed.
        self._notify_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._preflight_q: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        # label -> what we have seen live this run. Cleared per launch.
        self._live: dict[str, dict] = {}
        # label -> the frame count last written to the jobs file, so a tick
        # that reports the same number again writes nothing. See
        # _persist_progress for why that matters.
        self._persisted_done: dict[str, int] = {}
        # label -> the live stream thread currently watching it. Separate
        # from _stream_threads (which is a flat list used only for joining
        # at shutdown) because resuming has to answer a question that list
        # cannot: "does THIS label already have a stream?" Starting a
        # second one for the same worker would double every PROGRESS line
        # it reports and open a second Kaggle connection for no gain.
        self._stream_by_label: dict[str, threading.Thread] = {}
        # Labels whose stream was resumed at startup and has not yet
        # reported anything. While a label is in here the card is told it
        # is reconnecting, so a persisted frame count is never presented
        # as a live one. See _resume_streams and ready().
        self._resumed_labels: set[str] = set()
        self._resumed = False

        # A status poll is infrequent and costs a network call per account;
        # the live drain is cheap and purely in-memory. Two timers, two
        # rates -- the Qt UI's own split, for the same reasons.
        self._poll_timer = RepeatingTimer(POLL_INTERVAL_MS, self.poll,
                                          "poll")
        self._poll_timer.start()
        self._live_timer = RepeatingTimer(LIVE_INTERVAL_MS, self._live_tick,
                                          "live")
        self._live_timer.start()

    # ---- helpers ------------------------------------------------------
    def _scrub(self, text: str) -> str:
        """`text` with any configured account's token masked.

        Applied to everything this class writes to the diagnostic log.
        Messages here quote Kaggle's own errors and, now, whole tracebacks
        -- and this is a file the user is asked to send on when something
        goes wrong, so it must never be the thing that costs them a
        credential.
        """
        return _tokenless(text, self.store.list())

    def _note_notification(self, message: str, tone: str) -> None:
        self._note_user_message("notification", message, tone)

    def _note_log_line(self, message: str, tone: str) -> None:
        self._note_user_message("log", message, tone)

    def _note_user_message(self, kind: str, message: str, tone: str) -> None:
        """Record what the user was just told.

        An "offline" tone is this app's error tone -- the message names
        something that FAILED -- so it is written as critical, i.e. it
        survives the log's routine-message cap alongside crashes and fatal
        Qt lines. Every other tone is ordinary narration and is written at
        normal level.

        Consecutive duplicates are dropped: the 30-second poll failing
        while the network is down emits the identical sentence every 30
        seconds, and a log filled with one repeated line is how the fault
        that matters gets buried.
        """
        if (message, tone) == self._last_user_message:
            return
        self._last_user_message = (message, tone)
        crash_log.record(f"{kind} [{tone}] {self._scrub(message)}",
                         critical=(tone == "offline"))

    def _start(self, key: str, fn, action: str, on_ok, on_fail=None) -> bool:
        """Run `fn` off-thread under `key`, skipping if one is in flight.

        Skipping rather than queueing is deliberate and matches the Qt UI:
        a 30s poll timer firing again before a slow poll returns must not
        stack a second network call on top of the first.
        """
        if key in self._workers:
            return False
        worker = _Worker(fn, action, scrub=self._scrub)
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
        # Holding the worker in that set is what keeps a strong
        # reference to it for the whole of run(), so it cannot be
        # collected mid-flight.
        worker.finished.connect(lambda w=worker: self._running_workers.discard(w))
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
                    # How old that count is, in seconds, or None if it was
                    # never recorded. It is a SAVED reading, not a live
                    # one: after a restart it is whatever the last stream
                    # managed to write before the window closed, and the
                    # render has kept going since. Same rule as the cached
                    # hardware line -- a number without its age is a claim
                    # this app is not entitled to make.
                    "framesDoneAge": (
                        max(time.time() - worker.frames_done_at, 0.0)
                        if getattr(worker, "frames_done_at", 0.0) else None),
                    # WHAT KIND of number framesDone is -- see
                    # _frames_done_source. The page must never present a
                    # stopped render's leftover live reading as a count,
                    # and it cannot tell the difference from the number
                    # alone.
                    "framesDoneSource": _frames_done_source(worker),
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
                "live": self._live_payload(account.label, worker),
                # True only in the window between resuming this worker's
                # stream at startup and that stream reporting anything.
                # The card uses it to say "reconnecting, catching up"
                # rather than showing the saved frame count as though it
                # were current -- an empty card in that window reads as a
                # render that has stalled.
                "reconnecting": self._is_reconnecting(account.label, worker),
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

    def _is_reconnecting(self, label: str, worker=None) -> bool:
        """Is this label's resumed stream still catching up?

        Four conditions, all required. It was resumed at startup rather
        than launched here; nothing live has arrived yet (_slot clears the
        label the instant anything does); the thread is genuinely still
        trying; and the worker is not already known to have ENDED.

        The third matters because a resumed stream that died on its first
        connection would otherwise leave the card promising a reconnection
        that is never coming, which is a worse lie than the blank it
        replaced. When it drops out, the card falls back to the saved frame
        count carrying its age -- still honest, just older.

        The fourth is the one the field report was actually about. A stream
        attached to a kernel that has since finished can sit in
        log_stream's reconnect budget for minutes without ever delivering a
        line, so "reconnecting" outlived the render it referred to -- and
        by then the 30-second poll had already learned the kernel was
        complete. Once finished_at is stamped there is nothing left to
        catch up WITH, and the card must say finished instead.
        """
        if label not in self._resumed_labels:
            return False
        if worker is not None and getattr(worker, "finished_at", 0.0):
            return False
        thread = self._stream_by_label.get(label)
        return thread is not None and thread.is_alive()

    def _live_payload(self, label: str, worker=None) -> dict | None:
        """What the SSE stream has reported for `label` this run.

        None when nothing has arrived. That is the honest answer between
        renders: there is no idle session to poll, so "no live data" is a
        state, not a gap to paper over with the last run's numbers.

        ONCE THE SESSION HAS ENDED, the readings that only describe a
        RUNNING session are dropped: the phase, per-GPU load and memory,
        live system RAM and CPU. They were live once and they are facts
        about nothing now -- the machine they were measured on no longer
        exists. Leaving them in is the "everything is stuck" report: a card
        whose foot read "rendering · 15/15 frames" with GPU bars still up,
        for a job that ended hours ago, because `_live` is only ever
        cleared by a new launch. Worse after a restart, where the stream
        REPLAYS a finished kernel's whole log and rebuilds those readings
        from scratch.

        What survives is what stays true: how many frames the session
        finished, the hardware it actually got (preflight/CPU/RAM totals),
        and the last frame preview it sent. Those describe the render, not
        the moment.
        """
        slot = self._live.get(label)
        if not slot:
            return None
        ended = bool(worker is not None and getattr(worker, "finished_at", 0.0))
        return {
            "phase": "" if ended else slot["phase"],
            "framesDone": slot["framesDone"],
            "framesTotal": slot["framesTotal"],
            "gpus": ([] if ended
                     else [slot["gpus"][k] for k in sorted(slot["gpus"])]),
            "cpuCount": slot["cpuCount"],
            "ramTotal": slot["ramTotal"],
            "ramUsed": None if ended else slot["ramUsed"],
            "cpuPct": None if ended else slot["cpuPct"],
            "preflight": slot["preflight"],
            # A LOW-RESOLUTION preview of the newest frame this account
            # finished, or None -- never a placeholder. A frame with no
            # preview yet must show nothing, because an empty tile that
            # implied a frame had rendered would be a claim this app
            # cannot make.
            "thumb": slot["thumb"],
        }

    def _emit_state(self) -> None:
        self.stateChanged.emit(json.dumps(self._state_payload()))

    # ---- JS -> Python: reads ------------------------------------------
    def accounts(self) -> str:
        return json.dumps(self._accounts_payload())

    def state(self) -> str:
        return json.dumps(self._state_payload())

    def live_renders(self) -> dict:
        """What is still going, for the window to ask about on close.

        Plain Python rather than a Slot: the closing window is Qt, not
        the page, and by the time it asks the page may already be gone.

        "Live" is the same test the dashboard applies -- a worker with a
        state that is not terminal -- and NOT "not idle": a kernel Kaggle
        has accepted but not started answers `not_started`, which is
        neither active nor finished, and is exactly the case where
        closing would abandon a render that is about to start spending
        quota (see kaggle_client's own note on PENDING_STATES).
        """
        scenes: list[str] = []
        accounts = 0
        for job in self.fleet_factory(self.store.list()).load_jobs():
            live = [w for w in job.workers if w.state not in TERMINAL_STATES]
            if not live:
                continue
            scenes.append(job.scene_key)
            accounts += len(live)
        return {"scenes": scenes, "accounts": accounts}

    def ready(self) -> None:
        """The page has connected AND wired up every signal handler.

        Called once, as the very last line of app.js's QWebChannel
        callback. That position is the whole point of having this slot at
        all:

          - Backend.__init__ runs while the page is still loading. There is
            no channel yet, so anything emitted there reaches nobody.
          - state() is not safe either, even though the page calls it on
            connect: app.js calls `backend.state(...)` BEFORE it connects
            stateChanged, logLine and notification (three lines further
            down). A resume hooked there would emit into handlers that do
            not exist yet, and the "reconnecting" message would be lost
            exactly when it mattered.

        Called last, after every connect, there is nothing left to race:
        QWebChannel delivers the page's messages in the order they were
        sent, so by the time this runs, every signal this resume touches
        already has a listener.

        One-shot. The page only calls it on connect, but a reload must not
        start a second set of streams -- and _stream_worker's own duplicate
        guard is the backstop for that rather than the only defence.
        """
        if self._resumed:
            return
        self._resumed = True
        self._startup_check()

    def _startup_check(self) -> None:
        """Ask Kaggle what is still running BEFORE deciding anything.

        This used to call _resume_streams() straight away, and that is the
        bug the field report describes. _resume_streams reads each worker's
        state from the file on disk, which holds whatever was true when the
        app last CLOSED -- nothing had asked Kaggle since. So a fleet whose
        renders all finished overnight reopened as "Reconnecting to 5
        render(s) still running on Kaggle", opened an SSE stream per
        finished kernel, and showed five cards that could never advance
        because there was no progress left to send. The reasoning in
        _resume_streams was right all along; it was being handed a stale
        answer.

        Off the UI thread through _start (the established pattern here):
        this is one network round trip per tracked worker, and the window
        must draw and stay responsive while it happens. poll_all() then
        fans those round trips out across accounts on its own pool (see
        Fleet.POLL_FANOUT) -- taken one at a time they added up to the
        two-minute startup a user reported as "it takes a lot of time".
        Nothing about the threading contract here changes: this method
        still runs on ONE worker thread, still touches no Qt object from
        it, and still comes back through _start's succeeded/failed signals
        for the UI thread to act on.

        The Fleet is built HERE, on the UI thread, not inside work(): the
        completion handler has to read fleet.unreachable_workers, which is
        exactly how it tells "Kaggle says this is still running" apart from
        "Kaggle could not be asked" -- and a Fleet built inside the worker's
        closure goes out of scope with it. Same reason syncDataset() builds
        its own.
        """
        accounts = self.store.list()
        fleet = self.fleet_factory(accounts)
        try:
            jobs = fleet.load_jobs()
        except Exception as e:      # noqa: BLE001
            # A jobs file that cannot be read at all is already reported to
            # the page through unreadableJobs; failing here must not stop
            # the app starting.
            crash_log.record(self._scrub(
                "could not read the tracked jobs at startup, so no render "
                "already running on Kaggle will show live progress until "
                f"the next launch. {type(e).__name__}: {e}"), critical=True)
            return
        # What was pending when the app last closed -- the only workers
        # this check is about. Everything else was already finished on
        # disk, and the 30-second poll covers it from here.
        before = {(job.job_id, w.label): w.state
                  for job in jobs for w in job.workers}
        pending = sorted({label for (_job, label), state in before.items()
                          if state in PENDING_STATES})
        if not pending:
            # Nothing was mid-render, so there is nothing to reconnect to,
            # nothing to announce, and no reason to spend a network call
            # saying so. The routine poll takes over.
            return

        # Said BEFORE the call, not after: the check takes a moment per
        # account, and a fleet of cards showing last week's state with no
        # explanation is exactly how "everything is stuck" starts.
        self.notification.emit(
            f"Checking with Kaggle whether the {len(pending)} render(s) "
            f"tracked here are still running ({', '.join(pending)}). Until "
            "it answers, the cards below show what was true when BlendFleet "
            "last closed, which may be out of date. This spends no GPU "
            "quota and nothing needs restarting.", "idle")

        def work():
            return fleet.poll_all()

        def ok(polled) -> None:
            # dict(): unreachable_workers is rewritten in place by the next
            # poll_all(), and the 30-second timer can fire while this
            # handler is still running.
            self._finish_startup_check(before, polled,
                                       dict(fleet.unreachable_workers))

        def fail(message: str) -> None:
            # poll_all is tolerant per worker, so reaching here means the
            # whole check fell over (an unreadable state file, a Fleet that
            # could not be built) rather than one account failing.
            self._startup_unreachable(
                sorted(pending), {label: message for label in pending})

        self._start("startupCheck", work,
                    "Checking what is still running on Kaggle", ok, fail)

    def _finish_startup_check(self, before: dict, jobs: list,
                              unreachable: dict[str, str]) -> None:
        """Resume, and announce, from what Kaggle just said.

        Three disjoint buckets, in this priority order:

          - UNCHECKED first. poll_all() leaves an unreachable worker's
            state exactly as it was, so an unchecked worker still READS as
            pending -- indistinguishable, on disk, from one Kaggle
            confirmed. It must not be counted as running and must not get a
            stream, or the app is back to presenting a stale file as a live
            reading with a "reconnecting" card that never resolves.
          - FINISHED next: pending when the app closed, terminal now. No
            stream, deliberately (see _resume_streams) -- and this is the
            case the user reopens into most often, so it gets said out
            loud, along with where the frames are.
          - Everything still pending is what actually gets watched.

        A label pending in one job and finished in another is reported by
        its most demanding state, which is why the buckets subtract in that
        order rather than being built independently.
        """
        after = {(job.job_id, w.label): w.state
                 for job in jobs for w in job.workers}
        was_pending = [key for key, state in before.items()
                       if state in PENDING_STATES]
        unchecked = {label for _job, label in was_pending
                     if label in unreachable}
        finished = {label for job_id, label in was_pending
                    if after.get((job_id, label)) in TERMINAL_STATES
                    } - unchecked
        still = {label for job_id, label in was_pending
                 if after.get((job_id, label)) in PENDING_STATES
                 } - unchecked - finished

        watched = self._resume_streams(jobs, only=still)
        # Still rendering on Kaggle, but with no token here to watch it
        # with -- the account was removed while it ran. Named rather than
        # quietly dropped: those kernels are still spending quota.
        unwatched = sorted(still - set(watched))

        parts: list[str] = []
        if watched:
            parts.append(
                f"Kaggle says {len(watched)} render(s) are still running "
                f"({', '.join(sorted(watched))}) — reconnecting to each "
                "one's live log now. Kaggle replays a session's log from "
                "the start, so the phase, frame count and GPU readings "
                "rebuild themselves over the next few moments. Nothing "
                "needs restarting, and no quota is spent on this.")
        if finished:
            everything = not watched and not unchecked and not unwatched
            parts.append(
                (f"All {len(finished)} render(s) that were running when "
                 if everything else
                 f"{len(finished)} of the render(s) that were running when ")
                + "BlendFleet last closed have finished "
                f"({', '.join(sorted(finished))}) — nothing was reconnected "
                "for them, because a finished kernel has no progress left "
                "to send. Their frames are waiting on Kaggle: use “Collect "
                "frames…” on the scene to download them before you launch "
                "anything else on those accounts.")
        if unwatched:
            parts.append(
                f"{len(unwatched)} render(s) are still running on Kaggle "
                f"({', '.join(unwatched)}) but no account with that label is "
                "configured here any more, so BlendFleet cannot show their "
                "progress, cancel them or collect their frames. Re-add the "
                "account under Instances to get them back.")
        if unchecked:
            reasons = sorted({unreachable[label] for label in unchecked})
            parts.append(
                f"Kaggle could not be asked about {len(unchecked)} render(s) "
                f"({', '.join(sorted(unchecked))}): {'; '.join(reasons[:2])}. "
                "What their cards show is from when BlendFleet last closed "
                "and may be out of date — no log stream was reopened for "
                "them, because reconnecting to a render that has already "
                "finished shows a bar that never moves. The check runs "
                "again by itself every 30 seconds, or press Retry.")

        if parts:
            tone = "offline" if unchecked else "idle"
            self.notification.emit(self._scrub(" ".join(parts)), tone)
        # Unconditional: the poll rewrote the state file, and the cards are
        # still drawn from what was true before it.
        self._emit_state()

    def _startup_unreachable(self, pending: list[str],
                             reasons: dict[str, str]) -> None:
        """The whole startup check failed, not one account's status call.

        Nothing is resumed and nothing is claimed: the cards keep showing
        the file's own state, and the message says that is what they are.
        """
        self._finish_startup_check(
            {(None, label): "running" for label in pending}, [], reasons)

    def preferences(self) -> str:
        return json.dumps({
            "accent": self.settings.accent,
            "theme": self.settings.theme,
            "translucent": self.settings.translucent,
            "sound": self.settings.sound,
            "minGpus": self.settings.min_gpus,
            "fullscreen": self.settings.fullscreen,
            "frameThumbnails": self.settings.frame_thumbnails,
            "font": self.settings.font,
            "closeAction": self.settings.close_action,
        })

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

    def blenderVersions(self) -> str:
        """The versions offered, and the one currently chosen.

        The list is a menu, not a gate -- an unlisted but well-formed
        version is accepted, because Blender releases far more often than
        this app does.
        """
        return json.dumps({"versions": list(KNOWN_VERSIONS),
                           "current": self.settings.blender_version})

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
                    "already expired. If the render has finished, use "
                    "Download to collect the frames instead; the diagnostic "
                    "log (Settings → Diagnostics) records exactly which "
                    "files Kaggle listed for this session.", "idle")
                return
            self.framePreview.emit(json.dumps(
                {"frame": frame, "path": Path(path).as_uri(),
                 "label": owner.label}))

        self._start(f"preview:{frame}", work, f"Fetching frame {frame}", ok)

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
            # These never pass through notification/logLine -- they reach
            # the page as a per-account map beside the library -- so this
            # is the only place they can be kept. An empty-looking scene
            # library is a failure the user WILL report, and every account
            # having failed for its own reason is the answer.
            for label, message in errors.items():
                crash_log.record(
                    self._scrub(f"scene library: {label} listed nothing "
                                f"because {message}"), critical=True)
            self.scenesChanged.emit(json.dumps({
                "scenes": [_scene_payload(s) for s in found],
                "errors": errors,
            }))

        self._start("scenes", work, "Loading the scene library", ok)

    def _outputs_payload(self) -> dict:
        """Every render this app has tracked, newest first.

        Sourced from `Fleet.load_jobs()` and NOTHING else. That list is
        append-only -- it is only ever shortened one entry at a time by an
        explicit forgetJob() -- so it already holds every render this app
        has started, today's and last month's alike. A render started
        outside this app was never tracked and cannot appear here; there is
        no other record to read.

        Pure disk, no network, deliberately: this is what the Files page
        draws with the moment it opens, and the startup Kaggle poll already
        taught us what happens when a page waits on the network to show
        something it already knows. Whether Kaggle STILL has each render's
        output is a separate, slower question, answered by checkOutputs()
        afterwards and merged in here from `_output_availability`.
        """
        fleet = self.fleet_factory(self.store.list())
        jobs = fleet.load_jobs()
        # Newest first: the render someone wants to download is
        # overwhelmingly the one that just finished. load_jobs() is
        # oldest-first (append order), so this is a reverse, not a sort on
        # started_at -- a job recorded before started_at existed reads 0.0
        # and would sort to the bottom as if it were the oldest thing here,
        # when all that is actually known is when it was appended.
        return {
            "outputs": [_output_payload(job, self._output_availability.get(
                job.job_id)) for job in reversed(jobs)],
        }

    def outputs(self) -> None:
        """Emit the tracked-render list straight off disk.

        Synchronous on purpose -- see _outputs_payload. Availability is not
        touched here, so a row this session has already checked keeps its
        answer across a refresh instead of flickering back to unchecked.
        """
        self.outputsChanged.emit(json.dumps(self._outputs_payload()))

    def checkOutputs(self) -> None:
        """Ask Kaggle which tracked renders it still has output for.

        Kaggle deletes a kernel session's output after a while. The render
        still happened, and the row stays listed for exactly that reason --
        a render someone remembers doing must not vanish from the app that
        ran it just because the files behind it expired. What changes is
        that the row can say there is nothing left to download.

        Off-thread and after the fact: the list is already on screen by the
        time this starts, and if it never answers the rows simply stay
        unchecked, which is what they honestly are.

        Per JOB, a render is only "gone" when EVERY one of its accounts was
        actually reached and none of them still had a render file. One
        account answering, with files, is enough to make the render
        downloadable -- partly, and the row says how many. An account this
        app could not ask (revoked token, rate limit, no configured account
        for it any more) is neither: with no account answering "yes", the
        whole row falls back to unknown rather than claiming Kaggle has
        deleted something this app never managed to ask about.
        """
        accounts = self.store.list()

        def work():
            fleet = self.fleet_factory(accounts)
            jobs = fleet.load_jobs()
            by_label = {a.label: a for a in accounts}
            # The same label-then-username fallback collector.collect uses,
            # for the same reason: a label is a nickname the user can edit
            # at any time, the username is the account's real identity, and
            # a renamed account must not turn a perfectly collectable
            # render into an unreachable one.
            by_username = {getattr(a, "username", None): a for a in accounts
                           if getattr(a, "username", None)}
            found: dict[str, dict] = {}
            for job in jobs:
                with_output = 0
                checked = 0
                files = 0
                errors: dict[str, str] = {}
                for worker in job.workers:
                    account = (by_label.get(worker.label)
                               or by_username.get(worker.username))
                    if account is None:
                        errors[worker.label] = (
                            f"no configured account matches "
                            f"{worker.username or worker.label} any more, so "
                            f"BlendFleet has no token to ask Kaggle with. "
                            f"Re-add that account under Manage accounts… to "
                            f"find out whether its frames are still there.")
                        continue
                    try:
                        names = fleet.client_factory(
                            account.token).list_output_files(worker.kernel_slug)
                    except Exception as e:      # noqa: BLE001 -- becomes text
                        errors[worker.label] = explain(
                            f"Asking Kaggle whether {worker.label} still has "
                            f"this render's frames", e)
                        continue
                    checked += 1
                    if names:
                        with_output += 1
                        files += len(names)
                if with_output:
                    state = "available"
                elif checked and checked == len(job.workers):
                    state = "gone"
                else:
                    # Either nobody could be asked, or only some accounts
                    # answered and every one of those had nothing. Both are
                    # "not established", never "deleted".
                    state = "unknown"
                found[job.job_id] = {
                    "state": state,
                    "withOutput": with_output,
                    "checked": checked,
                    "workers": len(job.workers),
                    "files": files,
                    "errors": errors,
                    "at": time.time(),
                }
            return found

        def ok(found: dict) -> None:
            # update(), not replace: a job forgotten between the two reads
            # simply stops being emitted, and a job checked earlier this
            # session keeps its answer if this pass somehow skipped it.
            self._output_availability.update(found)
            self.outputsChanged.emit(json.dumps(self._outputs_payload()))

        def failed(message: str) -> None:
            # Nothing is marked. Every row stays unchecked, which is true,
            # and the user is told why rather than being left with a list
            # that never resolves.
            self.notification.emit(
                f"Could not check which renders Kaggle still has: {message} "
                f"The renders below are still listed — use Re-check once the "
                f"connection is back.", "offline")

        self._start("outputs-availability", work,
                    "Checking which renders Kaggle still has", ok, failed)

    # ---- JS -> Python: writes -----------------------------------------
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
                   "minGpus": "min_gpus", "blenderVersion": "blender_version",
                   "frameThumbnails": "frame_thumbnails", "font": "font",
                   "closeAction": "close_action"}
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
        # The Qt adapter re-applies the theme to the WINDOW here,
        # because its window is Qt. This one's window IS the page, and
        # the page restyles itself from its own CSS variables the moment
        # settingsChanged arrives -- there is nothing else to tell.
        self.settingsChanged.emit(self.preferences())

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

    def refreshQuota(self) -> None:
        """Best-effort per-account quota. A single account's fetch failing
        (rate limit, revoked token) only ever downgrades that figure to
        "unavailable" -- it must never break the page or block a render."""
        accounts = self.store.list()

        def work() -> dict[str, str]:
            try:
                client_factory = self.fleet_factory(accounts).client_factory
            except Exception as e:      # noqa: BLE001 -- see below
                # Swallowed on purpose: quota is decoration, and a fleet
                # that cannot be built must not stop the page drawing. But
                # "unavailable" on every card is also exactly what a
                # REVOKED token looks like, so the reason has to survive
                # somewhere -- it is the difference between "Kaggle is
                # rate-limiting you" and "this account is dead".
                client_factory = None
                crash_log.record(self._scrub(
                    "quota refresh: could not build a Fleet at all, so every "
                    f"account reads 'unavailable'. {type(e).__name__}: {e}"))
            result: dict[str, str] = {}
            for acct in accounts:
                if client_factory is None:
                    result[acct.label] = "unavailable"
                    continue
                try:
                    q = client_factory(acct.token).quota()
                    result[acct.label] = (f"{q.used_seconds / 3600.0:.1f} / "
                                          f"{q.total_seconds / 3600.0:.1f} h")
                except Exception as e:      # noqa: BLE001 -- see above
                    result[acct.label] = "unavailable"
                    crash_log.record(self._scrub(
                        f"quota refresh: {acct.label} reads 'unavailable' "
                        f"because {type(e).__name__}: {e}"))
            return result

        def ok(result: dict) -> None:
            self._quota.update(result)
            self._emit_state()
            self.healthChanged.emit(self.health())

        self._start("quota", work, "Refreshing quota", ok, lambda _m: None)

    # ---- JS -> Python: actions ----------------------------------------
    def setBlend(self, path: str = "") -> str:
        """Remember the .blend the shell's file chooser came back with.

        The Qt adapter opens the dialog itself (pickBlend). This one
        cannot: it is headless, with no window to parent a dialog to. The
        shell owns native UI, so it shows its own chooser and hands the
        path here -- and the page is unchanged, because preload.js still
        offers it a pickBlend() that does both halves.

        An empty path means the chooser was dismissed: the previous
        choice stands rather than being cleared, which is what a
        cancelled dialog means everywhere else.
        """
        if path:
            self.blend = Path(path)
        return json.dumps({"path": str(self.blend) if self.blend else "",
                           "name": self.blend.name if self.blend else ""})

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
                # Only the OWNER is required for a standalone upload.
                #
                # Omitting `required` means "every configured account is
                # mandatory", which is right for a launch -- an account
                # about to have a kernel pushed to it must be able to see
                # the scene, or it burns quota failing. But an upload
                # starts nothing and costs no quota, so a friend whose
                # READER grant Kaggle has not finished propagating is not
                # a failed upload: the bytes are on Kaggle, and the grant
                # lands moments later. Treating it as fatal is what made
                # the first Upload report failure and a second, identical
                # Upload succeed. Anything not shared yet comes back on
                # fleet.unshared_accounts and is reported below.
                required=[accounts[0]],
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
            # Say so when the upload landed but the sharing has not caught
            # up. Making a propagation delay non-fatal (see `required`
            # above) must not also make it invisible -- the user would
            # start a render on an account that still cannot see the
            # scene, and only find out when it failed.
            pending = sorted(fleet.unshared_accounts)
            if pending:
                self.notification.emit(
                    f"Uploaded {blend.name} to {slug}, but Kaggle has not "
                    f"finished sharing it with {', '.join(pending)} yet. "
                    "The upload itself is done and does not need repeating; "
                    "wait a moment and press Upload again to re-check "
                    "before rendering on those accounts.", "idle")
            else:
                self.notification.emit(
                    f"Uploaded {blend.name} to {slug}", "active")
            self._emit_state()

        self._start("dataset", work, "Uploading the scene", ok)

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

    def collect(self, label: str = "", job_id: str = "",
                destination: str = "") -> None:
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
        from blendfleet.collector import collect as collect_frames

        # Where the zip goes is the shell's question to ask (see
        # setBlend). No destination means the chooser was dismissed, and
        # a collect with nowhere to put its frames does not start.
        if not destination:
            return
        accounts = self.store.list()
        who = label or "the fleet"
        # Which job this collect actually ran against, filled in by work()
        # once it has resolved `label`/`job_id`/"the newest". The result
        # handler needs it to tell the ONE row that started this download
        # where its zip went, and it cannot re-derive it: "the newest job"
        # could have changed by the time the download finishes.
        resolved: dict[str, object] = {}

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
            resolved["jobId"] = job.job_id
            resolved["scene"] = job.scene_key
            # How many accounts THIS collect will fetch from -- one for a
            # per-instance download, the whole fleet otherwise, mirroring
            # collector.collect's own filter exactly. The page rolls the
            # per-account byte counts up into a single job figure and has
            # no other way to know when it has heard from everyone; a count
            # guessed from whoever has reported so far would read as
            # complete the moment the first account finished.
            resolved["workers"] = (
                sum(1 for w in job.workers if w.label == label) if label
                else len(job.workers))
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
                        # Which render these bytes belong to, and how many
                        # accounts are in it. Carried on the EXISTING
                        # progress signal rather than a second channel:
                        # the dashboard card still reads `label`, and the
                        # job-level bar is the same ticks added up.
                        "jobId": resolved["jobId"],
                        "jobWorkers": resolved["workers"],
                    })))

        def ok(report) -> None:
            if report is None:
                self.notification.emit(
                    "No render job found — start a render first.", "idle")
                return
            if report.archive_path is None:
                # Nothing came back, so collect() wrote no zip at all --
                # saying "collected 0 frames to <folder>" would send the
                # user looking for a file that is not there. Absent, not
                # zero.
                message = (f"Nothing to collect from {who} yet — no frames "
                           f"have finished rendering, so no zip was written "
                           f"to {destination}. Collect again once an "
                           f"account reports frames done")
            else:
                message = (f"Collected {report.copied} frame(s) from {who} "
                           f"into {report.archive_path} — unzip it to get "
                           f"the frames")
                if report.wanted_name:
                    # Re-collecting never replaces the zip already there:
                    # it may be the only copy of a longer render. The odd
                    # name has to be explained or it just looks like a bug.
                    message += (f" (a {report.wanted_name} was already in "
                                f"that folder, so this download was saved "
                                f"beside it instead of replacing it)")
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
            # The same sentence, addressed to the ONE row that started
            # this download, so it can stop showing a bar and say where
            # the zip went instead of leaving the user to find it in a
            # toast that has already faded.
            self.collectFinished.emit(json.dumps({
                "jobId": resolved.get("jobId", ""),
                "label": label,
                "archivePath": (str(report.archive_path)
                                if report.archive_path else ""),
                "wantedName": report.wanted_name,
                "copied": report.copied,
                "missing": len(report.missing_frames),
                "destination": destination,
                "message": message,
                "tone": tone,
            }))

        # Fix round 2: the busy key is BUTTON identity, not call identity.
        # `job_id` was folded into this key alongside `label`, but app.js
        # (btn-collect's own disable/re-enable, keyed by an EXACT match on
        # "collect:") only ever sends "" for both on the fleet-wide button
        # -- so the key it now saw was "collect::", which nothing in that
        # map matches, and the button never disabled while a collect ran.
        #
        # Round 3 restores a per-job key, but as its own PREFIX rather
        # than as a third segment of this one -- "collect-job:<id>", never
        # "collect::<id>". That is what keeps the exact-match "collect:"
        # entry (the fleet-wide button) and the "collect:<label>" per
        # instance buttons matching exactly as they did, while the Files
        # page's per-render Download button, which is genuinely one button
        # PER JOB and not per account, gets a key that identifies it. Same
        # idiom as launch-scene:/delete-scene:.
        key = f"collect-job:{job_id}" if job_id else f"collect:{label}"
        self._start(key, work, f"Collecting frames from {who}", ok)

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
                # The sentence above reaches the log too (see
                # _note_user_message), but only as its friendly half; the
                # traceback is what distinguishes a dropped connection from
                # a bug in the parser.
                crash_log.record(self._scrub(
                    f"the hardware probe stream for {label} ({kernel_slug}) "
                    f"failed. {type(e).__name__}: {e}\n"
                    + "".join(traceback.format_exception(
                        type(e), e, e.__traceback__))),
                    critical=True)

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
            self._stream_worker(account, worker)

    def _stream_worker(self, account, worker) -> None:
        """Start one worker's SSE log stream, unless it already has one.

        Shared by _start_streams (a launch) and _resume_streams (a
        restart), so the two can never watch a kernel differently. The
        duplicate guard is the reason this is one function: a resume that
        raced a launch, or a second call of either, would otherwise open a
        second connection for the same kernel and deliver every PROGRESS
        line twice.
        """
        label = account.label
        existing = self._stream_by_label.get(label)
        if existing is not None and existing.is_alive():
            return

        def run():
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
                    on_preflight=lambda r: self._preflight_q.put((label, r)),
                    on_thumbnail=lambda r: self._thumb_q.put((label, r)))
            except Exception as e:      # noqa: BLE001
                # Still swallowed -- a dead stream must never kill the
                # render, which keeps going on Kaggle regardless of
                # whether anyone is watching it. But this is the exact
                # shape of "the progress bar froze at 3/15 and nothing
                # said why": from here on that account simply reports
                # nothing, forever, and the traceback was the only
                # evidence of it. Recorded, not raised.
                crash_log.record(self._scrub(
                    f"the live log stream for {label} "
                    f"({worker.kernel_slug}) stopped and will not "
                    "reconnect, so this account's progress and telemetry "
                    "freeze at whatever they last showed. The render "
                    f"itself is unaffected. {type(e).__name__}: {e}\n"
                    + "".join(traceback.format_exception(
                        type(e), e, e.__traceback__))),
                    critical=True)

        thread = threading.Thread(target=run, daemon=True,
                                  name=f"blendfleet-stream-{label}")
        self._stream_by_label[label] = thread
        self._stream_threads.append(thread)
        thread.start()

    def _resume_streams(self, jobs: list | None = None,
                        only: set[str] | None = None) -> list[str]:
        """Re-attach a log stream to every worker still running on Kaggle.

        Returns the labels now being watched, so the caller can say
        precisely which renders it reconnected to rather than counting
        workers it hoped it had.

        `jobs` is what a poll JUST read from Kaggle, and `only` is the set
        of labels that poll confirmed are still pending. Both come from
        _startup_check, and they are the whole point: without them this
        method falls back to the state file, which holds whatever was true
        when the app last CLOSED -- so a render that finished overnight
        read as running, got a stream it could never learn anything from,
        and produced a card that said "reconnecting" for ever. The state
        below is only ever a starting point for what to watch; it is never
        evidence that anything IS running.

        The gap this closes: _start_streams is only ever called from the
        success callback of a launch. Nothing called it when the app
        started, so a render that was still going on Kaggle when the window
        closed got no stream at all when the window reopened -- no phase,
        no frame counter, no GPU telemetry, no system RAM. The job showed
        as "running" with nothing behind it, which is indistinguishable
        from stuck. The renders were fine; the app had simply stopped
        looking.

        PENDING_STATES, not ACTIVE_STATES: a kernel that has been pushed
        but whose Kaggle session has not started yet reports "not_started"
        (and one whose session has not run a cell yet, "new_script").
        Neither is running, but both are about to be -- and a render pushed
        moments before the app closed is exactly the case where the user
        has seen no progress at all and most needs it to appear. Kaggle's
        own stream endpoint waits for the log URL rather than failing, so
        the stream is already built to be started before there is anything
        to read. This is also the predicate busy_labels()/require_free()
        and deleteScene already use for "this account is still occupied",
        so a worker that holds an account is now exactly a worker that gets
        watched.

        TERMINAL_STATES get nothing, deliberately: a finished kernel's log
        is fixed, replaying it would rebuild a "rendering 15/15" phase for
        a job that ended hours ago, and it costs a Kaggle connection per
        account to learn nothing.

        Nothing is invented for the reconnecting window -- see
        _resumed_labels and _state_payload's "reconnecting" flag. What the
        user is TOLD about all this is composed by _finish_startup_check,
        not here: only that caller knows which renders finished while the
        app was closed and which could not be checked at all, and one
        sentence covering every case is the only way it can be true in
        every case.
        """
        by_label = {a.label: a for a in self.store.list()}
        resumed: list[str] = []
        if jobs is None:
            try:
                jobs = self.fleet_factory(self.store.list()).load_jobs()
            except Exception as e:      # noqa: BLE001
                # A jobs file that cannot even be read is already reported
                # to the page through unreadableJobs; failing to resume on
                # top of that must not stop the app starting.
                crash_log.record(self._scrub(
                    "could not read the tracked jobs at startup, so no "
                    "render already running on Kaggle will show live "
                    f"progress until the next launch. {type(e).__name__}: "
                    f"{e}"), critical=True)
                return []
        for job in jobs:
            for worker in job.workers:
                if worker.state not in PENDING_STATES:
                    continue
                if only is not None and worker.label not in only:
                    # Pending in the file, but this run's poll did not
                    # confirm it -- unreachable, or already reported
                    # finished by another job. Not watched, and not counted
                    # as running by the caller either.
                    continue
                account = by_label.get(worker.label)
                if account is None:
                    continue    # account removed since: no token, no stream
                before = self._stream_by_label.get(worker.label)
                if before is not None and before.is_alive():
                    # Already watched; never open a second one. Still
                    # reported as watched, because it is.
                    if worker.label not in resumed:
                        resumed.append(worker.label)
                    continue
                self._stream_worker(account, worker)
                # Seeded from what is on disk so the first replayed
                # PROGRESS line, which re-reports numbers already saved,
                # does not trigger a pointless rewrite of the jobs file.
                self._persisted_done[worker.label] = worker.frames_done
                self._resumed_labels.add(worker.label)
                resumed.append(worker.label)
        return resumed

    def _persist_progress(self, advanced: dict[str, int]) -> None:
        """Write the frame counts this tick learned through to the jobs file.

        WRITE FREQUENCY, deliberately. _live_tick runs every 2 seconds and
        Fleet.record_progress is a read-merge-write of the whole jobs file,
        so writing on every tick would be ~1800 rewrites an hour of a file
        whose loss orphans running kernels. Three things keep it far below
        that:

          - Only PROGRESS moves this. Telemetry, system RAM and the phase
            string arrive every few seconds and are deliberately NOT
            persisted: they are live-only readings, and a resumed stream
            rebuilds all of them by replaying the log anyway. A frame line
            arrives once per finished frame -- about once a minute at the
            57s/frame this app measures.
          - Only a CHANGE writes. _persisted_done remembers what is
            already on disk, so a replayed log (a reconnect re-delivers
            every line from the top) and an idle fleet both write nothing.
          - The whole tick is batched into ONE call. Four accounts each
            finishing a frame in the same 2-second window is one file
            write, not four.

        So the steady state is roughly one write per completed frame across
        the whole fleet, which is the same order as the 30-second poll
        already writes at -- and zero writes whenever nothing is rendering.

        Failure is recorded, never raised: the render is on Kaggle and
        keeps going regardless, and a state file that cannot be written is
        not a reason to stop showing live progress. _persisted_done is
        updated only after a call that did not raise, so a failed write is
        retried on the next frame rather than assumed done -- and it is
        updated for every label in the batch, not just the ones
        record_progress reports writing, because the other outcome is "disk
        already holds this number or a larger one", which needs no write
        either.
        """
        try:
            self.fleet_factory(self.store.list()).record_progress(advanced)
        except Exception as e:      # noqa: BLE001 -- see docstring
            crash_log.record(self._scrub(
                "could not save render progress to the jobs file, so a "
                "restart would show an older frame count for "
                f"{', '.join(sorted(advanced))}. The render itself is "
                f"unaffected. {type(e).__name__}: {e}"), critical=True)
            return
        self._persisted_done.update(advanced)

    def _slot(self, label: str) -> dict:
        # Anything arriving live means this label is no longer merely
        # reconnecting -- it is reporting. Cleared here rather than at each
        # queue drain so a stream that comes back with telemetry before its
        # first PROGRESS line still clears it.
        self._resumed_labels.discard(label)
        return self._live.setdefault(label, {
            "phase": "", "framesDone": 0, "framesTotal": 0,
            "gpus": {}, "cpuCount": None, "ramTotal": None, "preflight": None,
            # Live system memory, distinct from ramTotal (which is the
            # machine's size, reported once). None until the first
            # SYSTEM line -- never 0, which would read as "no memory
            # in use" rather than "not measured yet".
            "ramUsed": None, "cpuPct": None,
            # The single most recent live frame preview from this
            # account, or None. See _live_tick for why exactly one.
            "thumb": None,
        })

    def _live_tick(self) -> None:
        """Drain what the stream threads collected, on the UI thread.

        Bounded per tick so a flood of telemetry cannot starve the loop.
        """
        changed = False
        # Collected across this tick's whole drain and written ONCE at the
        # end, rather than per line -- see _persist_progress.
        advanced: dict[str, int] = {}
        for _ in range(200):
            try:
                label, done, total = self._progress_q.get_nowait()
            except queue.Empty:
                break
            slot = self._slot(label)
            slot["framesDone"], slot["framesTotal"] = done, total
            slot["phase"] = f"rendering · {done}/{total} frames"
            if done != self._persisted_done.get(label):
                advanced[label] = done
            changed = True
        if advanced:
            self._persist_progress(advanced)
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
        # LIVE FRAME PREVIEWS, AND WHY ONLY ONE SURVIVES PER ACCOUNT.
        #
        # Every one of these is an image -- about 5 kB of JPEG as ~7 kB of
        # base64 -- and _state_payload is re-serialised whole and pushed
        # across the QWebChannel on every tick. Keeping a 100-frame
        # render's previews would be 100 images held forever AND ~700 kB
        # of JSON crossing the channel every two seconds, for pictures
        # nobody is looking at.
        #
        # So: the NEWEST preview per account, and nothing else. The
        # question this feature answers is "what is it doing right now",
        # which only the newest frame can answer; the older ones are
        # already superseded by the time they would be drawn, and the
        # full-resolution frames are still all recoverable afterwards
        # through Collect. Draining in arrival order means the last one
        # out of the queue wins, which is the newest by construction --
        # one stdout, in frame order.
        #
        # The per-tick cap is about not starving the event loop, NOT about
        # memory -- memory is bounded by keeping one image per label
        # however many are drained. It matches the other queues' 200 so a
        # backlog (a stream that reconnected and replayed, a window that
        # was busy) is cleared in one tick rather than dribbled out over
        # several, which would draw previews minutes after the frames they
        # show were rendered.
        for _ in range(200):
            try:
                label, record = self._thumb_q.get_nowait()
            except queue.Empty:
                break
            slot = self._slot(label)
            slot["thumb"] = {
                "frame": record["frame"],
                # Built HERE rather than on the page: the page should not
                # have to know this is JPEG, and a data URL is the only
                # form a QWebChannel string payload can be drawn from
                # without writing a file per frame to the user's disk.
                "dataUrl": "data:image/jpeg;base64," + record["jpeg_b64"],
                "bytes": record["bytes"],
            }
            # A preview only exists because a frame finished inside a
            # running Blender, so its arrival is evidence of the same
            # thing telemetry is.
            if not slot["phase"]:
                slot["phase"] = "rendering"
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
        """Wait for whatever is in flight, streams included.

        Called when the pipe closes (rpc.__main__), which is how this
        process learns its shell is gone."""
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


def _frames_done_source(worker) -> str:
    """Where `framesDone` came from, so the page can never present one kind
    of number as another. One of:

      "final"   -- read from this worker's OWN kernel log after it stopped
                   (Fleet._read_final_frame_count). The render's last word,
                   not a cached live reading, and the only count of a
                   finished render this app is entitled to state.
      "unknown" -- the worker has STOPPED and that log could not be read,
                   or carried no PROGRESS line. framesDone is then whatever
                   a live stream last saved, which is a floor from some
                   moment before the render ended: the page must show the
                   count as not known, NOT as this number and NOT as zero.
      "saved"   -- a live stream wrote it while the render was still going.
                   framesDoneAge says how old it is; the card already
                   labels it as saved rather than measured.
      "none"    -- nothing has ever reported a count for this worker.

    The distinction only exists because Kaggle's kernel-status API reports
    no frame count at all: for a running kernel the live SSE stream is the
    only source, and it dies with the window. A render that finished
    overnight therefore reopened showing "1 / 2" -- the last thing the
    stream managed to save -- for a render that had done both frames.
    """
    if getattr(worker, "finished_at", 0.0):
        return "final" if getattr(worker, "final_count_known", False) \
            else "unknown"
    if getattr(worker, "frames_done_at", 0.0):
        return "saved"
    return "none"


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


def _output_payload(job, availability: dict | None) -> dict:
    """One tracked render, as the Files page's outputs list needs it.

    Everything here is read off the job record this app already keeps --
    no network, no guessing. The two judgement calls:

    `framesDoneKnown` is False unless EVERY worker's own kernel log has
    been read back for its final count (WorkerState.final_count_known).
    Short of that, `framesDone` is whatever a live log stream last managed
    to save before the window closed: a floor from some moment before the
    render ended, not a total. The row shows the frame RANGE either way --
    that is a fact about the job -- and only quotes a done count when it
    is the render's own last word.

    `finished` means every worker reached a terminal state, matching
    _job_payload's identical test. It says nothing about whether the
    frames still exist; that is `availability`, which comes from Kaggle.

    `availability` is "unchecked" when nothing has asked yet -- a real
    state, and the one every row starts in. Merging a default of
    "available" or "gone" here would be inventing a reading.
    """
    started = getattr(job, "started_at", 0.0) or 0.0
    avail = availability or {}
    return {
        "jobId": job.job_id,
        "scene": job.scene_key,
        "blend": job.blend_name,
        "startFrame": job.start_frame,
        "endFrame": job.end_frame,
        "frameCount": max(job.end_frame - job.start_frame + 1, 0),
        # Age in seconds rather than a date, matching framesDoneAge's own
        # contract: None means never recorded, which is what a job saved
        # before started_at existed loads as -- and an absent timestamp
        # must read as unknown, never as 1970.
        "ageSeconds": max(time.time() - started, 0.0) if started else None,
        "accounts": [w.label for w in job.workers],
        "usernames": [w.username for w in job.workers],
        "workerCount": len(job.workers),
        "framesDone": sum(w.frames_done for w in job.workers),
        "framesDoneKnown": bool(job.workers) and all(
            getattr(w, "final_count_known", False) for w in job.workers),
        "finished": bool(job.workers) and all(w.finished_at
                                              for w in job.workers),
        "availability": avail.get("state", "unchecked"),
        "availableAccounts": avail.get("withOutput", 0),
        "checkedAccounts": avail.get("checked", 0),
        "availabilityErrors": avail.get("errors", {}),
        "checkedAgeSeconds": (max(time.time() - avail["at"], 0.0)
                              if avail.get("at") else None),
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
