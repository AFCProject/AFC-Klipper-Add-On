from pathlib import Path
from typing import Any, List, Sequence, Tuple

import pytest

from installer.platform_core.console import NC, RED
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.ui_menus import additional_options_menu as module
from installer.ui_menus.additional_options_menu import AdditionalOptionsMenu, own_buffer_label
from installer.ui_menus.menus import MENUS
from installer.ui_menus.navigation import MenuDeps
from installer.ui_menus.state import MenuState
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import RecordingConsole, ScriptedPrompter
from tests.installer.test_ui_menus_basic import FakeActions, fake_deps, row_texts

FOOTER = [
    "", "I. Install system with current selections", "B. Back to unit type selection",
    "M. Return to Main Menu", "Q. Quit", "",
]
INTRO = (
    "This menu allows you to add ADDITIONAL unit types. "
    "It should NOT be used for your first unit.\n"
    "This is a best effort in adding an additional unit. You will probably be required\n"
    "to manually edit the configuration files to ensure proper operation.\n"
)
BASE = (
    "{name} created successfully, please look over config file and update lane numbers.\n"
    "Additionally, please ensure any MCU connections are updated in the appropriate files "
    "(CANBus, serial, etc)\n"
    "This is not a 100% turn-key solution and will require some manual configuration "
    "based on your specific setup."
)
RESTART = (
    f"\n\n{RED}Please restart the script after installation to install additional units "
    f"or make changes to the current unit.{NC}"
)


class Recorder:
    def __init__(self, shared_answer: str = "") -> None:
        self.calls: List[Tuple[str, ...]] = []
        self.shared_answer = shared_answer

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def unit(ctx: Any, console: Any, prompter: Any, sel: Any, buffer_type: str) -> None:
            self.calls.append(("unit", buffer_type, str(sel.is_additional_unit)))

        def shared(ctx: Any, console: Any, sel: Any, name: str) -> None:
            self.calls.append(("shared", name, str(sel.is_additional_unit)))

        def prompt(ctx: Any, console: Any, prompter: Any) -> str:
            self.calls.append(("prompt",))
            return self.shared_answer

        monkeypatch.setattr(module, "apply_unit_buffer", unit)
        monkeypatch.setattr(module, "apply_shared_buffer", shared)
        monkeypatch.setattr(module, "prompt_shared_buffer_name", prompt)


def build(
    tmp_path: Path, answers: Sequence[str], itype: str = "BoxTurtle (4-Lane)",
    buffer: str = "TurtleNeck", **sel: object,
) -> Tuple[AdditionalOptionsMenu, MenuState, MenuDeps, RecordingConsole, ScriptedPrompter]:
    ctx = Context.create(afc_path=tmp_path / "afc", home=tmp_path)
    ctx.afc_config_dir.mkdir(parents=True, exist_ok=True)
    (ctx.afc_config_dir / "AFC_Turtle_1.cfg").write_text("[AFC_stepper lane1]\nname: Turtle_1\n")
    sel.setdefault("is_additional_unit", True)
    selection = UnitSelection(
        installation_type=itype, additional_buffer_type=buffer, **sel  # type: ignore
    )
    state = MenuState(ctx=ctx, selection=selection)
    deps, console, prompter = fake_deps(answers)
    return AdditionalOptionsMenu(state, deps), state, deps, console, prompter


def calls(deps: MenuDeps) -> List[str]:
    assert isinstance(deps.actions, FakeActions)
    return deps.actions.calls


def write_printer_extruders(ctx: Context, *names: str) -> None:
    ctx.printer_config_dir.mkdir(parents=True, exist_ok=True)
    text = "".join(f"[{name}]\n" for name in names)
    (ctx.printer_config_dir / "printer.cfg").write_text(text)


class TestOwnBufferLabel:
    def test_none_means_shared(self) -> None:
        assert own_buffer_label("None") == "No (shared with an existing unit's buffer)"

    def test_a_type_is_yes_with_the_friendly_label(self) -> None:
        assert own_buffer_label("TurtleNeck") == "Yes: TurtleNeck"
        assert own_buffer_label("FPS_PSF") == "Yes: Proportional Sync-Feedback(PSF)"
        assert own_buffer_label("TurtleNeck2Remastered") == "Yes: TurtleNeck 2 Remastered"


class TestEntry:
    def test_shows_the_intro_and_offers_the_lowest_free_name(self, tmp_path: Path) -> None:
        _, state, _, _, _ = build(tmp_path, [])
        assert state.message == INTRO
        assert state.selection.unit_name == "Turtle_2"

    def test_a_name_the_user_chose_is_kept(self, tmp_path: Path) -> None:
        _, state, _, _, _ = build(tmp_path, [], unit_name="Mine", turtle_renamed=True)
        assert state.selection.unit_name == "Mine"

    def test_the_default_name_is_refreshed_before_each_draw(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["x"])
        state.selection.unit_name = "stale"
        menu.step()
        assert state.selection.unit_name == "Turtle_2"


class TestView:
    def test_a_boxturtle_with_its_own_buffer(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [])
        assert row_texts(menu.view()) == [
            "Unit type : BoxTurtle (4-Lane)", "",
            "1. BoxTurtle Name : Turtle_2",
            "2. Unit has its own buffer? : Yes: TurtleNeck",
            *FOOTER,
        ]

    def test_unit_options_come_before_the_buffer_row(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [], itype="QuattroBox")
        texts = row_texts(menu.view())
        assert texts[2:6] == [
            "1. QuattroBox Name : QuattroBox_1", "2. QuattroBox Board Type : MMB_2.0",
            "3. QuattroBox Motor Type : NEMA_14",
            "4. Unit has its own buffer? : Yes: TurtleNeck",
        ]

    def test_types_without_a_buffer_have_no_buffer_row(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [], itype="ViViD", buffer="None")
        texts = row_texts(menu.view())
        assert texts[2:3] == ["1. ViViD Name : Vivid_1"]
        assert not [t for t in texts if "own buffer" in t]

    def test_the_install_row_is_green(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [])
        install_row = menu.view().rows[-5]
        assert [(s.text, s.color) for s in install_row] == [
            ("I. Install system with current selections", "green")]

    def test_the_banner_follows_the_type(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [], itype="EMU")
        assert menu.view().art == "EMU"


class TestOptionRows:
    def test_the_name_row_asks_with_the_additional_prompt(self, tmp_path: Path) -> None:
        menu, state, _, _, prompter = build(tmp_path, ["1", "Foo"])
        menu.step()
        assert prompter.prompts[-1] == "Enter name for unit (Default: Turtle_2): "
        assert state.selection.unit_name == "Foo"
        assert state.message == "Naming unit Foo"

    def test_a_board_row_cycles(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["2"], itype="HTLF")
        menu.step()
        assert state.selection.htlf_board_type == "MMB_1.1"
        assert state.message == "HTLF Board Type: MMB_1.1"

    def test_the_buffer_row_cycles_through_the_types_options_and_wraps(
        self, tmp_path: Path
    ) -> None:
        menu, state, _, _, _ = build(tmp_path, ["2"] * 5)
        seen = []
        for _ in range(5):
            menu.step()
            seen.append(state.selection.additional_buffer_type)
        assert seen == [
            "TurtleNeckV2", "TurtleNeck2Remastered", "FPS_PSF", "None", "TurtleNeck"
        ]
        assert state.message == "Unit has its own buffer? Yes: TurtleNeck"

    def test_a_value_outside_the_list_restarts_at_the_first_option(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["2"], buffer="Bogus")
        menu.step()
        assert state.selection.additional_buffer_type == "None"

    def test_numbers_beyond_the_list_are_invalid(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["3"])
        assert menu.step() == "additional_options"
        assert state.message == "Invalid selection, please try again."


class TestExtruderRow:
    def test_a_single_printer_extruder_shows_no_row(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, [])
        write_printer_extruders(state.ctx, "extruder")
        assert not [r for r in row_texts(menu.view()) if "Extruder" in r]

    def test_multiple_extruders_add_a_row_after_the_buffer_row(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, [])
        write_printer_extruders(state.ctx, "extruder", "extruder1")
        texts = row_texts(menu.view())
        assert texts[2:5] == [
            "1. BoxTurtle Name : Turtle_2",
            "2. Unit has its own buffer? : Yes: TurtleNeck",
            "3. Extruder unit will be connected to : extruder",
        ]

    def test_cycling_wraps_back_to_the_first_extruder(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["3"] * 3)
        write_printer_extruders(state.ctx, "extruder", "extruder1", "extruder2")
        seen = []
        for _ in range(3):
            menu.step()
            seen.append(state.selection.extruder)
        assert seen == ["extruder1", "extruder2", "extruder"]
        assert state.message == "Extruder: extruder"

    def test_a_value_outside_the_list_restarts_at_the_first_extruder(
        self, tmp_path: Path
    ) -> None:
        menu, state, _, _, _ = build(tmp_path, ["3"], extruder="Bogus")
        write_printer_extruders(state.ctx, "extruder", "extruder1")
        menu.step()
        assert state.selection.extruder == "extruder"


class TestInstallKey:
    def test_own_buffer_installs_applies_the_buffer_and_returns_to_main(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = Recorder()
        rec.install(monkeypatch)
        menu, state, deps, _, _ = build(tmp_path, ["i"])
        assert menu.step() == "main"
        assert calls(deps) == ["install_additional"]
        assert rec.calls == [("unit", "TurtleNeck", "True")]
        assert state.selection.is_additional_unit is True
        assert state.message == BASE.format(name="Turtle_2") + RESTART

    def test_extruder_sees_is_additional_unit_true_when_buffer_is_supported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: List[str] = []

        def fake_apply_unit_extruder(ctx: Any, console: Any, prompter: Any, sel: Any) -> None:
            seen.append(str(sel.is_additional_unit))

        monkeypatch.setattr(module, "apply_unit_extruder", fake_apply_unit_extruder)
        Recorder().install(monkeypatch)
        menu, state, _, _, _ = build(tmp_path, ["i"])
        menu.step()
        assert seen == ["True"]
        assert state.selection.is_additional_unit is True

    def test_extruder_sees_is_additional_unit_true_when_buffer_is_not_supported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: List[str] = []

        def fake_apply_unit_extruder(ctx: Any, console: Any, prompter: Any, sel: Any) -> None:
            seen.append(str(sel.is_additional_unit))

        monkeypatch.setattr(module, "apply_unit_extruder", fake_apply_unit_extruder)
        menu, state, _, _, _ = build(tmp_path, ["i"], itype="ViViD", buffer="None")
        menu.step()
        assert seen == ["True"]
        assert state.selection.is_additional_unit is True

    def test_a_shared_buffer_asks_which_one_and_applies_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = Recorder(shared_answer="Main_Buf")
        rec.install(monkeypatch)
        menu, state, _, _, _ = build(tmp_path, ["i"], buffer="None")
        menu.step()
        assert rec.calls == [("prompt",), ("shared", "Main_Buf", "True")]
        assert state.message == (
            BASE.format(name="Turtle_2")
            + "\n\nTurtle_2 is set to share the existing buffer 'Main_Buf'." + RESTART
        )

    def test_a_skipped_shared_buffer_applies_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = Recorder(shared_answer="")
        rec.install(monkeypatch)
        menu, state, _, _, _ = build(tmp_path, ["i"], buffer="None")
        menu.step()
        assert rec.calls == [("prompt",)]
        assert state.message == BASE.format(name="Turtle_2") + RESTART

    def test_turtleneck_v2_adds_the_serial_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        Recorder().install(monkeypatch)
        menu, state, _, _, _ = build(tmp_path, ["i"], buffer="TurtleNeckV2")
        menu.step()
        assert state.message == (
            BASE.format(name="Turtle_2")
            + "\n\nEnsure you add the correct serial information to the "
            f"{state.ctx.afc_config_dir}/mcu/TurtleNeckv2.cfg file" + RESTART
        )

    def test_turtleneck_2_remastered_adds_the_serial_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        Recorder().install(monkeypatch)
        menu, state, _, _, _ = build(tmp_path, ["i"], buffer="TurtleNeck2Remastered")
        menu.step()
        assert state.message == (
            BASE.format(name="Turtle_2")
            + "\n\nEnsure you add the correct serial information to the "
            f"{state.ctx.afc_config_dir}/mcu/TN2.cfg file" + RESTART
        )

    def test_fps_psf_adds_the_adc_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        Recorder().install(monkeypatch)
        menu, state, _, _, _ = build(tmp_path, ["i"], buffer="FPS_PSF")
        menu.step()
        assert state.message == (
            BASE.format(name="Turtle_2")
            + "\n\nEnsure the PSF ADC pin in your buffer configuration matches where your "
            "wiring is connected to your MCU." + RESTART
        )

    def test_types_without_a_buffer_skip_the_buffer_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = Recorder()
        rec.install(monkeypatch)
        menu, state, deps, _, _ = build(tmp_path, ["i"], itype="ViViD", buffer="None")
        assert menu.step() == "main"
        assert calls(deps) == ["install_additional"]
        assert rec.calls == []
        assert state.message == BASE.format(name="Vivid_1") + RESTART

    def test_a_name_in_use_is_rejected_before_anything_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = Recorder()
        rec.install(monkeypatch)
        menu, state, deps, _, _ = build(
            tmp_path, ["i"], unit_name="Turtle_1", turtle_renamed=True
        )
        assert menu.step() == "additional_options"
        assert state.message == "Unit Turtle_1 already exists, please enter a unique unit name."
        assert calls(deps) == []
        assert rec.calls == []


class TestNavigation:
    def test_back_returns_to_the_picker_and_clears_the_message(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["b"])
        assert menu.step() == "unit_type_additional"
        assert state.message == ""

    def test_main_clears_the_message(self, tmp_path: Path) -> None:
        menu, state, _, _, _ = build(tmp_path, ["m"])
        assert menu.step() == "main"
        assert state.message == ""

    def test_quit_runs_the_exit_action(self, tmp_path: Path) -> None:
        menu, _, deps, _, _ = build(tmp_path, ["q"])
        assert menu.step() is None
        assert calls(deps) == ["exit"]

    @pytest.mark.parametrize("answer", ["0", "-1", "x", "", "²"])
    def test_anything_else_is_invalid(self, tmp_path: Path, answer: str) -> None:
        menu, state, deps, _, _ = build(tmp_path, [answer])
        assert menu.step() == "additional_options"
        assert state.message == "Invalid selection, please try again."
        assert calls(deps) == []

    def test_end_of_input_exits_one(self, tmp_path: Path) -> None:
        menu, _, _, _, _ = build(tmp_path, [])
        with pytest.raises(InstallerExit) as exc:
            menu.step()
        assert exc.value.code == 1


class TestRegistry:
    def test_registered(self, tmp_path: Path) -> None:
        _, state, deps, _, _ = build(tmp_path, [])
        assert isinstance(MENUS["additional_options"](state, deps), AdditionalOptionsMenu)
