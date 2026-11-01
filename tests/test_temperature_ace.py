"""Unit tests for extras/temperature_ace.py."""

from __future__ import annotations

import configparser
import logging
from typing import Any, Dict, Optional, Tuple

import pytest

from extras.AFC_ACE import afcACE
from extras.temperature_afc import TemperatureAFC
from extras.temperature_ace import (
    ACE_STALE_AFTER,
    _register_sensor_factory,
    load_config,
    TemperatureACE,
)
from tests.ace_helpers import (
    ace_isolation,
    AceConfig,
    AcePrinter,
    capture_log,
    Hook,
    LogCapture,
    make_ace2_unit,
    make_ace_printer,
    make_ace_unit,
    make_temperature_ace,
    Recorder,
)


pytestmark = pytest.mark.usefixtures(ace_isolation.__name__)


class TestTemperatureACEInit:
    @staticmethod
    def build(values: Optional[Dict[str, Any]] = None, *, debug_output: bool = False
              ) -> Tuple[TemperatureACE, AcePrinter, Dict[str, Any], LogCapture]:
        """
        Build [temperature_sensor ace_temp] straight through __init__, with no
        setup_minmax or handle_ready after it, capturing the fallback logger meanwhile.

        :param values: section options
        :param debug_output: start the printer in Klipper's debug-output mode
        :return tuple: the sensor, its printer, the printer objects it added and the
            "temperature_ace" logger's capture
        """
        printer = make_ace_printer()
        if debug_output:
            printer.start_args["debugoutput"] = "/dev/null"
        before = set(printer.objects)
        with capture_log("temperature_ace") as fallback:
            sensor = TemperatureACE(AceConfig("temperature_sensor ace_temp", printer, values))
        added = {name: obj for name, obj in printer.objects.items() if name not in before}
        return sensor, printer, added, fallback

    def test_name_and_defaults(self):
        sensor, printer, _, fallback = self.build()
        assert sensor.printer is printer
        assert sensor.reactor is printer.reactor
        assert sensor.name == "ace_temp"
        assert sensor.ace_unit_name == "Ace1"
        assert sensor.channel == "default"
        assert sensor.simulate_aht3x is True
        assert sensor.temp == 0.0
        assert sensor.min_temp == 0.0
        assert sensor.max_temp == 70.0
        assert sensor.measured_min == float("inf")
        assert sensor.measured_max == 0.0
        assert sensor.humidity == 0.0
        assert sensor._has_humidity is False
        assert sensor._ace_unit is None
        assert sensor._logger is None
        assert sensor._sample_error_logged is False
        assert sensor._stale_logged is False
        assert sensor._callback is None
        assert printer.logger.messages == []
        assert fallback.messages == []

    @pytest.mark.parametrize("channel", ["box1", "box2", "ptc1", "ptc2", "env"])
    def test_custom_ace_unit_and_channel(self, channel):
        sensor, printer, _, fallback = self.build({"ace_unit": "MyAce", "channel": channel})
        assert sensor.ace_unit_name == "MyAce"
        assert sensor.channel == channel
        assert printer.logger.messages == []
        assert fallback.messages == []

    def test_unknown_channel_is_a_config_error(self):
        with pytest.raises(configparser.Error):
            self.build({"channel": "ptc3"})

    def test_reads_afc_type_option(self):
        """afc_type must actually be read (not just accepted as a config
        value), so Klipper's own "unused option" check doesn't flag it when a
        section sets it for temperature_afc's benefit (sensor_type: aht2x)
        but sensor_type is temperature_ace directly."""
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor ace_temp", printer, {"afc_type": "ACE"})
        get_hook = config.get = Hook(config.get)

        TemperatureACE(config)

        assert get_hook.calls == [
            (("ace_unit", "Ace1"), {}),
            (("afc_type", None), {}),
            (("channel", "default"), {}),
        ]

    def test_simulate_aht3x_true_registers_aht10_object(self):
        sensor, printer, added, fallback = self.build()
        assert sensor.simulate_aht3x is True
        assert added == {"aht10 ace_temp": sensor}
        assert printer.logger.messages == []
        assert fallback.messages == []

    def test_simulate_aht3x_false_registers_temperature_ace_object(self):
        sensor, printer, added, fallback = self.build(
            {"simulate_supported_sensor_mainsail": False})
        assert sensor.simulate_aht3x is False
        assert added == {"temperature_ace ace_temp": sensor}
        assert printer.logger.messages == []
        assert fallback.messages == []

    def test_non_debug_registers_timer_and_ready_handler(self):
        sensor, printer, _, fallback = self.build()
        reactor = printer.reactor
        assert reactor.register_timer.calls == [((sensor._sample_ace_temperature,), {})]
        assert reactor.timers == [sensor.sample_timer]
        assert printer.event_handlers == {"klippy:ready": [sensor.handle_ready]}
        assert printer.logger.messages == []
        assert fallback.messages == []

    def test_debug_mode_skips_timer_and_handler(self):
        sensor, printer, added, fallback = self.build(debug_output=True)
        assert not hasattr(sensor, "sample_timer")
        assert printer.reactor.register_timer.calls == []
        assert printer.reactor.timers == []
        assert printer.event_handlers == {}
        # The sensor object is still registered before the debug-mode return.
        assert added == {"aht10 ace_temp": sensor}
        assert printer.logger.messages == []
        assert fallback.messages == []


class TestTemperatureACELog:
    def test_returns_afc_logger_when_set(self):
        sensor = make_temperature_ace()
        assert sensor._logger is sensor.printer.logger
        with capture_log("temperature_ace") as fallback:
            assert sensor._log() is sensor.printer.logger
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []

    def test_returns_fallback_logger_when_unset(self):
        sensor = make_temperature_ace(ready=False)
        assert sensor._logger is None
        with capture_log("temperature_ace") as fallback:
            assert sensor._log() is logging.getLogger("temperature_ace")
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []


class TestTemperatureACEHandleReady:
    LINKED = "temperature_ace: linked to AFC_ACE unit 'Ace1'"

    @staticmethod
    def unready(unit: Optional[afcACE] = None, **options: Any) -> TemperatureACE:
        """
        Build a sensor whose klippy:ready handler has not run yet.

        :param unit: unit "Ace1" it reads, None for a printer with no unit
        :param options: other make_temperature_ace keywords
        :return TemperatureACE: the sensor
        """
        return make_temperature_ace(unit=unit, ready=False, **options)

    def test_ace2_unit_sets_humidity_and_logs_link(self):
        unit = make_ace2_unit("Ace1")
        sensor = self.unready(unit)
        printer = sensor.printer
        with capture_log("temperature_ace") as fallback:
            sensor.handle_ready()
        assert sensor._ace_unit is unit
        assert sensor._logger is printer.logger
        assert sensor._has_humidity is True
        assert printer.logger.messages == [("info", self.LINKED)]
        assert fallback.messages == []
        # Armed at NOW (0.0), so the first sample runs on the next reactor pass.
        assert printer.reactor.update_timer.calls == [((sensor.sample_timer, 0.0), {})]

    def test_non_ace2_unit_keeps_humidity_false_and_logs_link(self):
        unit = make_ace_unit("Ace1")
        sensor = self.unready(unit)
        printer = sensor.printer
        with capture_log("temperature_ace") as fallback:
            sensor.handle_ready()
        assert sensor._ace_unit is unit
        assert sensor._logger is printer.logger
        assert sensor._has_humidity is False
        assert printer.logger.messages == [("info", self.LINKED)]
        assert fallback.messages == []
        assert printer.reactor.update_timer.calls == [((sensor.sample_timer, 0.0), {})]

    def test_missing_unit_logs_warning(self):
        sensor = self.unready()
        printer = sensor.printer
        with capture_log("temperature_ace") as fallback:
            sensor.handle_ready()
        assert sensor._ace_unit is None
        assert sensor._logger is printer.logger
        assert sensor._has_humidity is False
        assert printer.logger.messages == [
            ("warning", "temperature_ace: AFC_ACE unit 'Ace1' not found; reporting 0C")]
        assert fallback.messages == []
        assert printer.reactor.update_timer.calls == [((sensor.sample_timer, 0.0), {})]

    def test_afc_logger_lookup_failure_uses_fallback(self):
        # With no AFC object the unit is found through its [AFC_ACE Ace1] section.
        unit = make_ace_unit("Ace1", connect=False)
        sensor = self.unready(unit)
        printer = sensor.printer
        del printer.objects["AFC"]
        with capture_log("temperature_ace") as fallback:
            sensor.handle_ready()
        assert sensor._ace_unit is unit
        assert sensor._logger is None
        assert sensor._has_humidity is False
        assert fallback.messages == [("info", self.LINKED)]
        assert printer.logger.messages == []
        assert printer.reactor.update_timer.calls == [((sensor.sample_timer, 0.0), {})]

    def test_no_sample_timer_skips_update_timer(self):
        unit = make_ace_unit("Ace1")
        sensor = self.unready(unit, debug_output=True)
        printer = sensor.printer
        with capture_log("temperature_ace") as fallback:
            sensor.handle_ready()
        assert sensor._ace_unit is unit
        assert sensor._logger is printer.logger
        assert sensor._has_humidity is False
        assert printer.reactor.update_timer.calls == []
        assert printer.logger.messages == [("info", self.LINKED)]
        assert fallback.messages == []


class TestTemperatureACESetupMinmax:
    def test_sets_bounds(self):
        sensor = make_temperature_ace(min_temp=0.0, max_temp=70.0)
        with capture_log("temperature_ace") as fallback:
            sensor.setup_minmax(5.0, 65.0)
        assert sensor.min_temp == 5.0
        assert sensor.max_temp == 65.0
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []


class TestTemperatureACESetupCallback:
    def test_sets_callback(self):
        sensor = make_temperature_ace(callback=None)
        assert sensor._callback is None
        feed = Recorder()
        with capture_log("temperature_ace") as fallback:
            sensor.setup_callback(feed)
        assert sensor._callback is feed
        assert feed.calls == []
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []


class TestTemperatureACEGetReportTimeDelta:
    def test_returns_report_time(self):
        sensor = make_temperature_ace()
        with capture_log("temperature_ace") as fallback:
            assert sensor.get_report_time_delta() == 1.0
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []


class TestTemperatureACEResolveUnit:
    def test_returns_unit_from_afc_units(self):
        # An ACE 2 is registered as [AFC_ACE2 Ace_2], a section the direct-lookup
        # fallback does not try, so only afc.units can return it.
        unit = make_ace2_unit("Ace_2")
        sensor = make_temperature_ace(unit=unit, ready=False)
        assert sensor.ace_unit_name == "Ace_2"
        assert sensor.printer.lookup_object("AFC_ACE Ace_2", None) is None
        with capture_log("temperature_ace") as fallback:
            assert sensor._resolve_unit() is unit
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []

    def test_falls_back_to_direct_printer_lookup(self):
        unit = make_ace_unit("Ace_2", connect=False)
        sensor = make_temperature_ace(unit=unit, ready=False)
        assert sensor.ace_unit_name == "Ace_2"
        assert sensor.printer.afc.units == {}
        with capture_log("temperature_ace") as fallback:
            assert sensor._resolve_unit() is unit
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []

    def test_returns_none_when_unit_absent_everywhere(self):
        other = make_ace_unit("Ace_2")
        sensor = make_temperature_ace(printer=other.printer, values={"ace_unit": "Ace1"},
                                      ready=False)
        with capture_log("temperature_ace") as fallback:
            assert sensor._resolve_unit() is None
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []

    def test_returns_none_when_lookup_raises(self):
        sensor = make_temperature_ace(ready=False)
        printer = sensor.printer
        printer.lookup_object = Recorder(raises=RuntimeError("printer is shutting down"))
        with capture_log("temperature_ace") as fallback:
            assert sensor._resolve_unit() is None
        assert printer.lookup_object.calls == [(("AFC",), {}), (("AFC_ACE Ace1", None), {})]
        assert printer.logger.messages == []
        assert fallback.messages == []


class TestTemperatureACESampleAceTemperature:
    class LaggingMcu:
        """mcu stand-in whose print time runs a fixed time behind the reactor clock."""

        def __init__(self, behind: float) -> None:
            """
            :param behind: seconds the print clock lags the reactor clock
            """
            self.behind = behind

        def estimated_print_time(self, eventtime: float) -> float:
            """
            :param eventtime: reactor time
            :return float: the print time
            """
            return eventtime - self.behind

    @staticmethod
    def sensor_for(unit: Optional[afcACE], channel: str = "default", *, callback: Any = None,
                   min_temp: float = 0.0, max_temp: float = 1000.0) -> TemperatureACE:
        """
        Build a sensor that went ready linked to unit, with no heaters callback unless
        given. handle_ready marks an ACE 2 as humid up front; the flag starts clear here
        so each test shows what the sample itself decides.

        :param unit: unit "Ace1" it reads, None for a printer with no unit
        :param channel: thermal channel option
        :param callback: heaters callback, None for none
        :param min_temp: setup_minmax lower limit
        :param max_temp: setup_minmax upper limit
        :return TemperatureACE: the sensor
        """
        sensor = make_temperature_ace(unit=unit, values={"channel": channel},
                                      callback=callback, min_temp=min_temp,
                                      max_temp=max_temp)
        sensor._has_humidity = False
        return sensor

    @staticmethod
    def state(sensor: TemperatureACE) -> Dict[str, Any]:
        """
        :param sensor: the sensor
        :return dict: the reading state a sample updates
        """
        return {"temp": sensor.temp, "humidity": sensor.humidity,
                "has_humidity": sensor._has_humidity, "measured_min": sensor.measured_min,
                "measured_max": sensor.measured_max}

    def test_default_channel_reads_status_temp_and_humidity(self):
        # The GET_TEMP cache holds other values, so reading it would show.
        unit = make_ace2_unit("Ace1", hw_status={"temp": 42.0, "humidity": 31.0},
                              temp_info={"ptc1_temp": 55.0, "env_humidity": 35.0})
        sensor = self.sensor_for(unit)
        assert sensor._sample_ace_temperature(200.0) == 201.0
        assert self.state(sensor) == {"temp": 42.0, "humidity": 31.0, "has_humidity": True,
                                      "measured_min": 42.0, "measured_max": 42.0}
        assert sensor.printer.logger.messages == []
        assert sensor.printer.shutdowns == []

    def test_default_channel_v1_no_humidity_key(self):
        unit = make_ace_unit("Ace1", hw_status={"temp": 40.0})  # V1 ACE omits humidity
        sensor = self.sensor_for(unit)
        sensor._sample_ace_temperature(200.0)
        assert self.state(sensor) == {"temp": 40.0, "humidity": 0.0, "has_humidity": False,
                                      "measured_min": 40.0, "measured_max": 40.0}
        assert sensor.printer.logger.messages == []
        assert sensor.printer.shutdowns == []

    def test_ptc1_channel_reads_from_temp_cache(self):
        unit = make_ace2_unit(
            "Ace1", hw_status={"temp": 27.0, "humidity": 30.0},
            temp_info={"ptc1_temp": 55.0, "ptc2_temp": 60.0, "box1_temp": 24.0,
                       "env_temp": 27.0, "env_humidity": 35.0})
        sensor = self.sensor_for(unit, "ptc1")
        sensor._sample_ace_temperature(200.0)
        # ptc1 and env_humidity, not the status temp 27.0 and humidity 30.0
        assert self.state(sensor) == {"temp": 55.0, "humidity": 35.0, "has_humidity": True,
                                      "measured_min": 55.0, "measured_max": 55.0}
        assert sensor.printer.logger.messages == []
        assert sensor.printer.shutdowns == []

    @pytest.mark.parametrize("channel, expected", [("box1", 24.5), ("box2", 25.5),
                                                   ("ptc2", 60.0), ("env", 27.0)])
    def test_box_and_env_channels_select_the_right_field(self, channel, expected):
        unit = make_ace2_unit("Ace1", temp_info={
            "box1_temp": 24.5, "box2_temp": 25.5, "ptc1_temp": 55.0, "ptc2_temp": 60.0,
            "env_temp": 27.0})
        sensor = self.sensor_for(unit, channel)
        sensor._sample_ace_temperature(200.0)
        assert self.state(sensor) == {"temp": expected, "humidity": 0.0,
                                      "has_humidity": False, "measured_min": expected,
                                      "measured_max": expected}
        assert sensor.printer.logger.messages == []
        assert sensor.printer.shutdowns == []

    def test_missing_channel_field_reads_zero(self):
        unit = make_ace2_unit("Ace1", temp_info={"box1_temp": 24.0})  # no ptc1 present
        sensor = self.sensor_for(unit, "ptc1")
        sensor.temp = 5.0
        sensor._sample_ace_temperature(200.0)
        assert self.state(sensor) == {"temp": 0.0, "humidity": 0.0, "has_humidity": False,
                                      "measured_min": float("inf"), "measured_max": 0.0}
        assert sensor.printer.logger.messages == []
        assert sensor.printer.shutdowns == []

    @pytest.mark.parametrize("channel, hw_status, temp_info", [
        ("default", {"temp": None, "humidity": None}, {}),
        ("ptc1", {}, {"ptc1_temp": None, "env_humidity": None}),
    ])
    def test_null_readings_read_as_zero(self, channel, hw_status, temp_info):
        # Without the "or 0.0" guard, float(None) would raise and log a sampling error.
        unit = make_ace2_unit("Ace1", hw_status=hw_status, temp_info=temp_info)
        sensor = self.sensor_for(unit, channel)
        sensor.temp = 5.0
        sensor.humidity = 12.0
        sensor._sample_ace_temperature(200.0)
        assert self.state(sensor) == {"temp": 0.0, "humidity": 0.0, "has_humidity": True,
                                      "measured_min": float("inf"), "measured_max": 0.0}
        assert sensor._sample_error_logged is False
        assert sensor.printer.logger.messages == []
        assert sensor.printer.shutdowns == []

    def test_non_default_channel_tracks_measured_min_max(self):
        unit = make_ace2_unit("Ace1")
        sensor = self.sensor_for(unit, "ptc1")
        readings = []
        for ptc1 in (55.0, 48.0, 61.0):
            unit._cached_temp_info = {"ptc1_temp": ptc1}
            unit._temp_info_time = unit.printer.reactor.monotonic()
            sensor._sample_ace_temperature(200.0)
            readings.append((sensor.temp, sensor.measured_min, sensor.measured_max))
        assert readings == [(55.0, 55.0, 55.0), (48.0, 48.0, 55.0), (61.0, 48.0, 61.0)]
        assert sensor.printer.logger.messages == []
        assert sensor.printer.shutdowns == []

    def test_resolves_unit_when_none_then_reads_temp(self):
        unit = make_ace_unit("Ace1", hw_status={"temp": 30.0})
        sensor = make_temperature_ace(unit=unit, callback=None, ready=False)
        assert sensor._ace_unit is None
        with capture_log("temperature_ace") as fallback:
            sensor._sample_ace_temperature(200.0)
        assert sensor._ace_unit is unit
        assert self.state(sensor) == {"temp": 30.0, "humidity": 0.0, "has_humidity": False,
                                      "measured_min": 30.0, "measured_max": 30.0}
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []
        assert sensor.printer.shutdowns == []

    def test_no_unit_sets_temp_zero(self):
        sensor = self.sensor_for(None)
        sensor.temp = 5.0
        assert sensor._sample_ace_temperature(200.0) == 201.0
        assert sensor._ace_unit is None
        assert self.state(sensor) == {"temp": 0.0, "humidity": 0.0, "has_humidity": False,
                                      "measured_min": float("inf"), "measured_max": 0.0}
        assert sensor.printer.logger.messages == []
        assert sensor.printer.shutdowns == []

    def test_shutdown_below_minimum(self):
        unit = make_ace_unit("Ace1", hw_status={"temp": 40.26})
        sensor = self.sensor_for(unit, min_temp=50.0, max_temp=1000.0)
        sensor._sample_ace_temperature(200.0)
        assert sensor.printer.shutdowns == ["ACE temperature 40.3 below minimum of 50.0"]
        assert self.state(sensor) == {"temp": 40.26, "humidity": 0.0, "has_humidity": False,
                                      "measured_min": 40.26, "measured_max": 40.26}
        assert sensor.printer.logger.messages == []

    def test_no_min_shutdown_when_temp_above_minimum(self):
        unit = make_ace_unit("Ace1", hw_status={"temp": 60.0})
        sensor = self.sensor_for(unit, min_temp=50.0, max_temp=1000.0)
        sensor._sample_ace_temperature(200.0)
        assert sensor.printer.shutdowns == []
        assert self.state(sensor) == {"temp": 60.0, "humidity": 0.0, "has_humidity": False,
                                      "measured_min": 60.0, "measured_max": 60.0}
        assert sensor.printer.logger.messages == []

    def test_no_min_shutdown_when_temp_at_minimum(self):
        unit = make_ace_unit("Ace1", hw_status={"temp": 50.0})
        sensor = self.sensor_for(unit, min_temp=50.0, max_temp=1000.0)
        sensor._sample_ace_temperature(200.0)
        assert sensor.printer.shutdowns == []
        assert self.state(sensor) == {"temp": 50.0, "humidity": 0.0, "has_humidity": False,
                                      "measured_min": 50.0, "measured_max": 50.0}
        assert sensor.printer.logger.messages == []

    def test_no_min_shutdown_when_temp_not_positive(self):
        # 0.0 is below min_temp, but temp > 0 is False, so the minimum check must not
        # fire on it alone.
        unit = make_ace_unit("Ace1", hw_status={"temp": 0.0})
        sensor = self.sensor_for(unit, min_temp=50.0, max_temp=1000.0)
        sensor.temp = 55.0
        sensor._sample_ace_temperature(200.0)
        assert sensor.printer.shutdowns == []
        assert self.state(sensor) == {"temp": 0.0, "humidity": 0.0, "has_humidity": False,
                                      "measured_min": float("inf"), "measured_max": 0.0}
        assert sensor.printer.logger.messages == []

    def test_shutdown_above_maximum(self):
        unit = make_ace_unit("Ace1", hw_status={"temp": 80.26})
        sensor = self.sensor_for(unit, min_temp=0.0, max_temp=70.0)
        sensor._sample_ace_temperature(200.0)
        assert sensor.printer.shutdowns == ["ACE temperature 80.3 above maximum of 70.0"]
        assert self.state(sensor) == {"temp": 80.26, "humidity": 0.0, "has_humidity": False,
                                      "measured_min": 80.26, "measured_max": 80.26}
        assert sensor.printer.logger.messages == []

    def test_no_max_shutdown_when_temp_at_maximum(self):
        unit = make_ace_unit("Ace1", hw_status={"temp": 70.0})
        sensor = self.sensor_for(unit, min_temp=0.0, max_temp=70.0)
        sensor._sample_ace_temperature(200.0)
        assert sensor.printer.shutdowns == []
        assert self.state(sensor) == {"temp": 70.0, "humidity": 0.0, "has_humidity": False,
                                      "measured_min": 70.0, "measured_max": 70.0}
        assert sensor.printer.logger.messages == []

    def test_exception_logs_once_then_suppresses(self):
        unit = make_ace_unit("Ace1", hw_status={"temp": "bad"})
        sensor = self.sensor_for(unit)
        sensor.temp = 12.0
        logged = [("error", "temperature_ace: error sampling ACE temperature: "
                            "could not convert string to float: 'bad'")]

        assert sensor._sample_ace_temperature(200.0) == 201.0
        assert sensor.temp == 0.0
        assert sensor._sample_error_logged is True
        assert sensor.printer.logger.messages == logged

        sensor.temp = 12.0
        assert sensor._sample_ace_temperature(201.0) == 202.0
        assert sensor.temp == 0.0
        assert sensor._sample_error_logged is True
        assert sensor.printer.logger.messages == logged  # the second failure is not logged
        assert sensor.printer.shutdowns == []

    def test_callback_invoked_with_temperature(self):
        unit = make_ace_unit("Ace1", hw_status={"temp": 25.0})
        feed = Recorder()
        sensor = self.sensor_for(unit, callback=feed)
        printer = sensor.printer
        printer.objects["mcu"] = self.LaggingMcu(behind=40.0)
        printer.reactor.now = 130.0
        unit._hw_status_time = printer.reactor.now
        assert sensor._sample_ace_temperature(200.0) == 201.0
        # The print time is the mcu's estimate for the reactor clock: 130.0 - 40.0.
        assert feed.calls == [((90.0, 25.0), {})]
        assert sensor.temp == 25.0
        assert printer.logger.messages == []
        assert printer.shutdowns == []

    def test_default_channel_never_cached_is_not_logged_as_stale(self):
        unit = make_ace_unit("Ace1")
        sensor = self.sensor_for(unit)
        sensor.temp = 5.0
        sensor._sample_ace_temperature(200.0)
        assert sensor.temp == 5.0
        assert sensor._stale_logged is False
        assert sensor.printer.shutdowns == []
        assert sensor.printer.logger.messages == []

    def test_default_channel_stale_cache_is_logged_not_shut_down(self):
        unit = make_ace_unit("Ace1", hw_status={"temp": 40.0})
        sensor = self.sensor_for(unit)
        sensor.temp = 5.0
        sensor.printer.reactor.now = unit._hw_status_time + ACE_STALE_AFTER + 1
        sensor._sample_ace_temperature(200.0)
        expected = f"ACE temperature data is stale (no update for over {ACE_STALE_AFTER:.0f}s)"
        assert sensor.temp == 5.0  # the frozen cached 40.0 is not reported as live
        assert sensor._stale_logged is True
        assert sensor.printer.shutdowns == []
        assert sensor.printer.logger.messages == [("error", f"temperature_ace: {expected}")]

    def test_stale_cache_is_logged_only_once(self):
        unit = make_ace_unit("Ace1", hw_status={"temp": 40.0})
        sensor = self.sensor_for(unit)
        sensor.printer.reactor.now = unit._hw_status_time + ACE_STALE_AFTER + 1
        sensor._sample_ace_temperature(200.0)
        sensor._sample_ace_temperature(201.0)
        expected = f"ACE temperature data is stale (no update for over {ACE_STALE_AFTER:.0f}s)"
        assert sensor.printer.shutdowns == []
        assert sensor.printer.logger.messages == [("error", f"temperature_ace: {expected}")]

    def test_non_default_channel_never_cached_is_not_logged_as_stale(self):
        unit = make_ace2_unit("Ace1")
        sensor = self.sensor_for(unit, "ptc1")
        sensor.temp = 5.0
        sensor._sample_ace_temperature(200.0)
        assert sensor.temp == 5.0
        assert sensor._stale_logged is False
        assert sensor.printer.shutdowns == []
        assert sensor.printer.logger.messages == []

    def test_non_default_channel_stale_cache_is_logged_not_shut_down(self):
        unit = make_ace2_unit("Ace1", temp_info={"ptc1_temp": 55.0})
        sensor = self.sensor_for(unit, "ptc1")
        sensor.temp = 5.0
        sensor.printer.reactor.now = unit._temp_info_time + ACE_STALE_AFTER + 1
        sensor._sample_ace_temperature(200.0)
        expected = f"ACE temperature data is stale (no update for over {ACE_STALE_AFTER:.0f}s)"
        assert sensor.temp == 5.0  # the frozen cached 55.0 is not reported as live
        assert sensor._stale_logged is True
        assert sensor.printer.shutdowns == []
        assert sensor.printer.logger.messages == [("error", f"temperature_ace: {expected}")]


class TestTemperatureACEReportStaleCache:
    def test_never_cached_does_nothing(self):
        sensor = make_temperature_ace(unit=None, callback=None)
        sensor._report_stale_cache(None)
        assert sensor._stale_logged is False
        assert sensor.printer.shutdowns == []
        assert sensor.printer.logger.messages == []

    def test_stale_cache_is_logged(self):
        sensor = make_temperature_ace(unit=None, callback=None)
        sensor._report_stale_cache(50.0)
        expected = f"ACE temperature data is stale (no update for over {ACE_STALE_AFTER:.0f}s)"
        assert sensor._stale_logged is True
        assert sensor.printer.shutdowns == []
        assert sensor.printer.logger.messages == [("error", f"temperature_ace: {expected}")]

    def test_already_logged_does_not_repeat(self):
        sensor = make_temperature_ace(unit=None, callback=None)
        sensor._stale_logged = True
        sensor._report_stale_cache(50.0)
        assert sensor.printer.shutdowns == []
        assert sensor.printer.logger.messages == []


class TestTemperatureACEGetTemp:
    def test_returns_temp_and_zero_error(self):
        sensor = make_temperature_ace()
        sensor.temp = 33.3
        with capture_log("temperature_ace") as fallback:
            assert sensor.get_temp(200.0) == (33.3, 0.0)
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []


class TestTemperatureACEStats:
    def test_returns_false_and_status_line(self):
        sensor = make_temperature_ace("ace_dryer")
        sensor.temp = 44.46
        with capture_log("temperature_ace") as fallback:
            assert sensor.stats(200.0) == (False, "temperature_ace ace_dryer: temp=44.5")
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []


class TestTemperatureACEGetStatus:
    def test_without_humidity(self):
        sensor = make_temperature_ace()
        sensor.temp = 21.239
        sensor.humidity = 55.678
        sensor._has_humidity = False
        with capture_log("temperature_ace") as fallback:
            assert sensor.get_status(200.0) == {"temperature": 21.24}
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []

    def test_with_humidity(self):
        sensor = make_temperature_ace()
        sensor.temp = 21.239
        sensor.humidity = 55.678
        sensor._has_humidity = True
        with capture_log("temperature_ace") as fallback:
            assert sensor.get_status(200.0) == {"temperature": 21.24, "humidity": 55.68}
        assert sensor.printer.logger.messages == []
        assert fallback.messages == []


class TestLoadConfig:
    def test_registers_both_factories(self):
        printer = make_ace_printer()
        add = printer.heaters.add_sensor_factory = Hook(printer.heaters.add_sensor_factory)
        with capture_log("temperature_ace") as fallback:
            load_config(AceConfig("temperature_ace", printer))
        assert add.calls == [(("temperature_ace", TemperatureACE), {}),
                             (("aht2x", TemperatureAFC), {})]
        assert printer.heaters.factories == {"temperature_ace": TemperatureACE,
                                             "aht2x": TemperatureAFC}
        assert printer.heaters._temperature_ace_registered is True
        assert fallback.messages == []


class TestRegisterSensorFactory:
    def test_registers_both_factories_on_loaded_heaters(self):
        printer = make_ace_printer()
        add = printer.heaters.add_sensor_factory = Hook(printer.heaters.add_sensor_factory)
        with capture_log("temperature_ace") as fallback:
            _register_sensor_factory(AceConfig("temperature_ace", printer))
        assert add.calls == [(("temperature_ace", TemperatureACE), {}),
                             (("aht2x", TemperatureAFC), {})]
        assert printer.heaters.factories == {"temperature_ace": TemperatureACE,
                                             "aht2x": TemperatureAFC}
        assert printer.heaters._temperature_ace_registered is True
        assert fallback.messages == []

    def test_already_registered_returns_early(self):
        """Tracked on the heaters object (not a module flag) so a fresh
        heaters object after a Klipper RESTART is never mistaken for an
        already-registered one; see _register_sensor_factory's docstring."""
        printer = make_ace_printer()
        printer.heaters._temperature_ace_registered = True
        with capture_log("temperature_ace") as fallback:
            _register_sensor_factory(AceConfig("temperature_ace", printer))
        assert printer.heaters.factories == {}
        assert printer.heaters._temperature_ace_registered is True
        assert fallback.messages == []

    def test_load_object_fallback_when_lookup_fails(self):
        printer = make_ace_printer()
        heaters = printer.objects.pop("heaters")  # not loaded yet, so lookup_object raises
        add = heaters.add_sensor_factory = Hook(heaters.add_sensor_factory)
        printer.load_object = Recorder(result=heaters)
        config = AceConfig("temperature_ace", printer)
        with capture_log("temperature_ace") as fallback:
            _register_sensor_factory(config)
        # Klipper's load_object takes the config being loaded.
        assert printer.load_object.calls == [((config, "heaters"), {})]
        assert add.calls == [(("temperature_ace", TemperatureACE), {}),
                             (("aht2x", TemperatureAFC), {})]
        assert heaters.factories == {"temperature_ace": TemperatureACE,
                                     "aht2x": TemperatureAFC}
        assert heaters._temperature_ace_registered is True
        assert fallback.messages == []

    def test_load_object_failure_logs_warning(self):
        printer = make_ace_printer()
        del printer.objects["heaters"]
        with capture_log("temperature_ace") as fallback:
            _register_sensor_factory(AceConfig("temperature_ace", printer))
        assert fallback.messages == [
            ("warning", "temperature_ace: failed to load heaters: "
                        "Unable to load module 'heaters'")]
        assert printer.heaters.factories == {}
        assert not getattr(printer.heaters, "_temperature_ace_registered", False)
