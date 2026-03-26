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

import time
from unittest.mock import MagicMock
import pytest

from extras.AFC_functions import afcFunction, afcDeltaTime


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


# ── get_filament_status ───────────────────────────────────────────────────────

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