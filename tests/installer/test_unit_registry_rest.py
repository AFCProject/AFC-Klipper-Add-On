from pathlib import Path
from typing import Dict, List

import pytest

from installer.platform_core.context import Context
from installer.unit_registry.base import BufferTarget
from installer.unit_registry.naming import lowest_free_name
from installer.unit_registry.registry import (
    additional_buffer_default,
    additional_buffer_options,
    additional_buffer_supported,
    force_additional_buffer_default,
    unit_for,
)
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import PrinterTree, RecordingConsole, ScriptedPrompter
from tests.installer.unit_driver import FIXTURES
from tests.installer.unit_rows import additional_rows, install_rows

QUATTRO = "QuattroBox"
OPENAMS = "OpenAMS"
VIVID = "ViViD"
ACE = "ACE"


def make_ctx(tree: PrinterTree) -> Context:
    return Context.create(tree.afc_path, home=tree.home)


def install(tree: PrinterTree, sel: UnitSelection, additional: bool) -> Context:
    ctx = make_ctx(tree)
    unit = unit_for(sel.installation_type)
    assert unit is not None
    (ctx.afc_config_dir / "mcu").mkdir(parents=True, exist_ok=True)
    (ctx.afc_config_dir / "macros").mkdir(exist_ok=True)
    action = unit.install_additional if additional else unit.copy_files
    action(ctx, RecordingConsole(), ScriptedPrompter([]), sel)
    return ctx


def files(ctx: Context) -> List[str]:
    return sorted(
        str(p.relative_to(ctx.afc_config_dir))
        for p in ctx.afc_config_dir.rglob("*")
        if p.is_file()
    )


def text(ctx: Context, relative: str) -> str:
    return (ctx.afc_config_dir / relative).read_text()


def src(tree: PrinterTree, relative: str) -> bytes:
    return (tree.afc_path / relative).read_bytes()


class TestQuattroBox:
    @pytest.mark.parametrize("motor, template", [("NEMA_14", "14"), ("NEMA_17", "17")])
    @pytest.mark.parametrize("board", ["MMB_1.0", "MMB_1.1", "MMB_2.0"])
    def test_first_install_picks_the_template_by_motor_and_the_mcu_file_by_board(
        self, printer_tree: PrinterTree, motor: str, template: str, board: str
    ) -> None:
        sel = UnitSelection(installation_type=QUATTRO, qb_motor_type=motor, qb_board_type=board)
        ctx = install(printer_tree, sel, additional=False)
        assert files(ctx) == sorted(
            [
                "AFC_Hardware.cfg",
                "AFC_QuattroBox_1.cfg",
                "macros/Eject_buttons.cfg",
                f"mcu/{board}_QB.cfg",
            ]
        )
        unit_text = text(ctx, "AFC_QuattroBox_1.cfg")
        expected = src(printer_tree, f"templates/AFC_QuattroBox_{template}.cfg").decode()
        wanted = expected.replace("include mcu/MMB_QB.cfg", f"include mcu/{board}_QB.cfg")
        assert unit_text == wanted
        assert (ctx.afc_config_dir / f"mcu/{board}_QB.cfg").read_bytes() == src(
            printer_tree, f"config/mcu/{board}_QB.cfg"
        )

    def test_first_install_uses_the_quattrobox_hardware_and_the_eject_macros_unchanged(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = install(printer_tree, UnitSelection(installation_type=QUATTRO), additional=False)
        assert (ctx.afc_config_dir / "AFC_Hardware.cfg").read_bytes() == src(
            printer_tree, "templates/AFC_Hardware-QuattroBox.cfg"
        )
        assert (ctx.afc_config_dir / "macros/Eject_buttons.cfg").read_bytes() == src(
            printer_tree, "templates/qb_macros/Eject_buttons.cfg"
        )

    def test_an_unknown_motor_or_board_copies_only_what_it_can(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=QUATTRO, qb_motor_type="NEMA_23")
        ctx = install(printer_tree, sel, additional=False)
        assert files(ctx) == ["AFC_Hardware.cfg", "macros/Eject_buttons.cfg"]
        sel = UnitSelection(installation_type=QUATTRO, qb_board_type="MMB_9.9")
        ctx = install(printer_tree, sel, additional=False)
        assert "AFC_QuattroBox_1.cfg" in files(ctx)
        assert not any(name.startswith("mcu/") for name in files(ctx))
        assert "include mcu/MMB_QB.cfg" in text(ctx, "AFC_QuattroBox_1.cfg")

    def test_additional_unit_renames_the_unit_the_macros_and_the_mcu_file(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=QUATTRO, unit_name="QB_2", qb_board_type="MMB_1.1")
        ctx = install(printer_tree, sel, additional=True)
        assert files(ctx) == sorted(
            ["AFC_QB_2.cfg", "macros/Eject_buttons.cfg", "mcu/MMB_1.1_QB_QB_2.cfg"]
        )
        unit_text = text(ctx, "AFC_QB_2.cfg")
        assert "[include mcu/MMB_1.1_QB_QB_2.cfg]" in unit_text
        assert "QuattroBox_1" not in unit_text and "[mcu QB_2]" in unit_text
        mcu = text(ctx, "mcu/MMB_1.1_QB_QB_2.cfg")
        assert "mcu: QB_2" in mcu and "mcu: QuattroBox_1" not in mcu
        macros = text(ctx, "macros/Eject_buttons.cfg")
        assert "QuattroBox_1" not in macros and "QB_2" in macros

    def test_an_additional_unit_with_an_unknown_motor_only_gets_the_macros(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=QUATTRO, unit_name="QB_2", qb_motor_type="NEMA_23")
        ctx = install(printer_tree, sel, additional=True)
        assert files(ctx) == ["macros/Eject_buttons.cfg"]
        assert "QuattroBox_1" not in text(ctx, "macros/Eject_buttons.cfg")

    def test_an_additional_unit_with_an_unknown_board_has_a_renamed_unit_but_no_mcu_file(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=QUATTRO, unit_name="QB_2", qb_board_type="MMB_9.9")
        ctx = install(printer_tree, sel, additional=True)
        assert files(ctx) == sorted(["AFC_QB_2.cfg", "macros/Eject_buttons.cfg"])
        unit_text = text(ctx, "AFC_QB_2.cfg")
        assert "QuattroBox_1" not in unit_text
        assert "include mcu/MMB_QB.cfg" in unit_text  # no board, so no include rewrite

    def test_buffer_target_for_the_first_unit(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(QUATTRO)
        assert unit is not None
        target = unit.buffer_target(ctx, UnitSelection(installation_type=QUATTRO, unit_name="x"))
        assert target == BufferTarget(
            "QuattroBox_1", "AFC_QuattroBox", ctx.afc_config_dir / "AFC_QuattroBox_1.cfg",
            "QuattroBox_1", "[AFC_buffer QuattroBox_1]",
        )

    def test_buffer_target_for_an_additional_unit(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(QUATTRO)
        assert unit is not None
        sel = UnitSelection(installation_type=QUATTRO, unit_name="QB_2", is_additional_unit=True)
        assert unit.buffer_target(ctx, sel) == BufferTarget(
            "QB_2", "AFC_QuattroBox", ctx.afc_config_dir / "AFC_QB_2.cfg", "QB_2",
            "[AFC_buffer QB_2]",
        )

    def test_rows_and_message(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(QUATTRO)
        assert unit is not None
        sel = UnitSelection(
            installation_type=QUATTRO, qb_board_type="MMB_1.0", qb_motor_type="NEMA_17"
        )
        rows = [
            "QuattroBox Board Type : MMB_1.0",
            "QuattroBox Motor Type : NEMA_17",
        ]
        assert install_rows(unit, sel) == rows
        assert additional_rows(unit, ctx, sel) == [
            "QuattroBox Name : QuattroBox_1", *rows
        ]
        unit_file = f"{ctx.afc_config_dir}/AFC_QuattroBox_1.cfg"
        assert unit.message(ctx, sel) == (
            f"\n- You must update the {unit_file} file to reference the proper buffer "
            "configuration and pins.\n\n"
            f"- Ensure you enter either your CAN bus or serial information in the {unit_file} "
            "file\n  "
        )


class TestOpenAms:
    def test_first_install_copies_the_hardware_and_ams_files(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = install(printer_tree, UnitSelection(installation_type=OPENAMS), additional=False)
        assert files(ctx) == ["AFC_AMS_1.cfg", "AFC_Hardware.cfg"]
        assert (ctx.afc_config_dir / "AFC_AMS_1.cfg").read_bytes() == src(
            printer_tree, "templates/AFC_AMS_1.cfg"
        )

    def test_additional_unit_renames_ams_1(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(installation_type=OPENAMS, unit_name="AMS_2")
        ctx = install(printer_tree, sel, additional=True)
        assert files(ctx) == ["AFC_AMS_2.cfg"]
        unit_text = text(ctx, "AFC_AMS_2.cfg")
        assert "AMS_1" not in unit_text and "[AFC_OpenAMS AMS_2]" in unit_text

    def test_buffer_targets(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(OPENAMS)
        assert unit is not None
        first = unit.buffer_target(ctx, UnitSelection(installation_type=OPENAMS, unit_name="z"))
        assert first == BufferTarget(
            "AMS_1", "AFC_OpenAMS", ctx.afc_config_dir / "AFC_AMS_1.cfg", "AMS_1"
        )
        extra = unit.buffer_target(
            ctx,
            UnitSelection(installation_type=OPENAMS, unit_name="AMS_2", is_additional_unit=True),
        )
        assert extra == BufferTarget(
            "AMS_2", "AFC_OpenAMS", ctx.afc_config_dir / "AFC_AMS_2.cfg", "AMS_2"
        )

    def test_rows_message_and_default_name(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(OPENAMS)
        assert unit is not None
        sel = UnitSelection(installation_type=OPENAMS, unit_name="x")
        assert install_rows(unit, sel) == []
        assert additional_rows(unit, ctx, sel) == ["OpenAMS Name : AMS_1"]
        assert unit.message(ctx, sel) == (
            f"\n- Review and update the {ctx.afc_config_dir}/AFC_AMS_1.cfg file for your AMS "
            "unit settings.\n\n"
            "- Ensure OpenAMS is properly installed and configured per their instructions.\n  "
        )


class TestAceAdditionalFileStem:
    def test_unrenamed_default_name_is_upper_cased(self) -> None:
        unit = unit_for(ACE)
        assert unit is not None
        v1 = UnitSelection(installation_type=ACE, ace_unit_type="V1", turtle_renamed=False)
        assert unit._additional_file_stem(v1, "Ace_1") == "ACE_1"
        v2 = UnitSelection(installation_type=ACE, ace_unit_type="V2_Pro", turtle_renamed=False)
        assert unit._additional_file_stem(v2, "Ace2_1") == "ACE2_1"

    def test_renamed_name_is_left_exactly_as_chosen_even_if_it_looks_like_a_default(
        self,
    ) -> None:
        unit = unit_for(ACE)
        assert unit is not None
        sel = UnitSelection(installation_type=ACE, ace_unit_type="V1", turtle_renamed=True)
        assert unit._additional_file_stem(sel, "Ace_2") == "Ace_2"

    def test_unrenamed_name_not_matching_the_prefix_is_left_unchanged(self) -> None:
        unit = unit_for(ACE)
        assert unit is not None
        sel = UnitSelection(installation_type=ACE, ace_unit_type="V2_Pro", turtle_renamed=False)
        assert unit._additional_file_stem(sel, "CustomName2") == "CustomName2"


class TestAce:
    def test_first_install_v1_copies_the_hardware_and_v1_files(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=ACE, ace_unit_type="V1")
        ctx = install(printer_tree, sel, additional=False)
        assert files(ctx) == ["AFC_ACE_1.cfg", "AFC_Hardware.cfg"]
        assert (ctx.afc_config_dir / "AFC_ACE_1.cfg").read_bytes() == src(
            printer_tree, "templates/AFC_ACE_1.cfg"
        )

    def test_first_install_v2_pro_copies_the_hardware_and_v2_pro_files(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=ACE, ace_unit_type="V2_Pro")
        ctx = install(printer_tree, sel, additional=False)
        assert files(ctx) == ["AFC_ACE2_1.cfg", "AFC_Hardware.cfg"]
        assert (ctx.afc_config_dir / "AFC_ACE2_1.cfg").read_bytes() == src(
            printer_tree, "templates/AFC_ACE2_1.cfg"
        )

    def test_additional_v1_renamed_unit_keeps_its_exact_chosen_name_and_file(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(
            installation_type=ACE, ace_unit_type="V1", unit_name="Ace_2", turtle_renamed=True
        )
        ctx = install(printer_tree, sel, additional=True)
        assert files(ctx) == ["AFC_Ace_2.cfg"]
        unit_text = text(ctx, "AFC_Ace_2.cfg")
        assert "Ace_1" not in unit_text and "[AFC_ACE Ace_2]" in unit_text

    def test_additional_v2_pro_renamed_unit_keeps_its_exact_chosen_name_and_file(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(
            installation_type=ACE, ace_unit_type="V2_Pro", unit_name="CustomName",
            turtle_renamed=True,
        )
        ctx = install(printer_tree, sel, additional=True)
        assert files(ctx) == ["AFC_CustomName.cfg"]
        unit_text = text(ctx, "AFC_CustomName.cfg")
        assert "Ace2_1" not in unit_text and "[AFC_ACE2 CustomName]" in unit_text

    def test_additional_v2_pro_unrenamed_default_gets_an_uppercase_file_but_mixed_case_content(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(
            installation_type=ACE, ace_unit_type="V2_Pro", unit_name="Ace2_1",
            turtle_renamed=False,
        )
        ctx = install(printer_tree, sel, additional=True)
        assert files(ctx) == ["AFC_ACE2_1.cfg"]
        unit_text = text(ctx, "AFC_ACE2_1.cfg")
        assert "[AFC_ACE2 Ace2_1]" in unit_text
        assert "ACE2_1" not in unit_text

    def test_buffer_targets_match_the_actual_copied_file_names(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(ACE)
        assert unit is not None
        v1 = unit.buffer_target(ctx, UnitSelection(installation_type=ACE, ace_unit_type="V1"))
        assert v1 == BufferTarget(
            "Ace_1", "AFC_ACE", ctx.afc_config_dir / "AFC_ACE_1.cfg", "Ace_1"
        )
        v2 = unit.buffer_target(
            ctx, UnitSelection(installation_type=ACE, ace_unit_type="V2_Pro")
        )
        assert v2 == BufferTarget(
            "Ace2_1", "AFC_ACE2", ctx.afc_config_dir / "AFC_ACE2_1.cfg", "Ace2_1"
        )
        additional = unit.buffer_target(
            ctx,
            UnitSelection(
                installation_type=ACE, ace_unit_type="V2_Pro", unit_name="Foo",
                is_additional_unit=True, turtle_renamed=False,
            ),
        )
        assert additional == BufferTarget(
            "Foo", "AFC_ACE2", ctx.afc_config_dir / "AFC_Foo.cfg", "Foo"
        )
        additional_unrenamed_default = unit.buffer_target(
            ctx,
            UnitSelection(
                installation_type=ACE, ace_unit_type="V2_Pro", unit_name="Ace2_2",
                is_additional_unit=True, turtle_renamed=False,
            ),
        )
        assert additional_unrenamed_default == BufferTarget(
            "Ace2_2", "AFC_ACE2", ctx.afc_config_dir / "AFC_ACE2_2.cfg", "Ace2_2"
        )
        additional_renamed_to_a_default_looking_name = unit.buffer_target(
            ctx,
            UnitSelection(
                installation_type=ACE, ace_unit_type="V2_Pro", unit_name="Ace2_3",
                is_additional_unit=True, turtle_renamed=True,
            ),
        )
        assert additional_renamed_to_a_default_looking_name == BufferTarget(
            "Ace2_3", "AFC_ACE2", ctx.afc_config_dir / "AFC_Ace2_3.cfg", "Ace2_3"
        )

    def test_rows_and_message_differ_by_version(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(ACE)
        assert unit is not None
        v1 = UnitSelection(installation_type=ACE, ace_unit_type="V1", unit_name="x")
        v2 = UnitSelection(installation_type=ACE, ace_unit_type="V2_Pro", unit_name="x")
        assert install_rows(unit, v1) == ["ACE Unit Type : V1"]
        assert install_rows(unit, v2) == ["ACE Unit Type : V2_Pro"]
        assert additional_rows(unit, ctx, v1) == [
            "ACE Name : Ace_1", "ACE Unit Type : V1"
        ]
        assert additional_rows(unit, ctx, v2) == [
            "ACE Name : Ace2_1", "ACE Unit Type : V2_Pro"
        ]
        assert "AFC_ACE2_rfid" in unit.message(ctx, v2)
        assert "AFC_ACE2_rfid" not in unit.message(ctx, v1)

    def test_on_select_forces_sensor_and_internal_buffer_and_fixes_the_name(self) -> None:
        unit = unit_for(ACE)
        assert unit is not None
        sel = UnitSelection(
            installation_type=ACE, ace_unit_type="V2_Pro",
            toolhead_sensor="Ramming", buffer_type="Unknown", unit_name="Ace_1",
        )
        unit.on_select(sel)
        assert sel.toolhead_sensor == "Sensor"
        assert sel.buffer_type == "Internal"
        assert sel.unit_name == "Ace2_1"

    def test_cycle_buffer_order_wraps_without_returning_to_ramming(self) -> None:
        unit = unit_for(ACE)
        assert unit is not None
        sel = UnitSelection(installation_type=ACE, buffer_type="Internal")
        seen = [sel.buffer_type]
        for _ in range(6):
            unit.cycle_buffer(sel)
            seen.append(sel.buffer_type)
        assert seen == [
            "Internal", "TurtleNeck", "TurtleNeckV2", "TurtleNeck2Remastered",
            "FPS_PSF", "None", "Internal",
        ]

    def test_cycle_buffer_from_an_unknown_value_restarts_at_internal(self) -> None:
        unit = unit_for(ACE)
        assert unit is not None
        sel = UnitSelection(installation_type=ACE, buffer_type="Unknown")
        unit.cycle_buffer(sel)
        assert sel.buffer_type == "Internal"

    def test_name_prefix_follows_the_selected_version(self) -> None:
        unit = unit_for(ACE)
        assert unit is not None
        assert unit.name_prefix(UnitSelection(ace_unit_type="V1")) == "Ace"
        assert unit.name_prefix(UnitSelection(ace_unit_type="V2_Pro")) == "Ace2"

    def test_additional_probe_file_matches_the_upper_cased_default_file(self) -> None:
        unit = unit_for(ACE)
        assert unit is not None
        sel = UnitSelection(installation_type=ACE)
        assert unit.additional_probe_file("Ace", 1, sel) == "AFC_ACE_1.cfg"
        assert unit.additional_probe_file("Ace2", 3, sel) == "AFC_ACE2_3.cfg"

    def test_lowest_free_name_skips_an_already_installed_default(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=ACE, ace_unit_type="V1")
        ctx = install(printer_tree, sel, additional=False)
        assert (ctx.afc_config_dir / "AFC_ACE_1.cfg").is_file()
        assert lowest_free_name(ctx, sel, unit_for(ACE)) == "Ace_2"

    def test_additional_buffer_options_include_internal(self) -> None:
        assert additional_buffer_supported(ACE) is True
        assert additional_buffer_options(ACE) == (
            "None", "Internal", "TurtleNeck", "TurtleNeckV2", "TurtleNeck2Remastered", "FPS_PSF"
        )
        assert additional_buffer_default(ACE) == "Internal"

    def test_switching_in_always_forces_the_default_buffer(self) -> None:
        # TurtleNeck is also a valid ACE option, so this only passes because ACE
        # forces its own default rather than carrying a compatible buffer over.
        assert force_additional_buffer_default(ACE) is True


class TestVivid:
    def test_first_install_uses_fixed_names_whatever_the_unit_name(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=VIVID, unit_name="Ignored")
        ctx = install(printer_tree, sel, additional=False)
        assert files(ctx) == ["AFC_Hardware.cfg", "AFC_Vivid_1.cfg", "mcu/Vivid_1.cfg"]
        assert (ctx.afc_config_dir / "mcu/Vivid_1.cfg").read_bytes() == src(
            printer_tree, "config/mcu/Vivid.cfg"
        )

    def test_additional_unit_gets_a_same_named_mcu_file_and_both_are_renamed(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=VIVID, unit_name="Vivid_2")
        ctx = install(printer_tree, sel, additional=True)
        assert files(ctx) == ["AFC_Vivid_2.cfg", "mcu/Vivid_2.cfg"]
        assert "[include mcu/Vivid_2.cfg]" in text(ctx, "AFC_Vivid_2.cfg")
        assert "Vivid_1" not in text(ctx, "AFC_Vivid_2.cfg")
        assert "Vivid_1" not in text(ctx, "mcu/Vivid_2.cfg")

    def test_buffer_targets(self, printer_tree: PrinterTree) -> None:
        """ViViD's target exists only for extruder wiring; it has no buffer choice
        (on_select always forces buffer_type to "None"), so a PSF buffer is still
        never applied even though the target is no longer None."""
        ctx = make_ctx(printer_tree)
        unit = unit_for(VIVID)
        assert unit is not None
        first = unit.buffer_target(ctx, UnitSelection(installation_type=VIVID, unit_name="z"))
        assert first == BufferTarget(
            "Vivid_1", "AFC_vivid", ctx.afc_config_dir / "AFC_Vivid_1.cfg", "Vivid_1"
        )
        extra = unit.buffer_target(
            ctx,
            UnitSelection(installation_type=VIVID, unit_name="Vivid_2", is_additional_unit=True),
        )
        assert extra == BufferTarget(
            "Vivid_2", "AFC_vivid", ctx.afc_config_dir / "AFC_Vivid_2.cfg", "Vivid_2"
        )

    def test_rows_and_message(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(VIVID)
        assert unit is not None
        sel = UnitSelection(installation_type=VIVID, unit_name="x")
        assert install_rows(unit, sel) == []
        assert additional_rows(unit, ctx, sel) == ["ViViD Name : Vivid_1"]
        unit_file = f"{ctx.afc_config_dir}/AFC_Vivid_1.cfg"
        assert unit.message(ctx, sel) == (
            f"\n- Ensure you enter your serial information in the {unit_file} file\n\n"
            f"- Review the {unit_file} file to reference the proper buffer configuration "
            "and pins.\n  "
        )


class TestAdditionalBufferRulesForTheRest:
    def test_openams_offers_only_none_and_fps_psf_and_defaults_to_fps_psf(self) -> None:
        assert additional_buffer_supported(OPENAMS) is True
        assert additional_buffer_options(OPENAMS) == ("None", "FPS_PSF")
        assert additional_buffer_default(OPENAMS) == "FPS_PSF"

    def test_quattrobox_offers_the_full_cycle(self) -> None:
        assert additional_buffer_supported(QUATTRO) is True
        assert additional_buffer_options(QUATTRO) == (
            "None", "TurtleNeck", "TurtleNeckV2", "TurtleNeck2Remastered", "FPS_PSF"
        )

    def test_vivid_has_no_additional_unit_buffer(self) -> None:
        assert additional_buffer_supported(VIVID) is False


class TestMessagesAreInsideTheRecordedInstallMessages:
    @pytest.mark.parametrize(
        "fixture, unit_name, overrides",
        [
            ("QuattroBox__qb_board_type-MMB_1.0__qb_motor_type-NEMA_14", QUATTRO, {}),
            ("QuattroBox__qb_board_type-MMB_2.0__qb_motor_type-NEMA_17", QUATTRO, {}),
            ("OpenAMS", OPENAMS, {}),
            ("ViViD", VIVID, {}),
        ],
    )
    def test_the_unit_message_appears_verbatim_in_the_recorded_message_fixture(
        self, fixture: str, unit_name: str, overrides: Dict[str, str]
    ) -> None:
        fixture_text = (FIXTURES / f"message__{fixture}__buffer-None.txt").read_text()
        ctx = Context.create(Path("/repo"), home=Path("/h"), printer_config_dir=Path("<WORKDIR>"))
        unit = unit_for(unit_name)
        assert unit is not None
        sel = UnitSelection(installation_type=unit_name, unit_name="Turtle_1")
        assert unit.message(ctx, sel).strip() in fixture_text
