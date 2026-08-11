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
from PySide6.QtWidgets import (QAbstractButton, QDialog, QHBoxLayout,
                               QLabel, QPushButton, QSpinBox, QVBoxLayout,
                               QWidget)

from blendfleet.settings import Settings
from blendfleet.ui import mica, theme
from blendfleet.ui.theme import (ACCENTS, THEMES, current_theme, icon,
                                  ui_font)

SWATCH_SIZE = 56
SPIN_WIDTH = 160


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
                border: 2px solid {current_theme().ink};
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
                icon("check", current_theme().ink, 14).pixmap(14, 14))
        else:
            self.check_label.clear()


class Toggle(QAbstractButton):
    """The reference's .toggle -- a pill switch with a sliding knob.

    A real switch rather than a QCheckBox because the reference's settings
    rows are "label on the left, control on the right", and a checkbox's
    box-then-label shape cannot sit on the right without reading as
    backwards. Checkable, so `toggled` and `setChecked` work exactly as any
    other Qt button.
    """

    TRACK_W, TRACK_H, KNOB = 42, 22, 16

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("toggleTrack")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.TRACK_W, self.TRACK_H)
        self.knob = QWidget(self)
        self.knob.setObjectName("toggleKnob")
        self.knob.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.knob.setFixedSize(self.KNOB, self.KNOB)
        self._sync()
        self.toggled.connect(lambda _on: self._sync())

    def paintEvent(self, event) -> None:   # noqa: N802 -- Qt override
        pass                               # track and knob are both QSS

    def _sync(self) -> None:
        on = self.isChecked()
        pad = (self.TRACK_H - self.KNOB) // 2
        x = self.TRACK_W - self.KNOB - pad if on else pad
        self.knob.move(x, pad)
        self.setProperty("on", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class SettingsPanel(QWidget):
    """The settings controls themselves, with no window around them: the
    accent picker plus the minimum-GPU render gate.

    Split out from SettingsView so the Dashboard's Settings PAGE and the
    standalone dialog are the same widget rather than two implementations
    that drift. Everything that was on SettingsView -- `_swatches`,
    `min_gpus_spin`, `_on_picked` -- lives here now, and the dialog
    forwards to it.
    """

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(16)

        theme_heading = QLabel("<b>Theme</b>")
        v.addWidget(theme_heading)
        theme_sub = QLabel(
            "Light for daylight suites, dark for night shifts. Applies "
            "immediately and is remembered for next time.")
        theme_sub.setWordWrap(True)
        theme_sub.setProperty("secondary", True)
        v.addWidget(theme_sub)

        # A segmented control -- the reference's .seg -- rather than a
        # checkbox: "Light / Dark" names both states, where a checkbox
        # labelled "Dark mode" only names one and leaves the other implied.
        theme_row = QHBoxLayout()
        theme_row.setSpacing(0)
        self._theme_buttons: dict[str, QPushButton] = {}
        for name in THEMES:
            button = QPushButton(name.capitalize())
            button.setObjectName("segment")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, n=name: self._on_theme_picked(n))
            self._theme_buttons[name] = button
            theme_row.addWidget(button)
        theme_row.addStretch(1)
        v.addLayout(theme_row)

        translucent_row = QHBoxLayout()
        translucent_text = QVBoxLayout()
        translucent_text.setSpacing(2)
        translucent_heading = QLabel("<b>Translucent surfaces</b>")
        translucent_text.addWidget(translucent_heading)
        self.translucent_note = QLabel("")
        self.translucent_note.setWordWrap(True)
        self.translucent_note.setProperty("secondary", True)
        translucent_text.addWidget(self.translucent_note)
        translucent_row.addLayout(translucent_text, 1)
        self.translucent_toggle = Toggle()
        self.translucent_toggle.setChecked(self.settings.translucent)
        self.translucent_toggle.toggled.connect(self._on_translucent_toggled)
        translucent_row.addWidget(self.translucent_toggle, 0,
                                  Qt.AlignmentFlag.AlignTop)
        v.addLayout(translucent_row)
        # Say plainly when the machine cannot do it, rather than offering a
        # switch that silently changes nothing.
        if mica.is_supported():
            self.translucent_note.setText(
                "Let the desktop show through the app shell. Panels and "
                "cards stay solid so text never lands on your wallpaper.")
        else:
            self.translucent_note.setText(
                "Not available on this system — the desktop backdrop "
                "needs Windows 11 (build 22621 or newer).")
            self.translucent_toggle.setEnabled(False)

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

        gpu_heading = QLabel("<b>Minimum GPUs required</b>")
        v.addWidget(gpu_heading)
        gpu_sub = QLabel(
            "A new render will not start on an account whose Kaggle "
            "session reports fewer GPUs than this -- Kaggle can silently "
            "fall back to a single P100 if the requested hardware is not "
            "available, and this is what stops a long render from "
            "proceeding on far less GPU than expected. 0 turns the check "
            "off entirely.")
        gpu_sub.setWordWrap(True)
        gpu_sub.setProperty("secondary", True)
        v.addWidget(gpu_sub)

        self.min_gpus_spin = QSpinBox()
        # Capped and left-aligned rather than stretched: on the Settings
        # PAGE this sits in a column over a thousand pixels wide, and a
        # number entry field that wide reads as unfinished, not as usable.
        self.min_gpus_spin.setFixedWidth(SPIN_WIDTH)
        self.min_gpus_spin.setRange(0, 8)
        self.min_gpus_spin.setValue(self.settings.min_gpus)
        self.min_gpus_spin.valueChanged.connect(self._on_min_gpus_changed)
        v.addWidget(self.min_gpus_spin, 0, Qt.AlignmentFlag.AlignLeft)

        self._sync_selection()

    def _on_picked(self, name: str) -> None:
        self.settings.accent = name
        self.settings.save()
        self._reapply()

    def _on_theme_picked(self, name: str) -> None:
        self.settings.theme = name
        self.settings.save()
        self._reapply()

    def _reapply(self) -> None:
        """Push accent AND theme to the running app together.

        Both in one call, never one then the other: theme.apply() rebuilds
        the whole stylesheet from the pair it is given, so applying them
        separately would repaint the app once with the old half of the
        combination still in force -- a visible flash of the wrong colours.
        """
        app = self._app()
        if app is not None:
            theme.apply(app, self.settings.accent, self.settings.theme)
        self._sync_selection()

    def _on_translucent_toggled(self, on: bool) -> None:
        self.settings.translucent = bool(on)
        self.settings.save()
        # Not applied through theme.apply(): the backdrop is a WINDOW
        # attribute, not a stylesheet, so the window itself has to re-ask
        # DWM for it. theme_signal is the existing "chrome changed" channel
        # and Dashboard already re-runs _apply_window_effects on it.
        theme.theme_signal.changed.emit()

    def _on_min_gpus_changed(self, value: int) -> None:
        self.settings.min_gpus = value
        self.settings.save()

    def _sync_selection(self) -> None:
        for name, swatch in self._swatches.items():
            swatch.set_selected(name == self.settings.accent)
        for name, button in self._theme_buttons.items():
            # QSS selects on the dynamic property; Qt does not re-evaluate
            # that on its own, so the widget has to be re-polished.
            button.setProperty(
                "active", "true" if name == self.settings.theme else "false")
            button.style().unpolish(button)
            button.style().polish(button)

    @staticmethod
    def _app():
        from PySide6.QtWidgets import QApplication
        return QApplication.instance()


class SettingsView(QDialog):
    """The standalone, modal form of SettingsPanel.

    The Dashboard embeds SettingsPanel directly as a page, so this is no
    longer how settings are normally reached -- it is kept as the dialog
    form for any context with no page stack to put a panel in, and because
    a modal is still the right shape if settings ever need to be raised
    from on top of another dialog.

    `_swatches`, `min_gpus_spin` and `_on_picked` are forwarded rather than
    reimplemented: there is exactly one settings implementation, and it is
    the panel's.
    """

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("BlendFleet — settings")
        self.setMinimumWidth(420)

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 20, 20, 16)
        v.setSpacing(16)
        self.panel = SettingsPanel(settings, self)
        v.addWidget(self.panel)
        v.addStretch(1)

        done = QPushButton("Done")
        done.setObjectName("primaryButton")
        done.clicked.connect(self.accept)
        v.addWidget(done, 0, Qt.AlignmentFlag.AlignRight)

    # ---- forwarded surface (see class docstring) ----
    @property
    def _swatches(self) -> dict:
        return self.panel._swatches

    @property
    def min_gpus_spin(self) -> QSpinBox:
        return self.panel.min_gpus_spin

    def _on_picked(self, name: str) -> None:
        self.panel._on_picked(name)
