# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The utilities menu."""
from __future__ import annotations

from typing import Optional

from installer.ui_menus.navigation import MenuDeps, read_choice
from installer.ui_menus.state import MenuState
from installer.ui_menus.view import UPDATE_OPTIONS_BOX, UTILITIES_HEADER_BOX, MenuView, row


class UtilitiesMenu:
    """Troubleshooting helpers: remove AFC.var.unit and rename a unit."""

    def __init__(self, state: MenuState, deps: MenuDeps) -> None:
        """
        Create the menu.

        :param state: shared menu state
        :param deps: menu collaborators
        """
        self._state = state
        self._deps = deps

    def view(self) -> MenuView:
        """
        Describe the current screen.

        :return MenuView: the screen
        """
        return MenuView(
            art="utilities",
            help_box=UTILITIES_HEADER_BOX,
            message=self._state.unit_message,
            options_box=UPDATE_OPTIONS_BOX,
            rows=[
                row(), row("1. Remove AFC.var.unit file"), row("R. Rename existing unit"),
                row(), row("M. Return to Main Menu"), row("Q. Quit"), row(),
            ],
        )

    def step(self) -> Optional[str]:
        """
        Draw the screen, read one choice and handle it.

        :return Optional[str]: the next menu name, "utilities" to stay, or None to stop
        """
        deps = self._deps
        state = self._state
        deps.renderer.clear()
        deps.renderer.show(self.view())
        choice = read_choice(deps)
        if choice == "1":
            state.unit_message = deps.actions.delete_var_file(state)
        elif choice == "R":
            state.unit_message = deps.actions.rename_unit(state)
        elif choice == "M":
            state.message = ""
            return "main"
        elif choice == "Q":
            deps.actions.exit(state)
            return None
        else:
            state.message = "Invalid selection, please try again."
        return "utilities"
