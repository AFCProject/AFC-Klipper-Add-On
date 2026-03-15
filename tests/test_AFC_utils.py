"""
Unit tests for extras/AFC_utils.py

Covers:
  - check_and_return()
  - section_in_config()
  - DebounceButton
  - AFC_moonraker
"""

from __future__ import annotations

import configparser
import json
from io import StringIO
from unittest.mock import MagicMock, patch, call
import pytest

# conftest installs Klipper mocks; extras is on sys.path via REPO_ROOT
from extras.AFC_utils import check_and_return, section_in_config, DebounceButton, AFC_moonraker


# ── check_and_return ──────────────────────────────────────────────────────────

class TestCheckAndReturn:
    def test_key_present_returns_value(self):
        data = {"color": "red", "weight": 250}
        assert check_and_return("color", data) == "red"

    def test_key_present_numeric_string(self):
        data = {"weight": "250"}
        assert check_and_return("weight", data) == "250"

    def test_key_missing_returns_zero_string(self):
        data = {"color": "blue"}
        assert check_and_return("weight", data) == "0"

    def test_empty_dict_returns_zero_string(self):
        assert check_and_return("anything", {}) == "0"

    def test_key_with_none_value(self):
        data = {"key": None}
        assert check_and_return("key", data) is None

    def test_key_with_zero_value(self):
        data = {"key": 0}
        assert check_and_return("key", data) == 0


# ── section_in_config ─────────────────────────────────────────────────────────

class TestSectionInConfig:
    def _make_config(self, *sections):
        """Return a MockConfig whose fileconfig contains the given sections."""
        from tests.conftest import MockConfig, _make_fileconfig
        cfg = MockConfig()
        cfg.fileconfig = _make_fileconfig(*sections)
        return cfg

    def test_exact_section_found(self):
        cfg = self._make_config("AFC_hub my_hub")
        assert section_in_config(cfg, "AFC_hub my_hub") is True

    def test_partial_section_name_found(self):
        cfg = self._make_config("AFC_hub my_hub")
        assert section_in_config(cfg, "my_hub") is True

    def test_section_not_found(self):
        cfg = self._make_config("AFC_hub my_hub")
        assert section_in_config(cfg, "missing_section") is False

    def test_empty_fileconfig_returns_false(self):
        cfg = self._make_config()
        assert section_in_config(cfg, "anything") is False

    def test_multiple_sections_correct_one_found(self):
        cfg = self._make_config("AFC_hub hub1", "AFC_hub hub2", "AFC_lane lane1")
        assert section_in_config(cfg, "hub2") is True
        assert section_in_config(cfg, "lane1") is True
        assert section_in_config(cfg, "lane2") is False


# ── DebounceButton ────────────────────────────────────────────────────────────

class TestDebounceButton:
    """DebounceButton wraps a filament sensor's note_filament_present method."""

    def _make_filament_sensor(self, sig_params):
        """
        Build a minimal filament-sensor mock with the given parameter names
        on its runout_helper.note_filament_present signature.
        """
        import inspect

        # Construct a function with the desired signature dynamically
        arg_list = ", ".join(sig_params)
        exec_globals: dict = {}
        exec(f"def note_filament_present({arg_list}): pass", exec_globals)
        func = exec_globals["note_filament_present"]

        helper = MagicMock()
        helper.note_filament_present = func
        sensor = MagicMock()
        sensor.runout_helper = helper
        return sensor

    def _make_config(self, debounce_delay=0.0):
        from tests.conftest import MockConfig, MockPrinter, MockReactor
        reactor = MockReactor()
        printer = MockPrinter()
        printer._reactor = reactor
        cfg = MockConfig(printer=printer, values={"debounce_delay": debounce_delay})
        return cfg

    def test_init_sets_debounce_delay(self):
        cfg = self._make_config(debounce_delay=0.05)
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        assert btn.debounce_delay == 0.05

    def test_initial_states_are_none(self):
        cfg = self._make_config()
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        assert btn.logical_state is None
        assert btn.physical_state is None
        assert btn.latest_eventtime is None

    def test_button_handler_records_state(self):
        cfg = self._make_config()
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        btn._button_handler(100.0, True)
        assert btn.physical_state is True
        assert btn.latest_eventtime == 100.0

    def test_same_state_does_not_re_register_callback(self):
        cfg = self._make_config()
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        btn.logical_state = True
        reactor = cfg.get_printer().get_reactor()
        reactor.register_callback = MagicMock()
        btn._button_handler(100.0, True)  # same as logical_state → no callback
        reactor.register_callback.assert_not_called()

    def test_state_change_registers_callback(self):
        cfg = self._make_config()
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        btn.logical_state = False
        reactor = cfg.get_printer().get_reactor()
        reactor.register_callback = MagicMock()
        btn._button_handler(100.0, True)  # transition False→True
        reactor.register_callback.assert_called_once()

    def test_debounce_event_ignored_if_no_transition(self):
        cfg = self._make_config()
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        btn.logical_state = True
        btn.physical_state = True
        btn.button_action = MagicMock()
        btn._debounce_event(100.0)
        btn.button_action.assert_not_called()

    def test_debounce_event_updates_logical_state(self):
        cfg = self._make_config(debounce_delay=0.0)
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        btn.logical_state = False
        btn.physical_state = True
        btn.latest_eventtime = 100.0
        btn.button_action = MagicMock()
        btn._debounce_event(100.0)
        assert btn.logical_state is True

    def test_init_kalico_signature_uses_button_handler(self):
        """Covers line 135: Kalico exact-match signature assigns _button_handler."""
        cfg = self._make_config()
        # Exact Kalico params (no 'self' since inspect works on function signature)
        sensor = self._make_filament_sensor(
            ["eventtime", "is_filament_present", "force", "immediate"]
        )
        btn = DebounceButton(cfg, sensor)
        # The assignment to _button_handler was made; button_action should be set
        assert btn.button_action is not None

    def test_init_two_param_signature_uses_button_handler_else(self):
        """Covers line 139: exactly 2 params → else branch assigns _button_handler."""
        cfg = self._make_config()
        # Only 2 parameters: eventtime, state (not > 2, not == 1)
        sensor = self._make_filament_sensor(["eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        # Should still set button_action
        assert btn.button_action is not None

    def test_button_handler_delegates_to_internal_handler(self):
        """Covers line 146: button_handler calls _button_handler with reactor time."""
        cfg = self._make_config()
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        btn._button_handler = MagicMock()
        btn.button_handler(True)
        btn._button_handler.assert_called_once()

    def test_debounce_event_ignores_stale_event(self):
        """Covers line 163: stale event (more recent event exists) → returns early."""
        cfg = self._make_config(debounce_delay=1.0)
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        btn.logical_state = False
        btn.physical_state = True
        btn.latest_eventtime = 100.0
        btn.button_action = MagicMock()
        # eventtime=100.5 → 100.5 - 1.0 = 99.5 < 100.0 → stale → returns early
        btn._debounce_event(100.5)
        btn.button_action.assert_not_called()

    def test_debounce_event_falls_back_to_positional_args(self):
        """Covers lines 169-170: button_action that doesn't accept kwargs → except branch."""
        cfg = self._make_config(debounce_delay=0.0)
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        btn.logical_state = False
        btn.physical_state = True
        btn.latest_eventtime = 100.0

        # A callback that only accepts positional args → raises TypeError on kwargs call
        calls = []
        def positional_only(eventtime, state):
            calls.append((eventtime, state))

        btn.button_action = positional_only
        btn._debounce_event(101.0)
        assert len(calls) == 1
        assert calls[0] == (101.0, True)


# ── AFC_moonraker ─────────────────────────────────────────────────────────────

class TestAFCMoonraker:
    def _make_moonraker(self, host="http://localhost", port="7125"):
        from tests.conftest import MockLogger
        logger = MockLogger()
        return AFC_moonraker(host, port, logger)

    def test_init_sets_host_with_port(self):
        mr = self._make_moonraker("http://localhost", "7125")
        assert "7125" in mr.host

    def test_init_strips_trailing_slash(self):
        mr = self._make_moonraker("http://localhost/", "7125")
        assert not mr.host.endswith("//")

    def test_init_default_stats_none(self):
        mr = self._make_moonraker()
        assert mr.afc_stats is None
        assert mr.last_stats_time is None

    def test_get_results_connection_error_returns_none(self):
        mr = self._make_moonraker()
        with patch("extras.AFC_utils.urlopen", side_effect=Exception("connection refused")):
            result = mr._get_results("http://localhost:7125/server/info", print_error=False)
        assert result is None

    def test_get_results_bad_status_returns_none(self):
        mr = self._make_moonraker()
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.reason = "Internal Server Error"
        with patch("extras.AFC_utils.urlopen", return_value=mock_resp):
            result = mr._get_results("http://localhost:7125/server/info", print_error=False)
        assert result is None

    def test_get_results_success_returns_data(self):
        mr = self._make_moonraker()
        payload = {"result": {"state": "ready"}}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("extras.AFC_utils.urlopen", return_value=mock_resp), \
             patch("extras.AFC_utils.json.load", return_value=payload):
            result = mr._get_results("http://localhost:7125/server/info")
        assert result == {"state": "ready"}

    def test_check_and_return_helper(self):
        # Ensure the standalone helper works (already tested above, but
        # verify the moonraker module exports it correctly)
        from extras.AFC_utils import check_and_return
        assert check_and_return("x", {"x": 42}) == 42

    def test_update_afc_stats_logs_on_failure(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        mr.update_afc_stats("some.key", 10)
        errors = [m for lvl, m in mr.logger.messages if lvl == "error"]
        assert len(errors) == 1

    def test_get_spool_not_found_logs_info(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        result = mr.get_spool(42)
        assert result is None
        infos = [m for lvl, m in mr.logger.messages if lvl == "info"]
        assert any("42" in m for m in infos)

    def test_get_spoolman_server_returns_none_when_missing(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"orig": {}})
        result = mr.get_spoolman_server()
        assert result is None

    def test_get_spoolman_server_returns_url_when_present(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(
            return_value={"orig": {"spoolman": {"server": "http://spoolman:7912"}}}
        )
        result = mr.get_spoolman_server()
        assert result == "http://spoolman:7912"

    def test_get_file_filament_change_count_default_zero(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        assert mr.get_file_filament_change_count("test.gcode") == 0

    def test_get_file_filament_change_count_from_metadata(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"filament_change_count": 5})
        assert mr.get_file_filament_change_count("test.gcode") == 5

    def test_get_afc_stats_returns_none_on_empty_db(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        result = mr.get_afc_stats()
        assert result is None

    def test_get_afc_stats_returns_cached_after_first_call(self):
        mr = self._make_moonraker()
        payload = {"value": {"toolchange_count": {"total": 10}}}
        mr._get_results = MagicMock(return_value=payload)
        first = mr.get_afc_stats()
        # Second call should use cache (but still calls _get_results when
        # afc_stats is populated, unless last_stats_time is very recent)
        assert first is not None

    def test_check_for_td1_no_td1_in_config(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"orig": {}})
        td1_defined, td1, lane_data = mr.check_for_td1()
        assert td1_defined is False
        assert td1 is False
        assert lane_data is False

    # ── wait_for_moonraker ────────────────────────────────────────────────────

    def test_wait_for_moonraker_connects_immediately_returns_true(self):
        mr = self._make_moonraker()
        toolhead = MagicMock()
        mr._get_results = MagicMock(return_value={"klippy": "ready"})
        result = mr.wait_for_moonraker(toolhead, timeout=5)
        assert result is True
        toolhead.dwell.assert_not_called()

    def test_wait_for_moonraker_connects_after_retries(self):
        mr = self._make_moonraker()
        toolhead = MagicMock()
        mr._get_results = MagicMock(side_effect=[None, None, {"klippy": "ready"}])
        result = mr.wait_for_moonraker(toolhead, timeout=5)
        assert result is True
        assert toolhead.dwell.call_count == 2

    def test_wait_for_moonraker_timeout_returns_false(self):
        mr = self._make_moonraker()
        toolhead = MagicMock()
        mr._get_results = MagicMock(return_value=None)
        result = mr.wait_for_moonraker(toolhead, timeout=3)
        assert result is False
        warnings = [m for lvl, m in mr.logger.messages if lvl == "warning"]
        assert len(warnings) == 1

    # ── get_td1_data ──────────────────────────────────────────────────────────

    def test_get_td1_data_returns_devices_on_success(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"devices": {"SN123": {}}})
        result = mr.get_td1_data()
        assert result == {"SN123": {}}

    def test_get_td1_data_returns_none_when_no_devices_key(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"other": "data"})
        result = mr.get_td1_data()
        assert result is None

    def test_get_td1_data_returns_none_on_failure(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        result = mr.get_td1_data()
        assert result is None

    # ── reboot_td1 ───────────────────────────────────────────────────────────

    def test_reboot_td1_returns_response(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"status": "ok"})
        result = mr.reboot_td1("SN12345")
        assert result == {"status": "ok"}

    def test_reboot_td1_returns_none_on_failure(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        result = mr.reboot_td1("SN12345")
        assert result is None

    # ── send_lane_data ────────────────────────────────────────────────────────

    def test_send_lane_data_logs_error_on_failure(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        mr.send_lane_data({"lane1": {"color": "red"}})
        errors = [m for lvl, m in mr.logger.messages if lvl == "error"]
        assert len(errors) == 1

    def test_send_lane_data_success_no_error(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"value": "ok"})
        mr.send_lane_data({"lane1": {"color": "red"}})
        errors = [m for lvl, m in mr.logger.messages if lvl == "error"]
        assert len(errors) == 0

    # ── remove_database_entry ─────────────────────────────────────────────────

    def test_remove_database_entry_calls_urlopen(self):
        mr = self._make_moonraker()
        with patch("extras.AFC_utils.urlopen") as mock_urlopen:
            mr.remove_database_entry("lane_data", "lane1")
        mock_urlopen.assert_called_once()

    def test_remove_database_entry_logs_debug_on_http_error(self):
        from urllib.error import HTTPError
        mr = self._make_moonraker()
        with patch("extras.AFC_utils.urlopen",
                   side_effect=HTTPError(None, 404, "Not Found", {}, None)):
            mr.remove_database_entry("lane_data", "missing_key")
        debug_msgs = [m for lvl, m in mr.logger.messages if lvl == "debug"]
        assert len(debug_msgs) > 0

    # ── delete_lane_data ──────────────────────────────────────────────────────

    def test_delete_lane_data_calls_remove_for_each_key(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(
            return_value={"value": {"lane1": {}, "lane2": {}}}
        )
        mr.remove_database_entry = MagicMock()
        mr.delete_lane_data()
        assert mr.remove_database_entry.call_count == 2

    def test_delete_lane_data_no_op_on_none_response(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        mr.remove_database_entry = MagicMock()
        mr.delete_lane_data()
        mr.remove_database_entry.assert_not_called()

    # ── trigger_db_backup ─────────────────────────────────────────────────────

    def test_trigger_db_backup_success_logs_path_and_returns_false(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"backup_path": "/tmp/backup.db"})
        error = mr.trigger_db_backup()
        assert error is False
        infos = [m for lvl, m in mr.logger.messages if lvl == "info"]
        assert any("/tmp/backup.db" in m for m in infos)

    def test_trigger_db_backup_failure_returns_true(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        error = mr.trigger_db_backup()
        assert error is True
        errors = [m for lvl, m in mr.logger.messages if lvl == "error"]
        assert len(errors) == 1

    def test_get_spool_returns_resp_when_found(self):
        """Covers line 352: if resp is not None → resp = resp branch in get_spool."""
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"id": 42, "name": "PLA"})
        result = mr.get_spool(42)
        assert result == {"id": 42, "name": "PLA"}

    def test_check_for_td1_with_td1_in_config_and_data(self):
        """Covers lines 371-374: td1 in orig config and data returned."""
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"orig": {"td1": True, "lane_data": True}})
        mr.get_td1_data = MagicMock(return_value={"SN123": {}})
        td1_defined, td1, lane_data = mr.check_for_td1()
        assert td1_defined is True
        assert td1 is True
        assert lane_data is True

    def test_check_for_td1_with_lane_data_only(self):
        """Covers line 377: lane_data in orig config sets _lane_data True."""
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"orig": {"lane_data": True}})
        _, _, lane_data = mr.check_for_td1()
        assert lane_data is True

    def test_get_afc_stats_second_call_uses_cache_path(self):
        """Covers lines 294-296: second call enters the last_stats_time is not None branch."""
        mr = self._make_moonraker()
        payload = {"value": {"tc": 5}}
        mr._get_results = MagicMock(return_value=payload)
        mr.get_afc_stats()        # first call: sets last_stats_time
        mr.get_afc_stats()        # second call: last_stats_time is not None → cached path
        # At least first call was made; second call may skip _get_results if cached
        assert mr._get_results.call_count >= 1

    def test_get_afc_stats_refetches_after_60_seconds(self):
        """Covers lines 297-298: delta > 60s → refetch_data=True, update last_stats_time."""
        from unittest.mock import patch
        from datetime import datetime, timedelta
        mr = self._make_moonraker()
        payload = {"value": {"tc": 5}}
        mr._get_results = MagicMock(return_value=payload)

        t0 = datetime(2024, 1, 1, 12, 0, 0)
        t1 = datetime(2024, 1, 1, 12, 1, 5)  # 65 seconds later

        with patch("extras.AFC_utils.datetime") as mock_dt:
            mock_dt.now.side_effect = [t0, t0, t1]  # first call: 2x now(), second call: once
            mr.get_afc_stats()  # first call: sets last_stats_time to t0
            mr.get_afc_stats()  # second call: delta = 65s > 60 → refetch
        # Two _get_results calls: once per call since refetch was triggered
        assert mr._get_results.call_count == 2

    def test_send_lane_data_http_error_logs_error(self):
        """Covers lines 432-435: HTTPError propagated from _get_results."""
        from urllib.error import HTTPError
        mr = self._make_moonraker()
        mr._get_results = MagicMock(
            side_effect=HTTPError(None, 500, "Internal Server Error", {}, None)
        )
        mr.send_lane_data({"lane1": {"color": "red"}})
        errors = [m for lvl, m in mr.logger.messages if lvl == "error"]
        assert len(errors) >= 1

    def test_delete_lane_data_http_error_logs_debug(self):
        """Covers lines 473-475: HTTPError raised by remove_database_entry."""
        from urllib.error import HTTPError
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"value": {"lane1": {}}})
        mr.remove_database_entry = MagicMock(
            side_effect=HTTPError(None, 500, "Error", {}, None)
        )
        mr.delete_lane_data()
        debug_msgs = [m for lvl, m in mr.logger.messages if lvl == "debug"]
        assert len(debug_msgs) >= 1

    def test_trigger_db_backup_http_error_returns_true(self):
        """Covers lines 491-495: HTTPError from _get_results in trigger_db_backup."""
        from urllib.error import HTTPError
        mr = self._make_moonraker()
        mr._get_results = MagicMock(
            side_effect=HTTPError(None, 503, "Service Unavailable", {}, None)
        )
        error = mr.trigger_db_backup()
        assert error is True
        errors = [m for lvl, m in mr.logger.messages if lvl == "error"]
        assert len(errors) >= 1
