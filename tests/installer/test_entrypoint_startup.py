from pathlib import Path
from typing import Callable, List

import pytest

from installer import startup
from installer.platform_core.context import Context
from installer.startup import install_startup, update_startup
from tests.installer.fakes import FakeCommandRunner, RecordingConsole, ScriptedPrompter


class Steps:
    """Replaces every startup step with a recorder so only the order is under test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, console: RecordingConsole) -> None:
        self.calls: List[str] = []
        self.console = console
        for name in (
            "check_root", "check_for_hh", "check_for_zip_install", "check_python_version",
            "clone_and_maybe_restart", "check_existing_install",
        ):
            monkeypatch.setattr(startup, name, self._make(name))

    def _make(self, name: str) -> Callable[..., None]:
        def step(*args: object) -> None:
            self.calls.append(name)
            self.console.messages.append(("STEP", name))
        return step


def make_ctx(tmp_path: Path, git: bool = True, **kwargs: bool) -> Context:
    afc = tmp_path / "afc"
    if git:
        (afc / ".git").mkdir(parents=True)
    else:
        afc.mkdir()
    ctx = Context.create(afc_path=afc, home=tmp_path, test_mode=kwargs.get("test", False))
    ctx.is_snapmaker = kwargs.get("snapmaker", False)
    ctx.git_install = git
    return ctx


def run_install(
    monkeypatch: pytest.MonkeyPatch, ctx: Context
) -> "tuple[Steps, RecordingConsole, List[float]]":
    console = RecordingConsole()
    steps = Steps(monkeypatch, console)
    sleeps: List[float] = []
    install_startup(
        ctx, console, ScriptedPrompter([]), FakeCommandRunner(),  # type: ignore[arg-type]
        lambda: None, sleeps.append,
    )
    return steps, console, sleeps


def run_update(
    monkeypatch: pytest.MonkeyPatch, ctx: Context
) -> "tuple[Steps, RecordingConsole, List[float]]":
    console = RecordingConsole()
    steps = Steps(monkeypatch, console)
    sleeps: List[float] = []
    update_startup(
        ctx, console, FakeCommandRunner(), lambda: None, sleeps.append  # type: ignore[arg-type]
    )
    return steps, console, sleeps


FULL_INSTALL = [
    "check_root", "check_for_hh", "check_for_zip_install", "check_python_version",
    "clone_and_maybe_restart", "check_existing_install",
]


class TestInstallStartup:
    def test_git_install_runs_every_step_in_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        steps, console, sleeps = run_install(monkeypatch, make_ctx(tmp_path))
        assert steps.calls == FULL_INSTALL
        assert console.messages == [
            ("OTHER", "Ensuring we are not running as root (except on K1 OS)..."),
            ("STEP", "check_root"),
            ("OTHER", "Ensuring no conflicting software is present..."),
            ("STEP", "check_for_hh"),
            ("OTHER", "Checking installation method..."),
            ("STEP", "check_for_zip_install"),
            ("STEP", "check_python_version"),
            ("STEP", "clone_and_maybe_restart"),
            ("STEP", "check_existing_install"),
            ("OTHER", "Starting installation process.."),
        ]
        assert sleeps == [2.0]
        assert console.clears == 1

    def test_a_zip_install_skips_only_the_clone_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        steps, _, _ = run_install(monkeypatch, make_ctx(tmp_path, git=False))
        assert steps.calls == [s for s in FULL_INSTALL if s != "clone_and_maybe_restart"]

    def test_snapmaker_skips_the_clone_step_and_announces_the_u1_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        steps, console, _ = run_install(monkeypatch, make_ctx(tmp_path, snapmaker=True))
        assert steps.calls == [s for s in FULL_INSTALL if s != "clone_and_maybe_restart"]
        assert ("OTHER", "Sourcing u1 extended file") in console.messages

    def test_other_platforms_do_not_announce_the_u1_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _, console, _ = run_install(monkeypatch, make_ctx(tmp_path))
        assert ("OTHER", "Sourcing u1 extended file") not in console.messages

    def test_test_mode_skips_the_python_check_and_the_clone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        steps, _, _ = run_install(monkeypatch, make_ctx(tmp_path, test=True))
        assert steps.calls == [
            "check_root", "check_for_hh", "check_for_zip_install", "check_existing_install"
        ]


class TestUpdateStartup:
    def test_git_checkout_runs_every_step_in_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ctx = make_ctx(tmp_path)
        steps, console, sleeps = run_update(monkeypatch, ctx)
        assert steps.calls == [
            "check_root", "check_for_hh", "check_python_version", "clone_and_maybe_restart",
            "check_existing_install",
        ]
        assert console.messages == [
            ("OTHER", "Ensuring we are not running as root..."),
            ("STEP", "check_root"),
            ("OTHER", "Ensuring no conflicting software is present..."),
            ("STEP", "check_for_hh"),
            ("STEP", "check_python_version"),
            ("STEP", "clone_and_maybe_restart"),
            ("STEP", "check_existing_install"),
            ("OTHER", "Starting update process..."),
        ]
        assert ctx.git_install is True
        assert sleeps == [2.0]
        assert console.clears == 1

    def test_a_missing_git_directory_quietly_clears_git_install(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ctx = make_ctx(tmp_path, git=False)
        ctx.git_install = True
        steps, console, _ = run_update(monkeypatch, ctx)
        assert ctx.git_install is False
        assert "clone_and_maybe_restart" in steps.calls
        assert not [m for m in console.messages if m[0] == "WARNING"]

    def test_test_mode_skips_the_python_check_and_the_clone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        steps, _, _ = run_update(monkeypatch, make_ctx(tmp_path, test=True))
        assert steps.calls == ["check_root", "check_for_hh", "check_existing_install"]
