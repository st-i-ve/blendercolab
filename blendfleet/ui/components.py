"""The reference design's shared components, as Qt widgets.

Everything here is a piece the reference has and BlendFleet did not: the
KPI tiles across the top of its dashboard, its status badges, its fleet
event log, its toast stack and its offline banner. They are collected in
one module because they are all *chrome* -- none of them knows anything
about Kaggle, fleets or renders. Each takes plain values and displays them;
dashboard.py decides what those values are.

Colours are never captured here at construction. Anything painted with an
explicit colour re-reads current_theme()/current_accent() in a refresh_*
method, which Dashboard calls on theme_signal -- the rule the whole app
follows (see theme.theme_signal).
"""
from __future__ import annotations

from collections import deque

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from blendfleet.ui.theme import (RADIUS_MD, current_accent, current_theme,
                                 icon, mono_font, tracked_font)

# How many samples a stat tile's sparkline keeps. The reference's HISTORY.
SPARK_HISTORY = 48
TOAST_MS = 4200
TOAST_FADE_MS = 220


class Badge(QWidget):
    """A soft-tinted pill: coloured dot, then a WORD.

    The word is not decoration. Status in this app is never carried by
    colour alone -- see instance_card.status_for and the note on
    ThemePalette.warn -- and a dot without a word is exactly that failure.
    `tone` selects the tint via a QSS property, so the palette lives in the
    stylesheet with every other colour rather than here.
    """

    TONES = ("active", "idle", "offline", "paused", "warn", "accent")

    def __init__(self, text: str = "", tone: str = "idle",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("badge")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Maximum,
                           QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(9, 3, 9, 3)
        row.setSpacing(6)
        self.dot = QLabel()
        self.dot.setFixedSize(6, 6)
        row.addWidget(self.dot)
        self.label = QLabel(text)
        self.label.setFont(tracked_font(7, tracking=14.0))
        row.addWidget(self.label)
        self._tone = tone
        self.set_tone(tone)

    def set_text(self, text: str) -> None:
        self.label.setText(text)

    def set_tone(self, tone: str) -> None:
        self._tone = tone if tone in self.TONES else "idle"
        self.setProperty("tone", self._tone)
        self.style().unpolish(self)
        self.style().polish(self)
        self.refresh_theme()

    def refresh_theme(self) -> None:
        t = current_theme()
        colour = {
            "active": t.active, "idle": t.idle, "offline": t.offline,
            "paused": t.paused_ink, "warn": t.warn,
            "accent": current_accent().base,
        }[self._tone]
        self.dot.setStyleSheet(
            f"background-color: {colour}; border-radius: 3px;")


class Sparkbar(QWidget):
    """The small line chart inside a stat tile.

    Deliberately not charts.Sparkline: that one is a live telemetry gauge
    with a fixed range and a ring buffer sized for it. This is a shape --
    it autoscales to whatever it has been given, because "instances online"
    and "GB free" do not share an axis.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(110, 30)
        self._values: deque[float] = deque(maxlen=SPARK_HISTORY)

    def push(self, value: float) -> None:
        self._values.append(float(value))
        self.update()

    def paintEvent(self, event) -> None:   # noqa: N802 -- Qt override
        if len(self._values) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        lo, hi = min(self._values), max(self._values)
        span = (hi - lo) or 1.0
        w, h = self.width(), self.height() - 2
        n = len(self._values)
        pen = QPen(QColor(current_accent().base))
        pen.setWidthF(1.5)
        painter.setPen(pen)
        prev = None
        for i, value in enumerate(self._values):
            x = w * i / (n - 1)
            y = 1 + h - ((value - lo) / span) * h
            if prev is not None:
                painter.drawLine(prev[0], prev[1], x, y)
            prev = (x, y)
        painter.end()


class StatTile(QWidget):
    """One KPI: a label, a big mono value, a trend chip and a sparkline.

    The reference's .sum. `set_value` takes the number AND the string to
    show, because the two are not the same thing -- "31.3" displays with a
    unit and a decimal place, but the sparkline needs the raw figure.
    """

    def __init__(self, label: str, unit: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        v = QVBoxLayout(self)
        v.setContentsMargins(19, 15, 19, 15)
        v.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.label = QLabel(label)
        self.label.setFont(tracked_font(7, tracking=20.0))
        self.label.setProperty("tertiary", True)
        top.addWidget(self.label)
        top.addStretch(1)
        self.trend = Badge("—", "idle")
        top.addWidget(self.trend)
        v.addLayout(top)

        mid = QHBoxLayout()
        mid.setSpacing(10)
        self.value = QLabel("—")
        self.value.setFont(mono_font(17))
        mid.addWidget(self.value)
        if unit:
            unit_label = QLabel(unit)
            unit_label.setFont(mono_font(8))
            unit_label.setProperty("tertiary", True)
            mid.addWidget(unit_label, 0, Qt.AlignmentFlag.AlignBottom)
        mid.addStretch(1)
        self.spark = Sparkbar()
        mid.addWidget(self.spark, 0, Qt.AlignmentFlag.AlignBottom)
        v.addLayout(mid)

        self._previous: float | None = None

    def set_value(self, number: float | None, text: str) -> None:
        self.value.setText(text)
        if number is None:
            return
        self.spark.push(number)
        if self._previous is not None:
            delta = number - self._previous
            if abs(delta) < 1e-9:
                self.trend.set_text("—")
                self.trend.set_tone("idle")
            else:
                self.trend.set_text(f"{'+' if delta > 0 else ''}{delta:g}")
                self.trend.set_tone("active" if delta > 0 else "offline")
        self._previous = number

    def refresh_theme(self) -> None:
        self.trend.refresh_theme()
        self.spark.update()


class EventLog(QWidget):
    """The fleet log: timestamped lines, newest first, severity-coloured.

    Bounded rather than unbounded -- an app left open for a week must not
    accumulate an unread million-line list nobody will scroll. Severity is
    a tone name, so the colours come from the theme like everything else.
    """

    MAX_LINES = 200

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        head = QWidget()
        head_row = QHBoxLayout(head)
        head_row.setContentsMargins(17, 12, 17, 12)
        title = QLabel("Events")
        title.setFont(tracked_font(7, tracking=20.0))
        title.setProperty("secondary", True)
        head_row.addWidget(title)
        head_row.addStretch(1)
        self.clock = QLabel("--:--:--")
        self.clock.setFont(mono_font(8))
        self.clock.setProperty("tertiary", True)
        head_row.addWidget(self.clock)
        v.addWidget(head)

        self.body = QWidget()
        self._lines = QVBoxLayout(self.body)
        self._lines.setContentsMargins(17, 4, 17, 14)
        self._lines.setSpacing(4)
        self._lines.addStretch(1)
        v.addWidget(self.body)

        self._entries: list[QWidget] = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_clock)
        self._timer.start(1000)
        self._tick_clock()

    def _tick_clock(self) -> None:
        from datetime import datetime
        self.clock.setText(datetime.now().strftime("%H:%M:%S"))

    def append(self, message: str, tone: str = "idle") -> None:
        from datetime import datetime
        t = current_theme()
        colour = {"active": t.active_ink, "warn": t.warn_ink,
                  "offline": t.offline_ink}.get(tone, t.ink_2)
        row = QLabel(f"{datetime.now().strftime('%H:%M:%S')}  {message}")
        row.setFont(mono_font(8))
        row.setWordWrap(True)
        row.setStyleSheet(f"color: {colour};")
        self._lines.insertWidget(0, row)
        self._entries.insert(0, row)
        while len(self._entries) > self.MAX_LINES:
            old = self._entries.pop()
            old.setParent(None)
            old.deleteLater()

    def stop(self) -> None:
        """Stop the clock. Called from Dashboard.closeEvent -- a QTimer on
        a widget Qt is about to destroy is the same class of leak as a
        thread outliving its window."""
        self._timer.stop()


class Toast(QWidget):
    """One transient message. Fades in, waits, fades out, deletes itself."""

    def __init__(self, message: str, tone: str = "idle",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 11, 14, 11)
        row.setSpacing(10)
        self.badge = Badge("", tone)
        self.badge.label.hide()          # dot only: the message is the text
        row.addWidget(self.badge)
        label = QLabel(message)
        label.setWordWrap(True)
        label.setMaximumWidth(320)
        row.addWidget(label, 1)

        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(TOAST_FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.InOutQuad)

    def play(self) -> None:
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        QTimer.singleShot(TOAST_MS, self._dismiss)

    def _dismiss(self) -> None:
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._finish)
        self._fade.start()

    def _finish(self) -> None:
        self.setParent(None)
        self.deleteLater()


class ToastStack(QWidget):
    """Bottom-right stack of transient messages.

    The point of it: BlendFleet reports every outcome through a blocking
    QMessageBox today, including outcomes nobody needs to acknowledge
    ("frames collected"). A modal for those interrupts the thing the user
    is watching. Genuine decisions and genuine failures stay modal.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          True)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addStretch(1)
        self._v = v

    def post(self, message: str, tone: str = "idle") -> Toast:
        toast = Toast(message, tone, self)
        self._v.addWidget(toast, 0, Qt.AlignmentFlag.AlignRight)
        toast.play()
        return toast


class OfflineBanner(QWidget):
    """Shown when the app cannot reach Kaggle.

    Says the one thing that is actually reassuring and is easy to get
    wrong: the renders themselves are unaffected. They run on Kaggle, not
    here, so losing the app's connection stops the app watching -- it does
    not stop anybody's frames.
    """

    retry_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("offlineBanner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 14, 18, 14)
        row.setSpacing(14)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(20, 20)
        row.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(2)
        headline = QLabel(
            "<b>Can't reach Kaggle.</b> Renders already running are "
            "unaffected — they run on Kaggle, not here. Frames stay on each "
            "session until the app can collect them again.")
        headline.setWordWrap(True)
        text.addWidget(headline)
        self.detail = QLabel("")
        self.detail.setFont(mono_font(8))
        self.detail.setWordWrap(True)
        text.addWidget(self.detail)
        row.addLayout(text, 1)

        self.retry_btn = QPushButton("Retry now")
        self.retry_btn.setObjectName("cardButton")
        self.retry_btn.setFixedHeight(30)
        self.retry_btn.clicked.connect(self.retry_requested.emit)
        row.addWidget(self.retry_btn, 0, Qt.AlignmentFlag.AlignTop)

        self.hide()
        self.refresh_theme()

    def show_reason(self, detail: str) -> None:
        self.detail.setText(detail)
        self.show()

    def refresh_theme(self) -> None:
        self.icon_label.setPixmap(
            icon("triangle-alert", current_theme().offline_ink, 20).pixmap(20, 20))


class IconButton(QPushButton):
    """A square header button: an icon, an optional count bubble.

    The bubble is a child rather than part of the icon so it can sit
    half-outside the button's corner, which is where the reference puts it.
    """

    def __init__(self, icon_name: str, tooltip: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("iconButton")
        self._icon_name = icon_name
        self.setToolTip(tooltip)
        self.setFixedSize(40, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.count = QLabel("0", self)
        self.count.setObjectName("notifCount")
        self.count.setFont(mono_font(7))
        self.count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count.setFixedSize(18, 18)
        self.count.move(self.width() - 14, -4)
        self.count.hide()
        self.refresh_theme()

    def set_count(self, value: int) -> None:
        self.count.setText(str(value))
        self.count.setVisible(value > 0)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.refresh_theme()

    def refresh_theme(self) -> None:
        colour = (current_accent().ink() if self.property("active") == "true"
                  else current_theme().ink_2)
        self.setIcon(icon(self._icon_name, colour, 16))


class FloatingPanel(QWidget):
    """A popup anchored under a header button.

    Qt.Popup, not a frameless window: a popup closes itself when the user
    clicks anywhere else, which is the behaviour every one of these panels
    wants and none of them should have to implement.
    """

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("floatPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(320)
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(16, 14, 16, 14)
        self._v.setSpacing(10)
        heading = QLabel(title)
        heading.setFont(tracked_font(7, tracking=18.0))
        heading.setProperty("secondary", True)
        self._v.addWidget(heading)

    def popup_under(self, anchor: QWidget) -> None:
        """Show below `anchor`, right edges aligned, kept on screen."""
        corner = anchor.mapToGlobal(anchor.rect().bottomRight())
        x = corner.x() - self.width()
        self.adjustSize()
        self.move(max(x, 8), corner.y() + 8)
        self.show()


class NotificationPanel(FloatingPanel):
    """Everything the app has said recently, kept where it can be re-read.

    A toast is gone in four seconds; this is where it went. Without it,
    moving results off modals would mean genuinely losing them.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Notifications", parent)
        self.empty = QLabel("Nothing yet.")
        self.empty.setProperty("tertiary", True)
        self._v.addWidget(self.empty)
        self._list = QVBoxLayout()
        self._list.setSpacing(8)
        self._v.addLayout(self._list)
        clear = QPushButton("Clear all")
        clear.setObjectName("cardButton")
        clear.setFixedHeight(26)
        clear.clicked.connect(self.clear)
        self._v.addWidget(clear, 0, Qt.AlignmentFlag.AlignRight)
        self._entries: list[QWidget] = []

    def add(self, message: str, tone: str = "idle") -> None:
        from datetime import datetime
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(9)
        badge = Badge("", tone)
        badge.label.hide()
        h.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(1)
        body = QLabel(message)
        body.setWordWrap(True)
        text.addWidget(body)
        stamp = QLabel(datetime.now().strftime("%H:%M:%S"))
        stamp.setFont(mono_font(7))
        stamp.setProperty("tertiary", True)
        text.addWidget(stamp)
        h.addLayout(text, 1)
        self._list.insertWidget(0, row)
        self._entries.insert(0, row)
        self.empty.hide()

    def clear(self) -> None:
        while self._entries:
            row = self._entries.pop()
            row.setParent(None)
            row.deleteLater()
        self.empty.show()

    @property
    def unread(self) -> int:
        return len(self._entries)


class HealthPanel(FloatingPanel):
    """Whether the app can talk to Kaggle, and how well.

    Every row is sourced from something this app actually does -- the poll
    that already runs every 30 seconds -- rather than from a synthetic
    ping. There is no packet-loss row for that reason: nothing here
    measures packet loss, and a row that always reads 0% would be a
    decoration pretending to be an instrument.
    """

    rerun_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Connection health", parent)
        self.verdict = Badge("checking…", "idle")
        self._v.addWidget(self.verdict)
        self._rows: dict[str, QLabel] = {}
        for key, label in (("net", "Kaggle API"), ("latency", "Last poll took"),
                           ("sync", "Last successful poll"),
                           ("accounts", "Accounts reachable")):
            row = QHBoxLayout()
            name = QLabel(label)
            name.setProperty("secondary", True)
            row.addWidget(name)
            row.addStretch(1)
            value = QLabel("—")
            value.setFont(mono_font(8))
            row.addWidget(value)
            self._rows[key] = value
            self._v.addLayout(row)
        rerun = QPushButton("Check again")
        rerun.setObjectName("ghostButton")
        rerun.setFixedHeight(30)
        rerun.clicked.connect(self.rerun_requested.emit)
        self._v.addWidget(rerun)

    def set_row(self, key: str, text: str) -> None:
        if key in self._rows:
            self._rows[key].setText(text)

    def set_verdict(self, text: str, tone: str) -> None:
        self.verdict.set_text(text)
        self.verdict.set_tone(tone)
