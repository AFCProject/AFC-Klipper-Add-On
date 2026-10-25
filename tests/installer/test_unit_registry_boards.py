from pathlib import Path
from typing import List

import pytest

from installer.platform_core.context import Context
from installer.unit_registry.base import BufferTarget
from installer.unit_registry.registry import (
    UNIT_TYPES,
    additional_buffer_supported,
    additional_default_name,
    unit_for,
)
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import PrinterTree, RecordingConsole, ScriptedPrompter
from tests.installer.unit_driver import FIXTURES
from tests.installer.unit_rows import additional_rows, install_rows

NIGHTOWL = "NightOwl"
HTLF = "HTLF"
CLAYMORE = "Claymore"


def make_ctx(tree: PrinterTree) -> Context:
    return Context.create(tree.afc_path, home=tree.home)


def install(
    tree: PrinterTree, sel: UnitSelection, additional: bool, answers: List[str] = []
) -> Context:
    ctx = make_ctx(tree)
    unit = unit_for(sel.installation_type)
    assert unit is not None
    (ctx.afc_config_dir / "mcu").mkdir(parents=True, exist_ok=True)
    (ctx.afc_config_dir / "macros").mkdir(exist_ok=True)
    action = unit.install_additional if additional else unit.copy_files
    action(ctx, RecordingConsole(), ScriptedPrompter(list(answers)), sel)
    return ctx


def read(tree: PrinterTree, relative: str) -> str:
    return (make_ctx(tree).afc_config_dir / relative).read_text()


def source(tree: PrinterTree, relative: str) -> bytes:
    return (tree.afc_path / relative).read_bytes()


def files(ctx: Context) -> List[str]:
    return sorted(
        str(p.relative_to(ctx.afc_config_dir))
        for p in ctx.afc_config_dir.rglob("*")
        if p.is_file()
    )


class TestRegistryOrder:
    def test_the_board_aware_types_follow_boxturtle_in_menu_order(self) -> None:
        names = [u.display_name for u in UNIT_TYPES]
        assert names[:5] == [
            "BoxTurtle (4-Lane)",
            "BoxTurtle (8-Lane)",
            NIGHTOWL,
            HTLF,
            CLAYMORE,
        ]

    @pytest.mark.parametrize("name", [NIGHTOWL, HTLF, CLAYMORE])
    def test_all_three_can_have_their_own_additional_buffer(self, name: str) -> None:
        assert additional_buffer_supported(name) is True


class TestNightOwl:
    @pytest.mark.parametrize(
        "board, template",
        [("ERB_2.0", "AFC_NightOwl_1.cfg"), ("OWLFC_Mini", "AFC_NightOwl_1-OWLFC_Mini.cfg")],
    )
    def test_first_install_copies_the_board_files_to_fixed_names(
        self, printer_tree: PrinterTree, board: str, template: str
    ) -> None:
        sel = UnitSelection(installation_type=NIGHTOWL, nightowl_board_type=board, unit_name="Zed")
        ctx = install(printer_tree, sel, additional=False)
        assert files(ctx) == ["AFC_Hardware.cfg", "AFC_NightOwl_1.cfg", f"mcu/{board}.cfg"]
        assert (ctx.afc_config_dir / "AFC_NightOwl_1.cfg").read_bytes() == source(
            printer_tree, f"templates/{template}"
        )
        assert (ctx.afc_config_dir / "AFC_Hardware.cfg").read_bytes() == source(
            printer_tree, "templates/AFC_Hardware-NightOwl.cfg"
        )

    def test_the_crlf_mcu_file_is_copied_byte_for_byte(self, printer_tree: PrinterTree) -> None:
        source_bytes = source(printer_tree, "config/mcu/ERB_2.0.cfg")
        assert b"\r\n" in source_bytes  # the shipped file really is CRLF
        ctx = install(printer_tree, UnitSelection(installation_type=NIGHTOWL), additional=False)
        assert (ctx.afc_config_dir / "mcu" / "ERB_2.0.cfg").read_bytes() == source_bytes

    def test_additional_unit_gets_its_own_board_specific_mcu_file(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=NIGHTOWL, unit_name="Owl_B")
        ctx = install(printer_tree, sel, additional=True)
        assert files(ctx) == ["AFC_Owl_B.cfg", "mcu/ERB_2.0_Owl_B.cfg"]
        unit_text = read(printer_tree, "AFC_Owl_B.cfg")
        assert "[include mcu/ERB_2.0_Owl_B.cfg]" in unit_text
        assert "[include mcu/ERB_2.0.cfg]" not in unit_text

    def test_the_class_name_survives_the_unit_rename(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(installation_type=NIGHTOWL, unit_name="Owl_B")
        install(printer_tree, sel, additional=True)
        text = read(printer_tree, "AFC_Owl_B.cfg")
        assert "[AFC_NightOwl Owl_B]" in text
        assert "AFC_Owl_B" not in text
        assert "NightOwl_1" not in text

    def test_the_mcu_file_has_the_unit_name_substituted(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(installation_type=NIGHTOWL, unit_name="Owl_B")
        install(printer_tree, sel, additional=True)
        original = source(printer_tree, "config/mcu/ERB_2.0.cfg")
        mcu = (make_ctx(printer_tree).afc_config_dir / "mcu" / "ERB_2.0_Owl_B.cfg").read_bytes()
        assert b"NightOwl" in original and b"NightOwl" not in mcu
        assert mcu == original.replace(b"NightOwl", b"Owl_B")

    def test_buffer_target_for_the_first_unit_is_the_fixed_nightowl_section(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(NIGHTOWL)
        assert unit is not None
        target = unit.buffer_target(ctx, UnitSelection(installation_type=NIGHTOWL, unit_name="X"))
        assert target == BufferTarget(
            "NightOwl", "AFC_NightOwl", ctx.afc_config_dir / "AFC_NightOwl_1.cfg", "NightOwl"
        )

    def test_buffer_target_for_an_additional_unit_uses_its_name(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(NIGHTOWL)
        assert unit is not None
        sel = UnitSelection(installation_type=NIGHTOWL, unit_name="Owl_B", is_additional_unit=True)
        assert unit.buffer_target(ctx, sel) == BufferTarget(
            "Owl_B", "AFC_NightOwl", ctx.afc_config_dir / "AFC_Owl_B.cfg", "Owl_B"
        )

    def test_rows_show_the_board_and_the_additional_row_resets_the_name(
        self, printer_tree: PrinterTree
    ) -> None:
        unit = unit_for(NIGHTOWL)
        assert unit is not None
        sel = UnitSelection(
            installation_type=NIGHTOWL, unit_name="Foo", nightowl_board_type="OWLFC_Mini"
        )
        assert install_rows(unit, sel) == ["NightOwl Board Type : OWLFC_Mini"]
        rows = additional_rows(unit, make_ctx(printer_tree), sel)
        assert rows == [
            "NightOwl Name : NightOwl_1",
            "NightOwl Board Type : OWLFC_Mini",
        ]
        assert sel.unit_name == "NightOwl_1"


class TestHtlf:
    @pytest.mark.parametrize(
        "board, mcu, segment",
        [
            ("ERB", "HTLF_ERB.cfg", "ERB"),
            ("MMB_1.0", "HTLF_MMB_1.0.cfg", "MMB"),
            ("MMB_1.1", "HTLF_MMB_1.1.cfg", "MMB"),
        ],
    )
    def test_first_install_normalizes_mmb_boards_in_the_unit_file_name_only(
        self, printer_tree: PrinterTree, board: str, mcu: str, segment: str
    ) -> None:
        sel = UnitSelection(installation_type=HTLF, htlf_board_type=board, unit_name="H_1")
        ctx = install(printer_tree, sel, additional=False)
        assert files(ctx) == sorted(["AFC_Hardware.cfg", f"AFC_{segment}_H_1.cfg", f"mcu/{mcu}"])
        assert (ctx.afc_config_dir / f"AFC_{segment}_H_1.cfg").read_bytes() == source(
            printer_tree, f"templates/AFC_HTLF_1-{segment}.cfg"
        )
        assert (ctx.afc_config_dir / "AFC_Hardware.cfg").read_bytes() == source(
            printer_tree, "templates/AFC_Hardware-HTLF.cfg"
        )

    def test_additional_unit_renames_htlf_1_and_skips_the_hardware_file(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=HTLF, unit_name="Second")
        ctx = install(printer_tree, sel, additional=True)
        assert files(ctx) == ["AFC_ERB_Second.cfg", "mcu/HTLF_ERB.cfg"]
        text = read(printer_tree, "AFC_ERB_Second.cfg")
        assert "HTLF_1" not in text and "Second" in text

    def test_buffer_target_uses_the_normalized_board_in_the_file_name(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(HTLF)
        assert unit is not None
        first = unit.buffer_target(
            ctx, UnitSelection(installation_type=HTLF, htlf_board_type="MMB_1.1", unit_name="N")
        )
        assert first == BufferTarget(
            "HTLF_1", "AFC_HTLF", ctx.afc_config_dir / "AFC_MMB_N.cfg", "HTLF_1"
        )
        extra = unit.buffer_target(
            ctx,
            UnitSelection(
                installation_type=HTLF, htlf_board_type="MMB_1.1", unit_name="N",
                is_additional_unit=True,
            ),
        )
        assert extra == BufferTarget("N", "AFC_HTLF", ctx.afc_config_dir / "AFC_MMB_N.cfg", "N")

    def test_message_lists_three_follow_ups_for_the_board_named_file(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(HTLF)
        assert unit is not None
        text = unit.message(
            ctx, UnitSelection(installation_type=HTLF, htlf_board_type="MMB_1.0", unit_name="N")
        )
        unit_file = f"{ctx.afc_config_dir}/AFC_MMB_N.cfg"
        assert text == (
            f"\n- Ensure you enter either your CAN bus or serial information in the {unit_file} "
            "file.\n\n"
            f"- Ensure you modify the {unit_file} file to select the proper rotation distance\n"
            "  and gear ratio for your stepper motors.\n\n"
            f"- Ensure you update any necessary buffer information in the {unit_file} file\n  "
        )

    def test_rows_and_the_board_aware_default_name_probe(
        self, printer_tree: PrinterTree
    ) -> None:
        unit = unit_for(HTLF)
        assert unit is not None
        ctx = make_ctx(printer_tree)
        ctx.afc_config_dir.mkdir(parents=True)
        (ctx.afc_config_dir / "AFC_MMB_HTLF_1.cfg").write_text("")
        sel = UnitSelection(installation_type=HTLF, htlf_board_type="MMB_1.0", unit_name="x")
        assert install_rows(unit, sel) == [
            "HTLF Name : x",
            "HTLF Board Type : MMB_1.0",
        ]
        # the probe file carries the normalized board: AFC_MMB_HTLF_1.cfg is taken
        assert additional_default_name(ctx, sel) == "HTLF_2"
        assert additional_rows(unit, ctx, sel) == [
            "HTLF Name : HTLF_2",
            "HTLF Board Type : MMB_1.0",
        ]
        # a file for another board does not make HTLF_1 taken
        other = UnitSelection(installation_type=HTLF, htlf_board_type="ERB")
        assert additional_default_name(ctx, other) == "HTLF_1"


class TestClaymore:
    def test_first_install_with_the_default_name_becomes_claymore_1(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=CLAYMORE, unit_name="Turtle_1")
        ctx = install(printer_tree, sel, additional=False)
        assert sel.unit_name == "Claymore_1"
        assert files(ctx) == ["AFC_Claymore_1.cfg", "AFC_Hardware.cfg", "mcu/AFC_Lite_Claymore.cfg"]
        assert (ctx.afc_config_dir / "AFC_Claymore_1.cfg").read_bytes() == source(
            printer_tree, "templates/AFC_Claymore_1-AFC_Lite.cfg"
        )

    def test_first_install_honors_a_custom_name_inside_the_file_too(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=CLAYMORE, unit_name="Mine")
        install(printer_tree, sel, additional=False)
        assert sel.unit_name == "Mine"
        text = read(printer_tree, "AFC_Mine.cfg")
        assert "Claymore_1" not in text and "[AFC_Claymore Mine]" in text
        assert "Claymore_buffer" in text  # only the unit name is renamed, not the buffer

    def test_the_hardware_file_is_the_htlf_one(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(installation_type=CLAYMORE)
        ctx = install(printer_tree, sel, additional=False)
        assert (ctx.afc_config_dir / "AFC_Hardware.cfg").read_bytes() == source(
            printer_tree, "templates/AFC_Hardware-HTLF.cfg"
        )

    def test_additional_unit_also_renames_its_buffer_section(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=CLAYMORE, unit_name="Clay_2")
        ctx = install(printer_tree, sel, additional=True)
        assert files(ctx) == ["AFC_Clay_2.cfg", "mcu/AFC_Lite_Claymore.cfg"]
        text = read(printer_tree, "AFC_Clay_2.cfg")
        assert "Claymore_1" not in text and "Claymore_buffer" not in text
        assert "Clay_2_buffer" in text

    def test_buffer_target_for_the_first_unit_names_the_prebaked_buffer(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(CLAYMORE)
        assert unit is not None
        sel = UnitSelection(installation_type=CLAYMORE, unit_name="Mine")
        target = unit.buffer_target(ctx, sel)
        assert target == BufferTarget(
            "Mine", "AFC_Claymore", ctx.afc_config_dir / "AFC_Mine.cfg",
            "Claymore_buffer", "[AFC_buffer Claymore_buffer]",
        )

    def test_buffer_target_for_an_additional_unit_uses_the_renamed_buffer(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(CLAYMORE)
        assert unit is not None
        sel = UnitSelection(installation_type=CLAYMORE, unit_name="Clay_2", is_additional_unit=True)
        assert unit.buffer_target(ctx, sel) == BufferTarget(
            "Clay_2", "AFC_Claymore", ctx.afc_config_dir / "AFC_Clay_2.cfg",
            "Clay_2_buffer", "[AFC_buffer Clay_2_buffer]",
        )

    def test_rows_and_message(self, printer_tree: PrinterTree) -> None:
        unit = unit_for(CLAYMORE)
        assert unit is not None
        ctx = make_ctx(printer_tree)
        sel = UnitSelection(installation_type=CLAYMORE, unit_name="Mine")
        assert install_rows(unit, sel) == [
            "Claymore Name : Mine",
            "Claymore Board Type : AFC_Lite",
        ]
        unit_file = f"{ctx.afc_config_dir}/AFC_Mine.cfg"
        assert unit.message(ctx, sel) == (
            f"\n- Ensure you enter either your CAN bus or serial information in the {unit_file} "
            "file.\n\n"
            f"- Ensure you update any necessary buffer information in the {unit_file} file\n  "
        )
        rows = additional_rows(unit, ctx, sel)
        assert rows == [
            "Claymore Name : Claymore_1",
            "Claymore Board Type : AFC_Lite",
        ]


class TestMessagesAreInsideTheRecordedInstallMessages:
    """Each unit's own message must appear verbatim in the recorded install message."""

    @pytest.mark.parametrize(
        "fixture, unit_name, overrides",
        [
            ("NightOwl__nightowl_board_type-ERB_2.0", NIGHTOWL, {}),
            ("HTLF__htlf_board_type-ERB", HTLF, {"htlf_board_type": "ERB"}),
            ("HTLF__htlf_board_type-MMB_1.1", HTLF, {"htlf_board_type": "MMB_1.1"}),
            ("HTLF__htlf_board_type-MMB_1.0", HTLF, {"htlf_board_type": "MMB_1.0"}),
            ("Claymore__htlf2_board_type-AFC_Lite", CLAYMORE, {}),
        ],
    )
    def test_the_unit_message_appears_verbatim_in_the_recorded_message_fixture(
        self, fixture: str, unit_name: str, overrides: dict  # type: ignore[type-arg]
    ) -> None:
        text = (FIXTURES / f"message__{fixture}__buffer-None.txt").read_text()
        ctx = Context.create(Path("/repo"), home=Path("/h"), printer_config_dir=Path("<WORKDIR>"))
        unit = unit_for(unit_name)
        assert unit is not None
        sel = UnitSelection(installation_type=unit_name, unit_name="Turtle_1")
        for key, value in overrides.items():
            setattr(sel, key, value)
        # the recorded message case never runs copy_unit_files, so the name stays Turtle_1
        assert unit.message(ctx, sel).strip() in text
