from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton,
                               QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from blendfleet.accounts import AccountStore
from blendfleet.assignment import estimate
from blendfleet.fleet import Fleet, FleetState
from blendfleet.instance_state import GpuSnapshot, InstanceSnapshot, InstanceStore
from blendfleet.log_stream import stream_progress
from blendfleet.notebook_builder import RenderSettings
from blendfleet.ui.charts import Filmstrip, GpuPanel
from blendfleet.ui.messages import explain
from blendfleet.ui.setup_dialog import SetupDialog
from blendfleet.ui.theme import (ACCENT, WARNING, account_color, brand_icon,
                                  mono_font)
from blendfleet.ui.upload_view import UploadView

SETTINGS_URL = "https://www.kaggle.com/settings"
SECONDS_PER_FRAME_DEFAULT = 57.1     # measured: 1920x1080, 128spp, Tesla P100
POLL_INTERVAL_MS = 30_000            # real network calls: kernel status, quota
LIVE_INTERVAL_MS = 2_000             # cheap: drain in-memory progress/telemetry
# Per-thread budget for closeEvent to wait on an SSE log-stream thread.
# log_stream closes the response within STOP_POLL_SECONDS of `_stop` being
# set, so this is generous; it is a bound on shutdown, not the expected wait.
STREAM_JOIN_TIMEOUT_S = 3.0


class _AccountRow(QWidget):
    """One rail entry: a verification status (symbol + word, never colour
    alone) plus the account's own tint colour -- the same colour that
    tints its frames in the filmstrip below, so the rail doubles as a
    legend."""

    def __init__(self, index: int, account, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 8, 6)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {account_color(index).name()}; font-size: 13pt;")
        self.status = QLabel()
        self.status.setFont(mono_font(9))
        self.name = QLabel(account.label)
        h.addWidget(dot)
        h.addWidget(self.status)
        h.addWidget(self.name, 1)
        self.set_verified(account.verified)

    def set_verified(self, verified: bool) -> None:
        # The word is part of the visible label, not a tooltip: a tooltip
        # is invisible to anyone not hovering, and to screen readers in
        # many configurations, which defeats the entire "symbol + word"
        # rule this exists for (see SetupDialog._refresh, which this
        # matches).
        if verified:
            self.status.setText("✓ verified")
            self.status.setStyleSheet(f"color: {ACCENT};")
        else:
            self.status.setText("✗ not verified")
            self.status.setStyleSheet(f"color: {WARNING};")


class _LaunchWorker(QThread):
    """Runs Fleet.launch() off the UI thread.

    Fleet.launch() does the owner's .blend upload synchronously (one PUT
    that can take a long time for a large file, see uploader.py) plus
    several more Kaggle API round trips (share grants, kernel pushes) --
    all genuine network I/O. Calling it directly from a button handler is
    exactly the frozen-window bug this branch exists to fix, so it runs
    here instead, on its own thread, the same pattern setup_dialog.py
    already uses for account verification.
    """

    progress = Signal(object)   # blendfleet.uploader.UploadProgress
    succeeded = Signal(object)  # blendfleet.fleet.FleetState
    failed = Signal(str)

    def __init__(self, fleet: Fleet, blend: Path, settings: RenderSettings,
                 start_frame: int, end_frame: int, parent=None) -> None:
        super().__init__(parent)
        self._fleet = fleet
        self._blend = blend
        self._settings = settings
        self._start_frame = start_frame
        self._end_frame = end_frame

    def run(self) -> None:
        try:
            st = self._fleet.launch(
                self._blend, self._settings, self._start_frame,
                self._end_frame, on_progress=self.progress.emit)
        except Exception as e:  # noqa: BLE001 -- turned into a friendly message
            self.failed.emit(explain("Starting the render", e))
        else:
            self.succeeded.emit(st)


class _CallWorker(QThread):
    """Runs one arbitrary no-argument callable off the UI thread.

    This is the same pattern as _LaunchWorker (and setup_dialog.py's
    _VerifyWorker) generalised for every OTHER action that makes a real
    Kaggle API call: polling kernel status, refreshing quota, cancelling,
    collecting frames. All four used to run straight on the button-click/
    timer-tick handler, which is exactly the frozen-window bug this branch
    exists to fix -- see FINDING 1, task 5 fix round 1. `action` is a
    gerund phrase ("Checking render status", "Cancelling the render", ...)
    used to build a friendly message via blendfleet.ui.messages.explain if
    `fn` raises; callers never see a raw exception on `failed`.
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
        except Exception as e:  # noqa: BLE001 -- turned into a friendly message
            self.failed.emit(explain(self._action, e))
        else:
            self.succeeded.emit(result)


class Dashboard(QMainWindow):
    def __init__(self, store: AccountStore, fleet_factory, verifier) -> None:
        super().__init__()
        self.store = store
        self.fleet_factory = fleet_factory
        self.verifier = verifier
        self.blend: Path | None = None
        self._stop = threading.Event()
        # Every SSE log-stream thread started by _start_progress_threads,
        # so closeEvent can join them instead of leaving N daemon threads
        # mid-TLS while Qt destroys the window underneath them.
        self._stream_threads: list[threading.Thread] = []
        self._launch_worker: _LaunchWorker | None = None
        # One in-flight _CallWorker per action -- kept so a periodic tick
        # (poll/quota) can skip rather than stack a new worker on top of a
        # still-running one, and so closeEvent() has something to wait on.
        self._poll_worker: _CallWorker | None = None
        self._quota_worker: _CallWorker | None = None
        self._cancel_worker: _CallWorker | None = None
        self._collect_worker: _CallWorker | None = None
        self._last_state: FleetState | None = None
        # Keyed by kernel_slug (stable across polls) rather than kept on the
        # WorkerState instance: fleet.poll() rebuilds fresh WorkerState
        # objects from disk every timer tick, which would otherwise orphan
        # the objects the SSE threads are mutating and reset progress to 0
        # on the very next poll.
        self._live_progress: dict[str, int] = {}
        # Filled by the background SSE threads, drained on the UI thread by
        # _drain_telemetry() -- widgets must only ever be touched from the
        # UI thread, so telemetry never goes straight from a worker thread
        # into GpuPanel.
        self._telemetry_queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        # Last-known hardware per account, cached across app restarts so an
        # idle card (Task 4) can show what an account last ran on -- Kaggle
        # has no idle instances to poll instead. Loaded once here; every
        # write goes through _record_instance_snapshot below.
        self.instance_store = InstanceStore.load()
        # Per-run accumulator: label -> {gpu index -> mem_total}, built up
        # from telemetry as it arrives so a multi-GPU account's snapshot
        # reflects every GPU seen, not just whichever one's line happened
        # to be first. Reset in _start_progress_threads, i.e. as each new
        # render starts.
        self._instance_gpus: dict[str, dict[int, int]] = {}
        # Labels already persisted for the CURRENT run. Recording once per
        # account per run (not once per telemetry sample, which arrives
        # every ~5s for as long as the render runs) is the whole point --
        # see _record_instance_snapshot.
        self._recorded_instance_labels: set[str] = set()
        # Keyed by account label. Populated by _refresh_quota_async(),
        # which is best-effort: a fetch failure for one or all accounts
        # must never raise -- it only ever downgrades the displayed
        # figure to "unavailable" (see FINDING 1, task 9 fix round 1).
        self._quota_cache: dict[str, str] = {}
        self.setWindowTitle("BlendFleet")
        self.resize(1180, 760)

        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(root)

        outer.addWidget(self._build_rail())
        outer.addWidget(self._build_main(), 1)

        # Real network calls (kernel status, quota) -- infrequent.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(POLL_INTERVAL_MS)
        # Cheap in-memory refresh (live SSE progress + telemetry) --
        # frequent, so the filmstrip and GPU panel feel live.
        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self._live_tick)
        self.live_timer.start(LIVE_INTERVAL_MS)

        self._refresh_accounts()
        self._update_eta()
        self._refresh_quota_async()
        self._refresh_views()

    # ---------------- layout ----------------
    def _build_rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("rail")
        rail.setFixedWidth(230)
        v = QVBoxLayout(rail)
        v.setContentsMargins(0, 8, 0, 8)

        # The brand mark, tinted to the active accent (see theme.brand_icon)
        # rather than shipped as a fixed-colour logo -- so it belongs to the
        # app's own chrome and follows whichever accent the user picked,
        # instead of reading as a sticker pasted over it.
        brand = QHBoxLayout()
        brand.setContentsMargins(8, 0, 8, 8)
        brand.setSpacing(8)
        mark = QLabel()
        mark.setPixmap(brand_icon(ACCENT, 28).pixmap(28, 28))
        brand.addWidget(mark)
        brand.addWidget(QLabel("<b>BlendFleet</b>"), 1)
        v.addLayout(brand)

        title = QLabel("<b>accounts</b>")
        title.setContentsMargins(8, 0, 8, 4)
        v.addWidget(title)

        self.rail_rows_holder = QWidget()
        self.rail_rows_layout = QVBoxLayout(self.rail_rows_holder)
        self.rail_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rail_rows_layout.setSpacing(0)
        v.addWidget(self.rail_rows_holder)
        v.addStretch(1)

        add_btn = QPushButton("+ add account")
        add_btn.clicked.connect(self._manage)
        v.addWidget(add_btn)
        self.rail = rail
        return rail

    def _build_main(self) -> QWidget:
        main = QWidget()
        v = QVBoxLayout(main)
        v.setContentsMargins(16, 12, 16, 12)

        top = QHBoxLayout()
        self.project_label = QLabel("<b>no project selected</b>")
        browse = QPushButton("Browse for .blend…")
        browse.clicked.connect(self._pick)
        top.addWidget(self.project_label, 1)
        top.addWidget(browse)
        v.addLayout(top)

        form = QFormLayout()
        self.start = QSpinBox(); self.start.setRange(1, 1000000); self.start.setValue(1)
        self.end = QSpinBox(); self.end.setRange(1, 1000000); self.end.setValue(250)
        self.rx = QSpinBox(); self.rx.setRange(64, 8192); self.rx.setValue(1920)
        self.ry = QSpinBox(); self.ry.setRange(64, 8192); self.ry.setValue(1080)
        self.spp = QSpinBox(); self.spp.setRange(1, 16384); self.spp.setValue(128)
        self.fmt = QComboBox(); self.fmt.addItems(["PNG", "JPEG"])
        for lbl, wdg in (("Start frame", self.start), ("End frame", self.end),
                         ("Width", self.rx), ("Height", self.ry),
                         ("Samples", self.spp), ("Format", self.fmt)):
            form.addRow(lbl, wdg)
        v.addLayout(form)

        self.eta = QLabel(); v.addWidget(self.eta)
        for w in (self.start, self.end):
            w.valueChanged.connect(self._update_eta)

        btns = QHBoxLayout()
        self.render_btn = QPushButton("RENDER ACROSS FLEET")
        self.render_btn.setObjectName("primaryButton")
        self.render_btn.clicked.connect(self._launch)
        self.cancel_btn = QPushButton("Cancel all")
        self.cancel_btn.clicked.connect(self._cancel)
        self.collect_btn = QPushButton("Collect frames…")
        self.collect_btn.clicked.connect(self._collect)
        for b in (self.render_btn, self.cancel_btn, self.collect_btn):
            btns.addWidget(b)
        v.addLayout(btns)

        # The "approximate" wording is not hedging -- it is the honest
        # description of what this widget can know. See charts.frame_done:
        # the notebook reports a COUNT of successful frames, not which ones,
        # so the strip assumes the first N of each account's stride are the
        # finished ones. That holds exactly until a frame fails, after which
        # every later cell for that account is shifted by one. Saying so
        # here is the fix the review asked for: the user must not read a
        # green cell as proof that that specific frame exists.
        filmstrip_header = QLabel(
            "<b>Filmstrip</b> — one cell per frame, tinted by which account "
            "rendered it. Completed cells are <b>approximate</b>: the render "
            "reports how many frames succeeded, not which, so a failed frame "
            "shifts every later cell for that account. Collect frames… is the "
            "authoritative list of what actually exists.")
        filmstrip_header.setWordWrap(True)
        v.addWidget(filmstrip_header)
        self.filmstrip = Filmstrip()
        v.addWidget(self.filmstrip)
        self.filmstrip_caption = QLabel("no frames yet")
        self.filmstrip_caption.setFont(mono_font(9))
        self.filmstrip_caption.setProperty("secondary", True)
        v.addWidget(self.filmstrip_caption)

        v.addWidget(QLabel("<b>Upload</b>"))
        self.upload_view = UploadView()
        v.addWidget(self.upload_view)

        v.addWidget(QLabel("<b>GPU</b> — one row per physical GPU, never combined"))
        self.gpu_panel = GpuPanel()
        v.addWidget(self.gpu_panel)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Account", "Kaggle user", "Quota (API)", "Frames", "State", "Progress"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table)

        # A visible degraded-state marker for the periodic status poll --
        # see FINDING 3, task 5 fix round 1: a poll failure used to be
        # swallowed completely silently, with nothing like quota's
        # "unavailable" fallback. Hidden (empty) whenever the last poll
        # succeeded.
        self.poll_status_label = QLabel("")
        self.poll_status_label.setWordWrap(True)
        self.poll_status_label.setStyleSheet(f"color: {WARNING};")
        v.addWidget(self.poll_status_label)

        note = QLabel(
            f'The <b>Quota (API)</b> column above is exactly that: the figure '
            f'the Kaggle API reports right now, not a guarantee. It has been '
            f'observed to disagree with <a href="{SETTINGS_URL}">your settings '
            f'page</a> — check both before a long run.')
        note.setOpenExternalLinks(True)
        note.setWordWrap(True)
        v.addWidget(note)
        return main

    # --- helpers ---
    def _refresh_accounts(self) -> None:
        while self.rail_rows_layout.count():
            item = self.rail_rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for i, a in enumerate(self.store.list()):
            self.rail_rows_layout.addWidget(_AccountRow(i, a))

    def _update_eta(self) -> None:
        n = max(len(self.store.list()), 1)
        frames = max(self.end.value() - self.start.value() + 1, 0)
        hours = estimate(frames, SECONDS_PER_FRAME_DEFAULT, n)
        self.eta.setText(
            f"{frames} frames across {n} account(s) ≈ <b>{hours:.1f} h</b> each "
            f"(at {SECONDS_PER_FRAME_DEFAULT:.0f}s/frame measured on a P100 at "
            f"1920×1080/128spp — your scene will differ)")

    def _manage(self) -> None:
        SetupDialog(self.store, self.verifier, self).exec()
        self._refresh_accounts(); self._update_eta(); self._refresh_quota_async()

    def _refresh_quota_async(self) -> None:
        """Best-effort per-account GPU quota fetch, straight from
        KaggleClient.quota() -- run off the UI thread (FINDING 1, task 5
        fix round 1: this used to block the window on one real HTTP round
        trip per account). Skips rather than stacks a second worker if a
        previous quota fetch is still in flight (e.g. the 30s poll timer
        firing again before a slow fetch returned).

        The work callable is itself already best-effort and never allowed
        to raise: a single account's fetch failing (rate limit, network
        blip, revoked token, a fake/stub client_factory with no quota() at
        all) must not break the dashboard or block a render -- it only
        ever downgrades that account's figure to "unavailable".
        """
        if self._quota_worker is not None:
            return
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
                    used_h = q.used_seconds / 3600.0
                    total_h = q.total_seconds / 3600.0
                    result[acct.label] = f"{used_h:.1f} / {total_h:.1f} h"
                except Exception:
                    result[acct.label] = "unavailable"
            return result

        worker = _CallWorker(work, "Refreshing quota", self)
        self._quota_worker = worker

        def done_ok(result: dict) -> None:
            self._quota_worker = None
            self._quota_cache.update(result)
            self._refresh_views()

        def done_fail(_message: str) -> None:
            # work() above never actually raises (every account is
            # individually guarded), so this is belt-and-braces only.
            self._quota_worker = None

        worker.succeeded.connect(done_ok)
        worker.failed.connect(done_fail)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _pick(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Select .blend", "",
                                           "Blender (*.blend)")
        if f:
            self.blend = Path(f)
            self.project_label.setText(f"<b>{self.blend.name}</b>")

    # ---------------- launch (off the UI thread) ----------------
    def _launch(self) -> None:
        if not self.store.list():
            QMessageBox.warning(
                self, "No accounts added",
                "There is nothing to render with yet: add at least one "
                "Kaggle account under “+ add account”, then try again.")
            return
        if self.blend is None:
            QMessageBox.warning(
                self, "No project selected",
                "Choose a .blend file first (“Browse for .blend…” "
                "above), then start the render.")
            return
        if self.end.value() < self.start.value():
            QMessageBox.warning(
                self, "Frame range is invalid",
                f"The end frame ({self.end.value()}) is before the start "
                f"frame ({self.start.value()}). Fix the range and try again.")
            return
        settings = RenderSettings(self.rx.value(), self.ry.value(),
                                  self.spp.value(), self.fmt.currentText())
        owner_label = self.store.list()[0].label

        try:
            fleet = self.fleet_factory(self.store.list())
        except Exception as e:
            QMessageBox.critical(self, "Could not start the render",
                                 explain("Starting the render", e))
            return

        self.render_btn.setEnabled(False)
        self.render_btn.setText("Starting…")
        self.upload_view.clear()
        self.upload_view.ensure_row(owner_label)

        self._launch_worker = _LaunchWorker(
            fleet, self.blend, settings, self.start.value(), self.end.value(),
            self)
        self._launch_worker.progress.connect(
            lambda p: self.upload_view.update_progress(owner_label, p))
        self._launch_worker.succeeded.connect(self._on_launch_succeeded)
        self._launch_worker.failed.connect(
            lambda msg: self._on_launch_failed(owner_label, msg))
        self._launch_worker.finished.connect(self._launch_worker.deleteLater)
        self._launch_worker.start()

    def _on_launch_succeeded(self, st: FleetState) -> None:
        # Cleared here (not just left to worker.finished.connect(deleteLater)):
        # the Python attribute would otherwise go on pointing at a QThread
        # whose C++ object Qt has already destroyed, and closeEvent()'s
        # teardown loop calling .isRunning() on that stale reference raises
        # shiboken's "already deleted" RuntimeError.
        self._launch_worker = None
        owner_label = self.store.list()[0].label if self.store.list() else ""
        if owner_label:
            self.upload_view.set_complete(owner_label)
        self.render_btn.setEnabled(True)
        self.render_btn.setText("RENDER ACROSS FLEET")
        self._live_progress.clear()
        self._last_state = st
        self._refresh_quota_async()
        self._refresh_views()
        self._start_progress_threads(st)

    def _on_launch_failed(self, owner_label: str, message: str) -> None:
        self._launch_worker = None
        self.upload_view.set_failed(owner_label, message)
        self.render_btn.setEnabled(True)
        self.render_btn.setText("RENDER ACROSS FLEET")
        QMessageBox.critical(self, "Render did not start", message)

    def _start_progress_threads(self, st: FleetState) -> None:
        """One daemon thread per worker, reading its SSE log stream live.
        `kernels logs`/`kernels output` return nothing until COMPLETE
        (verified 2026-07-31), so this is the only source of live progress
        and the only source of live per-GPU telemetry.

        Workers are matched to accounts by LABEL, never by position.
        zip(self.store.list(), st.workers) silently mispairs the moment the
        two lists stop lining up -- remove an account between launching and
        the launch returning and every later worker would be streamed with
        the wrong person's token, which is both a privacy leak and a stream
        that simply 403s. Label is the join key everywhere else in this app
        (fleet.poll, fleet.cancel_all, collector.collect); it is the join
        key here too.
        """
        self._live_progress.clear()
        # A new render means new hardware may be handed out (Kaggle's
        # allocation varies run to run) -- so the "already recorded this
        # run" guard and its accumulator both reset here, per render.
        self._instance_gpus.clear()
        self._recorded_instance_labels.clear()
        # Drop the threads from the previous job that have already unwound,
        # so a long session's worth of renders does not accumulate dead
        # Thread objects that closeEvent then walks every time.
        self._stream_threads = [t for t in self._stream_threads if t.is_alive()]
        by_label = {a.label: a for a in self.store.list()}
        for w in st.workers:
            acct = by_label.get(w.label)
            if acct is None:
                # The account was removed while the launch was in flight.
                # No token, so no stream -- but the kernel is already
                # running and still shows in the table; skipping is the
                # only safe option, streaming it with somebody else's
                # token is not.
                continue

            def run(acct=acct, w=w):
                def bump(done, total):
                    w.frames_done = done
                    self._live_progress[w.kernel_slug] = done

                def telemetry(record, label=acct.label):
                    # Only touches a thread-safe queue here -- GpuPanel is a
                    # widget and must only ever be updated from the UI
                    # thread; _live_tick() drains this on a QTimer instead.
                    self._telemetry_queue.put((label, record))

                try:
                    stream_progress(acct.token, w.username,
                                    w.kernel_slug.split("/", 1)[1], bump,
                                    self._stop, on_telemetry=telemetry)
                except Exception:
                    pass  # a dead stream must never kill the render or the UI
            thread = threading.Thread(target=run, daemon=True,
                                      name=f"blendfleet-log-stream-{w.label}")
            # Kept, not fired and forgotten: closeEvent has to be able to
            # WAIT for these. A daemon thread still inside SSL when the
            # interpreter tears down is what aborts the process (the same
            # leak that made the test suite non-deterministic), and
            # "daemon=True" only hides it, it does not prevent it.
            self._stream_threads.append(thread)
            thread.start()

    def _cancel(self) -> None:
        """Cancel is user-initiated and its entire purpose is stopping
        other people's GPU quota from draining -- of everything in this
        app, it is the one action that must never freeze the window at
        the moment the user needs it to respond (FINDING 1, task 5 fix
        round 1). The button is disabled for the duration and re-enabled
        from both the success and failure paths, so the user always gets
        feedback instead of a dead window.
        """
        if QMessageBox.question(self, "Cancel all",
                                "Stop every running render?") != \
                QMessageBox.StandardButton.Yes:
            return
        if self._cancel_worker is not None:
            return   # a cancel is already in flight -- the button is disabled too
        accounts = self.store.list()

        def work():
            return self.fleet_factory(accounts).cancel_all()

        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("Cancelling…")

        worker = _CallWorker(work, "Cancelling the render", self)
        self._cancel_worker = worker

        def done_ok(results) -> None:
            self._cancel_worker = None
            self.cancel_btn.setEnabled(True)
            self.cancel_btn.setText("Cancel all")
            self._show_cancel_results(results)

        def done_fail(message: str) -> None:
            self._cancel_worker = None
            self.cancel_btn.setEnabled(True)
            self.cancel_btn.setText("Cancel all")
            QMessageBox.warning(self, "Could not cancel", message)

        worker.succeeded.connect(done_ok)
        worker.failed.connect(done_fail)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _show_cancel_results(self, results) -> None:
        results = list(results or [])
        if not results:
            QMessageBox.information(
                self, "Nothing to cancel",
                "No render job was found -- there is nothing running to stop.")
            return
        failed = [r for r in results if not r.ok]
        if not failed:
            QMessageBox.information(
                self, "Render cancelled",
                f"Cancel requested for {len(results)} account(s); Kaggle "
                "confirmed the stop.")
            return
        # A silent cancel failure is the worst outcome in the app: the user
        # believes the render stopped while it keeps draining a friend's
        # weekly GPU quota. Name every account that did not stop.
        detail = "\n".join(f"• {r.label} ({r.kernel_slug}): {r.error}"
                           for r in failed)
        QMessageBox.warning(
            self, "Some renders did NOT stop",
            f"{len(failed)} of {len(results)} account(s) could not be "
            f"cancelled and may still be running, spending their GPU "
            f"quota:\n\n{detail}\n\n"
            f"Stop them by hand at kaggle.com → the notebook → Stop session.")

    def _collect(self) -> None:
        """Downloading rendered output (`fetch_output`) is real, and
        potentially slow, network I/O -- moved off the UI thread for the
        same reason as _cancel above (FINDING 1, task 5 fix round 1)."""
        d = QFileDialog.getExistingDirectory(self, "Save frames to")
        if not d:
            return
        if self._collect_worker is not None:
            return   # a collect is already in flight -- the button is disabled too
        from blendfleet.collector import collect
        accounts = self.store.list()

        def work():
            fleet = self.fleet_factory(accounts)
            st = fleet.load()
            if st is None:
                return None
            return collect(st, accounts, fleet.client_factory, Path(d))

        self.collect_btn.setEnabled(False)
        self.collect_btn.setText("Collecting…")

        worker = _CallWorker(work, "Collecting frames", self)
        self._collect_worker = worker

        def done_ok(r) -> None:
            self._collect_worker = None
            self.collect_btn.setEnabled(True)
            self.collect_btn.setText("Collect frames…")
            if r is None:
                QMessageBox.information(
                    self, "Nothing to collect",
                    "No render job was found -- start a render first.")
                return
            msg = f"Copied {r.copied} frame(s) to {d}."
            if r.missing_frames:
                msg += (f"\n\n{len(r.missing_frames)} frame(s) are still "
                        f"missing (not rendered yet, or the render failed "
                        f"for that account): {r.missing_frames[:20]}"
                        f"{'…' if len(r.missing_frames) > 20 else ''}\n\n"
                        "Collect again once those accounts finish.")
            QMessageBox.information(self, "Frames collected", msg)

        def done_fail(message: str) -> None:
            self._collect_worker = None
            self.collect_btn.setEnabled(True)
            self.collect_btn.setText("Collect frames…")
            QMessageBox.critical(self, "Could not collect frames", message)

        worker.succeeded.connect(done_ok)
        worker.failed.connect(done_fail)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    # ---------------- polling / live refresh ----------------
    def _poll(self) -> None:
        """Refresh every worker's kernel status from Kaggle.

        Runs off the UI thread (FINDING 1, task 5 fix round 1): this fires
        on a 30s timer and makes one real HTTP call per account
        (fleet.poll() -> KaggleClient.status()), so with even 3 accounts
        it used to be able to lock up the window on 3 blocking calls
        twice a minute, forever. Skips this tick entirely -- rather than
        stacking a second worker on top of a still-running one -- if the
        previous poll has not returned yet.

        A poll failure must still never kill the dashboard, but silently
        swallowing it (the old behaviour) left the user with no idea the
        table had gone stale -- see FINDING 3. poll_status_label now
        carries a visible, worded degraded-state message instead, cleared
        again the moment a poll succeeds.
        """
        if self._poll_worker is not None:
            return
        accounts = self.store.list()

        def work():
            return self.fleet_factory(accounts).poll()

        worker = _CallWorker(work, "Checking render status", self)
        self._poll_worker = worker

        def done_ok(st) -> None:
            self._poll_worker = None
            if st:
                self._last_state = st
            self.poll_status_label.setText("")
            self._refresh_views()

        def done_fail(message: str) -> None:
            self._poll_worker = None
            self.poll_status_label.setText(
                f"⚠ {message} Showing the last known render status.")
            self._refresh_views()

        worker.succeeded.connect(done_ok)
        worker.failed.connect(done_fail)
        worker.finished.connect(worker.deleteLater)
        worker.start()

        self._refresh_quota_async()

    def _live_tick(self) -> None:
        """Cheap, frequent refresh: drain telemetry samples collected by
        the background SSE threads into the GPU panel, and repaint the
        filmstrip/table with whatever live frame progress has arrived --
        no network calls here, unlike _poll().

        Recorded from this UI-thread drain side, not from the worker
        closure that fills _telemetry_queue: that closure deliberately
        does nothing but enqueue (widgets, and now the instance-state
        write, must only ever happen off the SSE thread).
        """
        drained = 0
        newly_seen: set[str] = set()
        while drained < 200:  # bounded: never let a stuck consumer spin forever
            try:
                label, record = self._telemetry_queue.get_nowait()
            except queue.Empty:
                break
            self.gpu_panel.ingest(label, record)
            self._instance_gpus.setdefault(label, {})[record["gpu"]] = record["mem_total"]
            if label not in self._recorded_instance_labels:
                newly_seen.add(label)
            drained += 1
        for label in newly_seen:
            self._record_instance_snapshot(label)
        self._refresh_views()

    def _record_instance_snapshot(self, label: str) -> None:
        """Persist one InstanceSnapshot for `label`, once for the current
        run, from telemetry accumulated so far in _instance_gpus.

        cpu_count and ram_total have no source through this wiring -- see
        blendfleet/instance_state.py's module docstring -- so they are left
        None rather than guessed.
        """
        self._recorded_instance_labels.add(label)
        account = next((a for a in self.store.list() if a.label == label), None)
        gpus = [GpuSnapshot(index=idx, mem_total=mem_total)
               for idx, mem_total in sorted(self._instance_gpus.get(label, {}).items())]
        snapshot = InstanceSnapshot(
            username=account.username if account else None,
            gpus=gpus, cpu_count=None, ram_total=None,
            observed_at=time.time())
        self.instance_store.record(label, snapshot)
        self.instance_store.save()

    def _refresh_views(self) -> None:
        st = self._last_state
        if st is None:
            self.filmstrip.set_empty()
            self.filmstrip_caption.setText("no frames yet")
            self.table.setRowCount(0)
            return
        for w in st.workers:
            live = self._live_progress.get(w.kernel_slug, 0)
            if live > w.frames_done:
                w.frames_done = live
        self._render_table(st)
        self.filmstrip.set_workers(st.start_frame, st.end_frame, st.workers)
        self.filmstrip_caption.setText(
            f"{self.filmstrip.done_count}/{self.filmstrip.total_frames} frames"
            f" · {len(st.workers)} account(s)")
        self.project_label.setText(
            f"<b>{st.blend_name}</b>  frames {st.start_frame}-{st.end_frame}")

    def _render_table(self, st: FleetState) -> None:
        # Quota and frame counts are machine data -- set in monospace with
        # tabular figures, per the type discipline in blendfleet.ui.theme,
        # so they read as an instrument panel rather than reflowing prose.
        mono = mono_font(9)
        self.table.setRowCount(len(st.workers))
        for i, w in enumerate(st.workers):
            self.table.setItem(i, 0, QTableWidgetItem(w.label))
            self.table.setItem(i, 1, QTableWidgetItem(w.username))
            quota_item = QTableWidgetItem(self._quota_cache.get(w.label, "—"))
            quota_item.setFont(mono)
            self.table.setItem(i, 2, quota_item)
            frames_item = QTableWidgetItem(
                f"{len(w.frames)} ({w.frames[0] if w.frames else '-'}…)")
            frames_item.setFont(mono)
            self.table.setItem(i, 3, frames_item)
            self.table.setItem(i, 4, QTableWidgetItem(w.state))
            bar = QProgressBar()
            bar.setMaximum(max(len(w.frames), 1))
            bar.setValue(max(self._live_progress.get(w.kernel_slug, 0),
                             w.frames_done))
            self.table.setCellWidget(i, 5, bar)

    def closeEvent(self, event) -> None:
        self._stop.set()          # tells the daemon SSE threads to unwind
        self.timer.stop()
        self.live_timer.stop()
        # Threads must not outlive the window (FINDING 1, task 5 fix
        # round 1): wait for whichever _CallWorker/_LaunchWorker happens
        # to be in flight rather than letting Qt destroy a QObject whose
        # thread is still running underneath it. Bounded so a genuinely
        # stuck network call cannot hang application shutdown forever.
        for worker in (self._launch_worker, self._poll_worker,
                       self._quota_worker, self._cancel_worker,
                       self._collect_worker):
            if worker is None:
                continue
            try:
                if worker.isRunning():
                    worker.wait(5000)
            except RuntimeError:
                # The worker finished and its deleteLater() was already
                # processed between the None-check above and this call --
                # the underlying C++ QThread is gone, which is exactly the
                # "not running any more" outcome we were waiting for.
                pass
        # The SSE threads too. `_stop` above makes each one's response get
        # closed (log_stream._close_when_stopped), so this join is short --
        # but it has to happen, because a daemon thread still inside SSL
        # when the process tears down is what produces
        # "Fatal Python error: Aborted".
        for thread in self._stream_threads:
            thread.join(timeout=STREAM_JOIN_TIMEOUT_S)
        self._stream_threads = [t for t in self._stream_threads if t.is_alive()]
        super().closeEvent(event)
