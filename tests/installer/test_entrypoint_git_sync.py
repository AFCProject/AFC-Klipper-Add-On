import io
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from installer import git_sync
from installer.git_sync import clone_and_maybe_restart, make_reexec, switch_branch_early
from installer.platform_core.console import Console
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.platform_core.system import CommandRunner
from tests.installer.fakes import FakeCommandRunner, RecordingConsole

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required"
)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    }.items():
        monkeypatch.setenv(key, value)


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "-C", str(path), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def make_remote(root: Path) -> Path:
    """A bare remote with main and dev branches, each with one commit."""
    seed = root / "seed"
    seed.mkdir()
    git(seed, "init", "-q")
    (seed / "file.txt").write_text("one\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "one")
    git(seed, "branch", "-M", "main")
    git(seed, "branch", "dev")
    remote = root / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(remote)], check=True)
    return remote


def clone(remote: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(remote), str(dest)], check=True)
    return dest


def push_new_commit(remote: Path, work: Path) -> None:
    other = clone(remote, work)
    (other / "file.txt").write_text("two\n")
    git(other, "commit", "-q", "-am", "two")
    git(other, "push", "-q", "origin", "main")


class Marker:
    """Stands in for exec: records the call and returns, like a mocked os.execv."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.count = 0

    def __call__(self) -> None:
        self.count += 1
        self.console.write("MARK_exec\n")


def port_run(
    ctx: Context, action: str, branch: str = ""
) -> Tuple[str, int, int]:
    stream = io.StringIO()
    console = Console(stream)
    marker = Marker(console)
    code = 0
    try:
        if action == "early":
            switch_branch_early(ctx.afc_path, branch, console, CommandRunner(), marker)
        else:
            clone_and_maybe_restart(ctx, console, CommandRunner(), marker)
    except InstallerExit as exc:
        code = exc.code
    if code == 0:
        stream.write(f"git_install={ctx.git_install}\n")
    return stream.getvalue(), code, marker.count


def make_ctx(afc: Path, remote: Path, branch: str = "main") -> Context:
    return Context.create(
        afc_path=afc, home=afc.parent, branch=branch, gitrepo=str(remote)
    )


def prepare(tmp_path: Path, scenario: str) -> Tuple[Path, Path]:
    """Build a checkout for a scenario against a throwaway remote; returns remote and checkout."""
    remote = make_remote(tmp_path)
    afc = tmp_path / "afc"
    if scenario in ("clone", "behind", "dirty", "on_dev"):
        clone(remote, afc)
    elif scenario == "populated":
        afc.mkdir()
        (afc / "x.txt").write_text("zip")
    elif scenario == "empty":
        afc.mkdir()
    if scenario == "behind":
        push_new_commit(remote, tmp_path / "pusher")
    if scenario == "dirty":
        (afc / "file.txt").write_text("edited\n")
    if scenario == "on_dev":
        git(afc, "checkout", "-q", "-b", "dev", "origin/dev")
    return remote, afc


@needs_git
class TestCloneAndMaybeRestartOnRealRepos:
    @pytest.mark.parametrize(
        "scenario,needle,execs,code",
        [
            ("clone", "✓ Already up to date.", 0, 0),
            ("behind", "✓ Update applied. Restarting script…", 1, 0),
            ("dirty", "You have uncommitted changes", 0, 1),
            ("populated", "appears to have been installed via ZIP file", 0, 0),
            ("empty", "✓ Clone complete.", 0, 0),
            ("missing", "✓ Clone complete.", 0, 0),
            ("on_dev", "→ Switching to branch 'main'…", 0, 0),
        ],
    )
    def test_scenario(
        self, tmp_path: Path, scenario: str, needle: str, execs: int, code: int
    ) -> None:
        remote, afc = prepare(tmp_path, scenario)
        out, got_code, got_execs = port_run(make_ctx(afc, remote), "clone")
        assert got_code == code
        assert needle in out
        assert got_execs == execs
        zip_install = scenario == "populated"
        assert out.endswith(f"git_install={not zip_install}\n") is (code == 0)

    def test_a_pull_really_brings_in_the_new_commit(self, tmp_path: Path) -> None:
        remote, afc = prepare(tmp_path, "behind")
        port_run(make_ctx(afc, remote), "clone")
        assert (afc / "file.txt").read_text() == "two\n"

    def test_an_up_to_date_checkout_stays_on_the_branch_and_commit(self, tmp_path: Path) -> None:
        remote, afc = prepare(tmp_path, "clone")
        before = git(afc, "rev-parse", "HEAD")
        port_run(make_ctx(afc, remote), "clone")
        assert git(afc, "rev-parse", "HEAD") == before

    def test_a_clone_checks_out_the_requested_branch(self, tmp_path: Path) -> None:
        remote, afc = prepare(tmp_path, "missing")
        port_run(make_ctx(afc, remote), "clone")
        assert git(afc, "rev-parse", "--abbrev-ref", "HEAD") == "main"


@needs_git
class TestSwitchBranchEarlyOnRealRepos:
    @pytest.mark.parametrize(
        "scenario,branch,needle,execs,code,head",
        [
            ("clone", "dev", "✓ Branch switched. Restarting script…", 1, 0, "dev"),
            ("clone", "main", "", 0, 0, "main"),
            ("clone", "nope", "Branch 'nope' does not exist locally or on the remote.", 0, 1,
             "main"),
            ("clone", "", "", 0, 0, "main"),
            ("populated", "dev", "", 0, 0, None),
            ("on_dev", "dev", "", 0, 0, "dev"),
            ("on_dev", "main", "✓ Branch switched. Restarting script…", 1, 0, "main"),
        ],
    )
    def test_scenario(
        self, tmp_path: Path, scenario: str, branch: str, needle: str, execs: int, code: int,
        head: Optional[str],
    ) -> None:
        remote, afc = prepare(tmp_path, scenario)
        out, got_code, got_execs = port_run(make_ctx(afc, remote), "early", branch)
        assert (got_code, got_execs) == (code, execs)
        assert needle in out
        if head is not None:
            assert git(afc, "rev-parse", "--abbrev-ref", "HEAD") == head


def completed(code: int, out: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess([], code, stdout=out, stderr="")


def scripted(afc: Path, **results: "subprocess.CompletedProcess[str]") -> FakeCommandRunner:
    """A runner keyed by short names: head, fetch, origin, local, checkout, pull, status, ..."""
    base = ["git", "-C", str(afc)]
    table = {
        "head": base + ["rev-parse", "--abbrev-ref", "HEAD"],
        "fetch": base + ["fetch", "--prune", "--quiet"],
        "origin": base + ["rev-parse", "--verify", "--quiet", "origin/dev"],
        "local": base + ["rev-parse", "--verify", "--quiet", "dev"],
        "checkout": base + ["checkout", "--quiet", "dev"],
        "pull": base + ["pull", "--rebase", "--quiet"],
        "main_checkout": base + ["checkout", "--quiet", "main"],
        "status": base + ["status", "--porcelain", "--branch"],
        "diff": base + ["diff", "--quiet"],
    }
    return FakeCommandRunner({tuple(table[k]): v for k, v in results.items()})


class TestSwitchBranchEarly:
    def make(self, tmp_path: Path) -> Path:
        (tmp_path / ".git").mkdir()
        return tmp_path

    def call(
        self, afc: Path, runner: FakeCommandRunner, branch: str = "dev"
    ) -> Tuple[RecordingConsole, Marker]:
        console = RecordingConsole()
        marker = Marker(console)  # type: ignore[arg-type]
        switch_branch_early(afc, branch, console, runner, marker)  # type: ignore[arg-type]
        return console, marker

    def test_no_branch_or_no_git_directory_does_nothing(self, tmp_path: Path) -> None:
        runner = scripted(tmp_path)
        self.call(self.make(tmp_path), runner, branch="")
        other = tmp_path / "plain"
        other.mkdir()
        self.call(other, runner)
        assert runner.calls == []

    def test_already_on_the_branch_does_nothing_more(self, tmp_path: Path) -> None:
        afc = self.make(tmp_path)
        runner = scripted(afc, head=completed(0, "dev\n"))
        console, marker = self.call(afc, runner)
        assert len(runner.calls) == 1
        assert (console.messages, marker.count) == ([], 0)

    def test_a_failing_rev_parse_exits_with_its_status(self, tmp_path: Path) -> None:
        afc = self.make(tmp_path)
        runner = scripted(afc, head=completed(128))
        with pytest.raises(InstallerExit) as exc:
            self.call(afc, runner)
        assert exc.value.code == 128

    def test_a_branch_known_only_locally_or_only_remotely_is_accepted(
        self, tmp_path: Path
    ) -> None:
        afc = self.make(tmp_path)
        for known in ("origin", "local"):
            other = "local" if known == "origin" else "origin"
            runner = scripted(afc, head=completed(0, "main\n"), **{other: completed(1)})
            console, marker = self.call(afc, runner)
            assert marker.count == 1
            assert console.messages == [
                ("OTHER", "→ Switching to branch 'dev' before loading…"),
                ("OTHER", "✓ Branch switched. Restarting script…"),
            ]

    def test_neither_known_exits_one_with_the_message(self, tmp_path: Path) -> None:
        afc = self.make(tmp_path)
        runner = scripted(
            afc, head=completed(0, "main\n"), origin=completed(1), local=completed(1)
        )
        console = RecordingConsole()
        with pytest.raises(InstallerExit) as exc:
            switch_branch_early(afc, "dev", console, runner, lambda: None)  # type: ignore
        assert exc.value.code == 1
        assert console.messages[-1] == (
            "OTHER", "✗ Branch 'dev' does not exist locally or on the remote."
        )
        assert ["git", "-C", str(afc), "checkout", "--quiet", "dev"] not in runner.calls

    def test_fetch_and_checkout_failures_stop_the_script(self, tmp_path: Path) -> None:
        afc = self.make(tmp_path)
        for failing in ("fetch", "checkout"):
            runner = scripted(afc, head=completed(0, "main\n"), **{failing: completed(3)})
            with pytest.raises(InstallerExit) as exc:
                self.call(afc, runner)
            assert exc.value.code == 3

    def test_a_failed_pull_is_ignored(self, tmp_path: Path) -> None:
        afc = self.make(tmp_path)
        runner = scripted(afc, head=completed(0, "main\n"), pull=completed(1))
        _, marker = self.call(afc, runner)
        assert marker.count == 1

    def test_the_local_branch_is_only_looked_up_when_the_remote_one_is_missing(
        self, tmp_path: Path
    ) -> None:
        afc = self.make(tmp_path)
        runner = scripted(afc, head=completed(0, "main\n"), origin=completed(1))
        self.call(afc, runner)
        base = ["git", "-C", str(afc)]
        assert base + ["rev-parse", "--verify", "--quiet", "dev"] in runner.calls

    def test_the_order_of_git_calls(self, tmp_path: Path) -> None:
        afc = self.make(tmp_path)
        runner = scripted(afc, head=completed(0, "main\n"))
        self.call(afc, runner)
        base = ["git", "-C", str(afc)]
        assert runner.calls == [
            base + ["rev-parse", "--abbrev-ref", "HEAD"],
            base + ["fetch", "--prune", "--quiet"],
            base + ["rev-parse", "--verify", "--quiet", "origin/dev"],
            base + ["checkout", "--quiet", "dev"],
            base + ["pull", "--rebase", "--quiet"],
        ]


class TestCloneAndMaybeRestart:
    def ctx(self, afc: Path) -> Context:
        return Context.create(afc_path=afc, home=afc.parent, branch="main", gitrepo="REMOTE")

    def test_status_text_with_behind_anywhere_triggers_a_pull(self, tmp_path: Path) -> None:
        afc = tmp_path / "afc"
        (afc / ".git").mkdir(parents=True)
        runner = scripted(afc, status=completed(0, "## main\n M behind.txt\n"))
        console = RecordingConsole()
        marker = Marker(console)  # type: ignore[arg-type]
        clone_and_maybe_restart(self.ctx(afc), console, runner, marker)  # type: ignore
        assert marker.count == 1
        assert ["git", "-C", str(afc), "pull", "--rebase", "--quiet"] in runner.calls

    def test_up_to_date_when_status_has_no_behind(self, tmp_path: Path) -> None:
        afc = tmp_path / "afc"
        (afc / ".git").mkdir(parents=True)
        runner = scripted(afc, status=completed(0, "## main...origin/main\n"))
        console = RecordingConsole()
        marker = Marker(console)  # type: ignore[arg-type]
        clone_and_maybe_restart(self.ctx(afc), console, runner, marker)  # type: ignore
        assert marker.count == 0
        assert console.messages[-1] == ("OTHER", "✓ Already up to date.")
        assert not [c for c in runner.calls if "pull" in c]

    def test_clone_command_line(self, tmp_path: Path) -> None:
        afc = tmp_path / "afc"
        runner = FakeCommandRunner()
        console = RecordingConsole()
        clone_and_maybe_restart(self.ctx(afc), console, runner, lambda: None)  # type: ignore
        assert runner.calls == [
            ["git", "clone", "--branch", "main", "--single-branch", "--depth", "1",
             "REMOTE", str(afc)]
        ]
        assert console.messages == [
            ("OTHER", f"→ Cloning main from REMOTE into {afc}…"),
            ("OTHER", "✓ Clone complete."),
        ]

    def test_a_failed_clone_stops(self, tmp_path: Path) -> None:
        afc = tmp_path / "afc"
        runner = FakeCommandRunner({
            ("git", "clone", "--branch", "main", "--single-branch", "--depth", "1",
             "REMOTE", str(afc)): completed(128)
        })
        with pytest.raises(InstallerExit) as exc:
            clone_and_maybe_restart(
                self.ctx(afc), RecordingConsole(), runner, lambda: None  # type: ignore
            )
        assert exc.value.code == 128

    def test_zip_install_warnings_and_flag(self, tmp_path: Path) -> None:
        afc = tmp_path / "afc"
        afc.mkdir()
        (afc / "f").write_text("x")
        ctx = self.ctx(afc)
        console = RecordingConsole()
        runner = FakeCommandRunner()
        clone_and_maybe_restart(ctx, console, runner, lambda: None)  # type: ignore[arg-type]
        assert ctx.git_install is False
        assert console.messages == [
            ("WARNING", "AFC-Klipper-Add-On appears to have been installed via ZIP file."),
            ("WARNING", f"Updates are applied by extracting a fresh archive over {afc}."),
        ]
        assert runner.calls == []

    def test_checkout_and_fetch_failures_stop(self, tmp_path: Path) -> None:
        afc = tmp_path / "afc"
        (afc / ".git").mkdir(parents=True)
        for failing in ("main_checkout", "fetch"):
            runner = scripted(afc, **{failing: completed(5)})
            with pytest.raises(InstallerExit) as exc:
                clone_and_maybe_restart(
                    self.ctx(afc), RecordingConsole(), runner, lambda: None  # type: ignore
                )
            assert exc.value.code == 5

    def test_a_dirty_tree_stops_before_fetching(self, tmp_path: Path) -> None:
        afc = tmp_path / "afc"
        (afc / ".git").mkdir(parents=True)
        runner = scripted(afc, diff=completed(1))
        with pytest.raises(InstallerExit) as exc:
            clone_and_maybe_restart(
                self.ctx(afc), RecordingConsole(), runner, lambda: None  # type: ignore
            )
        assert exc.value.code == 1
        assert ["git", "-C", str(afc), "fetch", "--prune", "--quiet"] not in runner.calls


class TestMakeReexec:
    def test_replaces_the_process_with_the_script_and_its_arguments(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: List[Tuple[str, List[str]]] = []
        monkeypatch.setattr(git_sync.os, "execv", lambda path, argv: calls.append((path, argv)))
        make_reexec(tmp_path / "install-afc.sh", ["-t", "-b", "dev"])()
        script = str(tmp_path / "install-afc.sh")
        assert calls == [(script, [script, "-t", "-b", "dev"])]
