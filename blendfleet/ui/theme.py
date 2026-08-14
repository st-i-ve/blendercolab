"""The whole design system: themes, accents, radii, type, icons.

Applied ONCE, in __main__.py, at the QApplication level -- see apply() --
so no dialog (SetupDialog included) is left unstyled. Never write a raw
colour literal for UI chrome anywhere else; add a token here instead, so
the whole app can only ever have one look.

TWO THEMES, NOT ONE. Every colour lives on a ThemePalette (light and dark),
resolved at PAINT time through current_theme(). Nothing may capture a
colour at import time -- see the note on ACCENT below for the bug class
that rule exists to prevent, which a theme switch would otherwise reproduce
in monochrome.

The palettes are taken value-for-value from the reference design in
ref/render-farm (7).html, which is the app's visual specification. Two
things are deliberately NOT taken from it, at the user's direction: the
brand mark stays BlendFleet's own (see brand_icon), and the type is the
bundled Roboto family rather than the reference's Roboto Condensed --
tracked_font() reproduces the condensed-caps character from what we ship.

Every numeric/machine value (bytes, rates, frame numbers, GPU stats, quota)
is set in a monospace face with tabular figures so it reads as an
instrument panel: numbers hold their width tick to tick instead of
reflowing.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication


# ---------------- themes ----------------
@dataclass(frozen=True)
class ThemePalette:
    """Every non-accent colour, for one theme.

    Field names follow the reference's own token names (bg/card/ink/fill/
    border) rather than being renamed to something more Qt-ish, so a rule
    here can be checked against the stylesheet it came from without a
    translation step in between.
    """
    name: str
    bg: str             # window / shell background
    card: str           # raised surfaces: sidebar, cards, panels, table
    fill: str           # inset fills: hover, count pills, chips, tracks
    border: str
    border_2: str       # the stronger of the two: control outlines
    ink: str            # primary text
    ink_2: str          # secondary text
    ink_3: str          # tertiary: meta, captions, placeholders
    ink_4: str          # quaternary: timestamps, disabled, empty cells
    bar: str            # neutral progress fill
    active: str         # "healthy / online" marker
    active_t: str       # the same, as a soft background wash
    active_ink: str     # the same, as legible text
    idle: str
    offline: str
    offline_t: str
    offline_ink: str
    paused_t: str
    paused_ink: str
    warn: str           # amber marker
    warn_t: str
    warn_ink: str
    # The shell colour with alpha, for the Windows 11 Mica backdrop: the
    # window has to be genuinely see-through for DWM's composite to show
    # at all. See mica.py.
    bg_translucent: str


# Both palettes are the reference's, unchanged. Light is its default.
THEMES: dict[str, ThemePalette] = {
    "light": ThemePalette(
        name="light",
        bg="#F3F3F0", card="#FDFDFC", fill="#EFEFEB",
        border="#E7E7E2", border_2="#DCDCD6",
        ink="#3A3A36", ink_2="#6E6E68", ink_3="#9A9A93", ink_4="#C6C6C0",
        bar="#8A8A83",
        active="#4CAF7D", active_t="#E4F3EB", active_ink="#3E8A64",
        idle="#B9B9B2",
        offline="#D8807F", offline_t="#F7E9E8", offline_ink="#B05F5E",
        paused_t="#E8E8E5", paused_ink="#7A7A74",
        warn="#DEA85A", warn_t="#F8EEDF", warn_ink="#A9752E",
        bg_translucent="rgba(243, 243, 240, 0.72)"),
    "dark": ThemePalette(
        name="dark",
        bg="#101210", card="#191B19", fill="#222422",
        border="#262926", border_2="#313431",
        ink="#E4E5E2", ink_2="#A5A79F", ink_3="#767871", ink_4="#4A4C47",
        bar="#6E706A",
        active="#57BE8B", active_t="#1B2A22", active_ink="#7FD3A9",
        idle="#5A5C57",
        offline="#D8807F", offline_t="#2C1F1E", offline_ink="#E5A2A1",
        paused_t="#232522", paused_ink="#A9ABA3",
        warn="#DEA85A", warn_t="#2A2419", warn_ink="#E5BE84",
        bg_translucent="rgba(16, 18, 16, 0.70)"),
}
DEFAULT_THEME = "light"

# The theme in effect app-wide right now -- one writer (apply()), any
# number of readers, exactly like _active_accent_name below.
_active_theme_name: str = DEFAULT_THEME


def resolve_theme(name: str) -> ThemePalette:
    """The named palette, or the default's. Falls back rather than raising
    for the same reason resolve_accent does: a hand-edited or forward-dated
    settings.json must not brick the app with no UI left to fix it from."""
    return THEMES.get(name, THEMES[DEFAULT_THEME])


def current_theme() -> ThemePalette:
    """The palette for whichever theme is active RIGHT NOW.

    Call this at paint/use time, inside a method -- never at module import.
    A colour captured at import is frozen at whatever the theme was then,
    which is precisely the bug documented on ACCENT below, and a theme
    switch reproduces it across every neutral in the app at once.
    """
    return THEMES[_active_theme_name]


def current_theme_name() -> str:
    return _active_theme_name


def is_dark() -> bool:
    """For the places that must know -- e.g. asking Windows for a dark
    title bar, or picking the dark variant of an accent's ink."""
    return _active_theme_name == "dark"


# ---------------- corner radii ----------------
# Three steps, not a radius per widget: floating panels (the sidebar, cards)
# are the roundest, inset controls a step tighter, small chrome tighter
# again. Named because "how round is a card" is a design decision that has
# to stay the same in every module that draws one. Values are the
# reference's --r-lg / --r-md / --r-sm.
RADIUS_LG = 22
RADIUS_MD = 14
RADIUS_SM = 10


# ---------------- accents ----------------
@dataclass(frozen=True)
class AccentPalette:
    """The token set one accent colour expands into.

    hover/pressed/disabled are computed from `base` (see `_accent` below)
    rather than hand-listed per colour, so adding a new accent to ACCENTS
    is one line, and every accent is guaranteed to define the full set --
    no KeyError at paint time for a colour that forgot a shade.

    ink_light/ink_dark are NOT computed, they are the reference's own
    per-theme values. `base` is tuned to sit ON a surface as a fill; used
    as TEXT it is too pale on a light theme and too dim on a dark one, so
    each theme names its own legible variant. Read them through ink().
    """
    base: str
    hover: str
    pressed: str
    disabled: str
    ink_light: str
    ink_dark: str

    def ink(self) -> str:
        """The accent as legible TEXT in the active theme."""
        return self.ink_dark if is_dark() else self.ink_light


def _mix(a: QColor, b: QColor, t: float) -> str:
    """Linear RGB blend of `a` toward `b` by `t` in [0, 1], as a hex string."""
    r = round(a.red() * (1 - t) + b.red() * t)
    g = round(a.green() * (1 - t) + b.green() * t)
    bch = round(a.blue() * (1 - t) + b.blue() * t)
    return QColor(r, g, bch).name()


def _accent(base: str, ink_light: str, ink_dark: str) -> AccentPalette:
    c = QColor(base)
    return AccentPalette(
        base=base,
        hover=c.lighter(115).name(),
        pressed=c.darker(115).name(),
        # Muted toward mid-grey, not black -- a disabled accent chip should
        # read as "this surface", not "this surface plus a shadow". Mixed
        # against a fixed neutral rather than a theme token because
        # ACCENTS is built at import time, before any theme is active.
        disabled=_mix(c, QColor("#8A8A83"), 0.6),
        ink_light=ink_light,
        ink_dark=ink_dark,
    )


# The reference's five swatches, value for value. Its own names for them
# are blender/sky/mint/violet/rose; the keys stay the colour words this app
# has always used, so a settings.json written by an older build still
# resolves and nobody's saved choice silently resets.
#
# Every base is checked for >= 3:1 contrast against BOTH theme backgrounds,
# and every ink for >= 4.5:1 against its own theme's surfaces, in
# tests/test_theme.py -- using the WCAG relative-luminance formula, not
# eyeballed.
ACCENTS: dict[str, AccentPalette] = {
    "orange": _accent("#E8935A", "#C06F38", "#F0AC7C"),   # blender
    "blue":   _accent("#5A9BD8", "#3D7BB8", "#8CBCE8"),   # sky
    "green":  _accent("#4DB690", "#33936F", "#7DD0B0"),   # mint
    "purple": _accent("#9B7FD4", "#7A5CB8", "#BCA5E8"),   # violet
    "red":    _accent("#D4708F", "#B34E6F", "#E89AB4"),   # rose
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
# whichever accent was active at import time. THIS IS A TRAP: a reviewer
# confirmed on Task 4 that rendering with the red accent selected produced
# zero red pixels anywhere, because instance_card.py/dashboard.py/
# setup_dialog.py all captured this constant with `from ... import ACCENT`
# at their own import time and never looked at it again. Nothing in this
# codebase may import this name any more -- call current_accent() instead,
# which re-resolves on every call. Kept only because it is cheap to keep and
# some external/future script might still reach for the old name.
ACCENT = ACCENTS[DEFAULT_ACCENT].base

# The accent actually in effect app-wide right now -- set by apply() below,
# read by current_accent(). A plain module global (not hidden behind a
# class) because it has exactly one writer (apply()) and any number of
# readers, and every reader wants the SAME process-wide value: there is
# only ever one active accent for the whole application, never one per
# widget.
_active_accent_name: str = DEFAULT_ACCENT


def current_accent() -> AccentPalette:
    """The AccentPalette for whichever accent is active RIGHT NOW.

    Call this at paint/use time (inside a method, not at module import),
    so a widget built before an accent switch still picks up the new
    colour the next time it repaints -- unlike the frozen `ACCENT`
    constant above, which is exactly the bug this function exists to fix.
    """
    return resolve_accent(_active_accent_name)


def current_accent_name() -> str:
    """The name (e.g. "purple") of the accent current_accent() resolves."""
    return _active_accent_name


class _ThemeSignal(QObject):
    """A process-wide notifier for "the active accent just changed".

    QApplication's own stylesheet cascade (see apply()) already repaints
    everything styled purely through QSS the moment setStyleSheet() is
    called again -- buttons, borders, progress bars. It does NOT repaint
    a QLabel whose colour was set with an explicit setStyleSheet()/pixmap
    call (a status icon, a brand mark tinted to the accent, a "live"
    marker) -- those were painted once, from whatever current_accent()
    returned at THAT moment, and stay that colour until something tells
    them to repaint. `changed` is that something: apply() emits it after
    every call, and any widget holding one of those explicit accent
    colours connects to it and re-fetches current_accent() when it fires.
    """
    changed = Signal()


theme_signal = _ThemeSignal()

# A small, fixed set of account tint colours for the filmstrip and the card
# header dots -- the reference's own INST_COLORS, in its order. Cycled by
# account index. Deliberately distinct in both hue AND lightness from each
# other, so an account's tint is never confusable with a status colour.
ACCOUNT_COLORS = [
    "#E8935A",  # account 0 -- the accent hue doubles as "you"
    "#5A9BD8",
    "#4DB690",
    "#9B7FD4",
    "#D4708F",
    "#D8B44C",
    "#5FB8C4",
    "#B07A5A",
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


def tracked_font(point_size: int = 8, *,
                 weight: QFont.Weight = QFont.Weight.Bold,
                 tracking: float = 12.0) -> QFont:
    """Uppercase, letter-spaced Roboto -- navigation items, section headers,
    the page title, button labels.

    This is how the app gets the reference design's condensed-caps character
    WITHOUT vendoring a second typeface: bold Roboto, set uppercase and
    opened up with tracking, reads as deliberate label chrome rather than as
    body text, which is the whole job those labels do.

    It has to be a QFont rather than a stylesheet rule because Qt Style
    Sheets implement neither `letter-spacing` nor `text-transform` -- there
    is no QSS property for either, so both live here or nowhere.
    Capitalization is applied at PAINT time (AllUppercase), not by upper()ing
    the string, so a widget's text() still returns what was set: screen
    readers, tests and tooltips see the real words, not shouted ones.

    `tracking` is in percent ADDED to normal spacing (12.0 -> 112% of
    normal), matching the .12em the reference uses on nav items.
    """
    font = QFont(UI_FONT_FAMILY)
    font.setFamilies([UI_FONT_FAMILY, UI_FONT_FALLBACK, "sans-serif"])
    font.setPointSize(point_size)
    font.setWeight(weight)
    font.setCapitalization(QFont.Capitalization.AllUppercase)
    font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100.0 + tracking)
    return font


def soft(color: str, *, on: str, amount: float = 0.14) -> str:
    """`color` at `amount` opacity over `on`, as an OPAQUE hex colour.

    The reference expresses these washes as rgba(...,.14). Precomputing the
    blend instead of emitting a translucent colour is deliberate: a
    semi-transparent QSS background composites against whatever Qt happens
    to have painted underneath, which for a widget inside a styled parent
    is not reliably the surface we think it is -- so the same rule can
    render differently depending on stacking. A solid colour cannot.
    """
    return _mix(QColor(color), QColor(on), 1.0 - amount)


def accent_soft(accent: AccentPalette | None = None,
                theme: ThemePalette | None = None) -> str:
    """The active accent as a soft wash over the active theme's card."""
    accent = accent or current_accent()
    theme = theme or current_theme()
    return soft(accent.base, on=theme.card)


def accent_ink(accent: AccentPalette | None = None) -> str:
    """The active accent as legible text -- see AccentPalette.ink()."""
    return (accent or current_accent()).ink()


def _stylesheet(accent: AccentPalette, t: ThemePalette) -> str:
    """The whole application stylesheet, for one (accent, theme) pair.

    Every rule below traces to the reference design in ref/. Where QSS has
    no equivalent for a CSS property the reference uses, the difference is
    noted at the rule rather than silently dropped:

      - `transition` does not exist in QSS. State changes are instant; the
        few that must animate (the sidebar collapse) use QPropertyAnimation.
      - `letter-spacing` and `text-transform` do not exist in QSS. Both live
        on the QFont instead -- see tracked_font().
      - `box-shadow` does not exist in QSS. Elevation is carried by a real
        lightness step between bg/card/fill, which holds up better on a
        dark palette anyway.
      - `::after` does not exist in QSS. The rule that fills the rest of a
        section header row is a real QFrame -- see Dashboard._section.
    """
    return f"""
* {{
    color: {t.ink};
    font-family: "{UI_FONT_FAMILY}", "{UI_FONT_FALLBACK}", sans-serif;
    font-size: 10pt;
}}

/* Plain containers paint NOTHING. Only the window itself and the named
surfaces below (#card, #panel, #sidebar, ...) have a background.

This is the opposite of the obvious rule, and it is the important one: a
generic `QWidget {{ background-color: <shell> }}` means every layout
container INSIDE a card repaints the shell colour on top of the card,
which showed up as grey blocks across the instance cards and as a fleet-log
panel with no panel behind it. Transparent by default, painted by name,
makes that impossible. */
QWidget {{
    background-color: transparent;
}}

QMainWindow, QDialog, QMenu {{
    background-color: {t.bg};
}}

QMenu {{
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    padding: 4px;
}}

QMenu::item:selected {{
    background-color: {accent_soft(accent, t)};
    color: {accent.ink()};
    border-radius: 6px;
}}

QLabel {{
    background: transparent;
}}

QLabel[secondary="true"] {{
    color: {t.ink_2};
}}

QLabel[tertiary="true"] {{
    color: {t.ink_3};
}}

/* No qproperty-linkColor here. QLabel has no `linkColor` property -- link
colour is a QPalette role (QPalette::Link), not a widget property -- so Qt
rejected the rule and warned "QLabel(...) does not have a property named
linkColor" once per label per polish. It never coloured anything; it only
produced noise, and once the diagnostic log existed (see crash_log.py) it
was that log's single largest source of it. Removed rather than ported to
the palette because nothing in this UI renders a link today. */

/* ---------------- surfaces ---------------- */
#card, #panel {{
    background-color: {t.card};
    border: 1px solid {t.border};
    border-radius: {RADIUS_LG}px;
}}

#page {{
    background-color: {t.bg};
}}

#pageTitle {{
    color: {t.ink};
}}

#sectionRule {{
    background-color: {t.border};
    border: none;
}}

QListWidget, QTableWidget {{
    background-color: {t.card};
    border: 1px solid {t.border};
    border-radius: {RADIUS_LG}px;
    gridline-color: {t.border};
}}

QListWidget::item, QTableWidget::item {{
    padding: 6px;
    border: none;
}}

QListWidget::item:selected, QTableWidget::item:selected {{
    background-color: {accent_soft(accent, t)};
    color: {accent.ink()};
}}

QHeaderView::section {{
    background-color: {t.card};
    color: {t.ink_3};
    border: none;
    border-bottom: 1px solid {t.border};
    padding: 8px 6px;
}}

QTableCornerButton::section {{
    background-color: {t.card};
    border: none;
}}

/* ---------------- buttons ---------------- */
QPushButton {{
    background-color: {t.card};
    border: 1px solid {t.border_2};
    border-radius: 12px;
    padding: 9px 15px;
    color: {t.ink_2};
}}

QPushButton:hover {{
    border-color: {t.ink_3};
    color: {t.ink};
}}

QPushButton:pressed {{
    background-color: {t.fill};
}}

QPushButton:disabled {{
    color: {t.ink_4};
    border-color: {t.border};
}}

QPushButton:focus, QSpinBox:focus, QComboBox:focus, QLineEdit:focus,
QListWidget:focus, QTableWidget:focus {{
    border: 2px solid {accent.base};
}}

/* The label is a fixed near-black, NOT white and NOT the active theme's
ink. The reference sets white here, which measures 2.40:1 on the orange
accent -- the worst pairing in the whole design, on the app's single most
important button. A dark label on the same fill measures 5.72-7.84 across
all five accents and both themes, so the fill stays exactly the
reference's and only the text changes. It cannot be current_theme().ink
either: that is near-white in the dark theme, which puts us straight back
where we started. */
#primaryButton {{
    background-color: {accent.base};
    color: {THEMES["dark"].bg};
    font-weight: 700;
    border: 1px solid {accent.base};
}}

#primaryButton:hover {{
    background-color: {accent.hover};
}}

#primaryButton:pressed {{
    background-color: {accent.pressed};
}}

#primaryButton:disabled {{
    background-color: {accent.disabled};
    color: {t.ink_4};
    border-color: {accent.disabled};
}}

/* The reference's .btn.ghost-accent: an accent-tinted secondary action. */
#ghostButton {{
    background-color: {accent_soft(accent, t)};
    border: 1px solid {soft(accent.base, on=t.card, amount=0.5)};
    color: {accent.ink()};
}}

#ghostButton:hover {{
    border-color: {accent.base};
}}

/* .btn.danger */
#dangerButton {{
    color: {t.offline_ink};
    border-color: {soft(t.offline, on=t.card, amount=0.45)};
}}

#dangerButton:hover {{
    border-color: {t.offline};
    color: {t.offline_ink};
}}

/* Small buttons inside a card (Cancel, Download, View full log). The
default 9px vertical padding plus 10pt text needs more room than these
short fixed-height buttons have, and a fixed height wins the argument --
so the descenders get clipped. Less padding, not a taller button: the
card is dense on purpose. */
#cardButton {{
    padding: 2px 10px;
    border-radius: {RADIUS_SM}px;
}}

/* ---------------- inputs ---------------- */
QLineEdit, QSpinBox, QComboBox {{
    background-color: {t.fill};
    border: 1px solid {t.border_2};
    border-radius: {RADIUS_SM}px;
    padding: 7px 10px;
    color: {t.ink};
    font-family: "{MONO_FONT_FAMILY}", "{MONO_FONT_FALLBACK}", monospace;
    selection-background-color: {accent.base};
    selection-color: #FFFFFF;
}}

QComboBox::drop-down {{
    border: none;
    width: 22px;
}}

QComboBox QAbstractItemView {{
    background-color: {t.card};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    selection-background-color: {accent_soft(accent, t)};
    selection-color: {accent.ink()};
}}

/* ---------------- progress ---------------- */
/* The reference's .track: a rounded, unlabelled bar on the fill colour. */
QProgressBar {{
    background-color: {t.fill};
    border: none;
    border-radius: 5px;
    height: 8px;
    text-align: center;
    color: {t.ink_2};
}}

QProgressBar::chunk {{
    background-color: {accent.base};
    border-radius: 5px;
}}

/* ---------------- scrollbars ---------------- */
/* Thin, and invisible until the pointer is over the area -- the
reference's `scrollbar-color: transparent` until :hover. */
QScrollBar:vertical {{
    background: transparent;
    border: none;
    width: 8px;
    margin: 0px;
}}

QScrollBar:horizontal {{
    background: transparent;
    border: none;
    height: 8px;
    margin: 0px;
}}

QScrollBar::handle {{
    background: {t.ink_4};
    border-radius: 4px;
    min-height: 28px;
    min-width: 28px;
}}

QScrollBar::handle:hover {{
    background: {t.ink_3};
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0px;
    width: 0px;
    border: none;
    background: none;
}}

QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
}}

QScrollArea {{
    background: transparent;
    border: none;
}}

/* ---------------- tooltips, dialogs ---------------- */
QToolTip {{
    background-color: {t.card};
    color: {t.ink};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    padding: 6px 9px;
}}

QMessageBox {{
    background-color: {t.card};
}}

/* ---------------- shell: title bar ---------------- */
#titleBar {{
    background-color: transparent;
}}

#titleBarTitle {{
    color: {t.ink_3};
}}

#windowButton {{
    background-color: transparent;
    border: none;
    border-radius: {RADIUS_SM}px;
    padding: 0px;
    color: {t.ink_2};
}}

#windowButton:hover {{
    background-color: {t.fill};
    color: {t.ink};
}}

#closeButton:hover {{
    background-color: {t.offline};
    color: #FFFFFF;
}}

/* ---------------- shell: sidebar, navigation ---------------- */
/* A floating rounded panel inset from the window edge, not a full-height
flush rail. Children are NOT clipped by a parent's border-radius in Qt, so
every nav item is inset far enough that the corner never has to clip. */
#sidebar {{
    background-color: {t.card};
    border: 1px solid {t.border};
    border-radius: {RADIUS_LG}px;
}}

/* Collapsed, the sidebar is a pill of circular icon buttons rather than a
narrow version of the same rounded rectangle -- the shape change is what
makes "collapsed" legible at a glance instead of just "narrower". */
#sidebar[collapsed="true"] {{
    border-radius: 34px;
}}

/* A custom QAbstractButton (ui/sidebar.py) rather than a QPushButton: the
count pill has to sit at the trailing edge with the label filling the gap,
which a QPushButton's single text slot cannot express. WA_StyledBackground
is what lets these rules paint it at all -- see NavButton.__init__. */
#navButton {{
    background-color: transparent;
    border: none;
    border-radius: 12px;
}}

#navButton:hover {{
    background-color: {t.fill};
}}

#navButton[active="true"] {{
    background-color: {accent_soft(accent, t)};
}}

#navButton[collapsed="true"] {{
    border-radius: 22px;
}}

#navPill {{
    background-color: {t.fill};
    color: {t.ink_2};
    border-radius: 9px;
    padding: 1px 7px;
}}

#navButton[active="true"] #navPill {{
    background-color: {t.card};
    color: {accent.ink()};
}}

#navPill[alert="true"] {{
    background-color: {t.offline_t};
    color: {t.offline_ink};
}}

#collapseButton {{
    background-color: transparent;
    border: 1px solid {t.border_2};
    border-radius: 12px;
    color: {t.ink_3};
}}

#collapseButton:hover {{
    border-color: {t.ink_3};
    color: {t.ink};
}}

#sidebarFooter {{
    color: {t.ink_3};
}}

/* ---------------- header chrome ---------------- */
#iconButton {{
    background-color: {t.card};
    border: 1px solid {t.border_2};
    border-radius: {RADIUS_MD}px;
    padding: 0px;
    color: {t.ink_2};
}}

#iconButton:hover {{
    border-color: {t.ink_3};
    color: {t.ink};
}}

#iconButton[active="true"] {{
    background-color: {accent_soft(accent, t)};
    border-color: {soft(accent.base, on=t.card, amount=0.5)};
    color: {accent.ink()};
}}

/* The bell's unread bubble. Bordered in the shell colour so it reads as
sitting ON the button rather than inside it. */
#notifCount {{
    background-color: {accent.base};
    color: #FFFFFF;
    border: 2px solid {t.bg};
    border-radius: 9px;
}}

/* Connection-health button: latency in mono, arcs tinted by verdict. */
#wifiButton {{
    background-color: {t.card};
    border: 1px solid {t.border_2};
    border-radius: {RADIUS_MD}px;
    padding: 0px 12px;
    color: {t.ink_2};
}}

#wifiButton:hover {{
    border-color: {t.ink_3};
}}

/* ---------------- badges ---------------- */
/* The reference's .badge: soft-tinted pill, a dot, and a WORD. The word is
not decoration -- status is never carried by colour alone anywhere in this
app (see the note on ThemePalette.warn and instance_card.status_for). */
#badge {{
    border-radius: 9px;
    padding: 2px 9px;
}}

#badge[tone="active"]    {{ background-color: {t.active_t};  color: {t.active_ink}; }}
#badge[tone="idle"]      {{ background-color: {t.fill};      color: {t.ink_3}; }}
#badge[tone="offline"]   {{ background-color: {t.offline_t}; color: {t.offline_ink}; }}
#badge[tone="paused"]    {{ background-color: {t.paused_t};  color: {t.paused_ink}; }}
#badge[tone="warn"]      {{ background-color: {t.warn_t};    color: {t.warn_ink}; }}
#badge[tone="accent"]    {{ background-color: {accent_soft(accent, t)}; color: {accent.ink()}; }}

/* Hardware/meta chips inside a card -- the reference's .hw-chip. */
#chip {{
    background-color: {t.fill};
    border: 1px solid {t.border};
    border-radius: 8px;
    padding: 3px 9px;
    color: {t.ink_2};
}}

#chip[live="true"] {{
    background-color: {accent_soft(accent, t)};
    border-color: transparent;
    color: {accent.ink()};
}}

/* ---------------- panels floated over the page ---------------- */
#floatPanel {{
    background-color: {t.card};
    border: 1px solid {t.border_2};
    border-radius: {RADIUS_LG}px;
}}

#toast {{
    background-color: {t.card};
    border: 1px solid {t.border_2};
    border-radius: {RADIUS_MD}px;
}}

/* The offline banner: bordered in the offline colour rather than filled,
so a persistent warning never shouts as loudly as a transient error. */
#offlineBanner {{
    background-color: {t.card};
    border: 1px solid {soft(t.offline, on=t.card, amount=0.4)};
    border-radius: {RADIUS_LG}px;
}}

#offlineBanner QLabel {{
    color: {t.offline_ink};
}}

/* ---------------- dropzone ---------------- */
#dropzone {{
    background-color: transparent;
    border: 2px dashed {t.border_2};
    border-radius: {RADIUS_LG}px;
    color: {t.ink_3};
}}

#dropzone[hot="true"] {{
    border-color: {accent.base};
    background-color: {accent_soft(accent, t)};
    color: {accent.ink()};
}}

/* ---------------- settings ---------------- */
/* The reference's .seg -- a segmented control, one button per option. */
#segment {{
    background-color: {t.fill};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    padding: 6px 14px;
    color: {t.ink_2};
}}

#segment[active="true"] {{
    background-color: {t.card};
    color: {accent.ink()};
    border-color: {soft(accent.base, on=t.card, amount=0.5)};
}}

#settingRow {{
    background-color: transparent;
    border-bottom: 1px solid {t.border};
}}

/* The reference's .toggle -- a pill switch. The knob is a child widget, so
only the track is styled here. */
#toggleTrack {{
    background-color: {t.fill};
    border: 1px solid {t.border_2};
    border-radius: 11px;
}}

#toggleTrack[on="true"] {{
    background-color: {accent.base};
    border-color: {accent.base};
}}

#toggleKnob {{
    background-color: {t.card};
    border-radius: 8px;
}}
"""


# Backward-compatible: the default stylesheet, computed eagerly. Nothing in
# this codebase imports it.
STYLESHEET = _stylesheet(ACCENTS[DEFAULT_ACCENT], THEMES[DEFAULT_THEME])


def apply(app: QApplication, accent: str = DEFAULT_ACCENT,
          theme: str = DEFAULT_THEME) -> None:
    """Apply the BlendFleet design system to the whole application, for the
    given accent and theme, registering the bundled fonts on first call.

    Re-appliable at runtime: calling this again with a different accent or
    theme re-derives the stylesheet and sets it again -- nothing here
    accumulates state across calls other than the one-time font
    registration. A name neither ACCENTS nor THEMES recognises falls back
    to the default rather than raising, via resolve_accent/resolve_theme.

    Called once, right after constructing QApplication and before any
    window or dialog is shown, a stylesheet set at the QApplication level
    cascades to every widget created afterwards, including dialogs
    (SetupDialog) opened later -- that is the mechanism that guarantees no
    dialog is ever left unstyled.

    Updates the module globals `_active_accent_name` and
    `_active_theme_name` (what current_accent()/current_theme() read) and
    emits `theme_signal.changed`, in that order, BEFORE returning -- so a
    connected slot that calls either during the signal handler already sees
    the new values, not the ones being replaced.
    """
    global _active_accent_name, _active_theme_name
    register_fonts()
    _active_accent_name = accent if accent in ACCENTS else DEFAULT_ACCENT
    _active_theme_name = theme if theme in THEMES else DEFAULT_THEME
    app.setStyle("Fusion")
    app.setFont(ui_font())
    app.setStyleSheet(_stylesheet(ACCENTS[_active_accent_name],
                                  THEMES[_active_theme_name]))
    theme_signal.changed.emit()


def apply_theme(app: QApplication) -> None:
    """Backward-compatible entry point: apply() with the defaults."""
    apply(app, DEFAULT_ACCENT, DEFAULT_THEME)


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
    # Added with the navigation sidebar: the collapse toggle. Rotated 180
    # degrees when collapsed rather than shipped as a second mirrored file.
    "chevron-left",
    # Added with the frameless title bar (ui/title_bar.py). "x" doubles as
    # the close glyph, so only three are new.
    "minimise", "maximise", "restore",
]
