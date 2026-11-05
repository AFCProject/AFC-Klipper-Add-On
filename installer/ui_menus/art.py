# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""ASCII art banners, stored as data files in `art/` with `\\e` for the escape character."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from installer.unit_registry.registry import unit_for

ART_DIR = Path(__file__).parent / "art"
_NAME = re.compile(r"^[A-Za-z0-9_]+$")


@lru_cache(maxsize=None)
def load_art(name: str) -> str:
    """
    Load a banner and turn its `\\e` markers into real escape characters.

    An unknown or malformed name, or an unreadable file, gives an empty banner.

    :param name: art file name without extension, for example `main` or `EMU`
    :return str: the banner text, or an empty string
    """
    if not _NAME.match(name):
        return ""
    try:
        with open(ART_DIR / f"{name}.txt", encoding="utf-8", newline="") as handle:
            text = handle.read()
    except OSError:
        return ""
    return text.replace("\\e", "\033")


def art_name_for_type(installation_type: str) -> str:
    """
    Map an installation type to its art name (the unit key).

    :param installation_type: installation type as shown in the menu
    :return str: art name, or an empty string for an unknown type
    """
    unit = unit_for(installation_type)
    return "" if unit is None else unit.key
