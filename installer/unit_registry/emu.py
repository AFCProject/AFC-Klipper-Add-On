# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Generate the EMU unit and mcu config files."""
from __future__ import annotations

from pathlib import Path
from string import Template
from typing import List

from installer.platform_core.context import Context

# The text lives in data files. Their
# `${unit_name}` style placeholders are string.Template syntax, so nothing is rewritten.
TEMPLATE_DIR = Path(__file__).resolve().parent / "emu_templates"


def _render(name: str, **values: object) -> str:
    """
    Fill in one template file.

    :param name: template file name without the `.tmpl` suffix
    :param values: placeholder values
    :return str: the rendered text
    """
    text = (TEMPLATE_DIR / f"{name}.tmpl").read_text()
    return Template(text).substitute(**values)


def generate_emu_config(ctx: Context, unit_name: str, num_lanes: int, board_type: str) -> None:
    """
    Write `mcu/<unit>.cfg` and `AFC_<unit>.cfg` for an EMU with the given lanes.

    Existing files are replaced without asking. Any board
    other than `SLB` (including an empty one) is treated as `EBB`.

    :param ctx: installer context
    :param unit_name: unit name used throughout the generated config
    :param num_lanes: number of lanes (0 produces a unit with no lanes)
    :param board_type: `SLB`, or anything else for `EBB`
    """
    cfg = ctx.afc_config_dir
    (cfg / "mcu").mkdir(parents=True, exist_ok=True)
    slb = board_type == "SLB"
    lanes = range(1, num_lanes + 1)
    mcu_list = ", ".join(f"{unit_name}_lane{i}" for i in lanes)

    mcu_text = _render("mcu_slb" if slb else "mcu_ebb", unit_name=unit_name, mcu_list=mcu_list)
    (cfg / "mcu" / f"{unit_name}.cfg").write_text(mcu_text)

    parts: List[str] = [_render("header", unit_name=unit_name)]
    parts += [_render("lane_mcu", unit_name=unit_name, i=i) for i in lanes]
    parts.append(_render("unit", unit_name=unit_name))
    lane_template = "lane_slb" if slb else "lane_ebb"
    parts += [_render(lane_template, unit_name=unit_name, i=i) for i in lanes]
    parts.append(_render("buffer_hub", unit_name=unit_name))
    (cfg / f"AFC_{unit_name}.cfg").write_text("".join(parts))
