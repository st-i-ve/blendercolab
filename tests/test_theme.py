"""Accent-parameterised theme: ACCENTS, apply(), fonts, icons.

Contrast is computed with the WCAG relative-luminance formula below, not
eyeballed -- see https://www.w3.org/TR/WCAG21/#dfn-relative-luminance. This
is a second, independent implementation from whatever theme.py uses
internally, so a bug shared between the two would not hide behind these
tests agreeing with themselves.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import blendfleet.ui.theme as theme
from blendfleet.ui.theme import (ACCENTS, DEFAULT_ACCENT, ICON_NAMES, WARNING,
                                  apply, icon, resolve_accent)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ---------------- WCAG contrast (independent implementation) ----------------

def _srgb_to_linear(channel: float) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = _srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(hex_a: str, hex_b: str) -> float:
    la, lb = _relative_luminance(hex_a), _relative_luminance(hex_b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# ---------------- Step 1: ACCENTS shape / fallback ----------------

def test_five_named_accents_exist():
    assert set(ACCENTS) == {"orange", "green", "purple", "blue", "red"}


def test_default_accent_is_orange():
    assert DEFAULT_ACCENT == "orange"
    assert DEFAULT_ACCENT in ACCENTS


def test_every_accent_defines_the_full_token_set():
    """No KeyError at paint time: every accent has all four tokens, and
    they are real, distinct hex colours rather than a placeholder that
    silently aliases the base."""
    for name, palette in ACCENTS.items():
        for field in ("base", "hover", "pressed", "disabled"):
            value = getattr(palette, field)
            assert isinstance(value, str) and value.startswith("#") and len(value) == 7, (
                f"{name}.{field} is not a hex colour: {value!r}")
        assert palette.hover != palette.base, name
        assert palette.pressed != palette.base, name
        assert palette.disabled != palette.base, name


def test_resolve_accent_returns_the_named_palette():
    assert resolve_accent("blue") is ACCENTS["blue"]


def test_unknown_accent_name_falls_back_to_default_rather_than_raising():
    """A hand-edited config or a file from a future version with an accent
    name this build doesn't know must not brick the app on startup."""
    assert resolve_accent("ultraviolet") == ACCENTS[DEFAULT_ACCENT]


def test_apply_with_unknown_accent_does_not_raise(qapp):
    apply(qapp, "not-a-real-accent")
    assert qapp.styleSheet()  # applied something rather than raising


# ---------------- Step 4: contrast ----------------

@pytest.mark.parametrize("name", ["orange", "green", "purple", "blue", "red"])
def test_accent_meets_contrast_against_shell_background(name):
    ratio = _contrast_ratio(ACCENTS[name].base, theme.BG_SHELL)
    assert ratio >= 4.5, f"{name} contrast against shell is only {ratio:.2f}:1"


# ---------------- Step 5: amber survives every accent ----------------

def test_warning_is_amber_and_not_accent_derived():
    assert WARNING == "#E8B33A"
    all_accent_values = {
        v for p in ACCENTS.values() for v in (p.base, p.hover, p.pressed, p.disabled)
    }
    assert WARNING not in all_accent_values


def test_warning_survives_the_red_accent(qapp):
    """A red accent is the one case where warning-amber could plausibly get
    swallowed into "just another shade of red" -- assert explicitly."""
    apply(qapp, "red")
    assert theme.WARNING == "#E8B33A"
    ratio_vs_shell = _contrast_ratio(theme.WARNING, theme.BG_SHELL)
    assert ratio_vs_shell >= 4.5
    # amber must not collapse onto the red accent's own tokens
    red = ACCENTS["red"]
    assert theme.WARNING not in (red.base, red.hover, red.pressed, red.disabled)


# ---------------- Step 6: fonts ----------------

def test_bundled_fonts_register_as_roboto_and_roboto_mono(qapp):
    """A silent fallback to a system face is the failure mode: if the TTFs
    don't register, Qt just picks something close-enough-looking instead
    of raising, so this must check the *returned* family name, not merely
    that addApplicationFont didn't return -1."""
    families = theme.register_fonts()
    assert "Roboto" in families
    assert "Roboto Mono" in families


def test_register_fonts_is_idempotent(qapp):
    first = set(theme.register_fonts())
    second = set(theme.register_fonts())
    assert first == second


# ---------------- Step 6: icons ----------------

def test_icon_names_matches_the_eighteen_bundled_svgs():
    on_disk = {p.stem for p in theme.ICONS_DIR.glob("*.svg")}
    assert on_disk == set(ICON_NAMES)
    assert len(ICON_NAMES) == 18


@pytest.mark.parametrize("name", ICON_NAMES)
def test_every_bundled_icon_loads_non_null(qapp, name):
    ic = icon(name, "#F5792A")
    assert not ic.isNull()
    sizes = ic.availableSizes()
    assert sizes, f"icon {name!r} has no rendered sizes"
    pixmap = ic.pixmap(sizes[0])
    assert not pixmap.isNull()


def test_unknown_icon_name_raises_rather_than_rendering_blank(qapp):
    with pytest.raises(FileNotFoundError):
        icon("not-a-real-icon", "#F5792A")


def test_icon_tints_to_the_requested_colour(qapp):
    orange = icon("check", "#F5792A")
    blue = icon("check", "#5FB0F0")
    orange_img = orange.pixmap(24, 24).toImage()
    blue_img = blue.pixmap(24, 24).toImage()
    assert orange_img != blue_img
