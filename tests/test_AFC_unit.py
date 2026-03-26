"""
Unit tests for extras/AFC_unit.py

Covers:
  - afcUnit.__str__: returns name
  - afcUnit._check_and_errorout: None vs non-None object
  - afcUnit.get_status: correct keys and content
  - afcUnit.check_runout: returns False
  - afcUnit.return_to_home: returns None
  - afcUnit.lane_loaded/unloaded/loading/tool_loaded/tool_unloaded: call afc_led
  - afcUnit.set_logo_color: calls afc_led when color present, skips when None/empty
"""

from __future__ import annotations

from unittest.mock import MagicMock, call
import pytest

from extras.AFC_unit import afcUnit


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_unit(name="Turtle_1"):
    """Build an afcUnit bypassing the complex __init__."""
    unit = afcUnit.__new__(afcUnit)

    from tests.conftest import MockAFC, MockPrinter, MockLogger
    from extras.AFC_error import afcError
    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    afc.logger = MockLogger()
    afc.error = afcError.__new__(afcError)
    afc.error.logger = afc.logger
    unit.printer = printer
    unit.afc = afc
    unit.logger = afc.logger
    unit.name = name
    unit.full_name = ["AFC_BoxTurtle", name]
    unit.lanes = {}
    unit.hub_obj = None
    unit.extruder_obj = None
    unit.buffer_obj = None
    unit.hub = None
    unit.extruder = None
    unit.buffer_name = None
    unit.td1_defined = False
    unit.type = "Box_Turtle"
    unit.gcode = afc.gcode
    unit.led_logo_index = None
    unit.enable_buffer_tool_check = True

    return unit


def _make_lane(name="lane1", hub="hub1", extruder="ext1", buffer_name="buf1"):
    from extras.AFC_lane import AFCLane
    lane = AFCLane.__new__(AFCLane)
    lane.unit_obj = MagicMock()
    lane.name = name
    lane.hub = hub
    lane.extruder_name = extruder
    lane.buffer_name = buffer_name
    lane.led_ready = "0,1,0,0"
    lane.led_not_ready = "0,0,0,0.25"
    lane.led_loading = "0,0,1,0"
    lane.led_tool_loaded = "0,1,0,0"
    lane.led_tool_unloaded = "0,1,0,0"
    lane.led_index = "1"
    lane.led_spool_illum = "1,1,1,0"
    lane._load_state = True
    lane.short_moves_speed = 50
    lane.short_moves_accel = 50
    return lane


# ── __str__ ───────────────────────────────────────────────────────────────────

class TestStr:
    def test_str_returns_name(self):
        unit = _make_unit("Turtle_1")
        assert str(unit) == "Turtle_1"

    def test_str_reflects_different_name(self):
        unit = _make_unit("NightOwl_2")
        assert str(unit) == "NightOwl_2"


# ── _check_and_errorout ────────────────────────────────────────────────────────

class TestCheckAndErrorOut:
    def test_returns_true_when_obj_is_none(self):
        unit = _make_unit()
        error, msg = unit._check_and_errorout(None, "AFC_hub testname", "hub")
        assert error is True

    def test_error_msg_not_empty_when_obj_none(self):
        unit = _make_unit()
        _, msg = unit._check_and_errorout(None, "AFC_hub testname", "hub")
        assert len(msg) > 0

    def test_error_msg_contains_config_name(self):
        unit = _make_unit()
        _, msg = unit._check_and_errorout(None, "AFC_hub testname", "hub")
        assert "AFC_hub testname" in msg

    def test_returns_false_when_obj_present(self):
        unit = _make_unit()
        obj = MagicMock()
        error, msg = unit._check_and_errorout(obj, "AFC_hub", "hub")
        assert error is False

    def test_msg_empty_when_obj_present(self):
        unit = _make_unit()
        obj = MagicMock()
        _, msg = unit._check_and_errorout(obj, "AFC_hub", "hub")
        assert msg == ""


# ── get_status ────────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_has_lanes_key(self):
        unit = _make_unit()
        status = unit.get_status()
        assert "lanes" in status

    def test_has_extruders_key(self):
        unit = _make_unit()
        status = unit.get_status()
        assert "extruders" in status

    def test_has_hubs_key(self):
        unit = _make_unit()
        status = unit.get_status()
        assert "hubs" in status

    def test_has_buffers_key(self):
        unit = _make_unit()
        status = unit.get_status()
        assert "buffers" in status

    def test_lanes_list_contains_lane_name(self):
        unit = _make_unit()
        lane = _make_lane("lane1")
        unit.lanes = {"lane1": lane}
        status = unit.get_status()
        assert "lane1" in status["lanes"]

    def test_extruder_name_collected_from_lanes(self):
        unit = _make_unit()
        lane = _make_lane("lane1", extruder="my_extruder")
        unit.lanes = {"lane1": lane}
        status = unit.get_status()
        assert "my_extruder" in status["extruders"]

    def test_hub_name_collected_from_lanes(self):
        unit = _make_unit()
        lane = _make_lane("lane1", hub="my_hub")
        unit.lanes = {"lane1": lane}
        status = unit.get_status()
        assert "my_hub" in status["hubs"]

    def test_hub_name_collected_from_lanes_direct_hub(self):
        unit = _make_unit()
        lane = _make_lane("lane1", hub="direct")
        unit.lanes = {"lane1": lane}
        status = unit.get_status()
        assert "direct" not in status["hubs"]

    def test_hub_name_collected_from_lanes_direct_load_hub(self):
        unit = _make_unit()
        lane = _make_lane("lane1", hub="direct_load")
        unit.lanes = {"lane1": lane}
        status = unit.get_status()
        assert "direct_load" not in status["hubs"]

    def test_buffer_name_collected_from_lanes(self):
        unit = _make_unit()
        lane = _make_lane("lane1", buffer_name="my_buffer")
        unit.lanes = {"lane1": lane}
        status = unit.get_status()
        assert "my_buffer" in status["buffers"]

    def test_duplicate_extruder_not_repeated(self):
        """Two lanes sharing the same extruder should appear only once."""
        unit = _make_unit()
        lane1 = _make_lane("lane1", extruder="shared_ext")
        lane2 = _make_lane("lane2", extruder="shared_ext")
        unit.lanes = {"lane1": lane1, "lane2": lane2}
        status = unit.get_status()
        assert status["extruders"].count("shared_ext") == 1

    def test_empty_lanes_returns_empty_lists(self):
        unit = _make_unit()
        unit.lanes = {}
        status = unit.get_status()
        assert status["lanes"] == []
        assert status["extruders"] == []
        assert status["hubs"] == []
        assert status["buffers"] == []


# ── check_runout ──────────────────────────────────────────────────────────────

class TestCheckRunout:
    def test_returns_false(self):
        unit = _make_unit()
        assert unit.check_runout("lane") is False


# ── return_to_home ────────────────────────────────────────────────────────────

class TestReturnToHome:
    def test_returns_none(self):
        unit = _make_unit()
        result = unit.return_to_home()
        assert result is None


# ── LED helpers ───────────────────────────────────────────────────────────────

class TestLaneStatusLeds:
    def test_lane_loaded_calls_afc_led_with_ready_color(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.lane_loaded(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_ready, lane.led_index)

    def test_lane_unloaded_calls_afc_led_with_not_ready_color(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.lane_unloaded(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_not_ready, lane.led_index)

    def test_lane_loading_calls_afc_led_with_loading_color(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.lane_loading(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_loading, lane.led_index)

    def test_lane_tool_loaded_calls_afc_led_with_tool_loaded_color(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit.lane_tool_loaded(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_tool_loaded, lane.led_index)
        lane.extruder_obj.set_status_led.assert_called_once_with(lane.led_tool_loaded)

    def test_lane_tool_unloaded_calls_afc_led_with_ready_color(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit.lane_tool_unloaded(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_ready, lane.led_index)
        lane.extruder_obj.set_status_led.assert_called_once_with(lane.led_tool_unloaded)


# ── set_logo_color ────────────────────────────────────────────────────────────

class TestSetLogoColor:
    def test_calls_afc_led_when_color_present(self):
        unit = _make_unit()
        unit.led_logo_index = "0"
        unit.afc.function.HexToLedString = MagicMock(return_value="0,0,0,0")
        unit.set_logo_color("FF0000")
        unit.afc.function.afc_led.assert_called()

    def test_no_call_when_color_is_none(self):
        unit = _make_unit()
        unit.set_logo_color(None)
        unit.afc.function.afc_led.assert_not_called()

    def test_no_call_when_color_is_empty_string(self):
        unit = _make_unit()
        unit.set_logo_color("")
        unit.afc.function.afc_led.assert_not_called()


# ── _buffer_toolhead_load_check ────────────────────────────────────────────────────────────
class TestBufferToolheadLoadCheck:
    def test_homing_not_enabled_not_loaded(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.afc.homing_enabled = False
        lane.tool_loaded = False
        result = unit._buffer_toolhead_load_check(lane)
        assert result is False
    
    def test_homing_not_enabled_loaded(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.afc.homing_enabled = False
        lane.tool_loaded = True
        result = unit._buffer_toolhead_load_check(lane)
        assert result is True

    def test_homing_enabled_disable_buffer_tool_check_loaded(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.afc.homing_enabled = True
        lane.tool_loaded = True
        unit.enable_buffer_tool_check = False
        result = unit._buffer_toolhead_load_check(lane)
        assert result is True

    def test_homing_enabled_loaded_fail_extend(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.buffer_obj = MagicMock()
        unit.afc.homing_enabled = True
        lane.tool_loaded = True
        lane.buffer_obj.advance_state = False
        lane.move_to = MagicMock()
        lane.move_to.return_value = (False, 200, False)
        result = unit._buffer_toolhead_load_check(lane)
        assert result is False
        error_msgs = [m for lvl, m in unit.logger.messages if lvl == "error"]
        assert any(f"Buffer toolhead loaded check failed for {lane.name}. Please verify" in m for m in error_msgs)
    
    def test_homing_enabled_loaded_extended(self):
        from extras.AFC_lane import MoveDirection
        SIDE_EFFECT_DIST = 200
        unit = _make_unit()
        lane = _make_lane()
        lane.buffer_obj = MagicMock()
        unit.afc.homing_enabled = True
        lane.tool_loaded = True
        lane.buffer_obj.advance_state = False
        lane.move_to = MagicMock()
        lane.move = MagicMock()
        lane.move_to.side_effect = [(True, SIDE_EFFECT_DIST, False)]
        result = unit._buffer_toolhead_load_check(lane)
        call_args = lane.move.call_args[0]
        assert result is True
        assert call_args[0] == (SIDE_EFFECT_DIST * MoveDirection.NEG)