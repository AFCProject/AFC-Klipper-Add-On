import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

import pytest

from installer.install_flow.install import InstallOptions, install_afc
from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.unit_registry.selection import UnitSelection
from installer.update_flow.update import remove_t_macros, remove_velocity, update_afc
from tests.installer.fakes import FakeCommandRunner, RecordingConsole, ScriptedPrompter
from tests.installer.sandbox import (
    REPO_ROOT,
    Scenario,
    sandbox,
)

VERSION = "1.2.3-abc1234"
MACRO_FILES = sorted(p.name for p in (REPO_ROOT / "config" / "macros").glob("*.cfg"))
BUFFER_CFG = "[AFC_buffer B1]\nvelocity: 1\nadvance_pin: x\n\n[AFC_hub H]\nvelocity: 9\n"
T_MACROS = (
    "\n[gcode_macro T0]\ngcode:\n  M117 zero\n\n[gcode_macro T12]\ngcode:\n  M117 twelve\n"
    "\n[gcode_macro KEEP_ME]\ngcode:\n  M117 kept\n"
)
MODS = {"mods/buffer.cfg": BUFFER_CFG, "mods/t_macros.cfg": T_MACROS}
ANSWERS_NO = ["n"]
SKIP_ALL = ["y", "y"] + ["s"] * len(MACRO_FILES)


class TestUpdateAfc:
    def setup(self, tmp_path: Path, **kwargs: object) -> Context:
        home = sandbox(tmp_path, Scenario())
        ctx = Context.create(REPO_ROOT, home=home, **kwargs)  # type: ignore[arg-type]
        install_afc(
            ctx, RecordingConsole(), ScriptedPrompter([]), FakeCommandRunner(),
            InstallOptions(selection=UnitSelection()),
        )
        return ctx

    def update(
        self, ctx: Context, answers: List[str]
    ) -> "tuple[str, RecordingConsole, ScriptedPrompter]":
        console, prompter = RecordingConsole(), ScriptedPrompter(answers)
        text = update_afc(ctx, console, prompter, FakeCommandRunner(), VERSION)
        return text, console, prompter

    def test_the_two_questions_and_the_warnings_between_them(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        _text, console, prompter = self.update(ctx, ["y", "n"])
        assert prompter.prompts == [
            "Do you want to update the AFC provided macros? (y/n): ",
            "Please confirm you want to update these macros: (y/n): ",
        ]
        assert console.messages == [
            ("WARNING", "Updating macros will overwrite any custom changes you have made to "
                        "these macros."),
            ("WARNING", "This includes the brush, cut, kick, park, afc_macros, and poop macros."),
        ]

    def test_only_a_y_to_the_first_question_asks_the_second(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        _text, console, prompter = self.update(ctx, ["maybe"])
        assert len(prompter.prompts) == 1 and console.messages == []

    def test_the_exact_message_texts_and_their_indentation(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        text, _c, _p = self.update(ctx, ["y", "y"] + ["o"] * len(MACRO_FILES))
        assert text == (
            f"\nAFC Macros updated successfully ({len(MACRO_FILES)} copied, 0 skipped).\n"
            "          "
            f"\nAFC Klipper Add-On updated successfully with version v{VERSION}.\n"
        )

    def test_with_no_macro_files_in_the_checkout_it_says_so(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        empty = tmp_path / "afc_checkout"
        shutil.copytree(REPO_ROOT / "config", empty / "config")
        for cfg in (empty / "config" / "macros").glob("*.cfg"):
            cfg.unlink()
        (empty / "extras").mkdir()
        ctx.afc_path = empty
        text, _c, _p = self.update(ctx, ["y", "y"])
        assert text.startswith("\nNo macro files found to update.\n        ")

    def test_skipping_every_macro_file(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        text, _, _ = self.update(ctx, SKIP_ALL)
        count = len(MACRO_FILES)
        assert text.startswith(
            f"\nAFC Macros update skipped (all {count} files kept as-is).\n"
        )

    def test_overwriting_every_macro_file(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        text, _, _ = self.update(ctx, ["y", "y"] + ["o"] * len(MACRO_FILES))
        assert f"AFC Macros updated successfully ({len(MACRO_FILES)} copied, 0 skipped)." in text

    def test_a_mix_of_overwrite_and_skip_counts_both(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        choices = (["o", "s"] * len(MACRO_FILES))[: len(MACRO_FILES)]
        text, _, _ = self.update(ctx, ["y", "y"] + choices)
        copied = choices.count("o")
        skipped = len(MACRO_FILES) - copied
        assert f"AFC Macros updated successfully ({copied} copied, {skipped} skipped)." in text

    def test_files_are_flagged_as_updated(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        ctx.files_updated_or_installed = False
        self.update(ctx, ["n"])
        assert ctx.files_updated_or_installed is True

    def test_a_zip_install_does_not_touch_the_klipper_exclude_list(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path, git_install=False)
        exclude = ctx.klipper_dir / ".git" / "info" / "exclude"
        exclude.write_text("")
        self.update(ctx, ["n"])
        assert exclude.read_text() == ""

    def test_a_stray_init_symlink_into_afc_is_repaired(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        init = ctx.klipper_extra_path / "__init__.py"
        init.unlink()
        init.symlink_to(ctx.afc_path / "extras" / "__init__.py")
        git_checkout = (
            "git", "-C", str(ctx.klipper_dir), "checkout", "--", "klippy/extras/__init__.py"
        )
        failing = subprocess.CompletedProcess([], 128, stdout="", stderr="")
        runner = FakeCommandRunner(results={git_checkout: failing})
        update_afc(ctx, RecordingConsole(), ScriptedPrompter(["n"]), runner, VERSION)
        assert not init.is_symlink()
        assert init.read_text().startswith("# Package definition for the extras directory")

    def test_extensions_are_relinked(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        (ctx.klipper_extra_path / "AFC.py").unlink()
        self.update(ctx, ["n"])
        assert (ctx.klipper_extra_path / "AFC.py").is_symlink()

    def test_end_of_input_at_a_question_stops_the_installer(self, tmp_path: Path) -> None:
        ctx = self.setup(tmp_path)
        with pytest.raises(InstallerExit) as exc:
            self.update(ctx, [])
        assert exc.value.code == 1
        with pytest.raises(InstallerExit):
            self.update(ctx, ["y"])


class TestRemoveTMacros:
    def ctx(self, tmp_path: Path, text: Optional[str]) -> Context:
        ctx = Context.create(REPO_ROOT, home=tmp_path)
        if text is not None:
            path = ctx.afc_config_dir / "macros" / "AFC_macros.cfg"
            path.parent.mkdir(parents=True)
            path.write_text(text)
        return ctx

    def test_removes_every_t_number_macro_and_keeps_the_rest(self, tmp_path: Path) -> None:
        text = "[gcode_macro T0]\na: 1\n\n[gcode_macro T10]\nb: 2\n\n[keep]\nc: 3\n"
        ctx = self.ctx(tmp_path, text)
        remove_t_macros(ctx)
        assert (ctx.afc_config_dir / "macros" / "AFC_macros.cfg").read_text() == "[keep]\nc: 3\n"

    def test_a_t_token_elsewhere_names_a_section_that_does_not_exist_so_nothing_happens(
        self, tmp_path: Path
    ) -> None:
        text = "[gcode_macro START_T5X]\nz: 1\n"
        ctx = self.ctx(tmp_path, text)
        remove_t_macros(ctx)
        assert (ctx.afc_config_dir / "macros" / "AFC_macros.cfg").read_text() == text

    def test_a_missing_macros_file_is_fine(self, tmp_path: Path) -> None:
        remove_t_macros(self.ctx(tmp_path, None))

    def test_a_file_without_t_macros_is_untouched(self, tmp_path: Path) -> None:
        ctx = self.ctx(tmp_path, "[keep]\nc: 3\n")
        path = ctx.afc_config_dir / "macros" / "AFC_macros.cfg"
        before = path.stat().st_ino
        remove_t_macros(ctx)
        assert path.stat().st_ino == before


class TestRemoveVelocity:
    def ctx(self, tmp_path: Path, files: "dict[str, str]") -> Context:
        ctx = Context.create(REPO_ROOT, home=tmp_path)
        ctx.afc_config_dir.mkdir(parents=True)
        for name, text in files.items():
            (ctx.afc_config_dir / name).write_text(text)
        return ctx

    def test_removes_the_key_from_a_buffer_and_reports_the_file(self, tmp_path: Path) -> None:
        ctx = self.ctx(tmp_path, {"b.cfg": "[AFC_buffer B]\nvelocity: 1\nx: 2\n"})
        messages = remove_velocity(ctx)
        assert (ctx.afc_config_dir / "b.cfg").read_text() == "[AFC_buffer B]\nx: 2\n"
        assert messages == [
            f"\nRemoved deprecated velocity setting from {ctx.afc_config_dir / 'b.cfg'}.\n      "
        ]

    def test_every_buffer_section_in_a_file_is_cleaned_not_just_one(self, tmp_path: Path) -> None:
        text = "[AFC_buffer A]\nvelocity: 1\n\n[AFC_buffer B]\nvelocity: 2\nx: 1\n"
        ctx = self.ctx(tmp_path, {"b.cfg": text})
        remove_velocity(ctx)
        cleaned = (ctx.afc_config_dir / "b.cfg").read_text()
        assert cleaned == "[AFC_buffer A]\n\n[AFC_buffer B]\nx: 1\n"

    def test_files_without_a_buffer_section_are_never_touched(self, tmp_path: Path) -> None:
        ctx = self.ctx(tmp_path, {"hub.cfg": "[AFC_hub H]\nvelocity: 1\n"})
        assert remove_velocity(ctx) == []
        assert (ctx.afc_config_dir / "hub.cfg").read_text() == "[AFC_hub H]\nvelocity: 1\n"

    def test_no_cfg_files_at_all_is_nothing_to_do_not_an_abort(self, tmp_path: Path) -> None:
        assert remove_velocity(self.ctx(tmp_path, {})) == []

    def test_a_missing_directory_is_nothing_to_do(self, tmp_path: Path) -> None:
        assert remove_velocity(Context.create(REPO_ROOT, home=tmp_path)) == []

    def test_a_buffer_file_without_velocity_is_left_alone_and_unreported(
        self, tmp_path: Path
    ) -> None:
        ctx = self.ctx(tmp_path, {"b.cfg": "[AFC_buffer B]\nx: 2\n"})
        assert remove_velocity(ctx) == []

    def test_something_named_like_a_cfg_file_that_cannot_be_read_is_skipped(
        self, tmp_path: Path
    ) -> None:
        ctx = self.ctx(tmp_path, {"b.cfg": "[AFC_buffer B]\nvelocity: 1\n"})
        (ctx.afc_config_dir / "dir.cfg").mkdir()
        assert len(remove_velocity(ctx)) == 1

    def test_one_message_per_file_in_name_order(self, tmp_path: Path) -> None:
        files = {n: "[AFC_buffer B]\nvelocity: 1\n" for n in ("z.cfg", "a.cfg", "m.cfg")}
        messages = remove_velocity(self.ctx(tmp_path, files))
        assert [m.split("/")[-1].split(".cfg")[0] for m in messages] == ["a", "m", "z"]

    def test_the_detection_is_loose_so_a_velocity_in_another_section_still_reports(
        self, tmp_path: Path
    ) -> None:
        text = "[AFC_buffer B]\nx: 1\n\n[AFC_hub H]\nvelocity: 9\n"
        ctx = self.ctx(tmp_path, {"b.cfg": text})
        # the report says a removal happened even though nothing changed
        assert len(remove_velocity(ctx)) == 1
        assert (ctx.afc_config_dir / "b.cfg").read_text() == text
