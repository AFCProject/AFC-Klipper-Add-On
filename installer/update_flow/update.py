# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The update action and the clean-up of deprecated config."""
from __future__ import annotations

import re
from typing import List

from installer.config_editor.document import ConfigDocument
from installer.config_editor.sections import delete_key, delete_section
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.platform_core.fsops import safe_copy
from installer.platform_core.klipper_links import (
    check_init_symlink,
    exclude_from_klipper_git,
    link_extensions,
)
from installer.platform_core.system import CommandRunner

_BUFFER_HEADER = re.compile(r"\[AFC_buffer ([^\]]+)")
_VELOCITY_LINE = re.compile(r"\s*velocity")
_T_TOKEN = re.compile(r"T[0-9]+")


def _ask_yes(prompter: Prompter, prompt: str) -> bool:
    """
    Ask a y/n question; only a (case insensitive, trimmed) ``y`` is a yes.

    End of input stops the installer.

    :param prompter: where answers come from
    :param prompt: prompt text
    :return bool: True for yes
    """
    answer = prompter.ask(prompt)
    if answer is None:
        raise InstallerExit(1)
    return answer.strip().lower() == "y"


def remove_t_macros(ctx: Context) -> None:
    """
    Delete the old ``[gcode_macro T<n>]`` sections from AFC's macros file.

    Every ``T<digits>`` token anywhere in the file names a section to delete; a token
    that names no section changes nothing. A missing macros file is fine.

    :param ctx: installer context
    """
    path = ctx.afc_config_dir / "macros" / "AFC_macros.cfg"
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return
    for token in dict.fromkeys(_T_TOKEN.findall(text)):
        delete_section(path, f"gcode_macro {token}")


def remove_velocity(ctx: Context) -> List[str]:
    """
    Remove the deprecated ``velocity`` option from every ``[AFC_buffer ...]`` section.

    Only top level ``*.cfg`` files with a buffer section are considered. Any line starting
    with ``velocity`` in such a file triggers the report, even if it sits in another
    section. A file with several buffer sections is fully cleaned and a directory with no
    buffer files is simply nothing to do.

    :param ctx: installer context
    :return List[str]: one "removed" message per file that was processed
    """
    messages: List[str] = []
    for cfg in sorted(ctx.afc_config_dir.glob("*.cfg")):
        try:
            lines = ConfigDocument.load(cfg).lines
        except OSError:
            continue
        names: List[str] = []
        for line in lines:
            match = _BUFFER_HEADER.match(line)
            if match:
                names.append(match.group(1))
        if names and any(_VELOCITY_LINE.match(line) for line in lines):
            for name in dict.fromkeys(names):
                delete_key(cfg, f"AFC_buffer {name}", "velocity")
            messages.append(f"\nRemoved deprecated velocity setting from {cfg}.\n      ")
    return messages


def _refresh_macros(ctx: Context, console: Console, prompter: Prompter) -> str:
    """
    Offer to overwrite AFC's provided macros, after a second confirmation.

    :param ctx: installer context
    :param console: where the warnings go
    :param prompter: where answers come from
    :return str: the macro part of the update message (empty if nothing was done)
    """
    if not _ask_yes(prompter, "Do you want to update the AFC provided macros? (y/n): "):
        return ""
    console.print_msg(
        "WARNING",
        "Updating macros will overwrite any custom changes you have made to these macros.",
    )
    console.print_msg(
        "WARNING", "This includes the brush, cut, kick, park, afc_macros, and poop macros."
    )
    if not _ask_yes(prompter, "Please confirm you want to update these macros: (y/n): "):
        return ""
    sources = sorted((ctx.afc_path / "config" / "macros").glob("*.cfg"))
    if not sources:
        return "\nNo macro files found to update.\n        "
    target = ctx.afc_config_dir / "macros"
    target.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for source in sources:
        if safe_copy(ctx, console, prompter, source, target) == "copied":
            copied += 1
        else:
            skipped += 1
    if copied > 0:
        counts = f"{copied} copied, {skipped} skipped"
        return f"\nAFC Macros updated successfully ({counts}).\n          "
    return f"\nAFC Macros update skipped (all {skipped} files kept as-is).\n          "


def update_afc(
    ctx: Context,
    console: Console,
    prompter: Prompter,
    runner: CommandRunner,
    afc_version: str,
) -> str:
    """
    Update AFC: optionally refresh the macros, then relink and clean up old config.

    :param ctx: installer context, ``files_updated_or_installed`` is set
    :param console: where messages go
    :param prompter: where answers come from
    :param runner: command runner (used to repair a bad ``__init__.py`` link)
    :param afc_version: version text for the success message (git installs only)
    :return str: the update message
    """
    message = _refresh_macros(ctx, console, prompter)
    check_init_symlink(ctx, runner)
    link_extensions(ctx)
    remove_t_macros(ctx)
    message += "".join(remove_velocity(ctx))
    if ctx.git_install:
        if not ctx.test_mode:
            exclude_from_klipper_git(ctx, console)
        message += f"\nAFC Klipper Add-On updated successfully with version v{afc_version}.\n"
    else:
        message += "\nAFC Klipper Add-On update process completed.\n"
    ctx.files_updated_or_installed = True
    return message
