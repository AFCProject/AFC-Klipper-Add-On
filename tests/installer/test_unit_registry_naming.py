from pathlib import Path
from typing import Dict, List

import pytest

from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.unit_registry.naming import (
    check_existing_unit_installed,
    name_in_use_message,
    name_unit,
    rename_unit_prompt,
    replace_unit_name,
    valid_unit_name,
    verify_name_not_in_use,
)
from installer.unit_registry.registry import name_additional_unit
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import PrinterTree, RecordingConsole, ScriptedPrompter
from tests.installer.unit_driver import FIXTURES, replayable_cases, run_python_case

REPO_ROOT = Path(__file__).resolve().parents[2]
INVALID_TEXT = (
    "Invalid input. The unit name must consist of only a-z, A-Z, 0-9, -, and _ and be "
    "no more than 24 characters long."
)


def make_ctx(tree: PrinterTree) -> Context:
    return Context.create(tree.afc_path, home=tree.home)


def put(ctx: Context, relative: str, text: str = "") -> Path:
    path = ctx.afc_config_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TestValidUnitName:
    @pytest.mark.parametrize(
        "name", ["a", "Turtle_1", "my-unit", "A_b-C9", "x" * 24, "0", "_", "-"]
    )
    def test_accepts_ascii_letters_digits_dash_and_underscore_up_to_24(self, name: str) -> None:
        assert valid_unit_name(name) is True

    @pytest.mark.parametrize(
        "name",
        ["", "x" * 25, "has space", "dot.name", "slash/name", "semi;colon", "café",
         "tab\tname", "bad!", "new\nline", "trailing\n", "١٢"],
    )
    def test_rejects_everything_else(self, name: str) -> None:
        assert valid_unit_name(name) is False


class TestNameUnit:
    def test_enter_takes_the_default(self) -> None:
        sel = UnitSelection(unit_name="Old")
        prompter = ScriptedPrompter([""])
        message = name_unit(RecordingConsole(), prompter, sel)
        assert sel.unit_name == "Turtle_1"
        assert message == "Naming unit Turtle_1"
        assert prompter.prompts == ["Enter name for unit (Default: Turtle_1): "]

    def test_a_typed_name_is_used(self) -> None:
        sel = UnitSelection()
        assert name_unit(RecordingConsole(), ScriptedPrompter(["Side_Car"]), sel) == (
            "Naming unit Side_Car"
        )
        assert sel.unit_name == "Side_Car"

    def test_surrounding_whitespace_is_trimmed_and_blank_means_default(self) -> None:
        sel = UnitSelection()
        name_unit(RecordingConsole(), ScriptedPrompter(["  Bob  "]), sel)
        assert sel.unit_name == "Bob"
        name_unit(RecordingConsole(), ScriptedPrompter(["   "]), sel)
        assert sel.unit_name == "Turtle_1"

    def test_invalid_names_are_explained_and_asked_again(self) -> None:
        console, prompter = RecordingConsole(), ScriptedPrompter(["bad name!", "x" * 25, "Good"])
        sel = UnitSelection()
        name_unit(console, prompter, sel)
        assert sel.unit_name == "Good"
        assert len(prompter.prompts) == 3
        assert console.messages == [("OTHER", INVALID_TEXT), ("OTHER", INVALID_TEXT)]

    def test_end_of_input_stops_the_installer(self) -> None:
        sel = UnitSelection(unit_name="Keep")
        with pytest.raises(InstallerExit) as exc:
            name_unit(RecordingConsole(), ScriptedPrompter([]), sel)
        assert exc.value.code == 1
        assert sel.unit_name == "Keep"

    def test_it_does_not_mark_the_unit_as_renamed(self) -> None:
        sel = UnitSelection()
        name_unit(RecordingConsole(), ScriptedPrompter(["Bob"]), sel)
        assert sel.turtle_renamed is False


class TestNameAdditionalUnit:
    def test_the_prompt_offers_the_lowest_free_default_and_enter_takes_it(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_Turtle_1.cfg")
        sel = UnitSelection(installation_type="BoxTurtle (4-Lane)")
        prompter = ScriptedPrompter([""])
        message = name_additional_unit(ctx, RecordingConsole(), prompter, sel)
        assert prompter.prompts == ["Enter name for unit (Default: Turtle_2): "]
        assert (sel.unit_name, sel.turtle_renamed, message) == (
            "Turtle_2", True, "Naming unit Turtle_2"
        )

    def test_a_typed_name_is_used_and_marked_as_chosen(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(installation_type="OpenAMS")
        ctx = make_ctx(printer_tree)
        name_additional_unit(ctx, RecordingConsole(), ScriptedPrompter(["Mine"]), sel)
        assert (sel.unit_name, sel.turtle_renamed) == ("Mine", True)

    def test_invalid_input_is_explained_and_asked_again(self, printer_tree: PrinterTree) -> None:
        console, prompter = RecordingConsole(), ScriptedPrompter(["no good", "ok"])
        sel = UnitSelection()
        name_additional_unit(make_ctx(printer_tree), console, prompter, sel)
        assert sel.unit_name == "ok"
        assert console.messages == [("OTHER", INVALID_TEXT)]

    def test_end_of_input_stops_the_installer(self, printer_tree: PrinterTree) -> None:
        sel = UnitSelection(unit_name="Keep")
        with pytest.raises(InstallerExit):
            ctx = make_ctx(printer_tree)
            name_additional_unit(ctx, RecordingConsole(), ScriptedPrompter([]), sel)
        assert (sel.unit_name, sel.turtle_renamed) == ("Keep", False)


class TestReplaceUnitName:
    def test_rewrites_contents_everywhere_and_moves_the_two_named_files(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_Old.cfg", "[AFC_BoxTurtle Old]\nhub: Old\n")
        put(ctx, "mcu/Old.cfg", "[mcu Old]\n")
        put(ctx, "AFC.cfg", "[include AFC_Old.cfg]\n")
        put(ctx, "AFC.var.unit", '{"Old": 1}')
        replace_unit_name(ctx, "Old", "New")
        assert (ctx.afc_config_dir / "AFC_New.cfg").read_text() == "[AFC_BoxTurtle New]\nhub: New\n"
        assert (ctx.afc_config_dir / "mcu" / "New.cfg").read_text() == "[mcu New]\n"
        assert not (ctx.afc_config_dir / "AFC_Old.cfg").exists()
        assert not (ctx.afc_config_dir / "mcu" / "Old.cfg").exists()
        assert (ctx.afc_config_dir / "AFC.cfg").read_text() == "[include AFC_New.cfg]\n"
        assert (ctx.afc_config_dir / "AFC.var.unit").read_text() == '{"New": 1}'

    def test_a_nested_file_with_the_name_is_moved_to_the_top_level_like_find_exec_mv(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "sub/AFC_Old.cfg", "Old")
        replace_unit_name(ctx, "Old", "New")
        assert (ctx.afc_config_dir / "AFC_New.cfg").read_text() == "New"
        assert not (ctx.afc_config_dir / "sub" / "AFC_Old.cfg").exists()

    def test_a_symlink_with_the_unit_file_name_is_left_where_it_is_like_find_type_f(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        ctx = make_ctx(printer_tree)
        target = tmp_path / "elsewhere.cfg"
        target.write_text("Old stays Old here\n")
        ctx.afc_config_dir.mkdir(parents=True)
        (ctx.afc_config_dir / "AFC_Old.cfg").symlink_to(target)
        replace_unit_name(ctx, "Old", "New")
        assert (ctx.afc_config_dir / "AFC_Old.cfg").is_symlink()
        assert not (ctx.afc_config_dir / "AFC_New.cfg").exists()
        assert target.read_text() == "Old stays Old here\n"

    def test_an_existing_target_is_overwritten_without_asking(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_Old.cfg", "from old")
        put(ctx, "AFC_New.cfg", "already here")
        replace_unit_name(ctx, "Old", "New")
        assert (ctx.afc_config_dir / "AFC_New.cfg").read_text() == "from old"

    def test_a_missing_directory_is_not_an_error(self, printer_tree: PrinterTree) -> None:
        replace_unit_name(make_ctx(printer_tree), "Old", "New")

    def test_a_differently_cased_file_is_matched_and_moved_to_the_exact_new_name(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_ACE_1.cfg", "[AFC_ACE Ace_1]\nhub: Ace_1\n")
        replace_unit_name(ctx, "Ace_1", "Ace_2")
        assert (ctx.afc_config_dir / "AFC_Ace_2.cfg").read_text() == "[AFC_ACE Ace_2]\nhub: Ace_2\n"
        assert not (ctx.afc_config_dir / "AFC_ACE_1.cfg").exists()


class TestRenameUnitPrompt:
    def setup(self, tree: PrinterTree) -> Context:
        ctx = make_ctx(tree)
        put(ctx, "AFC_Turtle_1.cfg", "[AFC_BoxTurtle Turtle_1]\n")
        put(ctx, "mcu/Turtle_1.cfg", "[mcu Turtle_1]\n")
        return ctx

    def test_enter_twice_renames_turtle_1_to_turtle_2(self, printer_tree: PrinterTree) -> None:
        ctx = self.setup(printer_tree)
        prompter = ScriptedPrompter(["", ""])
        message = rename_unit_prompt(ctx, RecordingConsole(), prompter)
        assert message == "Renaming unit Turtle_1 to Turtle_2"
        assert prompter.prompts == [
            "Enter name of unit you would like to replace (Default: Turtle_1): ",
            "Enter new name for unit (Default: Turtle_2): ",
        ]
        assert (ctx.afc_config_dir / "AFC_Turtle_2.cfg").read_text() == "[AFC_BoxTurtle Turtle_2]\n"
        assert (ctx.afc_config_dir / "mcu" / "Turtle_2.cfg").is_file()

    def test_a_unit_that_does_not_exist_is_reported_and_nothing_changes(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = self.setup(printer_tree)
        message = rename_unit_prompt(ctx, RecordingConsole(), ScriptedPrompter(["Ghost", "New"]))
        assert message == "Unit Ghost not found, please enter a valid unit name."
        assert (ctx.afc_config_dir / "AFC_Turtle_1.cfg").is_file()

    def test_a_differently_cased_file_is_found_and_renamed(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_ACE_1.cfg", "[AFC_ACE Ace_1]\n")
        message = rename_unit_prompt(
            ctx, RecordingConsole(), ScriptedPrompter(["Ace_1", "Ace_2"])
        )
        assert message == "Renaming unit Ace_1 to Ace_2"
        assert (ctx.afc_config_dir / "AFC_Ace_2.cfg").read_text() == "[AFC_ACE Ace_2]\n"
        assert not (ctx.afc_config_dir / "AFC_ACE_1.cfg").exists()

    def test_each_name_is_validated_and_asked_again(self, printer_tree: PrinterTree) -> None:
        ctx = self.setup(printer_tree)
        console = RecordingConsole()
        prompter = ScriptedPrompter(["bad name", "Turtle_1", "also bad!", "Fresh"])
        message = rename_unit_prompt(ctx, console, prompter)
        assert message == "Renaming unit Turtle_1 to Fresh"
        assert console.messages == [("OTHER", INVALID_TEXT), ("OTHER", INVALID_TEXT)]
        assert (ctx.afc_config_dir / "AFC_Fresh.cfg").is_file()

    @pytest.mark.parametrize("answers", [[], ["Turtle_1"]], ids=["first_prompt", "second_prompt"])
    def test_end_of_input_stops_the_installer(
        self, printer_tree: PrinterTree, answers: List[str]
    ) -> None:
        ctx = self.setup(printer_tree)
        with pytest.raises(InstallerExit) as exc:
            rename_unit_prompt(ctx, RecordingConsole(), ScriptedPrompter(answers))
        assert exc.value.code == 1
        assert (ctx.afc_config_dir / "AFC_Turtle_1.cfg").is_file()


class TestVerifyNameNotInUse:
    def test_a_name_inside_any_file_content_is_in_use(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "sub/deep/x.cfg", "hub: Turtle_3\n")
        assert verify_name_not_in_use(ctx, "Turtle_3") is True

    def test_it_is_a_substring_match_so_turtle_1_is_blocked_by_turtle_10(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC.cfg", "[AFC_BoxTurtle Turtle_10]\n")
        assert verify_name_not_in_use(ctx, "Turtle_1") is True
        assert verify_name_not_in_use(ctx, "Turtle_11") is False

    def test_a_file_name_alone_does_not_count(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_Solo.cfg", "nothing relevant\n")
        assert verify_name_not_in_use(ctx, "Solo") is False

    def test_a_missing_directory_means_not_in_use(self, printer_tree: PrinterTree) -> None:
        assert verify_name_not_in_use(make_ctx(printer_tree), "Anything") is False

    def test_binary_files_are_searched_too(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        (ctx.afc_config_dir).mkdir(parents=True)
        (ctx.afc_config_dir / "blob.bin").write_bytes(b"\xff\x00 Bob \xfe")
        assert verify_name_not_in_use(ctx, "Bob") is True

    def test_symlinked_files_are_followed_like_grep_capital_r(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        ctx = make_ctx(printer_tree)
        outside = tmp_path / "outside.cfg"
        outside.write_text("hub: Linked_Name\n")
        ctx.afc_config_dir.mkdir(parents=True)
        (ctx.afc_config_dir / "link.cfg").symlink_to(outside)
        assert verify_name_not_in_use(ctx, "Linked_Name") is True

    def test_a_dangling_symlink_is_skipped_not_fatal(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        ctx.afc_config_dir.mkdir(parents=True)
        (ctx.afc_config_dir / "dangling.cfg").symlink_to("/nowhere/at/all")
        put(ctx, "ok.cfg", "hub: Seen\n")
        assert verify_name_not_in_use(ctx, "Seen") is True
        assert verify_name_not_in_use(ctx, "Unseen") is False

    def test_the_message_names_the_unit(self) -> None:
        assert name_in_use_message("Bob") == (
            "Unit Bob already exists, please enter a unique unit name."
        )


class TestCheckExistingUnitInstalled:
    def test_true_when_any_file_defines_a_lane(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "deep/AFC_Turtle_1.cfg", "[AFC_stepper lane1]\n")
        assert check_existing_unit_installed(ctx) is True

    @pytest.mark.parametrize(
        "text",
        ["[AFC_stepper]\n", "# [AFC_stepper lane1]\n[AFC_stepperlane1]\n", "AFC_stepper lane1"],
    )
    def test_false_without_a_lane_section_header_text(
        self, printer_tree: PrinterTree, text: str
    ) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC.cfg", text)
        assert check_existing_unit_installed(ctx) is ("[AFC_stepper lane" in text)

    def test_symlinked_files_are_skipped_like_grep_lowercase_r(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        ctx = make_ctx(printer_tree)
        outside = tmp_path / "outside.cfg"
        outside.write_text("[AFC_stepper lane1]\n")
        ctx.afc_config_dir.mkdir(parents=True)
        (ctx.afc_config_dir / "link.cfg").symlink_to(outside)
        assert check_existing_unit_installed(ctx) is False

    def test_false_for_a_missing_directory(self, printer_tree: PrinterTree) -> None:
        assert check_existing_unit_installed(make_ctx(printer_tree)) is False


NAMING_FIXTURES = [
    case for case in replayable_cases()
    if case[1] in ("name_additional_unit", "additional_default_name")
]


class TestReplayOfTheNamingFixtures:
    def test_all_nineteen_naming_fixtures_are_replayed(self) -> None:
        assert len(NAMING_FIXTURES) == 19

    @pytest.mark.parametrize(
        "name, mode, overrides", NAMING_FIXTURES, ids=[case[0] for case in NAMING_FIXTURES]
    )
    def test_output_equals_the_recorded_fixture(
        self, name: str, mode: str, overrides: Dict[str, str]
    ) -> None:
        assert run_python_case(mode, overrides) == (FIXTURES / f"{name}.txt").read_text()
