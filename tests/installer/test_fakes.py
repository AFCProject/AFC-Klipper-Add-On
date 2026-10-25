import subprocess
from typing import Dict, List, Tuple

import pytest

from tests.installer.fakes import (
    FakeCommandRunner,
    PrinterTree,
    RecordingConsole,
    ScriptedPrompter,
)


class TestRecordingConsolePrintMsg:
    def test_records_kind_and_text_in_order(self) -> None:
        console = RecordingConsole()
        console.print_msg("INFO", "first")
        console.print_msg("ERROR", "second")
        assert console.messages == [("INFO", "first"), ("ERROR", "second")]


class TestRecordingConsoleSectionDelimiter:
    def test_records_a_forty_dash_success_line(self) -> None:
        console = RecordingConsole()
        console.section_delimiter()
        assert console.messages == [("SUCCESS", "-" * 40)]


class TestRecordingConsoleClear:
    def test_counts_clear_calls_without_recording_messages(self) -> None:
        console = RecordingConsole()
        console.clear()
        console.clear()
        assert console.clears == 2
        assert console.messages == []


class TestScriptedPrompterAsk:
    def test_returns_answers_in_order_and_records_prompts(self) -> None:
        prompter = ScriptedPrompter(["a", "b"])
        assert prompter.ask("one? ") == "a"
        assert prompter.ask("two? ") == "b"
        assert prompter.prompts == ["one? ", "two? "]

    def test_returns_none_once_answers_are_exhausted(self) -> None:
        prompter = ScriptedPrompter(["only"])
        prompter.ask("x")
        assert prompter.ask("y") is None
        assert prompter.prompts == ["x", "y"]

    def test_empty_script_is_immediate_eof(self) -> None:
        assert ScriptedPrompter([]).ask("p") is None


class TestFakeCommandRunnerRun:
    def test_records_argv_and_returns_default_success(self) -> None:
        runner = FakeCommandRunner()
        result = runner.run(["git", "status"])
        assert runner.calls == [["git", "status"]]
        assert result.returncode == 0
        assert result.stdout == ""

    def test_returns_scripted_result_for_matching_argv(self) -> None:
        scripted: Dict[Tuple[str, ...], subprocess.CompletedProcess] = {
            ("python", "-V"): subprocess.CompletedProcess(
                ["python", "-V"], 0, stdout="3.9\n", stderr=""
            )
        }
        runner = FakeCommandRunner(results=scripted)
        assert runner.run(["python", "-V"]).stdout == "3.9\n"
        assert runner.run(["python", "-c", "x"]).stdout == ""

    def test_check_true_raises_on_nonzero_returncode(self) -> None:
        scripted: Dict[Tuple[str, ...], subprocess.CompletedProcess] = {
            ("false",): subprocess.CompletedProcess(["false"], 1, stdout="", stderr="")
        }
        runner = FakeCommandRunner(results=scripted)
        with pytest.raises(subprocess.CalledProcessError):
            runner.run(["false"], check=True)
        assert runner.calls == [["false"]]

    def test_check_true_does_not_raise_on_success(self) -> None:
        runner = FakeCommandRunner()
        assert runner.run(["true"], check=True).returncode == 0

    def test_accepts_the_capture_argument_like_the_real_runner(self) -> None:
        runner = FakeCommandRunner()
        assert runner.run(["ls"], capture=False).returncode == 0
        assert runner.calls == [["ls"]]

    def test_check_false_returns_nonzero_without_raising(self) -> None:
        scripted: Dict[Tuple[str, ...], subprocess.CompletedProcess] = {
            ("false",): subprocess.CompletedProcess(["false"], 1, stdout="", stderr="")
        }
        runner = FakeCommandRunner(results=scripted)
        assert runner.run(["false"]).returncode == 1


class TestFakeCommandRunnerExists:
    def test_true_only_for_listed_commands(self) -> None:
        runner = FakeCommandRunner(existing=["systemctl"])
        assert runner.exists("systemctl") is True
        assert runner.exists("service") is False


class TestPrinterTreeFixture:
    def test_layout_matches_a_real_printer(self, printer_tree: PrinterTree) -> None:
        assert printer_tree.printer_cfg.read_text().startswith("[printer]")
        assert printer_tree.moonraker_conf.read_text().startswith("[server]")
        assert printer_tree.printer_config_dir == printer_tree.printer_cfg.parent
        assert (printer_tree.klipper_extras / "__init__.py").is_file()
        assert (printer_tree.klipper_dir / ".git" / "info" / "exclude").is_file()

    def test_afc_checkout_has_repo_config_templates_and_extras(
        self, printer_tree: PrinterTree
    ) -> None:
        afc = printer_tree.afc_path
        assert (afc / "config" / "AFC.cfg").is_file()
        assert (afc / "templates" / "AFC_Turtle_1.cfg").is_file()
        extras: List[str] = sorted(p.name for p in (afc / "extras").glob("*.py"))
        assert extras == ["AFC.py", "AFC_lane.py", "AFC_unit.py", "__init__.py"]
        assert 'AFC_VERSION="1.2.8"' in (afc / "extras" / "AFC.py").read_text()

    def test_home_is_redirected_so_tests_never_touch_the_real_one(
        self, printer_tree: PrinterTree
    ) -> None:
        import os

        assert os.environ["HOME"] == str(printer_tree.home)
        # relative_to raises ValueError when the path is outside home (3.8 safe)
        printer_tree.printer_config_dir.relative_to(printer_tree.home)
