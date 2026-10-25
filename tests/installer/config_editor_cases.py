"""Case definitions for the config editor fixtures.

Each Case is an input config text plus the edit applied to it. The expected result of every
case is the committed ``<name>.out`` next to its ``<name>.in`` in fixtures/config_editor/.
Changing a committed .out file is a deliberate, reviewed behavior change.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "config_editor"

@dataclass(frozen=True)
class Case:
    name: str
    tool: str  # del_section | del_key | comment_range | uncomment_range
    args: Tuple[str, ...]
    text: str


def _ds(name: str, text: str, section: str) -> Case:
    return Case(name, "del_section", (section,), text)


def _dk(name: str, text: str, section: str, key: str) -> Case:
    return Case(name, "del_key", (section, key), text)


T_MACROS = (
    "[gcode_macro T0]\n"
    "gcode:\n"
    "    SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\n"
    "\n"
    "[gcode_macro T1]\n"
    "gcode:\n"
    "    SWITCH_OF_EXTENDED_EXTRUDER INDEX=1\n"
    "\n"
    "[gcode_macro KEEP_ME]\n"
    "gcode:\n"
    "    M117 untouched\n"
)

CASES: List[Case] = [
    # --- crudini --del <file> <section>: what remove_t_macros relies on ---
    _ds(
        "ds_middle_with_comments_and_continuation",
        "# top\n[a]\nx: 1\n\n# about b\n[gcode_macro T0]\ngcode:\n    M117 hi\n"
        "    # inner comment\n\n# about c\n[c]\ny: 2\n",
        "gcode_macro T0",
    ),
    _ds("ds_last_section", "[a]\nx: 1\n\n[gcode_macro T0]\ngcode:\n    M117 hi\n",
        "gcode_macro T0"),
    _ds("ds_absent_section", "[a]\nx: 1\n", "gcode_macro T9"),
    _ds("ds_no_trailing_newline", "[a]\nx: 1\n\n[gcode_macro T0]\ngcode: x",
        "gcode_macro T0"),
    _ds("ds_exact_name_not_prefix", "[gcode_macro T1]\na: 1\n[gcode_macro T10]\nb: 2\n",
        "gcode_macro T1"),
    _ds("ds_duplicate_sections", "[s]\na: 1\n[s]\nb: 2\n", "s"),
    _ds("ds_header_trailing_comment", "[gcode_macro T0] ; note\na: 1\n[b]\nc: 2\n",
        "gcode_macro T0"),
    _ds("ds_crlf", "[a]\r\nx: 1\r\n[gcode_macro T0]\r\ny: 2\r\n[b]\r\nz: 3\r\n",
        "gcode_macro T0"),
    _ds("ds_jinja_body",
        "[gcode_macro T0]\ngcode:\n  {% if 1 %}\n  # c\n  {% endif %}\n[x]\ny: 1\n",
        "gcode_macro T0"),
    _ds("ds_blank_both_sides", "[a]\nx: 1\n\n[b]\ny: 2\n\n[c]\nz: 3\n", "b"),
    _ds("ds_blank_before_only", "[a]\nx: 1\n\n[b]\ny: 2\n[c]\nz: 3\n", "b"),
    _ds("ds_blank_after_only", "[a]\nx: 1\n[b]\ny: 2\n\n[c]\nz: 3\n", "b"),
    _ds("ds_two_blank_before", "[a]\nx: 1\n\n\n[b]\ny: 2\n\n[c]\nz: 3\n", "b"),
    _ds("ds_three_after", "[a]\nx: 1\n[b]\ny: 2\n\n\n\n[c]\nz: 3\n", "b"),
    _ds("ds_three_before_three_after",
        "[a]\nx: 1\n\n\n\n[b]\ny: 2\n\n\n\n[c]\nz: 3\n", "b"),
    _ds("ds_first_section", "[b]\ny: 2\n\n[c]\nz: 3\n", "b"),
    _ds("ds_eof_blank_after", "[a]\nx: 1\n\n[b]\ny: 2\n\n", "b"),
    _ds("ds_only_section", "[b]\ny: 2\n", "b"),
    _ds("ds_comment_between_blanks",
        "[a]\nx: 1\n\n# b help\n[b]\ny: 2\n\n[c]\nz: 3\n", "b"),
    _ds("ds_trailing_comment_eof", "[a]\nx: 1\n\n[b]\ny: 2\n\n# end note\n", "b"),
    _ds("ds_tail_comment_kept",
        "[a]\nx: 1\n\n[b]\ny: 2\n# tail of b\n\n[c]\nz: 3\n", "b"),
    _ds("ds_adjacent_sections", "[a]\nx: 1\n\n[b]\ny: 2\n\n[b2]\nq: 1\n\n[c]\nz: 3\n",
        "b"),
    _ds("ds_comment_kept_then_blank_runs_merge",
        "[a]\nx: 1\n# keep\n\n[b]\ny: 2\n\n[c]\nz: 3\n", "b"),
    _ds("ds_realistic_t_macros",
        "[gcode_macro T0]\ngcode:\n    T0_DO\n\n[gcode_macro T1]\ngcode:\n    T1_DO\n\n"
        "[gcode_macro T10]\ngcode:\n    T10_DO\n\n[gcode_macro AFC_PARK]\ngcode:\n"
        "    PARK\n",
        "gcode_macro T1"),
    # --- crudini --del <file> <section> <key>: what remove_velocity relies on ---
    _dk("dk_middle_with_inline_comment",
        "[AFC_buffer B]\nadvance_pin: ^x\nvelocity: 1  # old\nmultiplier_high: 1.1\n",
        "AFC_buffer B", "velocity"),
    _dk("dk_absent_key", "[AFC_buffer B]\nadvance_pin: ^x\n", "AFC_buffer B", "velocity"),
    _dk("dk_absent_section", "[a]\nx: 1\n", "AFC_buffer B", "velocity"),
    _dk("dk_multiline_value", "[s]\nvelocity: 1\n  continued\nnext: 2\n", "s", "velocity"),
    _dk("dk_duplicate_keys", "[s]\nvelocity: 1\nx: 3\nvelocity: 2\n", "s", "velocity"),
    _dk("dk_equals_delimiter", "[s]\nvelocity = 1\nx: 3\n", "s", "velocity"),
    _dk("dk_commented_key_untouched", "[s]\n#velocity: 1\nx: 3\n", "s", "velocity"),
    _dk("dk_only_key_blank_after", "[s]\nvelocity: 1\n\n[t]\nz: 3\n", "s", "velocity"),
    _dk("dk_last_key_before_blank", "[s]\nx: 2\nvelocity: 1\n\n[t]\nz: 3\n", "s",
        "velocity"),
    _dk("dk_first_key", "[s]\nvelocity: 1\nx: 2\n", "s", "velocity"),
    _dk("dk_same_key_other_section_kept",
        "[s]\nvelocity: 1\n[t]\nvelocity: 2\n", "s", "velocity"),
    # --- found while building T10: values with blank lines, edges, odd headers ---
    _ds("ds_blank_lines_inside_a_value",
        "[a]\nx: 1\n\n[b]\ngcode:\n  M117 a\n\n  M117 b\n  # c\n\n[c]\nz: 3\n", "b"),
    _ds("ds_two_blank_lines_inside_a_value",
        "[a]\nx: 1\n\n[b]\ngcode:\n  M117 a\n\n\n  M117 b\n\n[c]\nz: 3\n", "b"),
    _ds("ds_comment_between_keys_is_removed",
        "[b]\nx: 1\n# between\ny: 2\n\n[c]\nz: 3\n", "b"),
    _ds("ds_section_without_keys_keeps_its_comment",
        "[a]\nx: 1\n\n[b]\n# only a comment\n\n[c]\nz: 3\n", "b"),
    _ds("ds_section_with_no_body_at_all", "[a]\nx: 1\n\n[b]\n\n[c]\nz: 3\n", "b"),
    _ds("ds_first_section_then_comment", "[b]\ny\n\n# note\n[c]\nz: 3\n", "b"),
    _ds("ds_first_section_blank_comment_blank",
        "[b]\ny: 1\n\n\n# note\n\n[c]\nz: 3\n", "b"),
    _ds("ds_option_before_the_first_section", "x: 1\n[a]\ny: 2\n", "a"),
    _ds("ds_comment_then_continuation_belongs_to_the_key",
        "[s]\nx: 1\n# c\n  cont\n[t]\nz: 1\n", "t"),
    _ds("ds_semicolon_comments", "[s]\nx: 1\n; semi\n[t]\nz: 1\n", "s"),
    _ds("ds_header_with_trailing_spaces", "[b]  \ny: 1\n[c]\nz: 1\n", "b"),
    _ds("ds_header_name_keeps_inner_spaces", "[ s ]\nvelocity: 1\n[t]\nz: 1\n", " s "),
    _ds("ds_bracket_text_in_a_value", "[s]\nx: [not a header]\ny: 2\n", "s"),
    _ds("ds_duplicate_sections_apart", "[b]\ny: 1\n[c]\nz: 1\n[b]\nw: 1\n", "b"),
    _ds("ds_trailing_blanks_after_continuation",
        "[s]\nx: 1\n  cont\n\n\n[t]\ny: 1\n", "s"),
    _ds("ds_whitespace_line_then_empty_lines_inside_a_value",
        "[a]\nx: 1\n  c\n   \n\n\n  d\n[b]\ny\n", "b"),
    _ds("ds_empty_lines_then_whitespace_line_inside_a_value",
        "[a]\nx: 1\n  c\n\n\n   \n  d\n[b]\ny\n", "b"),
    _ds("ds_every_run_of_empty_lines_collapses",
        "[a]\nx\n\n\n\n[b]\ny\n\n\n\n[c]\nz\n", "c"),
    _ds("ds_leading_empty_lines_collapse_to_one", "\n\n\n[a]\nx\n[b]\ny\n", "b"),
    _ds("ds_empty_lines_between_options_collapse", "[a]\nx\n\n\ny\n[b]\nz\n", "b"),
    _ds("ds_blank_after_a_later_header_is_dropped", "[a]\nx\n[b]\n\ny: 2\n[c]\nz\n", "a"),
    _ds("ds_blank_after_a_header_without_options_is_kept",
        "[a]\nx\n[b]\n\n# c\n[c]\nz\n", "a"),
    _ds("ds_trailing_empty_lines_are_dropped_wherever_the_cut_was",
        "[a]\nx: 1\n[b]\ny: 2\n\n\n", "a"),
    _dk("dk_keeps_blank_runs_and_trailing_blanks",
        "[s]\nx: 1\n\n\nvelocity: 2\n\n\n[t]\nz\n\n", "s", "velocity"),
    _dk("dk_own_copy_wins_over_a_global_option",
        "velocity: 1\n[a]\nx: 2\nvelocity: 3\n", "a", "velocity"),
    _ds("ds_whitespace_only_line_at_the_end_is_kept",
        "[a]\nx: 1\n[b]\ny: 2\n   \n", "a"),
    _ds("ds_whitespace_only_line_at_the_cut_is_kept", "[a]\nx\n   \n[b]\ny\n", "a"),
    _ds("ds_whitespace_only_line_after_a_header_is_dropped",
        "[a]\nx\n[b]\n   \ny: 2\n", "a"),
    _ds("ds_section_names_are_case_sensitive", "[S]\nvelocity: 1\n", "s"),
    _dk("dk_key_names_ignore_case", "[s]\nVelocity: 1\nx: 2\n", "s", "velocity"),
    _dk("dk_key_without_a_delimiter", "[s]\nvelocity\nx: 2\n", "s", "velocity"),
    _dk("dk_tight_delimiter", "[s]\nvelocity:1\nx: 2\n", "s", "velocity"),
    _dk("dk_spaced_delimiter", "[s]\nvelocity  :  1\nx: 2\n", "s", "velocity"),
    _dk("dk_blank_then_continuation_is_part_of_the_value",
        "[s]\nvelocity: 1\n\n  after blank\nx: 2\n", "s", "velocity"),
    _dk("dk_key_with_a_space_in_its_name", "[s]\npin tool: 1\nx: 2\n", "s", "pin tool"),
    _dk("dk_missing_section_leaves_others", "[s]\nvelocity: 1\n[t]\nvelocity: 2\n", "zz",
        "velocity"),
    _dk("dk_section_names_are_case_sensitive", "[S]\nvelocity: 1\n", "s", "velocity"),
    # --- sed ranges from comment_gcode_in_fluidd ---
    Case("sr_comment_basic", "comment_range", (), T_MACROS),
    Case("sr_comment_unterminated_runs_to_eof", "comment_range", (),
         "[gcode_macro T0]\ngcode:\n    M117 no end marker\n\n[printer]\nx: 1\n"),
    Case("sr_comment_non_t_header_untouched", "comment_range", (),
         "[gcode_macro TEST]\ngcode:\n    SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\n"),
    Case("sr_comment_header_trailing_space", "comment_range", (),
         "[gcode_macro T0]  \ngcode:\n    SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\nafter\n"),
    Case("sr_comment_blank_lines_in_range", "comment_range", (),
         "[gcode_macro T0]\n\ngcode:\n\n    SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\n"),
    Case("sr_comment_already_commented_lines", "comment_range", (),
         "[gcode_macro T0]\n# note\n    SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\n"),
    Case("sr_uncomment_basic", "uncomment_range", (),
         "# [gcode_macro T0]\n# gcode:\n#     SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\n# \n"
         "[keep]\nx: 1\n"),
    Case("sr_uncomment_only_prefix_stripped_once", "uncomment_range", (),
         "# [gcode_macro T0]\n# # inner\n#     SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\n"),
    Case("sr_uncomment_unterminated_runs_to_eof", "uncomment_range", (),
         "# [gcode_macro T0]\n# gcode:\n# x\nplain line\n# another\n"),
    Case("sr_uncomment_repeated_ranges", "uncomment_range", (),
         "# [gcode_macro T0]\n#     SWITCH_OF_EXTENDED_EXTRUDER INDEX=0\n# stays? no\n"
         "# [gcode_macro T1]\n#     SWITCH_OF_EXTENDED_EXTRUDER INDEX=1\n"),
]


# Inputs crudini rejects with exit status 1 (and leaves untouched). An indented line
# is a continuation, so it needs a key above it since the last section header.
ERROR_CASES: List[Case] = [
    _ds("err_indented_line_right_after_the_header", "[s]\n  velocity: 1\nx: 2\n", "s"),
    _ds("err_indented_first_line_of_the_file", "  cont\n[a]\ny: 2\n", "a"),
    _ds("err_indented_comment_right_after_the_header",
        "[s]\n  # c\nx: 1\n[t]\nz: 1\n", "t"),
    _ds("err_blank_line_then_indented_line_after_the_header",
        "[s]\n\n  cont\nx: 1\n[t]\nz: 1\n", "t"),
    _ds("err_indented_header", "  [b]\ny: 1\n[c]\nz: 1\n", "b"),
    _ds("err_text_after_the_bracket", "[b] junk\ny: 1\n[c]\n", "b"),
    _ds("err_empty_brackets", "[]\ny: 1\n[c]\n", "c"),
    _dk("err_key_delete_hits_the_same_parse_rules", "[s]\n  velocity: 1\nx: 2\n", "s",
        "velocity"),
    _ds("err_error_in_an_unrelated_section", "[a]\nx: 1\n[bad]\n  cont\n", "a"),
]
