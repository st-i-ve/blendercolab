#!/usr/bin/env bash
# packaging/build_linux.sh
set -euo pipefail
python3 -m pip install --upgrade pyinstaller
python3 -m PyInstaller --noconfirm --clean packaging/blendfleet.spec
echo
echo "Built dist/blendfleet"
echo "Config lives at \${XDG_CONFIG_HOME:-~/.config}/blendfleet"
