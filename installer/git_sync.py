# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Branch switching and self update (the -b early switch and clone_and_maybe_restart)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List

from installer.platform_core.console import Console
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.platform_core.system import CommandRunner, run_or_exit
from installer.update_flow.version import check_for_uncommitted_changes

Reexec = Callable[[], None]


def make_reexec(script: Path, original_args: List[str]) -> Reexec:
    """
    Build the function that replaces this process with the shim, so new code is loaded.

    :param script: the shim (resolved path) that started this run
    :param original_args: the arguments it was started with
    :return Reexec: a function that does not return
    """

    def reexec() -> None:
        os.execv(str(script), [str(script), *original_args])

    return reexec


def _git(path: Path, *args: str) -> List[str]:
    return ["git", "-C", str(path), *args]


def switch_branch_early(
    afc_path: Path, branch: str, console: Console, runner: CommandRunner, reexec: Reexec
) -> None:
    """
    Switch the checkout to the branch given with `-b` before anything else is loaded.

    Nothing happens without a branch, without a `.git` directory, or when already on it.
    After a switch the script restarts itself so the new branch's code runs.

    :param afc_path: the AFC checkout
    :param branch: requested branch, empty if none
    :param console: where messages go
    :param runner: command runner
    :param reexec: restarts the script with its original arguments
    """
    if not branch or not (afc_path / ".git").is_dir():
        return
    current = runner.run(_git(afc_path, "rev-parse", "--abbrev-ref", "HEAD"))
    if current.returncode != 0:
        raise InstallerExit(current.returncode)
    if current.stdout.rstrip("\n") == branch:
        return
    console.print_msg("OTHER", f"→ Switching to branch '{branch}' before loading…")
    run_or_exit(runner, _git(afc_path, "fetch", "--prune", "--quiet"))
    remote = runner.run(_git(afc_path, "rev-parse", "--verify", "--quiet", f"origin/{branch}"))
    if remote.returncode != 0:
        local = runner.run(_git(afc_path, "rev-parse", "--verify", "--quiet", branch))
        if local.returncode != 0:
            console.print_msg(
                "OTHER", f"✗ Branch '{branch}' does not exist locally or on the remote."
            )
            raise InstallerExit(1)
    run_or_exit(runner, _git(afc_path, "checkout", "--quiet", branch))
    runner.run(_git(afc_path, "pull", "--rebase", "--quiet"))
    console.print_msg("OTHER", "✓ Branch switched. Restarting script…")
    reexec()


def clone_and_maybe_restart(
    ctx: Context, console: Console, runner: CommandRunner, reexec: Reexec
) -> None:
    """
    Clone the repository, or update an existing checkout and restart when it moved.

    A non-empty directory without `.git` is treated as a ZIP install and left alone.

    :param ctx: installer context, `git_install` is cleared for a ZIP install
    :param console: where messages go
    :param runner: command runner
    :param reexec: restarts the script with its original arguments
    """
    path = ctx.afc_path
    if not (path / ".git").is_dir():
        if path.is_dir() and any(path.iterdir()):
            ctx.git_install = False
            console.print_msg(
                "WARNING", "AFC-Klipper-Add-On appears to have been installed via ZIP file."
            )
            console.print_msg(
                "WARNING",
                f"Updates are applied by extracting a fresh archive over {path}.",
            )
            return
        console.print_msg("OTHER", f"→ Cloning {ctx.branch} from {ctx.gitrepo} into {path}…")
        run_or_exit(
            runner,
            ["git", "clone", "--branch", ctx.branch, "--single-branch", "--depth", "1",
             ctx.gitrepo, str(path)],
        )
        console.print_msg("OTHER", "✓ Clone complete.")
        return
    console.print_msg("OTHER", f"→ Switching to branch '{ctx.branch}'…")
    run_or_exit(runner, _git(path, "checkout", "--quiet", ctx.branch))
    check_for_uncommitted_changes(ctx, console, runner)
    console.print_msg("OTHER", f"→ Fetching updates in {path}…")
    run_or_exit(runner, _git(path, "fetch", "--prune", "--quiet"))
    status = runner.run(_git(path, "status", "--porcelain", "--branch"))
    if "behind" in status.stdout:
        console.print_msg("OTHER", "→ New commits detected; rebasing…")
        run_or_exit(runner, _git(path, "pull", "--rebase", "--quiet"))
        console.print_msg("OTHER", "✓ Update applied. Restarting script…")
        reexec()
    else:
        console.print_msg("OTHER", "✓ Already up to date.")
