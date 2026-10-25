#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Starts the AFC updater (the Python package in installer/). All options are handled there;
# run with -h to list them.

set -e
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/include/py_installer.sh"
afc_exec_python_installer update "${SCRIPT_DIR}" "$0" "$@"
