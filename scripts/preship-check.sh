#!/usr/bin/env bash
# Pre-ship safety gate for Phantom.
#
# Fails (exit 1) if the built artifact contains anything that would deanonymize
# the seller or leak a buyer's data: absolute /Users/<name> paths, the build
# host's username or hostname, or config.json keys (bookmarks/recents/saved
# spoof coordinates). Wire this into the release build AFTER packaging and
# BEFORE signing/uploading. Example:  scripts/preship-check.sh dist/mac
#
# Usage: preship-check.sh [TARGET_DIR]   (default: dist)
set -u

TARGET="${1:-dist}"
if [ ! -e "$TARGET" ]; then
  echo "preship-check: target '$TARGET' does not exist" >&2
  exit 2
fi

USER_NAME="$(whoami)"
HOST_NAME="$(hostname -s 2>/dev/null || hostname)"

# Distinctive strings that must NEVER appear in a distributed artifact.
PATTERNS=(
  "/Users/"            # any absolute macOS home path
  "$USER_NAME"         # the build machine's username
  "$HOST_NAME"         # the build machine's hostname
  "/.local/share/uv"   # a dev uv interpreter path
  "virtual_lat"        # config.json key -> the file leaked in
  "\"bookmarks\""      # config.json key
  "\"recents\""        # config.json key
)

fail=0
echo "preship-check: scanning '$TARGET' (user=$USER_NAME host=$HOST_NAME)…"
for pat in "${PATTERNS[@]}"; do
  # --binary-files=text so venv shebangs / plists inside binaries are caught too.
  hits="$(grep -rlI --binary-files=text -- "$pat" "$TARGET" 2>/dev/null | sort -u)"
  if [ -n "$hits" ]; then
    echo "  ❌ found '$pat' in:"
    echo "$hits" | sed 's/^/       /'
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "preship-check: FAILED — strip the above before distributing."
  exit 1
fi
echo "preship-check: ✅ clean — no owner/buyer-identifying strings found."
