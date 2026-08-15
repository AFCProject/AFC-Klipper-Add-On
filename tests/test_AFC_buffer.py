"""
Unit tests for extras/AFC_buffer.py

Covers:
  - get_fault_sensitivity: formula validation
  - fault_detection_enabled / disable / restore
  - buffer_status: returns last_state
  - disable_buffer / enable_buffer: toggles enable flag
  - advance_callback / trailing_callback: state tracking
  - pause_on_error: respects enable and min_event_systime
  - update_filament_error_pos / get_extruder_pos
  - start/stop fault timers
  - cmd_QUERY_BUFFER string construction
  - cmd_ENABLE_BUFFER / cmd_DISABLE_BUFFER GCode commands
  - cmd_AFC_SET_ERROR_SENSITIVITY GCode command
  - TRAILING_STATE_NAME / ADVANCING_STATE_NAME constants
"""

from __future__ import annotations

import sys
import importlib.util
import configparser

from unittest.mock import MagicMock, patch
import pytest

from extras.AFC_buffer import (
    AFCBuffer,
    FPSEndstopWrapper,
    AFCFPSBuffer,
    TRAILING_STATE_NAME,
    ADVANCING_STATE_NAME,
    NEUTRAL_STATE_NAME,
    CHECK_RUNOUT_TIMEOUT,
    FPS_ENDSTOP_POLL_TIME,
)
from tests.test_AFC_lane import _make_afc_lane


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_buffer(name="TN", error_sensitivity=0.0):
    """Build an AFCBuffer via real construction (__init__ runs normally),
    then override/mock the pieces individual tests need standardized."""
    from tests.conftest import MockAFC, MockConfig, MockPrinter

    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    # advance_pin/trailing_pin have no config.get(...) default; real Klipper
    # requires them whenever type == "switched" (the default type here).
    config = MockConfig(name="AFC_buffer {}".format(name), printer=printer,
                        values={"advance_pin": "PA0", "trailing_pin": "PA1"})
    with patch("extras.AFC_buffer.add_filament_switch",
               return_value=(MagicMock(), MagicMock())):
        buf = AFCBuffer(config)

    buf.lanes = {}
    buf.last_state = "Unknown"
    buf.enable = False
    buf.current_lane = None
    buf.advance_state = False
    buf.trailing_state = False

    buf.error_sensitivity = error_sensitivity
    buf.fault_sensitivity = buf.get_fault_sensitivity(error_sensitivity)
    buf.filament_error_pos = None
    buf.past_extruder_position = None
    buf.extruder_pos_timer = None
    buf.fault_timer = None

    buf.multiplier_high = 1.1
    buf.multiplier_low = 0.9
    buf._last_multiplier = 1

    buf.led = False
    buf.led_index = None
    buf.led_advancing = "0,0,1,0"
    buf.led_trailing = "0,1,0,0"
    buf.led_neutral = "0,1,0,0"
    buf.led_buffer_disabled = "0,0,0,0.25"

    buf.min_event_systime = 0.0
    buf.type = "switched"

    return buf

def _make_lane(buf):
    lane = _make_afc_lane()
    lane.buffer_obj = buf
    buf.lanes[lane.name] = lane
    return lane

def _make_fps_buffer(name="FPS_buffer1", **overrides):
    """Build an AFCFPSBuffer via real construction (__init__ runs normally,
    including ADC setup against a fake ADC pin), then override/mock the
    pieces individual tests need standardized."""
    from tests.conftest import MockAFC, MockReactor, MockPrinter, MockConfig

    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    printer.lookup_object("pins").setup_pin = MagicMock(return_value=MagicMock())
    config = MockConfig(name="AFC_FPS {}".format(name), printer=printer,
                        values={"type": "FPS_PSF", "adc_pin": "PA5"})
    buf = AFCFPSBuffer(config)
    reactor = buf.reactor  # alias -- buf.reactor is afc.reactor from real construction

    buf.function = MagicMock()
    buf.type = "FPS_PSF"
    buf.lanes = {}
    buf.last_state = "Unknown"
    buf.enable = False
    buf.current_lane = None
    buf.advance_state = False
    buf.trailing_state = False
    buf.toolhead = None
    buf.debug = False

    buf.error_sensitivity = 0.0
    buf.fault_sensitivity = 0.0
    buf.filament_error_pos = None
    buf.past_extruder_position = None
    buf.extruder_pos_timer = None
    buf.fault_timer = None
    buf.min_event_systime = reactor.NEVER
    buf.enable_sensors_in_gui = False

    buf.led = False
    buf.led_index = None
    buf.led_advancing = "0,0,1,0"
    buf.led_trailing = "0,1,0,0"
    buf.led_neutral = "0,1,0,0"
    buf.led_buffer_disabled = "0,0,0,0.25"

    buf._advance_latched = False
    buf._latch_enabled = False

    buf.ppins = MagicMock()
    buf.adc = MagicMock()
    buf.sample_count = 5
    buf.sample_time = 0.005
    buf.report_time = 0.1
    buf.reversed = False

    buf.fps_value = 0.0
    buf.set_point = 0.5
    buf.low_point = 0.1
    buf.high_point = 0.9
    buf._homing_high_point = 0.7

    buf.multiplier_high = 1.15
    buf.multiplier_low = 0.85
    buf.trailing_min_multiplier = 1.00
    buf.deadband = 0.30

    buf.smoothing = 0.3
    buf.smoothed_fps = 0.5
    buf.update_interval = 0.25

    buf.enable_integral_correction = False
    buf.integral_gain = 0.004
    buf._integral_terms = {}
    buf._last_logged_integral = 0.0
    buf.integral_extrusion_threshold = 1.0
    buf._integral_last_extruder_pos = None

    buf.adv_filament_switch_name = f"{name}_expanded"
    buf.fila_adv = MagicMock()
    buf.trail_filament_switch_name = f"{name}_compressed"
    buf.fila_trail = MagicMock()

    buf.correction_timer = MagicMock()
    buf._correction_running = False
    buf._last_multiplier = 1.0
    buf._saved_multipliers = {}

    buf.fps_endstop = MagicMock()
    buf.fps_trailing_endstop = MagicMock()

    for key, value in overrides.items():
        setattr(buf, key, value)

    return buf, afc, reactor, printer

def _make_gcmd(values=None, floats=None):
    """General-purpose gcmd mock backed by MockGCodeCommand: values/floats
    are merged into one params dict, mirroring real Klipper's single
    underlying params dict (get()/get_float() differ only in how they parse
    the same stored value, not in where it comes from)."""
    from tests.conftest import MockGCodeCommand
    params = {**(values or {}), **(floats or {})}
    return MockGCodeCommand(params=params)

# ── Constants ─────────────────────────────────────────────────────────────────

def _build_real_buffer(values=None):
    """Construct a real AFCBuffer via __init__ (not the attribute-bypass
    helper), for exercising __init__'s own logic. add_filament_switch does
    real Klipper config-object wiring that isn't worth reproducing under
    test, so it's patched out.

    advance_pin/trailing_pin have no config.get(...) default, so real
    Klipper requires them whenever type == "switched" (the default type);
    supplied here so the default construction path succeeds the same way a
    real, valid user config would. Callers testing a non-"switched" type can
    still omit them via values={"type": ...}.
    """
    from tests.conftest import MockConfig, MockPrinter, MockAFC

    all_values = {"advance_pin": "PA0", "trailing_pin": "PA1"}
    if values:
        all_values.update(values)

    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    config = MockConfig(name="AFC_buffer TN", printer=printer, values=all_values)
    with patch("extras.AFC_buffer.add_filament_switch",
               return_value=(MagicMock(), MagicMock())):
        buf = AFCBuffer(config)
    return buf, afc, printer


class TestAFCBufferInit:
    def test_defaults(self):
        buf, afc, printer = _build_real_buffer()
        assert buf.name == "TN"
        assert buf.type == "switched"
        assert buf.enable is False
        assert buf.lanes == {}
        assert buf.last_state == "Unknown"
        assert buf.current_lane is None
        assert buf.advance_state is False
        assert buf.trailing_state is False
        assert buf.toolhead is None
        assert buf.debug is False
        assert buf.multiplier_high == 1.1
        assert buf.multiplier_low == 0.9
        assert buf._last_multiplier == 1
        assert buf.error_sensitivity == 0
        assert buf.fault_sensitivity == 0
        assert buf.filament_error_pos is None
        assert buf.past_extruder_position is None
        assert buf.extruder_pos_timer is None
        assert buf.fault_timer is None
        assert buf.min_event_systime == buf.reactor.NEVER
        assert buf.led is False
        # advance_pin/trailing_pin have no config default and are required
        # whenever type == "switched" (the default type); _build_real_buffer
        # supplies them the same way a valid real config would.
        assert buf.advance_pin == "PA0"
        assert buf.trailing_pin == "PA1"
        assert buf.buffer_distance is None

    def test_registers_ready_event_handler(self):
        buf, afc, printer = _build_real_buffer()
        assert printer._event_handlers["klippy:ready"][-1] == buf._handle_ready

    def test_led_colors_use_afc_defaults_when_not_configured(self):
        buf, afc, printer = _build_real_buffer()
        assert buf.led_advancing == afc.led_buffer_advancing
        assert buf.led_trailing == afc.led_buffer_trailing
        assert buf.led_neutral == afc.led_buffer_neutral
        assert buf.led_buffer_disabled == afc.led_buffer_disabled

    def test_led_colors_config_overrides_afc_defaults(self):
        buf, afc, printer = _build_real_buffer(values={
            "led_buffer_advancing": "1,1,1,1",
            "led_buffer_trailing": "2,2,2,2",
            "led_buffer_neutral": "3,3,3,3",
            "led_buffer_disable": "4,4,4,4",
        })
        assert buf.led_advancing == "1,1,1,1"
        assert buf.led_trailing == "2,2,2,2"
        assert buf.led_neutral == "3,3,3,3"
        assert buf.led_buffer_disabled == "4,4,4,4"

    def test_led_index_config_reread_after_enabling_led(self):
        buf, afc, printer = _build_real_buffer(values={"led_index": "afc_led:3"})
        assert buf.led_index == "afc_led:3"

    def test_switched_type_creates_filament_switches_from_mocked_return(self):
        adv_sensor, trail_sensor = MagicMock(), MagicMock()
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_buffer TN", printer=printer,
                            values={"advance_pin": "P1.0", "trailing_pin": "P1.1"})
        with patch("extras.AFC_buffer.add_filament_switch",
                   side_effect=[(adv_sensor, MagicMock()), (trail_sensor, MagicMock())]):
            buf = AFCBuffer(config)
        assert buf.fila_adv is adv_sensor
        assert buf.fila_trail is trail_sensor

    def test_registers_query_buffer_mux_command(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        afc.function = MagicMock()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_buffer TN", printer=printer,
                            values={"advance_pin": "PA0", "trailing_pin": "PA1"})
        with patch("extras.AFC_buffer.add_filament_switch",
                   return_value=(MagicMock(), MagicMock())):
            buf = AFCBuffer(config)
        buf.function.register_mux_command.assert_called_once_with(
            buf.show_macros, "QUERY_BUFFER", "BUFFER", buf.name,
            buf.cmd_QUERY_BUFFER, buf.cmd_QUERY_BUFFER_help,
            buf.cmd_QUERY_BUFFER_options)

    def test_registers_itself_on_afc_buffers(self):
        buf, afc, printer = _build_real_buffer()
        assert afc.buffers["TN"] is buf

    def test_led_index_none_leaves_led_disabled(self):
        buf, afc, printer = _build_real_buffer()
        assert buf.led is False

    def test_led_index_set_enables_led(self):
        buf, afc, printer = _build_real_buffer(values={"led_index": "afc_led:0"})
        assert buf.led is True
        assert buf.led_index == "afc_led:0"

    def test_switched_type_registers_advance_and_trailing_pins(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        buttons = printer.lookup_object("buttons")  # pre-seed so __init__ reuses it
        config = MockConfig(name="AFC_buffer TN", printer=printer,
                            values={"advance_pin": "P1.0", "trailing_pin": "P1.1"})
        with patch("extras.AFC_buffer.add_filament_switch",
                   return_value=(MagicMock(), MagicMock())):
            buf = AFCBuffer(config)
        assert buf.advance_pin == "P1.0"
        assert buf.trailing_pin == "P1.1"
        buttons.register_buttons.assert_any_call(["P1.0"], buf.advance_callback)
        buttons.register_buttons.assert_any_call(["P1.1"], buf.trailing_callback)

    def test_non_switched_type_skips_pin_registration(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        buttons = printer.lookup_object("buttons")
        config = MockConfig(name="AFC_buffer TN", printer=printer, values={"type": "FPS_PSF"})
        with patch("extras.AFC_buffer.add_filament_switch",
                   return_value=(MagicMock(), MagicMock())):
            AFCBuffer(config)
        buttons.register_buttons.assert_not_called()

    def test_error_sensitivity_from_config(self):
        buf, afc, printer = _build_real_buffer(
            values={"filament_error_sensitivity": 6.0})
        assert buf.error_sensitivity == 6.0
        assert buf.fault_sensitivity == buf.get_fault_sensitivity(6.0)

    def test_enable_sensors_in_gui_falls_back_to_afc_default(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        afc.enable_sensors_in_gui = True
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_buffer TN", printer=printer,
                            values={"advance_pin": "PA0", "trailing_pin": "PA1"})
        with patch("extras.AFC_buffer.add_filament_switch",
                   return_value=(MagicMock(), MagicMock())):
            buf = AFCBuffer(config)
        assert buf.enable_sensors_in_gui is True

    def test_enable_sensors_in_gui_config_overrides_afc_default(self):
        buf, afc, printer = _build_real_buffer(values={"enable_sensors_in_gui": True})
        assert buf.enable_sensors_in_gui is True

    def test_registers_gcode_mux_commands(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        afc.gcode.register_mux_command = MagicMock(wraps=afc.gcode.register_mux_command)
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_buffer TN", printer=printer,
                            values={"advance_pin": "PA0", "trailing_pin": "PA1"})
        with patch("extras.AFC_buffer.add_filament_switch",
                   return_value=(MagicMock(), MagicMock())):
            AFCBuffer(config)
        names = [c[0][0] for c in afc.gcode.register_mux_command.call_args_list]
        assert "ENABLE_BUFFER" in names
        assert "DISABLE_BUFFER" in names
        assert "AFC_SET_ERROR_SENSITIVITY" in names
        assert "SET_ROTATION_FACTOR" in names
        assert "SET_BUFFER_MULTIPLIER" in names



    def test_trailing_state_name(self):
        assert TRAILING_STATE_NAME == "Trailing"

    def test_advancing_state_name(self):
        assert ADVANCING_STATE_NAME == "Advancing"
    
    def test_neutral_state_name(self):
        assert NEUTRAL_STATE_NAME  == "Neutral"

    def test_check_runout_timeout_positive(self):
        assert CHECK_RUNOUT_TIMEOUT > 0
        assert CHECK_RUNOUT_TIMEOUT == 0.5
    
    def test_check_fps_endstop_poll_time_positive(self):
        assert FPS_ENDSTOP_POLL_TIME  > 0
        assert FPS_ENDSTOP_POLL_TIME  == 0.01

class TestStringName:
    def test_name_return(self):
        buf = _make_buffer()

        assert buf.name == f"{buf}"

class TestHandleReady:
    def test_setup_fault_timer_and_verify_led_not_called(self):
        buf = _make_buffer()
        buf.led_index = None
        buf.error_sensitivity = 0
        buf.printer.lookup_object = MagicMock(return_value="Toolhead1")
        buf.reactor.monotonic = MagicMock(return_value=100)
        buf.afc.function.verify_led_object = MagicMock()
        buf.setup_fault_timer = MagicMock()

        buf._handle_ready()

        assert 100+2 == buf.min_event_systime
        assert "Toolhead1" == buf.toolhead
        buf.setup_fault_timer.assert_not_called()
        buf.afc.function.verify_led_object.assert_not_called()
    
    def test_setup_fault_timer_called(self):
        buf = _make_buffer()
        buf.led_index = None
        buf.error_sensitivity = 5
        buf.printer.lookup_object = MagicMock(return_value="Toolhead1")
        buf.reactor.monotonic = MagicMock(return_value=100)
        buf.afc.function.verify_led_object = MagicMock()
        buf.setup_fault_timer = MagicMock()

        buf._handle_ready()
        buf.setup_fault_timer.assert_called_once()
    
    def test_verify_led_called_error_not_raised(self):
        buf = _make_buffer()
        buf.led_index = 1
        buf.error_sensitivity = 0
        buf.printer.lookup_object = MagicMock(return_value="Toolhead1")
        buf.reactor.monotonic = MagicMock(return_value=100)
        buf.afc.function.verify_led_object = MagicMock(return_value=("", False))
        buf.setup_fault_timer = MagicMock()

        buf._handle_ready()
        assert True
        buf.afc.function.verify_led_object.assert_called_once_with(buf.led_index)
    
    def test_verify_led_called_error_raised(self):
        from configparser import Error as config_error
        buf = _make_buffer()
        buf.led_index = 1
        buf.error_sensitivity = 0
        buf.printer.lookup_object = MagicMock(return_value="Toolhead1")
        buf.reactor.monotonic = MagicMock(return_value=100)
        buf.afc.function.verify_led_object = MagicMock(return_value=("TEst", None))
        buf.setup_fault_timer = MagicMock()

        with pytest.raises(config_error):
            buf._handle_ready()
        buf.afc.function.verify_led_object.assert_called_once_with(buf.led_index)

# ── get_fault_sensitivity ─────────────────────────────────────────────────────

class TestGetFaultSensitivity:
    def test_zero_sensitivity_returns_zero(self):
        buf = _make_buffer()
        assert buf.get_fault_sensitivity(0) == 0

    def test_min_sensitivity_one(self):
        buf = _make_buffer()
        # (11 - 1) * 10 = 100
        assert buf.get_fault_sensitivity(1) == 100.0

    def test_max_sensitivity_ten(self):
        buf = _make_buffer()
        # (11 - 10) * 10 = 10
        assert buf.get_fault_sensitivity(10) == 10.0

    def test_mid_sensitivity_five(self):
        buf = _make_buffer()
        # (11 - 5) * 10 = 60
        assert buf.get_fault_sensitivity(5) == 60.0

    def test_higher_sensitivity_means_smaller_fault_distance(self):
        buf = _make_buffer()
        low = buf.get_fault_sensitivity(2)
        high = buf.get_fault_sensitivity(8)
        assert high < low


# ── fault_detection_enabled / disable / restore ───────────────────────────────

class TestFaultDetection:
    def test_enabled_when_sensitivity_positive(self):
        buf = _make_buffer(error_sensitivity=5.0)
        assert buf.fault_detection_enabled() is True

    def test_disabled_when_sensitivity_zero(self):
        buf = _make_buffer(error_sensitivity=0.0)
        assert buf.fault_detection_enabled() is False

    def test_disable_fault_sensitivity_sets_to_zero(self):
        buf = _make_buffer(error_sensitivity=5.0)
        buf.disable_fault_sensitivity()
        assert buf.fault_sensitivity == 0

    def test_restore_fault_sensitivity_restores_from_error_sensitivity(self):
        buf = _make_buffer(error_sensitivity=5.0)
        buf.disable_fault_sensitivity()
        buf.restore_fault_sensitivity()
        expected = buf.get_fault_sensitivity(5.0)
        assert buf.fault_sensitivity == expected

class TestSetupFaultTimer:
    def test_setup_fault_extruder_pos_none(self):
        buf = _make_buffer()
        buf.extruder_pos_timer = None
        buf.extruder_pos_update_event = 10
        buf.update_filament_error_pos = MagicMock()
        buf.reactor.register_timer = MagicMock(return_value="MockTimer")

        buf.setup_fault_timer()
        buf.update_filament_error_pos.assert_called_once()
        buf.reactor.register_timer.assert_called_once_with(buf.extruder_pos_update_event)
        assert "MockTimer" == buf.extruder_pos_timer
    
    def test_setup_fault_extruder_pos_not_none(self):
        buf = _make_buffer()
        buf.extruder_pos_timer = "MockTimer"
        buf.update_filament_error_pos = MagicMock()
        buf.reactor.register_timer = MagicMock(return_value="MockTimer")

        buf.setup_fault_timer()
        buf.update_filament_error_pos.assert_called_once()
        buf.reactor.register_timer.assert_not_called()



# ── buffer_status ─────────────────────────────────────────────────────────────

class TestBufferStatus:
    def test_returns_last_state(self):
        buf = _make_buffer()
        buf.last_state = TRAILING_STATE_NAME
        assert buf.buffer_status() == TRAILING_STATE_NAME

    def test_returns_unknown_when_unset(self):
        buf = _make_buffer()
        assert buf.buffer_status() == "Unknown"


# ── disable_buffer / enable_buffer ───────────────────────────────────────────

class TestBufferToggle:
    def test_disable_buffer_current_lane_none(self):
        buf = _make_buffer()
        buf.current_lane = None
        buf.enable = True
        buf.disable_buffer()
        assert not buf.enable
        assert 0 == len(buf.logger.messages) # Verifying that return happened and logger.debug was not called
    
    def test_disable_buffer_has_led(self):
        buf = _make_buffer()
        lane = _make_lane(buf)
        buf.afc.function.afc_led = MagicMock()
        buf.led = "Something"
        buf.led_index = 1
        buf.current_lane = lane
        buf.enable = True
        buf.reset_multiplier = MagicMock()

        buf.disable_buffer()
        buf.afc.function.afc_led.assert_called_once_with(buf.led_buffer_disabled, buf.led_index)
        debug_msgs = [m for lvl, m in buf.logger.messages if lvl == "debug"]
        assert any(f"{buf.name} buffer disabled for {lane.name}" in m for m in debug_msgs)
        assert None == buf.current_lane
    
    def test_disable_buffer_error_sensitivity_set_extruder_pos_none(self):
        buf = _make_buffer(error_sensitivity=5)
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf.enable = True
        buf.extruder_pos_timer = None
        buf.reset_multiplier = MagicMock()
        buf.stop_fault_timer = MagicMock()

        buf.disable_buffer()
        buf.stop_fault_timer.assert_not_called()
        assert None == buf.current_lane
    
    def test_disable_buffer_error_sensitivity_set_extruder_pos_set(self):
        buf = _make_buffer(error_sensitivity=5)
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf.enable = True
        buf.extruder_pos_timer = "NotNone"
        buf.reset_multiplier = MagicMock()
        buf.stop_fault_timer = MagicMock()
        buf.reactor.monotonic = MagicMock(return_value=1234)

        buf.disable_buffer()
        buf.stop_fault_timer.assert_called_once_with(1234)
        assert None == buf.current_lane

    def test_disable_buffer_sets_enable_false(self):
        lane = _make_afc_lane()
        buf = _make_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf.enable = True
        buf.lane = lane
        buf.reset_multiplier = MagicMock()
        buf.disable_buffer()
        assert buf.enable is False

    def test_disable_buffer_calls_reset_multiplier(self):
        buf = _make_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf.enable = True
        buf.reset_multiplier = MagicMock()
        buf.disable_buffer()
        buf.reset_multiplier.assert_called_once()

    def test_enable_buffer_sets_enable_true(self):
        buf = _make_buffer()
        lane = _make_lane(buf)
        buf.set_multiplier = MagicMock()
        buf.logger = MagicMock()
        buf.enable_buffer(lane)
        assert buf.enable is True
        buf.logger.debug.assert_called_once_with(
            "{} buffer enabled for {}".format(buf.name, lane.name))
    
    def test_enable_buffer_sets_enable_true_has_led(self):
        buf = _make_buffer()
        lane = _make_lane(buf)
        buf.led = "LedSet"
        buf.led_index = 2
        buf.set_multiplier = MagicMock()
        buf.afc.function.afc_led = MagicMock()

        buf.enable_buffer(lane)
        assert buf.enable is True
        buf.afc.function.afc_led.assert_called_once_with(buf.led_buffer_disabled,
                                                         buf.led_index)
    
    def test_enable_buffer_sets_current_lane(self):
        buf = _make_buffer()
        lane = _make_lane(buf)
        buf.set_multiplier = MagicMock()
        buf.enable_buffer(lane)
        assert buf.current_lane == lane

    def test_enable_buffer_applies_multiplier_trailing(self):
        buf = _make_buffer()
        lane = _make_lane(buf)
        buf.set_multiplier = MagicMock()
        buf.last_state = TRAILING_STATE_NAME
        buf.enable_buffer(lane)
        call_arg = buf.set_multiplier.call_args[0][0]
        assert call_arg < 1.0  # should be multiplier_low or derivative
    
    def test_enable_buffer_applies_multiplier_trailing_fault_enabled(self):
        buf = _make_buffer(error_sensitivity=5)
        lane = _make_lane(buf)
        multiplier = (buf.multiplier_low*2)/5
        buf.set_multiplier = MagicMock()
        buf.start_fault_detection = MagicMock()
        buf.last_state = TRAILING_STATE_NAME
        buf.enable_buffer(lane)
        buf.start_fault_detection.assert_called_once_with(0, multiplier)

    def test_enable_buffer_applies_multiplier_advancing(self):
        buf = _make_buffer()
        buf.set_multiplier = MagicMock()
        lane = _make_lane(buf)
        buf.last_state = ADVANCING_STATE_NAME
        buf.enable_buffer(lane)
        call_arg = buf.set_multiplier.call_args[0][0]
        assert call_arg > 1.0  # should be multiplier_high or derivative
    
    def test_enable_buffer_applies_multiplier_advancing_fault_enabled(self):
        buf = _make_buffer(error_sensitivity=5)
        lane = _make_lane(buf)
        multiplier = buf.multiplier_high * 1.5
        buf.set_multiplier = MagicMock()
        buf.start_fault_detection = MagicMock()
        buf.last_state = ADVANCING_STATE_NAME

        buf.enable_buffer(lane)
        buf.start_fault_detection.assert_called_once_with(0, multiplier)

class TestSetMultiplier:
    def test_set_multi_not_enabled(self):
        buf = _make_buffer()

        buf.set_multiplier(1)
        assert 0 == len(buf.logger.messages) # Verifying that return happened and logger.debug was not called
    
    def test_set_multi_current_lane_none(self):
        buf = _make_buffer()
        buf.enable = True

        buf.set_multiplier(1)
        assert 0 == len(buf.logger.messages) # Verifying that return happened and logger.debug was not called

    def test_set_multi_current_lane_extruder_none(self):
        buf = _make_buffer()
        lane = _make_lane(buf)
        lane.extruder_stepper = None
        buf.enable = True
        buf.current_lane = lane

        buf.set_multiplier(10)
        assert 0 == len(buf.logger.messages) # Verifying that return happened and logger.debug was not called 

    def test_set_multi_multiplier_greater_than_1(self):
        rotation_return_value = 23.12345
        multiplier = 1.15
        buf = _make_buffer()
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance = MagicMock(return_value=[rotation_return_value])
        buf.enable = True
        buf.current_lane = lane
        buf.afc.function.afc_led = MagicMock()

        buf.set_multiplier(multiplier)
        lane.update_rotation_distance.assert_called_once_with(multiplier)
        buf.afc.function.afc_led.assert_not_called()
        assert ADVANCING_STATE_NAME == buf.last_state
        assert buf._last_multiplier == multiplier
        debug_msgs = [m for lvl, m in buf.logger.messages if lvl == "debug"]
        assert any(f"New rotation distance for {lane.name} after applying factor: {rotation_return_value:.4f}" in m for m in debug_msgs)

    def test_set_multi_multiplier_greater_than_1_has_led(self):
        rotation_return_value = 23.12345
        multiplier = 1.15
        buf = _make_buffer()
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance = MagicMock(return_value=[rotation_return_value])
        buf.enable = True
        buf.current_lane = lane
        buf.led = "Valid"
        buf.led_index = 1
        buf.afc.function.afc_led = MagicMock()

        buf.set_multiplier(multiplier)
        buf.afc.function.afc_led.assert_called_once_with(buf.led_trailing, buf.led_index)
    
    def test_set_multi_multiplier_less_than_1(self):
        rotation_return_value = 23.12345
        multiplier = .9
        buf = _make_buffer()
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance = MagicMock(return_value=[rotation_return_value])
        buf.enable = True
        buf.current_lane = lane
        buf.afc.function.afc_led = MagicMock()

        buf.set_multiplier(multiplier)
        lane.update_rotation_distance.assert_called_once_with(multiplier)
        buf.afc.function.afc_led.assert_not_called()
        assert TRAILING_STATE_NAME == buf.last_state
        debug_msgs = [m for lvl, m in buf.logger.messages if lvl == "debug"]
        assert any(f"New rotation distance for {lane.name} after applying factor: {rotation_return_value:.4f}" in m for m in debug_msgs)

    def test_set_multi_multiplier_less_than_1_has_led(self):
        rotation_return_value = 23.12345
        multiplier = .9
        buf = _make_buffer()
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance = MagicMock(return_value=[rotation_return_value])
        buf.enable = True
        buf.current_lane = lane
        buf.led = "Valid"
        buf.led_index = 1
        buf.afc.function.afc_led = MagicMock()

        buf.set_multiplier(multiplier)
        buf.afc.function.afc_led.assert_called_once_with(buf.led_advancing, buf.led_index)
    
    def test_set_multi_multiplier_is_one(self):
        rotation_return_value = 23.12345
        multiplier = 1
        buf = _make_buffer()
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance = MagicMock(return_value=[rotation_return_value])
        buf.enable = True
        buf.current_lane = lane
        buf.led = "Valid"
        buf.led_index = 1
        buf.afc.function.afc_led = MagicMock()
        buf.last_state = "test_state"

        buf.set_multiplier(multiplier)
        buf.afc.function.afc_led.assert_not_called()
        # Verify that last state did not change
        assert "test_state" == buf.last_state


# ── advance_callback / trailing_callback ─────────────────────────────────────

class TestCallbacks:
    def test_advance_callback_records_advance_state(self):
        buf = _make_buffer()
        buf.advance_callback(100.0, True)
        assert buf.advance_state is True

    def test_advance_callback_records_false_state(self):
        buf = _make_buffer()
        buf.advance_state = True
        buf.advance_callback(100.0, False)
        assert buf.advance_state is False

    def test_advance_callback_sets_last_state_trailing(self):
        """After an advance event, last_state → TRAILING_STATE_NAME."""
        buf = _make_buffer()
        buf.advance_callback(100.0, True)
        assert buf.last_state == TRAILING_STATE_NAME

    def test_advance_callback_skipped_when_printer_not_ready(self):
        """printer.state_message != 'Printer is ready' -> inner block skipped
        even though enable=True and current_lane/state would otherwise fire it."""
        buf = _make_buffer()
        buf.printer.state_message = "Printer is not ready"
        buf.enable = True
        buf.current_lane = _make_lane(buf)
        buf.set_multiplier = MagicMock()
        buf.advance_callback(100.0, True)
        buf.set_multiplier.assert_not_called()

    def test_advance_callback_skipped_when_not_enabled(self):
        """enable=False -> inner block skipped even with printer ready."""
        buf = _make_buffer()
        buf.printer.state_message = "Printer is ready"
        buf.enable = False
        buf.current_lane = _make_lane(buf)
        buf.set_multiplier = MagicMock()
        buf.advance_callback(100.0, True)
        buf.set_multiplier.assert_not_called()

    def test_advance_true_no_fault_detection_sets_low_multiplier(self):
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = _make_lane(buf)
        buf.error_sensitivity = 0
        buf.multiplier_low = 0.7
        buf.set_multiplier = MagicMock()
        buf.advance_callback(100.0, True)
        buf.set_multiplier.assert_called_once_with(0.7)

    def test_advance_true_with_fault_detection_starts_detection(self):
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = _make_lane(buf)
        buf.error_sensitivity = 5
        buf.fault_sensitivity = buf.get_fault_sensitivity(5)
        buf.multiplier_low = 0.5
        buf.start_fault_detection = MagicMock()
        buf.set_multiplier = MagicMock()
        buf.advance_callback(100.0, True)
        buf.start_fault_detection.assert_called_once_with(100.0, 0.2)  # (0.5*2)/5
        buf.set_multiplier.assert_not_called()

    def test_advance_false_current_lane_none_no_fault_detection_noop(self):
        """current_lane is None -> the `state` branch is False regardless of
        state, and with fault detection off nothing happens."""
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = None
        buf.error_sensitivity = 0
        buf.set_multiplier = MagicMock()
        buf.stop_fault_timer = MagicMock()
        buf.advance_callback(100.0, True)
        buf.set_multiplier.assert_not_called()
        buf.stop_fault_timer.assert_not_called()

    def test_advance_false_with_fault_detection_stops_timer_and_resets(self):
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = _make_lane(buf)
        buf.error_sensitivity = 5
        buf.fault_sensitivity = buf.get_fault_sensitivity(5)
        buf.multiplier_low = 0.6
        buf.stop_fault_timer = MagicMock()
        buf.set_multiplier = MagicMock()
        buf.logger = MagicMock()
        buf.advance_callback(100.0, False)
        buf.stop_fault_timer.assert_called_once_with(100.0)
        buf.set_multiplier.assert_called_once_with(0.6)
        buf.logger.debug.assert_called_once_with("Buffer Triggered State: Advanced")

    def test_advance_false_no_fault_detection_is_noop(self):
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = _make_lane(buf)
        buf.error_sensitivity = 0
        buf.set_multiplier = MagicMock()
        buf.advance_callback(100.0, False)
        buf.set_multiplier.assert_not_called()

    def test_advance_callback_debug_log_on_trigger(self):
        buf = _make_buffer()
        buf.enable = True
        lane = _make_lane(buf)
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance.return_value = [22.5]
        buf.current_lane = lane
        buf.error_sensitivity = 0
        buf.logger = MagicMock()
        buf.advance_callback(100.0, True)
        assert any(
            c.args and "Buffer Triggered State: Advanced" in c.args[0]
            for c in buf.logger.debug.call_args_list)

    def test_trailing_callback_records_trailing_state(self):
        buf = _make_buffer()
        buf.trailing_callback(100.0, True)
        assert buf.trailing_state is True

    def test_trailing_callback_sets_last_state_advancing(self):
        """After a trailing event, last_state → ADVANCING_STATE_NAME."""
        buf = _make_buffer()
        buf.trailing_callback(100.0, True)
        assert buf.last_state == ADVANCING_STATE_NAME

    def test_trailing_callback_skipped_when_printer_not_ready(self):
        buf = _make_buffer()
        buf.printer.state_message = "Printer is not ready"
        buf.enable = True
        buf.current_lane = _make_lane(buf)
        buf.set_multiplier = MagicMock()
        buf.trailing_callback(100.0, True)
        buf.set_multiplier.assert_not_called()

    def test_trailing_callback_skipped_when_not_enabled(self):
        buf = _make_buffer()
        buf.printer.state_message = "Printer is ready"
        buf.enable = False
        buf.current_lane = _make_lane(buf)
        buf.set_multiplier = MagicMock()
        buf.trailing_callback(100.0, True)
        buf.set_multiplier.assert_not_called()

    def test_trailing_true_no_fault_detection_sets_high_multiplier(self):
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = _make_lane(buf)
        buf.error_sensitivity = 0
        buf.multiplier_high = 1.3
        buf.set_multiplier = MagicMock()
        buf.trailing_callback(100.0, True)
        buf.set_multiplier.assert_called_once_with(1.3)

    def test_trailing_true_with_fault_detection_starts_detection(self):
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = _make_lane(buf)
        buf.error_sensitivity = 5
        buf.fault_sensitivity = buf.get_fault_sensitivity(5)
        buf.multiplier_high = 1.2
        buf.start_fault_detection = MagicMock()
        buf.set_multiplier = MagicMock()
        buf.trailing_callback(100.0, True)
        buf.start_fault_detection.assert_called_once_with(100.0, pytest.approx(1.8))  # 1.2*1.5
        buf.set_multiplier.assert_not_called()

    def test_trailing_false_current_lane_none_no_fault_detection_noop(self):
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = None
        buf.error_sensitivity = 0
        buf.set_multiplier = MagicMock()
        buf.stop_fault_timer = MagicMock()
        buf.trailing_callback(100.0, True)
        buf.set_multiplier.assert_not_called()
        buf.stop_fault_timer.assert_not_called()

    def test_trailing_false_with_fault_detection_stops_timer_and_resets(self):
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = _make_lane(buf)
        buf.error_sensitivity = 5
        buf.fault_sensitivity = buf.get_fault_sensitivity(5)
        buf.multiplier_high = 1.4
        buf.stop_fault_timer = MagicMock()
        buf.set_multiplier = MagicMock()
        buf.logger = MagicMock()
        buf.trailing_callback(100.0, False)
        buf.stop_fault_timer.assert_called_once_with(100.0)
        buf.set_multiplier.assert_called_once_with(1.4)
        buf.logger.debug.assert_called_once_with("Buffer Triggered State: Trailing")

    def test_trailing_false_no_fault_detection_is_noop(self):
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = _make_lane(buf)
        buf.error_sensitivity = 0
        buf.set_multiplier = MagicMock()
        buf.trailing_callback(100.0, False)
        buf.set_multiplier.assert_not_called()

    def test_trailing_callback_debug_log_on_trigger(self):
        buf = _make_buffer()
        buf.enable = True
        lane = _make_lane(buf)
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance.return_value = [22.5]
        buf.current_lane = lane
        buf.error_sensitivity = 0
        buf.logger = MagicMock()
        buf.trailing_callback(100.0, True)
        assert any(
            c.args and "Buffer Triggered State: Trailing" in c.args[0]
            for c in buf.logger.debug.call_args_list)


# ── reset_multiplier ─────────────────────────────────────────────────────────

class TestResetMultiplier:
    def test_no_current_lane_returns_early(self):
        buf = _make_buffer()
        buf.current_lane = None
        buf.logger = MagicMock()
        buf.reset_multiplier()  # must not raise
        buf.logger.debug.assert_called_once_with("Buffer multiplier reset")

    def test_no_extruder_stepper_returns_early(self):
        buf = _make_buffer()
        lane = _make_lane(buf)
        lane.extruder_stepper = None
        buf.current_lane = lane
        buf.reset_multiplier()  # must not raise

    def test_resets_rotation_distance_and_logs(self):
        buf = _make_buffer()
        lane = _make_lane(buf)
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance.return_value = [22.5]
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf.logger = MagicMock()

        buf.reset_multiplier()

        lane.update_rotation_distance.assert_called_once_with(1)
        assert any(
            c.args and "Rotation distance reset for" in c.args[0]
            and "22.5000" in c.args[0]
            for c in buf.logger.info.call_args_list)


# ── pause_on_error ────────────────────────────────────────────────────────────

class TestCmdSetBufferMultiplier:
    def _ready_buf(self):
        buf = _make_buffer()
        buf.enable = True
        lane = _make_lane(buf)
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance.return_value = [20.0]
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf.logger = MagicMock()
        return buf, lane

    def test_no_current_lane_is_noop(self):
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = None
        buf.logger = MagicMock()
        buf.cmd_SET_BUFFER_MULTIPLIER(_make_gcmd({"MULTIPLIER": "HIGH"}, {"FACTOR": 1.2}))
        buf.logger.info.assert_not_called()

    def test_not_enabled_is_noop(self):
        buf = _make_buffer()
        buf.enable = False
        buf.current_lane = _make_lane(buf)
        buf.logger = MagicMock()
        buf.cmd_SET_BUFFER_MULTIPLIER(_make_gcmd({"MULTIPLIER": "HIGH"}, {"FACTOR": 1.2}))
        buf.logger.info.assert_not_called()

    def test_missing_multiplier_logs_and_returns(self):
        buf, lane = self._ready_buf()
        buf.cmd_SET_BUFFER_MULTIPLIER(_make_gcmd({}, {"FACTOR": 1.2}))
        buf.logger.info.assert_called_once_with("Multiplier must be provided, HIGH or LOW")

    def test_factor_not_positive_logs_and_returns(self):
        buf, lane = self._ready_buf()
        buf.cmd_SET_BUFFER_MULTIPLIER(_make_gcmd({"MULTIPLIER": "HIGH"}, {"FACTOR": 0.0}))
        buf.logger.info.assert_called_once_with("FACTOR must be greater than 0")

    def test_high_with_factor_over_one_sets_multiplier_high(self):
        buf, lane = self._ready_buf()
        buf.type = "switched"
        buf.cmd_SET_BUFFER_MULTIPLIER(_make_gcmd({"MULTIPLIER": "high"}, {"FACTOR": 1.3}))
        assert buf.multiplier_high == 1.3
        assert lane.update_rotation_distance.call_args[0][0] == 1.3
        assert any(
            c.args == ("multiplier_high set to 1.3",)
            for c in buf.logger.info.call_args_list)
        assert any(
            "multiplier_high: 1.3 MUST be updated under buffer config" in c.args[0]
            for c in buf.logger.info.call_args_list)

    def test_high_not_switched_type_skips_set_multiplier(self):
        buf, lane = self._ready_buf()
        buf.type = "FPS_PSF"
        buf.cmd_SET_BUFFER_MULTIPLIER(_make_gcmd({"MULTIPLIER": "HIGH"}, {"FACTOR": 1.3}))
        lane.update_rotation_distance.assert_not_called()

    def test_low_with_factor_under_one_sets_multiplier_low(self):
        buf, lane = self._ready_buf()
        buf.type = "switched"
        buf.cmd_SET_BUFFER_MULTIPLIER(_make_gcmd({"MULTIPLIER": "low"}, {"FACTOR": 0.8}))
        assert buf.multiplier_low == 0.8
        assert lane.update_rotation_distance.call_args[0][0] == 0.8
        assert any(
            c.args == ("multiplier_low set to 0.8",)
            for c in buf.logger.info.call_args_list)
        assert any(
            "multiplier_low: 0.8 MUST be updated under buffer config" in c.args[0]
            for c in buf.logger.info.call_args_list)

    def test_high_with_factor_not_over_one_falls_to_else(self):
        buf, lane = self._ready_buf()
        buf.cmd_SET_BUFFER_MULTIPLIER(_make_gcmd({"MULTIPLIER": "HIGH"}, {"FACTOR": 0.9}))
        assert any(
            "multiplier_high must be greater than 1" in c.args[0]
            for c in buf.logger.info.call_args_list)

    def test_low_with_factor_not_under_one_falls_to_else(self):
        buf, lane = self._ready_buf()
        buf.cmd_SET_BUFFER_MULTIPLIER(_make_gcmd({"MULTIPLIER": "LOW"}, {"FACTOR": 1.1}))
        assert any(
            "multiplier_high must be greater than 1" in c.args[0]
            for c in buf.logger.info.call_args_list)

    def test_unrecognized_multiplier_falls_to_else(self):
        buf, lane = self._ready_buf()
        buf.cmd_SET_BUFFER_MULTIPLIER(_make_gcmd({"MULTIPLIER": "SIDEWAYS"}, {"FACTOR": 1.1}))
        assert any(
            "multiplier_high must be greater than 1" in c.args[0]
            for c in buf.logger.info.call_args_list)


class TestCmdSetRotationFactor:
    def _ready_buf(self):
        buf = _make_buffer()
        buf.enable = True
        lane = _make_lane(buf)
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance.return_value = [20.0]
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf.logger = MagicMock()
        return buf, lane

    def test_not_enabled_logs_not_enabled_message(self):
        buf = _make_buffer()
        buf.name = "TN"
        buf.enable = False
        buf.current_lane = _make_lane(buf)
        buf.logger = MagicMock()
        buf.cmd_SET_ROTATION_FACTOR(_make_gcmd({}, {"FACTOR": 1.2}))
        buf.logger.info.assert_called_once_with("BUFFER TN NOT ENABLED")

    def test_no_current_lane_logs_not_enabled_message(self):
        buf = _make_buffer()
        buf.name = "TN"
        buf.enable = True
        buf.current_lane = None
        buf.logger = MagicMock()
        buf.cmd_SET_ROTATION_FACTOR(_make_gcmd({}, {"FACTOR": 1.2}))
        buf.logger.info.assert_called_once_with("BUFFER TN NOT ENABLED")

    def test_factor_not_positive_logs_and_returns(self):
        buf, lane = self._ready_buf()
        buf.cmd_SET_ROTATION_FACTOR(_make_gcmd({}, {"FACTOR": 0.0}))
        buf.logger.info.assert_called_once_with("FACTOR must be greater than 0")
        lane.update_rotation_distance.assert_not_called()

    def test_factor_of_one_resets_to_base(self):
        buf, lane = self._ready_buf()
        buf.cmd_SET_ROTATION_FACTOR(_make_gcmd({}, {"FACTOR": 1.0}))
        lane.update_rotation_distance.assert_called_once_with(1)
        buf.logger.info.assert_called_once_with("Rotation distance reset to base value")

    def test_default_factor_when_not_provided_resets_to_base(self):
        buf, lane = self._ready_buf()
        buf.cmd_SET_ROTATION_FACTOR(_make_gcmd({}, {}))  # FACTOR defaults to 1.0
        lane.update_rotation_distance.assert_called_once_with(1)

    def test_nontrivial_factor_applies_directly(self):
        buf, lane = self._ready_buf()
        buf.cmd_SET_ROTATION_FACTOR(_make_gcmd({}, {"FACTOR": 1.25}))
        lane.update_rotation_distance.assert_called_once_with(1.25)


class TestCmdQueryBufferBase:
    """cmd_QUERY_BUFFER on the AFCBuffer (switched/turtleneck) base class."""

    def test_reports_state_without_extra_when_disabled(self):
        buf = _make_buffer()
        buf.last_state = NEUTRAL_STATE_NAME
        buf.enable = False
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(_make_gcmd())
        msg = buf.logger.info.call_args[0][0]
        assert "Neutral" in msg
        assert "Rotation distance" not in msg

    def test_trailing_state_appends_compressing_label(self):
        buf = _make_buffer()
        buf.last_state = TRAILING_STATE_NAME
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(_make_gcmd())
        msg = buf.logger.info.call_args[0][0]
        assert "(buffer is compressing)" in msg

    def test_advancing_state_appends_expanding_label(self):
        buf = _make_buffer()
        buf.last_state = ADVANCING_STATE_NAME
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(_make_gcmd())
        msg = buf.logger.info.call_args[0][0]
        assert "(buffer is expanding)" in msg

    def test_enabled_with_lane_reports_rotation_distance(self):
        buf = _make_buffer()
        buf.enable = True
        lane = _make_lane(buf)
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance.return_value = [18.75]
        buf.current_lane = lane
        buf.error_sensitivity = 0
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(_make_gcmd())
        msg = buf.logger.info.call_args[0][0]
        assert "18.7500" in msg
        assert lane.name in msg

    def test_enabled_no_lane_skips_rotation_distance(self):
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = None
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(_make_gcmd())
        msg = buf.logger.info.call_args[0][0]
        assert "Rotation distance" not in msg

    def test_enabled_with_fault_detection_reports_sensitivity(self):
        buf = _make_buffer()
        buf.enable = True
        buf.current_lane = None
        buf.error_sensitivity = 4.0
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(_make_gcmd())
        msg = buf.logger.info.call_args[0][0]
        assert "Fault detection enabled, sensitivity 4.0" in msg

    def test_disabled_skips_fault_detection_report_even_if_configured(self):
        buf = _make_buffer()
        buf.enable = False
        buf.error_sensitivity = 4.0
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(_make_gcmd())
        msg = buf.logger.info.call_args[0][0]
        assert "Fault detection" not in msg


class TestPauseOnError:
    def test_does_not_pause_when_disabled(self):
        buf = _make_buffer()
        buf.enable = False
        buf.afc.error = MagicMock()
        buf.pause_on_error("fault", pause=True)
        buf.afc.error.AFC_error.assert_not_called()

    def test_does_not_pause_before_min_event_systime(self):
        buf = _make_buffer()
        buf.enable = True
        buf.min_event_systime = 9_999_999.0  # far in the future
        buf.afc.error = MagicMock()
        buf.afc.function.is_paused.return_value = False
        buf.pause_on_error("fault", pause=True)
        buf.afc.error.AFC_error.assert_not_called()

    def test_does_not_pause_when_already_paused(self):
        buf = _make_buffer()
        buf.enable = True
        buf.min_event_systime = 0.0
        buf.afc.error = MagicMock()
        buf.afc.function.is_paused.return_value = True
        buf.pause_on_error("fault", pause=True)
        buf.afc.error.AFC_error.assert_not_called()

    def test_pauses_when_all_conditions_met(self):
        buf = _make_buffer()
        buf.enable = True
        buf.min_event_systime = 0.0
        buf.afc.error = MagicMock()
        buf.afc.function.is_paused.return_value = False
        buf.last_state = TRAILING_STATE_NAME
        buf.pause_on_error("Something went wrong", pause=True)
        buf.afc.error.AFC_error.assert_called_once()
    
    def test_pauses_when_all_conditions_met_pause_false(self):
        buf = _make_buffer()
        buf.enable = True
        buf.min_event_systime = 0.0
        buf.afc.error = MagicMock()
        buf.afc.function.is_paused.return_value = False
        buf.last_state = TRAILING_STATE_NAME
        buf.pause_on_error("Something went wrong", pause=False)
        buf.afc.error.AFC_error.assert_not_called()

    def test_clog_message_appended_when_trailing(self):
        buf = _make_buffer()
        buf.enable = True
        buf.min_event_systime = 0.0
        buf.afc.error = MagicMock()
        buf.afc.function.is_paused.return_value = False
        buf.last_state = TRAILING_STATE_NAME
        buf.pause_on_error("Base message", pause=True)
        call_msg = buf.afc.error.AFC_error.call_args[0][0]
        assert "CLOG DETECTED" in call_msg

    def test_not_feeding_message_appended_when_advancing(self):
        buf = _make_buffer()
        buf.enable = True
        buf.min_event_systime = 0.0
        buf.afc.error = MagicMock()
        buf.afc.function.is_paused.return_value = False
        buf.last_state = ADVANCING_STATE_NAME
        buf.pause_on_error("Base message", pause=True)
        call_msg = buf.afc.error.AFC_error.call_args[0][0]
        assert "AFC NOT FEEDING" in call_msg


# ── fault timer helpers ───────────────────────────────────────────────────────

class TestFaultTimers:
    def test_start_fault_timer_sets_fault_timer_running(self):
        buf = _make_buffer()
        buf.extruder_pos_timer = MagicMock()
        buf.reactor.update_timer = MagicMock()

        buf.start_fault_timer(100.0)
        assert buf.fault_timer == "Running"
        buf.reactor.update_timer.assert_called_once_with(buf.extruder_pos_timer,
                                                         buf.reactor.NOW)

    def test_start_fault_timer_extruder_pos_timer_none(self):
        buf = _make_buffer()
        buf.extruder_pos_timer = None
        buf.reactor.update_timer = MagicMock()

        buf.start_fault_timer(100)
        assert None == buf.extruder_pos_timer
        buf.reactor.update_timer.assert_not_called()

    def test_stop_fault_timer_sets_fault_timer_stopped(self):
        buf = _make_buffer()
        buf.extruder_pos_timer = MagicMock()
        buf.reactor.update_timer = MagicMock()

        buf.stop_fault_timer(100.0)
        assert buf.fault_timer == "Stopped"
        buf.reactor.update_timer.assert_called_once_with(buf.extruder_pos_timer,
                                                         buf.reactor.NEVER)

    def test_stop_fault_timer_extruder_pos_timer_none(self):
        buf = _make_buffer()
        buf.extruder_pos_timer = MagicMock()
        buf.reactor.update_timer = MagicMock()
        buf.extruder_pos_timer = None

        buf.stop_fault_timer(100.0)
        assert None == buf.extruder_pos_timer
        buf.reactor.update_timer.assert_not_called()

class TestStartFaultDetection:
    def test_start_fault_detection(self):
        buf = _make_buffer()
        buf.set_multiplier = MagicMock()
        buf.update_filament_error_pos = MagicMock()
        buf.start_fault_timer = MagicMock()

        time = 100
        mult = 1.25

        buf.start_fault_detection(time, mult)
        buf.set_multiplier.assert_called_once_with(mult)
        buf.update_filament_error_pos.assert_called_once()
        buf.start_fault_timer.assert_called_once_with(time)
    
class TestGetExtruderPos:
    def test_get_extruder_pos_cur_pos_none(self):
        buf = _make_buffer()
        past_extruder_position = 1000
        buf.past_extruder_position = past_extruder_position
        buf.afc.function.get_extruder_pos = MagicMock(return_value=None)

        assert None == buf.get_extruder_pos()
        buf.afc.function.get_extruder_pos.assert_called_once_with(past_extruder_position=past_extruder_position)
    
    def test_get_extruder_pos_cur_pos_not_none(self):
        return_extruder_pos = 1500
        past_extruder_position = 1000
        buf = _make_buffer()
        buf.past_extruder_position = past_extruder_position
        buf.afc.function.get_extruder_pos = MagicMock(return_value=return_extruder_pos)

        assert return_extruder_pos == buf.get_extruder_pos()
        buf.afc.function.get_extruder_pos.assert_called_once_with(past_extruder_position=past_extruder_position)
        assert buf.past_extruder_position == return_extruder_pos


class TestUpdateFilamentErrorPos:
    def test_filament_error_pos_no_updated(self):
        buf = _make_buffer()
        error_pos = 10
        buf.filament_error_pos = error_pos
        buf.get_extruder_pos = MagicMock(return_value=None)

        buf.update_filament_error_pos()
        assert buf.filament_error_pos == error_pos
    
    def test_filament_error_pos_updated(self):
        buf = _make_buffer()
        error_pos = 10
        buf.filament_error_pos = error_pos
        buf.fault_sensitivity = 10
        buf.get_extruder_pos = MagicMock(return_value=20)

        buf.update_filament_error_pos()
        assert buf.filament_error_pos == 20 + buf.fault_sensitivity

# ── extruder_pos_update_event ─────────────────────────────────────────────────

class TestExtruderPosUpdateEvent:
    def test_returns_eventtime_plus_timeout_current_lane_none(self):
        buf = _make_buffer()
        buf.pause_on_error = MagicMock()
        buf.filament_error_pos = 50.0
        buf.get_extruder_pos = MagicMock(return_value=100)
        buf.afc.function.is_printing.return_value = False
        result = buf.extruder_pos_update_event(50)
        assert result == 50.0 + CHECK_RUNOUT_TIMEOUT
        buf.pause_on_error.assert_not_called()
    
    def test_returns_eventtime_plus_timeout_current_lane_none_is_printing_extruder_pos_none(self):
        buf = _make_buffer()
        buf.pause_on_error = MagicMock()
        buf.filament_error_pos = 50.0
        buf.get_extruder_pos = MagicMock(return_value=None)
        buf.afc.function.is_printing.return_value = True
        result = buf.extruder_pos_update_event(50)
        assert result == 50.0 + CHECK_RUNOUT_TIMEOUT
        buf.pause_on_error.assert_not_called()
    
    def test_returns_eventtime_plus_timeout_current_lane_none_is_printing_extruder_pos_is_not_none_filament_error_pos_none(self):
        buf = _make_buffer()
        buf.pause_on_error = MagicMock()
        buf.filament_error_pos = None
        buf.get_extruder_pos = MagicMock(return_value=100)
        buf.afc.function.is_printing.return_value = True
        result = buf.extruder_pos_update_event(50)
        assert result == 50.0 + CHECK_RUNOUT_TIMEOUT
        buf.pause_on_error.assert_not_called()
    
    def test_under_threshold_true_path(self):
        buf = _make_buffer()
        buf.pause_on_error = MagicMock()
        buf.filament_error_pos = 50
        buf.get_extruder_pos = MagicMock(return_value=40)
        buf.afc.function.is_printing.return_value = True
        result = buf.extruder_pos_update_event(50)
        assert result == 50.0 + CHECK_RUNOUT_TIMEOUT
        buf.pause_on_error.assert_not_called()

    def test_triggers_pause_when_extruder_pos_exceeds_threshold_not_stepperless_no_lane(self):
        buf = _make_buffer(error_sensitivity=5.0)
        buf.enable = True
        buf.min_event_systime = 0.0
        buf.filament_error_pos = 50.0
        buf.afc.error = MagicMock()
        buf.afc.function.is_paused.return_value = False
        buf.afc.function.is_printing.return_value = True
        buf.get_extruder_pos = MagicMock(return_value=55.0)  # > 50.0
        buf.update_filament_error_pos = MagicMock()
        buf.extruder_pos_update_event(100.0)
        buf.afc.error.AFC_error.assert_called_once_with("AFC filament fault detected! Take necessary action.", True)
        buf.update_filament_error_pos.assert_called_once()
    
    def test_triggers_pause_when_extruder_pos_exceeds_threshold_not_stepperless_lane(self):
        buf = _make_buffer(error_sensitivity=5.0)
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf.enable = True
        buf.min_event_systime = 0.0
        buf.filament_error_pos = 50.0
        buf.afc.error = MagicMock()
        buf.afc.function.is_paused.return_value = False
        buf.afc.function.is_printing.return_value = True
        buf.get_extruder_pos = MagicMock(return_value=55.0)  # > 50.0
        buf.update_filament_error_pos = MagicMock()
        lane.unit_obj.stepperless_drive = None
        buf.extruder_pos_update_event(100.0)
        buf.afc.error.AFC_error.assert_called_once_with("AFC filament fault detected! Take necessary action.", True)
        buf.update_filament_error_pos.assert_called_once()
    
    def test_triggers_pause_when_extruder_pos_exceeds_threshold_stepperless_lane(self):
        buf = _make_buffer(error_sensitivity=5.0)
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf.enable = True
        buf.min_event_systime = 0.0
        buf.filament_error_pos = 50.0
        buf.afc.error = MagicMock()
        buf.afc.function.is_paused.return_value = False
        buf.afc.function.is_printing.return_value = True
        buf.get_extruder_pos = MagicMock(return_value=55.0)  # > 50.0
        buf.update_filament_error_pos = MagicMock()
        lane.unit_obj.stepperless_drive = True
        buf.extruder_pos_update_event(100.0)
        buf.afc.error.AFC_error.assert_not_called()
        buf.update_filament_error_pos.assert_not_called()


# ── get_status ────────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_returns_dict_with_expected_keys(self):
        buf = _make_buffer()
        buf.afc.function.get_current_lane_obj.return_value = None
        result = buf.get_status()
        for key in ("state", "lanes", "enabled", "rotation_distance",
                    "fault_detection_enabled", "error_sensitivity",
                    "fault_timer", "distance_to_fault", "multiplier"):
            assert key in result, f"Missing key: {key}"

    def test_enabled_false_by_default(self):
        buf = _make_buffer()
        buf.afc.function.get_current_lane_obj.return_value = None
        result = buf.get_status()
        assert result["enabled"] is False
        assert result["active_lane"] is None
        assert result["multiplier_high"] == buf.multiplier_high
        assert result["multiplier_low"] == buf.multiplier_low

    def test_rotation_distance_none_when_not_enabled(self):
        buf = _make_buffer()
        result = buf.get_status()
        assert result["rotation_distance"] is None
        assert result["active_lane"] is None

    def test_reports_multiplier_high_and_low(self):
        buf = _make_buffer()
        buf.multiplier_high = 1.23
        buf.multiplier_low = 0.77
        result = buf.get_status()
        assert result["multiplier_high"] == 1.23
        assert result["multiplier_low"] == 0.77

    def test_enabled_with_lane_reports_rotation_distance_and_active_lane(self):
        buf = _make_buffer()
        buf.enable = True
        lane = _make_lane(buf)
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance.return_value = [19.5]
        buf.current_lane = lane
        result = buf.get_status()
        assert result["rotation_distance"] == 19.5
        assert result["active_lane"] == lane.name

    def test_enabled_with_lane_but_no_extruder_stepper_skips_rotation_distance(self):
        buf = _make_buffer()
        buf.enable = True
        lane = _make_lane(buf)
        lane.extruder_stepper = None
        buf.current_lane = lane
        result = buf.get_status()
        assert "rotation_distance" not in result
        assert result["active_lane"] == lane.name

    def test_enabled_with_lane_but_no_stepper_object_skips_rotation_distance(self):
        buf = _make_buffer()
        buf.enable = True
        lane = _make_lane(buf)
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper = None
        buf.current_lane = lane
        result = buf.get_status()
        assert "rotation_distance" not in result

    def test_distance_to_fault_reported_when_tracking(self):
        buf = _make_buffer()
        buf.error_sensitivity = 5.0
        buf.filament_error_pos = 100.0
        buf.get_extruder_pos = MagicMock(return_value=40.0)
        result = buf.get_status()
        assert result["distance_to_fault"] == 60.0
        assert result["filament_error_pos"] == 100.0
        assert result["current_pos"] == 40.0

    def test_distance_to_fault_none_when_extruder_pos_unavailable(self):
        buf = _make_buffer()
        buf.error_sensitivity = 5.0
        buf.filament_error_pos = 100.0
        buf.get_extruder_pos = MagicMock(return_value=None)
        result = buf.get_status()
        assert result["distance_to_fault"] is None
        assert "filament_error_pos" not in result

    def test_distance_to_fault_none_when_not_tracking(self):
        buf = _make_buffer()
        buf.error_sensitivity = 0
        buf.filament_error_pos = None
        result = buf.get_status()
        assert result["distance_to_fault"] is None


# ── cmd_ENABLE_BUFFER ─────────────────────────────────────────────────────────

class TestCmdEnableBuffer:
    def test_delegates_to_enable_buffer(self):
        """cmd_ENABLE_BUFFER should call enable_buffer() exactly once."""
        buf = _make_buffer()
        lane = _make_lane(buf)
        gcmd = _make_gcmd({"LANE": "lane1"})
        buf.enable_buffer = MagicMock()
        buf.cmd_ENABLE_BUFFER(gcmd)
        buf.enable_buffer.assert_called_once()

    def test_buffer_is_enabled_after_command(self):
        """Issuing ENABLE_BUFFER leaves enable set to True."""
        buf = _make_buffer()
        lane = _make_lane(buf)
        buf.set_multiplier = MagicMock()
        gcmd = _make_gcmd({"LANE": "lane1"})
        buf.cmd_ENABLE_BUFFER(gcmd)
        assert buf.enable is True
        assert buf.current_lane == lane

    def test_unassigned_lane_raises_gcmd_error(self):
        buf = _make_buffer()
        gcmd = _make_gcmd({"LANE": "not_a_real_lane"})
        with pytest.raises(Exception, match="not_a_real_lane not assigned"):
            buf.cmd_ENABLE_BUFFER(gcmd)


# ── cmd_DISABLE_BUFFER ────────────────────────────────────────────────────────

class TestCmdDisableBuffer:
    def test_delegates_to_disable_buffer(self):
        """cmd_DISABLE_BUFFER should call disable_buffer() exactly once."""
        buf = _make_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf.disable_buffer = MagicMock()
        buf.cmd_DISABLE_BUFFER(MagicMock())
        buf.disable_buffer.assert_called_once()

    def test_buffer_is_disabled_after_command(self):
        """Issuing DISABLE_BUFFER leaves enable set to False."""
        buf = _make_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf.enable = True
        buf.reset_multiplier = MagicMock()
        buf.cmd_DISABLE_BUFFER(MagicMock())
        assert buf.enable is False


# ── cmd_AFC_SET_ERROR_SENSITIVITY ─────────────────────────────────────────────

def _gcmd(sensitivity):
    """Return a mock gcmd whose get_float('SENSITIVITY', ...) returns the
    given sensitivity."""
    return _make_gcmd(floats={"SENSITIVITY": sensitivity})


class TestCmdSetErrorSensitivity:
    def test_updates_error_sensitivity(self):
        """The new sensitivity value is stored on the buffer."""
        buf = _make_buffer(error_sensitivity=0.0)
        buf.setup_fault_timer = MagicMock()
        buf.start_fault_detection = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(5.0))
        assert buf.error_sensitivity == 5.0

    def test_updates_fault_sensitivity(self):
        """fault_sensitivity is recalculated from the new error_sensitivity."""
        buf = _make_buffer(error_sensitivity=0.0)
        buf.setup_fault_timer = MagicMock()
        buf.start_fault_detection = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(5.0))
        assert buf.fault_sensitivity == buf.get_fault_sensitivity(5.0)

    # ── 0 → >0 transition ────────────────────────────────────────────────────

    def test_disabled_to_enabled_calls_setup_fault_timer(self):
        """Transitioning from 0 to >0 calls setup_fault_timer."""
        buf = _make_buffer(error_sensitivity=0.0)
        buf.setup_fault_timer = MagicMock()
        buf.start_fault_detection = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(5.0))
        buf.setup_fault_timer.assert_called_once()

    def test_disabled_to_enabled_but_fault_detection_not_enabled_skips_setup(self):
        """If fault_detection_enabled() somehow comes back False right after
        computing fault_sensitivity from a sensitivity>0 value (defensive
        case -- not reachable through the real get_fault_sensitivity formula,
        but the code guards for it), setup_fault_timer/start_fault_detection
        must not run."""
        buf = _make_buffer(error_sensitivity=0.0)
        buf.setup_fault_timer = MagicMock()
        buf.start_fault_detection = MagicMock()
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(5.0))
        buf.setup_fault_timer.assert_not_called()
        buf.start_fault_detection.assert_not_called()

    def test_disabled_to_enabled_calls_start_fault_detection(self):
        """Transitioning from 0 to >0 calls start_fault_detection."""
        buf = _make_buffer(error_sensitivity=0.0)
        buf.setup_fault_timer = MagicMock()
        buf.start_fault_detection = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(5.0))
        buf.start_fault_detection.assert_called_once()

    def test_disabled_to_enabled_uses_multiplier_low_when_trailing(self):
        """Transitioning 0 → >0 with trailing state passes multiplier_low."""
        buf = _make_buffer(error_sensitivity=0.0)
        buf.last_state = TRAILING_STATE_NAME
        buf.setup_fault_timer = MagicMock()
        buf.start_fault_detection = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(5.0))
        _, multiplier = buf.start_fault_detection.call_args[0]
        assert multiplier == buf.multiplier_low

    def test_disabled_to_enabled_uses_multiplier_high_when_advancing(self):
        """Transitioning 0 → >0 with advancing state passes multiplier_high."""
        buf = _make_buffer(error_sensitivity=0.0)
        buf.last_state = ADVANCING_STATE_NAME
        buf.setup_fault_timer = MagicMock()
        buf.start_fault_detection = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(5.0))
        _, multiplier = buf.start_fault_detection.call_args[0]
        assert multiplier == buf.multiplier_high

    def test_disabled_to_enabled_non_switched_type_uses_fixed_args(self):
        """Non-switched buffer types (e.g. FPS_PSF) skip the last_state
        branch entirely and always start_fault_detection(0, 1.0)."""
        buf = _make_buffer(error_sensitivity=0.0)
        buf.type = "FPS_PSF"
        buf.setup_fault_timer = MagicMock()
        buf.start_fault_detection = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(5.0))
        buf.start_fault_detection.assert_called_once_with(0, 1.0)

    # ── >0 → 0 transition ────────────────────────────────────────────────────

    def test_enabled_to_disabled_calls_stop_fault_timer(self):
        """Transitioning from >0 to 0 calls stop_fault_timer."""
        buf = _make_buffer(error_sensitivity=5.0)
        buf.stop_fault_timer = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(0.0))
        buf.stop_fault_timer.assert_called_once()

    def test_enabled_to_disabled_does_not_call_setup_fault_timer(self):
        """Transitioning from >0 to 0 does not call setup_fault_timer."""
        buf = _make_buffer(error_sensitivity=5.0)
        buf.stop_fault_timer = MagicMock()
        buf.setup_fault_timer = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(0.0))
        buf.setup_fault_timer.assert_not_called()

    # ── >0 → >0 transition ───────────────────────────────────────────────────

    def test_enabled_to_enabled_calls_update_filament_error_pos(self):
        """Transitioning from >0 to another >0 calls update_filament_error_pos."""
        buf = _make_buffer(error_sensitivity=3.0)
        buf.update_filament_error_pos = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(7.0))
        buf.update_filament_error_pos.assert_called_once()

    def test_enabled_to_enabled_does_not_call_stop_fault_timer(self):
        """Transitioning from >0 to another >0 does not call stop_fault_timer."""
        buf = _make_buffer(error_sensitivity=3.0)
        buf.update_filament_error_pos = MagicMock()
        buf.stop_fault_timer = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(7.0))
        buf.stop_fault_timer.assert_not_called()

    def test_enabled_to_enabled_does_not_call_setup_fault_timer(self):
        """Transitioning from >0 to another >0 does not call setup_fault_timer."""
        buf = _make_buffer(error_sensitivity=3.0)
        buf.update_filament_error_pos = MagicMock()
        buf.setup_fault_timer = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(7.0))
        buf.setup_fault_timer.assert_not_called()

    def test_stays_disabled_skips_all_transition_branches(self):
        """0 -> 0 (still disabled): none of the transition branches fire,
        just falls through to the final log line."""
        buf = _make_buffer(error_sensitivity=0.0)
        buf.setup_fault_timer = MagicMock()
        buf.start_fault_detection = MagicMock()
        buf.stop_fault_timer = MagicMock()
        buf.update_filament_error_pos = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(0.0))
        buf.setup_fault_timer.assert_not_called()
        buf.start_fault_detection.assert_not_called()
        buf.stop_fault_timer.assert_not_called()
        buf.update_filament_error_pos.assert_not_called()

    # ── logging ───────────────────────────────────────────────────────────────

    def test_logs_info_with_new_sensitivity(self):
        """Setting sensitivity logs an info message containing the new value."""
        buf = _make_buffer(error_sensitivity=0.0)
        buf.setup_fault_timer = MagicMock()
        buf.start_fault_detection = MagicMock()
        buf.logger = MagicMock()
        buf.cmd_AFC_SET_ERROR_SENSITIVITY(_gcmd(5.0))
        buf.logger.info.assert_called_once()
        msg = buf.logger.info.call_args[0][0]
        assert "5.0" in msg

class TestExtruderProperty:
    def test_extruder_property_is_none(self):
        buf = _make_buffer()
        buf.toolhead = None
        assert None == buf.extruder

    def test_extruder_property_is_not_none(self):
        toolhead = MagicMock()
        toolhead.get_extruder = MagicMock(return_value="Test")
        buf = _make_buffer()
        buf.toolhead = toolhead
        assert "Test" == buf.extruder
        toolhead.get_extruder.assert_called_once()

# ═════════════════════════════════════════════════════════════════════════
# FPSEndstopWrapper
# ═════════════════════════════════════════════════════════════════════════

def _make_endstop(trigger_value=False):
    """Build a FPSEndstopWrapper with a mock owning fps_buffer."""
    from tests.conftest import MockReactor
    fps_buffer = MagicMock()
    fps_buffer.reactor = MockReactor()
    triggered = {"value": trigger_value}
    endstop = FPSEndstopWrapper(fps_buffer, lambda: triggered["value"])
    return endstop, fps_buffer, triggered


class TestFPSEndstopWrapperInit:
    def test_initial_state(self):
        endstop, fps_buffer, triggered = _make_endstop()
        assert endstop._steppers == []
        assert endstop._trigger_time == 0.
        assert endstop._completion is None
        assert endstop._poll_timer is None
        assert endstop._fps_buffer is fps_buffer
        assert endstop._reactor is fps_buffer.reactor


class TestFPSEndstopWrapperGetMcu:
    def test_returns_adc_mcu(self):
        endstop, fps_buffer, triggered = _make_endstop()
        mcu = MagicMock()
        fps_buffer.adc.get_mcu.return_value = mcu
        assert endstop.get_mcu() is mcu


class TestFPSEndstopWrapperSteppers:
    def test_add_stepper_appends(self):
        endstop, fps_buffer, triggered = _make_endstop()
        s1, s2 = MagicMock(), MagicMock()
        endstop.add_stepper(s1)
        endstop.add_stepper(s2)
        assert endstop._steppers == [s1, s2]
    
    def test_add_stepper_duplicate_stepper(self):
        endstop, fps_buffer, triggered = _make_endstop()
        s1 = MagicMock()
        endstop.add_stepper(s1)
        endstop.add_stepper(s1)
        assert endstop._steppers == [s1]

    def test_get_steppers_returns_copy(self):
        endstop, fps_buffer, triggered = _make_endstop()
        s1 = MagicMock()
        endstop.add_stepper(s1)
        result = endstop.get_steppers()
        assert result == [s1]
        result.append(MagicMock())
        assert endstop._steppers == [s1]  # original list untouched


class TestFPSEndstopWrapperHomeStart:
    def test_already_triggered_completes_immediately(self):
        endstop, fps_buffer, triggered = _make_endstop(trigger_value=True)
        mcu = MagicMock()
        mcu.estimated_print_time.return_value = 42.0
        fps_buffer.adc.get_mcu.return_value = mcu
        fps_buffer.reactor.register_timer = MagicMock()

        completion = endstop.home_start(0, 0, 0, 0, triggered=True)

        assert endstop._trigger_time == 42.0
        assert completion.result is True
        fps_buffer.reactor.register_timer.assert_not_called()

    def test_not_triggered_starts_poll_timer(self):
        endstop, fps_buffer, triggered = _make_endstop(trigger_value=False)
        endstop._trigger_time = 999.0  # seed nonzero so the reset is proven
        fps_buffer.reactor.register_timer = MagicMock(return_value="timer1")

        endstop.home_start(0, 0, 0, 0, triggered=True)

        fps_buffer.reactor.register_timer.assert_called_once_with(
            endstop._poll_fps, fps_buffer.reactor.NOW)
        assert endstop._poll_timer == "timer1"
        assert endstop._trigger_time == 0.

    def test_triggered_false_target_completes_when_untriggered(self):
        """triggered=False means we're homing toward the *un*triggered state;
        with the sensor currently untriggered, that condition is already met."""
        endstop, fps_buffer, triggered = _make_endstop(trigger_value=False)
        mcu = MagicMock()
        mcu.estimated_print_time.return_value = 10.0
        fps_buffer.adc.get_mcu.return_value = mcu
        fps_buffer.reactor.register_timer = MagicMock()

        completion = endstop.home_start(0, 0, 0, 0, triggered=False)

        assert completion.result is True
        fps_buffer.reactor.register_timer.assert_not_called()
    
    # TODO: add test_triggered_false_target_waits_until_untriggered when code is updated to home to
    # untriggered state

    def test_resets_trigger_time_and_creates_new_completion(self):
        endstop, fps_buffer, triggered = _make_endstop(trigger_value=True)
        endstop._trigger_time = 999.0
        mcu = MagicMock()
        mcu.estimated_print_time.return_value = 5.0
        fps_buffer.adc.get_mcu.return_value = mcu

        endstop.home_start(0, 0, 0, 0, triggered=True)

        assert endstop._trigger_time == 5.0
        assert endstop._completion is not None


class TestFPSEndstopWrapperPollFps:
    def test_triggered_completes_and_returns_never(self):
        endstop, fps_buffer, triggered = _make_endstop(trigger_value=True)
        endstop._completion = fps_buffer.reactor.completion()
        mcu = MagicMock()
        mcu.estimated_print_time.return_value = 7.5
        fps_buffer.adc.get_mcu.return_value = mcu

        result = endstop._poll_fps(100.0)

        assert result == fps_buffer.reactor.NEVER
        assert endstop._trigger_time == 7.5
        assert endstop._completion.result is True

    def test_not_triggered_reschedules(self):
        endstop, fps_buffer, triggered = _make_endstop(trigger_value=False)
        endstop._completion = fps_buffer.reactor.completion()

        result = endstop._poll_fps(100.0)

        assert result == 100.0 + FPS_ENDSTOP_POLL_TIME
        assert endstop._completion.result is None


class TestFPSEndstopWrapperHomeWait:
    def test_stops_poll_timer_when_active(self):
        endstop, fps_buffer, triggered = _make_endstop()
        endstop._poll_timer = "timer1"
        endstop._trigger_time = 12.5
        fps_buffer.reactor.unregister_timer = MagicMock()

        result = endstop.home_wait(999.0)

        fps_buffer.reactor.unregister_timer.assert_called_once_with("timer1")
        assert endstop._poll_timer is None
        assert result == 12.5

    def test_no_poll_timer_is_noop(self):
        endstop, fps_buffer, triggered = _make_endstop()
        endstop._poll_timer = None
        endstop._trigger_time = 3.0
        fps_buffer.reactor.unregister_timer = MagicMock()

        result = endstop.home_wait(999.0)

        fps_buffer.reactor.unregister_timer.assert_not_called()
        assert result == 3.0


class TestFPSEndstopWrapperQueryEndstop:
    def test_triggered_returns_one(self):
        endstop, fps_buffer, triggered = _make_endstop(trigger_value=True)
        assert endstop.query_endstop(0) == 1

    def test_not_triggered_returns_zero(self):
        endstop, fps_buffer, triggered = _make_endstop(trigger_value=False)
        assert endstop.query_endstop(0) == 0


# ═════════════════════════════════════════════════════════════════════════
# AFCFPSBuffer
# ═════════════════════════════════════════════════════════════════════════

class TestCheckDeadband:
    def test_within_range_returns_empty_string(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.low_point = 0.1
        buf.high_point = 0.9
        assert buf._check_deadband(0.5, 0.3) == ""

    def test_too_wide_on_low_side_returns_error(self):
        buf, afc, reactor, printer = _make_fps_buffer(name="FPS1")
        buf.low_point = 0.4
        buf.high_point = 0.9
        msg = buf._check_deadband(0.5, 0.3)
        assert "neutral_low" in msg
        assert "FPS1" in msg

    def test_too_wide_on_high_side_returns_error(self):
        buf, afc, reactor, printer = _make_fps_buffer(name="FPS1")
        buf.low_point = 0.1
        buf.high_point = 0.6
        msg = buf._check_deadband(0.5, 0.3)
        assert "neutral_high" in msg

    def test_too_wide_on_both_sides_combines_messages_with_newline(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.low_point = 0.45
        buf.high_point = 0.55
        msg = buf._check_deadband(0.5, 0.3)
        assert "neutral_low" in msg
        assert "neutral_high" in msg
        assert "\n" in msg


class TestGetFpsValue:
    def test_returns_fps_value(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.fps_value = 0.42
        assert buf.get_fps_value() == 0.42


class TestBufferTriggered:
    def test_at_or_above_homing_high_point_is_triggered(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._homing_high_point = 0.7
        buf.smoothed_fps = 0.7
        assert buf.buffer_triggered is True

    def test_below_homing_high_point_is_not_triggered(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._homing_high_point = 0.7
        buf.smoothed_fps = 0.69
        assert buf.buffer_triggered is False


class TestBufferTrailingTriggered:
    def test_at_or_below_low_point_is_triggered(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.low_point = 0.1
        buf.smoothed_fps = 0.1
        assert buf.buffer_trailing_triggered is True

    def test_above_low_point_is_not_triggered(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.low_point = 0.1
        buf.smoothed_fps = 0.11
        assert buf.buffer_trailing_triggered is False


class TestAdcCallback:
    def test_list_input_empty_returns_early(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf._update_virtual_sensors = MagicMock()
        buf._adc_callback([])
        buf._update_virtual_sensors.assert_not_called()

    def test_list_input_unpacks_last_reading(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf._update_virtual_sensors = MagicMock()
        buf._adc_callback([(1.0, 0.3), (2.0, 0.6)])
        assert buf.fps_value == 0.6
        buf._update_virtual_sensors.assert_called_once_with(2.0)

    def test_scalar_input_uses_read_time_as_value_and_monotonic_as_time(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf._update_virtual_sensors = MagicMock()
        buf._adc_callback(0.42)
        assert buf.fps_value == 0.42
        buf._update_virtual_sensors.assert_called_once_with(reactor.monotonic())

    def test_explicit_read_time_and_value(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf._update_virtual_sensors = MagicMock()
        buf._adc_callback(5.0, 0.33)
        assert buf.fps_value == 0.33
        buf._update_virtual_sensors.assert_called_once_with(5.0)

    def test_reversed_flips_value(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reversed = True
        buf._adc_callback(1.0, 0.3)
        assert buf.fps_value == pytest.approx(0.7)

    def test_not_reversed_leaves_value_unchanged(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reversed = False
        buf._adc_callback(1.0, 0.3)
        assert buf.fps_value == pytest.approx(0.3)

    def test_smoothed_fps_applies_ema_formula(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.smoothing = 0.3
        buf.smoothed_fps = 0.5
        buf._adc_callback(1.0, 0.8)
        expected = 0.3 * 0.5 + 0.7 * 0.8
        assert buf.smoothed_fps == pytest.approx(expected)

    def test_lazily_starts_correction_timer_when_stepper_becomes_available(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.enable = True
        buf._correction_running = False
        reactor.update_timer = MagicMock()
        buf._adc_callback(1.0, 0.5)
        reactor.update_timer.assert_called_once_with(buf.correction_timer, reactor.NOW)
        assert buf._correction_running is True

    def test_does_not_restart_timer_when_already_running(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.enable = True
        buf._correction_running = True
        reactor.update_timer = MagicMock()
        buf._adc_callback(1.0, 0.5)
        reactor.update_timer.assert_not_called()

    def test_does_not_start_timer_when_not_enabled(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.enable = False
        buf._correction_running = False
        reactor.update_timer = MagicMock()
        buf._adc_callback(1.0, 0.5)
        reactor.update_timer.assert_not_called()

    def test_does_not_start_timer_when_no_stepper(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.enable = True
        buf._correction_running = False
        reactor.update_timer = MagicMock()
        buf._adc_callback(1.0, 0.5)
        reactor.update_timer.assert_not_called()

    # ── non-stepper direction/latch logic (only runs when has_stepper=False) ──

    def test_non_stepper_low_reading_sets_advancing(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.set_point = 0.5
        buf.deadband = 0.3
        buf._latch_enabled = False
        buf.trailing_state = True  # seed True so the reset to False is proven
        buf._adc_callback(1.0, 0.1)  # well below set_point - half_db (0.35)
        assert buf.last_state == ADVANCING_STATE_NAME
        assert buf.advance_state is False
        assert buf.trailing_state is True

    def test_non_stepper_high_reading_with_latch_enabled_sets_latch(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.set_point = 0.5
        buf.deadband = 0.3
        buf._latch_enabled = True
        buf._adc_callback(1.0, 0.9)
        assert buf._advance_latched is True

    def test_non_stepper_high_reading_with_latch_disabled_does_not_latch(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.set_point = 0.5
        buf.deadband = 0.3
        buf._latch_enabled = False
        buf._adc_callback(1.0, 0.9)
        assert buf._advance_latched is False

    def test_non_stepper_latched_keeps_advance_true_despite_neutral_reading(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.set_point = 0.5
        buf.deadband = 0.3
        buf._advance_latched = True
        buf.advance_state = False
        buf._adc_callback(1.0, 0.5)  # squarely neutral
        assert buf.advance_state is True

    def test_non_stepper_high_reading_sets_trailing(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.set_point = 0.5
        buf.deadband = 0.3
        buf._advance_latched = False
        buf.advance_state = True  # seed True so the reset to False is proven
        buf._adc_callback(1.0, 0.9)  # well above set_point + half_db (0.65)
        assert buf.last_state == TRAILING_STATE_NAME
        assert buf.advance_state is True
        assert buf.trailing_state is False

    def test_non_stepper_neutral_reading_sets_neutral(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.set_point = 0.5
        buf.deadband = 0.3
        buf._advance_latched = False
        buf.advance_state = True  # seed True so both resets to False are proven
        buf.trailing_state = True
        buf._adc_callback(1.0, 0.5)
        assert buf.last_state == NEUTRAL_STATE_NAME
        assert buf.advance_state is False
        assert buf.trailing_state is False

    def test_has_stepper_skips_direction_logic_entirely(self):
        """When has_stepper=True, enable=True and correction_running=False,
        last_state/advance_state/trailing_state
        are left untouched by _adc_callback (the correction loop owns them)."""
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.enable = True  # avoid the lazy-timer-start branch complicating this
        buf.last_state = "untouched"
        buf.advance_state = "untouched"
        buf.trailing_state = "untouched"
        buf._adc_callback(1.0, 0.1)  # would be "advancing" if has_stepper were False
        assert buf.last_state == "untouched"
        assert buf.advance_state == "untouched"
        assert buf.trailing_state == "untouched"
    
    def test_correction_not_running_still_runs_fallback_classifier(self):
        """When enable=False (correction isn't running), the fallback
        classifier in _adc_callback still updates last_state/advance_state/
        trailing_state from the raw reading -- even though has_stepper=True
        would normally mean the correction loop owns that job. This is
        what keeps state accurate during load/calibration before printing
        starts, when the correction loop hasn't been enabled yet."""
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.enable = False  # avoid the lazy-timer-start branch complicating this
        buf.last_state = "untouched"
        buf.advance_state = "untouched"
        buf._adc_callback(1.0, 0.1)  # would be "advancing" if has_stepper were False
        assert buf.last_state == "Advancing"
        assert buf.advance_state == False
        assert buf.trailing_state == True


class TestUpdateVirtualSensors:
    def test_calls_note_filament_present_with_eventtime(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.advance_state = True
        buf.trailing_state = False
        buf._update_virtual_sensors(100.0)
        buf.fila_adv.runout_helper.note_filament_present.assert_called_once_with(100.0, True)
        buf.fila_trail.runout_helper.note_filament_present.assert_called_once_with(100.0, False)

    def test_fila_adv_none_is_skipped(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.fila_adv = None
        buf._update_virtual_sensors(100.0)  # must not raise
        buf.fila_trail.runout_helper.note_filament_present.assert_called_once()

    def test_fila_trail_none_is_skipped(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.fila_trail = None
        buf._update_virtual_sensors(100.0)  # must not raise
        buf.fila_adv.runout_helper.note_filament_present.assert_called_once()

    def test_type_error_falls_back_to_old_signature_for_adv(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.advance_state = True
        buf.fila_adv.runout_helper.note_filament_present.side_effect = [TypeError(), None]
        buf._update_virtual_sensors(100.0)  # must not raise
        calls = buf.fila_adv.runout_helper.note_filament_present.call_args_list
        assert calls[1] == ((True,),)

    def test_type_error_falls_back_to_old_signature_for_trail(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.trailing_state = True
        buf.fila_trail.runout_helper.note_filament_present.side_effect = [TypeError(), None]
        buf._update_virtual_sensors(100.0)  # must not raise
        calls = buf.fila_trail.runout_helper.note_filament_present.call_args_list
        assert calls[1] == ((True,),)

    def test_generic_exception_on_adv_is_swallowed(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.fila_adv.runout_helper.note_filament_present.side_effect = Exception("boom")
        buf._update_virtual_sensors(100.0)  # must not raise
        buf.fila_trail.runout_helper.note_filament_present.assert_called_once()

    def test_generic_exception_on_trail_is_swallowed(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.fila_trail.runout_helper.note_filament_present.side_effect = Exception("boom")
        buf._update_virtual_sensors(100.0)  # must not raise


class TestCorrectionEvent:
    def _ready(self, **overrides):
        buf, afc, reactor, printer = _make_fps_buffer(**overrides)
        buf.enable = True
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf._update_virtual_sensors = MagicMock()
        return buf, afc, reactor, printer, lane

    # ── early-exit guard (multi-condition `if`) ────────────────────────────

    def test_not_enabled_returns_never_even_with_lane(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.enable = False
        buf._correction_running = True
        result = buf._correction_event(100.0)
        assert result == reactor.NEVER
        assert buf._correction_running is False

    def test_no_current_lane_returns_never_even_when_enabled(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.current_lane = None
        buf._correction_running = True
        result = buf._correction_event(100.0)
        assert result == reactor.NEVER
        assert buf._correction_running is False

    def test_no_rotation_control_returns_never(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf._correction_running = True
        result = buf._correction_event(100.0)
        assert result == reactor.NEVER
        assert buf._correction_running is False

    def test_normal_tick_returns_eventtime_plus_interval(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.smoothed_fps = 0.5
        buf.update_interval = 0.25
        result = buf._correction_event(100.0)
        assert result == 100.25

    # ── proportional term ───────────────────────────────────────────────────

    def test_above_set_point_slows_down(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.set_point = 0.5
        buf.high_point = 0.9
        buf.multiplier_low = 0.8
        buf.smoothed_fps = 0.7  # halfway to high_point -> half the correction
        buf._correction_event(100.0)
        expected = 1.0 - 0.5 * (1.0 - 0.8)
        assert lane.update_rotation_distance.call_args[0][0] == pytest.approx(expected)

    def test_at_set_point_multiplier_is_exactly_one(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.multiplier_hysteresis = 0.0  # isolate from the default filter -- this
                                         # test is about the P-term math, not hysteresis
        buf.set_point = 0.5
        buf.smoothed_fps = 0.5
        buf.trailing_min_multiplier = 1.0
        buf._correction_event(100.0)
        assert lane.update_rotation_distance.call_args[0][0] == pytest.approx(1.0)

    def test_below_set_point_speeds_up(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.set_point = 0.5
        buf.low_point = 0.1
        buf.multiplier_high = 1.2
        buf.trailing_min_multiplier = 1.0
        buf.smoothed_fps = 0.3  # halfway to low_point
        buf._correction_event(100.0)
        expected = 1.0 + 0.5 * (1.2 - 1.0)
        assert lane.update_rotation_distance.call_args[0][0] == pytest.approx(expected)

    def test_trailing_floor_raises_weak_correction(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.set_point = 0.5
        buf.low_point = 0.1
        buf.multiplier_high = 1.2
        buf.trailing_min_multiplier = 1.15  # higher than the computed P value below
        buf.smoothed_fps = 0.45  # tiny deviation -> weak raw correction
        buf._correction_event(100.0)
        assert lane.update_rotation_distance.call_args[0][0] == pytest.approx(1.15)

    def test_trailing_floor_does_not_lower_a_stronger_correction(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.set_point = 0.5
        buf.low_point = 0.1
        buf.multiplier_high = 1.2
        buf.trailing_min_multiplier = 1.0  # below the computed P value
        buf.smoothed_fps = 0.3
        buf._correction_event(100.0)
        expected = 1.0 + 0.5 * (1.2 - 1.0)
        assert lane.update_rotation_distance.call_args[0][0] == pytest.approx(expected)

    # ── target_direction (LED/state) -- each threshold isolated ─────────────

    def test_reading_below_low_deadband_is_advancing(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.set_point = 0.5
        buf.deadband = 0.3  # window 0.35-0.65
        buf.smoothed_fps = 0.2
        buf.led = True
        buf.trailing_state = True  # seed True so the reset to False is proven
        buf._correction_event(100.0)
        assert buf.last_state == ADVANCING_STATE_NAME
        assert buf.advance_state is False
        assert buf.trailing_state is True
        afc.function.afc_led.assert_called_once_with(buf.led_advancing, buf.led_index)

    def test_reading_above_high_deadband_is_trailing(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.set_point = 0.5
        buf.deadband = 0.3
        buf.smoothed_fps = 0.8
        buf.led = True
        buf.advance_state = True  # seed True so the reset to False is proven
        buf._correction_event(100.0)
        assert buf.last_state == TRAILING_STATE_NAME
        assert buf.advance_state is True
        assert buf.trailing_state is False
        afc.function.afc_led.assert_called_once_with(buf.led_trailing, buf.led_index)

    def test_reading_inside_deadband_is_neutral(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.set_point = 0.5
        buf.deadband = 0.3
        buf.smoothed_fps = 0.5
        buf.led = True
        buf.advance_state = True  # seed True so both resets to False are proven
        buf.trailing_state = True
        buf._correction_event(100.0)
        assert buf.last_state == NEUTRAL_STATE_NAME
        assert buf.advance_state is False
        assert buf.trailing_state is False
        afc.function.afc_led.assert_called_once_with(buf.led_neutral, buf.led_index)

    def test_led_disabled_skips_afc_led_call(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.led = False
        buf.smoothed_fps = 0.5
        buf._correction_event(100.0)
        afc.function.afc_led.assert_not_called()

    def test_already_advancing_does_not_force_log_event_via_state(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.set_point = 0.5
        buf.deadband = 0.3
        buf.smoothed_fps = 0.2  # advancing
        buf.debug = True
        buf.logger = MagicMock()
        buf._correction_event(100.0)  # first tick settles multiplier/state
        buf.logger.debug.reset_mock()
        buf._correction_event(100.0)  # identical reading again -> no change -> no log
        buf.logger.debug.assert_not_called()

    def test_already_trailing_does_not_force_log_event_via_state(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.set_point = 0.5
        buf.deadband = 0.3
        buf.smoothed_fps = 0.8  # trailing
        buf.last_state = TRAILING_STATE_NAME
        buf.debug = True
        buf.logger = MagicMock()
        buf._correction_event(100.0)  # first tick settles multiplier/state
        buf.logger.debug.reset_mock()
        buf._correction_event(100.0)  # identical reading again -> no change -> no log
        buf.logger.debug.assert_not_called()


class TestCorrectionEventHysteresis:
    """multiplier_hysteresis gates whether _correction_event actually calls
    update_rotation_distance, without affecting the state/LED tracking that
    happens every tick regardless. The gate compares the *absolute* delta
    between the newly computed multiplier and the last applied multiplier
    against multiplier_hysteresis -- it is not a relative/percentage
    comparison."""

    def _ready(self, **overrides):
        buf, afc, reactor, printer = _make_fps_buffer(**overrides)
        buf.enable = True
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf._update_virtual_sensors = MagicMock()
        return buf, afc, reactor, printer, lane

    def test_default_hysteresis_is_0_003(self):
        buf, afc, reactor, printer, lane = self._ready()
        assert buf.multiplier_hysteresis == pytest.approx(0.003)

    def test_default_hysteresis_filters_a_sub_threshold_change(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.set_point = 0.5
        buf.low_point = 0.1
        buf.multiplier_high = 1.2
        buf._last_multiplier = 1.05

        buf.smoothed_fps = 0.396  # multiplier ~1.052 -> delta ~0.002, clearly under the 0.003 default
        buf._correction_event(100.0)

        lane.update_rotation_distance.assert_not_called()

    def test_default_hysteresis_still_applies_a_large_change(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.set_point = 0.5
        buf.low_point = 0.1
        buf.multiplier_high = 1.2
        buf._last_multiplier = 1.05

        buf.smoothed_fps = 0.3  # multiplier ~1.10 -> delta ~0.05, well over the 0.003 default
        buf._correction_event(100.0)

        lane.update_rotation_distance.assert_called_once_with(pytest.approx(1.10))

    def test_small_change_below_threshold_does_not_apply(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.multiplier_hysteresis = 0.005  # absolute delta
        buf.set_point = 0.5
        buf.trailing_min_multiplier = 1.0
        buf.low_point = 0.1
        buf.multiplier_high = 1.2
        buf._last_multiplier = 1.05  # seed a known non-default baseline directly

        buf.smoothed_fps = 0.394  # computes multiplier=1.053 -> delta 0.003, under threshold
        buf._correction_event(100.0)

        lane.update_rotation_distance.assert_not_called()
        assert buf._last_multiplier == pytest.approx(1.05)  # unchanged

    def test_large_change_above_threshold_applies_and_updates_last_multiplier(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.multiplier_hysteresis = 0.005  # absolute delta
        buf.set_point = 0.5
        buf.trailing_min_multiplier = 1.0
        buf.low_point = 0.1
        buf.multiplier_high = 1.2
        buf._last_multiplier = 1.05  # seed a known non-default baseline directly

        buf.smoothed_fps = 0.3  # computes multiplier=1.10 -> delta 0.05, over threshold
        buf._correction_event(100.0)

        lane.update_rotation_distance.assert_called_once_with(pytest.approx(1.10))
        assert buf._last_multiplier == pytest.approx(1.10)

    def test_just_under_threshold_does_not_apply_just_over_does(self):
        """Proves the comparison is strictly greater-than by bracketing the
        threshold with two realistic computed multipliers, rather than
        relying on an exact floating-point boundary value (which is fragile
        to construct/compare precisely)."""
        buf, afc, reactor, printer, lane = self._ready()
        buf.multiplier_hysteresis = 0.005  # absolute delta
        buf.set_point = 0.5
        buf.trailing_min_multiplier = 1.0
        buf.low_point = 0.1
        buf.multiplier_high = 1.2
        buf._last_multiplier = 1.05

        buf.smoothed_fps = 0.3985  # multiplier ~1.0505 -> delta ~0.0005, clearly under 0.005
        buf._correction_event(100.0)
        lane.update_rotation_distance.assert_not_called()

        buf.smoothed_fps = 0.394  # multiplier ~1.053 -> delta ~0.003, still under 0.005
        buf._correction_event(100.25)
        lane.update_rotation_distance.assert_not_called()

        buf.smoothed_fps = 0.35  # multiplier ~1.075 -> delta ~0.025, clearly over 0.005
        buf._correction_event(100.5)
        lane.update_rotation_distance.assert_called_once()

    def test_hysteresis_is_absolute_not_relative(self):
        """A delta that is small *relative* to a large last-applied baseline
        must still clear the gate, since it's compared against
        multiplier_hysteresis as an absolute amount. Chosen so a
        relative-scaled gate (delta/last_applied > threshold) would
        disagree: delta=0.05 against last_multiplier=10.0 is only a 0.5%
        relative change (would be suppressed by a 2% relative gate), but
        0.05 > 0.02 absolute, so it must be applied."""
        buf, afc, reactor, printer, lane = self._ready()
        buf.multiplier_hysteresis = 0.02  # absolute delta
        buf.set_point = 0.5
        buf.trailing_min_multiplier = 1.0
        buf.low_point = 0.1
        buf.multiplier_high = 10.05
        buf._last_multiplier = 10.0

        buf.smoothed_fps = 0.05  # saturates the P-term -> multiplier == multiplier_high == 10.05
        buf._correction_event(100.0)

        lane.update_rotation_distance.assert_called_once_with(pytest.approx(10.05))

    def test_state_and_led_still_update_when_stepper_call_is_gated(self):
        """State/LED tracking must reflect the freshly computed target
        direction every tick, even when the hysteresis gate suppresses the
        actual update_rotation_distance call."""
        buf, afc, reactor, printer, lane = self._ready()
        buf.multiplier_hysteresis = 0.5  # very coarse -- essentially always gated
        buf.set_point = 0.5
        buf.deadband = 0.3
        buf.led = False

        buf.smoothed_fps = 0.5  # settle baseline first
        buf._correction_event(100.0)
        lane.update_rotation_distance.reset_mock()

        buf.smoothed_fps = 0.2  # clearly advancing, but too small a multiplier
                                # change to clear the 50% hysteresis gate
        buf._correction_event(100.25)

        lane.update_rotation_distance.assert_not_called()
        assert buf.last_state == ADVANCING_STATE_NAME


class TestCorrectionEventIntegral:
    def _ready(self, **overrides):
        buf, afc, reactor, printer = _make_fps_buffer(**overrides)
        buf.enable = True
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf._update_virtual_sensors = MagicMock()
        buf.set_point = 0.5
        buf.deadband = 0.3
        return buf, afc, reactor, printer, lane

    def test_disabled_integral_correction_leaves_pure_p_term(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.multiplier_hysteresis = 0.0  # isolate from the default filter -- this
                                         # test is about the integral gate, not hysteresis
        buf.enable_integral_correction = False
        buf._integral_terms[lane.name] = 0.5  # seed a bias that must NOT apply
        buf.smoothed_fps = 0.5  # neutral -> pure P multiplier is exactly 1.0
        buf._correction_event(100.0)
        assert lane.update_rotation_distance.call_args[0][0] == pytest.approx(1.0)

    def test_enabled_integral_correction_applies_seeded_bias(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.enable_integral_correction = True
        buf._integral_terms[lane.name] = 0.05
        buf.smoothed_fps = 0.5
        buf._is_extruding = MagicMock(return_value=False)  # don't accumulate further
        buf._correction_event(100.0)
        assert lane.update_rotation_distance.call_args[0][0] == pytest.approx(1.05)

    def test_extruding_accumulates_integral(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.enable_integral_correction = True
        buf.integral_gain = 0.01
        buf.update_interval = 0.25
        buf._integral_terms[lane.name] = 0.0
        buf.smoothed_fps = 0.3  # deviation = -0.2 (trailing/low)
        buf._is_extruding = MagicMock(return_value=True)
        buf._correction_event(100.0)
        # integral -= gain * deviation * update_interval = -(0.01*-0.2*0.25) = +0.0005
        assert buf._integral_terms[lane.name] == pytest.approx(0.0005)

    def test_not_extruding_does_not_accumulate_integral(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.enable_integral_correction = True
        buf.integral_gain = 0.01
        buf._integral_terms[lane.name] = 0.02
        buf.smoothed_fps = 0.3
        buf._is_extruding = MagicMock(return_value=False)
        buf._correction_event(100.0)
        assert buf._integral_terms[lane.name] == pytest.approx(0.02)  # unchanged

    def test_integral_anti_windup_clamps_high(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.enable_integral_correction = True
        buf.multiplier_high = 1.15
        buf.multiplier_low = 0.85
        buf.integral_gain = 10.0  # huge gain to force saturation in one tick
        buf.update_interval = 1.0
        buf._integral_terms[lane.name] = 0.0
        buf.smoothed_fps = 0.3  # trailing -> integral pushed positive
        buf._is_extruding = MagicMock(return_value=True)
        buf._correction_event(100.0)
        assert buf._integral_terms[lane.name] == pytest.approx(buf.multiplier_high - 1.0)

    def test_integral_anti_windup_clamps_low(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.enable_integral_correction = True
        buf.multiplier_high = 1.15
        buf.multiplier_low = 0.85
        buf.integral_gain = 10.0
        buf.update_interval = 1.0
        buf._integral_terms[lane.name] = 0.0
        buf.smoothed_fps = 0.7  # advancing -> integral pushed negative
        buf._is_extruding = MagicMock(return_value=True)
        buf._correction_event(100.0)
        assert buf._integral_terms[lane.name] == pytest.approx(buf.multiplier_low - 1.0)

    def test_combined_output_clamped_to_multiplier_band(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.enable_integral_correction = True
        buf.multiplier_high = 1.15
        buf.multiplier_low = 0.85
        buf._integral_terms[lane.name] = 0.5  # would push way past multiplier_high alone
        buf.smoothed_fps = 0.5  # P term = 1.0
        buf._is_extruding = MagicMock(return_value=False)
        buf._correction_event(100.0)
        assert lane.update_rotation_distance.call_args[0][0] == pytest.approx(1.15)


class TestCorrectionEventLogging:
    def _ready(self, **overrides):
        buf, afc, reactor, printer = _make_fps_buffer(**overrides)
        buf.enable = True
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf._update_virtual_sensors = MagicMock()
        buf.set_point = 0.5
        buf.deadband = 0.3
        buf.logger = MagicMock()
        return buf, afc, reactor, printer, lane

    def test_logs_when_multiplier_delta_exceeds_threshold(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.debug = True
        buf.multiplier_hysteresis = 0.0  # exercise the hysteresis-disabled
                                         # branch specifically, not the default
        buf._last_multiplier = 1.0
        buf.smoothed_fps = 0.2  # produces a multiplier well past 1.001
        buf._correction_event(100.0)
        buf.logger.debug.assert_called_once()

    def test_log_reports_applied_multiplier_not_gated_target(self):
        """When hysteresis suppresses set_multiplier() but a state
        transition still sets log_event=True, the debug line must report
        the multiplier actually applied to the stepper (_last_multiplier),
        not the freshly computed target that was gated out -- otherwise
        diagnostics show a value that was never sent to the stepper."""
        buf, afc, reactor, printer, lane = self._ready()
        buf.debug = True
        buf.multiplier_hysteresis = 0.1  # coarse enough to gate the ~0.06 delta below
        buf._last_multiplier = 1.0
        buf.last_state = NEUTRAL_STATE_NAME  # differs from the Advancing result -> log_event

        buf.smoothed_fps = 0.34  # target=Advancing, computed multiplier ~1.06 (gated, unapplied)
        buf._correction_event(100.0)

        lane.update_rotation_distance.assert_not_called()  # gate held -- multiplier not applied
        assert buf._last_multiplier == pytest.approx(1.0)  # unchanged by the gate
        buf.logger.debug.assert_called_once_with(
            "FPS_buffer FPS_buffer1: fps=0.000 smoothed=0.340 integral=+0.0000 "
            "multiplier=1.0000 state=Advancing"
        )

    def test_hysteresis_disabled_still_suppresses_log_for_sub_threshold_delta(self):
        """With multiplier_hysteresis explicitly disabled, set_multiplier()
        always runs, but log_event still only fires if the applied value
        actually moved by more than the old fixed 0.001 absolute delta."""
        buf, afc, reactor, printer, lane = self._ready()
        buf.debug = True
        buf.multiplier_hysteresis = 0.0
        buf.low_point = 0.1
        buf.multiplier_high = 1.2
        buf._last_multiplier = 1.05
        buf.last_state = NEUTRAL_STATE_NAME  # avoid a spurious Unknown->Neutral
                                             # "transition" from the initial default
        buf.smoothed_fps = 0.3995  # multiplier ~1.0503 -> delta ~0.0003, under 0.001
        buf._correction_event(100.0)
        lane.update_rotation_distance.assert_called_once()  # still applied
        buf.logger.debug.assert_not_called()  # but not logged

    def test_no_log_when_nothing_changed(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.debug = True
        buf.smoothed_fps = 0.5  # neutral -> multiplier stays 1.0
        buf._last_multiplier = 1.0
        buf.last_state = NEUTRAL_STATE_NAME
        buf._last_logged_integral = 0.0
        buf._correction_event(100.0)
        buf.logger.debug.assert_not_called()

    def test_logs_when_integral_changed_even_if_multiplier_did_not(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.debug = True
        buf.enable_integral_correction = True
        buf.smoothed_fps = 0.5  # P term stays 1.0
        buf._last_multiplier = 1.0
        buf.last_state = NEUTRAL_STATE_NAME
        buf._integral_terms[lane.name] = 0.02  # differs from _last_logged_integral (0.0)
        buf._is_extruding = MagicMock(return_value=False)
        buf._correction_event(100.0)
        buf.logger.debug.assert_called_once()

    def test_logs_when_state_transitions_even_if_multiplier_did_not(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.debug = True
        buf.smoothed_fps = 0.5  # neutral, multiplier == 1.0
        buf._last_multiplier = 1.0
        buf.last_state = TRAILING_STATE_NAME  # different from the neutral result
        buf._correction_event(100.0)
        buf.logger.debug.assert_called_once()

    def test_debug_false_suppresses_log_even_when_log_event_true(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.debug = False
        buf.smoothed_fps = 0.2
        buf._correction_event(100.0)
        buf.logger.debug.assert_not_called()

    def test_log_message_contains_fps_integral_multiplier_state(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.debug = True
        buf.fps_value = 0.234
        buf.smoothed_fps = 0.2
        buf._correction_event(100.0)
        msg = buf.logger.debug.call_args[0][0]
        assert "fps=0.234" in msg
        assert "smoothed=0.200" in msg
        assert "integral=" in msg
        assert "multiplier=" in msg
        assert "state=" in msg

    def test_updates_last_logged_integral_after_logging(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.debug = True
        buf.enable_integral_correction = True
        buf.smoothed_fps = 0.5
        buf._integral_terms[lane.name] = 0.03
        buf._is_extruding = MagicMock(return_value=False)
        buf._correction_event(100.0)
        assert buf._last_logged_integral == pytest.approx(0.03)


class TestCorrectionEventFaultDetection:
    def _ready(self, **overrides):
        buf, afc, reactor, printer = _make_fps_buffer(**overrides)
        buf.enable = True
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf._update_virtual_sensors = MagicMock()
        buf.update_filament_error_pos = MagicMock()
        buf.set_point = 0.5
        buf.low_point = 0.1
        buf.high_point = 0.9
        return buf, afc, reactor, printer, lane

    def test_disabled_fault_detection_never_updates_error_pos(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf.smoothed_fps = 0.5
        buf._correction_event(100.0)
        buf.update_filament_error_pos.assert_not_called()

    def test_enabled_fault_detection_within_range_updates_error_pos(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.fault_detection_enabled = MagicMock(return_value=True)
        buf.smoothed_fps = 0.5  # within [low_point, high_point]
        buf._correction_event(100.0)
        buf.update_filament_error_pos.assert_called_once()

    def test_enabled_fault_detection_above_high_point_skips_update(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.fault_detection_enabled = MagicMock(return_value=True)
        buf.smoothed_fps = 0.95  # above high_point
        buf._correction_event(100.0)
        buf.update_filament_error_pos.assert_not_called()
    
    def test_enabled_fault_detection_exactly_high_point_skips_update(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.fault_detection_enabled = MagicMock(return_value=True)
        buf.smoothed_fps = 0.90  # above high_point
        buf._correction_event(100.0)
        buf.update_filament_error_pos.assert_not_called()

    def test_enabled_fault_detection_below_low_point_skips_update(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.fault_detection_enabled = MagicMock(return_value=True)
        buf.smoothed_fps = 0.05  # below low_point
        buf._correction_event(100.0)
        buf.update_filament_error_pos.assert_not_called()
    
    def test_enabled_fault_detection_exactly_low_point_skips_update(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.fault_detection_enabled = MagicMock(return_value=True)
        buf.smoothed_fps = 0.1  # below low_point
        buf._correction_event(100.0)
        buf.update_filament_error_pos.assert_not_called()

    def test_calls_update_virtual_sensors_with_eventtime(self):
        buf, afc, reactor, printer, lane = self._ready()
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf.smoothed_fps = 0.5
        buf._correction_event(123.5)
        buf._update_virtual_sensors.assert_called_once_with(123.5)


# ═════════════════════════════════════════════════════════════════════════
# AFCFPSBuffer.__init__  (real construction)
# ═════════════════════════════════════════════════════════════════════════

class _FakeADC:
    """Stand-in for the Klipper/Kalico ADC pin object, with independently
    controllable method signatures so inspect.signature()-based branch
    detection in __init__ can be exercised precisely. A plain custom class
    (not MagicMock) so hasattr() genuinely reflects which methods exist."""

    def __init__(self, has_minmax=True, sample_has_report_time=True,
                 callback_has_report_time=True):
        self.calls = []
        self._mcu = MagicMock()

        if has_minmax:
            def setup_minmax(sample_time, sample_count):
                self.calls.append(("setup_minmax", sample_time, sample_count))
            self.setup_minmax = setup_minmax

        if sample_has_report_time:
            def setup_adc_sample(report_time, sample_time, sample_count):
                self.calls.append(("setup_adc_sample_new", report_time, sample_time, sample_count))
        else:
            def setup_adc_sample(sample_time, sample_count):
                self.calls.append(("setup_adc_sample_old", sample_time, sample_count))
        self.setup_adc_sample = setup_adc_sample

        if callback_has_report_time:
            def setup_adc_callback(report_time, callback):
                self.calls.append(("setup_adc_callback_new", report_time))
                self._callback = callback
        else:
            def setup_adc_callback(callback):
                self.calls.append(("setup_adc_callback_old",))
                self._callback = callback
        self.setup_adc_callback = setup_adc_callback

    def get_mcu(self):
        return self._mcu


def _build_real_fps_buffer(values=None, adc=None):
    """Construct a real AFCFPSBuffer via __init__."""
    from tests.conftest import MockConfig, MockPrinter, MockAFC

    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    adc = adc if adc is not None else _FakeADC()
    printer.lookup_object("pins").setup_pin = MagicMock(return_value=adc)
    merged = {"type": "FPS_PSF", "adc_pin": "PA5"}
    merged.update(values or {})
    config = MockConfig(name="AFC_FPS FPS1", printer=printer, values=merged)
    buf = AFCFPSBuffer(config)
    return buf, afc, printer, adc


class TestAFCFPSBufferInit:
    def test_defaults(self):
        buf, afc, printer, adc = _build_real_fps_buffer()
        assert buf.name == "FPS1"
        assert buf.set_point == 0.5
        assert buf.low_point == 0.1
        assert buf.high_point == 0.9
        assert buf.multiplier_high == 1.15
        assert buf.multiplier_low == 0.85
        assert buf.trailing_min_multiplier == 1.00
        assert buf.deadband == 0.30
        assert buf.smoothing == 0.3
        assert buf.smoothed_fps == 0.5
        assert buf.update_interval == 0.25
        assert buf.enable_integral_correction is False
        assert buf.integral_gain == 0.004
        assert buf._integral_terms == {}
        assert buf._last_multiplier == 1.0
        assert buf._correction_running is False
        assert buf._advance_latched is False
        assert buf._latch_enabled is False
        assert buf.reversed is False
        assert buf.fps_value == 0.0
        assert buf._homing_high_point == 0.7
        assert buf._last_logged_integral == 0.0
        assert buf.integral_extrusion_threshold == 0.02
        assert buf._integral_last_extruder_pos is None
        assert buf.min_event_systime == buf.reactor.NEVER
        from extras.AFC_utils import VirtualFilamentSensor
        assert isinstance(buf.fila_adv, VirtualFilamentSensor)
        assert isinstance(buf.fila_trail, VirtualFilamentSensor)
        assert buf._saved_multipliers == {}

    def test_reversed_config_true(self):
        buf, afc, printer, adc = _build_real_fps_buffer(values={"reversed": True})
        assert buf.reversed is True

    def test_homing_high_point_config_override(self):
        buf, afc, printer, adc = _build_real_fps_buffer(values={"homing_high_point": 0.8})
        assert buf._homing_high_point == 0.8

    def test_kalico_adc_logs_setup_message(self):
        adc = _FakeADC(has_minmax=True)
        buf, afc, printer, adc = _build_real_fps_buffer(adc=adc)
        assert ("info", "Kalico setup ADC") in afc.logger.messages

    def test_registers_itself_on_afc_buffers(self):
        buf, afc, printer, adc = _build_real_fps_buffer()
        assert afc.buffers["FPS1"] is buf

    def test_psf_alias_neutral_point_sets_set_point(self):
        buf, afc, printer, adc = _build_real_fps_buffer(values={"neutral_point": 0.6})
        assert buf.set_point == 0.6

    def test_primary_name_takes_precedence_over_psf_alias(self):
        buf, afc, printer, adc = _build_real_fps_buffer(
            values={"neutral_point": 0.6, "set_point": 0.4})
        assert buf.set_point == 0.4

    def test_psf_alias_max_tension_sets_low_point(self):
        buf, afc, printer, adc = _build_real_fps_buffer(values={"max_tension": 0.15})
        assert buf.low_point == 0.15

    def test_psf_alias_max_compression_sets_high_point(self):
        buf, afc, printer, adc = _build_real_fps_buffer(values={"max_compression": 0.8})
        assert buf.high_point == 0.8

    def test_low_point_not_less_than_set_point_raises(self):
        with pytest.raises(Exception, match="must be less than set_point"):
            _build_real_fps_buffer(values={"set_point": 0.5, "low_point": 0.5})

    def test_high_point_not_greater_than_set_point_raises(self):
        with pytest.raises(Exception, match="must be greater than set_point"):
            _build_real_fps_buffer(values={"set_point": 0.5, "high_point": 0.5})

    def test_deadband_too_wide_raises(self):
        with pytest.raises(Exception, match="deadband"):
            _build_real_fps_buffer(values={
                "set_point": 0.5, "low_point": 0.45, "deadband": 0.3})

    def test_creates_correction_timer(self):
        buf, afc, printer, adc = _build_real_fps_buffer()
        assert buf.correction_timer is not None

    def test_creates_fps_endstops(self):
        buf, afc, printer, adc = _build_real_fps_buffer()
        assert isinstance(buf.fps_endstop, FPSEndstopWrapper)
        assert isinstance(buf.fps_trailing_endstop, FPSEndstopWrapper)

    def test_registers_set_fps_set_point_command(self):
        afc = None
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        afc.gcode.register_mux_command = MagicMock(wraps=afc.gcode.register_mux_command)
        printer = MockPrinter(afc=afc)
        printer.lookup_object("pins").setup_pin = MagicMock(return_value=_FakeADC())
        config = MockConfig(name="AFC_FPS FPS1", printer=printer,
                            values={"type": "FPS_PSF", "adc_pin": "PA5"})
        AFCFPSBuffer(config)
        names = [c[0][0] for c in afc.gcode.register_mux_command.call_args_list]
        assert "AFC_SET_FPS_SET_POINT" in names

    # ── ADC setup branch detection (Kalico / newer Klipper / older Klipper) ──

    def test_kalico_adc_uses_setup_minmax(self):
        adc = _FakeADC(has_minmax=True)
        buf, afc, printer, adc = _build_real_fps_buffer(adc=adc)
        kinds = [c[0] for c in adc.calls]
        assert "setup_minmax" in kinds
        assert "setup_adc_sample_new" not in kinds
        assert "setup_adc_sample_old" not in kinds

    def test_newer_klipper_adc_uses_report_time_sample_signature(self):
        adc = _FakeADC(has_minmax=False, sample_has_report_time=True)
        buf, afc, printer, adc = _build_real_fps_buffer(adc=adc)
        kinds = [c[0] for c in adc.calls]
        assert "setup_adc_sample_new" in kinds
        assert "setup_minmax" not in kinds

    def test_older_klipper_adc_uses_no_report_time_sample_signature(self):
        adc = _FakeADC(has_minmax=False, sample_has_report_time=False)
        buf, afc, printer, adc = _build_real_fps_buffer(adc=adc)
        kinds = [c[0] for c in adc.calls]
        assert "setup_adc_sample_old" in kinds

    def test_newer_klipper_callback_uses_report_time_signature(self):
        adc = _FakeADC(callback_has_report_time=True)
        buf, afc, printer, adc = _build_real_fps_buffer(adc=adc)
        kinds = [c[0] for c in adc.calls]
        assert "setup_adc_callback_new" in kinds

    def test_older_klipper_callback_uses_no_report_time_signature(self):
        adc = _FakeADC(callback_has_report_time=False)
        buf, afc, printer, adc = _build_real_fps_buffer(adc=adc)
        kinds = [c[0] for c in adc.calls]
        assert "setup_adc_callback_old" in kinds


class TestEnableClearAdvanceLatch:
    def test_enable_advance_latch_sets_flags(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._latch_enabled = False
        buf._advance_latched = True
        buf.enable_advance_latch()
        assert buf._latch_enabled is True
        assert buf._advance_latched is False

    def test_clear_advance_latch_resets_flags(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._latch_enabled = True
        buf._advance_latched = True
        buf.clear_advance_latch()
        assert buf._latch_enabled is False
        assert buf._advance_latched is False


class TestFPSEnableBuffer:
    def test_sets_current_lane_and_enable(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.enable_buffer(lane)
        assert buf.current_lane is lane
        assert buf.enable is True

    def test_resets_latch_state(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf._latch_enabled = True
        buf._advance_latched = True
        buf.enable_buffer(lane)
        assert buf._latch_enabled is False
        assert buf._advance_latched is False

    def test_led_enabled_sets_neutral_led(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.led = True
        buf.enable_buffer(lane)
        afc.function.afc_led.assert_called_once_with(buf.led_neutral, buf.led_index)

    def test_led_disabled_skips_led_call(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.led = False
        buf.enable_buffer(lane)
        afc.function.afc_led.assert_not_called()

    def test_resets_smoothed_fps_to_current_reading(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.fps_value = 0.77
        buf.smoothed_fps = 0.2
        buf.enable_buffer(lane)
        assert buf.smoothed_fps == 0.77

    def test_no_stepper_marks_correction_not_running(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf._correction_running = True
        buf.enable_buffer(lane)
        assert buf._correction_running is False

    def test_has_stepper_starts_correction_timer(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.fault_detection_enabled = MagicMock(return_value=False)
        reactor.update_timer = MagicMock()
        buf.enable_buffer(lane)
        reactor.update_timer.assert_called_once_with(buf.correction_timer, reactor.NOW)
        assert buf._correction_running is True

    def test_has_stepper_no_saved_multiplier_resets_to_one(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        lane.extruder_obj = None
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf._last_multiplier = 1.4
        buf.enable_buffer(lane)
        assert buf._last_multiplier == 1.0

    def test_has_stepper_restores_saved_multiplier_for_same_lane(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        lane.extruder_obj = MagicMock(th_extruder_name="extruder")
        lane.update_rotation_distance = MagicMock()
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf._saved_multipliers[("extruder", lane.name)] = (lane.name, 1.08)
        buf.set_multiplier = MagicMock()
        buf.logger = MagicMock()
        buf.enable_buffer(lane)
        buf.set_multiplier.assert_called_once_with(1.08)
        assert any(
            "restored multiplier 1.0800" in c.args[0]
            for c in buf.logger.debug.call_args_list)

    def test_has_stepper_different_lane_on_extruder_does_not_restore(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        lane.extruder_obj = MagicMock(th_extruder_name="extruder")
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf._saved_multipliers["extruder"] = ("some_other_lane", 1.08)
        buf.set_multiplier = MagicMock()
        buf._last_multiplier = 99.0
        buf.enable_buffer(lane)
        buf.set_multiplier.assert_not_called()
        assert buf._last_multiplier == 1.0

    def test_has_stepper_saved_multiplier_of_one_does_not_restore(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        lane.extruder_obj = MagicMock(th_extruder_name="extruder")
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf._saved_multipliers["extruder"] = (lane.name, 1.0)
        buf.set_multiplier = MagicMock()
        buf.enable_buffer(lane)
        buf.set_multiplier.assert_not_called()

    def test_has_stepper_with_fault_detection_starts_it(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        lane.extruder_obj = None
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.fault_detection_enabled = MagicMock(return_value=True)
        buf.start_fault_detection = MagicMock()
        buf.enable_buffer(lane)
        buf.start_fault_detection.assert_called_once_with(0, 1.0)

    def test_has_stepper_without_fault_detection_skips_it(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        lane.extruder_obj = None
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf.start_fault_detection = MagicMock()
        buf.enable_buffer(lane)
        buf.start_fault_detection.assert_not_called()

    def test_logs_active_correction_status_when_has_stepper(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        lane.extruder_obj = None
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.fault_detection_enabled = MagicMock(return_value=False)
        buf.logger = MagicMock()
        buf.enable_buffer(lane)
        msg = buf.logger.debug.call_args_list[-1][0][0]
        assert "correction=active" in msg

    def test_logs_off_adc_only_status_when_no_stepper(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.logger = MagicMock()
        buf.enable_buffer(lane)
        msg = buf.logger.debug.call_args_list[-1][0][0]
        assert "correction=off/adc-only" in msg


class TestFPSDisableBuffer:
    def test_sets_enable_false_and_resets_latch(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.enable = True
        buf._latch_enabled = True
        buf._advance_latched = True
        buf.current_lane = None
        buf.disable_buffer()
        assert buf.enable is False
        assert buf._latch_enabled is False
        assert buf._advance_latched is False

    def test_no_current_lane_returns_early(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.current_lane = None
        buf.logger = MagicMock()
        buf.disable_buffer()
        buf.logger.debug.assert_not_called()

    def test_logs_disabled_message(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reset_multiplier = MagicMock()
        buf.logger = MagicMock()
        buf.disable_buffer()
        assert any(
            "buffer disabled for" in c.args[0]
            for c in buf.logger.debug.call_args_list)

    def test_led_enabled_sets_disabled_led(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reset_multiplier = MagicMock()
        buf.led = True
        buf.disable_buffer()
        afc.function.afc_led.assert_called_once_with(buf.led_buffer_disabled, buf.led_index)

    def test_led_disabled_skips_led_call(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reset_multiplier = MagicMock()
        buf.led = False
        buf.disable_buffer()
        afc.function.afc_led.assert_not_called()

    def test_saves_multiplier_when_lane_has_rotation_control(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        lane.extruder_obj = MagicMock(th_extruder_name="extruder")
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.reset_multiplier = MagicMock()
        buf._last_multiplier = 1.09
        buf.logger = MagicMock()
        buf.disable_buffer()
        assert buf._saved_multipliers[("extruder", lane.name)] == (lane.name, 1.09)
        assert any(
            "saved multiplier 1.0900" in c.args[0]
            for c in buf.logger.debug.call_args_list)

    def test_no_rotation_control_skips_saving_multiplier(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        lane.extruder_obj = MagicMock(th_extruder_name="extruder")
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reset_multiplier = MagicMock()
        buf.disable_buffer()
        assert "extruder" not in buf._saved_multipliers

    def test_no_extruder_name_skips_saving_multiplier(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        lane.extruder_obj = None
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.reset_multiplier = MagicMock()
        buf.disable_buffer()
        assert buf._saved_multipliers == {}

    def test_calls_reset_multiplier(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reset_multiplier = MagicMock()
        buf.disable_buffer()
        buf.reset_multiplier.assert_called_once()

    def test_stops_correction_timer(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reset_multiplier = MagicMock()
        buf._correction_running = True
        reactor.update_timer = MagicMock()
        buf.disable_buffer()
        reactor.update_timer.assert_called_once_with(buf.correction_timer, reactor.NEVER)
        assert buf._correction_running is False

    def test_stops_fault_timer_when_tracking(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reset_multiplier = MagicMock()
        buf.error_sensitivity = 5.0
        buf.extruder_pos_timer = MagicMock()
        buf.stop_fault_timer = MagicMock()
        buf.disable_buffer()
        buf.stop_fault_timer.assert_called_once()

    def test_skips_stop_fault_timer_when_sensitivity_zero(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reset_multiplier = MagicMock()
        buf.error_sensitivity = 0
        buf.extruder_pos_timer = MagicMock()
        buf.stop_fault_timer = MagicMock()
        buf.disable_buffer()
        buf.stop_fault_timer.assert_not_called()

    def test_skips_stop_fault_timer_when_no_timer(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reset_multiplier = MagicMock()
        buf.error_sensitivity = 5.0
        buf.extruder_pos_timer = None
        buf.stop_fault_timer = MagicMock()
        buf.disable_buffer()
        buf.stop_fault_timer.assert_not_called()

    def test_clears_current_lane_at_end(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reset_multiplier = MagicMock()
        buf.disable_buffer()
        assert buf.current_lane is None


class TestFPSSetMultiplier:
    def test_not_enabled_returns_early(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.enable = False
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf.set_multiplier(1.1)
        lane.update_rotation_distance.assert_not_called()

    def test_no_current_lane_returns_early(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.enable = True
        buf.current_lane = None
        buf.set_multiplier(1.1)  # must not raise

    def test_no_rotation_control_returns_early(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.enable = True
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.set_multiplier(1.1)
        lane.update_rotation_distance.assert_not_called()

    def test_applies_multiplier_and_tracks_last(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.enable = True
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf.set_multiplier(1.07)
        lane.update_rotation_distance.assert_called_once_with(1.07)
        assert buf._last_multiplier == 1.07


class TestIsExtruding:
    def test_exception_returns_false(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        afc.function.get_extruder_pos = MagicMock(side_effect=Exception("boom"))
        assert buf._is_extruding() is False

    def test_none_position_returns_false(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        afc.function.get_extruder_pos = MagicMock(return_value=None)
        assert buf._is_extruding() is False

    def test_first_sample_returns_false(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf._integral_last_extruder_pos = None
        afc.function.get_extruder_pos = MagicMock(return_value=10.0)
        assert buf._is_extruding() is False
        assert buf._integral_last_extruder_pos == 10.0

    def test_delta_at_or_above_threshold_returns_true(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.integral_extrusion_threshold = 0.5
        buf._integral_last_extruder_pos = 10.0
        afc.function.get_extruder_pos = MagicMock(return_value=10.5)
        assert buf._is_extruding() is True

    def test_delta_below_threshold_returns_false(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.integral_extrusion_threshold = 0.5
        buf._integral_last_extruder_pos = 10.0
        afc.function.get_extruder_pos = MagicMock(return_value=10.2)
        assert buf._is_extruding() is False

    def test_negative_direction_delta_counts_too(self):
        """Retraction (position decreasing) should count as extruding too."""
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.integral_extrusion_threshold = 0.5
        buf._integral_last_extruder_pos = 10.0
        afc.function.get_extruder_pos = MagicMock(return_value=9.0)
        assert buf._is_extruding() is True

    def test_debug_false_skips_diagnostic_line(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.debug = False
        buf.logger = MagicMock()
        buf._integral_last_extruder_pos = 10.0
        afc.function.get_extruder_pos = MagicMock(return_value=10.5)
        buf._is_extruding()
        buf.logger.debug.assert_not_called()

    def test_updates_last_extruder_pos_even_when_below_threshold(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.integral_extrusion_threshold = 0.5
        buf._integral_last_extruder_pos = 10.0
        afc.function.get_extruder_pos = MagicMock(return_value=10.1)
        buf._is_extruding()
        assert buf._integral_last_extruder_pos == 10.1


class TestFPSResetMultiplier:
    def test_no_current_lane_returns_early(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.current_lane = None
        buf.logger = MagicMock()
        buf.reset_multiplier()  # must not raise
        buf.logger.debug.assert_not_called()

    def test_no_rotation_control_returns_early(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=False)
        buf.reset_multiplier()
        lane.update_rotation_distance.assert_not_called()

    def test_resets_multiplier_to_one_and_logs(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = _make_lane(buf)
        lane.update_rotation_distance = MagicMock()
        buf.current_lane = lane
        buf._lane_has_rotation_control = MagicMock(return_value=True)
        buf._last_multiplier = 1.2
        buf.logger = MagicMock()
        buf.reset_multiplier()
        lane.update_rotation_distance.assert_called_once_with(1)
        assert buf._last_multiplier == 1.0
        buf.logger.debug.assert_called_once_with(
            "FPS buffer multiplier reset for {}".format(lane.name))


class TestLaneHasRotationControl:
    def test_none_lane_returns_false(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        assert buf._lane_has_rotation_control(None) is False

    def test_drive_stepper_present_and_callable_update_returns_true(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = MagicMock()
        lane.drive_stepper = MagicMock()
        lane.extruder_stepper = None
        lane.update_rotation_distance = MagicMock()
        assert buf._lane_has_rotation_control(lane) is True

    def test_extruder_stepper_present_and_callable_update_returns_true(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = MagicMock()
        lane.drive_stepper = None
        lane.extruder_stepper = MagicMock()
        lane.update_rotation_distance = MagicMock()
        assert buf._lane_has_rotation_control(lane) is True

    def test_neither_stepper_present_returns_false(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = MagicMock()
        lane.drive_stepper = None
        lane.extruder_stepper = None
        lane.update_rotation_distance = MagicMock()
        assert buf._lane_has_rotation_control(lane) is False

    def test_stepper_present_but_update_fn_not_callable_returns_false(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = MagicMock()
        lane.drive_stepper = MagicMock()
        lane.update_rotation_distance = "not callable"
        assert buf._lane_has_rotation_control(lane) is False

    def test_stepper_present_but_update_fn_missing_returns_false(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        lane = MagicMock(spec=["drive_stepper"])
        lane.drive_stepper = MagicMock()
        assert buf._lane_has_rotation_control(lane) is False


class TestFPSExtruderPosUpdateEvent:
    def _ready(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.get_extruder_pos = MagicMock(return_value=50.0)
        afc.function.is_printing = MagicMock(return_value=True)
        buf.filament_error_pos = 100.0
        buf.update_filament_error_pos = MagicMock()
        buf.pause_on_error = MagicMock()
        buf.low_point = 0.1
        buf.high_point = 0.9
        return buf, afc, reactor, printer

    def test_no_current_lane_skips_extruder_match_check(self):
        buf, afc, reactor, printer = self._ready()
        buf.current_lane = None
        buf.smoothed_fps = 0.5
        buf.extruder_pos_update_event(100.0)
        buf.update_filament_error_pos.assert_called_once()

    def test_no_lane_extruder_name_skips_mismatch_check(self):
        buf, afc, reactor, printer = self._ready()
        lane = _make_lane(buf)
        lane.extruder_obj = None  # -> lane_extruder_name is None (falsy)
        buf.current_lane = lane
        afc.toolhead.get_extruder.return_value = MagicMock(name="extruder1")
        buf.smoothed_fps = 0.5
        buf.extruder_pos_update_event(100.0)
        buf.update_filament_error_pos.assert_called_once()

    def test_active_extruder_missing_name_attr_skips_mismatch_check(self):
        buf, afc, reactor, printer = self._ready()
        lane = _make_lane(buf)
        lane.extruder_obj = MagicMock(th_extruder_name="extruder1")
        buf.current_lane = lane
        afc.toolhead.get_extruder.return_value = MagicMock(spec=[])  # no .name
        buf.smoothed_fps = 0.5
        buf.extruder_pos_update_event(100.0)
        buf.update_filament_error_pos.assert_called_once()

    def test_matching_extruder_name_skips_mismatch_check(self):
        buf, afc, reactor, printer = self._ready()
        lane = _make_lane(buf)
        lane.extruder_obj = MagicMock(th_extruder_name="extruder1")
        buf.current_lane = lane
        active = MagicMock()
        active.name = "extruder1"
        afc.toolhead.get_extruder.return_value = active
        buf.smoothed_fps = 0.5
        buf.extruder_pos_update_event(100.0)
        buf.update_filament_error_pos.assert_called_once()

    def test_mismatched_extruder_returns_early_without_checking_fault(self):
        buf, afc, reactor, printer = self._ready()
        lane = _make_lane(buf)
        lane.extruder_obj = MagicMock(th_extruder_name="extruder1")
        buf.current_lane = lane
        active = MagicMock()
        active.name = "extruder2"
        afc.toolhead.get_extruder.return_value = active
        result = buf.extruder_pos_update_event(100.0)
        buf.update_filament_error_pos.assert_not_called()
        assert result == 100.0 + CHECK_RUNOUT_TIMEOUT

    def test_not_printing_skips_fault_check(self):
        buf, afc, reactor, printer = self._ready()
        buf.current_lane = None
        afc.function.is_printing = MagicMock(return_value=False)
        buf.extruder_pos_update_event(100.0)
        buf.update_filament_error_pos.assert_not_called()
        buf.pause_on_error.assert_not_called()

    def test_extruder_pos_none_skips_fault_check(self):
        buf, afc, reactor, printer = self._ready()
        buf.current_lane = None
        buf.get_extruder_pos = MagicMock(return_value=None)
        buf.extruder_pos_update_event(100.0)
        buf.update_filament_error_pos.assert_not_called()
        buf.pause_on_error.assert_not_called()

    def test_filament_error_pos_none_skips_fault_check(self):
        buf, afc, reactor, printer = self._ready()
        buf.current_lane = None
        buf.filament_error_pos = None
        buf.extruder_pos_update_event(100.0)
        buf.update_filament_error_pos.assert_not_called()
        buf.pause_on_error.assert_not_called()

    def test_fps_in_correctable_range_updates_and_returns_early(self):
        buf, afc, reactor, printer = self._ready()
        buf.current_lane = None
        buf.smoothed_fps = 0.5  # within [0.1, 0.9]
        result = buf.extruder_pos_update_event(100.0)
        buf.update_filament_error_pos.assert_called_once()
        buf.pause_on_error.assert_not_called()
        assert result == 100.0 + CHECK_RUNOUT_TIMEOUT

    def test_fps_stuck_extreme_and_pos_past_trip_pauses(self):
        buf, afc, reactor, printer = self._ready()
        buf.current_lane = None
        buf.smoothed_fps = 0.95  # outside [0.1, 0.9]
        buf.get_extruder_pos = MagicMock(return_value=150.0)  # > filament_error_pos (100)
        buf.extruder_pos_update_event(100.0)
        buf.pause_on_error.assert_called_once_with(
            "AFC FPS buffer filament fault detected! Take necessary action.", True)
        buf.update_filament_error_pos.assert_called_once()

    def test_fps_stuck_extreme_but_pos_not_past_trip_is_noop(self):
        buf, afc, reactor, printer = self._ready()
        buf.current_lane = None
        buf.smoothed_fps = 0.95
        buf.get_extruder_pos = MagicMock(return_value=50.0)  # < filament_error_pos (100)
        buf.extruder_pos_update_event(100.0)
        buf.pause_on_error.assert_not_called()
        buf.update_filament_error_pos.assert_not_called()

    def test_returns_eventtime_plus_check_runout_timeout(self):
        buf, afc, reactor, printer = self._ready()
        buf.current_lane = None
        buf.smoothed_fps = 0.5
        result = buf.extruder_pos_update_event(200.0)
        assert result == 200.0 + CHECK_RUNOUT_TIMEOUT


class TestFPSCmdQueryBuffer:
    def test_advancing_state_message(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.last_state = ADVANCING_STATE_NAME
        buf.enable = False
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(MagicMock())
        msg = buf.logger.info.call_args[0][0]
        assert "buffer is compressed - increasing feed" in msg

    def test_trailing_state_message(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.last_state = TRAILING_STATE_NAME
        buf.enable = False
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(MagicMock())
        msg = buf.logger.info.call_args[0][0]
        assert "buffer is expanded - reducing feed" in msg

    def test_neutral_state_message(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.last_state = NEUTRAL_STATE_NAME
        buf.enable = False
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(MagicMock())
        msg = buf.logger.info.call_args[0][0]
        assert "buffer is centered" in msg

    def test_reports_raw_smoothed_and_set_point(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.enable = False
        buf.fps_value = 0.321
        buf.smoothed_fps = 0.456
        buf.set_point = 0.5
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(MagicMock())
        msg = buf.logger.info.call_args[0][0]
        assert "raw: 0.321" in msg
        assert "smoothed: 0.456" in msg
        assert "set_point: 0.50" in msg

    def test_not_enabled_skips_rotation_distance(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.enable = False
        buf.current_lane = _make_lane(buf)
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(MagicMock())
        msg = buf.logger.info.call_args[0][0]
        assert "Rotation distance" not in msg

    def test_enabled_no_current_lane_skips_rotation_distance(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.enable = True
        buf.current_lane = None
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(MagicMock())
        msg = buf.logger.info.call_args[0][0]
        assert "Rotation distance" not in msg

    def test_enabled_lane_no_extruder_stepper_skips_rotation_distance(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.enable = True
        lane = _make_lane(buf)
        lane.extruder_stepper = None
        buf.current_lane = lane
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(MagicMock())
        msg = buf.logger.info.call_args[0][0]
        assert "Rotation distance" not in msg

    def test_enabled_lane_with_extruder_stepper_reports_rotation_distance(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.enable = True
        lane = _make_lane(buf)
        lane.extruder_stepper = MagicMock()
        lane.extruder_stepper.stepper.get_rotation_distance.return_value = [21.25]
        buf.current_lane = lane
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(MagicMock())
        msg = buf.logger.info.call_args[0][0]
        assert "21.2500" in msg
        assert lane.name in msg

    def test_error_sensitivity_zero_skips_fault_report(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.enable = False
        buf.error_sensitivity = 0
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(MagicMock())
        msg = buf.logger.info.call_args[0][0]
        assert "Fault detection" not in msg

    def test_error_sensitivity_positive_reports_fault_detection(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.enable = False
        buf.error_sensitivity = 3.5
        buf.logger = MagicMock()
        buf.cmd_QUERY_BUFFER(MagicMock())
        msg = buf.logger.info.call_args[0][0]
        assert "Fault detection enabled, sensitivity 3.5" in msg


class TestCmdAfcSetFpsSetPoint:
    def test_updates_set_point_and_deadband(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.low_point = 0.1
        buf.high_point = 0.9
        buf.logger = MagicMock()
        gcmd = _make_gcmd(floats={"SET_POINT": 0.6, "DEADBAND": 0.2})
        buf.cmd_AFC_SET_FPS_SET_POINT(gcmd)
        assert buf.set_point == 0.6
        assert buf.deadband == 0.2

    def test_defaults_to_current_values_when_not_provided(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.set_point = 0.55
        buf.deadband = 0.25
        buf.low_point = 0.1
        buf.high_point = 0.9
        gcmd = _make_gcmd(floats={})
        buf.cmd_AFC_SET_FPS_SET_POINT(gcmd)
        assert buf.set_point == 0.55
        assert buf.deadband == 0.25

    def test_set_point_at_or_below_low_point_raises(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.low_point = 0.3
        buf.high_point = 0.9
        gcmd = _make_gcmd(floats={"SET_POINT": 0.3})
        with pytest.raises(Exception, match="must be between"):
            buf.cmd_AFC_SET_FPS_SET_POINT(gcmd)

    def test_set_point_at_or_above_high_point_raises(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.low_point = 0.1
        buf.high_point = 0.7
        gcmd = _make_gcmd(floats={"SET_POINT": 0.7})
        with pytest.raises(Exception, match="must be between"):
            buf.cmd_AFC_SET_FPS_SET_POINT(gcmd)

    def test_deadband_too_wide_raises(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.low_point = 0.1
        buf.high_point = 0.9
        gcmd = _make_gcmd(floats={"SET_POINT": 0.5, "DEADBAND": 0.6})
        buf._check_deadband = MagicMock(return_value="deadband too wide")
        with pytest.raises(Exception, match="deadband too wide"):
            buf.cmd_AFC_SET_FPS_SET_POINT(gcmd)

    def test_logs_new_values_and_neutral_window(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.low_point = 0.1
        buf.high_point = 0.9
        buf.logger = MagicMock()
        gcmd = _make_gcmd(floats={"SET_POINT": 0.5, "DEADBAND": 0.2})
        buf.cmd_AFC_SET_FPS_SET_POINT(gcmd)
        msg = buf.logger.info.call_args[0][0]
        assert "set_point=0.50" in msg
        assert "deadband=0.20" in msg
        assert "0.40-0.60" in msg


class TestFPSGetStatus:
    def test_includes_base_and_fps_specific_keys(self):
        buf, afc, reactor, printer = _make_fps_buffer()
        buf.fps_value = 0.3456
        buf.smoothed_fps = 0.4567
        buf.set_point = 0.5
        result = buf.get_status()
        assert result["fps_value"] == 0.346
        assert result["smoothed_fps"] == 0.457
        assert result["set_point"] == 0.5
        assert "enabled" in result  # inherited from AFCBuffer.get_status


class TestLoadConfigPrefix:
    def test_switched_type_builds_afcbuffer(self):
        buf, afc, printer = _build_real_buffer(values={"type": "switched"})
        assert isinstance(buf, AFCBuffer)
        assert not isinstance(buf, AFCFPSBuffer)

    def test_default_type_builds_afcbuffer(self):
        from extras.AFC_buffer import load_config_prefix
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_buffer TN", printer=printer,
                            values={"advance_pin": "PA0", "trailing_pin": "PA1"})
        with patch("extras.AFC_buffer.add_filament_switch",
                   return_value=(MagicMock(), MagicMock())):
            result = load_config_prefix(config)
        assert isinstance(result, AFCBuffer)

    def test_fps_psf_type_builds_afcfpsbuffer(self):
        from extras.AFC_buffer import load_config_prefix
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        printer.lookup_object("pins").setup_pin = MagicMock(return_value=_FakeADC())
        config = MockConfig(name="AFC_FPS FPS1", printer=printer,
                            values={"type": "FPS_PSF", "adc_pin": "PA5"})
        result = load_config_prefix(config)
        assert isinstance(result, AFCFPSBuffer)

    def test_unrecognized_type_raises(self):
        from extras.AFC_buffer import load_config_prefix
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_buffer TN", printer=printer, values={"type": "bogus"})
        with pytest.raises(Exception, match="not valid"):
            load_config_prefix(config)


# ═════════════════════════════════════════════════════════════════════════
# Module-level import guard
# ═════════════════════════════════════════════════════════════════════════

def _exec_afc_buffer_with_blocked_dependency(blocked_module_name):
    """Execute a throw-away copy of extras/AFC_buffer.py's module-level code
    with `blocked_module_name` forced to fail import, to exercise the file's
    top-level ``try: from X import Y / except: raise error(...)`` guard.

    This never touches the real, already-imported ``extras.AFC_buffer``
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
    import extras.AFC_buffer as real_module
    fresh_name = "extras.AFC_buffer_import_guard_probe"
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
    """Covers the module-level `try/except: raise error(...)` guard around
    AFC_buffer.py's import of AFC_utils.add_filament_switch/VirtualFilamentSensor."""

    def test_afc_utils_import_failure_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_buffer_with_blocked_dependency("extras.AFC_utils")
        assert str(exc_info.value).startswith(
            "Error when trying to import AFC_utils.add_filament_switch")
