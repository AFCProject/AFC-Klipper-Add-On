# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Command line parsing and context building."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, TextIO, Tuple

from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit

_VALUE_FLAGS: Dict[str, str] = {
    "a": "moonraker_address",
    "k": "klipper_dir",
    "m": "moonraker_config_file",
    "n": "moonraker_port",
    "s": "klipper_service",
    "b": "branch",
    "p": "printer_config_dir",
    "y": "klipper_venv",
}
_HELP_LINES = (
    "Usage: install-afc.sh [options]",
    "",
    "Options:",
    "  -a <moonraker address>      Specify the address of the Moonraker server "
    "(default: http://localhost)",
    "  -k <path>                   Specify the path to the Klipper directory",
    "  -m <moonraker config path>  Specify the path to the Moonraker config file "
    "(default: ~/printer_data/config/moonraker.conf)",
    "  -n <moonraker port>         Specify the port of the Moonraker server (default: 7125)",
    "  -s <klipper service name>   Specify the name of the Klipper service (default: klipper)",
    "  -p <printer config dir>     Specify the path to the printer config directory "
    "(default: ~/printer_data/config)",
    "  -b <branch>                 Specify the branch to use (default: main)",
    "  -y <klipper venv dir>       Specify the klipper python venv dir "
    "(default: ~/klippy-env/bin)",
    "  -t                          Run in test mode. This is for development only and should "
    "not be run by regular users. This",
    "                              removes any checks for python and does not attempt to clone "
    "or update the repository. There",
    "                              is NO support provided when this is used.",
    "  -h                          Display this help message",
    "",
    "Example:",
    " {prog} [-a <moonraker address>] [-k <klipper_path>] [-s <klipper_service_name>] "
    "[-m <moonraker_config_path>] [-n <moonraker_port>] [-p <printer_config_dir>] "
    "[-p <printer_config_dir>] [-b <branch>] [-y <klipper venv dir>] [-h] ",
)


@dataclass
class CliArgs:
    """The parsed flags. A value of None means the flag was not given."""

    moonraker_address: Optional[str] = None
    klipper_dir: Optional[str] = None
    moonraker_config_file: Optional[str] = None
    moonraker_port: Optional[str] = None
    klipper_service: Optional[str] = None
    branch: Optional[str] = None
    printer_config_dir: Optional[str] = None
    klipper_venv: Optional[str] = None
    test_mode: bool = False


def help_text(prog: str) -> str:
    """
    The ``-h`` text. Only the example line shows the program name.

    :param prog: how the script was invoked (``$0``)
    :return str: help text ending with a newline
    """
    return "\n".join(line.replace("{prog}", prog) for line in _HELP_LINES) + "\n"


def _getopts(argv: List[str]) -> Iterator[Tuple[str, str]]:
    """
    Walk the flags as ``getopts`` would, yielding ``(flag, value)`` pairs.

    ``value`` is empty for ``t`` and ``h``. An unknown flag yields ``("?", letter)`` and a
    flag missing its value yields ``(":", letter)``; the walk stops after either.

    :param argv: arguments without the program name
    :return Iterator[Tuple[str, str]]: the flags in order
    """
    index = 0
    while index < len(argv):
        word = argv[index]
        index += 1
        if word == "--" or len(word) < 2 or not word.startswith("-"):
            return
        position = 1
        while position < len(word):
            flag = word[position]
            position += 1
            if flag in ("t", "h"):
                yield flag, ""
            elif flag in _VALUE_FLAGS:
                value = word[position:]
                position = len(word)
                if not value and index >= len(argv):
                    yield ":", flag
                    return
                if not value:
                    value = argv[index]
                    index += 1
                yield flag, value
            else:
                yield "?", flag
                return


def parse_args(
    argv: List[str], prog: str, out: Optional[TextIO] = None, err: Optional[TextIO] = None
) -> CliArgs:
    """
    Parse the flags ``-a -k -s -m -n -b -p -y -t -h``.

    Flags are handled in order and may be clustered (``-ty /path``). Parsing stops at
    the first non-flag argument or ``--``. ``-h`` prints the help and exits 0 on the spot.
    An unknown flag or a missing value prints getopts' message and exits 1, as does a
    ``-b`` value starting with ``-`` (git would read it as an option).

    :param argv: arguments without the program name
    :param prog: program name for messages and the help example
    :param out: stream for the help text, defaults to stdout
    :param err: stream for error messages, defaults to stderr
    :return CliArgs: the parsed flags
    """
    args = CliArgs()
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    for flag, value in _getopts(argv):
        if flag == "h":
            out.write(help_text(prog))
            raise InstallerExit(0)
        if flag == "t":
            args.test_mode = True
        elif flag == "?":
            err.write(f"{prog}: illegal option -- {value}\n")
            raise InstallerExit(1)
        elif flag == ":":
            err.write(f"{prog}: option requires an argument -- {value}\n")
            raise InstallerExit(1)
        elif flag == "b" and value.startswith("-"):
            err.write(f"{prog}: invalid branch name -- {value}\n")
            raise InstallerExit(1)
        else:
            setattr(args, _VALUE_FLAGS[flag], value)
    return args


def requested_branch(argv: List[str]) -> str:
    """
    The branch asked for with ``-b``, scanned quietly so it can be switched to before
    anything else loads.

    Errors and ``-h`` are ignored here; the real parse reports them.

    :param argv: arguments without the program name
    :return str: the last ``-b`` value, or an empty string
    """
    branch = ""
    for flag, value in _getopts(argv):
        if flag == "b" and not value.startswith("-"):
            branch = value
    return branch


def build_context(args: CliArgs, script_dir: Path, home: Path, argv: List[str]) -> Context:
    """
    Build the installer context from the flags.

    ``afc_path`` is the checkout holding the script, never a path under ``home``, and the
    Moonraker config defaults to the final printer config directory (issue #853).

    :param args: parsed flags
    :param script_dir: directory of the shim, which is the AFC checkout
    :param home: home directory
    :param argv: the original arguments, kept for the self re-exec
    :return Context: the new context
    """
    defaults = Context.create(afc_path=script_dir, home=home)
    return Context.create(
        afc_path=script_dir,
        home=home,
        printer_config_dir=_path(args.printer_config_dir),
        klipper_dir=_path(args.klipper_dir),
        klipper_venv=_path(args.klipper_venv),
        moonraker_config_file=_path(args.moonraker_config_file),
        klipper_service=_pick(args.klipper_service, defaults.klipper_service),
        moonraker_address=_pick(args.moonraker_address, defaults.moonraker_address),
        moonraker_port=_pick(args.moonraker_port, defaults.moonraker_port),
        branch=_pick(args.branch, defaults.branch),
        test_mode=args.test_mode,
        original_args=list(argv),
    )


def _path(value: Optional[str]) -> Optional[Path]:
    return Path(value) if value else None


def _pick(value: Optional[str], default: str) -> str:
    return default if value is None else value
