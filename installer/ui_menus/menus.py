# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The menu registry used by the entrypoint."""
from __future__ import annotations

from typing import Dict

from installer.ui_menus.additional_options_menu import AdditionalOptionsMenu
from installer.ui_menus.install_options_menu import InstallOptionsMenu
from installer.ui_menus.main_menu import MainMenu
from installer.ui_menus.navigation import MenuFactory
from installer.ui_menus.unit_type_menu import ADDITIONAL, INSTALL, UnitTypeMenu
from installer.ui_menus.update_menu import UpdateMenu
from installer.ui_menus.utilities_menu import UtilitiesMenu

MENUS: Dict[str, MenuFactory] = {
    "main": MainMenu,
    "additional_options": AdditionalOptionsMenu,
    "install_options": InstallOptionsMenu,
    "unit_type_install": lambda state, deps: UnitTypeMenu(state, deps, INSTALL),
    "unit_type_additional": lambda state, deps: UnitTypeMenu(state, deps, ADDITIONAL),
    "update": UpdateMenu,
    "utilities": UtilitiesMenu,
}
