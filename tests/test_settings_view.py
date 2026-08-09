"""SettingsView: the accent picker.

Companion to tests/test_theme.py (the mechanism: current_accent(),
theme_signal) and tests/test_instance_card.py / test_dashboard.py (proof
that a live switch reaches actual rendered pixels in real consumers) --
this file only covers the dialog itself: five labelled swatches, live
apply, persistence, and that selection state is drawn correctly.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import blendfleet.platform_paths as pp
import blendfleet.ui.theme as theme
from blendfleet.settings import Settings
from blendfleet.ui.settings_view import SettingsView


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_active_accent():
    """SettingsView calls theme.apply() live -- see test_theme.py's own
    fixture of the same name for why that state must not leak into later
    test modules in the same pytest session.

    Only actually calls theme.apply() -- which restyles the WHOLE
    QApplication, every widget any earlier test left alive under the one
    shared QApplication included -- when a test genuinely changed the
    accent; see tests/test_dashboard.py's fixture of the same name for
    the measured cost of calling it unconditionally on every teardown."""
    original = theme._active_accent_name
    yield
    if theme._active_accent_name != original:
        theme._active_accent_name = original
        theme.apply(QApplication.instance(), original)


# Every SettingsView a test builds, torn down deterministically -- same
# reasoning as tests/test_setup_dialog.py's _LIVE_DIALOGS.
_LIVE_VIEWS: list = []


def make_view(settings: Settings) -> SettingsView:
    dlg = SettingsView(settings)
    _LIVE_VIEWS.append(dlg)
    return dlg


@pytest.fixture(autouse=True)
def close_views(qapp):
    yield
    while _LIVE_VIEWS:
        dlg = _LIVE_VIEWS.pop()
        dlg.reject()
        dlg.deleteLater()
    for _ in range(20):
        QApplication.processEvents()


# ---------------- Step 1: labelled swatches ----------------

def test_five_swatches_exist_one_per_accent(qapp):
    dlg = make_view(Settings())
    assert set(dlg._swatches) == {"orange", "green", "purple", "blue", "red"}


def test_every_swatch_is_labelled_with_its_name_not_colour_alone(qapp):
    """The rule the rest of the app follows (theme.WARNING's own
    docstring: ~8% of men cannot reliably tell red from green) applies to
    the picker itself -- a coloured square with no text would fail it."""
    dlg = make_view(Settings())
    for name, swatch in dlg._swatches.items():
        assert swatch.name_label.text() == name.capitalize()


def test_default_settings_pre_selects_the_orange_swatch(qapp):
    dlg = make_view(Settings())
    assert dlg._swatches["orange"].button.isChecked()
    for name in ("green", "purple", "blue", "red"):
        assert not dlg._swatches[name].button.isChecked()


def test_existing_accent_is_pre_selected(qapp):
    dlg = make_view(Settings(accent="blue"))
    assert dlg._swatches["blue"].button.isChecked()
    assert not dlg._swatches["orange"].button.isChecked()


def test_only_the_selected_swatch_shows_a_check_mark(qapp):
    dlg = make_view(Settings(accent="green"))
    assert not dlg._swatches["green"].check_label.pixmap().isNull()
    for name in ("orange", "purple", "blue", "red"):
        assert dlg._swatches[name].check_label.pixmap() is None \
            or dlg._swatches[name].check_label.pixmap().isNull()


# ---------------- Step 2: picking applies live and persists ----------------

def test_picking_a_swatch_updates_settings_and_saves(qapp):
    settings = Settings()
    dlg = make_view(settings)
    dlg._on_picked("purple")
    assert settings.accent == "purple"
    reloaded = Settings.load()
    assert reloaded.accent == "purple"


def test_picking_a_swatch_applies_the_accent_to_the_running_app_immediately(qapp):
    """No restart, no separate Apply/OK step -- theme.current_accent()
    must reflect the pick the moment _on_picked returns."""
    dlg = make_view(Settings())
    dlg._on_picked("red")
    assert theme.current_accent_name() == "red"
    assert theme.current_accent().base in qapp.styleSheet()


def test_picking_a_swatch_moves_the_selection_marker(qapp):
    dlg = make_view(Settings(accent="orange"))
    dlg._on_picked("blue")
    assert dlg._swatches["blue"].button.isChecked()
    assert not dlg._swatches["orange"].button.isChecked()
    assert not dlg._swatches["blue"].check_label.pixmap().isNull()


def test_picking_an_already_selected_swatch_is_a_no_op_not_an_error(qapp):
    settings = Settings(accent="green")
    dlg = make_view(settings)
    dlg._on_picked("green")
    assert settings.accent == "green"
    assert dlg._swatches["green"].button.isChecked()
