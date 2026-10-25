# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Detect the K1 OS and Snapmaker U1 platforms."""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional

from installer.platform_core.constants import (
    K1_OS_MARKER,
    OS_RELEASE_PATH,
    SNAPMAKER_MARKER,
)


class PlatformInfo(NamedTuple):
    """Result of platform detection."""

    is_k1_os: bool
    is_snapmaker: bool


def detect_platform(os_release: Optional[Path] = None) -> PlatformInfo:
    """
    Detect K1 OS and Snapmaker U1 from the os-release file.

    Matches plain substrings: a missing or unreadable file means
    plain Linux, and the Snapmaker marker only counts on a K1 OS (buildroot) image.

    :param os_release: file to inspect, defaults to /etc/os-release
    :return PlatformInfo: the detected platform flags
    """
    path = os_release if os_release is not None else Path(OS_RELEASE_PATH)
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return PlatformInfo(False, False)
    is_k1_os = K1_OS_MARKER in text
    is_snapmaker = is_k1_os and SNAPMAKER_MARKER in text
    return PlatformInfo(is_k1_os, is_snapmaker)
