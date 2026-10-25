"""Test doubles shared by the installer tests.

They mirror the interfaces that platform-core will provide (Console, Prompter,
CommandRunner) so module tests never touch a real terminal, process or stdin.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


class RecordingConsole:
    """Console that records every message instead of printing it."""

    def __init__(self) -> None:
        self.messages: List[Tuple[str, str]] = []
        self.clears = 0
        self.written: List[str] = []

    def write(self, text: str) -> None:
        self.written.append(text)

    def print_msg(self, kind: str, text: str) -> None:
        self.messages.append((kind, text))

    def section_delimiter(self) -> None:
        self.print_msg("SUCCESS", "-" * 40)

    def clear(self) -> None:
        self.clears += 1


class ScriptedPrompter:
    """Prompter that answers from a fixed list, then reports EOF with None."""

    def __init__(self, answers: Sequence[str]) -> None:
        self._answers = list(answers)
        self.prompts: List[str] = []

    def ask(self, prompt: str) -> Optional[str]:
        self.prompts.append(prompt)
        if not self._answers:
            return None
        return self._answers.pop(0)


class FakeCommandRunner:
    """CommandRunner that records argv lists and returns scripted results."""

    def __init__(
        self,
        results: Optional[Dict[Tuple[str, ...], "subprocess.CompletedProcess[str]"]] = None,
        existing: Iterable[str] = (),
    ) -> None:
        self._results = dict(results or {})
        self._existing = set(existing)
        self.calls: List[List[str]] = []

    def run(
        self, args: Sequence[str], check: bool = False, capture: bool = True
    ) -> "subprocess.CompletedProcess[str]":
        argv = list(args)
        self.calls.append(argv)
        default = subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        result = self._results.get(tuple(argv), default)
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, argv)
        return result

    def exists(self, name: str) -> bool:
        return name in self._existing


@dataclass
class PrinterTree:
    """Paths of a throwaway printer layout built under a temp directory."""

    home: Path
    printer_config_dir: Path
    printer_cfg: Path
    moonraker_conf: Path
    klipper_dir: Path
    klipper_extras: Path
    afc_path: Path


def build_printer_tree(root: Path, repo_root: Path) -> PrinterTree:
    """
    Create a fake home with a printer config dir, Klipper checkout and AFC checkout.

    :param root: empty directory to build under
    :param repo_root: real repository, source of config/ and templates/
    :return PrinterTree: the created paths
    """
    home = root / "home"
    config_dir = home / "printer_data" / "config"
    config_dir.mkdir(parents=True)
    printer_cfg = config_dir / "printer.cfg"
    printer_cfg.write_text("[printer]\nkinematics: corexy\n")
    moonraker_conf = config_dir / "moonraker.conf"
    moonraker_conf.write_text("[server]\nhost: 0.0.0.0\n")

    klipper_dir = home / "klipper"
    klipper_extras = klipper_dir / "klippy" / "extras"
    klipper_extras.mkdir(parents=True)
    (klipper_extras / "__init__.py").write_text("# stock klipper extras package\n")
    exclude = klipper_dir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True)
    exclude.write_text("")

    afc_path = home / "AFC-Klipper-Add-On"
    shutil.copytree(repo_root / "config", afc_path / "config")
    shutil.copytree(repo_root / "templates", afc_path / "templates")
    extras = afc_path / "extras"
    extras.mkdir()
    (extras / "__init__.py").write_text("")
    (extras / "AFC.py").write_text('AFC_VERSION="1.2.8"\n')
    (extras / "AFC_lane.py").write_text("")
    (extras / "AFC_unit.py").write_text("")

    return PrinterTree(
        home=home,
        printer_config_dir=config_dir,
        printer_cfg=printer_cfg,
        moonraker_conf=moonraker_conf,
        klipper_dir=klipper_dir,
        klipper_extras=klipper_extras,
        afc_path=afc_path,
    )
