# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The ordered list of unit types and the rules that span all of them."""
from __future__ import annotations

from typing import List, Optional, Tuple

from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.unit_registry.base import GENERIC_BUFFER_OPTIONS, UnitType
from installer.unit_registry.naming import ask_for_name, lowest_free_name
from installer.unit_registry.selection import UnitSelection
from installer.unit_registry.units.ace import Ace
from installer.unit_registry.units.boxturtle import BoxTurtle4Lane, BoxTurtle8Lane
from installer.unit_registry.units.claymore import Claymore
from installer.unit_registry.units.emu import Emu
from installer.unit_registry.units.htlf import Htlf
from installer.unit_registry.units.nightowl import NightOwl
from installer.unit_registry.units.openams import OpenAms
from installer.unit_registry.units.quattrobox import QuattroBox
from installer.unit_registry.units.vivid import Vivid

# Menu order. Adding a unit type means writing its class and adding it here.
UNIT_TYPES: List[UnitType] = [
    BoxTurtle4Lane(),
    BoxTurtle8Lane(),
    NightOwl(),
    Htlf(),
    Claymore(),
    QuattroBox(),
    OpenAms(),
    Ace(),
    Vivid(),
    Emu(),
]

_GENERIC_DEFAULT_BUFFER = "TurtleNeck"


def unit_for(installation_type: str) -> Optional[UnitType]:
    """
    Look up a unit type by its display name.

    :param installation_type: display name, e.g. `BoxTurtle (4-Lane)`
    :return Optional[UnitType]: the unit, or None for an unknown type
    """
    for unit in UNIT_TYPES:
        if unit.display_name == installation_type:
            return unit
    return None


def additional_buffer_supported(installation_type: str) -> bool:
    """
    Can this type get a buffer through the "add additional unit" menu?

    :param installation_type: display name
    :return bool: True if supported; unknown types are not
    """
    unit = unit_for(installation_type)
    return unit is not None and unit.additional_buffer_supported


def additional_buffer_options(installation_type: str) -> Tuple[str, ...]:
    """
    The buffer choices the additional-unit menu cycles through for a type.

    :param installation_type: display name
    :return Tuple[str, ...]: the options (the generic cycle for unknown types)
    """
    unit = unit_for(installation_type)
    return unit.additional_buffer_options if unit is not None else GENERIC_BUFFER_OPTIONS


def additional_buffer_default(installation_type: str) -> str:
    """
    The "own buffer" choice an additional unit of this type starts with.

    :param installation_type: display name
    :return str: the buffer type
    """
    unit = unit_for(installation_type)
    return unit.additional_buffer_default if unit is not None else _GENERIC_DEFAULT_BUFFER


def force_additional_buffer_default(installation_type: str) -> bool:
    """
    Should switching into this type always reset the buffer to its own default?

    :param installation_type: display name
    :return bool: True to force the reset even when the incoming buffer_type would
        otherwise carry over; unknown types do not force it
    """
    unit = unit_for(installation_type)
    return unit is not None and unit.force_additional_buffer_default


def additional_default_name(ctx: Context, sel: UnitSelection) -> str:
    """
    The default name for an additional unit of the selected type.

    :param ctx: installer context
    :param sel: the user's unit choices
    :return str: the lowest free `<prefix>_N` name
    """
    return lowest_free_name(ctx, sel, unit_for(sel.installation_type))


def name_additional_unit(
    ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
) -> str:
    """
    Ask for an additional unit's name, offering the lowest free default for its type.

    :param ctx: installer context
    :param console: where messages go
    :param prompter: where answers come from
    :param sel: the user's unit choices, `unit_name` and `turtle_renamed` are updated
    :return str: the status message for the menu
    """
    default = additional_default_name(ctx, sel)
    prompt = f"Enter name for unit (Default: {default}): "
    sel.unit_name = ask_for_name(console, prompter, prompt, default)
    sel.turtle_renamed = True
    return f"Naming unit {sel.unit_name}"
