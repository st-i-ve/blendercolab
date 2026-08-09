"""The one place the user picks BlendFleet's accent colour.

Five accents are on offer -- orange (the default Blender-orange), green,
purple, blue, red -- each a named swatch, never colour alone (see
theme.WARNING's own docstring: roughly 8% of men cannot reliably tell red
from green, so every swatch is labelled with its name as text, and the
selected one also carries a check ICON, not just a highlighted border).

Applies the choice live: clicking a swatch calls theme.apply() on the
running QApplication immediately -- no separate "Apply" step -- and
persists it via Settings.save() so it survives a restart. theme.apply()
itself fires theme.theme_signal.changed, which is what lets Dashboard (and
every InstanceCard) repaint their accent-tinted chrome without a restart;
this dialog does not talk to those widgets directly.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from blendfleet.settings import Settings
from blendfleet.ui import theme
from blendfleet.ui.theme import ACCENTS, TEXT_PRIMARY, icon, ui_font

SWATCH_SIZE = 56


class _AccentSwatch(QWidget):
    """One accent: a coloured square, its name underneath, and a check
    icon that only appears on the selected one. Clicking anywhere on the
    swatch (button or name) selects it."""

    picked = Signal(str)

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name = name
        palette = ACCENTS[name]

        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(8)

        self.button = QPushButton()
        self.button.setCheckable(True)
        self.button.setFixedSize(SWATCH_SIZE, SWATCH_SIZE)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setStyleSheet(f"""
            QPushButton {{
                background-color: {palette.base};
                border: 2px solid transparent;
                border-radius: 10px;
            }}
            QPushButton:hover {{
                border: 2px solid {palette.hover};
            }}
            QPushButton:checked {{
                border: 2px solid {TEXT_PRIMARY};
            }}
        """)
        self.button.clicked.connect(lambda: self.picked.emit(name))
        v.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)

        self.name_label = QLabel(name.capitalize())
        self.name_label.setFont(ui_font(9))
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.name_label)

        self.check_label = QLabel()
        self.check_label.setFixedHeight(16)
        self.check_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.check_label)

    def set_selected(self, selected: bool) -> None:
        self.button.setChecked(selected)
        if selected:
            self.check_label.setPixmap(
                icon("check", TEXT_PRIMARY, 14).pixmap(14, 14))
        else:
            self.check_label.clear()


class SettingsView(QDialog):
    """Modal settings dialog -- currently just the accent picker, but its
    own module/class so a future setting (e.g. poll interval) has an
    obvious home instead of getting bolted onto SetupDialog."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("BlendFleet — settings")
        self.setMinimumWidth(420)

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 20, 20, 16)
        v.setSpacing(16)

        heading = QLabel("<b>Accent colour</b>")
        v.addWidget(heading)
        sub = QLabel(
            "Used for buttons, the active-render highlight, and the app "
            "mark. Applies immediately and is remembered for next time.")
        sub.setWordWrap(True)
        sub.setProperty("secondary", True)
        v.addWidget(sub)

        row = QHBoxLayout()
        row.setSpacing(12)
        self._swatches: dict[str, _AccentSwatch] = {}
        for name in ACCENTS:
            swatch = _AccentSwatch(name)
            swatch.picked.connect(self._on_picked)
            self._swatches[name] = swatch
            row.addWidget(swatch)
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)

        done = QPushButton("Done")
        done.setObjectName("primaryButton")
        done.clicked.connect(self.accept)
        v.addWidget(done, 0, Qt.AlignmentFlag.AlignRight)

        self._sync_selection()

    def _on_picked(self, name: str) -> None:
        self.settings.accent = name
        self.settings.save()
        app = self._app()
        if app is not None:
            theme.apply(app, name)
        self._sync_selection()

    def _sync_selection(self) -> None:
        for name, swatch in self._swatches.items():
            swatch.set_selected(name == self.settings.accent)

    @staticmethod
    def _app():
        from PySide6.QtWidgets import QApplication
        return QApplication.instance()
