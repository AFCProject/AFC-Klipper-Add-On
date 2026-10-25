import os
import shutil
import stat
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from installer.checks.checks import (
    check_existing_install,
    check_for_hh,
    check_for_zip_install,
    check_python_version,
    check_root,
)
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.platform_core.system import CommandRunner
from tests.installer.fakes import PrinterTree, RecordingConsole, ScriptedPrompter

REPO_ROOT = Path(__file__).resolve().parents[2]
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

PLAIN = "NAME=Debian\nID=debian\n"
K1 = "NAME=Buildroot\nID=buildroot\n"
SNAPMAKER = 'ID=buildroot\nRK_BUILD_INFO="snapmaker u1"\n'
HH_MESSAGES = [
    (
        "ERROR",
        "Happy Hare was found installed in your klipper extras. AFC is not currently "
        "compatible",
    ),
    (
        "ERROR",
        "with Happy Hare. Please remove it, and then re-run this install-afc.sh script.",
    ),
]


def make_ctx(tree: PrinterTree, **overrides: object) -> Context:
    return Context.create(tree.afc_path, home=tree.home, **overrides)  # type: ignore[arg-type]


def os_release(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "os-release"
    path.write_text(text)
    return path


class TestCheckRoot:
    @pytest.mark.parametrize("release", [PLAIN, K1, SNAPMAKER], ids=["plain", "k1", "snapmaker"])
    def test_a_normal_user_always_passes_silently(
        self, printer_tree: PrinterTree, tmp_path: Path, release: str
    ) -> None:
        console = RecordingConsole()
        check_root(make_ctx(printer_tree), console, 1000, os_release(tmp_path, release))
        assert console.messages == []

    @pytest.mark.parametrize("release", [PLAIN, SNAPMAKER], ids=["plain", "snapmaker"])
    def test_root_is_refused_on_plain_linux_and_on_snapmaker(
        self, printer_tree: PrinterTree, tmp_path: Path, release: str
    ) -> None:
        console = RecordingConsole()
        with pytest.raises(InstallerExit) as exc:
            check_root(make_ctx(printer_tree), console, 0, os_release(tmp_path, release))
        assert exc.value.code == 1
        assert console.messages == [("ERROR", "  Do not run as root, use a normal user")]

    def test_root_on_a_k1_is_allowed_with_a_warning(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        console = RecordingConsole()
        check_root(make_ctx(printer_tree), console, 0, os_release(tmp_path, K1))
        assert console.messages == [
            ("WARNING", "K1 OS detected, skipping root user check.")
        ]

    @pytest.mark.parametrize(
        "release, k1, snapmaker",
        [(PLAIN, False, False), (K1, True, False), (SNAPMAKER, True, True)],
        ids=["plain", "k1", "snapmaker"],
    )
    def test_the_detected_platform_is_recorded_on_the_context(
        self, printer_tree: PrinterTree, tmp_path: Path, release: str, k1: bool, snapmaker: bool
    ) -> None:
        ctx = make_ctx(printer_tree)
        check_root(ctx, RecordingConsole(), 1000, os_release(tmp_path, release))
        assert (ctx.is_k1_os, ctx.is_snapmaker) == (k1, snapmaker)

    def test_snapmaker_forces_the_system_python_directory(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        ctx = make_ctx(printer_tree, klipper_venv=Path("/custom"))
        check_root(ctx, RecordingConsole(), 1000, os_release(tmp_path, SNAPMAKER))
        assert ctx.klipper_venv == Path("/usr/bin")

    def test_the_effective_user_id_defaults_to_the_process_s(
        self, printer_tree: PrinterTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        with pytest.raises(InstallerExit):
            check_root(
                make_ctx(printer_tree), RecordingConsole(), None, os_release(tmp_path, PLAIN)
            )
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        check_root(make_ctx(printer_tree), RecordingConsole(), None, os_release(tmp_path, PLAIN))

    def test_a_missing_os_release_counts_as_plain_linux(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        with pytest.raises(InstallerExit):
            check_root(make_ctx(printer_tree), RecordingConsole(), 0, tmp_path / "nope")


class TestCheckForHh:
    def mmu(self, tree: PrinterTree) -> Path:
        return tree.klipper_extras / "mmu.py"

    def test_no_mmu_file_passes(self, printer_tree: PrinterTree) -> None:
        console = RecordingConsole()
        check_for_hh(make_ctx(printer_tree), console)
        assert console.messages == []

    def test_an_unrelated_mmu_file_passes(self, printer_tree: PrinterTree) -> None:
        self.mmu(printer_tree).write_text("# some other mmu module\n")
        console = RecordingConsole()
        check_for_hh(make_ctx(printer_tree), console)
        assert console.messages == []

    def test_happy_hare_is_a_hard_stop(self, printer_tree: PrinterTree) -> None:
        self.mmu(printer_tree).write_text("# Happy Hare MMU\n")
        console = RecordingConsole()
        with pytest.raises(InstallerExit) as exc:
            check_for_hh(make_ctx(printer_tree), console)
        assert exc.value.code == 1
        assert console.messages == HH_MESSAGES

    def test_the_text_may_appear_anywhere_even_in_a_binary_file(
        self, printer_tree: PrinterTree
    ) -> None:
        self.mmu(printer_tree).write_bytes(b"\xff\x00junk Happy Hare \xfe")
        with pytest.raises(InstallerExit):
            check_for_hh(make_ctx(printer_tree), RecordingConsole())

    def test_the_match_is_case_sensitive(self, printer_tree: PrinterTree) -> None:
        self.mmu(printer_tree).write_text("happy hare\n")
        check_for_hh(make_ctx(printer_tree), RecordingConsole())

    def test_a_directory_called_mmu_py_is_ignored(self, printer_tree: PrinterTree) -> None:
        self.mmu(printer_tree).mkdir()
        check_for_hh(make_ctx(printer_tree), RecordingConsole())

    @pytest.mark.skipif(IS_ROOT, reason="root can read anything")
    def test_an_unreadable_file_is_treated_as_not_happy_hare_like_grep_failing(
        self, printer_tree: PrinterTree
    ) -> None:
        path = self.mmu(printer_tree)
        path.write_text("Happy Hare")
        path.chmod(0)
        check_for_hh(make_ctx(printer_tree), RecordingConsole())

    def test_uses_the_klipper_dir_from_the_context(self, printer_tree: PrinterTree) -> None:
        other = printer_tree.home / "other_klipper" / "klippy" / "extras"
        other.mkdir(parents=True)
        (other / "mmu.py").write_text("Happy Hare")
        check_for_hh(make_ctx(printer_tree), RecordingConsole())
        with pytest.raises(InstallerExit):
            check_for_hh(
                make_ctx(printer_tree, klipper_dir=printer_tree.home / "other_klipper"),
                RecordingConsole(),
            )


def fake_python(directory: Path, output: str, exit_code: int = 0) -> Path:
    """An executable `python` that prints a fixed version line, for the version check."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "python"
    path.write_text(f"#!/bin/sh\necho '{output}'\nexit {exit_code}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class TestCheckPythonVersion:
    def run(
        self, tree: PrinterTree, venv: Path
    ) -> Tuple[List[Tuple[str, str]], Optional[int]]:
        console = RecordingConsole()
        ctx = make_ctx(tree, klipper_venv=venv)
        try:
            check_python_version(ctx, console, CommandRunner())
            return console.messages, None
        except InstallerExit as exc:
            return console.messages, exc.code

    @pytest.mark.parametrize("version", ["3.8", "3.9", "3.10", "3.13", "4.0", "10.1"])
    def test_versions_at_or_above_the_minimum_pass(
        self, printer_tree: PrinterTree, tmp_path: Path, version: str
    ) -> None:
        fake_python(tmp_path / "venv", version)
        messages, code = self.run(printer_tree, tmp_path / "venv")
        assert code is None
        assert messages == [("OTHER", f"Python version {version} is OK.")]

    @pytest.mark.parametrize("version", ["3.7", "3.0", "2.7", "1.9"])
    def test_older_versions_are_refused(
        self, printer_tree: PrinterTree, tmp_path: Path, version: str
    ) -> None:
        fake_python(tmp_path / "venv", version)
        messages, code = self.run(printer_tree, tmp_path / "venv")
        assert code == 1
        assert messages == [
            (
                "OTHER",
                f"Your available Klipper venv Python version {version} is too old. "
                "The BoxTurtle AFC software requires at least Python 3.8.",
            )
        ]

    def test_ten_is_newer_than_eight_not_older_as_a_string_compare_would_say(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        fake_python(tmp_path / "venv", "3.10")
        assert self.run(printer_tree, tmp_path / "venv")[1] is None

    def test_a_missing_python_names_the_path_and_the_dash_y_flag(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        messages, code = self.run(printer_tree, tmp_path / "no_venv")
        assert code == 1
        assert messages == [
            (
                "OTHER",
                f"Python not found at {tmp_path / 'no_venv' / 'python'}. Please double-check "
                "your klipper configuration or specify a directory with the -y flag.",
            )
        ]

    def test_a_python_that_is_not_executable_counts_as_missing(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        path = fake_python(tmp_path / "venv", "3.9")
        path.chmod(0o644)
        messages, code = self.run(printer_tree, tmp_path / "venv")
        assert code == 1
        assert messages[0][1].startswith("Python not found at ")

    def test_a_python_that_fails_cannot_be_judged(
        self, printer_tree: PrinterTree, tmp_path: Path
    ) -> None:
        fake_python(tmp_path / "venv", "3.9", exit_code=1)
        messages, code = self.run(printer_tree, tmp_path / "venv")
        assert code == 1
        assert messages == [("OTHER", "Failed to determine Python version")]

    @pytest.mark.parametrize("output", ["", "garbage", "3", "3.x", "x.9"])
    def test_unparseable_output_cannot_be_judged(
        self, printer_tree: PrinterTree, tmp_path: Path, output: str
    ) -> None:
        fake_python(tmp_path / "venv", output)
        messages, code = self.run(printer_tree, tmp_path / "venv")
        assert code == 1
        assert messages == [("OTHER", "Failed to determine Python version")]

    def test_the_real_interpreter_passes(self, printer_tree: PrinterTree) -> None:
        # whatever runs the tests is at least 3.8
        venv = Path(shutil.which("python3") or "/usr/bin/python3").parent
        console = RecordingConsole()
        ctx = make_ctx(printer_tree, klipper_venv=venv)
        runner = CommandRunner()
        if not os.access(venv / "python", os.X_OK):
            pytest.skip("no bare `python` next to python3")
        check_python_version(ctx, console, runner)
        assert console.messages[0][1].endswith("is OK.")


ZIP_WARNINGS = [
    ("WARNING", "AFC-Klipper-Add-On appears to have been installed via ZIP file."),
    ("WARNING", "Some features may not work as expected without a Git installation."),
    ("WARNING", "Any updates will require manual reinstallation via ZIP file."),
]


class TestCheckForZipInstall:
    def test_a_git_checkout_is_silent_and_never_prompts(self, printer_tree: PrinterTree) -> None:
        (printer_tree.afc_path / ".git").mkdir()
        console, prompter = RecordingConsole(), ScriptedPrompter([])
        ctx = make_ctx(printer_tree)
        check_for_zip_install(ctx, console, prompter)
        assert console.messages == []
        assert prompter.prompts == []
        assert ctx.git_install is True

    def test_it_never_turns_git_install_back_on(self, printer_tree: PrinterTree) -> None:
        (printer_tree.afc_path / ".git").mkdir()
        ctx = make_ctx(printer_tree, git_install=False)
        check_for_zip_install(ctx, RecordingConsole(), ScriptedPrompter([]))
        assert ctx.git_install is False

    def test_a_zip_install_warns_three_times_waits_for_enter_and_clears_the_flag(
        self, printer_tree: PrinterTree
    ) -> None:
        console, prompter = RecordingConsole(), ScriptedPrompter([""])
        ctx = make_ctx(printer_tree)
        check_for_zip_install(ctx, console, prompter)
        assert console.messages == ZIP_WARNINGS
        assert prompter.prompts == ["Press Enter to continue..."]
        assert ctx.git_install is False

    def test_any_typed_answer_continues(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        check_for_zip_install(ctx, RecordingConsole(), ScriptedPrompter(["whatever"]))
        assert ctx.git_install is False

    def test_a_dot_git_file_is_not_a_git_directory(self, printer_tree: PrinterTree) -> None:
        (printer_tree.afc_path / ".git").write_text("gitdir: elsewhere\n")
        ctx = make_ctx(printer_tree)
        check_for_zip_install(ctx, RecordingConsole(), ScriptedPrompter([""]))
        assert ctx.git_install is False

    def test_end_of_input_at_the_prompt_stops_the_installer(
        self, printer_tree: PrinterTree
    ) -> None:
        console = RecordingConsole()
        ctx = make_ctx(printer_tree)
        with pytest.raises(InstallerExit) as exc:
            check_for_zip_install(ctx, console, ScriptedPrompter([]))
        assert exc.value.code == 1
        assert console.messages == ZIP_WARNINGS
        assert ctx.git_install is False


class TestCheckExistingInstall:
    def link(self, tree: PrinterTree, name: str, target: Optional[Path] = None) -> None:
        (tree.klipper_extras / name).symlink_to(target or tree.afc_path / "extras" / name)

    def test_no_links_means_no_prior_installation(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        check_existing_install(ctx)
        assert ctx.prior_installation is False

    def test_one_linked_extension_is_enough(self, printer_tree: PrinterTree) -> None:
        self.link(printer_tree, "AFC_lane.py")
        ctx = make_ctx(printer_tree)
        check_existing_install(ctx)
        assert ctx.prior_installation is True

    def test_a_regular_file_with_the_same_name_is_not_a_link(
        self, printer_tree: PrinterTree
    ) -> None:
        (printer_tree.klipper_extras / "AFC.py").write_text("a copy, not a link")
        ctx = make_ctx(printer_tree)
        check_existing_install(ctx)
        assert ctx.prior_installation is False

    def test_a_dangling_link_still_counts_like_test_dash_L(
        self, printer_tree: PrinterTree
    ) -> None:
        self.link(printer_tree, "AFC.py", Path("/nowhere/at/all.py"))
        ctx = make_ctx(printer_tree)
        check_existing_install(ctx)
        assert ctx.prior_installation is True

    def test_a_link_to_a_file_that_is_not_one_of_ours_does_not_count(
        self, printer_tree: PrinterTree
    ) -> None:
        self.link(printer_tree, "klipper_own.py", Path("/x.py"))
        ctx = make_ctx(printer_tree)
        check_existing_install(ctx)
        assert ctx.prior_installation is False

    def test_a_stray_init_symlink_counts_as_installed(self, printer_tree: PrinterTree) -> None:
        (printer_tree.klipper_extras / "__init__.py").unlink()
        self.link(printer_tree, "__init__.py")
        ctx = make_ctx(printer_tree)
        check_existing_install(ctx)
        assert ctx.prior_installation is True

    def test_an_existing_true_is_never_reset(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        ctx.prior_installation = True
        check_existing_install(ctx)
        assert ctx.prior_installation is True

