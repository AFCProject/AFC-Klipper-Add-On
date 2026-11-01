# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""ViViD."""
from __future__ import annotations

from typing import List

from installer.config_editor.edits import replace_text
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.fsops import safe_copy
from installer.unit_registry.base import BufferTarget, UnitType
from installer.unit_registry.options import NameOption, UnitOption
from installer.unit_registry.selection import UnitSelection


class Vivid(UnitType):
    """ViViD. It has no buffer target, so a PSF buffer is skipped with a warning."""

    display_name = "ViViD"
    key = "ViViD"
    buffer_selectable = False
    additional_name_prefix = "Vivid"
    additional_buffer_supported = False

    def copy_files(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the fixed-name unit file, the hardware file and the mcu file.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        templates = ctx.afc_path / "templates"
        (cfg / "mcu").mkdir(parents=True, exist_ok=True)
        safe_copy(ctx, console, prompter, templates / "AFC_Vivid_1.cfg", cfg / "AFC_Vivid_1.cfg")
        hardware = templates / "AFC_Hardware-AFC.cfg"
        safe_copy(ctx, console, prompter, hardware, cfg / "AFC_Hardware.cfg")
        mcu = ctx.afc_path / "config" / "mcu" / "Vivid.cfg"
        safe_copy(ctx, console, prompter, mcu, cfg / "mcu" / "Vivid_1.cfg")

    def install_additional(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the unit and a same-named mcu file, renaming Vivid_1 in both.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        name = sel.unit_name
        (cfg / "mcu").mkdir(parents=True, exist_ok=True)
        unit_file = cfg / f"AFC_{name}.cfg"
        mcu_file = cfg / "mcu" / f"{name}.cfg"
        safe_copy(ctx, console, prompter, ctx.afc_path / "templates" / "AFC_Vivid_1.cfg", unit_file)
        safe_copy(ctx, console, prompter, ctx.afc_path / "config" / "mcu" / "Vivid.cfg", mcu_file)
        replace_text(unit_file, "Vivid_1", name)
        replace_text(unit_file, "include mcu/Vivid_1.cfg", f"include mcu/{name}.cfg")
        replace_text(mcu_file, "Vivid_1", name)

    def buffer_target(self, ctx: Context, sel: UnitSelection) -> BufferTarget:
        """
        The unit's own file and section.

        ViViD has no buffer choice (`buffer_selectable` is False), so this is only
        ever used to wire up the selected extruder.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return BufferTarget: where the extruder option goes
        """
        name = sel.unit_name if sel.is_additional_unit else "Vivid_1"
        return BufferTarget(
            unit_name=name,
            unit_section_prefix="AFC_vivid",
            extruder_file=ctx.afc_config_dir / f"AFC_{name}.cfg",
            section_name=name,
        )

    def message(self, ctx: Context, sel: UnitSelection) -> str:
        """
        Remind the user about serial information and the buffer pins.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return str: the message
        """
        unit_file = f"{ctx.afc_config_dir}/AFC_Vivid_1.cfg"
        return (
            f"\n- Ensure you enter your serial information in the {unit_file} file\n\n"
            f"- Review the {unit_file} file to reference the proper buffer configuration "
            "and pins.\n  "
        )

    def additional_options(self) -> List[UnitOption]:
        """
        The name option of the additional-unit screen.

        :return List[UnitOption]: the options
        """
        return [NameOption("ViViD Name", additional_prompt=True)]

    def on_select(self, sel: UnitSelection) -> None:
        """
        ViViD has no buffer choice by default.

        :param sel: the user's unit choices, `buffer_type` is cleared
        """
        sel.buffer_type = "None"

    def after_sensor_change(self, sel: UnitSelection) -> str:
        """
        Only a sensor with no buffer chosen yet changes it; ramming leaves the buffer alone.

        :param sel: the user's unit choices, `buffer_type` may change
        :return str: the status message
        """
        if sel.toolhead_sensor == "Sensor":
            if sel.buffer_type == "Unknown":
                sel.buffer_type = "None"
            return "Using toolhead sensor"
        return f"Using ramming with {self.ramming_hardware}"
