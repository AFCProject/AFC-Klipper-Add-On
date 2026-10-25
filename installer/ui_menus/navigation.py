# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Menu navigation: a loop over menu names, not menus calling each other."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Protocol

from installer.platform_core.console import Console, Prompter
from installer.platform_core.errors import InstallerExit
from installer.ui_menus.renderer import Renderer
from installer.ui_menus.state import Actions, MenuState


@dataclass
class MenuDeps:
    """The collaborators every menu needs."""

    console: Console
    prompter: Prompter
    renderer: Renderer
    actions: Actions


class Menu(Protocol):
    """One menu. A fresh instance is made each time the user navigates to it."""

    def step(self) -> Optional[str]:
        """
        Draw the screen, read one choice and handle it.

        :return Optional[str]: the next menu name, the same name to stay, or None to stop
        """


MenuFactory = Callable[[MenuState, MenuDeps], Menu]


def read_choice(deps: MenuDeps) -> str:
    """
    Ask for a menu choice, upper cased.

    End of input stops the installer with status 1 and no Klipper restart.

    :param deps: menu collaborators
    :return str: the upper cased choice
    """
    answer = deps.prompter.ask("Enter your choice: ")
    if answer is None:
        raise InstallerExit(1)
    return answer.upper()


def run_menus(
    start: str, state: MenuState, deps: MenuDeps, menus: Dict[str, MenuFactory]
) -> None:
    """
    Run menus until one returns None (the exit action finished).

    :param start: name of the first menu
    :param state: shared menu state
    :param deps: menu collaborators
    :param menus: menu name to factory
    """
    name: Optional[str] = start
    current: Optional[str] = None
    menu: Optional[Menu] = None
    while name is not None:
        if name != current or menu is None:
            menu = menus[name](state, deps)
            current = name
        name = menu.step()
