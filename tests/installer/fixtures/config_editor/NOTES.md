# Config editor behavior

The rules the Python config editors (`installer/config_editor/`) follow. They were first
observed from the tools the old shell installer used (crudini 0.9.6, GNU sed 4.10) and are
now pinned by the `<case>.in` / `<case>.out` pairs in this directory. Case names are in
backticks.

The `.out` files are recorded expected results. Changing one is a deliberate behavior change
that needs review.

## `crudini --del <file> <section>` (used by `remove_t_macros`)

1. **Removed:** the header line through the last key line of the section, including
   indented continuation lines (even ones that look like comments, such as a `# note`
   inside a multi-line `gcode:` value). `ds_middle_with_comments_and_continuation`,
   `ds_jinja_body`
2. **Kept:** a comment or blank line that follows the last key at column 0 is outside the
   section body and stays. `ds_tail_comment_kept`, `ds_trailing_comment_eof`. A comment
   directly above the header also stays. `ds_comment_between_blanks`
3. **Blank lines at the cut:** the blank run before the removed section and the blank run
   after it, when nothing but the removed text separated them, merge into **one** blank
   line, however many there were. `ds_blank_both_sides`, `ds_two_blank_before`,
   `ds_three_after`, `ds_three_before_three_after`, `ds_blank_before_only`,
   `ds_blank_after_only`, `ds_comment_kept_then_blank_runs_merge`, `ds_adjacent_sections`.
   If a comment sits between the two runs they stay separate (rule 2 cases).
4. **Cut at the file edges:** if the merged blank run would end up at the end of the file
   it is dropped (`ds_last_section`, `ds_eof_blank_after`, `ds_no_trailing_newline`), and
   if it would end up at the very start it is dropped too (`ds_first_section`). Deleting
   the only section leaves an empty file (`ds_only_section`).
5. **Name matching:** exact, case sensitive. `gcode_macro T1` does not touch
   `gcode_macro T10`. `ds_exact_name_not_prefix`, `ds_realistic_t_macros`
6. **Duplicates:** every section with that name is removed. `ds_duplicate_sections`
7. **Missing section:** no-op, exit code 0, file byte identical. `ds_absent_section`
8. **Header with a trailing comment** (`[x] ; note`) is matched normally.
   `ds_header_trailing_comment`
9. **CRLF** files keep CRLF on the remaining lines. `ds_crlf`

### Additions found while implementing T10 (fuzzed against live crudini)

10. **Deleting a section rewrites the whole file**, not just the cut:
    - every run of empty lines outside an option value becomes one (`ds_every_run_of_empty_lines_collapses`,
      `ds_empty_lines_between_options_collapse`);
    - empty lines at the end of the file are dropped wherever the cut was
      (`ds_trailing_empty_lines_are_dropped_wherever_the_cut_was`);
    - leading empty lines collapse to one, but if the cut removed the first line of the
      file the empty lines after it go (`ds_leading_empty_lines_collapse_to_one`,
      `ds_first_section`);
    - blank lines directly after a header go if that section has an option
      (`ds_blank_after_a_later_header_is_dropped`), and stay if it has none
      (`ds_blank_after_a_header_without_options_is_kept`).
11. **Blank lines inside a multi-line value belong to it**: they are kept when a
    continuation line follows, and the value is deleted with the key.
    `ds_blank_lines_inside_a_value`, `dk_blank_then_continuation_is_part_of_the_value`
12. **Whitespace-only lines are not empty lines**: they are kept at the end of the file
    and at a cut, but dropped right after a header with options.
    `ds_whitespace_only_line_*`
13. **Any successful run ends the file with a newline**, even when nothing was deleted;
    in a CRLF file the added terminator is CRLF.
14. **Key deletion does none of the clean-up in 10**: blank runs are untouched
    (`dk_keeps_blank_runs_and_trailing_blanks`). Key names match case-insensitively.
15. **Parse errors (exit 1, file untouched):** an indented line with no option above it since
    the last header or the start of the file, and a line starting with `[` that is not a
    complete header (`[b] junk`, `[]`, an indented `[b]`). The whole file is parsed first, so an error
    anywhere fails the command. See `ERROR_CASES`.
16. **Quirk, not copied:** `--del <section> <key>` crashes (Python traceback, exit 1) when the
    section has no own copy of the key but an option before the first section has it. The Python
    port raises `ConfigParseError` for that, before touching the file. If the section does
    have its own copy it works (`dk_own_copy_wins_over_a_global_option`).

### Known best-effort areas (not asserted against crudini)

- **CRLF files** with blank lines: crudini treats them differently from LF files (for
  example it keeps a final empty line). The simple `ds_crlf` case matches.
- **Duplicate section names** beyond the simple `ds_duplicate_sections*` fixtures. Klipper
  rejects duplicate sections, so real configs do not have them.
- Whitespace-only lines mixed with empty lines near a cut.

## `crudini --del <file> <section> <key>` (used by `remove_velocity`)

1. Removes the key line **and its continuation lines**. `dk_multiline_value`
2. Removes **every** occurrence of the key within that section. `dk_duplicate_keys`
3. Works with `:` or `=` delimiters. `dk_equals_delimiter`
4. A commented-out key (`#velocity: 1`) is not a match. `dk_commented_key_untouched`
5. The same key in another section is untouched. `dk_same_key_other_section_kept`
6. Missing key or missing section: no-op, exit code 0. `dk_absent_key`, `dk_absent_section`
7. Blank lines are **not** collapsed, so removing the only key leaves the blank line that
   followed it. `dk_only_key_blank_after`, `dk_last_key_before_blank`
8. A trailing inline comment on the key line goes with the key. `dk_middle_with_inline_comment`

## `sed` ranges in `comment_gcode_in_fluidd`

Comment: `/^\[gcode_macro T[0-9]\+\][[:space:]]*$/,/SWITCH_OF_EXTENDED_EXTRUDER INDEX/ s/^/# /`
Uncomment: `/^\# \[gcode_macro T[0-9]\+\]/,/SWITCH_OF_EXTENDED_EXTRUDER INDEX/ s/^# //`

1. The range starts at a `[gcode_macro T<digits>]` header and ends at the **first line
   containing** `SWITCH_OF_EXTENDED_EXTRUDER INDEX`, inclusive. `sr_comment_basic`
2. Ranges repeat: each new matching header after a closed range starts another.
   `sr_comment_basic`, `sr_uncomment_repeated_ranges`
3. **Hazard:** a range with no end marker runs to the **end of the file**, commenting out
   unrelated sections such as `[printer]`. `sr_comment_unterminated_runs_to_eof`,
   `sr_uncomment_unterminated_runs_to_eof`. The Python port reproduces this, it does not
   guard against it, until a reviewed decision says otherwise.
4. Comment prefixes every line in range with `# `, so blank lines become `# ` (with a
   trailing space) and already commented lines become `# # ...`.
   `sr_comment_blank_lines_in_range`, `sr_comment_already_commented_lines`
5. The header regex requires only whitespace after `]`, so trailing spaces are fine, and
   non `T<digits>` names such as `TEST` never start a range.
   `sr_comment_header_trailing_space`, `sr_comment_non_t_header_untouched`
6. Uncomment strips exactly one `# ` prefix, only inside the range, and leaves lines that
   do not start with `# ` alone. `sr_uncomment_only_prefix_stripped_once`,
   `sr_uncomment_basic`

## Other helpers

The Moonraker, include and value-edit helpers are simple line edits. They are covered by
direct before/after fixtures and unit tests rather than by the rules above.
