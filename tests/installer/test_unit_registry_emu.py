import re
from pathlib import Path
from typing import List

import pytest

from installer.platform_core.context import Context
from installer.unit_registry.base import BufferTarget
from installer.unit_registry.emu import TEMPLATE_DIR, generate_emu_config
from installer.unit_registry.registry import UNIT_TYPES, additional_buffer_supported, unit_for
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import PrinterTree, RecordingConsole, ScriptedPrompter
from tests.installer.unit_rows import additional_rows, install_rows

REPO_ROOT = Path(__file__).resolve().parents[2]
EMU = "EMU"


def make_ctx(tree: PrinterTree) -> Context:
    return Context.create(tree.afc_path, home=tree.home)


def generate(ctx: Context, name: str, lanes: int, board: str) -> None:
    generate_emu_config(ctx, name, lanes, board)


def read_pair(ctx: Context, name: str) -> List[str]:
    cfg = ctx.afc_config_dir
    return [(cfg / f"AFC_{name}.cfg").read_text(), (cfg / "mcu" / f"{name}.cfg").read_text()]


class TestTemplates:
    def test_all_eight_template_files_ship(self) -> None:
        names = sorted(p.name for p in TEMPLATE_DIR.iterdir())
        assert names == sorted(
            f"{n}.tmpl"
            for n in (
                "mcu_slb", "mcu_ebb", "header", "lane_mcu", "unit",
                "lane_slb", "lane_ebb", "buffer_hub",
            )
        )

    def test_only_the_three_known_placeholders_are_used_and_nothing_else_has_a_dollar(
        self,
    ) -> None:
        for path in TEMPLATE_DIR.iterdir():
            text = path.read_text()
            assert set(re.findall(r"\$\{(\w+)\}", text)) <= {"unit_name", "i", "mcu_list"}
            assert "$" not in re.sub(r"\$\{\w+\}", "", text), path.name

    def test_the_hub_comment_keeps_its_backticks(self) -> None:
        assert "`use_dist_hub: True`" in (TEMPLATE_DIR / "buffer_hub.tmpl").read_text()


class TestGenerateEmuConfig:
    def test_the_mcu_file_lists_every_lane_comma_separated(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        generate(ctx, "E_1", 3, "EBB")
        _unit, mcu = read_pair(ctx, "E_1")
        assert mcu.startswith(
            "[board_pins E_1]\nmcu: E_1_lane1, E_1_lane2, E_1_lane3\naliases:\n"
            "    MOT_UART=PA15,\n"
        )

    def test_the_unit_file_starts_with_the_include_then_one_mcu_section_per_lane(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        generate(ctx, "E_1", 2, "EBB")
        unit, _mcu = read_pair(ctx, "E_1")
        assert unit.startswith(
            "[include mcu/E_1.cfg]\n\n"
            "[mcu E_1_lane1]\ncanbus_uuid: <replace with your can UUID>\n"
        )
        assert unit.count("[mcu E_1_lane") == 2
        assert unit.count("[AFC_stepper lane") == 2
        assert unit.count("[AFC_EMU E_1]") == 1
        assert unit.count("[AFC_buffer E_1_buffer]") == 1
        assert unit.count("[AFC_hub E_1_HUB]") == 1

    @pytest.mark.parametrize("lanes", [1, 2, 3, 4, 5, 6, 7, 8])
    def test_the_lane_count_drives_every_per_lane_section(
        self, printer_tree: PrinterTree, lanes: int
    ) -> None:
        ctx = make_ctx(printer_tree)
        generate(ctx, "U", lanes, "SLB")
        unit, _mcu = read_pair(ctx, "U")
        for kind in ("AFC_stepper lane", "tmc2209 AFC_stepper lane", "AFC_button lane",
                     "temperature_sensor lane"):
            assert unit.count(f"[{kind}") == lanes, kind
        assert unit.count("[controller_fan U_fan_lane") == lanes

    def test_ebb_and_slb_use_different_pin_aliases_and_active_led_sections(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        generate(ctx, "E", 1, "EBB")
        ebb_unit, ebb_mcu = read_pair(ctx, "E")
        generate(ctx, "S", 1, "SLB")
        slb_unit, slb_mcu = read_pair(ctx, "S")
        assert "RGB=PD3," in ebb_mcu and "RGB_button" not in ebb_mcu
        assert "RGB_button=PA4," in slb_mcu and "MOT_UART=PB6," in slb_mcu
        assert "\n[AFC_led E_Indicator_1]\n" in ebb_unit
        assert "\n[AFC_led S_Indicator_Button_1]\n" in slb_unit
        assert "\n#[AFC_led S_Indicator_1]\n" in slb_unit

    def test_a_missing_or_unknown_board_means_ebb(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        generate(ctx, "unit_ref", 2, "EBB")
        generate(ctx, "unit_empty", 2, "")
        generate(ctx, "unit_xyz", 2, "XYZ")
        reference = read_pair(ctx, "unit_ref")
        for name in ("unit_empty", "unit_xyz"):
            others = [text.replace(name, "unit_ref") for text in read_pair(ctx, name)]
            assert others == reference

    def test_zero_lanes_gives_an_empty_mcu_list_and_no_lane_sections(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        generate(ctx, "Z", 0, "EBB")
        unit, mcu = read_pair(ctx, "Z")
        assert "\nmcu: \n" in mcu
        assert "[AFC_stepper" not in unit and "[AFC_hub Z_HUB]" in unit

    def test_it_creates_the_mcu_directory_and_overwrites_without_asking(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        ctx.afc_config_dir.mkdir(parents=True)
        (ctx.afc_config_dir / "AFC_E.cfg").write_text("old")
        generate(ctx, "E", 1, "EBB")
        assert (ctx.afc_config_dir / "AFC_E.cfg").read_text().startswith("[include mcu/E.cfg]")
        assert (ctx.afc_config_dir / "mcu" / "E.cfg").is_file()


class TestEmuUnit:
    def run(self, tree: PrinterTree, sel: UnitSelection, additional: bool) -> Context:
        ctx = make_ctx(tree)
        ctx.afc_config_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_for(EMU)
        assert unit is not None
        action = unit.install_additional if additional else unit.copy_files
        action(ctx, RecordingConsole(), ScriptedPrompter([]), sel)
        return ctx

    def test_a_first_install_with_the_default_name_becomes_emu_1(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=EMU, unit_name="Turtle_1", emu_num_lanes=2)
        ctx = self.run(printer_tree, sel, additional=False)
        assert sel.unit_name == "EMU_1"
        assert (ctx.afc_config_dir / "AFC_EMU_1.cfg").read_text().count("[AFC_stepper lane") == 2
        assert (ctx.afc_config_dir / "AFC_Hardware.cfg").read_bytes() == (
            printer_tree.afc_path / "templates" / "AFC_Hardware-AFC.cfg"
        ).read_bytes()

    def test_a_custom_name_is_kept_and_parametrizes_the_whole_config(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=EMU, unit_name="Mine", emu_board_type="SLB")
        ctx = self.run(printer_tree, sel, additional=False)
        assert sel.unit_name == "Mine"
        unit = (ctx.afc_config_dir / "AFC_Mine.cfg").read_text()
        assert "EMU_1" not in unit and "[AFC_EMU Mine]" in unit
        assert "RGB_button" in (ctx.afc_config_dir / "mcu" / "Mine.cfg").read_text()

    def test_an_additional_unit_generates_only_its_own_two_files(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=EMU, unit_name="EMU_2", emu_num_lanes=4)
        ctx = self.run(printer_tree, sel, additional=True)
        found = sorted(
            str(p.relative_to(ctx.afc_config_dir))
            for p in ctx.afc_config_dir.rglob("*")
            if p.is_file()
        )
        assert found == ["AFC_EMU_2.cfg", "mcu/EMU_2.cfg"]

    def test_the_hardware_file_still_asks_before_overwriting(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        ctx.afc_config_dir.mkdir(parents=True)
        (ctx.afc_config_dir / "AFC_Hardware.cfg").write_text("mine")
        unit = unit_for(EMU)
        assert unit is not None
        prompter = ScriptedPrompter(["s"])
        unit.copy_files(ctx, RecordingConsole(), prompter, UnitSelection(installation_type=EMU))
        assert len(prompter.prompts) == 1
        assert (ctx.afc_config_dir / "AFC_Hardware.cfg").read_text() == "mine"

    def test_buffer_target_points_at_the_generated_buffer_section(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(EMU)
        assert unit is not None
        sel = UnitSelection(installation_type=EMU, unit_name="E_7")
        assert unit.buffer_target(ctx, sel) == BufferTarget(
            "E_7", "AFC_EMU", ctx.afc_config_dir / "AFC_E_7.cfg", "E_7_buffer",
            "[AFC_buffer E_7_buffer]",
        )

    def test_message_rows_and_buffer_support(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for(EMU)
        assert unit is not None
        sel = UnitSelection(
            installation_type=EMU, unit_name="E_7", emu_num_lanes=3, emu_board_type="SLB"
        )
        cfg = ctx.afc_config_dir
        assert unit.message(ctx, sel) == (
            "\n- Ensure you enter either your CAN bus or serial information for each lane in "
            f"the {cfg}/AFC_E_7.cfg file\n\n"
            f"- The MCU board_pins configuration is at {cfg}/mcu/E_7.cfg\n  "
        )
        rows = ["EMU Lane Count : 3", "EMU Board Type : SLB"]
        assert install_rows(unit, sel) == ["EMU Name : E_7", *rows]
        assert additional_rows(unit, ctx, sel) == ["EMU Name : EMU_1", *rows]
        assert additional_buffer_supported(EMU) is True


class TestTheCompleteRegistry:
    def test_the_nine_types_are_in_menu_order(self) -> None:
        assert [u.display_name for u in UNIT_TYPES] == [
            "BoxTurtle (4-Lane)",
            "BoxTurtle (8-Lane)",
            "NightOwl",
            "HTLF",
            "Claymore",
            "QuattroBox",
            "OpenAMS",
            "ViViD",
            "EMU",
        ]

    def test_keys_are_the_unit_keys(self) -> None:
        assert [u.key for u in UNIT_TYPES] == [
            "BoxTurtle_4Lane", "BoxTurtle_8Lane", "NightOwl", "HTLF", "Claymore",
            "QuattroBox", "OpenAMS", "ViViD", "EMU",
        ]

    def test_only_vivid_lacks_additional_unit_buffer_support(self) -> None:
        unsupported = [u.display_name for u in UNIT_TYPES if not u.additional_buffer_supported]
        assert unsupported == ["ViViD"]
