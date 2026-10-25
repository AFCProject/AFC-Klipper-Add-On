# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Delete sections and options from a config file, keeping the rest of it intact."""
from __future__ import annotations

import re
from itertools import takewhile
from pathlib import Path
from typing import List, NamedTuple, Set, Tuple

from installer.config_editor.document import ConfigDocument

_HEADER = re.compile(r"\[([^\]]+)\]\s*(?:[;#].*)?")
_OPTION_NAME = re.compile(r"([^:=]*)[:=]")


class ConfigParseError(ValueError):
    """The file has a structure that cannot be edited safely."""


class _Item(NamedTuple):
    kind: str  # header | option | continuation | blank | comment
    name: str  # section name for headers, option name for options, else ""


def _is_blank(line: str) -> bool:
    return line.strip() == ""


def _is_empty(line: str) -> bool:
    """A line with nothing on it at all (whitespace-only lines are not empty)."""
    return line in ("\n", "\r\n")


def _parse(path: Path, lines: List[str]) -> List[_Item]:
    """
    Classify every line like ``iniparse``, rejecting what it rejects.

    An indented line continues the option above it, so it is an error when no option
    has appeared since the last section header (or the start of the file). A line
    starting with ``[`` must be a complete section header.

    :param path: file name, used in error messages
    :param lines: lines with their terminators
    :return List[_Item]: one item per line
    """
    items: List[_Item] = []
    have_option = False
    for number, line in enumerate(lines, start=1):
        body = line.rstrip("\n")
        if _is_blank(body):
            items.append(_Item("blank", ""))
        elif body[0].isspace():
            if not have_option:
                error_str = (
                    f"{path}: line {number}: indented line with no option above it "
                    "in its section"
                )
                raise ConfigParseError(error_str)
            items.append(_Item("continuation", ""))
        elif body[0] in "#;":
            items.append(_Item("comment", ""))
        elif body[0] == "[":
            match = _HEADER.fullmatch(body)
            if match is None:
                error_str = f"{path}: line {number}: invalid section header"
                raise ConfigParseError(error_str)
            items.append(_Item("header", match.group(1)))
            have_option = False
        else:
            name_match = _OPTION_NAME.match(body)
            name = (name_match.group(1) if name_match else body).strip()
            items.append(_Item("option", name))
            have_option = True
    return items


def _region_end(items: List[_Item], header: int) -> int:
    """
    Index just past a section: the next header, or the end of the file.

    :param items: parsed lines
    :param header: index of the section's header
    :return int: exclusive end index
    """
    index = header + 1
    while index < len(items) and items[index].kind != "header":
        index += 1
    return index


def _tidy_after_section_delete(path: Path, lines: List[str], cut_at_start: bool) -> List[str]:
    """
    Tidy the file after a section was deleted.

    Outside option values, every run of empty lines becomes one, blank lines right after
    a header are dropped when that section has an option, and empty lines at the end
    of the file are dropped. If the cut was at the very start of the file, the empty
    lines that followed it are dropped too.

    :param path: file name, used in parse errors
    :param lines: lines after the section was cut out
    :param cut_at_start: True if the first line of the file was removed
    :return List[str]: tidied lines
    """
    start = 0
    while cut_at_start and start < len(lines) and _is_empty(lines[start]):
        start += 1
    lines = lines[start:]
    items = _parse(path, lines)
    drop: Set[int] = set()
    index = 0
    while index < len(lines):
        if not _is_empty(lines[index]):
            index += 1
            continue
        end = index
        while end < len(lines) and _is_empty(lines[end]):
            end += 1
        if not _inside_value(items, index, end):
            drop.update(range(index + 1, end))
        index = end
    for index, item in enumerate(items):
        if item.kind != "header":
            continue
        region = _region_end(items, index)
        if any(items[i].kind == "option" for i in range(index + 1, region)):
            follower = index + 1
            while follower < region and items[follower].kind == "blank":
                drop.add(follower)
                follower += 1
    tidy = [line for number, line in enumerate(lines) if number not in drop]
    while tidy and _is_empty(tidy[-1]):
        tidy.pop()
    return tidy


def _inside_value(items: List[_Item], start: int, end: int) -> bool:
    """
    Is a run of blank lines part of a multi-line option value?

    It is when an option or continuation line precedes it and a continuation follows.

    :param items: parsed lines
    :param start: first blank index
    :param end: index just past the run
    :return bool: True if the run sits inside a value
    """
    before = start - 1
    while before >= 0 and items[before].kind == "blank":
        before -= 1
    after = end
    while after < len(items) and items[after].kind == "blank":
        after += 1
    return (
        before >= 0
        and items[before].kind in ("option", "continuation")
        and after < len(items)
        and items[after].kind == "continuation"
    )


def _finish(doc: ConfigDocument, changed: bool) -> None:
    """
    Save the file; it always ends with a newline, even if nothing was deleted.

    :param doc: edited document
    :param changed: whether lines were removed
    """
    unterminated = bool(doc.lines) and not doc.lines[-1].endswith("\n")
    if unterminated:
        # the added terminator matches the file's own style (first terminated line)
        crlf = any(line.endswith("\r\n") for line in doc.lines[:1]) or (
            len(doc.lines) > 1 and doc.lines[0].endswith("\r\n")
        )
        doc.lines[-1] += "\r\n" if crlf else "\n"
    if changed or unterminated:
        doc.save()


def delete_section(path: Path, section: str) -> None:
    """
    Remove every section with this exact name.

    A section runs from its header to its last option or continuation line. Comments and
    blank lines after that stay, and the blank lines around the cut become one (none at
    the start or end of the file). A missing section is not an error.

    :param path: config file
    :param section: section name, case sensitive
    :raises ConfigParseError: if the file cannot be edited safely
    """
    doc = ConfigDocument.load(path)
    items = _parse(path, doc.lines)
    spans: List[Tuple[int, int]] = []
    for index, item in enumerate(items):
        if item.kind == "header" and item.name == section:
            end = index
            for inner in range(index + 1, _region_end(items, index)):
                if items[inner].kind in ("option", "continuation"):
                    end = inner
            spans.append((index, end))
    if not spans:
        _finish(doc, changed=False)
        return
    removed = {line for start, end in spans for line in range(start, end + 1)}
    kept: List[str] = []
    junctions: Set[int] = set()
    for index, line in enumerate(doc.lines):
        if index in removed:
            junctions.add(len(kept))
        else:
            kept.append(line)
    doc.lines = _tidy_after_section_delete(path, kept, 0 in junctions)
    _finish(doc, changed=True)


def _shadowed_by_global_option(items: List[_Item], section: str, wanted: str) -> bool:
    """
    Is there an option before the first header that has this name, and this section?

    Deleting it would have to remove an option the section only inherits.

    :param items: parsed lines
    :param section: section name
    :param wanted: lower case option name
    :return bool: True if the deletion cannot be done
    """
    if not any(i.kind == "header" and i.name == section for i in items):
        return False
    before_first_header = takewhile(lambda item: item.kind != "header", items)
    return any(i.kind == "option" and i.name.lower() == wanted for i in before_first_header)


def delete_key(path: Path, section: str, key: str) -> bool:
    """
    Remove an option (and its continuation lines) from every section with this name.

    Option names match without regard to case, section names exactly. Surrounding blank
    lines are left alone.

    :param path: config file
    :param section: section name, case sensitive
    :param key: option name
    :return bool: True if anything was removed
    :raises ConfigParseError: if the file cannot be edited safely
    """
    doc = ConfigDocument.load(path)
    items = _parse(path, doc.lines)
    removed: Set[int] = set()
    wanted = key.lower()
    for header, item in enumerate(items):
        if item.kind != "header" or item.name != section:
            continue
        region_end = _region_end(items, header)
        index = header + 1
        while index < region_end:
            if items[index].kind == "option" and items[index].name.lower() == wanted:
                last = index
                for follower in range(index + 1, region_end):
                    if items[follower].kind == "continuation":
                        last = follower
                    elif items[follower].kind == "option":
                        break
                removed.update(range(index, last + 1))
                index = last
            index += 1
    if not removed:
        if _shadowed_by_global_option(items, section, wanted):
            error_str = (
                f"{path}: option '{key}' only exists before the first section; "
                f"it cannot be deleted from [{section}]"
            )
            raise ConfigParseError(error_str)
        _finish(doc, changed=False)
        return False
    doc.lines = [line for number, line in enumerate(doc.lines) if number not in removed]
    _finish(doc, changed=True)
    return True
