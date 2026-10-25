import os
import stat
from pathlib import Path
from typing import List

import pytest

from installer.config_editor.document import ConfigDocument

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_FILES: List[Path] = sorted(
    p for d in ("config", "templates") for p in (REPO_ROOT / d).rglob("*") if p.is_file()
)

TRICKY = {
    "empty": b"",
    "just_newline": b"\n",
    "no_trailing_newline": b"[a]\nx: 1",
    "crlf": b"[a]\r\nx: 1\r\n",
    "mixed_endings": b"[a]\r\nx: 1\ny: 2\r\n",
    "lone_cr_is_not_a_line_break": b"a\rb\n",
    "blank_lines_and_trailing_spaces": b"\n\n[a]  \n\tx: 1 \t\n\n",
    "invalid_utf8": b"[a]\nx: \xff\xfe\n",
    "unicode": "[a]\nname: café ☃\n".encode("utf-8"),
    "form_feed_and_line_separator": "a\x0cb c\x0bd\n".encode("utf-8"),
    "autosave_block": (
        b"[a]\nx: 1\n\n#*# <---------------------- SAVE_CONFIG ---------------------->\n"
        b"#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.\n#*#\n"
    ),
    "nul_byte": b"a\x00b\n",
}


class TestLoad:
    def test_keeps_each_line_with_its_own_terminator(self, tmp_path: Path) -> None:
        path = tmp_path / "f.cfg"
        path.write_bytes(b"one\r\ntwo\nthree")
        doc = ConfigDocument.load(path)
        assert doc.lines == ["one\r\n", "two\n", "three"]
        assert doc.path == path

    def test_empty_file_has_no_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "f.cfg"
        path.write_bytes(b"")
        assert ConfigDocument.load(path).lines == []

    def test_only_newline_characters_split_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "f.cfg"
        path.write_bytes("a\x0cb c\rd\n".encode("utf-8"))
        assert ConfigDocument.load(path).lines == ["a\x0cb c\rd\n"]

    def test_a_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ConfigDocument.load(tmp_path / "nope.cfg")

    def test_text_property_joins_the_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "f.cfg"
        path.write_bytes(b"a\r\nb")
        assert ConfigDocument.load(path).text == "a\r\nb"


class TestRoundTrip:
    @pytest.mark.parametrize("name", sorted(TRICKY))
    def test_tricky_content_survives_byte_for_byte(self, tmp_path: Path, name: str) -> None:
        path = tmp_path / "f.cfg"
        path.write_bytes(TRICKY[name])
        ConfigDocument.load(path).save()
        assert path.read_bytes() == TRICKY[name]

    @pytest.mark.parametrize(
        "source", REPO_FILES, ids=[str(p.relative_to(REPO_ROOT)) for p in REPO_FILES]
    )
    def test_every_repo_config_and_template_file_round_trips(
        self, tmp_path: Path, source: Path
    ) -> None:
        copy = tmp_path / source.name
        copy.write_bytes(source.read_bytes())
        ConfigDocument.load(copy).save()
        assert copy.read_bytes() == source.read_bytes()

    def test_there_are_repo_files_to_check(self) -> None:
        assert len(REPO_FILES) > 40


class TestSave:
    def test_saves_modified_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "f.cfg"
        path.write_bytes(b"a\nb\n")
        doc = ConfigDocument.load(path)
        doc.lines[1] = "changed\n"
        doc.lines.append("new")
        doc.save()
        assert path.read_bytes() == b"a\nchanged\nnew"

    def test_permissions_are_preserved(self, tmp_path: Path) -> None:
        path = tmp_path / "f.cfg"
        path.write_bytes(b"a\n")
        path.chmod(0o640)
        doc = ConfigDocument.load(path)
        doc.lines.append("b\n")
        doc.save()
        assert stat.S_IMODE(path.stat().st_mode) == 0o640

    def test_leaves_no_temporary_files_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "f.cfg"
        path.write_bytes(b"a\n")
        ConfigDocument.load(path).save()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["f.cfg"]

    def test_unencodable_content_fails_before_touching_the_original(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "f.cfg"
        path.write_bytes(b"original\n")
        doc = ConfigDocument.load(path)
        doc.lines.append("\ud800 lone surrogate\n")
        with pytest.raises(UnicodeEncodeError):
            doc.save()
        assert path.read_bytes() == b"original\n"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["f.cfg"]

    def test_a_failed_replace_keeps_the_original_and_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "f.cfg"
        path.write_bytes(b"original\n")
        doc = ConfigDocument.load(path)
        doc.lines.append("more\n")

        def boom(src: str, dst: str) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="replace failed"):
            doc.save()
        assert path.read_bytes() == b"original\n"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["f.cfg"]

    def test_saving_over_a_symlink_replaces_the_link_like_sed_dash_i(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "target.cfg"
        target.write_bytes(b"a\n")
        link = tmp_path / "link.cfg"
        link.symlink_to(target)
        doc = ConfigDocument.load(link)
        doc.lines.append("b\n")
        doc.save()
        assert not link.is_symlink()
        assert link.read_bytes() == b"a\nb\n"
        assert target.read_bytes() == b"a\n"
