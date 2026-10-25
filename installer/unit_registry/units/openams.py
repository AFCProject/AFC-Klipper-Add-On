# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""OpenAMS."""
from __future__ import annotations

from typing import List, Tuple

from installer.config_editor.edits import replace_text
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.fsops import safe_copy
from installer.unit_registry.base import BufferTarget, UnitType
from installer.unit_registry.options import NameOption, UnitOption
from installer.unit_registry.selection import UnitSelection


class OpenAms(UnitType):
    """OpenAMS; it has no TurtleNeck hardware, so only an FPS buffer or none."""

    display_name = "OpenAMS"
    key = "OpenAMS"
    ramming_hardware = "an FPS Board"
    buffer_selectable = False
    additional_name_prefix = "AMS"
    additional_buffer_supported = True
    additional_buffer_options: Tuple[str, ...] = ("None", "FPS_PSF")
    additional_buffer_default = "FPS_PSF"

    def copy_files(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the hardware file and the fixed-name AMS unit file.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        templates = ctx.afc_path / "templates"
        cfg = ctx.afc_config_dir
        hardware = templates / "AFC_Hardware-AFC.cfg"
        safe_copy(ctx, console, prompter, hardware, cfg / "AFC_Hardware.cfg")
        safe_copy(ctx, console, prompter, templates / "AFC_AMS_1.cfg", cfg / "AFC_AMS_1.cfg")

    def install_additional(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the AMS unit file under its new name and rename AMS_1 inside it.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        cfg.joinpath("macros").mkdir(parents=True, exist_ok=True)
        unit_file = cfg / f"AFC_{sel.unit_name}.cfg"
        safe_copy(ctx, console, prompter, ctx.afc_path / "templates" / "AFC_AMS_1.cfg", unit_file)
        replace_text(unit_file, "AMS_1", sel.unit_name)

    def buffer_target(self, ctx: Context, sel: UnitSelection) -> BufferTarget:
        """
        The fixed first unit, or the named additional unit.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return BufferTarget: where the buffer goes
        """
        name = sel.unit_name if sel.is_additional_unit else "AMS_1"
        return BufferTarget(name, "AFC_OpenAMS", ctx.afc_config_dir / f"AFC_{name}.cfg", name)

    def message(self, ctx: Context, sel: UnitSelection) -> str:
        """
        Point the user at the AMS settings and the OpenAMS setup.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return str: the message
        """
        return (
            f"\n- Review and update the {ctx.afc_config_dir}/AFC_AMS_1.cfg file for your AMS "
            "unit settings.\n\n"
            "- Ensure OpenAMS is properly installed and configured per their instructions.\n  "
        )

    def additional_options(self) -> List[UnitOption]:
        """
        The name option of the additional-unit screen.

        :return List[UnitOption]: the options
        """
        return [NameOption("OpenAMS Name", additional_prompt=True)]

    def on_select(self, sel: UnitSelection) -> None:
        """
        OpenAMS has no buffer choice: it follows ramming (FPS_PSF) or a sensor (none).

        :param sel: the user's unit choices, ``buffer_type`` is set
        """
        sel.buffer_type = "FPS_PSF" if sel.toolhead_sensor == "Ramming" else "None"

    def after_sensor_change(self, sel: UnitSelection) -> str:
        """
        Set the buffer from the sensor / ramming choice.

        :param sel: the user's unit choices, ``buffer_type`` is set
        :return str: the status message
        """
        if sel.toolhead_sensor == "Sensor":
            sel.buffer_type = "None"
            return "Using toolhead sensor"
        sel.buffer_type = "FPS_PSF"
        return f"Using ramming with {self.ramming_hardware}"
