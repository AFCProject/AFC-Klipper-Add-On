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

    The ten hardware command objects are normally created later, in
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
    oams.oams_current_pid_cmd = MagicMock()
    oams.oams_set_led_error_cmd = MagicMock()
    oams.oams_load_spool_cancel_cmd = MagicMock()
    oams.oams_spool_query_spool_cmd = MagicMock()

    return oams


def _make_gcmd(values=None):
    """Minimal gcmd stand-in matching AFC's GCodeCommand-ish interface."""
    from tests.conftest import MockGCodeCommand
    return MockGCodeCommand(params=values or {})


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
        assert printer._gcode.register_mux_command.call_count == 12
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
        assert oams.oams_current_pid_cmd is cmd_obj
        assert oams.oams_spool_query_spool_cmd is query_cmd_obj
        oams.clear_errors.assert_called_once()

    def test_current_pid_command_missing_sets_current_pid_cmd_none(self):
        """Regression test for issue #772/#791: an older firmware missing the
        dedicated current-PID command must not crash handle_connect, and
        must leave oams_current_pid_cmd as None rather than undefined."""
        oams = _make_oams()

        def lookup_command(cmd_string):
            if cmd_string.startswith("oams_cmd_current_pid"):
                raise Exception("not supported")
            return MagicMock()

        oams.mcu.lookup_command = MagicMock(side_effect=lookup_command)
        oams.mcu.alloc_command_queue = MagicMock(return_value=MagicMock())
        oams.mcu.lookup_query_command = MagicMock(return_value=MagicMock())
        oams.clear_errors = MagicMock()

        oams.handle_connect()

        assert oams.oams_current_pid_cmd is None
        warning_msgs = [m for lvl, m in oams.logger.messages if lvl == "warning"]
        msg = (
            "Failed to initialize OAMS current PID command: not supported\n"
            "Most likely the firmware needs to be updated to support this command."
        )
        assert warning_msgs == [msg]
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


class TestRequireMcuCmd:
    """_require_mcu_cmd guards every non-optional MCU command handle against
    handle_connect() never having initialized it (issue #791)."""

    def test_returns_cmd_unchanged_when_present(self):
        oams = _make_oams()
        sentinel = MagicMock()
        assert oams._require_mcu_cmd(sentinel, "some_cmd") is sentinel

    def test_raises_when_cmd_is_none(self):
        """Covers the `cmd is None` guard's True branch."""
        oams = _make_oams()
        with pytest.raises(Exception, match="some_cmd command not initialized"):
            oams._require_mcu_cmd(None, "some_cmd")

    def test_error_message_includes_oams_idx_and_name(self):
        oams = _make_oams(oams_idx=2)
        with pytest.raises(Exception) as exc_info:
            oams._require_mcu_cmd(None, "set_led_error")
        assert str(exc_info.value) == (
            "OAMS[2]: set_led_error command not initialized, "
            "MCU connection may have failed"
        )


class TestSetLedError:
    def test_sends_led_command(self):
        oams = _make_oams()
        oams.set_led_error(2, 1)
        oams.oams_set_led_error_cmd.send.assert_called_once_with([2, 1])
        assert ("debug", "Setting LED 2 to 1") in oams.logger.messages

    def test_raises_when_led_error_cmd_unavailable(self):
        """Covers _require_mcu_cmd's guard for this command specifically."""
        oams = _make_oams()
        oams.oams_set_led_error_cmd = None
        with pytest.raises(Exception):
            oams.set_led_error(2, 1)


class TestDetermineCurrentSpool:
    def test_raises_when_spool_query_cmd_unavailable(self):
        """Covers _require_mcu_cmd's guard for this command specifically."""
        oams = _make_oams()
        oams.oams_spool_query_spool_cmd = None
        with pytest.raises(Exception):
            oams.determine_current_spool()

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
        assert gcode.register_mux_command.call_count == 12
        registered_names = [c[0][0] for c in gcode.register_mux_command.call_args_list]
        assert "OAMS_LOAD_SPOOL" in registered_names
        assert "OAMS_RESET_RETRY_COUNTS" in registered_names
        assert "OAMS_SET_LED_ERROR" in registered_names


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

class TestTheErrorStateIsWhatActuallyStopsTheUnit:
    """
    MEASURED, on a jammed unload with the motor still pulling at 0.54 A four
    minutes after AFC had given up and paused the print:

        OAMS_ABORT_ACTION (firmware cancel)  i unchanged 0.56 -> 0.57
        OAMS_FOLLOWER ENABLE=0               i unchanged 0.49 -> 0.59
        OAMS_LOAD_SPOOL                      queued, never ran
        oams_set_led_error(bay, 1)           i 0.54 -> 0.00 in under 4s

    Only the last stops it. Nothing in the plugin said so -- the firmware is
    not published, the host sees a two-byte "set LED" command, and upstream
    calls it immediately before pausing, which reads as decoration. It is not.
    It also LATCHES: clearing the LED does not restart the motor.
    """

    def test_giving_up_sets_the_bay_error_state(self):
        oams = _make_oams()
        oams.current_spool = 1
        oams.abort_current_action = MagicMock()
        oams.reactor.pause = MagicMock()
        oams.gcode = MagicMock()
        oams.set_led_error = MagicMock()
        oams.unload_spool = MagicMock(
            side_effect=[(False, "OAMS is busy"), (False, "OAMS is busy")])

        success, _ = oams.unload_spool_with_retry(max_retries=2)

        assert success is False
        oams.set_led_error.assert_any_call(1, 1), (
            "giving up must actually stop the unit, not just report failure")

    def test_the_retry_stops_the_unit_so_the_next_attempt_can_land(self):
        # THE POINT OF THE WHOLE THING. The unit keeps driving after a failed
        # attempt returns, so attempt 2 was being sent to a unit mid-unload and
        # came back "OAMS is busy" -- the pair of refusals in the jam report.
        # Stop it, work the extruder, clear the latch, THEN retry.
        oams = _make_oams()
        oams.current_spool = 1
        oams.abort_current_action = MagicMock()
        oams.reactor.pause = MagicMock()
        oams.gcode = MagicMock()
        calls = []
        oams.set_led_error = lambda bay, val: calls.append((bay, val))
        oams.unload_spool = MagicMock(
            side_effect=[(False, "OAMS is busy"), (True, "unloaded")])

        assert oams.unload_spool_with_retry(max_retries=3)[0] is True
        # A fresh unload clears the bay first (see the previous test), so scope
        # this to the retry cycle: the stop, and then a clear AFTER it.
        assert (1, 1) in calls, "the unit was never actually stopped"
        stopped_at = calls.index((1, 1))
        assert (1, 0) in calls[stopped_at:], (
            "the latch must be cleared after the stop, or the retry starts on "
            "an errored unit")

    def test_a_fresh_unload_clears_a_latch_left_by_a_previous_give_up(self):
        # The operator's recovery path: a jam gives up with the bay latched
        # (that latch is what stopped the unit), they clear the filament by
        # hand, then ask for the unload again. That ask must not inherit the
        # flag that stopped the last one.
        oams = _make_oams()
        oams.current_spool = 1
        oams.reactor.pause = MagicMock()
        oams.gcode = MagicMock()
        calls = []
        oams.set_led_error = lambda bay, val: calls.append((bay, val))
        oams.unload_spool = MagicMock(return_value=(True, "unloaded"))
        oams._flag_bay(1)          # the previous give-up latched it

        oams.unload_spool_with_retry()

        assert calls and calls[0] == (1, 0), (
            "the first thing a fresh unload does must be to clear the bay")

    def test_the_bay_is_captured_before_a_success_clears_it(self):
        # unload_spool() sets current_spool to None on success, so the bay has
        # to be remembered from the start or the clear names nothing.
        oams = _make_oams()
        oams.current_spool = 2
        oams.reactor.pause = MagicMock()
        oams.gcode = MagicMock()
        oams.set_led_error = MagicMock()

        def _unload():
            oams.current_spool = None
            return True, "unloaded"
        oams.unload_spool = _unload
        oams._flag_bay(2)

        assert oams.unload_spool_with_retry()[0] is True
        oams.set_led_error.assert_any_call(2, 0)

    def test_a_successful_load_clears_the_bay_the_jam_flagged(self):
        # The operator's other recovery route: clear the jam by hand and load
        # the lane again. An unload cleared the latch; a load did not, so the
        # light stayed on after the problem was fixed.
        oams = _make_oams()
        oams.reactor.pause = MagicMock()
        oams.set_led_error = MagicMock()
        oams.load_spool = MagicMock(return_value=(OAMSOpCode.SUCCESS, "loaded"))
        oams._flag_bay(1)          # the jam that gave up latched it

        assert oams.load_spool_with_retry(1)[0] is True
        oams.set_led_error.assert_any_call(1, 0)

    def test_the_stop_forces_the_edge(self):
        # THE STOP IS THE EDGE, NOT THE STATE. Measured back to back on a unit
        # still pulling at 0.50 A: re-setting an already-latched bay left it
        # running (0.50, 0.50, 0.49); clear-then-set stopped it dead. Without
        # this a SECOND give-up on the same bay cannot stop the unit -- watched
        # live, stop_unit_motion logged success while the motor ran on.
        oams = _make_oams()
        calls = []
        oams.set_led_error = lambda bay, val: calls.append((bay, val))
        assert oams.stop_unit_motion(1) is True
        assert calls == [(1, 0), (1, 1)], (
            "the stop must clear then set, or an already-latched bay is a no-op")

    def test_stop_is_a_no_op_with_no_bay_to_name(self):
        oams = _make_oams()
        oams.current_spool = None
        oams.set_led_error = MagicMock()
        assert oams.stop_unit_motion() is False
        oams.set_led_error.assert_not_called()

    def test_a_failed_stop_does_not_raise(self):
        oams = _make_oams()
        oams.set_led_error = MagicMock(side_effect=Exception("link down"))
        assert oams.stop_unit_motion(1) is False

    def test_clearing_one_bay_leaves_the_others_alone(self):
        # clear_errors() blanks all four and resets the action state; another
        # bay's genuine error is not ours to discard.
        oams = _make_oams()
        oams.set_led_error = MagicMock()
        oams._flag_bay(3)
        oams._clear_bay_error(3)
        oams.set_led_error.assert_called_once_with(3, 0)


class TestWeOnlyClearWhatWeLit:
    """
    The clear runs on the ordinary load and unload paths, so it must not write
    to the bay error state unless we are the ones who set it. Otherwise every
    load and unload pokes a firmware latch we cannot read, and an error raised
    by the firmware itself is wiped by the next success on that bay.

    That last case cannot be tested from here: the host has no way to read the
    error state back, so a firmware-raised error is indistinguishable from no
    error at all. What IS testable is the property that protects it -- we
    write only to bays this set records, and the firmware cannot put a bay in
    that set.
    """

    def test_a_clean_unload_never_touches_the_bay(self):
        oams = _make_oams()
        oams.current_spool = 1
        oams.reactor.pause = MagicMock()
        oams.gcode = MagicMock()
        oams.set_led_error = MagicMock()
        oams.unload_spool = MagicMock(return_value=(True, "unloaded"))

        assert oams.unload_spool_with_retry()[0] is True
        oams.set_led_error.assert_not_called()

    def test_a_clean_load_never_touches_the_bay(self):
        oams = _make_oams()
        oams.reactor.pause = MagicMock()
        oams.set_led_error = MagicMock()
        oams.load_spool = MagicMock(return_value=(OAMSOpCode.SUCCESS, "loaded"))

        assert oams.load_spool_with_retry(1)[0] is True
        oams.set_led_error.assert_not_called()

    def test_the_stop_flags_the_bay_it_stopped(self):
        oams = _make_oams()
        oams.set_led_error = MagicMock()
        assert oams.stop_unit_motion(2) is True
        assert 2 in oams._bay_flags()

    def test_a_failed_stop_flags_nothing(self):
        oams = _make_oams()
        oams.set_led_error = MagicMock(side_effect=Exception("link down"))
        assert oams.stop_unit_motion(2) is False
        assert 2 not in oams._bay_flags()

    def test_the_flag_drops_once_cleared(self):
        oams = _make_oams()
        oams.set_led_error = MagicMock()
        oams._flag_bay(0)
        oams._clear_bay_error(0)
        assert 0 not in oams._bay_flags()
        oams.set_led_error.reset_mock()
        oams._clear_bay_error(0)
        oams.set_led_error.assert_not_called()

    def test_a_failed_clear_keeps_the_flag(self):
        # The LED is still lit if the send threw, so we still owe the clear.
        oams = _make_oams()
        oams._flag_bay(0)
        oams.set_led_error = MagicMock(side_effect=Exception("link down"))
        oams._clear_bay_error(0)
        assert 0 in oams._bay_flags()

    def test_clear_errors_drops_every_flag(self):
        oams = _make_oams()
        oams.set_led_error = MagicMock()
        oams.determine_current_spool = MagicMock(return_value=None)
        oams._flag_bay(0)
        oams._flag_bay(3)
        oams.clear_errors()
        assert oams._bay_flags() == set()

    def test_a_hand_set_error_is_ours_to_clear(self):
        # The operator's workflow: stop a bay by hand, free the jam, unload.
        # That set went through the plugin, so the unload that succeeds turns
        # the light back off.
        oams = _make_oams()
        oams.set_led_error = MagicMock()
        gcmd = MagicMock()
        gcmd.get_int.side_effect = lambda name, *a, **k: {"SPOOL": 1, "VALUE": 1}[name]
        oams.cmd_OAMS_SET_LED_ERROR(gcmd)
        assert 1 in oams._bay_flags()

    def test_a_hand_cleared_error_is_no_longer_owed(self):
        oams = _make_oams()
        oams.set_led_error = MagicMock()
        oams._flag_bay(1)
        gcmd = MagicMock()
        gcmd.get_int.side_effect = lambda name, *a, **k: {"SPOOL": 1, "VALUE": 0}[name]
        oams.cmd_OAMS_SET_LED_ERROR(gcmd)
        assert 1 not in oams._bay_flags()


class TestWorkingTheExtruderFree:
    """
    The retract between unload attempts was a flat 5 mm against a
    tool_stn_unload of 75 -- a fifteenth of the distance AFC itself says clears
    the gears. It could never free anything.

    It is worth doing at all because the unit keeps pulling after a failed
    attempt returns: measured on a deliberately jammed unload, the command
    errored and the motor was still straining four minutes later at 0.53-0.59 A
    with the encoder frozen. So the gap between attempts is the one moment when
    pulling from both ends at once can walk the filament out.
    """

    def _oams(self, **cfg):
        oams = _make_oams(config_values=cfg or None)
        oams.gcode = MagicMock()
        return oams

    def test_it_prefers_the_lanes_own_tool_stn_unload(self):
        oams = self._oams()
        oams.current_spool = 1
        oams._resolve_lane_name = lambda idx: "lane5"
        lane = types.SimpleNamespace(
            extruder_obj=types.SimpleNamespace(tool_stn_unload=75.0))
        oams.afc = types.SimpleNamespace(lanes={"lane5": lane})
        assert oams._stall_retract_mm() == 75.0

    def test_explicit_config_overrides_the_lane(self):
        oams = self._oams(unload_stall_retract_mm=20.0)
        oams.current_spool = 1
        oams._resolve_lane_name = lambda idx: "lane5"
        oams.afc = types.SimpleNamespace(lanes={"lane5": types.SimpleNamespace(
            extruder_obj=types.SimpleNamespace(tool_stn_unload=75.0))})
        assert oams._stall_retract_mm() == 20.0

    def test_an_unresolvable_lane_falls_back(self):
        # A bare OAMS_UNLOAD_SPOOL, no hardware service, a spool AFC does not
        # know: still retract something worth doing.
        oams = self._oams()
        oams.current_spool = None
        assert oams._stall_retract_mm() == 40.0

    def test_zero_skips_retracting_entirely(self):
        oams = self._oams(unload_stall_retract_mm=0.0,
                          unload_stall_retract_tries=0)
        oams.current_spool = None
        oams._work_the_extruder_free()
        oams.gcode.run_script_from_command.assert_not_called()

    def test_it_repeats(self):
        oams = self._oams(unload_stall_retract_mm=30.0,
                          unload_stall_retract_tries=3)
        oams._work_the_extruder_free()
        moves = [str(c) for c in oams.gcode.run_script_from_command.call_args_list
                 if "G1 E-" in str(c)]
        assert len(moves) == 3 and all("E-30.00" in m for m in moves)

    def test_a_refused_move_stops_retrying_and_does_not_raise(self):
        # A cold extruder, or a toolhead that is not the loaded one. The unload
        # retry must proceed without the retract rather than die with it.
        oams = self._oams(unload_stall_retract_mm=30.0,
                          unload_stall_retract_tries=3)
        oams.gcode.run_script_from_command.side_effect = Exception("cold")
        oams._work_the_extruder_free()
        assert oams.gcode.run_script_from_command.call_count == 1


class TestAWedgedUnitIsActuallyCancelled:
    """
    Host-side idle does not mean the unit is idle.

    action_status is cleared the moment the MCU answers, and ERROR_BUSY is an
    answer -- so after a refused command the host reads "nothing in flight"
    while the unit is still moving. abort_current_action's early return then
    skipped the firmware cancel in the exact case its own comment describes: a
    wedged MCU that rejects everything until power-cycled.

    Reported from a real jam: PLA swelled in a hot chamber, both unload
    attempts came back "OAMS is busy", AFC gave up and paused -- and the AMS
    carried on retracting, because nothing had ever told it to stop.
    """

    def test_force_sends_the_cancel_with_no_action_tracked(self):
        oams = _make_oams()
        oams.action_status = None
        oams.action_status_code = OAMSOpCode.ERROR_BUSY
        oams._unit_busy = True
        oams.load_spool_cancel = MagicMock()
        oams.abort_current_action(wait=False, force=True)
        oams.load_spool_cancel.assert_called_once()

    def test_force_does_not_erase_the_reason_for_the_abort(self):
        # Nothing was tracked, so there is no status to rewrite -- and the code
        # already recorded is why the caller is aborting.
        oams = _make_oams()
        oams.action_status = None
        oams.action_status_code = OAMSOpCode.ERROR_BUSY
        oams._unit_busy = True
        oams.load_spool_cancel = MagicMock()
        oams.abort_current_action(wait=False, force=True)
        assert oams.action_status_code == OAMSOpCode.ERROR_BUSY

    def test_without_force_it_still_short_circuits(self):
        # Unchanged for every existing caller.
        oams = _make_oams()
        oams.action_status = None
        oams.load_spool_cancel = MagicMock()
        oams.abort_current_action(wait=False)
        oams.load_spool_cancel.assert_not_called()

    def test_giving_up_on_an_unload_still_stops_the_unit(self):
        # THE BUG. Every path out of the retry loop leaves a command with the
        # MCU, and the caller's next act is to error and pause the print.
        oams = _make_oams()
        oams.abort_current_action = MagicMock()
        oams.reactor.pause = MagicMock()
        oams.gcode = MagicMock()
        oams.unload_spool = MagicMock(
            side_effect=[(False, "OAMS is busy"), (False, "OAMS is busy")])

        success, _ = oams.unload_spool_with_retry(max_retries=2)

        assert success is False
        assert oams.abort_current_action.call_args_list[-1].kwargs.get("force") is True, (
            "the unit must be cancelled when AFC gives up, not left running")

    def test_the_retry_abort_forces_too(self):
        # The previous attempt has ANSWERED, so action_status is already clear
        # and an unforced abort would send no cancel -- which is why attempt 2
        # got refused as well.
        oams = _make_oams()
        oams.abort_current_action = MagicMock()
        oams.reactor.pause = MagicMock()
        oams.gcode = MagicMock()
        oams.unload_spool = MagicMock(
            side_effect=[(False, "OAMS is busy"), (True, "unloaded")])

        oams.unload_spool_with_retry(max_retries=3)

        assert oams.abort_current_action.call_args_list[0].kwargs.get("force") is True


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
        # Two passes of M83/G92/G1/M400 -- the retract now repeats
        # (unload_stall_retract_tries) instead of a single token 5 mm.
        assert gcode.run_script_from_command.call_count == 8
        assert any("G1 E-40.00" in str(c) for c
                   in gcode.run_script_from_command.call_args_list), (
            "the retract must be a distance that can actually clear the gears")
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
            "OAMS[0]: could not retract the extruder (40mm, try 1/2): no extruder",
        ) in oams.logger.messages
        # ...and it gives up on retracting rather than retrying a move the
        # toolhead has already refused.
        assert gcode.run_script_from_command.call_count == 1

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
        oams.oams_current_pid_cmd.send.assert_called_once()

    def test_sends_via_current_pid_cmd_not_pressure_pid_cmd(self):
        """Regression test for issue #772: the current-loop PID gains must go
        to the dedicated current-PID command, never the pressure/FPS one."""
        oams = _make_oams()
        gcmd = _make_gcmd({"P": 1.0, "I": 2.0, "D": 3.0, "TARGET": 0.2})
        oams.cmd_OAMS_CURRENT_PID_SET(gcmd)
        oams.oams_current_pid_cmd.send.assert_called_once()
        oams.oams_pid_cmd.send.assert_not_called()

    def test_raises_when_current_pid_cmd_unavailable(self):
        """Covers the `self.oams_current_pid_cmd is None` guard's True branch
        (firmware too old to support the current-PID command)."""
        oams = _make_oams()
        oams.oams_current_pid_cmd = None
        gcmd = _make_gcmd({"P": 1.0, "I": 2.0, "D": 3.0})
        with pytest.raises(Exception):
            oams.cmd_OAMS_CURRENT_PID_SET(gcmd)


class TestPidSetCommand:
    def test_raises_when_pid_cmd_unavailable(self):
        """Covers _require_mcu_cmd's guard for this command specifically."""
        oams = _make_oams()
        oams.oams_pid_cmd = None
        gcmd = _make_gcmd({"P": 4.0, "I": 0.1, "D": 0.2})
        with pytest.raises(Exception):
            oams.cmd_OAMS_PID_SET(gcmd)

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

    def test_does_not_touch_current_pid_cmd(self):
        """Regression test for issue #772: the pressure/FPS PID gains must
        never go to the dedicated current-PID command."""
        oams = _make_oams()
        gcmd = _make_gcmd({"P": 4.0, "I": 0.1, "D": 0.2})
        oams.cmd_OAMS_PID_SET(gcmd)
        oams.oams_current_pid_cmd.send.assert_not_called()

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
    def test_raises_when_calibrate_hub_hes_cmd_unavailable(self):
        """Covers _require_mcu_cmd's guard for this command specifically, and
        that the guard runs before action_status latches (same wedging risk
        as issue #789)."""
        oams = _make_oams()
        oams.oams_calibrate_hub_hes_cmd = None
        gcmd = _make_gcmd({"SPOOL": 0})
        with pytest.raises(Exception):
            oams.cmd_OAMS_CALIBRATE_HUB_HES(gcmd)
        assert oams.action_status is None

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


    def test_stall_raises(self):
        oams = _make_oams()
        oams._wait_for_calibration = MagicMock(return_value=False)
        gcmd = _make_gcmd({"SPOOL": 2})

        with pytest.raises(Exception):
            oams.cmd_OAMS_CALIBRATE_HUB_HES(gcmd)

        oams._wait_for_calibration.assert_called_once_with("HES 2")
        gcmd.error.assert_called_once_with(
            "Calibration of HES 2 stopped: the OpenAMS stopped moving without an answer")
        oams.afc.function.ConfigRewrite.assert_not_called()


class TestCalibratePtfeLengthCommand:
    def test_missing_spool_raises(self):
        oams = _make_oams()
        gcmd = _make_gcmd({})
        with pytest.raises(Exception):
            oams.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)

    def test_missing_spool_does_not_latch_action_status(self):
        """Regression test for issue #789: action_status must not be set
        until after SPOOL validation succeeds, or a bad command wedges the
        unit as permanently CALIBRATING."""
        oams = _make_oams()
        oams.action_status = None
        gcmd = _make_gcmd({})
        with pytest.raises(Exception):
            oams.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)
        assert oams.action_status is None
        oams.oams_calibrate_ptfe_length_cmd.send.assert_not_called()

    def test_raises_when_calibrate_ptfe_length_cmd_unavailable(self):
        """Covers _require_mcu_cmd's guard for this command specifically, and
        that the guard runs before action_status latches."""
        oams = _make_oams()
        oams.oams_calibrate_ptfe_length_cmd = None
        gcmd = _make_gcmd({"SPOOL": 0})
        with pytest.raises(Exception):
            oams.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)
        assert oams.action_status is None


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

    def test_stall_raises(self):
        oams = _make_oams()
        oams._wait_for_calibration = MagicMock(return_value=False)
        gcmd = _make_gcmd({"SPOOL": 0})

        with pytest.raises(Exception):
            oams.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)

        oams._wait_for_calibration.assert_called_once_with("PTFE length")
        gcmd.error.assert_called_once_with(
            "Calibration of PTFE length stopped: the OpenAMS stopped moving without an "
            "answer. Check where the filament stopped short of the toolhead")
        oams.afc.function.ConfigRewrite.assert_not_called()

    def test_wait_loop_pauses_until_action_completes(self):
        oams = _make_oams()
        oams.action_status_code = OAMSOpCode.SUCCESS
        oams.action_status_value = 900
        oams.reactor.pause = MagicMock(side_effect=lambda t: setattr(oams, "action_status", None))
        gcmd = _make_gcmd({"SPOOL": 0})

        oams.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)

        oams.reactor.pause.assert_called_once()


class TestWaitForCalibration:
    @staticmethod
    def _clock(oams, clicks=None, answer_at=None):
        """
        Make reactor.pause move the clock to its target, feeding encoder
        readings and answering the calibration when the clock reaches answer_at.

        :param oams: unit under test
        :param clicks: encoder readings taken one per pause, the last repeating
        :param answer_at: clock time the firmware answers, None never
        """
        readings = list(clicks or [])

        def pause(until):
            oams.reactor._monotonic = until
            if readings:
                oams.encoder_clicks = readings.pop(0) if len(readings) > 1 else readings[0]
            if answer_at is not None and until >= answer_at:
                oams.action_status = None
        oams.reactor.pause = MagicMock(side_effect=pause)

    def test_answered_calibration_returns_true(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.CALIBRATING
        self._clock(oams, answer_at=101.0)

        assert oams._wait_for_calibration("PTFE length") is True
        assert oams.reactor.pause.call_count == 2
        oams.oams_load_spool_cancel_cmd.send.assert_not_called()
        assert oams.logger.messages == []

    def test_a_moving_encoder_keeps_waiting(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.CALIBRATING
        # Feeding for 40s, longer than the stall window, then answering.
        self._clock(oams, clicks=list(range(1, 82)), answer_at=140.0)

        assert oams._wait_for_calibration("PTFE length") is True
        oams.oams_load_spool_cancel_cmd.send.assert_not_called()
        assert oams.logger.messages == []

    def test_a_still_encoder_without_an_answer_cancels(self):
        oams = _make_oams(oams_idx=1)
        oams.action_status = OAMSStatus.CALIBRATING
        oams.encoder_clicks = 2042
        self._clock(oams)

        assert oams._wait_for_calibration("PTFE length") is False
        oams.oams_load_spool_cancel_cmd.send.assert_called_once_with()
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.ERROR_UNSPECIFIED
        assert oams.logger.messages[0] == (
            "error", "OAMS[1]: PTFE length calibration got no answer and the encoder "
                     "has not moved for 30s, cancelling it")
        assert oams.logger.messages[-1] == ("info", "OAMS[1]: Abort complete")


# ── load_spool ────────────────────────────────────────────────────────────────

class TestLoadSpool:
    def test_raises_when_load_spool_cmd_unavailable(self):
        """Covers _require_mcu_cmd's guard for this command, and that the
        guard runs before action_status latches."""
        oams = _make_oams()
        oams.oams_load_spool_cmd = None
        with pytest.raises(Exception):
            oams.load_spool(0)
        assert oams.action_status is None

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
    def test_raises_when_unload_spool_cmd_unavailable(self):
        """Covers _require_mcu_cmd's guard for this command, and that the
        guard runs before action_status latches."""
        oams = _make_oams()
        oams.oams_unload_spool_cmd = None
        with pytest.raises(Exception):
            oams.unload_spool()
        assert oams.action_status is None

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

    @staticmethod
    def _clocked(oams, on_pause=None):
        """Drive the reactor the way a real one behaves: monotonic() reports
        the clock, pause() advances it."""
        t = [0.0]
        oams.reactor.monotonic = lambda: t[0]

        def pause(_waketime):
            t[0] += 1.0
            if on_pause is not None:
                on_pause(t[0])
        oams.reactor.pause = pause
        return t

    def test_it_keeps_waiting_while_the_encoder_is_still_moving(self):
        # THE BUG. A genuine 41s unload tripped a hardcoded 40s deadline and
        # was reported as "MCU unresponsive" one second before the MCU
        # answered; the retry then "succeeded" only by finding the bay empty.
        # While the encoder ticks, the unit is working -- wait for it.
        oams = _make_oams()

        def tick(now):
            oams.encoder_clicks += 1          # still moving
            if now >= 60.0:                   # ...and then it finishes
                oams.action_status = None
                oams.action_status_code = OAMSOpCode.SUCCESS
        self._clocked(oams, on_pause=tick)

        success, message = oams.unload_spool()
        assert success is True                # not a timeout at 40s
        assert "unloaded successfully" in message

    def test_a_still_encoder_after_movement_is_a_stall(self):
        oams = _make_oams()

        def tick(now):
            if now <= 5.0:
                oams.encoder_clicks += 1      # moves, then stops dead
        self._clocked(oams, on_pause=tick)

        success, message = oams.unload_spool()
        assert success is False
        assert "stalled" in message and "jammed" in message
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.ERROR_UNSPECIFIED

    def test_an_encoder_that_never_reports_falls_through_to_the_backstop(self):
        # Cannot tell "not instrumented" from "stuck" with no movement ever
        # seen, so it does not guess -- it waits out unload_timeout.
        oams = _make_oams()
        oams.unload_timeout = 30.0
        self._clocked(oams)                   # encoder never changes

        success, message = oams.unload_spool()
        assert success is False
        assert "did not complete within 30s" in message
        assert "never reported any encoder movement" in message
        assert "stalled" not in message       # that would be a guess

    def test_the_backstop_does_not_claim_the_mcu_is_unresponsive(self):
        # It said "MCU unresponsive" for a unit that answered a second later,
        # which sends someone chasing a hardware fault that is not there.
        oams = _make_oams()
        oams.unload_timeout = 30.0
        self._clocked(oams)
        _success, message = oams.unload_spool()
        assert "unresponsive" not in message.lower()

    def test_the_encoder_check_can_be_disabled(self):
        oams = _make_oams()
        oams.unload_stall_dwell = 0.0
        oams.unload_timeout = 30.0

        def tick(now):
            if now <= 5.0:
                oams.encoder_clicks += 1
        self._clocked(oams, on_pause=tick)

        _success, message = oams.unload_spool()
        assert "stalled" not in message       # backstop only
        assert "did not complete" in message

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
        # force: an operator-invoked abort must reach the firmware even when
        # the unit has already answered and host status is clear.
        oams.abort_current_action.assert_called_once_with(
            code=OAMSOpCode.ERROR_KLIPPER_CALL, wait=True, force=True
        )

    def test_custom_code_and_no_wait(self):
        oams = _make_oams()
        oams.abort_current_action = MagicMock()
        gcmd = _make_gcmd({"CODE": OAMSOpCode.ERROR_BUSY, "WAIT": 0})
        oams.cmd_OAMS_ABORT_ACTION(gcmd)
        oams.abort_current_action.assert_called_once_with(
            code=OAMSOpCode.ERROR_BUSY, wait=False, force=True
        )


# ── set_oams_follower / abort_current_action ─────────────────────────────────

class TestSetOamsFollower:
    def test_sends_enable_and_direction(self):
        oams = _make_oams()
        oams.set_oams_follower(1, 0)
        oams.oams_follower_cmd.send.assert_called_once_with([1, 0])

    def test_raises_when_follower_cmd_unavailable(self):
        """Covers _require_mcu_cmd's guard for this command specifically."""
        oams = _make_oams()
        oams.oams_follower_cmd = None
        with pytest.raises(Exception):
            oams.set_oams_follower(1, 0)


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

    def test_wait_during_a_load_returns_on_the_cancel_ack(self):
        # The cancel's ack is what the wait is for, so it must end the load
        # rather than be swallowed and leave the wait to time out.
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams.action_status_value = 7
        clock = {"now": 100.0, "pauses": 0}

        def pause(t):
            clock["pauses"] += 1
            clock["now"] = t
            if clock["pauses"] == 1:
                oams._oams_action_status({"action": OAMSStatus.LOADING,
                                          "code": OAMSOpCode.CANCEL})

        oams.reactor.monotonic = lambda: clock["now"]
        oams.reactor.pause = pause
        oams.logger.messages.clear()

        oams.abort_current_action(code=OAMSOpCode.ERROR_KLIPPER_CALL, wait=True)

        assert clock["pauses"] == 1
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.ERROR_KLIPPER_CALL
        assert oams.action_status_value is None
        assert oams._pending_cancel_ack is False
        oams.oams_load_spool_cancel_cmd.send.assert_called_once_with()
        loading = int(OAMSStatus.LOADING)
        klipper_call = int(OAMSOpCode.ERROR_KLIPPER_CALL)
        assert oams.logger.messages == [
            ("debug", f"OAMS[0]: Aborting current action {loading} "
                      f"with code {klipper_call}"),
            ("debug", "OAMS status received"),
            ("info", "OAMS[0]: Abort complete"),
        ]

    def test_forced_abort_with_nothing_in_flight_leaves_no_flag(self):
        oams = _make_oams()
        oams.action_status = None
        oams.action_status_code = OAMSOpCode.ERROR_BUSY
        oams._unit_busy = True
        oams.logger.messages.clear()

        oams.abort_current_action(wait=False, force=True)

        oams.oams_load_spool_cancel_cmd.send.assert_called_once_with()
        assert oams._pending_cancel_ack is False
        assert oams.action_status_code == OAMSOpCode.ERROR_BUSY
        assert oams.logger.messages == [
            ("debug", "OAMS[0]: Abort without waiting - status cleared"),
        ]

        # A later real CANCEL is therefore not eaten by a stale flag.
        oams.action_status = OAMSStatus.UNLOADING
        oams._oams_action_status({"action": OAMSStatus.UNLOADING,
                                  "code": OAMSOpCode.CANCEL})
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.CANCEL

    def test_forced_abort_sends_nothing_to_a_unit_that_finished(self):
        # The firmware holds a cancel sent while idle and drops the next load
        # with it, so a load that reported SUCCESS gets no cancel after it.
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams._unit_busy = True
        oams._oams_action_status({"action": OAMSStatus.LOADING,
                                  "code": OAMSOpCode.SUCCESS})
        oams.logger.messages.clear()

        oams.abort_current_action(wait=True, force=True)

        oams.oams_load_spool_cancel_cmd.send.assert_not_called()
        assert oams._unit_busy is False
        assert oams.action_status_code == OAMSOpCode.SUCCESS
        assert oams.logger.messages == []

    def test_a_busy_refusal_leaves_the_unit_busy(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.UNLOADING
        oams._oams_action_status({"action": OAMSStatus.UNLOADING,
                                  "code": OAMSOpCode.ERROR_BUSY})
        assert oams._unit_busy is True

        oams.abort_current_action(wait=False, force=True)

        oams.oams_load_spool_cancel_cmd.send.assert_called_once_with()

    def test_a_late_cancel_ack_ends_the_busy_state(self):
        # The stall path gives up on a load host-side; the cancel's ack comes
        # with nothing in flight and still says the unit has stopped.
        oams = _make_oams()
        oams.action_status = None
        oams._unit_busy = True
        oams._oams_action_status({"action": OAMSStatus.LOADING,
                                  "code": OAMSOpCode.CANCEL})
        assert oams._unit_busy is False

    def test_the_swallowed_cancel_ack_leaves_the_auto_unload_busy(self):
        # Printer 3: the stall path cancels the load and starts the auto-unload;
        # the swallowed ack says nothing about the unload still running.
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams.load_spool_cancel()
        oams.action_status = None
        oams.action_status = OAMSStatus.UNLOADING
        oams._unit_busy = True
        oams._oams_action_status({"action": OAMSStatus.LOADING,
                                  "code": OAMSOpCode.CANCEL})
        assert oams._unit_busy is True

        oams._oams_action_status({"action": OAMSStatus.UNLOADING,
                                  "code": OAMSOpCode.SUCCESS})
        assert oams._unit_busy is False

    def test_a_reply_about_the_other_operation_leaves_the_unit_busy(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.UNLOADING
        oams._unit_busy = True
        oams._oams_action_status({"action": OAMSStatus.LOADING,
                                  "code": OAMSOpCode.SUCCESS})
        assert oams._unit_busy is True

    def test_abort_without_wait_keeps_the_flag_for_the_unwaited_ack(self):
        # A load was in flight and nobody waits for its cancel, so the ack
        # is still due and must stay marked.
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams.logger.messages.clear()

        oams.abort_current_action(code=OAMSOpCode.ERROR_BUSY, wait=False)

        assert oams._pending_cancel_ack is True
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.ERROR_BUSY
        assert oams.logger.messages == [
            ("debug", "OAMS[0]: Abort without waiting - status cleared"),
        ]


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
        oams.action_status = OAMSStatus.UNLOADING       # an unload IS in flight
        oams._oams_action_status({"action": OAMSStatus.UNLOADING, "code": OAMSOpCode.ERROR_BUSY})
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.ERROR_BUSY

    def test_error_action_clears_status(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams._oams_action_status({"action": OAMSStatus.ERROR, "code": OAMSOpCode.ERROR_UNSPECIFIED})
        assert oams.action_status is None

    def test_the_cancel_ack_does_not_finish_the_auto_unload(self):
        """THE RACE, as it happened. The load-stall path cancels the load and
        starts an auto-unload in the same breath, so the cancel's ack arrives
        with the unload waiting:

          Load stalled - encoder stopped advancing for 5s (spool stuck)
          Auto-unloading before retry
          Unload failed: Unload was cancelled (stale cancel response
            interfered). Attempt 1/2

        It recovered six seconds later having reported a failure that never
        happened."""
        oams = _make_oams()
        oams.load_spool_cancel()                        # ack now in flight
        oams.action_status = OAMSStatus.UNLOADING       # the auto-unload starts
        oams._oams_action_status({"action": OAMSStatus.LOADING,
                                  "code": OAMSOpCode.CANCEL})
        assert oams.action_status == OAMSStatus.UNLOADING   # still waiting
        assert oams.action_status_code != OAMSOpCode.CANCEL

        # ...and the unload's own reply still lands.
        oams._oams_action_status({"action": OAMSStatus.UNLOADING,
                                  "code": OAMSOpCode.SUCCESS})
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.SUCCESS

    def test_only_one_cancel_ack_is_swallowed(self):
        # The flag is armed by sending a cancel, not standing. A second CANCEL
        # is a real one and must not be eaten.
        oams = _make_oams()
        oams.load_spool_cancel()
        oams.action_status = OAMSStatus.UNLOADING
        oams._oams_action_status({"action": OAMSStatus.LOADING,
                                  "code": OAMSOpCode.CANCEL})
        oams._oams_action_status({"action": OAMSStatus.UNLOADING,
                                  "code": OAMSOpCode.CANCEL})
        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.CANCEL

    def test_stall_path_ack_is_swallowed_once_the_load_is_given_up(self):
        # The stall path cancels, clears the load host-side and starts the
        # auto-unload; the late ack must not finish that unload.
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams.load_spool_cancel()
        oams.action_status = None                       # load given up on
        oams.action_status = OAMSStatus.UNLOADING       # auto-unload starts
        oams.action_status_code = None
        oams.logger.messages.clear()

        oams._oams_action_status({"action": OAMSStatus.LOADING,
                                  "code": OAMSOpCode.CANCEL})

        assert oams.action_status == OAMSStatus.UNLOADING
        assert oams.action_status_code is None
        assert oams._pending_cancel_ack is False
        assert oams.logger.messages == [
            ("debug", "OAMS status received"),
            ("debug", "OAMS[0]: swallowed the cancel ack for the load "
                      "that was just cancelled"),
        ]

    def test_cancel_ack_finishes_a_load_that_is_waiting_on_it(self):
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams.load_spool_cancel()
        oams.logger.messages.clear()

        oams._oams_action_status({"action": OAMSStatus.LOADING,
                                  "code": OAMSOpCode.CANCEL})

        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.CANCEL
        assert oams._pending_cancel_ack is False
        assert oams.logger.messages == [("debug", "OAMS status received")]

    def test_a_stalled_loads_ack_does_not_finish_the_next_load(self):
        # Printer 3: the stall path cancelled a load and gave up, the next load
        # went out before the ack came, and the ack read as that load passing.
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams.load_spool_cancel()
        oams.action_status = None                       # stalled load given up on
        sent = []
        oams.oams_load_spool_cmd = MagicMock()
        oams.oams_load_spool_cmd.send.side_effect = sent.append
        replies = iter([{"action": OAMSStatus.LOADING, "code": OAMSOpCode.CANCEL},
                        {"action": OAMSStatus.LOADING, "code": OAMSOpCode.SUCCESS}])
        oams.load_stall_dwell = 0.0
        oams.reactor.pause = lambda t: oams._oams_action_status(next(replies))

        code, message = oams.load_spool(2)

        assert sent == [[2]]
        assert code == OAMSOpCode.SUCCESS
        assert message == "Spool loaded successfully"
        assert oams._pending_cancel_ack is False
        assert oams._cancel_ack_stale is False
        assert ("debug", "OAMS[0]: swallowed the cancel ack for the load "
                         "that was just cancelled") in oams.logger.messages

    def test_a_cancel_for_the_load_itself_still_ends_it(self):
        oams = _make_oams()
        oams.oams_load_spool_cmd = MagicMock()
        oams.load_stall_dwell = 0.0

        def pause(t):
            if oams._pending_cancel_ack is False:
                oams.load_spool_cancel()                # cancelled while it runs
            else:
                oams._oams_action_status({"action": OAMSStatus.LOADING,
                                          "code": OAMSOpCode.CANCEL})

        oams.reactor.pause = pause
        code, message = oams.load_spool(1)

        assert code == OAMSOpCode.CANCEL
        assert message == "Spool loading cancelled"

    def test_pending_flag_alone_does_not_swallow_a_non_cancel_reply(self):
        # Flag set but the reply is not a CANCEL: it is a normal result.
        oams = _make_oams()
        oams.action_status = OAMSStatus.LOADING
        oams.load_spool_cancel()
        oams.action_status = OAMSStatus.UNLOADING
        oams.logger.messages.clear()

        oams._oams_action_status({"action": OAMSStatus.UNLOADING,
                                  "code": OAMSOpCode.SUCCESS})

        assert oams.action_status is None
        assert oams.action_status_code == OAMSOpCode.SUCCESS
        assert oams._pending_cancel_ack is True
        assert oams.logger.messages == [("debug", "OAMS status received")]

    def test_a_reply_about_the_other_operation_is_ignored(self):
        # A load's reply cannot finish an unload, whatever it says.
        oams = _make_oams()
        oams.action_status = OAMSStatus.UNLOADING
        oams._oams_action_status({"action": OAMSStatus.LOADING,
                                  "code": OAMSOpCode.SUCCESS})
        assert oams.action_status == OAMSStatus.UNLOADING
        assert oams.action_status_code != OAMSOpCode.SUCCESS

    def test_a_reply_with_nothing_in_flight_is_ignored(self):
        # Recording its code would hand it to whatever runs next.
        oams = _make_oams()
        oams.action_status = None
        oams.action_status_code = None
        oams._oams_action_status({"action": OAMSStatus.LOADING,
                                  "code": OAMSOpCode.ERROR_BUSY})
        assert oams.action_status_code is None

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
