# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""A config file as a list of lines, saved byte for byte."""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import List

_ENCODING = "utf-8"
_ERRORS = "surrogateescape"  # lets any byte sequence round trip unchanged


class ConfigDocument:
    """A file split into lines that each keep their own terminator."""

    def __init__(self, path: Path, lines: List[str]) -> None:
        """
        Create a document.

        :param path: file the document was loaded from and saves back to
        :param lines: lines including their ``\\n`` or ``\\r\\n`` terminators
        """
        self.path = path
        self.lines = lines

    @staticmethod
    def split_lines(text: str) -> List[str]:
        """
        Split text into lines that keep their terminators; only ``\\n`` ends a line.

        :param text: text to split
        :return List[str]: the lines
        """
        parts = text.split("\n")
        lines = [f"{part}\n" for part in parts[:-1]]
        if parts[-1]:
            lines.append(parts[-1])
        return lines

    @classmethod
    def load(cls, path: Path) -> ConfigDocument:
        """
        Read a file. Only ``\\n`` ends a line, so ``\\r\\n`` stays attached to its line.

        :param path: file to read
        :return ConfigDocument: the loaded document
        """
        text = path.read_bytes().decode(_ENCODING, errors=_ERRORS)
        return cls(path, cls.split_lines(text))

    def append_text(self, text: str) -> None:
        """
        Append text like ``echo >>``: it continues an unterminated last line.

        :param text: text to add, may span several lines
        """
        tail = self.lines.pop() if self.lines and not self.lines[-1].endswith("\n") else ""
        self.lines.extend(self.split_lines(tail + text))

    @property
    def text(self) -> str:
        """The whole document as one string."""
        return "".join(self.lines)

    def save(self) -> None:
        """
        Write the document back atomically, keeping the file's permissions.

        The content is written to a temporary file in the same directory and renamed
        over the original, so a failure never leaves a half written config. A
        symlink is replaced rather than followed.
        """
        data = self.text.encode(_ENCODING, errors=_ERRORS)
        fd, temp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".cfgedit-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            shutil.copymode(str(self.path), temp_name)
            os.replace(temp_name, str(self.path))
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)
            raise
