import io
import subprocess
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import pytest

from installer import app
from installer.checks import checks
from installer.app import AppDeps, SCRIPT_DIR, run_install, run_u1, run_update
from installer.platform_core.console import Console
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.ui_menus.renderer import AnsiRenderer
from tests.installer.fakes import FakeCommandRunner, PrinterTree, ScriptedPrompter


class Reexecuted(Exception):
    """Raised by the fake re-exec, since the real one never returns."""


def reexec() -> None:
    raise Reexecuted()


def make_deps(
    tree: PrinterTree, answers: Sequence[str], **kwargs: Any
) -> Tuple[AppDeps, io.StringIO, FakeCommandRunner, List[float]]:
    stream = io.StringIO()
    console = Console(stream)
    runner = kwargs.pop("runner", FakeCommandRunner())
    sleeps: List[float] = []
    deps = AppDeps(
        console=console, prompter=ScriptedPrompter(answers),  # type: ignore[arg-type]
        runner=runner, script_dir=tree.afc_path, home=tree.home, env={},
        sleep=sleeps.append, renderer=AnsiRenderer(console), euid=1000, **kwargs,
    )
    return deps, stream, runner, sleeps


@pytest.fixture
def tree(printer_tree: PrinterTree) -> PrinterTree:
    (printer_tree.afc_path / ".git").mkdir()
    return printer_tree


class TestRunInstall:
    def test_quit_from_the_main_menu_exits_zero_after_the_startup_steps(
        self, tree: PrinterTree
    ) -> None:
        deps, stream, _, sleeps = make_deps(tree, ["Q"])
        assert run_install(["-t"], deps) == 0
        text = stream.getvalue()
        assert "Ensuring we are not running as root (except on K1 OS)..." in text
        assert "Starting installation process.." in text
        assert "Prior AFC-Klipper-Add-On installation detected" in text
        assert sleeps == [2.0]

    def test_end_of_input_exits_one(self, tree: PrinterTree) -> None:
        deps, _, _, _ = make_deps(tree, [])
        assert run_install(["-t"], deps) == 1

    def test_help_exits_zero_and_a_bad_flag_exits_one(
        self, tree: PrinterTree, capsys: pytest.CaptureFixture
    ) -> None:
        deps, _, _, sleeps = make_deps(tree, ["Q"])
        assert run_install(["-h"], deps) == 0
        assert capsys.readouterr().out.startswith("Usage: install-afc.sh [options]")
        assert run_install(["-x"], deps) == 1
        assert capsys.readouterr().err == "install-afc.sh: illegal option -- x\n"
        assert sleeps == []

    def test_the_invoked_name_comes_from_the_environment(
        self, tree: PrinterTree, capsys: pytest.CaptureFixture
    ) -> None:
        deps, _, _, _ = make_deps(tree, [])
        deps.env = {"AFC_PROG": "./install-afc.sh"}
        run_install(["-h"], deps)
        assert "\n ./install-afc.sh [-a <moonraker address>]" in capsys.readouterr().out

    def test_interrupt_exits_130(self, tree: PrinterTree) -> None:
        class Interrupting(ScriptedPrompter):
            def ask(self, prompt: str) -> Optional[str]:
                raise KeyboardInterrupt()

        deps, _, _, _ = make_deps(tree, [])
        deps.prompter = Interrupting([])  # type: ignore[assignment]
        assert run_install(["-t"], deps) == 130

    def test_a_branch_switch_restarts_before_anything_else_runs(
        self, tree: PrinterTree
    ) -> None:
        runner = FakeCommandRunner()
        deps, stream, _, sleeps = make_deps(tree, ["Q"], runner=runner)
        deps.reexec = reexec
        # the fake runner answers "" for HEAD, which is not "dev", so the switch happens
        with pytest.raises(Reexecuted):
            run_install(["-b", "dev", "-t"], deps)
        assert "Switching to branch 'dev' before loading" in stream.getvalue()
        assert sleeps == []

    def test_installing_a_unit_through_the_menus(self, tree: PrinterTree) -> None:
        deps, stream, _, _ = make_deps(tree, ["I", "1", "10", "I", "Q"])
        assert run_install(["-t"], deps) == 0
        assert (tree.printer_config_dir / "AFC" / "AFC.cfg").is_file()
        assert (tree.klipper_extras / "AFC.py").is_symlink()
        assert "Test mode enabled; skipping Klipper restart." in stream.getvalue()

    def test_the_real_reexec_uses_the_shim_from_the_environment(
        self, tree: PrinterTree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: List[Tuple[str, List[str]]] = []
        monkeypatch.setattr(
            "installer.git_sync.os.execv", lambda path, argv: calls.append((path, argv))
        )
        deps, _, _, _ = make_deps(tree, ["Q"])
        deps.env = {"AFC_SHIM": "/x/install-afc.sh"}
        run_install(["-b", "dev", "-t"], deps)
        assert calls[0] == ("/x/install-afc.sh", ["/x/install-afc.sh", "-b", "dev", "-t"])

    def test_the_default_shim_is_install_afc_in_the_script_dir(
        self, tree: PrinterTree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: List[Tuple[str, List[str]]] = []
        monkeypatch.setattr(
            "installer.git_sync.os.execv", lambda path, argv: calls.append((path, argv))
        )
        deps, _, _, _ = make_deps(tree, ["Q"])
        run_install(["-b", "dev", "-t"], deps)
        assert calls[0][0] == str(tree.afc_path / "install-afc.sh")


class TestRunUpdate:
    def test_quit_exits_zero_after_the_startup_steps(self, tree: PrinterTree) -> None:
        deps, stream, _, sleeps = make_deps(tree, ["Q"])
        assert run_update(["-t"], deps) == 0
        assert "Starting update process..." in stream.getvalue()
        assert sleeps == [2.0]

    def test_updating_through_the_menu_shows_the_message_on_the_next_screen(
        self, tree: PrinterTree
    ) -> None:
        deps, stream, runner, _ = make_deps(tree, ["U", "n", "Q"])
        assert run_update(["-t"], deps) == 0
        text = stream.getvalue()
        assert "AFC Klipper Add-On updated successfully with version v1.2.8-." in text
        assert (tree.klipper_extras / "AFC.py").is_symlink()

    def test_errors_and_help(self, tree: PrinterTree, capsys: pytest.CaptureFixture) -> None:
        deps, _, _, _ = make_deps(tree, [])
        assert run_update(["-h"], deps) == 0
        assert run_update(["-x"], deps) == 1
        assert run_update(["-t"], deps) == 1
        assert capsys.readouterr().err == "update-afc.sh: illegal option -- x\n"

    def test_interrupt_exits_130(self, tree: PrinterTree) -> None:
        class Interrupting(ScriptedPrompter):
            def ask(self, prompt: str) -> Optional[str]:
                raise KeyboardInterrupt()

        deps, _, _, _ = make_deps(tree, [])
        deps.prompter = Interrupting([])  # type: ignore[assignment]
        assert run_update(["-t"], deps) == 130

    def test_a_self_update_restarts_through_update_afc_sh(
        self, tree: PrinterTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: List[Tuple[str, List[str]]] = []
        monkeypatch.setattr(
            "installer.git_sync.os.execv", lambda path, argv: calls.append((path, argv))
        )
        venv = tmp_path / "venv"
        venv.mkdir()
        python = venv / "python"
        python.write_text("#!/bin/sh\n")
        python.chmod(0o755)
        base = ["git", "-C", str(tree.afc_path)]
        runner = FakeCommandRunner({
            (str(python), "-c", checks._VERSION_PROGRAM): subprocess.CompletedProcess(
                [], 0, stdout="3.11\n", stderr=""),
            tuple(base + ["status", "--porcelain", "--branch"]): subprocess.CompletedProcess(
                [], 0, stdout="## main...origin/main [behind 2]\n", stderr=""),
        })
        deps, _, _, _ = make_deps(tree, ["Q"], runner=runner)
        run_update(["-y", str(venv)], deps)
        script = str(tree.afc_path / "update-afc.sh")
        assert calls[0] == (script, [script, "-y", str(venv)])

    def test_does_not_switch_branches_early(self, tree: PrinterTree) -> None:
        deps, _, runner, _ = make_deps(tree, ["Q"])
        run_update(["-b", "dev", "-t"], deps)
        assert runner.calls == []


class TestRunU1:
    @pytest.fixture
    def recorded(self, monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, Path, Optional[int]]]:
        calls: List[Tuple[str, Path, Optional[int]]] = []

        def make(name: str) -> Any:
            def fake(ctx: Context, console: Any, runner: Any, euid: Optional[int] = None) -> None:
                calls.append((name, ctx.home, euid))
            return fake

        monkeypatch.setattr(app, "u1_enable", make("enable"))
        monkeypatch.setattr(app, "u1_disable", make("disable"))
        return calls

    def test_flags_run_in_the_order_given(
        self, tree: PrinterTree, recorded: list
    ) -> None:
        deps, _, _, _ = make_deps(tree, [])
        assert run_u1(["-ed", "-e"], deps) == 0
        assert [name for name, _, _ in recorded] == ["enable", "disable", "enable"]

    def test_no_flags_does_nothing(self, tree: PrinterTree, recorded: list) -> None:
        deps, _, _, _ = make_deps(tree, [])
        assert run_u1([], deps) == 0
        assert recorded == []

    @pytest.mark.parametrize("argv", [["stop", "-e"], ["--", "-e"], ["-"]])
    def test_parsing_stops_at_the_first_non_flag(
        self, tree: PrinterTree, recorded: list, argv: List[str]
    ) -> None:
        deps, _, _, _ = make_deps(tree, [])
        assert run_u1(argv, deps) == 0
        assert recorded == []

    def test_an_unknown_flag_exits_one_after_earlier_flags_ran(
        self, tree: PrinterTree, recorded: list, capsys: pytest.CaptureFixture
    ) -> None:
        deps, _, _, _ = make_deps(tree, [])
        assert run_u1(["-e", "-x", "-d"], deps) == 1
        assert [name for name, _, _ in recorded] == ["enable"]
        assert capsys.readouterr().err == "u1: illegal option -- x\n"

    def test_root_uses_the_lava_home_and_others_their_own(
        self, tree: PrinterTree, recorded: list
    ) -> None:
        deps, _, _, _ = make_deps(tree, [])
        deps.home = None
        deps.euid = 0
        run_u1(["-e"], deps)
        deps.euid = 1000
        run_u1(["-e"], deps)
        assert recorded[0] == ("enable", Path("/home/lava"), 0)
        assert recorded[1] == ("enable", Path.home(), 1000)

    def test_a_missing_fluidd_config_fails_with_the_sed_status(self, tree: PrinterTree) -> None:
        deps, _, _, _ = make_deps(tree, [])
        assert run_u1(["-d"], deps) == 2

    def test_the_euid_defaults_to_the_process(
        self, tree: PrinterTree, recorded: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(app.os, "geteuid", lambda: 4242)
        deps, _, _, _ = make_deps(tree, [])
        deps.euid = None
        run_u1(["-e"], deps)
        assert recorded[0][2] == 4242

    def test_a_failing_action_returns_its_exit_code(
        self, tree: PrinterTree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing(*args: Any, **kwargs: Any) -> None:
            raise InstallerExit(9)

        monkeypatch.setattr(app, "u1_enable", failing)
        deps, _, _, _ = make_deps(tree, [])
        assert run_u1(["-e"], deps) == 9

    def test_disable_runs_for_real_against_the_tree(self, tree: PrinterTree) -> None:
        (tree.printer_config_dir / "fluidd.cfg").write_text("[virtual_sdcard]\n")
        deps, stream, runner, _ = make_deps(tree, [])
        assert run_u1(["-d"], deps) == 0
        assert "AFC disabled. Restarting Klipper..." in stream.getvalue()
        assert runner.calls[-1][-1] == "/etc/init.d/S60klipper restart"


class TestMenusEndingWithoutAnExit:
    @pytest.mark.parametrize("runner_name", ["run_install", "run_update"])
    def test_a_menu_loop_that_just_ends_exits_zero(
        self, tree: PrinterTree, monkeypatch: pytest.MonkeyPatch, runner_name: str
    ) -> None:
        started: List[str] = []
        monkeypatch.setattr(app, "run_menus", lambda start, *a: started.append(start))
        deps, _, _, _ = make_deps(tree, [])
        assert getattr(app, runner_name)(["-t"], deps) == 0
        assert started == [{"run_install": "main", "run_update": "update"}[runner_name]]


class TestAppDeps:
    def test_defaults_point_at_the_checkout_and_the_environment(self) -> None:
        deps = AppDeps()
        assert deps.script_dir == SCRIPT_DIR
        assert (SCRIPT_DIR / "installer" / "app.py").is_file()
        assert deps.reexec is None
