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

from unittest.mock import MagicMock, patch, call
import pytest

from extras.AFC_error import afcError


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_afc_error():
    """Create an afcError instance bypassing __init__ and wiring up mocks."""
    from extras.AFC_error import afcError
    from extras.AFC_lane import AFCLaneState
    from extras.AFC import State
    from tests.conftest import MockAFC, MockPrinter, MockLogger

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

    # Build afcError without running __init__ (it needs a Klipper config)
    err = afcError.__new__(afcError)
    err.printer = printer
    err.afc = afc
    err.logger = MockLogger()
    err.pause = False
    err.pause_resume = pause_resume
    err.error_timeout = 600
    err.idle_timeout_obj = idle_timeout
    err.idle_timeout_val = idle_timeout.idle_timeout
    err.BASE_RESUME_NAME = "RESUME"
    err.AFC_RENAME_RESUME_NAME = "_AFC_RENAMED_RESUME_"
    err.BASE_PAUSE_NAME = "PAUSE"
    err.AFC_RENAME_PAUSE_NAME = "_AFC_RENAMED_PAUSE_"
    err.errorLog = {}

    return err, afc


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
        debug_msgs = [m for lvl, m in err.logger.messages if lvl == "debug"]
        assert len(debug_msgs) > 0


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
        error_msgs = [m for lvl, m in err.logger.messages if lvl == "error"]
        assert any("Bad thing happened" in m for m in error_msgs)


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


# ── AFC_error (the method) ────────────────────────────────────────────────────

class TestAFCErrorMethod:
    def test_logs_error_message(self):
        err, afc = _make_afc_error()
        err.pause_print = MagicMock()
        err.AFC_error("Catastrophic failure", pause=False)
        error_msgs = [m for lvl, m in err.logger.messages if lvl == "error"]
        assert any("Catastrophic failure" in m for m in error_msgs)

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
        from extras.AFC_unit import afcUnit
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        err.ToolHeadFix = MagicMock(return_value=False)
        lane = MagicMock()
        lane.led_index = "1"
        lane.unit_obj = afcUnit.__new__(afcUnit)
        lane.unit_obj.afc = afc
        result = err.fix("toolhead", lane)
        assert result is False
        afc.function.afc_led.assert_called_with(lane.led_fault, lane.led_index)

    def test_fix_other_problem_calls_pause_user_intervention(self):
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        lane = MagicMock()
        lane.led_index = "2"
        err.fix("jam", lane)
        err.PauseUserIntervention.assert_called_with("jam")

    def test_fix_none_problem_calls_pause_user_intervention_with_unknown_message(self):
        """Covers line 58: problem is None → PauseUserIntervention('Paused for unknown error')."""
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        lane = MagicMock()
        lane.led_index = "1"
        err.fix(None, lane)
        # Should be called with the 'unknown error' string
        calls = [str(c) for c in err.PauseUserIntervention.call_args_list]
        assert any("unknown" in c.lower() for c in calls)

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
        type(lane).raw_load_state = PropertyMock(side_effect=[True, True, False, False, True])
        result = err.ToolHeadFix(lane)
        assert result is True
        assert err.pause is False
        afc.save_vars.assert_called_once()

    def test_toolhead_empty_with_lane_filament_clears_flags_no_homing(self):
        from unittest.mock import PropertyMock
        from tests.test_AFC_lane import _make_afc_lane
        err, afc = _make_afc_error()
        err.PauseUserIntervention = MagicMock()
        afc.homing_enabled = False
        lane = _make_afc_lane()
        lane.get_toolhead_pre_sensor_state.return_value = False
        type(lane).raw_load_state = PropertyMock(side_effect=[True, True, False, False, True])
        err.ToolHeadFix(lane)
        assert lane.tool_loaded is False
        assert lane.loaded_to_hub is False
        assert lane.extruder_obj.lane_loaded == None
    
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
        type(lane).raw_load_state = PropertyMock(side_effect=[True, True, False])
        result = err.ToolHeadFix(lane)
        assert result is True
        assert err.pause is False
        afc.save_vars.assert_called_once()

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
        type(lane).raw_load_state = PropertyMock(side_effect=[True, True, False])
        err.ToolHeadFix(lane)
        assert lane.tool_loaded is False
        assert lane.loaded_to_hub is False
        assert lane.extruder_obj.lane_loaded == None
    
    def test_toolhead_empty_with_lane_filament_returns_false_timed_out_homing(self):
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
        type(lane).raw_load_state = PropertyMock(side_effect=[True, True, True, True, True, True])
        result = err.ToolHeadFix(lane)
        assert result is False
        assert err.pause is False
        err.PauseUserIntervention.assert_called_with("Failed to retract lane1 to load sensor")



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
        debug_msgs = [m for lvl, m in err.logger.messages if lvl == "debug"]
        assert any("not paused" in m.lower() or "not executing" in m.lower() for m in debug_msgs)

    def test_paused_calls_renamed_resume_macro(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        gcmd = MagicMock()
        gcmd.get_raw_command_parameters.return_value = ""
        err.set_error_state = MagicMock()
        err.cmd_AFC_RESUME(gcmd)
        afc.gcode.run_script_from_command.assert_called_once()
        call_arg = afc.gcode.run_script_from_command.call_args[0][0]
        assert err.AFC_RENAME_RESUME_NAME in call_arg

    def test_paused_z_below_threshold_calls_move_z_pos(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.z_hop = 0.5
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]  # z=0 ≤ 0+0.5
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        gcmd = MagicMock()
        gcmd.get_raw_command_parameters.return_value = ""
        err.set_error_state = MagicMock()
        err.cmd_AFC_RESUME(gcmd)
        afc.move_z_pos.assert_called_once()

    def test_paused_z_above_threshold_skips_move_z_pos(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.z_hop = 0.5
        afc.gcode_move.last_position = [0.0, 0.0, 10.0]  # z=10 > 0+0.5
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        gcmd = MagicMock()
        gcmd.get_raw_command_parameters.return_value = ""
        err.set_error_state = MagicMock()
        err.cmd_AFC_RESUME(gcmd)
        afc.move_z_pos.assert_not_called()

    def test_paused_with_error_state_calls_restore_pos(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        afc.error_state = True
        afc.position_saved = False
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]
        afc.move_z_pos = MagicMock()
        afc.restore_pos = MagicMock()
        gcmd = MagicMock()
        gcmd.get_raw_command_parameters.return_value = ""
        err.set_error_state = MagicMock()
        err.cmd_AFC_RESUME(gcmd)
        afc.restore_pos.assert_called_once_with(False)


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
        gcmd = MagicMock()
        gcmd.get_raw_command_parameters.return_value = ""
        err.cmd_AFC_PAUSE(gcmd)
        # run_script_from_command called at least twice: PAUSE macro + SET_IDLE_TIMEOUT
        assert afc.gcode.run_script_from_command.call_count >= 1
        calls = [c[0][0] for c in afc.gcode.run_script_from_command.call_args_list]
        assert any(err.AFC_RENAME_PAUSE_NAME in c for c in calls)

    def test_already_paused_logs_not_pausing(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        err.cmd_AFC_PAUSE(MagicMock())
        debug_msgs = [m for lvl, m in err.logger.messages if lvl == "debug"]
        assert any("not pausing" in m.lower() for m in debug_msgs)

    def test_already_paused_skips_pause_command(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = True
        err.cmd_AFC_PAUSE(MagicMock())
        err.pause_resume.send_pause_command.assert_not_called()

    def test_not_paused_z_below_threshold_calls_move_z_pos(self):
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = False
        afc.save_pos = MagicMock()
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        afc.z_hop = 0.5
        afc.gcode_move.last_position = [0.0, 0.0, 0.0]  # z=0 ≤ 0+0.5
        afc.move_z_pos = MagicMock()
        err.cmd_AFC_PAUSE(MagicMock())
        afc.move_z_pos.assert_called_once()

    def test_not_paused_z_above_threshold_skips_move_z_pos(self):
        """Covers line 239: current z already above target → log debug, skip move_z_pos."""
        err, afc = _make_afc_error()
        afc.function.is_paused.return_value = False
        afc.save_pos = MagicMock()
        afc.last_gcode_position = [0.0, 0.0, 0.0, 0.0]  # saved z = 0
        afc.z_hop = 0.5  # target = 0 + 0.5 = 0.5
        afc.gcode_move.last_position = [0.0, 0.0, 1.0]  # current z = 1.0 > 0.5
        afc.move_z_pos = MagicMock()
        gcmd = MagicMock()
        gcmd.get_raw_command_parameters.return_value = ""
        err.cmd_AFC_PAUSE(gcmd)
        afc.move_z_pos.assert_not_called()
