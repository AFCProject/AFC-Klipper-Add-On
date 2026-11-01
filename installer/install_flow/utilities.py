# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Small troubleshooting actions from the utilities menu."""
from __future__ import annotations

from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit


def del_var_file(ctx: Context, console: Console, prompter: Prompter) -> str:
    """
    Delete `AFC.var.unit` after a y/n confirmation (a troubleshooting step).

    Only a (case insensitive, trimmed) `y` confirms. End of input stops the installer.
    The returned text keeps its trailing newline.

    :param ctx: installer context
    :param console: where the warning goes
    :param prompter: where the answer comes from
    :return str: the status message for the menu
    """
    console.print_msg(
        "OTHER",
        "This is generally a troubleshooting step and should not be needed during normal "
        "operation.",
    )
    answer = prompter.ask("Do you want to proceed? (y/n): ")
    if answer is None:
        raise InstallerExit(1)
    if answer.strip().lower() != "y":
        return "Operation cancelled by user.\n"
    var_file = ctx.afc_config_dir / "AFC.var.unit"
    if var_file.is_file():
        var_file.unlink()
        return "Removed old AFC.var.unit file.\n"
    return "No AFC.var.unit file found to remove.\n"
