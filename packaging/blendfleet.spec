# packaging/blendfleet.spec  -- shared by Windows and Linux builds
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

_HERE = Path(SPECPATH)
datas = collect_data_files("kagglesdk") + collect_data_files("kaggle")
# Every asset the app can resolve at runtime -- via theme.py's LOGO_DIR /
# ICONS_DIR / FONTS_DIR, or __main__.py's _icon_path() -- must be listed
# here. PyInstaller only extracts what `datas` names into sys._MEIPASS at
# runtime; anything the source tree has but this list omits raises
# FileNotFoundError on first launch of the frozen build, not at build time.
# tests/test_packaging_assets.py cross-checks this list against every such
# path so a future addition (a new icon, a new font weight, a second mark
# variant) fails a test instead of only surfacing in a packaged build.
datas += [
    (str(_HERE / "../assets/logo/app-icon-256.png"), "assets/logo"),
    (str(_HERE / "../assets/logo/mark-white.png"), "assets/logo"),
]
# Whole-directory glob rather than a hand-maintained per-file list: adding
# a bundled icon or font weight to assets/icons|fonts/ must not also
# require remembering to add it here.
datas += [(str(p), "assets/icons")
          for p in (_HERE / "../assets/icons").glob("*.svg")]
datas += [(str(p), "assets/fonts")
          for p in (_HERE / "../assets/fonts").glob("*.ttf")]

a = Analysis(["../blendfleet/__main__.py"], pathex=[".."], binaries=[],
             datas=datas, hiddenimports=["kagglesdk", "kaggle"],
             hookspath=[], runtime_hooks=[], excludes=[],
             cipher=block_cipher)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
          name="blendfleet", debug=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False,
          icon=str(_HERE / "../assets/logo/blendfleet.ico"))
