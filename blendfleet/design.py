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

# WHICH MACHINE A RENDER ASKS KAGGLE FOR.
#
# Kaggle ships no enum for these -- kaggle_api_extended.py says so at the
# push site -- so the only source is the docstring on
# ApiSaveKernelRequest, which lists NvidiaTeslaT4, NvidiaTeslaP100 and
# Tpu1VmV38. Measured live (docs/machine-shape-findings.md): T4 yields TWO
# cards and Cycles genuinely splits a frame across both; P100 yields one
# faster card. Both are worth offering -- two T4s win on throughput, one
# P100 on a scene that needs its memory undivided.
#
# TPU IS DELIBERATELY ABSENT. Cycles cannot render on a TPU, so offering
# it would be offering a way to spend a session's quota producing nothing.
# An exact string matters: an invalid one is accepted at push time with no
# error and silently falls back to a single P100.
MACHINE_SHAPES = {
    "NvidiaTeslaT4": "T4 ×2",
    "NvidiaTeslaP100": "P100",
}
DEFAULT_MACHINE_SHAPE = "NvidiaTeslaT4"

# How long a render's session may run before Kaggle stops it, in minutes.
# 0 means "do not ask", which leaves Kaggle's own limit in force -- hours,
# during which a hung render spends quota nobody is watching. The cap is
# 12 hours because that is the longest session Kaggle grants; a larger
# number would be a request it ignores.
DEFAULT_SESSION_TIMEOUT_MINUTES = 0
MAX_SESSION_TIMEOUT_MINUTES = 12 * 60

# key -> the family name as the font itself reports it, which is what
# both Qt's font database and the page's CSS have to ask for.
FONT_FAMILIES = {
    "heebo": "Heebo",
    "inter": "Inter",
    "arimo": "Arimo",
    "oswald": "Oswald",
}
DEFAULT_FONT = "heebo"
