# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Optional ACE temperature sensor for Klipper.
# Reads the ACE device temperature from an AFC_ACE unit's cached
# hardware status and exposes it as a standard temperature_sensor.
#
# Adapted from Kobra-S1/ACEPRO temperature_ace.py for the AFC_ACE
# driver used in this project.
#
# Configuration example (place AFTER your [AFC_ACE ...] section):
#
#   [temperature_sensor ace_temp]
#   sensor_type: temperature_ace
#   ace_unit: Ace1           # Name of your AFC_ACE unit (default: Ace1)
#   min_temp: 0
#   max_temp: 70
#
# On an ACE 2 Pro you can add extra sensors for the individual thermal
# channels the unit reports (box1/box2 chamber, ptc1/ptc2 dryer heater, env),
# cached each heartbeat via GET_TEMP. Handy for watching the dryer heaters
# during a drying cycle:
#
#   [temperature_sensor ace_dryer_ptc1]
#   sensor_type: temperature_ace
#   ace_unit: Ace1
#   channel: ptc1            # default | box1 | box2 | ptc1 | ptc2 | env
#   min_temp: 0
#   max_temp: 120
#
# channel defaults to 'default' (the single box/env temp from get_status,
# unchanged from before). The other channels are ACE2-only.

import logging
from typing import Any, Callable, Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from extras.AFC import afc
    from extras.heaters import PrinterHeaters
    from mcu import MCU

ACE_REPORT_TIME = 1.0

_fallback_logger = logging.getLogger("temperature_ace")

class TemperatureACE:
    """Temperature sensor that reads ACE device temperature from an
    AFC_ACE unit's cached hardware status."""

    def __init__(self, config: "ConfigWrapper") -> None:
        """
        Initialize the ACE temperature sensor.

        :param config: Klipper config wrapper for this `[temperature_sensor]`
            section. Reads `ace_unit`, the AFC_ACE unit name whose cached
            hardware status supplies the temperature (default `Ace1`).
        """
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.ace_unit_name = config.get("ace_unit", "Ace1")
        # Thermal channel to report.
        self.channel = config.getchoice("channel",
            {c: c for c in ("default", "box1", "box2", "ptc1", "ptc2", "env")}, "default")
        self.simulate_aht3x = config.getboolean("simulate_supported_sensor_mainsail", True)

        self.temp = 0.0
        self.min_temp = 0.0
        self.max_temp = 70.0
        self.measured_min = float("inf")
        self.measured_max = 0.0
        self.humidity = 0.0
        self._has_humidity = False

        self._ace_unit: Any = None
        self._logger: Any = None
        self._sample_error_logged = False

        self._callback: Optional[Callable[[float, float], None]] = None

        # Register as "aht10 <name>" so Mainsail and Fluidd show humidity (ACE 2).
        # Fluidd needs sensor_type: aht2x, which maps onto aht10.
        if self.simulate_aht3x:
            self.printer.add_object("aht10 " + self.name, self)
        else:
            self.printer.add_object("temperature_ace " + self.name, self)

        # Skip timers in debug mode
        if self.printer.get_start_args().get("debugoutput") is not None:
            return

        self.sample_timer = self.reactor.register_timer(self._sample_ace_temperature)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

    def _log(self) -> Any:
        """
        Return AFC logger if available, otherwise fallback.

        :return Any: the AFC logger, or the module fallback logger
        """
        if self._logger is not None:
            return self._logger
        return _fallback_logger

    def handle_ready(self) -> None:
        """
        Resolve AFC_ACE unit reference once Klipper is ready.
        """
        self._ace_unit = self._resolve_unit()

        try:
            afc_obj: afc = self.printer.lookup_object("AFC")
            self._logger = afc_obj.logger
        except Exception:
            pass

        if self._ace_unit:
            if getattr(self._ace_unit, "type", "") == "ACE2":
                self._has_humidity = True
            self._log().info(f"temperature_ace: linked to AFC_ACE unit '{self.ace_unit_name}'")
        else:
            self._log().warning(
                f"temperature_ace: AFC_ACE unit '{self.ace_unit_name}' not found; reporting 0C")

        if hasattr(self, "sample_timer"):
            self.reactor.update_timer(self.sample_timer, self.reactor.NOW)

    def setup_minmax(self, min_temp: float, max_temp: float) -> None:
        """
        Set the allowed temperature range (Klipper sensor interface).

        :param min_temp: minimum allowed temperature in C; a reading below it
            (when temp > 0) triggers a printer shutdown.
        :param max_temp: maximum allowed temperature in C; a reading above it
            triggers a printer shutdown.
        """
        self.min_temp = min_temp
        self.max_temp = max_temp

    def setup_callback(self, cb: Callable[[float, float], None]) -> None:
        """
        Register the heaters callback that receives temperature samples.

        :param cb: callable invoked as `cb(measured_time, temp)` each sample.
        """
        self._callback = cb

    def get_report_time_delta(self) -> float:
        """
        Return the sampling interval (Klipper sensor interface).

        :return float: seconds between temperature reports (ACE_REPORT_TIME).
        """
        return ACE_REPORT_TIME

    def _resolve_unit(self) -> Any:
        """
        Look up the AFC_ACE unit object via AFC's units dict.

        :return Any: the AFC_ACE unit object for `ace_unit_name`, or None if AFC
            or the unit is not (yet) available.
        """
        try:
            afc_obj: afc = self.printer.lookup_object("AFC")
            units = getattr(afc_obj, "units", {})
            unit = units.get(self.ace_unit_name)
            if unit is not None:
                return unit
        except Exception:
            pass

        try:
            return self.printer.lookup_object(f"AFC_ACE {self.ace_unit_name}", None)
        except Exception:
            return None

    def _sample_ace_temperature(self, eventtime: float) -> float:
        """
        Timer callback: read ACE temperature and feed the heaters system.

        Reads `temp` from the AFC_ACE unit's cached hardware status, tracks
        measured min/max, enforces the min/max limits (shutdown on breach), and
        invokes the registered heaters callback with the sample.

        :param eventtime: reactor event time of this firing.
        :return float: the next reactor time to fire (eventtime + report time).
        """
        try:
            if not self._ace_unit:
                self._ace_unit = self._resolve_unit()

            if self._ace_unit:
                hw_status = getattr(self._ace_unit, "_cached_hw_status", {})
                if self.channel == "default":
                    self.temp = float(hw_status.get("temp", 0.0) or 0.0)
                    if "humidity" in hw_status:
                        self._has_humidity = True
                        self.humidity = float(hw_status.get("humidity", 0.0) or 0.0)
                else:
                    temp_info = getattr(self._ace_unit, "_cached_temp_info", {})
                    self.temp = float(temp_info.get(self.channel + "_temp", 0.0) or 0.0)
                    if "env_humidity" in temp_info:
                        self._has_humidity = True
                        self.humidity = float(temp_info.get("env_humidity", 0.0) or 0.0)

                if self.temp > 0:
                    self.measured_min = min(self.measured_min, self.temp)
                    self.measured_max = max(self.measured_max, self.temp)

                if (self.temp > 0
                    and self.temp < self.min_temp):
                    self.printer.invoke_shutdown(
                        f"ACE temperature {self.temp:.1f} below minimum of {self.min_temp:.1f}")
                if self.temp > self.max_temp:
                    self.printer.invoke_shutdown(
                        f"ACE temperature {self.temp:.1f} above maximum of {self.max_temp:.1f}")
            else:
                self.temp = 0.0
        except Exception as e:
            if not self._sample_error_logged:
                self._log().error(f"temperature_ace: error sampling ACE temperature: {e}")
                self._sample_error_logged = True
            self.temp = 0.0

        if self._callback:
            mcu_obj: MCU = self.printer.lookup_object("mcu")
            measured_time = self.reactor.monotonic()
            self._callback(mcu_obj.estimated_print_time(measured_time), self.temp)

        return eventtime + ACE_REPORT_TIME

    def get_temp(self, eventtime: float) -> Tuple[float, float]:
        """
        Return the current temperature (Klipper sensor interface).

        :param eventtime: reactor event time (unused).
        :return tuple: (current temperature in C, measured error 0.0).
        """
        return self.temp, 0.0

    def stats(self, eventtime: float) -> Tuple[bool, str]:
        """
        Return a stats line for the sensor (Klipper sensor interface).

        :param eventtime: reactor event time (unused).
        :return tuple: (False, status string carrying the current temperature).
        """
        return False, f"temperature_ace {self.name}: temp={self.temp:.1f}"

    def get_status(self, eventtime: float) -> Dict[str, float]:
        """
        Return sensor status for Moonraker / status queries.

        :param eventtime: reactor event time (unused).
        :return dict: `temperature`, plus `humidity` when the unit reports it
            (ACE 2 only).
        """
        status = {"temperature": round(self.temp, 2)}
        if self._has_humidity:
            status["humidity"] = round(self.humidity, 2)
        return status


def load_config(config: "ConfigWrapper") -> None:
    """
    Register the temperature_ace sensor factory with Klipper (config hook).

    :param config: Klipper config wrapper (used only to reach the printer).
    """
    _register_sensor_factory(config)


def _register_sensor_factory(config: "ConfigWrapper") -> None:
    """
    Idempotently register the temperature_ace sensor factory.

    :param config: the config being loaded; its printer's `heaters` object
        gets the `temperature_ace` sensor factory, and Klipper's
        `load_object` needs the config itself to load heaters if they are
        not loaded yet.
    """
    printer = config.get_printer()
    heaters: PrinterHeaters
    try:
        heaters = printer.lookup_object("heaters")
    except Exception:
        try:
            heaters = printer.load_object(config, "heaters")
        except Exception as e:
            _fallback_logger.warning(f"temperature_ace: failed to load heaters: {e}")
            return

    if getattr(heaters, "_temperature_ace_registered", False):
        return
    heaters.add_sensor_factory("temperature_ace", TemperatureACE)
    # Register the Fluidd alias early too: sensor_type aht2x is resolved in the main
    # config pass, before AFC_ACE loads, so registering only there fails at boot.
    heaters.add_sensor_factory("aht2x", TemperatureACE)
    heaters._temperature_ace_registered = True