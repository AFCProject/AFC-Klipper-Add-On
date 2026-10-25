import io
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from installer.platform_core.console import Console
from installer.platform_core.context import Context
from installer.platform_core.fsops import (
    backup_afc_config,
    remove_vars_tool_file,
    safe_copy,
)
from tests.installer.fakes import RecordingConsole, ScriptedPrompter

REPO_ROOT = Path(__file__).resolve().parents[2]
DATE = "20260101000000"
PROMPT = "  (o)verwrite / (s)kip / (b)ackup and copy? [o/s/b]: "


def make_ctx(tmp_path: Path) -> Context:
    return Context.create(tmp_path / "afc", home=tmp_path, backup_date=DATE)


def snapshot(root: Path) -> Dict[str, Optional[bytes]]:
    """Relative path -> bytes (None for directories) for everything under root."""
    result: Dict[str, Optional[bytes]] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        result[rel] = None if path.is_dir() and not path.is_symlink() else (
            os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        )
    return result


class TestSafeCopy:
    def test_file_to_a_new_path_copies_without_prompting(self, tmp_path: Path) -> None:
        src = tmp_path / "a.cfg"
        src.write_text("data")
        console, prompter = RecordingConsole(), ScriptedPrompter([])
        result = safe_copy(make_ctx(tmp_path), console, prompter, src, tmp_path / "b.cfg")
        assert result == "copied"
        assert (tmp_path / "b.cfg").read_text() == "data"
        assert prompter.prompts == []
        assert console.messages == []

    def test_file_into_an_existing_directory_lands_inside_it(self, tmp_path: Path) -> None:
        src = tmp_path / "a.cfg"
        src.write_text("data")
        dest = tmp_path / "out"
        dest.mkdir()
        result = safe_copy(
            make_ctx(tmp_path), RecordingConsole(), ScriptedPrompter([]), src, dest
        )
        assert result == "copied"
        assert (dest / "a.cfg").read_text() == "data"

    def test_directory_to_a_new_path_copies_the_whole_tree_and_symlinks(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "macros"
        (src / "sub").mkdir(parents=True)
        (src / "x.cfg").write_text("x")
        (src / "sub" / "y.cfg").write_text("y")
        (src / "link").symlink_to("x.cfg")
        safe_copy(
            make_ctx(tmp_path), RecordingConsole(), ScriptedPrompter([]), src, tmp_path / "new"
        )
        assert snapshot(tmp_path / "new") == snapshot(src)
        assert os.readlink(tmp_path / "new" / "link") == "x.cfg"

    def test_directory_into_an_existing_directory_lands_inside_it(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "macros"
        src.mkdir()
        (src / "x.cfg").write_text("x")
        dest = tmp_path / "AFC"
        dest.mkdir()
        safe_copy(make_ctx(tmp_path), RecordingConsole(), ScriptedPrompter([]), src, dest)
        assert (dest / "macros" / "x.cfg").read_text() == "x"

    def test_overwrite_replaces_the_file_and_reports_it(self, tmp_path: Path) -> None:
        src, dst = tmp_path / "a", tmp_path / "b"
        src.write_text("new")
        dst.write_text("old")
        console, prompter = RecordingConsole(), ScriptedPrompter(["o"])
        result = safe_copy(make_ctx(tmp_path), console, prompter, src, dst)
        assert result == "copied"
        assert dst.read_text() == "new"
        assert prompter.prompts == [PROMPT]
        assert console.messages == [
            ("WARNING", f"Destination already exists: {dst}"),
            ("INFO", f"Overwritten: {dst}"),
        ]

    def test_overwriting_a_directory_removes_stale_files_instead_of_merging(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "macros"
        src.mkdir()
        (src / "new.cfg").write_text("n")
        dest = tmp_path / "AFC"
        (dest / "macros").mkdir(parents=True)
        (dest / "macros" / "stale.cfg").write_text("s")
        safe_copy(make_ctx(tmp_path), RecordingConsole(), ScriptedPrompter(["o"]), src, dest)
        assert sorted(p.name for p in (dest / "macros").iterdir()) == ["new.cfg"]

    def test_skip_keeps_the_existing_file(self, tmp_path: Path) -> None:
        src, dst = tmp_path / "a", tmp_path / "b"
        src.write_text("new")
        dst.write_text("old")
        console = RecordingConsole()
        result = safe_copy(make_ctx(tmp_path), console, ScriptedPrompter(["s"]), src, dst)
        assert result == "skipped"
        assert dst.read_text() == "old"
        assert console.messages == [
            ("WARNING", f"Destination already exists: {dst}"),
            ("INFO", f"Skipped: {dst}"),
        ]

    def test_backup_renames_the_old_file_with_the_date_then_copies(
        self, tmp_path: Path
    ) -> None:
        src, dst = tmp_path / "a", tmp_path / "b"
        src.write_text("new")
        dst.write_text("old")
        console = RecordingConsole()
        result = safe_copy(make_ctx(tmp_path), console, ScriptedPrompter(["b"]), src, dst)
        backup = tmp_path / f"b.bak.{DATE}"
        assert result == "copied"
        assert dst.read_text() == "new"
        assert backup.read_text() == "old"
        assert console.messages == [
            ("WARNING", f"Destination already exists: {dst}"),
            ("INFO", f"Backed up to: {backup}"),
        ]

    # Overwrite is the probe: a mis-parsed answer would skip and leave "old", so only
    # a correctly normalized "o" can produce "new".
    @pytest.mark.parametrize("answer", ["O", " o ", "\to\t", " O"])
    def test_answers_are_case_insensitive_and_trimmed(
        self, tmp_path: Path, answer: str
    ) -> None:
        src, dst = tmp_path / "a", tmp_path / "b"
        src.write_text("new")
        dst.write_text("old")
        safe_copy(make_ctx(tmp_path), RecordingConsole(), ScriptedPrompter([answer]), src, dst)
        assert dst.read_text() == "new"

    def test_invalid_answer_reprompts_with_the_plain_hint(self, tmp_path: Path) -> None:
        src, dst = tmp_path / "a", tmp_path / "b"
        src.write_text("new")
        dst.write_text("old")
        console, prompter = RecordingConsole(), ScriptedPrompter(["x", "", "o"])
        result = safe_copy(make_ctx(tmp_path), console, prompter, src, dst)
        assert result == "copied"
        assert prompter.prompts == [PROMPT] * 3
        hint = ("OTHER", "  Invalid choice. Please enter o, s, or b.")
        assert console.messages == [
            ("WARNING", f"Destination already exists: {dst}"),
            hint,
            hint,
            ("INFO", f"Overwritten: {dst}"),
        ]

    def test_end_of_input_skips_and_says_why(self, tmp_path: Path) -> None:
        src, dst = tmp_path / "a", tmp_path / "b"
        src.write_text("new")
        dst.write_text("old")
        console = RecordingConsole()
        result = safe_copy(make_ctx(tmp_path), console, ScriptedPrompter([]), src, dst)
        assert result == "skipped"
        assert dst.read_text() == "old"
        assert console.messages == [
            ("WARNING", f"Destination already exists: {dst}"),
            ("INFO", f"No interactive input available; skipping: {dst}"),
        ]

    def test_conflict_is_reported_at_the_effective_path_inside_a_directory(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "AFC.cfg"
        src.write_text("new")
        dest = tmp_path / "AFC"
        dest.mkdir()
        (dest / "AFC.cfg").write_text("old")
        console = RecordingConsole()
        safe_copy(make_ctx(tmp_path), console, ScriptedPrompter(["s"]), src, dest)
        assert console.messages[0] == (
            "WARNING",
            f"Destination already exists: {dest / 'AFC.cfg'}",
        )
        assert (dest / "AFC.cfg").read_text() == "old"

    def test_overwriting_a_symlink_replaces_the_link_not_its_target(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "a"
        src.write_text("new")
        target = tmp_path / "target"
        target.write_text("precious")
        dst = tmp_path / "b"
        dst.symlink_to(target)
        safe_copy(make_ctx(tmp_path), RecordingConsole(), ScriptedPrompter(["o"]), src, dst)
        assert not dst.is_symlink()
        assert dst.read_text() == "new"
        assert target.read_text() == "precious"


def python_safe_copy(
    work: Path, src_rel: str, dst_rel: str, answers: List[str]
) -> Tuple[str, Dict[str, Optional[bytes]]]:
    out = io.StringIO()
    ctx = Context.create(work / "afc", home=work, backup_date=DATE)
    previous = os.getcwd()
    os.chdir(work)  # relative paths, so the messages show relative paths too
    try:
        safe_copy(ctx, Console(out), ScriptedPrompter(answers), Path(src_rel), Path(dst_rel))
    finally:
        os.chdir(previous)
    return out.getvalue(), snapshot(work)


def populate(work: Path, kind: str) -> Tuple[str, str]:
    """Create a scenario tree; returns (src_rel, dst_rel)."""
    if kind == "file":
        (work / "a.cfg").write_text("new")
        (work / "b.cfg").write_text("old")
        return "a.cfg", "b.cfg"
    if kind == "file_into_dir":
        (work / "a.cfg").write_text("new")
        (work / "out").mkdir()
        (work / "out" / "a.cfg").write_text("old")
        return "a.cfg", "out"
    (work / "macros" / "sub").mkdir(parents=True)
    (work / "macros" / "n.cfg").write_text("n")
    (work / "macros" / "sub" / "m.cfg").write_text("m")
    (work / "AFC" / "macros").mkdir(parents=True)
    (work / "AFC" / "macros" / "stale.cfg").write_text("s")
    return "macros", "AFC"


class TestBackupAfcConfig:
    def test_renames_the_afc_directory_with_the_backup_date(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        ctx.afc_config_dir.mkdir(parents=True)
        (ctx.afc_config_dir / "AFC.cfg").write_text("cfg")
        backup_afc_config(ctx)
        backup = ctx.printer_config_dir / f"AFC.backup.{DATE}"
        assert not ctx.afc_config_dir.exists()
        assert (backup / "AFC.cfg").read_text() == "cfg"

    def test_does_nothing_when_there_is_no_afc_directory(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        ctx.printer_config_dir.mkdir(parents=True)
        backup_afc_config(ctx)
        assert list(ctx.printer_config_dir.iterdir()) == []


class TestRemoveVarsToolFile:
    def test_removes_only_tool_files(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        ctx.afc_config_dir.mkdir(parents=True)
        for name in ("AFC.var.tool", "x.tool", "AFC.cfg", "AFC.var.unit", "tool"):
            (ctx.afc_config_dir / name).write_text("")
        remove_vars_tool_file(ctx)
        assert sorted(p.name for p in ctx.afc_config_dir.iterdir()) == [
            "AFC.cfg",
            "AFC.var.unit",
            "tool",
        ]

    def test_missing_directory_is_fine(self, tmp_path: Path) -> None:
        remove_vars_tool_file(make_ctx(tmp_path))

    def test_a_directory_named_like_a_tool_file_is_left_alone(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        (ctx.afc_config_dir / "weird.tool").mkdir(parents=True)
        remove_vars_tool_file(ctx)
        assert (ctx.afc_config_dir / "weird.tool").is_dir()


