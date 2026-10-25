# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Everything the menus mutate, and the callbacks the entrypoint wires in."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Protocol

from installer.platform_core.context import Context
from installer.platform_core.toggles import default_toggles
from installer.unit_registry.selection import UnitSelection


INSTALLED_NOTE = "AFC was installed in this session. Restart the script to make more changes."


@dataclass
class MenuState:
    """The mutable state shared by every menu."""

    ctx: Context
    selection: UnitSelection
    message: str = ""
    update_message: str = ""
    unit_message: str = ""
    toggles: Dict[str, bool] = field(default_factory=default_toggles)
    emu_lanes_index: int = 4  # survives leaving and re-entering a menu


class Actions(Protocol):
    """What the menus can trigger. The entrypoint supplies the implementation."""

    def install(self, state: MenuState) -> None:
        """Run the install action for the current selection."""

    def install_additional(self, state: MenuState) -> None:
        """Run the additional unit install action."""

    def update(self, state: MenuState) -> None:
        """Run the update action."""

    def uninstall(self, state: MenuState) -> None:
        """Run the uninstall action."""

    def delete_var_file(self, state: MenuState) -> str:
        """Remove AFC.var.unit after a confirmation; returns the status message."""

    def rename_unit(self, state: MenuState) -> str:
        """Ask for and apply a unit rename; returns the status message."""

    def exit(self, state: MenuState) -> None:
        """Run the exit action (restart, cleanup); normally raises InstallerExit."""
