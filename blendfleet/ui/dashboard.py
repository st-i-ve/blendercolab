from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton,
                               QSizePolicy, QSpinBox, QStackedWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from blendfleet.accounts import AccountStore
from blendfleet.assignment import estimate
from blendfleet.fleet import Fleet, FleetState, WorkerState
from blendfleet.instance_state import GpuSnapshot, InstanceSnapshot, InstanceStore
from blendfleet.log_stream import stream_progress
from blendfleet.notebook_builder import RenderSettings
from blendfleet.settings import Settings
from blendfleet.ui.charts import Filmstrip, GpuPanel
from blendfleet.ui.components import (EventLog, HealthPanel, IconButton,
                                      NotificationPanel, OfflineBanner,
                                      StatTile, ToastStack)
from blendfleet.ui.flow_layout import FlowLayout
from blendfleet.ui.instance_card import InstanceCard
from blendfleet.ui.messages import explain
from blendfleet.ui.settings_view import SettingsPanel, SettingsView
from blendfleet.ui.setup_dialog import SetupDialog
from blendfleet.ui import mica
from blendfleet.ui.sidebar import Sidebar
from blendfleet.ui.title_bar import TitleBar, FramelessMixin
from blendfleet.ui.theme import (current_accent, current_theme, icon,
                                  is_dark, mono_font, theme_signal,
                                  tracked_font)
from blendfleet.ui.upload_view import UploadView

SETTINGS_URL = "https://www.kaggle.com/settings"
# The header title per page. Keyed by the same page keys Sidebar.PAGES
# uses -- one dict, so a page can never be navigable under one name and
# titled with another.
PAGE_TITLES = {
    "dashboard": "Dashboard",
    "files": "Files",
    "instances": "Instances",
    "logs": "Logs",
    "settings": "Settings",
}
SECONDS_PER_FRAME_DEFAULT = 57.1     # measured: 1920x1080, 128spp, Tesla P100
POLL_INTERVAL_MS = 30_000            # real network calls: kernel status, quota
LIVE_INTERVAL_MS = 2_000             # cheap: drain in-memory progress/telemetry
# Per-thread budget for closeEvent to wait on an SSE log-stream thread.
# log_stream closes the response within STOP_POLL_SECONDS of `_stop` being
# set, so this is generous; it is a bound on shutdown, not the expected wait.
STREAM_JOIN_TIMEOUT_S = 3.0
# Spacing scale for the main content column -- named rather than sprinkled
# as bare integers, so "generous breathing room at large sizes" (the Task 6
# brief) is one deliberate set of numbers, not whatever a given widget
# happened to be given when it was added.
MARGIN = 24
GAP = 14
# Prose (not data) labels are capped to this width and left where they are
# rather than stretching edge-to-edge -- at 2560px the main column is well
# over 2000px wide, and a paragraph that wide is unreadable. The filmstrip,
# GPU panel, upload view and table are deliberately NOT capped: they are
# data-dense and genuinely benefit from the extra width.
MAX_PROSE_WIDTH = 760
# The main column (rail excluded) caps out here and centres, rather than
# stretching to whatever is left of the window -- see _build_main's own
# comment for the full reasoning. Chosen so it equals the main column's
# actual width at 1920x1080 (1920 - 320 rail = 1600): nothing changes at
# that size or below, only 2560+ gains side gutters instead of stretch.
MAX_CONTENT_WIDTH = 1600
# The frame-range/resolution/samples controls and the launch/cancel/collect
# buttons are capped a second, tighter time within MAX_CONTENT_WIDTH -- a
# number entry field is not more usable at 1600px than at 300px.
MAX_CONTROLS_WIDTH = 640
CONTROL_WIDTH = 160
# The reference's instance grid is repeat(auto-fill, minmax(320px, 1fr)) --
# this is that 320. Cards below this width start wrapping their quota and
# hardware lines into unreadable ribbons.
INSTANCE_CARD_MIN_WIDTH = 320


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


class _DownloadWorker(QThread):
    """Runs `collector.collect()` off the UI thread, threading
    DownloadProgress ticks back to whichever InstanceCard(s) they belong
    to -- the download-side counterpart of _LaunchWorker's own `progress`
    Signal for uploads.

    `fn` takes ONE argument: an `on_progress(label, progress)` callable
    (exactly collect()'s own `on_progress` parameter) that this worker
    hands it, wired straight to `self.progress.emit` -- Qt marshals that
    emit onto the UI thread automatically because emitter and receiver
    live on different threads, the same mechanism _LaunchWorker.progress
    already relies on for upload progress.
    """

    progress = Signal(str, object)   # (label, blendfleet.downloader.DownloadProgress)
    succeeded = Signal(object)       # blendfleet.collector.CollectReport | None
    failed = Signal(str)

    def __init__(self, fn: Callable[[Callable], object], action: str, parent=None) -> None:
        super().__init__(parent)
        self._fn = fn
        self._action = action

    def run(self) -> None:
        try:
            result = self._fn(lambda label, p: self.progress.emit(label, p))
        except Exception as e:  # noqa: BLE001 -- turned into a friendly message
            self.failed.emit(explain(self._action, e))
        else:
            self.succeeded.emit(result)


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


class Dashboard(FramelessMixin, QMainWindow):
    def __init__(self, store: AccountStore, fleet_factory, verifier,
                 settings: Settings | None = None) -> None:
        super().__init__()
        self.store = store
        self.fleet_factory = fleet_factory
        self.verifier = verifier
        # Accepting an already-loaded Settings (see __main__.main, which
        # applies its accent to the QApplication before any window shows)
        # rather than always loading a fresh one here: the app must have
        # exactly one Settings instance in play, not two copies that could
        # drift the moment one of them is saved. Falling back to a fresh
        # load keeps every existing test (which constructs Dashboard with
        # no settings= at all) working unchanged.
        self.settings = settings if settings is not None else Settings.load()
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
        # Task 6: the fleet-wide "Collect frames..." button now runs
        # through _DownloadWorker (not _CallWorker) so it can thread live
        # DownloadProgress ticks back to each account's own InstanceCard --
        # same attribute name as before Task 6, only the worker type
        # changed, so closeEvent/tests that reference _collect_worker by
        # name need no changes beyond that.
        self._collect_worker: _DownloadWorker | None = None
        # Task 3: per-instance cancel. Keyed by account label rather than a
        # single attribute like _cancel_worker above, because -- unlike
        # "Cancel all" -- more than one account's cancel can legitimately
        # be in flight at once (the user can click Cancel on two different
        # cards before either returns).
        self._instance_cancel_workers: dict[str, _CallWorker] = {}
        # Task 6: per-instance download ("can I just download instance 1").
        # Keyed by label for the same reason as _instance_cancel_workers --
        # more than one card's download can legitimately be in flight at
        # once.
        self._instance_download_workers: dict[str, _DownloadWorker] = {}
        # Task 4: "it just says error". Keyed by account label, holding
        # whatever text (kernels_status's own failure_message, or a
        # fetched kernel-log tail) explains that account's most recent
        # failure -- fetched via _maybe_fetch_failure_logs, at most ONCE
        # per failure, never on the polling path for a healthy worker.
        # Reset only when a new render starts (_start_progress_threads),
        # exactly like the other per-run caches below.
        self._failure_logs: dict[str, str] = {}
        self._log_fetch_workers: dict[str, _CallWorker] = {}
        self._last_state: FleetState | None = None
        # label -> the worker state already written to the fleet log, so a
        # transition is logged once rather than on every refresh tick.
        self._logged_states: dict[str, str] = {}
        # Header state: unread notification count, and what the health
        # panel reports about the last poll.
        self._unread = 0
        self._last_poll_ms: int | None = None
        self._last_poll_at: str | None = None
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
        # Filled by the same SSE threads from the notebook's first-cell
        # hardware banner (log_stream.parse_hardware_banner), drained on the
        # UI thread by _live_tick alongside _telemetry_queue -- same
        # widgets-only-from-the-UI-thread reasoning as telemetry above.
        self._hardware_queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        # Filled by the same SSE threads from the notebook's single
        # PREFLIGHT line (log_stream.parse_preflight) -- printed before
        # anything else, before even Blender is downloaded. Drained
        # separately from _hardware_queue/_telemetry_queue below and
        # pushed straight to the card the moment it arrives, rather than
        # waiting for the next _refresh_instance_cards tick, so the card
        # shows real hardware within seconds of launch instead of sitting
        # on "waiting for GPU telemetry…" for the entire download+setup
        # window.
        self._preflight_queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
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
        # Per-run accumulator: label -> {"cpu_count":.., "ram_total":..},
        # from the hardware banner's CPU/RAM line. Reset alongside
        # _instance_gpus.
        self._instance_hardware: dict[str, dict] = {}
        # Per-run accumulator: label -> ordered list of GPU model names, in
        # the order the hardware banner's nvidia-smi listing printed them
        # (that listing carries no GPU index of its own -- see
        # blendfleet/instance_state.py). Matched to telemetry's indexed
        # GPUs by position when a snapshot is built.
        self._instance_gpu_models: dict[str, list[str]] = {}
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
        # Keyed by account label, one InstanceCard per account -- the rich
        # per-account "SaaS dashboard" cards (Task 4) that live in the
        # rail. Rebuilt (and this dict replaced) only by _refresh_accounts;
        # every tick just calls setters on the existing widgets, so a
        # card's live Sparkline history is never reset out from under a
        # still-rendering account.
        self._instance_cards: dict[str, InstanceCard] = {}
        self.setWindowTitle("BlendFleet")
        self.resize(1180, 760)   # only matters until show_at_startup() runs;
                                 # see its docstring for why that is not show()

        # Which page the stack is showing. Set properly by _show_page below;
        # seeded here so _refresh_* can run before the shell finishes
        # building.
        self.current_page = "dashboard"

        # The window draws its own chrome (ui/title_bar.py): the OS bar is
        # hidden, so the app is styled edge to edge instead of sitting under
        # a grey Windows strip. Called before the central widget is built so
        # the flag is set once, not toggled on a realised window.
        self._init_frameless()

        root = QWidget()
        root.setObjectName("shell")
        shell = QVBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.setCentralWidget(root)

        # Floated over the shell, bottom-right, rather than laid out in it
        # -- a toast must not reflow the page it appears over.
        self.toasts = ToastStack(root)
        self.toasts.setFixedWidth(360)

        self.title_bar = TitleBar("BlendFleet")
        self.title_bar.close_requested.connect(self.close)
        shell.addWidget(self.title_bar)

        body = QWidget()
        body.setObjectName("shellBody")
        outer = QHBoxLayout(body)
        # The sidebar floats inset from the window edge (it has its own
        # rounded corners) rather than sitting flush against it, so the
        # shell -- not the sidebar -- owns this margin.
        outer.setContentsMargins(GAP, 0, 0, GAP)
        outer.setSpacing(GAP)
        outer.addWidget(self._build_sidebar())
        outer.addWidget(self._build_main(), 1)
        shell.addWidget(body, 1)

        # F11 toggles real (borderless, chrome-free) full screen. A real
        # toggle needs a real way back out that does not depend on the user
        # remembering the same key -- self._exit_fullscreen_bar (built in
        # _build_main) is that way out, shown only while full screen.
        self._fullscreen_shortcut = QShortcut(QKeySequence("F11"), self)
        self._fullscreen_shortcut.activated.connect(self._toggle_fullscreen)

        # Chrome that is painted with an explicit accent colour rather than
        # through the QApplication stylesheet cascade (the brand mark; every
        # InstanceCard's status icon/quota marker) does not repaint itself
        # just because theme.apply() changed the stylesheet -- see
        # theme.theme_signal's own docstring. This is what makes a Settings
        # accent change visible immediately instead of after a restart.
        #
        # The handle connect() returns is kept (not discarded) so
        # closeEvent can disconnect this exact connection deterministically
        # -- see closeEvent's comment for why a bound-method disconnect
        # alone is not enough.
        self._accent_connection = theme_signal.changed.connect(self._on_accent_changed)

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
    def _build_sidebar(self) -> QWidget:
        """The sidebar is NAVIGATION now, not content.

        It used to be a 320px rail holding one InstanceCard per account --
        i.e. the app's main content parked in the place a sidebar normally
        puts its navigation, which is why the app had no navigation at all
        and everything else had to share one scrolling column. The cards
        moved to the Dashboard page (_build_dashboard_page); this holds the
        five destinations.

        Sidebar emits page_selected and nothing more -- it never touches the
        QStackedWidget itself, so the two can be tested apart.
        """
        sidebar = Sidebar()
        sidebar.page_selected.connect(self._show_page)
        self.sidebar = sidebar
        # Kept under its historical name: this is still "the control that
        # takes you to settings", and both the tests and _open_settings
        # refer to it by that name. It is now a nav item that switches to
        # the Settings PAGE rather than a gear that opens a modal.
        self.settings_btn = sidebar.buttons["settings"]
        return sidebar

    def _section(self, title: str, meta: str = "") -> QWidget:
        """A section header: tracked caps, an optional mono sub-note, and a
        hairline rule filling the rest of the row.

        The rule is a real QFrame because QSS has no ::after pseudo-element
        to generate one with -- see docs/design-gap-analysis.md.
        """
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(2, 0, 2, 0)
        h.setSpacing(10)
        label = QLabel(title)
        label.setFont(tracked_font(8, tracking=22.0))
        label.setProperty("secondary", True)
        h.addWidget(label)
        if meta:
            meta_label = QLabel(meta)
            meta_label.setFont(mono_font(8))
            meta_label.setProperty("secondary", True)
            h.addWidget(meta_label)
        rule = QFrame()
        rule.setObjectName("sectionRule")
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFixedHeight(1)
        h.addWidget(rule, 1)
        return row

    def _build_main(self) -> QWidget:
        # At 2560px the rail (fixed, 320px) leaves ~2240px for "main" --
        # letting every child simply fill that is the "stretched two-column
        # layout looks broken" failure the Task 6 brief names explicitly: a
        # QSpinBox or a "Cancel all" button two thousand pixels wide is not
        # more usable, it just looks unfinished. `content` below is the
        # deliberate cap: the whole column tops out at MAX_CONTENT_WIDTH and
        # centres, so surplus width at 2560+ becomes generous side gutters
        # (the "breathing room at large sizes" half of the same brief)
        # instead of stretch. The filmstrip/table/GPU panel/upload view
        # still grow to fill THAT column -- they are data-dense and
        # genuinely benefit from the extra width up to the cap; only the
        # frame-range/resolution controls and the launch buttons are
        # capped a second, tighter time (MAX_CONTROLS_WIDTH) because a
        # number entry field or a button does not read as more usable at
        # 1600px than at 300px, only less finished.
        main = QWidget()
        outer = QHBoxLayout(main)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        content = QWidget()
        content.setObjectName("contentColumn")
        content.setMaximumWidth(MAX_CONTENT_WIDTH)
        # Stretch factor 0 (the pre-fix value) means QHBoxLayout hands 100%
        # of surplus width to the flanking addStretch(1) spacers regardless
        # of content's maximumWidth -- the spacers "win" the space race
        # before the cap is ever consulted, and content sits at a constant
        # ~453px whether the window is 1920 or 2560 wide (measured live;
        # this was the actual, reproduced bug, not a hypothetical one).
        #
        # Qt's QBoxLayout splits available width among stretchable items in
        # proportion to their stretch factors, THEN reallocates whatever an
        # item can't use (because its maximumWidth caps it) to the other
        # stretchable items in the same proportion. A factor of 1 -- equal
        # to each spacer's -- only wins content 1/3 of the surplus, not
        # "up to its cap first": measured content stuck at ~646px at both
        # 1280 and 1920px window width, only reaching 746px at 2560, nowhere
        # near the 1600px cap. Giving content a stretch factor that swamps
        # the spacers' (10_000 vs. 1 each) makes it claim ~10000/10002 of
        # main's width -- i.e. effectively all of it -- up to its
        # maximumWidth cap; only once it is capped does the leftover
        # (~2/10002 of surplus, now the whole remainder) fall through to
        # the spacers as side gutters. Measured with this value: content
        # tracks main's width exactly (960px at 1280, 1600px at 1920 --
        # main's width there equals the cap, so no gutters, matching the
        # documented "nothing changes at 1920 or below" design) and holds
        # at the 1600px cap at 2560 with gutters absorbing the rest.
        content.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Preferred)
        outer.addWidget(content, 10_000)
        outer.addStretch(1)
        # Exposed as an attribute (it was a bare local before) so a test can
        # measure content.width() directly rather than re-deriving it from
        # main's layout -- this is exactly the "verify by measurement, not
        # description" gap that let the stretch-factor-0 bug above ship
        # while a prior report claimed the layout worked.
        self.content_column = content

        v = QVBoxLayout(content)
        v.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        v.setSpacing(GAP)

        v.addLayout(self._build_header())

        # A visible degraded-state marker for the periodic status poll --
        # see FINDING 3, task 5 fix round 1: a poll failure used to be
        # swallowed completely silently, with nothing like quota's
        # "unavailable" fallback. Hidden (empty) whenever the last poll
        # succeeded.
        #
        # Deliberately OUTSIDE the page stack, directly under the header:
        # "the app has lost contact with Kaggle" is true on every page, so
        # it must be visible from every page. This is the same role the
        # reference design gives its offline banner.
        self.offline_banner = OfflineBanner()
        self.offline_banner.retry_requested.connect(self._poll)
        v.addWidget(self.offline_banner)
        # The banner's own detail line, kept under the name the rest of the
        # app (and its tests) already use for "what the last poll failure
        # said". One string, one place: setting it and showing the banner
        # cannot drift apart because they are the same widget.
        self.poll_status_label = self.offline_banner.detail

        self.pages = QStackedWidget()
        self._pages: dict[str, QWidget] = {}
        for key, build in (("dashboard", self._build_dashboard_page),
                            ("files", self._build_files_page),
                            ("instances", self._build_instances_page),
                            ("logs", self._build_logs_page),
                            ("settings", self._build_settings_page)):
            page = build()
            page.setObjectName("page")
            self._pages[key] = page
            self.pages.addWidget(page)
        v.addWidget(self.pages, 1)
        return main

    def _build_header(self) -> QHBoxLayout:
        """Page title on the left, full-screen escape hatch on the right."""
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 6)
        self.page_title = QLabel(PAGE_TITLES["dashboard"])
        self.page_title.setObjectName("pageTitle")
        self.page_title.setFont(tracked_font(17, tracking=10.0))
        row.addWidget(self.page_title)
        row.addStretch(1)
        # The one visible way back out of real full screen (F11) -- see
        # _toggle_fullscreen. Hidden whenever the window is NOT full screen,
        # which is the common case, so it never competes with the page's own
        # controls for a sighted user who never touches F11 at all.
        self.exit_fullscreen_btn = QPushButton(" Exit full screen (F11)")
        self.exit_fullscreen_btn.setIcon(icon("x", current_theme().ink_2, 14))
        self.exit_fullscreen_btn.clicked.connect(self._toggle_fullscreen)
        self.exit_fullscreen_btn.setVisible(False)
        row.addWidget(self.exit_fullscreen_btn)

        self.health_btn = IconButton("activity", "Connection health")
        self.health_panel = HealthPanel(self)
        self.health_panel.rerun_requested.connect(self._poll)
        self.health_btn.clicked.connect(
            lambda: self.health_panel.popup_under(self.health_btn))
        row.addWidget(self.health_btn)

        self.notif_btn = IconButton("circle-alert", "Notifications")
        self.notif_panel = NotificationPanel(self)
        self.notif_btn.clicked.connect(self._open_notifications)
        row.addWidget(self.notif_btn)
        return row

    def _open_notifications(self) -> None:
        self.notif_panel.popup_under(self.notif_btn)
        # Opening the panel IS reading them -- the bubble counts unread, so
        # it clears here rather than on some separate "mark read" action
        # nobody would ever click.
        self.notif_btn.set_count(0)
        self._unread = 0

    def notify(self, message: str, tone: str = "idle") -> None:
        """One place that reports an outcome three ways: a toast now, a
        line in the fleet log, and an entry in the notification panel for
        anyone who was not looking when it happened."""
        self.toast(message, tone)
        self.event_log.append(message, tone)
        self.notif_panel.add(message, tone)
        self._unread += 1
        self.notif_btn.set_count(self._unread)

    def _refresh_health(self) -> None:
        """Fill the health panel from the polling this app already does --
        no synthetic ping. Every row is something actually measured here."""
        ok = not self.offline_banner.isVisible()
        self.health_panel.set_verdict(
            "connected" if ok else "cannot reach Kaggle",
            "active" if ok else "offline")
        self.health_panel.set_row(
            "net", "reachable" if ok else "unreachable")
        self.health_panel.set_row(
            "latency", f"{self._last_poll_ms} ms" if self._last_poll_ms
            else "—")
        self.health_panel.set_row(
            "sync", self._last_poll_at or "never")
        reachable = sum(1 for label in self._quota_cache
                        if self._quota_cache[label] != "unavailable")
        self.health_panel.set_row(
            "accounts", f"{reachable}/{len(self.store.list())}")

    # ---------------- pages ----------------
    def _build_dashboard_page(self) -> QWidget:
        """At a glance: who is in the fleet, what each machine is doing, and
        how far the current job has got."""
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(GAP)

        # The reference's four KPI tiles. Every figure here is one this app
        # can actually know: there is no "fleet disk free" tile, because
        # the notebook reports no disk telemetry -- a tile showing a number
        # we cannot source is worse than no tile.
        summary = QHBoxLayout()
        summary.setSpacing(GAP)
        self.stat_instances = StatTile("Instances online")
        self.stat_rendering = StatTile("Rendering now")
        self.stat_gpus = StatTile("GPUs active")
        self.stat_frames = StatTile("Frames done")
        for tile in (self.stat_instances, self.stat_rendering,
                     self.stat_gpus, self.stat_frames):
            summary.addWidget(tile, 1)
        v.addLayout(summary)

        v.addWidget(self._section("Running processes", "one file · divided frames"))
        # One InstanceCard per account (see blendfleet/ui/instance_card.py):
        # quota (live) plus EITHER last-known hardware OR (only while that
        # account is actually rendering) live per-GPU gauges. Rebuilt only
        # by _refresh_accounts -- i.e. when the account list itself
        # changes -- never on a poll/telemetry tick, so a card's Sparkline
        # history survives every tick in between.
        #
        # FlowLayout, not a column: cards reflow to as many per row as fit,
        # which is what makes them usable now that they are on a full-width
        # page instead of in a 320px rail.
        # The reference's .dash-grid: content on the left, the fleet log in
        # a fixed-width column on the right.
        grid = QHBoxLayout()
        grid.setSpacing(GAP + 6)
        left = QVBoxLayout()
        left.setSpacing(GAP)
        grid.addLayout(left, 1)

        self.instances_holder = QWidget()
        self.instances_layout = FlowLayout(
            self.instances_holder, spacing=GAP,
            min_item_width=INSTANCE_CARD_MIN_WIDTH)
        left.addWidget(self.instances_holder)

        # The "approximate" wording is not hedging -- it is the honest
        # description of what this widget can know. See charts.frame_done:
        # the notebook reports a COUNT of successful frames, not which ones,
        # so the strip assumes the first N of each account's stride are the
        # finished ones. That holds exactly until a frame fails, after which
        # every later cell for that account is shifted by one. Saying so
        # here is the fix the review asked for: the user must not read a
        # green cell as proof that that specific frame exists.
        left.addWidget(self._section("GPU", "one row per physical GPU, never combined"))
        self.gpu_panel = GpuPanel()
        left.addWidget(self.gpu_panel)
        left.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(GAP)
        right.addWidget(self._section("Fleet log"))
        self.event_log = EventLog()
        right.addWidget(self.event_log)
        right.addStretch(1)
        log_column = QWidget()
        log_column.setLayout(right)
        log_column.setFixedWidth(336)
        grid.addWidget(log_column, 0, Qt.AlignmentFlag.AlignTop)

        v.addLayout(grid)
        v.addStretch(1)
        return page

    def _build_files_page(self) -> QWidget:
        """The .blend being rendered, the settings it is rendered with, and
        the upload that gets it onto Kaggle."""
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(GAP)

        v.addWidget(self._section("Project", ".blend scene"))
        top = QHBoxLayout()
        self.project_label = QLabel("<b>no project selected</b>")
        browse = QPushButton("Browse for .blend…")
        browse.clicked.connect(self._pick)
        top.addWidget(self.project_label, 1)
        top.addWidget(browse)
        v.addLayout(top)

        v.addWidget(self._section("Render settings"))
        # The controls block: frame range, resolution, samples, format, and
        # the three launch/cancel/collect buttons. Fixed-width and
        # left-aligned within the page (not stretched to fill it) -- a
        # number entry field is not more usable at 1600px than at 300px.
        controls = QWidget()
        controls.setMaximumWidth(MAX_CONTROLS_WIDTH)
        controls_v = QVBoxLayout(controls)
        controls_v.setContentsMargins(0, 0, 0, 0)
        controls_v.setSpacing(GAP)
        v.addWidget(controls, 0, Qt.AlignmentFlag.AlignLeft)

        form = QFormLayout()
        self.start = QSpinBox(); self.start.setRange(1, 1000000); self.start.setValue(1)
        self.end = QSpinBox(); self.end.setRange(1, 1000000); self.end.setValue(250)
        self.rx = QSpinBox(); self.rx.setRange(64, 8192); self.rx.setValue(1920)
        self.ry = QSpinBox(); self.ry.setRange(64, 8192); self.ry.setValue(1080)
        self.spp = QSpinBox(); self.spp.setRange(1, 16384); self.spp.setValue(128)
        self.fmt = QComboBox(); self.fmt.addItems(["PNG", "JPEG"])
        for spin in (self.start, self.end, self.rx, self.ry, self.spp):
            spin.setFixedWidth(CONTROL_WIDTH)
        self.fmt.setFixedWidth(CONTROL_WIDTH)
        for lbl, wdg in (("Start frame", self.start), ("End frame", self.end),
                         ("Width", self.rx), ("Height", self.ry),
                         ("Samples", self.spp), ("Format", self.fmt)):
            form.addRow(lbl, wdg)
        controls_v.addLayout(form)

        self.eta = QLabel()
        self.eta.setWordWrap(True)
        controls_v.addWidget(self.eta)
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
        controls_v.addLayout(btns)

        v.addWidget(self._section("Filmstrip", "one cell per frame"))
        filmstrip_header = QLabel(
            "Cells are tinted by which account rendered them. Completed cells "
            "are <b>approximate</b>: the render reports how many frames "
            "succeeded, not which, so a failed frame shifts every later cell "
            "for that account. Collect frames… is the authoritative list of "
            "what actually exists.")
        filmstrip_header.setWordWrap(True)
        filmstrip_header.setMaximumWidth(MAX_PROSE_WIDTH)
        filmstrip_header.setProperty("secondary", True)
        v.addWidget(filmstrip_header)
        self.filmstrip = Filmstrip()
        v.addWidget(self.filmstrip)
        self.filmstrip_caption = QLabel("no frames yet")
        self.filmstrip_caption.setFont(mono_font(9))
        self.filmstrip_caption.setProperty("secondary", True)
        v.addWidget(self.filmstrip_caption)


        v.addWidget(self._section("Upload", "owner account"))
        self.upload_view = UploadView()
        v.addWidget(self.upload_view)
        v.addStretch(1)
        return page

    def _build_instances_page(self) -> QWidget:
        """The fleet itself: who is in it, and what each account's render is
        doing right now."""
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(GAP)

        v.addWidget(self._section("Fleet setup"))
        add_row = QHBoxLayout()
        add_btn = QPushButton("+ add account")
        add_btn.clicked.connect(self._manage)
        add_row.addWidget(add_btn)
        add_row.addStretch(1)
        v.addLayout(add_row)

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
        note.setMaximumWidth(MAX_PROSE_WIDTH)
        note.setProperty("secondary", True)
        v.addWidget(note)
        v.addStretch(1)
        return page

    def _build_logs_page(self) -> QWidget:
        """Failures, kept where they can be read after the dialog that
        announced them has been dismissed.

        Until now a failure existed in exactly two places, both transient: a
        one-line summary on the account's card, and a modal the user clicks
        away. Nothing accumulated.
        """
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(GAP)

        v.addWidget(self._section("Instance failures", "grouped per account"))
        self.logs_empty = QLabel("No failures recorded this session.")
        self.logs_empty.setProperty("secondary", True)
        v.addWidget(self.logs_empty)
        self.logs_holder = QWidget()
        self.logs_layout = QVBoxLayout(self.logs_holder)
        self.logs_layout.setContentsMargins(0, 0, 0, 0)
        self.logs_layout.setSpacing(GAP)
        v.addWidget(self.logs_holder)
        v.addStretch(1)
        return page

    def _build_settings_page(self) -> QWidget:
        """The same controls SettingsView shows in its modal -- one
        SettingsPanel, embedded here instead of in a QDialog, so the two can
        never drift apart into two different settings screens."""
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(GAP)
        self.settings_panel = SettingsPanel(self.settings)
        v.addWidget(self.settings_panel)
        v.addStretch(1)
        return page

    # ---------------- navigation ----------------
    def _show_page(self, key: str) -> None:
        page = self._pages.get(key)
        if page is None:
            return
        self.pages.setCurrentWidget(page)
        self.page_title.setText(PAGE_TITLES.get(key, key.title()))
        self.sidebar.set_active(key)
        self.current_page = key

    def _refresh_stats(self) -> None:
        """Push the four KPI tiles. Every figure is one this app can
        actually source: accounts known, workers Kaggle says are running,
        GPUs telemetry has actually reported, frames counted done."""
        state = self._last_state
        workers = state.workers if state else []
        accounts = len(self.store.list())
        rendering = sum(1 for w in workers if w.state == "running")
        gpus = self.gpu_panel.gpu_count
        frames = sum(w.frames_done for w in workers)
        self.stat_instances.set_value(accounts, str(accounts))
        self.stat_rendering.set_value(rendering, str(rendering))
        self.stat_gpus.set_value(gpus, str(gpus))
        self.stat_frames.set_value(frames, str(frames))

    def _log_state_changes(self) -> None:
        """Append a fleet-log line whenever a worker CHANGES state.

        Diffed against the last seen state rather than logged every tick:
        _refresh_views runs twice a second at times, and a log that repeats
        "acct0 running" 120 times a minute is not a log.
        """
        state = self._last_state
        for worker in (state.workers if state else []):
            previous = self._logged_states.get(worker.label)
            if previous == worker.state:
                continue
            self._logged_states[worker.label] = worker.state
            if previous is None:
                continue      # first sighting is not a change
            tone = {"error": "offline", "complete": "active",
                    "running": "idle"}.get(worker.state, "idle")
            self.event_log.append(
                f"{worker.label} -> {worker.state}", tone)

    def _refresh_nav_counts(self) -> None:
        """Nav pills carry the counts you would otherwise have to change
        page to discover. Settings gets None -- a permanent 0 there would be
        meaningless chrome."""
        state = self._last_state
        running = sum(1 for w in (state.workers if state else [])
                      if w.state in ("running", "queued"))
        self.sidebar.set_count("dashboard", running)
        self.sidebar.set_count("files", 1 if self.blend is not None else 0)
        self.sidebar.set_count("instances", len(self.store.list()))
        self.sidebar.set_count("logs", len(self._failure_logs))
        self.sidebar.set_count("settings", None)

    # ---------------- window state: maximised/full-screen ----------------
    def _apply_window_effects(self) -> None:
        """Ask Windows for the Mica backdrop and dark window chrome.

        Best-effort and silent: off Windows 11 this no-ops and the shell
        stays opaque, which is the same app minus one flourish (see
        ui/mica.py). Re-run after a theme switch, because the immersive
        dark-mode flag has to follow the theme.

        Requires a native window handle, so it can only run once the window
        has been shown -- winId() on an unrealised window forces creation
        at a point where Qt has not finished setting the window up.
        """
        translucent = getattr(self.settings, "translucent", False)
        self._backdrop_active = mica.apply_backdrop(
            self, enabled=translucent, dark=is_dark())
        # The backdrop is only visible where the app does not paint over
        # it, so the shell surfaces go transparent when it is on and
        # opaque when it is off. Cards, panels and the sidebar stay opaque
        # either way -- content has to stay readable over a wallpaper.
        transparent = self._backdrop_active
        for name in ("shell", "shellBody"):
            widget = self.findChild(QWidget, name)
            if widget is not None:
                widget.setStyleSheet(
                    "background: transparent;" if transparent else "")

    def show_at_startup(self) -> None:
        """Show the window for the first time, in whichever state
        self.settings remembers -- full screen if the user last left it
        that way, maximised otherwise.

        NOT the same as plain .show(): calling .show() on a QMainWindow
        that has never been shown displays it at whatever .resize() set
        (see __init__) in a normal, restorable window -- it does not
        maximise or full-screen it. __main__.main() calls this instead of
        .show() for exactly that reason. Tests never call this (they only
        construct/close Dashboards headlessly), so it has no bearing on
        the test suite's own window state.
        """
        if self.settings.fullscreen:
            self.showFullScreen()
        else:
            self.showMaximized()
        self._apply_window_effects()
        # Driven by self.settings.fullscreen -- the state just REQUESTED --
        # not by re-reading self.isFullScreen() immediately afterwards. See
        # _toggle_fullscreen's own comment: querying window state back
        # right after requesting a change is a genuine race, not merely a
        # test artifact, since the platform applies it asynchronously.
        self.exit_fullscreen_btn.setVisible(self.settings.fullscreen)

    def _toggle_fullscreen(self) -> None:
        """F11: real full screen (no window chrome at all) <-> maximised.

        Persisted immediately, not just held in memory, so the next launch
        opens in whichever state the user left this session in -- the
        "persisted" half of the Task 6 brief's full-screen requirement.

        Decides the TARGET state up front (`entering_fullscreen`) and drives
        both showFullScreen()/showMaximized() and exit_fullscreen_btn's
        visibility from that one boolean, rather than calling
        self.showFullScreen() and then asking self.isFullScreen() what
        happened: the platform applies a window-state change asynchronously,
        so reading it back immediately can still observe the PRE-change
        state and leave the exit button permanently stuck hidden -- a real
        race, caught by tests/test_dashboard.py exercising this after many
        other windows had already been cycled through the same QApplication.
        """
        entering_fullscreen = not self.isFullScreen()
        if entering_fullscreen:
            self.showFullScreen()
        else:
            self.showMaximized()
        self.settings.fullscreen = entering_fullscreen
        self.settings.save()
        self.exit_fullscreen_btn.setVisible(entering_fullscreen)

    def resizeEvent(self, event) -> None:      # noqa: N802 -- Qt override
        super().resizeEvent(event)
        self._place_toasts()

    def _place_toasts(self) -> None:
        """Pin the toast stack to the bottom-right of the shell."""
        margin = MARGIN
        height = max(self.toasts.sizeHint().height(), 1)
        self.toasts.setGeometry(
            self.width() - self.toasts.width() - margin,
            self.height() - height - margin,
            self.toasts.width(), height)
        self.toasts.raise_()

    def toast(self, message: str, tone: str = "idle") -> None:
        """Report an outcome without stopping the user.

        Every outcome in this app used to be a modal QMessageBox, including
        the ones nobody needs to acknowledge -- "frames collected" was a
        dialog you had to dismiss to carry on watching the render it
        finished. Decisions and failures stay modal; results come here.
        """
        self.toasts.post(message, tone)
        self._place_toasts()

    def keyPressEvent(self, event) -> None:  # noqa: N802 -- Qt override
        # Esc is the other conventional way out of full screen, alongside
        # the visible exit_fullscreen_btn and F11 itself -- three ways
        # back out, never zero. Only intercepted while actually full
        # screen, so Esc keeps its normal (no-op, here) behaviour otherwise.
        if self.isFullScreen() and event.key() == Qt.Key.Key_Escape:
            self._toggle_fullscreen()
            return
        super().keyPressEvent(event)

    # ---------------- settings (accent) ----------------
    def _open_settings(self) -> None:
        """Show the Settings page.

        This used to open SettingsView as a modal. It is a page now -- the
        controls are identical (both are a SettingsPanel), but settings that
        live behind a modal cannot be left open while you watch what they
        change, which for an accent picker is most of the point.
        """
        self._show_page("settings")

    def _on_accent_changed(self) -> None:
        """theme.theme_signal fired -- re-paint every widget that captured
        an accent colour explicitly (not through the QApplication
        stylesheet cascade, which repaints itself) at the moment it was
        built. See theme.theme_signal's docstring for the full mechanism.

        The sidebar connects to theme_signal itself and repaints its own
        brand mark and nav icons; this only has to cover the cards and the
        title bar, which are owned here -- plus the window-level effects,
        since Windows' own dark-chrome flag has to follow the theme.
        """
        for card in self._instance_cards.values():
            card.refresh_accent()
        self.title_bar.refresh_icons()
        if self.isVisible():
            self._apply_window_effects()

    # --- helpers ---
    def _refresh_accounts(self) -> None:
        """Rebuild one InstanceCard per account. Only called when the
        account list itself changes (init, and after Manage accounts…
        closes) -- never from a poll/telemetry tick, so a card's live
        Sparkline history is never wiped out from under a still-rendering
        account. Immediately re-synced from currently known state (quota
        cache, cached hardware, current worker) via _refresh_views() below,
        so a freshly (re)built card is never blank until the next tick.
        """
        # Disconnect each outgoing card from theme_signal explicitly rather
        # than trust deleteLater() + Qt's auto-disconnect-on-destroy timing
        # -- see closeEvent's comment for why that was measured unreliable.
        # This can run more than once per Dashboard lifetime (account list
        # changes), so it matters here too, not just at final close.
        # disconnect_theme_signal() is idempotent (InstanceCard tracks its
        # own connection handle), so calling it again from closeEvent later
        # for a card already disconnected here is a safe no-op.
        for card in self._instance_cards.values():
            card.disconnect_theme_signal()
        while self.instances_layout.count():
            item = self.instances_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._instance_cards = {}
        for i, a in enumerate(self.store.list()):
            card = InstanceCard(i, a)
            card.cancel_requested.connect(self._cancel_instance)
            card.download_requested.connect(self._download_instance)
            self._instance_cards[a.label] = card
            self.instances_layout.addWidget(card)
        self._refresh_views()

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
                                  self.spp.value(), self.fmt.currentText(),
                                  min_gpus=self.settings.min_gpus)
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
        self._instance_hardware.clear()
        self._instance_gpu_models.clear()
        # A previous run's failure has nothing to do with this new one --
        # cached log text and the card's failure line must not survive
        # into it. In-flight fetch workers (if a failure from the PREVIOUS
        # job is still being fetched) are left alone: they pop themselves
        # from _log_fetch_workers when done rather than being torn down
        # here, so closeEvent still has them to join.
        self._failure_logs.clear()
        for card in self._instance_cards.values():
            card.set_preflight(None)
            card.set_failure(None)
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

                def hardware(record, label=acct.label):
                    # Same reasoning as telemetry() above: enqueue only,
                    # never touch self.instance_store from this thread.
                    self._hardware_queue.put((label, record))

                def preflight(record, label=acct.label):
                    # Same reasoning as telemetry()/hardware() above.
                    self._preflight_queue.put((label, record))

                try:
                    stream_progress(acct.token, w.username,
                                    w.kernel_slug.split("/", 1)[1], bump,
                                    self._stop, on_telemetry=telemetry,
                                    on_hardware=hardware,
                                    on_preflight=preflight)
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
            self.notify(f"Cancelled {len(results)} account(s)", "idle")
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

    # ---------------- per-instance cancel (Task 3) ----------------
    def _cancel_instance(self, label: str) -> None:
        """Stop exactly one account's render, leaving every other card's
        render running untouched -- the per-instance counterpart to
        _cancel() above, wired to InstanceCard.cancel_requested.

        Kaggle's own unit of control is a whole session, not an
        individual GPU within it: there is no way to release one GPU and
        keep the other, so the confirmation below is honest about
        cancelling the ACCOUNT's session, never worded as "release this
        GPU". Same discipline as _cancel(): confirm first, disable the
        (per-card) button for the duration, re-enable it on both the
        success and the failure path.
        """
        if label in self._instance_cancel_workers:
            return   # already in flight -- that card's button is disabled too
        account = next((a for a in self.store.list() if a.label == label), None)
        who = (account.username or label) if account else label
        if QMessageBox.question(
                self, "Cancel this instance",
                f"Stop the render on {who}? Kaggle's unit of control is "
                "the session, not the GPU, so this stops that account's "
                "whole session -- every other account keeps rendering.") \
                != QMessageBox.StandardButton.Yes:
            return
        accounts = self.store.list()
        card = self._instance_cards.get(label)

        def work():
            return self.fleet_factory(accounts).cancel_worker(label)

        if card is not None:
            card.set_cancel_busy(True)

        worker = _CallWorker(work, f"Cancelling {who}'s render", self)
        self._instance_cancel_workers[label] = worker

        def done_ok(result) -> None:
            self._instance_cancel_workers.pop(label, None)
            if card is not None:
                card.set_cancel_busy(False)
            self._show_cancel_instance_result(who, result)

        def done_fail(message: str) -> None:
            self._instance_cancel_workers.pop(label, None)
            if card is not None:
                card.set_cancel_busy(False)
            QMessageBox.warning(self, "Could not cancel", message)

        worker.succeeded.connect(done_ok)
        worker.failed.connect(done_fail)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _show_cancel_instance_result(self, who: str, result) -> None:
        if result is None:
            QMessageBox.information(
                self, "Nothing to cancel",
                f"{who}'s render had already stopped -- there was nothing "
                "left to cancel.")
            return
        if result.ok:
            QMessageBox.information(
                self, "Instance cancelled",
                f"Cancel requested for {who}; Kaggle confirmed the stop. "
                "Every other account keeps rendering.")
            return
        # Exactly _show_cancel_results' own reasoning: a silently-failed
        # cancel leaves this one account's quota draining for hours.
        QMessageBox.warning(
            self, "Render did NOT stop",
            f"{who} could not be cancelled and may still be running, "
            f"spending their GPU quota: {result.error}\n\n"
            f"Stop it by hand at kaggle.com → the notebook → Stop session.")

    # ---------------- failure logs (Task 4: "it just says error") -------
    def _maybe_fetch_failure_logs(self, st: FleetState | None) -> None:
        """Kick off a background fetch of the kernel log tail for any
        worker that just turned up "error" with no failure_message of its
        own -- and ONLY those. Called once, from _poll()'s own success
        path, right after a fresh FleetState lands; never from the 30s
        polling tick's own network call, and never a second time for a
        failure already fetched (or already being fetched) this run.
        """
        if st is None:
            return
        for w in st.workers:
            if w.state != "error" or w.message:
                continue
            if w.label in self._failure_logs or w.label in self._log_fetch_workers:
                continue
            self._fetch_failure_log(w.label)

    def _fetch_failure_log(self, label: str) -> None:
        accounts = self.store.list()

        def work():
            return self.fleet_factory(accounts).fetch_failure_log(label)

        worker = _CallWorker(work, f"Fetching {label}'s failure log", self)
        self._log_fetch_workers[label] = worker

        def done_ok(text: str) -> None:
            self._log_fetch_workers.pop(label, None)
            self._failure_logs[label] = text
            self._refresh_views()

        def done_fail(message: str) -> None:
            # Best-effort: the fetch itself failing (network blip, revoked
            # token) must still surface SOMETHING rather than going back
            # to silence -- explain() has already turned it into a full
            # sentence, which reads fine as the card's one-line cause too.
            self._log_fetch_workers.pop(label, None)
            self._failure_logs[label] = message
            self._refresh_views()

        worker.succeeded.connect(done_ok)
        worker.failed.connect(done_fail)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _clear_all_download_progress(self, *, except_labels=()) -> None:
        """Reset every card's download progress line -- called once the
        FLEET-WIDE download finishes (success or failure), so a card never
        goes on showing the last tick from that worker's own download.

        `except_labels` skips whichever cards have their OWN, separate
        per-instance download still in flight (review finding: the
        fleet-wide "Collect frames..." button and a per-card "Download"
        are not mutually exclusive -- clearing every card unconditionally
        used to blank a concurrently-running per-instance download's
        progress line the instant the UNRELATED fleet-wide one finished).
        Callers pass `set(self._instance_download_workers)` -- exactly the
        labels whose own worker is still tracked as in flight; a
        per-instance download itself always clears its own card directly
        once IT finishes, never through this method.
        """
        for label, card in self._instance_cards.items():
            if label in except_labels:
                continue
            card.set_download_progress(None)

    def _route_download_progress(self, label: str, progress) -> None:
        """One DownloadProgress tick, from either the fleet-wide worker or
        a per-instance one -- routed to whichever InstanceCard shares that
        label, exactly the same "join by label, never by position" rule
        _start_progress_threads already documents for telemetry/log
        streams."""
        card = self._instance_cards.get(label)
        if card is not None:
            card.set_download_progress(progress)

    def _describe_collect_result(self, r, *, destination_phrase: str) -> str:
        """One friendly message for a CollectReport, shared by the
        fleet-wide and per-instance download paths so the two do not grow
        two different tellings of the same report.

        `destination_phrase` is the WHOLE "to <where>" clause, fully
        formed by the caller (review finding: this used to take `who` +
        `dest` separately and glue them together with its own literal
        " to " -- but both call sites' `who` already ended in "to",
        producing a doubled "... to to <path>." in the actual dialog).
        """
        msg = f"Copied {r.copied} frame(s) {destination_phrase}."
        if r.missing_frames:
            msg += (f"\n\n{len(r.missing_frames)} frame(s) are still "
                    f"missing (not rendered yet, or the render failed for "
                    f"that account): {r.missing_frames[:20]}"
                    f"{'…' if len(r.missing_frames) > 20 else ''}\n\n"
                    "Collect again once those accounts finish.")
        if r.archive_errors:
            msg += ("\n\nRecovered from this account's loose frames "
                    "instead of its archive (the archive was corrupt) "
                    f"for: {', '.join(r.archive_errors)}.")
        if r.worker_errors:
            detail = "; ".join(f"{label}: {err}"
                               for label, err in r.worker_errors.items())
            msg += f"\n\nCould not reach: {detail}"
        return msg

    def _collect(self) -> None:
        """Downloading rendered output is real, and potentially slow,
        network I/O -- moved off the UI thread for the same reason as
        _cancel above (FINDING 1, task 5 fix round 1). Task 6: runs
        through _DownloadWorker so live DownloadProgress ticks reach each
        account's own InstanceCard as they arrive, the fleet-wide
        counterpart of _download_instance below."""
        d = QFileDialog.getExistingDirectory(self, "Save frames to")
        if not d:
            return
        if self._collect_worker is not None:
            return   # a collect is already in flight -- the button is disabled too
        from blendfleet.collector import collect
        accounts = self.store.list()

        def work(on_progress):
            fleet = self.fleet_factory(accounts)
            st = fleet.load()
            if st is None:
                return None
            return collect(st, accounts, fleet.client_factory, Path(d),
                           on_progress=on_progress)

        self.collect_btn.setEnabled(False)
        self.collect_btn.setText("Collecting…")

        worker = _DownloadWorker(work, "Collecting frames", self)
        self._collect_worker = worker

        def done_ok(r) -> None:
            self._collect_worker = None
            self.collect_btn.setEnabled(True)
            self.collect_btn.setText("Collect frames…")
            self._clear_all_download_progress(
                except_labels=set(self._instance_download_workers))
            if r is None:
                QMessageBox.information(
                    self, "Nothing to collect",
                    "No render job was found -- start a render first.")
                return
            msg = self._describe_collect_result(r, destination_phrase=f"to {d}")
            if r.worker_errors:
                # Consistent with _show_download_instance_result's own
                # per-instance path below: at least one account's fetch
                # genuinely failed, so this is a warning, not the same
                # success-toned dialog a clean collect gets (review
                # finding: this used to show "Frames collected" -- an
                # information icon -- even when worker_errors was
                # non-empty, while the per-instance path correctly warned
                # for the exact same condition).
                QMessageBox.warning(self, "Frames collected", msg)
                return
            # A result, not a decision -- see Dashboard.toast. The detail
            # still goes to the fleet log so nothing is lost by not being
            # acknowledged.
            self.notify(f"Collected {r.copied} frame(s)", "active")

        def done_fail(message: str) -> None:
            self._collect_worker = None
            self.collect_btn.setEnabled(True)
            self.collect_btn.setText("Collect frames…")
            self._clear_all_download_progress(
                except_labels=set(self._instance_download_workers))
            QMessageBox.critical(self, "Could not collect frames", message)

        worker.progress.connect(self._route_download_progress)
        worker.succeeded.connect(done_ok)
        worker.failed.connect(done_fail)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    # ---------------- per-instance download (Task 6) ---------------------
    def _download_instance(self, label: str) -> None:
        """Download exactly one account's frames -- "can I just download
        instance 1" -- leaving every other card untouched. A failure here
        is collector.collect()'s own per-worker report (worker_errors),
        never a raised exception for a SINGLE dead account, but the
        network call to even reach that point can still fail outright
        (revoked token, dead client_factory) -- both paths re-enable this
        card's button exactly like _cancel_instance does for Cancel.
        """
        if label in self._instance_download_workers:
            return   # already in flight -- that card's button is disabled too
        d = QFileDialog.getExistingDirectory(self, "Save frames to")
        if not d:
            return
        from blendfleet.collector import collect
        account = next((a for a in self.store.list() if a.label == label), None)
        who = (account.username or label) if account else label
        accounts = self.store.list()
        card = self._instance_cards.get(label)

        def work(on_progress):
            fleet = self.fleet_factory(accounts)
            st = fleet.load()
            if st is None:
                return None
            return collect(st, accounts, fleet.client_factory, Path(d),
                           worker_label=label, on_progress=on_progress)

        if card is not None:
            card.set_download_busy(True)

        worker = _DownloadWorker(work, f"Downloading {who}'s frames", self)
        self._instance_download_workers[label] = worker

        def done_ok(r) -> None:
            self._instance_download_workers.pop(label, None)
            if card is not None:
                card.set_download_busy(False)
                card.set_download_progress(None)
            self._show_download_instance_result(who, d, r)

        def done_fail(message: str) -> None:
            self._instance_download_workers.pop(label, None)
            if card is not None:
                card.set_download_busy(False)
                card.set_download_progress(None)
            QMessageBox.critical(self, "Could not download frames", message)

        worker.progress.connect(self._route_download_progress)
        worker.succeeded.connect(done_ok)
        worker.failed.connect(done_fail)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _show_download_instance_result(self, who: str, dest: str, r) -> None:
        """Report one account's collect() outcome.

        Routes through _describe_collect_result for the message body in
        every case -- including worker_errors -- rather than formatting
        that branch a second time here. That branch used to be built
        inline and returned on early, which meant _describe_collect_result's
        OWN worker_errors branch could never actually fire for this
        (per-instance) path despite the method's docstring claiming it
        was shared by both download paths -- a dead, misleading branch.
        A worker_errors report still gets a warning dialog, not the
        success-toned "Frames downloaded" one, since for a single-account
        download a fetch failure IS the whole story.
        """
        if r is None:
            QMessageBox.information(
                self, "Nothing to collect",
                "No render job was found -- start a render first.")
            return
        msg = self._describe_collect_result(
            r, destination_phrase=f"from {who} to {dest}")
        if r.worker_errors:
            QMessageBox.warning(self, "Could not download frames", msg)
            return
        self.notify(f"Downloaded {r.copied} frame(s) from {who}", "active")

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

        started = time.monotonic()

        def work():
            return self.fleet_factory(accounts).poll()

        worker = _CallWorker(work, "Checking render status", self)
        self._poll_worker = worker

        def done_ok(st) -> None:
            self._poll_worker = None
            self._last_poll_ms = int((time.monotonic() - started) * 1000)
            self._last_poll_at = time.strftime("%H:%M:%S")
            if st:
                self._last_state = st
            self.poll_status_label.setText("")
            if self.offline_banner.isVisible():
                self.offline_banner.hide()
                self.event_log.append("reconnected to Kaggle", "active")
            self._refresh_views()
            # Task 4: only ever triggered from here -- this poll's own
            # network call already happened above; this only ever starts
            # a SEPARATE fetch, and only for a worker that just turned up
            # "error" with nothing to show yet. Never runs on the
            # done_fail path below: a poll that itself failed produced no
            # fresh state to look for a new failure in.
            self._maybe_fetch_failure_logs(st)

        def done_fail(message: str) -> None:
            self._poll_worker = None
            self._last_poll_ms = int((time.monotonic() - started) * 1000)
            was_visible = self.offline_banner.isVisible()
            self.offline_banner.show_reason(
                f"{message} Showing the last known render status.")
            if not was_visible:
                self.event_log.append("lost contact with Kaggle", "offline")
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
        closures that fill _telemetry_queue/_hardware_queue: those closures
        deliberately do nothing but enqueue (widgets, and now the
        instance-state write, must only ever happen off the SSE thread).
        """
        drained = 0
        newly_seen: set[str] = set()
        while drained < 200:  # bounded: never let a stuck consumer spin forever
            try:
                label, record = self._telemetry_queue.get_nowait()
            except queue.Empty:
                break
            self.gpu_panel.ingest(label, record)
            card = self._instance_cards.get(label)
            if card is not None:
                # InstanceCard.ingest_telemetry itself refuses to do
                # anything once that account's worker has definitely
                # stopped (error/complete/cancelled) or there is none at
                # all (see its own docstring) -- this call site does not
                # need to duplicate that check, only route the sample to
                # the right card.
                card.ingest_telemetry(record)
            self._instance_gpus.setdefault(label, {})[record["gpu"]] = record["mem_total"]
            if label not in self._recorded_instance_labels:
                newly_seen.add(label)
            drained += 1
        # The hardware banner (notebook's first cell) always prints before
        # the render loop's TELEMETRY lines start, so draining it here --
        # ahead of the newly_seen recording below -- means a label's very
        # first snapshot already has whatever hardware data arrived.
        hw_drained = 0
        while hw_drained < 200:  # same bound, same reason as telemetry above
            try:
                label, record = self._hardware_queue.get_nowait()
            except queue.Empty:
                break
            if record["kind"] == "cpu_ram":
                self._instance_hardware[label] = {
                    "cpu_count": record["cpu_count"],
                    "ram_total": record["ram_total"]}
            elif record["kind"] == "gpu":
                self._instance_gpu_models.setdefault(label, []).append(
                    record["model"])
            hw_drained += 1
        # PREFLIGHT arrives before even the hardware banner above (it is
        # the very first line the notebook prints, before Blender is
        # downloaded) -- pushed straight to the card here, so the live
        # body shows real hardware within seconds of launch instead of
        # "waiting for GPU telemetry…" for the whole download+setup
        # window. The card's own preflight_label is what visually retains
        # this until _start_progress_threads clears it for the next run
        # (InstanceCard.set_preflight(None)) -- nothing here needs its own
        # copy of the record.
        pf_drained = 0
        while pf_drained < 200:  # same bound, same reason as telemetry above
            try:
                label, record = self._preflight_queue.get_nowait()
            except queue.Empty:
                break
            card = self._instance_cards.get(label)
            if card is not None:
                card.set_preflight(record)
            pf_drained += 1
        for label in newly_seen:
            self._record_instance_snapshot(label)
        self._refresh_views()

    def _record_instance_snapshot(self, label: str) -> None:
        """Persist one InstanceSnapshot for `label`, once for the current
        run, from telemetry/hardware-banner data accumulated so far.

        GPU model names come from _instance_gpu_models, matched to
        telemetry's indexed GPUs by position -- the hardware banner's
        nvidia-smi listing carries no index column of its own (see
        blendfleet/instance_state.py's module docstring). cpu_count/
        ram_total come from _instance_hardware; either stays None if that
        banner never arrived for this run (stream dropped early, or a
        CPU-only session with no nvidia-smi at all) rather than being
        guessed.
        """
        self._recorded_instance_labels.add(label)
        account = next((a for a in self.store.list() if a.label == label), None)
        models = self._instance_gpu_models.get(label, [])
        gpus = [
            GpuSnapshot(index=idx, mem_total=mem_total,
                       model=models[position] if position < len(models) else None)
            for position, (idx, mem_total)
            in enumerate(sorted(self._instance_gpus.get(label, {}).items()))
        ]
        hw = self._instance_hardware.get(label, {})
        snapshot = InstanceSnapshot(
            username=account.username if account else None,
            gpus=gpus,
            cpu_count=hw.get("cpu_count"),
            ram_total=hw.get("ram_total"),
            observed_at=time.time())
        self.instance_store.record(label, snapshot)
        self.instance_store.save()

    def _refresh_views(self) -> None:
        st = self._last_state
        # The nav pills and the Logs page are driven from the same state as
        # every other view, on the same tick -- a count in the sidebar that
        # updates on a different schedule from the page it points at is
        # worse than no count.
        self._refresh_nav_counts()
        self._refresh_logs_page()
        self._refresh_stats()
        self._log_state_changes()
        self._refresh_health()
        if st is None:
            self._refresh_instance_cards(None)
            self.filmstrip.set_empty()
            self.filmstrip_caption.setText("no frames yet")
            self.table.setRowCount(0)
            self._sync_table_height()
            return
        for w in st.workers:
            live = self._live_progress.get(w.kernel_slug, 0)
            if live > w.frames_done:
                w.frames_done = live
        self._refresh_instance_cards(st)
        self._render_table(st)
        self.filmstrip.set_workers(st.start_frame, st.end_frame, st.workers)
        self.filmstrip_caption.setText(
            f"{self.filmstrip.done_count}/{self.filmstrip.total_frames} frames"
            f" · {len(st.workers)} account(s)")
        self.project_label.setText(
            f"<b>{st.blend_name}</b>  frames {st.start_frame}-{st.end_frame}")

    def _refresh_instance_cards(self, st: FleetState | None) -> None:
        """Push everything an InstanceCard needs -- quota, cached hardware,
        current worker -- into every card that already exists. Cheap and
        safe to call every tick: these three setters only ever update
        widget text/visibility, never rebuild a card, so a GPU row's
        Sparkline history survives every call in between two renders.
        """
        workers_by_label = {w.label: w for w in (st.workers if st else [])}
        for account in self.store.list():
            card = self._instance_cards.get(account.label)
            if card is None:
                continue
            worker = workers_by_label.get(account.label)
            card.set_quota(self._quota_cache.get(account.label))
            card.set_snapshot(self.instance_store.get(account.label))
            card.set_worker(worker)
            card.set_failure(self._failure_text_for(worker))

    def _refresh_logs_page(self) -> None:
        """Rebuild the Logs page from _failure_logs.

        Rebuilt wholesale rather than diffed: there is at most one entry per
        account and it only changes when a failure actually arrives, so the
        simplest correct thing is also the cheapest one. The body is
        selectable because the useful thing to do with a Kaggle traceback is
        copy it somewhere else.
        """
        while self.logs_layout.count():
            item = self.logs_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.logs_empty.setVisible(not self._failure_logs)
        for label, text in sorted(self._failure_logs.items()):
            entry = QWidget()
            entry.setObjectName("card")
            ev = QVBoxLayout(entry)
            ev.setContentsMargins(14, 12, 14, 12)
            ev.setSpacing(6)
            heading = QLabel(label)
            heading.setFont(tracked_font(8, tracking=14.0))
            heading.setStyleSheet(f"color: {current_theme().warn_ink};")
            ev.addWidget(heading)
            body = QLabel(text)
            body.setFont(mono_font(8))
            body.setWordWrap(True)
            body.setProperty("secondary", True)
            body.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            ev.addWidget(body)
            self.logs_layout.addWidget(entry)

    def _failure_text_for(self, worker: WorkerState | None) -> str | None:
        """Whatever text explains `worker`'s failure right now, or None
        when there is nothing to show -- not in "error" at all, OR in
        "error" with kernels_status's own failure_message empty and the
        log tail not fetched (yet, or ever -- see _maybe_fetch_failure_logs)
        for it. Kaggle's own failure_message always wins when present:
        it is already the real cause and needs no network round trip.
        """
        if worker is None or worker.state != "error":
            return None
        return worker.message or self._failure_logs.get(worker.label)

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
        self._sync_table_height()

    def _sync_table_height(self) -> None:
        """Cap the table to roughly its own content height instead of the
        QAbstractItemView default (Expanding vertically, filling whatever
        column space is left over) -- with 3-4 accounts that used to leave
        several hundred pixels of empty striped background below the last
        real row, which reads as broken rather than spacious. Capped, not
        fixed, and never below a handful of rows' worth: a fleet with many
        more accounts than fit still scrolls inside the table rather than
        pushing the note/poll-status text below it off screen.
        """
        row_h = self.table.verticalHeader().defaultSectionSize()
        rows = max(self.table.rowCount(), 3)
        header_h = self.table.horizontalHeader().height()
        self.table.setMaximumHeight(header_h + rows * row_h + 6)

    def closeEvent(self, event) -> None:
        self._stop.set()          # tells the daemon SSE threads to unwind
        self.timer.stop()
        self.live_timer.stop()
        self.event_log.stop()
        # theme_signal is a process-global QObject that outlives any one
        # Dashboard -- deleteLater() + processEvents() (close_dashboards, in
        # the test harness) schedules this window's own destruction, which
        # normally auto-disconnects its signal connections too, but that is
        # a matter of WHEN the C++ side actually goes, not immediate. Measured
        # (Task 5 fix round 1) alongside a much bigger factor -- see
        # tests/test_dashboard.py's _restore_active_accent fixture -- that a
        # test session accumulating enough not-yet-fully-deleted Dashboards/
        # InstanceCards still connected here made every later
        # theme.apply()/theme_signal.changed.emit() progressively slower,
        # which is what looked like a hang. Disconnecting explicitly here
        # removes this dashboard's own connections the moment it closes
        # instead of waiting on deletion timing.
        #
        # This used to be `try: disconnect(bound_method) except (RuntimeError,
        # TypeError): pass`, on the theory that a repeat disconnect "just
        # emits a RuntimeWarning ... not an error worth stopping for". That
        # theory is what hid the actual bug: PySide6's disconnect() does not
        # raise on a redundant disconnect, it warns and returns, so the
        # except clause never ran and never could -- every one of those
        # disconnects was silently failing. And closeEvent DOES run more
        # than once per Dashboard in practice: QMainWindow.close()
        # re-invokes closeEvent every time it is called, including
        # close_dashboards' teardown call after a test already closed the
        # same dashboard itself (measured: 94 "Failed to disconnect"
        # warnings across tests/test_dashboard.py, one per redundant
        # disconnect, each leaving theme_signal connected to a widget this
        # window no longer owns).
        #
        # The fix is to make a repeat call a no-op instead of a repeat
        # disconnect, by tracking whether we are still connected -- the
        # `_accent_connection` handle __init__ stored, cleared to None the
        # first time it is actually used. No warning is possible because
        # disconnect() is never asked to remove the same connection twice.
        if self._accent_connection is not None:
            theme_signal.changed.disconnect(self._accent_connection)
            self._accent_connection = None
        # The sidebar holds its own theme_signal connection (it repaints its
        # brand mark and nav icons itself), so it has to be released here
        # too -- same reasoning, same idempotence contract as the cards'.
        self.sidebar.disconnect_theme_signal()
        for card in self._instance_cards.values():
            card.disconnect_theme_signal()
        # Threads must not outlive the window (FINDING 1, task 5 fix
        # round 1): wait for whichever _CallWorker/_LaunchWorker happens
        # to be in flight rather than letting Qt destroy a QObject whose
        # thread is still running underneath it. Bounded so a genuinely
        # stuck network call cannot hang application shutdown forever.
        for worker in (self._launch_worker, self._poll_worker,
                       self._quota_worker, self._cancel_worker,
                       self._collect_worker,
                       *self._instance_cancel_workers.values(),
                       *self._log_fetch_workers.values(),
                       *self._instance_download_workers.values()):
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
