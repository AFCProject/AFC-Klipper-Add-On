# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""QIDI Box."""
from __future__ import annotations

from pathlib import Path
from typing import List

from installer.config_editor.edits import replace_text
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.fsops import safe_copy
from installer.unit_registry.base import BufferTarget, UnitType
from installer.unit_registry.options import (
    QIDI_FIRMWARE_TYPES,
    ChoiceOption,
    NameOption,
    UnitOption,
)
from installer.unit_registry.selection import UnitSelection

_TEMPLATE = "AFC_QidiBox_1.cfg"
_MCU = "QidiBox.cfg"
_KLIPPER_SERIAL = "serial: /dev/serial/by-id/usb-Klipper_stm32f401xc_<insert id>-if00"
_STOCK_SERIAL = (
    "serial: /dev/serial/by-id/usb-Klipper_QIDI_BOX_V2_<insert version and id>-if00"
)
_SOFTWARE_I2C = (
    "i2c_software_scl_pin: QidiBox_1:AHT_SCL",
    "i2c_software_sda_pin: QidiBox_1:AHT_SDA",
)
_HARDWARE_I2C = "i2c_bus: i2c3"


def _apply_firmware(sel: UnitSelection, unit_file: Path, mcu_file: Path) -> None:
    """
    Switch freshly copied files to the stock QIDI firmware when that was chosen: its serial
    path and the hardware I2C bus for the dryer air sensor. Runs before any renaming.

    :param sel: the user's unit choices
    :param unit_file: the copied unit template
    :param mcu_file: the copied mcu file
    """
    if sel.qidi_firmware != "Stock":
        return
    replace_text(mcu_file, _KLIPPER_SERIAL, f"#{_KLIPPER_SERIAL}")
    replace_text(mcu_file, f"#{_STOCK_SERIAL}", _STOCK_SERIAL)
    for line in _SOFTWARE_I2C:
        replace_text(unit_file, line, f"#{line}")
    replace_text(unit_file, f"#{_HARDWARE_I2C}", _HARDWARE_I2C)


class QidiBox(UnitType):
    """QIDI Box: a USB Klipper MCU with four slot steppers and a dryer, one mcu file per box."""

    display_name = "QIDI Box"
    key = "QidiBox"
    additional_name_prefix = "QidiBox"
    additional_buffer_supported = True

    def copy_files(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the generic hardware file, the fixed-name unit file and the box's mcu file.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        templates = ctx.afc_path / "templates"
        (cfg / "mcu").mkdir(parents=True, exist_ok=True)
        safe_copy(ctx, console, prompter, templates / "AFC_Hardware-AFC.cfg",
                  cfg / "AFC_Hardware.cfg")
        safe_copy(ctx, console, prompter, templates / _TEMPLATE, cfg / _TEMPLATE)
        safe_copy(ctx, console, prompter, ctx.afc_path / "config" / "mcu" / _MCU,
                  cfg / "mcu" / "QidiBox_1.cfg")
        _apply_firmware(sel, cfg / _TEMPLATE, cfg / "mcu" / "QidiBox_1.cfg")

    def install_additional(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the unit and a same-named mcu file, renaming QidiBox_1 in both.

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
        safe_copy(ctx, console, prompter, ctx.afc_path / "templates" / _TEMPLATE, unit_file)
        safe_copy(ctx, console, prompter, ctx.afc_path / "config" / "mcu" / _MCU, mcu_file)
        _apply_firmware(sel, unit_file, mcu_file)
        replace_text(unit_file, "QidiBox_1", name)
        replace_text(mcu_file, "QidiBox_1", name)

    def buffer_target(self, ctx: Context, sel: UnitSelection) -> BufferTarget:
        """
        The unit's own file and section.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return BufferTarget: where the buffer goes
        """
        name = sel.unit_name if sel.is_additional_unit else "QidiBox_1"
        return BufferTarget(name, "AFC_QidiBox", ctx.afc_config_dir / f"AFC_{name}.cfg", name)

    def message(self, ctx: Context, sel: UnitSelection) -> str:
        """
        Remind the user about the serial id, the hub/direct mode and calibration.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return str: the message
        """
        cfg = ctx.afc_config_dir
        return (
            f"\n- Ensure you enter the box's serial id in the {cfg}/mcu/QidiBox_1.cfg file\n\n"
            f"- Review the hub or direct mode settings and sensor pins in the "
            f"{cfg}/AFC_QidiBox_1.cfg file, then run AFC_CALIBRATION.\n  "
        )

    def install_options(self) -> List[UnitOption]:
        """
        The firmware option of the install screen.

        :return List[UnitOption]: the options
        """
        return [ChoiceOption("QIDI Box Firmware", "qidi_firmware", QIDI_FIRMWARE_TYPES)]

    def additional_options(self) -> List[UnitOption]:
        """
        The name and firmware options of the additional-unit screen.

        :return List[UnitOption]: the options
        """
        return [
            NameOption("QIDI Box Name", additional_prompt=True),
            ChoiceOption("QIDI Box Firmware", "qidi_firmware", QIDI_FIRMWARE_TYPES),
        ]
