# packaging/blendfleet.spec  -- shared by Windows and Linux builds
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

_HERE = Path(SPECPATH)
datas = collect_data_files("kagglesdk") + collect_data_files("kaggle")
datas += [(str(_HERE / "../assets/blendfleet_icon_256.png"), "assets")]

a = Analysis(["../blendfleet/__main__.py"], pathex=[".."], binaries=[],
             datas=datas, hiddenimports=["kagglesdk", "kaggle"],
             hookspath=[], runtime_hooks=[], excludes=[],
             cipher=block_cipher)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
          name="blendfleet", debug=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False,
          icon=str(_HERE / "../assets/blendfleet.ico"))
