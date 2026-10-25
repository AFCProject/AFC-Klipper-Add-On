import os
import subprocess
from pathlib import Path
from typing import Dict

import pytest

from installer.install_flow.install import InstallOptions, install_afc
from installer.install_flow.snapmaker import (
    check_and_move_afc_files,
    u1_disable,
    u1_enable,
)
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import FakeCommandRunner, RecordingConsole, ScriptedPrompter
from tests.installer.sandbox import (
    REPO_ROOT,
    Scenario,
    call_tag,
    observe,
    sandbox,
)

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
not_root = pytest.mark.skipif(IS_ROOT, reason="would write to the real /oem as root")
U1_SCRIPT = REPO_ROOT / "include" / "U1_extended_firmware_install.sh"
FLUIDD = (
    "[gcode_macro T0]\ngcode:\n  SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\n\n"
    "[gcode_macro T1]\ngcode:\n  SWITCH_OF_EXTENDED_EXTRUDER INDEX=1\n\n[printer]\nx: 1\n"
)
U1_FILES = {
    "printer_data/config/fluidd.cfg": FLUIDD,
    "klipper/klippy/extras/AFC.py": "snapmaker AFC",
    "klipper/klippy/extras/AFC_unit.py": "snapmaker unit",
    "klipper/klippy/extras/AFC_lane.py": "snapmaker lane",
}
OEM_TAG = "OEM"


def tree_of(root: Path) -> Dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


class TestSnapmakerInstallBehavior:
    def test_root_creates_the_debug_file_itself_without_asking(self, tmp_path: Path) -> None:
        home = sandbox(tmp_path, Scenario(files=U1_FILES))
        ctx = Context.create(REPO_ROOT, home=home)
        ctx.is_snapmaker = True
        oem = tmp_path / "oem"
        oem.mkdir()
        runner = FakeCommandRunner()
        install_afc(
            ctx, RecordingConsole(), ScriptedPrompter([]), runner,
            InstallOptions(selection=UnitSelection()), euid=0, oem_dir=oem,
        )
        assert (oem / ".debug").is_file()
        assert runner.calls == []

    def test_a_normal_install_never_touches_the_oem_directory(self, tmp_path: Path) -> None:
        home = sandbox(tmp_path, Scenario())
        ctx = Context.create(REPO_ROOT, home=home)
        runner = FakeCommandRunner(existing=["systemctl"])
        install_afc(
            ctx, RecordingConsole(), ScriptedPrompter([]), runner,
            InstallOptions(selection=UnitSelection()), oem_dir=tmp_path / "never",
        )
        assert not (tmp_path / "never").exists()
        assert all(call[0] != "su" for call in runner.calls)


def link_checkout(home: Path) -> Path:
    """Make ``~/AFC-Klipper-Add-On`` point at the repo, where the firmware script looks."""
    link = home / "AFC-Klipper-Add-On"
    if not link.exists():
        link.symlink_to(REPO_ROOT)
    return link


def python_u1(home: Path, action: str, oem: Path) -> Dict[str, object]:
    ctx = Context.create(link_checkout(home), home=home)
    ctx.is_snapmaker = True
    console, runner = RecordingConsole(), FakeCommandRunner()
    if action == "enable":
        u1_enable(ctx, console, runner, euid=1000, oem_dir=oem)
    else:
        u1_disable(ctx, console, runner, euid=1000)
    seen = observe(home)
    seen["stdout"] = "".join(f"{text}\n" for _kind, text in console.messages)
    seen["calls"] = [call_tag(call) for call in runner.calls]
    return seen


def normalized_links(seen: Dict[str, object], home: Path) -> Dict[str, str]:
    links = seen["links"]
    assert isinstance(links, dict)
    return {name: target.replace(str(home), "<HOME>") for name, target in links.items()}


def observe_tree(home: Path) -> Dict[str, bytes]:
    tree = observe(home)["tree"]
    assert isinstance(tree, dict)
    return {k: v.replace(str(home).encode(), b"<HOME>") for k, v in tree.items()}


class TestEnableDisableBehavior:
    def setup(self, tmp_path: Path) -> Context:
        home = sandbox(tmp_path, Scenario(files=U1_FILES))
        return Context.create(REPO_ROOT, home=home)

    def test_enable_runs_the_steps_in_the_firmware_scripts_order(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        console, runner = RecordingConsole(), FakeCommandRunner()
        u1_enable(ctx, console, runner, euid=1000, oem_dir=tmp_path / "oem")
        assert [m[1] for m in console.messages if m[1].startswith(
            ("Enabling", "Adding", "Symlinking", "AFC enabled")
        )] == [
            "Enabling AFC", "Adding AFC include to printer.cfg file",
            "Symlinking AFC files to klipper extras folder", "AFC enabled. Restarting Klipper...",
        ]
        assert [call_tag(c) for c in runner.calls] == ["U1DEBUG", "CHOWN", "U1RESTART"]
        assert runner.calls[1] == ["chown", "-R", "lava:lava", str(ctx.afc_config_dir)]

    def test_root_writes_the_debug_file_and_restarts_without_su(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        oem = tmp_path / "oem"
        oem.mkdir()
        runner = FakeCommandRunner()
        u1_enable(ctx, RecordingConsole(), runner, euid=0, oem_dir=oem)
        assert (oem / ".debug").is_file()
        assert runner.calls[-1] == ["/etc/init.d/S60klipper", "restart"]

    def test_a_failing_chown_stops_the_script_with_its_status(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        failing = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        runner = FakeCommandRunner(
            results={("chown", "-R", "lava:lava", str(ctx.afc_config_dir)): failing}
        )
        with pytest.raises(InstallerExit) as exc:
            u1_enable(ctx, RecordingConsole(), runner, euid=1000, oem_dir=tmp_path / "oem")
        assert exc.value.code == 1
        assert "CHOWN" in [call_tag(c) for c in runner.calls]
        assert "U1RESTART" not in [call_tag(c) for c in runner.calls]

    def test_disable_runs_the_steps_in_the_firmware_scripts_order(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        u1_enable(ctx, RecordingConsole(), FakeCommandRunner(), euid=1000, oem_dir=tmp_path / "o")
        console, runner = RecordingConsole(), FakeCommandRunner()
        u1_disable(ctx, console, runner, euid=1000)
        steps = [m[1] for m in console.messages if m[1].startswith(
            ("Disabling", "Removing", "Moving", "AFC disabled")
        )]
        assert steps == [
            "Disabling AFC", "Removing AFC include from printer.cfg file",
            "Removing AFC symlinks in klipper extras folder", "Moving AFC lite files back",
            "AFC disabled. Restarting Klipper...",
        ]
        assert [call_tag(c) for c in runner.calls] == ["U1RESTART"]
        assert (ctx.klipper_extra_path / "AFC.py").read_text() == "snapmaker AFC"

    def test_disable_with_klipper_extras_missing_stops_with_the_error(self, tmp_path: Path) -> None:
        home = sandbox(tmp_path, Scenario(files=U1_FILES))
        ctx = Context.create(REPO_ROOT, home=home, klipper_dir=home / "nowhere")
        with pytest.raises(InstallerExit) as exc:
            u1_disable(ctx, RecordingConsole(), FakeCommandRunner(), euid=1000)
        assert exc.value.code == 1


class TestCheckAndMoveAfcFilesWhenPresent:
    def test_an_existing_afc_directory_is_left_alone(self, tmp_path: Path) -> None:
        home = sandbox(tmp_path, Scenario(files={"printer_data/config/AFC/keep.cfg": "mine\n"}))
        ctx = Context.create(REPO_ROOT, home=home)
        console = RecordingConsole()
        check_and_move_afc_files(ctx, console)
        assert console.messages == []
        assert [p.name for p in ctx.afc_config_dir.iterdir()] == ["keep.cfg"]
