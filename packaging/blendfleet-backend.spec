# packaging/blendfleet-backend.spec -- the headless sidecar.
#
# BlendFleet's backend, with no window and no Qt, for the Electron shell
# to spawn and talk to over a pipe (see blendfleet/rpc/). The two GUI
# specs beside this one freeze the same core with PySide6 attached; this
# one deliberately does not, which is most of why an Electron build is
# worth having at all.
#
# TWO THINGS HERE ARE LOAD-BEARING.
#
# 1. console=True. A windowed PyInstaller build sets sys.stdout and
#    sys.stderr to None -- and this process's entire job is to answer on
#    stdout. Built windowed, it would start, read its first line, and
#    raise on the reply, for reasons no log would explain. Electron
#    spawns it with windowsHide, so the console it technically owns is
#    never seen.
#
# 2. excludes=['PySide6', 'shiboken6']. Not an optimisation: it is the
#    assertion. blendfleet/design.py exists so Settings can validate an
#    accent without importing ui/theme, and if that ever regresses, the
#    build fails here rather than silently shipping 150 MB of Qt inside
#    an app that already ships Chromium.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

_HERE = Path(SPECPATH)

# kagglesdk ships data files (its own protos and cert bundle) that are
# read at runtime -- the same collection the GUI specs do.
datas = collect_data_files("kagglesdk") + collect_data_files("kaggle")

a = Analysis(["../blendfleet/rpc/__main__.py"], pathex=[".."], binaries=[],
             datas=datas, hiddenimports=["kagglesdk", "kaggle"],
             hookspath=[], runtime_hooks=[],
             excludes=["PySide6", "shiboken6", "PySide6.QtCore",
                       "PySide6.QtGui", "PySide6.QtWidgets", "tkinter"],
             noarchive=False)

pyz = PYZ(a.pure)

# onedir, like the GUI builds: a onefile sidecar unpacks itself into a
# temporary directory on EVERY launch, which is startup latency the
# window waits on, and a temp tree left behind if it is killed.
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name="blendfleet-backend", debug=False, strip=False, upx=False,
          console=True, disable_windowed_traceback=False,
          argv_emulation=False, target_arch=None, codesign_identity=None,
          entitlements_file=None)

coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,
               strip=False, upx=False, name="blendfleet-backend")
