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

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

# ---------------- palette ----------------
# Named, not inlined at call sites: a hex literal scattered through the UI
# code is how apps end up half-restyled. Everything here traces back to
# this one dict.
BG_SHELL = "#16181D"        # window / shell background
BG_SURFACE = "#1E212A"      # raised surfaces: rail, cards, table
BORDER = "#2A2F3A"          # dividers, borders, unfilled/gap cells
WARNING = "#E8B33A"         # unverified / retrying / failed -- amber, NEVER
                             # paired with a red/green opposite: ~8% of men
                             # cannot reliably tell that pair apart. This is
                             # a FIXED colour, independent of the selected
                             # accent -- see AccentPalette below. If it were
                             # derived from the accent, picking the red
                             # accent would make error states visually
                             # indistinguishable from ordinary chrome.
TELEMETRY = "#4FD1C5"       # GPU utilisation/memory sparklines -- a cool
                             # teal so telemetry never competes visually
                             # with the accent used for actions/state.
TEXT_PRIMARY = "#C9CEDB"
TEXT_SECONDARY = "#7C859B"


# ---------------- accents ----------------
@dataclass(frozen=True)
class AccentPalette:
    """The token set one accent colour expands into.

    hover/pressed/disabled are computed from `base` (see `_accent` below)
    rather than hand-listed per colour, so adding a new accent to ACCENTS
    is one line, and every accent is guaranteed to define the full set --
    no KeyError at paint time for a colour that forgot a shade.
    """
    base: str
    hover: str
    pressed: str
    disabled: str


def _mix(a: QColor, b: QColor, t: float) -> str:
    """Linear RGB blend of `a` toward `b` by `t` in [0, 1], as a hex string."""
    r = round(a.red() * (1 - t) + b.red() * t)
    g = round(a.green() * (1 - t) + b.green() * t)
    bch = round(a.blue() * (1 - t) + b.blue() * t)
    return QColor(r, g, bch).name()


def _accent(base: str) -> AccentPalette:
    c = QColor(base)
    return AccentPalette(
        base=base,
        hover=c.lighter(115).name(),
        pressed=c.darker(115).name(),
        # Muted toward the border colour, not black -- a disabled accent
        # chip should read as "this surface", not "this surface plus a
        # shadow".
        disabled=_mix(c, QColor(BORDER), 0.6),
    )


# Every base value below is checked against BG_SHELL for >= 4.5:1 contrast
# in tests/test_theme.py using the WCAG relative-luminance formula -- not
# eyeballed. Red is the accent most likely to run short of that margin
# against a dark shell, so its base sits comfortably above the threshold
# (~4.9:1) rather than right at it.
ACCENTS: dict[str, AccentPalette] = {
    "orange": _accent("#F5792A"),   # Blender orange -- the original accent
    "green": _accent("#4CAF6D"),
    "purple": _accent("#9B7EDE"),
    "blue": _accent("#5FB0F0"),
    "red": _accent("#E85454"),
}
DEFAULT_ACCENT = "orange"


def resolve_accent(name: str) -> AccentPalette:
    """The named palette, or the default's if `name` is not one ACCENTS
    knows about.

    Falling back rather than raising is deliberate: a hand-edited config,
    or a settings.json written by a future version with an accent this
    build has never heard of, must not brick the app on startup with no
    UI left to fix it from.
    """
    return ACCENTS.get(name, ACCENTS[DEFAULT_ACCENT])


# Backward-compatible module-level accent -- other modules import this by
# value (`from blendfleet.ui.theme import ACCENT`), which only reflects
# whichever accent was active at import time. Re-theming code that needs
# to follow a live accent switch should use resolve_accent()/apply()
# instead of this constant.
ACCENT = ACCENTS[DEFAULT_ACCENT].base

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
# One typeface (Roboto, in its proportional and monospace forms), bundled
# and registered from assets/fonts/ rather than relying on whatever the OS
# happens to have installed -- Segoe UI Variable/Cascadia Mono are Windows
# names that would silently be a different look (or a different font
# entirely) on the Linux build this app is headed for. See register_fonts().
UI_FONT_FAMILY = "Roboto"
UI_FONT_FALLBACK = "Segoe UI"
MONO_FONT_FAMILY = "Roboto Mono"
MONO_FONT_FALLBACK = "Consolas"

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
ICONS_DIR = ASSETS_DIR / "icons"
LOGO_DIR = ASSETS_DIR / "logo"

# The five TTFs vendored under assets/fonts/ (Apache-2.0) -- every one of
# them must register, not just enough to make the family name resolve, so
# that requesting the Medium/Bold weights doesn't silently synthesise them.
FONT_FILES = [
    "Roboto-Regular.ttf",
    "Roboto-Medium.ttf",
    "Roboto-Bold.ttf",
    "RobotoMono-Regular.ttf",
    "RobotoMono-Medium.ttf",
]

_registered_font_families: list[str] | None = None


def register_fonts() -> list[str]:
    """Register the bundled Roboto/Roboto Mono fonts with Qt's font
    database and return the family names Qt resolved each file to.

    Must run after a QApplication/QGuiApplication exists (QFontDatabase
    needs one). A silent fallback to a system face is the failure mode
    this guards against -- addApplicationFont() returning a non-negative
    id only means Qt parsed the file, not that the family name callers
    expect ("Roboto", "Roboto Mono") is what came back. Idempotent: calling
    it again (e.g. because apply() ran again for an accent switch) does
    not re-register the files, it just returns the cached result.
    """
    global _registered_font_families
    if _registered_font_families is not None:
        return _registered_font_families
    families: list[str] = []
    for filename in FONT_FILES:
        path = FONTS_DIR / filename
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id == -1:
            raise RuntimeError(
                f"failed to register bundled font {filename!r} from {path} "
                "-- Qt could not parse it")
        resolved = QFontDatabase.applicationFontFamilies(font_id)
        if not resolved:
            raise RuntimeError(
                f"{filename!r} registered but Qt returned no family name "
                "for it")
        families.extend(resolved)
    _registered_font_families = families
    return families


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


def _stylesheet(accent: AccentPalette) -> str:
    """The whole application stylesheet, parameterised by the active
    accent. Everything that used to be the literal ACCENT constant now
    reads from `accent.base`/`accent.hover` so switching accents at
    runtime is a matter of calling this again with a different palette."""
    return f"""
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
    border-color: {accent.base};
}}

QPushButton:pressed {{
    background-color: {accent.base};
    color: {BG_SHELL};
}}

QPushButton:disabled {{
    color: {TEXT_SECONDARY};
    border-color: {BORDER};
}}

QPushButton:focus, QSpinBox:focus, QComboBox:focus, QLineEdit:focus,
QListWidget:focus, QTableWidget:focus {{
    border: 2px solid {accent.base};
}}

#primaryButton {{
    background-color: {accent.base};
    color: {BG_SHELL};
    font-weight: 600;
    border: 1px solid {accent.base};
}}

#primaryButton:hover {{
    background-color: {accent.hover};
}}

#primaryButton:disabled {{
    background-color: {accent.disabled};
    color: {TEXT_SECONDARY};
    border-color: {BORDER};
}}

QLineEdit, QSpinBox, QComboBox {{
    background-color: {BG_SHELL};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 6px;
    selection-background-color: {accent.base};
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
    background-color: {accent.base};
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


# Backward-compatible: the default-accent stylesheet, computed eagerly.
# Nothing in this codebase imports it, but it mirrors ACCENT above so a
# caller reaching for the pre-Task-1 name still gets a working stylesheet.
STYLESHEET = _stylesheet(ACCENTS[DEFAULT_ACCENT])


def apply(app: QApplication, accent: str = DEFAULT_ACCENT) -> None:
    """Apply the BlendFleet theme to the whole application for the given
    accent, registering the bundled fonts on first call.

    Re-appliable at runtime: calling this again with a different accent
    name (e.g. after the user changes it in settings) re-derives the
    stylesheet and sets it again -- nothing here accumulates state across
    calls other than the one-time font registration. An accent name
    ACCENTS doesn't recognise falls back to the default rather than
    raising, via resolve_accent().

    Called once, right after constructing QApplication and before any
    window or dialog is shown, a stylesheet set at the QApplication level
    cascades to every widget created afterwards, including dialogs
    (SetupDialog) opened later -- that is the mechanism that guarantees no
    dialog is ever left unstyled.
    """
    register_fonts()
    palette = resolve_accent(accent)
    app.setStyle("Fusion")
    app.setFont(ui_font())
    app.setStyleSheet(_stylesheet(palette))


def apply_theme(app: QApplication) -> None:
    """Backward-compatible entry point: apply() with the default accent."""
    apply(app, DEFAULT_ACCENT)


# ---------------- icons ----------------
def icon(name: str, color: str, size: int = 24) -> QIcon:
    """Load `assets/icons/<name>.svg` and recolour its stroke to `color`.

    The bundled SVGs use stroke="currentColor" so they can be tinted to
    follow whichever accent is active without shipping a copy per colour.
    Qt's SVG renderer does not resolve CSS `currentColor` on its own, so
    the substitution happens on the raw markup before rendering.

    Raises FileNotFoundError for an unknown name and ValueError if the
    file doesn't render to a usable icon -- both cases that would
    otherwise show up at runtime as a blank square rather than a test
    failure.
    """
    path = ICONS_DIR / f"{name}.svg"
    if not path.exists():
        raise FileNotFoundError(
            f"no bundled icon named {name!r} (looked for {path})")
    svg_text = path.read_text(encoding="utf-8").replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
    if not renderer.isValid():
        raise ValueError(f"icon {name!r} did not parse as valid SVG")
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    if pixmap.isNull():
        raise ValueError(f"icon {name!r} rendered a null pixmap")
    return QIcon(pixmap)


def brand_icon(color: str, size: int = 32) -> QIcon:
    """The BlendFleet mark, tinted to `color`.

    assets/logo/mark-white.png is the tintable master `make_logo.py`
    derives from newLogo.png: flat white RGB, shape carried entirely by
    the alpha channel -- the same "recolour a stencil" contract as
    icon()'s SVGs, just rasterised instead of vector, because the source
    art is a photographed contact sheet rather than something that can be
    hand-authored as an SVG. Composited with SourceIn (paint `color` only
    where the mask has alpha) rather than a second asset per accent, so
    the in-app glyph follows the active accent the same way icon() does,
    with one file instead of five.

    Raises FileNotFoundError/ValueError on the same terms as icon(): a
    missing or unrenderable mark must fail a test, not show up as a blank
    square in the rail at runtime.
    """
    path = LOGO_DIR / "mark-white.png"
    if not path.exists():
        raise FileNotFoundError(f"brand mark not found at {path}")
    mask = QPixmap(str(path))
    if mask.isNull():
        raise ValueError(f"brand mark at {path} did not load as a pixmap")
    mask = mask.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    tinted = QPixmap(mask.size())
    tinted.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tinted)
    try:
        painter.drawPixmap(0, 0, mask)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(tinted.rect(), QColor(color))
    finally:
        painter.end()
    if tinted.isNull():
        raise ValueError("brand mark rendered a null pixmap")
    return QIcon(tinted)


# Every icon name the app is documented to reference (see the Task 1
# brief) -- tested in tests/test_theme.py so a missing file fails a test
# rather than rendering a blank square at runtime.
ICON_NAMES = [
    "check", "x", "circle-check", "circle-alert", "triangle-alert",
    "loader-circle", "upload", "download", "cpu", "activity", "settings",
    "plus", "trash-2", "play", "square", "folder-open", "users", "monitor",
]
