"""
Unit tests for extras/AFC_functions.py

Covers:
  - HexConvert: comma-separated RGBA float string → "#rrggbb"
  - HexToLedString: hex string → comma-separated float list
  - _get_led_indexes: "1-4,9,10" → [1,2,3,4,9,10]
  - _calc_length: bowden length arithmetic (reset / +/- / direct)
  - check_macro_present: detects macro in gcode handlers
  - get_filament_status: returns correct status string for lane state
  - afcDeltaTime: timing delta helper class
"""

from __future__ import annotations

from typing import Any, Dict, Tuple
from unittest.mock import ANY, MagicMock, patch, call
import pytest

from extras.AFC_functions import (
    afcFunction, afcDeltaTime, round_floats, get_gcode_absolute_extrude,
    set_gcode_absolute_extrude, BUTTON_CALIBRATION_POLL_TIME,
    BUTTON_CALIBRATION_SATURATION_MAX_RATIO, BUTTON_CALIBRATION_STAGE_TIMEOUT,
    BUTTON_CALIBRATION_ADC_SAMPLE_TIMEOUT,
)
# These now live on AFCButton as per-button config options (with these same names as their
# defaults); _make_adc_button() below mirrors the defaults onto its mock buttons so production code
# reading button.<name> still sees the same values these tests were written against.
from extras.AFC_button import (
    BUTTON_CALIBRATION_PRESS_DELTA, BUTTON_CALIBRATION_RANGE,
    BUTTON_CALIBRATION_SAMPLE_COUNT, ADC_SATURATION_RATIO,
)
BUTTON_CALIBRATION_AGREEMENT_TOLERANCE = 2.0 * BUTTON_CALIBRATION_RANGE
from extras.AFC_respond import AFCprompt

from tests.test_AFC import _make_afc
from tests.test_AFC_lane import _make_afc_lane

from copy import deepcopy


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_func():
    """Build an afcFunction instance bypassing __init__."""
    func = afcFunction.__new__(afcFunction)

    from tests.conftest import MockAFC, MockLogger

    afc = MockAFC()
    afc.reactor = MagicMock()
    func.afc = afc
    func.logger = MockLogger()
    func.printer = MagicMock()
    func.config = MagicMock()
    func.auto_var_file = None
    func.errorLog = {}
    func.pause = False
    func.mcu = MagicMock()
    return func


def _make_real_func():
    """Build an afcFunction through its real __init__, mocking its config
    and printer dependencies."""
    from tests.conftest import MockAFC, MockConfig, MockLogger, MockPrinter

    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    config = MockConfig(name="AFC_functions", printer=printer)
    config.access_tracking = {}
    func = afcFunction(config)
    func.afc = afc
    func.logger = MockLogger()
    return func


# ── round_floats ─────────────────────────────────────────────────────────────

class TestRoundFloats:
    def test_rounds_single_float(self):
        assert round_floats(2.882800219999981234) == 2.8828

    def test_leaves_int_unchanged(self):
        assert round_floats(5) == 5

    def test_rounds_list_of_floats(self):
        result = round_floats([165.1740931234, 256.3006781234, 3.0715305953986847])
        assert result == [165.174093, 256.300678, 3.071531]

    def test_rounds_tuple_of_floats(self):
        result = round_floats((165.1740931234, 256.3006781234))
        assert result == [165.174093, 256.300678]

    def test_custom_digit_count(self):
        assert round_floats(1.23456789, digits=2) == 1.23

    def test_non_numeric_passthrough(self):
        assert round_floats(True) is True
        assert round_floats("abc") == "abc"
        assert round_floats(None) is None


# ── log_toolhead_pos ──────────────────────────────────────────────────────────

class TestLogToolheadPos:
    def _wire_positions(self, func):
        func.afc.toolhead.get_position.return_value = [
            165.174093123, 256.300678987, 3.0715305953986847, 2882.80021999998,
        ]
        func.afc.gcode_move.base_position = [-0.075907456, 0.072678123, 0.0, 2847.634679999981]
        func.afc.gcode_move.last_position = [165.174093123, 256.300678987, 2.94, 2882.80021999998]
        func.afc.gcode_move.speed = 350.0
        func.afc.gcode_move.speed_factor = 0.016666666666666666
        func.afc.gcode_move.extrude_factor = 1.0
        func.afc.gcode_move.absolute_coord = True
        func.afc.gcode_move.absolute_extrude = False
        func.afc.gcode_move.allow_absolute_extrude = False
        func.afc.gcode_move.get_status.return_value = {"absolute_extrude": False}

    def test_logs_expected_message_with_move_pre(self):
        func = _make_func()
        self._wire_positions(func)

        func.log_toolhead_pos("Before toolswap: ")

        expected = (
            "Before toolswap: Position: [165.174093, 256.300679, 3.071531, 2882.80022] "
            "base_position: [-0.075907, 0.072678, 0.0, 2847.63468] "
            "last_position: [165.174093, 256.300679, 2.94, 2882.80022] "
            "speed: 350.0 speed_factor: 0.016667 extrude_factor: 1.0 "
            "absolute_coord: True absolute_extrude: False\n"
        )
        assert func.logger.messages == [("debug", expected)]

    def test_logs_expected_message_with_default_move_pre(self):
        func = _make_func()
        self._wire_positions(func)

        func.log_toolhead_pos()

        expected = (
            "Position: [165.174093, 256.300679, 3.071531, 2882.80022] "
            "base_position: [-0.075907, 0.072678, 0.0, 2847.63468] "
            "last_position: [165.174093, 256.300679, 2.94, 2882.80022] "
            "speed: 350.0 speed_factor: 0.016667 extrude_factor: 1.0 "
            "absolute_coord: True absolute_extrude: False\n"
        )
        assert func.logger.messages == [("debug", expected)]

def _make_gcmd(**values: Any) -> MagicMock:
    """
    Build a G-code command mock with Klipper-like typed getters.

    :param values: values returned by the typed getters
    :return MagicMock: configured G-code command mock
    """
    gcmd = MagicMock()
    gcmd.get_float.side_effect = lambda name, default=0.0, **kwargs: float(
        values.get(name, default))
    gcmd.get_int.side_effect = lambda name, default=0, **kwargs: int(values.get(name, default))
    gcmd.get.side_effect = lambda name, default=None, **kwargs: values.get(name, default)
    # A bare MagicMock answers hasattr(gcmd, "run_script") True for any
    # name, unlike a real GCodeCommand on older Kalico/mainline Klipper --
    # deleting the auto-created attribute makes hasattr correctly report
    # False here, so afc.run_script() takes its run_script_from_command
    # fallback path by default, matching what most callers actually have.
    del gcmd.run_script
    return gcmd


# ── Klipper PR 7349 absolute_extrude compat ───────────────────────────────────

class _OldKlipperGCodeMove:
    def __init__(self, value, status_value=None):
        self.absolute_extrude = value
        self._status_value = value if status_value is None else status_value

    def get_status(self, eventtime=None):
        return {"absolute_extrude": self._status_value}


class _NewKlipperGCodeMove:
    def __init__(self, value, status_value=None):
        self.allow_absolute_extrude = value
        self._status_value = value if status_value is None else status_value

    def get_status(self, eventtime=None):
        return {"absolute_extrude": self._status_value}


class _OldKlipperGCodeMoveAttrOnly:
    """Legacy Klipper mock with absolute_extrude and no get_status()."""

    def __init__(self, value):
        self.absolute_extrude = value


class _NewKlipperGCodeMoveAttrOnly:
    """Post-PR-7349 Klipper mock with allow_absolute_extrude and no get_status()."""

    def __init__(self, value):
        self.allow_absolute_extrude = value


class TestGcodeAbsoluteExtrudeCompat:
    def test_reads_attr_on_old_klipper_without_get_status(self):
        move = _OldKlipperGCodeMoveAttrOnly(False)
        assert not hasattr(move, "get_status")
        assert get_gcode_absolute_extrude(move) is False

    def test_reads_attr_on_new_klipper_without_get_status(self):
        move = _NewKlipperGCodeMoveAttrOnly(False)
        assert not hasattr(move, "get_status")
        assert get_gcode_absolute_extrude(move) is False

    def test_ignores_get_status_on_new_klipper(self):
        """get_gcode_absolute_extrude no longer consults get_status() at all
        (Klipper PR 7349 compat fix) -- a stale/conflicting status dict must
        not influence the result, only the allow_absolute_extrude attr does."""
        move = _NewKlipperGCodeMove(False, status_value=True)
        assert get_gcode_absolute_extrude(move) is False

    def test_ignores_get_status_on_old_klipper(self):
        move = _OldKlipperGCodeMove(False, status_value=True)
        assert get_gcode_absolute_extrude(move) is False

    def test_defaults_to_true_when_neither_attribute_present(self):
        """Covers the final getattr(..., True) fallback when the gcode_move
        object exposes neither allow_absolute_extrude nor absolute_extrude."""
        move = object()
        assert get_gcode_absolute_extrude(move) is True

    def test_writes_allow_absolute_extrude_on_new_klipper(self):
        move = _NewKlipperGCodeMove(False)
        set_gcode_absolute_extrude(move, True)
        assert move.allow_absolute_extrude is True
        assert not hasattr(move, "absolute_extrude")

    def test_writes_absolute_extrude_on_old_klipper(self):
        move = _OldKlipperGCodeMove(False)
        set_gcode_absolute_extrude(move, True)
        assert move.absolute_extrude is True
        assert not hasattr(move, "allow_absolute_extrude")

    def test_check_absolute_mode_sets_new_klipper_attr(self):
        func = _make_func()
        func.afc.gcode_move = _NewKlipperGCodeMove(False)
        func.afc.gcode_move.absolute_coord = True
        func.afc.gcode_move.base_position = [0, 0, 0, 0]
        func.afc.gcode_move.last_position = [0, 0, 0, 0]
        func.afc.gcode_move.speed = 1.0
        func.afc.gcode_move.speed_factor = 1.0
        func.afc.gcode_move.extrude_factor = 1.0
        func.afc.toolhead.get_position.return_value = [0, 0, 0, 0]
        func.check_absolute_mode("TEST")
        assert func.afc.gcode_move.allow_absolute_extrude is True


# ── HexConvert ────────────────────────────────────────────────────────────────

class TestHexConvert:
    def test_full_brightness_red(self):
        func = _make_func()
        result = func.HexConvert("1,0,0")
        assert result == "#ff0000"

    def test_full_brightness_green(self):
        func = _make_func()
        result = func.HexConvert("0,1,0")
        assert result == "#00ff00"

    def test_full_brightness_blue(self):
        func = _make_func()
        result = func.HexConvert("0,0,1")
        assert result == "#0000ff"

    def test_all_zeros_black(self):
        func = _make_func()
        result = func.HexConvert("0,0,0")
        assert result == "#000000"

    def test_half_red(self):
        func = _make_func()
        result = func.HexConvert("0.5,0,0")
        # 0.5 * 255 = 127 → 0x7f
        assert result == "#7f0000"

    def test_white_no_w_channel(self):
        func = _make_func()
        # Only first 3 channels used in HexConvert
        result = func.HexConvert("1,1,1")
        assert result == "#ffffff"

    def test_returns_string_starting_with_hash(self):
        func = _make_func()
        result = func.HexConvert("1,0.5,0")
        assert result.startswith("#")
        assert len(result) == 7


# ── HexToLedString ────────────────────────────────────────────────────────────

class TestHexToLedString:
    def test_black_returns_all_zeros(self):
        func = _make_func()
        result = func.HexToLedString("000000")
        assert result[0] == pytest.approx(0.0)
        assert result[1] == pytest.approx(0.0)
        assert result[2] == pytest.approx(0.0)

    def test_white_sets_w_channel(self):
        func = _make_func()
        result = func.HexToLedString("FFFFFF")
        assert result[3] == 1.0

    def test_red_channel(self):
        func = _make_func()
        result = func.HexToLedString("FF0000")
        assert result[0] == pytest.approx(1.0)
        assert result[1] == pytest.approx(0.0)
        assert result[2] == pytest.approx(0.0)

    def test_non_white_sets_w_zero(self):
        func = _make_func()
        result = func.HexToLedString("FF0000")
        assert result[3] == 0.0

    def test_returns_four_values(self):
        func = _make_func()
        result = func.HexToLedString("123456")
        assert len(result) == 4


# ── _get_led_indexes ──────────────────────────────────────────────────────────

class TestGetLedIndexes:
    def test_single_index(self):
        func = _make_func()
        assert func._get_led_indexes("5") == [5]

    def test_range_expands(self):
        func = _make_func()
        assert func._get_led_indexes("1-4") == [1, 2, 3, 4]

    def test_comma_separated(self):
        func = _make_func()
        assert func._get_led_indexes("9,10") == [9, 10]

    def test_range_and_singles(self):
        func = _make_func()
        assert func._get_led_indexes("1-4,9,10") == [1, 2, 3, 4, 9, 10]

    def test_single_element_range(self):
        func = _make_func()
        assert func._get_led_indexes("3-3") == [3]


# ── parse_led_groups ────────────────────────────────────────────────────────

class TestParseLedGroups:
    def test_single_led_single_index(self):
        func = _make_func()
        assert func.parse_led_groups("AFC_Indicator:1") == [("AFC_Indicator", "1")]

    def test_single_led_range(self):
        func = _make_func()
        assert func.parse_led_groups("AFC_Indicator:1-4") == [("AFC_Indicator", "1-4")]

    def test_single_led_range_and_singles(self):
        func = _make_func()
        assert func.parse_led_groups("AFC_Indicator:1-4,9,10") == [("AFC_Indicator", "1-4,9,10")]

    def test_multi_led_groups(self):
        func = _make_func()
        result = func.parse_led_groups("RGB1:1-4,RGB2:4-6,RGB3:5")
        assert result == [("RGB1", "1-4"), ("RGB2", "4-6"), ("RGB3", "5")]

    def test_multi_led_with_mixed_indexes(self):
        func = _make_func()
        result = func.parse_led_groups("RGB1:1-4,9,RGB2:4-6")
        assert result == [("RGB1", "1-4,9"), ("RGB2", "4-6")]

    def test_whitespace_handling(self):
        func = _make_func()
        result = func.parse_led_groups("RGB1: 1-4, RGB2: 5")
        assert result == [("RGB1", "1-4"), ("RGB2", "5")]

    def test_orphan_segment_logs_warning(self):
        func = _make_func()
        result = func.parse_led_groups("42,RGB1:1")
        # Orphan "42" is skipped, only the valid group is returned
        assert result == [("RGB1", "1")]
        assert any("42" in msg for _, msg in func.logger.messages)


# ── afc_led (integration) ───────────────────────────────────────────────────

class TestAfcLed:
    def test_none_idx_is_noop(self):
        func = _make_func()
        # Should not raise
        func.afc_led("1,0,0,0", idx=None)

    def test_single_led_group(self):
        led_mock = MagicMock()
        func = _make_func()
        func.printer.lookup_object.return_value = led_mock
        func.afc_led("1,0,0,0", idx="AFC_Indicator:1-4")
        func.printer.lookup_object.assert_called_once_with("AFC_led AFC_Indicator")
        led_mock.led_change.assert_called_once_with([1, 2, 3, 4], "1,0,0,0")

    def test_multi_led_groups(self):
        led1 = MagicMock()
        led2 = MagicMock()
        led3 = MagicMock()
        lookup_map = {
            "AFC_led RGB1": led1,
            "AFC_led RGB2": led2,
            "AFC_led RGB3": led3,
        }
        func = _make_func()
        func.printer.lookup_object.side_effect = lambda name: lookup_map[name]
        func.afc_led("0,1,0,0", idx="RGB1:1-4,RGB2:4-6,RGB3:5")
        led1.led_change.assert_called_once_with([1, 2, 3, 4], "0,1,0,0")
        led2.led_change.assert_called_once_with([4, 5, 6], "0,1,0,0")
        led3.led_change.assert_called_once_with([5], "0,1,0,0")

    def test_missing_led_logs_error(self):
        func = _make_func()
        func.printer.lookup_object.side_effect = Exception("not found")
        func.afc_led("1,0,0,0", idx="BadLed:1")
        assert any("BadLed" in msg for _, msg in func.logger.messages)


# ── _calc_length ──────────────────────────────────────────────────────────────

class TestCalcLength:
    def test_reset_returns_config_length(self):
        func = _make_func()
        result = func._calc_length(500.0, 600.0, "reset")
        assert result == 500.0

    def test_reset_case_insensitive(self):
        func = _make_func()
        result = func._calc_length(500.0, 600.0, "RESET")
        assert result == 500.0

    def test_positive_delta(self):
        func = _make_func()
        result = func._calc_length(500.0, 600.0, "+50")
        assert result == pytest.approx(650.0)

    def test_negative_delta(self):
        func = _make_func()
        result = func._calc_length(500.0, 600.0, "-100")
        assert result == pytest.approx(500.0)

    def test_direct_value(self):
        func = _make_func()
        result = func._calc_length(500.0, 600.0, "750")
        assert result == pytest.approx(750.0)

    def test_invalid_delta_returns_current(self):
        func = _make_func()
        result = func._calc_length(500.0, 600.0, "+abc")
        assert result == 600.0


# ── check_macro_present ───────────────────────────────────────────────────────

class TestCheckMacroPresentFunction:
    def test_returns_true_when_macro_exists(self):
        func = _make_func()
        func.afc.gcode.ready_gcode_handlers = {"MY_MACRO": MagicMock()}
        assert func.check_macro_present("MY_MACRO") is True

    def test_returns_false_when_macro_missing(self):
        func = _make_func()
        func.afc.gcode.ready_gcode_handlers = {}
        assert func.check_macro_present("MISSING_MACRO") is False

    def test_returns_false_when_no_handlers_attr(self):
        func = _make_func()
        # gcode has no ready_gcode_handlers attr
        if hasattr(func.afc.gcode, "ready_gcode_handlers"):
            del func.afc.gcode.ready_gcode_handlers
        assert func.check_macro_present("ANYTHING") is False


# Toolhead calibration

def _make_calibration_func() -> Tuple[afcFunction, MagicMock]:
    """
    Build a fully initialized function object with one loaded extruder.

    :return tuple: initialized AFC function and extruder mock
    """
    from tests.conftest import MockAFC, MockConfig, MockLogger, MockPrinter

    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    config = MockConfig(name="AFC_functions", printer=printer)
    config.access_tracking = {}
    func = afcFunction(config)
    func.afc = afc
    func.logger = MockLogger()
    func.ConfigRewrite = MagicMock()

    print_stats = MagicMock()
    print_stats.get_status.return_value = {"state": "standby", "filename": ""}
    printer._objects["print_stats"] = print_stats

    afc.current = "lane1"
    afc.tool_cut = True
    afc.tool_cut_cmd = "AFC_CUT"
    afc.move_e_pos = MagicMock()
    afc.toolhead = MagicMock()

    extruder = MagicMock()
    extruder.name = "extruder"
    extruder.fullname = "AFC_extruder extruder"
    extruder.tool_stn = 72.0
    extruder.tool_stn_unload = 100.0
    extruder.tool_start = "PA1"
    extruder.tool_start_state = True
    extruder.tool_end = None
    extruder.tool_end_state = False
    extruder.tool_load_speed = 1.0
    extruder.tool_unload_speed = 1.0
    extruder.is_standalone.return_value = False
    # Toolhead empty by default: afc.current="lane1" below only means lane1
    # is the generally "active"/selected lane, not that it's physically
    # loaded -- cmd_AFC_TOOLHEAD_CALIBRATION treats extruder.lane_loaded as
    # the separate, hardware-specific "is there filament in the toolhead"
    # signal that gates whether the tool_stn button is offered.
    extruder.lane_loaded = None
    extruder._update_tool_stn.side_effect = (
        lambda value: setattr(extruder, "tool_stn", value) if value > 0 else None)
    extruder._update_tool_stn_unload.side_effect = (
        lambda value: setattr(extruder, "tool_stn_unload", value) if value >= 0 else None)
    afc.tools = {"extruder": extruder}

    loaded_lane = MagicMock()
    loaded_lane.name = "lane1"
    loaded_lane.extruder_obj = extruder
    afc.lanes["lane1"] = loaded_lane
    extruder.lanes = {"lane1": loaded_lane}
    return func, extruder


def _make_adc_button(name: str, resistance_sequence: Any=None,
                     is_adc: bool=True, pin: str="PA0") -> MagicMock:
    """
    Build a lightweight stand-in for an ADC-capable AFC_button, as _get_adc_buttons()/button
    calibration see it: name, fullname, is_adc_button, analog_min/max, pin (shared default so
    buttons compare as being on the same ADC input unless overridden), a get_resistance() that feeds
    successive calls from `resistance_sequence`, is_adc_saturated() defaulting to False (override
    per-test for saturation-fallback cases), and ratio_at_resistance() defaulting to the real
    formula with a 4700 ohm pullup (override per-test for the near-saturation release fallback).

    :param name: button/lane name
    :param resistance_sequence: iterable of resistance values returned by successive
        get_resistance() calls; defaults to an unlimited sequence of 0.0 when omitted
    :param is_adc: value for is_adc_button
    :param pin: value for pin
    :return MagicMock: the fake button
    """
    button = MagicMock()
    button.name = name
    button.fullname = f"AFC_button {name}"
    button.is_adc_button = is_adc
    button.pin = pin
    button.analog_min = 0.0
    button.analog_max = 0.0
    button.calibration_range = BUTTON_CALIBRATION_RANGE
    button.calibration_press_delta = BUTTON_CALIBRATION_PRESS_DELTA
    button.calibration_sample_count = BUTTON_CALIBRATION_SAMPLE_COUNT
    button.adc_saturation_ratio = ADC_SATURATION_RATIO
    button.is_adc_saturated.return_value = False
    button.ratio_at_resistance.side_effect = lambda resistance: resistance / (resistance + 4700.0)
    button.resistance_at_ratio.side_effect = lambda ratio: 4700.0 * ratio / (1.0 - ratio)
    button._get_adc_ratio.return_value = 0.0
    if resistance_sequence is not None:
        button.get_resistance.side_effect = list(resistance_sequence)
    else:
        button.get_resistance.return_value = 0.0
    return button


class TestAfcFunctionInit:
    def test_toolhead_calibration_commands_are_hidden_from_ui(self) -> None:
        func, _ = _make_calibration_func()
        command_names = (
            "AFC_TOOLHEAD_CALIBRATION",
            "AFC_TOOL_STN_CALIBRATION",
            "AFC_TOOL_STN_UNLOAD_CALIBRATION",
            "AFC_CUTTER_CALIBRATION",
        )

        for command_name in command_names:
            assert command_name in func.printer._gcode._commands
            assert f"_{command_name}" not in func.printer._gcode._commands
            assert not func.config.fileconfig.has_section(f"gcode_macro {command_name}")

    def test_initializes_calibration_session_attributes(self) -> None:
        func, _ = _make_calibration_func()

        assert func.cutter_calibration_active is False
        assert func.cutter_calibration_distance == 0.0
        assert func.cutter_calibration_margin == 10.0
        assert func.cutter_calibration_extruder is None
        assert func.cutter_calibration_lane is None
        assert func.stn_unload_calibration_active is False
        assert func.stn_unload_calibration_distance == 0.0
        assert func.stn_unload_calibration_extruder is None
        assert func.stn_unload_calibration_lane is None
        assert func.stn_calibration_active is False
        assert func.stn_calibration_distance == 0.0
        assert func.stn_calibration_original == 0.0
        assert func.stn_calibration_extruder is None
        assert func.stn_calibration_lane is None


class TestCheckAndSetTempForCalibrationLane:
    def test_checks_extruder_temp_and_shows_exact_prompt_sequence(self) -> None:
        func = _make_func()
        lane = MagicMock()
        prompt = AFCprompt(_make_gcmd(), func.logger)

        func._check_and_set_temp_for_calibration_lane(lane, prompt)

        func.afc._check_extruder_temp.assert_called_once_with(lane)
        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Checking toolhead temperature"),
            ("raw", "// action:prompt_text Checking toolhead temperature and waiting for "
             "toolhead to reach temperature"),
            ("raw", "// action:prompt_show"),
            ("raw", "// action:prompt_end"),
        ]


class TestResetStnCalibrationValues:
    def test_clears_all_session_attributes(self) -> None:
        func = _make_func()
        func.stn_calibration_active = True
        func.stn_calibration_distance = 12.0
        func.stn_calibration_original = 34.0
        func.stn_calibration_extruder = "extruder"
        func.stn_calibration_lane = "lane1"

        func._reset_stn_calibration_values()

        assert func.stn_calibration_active is False
        assert func.stn_calibration_distance == 0.0
        assert func.stn_calibration_original == 0.0
        assert func.stn_calibration_extruder is None
        assert func.stn_calibration_lane is None


class TestResetStnUnloadCalibrationValues:
    def test_clears_all_session_attributes(self) -> None:
        func = _make_func()
        func.stn_unload_calibration_active = True
        func.stn_unload_calibration_distance = 12.0
        func.stn_unload_calibration_extruder = "extruder"
        func.stn_unload_calibration_lane = "lane1"

        func._reset_stn_unload_calibration_values()

        assert func.stn_unload_calibration_active is False
        assert func.stn_unload_calibration_distance == 0.0
        assert func.stn_unload_calibration_extruder is None
        assert func.stn_unload_calibration_lane is None


class TestResetCutterCalibrationValues:
    def test_clears_all_session_attributes(self) -> None:
        func = _make_func()
        func.cutter_calibration_active = True
        func.cutter_calibration_distance = 12.0
        func.cutter_calibration_extruder = "extruder"
        func.cutter_calibration_lane = "lane1"

        func._reset_cutter_calibration_values()

        assert func.cutter_calibration_active is False
        assert func.cutter_calibration_distance == 0.0
        assert func.cutter_calibration_extruder is None
        assert func.cutter_calibration_lane is None


class TestResolveCalibrationLane:
    def test_rejects_unknown_extruder(self) -> None:
        func, _ = _make_calibration_func()
        gcmd = MagicMock()

        lane = func._resolve_calibration_lane("other", "lane1", gcmd)

        assert lane is None
        func.afc.error.AFC_error.assert_called_once_with(
            "'other' is not a valid extruder", pause=False, gcmd=gcmd)

    def test_rejects_unknown_lane(self) -> None:
        func, _ = _make_calibration_func()
        gcmd = MagicMock()

        lane = func._resolve_calibration_lane("extruder", "other", gcmd)

        assert lane is None
        func.afc.error.AFC_error.assert_called_once_with(
            "'other' is not a valid lane", pause=False, gcmd=gcmd)

    def test_resolves_loaded_lane_when_name_is_omitted(self) -> None:
        func, _ = _make_calibration_func()

        lane = func._resolve_calibration_lane("extruder", None, MagicMock())

        assert lane is func.afc.lanes["lane1"]

    def test_resolves_only_lane_when_toolhead_is_empty(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None

        lane = func._resolve_calibration_lane("extruder", None, MagicMock())

        assert lane is func.afc.lanes["lane1"]

    def test_rejects_ambiguous_lane_when_name_is_omitted(self) -> None:
        func, extruder = _make_calibration_func()
        func.afc.current = None
        second_lane = MagicMock()
        second_lane.name = "lane2"
        second_lane.extruder_obj = extruder
        func.afc.lanes["lane2"] = second_lane
        gcmd = MagicMock()

        lane = func._resolve_calibration_lane("extruder", None, gcmd)

        assert lane is None
        func.afc.error.AFC_error.assert_called_once_with(
            "Select a lane from AFC_TOOLHEAD_CALIBRATION before continuing.", pause=False,
            gcmd=gcmd)

    def test_rejects_lane_owned_by_different_extruder(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.tools["other"] = MagicMock()
        gcmd = MagicMock()

        lane = func._resolve_calibration_lane("other", "lane1", gcmd)

        assert lane is None
        func.afc.error.AFC_error.assert_called_once_with(
            "Lane 'lane1' does not belong to extruder 'other'.", pause=False, gcmd=gcmd)


class TestLoadCalibrationLane:
    def test_rejects_unknown_extruder(self) -> None:
        func, _ = _make_calibration_func()
        prompt = MagicMock()

        lane = func._load_calibration_lane("other", "lane1", prompt)

        assert lane is None
        func.afc.error.AFC_error.assert_called_once_with(
            "'other' is not a valid extruder", pause=False, gcmd=prompt.gcode)

    def test_rejects_unknown_lane(self) -> None:
        func, _ = _make_calibration_func()
        prompt = MagicMock()

        lane = func._load_calibration_lane("extruder", "other", prompt)

        assert lane is None
        func.afc.error.AFC_error.assert_called_once_with(
            "'other' is not a valid lane", pause=False, gcmd=prompt.gcode)

    def test_returns_lane_that_is_already_loaded(self) -> None:
        func, _ = _make_calibration_func()

        lane = func._load_calibration_lane("extruder", "lane1", MagicMock())

        assert lane is func.afc.lanes["lane1"]
        func.afc.CHANGE_TOOL.assert_not_called()

    def test_loads_explicit_lane_when_toolhead_is_empty(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        func.afc.CHANGE_TOOL.side_effect = (
            lambda lane, gcmd=None: setattr(func.afc, "current", lane.name))
        prompt = MagicMock()

        lane = func._load_calibration_lane("extruder", "lane1", prompt)

        assert lane is func.afc.lanes["lane1"]
        func.afc.CHANGE_TOOL.assert_called_once_with(lane, gcmd=prompt.gcode)
        assert func.afc.current == "lane1"

    def test_loads_only_lane_when_name_is_omitted(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        func.afc.CHANGE_TOOL.side_effect = (
            lambda lane, gcmd=None: setattr(func.afc, "current", lane.name))
        prompt = MagicMock()

        lane = func._load_calibration_lane("extruder", None, prompt)

        assert lane is func.afc.lanes["lane1"]
        func.afc.CHANGE_TOOL.assert_called_once_with(lane, gcmd=prompt.gcode)

    def test_rejects_ambiguous_lane_when_name_is_omitted(self) -> None:
        func, extruder = _make_calibration_func()
        func.afc.current = None
        second_lane = MagicMock(name="lane2")
        second_lane.name = "lane2"
        second_lane.extruder_obj = extruder
        func.afc.lanes["lane2"] = second_lane
        extruder.lanes["lane2"] = second_lane
        prompt = MagicMock()

        lane = func._load_calibration_lane("extruder", None, prompt)

        assert lane is None
        func.afc.CHANGE_TOOL.assert_not_called()
        func.afc.error.AFC_error.assert_called_once_with(
            "Select a lane from AFC_TOOLHEAD_CALIBRATION before continuing.", pause=False,
            gcmd=prompt.gcode)

    def test_rejects_lane_owned_by_different_extruder(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.tools["other"] = MagicMock()
        prompt = MagicMock()

        lane = func._load_calibration_lane("other", "lane1", prompt)

        assert lane is None
        func.afc.CHANGE_TOOL.assert_not_called()
        func.afc.error.AFC_error.assert_called_once_with(
            "Lane 'lane1' does not belong to extruder 'other'.", pause=False, gcmd=prompt.gcode)

    def test_reports_failed_lane_load(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        prompt = MagicMock()

        lane = func._load_calibration_lane("extruder", "lane1", prompt)

        assert lane is None
        func.afc.error.AFC_error.assert_called_once_with(
            "Lane 'lane1' did not load for toolhead calibration.", pause=False,
            gcmd=prompt.gcode)


class TestCmdAfcCutterCalibration:
    def _make_loaded_func(self) -> afcFunction:
        """
        Build a calibration helper with filament loaded.

        :return afcFunction: initialized AFC function
        """
        func, _ = _make_calibration_func()
        return func

    def test_reports_failed_automatic_lane_load(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        gcmd = _make_gcmd()

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Lane 'lane1' did not load for toolhead calibration.", pause=False, gcmd=gcmd)

    def test_requires_a_configured_lane(self) -> None:
        func = self._make_loaded_func()
        del func.afc.lanes["lane1"]
        gcmd = _make_gcmd()

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Select a lane from AFC_TOOLHEAD_CALIBRATION before continuing.", pause=False,
            gcmd=gcmd)

    def test_rejects_extruder_that_does_not_own_loaded_lane(self) -> None:
        func = self._make_loaded_func()
        func.afc.tools["other"] = MagicMock()
        gcmd = _make_gcmd(EXTRUDER="other", LANE="lane1")

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Lane 'lane1' does not belong to extruder 'other'.", pause=False, gcmd=gcmd)

    def test_rejects_unknown_extruder(self) -> None:
        func = self._make_loaded_func()
        gcmd = _make_gcmd(EXTRUDER="other")

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Select a valid extruder for cutter calibration.", pause=False, gcmd=gcmd)

    def test_rejects_calibration_during_print(self) -> None:
        func = self._make_loaded_func()
        print_stats = func.printer.lookup_object("print_stats")
        print_stats.get_status.return_value = {"state": "printing", "filename": "part.gcode"}
        gcmd = _make_gcmd()

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Cutter retract calibration cannot run during a print.", pause=False, gcmd=gcmd)

    def test_rejects_prompt_action_before_start(self) -> None:
        func = self._make_loaded_func()
        gcmd = _make_gcmd(MOVE=-1)

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Start AFC_CUTTER_CALIBRATION before using its prompt actions.", pause=False,
            gcmd=gcmd)

    @pytest.mark.parametrize("action", [{"COMPLETE": 1}, {"CANCEL": 1}])
    def test_rejects_other_prompt_actions_before_start(self,
                                                       action: Dict[str, int]) -> None:
        func = self._make_loaded_func()
        gcmd = _make_gcmd(**action)

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Start AFC_CUTTER_CALIBRATION before using its prompt actions.", pause=False,
            gcmd=gcmd)

    def test_start_opens_prompt_without_moving(self) -> None:
        func = self._make_loaded_func()

        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(MARGIN=8))

        assert func.cutter_calibration_active is True
        assert func.cutter_calibration_margin == 8.0
        assert func.cutter_calibration_extruder == "extruder"
        assert func.cutter_calibration_lane == "lane1"
        func.afc.move_e_pos.assert_not_called()
        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("prompt_begin Cutter Retract Calibration" in message for message in raw_messages)

    def test_start_loads_selected_lane_when_toolhead_is_empty(self) -> None:
        func = self._make_loaded_func()
        func.afc.current = None
        func.afc.CHANGE_TOOL.side_effect = (
            lambda lane, gcmd=None: setattr(func.afc, "current", lane.name))
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1")

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.CHANGE_TOOL.assert_called_once_with(func.afc.lanes["lane1"], gcmd=gcmd)
        assert func.cutter_calibration_active is True

    def test_start_resolves_only_extruder_and_lane_when_toolhead_is_empty(self) -> None:
        func = self._make_loaded_func()
        func.afc.current = None
        func.afc.CHANGE_TOOL.side_effect = (
            lambda lane, gcmd=None: setattr(func.afc, "current", lane.name))
        gcmd = _make_gcmd()

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.CHANGE_TOOL.assert_called_once_with(func.afc.lanes["lane1"], gcmd=gcmd)
        assert func.cutter_calibration_extruder == "extruder"
        assert func.cutter_calibration_lane == "lane1"

    def test_start_requires_extruder_when_multiple_are_configured(self) -> None:
        func = self._make_loaded_func()
        func.afc.current = None
        func.afc.tools["other"] = MagicMock()
        gcmd = _make_gcmd()

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Select a valid extruder for cutter calibration.", pause=False, gcmd=gcmd)
        func.afc.CHANGE_TOOL.assert_not_called()

    def test_active_session_rejects_a_different_extruder(self) -> None:
        func = self._make_loaded_func()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd())
        func.cutter_calibration_extruder = "other"
        gcmd = _make_gcmd(EXTRUDER="extruder")

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Finish or cancel the active cutter calibration first.", pause=False, gcmd=gcmd)

    def test_active_session_rejects_a_different_lane(self) -> None:
        func = self._make_loaded_func()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd())
        func.cutter_calibration_lane = "other"
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1")

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Finish or cancel the active cutter calibration first.", pause=False, gcmd=gcmd)

    def test_active_session_rejects_when_loaded_lane_changes(self) -> None:
        func = self._make_loaded_func()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd())
        func.afc.current = "other"
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1")

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Finish or cancel the active cutter calibration first.", pause=False, gcmd=gcmd)

    def test_start_rejects_active_tool_stn_unload_calibration(self) -> None:
        func = self._make_loaded_func()
        func.stn_unload_calibration_active = True
        gcmd = _make_gcmd()

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "tool_stn_unload calibration is already active.", pause=False, gcmd=gcmd)
        assert func.cutter_calibration_active is False

    def test_retract_tracks_total_distance(self) -> None:
        func = self._make_loaded_func()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd())

        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(MOVE=-5))

        func.afc.move_e_pos.assert_called_once_with(
            -5.0, 1.0, "Cutter retract calibration", wait_tool=True)
        assert func.cutter_calibration_distance == 5.0

    def test_undo_cannot_advance_past_start(self) -> None:
        func = self._make_loaded_func()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd())
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(MOVE=-1))
        func.afc.move_e_pos.reset_mock()

        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(MOVE=5))

        func.afc.move_e_pos.assert_called_once_with(
            1.0, 1.0, "Cutter retract calibration", wait_tool=True)
        assert func.cutter_calibration_distance == 0.0

    def test_complete_restores_updates_and_saves_retract_length(self) -> None:
        func = self._make_loaded_func()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd())
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(MOVE=-25))
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(MOVE=-10))
        func.afc.move_e_pos.reset_mock()

        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(COMPLETE=1))

        func.afc.move_e_pos.assert_called_once_with(
            35.0, 1.0, "Cutter calibration restore", wait_tool=True)
        func.afc.gcode.run_script_from_command.assert_called_with(
            "SET_GCODE_VARIABLE MACRO=_AFC_CUT_TIP_VARS VARIABLE=retract_length VALUE=25.0")
        func.ConfigRewrite.assert_called_once_with(
            "gcode_macro _AFC_CUT_TIP_VARS", "variable_retract_length", 25.0, "")
        assert func.cutter_calibration_active is False
        assert func.cutter_calibration_distance == 0.0
        assert func.cutter_calibration_extruder is None
        assert func.cutter_calibration_lane is None

    def test_complete_updates_per_tool_retract_length_when_configured(self) -> None:
        func = self._make_loaded_func()
        func.printer._objects["gcode_macro _AFC_CUT_TIP_VARS_extruder"] = MagicMock()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(EXTRUDER="extruder", MARGIN=5))
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(EXTRUDER="extruder", MOVE=-20))

        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(EXTRUDER="extruder", COMPLETE=1))

        func.afc.gcode.run_script_from_command.assert_called_with(
            "SET_GCODE_VARIABLE MACRO=_AFC_CUT_TIP_VARS_extruder "
            "VARIABLE=retract_length VALUE=15.0")
        func.ConfigRewrite.assert_called_once_with(
            "gcode_macro _AFC_CUT_TIP_VARS_extruder",
            "variable_retract_length", 15.0, "")

    def test_complete_without_retraction_rejects_save(self) -> None:
        func = self._make_loaded_func()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd())
        gcmd = _make_gcmd(COMPLETE=1)

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.move_e_pos.assert_not_called()
        func.ConfigRewrite.assert_not_called()
        func.afc.error.AFC_error.assert_called_once_with(
            "Retract filament past the safety margin before saving.", pause=False, gcmd=gcmd)
        assert func.cutter_calibration_active is True

    def test_rejects_move_larger_than_limit(self) -> None:
        func = self._make_loaded_func()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd())
        gcmd = _make_gcmd(MOVE=-26)

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Cutter calibration MOVE must be between -25mm and 25mm.", pause=False, gcmd=gcmd)

    def test_rejects_total_retraction_larger_than_limit(self) -> None:
        func = self._make_loaded_func()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd())
        func.cutter_calibration_distance = 190.0
        gcmd = _make_gcmd(MOVE=-11)

        func.cmd_AFC_CUTTER_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Cutter calibration is limited to 200mm of total retraction.", pause=False, gcmd=gcmd)

    def test_undo_at_start_does_not_move(self) -> None:
        func = self._make_loaded_func()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd())

        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(MOVE=5))

        func.afc.move_e_pos.assert_not_called()

    def test_cancel_restores_without_saving(self) -> None:
        func = self._make_loaded_func()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd())
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(MOVE=-5))
        func.afc.move_e_pos.reset_mock()

        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(CANCEL=1))

        func.afc.move_e_pos.assert_called_once_with(
            5.0, 1.0, "Cutter calibration restore", wait_tool=True)
        func.ConfigRewrite.assert_not_called()
        assert func.cutter_calibration_active is False
        assert func.cutter_calibration_distance == 0.0
        assert func.cutter_calibration_extruder is None
        assert func.cutter_calibration_lane is None

    def test_cancel_without_movement_logs_exact_message(self) -> None:
        func = self._make_loaded_func()
        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd())
        func.logger.messages = []

        func.cmd_AFC_CUTTER_CALIBRATION(_make_gcmd(CANCEL=1))

        assert func.logger.messages == [
            ("raw", "// action:prompt_end"),
            ("info", "Cutter retract calibration cancelled; filament restored to its "
                     "starting position."),
        ]


# AFC calibration menu and commands

class TestCmdAfcCalibration:
    def test_loaded_toolhead_keeps_system_calibrations_visible(self) -> None:
        func, _ = _make_calibration_func()
        calibratable_lane = MagicMock()
        calibratable_lane.extruder_obj.is_standalone.return_value = False
        unit = MagicMock()
        unit.lanes = {"lane1": calibratable_lane}
        func.afc.units = {"unit1": unit}

        func.cmd_AFC_CALIBRATION(_make_gcmd())

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("following prompts will lead you through" in message
                   for message in raw_messages)
        assert any("UNIT_CALIBRATION UNIT=unit1" in message
                   for message in raw_messages)
        assert any("Toolhead Calibration|AFC_TOOLHEAD_CALIBRATION" in message
                   for message in raw_messages)

    def test_unloaded_menu_includes_toolhead_and_all_lane_actions(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        func.afc.units = {}

        func.cmd_AFC_CALIBRATION(_make_gcmd())

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("Toolhead Calibration|AFC_TOOLHEAD_CALIBRATION|primary" in message
                   for message in raw_messages)
        assert any("All Lanes in all units|ALL_CALIBRATION" in message
                   for message in raw_messages)

    def test_unloaded_menu_skips_units_without_calibratable_lanes(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        empty_unit = MagicMock()
        empty_unit.lanes = {}
        standalone_lane = MagicMock()
        standalone_lane.extruder_obj.is_standalone.return_value = True
        standalone_unit = MagicMock()
        standalone_unit.lanes = {"lane1": standalone_lane}
        func.afc.units = {"empty": empty_unit, "standalone": standalone_unit}

        func.cmd_AFC_CALIBRATION(_make_gcmd())

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert not any("UNIT_CALIBRATION UNIT=empty" in message for message in raw_messages)
        assert not any("UNIT_CALIBRATION UNIT=standalone" in message for message in raw_messages)

    def test_unloaded_menu_styles_multiple_units(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        first_lane = MagicMock()
        first_lane.extruder_obj.is_standalone.return_value = False
        second_lane = MagicMock()
        second_lane.extruder_obj.is_standalone.return_value = False
        first_unit = MagicMock()
        first_unit.lanes = {"lane1": first_lane}
        second_unit = MagicMock()
        second_unit.lanes = {"lane2": second_lane}
        func.afc.units = {"first": first_unit, "second": second_unit}

        func.cmd_AFC_CALIBRATION(_make_gcmd())

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("first|UNIT_CALIBRATION UNIT=first|primary" in message
                   for message in raw_messages)
        assert any("second|UNIT_CALIBRATION UNIT=second|secondary" in message
                   for message in raw_messages)

    def test_button_calibration_entry_hidden_without_adc_buttons(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.units = {}

        func.cmd_AFC_CALIBRATION(_make_gcmd())

        assert func.logger.messages == [
            ("raw", "// action:prompt_begin AFC Calibration"),
            ("raw", "// action:prompt_text The following prompts will lead you through the "
             "calibration of your AFC unit(s). First, select a unit to calibrate. *All values "
             "will be automatically updated in the appropriate config sections."),
            ("raw", "// action:prompt_button Toolhead Calibration|AFC_TOOLHEAD_CALIBRATION|"
             "primary"),
            ("raw", "// action:prompt_footer_button All Lanes in all units|ALL_CALIBRATION|"
             "secondary"),
            ("raw", "// action:prompt_footer_button Cancel|RESPOND TYPE=command "
             "MSG=action:prompt_end|warning"),
            ("raw", "// action:prompt_show"),
        ]

    def test_button_calibration_entry_shown_with_adc_buttons(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.units = {}
        func.printer._objects["AFC_button lane7"] = _make_adc_button("lane7")

        func.cmd_AFC_CALIBRATION(_make_gcmd())

        assert func.logger.messages == [
            ("raw", "// action:prompt_begin AFC Calibration"),
            ("raw", "// action:prompt_text The following prompts will lead you through the "
             "calibration of your AFC unit(s). First, select a unit to calibrate. *All values "
             "will be automatically updated in the appropriate config sections."),
            ("raw", "// action:prompt_button Toolhead Calibration|AFC_TOOLHEAD_CALIBRATION|"
             "primary"),
            ("raw", "// action:prompt_button Button Calibration|AFC_BUTTON_CALIBRATION|"
             "secondary"),
            ("raw", "// action:prompt_footer_button All Lanes in all units|ALL_CALIBRATION|"
             "secondary"),
            ("raw", "// action:prompt_footer_button Cancel|RESPOND TYPE=command "
             "MSG=action:prompt_end|warning"),
            ("raw", "// action:prompt_show"),
        ]

    def test_toolhead_and_button_calibration_continue_color_cascade_past_units(self) -> None:
        """index-based styling used to desync from the displayed buttons
        whenever a unit got filtered out (no lanes / standalone-only),
        since enumerate() still counted the skipped unit toward parity.
        Using a visible-only counter keeps Toolhead/Button Calibration
        alternating correctly regardless of how many units were skipped."""
        func, _ = _make_calibration_func()
        skipped_unit = MagicMock()
        skipped_unit.lanes = {}
        visible_lane = MagicMock()
        visible_lane.extruder_obj.is_standalone.return_value = False
        visible_unit = MagicMock()
        visible_unit.lanes = {"lane1": visible_lane}
        # One skipped unit (odd count) ahead of one visible unit: the visible unit lands on
        # "primary" (visible_count == 0), so Toolhead Calibration must continue the cascade as
        # "secondary".
        func.afc.units = {"skipped": skipped_unit, "visible": visible_unit}
        func.printer._objects["AFC_button lane7"] = _make_adc_button("lane7")

        func.cmd_AFC_CALIBRATION(_make_gcmd())

        assert func.logger.messages == [
            ("raw", "// action:prompt_begin AFC Calibration"),
            ("raw", "// action:prompt_text The following prompts will lead you through the "
             "calibration of your AFC unit(s). First, select a unit to calibrate. *All values "
             "will be automatically updated in the appropriate config sections."),
            ("raw", "// action:prompt_button visible|UNIT_CALIBRATION UNIT=visible|primary"),
            ("raw", "// action:prompt_button Toolhead Calibration|AFC_TOOLHEAD_CALIBRATION|"
             "secondary"),
            ("raw", "// action:prompt_button Button Calibration|AFC_BUTTON_CALIBRATION|"
             "primary"),
            ("raw", "// action:prompt_footer_button All Lanes in all units|ALL_CALIBRATION|"
             "secondary"),
            ("raw", "// action:prompt_footer_button Cancel|RESPOND TYPE=command "
             "MSG=action:prompt_end|warning"),
            ("raw", "// action:prompt_show"),
        ]


class TestGetAdcButtons:
    def test_returns_only_adc_buttons(self) -> None:
        func, _ = _make_calibration_func()
        adc_button = _make_adc_button("lane7", is_adc=True)
        digital_button = _make_adc_button("lane8", is_adc=False)
        func.printer._objects["AFC_button lane7"] = adc_button
        func.printer._objects["AFC_button lane8"] = digital_button

        assert func._get_adc_buttons() == [adc_button]

    def test_returns_empty_when_no_buttons_registered(self) -> None:
        func, _ = _make_calibration_func()
        assert func._get_adc_buttons() == []


class TestCmdAfcButtonCalibration:
    _TEXT = ("// action:prompt_text The following prompts will lead you through calibrating "
            "the press threshold for your ADC based buttons. First, select a button to "
            "calibrate.")
    _CANCEL = ("// action:prompt_footer_button Cancel|RESPOND TYPE=command "
              "MSG=action:prompt_end|warning")

    def test_creates_button_per_adc_button_with_alternating_style(self) -> None:
        func, _ = _make_calibration_func()
        func.printer._objects["AFC_button lane7"] = _make_adc_button("lane7")
        func.printer._objects["AFC_button lane8"] = _make_adc_button("lane8")

        func.cmd_AFC_BUTTON_CALIBRATION(_make_gcmd())

        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Button Calibration"),
            ("raw", self._TEXT),
            ("raw", "// action:prompt_button lane7|BUTTON_CALIBRATION BUTTON=lane7|primary"),
            ("raw", "// action:prompt_button lane8|BUTTON_CALIBRATION BUTTON=lane8|secondary"),
            ("raw", "// action:prompt_footer_button All Buttons|BUTTON_CALIBRATION "
             "ALL=1|secondary"),
            ("raw", self._CANCEL),
            ("raw", "// action:prompt_show"),
        ]

    def test_includes_all_buttons_footer(self) -> None:
        func, _ = _make_calibration_func()
        func.printer._objects["AFC_button lane7"] = _make_adc_button("lane7")

        func.cmd_AFC_BUTTON_CALIBRATION(_make_gcmd())

        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Button Calibration"),
            ("raw", self._TEXT),
            ("raw", "// action:prompt_button lane7|BUTTON_CALIBRATION BUTTON=lane7|primary"),
            ("raw", "// action:prompt_footer_button All Buttons|BUTTON_CALIBRATION "
             "ALL=1|secondary"),
            ("raw", self._CANCEL),
            ("raw", "// action:prompt_show"),
        ]

    def test_no_adc_buttons_shows_prompt_with_no_button_entries(self) -> None:
        func, _ = _make_calibration_func()

        func.cmd_AFC_BUTTON_CALIBRATION(_make_gcmd())

        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Button Calibration"),
            ("raw", self._TEXT),
            ("raw", "// action:prompt_footer_button All Buttons|BUTTON_CALIBRATION "
             "ALL=1|secondary"),
            ("raw", self._CANCEL),
            ("raw", "// action:prompt_show"),
        ]


class TestResetButtonCalibrationValues:
    def test_resets_all_session_state(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_baseline = 1000.0
        func.button_calibration_samples = [1.0, 2.0]
        func.button_calibration_first_value = 500.0
        func.button_calibration_gcmd = _make_gcmd()
        func.button_calibration_last_log_time = 5.0
        func.button_calibration_await_button = button
        func.button_calibration_await_baseline = 1000.0
        func.button_calibration_restart_pending = True
        func.button_calibration_stage_deadline = 1234.0
        func.button_calibration_stage_started = 1200.0
        func.button_calibration_no_sample_warned = True
        func.button_calibration_saved = ["lane7"]
        func.button_calibration_skipped = ["lane8"]

        func._reset_button_calibration_values()

        assert func.button_calibration_active is False
        assert func.button_calibration_queue == []
        assert func.button_calibration_stage == ""
        assert func.button_calibration_baseline is None
        assert func.button_calibration_samples == []
        assert func.button_calibration_first_value is None
        assert func.button_calibration_gcmd is None
        assert func.button_calibration_last_log_time == 0.0
        assert func.button_calibration_await_button is None
        assert func.button_calibration_await_baseline is None
        assert func.button_calibration_restart_pending is False
        assert func.button_calibration_stage_deadline is None
        assert func.button_calibration_stage_started is None
        assert func.button_calibration_no_sample_warned is False
        assert func.button_calibration_saved == []
        assert func.button_calibration_skipped == []

    def test_stops_poll_timer(self) -> None:
        func, _ = _make_calibration_func()
        func.button_calibration_timer = MagicMock()
        func.afc.reactor.update_timer = MagicMock()

        func._reset_button_calibration_values()

        func.afc.reactor.update_timer.assert_called_once_with(
            func.button_calibration_timer, func.afc.reactor.NEVER)


class TestShowButtonCalibrationPrompt:
    def test_shows_prompt_text_with_cancel_footer(self) -> None:
        func, _ = _make_calibration_func()
        func.button_calibration_gcmd = _make_gcmd()

        func._show_button_calibration_prompt("Press and hold the lane7 button now.")

        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Button Calibration"),
            ("raw", "// action:prompt_text Press and hold the lane7 button now."),
            ("raw", "// action:prompt_footer_button Cancel|BUTTON_CALIBRATION CANCEL=1|warning"),
            ("raw", "// action:prompt_show"),
        ]


class TestCmdButtonCalibration:
    def test_cancel_resets_state_and_shows_cancelled_prompt(self) -> None:
        func, _ = _make_calibration_func()
        func.button_calibration_active = True

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(CANCEL=1))

        assert func.button_calibration_active is False
        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Button Calibration Cancelled"),
            ("raw", "// action:prompt_text Button calibration cancelled."),
            ("raw", "// action:prompt_footer_button Cancel|RESPOND TYPE=command "
             "MSG=action:prompt_end|warning"),
            ("raw", "// action:prompt_show"),
        ]

    def test_already_active_shows_error_without_touching_queue(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.printer._objects["AFC_button lane7"] = button
        func.button_calibration_active = True
        func.button_calibration_queue = [button]

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(BUTTON="lane7"))

        func.afc.error.AFC_error.assert_called_once()
        assert func.button_calibration_queue == [button]
        assert func.button_calibration_active is True

    def test_already_active_does_not_close_the_active_session_s_prompt(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.printer._objects["AFC_button lane7"] = button
        func.button_calibration_active = True
        func.button_calibration_queue = [button]

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(BUTTON="lane7"))

        # The "already running" guard reports an error and returns without showing any prompt at
        # all, so nothing (prompt_end included) fires.
        assert func.logger.messages == []

    def test_refuses_to_start_during_a_print(self) -> None:
        func, _ = _make_calibration_func()
        func.printer._objects["AFC_button lane7"] = _make_adc_button("lane7")
        func.printer._objects["print_stats"].get_status.return_value = {
            "state": "printing", "filename": "test.gcode"}

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(BUTTON="lane7"))

        func.afc.error.AFC_error.assert_called_once()
        assert "print" in func.afc.error.AFC_error.call_args[0][0]
        assert func.button_calibration_active is False
        assert func.button_calibration_queue == []

    def test_allows_starting_when_not_printing(self) -> None:
        """Independent coverage of the in_print operand: the standby state
        the fixture defaults to must not itself block starting."""
        func, _ = _make_calibration_func()
        func.printer._objects["AFC_button lane7"] = _make_adc_button("lane7")

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(BUTTON="lane7"))

        func.afc.error.AFC_error.assert_not_called()
        assert func.button_calibration_active is True

    def test_invalid_button_name_shows_error(self) -> None:
        func, _ = _make_calibration_func()
        func.printer._objects["AFC_button lane7"] = _make_adc_button("lane7")

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(BUTTON="not_a_button"))

        func.afc.error.AFC_error.assert_called_once()
        assert "not_a_button" in func.afc.error.AFC_error.call_args[0][0]
        assert func.button_calibration_active is False

    def test_no_adc_buttons_configured_shows_error(self) -> None:
        func, _ = _make_calibration_func()

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(BUTTON="all"))

        func.afc.error.AFC_error.assert_called_once()
        assert func.button_calibration_active is False

    def test_single_button_starts_session(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.printer._objects["AFC_button lane7"] = button

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(BUTTON="lane7"))

        assert func.button_calibration_active is True
        assert func.button_calibration_queue == [button]
        assert func.button_calibration_stage == "press_1"
        assert func.button_calibration_baseline is None
        assert func.button_calibration_restart_pending is False

    def test_all_buttons_starts_session_with_full_queue(self) -> None:
        func, _ = _make_calibration_func()
        b7 = _make_adc_button("lane7")
        b8 = _make_adc_button("lane8")
        func.printer._objects["AFC_button lane7"] = b7
        func.printer._objects["AFC_button lane8"] = b8

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(BUTTON="all"))

        assert func.button_calibration_queue == [b7, b8]

    def test_all_param_selects_every_button_even_when_one_is_named_all(self) -> None:
        """ALL=1 is the dedicated "calibrate everything" parameter, so it must
        select every button even when one of them happens to be named "all",
        a case the BUTTON=all shortcut can't handle on its own."""
        func, _ = _make_calibration_func()
        literal_all = _make_adc_button("all")
        other = _make_adc_button("lane8")
        func.printer._objects["AFC_button all"] = literal_all
        func.printer._objects["AFC_button lane8"] = other

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(ALL=1))

        assert func.button_calibration_queue == [literal_all, other]

    def test_all_param_ignores_a_button_name(self) -> None:
        func, _ = _make_calibration_func()
        b7 = _make_adc_button("lane7")
        b8 = _make_adc_button("lane8")
        func.printer._objects["AFC_button lane7"] = b7
        func.printer._objects["AFC_button lane8"] = b8

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(ALL=1, BUTTON="lane7"))

        assert func.button_calibration_queue == [b7, b8]

    def test_button_literally_named_all_is_selected_on_its_own(self) -> None:
        """A button named "all" must still be individually selectable: an
        exact name match wins over the "calibrate everything" shortcut, or
        that button could never be picked on its own."""
        func, _ = _make_calibration_func()
        literal_all = _make_adc_button("all")
        other = _make_adc_button("lane8")
        func.printer._objects["AFC_button all"] = literal_all
        func.printer._objects["AFC_button lane8"] = other

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(BUTTON="all"))

        assert func.button_calibration_queue == [literal_all]

    def test_starts_a_fresh_stage_clock(self) -> None:
        func, _ = _make_calibration_func()
        func.printer._objects["AFC_button lane7"] = _make_adc_button("lane7")
        func.afc.reactor._monotonic = 500.0

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(BUTTON="lane7"))

        assert func.button_calibration_stage_deadline == 500.0 + BUTTON_CALIBRATION_STAGE_TIMEOUT
        assert func.button_calibration_stage_started == 500.0
        assert func.button_calibration_no_sample_warned is False

    def test_starts_poll_timer(self) -> None:
        func, _ = _make_calibration_func()
        func.printer._objects["AFC_button lane7"] = _make_adc_button("lane7")
        func.button_calibration_timer = MagicMock()
        func.afc.reactor.update_timer = MagicMock()

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(BUTTON="lane7"))

        func.afc.reactor.update_timer.assert_called_once_with(
            func.button_calibration_timer, func.afc.reactor.NOW)

    def test_shows_press_prompt_for_first_button(self) -> None:
        func, _ = _make_calibration_func()
        func.printer._objects["AFC_button lane7"] = _make_adc_button("lane7")

        func.cmd_BUTTON_CALIBRATION(_make_gcmd(BUTTON="lane7"))

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("Press and hold the lane7 button now." in message for message in raw_messages)


class TestCmdButtonCalibrationRestart:
    def test_refuses_when_not_pending(self) -> None:
        func, _ = _make_calibration_func()
        func.button_calibration_restart_pending = False
        func.afc.gcode.respond_raw = MagicMock()

        func.cmd_BUTTON_CALIBRATION_RESTART(_make_gcmd())

        func.afc.error.AFC_error.assert_called_once()
        func.afc.gcode.respond_raw.assert_not_called()
        func.afc.gcode.run_script_from_command.assert_not_called()

    def test_restarts_and_closes_prompt_when_pending(self) -> None:
        func, _ = _make_calibration_func()
        func.button_calibration_restart_pending = True
        func.afc.gcode.respond_raw = MagicMock()

        func.cmd_BUTTON_CALIBRATION_RESTART(_make_gcmd())

        func.afc.gcode.respond_raw.assert_called_once_with("// action:prompt_end")
        func.afc.gcode.run_script_from_command.assert_called_once_with("RESTART")
        assert func.button_calibration_restart_pending is False

    def test_refuses_during_a_print_but_leaves_pending_set(self) -> None:
        """A print that started after calibration completed but before the
        user clicked Restart must not be able to force a mid-print restart,
        but the pending flag must survive so the restart can still run once
        printing finishes."""
        func, _ = _make_calibration_func()
        func.button_calibration_restart_pending = True
        func.afc.gcode.respond_raw = MagicMock()
        func.printer._objects["print_stats"].get_status.return_value = {
            "state": "printing", "filename": "test.gcode"}

        func.cmd_BUTTON_CALIBRATION_RESTART(_make_gcmd())

        func.afc.error.AFC_error.assert_called_once()
        assert "print" in func.afc.error.AFC_error.call_args[0][0]
        func.afc.gcode.respond_raw.assert_not_called()
        func.afc.gcode.run_script_from_command.assert_not_called()
        assert func.button_calibration_restart_pending is True


class TestCollectButtonSample:
    def test_returns_none_while_collecting(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        for _ in range(button.calibration_sample_count - 1):
            assert func._collect_button_sample(button, 1000.0) is None
        assert len(func.button_calibration_samples) == button.calibration_sample_count - 1

    def test_returns_average_and_clears_buffer_once_full(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        values = [990.0, 995.0, 1000.0, 1005.0, 1010.0][:button.calibration_sample_count]
        result = None
        for value in values:
            result = func._collect_button_sample(button, value)
        assert result == pytest.approx(sum(values) / len(values))
        assert func.button_calibration_samples == []

    def test_uses_a_different_buttons_own_sample_count(self) -> None:
        """Independent coverage that the count comes from the button passed
        in, not a fixed/shared constant: a button configured for 2 samples
        must average after 2 reads, not BUTTON_CALIBRATION_SAMPLE_COUNT."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.calibration_sample_count = 2
        assert func._collect_button_sample(button, 100.0) is None
        result = func._collect_button_sample(button, 200.0)
        assert result == pytest.approx(150.0)
        assert func.button_calibration_samples == []


class TestIsReleased:
    def test_within_press_delta_returns_true_without_checking_ratio(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")

        result = func._is_released(button, 1000.0, 1000.0 + BUTTON_CALIBRATION_PRESS_DELTA)

        assert result is True
        button.ratio_at_resistance.assert_not_called()

    def test_outside_press_delta_but_within_ratio_tolerance_returns_true(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.ratio_at_resistance.side_effect = {
            234995300.0: 0.99998,
            469995300.0: 0.99999,
        }.get

        result = func._is_released(button, 234995300.0, 469995300.0)

        assert result is True

    def test_outside_press_delta_and_outside_ratio_tolerance_returns_false(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.ratio_at_resistance.side_effect = {
            234995300.0: 0.5,
            469995300.0: 0.99999,
        }.get

        result = func._is_released(button, 234995300.0, 469995300.0)

        assert result is False


class TestStartNextButtonCalibration:
    def test_sets_stage_and_clears_baseline_and_samples(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_baseline = 1234.0
        func.button_calibration_samples = [1.0, 2.0]

        func._start_next_button_calibration()

        assert func.button_calibration_stage == "press_1"
        assert func.button_calibration_baseline is None
        assert func.button_calibration_samples == []

    def test_shows_press_prompt_for_head_of_queue(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane8")
        func.button_calibration_queue = [button]
        func.button_calibration_gcmd = _make_gcmd()

        func._start_next_button_calibration()

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("Press and hold the lane8 button now." in message for message in raw_messages)

    def test_starts_a_fresh_stage_clock(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane8")
        func.button_calibration_queue = [button]
        func.button_calibration_gcmd = _make_gcmd()
        func.button_calibration_stage_deadline = None
        func.button_calibration_no_sample_warned = True
        func.afc.reactor._monotonic = 500.0

        func._start_next_button_calibration()

        assert func.button_calibration_stage_deadline == 500.0 + BUTTON_CALIBRATION_STAGE_TIMEOUT
        assert func.button_calibration_stage_started == 500.0
        assert func.button_calibration_no_sample_warned is False


class TestStartButtonCalibrationStageClock:
    def test_sets_deadline_relative_to_now(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.reactor._monotonic = 200.0

        func._start_button_calibration_stage_clock()

        assert func.button_calibration_stage_deadline == 200.0 + BUTTON_CALIBRATION_STAGE_TIMEOUT

    def test_sets_stage_started_to_now(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.reactor._monotonic = 200.0

        func._start_button_calibration_stage_clock()

        assert func.button_calibration_stage_started == 200.0

    def test_clears_no_sample_warned(self) -> None:
        func, _ = _make_calibration_func()
        func.button_calibration_no_sample_warned = True

        func._start_button_calibration_stage_clock()

        assert func.button_calibration_no_sample_warned is False


class TestTimeoutButtonCalibration:
    def test_resets_all_session_state(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "release"
        func.button_calibration_gcmd = _make_gcmd()

        func._timeout_button_calibration(button, gcmd="callback")

        assert func.button_calibration_active is False
        assert func.button_calibration_queue == []
        assert func.button_calibration_stage == ""

    def test_reports_the_button_and_stage_that_timed_out(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "release"
        func.button_calibration_gcmd = _make_gcmd()

        func._timeout_button_calibration(button, gcmd="callback")

        func.afc.error.AFC_error.assert_called_once_with(
            "Button calibration timed out waiting on lane7 during the 'release' stage; "
            "calibration was cancelled.", pause=False, gcmd="callback")

    def test_shows_a_prompt_when_a_gcmd_was_recorded(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_gcmd = _make_gcmd()

        func._timeout_button_calibration(button, gcmd="callback")

        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Button Calibration Timed Out"),
            ("raw", "// action:prompt_text Button calibration timed out waiting on lane7. "
             "Calibration was cancelled."),
            ("raw", "// action:prompt_footer_button Cancel|RESPOND TYPE=command "
             "MSG=action:prompt_end|warning"),
            ("raw", "// action:prompt_show"),
        ]

    def test_no_prompt_when_no_gcmd_was_recorded(self) -> None:
        """Independent coverage of the `gcmd is not None` branch: a session
        with no recorded gcmd (shouldn't normally happen) must still reset
        and report the error without crashing trying to show a prompt."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_gcmd = None

        func._timeout_button_calibration(button, gcmd="callback")

        func.afc.error.AFC_error.assert_called_once()
        assert func.logger.messages == []


class TestCancelButtonCalibrationForPrint:
    def test_resets_all_session_state(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "release"
        func.button_calibration_gcmd = _make_gcmd()

        func._cancel_button_calibration_for_print(gcmd="callback")

        assert func.button_calibration_active is False
        assert func.button_calibration_queue == []
        assert func.button_calibration_stage == ""

    def test_reports_the_cancellation(self) -> None:
        func, _ = _make_calibration_func()
        func.button_calibration_active = True
        func.button_calibration_queue = [_make_adc_button("lane7")]
        func.button_calibration_gcmd = _make_gcmd()

        func._cancel_button_calibration_for_print(gcmd="callback")

        func.afc.error.AFC_error.assert_called_once_with(
            "Button calibration cancelled because a print started.", pause=False, gcmd="callback")

    def test_shows_a_prompt_when_a_gcmd_was_recorded(self) -> None:
        func, _ = _make_calibration_func()
        func.button_calibration_active = True
        func.button_calibration_queue = [_make_adc_button("lane7")]
        func.button_calibration_gcmd = _make_gcmd()

        func._cancel_button_calibration_for_print(gcmd="callback")

        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Button Calibration Cancelled"),
            ("raw", "// action:prompt_text Button calibration cancelled because a print "
             "started."),
            ("raw", "// action:prompt_footer_button Cancel|RESPOND TYPE=command "
             "MSG=action:prompt_end|warning"),
            ("raw", "// action:prompt_show"),
        ]

    def test_no_prompt_when_no_gcmd_was_recorded(self) -> None:
        """Independent coverage of the `gcmd is not None` branch: a session
        with no recorded gcmd (shouldn't normally happen) must still reset
        and report the error without crashing trying to show a prompt."""
        func, _ = _make_calibration_func()
        func.button_calibration_active = True
        func.button_calibration_queue = [_make_adc_button("lane7")]
        func.button_calibration_gcmd = None

        func._cancel_button_calibration_for_print(gcmd="callback")

        func.afc.error.AFC_error.assert_called_once()
        assert func.logger.messages == []


class TestWarnIfAdcStalled:
    def test_already_warned_does_not_warn_again(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_no_sample_warned = True
        func.button_calibration_stage_started = 0.0

        func._warn_if_adc_stalled(button, BUTTON_CALIBRATION_ADC_SAMPLE_TIMEOUT + 10.0, gcmd="callback")

        func.afc.error.AFC_error.assert_not_called()

    def test_stage_not_started_does_not_warn(self) -> None:
        """Independent coverage of the `started is not None` operand: no
        stage-start reference means there's nothing to measure elapsed time
        against, so this must not warn (or crash) regardless of eventtime."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_no_sample_warned = False
        func.button_calibration_stage_started = None

        func._warn_if_adc_stalled(button, 10_000.0, gcmd="callback")

        func.afc.error.AFC_error.assert_not_called()

    def test_within_timeout_does_not_warn(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_no_sample_warned = False
        func.button_calibration_stage_started = 0.0

        func._warn_if_adc_stalled(button, BUTTON_CALIBRATION_ADC_SAMPLE_TIMEOUT - 1.0, gcmd="callback")

        func.afc.error.AFC_error.assert_not_called()
        assert func.button_calibration_no_sample_warned is False

    def test_past_timeout_warns_once_naming_the_pin(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7", pin="PA3")
        func.button_calibration_no_sample_warned = False
        func.button_calibration_stage_started = 0.0

        func._warn_if_adc_stalled(button, BUTTON_CALIBRATION_ADC_SAMPLE_TIMEOUT, gcmd="callback")

        func.afc.error.AFC_error.assert_called_once_with(
            "Button calibration: no ADC reading received for lane7 (pin PA3) after 5s; "
            "still waiting.", pause=False, gcmd="callback")
        assert func.button_calibration_no_sample_warned is True

    def test_second_call_past_timeout_does_not_warn_again(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_no_sample_warned = False
        func.button_calibration_stage_started = 0.0

        func._warn_if_adc_stalled(button, BUTTON_CALIBRATION_ADC_SAMPLE_TIMEOUT, gcmd="callback")
        func._warn_if_adc_stalled(button, BUTTON_CALIBRATION_ADC_SAMPLE_TIMEOUT + 1.0, gcmd="callback")

        func.afc.error.AFC_error.assert_called_once()


class TestPollAwaitRelease:
    def test_button_none_baseline_set_starts_next(self) -> None:
        func, _ = _make_calibration_func()
        next_button = _make_adc_button("lane8")
        func.button_calibration_active = True
        func.button_calibration_queue = [next_button]
        func.button_calibration_await_button = None
        func.button_calibration_await_baseline = 1000.0

        func._poll_await_release(100.0, gcmd="callback")

        assert func.button_calibration_stage == "press_1"

    def test_button_set_baseline_none_starts_next(self) -> None:
        func, _ = _make_calibration_func()
        prev_button = _make_adc_button("lane7")
        next_button = _make_adc_button("lane8")
        func.button_calibration_active = True
        func.button_calibration_queue = [next_button]
        func.button_calibration_await_button = prev_button
        func.button_calibration_await_baseline = None

        func._poll_await_release(100.0, gcmd="callback")

        assert func.button_calibration_stage == "press_1"

    def test_resistance_none_reschedules_without_progress(self) -> None:
        func, _ = _make_calibration_func()
        prev_button = _make_adc_button("lane7", resistance_sequence=[None])
        func.button_calibration_active = True
        func.button_calibration_queue = [_make_adc_button("lane8")]
        func.button_calibration_await_button = prev_button
        func.button_calibration_await_baseline = 1000.0

        result = func._poll_await_release(100.0, gcmd="callback")

        assert result == pytest.approx(100.0 + BUTTON_CALIBRATION_POLL_TIME)
        assert func.button_calibration_await_button is prev_button

    def test_resistance_none_past_adc_timeout_warns(self) -> None:
        func, _ = _make_calibration_func()
        prev_button = _make_adc_button("lane7", resistance_sequence=[None])
        func.button_calibration_active = True
        func.button_calibration_queue = [_make_adc_button("lane8")]
        func.button_calibration_await_button = prev_button
        func.button_calibration_await_baseline = 1000.0
        func.button_calibration_stage_started = 0.0

        func._poll_await_release(BUTTON_CALIBRATION_ADC_SAMPLE_TIMEOUT, gcmd="callback")

        func.afc.error.AFC_error.assert_called_once_with(
            "Button calibration: no ADC reading received for lane7 (pin PA0) after 5s; "
            "still waiting.", pause=False, gcmd="callback")

    def test_past_stage_deadline_times_out_the_session(self) -> None:
        func, _ = _make_calibration_func()
        prev_button = _make_adc_button("lane7")
        func.button_calibration_active = True
        func.button_calibration_queue = [_make_adc_button("lane8")]
        func.button_calibration_await_button = prev_button
        func.button_calibration_await_baseline = 1000.0
        func.button_calibration_stage_deadline = 100.0

        result = func._poll_await_release(100.0, gcmd="callback")

        assert result == func.afc.reactor.NEVER
        assert func.button_calibration_active is False
        func.afc.error.AFC_error.assert_called_once()

    def test_before_stage_deadline_does_not_time_out(self) -> None:
        """Independent coverage of the stage-deadline operand: being set
        isn't enough by itself, the current eventtime must have reached it."""
        func, _ = _make_calibration_func()
        prev_button = _make_adc_button("lane7", resistance_sequence=[None])
        func.button_calibration_active = True
        func.button_calibration_queue = [_make_adc_button("lane8")]
        func.button_calibration_await_button = prev_button
        func.button_calibration_await_baseline = 1000.0
        func.button_calibration_stage_deadline = 200.0

        result = func._poll_await_release(100.0, gcmd="callback")

        assert result == pytest.approx(100.0 + BUTTON_CALIBRATION_POLL_TIME)
        assert func.button_calibration_active is True
        func.afc.error.AFC_error.assert_not_called()

    def test_no_stage_deadline_does_not_time_out_regardless_of_eventtime(self) -> None:
        """Independent coverage of the `is not None` operand: no deadline at
        all must not time out even at an eventtime that would otherwise be
        past any real deadline."""
        func, _ = _make_calibration_func()
        prev_button = _make_adc_button("lane7", resistance_sequence=[None])
        func.button_calibration_active = True
        func.button_calibration_queue = [_make_adc_button("lane8")]
        func.button_calibration_await_button = prev_button
        func.button_calibration_await_baseline = 1000.0
        func.button_calibration_stage_deadline = None

        result = func._poll_await_release(1_000_000.0, gcmd="callback")

        assert result == pytest.approx(1_000_000.0 + BUTTON_CALIBRATION_POLL_TIME)
        assert func.button_calibration_active is True
        func.afc.error.AFC_error.assert_not_called()

    def test_within_delta_but_not_enough_samples_stays_in_await_release(self) -> None:
        func, _ = _make_calibration_func()
        prev_button = _make_adc_button("lane7")
        prev_button.get_resistance.return_value = 1000.0
        func.button_calibration_active = True
        func.button_calibration_queue = [_make_adc_button("lane8")]
        func.button_calibration_await_button = prev_button
        func.button_calibration_await_baseline = 1000.0

        result = func._poll_await_release(100.0, gcmd="callback")

        assert func.button_calibration_await_button is prev_button
        assert func.button_calibration_samples == [1000.0]
        assert result == pytest.approx(100.0 + BUTTON_CALIBRATION_POLL_TIME)

    def test_within_delta_enough_samples_starts_next_button(self) -> None:
        func, _ = _make_calibration_func()
        prev_button = _make_adc_button("lane7")
        prev_button.get_resistance.return_value = 1000.0
        next_button = _make_adc_button("lane8")
        func.button_calibration_active = True
        func.button_calibration_queue = [next_button]
        func.button_calibration_await_button = prev_button
        func.button_calibration_await_baseline = 1000.0

        for t in range(BUTTON_CALIBRATION_SAMPLE_COUNT):
            func._poll_await_release(100.0 + t, gcmd="callback")

        assert func.button_calibration_await_button is None
        assert func.button_calibration_await_baseline is None
        assert func.button_calibration_stage == "press_1"

    def test_outside_delta_clears_samples(self) -> None:
        func, _ = _make_calibration_func()
        prev_button = _make_adc_button("lane7")
        prev_button.get_resistance.return_value = 1000.0
        func.button_calibration_active = True
        func.button_calibration_queue = [_make_adc_button("lane8")]
        func.button_calibration_await_button = prev_button
        func.button_calibration_await_baseline = 1000.0
        func._poll_await_release(100.0, gcmd="callback")  # one partial sample toward release
        assert func.button_calibration_samples == [1000.0]

        prev_button.get_resistance.return_value = 1000.0 + BUTTON_CALIBRATION_PRESS_DELTA + 1
        func._poll_await_release(101.0, gcmd="callback")

        assert func.button_calibration_samples == []
        assert func.button_calibration_await_button is prev_button

    def test_release_detected_via_ratio_fallback_near_saturation(self) -> None:
        func, _ = _make_calibration_func()
        prev_button = _make_adc_button("lane7")
        prev_button.ratio_at_resistance.side_effect = {
            234995300.0: 0.99998,
            469995300.0: 0.99999,
        }.get
        prev_button.get_resistance.return_value = 234995300.0
        func.button_calibration_active = True
        func.button_calibration_queue = [_make_adc_button("lane8")]
        func.button_calibration_await_button = prev_button
        func.button_calibration_await_baseline = 469995300.0

        for t in range(BUTTON_CALIBRATION_SAMPLE_COUNT):
            func._poll_await_release(100.0 + t, gcmd="callback")

        assert func.button_calibration_await_button is None
        assert func.button_calibration_stage == "press_1"

    def test_log_throttled_on_rapid_successive_polls(self) -> None:
        """A second poll within BUTTON_CALIBRATION_LOG_INTERVAL of the first
        must not log again, proving the throttle actually gates logging
        rather than just being a no-op time check."""
        func, _ = _make_calibration_func()
        prev_button = _make_adc_button("lane7")
        prev_button.get_resistance.return_value = 1000.0
        func.button_calibration_active = True
        func.button_calibration_queue = [_make_adc_button("lane8")]
        func.button_calibration_await_button = prev_button
        func.button_calibration_await_baseline = 1000.0

        func._poll_await_release(100.0, gcmd="callback")
        expected = [("debug", "Button calibration: waiting for lane7 to release, "
                    "resistance=1000.0 ohms (baseline=1000.0, raw_ratio=0.0)")]
        assert func.logger.messages == expected

        func._poll_await_release(100.1, gcmd="callback")

        assert func.logger.messages == expected

    def test_returns_never_when_inactive(self) -> None:
        func, _ = _make_calibration_func()
        prev_button = _make_adc_button("lane7")
        prev_button.get_resistance.return_value = 1000.0
        func.button_calibration_queue = [_make_adc_button("lane8")]
        func.button_calibration_await_button = prev_button
        func.button_calibration_await_baseline = 1000.0
        func.button_calibration_active = False

        assert func._poll_await_release(100.0, gcmd="callback") == func.afc.reactor.NEVER


class TestButtonCalibrationPoll:
    def test_inactive_returns_never(self) -> None:
        func, _ = _make_calibration_func()
        func.button_calibration_active = False
        func.button_calibration_queue = [_make_adc_button("lane7")]

        assert func._button_calibration_poll(100.0) == func.afc.reactor.NEVER

    def test_active_but_empty_queue_returns_never(self) -> None:
        func, _ = _make_calibration_func()
        func.button_calibration_active = True
        func.button_calibration_queue = []

        assert func._button_calibration_poll(100.0) == func.afc.reactor.NEVER

    def test_print_starting_mid_session_cancels_the_session(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_gcmd = _make_gcmd()
        func.printer._objects["print_stats"].get_status.return_value = {
            "state": "printing", "filename": "test.gcode"}

        result = func._button_calibration_poll(100.0)

        assert result == func.afc.reactor.NEVER
        assert func.button_calibration_active is False
        assert func.button_calibration_queue == []
        func.afc.error.AFC_error.assert_called_once()
        assert "print" in func.afc.error.AFC_error.call_args[0][0]

    def test_not_printing_does_not_cancel_the_session(self) -> None:
        """Independent coverage of the in_print() guard: the standby state
        the fixture defaults to must not itself cancel an active session."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_baseline = 1000.0
        button.get_resistance.return_value = 1000.0

        func._button_calibration_poll(100.0)

        assert func.button_calibration_active is True
        func.afc.error.AFC_error.assert_not_called()

    def test_await_release_stage_delegates_to_poll_await_release(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "await_release"
        with patch.object(func, "_poll_await_release", return_value=123.0) as mock_poll:
            result = func._button_calibration_poll(100.0)
        mock_poll.assert_called_once_with(100.0, gcmd="callback")
        assert result == 123.0

    def test_resistance_none_reschedules(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7", resistance_sequence=[None])
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_baseline = 1000.0

        result = func._button_calibration_poll(100.0)

        assert result == pytest.approx(100.0 + BUTTON_CALIBRATION_POLL_TIME)

    def test_resistance_none_past_adc_timeout_warns(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7", resistance_sequence=[None])
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_baseline = 1000.0
        func.button_calibration_stage_started = 0.0

        func._button_calibration_poll(BUTTON_CALIBRATION_ADC_SAMPLE_TIMEOUT)

        func.afc.error.AFC_error.assert_called_once_with(
            "Button calibration: no ADC reading received for lane7 (pin PA0) after 5s; "
            "still waiting.", pause=False, gcmd="callback")

    def test_past_stage_deadline_times_out_the_session(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_baseline = 1000.0
        func.button_calibration_stage_deadline = 100.0

        result = func._button_calibration_poll(100.0)

        assert result == func.afc.reactor.NEVER
        assert func.button_calibration_active is False
        func.afc.error.AFC_error.assert_called_once()

    def test_before_stage_deadline_does_not_time_out(self) -> None:
        """Independent coverage of the stage-deadline operand: being set
        isn't enough by itself, the current eventtime must have reached it."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7", resistance_sequence=[None])
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_baseline = 1000.0
        func.button_calibration_stage_deadline = 200.0

        result = func._button_calibration_poll(100.0)

        assert result == pytest.approx(100.0 + BUTTON_CALIBRATION_POLL_TIME)
        assert func.button_calibration_active is True
        func.afc.error.AFC_error.assert_not_called()

    def test_no_stage_deadline_does_not_time_out_regardless_of_eventtime(self) -> None:
        """Independent coverage of the `is not None` operand: no deadline at
        all must not time out even at an eventtime that would otherwise be
        past any real deadline."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7", resistance_sequence=[None])
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_baseline = 1000.0
        func.button_calibration_stage_deadline = None

        result = func._button_calibration_poll(1_000_000.0)

        assert result == pytest.approx(1_000_000.0 + BUTTON_CALIBRATION_POLL_TIME)
        assert func.button_calibration_active is True
        func.afc.error.AFC_error.assert_not_called()

    def test_log_throttled_on_rapid_successive_polls(self) -> None:
        """A second poll within BUTTON_CALIBRATION_LOG_INTERVAL of the first
        must not log again, proving the throttle actually gates logging
        rather than just being a no-op time check."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_baseline = 1000.0

        func._button_calibration_poll(100.0)
        expected = [("debug", "Button calibration: lane7 resistance=1000.0 ohms "
                    "(baseline=1000.0, stage=press_1, raw_ratio=0.0)")]
        assert func.logger.messages == expected

        func._button_calibration_poll(100.1)

        assert func.logger.messages == expected

    def test_baseline_none_collects_partial_sample_without_evaluating_delta(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_baseline = None

        func._button_calibration_poll(100.0)

        assert func.button_calibration_baseline is None
        assert func.button_calibration_samples == [1000.0]

    def test_baseline_established_and_logged_once_averaged(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_baseline = None

        for t in range(BUTTON_CALIBRATION_SAMPLE_COUNT):
            func._button_calibration_poll(100.0 + t)

        assert func.button_calibration_baseline == pytest.approx(1000.0)
        debug_line = ("debug",
                      "Button calibration: lane7 resistance=1000.0 ohms "
                      "(baseline=None, stage=press_1, raw_ratio=0.0)")
        assert func.logger.messages == [
            debug_line, debug_line, debug_line, debug_line, debug_line,
            ("info", "Button calibration: baseline resistance for lane7 is 1000.0 ohms"),
        ]

    def _set_press1(self, func: afcFunction, button: Any, baseline: float=1000.0) -> None:
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_1"
        func.button_calibration_baseline = baseline

    # press_1 stage

    def test_press_1_within_delta_clears_samples(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0
        self._set_press1(func, button)
        func.button_calibration_samples = [999.0]

        func._button_calibration_poll(100.0)

        assert func.button_calibration_samples == []
        assert func.button_calibration_stage == "press_1"

    def test_press_1_outside_delta_not_enough_samples_stays_in_press_1(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0 + BUTTON_CALIBRATION_PRESS_DELTA + 1
        self._set_press1(func, button)

        func._button_calibration_poll(100.0)

        assert func.button_calibration_stage == "press_1"
        assert len(func.button_calibration_samples) == 1

    def test_press_1_outside_delta_enough_samples_transitions_to_release(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        resistance = 1000.0 + BUTTON_CALIBRATION_PRESS_DELTA + 1
        button.get_resistance.return_value = resistance
        self._set_press1(func, button)

        for t in range(BUTTON_CALIBRATION_SAMPLE_COUNT):
            func._button_calibration_poll(100.0 + t)

        assert func.button_calibration_stage == "release"
        assert func.button_calibration_first_value == pytest.approx(resistance)
        debug_line = ("debug",
                      f"Button calibration: lane7 resistance={resistance:.1f} ohms "
                      "(baseline=1000.0, stage=press_1, raw_ratio=0.0)")
        assert func.logger.messages == [
            debug_line, debug_line, debug_line, debug_line, debug_line,
            ("raw", "// action:prompt_begin Button Calibration"),
            ("raw", "// action:prompt_text Release the lane7 button, then press it again."),
            ("raw", "// action:prompt_footer_button Cancel|BUTTON_CALIBRATION "
             "CANCEL=1|warning"),
            ("raw", "// action:prompt_show"),
        ]

    def test_press_1_ratio_fallback_treats_near_saturation_drift_as_released(self) -> None:
        """A baseline drifting from one near-saturated ratio to another can swing by more than
        BUTTON_CALIBRATION_PRESS_DELTA ohms with no real press; press detection must fall back to
        the same ratio-space tolerance _is_released uses, not just the raw ohms delta, or this
        drift would be wrongly accepted as a press."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.ratio_at_resistance.side_effect = {
            234995300.0: 0.99998,
            469995300.0: 0.99999,
        }.get
        button.get_resistance.return_value = 234995300.0
        self._set_press1(func, button, baseline=469995300.0)

        for t in range(BUTTON_CALIBRATION_SAMPLE_COUNT):
            func._button_calibration_poll(100.0 + t)

        assert func.button_calibration_stage == "press_1"
        assert func.button_calibration_samples == []

    # release stage

    def test_release_outside_delta_clears_samples(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0 + BUTTON_CALIBRATION_PRESS_DELTA + 1
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "release"
        func.button_calibration_baseline = 1000.0
        func.button_calibration_samples = [1.0]

        func._button_calibration_poll(100.0)

        assert func.button_calibration_samples == []
        assert func.button_calibration_stage == "release"

    def test_release_within_delta_not_enough_samples_stays_in_release(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "release"
        func.button_calibration_baseline = 1000.0

        func._button_calibration_poll(100.0)

        assert func.button_calibration_stage == "release"
        assert len(func.button_calibration_samples) == 1

    def test_release_within_delta_enough_samples_transitions_to_press_2(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "release"
        func.button_calibration_baseline = 1000.0

        for t in range(BUTTON_CALIBRATION_SAMPLE_COUNT):
            func._button_calibration_poll(100.0 + t)

        assert func.button_calibration_stage == "press_2"

    def test_release_detected_via_ratio_fallback_near_saturation(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.ratio_at_resistance.side_effect = {
            234995300.0: 0.99998,
            469995300.0: 0.99999,
        }.get
        button.get_resistance.return_value = 234995300.0
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "release"
        func.button_calibration_baseline = 469995300.0

        for t in range(BUTTON_CALIBRATION_SAMPLE_COUNT):
            func._button_calibration_poll(100.0 + t)

        assert func.button_calibration_stage == "press_2"

    # press_2 stage

    def test_press_2_within_delta_clears_samples(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_2"
        func.button_calibration_baseline = 1000.0
        func.button_calibration_samples = [999.0]

        func._button_calibration_poll(100.0)

        assert func.button_calibration_samples == []
        assert func.button_calibration_stage == "press_2"

    def test_press_2_outside_delta_not_enough_samples_stays_in_press_2(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0 + BUTTON_CALIBRATION_PRESS_DELTA + 1
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_2"
        func.button_calibration_baseline = 1000.0

        func._button_calibration_poll(100.0)

        assert func.button_calibration_stage == "press_2"
        assert len(func.button_calibration_samples) == 1

    def test_press_2_outside_delta_enough_samples_finishes_measurement(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0 + BUTTON_CALIBRATION_PRESS_DELTA + 1
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_2"
        func.button_calibration_baseline = 1000.0
        func.button_calibration_first_value = 1000.0 + BUTTON_CALIBRATION_PRESS_DELTA + 1

        for t in range(BUTTON_CALIBRATION_SAMPLE_COUNT):
            func._button_calibration_poll(100.0 + t)

        func.ConfigRewrite.assert_called_once()
        assert button not in func.button_calibration_queue

    def test_returns_never_when_queue_emptied_during_this_tick(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0 + BUTTON_CALIBRATION_PRESS_DELTA + 1
        func.button_calibration_active = True
        func.button_calibration_queue = [button]
        func.button_calibration_stage = "press_2"
        func.button_calibration_baseline = 1000.0
        func.button_calibration_first_value = 1000.0 + BUTTON_CALIBRATION_PRESS_DELTA + 1

        result = 0.0
        for t in range(BUTTON_CALIBRATION_SAMPLE_COUNT):
            result = func._button_calibration_poll(100.0 + t)

        assert result == func.afc.reactor.NEVER

    def test_returns_next_poll_time_when_still_active(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0
        self._set_press1(func, button)

        result = func._button_calibration_poll(100.0)

        assert result == pytest.approx(100.0 + BUTTON_CALIBRATION_POLL_TIME)

    def test_unrecognized_stage_leaves_state_untouched(self) -> None:
        """A baseline already set but a stage outside press_1/release/press_2
        (shouldn't happen in practice) falls through the whole if/elif chain
        without matching any branch, rather than crashing or guessing."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.get_resistance.return_value = 1000.0
        self._set_press1(func, button)
        func.button_calibration_stage = "unexpected_stage"

        result = func._button_calibration_poll(100.0)

        assert func.button_calibration_stage == "unexpected_stage"
        assert func.button_calibration_samples == []
        assert result == pytest.approx(100.0 + BUTTON_CALIBRATION_POLL_TIME)


class TestRangeOverlapsOtherButton:
    def test_no_other_buttons_returns_none(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.printer._objects["AFC_button lane7"] = button

        result = func._range_overlaps_other_button(button, 50000.0, 100000.0)

        assert result is None

    def test_excludes_the_button_itself(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        button.analog_min = 50000.0
        button.analog_max = 100000.0
        func.printer._objects["AFC_button lane7"] = button

        result = func._range_overlaps_other_button(button, 50000.0, 100000.0)

        assert result is None

    def test_non_overlapping_other_button_above_returns_none(self) -> None:
        """Candidate range entirely below other's: `min_value <= other.analog_max`
        is False on its own, independent of `max_value >= other.analog_min`."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        other = _make_adc_button("lane8")
        other.analog_min = 0.0
        other.analog_max = 1000.0
        func.printer._objects["AFC_button lane7"] = button
        func.printer._objects["AFC_button lane8"] = other

        result = func._range_overlaps_other_button(button, 50000.0, 100000.0)

        assert result is None

    def test_non_overlapping_other_button_below_returns_none(self) -> None:
        """Candidate range entirely above other's: `max_value >= other.analog_min`
        is False on its own, independent of `min_value <= other.analog_max`."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        other = _make_adc_button("lane8")
        other.analog_min = 1000.0
        other.analog_max = 2000.0
        func.printer._objects["AFC_button lane7"] = button
        func.printer._objects["AFC_button lane8"] = other

        result = func._range_overlaps_other_button(button, 0.0, 500.0)

        assert result is None

    def test_overlapping_other_button_is_returned(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        other = _make_adc_button("lane8")
        other.analog_min = 60000.0
        other.analog_max = 70000.0
        func.printer._objects["AFC_button lane7"] = button
        func.printer._objects["AFC_button lane8"] = other

        result = func._range_overlaps_other_button(button, 50000.0, 100000.0)

        assert result is other

    def test_overlapping_other_button_on_a_different_pin_returns_none(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7", pin="PA0")
        other = _make_adc_button("lane8", pin="PA1")
        other.analog_min = 60000.0
        other.analog_max = 70000.0
        func.printer._objects["AFC_button lane7"] = button
        func.printer._objects["AFC_button lane8"] = other

        result = func._range_overlaps_other_button(button, 50000.0, 100000.0)

        assert result is None

    def test_pin_comparison_strips_whitespace(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7", pin=" PA0")
        other = _make_adc_button("lane8", pin="PA0 ")
        other.analog_min = 60000.0
        other.analog_max = 70000.0
        func.printer._objects["AFC_button lane7"] = button
        func.printer._objects["AFC_button lane8"] = other

        result = func._range_overlaps_other_button(button, 50000.0, 100000.0)

        assert result is other


class TestAdvanceButtonCalibrationQueue:
    def test_more_buttons_in_queue_enters_await_release(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        next_button = _make_adc_button("lane8")
        func.button_calibration_queue = [button, next_button]
        func.button_calibration_baseline = 1000.0
        func.button_calibration_gcmd = _make_gcmd()
        func.button_calibration_active = True

        func._advance_button_calibration_queue(button)

        assert func.button_calibration_queue == [next_button]
        assert func.button_calibration_stage == "await_release"
        assert func.button_calibration_await_button is button
        assert func.button_calibration_await_baseline == 1000.0
        assert func.button_calibration_baseline is None
        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Button Calibration"),
            ("raw", "// action:prompt_text Release the lane7 button."),
            ("raw", "// action:prompt_footer_button Cancel|BUTTON_CALIBRATION "
             "CANCEL=1|warning"),
            ("raw", "// action:prompt_show"),
        ]

    def test_last_button_finishes_session_with_restart_pending(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_baseline = 1000.0
        func.button_calibration_active = True
        func.button_calibration_gcmd = _make_gcmd()
        func.button_calibration_saved = ["lane7"]

        func._advance_button_calibration_queue(button)

        assert func.button_calibration_queue == []
        assert func.button_calibration_active is False
        assert func.button_calibration_stage == ""
        assert func.button_calibration_baseline is None
        assert func.button_calibration_restart_pending is True
        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Button Calibration Complete"),
            ("raw", "// action:prompt_text Calibrated: lane7."),
            ("raw", "// action:prompt_text "),
            ("raw", "// action:prompt_text The new analog_range values only take effect "
             "after a Klipper restart. Click Restart button to restart now or Done and "
             "restart later."),
            ("raw", "// action:prompt_footer_button Restart Klipper|"
             "BUTTON_CALIBRATION_RESTART|primary"),
            ("raw", "// action:prompt_footer_button Done|RESPOND TYPE=command "
             "MSG=action:prompt_end|info"),
            ("raw", "// action:prompt_show"),
        ]

    def test_finishes_with_nothing_saved_skips_restart_footer(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_baseline = 1000.0
        func.button_calibration_active = True
        func.button_calibration_gcmd = _make_gcmd()
        func.button_calibration_skipped = ["lane7"]

        func._advance_button_calibration_queue(button)

        assert func.button_calibration_restart_pending is False
        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Button Calibration Complete"),
            ("raw", "// action:prompt_text No buttons were calibrated."),
            ("raw", "// action:prompt_text "),
            ("raw", "// action:prompt_text Skipped (not changed): lane7."),
            ("raw", "// action:prompt_footer_button Done|RESPOND TYPE=command "
             "MSG=action:prompt_end|info"),
            ("raw", "// action:prompt_show"),
        ]

    def test_finishes_with_both_saved_and_skipped_buttons_reports_both(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane8")
        func.button_calibration_queue = [button]
        func.button_calibration_baseline = 1000.0
        func.button_calibration_active = True
        func.button_calibration_gcmd = _make_gcmd()
        func.button_calibration_saved = ["lane7"]
        func.button_calibration_skipped = ["lane8"]

        func._advance_button_calibration_queue(button)

        assert func.button_calibration_restart_pending is True
        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Button Calibration Complete"),
            ("raw", "// action:prompt_text Calibrated: lane7."),
            ("raw", "// action:prompt_text "),
            ("raw", "// action:prompt_text Skipped (not changed): lane8."),
            ("raw", "// action:prompt_text "),
            ("raw", "// action:prompt_text The new analog_range values only take effect "
             "after a Klipper restart. Click Restart button to restart now or Done and "
             "restart later."),
            ("raw", "// action:prompt_footer_button Restart Klipper|"
             "BUTTON_CALIBRATION_RESTART|primary"),
            ("raw", "// action:prompt_footer_button Done|RESPOND TYPE=command "
             "MSG=action:prompt_end|info"),
            ("raw", "// action:prompt_show"),
        ]


class TestFinishButtonMeasurement:
    def test_averages_first_and_second_values_and_saves_config(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 2500.0
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, 2600.0, gcmd="callback")

        average = (2500.0 + 2600.0) / 2.0
        expected_min = average - BUTTON_CALIBRATION_RANGE
        expected_max = average + BUTTON_CALIBRATION_RANGE
        func.ConfigRewrite.assert_called_once()
        call_args = func.ConfigRewrite.call_args[0]
        assert call_args[0] == "AFC_button lane7"
        assert call_args[1] == "analog_range"
        assert call_args[2] == f"{expected_min:.1f},{expected_max:.1f}"

    def test_uses_second_value_when_first_value_missing(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = None
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, 2600.0, gcmd="callback")

        expected_min = 2600.0 - BUTTON_CALIBRATION_RANGE
        expected_max = 2600.0 + BUTTON_CALIBRATION_RANGE
        call_args = func.ConfigRewrite.call_args[0]
        assert call_args[2] == f"{expected_min:.1f},{expected_max:.1f}"

    def test_sets_button_analog_min_max(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 2500.0
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, 2600.0, gcmd="callback")

        average = (2500.0 + 2600.0) / 2.0
        assert button.analog_min == pytest.approx(average - BUTTON_CALIBRATION_RANGE)
        assert button.analog_max == pytest.approx(average + BUTTON_CALIBRATION_RANGE)

    def test_disagreeing_presses_reject_the_save(self) -> None:
        """The two press readings themselves agree with each other within
        tolerance's complement: this drives agreement_delta over the limit
        while average_delta on its own would have passed, isolating the
        agreement-check operand."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 2000.0
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, 2000.0 + BUTTON_CALIBRATION_AGREEMENT_TOLERANCE + 1, gcmd="callback")

        func.ConfigRewrite.assert_not_called()
        func.afc.error.AFC_error.assert_called_once()
        error_msg = func.afc.error.AFC_error.call_args[0][0]
        assert "lane7" in error_msg
        assert "don't agree" in error_msg
        assert func.button_calibration_skipped == ["lane7"]
        assert func.button_calibration_saved == []

    def test_agreeing_presses_too_close_to_baseline_reject_the_save(self) -> None:
        """The two press readings agree with each other, isolating the
        average-vs-baseline operand: agreement_delta is well within
        tolerance, but the average sits inside BUTTON_CALIBRATION_PRESS_DELTA
        of the baseline, so there's nothing to show it was actually
        pressed."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 1000.0
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, 1000.0, gcmd="callback")

        func.ConfigRewrite.assert_not_called()
        func.afc.error.AFC_error.assert_called_once()
        error_msg = func.afc.error.AFC_error.call_args[0][0]
        assert "lane7" in error_msg
        assert "don't agree" in error_msg
        assert func.button_calibration_skipped == ["lane7"]
        assert func.button_calibration_saved == []

    def test_rejected_save_still_advances_the_queue(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 1000.0
        func.button_calibration_baseline = 1000.0
        func.button_calibration_active = True

        func._finish_button_measurement(button, 1000.0, gcmd="callback")

        assert func.button_calibration_queue == []
        assert func.button_calibration_skipped == ["lane7"]
        # Nothing was actually saved, so there's nothing to restart for.
        assert func.button_calibration_restart_pending is False

    def test_agreeing_and_far_enough_presses_save_normally(self) -> None:
        """Both operands satisfied together: agreement_delta and
        average_delta each pass, so the save proceeds."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 2500.0
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, 2600.0, gcmd="callback")

        func.ConfigRewrite.assert_called_once()
        func.afc.error.AFC_error.assert_not_called()
        assert func.button_calibration_saved == ["lane7"]
        assert func.button_calibration_skipped == []

    def test_no_baseline_skips_the_agreement_check(self) -> None:
        """Independent coverage of the `finished_baseline is not None` guard:
        with no baseline to compare against, disagreeing presses that would
        otherwise be rejected must still save."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 2000.0
        func.button_calibration_baseline = None

        func._finish_button_measurement(button, 2000.0 + BUTTON_CALIBRATION_AGREEMENT_TOLERANCE + 1, gcmd="callback")

        func.ConfigRewrite.assert_called_once()
        func.afc.error.AFC_error.assert_not_called()
        assert func.button_calibration_saved == ["lane7"]
        assert func.button_calibration_skipped == []

    def test_normal_range_overlapping_another_button_skips_save(self) -> None:
        """The non-saturated branch's derived [average-RANGE, average+RANGE]
        must be checked for an overlap too, not just the saturated branch's
        derived range."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        other = _make_adc_button("lane8")
        other.analog_min = 2550.0
        other.analog_max = 2650.0
        func.printer._objects["AFC_button lane7"] = button
        func.printer._objects["AFC_button lane8"] = other
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 2500.0
        func.button_calibration_baseline = 1000.0
        original_min, original_max = button.analog_min, button.analog_max

        func._finish_button_measurement(button, 2600.0, gcmd="callback")

        func.ConfigRewrite.assert_not_called()
        assert button.analog_min == original_min
        assert button.analog_max == original_max
        func.afc.error.AFC_error.assert_called_once()
        error_msg = func.afc.error.AFC_error.call_args[0][0]
        assert "lane7" in error_msg
        assert "lane8" in error_msg
        assert func.button_calibration_skipped == ["lane7"]
        assert func.button_calibration_saved == []

    def test_normal_range_not_overlapping_another_button_saves_normally(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        other = _make_adc_button("lane8")
        other.analog_min = 0.0
        other.analog_max = 100.0
        func.printer._objects["AFC_button lane7"] = button
        func.printer._objects["AFC_button lane8"] = other
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 2500.0
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, 2600.0, gcmd="callback")

        func.ConfigRewrite.assert_called_once()
        func.afc.error.AFC_error.assert_not_called()
        assert func.button_calibration_saved == ["lane7"]
        assert func.button_calibration_skipped == []

    def test_only_first_reading_saturated_rejects_via_ohms_agreement(self) -> None:
        """Independent coverage of the `and` in both_saturated: the first reading alone being
        saturated must not satisfy it. These ratios (0.9804 saturated, 0.9798 not) straddle the
        0.98 threshold and agree within the ratio-space tolerance (diff=0.0006 <= 0.001), so an
        incorrect `or` would treat both_saturated as True and wave this through via the lenient
        ratio check; only the correct `and` routes it through the strict ohms-based agreement
        check (resistances differ by ~7100 ohms, far past 2*calibration_range), which rejects it."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = button.resistance_at_ratio(0.9804)
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, button.resistance_at_ratio(0.9798), gcmd="callback")

        func.ConfigRewrite.assert_not_called()
        func.afc.error.AFC_error.assert_called_once()
        error_msg = func.afc.error.AFC_error.call_args[0][0]
        assert "lane7" in error_msg
        assert "don't agree" in error_msg
        assert func.button_calibration_skipped == ["lane7"]

    def test_only_second_reading_saturated_rejects_via_ohms_agreement(self) -> None:
        """Independent coverage of the `and` in both_saturated: the second reading alone being
        saturated must not satisfy it either. Same ratios as the first-reading-saturated case,
        in the opposite order."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = button.resistance_at_ratio(0.9798)
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, button.resistance_at_ratio(0.9804), gcmd="callback")

        func.ConfigRewrite.assert_not_called()
        func.afc.error.AFC_error.assert_called_once()
        error_msg = func.afc.error.AFC_error.call_args[0][0]
        assert "lane7" in error_msg
        assert "don't agree" in error_msg
        assert func.button_calibration_skipped == ["lane7"]

    def test_both_readings_saturated_but_disagreeing_in_ratio_rejects_the_save(self) -> None:
        """With both readings genuinely saturated, the ratio-space agreement check itself (not
        the ohms check) must still reject a real disagreement (diff=0.005, over the 0.001
        tolerance)."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = button.resistance_at_ratio(0.99)
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, button.resistance_at_ratio(0.995), gcmd="callback")

        func.ConfigRewrite.assert_not_called()
        func.afc.error.AFC_error.assert_called_once()
        error_msg = func.afc.error.AFC_error.call_args[0][0]
        assert "lane7" in error_msg
        assert "don't agree" in error_msg
        assert func.button_calibration_skipped == ["lane7"]

    def test_normal_range_containing_the_released_baseline_rejects_save(self) -> None:
        """With calibration_range wide enough to span back to the baseline, a real
        press reading can still land Klipper's analog_range on top of the idle
        reading, which would read the button as pressed while released."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.printer._objects["AFC_button lane7"] = button
        button.calibration_range = 1500.0
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 2100.0
        func.button_calibration_baseline = 1000.0
        original_min, original_max = button.analog_min, button.analog_max

        func._finish_button_measurement(button, 2100.0, gcmd="callback")

        func.ConfigRewrite.assert_not_called()
        assert button.analog_min == original_min
        assert button.analog_max == original_max
        func.afc.error.AFC_error.assert_called_once()
        error_msg = func.afc.error.AFC_error.call_args[0][0]
        assert "lane7" in error_msg
        assert "released baseline" in error_msg
        assert func.button_calibration_skipped == ["lane7"]
        assert func.button_calibration_saved == []

    def test_normal_range_excluding_the_released_baseline_saves_normally(self) -> None:
        """Independent coverage of the baseline-containment check: a range
        that does NOT reach back to the baseline must still save."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.printer._objects["AFC_button lane7"] = button
        button.calibration_range = 1500.0
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 4000.0
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, 4000.0, gcmd="callback")

        func.ConfigRewrite.assert_called_once()
        func.afc.error.AFC_error.assert_not_called()
        assert func.button_calibration_saved == ["lane7"]

    def test_no_baseline_skips_the_containment_check(self) -> None:
        """Independent coverage of the `finished_baseline is not None` guard
        on the containment check: with no baseline at all, a range that would
        have been rejected for containing it must still save."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.printer._objects["AFC_button lane7"] = button
        button.calibration_range = 1500.0
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 2100.0
        func.button_calibration_baseline = None

        func._finish_button_measurement(button, 2100.0, gcmd="callback")

        func.ConfigRewrite.assert_called_once()
        func.afc.error.AFC_error.assert_not_called()
        assert func.button_calibration_saved == ["lane7"]

    def test_debug_logs_unknown_baseline_when_none(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 900.0
        func.button_calibration_baseline = None

        func._finish_button_measurement(button, 1100.0, gcmd="callback")

        assert func.logger.messages == [
            ("debug", "Button calibration: lane7 baseline=unknown ohms, press1=900.0 ohms, "
             "press2=1100.0 ohms, average=1000.0 ohms, raw_ratio=0.0"),
            ("raw", "// action:prompt_begin Button Calibration Complete"),
            ("raw", "// action:prompt_text Calibrated: lane7."),
            ("raw", "// action:prompt_text "),
            ("raw", "// action:prompt_text The new analog_range values only take effect "
             "after a Klipper restart. Click Restart button to restart now or Done and "
             "restart later."),
            ("raw", "// action:prompt_footer_button Restart Klipper|"
             "BUTTON_CALIBRATION_RESTART|primary"),
            ("raw", "// action:prompt_footer_button Done|RESPOND TYPE=command "
             "MSG=action:prompt_end|info"),
            ("raw", "// action:prompt_show"),
        ]

    def test_debug_logs_baseline_value_when_known(self) -> None:
        """average==baseline here, so this measurement is rejected (too close
        to baseline) rather than saved; this test only verifies the debug log
        line, not the save outcome."""
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 900.0
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, 1100.0, gcmd="callback")

        assert func.logger.messages[0] == (
            "debug", "Button calibration: lane7 baseline=1000.0 ohms, press1=900.0 ohms, "
            "press2=1100.0 ohms, average=1000.0 ohms, raw_ratio=0.0")
        func.ConfigRewrite.assert_not_called()

    def test_pops_queue_and_clears_first_value(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 900.0
        func.button_calibration_baseline = 1000.0

        func._finish_button_measurement(button, 1100.0, gcmd="callback")

        assert func.button_calibration_queue == []
        assert func.button_calibration_first_value is None

    def test_more_buttons_in_queue_enters_await_release(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        next_button = _make_adc_button("lane8")
        func.button_calibration_queue = [button, next_button]
        func.button_calibration_first_value = 900.0
        func.button_calibration_baseline = 1000.0
        func.button_calibration_gcmd = _make_gcmd()
        func.button_calibration_active = True

        func._finish_button_measurement(button, 1100.0, gcmd="callback")

        assert func.button_calibration_stage == "await_release"
        assert func.button_calibration_await_button is button
        assert func.button_calibration_await_baseline == 1000.0
        assert func.button_calibration_baseline is None
        assert func.button_calibration_active is True  # session still running
        debug_line = ("debug",
                      "Button calibration: lane7 baseline=1000.0 ohms, press1=900.0 ohms, "
                      "press2=1100.0 ohms, average=1000.0 ohms, raw_ratio=0.0")
        assert func.logger.messages == [
            debug_line,
            ("raw", "// action:prompt_begin Button Calibration"),
            ("raw", "// action:prompt_text Release the lane7 button."),
            ("raw", "// action:prompt_footer_button Cancel|BUTTON_CALIBRATION "
             "CANCEL=1|warning"),
            ("raw", "// action:prompt_show"),
        ]

    def test_last_button_finishes_session_with_restart_pending(self) -> None:
        func, _ = _make_calibration_func()
        button = _make_adc_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 2500.0
        func.button_calibration_baseline = 1000.0
        func.button_calibration_active = True
        func.button_calibration_gcmd = _make_gcmd()

        func._finish_button_measurement(button, 2600.0, gcmd="callback")

        average = (2500.0 + 2600.0) / 2.0
        assert func.button_calibration_active is False
        assert func.button_calibration_stage == ""
        assert func.button_calibration_baseline is None
        assert func.button_calibration_restart_pending is True
        assert func.logger.messages == [
            ("debug", "Button calibration: lane7 baseline=1000.0 ohms, press1=2500.0 ohms, "
             f"press2=2600.0 ohms, average={average:.1f} ohms, raw_ratio=0.0"),
            ("raw", "// action:prompt_begin Button Calibration Complete"),
            ("raw", "// action:prompt_text Calibrated: lane7."),
            ("raw", "// action:prompt_text "),
            ("raw", "// action:prompt_text The new analog_range values only take effect "
             "after a Klipper restart. Click Restart button to restart now or Done and "
             "restart later."),
            ("raw", "// action:prompt_footer_button Restart Klipper|"
             "BUTTON_CALIBRATION_RESTART|primary"),
            ("raw", "// action:prompt_footer_button Done|RESPOND TYPE=command "
             "MSG=action:prompt_end|info"),
            ("raw", "// action:prompt_show"),
        ]

    def _make_saturated_button(self, name: str) -> MagicMock:
        """ADC button stand-in whose resistance_at_ratio() returns distinct,
        easy-to-assert-on sentinel values for the two ratios
        _finish_button_measurement's saturated branch is expected to use."""
        button = _make_adc_button(name)
        button.is_adc_saturated.return_value = True
        button.resistance_at_ratio = MagicMock(side_effect=lambda ratio: {
            ADC_SATURATION_RATIO: 50000.0,
            BUTTON_CALIBRATION_SATURATION_MAX_RATIO: 9999900.0,
        }[ratio])
        return button

    def test_saturated_reading_derives_min_from_saturation_min_ratio(self) -> None:
        func, _ = _make_calibration_func()
        button = self._make_saturated_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 9500000.0
        func.button_calibration_baseline = 4.6

        func._finish_button_measurement(button, 9500050.0, gcmd="callback")

        button.resistance_at_ratio.assert_any_call(ADC_SATURATION_RATIO)
        assert button.analog_min == pytest.approx(50000.0)

    def test_saturated_reading_derives_max_from_saturation_max_ratio_plus_range(self) -> None:
        func, _ = _make_calibration_func()
        button = self._make_saturated_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 9500000.0
        func.button_calibration_baseline = 4.6

        func._finish_button_measurement(button, 9500050.0, gcmd="callback")

        button.resistance_at_ratio.assert_any_call(BUTTON_CALIBRATION_SATURATION_MAX_RATIO)
        expected_max = 9999900.0 + BUTTON_CALIBRATION_RANGE
        assert button.analog_max == pytest.approx(expected_max)

    def test_saturated_reading_saves_derived_range_to_config(self) -> None:
        func, _ = _make_calibration_func()
        button = self._make_saturated_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 9500000.0
        func.button_calibration_baseline = 4.6

        func._finish_button_measurement(button, 9500050.0, gcmd="callback")

        expected_max = 9999900.0 + BUTTON_CALIBRATION_RANGE
        call_args = func.ConfigRewrite.call_args[0]
        assert call_args[2] == f"{50000.0:.1f},{expected_max:.1f}"

    def test_saturated_reading_logs_info_message(self) -> None:
        func, _ = _make_calibration_func()
        button = self._make_saturated_button("lane7")
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 9500000.0
        func.button_calibration_baseline = 4.6

        func._finish_button_measurement(button, 9500050.0, gcmd="callback")

        assert func.logger.messages == [
            ("debug", "Button calibration: lane7 baseline=4.6 ohms, press1=9500000.0 ohms, "
             "press2=9500050.0 ohms, average=9500025.0 ohms, raw_ratio=0.0"),
            ("debug", "Button calibration: lane7 press reading is maxed out "
             "(average=9500025.0 ohms); using a range derived from the ADC ratio instead."),
            ("raw", "// action:prompt_begin Button Calibration Complete"),
            ("raw", "// action:prompt_text Calibrated: lane7."),
            ("raw", "// action:prompt_text "),
            ("raw", "// action:prompt_text The new analog_range values only take effect "
             "after a Klipper restart. Click Restart button to restart now or Done and "
             "restart later."),
            ("raw", "// action:prompt_footer_button Restart Klipper|"
             "BUTTON_CALIBRATION_RESTART|primary"),
            ("raw", "// action:prompt_footer_button Done|RESPOND TYPE=command "
             "MSG=action:prompt_end|info"),
            ("raw", "// action:prompt_show"),
        ]

    def test_overlapping_saturated_range_skips_save_and_reports_conflict(self) -> None:
        func, _ = _make_calibration_func()
        button = self._make_saturated_button("lane7")
        other = _make_adc_button("lane8")
        other.analog_min = 60000.0
        other.analog_max = 70000.0
        func.printer._objects["AFC_button lane7"] = button
        func.printer._objects["AFC_button lane8"] = other
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 9500000.0
        func.button_calibration_baseline = 4.6
        original_min, original_max = button.analog_min, button.analog_max

        func._finish_button_measurement(button, 9500050.0, gcmd="callback")

        func.ConfigRewrite.assert_not_called()
        assert button.analog_min == original_min
        assert button.analog_max == original_max
        func.afc.error.AFC_error.assert_called_once()
        error_msg = func.afc.error.AFC_error.call_args[0][0]
        assert "lane7" in error_msg
        assert "lane8" in error_msg

    def test_overlapping_saturated_range_still_advances_the_queue(self) -> None:
        func, _ = _make_calibration_func()
        button = self._make_saturated_button("lane7")
        other = _make_adc_button("lane8")
        other.analog_min = 60000.0
        other.analog_max = 70000.0
        func.printer._objects["AFC_button lane7"] = button
        func.printer._objects["AFC_button lane8"] = other
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 9500000.0
        func.button_calibration_baseline = 4.6
        func.button_calibration_active = True

        func._finish_button_measurement(button, 9500050.0, gcmd="callback")

        assert func.button_calibration_queue == []
        assert func.button_calibration_active is False
        # The overlap conflict rejected the save, so there's nothing to restart for.
        assert func.button_calibration_restart_pending is False
        assert func.button_calibration_skipped == ["lane7"]

    def test_non_overlapping_saturated_range_still_saves_normally(self) -> None:
        func, _ = _make_calibration_func()
        button = self._make_saturated_button("lane7")
        other = _make_adc_button("lane8")
        other.analog_min = 0.0
        other.analog_max = 1000.0
        func.printer._objects["AFC_button lane7"] = button
        func.printer._objects["AFC_button lane8"] = other
        func.button_calibration_queue = [button]
        func.button_calibration_first_value = 9500000.0
        func.button_calibration_baseline = 4.6

        func._finish_button_measurement(button, 9500050.0, gcmd="callback")

        func.ConfigRewrite.assert_called_once()
        func.afc.error.AFC_error.assert_not_called()
        assert button.analog_min == pytest.approx(50000.0)


class TestCmdAfcToolheadCalibration:
    def test_toolhead_menu_includes_both_stn_values(self) -> None:
        func, _ = _make_calibration_func()
        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd())
        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("tool_stn|AFC_TOOL_STN_CALIBRATION" in message for message in raw_messages)
        assert any("tool_stn_unload|AFC_TOOL_STN_UNLOAD_CALIBRATION" in message
                   for message in raw_messages)
        assert any("AFC_CUTTER_CALIBRATION EXTRUDER=extruder" in message
                   for message in raw_messages)
        assert any("LANE=lane1" in message for message in raw_messages)

    def test_toolhead_menu_prompts_for_lane_when_multiple_are_available(self) -> None:
        func, extruder = _make_calibration_func()
        func.afc.current = None
        second_lane = MagicMock(name="lane2")
        second_lane.name = "lane2"
        second_lane.extruder_obj = extruder
        func.afc.lanes["lane2"] = second_lane
        extruder.lanes["lane2"] = second_lane

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder"))

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("representative lane" in message for message in raw_messages)
        assert any("not for every lane" in message for message in raw_messages)
        assert any("LANE=lane1" in message for message in raw_messages)
        assert any("LANE=lane2" in message for message in raw_messages)
        assert any("|primary" in message for message in raw_messages)
        assert any("|secondary" in message for message in raw_messages)

    def test_toolhead_menu_resolves_only_lane_when_toolhead_is_empty(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder"))

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("AFC_TOOL_STN_CALIBRATION EXTRUDER=extruder LANE=lane1" in message
                   for message in raw_messages)

    def test_toolhead_menu_accepts_explicit_lane(self) -> None:
        func, _ = _make_calibration_func()

        func.cmd_AFC_TOOLHEAD_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", LANE="lane1"))

        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Toolhead Calibration"),
            ("raw", "// action:prompt_text Calibrate extruder with lane1."),
            ("raw", "// action:prompt_text tool_stn controls loading to the nozzle(Note: "
             "Button only shows up when toolhead is unloaded)"),
            ("raw", "// action:prompt_text tool_stn_unload controls the movement after "
             "cutting that clears the toolhead sensor or extruder gears."),
            ("raw", "// action:prompt_text Cutter Retract helps you find how much to "
             "retract when cutting."),
            ("raw", "// action:prompt_button tool_stn|AFC_TOOL_STN_CALIBRATION EXTRUDER=extruder "
             "LANE=lane1|primary"),
            ("raw", "// action:prompt_button Cutter Retract|AFC_CUTTER_CALIBRATION "
             "EXTRUDER=extruder LANE=lane1|info"),
            ("raw", "// action:prompt_button tool_stn_unload|AFC_TOOL_STN_UNLOAD_CALIBRATION "
             "EXTRUDER=extruder LANE=lane1|secondary"),
            ("raw", "// action:prompt_footer_button Cancel|RESPOND TYPE=command "
             "MSG=action:prompt_end|warning"),
            ("raw", "// action:prompt_show"),
        ]

    def test_toolhead_menu_rejects_unknown_explicit_lane(self) -> None:
        func, _ = _make_calibration_func()

        func.cmd_AFC_TOOLHEAD_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", LANE="other"))

        assert "not a valid lane" in func.afc.error.AFC_error.call_args.args[0]

    def test_toolhead_menu_rejects_explicit_lane_without_load_state(self) -> None:
        """An explicit LANE= selection must also require filament loaded into
        the slot, closing the bypass of the picker's own load_state filter."""
        func, _ = _make_calibration_func()
        func.afc.lanes["lane1"].load_state = False

        func.cmd_AFC_TOOLHEAD_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", LANE="lane1"))

        assert "not a valid lane" in func.afc.error.AFC_error.call_args.args[0]
        assert func.logger.messages == [("raw", "// action:prompt_end")]

    def test_toolhead_menu_accepts_explicit_lane_with_load_state(self) -> None:
        """The counterpart of the rejection test above: an explicit LANE=
        selection with filament loaded into the slot must still be accepted."""
        func, _ = _make_calibration_func()
        func.afc.lanes["lane1"].load_state = True

        func.cmd_AFC_TOOLHEAD_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", LANE="lane1"))

        func.afc.error.AFC_error.assert_not_called()
        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("LANE=lane1" in message for message in raw_messages)

    def test_toolhead_menu_rejects_lane_owned_by_different_extruder(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.tools["other"] = MagicMock()

        func.cmd_AFC_TOOLHEAD_CALIBRATION(
            _make_gcmd(EXTRUDER="other", LANE="lane1"))

        assert "not a valid lane" in func.afc.error.AFC_error.call_args.args[0]

    def test_toolhead_menu_reports_extruder_without_lanes(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        func.afc.lanes = {}

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder"))

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("No loaded lanes are available" in message for message in raw_messages)

    def test_toolhead_menu_ignores_current_lane_owned_by_another_extruder(self) -> None:
        func, loaded_extruder = _make_calibration_func()
        other_extruder = MagicMock()
        other_extruder.name = "other"
        other_extruder.is_standalone.return_value = False
        other_extruder.lane_loaded = None
        other_lane = MagicMock()
        other_lane.name = "lane2"
        other_lane.extruder_obj = other_extruder
        func.afc.tools["other"] = other_extruder
        func.afc.lanes["lane1"].extruder_obj = loaded_extruder
        func.afc.lanes["lane2"] = other_lane

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd(EXTRUDER="other"))

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("LANE=lane2" in message for message in raw_messages)

    def test_toolhead_menu_prompts_for_multiple_extruders(self) -> None:
        func, _ = _make_calibration_func()
        other_extruder = MagicMock()
        other_extruder.is_standalone.return_value = False
        func.afc.tools["other"] = other_extruder

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd())

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("Select the toolhead extruder" in message for message in raw_messages)
        assert any("|primary" in message for message in raw_messages)
        assert any("|secondary" in message for message in raw_messages)

    def test_toolhead_menu_skips_standalone_extruder_in_selection(self) -> None:
        """A standalone extruder must not get a button in the multi-extruder
        selection menu at all."""
        func, _ = _make_calibration_func()
        standalone_extruder = MagicMock()
        standalone_extruder.is_standalone.return_value = True
        func.afc.tools["other"] = standalone_extruder

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd())

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("Select the toolhead extruder" in message for message in raw_messages)
        assert not any("EXTRUDER=other" in message for message in raw_messages)

    def test_toolhead_menu_resolves_lane_loaded_on_toolchanger_extruder(self) -> None:
        """Independent coverage of the toolchanger-specific
        `elif extruder.lane_loaded is not None` resolution path, and of the
        `if not extruder.lane_loaded:` False side that suppresses the
        tool_stn button once something is actually loaded."""
        func, extruder = _make_calibration_func()
        func.afc.current = None
        second_lane = MagicMock()
        second_lane.name = "lane2"
        second_lane.extruder_obj = extruder
        func.afc.lanes["lane2"] = second_lane
        extruder.lanes["lane2"] = second_lane
        extruder.lane_loaded = "lane2"

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder"))

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("LANE=lane2" in message for message in raw_messages)
        assert not any("tool_stn|AFC_TOOL_STN_CALIBRATION" in message for message in raw_messages)
        assert any("tool_stn_unload|AFC_TOOL_STN_UNLOAD_CALIBRATION" in message
                   for message in raw_messages)

    def test_toolhead_menu_skips_current_lane_without_load_state(self) -> None:
        """The auto-selected `current_lane` fallback must also require
        load_state, same as an explicit LANE=, instead of trusting
        afc.current alone. Falls through to the single-remaining-lane pick
        since lane1 (the only other lane) is also unloaded here."""
        func, extruder = _make_calibration_func()
        func.afc.lanes["lane1"].load_state = False
        second_lane = MagicMock()
        second_lane.name = "lane2"
        second_lane.extruder_obj = extruder
        second_lane.load_state = True
        func.afc.lanes["lane2"] = second_lane
        extruder.lanes["lane2"] = second_lane

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder"))

        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Toolhead Calibration"),
            ("raw", "// action:prompt_text Calibrate extruder with lane2."),
            ("raw", "// action:prompt_text tool_stn controls loading to the nozzle(Note: "
             "Button only shows up when toolhead is unloaded)"),
            ("raw", "// action:prompt_text tool_stn_unload controls the movement after "
             "cutting that clears the toolhead sensor or extruder gears."),
            ("raw", "// action:prompt_text Cutter Retract helps you find how much to "
             "retract when cutting."),
            ("raw", "// action:prompt_button tool_stn|AFC_TOOL_STN_CALIBRATION EXTRUDER=extruder "
             "LANE=lane2|primary"),
            ("raw", "// action:prompt_button Cutter Retract|AFC_CUTTER_CALIBRATION "
             "EXTRUDER=extruder LANE=lane2|info"),
            ("raw", "// action:prompt_button tool_stn_unload|AFC_TOOL_STN_UNLOAD_CALIBRATION "
             "EXTRUDER=extruder LANE=lane2|secondary"),
            ("raw", "// action:prompt_footer_button Cancel|RESPOND TYPE=command "
             "MSG=action:prompt_end|warning"),
            ("raw", "// action:prompt_show"),
        ]

    def test_toolhead_menu_resolves_lane_loaded_requires_load_state(self) -> None:
        """Independent coverage of the toolchanger-specific fallback: a stale
        or desynced extruder.lane_loaded pointing at a lane that isn't
        actually load_state must not be trusted either."""
        func, extruder = _make_calibration_func()
        func.afc.current = None
        func.afc.lanes["lane1"].load_state = False
        second_lane = MagicMock()
        second_lane.name = "lane2"
        second_lane.extruder_obj = extruder
        second_lane.load_state = False
        func.afc.lanes["lane2"] = second_lane
        extruder.lanes["lane2"] = second_lane
        extruder.lane_loaded = "lane2"

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder"))

        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Toolhead Calibration"),
            ("raw", "// action:prompt_text No loaded lanes are available for extruder "
             "'extruder'."),
            ("raw", "// action:prompt_footer_button Cancel|RESPOND TYPE=command "
             "MSG=action:prompt_end|warning"),
            ("raw", "// action:prompt_show"),
        ]

    def test_toolhead_menu_reports_no_configured_extruders(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.tools = {}

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd())

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("No AFC toolhead extruders" in message for message in raw_messages)

    def test_toolhead_menu_rejects_unknown_extruder(self) -> None:
        func, _ = _make_calibration_func()
        gcmd = _make_gcmd(EXTRUDER="other")

        func.cmd_AFC_TOOLHEAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "'other' is not a valid extruder", pause=False, gcmd=gcmd)

    def test_toolhead_menu_excludes_unloaded_lane_from_single_lane_resolution(self) -> None:
        """A lane with no filament loaded into its slot must not be
        auto-resolved as the representative lane for tool_stn calibration."""
        func, extruder = _make_calibration_func()
        func.afc.current = None
        func.afc.lanes["lane1"].load_state = False

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder"))

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("No loaded lanes are available" in message for message in raw_messages)
        assert not any("LANE=lane1" in message for message in raw_messages)

    def test_toolhead_menu_excludes_unloaded_lane_from_picker(self) -> None:
        """With three lanes on the same extruder, the picker buttons only
        offer the two with filament loaded into their slot."""
        func, extruder = _make_calibration_func()
        func.afc.current = None
        func.afc.lanes["lane1"].load_state = True
        second_lane = MagicMock()
        second_lane.name = "lane2"
        second_lane.extruder_obj = extruder
        second_lane.load_state = True
        third_lane = MagicMock()
        third_lane.name = "lane3"
        third_lane.extruder_obj = extruder
        third_lane.load_state = False
        func.afc.lanes["lane2"] = second_lane
        func.afc.lanes["lane3"] = third_lane
        extruder.lanes["lane2"] = second_lane
        extruder.lanes["lane3"] = third_lane

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder"))

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("LANE=lane1" in message for message in raw_messages)
        assert any("LANE=lane2" in message for message in raw_messages)
        assert not any("LANE=lane3" in message for message in raw_messages)

    def test_toolhead_menu_includes_loaded_lane_in_picker(self) -> None:
        """The counterpart of the exclusion test above: a lane with filament
        loaded into its slot must still appear in the picker."""
        func, extruder = _make_calibration_func()
        func.afc.current = None
        func.afc.lanes["lane1"].load_state = True
        second_lane = MagicMock()
        second_lane.name = "lane2"
        second_lane.extruder_obj = extruder
        second_lane.load_state = False
        func.afc.lanes["lane2"] = second_lane
        extruder.lanes["lane2"] = second_lane

        func.cmd_AFC_TOOLHEAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder"))

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("AFC_TOOL_STN_CALIBRATION EXTRUDER=extruder LANE=lane1" in message
                   for message in raw_messages)
        assert not any("LANE=lane2" in message for message in raw_messages)


class TestCmdAfcToolStnCalibration:
    def _start(self) -> Tuple[afcFunction, MagicMock]:
        """
        Start calibration with a successful load to the extruder gears.

        :return tuple: active calibration helper and extruder mock
        """
        func, extruder = _make_calibration_func()
        func.afc.current = None
        func.afc.TOOL_LOAD.side_effect = (
            lambda lane, **kwargs: setattr(func.afc, "current", lane.name) or True)
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1")
        func._test_start_gcmd = gcmd

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        return func, extruder

    def test_starts_unloaded_and_loads_only_to_gears(self) -> None:
        func, extruder = self._start()

        func.afc.TOOL_LOAD.assert_called_once_with(
            func.afc.lanes["lane1"], load_to_gears=True, restore_temp=False,
            gcmd=func._test_start_gcmd)
        assert func.stn_calibration_active is True
        assert func.stn_calibration_distance == 0.0
        assert func.stn_calibration_original == 72.0
        assert func.stn_calibration_extruder == "extruder"
        assert func.stn_calibration_lane == "lane1"
        extruder._update_tool_stn.assert_not_called()
        assert extruder.tool_stn == 0.0
        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("Forward 1mm" in message for message in raw_messages)
        assert any("Back 1mm" in message for message in raw_messages)
        assert any("Unload & Reload" in message for message in raw_messages)
        assert any("Moved 0.000mm" in message for message in raw_messages)

    def test_moves_forward_slowly_and_saves_measured_distance(self) -> None:
        func, extruder = self._start()
        extruder._update_tool_stn.reset_mock()

        func.cmd_AFC_TOOL_STN_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", LANE="lane1", MOVE=5))
        func.cmd_AFC_TOOL_STN_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", LANE="lane1", SAVE=1))

        func.afc.move_e_pos.assert_called_once_with(
            5.0, 1.0, "tool_stn calibration", wait_tool=True)
        extruder._update_tool_stn.assert_called_once_with(5.0)
        assert extruder.tool_stn == 5.0
        func.ConfigRewrite.assert_called_once_with(
            "AFC_extruder extruder", "tool_stn", 5.0, "")
        assert func.stn_calibration_active is False
        assert func.stn_calibration_distance == 0.0
        assert func.stn_calibration_original == 0.0
        assert func.stn_calibration_extruder is None
        assert func.stn_calibration_lane is None

    def test_tool_stn_rejects_unknown_extruder(self) -> None:
        func, _ = _make_calibration_func()
        gcmd = _make_gcmd(EXTRUDER="other")

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "'other' is not a valid extruder", pause=False, gcmd=gcmd)

    def test_rejects_start_while_toolhead_is_loaded(self) -> None:
        func, extruder = _make_calibration_func()
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1")

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        extruder._update_tool_stn.assert_not_called()
        func.afc.error.AFC_error.assert_called_once_with(
            "Unload the toolhead before starting tool_stn calibration.", pause=False, gcmd=gcmd)

    def test_rejects_move_behind_starting_point(self) -> None:
        """Unlike the sibling calibration macros, an out-of-range MOVE here
        is reported inline in the calibration status prompt text, not via
        AFC_error -- intended behavior, confirmed by the user."""
        func, extruder = self._start()
        extruder._update_tool_stn.reset_mock()

        func.cmd_AFC_TOOL_STN_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", LANE="lane1", MOVE=-1))

        func.afc.move_e_pos.assert_not_called()
        extruder._update_tool_stn.assert_not_called()
        func.afc.error.AFC_error.assert_not_called()
        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any(
            "Error: tool_stn calibration cannot move behind its starting point." in message
            for message in raw_messages)

    def test_tool_stn_rejects_calibration_during_print(self) -> None:
        func, _ = _make_calibration_func()
        print_stats = func.printer.lookup_object("print_stats")
        print_stats.get_status.return_value = {"state": "printing", "filename": "part.gcode"}

        func.cmd_AFC_TOOL_STN_CALIBRATION(_make_gcmd(EXTRUDER="extruder"))

        assert "during a print" in func.afc.error.AFC_error.call_args.args[0]
        func.afc.CHANGE_TOOL.assert_not_called()

    def test_stops_when_lane_cannot_load_to_gears(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        func.afc.TOOL_LOAD.return_value = False
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1")

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Lane 'lane1' did not load to the extruder gears or sensor.", pause=False, gcmd=gcmd)
        assert func.stn_calibration_active is False

    def test_unload_and_reload_tests_current_distance(self) -> None:
        func, _ = self._start()
        func.afc.TOOL_LOAD.reset_mock()
        func.afc.TOOL_LOAD.side_effect = (
            lambda lane, **kwargs: setattr(func.afc, "current", lane.name) or True)
        func.afc.TOOL_UNLOAD.side_effect = (
            lambda lane, **kwargs: setattr(func.afc, "current", None) or True)
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1", TEST=1)

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        func.afc.TOOL_UNLOAD.assert_called_once_with(
            func.afc.lanes["lane1"], restore_temp=False, gcmd=gcmd)
        func.afc.TOOL_LOAD.assert_called_once_with(
            func.afc.lanes["lane1"], restore_temp=False, gcmd=gcmd)
        assert func.stn_calibration_active is True

    def test_cancel_restores_original_value_and_resets_state(self) -> None:
        func, extruder = self._start()
        extruder._update_tool_stn.reset_mock()

        func.cmd_AFC_TOOL_STN_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", LANE="lane1", CANCEL=1))

        extruder._update_tool_stn.assert_called_once_with(72.0)
        assert extruder.tool_stn == 72.0
        assert func.stn_calibration_active is False
        assert func.stn_calibration_distance == 0.0
        assert func.stn_calibration_original == 0.0
        assert func.stn_calibration_extruder is None
        assert func.stn_calibration_lane is None

    def test_moves_back_after_a_forward_move(self) -> None:
        func, extruder = self._start()
        extruder._update_tool_stn.reset_mock()

        func.cmd_AFC_TOOL_STN_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", LANE="lane1", MOVE=5))
        func.cmd_AFC_TOOL_STN_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", LANE="lane1", MOVE=-1))

        assert func.afc.move_e_pos.call_args_list == [
            call(5.0, 1.0, "tool_stn calibration", wait_tool=True),
            call(-1.0, 1.0, "tool_stn calibration", wait_tool=True),
        ]
        assert extruder._update_tool_stn.call_args_list == [call(5.0), call(4.0)]
        assert func.stn_calibration_distance == 4.0
        assert extruder.tool_stn == 4.0

    def test_rejects_move_larger_than_limit(self) -> None:
        """Reported inline in the status prompt text, not via AFC_error --
        see test_rejects_move_behind_starting_point."""
        func, extruder = self._start()
        extruder._update_tool_stn.reset_mock()

        func.cmd_AFC_TOOL_STN_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", LANE="lane1", MOVE=26))

        func.afc.move_e_pos.assert_not_called()
        extruder._update_tool_stn.assert_not_called()
        func.afc.error.AFC_error.assert_not_called()
        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("Error: Move must be between -25mm and 25mm." in message
                   for message in raw_messages)

    def test_rejects_total_distance_larger_than_limit(self) -> None:
        """Reported inline in the status prompt text, not via AFC_error --
        see test_rejects_move_behind_starting_point."""
        func, extruder = self._start()
        func.stn_calibration_distance = 190.0
        extruder.tool_stn = 190.0
        extruder._update_tool_stn.reset_mock()

        func.cmd_AFC_TOOL_STN_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", LANE="lane1", MOVE=11))

        func.afc.move_e_pos.assert_not_called()
        extruder._update_tool_stn.assert_not_called()
        func.afc.error.AFC_error.assert_not_called()
        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("Error: tool_stn calibration cannot exceed 200mm." in message
                   for message in raw_messages)

    def test_rejects_save_before_filament_has_moved(self) -> None:
        func, extruder = self._start()
        extruder._update_tool_stn.reset_mock()
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1", SAVE=1)

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        func.ConfigRewrite.assert_not_called()
        assert func.stn_calibration_active is True
        func.afc.error.AFC_error.assert_called_once_with(
            "Move filament to the nozzle before saving tool_stn.", pause=False, gcmd=gcmd)

    @pytest.mark.parametrize("active_attr", [
        "stn_unload_calibration_active",
        "cutter_calibration_active",
    ])
    def test_rejects_other_active_toolhead_calibration(self, active_attr: str) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        setattr(func, active_attr, True)
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1")

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        func.afc.TOOL_LOAD.assert_not_called()
        func.afc.error.AFC_error.assert_called_once_with(
            "Finish or cancel the active toolhead calibration first.", pause=False, gcmd=gcmd)

    def test_rejects_unknown_lane_before_loading(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="other")

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        func.afc.TOOL_LOAD.assert_not_called()
        func.afc.error.AFC_error.assert_called_once_with(
            "'other' is not a valid lane", pause=False, gcmd=gcmd)

    def test_failed_test_unload_resets_session(self) -> None:
        """A failed test-unload resets the calibration session, matching
        the failed-test-reload path right below -- intended behavior,
        confirmed by the user."""
        func, _ = self._start()
        func.afc.TOOL_UNLOAD.return_value = False
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1", TEST=1)

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        assert func.stn_calibration_active is False
        assert func.stn_calibration_distance == 0.0
        assert func.stn_calibration_original == 0.0
        assert func.stn_calibration_extruder is None
        assert func.stn_calibration_lane is None
        func.afc.error.AFC_error.assert_called_once_with(
            "Lane 'lane1' did not unload during the tool_stn test.", pause=False, gcmd=gcmd)

    def test_failed_test_reload_restores_original_and_resets_session(self) -> None:
        func, extruder = self._start()
        extruder._update_tool_stn.reset_mock()
        func.afc.TOOL_LOAD.reset_mock()
        func.afc.TOOL_LOAD.side_effect = None
        func.afc.TOOL_LOAD.return_value = False
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1", TEST=1)

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        extruder._update_tool_stn.assert_called_once_with(72.0)
        assert extruder.tool_stn == 72.0
        assert func.stn_calibration_active is False
        assert func.stn_calibration_distance == 0.0
        assert func.stn_calibration_original == 0.0
        assert func.stn_calibration_extruder is None
        assert func.stn_calibration_lane is None
        func.afc.error.AFC_error.assert_called_once_with(
            "Lane 'lane1' did not reload; the original tool_stn was restored.",
            pause=False, gcmd=gcmd)

    def test_active_session_rejects_different_extruder(self) -> None:
        func, _ = self._start()
        func.afc.tools["other"] = MagicMock()
        gcmd = _make_gcmd(EXTRUDER="other", LANE="lane1")

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Finish or cancel the active tool_stn calibration first.", pause=False, gcmd=gcmd)

    def test_active_session_rejects_different_lane(self) -> None:
        func, _ = self._start()
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="other")

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Finish or cancel the active tool_stn calibration first.", pause=False, gcmd=gcmd)

    def test_active_session_rejects_missing_lane(self) -> None:
        func, _ = self._start()
        del func.afc.lanes["lane1"]
        gcmd = _make_gcmd(EXTRUDER="extruder")

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "The calibrated lane is no longer loaded; cancel and restart calibration.",
            pause=False, gcmd=gcmd)

    def test_active_session_rejects_changed_loaded_lane(self) -> None:
        func, _ = self._start()
        func.afc.current = None
        gcmd = _make_gcmd(EXTRUDER="extruder")

        func.cmd_AFC_TOOL_STN_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "The calibrated lane is no longer loaded; cancel and restart calibration.",
            pause=False, gcmd=gcmd)

    def test_cancel_remains_available_after_loaded_lane_changes(self) -> None:
        func, extruder = self._start()
        func.afc.current = None
        extruder._update_tool_stn.reset_mock()

        func.cmd_AFC_TOOL_STN_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", CANCEL=1))

        extruder._update_tool_stn.assert_called_once_with(72.0)
        assert func.stn_calibration_active is False


class TestCmdAfcToolStnUnloadCalibration:
    def test_tool_stn_unload_rejects_unknown_extruder(self) -> None:
        func, _ = _make_calibration_func()
        gcmd = _make_gcmd(EXTRUDER="other")

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "'other' is not a valid extruder", pause=False, gcmd=gcmd)

    def test_tool_stn_unload_reports_failed_automatic_lane_load(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        gcmd = _make_gcmd(EXTRUDER="extruder")

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Lane 'lane1' did not load for toolhead calibration.", pause=False, gcmd=gcmd)

    def test_tool_stn_unload_requires_a_configured_lane(self) -> None:
        func, _ = _make_calibration_func()
        del func.afc.lanes["lane1"]
        gcmd = _make_gcmd(EXTRUDER="extruder")

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Select a lane from AFC_TOOLHEAD_CALIBRATION before continuing.", pause=False,
            gcmd=gcmd)

    def test_tool_stn_unload_rejects_calibration_during_print(self) -> None:
        func, _ = _make_calibration_func()
        print_stats = func.printer.lookup_object("print_stats")
        print_stats.get_status.return_value = {"state": "printing", "filename": "part.gcode"}
        gcmd = _make_gcmd(EXTRUDER="extruder")

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "tool_stn_unload calibration cannot run during a print.", pause=False, gcmd=gcmd)

    def test_tool_stn_unload_initial_call_prompts_before_cutting(self) -> None:
        func, _ = _make_calibration_func()

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder"))

        func.afc.gcode.run_script_from_command.assert_not_called()
        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("Cut & Begin" in message for message in raw_messages)

    def test_tool_stn_unload_starts_with_cut(self) -> None:
        func, _ = _make_calibration_func()
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))
        func.afc.gcode.run_script_from_command.assert_called_once_with("AFC_CUT EXTRUDER=extruder")
        func.afc.toolhead.wait_moves.assert_called_once()
        assert func.stn_unload_calibration_active is True
        assert func.stn_unload_calibration_distance == 0.0
        assert func.stn_unload_calibration_extruder == "extruder"
        assert func.stn_unload_calibration_lane == "lane1"

    def test_tool_stn_unload_loads_selected_lane_before_cutting(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.current = None
        func.afc.CHANGE_TOOL.side_effect = (
            lambda lane, gcmd=None: setattr(func.afc, "current", lane.name))
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1", START=1)

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.CHANGE_TOOL.assert_called_once_with(func.afc.lanes["lane1"], gcmd=gcmd)
        func.afc.gcode.run_script_from_command.assert_called_once_with(
            "AFC_CUT EXTRUDER=extruder")

    def test_tool_stn_unload_falls_back_to_default_cutter_command(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.tool_cut_cmd = None

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))

        func.afc.gcode.run_script_from_command.assert_called_once_with("AFC_CUT EXTRUDER=extruder")

    def test_tool_stn_unload_uses_configured_cutter_command(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.tool_cut_cmd = "CUSTOM_CUT"

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))

        func.afc.gcode.run_script_from_command.assert_called_once_with(
            "CUSTOM_CUT EXTRUDER=extruder")

    def test_tool_stn_unload_rejects_disabled_cutter(self) -> None:
        func, _ = _make_calibration_func()
        func.afc.tool_cut = False
        gcmd = _make_gcmd(EXTRUDER="extruder", START=1)

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "tool_stn_unload calibration requires tool cutting, but the cutter is disabled.",
            pause=False, gcmd=gcmd)
        func.afc.gcode.run_script_from_command.assert_not_called()

    def test_tool_stn_unload_rejects_active_cutter_calibration(self) -> None:
        func, _ = _make_calibration_func()
        func.cutter_calibration_active = True
        gcmd = _make_gcmd(EXTRUDER="extruder", START=1)

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Cutter retract calibration is already active.", pause=False, gcmd=gcmd)
        func.afc.gcode.run_script_from_command.assert_not_called()

    def test_tool_stn_unload_rejects_extruder_without_loaded_lane(self) -> None:
        func, loaded_extruder = _make_calibration_func()
        selected_extruder = MagicMock()
        selected_extruder.name = "other_extruder"
        func.afc.tools["other_extruder"] = selected_extruder
        loaded_lane = MagicMock()
        loaded_lane.name = "lane1"
        loaded_lane.extruder_obj = loaded_extruder
        func.afc.lanes["lane1"] = loaded_lane
        gcmd = _make_gcmd(EXTRUDER="other_extruder", LANE="lane1", START=1)

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Lane 'lane1' does not belong to extruder 'other_extruder'.", pause=False, gcmd=gcmd)
        func.afc.gcode.run_script_from_command.assert_not_called()

    def test_active_tool_stn_unload_rejects_different_extruder(self) -> None:
        func, _ = _make_calibration_func()
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))
        func.stn_unload_calibration_extruder = "other"
        gcmd = _make_gcmd(EXTRUDER="extruder")

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Finish or cancel the active tool_stn_unload calibration first.", pause=False,
            gcmd=gcmd)

    def test_active_tool_stn_unload_rejects_different_lane(self) -> None:
        func, _ = _make_calibration_func()
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", START=1))
        func.stn_unload_calibration_lane = "other"
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1")

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Finish or cancel the active tool_stn_unload calibration first.", pause=False,
            gcmd=gcmd)

    def test_active_tool_stn_unload_rejects_when_loaded_lane_changes(self) -> None:
        func, _ = _make_calibration_func()
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(
            _make_gcmd(EXTRUDER="extruder", START=1))
        func.afc.current = "other"
        gcmd = _make_gcmd(EXTRUDER="extruder", LANE="lane1")

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Finish or cancel the active tool_stn_unload calibration first.", pause=False,
            gcmd=gcmd)

    def test_tool_stn_unload_measures_restores_and_saves(self) -> None:
        func, extruder = _make_calibration_func()
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", MOVE=-25))
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", MOVE=-5))
        extruder.tool_start_state = False
        func.afc.move_e_pos.reset_mock()
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", COMPLETE=1))
        # Restores extruder.tool_stn (the full gear-to-nozzle distance), not the
        # measured retract amount: a normal toolchange moves tool_stn once filament
        # reaches the gears/sensor, and AFC_CUT's own retract isn't accounted for
        # in `measured`, so tool_stn is the correct amount to load back.
        func.afc.move_e_pos.assert_called_once_with(
            72.0, 1.0, "tool_stn_unload calibration restore", wait_tool=True)
        extruder._update_tool_stn_unload.assert_called_once_with(30.0)
        assert extruder.tool_stn_unload == 30.0
        func.ConfigRewrite.assert_called_once_with(
            "AFC_extruder extruder", "tool_stn_unload", 30.0, "")
        assert func.stn_unload_calibration_active is False
        assert func.stn_unload_calibration_distance == 0.0
        assert func.stn_unload_calibration_extruder is None
        assert func.stn_unload_calibration_lane is None

    def test_tool_stn_unload_rejects_zero_without_restore_move(self) -> None:
        func, extruder = _make_calibration_func()
        extruder.tool_start_state = False
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))
        gcmd = _make_gcmd(EXTRUDER="extruder", COMPLETE=1)

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.move_e_pos.assert_not_called()
        extruder._update_tool_stn_unload.assert_not_called()
        func.ConfigRewrite.assert_not_called()
        func.afc.error.AFC_error.assert_called_once_with(
            "Retract filament before saving tool_stn_unload.", pause=False, gcmd=gcmd)
        assert func.stn_unload_calibration_active is True

    def test_tool_stn_unload_cancel_with_zero_movement_skips_restore_move(self) -> None:
        """Independent coverage of the `if measured:` False side: CANCEL
        isn't rejected for a zero measured distance the way COMPLETE is, but
        with nothing retracted there's nothing to restore."""
        func, extruder = _make_calibration_func()
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", CANCEL=1))

        func.afc.move_e_pos.assert_not_called()
        assert func.stn_unload_calibration_active is False

    def test_tool_stn_unload_rejects_save_while_sensor_is_triggered(self) -> None:
        """A rejected save while a sensor is triggered resets the
        calibration session -- intended behavior, confirmed by the user."""
        func, extruder = _make_calibration_func()
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))
        gcmd = _make_gcmd(EXTRUDER="extruder", COMPLETE=1)

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Cannot save tool_stn_unload while a toolhead sensor is still triggered.",
            pause=False, gcmd=gcmd)
        extruder._update_tool_stn_unload.assert_not_called()
        func.ConfigRewrite.assert_not_called()
        assert func.stn_unload_calibration_active is False
        assert func.stn_unload_calibration_distance == 0.0
        assert func.stn_unload_calibration_extruder is None
        assert func.stn_unload_calibration_lane is None

    def test_tool_stn_unload_rejects_save_while_tool_end_sensor_is_triggered(self) -> None:
        """Same reset-on-rejection behavior as the tool_start sensor case
        above, triggered via tool_end instead."""
        func, extruder = _make_calibration_func()
        extruder.tool_start = None
        extruder.tool_start_state = False
        extruder.tool_end = "PA2"
        extruder.tool_end_state = True
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))
        gcmd = _make_gcmd(EXTRUDER="extruder", COMPLETE=1)

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Cannot save tool_stn_unload while a toolhead sensor is still triggered.",
            pause=False, gcmd=gcmd)
        extruder._update_tool_stn_unload.assert_not_called()
        assert func.stn_unload_calibration_active is False
        assert func.stn_unload_calibration_distance == 0.0
        assert func.stn_unload_calibration_extruder is None
        assert func.stn_unload_calibration_lane is None

    def test_tool_stn_unload_cancel_restores_and_logs_exact_message(self) -> None:
        func, _ = _make_calibration_func()
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", MOVE=-5))
        func.afc.move_e_pos.reset_mock()
        func.logger.messages = []

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", CANCEL=1))

        # Restores extruder.tool_stn (72.0 from the fixture), not the 5.0mm
        # measured retract -- see test_tool_stn_unload_measures_restores_and_saves.
        func.afc.move_e_pos.assert_called_once_with(
            72.0, 1.0, "tool_stn_unload calibration restore", wait_tool=True)
        assert func.logger.messages == [
            ("raw", "// action:prompt_begin Moving extruder"),
            ("raw", "// action:prompt_text Moving filament back tool_stn distance(72.0)"),
            ("raw", "// action:prompt_show"),
            ("raw", "// action:prompt_end"),
            ("info", "tool_stn_unload calibration cancelled; measured movement was restored."),
        ]
        assert func.stn_unload_calibration_active is False
        assert func.stn_unload_calibration_distance == 0.0
        assert func.stn_unload_calibration_extruder is None
        assert func.stn_unload_calibration_lane is None

    def test_tool_stn_unload_rejects_move_larger_than_limit(self) -> None:
        func, _ = _make_calibration_func()
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))
        gcmd = _make_gcmd(EXTRUDER="extruder", MOVE=-26)

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Calibration MOVE must be between -25mm and 25mm.", pause=False, gcmd=gcmd)

    def test_tool_stn_unload_rejects_total_retraction_larger_than_limit(self) -> None:
        func, _ = _make_calibration_func()
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))
        func.stn_unload_calibration_distance = 190.0
        gcmd = _make_gcmd(EXTRUDER="extruder", MOVE=-11)

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Calibration is limited to 200mm of retraction.", pause=False, gcmd=gcmd)

    def test_tool_stn_unload_undo_at_start_does_not_move(self) -> None:
        func, _ = _make_calibration_func()
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", MOVE=5))

        func.afc.move_e_pos.assert_not_called()

    def test_tool_stn_unload_describes_missing_tool_start_sensor(self) -> None:
        func, extruder = _make_calibration_func()
        extruder.tool_start = None

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("Tool-start sensor status: unavailable" in message for message in raw_messages)

    def test_tool_stn_unload_describes_triggered_tool_start_sensor(self) -> None:
        func, _ = _make_calibration_func()

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("Tool-start sensor status: triggered" in message for message in raw_messages)

    def test_tool_stn_unload_describes_clear_tool_start_sensor(self) -> None:
        func, extruder = _make_calibration_func()
        extruder.tool_start_state = False

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))

        raw_messages = [message for level, message in func.logger.messages if level == "raw"]
        assert any("Tool-start sensor status: clear" in message for message in raw_messages)

    def test_tool_stn_unload_buffer_sensor_rejects_zero_save(self) -> None:
        func, extruder = _make_calibration_func()
        extruder.tool_start = "buffer"
        extruder.tool_start_state = True
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))
        gcmd = _make_gcmd(EXTRUDER="extruder", COMPLETE=1)

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        extruder._update_tool_stn_unload.assert_not_called()
        func.ConfigRewrite.assert_not_called()
        func.afc.error.AFC_error.assert_called_once_with(
            "Retract filament before saving tool_stn_unload.", pause=False, gcmd=gcmd)
        assert func.stn_unload_calibration_active is True

    def test_tool_stn_unload_clear_tool_end_sensor_rejects_zero_save(self) -> None:
        func, extruder = _make_calibration_func()
        extruder.tool_start = None
        extruder.tool_end = "PA2"
        extruder.tool_end_state = False
        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(_make_gcmd(EXTRUDER="extruder", START=1))
        gcmd = _make_gcmd(EXTRUDER="extruder", COMPLETE=1)

        func.cmd_AFC_TOOL_STN_UNLOAD_CALIBRATION(gcmd)

        extruder._update_tool_stn_unload.assert_not_called()
        func.ConfigRewrite.assert_not_called()
        func.afc.error.AFC_error.assert_called_once_with(
            "Retract filament before saving tool_stn_unload.", pause=False, gcmd=gcmd)
        assert func.stn_unload_calibration_active is True


class TestGetFilamentStatus:
    def _make_lane(self, prep=False, load=False, tool_loaded=False):
        lane = MagicMock()
        lane.prep_state = prep
        lane.load_state = load
        lane.name = "lane1"
        lane.led_tool_loaded = "0,1,0,0"
        lane.led_ready = "0,1,0,0"
        lane.led_prep_loaded = "0,0.5,0,0"
        lane.led_not_ready = "0,0,0,0.25"
        lane.material = "PLA"
        if tool_loaded:
            ext = MagicMock()
            ext.lane_loaded = "lane1"
            lane.extruder_obj = ext
        else:
            if lane.extruder_obj:
                lane.extruder_obj.lane_loaded = ""
        return lane

    def test_not_ready_when_not_prep(self):
        func = _make_func()
        lane = self._make_lane(prep=False, load=False)
        status = func.get_filament_status(lane)
        assert "Not Ready" in status

    def test_prep_only_when_prep_no_load(self):
        func = _make_func()
        lane = self._make_lane(prep=True, load=False)
        status = func.get_filament_status(lane)
        assert "Prep" in status

    def test_ready_when_prep_and_load(self):
        func = _make_func()
        lane = self._make_lane(prep=True, load=True, tool_loaded=False)
        lane.extruder_obj = MagicMock()
        lane.extruder_obj.lane_loaded = "other_lane"
        status = func.get_filament_status(lane)
        assert "Ready" in status

    def test_in_tool_when_tool_loaded(self):
        func = _make_func()
        lane = self._make_lane(prep=True, load=True, tool_loaded=True)
        status = func.get_filament_status(lane)
        assert "In Tool" in status


# ── afcDeltaTime ──────────────────────────────────────────────────────────────

class TestAfcDeltaTime:
    def _make_delta(self):
        from tests.conftest import MockAFC
        afc = MockAFC()
        return afcDeltaTime(afc)

    def test_set_start_time_sets_start(self):
        dt = self._make_delta()
        dt.set_start_time()
        assert dt.start_time is not None

    def test_set_start_time_sets_last_time(self):
        dt = self._make_delta()
        dt.set_start_time()
        assert dt.last_time is not None

    def test_log_with_time_logs_message(self):
        dt = self._make_delta()
        dt.set_start_time()
        dt.log_with_time("test message", debug=True)
        debug_msgs = [m for lvl, m in dt.logger.messages if lvl == "debug"]
        assert any("test message" in m for m in debug_msgs)

    def test_log_with_time_includes_delta(self):
        dt = self._make_delta()
        dt.set_start_time()
        dt.log_with_time("delta check", debug=False)
        info_msgs = [m for lvl, m in dt.logger.messages if lvl == "info"]
        assert any("delta check" in m for m in info_msgs)

    def test_log_total_time_returns_float(self):
        dt = self._make_delta()
        dt.set_start_time()
        result = dt.log_total_time("total")
        assert isinstance(result, float)
        assert result >= 0.0

    def test_log_major_delta_returns_float(self):
        dt = self._make_delta()
        dt.set_start_time()
        result = dt.log_major_delta("major delta")
        assert isinstance(result, float)
        assert result >= 0.0

    def test_log_with_time_before_set_start_does_not_raise(self):
        dt = self._make_delta()
        dt.start_time = None
        dt.last_time = None
        # Should not raise (caught internally)
        dt.log_with_time("safe call")


# ── is_homed ──────────────────────────────────────────────────────────────────

class TestIsHomed:
    def _wire(self, func, homed_axes, disable_homing_check):
        """Wire up func.afc.toolhead.get_kinematics().get_status() to report
        `homed_axes` (a string such as "xyz", "xy", or "") and set
        func.afc.disable_homing_check."""
        func.afc.disable_homing_check = disable_homing_check
        kin = MagicMock()
        kin.get_status.return_value = {"homed_axes": homed_axes}
        func.afc.toolhead.get_kinematics.return_value = kin

    @pytest.mark.parametrize(
        "for_move,disable_homing_check,homed_axes,expected",
        [
            # for_move=True: homing_check is always True, disable_homing_check
            # is ignored entirely, and each individually-missing axis returns False.
            (True, False, "xyz", True),
            (True, False, "xy", False),
            (True, False, "xz", False),
            (True, False, "yz", False),
            (True, False, "", False),
            (True, True, "xyz", True),
            (True, True, "xy", False),
            (True, True, "xz", False),
            (True, True, "yz", False),
            (True, True, "", False),
            # for_move=False, disable_homing_check=False: homing_check is True,
            # so behavior matches the for_move=True/disable_homing_check=False case.
            (False, False, "xyz", True),
            (False, False, "xy", False),
            (False, False, "xz", False),
            (False, False, "yz", False),
            (False, False, "", False),
            # for_move=False, disable_homing_check=True: homing_check is False,
            # so the result is always True regardless of which axes are homed.
            (False, True, "xyz", True),
            (False, True, "xy", True),
            (False, True, "xz", True),
            (False, True, "yz", True),
            (False, True, "", True),
        ],
    )
    def test_all_permutations(self, for_move, disable_homing_check, homed_axes, expected):
        func = _make_func()
        self._wire(func, homed_axes, disable_homing_check)
        assert func.is_homed(for_move=for_move) is expected

    def test_default_for_move_is_false(self):
        """for_move defaults to False, so it should behave like the
        for_move=False cases above when called with no argument."""
        func = _make_func()
        self._wire(func, homed_axes="xy", disable_homing_check=False)
        assert func.is_homed() is False

        func = _make_func()
        self._wire(func, homed_axes="xyz", disable_homing_check=False)
        assert func.is_homed() is True

        func = _make_func()
        self._wire(func, homed_axes="xy", disable_homing_check=True)
        assert func.is_homed() is True


# ── _rename ───────────────────────────────────────────────────────────────────

class TestRename:
    def test_rename_calls_register_command_for_base(self):
        func = _make_func()
        func.afc.gcode.register_command = MagicMock(return_value=MagicMock())
        mock_func = MagicMock()
        func._rename("RESUME", "_AFC_RENAMED_RESUME_", mock_func, "help text")
        calls = func.afc.gcode.register_command.call_args_list
        # Should call at least: register_command("RESUME", None) and
        # register_command("RESUME", mock_func, ...)
        names = [c[0][0] for c in calls]
        assert "RESUME" in names

    def test_rename_registers_afc_function_under_base_name(self):
        func = _make_func()
        mock_func = MagicMock()
        prev_cmd = MagicMock()
        # _rename calls register_command 3 times: unregister, re-register old, register new
        func.afc.gcode.register_command = MagicMock(side_effect=[prev_cmd, None, None])
        func._rename("RESUME", "_AFC_RENAMED_RESUME_", mock_func, "help")
        final_call = func.afc.gcode.register_command.call_args_list[-1]
        assert final_call[0][1] is mock_func

    def test_rename_logs_debug_when_command_not_found(self):
        func = _make_func()
        func.afc.gcode.register_command = MagicMock(return_value=None)
        func._rename("RESUME", "_AFC_RENAMED_RESUME_", MagicMock(), "help")
        debug_msgs = [m for lvl, m in func.logger.messages if lvl == "debug"]
        assert any("RESUME" in m for m in debug_msgs)

# ── get_extruder_pos ──────────────────────────────────────────────────────────

def _wire_extruder(func, last_position):
    """
    Return a mock extruder whose find_past_position returns last_position,
    and wire it up as the toolhead's active extruder.
    """
    extruder = MagicMock()
    extruder.find_past_position.return_value = last_position
    func.afc.toolhead.get_extruder.return_value = extruder
    return extruder


# ── eventtime resolution ──────────────────────────────────────────────────────

class TestGetExtruderPos_EventTime:
    def test_uses_reactor_monotonic_when_eventtime_is_none(self):
        func = _make_func()
        func.afc.reactor.monotonic.return_value = 42.0
        func.mcu.estimated_print_time.return_value = 10.0
        _wire_extruder(func, last_position=5.0)

        func.get_extruder_pos(eventtime=None)

        func.afc.reactor.monotonic.assert_called_once()

    def test_passes_reactor_monotonic_to_mcu(self):
        func = _make_func()
        func.afc.reactor.monotonic.return_value = 42.0
        func.mcu.estimated_print_time.return_value = 10.0
        _wire_extruder(func, last_position=5.0)

        func.get_extruder_pos(eventtime=None)

        func.mcu.estimated_print_time.assert_called_once_with(42.0)

    def test_uses_provided_eventtime_directly(self):
        func = _make_func()
        func.mcu.estimated_print_time.return_value = 10.0
        _wire_extruder(func, last_position=5.0)

        func.get_extruder_pos(eventtime=99.0)

        func.mcu.estimated_print_time.assert_called_once_with(99.0)

    def test_reactor_not_called_when_eventtime_provided(self):
        func = _make_func()
        func.mcu.estimated_print_time.return_value = 10.0
        _wire_extruder(func, last_position=5.0)

        func.get_extruder_pos(eventtime=99.0)

        func.afc.reactor.monotonic.assert_not_called()


# ── extruder resolution ───────────────────────────────────────────────────────

class TestGetExtruderPos_ExtruderParam:
    def test_falls_back_to_toolhead_get_extruder_when_none(self):
        func = _make_func()
        func.mcu.estimated_print_time.return_value = 10.0
        _wire_extruder(func, last_position=5.0)

        func.get_extruder_pos(eventtime=1.0, extruder=None)

        func.afc.toolhead.get_extruder.assert_called_once()

    def test_uses_provided_extruder_directly(self):
        func = _make_func()
        func.mcu.estimated_print_time.return_value = 10.0

        custom_extruder = MagicMock()
        custom_extruder.find_past_position.return_value = 7.0

        func.get_extruder_pos(eventtime=1.0, extruder=custom_extruder)

        custom_extruder.find_past_position.assert_called_once()
        func.afc.toolhead.get_extruder.assert_not_called()

    def test_find_past_position_receives_print_time(self):
        func = _make_func()
        func.mcu.estimated_print_time.return_value = 55.5

        custom_extruder = MagicMock()
        custom_extruder.find_past_position.return_value = 3.0

        func.get_extruder_pos(eventtime=1.0, extruder=custom_extruder)

        custom_extruder.find_past_position.assert_called_once_with(55.5)


# ── return value logic ────────────────────────────────────────────────────────

class TestGetExtruderPos_ReturnValue:
    def test_returns_last_position_when_past_is_none(self):
        func = _make_func()
        func.mcu.estimated_print_time.return_value = 10.0
        _wire_extruder(func, last_position=8.0)

        result = func.get_extruder_pos(eventtime=1.0, past_extruder_position=None)

        assert result == pytest.approx(8.0)

    def test_returns_last_position_when_greater_than_past(self):
        func = _make_func()
        func.mcu.estimated_print_time.return_value = 10.0
        _wire_extruder(func, last_position=15.0)

        result = func.get_extruder_pos(eventtime=1.0, past_extruder_position=10.0)

        assert result == pytest.approx(15.0)

    def test_returns_past_position_when_last_equals_past(self):
        func = _make_func()
        func.mcu.estimated_print_time.return_value = 10.0
        _wire_extruder(func, last_position=10.0)

        result = func.get_extruder_pos(eventtime=1.0, past_extruder_position=10.0)

        assert result == pytest.approx(10.0)

    def test_returns_past_position_when_last_less_than_past(self):
        func = _make_func()
        func.mcu.estimated_print_time.return_value = 10.0
        _wire_extruder(func, last_position=3.0)

        result = func.get_extruder_pos(eventtime=1.0, past_extruder_position=9.0)

        assert result == pytest.approx(9.0)

    def test_returns_zero_when_last_is_zero_and_past_is_none(self):
        func = _make_func()
        func.mcu.estimated_print_time.return_value = 10.0
        _wire_extruder(func, last_position=0.0)

        result = func.get_extruder_pos(eventtime=1.0, past_extruder_position=None)

        assert result == pytest.approx(0.0)

    def test_returns_float(self):
        func = _make_func()
        func.mcu.estimated_print_time.return_value = 10.0
        _wire_extruder(func, last_position=4.5)

        result = func.get_extruder_pos(eventtime=1.0)

        assert isinstance(result, float)
# ── end get_extruder_pos ──────────────────────────────────────────────────────

# ── register_tool_macro ─────────────────────────────────────────────────────────

def _wire_change_tool(func):
    """Set the cmd_CHANGE_TOOL attributes register_tool_macro forwards along."""
    func.afc.cmd_CHANGE_TOOL = MagicMock()
    func.afc.cmd_CHANGE_TOOL_help = "help text"


class TestRegisterToolMacro:
    def test_no_rename_registers_command_directly(self):
        func = _make_func()
        _wire_change_tool(func)
        func.afc.gcode.register_command = MagicMock()

        func.register_tool_macro("lane1", "T0", "")

        func.afc.gcode.register_command.assert_called_once_with(
            "T0", func.afc.cmd_CHANGE_TOOL, desc=func.afc.cmd_CHANGE_TOOL_help
        )

    def test_rename_delegates_to_rename_method(self):
        func = _make_func()
        _wire_change_tool(func)
        func._rename = MagicMock()

        func.register_tool_macro("lane1", "T0", "_T0")

        func._rename.assert_called_once_with(
            "T0", "_T0", func.afc.cmd_CHANGE_TOOL, func.afc.cmd_CHANGE_TOOL_help
        )
        assert "T0" not in func.afc.gcode._commands

    def test_exception_when_registering_directly_logs_error(self):
        func = _make_func()
        _wire_change_tool(func)
        func.afc.gcode.register_command = MagicMock(side_effect=Exception("boom"))

        func.register_tool_macro("lane1", "T0", "")

        assert func.logger.messages == [
            ("error", "Error trying to map lane lane1 to T0, please make sure there "
                      "are no macros already setup for T0")
        ]

    def test_exception_when_renaming_logs_error(self):
        func = _make_func()
        _wire_change_tool(func)
        func._rename = MagicMock(side_effect=Exception("boom"))

        func.register_tool_macro("lane1", "T0", "_T0")

        assert func.logger.messages == [
            ("error", "Error trying to map lane lane1 to T0, please make sure there "
                      "are no macros already setup for T0")
        ]

# ── end register_tool_macro ───────────────────────────────────────────────────

# ── TcmdAssign ───────────────────────────────────────────────────────────────

class TestTcmdAssign:
    def test_use_rename_true_when_own_map_manually_assigned(self):
        func = _make_func()
        cur_lane = _make_afc_lane("AFC_stepper lane1")
        cur_lane.map = ["T0"]
        cur_lane._map = ["T0"]
        func.afc.lanes = {"lane1": cur_lane}
        func.afc.force_assign_map = False
        func.register_tool_macro = MagicMock()

        func.TcmdAssign(cur_lane)

        func.register_tool_macro.assert_called_once_with("lane1", "T0", "_T0")

    def test_use_rename_true_when_force_assign_map(self):
        func = _make_func()
        cur_lane = _make_afc_lane("AFC_stepper lane1")
        cur_lane.map = ["T3"]
        cur_lane._map = []
        func.afc.lanes = {"lane1": cur_lane}
        func.afc.force_assign_map = True
        func.register_tool_macro = MagicMock()

        func.TcmdAssign(cur_lane)

        func.register_tool_macro.assert_called_once_with("lane1", "T3", "_T3")

    def test_use_rename_true_when_map_manually_assigned_on_other_lane(self):
        """cur_lane's own _map is empty, but the T(n) command it currently
        holds was manually assigned to a different lane in the config."""
        func = _make_func()
        cur_lane = _make_afc_lane("AFC_stepper lane1")
        cur_lane.map = ["T5"]
        cur_lane._map = []
        other_lane = _make_afc_lane("AFC_stepper lane2")
        other_lane.map = ["T1"]
        other_lane._map = ["T5"]
        func.afc.lanes = {"lane1": cur_lane, "lane2": other_lane}
        func.afc.force_assign_map = False
        func.register_tool_macro = MagicMock()

        func.TcmdAssign(cur_lane)

        func.register_tool_macro.assert_called_once_with("lane1", "T5", "_T5")

    def test_use_rename_false_when_no_manual_mapping_anywhere(self):
        func = _make_func()
        cur_lane = _make_afc_lane("AFC_stepper lane1")
        cur_lane.map = ["T3"]
        cur_lane._map = []
        other_lane = _make_afc_lane("AFC_stepper lane2")
        other_lane.map = ["T1"]
        other_lane._map = []
        func.afc.lanes = {"lane1": cur_lane, "lane2": other_lane}
        func.afc.force_assign_map = False
        func.register_tool_macro = MagicMock()

        func.TcmdAssign(cur_lane)

        func.register_tool_macro.assert_called_once_with("lane1", "T3", "")

    def test_auto_assigns_first_available_command(self):
        func = _make_func()
        cur_lane = _make_afc_lane("AFC_stepper lane1")
        cur_lane.map = []
        cur_lane._map = []
        cur_lane.current_map = ""
        func.afc.lanes = {"lane1": cur_lane}
        func.afc.tool_cmds = {}
        func.afc.force_assign_map = False
        func.afc.gcode.ready_gcode_handlers = {}
        func.register_tool_macro = MagicMock()

        func.TcmdAssign(cur_lane)

        assert cur_lane.map == ["T0"]
        assert cur_lane.current_map == "T0"

    def test_auto_assign_skips_command_manually_assigned_to_another_lane(self):
        func = _make_func()
        cur_lane = _make_afc_lane("AFC_stepper lane1")
        cur_lane.map = []
        cur_lane._map = []
        other_lane = _make_afc_lane("AFC_stepper lane2")
        other_lane.map = ["T0"]
        other_lane._map = ["T0"]
        func.afc.lanes = {"lane1": cur_lane, "lane2": other_lane}
        func.afc.tool_cmds = {}
        func.afc.force_assign_map = False
        func.afc.gcode.ready_gcode_handlers = {}
        func.register_tool_macro = MagicMock()

        func.TcmdAssign(cur_lane)

        assert cur_lane.map == ["T1"]

    def test_auto_assign_skips_command_already_in_tool_cmds(self):
        func = _make_func()
        cur_lane = _make_afc_lane("AFC_stepper lane1")
        cur_lane.map = []
        cur_lane._map = []
        func.afc.lanes = {"lane1": cur_lane}
        func.afc.tool_cmds = {"T0": "lane2"}
        func.afc.force_assign_map = False
        func.afc.gcode.ready_gcode_handlers = {}
        func.register_tool_macro = MagicMock()

        func.TcmdAssign(cur_lane)

        assert cur_lane.map == ["T1"]

    def test_auto_assign_skips_command_with_existing_gcode_macro(self):
        """When a candidate T(n) already has a registered gcode handler and
        force_assign_map is off, TcmdAssign moves on without claiming it, and
        current_map ends up as the command the lane actually owns."""
        func = _make_func()
        cur_lane = _make_afc_lane("AFC_stepper lane1")
        cur_lane.map = []
        cur_lane._map = []
        cur_lane.current_map = ""
        func.afc.lanes = {"lane1": cur_lane}
        func.afc.tool_cmds = {}
        func.afc.force_assign_map = False
        func.afc.gcode.ready_gcode_handlers = {"T0": MagicMock()}
        func.register_tool_macro = MagicMock()

        func.TcmdAssign(cur_lane)

        assert cur_lane.map == ["T1"]
        assert cur_lane.current_map == "T1"

    def test_auto_assign_force_assign_map_ignores_existing_gcode_macro(self):
        """force_assign_map short-circuits the existing-macro check, so the
        first candidate is claimed even though a handler is already registered."""
        func = _make_func()
        cur_lane = _make_afc_lane("AFC_stepper lane1")
        cur_lane.map = []
        cur_lane._map = []
        func.afc.lanes = {"lane1": cur_lane}
        func.afc.tool_cmds = {}
        func.afc.force_assign_map = True
        func.afc.gcode.ready_gcode_handlers = {"T0": MagicMock()}
        func.register_tool_macro = MagicMock()

        func.TcmdAssign(cur_lane)

        assert cur_lane.map == ["T0"]

    def test_auto_assign_second_existing_macro_hit_keeps_current_map_unset(self):
        """Covers the loop continuing past two already-claimed candidates
        without setting current_map to either, since neither is owned by
        this lane; current_map ends up as the real assignment, T2."""
        func = _make_func()
        cur_lane = _make_afc_lane("AFC_stepper lane1")
        cur_lane.map = []
        cur_lane._map = []
        cur_lane.current_map = ""
        func.afc.lanes = {"lane1": cur_lane}
        func.afc.tool_cmds = {}
        func.afc.force_assign_map = False
        func.afc.gcode.ready_gcode_handlers = {"T0": MagicMock(), "T1": MagicMock()}
        func.register_tool_macro = MagicMock()

        func.TcmdAssign(cur_lane)

        assert cur_lane.map == ["T2"]
        assert cur_lane.current_map == "T2"

    def test_auto_assign_exhausts_range_without_finding_command(self):
        """Covers the range(99) loop finishing without a break when every
        candidate command is already claimed in tool_cmds."""
        func = _make_func()
        cur_lane = _make_afc_lane("AFC_stepper lane1")
        cur_lane.map = []
        cur_lane._map = []
        func.afc.lanes = {"lane1": cur_lane}
        func.afc.tool_cmds = {f"T{i}": "otherlane" for i in range(99)}
        func.afc.force_assign_map = False
        func.afc.gcode.ready_gcode_handlers = {}
        func.register_tool_macro = MagicMock()

        func.TcmdAssign(cur_lane)

        assert cur_lane.map == []
        func.register_tool_macro.assert_not_called()

    def test_skips_none_placeholder_already_in_map(self):
        """cur_lane.map already holding a "NONE" placeholder alongside a real
        mapping should skip NONE and only register the real command."""
        func = _make_func()
        cur_lane = _make_afc_lane("AFC_stepper lane1")
        cur_lane.map = ["NONE", "T1"]
        cur_lane._map = []
        cur_lane.current_map = ""
        func.afc.lanes = {"lane1": cur_lane}
        func.afc.tool_cmds = {}
        func.afc.force_assign_map = False
        func.register_tool_macro = MagicMock()

        func.TcmdAssign(cur_lane)

        assert "NONE" not in func.afc.tool_cmds
        assert func.afc.tool_cmds == {"T1": "lane1"}
        func.register_tool_macro.assert_called_once_with("lane1", "T1", "")

# ── end TcmdAssign ─────────────────────────────────────────────────────────────

class TestLaneCalibration:
    """Covers afcFunction._lane_calibration directly.

    Regression coverage for AFCProject/AFC-Klipper-Add-On#883 review comment:
    a lane that is merely skipped (standalone, not loaded, or no lane/unit
    matched at all) must not be reported as a calibration failure, since the
    caller (cmd_CALIBRATE_AFC) treats checked=False as "a fail prompt was
    already shown, stop here" and aborts any requested Bowden/TD-1
    calibration on that signal alone.
    """

    def _make_func_and_lane(self, standalone=False, load_state=True, prep_state=True):
        func = _make_real_func()
        func._afc_cali_fail = MagicMock()
        afc = _make_afc()
        afc.error = MagicMock()
        lane = _make_afc_lane()
        lane.is_direct_hub = MagicMock()
        lane.hub_obj = MagicMock()
        lane.hub_obj.is_virtual_pin = MagicMock(return_value=False)
        lane.extruder_obj.is_standalone = MagicMock(return_value=standalone)
        lane._load_state = load_state
        lane.prep_state = prep_state
        afc.lanes[lane.name] = lane
        func.afc = afc
        return func, lane

    def test_lane_not_found_returns_checked_true_with_empty_results(self):
        """No lane/unit matched the request at all; nothing failed."""
        func, lane = self._make_func_and_lane()

        result = func._lane_calibration("does_not_exist", None, 5, 25, MagicMock())

        assert result == (True, [], [])
        assert func.logger.messages == [
            ("info", "Starting AFC distance Calibrations"),
            ("info", "Lane calibration Done!"),
        ]
        func._afc_cali_fail.assert_not_called()

    def test_standalone_lane_is_skipped_and_returns_checked_true(self):
        func, lane = self._make_func_and_lane(standalone=True)

        result = func._lane_calibration(lane.name, None, 5, 25, MagicMock())

        assert result == (True, [], [])
        assert func.logger.messages == [
            ("info", "Starting AFC distance Calibrations"),
            ("info", f"{lane.name} is a standalone lane, skipping calibration"),
            ("info", "Lane calibration Done!"),
        ]
        func._afc_cali_fail.assert_not_called()
        lane.is_direct_hub.assert_not_called()

    def test_lane_not_loaded_is_skipped_and_returns_checked_true(self):
        func, lane = self._make_func_and_lane(load_state=False, prep_state=True)

        result = func._lane_calibration(lane.name, None, 5, 25, MagicMock())

        assert result == (True, [], [])
        assert func.logger.messages == [
            ("info", "Starting AFC distance Calibrations"),
            ("info", f"{lane.name} not loaded skipping to next loaded lane"),
            ("info", "Lane calibration Done!"),
        ]
        func._afc_cali_fail.assert_not_called()
        lane.is_direct_hub.assert_not_called()

    def test_lane_not_prepped_is_skipped_and_returns_checked_true(self):
        """prep_state alone (load_state True) must also trigger the skip,
        independent of load_state."""
        func, lane = self._make_func_and_lane(load_state=True, prep_state=False)

        result = func._lane_calibration(lane.name, None, 5, 25, MagicMock())

        assert result == (True, [], [])
        lane.is_direct_hub.assert_not_called()

    def test_lane_calibration_failure_returns_checked_false(self):
        func, lane = self._make_func_and_lane()
        lane.is_direct_hub.return_value = False
        lane.unit_obj.calibrate_lane = MagicMock(return_value=(False, "bad reading", 12))
        lane.hub_obj.state = True
        gcmd = MagicMock()

        result = func._lane_calibration(lane.name, None, 5, 25, gcmd)

        assert result == (False, [], [])
        func.afc.error.AFC_error.assert_called_once_with("bad reading", False, gcmd=gcmd)
        func._afc_cali_fail.assert_called_once_with(
            cali=lane.name, dis=12, reset_lane=True, fail_message="bad reading", gcmd=gcmd)

    def test_lane_calibration_success_returns_checked_true_with_calibrated_lane(self):
        func, lane = self._make_func_and_lane()
        lane.is_direct_hub.return_value = False
        lane.unit_obj.calibrate_lane = MagicMock(return_value=(True, "ok", 0))

        result = func._lane_calibration(lane.name, None, 5, 25, MagicMock())

        assert result == (True, [lane.name], [])
        assert func.logger.messages == [
            ("info", "Starting AFC distance Calibrations"),
            ("info", "Lane calibration Done!"),
        ]
        func._afc_cali_fail.assert_not_called()

    def test_resets_drive_stepper_before_calibrating_lane(self):
        """Not just that reset ran, but that it ran before calibrate_lane,
        not merely alongside or after it."""
        func, lane = self._make_func_and_lane()
        lane.is_direct_hub.return_value = False
        call_order = []
        lane.reset_drive_stepper = MagicMock(
            side_effect=lambda: call_order.append("reset") or 22.6789)
        lane.unit_obj.calibrate_lane = MagicMock(
            side_effect=lambda *a, **k: call_order.append("calibrate") or (True, "ok", 0))

        func._lane_calibration(lane.name, None, 5, 25, MagicMock())

        assert call_order == ["reset", "calibrate"]
        assert func.logger.messages == [
            ("info", "Starting AFC distance Calibrations"),
            ("info", "Lane calibration Done!"),
        ]

    def test_restores_drive_stepper_after_calibrating_lane(self):
        """Not just that restore ran with the reset value, but that it ran
        after calibrate_lane, not before it."""
        func, lane = self._make_func_and_lane()
        lane.is_direct_hub.return_value = False
        call_order = []
        lane.reset_drive_stepper = MagicMock(return_value=22.6789)
        lane.unit_obj.calibrate_lane = MagicMock(
            side_effect=lambda *a, **k: call_order.append("calibrate") or (True, "ok", 0))
        lane.restore_drive_stepper = MagicMock(
            side_effect=lambda v: call_order.append(("restore", v)))

        func._lane_calibration(lane.name, None, 5, 25, MagicMock())

        assert call_order == ["calibrate", ("restore", 22.6789)]
        assert func.logger.messages == [
            ("info", "Starting AFC distance Calibrations"),
            ("info", "Lane calibration Done!"),
        ]

    def test_restores_drive_stepper_even_when_calibration_fails(self):
        """The restore must happen on every exit path out of the per-lane
        calibration, including the early return on a failed calibration, not
        just the success path, and must still run after calibrate_lane."""
        func, lane = self._make_func_and_lane()
        lane.is_direct_hub.return_value = False
        lane.hub_obj.state = True
        call_order = []
        lane.reset_drive_stepper = MagicMock(return_value=22.6789)
        lane.unit_obj.calibrate_lane = MagicMock(
            side_effect=lambda *a, **k: call_order.append("calibrate")
            or (False, "bad reading", 12))
        lane.restore_drive_stepper = MagicMock(
            side_effect=lambda v: call_order.append(("restore", v)))

        func._lane_calibration(lane.name, None, 5, 25, MagicMock())

        assert call_order == ["calibrate", ("restore", 22.6789)]
        assert func.logger.messages == [("info", "Starting AFC distance Calibrations")]

# ── end _lane_calibration ─────────────────────────────────────────────────────

class TestCalibrateAFC:
    # ------------------------------------------------------------------
    # Fixtures / builders
    # ------------------------------------------------------------------

    def _make_afc_lane_for_calibration(self, standalone=False):
        afc = _make_afc()
        afc.error = MagicMock()
        lane = _make_afc_lane()
        lane.is_direct_hub = MagicMock()
        lane.hub_obj = MagicMock()

        afc.function.get_current_lane = MagicMock(return_value=None)
        lane.extruder_obj.is_standalone = MagicMock(return_value=standalone)
        lane.extruder_obj.name = "extruder"

        afc.lanes[lane.name] = lane

        return afc, lane

    def _make_gcmd_for_calibration(self, distance=25, tolerance=5, bowden=None, lane=None,
                                   unit=None, TD1=None):
        from tests.conftest import MockGCodeCommand
        return MockGCodeCommand(params={
            "BOWDEN": bowden,
            "LANE": lane,
            "UNIT": unit,
            "TD1": TD1,
            "DISTANCE": distance,
            "TOLERANCE": tolerance,
        })

    def _setup_calibration(self, standalone=False, td1_present=False, moonraker=None,
                           real_func=False):
        """Builds func/afc/lane with the defaults nearly every test relies on.

        :param real_func: Build `func` through its real `__init__` instead of
            `_make_func()`'s `__new__` bypass, for tests that care about
            constructor-initialized state.
        """
        func = _make_real_func() if real_func else _make_func()
        func._lane_calibration = MagicMock()
        func._afc_cali_comp = MagicMock()
        func._afc_cali_fail = MagicMock()
        afc, lane = self._make_afc_lane_for_calibration(standalone=standalone)
        func.afc = afc
        func.afc._td1_present = td1_present
        func.afc.moonraker = moonraker
        return func, afc, lane

    # ------------------------------------------------------------------
    # Action helper
    # ------------------------------------------------------------------

    def _call_cmd_calibrate_afc(self, func, gcmd):
        """Runs cmd_CALIBRATE_AFC and verifies the AFCprompt wrapper, which
        every test does regardless of outcome."""
        with patch("extras.AFC_functions.AFCprompt") as mocked_afc_prompt:
            func.cmd_CALIBRATE_AFC(gcmd)
        mocked_afc_prompt.assert_called_once_with(gcmd, func.logger)
        mocked_afc_prompt.return_value.p_end.assert_called_once()
        return mocked_afc_prompt

    # ------------------------------------------------------------------
    # Logger assertions
    # ------------------------------------------------------------------

    def _get_messages(self, func, level):
        return [m for lvl, m in func.logger.messages if lvl == level]

    def _assert_info_contains(self, func, substring):
        assert any(substring in m for m in self._get_messages(func, "info"))

    def _assert_info_not_contains(self, func, substring):
        assert not any(substring in m for m in self._get_messages(func, "info"))

    def _assert_error_contains(self, func, substring):
        assert any(substring in m for m in self._get_messages(func, "error"))

    def _assert_afc_error(self, func, message, gcmd=ANY):
        func.afc.error.AFC_error.assert_called_once_with(message, pause=False, gcmd=gcmd)

    # ------------------------------------------------------------------
    # _lane_calibration helpers
    # ------------------------------------------------------------------

    def _set_lane_calibration_result(self, func, result):
        func._lane_calibration = MagicMock(return_value=deepcopy(result))

    def _assert_lane_calibration_called(self, func, lane_name, bowden=None, tolerance=5,
                                        distance=25, gcmd=None):
        func._lane_calibration.assert_called_once_with(lane_name, bowden, tolerance, distance, gcmd)

    def _assert_lane_calibration_not_called(self, func):
        func._lane_calibration.assert_not_called()

    # ------------------------------------------------------------------
    # Bowden / direct-hub helpers
    # ------------------------------------------------------------------

    def _set_calibrate_bowden_result(self, lane, result):
        lane.unit_obj.calibrate_bowden = MagicMock(return_value=result)

    def _assert_calibrate_bowden_called(self, lane, distance=25, tolerance=5, gcmd=None):
        lane.unit_obj.calibrate_bowden.assert_called_once_with(
            lane, distance, tolerance, gcmd=gcmd)

    def _assert_calibrate_bowden_not_called(self, lane):
        lane.unit_obj.calibrate_bowden.assert_not_called()

    def _assert_calibrate_td1_not_called(self, lane):
        lane.unit_obj.calibrate_td1.assert_not_called()

    def _assert_bowden_calibration_not_attempted(self, lane):
        """Neither the standard nor the direct-hub bowden path ran."""
        self._assert_calibrate_bowden_not_called(lane)
        self._assert_calibrate_td1_not_called(lane)

    def _assert_no_calibration_attempted(self, func, lane):
        """Calibration was aborted before any lane/bowden work happened."""
        self._assert_lane_calibration_not_called(func)
        self._assert_bowden_calibration_not_attempted(lane)

    def _assert_select_unselect_tool_called(self, func, lane):
        expected = [
            call(f"AFC_SELECT_TOOL TOOL={lane.extruder_obj.name}"),
            call("AFC_UNSELECT_TOOL")
        ]
        assert func.afc.gcode.run_script_from_command.call_args_list == expected

    def _assert_afc_cali_comp_called(self, func, lanes, messages, title="AFC Calibration",
                                     gcmd=ANY):
        func._afc_cali_comp.assert_called_once_with(", ".join(lanes),
            title,
            " ".join(messages),
            gcmd=gcmd
        )

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_calibration_current_lane(self):
        func, afc, lane = self._setup_calibration()
        gcmd = self._make_gcmd_for_calibration()
        afc.function.get_current_lane = MagicMock(return_value=lane.name)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_info_contains(func, f"current afc {lane.name} is not None, cannot calibrate")
        self._assert_error_contains(func, "Tool must be unloaded to calibrate system")

    def test_calibration_current_none_lane_none(self):
        func, afc, lane = self._setup_calibration()
        gcmd = self._make_gcmd_for_calibration()

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_info_contains(func, "No lanes selected to calibrate dist_hub")
        self._assert_no_calibration_attempted(func, lane)

    def test_calibration_lane_not_in_afc(self):
        func, afc, lane = self._setup_calibration()
        lane_name = "lane2"
        gcmd = self._make_gcmd_for_calibration(lane=lane_name)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, f"'{lane_name}' is not a valid lane", gcmd=gcmd)
        self._assert_no_calibration_attempted(func, lane)

    def test_calibration_lane_is_standalone(self):
        func, afc, lane = self._setup_calibration(standalone=True)
        gcmd = self._make_gcmd_for_calibration(lane=lane.name)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, f"{lane.name} is a standalone lane, cannot calibrate",
                              gcmd=gcmd)
        self._assert_no_calibration_attempted(func, lane)

    def test_calibration_unit_not_valid(self):
        func, afc, lane = self._setup_calibration()
        unit_name = "test"
        gcmd = self._make_gcmd_for_calibration(lane=lane.name, unit=unit_name)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, f"'{unit_name}' is not a valid unit", gcmd=gcmd)
        self._assert_no_calibration_attempted(func, lane)

    def test_calibration_bowden_no_in_lanes(self):
        func, afc, lane = self._setup_calibration()
        lane_name = "lane2"
        gcmd = self._make_gcmd_for_calibration(bowden=lane_name)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, f"'{lane_name}' is not a valid lane to calibrate bowden length", gcmd=gcmd)
        self._assert_no_calibration_attempted(func, lane)

    def test_calibration_td1_not_present(self):
        func, afc, lane = self._setup_calibration()
        gcmd = self._make_gcmd_for_calibration(TD1="lane2")

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, "TD-1 is not present, will not be able to calibrate bowden length for TD-1", gcmd=gcmd)
        self._assert_no_calibration_attempted(func, lane)
    
    def test_calibration_td1_lane_not_valid(self):
        func, afc, lane = self._setup_calibration(td1_present=True)
        gcmd = self._make_gcmd_for_calibration(TD1="lane2")

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(
            func, "'lane2' is not a valid lane to calibrate TD-1 bowden length", gcmd=gcmd)
        self._assert_no_calibration_attempted(func, lane)

    def test_calibration_lane_valid_success(self):
        func, afc, lane = self._setup_calibration()
        lane_name = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        self._assert_afc_cali_comp_called(func, lane_calibration_call[1], lane_calibration_call[2],
                                  gcmd=gcmd)
        self._assert_bowden_calibration_not_attempted(lane)

    def test_calibration_lane_valid_success_no_additional_msg(self):
        func, afc, lane = self._setup_calibration()
        lane_name = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name)
        lane_calibration_call = (True, [lane_name], [])
        self._set_lane_calibration_result(func, lane_calibration_call)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        self._assert_afc_cali_comp_called(func, lane_calibration_call[1], [], gcmd=gcmd)
        self._assert_bowden_calibration_not_attempted(lane)

    def test_calibration_lane_valid_checked_false(self):
        func, afc, lane = self._setup_calibration()
        lane_name = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name)
        lane_calibration_call = (False, [lane_name], [])
        self._set_lane_calibration_result(func, lane_calibration_call)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        func._afc_cali_comp.assert_not_called()
        self._assert_bowden_calibration_not_attempted(lane)

    def test_calibration_lane_checked_false_skips_requested_bowden(self):
        """Regression coverage for AFCProject/AFC-Klipper-Add-On#747: a failed lane
        calibration must stop the macro instead of still running the requested Bowden
        calibration, which could overwrite the failure with a misleading success popup."""
        func, afc, lane = self._setup_calibration()
        lane_name = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=lane_name)
        self._set_lane_calibration_result(func, (False, [], []))
        self._set_calibrate_bowden_result(lane, (True, "afc_bowden_length successful", 1000))

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        self._assert_bowden_calibration_not_attempted(lane)
        func._afc_cali_comp.assert_not_called()

    def test_calibration_lane_checked_false_skips_requested_td1(self):
        """Same regression as above, but for a failed lane calibration requested
        alongside TD-1 calibration."""
        func, afc, lane = self._setup_calibration(td1_present=True)
        lane_name = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, TD1=lane_name)
        self._set_lane_calibration_result(func, (False, [], []))
        lane.unit_obj.calibrate_td1 = MagicMock(return_value=(True, "td1_bowden_length calibration successful", 500))

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        self._assert_calibrate_td1_not_called(lane)
        func._afc_cali_comp.assert_not_called()

    def test_calibration_lane_merely_skipped_still_runs_requested_bowden(self):
        """Regression coverage for the review comment on AFCProject/AFC-Klipper-Add-On#883:
        unlike the checked=False tests above, a LANE that is simply unloaded (not a
        calibration failure) must not abort the requested BOWDEN calibration. This exercises
        the real _lane_calibration (not mocked), since the bug was in that method returning
        checked=False for a skip with no failure ever reported."""
        func, afc, lane1 = self._setup_calibration()
        lane2 = _make_afc_lane("AFC_stepper lane2")
        lane2.extruder_obj.is_standalone = MagicMock(return_value=False)
        lane2._load_state = False
        lane2.prep_state = False
        afc.lanes[lane2.name] = lane2
        del func._lane_calibration  # use the real method instead of the mock from _setup_calibration
        gcmd = self._make_gcmd_for_calibration(lane=lane2.name, bowden=lane1.name)
        self._set_calibrate_bowden_result(lane1, (True, "afc_bowden_length successful", 1000))

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_calibrate_bowden_called(lane1, gcmd=gcmd)
        func._afc_cali_comp.assert_called_once_with(
            "Bowden_length: lane1", "AFC Calibration", "", gcmd=gcmd)

    def test_calibration_all_lanes_valid_success(self):
        func, afc, lane = self._setup_calibration()
        lane_name = "all"
        gcmd = self._make_gcmd_for_calibration(lane=lane_name)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        self._assert_afc_cali_comp_called(func, lane_calibration_call[1], lane_calibration_call[2],
                                  gcmd=gcmd)
        self._assert_bowden_calibration_not_attempted(lane)

    def test_calibration_lanes_valid_bowden_fail_tool_start_and_tool_end_none(self):
        """Regression coverage: a failed Bowden tool_start/tool_end check must stop
        the macro instead of still showing the completion popup left over from a
        prior successful LANE calibration (same root cause as #747)."""
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = None
        lane.extruder_obj.tool_end = None
        lane.extruder_obj.fila_tool_start = None
        lane_name = "all"
        bowden_lane = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)
        bowden_error = (
            "\nBowden Calibration Error:"
             "\nCannot calibrate with only post extruder sensor and no turtleneck buffer defined in config"
        )

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        func._afc_cali_comp.assert_not_called()
        self._assert_bowden_calibration_not_attempted(lane)
        self._assert_afc_error(func, bowden_error, gcmd=gcmd)

    def test_calibration_lanes_valid_bowden_success_tool_start_none_tool_end_buffer_set_lane(self):
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = None
        lane.extruder_obj.tool_end = MagicMock(return_value="Turtle_1:SW1")
        lane.extruder_obj.fila_tool_start = None
        lane.buffer_obj = "Test"
        lane_name = "all"
        bowden_lane = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        calibrated_msg = [lane_calibration_call[1][0], f"Bowden_length: {bowden_lane}"]
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (True, "afc_bowden_length successful", 1000))

        self._call_cmd_calibrate_afc(func, gcmd)

        assert lane.extruder_obj.tool_start is None
        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        self._assert_calibrate_bowden_called(lane, gcmd=gcmd)
        self._assert_afc_cali_comp_called(func, calibrated_msg, lane_calibration_call[2],
                                  gcmd=gcmd)
        self._assert_info_contains(func, "Cannot run calibration using post extruder sensor, using buffer to calibrate bowden length")

    def test_calibration_lanes_valid_bowden_success_u1_sensor_needs_no_buffer(self):
        """A U1-style motion sensor (u1_filament_sensor_name) already gives
        get_toolhead_pre_sensor_state() something real to confirm against, so
        calibration must pass without tool_end/buffer_obj and without the
        tool_start='buffer' override the tool_end+buffer path uses."""
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = None
        lane.extruder_obj.tool_end = None
        lane.extruder_obj.fila_tool_start = MagicMock()
        lane.buffer_obj = None
        lane_name = "all"
        bowden_lane = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        calibrated_msg = [lane_calibration_call[1][0], f"Bowden_length: {bowden_lane}"]
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (True, "afc_bowden_length successful", 1000))

        self._call_cmd_calibrate_afc(func, gcmd)

        # No "using buffer to calibrate" override: tool_start stays None.
        assert lane.extruder_obj.tool_start is None
        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        self._assert_calibrate_bowden_called(lane, gcmd=gcmd)
        self._assert_afc_cali_comp_called(func, calibrated_msg, lane_calibration_call[2],
                                  gcmd=gcmd)
        self._assert_info_contains(func, "Starting AFC distance Calibrations")
        self._assert_info_contains(func, "Bowden length calibration Done!")
        self._assert_calibrate_td1_not_called(lane)

    def test_calibration_lanes_valid_bowden_success_tool_start_buffer(self):
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane_name = "all"
        bowden_lane = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        calibrated_msg = [lane_calibration_call[1][0], f"Bowden_length: {bowden_lane}"]
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (True, "afc_bowden_length successful", 1000))

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        self._assert_calibrate_bowden_called(lane, gcmd=gcmd)
        self._assert_afc_cali_comp_called(func, calibrated_msg, lane_calibration_call[2],
                                  gcmd=gcmd)
        self._assert_info_not_contains(func, "Cannot run calibration using post extruder sensor, using buffer to calibrate bowden length")
        self._assert_info_contains(func, "Starting AFC distance Calibrations")
        self._assert_info_contains(func, "Bowden length calibration Done!")
        self._assert_calibrate_td1_not_called(lane)

    def test_calibration_lanes_valid_bowden_fail_tool_start_buffer(self):
        """pos=0 here covers the buffer-already-extended failure, where filament is
        still sitting at the hub (hub_obj.state True): the reset button must use
        hub_obj.state rather than pos!=0, and dis must be None rather than the
        invalid DISTANCE=0 AFC_LANE_RESET would reject."""
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.hub_obj.state = True
        lane_name = "all"
        bowden_lane = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        fail_msg = f"{bowden_lane} failed to calibrate bowden length Bowden Calibration Failed"
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (False, "Bowden Calibration Failed", 0))

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        self._assert_calibrate_bowden_called(lane, gcmd=gcmd)
        self._assert_afc_error(func, fail_msg, gcmd=gcmd)
        func._afc_cali_fail.assert_called_once_with(
            cali=lane.name, dis=None, reset_lane=True, title= "AFC Bowden Calibration Failed",
            fail_message=fail_msg, gcmd=gcmd
        )
        func._afc_cali_comp.assert_not_called()
        self._assert_info_not_contains(func, "Cannot run calibration using post extruder sensor, using buffer to calibrate bowden length")
        self._assert_info_contains(func, "Starting AFC distance Calibrations")
        self._assert_info_not_contains(func, "Bowden length calibration Done!")
        self._assert_calibrate_td1_not_called(lane)

    def test_calibration_lanes_valid_bowden_fail_tool_start_buffer_hub_cleared(self):
        """Same pos=0 failure, but hub_obj.state is False (hub already clear): no
        reset button should be offered since there is nothing at the hub to reset."""
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.hub_obj.state = False
        lane_name = "all"
        bowden_lane = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        fail_msg = f"{bowden_lane} failed to calibrate bowden length Bowden Calibration Failed"
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (False, "Bowden Calibration Failed", 0))

        self._call_cmd_calibrate_afc(func, gcmd)

        func._afc_cali_fail.assert_called_once_with(
            cali=lane.name, dis=None, reset_lane=False, title= "AFC Bowden Calibration Failed",
            fail_message=fail_msg, gcmd=gcmd
        )
        func._afc_cali_comp.assert_not_called()

    def test_calibration_lanes_valid_bowden_fail_nonzero_pos_hub_cleared(self):
        """A non-zero pos failure with the hub already clear (filament moved past
        the hub toward the toolhead): reset_lane now follows hub_obj.state, not
        pos!=0, so no reset button should be offered here."""
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.hub_obj.state = False
        lane_name = "all"
        bowden_lane = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        fail_msg = f"{bowden_lane} failed to calibrate bowden length Bowden Calibration Failed"
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (False, "Bowden Calibration Failed", 1000))

        self._call_cmd_calibrate_afc(func, gcmd)

        func._afc_cali_fail.assert_called_once_with(
            cali=lane.name, dis=1000, reset_lane=False, title= "AFC Bowden Calibration Failed",
            fail_message=fail_msg, gcmd=gcmd
        )
        func._afc_cali_comp.assert_not_called()

    def test_calibration_lanes_valid_bowden_fail_nonzero_pos_hub_triggered(self):
        """A non-zero pos failure with the hub still triggered: reset_lane should
        be True with the real distance passed through unchanged."""
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.hub_obj.state = True
        lane_name = "all"
        bowden_lane = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        fail_msg = f"{bowden_lane} failed to calibrate bowden length Bowden Calibration Failed"
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (False, "Bowden Calibration Failed", 1000))

        self._call_cmd_calibrate_afc(func, gcmd)

        func._afc_cali_fail.assert_called_once_with(
            cali=lane.name, dis=1000, reset_lane=True, title= "AFC Bowden Calibration Failed",
            fail_message=fail_msg, gcmd=gcmd
        )
        func._afc_cali_comp.assert_not_called()

    def test_calibration_lanes_valid_bowden_success_tool_start_buffer_snapmaker_no_park_pre_load(self):
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane_name = "all"
        bowden_lane = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        calibrated_msg = [lane_calibration_call[1][0], f"Bowden_length: {bowden_lane}"]
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (True, "afc_bowden_length successful", 1000))
        func.afc.snapmaker_printer = MagicMock(return_value=True)
        func.afc.park_pre_load = False
        func.check_homed = MagicMock()

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        self._assert_calibrate_bowden_called(lane, gcmd=gcmd)
        func.check_homed.assert_not_called()
        self._assert_afc_cali_comp_called(func, calibrated_msg, lane_calibration_call[2],
                                  gcmd=gcmd)
        self._assert_info_not_contains(func, "Cannot run calibration using post extruder sensor, using buffer to calibrate bowden length")
        self._assert_info_contains(func, "Starting AFC distance Calibrations")
        self._assert_info_contains(func, "Bowden length calibration Done!")
        self._assert_calibrate_td1_not_called(lane)

    def test_calibration_lanes_valid_bowden_success_tool_start_buffer_snapmaker_fail_check_home(self):
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane_name = "all"
        bowden_lane = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (True, "afc_bowden_length successful", 1000))
        func.afc.snapmaker_printer = MagicMock(return_value=True)
        func.afc.park_pre_load = True
        func.check_homed = MagicMock(return_value=False)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        func.check_homed.assert_called_once_with(
            "Printer needs to be homed before calibrating PTFE length to toolhead",
            gcmd=gcmd)
        self._assert_info_not_contains(func, "Starting AFC distance Calibrations")
        self._assert_bowden_calibration_not_attempted(lane)
        func._afc_cali_comp.assert_not_called()

    def test_calibration_lanes_valid_bowden_success_tool_start_buffer_snapmaker_homed_selected_tool_none_fail(self):
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane_name = "all"
        bowden_lane = lane.name
        func.get_current_extruder_obj = MagicMock(return_value=None)
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (True, "afc_bowden_length successful", 1000))
        func.afc.snapmaker_printer = MagicMock(return_value=True)
        func.afc.park_pre_load = True
        func.check_homed = MagicMock(return_value=True)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        func.check_homed.assert_called_once_with(
            "Printer needs to be homed before calibrating PTFE length to toolhead",
            gcmd=gcmd)
        self._assert_select_unselect_tool_called(func, lane)
        self._assert_info_not_contains(func, "Starting AFC distance Calibrations")
        self._assert_afc_error(func, f"Failed to select tool '{lane.extruder_obj.name}' before Bowden calibration", gcmd=gcmd)
        self._assert_bowden_calibration_not_attempted(lane)
        func._afc_cali_comp.assert_not_called()

    def test_calibration_lanes_valid_bowden_success_tool_start_buffer_snapmaker_homed_fail_selected_tool_extruder1_fail(self):
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane_name = "all"
        bowden_lane = lane.name
        func.get_current_extruder_obj = MagicMock(return_value=MagicMock())
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (True, "afc_bowden_length successful", 1000))
        func.afc.snapmaker_printer = MagicMock(return_value=True)
        func.afc.park_pre_load = True
        func.check_homed = MagicMock(return_value=True)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        func.check_homed.assert_called_once_with(
            "Printer needs to be homed before calibrating PTFE length to toolhead",
            gcmd=gcmd)
        self._assert_select_unselect_tool_called(func, lane)
        self._assert_info_not_contains(func, "Starting AFC distance Calibrations")
        self._assert_afc_error(func, f"Failed to select tool '{lane.extruder_obj.name}' before Bowden calibration", gcmd=gcmd)
        self._assert_bowden_calibration_not_attempted(lane)
        func._afc_cali_comp.assert_not_called()

    def test_calibration_lanes_valid_bowden_success_tool_start_buffer_snapmaker_homed_fail_selected_tool_success_park_pre_load_cmd_none(self):
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane_name = "all"
        bowden_lane = lane.name
        func.get_current_extruder_obj = MagicMock(return_value=lane.extruder_obj)
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (True, "afc_bowden_length successful", 1000))
        func.afc.snapmaker_printer = MagicMock(return_value=True)
        func.afc.park_pre_load = True
        func.check_homed = MagicMock(return_value=True)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        func.check_homed.assert_called_once_with(
            "Printer needs to be homed before calibrating PTFE length to toolhead",
            gcmd=gcmd)
        self._assert_select_unselect_tool_called(func, lane)
        self._assert_info_contains(func, "Starting AFC distance Calibrations")
        func.afc.error.AFC_error.assert_not_called()
        lane.unit_obj.calibrate_bowden.assert_called_once()
        self._assert_calibrate_td1_not_called(lane)

    def test_calibration_lanes_valid_bowden_success_tool_start_buffer_snapmaker_homed_fail_selected_tool_success(self):
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane_name = "all"
        bowden_lane = lane.name
        func.get_current_extruder_obj = MagicMock(return_value=lane.extruder_obj)
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (True, "afc_bowden_length successful", 1000))
        func.afc.snapmaker_printer = MagicMock(return_value=True)
        func.afc.park_pre_load = True
        func.afc.park_pre_load_cmd = "AFC_PARK_PRE_LOAD"
        func.check_homed = MagicMock(return_value=True)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        func.check_homed.assert_called_once_with(
            "Printer needs to be homed before calibrating PTFE length to toolhead",
            gcmd=gcmd)
        expected_calls = [
            call(f"AFC_SELECT_TOOL TOOL={lane.extruder_obj.name}"),
            call("AFC_PARK_PRE_LOAD"),
            call("AFC_UNSELECT_TOOL")
        ]
        assert func.afc.gcode.run_script_from_command.call_args_list == expected_calls
        self._assert_info_contains(func, "Starting AFC distance Calibrations")
        func.afc.error.AFC_error.assert_not_called()
        lane.unit_obj.calibrate_bowden.assert_called_once()
        self._assert_calibrate_td1_not_called(lane)

    def test_calibration_td1_valid_direct_hub_loaded_toolhead(self):
        func, afc, lane = self._setup_calibration(td1_present=True)
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.is_direct_hub = MagicMock(return_value=True)
        lane.tool_loaded = True
        gcmd = self._make_gcmd_for_calibration(TD1=lane.name)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(
            func,
            f"{lane.name} loaded to toolhead, unload from toolhead before "
            "trying to calibrate td1_bowden_length."
        , gcmd=gcmd)
        self._assert_lane_calibration_not_called(func)
        self._assert_calibrate_bowden_not_called(lane)
        func._afc_cali_comp.assert_not_called()

    def test_calibration_td1_tool_loaded_fail_does_not_show_completion_after_lane_success(self):
        """Regression coverage: a successful LANE calibration followed by a failed
        TD-1 tool_loaded precheck must not leave `checked` True from the LANE step,
        which would otherwise still show the "calibration completed" popup."""
        func, afc, lane = self._setup_calibration(td1_present=True)
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.is_direct_hub = MagicMock(return_value=True)
        lane.tool_loaded = True
        lane_name = "all"
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, TD1=lane.name)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(
            func,
            f"{lane.name} loaded to toolhead, unload from toolhead before "
            "trying to calibrate td1_bowden_length."
        , gcmd=gcmd)
        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        self._assert_calibrate_td1_not_called(lane)
        func._afc_cali_comp.assert_not_called()

    def test_calibration_td1_valid_direct_hub_toolhead_not_loaded_hub_true(self):
        func, afc, lane = self._setup_calibration(td1_present=True)
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.is_direct_hub = MagicMock(return_value=True)
        lane.tool_loaded = False
        lane.hub_obj.state = True
        lane.hub_obj.name = "Hub_1"
        gcmd = self._make_gcmd_for_calibration(TD1=lane.name)

        error_message = (f"{lane.hub_obj.name} hub is triggered, make sure hub is clear "
                          "before trying to calibrate TD-1 bowden length")

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, error_message, gcmd=gcmd)
        func.afc.gcode.run_script_from_command.assert_called_once_with(
            f"AFC_CALI_FAIL TITLE='TD-1 Calibration Failed' FAIL={lane.name} DISTANCE=0 msg='{error_message}' RESET=0"
        )
        self._assert_lane_calibration_not_called(func)
        self._assert_calibrate_bowden_not_called(lane)
        func._afc_cali_comp.assert_not_called()

    def test_calibration_td1_hub_triggered_fail_does_not_show_completion_after_lane_success(self):
        """Same regression as above, but for the TD-1 hub-triggered precheck, which
        also fires its own AFC_CALI_FAIL popup; the stale `checked` must not add a
        contradictory completion popup on top of it."""
        func, afc, lane = self._setup_calibration(td1_present=True)
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.is_direct_hub = MagicMock(return_value=True)
        lane.tool_loaded = False
        lane.hub_obj.state = True
        lane.hub_obj.name = "Hub_1"
        lane_name = "all"
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, TD1=lane.name)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)

        error_message = (f"{lane.hub_obj.name} hub is triggered, make sure hub is clear "
                          "before trying to calibrate TD-1 bowden length")

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, error_message, gcmd=gcmd)
        func.afc.gcode.run_script_from_command.assert_called_once_with(
            f"AFC_CALI_FAIL TITLE='TD-1 Calibration Failed' FAIL={lane.name} DISTANCE=0 msg='{error_message}' RESET=0"
        )
        self._assert_lane_calibration_called(func, lane_name, gcmd=gcmd)
        self._assert_calibrate_td1_not_called(lane)
        func._afc_cali_comp.assert_not_called()

    def test_calibration_td1_valid_direct_hub_toolhead_not_loaded_hub_not_loaded_calibration_fail(self):
        func, afc, lane = self._setup_calibration(td1_present=True)
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.is_direct_hub = MagicMock(return_value=True)
        lane.tool_loaded = False
        lane.hub_obj.state = False
        lane.hub_obj.name = "Hub_1"
        gcmd = self._make_gcmd_for_calibration(TD1=lane.name)

        cal_td1_return = (False, "TD-1 failed to detect filament after moving 1000mm", 1000)
        lane.unit_obj.calibrate_td1 = MagicMock(return_value=cal_td1_return)
        error_message = f"{lane.name} failed to calibrate TD-1 bowden length, {cal_td1_return[1]}"

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, error_message, gcmd=gcmd)
        func.afc.gcode.run_script_from_command.assert_called_once_with(
            f"AFC_CALI_FAIL TITLE='TD-1 Calibration Failed' FAIL={lane.name} DISTANCE={cal_td1_return[2]} msg='{error_message}' RESET=1"
        )
        self._assert_lane_calibration_not_called(func)
        self._assert_calibrate_bowden_not_called(lane)
        func._afc_cali_comp.assert_not_called()

    def test_calibration_td1_valid_direct_hub_toolhead_not_loaded_hub_not_loaded_calibration_pass(self):
        func, afc, lane = self._setup_calibration(td1_present=True)
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.is_direct_hub = MagicMock(return_value=True)
        lane.tool_loaded = False
        lane.hub_obj.state = False
        lane.hub_obj.name = "Hub_1"
        gcmd = self._make_gcmd_for_calibration(TD1=lane.name)

        cal_td1_return = (True, "td1_bowden_length calibration successful", 1000)
        lane.unit_obj.calibrate_td1 = MagicMock(return_value=cal_td1_return)

        self._call_cmd_calibrate_afc(func, gcmd)

        func.afc.error.AFC_error.assert_not_called()
        self._assert_lane_calibration_not_called(func)
        self._assert_calibrate_bowden_not_called(lane)
        self._assert_afc_cali_comp_called(
            func, [f"'TD1_Bowden_length: {lane.name}'"], [], title="TD-1 Calibration", gcmd=gcmd
        )

    def test_resets_drive_stepper_before_bowden_calibration(self):
        """Not just that reset ran, but that it ran before calibrate_bowden,
        not merely alongside or after it."""
        func, afc, lane = self._setup_calibration(real_func=True)
        gcmd = self._make_gcmd_for_calibration(bowden=lane.name)
        call_order = []
        lane.unit_obj.calibrate_bowden = MagicMock(
            side_effect=lambda *a, **k: call_order.append("calibrate")
            or (True, "afc_bowden_length successful", 1000))
        lane.reset_drive_stepper = MagicMock(
            side_effect=lambda: call_order.append("reset") or 22.6789)

        self._call_cmd_calibrate_afc(func, gcmd)

        assert call_order == ["reset", "calibrate"]

    def test_restores_drive_stepper_after_bowden_calibration_success(self):
        """Not just that restore ran with the reset value, but that it ran
        after calibrate_bowden, not before it."""
        func, afc, lane = self._setup_calibration(real_func=True)
        gcmd = self._make_gcmd_for_calibration(bowden=lane.name)
        call_order = []
        lane.unit_obj.calibrate_bowden = MagicMock(
            side_effect=lambda *a, **k: call_order.append("calibrate")
            or (True, "afc_bowden_length successful", 1000))
        lane.reset_drive_stepper = MagicMock(return_value=22.6789)
        lane.restore_drive_stepper = MagicMock(
            side_effect=lambda v: call_order.append(("restore", v)))

        self._call_cmd_calibrate_afc(func, gcmd)

        assert call_order == ["calibrate", ("restore", 22.6789)]

    def test_restores_drive_stepper_even_when_bowden_calibration_fails(self):
        """The restore must still run after calibrate_bowden on the failure
        path, not just when calibration succeeds."""
        func, afc, lane = self._setup_calibration(real_func=True)
        gcmd = self._make_gcmd_for_calibration(bowden=lane.name)
        lane.hub_obj.state = True
        call_order = []
        lane.unit_obj.calibrate_bowden = MagicMock(
            side_effect=lambda *a, **k: call_order.append("calibrate")
            or (False, "bad reading", 12))
        lane.reset_drive_stepper = MagicMock(return_value=22.6789)
        lane.restore_drive_stepper = MagicMock(
            side_effect=lambda v: call_order.append(("restore", v)))

        self._call_cmd_calibrate_afc(func, gcmd)

        assert call_order == ["calibrate", ("restore", 22.6789)]

    def test_resets_drive_stepper_before_td1_calibration(self):
        """Not just that reset ran, but that it ran before calibrate_td1,
        not merely alongside or after it."""
        func, afc, lane = self._setup_calibration(td1_present=True, real_func=True)
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.is_direct_hub = MagicMock(return_value=True)
        lane.tool_loaded = False
        lane.hub_obj.state = False
        lane.hub_obj.name = "Hub_1"
        gcmd = self._make_gcmd_for_calibration(TD1=lane.name)
        call_order = []
        lane.unit_obj.calibrate_td1 = MagicMock(
            side_effect=lambda *a, **k: call_order.append("calibrate")
            or (True, "td1_bowden_length calibration successful", 1000))
        lane.reset_drive_stepper = MagicMock(
            side_effect=lambda: call_order.append("reset") or 22.6789)

        self._call_cmd_calibrate_afc(func, gcmd)

        assert call_order == ["reset", "calibrate"]

    def test_restores_drive_stepper_after_td1_calibration_success(self):
        """Not just that restore ran with the reset value, but that it ran
        after calibrate_td1, not before it."""
        func, afc, lane = self._setup_calibration(td1_present=True, real_func=True)
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.is_direct_hub = MagicMock(return_value=True)
        lane.tool_loaded = False
        lane.hub_obj.state = False
        lane.hub_obj.name = "Hub_1"
        gcmd = self._make_gcmd_for_calibration(TD1=lane.name)
        call_order = []
        lane.unit_obj.calibrate_td1 = MagicMock(
            side_effect=lambda *a, **k: call_order.append("calibrate")
            or (True, "td1_bowden_length calibration successful", 1000))
        lane.reset_drive_stepper = MagicMock(return_value=22.6789)
        lane.restore_drive_stepper = MagicMock(
            side_effect=lambda v: call_order.append(("restore", v)))

        self._call_cmd_calibrate_afc(func, gcmd)

        assert call_order == ["calibrate", ("restore", 22.6789)]

    def test_restores_drive_stepper_even_when_td1_calibration_fails(self):
        """The restore must still run after calibrate_td1 on the failure
        path, not just when calibration succeeds."""
        func, afc, lane = self._setup_calibration(td1_present=True, real_func=True)
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane.is_direct_hub = MagicMock(return_value=True)
        lane.tool_loaded = False
        lane.hub_obj.state = False
        lane.hub_obj.name = "Hub_1"
        gcmd = self._make_gcmd_for_calibration(TD1=lane.name)
        call_order = []
        lane.unit_obj.calibrate_td1 = MagicMock(
            side_effect=lambda *a, **k: call_order.append("calibrate")
            or (False, "TD-1 failed to detect filament after moving 1000mm", 1000))
        lane.reset_drive_stepper = MagicMock(return_value=22.6789)
        lane.restore_drive_stepper = MagicMock(
            side_effect=lambda v: call_order.append(("restore", v)))

        self._call_cmd_calibrate_afc(func, gcmd)

        assert call_order == ["calibrate", ("restore", 22.6789)]


# ── cmd_AFC_LANE_RESET: stepperless unit delegation ─────────────────────────
# New branch: units that expose get_lane_reset_command (e.g. OpenAMS) can't be
# retracted with a lane-stepper move loop, so the reset command is delegated
# to the unit instead of running the default move-to-hub retract loop.

class TestCmdAfcLaneResetUnitDelegation:
    def _make(self, distance=50):
        func = _make_func()
        afc, lane = self._make_afc_lane()
        func.afc = afc
        func.get_current_lane_obj = MagicMock(return_value=None)
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand(params={"LANE": lane.name, "DISTANCE": distance})
        return func, lane, gcmd

    def _make_afc_lane(self):
        afc = _make_afc()
        afc.error = MagicMock()
        lane = _make_afc_lane()
        lane.hub_obj = MagicMock()
        lane.hub_obj.state = True
        lane.short_move_dis = 10
        afc.lanes[lane.name] = lane
        return afc, lane

    def _run(self, func, gcmd):
        with patch("extras.AFC_functions.AFCprompt") as mocked_afc_prompt:
            func.cmd_AFC_LANE_RESET(gcmd)
        return mocked_afc_prompt

    def test_hook_present_returns_command_runs_it_and_skips_default_retract(self):
        func, lane, gcmd = self._make()
        lane.unit_obj = MagicMock(spec=["get_lane_reset_command"])
        lane.unit_obj.get_lane_reset_command.return_value = "OAMS_RETRACT LANE=lane1"

        self._run(func, gcmd)

        lane.unit_obj.get_lane_reset_command.assert_called_once_with(lane, 50.0)
        func.afc.gcode.run_script_from_command.assert_called_once_with("OAMS_RETRACT LANE=lane1")
        func.afc.gcode.respond_info.assert_not_called()

    def test_hook_present_returns_none_runs_nothing_and_skips_default_retract(self):
        func, lane, gcmd = self._make()
        lane.unit_obj = MagicMock(spec=["get_lane_reset_command"])
        lane.unit_obj.get_lane_reset_command.return_value = None

        self._run(func, gcmd)

        lane.unit_obj.get_lane_reset_command.assert_called_once_with(lane, 50.0)
        func.afc.gcode.run_script_from_command.assert_not_called()
        # respond_info() is the first call of the default retract path; its
        # absence proves that path never ran.
        func.afc.gcode.respond_info.assert_not_called()

    def test_no_hook_falls_through_to_default_retract_path(self):
        # A tiny stand-in for the hub: reports the hub as blocked on the
        # first read (so the early "hub already clear" guard is skipped),
        # then reports it clear on every read after (so the default
        # move-to-hub retract while-loop exits immediately).
        class FakeHub:
            def __init__(self):
                self.reads = 0
                self.hub_clear_move_dis = 65.0

            @property
            def state(self):
                self.reads += 1
                return self.reads <= 1

        func, lane, gcmd = self._make()
        func.afc.homing_enabled = True
        lane.unit_obj = MagicMock(spec=["move_to_hub"])
        lane.hub_obj = FakeHub()
        lane.move = MagicMock()

        self._run(func, gcmd)

        assert not hasattr(lane.unit_obj, "get_lane_reset_command")


class TestCmdAfcLaneResetToolheadLoadedGuard:
    """Regression coverage for AFCProject/AFC-Klipper-Add-On#803: the
    toolhead-loaded guard logged an error but had no `return`, so the
    reset retract ran anyway."""

    def _make(self, distance=50):
        func = _make_func()
        afc, lane = self._make_afc_lane()
        func.afc = afc
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand(params={"LANE": lane.name, "DISTANCE": distance})
        return func, lane, gcmd

    def _make_afc_lane(self):
        afc = _make_afc()
        afc.error = MagicMock()
        lane = _make_afc_lane()
        lane.hub_obj = MagicMock()
        lane.hub_obj.state = True
        lane.short_move_dis = 10
        # Only expose move_to_hub, so a call to the unconditional-retract
        # path would fail loudly rather than silently succeeding through
        # a stray MagicMock attribute.
        lane.unit_obj = MagicMock(spec=["move_to_hub"])
        afc.lanes[lane.name] = lane
        return afc, lane

    def _run(self, func, gcmd):
        with patch("extras.AFC_functions.AFCprompt") as mocked_afc_prompt:
            func.cmd_AFC_LANE_RESET(gcmd)
        return mocked_afc_prompt

    def test_toolhead_loaded_aborts_before_retract(self):
        func, lane, gcmd = self._make()
        tool_lane = MagicMock()
        tool_lane.name = "lane2"
        func.get_current_lane_obj = MagicMock(return_value=tool_lane)

        mocked_afc_prompt = self._run(func, gcmd)

        func.afc.error.AFC_error.assert_called_once_with(
            "Toolhead is loaded with 'lane2', unload or check sensor before resetting lane",
            pause=False, gcmd=gcmd,
        )
        mocked_afc_prompt.return_value.p_end.assert_called_once()
        lane.unit_obj.move_to_hub.assert_not_called()
        func.afc.gcode.run_script_from_command.assert_not_called()
        func.afc.gcode.respond_info.assert_not_called()

    def test_toolhead_clear_proceeds_to_retract(self):
        # A tiny stand-in for the hub: reports the hub as blocked on the
        # first read (so the earlier "hub already clear" guard is passed),
        # then reports it clear on every read after (so the move-to-hub
        # retract while-loop exits immediately).
        class FakeHub:
            def __init__(self):
                self.reads = 0
                self.hub_clear_move_dis = 65.0

            @property
            def state(self):
                self.reads += 1
                return self.reads <= 1

        func, lane, gcmd = self._make()
        func.get_current_lane_obj = MagicMock(return_value=None)
        func.afc.homing_enabled = True
        lane.hub_obj = FakeHub()
        lane.move = MagicMock()

        self._run(func, gcmd)

        func.afc.error.AFC_error.assert_not_called()
        lane.unit_obj.move_to_hub.assert_called_once()
        func.afc.gcode.respond_info.assert_any_call("Resetting lane1 to hub")


# ── handle_activate_extruder / handle_activate_extruder ─────────────────────

class TestHandleActivateExtruder:
    def _make(self, cur_lane_loaded=None):
        func = _make_func()
        func.get_current_lane_obj = MagicMock(return_value=cur_lane_loaded)
        func.afc.spool = MagicMock()
        return func

    def _make_other_lane(self, name="lane2", prep_state=False, load_state=False,
                         tool_loaded=False):
        lane = _make_afc_lane(f"AFC_stepper {name}")
        lane.do_enable = MagicMock()
        lane.disable_buffer = MagicMock()
        lane.unsync_to_extruder = MagicMock()
        lane.prep_state = prep_state
        lane._load_state = load_state
        lane.tool_loaded = tool_loaded
        lane.unit_obj = MagicMock()
        return lane

    def test_handle_activate_extruder_cb_delegates_to_underscore_variant(self):
        """handle_activate_extruder_cb is the extruder:activate_extruder event
        handler, which Klipper always calls with no arguments, so it has no
        real gcmd to pass through and uses the "callback" literal instead."""
        func = self._make()
        func.handle_activate_extruder = MagicMock()
        func.handle_activate_extruder_cb()
        func.handle_activate_extruder.assert_called_once_with(0, gcmd="sub_command")

    def test_not_ready_lane_calls_lane_not_ready(self):
        """Covers the `prep_state and load_state` guard's False branch --
        this now calls lane_not_ready (not the old lane_unloaded)."""
        func = self._make(cur_lane_loaded=None)
        lane = self._make_other_lane(prep_state=False, load_state=False)
        func.afc.lanes = {"lane2": lane}
        gcmd = MagicMock()
        func.handle_activate_extruder(0, gcmd=gcmd)
        lane.unit_obj.lane_not_ready.assert_called_once_with(lane, gcmd=gcmd)
        lane.unit_obj.lane_loaded.assert_not_called()
        lane.unit_obj.lane_tool_loaded_idle.assert_not_called()

    def test_ready_prep_only_calls_lane_not_ready(self):
        """prep_state alone must not satisfy the guard."""
        func = self._make(cur_lane_loaded=None)
        lane = self._make_other_lane(prep_state=True, load_state=False)
        func.afc.lanes = {"lane2": lane}
        gcmd = MagicMock()
        func.handle_activate_extruder(0, gcmd=gcmd)
        lane.unit_obj.lane_not_ready.assert_called_once_with(lane, gcmd=gcmd)

    def test_ready_load_only_calls_lane_not_ready(self):
        """load_state alone must not satisfy the guard."""
        func = self._make(cur_lane_loaded=None)
        lane = self._make_other_lane(prep_state=False, load_state=True)
        func.afc.lanes = {"lane2": lane}
        gcmd = MagicMock()
        func.handle_activate_extruder(0, gcmd=gcmd)
        lane.unit_obj.lane_not_ready.assert_called_once_with(lane, gcmd=gcmd)

    def test_ready_not_tool_loaded_calls_lane_loaded(self):
        func = self._make(cur_lane_loaded=None)
        lane = self._make_other_lane(prep_state=True, load_state=True, tool_loaded=False)
        func.afc.lanes = {"lane2": lane}
        gcmd = MagicMock()
        func.handle_activate_extruder(0, gcmd=gcmd)
        lane.unit_obj.lane_loaded.assert_called_once_with(lane, gcmd=gcmd)
        lane.unit_obj.lane_not_ready.assert_not_called()

    def test_ready_and_tool_loaded_calls_lane_tool_loaded_idle(self):
        func = self._make(cur_lane_loaded=None)
        lane = self._make_other_lane(prep_state=True, load_state=True, tool_loaded=True)
        func.afc.lanes = {"lane2": lane}
        gcmd = MagicMock()
        func.handle_activate_extruder(0, gcmd=gcmd)
        lane.unit_obj.lane_tool_loaded_idle.assert_called_once_with(lane, gcmd=gcmd)
        lane.unit_obj.lane_loaded.assert_not_called()
        lane.unit_obj.lane_not_ready.assert_not_called()

    def test_active_lane_is_skipped_entirely(self):
        """The currently-loaded lane must not be touched by this loop at all."""
        func = self._make()
        active_lane = self._make_other_lane("lane1", prep_state=True, load_state=True)
        active_lane.enable_buffer = MagicMock()
        active_lane.sync_to_extruder = MagicMock()
        active_lane.spool_id = None
        func.get_current_lane_obj = MagicMock(return_value=active_lane)
        func.afc.lanes = {"lane1": active_lane}
        func.handle_activate_extruder(0, gcmd=MagicMock())
        # do_enable(True) is legitimately called later, outside the loop, to
        # enable the active lane -- do_enable(False) is what the (skipped)
        # loop body would have called, so that's the call that must be absent.
        active_lane.do_enable.assert_called_once_with(True)
        active_lane.unit_obj.lane_not_ready.assert_not_called()
        active_lane.unit_obj.lane_loaded.assert_not_called()


# ── unset_lane_loaded ──────────────────────────────────────────────────────

class TestUnsetLaneLoaded:
    def _make(self, cur_lane_loaded=None):
        func = _make_real_func()
        func.get_current_lane_obj = MagicMock(return_value=cur_lane_loaded)
        func.afc.function = func
        func.handle_activate_extruder = MagicMock()
        func.afc.save_vars = MagicMock()
        return func

    def _make_lane(self):
        lane = _make_afc_lane("AFC_stepper lane1")
        lane.unsync_to_extruder = MagicMock()
        lane.set_tool_unloaded = MagicMock()
        lane.unit_obj = MagicMock()
        return lane

    def test_no_current_lane_does_nothing(self):
        func = self._make(cur_lane_loaded=None)
        gcmd = _make_gcmd()

        func.unset_lane_loaded(gcmd)

        func.afc.save_vars.assert_not_called()

    def test_stops_feed_assist_before_unsyncing(self):
        lane = self._make_lane()
        func = self._make(cur_lane_loaded=lane)
        gcmd = _make_gcmd()

        func.unset_lane_loaded(gcmd)

        lane.unit_obj.stop_feed_assist.assert_called_once_with(lane)
        lane.unsync_to_extruder.assert_called_once_with(gcmd=gcmd)
        lane.set_tool_unloaded.assert_called_once_with(normal_toolchange=True, gcmd=gcmd)
        lane.unit_obj.return_to_home.assert_called_once_with(gcmd=gcmd)
        func.afc.save_vars.assert_called_once()


# ── check_for_td1_error / _check_td1_error_in_data ────────────────────────────

class TestCheckForTd1Error:
    def test_delegates_to_moonraker_fetch_then_checks_data(self):
        func = _make_func()
        func.afc.moonraker = MagicMock()
        func.afc.moonraker.get_td1_data.return_value = {"SN1": {"error": None}}
        error_occurred, msg = func.check_for_td1_error()
        func.afc.moonraker.get_td1_data.assert_called_once_with()
        assert error_occurred is False
        assert msg == ""


class TestCheckTd1ErrorInData:
    def test_none_data_reports_no_error(self):
        """Covers td1_data being None (a failed fetch) without crashing."""
        func = _make_func()
        error_occurred, msg = func._check_td1_error_in_data(None)
        assert error_occurred is False
        assert msg == ""

    def test_no_devices_have_errors(self):
        func = _make_func()
        td1_data = {"SN1": {"error": None}, "SN2": {"error": None}}
        error_occurred, msg = func._check_td1_error_in_data(td1_data)
        assert error_occurred is False
        assert msg == ""

    def test_matching_serial_with_error_reports_it(self):
        func = _make_func()
        td1_data = {"SN1": {"error": "jam"}}
        error_occurred, msg = func._check_td1_error_in_data(td1_data, serial_number="SN1")
        assert error_occurred is True
        assert "SN1" in msg
        assert "jam" in msg

    def test_non_matching_serial_number_ignores_other_devices_error(self):
        """A specific serial_number filters out errors on other devices."""
        func = _make_func()
        td1_data = {"SN1": {"error": "jam"}}
        error_occurred, msg = func._check_td1_error_in_data(td1_data, serial_number="SN2")
        assert error_occurred is False
        assert msg == ""

    def test_serial_number_none_checks_all_devices(self):
        func = _make_func()
        td1_data = {"SN1": {"error": None}, "SN2": {"error": "overheating"}}
        error_occurred, msg = func._check_td1_error_in_data(td1_data, serial_number=None)
        assert error_occurred is True
        assert "SN2" in msg

    def test_print_error_true_logs_error(self):
        func = _make_func()
        td1_data = {"SN1": {"error": "jam"}}
        func._check_td1_error_in_data(td1_data, serial_number="SN1", print_error=True)
        assert func.logger.messages == [
            ("error",
             "Error with TD-1 Serial: SN1, please fix error with TD-1 and run 'AFC_RESET_TD1 SERIAL=SN1' macro.\n"
             "Some errors can occur when first booting machine and filament is in TD-1 device\n"
             "Reported Error: jam"),
        ]

    def test_print_error_false_does_not_log(self):
        func = _make_func()
        td1_data = {"SN1": {"error": "jam"}}
        func._check_td1_error_in_data(td1_data, serial_number="SN1", print_error=False)
        assert func.logger.messages == []


# ── check_for_td1_id / _check_td1_id_in_data ──────────────────────────────────

class TestCheckForTd1Id:
    def test_delegates_to_moonraker_fetch_then_checks_data(self):
        func = _make_func()
        func.afc.moonraker = MagicMock()
        func.afc.moonraker.get_td1_data.return_value = {"SN1": {"error": None}}
        valid, msg = func.check_for_td1_id("SN1")
        func.afc.moonraker.get_td1_data.assert_called_once_with()
        assert valid is True


class TestCheckTd1IdInData:
    def test_none_data_reports_not_found(self):
        """Covers td1_data being None (a failed fetch) without crashing."""
        func = _make_func()
        valid, msg = func._check_td1_id_in_data(None, "SN1")
        assert valid is False
        assert "SN1" in msg

    def test_serial_not_in_data_reports_not_found(self):
        func = _make_func()
        valid, msg = func._check_td1_id_in_data({"SN2": {"error": None}}, "SN1")
        assert valid is False
        assert "SN1" in msg

    def test_serial_present_with_no_error_is_valid(self):
        func = _make_func()
        valid, msg = func._check_td1_id_in_data({"SN1": {"error": None}}, "SN1")
        assert valid is True

    def test_serial_present_with_error_is_not_valid(self):
        func = _make_func()
        valid, msg = func._check_td1_id_in_data({"SN1": {"error": "jam"}}, "SN1")
        assert valid is False
        assert "jam" in msg


class TestCmdAfcTestLanesParkAndVerify:
    def _make_loaded(self, func):
        lane = _make_afc_lane()
        lane._load_state = True
        lane.prep_state = True
        func.afc.lanes = {lane.name: lane}
        func.afc.current = None
        return lane

    def test_aborts_when_macro_positions_not_set(self):
        from tests.conftest import MockGCodeCommand
        func = _make_func()
        self._make_loaded(func)
        err = "park is set to True and variable_park_loc_xy has not been updated.\n"
        func.afc.verify_macro_positions = MagicMock(return_value=err)

        with patch("extras.AFC_functions.AFCprompt") as mocked_prompt:
            func.cmd_AFC_TEST_LANES(MockGCodeCommand())

        mocked_prompt.assert_not_called()
        assert ("error", err) in func.logger.messages

    def test_creates_prompt_when_macro_positions_ok(self):
        from tests.conftest import MockGCodeCommand
        func = _make_func()
        self._make_loaded(func)
        func.afc.verify_macro_positions = MagicMock(return_value="")

        with patch("extras.AFC_functions.AFCprompt") as mocked_prompt:
            func.cmd_AFC_TEST_LANES(MockGCodeCommand())

        func.afc.verify_macro_positions.assert_called_once_with()
        mocked_prompt.return_value.create_custom_p.assert_called_once()

    def _run_test_lane(self, func, park, park_cmd):
        from tests.conftest import MockGCodeCommand
        func.afc.park = park
        func.afc.park_cmd = park_cmd
        gcmd = MockGCodeCommand(params={"LANE": "lane1", "ITERATION": 0})
        with patch("extras.AFC_functions.AFCprompt"):
            func.cmd_TEST_LANE(gcmd)

    def test_parks_with_user_configured_park_cmd(self):
        func = _make_func()
        self._run_test_lane(func, park=True, park_cmd="MY_PARK")
        func.afc.gcode.run_script_from_command.assert_called_once_with("MY_PARK")

    def test_does_not_park_when_park_disabled(self):
        func = _make_func()
        self._run_test_lane(func, park=False, park_cmd="MY_PARK")
        func.afc.gcode.run_script_from_command.assert_not_called()

    def test_does_not_park_when_park_cmd_unset(self):
        func = _make_func()
        self._run_test_lane(func, park=True, park_cmd=None)
        func.afc.gcode.run_script_from_command.assert_not_called()
