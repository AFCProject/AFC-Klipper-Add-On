# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The install and update startup sequences."""
from __future__ import annotations

import time
from typing import Callable

from installer.checks.checks import (
    check_existing_install,
    check_for_hh,
    check_for_zip_install,
    check_python_version,
    check_root,
)
from installer.git_sync import Reexec, clone_and_maybe_restart
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.system import CommandRunner

_PAUSE_SECONDS = 2.0


def install_startup(
    ctx: Context,
    console: Console,
    prompter: Prompter,
    runner: CommandRunner,
    reexec: Reexec,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """
    Run the install checks in order, ending with the screen clear before the main menu.

    No external config tools are needed, so there is no prerequisite check.

    :param ctx: installer context
    :param console: where messages go
    :param prompter: used for the ZIP install prompt
    :param runner: command runner
    :param reexec: restarts the script after a self update
    :param sleep: pause function, replaced in tests
    """
    console.print_msg("OTHER", "Ensuring we are not running as root (except on K1 OS)...")
    check_root(ctx, console)
    console.print_msg("OTHER", "Ensuring no conflicting software is present...")
    check_for_hh(ctx, console)
    if ctx.is_snapmaker:
        console.print_msg("OTHER", "Sourcing u1 extended file")
    console.print_msg("OTHER", "Checking installation method...")
    check_for_zip_install(ctx, console, prompter)
    if not ctx.test_mode:
        check_python_version(ctx, console, runner)
        if ctx.git_install and not ctx.is_snapmaker:
            clone_and_maybe_restart(ctx, console, runner, reexec)
    check_existing_install(ctx)
    console.print_msg("OTHER", "Starting installation process..")
    sleep(_PAUSE_SECONDS)
    console.clear()


def update_startup(
    ctx: Context,
    console: Console,
    runner: CommandRunner,
    reexec: Reexec,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """
    Run the update checks in order, ending with the screen clear before the update menu.

    A missing `.git` quietly marks a ZIP install, and unlike install the update always
    tries the clone / pull step (outside test mode).

    :param ctx: installer context
    :param console: where messages go
    :param runner: command runner
    :param reexec: restarts the script after a self update
    :param sleep: pause function, replaced in tests
    """
    if not (ctx.afc_path / ".git").is_dir():
        ctx.git_install = False
    console.print_msg("OTHER", "Ensuring we are not running as root...")
    check_root(ctx, console)
    console.print_msg("OTHER", "Ensuring no conflicting software is present...")
    check_for_hh(ctx, console)
    if not ctx.test_mode:
        check_python_version(ctx, console, runner)
        clone_and_maybe_restart(ctx, console, runner, reexec)
    check_existing_install(ctx)
    console.print_msg("OTHER", "Starting update process...")
    sleep(_PAUSE_SECONDS)
    console.clear()
