from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                               QFormLayout, QSpinBox, QComboBox, QPushButton,
                               QLabel, QFileDialog, QMessageBox, QTableWidget,
                               QTableWidgetItem, QProgressBar, QHeaderView)

from blendfleet.accounts import AccountStore
from blendfleet.assignment import estimate
from blendfleet.fleet import Fleet, FleetBusyError
from blendfleet.log_stream import stream_progress
from blendfleet.notebook_builder import RenderSettings
from blendfleet.ui.setup_dialog import SetupDialog

SETTINGS_URL = "https://www.kaggle.com/settings"
SECONDS_PER_FRAME_DEFAULT = 57.1     # measured: 1920x1080, 128spp, Tesla P100


class Dashboard(QMainWindow):
    def __init__(self, store: AccountStore, fleet_factory) -> None:
        super().__init__()
        self.store = store
        self.fleet_factory = fleet_factory
        self.blend: Path | None = None
        self._stop = threading.Event()
        # Keyed by kernel_slug (stable across polls) rather than kept on the
        # WorkerState instance: fleet.poll() rebuilds fresh WorkerState
        # objects from disk every timer tick, which would otherwise orphan
        # the objects the SSE threads are mutating and reset progress to 0
        # on the very next poll.
        self._live_progress: dict[str, int] = {}
        # Keyed by account label. Populated by _refresh_quota(), which is
        # best-effort: a fetch failure for one or all accounts must never
        # raise -- it only ever downgrades the displayed figure to
        # "unavailable" (see FINDING 1, task 9 fix round 1).
        self._quota_cache: dict[str, str] = {}
        self.setWindowTitle("BlendFleet")
        self.resize(900, 620)

        root = QWidget(); v = QVBoxLayout(root); self.setCentralWidget(root)

        top = QHBoxLayout()
        self.acct_label = QLabel()
        mgr = QPushButton("Manage accounts…"); mgr.clicked.connect(self._manage)
        top.addWidget(self.acct_label, 1); top.addWidget(mgr)
        v.addLayout(top)

        form = QFormLayout()
        self.file_label = QLabel("<i>no .blend selected</i>")
        browse = QPushButton("Browse…"); browse.clicked.connect(self._pick)
        fr = QHBoxLayout(); fr.addWidget(self.file_label, 1); fr.addWidget(browse)
        holder = QWidget(); holder.setLayout(fr)
        form.addRow("Project", holder)

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
        self.render_btn.clicked.connect(self._launch)
        self.cancel_btn = QPushButton("Cancel all")
        self.cancel_btn.clicked.connect(self._cancel)
        self.collect_btn = QPushButton("Collect frames…")
        self.collect_btn.clicked.connect(self._collect)
        for b in (self.render_btn, self.cancel_btn, self.collect_btn):
            btns.addWidget(b)
        v.addLayout(btns)

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

        self.timer = QTimer(self); self.timer.timeout.connect(self._poll)
        self.timer.start(30_000)
        self._refresh_accounts(); self._update_eta(); self._refresh_quota()

    # --- helpers ---
    def _refresh_accounts(self) -> None:
        n = len(self.store.list())
        names = ", ".join(a.label for a in self.store.list()) or "none"
        self.acct_label.setText(f"<b>{n} account(s):</b> {names}")

    def _update_eta(self) -> None:
        n = max(len(self.store.list()), 1)
        frames = max(self.end.value() - self.start.value() + 1, 0)
        hours = estimate(frames, SECONDS_PER_FRAME_DEFAULT, n)
        self.eta.setText(
            f"{frames} frames across {n} account(s) ≈ <b>{hours:.1f} h</b> each "
            f"(at {SECONDS_PER_FRAME_DEFAULT:.0f}s/frame measured on a P100 at "
            f"1920×1080/128spp — your scene will differ)")

    def _manage(self) -> None:
        SetupDialog(self.store, self).exec()
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
            self.file_label.setText(self.blend.name)

    def _launch(self) -> None:
        if not self.store.list():
            QMessageBox.warning(self, "No accounts", "Add at least one account.")
            return
        if self.blend is None:
            QMessageBox.warning(self, "No project", "Select a .blend first.")
            return
        if self.end.value() < self.start.value():
            QMessageBox.warning(self, "Bad range", "End frame is before start.")
            return
        settings = RenderSettings(self.rx.value(), self.ry.value(),
                                  self.spp.value(), self.fmt.currentText())
        self.render_btn.setEnabled(False)
        try:
            fleet = self.fleet_factory(self.store.list())
            st = fleet.launch(self.blend, settings,
                              self.start.value(), self.end.value())
            self._refresh_quota()
            self._render_table(st)
            self._start_progress_threads(st)
        except FleetBusyError as e:
            QMessageBox.warning(self, "Render already running", str(e))
        except Exception as e:
            QMessageBox.critical(self, "Launch failed", str(e))
        finally:
            self.render_btn.setEnabled(True)

    def _start_progress_threads(self, st) -> None:
        """One daemon thread per worker, reading its SSE log stream live.
        `kernels logs`/`kernels output` return nothing until COMPLETE
        (verified 2026-07-31), so this is the only source of live progress.
        """
        self._live_progress.clear()
        for acct, w in zip(self.store.list(), st.workers):
            def run(acct=acct, w=w):
                def bump(done, total):
                    w.frames_done = done
                    self._live_progress[w.kernel_slug] = done
                try:
                    stream_progress(acct.token, w.username,
                                    w.kernel_slug.split("/", 1)[1], bump,
                                    self._stop)
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
            QMessageBox.warning(self, "Cancel failed", str(e))
            return
        results = list(results or [])
        if not results:
            QMessageBox.information(self, "Nothing to cancel",
                                    "No job found to cancel.")
            return
        failed = [r for r in results if not r.ok]
        if not failed:
            QMessageBox.information(
                self, "Cancelled",
                f"Cancel requested for {len(results)} kernel(s).")
            return
        # A silent cancel failure is the worst outcome in the app: the user
        # believes the render stopped while it keeps draining a friend's
        # weekly GPU quota. Name every account that did not stop.
        detail = "\n".join(f"• {r.label} ({r.kernel_slug}): {r.error}"
                           for r in failed)
        QMessageBox.warning(
            self, "Cancel FAILED",
            f"{len(failed)} of {len(results)} kernel(s) were NOT cancelled and "
            f"may still be running:\n\n{detail}\n\n"
            f"Stop them manually at kaggle.com → the notebook → Stop session.")

    def _collect(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Save frames to")
        if not d:
            return
        from blendfleet.collector import collect
        fleet = self.fleet_factory(self.store.list())
        st = fleet.load()
        if st is None:
            QMessageBox.information(self, "Nothing to collect", "No job found.")
            return
        r = collect(st, self.store.list(), fleet.client_factory, Path(d))
        msg = f"Copied {r.copied} frame(s)."
        if r.missing_frames:
            msg += (f"\n\nSTILL MISSING {len(r.missing_frames)}: "
                    f"{r.missing_frames[:20]}"
                    f"{'…' if len(r.missing_frames) > 20 else ''}")
        QMessageBox.information(self, "Collected", msg)

    def _poll(self) -> None:
        try:
            fleet = self.fleet_factory(self.store.list())
            st = fleet.poll()
            if st:
                self._render_table(st)
        except Exception:
            pass          # a transient poll failure must not kill the dashboard
        try:
            self._refresh_quota()
        except Exception:
            pass          # same guarantee for the quota side-channel

    def _render_table(self, st) -> None:
        self.table.setRowCount(len(st.workers))
        for i, w in enumerate(st.workers):
            self.table.setItem(i, 0, QTableWidgetItem(w.label))
            self.table.setItem(i, 1, QTableWidgetItem(w.username))
            self.table.setItem(i, 2, QTableWidgetItem(
                self._quota_cache.get(w.label, "—")))
            self.table.setItem(i, 3, QTableWidgetItem(
                f"{len(w.frames)} ({w.frames[0] if w.frames else '-'}…)"))
            self.table.setItem(i, 4, QTableWidgetItem(w.state))
            bar = QProgressBar()
            bar.setMaximum(max(len(w.frames), 1))
            # w.frames_done reflects only what was persisted to disk; a
            # freshly-loaded WorkerState from fleet.poll() starts at 0 even
            # while the SSE thread is live-updating a separate instance, so
            # the dashboard-level cache is the authoritative live value.
            bar.setValue(max(self._live_progress.get(w.kernel_slug, 0),
                             w.frames_done))
            self.table.setCellWidget(i, 5, bar)

    def closeEvent(self, event) -> None:
        self._stop.set()
        super().closeEvent(event)
