"""Tests for the TurtleNeck 2 Remastered buffer installer support."""
from __future__ import annotations

from test_shared_buffer import run_bash

EXPECTED_BLOCK = """[AFC_buffer Turtle_1]
type: FPS_PSF
adc_pin: TN2:PA0
neutral_point: 0.5
max_tension: 0.1
max_compression: 0.9
reversed: true  #Test slide by hand and look at buffer state in mainsail/fluidd Set to true if display shows opposite of your tested state.
led_index: Buffer_Indicator:1

[AFC_led Buffer_Indicator]
pin: TN2:PB1
chain_count: 1
color_order: GRBW
initial_RED: 0.0
initial_GREEN: 0.0
initial_BLUE: 0.0
initial_WHITE: 0.0"""


class TestBuildBufferConfig:
    def test_builds_fps_psf_block_with_tn2_pins_and_led(self) -> None:
        body = (
            'build_buffer_config TurtleNeck2Remastered "" "" Turtle_1\n'
            'printf "%s\\n---\\n%s" "$buffer_config" "$buffer_name"\n'
        )
        result = run_bash(body)
        assert result.returncode == 0
        assert result.stdout == f"{EXPECTED_BLOCK}\n---\nTurtle_1"

    def test_uses_given_unit_name_for_section_and_buffer_name(self) -> None:
        body = (
            'build_buffer_config TurtleNeck2Remastered "" "" HTLF_2\n'
            'printf "%s\\n---\\n%s" "$(head -n 1 <<< "$buffer_config")" "$buffer_name"\n'
        )
        result = run_bash(body)
        assert result.stdout == "[AFC_buffer HTLF_2]\n---\nHTLF_2"


class TestAppendBufferConfig:
    def test_appends_block_copies_mcu_file_and_adds_include(self) -> None:
        body = (
            'target="$afc_config_dir/AFC_Hardware.cfg"\n'
            'printf "[AFC_extruder extruder]\\n" > "$target"\n'
            'append_buffer_config TurtleNeck2Remastered "" "" Turtle_1 "$target"\n'
            'cat "$target"\n'
            'echo "=== MCU ==="\n'
            'cat "$afc_config_dir/mcu/TN2.cfg"\n'
        )
        result = run_bash(body)
        assert result.returncode == 0
        expected = (
            "[AFC_extruder extruder]\n\n"
            f"{EXPECTED_BLOCK}\n\n"
            "[include mcu/TN2.cfg]\n"
            "=== MCU ===\n"
            "[mcu TN2]\n"
            "serial: /dev/serial/by-id/**YOUR SERIAL HERE**\n"
        )
        assert result.stdout == expected

    def test_include_not_duplicated_when_already_present(self) -> None:
        body = (
            'target="$afc_config_dir/AFC_Hardware.cfg"\n'
            'printf "[include mcu/TN2.cfg]\\n" > "$target"\n'
            'append_buffer_config TurtleNeck2Remastered "" "" Turtle_1 "$target"\n'
            'grep -c "include mcu/TN2.cfg" "$target"\n'
        )
        result = run_bash(body)
        assert result.stdout == "1\n"

    def test_other_buffer_types_do_not_add_tn2_include(self) -> None:
        body = (
            'target="$afc_config_dir/AFC_Hardware.cfg"\n'
            'printf "[AFC_extruder extruder]\\n" > "$target"\n'
            'append_buffer_config TurtleNeckV2 "" "" Turtle_1 "$target"\n'
            'grep -c "TN2" "$target"; ls "$afc_config_dir/mcu"\n'
        )
        result = run_bash(body)
        assert result.stdout == "0\nTurtleNeckv2.cfg\n"


    def test_remastered_does_not_copy_or_include_turtleneck_v2_file(self) -> None:
        body = (
            'target="$afc_config_dir/AFC_Hardware.cfg"\n'
            'printf "[AFC_extruder extruder]\\n" > "$target"\n'
            'append_buffer_config TurtleNeck2Remastered "" "" Turtle_1 "$target"\n'
            'grep -c "TurtleNeckv2" "$target"; ls "$afc_config_dir/mcu"\n'
        )
        result = run_bash(body)
        assert result.stdout == "0\nTN2.cfg\n"


class TestBufferTypeLabel:
    def test_label_is_user_facing_name(self) -> None:
        result = run_bash('buffer_type_label TurtleNeck2Remastered')
        assert result.stdout == "TurtleNeck 2 Remastered\n"
