# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""The settings shown on the main menu that can be changed there (rows 1 to 4 and 6)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Tuple

from installer.checks.checks import check_existing_install
from installer.platform_core.context import Context

_SERVICE = re.compile(r"^[A-Za-z0-9_.@:-]+$")
_HOST = re.compile(r"^[A-Za-z0-9._-]+$")


def _path(text: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(text.strip())))


def set_printer_config_dir(ctx: Context, text: str) -> str:
    """
    Change the printer config directory; the Moonraker config follows while it is the default.

    :param ctx: installer context, updated in place
    :param text: the typed directory
    :return str: the status message
    :raises ValueError: if the directory does not exist (the message says why)
    """
    path = _path(text)
    if not path.is_dir():
        error_str = f"Directory not found: {path}"
        raise ValueError(error_str)
    if ctx.moonraker_config_file == ctx.printer_config_dir / "moonraker.conf":
        ctx.moonraker_config_file = path / "moonraker.conf"
    ctx.printer_config_dir = path
    return f"Printer config directory set to {path}"


def set_klipper_dir(ctx: Context, text: str) -> str:
    """
    Change the Klipper directory and re-check whether AFC is already linked into it.

    :param ctx: installer context, updated in place
    :param text: the typed directory
    :return str: the status message
    :raises ValueError: if the directory does not exist (the message says why)
    """
    path = _path(text)
    if not path.is_dir():
        error_str = f"Directory not found: {path}"
        raise ValueError(error_str)
    ctx.klipper_dir = path
    ctx.prior_installation = False
    check_existing_install(ctx)
    return f"Klipper directory set to {path}"


def set_moonraker_config_file(ctx: Context, text: str) -> str:
    """
    Change the Moonraker config file; the file may be new but its folder must exist.

    :param ctx: installer context, updated in place
    :param text: the typed file path
    :return str: the status message
    :raises ValueError: if the folder does not exist (the message says why)
    """
    path = _path(text)
    if not path.parent.is_dir():
        error_str = f"Directory not found: {path.parent}"
        raise ValueError(error_str)
    ctx.moonraker_config_file = path
    return f"Moonraker config file set to {path}"


def set_klipper_service(ctx: Context, text: str) -> str:
    """
    Change the Klipper service name.

    :param ctx: installer context, updated in place
    :param text: the typed service name
    :return str: the status message
    :raises ValueError: if the name has characters a service name cannot have
    """
    name = text.strip()
    if not _SERVICE.match(name):
        error_str = "A service name may only use letters, digits and . _ @ : -"
        raise ValueError(error_str)
    ctx.klipper_service = name
    return f"Klipper service name set to {name}"


def parse_moonraker_address(text: str, current_port: str) -> Tuple[str, str]:
    """
    Split `[http[s]://]host[:port]` into the address and port the context stores.

    :param text: the typed address
    :param current_port: the port to keep when none is typed
    :return Tuple[str, str]: the address with its scheme (default `http://`) and the port
    :raises ValueError: if the scheme, host or port is not valid (the message says why)
    """
    rest = text.strip()
    scheme = "http://"
    if "://" in rest:
        scheme, rest = rest.split("://", 1)
        scheme += "://"
        if scheme not in ("http://", "https://"):
            error_str = "The address must start with http:// or https://"
            raise ValueError(error_str)
    host, port = rest, current_port
    if ":" in rest:
        host, _, port = rest.rpartition(":")
        if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
            error_str = "The port must be a number from 1 to 65535"
            raise ValueError(error_str)
        port = str(int(port))
    if not _HOST.match(host):
        error_str = "The host may only use letters, digits and . _ -"
        raise ValueError(error_str)
    return f"{scheme}{host}", port


def set_moonraker_address(ctx: Context, text: str) -> str:
    """
    Change the Moonraker address and, when one is typed, its port.

    :param ctx: installer context, updated in place
    :param text: the typed address
    :return str: the status message
    :raises ValueError: if the address is not valid (the message says why)
    """
    address, port = parse_moonraker_address(text, ctx.moonraker_port)
    ctx.moonraker_address = address
    ctx.moonraker_port = port
    return f"Moonraker address set to {address}:{port}"


@dataclass(frozen=True)
class Setting:
    """One editable row: what to call it and how to apply a typed value."""

    noun: str
    apply: Callable[[Context, str], str]


SETTINGS: Dict[str, Setting] = {
    "1": Setting("printer config directory", set_printer_config_dir),
    "2": Setting("klipper directory", set_klipper_dir),
    "3": Setting("moonraker config file", set_moonraker_config_file),
    "4": Setting("klipper service name", set_klipper_service),
    "6": Setting("moonraker address", set_moonraker_address),
}
