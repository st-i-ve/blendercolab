"""Every asset the app can resolve at runtime must also be declared in
packaging/blendfleet.spec's `datas`.

PyInstaller only extracts files named in `datas` into sys._MEIPASS at
runtime; a file the source tree has but the spec omits raises
FileNotFoundError the first time the frozen build tries to read it (e.g.
theme.brand_icon() reading assets/logo/mark-white.png, or icon() reading
assets/icons/<name>.svg) -- not at build time, and not in any test that
only runs against the source tree. This test closes that gap by cross-
checking the spec's declared datas against the concrete set of paths
theme.py/__main__.py can resolve, so a future addition (a new icon, a new
font weight, a second mark variant) that forgets the spec fails here
instead of only surfacing in a packaged build.
"""
from __future__ import annotations

from pathlib import Path

import blendfleet.__main__ as main_mod
from blendfleet.ui import theme

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "packaging" / "blendfleet.spec"
ASSETS_DIR = REPO_ROOT / "assets"


def _spec_datas() -> list[tuple[str, str]]:
    """The `datas` list the spec builds, without invoking PyInstaller's
    Analysis/PYZ/EXE (which need a full build context this test doesn't
    have) -- only the data-collection prelude above `a = Analysis(...)`
    runs.
    """
    text = SPEC_PATH.read_text(encoding="utf-8")
    prelude, marker, _ = text.partition("\na = Analysis(")
    assert marker, (
        "packaging/blendfleet.spec no longer has an 'a = Analysis(' line -- "
        "update this test's split point")
    namespace = {"__file__": str(SPEC_PATH), "SPECPATH": str(SPEC_PATH.parent)}
    exec(compile(prelude, str(SPEC_PATH), "exec"), namespace)
    datas = namespace.get("datas")
    assert isinstance(datas, list) and datas, (
        "packaging/blendfleet.spec's datas evaluated empty -- test harness "
        "problem, not a real empty bundle")
    return datas


def _runtime_resolvable_assets() -> set[Path]:
    """Every concrete file path the running app can resolve from source --
    the same files a frozen build needs under sys._MEIPASS."""
    assets = {
        ASSETS_DIR / "logo" / "app-icon-256.png",  # __main__._icon_path()
        theme.LOGO_DIR / "mark-white.png",          # theme.brand_icon()
    }
    assets.update(theme.ICONS_DIR / f"{name}.svg" for name in theme.ICON_NAMES)
    assets.update(theme.FONTS_DIR / filename for filename in theme.FONT_FILES)
    return assets


def test_runtime_resolvable_assets_exist_on_disk():
    """Sanity check on the test's own asset list, so a typo here fails
    loudly instead of silently passing the spec check below."""
    for asset in _runtime_resolvable_assets():
        assert asset.exists(), f"asset the app expects to resolve is missing: {asset}"


def test_icon_path_asset_is_declared_in_the_spec():
    path = main_mod._icon_path()
    assert path is not None
    _assert_declared(path)


def test_every_runtime_resolvable_asset_is_declared_in_the_spec_datas():
    declared_sources = {Path(src).resolve() for src, _dest in _spec_datas()}
    missing = sorted(
        str(asset) for asset in _runtime_resolvable_assets()
        if asset.resolve() not in declared_sources)
    assert not missing, (
        "these runtime-resolvable assets are not declared in "
        "packaging/blendfleet.spec's datas -- a frozen build would raise "
        f"FileNotFoundError on first use: {missing}")


def _assert_declared(path: Path) -> None:
    declared_sources = {Path(src).resolve() for src, _dest in _spec_datas()}
    assert path.resolve() in declared_sources, (
        f"{path} is resolvable at runtime but not declared in "
        "packaging/blendfleet.spec's datas")
