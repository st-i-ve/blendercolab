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

from PySide6.QtCore import QFile, QIODevice, Qt, QUrl
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEngineScript, QWebEngineSettings,
                                     QWebEngineProfile)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget

from blendfleet.ui import mica
from blendfleet.ui.bridge import Backend
from blendfleet.ui.theme import current_theme, is_dark, theme_signal
from blendfleet.ui.title_bar import FramelessMixin, TitleBar

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


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


class WebHost(FramelessMixin, QMainWindow):
    """Frameless window + title bar + web view + bridge."""

    def __init__(self, store, fleet_factory, verifier, settings) -> None:
        super().__init__()
        self.settings = settings
        self.setWindowTitle("BlendFleet")
        self.resize(1400, 900)
        self._init_frameless()

        root = QWidget()
        root.setObjectName("shell")
        shell = QVBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.setCentralWidget(root)

        self.title_bar = TitleBar("BlendFleet")
        self.title_bar.close_requested.connect(self.close)
        shell.addWidget(self.title_bar)

        self.view = QWebEngineView()
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
        transparent = self._backdrop_active
        widget = self.findChild(QWidget, "shell")
        if widget is not None:
            widget.setStyleSheet(
                "background: transparent;" if transparent
                else f"background-color: {current_theme().bg};")

    def _on_theme_changed(self) -> None:
        self.title_bar.refresh_icons()
        if self.isVisible():
            self._apply_window_effects()

    def closeEvent(self, event) -> None:   # noqa: N802 -- Qt override
        if self._accent_connection is not None:
            theme_signal.changed.disconnect(self._accent_connection)
            self._accent_connection = None
        # Threads must not outlive the window -- same contract the Qt UI
        # spells out at length in Dashboard.closeEvent.
        self.backend.stop()
        super().closeEvent(event)
