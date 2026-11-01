"""
Unit tests for extras/AFC_BoxTurtle.py

Covers:
  - afcBoxTurtle: MAX_NUM_MOVES constant
  - system_Test: LED logic for each lane state combination
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from extras.AFC_BoxTurtle import afcBoxTurtle
from tests.test_AFC_lane import _make_afc_lane


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
    unit.short_move_dis = 10

    return unit


def _make_lane(prep_state=False, load_state=False, tool_loaded=False):
    lane = MagicMock()
    lane.move = MagicMock()
    lane.name = "lane1"
    lane.prep_state = prep_state
    lane.load_state = load_state
    lane.tool_loaded = tool_loaded
    lane.map = ["T0"]
    lane.current_map = "T0"
    lane.map_to_string = MagicMock(return_value="T0")
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
        """prep=False, load=False → lane's own unit_obj.lane_not_ready is called."""
        unit = _make_box_turtle()
        lane = _make_lane(prep_state=False, load_state=False)
        unit.system_Test(lane, delay=0.0, assignTcmd=True, enable_movement=False)
        lane.unit_obj.lane_not_ready.assert_called_once_with(lane)

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
        lane = _make_afc_lane()
        with patch.object(type(lane), "load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect=[True]

            result = unit._move_lane(lane, delay=1, enable_movement=False)
        assert result is True
    
    def test_returns_not_enable_movement_not_loaded(self):
        from unittest.mock import PropertyMock
        unit = _make_box_turtle()
        lane = _make_afc_lane()
        with patch.object(type(lane), "load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect=[False]

            result = unit._move_lane(lane, delay=1, enable_movement=False)
        assert result is False
    
    def test_returns_enable_movement_not_loaded(self):
        from unittest.mock import PropertyMock
        unit = _make_box_turtle()
        lane = _make_afc_lane()
        pause_mock = MagicMock()
        unit.afc.reactor.pause = pause_mock
        with patch.object(type(lane), "load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect=[False]

            result = unit._move_lane(lane, delay=1, enable_movement=True)
        assert result is False
        pause_mock.assert_called()


# ── prep_load ────────────────────────────────────────────────────────────────

class TestPrepLoad:
    def _make_lane_that_never_triggers(self):
        """A lane that stays prepped but never reports load-sensor triggered,
        so prep_load's retry loop always keeps going until MAX_NUM_MOVES."""
        lane = _make_afc_lane()
        lane.prep_state = True
        lane.load = "PA1"
        lane._load_state = False
        lane.move = MagicMock()
        lane.unit_obj = MagicMock()
        return lane

    def test_exceeding_max_num_moves_calls_lane_fault(self):
        unit = _make_box_turtle()
        unit.afc.homing_enabled = False
        lane = self._make_lane_that_never_triggers()

        unit.prep_load(lane)

        lane.unit_obj.lane_fault.assert_called_once_with(lane)

    def test_exceeding_max_num_moves_resets_status_to_none(self):
        from extras.AFC_lane import AFCLaneState
        unit = _make_box_turtle()
        unit.afc.homing_enabled = False
        lane = self._make_lane_that_never_triggers()

        unit.prep_load(lane)

        assert lane.status == AFCLaneState.NONE

    def test_exceeding_max_num_moves_reports_afc_error(self):
        unit = _make_box_turtle()
        unit.afc.homing_enabled = False
        lane = self._make_lane_that_never_triggers()

        unit.prep_load(lane)

        msg = unit.afc.error.AFC_error.call_args[0][0]
        assert "FAILED TO LOAD" in msg

    def test_sensor_triggering_before_max_moves_skips_fault(self):
        """Covers the `x > MAX_NUM_MOVES` guard's False branch: the loop
        exits normally (via raw_load_state going True) without faulting."""
        unit = _make_box_turtle()
        unit.afc.homing_enabled = False
        lane = _make_afc_lane()
        lane.prep_state = True
        lane.load = "PA1"
        lane._load_state = False
        lane.unit_obj = MagicMock()

        def _trigger_after_one_move(*args, **kwargs):
            lane._load_state = True

        lane.move = MagicMock(side_effect=_trigger_after_one_move)

        unit.prep_load(lane)

        lane.unit_obj.lane_fault.assert_not_called()
        unit.afc.error.AFC_error.assert_not_called()


class TestCalibrateBowdenNegativeDistance:
    """Regression coverage for AFCProject/AFC-Klipper-Add-On#800: a negative
    bowden_dist was written into live lane/hub state before the < 0 check
    ran, so an invalid calibration still corrupted cur_lane.dist_hub /
    cur_lane.hub_obj.afc_bowden_length even though it returned an error."""

    def _make_lane_for_calibration(self, dist_hub=900, short_move_dis=10):
        from extras.AFC_lane import AFCMoveWarning

        lane = MagicMock()
        lane.name = "lane1"
        lane.fullname = "AFC_stepper lane1"
        lane.dist_hub = dist_hub
        lane.short_move_dis = short_move_dis
        lane.short_moves_speed = 50
        lane.short_moves_accel = 100
        lane.is_direct_hub.return_value = True
        lane.is_direct_dist.return_value = True
        lane.extruder_obj.tool_start = "toolhead"
        # Sensor reads "not yet at toolhead" once, then "arrived" — exits
        # the homing loop after a single short move.
        lane.get_toolhead_pre_sensor_state.side_effect = [False, True]
        lane.move_to.return_value = (True, 1, AFCMoveWarning.NONE)
        lane.unit_obj.move_to_load.return_value = (True, 0, None)
        return lane

    def test_negative_bowden_dist_does_not_mutate_dist_hub(self):
        unit = _make_box_turtle()
        unit.afc.homing_enabled = False
        unit.afc.error = MagicMock()
        unit.afc.function = MagicMock()

        lane = self._make_lane_for_calibration(dist_hub=900, short_move_dis=10)

        # First call is the initial "retract to extruder" position (used
        # only for bookkeeping); second call is the post-homing retract
        # that yields the final bow_pos=3, which is less than
        # short_move_dis=10 and drives bowden_dist negative.
        unit.calc_position = MagicMock(side_effect=[
            (0, "retract to extruder", True),
            (3, "retract from toolhead sensor", True),
        ])

        success, message, bowden_dist = unit.calibrate_bowden(lane, dis=1, tol=1)

        assert success is False
        assert bowden_dist < 0
        unit.afc.error.AFC_error.assert_called_once()
        # The whole point of the fix: dist_hub must be left untouched.
        assert lane.dist_hub == 900
        unit.afc.function.ConfigRewrite.assert_not_called()


class TestCalibrateBowdenU1Sensor:
    """A U1-style motion sensor (u1_filament_sensor_name) leaves tool_start
    None, so the toolhead-confirm retry loop must also check fila_tool_start
    directly rather than only cur_extruder.tool_start."""

    def test_u1_sensor_enters_the_retry_loop(self):
        from extras.AFC_lane import AFCMoveWarning

        unit = _make_box_turtle()
        unit.afc.homing_enabled = False
        unit.afc.error = MagicMock()
        unit.afc.function = MagicMock()

        lane = TestCalibrateBowdenNegativeDistance()._make_lane_for_calibration()
        lane.extruder_obj.tool_start = None
        lane.extruder_obj.fila_tool_start = MagicMock()
        unit.calc_position = MagicMock(return_value=(10, "retract from toolhead sensor", True))

        unit.calibrate_bowden(lane, dis=1, tol=1)

        # The retry loop's homed move only runs when the presence check
        # passes; with tool_start None this proves fila_tool_start was
        # consulted instead of short-circuiting as "no sensor configured".
        lane.move_to.assert_called()

    def test_no_sensor_at_all_skips_the_retry_loop(self):
        unit = _make_box_turtle()
        unit.afc.homing_enabled = False
        unit.afc.error = MagicMock()
        unit.afc.function = MagicMock()

        lane = TestCalibrateBowdenNegativeDistance()._make_lane_for_calibration()
        lane.extruder_obj.tool_start = None
        lane.extruder_obj.fila_tool_start = None
        unit.calc_position = MagicMock(return_value=(0, "retract from toolhead sensor", True))

        unit.calibrate_bowden(lane, dis=1, tol=1)

        lane.move_to.assert_not_called()


class TestCalibrateBowdenBufferExtended:
    """Regression coverage for AFCProject/AFC-Klipper-Add-On#742: an already
    extended buffer made the toolhead-confirm loop exit immediately with
    bow_pos=0, which then surfaced as the unrelated, confusing
    "Failed to home filament back to hub" message instead of a clear one."""

    def test_buffer_already_extended_returns_before_entering_retry_loop(self):
        unit = _make_box_turtle()
        unit.afc.homing_enabled = False
        unit.afc.error = MagicMock()
        unit.afc.function = MagicMock()

        lane = TestCalibrateBowdenNegativeDistance()._make_lane_for_calibration()
        lane.extruder_obj.tool_start = "buffer"
        lane.buffer_obj = MagicMock()
        lane.buffer_obj.name = "TN1"
        lane.buffer_obj.advance_state = True
        unit.calc_position = MagicMock(return_value=(0, "retract to extruder", True))

        success, message, pos = unit.calibrate_bowden(lane, dis=1, tol=1)

        msg = "TN1 buffer is already extended, please check buffer sensors and rerun calibration"

        assert success is False
        assert message == msg
        assert pos == 0
        lane.move_to.assert_not_called()

    def test_buffer_not_extended_enters_the_retry_loop(self):
        unit = _make_box_turtle()
        unit.afc.homing_enabled = False
        unit.afc.error = MagicMock()
        unit.afc.function = MagicMock()

        lane = TestCalibrateBowdenNegativeDistance()._make_lane_for_calibration()
        lane.extruder_obj.tool_start = "buffer"
        lane.buffer_obj = MagicMock()
        lane.buffer_obj.name = "TN1"
        lane.buffer_obj.advance_state = False
        unit.calc_position = MagicMock(return_value=(10, "retract from toolhead sensor", True))

        unit.calibrate_bowden(lane, dis=1, tol=1)

        lane.move_to.assert_called()

    def test_extended_buffer_state_ignored_when_tool_start_is_not_buffer(self):
        unit = _make_box_turtle()
        unit.afc.homing_enabled = False
        unit.afc.error = MagicMock()
        unit.afc.function = MagicMock()

        lane = TestCalibrateBowdenNegativeDistance()._make_lane_for_calibration()
        lane.extruder_obj.tool_start = "toolhead"
        lane.buffer_obj = MagicMock()
        lane.buffer_obj.advance_state = True
        unit.calc_position = MagicMock(return_value=(10, "retract from toolhead sensor", True))

        unit.calibrate_bowden(lane, dis=1, tol=1)

        lane.move_to.assert_called()


class TestCalibrateBowdenFaultDistance:
    """Regression coverage: reaching fault_dis inside the toolhead-confirm
    retry loop must not fault when the move that reached it also homed
    successfully and the toolhead sensor is now triggered, since that is a
    successful calibration rather than a real failure to reach the sensor."""

    def _make_lane_for_fault_dis(self, dist_hub=900):
        lane = TestCalibrateBowdenNegativeDistance()._make_lane_for_calibration(dist_hub=dist_hub)
        lane.extruder_obj.tool_start = "toolhead"
        return lane

    def test_homed_and_sensor_triggered_succeeds_despite_reaching_fault_dis(self):
        from extras.AFC_lane import AFCMoveWarning

        unit = _make_box_turtle()
        unit.afc.homing_enabled = True
        unit.afc.error = MagicMock()
        unit.afc.function = MagicMock()

        lane = self._make_lane_for_fault_dis(dist_hub=900)
        # fault_dis = dist_hub + 500 = 1400; a single homed move reaches it exactly.
        lane.move_to.return_value = (True, 1400, AFCMoveWarning.NONE)
        lane.get_toolhead_pre_sensor_state.side_effect = [False, True]

        success, message, bowden_dist = unit.calibrate_bowden(lane, dis=1, tol=1)

        assert success is True
        assert message == "dist_hub successful"
        assert bowden_dist == 1400

    def test_homed_but_sensor_not_triggered_faults_at_fault_dis(self):
        from extras.AFC_lane import AFCMoveWarning

        unit = _make_box_turtle()
        unit.afc.homing_enabled = True
        unit.afc.error = MagicMock()
        unit.afc.function = MagicMock()

        lane = self._make_lane_for_fault_dis(dist_hub=900)
        lane.move_to.return_value = (True, 1400, AFCMoveWarning.NONE)
        lane.get_toolhead_pre_sensor_state.side_effect = [False, False]

        success, message, bow_pos = unit.calibrate_bowden(lane, dis=1, tol=1)

        assert success is False
        assert "while moving to toolhead" in message
        assert bow_pos == 1400

    def test_homing_never_occurred_faults_at_fault_dis_even_if_sensor_would_read_true(self):
        from extras.AFC_lane import AFCMoveWarning

        unit = _make_box_turtle()
        unit.afc.homing_enabled = True
        unit.afc.error = MagicMock()
        unit.afc.function = MagicMock()

        lane = self._make_lane_for_fault_dis(dist_hub=900)
        lane.move_to.return_value = (False, 1400, AFCMoveWarning.NONE)
        # Second value would report "triggered" if it were ever read, proving
        # the fault fires purely off homed being False, via the `and` short-circuit.
        lane.get_toolhead_pre_sensor_state.side_effect = [False, True]

        success, message, bow_pos = unit.calibrate_bowden(lane, dis=1, tol=1)

        assert success is False
        assert "while moving to toolhead" in message
        assert bow_pos == 1400
        assert lane.get_toolhead_pre_sensor_state.call_count == 1

    def test_homed_within_overshoot_past_fault_dis_still_succeeds(self):
        """A homed move can legitimately land past fault_dis by up to
        homing_overshoot and still be a real successful load: dist_hub=1092
        gives fault_dis=1592, and a homed distance of 1600 is still within
        dist_hub + 500 + homing_overshoot(50) = 1642."""
        from extras.AFC_lane import AFCMoveWarning

        unit = _make_box_turtle()
        unit.afc.homing_enabled = True
        unit.afc.error = MagicMock()
        unit.afc.function = MagicMock()

        lane = self._make_lane_for_fault_dis(dist_hub=1092)
        lane.move_to.return_value = (True, 1600, AFCMoveWarning.NONE)
        lane.get_toolhead_pre_sensor_state.side_effect = [False, True]

        success, message, bowden_dist = unit.calibrate_bowden(lane, dis=1, tol=1)

        assert success is True
        assert message == "dist_hub successful"
        assert bowden_dist == 1600


class TestCalibrateTd1:
    """Regression coverage for AFCProject/AFC-Klipper-Add-On#799: a failed
    move_to_hub/move_to_load result was ignored, letting calibrate_td1 reset
    position, rewrite config, and report success after a failed move."""

    def _make_lane_for_td1(self, is_direct_dist, dist_hub=900):
        lane = MagicMock()
        lane.name = "lane1"
        lane.fullname = "AFC_stepper lane1"
        lane.td1_device_id = "SN1"
        lane.dist_hub = dist_hub
        lane.short_move_dis = 10
        lane.short_moves_speed = 50
        lane.short_moves_accel = 100
        lane.td1_bowden_length = 0
        lane.is_direct_dist.return_value = is_direct_dist
        lane.is_direct_hub.return_value = True
        lane.hub_obj.afc_bowden_length = dist_hub
        lane.hub_obj.fullname = "AFC_hub hub1"
        return lane

    def _make_unit_for_td1(self):
        unit = _make_box_turtle()
        unit.afc.homing_enabled = False
        unit.afc.function = MagicMock()
        unit.afc.function.check_for_td1_id.return_value = (True, "")
        unit.get_td1_data = MagicMock(return_value=True)
        return unit

    def test_move_to_hub_failure_returns_early_and_disables_lane(self):
        unit = self._make_unit_for_td1()
        lane = self._make_lane_for_td1(is_direct_dist=False)
        lane.unit_obj.move_to_hub.return_value = (False, 0, None)

        success, message, pos = unit.calibrate_td1(lane, dis=1, tol=1)

        assert success is False
        assert message == "Failed to home filament back to hub"
        assert pos == 0
        lane.do_enable.assert_called_with(False)
        unit.afc.function.ConfigRewrite.assert_not_called()

    def test_move_to_load_failure_returns_early_and_disables_lane(self):
        unit = self._make_unit_for_td1()
        lane = self._make_lane_for_td1(is_direct_dist=True)
        lane.unit_obj.move_to_load.return_value = (False, 0, None)

        success, message, pos = unit.calibrate_td1(lane, dis=1, tol=1)

        assert success is False
        assert message == "Failed to home filament back to hub"
        assert pos == 0
        lane.do_enable.assert_called_with(False)
        unit.afc.function.ConfigRewrite.assert_not_called()

    def test_move_to_hub_success_continues_to_completion(self):
        unit = self._make_unit_for_td1()
        unit.calc_position = MagicMock(return_value=(0, "Moving to hub", True))
        lane = self._make_lane_for_td1(is_direct_dist=False)
        # is_direct_hub=False so `checkpoint` is assigned via move_until_state before
        # the post-move "Reset to hub" calc_position call reads it.
        lane.is_direct_hub.return_value = False
        unit.move_until_state = MagicMock(return_value=(0, "Moving to hub", True))
        lane.unit_obj.move_to_hub.return_value = (True, 0, None)

        success, message, pos = unit.calibrate_td1(lane, dis=1, tol=1)

        assert success is True
        assert message == "td1_bowden_length calibration successful"
        unit.afc.function.ConfigRewrite.assert_called_once_with(
            lane.hub_obj.fullname, "td1_bowden_length", pos,
            f"\n td1_bowden_length: New: {pos} Old: 0"
        )

    def test_move_to_load_success_continues_to_completion(self):
        unit = self._make_unit_for_td1()
        lane = self._make_lane_for_td1(is_direct_dist=True)
        lane.unit_obj.move_to_load.return_value = (True, 0, None)

        success, message, pos = unit.calibrate_td1(lane, dis=1, tol=1)

        assert success is True
        assert message == "td1_bowden_length calibration successful"
        assert lane.td1_bowden_length == pos
        unit.afc.function.ConfigRewrite.assert_called_once_with(
            lane.fullname, "td1_bowden_length", pos,
            f"\n td1_bowden_length: New: {pos} Old: 0"
        )