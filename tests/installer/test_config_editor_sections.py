import random
import shutil
from pathlib import Path
from typing import List, Optional

import pytest

from installer.config_editor.sections import ConfigParseError, delete_key, delete_section
from tests.installer.config_editor_cases import (
    CASES,
    ERROR_CASES,
    FIXTURE_DIR,
    Case,
)

needs_crudini = pytest.mark.skipif(shutil.which("crudini") is None, reason="needs crudini")
DELETE_CASES = [c for c in CASES if c.tool in ("del_section", "del_key")]


def apply(case: Case, path: Path) -> Optional[bool]:
    if case.tool == "del_section":
        delete_section(path, case.args[0])
        return None
    return delete_key(path, case.args[0], case.args[1])


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "f.cfg"
    path.write_bytes(text.encode())
    return path


class TestAgainstTheCommittedCrudiniFixtures:
    @pytest.mark.parametrize("case", DELETE_CASES, ids=[c.name for c in DELETE_CASES])
    def test_output_equals_what_real_crudini_produced(
        self, case: Case, tmp_path: Path
    ) -> None:
        path = tmp_path / "f.cfg"
        path.write_bytes((FIXTURE_DIR / f"{case.name}.in").read_bytes())
        apply(case, path)
        assert path.read_bytes() == (FIXTURE_DIR / f"{case.name}.out").read_bytes()

    def test_there_are_enough_fixture_cases_to_mean_something(self) -> None:
        assert len(DELETE_CASES) > 55


class TestParseErrors:
    @pytest.mark.parametrize("case", ERROR_CASES, ids=[c.name for c in ERROR_CASES])
    def test_inputs_crudini_rejects_raise_and_leave_the_file_alone(
        self, case: Case, tmp_path: Path
    ) -> None:
        path = write(tmp_path, case.text)
        with pytest.raises(ConfigParseError):
            apply(case, path)
        assert path.read_bytes() == case.text.encode()

    def test_the_message_names_the_file_and_line(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[s]\nx: 1\n[t]\n  orphan\n")
        with pytest.raises(ConfigParseError) as exc:
            delete_section(path, "s")
        assert str(exc.value) == (
            f"{path}: line 4: indented line with no option above it in its section"
        )

    def test_is_a_value_error_so_callers_can_catch_it_generically(self) -> None:
        assert issubclass(ConfigParseError, ValueError)


class TestFinish:
    def test_an_unterminated_last_line_gets_a_newline_in_the_files_own_style(
        self, tmp_path: Path
    ) -> None:
        lf = write(tmp_path, "[a]\nx: 1\n[b]\ny: 2")
        delete_section(lf, "a")
        assert lf.read_bytes() == b"[b]\ny: 2\n"
        crlf = write(tmp_path, "[a]\r\nx: 1\r\n[b]\r\ny: 2")
        delete_section(crlf, "a")
        assert crlf.read_bytes() == b"[b]\r\ny: 2\r\n"

    def test_a_terminated_file_with_nothing_to_delete_is_left_alone(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[b]\ny: 2\n")
        delete_section(path, "a")
        assert path.read_bytes() == b"[b]\ny: 2\n"


class TestDeleteSection:
    def test_a_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            delete_section(tmp_path / "nope.cfg", "a")

    def test_nothing_to_delete_does_not_rewrite_the_file(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[a]\nx: 1\n")
        before = path.stat().st_ino
        delete_section(path, "zz")
        assert path.stat().st_ino == before

    def test_permissions_survive(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[a]\nx: 1\n\n[b]\ny: 2\n")
        path.chmod(0o640)
        delete_section(path, "b")
        assert path.stat().st_mode & 0o777 == 0o640

    def test_crlf_line_endings_of_the_remaining_lines_are_kept(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[a]\r\nx: 1\r\n[b]\r\ny: 2\r\n[c]\r\nz: 3\r\n")
        delete_section(path, "b")
        assert path.read_bytes() == b"[a]\r\nx: 1\r\n[c]\r\nz: 3\r\n"


class TestDeleteKey:
    def test_returns_true_when_something_was_removed(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[s]\nvelocity: 1\n")
        assert delete_key(path, "s", "velocity") is True
        assert path.read_text() == "[s]\n"

    @pytest.mark.parametrize(
        "text",
        ["[s]\nx: 1\n", "[t]\nvelocity: 1\n", "x: 1\n"],
        ids=["absent_key", "other_section", "no_section"],
    )
    def test_returns_false_and_does_not_rewrite_when_nothing_matches(
        self, tmp_path: Path, text: str
    ) -> None:
        path = write(tmp_path, text)
        before = path.stat().st_ino
        assert delete_key(path, "s", "velocity") is False
        assert path.read_text() == text
        assert path.stat().st_ino == before

    def test_a_same_named_option_before_the_first_section_is_an_error_like_crudini(
        self, tmp_path: Path
    ) -> None:
        text = "#h\nVelocity: 1\n[a]\nx: 1\n"
        path = write(tmp_path, text)
        with pytest.raises(ConfigParseError, match="only exists before the first section"):
            delete_key(path, "a", "velocity")
        assert path.read_text() == text

    def test_a_global_option_does_not_matter_when_the_section_has_its_own_copy(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, "velocity: 1\n[a]\nx: 2\nvelocity: 3\n")
        assert delete_key(path, "a", "velocity") is True
        assert path.read_text() == "velocity: 1\n[a]\nx: 2\n"

    def test_the_global_option_only_matters_when_the_section_exists(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, "velocity: 1\n[b]\nx: 1\n")
        assert delete_key(path, "a", "velocity") is False

    def test_a_global_option_with_a_different_name_is_harmless(self, tmp_path: Path) -> None:
        path = write(tmp_path, "other: 1\n[a]\nvelocity: 2\n")
        assert delete_key(path, "a", "velocity") is True
        assert path.read_text() == "other: 1\n[a]\n"

    def test_an_option_before_any_section_is_never_targeted(self, tmp_path: Path) -> None:
        path = write(tmp_path, "velocity: 1\n[s]\nx: 1\n")
        assert delete_key(path, "", "velocity") is False
        assert path.read_text() == "velocity: 1\n[s]\nx: 1\n"

    def test_removes_the_key_in_every_section_with_that_name(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[s]\nvelocity: 1\n[t]\nvelocity: 2\n[s]\nvelocity: 3\nx: 4\n")
        assert delete_key(path, "s", "velocity") is True
        assert path.read_text() == "[s]\n[t]\nvelocity: 2\n[s]\nx: 4\n"

    def test_a_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            delete_key(tmp_path / "nope.cfg", "s", "k")


# --- seeded differential fuzzing against the real tool ------------------------------------

SECTION_NAMES = ["a", "b", "gcode_macro T0", "AFC_buffer B", "S"]
KEY_NAMES = ["velocity", "Velocity", "x", "gcode", "pin tool"]


def random_config(rng: random.Random) -> str:
    """
    A random config built from shapes seen in Klipper files, some of them invalid.

    Section names are never repeated: Klipper rejects duplicate sections, and crudini's
    handling of them (via iniparse) is only pinned by the simple fixture cases.
    """
    eol = "\n"  # CRLF files are best effort, see NOTES.md
    lines: List[str] = []
    unused = list(SECTION_NAMES)
    rng.shuffle(unused)
    for _ in range(rng.randint(1, 14)):
        kind = rng.choice(
            ["header", "header", "key", "key", "key", "cont", "cont", "blank", "blank",
             "comment", "semi", "icomment", "nodelim", "header_comment", "header_space"]
        )
        if kind == "header":
            lines.append(f"[{unused.pop()}]" if unused else "")
        elif kind == "header_comment":
            lines.append(f"[{unused.pop()}] ; note" if unused else "")
        elif kind == "header_space":
            lines.append(f"[{unused.pop()}]  " if unused else "")
        elif kind == "key":
            delim = rng.choice([": ", ":", " : ", "= ", " = "])
            lines.append(f"{rng.choice(KEY_NAMES)}{delim}{rng.choice(['1', 'True', '^p:PA1'])}")
        elif kind == "cont":
            lines.append(rng.choice(["  M117 hi", "\tM118", "    # indented", "  x: y"]))
        elif kind == "blank":
            lines.append("")  # whitespace-only lines are best effort, see NOTES.md
        elif kind == "comment":
            lines.append(f"# {rng.choice(['note', 'velocity: 9', ''])}")
        elif kind == "semi":
            lines.append("; semi")
        elif kind == "icomment":
            lines.append("  # c")
        else:
            lines.append(rng.choice(["velocity", "lonely"]))
    text = eol.join(lines)
    return text + (eol if rng.random() < 0.85 else "")


