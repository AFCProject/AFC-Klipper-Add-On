# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The installer Context: the settings every step reads and updates."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from installer.platform_core.constants import (
    DEFAULT_BRANCH,
    DEFAULT_GIT_REPO,
    DEFAULT_KLIPPER_SERVICE,
    DEFAULT_MOONRAKER_ADDRESS,
    DEFAULT_MOONRAKER_PORT,
    SNAPMAKER_KLIPPER_VENV,
)
from installer.platform_core.platform_info import PlatformInfo


def _timestamp() -> str:
    """
    Current time in the ``%Y%m%d%H%M%S`` form used in backup names.

    :return str: the timestamp
    """
    return datetime.now().strftime("%Y%m%d%H%M%S")


@dataclass
class Context:
    """Paths, flags and settings shared by every installer step."""

    afc_path: Path
    printer_config_dir: Path
    klipper_dir: Path
    klipper_venv: Path
    moonraker_config_file: Path
    klipper_service: str = DEFAULT_KLIPPER_SERVICE
    moonraker_address: str = DEFAULT_MOONRAKER_ADDRESS
    moonraker_port: str = DEFAULT_MOONRAKER_PORT
    branch: str = DEFAULT_BRANCH
    gitrepo: str = DEFAULT_GIT_REPO
    git_install: bool = True
    test_mode: bool = False
    prior_installation: bool = False
    files_updated_or_installed: bool = False
    is_k1_os: bool = False
    is_snapmaker: bool = False
    backup_date: str = field(default_factory=_timestamp)
    original_args: List[str] = field(default_factory=list)
    home: Path = field(default_factory=Path.home)

    @classmethod
    def create(
        cls,
        afc_path: Path,
        home: Optional[Path] = None,
        printer_config_dir: Optional[Path] = None,
        klipper_dir: Optional[Path] = None,
        klipper_venv: Optional[Path] = None,
        moonraker_config_file: Optional[Path] = None,
        klipper_service: str = DEFAULT_KLIPPER_SERVICE,
        moonraker_address: str = DEFAULT_MOONRAKER_ADDRESS,
        moonraker_port: str = DEFAULT_MOONRAKER_PORT,
        branch: str = DEFAULT_BRANCH,
        gitrepo: str = DEFAULT_GIT_REPO,
        git_install: bool = True,
        test_mode: bool = False,
        backup_date: Optional[str] = None,
        original_args: Optional[List[str]] = None,
    ) -> Context:
        """
        Build a Context using the defaults for anything not overridden.

        ``afc_path`` is always the checkout the installer runs from, never ``$HOME``
        based (issue #853). The Moonraker config defaults to a file inside the
        final printer config directory, so a ``-p`` override moves it too.

        :param afc_path: the AFC-Klipper-Add-On checkout
        :param home: home directory, defaults to the current user's
        :param printer_config_dir: override for ``~/printer_data/config``
        :param klipper_dir: override for ``~/klipper``
        :param klipper_venv: override for ``~/klippy-env/bin``
        :param moonraker_config_file: override for ``<printer_config_dir>/moonraker.conf``
        :param klipper_service: Klipper service name
        :param moonraker_address: Moonraker host with scheme
        :param moonraker_port: Moonraker port
        :param branch: git branch to use
        :param gitrepo: git remote URL
        :param git_install: whether the checkout is a git clone
        :param test_mode: skip git and restart actions (development only)
        :param backup_date: timestamp used in backup names, defaults to now
        :param original_args: arguments to re-exec with after a self update
        :return Context: the new context
        """
        base = home if home is not None else Path.home()
        config_dir = printer_config_dir or base / "printer_data" / "config"
        return cls(
            afc_path=afc_path,
            printer_config_dir=config_dir,
            klipper_dir=klipper_dir or base / "klipper",
            klipper_venv=klipper_venv or base / "klippy-env" / "bin",
            moonraker_config_file=moonraker_config_file or config_dir / "moonraker.conf",
            klipper_service=klipper_service,
            moonraker_address=moonraker_address,
            moonraker_port=moonraker_port,
            branch=branch,
            gitrepo=gitrepo,
            git_install=git_install,
            test_mode=test_mode,
            backup_date=backup_date if backup_date is not None else _timestamp(),
            original_args=list(original_args or []),
            home=base,
        )

    @property
    def afc_config_dir(self) -> Path:
        """Directory holding the installed AFC configuration."""
        return self.printer_config_dir / "AFC"

    @property
    def afc_file(self) -> Path:
        """The main AFC.cfg file."""
        return self.afc_config_dir / "AFC.cfg"

    @property
    def moonraker(self) -> str:
        """Moonraker base URL as ``address:port``."""
        return f"{self.moonraker_address}:{self.moonraker_port}"

    @property
    def klipper_extra_path(self) -> Path:
        """Klipper's ``klippy/extras`` directory, derived from ``klipper_dir``."""
        return self.klipper_dir / "klippy" / "extras"

    def apply_platform(self, info: PlatformInfo) -> None:
        """
        Record the detected platform; Snapmaker forces the system python directory.

        :param info: detection result from ``detect_platform``
        """
        self.is_k1_os = info.is_k1_os
        self.is_snapmaker = info.is_snapmaker
        if info.is_snapmaker:
            self.klipper_venv = Path(SNAPMAKER_KLIPPER_VENV)
