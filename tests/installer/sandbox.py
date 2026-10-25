"""A fake home for the flow tests: printer config, Klipper checkout and git exclude file."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from tests.installer.paths import REPO_ROOT  # noqa: F401  (re-exported for the tests)

MOONRAKER_BLOCK_TEXT = "[update_manager afc-software]"


@dataclass
class Scenario:
    itype: str = "BoxTurtle (4-Lane)"
    name: str = "Turtle_1"
    variables: Dict[str, str] = field(default_factory=dict)
    toggles: Dict[str, bool] = field(default_factory=dict)
    buffer_type: str = "Unknown"
    sensor: str = "Ramming"
    pin: str = "Unknown"
    git_install: bool = True
    test_mode: bool = False
    service: str = "klipper"
    printer_cfg: Optional[str] = "[printer]\nkinematics: corexy\n"
    moonraker: Optional[str] = "[server]\nhost: 0.0.0.0\n"
    answers: List[str] = field(default_factory=list)
    files: Dict[str, str] = field(default_factory=dict)  # home-relative extra files
    snapmaker: bool = False


TOGGLE_DEFAULTS = {
    "afc_includes": True, "tip_forming": False, "toolhead_cutter": True, "hub_cutter": False,
    "kick_macro": True, "park_macro": True, "poop_macro": True, "wipe_macro": True,
    "tip_cut_servo": False,
}


def sandbox(root: Path, sc: Scenario) -> Path:
    home = root / "home"
    cfg = home / "printer_data" / "config"
    cfg.mkdir(parents=True)
    if sc.printer_cfg is not None:
        (cfg / "printer.cfg").write_text(sc.printer_cfg)
    if sc.moonraker is not None:
        (cfg / "moonraker.conf").write_text(sc.moonraker)
    extras = home / "klipper" / "klippy" / "extras"
    extras.mkdir(parents=True)
    (extras / "__init__.py").write_text("stock\n")
    exclude = home / "klipper" / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True)
    exclude.write_text("")
    for relative, text in sc.files.items():
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return home


def observe(home: Path) -> Dict[str, object]:
    cfg = home / "printer_data" / "config"
    extras = home / "klipper" / "klippy" / "extras"
    root = str(home).encode()
    return {
        "tree": {
            re.sub(r"\.(backup|bak)\.\d{14}", r".\1.<DATE>", str(p.relative_to(cfg))):
                p.read_bytes().replace(root, b"<HOME>")
            for p in sorted(cfg.rglob("*"))
            if p.is_file()
        },
        "links": {p.name: os.readlink(p) for p in sorted(extras.iterdir()) if p.is_symlink()},
        "extras_files": {
            p.name: p.read_bytes()
            for p in sorted(extras.iterdir())
            if p.is_file() and not p.is_symlink()
        },
        "exclude": sorted((home / "klipper" / ".git" / "info" / "exclude").read_text().split()),
    }


def call_tag(call: List[str]) -> str:
    """Name an external command by a short tag."""
    if call[:3] == ["sudo", "systemctl", "restart"]:
        return f"RESTART:{call[3]}"
    if call[:3] == ["su", "root", "-c"]:
        if call[3] == "/etc/init.d/S60klipper restart":
            return "U1RESTART"
        if call[3].startswith("touch ") and call[3].endswith("/.debug"):
            return "U1DEBUG"
    if call[:3] == ["chown", "-R", "lava:lava"]:
        return "CHOWN"
    raise AssertionError(f"unexpected external command: {call}")
