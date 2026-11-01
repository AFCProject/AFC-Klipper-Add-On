# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The UnitType interface every supported unit implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.unit_registry.options import UnitOption
from installer.unit_registry.selection import UnitSelection

GENERIC_BUFFER_OPTIONS: Tuple[str, ...] = (
    "None", "TurtleNeck", "TurtleNeckV2", "TurtleNeck2Remastered", "FPS_PSF"
)


class BufferTarget(NamedTuple):
    """Where a buffer selection is wired into a unit's config."""

    unit_name: str
    unit_section_prefix: str
    extruder_file: Path
    section_name: str
    prebaked_header: str = ""


class UnitType(ABC):
    """
    A supported unit type.

    Only `copy_files` is required. The other hooks default to "nothing to do", so a
    type only implements what it needs.
    """

    display_name: str
    key: str
    additional_name_prefix: str  # additional units are named <prefix>_<N>
    additional_buffer_supported: bool = False
    additional_buffer_options: Tuple[str, ...] = GENERIC_BUFFER_OPTIONS
    additional_buffer_default: str = "TurtleNeck"
    # When True, switching an additional unit to this type always resets its buffer to
    # additional_buffer_default.
    force_additional_buffer_default: bool = False
    buffer_selectable: bool = True  # False: the buffer follows the sensor / ramming choice
    ramming_hardware: str = "a TurtleNeck buffer"  # wording for the sensor / ramming option
    ramming_supported: bool = True  # False: always a toolhead sensor, no ramming toggle shown

    @abstractmethod
    def copy_files(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy this unit's template and mcu files for a first-time install.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """

    def install_additional(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy this unit's files when adding it as an additional unit. No-op by default.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """

    def buffer_target(self, ctx: Context, sel: UnitSelection) -> Optional[BufferTarget]:
        """
        Where a PSF style buffer should be wired in, or None if this type has no target.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return Optional[BufferTarget]: the target
        """
        return None

    def message(self, ctx: Context, sel: UnitSelection) -> str:
        """
        Follow-up instructions appended to the install message.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return str: text (empty by default)
        """
        return ""

    @property
    def default_name(self) -> str:
        """
        The name a first unit of this type starts with (`<prefix>_1`, for example `HTLF_1`).

        :return str: the default unit name
        """
        return f"{self.additional_name_prefix}_1"

    def install_options(self) -> List[UnitOption]:
        """
        The options this type adds to the first-unit install screen.

        :return List[UnitOption]: the options, in display order
        """
        return []

    def additional_options(self) -> List[UnitOption]:
        """
        The options this type adds to the additional-unit screen (the name comes first).

        :return List[UnitOption]: the options, in display order
        """
        return []

    def refresh_additional_name(self, ctx: Context, sel: UnitSelection) -> None:
        """
        Reset the unit name to the lowest free default unless the user chose a name.

        :param ctx: installer context
        :param sel: the user's unit choices, `unit_name` may be updated
        """
        from installer.unit_registry.naming import lowest_free_name

        if not sel.turtle_renamed:
            sel.unit_name = lowest_free_name(ctx, sel, self)

    def on_select(self, sel: UnitSelection) -> None:
        """
        Fix up the selection when this type is chosen. Nothing by default.

        :param sel: the user's unit choices, updated in place
        """

    def after_sensor_change(self, sel: UnitSelection) -> str:
        """
        Adjust the buffer after the toolhead sensor / ramming choice flipped.

        :param sel: the user's unit choices, `buffer_type` may change
        :return str: the status message
        """
        if sel.toolhead_sensor == "Sensor":
            if sel.buffer_type == "Unknown":
                sel.buffer_type = "None"
            return "Using toolhead sensor"
        message = f"Using ramming with {self.ramming_hardware}"
        if sel.buffer_type == "None":
            sel.buffer_type = "Unknown"
        if sel.buffer_type == "Unknown":
            message += ". Select a buffer type"
        return message

    def cycle_buffer(self, sel: UnitSelection) -> None:
        """
        Step to the next buffer type (a buffer is mandatory with ramming, optional with a sensor).

        :param sel: the user's unit choices, `buffer_type` is updated
        """
        order = {
            "TurtleNeck": "TurtleNeckV2",
            "TurtleNeckV2": "TurtleNeck2Remastered",
            "TurtleNeck2Remastered": "FPS_PSF",
        }
        if sel.buffer_type == "FPS_PSF":
            sel.buffer_type = "TurtleNeck" if sel.toolhead_sensor == "Ramming" else "None"
        else:
            sel.buffer_type = order.get(sel.buffer_type, "TurtleNeck")

    def additional_probe_file(self, prefix: str, number: int, sel: UnitSelection) -> str:
        """
        File name whose existence means unit `<prefix>_<number>` is taken.

        :param prefix: name prefix, e.g. `Turtle`
        :param number: candidate number
        :param sel: the user's unit choices
        :return str: file name inside the AFC config directory
        """
        return f"AFC_{prefix}_{number}.cfg"

    def name_prefix(self, sel: UnitSelection) -> str:
        """
        The prefix additional units of this type are named after.

        By default this is the static `additional_name_prefix`; a type whose prefix
        depends on a sub-type choice (for example ACE's V1 vs V2_Pro) overrides this.

        :param sel: the user's unit choices
        :return str: the prefix
        """
        return self.additional_name_prefix
