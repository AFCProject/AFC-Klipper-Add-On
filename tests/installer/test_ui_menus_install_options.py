from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import pytest

from installer.platform_core.console import NC, RED
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.platform_core.toggles import TOGGLES
from installer.ui_menus.install_options_menu import InstallOptionsMenu
from installer.ui_menus.menus import MENUS
from installer.ui_menus.navigation import MenuDeps, run_menus
from installer.ui_menus.state import MenuState
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import RecordingConsole, ScriptedPrompter
from tests.installer.test_ui_menus_basic import FakeActions, fake_deps, row_texts

NEEDS_BUFFER = f"{RED}Ramming requires a buffer type. Choose one from the list.{NC}"
INVALID = "Invalid selection, please try again."
FOOTER = [
    "\nI. Install system with current selections", "B. Back to unit type selection",
    "M. Return to Main Menu", "Q. Quit", "",
]


def build(
    tmp_path: Path, answers: Sequence[str], itype: str = "BoxTurtle (4-Lane)",
    sensor: str = "Ramming", buffer: str = "Unknown", **sel: object,
) -> Tuple[InstallOptionsMenu, MenuState, MenuDeps, RecordingConsole, ScriptedPrompter]:
    ctx = Context.create(afc_path=tmp_path / "afc", home=tmp_path)
    selection = UnitSelection(
        installation_type=itype, toolhead_sensor=sensor, buffer_type=buffer, **sel  # type: ignore
    )
    state = MenuState(ctx=ctx, selection=selection)
    deps, console, prompter = fake_deps(answers)
    return InstallOptionsMenu(state, deps), state, deps, console, prompter


def calls(deps: MenuDeps) -> List[str]:
    assert isinstance(deps.actions, FakeActions)
    return deps.actions.calls


class TestView:
    def test_a_boxturtle_with_ramming_and_no_buffer(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [])
        assert row_texts(menu.view()) == [
            "Unit type : BoxTurtle (4-Lane)", "",
            "1. Add AFC includes? : True", "2. Enable tip-forming? : False",
            "3. Enable toolhead cutter? : True", "4. Enable hub cutter? : False",
            "5. Enable kick macro? : True", "6. Enable park macro? : True",
            "7. Enable poop macro? : True", "8. Enable wipe macro? : True",
            "9. Include the tip cut servo example? : False",
            "10. Use a toolhead sensor or ramming with a TurtleNeck buffer? : Ramming",
            "11. Buffer type : Unknown (a buffer is required with ramming)",
            "12. BoxTurtle Name : Turtle_1",
            *FOOTER,
        ]

    def test_the_sensor_pin_comes_last_and_nothing_else_moves(self, tmp_path: Path) -> None:
        ramming, _, _, _, _ = build(tmp_path, [])
        sensor, _, _, _, _ = build(tmp_path, [], sensor="Sensor", buffer="None")
        ramming_rows, sensor_rows = row_texts(ramming.view()), row_texts(sensor.view())
        assert sensor_rows[-len(FOOTER) - 1] == "13. Toolhead sensor pin : Unknown"
        assert len(sensor_rows) == len(ramming_rows) + 1
        for number in ("1.", "5.", "9.", "12."):
            ours = [r for r in sensor_rows if r.startswith(f"{number} ")]
            theirs = [r for r in ramming_rows if r.startswith(f"{number} ")]
            assert ours == theirs

    def test_an_unknown_pin_is_red_and_a_set_one_is_not(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, [], sensor="Sensor", buffer="None")
        pin_row = menu.view().rows[-len(FOOTER) - 1]
        assert [(s.text, s.color) for s in pin_row] == [
            ("13. Toolhead sensor pin : ", ""), ("Unknown", "red")]
        state.selection.toolhead_sensor_pin = "nhk:gpio1"
        pin_row = menu.view().rows[-len(FOOTER) - 1]
        assert [(s.text, s.color) for s in pin_row] == [
            ("13. Toolhead sensor pin : ", ""), ("nhk:gpio1", "")]

    def test_a_required_buffer_is_red(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [], buffer="None")
        buffer_row = menu.view().rows[12]
        assert [(s.text, s.color) for s in buffer_row] == [
            ("11. Buffer type : ", ""), ("None", "red"),
            (" (a buffer is required with ramming)", "")]

    def test_a_chosen_buffer_uses_the_friendly_label(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [], buffer="FPS_PSF")
        assert "11. Buffer type : Proportional Sync-Feedback(PSF)" in row_texts(menu.view())

    def test_with_a_sensor_no_buffer_is_fine(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [], sensor="Sensor", buffer="None")
        assert "11. Buffer type : None" in row_texts(menu.view())

    @pytest.mark.parametrize("itype", ["ViViD", "OpenAMS"])
    def test_types_without_a_buffer_choice_have_no_buffer_row(
        self, tmp_path: Path, itype: str
    ) -> None:
        menu, _, _, _, _ = build(tmp_path, [], itype=itype, buffer="None")
        assert not [r for r in row_texts(menu.view()) if "Buffer type" in r]

    def test_openams_names_the_fps_board_and_has_no_unit_rows(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [], itype="OpenAMS", buffer="FPS_PSF")
        texts = row_texts(menu.view())
        assert "10. Use a toolhead sensor or ramming with an FPS Board? : Ramming" in texts
        assert texts[-len(FOOTER) - 1].startswith("10.")

    def test_unit_options_follow_the_buffer_row(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [], itype="HTLF", buffer="TurtleNeck")
        texts = row_texts(menu.view())
        assert texts[-len(FOOTER) - 2:-len(FOOTER)] == [
            "12. HTLF Name : Turtle_1", "13. HTLF Board Type : ERB"]

    def test_the_banner_follows_the_type(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [], itype="EMU")
        assert menu.view().art == "EMU"

    def test_ace_has_no_sensor_row_only_a_buffer_and_version_row(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(
            tmp_path, [], itype="ACE", sensor="Sensor", buffer="Internal", ace_unit_type="V1"
        )
        texts = row_texts(menu.view())
        assert not [r for r in texts if "toolhead sensor or ramming" in r]
        assert "10. Buffer type : Internal buffer" in texts
        assert "11. ACE Unit Type : V1" in texts
        assert "12. Toolhead sensor pin : " in "".join(texts)

    def test_after_the_install_only_the_footer_remains(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, [])
        state.ctx.files_updated_or_installed = True
        assert row_texts(menu.view()) == ["M. Return to Main Menu", "Q. Quit", ""]


class TestToggles:
    @pytest.mark.parametrize("number", range(1, len(TOGGLES) + 1))
    def test_a_number_flips_its_own_toggle_only(self, tmp_path: Path, number: int) -> None:
        menu, state, _, _, _ = build(tmp_path, [str(number), str(number)])
        before = dict(state.toggles)
        name, label = TOGGLES[number - 1].key, TOGGLES[number - 1].label
        assert menu.step() == "install_options"
        assert state.toggles == {**before, name: not before[name]}
        assert state.message == f"{label} {'Disabled' if before[name] else 'Enabled'}"
        menu.step()
        assert state.toggles == before
        assert state.message == f"{label} {'Enabled' if before[name] else 'Disabled'}"


class TestTipCutServoToggle:
    def test_it_is_the_ninth_row_and_off_by_default(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["9"])
        assert state.toggles["tip_cut_servo"] is False
        menu.step()
        assert state.toggles["tip_cut_servo"] is True
        assert state.message == "Include the tip cut servo example? Enabled"


class TestSensorRow:
    def test_flipping_to_a_sensor_clears_an_unknown_buffer(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["10"])
        menu.step()
        assert state.selection.toolhead_sensor == "Sensor"
        assert state.selection.buffer_type == "None"
        assert state.message == "Using toolhead sensor"

    def test_flipping_back_to_ramming_asks_for_a_buffer(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["10"], sensor="Sensor", buffer="None")
        menu.step()
        assert state.selection.toolhead_sensor == "Ramming"
        assert state.selection.buffer_type == "Unknown"
        assert state.message == "Using ramming with a TurtleNeck buffer. Select a buffer type"

    def test_the_unit_type_decides_the_buffer_for_openams(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["10"], itype="OpenAMS", buffer="FPS_PSF")
        menu.step()
        assert (state.selection.toolhead_sensor, state.selection.buffer_type) == ("Sensor", "None")


class TestBufferRow:
    def test_remastered_is_named_with_its_friendly_label(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["11"] * 3)
        for _ in range(3):
            menu.step()
        assert state.selection.buffer_type == "TurtleNeck2Remastered"
        assert state.message == "Buffer Type: TurtleNeck 2 Remastered"

    def test_a_number_cycles_the_buffer_and_names_it(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["11"] * 4)
        seen = []
        for _ in range(4):
            menu.step()
            seen.append(state.selection.buffer_type)
        assert seen == ["TurtleNeck", "TurtleNeckV2", "TurtleNeck2Remastered", "FPS_PSF"]
        assert state.message == "Buffer Type: Proportional Sync-Feedback(PSF)"


class TestUnitOptionRows:
    def test_the_name_row_asks_for_a_name(self, tmp_path: Path) -> None:
        menu, state, _, _, prompter = build(tmp_path, ["12", "MyUnit"])
        menu.step()
        assert prompter.prompts[-1] == "Enter name for unit (Default: Turtle_1): "
        assert state.selection.unit_name == "MyUnit"
        assert state.message == "Naming unit MyUnit"

    def test_a_board_row_cycles(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["13"], itype="HTLF", buffer="TurtleNeck")
        menu.step()
        assert state.selection.htlf_board_type == "MMB_1.1"
        assert state.message == "HTLF Board Type: MMB_1.1"

    def test_numbers_beyond_the_list_are_invalid(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["13"])
        assert menu.step() == "install_options"
        assert state.message == INVALID


class TestPinRow:
    def test_asks_for_the_pin_and_stores_it(self, tmp_path: Path) -> None:
        menu, state, _, _, prompter = build(
            tmp_path, ["13", "nhk:gpio13"], sensor="Sensor", buffer="None"
        )
        menu.step()
        assert prompter.prompts[-1] == "Enter toolhead sensor pin (Example: nhk:gpio13): "
        assert state.selection.toolhead_sensor_pin == "nhk:gpio13"
        assert state.message == "Toolhead sensor pin set to nhk:gpio13"

    def test_end_of_input_exits_one_and_keeps_the_pin(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["13"], sensor="Sensor", buffer="None")
        with pytest.raises(InstallerExit) as exc:
            menu.step()
        assert exc.value.code == 1
        assert state.selection.toolhead_sensor_pin == "Unknown"


class TestInstallKey:
    @pytest.mark.parametrize("buffer", ["Unknown", "None"])
    def test_ramming_without_a_buffer_is_refused(self, tmp_path: Path, buffer: str) -> None:
        menu, state, deps, _, _ = build(tmp_path, ["i"], buffer=buffer)
        assert menu.step() == "install_options"
        assert state.message == NEEDS_BUFFER
        assert calls(deps) == []

    @pytest.mark.parametrize("itype", ["ViViD", "OpenAMS"])
    def test_types_without_a_buffer_choice_are_never_refused(
        self, tmp_path: Path, itype: str
    ) -> None:
        menu, _, deps, _, _ = build(tmp_path, ["i"], itype=itype, buffer="None")
        assert menu.step() == "main"
        assert calls(deps) == ["install"]

    def test_a_sensor_needs_no_buffer(self, tmp_path: Path) -> None:
        menu, _, deps, _, _ = build(tmp_path, ["i"], sensor="Sensor", buffer="None")
        assert menu.step() == "main"
        assert calls(deps) == ["install"]

    def test_a_chosen_buffer_installs_and_returns_to_main_keeping_the_message(
        self, tmp_path: Path
    ) -> None:
        menu, state, deps, _, _ = build(tmp_path, ["i"], buffer="TurtleNeck")
        state.message = "left by the action"
        assert menu.step() == "main"
        assert calls(deps) == ["install"]
        assert state.message == "left by the action"


class TestNavigation:
    def test_back_returns_to_the_picker_and_clears_the_message(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["b"])
        state.message = "x"
        assert menu.step() == "unit_type_install"
        assert state.message == ""

    def test_main_clears_the_message(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["m"])
        state.message = "x"
        assert menu.step() == "main"
        assert state.message == ""

    def test_quit_runs_the_exit_action(self, tmp_path: Path) -> None:
        menu, _, deps, _, _ = build(tmp_path, ["q"])
        assert menu.step() is None
        assert calls(deps) == ["exit"]

    @pytest.mark.parametrize("answer", ["0", "-1", "x", "", "1.5", "²"])
    def test_anything_else_is_invalid(self, tmp_path: Path, answer: str) -> None:
        menu, state, deps, _, _ = build(tmp_path, [answer])
        assert menu.step() == "install_options"
        assert state.message == INVALID
        assert calls(deps) == []

    def test_end_of_input_exits_one(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [])
        with pytest.raises(InstallerExit):
            menu.step()

    @pytest.mark.parametrize("answer", ["1", "I", "B"])
    def test_after_the_install_the_options_are_locked(
        self, tmp_path: Path, answer: str
    ) -> None:
        menu, state, deps, _, _ = build(tmp_path, [answer], buffer="TurtleNeck")
        state.ctx.files_updated_or_installed = True
        before = dict(state.toggles)
        assert menu.step() == "install_options"
        assert state.message == (
            "AFC was installed in this session. Restart the script to make more changes."
        )
        assert state.toggles == before
        assert calls(deps) == []

    def test_after_the_install_main_and_quit_still_work(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["m"])
        state.ctx.files_updated_or_installed = True
        assert menu.step() == "main"


class TestRegistry:
    def test_registered_and_walks_back_to_the_picker(self, tmp_path: Path) -> None:
        menu, state, deps, _, _ = build(tmp_path, ["b", "m"])
        assert isinstance(MENUS["install_options"](state, deps), InstallOptionsMenu)
        started: List[str] = []

        def stub(name: str):  # type: ignore[no-untyped-def]
            def build_stub(s: MenuState, d: MenuDeps):  # type: ignore[no-untyped-def]
                started.append(name)

                class Stop:
                    def step(self) -> Optional[str]:
                        return None

                return Stop()
            return build_stub

        run_menus(
            "install_options", state, deps,
            {"install_options": MENUS["install_options"], "unit_type_install": stub("picker")},
        )
        assert started == ["picker"]
