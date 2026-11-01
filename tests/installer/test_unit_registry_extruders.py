from pathlib import Path

import pytest

from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.unit_registry.extruders import (
    apply_unit_extruder,
    find_afc_extruder_sections,
    find_printer_extruders,
    resolve_afc_extruder_section,
    section_for_extruder,
)
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import RecordingConsole, ScriptedPrompter


def _ctx(tmp_path: Path) -> Context:
    ctx = Context.create(afc_path=tmp_path / "afc", home=tmp_path)
    ctx.printer_config_dir.mkdir(parents=True, exist_ok=True)
    return ctx


class TestFindPrinterExtruders:
    def test_missing_printer_cfg_returns_empty_list(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)

        assert find_printer_extruders(ctx) == []

    def test_single_extruder_is_found(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        (ctx.printer_config_dir / "printer.cfg").write_text("[extruder]\nnozzle_diameter: 0.4\n")

        assert find_printer_extruders(ctx) == ["extruder"]

    def test_multiple_extruders_are_found_in_order(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        (ctx.printer_config_dir / "printer.cfg").write_text(
            "[extruder]\nnozzle_diameter: 0.4\n\n[extruder1]\nnozzle_diameter: 0.4\n"
        )

        assert find_printer_extruders(ctx) == ["extruder", "extruder1"]

    def test_a_similarly_named_section_is_not_matched(self, tmp_path: Path) -> None:
        """extruder_stepper and other non-exact names must not be mistaken for a lane."""
        ctx = _ctx(tmp_path)
        (ctx.printer_config_dir / "printer.cfg").write_text(
            "[extruder]\nnozzle_diameter: 0.4\n\n[extruder_stepper my_extra]\n"
        )

        assert find_printer_extruders(ctx) == ["extruder"]

    def test_a_commented_out_header_is_not_matched(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        (ctx.printer_config_dir / "printer.cfg").write_text("#[extruder1]\n[extruder]\n")

        assert find_printer_extruders(ctx) == ["extruder"]

    def test_follows_a_plain_include(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        (ctx.printer_config_dir / "printer.cfg").write_text(
            "[include toolhead.cfg]\n[extruder]\n"
        )
        (ctx.printer_config_dir / "toolhead.cfg").write_text("[extruder1]\n")

        assert find_printer_extruders(ctx) == ["extruder1", "extruder"]

    def test_follows_a_glob_include(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        (ctx.printer_config_dir / "printer.cfg").write_text("[include tools/*.cfg]\n")
        tools = ctx.printer_config_dir / "tools"
        tools.mkdir()
        (tools / "a.cfg").write_text("[extruder]\n")
        (tools / "b.cfg").write_text("[extruder1]\n")

        assert find_printer_extruders(ctx) == ["extruder", "extruder1"]

    def test_a_circular_include_does_not_hang(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        (ctx.printer_config_dir / "printer.cfg").write_text(
            "[include loop.cfg]\n[extruder]\n"
        )
        (ctx.printer_config_dir / "loop.cfg").write_text("[include printer.cfg]\n[extruder1]\n")

        assert find_printer_extruders(ctx) == ["extruder1", "extruder"]

    def test_the_same_extruder_name_from_two_files_is_not_added_twice(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        (ctx.printer_config_dir / "printer.cfg").write_text(
            "[extruder]\n[include dup.cfg]\n"
        )
        (ctx.printer_config_dir / "dup.cfg").write_text("[extruder]\n")

        assert find_printer_extruders(ctx) == ["extruder"]

    def test_an_included_file_that_does_not_exist_is_skipped(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        (ctx.printer_config_dir / "printer.cfg").write_text(
            "[include missing.cfg]\n[extruder]\n"
        )

        assert find_printer_extruders(ctx) == ["extruder"]


def _write_hardware(ctx: Context, text: str) -> Path:
    ctx.afc_config_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.afc_config_dir / "AFC_Hardware.cfg"
    path.write_text(text)
    return path


def _write_template(ctx: Context, text: str) -> Path:
    templates = ctx.afc_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    path = templates / "AFC_Hardware-AFC.cfg"
    path.write_text(text)
    return path


class TestFindAfcExtruderSections:
    def test_no_cfg_files_returns_empty_dict(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        ctx.afc_config_dir.mkdir(parents=True)

        assert find_afc_extruder_sections(ctx) == {}

    def test_a_section_with_no_extruder_name_targets_its_own_name(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[force_move]\nenable_force_move: True\n\n"
                            "[AFC_extruder extruder]\npin_tool_start: enter_tool_pin\n")

        assert find_afc_extruder_sections(ctx) == {"extruder": "extruder"}

    def test_an_extruder_name_option_overrides_the_default_target(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(
            ctx, "[AFC_extruder MyExtruder]\npin_tool_start: x\nextruder_name: extruder1\n"
        )

        assert find_afc_extruder_sections(ctx) == {"MyExtruder": "extruder1"}

    def test_a_commented_out_extruder_name_is_not_matched(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[AFC_extruder extruder]\n#extruder_name: extruder1\n")

        assert find_afc_extruder_sections(ctx) == {"extruder": "extruder"}

    def test_a_section_adjacent_to_another_header_with_no_blank_line_is_bounded(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(
            ctx,
            "[AFC_extruder extruder]\nextruder_name: extruder\n"
            "[filament_switch_sensor bypass]\nextruder_name: extruder1\n",
        )

        assert find_afc_extruder_sections(ctx) == {"extruder": "extruder"}

    def test_multiple_sections_across_multiple_files_are_all_found(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[AFC_extruder extruder]\n")
        ctx.afc_config_dir.mkdir(parents=True, exist_ok=True)
        (ctx.afc_config_dir / "AFC_Turtle_2.cfg").write_text(
            "[AFC_extruder extruder1]\nextruder_name: extruder1\n"
        )

        assert find_afc_extruder_sections(ctx) == {
            "extruder": "extruder", "extruder1": "extruder1",
        }


class TestSectionForExtruder:
    def test_a_matching_section_is_found(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[AFC_extruder MyExtruder]\nextruder_name: extruder1\n")

        assert section_for_extruder(ctx, "extruder1") == "MyExtruder"

    def test_the_right_section_is_picked_among_several(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(
            ctx,
            "[AFC_extruder extruder]\n\n[AFC_extruder Second]\nextruder_name: extruder1\n",
        )

        assert section_for_extruder(ctx, "extruder1") == "Second"

    def test_no_matching_section_returns_none(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[AFC_extruder extruder]\n")

        assert section_for_extruder(ctx, "extruder1") is None


class TestResolveAfcExtruderSection:
    def test_an_existing_section_is_returned_without_asking(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[AFC_extruder extruder]\n")
        console, prompter = RecordingConsole(), ScriptedPrompter([])

        name = resolve_afc_extruder_section(ctx, console, prompter, "extruder")

        assert name == "extruder"
        assert console.messages == []
        assert prompter.prompts == []

    def test_an_existing_renamed_section_is_found_via_extruder_name(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[AFC_extruder MyExtruder]\nextruder_name: extruder1\n")
        console, prompter = RecordingConsole(), ScriptedPrompter([])

        name = resolve_afc_extruder_section(ctx, console, prompter, "extruder1")

        assert name == "MyExtruder"
        assert console.messages == []

    def test_no_match_with_a_blank_answer_creates_a_section_named_after_the_extruder(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        hardware = _write_hardware(
            ctx, "[AFC_extruder extruder]\npin_tool_start: enter_tool_pin\ntool_stn: 72\n"
        )
        console, prompter = RecordingConsole(), ScriptedPrompter([""])

        name = resolve_afc_extruder_section(ctx, console, prompter, "extruder1")

        assert name == "extruder1"
        assert console.messages == [
            ("INFO", "\nPrinter extruder 'extruder1' has no AFC_extruder section yet. A new "
                    "AFC_extruder\nconfig will be added to AFC_Hardware.cfg file with name "
                    "specified below.")
        ]
        assert prompter.prompts == [
            "Enter a name for the new AFC_extruder section or leave as default "
            "(default: extruder1): "
        ]
        text = hardware.read_text()
        assert "[AFC_extruder extruder1]" in text
        assert "pin_tool_start: enter_tool_pin" in text.split("[AFC_extruder extruder1]")[1]
        assert "extruder_name: extruder1" in text

    def test_no_match_with_a_custom_name_uses_that_name(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        hardware = _write_hardware(ctx, "[AFC_extruder extruder]\n")
        console, prompter = RecordingConsole(), ScriptedPrompter(["MyNewExtruder"])

        name = resolve_afc_extruder_section(ctx, console, prompter, "extruder1")

        assert name == "MyNewExtruder"
        text = hardware.read_text()
        assert "[AFC_extruder MyNewExtruder]" in text
        assert "extruder_name: extruder1" in text

    def test_end_of_input_raises_installer_exit_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        hardware = _write_hardware(ctx, "[AFC_extruder extruder]\n")
        before = hardware.read_text()
        console, prompter = RecordingConsole(), ScriptedPrompter([])

        with pytest.raises(InstallerExit) as exc:
            resolve_afc_extruder_section(ctx, console, prompter, "extruder1")

        assert exc.value.code == 1
        assert hardware.read_text() == before

    def test_no_template_file_falls_back_to_a_built_in_default_body(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        ctx.afc_config_dir.mkdir(parents=True)
        hardware = ctx.afc_config_dir / "AFC_Hardware.cfg"
        console, prompter = RecordingConsole(), ScriptedPrompter([""])

        name = resolve_afc_extruder_section(ctx, console, prompter, "extruder")

        assert name == "extruder"
        text = hardware.read_text()
        assert "[AFC_extruder extruder]" in text
        assert "pin_tool_start: enter_tool_pin" in text
        assert "extruder_name: extruder" in text

    def test_a_template_with_no_afc_extruder_section_falls_back_to_the_default_body(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        hardware = _write_hardware(ctx, "[force_move]\nenable_force_move: True\n")
        _write_template(ctx, "[force_move]\nenable_force_move: True\n")
        console, prompter = RecordingConsole(), ScriptedPrompter([""])

        name = resolve_afc_extruder_section(ctx, console, prompter, "extruder")

        assert name == "extruder"
        text = hardware.read_text()
        assert "[force_move]" in text
        assert "[AFC_extruder extruder]" in text
        assert "pin_tool_start: enter_tool_pin" in text

    def test_the_body_is_cloned_from_the_template_not_the_live_hardware_file(
        self, tmp_path: Path
    ) -> None:
        """AFCProject-requested behavior: a new section's body always comes from
        templates/AFC_Hardware-AFC.cfg, never from whatever is already in the live
        AFC_Hardware.cfg (which, for example, could be a Snapmaker U1 style block)."""
        ctx = _ctx(tmp_path)
        hardware = _write_hardware(
            ctx,
            "[AFC_extruder e0]\n"
            "u1_filament_sensor_name: e0_filament\n"
            "toolchanger_unit: Tools\n"
            "custom_tool_swap : PICK_EXTRUDER\n"
            "extruder_name: extruder\n",
        )
        _write_template(
            ctx, "[AFC_extruder extruder]\npin_tool_start: enter_tool_pin\ntool_stn: 72\n"
        )
        console, prompter = RecordingConsole(), ScriptedPrompter([""])

        name = resolve_afc_extruder_section(ctx, console, prompter, "extruder1")

        assert name == "extruder1"
        new_section = hardware.read_text().split("[AFC_extruder extruder1]")[1]
        assert "pin_tool_start: enter_tool_pin" in new_section
        assert "tool_stn: 72" in new_section
        assert "u1_filament_sensor_name" not in new_section
        assert "toolchanger_unit" not in new_section
        assert "custom_tool_swap" not in new_section

    def test_cloning_the_templates_own_extruder_name_drops_it(self, tmp_path: Path) -> None:
        """The real template has no extruder_name: line today, but if the cloned
        section ever had one it must still be dropped, so only the fresh value for
        the extruder being created for is ever kept."""
        ctx = _ctx(tmp_path)
        hardware = _write_hardware(ctx, "[AFC_extruder extruder]\n")
        _write_template(
            ctx, "[AFC_extruder extruder]\npin_tool_start: x\nextruder_name: extruder\n"
        )
        console, prompter = RecordingConsole(), ScriptedPrompter([""])

        name = resolve_afc_extruder_section(ctx, console, prompter, "extruder1")

        assert name == "extruder1"
        new_section = hardware.read_text().split("[AFC_extruder extruder1]")[1]
        assert "pin_tool_start: x" in new_section
        assert new_section.count("extruder_name:") == 1
        assert "extruder_name: extruder1" in new_section

    def test_a_section_glued_to_a_following_header_with_no_blank_line_is_cloned_without_it(
        self, tmp_path: Path
    ) -> None:
        """Covers the body-boundary check's other operand (a following header, not a
        blank line) independently of the blank-line case already exercised above."""
        ctx = _ctx(tmp_path)
        hardware = _write_hardware(ctx, "[AFC_extruder extruder]\n")
        _write_template(
            ctx,
            "[AFC_extruder extruder]\npin_tool_start: x\n"
            "[AFC_extruder other]\npin_tool_start: y\n",
        )
        console, prompter = RecordingConsole(), ScriptedPrompter([""])

        name = resolve_afc_extruder_section(ctx, console, prompter, "extruder1")

        assert name == "extruder1"
        new_section = hardware.read_text().split("[AFC_extruder extruder1]")[1]
        assert "pin_tool_start: x" in new_section
        assert "pin_tool_start: y" not in new_section


class TestApplyUnitExtruderFillsPinToolStart:
    """pin_tool_start: on a newly created AFC_extruder section must match the unit's
    own sensor/ramming choice, the same values install_afc gives the first unit's
    default section, instead of staying on the cloned template's placeholder."""

    def _new_section(self, hardware: Path, name: str) -> str:
        return hardware.read_text().split(f"[AFC_extruder {name}]")[1]

    def test_a_sensor_choice_uses_its_own_pin(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        hardware = _write_hardware(ctx, "[AFC_extruder extruder]\n")
        unit_file = ctx.afc_config_dir / "AFC_Turtle_1.cfg"
        unit_file.write_text("[AFC_BoxTurtle Turtle_1]\nextruder: extruder\n")
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)", unit_name="Turtle_1",
                            extruder="extruder1", toolhead_sensor="Sensor",
                            toolhead_sensor_pin="nhk:gpio13")
        console, prompter = RecordingConsole(), ScriptedPrompter([""])

        apply_unit_extruder(ctx, console, prompter, sel)

        assert "pin_tool_start: nhk:gpio13" in self._new_section(hardware, "extruder1")

    def test_ramming_on_a_regular_unit_uses_the_buffer_pin(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        hardware = _write_hardware(ctx, "[AFC_extruder extruder]\n")
        unit_file = ctx.afc_config_dir / "AFC_Turtle_1.cfg"
        unit_file.write_text("[AFC_BoxTurtle Turtle_1]\nextruder: extruder\n")
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)", unit_name="Turtle_1",
                            extruder="extruder1", toolhead_sensor="Ramming")
        console, prompter = RecordingConsole(), ScriptedPrompter([""])

        apply_unit_extruder(ctx, console, prompter, sel)

        new_section = self._new_section(hardware, "extruder1")
        assert "pin_tool_start: buffer" in new_section
        assert "pin_tool_start: AMS_extruder" not in new_section

    def test_ramming_on_openams_uses_the_ams_extruder_pin(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        hardware = _write_hardware(ctx, "[AFC_extruder extruder]\n")
        unit_file = ctx.afc_config_dir / "AFC_AMS_1.cfg"
        unit_file.write_text("[AFC_OpenAMS AMS_1]\nextruder: extruder\n")
        sel = UnitSelection(installation_type="OpenAMS", extruder="extruder1",
                            toolhead_sensor="Ramming")
        console, prompter = RecordingConsole(), ScriptedPrompter([""])

        apply_unit_extruder(ctx, console, prompter, sel)

        new_section = self._new_section(hardware, "extruder1")
        assert "pin_tool_start: AMS_extruder" in new_section
        assert "pin_tool_start: buffer" not in new_section

    def test_neither_sensor_nor_ramming_leaves_the_templates_placeholder(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        hardware = _write_hardware(ctx, "[AFC_extruder extruder]\n")
        unit_file = ctx.afc_config_dir / "AFC_Turtle_1.cfg"
        unit_file.write_text("[AFC_BoxTurtle Turtle_1]\nextruder: extruder\n")
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)", unit_name="Turtle_1",
                            extruder="extruder1", toolhead_sensor="Other")
        console, prompter = RecordingConsole(), ScriptedPrompter([""])

        apply_unit_extruder(ctx, console, prompter, sel)

        assert "pin_tool_start: enter_tool_pin" in self._new_section(hardware, "extruder1")

    def test_an_existing_section_keeps_its_own_pin_untouched(self, tmp_path: Path) -> None:
        """Pin filling only ever applies to a newly created section; a section that
        already targets the selected extruder is returned as-is, whatever the
        unit's own sensor/ramming choice implies."""
        ctx = _ctx(tmp_path)
        hardware = _write_hardware(
            ctx,
            "[AFC_extruder MyExtruder]\npin_tool_start: some_other_pin\n"
            "extruder_name: extruder1\n",
        )
        unit_file = ctx.afc_config_dir / "AFC_Turtle_1.cfg"
        unit_file.write_text("[AFC_BoxTurtle Turtle_1]\nextruder: extruder\n")
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)", unit_name="Turtle_1",
                            extruder="extruder1", toolhead_sensor="Sensor",
                            toolhead_sensor_pin="nhk:gpio13")
        console, prompter = RecordingConsole(), ScriptedPrompter([])

        apply_unit_extruder(ctx, console, prompter, sel)

        assert "pin_tool_start: some_other_pin" in hardware.read_text()
        assert "pin_tool_start: nhk:gpio13" not in hardware.read_text()


class TestApplyUnitExtruder:
    def test_sets_the_extruder_option_to_the_matching_section_name(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[AFC_extruder MyExtruder]\nextruder_name: extruder1\n")
        unit_file = ctx.afc_config_dir / "AFC_Turtle_1.cfg"
        unit_file.write_text("[AFC_BoxTurtle Turtle_1]\nextruder: extruder\nhub: Turtle_1\n")
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)", unit_name="Turtle_1",
                            extruder="extruder1")
        console, prompter = RecordingConsole(), ScriptedPrompter([])

        apply_unit_extruder(ctx, console, prompter, sel)

        assert unit_file.read_text() == (
            "[AFC_BoxTurtle Turtle_1]\nextruder: MyExtruder \nhub: Turtle_1\n"
        )
        assert console.messages == []

    def test_no_matching_section_creates_one_and_uses_its_name(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[AFC_extruder extruder]\n")
        unit_file = ctx.afc_config_dir / "AFC_Turtle_1.cfg"
        unit_file.write_text("[AFC_BoxTurtle Turtle_1]\nextruder: extruder\nhub: Turtle_1\n")
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)", unit_name="Turtle_1",
                            extruder="extruder1")
        console, prompter = RecordingConsole(), ScriptedPrompter([""])

        apply_unit_extruder(ctx, console, prompter, sel)

        assert "extruder: extruder1" in unit_file.read_text()

    def test_a_trailing_comment_keeps_its_original_column_for_a_shorter_value(
        self, tmp_path: Path
    ) -> None:
        """Regression coverage for a user-reported nit: writing a short section name
        (e.g. "e1") must not collapse the comment down to a single space when the
        original line had it aligned further out, matching the shipped templates."""
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[AFC_extruder e1]\nextruder_name: extruder1\n")
        unit_file = ctx.afc_config_dir / "AFC_Turtle_1.cfg"
        original_line = (
            "extruder: extruder          # the [AFC_extruder] fed; a lane can override\n"
        )
        unit_file.write_text(f"[AFC_BoxTurtle Turtle_1]\n{original_line}hub: Turtle_1\n")
        comment_col = original_line.index("#")
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)", unit_name="Turtle_1",
                            extruder="extruder1")
        console, prompter = RecordingConsole(), ScriptedPrompter([])

        apply_unit_extruder(ctx, console, prompter, sel)

        lines = unit_file.read_text().splitlines()
        new_line = lines[1]
        assert new_line.startswith("extruder: e1")
        assert new_line.index("#") == comment_col
        assert new_line[len("extruder: e1"):comment_col] == (
            " " * (comment_col - len("extruder: e1"))
        )
        assert new_line[comment_col:] == original_line[comment_col:].rstrip("\n")

    def test_a_trailing_comment_falls_back_to_one_space_for_a_longer_value(
        self, tmp_path: Path
    ) -> None:
        long_name = "ThisIsAVeryLongAfcExtruderSectionName"
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, f"[AFC_extruder {long_name}]\nextruder_name: extruder1\n")
        unit_file = ctx.afc_config_dir / "AFC_Turtle_1.cfg"
        unit_file.write_text(
            "[AFC_BoxTurtle Turtle_1]\n"
            "extruder: extruder          # the [AFC_extruder] fed; a lane can override\n"
            "hub: Turtle_1\n"
        )
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)", unit_name="Turtle_1",
                            extruder="extruder1")
        console, prompter = RecordingConsole(), ScriptedPrompter([])

        apply_unit_extruder(ctx, console, prompter, sel)

        assert unit_file.read_text() == (
            "[AFC_BoxTurtle Turtle_1]\n"
            f"extruder: {long_name} # the [AFC_extruder] fed; a lane can override\n"
            "hub: Turtle_1\n"
        )

    def test_a_line_with_no_comment_gets_a_single_trailing_space(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[AFC_extruder MyExtruder]\nextruder_name: extruder1\n")
        unit_file = ctx.afc_config_dir / "AFC_Turtle_1.cfg"
        unit_file.write_text("[AFC_BoxTurtle Turtle_1]\nextruder: extruder\nhub: Turtle_1\n")
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)", unit_name="Turtle_1",
                            extruder="extruder1")
        console, prompter = RecordingConsole(), ScriptedPrompter([])

        apply_unit_extruder(ctx, console, prompter, sel)

        assert unit_file.read_text() == (
            "[AFC_BoxTurtle Turtle_1]\nextruder: MyExtruder \nhub: Turtle_1\n"
        )

    def test_a_non_matching_line_such_as_extruder_name_is_left_alone(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[AFC_extruder MyExtruder]\nextruder_name: extruder1\n")
        unit_file = ctx.afc_config_dir / "AFC_Turtle_1.cfg"
        unit_file.write_text(
            "[AFC_BoxTurtle Turtle_1]\n#extruder: extruder\nextruder_name: extruder\n"
        )
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)", unit_name="Turtle_1",
                            extruder="extruder1")
        console, prompter = RecordingConsole(), ScriptedPrompter([])

        apply_unit_extruder(ctx, console, prompter, sel)

        assert unit_file.read_text() == (
            "[AFC_BoxTurtle Turtle_1]\n#extruder: extruder\nextruder_name: extruder\n"
        )

    def test_every_matching_line_in_a_multi_lane_file_is_rewritten(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        _write_hardware(ctx, "[AFC_extruder MyExtruder]\nextruder_name: extruder1\n")
        unit_file = ctx.afc_config_dir / "AFC_Turtle_1.cfg"
        unit_file.write_text(
            "[AFC_stepper lane1]\nextruder: extruder\n\n"
            "[AFC_stepper lane2]\nextruder: extruder\n"
        )
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)", unit_name="Turtle_1",
                            extruder="extruder1")
        console, prompter = RecordingConsole(), ScriptedPrompter([])

        apply_unit_extruder(ctx, console, prompter, sel)

        assert unit_file.read_text() == (
            "[AFC_stepper lane1]\nextruder: MyExtruder \n\n"
            "[AFC_stepper lane2]\nextruder: MyExtruder \n"
        )

    def test_a_missing_target_file_is_skipped_quietly_before_any_prompt(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path)
        ctx.afc_config_dir.mkdir(parents=True)
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)", unit_name="Turtle_1",
                            extruder="extruder1")
        console, prompter = RecordingConsole(), ScriptedPrompter([])

        apply_unit_extruder(ctx, console, prompter, sel)

        assert console.messages == []
        assert prompter.prompts == []
        assert not (ctx.afc_config_dir / "AFC_Turtle_1.cfg").exists()

    def test_an_unknown_installation_type_warns_and_does_nothing(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        sel = UnitSelection(installation_type="Not A Real Type")
        console, prompter = RecordingConsole(), ScriptedPrompter([])

        apply_unit_extruder(ctx, console, prompter, sel)

        assert console.messages == [
            ("WARNING", "No unit target is known for installation type "
                        "'Not A Real Type'; extruder selection was not applied.")
        ]
        assert prompter.prompts == []
