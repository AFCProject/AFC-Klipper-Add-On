# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The unit type picker shown before the install and additional-unit options screens."""
from __future__ import annotations

from typing import List, Optional

from installer.platform_core.errors import InstallerExit
from installer.ui_menus.navigation import MenuDeps, read_choice
from installer.ui_menus.state import MenuState
from installer.ui_menus.view import (
    PICKER_ADDITIONAL_BOX,
    PICKER_INSTALL_BOX,
    MenuView,
    Row,
    row,
)
from installer.unit_registry.naming import check_existing_unit_installed
from installer.unit_registry.registry import (
    UNIT_TYPES,
    additional_buffer_default,
    additional_buffer_options,
    additional_buffer_supported,
)

INSTALL = "install"
ADDITIONAL = "additional"
_ENTER = "Press Enter to return to the main menu..."


class UnitTypeMenu:
    """A numbered list of unit types; choosing one opens that type's options screen."""

    def __init__(self, state: MenuState, deps: MenuDeps, mode: str) -> None:
        """
        Create the picker.

        :param state: shared menu state
        :param deps: menu collaborators
        :param mode: ``install`` for the first unit or ``additional`` for another one
        """
        self._state = state
        self._deps = deps
        self._mode = mode
        state.message = ""
        self._refused = mode == ADDITIONAL and not check_existing_unit_installed(state.ctx)

    def view(self) -> MenuView:
        """
        Describe the current screen.

        :return MenuView: the screen
        """
        rows: List[Row] = [
            row(f"{number}. {unit.display_name}") for number, unit in enumerate(UNIT_TYPES, 1)
        ]
        rows += [row(), row("M. Return to Main Menu"), row("Q. Quit"), row()]
        box = PICKER_INSTALL_BOX if self._mode == INSTALL else PICKER_ADDITIONAL_BOX
        return MenuView(
            art="main", message=self._state.message, options_box=box, rows=rows)

    def _refuse(self) -> str:
        """
        Explain that a first unit is needed and wait for Enter.

        :return str: the next menu, always "main"
        """
        deps = self._deps
        deps.console.write(
            f"\nNo existing unit installation found in {self._state.ctx.afc_config_dir}.\n"
            "Please install your first unit from the main Install menu before adding "
            "additional units.\n\n"
        )
        if deps.prompter.ask(_ENTER) is None:
            raise InstallerExit(1)
        return "main"

    def _choose(self, number: int) -> str:
        """
        Select the unit type and prepare the selection for its options screen.

        :param number: 1 based position in the list
        :return str: the options screen to open
        """
        sel = self._state.selection
        unit = UNIT_TYPES[number - 1]
        sel.installation_type = unit.display_name
        if self._mode == INSTALL:
            sel.unit_name = unit.default_name
            unit.on_select(sel)
            return "install_options"
        if not additional_buffer_supported(unit.display_name):
            sel.additional_buffer_type = "None"
        elif (
            sel.buffer_type == "None"
            or sel.buffer_type not in additional_buffer_options(unit.display_name)
        ):
            sel.additional_buffer_type = additional_buffer_default(unit.display_name)
        else:
            sel.additional_buffer_type = sel.buffer_type
        return "additional_options"

    def step(self) -> Optional[str]:
        """
        Draw the list, read one choice and handle it.

        :return Optional[str]: the next menu name, the picker's own name to stay, or None to stop
        """
        deps = self._deps
        state = self._state
        stay = f"unit_type_{self._mode}"
        if self._refused:
            return self._refuse()
        deps.renderer.clear()
        deps.renderer.show(self.view())
        choice = read_choice(deps)
        if choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(UNIT_TYPES):
            return self._choose(int(choice))
        if choice == "M":
            return "main"
        if choice == "Q":
            deps.actions.exit(state)
            return None
        state.message = "Invalid selection, please try again."
        return stay
