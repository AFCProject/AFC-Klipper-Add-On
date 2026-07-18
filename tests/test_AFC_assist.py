"""
Unit tests for extras/AFC_assist.py

Covers:
  - AFCassistMotor: attribute initialization
  - _set_pin: pin state changes, same-value skip, is_resend, PWM vs digital
  - get_status: returns correct dict
  - EspoolerDir: direction constants
  - AFCEspoolerStats: direction/start_time/end_time setters, reset, update
  - Espooler.do_assist_move: early return, print_time ternary, weight-vs-
    threshold branch, debug logging (built through Espooler's real __init__,
    unlike the __new__-bypassed helpers above -- its constructor is tractable
    to build for real with MockConfig/MockPrinter)
"""

from __future__ import annotations

import math

from unittest.mock import MagicMock
import pytest

from extras.AFC_assist import AFCassistMotor, EspoolerDir, AFCEspoolerStats, Espooler

PIN_MIN_TIME = 0.100  # Must match source constant


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_assist_motor(is_pwm=False):
    """Build an AFCassistMotor bypassing the complex __init__."""
    motor = AFCassistMotor.__new__(AFCassistMotor)
    motor.is_pwm = is_pwm
    motor.scale = 1.0
    motor.mcu_pin = MagicMock()
    motor.last_value = 0.0
    motor.last_print_time = 0.0
    motor.resend_interval = 0.0
    motor.resend_timer = None
    motor.reactor = MagicMock()
    motor.shutdown_value = 0.0
    return motor


def _make_espooler_stats():
    """Build an AFCEspoolerStats bypassing __init__."""
    from tests.conftest import MockLogger
    stats = AFCEspoolerStats.__new__(AFCEspoolerStats)
    stats._n20_runtime_fwd = MagicMock()
    stats._n20_runtime_rwd = MagicMock()
    stats._n20_runtime_fwd.value = 0
    stats._n20_runtime_rwd.value = 0
    stats._fwd_updated = False
    stats._rwd_updated = False
    stats._direction = None
    stats._direction_start = None
    stats._direction_end = None
    stats._delta = None
    stats.logger = MockLogger()
    return stats


ESPOOLER_VALUES_CONFIG = {
    "max_motor_rpm": 6000.0,
    "espool_rot_dist": 5.0,
    "spool_ratio": 2.0,
    "full_weight": 1000.0,
    "spool_outer_diameter": 200.0,
    "spool_inner_diameter": 100.0,
    "delta_movement": 10.0,
    "spoolrate": 1.0,
    "kick_start_time": 0.5,
}


def _expected_cruise_time(weight, cfg=ESPOOLER_VALUES_CONFIG):
    """Independently computes the same formula as Espooler_values.calculate_cruise_time,
    so tests aren't just trusting the code under test to grade itself."""
    rps = cfg["max_motor_rpm"] / 60
    outer_circ = cfg["spool_outer_diameter"] * math.pi
    delta_circ = (cfg["spool_outer_diameter"] - cfg["spool_inner_diameter"]) * math.pi
    spool_rot_s = (cfg["espool_rot_dist"] * (rps / cfg["spool_ratio"])) / outer_circ
    w_r = ((weight / cfg["full_weight"]) + 1) * delta_circ
    return cfg["delta_movement"] / w_r / spool_rot_s

# Built through Espooler's real __init__ (MockConfig/MockPrinter supply the
# dependencies), rather than __new__-bypassed like the helpers above --
# Espooler's constructor doesn't need the heavy config-section/pin/hub
# scaffolding that makes some other extras classes impractical to build for
# real in a unit test.
def _make_real_espooler(has_rwd=True, has_fwd=True, debug=False,
                        enable_assist_weight=500.0, weight=100.0):
    """Builds a real Espooler via its actual __init__, using MockConfig/
    MockPrinter to supply Klipper's config/printer/reactor dependencies."""
    from tests.conftest import MockConfig, MockPrinter, MockAFC

    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    values = dict(ESPOOLER_VALUES_CONFIG)
    values["enable_assist_weight"] = enable_assist_weight
    values["debug"] = debug
    if has_rwd:
        values["afc_motor_rwd"] = "some_mcu:RWD"
    if has_fwd:
        values["afc_motor_fwd"] = "some_mcu:FWD"
    config = MockConfig(printer=printer, values=values)

    espooler = Espooler("lane1", config)
    espooler.lane_obj = MagicMock(weight=weight)
    return espooler


# ── Initialization ────────────────────────────────────────────────────────────

class TestAFCassistMotorInit:
    def test_last_value_initially_zero(self):
        motor = _make_assist_motor()
        assert motor.last_value == 0.0

    def test_last_print_time_initially_zero(self):
        motor = _make_assist_motor()
        assert motor.last_print_time == 0.0

    def test_resend_interval_initially_zero(self):
        motor = _make_assist_motor()
        assert motor.resend_interval == 0.0

    def test_is_pwm_stored(self):
        motor = _make_assist_motor(is_pwm=True)
        assert motor.is_pwm is True

    def test_scale_default_one(self):
        motor = _make_assist_motor()
        assert motor.scale == 1.0


# ── _set_pin ──────────────────────────────────────────────────────────────────

class TestSetPin:
    def test_same_value_no_resend_skips_pin(self):
        """When value matches last_value and is_resend=False, pin is not touched."""
        motor = _make_assist_motor(is_pwm=False)
        motor.last_value = 0.5
        motor._set_pin(0.0, 0.5, is_resend=False)
        motor.mcu_pin.set_digital.assert_not_called()
        motor.mcu_pin.set_pwm.assert_not_called()

    def test_same_value_with_resend_calls_digital_pin(self):
        """When is_resend=True, pin is called even if value is the same."""
        motor = _make_assist_motor(is_pwm=False)
        motor.last_value = 1.0
        motor._set_pin(1.0, 1.0, is_resend=True)
        motor.mcu_pin.set_digital.assert_called()

    def test_digital_pin_called_when_not_pwm(self):
        motor = _make_assist_motor(is_pwm=False)
        motor._set_pin(0.0, 1.0)
        motor.mcu_pin.set_digital.assert_called()
        motor.mcu_pin.set_pwm.assert_not_called()

    def test_pwm_pin_called_when_pwm(self):
        motor = _make_assist_motor(is_pwm=True)
        motor._set_pin(0.0, 0.75)
        motor.mcu_pin.set_pwm.assert_called()
        motor.mcu_pin.set_digital.assert_not_called()

    def test_last_value_updated_after_set(self):
        motor = _make_assist_motor()
        motor._set_pin(0.0, 1.0)
        assert motor.last_value == 1.0

    def test_last_print_time_updated_after_set(self):
        motor = _make_assist_motor()
        motor._set_pin(1.5, 1.0)
        # print_time = max(1.5, 0.0 + 0.1) = 1.5
        assert motor.last_print_time == 1.5

    def test_print_time_minimum_enforced(self):
        """print_time is clamped to max(requested, last + PIN_MIN_TIME)."""
        motor = _make_assist_motor(is_pwm=False)
        motor.last_print_time = 5.0
        motor._set_pin(0.0, 1.0)  # 0.0 < 5.0 + 0.1 = 5.1 → clamped
        call_args = motor.mcu_pin.set_digital.call_args
        actual_time = call_args[0][0]
        assert actual_time >= 5.0 + PIN_MIN_TIME


# ── get_status ────────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_returns_dict_with_value_key(self):
        motor = _make_assist_motor()
        motor.last_value = 0.42
        status = motor.get_status(0.0)
        assert "value" in status

    def test_value_reflects_last_value(self):
        motor = _make_assist_motor()
        motor.last_value = 0.75
        status = motor.get_status(0.0)
        assert status["value"] == 0.75


# ── EspoolerDir ───────────────────────────────────────────────────────────────

class TestEspoolerDir:
    def test_fwd_constant(self):
        assert EspoolerDir.FWD == "Forwards"

    def test_rwd_constant(self):
        assert EspoolerDir.RWD == "Reverse"


# ── AFCEspoolerStats: direction setter ────────────────────────────────────────

class TestAFCEspoolerStatsDirection:
    def test_direction_set_when_none(self):
        stats = _make_espooler_stats()
        stats.direction = EspoolerDir.FWD
        assert stats._direction == EspoolerDir.FWD

    def test_direction_not_overwritten_when_already_set(self):
        stats = _make_espooler_stats()
        stats._direction = EspoolerDir.FWD
        stats.direction = EspoolerDir.RWD
        assert stats._direction == EspoolerDir.FWD


# ── AFCEspoolerStats: start_time setter ───────────────────────────────────────

class TestAFCEspoolerStatsStartTime:
    def test_start_time_set_when_none(self):
        stats = _make_espooler_stats()
        stats.start_time = 100.0
        assert stats._direction_start == 100.0

    def test_start_time_not_overwritten_when_set(self):
        stats = _make_espooler_stats()
        stats._direction_start = 50.0
        stats.start_time = 200.0
        assert stats._direction_start == 50.0


# ── AFCEspoolerStats: end_time setter ─────────────────────────────────────────

class TestAFCEspoolerStatsEndTime:
    def test_end_time_when_no_direction_does_nothing(self):
        """If _direction is None, end_time setter returns early (no delta calc)."""
        stats = _make_espooler_stats()
        stats.end_time = 200.0
        assert stats._direction is None  # nothing changed

    def test_fwd_runtime_incremented(self):
        stats = _make_espooler_stats()
        stats._direction = EspoolerDir.FWD
        stats._direction_start = 100.0
        stats._n20_runtime_fwd.value = 0
        stats.end_time = 105.0
        assert stats._fwd_updated is True

    def test_rwd_runtime_incremented(self):
        stats = _make_espooler_stats()
        stats._direction = EspoolerDir.RWD
        stats._direction_start = 100.0
        stats._n20_runtime_rwd.value = 0
        stats.end_time = 108.0
        assert stats._rwd_updated is True

    def test_state_reset_after_end_time_set(self):
        stats = _make_espooler_stats()
        stats._direction = EspoolerDir.FWD
        stats._direction_start = 100.0
        stats.end_time = 105.0
        assert stats._direction is None
        assert stats._direction_start is None
        assert stats._direction_end is None

    def test_no_delta_when_end_not_after_start(self):
        """When end_time <= start_time, no delta is applied."""
        stats = _make_espooler_stats()
        stats._direction = EspoolerDir.FWD
        stats._direction_start = 100.0
        stats._n20_runtime_fwd.value = 0
        stats.end_time = 99.0  # end < start → no update
        assert stats._fwd_updated is False


# ── AFCEspoolerStats: reset_runtimes ─────────────────────────────────────────

class TestResetRuntimes:
    def test_fwd_reset_called(self):
        stats = _make_espooler_stats()
        stats.reset_runtimes()
        stats._n20_runtime_fwd.reset_count.assert_called_once()

    def test_rwd_reset_called(self):
        stats = _make_espooler_stats()
        stats.reset_runtimes()
        stats._n20_runtime_rwd.reset_count.assert_called_once()


# ── AFCEspoolerStats: update_database ────────────────────────────────────────

class TestUpdateDatabase:
    def test_fwd_database_updated_when_flag_set(self):
        stats = _make_espooler_stats()
        stats._fwd_updated = True
        stats.update_database()
        stats._n20_runtime_fwd.update_database.assert_called_once()
        assert stats._fwd_updated is False

    def test_rwd_database_updated_when_flag_set(self):
        stats = _make_espooler_stats()
        stats._rwd_updated = True
        stats.update_database()
        stats._n20_runtime_rwd.update_database.assert_called_once()
        assert stats._rwd_updated is False

    def test_no_update_when_neither_flag_set(self):
        stats = _make_espooler_stats()
        stats.update_database()
        stats._n20_runtime_fwd.update_database.assert_not_called()
        stats._n20_runtime_rwd.update_database.assert_not_called()


# ── do_assist_move ──────────────────────────────────────────────────────────
class TestDoAssistMove:

    def test_returns_early_when_no_fwd_motor(self):
        espooler = _make_real_espooler(has_fwd=False, debug=True)
        espooler._kick_start = MagicMock()
        espooler.move_forwards = MagicMock()
        espooler.set_enable_pin = MagicMock()

        espooler.do_assist_move(1000.0)

        espooler._kick_start.assert_not_called()
        espooler.move_forwards.assert_not_called()
        espooler.set_enable_pin.assert_not_called()
        assert espooler.logger.messages == []

    def test_does_not_return_early_when_rwd_missing_but_fwd_defined(self):
        """Regression test: do_assist_move only ever operates on afc_motor_fwd
        internally (kick-start/move_forwards/final _set_pin), never on
        afc_motor_rwd -- so a lane with rwd undefined but fwd defined must
        still run the assist move, not be skipped."""
        espooler = _make_real_espooler(has_rwd=False, has_fwd=True,
                                       weight=100.0, enable_assist_weight=500.0)
        espooler._kick_start = MagicMock(return_value=1050.0)
        espooler.move_forwards = MagicMock()
        espooler.set_enable_pin = MagicMock()

        espooler.do_assist_move(1000.0)

        espooler._kick_start.assert_called_once_with(1000.0)
        espooler.move_forwards.assert_called_once_with(1050.0, 1)
        espooler.set_enable_pin.assert_called_once()

    def test_uses_provided_print_time_without_calling_get_print_time(self):
        # weight above threshold keeps this isolated to just the ternary
        espooler = _make_real_espooler(weight=900.0, enable_assist_weight=500.0)
        espooler._get_print_time = MagicMock()

        espooler.do_assist_move(1000.0)

        espooler._get_print_time.assert_not_called()

    def test_computes_print_time_when_none_provided(self):
        espooler = _make_real_espooler(weight=900.0, enable_assist_weight=500.0)
        espooler._get_print_time = MagicMock(return_value=2000.0)
        espooler.espooler_values.cruise_time = 0.0  # simulate handle_connect having run

        espooler.do_assist_move(None)

        espooler._get_print_time.assert_called_once_with()

    def test_weight_below_threshold_triggers_assist_move(self):
        espooler = _make_real_espooler(weight=100.0, enable_assist_weight=500.0)
        espooler._kick_start = MagicMock(return_value=1050.0)
        espooler.move_forwards = MagicMock()
        espooler.set_enable_pin = MagicMock()
        # move_forwards is mocked away for isolation, but in the real flow it
        # would have set afc_motor_fwd to 1 before the following _set_pin(0)
        # call -- without that, _set_pin's own same-value early-return would
        # make the transition to 0 a no-op, since last_value already starts
        # at 0. Simulate that prior state explicitly.
        espooler.afc_motor_fwd.last_value = 1.0

        espooler.do_assist_move(1000.0)

        espooler._kick_start.assert_called_once_with(1000.0)
        espooler.move_forwards.assert_called_once_with(1050.0, 1)

        expected_cruise_time = _expected_cruise_time(100.0)
        assert espooler.espooler_values.cruise_time == pytest.approx(expected_cruise_time)

        expected_final_print_time = 1050.0 + expected_cruise_time
        # afc_motor_fwd._set_pin runs for real (not mocked) -- assert its
        # actual resulting state rather than just that it was called.
        assert espooler.afc_motor_fwd.last_value == 0
        assert espooler.afc_motor_fwd.last_print_time == pytest.approx(expected_final_print_time)

        espooler.set_enable_pin.assert_called_once()
        call_args = espooler.set_enable_pin.call_args.args
        assert call_args[0] == pytest.approx(expected_final_print_time)
        assert call_args[1] == 0

    def test_weight_above_threshold_skips_assist_move(self):
        espooler = _make_real_espooler(weight=900.0, enable_assist_weight=500.0)
        espooler.espooler_values.cruise_time = 0.05  # pre-existing value from handle_connect
        espooler._kick_start = MagicMock()
        espooler.move_forwards = MagicMock()
        espooler.set_enable_pin = MagicMock()
        original_last_value = espooler.afc_motor_fwd.last_value

        espooler.do_assist_move(1000.0)

        espooler._kick_start.assert_not_called()
        espooler.move_forwards.assert_not_called()
        espooler.set_enable_pin.assert_not_called()
        # cruise_time must NOT be recomputed when the branch is skipped
        assert espooler.espooler_values.cruise_time == 0.05
        assert espooler.afc_motor_fwd.last_value == original_last_value

    def test_weight_exactly_equal_to_threshold_skips_assist_move(self):
        """Proves the comparison is strictly `<`, not `<=` -- weight equal to
        the threshold must NOT trigger the assist move."""
        espooler = _make_real_espooler(weight=500.0, enable_assist_weight=500.0)
        espooler.espooler_values.cruise_time = 0.05
        espooler._kick_start = MagicMock()
        espooler.move_forwards = MagicMock()
        espooler.set_enable_pin = MagicMock()

        espooler.do_assist_move(1000.0)

        espooler._kick_start.assert_not_called()
        espooler.move_forwards.assert_not_called()
        espooler.set_enable_pin.assert_not_called()
        assert espooler.espooler_values.cruise_time == 0.05

    def test_debug_true_logs_message_with_correct_content_on_assist_branch(self):
        espooler = _make_real_espooler(weight=100.0, enable_assist_weight=500.0, debug=True)
        espooler._kick_start = MagicMock(return_value=1050.0)
        espooler.move_forwards = MagicMock()
        espooler.set_enable_pin = MagicMock()

        espooler.do_assist_move(1000.0)

        expected_cruise_time = _expected_cruise_time(100.0)
        expected_print_time = 1050.0 + expected_cruise_time
        expected_msg = (
            f"Cruise time: {expected_cruise_time:0.03f} "
            f"1000.000 {expected_print_time:0.03f}, "
            f"Weight: 100.0, Enable weight: 500.0"
        )
        assert espooler.logger.messages == [("debug", expected_msg)]

    def test_debug_true_logs_message_with_correct_content_on_skip_branch(self):
        """The debug log is a separate top-level `if`, not nested inside the
        weight-check block -- proves it still fires (with the unmodified
        time/print_time/cruise_time) even when the assist move is skipped."""
        espooler = _make_real_espooler(weight=900.0, enable_assist_weight=500.0, debug=True)
        espooler.espooler_values.cruise_time = 0.05
        espooler._kick_start = MagicMock()
        espooler.move_forwards = MagicMock()
        espooler.set_enable_pin = MagicMock()

        espooler.do_assist_move(1000.0)

        expected_msg = "Cruise time: 0.050 1000.000 1000.000, Weight: 900.0, Enable weight: 500.0"
        assert espooler.logger.messages == [("debug", expected_msg)]

    def test_debug_false_does_not_log_on_assist_branch(self):
        espooler = _make_real_espooler(weight=100.0, enable_assist_weight=500.0, debug=False)
        espooler._kick_start = MagicMock(return_value=1050.0)
        espooler.move_forwards = MagicMock()
        espooler.set_enable_pin = MagicMock()

        espooler.do_assist_move(1000.0)

        assert espooler.logger.messages == []

    def test_debug_false_does_not_log_on_skip_branch(self):
        espooler = _make_real_espooler(weight=900.0, enable_assist_weight=500.0, debug=False)
        espooler.espooler_values.cruise_time = 0.05
        espooler._kick_start = MagicMock()
        espooler.move_forwards = MagicMock()
        espooler.set_enable_pin = MagicMock()

        espooler.do_assist_move(1000.0)

        assert espooler.logger.messages == []