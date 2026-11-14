# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Claymore."""
from __future__ import annotations

from pathlib import Path
from typing import List

from installer.config_editor.edits import replace_text
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.fsops import safe_copy
from installer.unit_registry.base import BufferTarget, UnitType
from installer.unit_registry.options import (
    CLAYMORE_BOARD_TYPES,
    ChoiceOption,
    NameOption,
    UnitOption,
)
from installer.unit_registry.selection import UnitSelection

_MCU = "AFC_Lite_Claymore.cfg"


class Claymore(UnitType):
    """The Claymore unit."""

    display_name = "Claymore"
    key = "Claymore"
    additional_name_prefix = "Claymore"
    additional_buffer_supported = True

    def _template(self, ctx: Context, sel: UnitSelection) -> Path:
        return ctx.afc_path / "templates" / f"AFC_Claymore_1-{sel.claymore_board_type}.cfg"

    def _copy_mcu(self, ctx: Context, console: Console, prompter: Prompter) -> None:
        mcu_dir = ctx.afc_config_dir / "mcu"
        mcu_dir.mkdir(parents=True, exist_ok=True)
        safe_copy(ctx, console, prompter, ctx.afc_path / "config" / "mcu" / _MCU, mcu_dir / _MCU)

    def copy_files(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the mcu, unit and hardware files.

        A name still at the generic install default `Turtle_1` becomes `Claymore_1`
        (this updates the selection). A custom name is also written into the unit file.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices, modified in place
        """
        cfg = ctx.afc_config_dir
        if sel.unit_name == "Turtle_1":
            sel.unit_name = "Claymore_1"
        self._copy_mcu(ctx, console, prompter)
        unit_file = cfg / f"AFC_{sel.unit_name}.cfg"
        safe_copy(ctx, console, prompter, self._template(ctx, sel), unit_file)
        if sel.unit_name != "Claymore_1":
            replace_text(unit_file, "Claymore_1", sel.unit_name)
        hardware = ctx.afc_path / "templates" / "AFC_Hardware-HTLF.cfg"
        safe_copy(ctx, console, prompter, hardware, cfg / "AFC_Hardware.cfg")

    def install_additional(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the unit file and rename the unit and its buffer section.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        self._copy_mcu(ctx, console, prompter)
        unit_file = ctx.afc_config_dir / f"AFC_{sel.unit_name}.cfg"
        safe_copy(ctx, console, prompter, self._template(ctx, sel), unit_file)
        replace_text(unit_file, "Claymore_1", sel.unit_name)
        replace_text(unit_file, "Claymore_buffer", f"{sel.unit_name}_buffer")

    def buffer_target(self, ctx: Context, sel: UnitSelection) -> BufferTarget:
        """
        The unit file and the buffer section already present in its template.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return BufferTarget: where the buffer goes
        """
        buffer_name = f"{sel.unit_name}_buffer" if sel.is_additional_unit else "Claymore_buffer"
        return BufferTarget(
            sel.unit_name,
            "AFC_Claymore",
            ctx.afc_config_dir / f"AFC_{sel.unit_name}.cfg",
            buffer_name,
            f"[AFC_buffer {buffer_name}]",
        )

    def message(self, ctx: Context, sel: UnitSelection) -> str:
        """
        Remind the user about connection and buffer settings.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return str: the message
        """
        unit_file = f"{ctx.afc_config_dir}/AFC_{sel.unit_name}.cfg"
        return (
            "\n- Ensure you enter either your CAN bus or serial information in the "
            f"{unit_file} file.\n\n"
            f"- Ensure you update any necessary buffer information in the {unit_file} file\n  "
        )

    def install_options(self) -> List[UnitOption]:
        """
        The name and board options of the install screen.

        :return List[UnitOption]: the options
        """
        return [
            NameOption("Claymore Name"),
            ChoiceOption("Claymore Board Type", "claymore_board_type", CLAYMORE_BOARD_TYPES),
        ]

    def additional_options(self) -> List[UnitOption]:
        """
        The name and board options of the additional-unit screen.

        :return List[UnitOption]: the options
        """
        return [
            NameOption("Claymore Name", additional_prompt=True),
            ChoiceOption("Claymore Board Type", "claymore_board_type", CLAYMORE_BOARD_TYPES),
        ]
