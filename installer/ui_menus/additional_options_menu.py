# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The options screen for adding a further unit of the chosen type to an existing install."""
from __future__ import annotations

from typing import List, Optional

from installer.platform_core.console import NC, RED
from installer.ui_menus.art import art_name_for_type
from installer.ui_menus.entries import Entry, buffer_label, require_unit
from installer.ui_menus.navigation import MenuDeps, read_choice
from installer.ui_menus.state import MenuState
from installer.ui_menus.view import ADDITIONAL_OPTIONS_BOX, MenuView, Row, row
from installer.unit_registry.buffers import (
    apply_shared_buffer,
    apply_unit_buffer,
    prompt_shared_buffer_name,
)
from installer.unit_registry.options import UnitOption
from installer.unit_registry.naming import name_in_use_message, verify_name_not_in_use
from installer.unit_registry.registry import (
    additional_buffer_options,
    additional_buffer_supported,
)

_INTRO = (
    "This menu allows you to add ADDITIONAL unit types. "
    "It should NOT be used for your first unit.\n"
    "This is a best effort in adding an additional unit. You will probably be required\n"
    "to manually edit the configuration files to ensure proper operation.\n"
)
_INSTALL_LABEL = "I. Install system with current selections"


def own_buffer_label(buffer_type: str) -> str:
    """
    Text of the own-buffer row for an additional buffer choice.

    :param buffer_type: the additional buffer type
    :return str: the label
    """
    if buffer_type == "None":
        return "No (shared with an existing unit's buffer)"
    return f"Yes: {buffer_label(buffer_type)}"


class AdditionalOptionsMenu:
    """Numbered options for the new unit, with install, back, main and quit."""

    def __init__(self, state: MenuState, deps: MenuDeps) -> None:
        """
        Create the screen: show the intro and offer the lowest free default name.

        :param state: shared menu state
        :param deps: menu collaborators
        """
        self._state = state
        self._deps = deps
        state.message = _INTRO
        self._refresh_name()

    def _refresh_name(self) -> None:
        sel = self._state.selection
        unit = require_unit(sel.installation_type)
        unit.refresh_additional_name(self._state.ctx, sel)

    def _buffer(self) -> Entry:
        sel = self._state.selection

        def activate() -> str:
            options = additional_buffer_options(sel.installation_type)
            current = sel.additional_buffer_type
            position = options.index(current) + 1 if current in options else 0
            sel.additional_buffer_type = options[position % len(options)]
            return f"Unit has its own buffer? {own_buffer_label(sel.additional_buffer_type)}"

        return Entry(
            lambda n: row(
                f"{n}. Unit has its own buffer? : {own_buffer_label(sel.additional_buffer_type)}"
            ),
            activate,
        )

    def _entries(self) -> List[Entry]:
        """
        Build the numbered rows for the current selection.

        :return List[Entry]: the entries in display order
        """
        state = self._state
        sel = state.selection
        unit = require_unit(sel.installation_type)
        entries = [self._unit_option(option) for option in unit.additional_options()]
        if additional_buffer_supported(sel.installation_type):
            entries.append(self._buffer())
        return entries

    def _unit_option(self, option: UnitOption) -> Entry:
        state = self._state
        sel = state.selection

        def activate() -> str:
            return option.activate(state.ctx, self._deps.console, self._deps.prompter, sel)

        return Entry(lambda n: row(f"{n}. {option.label} : {option.value(sel)}"), activate)

    def view(self) -> MenuView:
        """
        Describe the current screen.

        :return MenuView: the screen
        """
        state = self._state
        sel = state.selection
        rows: List[Row] = [row(f"Unit type : {sel.installation_type}"), row()]
        rows += [entry.row(number) for number, entry in enumerate(self._entries(), 1)]
        rows += [
            row(),
            row((_INSTALL_LABEL, "green")),
            row("B. Back to unit type selection"),
            row("M. Return to Main Menu"),
            row("Q. Quit"),
            row(),
        ]
        return MenuView(
            art=art_name_for_type(sel.installation_type), message=state.message,
            options_box=ADDITIONAL_OPTIONS_BOX, rows=rows,
        )

    def _install(self) -> str:
        """
        Install the unit, wire up its buffer, and build the success message.

        :return str: the next menu: "main" on success, this screen if the name is taken
        """
        deps = self._deps
        state = self._state
        ctx = state.ctx
        sel = state.selection
        if verify_name_not_in_use(ctx, sel.unit_name):
            state.message = name_in_use_message(sel.unit_name)
            return "additional_options"
        supported = additional_buffer_supported(sel.installation_type)
        shared = ""
        if supported and sel.additional_buffer_type == "None":
            shared = prompt_shared_buffer_name(ctx, deps.console, deps.prompter)
        deps.actions.install_additional(state)
        if supported:
            sel.is_additional_unit = True
            if sel.additional_buffer_type != "None":
                apply_unit_buffer(
                    ctx, deps.console, deps.prompter, sel, sel.additional_buffer_type
                )
            elif shared:
                apply_shared_buffer(ctx, deps.console, sel, shared)
            sel.is_additional_unit = False
        message = (
            f"{sel.unit_name} created successfully, please look over config file and update "
            "lane numbers.\n"
            "Additionally, please ensure any MCU connections are updated in the appropriate "
            "files (CANBus, serial, etc)\n"
            "This is not a 100% turn-key solution and will require some manual configuration "
            "based on your specific setup."
        )
        if shared:
            message += f"\n\n{sel.unit_name} is set to share the existing buffer '{shared}'."
        if sel.additional_buffer_type == "TurtleNeckV2":
            message += (
                "\n\nEnsure you add the correct serial information to the "
                f"{ctx.afc_config_dir}/mcu/TurtleNeckv2.cfg file"
            )
        if sel.additional_buffer_type == "TurtleNeck2Remastered":
            message += (
                "\n\nEnsure you add the correct serial information to the "
                f"{ctx.afc_config_dir}/mcu/TN2.cfg file"
            )
        if sel.additional_buffer_type == "FPS_PSF":
            message += (
                "\n\nEnsure the PSF ADC pin in your buffer configuration matches where your "
                "wiring is connected to your MCU."
            )
        message += (
            f"\n\n{RED}Please restart the script after installation to install additional "
            f"units or make changes to the current unit.{NC}"
        )
        state.message = message
        return "main"

    def step(self) -> Optional[str]:
        """
        Draw the screen, read one choice and handle it.

        :return Optional[str]: the next menu name, "additional_options" to stay, or None to stop
        """
        deps = self._deps
        state = self._state
        self._refresh_name()
        deps.renderer.clear()
        deps.renderer.show(self.view())
        choice = read_choice(deps)
        entries = self._entries()
        if choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(entries):
            state.message = entries[int(choice) - 1].activate()
            return "additional_options"
        if choice == "I":
            return self._install()
        if choice == "B":
            state.message = ""
            return "unit_type_additional"
        if choice == "M":
            state.message = ""
            return "main"
        if choice == "Q":
            deps.actions.exit(state)
            return None
        state.message = "Invalid selection, please try again."
        return "additional_options"
