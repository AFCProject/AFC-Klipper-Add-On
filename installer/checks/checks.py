# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Preflight checks run before the installer shows its menus."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

from installer.platform_core.console import Console, Prompter
from installer.platform_core.constants import MINIMUM_PYTHON
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.platform_core.platform_info import detect_platform
from installer.platform_core.system import CommandRunner

_VERSION_PROGRAM = "import sys; print('.'.join(map(str, sys.version_info[:2])))"


def check_root(
    ctx: Context,
    console: Console,
    euid: Optional[int] = None,
    os_release: Optional[Path] = None,
) -> None:
    """
    Refuse to run as root, except on a K1 OS (not Snapmaker) where root is the norm.

    Platform detection runs first and is recorded on the context.

    :param ctx: installer context, updated with the detected platform
    :param console: where messages go
    :param euid: effective user id, defaults to the current process's
    :param os_release: os-release file to inspect, defaults to /etc/os-release
    """
    ctx.apply_platform(detect_platform(os_release))
    user = os.geteuid() if euid is None else euid
    if user != 0:
        return
    if ctx.is_k1_os and not ctx.is_snapmaker:
        console.print_msg("WARNING", "K1 OS detected, skipping root user check.")
        return
    console.print_msg("ERROR", "  Do not run as root, use a normal user")
    raise InstallerExit(1)


def check_for_hh(ctx: Context, console: Console) -> None:
    """
    Stop if Happy Hare is installed in Klipper's extras, since AFC cannot coexist with it.

    :param ctx: installer context
    :param console: where the error goes
    """
    mmu = ctx.klipper_extra_path / "mmu.py"
    try:
        found = b"Happy Hare" in mmu.read_bytes()
    except OSError:  # missing, a directory, or unreadable: no match
        found = False
    if found:
        console.print_msg(
            "ERROR",
            "Happy Hare was found installed in your klipper extras. AFC is not currently "
            "compatible",
        )
        console.print_msg(
            "ERROR",
            "with Happy Hare. Please remove it, and then re-run this install-afc.sh script.",
        )
        raise InstallerExit(1)


def _venv_python_version(python: Path, runner: CommandRunner) -> Optional[Tuple[int, int]]:
    """
    Ask an interpreter for its `(major, minor)` version.

    :param python: interpreter to run
    :param runner: command runner
    :return Optional[Tuple[int, int]]: the version, or None if it could not be read
    """
    result = runner.run([str(python), "-c", _VERSION_PROGRAM])
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1])


def check_python_version(ctx: Context, console: Console, runner: CommandRunner) -> None:
    """
    Require the Klipper venv's Python to be at least the supported minimum.

    The versions are compared as numbers, so 3.10 is newer than 3.8. The messages print
    uncolored.

    :param ctx: installer context, `klipper_venv` holds the interpreter directory
    :param console: where messages go
    :param runner: command runner
    """
    python = ctx.klipper_venv / "python"
    if not os.access(python, os.X_OK):
        console.print_msg(
            "OTHER",
            f"Python not found at {python}. Please double-check your klipper "
            "configuration or specify a directory with the -y flag.",
        )
        raise InstallerExit(1)
    version = _venv_python_version(python, runner)
    if version is None:
        console.print_msg("OTHER", "Failed to determine Python version")
        raise InstallerExit(1)
    text = f"{version[0]}.{version[1]}"
    if version < MINIMUM_PYTHON:
        console.print_msg(
            "OTHER",
            f"Your available Klipper venv Python version {text} is too old. The BoxTurtle "
            f"AFC software requires at least Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}.",
        )
        raise InstallerExit(1)
    console.print_msg("OTHER", f"Python version {text} is OK.")


def check_for_zip_install(ctx: Context, console: Console, prompter: Prompter) -> None:
    """
    Detect a ZIP (non-git) install, warn about it, and wait for Enter.

    A `.git` file (as in a worktree) is not a git directory, so it counts as a ZIP
    install too. End of input at the prompt stops the installer.

    :param ctx: installer context, `git_install` is cleared for a ZIP install
    :param console: where the warnings go
    :param prompter: used for the "Press Enter" prompt
    """
    if (ctx.afc_path / ".git").is_dir():
        return
    ctx.git_install = False
    console.print_msg("WARNING", "AFC-Klipper-Add-On appears to have been installed via ZIP file.")
    console.print_msg(
        "WARNING", "Some features may not work as expected without a Git installation."
    )
    console.print_msg("WARNING", "Any updates will require manual reinstallation via ZIP file.")
    if prompter.ask("Press Enter to continue...") is None:
        raise InstallerExit(1)


def check_existing_install(ctx: Context) -> None:
    """
    Set `prior_installation` if any AFC extension is already symlinked into Klipper.

    Every `extras/*.py` name is checked, `__init__.py` included, and a dangling
    link counts. The flag is never cleared here.

    :param ctx: installer context
    """
    extras = sorted((ctx.afc_path / "extras").glob("*.py"))
    if any((ctx.klipper_extra_path / path.name).is_symlink() for path in extras):
        ctx.prior_installation = True
