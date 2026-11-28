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

from installer.config_editor.blocks import replace_section_block
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

    Existing files are replaced without asking. Boards other than `SLB` and `EMB_C`
    are treated as `EBB`. EMB-C uses one MCU and supports two to five lanes.

    :param ctx: installer context
    :param unit_name: unit name used throughout the generated config
    :param num_lanes: number of lanes (EBB/SLB allow zero for an empty unit)
    :param board_type: `SLB`, `EMB_C`, or anything else for `EBB`
    :raises ValueError: when EMB-C has fewer than two or more than five lanes
    """
    emb_c = board_type == "EMB_C"
    if (emb_c
        and num_lanes not in range(2, 6)):
        error_str = "EMB-C supports 2 to 5 lanes"
        raise ValueError(error_str)
    cfg = ctx.afc_config_dir
    (cfg / "mcu").mkdir(parents=True, exist_ok=True)
    lanes = range(1, num_lanes + 1)
    mcu_names = [unit_name] if emb_c else [f"{unit_name}_lane{i}" for i in lanes]
    mcu_list = ", ".join(mcu_names)

    mcu_template = {"SLB": "mcu_slb", "EMB_C": "mcu_emb_c"}.get(board_type, "mcu_ebb")
    mcu_text = _render(mcu_template, unit_name=unit_name, mcu_list=mcu_list)
    (cfg / "mcu" / f"{unit_name}.cfg").write_text(mcu_text)

    parts: List[str] = [_render("header", unit_name=unit_name)]
    parts += [_render("lane_mcu", mcu_name=name) for name in mcu_names]
    parts.append(_render("unit", unit_name=unit_name))
    if emb_c:
        parts.append(_render("temperature_emb_c", unit_name=unit_name) + "\n")
    lane_template = {"SLB": "lane_slb", "EMB_C": "lane_emb_c"}.get(board_type, "lane_ebb")
    parts += [_render(lane_template, unit_name=unit_name, i=i, uart_index=min(i, 4),
                      uart_address=int(i == 5))
              + ("\n" if emb_c else "") for i in lanes]
    parts.append(_render("buffer_hub", unit_name=unit_name))
    unit_file = cfg / f"AFC_{unit_name}.cfg"
    unit_file.write_text("".join(parts))
    if emb_c:
        from installer.unit_registry.buffers import build_buffer_config

        buffer_name = f"{unit_name}_buffer"
        buffer_config = build_buffer_config("FPS_PSF", buffer_name, adc_pin=f"{unit_name}:TH1")
        replace_section_block(unit_file, f"[AFC_buffer {buffer_name}]", buffer_config)
