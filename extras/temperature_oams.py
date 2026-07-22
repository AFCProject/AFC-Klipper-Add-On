# OpenAMS temperature and humidity sensor (HDC1080 I2C driver)
#
# Copyright (C) 2024 JR Lomas (discord:knight_rad.iant) <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Configuration example:
#
#   [temperature_sensor oams1]
#   sensor_type: temperature_oams
#   i2c_mcu: oams_mcu1
#   i2c_bus: i2c0
#   i2c_speed: 200000
#   min_temp: 0
#   max_temp: 100

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Tuple, TYPE_CHECKING
from . import bus

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from klippy import Printer

# HDC1080 register addresses
TEMP_REG = 0x00
HUMI_REG = 0x01
CONF_REG = 0x02
FSER_REG = 0xFB
MSER_REG = 0xFC
LSER_REG = 0xFD
MFID_REG = 0xFE
DVID_REG = 0xFF

HDC1080_I2C_ADDR = 0x40

CONFIG_RESET_BIT = 0x8000
CONFIG_BATTERY_STATUS_BIT = 0x0800
HEATER_ENABLE_BIT = 0x2000

TEMP_RES_14 = 0x0000
TEMP_RES_11 = 0x0400
TEMP_RES = {14: TEMP_RES_14, 11: TEMP_RES_11}

HUMI_RES_14 = 0x0000
HUMI_RES_11 = 0x0100
HUMI_RES_8  = 0x0200
HUMI_RES = {14: HUMI_RES_14, 11: HUMI_RES_11, 8: HUMI_RES_8}


class TemperatureOAMS:
    """HDC1080-based temperature and humidity sensor for OpenAMS units."""

    def __init__(self, config: ConfigWrapper) -> None:
        """
        Initialize the HDC1080 temperature/humidity sensor driver.

        :param config: ConfigWrapper providing the sensor name, I2C bus settings,
                       reporting interval, resolution, offsets, and heater option.
        """
        self.printer: Printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.reactor = self.printer.get_reactor()
        self.i2c = bus.MCU_I2C_from_config(
            config, default_addr=HDC1080_I2C_ADDR, default_speed=100000)
        self.report_time = config.getint('report_time', 5, minval=5)
        self.temp = self.min_temp = self.max_temp = self.humidity = 0.
        self.sample_timer = self.reactor.register_timer(self._sample)
        self.simulate_aht3x = config.getboolean(
            'simulate_supported_sensor_mainsail', True)
        if self.simulate_aht3x:
            self.printer.add_object("aht3x " + self.name, self)
        else:
            self.printer.add_object("temperature_oams " + self.name, self)
        self.printer.register_event_handler("klippy:connect",
                                            self.handle_connect)
        self.temp_resolution = config.getint('temp_resolution', 14, minval=11, maxval=14)
        if self.temp_resolution not in TEMP_RES:
            valid = ", ".join(str(x) for x in TEMP_RES)
            error_msg = f"Invalid temperature resolution, valid: {valid}"
            raise ValueError(error_msg)
        self.temp_resolution = TEMP_RES[self.temp_resolution]

        self.humidity_resolution = config.getint('humidity_resolution', 14, minval=8, maxval=14)
        if self.humidity_resolution not in HUMI_RES:
            valid = ", ".join(str(x) for x in HUMI_RES)
            error_msg = f"Invalid humidity resolution, valid: {valid}"
            raise ValueError(error_msg)
        self.humidity_resolution = HUMI_RES[self.humidity_resolution]

        self.temp_offset = config.getfloat('temp_offset', 0.0)
        self.humidity_offset = config.getfloat('humidity_offset', 0.0)
        self.heater_enabled = config.getboolean('heater_enabled', False)

        self.is_calibrated = False
        self.init_sent = False
        self._consecutive_errors = 0
        self._max_consecutive_errors = 5
        self._last_good_temp = 0.0
        self._callback: Optional[Callable[[float, float], None]] = None

    def handle_connect(self) -> None:
        """
        Initialize the device and start the sampling timer on klippy:connect.
        """
        self._init_device()
        self.reactor.update_timer(self.sample_timer, self.reactor.NOW)

    def setup_minmax(self, min_temp: float, max_temp: float) -> None:
        """
        Store the allowed temperature range used for shutdown protection.

        :param min_temp: Minimum allowed temperature in degrees Celsius.
        :param max_temp: Maximum allowed temperature in degrees Celsius.
        """
        self.min_temp = min_temp
        self.max_temp = max_temp

    def setup_callback(self, cb: Callable[[float, float], None]) -> None:
        """
        Register the heaters callback used to report measured temperatures.

        :param cb: Callable invoked as ``cb(print_time, temperature)``.
        """
        self._callback = cb

    def get_report_time_delta(self) -> float:
        """
        Return the sensor reporting interval.

        :return float: Reporting interval in seconds.
        """
        return self.report_time

    def _init_device(self) -> None:
        """
        Configure the HDC1080: set resolutions, optional heater, and read IDs.

        First configures HFC1080 configuration register mode of acquisition into "Temperature
        and Humidity are acquired in sequence. Temperature first."

        Then Reads the manufacturer/device IDs, and device ID, applies the configured temperature
        and humidity resolutions, enables the heater when requested, and marks the
        device as initialized so sampling can begin.
        """
        data = [CONF_REG, 1 << 4, 0x00]
        self.i2c.i2c_write(data)
        manufacturer_id = self._read_register_16(MFID_REG)
        device_id = self._read_register_16(DVID_REG)

        self._set_resolution(CONF_REG, 0x0400, self.temp_resolution)
        self._set_resolution(CONF_REG, 0x0300, self.humidity_resolution)

        if self.heater_enabled:
            self._set_config_bit(HEATER_ENABLE_BIT, True)

        logging.info("temperature_oams %s: manufacturer=%s device=%s",
                     self.name, hex(manufacturer_id), hex(device_id))
        self.init_sent = True

    def _read_register_16(self, reg: int) -> int:
        """
        Read a 16-bit big-endian value from an HDC1080 register.

        :param reg: Register address to read from.
        :return int: The 16-bit register value.
        """
        self.i2c.i2c_write([reg])
        self.reactor.pause(self.reactor.monotonic() + 0.0635)
        read = self.i2c.i2c_read([], 2)
        data = bytearray(read['response'])
        return (data[0] << 8) | data[1]

    def _set_resolution(self, reg: int, mask: int, value: int) -> None:
        """
        Update the resolution bits of a configuration register.

        :param reg: Configuration register address.
        :param mask: Bit mask selecting the resolution field to clear.
        :param value: Resolution bits to write into the masked field.
        """
        config = self._read_register_16(reg)
        config = (config & ~mask) | value
        data = [reg, config >> 8, 0x00]
        self.i2c.i2c_write(data)
        self.reactor.pause(self.reactor.monotonic() + 0.015)

    def _set_config_bit(self, bit: int, enable: bool) -> None:
        """
        Set or clear a single bit in the HDC1080 configuration register.

        :param bit: Bit mask of the configuration flag to modify.
        :param enable: When True the bit is set, otherwise it is cleared.
        """
        config = self._read_register_16(CONF_REG)
        if enable:
            config |= bit
        else:
            config &= ~bit
        data = [CONF_REG, config >> 8, 0x00]
        self.i2c.i2c_write(data)
        self.reactor.pause(self.reactor.monotonic() + 0.015)

    def _read_temp(self) -> Tuple[float, bool]:
        """
        Read and convert the current temperature from the HDC1080.

        :return tuple: (temperature in degrees Celsius, success flag); (0.0, False) on I2C failure.
        """
        try:
            self.i2c.i2c_write([TEMP_REG])
            self.reactor.pause(self.reactor.monotonic() + 0.0635)
            read = self.i2c.i2c_read([], 2)
            data = bytearray(read['response'])
            raw = (data[0] << 8) | data[1]
            celsius = (raw / 65536.0) * 165.0 - 40
            return celsius, True
        except Exception as e:
            logging.debug("temperature_oams %s: temp read failed: %s", self.name, e)
            return 0.0, False

    def _read_humidity(self) -> Tuple[float, bool]:
        """
        Read and convert the current relative humidity from the HDC1080.

        :return tuple: (relative humidity in percent, success flag); (0.0, False) on I2C failure.
        """
        try:
            self.i2c.i2c_write([HUMI_REG])
            self.reactor.pause(self.reactor.monotonic() + 0.0635)
            read = self.i2c.i2c_read([], 2)
            data = bytearray(read['response'])
            raw = (data[0] << 8) | data[1]
            percent = (raw / 65536.0) * 100.0
            return percent, True
        except Exception as e:
            logging.debug("temperature_oams %s: humidity read failed: %s", self.name, e)
            return 0.0, False

    def _sample(self, eventtime: float) -> float:
        """
        Reactor timer callback that samples temperature and humidity.

        Reads both values, applies offsets, tracks consecutive I2C errors and
        backs off the report interval when too many occur, triggers a printer
        shutdown if a valid temperature is outside the configured range, and
        forwards good temperatures to the registered heaters callback.

        :param eventtime: Reactor event time at which the timer fired.
        :return float: The reactor time at which the timer should next fire.
        """
        if not self.init_sent:
            return eventtime + self.report_time

        temp_val, temp_ok = self._read_temp()
        self.reactor.pause(self.reactor.monotonic() + 0.015)
        humi_val, humi_ok = self._read_humidity()

        if temp_ok:
            self.temp = temp_val + self.temp_offset
            self._last_good_temp = self.temp
            self._consecutive_errors = 0
        else:
            self._consecutive_errors += 1
            self.temp = self._last_good_temp

        if humi_ok:
            self.humidity = humi_val + self.humidity_offset

        if not (temp_ok or humi_ok):
            if self._consecutive_errors >= self._max_consecutive_errors:
                logging.warning(
                    "temperature_oams %s: %d consecutive I2C errors, backing off",
                    self.name, self._consecutive_errors)
                return eventtime + self.report_time * 3
            return eventtime + self.report_time

        if self._consecutive_errors == 0:
            if self.temp < self.min_temp or self.temp > self.max_temp:
                shutdown_msg = (f"temperature_oams {self.name}: {self.temp:.1f} outside range "
                                f"{self.min_temp:.1f}{self.max_temp:.1f}")
                self.printer.invoke_shutdown(shutdown_msg)

        measured_time = self.reactor.monotonic()
        print_time = self.i2c.get_mcu().estimated_print_time(measured_time)
        if self._callback is not None:
            self._callback(print_time, self.temp)
        return measured_time + self.report_time

    def get_status(self, eventtime: float) -> Dict[str, float]:
        """
        Return the latest measured temperature and humidity.

        :param eventtime: Reactor event time (unused).
        :return dict: Dict with rounded ``temperature`` and ``humidity`` values.
        """
        return {
            'temperature': round(self.temp, 2),
            'humidity': round(self.humidity, 2),
        }


def load_config(config: ConfigWrapper) -> None:
    """
    Register ``temperature_oams`` as a heaters sensor factory.

    :param config: ConfigWrapper used to look up the heaters object.
    """
    pheater = config.get_printer().lookup_object("heaters")
    pheater.add_sensor_factory("temperature_oams", TemperatureOAMS)
