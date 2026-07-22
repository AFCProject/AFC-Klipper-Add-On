"""
Unit tests for extras/temperature_oams.py

Covers:
  - TemperatureOAMS.__init__: config plumbing, resolution validation,
    simulate_aht3x branch, invalid resolution errors
  - handle_connect: device init + timer start
  - setup_minmax / setup_callback / get_report_time_delta
  - _init_device: register writes, heater branch
  - _read_register_16: byte packing
  - _set_resolution / _set_config_bit
  - _read_temp / _read_humidity: success and I2C failure branches
  - _sample: all (temp_ok, humi_ok) branch combinations, consecutive
    error backoff, and out-of-range shutdown
  - get_status: rounding
  - load_config: registers sensor factory
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch
import pytest

# temperature_oams.py does `from . import bus`, which resolves to
# `extras.bus`. That module is part of Klipper core and isn't vendored in
# this repo, so it must be stubbed before the module under test is imported
# (mirrors the extras.led / extras.force_move stubs in conftest.py).
_bus_stub = types.ModuleType("extras.bus")
_bus_stub.MCU_I2C_from_config = MagicMock()
sys.modules.setdefault("extras.bus", _bus_stub)
try:
    import extras  # noqa: E402
    if not hasattr(extras, "bus"):
        extras.bus = _bus_stub
except ImportError:
    pass

from extras.temperature_oams import (  # noqa: E402
    TemperatureOAMS,
    TEMP_REG,
    HUMI_REG,
    CONF_REG,
    MFID_REG,
    DVID_REG,
    HDC1080_I2C_ADDR,
    HEATER_ENABLE_BIT,
    TEMP_RES_14,
    TEMP_RES_11,
    HUMI_RES_14,
    HUMI_RES_11,
    HUMI_RES_8,
    load_config,
)
from tests.conftest import MockAFC, MockPrinter, MockReactor, MockConfig


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_sensor(report_time=5, init_sent=False, config_values=None):
    """Build a TemperatureOAMS instance via its real constructor.

    A couple of fields (``max_temp``, ``init_sent``, ``_callback``) are
    adjusted afterward purely to give tests a realistic operating range and
    a callback to assert against -- the same state ``handle_connect()`` /
    ``setup_minmax()`` / ``setup_callback()`` would normally establish.
    """
    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    reactor = MockReactor()
    printer._reactor = reactor
    printer.add_object = MagicMock()

    values = {"report_time": report_time}
    if config_values:
        values.update(config_values)
    config = MockConfig(
        name="temperature_sensor oams1", printer=printer, values=values)

    with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
        sensor = TemperatureOAMS(config)

    sensor.max_temp = 100.0
    sensor.init_sent = init_sent
    sensor._callback = MagicMock()

    return sensor


def _i2c_read_response(*byte_pairs):
    """Return a MagicMock side_effect cycling through given (hi, lo) byte pairs."""
    return [{"response": bytearray([hi, lo])} for hi, lo in byte_pairs]


# ── __init__ ──────────────────────────────────────────────────────────────────

class TestInit:
    def _config(self, values=None):
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        printer.add_object = MagicMock()
        config = MockConfig(
            name="temperature_sensor oams1", printer=printer, values=values or {}
        )
        return config, printer

    def test_sets_name_from_config(self):
        config, printer = self._config()
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.name == "oams1"

    def test_default_report_time(self):
        config, printer = self._config()
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.report_time == 5

    def test_custom_report_time(self):
        config, printer = self._config(values={"report_time": 10})
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.report_time == 10

    def test_initial_temp_humidity_zero(self):
        config, printer = self._config()
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.temp == 0.0
        assert sensor.humidity == 0.0

    def test_registers_sample_timer(self):
        config, printer = self._config()
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.sample_timer is not None

    def test_simulate_aht3x_default_true_registers_aht3x_object(self):
        config, printer = self._config()
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        printer.add_object.assert_called_once_with("aht3x oams1", sensor)

    def test_simulate_aht3x_false_registers_temperature_oams_object(self):
        config, printer = self._config(values={"simulate_supported_sensor_mainsail": False})
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        printer.add_object.assert_called_once_with("temperature_oams oams1", sensor)

    def test_registers_klippy_connect_handler(self):
        config, printer = self._config()
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.handle_connect in printer._event_handlers["klippy:connect"]

    def test_default_temp_resolution_is_14(self):
        config, printer = self._config()
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.temp_resolution == TEMP_RES_14

    def test_custom_temp_resolution_11(self):
        config, printer = self._config(values={"temp_resolution": 11})
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.temp_resolution == TEMP_RES_11

    def test_invalid_temp_resolution_raises(self):
        config, printer = self._config(values={"temp_resolution": 12})
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            with pytest.raises(ValueError):
                TemperatureOAMS(config)

    def test_default_humidity_resolution_is_14(self):
        config, printer = self._config()
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.humidity_resolution == HUMI_RES_14

    def test_custom_humidity_resolution_8(self):
        config, printer = self._config(values={"humidity_resolution": 8})
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.humidity_resolution == HUMI_RES_8

    def test_invalid_humidity_resolution_raises(self):
        config, printer = self._config(values={"humidity_resolution": 9})
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            with pytest.raises(ValueError):
                TemperatureOAMS(config)

    def test_temp_offset_and_humidity_offset(self):
        config, printer = self._config(
            values={"temp_offset": 1.5, "humidity_offset": -2.0}
        )
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.temp_offset == 1.5
        assert sensor.humidity_offset == -2.0

    def test_heater_enabled_default_false(self):
        config, printer = self._config()
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.heater_enabled is False

    def test_heater_enabled_true(self):
        config, printer = self._config(values={"heater_enabled": True})
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.heater_enabled is True

    def test_initial_state_flags(self):
        config, printer = self._config()
        with patch("extras.temperature_oams.bus.MCU_I2C_from_config", MagicMock()):
            sensor = TemperatureOAMS(config)
        assert sensor.is_calibrated is False
        assert sensor.init_sent is False
        assert sensor._consecutive_errors == 0
        assert sensor._max_consecutive_errors == 5
        assert sensor._last_good_temp == 0.0


# ── handle_connect ────────────────────────────────────────────────────────────

class TestHandleConnect:
    def test_calls_init_device(self):
        sensor = _make_sensor()
        sensor._init_device = MagicMock()
        sensor.handle_connect()
        sensor._init_device.assert_called_once()

    def test_starts_sample_timer_now(self):
        sensor = _make_sensor()
        sensor._init_device = MagicMock()
        sensor.reactor.update_timer = MagicMock()
        sensor.handle_connect()
        sensor.reactor.update_timer.assert_called_once_with(
            sensor.sample_timer, sensor.reactor.NOW
        )


# ── setup_minmax / setup_callback / get_report_time_delta ────────────────────

class TestSimpleAccessors:
    def test_setup_minmax_sets_bounds(self):
        sensor = _make_sensor()
        sensor.setup_minmax(10, 90)
        assert sensor.min_temp == 10
        assert sensor.max_temp == 90

    def test_setup_callback_sets_callback(self):
        sensor = _make_sensor()
        cb = MagicMock()
        sensor.setup_callback(cb)
        assert sensor._callback is cb

    def test_get_report_time_delta_returns_report_time(self):
        sensor = _make_sensor(report_time=7)
        assert sensor.get_report_time_delta() == 7


# ── _read_register_16 ─────────────────────────────────────────────────────────

class TestReadRegister16:
    def test_combines_high_and_low_byte(self):
        sensor = _make_sensor()
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x12, 0x34])}
        value = sensor._read_register_16(MFID_REG)
        assert value == 0x1234

    def test_writes_register_address_first(self):
        sensor = _make_sensor()
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x00, 0x00])}
        sensor._read_register_16(DVID_REG)
        sensor.i2c.i2c_write.assert_called_once_with([DVID_REG])


# ── _init_device ──────────────────────────────────────────────────────────────

class TestInitDevice:
    def test_sends_measurement_mode_config_first(self):
        sensor = _make_sensor()
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x00, 0x00])}
        sensor._init_device()
        first_call = sensor.i2c.i2c_write.call_args_list[0]
        assert first_call[0][0] == [CONF_REG, 1 << 4, 0x00]

    def test_marks_init_sent_true(self, caplog):
        import logging
        sensor = _make_sensor()
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x00, 0x00])}
        with caplog.at_level(logging.INFO):
            sensor._init_device()
        assert sensor.init_sent is True
        assert [r.getMessage() for r in caplog.records] == [
            "temperature_oams oams1: manufacturer=0x0 device=0x0"
        ]

    def test_sets_temp_resolution_bits(self):
        sensor = _make_sensor()
        sensor.temp_resolution = TEMP_RES_11
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x00, 0x00])}
        sensor._init_device()
        calls = [c[0][0] for c in sensor.i2c.i2c_write.call_args_list]
        assert [CONF_REG, TEMP_RES_11 >> 8, 0x00] in calls

    def test_sets_humidity_resolution_bits(self):
        sensor = _make_sensor()
        sensor.humidity_resolution = HUMI_RES_8
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x00, 0x00])}
        sensor._init_device()
        calls = [c[0][0] for c in sensor.i2c.i2c_write.call_args_list]
        assert [CONF_REG, HUMI_RES_8 >> 8, 0x00] in calls

    def test_heater_disabled_does_not_write_heater_bit(self):
        sensor = _make_sensor()
        sensor.heater_enabled = False
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x00, 0x00])}
        sensor._init_device()
        calls = [c[0][0] for c in sensor.i2c.i2c_write.call_args_list]
        assert [CONF_REG, HEATER_ENABLE_BIT >> 8, 0x00] not in calls

    def test_heater_enabled_writes_heater_bit(self):
        sensor = _make_sensor()
        sensor.heater_enabled = True
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x00, 0x00])}
        sensor._init_device()
        calls = [c[0][0] for c in sensor.i2c.i2c_write.call_args_list]
        assert [CONF_REG, HEATER_ENABLE_BIT >> 8, 0x00] in calls


# ── _set_resolution / _set_config_bit ────────────────────────────────────────

class TestSetResolution:
    def test_preserves_bits_outside_mask(self):
        sensor = _make_sensor()
        # existing config has an unrelated bit (0x0800) set
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x08, 0x00])}
        sensor._set_resolution(CONF_REG, 0x0400, TEMP_RES_11)
        written = sensor.i2c.i2c_write.call_args_list[-1][0][0]
        # 0x0800 | 0x0400 = 0x0C00 -> high byte 0x0C
        assert written == [CONF_REG, 0x0C, 0x00]

    def test_pauses_after_read_and_after_write(self):
        sensor = _make_sensor()
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x00, 0x00])}
        sensor.reactor.pause = MagicMock()
        sensor._set_resolution(CONF_REG, 0x0400, TEMP_RES_11)
        # One pause from the internal _read_register_16 call, one after the write
        assert sensor.reactor.pause.call_count == 2


class TestSetConfigBit:
    def test_enable_true_sets_bit(self):
        sensor = _make_sensor()
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x00, 0x00])}
        sensor.reactor.pause = MagicMock()
        sensor._set_config_bit(HEATER_ENABLE_BIT, True)
        written = sensor.i2c.i2c_write.call_args_list[-1][0][0]
        assert written == [CONF_REG, HEATER_ENABLE_BIT >> 8, 0x00]
        # One pause from the internal register read, one after the write.
        assert sensor.reactor.pause.call_count == 2

    def test_enable_false_clears_bit(self):
        sensor = _make_sensor()
        sensor.i2c.i2c_read.return_value = {
            "response": bytearray([HEATER_ENABLE_BIT >> 8, 0x00])
        }
        sensor._set_config_bit(HEATER_ENABLE_BIT, False)
        written = sensor.i2c.i2c_write.call_args_list[-1][0][0]
        assert written == [CONF_REG, 0x00, 0x00]


# ── _read_temp / _read_humidity ──────────────────────────────────────────────

class TestReadTemp:
    def test_success_converts_raw_to_celsius(self):
        sensor = _make_sensor()
        # raw = 0x8000 (32768) -> (32768/65536)*165 - 40 = 42.5
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x80, 0x00])}
        sensor.reactor.pause = MagicMock()
        celsius, ok = sensor._read_temp()
        assert ok is True
        assert celsius == pytest.approx(42.5)
        sensor.reactor.pause.assert_called_once()

    def test_i2c_exception_returns_zero_and_false(self, caplog):
        import logging
        sensor = _make_sensor()
        sensor.i2c.i2c_write.side_effect = Exception("i2c bus error")
        with caplog.at_level(logging.DEBUG):
            celsius, ok = sensor._read_temp()
        assert ok is False
        assert celsius == 0.0
        assert [r.getMessage() for r in caplog.records] == [
            "temperature_oams oams1: temp read failed: i2c bus error"
        ]

    def test_writes_temp_register_first(self):
        sensor = _make_sensor()
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x00, 0x00])}
        sensor._read_temp()
        assert sensor.i2c.i2c_write.call_args_list[0][0][0] == [TEMP_REG]


class TestReadHumidity:
    def test_success_converts_raw_to_percent(self):
        sensor = _make_sensor()
        # raw = 0x8000 -> (32768/65536)*100 = 50.0
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x80, 0x00])}
        sensor.reactor.pause = MagicMock()
        percent, ok = sensor._read_humidity()
        assert ok is True
        assert percent == pytest.approx(50.0)
        sensor.reactor.pause.assert_called_once()

    def test_i2c_exception_returns_zero_and_false(self, caplog):
        import logging
        sensor = _make_sensor()
        sensor.i2c.i2c_write.side_effect = Exception("i2c bus error")
        with caplog.at_level(logging.DEBUG):
            percent, ok = sensor._read_humidity()
        assert ok is False
        assert percent == 0.0
        assert [r.getMessage() for r in caplog.records] == [
            "temperature_oams oams1: humidity read failed: i2c bus error"
        ]

    def test_writes_humidity_register_first(self):
        sensor = _make_sensor()
        sensor.i2c.i2c_read.return_value = {"response": bytearray([0x00, 0x00])}
        sensor._read_humidity()
        assert sensor.i2c.i2c_write.call_args_list[0][0][0] == [HUMI_REG]


# ── _sample ───────────────────────────────────────────────────────────────────

class TestSample:
    def test_not_init_sent_reschedules_without_reading(self):
        sensor = _make_sensor(init_sent=False, report_time=5)
        sensor._read_temp = MagicMock()
        result = sensor._sample(100.0)
        assert result == 105.0
        sensor._read_temp.assert_not_called()

    def test_both_ok_calls_callback_and_checks_range(self):
        sensor = _make_sensor(init_sent=True)
        sensor._consecutive_errors = 3  # seed nonzero so the reset-to-0 below is proven
        sensor._read_temp = MagicMock(return_value=(50.0, True))
        sensor._read_humidity = MagicMock(return_value=(40.0, True))
        sensor.min_temp = 0.0
        sensor.max_temp = 100.0
        sensor.i2c.get_mcu.return_value.estimated_print_time.return_value = 123.0
        sensor.printer.invoke_shutdown = MagicMock()
        sensor.reactor.pause = MagicMock()

        result = sensor._sample(100.0)

        assert sensor.temp == 50.0
        assert sensor.humidity == 40.0
        assert sensor._last_good_temp == 50.0
        assert sensor._consecutive_errors == 0
        sensor.printer.invoke_shutdown.assert_not_called()
        sensor._callback.assert_called_once_with(123.0, 50.0)
        assert result == sensor.reactor.monotonic() + sensor.report_time
        sensor.reactor.pause.assert_called_once()

    def test_temp_ok_out_of_range_invokes_shutdown(self):
        sensor = _make_sensor(init_sent=True)
        sensor._read_temp = MagicMock(return_value=(150.0, True))
        sensor._read_humidity = MagicMock(return_value=(40.0, True))
        sensor.min_temp = 0.0
        sensor.max_temp = 100.0
        sensor.printer.invoke_shutdown = MagicMock()

        sensor._sample(100.0)

        sensor.printer.invoke_shutdown.assert_called_once()
    
    def test_temp_below_min_invokes_shutdown(self):
        sensor = _make_sensor(init_sent=True)
        sensor._read_temp = MagicMock(return_value=(-10.0, True))
        sensor._read_humidity = MagicMock(return_value=(40.0, True))
        sensor.min_temp = 0.0
        sensor.max_temp = 100.0
        sensor.printer.invoke_shutdown = MagicMock()

        sensor._sample(100.0)

        sensor.printer.invoke_shutdown.assert_called_once()

    def test_temp_fail_humi_ok_uses_last_good_temp_no_shutdown_check(self):
        sensor = _make_sensor(init_sent=True)
        sensor._last_good_temp = 500.0  # deliberately out of range value
        sensor.min_temp = 0.0
        sensor.max_temp = 100.0
        sensor._read_temp = MagicMock(return_value=(0.0, False))
        sensor._read_humidity = MagicMock(return_value=(40.0, True))
        sensor.printer.invoke_shutdown = MagicMock()

        sensor._sample(100.0)

        assert sensor.temp == 500.0
        assert sensor._consecutive_errors == 1
        # consecutive_errors != 0 so the range check is skipped even though
        # temp (500.0) is out of [min_temp, max_temp]
        sensor.printer.invoke_shutdown.assert_not_called()
        sensor._callback.assert_called_once()

    def test_temp_ok_humi_fail_still_calls_callback(self):
        sensor = _make_sensor(init_sent=True)
        sensor.humidity = 10.0
        sensor._read_temp = MagicMock(return_value=(30.0, True))
        sensor._read_humidity = MagicMock(return_value=(0.0, False))

        sensor._sample(100.0)

        assert sensor.temp == 30.0
        assert sensor.humidity == 10.0  # unchanged, humi read failed
        sensor._callback.assert_called_once()

    def test_both_fail_below_threshold_reschedules_normally_no_callback(self):
        sensor = _make_sensor(init_sent=True, report_time=5)
        sensor._consecutive_errors = 0
        sensor._read_temp = MagicMock(return_value=(0.0, False))
        sensor._read_humidity = MagicMock(return_value=(0.0, False))

        result = sensor._sample(100.0)

        assert sensor._consecutive_errors == 1
        assert result == 105.0
        sensor._callback.assert_not_called()

    def test_both_fail_at_threshold_backs_off(self, caplog):
        import logging
        sensor = _make_sensor(init_sent=True, report_time=5)
        sensor._max_consecutive_errors = 5
        sensor._consecutive_errors = 4
        sensor._read_temp = MagicMock(return_value=(0.0, False))
        sensor._read_humidity = MagicMock(return_value=(0.0, False))

        with caplog.at_level(logging.WARNING):
            result = sensor._sample(100.0)

        # 4 -> +1 (temp fail, single increment) = 5 >= 5
        assert sensor._consecutive_errors == 5
        assert result == 100.0 + 5 * 3
        sensor._callback.assert_not_called()
        assert [r.getMessage() for r in caplog.records] == [
            "temperature_oams oams1: 5 consecutive I2C errors, backing off"
        ]

    def test_no_callback_registered_skips_callback_without_raising(self):
        """setup_callback() may not have been called yet (e.g. before the
        heaters system wires it up) -- _callback stays None, and _sample
        must not try to call it."""
        sensor = _make_sensor(init_sent=True)
        sensor._callback = None
        sensor._read_temp = MagicMock(return_value=(50.0, True))
        sensor._read_humidity = MagicMock(return_value=(40.0, True))
        sensor.min_temp = 0.0
        sensor.max_temp = 100.0
        sensor.i2c.get_mcu.return_value.estimated_print_time.return_value = 123.0

        result = sensor._sample(100.0)  # should not raise despite _callback is None

        assert sensor.temp == 50.0
        assert result == sensor.reactor.monotonic() + sensor.report_time

    def test_both_fail_increments_consecutive_errors_by_one_not_two(self):
        sensor = _make_sensor(init_sent=True)
        sensor._consecutive_errors = 0
        sensor._read_temp = MagicMock(return_value=(0.0, False))
        sensor._read_humidity = MagicMock(return_value=(0.0, False))

        sensor._sample(100.0)

        assert sensor._consecutive_errors == 1

    def test_applies_temp_and_humidity_offsets(self):
        sensor = _make_sensor(init_sent=True)
        sensor.temp_offset = 2.0
        sensor.humidity_offset = -1.0
        sensor._read_temp = MagicMock(return_value=(20.0, True))
        sensor._read_humidity = MagicMock(return_value=(50.0, True))

        sensor._sample(100.0)

        assert sensor.temp == 22.0
        assert sensor.humidity == 49.0


# ── get_status ────────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_rounds_temperature_and_humidity(self):
        sensor = _make_sensor()
        sensor.temp = 21.23456
        sensor.humidity = 55.6789
        status = sensor.get_status(0.0)
        assert status == {"temperature": 21.23, "humidity": 55.68}


# ── load_config ───────────────────────────────────────────────────────────────

class TestLoadConfig:
    def test_registers_sensor_factory(self):
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        heaters = MagicMock()
        printer._objects["heaters"] = heaters
        config = MockConfig(name="temperature_sensor oams1", printer=printer)

        load_config(config)

        heaters.add_sensor_factory.assert_called_once_with(
            "temperature_oams", TemperatureOAMS
        )
