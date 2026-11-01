# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The uninstall action."""
from __future__ import annotations

from typing import Callable, Optional

from installer.config_editor.includes import manage_include
from installer.install_flow.snapmaker import comment_gcode_in_fluidd, move_lite_files_back
from installer.platform_core.console import Console
from installer.platform_core.context import Context
from installer.platform_core.fsops import backup_afc_config
from installer.platform_core.klipper_links import unlink_extensions
from installer.platform_core.system import CommandRunner, query_printer_status, restart_klipper

_UNINSTALLED = (
    "\n  AFC has been uninstalled successfully.\n\n"
    "  Please restart your printer to complete the uninstallation process.\n\n"
    "  We always welcome feedback on the software on our Discord channel at\n\n"
    "  https://discord.gg/z2tgWEnfDT.\n  "
)


def uninstall_afc(
    ctx: Context,
    console: Console,
    runner: CommandRunner,
    status_query: Callable[[str], bool] = query_printer_status,
    euid: Optional[int] = None,
) -> str:
    """
    Remove AFC: unlink the extensions, drop the printer.cfg include, back up the config.

    The AFC directory is moved to `AFC.backup.<date>`, never deleted. On a Snapmaker the
    original Klipper files come back and the fluidd macros are uncommented. Klipper is
    restarted if the printer is idle.

    :param ctx: installer context
    :param console: where messages go
    :param runner: command runner
    :param status_query: returns True when the printer is idle (injectable for tests)
    :param euid: effective user id, defaults to the current process's
    :return str: the uninstall message
    """
    unlink_extensions(ctx, console)
    manage_include(ctx.printer_config_dir / "printer.cfg", "remove")
    if ctx.is_snapmaker:
        move_lite_files_back(ctx)
        comment_gcode_in_fluidd(ctx, console, comment=False)
    backup_afc_config(ctx)
    restart_klipper(ctx, console, runner, status_query, euid)
    return _UNINSTALLED
