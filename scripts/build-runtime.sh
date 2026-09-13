#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="$ROOT/engine"
PYINSTALLER="$ENGINE/.venv/bin/pyinstaller"

if [ ! -x "$PYINSTALLER" ]; then
  echo "PyInstaller is missing. Run: uv pip install --python engine/.venv/bin/python pyinstaller" >&2
  exit 1
fi

cd "$ENGINE"
"$PYINSTALLER" \
  --noconfirm \
  --clean \
  --onedir \
  --name phantom-runtime \
  --paths "$ENGINE" \
  --add-data "phantom/webui:phantom/webui" \
  --collect-all pymobiledevice3 \
  --copy-metadata apple-compress \
  --copy-metadata pyimg4 \
  --copy-metadata ipsw-parser \
  --copy-metadata pymobiledevice3 \
  --collect-submodules uvicorn \
  --collect-submodules fastapi \
  phantom_runtime.py

"$ENGINE/dist/phantom-runtime/phantom-runtime" --help >/dev/null
"$ENGINE/dist/phantom-runtime/phantom-runtime" tunneld --check

echo "Phantom runtime built at $ENGINE/dist/phantom-runtime"
