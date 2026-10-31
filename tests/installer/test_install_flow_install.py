from pathlib import Path
from typing import Dict

import pytest

from installer.install_flow.install import (
    InstallOptions,
    apply_boxturtle_rename,
    build_install_message,
    install_afc,
)
from installer.platform_core.console import NC, RED
from installer.platform_core.context import Context
from installer.unit_registry.registry import UNIT_TYPES
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import FakeCommandRunner, RecordingConsole, ScriptedPrompter
from tests.installer.sandbox import (
    MOONRAKER_BLOCK_TEXT,
    REPO_ROOT,
    TOGGLE_DEFAULTS,
    Scenario,
    sandbox,
)
from tests.installer.unit_driver import FIXTURES, all_cases, selection_from


TYPES = [u.display_name for u in UNIT_TYPES]


def toggles(**overrides: bool) -> Dict[str, bool]:
    return {**TOGGLE_DEFAULTS, **overrides}


class TestBuildInstallMessage:
    def ctx(self) -> Context:
        return Context.create(
            Path("/repo"), home=Path("/h"), printer_config_dir=Path("/cfg"),
            klipper_dir=Path("/klip"),
        )

    def test_the_standard_text_around_the_unit_message(self) -> None:
        sel = UnitSelection(unit_name="Bob")
        text = build_install_message(self.ctx(), sel)
        assert text == (
            "\n- AFC Configuration updated with selected options at /cfg/AFC/AFC.cfg\n\n"
            "- AFC-Klipper-Add-On python extensions installed to /klip/klippy/extras/\n"
            "\n- Ensure you enter either your CAN bus or serial information in the "
            "/cfg/AFC/AFC_Bob.cfg file\n  "
            "\nYou may now quit the script or return to the main menu.\n\n"
            f"{RED}If you would like to add any additional units, please restart the script to "
            "ensure the\ncurrent units are loaded correctly."
            f"{NC}\n"
        )

    def test_turtleneck_v2_adds_the_serial_note(self) -> None:
        sel = UnitSelection(unit_name="Bob", buffer_type="TurtleNeckV2")
        assert (
            "\n- Ensure you add the correct serial information to the "
            "/cfg/AFC/mcu/TurtleNeckv2.cfg file\n  "
        ) in build_install_message(self.ctx(), sel)

    def test_turtleneck_2_remastered_adds_the_serial_note(self) -> None:
        sel = UnitSelection(unit_name="Bob", buffer_type="TurtleNeck2Remastered")
        message = build_install_message(self.ctx(), sel)
        assert (
            "\n- Ensure you add the correct serial information to the "
            "/cfg/AFC/mcu/TN2.cfg file\n  "
        ) in message
        assert "TurtleNeckv2.cfg" not in message

    def test_fps_psf_adds_the_adc_pin_note(self) -> None:
        sel = UnitSelection(unit_name="Bob", buffer_type="FPS_PSF")
        assert (
            "\n- Ensure the PSF ADC pin in your buffer configuration matches where your wiring "
            "is connected to your MCU.\n  "
        ) in build_install_message(self.ctx(), sel)

    @pytest.mark.parametrize("buffer", ["None", "Unknown", "TurtleNeck"])
    def test_other_buffers_add_no_note(self, buffer: str) -> None:
        sel = UnitSelection(unit_name="Bob", buffer_type=buffer)
        text = build_install_message(self.ctx(), sel)
        assert "TurtleNeckv2" not in text and "PSF ADC" not in text

    def test_an_unknown_unit_type_has_no_unit_message(self) -> None:
        sel = UnitSelection(installation_type="Nope")
        assert "CAN bus" not in build_install_message(self.ctx(), sel)


MESSAGE_CASES = [case for case in all_cases() if case[1] == "message"]


class TestReplayOfTheMessageFixtures:
    def test_all_120_message_fixtures_are_present(self) -> None:
        assert len(MESSAGE_CASES) == 120

    @pytest.mark.parametrize(
        "name, mode, overrides", MESSAGE_CASES, ids=[case[0] for case in MESSAGE_CASES]
    )
    def test_message_equals_the_recorded_fixture(
        self, name: str, mode: str, overrides: Dict[str, str]
    ) -> None:
        ctx = Context.create(
            REPO_ROOT, home=Path("<HOME>"), printer_config_dir=Path("<WORKDIR>"),
            klipper_dir=Path("<WORKDIR>/klipper"),
        )
        sel = selection_from(overrides)
        sel.buffer_type = overrides["buffer_type"]
        text = build_install_message(ctx, sel).replace("\033", "\\033")
        assert "=== MESSAGE ===\n" + text == (FIXTURES / f"{name}.txt").read_text()


class TestApplyBoxturtleRename:
    def make(self, tmp_path: Path, name: str, itype: str) -> Context:
        ctx = Context.create(REPO_ROOT, home=tmp_path)
        ctx.afc_config_dir.mkdir(parents=True)
        (ctx.afc_config_dir / "AFC_x.cfg").write_text("Turtle_1 and Turtle_1\n")
        (ctx.afc_config_dir / "sub").mkdir()
        (ctx.afc_config_dir / "sub" / "y.cfg").write_text("Turtle_1\n")
        apply_boxturtle_rename(ctx, UnitSelection(installation_type=itype, unit_name=name))
        return ctx

    @pytest.mark.parametrize("itype", ["BoxTurtle (4-Lane)", "BoxTurtle (8-Lane)"])
    def test_a_custom_name_replaces_turtle_1_in_every_file(
        self, tmp_path: Path, itype: str
    ) -> None:
        ctx = self.make(tmp_path, "Bob", itype)
        assert (ctx.afc_config_dir / "AFC_x.cfg").read_text() == "Bob and Bob\n"
        assert (ctx.afc_config_dir / "sub" / "y.cfg").read_text() == "Bob\n"

    def test_the_default_name_changes_nothing(self, tmp_path: Path) -> None:
        ctx = self.make(tmp_path, "Turtle_1", "BoxTurtle (4-Lane)")
        assert (ctx.afc_config_dir / "AFC_x.cfg").read_text() == "Turtle_1 and Turtle_1\n"

    @pytest.mark.parametrize("itype", ["NightOwl", "HTLF", "EMU", "OpenAMS"])
    def test_other_unit_types_are_never_renamed_here(self, tmp_path: Path, itype: str) -> None:
        ctx = self.make(tmp_path, "Bob", itype)
        assert (ctx.afc_config_dir / "AFC_x.cfg").read_text() == "Turtle_1 and Turtle_1\n"


class TestInstallAfcBehavior:
    def run(self, tmp_path: Path, options: InstallOptions, **ctx_kwargs: object) -> Context:
        home = sandbox(tmp_path, Scenario())
        ctx = Context.create(REPO_ROOT, home=home, **ctx_kwargs)  # type: ignore[arg-type]
        install_afc(ctx, RecordingConsole(), ScriptedPrompter([]), FakeCommandRunner(), options)
        return ctx

    def test_it_is_always_a_first_unit(self, tmp_path: Path) -> None:
        sel = UnitSelection(is_additional_unit=True)
        self.run(tmp_path, InstallOptions(selection=sel))
        assert sel.is_additional_unit is False

    def test_an_unknown_unit_type_is_rejected_before_anything_is_written(
        self, tmp_path: Path
    ) -> None:
        home = sandbox(tmp_path, Scenario())
        ctx = Context.create(REPO_ROOT, home=home)
        options = InstallOptions(selection=UnitSelection(installation_type="Nope"))
        with pytest.raises(ValueError, match="Unknown installation type 'Nope'"):
            install_afc(ctx, RecordingConsole(), ScriptedPrompter([]), FakeCommandRunner(), options)
        assert not ctx.afc_config_dir.exists()

    def test_a_sensor_choice_that_is_neither_sensor_nor_ramming_leaves_the_pin_alone(
        self, tmp_path: Path
    ) -> None:
        sel = UnitSelection(toolhead_sensor="Other")
        ctx = self.run(tmp_path, InstallOptions(selection=sel, toggles=toggles(tip_cut_servo=True)))
        template = (REPO_ROOT / "templates" / "AFC_Hardware-AFC.cfg").read_bytes()
        assert (ctx.afc_config_dir / "AFC_Hardware.cfg").read_bytes() == template

    def test_the_options_have_their_documented_defaults(self) -> None:
        options = InstallOptions(selection=UnitSelection())
        assert options.toggles == TOGGLE_DEFAULTS

    def test_the_flag_that_hides_the_install_option_is_set(self, tmp_path: Path) -> None:
        ctx = self.run(tmp_path, InstallOptions(selection=UnitSelection()))
        assert ctx.files_updated_or_installed is True

    def test_a_missing_moonraker_conf_is_skipped_quietly(self, tmp_path: Path) -> None:
        home = sandbox(tmp_path, Scenario(moonraker=None))
        ctx = Context.create(REPO_ROOT, home=home)
        runner = FakeCommandRunner(existing=["systemctl"])
        install_afc(
            ctx, RecordingConsole(), ScriptedPrompter([]), runner,
            InstallOptions(selection=UnitSelection()),
        )
        assert not ctx.moonraker_config_file.exists()
        assert ["sudo", "systemctl", "restart", "moonraker"] not in runner.calls

    def test_the_tip_cut_servo_example_is_left_out_of_the_hardware_file_by_default(
        self, tmp_path: Path
    ) -> None:
        ctx = self.run(tmp_path, InstallOptions(selection=UnitSelection()))
        text = (ctx.afc_config_dir / "AFC_Hardware.cfg").read_text()
        assert "Tip Cut Servo" not in text and "tool_cut" not in text

    def test_the_tip_cut_servo_example_can_be_included(self, tmp_path: Path) -> None:
        options = InstallOptions(selection=UnitSelection(), toggles=toggles(tip_cut_servo=True))
        ctx = self.run(tmp_path, options)
        text = (ctx.afc_config_dir / "AFC_Hardware.cfg").read_text()
        assert "######### Tip Cut Servo" in text and "#[servo tool_cut]" in text

    @pytest.mark.parametrize("itype", [u.display_name for u in UNIT_TYPES])
    def test_the_default_leaves_every_unit_types_hardware_file_without_the_example(
        self, tmp_path: Path, itype: str
    ) -> None:
        options = InstallOptions(selection=UnitSelection(installation_type=itype))
        ctx = self.run(tmp_path, options)
        assert "Tip Cut Servo" not in (ctx.afc_config_dir / "AFC_Hardware.cfg").read_text()

    def test_the_include_line_can_be_switched_off(self, tmp_path: Path) -> None:
        home = sandbox(tmp_path, Scenario())
        ctx = Context.create(REPO_ROOT, home=home)
        install_afc(
            ctx, RecordingConsole(), ScriptedPrompter([]), FakeCommandRunner(),
            InstallOptions(selection=UnitSelection(), toggles=toggles(afc_includes=False)),
        )
        assert "[include AFC/*.cfg]" not in (ctx.printer_config_dir / "printer.cfg").read_text()

    def test_a_new_update_block_in_moonraker_conf_restarts_moonraker(self, tmp_path: Path) -> None:
        home = sandbox(tmp_path, Scenario())
        ctx = Context.create(REPO_ROOT, home=home)
        runner = FakeCommandRunner(existing=["systemctl"])
        install_afc(
            ctx, RecordingConsole(), ScriptedPrompter([]), runner,
            InstallOptions(selection=UnitSelection()),
        )
        assert ["sudo", "systemctl", "restart", "moonraker"] in runner.calls

    def test_an_existing_update_block_means_no_moonraker_restart(self, tmp_path: Path) -> None:
        home = sandbox(tmp_path, Scenario())
        ctx = Context.create(REPO_ROOT, home=home)
        for _ in range(2):
            runner = FakeCommandRunner(existing=["systemctl"])
            install_afc(
                ctx, RecordingConsole(), ScriptedPrompter(["o"] * 10), runner,
                InstallOptions(selection=UnitSelection()),
            )
        assert ["sudo", "systemctl", "restart", "moonraker"] not in runner.calls

    def test_test_mode_keeps_a_git_install_out_of_the_klipper_exclude_list(
        self, tmp_path: Path
    ) -> None:
        home = sandbox(tmp_path, Scenario())
        ctx = Context.create(REPO_ROOT, home=home, test_mode=True)
        console = RecordingConsole()
        install_afc(
            ctx, console, ScriptedPrompter([]), FakeCommandRunner(),
            InstallOptions(selection=UnitSelection()),
        )
        assert (home / "klipper" / ".git" / "info" / "exclude").read_text() == ""
        assert not [m for m in console.messages if "exclude" in m[1]]

    def test_a_second_install_over_the_same_tree_keeps_a_single_include_and_prep(
        self, tmp_path: Path
    ) -> None:
        home = sandbox(tmp_path, Scenario())
        ctx = Context.create(REPO_ROOT, home=home)
        for _ in range(2):
            install_afc(
                ctx, RecordingConsole(), ScriptedPrompter(["o"] * 10), FakeCommandRunner(),
                InstallOptions(selection=UnitSelection()),
            )
        printer = (ctx.printer_config_dir / "printer.cfg").read_text()
        afc = (ctx.afc_config_dir / "AFC.cfg").read_text()
        assert printer.count("[include AFC/*.cfg]") == 1
        assert afc.count("[AFC_prep]") == 1
        assert (ctx.moonraker_config_file).read_text().count(MOONRAKER_BLOCK_TEXT) == 1
