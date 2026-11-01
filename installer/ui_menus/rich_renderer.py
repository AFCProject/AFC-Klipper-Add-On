# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Optional ``rich`` renderer. ``rich`` is never required: it is imported only when used."""
from __future__ import annotations

import importlib
import os
from typing import Any, Dict, Mapping, Optional

from installer.platform_core.console import Console
from installer.ui_menus.art import load_art
from installer.ui_menus.renderer import AnsiRenderer, Renderer
from installer.ui_menus.view import BOX_WIDTH, Box, MenuView, Row

_STYLES: Dict[str, str] = {"red": "red", "green": "green", "bold_green": "bold green"}
_BORDER = "color(143)"
NO_RICH_ENV = "AFC_NO_RICH"  # testing only: force the plain renderer


def load_rich() -> Optional[Any]:
    """
    Import ``rich`` if it is installed.

    :return Optional[Any]: a namespace-like object with Console, Panel, Text and Group, or None
    """
    try:
        console = importlib.import_module("rich.console")
        panel = importlib.import_module("rich.panel")
        text = importlib.import_module("rich.text")
    except ImportError:
        return None

    class Rich:
        Console = console.Console
        Group = console.Group
        Panel = panel.Panel
        Text = text.Text

    return Rich


def make_rich_console(rich: Any, **kwargs: Any) -> Any:
    """
    Build a `rich` Console that keeps 256 colors on terminals it only detects as 16 color.

    Terminals such as ghostty, kitty or wezterm set neither `COLORTERM` nor a `*256color`
    `TERM` over ssh, so `rich` picks the 16 color palette and the art and borders shift hue.
    The plain renderer always writes 256 color codes, so this matches it.

    :param rich: the object returned by `load_rich`
    :param kwargs: passed to the `rich` Console
    :return Any: the Console
    """
    console = rich.Console(**kwargs)
    if console.color_system == "standard":
        console = rich.Console(**kwargs, color_system="256")
    return console


class RichRenderer:
    """Draws the same MenuView with panels and styled text."""

    def __init__(self, console: Console, rich: Any, rich_console: Optional[Any] = None) -> None:
        """
        Create the renderer.

        :param console: used to clear the screen
        :param rich: the object returned by ``load_rich``
        :param rich_console: a ``rich`` Console to draw on, defaults to a new one on stdout
        """
        self._console = console
        self._rich = rich
        self._out = rich_console if rich_console is not None else make_rich_console(rich)

    def clear(self) -> None:
        """Clear the screen (only when writing to a terminal)."""
        self._console.clear()

    def _row(self, row: Row) -> Any:
        text = self._rich.Text()
        for segment in row:
            text.append(segment.text, style=_STYLES.get(segment.color))
        return text

    def _box(self, box: Box) -> Any:
        lines = [
            self._rich.Text((line.left + line.right).strip(), justify="center") for line in box
        ]
        return self._rich.Panel(
            self._rich.Group(*lines), border_style=_BORDER, width=BOX_WIDTH + 1
        )

    def show(self, view: MenuView) -> None:
        """
        Draw one screen.

        :param view: the screen to draw
        """
        out = self._out
        art = load_art(view.art)
        if art:
            out.print(self._rich.Text.from_ansi(art.rstrip("\n")), soft_wrap=True)
        for row in view.top:
            out.print(self._row(row), soft_wrap=True)
        out.print(self._box(view.help_box))
        out.print(self._rich.Text.from_ansi(view.message), soft_wrap=True)
        out.print(self._box(view.options_box))
        for row in view.rows:
            out.print(self._row(row), soft_wrap=True)


def rich_disabled(env: Mapping[str, str]) -> bool:
    """
    Check the testing switch that forces the plain renderer.

    Set ``AFC_NO_RICH`` to anything except an empty value or ``0`` to turn ``rich`` off.

    :param env: the environment to look in
    :return bool: True if ``rich`` must not be used
    """
    return env.get(NO_RICH_ENV, "") not in ("", "0")


def choose_renderer(
    console: Console, is_tty: bool, env: Optional[Mapping[str, str]] = None
) -> Renderer:
    """
    Pick the rich renderer when ``rich`` is installed and output is a terminal.

    :param console: used by the renderers to clear the screen
    :param is_tty: whether stdout is a terminal
    :param env: environment holding the ``AFC_NO_RICH`` switch, defaults to ``os.environ``
    :return Renderer: a RichRenderer, otherwise the plain ANSI renderer
    """
    if is_tty and not rich_disabled(os.environ if env is None else env):
        rich = load_rich()
        if rich is not None:
            return RichRenderer(console, rich)
    return AnsiRenderer(console)
