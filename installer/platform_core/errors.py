# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Exceptions shared across the installer."""
from __future__ import annotations


class InstallerExit(Exception):
    """Raised instead of calling exit(); the entrypoint maps it to a process exit code."""

    def __init__(self, code: int) -> None:
        """
        Create the exception.

        :param code: process exit code the entrypoint should use
        """
        super().__init__(f"installer exited with code {code}")
        self.code = code
