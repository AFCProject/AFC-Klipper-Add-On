from pathlib import Path
from typing import Callable, List, Optional

import pytest

from installer.install_flow.exit import exit_afc_install
from installer.install_flow.install import install_afc
from installer.install_flow.snapmaker import (
    check_and_move_lite_files,
    comment_gcode_in_fluidd,
    move_lite_files_back,
)
from installer.install_flow.uninstall import uninstall_afc
from installer.install_flow.utilities import del_var_file
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from tests.installer.fakes import FakeCommandRunner, RecordingConsole, ScriptedPrompter
from tests.installer.sandbox import (
    REPO_ROOT,
    Scenario,
    sandbox,
)

IDLE = "query_printer_status() { return 0; }"
BUSY = "query_printer_status() { return 1; }"
UNINSTALLED_TEXT = (
    "\n  AFC has been uninstalled successfully.\n\n"
    "  Please restart your printer to complete the uninstallation process.\n\n"
    "  We always welcome feedback on the software on our Discord channel at\n\n"
    "  https://discord.gg/z2tgWEnfDT.\n  "
)


def printer_idle(idle: bool) -> Callable[[str], bool]:
    return lambda _url: idle


def install_then_uninstall(idle: bool) -> Callable[..., Optional[str]]:
    def action(ctx, console, prompter, runner, options):  # type: ignore[no-untyped-def]
        install_afc(ctx, console, prompter, runner, options)
        return uninstall_afc(ctx, console, runner, printer_idle(idle))

    return action


class TestUninstallAfc:
    def ctx(self, tmp_path: Path) -> Context:
        home = sandbox(tmp_path, Scenario())
        return Context.create(REPO_ROOT, home=home)

    def test_the_message_thanks_the_user_and_links_the_discord(self, tmp_path: Path) -> None:
        text = uninstall_afc(self.ctx(tmp_path), RecordingConsole(), FakeCommandRunner(),
                             printer_idle(False))
        assert text == UNINSTALLED_TEXT

    def test_a_missing_klipper_extras_directory_stops_with_the_error(
        self, tmp_path: Path
    ) -> None:
        home = sandbox(tmp_path, Scenario())
        ctx = Context.create(REPO_ROOT, home=home, klipper_dir=home / "nowhere")
        console = RecordingConsole()
        with pytest.raises(InstallerExit) as exc:
            uninstall_afc(ctx, console, FakeCommandRunner(), printer_idle(True))
        assert exc.value.code == 1
        assert console.messages == [
            ("ERROR", "AFC Klipper extensions not uninstalled; Klipper extras directory not found.")
        ]
        assert ctx.printer_config_dir.joinpath("printer.cfg").read_text().count("include") == 0

    def test_an_idle_printer_gets_a_klipper_restart_and_a_busy_one_a_warning(
        self, tmp_path: Path
    ) -> None:
        runner, console = FakeCommandRunner(existing=["systemctl"]), RecordingConsole()
        uninstall_afc(self.ctx(tmp_path), console, runner, printer_idle(True))
        assert runner.calls == [["sudo", "systemctl", "restart", "klipper"]]
        runner, console = FakeCommandRunner(), RecordingConsole()
        uninstall_afc(self.ctx(tmp_path / "again"), console, runner, printer_idle(False))
        assert runner.calls == []
        assert console.messages[0][0] == "WARNING"

    def test_snapmaker_restores_the_lite_files_and_uncomments_the_fluidd_macros(
        self, tmp_path: Path
    ) -> None:
        ctx = self.ctx(tmp_path)
        ctx.is_snapmaker = True
        extras = ctx.klipper_extra_path
        (extras / "AFC.py.lite").write_text("original")
        (extras / "AFC.py").write_text("afc replacement")
        fluidd = ctx.printer_config_dir / "fluidd.cfg"
        fluidd.write_text("# [gcode_macro T0]\n# gcode:\n#   SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\n")
        console = RecordingConsole()
        uninstall_afc(ctx, console, FakeCommandRunner(), printer_idle(False))
        assert (extras / "AFC.py").read_text() == "original"
        assert not (extras / "AFC.py.lite").exists()
        expected = "[gcode_macro T0]\ngcode:\n  SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\n"
        assert fluidd.read_text() == expected
        assert console.messages[0] == (
            "OTHER", "Uncommenting gcode_macro T(n) lines in fluidd.cfg file"
        )
        assert console.messages[-1] == (
            "WARNING",
            "Please manually restart klipper through <ip_address>/firmware-config panel, "
            "or fully power-cycle printer.",
        )

    def test_snapmaker_restarts_through_the_u1_script_when_idle(self, tmp_path: Path) -> None:
        ctx = self.ctx(tmp_path)
        ctx.is_snapmaker = True
        (ctx.printer_config_dir / "fluidd.cfg").write_text("")
        runner = FakeCommandRunner()
        uninstall_afc(ctx, RecordingConsole(), runner, printer_idle(True), euid=1000)
        assert runner.calls == [["su", "root", "-c", "/etc/init.d/S60klipper restart"]]


class TestExitAfcInstall:
    def ctx(self, tmp_path: Path, **kwargs: object) -> Context:
        home = sandbox(tmp_path, Scenario())
        return Context.create(REPO_ROOT, home=home, **kwargs)  # type: ignore[arg-type]

    def test_after_changes_it_announces_restarts_klipper_and_exits_zero(
        self, tmp_path: Path
    ) -> None:
        ctx = self.ctx(tmp_path)
        ctx.files_updated_or_installed = True
        console, runner = RecordingConsole(), FakeCommandRunner(existing=["systemctl"])
        with pytest.raises(InstallerExit) as exc:
            exit_afc_install(ctx, console, runner, printer_idle(True))
        assert exc.value.code == 0
        assert console.messages == [("INFO", "Restarting Klipper service to apply changes...")]
        assert runner.calls == [["sudo", "systemctl", "restart", "klipper"]]

    def test_test_mode_skips_the_restart_and_says_so(self, tmp_path: Path) -> None:
        ctx = self.ctx(tmp_path, test_mode=True)
        ctx.files_updated_or_installed = True
        console, runner = RecordingConsole(), FakeCommandRunner()
        with pytest.raises(InstallerExit):
            exit_afc_install(ctx, console, runner, printer_idle(True))
        assert console.messages == [("INFO", "Test mode enabled; skipping Klipper restart.")]
        assert runner.calls == []

    def test_with_nothing_changed_it_is_silent_and_never_restarts(self, tmp_path: Path) -> None:
        console, runner = RecordingConsole(), FakeCommandRunner()
        with pytest.raises(InstallerExit) as exc:
            exit_afc_install(self.ctx(tmp_path), console, runner, printer_idle(True))
        assert exc.value.code == 0
        assert console.messages == [] and runner.calls == []

    def test_it_always_removes_the_tool_files_first(self, tmp_path: Path) -> None:
        ctx = self.ctx(tmp_path)
        ctx.afc_config_dir.mkdir(parents=True)
        (ctx.afc_config_dir / "AFC.var.tool").write_text("")
        (ctx.afc_config_dir / "AFC.var.unit").write_text("keep")
        with pytest.raises(InstallerExit):
            exit_afc_install(ctx, RecordingConsole(), FakeCommandRunner(), printer_idle(True))
        assert sorted(p.name for p in ctx.afc_config_dir.iterdir()) == ["AFC.var.unit"]


class TestDelVarFile:
    def ctx_with(self, tmp_path: Path, present: bool) -> Context:
        ctx = Context.create(REPO_ROOT, home=tmp_path)
        ctx.afc_config_dir.mkdir(parents=True)
        if present:
            (ctx.afc_config_dir / "AFC.var.unit").write_text("{}")
        return ctx

    def run(
        self, ctx: Context, answers: List[str]
    ) -> "tuple[str, RecordingConsole, ScriptedPrompter]":
        console, prompter = RecordingConsole(), ScriptedPrompter(answers)
        return del_var_file(ctx, console, prompter), console, prompter

    def test_a_confirmed_removal_deletes_the_file(self, tmp_path: Path) -> None:
        ctx = self.ctx_with(tmp_path, True)
        message, console, prompter = self.run(ctx, ["y"])
        assert message == "Removed old AFC.var.unit file.\n"
        assert not (ctx.afc_config_dir / "AFC.var.unit").exists()
        assert prompter.prompts == ["Do you want to proceed? (y/n): "]
        assert console.messages == [(
            "OTHER",
            "This is generally a troubleshooting step and should not be needed during "
            "normal operation.",
        )]

    def test_a_missing_file_is_reported(self, tmp_path: Path) -> None:
        message = self.run(self.ctx_with(tmp_path, False), ["y"])[0]
        assert message == "No AFC.var.unit file found to remove.\n"

    @pytest.mark.parametrize("answer", ["n", "", "yes", "no", "maybe"])
    def test_anything_but_y_cancels_and_keeps_the_file(
        self, tmp_path: Path, answer: str
    ) -> None:
        ctx = self.ctx_with(tmp_path, True)
        assert self.run(ctx, [answer])[0] == "Operation cancelled by user.\n"
        assert (ctx.afc_config_dir / "AFC.var.unit").exists()

    def test_the_answer_is_case_insensitive_and_trimmed(self, tmp_path: Path) -> None:
        ctx = self.ctx_with(tmp_path, True)
        assert self.run(ctx, ["  Y "])[0] == "Removed old AFC.var.unit file.\n"

    def test_end_of_input_stops_the_installer(self, tmp_path: Path) -> None:
        with pytest.raises(InstallerExit) as exc:
            self.run(self.ctx_with(tmp_path, True), [])
        assert exc.value.code == 1


class TestSnapmakerLiteFiles:
    NAMES = ["AFC.py", "AFC_unit.py", "AFC_lane.py"]

    def ctx(self, tmp_path: Path) -> Context:
        home = sandbox(tmp_path, Scenario())
        ctx = Context.create(REPO_ROOT, home=home)
        for name in self.NAMES:
            (ctx.klipper_extra_path / name).write_text(f"original {name}")
        return ctx

    def test_each_original_is_copied_to_a_lite_backup_once(
        self, tmp_path: Path
    ) -> None:
        ctx = self.ctx(tmp_path)
        console = RecordingConsole()
        check_and_move_lite_files(ctx, console)
        assert console.messages == [
            ("OTHER", "Copying AFC.py to AFC.py.lite"),
            ("OTHER", "Copying AFC_unit.py to AFC.py.lite"),
            ("OTHER", "Copying AFC_lane.py to AFC.py.lite"),
        ]
        for name in self.NAMES:
            assert (ctx.klipper_extra_path / f"{name}.lite").read_text() == f"original {name}"

    def test_an_existing_lite_backup_is_never_overwritten(self, tmp_path: Path) -> None:
        ctx = self.ctx(tmp_path)
        (ctx.klipper_extra_path / "AFC.py.lite").write_text("the true original")
        (ctx.klipper_extra_path / "AFC.py").write_text("afc now")
        console = RecordingConsole()
        check_and_move_lite_files(ctx, console)
        assert (ctx.klipper_extra_path / "AFC.py.lite").read_text() == "the true original"
        assert [m[1] for m in console.messages] == [
            "Copying AFC_unit.py to AFC.py.lite", "Copying AFC_lane.py to AFC.py.lite",
        ]

    def test_moving_back_restores_each_original_over_the_replacement(
        self, tmp_path: Path
    ) -> None:
        ctx = self.ctx(tmp_path)
        check_and_move_lite_files(ctx, RecordingConsole())
        for name in self.NAMES:
            (ctx.klipper_extra_path / name).write_text("replacement")
        move_lite_files_back(ctx)
        for name in self.NAMES:
            assert (ctx.klipper_extra_path / name).read_text() == f"original {name}"
            assert not (ctx.klipper_extra_path / f"{name}.lite").exists()

    def test_moving_back_replaces_a_symlink_with_the_original_file(self, tmp_path: Path) -> None:
        ctx = self.ctx(tmp_path)
        (ctx.klipper_extra_path / "AFC.py.lite").write_text("original")
        (ctx.klipper_extra_path / "AFC.py").unlink()
        # the link target must be a throwaway file: a bug that writes through the link
        # must never be able to touch a real file in the repository
        target = tmp_path / "linked_afc.py"
        target.write_text("link target")
        (ctx.klipper_extra_path / "AFC.py").symlink_to(target)
        move_lite_files_back(ctx)
        assert not (ctx.klipper_extra_path / "AFC.py").is_symlink()
        assert (ctx.klipper_extra_path / "AFC.py").read_text() == "original"
        assert target.read_text() == "link target"  # replaced the link, not written through it

    def test_moving_back_with_no_lite_files_does_nothing(self, tmp_path: Path) -> None:
        ctx = self.ctx(tmp_path)
        move_lite_files_back(ctx)
        assert (ctx.klipper_extra_path / "AFC.py").read_text() == "original AFC.py"


class TestCommentGcodeInFluidd:
    TEXT = "[gcode_macro T0]\ngcode:\n  SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\n[keep]\nx: 1\n"

    def ctx(self, tmp_path: Path) -> Context:
        ctx = Context.create(REPO_ROOT, home=tmp_path)
        ctx.printer_config_dir.mkdir(parents=True)
        (ctx.printer_config_dir / "fluidd.cfg").write_text(self.TEXT)
        return ctx

    def test_commenting_announces_and_prefixes_the_macro_range(self, tmp_path: Path) -> None:
        ctx = self.ctx(tmp_path)
        console = RecordingConsole()
        comment_gcode_in_fluidd(ctx, console, comment=True)
        assert console.messages == [("OTHER", "Commenting out gcode_macro T(n) in fluidd.cfg file")]
        assert (ctx.printer_config_dir / "fluidd.cfg").read_text() == (
            "# [gcode_macro T0]\n# gcode:\n#   SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\n[keep]\nx: 1\n"
        )

    def test_uncommenting_reverses_it(self, tmp_path: Path) -> None:
        ctx = self.ctx(tmp_path)
        comment_gcode_in_fluidd(ctx, RecordingConsole(), comment=True)
        console = RecordingConsole()
        comment_gcode_in_fluidd(ctx, console, comment=False)
        assert console.messages == [
            ("OTHER", "Uncommenting gcode_macro T(n) lines in fluidd.cfg file")
        ]
        assert (ctx.printer_config_dir / "fluidd.cfg").read_text() == self.TEXT

    def test_a_missing_fluidd_cfg_stops_with_sed_s_exit_status(self, tmp_path: Path) -> None:
        ctx = Context.create(REPO_ROOT, home=tmp_path)
        ctx.printer_config_dir.mkdir(parents=True)
        console = RecordingConsole()
        with pytest.raises(InstallerExit) as exc:
            comment_gcode_in_fluidd(ctx, console, comment=True)
        assert exc.value.code == 2
        assert console.messages[-1] == (
            "ERROR",
            f"sed: can't read {ctx.printer_config_dir / 'fluidd.cfg'}: No such file or directory",
        )

