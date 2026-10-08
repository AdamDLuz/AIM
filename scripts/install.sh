#!/bin/sh
# Install AIM on macOS or Ubuntu. Arguments go to `aim install`.
# A pair file passed with --secret-file is read by the program and is not printed.
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$root/src"
export PYTHONUTF8=1
if command -v python3 >/dev/null 2>&1; then
  exec python3 -m aim install "$@"
fi
exec python -m aim install "$@"
