"""
Unit tests for extras/AFC_OAMS.py

Covers:
  - OAMSStatus / OAMSOpCode: enum constants and _oams_enum_name helper
  - RetryState: init / reset
  - AFC_OAMS.__init__: config wiring, AMSHardwareService attach + failure
  - _register_mcu_response: register_response / serial / legacy / error branches
  - _resolve_lane_name: with/without hardware_service
  - get_status / stats / is_bay_ready / is_bay_loaded / get_spool_status: bounds
  - handle_connect: command lookup, cancel-cmd fallback, general failure
  - handle_ready: clears action state
  - clear_errors / set_led_error / determine_current_spool
  - register_commands: no-gcode / with-gcode branches
  - cmd_OAMS_RETRY_STATUS / cmd_OAMS_RESET_RETRY_COUNTS
  - load_spool_with_retry / unload_spool_with_retry: success, retry, and
    failure-with-auto-unload branches
  - get_last_load_attempt_time / get_last_successful_load_time / last_load_was_retry
  - load_spool_cancel
  - cmd_OAMS_CURRENT_PID_SET / cmd_OAMS_PID_SET / cmd_OAMS_PID_AUTOTUNE
  - cmd_OAMS_CALIBRATE_HUB_HES / cmd_OAMS_CALIBRATE_PTFE_LENGTH
  - load_spool / unload_spool: all result-code branches, stall + timeout
  - cmd_OAMS_LOAD_SPOOL / cmd_OAMS_UNLOAD_SPOOL / cmd_OAMS_ABORT_ACTION
  - set_oams_follower / abort_current_action / cmd_OAMS_FOLLOWER
  - _oams_cmd_stats / _oams_cmd_current_status / get_current / _oams_action_status
  - float_to_u32 / u32_to_float round trip
  - _build_config
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch, call
import pytest

# AFC_OAMS.py does `import mcu` at module scope. `mcu` is a Klipper-core
# module not vendored in this repo, so stub it before import (mirrors the
# chelper stub pattern used in test_AFC_stepper.py).
_mcu_stub = types.ModuleType("mcu")
_mcu_stub.get_printer_mcu = MagicMock()
sys.modules.setdefault("mcu", _mcu_stub)

from extras.AFC_OAMS import (  # noqa: E402
    AFC_OAMS,
    OAMSStatus,
    OAMSOpCode,
    RetryState,
    _oams_enum_name,
    load_config_prefix,
)
from tests.conftest import MockAFC, MockPrinter, MockReactor, MockLogger, MockConfig


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_oams(oams_idx=0, config_values=None):
    """Build an AFC_OAMS instance via its real constructor.

    The nine hardware command objects are normally created later, in
    ``handle_connect()``; since most tests exercise methods that assume
    that has already happened, they're attached as test doubles here, the
    same way ``handle_connect()`` would populate them from the MCU.
    ``hardware_service`` is reset to ``None`` after construction so each
    test starts from the same isolated baseline the AMSHardwareService-
    specific tests in ``TestInit`` exercise explicitly.
    """
    afc = MockAFC()
    reactor = MockReactor()
    printer = MockPrinter(afc=afc)
    printer._reactor = reactor

    values = {
        "fps_upper_threshold": 0.8,
        "fps_lower_threshold": 0.2,
        "fps_is_reversed": False,
        "f1s_hes_on": "0.1,0.2,0.3,0.4",
        "f1s_hes_is_above": True,
        "hub_hes_on": "0.5,0.6,0.7,0.8",
        "hub_hes_is_above": True,
        "ptfe_length": 1000.0,
        "oams_idx": oams_idx,
    }
    if config_values:
        values.update(config_values)
    config = MockConfig(
        name=f"oams oams{oams_idx}", printer=printer, values=values)

    # Give this instance its own MCU double rather than sharing the return
    # value cached on the module-level `mcu.get_printer_mcu` stub.
    with patch("extras.AFC_OAMS.mcu.get_printer_mcu", MagicMock()):
        oams = AFC_OAMS(config)

    oams.hardware_service = None

    # Command objects normally created in handle_connect()
    oams.oams_load_spool_cmd = MagicMock()
    oams.oams_unload_spool_cmd = MagicMock()
    oams.oams_follower_cmd = MagicMock()
    oams.oams_calibrate_ptfe_length_cmd = MagicMock()
    oams.oams_calibrate_hub_hes_cmd = MagicMock()
    oams.oams_pid_cmd = MagicMock()
    oams.oams_set_led_error_cmd = MagicMock()
    oams.oams_load_spool_cancel_cmd = MagicMock()
    oams.oams_spool_query_spool_cmd = MagicMock()

    return oams


def _make_gcmd(values=None):
    """Minimal gcmd stand-in matching AFC's GCodeCommand-ish interface."""
    values = values or {}
    gcmd = MagicMock()

    def get_float(name, default=None, **kwargs):
        return values.get(name, default)

    def get_int(name, default=None, **kwargs):
        return values.get(name, default)

    gcmd.get_float = MagicMock(side_effect=get_float)
    gcmd.get_int = MagicMock(side_effect=get_int)
    gcmd.error = MagicMock(side_effect=lambda msg: Exception(msg))
    gcmd.respond_info = MagicMock()
    return gcmd


# ── OAMSStatus / OAMSOpCode / _oams_enum_name ────────────────────────────────

class TestEnums:
    def test_oams_status_values(self):
        assert OAMSStatus.LOADING == 0
        assert OAMSStatus.UNLOADING == 1
        assert OAMSStatus.FORWARD_FOLLOWING == 2
        assert OAMSStatus.REVERSE_FOLLOWING == 3
        assert OAMSStatus.COASTING == 4
        assert OAMSStatus.STOPPED == 5
        assert OAMSStatus.CALIBRATING == 6
        assert OAMSStatus.ERROR == 7

    def test_oams_opcode_values(self):
        assert OAMSOpCode.SUCCESS == 0
        assert OAMSOpCode.ERROR_UNSPECIFIED == 1
        assert OAMSOpCode.ERROR_BUSY == 2
        assert OAMSOpCode.SPOOL_ALREADY_IN_BAY == 3
        assert OAMSOpCode.NO_SPOOL_IN_BAY == 4
        assert OAMSOpCode.ERROR_KLIPPER_CALL == 5
        assert OAMSOpCode.CANCEL == 6

    def test_enum_name_known_value(self):
        assert _oams_enum_name(OAMSStatus, 5, "action") == "stopped"

    def test_enum_name_known_value_with_underscore(self):
        assert _oams_enum_name(OAMSOpCode, 4, "code") == "no spool in bay"

    def test_enum_name_unknown_value_falls_back(self):
        assert _oams_enum_name(OAMSStatus, 999, "action") == "action 999"


# ── RetryState ────────────────────────────────────────────────────────────────

class TestRetryState:
    def test_init_defaults(self):
        rs = RetryState()
        assert rs.count == 0
        assert rs.last_attempt is None
        assert rs.was_retry is False

    def test_reset_restores_defaults(self):
        rs = RetryState()
        rs.count = 5
        rs.last_attempt = 123.0
        rs.was_retry = True
        rs.reset()
        assert rs.count == 0
        assert rs.last_attempt is None
        assert rs.was_retry is False


# ── AFC_OAMS.__init__ ─────────────────────────────────────────────────────────

class TestInit:
    def _config(self, values=None):
        base_values = {
            "fps_upper_threshold": 0.8,
            "fps_lower_threshold": 0.2,
            "fps_is_reversed": False,
            "f1s_hes_on": "0.1,0.2,0.3,0.4",
            "f1s_hes_is_above": True,
            "hub_hes_on": "0.5,0.6,0.7,0.8",
            "hub_hes_is_above": True,
            "ptfe_length": 1000.0,
            "oams_idx": 0,
        }
        if values:
            base_values.update(values)
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(
            name="AFC_OAMS oams1", printer=printer, values=base_values
        )
        return config, printer, afc

    def test_sets_name_and_section_name(self):
        config, printer, afc = self._config()
        with patch("extras.AFC_OAMS.AMSHardwareService") as mock_service_cls:
            mock_service_cls.for_printer.return_value = MagicMock()
            oams = AFC_OAMS(config)
        assert oams.name == "oams1"
        assert oams.section_name == "oams1"

    def test_parses_f1s_and_hub_thresholds(self):
        config, printer, afc = self._config()
        with patch("extras.AFC_OAMS.AMSHardwareService") as mock_service_cls:
            mock_service_cls.for_printer.return_value = MagicMock()
            oams = AFC_OAMS(config)
        assert oams.f1s_hes_on == [0.1, 0.2, 0.3, 0.4]
        assert oams.hub_hes_on == [0.5, 0.6, 0.7, 0.8]

    def test_default_pid_gains(self):
        config, printer, afc = self._config()
        with patch("extras.AFC_OAMS.AMSHardwareService") as mock_service_cls:
            mock_service_cls.for_printer.return_value = MagicMock()
            oams = AFC_OAMS(config)
        assert oams.kp == 6.0
        assert oams.ki == 0.0
        assert oams.kd == 0.0
        assert oams.current_kp == 0.375

    def test_default_retry_config(self):
        config, printer, afc = self._config()
        with patch("extras.AFC_OAMS.AMSHardwareService") as mock_service_cls:
            mock_service_cls.for_printer.return_value = MagicMock()
            oams = AFC_OAMS(config)
        assert oams.load_retry_max == 3
        assert oams.unload_retry_max == 2
        assert oams.retry_delay == 3.0
        assert oams.auto_unload_on_failed_load is True

    def test_registers_mcu_responses_and_config_callback(self):
        config, printer, afc = self._config()
        with patch("extras.AFC_OAMS.AMSHardwareService") as mock_service_cls:
            mock_service_cls.for_printer.return_value = MagicMock()
            oams = AFC_OAMS(config)
        registered_messages = [c[0][1] for c in oams.mcu.register_response.call_args_list]
        assert "oams_action_status" in registered_messages
        assert "oams_cmd_stats" in registered_messages
        assert "oams_cmd_current_status" in registered_messages
        oams.mcu.register_config_callback.assert_any_call(oams._build_config)

    def test_calls_register_commands_during_construction(self):
        config, printer, afc = self._config()
        printer._gcode = MagicMock()
        with patch("extras.AFC_OAMS.AMSHardwareService") as mock_service_cls:
            mock_service_cls.for_printer.return_value = MagicMock()
            oams = AFC_OAMS(config)
        assert printer._gcode.register_mux_command.call_count == 11
        assert oams.gcode is printer._gcode

    def test_default_runtime_state(self):
        config, printer, afc = self._config()
        with patch("extras.AFC_OAMS.AMSHardwareService") as mock_service_cls:
            mock_service_cls.for_printer.return_value = MagicMock()
            oams = AFC_OAMS(config)
        assert oams.current_spool is None
        assert oams.encoder_clicks == 0
        assert oams.i_value == 0.0
        assert oams.action_status is None
        assert oams.action_status_code is None
        assert oams.action_status_value is None
        assert oams.load_stall_grace == 3.0
        assert oams.load_stall_dwell == 5.0
        assert oams._load_retry_state == {}
        assert oams._unload_retry_count == 0
        assert oams._last_unload_attempt == 0.0
        assert oams._last_successful_load == {}
        assert oams._load_retry_failures == 0
        assert oams._unload_retry_failures == 0
        assert oams._last_load_failure_time is None
        assert oams._last_unload_failure_time is None

    def test_attaches_hardware_service_on_success(self):
        config, printer, afc = self._config()
        service = MagicMock()
        with patch("extras.AFC_OAMS.AMSHardwareService") as mock_service_cls:
            mock_service_cls.for_printer.return_value = service
            oams = AFC_OAMS(config)
        service.attach_controller.assert_called_once_with(oams)
        assert oams.hardware_service is service

    def test_hardware_service_failure_is_logged_not_raised(self):
        config, printer, afc = self._config()
        with patch("extras.AFC_OAMS.AMSHardwareService") as mock_service_cls:
            mock_service_cls.for_printer.side_effect = Exception("boom")
            oams = AFC_OAMS(config)  # should not raise
        assert oams.hardware_service is None
        assert (
            "error",
            "Failed to register OAMS controller with AMSHardwareService: boom",
        ) in afc.logger.messages

    def test_hardware_service_class_unavailable_skips_registration(self):
        """When AMSHardwareService failed to import (module attr is None,
        as in the try/except ImportError guard), __init__ must skip the
        registration block entirely rather than erroring."""
        config, printer, afc = self._config()
        with patch("extras.AFC_OAMS.AMSHardwareService", None):
            oams = AFC_OAMS(config)
        assert oams.hardware_service is None

    def test_registers_klippy_connect_and_ready_handlers(self):
        config, printer, afc = self._config()
        with patch("extras.AFC_OAMS.AMSHardwareService") as mock_service_cls:
            mock_service_cls.for_printer.return_value = MagicMock()
            oams = AFC_OAMS(config)
        assert oams.handle_connect in printer._event_handlers["klippy:connect"]
        assert oams.handle_ready in printer._event_handlers["klippy:ready"]


# ── _register_mcu_response ────────────────────────────────────────────────────

class TestRegisterMcuResponse:
    def test_uses_modern_register_response_when_available(self):
        oams = _make_oams()
        oams.mcu = MagicMock(spec=["register_response"])
        cb = MagicMock()
        oams._register_mcu_response(cb, "some_msg", oid=5)
        oams.mcu.register_response.assert_called_once_with(cb, "some_msg", 5)

    def test_falls_back_to_serial_register_response(self):
        oams = _make_oams()
        oams.mcu = MagicMock(spec=["_serial"])
        oams.mcu._serial = MagicMock(spec=["register_response"])
        cb = MagicMock()
        oams._register_mcu_response(cb, "some_msg")
        oams.mcu._serial.register_response.assert_called_once_with(cb, "some_msg", None)

    def test_falls_back_to_legacy_register_serial_response(self):
        oams = _make_oams()
        oams.mcu = MagicMock(spec=["register_serial_response"])
        cb = MagicMock()
        oams._register_mcu_response(cb, "some_msg")
        oams.mcu.register_serial_response.assert_called_once_with(cb, "some_msg", None)

    def test_raises_config_error_when_unsupported(self):
        from configfile import error as KlipperError
        oams = _make_oams()
        oams.mcu = MagicMock(spec=["get_name"])
        oams.mcu.get_name.return_value = "mcu"
        oams.printer.config_error = KlipperError
        cb = MagicMock()
        with pytest.raises(KlipperError):
            oams._register_mcu_response(cb, "some_msg")


# ── _resolve_lane_name ────────────────────────────────────────────────────────

class TestResolveLaneName:
    def test_no_hardware_service_returns_none(self):
        oams = _make_oams()
        oams.hardware_service = None
        assert oams._resolve_lane_name(0) is None

    def test_resolves_via_hardware_service(self):
        oams = _make_oams()
        service = MagicMock()
        service.resolve_lane_for_spool_with_afc.return_value = "lane1"
        oams.hardware_service = service
        result = oams._resolve_lane_name(2)
        assert result == "lane1"
        service.resolve_lane_for_spool_with_afc.assert_called_once_with("oams0", 2)


# ── get_status / stats ────────────────────────────────────────────────────────

class TestGetStatus:
    def test_returns_expected_keys_and_values(self):
        oams = _make_oams()
        oams.current_spool = 1
        oams.f1s_hes_value = [1, 0, 0, 0]
        oams.hub_hes_value = [0, 1, 0, 0]
        oams.fps_value = 0.42
        oams._load_retry_failures = 2
        oams._unload_retry_failures = 1
        oams._last_load_failure_time = 10.0
        oams._last_unload_failure_time = 20.0

        status = oams.get_status(0.0)

        assert status["current_spool"] == 1
        assert status["f1s_hes_value"] == [1, 0, 0, 0]
        assert status["hub_hes_value"] == [0, 1, 0, 0]
        assert status["fps_value"] == 0.42
        assert status["load_retry_failures"] == 2
        assert status["unload_retry_failures"] == 1
        assert status["last_load_failure_time"] == 10.0
        assert status["last_unload_failure_time"] == 20.0

    def test_get_status_returns_copy_of_lists(self):
        oams = _make_oams()
        status = oams.get_status(0.0)
        status["f1s_hes_value"].append(99)
        assert 99 not in oams.f1s_hes_value


class TestStats:
    def test_stats_returns_false_and_formatted_string(self):
        oams = _make_oams()
        oams.current_spool = 2
        oams.fps_value = 0.5
        oams.f1s_hes_value = [1, 0, 1, 0]
        oams.hub_hes_value = [0, 1, 0, 1]
        oams.encoder_clicks = 42
        oams.i_value = 0.33

        active, message = oams.stats(0.0)

        assert active is False
        assert "OAMS[0]" in message
        assert "current_spool=2" in message
        assert "encoder_clicks=42" in message


# ── is_bay_ready / is_bay_loaded / get_spool_status ──────────────────────────

class TestBayHelpers:
    def test_is_bay_ready_true(self):
        oams = _make_oams()
        oams.f1s_hes_value = [1, 0, 0, 0]
        assert oams.is_bay_ready(0) is True

    def test_is_bay_ready_false(self):
        oams = _make_oams()
        oams.f1s_hes_value = [0, 0, 0, 0]
        assert oams.is_bay_ready(0) is False

    def test_is_bay_ready_out_of_range_logs_and_returns_false(self):
        oams = _make_oams()
        result = oams.is_bay_ready(10)
        assert result is False
        assert ("error", "Invalid bay_index 10, must be 0-3") in oams.logger.messages

    def test_is_bay_loaded_true(self):
        oams = _make_oams()
        oams.hub_hes_value = [0, 1, 0, 0]
        assert oams.is_bay_loaded(1) is True

    def test_is_bay_loaded_out_of_range(self):
        oams = _make_oams()
        assert oams.is_bay_loaded(-1) is False
        assert ("error", "Invalid bay_index -1, must be 0-3") in oams.logger.messages

    def test_get_spool_status_in_range(self):
        oams = _make_oams()
        oams.f1s_hes_value = [1, 2, 3, 4]
        assert oams.get_spool_status(2) == 3

    def test_get_spool_status_out_of_range_returns_zero(self):
        oams = _make_oams()
        assert oams.get_spool_status(5) == 0
        assert ("error", "Invalid bay_index 5, must be 0-3") in oams.logger.messages


# ── handle_connect ────────────────────────────────────────────────────────────

class TestHandleConnect:
    def test_looks_up_all_commands_and_clears_errors(self):
        oams = _make_oams()
        cmd_obj = MagicMock()
        query_cmd_obj = MagicMock()
        oams.mcu.lookup_command = MagicMock(return_value=cmd_obj)
        oams.mcu.alloc_command_queue = MagicMock(return_value=MagicMock())
        oams.mcu.lookup_query_command = MagicMock(return_value=query_cmd_obj)
        oams.clear_errors = MagicMock()

        oams.handle_connect()

        assert oams.oams_load_spool_cmd is cmd_obj
        assert oams.oams_unload_spool_cmd is cmd_obj
        assert oams.oams_load_spool_cancel_cmd is cmd_obj
        assert oams.oams_spool_query_spool_cmd is query_cmd_obj
        oams.clear_errors.assert_called_once()

    def test_cancel_command_missing_sets_cancel_cmd_none(self):
        oams = _make_oams()

        def lookup_command(cmd_string):
            if cmd_string == "oams_cmd_load_spool_cancel":
                raise Exception("not supported")
            return MagicMock()

        oams.mcu.lookup_command = MagicMock(side_effect=lookup_command)
        oams.mcu.alloc_command_queue = MagicMock(return_value=MagicMock())
        oams.mcu.lookup_query_command = MagicMock(return_value=MagicMock())
        oams.clear_errors = MagicMock()

        oams.handle_connect()

        assert oams.oams_load_spool_cancel_cmd is None
        warning_msgs = [m for lvl, m in oams.logger.messages if lvl == "warning"]
        msg = (
            f"Failed to initialize OAMS load filament cancel command: not supported\n"
            "Most likely the firmware needs to be updated to support this command."
        )
        assert any(msg in m for m in warning_msgs)
        oams.clear_errors.assert_called_once()

    def test_general_failure_is_logged_not_raised(self):
        oams = _make_oams()
        oams.mcu.lookup_command = MagicMock(side_effect=Exception("mcu offline"))

        oams.handle_connect()  # must not raise

        assert ("error", "Failed to initialize OAMS commands: mcu offline") in oams.logger.messages


# ── handle_ready ──────────────────────────────────────────────────────────────

class TestHandleReady:
    def test_clears_action_state(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams.action_status_code = OAMSOpCode.SUCCESS
        oams.action_status_value = 1.0

        oams.handle_ready()

        assert oams.action_status is None
        assert oams.action_status_code is None
        assert oams.action_status_value is None
        assert (
            "info", "OAMS[0]: Cleared software error states on ready"
        ) in oams.logger.messages


# ── clear_errors / set_led_error / determine_current_spool ──────────────────

class TestClearErrors:
    def test_clears_led_for_all_four_bays(self):
        oams = _make_oams()
        oams.set_led_error = MagicMock()
        oams.determine_current_spool = MagicMock(return_value=None)

        oams.clear_errors()

        assert oams.set_led_error.call_count == 4
        oams.set_led_error.assert_any_call(0, 0)
        oams.set_led_error.assert_any_call(3, 0)

    def test_led_failure_for_one_bay_does_not_stop_others(self):
        oams = _make_oams()
        oams.set_led_error = MagicMock(side_effect=[None, Exception("fail"), None, None])
        oams.determine_current_spool = MagicMock(return_value=None)

        oams.clear_errors()

        assert oams.set_led_error.call_count == 4
        assert (
            "error", "Failed to clear LED error for bay 1 on oams0: fail"
        ) in oams.logger.messages

    def test_sets_current_spool_from_determine(self):
        oams = _make_oams()
        oams.set_led_error = MagicMock()
        oams.determine_current_spool = MagicMock(return_value=2)

        oams.clear_errors()

        assert oams.current_spool == 2

    def test_determine_current_spool_failure_is_logged(self):
        oams = _make_oams()
        oams.set_led_error = MagicMock()
        oams.determine_current_spool = MagicMock(side_effect=Exception("query failed"))

        oams.clear_errors()  # must not raise

        assert (
            "error",
            "Failed to determine current spool during clear_errors on oams0: query failed",
        ) in oams.logger.messages

    def test_clears_action_status_fields(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams.action_status_code = OAMSOpCode.SUCCESS
        oams.action_status_value = 1
        oams.set_led_error = MagicMock()
        oams.determine_current_spool = MagicMock(return_value=None)

        oams.clear_errors()

        assert oams.action_status is None
        assert oams.action_status_code is None
        assert oams.action_status_value is None


class TestSetLedError:
    def test_sends_led_command(self):
        oams = _make_oams()
        oams.set_led_error(2, 1)
        oams.oams_set_led_error_cmd.send.assert_called_once_with([2, 1])
        assert ("debug", "Setting LED 2 to 1") in oams.logger.messages


class TestDetermineCurrentSpool:
    def test_no_response_returns_none(self):
        oams = _make_oams()
        oams.oams_spool_query_spool_cmd.send = MagicMock(return_value=None)
        assert oams.determine_current_spool() is None
        assert (
            "warning",
            "OAMS[0]: Failed to query current spool - no response from MCU",
        ) in oams.logger.messages

    def test_missing_spool_field_returns_none(self):
        oams = _make_oams()
        oams.oams_spool_query_spool_cmd.send = MagicMock(return_value={})
        assert oams.determine_current_spool() is None
        assert (
            "warning", "OAMS[0]: Spool query response missing 'spool' field"
        ) in oams.logger.messages

    def test_valid_spool_index_returned(self):
        oams = _make_oams()
        oams.oams_spool_query_spool_cmd.send = MagicMock(return_value={"spool": 2})
        assert oams.determine_current_spool() == 2

    def test_255_means_no_spool(self):
        oams = _make_oams()
        oams.oams_spool_query_spool_cmd.send = MagicMock(return_value={"spool": 255})
        assert oams.determine_current_spool() is None
        assert (
            "debug", "OAMS[0]: No spool loaded (hardware returned 255)"
        ) in oams.logger.messages

    def test_unexpected_value_logs_warning_and_returns_none(self):
        oams = _make_oams()
        oams.oams_spool_query_spool_cmd.send = MagicMock(return_value={"spool": 99})
        result = oams.determine_current_spool()
        assert result is None
        assert (
            "warning",
            "OAMS[0]: Unexpected spool index 99 from hardware "
            "(expected 0-3 or 255); treating as no spool loaded",
        ) in oams.logger.messages


# ── register_commands ─────────────────────────────────────────────────────────

class TestRegisterCommands:
    def test_registers_expected_commands_on_the_gcode_object_set_at_init(self):
        oams = _make_oams()
        gcode = MagicMock()
        oams.gcode = gcode
        oams.register_commands("oams1")
        assert gcode.register_mux_command.call_count == 11
        registered_names = [c[0][0] for c in gcode.register_mux_command.call_args_list]
        assert "OAMS_LOAD_SPOOL" in registered_names
        assert "OAMS_RESET_RETRY_COUNTS" in registered_names


# ── cmd_OAMS_RETRY_STATUS / cmd_OAMS_RESET_RETRY_COUNTS ──────────────────────

class TestRetryStatusCommand:
    def test_no_active_retries_message(self):
        oams = _make_oams()
        gcmd = _make_gcmd()
        oams.cmd_OAMS_RETRY_STATUS(gcmd)
        msg = gcmd.respond_info.call_args[0][0]
        assert "No active load retries" in msg

    def test_active_retries_listed(self):
        oams = _make_oams()
        retry = RetryState()
        retry.count = 1
        oams._load_retry_state = {0: retry}
        gcmd = _make_gcmd()
        oams.cmd_OAMS_RETRY_STATUS(gcmd)
        msg = gcmd.respond_info.call_args[0][0]
        assert "Load retry counts:" in msg
        assert "Spool 0: 1/3" in msg


class TestResetRetryCountsCommand:
    def test_clears_all_retry_state(self):
        oams = _make_oams()
        oams._load_retry_state = {0: RetryState()}
        oams._unload_retry_count = 2
        oams._last_unload_attempt = 5.0
        oams._last_successful_load = {0: 1.0}
        gcmd = _make_gcmd()

        oams.cmd_OAMS_RESET_RETRY_COUNTS(gcmd)

        assert oams._load_retry_state == {}
        assert oams._unload_retry_count == 0
        assert oams._last_unload_attempt == 0.0
        assert oams._last_successful_load == {}
        gcmd.respond_info.assert_called_once()


# ── _calculate_retry_delay ────────────────────────────────────────────────────

class TestCalculateRetryDelay:
    def test_returns_configured_delay_regardless_of_attempt(self):
        oams = _make_oams()
        oams.retry_delay = 4.5
        assert oams._calculate_retry_delay(0) == 4.5
        assert oams._calculate_retry_delay(3) == 4.5


# ── load_spool_with_retry ──────────────────────────────────────────────────────

class TestLoadSpoolWithRetry:
    def test_success_first_attempt(self):
        oams = _make_oams()
        oams.load_spool = MagicMock(return_value=(OAMSOpCode.SUCCESS, "Spool loaded successfully"))
        success, message = oams.load_spool_with_retry(0)
        assert success is True
        assert message == "Spool loaded successfully"
        assert 0 not in oams._load_retry_state
        assert 0 in oams._last_successful_load

    def test_cancel_code_counts_as_success(self):
        oams = _make_oams()
        oams.load_spool = MagicMock(return_value=(OAMSOpCode.CANCEL, "Spool loading cancelled"))
        success, message = oams.load_spool_with_retry(0)
        assert success is True

    def test_success_after_one_retry_marks_was_retry(self):
        oams = _make_oams()
        oams.abort_current_action = MagicMock()
        oams.reactor.pause = MagicMock()
        oams.unload_spool_with_retry = MagicMock(return_value=(True, "unloaded"))
        oams.load_spool = MagicMock(
            side_effect=[
                (OAMSOpCode.ERROR_UNSPECIFIED, "fail"),
                (OAMSOpCode.SUCCESS, "ok"),
            ]
        )
        success, message = oams.load_spool_with_retry(1, max_retries=3)
        assert success is True
        assert oams.last_load_was_retry(1) is False  # state reset after success
        assert oams.load_spool.call_count == 2
        oams.abort_current_action.assert_called_once()
        # Both the pre-retry delay pause and the post-abort pause fire.
        assert oams.reactor.pause.call_count == 2
        assert any(
            lvl == "warning" and "Attempt 1/3" in m for lvl, m in oams.logger.messages)
        assert any(
            lvl == "info" and "Load retry 2/3" in m for lvl, m in oams.logger.messages)
        assert any(
            lvl == "info" and "Successfully loaded" in m and "attempt 2" in m
            for lvl, m in oams.logger.messages)

    def test_success_after_one_retry_uses_resolved_lane_name(self):
        """When hardware_service resolves a real lane name, the retry-wait
        and success log messages should use 'lane <name>' rather than the
        'lane (spool N)' fallback used when no lane is bound."""
        oams = _make_oams()
        service = MagicMock()
        service.resolve_lane_for_spool_with_afc.return_value = "lane1"
        oams.hardware_service = service
        oams.abort_current_action = MagicMock()
        oams.reactor.pause = MagicMock()
        oams.unload_spool_with_retry = MagicMock(return_value=(True, "unloaded"))
        oams.load_spool = MagicMock(
            side_effect=[
                (OAMSOpCode.ERROR_UNSPECIFIED, "fail"),
                (OAMSOpCode.SUCCESS, "ok"),
            ]
        )
        success, message = oams.load_spool_with_retry(1, max_retries=3)
        assert success is True
        assert any(
            lvl == "info" and "Load retry 2/3 for lane lane1" in m
            for lvl, m in oams.logger.messages)
        assert any(
            lvl == "info" and "Successfully loaded lane lane1 on attempt 2" in m
            for lvl, m in oams.logger.messages)
        # No fallback "(spool N)" wording leaked through.
        assert not any("(spool" in m for _lvl, m in oams.logger.messages)

    def test_all_attempts_fail_uses_resolved_lane_name(self):
        """Mid-loop warning and final failure message should also use the
        resolved lane name instead of the 'lane (spool N)' fallback."""
        oams = _make_oams()
        service = MagicMock()
        service.resolve_lane_for_spool_with_afc.return_value = "lane1"
        oams.hardware_service = service
        oams.auto_unload_on_failed_load = False
        oams.abort_current_action = MagicMock()
        oams.load_spool = MagicMock(return_value=(OAMSOpCode.ERROR_UNSPECIFIED, "nope"))

        success, message = oams.load_spool_with_retry(0, max_retries=2)

        assert success is False
        assert "Failed to load lane lane1 after 2 attempts" in message
        assert any(
            lvl == "warning" and "Load failed for lane lane1" in m
            for lvl, m in oams.logger.messages)
        assert not any("(spool" in m for _lvl, m in oams.logger.messages)
        assert "(spool" not in message

    def test_auto_unload_failure_aborts_load(self):
        oams = _make_oams()
        oams.auto_unload_on_failed_load = True
        oams.abort_current_action = MagicMock()
        oams.load_spool = MagicMock(return_value=(OAMSOpCode.ERROR_UNSPECIFIED, "fail"))
        oams.unload_spool_with_retry = MagicMock(return_value=(False, "unload failed"))

        success, message = oams.load_spool_with_retry(0, max_retries=3)

        assert success is False
        assert "Failed to unload" in message
        assert oams._load_retry_failures == 1
        assert oams._last_load_failure_time is not None
        assert (
            "info", "OAMS[0]: Auto-unloading before retry"
        ) in oams.logger.messages
        assert (
            "error", "OAMS[0]: Failed to unload before retry: unload failed"
        ) in oams.logger.messages
        assert 0 not in oams._load_retry_state

    def test_all_attempts_fail_returns_history(self):
        oams = _make_oams()
        oams.auto_unload_on_failed_load = False
        oams.abort_current_action = MagicMock()
        oams.load_spool = MagicMock(return_value=(OAMSOpCode.ERROR_UNSPECIFIED, "nope"))

        success, message = oams.load_spool_with_retry(0, max_retries=2)

        assert success is False
        assert "after 2 attempts" in message
        assert "Attempt 1: nope" in message  # attempt_history entries surface in the message
        assert oams._load_retry_failures == 1
        assert 0 not in oams._load_retry_state
        assert oams._last_load_failure_time is not None
        assert any(
            lvl == "warning" and "Attempt 1/2" in m for lvl, m in oams.logger.messages)

    def test_no_auto_unload_skips_unload_call(self):
        oams = _make_oams()
        oams.auto_unload_on_failed_load = False
        oams.abort_current_action = MagicMock()
        oams.unload_spool_with_retry = MagicMock()
        oams.load_spool = MagicMock(return_value=(OAMSOpCode.ERROR_UNSPECIFIED, "nope"))

        oams.load_spool_with_retry(0, max_retries=2)

        oams.unload_spool_with_retry.assert_not_called()

    def test_zero_max_retries_falls_back_to_configured_max(self):
        """max_retries=0 (falsy/non-positive) now falls back to the
        configured load_retry_max instead of leaving retry_limit at 0,
        which previously caused an UnboundLocalError."""
        oams = _make_oams()
        oams.load_retry_max = 4
        oams.load_spool = MagicMock(return_value=(OAMSOpCode.ERROR_UNSPECIFIED, "busy"))
        oams.unload_spool_with_retry = MagicMock(return_value=(True, "unloaded"))

        success, message = oams.load_spool_with_retry(0, max_retries=0)

        assert success is False
        assert oams.load_spool.call_count == 4  # used load_retry_max, not 0
        assert "after 4 attempts" in message

    def test_negative_max_retries_falls_back_to_configured_max(self):
        oams = _make_oams()
        oams.load_retry_max = 2
        oams.load_spool = MagicMock(return_value=(OAMSOpCode.ERROR_UNSPECIFIED, "busy"))
        oams.unload_spool_with_retry = MagicMock(return_value=(True, "unloaded"))

        success, message = oams.load_spool_with_retry(0, max_retries=-1)

        assert success is False
        assert oams.load_spool.call_count == 2


    def test_get_last_load_attempt_time_none_when_untracked(self):
        oams = _make_oams()
        assert oams.get_last_load_attempt_time(3) is None

    def test_get_last_load_attempt_time_returns_value(self):
        oams = _make_oams()
        retry = RetryState()
        retry.last_attempt = 99.0
        oams._load_retry_state[0] = retry
        assert oams.get_last_load_attempt_time(0) == 99.0

    def test_get_last_successful_load_time_none_when_untracked(self):
        oams = _make_oams()
        assert oams.get_last_successful_load_time(0) is None

    def test_get_last_successful_load_time_returns_value(self):
        oams = _make_oams()
        oams._last_successful_load[0] = 42.0
        assert oams.get_last_successful_load_time(0) == 42.0

    def test_last_load_was_retry_false_when_untracked(self):
        oams = _make_oams()
        assert oams.last_load_was_retry(0) is False

    def test_last_load_was_retry_true(self):
        oams = _make_oams()
        retry = RetryState()
        retry.was_retry = True
        oams._load_retry_state[0] = retry
        assert oams.last_load_was_retry(0) is True


# ── unload_spool_with_retry ────────────────────────────────────────────────────

class TestUnloadSpoolWithRetry:
    def test_success_first_attempt(self):
        oams = _make_oams()
        oams.unload_spool = MagicMock(return_value=(True, "Spool unloaded successfully"))
        success, message = oams.unload_spool_with_retry()
        assert success is True
        assert oams._unload_retry_count == 0
        assert oams._last_unload_attempt == 0.0
        assert any(
            lvl == "info" and "Successfully unloaded" in m and "attempt 1" in m
            for lvl, m in oams.logger.messages)

    def test_success_after_retry_retracts_extruder(self):
        oams = _make_oams()
        oams.abort_current_action = MagicMock()
        oams.reactor.pause = MagicMock()
        oams.unload_spool = MagicMock(
            side_effect=[(False, "busy"), (True, "unloaded")]
        )
        gcode = MagicMock()
        oams.gcode = gcode

        success, message = oams.unload_spool_with_retry(max_retries=3)

        assert success is True
        assert gcode.run_script_from_command.call_count == 4  # M83, G92, G1, M400
        oams.abort_current_action.assert_called_once()
        assert oams.reactor.pause.call_count == 2  # pre-retry delay + post-abort
        assert any(
            lvl == "info" and "Unload retry 2/3" in m for lvl, m in oams.logger.messages)
        assert any(
            lvl == "warning" and "Attempt 1/3" in m for lvl, m in oams.logger.messages)
        assert any(
            lvl == "info" and "Successfully unloaded" in m and "attempt 2" in m
            for lvl, m in oams.logger.messages)

    def test_success_uses_resolved_lane_name_when_current_spool_set(self):
        """current_spool not None + a hardware_service that resolves a lane
        name should produce 'lane <name>' in the success message, instead of
        the 'filament' fallback used when there's no bound lane."""
        oams = _make_oams()
        oams.current_spool = 2
        service = MagicMock()
        service.resolve_lane_for_spool_with_afc.return_value = "lane2"
        oams.hardware_service = service
        oams.unload_spool = MagicMock(return_value=(True, "Spool unloaded successfully"))

        success, message = oams.unload_spool_with_retry()

        assert success is True
        assert any(
            lvl == "info" and "Successfully unloaded lane lane2 on attempt 1" in m
            for lvl, m in oams.logger.messages)
        service.resolve_lane_for_spool_with_afc.assert_called_once_with("oams0", 2)

    def test_retract_failure_is_logged_but_retry_continues(self):
        oams = _make_oams()
        oams.abort_current_action = MagicMock()
        oams.unload_spool = MagicMock(
            side_effect=[(False, "busy"), (True, "unloaded")]
        )
        gcode = MagicMock()
        gcode.run_script_from_command.side_effect = Exception("no extruder")
        oams.gcode = gcode

        success, message = oams.unload_spool_with_retry(max_retries=3)

        assert success is True
        assert (
            "warning",
            "OAMS[0]: Failed to retract extruder before unload retry: no extruder",
        ) in oams.logger.messages

    def test_all_attempts_fail(self):
        oams = _make_oams()
        oams.abort_current_action = MagicMock()
        captured_attempt_times = []

        def fake_unload():
            captured_attempt_times.append(oams._last_unload_attempt)
            return (False, "busy")
        oams.unload_spool = MagicMock(side_effect=fake_unload)

        success, message = oams.unload_spool_with_retry(max_retries=2)

        assert success is False
        assert "after 2 attempts" in message
        assert oams._unload_retry_failures == 1
        assert oams._last_unload_failure_time is not None
        # _last_unload_attempt is stamped with reactor.monotonic() before
        # every attempt, so both captured attempts should show the (fixed
        # mock) reactor time rather than the 0.0 default.
        assert captured_attempt_times == [100.0, 100.0]
        assert "Attempt 1: busy" in message  # attempt_history entries surface in the message
        assert any(
            lvl == "warning" and "Attempt 1/2" in m for lvl, m in oams.logger.messages)
        assert oams._unload_retry_count == 0  # reset even on total failure

    def test_zero_max_retries_falls_back_to_configured_max(self):
        """max_retries=0 (falsy/non-positive) now falls back to the
        configured unload_retry_max instead of leaving retry_limit at 0,
        which previously caused an UnboundLocalError."""
        oams = _make_oams()
        oams.unload_retry_max = 3
        oams.abort_current_action = MagicMock()
        oams.unload_spool = MagicMock(return_value=(False, "busy"))

        success, message = oams.unload_spool_with_retry(max_retries=0)

        assert success is False
        assert oams.unload_spool.call_count == 3  # used unload_retry_max, not 0
        assert "after 3 attempts" in message

    def test_negative_max_retries_falls_back_to_configured_max(self):
        oams = _make_oams()
        oams.unload_retry_max = 2
        oams.abort_current_action = MagicMock()
        oams.unload_spool = MagicMock(return_value=(False, "busy"))

        success, message = oams.unload_spool_with_retry(max_retries=-5)

        assert success is False
        assert oams.unload_spool.call_count == 2

# ── load_spool_cancel ─────────────────────────────────────────────────────────

class TestLoadSpoolCancel:
    def test_sends_cancel_when_available(self):
        oams = _make_oams()
        message = oams.load_spool_cancel()
        oams.oams_load_spool_cancel_cmd.send.assert_called_once()
        assert message == "OAMS load spool operation cancelled"

    def test_returns_message_when_unavailable(self):
        oams = _make_oams()
        oams.oams_load_spool_cancel_cmd = None
        message = oams.load_spool_cancel()
        assert "not available" in message


# ── cmd_OAMS_CURRENT_PID_SET / cmd_OAMS_PID_SET ──────────────────────────────

class TestCurrentPidSetCommand:
    def test_missing_p_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"I": 1.0, "D": 1.0})
        with pytest.raises(Exception):
            oams.cmd_OAMS_CURRENT_PID_SET(gcmd)

    def test_missing_i_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"P": 1.0, "D": 1.0})
        with pytest.raises(Exception):
            oams.cmd_OAMS_CURRENT_PID_SET(gcmd)

    def test_missing_d_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"P": 1.0, "I": 1.0})
        with pytest.raises(Exception):
            oams.cmd_OAMS_CURRENT_PID_SET(gcmd)

    def test_default_target_used_when_not_provided(self):
        oams = _make_oams()
        oams.current_target = 0.35
        gcmd = _make_gcmd({"P": 1.0, "I": 2.0, "D": 3.0})
        oams.cmd_OAMS_CURRENT_PID_SET(gcmd)
        assert oams.current_target == 0.35
        assert oams.current_kp == 1.0
        assert oams.current_ki == 2.0
        assert oams.current_kd == 3.0
        gcmd.respond_info.assert_called_once_with(
            "Current PID values set to P=1.000000 I=2.000000 D=3.000000 TARGET=0.350000")

    def test_explicit_target_overrides_default(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"P": 1.0, "I": 2.0, "D": 3.0, "TARGET": 0.2})
        oams.cmd_OAMS_CURRENT_PID_SET(gcmd)
        assert oams.current_target == 0.2
        oams.oams_pid_cmd.send.assert_called_once()


class TestPidSetCommand:
    def test_missing_p_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({})
        with pytest.raises(Exception):
            oams.cmd_OAMS_PID_SET(gcmd)

    def test_missing_i_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"P": 4.0})
        with pytest.raises(Exception):
            oams.cmd_OAMS_PID_SET(gcmd)

    def test_missing_d_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"P": 4.0, "I": 0.1})
        with pytest.raises(Exception):
            oams.cmd_OAMS_PID_SET(gcmd)

    def test_sets_pressure_pid_and_default_target(self):
        oams = _make_oams()
        oams.fps_target = 0.6
        gcmd = _make_gcmd({"P": 4.0, "I": 0.1, "D": 0.2})
        oams.cmd_OAMS_PID_SET(gcmd)
        assert oams.kp == 4.0
        assert oams.ki == 0.1
        assert oams.kd == 0.2
        assert oams.fps_target == 0.6
        oams.oams_pid_cmd.send.assert_called_once()
        gcmd.respond_info.assert_called_once_with(
            "PID values set to P=4.000000 I=0.100000 D=0.200000 TARGET=0.600000")

    def test_explicit_target_used(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"P": 4.0, "I": 0.1, "D": 0.2, "TARGET": 0.7})
        oams.cmd_OAMS_PID_SET(gcmd)
        assert oams.fps_target == 0.7


class TestPidAutotuneCommand:
    def test_missing_target_flow_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"TARGET_TEMP": 200.0})
        with pytest.raises(Exception):
            oams.cmd_OAMS_PID_AUTOTUNE(gcmd)

    def test_missing_target_temp_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"TARGET_FLOW": 5.0})
        with pytest.raises(Exception):
            oams.cmd_OAMS_PID_AUTOTUNE(gcmd)

    def test_success_runs_heat_and_extrude(self):
        oams = _make_oams()
        gcode = MagicMock()
        oams.gcode = gcode
        gcmd = _make_gcmd({"TARGET_FLOW": 5.0, "TARGET_TEMP": 210.0})
        oams.cmd_OAMS_PID_AUTOTUNE(gcmd)
        calls = [c[0][0] for c in gcode.run_script_from_command.call_args_list]
        assert any("M104 S210" in c for c in calls)
        assert any(c.startswith("G1 E") for c in calls)


# ── cmd_OAMS_CALIBRATE_HUB_HES / cmd_OAMS_CALIBRATE_PTFE_LENGTH ─────────────

class TestCalibrateHubHesCommand:
    def test_missing_spool_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({})
        with pytest.raises(Exception):
            oams.cmd_OAMS_CALIBRATE_HUB_HES(gcmd)

    def test_out_of_range_spool_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"SPOOL": 4})
        with pytest.raises(Exception):
            oams.cmd_OAMS_CALIBRATE_HUB_HES(gcmd)

    def test_success_saves_calibration(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.CALIBRATING
        oams.action_status_code = OAMSOpCode.SUCCESS
        oams.action_status_value = oams.float_to_u32(0.55)
        gcmd = _make_gcmd({"SPOOL": 1})

        def send(args):
            oams.action_status = None  # simulate firmware completion
        oams.oams_calibrate_hub_hes_cmd.send = MagicMock(side_effect=send)

        oams.cmd_OAMS_CALIBRATE_HUB_HES(gcmd)

        oams.oams_calibrate_hub_hes_cmd.send.assert_called_once_with([1])
        assert oams.hub_hes_on[1] == pytest.approx(0.55, rel=1e-4)
        gcmd.respond_info.assert_any_call(
            "Calibrated HES 1 to 0.550000 threshold")
        gcmd.respond_info.assert_any_call(
            "HES calibration complete: hub_hes_on index 1 = 0.550000 saved to config")
        oams.afc.function.ConfigRewrite.assert_called_once()

    def test_failure_calls_gcmd_error(self):
        oams = _make_oams()
        oams.action_status_code = OAMSOpCode.ERROR_UNSPECIFIED

        def send(args):
            oams.action_status = None
        oams.oams_calibrate_hub_hes_cmd.send = MagicMock(side_effect=send)
        gcmd = _make_gcmd({"SPOOL": 0})

        with pytest.raises(Exception):
            oams.cmd_OAMS_CALIBRATE_HUB_HES(gcmd)

        gcmd.error.assert_called_once_with("Calibration of HES 0 failed")

    def test_wait_loop_pauses_until_action_completes(self):
        oams = _make_oams()
        oams.action_status_code = OAMSOpCode.SUCCESS
        oams.action_status_value = oams.float_to_u32(0.4)
        # action_status stays set through the send() call; pause() clears it
        # on its first invocation so the loop body actually runs at least once.
        oams.reactor.pause = MagicMock(side_effect=lambda t: setattr(oams, "action_status", None))
        gcmd = _make_gcmd({"SPOOL": 0})

        oams.cmd_OAMS_CALIBRATE_HUB_HES(gcmd)

        oams.reactor.pause.assert_called_once()
        assert oams.hub_hes_on[0] == pytest.approx(0.4, rel=1e-4)


class TestCalibratePtfeLengthCommand:
    def test_missing_spool_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({})
        with pytest.raises(Exception):
            oams.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)

    def test_success_saves_calibration(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.CALIBRATING
        oams.action_status_code = OAMSOpCode.SUCCESS
        oams.action_status_value = 850

        def send(args):
            oams.action_status = None
        oams.oams_calibrate_ptfe_length_cmd.send = MagicMock(side_effect=send)
        gcmd = _make_gcmd({"SPOOL": 0})

        oams.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)

        oams.oams_calibrate_ptfe_length_cmd.send.assert_called_once_with([0])
        oams.afc.function.ConfigRewrite.assert_called_once()
        gcmd.respond_info.assert_any_call("Calibrated PTFE length to 850")
        gcmd.respond_info.assert_any_call(
            "PTFE calibration complete: ptfe_length 850 saved to config")

    def test_failure_calls_gcmd_error(self):
        oams = _make_oams()
        oams.action_status_code = OAMSOpCode.ERROR_UNSPECIFIED

        def send(args):
            oams.action_status = None
        oams.oams_calibrate_ptfe_length_cmd.send = MagicMock(side_effect=send)
        gcmd = _make_gcmd({"SPOOL": 0})

        with pytest.raises(Exception):
            oams.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)

        gcmd.error.assert_called_once_with("Calibration of PTFE length failed")

    def test_wait_loop_pauses_until_action_completes(self):
        oams = _make_oams()
        oams.action_status_code = OAMSOpCode.SUCCESS
        oams.action_status_value = 900
        oams.reactor.pause = MagicMock(side_effect=lambda t: setattr(oams, "action_status", None))
        gcmd = _make_gcmd({"SPOOL": 0})

        oams.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)

        oams.reactor.pause.assert_called_once()


# ── load_spool ────────────────────────────────────────────────────────────────

class TestLoadSpool:
    def test_success_sets_current_spool(self):
        oams = _make_oams()
        oams.reactor._monotonic = 100.0

        def send(args):
            oams.action_status = None
            oams.action_status_code = OAMSOpCode.SUCCESS
        oams.oams_load_spool_cmd.send = MagicMock(side_effect=send)

        code, message = oams.load_spool(2)

        assert code == OAMSOpCode.SUCCESS
        assert oams.current_spool == 2
        assert message == "Spool loaded successfully"
        oams.oams_load_spool_cmd.send.assert_called_once_with([2])

    def test_error_klipper_call(self):
        oams = _make_oams()

        def send(args):
            oams.action_status = None
            oams.action_status_code = OAMSOpCode.ERROR_KLIPPER_CALL
        oams.oams_load_spool_cmd.send = MagicMock(side_effect=send)

        code, message = oams.load_spool(0)
        assert code == OAMSOpCode.ERROR_KLIPPER_CALL
        assert "klipper monitor" in message

    def test_error_busy(self):
        oams = _make_oams()

        def send(args):
            oams.action_status = None
            oams.action_status_code = OAMSOpCode.ERROR_BUSY
        oams.oams_load_spool_cmd.send = MagicMock(side_effect=send)

        code, message = oams.load_spool(0)
        assert code == OAMSOpCode.ERROR_BUSY
        assert "busy" in message

    def test_cancel(self):
        oams = _make_oams()

        def send(args):
            oams.action_status = None
            oams.action_status_code = OAMSOpCode.CANCEL
        oams.oams_load_spool_cmd.send = MagicMock(side_effect=send)

        code, message = oams.load_spool(0)
        assert code == OAMSOpCode.CANCEL
        assert "cancelled" in message

    def test_unknown_code(self):
        oams = _make_oams()

        def send(args):
            oams.action_status = None
            oams.action_status_code = 42
        oams.oams_load_spool_cmd.send = MagicMock(side_effect=send)

        code, message = oams.load_spool(0)
        assert code == 42
        assert "Unknown error" in message

    def test_timeout_cancels_and_returns_error(self):
        oams = _make_oams()
        oams.load_stall_dwell = 0.0  # disable stall detection for this test
        times = iter([0.0, 0.0, 46.0, 46.0])

        def monotonic():
            return next(times, 46.0)
        oams.reactor.monotonic = monotonic
        oams.load_spool_cancel = MagicMock()

        code, message = oams.load_spool(0)

        assert code == OAMSOpCode.ERROR_UNSPECIFIED
        assert "timed out" in message
        oams.load_spool_cancel.assert_called_once()
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.ERROR_UNSPECIFIED
        assert (
            "error", "OAMS[0]: Load operation timed out after 45 seconds"
        ) in oams.logger.messages

    def test_stall_detected_cancels_and_returns_error(self):
        oams = _make_oams()
        oams.load_stall_dwell = 5.0
        oams.load_stall_grace = 1.0
        # start=0; loop time progresses past grace+dwell without encoder movement
        times = iter([0.0, 0.0, 7.0, 7.0])

        def monotonic():
            return next(times, 7.0)
        oams.reactor.monotonic = monotonic
        oams.load_spool_cancel = MagicMock()

        code, message = oams.load_spool(0)

        assert code == OAMSOpCode.ERROR_UNSPECIFIED
        assert "stalled" in message
        oams.load_spool_cancel.assert_called_once()
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.ERROR_UNSPECIFIED
        assert (
            "error",
            "OAMS[0]: Load stalled - encoder stopped advancing for 5s (spool stuck)",
        ) in oams.logger.messages

    def test_encoder_movement_resets_stall_timer_then_succeeds(self):
        oams = _make_oams()
        oams.load_stall_dwell = 5.0
        oams.load_stall_grace = 1.0
        oams.encoder_clicks = 0
        # monotonic(): start=0.0, then 1.0 (iter 1 "now"), 2.0 (iter 2 "now")
        times = iter([0.0, 1.0, 2.0])

        def monotonic():
            return next(times, 2.0)
        oams.reactor.monotonic = monotonic

        pause_calls = {"n": 0}

        def pause(t):
            pause_calls["n"] += 1
            if pause_calls["n"] == 1:
                # Simulate the encoder advancing between the first and
                # second loop iterations.
                oams.encoder_clicks = 5
            else:
                oams.action_status = None
                oams.action_status_code = OAMSOpCode.SUCCESS
        oams.reactor.pause = pause

        code, message = oams.load_spool(0)

        assert code == OAMSOpCode.SUCCESS
        assert pause_calls["n"] == 2

    def test_stall_cancel_failure_is_logged_as_warning(self):
        oams = _make_oams()
        oams.load_stall_dwell = 5.0
        oams.load_stall_grace = 1.0
        times = iter([0.0, 0.0, 7.0, 7.0])

        def monotonic():
            return next(times, 7.0)
        oams.reactor.monotonic = monotonic
        oams.load_spool_cancel = MagicMock(side_effect=Exception("cancel failed"))

        code, message = oams.load_spool(0)

        assert code == OAMSOpCode.ERROR_UNSPECIFIED
        assert (
            "warning", "OAMS[0]: Failed to cancel stalled load: cancel failed"
        ) in oams.logger.messages

    def test_timeout_cancel_failure_is_logged_as_warning(self):
        oams = _make_oams()
        oams.load_stall_dwell = 0.0
        times = iter([0.0, 0.0, 46.0, 46.0])

        def monotonic():
            return next(times, 46.0)
        oams.reactor.monotonic = monotonic
        oams.load_spool_cancel = MagicMock(side_effect=Exception("cancel failed"))

        code, message = oams.load_spool(0)

        assert code == OAMSOpCode.ERROR_UNSPECIFIED
        assert "timed out" in message
        assert (
            "warning",
            "OAMS[0]: Failed to cancel stuck load after timeout: cancel failed",
        ) in oams.logger.messages


# ── cmd_OAMS_LOAD_SPOOL ───────────────────────────────────────────────────────

class TestLoadSpoolCommand:
    def test_missing_spool_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({})
        with pytest.raises(Exception):
            oams.cmd_OAMS_LOAD_SPOOL(gcmd)

    def test_out_of_range_spool_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"SPOOL": 5})
        with pytest.raises(Exception):
            oams.cmd_OAMS_LOAD_SPOOL(gcmd)

    def test_success_quiet_suppresses_message(self):
        oams = _make_oams()
        oams.load_spool_with_retry = MagicMock(return_value=(True, "ok"))
        gcmd = _make_gcmd({"SPOOL": 0, "QUIET": 1})
        oams.cmd_OAMS_LOAD_SPOOL(gcmd)
        gcmd.respond_info.assert_not_called()

    def test_success_not_quiet_responds(self):
        oams = _make_oams()
        oams.load_spool_with_retry = MagicMock(return_value=(True, "ok"))
        gcmd = _make_gcmd({"SPOOL": 0, "QUIET": 0})
        oams.cmd_OAMS_LOAD_SPOOL(gcmd)
        gcmd.respond_info.assert_called_once_with("ok")
        assert oams.action_status == OAMSStatus.LOADING

    def test_failure_calls_gcmd_error(self):
        oams = _make_oams()
        oams.load_spool_with_retry = MagicMock(return_value=(False, "nope"))
        gcmd = _make_gcmd({"SPOOL": 0})
        with pytest.raises(Exception):
            oams.cmd_OAMS_LOAD_SPOOL(gcmd)
        gcmd.error.assert_called_once_with("nope")


# ── unload_spool ──────────────────────────────────────────────────────────────

class TestUnloadSpool:
    def test_success_clears_current_spool(self):
        oams = _make_oams()
        oams.current_spool = 1

        def send():
            oams.action_status = None
            oams.action_status_code = OAMSOpCode.SUCCESS
        oams.oams_unload_spool_cmd.send = MagicMock(side_effect=send)

        success, message = oams.unload_spool()
        assert success is True
        assert oams.current_spool is None
        oams.oams_unload_spool_cmd.send.assert_called_once_with()

    def test_no_spool_in_bay_treated_as_success(self):
        oams = _make_oams()
        oams.current_spool = 1

        def send():
            oams.action_status = None
            oams.action_status_code = OAMSOpCode.NO_SPOOL_IN_BAY
        oams.oams_unload_spool_cmd.send = MagicMock(side_effect=send)

        success, message = oams.unload_spool()
        assert success is True
        assert oams.current_spool is None
        assert "already unloaded" in message

    def test_error_klipper_call(self):
        oams = _make_oams()

        def send():
            oams.action_status = None
            oams.action_status_code = OAMSOpCode.ERROR_KLIPPER_CALL
        oams.oams_unload_spool_cmd.send = MagicMock(side_effect=send)

        success, message = oams.unload_spool()
        assert success is False

    def test_error_busy(self):
        oams = _make_oams()

        def send():
            oams.action_status = None
            oams.action_status_code = OAMSOpCode.ERROR_BUSY
        oams.oams_unload_spool_cmd.send = MagicMock(side_effect=send)

        success, message = oams.unload_spool()
        assert success is False
        assert "busy" in message

    def test_cancel(self):
        oams = _make_oams()

        def send():
            oams.action_status = None
            oams.action_status_code = OAMSOpCode.CANCEL
        oams.oams_unload_spool_cmd.send = MagicMock(side_effect=send)

        success, message = oams.unload_spool()
        assert success is False
        assert "cancelled" in message

    def test_unknown_code(self):
        oams = _make_oams()

        def send():
            oams.action_status = None
            oams.action_status_code = 123
        oams.oams_unload_spool_cmd.send = MagicMock(side_effect=send)

        success, message = oams.unload_spool()
        assert success is False
        assert "Unknown error" in message

    def test_timeout_returns_error(self):
        oams = _make_oams()
        times = iter([0.0, 41.0, 41.0])

        def monotonic():
            return next(times, 41.0)
        oams.reactor.monotonic = monotonic

        success, message = oams.unload_spool()
        assert success is False
        assert "timed out" in message
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.ERROR_UNSPECIFIED
        assert (
            "error", "OAMS[0]: Unload operation timed out after 40 seconds"
        ) in oams.logger.messages

    def test_loop_body_pauses_before_success(self):
        oams = _make_oams()
        # timeout check false on first pass, then action completes on pause.
        times = iter([0.0, 1.0])

        def monotonic():
            return next(times, 1.0)
        oams.reactor.monotonic = monotonic

        def pause(t):
            oams.action_status = None
            oams.action_status_code = OAMSOpCode.SUCCESS
        oams.reactor.pause = MagicMock(side_effect=pause)

        success, message = oams.unload_spool()

        assert success is True
        oams.reactor.pause.assert_called_once()


class TestUnloadSpoolCommand:
    def test_success_responds(self):
        oams = _make_oams()
        oams.unload_spool_with_retry = MagicMock(return_value=(True, "unloaded"))
        gcmd = _make_gcmd()
        oams.cmd_OAMS_UNLOAD_SPOOL(gcmd)
        gcmd.respond_info.assert_called_once_with("unloaded")

    def test_failure_errors(self):
        oams = _make_oams()
        oams.unload_spool_with_retry = MagicMock(return_value=(False, "failed"))
        gcmd = _make_gcmd()
        with pytest.raises(Exception):
            oams.cmd_OAMS_UNLOAD_SPOOL(gcmd)
        gcmd.error.assert_called_once_with("failed")

    def test_passes_max_retries_override(self):
        oams = _make_oams()
        oams.unload_spool_with_retry = MagicMock(return_value=(True, "ok"))
        gcmd = _make_gcmd({"MAX_RETRIES": 5})
        oams.cmd_OAMS_UNLOAD_SPOOL(gcmd)
        oams.unload_spool_with_retry.assert_called_once_with(max_retries=5)


# ── cmd_OAMS_ABORT_ACTION ─────────────────────────────────────────────────────

class TestAbortActionCommand:
    def test_default_code_and_wait(self):
        oams = _make_oams()
        oams.abort_current_action = MagicMock()
        gcmd = _make_gcmd({})
        oams.cmd_OAMS_ABORT_ACTION(gcmd)
        oams.abort_current_action.assert_called_once_with(
            code=OAMSOpCode.ERROR_KLIPPER_CALL, wait=True
        )

    def test_custom_code_and_no_wait(self):
        oams = _make_oams()
        oams.abort_current_action = MagicMock()
        gcmd = _make_gcmd({"CODE": OAMSOpCode.ERROR_BUSY, "WAIT": 0})
        oams.cmd_OAMS_ABORT_ACTION(gcmd)
        oams.abort_current_action.assert_called_once_with(
            code=OAMSOpCode.ERROR_BUSY, wait=False
        )


# ── set_oams_follower / abort_current_action ─────────────────────────────────

class TestSetOamsFollower:
    def test_sends_enable_and_direction(self):
        oams = _make_oams()
        oams.set_oams_follower(1, 0)
        oams.oams_follower_cmd.send.assert_called_once_with([1, 0])


class TestAbortCurrentAction:
    def test_no_action_returns_immediately(self):
        oams = _make_oams()
        oams.action_status = None
        oams.load_spool_cancel = MagicMock()
        oams.abort_current_action()
        oams.load_spool_cancel.assert_not_called()

    def test_wait_true_clears_when_action_completes(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams.action_status_value = 123  # seed nonzero so the reset below is proven
        oams.load_spool_cancel = MagicMock()
        calls = {"n": 0}

        def pause(t):
            calls["n"] += 1
            if calls["n"] >= 1:
                oams.action_status = None

        oams.reactor.pause = pause
        oams.abort_current_action(code=OAMSOpCode.ERROR_KLIPPER_CALL, wait=True)

        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.ERROR_KLIPPER_CALL
        assert oams.action_status_value is None
        oams.load_spool_cancel.assert_called_once()
        assert any(
            lvl == "debug" and "Aborting current action" in m
            for lvl, m in oams.logger.messages)
        assert ("info", "OAMS[0]: Abort complete") in oams.logger.messages

    def test_wait_true_forces_clear_on_timeout(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams.load_spool_cancel = MagicMock()
        times = iter([0.0] + [10.0] * 20)

        def monotonic():
            return next(times, 10.0)
        oams.reactor.monotonic = monotonic
        oams.reactor.pause = MagicMock()

        oams.abort_current_action(wait=True)

        assert oams.action_status is None
        assert (
            "debug", "OAMS[0]: Abort timeout - forcing clear"
        ) in oams.logger.messages

    def test_wait_false_clears_immediately(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.UNLOADING
        oams.action_status_value = 456  # seed nonzero so the reset below is proven
        oams.load_spool_cancel = MagicMock()
        oams.abort_current_action(code=OAMSOpCode.ERROR_BUSY, wait=False)
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.ERROR_BUSY
        assert oams.action_status_value is None
        assert (
            "debug", "OAMS[0]: Abort without waiting - status cleared"
        ) in oams.logger.messages

    def test_cancel_failure_is_logged_not_raised(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams.load_spool_cancel = MagicMock(side_effect=Exception("no cancel cmd"))
        oams.abort_current_action(wait=False)  # must not raise
        assert (
            "warning",
            "OAMS[0]: Failed to send firmware cancel during abort: no cancel cmd",
        ) in oams.logger.messages


# ── cmd_OAMS_FOLLOWER ──────────────────────────────────────────────────────────

class TestFollowerCommand:
    def test_missing_enable_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"DIRECTION": 1})
        with pytest.raises(Exception):
            oams.cmd_OAMS_FOLLOWER(gcmd)

    def test_missing_direction_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({"ENABLE": 1})
        with pytest.raises(Exception):
            oams.cmd_OAMS_FOLLOWER(gcmd)

    def test_enable_reverse_message(self):
        oams = _make_oams()
        oams.set_oams_follower = MagicMock()
        gcmd = _make_gcmd({"ENABLE": 1, "DIRECTION": 0})
        oams.cmd_OAMS_FOLLOWER(gcmd)
        gcmd.respond_info.assert_called_once_with("Follower enable in reverse direction")

    def test_enable_forward_message(self):
        oams = _make_oams()
        oams.set_oams_follower = MagicMock()
        gcmd = _make_gcmd({"ENABLE": 1, "DIRECTION": 1})
        oams.cmd_OAMS_FOLLOWER(gcmd)
        gcmd.respond_info.assert_called_once_with("Follower enable in forward direction")

    def test_disable_message(self):
        oams = _make_oams()
        oams.set_oams_follower = MagicMock()
        gcmd = _make_gcmd({"ENABLE": 0, "DIRECTION": 1})
        oams.cmd_OAMS_FOLLOWER(gcmd)
        gcmd.respond_info.assert_called_once_with("Follower disabled")

    def test_unrecognized_enable_value_sends_command_without_response(self):
        oams = _make_oams()
        oams.set_oams_follower = MagicMock()
        gcmd = _make_gcmd({"ENABLE": 2, "DIRECTION": 1})
        oams.cmd_OAMS_FOLLOWER(gcmd)
        oams.set_oams_follower.assert_called_once_with(2, 1)
        gcmd.respond_info.assert_not_called()


class TestStopOamsFollowerCommand:
    def test_disables_follower_via_oams_follower_command(self):
        oams = _make_oams(oams_idx=1)
        gcode = MagicMock()
        oams.gcode = gcode
        gcmd = _make_gcmd()

        oams.cmd_AFC_STOP_OAMS_FOLLOWER(gcmd)

        gcode.run_script_from_command.assert_called_once_with(
            "OAMS_FOLLOWER OAMS=1 ENABLE=0 DIRECTION=1")

    def test_uses_this_units_oams_idx(self):
        oams = _make_oams(oams_idx=2)
        gcode = MagicMock()
        oams.gcode = gcode
        gcmd = _make_gcmd()

        oams.cmd_AFC_STOP_OAMS_FOLLOWER(gcmd)

        sent = gcode.run_script_from_command.call_args[0][0]
        assert "OAMS=2" in sent
        assert "OAMS=1" not in sent


class TestStartOamsFollowerCommand:
    def test_enables_follower_forward_via_oams_follower_command(self):
        oams = _make_oams(oams_idx=1)
        gcode = MagicMock()
        oams.gcode = gcode
        gcmd = _make_gcmd()

        oams.cmd_AFC_START_OAMS_FOLLOWER(gcmd)

        gcode.run_script_from_command.assert_called_once_with(
            "OAMS_FOLLOWER OAMS=1 ENABLE=1 DIRECTION=1")

    def test_uses_this_units_oams_idx(self):
        oams = _make_oams(oams_idx=2)
        gcode = MagicMock()
        oams.gcode = gcode
        gcmd = _make_gcmd()

        oams.cmd_AFC_START_OAMS_FOLLOWER(gcmd)

        sent = gcode.run_script_from_command.call_args[0][0]
        assert "OAMS=2" in sent
        assert "OAMS=1" not in sent


class TestRegisterCommandsIncludesFollowerMacros:
    def test_registers_stop_and_start_follower_macros(self):
        oams = _make_oams(oams_idx=1)
        oams.gcode = MagicMock()

        oams.register_commands("oams1")

        registered_names = [
            c.args[1] if len(c.args) > 1 else None
            for c in oams.afc.function.register_mux_command.call_args_list
        ]
        assert "AFC_STOP_OAMS_FOLLOWER" in registered_names
        assert "AFC_START_OAMS_FOLLOWER" in registered_names


# ── _oams_cmd_stats / _oams_cmd_current_status / get_current ────────────────

class TestOamsCmdStats:
    def test_updates_all_sensor_arrays(self):
        oams = _make_oams()
        params = {
            "fps_value": oams.float_to_u32(0.75),
            # Every index differs from the [0, 0, 0, 0] pre-existing default
            # so a missing per-index assignment is actually observable.
            "f1s_hes_value_0": 1, "f1s_hes_value_1": 1,
            "f1s_hes_value_2": 1, "f1s_hes_value_3": 1,
            "hub_hes_value_0": 1, "hub_hes_value_1": 1,
            "hub_hes_value_2": 1, "hub_hes_value_3": 1,
            "encoder_clicks": 500,
        }
        oams._oams_cmd_stats(params)
        assert oams.fps_value == pytest.approx(0.75)
        assert oams.f1s_hes_value == [1, 1, 1, 1]
        assert oams.hub_hes_value == [1, 1, 1, 1]
        assert oams.encoder_clicks == 500


class TestOamsCmdCurrentStatus:
    def test_sets_i_value(self):
        oams = _make_oams()
        params = {"current_value": oams.float_to_u32(0.29)}
        oams._oams_cmd_current_status(params)
        assert oams.i_value == pytest.approx(0.29)


class TestGetCurrent:
    def test_returns_i_value(self):
        oams = _make_oams()
        oams.i_value = 0.44
        assert oams.get_current() == 0.44


# ── _oams_action_status ───────────────────────────────────────────────────────

class TestOamsActionStatus:
    def test_loading_action_clears_status_and_sets_code(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams._oams_action_status({"action": OAMSStatus.LOADING, "code": OAMSOpCode.SUCCESS})
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.SUCCESS
        assert ("debug", "OAMS status received") in oams.logger.messages

    def test_unloading_action_clears_status(self):
        oams = _make_oams()
        oams._oams_action_status({"action": OAMSStatus.UNLOADING, "code": OAMSOpCode.ERROR_BUSY})
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.ERROR_BUSY

    def test_error_action_clears_status(self):
        oams = _make_oams()
        oams._oams_action_status({"action": OAMSStatus.ERROR, "code": OAMSOpCode.ERROR_UNSPECIFIED})
        assert oams.action_status is None

    def test_calibrating_action_captures_value(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.CALIBRATING  # seed so the clear-to-None below is proven
        oams._oams_action_status(
            {"action": OAMSStatus.CALIBRATING, "code": OAMSOpCode.SUCCESS, "value": 12345}
        )
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.SUCCESS
        assert oams.action_status_value == 12345

    def test_error_klipper_call_code_clears_status_for_other_actions(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.COASTING  # seed so the clear-to-None below is proven
        # action is a "non-action" status but code is ERROR_KLIPPER_CALL
        oams._oams_action_status(
            {"action": OAMSStatus.COASTING, "code": OAMSOpCode.ERROR_KLIPPER_CALL}
        )
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.ERROR_KLIPPER_CALL

    def test_follower_status_logged_as_debug_only(self):
        oams = _make_oams()
        oams.action_status = "unchanged"
        oams._oams_action_status(
            {"action": OAMSStatus.FORWARD_FOLLOWING, "code": OAMSOpCode.SUCCESS}
        )
        # non-action status: action_status untouched
        assert oams.action_status == "unchanged"
        assert (
            "debug",
            "OAMS status update (non-action): forward following (success)",
        ) in oams.logger.messages

    def test_unhandled_status_logged_as_debug(self):
        oams = _make_oams()
        oams.action_status = "unchanged"
        oams._oams_action_status({"action": 999, "code": OAMSOpCode.SUCCESS})
        assert oams.action_status == "unchanged"
        assert (
            "debug", "OAMS status update (unhandled): action 999 (success)"
        ) in oams.logger.messages


# ── float_to_u32 / u32_to_float ───────────────────────────────────────────────

class TestFloatU32RoundTrip:
    def test_round_trip_preserves_value(self):
        oams = _make_oams()
        for value in (0.0, 1.0, -1.0, 0.5, 123.456, -99.9):
            u32 = oams.float_to_u32(value)
            assert oams.u32_to_float(u32) == pytest.approx(value)


# ── _build_config ─────────────────────────────────────────────────────────────

class TestBuildConfig:
    def test_sends_all_expected_config_commands(self):
        oams = _make_oams()
        oams.mcu.add_config_cmd = MagicMock()
        oams._build_config()
        cmd_names = [c[0][0].split()[0] for c in oams.mcu.add_config_cmd.call_args_list]
        assert "config_oams_buffer" in cmd_names
        assert "config_oams_f1s_hes" in cmd_names
        assert "config_oams_hub_hes" in cmd_names
        assert "config_oams_pid" in cmd_names
        assert "config_oams_ptfe" in cmd_names
        assert "config_oams_current_pid" in cmd_names
        assert "config_oams_logger" in cmd_names

    def test_logger_index_matches_oams_idx(self):
        oams = _make_oams(oams_idx=3)
        oams.mcu.add_config_cmd = MagicMock()
        oams._build_config()
        logger_call = [
            c[0][0] for c in oams.mcu.add_config_cmd.call_args_list
            if c[0][0].startswith("config_oams_logger")
        ][0]
        assert logger_call == "config_oams_logger idx=3"


# ── load_config_prefix ────────────────────────────────────────────────────

class TestLoadConfigPrefix:
    def test_constructs_afc_oams_instance(self):
        with patch("extras.AFC_OAMS.AFC_OAMS") as mock_cls:
            mock_cls.return_value = "instance"
            result = load_config_prefix("cfg")
        mock_cls.assert_called_once_with("cfg")
        assert result == "instance"
