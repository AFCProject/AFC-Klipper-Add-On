import io
import subprocess
import sys
from pathlib import Path

import pytest

from installer.platform_core.console import (
    Console,
    Prompter,
)
from installer.platform_core.errors import InstallerExit
from installer.platform_core.system import CommandRunner

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestInstallerExit:
    def test_carries_the_exit_code(self) -> None:
        assert InstallerExit(3).code == 3

    def test_is_an_exception_with_a_readable_message(self) -> None:
        with pytest.raises(InstallerExit) as exc:
            raise InstallerExit(1)
        assert str(exc.value) == "installer exited with code 1"

    def test_code_zero_is_allowed_for_a_clean_exit(self) -> None:
        assert InstallerExit(0).code == 0


class TestConsolePrintMsg:
    @pytest.mark.parametrize(
        "kind, expected",
        [
            ("INFO", "\033[0;37mhello\033[0m\n"),
            ("WARNING", "\033[1;33mhello\033[0m\n"),
            ("ERROR", "\033[1;31mhello\033[0m\n"),
            ("PROMPT", "\033[1;37mhello\033[0m\n"),
            ("SUCCESS", "hello\n"),  # SUCCESS prints uncolored
            ("OTHER", "hello\n"),
        ],
    )
    def test_each_kind_uses_its_color_and_resets(self, kind: str, expected: str) -> None:
        out = io.StringIO()
        Console(out).print_msg(kind, "hello")
        assert out.getvalue() == expected


    def test_multiline_text_is_kept_as_is(self) -> None:
        out = io.StringIO()
        Console(out).print_msg("INFO", "one\ntwo")
        assert out.getvalue() == "\033[0;37mone\ntwo\033[0m\n"

    def test_defaults_to_stdout_resolved_at_call_time(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        Console().print_msg("OTHER", "late bound")
        assert capsys.readouterr().out == "late bound\n"


class TestConsoleSectionDelimiter:
    def test_prints_forty_plain_dashes(self) -> None:
        out = io.StringIO()
        Console(out).section_delimiter()
        assert out.getvalue() == "-" * 40 + "\n"


class TestConsoleClear:
    def test_writes_the_clear_sequence_on_a_terminal(self) -> None:
        out = FakeTty()
        Console(out).clear()
        assert out.getvalue() == "\033[H\033[2J\033[3J"

    def test_writes_nothing_when_output_is_not_a_terminal(self) -> None:
        out = io.StringIO()
        Console(out).clear()
        assert out.getvalue() == ""


class FakeStdin(io.StringIO):
    def __init__(self, text: str, tty: bool) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestPrompterAsk:
    def test_a_terminal_gets_the_prompt_and_the_typed_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = []
        monkeypatch.setattr(sys, "stdin", FakeStdin("", True))
        monkeypatch.setattr("builtins.input", lambda prompt="": seen.append(prompt) or "yes")
        assert Prompter().ask("Continue? ") == "yes"
        assert seen == ["Continue? "]

    def test_a_terminal_empty_line_is_an_empty_string_not_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "stdin", FakeStdin("", True))
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        assert Prompter().ask("? ") == ""

    def test_a_terminal_end_of_input_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def eof(prompt: str = "") -> str:
            raise EOFError

        monkeypatch.setattr(sys, "stdin", FakeStdin("", True))
        monkeypatch.setattr("builtins.input", eof)
        assert Prompter().ask("? ") is None

    def test_piped_input_reads_lines_without_showing_the_prompt(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(sys, "stdin", FakeStdin("first\n\nlast\n", False))
        prompter = Prompter()
        assert [prompter.ask("A: "), prompter.ask("B: "), prompter.ask("C: ")] == [
            "first", "", "last"]
        assert capsys.readouterr().out == ""

    def test_piped_end_of_input_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", FakeStdin("", False))
        assert Prompter().ask("? ") is None

    def test_piped_last_line_without_a_newline_counts_as_end_of_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "stdin", FakeStdin("tail", False))
        assert Prompter().ask("? ") is None


class TestCommandRunnerRun:
    def test_captures_stdout_and_returns_zero(self) -> None:
        result = CommandRunner().run([sys.executable, "-c", "print('hi')"])
        assert (result.returncode, result.stdout) == (0, "hi\n")

    def test_nonzero_exit_is_returned_not_raised_by_default(self) -> None:
        result = CommandRunner().run([sys.executable, "-c", "import sys; sys.exit(4)"])
        assert result.returncode == 4

    def test_check_true_raises_on_nonzero(self) -> None:
        with pytest.raises(subprocess.CalledProcessError) as exc:
            CommandRunner().run([sys.executable, "-c", "import sys; sys.exit(2)"], check=True)
        assert exc.value.returncode == 2

    def test_check_true_does_not_raise_on_success(self) -> None:
        assert CommandRunner().run([sys.executable, "-c", "pass"], check=True).returncode == 0

    def test_missing_executable_behaves_like_shell_127(self) -> None:
        result = CommandRunner().run(["definitely-not-a-real-command-xyz"])
        assert result.returncode == 127
        assert result.stderr == "definitely-not-a-real-command-xyz: command not found"

    def test_missing_executable_with_check_raises(self) -> None:
        with pytest.raises(subprocess.CalledProcessError) as exc:
            CommandRunner().run(["definitely-not-a-real-command-xyz"], check=True)
        assert exc.value.returncode == 127

    def test_capture_false_does_not_capture_output(self) -> None:
        result = CommandRunner().run([sys.executable, "-c", "pass"], capture=False)
        assert result.stdout is None

    def test_arguments_are_passed_as_an_argv_list_never_through_a_shell(self) -> None:
        result = CommandRunner().run(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", "a b; echo injected"]
        )
        assert result.stdout == "a b; echo injected\n"


class TestCommandRunnerExists:
    def test_true_for_an_executable_on_path(self) -> None:
        assert CommandRunner().exists("sh") is True

    def test_false_for_a_missing_command(self) -> None:
        assert CommandRunner().exists("definitely-not-a-real-command-xyz") is False
