import runpy
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import pytest

from installer import __main__ as entry
from installer.__main__ import COMMANDS, main

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestMain:
    @pytest.mark.parametrize("argv", [[], ["-h"], ["--help"]])
    def test_no_command_or_help_prints_usage_and_returns_zero(
        self, argv: List[str], capsys: pytest.CaptureFixture
    ) -> None:
        assert main(argv) == 0
        out = capsys.readouterr().out
        assert out.startswith("usage: python -m installer {install,update,u1}")
        assert "AFC installer" in out

    def test_unknown_command_returns_two_with_usage_on_stderr(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        assert main(["bogus"]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("installer: unknown command 'bogus'\nusage: python -m")

    @pytest.mark.parametrize("command", ["install", "update", "u1"])
    def test_each_command_gets_the_remaining_arguments(
        self, command: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: Dict[str, List[str]] = {}

        def fake(argv: List[str]) -> int:
            seen[command] = argv
            return 7

        monkeypatch.setitem(COMMANDS, command, fake)
        assert main([command, "-t", "-b", "dev"]) == 7
        assert seen == {command: ["-t", "-b", "dev"]}

    def test_the_c_locale_is_exported_for_child_processes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        monkeypatch.setitem(COMMANDS, "install", lambda argv: 0)
        main(["install"])
        assert entry.os.environ["LC_ALL"] == "C"

    def test_help_and_unknown_do_not_touch_the_locale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        main(["--help"])
        main(["bogus"])
        assert entry.os.environ["LC_ALL"] == "en_US.UTF-8"

    def test_argv_defaults_to_the_process_arguments(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["installer", "--help"])
        assert main() == 0
        assert "AFC installer" in capsys.readouterr().out


class TestModuleEntry:
    def test_running_as_main_exits_with_the_return_code(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["installer"])
        # the module is already imported above; runpy warns if it is still cached
        monkeypatch.delitem(sys.modules, "installer.__main__")
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("installer", run_name="__main__")
        assert exc.value.code == 0
        assert "AFC installer" in capsys.readouterr().out

    def test_python_dash_m_help_exits_zero(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "installer", "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "AFC installer" in result.stdout

    def test_python_dash_m_install_help_prints_the_option_list(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "installer", "install", "-h"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert result.stdout.startswith("Usage: install-afc.sh [options]\n")
        assert "\n  -t " in result.stdout
