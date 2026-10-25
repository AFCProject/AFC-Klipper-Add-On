# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Buffer configuration: building, wiring into a unit, and the pin prompts."""
from __future__ import annotations

import re
from pathlib import Path
from string import Template
from typing import List, Optional, Tuple

from installer.config_editor.blocks import (
    remove_section_block,
    replace_section_block,
    section_names,
    set_key_in_section,
)
from installer.config_editor.document import ConfigDocument
from installer.config_editor.edits import ensure_trailing_newline
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.platform_core.fsops import safe_copy
from installer.unit_registry.base import BufferTarget
from installer.unit_registry.registry import unit_for
from installer.unit_registry.selection import UnitSelection

# Block text kept verbatim; one line is well over 100 characters.
TEMPLATE_DIR = Path(__file__).resolve().parent / "buffer_templates"
_TEMPLATES = {
    "TurtleNeck": "turtleneck",
    "TurtleNeckV2": "turtleneck_v2",
    "TurtleNeck2Remastered": "turtleneck_2_remastered",
    "FPS_PSF": "fps_psf",
}
# Buffer types that ship an mcu file, copied next to the config and included once.
_MCU_FILES = {
    "TurtleNeckV2": "TurtleNeckv2.cfg",
    "TurtleNeck2Remastered": "TN2.cfg",
}
_SHARE_PROMPT = "Enter the number or name of the buffer to share (blank to skip): "


def build_buffer_config(
    buffer_type: str,
    unit_name: str,
    advance_pin: str = "",
    trailing_pin: str = "",
    adc_pin: str = "",
) -> str:
    """
    The text of an ``[AFC_buffer <name>]`` block, without a trailing newline.

    :param buffer_type: ``TurtleNeck``, ``TurtleNeckV2``, ``TurtleNeck2Remastered`` or ``FPS_PSF``
    :param unit_name: section name
    :param advance_pin: TurtleNeck advance pin
    :param trailing_pin: TurtleNeck trailing pin
    :param adc_pin: FPS_PSF ADC pin
    :return str: the block
    :raises ValueError: for any other buffer type
    """
    if buffer_type not in _TEMPLATES:
        error_str = f"Invalid BUFFER_SYSTEM: {buffer_type}"
        raise ValueError(error_str)
    text = (TEMPLATE_DIR / f"{_TEMPLATES[buffer_type]}.tmpl").read_text()
    return Template(text).substitute(
        unit_name=unit_name,
        advance_pin=advance_pin,
        trailing_pin=trailing_pin,
        adc_pin=adc_pin,
    )


def _ask(prompter: Prompter, prompt: str) -> str:
    """
    One trimmed answer; end of input stops the installer.

    :param prompter: where answers come from
    :param prompt: prompt text
    :return str: the answer without surrounding whitespace
    """
    answer = prompter.ask(prompt)
    if answer is None:
        raise InstallerExit(1)
    return answer.strip()


def query_tn_pins(
    console: Console, prompter: Prompter, buffer_name: str, unit_name: str = "Turtle_1"
) -> Tuple[str, str]:
    """
    Ask for the TurtleNeck advance and trailing pins; blank keeps each default.

    :param console: where the explanation goes
    :param prompter: where answers come from
    :param buffer_name: name shown in the messages
    :param unit_name: unit whose name forms the default pins
    :return Tuple[str, str]: advance pin, trailing pin
    """
    advance = f"^{unit_name}:TN_ADV"
    trailing = f"^{unit_name}:TN_TRL"
    console.print_msg(
        "INFO", f"\nPlease enter the pin numbers for the TurtleNeck buffer '{buffer_name}':"
    )
    console.print_msg("INFO", "(Leave blank to use the default values)")
    console.print_msg("INFO", "Ensure you use a pull-up '^' if you are using a AFC end stop pin.")
    console.print_msg("INFO", f"(Default: ^{unit_name}:TN_ADV)")
    console.print_msg("INFO", f"(Default: ^{unit_name}:TN_TRL)")
    advance = _ask(prompter, f"  Enter the advance pin (default: {advance}): ") or advance
    trailing = _ask(prompter, f"  Enter the trailing pin (default: {trailing}): ") or trailing
    console.print_msg("INFO", f"Set {buffer_name} Advance pin: {advance}")
    console.print_msg("INFO", f"Set {buffer_name} Trailing pin: {trailing}")
    return advance, trailing


def query_fps_pin(
    console: Console,
    prompter: Prompter,
    buffer_name: str,
    unit_name: str = "Turtle_1",
    default_pin: str = "",
) -> str:
    """
    Ask for the FPS/PSF sensor's ADC pin; blank keeps the default.

    :param console: where the explanation goes
    :param prompter: where answers come from
    :param buffer_name: name shown in the messages
    :param unit_name: unit whose name forms the default pin
    :param default_pin: default pin, or empty for ``<unit>:PSF``
    :return str: the ADC pin
    """
    default = default_pin or f"{unit_name}:PSF"
    console.print_msg(
        "INFO", f"\nPlease enter the ADC pin for the FPS/PSF buffer '{buffer_name}':"
    )
    console.print_msg("INFO", "(Leave blank to use the default value)")
    console.print_msg("INFO", "Proportional Sync-Feedback (PSF) sensor is wired to.")
    console.print_msg("INFO", f"(Default: {default})")
    pin = _ask(prompter, f"  Enter the ADC pin (default: {default}): ") or default
    console.print_msg("INFO", f"Set {buffer_name} ADC pin: {pin}")
    return pin


def append_buffer_config(
    ctx: Context,
    console: Console,
    prompter: Prompter,
    buffer_type: str,
    unit_name: str,
    target_file: Path,
    advance_pin: str = "",
    trailing_pin: str = "",
    adc_pin: str = "",
) -> None:
    """
    Add a buffer block to a config file, replacing a block with the same header.

    TurtleNeckV2 and TurtleNeck2Remastered also copy their mcu file and add its include
    line once.

    :param ctx: installer context
    :param console: where messages go
    :param prompter: used if the mcu file already exists
    :param buffer_type: ``TurtleNeck``, ``TurtleNeckV2``, ``TurtleNeck2Remastered`` or ``FPS_PSF``
    :param unit_name: section name for the buffer
    :param target_file: file to write into (created if missing)
    :param advance_pin: TurtleNeck advance pin
    :param trailing_pin: TurtleNeck trailing pin
    :param adc_pin: FPS_PSF ADC pin
    """
    config = build_buffer_config(buffer_type, unit_name, advance_pin, trailing_pin, adc_pin)
    if not target_file.exists():
        target_file.write_text("")
    header = config.split("\n", 1)[0]
    doc = ConfigDocument.load(target_file)
    if any(line.rstrip("\n") == header for line in doc.lines):
        replace_section_block(target_file, header, config)
    else:
        ensure_trailing_newline(target_file)
        doc = ConfigDocument.load(target_file)
        doc.append_text(f"\n{config}\n")
        doc.save()
    mcu_file = _MCU_FILES.get(buffer_type)
    if mcu_file is not None:
        include = f"[include mcu/{mcu_file}]"
        mcu_dir = ctx.afc_config_dir / "mcu"
        mcu_dir.mkdir(parents=True, exist_ok=True)
        source = ctx.afc_path / "config" / "mcu" / mcu_file
        safe_copy(ctx, console, prompter, source, mcu_dir / mcu_file)
        doc = ConfigDocument.load(target_file)
        if not any(include in line for line in doc.lines):
            ensure_trailing_newline(target_file)
            doc = ConfigDocument.load(target_file)
            doc.append_text(f"\n{include}\n")
            doc.save()


def _target_for(ctx: Context, sel: UnitSelection) -> Optional[BufferTarget]:
    """
    Where this unit's buffer is wired in, or None if its type has no target.

    :param ctx: installer context
    :param sel: the user's unit choices
    :return Optional[BufferTarget]: the target
    """
    unit = unit_for(sel.installation_type)
    return unit.buffer_target(ctx, sel) if unit is not None else None


def apply_unit_buffer(
    ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection, buffer_type: str
) -> None:
    """
    Give a unit its own TurtleNeck, TurtleNeckV2, TurtleNeck2Remastered or FPS_PSF buffer.

    Other buffer types do nothing. A unit type without a target is skipped with a
    warning. Pin prompts default from the unit name (EMU and OpenAMS have their own
    FPS defaults).

    :param ctx: installer context
    :param console: where messages go
    :param prompter: where pin answers come from
    :param sel: the user's unit choices
    :param buffer_type: the buffer to apply
    """
    if buffer_type not in _TEMPLATES:
        return
    target = _target_for(ctx, sel)
    if target is None:
        console.print_msg(
            "WARNING",
            "PSF buffer selected but no unit target is known for installation type "
            f"'{sel.installation_type}'; skipping.",
        )
        return
    advance = trailing = adc = ""
    if buffer_type == "TurtleNeck":
        advance, trailing = query_tn_pins(console, prompter, "TN", target.unit_name)
    elif buffer_type == "FPS_PSF":
        default = ""
        if sel.installation_type == "EMU":
            default = f"{target.unit_name}_lane1:TN"
        elif sel.installation_type == "OpenAMS":
            default = "fps:PA2"
        adc = query_fps_pin(console, prompter, "FPS_PSF", target.unit_name, default)
    append_buffer_config(
        ctx, console, prompter, buffer_type, target.section_name, target.extruder_file,
        advance, trailing, adc,
    )
    header = f"[{target.unit_section_prefix} {target.unit_name}]"
    set_key_in_section(target.extruder_file, header, "buffer", target.section_name)


def apply_shared_buffer(
    ctx: Context, console: Console, sel: UnitSelection, shared_name: str
) -> None:
    """
    Point a unit at an existing buffer and drop its now-unused pre-baked buffer section.

    :param ctx: installer context
    :param console: where the warning goes
    :param sel: the user's unit choices
    :param shared_name: name of the existing ``[AFC_buffer <name>]`` section
    """
    target = _target_for(ctx, sel)
    if target is None:
        console.print_msg(
            "WARNING",
            f"No unit target is known for installation type '{sel.installation_type}'; skipping.",
        )
        return
    header = f"[{target.unit_section_prefix} {target.unit_name}]"
    set_key_in_section(target.extruder_file, header, "buffer", shared_name)
    if target.prebaked_header and target.prebaked_header != f"[AFC_buffer {shared_name}]":
        remove_section_block(target.extruder_file, target.prebaked_header)


def list_existing_buffers(ctx: Context) -> List[str]:
    """
    Names of every buffer defined in the top-level ``*.cfg`` files of the AFC directory.

    :param ctx: installer context
    :return List[str]: sorted unique names
    """
    return section_names(sorted(ctx.afc_config_dir.glob("*.cfg")), "AFC_buffer")


def prompt_shared_buffer_name(ctx: Context, console: Console, prompter: Prompter) -> str:
    """
    Ask which existing buffer a new unit should share.

    A number picks from the list, an exact name picks that buffer, and a blank answer or
    end of input skips (unlike other prompts, which stop the installer).

    :param ctx: installer context
    :param console: where the list and warnings go
    :param prompter: where the answer comes from
    :return str: the chosen buffer name, or an empty string to skip
    """
    buffers = list_existing_buffers(ctx)
    if not buffers:
        console.print_msg(
            "WARNING",
            f"No existing buffers found in {ctx.afc_config_dir}; set 'buffer:' manually.",
        )
        return ""
    console.print_msg("OTHER", "")
    console.print_msg("OTHER", "Existing buffers:")
    for number, name in enumerate(buffers, start=1):
        console.print_msg("OTHER", f"  {number}. {name}")
    while True:
        answer = prompter.ask(_SHARE_PROMPT)
        choice = (answer or "").strip()
        if not choice:
            return ""
        chosen = ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice)
            chosen = buffers[index - 1] if 1 <= index <= len(buffers) else ""
        elif choice in buffers:
            chosen = choice
        if chosen:
            return chosen
        console.print_msg("WARNING", f"'{choice}' is not one of the existing buffers.")
