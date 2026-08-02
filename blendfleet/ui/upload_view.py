"""The upload view: one row per account, fed by UploadProgress ticks from
a worker thread (see dashboard.py's _LaunchWorker). All the numbers here
(bytes, %, speed, ETA, retries, resumed-from) are the thing this view
exists for: the user must be able to tell *slow* from *stuck*, which a
bare percentage cannot do on its own -- a rate reading "stalled" plus a
climbing retry count is what makes the difference visible.
"""
from __future__ import annotations

from PySide6.QtWidgets import (QLabel, QProgressBar, QSizePolicy, QVBoxLayout,
                               QWidget)

from blendfleet.ui.formatting import format_bytes, format_eta, format_rate
from blendfleet.ui.theme import TEXT_SECONDARY, WARNING, mono_font
from blendfleet.uploader import UploadProgress

STATE_IDLE = "idle"
STATE_UPLOADING = "uploading"
STATE_COMPLETE = "complete"
STATE_FAILED = "failed"


class UploadRow(QWidget):
    """One account's upload: label, progress bar, and a monospace stats
    line -- bytes, percent, rate, ETA, retries, resumed-from."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = label
        self.state = STATE_IDLE

        v = QVBoxLayout(self)
        v.setContentsMargins(4, 2, 4, 2)
        v.setSpacing(2)

        self.name_label = QLabel(f"upload    {label}")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Fixed)

        self.stats_label = QLabel(self._idle_text())
        self.stats_label.setFont(mono_font(9))
        self.stats_label.setProperty("secondary", True)

        v.addWidget(self.name_label)
        v.addWidget(self.bar)
        v.addWidget(self.stats_label)

    @staticmethod
    def _idle_text() -> str:
        return "not started yet"

    def set_idle(self) -> None:
        self.state = STATE_IDLE
        self.bar.setValue(0)
        self.stats_label.setText(self._idle_text())

    def update_progress(self, progress: UploadProgress) -> None:
        self.state = STATE_UPLOADING
        total = max(progress.total, 1)
        fraction = min(max(progress.uploaded / total, 0.0), 1.0)
        self.bar.setValue(int(fraction * 1000))

        pct = fraction * 100.0
        line = (f"{format_bytes(progress.uploaded)}/{format_bytes(progress.total)}"
               f"  {pct:5.1f}%  {format_rate(progress.rate_bps):>10}"
               f"  eta {format_eta(progress.uploaded, progress.total, progress.rate_bps)}")
        if progress.retries:
            line += f"  retries {progress.retries}"
        if progress.resumed_from:
            line += f"  resumed from {format_bytes(progress.resumed_from)}"
        self.stats_label.setText(line)

    def set_complete(self) -> None:
        self.state = STATE_COMPLETE
        self.bar.setValue(1000)
        self.stats_label.setText("done -- the .blend file finished uploading")

    def set_failed(self, message: str) -> None:
        self.state = STATE_FAILED
        self.stats_label.setText(message)
        self.stats_label.setStyleSheet(f"color: {WARNING};")


class UploadView(QWidget):
    """One UploadRow per account. Rows are created on demand (ensure_row)
    so this view works whether one account is uploading (the owner, who
    the .blend is staged through) or several are shown side by side."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._rows: dict[str, UploadRow] = {}
        self._placeholder = QLabel("no upload in progress")
        self._placeholder.setProperty("secondary", True)
        self._layout.addWidget(self._placeholder)

    def ensure_row(self, label: str) -> UploadRow:
        if label not in self._rows:
            if not self._rows:
                self._placeholder.hide()
            row = UploadRow(label)
            self._rows[label] = row
            self._layout.addWidget(row)
        return self._rows[label]

    def update_progress(self, label: str, progress: UploadProgress) -> None:
        self.ensure_row(label).update_progress(progress)

    def set_complete(self, label: str) -> None:
        self.ensure_row(label).set_complete()

    def set_failed(self, label: str, message: str) -> None:
        self.ensure_row(label).set_failed(message)

    def clear(self) -> None:
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._placeholder.show()
