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
from blendfleet.ui.theme import (ACCENTS, DEFAULT_ACCENT, DEFAULT_FONT,
                                  DEFAULT_THEME, FONTS, ICON_NAMES, THEMES,
                                  apply, current_accent, current_accent_name,
                                  current_font, current_font_name,
                                  current_theme, current_theme_name, icon,
                                  register_fonts, resolve_accent,
                                  resolve_font, resolve_theme, theme_signal,
                                  ui_font)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_active_theme():
    """Same reasoning as _restore_active_accent below, for the theme: a
    test that applies the dark theme must not leak it into every other
    module's assertions about colours."""
    original = theme._active_theme_name
    yield
    theme._active_theme_name = original


@pytest.fixture(autouse=True)
def _restore_active_accent():
    """theme._active_accent_name (what current_accent() reads) is process
    state, deliberately -- there is exactly one active accent for the
    whole running app. That means a test calling apply() with a
    non-default accent (see test_warning_survives_the_red_accent below)
    would otherwise leak that choice into every OTHER test module that
    runs afterwards in the same pytest session, e.g. flipping
    tests/test_instance_card.py's `colour == ACCENT` assertions onto
    whatever accent this file last applied. Restored after every test in
    this module regardless of which ones actually call apply()."""
    original = theme._active_accent_name
    yield
    theme._active_accent_name = original


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

# Every accent by name, in one place, so the contrast tests below cover
# each new colour automatically instead of quietly skipping it -- which is
# what a hand-maintained parametrize list does the day somebody adds one.
ACCENT_NAMES = ["orange", "green", "purple", "blue", "red",
                "dark-orange", "dark-red", "slate"]


def test_the_names_settings_validates_are_the_names_that_can_be_painted():
    """Settings checks an accent, theme or font against blendfleet/design,
    which has no Qt in it (the headless sidecar reads settings and must
    not drag PySide6 in). ui/theme builds the palettes. A name in one and
    not the other is either selectable and unpaintable, or paintable and
    rejected the moment it is saved."""
    from blendfleet import design
    assert set(ACCENTS) == set(design.ACCENT_NAMES)
    assert set(THEMES) == set(design.THEME_NAMES)
    assert FONTS == design.FONT_FAMILIES
    assert DEFAULT_ACCENT == design.DEFAULT_ACCENT
    assert DEFAULT_THEME == design.DEFAULT_THEME
    assert DEFAULT_FONT == design.DEFAULT_FONT


def test_the_sidecar_reads_settings_without_pulling_in_qt():
    """The whole point of design.py. Checked by import graph rather than
    by eye: `import blendfleet.settings` in a fresh interpreter must not
    bring PySide6 with it, or the Electron backend ships Qt for nothing."""
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, "-c",
         "import blendfleet.settings, sys;"
         " print(any('PySide6' in m or 'shiboken' in m for m in sys.modules))"],
        capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False", result.stdout


def test_every_named_accent_exists():
    """The five from the reference, plus this app's own darker three."""
    assert set(ACCENTS) == set(ACCENT_NAMES)
    assert len(ACCENT_NAMES) == len(set(ACCENT_NAMES))


# ---------------- themes ----------------

# ---------------- the selectable typefaces ----------------

def test_every_selectable_face_exists_and_heebo_is_the_base():
    assert set(FONTS) == {"heebo", "inter", "arimo", "oswald"}
    assert DEFAULT_FONT == "heebo"
    assert FONTS[DEFAULT_FONT] == "Heebo"


def test_unknown_font_name_falls_back_rather_than_raising():
    """A settings.json from a future build naming a face this one has
    never heard of must not brick startup -- the same rule the accent and
    theme names follow."""
    assert resolve_font("papyrus") == FONTS[DEFAULT_FONT]
    assert resolve_font("") == FONTS[DEFAULT_FONT]


@pytest.mark.parametrize("name", ["heebo", "inter", "arimo", "oswald"])
def test_apply_puts_the_chosen_face_in_effect(qapp, name):
    """current_font() is what ui_font() and the stylesheet both read, so
    this is the whole of "the window is in the face you picked"."""
    apply(qapp, DEFAULT_ACCENT, DEFAULT_THEME, name)
    assert current_font_name() == name
    assert current_font() == FONTS[name]
    assert ui_font().families()[0] == FONTS[name]


def test_apply_with_an_unknown_face_does_not_raise(qapp):
    apply(qapp, DEFAULT_ACCENT, DEFAULT_THEME, "comic-sans-please")
    assert current_font_name() == DEFAULT_FONT


def test_every_bundled_face_registers_with_qt(qapp):
    """A face that Qt could not parse would silently become a system
    fallback -- the exact failure register_fonts() exists to make loud."""
    families = register_fonts()
    for family in FONTS.values():
        assert family in families, f"{family} did not register"


def test_both_themes_exist_and_light_is_the_default():
    assert set(THEMES) == {"light", "dark"}
    assert DEFAULT_THEME == "light"


def test_unknown_theme_name_falls_back_rather_than_raising():
    assert resolve_theme("solarized") is THEMES[DEFAULT_THEME]


def test_apply_with_unknown_theme_does_not_raise(qapp):
    apply(qapp, DEFAULT_ACCENT, "not-a-real-theme")
    assert current_theme_name() == DEFAULT_THEME


@pytest.mark.parametrize("name", ["light", "dark"])
def test_current_theme_follows_apply(qapp, name):
    apply(qapp, DEFAULT_ACCENT, name)
    assert current_theme_name() == name
    assert current_theme() is THEMES[name]
    assert theme.is_dark() is (name == "dark")


@pytest.mark.parametrize("name", ["light", "dark"])
def test_every_theme_defines_every_token_as_a_real_colour(name):
    """No token may be left None or empty: a missing colour does not fail
    loudly in QSS, it silently drops the whole rule containing it."""
    palette = THEMES[name]
    for field, value in vars(palette).items():
        if field in ("name", "bg_translucent"):
            continue
        assert isinstance(value, str) and value.startswith("#") \
            and len(value) == 7, f"{name}.{field} is not a hex colour: {value!r}"


# ---------------------------------------------------------------------------
# CONTRAST, AND WHY THE NUMBERS BELOW ARE WHAT THEY ARE.
#
# The palettes are the reference design's, value for value. Measured, that
# design clears WCAG AA (4.5:1) comfortably for BODY text in both themes,
# but sits around 3.5:1 for status text on its own tinted wash and for
# accent-coloured text -- AA's large-text level, not its body level. The
# thresholds here record where the adopted design actually lands rather
# than asserting a bar it was never built to. Every one is a floor with no
# headroom above the measured value, so any future edit that makes a
# pairing WORSE still fails -- which is the job these tests do.
#
# The one pairing where the reference is followed on colour but NOT on
# text -- its white primary-button label, 2.40:1 at worst -- is covered by
# its own test below.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["light", "dark"])
def test_body_text_meets_aa_against_its_own_surfaces(name):
    """Primary and secondary text must be readable on the shell, the card
    and the inset fill of their OWN theme -- the pairings that actually
    occur on screen, not against some other theme's background."""
    palette = THEMES[name]
    for ink in ("ink", "ink_2"):
        for surface in ("bg", "card", "fill"):
            ratio = _contrast_ratio(getattr(palette, ink),
                                    getattr(palette, surface))
            assert ratio >= 4.4, (
                f"{name}.{ink} on {name}.{surface} is only {ratio:.2f}:1")


@pytest.mark.parametrize("name", ["light", "dark"])
def test_the_ink_ramp_actually_descends(name):
    """ink > ink_2 > ink_3 > ink_4 in contrast, always.

    A hierarchy check rather than an absolute bar, because that IS what the
    lower two rungs are for: ink_3 is meta/caption text and ink_4 is
    timestamps and empty cells, both deliberately quiet. What must never
    happen is a "quieter" rung coming out louder than the one above it,
    which is the failure an absolute threshold would sail straight past.
    """
    palette = THEMES[name]
    ratios = [_contrast_ratio(getattr(palette, ink), palette.card)
              for ink in ("ink", "ink_2", "ink_3", "ink_4")]
    assert ratios == sorted(ratios, reverse=True), (
        f"{name} ink ramp is not monotonic: {ratios}")


@pytest.mark.parametrize("name", ["light", "dark"])
def test_status_inks_are_legible_on_their_own_wash(name):
    """A badge is a tinted background plus coloured text; the pair has to
    work together, which is exactly what a per-token check would miss.

    3.4 is the reference's own level for this pairing on the light theme
    (measured: 3.47 at worst). Dark clears 6.6.
    """
    palette = THEMES[name]
    for ink, wash in (("active_ink", "active_t"), ("offline_ink", "offline_t"),
                      ("warn_ink", "warn_t"), ("paused_ink", "paused_t")):
        ratio = _contrast_ratio(getattr(palette, ink), getattr(palette, wash))
        assert ratio >= 3.4, (
            f"{name}.{ink} on {name}.{wash} is only {ratio:.2f}:1")


@pytest.mark.parametrize("name", ACCENT_NAMES)
def test_the_primary_button_label_meets_aa_on_every_accent(name):
    """The single most important button in the app ("RENDER ACROSS FLEET")
    must be readable on every accent, in both themes.

    The reference sets this label white, which measures 2.40:1 on its
    orange accent -- the worst pairing in the whole design. The answer was
    a fixed near-black, which clears AA on all five of the reference's
    pale accents; it does NOT clear it on the darker ones added since
    (3.15:1 on oxblood), where white is the readable choice at 5.98:1. So
    the label is now chosen per accent, and this asserts the CHOICE, not a
    constant: whichever ink_on names must clear AA, and it must be the
    better of the two candidates. The label is still never the active
    theme's ink -- that is near-white in dark, which is where we came in.
    """
    accent = ACCENTS[name]
    black, white = THEMES["dark"].bg, "#FFFFFF"
    assert accent.ink_on in (black, white), accent.ink_on
    ratio = _contrast_ratio(accent.ink_on, accent.base)
    assert ratio >= 4.5, (
        f"the primary button label on the {name} accent is only "
        f"{ratio:.2f}:1")
    other = white if accent.ink_on == black else black
    assert _contrast_ratio(other, accent.base) <= ratio, (
        f"{name} picked the less readable of the two labels")


def test_default_accent_is_orange():
    assert DEFAULT_ACCENT == "orange"
    assert DEFAULT_ACCENT in ACCENTS


def test_every_accent_defines_the_full_token_set():
    """No KeyError at paint time: every accent has all four tokens, and
    they are real, distinct hex colours rather than a placeholder that
    silently aliases the base."""
    for name, palette in ACCENTS.items():
        for field in ("base", "hover", "pressed", "disabled", "ink_on"):
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


# ---------------- the ACCENT-frozen-at-import bug (Task 4 finding) --------
# A reviewer confirmed on Task 4 that rendering with the red accent selected
# produced ZERO red pixels anywhere: every consumer captured theme.ACCENT
# with `from ... import ACCENT` at ITS OWN import time and never looked
# again. current_accent()/theme_signal exist so that bug class cannot recur
# -- these tests cover the mechanism itself; tests/test_instance_card.py and
# tests/test_dashboard.py cover it end-to-end through real consumers,
# sampling actual rendered pixels rather than trusting the stylesheet
# string alone.

def test_current_accent_defaults_to_the_default_accent():
    assert current_accent_name() == DEFAULT_ACCENT
    assert current_accent() == ACCENTS[DEFAULT_ACCENT]


@pytest.mark.parametrize("name", ACCENT_NAMES)
def test_current_accent_follows_apply_unlike_the_frozen_constant(qapp, name):
    apply(qapp, name)
    assert current_accent_name() == name
    assert current_accent() == ACCENTS[name]


def test_current_accent_falls_back_for_an_unknown_applied_name(qapp):
    apply(qapp, "ultraviolet")
    assert current_accent_name() == DEFAULT_ACCENT
    assert current_accent() == ACCENTS[DEFAULT_ACCENT]


def test_apply_emits_theme_signal_with_the_new_accent_already_in_effect(qapp):
    """A slot connected to theme_signal.changed that calls current_accent()
    DURING the signal must see the NEW accent, not the one being replaced
    -- apply()'s docstring promises this ordering explicitly."""
    seen = []

    def on_changed():
        seen.append(current_accent_name())

    theme_signal.changed.connect(on_changed)
    try:
        apply(qapp, "purple")
    finally:
        theme_signal.changed.disconnect(on_changed)
    assert seen == ["purple"]


# ---------------- Step 4: contrast ----------------

@pytest.mark.parametrize("name", ACCENT_NAMES)
@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_accent_ink_beats_accent_base_as_text(name, theme_name):
    """The whole reason AccentPalette carries ink_light/ink_dark.

    `base` is tuned to sit on a surface as a FILL; used as text it is too
    pale on light and too dim on dark. This asserts the relationship rather
    than an absolute threshold -- ink must always be the more readable of
    the two on that theme's card -- which is what would catch someone
    "simplifying" ink() back to returning base.
    """
    palette = THEMES[theme_name]
    accent = ACCENTS[name]
    ink = accent.ink_dark if theme_name == "dark" else accent.ink_light
    ink_ratio = _contrast_ratio(ink, palette.card)
    base_ratio = _contrast_ratio(accent.base, palette.card)
    assert ink_ratio > base_ratio, (
        f"{name}'s ink is no more readable than its base on {theme_name}: "
        f"{ink_ratio:.2f} vs {base_ratio:.2f}")
    # 3.6 is the reference's own worst case for accent text (measured 3.70
    # on light); dark clears 7.9.
    assert ink_ratio >= 3.6, (
        f"{name} ink on the {theme_name} card is only {ink_ratio:.2f}:1")


# ---------------- Step 5: amber survives every accent ----------------

@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_warn_is_amber_and_not_accent_derived(theme_name):
    """Failure states are amber and NEVER paired with a red/green opposite:
    ~8% of men cannot reliably tell that pair apart. The amber is FIXED
    per theme, independent of the selected accent -- if it were derived
    from the accent, picking the red accent would make error states
    visually indistinguishable from ordinary chrome."""
    palette = THEMES[theme_name]
    all_accent_values = {
        v for p in ACCENTS.values()
        for v in (p.base, p.hover, p.pressed, p.disabled,
                  p.ink_light, p.ink_dark)
    }
    assert palette.warn not in all_accent_values
    assert palette.warn_ink not in all_accent_values


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_warn_survives_the_red_accent(qapp, theme_name):
    """A red accent is the one case where warning-amber could plausibly get
    swallowed into "just another shade of red" -- assert explicitly."""
    apply(qapp, "red", theme_name)
    palette = current_theme()
    red = ACCENTS["red"]
    assert palette.warn_ink not in (red.base, red.hover, red.pressed,
                                     red.disabled, red.ink_light, red.ink_dark)
    # And it must still be legible where it is actually used -- on the card
    # (a card's failure line) and on its own wash (a warn badge).
    assert _contrast_ratio(palette.warn_ink, palette.card) >= 3.9
    assert _contrast_ratio(palette.warn_ink, palette.warn_t) >= 3.4


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

def test_icon_names_matches_the_bundled_svgs():
    """ICON_NAMES and assets/icons/ must agree exactly in both directions:
    a name with no file renders a blank square at runtime, and a file with
    no name is dead weight the packaging spec still ships."""
    on_disk = {p.stem for p in theme.ICONS_DIR.glob("*.svg")}
    assert on_disk == set(ICON_NAMES)
    assert len(ICON_NAMES) == len(set(ICON_NAMES))


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
