"""The new brand mark: window icon, in-app glyph, and the retired asset set.

Companion to tests/test_theme.py's icon() coverage -- this exercises the
raster equivalent (brand_icon(), backed by assets/logo/mark-white.png) and
guards against the old assets/blendfleet_icon*.png / blendfleet.ico /
make_icon.py generator quietly reappearing.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

import blendfleet.__main__ as main_mod
from blendfleet.ui.theme import ACCENTS, LOGO_DIR, brand_icon

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ---------------- window icon ----------------

def test_icon_path_resolves_under_the_new_logo_set():
    path = main_mod._icon_path()
    assert path is not None
    assert path.exists()
    assert path == ASSETS_DIR / "logo" / "app-icon-256.png"


def test_window_icon_loads_non_null(qapp):
    path = main_mod._icon_path()
    icon = QIcon(str(path))
    assert not icon.isNull()
    sizes = icon.availableSizes()
    assert sizes
    assert not icon.pixmap(sizes[0]).isNull()


# ---------------- in-app glyph ----------------

def test_brand_icon_loads_non_null(qapp):
    ic = brand_icon("#F5792A", 28)
    assert not ic.isNull()
    pixmap = ic.pixmap(28, 28)
    assert not pixmap.isNull()


def test_brand_icon_tints_to_the_requested_colour(qapp):
    orange = brand_icon("#F5792A", 28)
    blue = brand_icon("#5FB0F0", 28)
    assert orange.pixmap(28, 28).toImage() != blue.pixmap(28, 28).toImage()


@pytest.mark.parametrize("name", list(ACCENTS))
def test_brand_icon_renders_for_every_accent(qapp, name):
    ic = brand_icon(ACCENTS[name].base, 28)
    assert not ic.isNull()


def test_brand_icon_missing_mark_raises(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("blendfleet.ui.theme.LOGO_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        brand_icon("#F5792A", 28)


# ---------------- the old icon set is gone ----------------

def test_old_icon_generator_and_outputs_are_gone():
    old_names = [
        "blendfleet_icon.png", "blendfleet_icon_32.png",
        "blendfleet_icon_64.png", "blendfleet_icon_128.png",
        "blendfleet_icon_256.png", "blendfleet_icon_512.png",
        "blendfleet.ico", "make_icon.py",
    ]
    for name in old_names:
        assert not (ASSETS_DIR / name).exists(), (
            f"superseded asset {name!r} is still present under {ASSETS_DIR}")


def test_new_logo_set_is_the_only_icon_source():
    assert LOGO_DIR.exists()
    for name in ("mark-white.png", "app-icon-256.png", "blendfleet.ico"):
        assert (LOGO_DIR / name).exists()


def test_no_remaining_source_reference_to_the_old_asset_names():
    """The spec and __main__ must both have moved on -- a stale reference
    is exactly how a future change ends up wiring the wrong icon set back
    in (see the task brief)."""
    repo_root = ASSETS_DIR.parent
    offenders = []
    for path in (repo_root / "blendfleet" / "__main__.py",
                 repo_root / "packaging" / "blendfleet.spec"):
        text = path.read_text(encoding="utf-8")
        if "blendfleet_icon" in text or "assets/blendfleet.ico" in text:
            offenders.append(path)
    assert not offenders, offenders
