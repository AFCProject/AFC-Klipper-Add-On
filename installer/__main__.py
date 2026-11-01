# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Entry point for `python -m installer install|update|u1`."""
from __future__ import annotations

import os
import sys
from typing import Callable, Dict, List, Optional

from installer.app import run_install, run_u1, run_update

COMMANDS: Dict[str, Callable[[List[str]], int]] = {
    "install": run_install,
    "update": run_update,
    "u1": run_u1,
}
_USAGE = (
    "usage: python -m installer {install,update,u1} [options]\n"
    "\n"
    "AFC installer. Commands:\n"
    "  install   install AFC (what install-afc.sh does)\n"
    "  update    update AFC (what update-afc.sh does)\n"
    "  u1        Snapmaker U1 firmware actions (-e enable, -d disable)\n"
)


def main(argv: Optional[List[str]] = None) -> int:
    """
    Run one of the installer commands.

    :param argv: arguments to parse, defaults to `sys.argv[1:]`
    :return int: process exit code
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    command = COMMANDS.get(args[0])
    if command is None:
        sys.stderr.write(f"installer: unknown command '{args[0]}'\n{_USAGE}")
        return 2
    os.environ["LC_ALL"] = "C"
    return command(args[1:])


if __name__ == "__main__":
    sys.exit(main())
