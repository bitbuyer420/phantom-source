#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
node --check app/main.js
node --check engine/phantom/webui/app.js
node --test app/tests/*.test.cjs tests/*.test.cjs
PYTHONDONTWRITEBYTECODE=1 engine/.venv/bin/python -m unittest discover -s engine/tests -v
