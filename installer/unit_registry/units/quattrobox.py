# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""QuattroBox."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from installer.config_editor.edits import replace_text
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.fsops import safe_copy
from installer.unit_registry.base import BufferTarget, UnitType
from installer.unit_registry.options import (
    QB_BOARD_TYPES,
    QB_MOTOR_TYPES,
    ChoiceOption,
    NameOption,
    UnitOption,
)
from installer.unit_registry.selection import UnitSelection

_MOTOR_TEMPLATES = {"NEMA_14": "AFC_QuattroBox_14.cfg", "NEMA_17": "AFC_QuattroBox_17.cfg"}
_BOARDS = ("MMB_1.0", "MMB_1.1", "MMB_2.0")
_EJECT = "Eject_buttons.cfg"


class QuattroBox(UnitType):
    """The QuattroBox unit; its template depends on the motor, its mcu file on the board."""

    display_name = "QuattroBox"
    key = "QuattroBox"
    additional_name_prefix = "QuattroBox"
    additional_buffer_supported = True

    def _copy_eject_macros(
        self, ctx: Context, console: Console, prompter: Prompter
    ) -> Path:
        macros = ctx.afc_config_dir / "macros"
        macros.mkdir(parents=True, exist_ok=True)
        source = ctx.afc_path / "templates" / "qb_macros" / _EJECT
        safe_copy(ctx, console, prompter, source, macros / _EJECT)
        return macros / _EJECT

    def copy_files(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy hardware, eject macros, the motor's unit template and the board's mcu file.

        An unknown motor or board copies only the parts it can.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        hardware = ctx.afc_path / "templates" / "AFC_Hardware-QuattroBox.cfg"
        safe_copy(ctx, console, prompter, hardware, cfg / "AFC_Hardware.cfg")
        self._copy_eject_macros(ctx, console, prompter)
        template = _MOTOR_TEMPLATES.get(sel.qb_motor_type)
        if template is None:
            return
        unit_file = cfg / "AFC_QuattroBox_1.cfg"
        safe_copy(ctx, console, prompter, ctx.afc_path / "templates" / template, unit_file)
        board = sel.qb_board_type
        if board in _BOARDS:
            mcu = f"{board}_QB.cfg"
            (cfg / "mcu").mkdir(parents=True, exist_ok=True)
            source = ctx.afc_path / "config" / "mcu" / mcu
            safe_copy(ctx, console, prompter, source, cfg / "mcu" / mcu)
            replace_text(unit_file, "include mcu/MMB_QB.cfg", f"include mcu/{mcu}")

    def install_additional(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the unit, a per-unit mcu file and the eject macros, renaming QuattroBox_1.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        name = sel.unit_name
        (cfg / "mcu").mkdir(parents=True, exist_ok=True)
        eject = self._copy_eject_macros(ctx, console, prompter)
        replace_text(eject, "QuattroBox_1", name)
        template = _MOTOR_TEMPLATES.get(sel.qb_motor_type)
        if template is None:
            return
        unit_file = cfg / f"AFC_{name}.cfg"
        safe_copy(ctx, console, prompter, ctx.afc_path / "templates" / template, unit_file)
        board = sel.qb_board_type
        if board in _BOARDS:
            mcu_name = f"{board}_QB_{name}.cfg"
            replace_text(unit_file, "include mcu/MMB_QB.cfg", f"include mcu/{mcu_name}")
            source = ctx.afc_path / "config" / "mcu" / f"{board}_QB.cfg"
            safe_copy(ctx, console, prompter, source, cfg / "mcu" / mcu_name)
            replace_text(cfg / "mcu" / mcu_name, "mcu: QuattroBox_1", f"mcu: {name}")
        replace_text(unit_file, "QuattroBox_1", name)

    def buffer_target(self, ctx: Context, sel: UnitSelection) -> Optional[BufferTarget]:
        """
        The unit's file and the buffer section already present in its template.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return Optional[BufferTarget]: where the buffer goes
        """
        name = sel.unit_name if sel.is_additional_unit else "QuattroBox_1"
        return BufferTarget(
            name,
            "AFC_QuattroBox",
            ctx.afc_config_dir / f"AFC_{name}.cfg",
            name,
            f"[AFC_buffer {name}]",
        )

    def message(self, ctx: Context, sel: UnitSelection) -> str:
        """
        Remind the user to set up the buffer pins and the connection.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return str: the message
        """
        unit_file = f"{ctx.afc_config_dir}/AFC_QuattroBox_1.cfg"
        return (
            f"\n- You must update the {unit_file} file to reference the proper buffer "
            "configuration and pins.\n\n"
            f"- Ensure you enter either your CAN bus or serial information in the {unit_file} "
            "file\n  "
        )

    def _hardware_options(self) -> List[UnitOption]:
        return [
            ChoiceOption("QuattroBox Board Type", "qb_board_type", QB_BOARD_TYPES),
            ChoiceOption("QuattroBox Motor Type", "qb_motor_type", QB_MOTOR_TYPES),
        ]

    def install_options(self) -> List[UnitOption]:
        """
        The board and motor options of the install screen.

        :return List[UnitOption]: the options
        """
        return self._hardware_options()

    def additional_options(self) -> List[UnitOption]:
        """
        The name, board and motor options of the additional-unit screen.

        :return List[UnitOption]: the options
        """
        return [NameOption("QuattroBox Name", additional_prompt=True), *self._hardware_options()]
