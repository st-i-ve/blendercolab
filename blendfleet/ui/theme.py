"""One theme, applied at QApplication level.

BlendFleet renders frames borrowed across a handful of friends' Kaggle
accounts -- the subject is a render farm assembled out of people, not a
generic SaaS dashboard. The palette is warm-dark (Blender-orange accent on
a charcoal-brown shell) rather than the near-black-plus-acid-accent look
most dark UIs default to, and every numeric/machine value (bytes, rates,
frame numbers, GPU stats, quota) is set in a monospace face with tabular
figures so it reads as an instrument panel: numbers hold their width tick
to tick instead of reflowing.

Applied ONCE, in __main__.py, at the QApplication level -- see
apply_theme() -- so no dialog (SetupDialog included) is left unstyled.
Never import a raw QColor literal for UI chrome anywhere else; add it
here instead, so the whole app can only ever have one look.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication

# ---------------- palette ----------------
# Named, not inlined at call sites: a hex literal scattered through the UI
# code is how apps end up half-restyled. Everything here traces back to
# this one dict.
BG_SHELL = "#16181D"        # window / shell background
BG_SURFACE = "#1E212A"      # raised surfaces: rail, cards, table
BORDER = "#2A2F3A"          # dividers, borders, unfilled/gap cells
ACCENT = "#F5792A"          # primary accent -- Blender orange
WARNING = "#E8B33A"         # unverified / retrying / failed -- amber, NEVER
                             # paired with a red/green opposite: ~8% of men
                             # cannot reliably tell that pair apart.
TELEMETRY = "#4FD1C5"       # GPU utilisation/memory sparklines -- a cool
                             # teal so telemetry never competes visually
                             # with the orange accent used for actions/state.
TEXT_PRIMARY = "#C9CEDB"
TEXT_SECONDARY = "#7C859B"

# A small, fixed set of account tint colours for the filmstrip and rail
# status dots. Cycled by account index. Deliberately distinct in both hue
# AND lightness from each other and from WARNING/ACCENT, so an account's
# tint is never confusable with a status colour.
ACCOUNT_COLORS = [
    "#F5792A",  # account 0 -- accent orange doubles as "you"
    "#4FD1C5",  # account 1 -- teal
    "#9B7EDE",  # account 2 -- violet
    "#5FB0F0",  # account 3 -- sky blue
    "#E85D9E",  # account 4 -- pink
    "#8BC34A",  # account 5 -- muted green (paired with word/symbol, never
                # relied on alone, so it is safe alongside WARNING amber)
]


def account_color(index: int) -> QColor:
    """The tint for account `index`, cycling through ACCOUNT_COLORS."""
    return QColor(ACCOUNT_COLORS[index % len(ACCOUNT_COLORS)])


# ---------------- type ----------------
UI_FONT_FAMILY = "Segoe UI Variable"
UI_FONT_FALLBACK = "Segoe UI"
MONO_FONT_FAMILY = "Cascadia Mono"
MONO_FONT_FALLBACK = "Consolas"


def mono_font(point_size: int = 9) -> QFont:
    """The font for every numeric/machine value: byte counts, rates, frame
    numbers, GPU stats, quota. Tabular figures so digits never reflow as
    they tick over."""
    font = QFont(MONO_FONT_FAMILY)
    font.setFamilies([MONO_FONT_FAMILY, MONO_FONT_FALLBACK, "monospace"])
    font.setPointSize(point_size)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
    return font


def ui_font(point_size: int = 9) -> QFont:
    font = QFont(UI_FONT_FAMILY)
    font.setFamilies([UI_FONT_FAMILY, UI_FONT_FALLBACK, "sans-serif"])
    font.setPointSize(point_size)
    return font


STYLESHEET = f"""
* {{
    color: {TEXT_PRIMARY};
    font-family: "{UI_FONT_FAMILY}", "{UI_FONT_FALLBACK}", sans-serif;
    font-size: 10pt;
}}

QWidget {{
    background-color: {BG_SHELL};
}}

QMainWindow, QDialog {{
    background-color: {BG_SHELL};
}}

QLabel {{
    background: transparent;
}}

QLabel[secondary="true"] {{
    color: {TEXT_SECONDARY};
}}

#rail, #card, QListWidget, QTableWidget {{
    background-color: {BG_SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}

QPushButton {{
    background-color: {BG_SURFACE};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 14px;
    color: {TEXT_PRIMARY};
}}

QPushButton:hover {{
    border-color: {ACCENT};
}}

QPushButton:pressed {{
    background-color: {ACCENT};
    color: {BG_SHELL};
}}

QPushButton:disabled {{
    color: {TEXT_SECONDARY};
    border-color: {BORDER};
}}

QPushButton:focus, QSpinBox:focus, QComboBox:focus, QLineEdit:focus,
QListWidget:focus, QTableWidget:focus {{
    border: 2px solid {ACCENT};
}}

#primaryButton {{
    background-color: {ACCENT};
    color: {BG_SHELL};
    font-weight: 600;
    border: 1px solid {ACCENT};
}}

#primaryButton:hover {{
    background-color: #FF8A3D;
}}

#primaryButton:disabled {{
    background-color: {BORDER};
    color: {TEXT_SECONDARY};
    border-color: {BORDER};
}}

QLineEdit, QSpinBox, QComboBox {{
    background-color: {BG_SHELL};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 6px;
    selection-background-color: {ACCENT};
}}

QListWidget::item, QTableWidget::item {{
    padding: 4px;
}}

QListWidget::item:selected, QTableWidget::item:selected {{
    background-color: {BORDER};
}}

QHeaderView::section {{
    background-color: {BG_SURFACE};
    color: {TEXT_SECONDARY};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 4px;
}}

QProgressBar {{
    background-color: {BG_SHELL};
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
    color: {TEXT_PRIMARY};
}}

QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 3px;
}}

QScrollBar:vertical, QScrollBar:horizontal {{
    background: {BG_SHELL};
    border: none;
}}

QScrollBar::handle {{
    background: {BORDER};
    border-radius: 4px;
}}

QToolTip {{
    background-color: {BG_SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
}}

QMessageBox {{
    background-color: {BG_SURFACE};
}}
"""


def apply_theme(app: QApplication) -> None:
    """Apply the one BlendFleet theme to the whole application.

    Call this exactly once, right after constructing QApplication, before
    any window or dialog is shown -- a stylesheet set at the QApplication
    level cascades to every widget created afterwards, including dialogs
    (SetupDialog) opened later. That is the mechanism that guarantees no
    dialog is ever left unstyled.
    """
    app.setStyle("Fusion")
    app.setFont(ui_font())
    app.setStyleSheet(STYLESHEET)
