#!/usr/bin/env bash
# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Finds a Python 3.8+ interpreter and hands over to the Python installer (installer/).

afc_python_ok() {
  # True if "$1" runs and is Python 3.8 or newer.
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1
}

afc_find_python() {
  # Prints the interpreter to use: the -y directory's python, then the Klipper venv, then
  # python3, then python (K1 OS only has /usr/bin/python). Returns 1 if none is 3.8+.
  local venv="" opt candidate
  OPTIND=1
  while getopts ":a:k:s:m:n:b:p:y:th" opt; do
    if [ "${opt}" == "y" ]; then
      venv="${OPTARG}"
    fi
  done
  OPTIND=1
  for candidate in "${venv:+${venv}/python}" "${HOME}/klippy-env/bin/python" python3 python; do
    [ -n "${candidate}" ] || continue
    if command -v "${candidate}" >/dev/null 2>&1 && afc_python_ok "${candidate}"; then
      command -v "${candidate}"
      return 0
    fi
  done
  return 1
}

afc_exec_python_installer() {
  # Usage: afc_exec_python_installer <install|update|u1> <script dir> <script path> [args...]
  local command="$1" script_dir="$2" script_path="$3" python
  shift 3
  if ! python="$(afc_find_python "$@")"; then
    echo "Python 3.8 or newer was not found. Install it or point -y at the Klipper venv bin directory."
    exit 1
  fi
  export AFC_PROG="${script_path}"
  export AFC_SHIM="${script_dir}/$(basename "${script_path}")"
  export PYTHONPATH="${script_dir}${PYTHONPATH:+:${PYTHONPATH}}"
  exec "${python}" -m installer "${command}" "$@"
}
