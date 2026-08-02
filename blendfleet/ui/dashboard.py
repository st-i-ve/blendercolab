from __future__ import annotations

import queue
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton,
                               QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from blendfleet.accounts import AccountStore
from blendfleet.assignment import estimate
from blendfleet.fleet import Fleet, FleetBusyError, FleetState
from blendfleet.log_stream import stream_progress
from blendfleet.notebook_builder import RenderSettings
from blendfleet.ui.charts import Filmstrip, GpuPanel
from blendfleet.ui.messages import explain
from blendfleet.ui.setup_dialog import SetupDialog
from blendfleet.ui.theme import ACCENT, WARNING, account_color, mono_font
from blendfleet.ui.upload_view import UploadView

SETTINGS_URL = "https://www.kaggle.com/settings"
SECONDS_PER_FRAME_DEFAULT = 57.1     # measured: 1920x1080, 128spp, Tesla P100
POLL_INTERVAL_MS = 30_000            # real network calls: kernel status, quota
LIVE_INTERVAL_MS = 2_000             # cheap: drain in-memory progress/telemetry


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
        if verified:
            self.status.setText("✓")
            self.status.setToolTip("verified")
            self.status.setStyleSheet(f"color: {ACCENT};")
        else:
            self.status.setText("✗")
            self.status.setToolTip("not verified")
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


class Dashboard(QMainWindow):
    def __init__(self, store: AccountStore, fleet_factory, verifier) -> None:
        super().__init__()
        self.store = store
        self.fleet_factory = fleet_factory
        self.verifier = verifier
        self.blend: Path | None = None
        self._stop = threading.Event()
        self._launch_worker: _LaunchWorker | None = None
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
        # Keyed by account label. Populated by _refresh_quota(), which is
        # best-effort: a fetch failure for one or all accounts must never
        # raise -- it only ever downgrades the displayed figure to
        # "unavailable" (see FINDING 1, task 9 fix round 1).
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
        self._refresh_quota()
        self._refresh_views()

    # ---------------- layout ----------------
    def _build_rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("rail")
        rail.setFixedWidth(200)
        v = QVBoxLayout(rail)
        v.setContentsMargins(0, 8, 0, 8)
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

        v.addWidget(QLabel("<b>Filmstrip</b> — one cell per frame, tinted by "
                           "which account rendered it"))
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
        self._refresh_accounts(); self._update_eta(); self._refresh_quota()

    def _refresh_quota(self) -> None:
        """Best-effort per-account GPU quota fetch, straight from
        KaggleClient.quota(). Never allowed to raise: a single account's
        fetch failing (rate limit, network blip, revoked token, a fake/stub
        client_factory with no quota() at all) must not break the dashboard
        or block a render -- it only ever downgrades that account's figure
        to "unavailable"."""
        try:
            client_factory = self.fleet_factory(self.store.list()).client_factory
        except Exception:
            client_factory = None
        for acct in self.store.list():
            if client_factory is None:
                self._quota_cache[acct.label] = "unavailable"
                continue
            try:
                q = client_factory(acct.token).quota()
                used_h = q.used_seconds / 3600.0
                total_h = q.total_seconds / 3600.0
                self._quota_cache[acct.label] = f"{used_h:.1f} / {total_h:.1f} h"
            except Exception:
                self._quota_cache[acct.label] = "unavailable"

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
        owner_label = self.store.list()[0].label if self.store.list() else ""
        if owner_label:
            self.upload_view.set_complete(owner_label)
        self.render_btn.setEnabled(True)
        self.render_btn.setText("RENDER ACROSS FLEET")
        self._live_progress.clear()
        self._last_state = st
        self._refresh_quota()
        self._refresh_views()
        self._start_progress_threads(st)

    def _on_launch_failed(self, owner_label: str, message: str) -> None:
        self.upload_view.set_failed(owner_label, message)
        self.render_btn.setEnabled(True)
        self.render_btn.setText("RENDER ACROSS FLEET")
        QMessageBox.critical(self, "Render did not start", message)

    def _start_progress_threads(self, st: FleetState) -> None:
        """One daemon thread per worker, reading its SSE log stream live.
        `kernels logs`/`kernels output` return nothing until COMPLETE
        (verified 2026-07-31), so this is the only source of live progress
        and the only source of live per-GPU telemetry.
        """
        self._live_progress.clear()
        for acct, w in zip(self.store.list(), st.workers):
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
            threading.Thread(target=run, daemon=True).start()

    def _cancel(self) -> None:
        if QMessageBox.question(self, "Cancel all",
                                "Stop every running render?") != \
                QMessageBox.StandardButton.Yes:
            return
        try:
            results = self.fleet_factory(self.store.list()).cancel_all()
        except Exception as e:
            QMessageBox.warning(self, "Could not cancel",
                                explain("Cancelling the render", e))
            return
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
        d = QFileDialog.getExistingDirectory(self, "Save frames to")
        if not d:
            return
        from blendfleet.collector import collect
        try:
            fleet = self.fleet_factory(self.store.list())
            st = fleet.load()
            if st is None:
                QMessageBox.information(
                    self, "Nothing to collect",
                    "No render job was found -- start a render first.")
                return
            r = collect(st, self.store.list(), fleet.client_factory, Path(d))
        except Exception as e:
            QMessageBox.critical(self, "Could not collect frames",
                                 explain("Collecting frames", e))
            return
        msg = f"Copied {r.copied} frame(s) to {d}."
        if r.missing_frames:
            msg += (f"\n\n{len(r.missing_frames)} frame(s) are still "
                    f"missing (not rendered yet, or the render failed for "
                    f"that account): {r.missing_frames[:20]}"
                    f"{'…' if len(r.missing_frames) > 20 else ''}\n\n"
                    "Collect again once those accounts finish.")
        QMessageBox.information(self, "Frames collected", msg)

    # ---------------- polling / live refresh ----------------
    def _poll(self) -> None:
        try:
            fleet = self.fleet_factory(self.store.list())
            st = fleet.poll()
            if st:
                self._last_state = st
        except Exception:
            pass          # a transient poll failure must not kill the dashboard
        try:
            self._refresh_quota()
        except Exception:
            pass          # same guarantee for the quota side-channel
        self._refresh_views()

    def _live_tick(self) -> None:
        """Cheap, frequent refresh: drain telemetry samples collected by
        the background SSE threads into the GPU panel, and repaint the
        filmstrip/table with whatever live frame progress has arrived --
        no network calls here, unlike _poll()."""
        drained = 0
        while drained < 200:  # bounded: never let a stuck consumer spin forever
            try:
                label, record = self._telemetry_queue.get_nowait()
            except queue.Empty:
                break
            self.gpu_panel.ingest(label, record)
            drained += 1
        self._refresh_views()

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
        self._stop.set()
        super().closeEvent(event)
