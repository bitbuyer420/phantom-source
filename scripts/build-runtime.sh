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
"$PYINSTALLER" --noconfirm --clean phantom-runtime.spec

"$ENGINE/dist/phantom-runtime/phantom-runtime" --help >/dev/null
"$ENGINE/dist/phantom-runtime/phantom-runtime" tunneld --check

echo "Phantom runtime built at $ENGINE/dist/phantom-runtime"
