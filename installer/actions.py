# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The real Actions the menus call, wiring them to the install, update and uninstall flows."""
from __future__ import annotations

from typing import NoReturn

from installer.install_flow.exit import exit_afc_install
from installer.install_flow.install import InstallOptions, install_afc
from installer.install_flow.uninstall import uninstall_afc
from installer.install_flow.utilities import del_var_file
from installer.platform_core.console import Console, Prompter
from installer.platform_core.system import CommandRunner
from installer.ui_menus.state import MenuState
from installer.unit_registry.naming import rename_unit_prompt
from installer.unit_registry.registry import unit_for
from installer.update_flow.update import update_afc
from installer.update_flow.version import get_git_version


class RealActions:
    """Runs the flows for the menus and stores their messages on the menu state."""

    def __init__(self, console: Console, prompter: Prompter, runner: CommandRunner) -> None:
        """
        Create the actions.

        :param console: where messages go
        :param prompter: where answers come from
        :param runner: command runner
        """
        self._console = console
        self._prompter = prompter
        self._runner = runner

    def install(self, state: MenuState) -> None:
        """
        Install the first unit and show the install message.

        :param state: menu state, `message` is set
        """
        options = InstallOptions(selection=state.selection, toggles=dict(state.toggles))
        state.message = install_afc(
            state.ctx, self._console, self._prompter, self._runner, options
        )

    def install_additional(self, state: MenuState) -> None:
        """
        Copy the files for an additional unit (the menu wires up its buffer afterwards).

        :param state: menu state
        """
        unit = unit_for(state.selection.installation_type)
        if unit is not None:
            unit.install_additional(state.ctx, self._console, self._prompter, state.selection)

    def update(self, state: MenuState) -> None:
        """
        Update AFC and add the result to the update menu's message.

        :param state: menu state, `update_message` grows with each update in the session
        """
        version = get_git_version(state.ctx, self._runner) if state.ctx.git_install else ""
        state.update_message += update_afc(
            state.ctx, self._console, self._prompter, self._runner, version
        )

    def uninstall(self, state: MenuState) -> None:
        """
        Remove AFC and show the uninstall message.

        :param state: menu state, `message` is set
        """
        state.message = uninstall_afc(state.ctx, self._console, self._runner)

    def delete_var_file(self, state: MenuState) -> str:
        """
        Remove AFC.var.unit after a confirmation.

        :param state: menu state
        :return str: the status message
        """
        return del_var_file(state.ctx, self._console, self._prompter)

    def rename_unit(self, state: MenuState) -> str:
        """
        Ask for and apply a unit rename.

        :param state: menu state
        :return str: the status message
        """
        return rename_unit_prompt(state.ctx, self._console, self._prompter)

    def exit(self, state: MenuState) -> NoReturn:
        """
        Restart Klipper if needed, clean up and end the installer.

        :param state: menu state
        """
        exit_afc_install(state.ctx, self._console, self._runner)
