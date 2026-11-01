# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The AFC include in printer.cfg and the Moonraker update block (update_commands.sh)."""
from __future__ import annotations

import re
from pathlib import Path

from installer.config_editor.document import ConfigDocument

INCLUDE_STATEMENT = "[include AFC/*.cfg]"
_SAVE_CONFIG_LINE = "#*# <---------------------- SAVE_CONFIG ---------------------->"
# The marker may be written with one or more leading '#', so the shorter form is searched for.
_SAVE_CONFIG_REGEX = re.compile(r"# <---------------------- SAVE_CONFIG ---------------------->")
_MOONRAKER_SECTION = "[update_manager afc-software]"


def manage_include(path: Path, action: str) -> None:
    """
    Add or remove the `[include AFC/*.cfg]` line in a config file.

    `add` does nothing if any line already contains the statement. Otherwise it goes
    before every `SAVE_CONFIG` marker line, or at the end when the exact marker text
    is absent. `remove` drops every line containing the statement.

    :param path: config file, normally printer.cfg
    :param action: `"add"` or `"remove"`
    """
    if action not in ("add", "remove"):
        error_str = f"manage_include: unknown action '{action}'"
        raise ValueError(error_str)
    if not path.exists():
        # a missing file is created when adding an include
        if action == "add":
            path.write_text("")
        else:
            return
    doc = ConfigDocument.load(path)
    if not any(INCLUDE_STATEMENT in line for line in doc.lines):
        if action == "add":
            _add_include(doc)
        return
    if action == "remove":
        doc.lines = [line for line in doc.lines if INCLUDE_STATEMENT not in line]
        # every kept line is written with a newline
        if doc.lines and not doc.lines[-1].endswith("\n"):
            doc.lines[-1] += "\n"
        doc.save()


def _add_include(doc: ConfigDocument) -> None:
    """
    Insert the include statement and save.

    :param doc: loaded document that does not yet contain the include
    """
    if any(_SAVE_CONFIG_LINE in line for line in doc.lines):
        lines = []
        for line in doc.lines:
            if _SAVE_CONFIG_REGEX.search(line):
                lines.append(f"{INCLUDE_STATEMENT}\n")
            lines.append(line)
        doc.lines = lines
    else:
        # never glue the include onto an unterminated last line
        if doc.lines and not doc.lines[-1].endswith("\n"):
            doc.lines[-1] += "\n"
        doc.lines.append(f"{INCLUDE_STATEMENT}\n")
    doc.save()


def moonraker_update_block(afc_path: str, gitrepo: str, service: str, home: str) -> str:
    """
    Build the `[update_manager afc-software]` block for moonraker.conf.

    A checkout under `home` is written as `~/...`. The primary branch is always
    `main`, whatever branch is installed.

    :param afc_path: the AFC-Klipper-Add-On checkout
    :param gitrepo: git remote URL
    :param service: Klipper service managed by the update
    :param home: the user's home directory
    :return str: the block, starting and ending with a newline
    """
    repo_path = afc_path
    if afc_path.startswith(f"{home}/"):
        repo_path = f"~{afc_path[len(home):]}"
    return (
        "\n[update_manager afc-software]\n"
        "type: git_repo\n"
        f"path: {repo_path}\n"
        f"origin: {gitrepo}\n"
        f"managed_services: {service}\n"
        "primary_branch: main\n"
        "is_system_service: False\n"
        "info_tags:\n"
        "    desc=AFC Klipper Add On\n"
    )


def ensure_moonraker_update_block(path: Path, block: str) -> bool:
    """
    Append the update block to moonraker.conf unless a line already names the section.

    The caller restarts Moonraker when this returns True.

    :param path: moonraker.conf
    :param block: text from :func:`moonraker_update_block`
    :return bool: True if the block was added
    """
    doc = ConfigDocument.load(path)
    if any(_MOONRAKER_SECTION in line for line in doc.lines):
        return False
    doc.append_text(f"\n{block}")
    doc.save()
    return True
