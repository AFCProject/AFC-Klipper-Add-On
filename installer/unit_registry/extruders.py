# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Detecting the printer's extruders and wiring the chosen one into a unit."""
from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Set

from installer.config_editor.document import ConfigDocument
from installer.config_editor.edits import ensure_trailing_newline
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.unit_registry.registry import unit_for
from installer.unit_registry.selection import UnitSelection

_EXTRUDER_HEADER = re.compile(r"^\[(extruder\d*)\]\s*(?:[;#].*)?$")
_INCLUDE = re.compile(r"^\[include\s+(.+?)\]\s*(?:[;#].*)?$")
_AFC_EXTRUDER_HEADER = re.compile(r"^\[AFC_extruder\s+([^\]]+)\]\s*(?:[;#].*)?$")
_EXTRUDER_NAME_OPTION = re.compile(r"^extruder_name\s*:\s*(\S+)")
_EXTRUDER_OPTION_LINE = re.compile(r"^[ \t]*extruder[ \t]*:[ \t]*(\S+)[ \t]*(#.*)?$")
# Matches the interior width of the menu boxes in installer.ui_menus.view.BOX_WIDTH.
_NOTICE_WRAP_WIDTH = 85
_FALLBACK_AFC_EXTRUDER_BODY: List[str] = [
    "pin_tool_start: enter_tool_pin",
    "#pin_tool_end: None",
    "tool_stn: 72",
    "tool_stn_unload: 100",
    "tool_sensor_after_extruder: 0",
    "tool_unload_speed: 25",
    "tool_load_speed: 25",
]


def find_printer_extruders(ctx: Context) -> List[str]:
    """
    List every `[extruder]`/`[extruderN]` section in the user's printer config.

    Follows `[include ...]` lines the same way Klipper does (globs included), so an
    extruder defined in an included file is also found. A missing printer.cfg yields
    an empty list.

    :param ctx: installer context
    :return List[str]: extruder section names in the order they're first found, for
        example `["extruder", "extruder1"]`
    """
    found: List[str] = []
    _scan_file(ctx.printer_config_dir / "printer.cfg", found, set())
    return found


def _scan_file(path: Path, found: List[str], visited: Set[Path]) -> None:
    """
    Scan one config file for extruder headers, following its own includes.

    :param path: file to scan, ignored if it doesn't exist
    :param found: extruder section names found so far, extended in place
    :param visited: resolved paths already scanned, extended in place
    """
    if not path.is_file():
        return
    resolved = path.resolve()
    if resolved in visited:
        return
    visited.add(resolved)
    for line in path.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        header_match = _EXTRUDER_HEADER.match(stripped)
        if header_match:
            name = header_match.group(1)
            if name not in found:
                found.append(name)
            continue
        include_match = _INCLUDE.match(stripped)
        if include_match:
            for included in sorted(path.parent.glob(include_match.group(1))):
                _scan_file(included, found, visited)


def find_afc_extruder_sections(ctx: Context) -> Dict[str, str]:
    """
    Every `[AFC_extruder <name>]` section in the AFC config directory, mapped to the
    real printer extruder it targets.

    The target is the section's own `extruder_name:` value, or the section's own
    name when that option is absent, matching `AFC_extruder`'s own default
    (`config.get("extruder_name", self.name)`).

    :param ctx: installer context
    :return Dict[str, str]: `{section_name: target_extruder_name}`
    """
    sections: Dict[str, str] = {}
    for path in sorted(ctx.afc_config_dir.glob("*.cfg")):
        current: Optional[str] = None
        for line in path.read_text(errors="ignore").splitlines():
            stripped = line.strip()
            header_match = _AFC_EXTRUDER_HEADER.match(stripped)
            if header_match:
                current = header_match.group(1)
                sections[current] = current
                continue
            if current is None:
                continue
            if stripped.startswith("["):
                current = None
                continue
            name_match = _EXTRUDER_NAME_OPTION.match(stripped)
            if name_match:
                sections[current] = name_match.group(1)
    return sections


def section_for_extruder(ctx: Context, target: str) -> Optional[str]:
    """
    The name of the `[AFC_extruder <name>]` section that targets a real extruder.

    :param ctx: installer context
    :param target: real printer extruder, e.g. `extruder1`
    :return Optional[str]: the section name, or None if no section targets it yet
    """
    for name, mapped in find_afc_extruder_sections(ctx).items():
        if mapped == target:
            return name
    return None


def _afc_extruder_block_body(ctx: Context) -> List[str]:
    """
    The option lines (without the header) of the standard `[AFC_extruder extruder]`
    section shipped in `templates/AFC_Hardware-AFC.cfg`, to use as the body of a
    new section.

    The live `AFC_Hardware.cfg` is never used as the source: on a Snapmaker U1 it
    holds the U1-specific block from `templates/AFC_Hardware_U1.cfg` instead (with
    `u1_filament_sensor_name`, `toolchanger_unit`, etc.), which a new section for
    a different, non-U1 unit must not inherit.

    :param ctx: installer context
    :return List[str]: the body lines, or a minimal built-in default if the
        template is missing or has no `[AFC_extruder ...]` section
    """
    template = ctx.afc_path / "templates" / "AFC_Hardware-AFC.cfg"
    if not template.is_file():
        return list(_FALLBACK_AFC_EXTRUDER_BODY)
    lines = template.read_text(errors="ignore").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if _AFC_EXTRUDER_HEADER.match(line.strip())), None
    )
    if start is None:
        return list(_FALLBACK_AFC_EXTRUDER_BODY)
    end = start + 1
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped == "" or stripped.startswith("["):
            break
        end += 1
    return lines[start + 1:end]


def _resolve_pin_tool_start(sel: UnitSelection) -> Optional[str]:
    """
    The `pin_tool_start:` value implied by the unit's sensor/ramming choice.

    Matches the logic `install_afc` applies to the first unit's default
    AFC_extruder section, so a new section created for a different (or additional)
    extruder gets the same real pin instead of the template's placeholder.

    :param sel: the user's unit choices
    :return Optional[str]: the resolved pin, or None to leave the cloned template's
        own placeholder (`toolhead_sensor` is neither `"Sensor"` nor `"Ramming"`)
    """
    if sel.toolhead_sensor == "Sensor":
        return sel.toolhead_sensor_pin
    if sel.toolhead_sensor == "Ramming":
        return "AMS_extruder" if sel.installation_type == "OpenAMS" else "buffer"
    return None


def _build_afc_extruder_block(
    ctx: Context, section_name: str, target: str, pin_tool_start: Optional[str]
) -> str:
    """
    Text of a new `[AFC_extruder <section_name>]` section targeting `target`.

    :param ctx: installer context, used to find the standard template for the body
    :param section_name: name for the new section
    :param target: real printer extruder the new section must target
    :param pin_tool_start: value to give the cloned `pin_tool_start:` line, or
        None to leave the template's own placeholder
    :return str: the block, ending with a newline
    """
    body = [
        line for line in _afc_extruder_block_body(ctx)
        if not line.strip().startswith("extruder_name")
    ]
    if pin_tool_start is not None:
        body = [
            f"pin_tool_start: {pin_tool_start}" if line.strip().startswith("pin_tool_start:")
            else line
            for line in body
        ]
    body.append(f"extruder_name: {target}")
    return "\n".join([f"[AFC_extruder {section_name}]", *body]) + "\n"


def _append_afc_extruder_block(hardware: Path, block: str) -> None:
    """
    Add a new AFC_extruder block to the end of the hardware file.

    :param hardware: the AFC_Hardware.cfg file, created if missing
    :param block: text from :func:`_build_afc_extruder_block`
    """
    if not hardware.exists():
        hardware.write_text(block)
        return
    ensure_trailing_newline(hardware)
    doc = ConfigDocument.load(hardware)
    doc.append_text(f"\n{block}")
    doc.save()


def _ask_afc_extruder_name(console: Console, prompter: Prompter, target: str) -> str:
    """
    Ask for a name for a new AFC_extruder section; the blank answer keeps the default.

    :param console: where the notice goes
    :param prompter: where the answer comes from
    :param target: real printer extruder the new section will target, also its
        suggested (default) name
    :return str: the chosen section name
    :raises InstallerExit: if the prompter reports end of input
    """
    notice = textwrap.fill(
        f"Printer extruder '{target}' has no AFC_extruder section yet. A new AFC_extruder "
        "config will be added to AFC_Hardware.cfg file with name specified below.",
        width=_NOTICE_WRAP_WIDTH,
    )
    console.print_msg("INFO", f"\n{notice}")
    prompt = (
        f"Enter a name for the new AFC_extruder section or leave as default (default: "
        f"{target}): "
    )
    answer = prompter.ask(prompt)
    if answer is None:
        raise InstallerExit(1)
    return answer.strip() or target


def resolve_afc_extruder_section(
    ctx: Context, console: Console, prompter: Prompter, target: str,
    pin_tool_start: Optional[str] = None,
) -> str:
    """
    The AFC_extruder section name that targets a real printer extruder.

    Cross references every `[AFC_extruder <name>]` section's effective target (see
    :func:`find_afc_extruder_sections`) so a section the user renamed is still found.
    If none targets it yet, asks for a name (default: the extruder's own name) and
    creates a new section in AFC_Hardware.cfg with `extruder_name:` set explicitly,
    the same pattern `templates/AFC_Hardware_U1.cfg` uses for its tool extruders.

    :param ctx: installer context
    :param console: where prompts and notices go
    :param prompter: where the name answer comes from
    :param target: the real printer extruder to find or create a section for
    :param pin_tool_start: value to give a newly created section's
        `pin_tool_start:`, or None to leave the template's own placeholder;
        unused when an existing section is found
    :return str: the AFC_extruder section name
    """
    existing = section_for_extruder(ctx, target)
    if existing is not None:
        return existing
    hardware = ctx.afc_config_dir / "AFC_Hardware.cfg"
    name = _ask_afc_extruder_name(console, prompter, target)
    block = _build_afc_extruder_block(ctx, name, target, pin_tool_start)
    _append_afc_extruder_block(hardware, block)
    return name


def _set_extruder_option(path: Path, value: str) -> None:
    """
    Rewrite `extruder: <value>` on every matching line in a unit's config file.

    A trailing comment keeps its original column when the new value is short enough
    to fit before it (templates line their `extruder:` comments up with the other
    options in the section); otherwise it falls back to a single space.

    :param path: unit config file to edit
    :param value: new value, an `[AFC_extruder <name>]` section name
    """
    doc = ConfigDocument.load(path)
    changed = False
    for index, line in enumerate(doc.lines):
        body = line[:-1] if line.endswith("\n") else line
        match = _EXTRUDER_OPTION_LINE.fullmatch(body)
        if not match:
            continue
        comment = match.group(2)
        prefix = f"extruder: {value}"
        if comment:
            padding = max(1, body.index("#") - len(prefix))
            new_body = f"{prefix}{' ' * padding}{comment}"
        else:
            new_body = f"{prefix} "
        doc.lines[index] = f"{new_body}\n"
        changed = True
    if changed:
        doc.save()


def apply_unit_extruder(
    ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
) -> None:
    """
    Point a unit's `extruder:` option at the AFC_extruder section for the selected
    printer extruder, creating that section first if none targets it yet.

    A newly created section's `pin_tool_start:` is filled from the unit's own
    sensor/ramming choice (see :func:`_resolve_pin_tool_start`), the same values
    `install_afc` gives the first unit's default section, instead of being left on
    the cloned template's placeholder.

    A unit type with no known config file to edit is skipped with a warning. A target
    file that doesn't exist yet (its unit's files weren't actually copied, as in a test)
    is skipped quietly.

    :param ctx: installer context
    :param console: where warnings, notices and prompts go
    :param prompter: where a new section's name answer comes from
    :param sel: the user's unit choices
    """
    unit = unit_for(sel.installation_type)
    target = unit.buffer_target(ctx, sel) if unit is not None else None
    if target is None:
        console.print_msg(
            "WARNING",
            "No unit target is known for installation type "
            f"'{sel.installation_type}'; extruder selection was not applied.",
        )
        return
    if not target.extruder_file.exists():
        return
    section_name = resolve_afc_extruder_section(
        ctx, console, prompter, sel.extruder, _resolve_pin_tool_start(sel)
    )
    _set_extruder_option(target.extruder_file, section_name)
