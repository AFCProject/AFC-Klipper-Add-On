"""
Unit tests for extras/AFC_error.py

Covers:
  - set_error_state: sets error_state and current_state on AFC
  - reset_failure: resets error_state, pause, position_saved, in_toolchange
  - PauseUserIntervention: only pauses when homed and not already paused
  - pause_print: calls PAUSE script
  - handle_lane_failure: disables stepper, sets lane status, calls AFC_error
  - AFC_error: logs error, optionally calls pause_print
"""

from __future__ import annotations

import sys
import itertools
import importlib.util
import configparser
from unittest.mock import MagicMock, patch, call, PropertyMock
import pytest

from extras.AFC_error import afcError, load_config


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_afc_error():
    """Create an afcError instance through its real __init__ and
    handle_connect (fired via the klippy:connect event), mocking only the
    Klipper collaborators (config/printer/AFC/reactor)."""
    from extras.AFC_error import afcError
    from extras.AFC import State
    from tests.conftest import MockAFC, MockPrinter, MockConfig

    afc = MockAFC()
    afc.error_state = False
    afc.current_state = State.IDLE
    afc.function = MagicMock()
    afc.function.is_homed = MagicMock(return_value=True)
    afc.function.is_paused = MagicMock(return_value=False)
    afc.save_pos = MagicMock()
    afc.save_vars = MagicMock()

    pause_resume = MagicMock()
    idle_timeout = MagicMock()
    idle_timeout.idle_timeout = 600

    printer = MockPrinter(afc=afc)
    printer._objects["pause_resume"] = pause_resume
    printer._objects["idle_timeout"] = idle_timeout

    config = MockConfig(name="AFC_error", printer=printer)
    err = afcError(config)
    printer.send_event("klippy:connect")

    return err, afc


def _make_real_unit_obj(afc):
    """Build a real afcUnit via its actual __init__, reusing the given afc
    object so lane.unit_obj.afc is the same instance the test already
    wired up, for tests that need lane_fault()'s LED logic to run for
    real rather than as a mocked call."""
    from extras.AFC_unit import afcUnit
    from tests.conftest import MockConfig, MockPrinter

    printer = MockPrinter(afc=afc)
    config = MockConfig(name="afcUnit test_unit", printer=printer)
    return afcUnit(config)


# ── load_config ───────────────────────────────────────────────────────────────

class TestLoadConfig:
    def test_load_config_returns_afc_error_instance(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_error", printer=printer)
        result = load_config(config)
        assert isinstance(result, afcError)


# ── afcError.__init__ and handle_connect ──────────────────────────────────────

class TestAfcErrorInit:
    def test_init_sets_error_log_empty(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_error", printer=printer)
        err = afcError(config)
        assert err.errorLog == {}

    def test_init_sets_pause_false(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_error", printer=printer)
        err = afcError(config)
        assert err.pause is False

    def test_init_registers_klippy_connect_handler(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_error", printer=printer)
        err = afcError(config)
        assert "klippy:connect" in printer._event_handlers


class TestAfcErrorHandleConnect:
    def test_handle_connect_sets_afc(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_error", printer=printer)
        err = afcError(config)
        printer.send_event("klippy:connect")
        assert err.afc is afc

    def test_handle_connect_sets_logger(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_error", printer=printer)
        err = afcError(config)
        printer.send_event("klippy:connect")
        assert err.logger is afc.logger

    def test_handle_connect_registers_reset_failure_command(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_error", printer=printer)
        err = afcError(config)
        printer.send_event("klippy:connect")
        assert "RESET_FAILURE" in afc.gcode._commands

    def test_handle_connect_registers_afc_resume_command(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_error", printer=printer)
        err = afcError(config)
        printer.send_event("klippy:connect")
        assert "AFC_RESUME" in afc.gcode._commands

    def test_handle_connect_sets_rename_macros(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_error", printer=printer)
        err = afcError(config)
        printer.send_event("klippy:connect")
        assert err.BASE_RESUME_NAME == "RESUME"
        assert "_AFC_RENAMED_RESUME_" in err.AFC_RENAME_RESUME_NAME


# ── set_error_state ───────────────────────────────────────────────────────────

class TestSetErrorState:
    def test_set_true_sets_error_state(self):
        err, afc = _make_afc_error()
        err.set_error_state(True)
        assert afc.error_state is True

    def test_set_true_changes_current_state_to_error(self):
        from extras.AFC import State
        err, afc = _make_afc_error()
        err.set_error_state(True)
        assert afc.current_state == State.ERROR

    def test_set_false_clears_error_state(self):
        err, afc = _make_afc_error()
        afc.error_state = True
        err.set_error_state(False)
        assert afc.error_state is False

    def test_set_false_changes_current_state_to_idle(self):
        from extras.AFC import State
        err, afc = _make_afc_error()
        afc.error_state = True
        err.set_error_state(False)
        assert afc.current_state == State.IDLE

    def test_set_true_when_not_yet_error_calls_save_pos(self):
        err, afc = _make_afc_error()
        afc.error_state = False
        err.set_error_state(True)
        afc.save_pos.assert_called_once()

    def test_set_true_when_already_error_does_not_duplicate_save_pos(self):
        err, afc = _make_afc_error()
        afc.error_state = True
        err.set_error_state(True)
        afc.save_pos.assert_not_called()

    def test_set_false_does_not_call_save_pos(self):
        """state=False alone must skip save_pos, even though `not
        afc.error_state` is satisfied (error_state is False beforehand)."""
        err, afc = _make_afc_error()
        afc.error_state = False
        err.set_error_state(False)
        afc.save_pos.assert_not_called()


# ── reset_failure ─────────────────────────────────────────────────────────────

class TestResetFailure:
    def test_reset_failure_clears_error_state(self):
        err, afc = _make_afc_error()
        afc.error_state = True
        err.reset_failure()
        assert afc.error_state is False

    def test_reset_failure_clears_pause_flag(self):
        err, afc = _make_afc_error()
        err.pause = True
        err.reset_failure()
        assert err.pause is False

    def test_reset_failure_clears_position_saved(self):
        err, afc = _make_afc_error()
        afc.position_saved = True
        err.reset_failure()
        assert afc.position_saved is False

    def test_reset_failure_clears_in_toolchange(self):
        err, afc = _make_afc_error()
        afc.in_toolchange = True
        err.reset_failure()
        assert afc.in_toolchange is False

    def test_reset_failure_logs_debug(self):
        err, afc = _make_afc_error()
        err.reset_failure()
        assert err.logger.messages == [("debug", "Resetting failures")]


# ── PauseUserIntervention ─────────────────────────────────────────────────────

class TestPauseUserIntervention:
    def test_pause_when_homed_and_not_paused(self):
        err, afc = _make_afc_error()
        err.pause = True
        err.pause_print = MagicMock()
        afc.function.is_homed.return_value = True
        afc.function.is_paused.return_value = False
        err.PauseUserIntervention("Some message")
        err.pause_print.assert_called_once()

    def test_no_pause_when_not_homed(self):
        err, afc = _make_afc_error()
        err.pause = True
        err.pause_print = MagicMock()
        afc.function.is_homed.return_value = False
        err.PauseUserIntervention("Some message")
        err.pause_print.assert_not_called()

    def test_no_pause_when_already_paused(self):
        err, afc = _make_afc_error()
        err.pause = True
        err.pause_print = MagicMock()
        afc.function.is_homed.return_value = True
        afc.function.is_paused.return_value = True
        err.PauseUserIntervention("Some message")
        err.pause_print.assert_not_called()

    def test_error_is_logged(self):
        err, afc = _make_afc_error()
        err.pause_print = MagicMock()
        afc.function.is_homed.return_value = True
        afc.function.is_paused.return_value = False
        err.PauseUserIntervention("Bad thing happened")
        assert err.logger.messages == [("error", "Bad thing happened")]


# ── pause_print ───────────────────────────────────────────────────────────────

class TestPausePrint:
    def test_pause_print_calls_gcode_pause(self):
        err, afc = _make_afc_error()
        err.set_error_state = MagicMock()
        afc.function.log_toolhead_pos = MagicMock()
        afc.gcode.run_script_from_command = MagicMock()
        err.pause_print()
        afc.gcode.run_script_from_command.assert_called()
        script_arg = afc.gcode.run_script_from_command.call_args[0][0]
        assert "PAUSE" in script_arg

    def test_pause_print_sets_error_state(self):
        err, afc = _make_afc_error()
        err.set_error_state = MagicMock()
        afc.function.log_toolhead_pos = MagicMock()
        afc.gcode.run_script_from_command = MagicMock()
        err.pause_print()
        err.set_error_state.assert_called_once_with(True)

    def test_pause_print_logs_pausing_and_after_pause(self):
        err, afc = _make_afc_error()
        err.set_error_state = MagicMock()
        afc.function.log_toolhead_pos = MagicMock()
        afc.gcode.run_script_from_command = MagicMock()
        err.pause_print()
        assert err.logger.messages == [
            ("info", "PAUSING"),
            ("debug", "After User Pause"),
        ]


# ── handle_lane_failure ───────────────────────────────────────────────────────

class TestHandleLaneFailure:
    def test_disables_lane_stepper(self):
        from extras.AFC_lane import AFCLaneState
        err, afc = _make_afc_error()
        err.AFC_error = MagicMock()
        cur_lane = MagicMock()
        cur_lane.name = "lane1"
        cur_lane.do_enable = MagicMock()
        cur_lane.led_index = "1"
        err.handle_lane_failure(cur_lane, "jammed", pause=False)
        cur_lane.do_enable.assert_called_once_with(False)

    def test_sets_lane_status_to_error(self):
        from extras.AFC_lane import AFCLaneState
        err, afc = _make_afc_error()
        err.AFC_error = MagicMock()
        cur_lane = MagicMock()
        cur_lane.name = "lane1"
        err.handle_lane_failure(cur_lane, "jammed", pause=False)
        assert cur_lane.status == AFCLaneState.ERROR

    def test_calls_lane_fault(self):
        err, afc = _make_afc_error()
        err.AFC_error = MagicMock()
        cur_lane = MagicMock()
        cur_lane.name = "lane1"
        err.handle_lane_failure(cur_lane, "jammed", pause=False)
        cur_lane.unit_obj.lane_fault.assert_called_once_with(cur_lane)

    def test_calls_afc_error_with_lane_name_in_message(self):
        err, afc = _make_afc_error()
        err.AFC_error = MagicMock()
        cur_lane = MagicMock()
        cur_lane.name = "lane2"
        cur_lane.led_index = "2"
        err.handle_lane_failure(cur_lane, "overheated", pause=False)
        called_msg = err.AFC_error.call_args[0][0]
        assert "lane2" in called_msg
        assert "overheated" in called_msg

    def test_calls_afc_error_with_real_caller_function_name(self):
        """Frame inspection resolves stack_name to the name of the function
        that called handle_lane_failure."""
        err, afc = _make_afc_error()
        err.AFC_error = MagicMock()
        cur_lane = MagicMock()
        cur_lane.name = "lane2"
        cur_lane.led_index = "2"
        err.handle_lane_failure(cur_lane, "overheated", pause=False)
        assert err.AFC_error.call_args.kwargs["stack_name"] == \
            "test_calls_afc_error_with_real_caller_function_name"

    def test_stack_name_falls_back_to_empty_when_currentframe_is_none(self):
        """inspect.currentframe() returning None (interpreters without frame
        support) must fall back to an empty stack_name, not crash on
        frame.f_back."""
        err, afc = _make_afc_error()
        err.AFC_error = MagicMock()
        cur_lane = MagicMock()
        cur_lane.name = "lane2"
        cur_lane.led_index = "2"
        with patch("extras.AFC_error.inspect.currentframe", return_value=None):
            err.handle_lane_failure(cur_lane, "overheated", pause=False)
        assert err.AFC_error.call_args.kwargs["stack_name"] == ""

    def test_stack_name_falls_back_to_empty_when_no_caller_frame(self):
        """A frame with no caller (f_back is None) must also fall back to an
        empty stack_name, not crash on None.f_code."""
        err, afc = _make_afc_error()
        err.AFC_error = MagicMock()
        cur_lane = MagicMock()
        cur_lane.name = "lane2"
        cur_lane.led_index = "2"
        fake_frame = MagicMock()
        fake_frame.f_back = None
        with patch("extras.AFC_error.inspect.currentframe", return_value=fake_frame):
            err.handle_lane_failure(cur_lane, "overheated", pause=False)
        assert err.AFC_error.call_args.kwargs["stack_name"] == ""


# ── AFC_error (the method) ────────────────────────────────────────────────────

class TestAFCErrorMethod:
    def test_logs_error_message(self):
        err, afc = _make_afc_error()
        err.pause_print = MagicMock()
        err.AFC_error("Catastrophic failure", pause=False)
        assert err.logger.messages == [("error", "Catastrophic failure")]

    def test_explicit_stack_name_skips_frame_inspection(self):
        """A caller-supplied stack_name is passed through as-is, instead of
        being overwritten by inspect.currentframe() lookup."""
        err, afc = _make_afc_error()
        err.pause_print = MagicMock()
        err.logger = MagicMock()
        err.AFC_error("Uh oh", pause=False, stack_name="custom_caller")
        err.logger.error.assert_called_once_with(message="Uh oh", stack_name="custom_caller")

    def test_no_stack_name_uses_real_caller_function_name(self):
        """stack_name=None triggers frame inspection, resolving to the name
        of the function that called AFC_error."""
        err, afc = _make_afc_error()
        err.pause_print = MagicMock()
        err.logger = MagicMock()
        err.AFC_error("Uh oh", pause=False)
        err.logger.error.assert_called_once_with(
            message="Uh oh",
            stack_name="test_no_stack_name_uses_real_caller_function_name",
        )

    def test_no_stack_name_falls_back_to_empty_when_currentframe_is_none(self):
        """inspect.currentframe() returning None (interpreters without frame
        support) must fall back to an empty stack_name, not crash on
        frame.f_back."""
        err, afc = _make_afc_error()
        err.pause_print = MagicMock()
        err.logger = MagicMock()
        with patch("extras.AFC_error.inspect.currentframe", return_value=None):
            err.AFC_error("Uh oh", pause=False)
        err.logger.error.assert_called_once_with(message="Uh oh", stack_name="")

    def test_no_stack_name_falls_back_to_empty_when_no_caller_frame(self):
        """A frame with no caller (f_back is None) must also fall back to an
        empty stack_name, not crash on None.f_code."""
        err, afc = _make_afc_error()
        err.pause_print = MagicMock()
        err.logger = MagicMock()
        fake_frame = MagicMock()
        fake_frame.f_back = None
        with patch("extras.AFC_error.inspect.currentframe", return_value=fake_frame):
            err.AFC_error("Uh oh", pause=False)
        err.logger.error.assert_called_once_with(message="Uh oh", stack_name="")

    def test_pause_true_calls_pause_print(self):
        err, afc = _make_afc_error()
        err.pause_print = MagicMock()
        err.AFC_error("Uh oh", pause=True)
        err.pause_print.assert_called_once()

    def test_pause_false_skips_pause_print(self):
        err, afc = _make_afc_error()
        err.pause_print = MagicMock()
        err.AFC_error("Uh oh", pause=False)
        err.pause_print.assert_not_called()


# ── cmd_RESET_FAILURE ─────────────────────────────────────────────────────────

class TestCmdResetFailure:
    def test_delegates_to_reset_failure(self):
        err, afc = _make_afc_error()
        err.reset_failure = MagicMock()
        gcmd = MagicMock()
        err.cmd_RESET_FAILURE(gcmd)
        err.reset_failure.assert_called_once()

    def test_reset_failure_called_with_no_args(self):
        err, afc = _make_afc_error()
        err.reset_failure = MagicMock()
        err.cmd_RESET_FAILURE(MagicMock())
        err.reset_failure.assert_called_once_with()


# ── fix ───────────────────────────────────────────────────────────────────────

class TestFix:
    def test_fix_sets_pause_true(self):
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        err.ToolHeadFix = MagicMock(return_value=False)
        lane = MagicMock()
        err.fix("toolhead", lane)
        assert err.pause is True

    def test_fix_toolhead_calls_toolhead_fix(self):
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        err.ToolHeadFix = MagicMock(return_value=True)
        lane = MagicMock()
        err.fix("toolhead", lane)
        err.ToolHeadFix.assert_called_once_with(lane)

    def test_fix_toolhead_success_skips_led_fault(self):
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        err.ToolHeadFix = MagicMock(return_value=True)
        lane = MagicMock()
        err.fix("toolhead", lane)
        afc.function.afc_led.assert_not_called()

    def test_fix_toolhead_failure_calls_led_fault(self):
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        err.ToolHeadFix = MagicMock(return_value=False)
        lane = MagicMock()
        lane.led_index = "1"
        lane.unit_obj = _make_real_unit_obj(afc)
        result = err.fix("toolhead", lane)
        assert result is False
        afc.function.afc_led.assert_called_with(lane.led_fault, lane.led_index)

    def test_fix_other_problem_calls_pause_user_intervention(self):
        """A real lane object alone isn't enough to reach ToolHeadFix -
        problem must also equal 'toolhead'."""
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        err.ToolHeadFix = MagicMock()
        lane = MagicMock()
        lane.led_index = "2"
        err.fix("jam", lane)
        err.PauseUserIntervention.assert_called_with("jam")
        err.ToolHeadFix.assert_not_called()

    def test_fix_none_problem_calls_pause_user_intervention_with_unknown_message(self):
        """problem is None -> PauseUserIntervention('Paused for unknown error')
        exactly once; the elif chain must not also fall into the else branch
        and call PauseUserIntervention(None) a second time."""
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        lane = MagicMock()
        lane.led_index = "1"
        err.fix(None, lane)
        assert err.PauseUserIntervention.call_args_list == [
            call('Paused for unknown error'),
        ]

    def test_fix_returns_error_handled_from_toolhead_fix(self):
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        err.ToolHeadFix = MagicMock(return_value=True)
        lane = MagicMock()
        result = err.fix("toolhead", lane)
        assert result is True

    def test_fix_returns_false_for_non_toolhead_problem(self):
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        lane = MagicMock()
        lane.led_index = "1"
        result = err.fix("jam", lane)
        assert result is False

    def test_fix_resolves_lane_name_string_via_afc_lanes(self):
        """A string LANE argument is looked up in afc.lanes before dispatch."""
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        err.ToolHeadFix = MagicMock(return_value=False)
        lane = MagicMock()
        lane.led_index = "1"
        lane.unit_obj = _make_real_unit_obj(afc)
        afc.lanes = {"lane1": lane}
        err.fix("toolhead", "lane1")
        err.ToolHeadFix.assert_called_once_with(lane)
        afc.function.afc_led.assert_called_with(lane.led_fault, lane.led_index)

    def test_fix_unmatched_lane_name_string_does_not_crash(self):
        """A string LANE that doesn't match any key in afc.lanes resolves to
        None via dict.get()'s fallback default. fix() must treat that as a
        graceful no-match: skip ToolHeadFix (no lane to hand it), fall into
        the PauseUserIntervention(problem) branch instead, skip lane_fault
        (nothing to fault), and return False without raising."""
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        err.ToolHeadFix = MagicMock(return_value=False)
        afc.lanes = {}  # "missing_lane" is not a registered lane
        result = err.fix("toolhead", "missing_lane")
        assert result is False
        err.ToolHeadFix.assert_not_called()
        err.PauseUserIntervention.assert_called_once_with(
            "Unknown lane 'missing_lane' reported for problem: toolhead"
        )
        afc.function.afc_led.assert_not_called()


# ── ToolHeadFix ───────────────────────────────────────────────────────────────

class TestToolHeadFix:
    def test_toolhead_has_filament_matching_lane_but_not_loaded_pauses(self):
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        lane = MagicMock()
        lane.name = "lane1"
        lane.get_toolhead_pre_sensor_state.return_value = True
        lane.extruder_obj.lane_loaded = "lane1"
        lane.raw_load_state = False  # load sensor not active
        err.ToolHeadFix(lane)
        err.PauseUserIntervention.assert_called_with("Filament not loaded in Lane")

    def test_toolhead_has_filament_matching_lane_and_loaded_pauses_no_error(self):
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        lane = MagicMock()
        lane.name = "lane1"
        lane.get_toolhead_pre_sensor_state.return_value = True
        lane.extruder_obj.lane_loaded = "lane1"
        lane.raw_load_state = True
        err.ToolHeadFix(lane)
        err.PauseUserIntervention.assert_called_with("no error detected")

    def test_toolhead_has_filament_wrong_lane_pauses(self):
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        lane = MagicMock()
        lane.name = "lane1"
        lane.get_toolhead_pre_sensor_state.return_value = True
        lane.extruder_obj.lane_loaded = "lane2"  # Mismatch
        err.ToolHeadFix(lane)
        err.PauseUserIntervention.assert_called_with("laneloaded does not match extruder")

    def test_toolhead_empty_with_lane_filament_returns_true_no_homing(self):
        """Filament is retracted to lane and reloaded; returns True."""
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        afc.homing_enabled = False
        lane = _make_afc_lane()
        lane.get_toolhead_pre_sensor_state.return_value = False  # toolhead empty
        # Sequence: if check(True→enter), while check(True→loop), while check(False→exit),
        #           while-not check(False→enter), while-not check(True→exit)
        with patch.object(type(lane), "raw_load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect = [True, True, False, False, True]
            result = err.ToolHeadFix(lane)
        assert result is True
        assert err.pause is False
        afc.save_vars.assert_called_once()
        assert err.logger.messages == [
            ("info", "Retracting lane1 back to load switch"),
            ("info", "Done resetting lane1"),
        ]

    def test_toolhead_empty_with_lane_filament_clears_flags_no_homing(self):
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        afc.homing_enabled = False
        lane = _make_afc_lane()
        lane.get_toolhead_pre_sensor_state.return_value = False
        with patch.object(type(lane), "raw_load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect=[True, True, False, False, True]
            err.ToolHeadFix(lane)
        assert lane.tool_loaded is False
        assert lane.loaded_to_hub is False
        assert lane.extruder_obj.lane_loaded == None
        assert err.logger.messages == [
            ("info", "Retracting lane1 back to load switch"),
            ("info", "Done resetting lane1"),
        ]

    def test_toolhead_empty_with_lane_filament_returns_true_homing(self):
        """Filament is retracted to lane and reloaded; returns True."""
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        afc.homing_enabled = True
        lane = _make_afc_lane()
        lane.get_toolhead_pre_sensor_state.return_value = False  # toolhead empty
        lane.hub_obj = MagicMock()
        lane.hub_obj.afc_bowden_length = 1300
        # Sequence: if check(True→enter), while check(True→loop), while check(False→exit),
        #           while-not check(False→enter), while-not check(True→exit)
        with patch.object(type(lane), "raw_load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect=[True, True, False]
            result = err.ToolHeadFix(lane)
        assert result is True
        assert err.pause is False
        afc.save_vars.assert_called_once()
        assert err.logger.messages == [
            ("info", "Retracting lane1 back to load switch"),
            ("info", "Done resetting lane1"),
        ]

    def test_toolhead_empty_with_lane_filament_clears_flags_homing(self):
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        afc.homing_enabled = True
        lane = _make_afc_lane()
        lane.hub_obj = MagicMock()
        lane.hub_obj.afc_bowden_length = 1300
        lane.get_toolhead_pre_sensor_state.return_value = False
        with patch.object(type(lane), "raw_load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect=[True, True, False]
            err.ToolHeadFix(lane)
        assert lane.tool_loaded is False
        assert lane.loaded_to_hub is False
        assert lane.extruder_obj.lane_loaded == None
        assert err.logger.messages == [
            ("info", "Retracting lane1 back to load switch"),
            ("info", "Done resetting lane1"),
        ]

    def test_toolhead_empty_homing_no_hub_obj_excludes_bowden_length(self):
        """When hub_obj is None during the homing retract loop, the move
        distance must fall back to dist_hub + 500 without touching
        hub_obj.afc_bowden_length (which would crash on None)."""
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        afc.homing_enabled = True
        lane = _make_afc_lane()
        lane.get_toolhead_pre_sensor_state.return_value = False  # toolhead empty
        lane.hub_obj = None
        # Sequence: outer if check(True→enter), while check(True→loop), while check(False→exit)
        with patch.object(type(lane), "raw_load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect = [True, True, False]
            result = err.ToolHeadFix(lane)
        assert result is True
        lane.unit_obj.move_to_load.assert_called_once()
        total_move_dist = lane.unit_obj.move_to_load.call_args[0][1]
        assert total_move_dist == lane.dist_hub + 500

    def test_toolhead_empty_homing_with_hub_obj_includes_bowden_length(self):
        """When hub_obj is set, the move distance must include
        hub_obj.afc_bowden_length on top of dist_hub + 500."""
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        afc.homing_enabled = True
        lane = _make_afc_lane()
        lane.get_toolhead_pre_sensor_state.return_value = False  # toolhead empty
        lane.hub_obj = MagicMock()
        lane.hub_obj.afc_bowden_length = 1300
        # Sequence: outer if check(True→enter), while check(True→loop), while check(False→exit)
        with patch.object(type(lane), "raw_load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect = [True, True, False]
            result = err.ToolHeadFix(lane)
        assert result is True
        lane.unit_obj.move_to_load.assert_called_once()
        total_move_dist = lane.unit_obj.move_to_load.call_args[0][1]
        assert total_move_dist == 2700

    def test_toolhead_empty_with_lane_filament_returns_false_timed_out_homing(self):
        """Homing retract never sees raw_load_state clear -> 5 tries exhausted,
        pauses and returns False."""
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        afc.homing_enabled = True
        lane = _make_afc_lane()
        lane.get_toolhead_pre_sensor_state.return_value = False  # toolhead empty
        lane.hub_obj = MagicMock()
        lane.hub_obj.afc_bowden_length = 1300
        # Sequence: if check(True→enter), while check(True→loop), while check(False→exit),
        #           while-not check(False→enter), while-not check(True→exit)
        with patch.object(type(lane), "raw_load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect=[True, True, True, True, True, True]
            result = err.ToolHeadFix(lane)
        assert result is False
        assert err.pause is False
        err.PauseUserIntervention.assert_called_with("Failed to retract lane1 to load sensor")
        assert err.logger.messages == [("info", "Retracting lane1 back to load switch")]

    def test_toolhead_empty_retract_loop_times_out_no_homing(self):
        """No-homing retract loop exhausts its max_length budget without ever
        clearing raw_load_state, so it must pause and return False."""
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        afc.homing_enabled = False
        lane = _make_afc_lane()
        lane.move = MagicMock()
        lane.get_toolhead_pre_sensor_state.return_value = False  # toolhead empty
        with patch.object(type(lane), "raw_load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.return_value = True  # never clears -> loop runs until max_length exhausted
            result = err.ToolHeadFix(lane)
        assert result is False
        err.PauseUserIntervention.assert_called_with("Failed to retract lane1 to load sensor")
        assert err.logger.messages == [("info", "Retracting lane1 back to load switch")]

    def test_toolhead_empty_reload_loop_times_out_no_homing(self):
        """No-homing reload loop exhausts its max_length budget because
        raw_load_state never goes True, so it must pause and return False."""
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        afc.homing_enabled = False
        lane = _make_afc_lane()
        lane.move = MagicMock()
        lane.get_toolhead_pre_sensor_state.return_value = False  # toolhead empty
        with patch.object(type(lane), "raw_load_state", new_callable=PropertyMock) as mock_prop:
            # First read is the outer `if cur_lane.raw_load_state` guard, which
            # must be True to enter this branch at all; the first while loop's
            # own check then reads False so it exits with zero iterations; the
            # second loop ("while not raw_load_state") then never sees it go
            # True, exhausting max_length.
            mock_prop.side_effect = itertools.chain([True, False], itertools.repeat(False))
            result = err.ToolHeadFix(lane)
        assert result is False
        err.PauseUserIntervention.assert_called_with("Failed to move back lane1 to load sensor")
        assert err.logger.messages == [("info", "Retracting lane1 back to load switch")]

    def test_toolhead_empty_already_at_lane_extruder_takes_no_action(self):
        """When raw_load_state is already False, neither retract nor reload
        is needed -> no PauseUserIntervention call, no move, and the method
        returns False (falls off the end of the outer if)."""
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        lane = _make_afc_lane()
        lane.move = MagicMock()
        lane.get_toolhead_pre_sensor_state.return_value = False  # toolhead empty
        with patch.object(type(lane), "raw_load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.return_value = False
            result = err.ToolHeadFix(lane)
        assert result is False
        err.PauseUserIntervention.assert_not_called()
        lane.move.assert_not_called()
        assert err.logger.messages == []

    def test_toolhead_empty_direct_hub_takes_no_action(self):
        """raw_load_state alone isn't enough: a direct hub (is_direct_hub()
        True) skips retract/reload even though raw_load_state is True and
        tool_start != 'buffer'."""
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        lane = _make_afc_lane()
        lane.hub = "direct"  # VALID_DIRECT_HUB -> is_direct_hub() is True
        lane.move = MagicMock()
        lane.get_toolhead_pre_sensor_state.return_value = False  # toolhead empty
        with patch.object(type(lane), "raw_load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.return_value = True
            result = err.ToolHeadFix(lane)
        assert result is False
        err.PauseUserIntervention.assert_not_called()
        lane.move.assert_not_called()
        assert err.logger.messages == []

    def test_toolhead_empty_buffer_tool_start_takes_no_action(self):
        """raw_load_state and a non-direct hub alone aren't enough: a
        tool_start of 'buffer' skips retract/reload even with raw_load_state
        True and is_direct_hub() False."""
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        lane = _make_afc_lane()
        lane.hub = "PB1"  # not in VALID_DIRECT_HUB -> is_direct_hub() is False
        lane.extruder_obj.tool_start = "buffer"
        lane.move = MagicMock()
        lane.get_toolhead_pre_sensor_state.return_value = False  # toolhead empty
        with patch.object(type(lane), "raw_load_state", new_callable=PropertyMock) as mock_prop:
            mock_prop.return_value = True
            result = err.ToolHeadFix(lane)
        assert result is False
        err.PauseUserIntervention.assert_not_called()
        lane.move.assert_not_called()
        assert err.logger.messages == []


# ── cmd_AFC_RESUME ────────────────────────────────────────────────────────────

class TestCmdAfcResume:
    def test_not_paused_sets_in_toolchange_false_and_returns_early(self):
        err, afc = _make_afc_error()
        afc.in_toolchange = True
        afc.function.is_paused.return_value = False
        gcmd = MagicMock()
        err.cmd_AFC_RESUME(gcmd)
        assert afc.in_toolchange is False
        afc.gcode.run_script_from_command.assert_not_called()

    def test_not_paused_logs_debug(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = False
        err.cmd_AFC_RESUME(MagicMock())
        assert err.logger.messages == [
            ("debug", "AFC_RESUME: Printer not paused, not executing resume code"),
        ]

    def test_paused_calls_renamed_resume_macro(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand()
        err.set_error_state = MagicMock()
        err.cmd_AFC_RESUME(gcmd)
        afc.gcode.run_script_from_command.assert_called_once()
        call_arg = afc.gcode.run_script_from_command.call_args[0][0]
        assert err.AFC_RENAME_RESUME_NAME in call_arg

    def test_paused_z_below_threshold_calls_move_z_pos(self):
        """Independent coverage of the `last_position[2] <= move_z_pos`
        operand of `is_homed(for_move=True) and last_position[2] <= move_z_pos`
        (is_homed defaults to True via _make_afc_error)."""
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.z_hop = 0.5
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]  # z=0 ≤ 0+0.5
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand()
        err.set_error_state = MagicMock()
        err.cmd_AFC_RESUME(gcmd)
        afc.move_z_pos.assert_called_once()

    def test_paused_z_above_threshold_skips_move_z_pos(self):
        """Independent coverage of the `last_position[2] <= move_z_pos`
        operand failing while is_homed stays True (default)."""
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.z_hop = 0.5
        afc.gcode_move.last_position = [0.0, 0.0, 10.0]  # z=10 > 0+0.5
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand()
        err.set_error_state = MagicMock()
        err.cmd_AFC_RESUME(gcmd)
        afc.move_z_pos.assert_not_called()
        assert err.logger.messages == [
            ("debug", "AFC_RESUME: not moving in z cur_pos:[0.0, 0.0, 10.0] move_z_pos:0.5"),
            ("debug", "AFC_RESUME: Before User Restore"),
            ("debug", "RESUME-Error State: False, Is Paused True, "
                      "Position_saved False, in toolchange: False"),
        ]

    def test_paused_not_homed_skips_move_z_pos_even_when_z_below_threshold(self):
        """Independent coverage of the `is_homed(for_move=True)` operand of
        `is_homed(for_move=True) and last_position[2] <= move_z_pos`: the z
        condition alone (held True, as in the "below threshold" test above)
        is not enough when the printer isn't homed."""
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        afc.function.is_homed.return_value = False
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.z_hop = 0.5
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]  # z=0 ≤ 0+0.5, same as the "below threshold" case
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand()
        err.set_error_state = MagicMock()
        err.cmd_AFC_RESUME(gcmd)
        afc.move_z_pos.assert_not_called()

    def test_paused_with_error_state_calls_restore_pos(self):
        """error_state alone, independent of temp_is_paused, is sufficient to
        trigger the restore branch."""
        err, afc = _make_afc_error()
        # is_paused(): True at the entry guard (bypasses the early return),
        # then False for the temp_is_paused capture and the final debug log,
        # so error_state is the only reason the restore branch runs here.
        afc.function.is_paused.side_effect = [True, False, False]
        afc.error_state = True
        afc.position_saved = False
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand()
        err.set_error_state = MagicMock()
        err.pause = True
        err.cmd_AFC_RESUME(gcmd)
        afc.restore_pos.assert_called_once_with(False)
        assert err.pause is False

    def test_paused_with_position_saved_calls_restore_pos(self):
        """position_saved alone, independent of error_state and
        temp_is_paused, is sufficient to trigger the restore branch."""
        err, afc = _make_afc_error()
        afc.function.is_paused.side_effect = [True, False, False]
        afc.error_state = False
        afc.position_saved = True
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand()
        err.set_error_state = MagicMock()
        err.pause = True
        err.cmd_AFC_RESUME(gcmd)
        afc.restore_pos.assert_called_once_with(False)
        assert err.pause is False

    def test_paused_with_temp_is_paused_alone_calls_restore_pos(self):
        """temp_is_paused alone, independent of error_state and
        position_saved, is sufficient to trigger the restore branch."""
        err, afc = _make_afc_error()
        # Constant True: the entry guard, the temp_is_paused capture, and the
        # final debug log all see the printer as paused.
        afc.function.is_paused.return_value = True
        afc.error_state = False
        afc.position_saved = False
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand()
        err.set_error_state = MagicMock()
        err.pause = True
        err.cmd_AFC_RESUME(gcmd)
        afc.restore_pos.assert_called_once_with(False)
        assert err.pause is False

    def test_paused_without_error_or_saved_position_skips_restore_pos(self):
        """Printer was paused at entry (temp_is_paused captured as True) but by
        the time the resume block runs there's no error_state, the paused flag
        has since cleared, and no position was saved -> restore branch must
        not run."""
        err, afc = _make_afc_error()
        # 3 reads of is_paused(): early-return guard, temp_is_paused capture,
        # final debug-log format call.
        afc.function.is_paused.side_effect = [True, False, False]
        afc.error_state = False
        afc.position_saved = False
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand()
        err.set_error_state = MagicMock()
        err.pause = True
        err.cmd_AFC_RESUME(gcmd)
        afc.restore_pos.assert_not_called()
        err.set_error_state.assert_not_called()
        assert err.pause is True

    def test_paused_with_error_state_but_not_homed_skips_restore_pos(self):
        """Entering the resume block (error_state True) but not homed must
        still clear set_error_state/pause, just without restoring position."""
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        afc.function.is_homed.return_value = False
        afc.error_state = True
        afc.position_saved = False
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand()
        err.set_error_state = MagicMock()
        err.pause = True
        err.cmd_AFC_RESUME(gcmd)
        afc.restore_pos.assert_not_called()
        err.set_error_state.assert_called_once_with(False)
        assert err.pause is False


# ── cmd_AFC_PAUSE ─────────────────────────────────────────────────────────────

class TestCmdAfcPause:
    def test_not_paused_saves_position(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = False
        afc.save_pos = MagicMock()
        afc.move_z_pos = MagicMock()
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]
        err.cmd_AFC_PAUSE(MagicMock())
        afc.save_pos.assert_called_once()

    def test_not_paused_sends_pause_command(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = False
        afc.save_pos = MagicMock()
        afc.move_z_pos = MagicMock()
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]
        err.cmd_AFC_PAUSE(MagicMock())
        err.pause_resume.send_pause_command.assert_called_once()

    def test_not_paused_calls_renamed_pause_macro(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = False
        afc.save_pos = MagicMock()
        afc.move_z_pos = MagicMock()
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand()
        err.cmd_AFC_PAUSE(gcmd)
        # run_script_from_command called at least twice: PAUSE macro + SET_IDLE_TIMEOUT
        assert afc.gcode.run_script_from_command.call_count >= 1
        calls = [c[0][0] for c in afc.gcode.run_script_from_command.call_args_list]
        assert any(err.AFC_RENAME_PAUSE_NAME in c for c in calls)

    def test_already_paused_logs_not_pausing(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        err.cmd_AFC_PAUSE(MagicMock())
        assert err.logger.messages == [
            ("debug", "AFC_PAUSE: Not Pausing"),
            ("debug", "PAUSE-Error State: False, Is Paused True, "
                      "Position_saved False, in toolchange: False"),
        ]

    def test_already_paused_skips_pause_command(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        err.cmd_AFC_PAUSE(MagicMock())
        err.pause_resume.send_pause_command.assert_not_called()

    def test_not_paused_z_below_threshold_calls_move_z_pos(self):
        """Independent coverage of the `last_position[2] <= move_z_pos`
        operand of `is_homed(for_move=True) and last_position[2] <= move_z_pos`
        (is_homed defaults to True via _make_afc_error)."""
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = False
        afc.save_pos = MagicMock()
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.z_hop = 0.5
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]  # z=0 ≤ 0+0.5
        afc.move_z_pos = MagicMock()
        err.cmd_AFC_PAUSE(MagicMock())
        afc.move_z_pos.assert_called_once()

    def test_not_paused_not_homed_skips_move_z_pos_even_when_z_below_threshold(self):
        """Independent coverage of the `is_homed(for_move=True)` operand: the
        z condition alone (held True, as above) is not enough when the
        printer isn't homed."""
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = False
        afc.function.is_homed.return_value = False
        afc.save_pos = MagicMock()
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.z_hop = 0.5
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]  # z=0 ≤ 0+0.5, same as above
        afc.move_z_pos = MagicMock()
        err.cmd_AFC_PAUSE(MagicMock())
        afc.move_z_pos.assert_not_called()

    def test_not_paused_z_above_threshold_skips_move_z_pos(self):
        """Current z already above the z-hop target -> log debug, skip move_z_pos."""
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = False
        afc.save_pos = MagicMock()
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]  # saved z = 0
        afc.z_hop = 0.5  # target = 0 + 0.5 = 0.5
        afc.gcode_move.last_position = [0.0, 0.0, 1.0]  # current z = 1.0 > 0.5
        afc.move_z_pos = MagicMock()
        from tests.conftest import MockGCodeCommand
        gcmd = MockGCodeCommand()
        err.cmd_AFC_PAUSE(gcmd)
        afc.move_z_pos.assert_not_called()
        assert err.logger.messages == [
            ("debug", "AFC_PAUSE: Pausing"),
            ("debug", "AFC_PAUSE: not moving in z cur_pos:[0.0, 0.0, 1.0] move_z_pos:0.5"),
            ("debug", "PAUSE-Error State: False, Is Paused False, "
                      "Position_saved False, in toolchange: False"),
        ]


# ═════════════════════════════════════════════════════════════════════════
# Module-level import guards
# ═════════════════════════════════════════════════════════════════════════

def _exec_afc_error_with_blocked_dependency(blocked_module_name):
    """Execute a throw-away copy of extras/AFC_error.py's module-level code
    with `blocked_module_name` forced to fail import, to exercise the file's
    top-level ``try: from X import Y / except: raise error(...)`` guards.

    This never touches the real, already-imported ``extras.AFC_error``
    module that the rest of this test suite depends on: the copy is loaded
    under a throwaway module name and discarded afterward, whether or not it
    raises. Blocking an import via ``sys.modules[name] = None`` is a standard
    Python mechanism -- it makes any ``import``/``from ... import`` of that
    name raise ImportError immediately, without touching the module itself.

    Cleanup restores the *exact same* pre-existing module object in
    sys.modules (not just removes the block) -- simply deleting the entry
    would let it get re-imported fresh the next time anything touches it,
    producing new, distinct class objects that no longer match what other
    test files already imported and bound references to.
    """
    import extras.AFC_error as real_module
    fresh_name = "extras.AFC_error_import_guard_probe"
    original_blocked_module = sys.modules.get(blocked_module_name)
    sys.modules[blocked_module_name] = None
    try:
        spec = importlib.util.spec_from_file_location(fresh_name, real_module.__file__)
        fresh = importlib.util.module_from_spec(spec)
        sys.modules[fresh_name] = fresh
        try:
            spec.loader.exec_module(fresh)
        finally:
            sys.modules.pop(fresh_name, None)
    finally:
        if original_blocked_module is not None:
            sys.modules[blocked_module_name] = original_blocked_module
        else:
            sys.modules.pop(blocked_module_name, None)


class TestModuleImportGuards:
    """Covers the three module-level `try/except: raise error(...)` guards in
    AFC_error.py, one per dependency import."""

    def test_afc_utils_import_failure_raises_configparser_error(self):
        """The very first guard imports ERROR_STR itself from AFC_utils, so
        it can't use ERROR_STR.format(...) in its own except clause."""
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_error_with_blocked_dependency("extras.AFC_utils")
        assert str(exc_info.value).startswith(
            "Error when trying to import AFC_utils.ERROR_STR"
        )

    def test_afc_import_failure_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_error_with_blocked_dependency("extras.AFC")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC, please rerun install-afc.sh"
        )

    def test_afc_lane_import_failure_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_error_with_blocked_dependency("extras.AFC_lane")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_lane, please rerun install-afc.sh"
        )
