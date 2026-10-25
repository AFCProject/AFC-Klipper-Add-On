# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""EMU."""
from __future__ import annotations

from typing import List

from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.fsops import safe_copy
from installer.unit_registry.base import BufferTarget, UnitType
from installer.unit_registry.emu import generate_emu_config
from installer.unit_registry.options import (
    EMU_BOARD_TYPES,
    EMU_LANE_OPTIONS,
    ChoiceOption,
    NameOption,
    UnitOption,
)
from installer.unit_registry.selection import UnitSelection


class Emu(UnitType):
    """The EMU unit, whose config is generated for any number of lanes."""

    display_name = "EMU"
    key = "EMU"
    additional_name_prefix = "EMU"
    additional_buffer_supported = True

    def copy_files(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the hardware file and generate the unit and mcu files.

        A name still at the generic install default ``Turtle_1`` becomes ``EMU_1`` (this
        updates the selection). The generated files replace existing ones without asking.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when the hardware file already exists
        :param sel: the user's unit choices, modified in place
        """
        if sel.unit_name == "Turtle_1":
            sel.unit_name = "EMU_1"
        hardware = ctx.afc_path / "templates" / "AFC_Hardware-AFC.cfg"
        safe_copy(ctx, console, prompter, hardware, ctx.afc_config_dir / "AFC_Hardware.cfg")
        generate_emu_config(ctx, sel.unit_name, sel.emu_num_lanes, sel.emu_board_type)

    def install_additional(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Generate the unit and mcu files for an additional EMU.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: unused, generated files are written without asking
        :param sel: the user's unit choices
        """
        generate_emu_config(ctx, sel.unit_name, sel.emu_num_lanes, sel.emu_board_type)

    def buffer_target(self, ctx: Context, sel: UnitSelection) -> BufferTarget:
        """
        The unit file and the ``<name>_buffer`` section the generator already wrote.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return BufferTarget: where the buffer goes
        """
        buffer_name = f"{sel.unit_name}_buffer"
        return BufferTarget(
            sel.unit_name,
            "AFC_EMU",
            ctx.afc_config_dir / f"AFC_{sel.unit_name}.cfg",
            buffer_name,
            f"[AFC_buffer {buffer_name}]",
        )

    def message(self, ctx: Context, sel: UnitSelection) -> str:
        """
        Point the user at the per-lane connection settings and the board pins file.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return str: the message
        """
        cfg = ctx.afc_config_dir
        return (
            "\n- Ensure you enter either your CAN bus or serial information for each lane in "
            f"the {cfg}/AFC_{sel.unit_name}.cfg file\n\n"
            f"- The MCU board_pins configuration is at {cfg}/mcu/{sel.unit_name}.cfg\n  "
        )

    def _hardware_options(self) -> List[UnitOption]:
        return [
            ChoiceOption("EMU Lane Count", "emu_num_lanes", EMU_LANE_OPTIONS, cast=int),
            ChoiceOption("EMU Board Type", "emu_board_type", EMU_BOARD_TYPES),
        ]

    def install_options(self) -> List[UnitOption]:
        """
        The name, lane count and board options of the install screen.

        EMU names its first unit with the additional-unit prompt, which offers the lowest free name.

        :return List[UnitOption]: the options
        """
        return [NameOption("EMU Name", additional_prompt=True), *self._hardware_options()]

    def additional_options(self) -> List[UnitOption]:
        """
        The name, lane count and board options of the additional-unit screen.

        :return List[UnitOption]: the options
        """
        return [NameOption("EMU Name", additional_prompt=True), *self._hardware_options()]
