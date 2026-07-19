"""
Unit tests for extras/AFC_assist.py

All test objects are built through their real __init__ (mocking only the
Klipper-level dependencies -- config/printer/reactor/pins via MockConfig/
MockPrinter), rather than bypassing construction with __new__.

Covers:
  - AFCassistMotor: init (digital/PWM, deprecated static_value/
    maximum_mcu_duration branches), _set_pin, _resend_current_val, get_status
  - EspoolerDir: direction constants
  - AFCEspoolerStats: direction/start_time/end_time getters+setters,
    _convert_value, n20_runtime_fwd/rwd, reset_runtimes, update_database
  - Espooler_values: init, calculate_cruise_time, handle_connect
    (lane_obj/unit_obj fallback logic), every property getter/setter
  - Espooler: init (pin-combination branches, macro registration),
    handle_ready, handle_connect, handle_moonraker_connect,
    timer_stats_callback, timer_callback (incl. its 4-condition guard),
    _get_print_time, _kick_start, set_enable_pin, do_assist_move,
    move_forwards/move_reverse, assist, break_espooler, enable_timer/
    disable_timer, get_spooler_stats, and all cmd_* gcode macros
  - Module-level import guards around AFC_utils.ERROR_STR and
    AFC_stats.AFCStats_var
"""

from __future__ import annotations

import math
import sys
import importlib.util

from unittest.mock import MagicMock
import pytest

from extras.AFC_assist import (
    AFCassistMotor, EspoolerDir, AFCEspoolerStats, Espooler, RESEND_HOST_TIME,
)

PIN_MIN_TIME = 0.100  # Must match source constant


# ── Helpers ───────────────────────────────────────────────────────────────────
#
# All helpers below build test objects through their real __init__ (mocking
# only the Klipper-level dependencies -- config/printer/reactor/pins).

def _make_assist_motor(motor_type="fwd", is_pwm=False, printer=None, **config_overrides):
    """Build a real AFCassistMotor via its actual __init__."""
    from tests.conftest import MockConfig, MockPrinter

    printer = printer or MockPrinter()
    values = {"pwm": is_pwm, "afc_motor_{}".format(motor_type): "some_mcu:PIN"}
    values.update(config_overrides)
    config = MockConfig(printer=printer, values=values)
    return AFCassistMotor(config, motor_type)


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


def _make_espooler_values(printer=None, **config_overrides):
    """Build a real Espooler_values via its actual __init__."""
    from tests.conftest import MockConfig, MockPrinter
    from extras.AFC_assist import Espooler_values

    printer = printer or MockPrinter()
    values = dict(ESPOOLER_VALUES_CONFIG)
    values.update(config_overrides)
    config = MockConfig(printer=printer, values=values)
    return Espooler_values(config)


def _make_real_espooler(has_rwd=True, has_fwd=True, has_enb=False, debug=False,
                        enable_assist_weight=500.0, weight=100.0, **config_overrides):
    """Builds a real Espooler via its actual __init__, using MockConfig/
    MockPrinter to supply Klipper's config/printer/reactor dependencies."""
    from tests.conftest import MockConfig, MockPrinter, MockAFC

    afc = MockAFC()
    afc.gcode.register_mux_command = MagicMock(wraps=afc.gcode.register_mux_command)
    printer = MockPrinter(afc=afc)
    values = dict(ESPOOLER_VALUES_CONFIG)
    values["enable_assist_weight"] = enable_assist_weight
    values["debug"] = debug
    if has_rwd:
        values["afc_motor_rwd"] = "some_mcu:RWD"
    if has_fwd:
        values["afc_motor_fwd"] = "some_mcu:FWD"
    if has_enb:
        values["afc_motor_enb"] = "some_mcu:ENB"
    values.update(config_overrides)
    config = MockConfig(printer=printer, values=values)

    espooler = Espooler("lane1", config)
    espooler.lane_obj = MagicMock(weight=weight)
    return espooler


def _connect_stats(espooler):
    """Populates espooler.stats with a real AFCEspoolerStats, without going
    through the rest of handle_connect's lane_obj.unit_obj fallback logic --
    for tests that only need a working .stats collaborator."""
    espooler.stats = AFCEspoolerStats(espooler.name, espooler)
    espooler.stats.handle_moonraker_stats()
    return espooler.stats


def _make_espooler_stats(espooler=None):
    """Build a real AFCEspoolerStats via its actual __init__, then run
    handle_moonraker_stats() (as production code does during PREP) so
    _n20_runtime_fwd/_n20_runtime_rwd are real AFCStats_var objects instead
    of the placeholder ints __init__ leaves them at."""
    espooler = espooler or _make_real_espooler()
    stats = AFCEspoolerStats("lane1", espooler)
    stats.handle_moonraker_stats()
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


# ── AFCassistMotor: __init__ deprecated-value branches ─────────────────────────

class TestAFCassistMotorInitDeprecatedBranches:
    def test_max_mcu_duration_sets_resend_interval_and_deprecates(self):
        motor = _make_assist_motor(maximum_mcu_duration=1.0)
        assert motor.resend_interval == pytest.approx(1.0 - RESEND_HOST_TIME)

    def test_no_max_mcu_duration_leaves_resend_interval_zero(self):
        motor = _make_assist_motor()
        assert motor.resend_interval == 0.0

    def test_static_value_sets_last_and_shutdown_value(self):
        motor = _make_assist_motor(static_value=0.5)
        assert motor.last_value == 0.5
        assert motor.shutdown_value == 0.5

    def test_no_static_value_uses_value_and_shutdown_value_keys(self):
        motor = _make_assist_motor(value=0.3, shutdown_value=0.2)
        assert motor.last_value == 0.3
        assert motor.shutdown_value == 0.2

    def test_pwm_scale_applied_to_value(self):
        """value/shutdown_value are divided by scale -- verify the scaling,
        not just that some number came back."""
        motor = _make_assist_motor(motor_type="fwd", is_pwm=True, scale=2.0, value=1.0)
        assert motor.last_value == 0.5

    def test_enb_type_forces_digital_even_when_pwm_requested(self):
        """type == 'enb' always goes digital, regardless of the pwm config key."""
        motor = _make_assist_motor(motor_type="enb", is_pwm=True)
        assert motor.is_pwm is False
        assert motor.scale == 1.0


# ── AFCassistMotor._resend_current_val ─────────────────────────────────────────

class TestResendCurrentVal:
    def test_unregisters_timer_when_value_matches_shutdown(self):
        motor = _make_assist_motor(maximum_mcu_duration=1.0)
        motor.last_value = motor.shutdown_value = 0.0
        motor.resend_timer = "sentinel_timer"
        motor.reactor.unregister_timer = MagicMock()

        result = motor._resend_current_val(0.0)

        motor.reactor.unregister_timer.assert_called_once_with("sentinel_timer")
        assert motor.resend_timer is None
        assert result == motor.reactor.NEVER

    def test_reschedules_when_time_diff_positive(self):
        motor = _make_assist_motor(maximum_mcu_duration=1.0)
        motor.last_value = 1.0
        motor.shutdown_value = 0.0
        motor.last_print_time = 100.0
        motor.reactor.monotonic = MagicMock(return_value=50.0)
        motor.mcu_pin.get_mcu.return_value.estimated_print_time = MagicMock(return_value=40.0)
        # time_diff = (last_print_time + resend_interval) - print_time
        #           = (100.0 + resend_interval) - 40.0 > 0 -> reschedule
        original_set_pin = motor._set_pin
        motor._set_pin = MagicMock(wraps=original_set_pin)

        result = motor._resend_current_val(50.0)

        motor._set_pin.assert_not_called()
        expected_time_diff = (100.0 + motor.resend_interval) - 40.0
        assert result == pytest.approx(50.0 + expected_time_diff)

    def test_resends_pin_when_time_diff_not_positive(self):
        motor = _make_assist_motor(maximum_mcu_duration=1.0)
        motor.last_value = 1.0
        motor.shutdown_value = 0.0
        motor.last_print_time = 0.0
        motor.reactor.monotonic = MagicMock(return_value=1000.0)
        motor.mcu_pin.get_mcu.return_value.estimated_print_time = MagicMock(return_value=2000.0)
        # time_diff = (0.0 + resend_interval) - 2000.0 -- very negative
        original_set_pin = motor._set_pin
        motor._set_pin = MagicMock(wraps=original_set_pin)

        result = motor._resend_current_val(1000.0)

        motor._set_pin.assert_called_once_with(2000.0 + PIN_MIN_TIME, 1.0, True)
        assert result == pytest.approx(1000.0 + motor.resend_interval)


# ── EspoolerDir ───────────────────────────────────────────────────────────────

class TestEspoolerDir:
    def test_fwd_constant(self):
        assert EspoolerDir.FWD == "Forwards"

    def test_rwd_constant(self):
        assert EspoolerDir.RWD == "Reverse"


# ── AFCEspoolerStats._convert_value ────────────────────────────────────────────

class TestConvertValue:
    def test_value_under_threshold_stays_seconds(self):
        stats = _make_espooler_stats()
        value, unit = stats._convert_value(5000)
        assert value == 5000
        assert unit == 's'

    def test_value_over_threshold_converts_to_minutes(self):
        """20000 > 9999 -> minutes; the resulting 333.3.. is NOT > 9999,
        so it must not also convert to hours."""
        stats = _make_espooler_stats()
        value, unit = stats._convert_value(20000)
        assert value == pytest.approx(20000 / 60)
        assert unit == 'm'

    def test_value_far_over_threshold_converts_to_hours(self):
        """700000 > 9999 -> minutes (11666.67), which is ALSO > 9999 ->
        hours. Proves the second check re-evaluates the already-divided
        value, not the original."""
        stats = _make_espooler_stats()
        value, unit = stats._convert_value(700000)
        expected = (700000 / 60) / 60
        assert value == pytest.approx(expected)
        assert unit == 'h'


# ── AFCEspoolerStats: n20_runtime_fwd / n20_runtime_rwd ────────────────────────

class TestN20RuntimeProperties:
    def test_fwd_property_formats_seconds(self):
        stats = _make_espooler_stats()
        stats._n20_runtime_fwd.value = 12.3456
        assert stats.n20_runtime_fwd == "12.35s"

    def test_rwd_property_formats_seconds(self):
        stats = _make_espooler_stats()
        stats._n20_runtime_rwd.value = 8.0
        assert stats.n20_runtime_rwd == "8.00s"

    def test_fwd_property_formats_minutes(self):
        stats = _make_espooler_stats()
        stats._n20_runtime_fwd.value = 20000
        assert stats.n20_runtime_fwd == f"{20000/60:.2f}m"


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

    def test_direction_getter_returns_current_value(self):
        stats = _make_espooler_stats()
        stats._direction = EspoolerDir.RWD
        assert stats.direction == EspoolerDir.RWD


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

    def test_start_time_getter_returns_current_value(self):
        stats = _make_espooler_stats()
        stats._direction_start = 77.0
        assert stats.start_time == 77.0

# ── AFCEspoolerStats: end_time setter ─────────────────────────────────────────

class TestAFCEspoolerStatsEndTime:
    def test_end_time_when_no_direction_does_nothing(self):
        """If _direction is None, end_time setter returns early (no delta calc)."""
        stats = _make_espooler_stats()
        stats.end_time = 200.0
        assert stats._direction is None  # nothing changed

    def test_end_time_getter_returns_current_value(self):
        stats = _make_espooler_stats()
        stats._direction_end = 88.0
        assert stats.end_time == 88.0

    def test_fwd_runtime_incremented(self):
        stats = _make_espooler_stats()
        stats._direction = EspoolerDir.FWD
        stats._direction_start = 100.0
        stats._n20_runtime_fwd.value = 0
        stats.end_time = 105.0
        assert stats._fwd_updated is True
        assert stats._n20_runtime_fwd.value == 5.0

    def test_rwd_runtime_incremented(self):
        stats = _make_espooler_stats()
        stats._direction = EspoolerDir.RWD
        stats._direction_start = 100.0
        stats._n20_runtime_rwd.value = 0
        stats.end_time = 108.0
        assert stats._rwd_updated is True
        assert stats._n20_runtime_rwd.value == 8.0

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
    def test_fwd_reset_to_zero_and_pushed_to_moonraker(self):
        stats = _make_espooler_stats()
        stats._n20_runtime_fwd.value = 42
        stats.reset_runtimes()
        assert stats._n20_runtime_fwd.value == 0
        assert stats._n20_runtime_fwd.new_average is True
        key = f"{stats._n20_runtime_fwd.parent_name}.{stats._n20_runtime_fwd.name}"
        assert stats._n20_runtime_fwd.moonraker._stats[key] == 0

    def test_rwd_reset_to_zero_and_pushed_to_moonraker(self):
        stats = _make_espooler_stats()
        stats._n20_runtime_rwd.value = 17
        stats.reset_runtimes()
        assert stats._n20_runtime_rwd.value == 0
        assert stats._n20_runtime_rwd.new_average is True
        key = f"{stats._n20_runtime_rwd.parent_name}.{stats._n20_runtime_rwd.name}"
        assert stats._n20_runtime_rwd.moonraker._stats[key] == 0


# ── AFCEspoolerStats: update_database ────────────────────────────────────────

class TestUpdateDatabase:
    def test_fwd_database_updated_when_flag_set(self):
        stats = _make_espooler_stats()
        stats._fwd_updated = True
        stats._n20_runtime_fwd.value = 5.0
        stats.update_database()
        key = f"{stats._n20_runtime_fwd.parent_name}.{stats._n20_runtime_fwd.name}"
        assert stats._n20_runtime_fwd.moonraker._stats[key] == 5.0
        assert stats._fwd_updated is False

    def test_rwd_database_updated_when_flag_set(self):
        stats = _make_espooler_stats()
        stats._rwd_updated = True
        stats._n20_runtime_rwd.value = 7.0
        stats.update_database()
        key = f"{stats._n20_runtime_rwd.parent_name}.{stats._n20_runtime_rwd.name}"
        assert stats._n20_runtime_rwd.moonraker._stats[key] == 7.0
        assert stats._rwd_updated is False

    def test_no_update_when_neither_flag_set(self):
        stats = _make_espooler_stats()
        stats._n20_runtime_fwd.value = 5.0
        stats._n20_runtime_rwd.value = 7.0
        stats.update_database()
        # Neither _fwd_updated nor _rwd_updated was set, so update_database()
        # should never have pushed these values into moonraker's stats dict
        fwd_key = f"{stats._n20_runtime_fwd.parent_name}.{stats._n20_runtime_fwd.name}"
        rwd_key = f"{stats._n20_runtime_rwd.parent_name}.{stats._n20_runtime_rwd.name}"
        assert fwd_key not in stats._n20_runtime_fwd.moonraker._stats
        assert rwd_key not in stats._n20_runtime_rwd.moonraker._stats


# ── Espooler_values.__init__ ────────────────────────────────────────────────

class TestEspoolerValuesInit:
    def test_reads_config_values(self):
        values = _make_espooler_values()
        assert values.max_motor_rpm == ESPOOLER_VALUES_CONFIG["max_motor_rpm"]
        assert values.espool_rot_dist == ESPOOLER_VALUES_CONFIG["espool_rot_dist"]
        assert values.spool_ratio == ESPOOLER_VALUES_CONFIG["spool_ratio"]
        assert values.full_weight == ESPOOLER_VALUES_CONFIG["full_weight"]
        assert values.spool_outer_diameter == ESPOOLER_VALUES_CONFIG["spool_outer_diameter"]
        assert values.spool_inner_diameter == ESPOOLER_VALUES_CONFIG["spool_inner_diameter"]

    def test_unset_values_default_to_none(self):
        values = _make_espooler_values(max_motor_rpm=None, espool_rot_dist=None,
                                       spool_ratio=None, full_weight=None,
                                       spool_outer_diameter=100.0, spool_inner_diameter=1.0)
        assert values._max_motor_rpm is None
        assert values._espool_rot_dist is None
        assert values._spool_ratio is None
        assert values._full_weight is None


# ── Espooler_values.calculate_cruise_time ──────────────────────────────────

class TestCalculateCruiseTime:
    def test_matches_independently_computed_formula(self):
        values = _make_espooler_values()
        result = values.calculate_cruise_time(250.0)
        assert result == pytest.approx(_expected_cruise_time(250.0))

    def test_sets_cruise_time_attribute(self):
        values = _make_espooler_values()
        result = values.calculate_cruise_time(250.0)
        assert values.cruise_time == result


# ── Espooler_values.handle_connect ─────────────────────────────────────────

class TestEspoolerValuesHandleConnect:
    def _make_lane_obj(self, **overrides):
        lane = MagicMock()
        lane.printer = "sentinel_printer"
        lane.outer_diameter = 111.0
        lane.inner_diameter = 22.0
        lane.max_motor_rpm = 3000.0
        lane.unit_obj.kick_start_time = 0.9
        lane.unit_obj.espool_rot_dist = 9.0
        lane.unit_obj.delta_movement = 8.0
        lane.unit_obj.scaling = 2.0
        lane.unit_obj.spool_ratio = 4.0
        lane.unit_obj.full_weight = 1500.0
        for key, value in overrides.items():
            setattr(lane, key, value)
        return lane

    def test_all_unset_fields_fall_back_to_lane_or_unit_obj(self):
        # None of the espooler_values config keys are set, so every field
        # should be pulled from lane_obj/lane_obj.unit_obj.
        values = _make_espooler_values(
            max_motor_rpm=None, espool_rot_dist=None, spool_ratio=None,
            full_weight=None, spool_outer_diameter=None, spool_inner_diameter=None,
            delta_movement=None, spoolrate=None, kick_start_time=None,
        )
        lane = self._make_lane_obj()

        values.handle_connect(lane)

        assert values._kick_start_time == 0.9
        assert values._spool_outer_diameter == 111.0
        assert values._spool_inner_diameter == 22.0
        assert values._max_motor_rpm == 3000.0
        assert values._espool_rot_dist == 9.0
        assert values._delta_movement == 8.0
        assert values._scaling == 2.0
        assert values._spool_ratio == 4.0
        assert values._full_weight == 1500.0
        assert values.printer == "sentinel_printer"

    def test_already_set_fields_are_not_overridden(self):
        values = _make_espooler_values()  # every field already set via config
        lane = self._make_lane_obj()

        values.handle_connect(lane)

        assert values._max_motor_rpm == ESPOOLER_VALUES_CONFIG["max_motor_rpm"]
        assert values._espool_rot_dist == ESPOOLER_VALUES_CONFIG["espool_rot_dist"]
        assert values._spool_ratio == ESPOOLER_VALUES_CONFIG["spool_ratio"]
        assert values._full_weight == ESPOOLER_VALUES_CONFIG["full_weight"]
        assert values._spool_outer_diameter == ESPOOLER_VALUES_CONFIG["spool_outer_diameter"]
        assert values._spool_inner_diameter == ESPOOLER_VALUES_CONFIG["spool_inner_diameter"]
        assert values._delta_movement == ESPOOLER_VALUES_CONFIG["delta_movement"]
        assert values._scaling == ESPOOLER_VALUES_CONFIG["spoolrate"]
        assert values._kick_start_time == ESPOOLER_VALUES_CONFIG["kick_start_time"]
        # lane's values should never have been touched
        assert values._max_motor_rpm != lane.max_motor_rpm

    def test_recomputes_cruise_time_using_full_weight(self):
        values = _make_espooler_values()
        lane = self._make_lane_obj()

        values.handle_connect(lane)

        assert values.cruise_time == pytest.approx(
            _expected_cruise_time(ESPOOLER_VALUES_CONFIG["full_weight"]))


# ── Espooler_values: property getters/setters ──────────────────────────────

class TestEspoolerValuesProperties:
    def test_cruise_time_getter_setter(self):
        values = _make_espooler_values()
        values.cruise_time = 1.5
        assert values.cruise_time == 1.5

    def test_kick_start_time_scaled_by_scaling(self):
        values = _make_espooler_values(kick_start_time=2.0, spoolrate=3.0)
        assert values.kick_start_time == 6.0

    def test_kick_start_time_setter_updates_raw_value(self):
        values = _make_espooler_values(spoolrate=1.0)
        values.kick_start_time = 4.0
        assert values._kick_start_time == 4.0
        assert values.kick_start_time == 4.0  # scaling is 1.0 here

    def test_delta_movement_scaled_by_scaling(self):
        values = _make_espooler_values(delta_movement=5.0, spoolrate=2.0)
        assert values.delta_movement == 10.0

    def test_delta_movement_setter_updates_raw_value(self):
        values = _make_espooler_values(spoolrate=1.0)
        values.delta_movement = 7.0
        assert values._delta_movement == 7.0

    def test_outer_circ_is_diameter_times_pi(self):
        values = _make_espooler_values(spool_outer_diameter=100.0)
        assert values.outer_circ == pytest.approx(100.0 * math.pi)

    def test_delta_circ_is_diameter_difference_times_pi(self):
        values = _make_espooler_values(spool_outer_diameter=100.0, spool_inner_diameter=40.0)
        assert values.delta_circ == pytest.approx(60.0 * math.pi)

    def test_scaling_getter_setter(self):
        values = _make_espooler_values()
        values.scaling = 9.0
        assert values.scaling == 9.0

    def test_full_weight_getter_setter(self):
        values = _make_espooler_values()
        values.full_weight = 2500.0
        assert values.full_weight == 2500.0

    def test_spool_ratio_getter_setter(self):
        values = _make_espooler_values()
        values.spool_ratio = 3.5
        assert values.spool_ratio == 3.5

    def test_max_motor_rpm_getter_setter(self):
        values = _make_espooler_values()
        values.max_motor_rpm = 8000.0
        assert values.max_motor_rpm == 8000.0

    def test_espool_rot_dist_getter_setter(self):
        values = _make_espooler_values()
        values.espool_rot_dist = 12.0
        assert values.espool_rot_dist == 12.0

    def test_spool_outer_diameter_getter_setter(self):
        values = _make_espooler_values()
        values.spool_outer_diameter = 150.0
        assert values.spool_outer_diameter == 150.0

    def test_spool_inner_diameter_getter_setter(self):
        values = _make_espooler_values()
        values.spool_inner_diameter = 50.0
        assert values.spool_inner_diameter == 50.0


# ── Espooler.__init__ ───────────────────────────────────────────────────────

class TestEspoolerInit:
    def test_rwd_only_constructs_rwd_motor_no_macros(self):
        espooler = _make_real_espooler(has_rwd=True, has_fwd=False, has_enb=False)
        assert isinstance(espooler.afc_motor_rwd, AFCassistMotor)
        assert espooler.afc_motor_fwd is None
        assert espooler.afc_motor_enb is None

    def test_fwd_only_constructs_fwd_motor_and_registers_macros(self):
        espooler = _make_real_espooler(has_rwd=False, has_fwd=True, has_enb=False)
        assert isinstance(espooler.afc_motor_fwd, AFCassistMotor)
        registered = [c.args[0] for c in espooler.afc.gcode.register_mux_command.call_args_list]
        assert "SET_ESPOOLER_VALUES" in registered
        assert "TEST_ESPOOLER_ASSIST" in registered
        assert "ENABLE_ESPOOLER_ASSIST" in registered
        assert "DISABLE_ESPOOLER_ASSIST" in registered

    def test_no_fwd_pin_registers_no_espooler_macros(self):
        espooler = _make_real_espooler(has_rwd=True, has_fwd=False, has_enb=False)
        espooler.afc.gcode.register_mux_command.assert_not_called()

    def test_enb_pin_constructs_enb_motor(self):
        espooler = _make_real_espooler(has_enb=True)
        assert isinstance(espooler.afc_motor_enb, AFCassistMotor)

    def test_no_enb_pin_leaves_enb_none(self):
        espooler = _make_real_espooler(has_enb=False)
        assert espooler.afc_motor_enb is None

    def test_fwd_or_rwd_present_registers_reset_motor_time_macro(self):
        espooler = _make_real_espooler(has_rwd=True, has_fwd=False)
        espooler.function.register_mux_command.assert_called_once()
        assert espooler.function.register_mux_command.call_args.args[1] == "AFC_RESET_MOTOR_TIME"

    def test_neither_fwd_nor_rwd_skips_reset_motor_time_macro(self):
        espooler = _make_real_espooler(has_rwd=False, has_fwd=False)
        espooler.function.register_mux_command.assert_not_called()

    def test_name_stored(self):
        espooler = _make_real_espooler()
        assert espooler.name == "lane1"

    def test_lane_obj_initially_none(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(printer=printer, values=dict(ESPOOLER_VALUES_CONFIG))
        espooler = Espooler("lane1", config)
        assert espooler.lane_obj is None

    def test_past_extruder_position_initially_negative_one(self):
        espooler = _make_real_espooler()
        assert espooler.past_extruder_position == -1


# ── Espooler.handle_ready ───────────────────────────────────────────────────

class TestHandleReady:
    def test_fwd_present_starts_stats_timer(self):
        espooler = _make_real_espooler(has_rwd=False, has_fwd=True)
        espooler.reactor.update_timer = MagicMock()
        espooler.reactor.monotonic = MagicMock(return_value=1000.0)

        espooler.handle_ready()

        espooler.reactor.update_timer.assert_called_once_with(
            espooler.stats_timer, 1030.0)
        assert espooler.logger.messages == []

    def test_rwd_present_starts_stats_timer(self):
        espooler = _make_real_espooler(has_rwd=True, has_fwd=False)
        espooler.reactor.update_timer = MagicMock()
        espooler.reactor.monotonic = MagicMock(return_value=500.0)

        espooler.handle_ready()

        espooler.reactor.update_timer.assert_called_once_with(
            espooler.stats_timer, 530.0)
        assert espooler.logger.messages == []

    def test_neither_motor_logs_and_does_not_start_timer(self):
        espooler = _make_real_espooler(has_rwd=False, has_fwd=False)
        espooler.reactor.update_timer = MagicMock()

        espooler.handle_ready()

        espooler.reactor.update_timer.assert_not_called()
        assert espooler.logger.messages == [("info", "Not starting timer for lane1")]


# ── Espooler.handle_connect ─────────────────────────────────────────────────

class TestEspoolerHandleConnect:
    def _make_lane_obj(self):
        lane = MagicMock()
        lane.printer = "sentinel_printer"
        lane.outer_diameter = 111.0
        lane.inner_diameter = 22.0
        lane.max_motor_rpm = 3000.0
        lane.unit_obj.n20_break_delay_time = 0.25
        lane.unit_obj.timer_delay = 1.5
        lane.unit_obj.enable_assist = True
        lane.unit_obj.enable_assist_weight = 750.0
        lane.unit_obj.debug = True
        lane.unit_obj.enable_kick_start = True
        lane.unit_obj.kick_start_time = 0.9
        lane.unit_obj.espool_rot_dist = 9.0
        lane.unit_obj.delta_movement = 8.0
        lane.unit_obj.scaling = 2.0
        lane.unit_obj.spool_ratio = 4.0
        lane.unit_obj.full_weight = 1500.0
        return lane

    def test_unset_fields_fall_back_to_unit_obj(self):
        espooler = _make_real_espooler(
            n20_break_delay_time=None, timer_delay=None, enable_assist=None,
            enable_assist_weight=None, debug=None, enable_kick_start=None,
        )
        lane = self._make_lane_obj()

        espooler.handle_connect(lane)

        assert espooler.n20_break_delay_time == 0.25
        assert espooler.timer_delay == 1.5
        assert espooler.enable_assist is True
        assert espooler.enable_assist_weight == 750.0
        assert espooler.debug is True
        assert espooler.enable_kick_start is True

    def test_already_set_fields_not_overridden(self):
        espooler = _make_real_espooler(
            n20_break_delay_time=0.1, timer_delay=2.0, enable_assist=False,
            enable_assist_weight=100.0, debug=False, enable_kick_start=False,
        )
        lane = self._make_lane_obj()

        espooler.handle_connect(lane)

        assert espooler.n20_break_delay_time == 0.1
        assert espooler.timer_delay == 2.0
        assert espooler.enable_assist is False
        assert espooler.enable_assist_weight == 100.0
        assert espooler.debug is False
        assert espooler.enable_kick_start is False

    def test_stats_becomes_real_afc_espooler_stats(self):
        espooler = _make_real_espooler()
        lane = self._make_lane_obj()
        assert espooler.stats is None

        espooler.handle_connect(lane)

        assert isinstance(espooler.stats, AFCEspoolerStats)

    def test_lane_obj_stored(self):
        espooler = _make_real_espooler()
        lane = self._make_lane_obj()

        espooler.handle_connect(lane)

        assert espooler.lane_obj is lane

    def test_espooler_values_handle_connect_delegated(self):
        espooler = _make_real_espooler()
        lane = self._make_lane_obj()

        espooler.handle_connect(lane)

        # espooler_values.handle_connect recomputes cruise_time from full_weight
        assert espooler.espooler_values.cruise_time == pytest.approx(
            _expected_cruise_time(ESPOOLER_VALUES_CONFIG["full_weight"]))


# ── Espooler.handle_moonraker_connect ───────────────────────────────────────

class TestHandleMoonrakerConnect:
    def test_delegates_to_stats(self):
        espooler = _make_real_espooler()
        espooler.handle_connect(MagicMock(unit_obj=MagicMock()))
        espooler.stats.handle_moonraker_stats = MagicMock()

        espooler.handle_moonraker_connect()

        espooler.stats.handle_moonraker_stats.assert_called_once_with()


# ── Espooler.timer_stats_callback ───────────────────────────────────────────

class TestTimerStatsCallback:
    def test_updates_database_when_not_printing(self):
        espooler = _make_real_espooler()
        espooler.handle_connect(MagicMock(unit_obj=MagicMock()))
        espooler.afc.function.is_printing = MagicMock(return_value=False)
        espooler.stats.update_database = MagicMock()
        espooler.reactor.monotonic = MagicMock(return_value=100.0)

        result = espooler.timer_stats_callback(0.0)

        espooler.afc.function.is_printing.assert_called_once_with(True)
        espooler.stats.update_database.assert_called_once()
        assert result == 130.0

    def test_skips_update_when_printing(self):
        espooler = _make_real_espooler()
        espooler.handle_connect(MagicMock(unit_obj=MagicMock()))
        espooler.afc.function.is_printing = MagicMock(return_value=True)
        espooler.stats.update_database = MagicMock()

        espooler.timer_stats_callback(0.0)

        espooler.stats.update_database.assert_not_called()


# ── Espooler.timer_callback ─────────────────────────────────────────────────

class TestTimerCallback:
    def _setup(self, enable_assist=True, in_print=True, is_paused=False, in_toolchange=False,
              past_extruder_position=-1, extruder_pos=0.0, delta_movement=5.0, debug=False):
        espooler = _make_real_espooler(delta_movement=delta_movement, debug=debug)
        espooler.enable_assist = enable_assist
        espooler.afc.function.in_print = MagicMock(return_value=in_print)
        espooler.afc.function.is_paused = MagicMock(return_value=is_paused)
        espooler.afc.in_toolchange = in_toolchange
        espooler.afc.function.get_extruder_pos = MagicMock(return_value=extruder_pos)
        espooler.past_extruder_position = past_extruder_position
        espooler.do_assist_move = MagicMock()
        espooler.reactor.monotonic = MagicMock(return_value=1000.0)
        espooler.timer_delay = 2.0
        return espooler

    def test_returns_monotonic_plus_timer_delay_regardless_of_branch(self):
        espooler = self._setup(enable_assist=False)
        result = espooler.timer_callback(0.0)
        assert result == 1002.0
        assert espooler.logger.messages == []

    def test_enable_assist_false_blocks_even_when_others_true(self):
        espooler = self._setup(enable_assist=False, in_print=True, is_paused=False, in_toolchange=False)
        espooler.timer_callback(0.0)
        espooler.afc.function.get_extruder_pos.assert_not_called()
        assert espooler.logger.messages == []

    def test_not_in_print_blocks_even_when_others_true(self):
        espooler = self._setup(enable_assist=True, in_print=False, is_paused=False, in_toolchange=False)
        espooler.timer_callback(0.0)
        espooler.afc.function.get_extruder_pos.assert_not_called()
        assert espooler.logger.messages == []

    def test_is_paused_blocks_even_when_others_true(self):
        espooler = self._setup(enable_assist=True, in_print=True, is_paused=True, in_toolchange=False)
        espooler.timer_callback(0.0)
        espooler.afc.function.get_extruder_pos.assert_not_called()
        assert espooler.logger.messages == []

    def test_in_toolchange_blocks_even_when_others_true(self):
        espooler = self._setup(enable_assist=True, in_print=True, is_paused=False, in_toolchange=True)
        espooler.timer_callback(0.0)
        espooler.afc.function.get_extruder_pos.assert_not_called()
        assert espooler.logger.messages == []

    def test_all_conditions_true_proceeds(self):
        espooler = self._setup(enable_assist=True, in_print=True, is_paused=False, in_toolchange=False)
        espooler.timer_callback(0.0)
        espooler.afc.function.get_extruder_pos.assert_called_once_with(0.0, -1)
        assert espooler.logger.messages == []  # debug defaults to False

    def test_initial_position_negative_one_just_records_position(self):
        espooler = self._setup(past_extruder_position=-1, extruder_pos=42.0)
        espooler.timer_callback(0.0)
        assert espooler.past_extruder_position == 42.0
        espooler.do_assist_move.assert_not_called()
        assert espooler.logger.messages == []

    def test_delta_exceeds_threshold_triggers_assist_move(self):
        espooler = self._setup(past_extruder_position=10.0, extruder_pos=20.0, delta_movement=5.0)
        espooler._get_print_time = MagicMock(return_value=999.0)
        espooler.timer_callback(0.0)
        assert espooler.past_extruder_position == 20.0
        espooler.do_assist_move.assert_called_once_with(999.0)
        assert espooler.logger.messages == []

    def test_delta_within_threshold_does_not_trigger_assist_move(self):
        espooler = self._setup(past_extruder_position=10.0, extruder_pos=12.0, delta_movement=5.0)
        espooler.timer_callback(0.0)
        assert espooler.past_extruder_position == 10.0
        espooler.do_assist_move.assert_not_called()
        assert espooler.logger.messages == []

    def test_debug_true_logs_message(self):
        espooler = self._setup(past_extruder_position=10.0, extruder_pos=20.0, delta_movement=5.0, debug=True)
        espooler._get_print_time = MagicMock(return_value=999.0)
        espooler.timer_callback(1.5)
        expected = "Timer Callback 1.500 e:20.000 d:10.000 p:20.000"
        assert espooler.logger.messages == [("info", expected)]

    def test_debug_false_no_log(self):
        espooler = self._setup(past_extruder_position=10.0, extruder_pos=20.0, delta_movement=5.0, debug=False)
        espooler.timer_callback(0.0)
        assert espooler.logger.messages == []


# ── Espooler._get_print_time ────────────────────────────────────────────────

class TestGetPrintTime:
    def test_uses_current_monotonic_when_systime_none(self):
        espooler = _make_real_espooler()
        espooler.reactor.monotonic = MagicMock(return_value=100.0)
        espooler.mcu.estimated_print_time = MagicMock(return_value=555.0)

        result = espooler._get_print_time(None)

        espooler.mcu.estimated_print_time.assert_called_once_with(100.0 + PIN_MIN_TIME)
        assert result == 555.0

    def test_uses_provided_systime_when_given(self):
        espooler = _make_real_espooler()
        espooler.reactor.monotonic = MagicMock(return_value=100.0)
        espooler.mcu.estimated_print_time = MagicMock(return_value=777.0)

        result = espooler._get_print_time(200.0)

        espooler.mcu.estimated_print_time.assert_called_once_with(200.0)
        assert result == 777.0

    def test_clamps_to_minimum_pin_time_when_systime_too_close(self):
        espooler = _make_real_espooler()
        espooler.reactor.monotonic = MagicMock(return_value=100.0)
        espooler.mcu.estimated_print_time = MagicMock(return_value=42.0)

        espooler._get_print_time(100.01)

        espooler.mcu.estimated_print_time.assert_called_once_with(100.0 + PIN_MIN_TIME)


# ── Espooler._kick_start ────────────────────────────────────────────────────

class TestKickStart:
    def test_forward_calls_move_forwards_not_move_reverse(self):
        espooler = _make_real_espooler()
        espooler.move_forwards = MagicMock()
        espooler.move_reverse = MagicMock()

        result = espooler._kick_start(1000.0, reverse=False)

        espooler.move_forwards.assert_called_once_with(1000.0, 1)
        espooler.move_reverse.assert_not_called()
        assert result == pytest.approx(1000.0 + espooler.espooler_values.kick_start_time)

    def test_reverse_calls_move_reverse_not_move_forwards(self):
        espooler = _make_real_espooler()
        espooler.move_forwards = MagicMock()
        espooler.move_reverse = MagicMock()

        result = espooler._kick_start(1000.0, reverse=True)

        espooler.move_reverse.assert_called_once_with(1000.0, 1)
        espooler.move_forwards.assert_not_called()
        assert result == pytest.approx(1000.0 + espooler.espooler_values.kick_start_time)

    def test_default_direction_is_forward(self):
        espooler = _make_real_espooler()
        espooler.move_forwards = MagicMock()
        espooler.move_reverse = MagicMock()

        espooler._kick_start(1000.0)

        espooler.move_forwards.assert_called_once()
        espooler.move_reverse.assert_not_called()


# ── Espooler.set_enable_pin ─────────────────────────────────────────────────

class TestSetEnablePin:
    def test_enb_present_and_value_zero_sets_end_time(self):
        espooler = _make_real_espooler(has_enb=True)
        _connect_stats(espooler)
        espooler.afc_motor_enb.last_value = 1.0  # so _set_pin sees a real transition
        espooler.stats._direction = EspoolerDir.FWD  # avoid end_time's own no-op guard
        espooler.stats._direction_start = 500.0  # must be set + < print_time for the real setter to run cleanly

        espooler.set_enable_pin(1000.0, 0)

        assert espooler.afc_motor_enb.last_value == 0
        # end_time's setter resets _direction_end back to None once it
        # applies the delta -- confirm it actually ran via the real
        # observable side effect (the runtime delta) rather than the
        # transient getter.
        assert espooler.stats._n20_runtime_fwd.value == 500.0
        assert espooler.stats.start_time is None

    def test_enb_present_and_nonzero_value_sets_start_time(self):
        espooler = _make_real_espooler(has_enb=True)
        _connect_stats(espooler)

        espooler.set_enable_pin(1000.0, 1)

        assert espooler.afc_motor_enb.last_value == 1
        assert espooler.stats.start_time == 1000.0
        assert espooler.stats.end_time is None

    def test_enb_absent_still_sets_end_time_for_zero_value(self):
        espooler = _make_real_espooler(has_enb=False)
        _connect_stats(espooler)
        espooler.stats._direction = EspoolerDir.FWD  # avoid end_time's own no-op guard
        espooler.stats._direction_start = 500.0  # must be set + < print_time for the real setter to run cleanly

        espooler.set_enable_pin(1000.0, 0)

        assert espooler.stats._n20_runtime_fwd.value == 500.0

    def test_enb_absent_still_sets_start_time_for_nonzero_value(self):
        espooler = _make_real_espooler(has_enb=False)
        _connect_stats(espooler)

        espooler.set_enable_pin(1000.0, 1)

        assert espooler.stats.start_time == 1000.0


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
        assert espooler.logger.messages == []  # debug defaults to False

    def test_uses_provided_print_time_without_calling_get_print_time(self):
        # weight above threshold keeps this isolated to just the ternary
        espooler = _make_real_espooler(weight=900.0, enable_assist_weight=500.0)
        espooler._get_print_time = MagicMock()

        espooler.do_assist_move(1000.0)

        espooler._get_print_time.assert_not_called()
        assert espooler.logger.messages == []

    def test_computes_print_time_when_none_provided(self):
        espooler = _make_real_espooler(weight=900.0, enable_assist_weight=500.0)
        espooler._get_print_time = MagicMock(return_value=2000.0)
        espooler.espooler_values.cruise_time = 0.0  # simulate handle_connect having run

        espooler.do_assist_move(None)

        espooler._get_print_time.assert_called_once_with()
        assert espooler.logger.messages == []

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
        assert espooler.logger.messages == []  # debug defaults to False

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
        assert espooler.logger.messages == []

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
        assert espooler.logger.messages == []

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


# ── Espooler.move_forwards / move_reverse ───────────────────────────────────

class TestMoveForwardsReverse:
    def test_move_forwards_sets_fwd_direction_and_pin(self):
        espooler = _make_real_espooler()
        _connect_stats(espooler)
        espooler.set_enable_pin = MagicMock()

        espooler.move_forwards(1000.0, 0.8)

        assert espooler.stats._direction == EspoolerDir.FWD
        espooler.set_enable_pin.assert_called_once_with(1000.0, 1)
        assert espooler.afc_motor_fwd.last_value == 0.8

    def test_move_reverse_sets_rwd_direction_and_pin(self):
        espooler = _make_real_espooler()
        _connect_stats(espooler)
        espooler.set_enable_pin = MagicMock()

        espooler.move_reverse(1000.0, 0.6)

        assert espooler.stats._direction == EspoolerDir.RWD
        espooler.set_enable_pin.assert_called_once_with(1000.0, 1)
        assert espooler.afc_motor_rwd.last_value == 0.6


# ── Espooler.break_espooler ─────────────────────────────────────────────────

class TestBreakEspooler:
    def test_enb_present_brakes_then_releases_fwd_and_rwd(self):
        espooler = _make_real_espooler(has_enb=True)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)
        espooler.n20_break_delay_time = 0.5
        espooler.set_enable_pin = MagicMock()
        espooler.afc_motor_rwd._set_pin = MagicMock(wraps=espooler.afc_motor_rwd._set_pin)
        espooler.afc_motor_fwd._set_pin = MagicMock(wraps=espooler.afc_motor_fwd._set_pin)

        espooler.break_espooler()

        # brake phase: rwd + fwd set to 1 at print_time
        assert espooler.afc_motor_rwd._set_pin.call_args_list[0].args == (1000.0, 1)
        assert espooler.afc_motor_fwd._set_pin.call_args_list[0].args == (1000.0, 1)
        # release phase: rwd + fwd set to 0 at print_time + break_delay
        assert espooler.afc_motor_rwd._set_pin.call_args_list[1].args == (1000.5, 0)
        assert espooler.afc_motor_fwd._set_pin.call_args_list[1].args == (1000.5, 0)
        assert espooler.set_enable_pin.call_args_list == [
            ((1000.0, 1),), ((1000.5, 0),)
        ]
        # final resting state after brake+release
        assert espooler.afc_motor_rwd.last_value == 0
        assert espooler.afc_motor_fwd.last_value == 0

    def test_enb_present_but_no_fwd_disables_rwd_and_enb_directly(self):
        """Without both direction pins there's no electronic-brake trick to
        do, so this falls to the single-pin path: no brake-to-1 step, just
        an immediate disable of whatever pins actually exist (rwd + enb;
        fwd doesn't exist to touch)."""
        espooler = _make_real_espooler(has_rwd=True, has_fwd=False, has_enb=True)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)
        espooler.n20_break_delay_time = 0.5
        espooler.set_enable_pin = MagicMock()

        espooler.break_espooler()  # should not raise despite afc_motor_fwd is None

        espooler.set_enable_pin.assert_called_once_with(1000.0, 0)
        assert espooler.afc_motor_rwd.last_value == 0

    def test_enb_present_but_no_rwd_disables_fwd_and_enb_directly(self):
        """Symmetric to the no-fwd case above: rwd doesn't exist to touch,
        but fwd + enb must still be disabled directly."""
        espooler = _make_real_espooler(has_rwd=False, has_fwd=True, has_enb=True)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)
        espooler.set_enable_pin = MagicMock()

        espooler.break_espooler()  # should not raise despite afc_motor_rwd is None

        espooler.set_enable_pin.assert_called_once_with(1000.0, 0)
        assert espooler.afc_motor_fwd.last_value == 0

    def test_fwd_only_no_enb_still_disables_fwd(self):
        """The original regression: a fwd-only espooler with no enable pin
        must still have its fwd motor zeroed, not silently left running."""
        espooler = _make_real_espooler(has_rwd=False, has_fwd=True, has_enb=False)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)
        espooler.assist(1.0)
        assert espooler.afc_motor_fwd.last_value == 1.0  # sanity: motor is running

        espooler.assist(0)  # routes through break_espooler()

        assert espooler.afc_motor_fwd.last_value == 0

    def test_enb_absent_both_directions_present_disables_both_directly(self):
        """Without enb there's no way to actually engage the h-bridge brake
        (per the datasheet, driving both direction pins high does nothing
        unless the driver is enabled), so both direction pins are zeroed
        directly -- a single call each, not the brake-to-1-then-release-to-0
        sequence used when all three pins exist. The call-count assertion is
        what actually distinguishes this from the full-cycle branch;
        previously (before either fix) only rwd was ever touched here,
        silently leaving fwd energized."""
        espooler = _make_real_espooler(has_enb=False)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)
        espooler.n20_break_delay_time = 0.5
        espooler.set_enable_pin = MagicMock()
        espooler.afc_motor_rwd._set_pin = MagicMock(wraps=espooler.afc_motor_rwd._set_pin)
        espooler.afc_motor_fwd._set_pin = MagicMock(wraps=espooler.afc_motor_fwd._set_pin)

        espooler.break_espooler()

        espooler.set_enable_pin.assert_not_called()
        espooler.afc_motor_rwd._set_pin.assert_called_once_with(1000.0, 0)
        espooler.afc_motor_fwd._set_pin.assert_called_once_with(1000.0, 0)

    def test_no_motors_only_disables_enb(self):
        """With neither direction pin configured there is nothing to brake
        or zero, but a stray enable pin (an unusual config) is still
        disabled defensively."""
        espooler = _make_real_espooler(has_rwd=False, has_fwd=False, has_enb=True)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)
        espooler.set_enable_pin = MagicMock()

        espooler.break_espooler()  # should not raise despite no motors configured

        espooler.set_enable_pin.assert_called_once_with(1000.0, 0)

    def test_nothing_configured_is_a_true_noop(self):
        espooler = _make_real_espooler(has_rwd=False, has_fwd=False, has_enb=False)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)
        espooler.set_enable_pin = MagicMock()

        espooler.break_espooler()  # should not raise despite nothing configured

        espooler.set_enable_pin.assert_not_called()


# ── Espooler.assist ──────────────────────────────────────────────────────────

class TestAssist:
    def test_positive_value_drives_forward_even_without_rwd_motor(self):
        """A missing RWD motor must not block FWD-bound assist calls -- only
        the top-level "both pins missing" guard and each direction's own
        motor check should gate early return."""
        espooler = _make_real_espooler(has_rwd=False, has_fwd=True, pwm=True)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)

        espooler.assist(0.5)

        assert espooler.stats._direction == EspoolerDir.FWD
        assert espooler.stats.start_time == 1000.0
        assert espooler.afc_motor_fwd.last_value == pytest.approx(0.5)

    def test_negative_value_returns_early_when_no_rwd_motor(self):
        """A reverse-bound value with no RWD motor configured must still
        short-circuit -- verified via state that would have changed had
        execution continued past the guard."""
        espooler = _make_real_espooler(has_rwd=False, has_fwd=True)
        _connect_stats(espooler)
        original_fwd_value = espooler.afc_motor_fwd.last_value

        espooler.assist(-0.5)

        assert espooler.stats._direction is None
        assert espooler.stats.start_time is None
        assert espooler.afc_motor_fwd.last_value == original_fwd_value

    def test_returns_early_when_both_motors_missing(self):
        """With neither pin configured, assist(0) must not fall through to
        break_espooler() -- the value==0 branch has no per-motor check of
        its own, so the top-level guard is the only thing stopping it."""
        espooler = _make_real_espooler(has_rwd=False, has_fwd=False)
        _connect_stats(espooler)
        espooler.break_espooler = MagicMock()

        espooler.assist(0)

        espooler.break_espooler.assert_not_called()
        assert espooler.stats.end_time is None

    def test_negative_value_drives_reverse(self):
        espooler = _make_real_espooler(has_rwd=True, has_fwd=True, pwm=True)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)

        espooler.assist(-0.5)

        assert espooler.stats._direction == EspoolerDir.RWD
        assert espooler.stats.start_time == 1000.0
        assert espooler.afc_motor_rwd.last_value == pytest.approx(0.5)

    def test_positive_value_drives_forward(self):
        espooler = _make_real_espooler(has_rwd=True, has_fwd=True, pwm=True)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)

        espooler.assist(0.5)

        assert espooler.stats._direction == EspoolerDir.FWD
        assert espooler.stats.start_time == 1000.0
        assert espooler.afc_motor_fwd.last_value == pytest.approx(0.5)

    def test_positive_value_returns_early_when_no_fwd_motor(self):
        espooler = _make_real_espooler(has_rwd=True, has_fwd=False)
        _connect_stats(espooler)

        espooler.assist(0.5)  # should not raise despite afc_motor_fwd is None

        assert espooler.stats._direction is None  # never got that far

    def test_zero_value_brakes_and_sets_end_time(self):
        espooler = _make_real_espooler(has_rwd=True, has_fwd=True)
        _connect_stats(espooler)
        espooler.stats._direction = EspoolerDir.FWD
        espooler.stats._direction_start = 1.0
        espooler._get_print_time = MagicMock(return_value=1000.0)
        espooler.break_espooler = MagicMock()

        espooler.assist(0)

        espooler.break_espooler.assert_called_once()
        # end_time's setter resets _direction_end after applying the delta --
        # confirm the real side effect (the runtime got a delta added) fired.
        assert espooler.stats._n20_runtime_fwd.value == pytest.approx(999.0)

    def test_non_comparable_value_falls_through_to_brake_not_crash(self):
        """value < 0, value > 0, and value == 0 are all False for NaN -- the
        only float that satisfies none of them. Rather than leaving that as
        an unhandled gap (previously an UnboundLocalError from the unset
        assist_motor), it now falls through to the same safe disable/brake
        path as value == 0, matching the docstring's stated 3-way contract
        (negative/positive/anything-else)."""
        espooler = _make_real_espooler(has_rwd=True, has_fwd=True)
        _connect_stats(espooler)
        espooler.stats._direction = EspoolerDir.FWD
        espooler.stats._direction_start = 1.0
        espooler._get_print_time = MagicMock(return_value=1000.0)
        espooler.break_espooler = MagicMock()

        espooler.assist(float("nan"))  # should not raise

        espooler.break_espooler.assert_called_once()
        assert espooler.stats._n20_runtime_fwd.value == pytest.approx(999.0)

    def test_digital_motor_clamps_fractional_value_to_one(self):
        """Non-PWM motors only support 0/1 -- a fractional positive value
        must clamp to 1, not error or stay fractional."""
        espooler = _make_real_espooler(has_rwd=True, has_fwd=True)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)
        assert espooler.afc_motor_fwd.is_pwm is False

        espooler.assist(0.3)

        assert espooler.afc_motor_fwd.last_value == 1

    def test_enb_present_gets_enabled_for_nonzero_value(self):
        espooler = _make_real_espooler(has_rwd=True, has_fwd=True, has_enb=True)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)

        espooler.assist(0.5)

        assert espooler.afc_motor_enb.last_value == 1

    def test_enable_kick_start_true_offsets_print_time(self):
        espooler = _make_real_espooler(has_rwd=True, has_fwd=True, enable_kick_start=True)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)
        espooler.move_forwards = MagicMock()  # avoid the kick-start's own side effects

        espooler.assist(0.5)

        expected_kicked_time = 1000.0 + espooler.espooler_values.kick_start_time
        assert espooler.afc_motor_fwd.last_print_time == pytest.approx(expected_kicked_time)

    def test_enable_kick_start_false_uses_original_print_time(self):
        espooler = _make_real_espooler(has_rwd=True, has_fwd=True, enable_kick_start=False)
        _connect_stats(espooler)
        espooler._get_print_time = MagicMock(return_value=1000.0)

        espooler.assist(0.5)

        assert espooler.afc_motor_fwd.last_print_time == 1000.0


# ── Espooler.enable_timer / disable_timer ───────────────────────────────────

class TestEnableTimer:
    def test_returns_early_when_no_fwd_motor(self):
        espooler = _make_real_espooler(has_fwd=False)
        espooler.past_extruder_position = 42.0
        espooler.reactor.update_timer = MagicMock()

        espooler.enable_timer()

        assert espooler.past_extruder_position == 42.0  # untouched
        espooler.reactor.update_timer.assert_not_called()
        assert espooler.logger.messages == []

    def test_resets_position_but_skips_timer_when_assist_disabled(self):
        espooler = _make_real_espooler(has_fwd=True)
        espooler.enable_assist = False
        espooler.past_extruder_position = 42.0
        espooler.reactor.update_timer = MagicMock()

        espooler.enable_timer()

        assert espooler.past_extruder_position == -1
        espooler.reactor.update_timer.assert_not_called()
        assert espooler.logger.messages == []

    def test_starts_timer_when_assist_enabled(self):
        espooler = _make_real_espooler(has_fwd=True)
        espooler.enable_assist = True
        espooler.timer_delay = 5.0
        espooler.debug = False
        espooler.reactor.update_timer = MagicMock()
        espooler.reactor.monotonic = MagicMock(return_value=100.0)

        espooler.enable_timer()

        espooler.reactor.update_timer.assert_called_once_with(espooler.callback_timer, 105.0)
        assert espooler.logger.messages == []

    def test_debug_true_logs_when_timer_started(self):
        espooler = _make_real_espooler(has_fwd=True)
        espooler.enable_assist = True
        espooler.debug = True
        espooler.timer_delay = 1.0
        espooler.reactor.update_timer = MagicMock()

        espooler.enable_timer()

        assert espooler.logger.messages == [("info", "lane1 espooler timer enabled")]


class TestDisableTimer:
    def test_returns_early_when_no_fwd_motor(self):
        espooler = _make_real_espooler(has_fwd=False)
        espooler.past_extruder_position = 42.0
        espooler.reactor.update_timer = MagicMock()

        espooler.disable_timer()

        assert espooler.past_extruder_position == 42.0
        espooler.reactor.update_timer.assert_not_called()

    def test_resets_position_and_disables_timer(self):
        espooler = _make_real_espooler(has_fwd=True)
        espooler.past_extruder_position = 42.0
        espooler.reactor.update_timer = MagicMock()

        espooler.disable_timer()

        assert espooler.past_extruder_position == -1
        espooler.reactor.update_timer.assert_called_once_with(
            espooler.callback_timer, espooler.reactor.NEVER)


# ── Espooler.get_spooler_stats ──────────────────────────────────────────────

class TestGetSpoolerStats:
    def test_neither_motor_returns_empty_string(self):
        espooler = _make_real_espooler(has_fwd=False, has_rwd=False)
        assert espooler.get_spooler_stats() == ""

    def test_fwd_only_long_format(self):
        espooler = _make_real_espooler(has_fwd=True, has_rwd=False)
        _connect_stats(espooler)
        espooler.stats._n20_runtime_fwd.value = 12.0

        result = espooler.get_spooler_stats(short=False)

        assert result == f"N20 active time: fwd:{'12.00s':>8}"

    def test_rwd_only_long_format(self):
        espooler = _make_real_espooler(has_fwd=False, has_rwd=True)
        _connect_stats(espooler)
        espooler.stats._n20_runtime_rwd.value = 8.0

        result = espooler.get_spooler_stats(short=False)

        assert result == f"N20 active time: rwd:{'8.00s':>8}"

    def test_both_motors_long_format(self):
        espooler = _make_real_espooler(has_fwd=True, has_rwd=True)
        _connect_stats(espooler)
        espooler.stats._n20_runtime_fwd.value = 12.0
        espooler.stats._n20_runtime_rwd.value = 8.0

        result = espooler.get_spooler_stats(short=False)

        assert result == f"N20 active time: fwd:{'12.00s':>8} rwd:{'8.00s':>8}"

    def test_both_motors_short_format(self):
        espooler = _make_real_espooler(has_fwd=True, has_rwd=True)
        _connect_stats(espooler)
        espooler.stats._n20_runtime_fwd.value = 12.0
        espooler.stats._n20_runtime_rwd.value = 8.0

        result = espooler.get_spooler_stats(short=True)

        ret_str = "N20 active time:"
        ret_str += " fwd:"
        ret_str = f"{ret_str:{' '}>31}{'12.00s':>8}   |\n"
        ret_str += "|" + f"{'rwd:':{' '}>31}{'8.00s':>8}   "
        assert result == ret_str


# ── Espooler gcode macros ────────────────────────────────────────────────────

class TestCmdTestEspoolerAssist:
    def test_delegates_to_do_assist_move_with_computed_print_time(self):
        espooler = _make_real_espooler()
        espooler._get_print_time = MagicMock(return_value=1234.0)
        espooler.do_assist_move = MagicMock()

        espooler.cmd_TEST_ESPOOLER_ASSIST(MagicMock())

        espooler.do_assist_move.assert_called_once_with(1234.0)


class TestCmdEnableEspoolerAssist:
    def test_current_lane_matches_enables_timer_and_logs(self):
        espooler = _make_real_espooler()
        espooler.afc.function.get_current_lane = MagicMock(return_value="lane1")
        espooler.enable_timer = MagicMock()

        espooler.cmd_ENABLE_ESPOOLER_ASSIST(MagicMock())

        assert espooler.enable_assist is True
        espooler.enable_timer.assert_called_once()
        assert espooler.logger.messages == [("info", "Espooler assist enabled for lane1")]

    def test_current_lane_differs_skips_timer_and_logs_alternate_message(self):
        espooler = _make_real_espooler()
        espooler.afc.function.get_current_lane = MagicMock(return_value="lane2")
        espooler.enable_timer = MagicMock()

        espooler.cmd_ENABLE_ESPOOLER_ASSIST(MagicMock())

        assert espooler.enable_assist is True
        espooler.enable_timer.assert_not_called()
        assert espooler.logger.messages == [
            ("info", "lane1 currently not loaded only enabling assist, not enabling timer")
        ]


class TestCmdDisableEspoolerAssist:
    def test_disables_timer_and_assist_and_logs(self):
        espooler = _make_real_espooler()
        espooler.enable_assist = True
        espooler.disable_timer = MagicMock()

        espooler.cmd_DISABLE_ESPOOLER_ASSIST(MagicMock())

        espooler.disable_timer.assert_called_once()
        assert espooler.enable_assist is False
        assert espooler.logger.messages == [("info", "Espooler assist disabled for lane1")]


class TestCmdSetEspoolerValues:
    def _make_gcmd_passthrough(self):
        """gcode_get_value's 3rd positional arg is the current value to fall
        back to when the gcmd doesn't override it -- returning it unchanged
        simulates 'no override supplied' for every field."""
        return MagicMock(side_effect=lambda gcmd, getter, current, *a, **kw: current)

    def test_all_fields_left_unchanged_when_gcmd_supplies_nothing(self):
        espooler = _make_real_espooler()
        espooler.handle_connect(MagicMock(unit_obj=MagicMock()))
        espooler.lane_obj.fullname = "lane1"
        original_break_delay = espooler.n20_break_delay_time
        original_timer_delay = espooler.timer_delay
        espooler.function.gcode_get_value = self._make_gcmd_passthrough()

        espooler.cmd_SET_ESPOOLER_VALUES(MagicMock())

        assert espooler.n20_break_delay_time == original_break_delay
        assert espooler.timer_delay == original_timer_delay

    def test_queries_every_field_with_correct_gcode_param_name(self):
        espooler = _make_real_espooler()
        espooler.handle_connect(MagicMock(unit_obj=MagicMock()))
        espooler.lane_obj.fullname = "lane1"
        espooler.function.gcode_get_value = self._make_gcmd_passthrough()

        espooler.cmd_SET_ESPOOLER_VALUES(MagicMock())

        queried_params = [c.args[3] for c in espooler.function.gcode_get_value.call_args_list]
        for expected_param in ("BREAK_DELAY", "KICK_START_TIME", "SPOOL_OUTER_DIAMETER",
                               "SPOOL_INNER_DIAMETER", "FULL_WEIGHT", "SPOOL_RATIO",
                               "MAX_MOTOR_RPM", "ESPOOL_ROT_DIST", "DELTA_MOVEMENT",
                               "SPOOLRATE", "TIMER_DELAY", "ASSIST_WEIGHT",
                               "ENABLE_ASSIST", "DEBUG", "ENABLE_KICK_START"):
            assert expected_param in queried_params, f"{expected_param} was not queried"

    def test_gcmd_override_is_applied(self):
        espooler = _make_real_espooler()
        espooler.handle_connect(MagicMock(unit_obj=MagicMock()))
        espooler.lane_obj.fullname = "lane1"

        def side_effect(gcmd, getter, current, param_name, *a, **kw):
            if param_name == "BREAK_DELAY":
                return 0.75
            return current
        espooler.function.gcode_get_value = MagicMock(side_effect=side_effect)

        espooler.cmd_SET_ESPOOLER_VALUES(MagicMock())

        assert espooler.n20_break_delay_time == 0.75

    def test_recomputes_cruise_time_from_full_weight(self):
        espooler = _make_real_espooler()
        espooler.handle_connect(MagicMock(unit_obj=MagicMock()))
        espooler.lane_obj.fullname = "lane1"
        espooler.function.gcode_get_value = self._make_gcmd_passthrough()

        espooler.cmd_SET_ESPOOLER_VALUES(MagicMock())

        assert espooler.espooler_values.cruise_time == pytest.approx(
            _expected_cruise_time(ESPOOLER_VALUES_CONFIG["full_weight"]))

    def test_every_override_lands_on_the_correct_attribute(self):
        """Gives every one of the 15 GCODE params a distinct override value
        and confirms each lands on its own specific attribute -- catches a
        copy-paste bug that swapped two fields' destinations, which
        test_gcmd_override_is_applied (checking only BREAK_DELAY) would not."""
        espooler = _make_real_espooler()
        espooler.handle_connect(MagicMock(unit_obj=MagicMock()))
        espooler.lane_obj.fullname = "lane1"

        overrides = {
            "BREAK_DELAY": 1.1,
            "KICK_START_TIME": 2.2,
            "SPOOL_OUTER_DIAMETER": 3.3,
            "SPOOL_INNER_DIAMETER": 4.4,
            "FULL_WEIGHT": 5.5,
            "SPOOL_RATIO": 6.6,
            "MAX_MOTOR_RPM": 7.7,
            "ESPOOL_ROT_DIST": 8.8,
            "DELTA_MOVEMENT": 9.9,
            "SPOOLRATE": 1.0,  # keep at 1.0 so the scaled getters below stay simple
            "TIMER_DELAY": 11.11,
            "ASSIST_WEIGHT": 12.12,
            "ENABLE_ASSIST": True,
            "DEBUG": True,
            "ENABLE_KICK_START": True,
        }

        def side_effect(gcmd, getter, current, param_name, *a, **kw):
            return overrides[param_name]
        espooler.function.gcode_get_value = MagicMock(side_effect=side_effect)

        espooler.cmd_SET_ESPOOLER_VALUES(MagicMock())

        assert espooler.n20_break_delay_time == 1.1
        assert espooler.espooler_values._kick_start_time == 2.2
        assert espooler.espooler_values._spool_outer_diameter == 3.3
        assert espooler.espooler_values._spool_inner_diameter == 4.4
        assert espooler.espooler_values._full_weight == 5.5
        assert espooler.espooler_values._spool_ratio == 6.6
        assert espooler.espooler_values._max_motor_rpm == 7.7
        assert espooler.espooler_values._espool_rot_dist == 8.8
        assert espooler.espooler_values._delta_movement == 9.9
        assert espooler.espooler_values._scaling == 1.0
        assert espooler.timer_delay == 11.11
        assert espooler.enable_assist_weight == 12.12
        assert espooler.enable_assist is True
        assert espooler.debug is True
        assert espooler.enable_kick_start is True
        # cruise_time recompute uses all the just-overridden values, not the
        # module-level defaults
        overridden_cfg = {
            "max_motor_rpm": 7.7,
            "espool_rot_dist": 8.8,
            "spool_ratio": 6.6,
            "full_weight": 5.5,
            "spool_outer_diameter": 3.3,
            "spool_inner_diameter": 4.4,
            "delta_movement": 9.9,
        }
        assert espooler.espooler_values.cruise_time == pytest.approx(
            _expected_cruise_time(5.5, cfg=overridden_cfg))


class TestCmdAfcResetMotorTime:
    def test_resets_runtimes_and_logs(self):
        espooler = _make_real_espooler()
        _connect_stats(espooler)
        espooler.stats.reset_runtimes = MagicMock()

        espooler.cmd_AFC_RESET_MOTOR_TIME(MagicMock())

        espooler.stats.reset_runtimes.assert_called_once()
        assert espooler.logger.messages == [
            ("info", "N20 active time has been reset for lane1")
        ]


# ═════════════════════════════════════════════════════════════════════════
# Module-level import guards
# ═════════════════════════════════════════════════════════════════════════

def _exec_afc_assist_with_blocked_dependency(blocked_module_name):
    """Execute a throw-away copy of extras/AFC_assist.py's module-level code
    with `blocked_module_name` forced to fail import, to exercise the file's
    top-level ``try: from X import Y / except: raise error(...)`` guards.

    This never touches the real, already-imported ``extras.AFC_assist``
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
    import extras.AFC_assist as real_module
    fresh_name = "extras.AFC_assist_import_guard_probe"
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


class TestModuleImportGuard:
    """Covers the two module-level `try/except: raise error(...)` guards
    around AFC_assist.py's imports of AFC_utils.ERROR_STR and
    AFC_stats.AFCStats_var.

    AFC_assist.py imports `error` from `configfile` (unlike AFC_buffer.py,
    which imports it from `configparser` directly) -- under this test
    suite's Klipper stubs that's a distinct exception type, so we import it
    fresh here rather than assuming configparser.Error applies."""

    def test_afc_utils_import_failure_raises_configfile_error(self):
        from configfile import error as KlipperError
        with pytest.raises(KlipperError) as exc_info:
            _exec_afc_assist_with_blocked_dependency("extras.AFC_utils")
        assert str(exc_info.value).startswith(
            "Error when trying to import AFC_utils.ERROR_STR")

    def test_afc_stats_import_failure_raises_configfile_error(self):
        from configfile import error as KlipperError
        with pytest.raises(KlipperError) as exc_info:
            _exec_afc_assist_with_blocked_dependency("extras.AFC_stats")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_stats, please rerun install-afc.sh script")
