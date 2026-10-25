from pathlib import Path
from typing import List, Sequence

import pytest

from installer.platform_core.context import Context
from installer.unit_registry.base import UnitType
from installer.unit_registry.options import (
    ChoiceOption,
    NameOption,
    UnitOption,
)
from installer.unit_registry.registry import UNIT_TYPES, unit_for
from installer.unit_registry.selection import UnitSelection
from installer.unit_registry.units.openams import OpenAms
from installer.unit_registry.units.vivid import Vivid
from tests.installer.fakes import RecordingConsole, ScriptedPrompter


def make_ctx(tmp_path: Path) -> Context:
    return Context.create(afc_path=tmp_path / "afc", home=tmp_path)


def activate(
    option: UnitOption, tmp_path: Path, sel: UnitSelection, answers: Sequence[str] = ()
) -> str:
    return option.activate(
        make_ctx(tmp_path), RecordingConsole(), ScriptedPrompter(answers), sel  # type: ignore
    )


class TestChoiceOption:
    def option(self) -> ChoiceOption:
        return ChoiceOption("Thing", "htlf_board_type", ("a", "b", "c"))

    def test_value_is_the_current_attribute_text(self) -> None:
        sel = UnitSelection(htlf_board_type="b")
        assert self.option().value(sel) == "b"

    def test_activate_steps_to_the_next_choice_and_reports_it(self, tmp_path: Path) -> None:
        sel = UnitSelection(htlf_board_type="a")
        assert activate(self.option(), tmp_path, sel) == "Thing: b"
        assert sel.htlf_board_type == "b"

    def test_it_wraps_around_after_the_last_choice(self, tmp_path: Path) -> None:
        sel = UnitSelection(htlf_board_type="c")
        assert activate(self.option(), tmp_path, sel) == "Thing: a"
        assert sel.htlf_board_type == "a"

    def test_an_unknown_current_value_restarts_at_the_first_choice(
        self, tmp_path: Path
    ) -> None:
        sel = UnitSelection(htlf_board_type="zzz")
        activate(self.option(), tmp_path, sel)
        assert sel.htlf_board_type == "a"

    def test_a_single_choice_stays_put(self, tmp_path: Path) -> None:
        sel = UnitSelection(claymore_board_type="AFC_Lite")
        option = ChoiceOption("Board", "claymore_board_type", ("AFC_Lite",))
        assert activate(option, tmp_path, sel) == "Board: AFC_Lite"

    def test_cast_converts_the_stored_value_only(self, tmp_path: Path) -> None:
        sel = UnitSelection(emu_num_lanes=8)
        option = ChoiceOption("Lanes", "emu_num_lanes", ("1", "2", "8"), cast=int)
        assert option.value(sel) == "8"
        assert activate(option, tmp_path, sel) == "Lanes: 1"
        assert sel.emu_num_lanes == 1
        assert isinstance(sel.emu_num_lanes, int)


class TestNameOption:
    def test_the_first_unit_prompt(self, tmp_path: Path) -> None:
        sel = UnitSelection(installation_type="HTLF")
        console, prompter = RecordingConsole(), ScriptedPrompter(["MyUnit"])
        message = NameOption("HTLF Name").activate(
            make_ctx(tmp_path), console, prompter, sel  # type: ignore[arg-type]
        )
        assert prompter.prompts == ["Enter name for unit (Default: HTLF_1): "]
        assert (sel.unit_name, message) == ("MyUnit", "Naming unit MyUnit")

    def test_an_empty_answer_takes_the_unit_types_default_name(self, tmp_path: Path) -> None:
        sel = UnitSelection(installation_type="HTLF", unit_name="Other")
        NameOption("HTLF Name").activate(
            make_ctx(tmp_path), RecordingConsole(), ScriptedPrompter([""]), sel  # type: ignore
        )
        assert sel.unit_name == "HTLF_1"

    def test_an_unknown_type_falls_back_to_turtle_1(self, tmp_path: Path) -> None:
        sel = UnitSelection(installation_type="Mystery", unit_name="Other")
        prompter = ScriptedPrompter([""])
        NameOption("X Name").activate(
            make_ctx(tmp_path), RecordingConsole(), prompter, sel  # type: ignore[arg-type]
        )
        assert prompter.prompts == ["Enter name for unit (Default: Turtle_1): "]
        assert sel.unit_name == "Turtle_1"

    def test_the_additional_prompt_offers_the_lowest_free_name(self, tmp_path: Path) -> None:
        sel = UnitSelection(installation_type="HTLF")
        prompter = ScriptedPrompter([""])
        NameOption("HTLF Name", additional_prompt=True).activate(
            make_ctx(tmp_path), RecordingConsole(), prompter, sel  # type: ignore[arg-type]
        )
        assert prompter.prompts == ["Enter name for unit (Default: HTLF_1): "]
        assert sel.unit_name == "HTLF_1"

    def test_value_is_the_unit_name(self) -> None:
        assert NameOption("X").value(UnitSelection(unit_name="Abc")) == "Abc"


class TestUnitTypeDefaults:
    def test_a_type_without_options_lists_none(self) -> None:
        class Plain(UnitType):
            display_name = "Plain"
            key = "Plain"
            additional_name_prefix = "Plain"

            def copy_files(self, ctx, console, prompter, sel) -> None:  # type: ignore
                pass

        assert Plain().install_options() == []
        assert Plain().additional_options() == []

    @pytest.mark.parametrize("unit", UNIT_TYPES, ids=[u.key for u in UNIT_TYPES])
    def test_buffer_selectable_and_ramming_wording(self, unit) -> None:  # type: ignore
        no_choice = unit.key in ("ViViD", "OpenAMS")
        assert unit.buffer_selectable is not no_choice
        expected = "an FPS Board" if unit.key == "OpenAMS" else "a TurtleNeck buffer"
        assert unit.ramming_hardware == expected


class TestQuattroBoxCycling:
    def test_it_steps_from_its_current_value_unlike_the_old_counter(
        self, tmp_path: Path
    ) -> None:
        """The old menu counted from 0 whatever the value was (E gave MMB_1.1, F did nothing)."""
        unit = unit_for("QuattroBox")
        assert unit is not None
        board, motor = unit.install_options()
        sel = UnitSelection()
        assert (sel.qb_board_type, sel.qb_motor_type) == ("MMB_2.0", "NEMA_14")
        assert activate(board, tmp_path, sel) == "QuattroBox Board Type: MMB_1.0"
        assert activate(motor, tmp_path, sel) == "QuattroBox Motor Type: NEMA_17"


class TestDefaultName:
    @pytest.mark.parametrize(
        "key,expected",
        [
            ("BoxTurtle_4Lane", "Turtle_1"), ("BoxTurtle_8Lane", "Turtle_1"),
            ("NightOwl", "NightOwl_1"), ("HTLF", "HTLF_1"), ("Claymore", "Claymore_1"),
            ("QuattroBox", "QuattroBox_1"), ("OpenAMS", "AMS_1"), ("ViViD", "Vivid_1"),
            ("EMU", "EMU_1"),
        ],
    )
    def test_each_type_starts_with_its_own_first_name(self, key: str, expected: str) -> None:
        unit = [u for u in UNIT_TYPES if u.key == key][0]
        assert unit.default_name == expected


class TestUnitOptionLists:
    @pytest.mark.parametrize(
        "key,install,additional",
        [
            ("BoxTurtle_4Lane", ["BoxTurtle Name"], ["BoxTurtle Name"]),
            ("BoxTurtle_8Lane", ["BoxTurtle Name"], ["BoxTurtle Name"]),
            ("NightOwl", ["NightOwl Board Type"], ["NightOwl Name", "NightOwl Board Type"]),
            ("HTLF", ["HTLF Name", "HTLF Board Type"], ["HTLF Name", "HTLF Board Type"]),
            ("Claymore", ["Claymore Name", "Claymore Board Type"],
             ["Claymore Name", "Claymore Board Type"]),
            ("QuattroBox", ["QuattroBox Board Type", "QuattroBox Motor Type"],
             ["QuattroBox Name", "QuattroBox Board Type", "QuattroBox Motor Type"]),
            ("OpenAMS", [], ["OpenAMS Name"]),
            ("ViViD", [], ["ViViD Name"]),
            ("EMU", ["EMU Name", "EMU Lane Count", "EMU Board Type"],
             ["EMU Name", "EMU Lane Count", "EMU Board Type"]),
        ],
    )
    def test_labels_in_order(
        self, key: str, install: List[str], additional: List[str]
    ) -> None:
        unit = [u for u in UNIT_TYPES if u.key == key][0]
        assert [o.label for o in unit.install_options()] == install
        assert [o.label for o in unit.additional_options()] == additional

    def test_only_emu_names_its_first_unit_with_the_additional_prompt(self) -> None:
        flags = {
            u.key: [getattr(o, "additional_prompt", None) for o in u.install_options()
                    if o.label.endswith(" Name")]
            for u in UNIT_TYPES
        }
        assert flags["EMU"] == [True]
        assert all(v in ([], [False]) for k, v in flags.items() if k != "EMU")

    def test_every_additional_name_option_uses_the_additional_prompt(self) -> None:
        for unit in UNIT_TYPES:
            names = [o for o in unit.additional_options() if o.label.endswith(" Name")]
            assert names and all(getattr(o, "additional_prompt") for o in names), unit.key

    def test_the_lane_count_is_stored_as_a_number(self, tmp_path: Path) -> None:
        unit = unit_for("EMU")
        assert unit is not None
        lanes = [o for o in unit.install_options() if o.label == "EMU Lane Count"][0]
        sel = UnitSelection()
        assert lanes.value(sel) == "5"
        activate(lanes, tmp_path, sel)
        assert sel.emu_num_lanes == 6
        assert isinstance(sel.emu_num_lanes, int)


class TestRefreshAdditionalName:
    def test_resets_to_the_lowest_free_default(self, tmp_path: Path) -> None:
        unit = unit_for("HTLF")
        assert unit is not None
        sel = UnitSelection(installation_type="HTLF", unit_name="stale")
        unit.refresh_additional_name(make_ctx(tmp_path), sel)
        assert sel.unit_name == "HTLF_1"

    def test_keeps_a_name_the_user_chose(self, tmp_path: Path) -> None:
        unit = unit_for("HTLF")
        assert unit is not None
        sel = UnitSelection(installation_type="HTLF", unit_name="Mine", turtle_renamed=True)
        unit.refresh_additional_name(make_ctx(tmp_path), sel)
        assert sel.unit_name == "Mine"


class TestOnSelect:
    def test_most_types_leave_the_selection_alone(self) -> None:
        unit = unit_for("BoxTurtle (4-Lane)")
        assert unit is not None
        sel = UnitSelection(buffer_type="TurtleNeckV2")
        unit.on_select(sel)
        assert sel.buffer_type == "TurtleNeckV2"

    def test_vivid_clears_the_buffer(self) -> None:
        sel = UnitSelection(buffer_type="FPS_PSF")
        Vivid().on_select(sel)
        assert sel.buffer_type == "None"

    @pytest.mark.parametrize("sensor,expected", [("Ramming", "FPS_PSF"), ("Sensor", "None")])
    def test_openams_follows_the_sensor_choice(self, sensor: str, expected: str) -> None:
        sel = UnitSelection(toolhead_sensor=sensor, buffer_type="TurtleNeck")
        OpenAms().on_select(sel)
        assert sel.buffer_type == expected


class TestAfterSensorChange:
    def change(self, itype: str, sensor: str, buffer: str) -> "tuple[str, str]":
        unit = unit_for(itype)
        assert unit is not None
        sel = UnitSelection(installation_type=itype, toolhead_sensor=sensor, buffer_type=buffer)
        message = unit.after_sensor_change(sel)
        return message, sel.buffer_type

    @pytest.mark.parametrize(
        "buffer,expected", [("Unknown", "None"), ("None", "None"), ("TurtleNeckV2", "TurtleNeckV2")]
    )
    def test_default_type_with_a_sensor(self, buffer: str, expected: str) -> None:
        assert self.change("HTLF", "Sensor", buffer) == ("Using toolhead sensor", expected)

    def test_default_type_back_to_ramming_with_no_buffer_asks_for_one(self) -> None:
        assert self.change("HTLF", "Ramming", "None") == (
            "Using ramming with a TurtleNeck buffer. Select a buffer type", "Unknown")

    def test_default_type_back_to_ramming_with_an_unknown_buffer_still_asks(self) -> None:
        message, buffer = self.change("HTLF", "Ramming", "Unknown")
        assert message.endswith(". Select a buffer type")
        assert buffer == "Unknown"

    def test_default_type_back_to_ramming_keeps_a_chosen_buffer(self) -> None:
        assert self.change("HTLF", "Ramming", "FPS_PSF") == (
            "Using ramming with a TurtleNeck buffer", "FPS_PSF")

    def test_vivid_only_clears_an_unknown_buffer_for_a_sensor(self) -> None:
        assert self.change("ViViD", "Sensor", "Unknown") == ("Using toolhead sensor", "None")
        assert self.change("ViViD", "Sensor", "FPS_PSF") == ("Using toolhead sensor", "FPS_PSF")

    def test_vivid_ramming_changes_nothing(self) -> None:
        assert self.change("ViViD", "Ramming", "None") == (
            "Using ramming with a TurtleNeck buffer", "None")

    def test_openams_sets_the_buffer_either_way(self) -> None:
        assert self.change("OpenAMS", "Sensor", "FPS_PSF") == ("Using toolhead sensor", "None")
        assert self.change("OpenAMS", "Ramming", "None") == (
            "Using ramming with an FPS Board", "FPS_PSF")


class TestCycleBuffer:
    @pytest.mark.parametrize(
        "sensor,current,expected",
        [
            ("Ramming", "TurtleNeck", "TurtleNeckV2"),
            ("Ramming", "TurtleNeckV2", "TurtleNeck2Remastered"),
            ("Ramming", "TurtleNeck2Remastered", "FPS_PSF"),
            ("Ramming", "FPS_PSF", "TurtleNeck"),
            ("Sensor", "FPS_PSF", "None"),
            ("Sensor", "None", "TurtleNeck"),
            ("Ramming", "Unknown", "TurtleNeck"),
        ],
    )
    def test_cycle(self, sensor: str, current: str, expected: str) -> None:
        unit = unit_for("HTLF")
        assert unit is not None
        sel = UnitSelection(toolhead_sensor=sensor, buffer_type=current)
        unit.cycle_buffer(sel)
        assert sel.buffer_type == expected
