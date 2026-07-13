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

from extras.AFC_stepper import AFCExtruderStepper, TrapqAppendWrapper


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_stepper(name="lane1", extruder_name="extruder"):
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
    stepper.extruder_obj = MagicMock()
    stepper.extruder_obj.th_extruder_name = stepper.extruder_obj.name = extruder_name
    stepper.afc_extruder_name = extruder_name
    stepper._synced_to_extruder = False
    stepper._print_current_set = False

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
        s._print_current_set = True
        s._set_current = MagicMock()
        s.set_load_current()
        s._set_current.assert_called_once_with(1.2)
        assert not s._print_current_set
    
    def test_set_load_current_print_current_not_set(self):
        s = _make_stepper()
        s.tmc_print_current = 0.8
        s.tmc_load_current = 1.2
        s._print_current_set = False
        s._set_current = MagicMock()
        s.set_load_current()
        s._set_current.assert_not_called()

    def test_set_load_current_print_current_not_set_called_twice(self):
        s = _make_stepper()
        s.tmc_print_current = 0.8
        s.tmc_load_current = 1.2
        s._print_current_set = True
        s._set_current = MagicMock()
        s.set_load_current()
        s.set_load_current()
        s._set_current.assert_called_once_with(1.2)
        assert not s._print_current_set

    def test_set_print_current_calls_set_current_with_print_value(self):
        s = _make_stepper()
        s.tmc_print_current = 0.5
        s.tmc_load_current = 1.0
        s._set_current = MagicMock()
        s.set_print_current()
        s._set_current.assert_called_once_with(0.5)
        assert s._print_current_set

    def test_set_print_current_calls_set_current_with_print_value_called_twice(self):
        s = _make_stepper()
        s.tmc_print_current = 0.5
        s.tmc_load_current = 1.0
        s._set_current = MagicMock()
        s.set_print_current()
        s.set_print_current()
        s._set_current.assert_called_once_with(0.5)
        assert s._print_current_set

    def test_set_print_current_call_already_enabled(self):
        s = _make_stepper()
        s._print_current_set = True
        s.tmc_print_current = 0.5
        s.tmc_load_current = 1.0
        s._set_current = MagicMock()
        s.set_print_current()
        s._set_current.assert_not_called()


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
        s = _make_stepper(extruder_name="extruder")
        s._set_current = MagicMock()
        s.set_print_current = MagicMock()
        s.sync_to_extruder(update_current=False)
        s.extruder_stepper.sync_to_extruder.assert_called_once_with("extruder")

    def test_sync_calls_extruder_stepper_sync_already_synced(self):
        s = _make_stepper(extruder_name="extruder")
        s._synced_to_extruder = True
        s._set_current = MagicMock()
        s.set_print_current = MagicMock()
        s.sync_to_extruder(update_current=True)
        s.extruder_stepper.sync_to_extruder.assert_not_called()
        s.set_print_current.assert_called_once()

    def test_sync_calls_extruder_stepper_sync_called_twice(self):
        s = _make_stepper(extruder_name="extruder")
        s._set_current = MagicMock()
        s.set_print_current = MagicMock()
        s.sync_to_extruder(update_current=False)
        s.sync_to_extruder(update_current=False)
        s.extruder_stepper.sync_to_extruder.assert_called_once_with("extruder")
        assert s._synced_to_extruder

    def test_sync_calls_set_print_current_when_update_current(self):
        s = _make_stepper(extruder_name="extruder")
        s.set_print_current = MagicMock()
        s.sync_to_extruder(update_current=True)
        s.set_print_current.assert_called_once()

    def test_sync_calls_set_print_current_when_update_current_called_twice(self):
        s = _make_stepper(extruder_name="extruder")
        s.set_print_current = MagicMock()
        s.sync_to_extruder(update_current=True)
        s.sync_to_extruder(update_current=True)
        s.extruder_stepper.sync_to_extruder.assert_called_once_with("extruder")

    def test_sync_calls_set_print_current_when_update_current_synced_set(self):
        s = _make_stepper(extruder_name="extruder")
        s._synced_to_extruder = True
        s.set_print_current = MagicMock()
        s.sync_to_extruder(update_current=True)
        s.extruder_stepper.sync_to_extruder.assert_not_called()
        s.set_print_current.assert_called_once()

    def test_sync_skips_set_print_current_when_update_current_false(self):
        s = _make_stepper(extruder_name="extruder")
        s.set_print_current = MagicMock()
        s.sync_to_extruder(update_current=False)
        s.set_print_current.assert_not_called()

    def test_sync_uses_override_extruder_name(self):
        s = _make_stepper(extruder_name="extruder")
        s.set_print_current = MagicMock()
        s.sync_to_extruder(update_current=False, th_extruder_name="extruder2")
        s.extruder_stepper.sync_to_extruder.assert_called_once_with("extruder2")
        assert s._synced_to_extruder

    def test_unsync_calls_sync_with_none(self):
        s = _make_stepper()
        s.set_load_current = MagicMock()
        s._synced_to_extruder = True
        s.unsync_to_extruder(update_current=False)
        s.extruder_stepper.sync_to_extruder.assert_called_once_with(None)
        assert not s._synced_to_extruder

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

    def test_unsync_synced_twice(self):
        s = _make_stepper()
        s._synced_to_extruder = True
        s.set_load_current = MagicMock()
        s.unsync_to_extruder(update_current=False)
        s.unsync_to_extruder(update_current=False)
        s.extruder_stepper.sync_to_extruder.assert_called_once_with(None)
        assert not s._synced_to_extruder

    def test_unsync_unsynced_twice(self):
        s = _make_stepper()
        s._synced_to_extruder = False
        s.set_load_current = MagicMock()
        s.unsync_to_extruder(update_current=False)
        s.unsync_to_extruder(update_current=False)
        s.extruder_stepper.sync_to_extruder.assert_not_called()
        assert not s._synced_to_extruder

# ── TrapqAppendWrapper  ───────────────────────────────────────────────────
 
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

# chelper is imported at the top level of AFC_stepper.py, so it must exist
# in sys.modules before we import the module, otherwise the import fails.
_chelper_stub = ModuleType("chelper")
_chelper_stub.get_ffi = MagicMock()
sys.modules.setdefault("chelper", _chelper_stub)

def _make_chelper_mock(num_args: int):
    sig_mock = MagicMock()
    sig_mock.args = [MagicMock()] * num_args

    ffi_main = MagicMock()
    ffi_main.typeof.return_value = sig_mock

    ffi_lib = MagicMock()

    chelper_mod = ModuleType("chelper")
    chelper_mod.get_ffi = MagicMock(return_value=(ffi_main, ffi_lib))

    return chelper_mod, ffi_main, ffi_lib


def _make_wrapper(num_args: int):
    chelper_mock, ffi_main, ffi_lib = _make_chelper_mock(num_args)
    with patch("extras.AFC_stepper.chelper", chelper_mock):
        obj = TrapqAppendWrapper()
    return obj, chelper_mock, ffi_main, ffi_lib


class TestSnapmakerTrapqAppendSig:

    def test_get_ffi_is_called(self):
        """chelper.get_ffi() must be called exactly once during construction."""
        _, chelper_mod, _, _ = _make_wrapper(num_args=10)
        chelper_mod.get_ffi.assert_called_once()

    def test_typeof_called_with_trapq_append(self):
        """ffi_main.typeof() must receive ffi_lib.trapq_append as its argument."""
        _, _, ffi_main, ffi_lib = _make_wrapper(num_args=10)
        ffi_main.typeof.assert_called_once_with(ffi_lib.trapq_append)

    def test_false_when_args_less_than_snapmaker(self):
        """14 args (one fewer than Snapmaker) must yield False."""
        obj, *_ = _make_wrapper(num_args=14)
        assert obj.snapmaker_trapq_append_sig is False

    def test_false_when_args_greater_than_snapmaker(self):
        """16 args (one more than Snapmaker) must yield False."""
        obj, *_ = _make_wrapper(num_args=16)
        assert obj.snapmaker_trapq_append_sig is False

    def test_false_when_zero_args(self):
        """Edge case: zero args must yield False."""
        obj, *_ = _make_wrapper(num_args=0)
        assert obj.snapmaker_trapq_append_sig is False

    def test_true_when_exactly_snapmaker_len(self):
        """Exactly 15 args must yield True."""
        obj, *_ = _make_wrapper(num_args=15)
        assert obj.snapmaker_trapq_append_sig is True


class TestTrapqAppend:

    def test_no_padding_on_standard_signature(self):
        """Standard signature: args tuple must be passed through unchanged."""
        obj, *_ = _make_wrapper(num_args=14)
        fn = MagicMock()
        obj.trapq_append(fn, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)
        fn.assert_called_once_with(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)

    def test_padding_added_on_snapmaker_signature(self):
        """Snapmaker signature: a trailing 0 must be appended to args."""
        obj, *_ = _make_wrapper(num_args=15)
        fn = MagicMock()
        obj.trapq_append(fn, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)
        fn.assert_called_once_with(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 0)

    def test_fn_called_exactly_once(self):
        """trapq_append_fn must be called exactly once per invocation."""
        obj, *_ = _make_wrapper(num_args=14)
        fn = MagicMock()
        obj.trapq_append(fn, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)
        assert fn.call_count == 1

class TestDoEnable:
    def test_do_enable_enable_lines_is_none(self):
        s = _make_stepper()
        stepper_name = f"AFC_stepper {s.name}"
        extruder_stepper = MagicMock()
        s.stepper_enable = MagicMock()
        s.stepper_enable.set_motors_enable = MagicMock()
        s.stepper_enable.enable_lines = {}
        s.do_enable(True)

        s.stepper_enable.set_motors_enable.assert_not_called()
        s.stepper_enable.motor_debug_enable.assert_not_called()

    def test_do_enable_motor_is_enabled_noop_klipper(self):
        s = _make_stepper()
        stepper_name = f"AFC_stepper {s.name}"
        extruder_stepper = MagicMock()
        s.stepper_enable = MagicMock()
        s.stepper_enable.set_motors_enable = MagicMock()
        s.stepper_enable.enable_lines = {stepper_name: extruder_stepper}
        extruder_stepper.is_motor_enabled = MagicMock(return_value=True)
        s.do_enable(True)

        s.stepper_enable.set_motors_enable.assert_not_called()
        s.stepper_enable.motor_debug_enable.assert_not_called()
    
    def test_do_enable_motor_is_disabled_noop_klipper(self):
        s = _make_stepper()
        stepper_name = f"AFC_stepper {s.name}"
        extruder_stepper = MagicMock()
        s.stepper_enable = MagicMock()
        s.stepper_enable.set_motors_enable = MagicMock()
        s.stepper_enable.enable_lines = {stepper_name: extruder_stepper}
        extruder_stepper.is_motor_enabled = MagicMock(return_value=False)
        s.do_enable(False)

        s.stepper_enable.set_motors_enable.assert_not_called()
        s.stepper_enable.motor_debug_enable.assert_not_called()

    def test_do_enable_motor_is_enabled_old_klipper(self):
        s = _make_stepper()
        stepper_name = f"AFC_stepper {s.name}"
        extruder_stepper = MagicMock()
        s.stepper_enable = MagicMock()
        s.stepper_enable.motor_debug_enable = MagicMock()
        s.stepper_enable.enable_lines = {stepper_name: extruder_stepper}
        extruder_stepper.is_motor_enabled = MagicMock(return_value=True)

        # Need to make sure hasattr always returns False for this test
        with patch('builtins.hasattr', return_value=False):
            s.do_enable(True)

        s.stepper_enable.set_motors_enable.assert_not_called()
        s.stepper_enable.motor_debug_enable.assert_not_called()

    def test_do_enable_motor_is_enabled_disable_klipper(self):
        s = _make_stepper()
        stepper_name = f"AFC_stepper {s.name}"
        extruder_stepper = MagicMock()
        s.stepper_enable = MagicMock()
        s.stepper_enable.set_motors_enable = MagicMock()
        s.stepper_enable.enable_lines = {stepper_name: extruder_stepper}
        extruder_stepper.is_motor_enabled = MagicMock(return_value=True)
        s.do_enable(False)

        s.stepper_enable.set_motors_enable.assert_called_once_with([stepper_name], False)
        s.stepper_enable.motor_debug_enable.assert_not_called()

    def test_do_enable_motor_is_enabled_disable_old_klipper(self):
        s = _make_stepper()
        stepper_name = f"AFC_stepper {s.name}"
        extruder_stepper = MagicMock()
        s.stepper_enable = MagicMock(autospec=True)
        s.stepper_enable.motor_debug_enable = MagicMock()
        s.stepper_enable.enable_lines = {stepper_name: extruder_stepper}
        extruder_stepper.is_motor_enabled = MagicMock(return_value=True)

        # Need to make sure hasattr always returns False for this test
        with patch('builtins.hasattr', return_value=False):
            s.do_enable(False)

        s.stepper_enable.set_motors_enable.assert_not_called()
        s.stepper_enable.motor_debug_enable.assert_called_once_with(stepper_name, False)

    def test_do_enable_motor_is_disabled_enable_klipper(self):
        s = _make_stepper()
        stepper_name = f"AFC_stepper {s.name}"
        extruder_stepper = MagicMock()
        s.stepper_enable = MagicMock()
        s.stepper_enable.set_motors_enable = MagicMock()
        s.stepper_enable.enable_lines = {stepper_name: extruder_stepper}
        extruder_stepper.is_motor_enabled = MagicMock(return_value=False)
        s.do_enable(True)

        s.stepper_enable.set_motors_enable.assert_called_once_with([stepper_name], True)
        s.stepper_enable.motor_debug_enable.assert_not_called()

    def test_do_enable_motor_is_disabled_enable_old_klipper(self):
        s = _make_stepper()
        stepper_name = f"AFC_stepper {s.name}"
        extruder_stepper = MagicMock()
        s.stepper_enable = MagicMock(autospec=True)
        s.stepper_enable.motor_debug_enable = MagicMock()
        s.stepper_enable.enable_lines = {stepper_name: extruder_stepper}
        extruder_stepper.is_motor_enabled = MagicMock(return_value=False)

        # Need to make sure hasattr always returns False for this test
        with patch('builtins.hasattr', return_value=False):
            s.do_enable(True)

        s.stepper_enable.set_motors_enable.assert_not_called()
        s.stepper_enable.motor_debug_enable.assert_called_once_with(stepper_name, True)

# ── FPS_PSF endstop wiring ──────────────────────────────────────────────────
#
# Covers the FPS_PSF-specific additions in AFC_stepper.py:
#   - _handle_ready(): registers tool_start/buffer_advance/buffer_trailing
#     against the buffer's software FPS endstops when buffer_obj.type == "FPS_PSF"
#   - _init_endstops(): the is_fps_psf_buffer branch that lets tool_start
#     resolution succeed (skip, no error) when a FPS_PSF buffer has no
#     advance_pin, since the FPS endstop is wired up later in _handle_ready
#   - _add_endstop(): the mcu_endstop parameter path that registers a
#     pre-built MCU endstop (e.g. FPSEndstopWrapper) instead of building one
#     from a pin

from extras.AFC_lane import AFCLane


class TestHandleReadyFpsEndstops:
    def test_registers_fps_endstops_when_buffer_is_fps_psf(self):
        s = _make_stepper()
        s._add_endstop = MagicMock()
        s.buffer_obj = MagicMock()
        s.buffer_obj.type = "FPS_PSF"
        s.buffer_obj.fps_endstop = "SENTINEL_ADVANCE_ENDSTOP"
        s.buffer_obj.fps_trailing_endstop = "SENTINEL_TRAILING_ENDSTOP"

        with patch.object(AFCLane, "_handle_ready", return_value=None) as super_ready:
            s._handle_ready()
        super_ready.assert_called_once()

        registered = {call.args[0]: (call.args[2], call.kwargs.get("mcu_endstop"))
                     for call in s._add_endstop.call_args_list}
        assert registered["tool_start"] == ("lane1_tool_start", "SENTINEL_ADVANCE_ENDSTOP")
        assert registered["buffer_advance"] == ("lane1_buffer_adv", "SENTINEL_ADVANCE_ENDSTOP")
        assert registered["buffer_trailing"] == ("lane1_buffer_trailing", "SENTINEL_TRAILING_ENDSTOP")
        assert s._add_endstop.call_count == 3
        # pin (args[1]) should always be None -- the FPS endstop is pre-built,
        # not created from a pin
        assert all(call.args[1] is None for call in s._add_endstop.call_args_list)

    def test_skips_fps_endstops_when_buffer_is_switched(self):
        s = _make_stepper()
        s._add_endstop = MagicMock()
        s.buffer_obj = MagicMock()
        s.buffer_obj.type = "switched"

        with patch.object(AFCLane, "_handle_ready", return_value=None):
            s._handle_ready()

        s._add_endstop.assert_not_called()

    def test_skips_fps_endstops_when_no_buffer_obj(self):
        s = _make_stepper()
        s._add_endstop = MagicMock()
        s.buffer_obj = None

        with patch.object(AFCLane, "_handle_ready", return_value=None):
            s._handle_ready()  # should not raise

        s._add_endstop.assert_not_called()

    def test_always_calls_super_handle_ready(self):
        s = _make_stepper()
        s._add_endstop = MagicMock()
        s.buffer_obj = None

        with patch.object(AFCLane, "_handle_ready", return_value=None) as super_ready:
            s._handle_ready()

        super_ready.assert_called_once()


class TestInitEndstopsFpsPfsBuffer:
    """Exercises the is_fps_psf_buffer branch inside _init_endstops(), where
    tool_start is configured as 'buffer' but the FPS_PSF buffer section has
    no advance_pin (it uses adc_pin instead)."""

    def _make_endstop_stepper(self, section_values, name="lane1"):
        """
        Build a stepper bypassing __init__, wired up just enough to drive
        _init_endstops(). `section_values` maps
        (section_prefix, name, key) -> value, mirroring _get_section_value.
        """
        s = AFCExtruderStepper.__new__(AFCExtruderStepper)
        from tests.conftest import MockAFC, MockPrinter, MockLogger, MockConfig

        afc = MockAFC()
        afc.logger = MockLogger()
        printer = MockPrinter(afc=afc)

        s.printer = printer
        s.afc = afc
        s.logger = afc.logger
        s.name = name
        s._config = MockConfig(printer=printer)

        s.load = None
        s._extra_homing_pins = []
        s.hub_endstop = None
        s.hub = None
        s.afc_extruder_name = "extruder"
        s.buffer_name = "FPS_buffer1"
        s.unit = None

        s._add_endstop = MagicMock()

        def _get_section_value(section_prefix, name, key, default=None):
            return section_values.get((section_prefix, name, key), default)
        s._get_section_value = MagicMock(side_effect=_get_section_value)

        return s

    def test_fps_psf_buffer_missing_advance_pin_skips_without_error(self):
        s = self._make_endstop_stepper({
            ("AFC_extruder", "extruder", "pin_tool_start"): "buffer",
            ("AFC_extruder", "extruder", "buffer"): "FPS_buffer1",
            ("AFC_buffer", "FPS_buffer1", "advance_pin"): None,
            ("AFC_buffer", "FPS_buffer1", "trailing_pin"): None,
            ("AFC_buffer", "FPS_buffer1", "type"): "FPS_PSF",
        })

        s._init_endstops()  # should not raise

        tool_start_calls = [c for c in s._add_endstop.call_args_list
                            if c.args[0] == "tool_start"]
        assert tool_start_calls == []
        # _init_endstops always wires these up regardless of buffer type
        assert s._ppins is s.printer.lookup_object('pins')
        assert s._qes is not None

    def test_switched_buffer_missing_advance_pin_still_raises(self):
        """Regression check: a non-FPS_PSF buffer with tool_start=buffer and
        no advance_pin should still raise, same as before this branch."""
        s = self._make_endstop_stepper({
            ("AFC_extruder", "extruder", "pin_tool_start"): "buffer",
            ("AFC_extruder", "extruder", "buffer"): "missing_buffer",
            ("AFC_buffer", "missing_buffer", "advance_pin"): None,
            ("AFC_buffer", "missing_buffer", "trailing_pin"): None,
            ("AFC_buffer", "missing_buffer", "type"): None,
        })
        s.buffer_name = "missing_buffer"

        with pytest.raises(Exception):
            s._init_endstops()

    def test_non_fps_psf_type_value_missing_advance_pin_still_raises(self):
        """Proves is_fps_psf_buffer requires the *equality* check against
        "FPS_PSF", not just a not-None check -- a buffer with a real but
        different `type` value (e.g. "switched") and no advance_pin must
        still raise, not silently skip like a true FPS_PSF buffer would."""
        s = self._make_endstop_stepper({
            ("AFC_extruder", "extruder", "pin_tool_start"): "buffer",
            ("AFC_extruder", "extruder", "buffer"): "Turtle_1",
            ("AFC_buffer", "Turtle_1", "advance_pin"): None,
            ("AFC_buffer", "Turtle_1", "trailing_pin"): None,
            ("AFC_buffer", "Turtle_1", "type"): "switched",
        })
        s.buffer_name = "Turtle_1"

        with pytest.raises(Exception):
            s._init_endstops()

    def test_switched_buffer_with_advance_pin_registers_tool_start(self):
        """Regression check: normal switched-buffer path is unaffected by the
        is_fps_psf_buffer addition."""
        s = self._make_endstop_stepper({
            ("AFC_extruder", "extruder", "pin_tool_start"): "buffer",
            ("AFC_extruder", "extruder", "buffer"): "Turtle_1",
            ("AFC_buffer", "Turtle_1", "advance_pin"): "PC6",
            ("AFC_buffer", "Turtle_1", "trailing_pin"): "PC7",
            ("AFC_buffer", "Turtle_1", "type"): "switched",
        })
        s.buffer_name = "Turtle_1"

        s._init_endstops()

        tool_start_calls = [c for c in s._add_endstop.call_args_list
                            if c.args[0] == "tool_start"]
        assert len(tool_start_calls) == 1
        assert tool_start_calls[0].args[1] == "PC6"


class TestAddEndstopMcuEndstopParam:
    """Exercises _add_endstop's mcu_endstop parameter, which lets a
    pre-built MCU endstop (e.g. an FPS software endstop) be registered
    directly, bypassing pin-based endstop creation.

    Also asserts every self.logger call the method makes (info/debug on the
    success paths, plus the three exception-handling log calls), since those
    are all real, user-visible log lines and not just incidental behavior.
    """

    def _make_add_endstop_stepper(self, name="lane1"):
        s = AFCExtruderStepper.__new__(AFCExtruderStepper)
        from tests.conftest import MockAFC, MockPrinter, MockLogger

        afc = MockAFC()
        afc.logger = MockLogger()
        printer = MockPrinter(afc=afc)

        s.printer = printer
        s.afc = afc
        s.logger = afc.logger
        s.name = name
        s.lane = name
        s._endstops = {}
        s._ppins = MagicMock()
        s._qes = MagicMock()
        s.extruder_stepper = MagicMock()

        return s

    def test_mcu_endstop_provided_skips_pin_based_setup(self):
        s = self._make_add_endstop_stepper()
        sentinel_endstop = MagicMock(name="fps_endstop")

        s._add_endstop("buffer_advance", None, "buffer_adv", mcu_endstop=sentinel_endstop)

        s._ppins.setup_pin.assert_not_called()
        s._ppins.allow_multi_use_pin.assert_not_called()
        s._qes.register_endstop.assert_called_once_with(sentinel_endstop, "buffer_adv")
        sentinel_endstop.add_stepper.assert_called_once_with(
            s.extruder_stepper.stepper)
        assert s._endstops["buffer_advance"] == (sentinel_endstop, "buffer_adv")
        assert s.logger.messages == [
            ("debug", "lane1 adding endstop buffer_advance:buffer_adv:None"),
        ]

    def test_mcu_endstop_uses_fullname_when_provided(self):
        s = self._make_add_endstop_stepper()
        sentinel_endstop = MagicMock(name="fps_endstop")

        s._add_endstop("tool_start", None, "tool_start", fullname="lane1_tool_start",
                       mcu_endstop=sentinel_endstop)

        s._qes.register_endstop.assert_called_once_with(sentinel_endstop, "lane1_tool_start")
        assert s._endstops["tool_start"] == (sentinel_endstop, "lane1_tool_start")
        assert s.logger.messages == [
            ("debug", "lane1 adding endstop tool_start:lane1_tool_start:None"),
        ]

    def test_no_pin_and_no_mcu_endstop_logs_and_returns(self):
        """Regression check: existing behavior when neither pin nor
        mcu_endstop is supplied should be unchanged."""
        s = self._make_add_endstop_stepper()

        s._add_endstop("buffer_advance", None, "buffer_adv")

        s._qes.register_endstop.assert_not_called()
        assert "buffer_advance" not in s._endstops
        assert s.logger.messages == [
            ("info", "Pin for buffer_advance is none for lane1"),
        ]

    def test_pin_provided_uses_normal_pin_based_setup(self):
        """Regression check: normal pin-based endstop creation still works
        after the mcu_endstop parameter was added."""
        s = self._make_add_endstop_stepper()
        pin_based_endstop = MagicMock(name="pin_based_endstop")
        s._ppins.setup_pin.return_value = pin_based_endstop

        s._add_endstop("hub", "PC5", "hub")

        s._ppins.allow_multi_use_pin.assert_called_once_with("PC5")
        s._ppins.setup_pin.assert_called_once_with("endstop", "PC5")
        s._qes.register_endstop.assert_called_once_with(pin_based_endstop, "hub")
        pin_based_endstop.add_stepper.assert_called_once_with(
            s.extruder_stepper.stepper)
        assert s._endstops["hub"] == (pin_based_endstop, "hub")
        assert s.logger.messages == [
            ("debug", "lane1 adding endstop hub:hub:PC5"),
        ]

    def test_pin_and_mcu_endstop_both_provided_pin_wins(self):
        """Proves `pin is None` is required for the mcu_endstop bypass, not
        just `mcu_endstop is not None` alone: when both are supplied, the
        pin-based path is used and the passed-in mcu_endstop is discarded in
        favor of the one built from the pin."""
        s = self._make_add_endstop_stepper()
        pin_based_endstop = MagicMock(name="pin_based_endstop")
        s._ppins.setup_pin.return_value = pin_based_endstop
        ignored_endstop = MagicMock(name="should_be_ignored")

        s._add_endstop("hub", "PC5", "hub", mcu_endstop=ignored_endstop)

        s._ppins.allow_multi_use_pin.assert_called_once_with("PC5")
        s._ppins.setup_pin.assert_called_once_with("endstop", "PC5")
        s._qes.register_endstop.assert_called_once_with(pin_based_endstop, "hub")
        pin_based_endstop.add_stepper.assert_called_once_with(
            s.extruder_stepper.stepper)
        ignored_endstop.add_stepper.assert_not_called()
        assert s._endstops["hub"] == (pin_based_endstop, "hub")
        assert s.logger.messages == [
            ("debug", "lane1 adding endstop hub:hub:PC5"),
        ]

    def test_pin_parsing_exception_logs_and_returns(self):
        """Covers the pin-parsing except branch: allow_multi_use_pin/parse_pin/
        setup_pin raising should log twice and return without registering."""
        s = self._make_add_endstop_stepper()
        s._ppins.allow_multi_use_pin.side_effect = ValueError("bad pin token")

        s._add_endstop("hub", "PC5", "hub")

        s._qes.register_endstop.assert_not_called()
        assert "hub" not in s._endstops
        assert s.logger.messages == [
            ("info", "Error parsing pin for hub is none for lane1"),
            ("info", "bad pin token"),
        ]

    def test_register_endstop_exception_is_logged_but_does_not_raise(self):
        """register_endstop raising should be caught, logged, and execution
        should continue on to still record the endstop and debug log."""
        s = self._make_add_endstop_stepper()
        sentinel_endstop = MagicMock(name="fps_endstop")
        s._qes.register_endstop.side_effect = Exception("already registered")

        s._add_endstop("buffer_advance", None, "buffer_adv", mcu_endstop=sentinel_endstop)

        sentinel_endstop.add_stepper.assert_called_once_with(
            s.extruder_stepper.stepper)
        assert s._endstops["buffer_advance"] == (sentinel_endstop, "buffer_adv")
        assert s.logger.messages == [
            ("info", "Error when registering buffer_adv as endstop for lane1"),
            ("debug", "lane1 adding endstop buffer_advance:buffer_adv:None"),
        ]

    def test_add_stepper_exception_is_logged_but_does_not_raise(self):
        """add_stepper raising should be caught, logged, and execution should
        still continue on to record the endstop and debug log."""
        s = self._make_add_endstop_stepper()
        sentinel_endstop = MagicMock(name="fps_endstop")
        sentinel_endstop.add_stepper.side_effect = Exception("stepper mismatch")

        s._add_endstop("buffer_advance", None, "buffer_adv", mcu_endstop=sentinel_endstop)

        s._qes.register_endstop.assert_called_once_with(sentinel_endstop, "buffer_adv")
        assert s._endstops["buffer_advance"] == (sentinel_endstop, "buffer_adv")
        assert s.logger.messages == [
            ("info", "Error when registering stepper lane1"),
            ("debug", "lane1 adding endstop buffer_advance:buffer_adv:None"),
        ]
