# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Version lookup and the dirty work tree check (get_git_version, check_for_uncommitted_changes)."""
from __future__ import annotations

from installer.platform_core.console import Console
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.platform_core.system import CommandRunner

_VERSION_KEY = "AFC_VERSION="


def get_git_version(ctx: Context, runner: CommandRunner) -> str:
    """
    Build the version string from ``extras/AFC.py``, plus the short hash for a git checkout.

    A missing ``AFC_VERSION`` line gives an empty version.

    :param ctx: installer context
    :param runner: command runner
    :return str: version, or ``version-hash`` when the checkout is a git clone
    """
    version = ""
    try:
        text = (ctx.afc_path / "extras" / "AFC.py").read_text(errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        if _VERSION_KEY in line:
            version = line.split("=")[1].replace(" ", "").replace('"', "")
            break
    if not (ctx.afc_path / ".git").is_dir():
        return version
    result = runner.run(["git", "-C", str(ctx.afc_path), "rev-parse", "--short", "HEAD"])
    if result.returncode != 0:
        raise InstallerExit(1)
    return f"{version}-{result.stdout.rstrip(chr(10))}"


def check_for_uncommitted_changes(ctx: Context, console: Console, runner: CommandRunner) -> None:
    """
    Exit with recovery hints when the work tree or index has uncommitted changes.

    :param ctx: installer context
    :param console: output
    :param runner: command runner
    """
    console.print_msg("SUCCESS", "→ Checking for uncommitted changes…")
    path = str(ctx.afc_path)
    dirty = runner.run(["git", "-C", path, "diff", "--quiet"]).returncode != 0
    if not dirty:
        dirty = runner.run(["git", "-C", path, "diff", "--quiet", "--cached"]).returncode != 0
    if not dirty:
        return
    for line in (
        f"❌ You have uncommitted changes in {path}.",
        "   Please commit or stash them before running this script.",
        "",
        "💡 To discard your changes and reset the branch:",
        f'   cd "{path}"',
        f"   git checkout {ctx.branch}",
        f"   git reset --hard origin/{ctx.branch}",
        "   git clean -fd",
    ):
        console.print_msg("SUCCESS", line)
    raise InstallerExit(1)
