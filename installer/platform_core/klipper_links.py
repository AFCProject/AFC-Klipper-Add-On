# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Link AFC's Python extras into Klipper."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Set

from installer.platform_core.console import Console
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.platform_core.system import CommandRunner

_STOCK_INIT_HEADER = (
    "# Package definition for the extras directory\n"
    "#\n"
    "# Copyright (C) 2018  Kevin O'Connor <kevin@koconnor.net>\n"
    "#\n"
    "# This file may be distributed under the terms of the GNU GPLv3 license.\n"
)


def _extension_files(ctx: Context) -> List[Path]:
    """
    AFC's extras/*.py files, skipping any whose name ends in `__init__.py`.

    :param ctx: installer context
    :return List[Path]: sorted extension paths
    """
    extras = ctx.afc_path / "extras"
    return sorted(p for p in extras.glob("*.py") if not p.name.endswith("__init__.py"))


def link_extensions(ctx: Context) -> Optional[str]:
    """
    Symlink every AFC extension into Klipper's extras directory, replacing old ones.

    The returned "not installed" message is for the caller to keep; it is never shown
    to the user.

    :param ctx: installer context
    :return Optional[str]: a message when the Klipper extras directory is missing
    """
    extras_dir = ctx.klipper_extra_path
    if not extras_dir.is_dir():
        return "AFC Klipper extensions not installed; Klipper extras directory not found."
    for source in _extension_files(ctx):
        dest = extras_dir / source.name
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        dest.symlink_to(ctx.afc_path / "extras" / source.name)
    return None


def unlink_extensions(ctx: Context, console: Console) -> None:
    """
    Remove AFC's extension files from Klipper's extras directory, like `rm -f`.

    :param ctx: installer context
    :param console: where the error message goes
    """
    extras_dir = ctx.klipper_extra_path
    if not extras_dir.is_dir():
        console.print_msg(
            "ERROR",
            "AFC Klipper extensions not uninstalled; Klipper extras directory not found.",
        )
        raise InstallerExit(1)
    for source in _extension_files(ctx):
        (extras_dir / source.name).unlink(missing_ok=True)


def exclude_from_klipper_git(ctx: Context, console: Console) -> None:
    """
    Add AFC's extension files to Klipper's `.git/info/exclude`.

    Walks `extras/` recursively and flattens each file to `klippy/extras/<name>`,
    skipping `__init__.py`. Entries are matched as whole lines. A last line without a
    newline gets the first new entry appended to it.

    :param ctx: installer context
    :param console: where the error message goes
    """
    exclude_file = ctx.klipper_dir / ".git" / "info" / "exclude"
    try:
        existing: Set[str] = set(exclude_file.read_text().splitlines())
    except FileNotFoundError:
        existing = set()
    candidates = sorted((ctx.afc_path / "extras").rglob("*.py"))
    for path in candidates:
        if path.is_symlink() or not path.is_file() or path.name.endswith("__init__.py"):
            continue
        entry = f"klippy/extras/{path.name}"
        if entry in existing:
            continue
        try:
            with open(exclude_file, "a") as handle:
                handle.write(f"{entry}\n")
        except OSError as exc:
            console.print_msg("ERROR", f"Unable to update {exclude_file}: {exc.strerror}")
            raise InstallerExit(1) from exc
        existing.add(entry)


def check_init_symlink(ctx: Context, runner: CommandRunner) -> None:
    """
    Repair `klippy/extras/__init__.py` if it was once symlinked to AFC's own.

    The link is removed and restored from Klipper's git checkout. If git cannot do
    that, the stock header file is written instead.

    :param ctx: installer context
    :param runner: used to run git
    """
    link = ctx.klipper_extra_path / "__init__.py"
    target = ctx.afc_path / "extras" / "__init__.py"
    if link.is_symlink() and link.resolve() == target.resolve():
        link.unlink()
        result = runner.run(
            ["git", "-C", str(ctx.klipper_dir), "checkout", "--", "klippy/extras/__init__.py"]
        )
        if result.returncode != 0:
            link.write_text(_STOCK_INIT_HEADER)
