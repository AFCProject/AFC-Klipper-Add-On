# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Section block edits and range based line edits on config files."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable, List, Set

from installer.config_editor.document import ConfigDocument

# The patterns comment_gcode_in_fluidd uses, in Python regex form.
FLUIDD_T_MACRO_START = r"^\[gcode_macro T[0-9]+\][ \t\n\v\f\r]*$"
FLUIDD_T_MACRO_COMMENTED_START = r"^# \[gcode_macro T[0-9]+\]"
FLUIDD_T_MACRO_END = "SWITCH_OF_EXTENDED_EXTRUDER INDEX"

_ANY_HEADER = re.compile(r"\[.+\]")


def _bodies(doc: ConfigDocument) -> List[str]:
    """
    Lines without their ``\\n`` (a ``\\r`` stays).

    :param doc: loaded document
    :return List[str]: line contents
    """
    return [line[:-1] if line.endswith("\n") else line for line in doc.lines]


def _save_awk_output(doc: ConfigDocument, out: List[str]) -> None:
    """
    Save the lines with every line ending in ``\\n``.

    :param doc: document being edited
    :param out: line contents without terminators
    """
    new_lines = [f"{line}\n" for line in out]
    if new_lines != doc.lines:
        doc.lines = new_lines
        doc.save()


def set_key_in_section(path: Path, header: str, key: str, value: str) -> None:
    """
    Set ``key: value`` in the section whose header line is exactly ``header``.

    An existing ``key:`` line is replaced in place and repeats are dropped. If there is
    none, the line goes before the first empty line of the section, before the next
    header, or at the end of the file. Every line is rewritten with a ``\\n``.

    :param path: config file
    :param header: full header line, e.g. ``[AFC_BoxTurtle Turtle_1]``
    :param key: option name
    :param value: option value
    """
    doc = ConfigDocument.load(path)
    new_line = f"{key}: {value}"
    prefix = f"{key}:"
    out: List[str] = []
    in_section = False
    done = False
    for body in _bodies(doc):
        if body == header:
            in_section = True
            out.append(body)
            continue
        if in_section and body.startswith(prefix):
            if not done:
                out.append(new_line)
                done = True
            continue
        if in_section and body == "":
            if not done:
                out.append(new_line)
                done = True
            in_section = False
        if in_section and _ANY_HEADER.match(body):
            if not done:
                out.append(new_line)
                done = True
            in_section = False
        out.append(body)
    if in_section and not done:
        out.append(new_line)
    _save_awk_output(doc, out)


def replace_section_block(path: Path, header: str, new_block: str) -> bool:
    """
    Replace a section (header and body) with new text, keeping the blank line after it.

    The check for the header is a plain substring search, but only a line
    equal to ``header`` is replaced. A body ends at the first empty line or next header.

    :param path: config file
    :param header: exact header line
    :param new_block: replacement text, header included
    :return bool: False if no line mentions the header (the file is left alone)
    """
    doc = ConfigDocument.load(path)
    if not any(header in line for line in doc.lines):
        return False
    out: List[str] = []
    in_section = False
    for body in _bodies(doc):
        if body == header:
            in_section = True
            out.append(new_block)
            continue
        if in_section and body == "":
            in_section = False
            out.append(body)
            continue
        if in_section and _ANY_HEADER.match(body):
            in_section = False
        if in_section:
            continue
        out.append(body)
    _save_awk_output(doc, out)
    return True


def remove_section_block(path: Path, header: str) -> None:
    """
    Remove a section: header, body and one trailing empty line.

    :param path: config file
    :param header: exact header line
    """
    doc = ConfigDocument.load(path)
    out: List[str] = []
    in_section = False
    for body in _bodies(doc):
        if body == header:
            in_section = True
            continue
        if in_section and body == "":
            in_section = False
            continue
        if in_section and _ANY_HEADER.match(body):
            in_section = False
        if in_section:
            continue
        out.append(body)
    _save_awk_output(doc, out)


def section_names(paths: Iterable[Path], prefix: str) -> List[str]:
    """
    Names of every ``[<prefix> <name>]`` section in the given files, sorted and unique.

    :param paths: files to read; ones that cannot be read are skipped
    :param prefix: section type, e.g. ``AFC_buffer``
    :return List[str]: the names
    """
    pattern = re.compile(rf"\[{re.escape(prefix)} ([^\]]+)\]")
    names: Set[str] = set()
    for path in paths:
        try:
            doc = ConfigDocument.load(path)
        except OSError:
            continue
        for body in _bodies(doc):
            match = pattern.match(body)
            if match:
                names.add(match.group(1))
    return sorted(names)


def _edit_range(
    path: Path, start_regex: str, end_text: str, transform: Callable[[str], str]
) -> None:
    """
    Apply a line transform over every range from a start pattern to an end pattern.

    The end marker is only looked for after the start line, and a range with no end runs
    to the end of the file.

    :param path: file to edit
    :param start_regex: regex searched in each line to open a range
    :param end_text: text whose first occurrence in a later line closes the range
    :param transform: function applied to each line in a range (without its ``\\n``)
    """
    start = re.compile(start_regex)
    doc = ConfigDocument.load(path)
    in_range = False
    changed = False
    for index, line in enumerate(doc.lines):
        body = line[:-1] if line.endswith("\n") else line
        newline = line[len(body):]
        if in_range:
            in_range = end_text not in body
        elif start.search(body):
            in_range = True
        else:
            continue
        updated = transform(body) + newline
        if updated != line:
            doc.lines[index] = updated
            changed = True
    if changed:
        doc.save()


def comment_range(path: Path, start_regex: str, end_text: str) -> None:
    """
    Prefix every line from a start match through the end marker with ``# ``.

    :param path: file to edit
    :param start_regex: regex that opens a range
    :param end_text: text that closes it
    """
    _edit_range(path, start_regex, end_text, lambda body: f"# {body}")


def uncomment_range(path: Path, start_regex: str, end_text: str) -> None:
    """
    Remove one leading ``# `` from each line of a range, leaving other lines as they are.

    :param path: file to edit
    :param start_regex: regex that opens a range
    :param end_text: text that closes it
    """
    _edit_range(
        path,
        start_regex,
        end_text,
        lambda body: body[2:] if body.startswith("# ") else body,
    )
