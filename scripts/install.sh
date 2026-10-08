#!/bin/sh
# Install Intravo Messenger on macOS or Ubuntu. Arguments go to `ivm install`.
# A pair file passed with --secret-file is read by the program and is not printed.
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$root/src"
export PYTHONUTF8=1
if command -v python3 >/dev/null 2>&1; then
  exec python3 -m intravo_messenger install "$@"
fi
exec python -m intravo_messenger install "$@"
