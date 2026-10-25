# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""HTLF."""
from __future__ import annotations

from typing import List

from installer.config_editor.edits import replace_text
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.fsops import safe_copy
from installer.unit_registry.base import BufferTarget, UnitType
from installer.unit_registry.options import (
    HTLF_BOARD_TYPES,
    ChoiceOption,
    NameOption,
    UnitOption,
)
from installer.unit_registry.selection import UnitSelection


def normalize_board_type(board: str) -> str:
    """
    MMB_1.0 and MMB_1.1 share one template and file name segment, ``MMB``.

    :param board: HTLF board type
    :return str: the board name used in template and unit file names
    """
    return "MMB" if board in ("MMB_1.0", "MMB_1.1") else board


class Htlf(UnitType):
    """The HTLF unit; its file names carry the board type."""

    display_name = "HTLF"
    key = "HTLF"
    additional_name_prefix = "HTLF"
    additional_buffer_supported = True

    def _unit_file_name(self, sel: UnitSelection) -> str:
        return f"AFC_{normalize_board_type(sel.htlf_board_type)}_{sel.unit_name}.cfg"

    def _copy_mcu(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        mcu = f"HTLF_{sel.htlf_board_type}.cfg"
        mcu_dir = ctx.afc_config_dir / "mcu"
        mcu_dir.mkdir(parents=True, exist_ok=True)
        source = ctx.afc_path / "config" / "mcu" / mcu
        safe_copy(ctx, console, prompter, source, mcu_dir / mcu)

    def copy_files(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the board's mcu file, the unit file named after board and unit, and hardware.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        self._copy_mcu(ctx, console, prompter, sel)
        board = normalize_board_type(sel.htlf_board_type)
        template = ctx.afc_path / "templates" / f"AFC_HTLF_1-{board}.cfg"
        safe_copy(ctx, console, prompter, template, cfg / self._unit_file_name(sel))
        hardware = ctx.afc_path / "templates" / "AFC_Hardware-HTLF.cfg"
        safe_copy(ctx, console, prompter, hardware, cfg / "AFC_Hardware.cfg")

    def install_additional(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the mcu and unit files, then rename ``HTLF_1`` to the unit name.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        self._copy_mcu(ctx, console, prompter, sel)
        board = normalize_board_type(sel.htlf_board_type)
        unit_file = ctx.afc_config_dir / self._unit_file_name(sel)
        template = ctx.afc_path / "templates" / f"AFC_HTLF_1-{board}.cfg"
        safe_copy(ctx, console, prompter, template, unit_file)
        replace_text(unit_file, "HTLF_1", sel.unit_name)

    def buffer_target(self, ctx: Context, sel: UnitSelection) -> BufferTarget:
        """
        The board-named unit file; the section is HTLF_1 for the first unit.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return BufferTarget: where the buffer goes
        """
        name = sel.unit_name if sel.is_additional_unit else "HTLF_1"
        return BufferTarget(
            name, "AFC_HTLF", ctx.afc_config_dir / self._unit_file_name(sel), name
        )

    def message(self, ctx: Context, sel: UnitSelection) -> str:
        """
        Remind the user about connection, rotation distance and buffer settings.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return str: the message
        """
        unit_file = f"{ctx.afc_config_dir}/{self._unit_file_name(sel)}"
        return (
            "\n- Ensure you enter either your CAN bus or serial information in the "
            f"{unit_file} file.\n\n"
            f"- Ensure you modify the {unit_file} file to select the proper rotation distance\n"
            "  and gear ratio for your stepper motors.\n\n"
            f"- Ensure you update any necessary buffer information in the {unit_file} file\n  "
        )

    def install_options(self) -> List[UnitOption]:
        """
        The name and board options of the install screen.

        :return List[UnitOption]: the options
        """
        return [
            NameOption("HTLF Name"),
            ChoiceOption("HTLF Board Type", "htlf_board_type", HTLF_BOARD_TYPES),
        ]

    def additional_options(self) -> List[UnitOption]:
        """
        The name and board options of the additional-unit screen.

        :return List[UnitOption]: the options
        """
        return [
            NameOption("HTLF Name", additional_prompt=True),
            ChoiceOption("HTLF Board Type", "htlf_board_type", HTLF_BOARD_TYPES),
        ]

    def additional_probe_file(self, prefix: str, number: int, sel: UnitSelection) -> str:
        """
        HTLF unit files carry the board: ``AFC_<board>_<prefix>_<n>.cfg``.

        :param prefix: name prefix
        :param number: candidate number
        :param sel: the user's unit choices
        :return str: file name to look for
        """
        return f"AFC_{normalize_board_type(sel.htlf_board_type)}_{prefix}_{number}.cfg"
