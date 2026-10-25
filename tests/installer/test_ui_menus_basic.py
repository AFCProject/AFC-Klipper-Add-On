import io
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from installer.platform_core.console import Console
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.ui_menus.main_menu import MainMenu
from installer.ui_menus.menus import MENUS
from installer.ui_menus.navigation import MenuDeps, MenuFactory, read_choice, run_menus
from installer.ui_menus.renderer import AnsiRenderer
from installer.ui_menus.state import MenuState
from installer.ui_menus.update_menu import UpdateMenu
from installer.ui_menus.utilities_menu import UtilitiesMenu
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import RecordingConsole, ScriptedPrompter

ENTER = "Press Enter to continue or CTRL+C to cancel..."


class FakeActions:
    """Records calls; the string actions return scripted text."""

    def __init__(self, out: Optional[Console] = None) -> None:
        self.calls: List[str] = []
        self._out = out

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self._out is not None:
            self._out.write(f"MARK_{name}\n")

    def install(self, state: MenuState) -> None:
        self._record("install")

    def install_additional(self, state: MenuState) -> None:
        self._record("install_additional")

    def update(self, state: MenuState) -> None:
        self._record("update")

    def uninstall(self, state: MenuState) -> None:
        self._record("uninstall")

    def delete_var_file(self, state: MenuState) -> str:
        self._record("delete_var_file")
        return "Removed old AFC.var.unit file.\n"

    def rename_unit(self, state: MenuState) -> str:
        self._record("rename_unit")
        return "Renaming unit a to b"

    def exit(self, state: MenuState) -> None:
        self._record("exit")


def make_state(
    tmp_path: Path, prior: bool = False, installed: bool = False, git: bool = True
) -> MenuState:
    ctx = Context.create(
        afc_path=tmp_path / "afc", home=tmp_path, branch="dev", klipper_service="klipper_x",
        git_install=git,
    )
    ctx.prior_installation = prior
    ctx.files_updated_or_installed = installed
    return MenuState(ctx=ctx, selection=UnitSelection())


def fake_deps(answers: Sequence[str]) -> Tuple[MenuDeps, RecordingConsole, ScriptedPrompter]:
    console = RecordingConsole()
    prompter = ScriptedPrompter(answers)
    deps = MenuDeps(
        console=console,  # type: ignore[arg-type]
        prompter=prompter,  # type: ignore[arg-type]
        renderer=AnsiRenderer(console),  # type: ignore[arg-type]
        actions=FakeActions(),
    )
    return deps, console, prompter


def actions_of(deps: MenuDeps) -> FakeActions:
    assert isinstance(deps.actions, FakeActions)
    return deps.actions


class Marker:
    """A stand-in menu that prints its name and stops."""

    def __init__(self, name: str, deps: MenuDeps) -> None:
        self._name = name
        self._deps = deps

    def step(self) -> Optional[str]:
        self._deps.console.write(f"MARK_menu_{self._name}\n")
        return None


def marker_menus() -> Dict[str, MenuFactory]:
    menus = dict(MENUS)
    for name in ("install", "additional"):
        menus[f"unit_type_{name}"] = lambda state, deps, name=name: Marker(  # type: ignore
            name, deps
        )
    return menus


def port_session(
    state: MenuState, start: str, answers: Sequence[str]
) -> Tuple[str, int]:
    """Run the menus against a real Console on a StringIO; return stdout and exit code."""
    stream = io.StringIO()
    console = Console(stream)
    deps = MenuDeps(
        console=console, prompter=ScriptedPrompter(answers),  # type: ignore[arg-type]
        renderer=AnsiRenderer(console), actions=FakeActions(console),
    )
    code = 0
    try:
        run_menus(start, state, deps, marker_menus())
    except InstallerExit as exc:
        code = exc.code
    return stream.getvalue(), code


def ctx_variables(state: MenuState) -> Dict[str, str]:
    ctx = state.ctx
    return {
        "printer_config_dir": str(ctx.printer_config_dir), "klipper_dir": str(ctx.klipper_dir),
        "moonraker_config_file": str(ctx.moonraker_config_file),
        "klipper_service": ctx.klipper_service, "branch": ctx.branch,
        "moonraker": f"{ctx.moonraker_address}:{ctx.moonraker_port}",
        "prior_installation": str(ctx.prior_installation),
        "files_updated_or_installed": str(ctx.files_updated_or_installed),
        "git_install": str(ctx.git_install),
    }


def row_texts(view) -> List[str]:  # type: ignore[no-untyped-def]
    return ["".join(seg.text for seg in r) for r in view.rows]


class TestReadChoice:
    def test_upper_cases_the_answer_and_asks_the_standard_prompt(self) -> None:
        deps, _, prompter = fake_deps(["q"])
        assert read_choice(deps) == "Q"
        assert prompter.prompts == ["Enter your choice: "]

    def test_end_of_input_exits_with_one(self) -> None:
        deps, _, _ = fake_deps([])
        with pytest.raises(InstallerExit) as exc:
            read_choice(deps)
        assert exc.value.code == 1


class TestRunMenus:
    def test_a_new_instance_is_made_per_navigation_and_kept_while_staying(
        self, tmp_path: Path
    ) -> None:
        made: List[str] = []
        scripts: Dict[str, List[List[Optional[str]]]] = {
            "a": [["a", "b"], [None]],
            "b": [["a"]],
        }

        class Stub:
            def __init__(self, script: List[Optional[str]]) -> None:
                self.script = script

            def step(self) -> Optional[str]:
                return self.script.pop(0)

        def factory(name: str) -> MenuFactory:
            def build(state: MenuState, deps: MenuDeps) -> Stub:
                made.append(name)
                return Stub(scripts[name].pop(0))
            return build

        deps, _, _ = fake_deps([])
        run_menus("a", make_state(tmp_path), deps, {"a": factory("a"), "b": factory("b")})
        assert made == ["a", "b", "a"]

    def test_stops_when_a_menu_returns_none(self, tmp_path: Path) -> None:
        deps, _, _ = fake_deps(["Q"])
        run_menus("main", make_state(tmp_path), deps, MENUS)
        assert actions_of(deps).calls == ["exit"]


class TestMainMenu:
    def make(self, tmp_path: Path, answers: Sequence[str], **kwargs: bool):  # type: ignore
        state = make_state(tmp_path, **kwargs)
        deps, console, _ = fake_deps(answers)
        return MainMenu(state, deps), state, deps, console

    def test_the_message_is_kept_so_an_install_result_shows_here(self, tmp_path: Path) -> None:
        state = make_state(tmp_path)
        state.message = "Installed!"
        deps, _, _ = fake_deps([])
        menu = MainMenu(state, deps)
        assert state.message == "Installed!"
        assert menu.view().message == "Installed!"

    def test_the_branch_row_stays_read_only(self, tmp_path: Path) -> None:
        menu, state, _, _ = self.make(tmp_path, ["5"])
        assert menu.step() == "main"
        assert state.message == (
            "To change the branch, please re-run this script with a '-b <branch>' option."
        )

    @pytest.mark.parametrize("key", ["1", "2", "3", "4", "6"])
    def test_the_other_settings_rows_ask_for_a_value(self, tmp_path: Path, key: str) -> None:
        menu, state, _, _ = self.make(tmp_path, [key, ""])
        assert menu.step() == "main"
        assert state.message.endswith("was left unchanged.")

    @pytest.mark.parametrize(
        "key,target",
        [("I", "unit_type_install"), ("u", "update"), ("C", "utilities"),
         ("a", "unit_type_additional")],
    )
    def test_navigation_keys_name_the_next_menu(
        self, tmp_path: Path, key: str, target: str
    ) -> None:
        menu, _, deps, _ = self.make(tmp_path, [key])
        assert menu.step() == target
        assert actions_of(deps).calls == []

    def test_install_after_an_install_in_this_session_explains_instead(
        self, tmp_path: Path
    ) -> None:
        menu, state, deps, _ = self.make(tmp_path, ["I"], installed=True)
        assert menu.step() == "main"
        assert state.message == (
            "AFC was installed in this session. Restart the script to make more changes."
        )

    def test_additional_is_still_available_after_an_install(self, tmp_path: Path) -> None:
        menu, _, _, _ = self.make(tmp_path, ["A"], installed=True)
        assert menu.step() == "unit_type_additional"

    def test_remove_runs_the_uninstall_action_and_stays(self, tmp_path: Path) -> None:
        menu, _, deps, _ = self.make(tmp_path, ["r"])
        assert menu.step() == "main"
        assert actions_of(deps).calls == ["uninstall"]

    def test_quit_runs_the_exit_action_and_stops(self, tmp_path: Path) -> None:
        menu, _, deps, _ = self.make(tmp_path, ["q"])
        assert menu.step() is None
        assert actions_of(deps).calls == ["exit"]

    def test_invalid_choice_is_reported_and_stays(self, tmp_path: Path) -> None:
        menu, state, deps, console = self.make(tmp_path, ["zz"])
        assert menu.step() == "main"
        assert console.written[-1] == "Invalid choice\n"
        assert state.message == ""
        assert actions_of(deps).calls == []

    def test_end_of_input_exits_one_without_the_exit_action(self, tmp_path: Path) -> None:
        menu, _, deps, _ = self.make(tmp_path, [])
        with pytest.raises(InstallerExit) as exc:
            menu.step()
        assert exc.value.code == 1
        assert actions_of(deps).calls == []

    def test_each_step_clears_then_draws(self, tmp_path: Path) -> None:
        menu, _, _, console = self.make(tmp_path, ["5"])
        menu.step()
        assert console.clears == 1
        assert len(console.written) == 1

    @pytest.mark.parametrize(
        "prior,installed,i_shown,u_shown",
        [(False, False, True, False), (True, False, False, True),
         (False, True, False, False), (True, True, False, True)],
    )
    def test_install_and_update_rows(
        self, tmp_path: Path, prior: bool, installed: bool, i_shown: bool, u_shown: bool
    ) -> None:
        menu, _, _, _ = self.make(tmp_path, [], prior=prior, installed=installed)
        texts = row_texts(menu.view())
        assert ("I. Install New System" in texts) is i_shown
        assert ("U. Update AFC Klipper Add-On" in texts) is u_shown
        assert texts[-5:] == [
            "A. Install Additional System", "C. Utilities", "R. Remove AFC Klipper Add-On",
            "Q. Exit", "",
        ]

    @pytest.mark.parametrize(
        "prior,installed,expected",
        [(False, False, ("False", "red")), (True, False, ("True", "green")),
         (False, True, ("True", "green")), (True, True, ("True", "green"))],
    )
    def test_prior_installation_status_color(
        self, tmp_path: Path, prior: bool, installed: bool, expected: Tuple[str, str]
    ) -> None:
        menu, _, _, _ = self.make(tmp_path, [], prior=prior, installed=installed)
        status = menu.view().top[1]
        assert (status[1].text, status[1].color) == expected

    def test_settings_rows_show_the_context(self, tmp_path: Path) -> None:
        menu, state, _, _ = self.make(tmp_path, [])
        ctx = state.ctx
        top = ["".join(seg.text for seg in r) for r in menu.view().top]
        assert top[2:8] == [
            f"\n      1. Printer Config Directory : {ctx.printer_config_dir} ",
            f"      2. Klipper Directory        : {ctx.klipper_dir} ",
            f"      3. Moonraker Config File    : {ctx.moonraker_config_file} ",
            "      4. Klipper Service Name     : klipper_x ",
            "      5. Branch                   : dev ",
            "      6. Moonraker Address        : http://localhost:7125    ",
        ]


class TestUpdateMenu:
    def make(self, tmp_path: Path, answers: Sequence[str], git: bool = True):  # type: ignore
        state = make_state(tmp_path, git=git)
        deps, console, prompter = fake_deps(answers)
        return UpdateMenu(state, deps), state, deps, console, prompter

    def test_git_install_updates_without_warnings(self, tmp_path: Path) -> None:
        menu, _, deps, console, prompter = self.make(tmp_path, ["u"])
        assert menu.step() == "update"
        assert actions_of(deps).calls == ["update"]
        assert console.messages == []
        assert prompter.prompts == ["Enter your choice: "]

    def test_zip_install_warns_waits_for_enter_then_updates(self, tmp_path: Path) -> None:
        menu, _, deps, console, prompter = self.make(tmp_path, ["u", ""], git=False)
        assert menu.step() == "update"
        assert console.messages == [
            ("WARNING", "Git installation not detected."),
            ("WARNING", "Ensure you have manually updated the AFC-Klipper-Add-On directory "
                        "with the latest files from the GitHub repository."),
        ]
        assert prompter.prompts == ["Enter your choice: ", ENTER]
        assert actions_of(deps).calls == ["update"]

    def test_zip_install_end_of_input_at_enter_exits_one_without_updating(
        self, tmp_path: Path
    ) -> None:
        menu, _, deps, _, _ = self.make(tmp_path, ["u"], git=False)
        with pytest.raises(InstallerExit) as exc:
            menu.step()
        assert exc.value.code == 1
        assert actions_of(deps).calls == []

    def test_main_returns_to_the_main_menu(self, tmp_path: Path) -> None:
        menu, _, deps, _, _ = self.make(tmp_path, ["m"])
        assert menu.step() == "main"
        assert actions_of(deps).calls == []

    def test_quit_runs_exit_and_stops(self, tmp_path: Path) -> None:
        menu, _, deps, _, _ = self.make(tmp_path, ["q"])
        assert menu.step() is None
        assert actions_of(deps).calls == ["exit"]

    def test_invalid_selection_sets_the_message_and_stays(self, tmp_path: Path) -> None:
        menu, state, deps, _, _ = self.make(tmp_path, ["z"])
        assert menu.step() == "update"
        assert state.message == "Invalid selection, please try again."
        assert actions_of(deps).calls == []

    def test_view_shows_the_update_message(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = self.make(tmp_path, [])
        state.update_message = "done"
        view = menu.view()
        assert view.message == "done"
        assert row_texts(view) == [
            "", "U. Update AFC Klipper Add-On", "", "M. Return to Main Menu", "Q. Quit", "",
        ]


class TestUtilitiesMenu:
    def make(self, tmp_path: Path, answers: Sequence[str]):  # type: ignore[no-untyped-def]
        state = make_state(tmp_path)
        deps, _, _ = fake_deps(answers)
        return UtilitiesMenu(state, deps), state, deps

    def test_one_removes_the_var_file_and_shows_its_message(self, tmp_path: Path) -> None:
        menu, state, deps = self.make(tmp_path, ["1"])
        assert menu.step() == "utilities"
        assert actions_of(deps).calls == ["delete_var_file"]
        assert state.unit_message == "Removed old AFC.var.unit file.\n"

    def test_r_renames_a_unit_and_shows_its_message(self, tmp_path: Path) -> None:
        menu, state, deps = self.make(tmp_path, ["r"])
        assert menu.step() == "utilities"
        assert actions_of(deps).calls == ["rename_unit"]
        assert state.unit_message == "Renaming unit a to b"

    def test_main_returns_to_the_main_menu(self, tmp_path: Path) -> None:
        menu, _, deps = self.make(tmp_path, ["m"])
        assert menu.step() == "main"
        assert actions_of(deps).calls == []

    def test_quit_runs_exit_and_stops(self, tmp_path: Path) -> None:
        menu, _, deps = self.make(tmp_path, ["Q"])
        assert menu.step() is None
        assert actions_of(deps).calls == ["exit"]

    def test_invalid_selection_sets_the_message_and_stays(self, tmp_path: Path) -> None:
        menu, state, deps = self.make(tmp_path, ["z"])
        assert menu.step() == "utilities"
        assert state.message == "Invalid selection, please try again."
        assert state.unit_message == ""
        assert actions_of(deps).calls == []

    def test_view_shows_the_unit_message(self, tmp_path: Path) -> None:
        menu, state, _ = self.make(tmp_path, [])
        state.unit_message = "note"
        view = menu.view()
        assert (view.art, view.message) == ("utilities", "note")
        assert row_texts(view) == [
            "", "1. Remove AFC.var.unit file", "R. Rename existing unit", "",
            "M. Return to Main Menu", "Q. Quit", "",
        ]
