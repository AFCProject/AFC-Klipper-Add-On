# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Fixed defaults. Paths under HOME are built at runtime."""
from typing import Tuple

MINIMUM_PYTHON: Tuple[int, int] = (3, 8)

DEFAULT_GIT_REPO = "https://github.com/AFCProject/AFC-Klipper-Add-On.git"
DEFAULT_BRANCH = "main"

DEFAULT_KLIPPER_SERVICE = "klipper"
DEFAULT_MOONRAKER_ADDRESS = "http://localhost"
DEFAULT_MOONRAKER_PORT = "7125"

# Snapmaker U1 ships its python in /usr/bin and has no klippy-env
SNAPMAKER_KLIPPER_VENV = "/usr/bin"

OS_RELEASE_PATH = "/etc/os-release"
K1_OS_MARKER = "ID=buildroot"
SNAPMAKER_MARKER = 'RK_BUILD_INFO="snapmaker'
