# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The yes / no install toggles: the one table the menu, the defaults and the install share."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Toggle:
    """One install toggle."""

    key: str
    label: str
    default: bool
    config_key: Optional[str] = None  # AFC.cfg option it sets, None if handled elsewhere


TOGGLES: Tuple[Toggle, ...] = (
    Toggle("afc_includes", "Add AFC includes?", True),
    Toggle("tip_forming", "Enable tip-forming?", False, "form_tip"),
    Toggle("toolhead_cutter", "Enable toolhead cutter?", True, "tool_cut"),
    Toggle("hub_cutter", "Enable hub cutter?", False, "hub_cut"),
    Toggle("kick_macro", "Enable kick macro?", True, "kick"),
    Toggle("park_macro", "Enable park macro?", True, "park"),
    Toggle("poop_macro", "Enable poop macro?", True, "poop"),
    Toggle("wipe_macro", "Enable wipe macro?", True, "wipe"),
    Toggle("tip_cut_servo", "Include the tip cut servo example?", False),
)


def default_toggles() -> Dict[str, bool]:
    """
    Build the toggle values with their defaults.

    :return Dict[str, bool]: toggle key to enabled flag
    """
    return {toggle.key: toggle.default for toggle in TOGGLES}
