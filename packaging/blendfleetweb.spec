# packaging/blendfleetweb.spec -- the web-UI build.
#
# Same datas as blendfleet.spec (they share every asset, plus
# blendfleet/web/), a different entry point and a different exe name,
# so both builds can sit side by side while the port is in progress.
# PyInstaller's PySide6 hooks pull in QtWebEngineProcess.exe and
# Chromium's .pak/icu resources on their own -- they are not listed
# here because they are not OUR assets, and hard-coding their paths
# would break on the next Qt update.
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
# The web UI. Kept at blendfleet/web/ in the bundle because app.css reaches
# the vendored fonts and the brand mark with ../../assets/... -- the same
# relative layout as the source tree, so one set of paths works in both.
datas += [(str(p), "blendfleet/web")
          for p in (_HERE / "../blendfleet/web").glob("*.*")]

a = Analysis(["../blendfleet/web_main.py"], pathex=[".."], binaries=[],
             datas=datas, hiddenimports=["kagglesdk", "kaggle"],
             hookspath=[], runtime_hooks=[], excludes=[],
             cipher=block_cipher)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ONEDIR, not onefile, and the difference is the whole startup cost.
#
# A onefile EXE (binaries/zipfiles/datas passed straight into EXE(...)) is a
# self-extracting archive: every single launch unpacks ~228 MB of Qt,
# Chromium and Python into a fresh %TEMP%\_MEIxxxxx directory BEFORE the
# first window can appear, then deletes it on exit. On a 16 GB laptop with
# ordinary disk that is tens of seconds of nothing happening, repeated in
# full on every start, and none of it is cached between runs.
#
# exclude_binaries=True keeps those out of the EXE and COLLECT lays them
# down next to it once, at build time. sys._MEIPASS then points at the
# install folder instead of a temp dir, which every asset lookup here
# already tolerates (blendfleet/ui/web_host.py's WEB_DIR resolves relative
# to the package, blendfleet/__main__.py's _icon_path() checks _MEIPASS
# first) -- so the layout below deliberately mirrors the source tree.
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name="blendfleetweb", debug=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False,
          icon=str(_HERE / "../assets/logo/blendfleet.ico"))
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,
               strip=False, upx=False, name="blendfleetweb")
