#!/usr/bin/env bash
# AFCProject Automated Filament Changer
#
# Copyright (C) 2024-2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Wrapper for afc_debug.py. Finds a Python 3.8+ interpreter and hands over to it. All
# options are handled there; run with -h to list them.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python_ok() {
  # True if "$1" runs and is Python 3.8 or newer.
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1
}

find_python() {
  # Prints the first Python 3.8+ found: a Klipper venv (home directory, then the
  # simpleAF locations), then python3, then python.
  local candidate
  for candidate in "${HOME}/klippy-env/bin/python" /usr/share/klippy-env/bin/python \
                   /root/klippy-env/bin/python python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1 && python_ok "${candidate}"; then
      command -v "${candidate}"
      return 0
    fi
  done
  return 1
}

if ! PYTHON="$(find_python)"; then
  echo "Python 3.8 or newer was not found."
  echo "Install it and rerun the script."
  exit 1
fi

export AFC_PROG="$0"
exec "${PYTHON}" "${SCRIPT_DIR}/afc_debug.py" "$@"
