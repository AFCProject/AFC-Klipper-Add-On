# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The user's current unit choices."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UnitSelection:
    """Everything the menus let the user pick for a unit."""

    installation_type: str = "BoxTurtle (4-Lane)"
    unit_name: str = "Turtle_1"
    turtle_renamed: bool = False
    is_additional_unit: bool = False
    htlf_board_type: str = "ERB"
    nightowl_board_type: str = "ERB_2.0"
    claymore_board_type: str = "AFC_Lite"
    qb_board_type: str = "MMB_2.0"
    qb_motor_type: str = "NEMA_14"
    emu_num_lanes: int = 5
    emu_board_type: str = "EBB"
    buffer_type: str = "Unknown"
    additional_buffer_type: str = "None"
    toolhead_sensor: str = "Ramming"
    toolhead_sensor_pin: str = "Unknown"
