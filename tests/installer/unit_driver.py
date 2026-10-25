"""Replays the recorded unit cases against the Python code.

``run_python_case`` produces the text a case is recorded as, so the files in
``fixtures/unit_cases`` are the expected results.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from installer.platform_core.console import Console
from installer.platform_core.context import Context
from installer.unit_registry.registry import (
    additional_default_name,
    name_additional_unit,
    unit_for,
)
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import ScriptedPrompter
from tests.installer.unit_cases import iter_cases

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "installer" / "fixtures" / "unit_cases"

# Fixture groups replayed by another test module instead of this driver. The coverage test
# fails if a fixture belongs to neither a replayed mode nor this list.
REPLAYED_ELSEWHERE: Dict[str, str] = {
    "message": "tests/installer/test_install_flow_install.py (whole build_install_message)",
}


def all_cases() -> List[Tuple[str, str, Dict[str, str]]]:
    return list(iter_cases())


def selection_from(overrides: Dict[str, str]) -> UnitSelection:
    """Map the variable names a recorded case sets onto a UnitSelection."""
    sel = UnitSelection()
    names = {
        "installation_type": "installation_type",
        "boxturtle_name": "unit_name",
        "htlf_board_type": "htlf_board_type",
        "nightowl_board_type": "nightowl_board_type",
        "htlf2_board_type": "claymore_board_type",
        "qb_board_type": "qb_board_type",
        "qb_motor_type": "qb_motor_type",
        "emu_board_type": "emu_board_type",
    }
    for key, value in overrides.items():
        if key in names:
            setattr(sel, names[key], value)
        elif key == "emu_num_lanes":
            sel.emu_num_lanes = int(value)
        elif key == "is_additional_unit":
            sel.is_additional_unit = value == "True"
        elif key == "turtle_renamed":
            sel.turtle_renamed = value == "True"
    return sel


def dump_config_dir(afc_config_dir: Path) -> str:
    """The ``=== FILES ===`` block: every file, sorted, between marker lines."""
    out = ["=== FILES ===\n"]
    files = sorted(
        str(p.relative_to(afc_config_dir)) for p in afc_config_dir.rglob("*") if p.is_file()
    )
    for rel in files:
        content = (afc_config_dir / rel).read_bytes().decode("utf-8")
        # the recorded text folds CRLF (e.g. the ERB_2.0 mcu file) into LF, so the fixtures
        # are LF; byte-exact copies are tested apart
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        out.append(f"--- {rel} ---\n{content}--- end {rel} ---\n")
    return "".join(out)


# The recorded rows start with a key letter, while the menus now number their options.
# The replay maps each option back to the letter it was recorded with.
LEGACY_KEYS = {
    "Board Type": {"HTLF": "D", "QuattroBox": "E", "EMU": "H", "Claymore": "H", "NightOwl": "J"},
    "Motor Type": {"QuattroBox": "F"},
    "Lane Count": {"EMU": "G"},
}


def legacy_rows(options: List[Any], sel: Any, additional: bool) -> List[Tuple[str, str]]:
    """Render options as the (key, text) pairs the recorded cases hold."""
    rows = []
    for option in options:
        value = option.value(sel)
        if option.label.endswith(" Name"):
            key = "1" if additional else "C"
            text = f"{option.label}{':' if additional else ' :'} {value}"
        else:
            unit, _, kind = option.label.partition(" ")
            key = LEGACY_KEYS[kind][unit]
            text = f"{option.label} : {value}"
        rows.append((key, text))
    return rows


def render_rows(rows: List[Tuple[str, str]]) -> str:
    return "".join(f"{key}. {text} \n" for key, text in rows)


def run_python_case(mode: str, overrides: Dict[str, str], workdir: Optional[Path] = None) -> str:
    """Replay one recorded case and return the text it is recorded as."""
    if workdir is None:
        with tempfile.TemporaryDirectory() as tmp:
            return run_python_case(mode, overrides, Path(tmp))
    ctx = Context.create(REPO_ROOT, home=workdir, printer_config_dir=workdir)
    sel = selection_from(overrides)
    cfg = ctx.afc_config_dir
    unit = unit_for(sel.installation_type)
    assert unit is not None, sel.installation_type
    console, prompter = Console(open("/dev/null", "w")), ScriptedPrompter([])
    if mode in ("copy_unit_files", "install_additional_unit"):
        (cfg / "mcu").mkdir(parents=True)
        (cfg / "macros").mkdir()
        if mode == "copy_unit_files":
            unit.copy_files(ctx, console, prompter, sel)
        else:
            (cfg / "AFC_Turtle_1.cfg").write_text("placeholder\n")
            unit.install_additional(ctx, console, prompter, sel)
        return dump_config_dir(cfg)
    if mode == "buffer_target":
        target = unit.buffer_target(ctx, sel)
        values = ["", "", "", "", ""]
        if target is not None:
            relative = str(target.extruder_file.relative_to(cfg))
            values = [
                target.unit_name,
                target.unit_section_prefix,
                relative,
                target.section_name,
                target.prebaked_header,
            ]
        names = [
            "buffer_unit_name",
            "buffer_unit_section_prefix",
            "buffer_extruder_file",
            "buffer_section_name",
            "buffer_prebaked_header",
        ]
        return "=== VARS ===\n" + "".join(f"{n}={v}\n" for n, v in zip(names, values))
    if mode == "install_menu_options":
        return "=== OUTPUT ===\n" + render_rows(legacy_rows(unit.install_options(), sel, False))
    if mode == "additional_menu_row":
        unit.refresh_additional_name(ctx, sel)
        rows = legacy_rows(unit.additional_options(), sel, True)
        variables = f"=== VARS ===\nboxturtle_name={sel.unit_name}\n"
        return "=== OUTPUT ===\n" + render_rows(rows) + variables
    if mode == "name_additional_unit":
        # the recording answered with nothing, which takes the default; an empty line does too
        name_additional_unit(ctx, console, ScriptedPrompter([""]), sel)
        return (
            f"=== VARS ===\nboxturtle_name={sel.unit_name}\nturtle_renamed={sel.turtle_renamed}\n"
        )
    if mode == "additional_default_name":
        cfg.mkdir(parents=True)
        for name in filter(None, overrides.get("seed_files", "").split(",")):
            (cfg / name).write_text("placeholder\n")
        return f"=== NAME ===\n{additional_default_name(ctx, sel)}\n"
    raise ValueError(f"unsupported mode {mode}")


def replayable_cases() -> Iterator[Tuple[str, str, Dict[str, str]]]:
    """Cases whose mode is replayed and whose unit type is registered."""
    for name, mode, overrides in all_cases():
        if mode in REPLAYED_ELSEWHERE or mode not in (
            "copy_unit_files",
            "install_additional_unit",
            "buffer_target",
            "install_menu_options",
            "additional_menu_row",
            "name_additional_unit",
            "additional_default_name",
        ):
            continue
        if unit_for(overrides.get("installation_type", "")) is not None:
            yield name, mode, overrides

