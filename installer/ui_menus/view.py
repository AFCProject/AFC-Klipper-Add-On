# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Renderer independent description of a menu screen."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple, Union

BOX_WIDTH = 87


@dataclass(frozen=True)
class Segment:
    """A piece of a row. ``color`` is "", "red", "green" or "bold_green"."""

    text: str
    color: str = ""


Row = Tuple[Segment, ...]


def row(*parts: Union[str, Tuple[str, str]]) -> Row:
    """
    Build a row from plain strings and ``(text, color)`` pairs.

    :param parts: row pieces in order
    :return Row: the row (no parts is a blank line)
    """
    return tuple(
        Segment(part) if isinstance(part, str) else Segment(part[0], part[1]) for part in parts
    )


@dataclass(frozen=True)
class BoxLine:
    """A banner box line: ``left`` in the default color, ``right`` after the switch to green."""

    left: str
    right: str


Box = Tuple[BoxLine, ...]

_TYPE_HINT = BoxLine(
    "           Type a number or letter and press Enter/Return to toggle choice", "           "
)
_UPDATE_HINT = (
    BoxLine(
        "            Please review the following options to update your system",
        "                ",
    ),
    BoxLine(
        "        Use the provided option selection to cycle through available choices",
        "         ",
    ),
)

HELP_BOX: Box = (
    BoxLine("                                    AFC Script Help      ", " " * 28),
)
MAIN_OPTIONS_BOX: Box = (
    BoxLine("                                  AFC Install Options", " " * 32),
    _TYPE_HINT,
)
INSTALL_OPTIONS_BOX: Box = (
    BoxLine(
        "            Please review the following options to configure your system",
        "             ",
    ),
    _TYPE_HINT,
)
ADDITIONAL_OPTIONS_BOX: Box = (
    BoxLine(
        "         Please review the following options to add an additional unit type",
        "          ",
    ),
    _TYPE_HINT,
)
UPDATE_OPTIONS_BOX: Box = _UPDATE_HINT
def centered(text: str) -> BoxLine:
    """
    A box line with the text centered between the borders.

    :param text: the text to show
    :return BoxLine: a line exactly as wide as the inside of every other box
    """
    left = " " * ((BOX_WIDTH - 2 - len(text)) // 2) + text
    return BoxLine(left, " " * (BOX_WIDTH - 2 - len(left)))


PICKER_INSTALL_BOX: Box = (
    centered("Choose the type of unit to install"),
    centered("Type its number and press Enter/Return to continue"),
)
PICKER_ADDITIONAL_BOX: Box = (
    centered("Choose the type of unit to add"),
    centered("Type its number and press Enter/Return to continue"),
)
UTILITIES_HEADER_BOX: Box = (
    BoxLine("                                 System Utilities Menu    ", " " * 27),
    HELP_BOX[0],
)


@dataclass
class MenuView:
    """One screen: art, intro rows, help box, message, options box, option rows."""

    art: str = ""
    top: List[Row] = field(default_factory=list)
    help_box: Box = HELP_BOX
    message: str = ""
    options_box: Box = ()
    rows: List[Row] = field(default_factory=list)
