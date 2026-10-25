# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""BoxTurtle 4-Lane and 8-Lane."""
from __future__ import annotations

from typing import List

from installer.config_editor.edits import replace_text
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.fsops import safe_copy
from installer.unit_registry.base import BufferTarget, UnitType
from installer.unit_registry.options import NameOption, UnitOption
from installer.unit_registry.selection import UnitSelection


class _BoxTurtle(UnitType):
    """Behavior shared by both BoxTurtle sizes; they differ only in two file names."""

    additional_name_prefix = "Turtle"
    additional_buffer_supported = True
    mcu_name: str
    template: str

    def copy_files(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the mcu file, the shared hardware file and the named unit file.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        mcu = f"{self.mcu_name}.cfg"
        safe_copy(ctx, console, prompter, ctx.afc_path / "config" / "mcu" / mcu, cfg / "mcu" / mcu)
        hardware = ctx.afc_path / "templates" / "AFC_Hardware-AFC.cfg"
        safe_copy(ctx, console, prompter, hardware, cfg / "AFC_Hardware.cfg")
        unit_template = ctx.afc_path / "templates" / self.template
        safe_copy(ctx, console, prompter, unit_template, cfg / f"AFC_{sel.unit_name}.cfg")

    def install_additional(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the unit file under its new name with its own mcu file.

        The edits run even if the user chose to keep an existing file.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        name = sel.unit_name
        unit_file = cfg / f"AFC_{name}.cfg"
        unit_template = ctx.afc_path / "templates" / self.template
        safe_copy(ctx, console, prompter, unit_template, unit_file)
        replace_text(unit_file, "Turtle_1", name)
        mcu = ctx.afc_path / "config" / "mcu" / f"{self.mcu_name}.cfg"
        safe_copy(ctx, console, prompter, mcu, cfg / "mcu" / f"AFC_{name}_mcu.cfg")
        replace_text(
            unit_file, f"include mcu/{self.mcu_name}.cfg", f"include mcu/AFC_{name}_mcu.cfg"
        )

    def buffer_target(self, ctx: Context, sel: UnitSelection) -> BufferTarget:
        """
        The unit's own file and section.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return BufferTarget: where the buffer goes
        """
        return BufferTarget(
            unit_name=sel.unit_name,
            unit_section_prefix="AFC_BoxTurtle",
            extruder_file=ctx.afc_config_dir / f"AFC_{sel.unit_name}.cfg",
            section_name=sel.unit_name,
        )

    def message(self, ctx: Context, sel: UnitSelection) -> str:
        """
        Remind the user to enter CAN bus or serial information.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return str: the message, indented to line up with the install message
        """
        return (
            "\n- Ensure you enter either your CAN bus or serial information in the "
            f"{ctx.afc_config_dir}/AFC_{sel.unit_name}.cfg file\n  "
        )

    def install_options(self) -> List[UnitOption]:
        """
        The name option of the install screen.

        :return List[UnitOption]: the options
        """
        return [NameOption("BoxTurtle Name")]

    def additional_options(self) -> List[UnitOption]:
        """
        The name option of the additional-unit screen.

        :return List[UnitOption]: the options
        """
        return [NameOption("BoxTurtle Name", additional_prompt=True)]





class BoxTurtle4Lane(_BoxTurtle):
    """BoxTurtle with four lanes."""

    display_name = "BoxTurtle (4-Lane)"
    key = "BoxTurtle_4Lane"
    mcu_name = "AFC_Lite"
    template = "AFC_Turtle_1.cfg"


class BoxTurtle8Lane(_BoxTurtle):
    """BoxTurtle with eight lanes."""

    display_name = "BoxTurtle (8-Lane)"
    key = "BoxTurtle_8Lane"
    mcu_name = "AFC_Pro"
    template = "AFC_Pro_Turtle_1.cfg"
