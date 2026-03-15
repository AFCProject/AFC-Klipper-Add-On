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