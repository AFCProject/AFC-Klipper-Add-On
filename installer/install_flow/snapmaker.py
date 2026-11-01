# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Snapmaker U1 helpers shared by install, uninstall and the enable/disable entry."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from installer.config_editor.blocks import (
    FLUIDD_T_MACRO_COMMENTED_START,
    FLUIDD_T_MACRO_END,
    FLUIDD_T_MACRO_START,
    comment_range,
    uncomment_range,
)
from installer.config_editor.includes import manage_include
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.platform_core.fsops import safe_copy
from installer.platform_core.klipper_links import link_extensions, unlink_extensions
from installer.platform_core.system import (
    CommandRunner,
    run_or_exit,
    u1_restart_klipper,
    u1_write_debug_file,
)

LITE_NAMES = ("AFC.py", "AFC_unit.py", "AFC_lane.py")


def check_and_move_lite_files(ctx: Context, console: Console) -> None:
    """
    Keep a `.lite` copy of each Snapmaker original before AFC replaces it.

    An existing copy is never overwritten. Every message says the file is copied to
    `AFC.py.lite`, whichever file it is.

    :param ctx: installer context
    :param console: where progress messages go
    """
    for name in LITE_NAMES:
        lite = ctx.klipper_extra_path / f"{name}.lite"
        if not lite.is_file():
            console.print_msg("OTHER", f"Copying {name} to AFC.py.lite")
            shutil.copy(ctx.klipper_extra_path / name, lite)


def move_lite_files_back(ctx: Context) -> None:
    """
    Put each `.lite` original back over the file AFC installed (even over a symlink).

    :param ctx: installer context
    """
    for name in LITE_NAMES:
        lite = ctx.klipper_extra_path / f"{name}.lite"
        if lite.is_file():
            os.replace(lite, ctx.klipper_extra_path / name)


def comment_gcode_in_fluidd(ctx: Context, console: Console, comment: bool) -> None:
    """
    Comment out (or restore) Snapmaker's `T<n>` macros in fluidd.cfg.

    :param ctx: installer context
    :param console: where progress and errors go
    :param comment: True to comment the macros out, False to uncomment them
    """
    path = ctx.printer_config_dir / "fluidd.cfg"
    try:
        if comment:
            console.print_msg("OTHER", "Commenting out gcode_macro T(n) in fluidd.cfg file")
            comment_range(path, FLUIDD_T_MACRO_START, FLUIDD_T_MACRO_END)
        else:
            console.print_msg("OTHER", "Uncommenting gcode_macro T(n) lines in fluidd.cfg file")
            uncomment_range(path, FLUIDD_T_MACRO_COMMENTED_START, FLUIDD_T_MACRO_END)
    except FileNotFoundError as exc:
        # a missing fluidd.cfg is a hard stop (exit status 2)
        console.print_msg("ERROR", f"sed: can't read {path}: No such file or directory")
        raise InstallerExit(2) from exc


def copy_snapmaker_config(ctx: Context, console: Console, prompter: Prompter) -> None:
    """
    Copy the Snapmaker flavored AFC config (U1 macros and hardware file) for an install.

    Each copy asks before replacing something.

    :param ctx: installer context
    :param console: where messages go
    :param prompter: used when a destination already exists
    """
    cfg = ctx.afc_config_dir
    templates = ctx.afc_path / "templates"
    cfg.mkdir(parents=True, exist_ok=True)
    safe_copy(ctx, console, prompter, templates / "u1_macros" / "AFC.cfg", cfg)
    safe_copy(ctx, console, prompter, ctx.afc_path / "config" / "AFC_Macro_Vars.cfg", cfg)
    (cfg / "mcu").mkdir(exist_ok=True)
    (cfg / "macros").mkdir(exist_ok=True)
    macros = cfg / "macros"
    safe_copy(ctx, console, prompter, templates / "u1_macros" / "Snapmaker_macros.cfg", macros)
    afc_macros = ctx.afc_path / "config" / "macros" / "AFC_macros.cfg"
    safe_copy(ctx, console, prompter, afc_macros, macros)
    safe_copy(ctx, console, prompter, templates / "AFC_Hardware_U1.cfg", cfg / "AFC_Hardware.cfg")


def check_and_move_afc_files(ctx: Context, console: Console) -> None:
    """
    Create the AFC config directory from the U1 templates, unless it already exists.

    Unlike the installer, this copies without asking (the firmware has no terminal).

    :param ctx: installer context
    :param console: where progress messages go
    """
    cfg = ctx.afc_config_dir
    if cfg.is_dir():
        return
    console.print_msg("OTHER", "Making AFC directories in printer_data/config directory")
    (cfg / "macros").mkdir(parents=True)
    (cfg / "mcu").mkdir()
    console.print_msg("OTHER", "Copying AFC config files to printer_data/config/AFC directory")
    templates = ctx.afc_path / "templates"
    shutil.copy(templates / "u1_macros" / "AFC.cfg", cfg)
    shutil.copy(ctx.afc_path / "config" / "AFC_Macro_Vars.cfg", cfg)
    shutil.copy(ctx.afc_path / "config" / "macros" / "AFC_macros.cfg", cfg / "macros")
    shutil.copy(templates / "u1_macros" / "Snapmaker_macros.cfg", cfg / "macros")
    shutil.copy(templates / "AFC_Hardware_U1.cfg", cfg / "AFC_Hardware.cfg")


def u1_enable(
    ctx: Context,
    console: Console,
    runner: CommandRunner,
    euid: Optional[int] = None,
    oem_dir: Path = Path("/oem"),
) -> None:
    """
    Enable AFC on a Snapmaker U1 (the firmware's `-e` action) and restart Klipper.

    :param ctx: installer context
    :param console: where progress messages go
    :param runner: command runner
    :param euid: effective user id, defaults to the current process's
    :param oem_dir: location of the oem directory
    """
    console.print_msg("OTHER", "Enabling AFC")
    u1_write_debug_file(console, runner, euid, oem_dir)
    check_and_move_lite_files(ctx, console)
    check_and_move_afc_files(ctx, console)
    run_or_exit(runner, ["chown", "-R", "lava:lava", str(ctx.afc_config_dir)])
    comment_gcode_in_fluidd(ctx, console, comment=True)
    console.print_msg("OTHER", "Adding AFC include to printer.cfg file")
    manage_include(ctx.printer_config_dir / "printer.cfg", "add")
    console.print_msg("OTHER", "Symlinking AFC files to klipper extras folder")
    link_extensions(ctx)
    console.print_msg("OTHER", "AFC enabled. Restarting Klipper...")
    u1_restart_klipper(runner, euid)


def u1_disable(
    ctx: Context, console: Console, runner: CommandRunner, euid: Optional[int] = None
) -> None:
    """
    Disable AFC on a Snapmaker U1 (the firmware's `-d` action) and restart Klipper.

    :param ctx: installer context
    :param console: where progress messages go
    :param runner: command runner
    :param euid: effective user id, defaults to the current process's
    """
    console.print_msg("OTHER", "Disabling AFC")
    console.print_msg("OTHER", "Removing AFC include from printer.cfg file")
    manage_include(ctx.printer_config_dir / "printer.cfg", "remove")
    console.print_msg("OTHER", "Removing AFC symlinks in klipper extras folder")
    unlink_extensions(ctx, console)
    console.print_msg("OTHER", "Moving AFC lite files back")
    move_lite_files_back(ctx)
    comment_gcode_in_fluidd(ctx, console, comment=False)
    console.print_msg("OTHER", "AFC disabled. Restarting Klipper...")
    u1_restart_klipper(runner, euid)
