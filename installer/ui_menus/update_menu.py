# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The update menu."""
from __future__ import annotations

from typing import Optional

from installer.ui_menus.navigation import MenuDeps, read_choice
from installer.ui_menus.state import MenuState
from installer.ui_menus.view import UPDATE_OPTIONS_BOX, MenuView, row
from installer.platform_core.errors import InstallerExit


class UpdateMenu:
    """Offers the update action."""

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
            message=self._state.update_message,
            options_box=UPDATE_OPTIONS_BOX,
            rows=[
                row(), row("U. Update AFC Klipper Add-On"), row(),
                row("M. Return to Main Menu"), row("Q. Quit"), row(),
            ],
        )

    def step(self) -> Optional[str]:
        """
        Draw the screen, read one choice and handle it.

        :return Optional[str]: the next menu name, "update" to stay, or None to stop
        """
        deps = self._deps
        state = self._state
        deps.renderer.clear()
        deps.renderer.show(self.view())
        choice = read_choice(deps)
        if choice == "U":
            if not state.ctx.git_install:
                deps.console.print_msg("WARNING", "Git installation not detected.")
                deps.console.print_msg(
                    "WARNING",
                    "Ensure you have manually updated the AFC-Klipper-Add-On directory "
                    "with the latest files from the GitHub repository.",
                )
                if deps.prompter.ask("Press Enter to continue or CTRL+C to cancel...") is None:
                    raise InstallerExit(1)
            deps.actions.update(state)
        elif choice == "M":
            state.message = ""
            return "main"
        elif choice == "Q":
            deps.actions.exit(state)
            return None
        else:
            state.message = "Invalid selection, please try again."
        return "update"
