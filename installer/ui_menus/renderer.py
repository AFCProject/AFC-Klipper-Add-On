# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The renderer interface and the stdlib ANSI renderer."""
from __future__ import annotations

from typing import Dict, List, Protocol

from installer.platform_core.console import BOLD_GREEN, GREEN, MENU_GREEN, NC, RED, Console
from installer.ui_menus.art import load_art
from installer.ui_menus.view import BOX_WIDTH, Box, MenuView, Row

_COLORS: Dict[str, str] = {"red": RED, "green": GREEN, "bold_green": BOLD_GREEN}


class Renderer(Protocol):
    """Draws menu screens. Implementations never change menu state."""

    def clear(self) -> None:
        """Clear the screen."""

    def show(self, view: MenuView) -> None:
        """Draw one screen."""


def _row_text(row: Row) -> str:
    parts: List[str] = []
    for segment in row:
        color = _COLORS.get(segment.color)
        parts.append(segment.text if color is None else f"{color}{segment.text}{NC}")
    return "".join(parts) + "\n"


def _box_text(box: Box) -> str:
    lines = [f"{MENU_GREEN}{'▄' * BOX_WIDTH} \n"]
    for line in box:
        lines.append(f"█{NC}{line.left}{MENU_GREEN}{line.right}█\n")
    lines.append(f"{MENU_GREEN}{'▀' * BOX_WIDTH}{NC} \n")
    return "".join(lines)


class AnsiRenderer:
    """Writes the screen as plain ANSI text."""

    def __init__(self, console: Console) -> None:
        """
        Create the renderer.

        :param console: where output goes
        """
        self._console = console

    def clear(self) -> None:
        """Clear the screen (only when writing to a terminal)."""
        self._console.clear()

    @staticmethod
    def render(view: MenuView) -> str:
        """
        Build the screen text.

        :param view: the screen to draw
        :return str: text with ANSI escapes
        """
        text = load_art(view.art)
        text += "".join(_row_text(row) for row in view.top)
        text += _box_text(view.help_box)
        text += f"{view.message}\n"
        text += _box_text(view.options_box)
        text += "".join(_row_text(row) for row in view.rows)
        return text

    def show(self, view: MenuView) -> None:
        """
        Draw one screen.

        :param view: the screen to draw
        """
        self._console.write(self.render(view))
