import shutil
import subprocess
from pathlib import Path

import pytest

from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.update_flow.version import check_for_uncommitted_changes, get_git_version
from tests.installer.fakes import FakeCommandRunner, RecordingConsole

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required"
)
GIT_ENV = [
    "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false",
]


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *GIT_ENV, "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def make_checkout(root: Path, version_line: str = 'AFC_VERSION="1.2.8"') -> Path:
    path = root / "AFC-Klipper-Add-On"
    (path / "extras").mkdir(parents=True)
    (path / "extras" / "AFC.py").write_text(f"x = 1\n{version_line}\ny = 2\n")
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def make_ctx(afc_path: Path, branch: str = "main") -> Context:
    return Context.create(afc_path=afc_path, home=afc_path.parent, branch=branch)


def messages_text(console: RecordingConsole) -> str:
    return "".join(f"{text}\n" for _, text in console.messages)


class TestGetGitVersion:
    def test_the_first_version_line_wins_and_the_hash_is_asked_of_git(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "afc"
        (path / ".git").mkdir(parents=True)
        (path / "extras").mkdir()
        (path / "extras" / "AFC.py").write_text('AFC_VERSION="3.0.0"\nAFC_VERSION="9"\n')
        runner = FakeCommandRunner(
            {("git", "-C", str(path), "rev-parse", "--short", "HEAD"): subprocess.CompletedProcess(
                [], 0, stdout="deadbee\n", stderr="")}
        )
        assert get_git_version(make_ctx(path), runner) == "3.0.0-deadbee"
        assert runner.calls == [["git", "-C", str(path), "rev-parse", "--short", "HEAD"]]

    def test_the_version_line_may_come_after_other_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "afc"
        (path / "extras").mkdir(parents=True)
        (path / "extras" / "AFC.py").write_text('x = 1\n"""doc"""\nAFC_VERSION="4.5.6"\n')
        assert get_git_version(make_ctx(path), FakeCommandRunner()) == "4.5.6"

    def test_zip_install_never_calls_git(self, tmp_path: Path) -> None:
        path = tmp_path / "afc"
        (path / "extras").mkdir(parents=True)
        (path / "extras" / "AFC.py").write_text('AFC_VERSION="3.0.0"\n')
        runner = FakeCommandRunner()
        assert get_git_version(make_ctx(path), runner) == "3.0.0"
        assert runner.calls == []

    def test_a_git_failure_exits_with_one(self, tmp_path: Path) -> None:
        path = tmp_path / "afc"
        (path / ".git").mkdir(parents=True)
        runner = FakeCommandRunner(
            {("git", "-C", str(path), "rev-parse", "--short", "HEAD"): subprocess.CompletedProcess(
                [], 128, stdout="", stderr="fatal")}
        )
        with pytest.raises(InstallerExit) as exc:
            get_git_version(make_ctx(path), runner)
        assert exc.value.code == 1


def dirty_results(path: Path, unstaged: int, staged: int) -> FakeCommandRunner:
    base = ["git", "-C", str(path), "diff", "--quiet"]
    return FakeCommandRunner({
        tuple(base): subprocess.CompletedProcess(base, unstaged, stdout="", stderr=""),
        tuple(base + ["--cached"]): subprocess.CompletedProcess(base, staged, stdout="", stderr=""),
    })


class TestCheckForUncommittedChanges:
    def test_clean_tree_runs_both_diffs_and_prints_only_the_banner(self, tmp_path: Path) -> None:
        console = RecordingConsole()
        runner = dirty_results(tmp_path, 0, 0)
        check_for_uncommitted_changes(make_ctx(tmp_path), console, runner)
        assert console.messages == [("SUCCESS", "→ Checking for uncommitted changes…")]
        assert runner.calls == [
            ["git", "-C", str(tmp_path), "diff", "--quiet"],
            ["git", "-C", str(tmp_path), "diff", "--quiet", "--cached"],
        ]

    @pytest.mark.parametrize("unstaged,staged", [(1, 0), (0, 1), (1, 1)])
    def test_either_diff_alone_is_enough_to_exit(
        self, tmp_path: Path, unstaged: int, staged: int
    ) -> None:
        console = RecordingConsole()
        runner = dirty_results(tmp_path, unstaged, staged)
        with pytest.raises(InstallerExit) as exc:
            check_for_uncommitted_changes(make_ctx(tmp_path, "dev"), console, runner)
        assert exc.value.code == 1
        assert console.messages == [
            ("SUCCESS", "→ Checking for uncommitted changes…"),
            ("SUCCESS", f"❌ You have uncommitted changes in {tmp_path}."),
            ("SUCCESS", "   Please commit or stash them before running this script."),
            ("SUCCESS", ""),
            ("SUCCESS", "💡 To discard your changes and reset the branch:"),
            ("SUCCESS", f'   cd "{tmp_path}"'),
            ("SUCCESS", "   git checkout dev"),
            ("SUCCESS", "   git reset --hard origin/dev"),
            ("SUCCESS", "   git clean -fd"),
        ]

    def test_unstaged_change_skips_the_cached_diff(self, tmp_path: Path) -> None:
        runner = dirty_results(tmp_path, 1, 0)
        with pytest.raises(InstallerExit):
            check_for_uncommitted_changes(make_ctx(tmp_path), RecordingConsole(), runner)
        assert runner.calls == [["git", "-C", str(tmp_path), "diff", "--quiet"]]
