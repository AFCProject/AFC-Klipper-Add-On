# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Filesystem helpers: safe_copy and the AFC config backups."""
from __future__ import annotations

import shutil
from pathlib import Path

from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context

_CHOICE_PROMPT = "  (o)verwrite / (s)kip / (b)ackup and copy? [o/s/b]: "


def _copy(src: Path, dst: Path) -> None:
    """
    Copy like `cp` / `cp -R`: into `dst` when it is an existing directory.

    :param src: file or directory to copy
    :param dst: destination path or existing directory
    """
    target = dst / src.name if dst.is_dir() else dst
    if src.is_dir():
        shutil.copytree(src, target, symlinks=True)
    else:
        shutil.copy(src, target)


def _remove(path: Path) -> None:
    """
    Remove a file, link or directory tree like `rm -rf`.

    :param path: what to remove
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def safe_copy(
    ctx: Context, console: Console, prompter: Prompter, src: Path, dst: Path
) -> str:
    """
    Copy a file or directory, asking before replacing an existing destination.

    :param ctx: installer context, for the backup date
    :param console: where messages go
    :param prompter: where the overwrite/skip/backup answer comes from
    :param src: file or directory to copy
    :param dst: destination path, or an existing directory to copy into
    :return str: `"copied"` or `"skipped"`
    """
    effective = dst / src.name if dst.is_dir() else dst
    if not effective.exists():
        _copy(src, dst)
        return "copied"

    console.print_msg("WARNING", f"Destination already exists: {effective}")
    while True:
        answer = prompter.ask(_CHOICE_PROMPT)
        if answer is None:
            console.print_msg(
                "INFO", f"No interactive input available; skipping: {effective}"
            )
            return "skipped"
        choice = answer.strip().lower()
        if choice == "o":
            _remove(effective)
            _copy(src, dst)
            console.print_msg("INFO", f"Overwritten: {effective}")
            return "copied"
        if choice == "s":
            console.print_msg("INFO", f"Skipped: {effective}")
            return "skipped"
        if choice == "b":
            backup = Path(f"{effective}.bak.{ctx.backup_date}")
            shutil.move(str(effective), str(backup))
            console.print_msg("INFO", f"Backed up to: {backup}")
            _copy(src, dst)
            return "copied"
        console.print_msg("OTHER", "  Invalid choice. Please enter o, s, or b.")


def backup_afc_config(ctx: Context) -> None:
    """
    Move the AFC config directory to `AFC.backup.<backup_date>` if it exists.

    :param ctx: installer context
    """
    if ctx.afc_config_dir.is_dir():
        backup = ctx.printer_config_dir / f"AFC.backup.{ctx.backup_date}"
        shutil.move(str(ctx.afc_config_dir), str(backup))


def remove_vars_tool_file(ctx: Context) -> None:
    """
    Delete `AFC/*.tool` files, like `rm -f`; directories and others are kept.

    :param ctx: installer context
    """
    if not ctx.afc_config_dir.is_dir():
        return
    for path in ctx.afc_config_dir.glob("*.tool"):
        if not path.is_dir():
            path.unlink()
