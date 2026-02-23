"""
Unit tests for extras/AFC_stepper.py

Covers:
  - AFCExtruderStepper: pure helper methods that don't need hardware
  - dwell: increments next_cmd_time by max(0, delay)
  - set_position: always sets _manual_axis_pos to 0.0
  - get_position: delegates to stepper get_commanded_position
  - _set_current: calls gcode script when tmc_print_current is set
  - set_load_current / set_print_current: call _set_current with correct value
  - update_rotation_distance: calls set_rotation_distance with base/multiplier
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from extras.AFC_stepper import AFCExtruderStepper


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_stepper(name="lane1"):
    """Build an AFCExtruderStepper bypassing the complex __init__."""
    stepper = AFCExtruderStepper.__new__(AFCExtruderStepper)

    from tests.conftest import MockAFC, MockPrinter, MockLogger, MockGcode

    afc = MockAFC()
    afc.logger = MockLogger()
    printer = MockPrinter(afc=afc)

    stepper.printer = printer
    stepper.afc = afc
    stepper.logger = afc.logger
    stepper.name = name
    stepper.fullname = name
    stepper.next_cmd_time = 0.0
    stepper._manual_axis_pos = 0.0

    # Extruder stepper mock
    stepper.extruder_stepper = MagicMock()
    stepper.extruder_stepper.stepper.get_commanded_position.return_value = 42.0
    stepper.extruder_stepper.stepper.get_rotation_distance.return_value = (8.0, 200)

    stepper.base_rotation_dist = 8.0

    # TMC / current settings
    stepper.tmc_print_current = None
    stepper.tmc_load_current = None

    # Gcode mock
    stepper.gcode = MockGcode()
    stepper.gcode.run_script_from_command = MagicMock()

    return stepper


# ── dwell ─────────────────────────────────────────────────────────────────────

class TestDwell:
    def test_dwell_increments_next_cmd_time(self):
        s = _make_stepper()
        s.next_cmd_time = 10.0
        s.dwell(0.5)
        assert s.next_cmd_time == 10.5

    def test_dwell_zero_delay_no_change(self):
        s = _make_stepper()
        s.next_cmd_time = 5.0
        s.dwell(0.0)
        assert s.next_cmd_time == 5.0

    def test_dwell_negative_delay_no_change(self):
        """Negative delay is clamped to 0 via max(0., delay)."""
        s = _make_stepper()
        s.next_cmd_time = 5.0
        s.dwell(-1.0)
        assert s.next_cmd_time == 5.0

    def test_dwell_accumulates(self):
        s = _make_stepper()
        s.next_cmd_time = 0.0
        s.dwell(1.0)
        s.dwell(2.0)
        assert s.next_cmd_time == 3.0


# ── set_position ──────────────────────────────────────────────────────────────

class TestSetPosition:
    def test_set_position_always_zeros_manual_axis_pos(self):
        s = _make_stepper()
        s._manual_axis_pos = 99.0
        s.set_position([10.0, 0., 0., 0.], "x")
        assert s._manual_axis_pos == 0.0

    def test_set_position_ignores_newpos(self):
        """Regardless of newpos, _manual_axis_pos is reset to 0."""
        s = _make_stepper()
        s.set_position([500.0], "")
        assert s._manual_axis_pos == 0.0

# ── get_position ──────────────────────────────────────────────────────────────

class TestGetPosition:
    def test_returns_list_of_four_floats(self):
        s = _make_stepper()
        pos = s.get_position()
        assert isinstance(pos, list)
        assert len(pos) == 4

    def test_first_element_is_stepper_position(self):
        s = _make_stepper()
        s.extruder_stepper.stepper.get_commanded_position.return_value = 123.0
        pos = s.get_position()
        assert pos[0] == 123.0

    def test_other_elements_are_zero(self):
        s = _make_stepper()
        pos = s.get_position()
        assert pos[1] == 0.0
        assert pos[2] == 0.0
        assert pos[3] == 0.0


# ── _set_current ──────────────────────────────────────────────────────────────

class TestSetCurrent:
    def test_no_script_when_tmc_print_current_is_none(self):
        s = _make_stepper()
        s.tmc_print_current = None
        s._set_current(1.0)
        s.gcode.run_script_from_command.assert_not_called()

    def test_no_script_when_current_arg_is_none(self):
        s = _make_stepper()
        s.tmc_print_current = 0.8
        s._set_current(None)
        s.gcode.run_script_from_command.assert_not_called()

    def test_runs_script_when_both_set(self):
        s = _make_stepper()
        s.tmc_print_current = 0.8
        s._set_current(0.6)
        s.gcode.run_script_from_command.assert_called_once()

    def test_script_contains_stepper_name(self):
        s = _make_stepper(name="lane1")
        s.tmc_print_current = 0.8
        s._set_current(0.6)
        script = s.gcode.run_script_from_command.call_args[0][0]
        assert "lane1" in script

    def test_script_contains_current_value(self):
        s = _make_stepper()
        s.tmc_print_current = 0.8
        s._set_current(0.6)
        script = s.gcode.run_script_from_command.call_args[0][0]
        assert "0.6" in script


# ── set_load_current / set_print_current ─────────────────────────────────────

class TestCurrentHelpers:
    def test_set_load_current_calls_set_current_with_load_value(self):
        s = _make_stepper()
        s.tmc_print_current = 0.8
        s.tmc_load_current = 1.2
        s._set_current = MagicMock()
        s.set_load_current()
        s._set_current.assert_called_once_with(1.2)

    def test_set_print_current_calls_set_current_with_print_value(self):
        s = _make_stepper()
        s.tmc_print_current = 0.5
        s.tmc_load_current = 1.0
        s._set_current = MagicMock()
        s.set_print_current()
        s._set_current.assert_called_once_with(0.5)


# ── update_rotation_distance ──────────────────────────────────────────────────

class TestUpdateRotationDistance:
    def test_sets_rotation_distance_to_base_divided_by_multiplier(self):
        s = _make_stepper()
        s.base_rotation_dist = 8.0
        s.update_rotation_distance(2.0)
        s.extruder_stepper.stepper.set_rotation_distance.assert_called_once_with(4.0)

    def test_multiplier_of_one_restores_base(self):
        s = _make_stepper()
        s.base_rotation_dist = 8.0
        s.update_rotation_distance(1.0)
        s.extruder_stepper.stepper.set_rotation_distance.assert_called_once_with(8.0)

    def test_multiplier_of_four_quarters_distance(self):
        s = _make_stepper()
        s.base_rotation_dist = 8.0
        s.update_rotation_distance(4.0)
        s.extruder_stepper.stepper.set_rotation_distance.assert_called_once_with(2.0)


# ── get_kinematics / get_steppers / calc_position ────────────────────────────

class TestKinematicsShims:
    def test_get_kinematics_returns_self(self):
        s = _make_stepper()
        assert s.get_kinematics() is s

    def test_get_steppers_returns_list_with_stepper(self):
        s = _make_stepper()
        steppers = s.get_steppers()
        assert isinstance(steppers, list)
        assert len(steppers) == 1
        assert steppers[0] is s.extruder_stepper.stepper


# ── sync_print_time ───────────────────────────────────────────────────────────

def _make_stepper_with_toolhead(next_cmd_time=0.0, last_move_time=0.0):
    """Build a stepper with a mocked toolhead for sync_print_time tests."""
    s = _make_stepper()
    s.next_cmd_time = next_cmd_time

    toolhead = MagicMock()
    toolhead.get_last_move_time.return_value = last_move_time
    s.printer._objects["toolhead"] = toolhead
    s.printer.lookup_object = lambda name, default=None: (
        toolhead if name == "toolhead" else (s.afc if name == "AFC" else MagicMock())
    )
    s._toolhead = toolhead
    return s


class TestSyncPrintTime:
    def test_no_dwell_when_next_cmd_time_is_behind(self):
        """When next_cmd_time < print_time, update next_cmd_time to print_time."""
        s = _make_stepper_with_toolhead(next_cmd_time=0.0, last_move_time=5.0)
        s.sync_print_time()
        assert s.next_cmd_time == 5.0

    def test_dwell_called_when_next_cmd_time_is_ahead(self):
        """When next_cmd_time > print_time, toolhead.dwell is called."""
        s = _make_stepper_with_toolhead(next_cmd_time=10.0, last_move_time=5.0)
        s.sync_print_time()
        s._toolhead.dwell.assert_called_once_with(5.0)

    def test_no_dwell_when_times_equal(self):
        """When next_cmd_time == print_time, no dwell and no update needed."""
        s = _make_stepper_with_toolhead(next_cmd_time=5.0, last_move_time=5.0)
        s.sync_print_time()
        s._toolhead.dwell.assert_not_called()
        assert s.next_cmd_time == 5.0


# ── flush_step_generation ─────────────────────────────────────────────────────

class TestFlushStepGeneration:
    def test_delegates_to_sync_print_time(self):
        s = _make_stepper_with_toolhead(next_cmd_time=0.0, last_move_time=7.0)
        s.flush_step_generation()
        # After sync, next_cmd_time should be updated to print_time
        assert s.next_cmd_time == 7.0


# ── get_last_move_time ────────────────────────────────────────────────────────

class TestGetLastMoveTime:
    def test_returns_next_cmd_time_after_sync(self):
        s = _make_stepper_with_toolhead(next_cmd_time=0.0, last_move_time=3.5)
        result = s.get_last_move_time()
        assert result == 3.5

    def test_returns_next_cmd_time_when_ahead(self):
        s = _make_stepper_with_toolhead(next_cmd_time=8.0, last_move_time=3.0)
        result = s.get_last_move_time()
        assert result == 8.0


# ── sync_to_extruder / unsync_to_extruder ────────────────────────────────────

class TestSyncUnsync:
    def test_sync_calls_extruder_stepper_sync(self):
        s = _make_stepper()
        s.extruder_name = "extruder"
        s._set_current = MagicMock()
        s.set_print_current = MagicMock()
        s.sync_to_extruder(update_current=False)
        s.extruder_stepper.sync_to_extruder.assert_called_once_with("extruder")

    def test_sync_calls_set_print_current_when_update_current(self):
        s = _make_stepper()
        s.extruder_name = "extruder"
        s.set_print_current = MagicMock()
        s.sync_to_extruder(update_current=True)
        s.set_print_current.assert_called_once()

    def test_sync_skips_set_print_current_when_update_current_false(self):
        s = _make_stepper()
        s.extruder_name = "extruder"
        s.set_print_current = MagicMock()
        s.sync_to_extruder(update_current=False)
        s.set_print_current.assert_not_called()

    def test_sync_uses_override_extruder_name(self):
        s = _make_stepper()
        s.extruder_name = "extruder"
        s.set_print_current = MagicMock()
        s.sync_to_extruder(update_current=False, extruder_name="extruder2")
        s.extruder_stepper.sync_to_extruder.assert_called_once_with("extruder2")

    def test_unsync_calls_sync_with_none(self):
        s = _make_stepper()
        s.set_load_current = MagicMock()
        s.unsync_to_extruder(update_current=False)
        s.extruder_stepper.sync_to_extruder.assert_called_once_with(None)

    def test_unsync_calls_set_load_current_when_update_current(self):
        s = _make_stepper()
        s.set_load_current = MagicMock()
        s.unsync_to_extruder(update_current=True)
        s.set_load_current.assert_called_once()

    def test_unsync_skips_set_load_current_when_update_current_false(self):
        s = _make_stepper()
        s.set_load_current = MagicMock()
        s.unsync_to_extruder(update_current=False)
        s.set_load_current.assert_not_called()
