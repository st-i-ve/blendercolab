"""The NAMES of the design choices, with no Qt attached.

Settings has to validate an accent, a theme and a typeface -- "is this a
name this build knows?" -- and those names lived in ui/theme.py, which
imports PySide6 to build the palettes themselves. That import made the
whole design system a dependency of the settings file, and through it of
anything that reads settings: including the headless sidecar
(blendfleet/rpc), which exists precisely so an Electron shell does not
have to ship Qt.

So the names live here, where nothing imports a widget toolkit, and
ui/theme.py builds its palettes for these names. tests/test_theme.py
asserts the two agree exactly -- a colour defined in one and not the
other would be selectable but unpaintable, or paintable but rejected on
save, and both are worse than either alone.
"""
from __future__ import annotations

# Five from the reference design, three darker ones of this app's own.
ACCENT_NAMES = ("orange", "blue", "green", "purple", "red",
                "dark-orange", "dark-red", "slate")
DEFAULT_ACCENT = "orange"

# Two palettes, and one instruction. "system" is not a palette: it means
# "whichever of the other two the operating system is using right now", and
# each shell resolves it for its own chrome (ui/theme.resolve_theme via
# QStyleHints, Electron via nativeTheme) while the page resolves it with
# prefers-color-scheme. Kept in this list because it is a value Settings
# must accept and save; PALETTE_THEME_NAMES below is the list of things
# that can actually be painted.
THEME_NAMES = ("light", "dark", "system")
PALETTE_THEME_NAMES = ("light", "dark")
DEFAULT_THEME = "light"

# key -> the family name as the font itself reports it, which is what
# both Qt's font database and the page's CSS have to ask for.
FONT_FAMILIES = {
    "heebo": "Heebo",
    "inter": "Inter",
    "arimo": "Arimo",
    "oswald": "Oswald",
}
DEFAULT_FONT = "heebo"
