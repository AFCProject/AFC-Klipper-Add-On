"""Tests for the Ramming -> buffer type selection rule in install_menu()."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

DRIVER = r"""
source "$REPO_ROOT/include/units/registry.sh"
source "$REPO_ROOT/include/constants.sh"
source "$REPO_ROOT/include/colors.sh"
source "$REPO_ROOT/include/menus/install_menu.sh"
clear() { :; }
print_unit_art() { :; }
unit_print_install_menu_options() { :; }
main_menu() { echo "MAIN_MENU"; exit 0; }
install_afc() { echo "INSTALL_CALLED"; }
exit_afc_install() { echo "FINAL buffer_type=$buffer_type"; exit 0; }
files_updated_or_installed="False"
installation_options=("Box_Turtle" "ViViD" "OpenAMS")
message=""
installation_type="$CASE_installation_type"
toolhead_sensor="$CASE_toolhead_sensor"
buffer_type="$CASE_buffer_type"
install_menu
"""

ERR = "Ramming requires a buffer type. Select one with B"
RED = "\x1b[0;31m"


def run_menu(
    keys: List[str],
    installation_type: str = "Box_Turtle",
    toolhead_sensor: str = "Ramming",
    buffer_type: str = "Unknown",
) -> str:
    env = {
        "REPO_ROOT": str(REPO_ROOT),
        "CASE_installation_type": installation_type,
        "CASE_toolhead_sensor": toolhead_sensor,
        "CASE_buffer_type": buffer_type,
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
    }
    result = subprocess.run(
        ["bash", "-c", DRIVER],
        env=env,
        input="\n".join(keys + ["Q"]) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestInstallMenuDefault:
    def test_default_buffer_type_is_unknown(self) -> None:
        out = subprocess.run(
            ["bash", "-c", 'source "$REPO_ROOT/include/constants.sh"; echo "$buffer_type"'],
            env={"REPO_ROOT": str(REPO_ROOT), "PATH": "/usr/bin:/bin", "HOME": "/tmp"},
            capture_output=True,
            text=True,
        ).stdout
        assert out == "Unknown\n"


class TestInstallMenuInstallRequiresBuffer:
    def test_ramming_unknown_blocks_install(self) -> None:
        out = run_menu(["I"])
        assert "INSTALL_CALLED" not in out
        assert ERR in out
        assert "FINAL buffer_type=Unknown" in out

    def test_ramming_with_selected_buffer_installs(self) -> None:
        out = run_menu(["B", "I"])
        assert out.count("INSTALL_CALLED") == 1
        assert ERR not in out

    def test_ramming_with_none_buffer_blocks_install(self) -> None:
        out = run_menu(["I"], buffer_type="None")
        assert "INSTALL_CALLED" not in out
        assert ERR in out
        assert "FINAL buffer_type=None" in out

    @pytest.mark.parametrize("unit_type", ["ViViD", "OpenAMS"])
    def test_exempt_types_install_with_none(self, unit_type: str) -> None:
        out = run_menu(["I"], installation_type=unit_type, buffer_type="None")
        assert out.count("INSTALL_CALLED") == 1
        assert ERR not in out

    def test_sensor_with_none_buffer_installs(self) -> None:
        out = run_menu(["I"], toolhead_sensor="Sensor", buffer_type="None")
        assert out.count("INSTALL_CALLED") == 1
        assert ERR not in out

    def test_sensor_unknown_installs(self) -> None:
        out = run_menu(["I"], toolhead_sensor="Sensor")
        assert out.count("INSTALL_CALLED") == 1
        assert ERR not in out

    @pytest.mark.parametrize("unit_type", ["ViViD", "OpenAMS"])
    def test_exempt_types_install_with_unknown(self, unit_type: str) -> None:
        out = run_menu(["I"], installation_type=unit_type)
        assert out.count("INSTALL_CALLED") == 1
        assert ERR not in out


class TestInstallMenuCycleBuffer:
    def test_ramming_cycle_skips_none(self) -> None:
        # Unknown -> TurtleNeck -> TurtleNeckV2 -> TurtleNeck2Remastered -> FPS_PSF -> TurtleNeck
        out = run_menu(["B", "B", "B", "B", "B"])
        assert "FINAL buffer_type=TurtleNeck" in out
        assert "Buffer Type: Proportional Sync-Feedback(PSF)" in out
        assert "Buffer Type: None" not in out

    def test_sensor_cycle_reaches_none(self) -> None:
        out = run_menu(["B", "B", "B", "B", "B"], toolhead_sensor="Sensor")
        assert "FINAL buffer_type=None" in out


class TestInstallMenuBufferDisplay:
    def test_unknown_with_ramming_is_red_with_hint(self) -> None:
        out = run_menu([])
        assert f"B. Buffer type : {RED}Unknown" in out
        assert "(a buffer is required with ramming)" in out

    def test_none_with_ramming_is_red_with_hint(self) -> None:
        out = run_menu([], buffer_type="None")
        assert f"B. Buffer type : {RED}None" in out
        assert "(a buffer is required with ramming)" in out

    def test_unknown_with_sensor_is_plain(self) -> None:
        out = run_menu([], toolhead_sensor="Sensor")
        assert "B. Buffer type : Unknown \n" in out
        assert "(a buffer is required with ramming)" not in out

    def test_selected_buffer_with_ramming_is_plain(self) -> None:
        out = run_menu([], buffer_type="TurtleNeck")
        assert "B. Buffer type : TurtleNeck \n" in out
        assert "(a buffer is required with ramming)" not in out


class TestInstallMenuToggleSensor:
    def test_toggle_to_sensor_resets_unknown_to_none(self) -> None:
        out = run_menu(["9"])
        assert "FINAL buffer_type=None" in out
        assert "Using toolhead sensor" in out

    def test_toggle_to_sensor_keeps_selected_buffer(self) -> None:
        out = run_menu(["9"], buffer_type="TurtleNeckV2")
        assert "FINAL buffer_type=TurtleNeckV2" in out


class TestInstallMenuToggleRamming:
    def test_toggle_to_ramming_with_unknown_shows_hint(self) -> None:
        out = run_menu(["9"], toolhead_sensor="Sensor")
        assert (
            "Using ramming with a TurtleNeck buffer. "
            "Select a buffer type with B"
        ) in out

    def test_toggle_to_ramming_resets_none_to_unknown(self) -> None:
        out = run_menu(["9"], toolhead_sensor="Sensor", buffer_type="None")
        assert "FINAL buffer_type=Unknown" in out
        assert "Select a buffer type with B" in out

    def test_toggle_to_ramming_keeps_none_for_vivid(self) -> None:
        out = run_menu(
            ["9"],
            installation_type="ViViD",
            toolhead_sensor="Sensor",
            buffer_type="None",
        )
        assert "FINAL buffer_type=None" in out
        assert "Select a buffer type with B" not in out

    def test_toggle_to_ramming_with_buffer_has_no_hint(self) -> None:
        out = run_menu(["9"], toolhead_sensor="Sensor", buffer_type="TurtleNeck")
        assert "Using ramming with a TurtleNeck buffer\n" in out
        assert "Select a buffer type with B" not in out
