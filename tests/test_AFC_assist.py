"""
Unit tests for extras/AFC_assist.py

Covers:
  - AFCassistMotor: attribute initialization
  - _set_pin: pin state changes, same-value skip, is_resend, PWM vs digital
  - get_status: returns correct dict
  - EspoolerDir: direction constants
  - AFCEspoolerStats: direction/start_time/end_time setters, reset, update
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from extras.AFC_assist import AFCassistMotor, EspoolerDir, AFCEspoolerStats

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
