# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Value edits on config files."""
from __future__ import annotations

import os
import re
from pathlib import Path

from installer.config_editor.document import ConfigDocument

# ASCII whitespace only, spelled out so Python's wider \s is not used
_WS = r"[ \t\n\v\f\r]"
_NWS = r"[^ \t\n\v\f\r]"

_EXTRUDER_HEADER = "[AFC_extruder extruder]"
_PIN_PREFIX = "pin_tool_start:"
_PREP_HEADER = "[AFC_prep]"
_TIP_CUT_TITLE = "######### Tip Cut Servo"
_TIP_CUT_RULE = "#--====="
_OLD_VARFILE = "VarFile: ../printer_data/config/AFC/AFC.var"


def _body(line: str) -> str:
    """
    A line without its `\\n`. A `\\r` stays.

    :param line: line with its terminator
    :return str: the line content
    """
    return line[:-1] if line.endswith("\n") else line


def set_value(path: Path, key: str, value: str) -> None:
    """
    Rewrite `key: <value>` on every line holding a single-token value for `key`.

    Indentation is dropped and a trailing `#` comment is kept. The output
    always has a space before the (possibly empty) comment. Lines
    are matched anywhere in the file, not per section.

    :param path: config file to edit
    :param key: option name, matched literally
    :param value: new value
    """
    pattern = re.compile(
        rf"{_WS}*{re.escape(key)}{_WS}*:{_WS}*({_NWS}+){_WS}*(#.*)?"
    )
    doc = ConfigDocument.load(path)
    changed = False
    for index, line in enumerate(doc.lines):
        match = pattern.fullmatch(_body(line))
        if match:
            comment = match.group(2) or ""
            doc.lines[index] = f"{key}: {value} {comment}\n"
            changed = True
    if changed:
        doc.save()


def set_tool_start_pin(path: Path, value: str) -> None:
    """
    Set the first `pin_tool_start:` after the `[AFC_extruder extruder]` header.

    The scan never leaves the section on a later header, so a pin in a
    following section is rewritten if the extruder section had none.

    :param path: config file to edit
    :param value: new pin
    """
    doc = ConfigDocument.load(path)
    in_section = False
    changed = False
    for index, line in enumerate(doc.lines):
        body = _body(line)
        if body == _EXTRUDER_HEADER:
            in_section = True
        elif in_section and body.startswith(_PIN_PREFIX):
            doc.lines[index] = f"pin_tool_start: {value}\n"
            in_section = False
            changed = True
    if changed:
        doc.save()


def ensure_prep_section(path: Path) -> None:
    """
    Append `[AFC_prep]` / `enable: True` unless a line already mentions the header.

    :param path: config file to edit
    """
    doc = ConfigDocument.load(path)
    if any(_PREP_HEADER in line for line in doc.lines):
        return
    doc.lines.extend(["\n", f"{_PREP_HEADER}\n", "enable: True\n"])
    doc.save()


def replace_varfile_path(path: Path, afc_config_dir: Path) -> None:
    """
    Point the old default `VarFile:` path at the real AFC config directory.

    Only the first occurrence on each line is replaced.

    :param path: config file to edit
    :param afc_config_dir: directory that holds `AFC.var`
    """
    new = f"VarFile: {afc_config_dir}/AFC.var"
    doc = ConfigDocument.load(path)
    changed = False
    for index, line in enumerate(doc.lines):
        if _OLD_VARFILE in line:
            doc.lines[index] = line.replace(_OLD_VARFILE, new, 1)
            changed = True
    if changed:
        doc.save()


def replace_text(path: Path, old: str, new: str) -> None:
    """
    Replace every occurrence of a literal string.

    :param path: file to edit
    :param old: text to find, must not be empty
    :param new: replacement text
    """
    if not old:
        error_str = "replace_text: the search text is empty"
        raise ValueError(error_str)
    doc = ConfigDocument.load(path)
    updated = [line.replace(old, new) for line in doc.lines]
    if updated != doc.lines:
        doc.lines = updated
        doc.save()


def replace_text_in_tree(directory: Path, old: str, new: str) -> None:
    """
    Run :func:`replace_text` on every regular file under a directory.

    Symlinks are skipped, matching `find -type f`.

    :param directory: tree to walk, a missing directory is ignored
    :param old: text to find, must not be empty
    :param new: replacement text
    """
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            file_path = Path(root) / name
            if not file_path.is_symlink():
                replace_text(file_path, old, new)


def remove_tip_cut_servo_section(path: Path) -> bool:
    """
    Remove the commented out "Tip Cut Servo" example from a hardware file.

    The example is the banner (a rule, the title, a rule) and the comment lines under it. It
    ends at the first line that is not a comment, so a servo section the user has already
    written for real is left alone. A blank line that would be left dangling at the end of
    the file, or doubled up, is removed with it.

    :param path: hardware config file to edit
    :return bool: True if the example was found and removed
    """
    doc = ConfigDocument.load(path)
    lines = doc.lines
    title = next((i for i, line in enumerate(lines) if line.startswith(_TIP_CUT_TITLE)), None)
    if title is None:
        return False
    start = title - 1 if title > 0 and lines[title - 1].startswith(_TIP_CUT_RULE) else title
    end = title + 1
    while end < len(lines) and lines[end].lstrip().startswith("#"):
        end += 1
    blank_before = start > 0 and lines[start - 1].strip() == ""
    if blank_before and end >= len(lines):
        start -= 1
    elif blank_before and lines[end].strip() == "":
        end += 1
    del lines[start:end]
    doc.save()
    return True


def ensure_trailing_newline(path: Path) -> None:
    """
    Add a final newline to a non-empty file that lacks one.

    :param path: file to edit
    """
    doc = ConfigDocument.load(path)
    if doc.lines and not doc.lines[-1].endswith("\n"):
        doc.lines[-1] += "\n"
        doc.save()
