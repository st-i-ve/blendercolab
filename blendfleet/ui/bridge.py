"""The one seam between the web UI and the Python that does the work.

Everything the page can ask for, and everything it gets told, passes
through the Backend object below. That is the point: the UI is HTML/CSS/JS
in a QWebEngineView, but nothing in it may invent state. `render-farm
(7).html` -- the design this port follows -- is a SIMULATION, with its own
fake `instances[]`, its own `tickRender()`, its own generated frame
images. Every one of those has to be replaced by a call through here, or
the app goes back to showing numbers nobody measured.

WHAT CHANGED (2026-08-17): THIS FILE NO LONGER IMPLEMENTS ANY OF IT.

The behaviour -- all 117 methods, every payload, every honesty rule --
lives in `blendfleet/rpc/session.py`, and this is the Qt face of it. For
one day there were two copies: `session.py` was ported out of this file
so an Electron shell could run without Qt, and duplicating was chosen
over sharing because a subtle threading regression in the app being used
for real renders was worse than visible, temporary duplication (see
docs/superpowers/specs/2026-08-16-electron-shell-design.md). Electron is
now packaged and proven, so the copies collapse, and this is the half
that goes: a normalised diff of the two showed the bodies identical
except for the Qt-specific items listed below.

WHAT IS ACTUALLY QT-SPECIFIC, and therefore all that is left here:

  - The 14 signals. QWebChannel can only expose Qt signals, so they have
    to be declared on a QObject. A Session emits through `Emitter`, and
    `_relay` below wires each one to its Qt twin.
  - The @Slot decorations. QWebChannel exposes slots and nothing else --
    which is also why the relay's own delivery method is NOT a slot: it
    would appear on the page as `backend._deliver`.
  - `pickBlend` and `collect`, which open native dialogs. A headless
    Session cannot: it has no window to parent one to. So those two show
    the dialog and hand the answer to the Session (`setBlend`, and
    `collect`'s `destination`), and the page cannot tell.
  - `setPreference` re-applying the Qt theme. The page restyles itself
    from its own CSS variables, but the WINDOW is Qt -- title bar, the
    ground behind the view, the Mica flags, any dialog opened later.

WHAT IS GONE, and worth saying: the QThread worker, its parenting, its
`deleteLater`, and the `setParent(None)` in `_orphan`. All of that existed
to stop Qt aborting with `qFatal("QThread: Destroyed while thread is
still running")` when the window closed over a live Kaggle request.
Session's workers are plain daemon threads, which cannot abort a process
that way, so the hazard is not ported -- it is deleted.

CONTRACT (unchanged, and now stated in one place):
  - JS -> Python is a @Slot returning a JSON string, not a QVariant map:
    an explicit `json.dumps` at the boundary means the shape of every
    payload is written down in one place and cannot drift silently as a
    dataclass gains a field.
  - Python -> JS is a signal carrying a JSON string, for the same reason.
  - Nothing here blocks. Every call that touches the network runs on a
    worker and answers with a signal, because a slot invoked from the
    page runs on the UI thread and a blocking one freezes the render, not
    just the widget.

Honesty rules, which live with the behaviour in session.py and are
repeated here because losing them is the one unacceptable regression:
quota is what the API says right now, never a promise; cached hardware is
labelled with its age, because Kaggle reallocates; there is no idle
instance to poll, so live telemetry exists only while a kernel runs; and
frames "done" is a COUNT the notebook reports, not a list, so the frame
grid is approximate and says so.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal, Slot
# The one honest way to ask "does this wrapper still have a C++ object
# behind it?" -- see _forward, where the answer decides between emitting a
# signal and segfaulting.
from shiboken6 import isValid

from blendfleet.rpc.protocol import EVENTS
# Re-exported, not re-implemented: web_main, web_host and the tests import
# these from here, and they are the Session's now.
from blendfleet.rpc.session import (            # noqa: F401 -- re-exports
    LIVE_INTERVAL_MS,
    POLL_INTERVAL_MS,
    SECONDS_PER_FRAME_DEFAULT,
    Session,
    _STOP_GRACE_MS,
    _Worker,
    _elapsed,
    _frames_done_source,
    _job_payload,
    _orphan,
    _output_payload,
    _scene_payload,
    _snapshot_payload,
    _unreadable_jobs_payload,
    orphaned_workers,
)


class Backend(QObject):
    """Registered on the QWebChannel as `backend`.

    The page does `new QWebChannel(qt.webChannelTransport, ch => {
    window.backend = ch.objects.backend })` and from then on calls the
    slots and connects to the signals below. Each slot hands straight to
    the Session; the Session's events come back through `_relay`.
    """

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
    outputsChanged = Signal(str)
    collectFinished = Signal(str)
    straySessionsChanged = Signal(str)

    # Private, and deliberately not a Slot: QWebChannel exposes every slot
    # it finds, and the page has no business being able to inject events.
    _relayed = Signal(str, list)

    def __init__(self, store, fleet_factory, verifier,
                 settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Whichever thread built the window: the one Qt signals may be
        # emitted from directly. Recorded before the Session exists, since
        # its timers start inside its constructor.
        self._home_thread = threading.get_ident()
        self._session = Session(store, fleet_factory, verifier, settings)
        self._relay()

    # ---- the relay ---------------------------------------------------
    def _relay(self) -> None:
        """Session events -> Qt signals, always on the main thread.

        A Session emits from whichever thread did the work: a worker
        finishing a Kaggle call, a poll timer, a log stream. Off-thread
        events funnel through one private signal with an explicit
        QueuedConnection, which is the hop onto the main thread that
        QWebChannel's transport requires.

        ON-THREAD EVENTS ARE EMITTED DIRECTLY, and that is not an
        optimisation -- it is the old behaviour. A QThread worker's
        completion signal was queued, so those arrived after the loop was
        pumped; but a `notification.emit` inside a slot ran on the UI
        thread and arrived immediately. Queueing both broke 28 tests that
        call a slot and then read what the page was told, and they were
        right to break: "the page hears this before the call returns" is
        part of the contract for everything that answers without touching
        the network.
        """
        self._relayed.connect(self._deliver, Qt.QueuedConnection)
        for name in EVENTS:
            getattr(self._session, name).connect(self._forward(name))

    def _forward(self, name: str):
        def handler(*args) -> None:
            # THE WINDOW MAY BE GONE. A Session's poll and live timers are
            # threads it owns, and they outlive a Backend whose C++ half Qt
            # has already destroyed -- the live one ticks every two
            # seconds, so this is not a narrow race. Emitting a signal on a
            # deleted QObject is not an exception, it is a SEGMENTATION
            # FAULT: exit 139, no traceback, no failing test name. That is
            # what this cost when the collapse first landed, and it is only
            # possible now because the old QTimers were Qt's children and
            # died with their parent.
            #
            # Cancelled rather than merely ignored, or the thread would go
            # on ticking into nothing for the life of the process.
            if not isValid(self):
                session = self.__dict__.get("_session")
                if session is not None and not session._timers_cancelled:
                    session.cancel_timers()
                return
            # Ident, not QThread identity: this asks a plain Python
            # question ("am I on the thread that built this?") and answers
            # it without depending on how PySide wraps a QThread pointer.
            if threading.get_ident() == self._home_thread:
                getattr(self, name).emit(*args)
            else:
                self._relayed.emit(name, list(args))
        return handler

    def _deliver(self, name: str, args: list) -> None:
        getattr(self, name).emit(*args)

    # ---- everything else is the Session's ----------------------------
    def __getattr__(self, name: str):
        """Anything not defined here is the Session's.

        `settings`, `store`, `blend`, `fleet`, `stop()`, `live_renders()`
        and the private bookkeeping the tests reach into (`_workers`,
        `_live_tick`, `_dataset`, `_thumb_q`, ...) all live on the Session
        now. Forwarding rather than restating them keeps this file about
        Qt, and keeps one implementation of the thing being forwarded to.
        """
        if name == "_session":
            # Only reachable before __init__ finishes; without this the
            # lookup below would recurse forever.
            raise AttributeError(name)
        session = self.__dict__.get("_session")
        if session is not None:
            try:
                return getattr(session, name)
            except AttributeError:
                pass
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value) -> None:
        """Writes follow reads, or the two objects disagree.

        Tests set `_dataset`, `_last_state` and `_unshared_accounts` to
        stage a situation. If those landed on the Backend while every
        reader looked at the Session, the staging would silently do
        nothing -- a test that passes while testing the wrong object.

        Names this class declares (a signal, a slot, `_session` itself)
        are never forwarded: shadowing a Signal would break the channel.
        """
        session = self.__dict__.get("_session")
        if (session is not None and name != "_session"
                and not hasattr(type(self), name)
                and hasattr(session, name)):
            setattr(session, name, value)
        else:
            super().__setattr__(name, value)

    # ---- slots: the page's whole vocabulary --------------------------
    @Slot(result=str)
    def accounts(self) -> str:
        return self._session.accounts()

    @Slot(result=str)
    def state(self) -> str:
        return self._session.state()

    @Slot()
    def ready(self) -> None:
        self._session.ready()

    @Slot(result=str)
    def preferences(self) -> str:
        return self._session.preferences()

    @Slot(result=str)
    def diagnostics(self) -> str:
        return self._session.diagnostics()

    @Slot(result=str)
    def blenderVersions(self) -> str:
        return self._session.blenderVersions()

    @Slot(int, int, result=str)
    def estimateRender(self, start_frame: int, end_frame: int) -> str:
        return self._session.estimateRender(start_frame, end_frame)

    @Slot(int)
    @Slot(int, str)
    def previewFrame(self, frame: int, job_id: str = "") -> None:
        self._session.previewFrame(frame, job_id)

    @Slot(result=str)
    def health(self) -> str:
        return self._session.health()

    @Slot()
    def scenes(self) -> None:
        self._session.scenes()

    @Slot()
    def outputs(self) -> None:
        self._session.outputs()

    @Slot()
    def checkOutputs(self) -> None:
        self._session.checkOutputs()

    @Slot()
    def poll(self) -> None:
        self._session.poll()

    @Slot()
    def refreshQuota(self) -> None:
        self._session.refreshQuota()

    @Slot()
    def syncDataset(self) -> None:
        self._session.syncDataset()

    @Slot(str)
    def launch(self, options_json: str) -> None:
        self._session.launch(options_json)

    @Slot(str, str)
    def renderScene(self, slug: str, options_json: str) -> None:
        self._session.renderScene(slug, options_json)

    @Slot(str)
    def deleteScene(self, slug: str) -> None:
        self._session.deleteScene(slug)

    @Slot(str)
    def startInstances(self, labels_json: str) -> None:
        self._session.startInstances(labels_json)

    @Slot(str)
    def sendJob(self, options_json: str) -> None:
        self._session.sendJob(options_json)

    @Slot()
    def cancelAll(self) -> None:
        self._session.cancelAll()

    @Slot(str)
    def cancelJob(self, job_id: str) -> None:
        self._session.cancelJob(job_id)

    @Slot()
    def forgetJob(self) -> None:
        self._session.forgetJob()

    @Slot(int, str)
    def forgetUnreadableJob(self, index: int, fingerprint: str) -> None:
        self._session.forgetUnreadableJob(index, fingerprint)

    @Slot(str)
    def cancelInstance(self, label: str) -> None:
        self._session.cancelInstance(label)

    @Slot(str, str)
    def addAccount(self, label: str, token: str) -> None:
        self._session.addAccount(label, token)

    @Slot(str, str)
    def setUsername(self, label: str, username: str) -> None:
        self._session.setUsername(label, username)

    @Slot(str)
    def removeAccount(self, label: str) -> None:
        self._session.removeAccount(label)

    @Slot(str)
    def checkHardware(self, label: str) -> None:
        self._session.checkHardware(label)

    @Slot()
    def findStraySessions(self) -> None:
        self._session.findStraySessions()

    @Slot(str, str)
    def cancelStraySession(self, label: str, slug: str) -> None:
        self._session.cancelStraySession(label, slug)

    # ---- the three that need Qt --------------------------------------
    @Slot(result=str)
    def pickBlend(self) -> str:
        """Open the OS file chooser and remember the choice.

        A native dialog rather than an <input type=file>: the page is not
        given filesystem access, and the app needs a real path to upload,
        not a sandboxed File object. Everything after the dialog is
        `setBlend`, including "a dismissed chooser leaves the previous
        choice standing".
        """
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            None, "Select .blend", "", "Blender (*.blend)")
        return self._session.setBlend(path)

    @Slot(str)
    @Slot(str, str)
    def collect(self, label: str = "", job_id: str = "") -> None:
        """Ask where the frames should go, then download exactly one job's.

        Which job, and why one rather than all of them, is
        `Session.collect`'s to explain -- it carries the whole history of
        that decision. This adds only the directory chooser, and returns
        without collecting if it is dismissed.
        """
        from PySide6.QtWidgets import QFileDialog
        destination = QFileDialog.getExistingDirectory(None, "Save frames to")
        if not destination:
            return
        self._session.collect(label, job_id, destination)

    @Slot(str, str)
    def setPreference(self, key: str, value: str) -> None:
        """Save a preference, then restyle the window around the page.

        The page restyles itself from its own CSS variables when it hears
        `settingsChanged`, but the WINDOW is Qt: the title bar, the ground
        behind the view, the Mica/dark-chrome flags, and any dialog
        (SetupDialog, QFileDialog) opened later. Saving without applying
        leaves all of that on the old theme, which is exactly the
        "background does not change" bug. `theme.apply()` also fires
        theme_signal, which is what WebHost listens to.

        Applied BEFORE the page hears anything: `settingsChanged` is
        relayed through a queued connection, so it is delivered after this
        method returns. The window and the page therefore change together
        rather than one lagging the other.
        """
        self._session.setPreference(key, value)
        if key in ("theme", "accent", "font"):
            from PySide6.QtWidgets import QApplication
            from blendfleet.ui import theme as theme_module
            app = QApplication.instance()
            if app is not None:
                theme_module.apply(app, self.settings.accent,
                                   self.settings.theme, self.settings.font)
