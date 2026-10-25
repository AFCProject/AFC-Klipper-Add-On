import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from tests.installer.paths import REPO_ROOT

SCRIPTS = [("install-afc.sh", "install"), ("update-afc.sh", "update")]
NO_PYTHON = (
    "Python 3.8 or newer was not found. Install it or point -y at the Klipper venv bin "
    "directory.\n"
)


def fake_python(path: Path, ok: bool, log: Path) -> Path:
    """An interpreter stand-in: the version probe (-c) passes or fails, other calls are logged."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-c" ]; then exit {0 if ok else 1}; fi\n'
        f'echo "$0|$*|$AFC_PROG|$AFC_SHIM|$PYTHONPATH" >> "{log}"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Host:
    """A fake machine: a HOME, a bin directory first on PATH, and a call log."""

    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.bin = root / "bin"
        self.log = root / "calls.log"
        self.home.mkdir()
        self.bin.mkdir()
        (self.home / "AFC-Klipper-Add-On").symlink_to(REPO_ROOT)

    def python(self, name: str, ok: bool = True) -> Path:
        return fake_python(self.bin / name, ok, self.log)

    def venv_python(self, directory: Path, ok: bool = True) -> Path:
        return fake_python(directory / "python", ok, self.log)

    def run(
        self, script: str, *args: str, shell: Optional[str] = None
    ) -> "subprocess.CompletedProcess[str]":
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
        }
        argv = ["bash", script, *args] if shell is None else ["bash", "-c", shell]
        return subprocess.run(argv, cwd=REPO_ROOT, env=env, capture_output=True, text=True)

    def calls(self) -> List[List[str]]:
        if not self.log.exists():
            return []
        return [line.split("|") for line in self.log.read_text().splitlines()]


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


class TestInterpreterChoice:
    def test_python3_only_host(self, host: Host) -> None:
        host.python("python3")
        result = host.run("install-afc.sh", "-t")
        assert result.returncode == 0, result.stderr
        assert host.calls()[0][0] == str(host.bin / "python3")

    def test_k1_style_host_with_only_a_python_binary(self, host: Host) -> None:
        host.python("python3", ok=False)
        host.python("python")
        host.run("install-afc.sh", "-t")
        assert host.calls()[0][0] == str(host.bin / "python")

    def test_a_k1_layout_with_no_python3_command_at_all(self, host: Host) -> None:
        for tool in ("dirname", "basename"):
            (host.bin / tool).symlink_to(shutil.which(tool) or tool)
        python = host.python("python")
        env = {"HOME": str(host.home), "PATH": str(host.bin)}
        result = subprocess.run(
            [shutil.which("bash") or "bash", "install-afc.sh", "-t"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert host.calls()[0][0] == str(python)

    def test_the_klipper_venv_beats_python3(self, host: Host) -> None:
        host.python("python3")
        venv = host.venv_python(host.home / "klippy-env" / "bin")
        host.run("install-afc.sh", "-t")
        assert host.calls()[0][0] == str(venv)

    def test_the_y_directory_beats_the_klipper_venv(self, host: Host, tmp_path: Path) -> None:
        host.venv_python(host.home / "klippy-env" / "bin")
        chosen = host.venv_python(tmp_path / "custom")
        host.run("install-afc.sh", "-y", str(tmp_path / "custom"), "-t")
        assert host.calls()[0][0] == str(chosen)

    def test_the_y_directory_is_found_in_a_cluster(self, host: Host, tmp_path: Path) -> None:
        chosen = host.venv_python(tmp_path / "custom")
        host.run("install-afc.sh", "-ty", str(tmp_path / "custom"))
        assert host.calls()[0][0] == str(chosen)

    def test_a_too_old_interpreter_is_skipped_for_the_next_one(
        self, host: Host, tmp_path: Path
    ) -> None:
        host.venv_python(tmp_path / "old", ok=False)
        host.venv_python(host.home / "klippy-env" / "bin", ok=False)
        good = host.python("python3")
        host.run("install-afc.sh", "-y", str(tmp_path / "old"))
        assert host.calls()[0][0] == str(good)

    @pytest.mark.parametrize(
        "version,accepted", [((3, 7, 9), False), ((3, 8, 0), True), ((3, 12, 1), True)]
    )
    def test_the_version_bound_is_three_eight(
        self, host: Host, version: Tuple[int, int, int], accepted: bool
    ) -> None:
        probe = host.bin / "python3"
        probe.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"sys.version_info = {version!r}\n"
            'if sys.argv[1] == "-c":\n'
            "    exec(sys.argv[2])\n"
            f'open("{host.log}", "a").write("python3|" + " ".join(sys.argv[1:]) + "|||\\n")\n'
        )
        probe.chmod(0o755)
        host.python("python", ok=False)
        result = host.run("install-afc.sh", "-t")
        assert (result.returncode == 0) is accepted
        assert bool(host.calls()) is accepted

    def test_no_usable_interpreter_prints_the_message_and_exits_one(self, host: Host) -> None:
        host.python("python3", ok=False)
        host.python("python", ok=False)
        result = host.run("install-afc.sh", "-t")
        assert (result.returncode, result.stdout) == (1, NO_PYTHON)
        assert host.calls() == []


class TestHandOff:
    @pytest.mark.parametrize("script,command", SCRIPTS)
    def test_the_command_arguments_and_environment(
        self, host: Host, script: str, command: str
    ) -> None:
        host.python("python3")
        host.run(script, "-t", "-b", "dev")
        [call] = host.calls()
        assert call[1] == f"-m installer {command} -t -b dev"
        assert call[2] == script
        assert call[3] == str(REPO_ROOT / script)
        assert call[4].split(":")[0] == str(REPO_ROOT)

    def test_the_u1_script_hands_off_by_path(self, host: Host) -> None:
        host.python("python3")
        result = host.run("include/U1_extended_firmware_install.sh", "-e")
        assert result.returncode == 0, result.stderr
        [call] = host.calls()
        assert call[1] == "-m installer u1 -e"
        assert call[3] == str(host.home / "AFC-Klipper-Add-On" / "U1_extended_firmware_install.sh")


class TestRealInterpreter:
    """The shims with the real installer behind them (no fakes)."""

    def run(self, script: str, *args: str) -> "subprocess.CompletedProcess[str]":
        env = {**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"}
        return subprocess.run(
            ["bash", script, *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True
        )

    @pytest.mark.parametrize("script,_command", SCRIPTS)
    def test_help_lists_the_options(self, script: str, _command: str) -> None:
        result = self.run(script, "-h")
        assert result.returncode == 0
        assert result.stdout.startswith("Usage: install-afc.sh [options]\n")
        assert "\n  -t " in result.stdout

    @pytest.mark.parametrize("script,_command", SCRIPTS)
    def test_an_unknown_flag_exits_one(self, script: str, _command: str) -> None:
        result = self.run(script, "-x")
        assert result.returncode == 1
        assert f"{script}: illegal option -- x" in result.stderr
