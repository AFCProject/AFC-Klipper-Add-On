# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Pieces shared by the two options screens: numbered entries, labels and unit lookup."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from installer.ui_menus.view import Row
from installer.unit_registry.base import UnitType
from installer.unit_registry.registry import unit_for


_BUFFER_LABELS = {
    "TurtleNeck2Remastered": "TurtleNeck 2 Remastered",
    "FPS_PSF": "Proportional Sync-Feedback(PSF)",
    "Internal": "Internal buffer",
}


@dataclass
class Entry:
    """One numbered row: how to draw it and what choosing it does."""

    row: Callable[[int], Row]
    activate: Callable[[], str]


def buffer_label(buffer_type: str) -> str:
    """
    The menu label for a buffer type token.

    :param buffer_type: internal token
    :return str: user facing label
    """
    return _BUFFER_LABELS.get(buffer_type, buffer_type)


def require_unit(installation_type: str) -> UnitType:
    """
    The registered unit type for an installation type the menus already validated.

    :param installation_type: display name of the selected type
    :return UnitType: the unit type
    :raises ValueError: if no unit type is registered under that name
    """
    unit = unit_for(installation_type)
    if unit is None:
        error_str = f"Unknown installation type '{installation_type}'"
        raise ValueError(error_str)
    return unit
