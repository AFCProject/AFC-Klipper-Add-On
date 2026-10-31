# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Declarative unit options: what a unit type lets the user choose, with no menu code."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Tuple

from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.unit_registry.selection import UnitSelection

HTLF_BOARD_TYPES: Tuple[str, ...] = ("ERB", "MMB_1.1", "MMB_1.0")
NIGHTOWL_BOARD_TYPES: Tuple[str, ...] = ("ERB_2.0", "OWLFC_Mini")
CLAYMORE_BOARD_TYPES: Tuple[str, ...] = ("AFC_Lite",)
QB_BOARD_TYPES: Tuple[str, ...] = ("MMB_1.0", "MMB_1.1", "MMB_2.0")
QB_MOTOR_TYPES: Tuple[str, ...] = ("NEMA_17", "NEMA_14")
EMU_LANE_OPTIONS: Tuple[str, ...] = ("1", "2", "3", "4", "5", "6", "7", "8")
EMU_BOARD_TYPES: Tuple[str, ...] = ("EBB", "SLB")
ACE_UNIT_TYPES: Tuple[str, ...] = ("V1", "V2_Pro")


class UnitOption(ABC):
    """One row on a unit's options screen."""

    label: str

    @abstractmethod
    def value(self, sel: UnitSelection) -> str:
        """
        The text shown after the label.

        :param sel: the user's unit choices
        :return str: the current value
        """

    @abstractmethod
    def activate(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> str:
        """
        Act on the user choosing this row (cycle a value or ask for one).

        :param ctx: installer context
        :param console: where prompts print
        :param prompter: where answers come from
        :param sel: the user's unit choices, updated in place
        :return str: the status message to show
        """


class ChoiceOption(UnitOption):
    """A value that steps through a fixed list each time it is chosen."""

    def __init__(
        self,
        label: str,
        attr: str,
        choices: Tuple[str, ...],
        cast: Callable[[str], object] = str,
    ) -> None:
        """
        Create the option.

        :param label: row label, also used in the status message
        :param attr: the ``UnitSelection`` attribute that holds the value
        :param choices: the values to step through
        :param cast: converts a choice before it is stored (for example ``int``)
        """
        self.label = label
        self.attr = attr
        self.choices = choices
        self.cast = cast

    def value(self, sel: UnitSelection) -> str:
        """
        The current value.

        :param sel: the user's unit choices
        :return str: the value as text
        """
        return str(getattr(sel, self.attr))

    def activate(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> str:
        """
        Step to the choice after the current one, wrapping, or to the first if unknown.

        :param ctx: installer context
        :param console: unused
        :param prompter: unused
        :param sel: the user's unit choices, updated in place
        :return str: ``<label>: <new value>``
        """
        current = self.value(sel)
        if current in self.choices:
            following = self.choices[(self.choices.index(current) + 1) % len(self.choices)]
        else:
            following = self.choices[0]
        setattr(sel, self.attr, self.cast(following))
        return f"{self.label}: {following}"


class NameOption(UnitOption):
    """The unit's name, asked for with the standard name prompt."""

    def __init__(self, label: str, additional_prompt: bool = False) -> None:
        """
        Create the option.

        :param label: row label
        :param additional_prompt: use the additional-unit prompt (offers the lowest free name)
        """
        self.label = label
        self.additional_prompt = additional_prompt

    def value(self, sel: UnitSelection) -> str:
        """
        The current name.

        :param sel: the user's unit choices
        :return str: the unit name
        """
        return sel.unit_name

    def activate(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> str:
        """
        Ask for a name and store it. The empty answer is the unit type's default name.

        :param ctx: installer context
        :param console: where prompts print
        :param prompter: where answers come from
        :param sel: the user's unit choices, updated in place
        :return str: ``Naming unit <name>``
        """
        from installer.unit_registry.naming import name_unit
        from installer.unit_registry.registry import name_additional_unit, unit_for

        if self.additional_prompt:
            return name_additional_unit(ctx, console, prompter, sel)
        unit = unit_for(sel.installation_type)
        if unit is None:
            return name_unit(console, prompter, sel)
        return name_unit(console, prompter, sel, unit.default_name)
