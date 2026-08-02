#!/usr/bin/env bash
# packaging/build_linux.sh
set -euo pipefail

# Build with the project's own interpreter, not whatever `python3` resolves
# to. PyInstaller freezes the environment it RUNS IN -- a system Python
# lacking PySide6/kaggle still reports "Build complete!" and produces a
# small binary that only fails when the user launches it.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "Project virtualenv not found at $PY" >&2
  echo "Create it and install deps first:" >&2
  echo "  python3 -m venv .venv && .venv/bin/python -m pip install PySide6 kaggle pyinstaller" >&2
  exit 1
fi

cd "$ROOT"

"$PY" - <<'PYCHECK'
import importlib.util, sys
missing = [m for m in ("PySide6", "kaggle", "kagglesdk")
           if importlib.util.find_spec(m) is None]
if missing:
    sys.exit("MISSING from build interpreter: " + ", ".join(missing))
print("build interpreter has PySide6, kaggle, kagglesdk")
PYCHECK

"$PY" -m pip install --upgrade --quiet pyinstaller
"$PY" -m PyInstaller --noconfirm --clean packaging/blendfleet.spec

# A build that omits PySide6 still "succeeds" -- check the result is real.
BIN="$ROOT/dist/blendfleet"
[ -f "$BIN" ] || { echo "Build reported success but $BIN does not exist." >&2; exit 1; }

WARN="$ROOT/build/blendfleet/warn-blendfleet.txt"
if [ -f "$WARN" ] && grep -qE 'missing module named (PySide6|kaggle|kagglesdk)$' "$WARN"; then
  echo "Build omitted required packages -- the binary would fail at launch:" >&2
  grep -E 'missing module named (PySide6|kaggle|kagglesdk)$' "$WARN" >&2
  exit 1
fi

MB=$(( $(stat -c%s "$BIN") / 1048576 ))
if [ "$MB" -lt 30 ]; then
  echo "dist/blendfleet is only ${MB} MB. PySide6 alone is larger, so something was not bundled." >&2
  exit 1
fi

echo
echo "Built dist/blendfleet (${MB} MB)"
echo "Config lives at \${XDG_CONFIG_HOME:-~/.config}/blendfleet"
