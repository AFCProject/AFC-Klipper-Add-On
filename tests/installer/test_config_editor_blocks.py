import shutil
from pathlib import Path
from typing import List

import pytest

from installer.config_editor.blocks import (
    FLUIDD_T_MACRO_COMMENTED_START,
    FLUIDD_T_MACRO_START,
    FLUIDD_T_MACRO_END,
    comment_range,
    remove_section_block,
    replace_section_block,
    section_names,
    set_key_in_section,
    uncomment_range,
)
from tests.installer.config_editor_cases import CASES, FIXTURE_DIR, Case

REPO_ROOT = Path(__file__).resolve().parents[2]
needs_sed = pytest.mark.skipif(shutil.which("sed") is None, reason="sed needed")
RANGE_CASES = [c for c in CASES if c.tool in ("comment_range", "uncomment_range")]
HEADER = "[AFC_BoxTurtle Turtle_1]"


def write(tmp_path: Path, text: str, name: str = "f.cfg") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode())
    return path


# --- set_key_in_section (add_buffer_to_extruder) -------------------------------------------


class TestSetKeyInSection:
    def test_inserts_before_the_first_blank_line_of_the_section(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"{HEADER}\nhub: h\nextruder: e\n\n[other]\nx: 1\n")
        set_key_in_section(path, HEADER, "buffer", "Turtle_1")
        assert path.read_text() == (
            f"{HEADER}\nhub: h\nextruder: e\nbuffer: Turtle_1\n\n[other]\nx: 1\n"
        )

    def test_replaces_an_existing_line_in_place_and_drops_repeats(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"{HEADER}\nbuffer: old\nhub: h\nbuffer:dup\n\n")
        set_key_in_section(path, HEADER, "buffer", "new")
        assert path.read_text() == f"{HEADER}\nbuffer: new\nhub: h\n\n"

    def test_a_header_after_an_already_replaced_key_ends_the_section_without_another_insert(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, f"{HEADER}\nbuffer: old\n[other]\nx: 1\n")
        set_key_in_section(path, HEADER, "buffer", "new")
        assert path.read_text() == f"{HEADER}\nbuffer: new\n[other]\nx: 1\n"

    def test_inserts_before_the_next_header_when_there_is_no_blank_line(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, f"{HEADER}\nhub: h\n[other]\nx: 1\n")
        set_key_in_section(path, HEADER, "buffer", "b")
        assert path.read_text() == f"{HEADER}\nhub: h\nbuffer: b\n[other]\nx: 1\n"

    def test_appends_when_the_section_is_last_with_no_blank_line(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"[first]\nx: 1\n{HEADER}\nhub: h\n")
        set_key_in_section(path, HEADER, "buffer", "b")
        assert path.read_text() == f"[first]\nx: 1\n{HEADER}\nhub: h\nbuffer: b\n"

    def test_an_unterminated_last_line_gets_a_newline_like_awk_print(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, f"{HEADER}\nhub: h")
        set_key_in_section(path, HEADER, "buffer", "b")
        assert path.read_text() == f"{HEADER}\nhub: h\nbuffer: b\n"

    def test_a_missing_section_changes_nothing(self, tmp_path: Path) -> None:
        text = "[other]\nbuffer: x\n"
        path = write(tmp_path, text)
        set_key_in_section(path, HEADER, "buffer", "b")
        assert path.read_text() == text

    def test_the_header_must_match_the_whole_line(self, tmp_path: Path) -> None:
        text = f"{HEADER} # c\nhub: h\n"
        path = write(tmp_path, text)
        set_key_in_section(path, HEADER, "buffer", "b")
        assert path.read_text() == text

    def test_a_key_that_only_starts_with_the_name_is_not_replaced(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"{HEADER}\nbuffer_extra: x\n\n")
        set_key_in_section(path, HEADER, "buffer", "b")
        assert path.read_text() == f"{HEADER}\nbuffer_extra: x\nbuffer: b\n\n"

    def test_a_line_that_only_contains_the_key_text_is_not_replaced(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, f"{HEADER}\nnote: see buffer: x\n\n")
        set_key_in_section(path, HEADER, "buffer", "b")
        assert path.read_text() == f"{HEADER}\nnote: see buffer: x\nbuffer: b\n\n"

    def test_a_whitespace_only_line_does_not_end_the_section(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"{HEADER}\nhub: h\n   \nextruder: e\n\n")
        set_key_in_section(path, HEADER, "buffer", "b")
        assert path.read_text() == f"{HEADER}\nhub: h\n   \nextruder: e\nbuffer: b\n\n"

    def test_a_key_after_the_blank_line_that_closed_the_section_is_left_alone(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, f"{HEADER}\nhub: h\n\nbuffer: later\n")
        set_key_in_section(path, HEADER, "buffer", "b")
        assert path.read_text() == f"{HEADER}\nhub: h\nbuffer: b\n\nbuffer: later\n"

    def test_keys_outside_the_section_are_left_alone(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"[a]\nbuffer: keep\n\n{HEADER}\nhub: h\n\n[c]\nbuffer: keep\n")
        set_key_in_section(path, HEADER, "buffer", "b")
        assert path.read_text() == (
            f"[a]\nbuffer: keep\n\n{HEADER}\nhub: h\nbuffer: b\n\n[c]\nbuffer: keep\n"
        )

    def test_crlf_lines_keep_their_carriage_returns(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"{HEADER}\r\nhub: h\r\n\r\n")
        set_key_in_section(path, HEADER, "buffer", "b")
        # the header line has a \r so awk's `$0 == section` never matches: no change
        assert path.read_bytes() == f"{HEADER}\r\nhub: h\r\n\r\n".encode()


BLOCK_CORPUS: List[str] = [
    f"{HEADER}\nhub: h\n\n[b]\nx\n",
    f"{HEADER}\nhub: h\nbuffer: old\n\n",
    f"{HEADER}\n[b]\nx: 1\n",
    f"{HEADER}\n\n[b]\n",
    f"[a]\n\n{HEADER}\nbuffer: old\nbuffer: old2\nhub\n[b]\n",
    f"{HEADER}\nhub: h",
    f"{HEADER}\n",
    "",
    f"{HEADER}\nx\n\n{HEADER}\ny\n\n",
    f"{HEADER}\nbuffer:one\nz\n\n[c]\nbuffer: keep\n",
    f"[x]\n{HEADER}\r\nhub\r\n",
]


# --- replace_section_block / remove_section_block ------------------------------------------

NEW_BLOCK = "[AFC_buffer B]\ntype: FPS_PSF\nadc_pin: p"


class TestReplaceSectionBlock:
    def test_swaps_the_header_and_body_keeping_the_blank_line(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[a]\nx\n\n[AFC_buffer B]\nadvance_pin: x\n\n[c]\nz\n")
        assert replace_section_block(path, "[AFC_buffer B]", NEW_BLOCK) is True
        assert path.read_text() == f"[a]\nx\n\n{NEW_BLOCK}\n\n[c]\nz\n"

    def test_stops_at_the_next_header_when_there_is_no_blank_line(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[AFC_buffer B]\nadvance_pin: x\n[c]\nz\n")
        replace_section_block(path, "[AFC_buffer B]", NEW_BLOCK)
        assert path.read_text() == f"{NEW_BLOCK}\n[c]\nz\n"

    def test_a_section_at_the_end_of_the_file_is_replaced(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[a]\nx\n[AFC_buffer B]\nadvance_pin: x\n")
        replace_section_block(path, "[AFC_buffer B]", NEW_BLOCK)
        assert path.read_text() == f"[a]\nx\n{NEW_BLOCK}\n"

    def test_every_matching_section_is_replaced(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[AFC_buffer B]\na\n\n[AFC_buffer B]\nb\n\n")
        replace_section_block(path, "[AFC_buffer B]", "[N]")
        assert path.read_text() == "[N]\n\n[N]\n\n"

    def test_a_missing_header_returns_false_and_leaves_the_file_alone(
        self, tmp_path: Path
    ) -> None:
        text = "[a]\nx"
        path = write(tmp_path, text)
        before = path.stat().st_ino
        assert replace_section_block(path, "[AFC_buffer B]", NEW_BLOCK) is False
        assert path.read_text() == text
        assert path.stat().st_ino == before

    def test_the_presence_check_is_a_substring_but_replacement_needs_the_whole_line(
        self, tmp_path: Path
    ) -> None:
        # the header must match as a whole line, so a mention inside a comment does not count
        path = write(tmp_path, "# [AFC_buffer B] old\nx")
        assert replace_section_block(path, "[AFC_buffer B]", NEW_BLOCK) is True
        assert path.read_text() == "# [AFC_buffer B] old\nx\n"


class TestRemoveSectionBlock:
    def test_removes_header_body_and_one_trailing_blank_line(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[a]\nx\n\n[AFC_buffer B]\nadvance_pin: x\n\n\n[c]\nz\n")
        remove_section_block(path, "[AFC_buffer B]")
        assert path.read_text() == "[a]\nx\n\n\n[c]\nz\n"

    def test_stops_at_the_next_header(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[AFC_buffer B]\nx\n[c]\nz\n")
        remove_section_block(path, "[AFC_buffer B]")
        assert path.read_text() == "[c]\nz\n"

    def test_commented_lines_are_not_a_header(self, tmp_path: Path) -> None:
        text = "#[AFC_buffer B]\n#x\n"
        path = write(tmp_path, text)
        remove_section_block(path, "[AFC_buffer B]")
        assert path.read_text() == text

    def test_missing_header_only_normalizes_the_final_newline(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[a]\nx")
        remove_section_block(path, "[AFC_buffer B]")
        assert path.read_text() == "[a]\nx\n"


# --- section_names (list_existing_buffers) --------------------------------------------------


class TestSectionNames:
    def test_the_result_is_sorted_not_in_set_order(self, tmp_path: Path) -> None:
        names = ["k", "b", "z", "a", "m", "q", "d", "x", "c", "p"]
        path = write(tmp_path, "".join(f"[AFC_buffer {n}]\n" for n in names))
        assert section_names([path], "AFC_buffer") == sorted(names)

    def test_collects_sorted_unique_names_across_files(self, tmp_path: Path) -> None:
        one = write(tmp_path, "[AFC_buffer Zed]\n[AFC_buffer Alpha] # c\n[AFC_hub x]\n", "a.cfg")
        two = write(tmp_path, "[AFC_buffer Alpha]\n[AFC_buffer mid name]\n", "b.cfg")
        assert section_names([one, two], "AFC_buffer") == ["Alpha", "Zed", "mid name"]

    def test_only_headers_at_the_start_of_a_line_with_the_exact_prefix_count(
        self, tmp_path: Path
    ) -> None:
        path = write(
            tmp_path,
            " [AFC_buffer Indented]\n#[AFC_buffer Commented]\n[AFC_buffer_x No]\n"
            "[AFC_buffer]\n[AFC_buffer ]\n[AFC_buffer Real]\n",
        )
        assert section_names([path], "AFC_buffer") == ["Real"]

    def test_missing_files_are_ignored(self, tmp_path: Path) -> None:
        assert section_names([tmp_path / "nope.cfg"], "AFC_buffer") == []

    def test_no_files_gives_an_empty_list(self) -> None:
        assert section_names([], "AFC_buffer") == []


# --- comment_range / uncomment_range --------------------------------------------------------


def apply_range(case: Case, path: Path) -> None:
    if case.tool == "comment_range":
        comment_range(path, FLUIDD_T_MACRO_START, FLUIDD_T_MACRO_END)
    else:
        uncomment_range(path, FLUIDD_T_MACRO_COMMENTED_START, FLUIDD_T_MACRO_END)


class TestRangesMatchTheCommittedSedFixtures:
    @pytest.mark.parametrize("case", RANGE_CASES, ids=[c.name for c in RANGE_CASES])
    def test_output_equals_what_real_sed_produced(self, case: Case, tmp_path: Path) -> None:
        path = tmp_path / "f.cfg"
        path.write_bytes((FIXTURE_DIR / f"{case.name}.in").read_bytes())
        apply_range(case, path)
        assert path.read_bytes() == (FIXTURE_DIR / f"{case.name}.out").read_bytes()

    def test_there_are_range_fixtures_to_check(self) -> None:
        assert len(RANGE_CASES) >= 10


class TestRanges:
    def test_comment_range_on_a_custom_pair(self, tmp_path: Path) -> None:
        path = write(tmp_path, "keep\nSTART\nmid\nEND\nafter\n")
        comment_range(path, r"^START$", "END")
        assert path.read_text() == "keep\n# START\n# mid\n# END\nafter\n"

    def test_the_end_marker_is_only_looked_for_after_the_start_line(
        self, tmp_path: Path
    ) -> None:
        # sed `/a/,/b/` checks the end address starting with the line after the start
        path = write(tmp_path, "START END\nmid\nEND\nafter\n")
        comment_range(path, r"^START", "END")
        assert path.read_text() == "# START END\n# mid\n# END\nafter\n"

    def test_a_second_range_can_start_after_the_first_ends(self, tmp_path: Path) -> None:
        path = write(tmp_path, "START\nEND\nx\nSTART\ny\nEND\n")
        comment_range(path, r"^START$", "END")
        assert path.read_text() == "# START\n# END\nx\n# START\n# y\n# END\n"

    def test_nothing_matching_leaves_the_file_untouched(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a\nb\n")
        before = path.stat().st_ino
        comment_range(path, r"^START$", "END")
        assert path.stat().st_ino == before

    def test_uncomment_only_strips_one_hash_space_inside_the_range(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, "# START\n# # x\n#y\n# END\n# after\n")
        uncomment_range(path, r"^# START$", "END")
        assert path.read_text() == "START\n# x\n#y\nEND\n# after\n"

    def test_an_unterminated_last_line_stays_unterminated(self, tmp_path: Path) -> None:
        path = write(tmp_path, "START\nlast")
        comment_range(path, r"^START$", "END")
        assert path.read_bytes() == b"# START\n# last"


FUZZ_LINES = [
    "[gcode_macro T0]", "[gcode_macro T12]  ", "[gcode_macro TEST]", "[printer]",
    "# [gcode_macro T1]", "# [gcode_macro T3] # x", "gcode:",
    "    SWITCH_OF_EXTENDED_EXTRUDER INDEX=0",
    "# SWITCH_OF_EXTENDED_EXTRUDER INDEX=1", "", "# ", "# # twice", "plain", "#nospace",
]
