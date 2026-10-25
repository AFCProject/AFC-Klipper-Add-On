"""Check the config editor fixtures against their case definitions.

The committed .in/.out pairs in fixtures/config_editor/ are the expected results the editor
tests replay. These tests keep the files and the case definitions in step.
"""
from tests.installer.config_editor_cases import CASES, FIXTURE_DIR


class TestCaseDefinitions:
    def test_case_names_are_unique(self) -> None:
        names = [case.name for case in CASES]
        assert len(names) == len(set(names))

    def test_every_tool_is_a_known_one(self) -> None:
        tools = {case.tool for case in CASES}
        assert tools == {"del_section", "del_key", "comment_range", "uncomment_range"}

    def test_all_blank_line_scenarios_for_section_delete_are_covered(self) -> None:
        names = {case.name for case in CASES}
        assert {
            "ds_blank_both_sides",
            "ds_blank_before_only",
            "ds_blank_after_only",
            "ds_two_blank_before",
            "ds_three_before_three_after",
            "ds_first_section",
            "ds_last_section",
            "ds_eof_blank_after",
            "ds_only_section",
            "ds_comment_between_blanks",
            "ds_tail_comment_kept",
        } <= names


class TestFixtureFiles:
    def test_every_case_has_an_input_and_output_file(self) -> None:
        missing = [
            f"{case.name}.{ext}"
            for case in CASES
            for ext in ("in", "out")
            if not (FIXTURE_DIR / f"{case.name}.{ext}").is_file()
        ]
        assert missing == []

    def test_there_are_no_orphan_fixture_files(self) -> None:
        expected = {f"{case.name}.{ext}" for case in CASES for ext in ("in", "out")}
        on_disk = {
            p.name for p in FIXTURE_DIR.iterdir() if p.suffix in (".in", ".out")
        }
        assert on_disk - expected == set()

    def test_input_files_match_the_case_definitions_byte_for_byte(self) -> None:
        for case in CASES:
            assert (FIXTURE_DIR / f"{case.name}.in").read_bytes() == case.text.encode()
