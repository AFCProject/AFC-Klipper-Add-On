from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from installer import actions
from installer.actions import RealActions
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.ui_menus.state import MenuState
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import FakeCommandRunner, RecordingConsole, ScriptedPrompter


def make(tmp_path: Path, git: bool = True) -> Tuple[RealActions, MenuState, Dict[str, Any]]:
    console = RecordingConsole()
    prompter = ScriptedPrompter([])
    runner = FakeCommandRunner()
    ctx = Context.create(afc_path=tmp_path / "afc", home=tmp_path, git_install=git)
    state = MenuState(ctx=ctx, selection=UnitSelection())
    parts = {"console": console, "prompter": prompter, "runner": runner}
    return RealActions(console, prompter, runner), state, parts  # type: ignore[arg-type]


class TestInstall:
    def test_passes_the_selection_and_toggles_and_stores_the_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: List[Any] = []

        def fake(ctx: Any, console: Any, prompter: Any, runner: Any, options: Any) -> str:
            seen.extend([ctx, console, prompter, runner, options])
            return "installed!"

        monkeypatch.setattr(actions, "install_afc", fake)
        act, state, parts = make(tmp_path)
        state.toggles["tip_forming"] = True
        state.toggles["wipe_macro"] = False
        act.install(state)
        ctx, console, prompter, runner, options = seen
        assert (ctx, console, prompter, runner) == (
            state.ctx, parts["console"], parts["prompter"], parts["runner"]
        )
        assert options.selection is state.selection
        assert options.toggles["tip_forming"] is True
        assert options.toggles["wipe_macro"] is False
        assert options.toggles["afc_includes"] is True
        assert options.toggles is not state.toggles
        assert state.message == "installed!"


class TestInstallAdditional:
    def test_delegates_to_the_unit_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        act, state, parts = make(tmp_path)
        calls: List[Tuple[Any, ...]] = []
        unit = actions.unit_for("HTLF")
        assert unit is not None
        monkeypatch.setattr(
            type(unit), "install_additional",
            lambda self, *a: calls.append(a),
        )
        state.selection.installation_type = "HTLF"
        act.install_additional(state)
        assert calls == [(state.ctx, parts["console"], parts["prompter"], state.selection)]

    def test_an_unknown_type_does_nothing(self, tmp_path: Path) -> None:
        act, state, parts = make(tmp_path)
        state.selection.installation_type = "Mystery"
        act.install_additional(state)
        assert parts["console"].messages == []
        assert parts["runner"].calls == []


class TestUpdate:
    def test_git_install_passes_the_version_and_appends_the_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: List[str] = []
        monkeypatch.setattr(actions, "get_git_version", lambda ctx, runner: "1.2.3-abc")

        def fake(ctx: Any, console: Any, prompter: Any, runner: Any, version: str) -> str:
            seen.append(version)
            return "done\n"

        monkeypatch.setattr(actions, "update_afc", fake)
        act, state, _ = make(tmp_path)
        state.update_message = "earlier\n"
        act.update(state)
        act.update(state)
        assert seen == ["1.2.3-abc", "1.2.3-abc"]
        assert state.update_message == "earlier\ndone\ndone\n"

    def test_a_zip_install_skips_the_version_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(ctx: Any, runner: Any) -> str:
            raise AssertionError("no git version for a ZIP install")

        seen: List[str] = []
        monkeypatch.setattr(actions, "get_git_version", boom)
        monkeypatch.setattr(
            actions, "update_afc", lambda c, o, p, r, version: seen.append(version) or "ok"
        )
        act, state, _ = make(tmp_path, git=False)
        act.update(state)
        assert seen == [""]
        assert state.update_message == "ok"


class TestUninstall:
    def test_stores_the_message(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: List[Any] = []
        monkeypatch.setattr(
            actions, "uninstall_afc", lambda ctx, console, runner: seen.append(ctx) or "gone"
        )
        act, state, _ = make(tmp_path)
        act.uninstall(state)
        assert seen == [state.ctx]
        assert state.message == "gone"


class TestDeleteVarFile:
    def test_returns_the_flow_message_without_touching_the_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            actions, "del_var_file",
            lambda ctx, console, prompter: "Removed old AFC.var.unit file.\n",
        )
        act, state, _ = make(tmp_path)
        assert act.delete_var_file(state) == "Removed old AFC.var.unit file.\n"
        assert state.unit_message == ""


class TestRenameUnit:
    def test_returns_the_flow_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            actions, "rename_unit_prompt", lambda ctx, console, prompter: "Renaming unit a to b"
        )
        act, state, _ = make(tmp_path)
        assert act.rename_unit(state) == "Renaming unit a to b"


class TestExit:
    def test_runs_the_exit_flow(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake(ctx: Any, console: Any, runner: Any) -> None:
            raise InstallerExit(0)

        monkeypatch.setattr(actions, "exit_afc_install", fake)
        act, state, _ = make(tmp_path)
        with pytest.raises(InstallerExit) as exc:
            act.exit(state)
        assert exc.value.code == 0
