from pathlib import Path

import pytest

from installer.config_editor.edits import (
    remove_tip_cut_servo_section,
    ensure_prep_section,
    ensure_trailing_newline,
    replace_text,
    replace_text_in_tree,
    replace_varfile_path,
    set_tool_start_pin,
    set_value,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def write(tmp_path: Path, text: str, name: str = "f.cfg") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode())
    return path


def inode(path: Path) -> int:
    return path.stat().st_ino


SET_VALUE_CORPUS = "".join(
    line + "\n"
    for line in [
        "[AFC]",
        "park: True",
        "  park:   True   # indented, comment kept",
        "\tpoop:\tTrue",
        "park: True#glued",
        "park: True  #  spaced   comment  ",
        "park : True",
        "park:True",
        "park: two tokens",
        "parking: True",
        "#park: True",
        "; park: True",
        "park:",
        "park: True\r",
        "park: True # c\r",
        "park\u00a0: True",
        "park:\u00a0True",
        "park:\x0bTrue\x0c# vt and ff are whitespace",
        "park:\u2003True",
        "other: park: True",
        "",
        "[second]",
        "poop: True",
        "form_tip: False",
        "tool_cut: True  # note",
    ]
)


class TestSetValue:
    def test_rewrites_the_value_and_leaves_a_trailing_space(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, "park: True\n")
        set_value(path, "park", "False")
        assert path.read_bytes() == b"park: False \n"

    def test_keeps_the_trailing_comment(self, tmp_path: Path) -> None:
        path = write(tmp_path, "park: True  # keep me\n")
        set_value(path, "park", "False")
        assert path.read_bytes() == b"park: False # keep me\n"

    def test_indentation_is_dropped_on_a_rewritten_line(self, tmp_path: Path) -> None:
        path = write(tmp_path, "  park: True\n")
        set_value(path, "park", "False")
        assert path.read_bytes() == b"park: False \n"

    def test_every_matching_line_in_every_section_is_rewritten(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[a]\npark: 1\n[b]\npark: 2\n")
        set_value(path, "park", "x")
        assert path.read_bytes() == b"[a]\npark: x \n[b]\npark: x \n"

    def test_lines_that_do_not_match_are_untouched(self, tmp_path: Path) -> None:
        text = "parking: 1\n#park: 1\npark: two tokens\npark:\nxpark: 1\n"
        path = write(tmp_path, text)
        set_value(path, "park", "x")
        assert path.read_text() == text

    def test_the_key_is_literal_not_a_regex(self, tmp_path: Path) -> None:
        path = write(tmp_path, "aXb: 1\na.b: 2\n")
        set_value(path, "a.b", "z")
        assert path.read_bytes() == b"aXb: 1\na.b: z \n"

    def test_a_file_with_no_match_is_not_rewritten(self, tmp_path: Path) -> None:
        path = write(tmp_path, "nothing: here\n")
        before = inode(path)
        set_value(path, "park", "x")
        assert inode(path) == before

    def test_a_matching_last_line_without_a_newline_is_kept_not_dropped(
        self, tmp_path: Path
    ) -> None:
        # an unterminated final line must be kept, not silently dropped
        path = write(tmp_path, "a: 1\npark: True")
        set_value(path, "park", "False")
        assert path.read_bytes() == b"a: 1\npark: False \n"

    def test_an_unmatched_last_line_without_a_newline_stays_unterminated(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, "park: True\nlast: 1")
        set_value(path, "park", "False")
        assert path.read_bytes() == b"park: False \nlast: 1"

    def test_file_permissions_are_kept(self, tmp_path: Path) -> None:
        path = write(tmp_path, "park: True\n")
        path.chmod(0o640)
        set_value(path, "park", "False")
        assert path.stat().st_mode & 0o777 == 0o640


class TestSetToolStartPin:
    TEXT = (
        "[AFC_extruder extruder]\n"
        "pin_tool_end: x\n"
        "pin_tool_start: buffer  # old comment\n"
        "pin_tool_start: second\n"
        "\n"
        "[other]\n"
        "pin_tool_start: untouched\n"
    )

    def test_replaces_only_the_first_pin_in_the_extruder_section(self, tmp_path: Path) -> None:
        path = write(tmp_path, self.TEXT)
        set_tool_start_pin(path, "nhk:gpio13")
        assert path.read_text() == (
            "[AFC_extruder extruder]\n"
            "pin_tool_end: x\n"
            "pin_tool_start: nhk:gpio13\n"
            "pin_tool_start: second\n"
            "\n"
            "[other]\n"
            "pin_tool_start: untouched\n"
        )

    def test_no_extruder_section_means_no_change(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[AFC_extruder other]\npin_tool_start: a\n")
        before = inode(path)
        set_tool_start_pin(path, "x")
        assert path.read_text() == "[AFC_extruder other]\npin_tool_start: a\n"
        assert inode(path) == before

    def test_the_header_must_be_the_whole_line(self, tmp_path: Path) -> None:
        text = "[AFC_extruder extruder] # c\npin_tool_start: a\n"
        path = write(tmp_path, text)
        set_tool_start_pin(path, "x")
        assert path.read_text() == text

    def test_a_pin_before_the_header_is_not_touched(self, tmp_path: Path) -> None:
        path = write(tmp_path, "pin_tool_start: a\n[AFC_extruder extruder]\npin_tool_start: b\n")
        set_tool_start_pin(path, "x")
        assert path.read_text() == "pin_tool_start: a\n[AFC_extruder extruder]\npin_tool_start: x\n"

    def test_a_section_without_the_pin_lets_the_next_section_pin_be_replaced(
        self, tmp_path: Path
    ) -> None:
        # the scan never leaves the section on a new header, so it rewrites a later pin
        path = write(tmp_path, "[AFC_extruder extruder]\nx: 1\n[other]\npin_tool_start: a\n")
        set_tool_start_pin(path, "z")
        assert path.read_text() == (
            "[AFC_extruder extruder]\nx: 1\n[other]\npin_tool_start: z\n"
        )

    def test_the_prefix_match_does_not_need_a_value_or_space(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[AFC_extruder extruder]\npin_tool_start:\n")
        set_tool_start_pin(path, "v")
        assert path.read_text() == "[AFC_extruder extruder]\npin_tool_start: v\n"


class TestEnsurePrepSection:
    def test_appends_a_blank_line_header_and_enable(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[a]\nx: 1\n")
        ensure_prep_section(path)
        assert path.read_bytes() == b"[a]\nx: 1\n\n[AFC_prep]\nenable: True\n"

    def test_a_file_without_a_trailing_newline_gets_one_but_no_blank_line(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, "[a]\nx: 1")
        ensure_prep_section(path)
        assert path.read_bytes() == b"[a]\nx: 1\n[AFC_prep]\nenable: True\n"

    def test_an_empty_file_gets_just_the_section(self, tmp_path: Path) -> None:
        path = write(tmp_path, "")
        ensure_prep_section(path)
        assert path.read_bytes() == b"\n[AFC_prep]\nenable: True\n"

    def test_any_line_containing_the_header_text_counts_as_present(
        self, tmp_path: Path
    ) -> None:
        text = "# was [AFC_prep] once\n"
        path = write(tmp_path, text)
        before = inode(path)
        ensure_prep_section(path)
        assert path.read_text() == text
        assert inode(path) == before

    def test_running_twice_adds_it_once(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[a]\n")
        ensure_prep_section(path)
        ensure_prep_section(path)
        assert path.read_text().count("[AFC_prep]") == 1


class TestReplaceVarfilePath:
    OLD = "VarFile: ../printer_data/config/AFC/AFC.var"

    def test_rewrites_the_old_default_path(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"[AFC]\n{self.OLD}\n")
        replace_varfile_path(path, Path("/home/u/printer_data/config/AFC"))
        assert path.read_text() == "[AFC]\nVarFile: /home/u/printer_data/config/AFC/AFC.var\n"

    def test_a_custom_varfile_line_is_left_alone(self, tmp_path: Path) -> None:
        text = "VarFile: /custom/AFC.var\n"
        path = write(tmp_path, text)
        before = inode(path)
        replace_varfile_path(path, Path("/x/AFC"))
        assert path.read_text() == text
        assert inode(path) == before

    def test_only_the_first_match_on_a_line_is_replaced(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"{self.OLD} {self.OLD}\n")
        replace_varfile_path(path, Path("/n"))
        assert path.read_text() == f"VarFile: /n/AFC.var {self.OLD}\n"

    def test_every_line_with_the_old_path_is_replaced(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"{self.OLD}\n{self.OLD}\n")
        replace_varfile_path(path, Path("/n"))
        assert path.read_text() == "VarFile: /n/AFC.var\nVarFile: /n/AFC.var\n"


class TestReplaceText:
    def test_replaces_every_occurrence_on_every_line(self, tmp_path: Path) -> None:
        path = write(tmp_path, "Turtle_1 Turtle_1\nx\nTurtle_1\n")
        replace_text(path, "Turtle_1", "Bob")
        assert path.read_text() == "Bob Bob\nx\nBob\n"

    def test_the_search_text_is_literal_not_a_regex(self, tmp_path: Path) -> None:
        path = write(tmp_path, "include mcu/AFC_Lite.cfg\nAFCXLiteXcfg\n")
        replace_text(path, "AFC_Lite.cfg", "N.cfg")
        assert path.read_text() == "include mcu/N.cfg\nAFCXLiteXcfg\n"

    def test_a_file_with_nothing_to_replace_is_not_rewritten(self, tmp_path: Path) -> None:
        path = write(tmp_path, "nothing\n")
        before = inode(path)
        replace_text(path, "zzz", "y")
        assert inode(path) == before

    def test_an_unterminated_last_line_stays_unterminated(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a\nTurtle_1")
        replace_text(path, "Turtle_1", "B")
        assert path.read_bytes() == b"a\nB"

    def test_crlf_line_endings_are_preserved(self, tmp_path: Path) -> None:
        path = write(tmp_path, "T\r\nT\r\n")
        replace_text(path, "T", "U")
        assert path.read_bytes() == b"U\r\nU\r\n"

    def test_an_empty_search_text_is_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, "abc\n")
        with pytest.raises(ValueError, match="empty"):
            replace_text(path, "", "x")
        assert path.read_text() == "abc\n"


class TestReplaceTextInTree:
    def test_rewrites_every_regular_file_recursively(self, tmp_path: Path) -> None:
        (tmp_path / "mcu").mkdir()
        write(tmp_path, "Turtle_1\n", "AFC.cfg")
        write(tmp_path / "mcu", "mcu: Turtle_1\n", "t.cfg")
        write(tmp_path, "AFC var Turtle_1 data\n", "AFC.var.unit")
        replace_text_in_tree(tmp_path, "Turtle_1", "Bob")
        assert (tmp_path / "AFC.cfg").read_text() == "Bob\n"
        assert (tmp_path / "mcu" / "t.cfg").read_text() == "mcu: Bob\n"
        assert (tmp_path / "AFC.var.unit").read_text() == "AFC var Bob data\n"

    def test_binary_files_survive_with_their_bytes(self, tmp_path: Path) -> None:
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"\xff\xfe Turtle_1 \x00\x80")
        replace_text_in_tree(tmp_path, "Turtle_1", "Bob")
        assert blob.read_bytes() == b"\xff\xfe Bob \x00\x80"

    def test_symlinks_are_not_followed_like_find_type_f(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        target = write(outside, "Turtle_1\n", "t.cfg")
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "link.cfg").symlink_to(target)
        replace_text_in_tree(tree, "Turtle_1", "Bob")
        assert target.read_text() == "Turtle_1\n"
        assert (tree / "link.cfg").is_symlink()

    def test_files_without_a_match_keep_their_identity(self, tmp_path: Path) -> None:
        path = write(tmp_path, "nothing\n")
        before = inode(path)
        replace_text_in_tree(tmp_path, "zzz", "y")
        assert inode(path) == before

    def test_a_missing_directory_is_a_no_op(self, tmp_path: Path) -> None:
        replace_text_in_tree(tmp_path / "nope", "a", "b")


class TestEnsureTrailingNewline:
    @pytest.mark.parametrize(
        "before, after",
        [(b"a", b"a\n"), (b"a\n", b"a\n"), (b"", b""), (b"a\r", b"a\r\n"), (b"a\r\n", b"a\r\n")],
    )
    def test_adds_a_newline_only_when_the_last_line_lacks_one(
        self, tmp_path: Path, before: bytes, after: bytes
    ) -> None:
        path = tmp_path / "f"
        path.write_bytes(before)
        ensure_trailing_newline(path)
        assert path.read_bytes() == after

    def test_a_terminated_file_is_not_rewritten(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a\n")
        before = inode(path)
        ensure_trailing_newline(path)
        assert inode(path) == before




RULE = "#--=================================================================================--#"
TITLE = "######### Tip Cut Servo ###############################################################"
EXAMPLE = f"{RULE}\n{TITLE}\n{RULE}\n#[servo tool_cut]\n# Pin for the cut pin servo.\n#pin: X\n"


class TestRemoveTipCutServoSection:
    def test_the_example_at_the_end_goes_with_the_blank_line_before_it(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, f"[a]\nx: 1\n\n{EXAMPLE}")
        assert remove_tip_cut_servo_section(path) is True
        assert path.read_text() == "[a]\nx: 1\n"

    def test_text_after_the_example_is_kept_and_blank_lines_are_not_doubled(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, f"[a]\nx: 1\n\n{EXAMPLE}\n[b]\ny: 2\n")
        remove_tip_cut_servo_section(path)
        assert path.read_text() == "[a]\nx: 1\n\n[b]\ny: 2\n"

    def test_the_example_with_no_blank_line_before_it(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"[a]\nx: 1\n{EXAMPLE}\n[b]\ny: 2\n")
        remove_tip_cut_servo_section(path)
        assert path.read_text() == "[a]\nx: 1\n\n[b]\ny: 2\n"

    def test_a_servo_section_the_user_wrote_is_left_alone(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"[a]\nx: 1\n\n{EXAMPLE}[servo tool_cut]\npin: PA1\n")
        remove_tip_cut_servo_section(path)
        assert path.read_text() == "[a]\nx: 1\n\n[servo tool_cut]\npin: PA1\n"

    def test_a_bare_title_without_the_rules_is_removed_with_its_comments(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, f"[a]\nx: 1\n{TITLE}\n#[servo tool_cut]\n#pin: X\n")
        remove_tip_cut_servo_section(path)
        assert path.read_text() == "[a]\nx: 1\n"

    def test_a_file_without_the_example_is_untouched(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[a]\nx: 1\n#[servo tool_cut]\n")
        assert remove_tip_cut_servo_section(path) is False
        assert path.read_bytes() == b"[a]\nx: 1\n#[servo tool_cut]\n"

    def test_windows_line_endings_are_kept(self, tmp_path: Path) -> None:
        text = f"[a]\r\nx: 1\r\n\r\n{EXAMPLE}".replace("\n", "\r\n").replace("\r\r", "\r")
        path = write(tmp_path, text)
        remove_tip_cut_servo_section(path)
        assert path.read_bytes() == b"[a]\r\nx: 1\r\n"

    def test_the_title_on_the_first_line_leaves_an_empty_file(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"{TITLE}\n#[servo tool_cut]\n")
        remove_tip_cut_servo_section(path)
        assert path.read_text() == ""

    @pytest.mark.parametrize(
        "template", ["AFC", "HTLF", "NightOwl", "QuattroBox"],
    )
    def test_the_shipped_hardware_templates(self, tmp_path: Path, template: str) -> None:
        source = REPO_ROOT / "templates" / f"AFC_Hardware-{template}.cfg"
        path = write(tmp_path, source.read_text())
        assert remove_tip_cut_servo_section(path) is True
        text = path.read_text()
        assert "Tip Cut Servo" not in text and "tool_cut" not in text
        assert text.endswith("\n") and not text.endswith("\n\n")
        assert source.read_text().startswith(text.rstrip("\n"))

    @pytest.mark.parametrize("template", ["AFC_Hardware-MMB.cfg", "AFC_Hardware_U1.cfg"])
    def test_templates_without_the_example_are_untouched(
        self, tmp_path: Path, template: str
    ) -> None:
        source = REPO_ROOT / "templates" / template
        path = write(tmp_path, source.read_text())
        assert remove_tip_cut_servo_section(path) is False
        assert path.read_bytes() == source.read_bytes()
