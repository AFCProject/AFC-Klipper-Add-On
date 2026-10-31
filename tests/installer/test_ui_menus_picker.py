from pathlib import Path
from typing import List, Sequence, Tuple

import pytest

from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.ui_menus.menus import MENUS
from installer.ui_menus.navigation import MenuDeps
from installer.ui_menus.state import MenuState
from installer.ui_menus.unit_type_menu import ADDITIONAL, INSTALL, UnitTypeMenu
from installer.ui_menus.view import PICKER_ADDITIONAL_BOX, PICKER_INSTALL_BOX
from installer.unit_registry.registry import UNIT_TYPES
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import RecordingConsole, ScriptedPrompter
from tests.installer.test_ui_menus_basic import FakeActions, fake_deps, row_texts

NAMES = [unit.display_name for unit in UNIT_TYPES]


def make_state(tmp_path: Path, first_unit: bool = True, **sel: str) -> MenuState:
    ctx = Context.create(afc_path=tmp_path / "afc", home=tmp_path)
    if first_unit:
        ctx.afc_config_dir.mkdir(parents=True, exist_ok=True)
        (ctx.afc_config_dir / "AFC_Turtle_1.cfg").write_text("[AFC_stepper lane1]\n")
    return MenuState(ctx=ctx, selection=UnitSelection(**sel))


def build(
    tmp_path: Path, mode: str, answers: Sequence[str], first_unit: bool = True, **sel: str
) -> Tuple[UnitTypeMenu, MenuState, MenuDeps, RecordingConsole, ScriptedPrompter]:
    state = make_state(tmp_path, first_unit, **sel)
    deps, console, prompter = fake_deps(answers)
    return UnitTypeMenu(state, deps, mode), state, deps, console, prompter


def calls(deps: MenuDeps) -> List[str]:
    assert isinstance(deps.actions, FakeActions)
    return deps.actions.calls


class TestView:
    def test_lists_every_registered_type_in_order_with_numbers(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, INSTALL, [])
        assert row_texts(menu.view()) == [
            *[f"{n}. {name}" for n, name in enumerate(NAMES, 1)],
            "", "M. Return to Main Menu", "Q. Quit", "",
        ]

    def test_each_mode_has_its_own_heading(self, tmp_path: Path) -> None:
        install, _, _, _, _ = build(tmp_path, INSTALL, [])
        additional, _, _, _, _ = build(tmp_path, ADDITIONAL, [])
        assert install.view().options_box is PICKER_INSTALL_BOX
        assert additional.view().options_box is PICKER_ADDITIONAL_BOX
        assert PICKER_INSTALL_BOX != PICKER_ADDITIONAL_BOX

    def test_shows_the_current_message(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, INSTALL, [])
        state.message = "hello"
        assert menu.view().message == "hello"


class TestStepInstall:
    @pytest.mark.parametrize("number", range(1, len(NAMES) + 1))
    def test_choosing_a_type_opens_the_install_options(
        self, tmp_path: Path, number: int
    ) -> None:
        menu, state, _, console, _ = build(tmp_path, INSTALL, [str(number)])
        assert menu.step() == "install_options"
        assert state.selection.installation_type == NAMES[number - 1]
        assert console.clears == 1

    def test_the_unit_type_fixes_up_the_selection(self, tmp_path: Path) -> None:
        index = NAMES.index("ViViD") + 1
        menu, state, _, _, _ = build(tmp_path, INSTALL, [str(index)], buffer_type="FPS_PSF")
        menu.step()
        assert state.selection.buffer_type == "None"

    @pytest.mark.parametrize("number", range(1, len(NAMES) + 1))
    def test_the_unit_name_becomes_the_types_default(self, tmp_path: Path, number: int) -> None:
        menu, state, _, _, _ = build(tmp_path, INSTALL, [str(number)], unit_name="Custom_1")
        menu.step()
        assert state.selection.unit_name == UNIT_TYPES[number - 1].default_name

    def test_the_additional_picker_leaves_the_name_to_its_options_screen(
        self, tmp_path: Path
    ) -> None:
        menu, state, _, _, _ = build(tmp_path, ADDITIONAL, ["4"], unit_name="Custom_1")
        menu.step()
        assert state.selection.unit_name == "Custom_1"

    def test_the_message_starts_empty(self, tmp_path: Path) -> None:
        state = make_state(tmp_path)
        state.message = "left over"
        UnitTypeMenu(state, fake_deps([])[0], INSTALL)
        assert state.message == ""

    def test_install_mode_does_not_need_a_first_unit(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, INSTALL, ["1"], first_unit=False)
        assert menu.step() == "install_options"


class TestStepAdditional:
    def test_choosing_a_type_opens_the_additional_options(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ADDITIONAL, ["4"])
        assert menu.step() == "additional_options"
        assert state.selection.installation_type == NAMES[3]

    def test_a_buffer_the_type_supports_is_kept(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ADDITIONAL, ["1"], buffer_type="TurtleNeckV2")
        menu.step()
        assert state.selection.additional_buffer_type == "TurtleNeckV2"

    @pytest.mark.parametrize("buffer", ["None", "Unknown", "Bogus"])
    def test_otherwise_the_types_default_buffer_is_used(
        self, tmp_path: Path, buffer: str
    ) -> None:
        menu, state, _, _, _ = build(tmp_path, ADDITIONAL, ["1"], buffer_type=buffer)
        menu.step()
        assert state.selection.additional_buffer_type == "TurtleNeck"

    def test_ace_forces_its_own_default_even_when_the_buffer_is_shared(
        self, tmp_path: Path
    ) -> None:
        # TurtleNeck is a valid ACE additional_buffer_option too, so without the
        # force-default flag this would be carried over instead of reset.
        menu, state, _, _, _ = build(
            tmp_path, ADDITIONAL, [str(NAMES.index("ACE") + 1)], buffer_type="TurtleNeck"
        )
        menu.step()
        assert state.selection.additional_buffer_type == "Internal"

    def test_openams_defaults_to_the_psf_buffer(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(
            tmp_path, ADDITIONAL, [str(NAMES.index("OpenAMS") + 1)], buffer_type="TurtleNeck"
        )
        menu.step()
        assert state.selection.additional_buffer_type == "FPS_PSF"

    def test_types_without_a_buffer_choice_have_none(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(
            tmp_path, ADDITIONAL, [str(NAMES.index("ViViD") + 1)], buffer_type="TurtleNeck"
        )
        state.selection.additional_buffer_type = "TurtleNeck"
        menu.step()
        assert state.selection.additional_buffer_type == "None"

    def test_the_selection_is_not_run_through_the_install_rules(self, tmp_path: Path) -> None:
        index = NAMES.index("ViViD") + 1
        menu, state, _, _, _ = build(tmp_path, ADDITIONAL, [str(index)], buffer_type="FPS_PSF")
        menu.step()
        assert state.selection.buffer_type == "FPS_PSF"

    def test_without_a_first_unit_it_explains_and_returns_to_main(self, tmp_path: Path) -> None:
        menu, state, _, console, prompter = build(tmp_path, ADDITIONAL, [""], first_unit=False)
        assert menu.step() == "main"
        assert console.written == [
            f"\nNo existing unit installation found in {state.ctx.afc_config_dir}.\n"
            "Please install your first unit from the main Install menu before adding "
            "additional units.\n\n"
        ]
        assert prompter.prompts == ["Press Enter to return to the main menu..."]
        assert console.clears == 0

    def test_end_of_input_at_the_refusal_exits_one(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, ADDITIONAL, [], first_unit=False)
        with pytest.raises(InstallerExit) as exc:
            menu.step()
        assert exc.value.code == 1


class TestStepNavigation:
    @pytest.mark.parametrize("mode", [INSTALL, ADDITIONAL])
    def test_main_goes_back(self, tmp_path: Path, mode: str) -> None:
        menu, _, deps, _, _ = build(tmp_path, mode, ["m"])
        assert menu.step() == "main"
        assert calls(deps) == []

    @pytest.mark.parametrize("mode", [INSTALL, ADDITIONAL])
    def test_quit_runs_the_exit_action_and_stops(self, tmp_path: Path, mode: str) -> None:
        menu, _, deps, _, _ = build(tmp_path, mode, ["Q"])
        assert menu.step() is None
        assert calls(deps) == ["exit"]

    @pytest.mark.parametrize(
        "answer", ["0", str(len(UNIT_TYPES) + 1), "-1", "1.5", "x", "", "²", "١"]
    )
    @pytest.mark.parametrize("mode", [INSTALL, ADDITIONAL])
    def test_anything_else_is_reported_and_the_picker_stays(
        self, tmp_path: Path, mode: str, answer: str
    ) -> None:
        menu, state, deps, _, _ = build(tmp_path, mode, [answer])
        assert menu.step() == f"unit_type_{mode}"
        assert state.message == "Invalid selection, please try again."
        assert state.selection.installation_type == "BoxTurtle (4-Lane)"
        assert calls(deps) == []

    def test_end_of_input_exits_one(self, tmp_path: Path) -> None:
        menu, _, deps, _, _ = build(tmp_path, INSTALL, [])
        with pytest.raises(InstallerExit) as exc:
            menu.step()
        assert exc.value.code == 1
        assert calls(deps) == []


class TestRegistry:
    def test_both_pickers_are_registered(self, tmp_path: Path) -> None:
        deps, _, _ = fake_deps([])
        state = make_state(tmp_path)
        assert isinstance(MENUS["unit_type_install"](state, deps), UnitTypeMenu)
        assert isinstance(MENUS["unit_type_additional"](state, deps), UnitTypeMenu)
