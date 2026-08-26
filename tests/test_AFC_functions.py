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

from unittest.mock import MagicMock, patch, call
import pytest

from extras.AFC_functions import afcFunction, afcDeltaTime, round_floats
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

    def _setup_calibration(self, standalone=False, td1_present=False, moonraker=None):
        """Builds func/afc/lane with the defaults nearly every test relies on."""
        func = _make_func()
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

    def _assert_afc_error(self, func, message):
        func.afc.error.AFC_error.assert_called_once_with(message, pause=False)

    # ------------------------------------------------------------------
    # _lane_calibration helpers
    # ------------------------------------------------------------------

    def _set_lane_calibration_result(self, func, result):
        func._lane_calibration = MagicMock(return_value=deepcopy(result))

    def _assert_lane_calibration_called(self, func, lane_name, bowden=None, tolerance=5, distance=25):
        func._lane_calibration.assert_called_once_with(lane_name, bowden, tolerance, distance)

    def _assert_lane_calibration_not_called(self, func):
        func._lane_calibration.assert_not_called()

    # ------------------------------------------------------------------
    # Bowden / direct-hub helpers
    # ------------------------------------------------------------------

    def _set_calibrate_bowden_result(self, lane, result):
        lane.unit_obj.calibrate_bowden = MagicMock(return_value=result)

    def _assert_calibrate_bowden_called(self, lane, distance=25, tolerance=5):
        lane.unit_obj.calibrate_bowden.assert_called_once_with(lane, distance, tolerance)

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

    def _assert_afc_cali_comp_called(self, func, lanes, messages, title="AFC Calibration"):
        func._afc_cali_comp.assert_called_once_with(
            ", ".join(lanes),
            title,
            " ".join(messages)
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

        self._assert_afc_error(func, f"'{lane_name}' is not a valid lane")
        self._assert_no_calibration_attempted(func, lane)

    def test_calibration_lane_is_standalone(self):
        func, afc, lane = self._setup_calibration(standalone=True)
        gcmd = self._make_gcmd_for_calibration(lane=lane.name)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, f"{lane.name} is a standalone lane, cannot calibrate")
        self._assert_no_calibration_attempted(func, lane)

    def test_calibration_unit_not_valid(self):
        func, afc, lane = self._setup_calibration()
        unit_name = "test"
        gcmd = self._make_gcmd_for_calibration(lane=lane.name, unit=unit_name)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, f"'{unit_name}' is not a valid unit")
        self._assert_no_calibration_attempted(func, lane)

    def test_calibration_bowden_no_in_lanes(self):
        func, afc, lane = self._setup_calibration()
        lane_name = "lane2"
        gcmd = self._make_gcmd_for_calibration(bowden=lane_name)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, f"'{lane_name}' is not a valid lane to calibrate bowden length")
        self._assert_no_calibration_attempted(func, lane)

    def test_calibration_td1_not_present(self):
        func, afc, lane = self._setup_calibration()
        gcmd = self._make_gcmd_for_calibration(TD1="lane2")

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, "TD-1 is not present, will not be able to calibrate bowden length for TD-1")
        self._assert_no_calibration_attempted(func, lane)
    
    def test_calibration_td1_lane_not_valid(self):
        func, afc, lane = self._setup_calibration(td1_present=True)
        gcmd = self._make_gcmd_for_calibration(TD1="lane2")

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_afc_error(func, "'lane2' is not a valid lane to calibrate TD-1 bowden length")
        self._assert_no_calibration_attempted(func, lane)

    def test_calibration_lane_valid_success(self):
        func, afc, lane = self._setup_calibration()
        lane_name = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name)
        self._assert_afc_cali_comp_called(func, lane_calibration_call[1], lane_calibration_call[2])
        self._assert_bowden_calibration_not_attempted(lane)

    def test_calibration_lane_valid_success_no_additional_msg(self):
        func, afc, lane = self._setup_calibration()
        lane_name = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name)
        lane_calibration_call = (True, [lane_name], [])
        self._set_lane_calibration_result(func, lane_calibration_call)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name)
        self._assert_afc_cali_comp_called(func, lane_calibration_call[1], [])
        self._assert_bowden_calibration_not_attempted(lane)

    def test_calibration_lane_valid_checked_false(self):
        func, afc, lane = self._setup_calibration()
        lane_name = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name)
        lane_calibration_call = (False, [lane_name], [])
        self._set_lane_calibration_result(func, lane_calibration_call)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name)
        func._afc_cali_comp.assert_not_called()
        self._assert_bowden_calibration_not_attempted(lane)

    def test_calibration_all_lanes_valid_success(self):
        func, afc, lane = self._setup_calibration()
        lane_name = "all"
        gcmd = self._make_gcmd_for_calibration(lane=lane_name)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name)
        self._assert_afc_cali_comp_called(func, lane_calibration_call[1], lane_calibration_call[2])
        self._assert_bowden_calibration_not_attempted(lane)

    def test_calibration_lanes_valid_bowden_fail_tool_start_and_tool_end_none(self):
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = None
        lane.extruder_obj.tool_end = None
        lane_name = "all"
        bowden_lane = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        self._set_lane_calibration_result(func, lane_calibration_call)
        bowden_error = (
            "\nBowden Calibration Error:"
             "\nCannot calibrate with only post extruder sensor and no turtleneck buffer defined in config"
        )
        additional_msg = [
            lane_calibration_call[2][0],
            bowden_error
        ]

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name)
        self._assert_afc_cali_comp_called(func, lane_calibration_call[1], additional_msg)
        self._assert_bowden_calibration_not_attempted(lane)
        self._assert_afc_error(func, bowden_error)

    def test_calibration_lanes_valid_bowden_success_tool_start_none_tool_end_buffer_set_lane(self):
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = None
        lane.extruder_obj.tool_end = MagicMock(return_value="Turtle_1:SW1")
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
        self._assert_lane_calibration_called(func, lane_name)
        self._assert_calibrate_bowden_called(lane)
        self._assert_afc_cali_comp_called(func, calibrated_msg, lane_calibration_call[2])
        self._assert_info_contains(func, "Cannot run calibration using post extruder sensor, using buffer to calibrate bowden length")
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

        self._assert_lane_calibration_called(func, lane_name)
        self._assert_calibrate_bowden_called(lane)
        self._assert_afc_cali_comp_called(func, calibrated_msg, lane_calibration_call[2])
        self._assert_info_not_contains(func, "Cannot run calibration using post extruder sensor, using buffer to calibrate bowden length")
        self._assert_info_contains(func, "Starting AFC distance Calibrations")
        self._assert_info_contains(func, "Bowden length calibration Done!")
        self._assert_calibrate_td1_not_called(lane)

    def test_calibration_lanes_valid_bowden_fail_tool_start_buffer(self):
        func, afc, lane = self._setup_calibration()
        lane.extruder_obj.tool_start = MagicMock(return_value="buffer")
        lane_name = "all"
        bowden_lane = lane.name
        gcmd = self._make_gcmd_for_calibration(lane=lane_name, bowden=bowden_lane)
        lane_calibration_call = (True, [lane_name], ["success"])
        fail_msg = f"{bowden_lane} failed to calibrate bowden length Bowden Calibration Failed"
        self._set_lane_calibration_result(func, lane_calibration_call)
        self._set_calibrate_bowden_result(lane, (False, "Bowden Calibration Failed", 0))

        self._call_cmd_calibrate_afc(func, gcmd)

        self._assert_lane_calibration_called(func, lane_name)
        self._assert_calibrate_bowden_called(lane)
        self._assert_afc_error(func, fail_msg)
        func._afc_cali_fail.assert_called_once_with(
            cali=lane.name, dis=0, reset_lane=False, title= "AFC Bowden Calibration Failed",
            fail_message=fail_msg
        )
        func._afc_cali_comp.assert_not_called()
        self._assert_info_not_contains(func, "Cannot run calibration using post extruder sensor, using buffer to calibrate bowden length")
        self._assert_info_contains(func, "Starting AFC distance Calibrations")
        self._assert_info_not_contains(func, "Bowden length calibration Done!")
        self._assert_calibrate_td1_not_called(lane)

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

        self._assert_lane_calibration_called(func, lane_name)
        self._assert_calibrate_bowden_called(lane)
        func.check_homed.assert_not_called()
        self._assert_afc_cali_comp_called(func, calibrated_msg, lane_calibration_call[2])
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

        self._assert_lane_calibration_called(func, lane_name)
        func.check_homed.assert_called_once_with(
            "Printer needs to be homed before calibrating PTFE length to toolhead")
        self._assert_info_not_contains(func, "Starting AFC distance Calibrations")
        self._assert_bowden_calibration_not_attempted(lane)

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

        self._assert_lane_calibration_called(func, lane_name)
        func.check_homed.assert_called_once_with(
            "Printer needs to be homed before calibrating PTFE length to toolhead")
        self._assert_select_unselect_tool_called(func, lane)
        self._assert_info_not_contains(func, "Starting AFC distance Calibrations")
        self._assert_afc_error(func, f"Failed to select tool '{lane.extruder_obj.name}' before Bowden calibration")
        self._assert_bowden_calibration_not_attempted(lane)

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

        self._assert_lane_calibration_called(func, lane_name)
        func.check_homed.assert_called_once_with(
            "Printer needs to be homed before calibrating PTFE length to toolhead")
        self._assert_select_unselect_tool_called(func, lane)
        self._assert_info_not_contains(func, "Starting AFC distance Calibrations")
        self._assert_afc_error(func, f"Failed to select tool '{lane.extruder_obj.name}' before Bowden calibration")
        self._assert_bowden_calibration_not_attempted(lane)

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

        self._assert_lane_calibration_called(func, lane_name)
        func.check_homed.assert_called_once_with(
            "Printer needs to be homed before calibrating PTFE length to toolhead")
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

        self._assert_lane_calibration_called(func, lane_name)
        func.check_homed.assert_called_once_with(
            "Printer needs to be homed before calibrating PTFE length to toolhead")
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
        )
        self._assert_lane_calibration_not_called(func)
        self._assert_calibrate_bowden_not_called(lane)
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

        self._assert_afc_error(func, error_message)
        func.afc.gcode.run_script_from_command.assert_called_once_with(
            f"AFC_CALI_FAIL TITLE='TD-1 Calibration Failed' FAIL={lane.name} DISTANCE=0 msg='{error_message}' RESET=0"
        )
        self._assert_lane_calibration_not_called(func)
        self._assert_calibrate_bowden_not_called(lane)
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

        self._assert_afc_error(func, error_message)
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
            func, [f"'TD1_Bowden_length: {lane.name}'"], [], title="TD-1 Calibration"
        )


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
            pause=False,
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


# ── handle_activate_extruder / _handle_activate_extruder ─────────────────────

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

    def test_handle_activate_extruder_delegates_to_underscore_variant(self):
        func = self._make()
        func._handle_activate_extruder = MagicMock()
        func.handle_activate_extruder()
        func._handle_activate_extruder.assert_called_once_with(0)

    def test_not_ready_lane_calls_lane_not_ready(self):
        """Covers the `prep_state and load_state` guard's False branch --
        this now calls lane_not_ready (not the old lane_unloaded)."""
        func = self._make(cur_lane_loaded=None)
        lane = self._make_other_lane(prep_state=False, load_state=False)
        func.afc.lanes = {"lane2": lane}
        func._handle_activate_extruder(0)
        lane.unit_obj.lane_not_ready.assert_called_once_with(lane)
        lane.unit_obj.lane_loaded.assert_not_called()
        lane.unit_obj.lane_tool_loaded_idle.assert_not_called()

    def test_ready_prep_only_calls_lane_not_ready(self):
        """prep_state alone must not satisfy the guard."""
        func = self._make(cur_lane_loaded=None)
        lane = self._make_other_lane(prep_state=True, load_state=False)
        func.afc.lanes = {"lane2": lane}
        func._handle_activate_extruder(0)
        lane.unit_obj.lane_not_ready.assert_called_once_with(lane)

    def test_ready_load_only_calls_lane_not_ready(self):
        """load_state alone must not satisfy the guard."""
        func = self._make(cur_lane_loaded=None)
        lane = self._make_other_lane(prep_state=False, load_state=True)
        func.afc.lanes = {"lane2": lane}
        func._handle_activate_extruder(0)
        lane.unit_obj.lane_not_ready.assert_called_once_with(lane)

    def test_ready_not_tool_loaded_calls_lane_loaded(self):
        func = self._make(cur_lane_loaded=None)
        lane = self._make_other_lane(prep_state=True, load_state=True, tool_loaded=False)
        func.afc.lanes = {"lane2": lane}
        func._handle_activate_extruder(0)
        lane.unit_obj.lane_loaded.assert_called_once_with(lane)
        lane.unit_obj.lane_not_ready.assert_not_called()

    def test_ready_and_tool_loaded_calls_lane_tool_loaded_idle(self):
        func = self._make(cur_lane_loaded=None)
        lane = self._make_other_lane(prep_state=True, load_state=True, tool_loaded=True)
        func.afc.lanes = {"lane2": lane}
        func._handle_activate_extruder(0)
        lane.unit_obj.lane_tool_loaded_idle.assert_called_once_with(lane)
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
        func._handle_activate_extruder(0)
        # do_enable(True) is legitimately called later, outside the loop, to
        # enable the active lane -- do_enable(False) is what the (skipped)
        # loop body would have called, so that's the call that must be absent.
        active_lane.do_enable.assert_called_once_with(True)
        active_lane.unit_obj.lane_not_ready.assert_not_called()
        active_lane.unit_obj.lane_loaded.assert_not_called()