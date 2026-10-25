from typing import Dict, List, Tuple

import pytest

from installer.platform_core.context import Context
from installer.unit_registry import registry
from installer.unit_registry.base import BufferTarget, UnitType
from installer.unit_registry.registry import (
    UNIT_TYPES,
    additional_buffer_default,
    additional_buffer_options,
    additional_buffer_supported,
    additional_default_name,
    unit_for,
)
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import PrinterTree, RecordingConsole, ScriptedPrompter
from tests.installer.unit_driver import (
    FIXTURES,
    REPLAYED_ELSEWHERE,
    all_cases,
    replayable_cases,
    run_python_case,
)
from tests.installer.unit_rows import additional_rows, install_rows

REPLAYED = list(replayable_cases())


class TestReplayOfTheRecordedCases:
    @pytest.mark.parametrize(
        "name, mode, overrides", REPLAYED, ids=[case[0] for case in REPLAYED]
    )
    def test_output_equals_the_recorded_fixture(
        self, name: str, mode: str, overrides: Dict[str, str]
    ) -> None:
        expected = (FIXTURES / f"{name}.txt").read_text()
        assert run_python_case(mode, overrides) == expected

    def test_every_fixture_is_either_replayed_or_has_a_task_that_will_replay_it(self) -> None:
        replayed = {case[0] for case in REPLAYED}
        pending: List[str] = []
        unregistered: List[str] = []
        for name, mode, overrides in all_cases():
            if name in replayed:
                continue
            if mode in REPLAYED_ELSEWHERE:
                pending.append(name)
            else:
                unregistered.append(name)
        # cases for unit types that land in later tasks (T15 to T17) are the only others
        registered_types = {u.display_name for u in UNIT_TYPES}
        missing_types = {
            overrides["installation_type"]
            for name, _mode, overrides in all_cases()
            if name in unregistered
        }
        assert missing_types.isdisjoint(registered_types)
        assert replayed, "no BoxTurtle fixtures were replayed"
        assert (FIXTURES / "message__BoxTurtle_4Lane__buffer-None.txt").exists()
        assert len(pending) == 110  # replayed by test_install_flow_install.py
        assert not unregistered


def make_ctx(tree: PrinterTree) -> Context:
    return Context.create(tree.afc_path, home=tree.home)


def run_unit(
    tree: PrinterTree, unit_name: str, sel: UnitSelection, additional: bool
) -> Tuple[RecordingConsole, ScriptedPrompter]:
    ctx = make_ctx(tree)
    unit = unit_for(unit_name)
    assert unit is not None
    console, prompter = RecordingConsole(), ScriptedPrompter([])
    (ctx.afc_config_dir / "mcu").mkdir(parents=True, exist_ok=True)
    (ctx.afc_config_dir / "macros").mkdir(exist_ok=True)
    if additional:
        unit.install_additional(ctx, console, prompter, sel)
    else:
        unit.copy_files(ctx, console, prompter, sel)
    return console, prompter


class TestBoxTurtleCopyFiles:
    @pytest.mark.parametrize(
        "unit_name, mcu, template",
        [
            ("BoxTurtle (4-Lane)", "AFC_Lite", "AFC_Turtle_1.cfg"),
            ("BoxTurtle (8-Lane)", "AFC_Pro", "AFC_Pro_Turtle_1.cfg"),
        ],
    )
    def test_copies_the_mcu_file_the_hardware_file_and_the_named_unit_file(
        self, printer_tree: PrinterTree, unit_name: str, mcu: str, template: str
    ) -> None:
        sel = UnitSelection(installation_type=unit_name, unit_name="Bob_7")
        run_unit(printer_tree, unit_name, sel, additional=False)
        cfg = make_ctx(printer_tree).afc_config_dir
        afc = printer_tree.afc_path
        assert (cfg / "mcu" / f"{mcu}.cfg").read_bytes() == (
            afc / "config" / "mcu" / f"{mcu}.cfg"
        ).read_bytes()
        assert (cfg / "AFC_Hardware.cfg").read_bytes() == (
            afc / "templates" / "AFC_Hardware-AFC.cfg"
        ).read_bytes()
        assert (cfg / "AFC_Bob_7.cfg").read_bytes() == (
            afc / "templates" / template
        ).read_bytes()

    def test_the_unit_file_keeps_turtle_1_inside_for_the_later_directory_wide_rename(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(unit_name="Bob_7")
        run_unit(printer_tree, "BoxTurtle (4-Lane)", sel, additional=False)
        text = (make_ctx(printer_tree).afc_config_dir / "AFC_Bob_7.cfg").read_text()
        assert "Turtle_1" in text and "Bob_7" not in text

    def test_an_existing_file_triggers_the_overwrite_prompt(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        ctx.afc_config_dir.mkdir(parents=True)
        (ctx.afc_config_dir / "AFC_Hardware.cfg").write_text("mine")
        sel = UnitSelection(unit_name="Turtle_1")
        console, prompter = run_unit(printer_tree, "BoxTurtle (4-Lane)", sel, additional=False)
        assert len(prompter.prompts) == 1
        assert (ctx.afc_config_dir / "AFC_Hardware.cfg").read_text() == "mine"
        assert console.messages[0][0] == "WARNING"


class TestBoxTurtleInstallAdditional:
    def test_renames_the_unit_and_points_it_at_its_own_mcu_file(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(unit_name="Turtle_2")
        run_unit(printer_tree, "BoxTurtle (4-Lane)", sel, additional=True)
        cfg = make_ctx(printer_tree).afc_config_dir
        text = (cfg / "AFC_Turtle_2.cfg").read_text()
        assert "[include mcu/AFC_Turtle_2_mcu.cfg]" in text
        assert "AFC_Lite.cfg" not in text
        assert (cfg / "mcu" / "AFC_Turtle_2_mcu.cfg").is_file()
        assert not (cfg / "mcu" / "AFC_Lite.cfg").exists()

    def test_every_turtle_1_in_the_unit_file_becomes_the_new_name(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(unit_name="Side_Car")
        run_unit(printer_tree, "BoxTurtle (4-Lane)", sel, additional=True)
        text = (make_ctx(printer_tree).afc_config_dir / "AFC_Side_Car.cfg").read_text()
        assert "Turtle_1" not in text
        assert "[AFC_BoxTurtle Side_Car]" in text

    def test_the_eight_lane_variant_uses_the_pro_files(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(installation_type="BoxTurtle (8-Lane)", unit_name="Big_2")
        run_unit(printer_tree, "BoxTurtle (8-Lane)", sel, additional=True)
        cfg = make_ctx(printer_tree).afc_config_dir
        assert "[include mcu/AFC_Big_2_mcu.cfg]" in (cfg / "AFC_Big_2.cfg").read_text()
        assert (cfg / "mcu" / "AFC_Big_2_mcu.cfg").read_bytes() == (
            printer_tree.afc_path / "config" / "mcu" / "AFC_Pro.cfg"
        ).read_bytes()

    def test_it_does_not_copy_the_shared_hardware_file(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(unit_name="Turtle_2")
        run_unit(printer_tree, "BoxTurtle (4-Lane)", sel, additional=True)
        assert not (make_ctx(printer_tree).afc_config_dir / "AFC_Hardware.cfg").exists()

    def test_a_skipped_unit_file_is_still_edited_in_place(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        ctx.afc_config_dir.mkdir(parents=True)
        existing = ctx.afc_config_dir / "AFC_Turtle_2.cfg"
        existing.write_text("[AFC_BoxTurtle Turtle_1]\n")
        unit = unit_for("BoxTurtle (4-Lane)")
        assert unit is not None
        (ctx.afc_config_dir / "mcu").mkdir()
        # "s" skips the existing unit file; the rename still runs on it afterwards
        unit.install_additional(
            ctx, RecordingConsole(), ScriptedPrompter(["s"]), UnitSelection(unit_name="Turtle_2")
        )
        assert existing.read_text() == "[AFC_BoxTurtle Turtle_2]\n"


class TestBoxTurtleBufferTarget:
    @pytest.mark.parametrize("unit_name", ["BoxTurtle (4-Lane)", "BoxTurtle (8-Lane)"])
    def test_targets_the_units_own_file_and_section(
        self, printer_tree: PrinterTree, unit_name: str
    ) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(unit_name)
        assert unit is not None
        target = unit.buffer_target(ctx, UnitSelection(unit_name="Bob_2"))
        assert target == BufferTarget(
            unit_name="Bob_2",
            unit_section_prefix="AFC_BoxTurtle",
            extruder_file=ctx.afc_config_dir / "AFC_Bob_2.cfg",
            section_name="Bob_2",
            prebaked_header="",
        )

    def test_the_additional_flag_changes_nothing_for_boxturtle(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for("BoxTurtle (4-Lane)")
        assert unit is not None
        first = unit.buffer_target(ctx, UnitSelection(unit_name="X_1"))
        second = unit.buffer_target(ctx, UnitSelection(unit_name="X_1", is_additional_unit=True))
        assert first == second


class TestBoxTurtleMessageAndRows:
    def test_message_names_the_unit_file_with_the_original_indentation(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for("BoxTurtle (4-Lane)")
        assert unit is not None
        expected = (
            "\n- Ensure you enter either your CAN bus or serial information in the "
            f"{ctx.afc_config_dir}/AFC_Bob.cfg file\n  "
        )
        assert unit.message(ctx, UnitSelection(unit_name="Bob")) == expected

    def test_install_menu_row_shows_the_current_name(self) -> None:
        unit = unit_for("BoxTurtle (8-Lane)")
        assert unit is not None
        assert install_rows(unit, UnitSelection(unit_name="Foo_1")) == [
            "BoxTurtle Name : Foo_1"
        ]

    def test_additional_row_keeps_a_name_the_user_already_chose(
        self, printer_tree: PrinterTree
    ) -> None:
        unit = unit_for("BoxTurtle (4-Lane)")
        assert unit is not None
        sel = UnitSelection(unit_name="Mine", turtle_renamed=True)
        assert additional_rows(unit, make_ctx(printer_tree), sel) == [
            "BoxTurtle Name : Mine"
        ]
        assert sel.unit_name == "Mine"

    def test_additional_row_resets_an_unchosen_name_to_the_next_free_default(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        ctx.afc_config_dir.mkdir(parents=True)
        (ctx.afc_config_dir / "AFC_Turtle_1.cfg").write_text("")
        unit = unit_for("BoxTurtle (4-Lane)")
        assert unit is not None
        sel = UnitSelection(unit_name="Foo_1", turtle_renamed=False)
        rows = additional_rows(unit, ctx, sel)
        assert rows == ["BoxTurtle Name : Turtle_2"]
        assert sel.unit_name == "Turtle_2"


class TestAdditionalDefaultName:
    def sel(self) -> UnitSelection:
        return UnitSelection(installation_type="BoxTurtle (4-Lane)")

    def test_starts_at_one_when_nothing_exists(self, printer_tree: PrinterTree) -> None:
        assert additional_default_name(make_ctx(printer_tree), self.sel()) == "Turtle_1"

    @pytest.mark.parametrize(
        "existing, expected",
        [
            (["AFC_Turtle_1.cfg"], "Turtle_2"),
            (["AFC_Turtle_1.cfg", "AFC_Turtle_2.cfg"], "Turtle_3"),
            (["AFC_Turtle_2.cfg"], "Turtle_1"),
            (["AFC_Turtle_1.cfg", "AFC_Turtle_3.cfg"], "Turtle_2"),
        ],
    )
    def test_picks_the_lowest_free_number(
        self, printer_tree: PrinterTree, existing: List[str], expected: str
    ) -> None:
        ctx = make_ctx(printer_tree)
        ctx.afc_config_dir.mkdir(parents=True)
        for name in existing:
            (ctx.afc_config_dir / name).write_text("")
        assert additional_default_name(ctx, self.sel()) == expected

    def test_a_directory_with_that_name_does_not_count_as_taken(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        (ctx.afc_config_dir / "AFC_Turtle_1.cfg").mkdir(parents=True)
        assert additional_default_name(ctx, self.sel()) == "Turtle_1"

    def test_an_unknown_type_falls_back_to_unit_n(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(installation_type="Nope")
        assert additional_default_name(make_ctx(printer_tree), sel) == "Unit_1"


class TestRegistry:
    def test_boxturtle_types_lead_the_menu_order(self) -> None:
        assert [u.display_name for u in UNIT_TYPES][:2] == [
            "BoxTurtle (4-Lane)",
            "BoxTurtle (8-Lane)",
        ]

    def test_keys_are_unique_and_identifier_safe(self) -> None:
        keys = [u.key for u in UNIT_TYPES]
        assert len(keys) == len(set(keys))
        assert all(key.isidentifier() for key in keys)

    def test_unknown_types_return_none(self) -> None:
        assert unit_for("NoSuchType") is None
        assert unit_for("") is None

    def test_a_new_unit_type_needs_only_its_own_class_and_a_list_entry(
        self, printer_tree: PrinterTree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Dummy(UnitType):
            display_name = "Dummy"
            key = "Dummy"
            additional_name_prefix = "Dummy"
            additional_buffer_supported = True
            additional_buffer_options = ("None", "Special")
            additional_buffer_default = "Special"

            def copy_files(self, ctx, console, prompter, sel):  # type: ignore[no-untyped-def]
                (ctx.afc_config_dir / "dummy.cfg").write_text("dummy")

        monkeypatch.setattr(registry, "UNIT_TYPES", [*registry.UNIT_TYPES, Dummy()])
        unit = unit_for("Dummy")
        assert unit is not None
        ctx = make_ctx(printer_tree)
        ctx.afc_config_dir.mkdir(parents=True)
        sel = UnitSelection(installation_type="Dummy")
        unit.copy_files(ctx, RecordingConsole(), ScriptedPrompter([]), sel)
        assert (ctx.afc_config_dir / "dummy.cfg").read_text() == "dummy"
        # every optional hook defaults to "nothing to do"
        unit.install_additional(ctx, RecordingConsole(), ScriptedPrompter([]), sel)
        assert unit.buffer_target(ctx, sel) is None
        assert unit.message(ctx, sel) == ""
        assert unit.install_options() == []
        assert unit.additional_options() == []
        # per-type buffer settings are honored, not replaced by the generic ones
        assert additional_buffer_supported("Dummy") is True
        assert additional_buffer_options("Dummy") == ("None", "Special")
        assert additional_buffer_default("Dummy") == "Special"
        assert additional_default_name(ctx, sel) == "Dummy_1"


class TestAdditionalBufferRules:
    def test_boxturtle_can_have_its_own_buffer_with_the_full_cycle(self) -> None:
        assert additional_buffer_supported("BoxTurtle (4-Lane)") is True
        assert additional_buffer_options("BoxTurtle (4-Lane)") == (
            "None",
            "TurtleNeck",
            "TurtleNeckV2",
            "TurtleNeck2Remastered",
            "FPS_PSF",
        )

    def test_unknown_types_are_unsupported_but_get_the_generic_cycle(self) -> None:
        assert additional_buffer_supported("Nope") is False
        assert additional_buffer_options("Nope") == (
            "None",
            "TurtleNeck",
            "TurtleNeckV2",
            "TurtleNeck2Remastered",
            "FPS_PSF",
        )
