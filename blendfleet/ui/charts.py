"""Custom-painted charts: the frame filmstrip and per-GPU sparklines.

No QtCharts dependency -- both are drawn directly with QPainter over small,
bounded data (a ring buffer of recent telemetry samples; one cell per
frame in the render range), which is all either of these needs.
"""
from __future__ import annotations

from collections import deque

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGridLayout, QLabel, QSizePolicy, QWidget

from blendfleet.fleet import WorkerState
from blendfleet.ui.formatting import format_bytes
from blendfleet.ui.theme import (account_color, current_accent, current_theme,
                                  mono_font)

# States fleet.WorkerState.state can carry that mean "this worker will not
# render any more of its assigned frames" -- the remainder of its stride is
# painted as a gap/failure rather than "still coming".
_STOPPED_STATES = {"error", "cancel_acknowledged", "cancel_requested"}


# ---------------- pure logic (unit-testable without a QPainter) ----------

def frame_owners(start_frame: int, end_frame: int,
                  workers: list[WorkerState]) -> list[int | None]:
    """Which worker index (into `workers`) owns each frame in
    [start_frame, end_frame], or None if no worker claims it.

    Frames are assigned by stride (assignment.assign_frames), so this just
    inverts worker.frames back to a per-frame owner lookup -- the thing the
    filmstrip actually paints.
    """
    owner_by_frame: dict[int, int] = {}
    for idx, w in enumerate(workers):
        for f in w.frames:
            owner_by_frame[f] = idx
    return [owner_by_frame.get(f) for f in range(start_frame, end_frame + 1)]


def frame_done(start_frame: int, end_frame: int,
               workers: list[WorkerState]) -> list[bool]:
    """Whether each frame in [start_frame, end_frame] has been rendered --
    APPROXIMATELY. Read the caveat below before trusting a cell.

    WorkerState carries only a *count* of completed frames (frames_done),
    never which frame numbers, and that count comes from the notebook's
    `done=` field (notebook_builder.py:184), which counts SUCCESSES ONLY:
    a frame whose Blender subprocess exits non-zero goes into `failed` and
    does not advance `done`. Frames are attempted in order, so as long as
    nothing fails, the first `frames_done` entries of worker.frames are
    exactly the finished ones and this is exact.

    The moment ONE frame fails, it is not. Say a worker owns [1, 4, 7, 10]
    and frame 4 fails: done=3 after frame 10, and this function reports
    1, 4 and 7 as complete -- frame 4 is painted done although it does not
    exist, and frame 10 is painted pending although it does. Every later
    cell for that worker is shifted by one per failure.

    Fixing this properly means carrying explicit frame numbers all the way
    from the notebook's PROGRESS line (which does print `frame=`) through
    log_stream.parse_progress and WorkerState. Until then the
    approximation is stated in the UI itself -- the filmstrip's own header
    label in dashboard.py says so in words -- rather than only here, so
    the person looking at the strip knows what it can and cannot tell them.
    """
    done_frames: set[int] = set()
    for w in workers:
        done_frames.update(w.frames[:max(w.frames_done, 0)])
    return [f in done_frames for f in range(start_frame, end_frame + 1)]


def frame_stopped(start_frame: int, end_frame: int,
                   workers: list[WorkerState]) -> list[bool]:
    """Whether each frame belongs to a worker that has stopped rendering
    (errored or been cancelled) before reaching that frame -- painted as a
    gap/failure rather than "still coming"."""
    stopped_frames: set[int] = set()
    for w in workers:
        if w.state in _STOPPED_STATES:
            stopped_frames.update(w.frames[max(w.frames_done, 0):])
    return [f in stopped_frames for f in range(start_frame, end_frame + 1)]


# ---------------- Filmstrip ----------------

class Filmstrip(QWidget):
    """The signature element: a horizontal strip of cells, one per frame
    in the render range, tinted by which account rendered it.

    Because frames are assigned by stride (worker i takes frames
    i, i+n, i+2n, ...), the strip visibly interleaves account colours, and
    a missing/failed frame reads as a gap instantly. For 250+ frames the
    cells become thin vertical bars, which is deliberate -- it still shows
    the interleave and the gaps at a glance.
    """

    MIN_HEIGHT = 40

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(self.MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                          QSizePolicy.Policy.Fixed)
        self._start_frame = 0
        self._end_frame = -1
        self._owners: list[int | None] = []
        self._done: list[bool] = []
        self._stopped: list[bool] = []

    def set_empty(self) -> None:
        """No job loaded yet -- an empty strip, not a blank widget: the
        cells are still drawn, all as gaps, so the user can see the range
        is simply not started rather than wondering if the widget is
        broken."""
        self._start_frame = 0
        self._end_frame = -1
        self._owners = []
        self._done = []
        self._stopped = []
        self.update()

    def set_workers(self, start_frame: int, end_frame: int,
                    workers: list[WorkerState]) -> None:
        self._start_frame = start_frame
        self._end_frame = end_frame
        self._owners = frame_owners(start_frame, end_frame, workers)
        self._done = frame_done(start_frame, end_frame, workers)
        self._stopped = frame_stopped(start_frame, end_frame, workers)
        self.update()

    @property
    def total_frames(self) -> int:
        return len(self._owners)

    @property
    def done_count(self) -> int:
        return sum(self._done)

    def paintEvent(self, event) -> None:  # noqa: N802 -- Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = self.rect()
        painter.fillRect(rect, QColor(current_theme().fill))

        n = len(self._owners)
        if n == 0:
            painter.end()
            return

        width = rect.width()
        height = rect.height()
        cell_w = max(width / n, 1.0)
        gap_color = QColor(current_theme().fill)
        stopped_color = QColor(current_theme().warn)

        for i in range(n):
            x0 = i * cell_w
            x1 = (i + 1) * cell_w if i < n - 1 else width
            cell_rect = QRectF(x0, 0, max(x1 - x0, 1.0), height)
            if self._stopped[i]:
                painter.fillRect(cell_rect, stopped_color)
                continue
            owner = self._owners[i]
            if owner is None:
                painter.fillRect(cell_rect, gap_color)
                continue
            base = account_color(owner)
            if self._done[i]:
                painter.fillRect(cell_rect, base)
            else:
                # Assigned but not yet rendered: paint the gap background
                # first, then the account tint at partial alpha, so a
                # pending cell reads as "this account's turn is coming"
                # and is never mistaken for a completed one.
                painter.fillRect(cell_rect, gap_color)
                faded = QColor(base)
                faded.setAlpha(90)
                painter.fillRect(cell_rect, faded)
        painter.end()


# ---------------- Sparkline ----------------

class Sparkline(QWidget):
    """A small line chart over a bounded ring buffer of recent samples.

    Used for per-GPU utilisation % and VRAM used -- never aggregated
    across GPUs (see GpuPanel), and never backed by QtCharts.
    """

    def __init__(self, capacity: int = 60, minimum: float = 0.0,
                 maximum: float = 100.0, color: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(90, 28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                          QSizePolicy.Policy.Fixed)
        self._buf: deque[float] = deque(maxlen=capacity)
        self._minimum = minimum
        self._maximum = maximum
        # None means "follow the accent", resolved at PAINT time rather
        # than stored -- the reference draws its instance sparklines in
        # var(--accent), and a colour captured here would freeze at
        # whichever accent happened to be active when the widget was built.
        self._color_override = color

    def push(self, value: float) -> None:
        self._buf.append(value)
        self.update()

    def clear(self) -> None:
        self._buf.clear()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        painter.fillRect(rect, QColor(current_theme().fill))
        if len(self._buf) < 2:
            painter.end()
            return

        span = max(self._maximum - self._minimum, 1e-9)
        n = len(self._buf)
        w = rect.width()
        h = rect.height()

        def point(i: int, value: float):
            x = w * i / (n - 1)
            frac = (value - self._minimum) / span
            frac = min(max(frac, 0.0), 1.0)
            y = h - frac * h
            return x, y

        pen = QPen(QColor(self._color_override or current_accent().base))
        pen.setWidthF(1.6)
        painter.setPen(pen)
        prev = None
        for i, value in enumerate(self._buf):
            cur = point(i, value)
            if prev is not None:
                painter.drawLine(prev[0], prev[1], cur[0], cur[1])
            prev = cur
        painter.end()


# ---------------- GPU panel ----------------

class GpuRow(QWidget):
    """One physical GPU on one account's Kaggle session: label,
    utilisation sparkline, memory sparkline, and monospace stats. GPUs are
    never aggregated -- Kaggle's allocation is not guaranteed (a request
    for T4 x2 has come back as a single P100), and each account renders on
    its own separate machine, so a "GPU 0" from one account is not the same
    physical device as "GPU 0" from another -- the account label is part
    of the row's identity, not decoration.
    """

    def __init__(self, account_label: str, gpu_index: int,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.account_label = account_label
        self.gpu_index = gpu_index
        layout = QGridLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)

        self.title = QLabel(f"{account_label} · GPU {gpu_index}")
        self.title.setFont(mono_font(9))

        self.util_spark = Sparkline(minimum=0, maximum=100)
        self.util_label = QLabel("util —")
        self.util_label.setFont(mono_font(9))

        self.mem_spark = Sparkline(minimum=0, maximum=100)
        self.mem_label = QLabel("mem —")
        self.mem_label.setFont(mono_font(9))

        layout.addWidget(self.title, 0, 0)
        layout.addWidget(QLabel("util"), 0, 1)
        layout.addWidget(self.util_spark, 0, 2)
        layout.addWidget(self.util_label, 0, 3)
        layout.addWidget(QLabel("mem"), 0, 4)
        layout.addWidget(self.mem_spark, 0, 5)
        layout.addWidget(self.mem_label, 0, 6)
        layout.setColumnStretch(2, 1)
        layout.setColumnStretch(5, 1)

    def update_sample(self, util: int, mem_used: int, mem_total: int,
                      temp: int, power: float | None) -> None:
        self.util_spark.push(util)
        self.util_label.setText(f"{util:3d}%")
        mem_pct = (mem_used / mem_total * 100.0) if mem_total else 0.0
        self.mem_spark.push(mem_pct)
        used_gb = format_bytes(mem_used * (1 << 20))
        total_gb = format_bytes(mem_total * (1 << 20))
        self.mem_label.setText(f"{used_gb}/{total_gb}")


class GpuPanel(QWidget):
    """Container that creates one GpuRow per (account, physical GPU index)
    the telemetry stream reports, in the order first seen. Renders 1 GPU
    or 4 per account with no special-casing -- there is no "expected" GPU
    count anywhere in this widget, and one account's GPUs are never merged
    with another's."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._rows: dict[tuple[str, int], GpuRow] = {}
        self._empty_label = QLabel("no GPU telemetry yet")
        self._empty_label.setProperty("secondary", True)
        self._layout.addWidget(self._empty_label, 0, 0)

    def ingest(self, account_label: str, record: dict) -> None:
        key = (account_label, record["gpu"])
        if key not in self._rows:
            if not self._rows:
                self._empty_label.hide()
            row = GpuRow(account_label, record["gpu"])
            self._rows[key] = row
            self._layout.addWidget(row, len(self._rows) - 1, 0)
        self._rows[key].update_sample(
            record["util"], record["mem_used"], record["mem_total"],
            record["temp"], record["power"])

    def clear(self) -> None:
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._empty_label.show()

    @property
    def gpu_count(self) -> int:
        return len(self._rows)
