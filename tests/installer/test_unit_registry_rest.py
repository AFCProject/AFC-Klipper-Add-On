from pathlib import Path
from typing import Dict, List

import pytest

from installer.platform_core.context import Context
from installer.unit_registry.base import BufferTarget
from installer.unit_registry.registry import (
    additional_buffer_default,
    additional_buffer_options,
    additional_buffer_supported,
    unit_for,
)
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import PrinterTree, RecordingConsole, ScriptedPrompter
from tests.installer.unit_driver import FIXTURES
from tests.installer.unit_rows import additional_rows, install_rows

QUATTRO = "QuattroBox"
OPENAMS = "OpenAMS"
VIVID = "ViViD"


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

    def test_has_no_buffer_target_so_a_psf_buffer_is_skipped(
        self, printer_tree: PrinterTree
    ) -> None:
        unit = unit_for(VIVID)
        assert unit is not None
        sel = UnitSelection(installation_type=VIVID)
        assert unit.buffer_target(make_ctx(printer_tree), sel) is None

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
