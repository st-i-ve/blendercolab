"""A third theme value that is not a colour: follow the desktop.

"system" is an instruction rather than a palette, and that distinction is
the whole design. Three places resolve it, independently and to the same
answer:

  - ui/theme.resolve_theme, through QStyleHints, for the window's chrome
  - the page, through prefers-color-scheme, for the dashboard itself
  - electron/main.js, through nativeTheme, for the parts Chromium draws

The page resolving it ITSELF is what keeps blendfleet/web identical for
both shells: neither shell has to tell it which colours to use.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from blendfleet import design
from blendfleet.settings import Settings
from blendfleet.ui.theme import THEMES, resolve_theme, system_theme_name

WEB = Path(__file__).resolve().parents[1] / "blendfleet" / "web"
ELECTRON = Path(__file__).resolve().parents[1] / "electron"


# ---- the name -------------------------------------------------------

def test_system_is_selectable_and_saveable():
    """Settings validates against design.THEME_NAMES; a value the settings
    page offers and Settings rejects would be a control that silently does
    nothing."""
    assert "system" in design.THEME_NAMES

    settings = Settings()
    settings.theme = "system"
    settings.__post_init__()        # the validation Settings runs on save

    assert settings.theme == "system", "the preference was rejected or reset"


def test_system_is_not_itself_a_palette():
    """Palettes are the paintable names. If "system" became one, something
    would eventually paint a window in it."""
    assert "system" not in design.PALETTE_THEME_NAMES
    assert "system" not in THEMES


# ---- resolving it ---------------------------------------------------

def test_it_resolves_to_a_real_palette():
    assert resolve_theme("system") in THEMES.values()


def test_it_resolves_to_whichever_the_desktop_is_using(monkeypatch):
    """Both directions, since a follow-the-system theme that only ever
    resolved one way would look exactly like a working one on the machine
    it was written on."""
    import blendfleet.ui.theme as theme_mod

    monkeypatch.setattr(theme_mod, "system_theme_name", lambda: "dark")
    assert resolve_theme("system") is THEMES["dark"]

    monkeypatch.setattr(theme_mod, "system_theme_name", lambda: "light")
    assert resolve_theme("system") is THEMES["light"]


def test_a_platform_that_cannot_say_is_not_read_as_a_preference():
    """Qt reports ColorScheme.Unknown where the OS has no answer. Treating
    that as dark would give half the world a dark window it never asked
    for."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    class _Hints:
        def colorScheme(self):
            return Qt.ColorScheme.Unknown

    original = QGuiApplication.styleHints
    try:
        QGuiApplication.styleHints = staticmethod(lambda: _Hints())
        assert system_theme_name() == design.DEFAULT_THEME
    finally:
        QGuiApplication.styleHints = original


# ---- the page resolves it too, on its own ---------------------------

def _app_js() -> str:
    return (WEB / "app.js").read_text(encoding="utf-8")


def test_the_page_resolves_it_with_prefers_color_scheme():
    """Not by being told: a shell-specific message here would be the page
    knowing which shell it is in, which is the one thing blendfleet/web is
    not allowed to know."""
    source = _app_js()
    assert "prefers-color-scheme: dark" in source
    resolver = re.search(r"function resolvedTheme\(\)(.*?)\n\}", source, re.S)
    assert resolver, "nothing resolves the preference into a palette name"
    assert "'system'" in resolver.group(1)


def test_the_resolved_name_is_what_reaches_the_dom_not_the_preference():
    """`data-theme="system"` would match no CSS at all -- every token in
    app.css hangs off light or dark."""
    source = _app_js()
    assert "dataset.theme = resolvedTheme()" in source


def test_the_page_keeps_following_while_the_preference_says_to():
    """Someone whose desktop goes dark at sunset expects the app to go with
    it, without reopening it -- and expects an EXPLICIT choice to stick."""
    source = _app_js()
    listener = re.search(r"darkOutside\.addEventListener\('change',(.*?)\n\}\);",
                         source, re.S)
    assert listener, "an OS theme change is not noticed at all"
    assert "prefs.theme !== 'system'" in listener.group(1), (
        "an explicit light/dark choice would be overridden by the OS")
    assert "theme-snap" in listener.group(1), (
        "the switch has to be snapped, or the ground fades under cards that "
        "have already changed")


def test_the_settings_page_offers_it():
    markup = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'data-v="system"' in markup


# ---- and the Electron shell -----------------------------------------

def test_everything_main_js_uses_from_electron_is_actually_imported():
    """The limit of a text assertion, learned the hard way.

    The test below asserted `nativeTheme.themeSource` appears in main.js.
    It passed while the code was broken, because `nativeTheme` was never
    added to the require at the top -- the app started and threw
    `ReferenceError: nativeTheme is not defined` into an unhandled promise
    rejection, which only a real launch revealed.

    So this checks the import list against what the file actually uses. It
    is still text, but it is the half that text CAN check.
    """
    source = (ELECTRON / "main.js").read_text(encoding="utf-8")
    imported = set(re.findall(r"[\w]+", re.search(
        r"require\('electron'\)", source) and re.search(
        r"const \{(.*?)\} = require\('electron'\)", source, re.S).group(1)))

    for name in ("nativeTheme", "powerSaveBlocker", "powerMonitor",
                 "Notification", "nativeImage"):
        assert name in imported, (
            f"main.js uses {name} without importing it -- the app throws the "
            "moment that line runs")
        assert name in source.split("require('electron')", 1)[1], (
            f"{name} is imported but never used")


def test_the_electron_shell_puts_the_preference_where_chromium_reads_it():
    source = (ELECTRON / "main.js").read_text(encoding="utf-8")
    setter = re.search(r"function matchShellTheme\((.*?)\n\}", source, re.S)
    assert setter, "the shell never tells Chromium which theme is in use"
    assert "nativeTheme.themeSource" in setter.group(1)
    # 'system' is the fallback for anything that is not an explicit choice,
    # which is also what makes prefers-color-scheme follow the OS.
    assert "'system'" in setter.group(1)


@pytest.mark.parametrize("hook", ["settingsChanged", "preferences"])
def test_the_shell_learns_the_theme_at_startup_and_on_change(hook):
    """settingsChanged alone would leave a saved dark preference painting
    light native menus until the user touched a setting."""
    source = (ELECTRON / "main.js").read_text(encoding="utf-8")
    assert hook in source
