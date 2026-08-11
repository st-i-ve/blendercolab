"""Our own window chrome, so the app is styled edge to edge.

Chrome, Discord and VS Code all draw their own title bar; the OS one is
simply hidden. Until now it was the only part of BlendFleet that was not
ours -- a grey Windows strip sitting above a shell whose sidebar is
deliberately inset from the window edge, which reads as a page with a
margin rather than as a designed window.

TWO Qt calls do the work that makes this feel native rather than
approximated:

  - QWindow.startSystemMove() hands the drag back to the OS, so Aero Snap,
    drag-to-edge, shake-to-minimise and multi-monitor all keep working.
  - QWindow.startSystemResize(edge) does the same for the borders.

Hand-computing mouse deltas instead -- the usual approach -- is what makes
most frameless apps feel subtly wrong, and loses snapping entirely.

NOT implemented: Windows 11's Snap Layouts (the fly-out when you hover the
maximise button). That needs native WM_NCHITTEST handling, which is a
separate piece of work; everything else about the window behaves normally
without it.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from blendfleet.ui.theme import current_theme, icon, tracked_font

BAR_HEIGHT = 38
BUTTON_SIZE = 30
ICON_SIZE = 13
# How close to an edge counts as "grab to resize". 6px is the Windows
# default-ish feel; much less and the window becomes fiddly to grab, much
# more and clicks near a panel edge start resizing instead.
RESIZE_MARGIN = 6


class TitleBar(QWidget):
    """The draggable strip: title on the left, window buttons on the right.

    Owns no window state of its own -- it asks its window() for everything
    and emits nothing but `close_requested`, so a test can drive the window
    directly without going through the bar and vice versa.
    """

    close_requested = Signal()

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(BAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 6, 0)
        row.setSpacing(6)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("titleBarTitle")
        self.title_label.setFont(tracked_font(7, tracking=18.0))
        row.addWidget(self.title_label)
        row.addStretch(1)

        self.minimise_btn = self._window_button("minimise", "Minimise")
        self.minimise_btn.clicked.connect(self._on_minimise)
        row.addWidget(self.minimise_btn)

        self.maximise_btn = self._window_button("maximise", "Maximise")
        self.maximise_btn.clicked.connect(self.toggle_maximised)
        row.addWidget(self.maximise_btn)

        self.close_btn = self._window_button("x", "Close")
        self.close_btn.setObjectName("closeButton")
        self.close_btn.clicked.connect(self.close_requested.emit)
        row.addWidget(self.close_btn)

        self.refresh_icons()

    def _window_button(self, icon_name: str, tooltip: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName("windowButton")
        button.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.ArrowCursor)
        button.setProperty("iconName", icon_name)
        return button

    def refresh_icons(self) -> None:
        """Re-tint every glyph for the active theme.

        Icon pixmaps are painted once from whatever colour was current at
        the time, so they do not follow a theme switch on their own -- the
        same rule as everywhere else in this app.
        """
        colour = current_theme().ink_2
        for button in (self.minimise_btn, self.maximise_btn, self.close_btn):
            name = button.property("iconName")
            if name == "maximise":
                name = "restore" if self._is_maximised() else "maximise"
            button.setIcon(icon(name, colour, ICON_SIZE))

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)

    # ---- window actions -------------------------------------------------
    def _is_maximised(self) -> bool:
        window = self.window()
        return bool(window) and window.isMaximized()

    def _on_minimise(self) -> None:
        self.window().showMinimized()

    def toggle_maximised(self) -> None:
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()
        self.refresh_icons()

    # ---- dragging -------------------------------------------------------
    def mousePressEvent(self, event) -> None:      # noqa: N802 -- Qt override
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        handle = self.window().windowHandle()
        if handle is not None:
            # The OS takes over from here: snapping, edge-drag and
            # multi-monitor behaviour all come free, and the drag ends
            # without us tracking a release at all.
            handle.startSystemMove()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 -- Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximised()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class FramelessMixin:
    """Edge resizing for a frameless top-level window.

    Mixed into the QMainWindow rather than living in TitleBar, because the
    resizable edges are the WINDOW's, not the bar's -- a bar-owned
    implementation cannot see a drag that starts at the bottom-left corner.

    Expects the host to call _init_frameless() after its own __init__ has
    set up the window.
    """

    def _init_frameless(self) -> None:
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        # Needed for the Mica backdrop to show through at all, and for the
        # rounded corners DWM draws not to be filled in by Qt.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setMouseTracking(True)

    def _edge_at(self, pos: QPoint):
        """Which window edge (if any) `pos` is close enough to grab."""
        if self.isMaximized() or self.isFullScreen():
            return None            # a maximised window has no edges to drag
        rect = self.rect()
        left = pos.x() <= RESIZE_MARGIN
        right = pos.x() >= rect.width() - RESIZE_MARGIN
        top = pos.y() <= RESIZE_MARGIN
        bottom = pos.y() >= rect.height() - RESIZE_MARGIN
        edges = Qt.Edge(0)
        if left:
            edges |= Qt.Edge.LeftEdge
        if right:
            edges |= Qt.Edge.RightEdge
        if top:
            edges |= Qt.Edge.TopEdge
        if bottom:
            edges |= Qt.Edge.BottomEdge
        return edges or None

    @staticmethod
    def _cursor_for(edges) -> Qt.CursorShape:
        left = bool(edges & Qt.Edge.LeftEdge)
        right = bool(edges & Qt.Edge.RightEdge)
        top = bool(edges & Qt.Edge.TopEdge)
        bottom = bool(edges & Qt.Edge.BottomEdge)
        if (left and top) or (right and bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if (right and top) or (left and bottom):
            return Qt.CursorShape.SizeBDiagCursor
        if left or right:
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.SizeVerCursor

    def mouseMoveEvent(self, event) -> None:       # noqa: N802 -- Qt override
        edges = self._edge_at(event.position().toPoint())
        if edges is None:
            self.unsetCursor()
        else:
            self.setCursor(self._cursor_for(edges))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:      # noqa: N802 -- Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            edges = self._edge_at(event.position().toPoint())
            handle = self.windowHandle()
            if edges is not None and handle is not None:
                handle.startSystemResize(edges)
                event.accept()
                return
        super().mousePressEvent(event)
