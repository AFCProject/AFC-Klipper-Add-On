from pathlib import Path
from typing import List

from installer.platform_core.context import Context
from installer.unit_registry.base import BufferTarget
from installer.unit_registry.naming import lowest_free_name
from installer.unit_registry.registry import (
    UNIT_TYPES,
    additional_buffer_default,
    additional_buffer_options,
    additional_buffer_supported,
    unit_for,
)
from installer.unit_registry.selection import UnitSelection
from installer.unit_registry.units.qidibox import QidiBox
from tests.installer.fakes import PrinterTree, RecordingConsole, ScriptedPrompter
from tests.installer.unit_rows import additional_rows, install_rows

QIDI = "QIDI Box"


def make_ctx(tree: PrinterTree) -> Context:
    return Context.create(tree.afc_path, home=tree.home)


def install(tree: PrinterTree, sel: UnitSelection, additional: bool) -> Context:
    ctx = make_ctx(tree)
    unit = unit_for(QIDI)
    assert unit is not None
    action = unit.install_additional if additional else unit.copy_files
    action(ctx, RecordingConsole(), ScriptedPrompter([]), sel)
    return ctx


def files(ctx: Context) -> List[str]:
    return sorted(
        p.relative_to(ctx.afc_config_dir).as_posix()
        for p in ctx.afc_config_dir.rglob("*")
        if p.is_file()
    )


def text(ctx: Context, relative: str) -> str:
    return (ctx.afc_config_dir / relative).read_text()


def src(tree: PrinterTree, relative: str) -> bytes:
    return (tree.afc_path / relative).read_bytes()


class TestRegistration:
    def test_is_registered_last_in_the_menu(self) -> None:
        assert isinstance(UNIT_TYPES[-1], QidiBox)
        assert unit_for(QIDI) is UNIT_TYPES[-1]

    def test_names(self) -> None:
        unit = QidiBox()
        assert unit.key == "QidiBox"
        assert unit.default_name == "QidiBox_1"

    def test_banner_exists_for_the_key(self) -> None:
        banner = Path(__file__).resolve().parents[2] / "installer/ui_menus/art/QidiBox.txt"
        assert "QIDI Box" in banner.read_text(encoding="utf-8")


class TestCopyFiles:
    def test_first_install_uses_fixed_names_whatever_the_unit_name(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=QIDI, unit_name="Ignored")
        ctx = install(printer_tree, sel, additional=False)
        assert files(ctx) == ["AFC_Hardware.cfg", "AFC_QidiBox_1.cfg", "mcu/QidiBox_1.cfg"]
        assert (ctx.afc_config_dir / "AFC_QidiBox_1.cfg").read_bytes() == src(
            printer_tree, "templates/AFC_QidiBox_1.cfg"
        )
        assert (ctx.afc_config_dir / "mcu/QidiBox_1.cfg").read_bytes() == src(
            printer_tree, "config/mcu/QidiBox.cfg"
        )
        assert (ctx.afc_config_dir / "AFC_Hardware.cfg").read_bytes() == src(
            printer_tree, "templates/AFC_Hardware-AFC.cfg"
        )

    def test_template_includes_the_copied_mcu_file(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(installation_type=QIDI, unit_name="QidiBox_1")
        ctx = install(printer_tree, sel, additional=False)
        assert "[include mcu/QidiBox_1.cfg]" in text(ctx, "AFC_QidiBox_1.cfg")
        assert "[mcu QidiBox_1]" in text(ctx, "mcu/QidiBox_1.cfg")

    def test_klipper_firmware_keeps_converted_serial_and_software_i2c(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=QIDI, unit_name="QidiBox_1",
                            qidi_firmware="Klipper")
        ctx = install(printer_tree, sel, additional=False)
        lines = text(ctx, "AFC_QidiBox_1.cfg").splitlines()
        mcu_lines = text(ctx, "mcu/QidiBox_1.cfg").splitlines()
        assert "i2c_software_scl_pin: QidiBox_1:AHT_SCL" in lines
        assert "i2c_software_sda_pin: QidiBox_1:AHT_SDA" in lines
        assert "#i2c_bus: i2c3" in lines
        assert "serial: /dev/serial/by-id/usb-Klipper_stm32f401xc_<insert id>-if00" in mcu_lines
        assert ("#serial: /dev/serial/by-id/usb-Klipper_QIDI_BOX_V2_<insert version and id>"
                "-if00") in mcu_lines

    def test_stock_firmware_switches_serial_and_i2c(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(installation_type=QIDI, unit_name="QidiBox_1", qidi_firmware="Stock")
        ctx = install(printer_tree, sel, additional=False)
        lines = text(ctx, "AFC_QidiBox_1.cfg").splitlines()
        mcu_lines = text(ctx, "mcu/QidiBox_1.cfg").splitlines()
        assert "#i2c_software_scl_pin: QidiBox_1:AHT_SCL" in lines
        assert "#i2c_software_sda_pin: QidiBox_1:AHT_SDA" in lines
        assert "i2c_bus: i2c3" in lines
        assert "#serial: /dev/serial/by-id/usb-Klipper_stm32f401xc_<insert id>-if00" in mcu_lines
        assert ("serial: /dev/serial/by-id/usb-Klipper_QIDI_BOX_V2_<insert version and id>"
                "-if00") in mcu_lines


class TestInstallAdditional:
    def test_unit_and_mcu_file_are_renamed(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(installation_type=QIDI, unit_name="QidiBox_2")
        ctx = install(printer_tree, sel, additional=True)
        assert files(ctx) == ["AFC_QidiBox_2.cfg", "mcu/QidiBox_2.cfg"]
        unit_text = text(ctx, "AFC_QidiBox_2.cfg")
        mcu_text = text(ctx, "mcu/QidiBox_2.cfg")
        assert "[include mcu/QidiBox_2.cfg]" in unit_text
        assert "[AFC_QidiBox QidiBox_2]" in unit_text
        assert "QidiBox_1" not in unit_text
        assert "[mcu QidiBox_2]" in mcu_text
        assert "QidiBox_1" not in mcu_text

    def test_stock_firmware_is_applied_before_renaming(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(installation_type=QIDI, unit_name="QidiBox_2", qidi_firmware="Stock")
        ctx = install(printer_tree, sel, additional=True)
        lines = text(ctx, "AFC_QidiBox_2.cfg").splitlines()
        mcu_lines = text(ctx, "mcu/QidiBox_2.cfg").splitlines()
        assert "#i2c_software_scl_pin: QidiBox_2:AHT_SCL" in lines
        assert "#i2c_software_sda_pin: QidiBox_2:AHT_SDA" in lines
        assert "i2c_bus: i2c3" in lines
        assert ("serial: /dev/serial/by-id/usb-Klipper_QIDI_BOX_V2_<insert version and id>"
                "-if00") in mcu_lines


class TestBufferTarget:
    def test_first_unit(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        target = QidiBox().buffer_target(ctx, UnitSelection(installation_type=QIDI,
                                                            unit_name="z"))
        assert target == BufferTarget(
            "QidiBox_1", "AFC_QidiBox", ctx.afc_config_dir / "AFC_QidiBox_1.cfg", "QidiBox_1"
        )

    def test_additional_unit(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        sel = UnitSelection(installation_type=QIDI, unit_name="QidiBox_2",
                            is_additional_unit=True)
        assert QidiBox().buffer_target(ctx, sel) == BufferTarget(
            "QidiBox_2", "AFC_QidiBox", ctx.afc_config_dir / "AFC_QidiBox_2.cfg", "QidiBox_2"
        )


class TestOptionsAndMessage:
    def test_rows(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        unit = QidiBox()
        sel = UnitSelection(installation_type=QIDI, unit_name="x")
        assert install_rows(unit, sel) == ["QIDI Box Firmware : Klipper"]
        assert additional_rows(unit, ctx, sel) == [
            "QIDI Box Name : QidiBox_1", "QIDI Box Firmware : Klipper"
        ]

    def test_firmware_option_cycles_klipper_and_stock(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        option = QidiBox().install_options()[0]
        sel = UnitSelection(installation_type=QIDI)
        option.activate(ctx, RecordingConsole(), ScriptedPrompter([]), sel)
        assert sel.qidi_firmware == "Stock"
        option.activate(ctx, RecordingConsole(), ScriptedPrompter([]), sel)
        assert sel.qidi_firmware == "Klipper"

    def test_default_additional_name_skips_an_installed_box(
        self, printer_tree: PrinterTree
    ) -> None:
        sel = UnitSelection(installation_type=QIDI, unit_name="QidiBox_1")
        ctx = install(printer_tree, sel, additional=False)
        assert lowest_free_name(ctx, sel, QidiBox()) == "QidiBox_2"

    def test_message(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        sel = UnitSelection(installation_type=QIDI, unit_name="x")
        cfg = ctx.afc_config_dir
        assert QidiBox().message(ctx, sel) == (
            f"\n- Ensure you enter the box's serial id in the {cfg}/mcu/QidiBox_1.cfg file\n\n"
            f"- Review the hub or direct mode settings and sensor pins in the "
            f"{cfg}/AFC_QidiBox_1.cfg file, then run AFC_CALIBRATION.\n  "
        )

    def test_additional_buffer_uses_the_generic_cycle(self) -> None:
        assert additional_buffer_supported(QIDI) is True
        assert additional_buffer_options(QIDI) == (
            "None", "TurtleNeck", "TurtleNeckV2", "TurtleNeck2Remastered", "Compensator", "FPS_PSF"
        )
        assert additional_buffer_default(QIDI) == "TurtleNeck"
