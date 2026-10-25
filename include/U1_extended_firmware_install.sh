#!/usr/bin/env bash
# Automated Filament Changer Klipper-Add-On
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Enables (-e) or disables (-d) AFC on a Snapmaker U1. The firmware's extended settings page
# runs this script by path; the work is done by the Python installer.

set -e

# This script runs as root from the extended firmware firmware-config page
if [ $EUID -eq 0 ]; then
    HOME="/home/lava"
fi
afc_dir="${HOME}/AFC-Klipper-Add-On"

source "${afc_dir}/include/py_installer.sh"
afc_exec_python_installer u1 "${afc_dir}" "$0" "$@"
