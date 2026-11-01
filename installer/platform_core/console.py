# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Terminal output and input seams."""
from __future__ import annotations

import sys
from typing import Dict, Optional, TextIO

# ANSI color escape sequences.
NC = "\033[0m"
RED = "\033[0;31m"
GREEN = "\033[0;32m"
WHITE = "\033[0;37m"
MENU_GREEN = "\033[38;5;143;49m"
BOLD_RED = "\033[1;31m"
BOLD_GREEN = "\033[1;32m"
BOLD_YELLOW = "\033[1;33m"
BOLD_WHITE = "\033[1;37m"

_KIND_COLORS: Dict[str, str] = {
    "INFO": WHITE,
    "WARNING": BOLD_YELLOW,
    "ERROR": BOLD_RED,
    "PROMPT": BOLD_WHITE,
}
# SUCCESS and any other kind print uncolored
_CLEAR_SEQUENCE = "\033[H\033[2J\033[3J"
_DELIMITER = "-" * 40


class Console:
    """Colored message output, replacing `print_msg` and `clear`."""

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        """
        Create a console.

        :param stream: where to write, defaults to `sys.stdout` looked up on each call
        """
        self._stream = stream

    def _out(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stdout

    def print_msg(self, kind: str, text: str) -> None:
        """
        Print a message in the color for its kind.

        Unknown kinds, including SUCCESS, print uncolored.

        :param kind: INFO, WARNING, ERROR, PROMPT, SUCCESS or anything else
        :param text: message text, with real newlines
        """
        color = _KIND_COLORS.get(kind)
        line = text if color is None else f"{color}{text}{NC}"
        out = self._out()
        out.write(f"{line}\n")
        out.flush()

    def write(self, text: str) -> None:
        """
        Write text exactly as given (no color, no added newline).

        :param text: text to write
        """
        out = self._out()
        out.write(text)
        out.flush()

    def section_delimiter(self) -> None:
        """Print the 40 dash section delimiter (uncolored)."""
        self.print_msg("SUCCESS", _DELIMITER)

    def clear(self) -> None:
        """Clear the screen, but only when writing to a terminal."""
        out = self._out()
        if out.isatty():
            out.write(_CLEAR_SEQUENCE)
            out.flush()


class Prompter:
    """Line input."""

    def ask(self, prompt: str) -> Optional[str]:
        """
        Prompt the user for one line.

        The prompt is only shown when input comes from a terminal, and a last line without a
        newline counts as end of input.

        :param prompt: text shown before reading
        :return Optional[str]: the line without its newline, or None at end of input
        """
        if sys.stdin.isatty():
            try:
                return input(prompt)
            except EOFError:
                return None
        line = sys.stdin.readline()
        if not line.endswith("\n"):
            return None
        return line[:-1]
