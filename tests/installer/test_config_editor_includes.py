from pathlib import Path

import pytest

from installer.config_editor.document import ConfigDocument
from installer.config_editor.includes import (
    ensure_moonraker_update_block,
    manage_include,
    moonraker_update_block,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
INCLUDE = "[include AFC/*.cfg]"
SAVE = "#*# <---------------------- SAVE_CONFIG ---------------------->"


def write(tmp_path: Path, text: str, name: str = "printer.cfg") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode())
    return path


def inode(path: Path) -> int:
    return path.stat().st_ino


class TestAppendText:
    def doc(self, tmp_path: Path, content: bytes) -> ConfigDocument:
        path = tmp_path / "f.cfg"
        path.write_bytes(content)
        return ConfigDocument.load(path)

    def test_adds_whole_lines_after_a_terminated_file(self, tmp_path: Path) -> None:
        doc = self.doc(tmp_path, b"a\n")
        doc.append_text("b\nc\n")
        assert doc.lines == ["a\n", "b\n", "c\n"]

    def test_continues_an_unterminated_last_line(self, tmp_path: Path) -> None:
        doc = self.doc(tmp_path, b"a")
        doc.append_text("b\nc")
        assert doc.lines == ["ab\n", "c"]

    def test_empty_text_changes_nothing(self, tmp_path: Path) -> None:
        doc = self.doc(tmp_path, b"a")
        doc.append_text("")
        assert doc.lines == ["a"]

    def test_into_an_empty_document(self, tmp_path: Path) -> None:
        doc = self.doc(tmp_path, b"")
        doc.append_text("\nx\n")
        assert doc.lines == ["\n", "x\n"]


class TestManageIncludeAdd:
    def test_appends_when_there_is_no_save_config_block(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[printer]\nx: 1\n")
        manage_include(path, "add")
        assert path.read_text() == f"[printer]\nx: 1\n{INCLUDE}\n"

    def test_inserts_before_the_save_config_marker(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"[printer]\nx: 1\n\n{SAVE}\n#*# [stepper_x]\n")
        manage_include(path, "add")
        assert path.read_text() == f"[printer]\nx: 1\n\n{INCLUDE}\n{SAVE}\n#*# [stepper_x]\n"

    def test_inserts_before_every_marker_line(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"{SAVE}\nmid\n{SAVE}\n")
        manage_include(path, "add")
        assert path.read_text() == f"{INCLUDE}\n{SAVE}\nmid\n{INCLUDE}\n{SAVE}\n"

    def test_is_idempotent(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[printer]\n")
        manage_include(path, "add")
        manage_include(path, "add")
        assert path.read_text().count(INCLUDE) == 1

    def test_any_line_containing_the_statement_counts_as_present(self, tmp_path: Path) -> None:
        text = f"# disabled: {INCLUDE}\n"
        path = write(tmp_path, text)
        before = inode(path)
        manage_include(path, "add")
        assert path.read_text() == text
        assert inode(path) == before

    def test_a_marker_that_grep_f_would_not_find_means_append_not_insert(
        self, tmp_path: Path
    ) -> None:
        # only the exact marker line counts; a look-alike means append, not insert
        text = "[printer]\n# <---------------------- SAVE_CONFIG ---------------------->\n"
        path = write(tmp_path, text)
        manage_include(path, "add")
        assert path.read_text() == text + f"{INCLUDE}\n"

    def test_a_file_without_a_trailing_newline_gets_the_include_on_its_own_line(
        self, tmp_path: Path
    ) -> None:
        # the include must not be glued onto an unterminated last line
        path = write(tmp_path, "[printer]\nx: 1")
        manage_include(path, "add")
        assert path.read_text() == f"[printer]\nx: 1\n{INCLUDE}\n"

    def test_an_empty_file_just_gets_the_include(self, tmp_path: Path) -> None:
        path = write(tmp_path, "")
        manage_include(path, "add")
        assert path.read_text() == f"{INCLUDE}\n"


class TestManageIncludeOnAMissingFile:
    def test_add_creates_a_missing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "printer.cfg"
        manage_include(path, "add")
        assert path.read_text() == f"{INCLUDE}\n"

    def test_remove_has_nothing_to_do_and_does_not_create_it(self, tmp_path: Path) -> None:
        path = tmp_path / "printer.cfg"
        manage_include(path, "remove")
        assert not path.exists()


class TestManageIncludeRemove:
    def test_removes_the_statement_line(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"[printer]\n{INCLUDE}\n[mcu]\n")
        manage_include(path, "remove")
        assert path.read_text() == "[printer]\n[mcu]\n"

    def test_removes_every_line_that_contains_it(self, tmp_path: Path) -> None:
        path = write(tmp_path, f"a\n{INCLUDE}\n  {INCLUDE}  # x\nb # {INCLUDE}\nc\n")
        manage_include(path, "remove")
        assert path.read_text() == "a\nc\n"

    def test_without_the_statement_nothing_is_rewritten(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a\nb")
        before = inode(path)
        manage_include(path, "remove")
        assert path.read_bytes() == b"a\nb"
        assert inode(path) == before

    def test_a_rewritten_file_ends_with_a_newline_like_grep_output(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, f"{INCLUDE}\nlast")
        manage_include(path, "remove")
        assert path.read_bytes() == b"last\n"


class TestManageIncludeUnknownAction:
    def test_is_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a\n")
        with pytest.raises(ValueError, match="'toggle'"):
            manage_include(path, "toggle")
        assert path.read_text() == "a\n"


class TestMoonrakerUpdateBlock:
    def block(self, **overrides: str) -> str:
        args = dict(
            afc_path="/usr/data/AFC-Klipper-Add-On",
            gitrepo="https://example.com/repo.git",
            service="klipper",
            home="/home/pi",
        )
        args.update(overrides)
        return moonraker_update_block(**args)  # type: ignore[arg-type]

    def test_exact_text_for_a_checkout_outside_home(self) -> None:
        assert self.block() == (
            "\n[update_manager afc-software]\n"
            "type: git_repo\n"
            "path: /usr/data/AFC-Klipper-Add-On\n"
            "origin: https://example.com/repo.git\n"
            "managed_services: klipper\n"
            "primary_branch: main\n"
            "is_system_service: False\n"
            "info_tags:\n"
            "    desc=AFC Klipper Add On\n"
        )

    def test_a_checkout_under_home_is_written_with_a_tilde(self) -> None:
        text = self.block(afc_path="/home/pi/AFC-Klipper-Add-On")
        assert "\npath: ~/AFC-Klipper-Add-On\n" in text

    def test_a_sibling_directory_with_the_same_prefix_is_not_under_home(self) -> None:
        text = self.block(afc_path="/home/pi2/AFC", home="/home/pi")
        assert "\npath: /home/pi2/AFC\n" in text

    def test_home_itself_is_not_rewritten(self) -> None:
        assert "\npath: /home/pi\n" in self.block(afc_path="/home/pi")

    def test_the_service_name_and_repo_are_inserted(self) -> None:
        text = self.block(service="klipper-2", gitrepo="https://x/y.git")
        assert "managed_services: klipper-2\n" in text
        assert "origin: https://x/y.git\n" in text

    def test_the_primary_branch_is_always_main(self) -> None:
        assert "primary_branch: main\n" in self.block()


class TestEnsureMoonrakerUpdateBlock:
    BLOCK = "\n[update_manager afc-software]\ntype: git_repo\n"

    def test_appends_a_blank_line_then_the_block(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[server]\nhost: 0.0.0.0\n", "moonraker.conf")
        assert ensure_moonraker_update_block(path, self.BLOCK) is True
        assert path.read_text() == (
            "[server]\nhost: 0.0.0.0\n\n\n[update_manager afc-software]\ntype: git_repo\n"
        )

    def test_a_second_call_adds_nothing(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[server]\n", "moonraker.conf")
        ensure_moonraker_update_block(path, self.BLOCK)
        before = path.read_bytes()
        node = inode(path)
        assert ensure_moonraker_update_block(path, self.BLOCK) is False
        assert path.read_bytes() == before
        assert inode(path) == node

    def test_any_line_mentioning_the_section_counts(self, tmp_path: Path) -> None:
        text = "# [update_manager afc-software] is configured elsewhere\n"
        path = write(tmp_path, text, "moonraker.conf")
        assert ensure_moonraker_update_block(path, self.BLOCK) is False
        assert path.read_text() == text

    def test_an_unterminated_last_line_is_completed_first(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[server]\nhost: x", "moonraker.conf")
        ensure_moonraker_update_block(path, self.BLOCK)
        assert path.read_text() == (
            "[server]\nhost: x\n\n[update_manager afc-software]\ntype: git_repo\n"
        )

