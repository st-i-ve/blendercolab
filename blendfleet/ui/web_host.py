"""The window the web UI lives in.

Keeps everything the Qt shell already earned -- the frameless window, our
own title bar, the Windows 11 Mica backdrop -- and puts a QWebEngineView
where the widget pages used to be. The chrome stays Qt; the content
becomes the reference design's own HTML/CSS/JS, which is the whole point
of the move: transitions, keyframes, backdrop-filter and Web Audio all
work in Chromium and none of them have Qt equivalents.

Two details that are easy to get wrong:

  - qwebchannel.js is INJECTED as a QWebEngineScript at DocumentCreation
    rather than shipped as a file the page loads. It lives in Qt's own
    resource system, so injecting it means the bridge is available before
    the page's first line runs and there is nothing extra to package.
  - The page background is set transparent. Without it the web view paints
    opaque white behind everything, which hides the Mica backdrop and
    flashes white on every load, dark theme or not.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFile, QIODevice, QPointF, Qt, QUrl
from PySide6.QtGui import (QAction, QBrush, QColor, QIcon, QPainter,
                           QRadialGradient)
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEngineContextMenuRequest,
                                     QWebEnginePage, QWebEngineScript,
                                     QWebEngineSettings, QWebEngineProfile)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog,
                               QMainWindow, QMenu, QMessageBox,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from blendfleet.ui import mica
from blendfleet.ui.bridge import Backend
from blendfleet.ui.theme import (current_accent, current_theme, is_dark,
                                 theme_signal, ui_font)
from blendfleet.ui.title_bar import FramelessMixin, TitleBar

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

# What a right-click offers, by what was clicked. Chromium's own menu is
# never shown: it is drawn outside the app's stylesheet (a black slab in
# the middle of a light UI), and every item on it is either meaningless
# here or actively harmful. "Back", "Forward" and "Reload" navigate a
# SINGLE-page app -- Back leaves it blank, Reload throws away the live
# state and every loaded thumbnail. "Save page" saves the app's own
# shell. "Copy image address" copies a file:// path out of a cache
# directory nobody asked about.
#
# What IS worth offering is what a person right-clicks a rendered frame
# FOR, so an image gets exactly those two, and everything else gets no
# menu at all rather than a menu of things not to press.
CONTEXT_ACTIONS: dict = {
    QWebEngineContextMenuRequest.MediaType.MediaTypeImage: (
        ("Save image as…", QWebEnginePage.WebAction.DownloadImageToDisk),
        ("Copy image", QWebEnginePage.WebAction.CopyImageToClipboard),
    ),
}


def context_actions(media_type) -> tuple:
    """The (label, WebAction) pairs a right-click on `media_type` offers.

    Separated from the widget so the decision -- which is the whole of
    the policy above -- can be tested without a window, a page or a real
    context-menu event.
    """
    return CONTEXT_ACTIONS.get(media_type, ())


class FrameView(QWebEngineView):
    """The web view, with its own context menu.

    QWebEngineView builds Chromium's menu in contextMenuEvent; overriding
    it is what stops that menu existing at all, rather than styling
    something we do not want to show.
    """

    def contextMenuEvent(self, event) -> None:      # noqa: N802
        request = self.lastContextMenuRequest()
        actions = context_actions(
            request.mediaType() if request is not None else None)
        if not actions:
            event.accept()          # nothing to offer, so nothing appears
            return
        menu = QMenu(self)
        menu.setFont(ui_font(9))
        for label, web_action in actions:
            menu.addAction(label).triggered.connect(
                lambda _checked=False, a=web_action: self.page().triggerAction(a))
        menu.exec(event.globalPos())
        event.accept()


def save_download(download, parent=None, chooser=None) -> bool:
    """Ask where a download should go, and put it there.

    Qt hands a download nowhere by default: a QWebEngineDownloadRequest
    that is never accept()ed is silently dropped, which is what "Save
    image as..." would otherwise do -- offer, then nothing. `chooser` is
    injectable so the path decision can be tested without a dialog.
    """
    suggested = download.downloadFileName() or "frame.png"
    if chooser is None:
        def chooser(name):
            return QFileDialog.getSaveFileName(parent, "Save image", name)[0]
    target = chooser(suggested)
    if not target:
        download.cancel()
        return False
    path = Path(target)
    download.setDownloadDirectory(str(path.parent))
    download.setDownloadFileName(path.name)
    download.accept()
    return True


def _qwebchannel_source() -> str:
    """qwebchannel.js, read out of Qt's resource system.

    Importing QtWebChannel is what registers `:/qtwebchannel/`, so the
    import above is load-bearing even though nothing here names it twice.
    """
    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError(
            "qwebchannel.js is missing from Qt's resources -- the bridge "
            "cannot be established without it")
    try:
        return bytes(f.readAll()).decode("utf-8")
    finally:
        f.close()


# The ambient accent wash, as (x fraction, y fraction, x radius, y
# radius) -- the two ellipses the page used to draw in body::before,
# moved out here. They are painted by the WINDOW now, behind the title
# bar and the web view alike, because that is the only way they can be
# continuous across both: the page's own wash stopped dead at the top of
# the view, and the flat shell colour above it left a seam straight
# across the window under the title bar.
SHELL_WASHES = (
    (0.88, -0.06, 760, 360),      # bleeding in from the top right
    (-0.06, 1.08, 640, 320),      # and out at the bottom left
)
# The wash is a TINT, not a colour: the same ~14% the CSS
# --accent-soft tokens carry, so moving it did not change how strong it
# is, only where it is drawn.
WASH_ALPHA = 36     # of 255


def wash_geometry(width: int, height: int) -> list:
    """Each wash as (centre, x radius, y radius) in widget pixels.

    Separated from the painting so the arithmetic can be tested without
    a window: the failure this guards is a wash centred off-window,
    which paints nothing and looks exactly like the seam it replaced.
    """
    return [(QPointF(fx * width, fy * height), rx, ry)
            for fx, fy, rx, ry in SHELL_WASHES]


# ---------------- closing, while something is still rendering ----------
#
# The renders themselves do not care whether this window is open: they run
# on Kaggle. What closing loses is this app's VIEW of them -- the live
# stream that advances the frame counts, the chime when one finishes, and
# any chance of collecting frames until it is opened again. So the choice
# is offered, once, and only when there is something to lose.
def close_decision(preference: str, live_accounts: int,
                   tray_available: bool = True) -> str:
    """One of "quit", "hide" or "ask", from the saved preference, what is
    actually running, and whether there is anywhere to hide TO.

    Pure, and separated from the window for that reason: this is the
    whole of the policy, and it is worth being able to read it as a
    table.

    Two rules that are not preferences and cannot be overridden by one:

      - Nothing rendering ALWAYS quits. A tray icon for an idle app is
        litter, and "keep running" with nothing to keep running for is a
        promise about nothing.
      - No system tray means no hiding, ever. A window that hides itself
        into a notification area that does not exist is gone: no icon to
        click, no window to find, and a process still holding the render
        state. Quitting is the honest outcome there.
    """
    if live_accounts <= 0:
        return "quit"
    if not tray_available:
        return "quit"
    if preference == "background":
        return "hide"
    if preference == "quit":
        return "quit"
    return "ask"


def close_question(scenes: list, accounts: int) -> str:
    """What the dialog says, named rather than vague.

    It says what IS still running (by scene, because that is what the
    user launched) and what staying open actually buys -- never that
    quitting cancels anything, because it does not.
    """
    if len(scenes) == 1:
        what = f"<b>{scenes[0]}</b> is still rendering"
    elif scenes:
        what = f"<b>{len(scenes)} scenes</b> are still rendering"
    else:                                   # pragma: no cover - guarded
        what = "A render is still going"
    machines = f"{accounts} account{'' if accounts == 1 else 's'}"
    return (f"{what} on {machines}.<br><br>"
            "The render itself is on Kaggle either way — quitting does not "
            "cancel it. What quitting loses is BlendFleet following it: the "
            "frame counts stop advancing, nothing chimes when it finishes, "
            "and its frames wait on Kaggle until you open this again.")


class Shell(QWidget):
    """The window's ground: one flat fill and the accent washes over it.

    Painted rather than styled because a QSS `background-color` cannot
    hold a gradient positioned in fractions of the widget, and because
    the page above it is transparent (html,body in app.css) -- so what
    this paints IS the app's background, seen through the web view and
    beside it in the title bar. One surface, one seam-free field.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("shell")
        self.transparent = False        # set true while Mica is showing

    def paintEvent(self, event) -> None:        # noqa: N802 -- Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = current_theme()
        if not self.transparent:
            painter.fillRect(self.rect(), QColor(theme.bg))
        tint = QColor(current_accent().base)
        tint.setAlpha(WASH_ALPHA)
        clear = QColor(tint)
        clear.setAlpha(0)
        painter.setPen(Qt.PenStyle.NoPen)
        for centre, rx, ry in wash_geometry(self.width(), self.height()):
            gradient = QRadialGradient(QPointF(0, 0), rx)
            gradient.setColorAt(0.0, tint)
            gradient.setColorAt(0.65, clear)
            painter.save()
            painter.translate(centre)
            # A radial gradient in Qt is a circle; the ellipse the design
            # asks for is that circle drawn in a squashed space.
            painter.scale(1.0, ry / rx)
            painter.setBrush(QBrush(gradient))
            painter.drawEllipse(QPointF(0, 0), rx, rx)
            painter.restore()
        painter.end()


class WebHost(FramelessMixin, QMainWindow):
    """Frameless window + title bar + web view + bridge."""

    def __init__(self, store, fleet_factory, verifier, settings) -> None:
        super().__init__()
        self.settings = settings
        self.setWindowTitle("BlendFleet")
        self.resize(1400, 900)
        self._init_frameless()

        root = Shell()
        shell = QVBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.setCentralWidget(root)

        self.title_bar = TitleBar("BlendFleet")
        self.title_bar.close_requested.connect(self.close)
        shell.addWidget(self.title_bar)

        self.view = FrameView()
        shell.addWidget(self.view, 1)

        page = self.view.page()
        # Transparent, so the Mica backdrop behind the window is not hidden
        # by Chromium's default white page fill.
        page.setBackgroundColor(QColor(Qt.GlobalColor.transparent))
        web_settings = page.settings()
        for attribute in (
                QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
                QWebEngineSettings.WebAttribute.JavascriptEnabled,
                QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture):
            enabled = attribute is not (
                QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture)
            web_settings.setAttribute(attribute, enabled)

        # The bridge. Registered before the page loads so `backend` exists
        # by the time the page's own script runs.
        self.backend = Backend(store, fleet_factory, verifier, settings, self)
        self.channel = QWebChannel(self)
        self.channel.registerObject("backend", self.backend)
        page.setWebChannel(self.channel)

        script = QWebEngineScript()
        script.setName("qwebchannel")
        script.setSourceCode(_qwebchannel_source())
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        page.scripts().insert(script)

        # "Save image as..." is a download, and a download Qt is not told
        # what to do with is dropped without a word.
        page.profile().downloadRequested.connect(
            lambda download: save_download(download, self))

        # Closing state. `_quitting` is what tells closeEvent that the
        # decision has already been made and it must not ask again.
        self._tray: QSystemTrayIcon | None = None
        self._quitting = False
        self._said_where_it_went = False

        self._accent_connection = theme_signal.changed.connect(
            self._on_theme_changed)
        self.view.load(QUrl.fromLocalFile(str(WEB_DIR / "index.html")))

    # ---- window effects ----------------------------------------------
    def show_at_startup(self) -> None:
        if self.settings.fullscreen:
            self.showFullScreen()
        else:
            self.showMaximized()
        self._apply_window_effects()

    def _apply_window_effects(self) -> None:
        """Mica plus dark window chrome -- best effort, silent off Windows
        11 (see ui/mica.py)."""
        self._backdrop_active = mica.apply_backdrop(
            self, enabled=bool(getattr(self.settings, "translucent", False)),
            dark=is_dark())
        # The ground is painted, not styled (see Shell) -- a stylesheet
        # background would sit on top of the washes and flatten them.
        widget = self.findChild(QWidget, "shell")
        if isinstance(widget, Shell):
            widget.transparent = self._backdrop_active
            widget.setStyleSheet("background: transparent;")
            widget.update()

    def _on_theme_changed(self) -> None:
        self.title_bar.refresh_icons()
        # The washes are drawn in the accent and filled with the theme's
        # ground, so both have just changed under them.
        widget = self.findChild(QWidget, "shell")
        if widget is not None:
            widget.update()
        if self.isVisible():
            self._apply_window_effects()

    # ---- staying open ------------------------------------------------
    def _ensure_tray(self) -> QSystemTrayIcon:
        """The tray icon, built the first time the window hides.

        Not created at startup: an icon that appears the moment the app
        launches and does nothing is one more thing in the notification
        area to explain.
        """
        if self._tray is not None:
            return self._tray
        tray = QSystemTrayIcon(self)
        icon = self.windowIcon()
        if icon is None or icon.isNull():
            # The window's own icon is set by the entry point; this is
            # the fallback for a window that never got one.
            from blendfleet.__main__ import _icon_path
            path = _icon_path()
            icon = QIcon(str(path)) if path is not None else QIcon()
        tray.setIcon(icon)
        menu = QMenu()
        menu.setFont(ui_font(9))
        open_action = QAction("Open BlendFleet", menu)
        open_action.triggered.connect(self._restore_from_tray)
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.quit_now)
        menu.addAction(open_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        # Double-click is what people try first; the menu is for the rest.
        tray.activated.connect(
            lambda reason: self._restore_from_tray()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self._tray = tray
        return tray

    def tray_tooltip(self) -> str:
        """What the icon says it is doing, so hovering answers the
        question the tray exists for."""
        live = self.backend.live_renders()
        scenes = live.get("scenes") or []
        if not scenes:
            return "BlendFleet — nothing rendering"
        if len(scenes) == 1:
            return f"BlendFleet — {scenes[0]} rendering"
        return f"BlendFleet — {len(scenes)} scenes rendering"

    def hide_to_tray(self) -> None:
        """Out of the way, still following. The renders were never in
        this window; what stays alive here is the stream watching them."""
        tray = self._ensure_tray()
        tray.setToolTip(self.tray_tooltip())
        tray.show()
        self.hide()
        if not self._said_where_it_went:
            self._said_where_it_went = True
            # A new tray icon on Windows usually lands in the overflow
            # chevron, so an app that simply vanished would look closed.
            tray.showMessage(
                "BlendFleet is still running",
                "It is following your render from here. Open it again from "
                "this icon — or quit from its menu.",
                QSystemTrayIcon.MessageIcon.Information, 6000)

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self._tray is not None:
            self._tray.hide()

    def quit_now(self) -> None:
        """Really quit, from wherever the choice was made."""
        self._quitting = True
        self.close()

    def _ask_on_close(self, live: dict) -> str:
        """The dialog, returning "hide", "quit" or "cancel"."""
        box = QMessageBox(self)
        box.setWindowTitle("BlendFleet")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(close_question(live.get("scenes") or [],
                                   int(live.get("accounts") or 0)))
        keep = box.addButton("Keep running", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Quit", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(keep)
        remember = QCheckBox("Remember my choice", box)
        box.setCheckBox(remember)
        box.exec()
        clicked = box.clickedButton()
        if clicked is None:                 # dismissed: change nothing
            return "cancel"
        choice = "hide" if clicked is keep else "quit"
        if remember.isChecked():
            # Recorded through the same setter the Settings page uses, so
            # the choice shows up there and can be taken back.
            self.backend.setPreference(
                "closeAction", '"background"' if choice == "hide" else '"quit"')
        return choice

    def closeEvent(self, event) -> None:   # noqa: N802 -- Qt override
        # A render that is still going gets a say in this. Skipped
        # entirely once the decision is made (quit_now), so the dialog
        # cannot ask twice about the same close.
        if not self._quitting:
            live = self.backend.live_renders()
            decision = close_decision(
                getattr(self.settings, "close_action", "ask"),
                int(live.get("accounts") or 0),
                QSystemTrayIcon.isSystemTrayAvailable())
            if decision == "ask":
                decision = self._ask_on_close(live)
            if decision == "cancel":
                event.ignore()
                return
            if decision == "hide":
                event.ignore()
                self.hide_to_tray()
                return

        if self._tray is not None:
            self._tray.hide()
        if self._accent_connection is not None:
            theme_signal.changed.disconnect(self._accent_connection)
            self._accent_connection = None
        # Threads must not outlive the window -- same contract the Qt UI
        # spells out at length in Dashboard.closeEvent.
        self.backend.stop()
        super().closeEvent(event)
        # The app is told to survive its last window closing (so hiding to
        # the tray does not end the process), which means a real close has
        # to end it explicitly. Without this, quitting would leave a
        # window-less process running with no way back to it.
        app = QApplication.instance()
        if app is not None:
            app.quit()
