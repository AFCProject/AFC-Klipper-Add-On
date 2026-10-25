# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Process helpers. Commands are always argv lists, never shell strings."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from http.client import HTTPException
from pathlib import Path
from typing import Callable, Optional, Sequence

from installer.platform_core.console import Console
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit


class CommandRunner:
    """Runs external commands; replaced by a fake in tests."""

    def run(
        self, args: Sequence[str], check: bool = False, capture: bool = True
    ) -> "subprocess.CompletedProcess[str]":
        """
        Run a command.

        A missing executable behaves like a shell: exit status 127 and a
        ``command not found`` message on stderr (raised if ``check`` is set).

        :param args: argv list, executable first
        :param check: raise CalledProcessError on a nonzero exit status
        :param capture: capture stdout and stderr, otherwise inherit the terminal
        :return subprocess.CompletedProcess[str]: the finished process
        """
        argv = list(args)
        try:
            return subprocess.run(argv, check=check, capture_output=capture, text=True)
        except FileNotFoundError:
            message = f"{argv[0]}: command not found"
            if check:
                raise subprocess.CalledProcessError(127, argv, stderr=message)
            return subprocess.CompletedProcess(argv, 127, stdout="", stderr=message)

    def exists(self, name: str) -> bool:
        """
        Check whether a command is on PATH, like ``command -v``.

        :param name: command name
        :return bool: True if it can be found
        """
        return shutil.which(name) is not None


_U1_INIT_SCRIPT = "/etc/init.d/S60klipper"
_NOT_IDLE_WARNING = (
    "Your printer is not idle/ready, or its status could not be confirmed. Automatic "
    "Klipper restart has been skipped; please restart the Klipper service manually "
    "once the printer is idle."
)
_NOT_IDLE_SNAPMAKER_WARNING = (
    "Please manually restart klipper through <ip_address>/firmware-config panel, "
    "or fully power-cycle printer."
)


def run_or_exit(runner: CommandRunner, argv: Sequence[str]) -> None:
    """
    Run a command with the terminal attached; a failure ends the installer.

    A failed command stops the installer with that command's exit status.

    :param runner: command runner
    :param argv: command and arguments
    """
    result = runner.run(argv, capture=False)
    if result.returncode != 0:
        raise InstallerExit(result.returncode)


def restart_service(runner: CommandRunner, name: str) -> None:
    """
    Restart a service with ``sudo systemctl``, or ``sudo service`` without systemd.

    :param runner: command runner
    :param name: service name
    """
    if runner.exists("systemctl"):
        run_or_exit(runner, ["sudo", "systemctl", "restart", name])
    else:
        run_or_exit(runner, ["sudo", "service", name, "restart"])


_HTTP_SCHEMES = ("http://", "https://")


def _fetch(url: str) -> bytes:
    """
    GET a URL and return the body even for an HTTP error status, like ``curl -s``.

    :param url: address to request
    :return bytes: response body
    """
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body: bytes = response.read()
            return body
    except urllib.error.HTTPError as exc:
        error_body: bytes = exc.read()
        return error_body


def query_printer_status(moonraker_url: str) -> bool:
    """
    Ask Moonraker whether the printer is idle enough to restart Klipper.

    :param moonraker_url: Moonraker base URL as ``address:port``
    :return bool: True when a state was reported and it is not ``Printing``. A non-http(s)
        address is never queried and counts as not idle.
    """
    if not moonraker_url.startswith(_HTTP_SCHEMES):
        return False
    try:
        body = _fetch(f"{moonraker_url}/printer/objects/query?idle_timeout")
        payload = json.loads(body.decode("utf-8", errors="replace"))
        state = payload["result"]["status"]["idle_timeout"]["state"]
    except (OSError, ValueError, HTTPException, KeyError, TypeError):
        return False
    text = "" if state is None else str(state)
    if text in ("", "null"):
        return False
    return text != "Printing"


def u1_restart_klipper(runner: CommandRunner, euid: Optional[int] = None) -> None:
    """
    Restart Klipper on a Snapmaker U1 through its init script.

    :param runner: command runner
    :param euid: effective user id, defaults to the current process's
    """
    user = os.geteuid() if euid is None else euid
    if user != 0:
        run_or_exit(runner, ["su", "root", "-c", f"{_U1_INIT_SCRIPT} restart"])
    else:
        run_or_exit(runner, [_U1_INIT_SCRIPT, "restart"])


def u1_write_debug_file(
    console: Console,
    runner: CommandRunner,
    euid: Optional[int] = None,
    oem_dir: Path = Path("/oem"),
) -> None:
    """
    Create ``/oem/.debug`` so Klipper changes survive a U1 reboot.

    :param console: where progress messages go
    :param runner: command runner, used to escalate with ``su`` when not root
    :param euid: effective user id, defaults to the current process's
    :param oem_dir: location of the oem directory
    """
    user = os.geteuid() if euid is None else euid
    debug_file = oem_dir / ".debug"
    if user != 0:
        console.print_msg("OTHER", "Writing .debug file to /oem/ directory")
        run_or_exit(runner, ["su", "root", "-c", f"touch {debug_file}"])
    elif not debug_file.is_file():
        console.print_msg("OTHER", "Adding /oem/.debug file")
        debug_file.touch()


def restart_klipper(
    ctx: Context,
    console: Console,
    runner: CommandRunner,
    status_query: Callable[[str], bool] = query_printer_status,
    euid: Optional[int] = None,
) -> None:
    """
    Restart Klipper when the printer is idle, otherwise warn and leave it running.

    The service restarted is the literal ``klipper``, not the ``-s`` value.

    :param ctx: installer context
    :param console: where warnings go
    :param runner: command runner
    :param status_query: returns True when the printer is idle (injectable for tests)
    :param euid: effective user id, defaults to the current process's
    """
    if status_query(ctx.moonraker):
        if ctx.is_snapmaker:
            u1_restart_klipper(runner, euid)
        else:
            restart_service(runner, "klipper")
    elif ctx.is_snapmaker:
        console.print_msg("WARNING", _NOT_IDLE_SNAPMAKER_WARNING)
    else:
        console.print_msg("WARNING", _NOT_IDLE_WARNING)
