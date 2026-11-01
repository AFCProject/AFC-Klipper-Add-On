# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The install action."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from installer.config_editor.edits import (
    ensure_prep_section,
    remove_tip_cut_servo_section,
    replace_text_in_tree,
    replace_varfile_path,
    set_tool_start_pin,
    set_value,
)
from installer.config_editor.includes import (
    ensure_moonraker_update_block,
    manage_include,
    moonraker_update_block,
)
from installer.install_flow.config_copy import copy_config
from installer.install_flow.snapmaker import (
    check_and_move_lite_files,
    comment_gcode_in_fluidd,
    copy_snapmaker_config,
)
from installer.platform_core.console import NC, RED, Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.klipper_links import exclude_from_klipper_git, link_extensions
from installer.platform_core.toggles import TOGGLES, default_toggles
from installer.platform_core.system import CommandRunner, restart_service, u1_write_debug_file
from installer.unit_registry.buffers import apply_unit_buffer
from installer.unit_registry.extruders import apply_unit_extruder
from installer.unit_registry.registry import unit_for
from installer.unit_registry.selection import UnitSelection

_BOXTURTLE_TYPES = ("BoxTurtle (4-Lane)", "BoxTurtle (8-Lane)")


@dataclass
class InstallOptions:
    """The unit choices and the option toggles from the install menu."""

    selection: UnitSelection
    toggles: Dict[str, bool] = field(default_factory=default_toggles)


def apply_boxturtle_rename(ctx: Context, sel: UnitSelection) -> None:
    """
    Rewrite the template name `Turtle_1` everywhere if a BoxTurtle got another name.

    This is deliberately directory wide: the first unit's name can appear in shared files.
    Other unit types rename themselves while their files are copied.

    :param ctx: installer context
    :param sel: the user's unit choices
    """
    if sel.unit_name != "Turtle_1" and sel.installation_type in _BOXTURTLE_TYPES:
        replace_text_in_tree(ctx.afc_config_dir, "Turtle_1", sel.unit_name)


def build_install_message(ctx: Context, sel: UnitSelection) -> str:
    """
    The text shown after an install: what changed, unit follow-ups and buffer notes.

    :param ctx: installer context
    :param sel: the user's unit choices
    :return str: the message, with real terminal color escapes
    """
    message = (
        f"\n- AFC Configuration updated with selected options at {ctx.afc_file}\n\n"
        "- AFC-Klipper-Add-On python extensions installed to "
        f"{ctx.klipper_dir}/klippy/extras/\n"
    )
    unit = unit_for(sel.installation_type)
    if unit is not None:
        message += unit.message(ctx, sel)
    if sel.buffer_type == "TurtleNeckV2":
        message += (
            "\n- Ensure you add the correct serial information to the "
            f"{ctx.afc_config_dir}/mcu/TurtleNeckv2.cfg file\n  "
        )
    if sel.buffer_type == "TurtleNeck2Remastered":
        message += (
            "\n- Ensure you add the correct serial information to the "
            f"{ctx.afc_config_dir}/mcu/TN2.cfg file\n  "
        )
    if sel.buffer_type == "FPS_PSF":
        message += (
            "\n- Ensure the PSF ADC pin in your buffer configuration matches where your "
            "wiring is connected to your MCU.\n  "
        )
    message += (
        "\nYou may now quit the script or return to the main menu.\n\n"
        f"{RED}If you would like to add any additional units, please restart the script to "
        f"ensure the\ncurrent units are loaded correctly.{NC}\n"
    )
    return message


def _update_moonraker_config(ctx: Context, runner: CommandRunner) -> None:
    """
    Add the update manager block to moonraker.conf and restart Moonraker if it was added.

    A missing moonraker.conf is skipped quietly.

    :param ctx: installer context
    :param runner: command runner
    """
    if not ctx.moonraker_config_file.exists():
        return
    block = moonraker_update_block(
        str(ctx.afc_path), ctx.gitrepo, ctx.klipper_service, str(ctx.home)
    )
    if ensure_moonraker_update_block(ctx.moonraker_config_file, block):
        restart_service(runner, "moonraker")


def install_afc(
    ctx: Context,
    console: Console,
    prompter: Prompter,
    runner: CommandRunner,
    options: InstallOptions,
    euid: Optional[int] = None,
    oem_dir: Path = Path("/oem"),
) -> str:
    """
    Install the first unit: copy configs, link extensions and apply every selected option.

    On a Snapmaker U1 it also keeps `.lite` backups of Klipper's originals, uses the U1
    config, comments out fluidd's `T<n>` macros and writes `/oem/.debug`.

    :param ctx: installer context, `files_updated_or_installed` is set
    :param console: where messages go
    :param prompter: used for overwrite and pin prompts
    :param runner: command runner (restarting Moonraker)
    :param options: unit choices and toggles
    :param euid: effective user id, defaults to the current process's (Snapmaker only)
    :param oem_dir: location of the oem directory (Snapmaker only)
    :return str: the install message
    :raises ValueError: for an installation type that is not registered
    """
    sel = options.selection
    unit = unit_for(sel.installation_type)
    if unit is None:
        # an unregistered type would only fail later, editing a hardware file nothing copied
        error_str = f"Unknown installation type '{sel.installation_type}'"
        raise ValueError(error_str)
    sel.is_additional_unit = False
    if ctx.is_snapmaker:
        check_and_move_lite_files(ctx, console)
        copy_snapmaker_config(ctx, console, prompter)
        comment_gcode_in_fluidd(ctx, console, comment=True)
    else:
        copy_config(ctx, console, prompter)
    link_extensions(ctx)
    unit.copy_files(ctx, console, prompter, sel)
    apply_unit_extruder(ctx, console, prompter, sel)
    hardware = ctx.afc_config_dir / "AFC_Hardware.cfg"
    if not options.toggles["tip_cut_servo"]:
        remove_tip_cut_servo_section(hardware)
    if ctx.git_install:
        if not ctx.test_mode:
            exclude_from_klipper_git(ctx, console)
    else:
        console.print_msg("INFO", "Skipping exclude from klipper git for git installations.")
    if options.toggles["afc_includes"]:
        manage_include(ctx.printer_config_dir / "printer.cfg", "add")
    for toggle in TOGGLES:
        if toggle.config_key is not None:
            set_value(ctx.afc_file, toggle.config_key, str(options.toggles[toggle.key]))
    if sel.toolhead_sensor == "Sensor":
        set_tool_start_pin(hardware, sel.toolhead_sensor_pin)
    elif sel.toolhead_sensor == "Ramming":
        ramming_pin = "AMS_extruder" if sel.installation_type == "OpenAMS" else "buffer"
        set_tool_start_pin(hardware, ramming_pin)
    apply_boxturtle_rename(ctx, sel)
    apply_unit_buffer(ctx, console, prompter, sel, sel.buffer_type)
    ensure_prep_section(ctx.afc_file)
    replace_varfile_path(ctx.afc_file, ctx.afc_config_dir)
    if ctx.git_install and not ctx.is_snapmaker:
        _update_moonraker_config(ctx, runner)
    if ctx.is_snapmaker:
        u1_write_debug_file(console, runner, euid, oem_dir)
    ctx.files_updated_or_installed = True
    return build_install_message(ctx, sel)
