# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The main menu."""
from __future__ import annotations

from typing import List, Optional

from installer.platform_core.errors import InstallerExit
from installer.ui_menus.navigation import MenuDeps, read_choice
from installer.ui_menus.settings import SETTINGS
from installer.ui_menus.state import INSTALLED_NOTE, MenuState
from installer.ui_menus.view import MAIN_OPTIONS_BOX, MenuView, Row, row

_BRANCH_NOTE = "To change the branch, please re-run this script with a '-b <branch>' option."


class MainMenu:
    """The top level menu."""

    def __init__(self, state: MenuState, deps: MenuDeps) -> None:
        """
        Create the menu. The message is kept, so an install result shows here.

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
        ctx = self._state.ctx
        installed = ctx.files_updated_or_installed or ctx.prior_installation
        status = ("True", "green") if installed else ("False", "red")
        top: List[Row] = [
            row("    This installation script will install/update the AFC Klipper extension "
                "to your system.\n"),
            row("    Prior AFC-Klipper-Add-On installation detected: ", status),
            row(f"\n      1. Printer Config Directory : {ctx.printer_config_dir} "),
            row(f"      2. Klipper Directory        : {ctx.klipper_dir} "),
            row(f"      3. Moonraker Config File    : {ctx.moonraker_config_file} "),
            row(f"      4. Klipper Service Name     : {ctx.klipper_service} "),
            row(f"      5. Branch                   : {ctx.branch} "),
            row(f"      6. Moonraker Address        : "
                f"{ctx.moonraker_address}:{ctx.moonraker_port}    "),
            row(),
        ]
        rows: List[Row] = [row()]
        if not ctx.prior_installation and not ctx.files_updated_or_installed:
            rows.append(row("I. Install New System"))
        if ctx.prior_installation:
            rows.append(row("U. Update AFC Klipper Add-On"))
        rows += [
            row("A. Install Additional System"),
            row("C. Utilities"),
            row("R. Remove AFC Klipper Add-On"),
            row("Q. Exit"),
            row(),
        ]
        return MenuView(
            art="main", top=top, message=self._state.message,
            options_box=MAIN_OPTIONS_BOX, rows=rows,
        )

    def _edit(self, choice: str) -> str:
        """
        Ask for a new value for a settings row and apply it.

        A blank answer keeps the value; a bad one keeps it and says why.

        :param choice: the row number typed
        :return str: the status message
        """
        setting = SETTINGS[choice]
        answer = self._deps.prompter.ask(f"Enter new {setting.noun} (blank to keep): ")
        if answer is None:
            raise InstallerExit(1)
        if not answer.strip():
            return f"The {setting.noun} was left unchanged."
        try:
            return setting.apply(self._state.ctx, answer)
        except ValueError as exc:
            return f"The {setting.noun} was not changed. {exc}"

    def step(self) -> Optional[str]:
        """
        Draw the screen, read one choice and handle it.

        :return Optional[str]: the next menu name, "main" to stay, or None to stop
        """
        deps = self._deps
        state = self._state
        deps.renderer.clear()
        deps.renderer.show(self.view())
        choice = read_choice(deps)
        if choice in SETTINGS:
            state.message = self._edit(choice)
        elif choice == "5":
            state.message = _BRANCH_NOTE
        elif choice == "I":
            if state.ctx.files_updated_or_installed:
                state.message = INSTALLED_NOTE
            else:
                return "unit_type_install"
        elif choice == "U":
            return "update"
        elif choice == "R":
            deps.actions.uninstall(state)
        elif choice == "C":
            return "utilities"
        elif choice == "A":
            return "unit_type_additional"
        elif choice == "Q":
            deps.actions.exit(state)
            return None
        else:
            deps.console.write("Invalid choice\n")
        return "main"
