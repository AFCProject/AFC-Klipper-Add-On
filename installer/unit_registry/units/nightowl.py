# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""NightOwl."""
from __future__ import annotations

from typing import List

from installer.config_editor.edits import replace_text
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.fsops import safe_copy
from installer.unit_registry.base import BufferTarget, UnitType
from installer.unit_registry.options import (
    NIGHTOWL_BOARD_TYPES,
    ChoiceOption,
    NameOption,
    UnitOption,
)
from installer.unit_registry.selection import UnitSelection

_PLACEHOLDER = "AFC_NIGHTOWL_CLASS_PLACEHOLDER"


def template_for_board(board: str) -> str:
    """
    Template file for a board: ERB_2.0 keeps the plain name, others add the board.

    :param board: NightOwl board type
    :return str: template file name
    """
    if board == "ERB_2.0":
        return "AFC_NightOwl_1.cfg"
    return f"AFC_NightOwl_1-{board}.cfg"


class NightOwl(UnitType):
    """The NightOwl unit, available on two boards."""

    display_name = "NightOwl"
    key = "NightOwl"
    additional_name_prefix = "NightOwl"
    additional_buffer_supported = True

    def copy_files(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the board's mcu file, the hardware file and the unit file (fixed name).

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        board = sel.nightowl_board_type
        mcu = f"{board}.cfg"
        safe_copy(ctx, console, prompter, ctx.afc_path / "config" / "mcu" / mcu, cfg / "mcu" / mcu)
        hardware = ctx.afc_path / "templates" / "AFC_Hardware-NightOwl.cfg"
        safe_copy(ctx, console, prompter, hardware, cfg / "AFC_Hardware.cfg")
        template = ctx.afc_path / "templates" / template_for_board(board)
        safe_copy(ctx, console, prompter, template, cfg / "AFC_NightOwl_1.cfg")

    def install_additional(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the unit and a per-unit mcu file, then rename everything but the class name.

        The class name ``AFC_NightOwl`` is parked behind a placeholder while the unit
        name is substituted for ``NightOwl``.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        name = sel.unit_name
        board = sel.nightowl_board_type
        unit_file = cfg / f"AFC_{name}.cfg"
        template = ctx.afc_path / "templates" / template_for_board(board)
        safe_copy(ctx, console, prompter, template, unit_file)
        mcu_name = f"{board}_{name}.cfg"
        mcu_file = cfg / "mcu" / mcu_name
        mcu_source = ctx.afc_path / "config" / "mcu" / f"{board}.cfg"
        safe_copy(ctx, console, prompter, mcu_source, mcu_file)
        replace_text(mcu_file, "NightOwl", name)
        replace_text(unit_file, "AFC_NightOwl", _PLACEHOLDER)
        replace_text(unit_file, "NightOwl", name)
        replace_text(unit_file, _PLACEHOLDER, "AFC_NightOwl")
        replace_text(unit_file, f"[include mcu/{board}.cfg]", f"[include mcu/{mcu_name}]")

    def buffer_target(self, ctx: Context, sel: UnitSelection) -> BufferTarget:
        """
        The fixed first unit, or the named additional unit.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return BufferTarget: where the buffer goes
        """
        if sel.is_additional_unit:
            return BufferTarget(
                sel.unit_name,
                "AFC_NightOwl",
                ctx.afc_config_dir / f"AFC_{sel.unit_name}.cfg",
                sel.unit_name,
            )
        return BufferTarget(
            "NightOwl", "AFC_NightOwl", ctx.afc_config_dir / "AFC_NightOwl_1.cfg", "NightOwl"
        )

    def message(self, ctx: Context, sel: UnitSelection) -> str:
        """
        Remind the user to enter CAN bus or serial information.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return str: the message
        """
        return (
            "\n- Ensure you enter either your CAN bus or serial information in the "
            f"{ctx.afc_config_dir}/AFC_NightOwl_1.cfg file\n  "
        )

    def install_options(self) -> List[UnitOption]:
        """
        The board option of the install screen.

        :return List[UnitOption]: the options
        """
        return [ChoiceOption("NightOwl Board Type", "nightowl_board_type", NIGHTOWL_BOARD_TYPES)]

    def additional_options(self) -> List[UnitOption]:
        """
        The name and board options of the additional-unit screen.

        :return List[UnitOption]: the options
        """
        return [
            NameOption("NightOwl Name", additional_prompt=True),
            ChoiceOption("NightOwl Board Type", "nightowl_board_type", NIGHTOWL_BOARD_TYPES),
        ]
