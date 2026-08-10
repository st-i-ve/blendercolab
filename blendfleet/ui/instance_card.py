"""One card per Kaggle account: the "SaaS dashboard" view of quota and
hardware -- and the one place in the app where the honesty rule in
blendfleet/instance_state.py's module docstring has to be enforced visibly,
not just documented.

THE GOVERNING FACT: Kaggle has no idle instances. A session exists only
while a kernel is running; between renders there is no machine to poll.
So a card can genuinely show:

  - quota            -- live, free to poll, sourced from KaggleClient.quota()
                         via dashboard.py's existing quota cache.
  - last-known hardware -- cached in InstanceStore, labelled with its age
                         (see format_age below), because Kaggle's own GPU
                         allocation varies between runs (a P100 last time
                         does not mean a P100 next time).
  - live util/VRAM per GPU -- ONLY while that account's kernel is actually
                         running and telemetry is arriving for it.

There is no live RAM-used row, and this is deliberate, not an oversight:
notebook_builder.py's telemetry thread queries nvidia-smi only -- CPU/RAM
figures are printed exactly once, at the start of a run (the "hardware
banner" instance_state.py's module docstring describes), and log_stream.py
has no line anywhere that carries a live RAM-used sample. A "RAM" gauge
with a moving needle would have nothing genuine feeding it; ram_total only
ever appears inside the cached hardware line below, in prose, never dressed
up as a live figure.

set_worker(None) is what "idle" means: no active WorkerState for this
account. Idle, queued, errored, cancelled and completed workers all show
the CACHED body (last-known hardware) -- only "running" switches to the
LIVE body (per-GPU rows + frame count), because that is the only state in
which telemetry can possibly be arriving. Switching the body is done by
hiding one QWidget and showing the other, not by destroying/rebuilding
widgets, so a Sparkline's ring buffer survives every intervening idle tick
and a card's identity never resets just because a poll came in.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

from blendfleet.accounts import Account
from blendfleet.downloader import DownloadProgress
from blendfleet.fleet import WorkerState
from blendfleet.instance_state import InstanceSnapshot
from blendfleet.ui.charts import Sparkline
from blendfleet.ui.formatting import format_bytes, format_eta, format_rate
from blendfleet.ui.messages import explain_kernel_failure
from blendfleet.ui.theme import (TELEMETRY, TEXT_SECONDARY, WARNING,
                                  account_color, current_accent, icon,
                                  mono_font, theme_signal, ui_font)

# States fleet.WorkerState.state can carry -- kaggle_client.ACTIVE_STATES
# duplicated as a literal set here (not imported) would tie this UI module
# to that one module's naming; the strings themselves are Kaggle's own
# vocabulary (kernels_status's normalised .state) and are already spelled
# out identically in charts.py's _STOPPED_STATES, so this follows the same
# convention rather than inventing a third.
_RUNNING_STATE = "running"
_QUEUED_STATE = "queued"
_ERROR_STATE = "error"
_COMPLETE_STATE = "complete"
_CANCELLED_STATES = {"cancel_requested", "cancel_acknowledged"}
# A kernel in any of these states has definitely stopped -- no telemetry
# can legitimately arrive for it again until a NEW worker (a new launch)
# replaces it. Used to reset the "telemetry has been seen this run" latch
# below, so a stale TELEMETRY sample racing in after cancel/error/complete
# can never resurrect a live gauge for a kernel that has already stopped.
_STOPPED_STATES = {_ERROR_STATE, _COMPLETE_STATE} | _CANCELLED_STATES

NEVER_RUN_TEXT = "never run — launch to see specs"


# ---------------- pure logic (unit-testable without a QPainter) ----------

def format_age(seconds: float) -> str:
    """A cached snapshot's age in words: "just now", "14m ago", "2h ago",
    "3d ago". Never negative (a clock skew or a snapshot recorded a moment
    after "now" was captured must not print "-1s ago")."""
    seconds = max(seconds, 0.0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def format_hardware_summary(snapshot: InstanceSnapshot) -> str:
    """"Tesla P100-PCIE-16GB 15.9 GB, 4 vCPU, 31.3 GB" -- whatever subset of
    gpus/cpu_count/ram_total actually arrived for this snapshot (see
    instance_state.py's module docstring: any of them can be missing if the
    hardware banner never reached the stream). A GPU with no model name
    (the banner never arrived, only telemetry did) reads as "GPU <size>"
    rather than a blank -- never guessed, never hidden."""
    parts: list[str] = []
    if snapshot.gpus:
        parts.append(", ".join(
            f"{g.model or 'GPU'} {format_bytes(g.mem_total * (1 << 20))}"
            for g in snapshot.gpus))
    if snapshot.cpu_count is not None:
        parts.append(f"{snapshot.cpu_count} vCPU")
    if snapshot.ram_total is not None:
        parts.append(f"{snapshot.ram_total:.1f} GB")
    return ", ".join(parts) if parts else "hardware details unavailable"


def format_preflight_summary(record: dict) -> str:
    """"2x Tesla T4, 4 vCPU, 31.3 GB RAM" from one PREFLIGHT record (see
    log_stream.parse_preflight) -- the real hardware this session actually
    got, seconds after the kernel started, before Blender is even
    downloaded. Repeated identical GPU names are counted ("2x Tesla T4"),
    not listed twice, and a GPU-less session reads as "CPU only" rather
    than silently dropping the GPU clause."""
    parts: list[str] = []
    names = record.get("gpu_names") or []
    if names:
        counts: dict[str, int] = {}
        for name in names:
            counts[name] = counts.get(name, 0) + 1
        parts.append(", ".join(
            f"{n}x {model}" if n > 1 else model
            for model, n in counts.items()))
    else:
        parts.append("CPU only")
    cpu_count = record.get("cpu_count")
    if cpu_count is not None:
        parts.append(f"{cpu_count} vCPU")
    ram_total = record.get("ram_total")
    if ram_total is not None:
        parts.append(f"{ram_total:.1f} GB RAM")
    return ", ".join(parts)


def status_for(worker: WorkerState | None,
               verified: bool = True) -> tuple[str, str, str]:
    """(icon name, colour, word) for the card header's status.

    Symbol AND word, always -- never colour alone (theme.WARNING's own
    docstring: ~8% of men cannot reliably tell red from green). Failure is
    amber (WARNING), a colour fixed independently of the active accent, so
    an "error" card never becomes visually confusable with a "rendering"
    card even when the user has picked the red accent for everything else.

    `verified=False` overrides everything else: an account whose token
    SetupDialog could not confirm cannot render at all (fleet.Fleet.launch
    never even gets a client for it), so its card must say so up front
    rather than showing "idle" as if it were merely waiting its turn.
    """
    if not verified:
        return "x", WARNING, "not verified"
    if worker is None:
        return "monitor", TEXT_SECONDARY, "idle"
    accent = current_accent().base
    state = worker.state
    if state == _RUNNING_STATE:
        return "activity", accent, "rendering"
    if state == _QUEUED_STATE:
        return "loader-circle", accent, "queued"
    if state == _ERROR_STATE:
        return "triangle-alert", WARNING, "error"
    if state in _CANCELLED_STATES:
        return "square", TEXT_SECONDARY, "cancelled"
    if state == _COMPLETE_STATE:
        return "circle-check", accent, "complete"
    return "circle-alert", TEXT_SECONDARY, state or "unknown"


def is_live(worker: WorkerState | None) -> bool:
    """True once Kaggle's OWN polled status says "running".

    This is necessary but not sufficient for InstanceCard to show its live
    body -- see InstanceCard._telemetry_active's docstring for the reason:
    dashboard.py polls kernel status only every 30s
    (dashboard.POLL_INTERVAL_MS), but a kernel's telemetry thread can start
    printing within ~5s of the kernel actually starting. A worker can
    therefore have TELEMETRY lines arriving for several seconds while this
    function still returns False because the last poll only ever saw
    "queued". Kept as a small, pure, independently-testable predicate in
    its own right (used for the status WORD and as one half of the
    live-body OR below), not as InstanceCard's sole answer to "is this
    card live right now".
    """
    return worker is not None and worker.state == _RUNNING_STATE


# ---------------- widgets ----------------

class GpuLiveRow(QWidget):
    """One physical GPU's live utilisation/VRAM, for exactly as long as its
    account is actually rendering. Never aggregated -- one row per GPU
    index, however many arrive."""

    def __init__(self, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)

        self.title = QLabel(f"GPU {index}")
        self.title.setFont(mono_font(9))
        h.addWidget(self.title)

        util_word = QLabel("util")
        util_word.setProperty("secondary", True)
        h.addWidget(util_word)

        self.spark = Sparkline(capacity=30, minimum=0, maximum=100,
                               color=TELEMETRY)
        h.addWidget(self.spark, 1)

        self.util_value = QLabel("—")
        self.util_value.setFont(mono_font(9))
        h.addWidget(self.util_value)

        self.mem_value = QLabel("—")
        self.mem_value.setFont(mono_font(9))
        h.addWidget(self.mem_value)

        self.live_marker = QLabel("live")
        self.live_marker.setFont(mono_font(9))
        self.live_marker.setStyleSheet(f"color: {TELEMETRY};")
        h.addWidget(self.live_marker)

    def update_sample(self, util: int, mem_used: int, mem_total: int) -> None:
        self.spark.push(util)
        self.util_value.setText(f"{util:3d}%")
        self.mem_value.setText(
            f"{format_bytes(mem_used * (1 << 20))}/"
            f"{format_bytes(mem_total * (1 << 20))}")


class InstanceCard(QWidget):
    """One account, one card: quota, then EITHER cached last-known hardware
    OR (only while actually rendering) live per-GPU gauges and frame count.

    `idle_container` and `live_container` are separate child widgets, one
    always hidden -- see the module docstring for why that split, rather
    than one body that reformats itself, is what makes "an idle card must
    never show a live gauge" hold structurally instead of by convention.

    The live/idle decision is NOT simply `is_live(worker)`
    (worker.state == "running"). dashboard.py polls Kaggle's kernel status
    only every 30s, but a kernel's telemetry can start arriving within a
    few seconds of it actually starting -- so a worker can be genuinely
    streaming TELEMETRY lines while the last poll still says "queued". A
    TELEMETRY line arriving IS proof the kernel is running, regardless of
    what the last poll happened to catch, so `_telemetry_active` -- set the
    moment ingest_telemetry receives a sample, cleared the moment
    set_worker sees a stopped/None worker -- is the authority the live body
    actually defers to. `is_live()`/status_for() are used for the status
    WORD, which is honestly whatever Kaggle's poll last reported (allowed
    to lag by design), not for what the body shows.

    Task 3's per-instance Cancel control is deliberately gated on
    `is_live(worker)` -- the exact "running" check, not the broader
    ACTIVE_STATES a queued kernel also satisfies -- because that is the
    UI-facing rule the brief states explicitly: never show it for a
    worker that is not running. Fleet.cancel_worker() itself is more
    permissive (it will also cancel a merely queued kernel); this card
    simply never offers the button for that case.
    """

    # Emitted with this card's account label when the user clicks Cancel
    # and confirms -- Dashboard owns the confirmation dialog, the
    # _CallWorker, and disabling/re-enabling this exact button (via
    # set_cancel_busy below), the same division of responsibility as
    # every other network-backed action in this app.
    cancel_requested = Signal(str)
    # Task 6: "can I just download instance 1" -- emitted with this card's
    # account label when its own Download button is clicked. Dashboard
    # owns the destination-folder dialog and the background worker exactly
    # like cancel_requested above.
    download_requested = Signal(str)

    def __init__(self, index: int, account: Account,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.index = index
        self.label = account.label
        self._verified = account.verified
        self._telemetry_active = False
        self._preflight_active = False
        self._gpu_rows: dict[int, GpuLiveRow] = {}
        self._failure_detail = ""

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        # ---- header: dot + "INSTANCE N" + username + status symbol/word ----
        header = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {account_color(index).name()}; font-size: 12pt;")
        header.addWidget(dot)
        title = QLabel(f"<b>INSTANCE {index + 1}</b>")
        header.addWidget(title)
        self.username_label = QLabel(account.username or account.label)
        header.addWidget(self.username_label, 1)
        self.status_icon = QLabel()
        header.addWidget(self.status_icon)
        self.status_word = QLabel()
        self.status_word.setFont(ui_font(9))
        header.addWidget(self.status_word)
        # Per-instance cancel -- the real equivalent of "stop just this
        # GPU": Kaggle's own unit of control is the session, not a GPU
        # within it, so this stops this account's whole session while
        # every other card's render keeps going untouched. Visibility is
        # driven entirely from set_worker (is_live() only -- see the
        # class docstring), never toggled directly by a caller.
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setFixedHeight(22)
        self.cancel_btn.hide()
        self.cancel_btn.clicked.connect(
            lambda: self.cancel_requested.emit(self.label))
        header.addWidget(self.cancel_btn)
        # Task 6: per-instance download. Unlike Cancel, this is not gated
        # on "running" -- there is something worth grabbing (partial
        # frames included) at any point once a job exists on disk, and
        # collector.collect() itself already reports cleanly ("nothing to
        # collect") when there is nothing there yet, so this button is
        # simply always available rather than trying to predict that here.
        self.download_btn = QPushButton("Download")
        self.download_btn.setFixedHeight(22)
        self.download_btn.clicked.connect(
            lambda: self.download_requested.emit(self.label))
        header.addWidget(self.download_btn)
        v.addLayout(header)

        # ---- download progress: bytes/rate/ETA, exactly like the upload
        # view's own stats line, reusing the same formatting helpers. One
        # line, hidden whenever no download is in flight for this card.
        self.download_progress_label = QLabel("")
        self.download_progress_label.setFont(mono_font(9))
        self.download_progress_label.setProperty("secondary", True)
        self.download_progress_label.hide()
        v.addWidget(self.download_progress_label)

        # ---- quota (always visible: this IS obtainable at rest) ----
        quota_row = QHBoxLayout()
        quota_label = QLabel("Quota (API)")
        quota_label.setProperty("secondary", True)
        quota_row.addWidget(quota_label)
        self.quota_value = QLabel("—")
        self.quota_value.setFont(mono_font(9))
        quota_row.addWidget(self.quota_value, 1)
        self.quota_marker = QLabel("")
        self.quota_marker.setFont(mono_font(9))
        self.quota_marker.setStyleSheet(f"color: {current_accent().base};")
        quota_row.addWidget(self.quota_marker)
        v.addLayout(quota_row)

        # ---- idle body: last-known hardware, honestly aged ----
        self.idle_container = QWidget()
        idle_v = QVBoxLayout(self.idle_container)
        idle_v.setContentsMargins(0, 0, 0, 0)
        last_run_row = QHBoxLayout()
        last_run_label = QLabel("Last run")
        last_run_label.setProperty("secondary", True)
        last_run_row.addWidget(last_run_label)
        self.last_run_value = QLabel(NEVER_RUN_TEXT)
        self.last_run_value.setFont(mono_font(9))
        self.last_run_value.setWordWrap(True)
        last_run_row.addWidget(self.last_run_value, 1)
        idle_v.addLayout(last_run_row)
        v.addWidget(self.idle_container)

        # ---- live body: per-GPU rows + frame count, "running" only ----
        self.live_container = QWidget()
        self._live_v = QVBoxLayout(self.live_container)
        self._live_v.setContentsMargins(0, 0, 0, 0)
        # PREFLIGHT arrives seconds after the kernel starts, before Blender
        # is even downloaded -- shown here so the live body reports real
        # hardware immediately instead of only "waiting for GPU
        # telemetry…" for the whole download+setup window. Hidden until
        # set_preflight() has something to show; per-GPU live gauges
        # (below) are strictly more specific once they exist, but this
        # line is not replaced by them -- it is the only place total
        # CPU/RAM ever appears while a render is actually live.
        self.preflight_label = QLabel("")
        self.preflight_label.setProperty("secondary", True)
        self.preflight_label.setWordWrap(True)
        self.preflight_label.hide()
        self._live_v.addWidget(self.preflight_label)
        self._gpu_placeholder = QLabel("waiting for GPU telemetry…")
        self._gpu_placeholder.setProperty("secondary", True)
        self._live_v.addWidget(self._gpu_placeholder)
        frames_row = QHBoxLayout()
        frames_label = QLabel("Frames")
        frames_label.setProperty("secondary", True)
        frames_row.addWidget(frames_label)
        self.frames_label = QLabel("—")
        self.frames_label.setFont(mono_font(9))
        frames_row.addWidget(self.frames_label, 1)
        self._live_v.addLayout(frames_row)
        v.addWidget(self.live_container)

        # ---- failure: one line, plain language, "error" only ----
        # Independent of the idle/live split above -- an errored worker
        # shows the CACHED (idle) body for its last-known hardware exactly
        # like any other stopped worker, plus this one extra line. Never a
        # raw traceback: set_failure always renders through
        # messages.explain_kernel_failure. "View full log" is the escape
        # hatch onto whatever untranslated detail was actually available.
        self.failure_label = QLabel("")
        self.failure_label.setWordWrap(True)
        self.failure_label.setStyleSheet(f"color: {WARNING};")
        self.failure_label.hide()
        v.addWidget(self.failure_label)
        self.view_log_btn = QPushButton("View full log")
        self.view_log_btn.setFixedHeight(22)
        self.view_log_btn.hide()
        self.view_log_btn.clicked.connect(self._show_full_failure)
        v.addWidget(self.view_log_btn)

        self._worker: WorkerState | None = None
        self.set_worker(None)
        # The status icon/word and the quota "live" marker are painted with
        # an explicit colour (current_accent().base at the moment they were
        # set), not through the QApplication stylesheet cascade -- so they
        # need telling, explicitly, when the accent changes.
        #
        # theme_signal is a process-global QObject that outlives this card,
        # so Qt's "disconnect automatically when the receiver is destroyed"
        # only fires once the underlying C++ object is actually gone --
        # not at close()/deleteLater() time, and (measured) not reliably
        # inside a single test session's lifetime either. The connection
        # handle connect() returns is what disconnect_theme_signal() below
        # uses to remove this exact connection deterministically, instead
        # of leaving it for eventual GC -- see that method's docstring.
        self._accent_connection = theme_signal.changed.connect(self.refresh_accent)

    # ---------------- live accent switch ----------------
    def refresh_accent(self) -> None:
        """Re-paint every explicitly-accent-coloured element from whatever
        current_accent() returns right now. Connected to
        theme.theme_signal.changed so a Settings accent change is reflected
        without rebuilding (or restarting) this card."""
        self.quota_marker.setStyleSheet(f"color: {current_accent().base};")
        self.set_worker(self._worker)

    def disconnect_theme_signal(self) -> None:
        """Remove this card's connection to theme.theme_signal, exactly
        once, idempotently.

        Dashboard calls this both when a card is discarded (account list
        change) and again at its own closeEvent as a backstop -- the same
        card can legitimately go through both paths, and Dashboard.close()
        itself can run more than once (the test harness's close_dashboards
        fixture closes every still-registered Dashboard even if the test
        already closed it, and QMainWindow.close() re-invokes closeEvent
        every time, not just the first).

        Disconnecting the same connection twice does not raise in PySide6
        -- it emits an unclosable "libpyside: Failed to disconnect ...
        RuntimeWarning" and returns, so a try/except around it catches
        nothing (measured: 94 such warnings per tests/test_dashboard.py
        run, each one an accumulating connection that made every later
        theme.apply() slower). Tracking the connection handle and clearing
        it to None after use makes a repeat call a no-op instead of a
        repeat disconnect, so there is nothing left for PySide6 to warn
        about.
        """
        if self._accent_connection is not None:
            theme_signal.changed.disconnect(self._accent_connection)
            self._accent_connection = None

    # ---------------- quota ----------------
    def set_quota(self, raw: str | None) -> None:
        """`raw` is exactly whatever dashboard.py's quota cache holds for
        this account: a formatted "X.X / Y.Y h" string, the literal
        "unavailable" (a failed fetch -- see Dashboard._refresh_quota_async),
        or None (nothing fetched yet). Only the first case earns a "live"
        marker: "unavailable" is not a number at all, and a fetch that
        hasn't happened yet must not imply one is coming any second."""
        self.quota_value.setStyleSheet("")
        if raw is None:
            self.quota_value.setText("—")
            self.quota_marker.setText("")
            return
        if raw == "unavailable":
            self.quota_value.setText("unavailable")
            self.quota_value.setStyleSheet(f"color: {WARNING};")
            self.quota_marker.setText("")
            return
        self.quota_value.setText(raw)
        self.quota_marker.setText("live")

    # ---------------- cached hardware (idle body) ----------------
    def set_snapshot(self, snapshot: InstanceSnapshot | None,
                     *, now: float | None = None) -> None:
        """Render the "Last run" line from the account's cached
        InstanceSnapshot. `now`, if given, makes staleness deterministic
        for tests -- see InstanceSnapshot.is_stale's own signature, which
        this mirrors exactly."""
        self.last_run_value.setStyleSheet("")
        if snapshot is None:
            self.last_run_value.setText(NEVER_RUN_TEXT)
            return
        age = format_age((now if now is not None else time.time())
                         - snapshot.observed_at)
        summary = format_hardware_summary(snapshot)
        text = f"{age} — {summary}"
        if snapshot.is_stale(now=now):
            # Worded, not just coloured -- amber alone would fail exactly
            # the red/green-blind reader theme.WARNING's docstring warns
            # about, and here there is no green counterpart to confuse it
            # with anyway, but the discipline is the same everywhere.
            text += " (stale)"
            self.last_run_value.setStyleSheet(f"color: {WARNING};")
        self.last_run_value.setText(text)

    # ---------------- verification (SetupDialog's own status, echoed here) ----------------
    def set_verified(self, verified: bool) -> None:
        """An account re-verified (or newly failing verification) after
        this card was built -- see SetupDialog. Re-renders the header from
        the CURRENT worker, since verified alone does not decide the
        idle/live body split."""
        self._verified = verified
        self.set_worker(self._worker)

    # ---------------- status + idle/live switch ----------------
    def set_worker(self, worker: WorkerState | None) -> None:
        self._worker = worker
        if worker is None or worker.state in _STOPPED_STATES:
            # A stopped (or absent) kernel cannot legitimately produce
            # another TELEMETRY line for THIS worker -- clearing the latch
            # here, not only in _sync_body below, is what stops a slow
            # straggler sample (already in flight when Kaggle reported
            # "error"/"complete") from re-opening the live body a moment
            # after this call returns "idle".
            self._telemetry_active = False
            self._preflight_active = False

        name, colour, word = status_for(worker, self._verified)
        self.status_icon.setPixmap(icon(name, colour, 14).pixmap(14, 14))
        self.status_word.setText(word)
        self.status_word.setStyleSheet(f"color: {colour};")

        # Never shown for a worker that is not running -- see the class
        # docstring. Going non-running (including simply going idle again
        # once a poll catches up) always resets the busy state: nothing
        # can legitimately still be "in flight" for a cancel request
        # against a worker that no longer qualifies for the button at all.
        running = is_live(worker)
        self.cancel_btn.setVisible(running)
        if not running:
            self.set_cancel_busy(False)

        if self._currently_live() and worker is not None:
            self.frames_label.setText(f"{worker.frames_done} / {len(worker.frames)}")
        self._sync_body()

    def set_cancel_busy(self, busy: bool) -> None:
        """Disable (and re-word) the per-card Cancel control while a
        cancel_worker() request for this account is in flight.

        Dashboard is the sole caller: it owns the _CallWorker for this
        specific action and therefore knows exactly when a request starts
        and ends, on both the success AND the failure path -- this method
        only ever reflects that, it never decides it.
        """
        self.cancel_btn.setEnabled(not busy)
        self.cancel_btn.setText("Cancelling…" if busy else "Cancel")

    # ---------------- download (Task 6) ----------------
    def set_download_busy(self, busy: bool) -> None:
        """Disable (and re-word) the per-card Download control while a
        collect() request for this account is in flight -- the download
        counterpart of set_cancel_busy above, same division of
        responsibility (Dashboard owns the worker and calls this on both
        the success and the failure path)."""
        self.download_btn.setEnabled(not busy)
        self.download_btn.setText("Downloading…" if busy else "Download")

    def set_download_progress(self, progress: DownloadProgress | None) -> None:
        """Render one DownloadProgress tick, or clear the line entirely
        when `progress` is None (no download in flight for this card --
        the initial state, and the state Dashboard restores once a
        download finishes or fails).

        Deliberately reuses blendfleet.ui.formatting's format_bytes/
        format_rate/format_eta -- the exact helpers upload_view.py already
        uses for the upload side -- rather than writing a second copy of
        "0 B/s reads as stalled, not 0.0 B/s" or any of the other
        edge-case discipline those already encode.
        """
        if progress is None:
            self.download_progress_label.setText("")
            self.download_progress_label.hide()
            return
        line = (f"{format_bytes(progress.downloaded)}/"
               f"{format_bytes(progress.total)}"
               f"  {format_rate(progress.rate_bps):>10}"
               f"  eta {format_eta(progress.downloaded, progress.total, progress.rate_bps)}")
        self.download_progress_label.setText(line)
        self.download_progress_label.show()

    def _currently_live(self) -> bool:
        """Whether the LIVE BODY should be showing right now.

        Deliberately not just `is_live(self._worker)` -- see the class
        docstring's explanation of the poll/telemetry race. Telemetry OR
        PREFLIGHT that has already arrived for the current (non-stopped)
        worker outranks what the last 30s poll happened to say -- PREFLIGHT
        in particular arrives while the poll may still say "queued" (it is
        printed before Blender is even downloaded, long before a frame's
        TELEMETRY line could exist), so without this the live body would
        stay hidden -- and PREFLIGHT invisible with it -- for the entire
        download+setup window.
        """
        w = self._worker
        if w is None or w.state in _STOPPED_STATES:
            return False
        return is_live(w) or self._telemetry_active or self._preflight_active

    def _sync_body(self) -> None:
        live = self._currently_live()
        self.live_container.setVisible(live)
        self.idle_container.setVisible(not live)
        if not live:
            # Going idle: the live body must genuinely have nothing left
            # over to show next time it is revealed -- see
            # test_ingest_telemetry_ignored_while_idle for why this alone
            # is not sufficient (ingest_telemetry also refuses while idle)
            # but it is what keeps re-showing this card on the next render
            # from flashing the PREVIOUS run's stale GPU rows for a frame.
            for row in self._gpu_rows.values():
                row.setParent(None)
                row.deleteLater()
            self._gpu_rows.clear()
            self._gpu_placeholder.show()
            self.frames_label.setText("—")
            self.preflight_label.setText("")
            self.preflight_label.hide()
            self._preflight_active = False

    # ---------------- live telemetry ----------------
    def ingest_telemetry(self, record: dict) -> None:
        """One GPU's live sample.

        Refuses to do anything once this worker has definitely stopped
        (error/complete/cancelled) or there is no worker at all -- belt and
        braces alongside _sync_body's own visibility toggle, so a caller
        wiring telemetry to the wrong card (or a sample arriving one tick
        late, after a kernel already stopped) cannot make an idle card show
        a moving gauge, which is the one outcome this whole module exists
        to prevent. Otherwise, receiving a sample AT ALL is treated as
        proof the kernel is running -- see the class docstring -- even if
        the last poll still says "queued".
        """
        w = self._worker
        if w is None or w.state in _STOPPED_STATES:
            return
        self._telemetry_active = True
        self._sync_body()

        index = record["gpu"]
        row = self._gpu_rows.get(index)
        if row is None:
            self._gpu_placeholder.hide()
            row = GpuLiveRow(index)
            self._gpu_rows[index] = row
            self._live_v.insertWidget(len(self._gpu_rows) - 1, row)
        row.update_sample(record["util"], record["mem_used"], record["mem_total"])

    # ---------------- preflight (real hardware, seconds after launch) ----
    def set_preflight(self, record: dict | None) -> None:
        """Show the notebook's PREFLIGHT record (see
        log_stream.parse_preflight) the moment it arrives -- seconds after
        the kernel starts, before Blender is even downloaded -- so the
        live body reports real hardware instead of sitting on "waiting for
        GPU telemetry…" for the whole download+setup window.

        `record=None` clears the line unconditionally (dashboard.py calls
        this at the start of every new render, before this run's own
        PREFLIGHT line can possibly have arrived, so a card must never open
        still showing the PREVIOUS run's hardware). A real record is
        subject to the same defense-in-depth as ingest_telemetry: refused
        once this worker has definitely stopped or there is none at all.
        """
        if record is None:
            self._preflight_active = False
            self.preflight_label.setText("")
            self.preflight_label.hide()
            self._sync_body()
            return
        w = self._worker
        if w is None or w.state in _STOPPED_STATES:
            return
        self._preflight_active = True
        self.preflight_label.setText(format_preflight_summary(record))
        self.preflight_label.show()
        self._sync_body()

    # ---------------- failure (Task 4: "it just says error") ------------
    def set_failure(self, raw: str | None) -> None:
        """Show why this account's render failed, in one plain-language
        line, with "View full log" as the way to see the rest.

        `raw` is whatever text Dashboard has for this worker: Kaggle's own
        failure_message (WorkerState.message) when it is non-empty, or
        the tail of the kernel log Dashboard fetched via
        Fleet.fetch_failure_log when it was not -- or None once this
        worker is not (or no longer) in "error" at all, which hides the
        line unconditionally (a card must never go on showing a PREVIOUS
        run's failure once a new render has started, exactly like
        set_preflight(None)).

        Always renders through messages.explain_kernel_failure -- never
        the raw text directly in the one-line label -- so the label is
        never a bare status word or an unread traceback; the untranslated
        detail is still reachable, deliberately, via View full log.
        """
        if raw is None:
            self.failure_label.setText("")
            self.failure_label.hide()
            self.view_log_btn.hide()
            self._failure_detail = ""
            return
        cause, explanation = explain_kernel_failure(raw)
        # Symbol AND word, never colour alone -- same discipline as
        # status_for()'s own icon+word pairing (theme.WARNING's docstring).
        self.failure_label.setText(f"⚠ {cause}")
        self.failure_label.show()
        self._failure_detail = explanation
        self.view_log_btn.show()

    def _show_full_failure(self) -> None:
        QMessageBox.information(self, "Why this instance failed",
                                self._failure_detail)
