# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Leaving the installer."""
from __future__ import annotations

from typing import Callable, NoReturn, Optional

from installer.platform_core.console import Console
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.platform_core.fsops import remove_vars_tool_file
from installer.platform_core.system import CommandRunner, query_printer_status, restart_klipper


def exit_afc_install(
    ctx: Context,
    console: Console,
    runner: CommandRunner,
    status_query: Callable[[str], bool] = query_printer_status,
    euid: Optional[int] = None,
) -> NoReturn:
    """
    Restart Klipper if anything was installed or updated, clean up, and exit with status 0.

    In test mode the restart is skipped with a notice.

    :param ctx: installer context
    :param console: where messages go
    :param runner: command runner
    :param status_query: returns True when the printer is idle (injectable for tests)
    :param euid: effective user id, defaults to the current process's
    :raises InstallerExit: always, with code 0
    """
    if ctx.files_updated_or_installed:
        if not ctx.test_mode:
            console.print_msg("INFO", "Restarting Klipper service to apply changes...")
            restart_klipper(ctx, console, runner, status_query, euid)
        else:
            console.print_msg("INFO", "Test mode enabled; skipping Klipper restart.")
    remove_vars_tool_file(ctx)
    raise InstallerExit(0)
