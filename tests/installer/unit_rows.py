"""Helpers that render a unit type's option objects as the text rows the menus show."""
from typing import List

from installer.platform_core.context import Context
from installer.unit_registry.base import UnitType
from installer.unit_registry.selection import UnitSelection


def install_rows(unit: UnitType, sel: UnitSelection) -> List[str]:
    """The install screen's unit specific rows as ``label : value``."""
    return [f"{option.label} : {option.value(sel)}" for option in unit.install_options()]


def additional_rows(unit: UnitType, ctx: Context, sel: UnitSelection) -> List[str]:
    """The additional screen's rows, after the default name is refreshed like the menu does."""
    unit.refresh_additional_name(ctx, sel)
    return [f"{option.label} : {option.value(sel)}" for option in unit.additional_options()]
