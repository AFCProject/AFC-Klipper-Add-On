"""Tests for sharing an existing buffer when adding an additional unit."""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]

PRELUDE = r"""
source "$REPO_ROOT/include/units/registry.sh"
source "$REPO_ROOT/include/constants.sh"
export afc_path="$REPO_ROOT"
source "$REPO_ROOT/include/colors.sh"
source "$REPO_ROOT/include/utils.sh"
source "$REPO_ROOT/include/check_commands.sh"
source "$REPO_ROOT/include/buffer_configurations.sh"
source "$REPO_ROOT/include/emu_templater.sh"
source "$REPO_ROOT/include/install_functions.sh"
source "$REPO_ROOT/include/unit_functions.sh"
source "$REPO_ROOT/include/menus/install_menu.sh"
source "$REPO_ROOT/include/menus/additional_system_menu.sh"
afc_config_dir="$WORKDIR/AFC"
mkdir -p "$afc_config_dir/mcu" "$afc_config_dir/macros"
"""


def run_bash(
    body: str, stdin: str = "", extra_env: Optional[dict] = None
) -> subprocess.CompletedProcess:
    env = {
        "REPO_ROOT": str(REPO_ROOT),
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "TERM": "dumb",
    }
    env.update(extra_env or {})
    script = 'WORKDIR=$(mktemp -d); trap \'rm -rf "$WORKDIR"\' EXIT\n' + PRELUDE + body
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=15,
    )


def seed(*files: str) -> str:
    """Bash that writes each 'name::content' pair into $afc_config_dir."""
    return "\n".join(
        f"printf '%s' {shlex.quote(content)} > \"$afc_config_dir/{name}\""
        for name, content in (f.split("::", 1) for f in files)
    )


class TestListExistingBuffers:
    def test_lists_sorted_unique_names_from_cfg_files(self) -> None:
        body = seed(
            "a.cfg::[AFC_buffer Zed]\nx: 1\n\n[AFC_buffer Alpha]\n",
            "b.cfg::[AFC_buffer Alpha]\n",
        ) + "\nlist_existing_buffers"
        out = run_bash(body)
        assert out.stdout == "Alpha\nZed\n"

    def test_ignores_commented_sections_and_non_cfg_files(self) -> None:
        body = seed(
            "a.cfg::#[AFC_buffer Hidden]\n[AFC_buffer Shown]\n",
            "b.txt::[AFC_buffer NotCfg]\n",
        ) + "\nlist_existing_buffers"
        out = run_bash(body)
        assert out.stdout == "Shown\n"

    def test_empty_when_no_buffers_defined(self) -> None:
        out = run_bash(seed("a.cfg::[AFC_hub X]\n") + "\nlist_existing_buffers")
        assert out.stdout == ""


class TestPromptSharedBufferName:
    BODY = (
        seed("a.cfg::[AFC_buffer Alpha]\n[AFC_buffer Beta]\n")
        + '\nprompt_shared_buffer_name\necho "RESULT=[$shared_buffer_name]"'
    )

    def test_select_by_number(self) -> None:
        out = run_bash(self.BODY, stdin="2\n")
        assert "RESULT=[Beta]" in out.stdout
        assert "  1. Alpha\n  2. Beta\n" in out.stdout

    def test_select_by_name(self) -> None:
        out = run_bash(self.BODY, stdin="Alpha\n")
        assert "RESULT=[Alpha]" in out.stdout

    def test_blank_skips(self) -> None:
        out = run_bash(self.BODY, stdin="\n")
        assert "RESULT=[]" in out.stdout

    def test_eof_skips(self) -> None:
        out = run_bash(self.BODY, stdin="")
        assert "RESULT=[]" in out.stdout

    def test_invalid_entries_reprompt_until_valid(self) -> None:
        out = run_bash(self.BODY, stdin="Nope\n9\n1\n")
        assert "RESULT=[Alpha]" in out.stdout
        assert out.stdout.count("is not one of the existing buffers.") == 2
        assert "'Nope' is not one" in out.stdout
        assert "'9' is not one" in out.stdout

    def test_partial_name_is_not_accepted(self) -> None:
        out = run_bash(self.BODY, stdin="Alph\n\n")
        assert "RESULT=[]" in out.stdout
        assert "'Alph' is not one" in out.stdout

    def test_no_existing_buffers_warns_and_skips(self) -> None:
        body = 'prompt_shared_buffer_name\necho "RESULT=[$shared_buffer_name]"'
        out = run_bash(body)
        assert "RESULT=[]" in out.stdout
        assert "No existing buffers found in" in out.stdout
        assert "set 'buffer:' manually." in out.stdout


class TestRemoveBufferSection:
    SRC = "[A x]\na: 1\n\n[AFC_buffer Gone]\nb: 2\n\n#[AFC_buffer Gone]\n[AFC_buffer Kept]\nc: 3\n"

    def run(self, src: str, header: str) -> str:
        body = (
            seed("f.cfg::" + src)
            + f'\nremove_buffer_section "$afc_config_dir/f.cfg" {shlex.quote(header)}'
            + '\ncat "$afc_config_dir/f.cfg"'
        )
        return run_bash(body).stdout

    def test_removes_header_body_and_one_blank_line(self) -> None:
        out = self.run(self.SRC, "[AFC_buffer Gone]")
        assert out == "[A x]\na: 1\n\n#[AFC_buffer Gone]\n[AFC_buffer Kept]\nc: 3\n"

    def test_stops_at_next_section_header_without_blank_line(self) -> None:
        out = self.run("[AFC_buffer Gone]\nb: 2\n[AFC_buffer Kept]\nc: 3\n", "[AFC_buffer Gone]")
        assert out == "[AFC_buffer Kept]\nc: 3\n"

    def test_missing_header_leaves_file_unchanged(self) -> None:
        out = self.run(self.SRC, "[AFC_buffer Nope]")
        assert out == self.SRC

    def test_section_at_end_of_file(self) -> None:
        out = self.run("[A x]\na: 1\n\n[AFC_buffer Gone]\nb: 2\n", "[AFC_buffer Gone]")
        assert out == "[A x]\na: 1\n\n"


class TestApplySharedBuffer:
    def test_replaces_existing_buffer_line_for_claymore(self) -> None:
        body = r"""
installation_type="Claymore"; boxturtle_name="Claymore_2"; is_additional_unit="True"
htlf2_board_type="AFC_Lite"
install_additional_unit
apply_shared_buffer "Claymore_buffer"
cat "$afc_config_dir/AFC_Claymore_2.cfg"
"""
        out = run_bash(body).stdout
        assert out.count("buffer: Claymore_buffer\n") == 1
        assert "buffer: Claymore_2_buffer\n" not in out
        assert "[AFC_buffer Claymore_2_buffer]" not in out
        assert "Claymore_2:ADV" not in out
        # Neighbouring sections are untouched
        assert "[AFC_hub Claymore_2]\n" in out
        assert "[AFC_led Claymore_Indicator_1]\n" in out

    def test_removes_prebaked_section_for_quattrobox(self) -> None:
        body = r"""
installation_type="QuattroBox"; boxturtle_name="QB_2"; is_additional_unit="True"
qb_board_type="MMB_1.1"; qb_motor_type="NEMA_14"
install_additional_unit
apply_shared_buffer "QuattroBox_1"
cat "$afc_config_dir/AFC_QB_2.cfg"
"""
        out = run_bash(body).stdout
        assert "[AFC_buffer QB_2]" not in out
        assert "<insert_advance_pin>" not in out
        assert "buffer: QuattroBox_1" in out
        assert "[AFC_QuattroBox QB_2]\n" in out

    def test_keeps_prebaked_section_when_shared_name_matches_it(self) -> None:
        body = r"""
installation_type="Claymore"; boxturtle_name="Claymore_2"; is_additional_unit="True"
htlf2_board_type="AFC_Lite"
install_additional_unit
apply_shared_buffer "Claymore_2_buffer"
cat "$afc_config_dir/AFC_Claymore_2.cfg"
"""
        out = run_bash(body).stdout
        assert out.count("[AFC_buffer Claymore_2_buffer]\n") == 1
        assert "buffer: Claymore_2_buffer\n" in out

    def test_inserts_buffer_line_when_template_has_only_placeholder(self) -> None:
        body = r"""
installation_type="NightOwl"; boxturtle_name="Owl_2"; is_additional_unit="True"
install_additional_unit
apply_shared_buffer "Shared_1"
sed -n '/^\[AFC_NightOwl Owl_2\]/,/^$/p' "$afc_config_dir/AFC_Owl_2.cfg"
"""
        out = run_bash(body).stdout
        assert "buffer: Shared_1\n" in out
        assert "# buffer: <buffer_name>" in out

    def test_unknown_target_warns_and_changes_nothing(self) -> None:
        body = r"""
installation_type="ViViD"; boxturtle_name="Vivid_2"; is_additional_unit="True"
apply_shared_buffer "Shared_1"
echo "rc=$?"
find "$afc_config_dir" -type f | wc -l
"""
        out = run_bash(body).stdout
        assert "No unit target is known for installation type 'ViViD'; skipping." in out
        assert "rc=0\n0\n" in out


class TestClaymoreAdditionalBufferName:
    def test_additional_unit_gets_unique_buffer_section_and_reference(self) -> None:
        body = r"""
installation_type="Claymore"; boxturtle_name="Claymore_2"; htlf2_board_type="AFC_Lite"
install_additional_unit
cat "$afc_config_dir/AFC_Claymore_2.cfg"
"""
        out = run_bash(body).stdout
        assert "buffer: Claymore_2_buffer\n" in out
        assert "[AFC_buffer Claymore_2_buffer]\n" in out
        assert "Claymore_buffer\n" not in out.replace("Claymore_2_buffer", "")

    def test_buffer_target_additional_uses_unit_named_section(self) -> None:
        body = r"""
installation_type="Claymore"; boxturtle_name="Claymore_2"; is_additional_unit="True"
get_unit_buffer_target
echo "$buffer_section_name|$buffer_prebaked_header"
"""
        out = run_bash(body).stdout
        assert out == "Claymore_2_buffer|[AFC_buffer Claymore_2_buffer]\n"

    def test_buffer_target_first_unit_keeps_shared_name(self) -> None:
        body = r"""
installation_type="Claymore"; boxturtle_name="Claymore_1"; is_additional_unit="False"
get_unit_buffer_target
echo "$buffer_section_name|$buffer_prebaked_header"
"""
        out = run_bash(body).stdout
        assert out == "Claymore_buffer|[AFC_buffer Claymore_buffer]\n"


MENU_DRIVER = r"""
clear() { :; }
print_unit_art() { :; }
check_existing_unit_installed() { return 0; }
main_menu() { exit 0; }
exit_afc_install() {
  echo "FINAL shared=[$shared_buffer_name]"
  echo "--- unit file ---"
  cat "$afc_config_dir/AFC_Claymore_2.cfg"
  exit 0
}
installation_type="Claymore"
htlf2_board_type="AFC_Lite"
files_updated_or_installed="False"
installation_options=("Claymore")
boxturtle_name="Claymore_1"
copy_unit_files
# Avoid safe_copy's overwrite prompt for the shared MCU file.
rm -f "$afc_config_dir/mcu/AFC_Lite_Claymore.cfg"
boxturtle_name=""
buffer_type="None"
additional_system_menu
"""


def run_menu(keys: List[str]) -> str:
    result = run_bash(MENU_DRIVER, stdin="\n".join(keys) + "\n")
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestAdditionalMenuSharedBuffer:
    def test_shared_choice_prompts_and_applies_selected_buffer(self) -> None:
        # Default is TurtleNeck; B x4 cycles to None. Then I, pick buffer 1.
        out = run_menu(["B", "B", "B", "B", "I", "1", "Q"])
        assert "Existing buffers:\n  1. Claymore_buffer\n" in out
        assert "is set to share the existing buffer 'Claymore_buffer'." in out
        assert "FINAL shared=[Claymore_buffer]" in out
        assert "buffer: Claymore_buffer\n" in out.split("--- unit file ---")[1]

    def test_shared_choice_skipped_leaves_template_reference(self) -> None:
        out = run_menu(["B", "B", "B", "I", "", "Q"])
        assert "FINAL shared=[]" in out
        assert "share the existing buffer" not in out
        unit_file = out.split("--- unit file ---")[1]
        assert "buffer: Claymore_2_buffer\n" in unit_file

    def test_own_buffer_choice_does_not_prompt(self) -> None:
        # Default is TurtleNeck (own buffer); answer the pin prompts with blanks.
        out = run_menu(["I", "", "", "Q"])
        assert "Existing buffers:" not in out
        assert "FINAL shared=[]" in out
