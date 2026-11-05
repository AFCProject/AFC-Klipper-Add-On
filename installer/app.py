# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The install, update and U1 programs."""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Mapping, Optional

from installer.actions import RealActions
from installer.cli import build_context, parse_args, requested_branch
from installer.git_sync import Reexec, make_reexec, switch_branch_early
from installer.install_flow.snapmaker import u1_disable, u1_enable
from installer.platform_core.console import Console, Prompter
from installer.platform_core.errors import InstallerExit
from installer.platform_core.system import CommandRunner
from installer.startup import install_startup, update_startup
from installer.ui_menus.menus import MENUS
from installer.ui_menus.navigation import MenuDeps, run_menus
from installer.ui_menus.renderer import Renderer
from installer.ui_menus.rich_renderer import choose_renderer
from installer.ui_menus.state import MenuState
from installer.unit_registry.selection import UnitSelection

SCRIPT_DIR = Path(__file__).resolve().parents[1]
_U1_HOME = Path("/home/lava")
_INTERRUPTED = 130


@dataclass
class AppDeps:
    """Everything the programs touch, so tests can swap it out."""

    console: Console = field(default_factory=Console)
    prompter: Prompter = field(default_factory=Prompter)
    runner: CommandRunner = field(default_factory=CommandRunner)
    script_dir: Path = SCRIPT_DIR
    home: Optional[Path] = None
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    sleep: Callable[[float], None] = time.sleep
    reexec: Optional[Reexec] = None
    renderer: Optional[Renderer] = None
    euid: Optional[int] = None


def _prog(deps: AppDeps, default: str) -> str:
    return deps.env.get("AFC_PROG") or default


def _reexec(deps: AppDeps, argv: List[str], default_shim: str) -> Reexec:
    if deps.reexec is not None:
        return deps.reexec
    shim = deps.env.get("AFC_SHIM") or str(deps.script_dir / default_shim)
    return make_reexec(Path(shim), argv)


def _run_menus(deps: AppDeps, state: MenuState, start: str) -> None:
    renderer = deps.renderer or choose_renderer(deps.console, sys.stdout.isatty(), deps.env)
    actions = RealActions(deps.console, deps.prompter, deps.runner)
    run_menus(start, state, MenuDeps(deps.console, deps.prompter, renderer, actions), MENUS)


def run_install(argv: List[str], deps: Optional[AppDeps] = None) -> int:
    """
    Run the installer (install-afc.sh): optional branch switch, startup checks, main menu.

    :param argv: arguments after the command name
    :param deps: collaborators, defaults to the real ones
    :return int: process exit code
    """
    deps = deps or AppDeps()
    reexec = _reexec(deps, argv, "install-afc.sh")
    try:
        switch_branch_early(
            deps.script_dir, requested_branch(argv), deps.console, deps.runner, reexec
        )
        args = parse_args(argv, _prog(deps, "install-afc.sh"))
        ctx = build_context(args, deps.script_dir, deps.home or Path.home(), argv)
        install_startup(
            ctx, deps.console, deps.prompter, deps.runner, reexec, deps.sleep
        )
        _run_menus(deps, MenuState(ctx=ctx, selection=UnitSelection()), "main")
    except InstallerExit as exc:
        return exc.code
    except KeyboardInterrupt:
        return _INTERRUPTED
    return 0


def run_update(argv: List[str], deps: Optional[AppDeps] = None) -> int:
    """
    Run the updater (update-afc.sh): startup checks, then the update menu.

    :param argv: arguments after the command name
    :param deps: collaborators, defaults to the real ones
    :return int: process exit code
    """
    deps = deps or AppDeps()
    reexec = _reexec(deps, argv, "update-afc.sh")
    try:
        args = parse_args(argv, _prog(deps, "update-afc.sh"))
        ctx = build_context(args, deps.script_dir, deps.home or Path.home(), argv)
        update_startup(ctx, deps.console, deps.runner, reexec, deps.sleep)
        _run_menus(deps, MenuState(ctx=ctx, selection=UnitSelection()), "update")
    except InstallerExit as exc:
        return exc.code
    except KeyboardInterrupt:
        return _INTERRUPTED
    return 0


def run_u1(argv: List[str], deps: Optional[AppDeps] = None) -> int:
    """
    Run the Snapmaker U1 firmware actions: `-e` enables AFC, `-d` disables it.

    Flags run in the order given and parsing stops at the first non-flag. Root runs with the
    `lava` user's home, as the firmware page does.

    :param argv: arguments after the command name
    :param deps: collaborators, defaults to the real ones
    :return int: process exit code
    """
    deps = deps or AppDeps()
    euid = os.geteuid() if deps.euid is None else deps.euid
    home = deps.home or (_U1_HOME if euid == 0 else Path.home())
    ctx = build_context(parse_args([], "u1"), deps.script_dir, home, argv)
    try:
        for word in argv:
            if word == "--" or len(word) < 2 or not word.startswith("-"):
                break
            for flag in word[1:]:
                if flag == "e":
                    u1_enable(ctx, deps.console, deps.runner, euid)
                elif flag == "d":
                    u1_disable(ctx, deps.console, deps.runner, euid)
                else:
                    sys.stderr.write(f"u1: illegal option -- {flag}\n")
                    return 1
    except InstallerExit as exc:
        return exc.code
    return 0
