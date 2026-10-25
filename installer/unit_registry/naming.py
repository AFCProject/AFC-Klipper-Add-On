# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Unit naming helpers shared by the unit types and the registry."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

from installer.config_editor.edits import replace_text_in_tree
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.unit_registry.base import UnitType
from installer.unit_registry.selection import UnitSelection

_VALID_NAME = re.compile(r"[A-Za-z0-9_-]+")
_INVALID_NAME_TEXT = (
    "Invalid input. The unit name must consist of only a-z, A-Z, 0-9, -, and _ and be "
    "no more than 24 characters long."
)
_MAX_NAME_LENGTH = 24
_FIRST_NAME = "Turtle_1"
_SECOND_NAME = "Turtle_2"


def lowest_free_name(ctx: Context, sel: UnitSelection, unit: Optional[UnitType]) -> str:
    """
    The default name for an additional unit: the lowest free ``<prefix>_N``.

    "Free" means no matching unit file exists yet in the AFC config directory.

    :param ctx: installer context
    :param sel: the user's unit choices
    :param unit: the unit type, or None for an unknown type
    :return str: the name
    """
    prefix = unit.additional_name_prefix if unit is not None else "Unit"
    number = 1
    while True:
        if unit is not None:
            probe = unit.additional_probe_file(prefix, number, sel)
        else:
            probe = f"AFC_{prefix}_{number}.cfg"
        if not (ctx.afc_config_dir / probe).is_file():
            return f"{prefix}_{number}"
        number += 1


def valid_unit_name(name: str) -> bool:
    """
    Is this an allowed unit name: ASCII letters, digits, ``-`` and ``_``, at most 24 long?

    :param name: candidate name
    :return bool: True if allowed
    """
    return len(name) <= _MAX_NAME_LENGTH and _VALID_NAME.fullmatch(name) is not None


def ask_for_name(console: Console, prompter: Prompter, prompt: str, default: str) -> str:
    """
    Ask until the answer is a valid name; an empty answer takes the default.

    End of input stops the installer.

    :param console: where the "invalid" explanation goes
    :param prompter: where answers come from
    :param prompt: prompt text
    :param default: name used for an empty answer
    :return str: a valid name
    """
    while True:
        answer = prompter.ask(prompt)
        if answer is None:
            raise InstallerExit(1)
        name = answer.strip() or default
        if valid_unit_name(name):
            return name
        console.print_msg("OTHER", _INVALID_NAME_TEXT)


def name_unit(
    console: Console, prompter: Prompter, sel: UnitSelection, default: str = _FIRST_NAME
) -> str:
    """
    Ask for the first unit's name and store it on the selection.

    :param console: where messages go
    :param prompter: where answers come from
    :param sel: the user's unit choices, ``unit_name`` is updated
    :param default: the name used when the answer is empty
    :return str: the status message for the menu
    """
    sel.unit_name = ask_for_name(
        console, prompter, f"Enter name for unit (Default: {default}): ", default
    )
    return f"Naming unit {sel.unit_name}"


def replace_unit_name(ctx: Context, old: str, new: str) -> None:
    """
    Rename a unit everywhere in the AFC config directory.

    Every file's contents are rewritten, then each ``AFC_<old>.cfg`` anywhere in the tree
    moves to ``AFC_<new>.cfg`` at the top and each ``mcu/**/<old>.cfg`` to ``mcu/<new>.cfg``,
    replacing whatever is there, like the ``find -exec mv`` it ports.

    :param ctx: installer context
    :param old: current unit name
    :param new: new unit name
    """
    cfg = ctx.afc_config_dir
    replace_text_in_tree(cfg, old, new)
    for found in _regular_files_named(cfg, f"AFC_{old}.cfg"):
        os.replace(found, cfg / f"AFC_{new}.cfg")
    for found in _regular_files_named(cfg / "mcu", f"{old}.cfg"):
        os.replace(found, cfg / "mcu" / f"{new}.cfg")


def _regular_files_named(directory: Path, name: str) -> List[Path]:
    """
    Regular files (not links) with this exact name anywhere under a directory.

    :param directory: tree to search; a missing one has no files
    :param name: file name
    :return List[Path]: the matches
    """
    found: List[Path] = []
    for root, _dirs, files in os.walk(directory):
        candidate = Path(root) / name
        if name in files and not candidate.is_symlink():
            found.append(candidate)
    return found


def rename_unit_prompt(ctx: Context, console: Console, prompter: Prompter) -> str:
    """
    The utilities menu's "rename existing unit": ask for both names, then rename.

    :param ctx: installer context
    :param console: where messages go
    :param prompter: where answers come from
    :return str: the status message for the menu
    """
    old = ask_for_name(
        console,
        prompter,
        f"Enter name of unit you would like to replace (Default: {_FIRST_NAME}): ",
        _FIRST_NAME,
    )
    new = ask_for_name(
        console, prompter, f"Enter new name for unit (Default: {_SECOND_NAME}): ", _SECOND_NAME
    )
    if not (ctx.afc_config_dir / f"AFC_{old}.cfg").is_file():
        return f"Unit {old} not found, please enter a valid unit name."
    replace_unit_name(ctx, old, new)
    return f"Renaming unit {old} to {new}"


def _any_file_contains(directory: Path, needle: bytes, follow_links: bool) -> bool:
    """
    Does any file under a directory contain these bytes (searched over a whole tree)?

    :param directory: tree to search; a missing one contains nothing
    :param needle: bytes to look for
    :param follow_links: follow symlinks while walking, otherwise skip them
    :return bool: True if some file matches
    """
    for root, _dirs, files in os.walk(directory, followlinks=follow_links):
        for name in files:
            path = Path(root) / name
            if path.is_symlink() and not follow_links:
                continue
            try:
                if needle in path.read_bytes():
                    return True
            except OSError:
                continue
    return False


def verify_name_not_in_use(ctx: Context, name: str) -> bool:
    """
    Is this name already used anywhere in the AFC config directory contents?

    A plain substring search over every file, so ``Turtle_1`` is blocked by ``Turtle_10``.

    :param ctx: installer context
    :param name: candidate unit name
    :return bool: True if the name is in use
    """
    return _any_file_contains(ctx.afc_config_dir, name.encode(), follow_links=True)


def name_in_use_message(name: str) -> str:
    """
    The message shown when a chosen name is taken.

    :param name: the rejected name
    :return str: the message
    """
    return f"Unit {name} already exists, please enter a unique unit name."


def check_existing_unit_installed(ctx: Context) -> bool:
    """
    Has a first unit been installed (does any config file define an ``[AFC_stepper lane``)?

    :param ctx: installer context
    :return bool: True if a lane section exists
    """
    return _any_file_contains(ctx.afc_config_dir, b"[AFC_stepper lane", follow_links=False)
