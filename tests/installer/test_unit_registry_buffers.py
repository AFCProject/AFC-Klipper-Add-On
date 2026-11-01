from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.unit_registry.buffers import (
    append_buffer_config,
    apply_shared_buffer,
    apply_unit_buffer,
    build_buffer_config,
    list_existing_buffers,
    prompt_shared_buffer_name,
    query_fps_pin,
    query_tn_pins,
)
from installer.unit_registry.registry import UNIT_TYPES, unit_for
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import PrinterTree, RecordingConsole, ScriptedPrompter

REPO_ROOT = Path(__file__).resolve().parents[2]
TN_HEADER = "[AFC_buffer Buf]"


def make_ctx(tree: PrinterTree) -> Context:
    return Context.create(tree.afc_path, home=tree.home)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TestBuildBufferConfig:
    def test_turtleneck_embeds_the_pins_and_the_default_multipliers(self) -> None:
        assert build_buffer_config("TurtleNeck", "Buf", "^U:TN_ADV", "^U:TN_TRL") == (
            "[AFC_buffer Buf]\n"
            "advance_pin: ^U:TN_ADV    # set advance pin\n"
            "trailing_pin: ^U:TN_TRL  # set trailing pin\n"
            "multiplier_high: 1.05   # default 1.05, factor to feed more filament\n"
            "multiplier_low:  0.95   # default 0.95, factor to feed less filament"
        )

    def test_turtleneck_v2_has_fixed_pins_and_its_own_led_section(self) -> None:
        text = build_buffer_config("TurtleNeckV2", "Buf")
        assert text.startswith("[AFC_buffer Buf]\nadvance_pin: !turtleneck:ADVANCE\n")
        assert "\nled_index: Buf_Buffer_Indicator:1\n\n[AFC_led Buf_Buffer_Indicator]\n" in text
        assert text.endswith("initial_WHITE: 0.0")

    def test_turtleneck_2_remastered_is_an_fps_psf_block_on_the_tn2r_pins(self) -> None:
        text = build_buffer_config("TurtleNeck2Remastered", "Buf")
        assert text.startswith("[AFC_buffer Buf]\ntype: FPS_PSF\nadc_pin: TN2R:PSF\n")
        led = "\nled_index: Buf_Buffer_Indicator:1\n\n[AFC_led Buf_Buffer_Indicator]\n"
        assert led + "pin: TN2R:RGB\n" in text
        assert "reversed: true  #Test slide by hand" in text
        assert text.endswith("initial_WHITE: 0.0")

    def test_fps_psf_embeds_the_adc_pin(self) -> None:
        text = build_buffer_config("FPS_PSF", "Buf", adc_pin="U:PSF")
        assert text.startswith("[AFC_buffer Buf]\ntype: FPS_PSF\nadc_pin: U:PSF\n")
        assert text.endswith("Set to true if display shows opposite of your tested state.")

    def test_there_is_no_trailing_newline_like_command_substitution(self) -> None:
        for kind in ("TurtleNeck", "TurtleNeckV2", "FPS_PSF"):
            assert not build_buffer_config(kind, "B").endswith("\n")

    def test_an_unknown_type_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid BUFFER_SYSTEM: Bogus"):
            build_buffer_config("Bogus", "B")


class TestQueryPins:
    def test_turtleneck_defaults_use_the_unit_name_and_explain_them(self) -> None:
        console, prompter = RecordingConsole(), ScriptedPrompter(["", ""])
        pins = query_tn_pins(console, prompter, "TN", "Bob_2")
        assert pins == ("^Bob_2:TN_ADV", "^Bob_2:TN_TRL")
        assert prompter.prompts == [
            "  Enter the advance pin (default: ^Bob_2:TN_ADV): ",
            "  Enter the trailing pin (default: ^Bob_2:TN_TRL): ",
        ]
        assert console.messages == [
            ("INFO", "\nPlease enter the pin numbers for the TurtleNeck buffer 'TN':"),
            ("INFO", "(Leave blank to use the default values)"),
            ("INFO", "Ensure you use a pull-up '^' if you are using a AFC end stop pin."),
            ("INFO", "(Default: ^Bob_2:TN_ADV)"),
            ("INFO", "(Default: ^Bob_2:TN_TRL)"),
            ("INFO", "Set TN Advance pin: ^Bob_2:TN_ADV"),
            ("INFO", "Set TN Trailing pin: ^Bob_2:TN_TRL"),
        ]

    def test_typed_pins_replace_the_defaults_one_at_a_time(self) -> None:
        console = RecordingConsole()
        pins = query_tn_pins(console, ScriptedPrompter(["PA1", "  "]), "TN", "U")
        assert pins == ("PA1", "^U:TN_TRL")
        assert console.messages[-2:] == [
            ("INFO", "Set TN Advance pin: PA1"),
            ("INFO", "Set TN Trailing pin: ^U:TN_TRL"),
        ]

    def test_the_unit_name_defaults_to_turtle_1(self) -> None:
        pins = query_tn_pins(RecordingConsole(), ScriptedPrompter(["", ""]), "TN")
        assert pins == ("^Turtle_1:TN_ADV", "^Turtle_1:TN_TRL")

    @pytest.mark.parametrize("answers", [[], [""]], ids=["first_prompt", "second_prompt"])
    def test_end_of_input_stops_the_installer(self, answers: List[str]) -> None:
        with pytest.raises(InstallerExit) as exc:
            query_tn_pins(RecordingConsole(), ScriptedPrompter(answers), "TN", "U")
        assert exc.value.code == 1

    def test_fps_default_pin_comes_from_the_unit_name(self) -> None:
        console, prompter = RecordingConsole(), ScriptedPrompter([""])
        assert query_fps_pin(console, prompter, "FPS_PSF", "Bob_2") == "Bob_2:PSF"
        assert prompter.prompts == ["  Enter the ADC pin (default: Bob_2:PSF): "]
        assert console.messages == [
            ("INFO", "\nPlease enter the ADC pin for the FPS/PSF buffer 'FPS_PSF':"),
            ("INFO", "(Leave blank to use the default value)"),
            ("INFO", "Proportional Sync-Feedback (PSF) sensor is wired to."),
            ("INFO", "(Default: Bob_2:PSF)"),
            ("INFO", "Set FPS_PSF ADC pin: Bob_2:PSF"),
        ]

    def test_fps_uses_an_explicit_default_pin_when_given_and_an_empty_one_means_none(
        self,
    ) -> None:
        assert query_fps_pin(
            RecordingConsole(), ScriptedPrompter([""]), "F", "U", "fps:PA2"
        ) == "fps:PA2"
        assert query_fps_pin(RecordingConsole(), ScriptedPrompter([""]), "F", "U", "") == "U:PSF"

    def test_fps_typed_pin_wins_and_end_of_input_stops(self) -> None:
        assert query_fps_pin(RecordingConsole(), ScriptedPrompter(["PB3"]), "F", "U") == "PB3"
        with pytest.raises(InstallerExit):
            query_fps_pin(RecordingConsole(), ScriptedPrompter([]), "F", "U")


class TestAppendBufferConfig:
    def append(
        self, tree: PrinterTree, target: Path, kind: str, name: str = "Buf",
        answers: Optional[List[str]] = None,
    ) -> Context:
        ctx = make_ctx(tree)
        append_buffer_config(
            ctx, RecordingConsole(), ScriptedPrompter(answers or []), kind, name, target,
            "ADV", "TRL", "ADC",
        )
        return ctx

    def test_appends_after_a_blank_line_with_a_final_newline(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        target = write(tmp_path / "t.cfg", "[a]\nx: 1\n")
        self.append(printer_tree, target, "TurtleNeck")
        assert target.read_text() == (
            "[a]\nx: 1\n\n" + build_buffer_config("TurtleNeck", "Buf", "ADV", "TRL") + "\n"
        )

    def test_an_unterminated_file_gets_its_newline_first(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        target = write(tmp_path / "t.cfg", "[a]\nx: 1")
        self.append(printer_tree, target, "FPS_PSF")
        assert target.read_text().startswith("[a]\nx: 1\n\n[AFC_buffer Buf]\ntype: FPS_PSF\n")

    def test_an_existing_section_with_that_header_is_replaced_not_duplicated(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        before = "[a]\nx: 1\n\n[AFC_buffer Buf]\nadvance_pin: old\n\n[z]\n"
        target = write(tmp_path / "t.cfg", before)
        self.append(printer_tree, target, "FPS_PSF")
        text = target.read_text()
        assert text.count("[AFC_buffer Buf]") == 1 and "advance_pin: old" not in text
        assert "type: FPS_PSF" in text and text.endswith("\n\n[z]\n")

    def test_only_a_whole_line_header_counts_as_existing_a_comment_mention_does_not(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        target = write(tmp_path / "t.cfg", "# was [AFC_buffer Buf] before\n[a]\nx: 1\n")
        self.append(printer_tree, target, "FPS_PSF")
        text = target.read_text()
        assert text.startswith("# was [AFC_buffer Buf] before\n[a]\nx: 1\n\n[AFC_buffer Buf]\n")
        assert text.count("type: FPS_PSF") == 1

    def test_a_missing_target_file_is_created_with_a_leading_blank_line(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        target = tmp_path / "new.cfg"
        self.append(printer_tree, target, "TurtleNeck")
        assert target.read_text() == (
            "\n" + build_buffer_config("TurtleNeck", "Buf", "ADV", "TRL") + "\n"
        )

    def test_turtleneck_v2_adds_the_mcu_include_once_and_copies_the_mcu_file(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        target = write(tmp_path / "t.cfg", "[a]\n")
        ctx = self.append(printer_tree, target, "TurtleNeckV2")
        self.append(printer_tree, target, "TurtleNeckV2")
        text = target.read_text()
        assert text.count("[include mcu/TurtleNeckv2.cfg]") == 1
        assert text.endswith("[include mcu/TurtleNeckv2.cfg]\n")
        assert (ctx.afc_config_dir / "mcu" / "TurtleNeckv2.cfg").read_bytes() == (
            printer_tree.afc_path / "config" / "mcu" / "TurtleNeckv2.cfg"
        ).read_bytes()

    def test_turtleneck_2_remastered_copies_tn2_and_includes_it_once(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        target = write(tmp_path / "t.cfg", "[a]\n")
        ctx = self.append(printer_tree, target, "TurtleNeck2Remastered")
        self.append(printer_tree, target, "TurtleNeck2Remastered")
        text = target.read_text()
        assert text.count("[include mcu/TN2.cfg]") == 1
        assert text.endswith("[include mcu/TN2.cfg]\n")
        assert "TurtleNeckv2" not in text
        mcu = sorted(p.name for p in (ctx.afc_config_dir / "mcu").iterdir())
        assert mcu == ["TN2.cfg"]
        assert (ctx.afc_config_dir / "mcu" / "TN2.cfg").read_bytes() == (
            printer_tree.afc_path / "config" / "mcu" / "TN2.cfg"
        ).read_bytes()

    def test_other_buffer_types_do_not_touch_the_mcu_directory(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        target = write(tmp_path / "t.cfg", "[a]\n")
        ctx = self.append(printer_tree, target, "TurtleNeck")
        assert not (ctx.afc_config_dir / "mcu").exists()
        assert "include" not in target.read_text()

    def test_an_unknown_type_changes_nothing(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        target = write(tmp_path / "t.cfg", "[a]\n")
        with pytest.raises(ValueError):
            self.append(printer_tree, target, "Bogus")
        assert target.read_text() == "[a]\n"


TYPES = [u.display_name for u in UNIT_TYPES]


def installed(  # type: ignore[no-untyped-def]
    tree: PrinterTree, itype: str, name: str = "Turtle_1"
):
    """A first install of a unit type into the fake tree; returns ctx, unit and selection."""
    ctx = make_ctx(tree)
    unit = unit_for(itype)
    assert unit is not None
    sel = UnitSelection(installation_type=itype, unit_name=name)
    (ctx.afc_config_dir / "mcu").mkdir(parents=True)
    (ctx.afc_config_dir / "macros").mkdir()
    unit.copy_files(ctx, RecordingConsole(), ScriptedPrompter([]), sel)
    return ctx, unit, sel


class TestApplyUnitBuffer:
    def test_turtleneck_writes_the_block_with_the_typed_pins_and_links_it(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx, unit, sel = installed(printer_tree, "BoxTurtle (4-Lane)")
        target = unit.buffer_target(ctx, sel)
        assert target is not None
        apply_unit_buffer(
            ctx, RecordingConsole(), ScriptedPrompter(["adv:1", "trail:2"]), sel, "TurtleNeck"
        )
        text = target.extruder_file.read_text()
        block = build_buffer_config("TurtleNeck", target.section_name, "adv:1", "trail:2")
        assert block in text
        assert f"buffer: {target.section_name}" in text

    def test_turtleneck_pin_defaults_come_from_the_unit_name(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx, unit, sel = installed(printer_tree, "BoxTurtle (4-Lane)")
        target = unit.buffer_target(ctx, sel)
        assert target is not None
        console = RecordingConsole()
        apply_unit_buffer(ctx, console, ScriptedPrompter(["", ""]), sel, "TurtleNeck")
        advance, trailing = query_tn_pins(
            RecordingConsole(), ScriptedPrompter(["", ""]), "TN", target.unit_name
        )
        assert build_buffer_config(
            "TurtleNeck", target.section_name, advance, trailing
        ) in target.extruder_file.read_text()

    def test_turtleneck_v2_needs_no_pins_and_brings_its_mcu_file_and_include(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx, unit, sel = installed(printer_tree, "BoxTurtle (4-Lane)")
        target = unit.buffer_target(ctx, sel)
        assert target is not None
        prompter = ScriptedPrompter([])
        apply_unit_buffer(ctx, RecordingConsole(), prompter, sel, "TurtleNeckV2")
        assert prompter.prompts == []
        assert (ctx.afc_config_dir / "mcu" / "TurtleNeckv2.cfg").is_file()
        text = target.extruder_file.read_text()
        assert build_buffer_config("TurtleNeckV2", target.section_name) in text
        assert text.count("[include mcu/TurtleNeckv2.cfg]") == 1

    def test_fps_psf_asks_for_the_adc_pin(self, printer_tree: PrinterTree) -> None:
        ctx, unit, sel = installed(printer_tree, "BoxTurtle (4-Lane)")
        target = unit.buffer_target(ctx, sel)
        assert target is not None
        apply_unit_buffer(ctx, RecordingConsole(), ScriptedPrompter(["PA9"]), sel, "FPS_PSF")
        block = build_buffer_config("FPS_PSF", target.section_name, adc_pin="PA9")
        assert block in target.extruder_file.read_text()

    def test_emu_defaults_the_adc_pin_to_its_first_lane(self, printer_tree: PrinterTree) -> None:
        ctx, unit, sel = installed(printer_tree, "EMU", name="EMU_1")
        target = unit.buffer_target(ctx, sel)
        assert target is not None
        apply_unit_buffer(ctx, RecordingConsole(), ScriptedPrompter([""]), sel, "FPS_PSF")
        block = build_buffer_config(
            "FPS_PSF", target.section_name, adc_pin=f"{target.unit_name}_lane1:TN"
        )
        assert block in target.extruder_file.read_text()

    def test_openams_defaults_the_adc_pin_to_the_fps_board(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx, unit, sel = installed(printer_tree, "OpenAMS")
        target = unit.buffer_target(ctx, sel)
        assert target is not None
        apply_unit_buffer(ctx, RecordingConsole(), ScriptedPrompter([""]), sel, "FPS_PSF")
        block = build_buffer_config("FPS_PSF", target.section_name, adc_pin="fps:PA2")
        assert block in target.extruder_file.read_text()

    @pytest.mark.parametrize("buffer", ["None", "Unknown", "Bogus"])
    def test_other_buffer_types_change_nothing(
        self, printer_tree: PrinterTree, buffer: str
    ) -> None:
        ctx, unit, sel = installed(printer_tree, "BoxTurtle (4-Lane)")
        before = {p: p.read_bytes() for p in ctx.afc_config_dir.rglob("*") if p.is_file()}
        console, prompter = RecordingConsole(), ScriptedPrompter([])
        apply_unit_buffer(ctx, console, prompter, sel, buffer)
        after = {p: p.read_bytes() for p in ctx.afc_config_dir.rglob("*") if p.is_file()}
        assert (before, console.messages, prompter.prompts) == (after, [], [])

    def test_a_type_without_a_target_warns_and_skips(self, printer_tree: PrinterTree) -> None:
        console = RecordingConsole()
        sel = UnitSelection(installation_type="Not Registered")
        apply_unit_buffer(make_ctx(printer_tree), console, ScriptedPrompter([]), sel, "FPS_PSF")
        assert console.messages == [
            ("WARNING", "PSF buffer selected but no unit target is known for installation "
                        "type 'Not Registered'; skipping.")
        ]

    @pytest.mark.parametrize("buffer", ["TurtleNeck", "TurtleNeckV2", "FPS_PSF"])
    @pytest.mark.parametrize(
        "itype", [t for t in TYPES if t not in ("ViViD",)], ids=lambda t: t
    )
    def test_every_unit_type_with_a_target_gets_its_block(
        self, printer_tree: PrinterTree, itype: str, buffer: str
    ) -> None:
        ctx, unit, sel = installed(printer_tree, itype)
        target = unit.buffer_target(ctx, sel)
        if target is None:
            return
        apply_unit_buffer(ctx, RecordingConsole(), ScriptedPrompter(["a", "b"]), sel, buffer)
        text = target.extruder_file.read_text()
        assert f"[AFC_buffer {target.section_name}]" in text
        assert f"buffer: {target.section_name}" in text


class TestApplySharedBuffer:
    def test_it_points_the_unit_at_the_buffer_and_drops_the_prebaked_one(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for("Claymore")
        assert unit is not None
        sel = UnitSelection(installation_type="Claymore", unit_name="Turtle_1")
        unit.copy_files(ctx, RecordingConsole(), ScriptedPrompter([]), sel)
        apply_shared_buffer(ctx, RecordingConsole(), sel, "Elsewhere")
        text = (ctx.afc_config_dir / "AFC_Claymore_1.cfg").read_text()
        assert "buffer: Elsewhere" in text
        assert "[AFC_buffer Claymore_buffer]" not in text

    def test_choosing_the_prebaked_buffer_itself_keeps_it(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        unit = unit_for("Claymore")
        assert unit is not None
        sel = UnitSelection(installation_type="Claymore", unit_name="Turtle_1")
        unit.copy_files(ctx, RecordingConsole(), ScriptedPrompter([]), sel)
        apply_shared_buffer(ctx, RecordingConsole(), sel, "Claymore_buffer")
        text = (ctx.afc_config_dir / "AFC_Claymore_1.cfg").read_text()
        assert "[AFC_buffer Claymore_buffer]" in text and "buffer: Claymore_buffer" in text

    def test_a_type_without_a_target_warns_and_skips(self, printer_tree: PrinterTree) -> None:
        console = RecordingConsole()
        sel = UnitSelection(installation_type="Not Registered")
        apply_shared_buffer(make_ctx(printer_tree), console, sel, "Shared")
        assert console.messages == [
            ("WARNING",
             "No unit target is known for installation type 'Not Registered'; skipping.")
        ]


class TestListExistingBuffers:
    def test_reads_the_top_level_cfg_files_only_sorted_and_unique(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        write(ctx.afc_config_dir / "b.cfg", "[AFC_buffer Zed]\n[AFC_buffer Alpha]\n")
        write(ctx.afc_config_dir / "a.cfg", "[AFC_buffer Alpha]\n[AFC_buffer Mid]\n")
        write(ctx.afc_config_dir / "mcu" / "x.cfg", "[AFC_buffer Hidden]\n")
        write(ctx.afc_config_dir / "notes.txt", "[AFC_buffer Text]\n")
        assert list_existing_buffers(ctx) == ["Alpha", "Mid", "Zed"]

    def test_a_missing_directory_gives_an_empty_list(self, printer_tree: PrinterTree) -> None:
        assert list_existing_buffers(make_ctx(printer_tree)) == []


class TestPromptSharedBufferName:
    PROMPT = "Enter the number or name of the buffer to share (blank to skip): "

    def ctx_with(self, tree: PrinterTree, names: List[str]) -> Context:
        ctx = make_ctx(tree)
        write(ctx.afc_config_dir / "AFC.cfg", "".join(f"[AFC_buffer {n}]\n" for n in names))
        return ctx

    def ask(
        self, ctx: Context, answers: List[str]
    ) -> Tuple[str, RecordingConsole, ScriptedPrompter]:
        console, prompter = RecordingConsole(), ScriptedPrompter(answers)
        return prompt_shared_buffer_name(ctx, console, prompter), console, prompter

    def test_no_buffers_warns_and_returns_nothing(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        chosen, console, prompter = self.ask(ctx, [])
        assert chosen == "" and prompter.prompts == []
        assert console.messages == [
            (
                "WARNING",
                f"No existing buffers found in {ctx.afc_config_dir}; set 'buffer:' manually.",
            )
        ]

    def test_lists_the_buffers_and_accepts_a_number(self, printer_tree: PrinterTree) -> None:
        ctx = self.ctx_with(printer_tree, ["Beta", "Alpha"])
        chosen, console, prompter = self.ask(ctx, ["2"])
        assert chosen == "Beta"
        assert prompter.prompts == [self.PROMPT]
        assert console.messages == [
            ("OTHER", ""), ("OTHER", "Existing buffers:"),
            ("OTHER", "  1. Alpha"), ("OTHER", "  2. Beta"),
        ]

    def test_accepts_an_exact_name_and_trims_whitespace(self, printer_tree: PrinterTree) -> None:
        ctx = self.ctx_with(printer_tree, ["Beta", "Alpha"])
        assert self.ask(ctx, ["  Alpha  "])[0] == "Alpha"

    def test_leading_zeros_in_a_number_are_fine(self, printer_tree: PrinterTree) -> None:
        ctx = self.ctx_with(printer_tree, ["Beta", "Alpha"])
        assert self.ask(ctx, ["01"])[0] == "Alpha"

    @pytest.mark.parametrize("answers", [[""], ["  "], []], ids=["blank", "spaces", "eof"])
    def test_blank_or_end_of_input_skips(
        self, printer_tree: PrinterTree, answers: List[str]
    ) -> None:
        ctx = self.ctx_with(printer_tree, ["Alpha"])
        assert self.ask(ctx, answers)[0] == ""

    def test_bad_answers_warn_and_ask_again(self, printer_tree: PrinterTree) -> None:
        ctx = self.ctx_with(printer_tree, ["Alpha", "Beta"])
        chosen, console, prompter = self.ask(ctx, ["9", "0", "alpha", "Gamma", "Beta"])
        assert chosen == "Beta"
        assert len(prompter.prompts) == 5
        warnings = [text for kind, text in console.messages if kind == "WARNING"]
        assert warnings == [
            "'9' is not one of the existing buffers.", "'0' is not one of the existing buffers.",
            "'alpha' is not one of the existing buffers.",
            "'Gamma' is not one of the existing buffers.",
        ]

