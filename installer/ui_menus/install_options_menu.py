# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The options screen for installing the first unit of the chosen type."""
from __future__ import annotations

from typing import List, Optional

from installer.platform_core.console import NC, RED
from installer.platform_core.errors import InstallerExit
from installer.platform_core.toggles import TOGGLES
from installer.ui_menus.art import art_name_for_type
from installer.ui_menus.entries import Entry, buffer_label, require_unit
from installer.ui_menus.navigation import MenuDeps, read_choice
from installer.ui_menus.state import INSTALLED_NOTE, MenuState
from installer.ui_menus.view import INSTALL_OPTIONS_BOX, MenuView, Row, row
from installer.unit_registry.extruders import find_printer_extruders
from installer.unit_registry.options import UnitOption

_PIN_PROMPT = "Enter toolhead sensor pin (Example: nhk:gpio13): "
_NEEDS_BUFFER = "Ramming requires a buffer type. Choose one from the list."
_INSTALL_LABEL = "I. Install system with current selections"


class InstallOptionsMenu:
    """Numbered options for the selected unit type, with install, back, main and quit."""

    def __init__(self, state: MenuState, deps: MenuDeps) -> None:
        """
        Create the screen.

        :param state: shared menu state
        :param deps: menu collaborators
        """
        self._state = state
        self._deps = deps

    def _needs_buffer(self) -> bool:
        sel = self._state.selection
        unit = require_unit(sel.installation_type)
        return (
            unit.buffer_selectable
            and sel.toolhead_sensor == "Ramming"
            and sel.buffer_type in ("Unknown", "None")
        )

    def _toggle(self, name: str, label: str) -> Entry:
        state = self._state

        def activate() -> str:
            state.toggles[name] = not state.toggles[name]
            return f"{label} {'Enabled' if state.toggles[name] else 'Disabled'}"

        return Entry(lambda n: row(f"{n}. {label} : {state.toggles[name]}"), activate)

    def _sensor(self) -> Entry:
        sel = self._state.selection
        unit = require_unit(sel.installation_type)

        def activate() -> str:
            sel.toolhead_sensor = "Ramming" if sel.toolhead_sensor == "Sensor" else "Sensor"
            return unit.after_sensor_change(sel)

        return Entry(
            lambda n: row(
                f"{n}. Use a toolhead sensor or ramming with {unit.ramming_hardware}? : "
                f"{sel.toolhead_sensor}"
            ),
            activate,
        )

    def _buffer(self) -> Entry:
        sel = self._state.selection
        unit = require_unit(sel.installation_type)

        def draw(number: int) -> Row:
            if self._needs_buffer():
                return row(
                    f"{number}. Buffer type : ", (sel.buffer_type, "red"),
                    " (a buffer is required with ramming)",
                )
            return row(f"{number}. Buffer type : {buffer_label(sel.buffer_type)}")

        def activate() -> str:
            unit.cycle_buffer(sel)
            return f"Buffer Type: {buffer_label(sel.buffer_type)}"

        return Entry(draw, activate)

    def _extruder(self, extruders: List[str]) -> Entry:
        sel = self._state.selection

        def activate() -> str:
            position = extruders.index(sel.extruder) + 1 if sel.extruder in extruders else 0
            sel.extruder = extruders[position % len(extruders)]
            return f"Extruder: {sel.extruder}"

        return Entry(
            lambda n: row(f"{n}. Extruder unit will be connected to : {sel.extruder}"), activate
        )

    def _pin(self) -> Entry:
        sel = self._state.selection

        def draw(number: int) -> Row:
            pin = sel.toolhead_sensor_pin
            shown = (pin, "red") if pin == "Unknown" else pin
            return row(f"{number}. Toolhead sensor pin : ", shown)

        def activate() -> str:
            answer = self._deps.prompter.ask(_PIN_PROMPT)
            if answer is None:
                raise InstallerExit(1)
            sel.toolhead_sensor_pin = answer
            return f"Toolhead sensor pin set to {answer}"

        return Entry(draw, activate)

    def _unit_option(self, option: UnitOption) -> Entry:
        state = self._state
        sel = state.selection

        def activate() -> str:
            return option.activate(state.ctx, self._deps.console, self._deps.prompter, sel)

        return Entry(lambda n: row(f"{n}. {option.label} : {option.value(sel)}"), activate)

    def _entries(self) -> List[Entry]:
        """
        Build the numbered rows for the current selection.

        The sensor pin comes last so no other number moves when it appears.

        :return List[Entry]: the entries in display order
        """
        state = self._state
        sel = state.selection
        unit = require_unit(sel.installation_type)
        entries = [self._toggle(toggle.key, toggle.label) for toggle in TOGGLES]
        if unit.ramming_supported:
            entries.append(self._sensor())
        if unit.buffer_selectable:
            entries.append(self._buffer())
        entries += [self._unit_option(option) for option in unit.install_options()]
        extruders = find_printer_extruders(state.ctx)
        if len(extruders) > 1:
            entries.append(self._extruder(extruders))
        if sel.toolhead_sensor == "Sensor":
            entries.append(self._pin())
        return entries

    def view(self) -> MenuView:
        """
        Describe the current screen.

        :return MenuView: the screen
        """
        state = self._state
        sel = state.selection
        rows: List[Row] = []
        if not state.ctx.files_updated_or_installed:
            rows.append(row(f"Unit type : {sel.installation_type}"))
            rows.append(row())
            rows += [entry.row(number) for number, entry in enumerate(self._entries(), 1)]
            rows.append(row("\n", (_INSTALL_LABEL, "bold_green")))
            rows.append(row("B. Back to unit type selection"))
        rows += [row("M. Return to Main Menu"), row("Q. Quit"), row()]
        return MenuView(
            art=art_name_for_type(sel.installation_type), message=state.message,
            options_box=INSTALL_OPTIONS_BOX, rows=rows,
        )

    def _install(self) -> str:
        """
        Install unless ramming has no buffer, then go back to the main menu.

        :return str: the next menu, "install_options" when the install was refused
        """
        state = self._state
        if self._needs_buffer():
            state.message = f"{RED}{_NEEDS_BUFFER}{NC}"
            return "install_options"
        self._deps.actions.install(state)
        return "main"

    def step(self) -> Optional[str]:
        """
        Draw the screen, read one choice and handle it.

        :return Optional[str]: the next menu name, "install_options" to stay, or None to stop
        """
        deps = self._deps
        state = self._state
        deps.renderer.clear()
        deps.renderer.show(self.view())
        choice = read_choice(deps)
        done = state.ctx.files_updated_or_installed
        entries = [] if done else self._entries()
        if choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(entries):
            state.message = entries[int(choice) - 1].activate()
            return "install_options"
        if choice == "I" and not done:
            return self._install()
        if choice == "B" and not done:
            state.message = ""
            return "unit_type_install"
        if choice == "M":
            state.message = ""
            return "main"
        if choice == "Q":
            deps.actions.exit(state)
            return None
        state.message = INSTALLED_NOTE if done else "Invalid selection, please try again."
        return "install_options"
