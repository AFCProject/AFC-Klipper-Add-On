"""
Unit tests for extras/AFC_BoxTurtle.py

Covers:
  - afcBoxTurtle: MAX_NUM_MOVES constant
  - system_Test: LED logic for each lane state combination
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from extras.AFC_BoxTurtle import afcBoxTurtle


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_box_turtle(name="Turtle_1"):
    """Build an afcBoxTurtle bypassing the complex __init__."""
    unit = afcBoxTurtle.__new__(afcBoxTurtle)

    from tests.conftest import MockAFC, MockPrinter, MockLogger, MockReactor

    afc = MockAFC()
    reactor = MockReactor()
    afc.reactor = reactor
    afc.logger = MockLogger()
    printer = MockPrinter(afc=afc)

    unit.printer = printer
    unit.afc = afc
    unit.logger = afc.logger
    unit.reactor = reactor
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

    return unit


def _make_lane(prep_state=False, load_state=False, tool_loaded=False):
    lane = MagicMock()
    lane.move = MagicMock()
    lane.name = "lane1"
    lane.prep_state = prep_state
    lane.load_state = load_state
    lane.tool_loaded = tool_loaded
    lane.map = "T0"
    lane.led_ready = "0,1,0,0"
    lane.led_not_ready = "0,0,0,0.25"
    lane.led_fault = "1,0,0,0"
    lane.led_loading = "0,0,1,0"
    lane.led_spool_illum = "1,1,1,0"
    lane.led_index = "1"
    lane.led_spool_index = "2"
    lane.status = None
    return lane


# ── Constants ────────────────────────────────────────────────────────────────

class TestConstants:
    def test_max_num_moves(self):
        assert afcBoxTurtle.MAX_NUM_MOVES == 40


# ── system_Test: empty lane (prep=F, load=F) ──────────────────────────────────

class TestSystemTestEmptyLane:
    def test_empty_lane_sets_not_ready_led(self):
        """prep=False, load=False → LED set to led_not_ready."""
        unit = _make_box_turtle()
        lane = _make_lane(prep_state=False, load_state=False)
        unit.system_Test(lane, delay=0.0, assignTcmd=True, enable_movement=False)
        unit.afc.function.afc_led.assert_any_call(lane.led_not_ready, lane.led_index)

    def test_empty_lane_does_not_set_fault_led(self):
        unit = _make_box_turtle()
        lane = _make_lane(prep_state=False, load_state=False)
        unit.system_Test(lane, delay=0.0, assignTcmd=True, enable_movement=False)
        for c in unit.afc.function.afc_led.call_args_list:
            assert c[0][0] != lane.led_fault


# ── system_Test: fault lane (prep=F, load=T) ─────────────────────────────────

class TestSystemTestFaultLane:
    def test_fault_state_sets_fault_led(self):
        """prep=False, load=True → LED set to led_fault."""
        unit = _make_box_turtle()
        lane = _make_lane(prep_state=False, load_state=True)
        unit.system_Test(lane, delay=0.0, assignTcmd=True, enable_movement=False)
        unit.afc.function.afc_led.assert_any_call(lane.led_fault, lane.led_index)

    def test_fault_state_calls_do_enable_false(self):
        """Faulted lane should disable stepper."""
        unit = _make_box_turtle()
        lane = _make_lane(prep_state=False, load_state=True)
        unit.system_Test(lane, delay=0.0, assignTcmd=True, enable_movement=False)
        lane.do_enable.assert_called_with(False)


# ── system_Test: loaded (prep=T, load=T) ──────────────────────────────────────

class TestSystemTestLoadedLane:
    def test_loaded_sets_ready_led(self):
        """prep=True, load=True → LED set to led_ready."""
        unit = _make_box_turtle()
        lane = _make_lane(prep_state=True, load_state=True)
        unit.system_Test(lane, delay=0.0, assignTcmd=True, enable_movement=False)
        unit.afc.function.afc_led.assert_any_call(lane.led_ready, lane.led_index)

    def test_loaded_sets_status_to_loaded(self):
        from extras.AFC_lane import AFCLaneState
        unit = _make_box_turtle()
        lane = _make_lane(prep_state=True, load_state=True)
        unit.system_Test(lane, delay=0.0, assignTcmd=True, enable_movement=False)
        assert lane.status == AFCLaneState.LOADED


# ── system_Test: locked not loaded (prep=T, load=F) ──────────────────────────

class TestSystemTestLockedNotLoaded:
    def test_locked_not_loaded_sets_not_ready_led(self):
        """prep=True, load=False → LED set to led_not_ready."""
        unit = _make_box_turtle()
        lane = _make_lane(prep_state=True, load_state=False)
        unit.system_Test(lane, delay=0.0, assignTcmd=True, enable_movement=False)
        unit.afc.function.afc_led.assert_any_call(lane.led_not_ready, lane.led_index)


# ── system_Test: reactor pause called when no movement ───────────────────────

class TestSystemTestReactorPause:
    def test_reactor_pause_called_when_no_movement(self):
        unit = _make_box_turtle()
        lane = _make_lane()
        pause_mock = MagicMock()
        unit.afc.reactor.pause = pause_mock
        unit.system_Test(lane, delay=0.0, assignTcmd=True, enable_movement=False)
        pause_mock.assert_called()

# ── _move_lane ──────────────────────────────────────────────────
class Test_MoveLane:
    def test_returns_not_enable_movement_loaded(self):
        from unittest.mock import PropertyMock
        unit = _make_box_turtle()
        lane = _make_lane()
        type(lane).load_state = PropertyMock(side_effect=[True])

        result = unit._move_lane(lane, delay=1, enable_movement=False)
        assert result is True
    
    def test_returns_not_enable_movement_not_loaded(self):
        from unittest.mock import PropertyMock
        unit = _make_box_turtle()
        lane = _make_lane()
        type(lane).load_state = PropertyMock(side_effect=[False])

        result = unit._move_lane(lane, delay=1, enable_movement=False)
        assert result is False
    
    def test_returns_enable_movement_not_loaded(self):
        from unittest.mock import PropertyMock
        unit = _make_box_turtle()
        lane = _make_lane()
        pause_mock = MagicMock()
        unit.afc.reactor.pause = pause_mock
        type(lane).load_state = PropertyMock(side_effect=[False])

        result = unit._move_lane(lane, delay=1, enable_movement=True)
        assert result is False
        pause_mock.assert_called()