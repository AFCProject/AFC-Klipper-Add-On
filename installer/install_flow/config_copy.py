# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Copy the base AFC configuration into the printer's config directory."""
from __future__ import annotations

from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.fsops import safe_copy


def copy_config(ctx: Context, console: Console, prompter: Prompter) -> None:
    """
    Copy `AFC.cfg`, `AFC_Macro_Vars.cfg` and the macros into the AFC config directory.

    OpenAMS uses the same copy. Each copy asks before replacing something.

    :param ctx: installer context
    :param console: where messages go
    :param prompter: used when a destination already exists
    """
    cfg = ctx.afc_config_dir
    source = ctx.afc_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    safe_copy(ctx, console, prompter, source / "AFC.cfg", cfg)
    safe_copy(ctx, console, prompter, source / "AFC_Macro_Vars.cfg", cfg)
    (cfg / "mcu").mkdir(exist_ok=True)
    safe_copy(ctx, console, prompter, source / "macros", cfg)
