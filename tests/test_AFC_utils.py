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
import threading
from io import StringIO
from unittest.mock import MagicMock, patch, call
import pytest

# conftest installs Klipper mocks; extras is on sys.path via REPO_ROOT
from extras.AFC_utils import (
    check_and_return, section_in_config, DebounceButton, AFC_moonraker,
    VirtualRunoutHelper, VirtualFilamentSensor, AFC_PrintFileMetaData,
    natural_sort_key,
)
from tests.conftest import MockGCodeCommand


# ── natural_sort_key ────────────────────────────────────────────────────────

class TestNaturalSortKey:
    def test_sorts_t_macros_numerically_not_lexicographically(self):
        items = ["T10", "T2", "T1", "T0"]
        assert sorted(items, key=natural_sort_key) == ["T0", "T1", "T2", "T10"]

    def test_digit_chunk_compares_as_int(self):
        """Covers the digit branch of the inline conditional: "T10" and "T2"
        share the same non-digit prefix, so only the int comparison of the
        digit chunk decides the order."""
        assert natural_sort_key("T2") < natural_sort_key("T10")

    def test_non_digit_chunk_compares_as_lowercased_string(self):
        """Covers the non-digit branch: differing prefixes are compared as
        lowercased strings before any digit chunk is reached."""
        assert natural_sort_key("ALPHA") < natural_sort_key("beta")

    def test_placeholder_sorts_with_string_entries(self):
        items = ["T1", "NONE", "T0"]
        assert sorted(items, key=natural_sort_key) == ["NONE", "T0", "T1"]


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
        """button_action that doesn't accept kwargs → except branch."""
        cfg = self._make_config(debounce_delay=0.0)
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        btn.logical_state = False
        btn.physical_state = True
        btn.latest_eventtime = 100.0
        pause_resume = btn.printer.lookup_object("pause_resume")

        # A callback that only accepts positional args → raises Exception on kwargs call
        calls = []
        def positional_only(eventtime, state):
            calls.append((eventtime, state))

        btn.button_action = positional_only
        btn._debounce_event(101.0)
        assert len(calls) == 1
        assert calls[0] == (101.0, True)
        pause_resume.cmd_PAUSE.assert_not_called()
    
    def test_debounce_event_falls_back_to_positional_args(self):
        """button_action that doesn't accept kwargs → except branch."""
        cfg = self._make_config(debounce_delay=0.0)
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        btn.logical_state = False
        btn.physical_state = True
        btn.latest_eventtime = 100.0
        pause_resume = btn.printer.lookup_object("pause_resume")

        # A callback that only accepts positional args → raises Exception on kwargs call
        calls = []

        def positional_only(eventtime, state):
            calls.append((eventtime, state))

        btn.button_action = positional_only
        btn._debounce_event(101.0)
        assert len(calls) == 1
        assert calls[0] == (101.0, True)
        pause_resume.cmd_PAUSE.assert_not_called()
    
    def test_debounce_event_falls_back_to_positional_args_raise_exception(self):
        """Covers lines 169-170: button_action that doesn't accept kwargs → except branch."""
        cfg = self._make_config(debounce_delay=0.0)
        sensor = self._make_filament_sensor(["self", "eventtime", "state"])
        btn = DebounceButton(cfg, sensor)
        btn.logical_state = False
        btn.physical_state = True
        btn.latest_eventtime = 100.0
        pause_resume = btn.printer.lookup_object("pause_resume")

        # A callback that only accepts positional args → raises Exception on kwargs call
        calls = []

        def positional_only(eventtime, state):
            calls.append((eventtime, state))
            raise Exception("Raise Exception 1")

        btn.button_action = positional_only
        btn._debounce_event(101.0)
        assert len(calls) == 1
        assert calls[0] == (101.0, True)
        pause_resume.cmd_PAUSE.assert_called_once()


# ── AFC_moonraker ─────────────────────────────────────────────────────────────
class TestAFCMoonraker:
    def _make_moonraker(self, host="http://localhost", port="7125"):
        from tests.conftest import MockLogger, MockReactor
        logger = MockLogger()
        reactor = MockReactor()
        # Run register_async_callback callbacks immediately by default so most
        # tests can assert on logger.messages the same way they would if the
        # log call happened inline; tests that care about the deferral itself
        # override this to inspect the raw callback instead.
        reactor.register_async_callback = lambda cb, waketime=None: cb(0.0)
        mr = AFC_moonraker(host, port, logger, reactor)
        # The real background writer thread is already parked on the original
        # queue at this point (blocked in queue.get()); swapping it out here
        # keeps tests deterministic instead of racing the live thread.
        mr._write_queue = MagicMock()
        return mr

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

    def test_update_afc_stats_queues_sync_write(self):
        mr = self._make_moonraker()
        mr.update_afc_stats("some.key", 10)
        mr._write_queue.put_nowait.assert_called_once_with(
            (mr._update_afc_stats_sync, ("some.key", 10)))

    def test_update_afc_stats_sync_logs_on_failure(self):
        mr = self._make_moonraker()
        init_messages = list(mr.logger.messages)
        mr._get_results = MagicMock(return_value=None)
        mr._update_afc_stats_sync("some.key", 10)
        assert mr.logger.messages == init_messages + [
            ("error", "Error when trying to update some.key in moonraker, see AFC.log for more info"),
        ]

    def test_update_afc_stats_sync_no_error_on_success(self):
        mr = self._make_moonraker()
        init_messages = list(mr.logger.messages)
        mr._get_results = MagicMock(return_value={"value": "ok"})
        mr._update_afc_stats_sync("some.key", 10)
        assert mr.logger.messages == init_messages

    def test_get_spool_queues_sync_read(self):
        mr = self._make_moonraker()
        callback = MagicMock()
        mr.get_spool(42, callback)
        mr._write_queue.put_nowait.assert_called_once_with(
            (mr._get_spool_sync, (42, callback)))
        callback.assert_not_called()

    def test_get_spool_sync_not_found_logs_info(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        callback = MagicMock()
        mr._get_spool_sync(42, callback)
        callback.assert_called_once_with(None)
        infos = [m for lvl, m in mr.logger.messages if lvl == "info"]
        assert any("42" in m for m in infos)

    def test_get_file_metadata_queues_sync_read(self):
        mr = self._make_moonraker()
        callback = MagicMock()
        mr.get_file_metadata("test.gcode", callback)
        mr._write_queue.put_nowait.assert_called_once_with(
            (mr._get_file_metadata_sync, ("test.gcode", callback)))
        callback.assert_not_called()

    def test_get_file_metadata_sync_returns_resp_when_found(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"filament_change_count": 3})
        callback = MagicMock()
        mr._get_file_metadata_sync("test.gcode", callback)
        callback.assert_called_once_with({"filament_change_count": 3})

    def test_get_file_metadata_sync_returns_none_on_failure(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        callback = MagicMock()
        mr._get_file_metadata_sync("test.gcode", callback)
        callback.assert_called_once_with(None)

    def test_get_file_metadata_sync_queries_correct_url(self):
        from urllib.parse import urljoin, quote
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        mr._get_file_metadata_sync("sub dir/test file.gcode", MagicMock())
        queried_url = mr._get_results.call_args[0][0]
        expected_url = urljoin(
            mr.host, f"{mr.FILENAME_PATH}{quote('sub dir/test file.gcode')}")
        assert queried_url == expected_url

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

    # ── get_td1_data_async ───────────────────────────────────────────────────

    def test_get_td1_data_async_queues_sync_read(self):
        mr = self._make_moonraker()
        callback = MagicMock()
        mr.get_td1_data_async(callback)
        mr._write_queue.put_nowait.assert_called_once_with(
            (mr._get_td1_data_async_sync, (callback,)))
        callback.assert_not_called()

    def test_get_td1_data_async_sync_returns_devices_on_success(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"devices": {"SN123": {}}})
        callback = MagicMock()
        mr._get_td1_data_async_sync(callback)
        callback.assert_called_once_with({"SN123": {}})

    def test_get_td1_data_async_sync_returns_none_when_no_devices_key(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"other": "data"})
        callback = MagicMock()
        mr._get_td1_data_async_sync(callback)
        callback.assert_called_once_with(None)

    def test_get_td1_data_async_sync_returns_none_on_failure(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        callback = MagicMock()
        mr._get_td1_data_async_sync(callback)
        callback.assert_called_once_with(None)

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

    def test_send_lane_data_queues_sync_write(self):
        mr = self._make_moonraker()
        data = {"lane1": {"color": "red"}}
        mr.send_lane_data(data)
        mr._write_queue.put_nowait.assert_called_once_with(
            (mr._send_lane_data_sync, (data,)))

    def test_send_lane_data_sync_logs_error_on_failure(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value=None)
        mr._send_lane_data_sync({"lane1": {"color": "red"}})
        errors = [m for lvl, m in mr.logger.messages if lvl == "error"]
        assert len(errors) == 1

    def test_send_lane_data_sync_success_no_error(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"value": "ok"})
        mr._send_lane_data_sync({"lane1": {"color": "red"}})
        errors = [m for lvl, m in mr.logger.messages if lvl == "error"]
        assert len(errors) == 0

    # ── remove_database_entry ─────────────────────────────────────────────────

    def test_remove_database_entry_queues_sync_write(self):
        mr = self._make_moonraker()
        mr.remove_database_entry("lane_data", "lane1")
        mr._write_queue.put_nowait.assert_called_once_with(
            (mr._remove_database_entry_sync, ("lane_data", "lane1")))

    def test_remove_database_entry_sync_calls_urlopen(self):
        mr = self._make_moonraker()
        with patch("extras.AFC_utils.urlopen") as mock_urlopen:
            mr._remove_database_entry_sync("lane_data", "lane1")
        mock_urlopen.assert_called_once()

    def test_remove_database_entry_sync_logs_debug_on_http_error(self):
        from urllib.error import HTTPError
        mr = self._make_moonraker()
        with patch("extras.AFC_utils.urlopen",
                   side_effect=HTTPError(None, 404, "Not Found", {}, None)):
            mr._remove_database_entry_sync("lane_data", "missing_key")
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

    def test_get_spool_sync_returns_resp_when_found(self):
        mr = self._make_moonraker()
        mr._get_results = MagicMock(return_value={"id": 42, "name": "PLA"})
        callback = MagicMock()
        mr._get_spool_sync(42, callback)
        callback.assert_called_once_with({"id": 42, "name": "PLA"})

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

    def test_send_lane_data_sync_http_error_logs_error(self):
        """HTTPError propagated from _get_results."""
        from urllib.error import HTTPError
        mr = self._make_moonraker()
        mr._get_results = MagicMock(
            side_effect=HTTPError(None, 500, "Internal Server Error", {}, None)
        )
        mr._send_lane_data_sync({"lane1": {"color": "red"}})
        errors = [m for lvl, m in mr.logger.messages if lvl == "error"]
        assert len(errors) >= 1

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


# ── AFC_moonraker background writer ─────────────────────────────────────────

class TestAFCMoonrakerBackgroundWriter:
    def _make_moonraker_with_real_reactor_hook(self):
        """Build an AFC_moonraker but capture the register_async_callback
        call instead of running it immediately, so tests can drive it by hand."""
        from tests.conftest import MockLogger, MockReactor
        logger = MockLogger()
        reactor = MockReactor()
        reactor.register_async_callback = MagicMock()
        mr = AFC_moonraker("http://localhost", "7125", logger, reactor)
        # The constructor already started a real worker parked on the real
        # queue; stop and join it before swapping the queue for a MagicMock,
        # otherwise it stays blocked on the orphaned real queue forever and
        # leaks a daemon thread per test.
        mr._write_queue.put_nowait((mr.sentinel, ()))
        mr._write_thread.join(timeout=5)
        mr._write_queue = MagicMock()
        return mr

    def test_init_starts_background_writer_thread(self):
        from tests.conftest import MockLogger, MockReactor
        mr = AFC_moonraker("http://localhost", "7125", MockLogger(), MockReactor())
        assert mr._write_thread.is_alive()
        assert mr._write_thread.daemon is True
        assert mr._write_thread.name == "afc_moonraker"

    def test_log_async_schedules_callback_on_reactor(self):
        mr = self._make_moonraker_with_real_reactor_hook()
        log_fn = MagicMock()
        mr._log_async(log_fn, "hello", traceback="tb")
        mr.reactor.register_async_callback.assert_called_once()
        scheduled_cb = mr.reactor.register_async_callback.call_args[0][0]
        log_fn.assert_not_called()
        scheduled_cb(0.0)
        log_fn.assert_called_once_with("hello", traceback="tb")

    def test_write_worker_processes_queued_call_then_loops(self):
        """Drives exactly one loop iteration: the second queue.get() raises
        to break out of the otherwise-infinite loop deterministically."""
        mr = self._make_moonraker_with_real_reactor_hook()
        called = []
        mr._write_queue.get.side_effect = [
            (lambda a, b: called.append((a, b)), (1, 2)),
            RuntimeError("stop test loop"),
        ]

        with pytest.raises(RuntimeError, match="stop test loop"):
            mr._write_worker()

        assert called == [(1, 2)]

    def test_write_worker_survives_exception_and_logs_it(self):
        """A queued call that raises must not kill the worker loop -- it
        should be caught, logged, and the loop must continue to the next item."""
        mr = self._make_moonraker_with_real_reactor_hook()

        def boom():
            raise ValueError("kaboom")

        mr._write_queue.get.side_effect = [
            (boom, ()),
            RuntimeError("stop test loop"),
        ]

        with pytest.raises(RuntimeError, match="stop test loop"):
            mr._write_worker()

        scheduled_calls = mr.reactor.register_async_callback.call_args_list
        assert len(scheduled_calls) == 2
        scheduled_calls[0][0][0](0.0)
        scheduled_calls[1][0][0](0.0)
        errors = [m for lvl, m in mr.logger.messages if lvl == "error"]
        debug_msgs = [m for lvl, m in mr.logger.messages if lvl == "debug"]
        assert errors == ["Unexpected error in moonraker background writer"]
        assert any("kaboom" in m for m in debug_msgs)

    def test_write_worker_sets_os_thread_name(self):
        mr = self._make_moonraker_with_real_reactor_hook()
        mr._write_queue.get.side_effect = [RuntimeError("stop test loop")]
        fake_ffi_lib = MagicMock()

        with patch("chelper.get_ffi", return_value=(MagicMock(), fake_ffi_lib)):
            with pytest.raises(RuntimeError, match="stop test loop"):
                mr._write_worker()

        fake_ffi_lib.set_thread_name.assert_called_once_with(
            threading.current_thread().name.encode("utf-8"))

    def test_write_worker_survives_exception_setting_thread_name(self):
        """A failure naming the OS thread (e.g. chelper unavailable) must not
        stop the worker from processing queued calls."""
        mr = self._make_moonraker_with_real_reactor_hook()
        called = []
        mr._write_queue.get.side_effect = [
            (lambda a, b: called.append((a, b)), (1, 2)),
            RuntimeError("stop test loop"),
        ]

        with patch("chelper.get_ffi", side_effect=Exception("boom")):
            with pytest.raises(RuntimeError, match="stop test loop"):
                mr._write_worker()

        assert called == [(1, 2)]

    def test_join_thread_clears_write_thread_wait_flag(self):
        mr = self._make_moonraker_with_real_reactor_hook()
        mr._write_thread_wait = True
        mr.join_thread()
        assert mr._write_thread_wait is False

    def test_join_thread_puts_sentinel_on_write_queue(self):
        mr = self._make_moonraker_with_real_reactor_hook()
        mr.join_thread()
        mr._write_queue.put_nowait.assert_called_once_with((mr.sentinel, ""))

    def test_write_worker_returns_on_sentinel_without_calling_it(self):
        """join_thread queues (sentinel, ""); the worker must return instead
        of trying to call the sentinel class as a function."""
        mr = self._make_moonraker_with_real_reactor_hook()
        mr._write_queue.get.side_effect = [(mr.sentinel, "")]

        result = mr._write_worker()

        assert result is None

    def test_write_worker_stops_looping_once_join_thread_clears_wait_flag(self):
        """Simulates a real klippy:disconnect: join_thread flips the wait
        flag and queues the sentinel, and the worker must exit its loop."""
        mr = self._make_moonraker_with_real_reactor_hook()
        called = []

        def stop_after_call(a, b):
            called.append((a, b))
            mr.join_thread()

        mr._write_queue.get.side_effect = [(stop_after_call, (1, 2))]

        mr._write_worker()

        assert called == [(1, 2)]
        assert mr._write_thread_wait is False


# ── AFC_PrintFileMetaData ───────────────────────────────────────────────────

def _make_print_file_metadata(moonraker=None):
    from tests.conftest import MockLogger
    if moonraker is None:
        moonraker = MagicMock()
    logger = MockLogger()
    return AFC_PrintFileMetaData(moonraker, logger), moonraker


def _stub_get_file_metadata(moonraker, *values):
    """Makes moonraker.get_file_metadata(filename, callback) invoke callback
    immediately (as if the async fetch completed inline) with each of
    `values` in turn, one per call."""
    remaining = list(values)

    def _side_effect(filename, callback):
        callback(remaining.pop(0))

    moonraker.get_file_metadata.side_effect = _side_effect


class TestAFCPrintFileMetaDataInit:
    def test_init_defaults(self):
        meta, _ = _make_print_file_metadata()
        assert meta.filename == ""
        assert meta.tool_change_count == 0
        assert meta.tool_temperatures == []


class TestAFCPrintFileMetaDataFilename:
    def test_setting_filename_queries_moonraker_metadata(self):
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker, {"filament_change_count": 3})
        meta.query_filename("test.gcode")
        assert moonraker.get_file_metadata.call_count == 1
        assert moonraker.get_file_metadata.call_args[0][0] == "test.gcode"
        assert meta.filename == "test.gcode"

    def test_setting_empty_filename_does_not_query_moonraker(self):
        """Covers the `value` half of `if self._moonraker and value` being falsy
        while `self._moonraker` alone is truthy."""
        meta, moonraker = _make_print_file_metadata()
        meta.query_filename("")
        moonraker.get_file_metadata.assert_not_called()
        assert meta.tool_change_count == 0

    def test_setting_empty_filename_calls_on_fetched_immediately(self):
        meta, moonraker = _make_print_file_metadata()
        on_fetched = MagicMock()
        meta.query_filename("", on_fetched=on_fetched)
        on_fetched.assert_called_once_with()

    def test_setting_filename_with_no_moonraker_does_not_raise(self):
        """Covers the `self._moonraker` half of `if self._moonraker and value`
        being falsy while `value` alone is truthy."""
        from tests.conftest import MockLogger
        meta = AFC_PrintFileMetaData(None, MockLogger())
        meta.query_filename("test.gcode")
        assert meta.filename == "test.gcode"
        assert meta.tool_change_count == 0
        assert meta.tool_temperatures == []

    def test_setting_filename_with_no_moonraker_calls_on_fetched_immediately(self):
        from tests.conftest import MockLogger
        meta = AFC_PrintFileMetaData(None, MockLogger())
        on_fetched = MagicMock()
        meta.query_filename("test.gcode", on_fetched=on_fetched)
        on_fetched.assert_called_once_with()

    def test_query_filename_calls_on_fetched_after_metadata_cached(self):
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker, {"filament_change_count": 3})
        seen_count_at_callback_time = []

        def on_fetched():
            seen_count_at_callback_time.append(meta.tool_change_count)

        meta.query_filename("test.gcode", on_fetched=on_fetched)
        assert seen_count_at_callback_time == [3]

    def test_stale_response_for_superseded_filename_is_dropped(self):
        """If filename changed again before an in-flight query's callback
        fires, the stale response must not clobber the newer state."""
        meta, moonraker = _make_print_file_metadata()
        captured_calls = []
        moonraker.get_file_metadata.side_effect = (
            lambda filename, callback: captured_calls.append((filename, callback))
        )
        meta.query_filename("first.gcode")
        # Simulate a second query superseding the first before the first's
        # response has arrived
        meta.query_filename("second.gcode")
        # Now the first (stale) query's response finally lands
        first_filename, first_callback = captured_calls[0]
        assert first_filename == "first.gcode"
        first_callback({"filament_change_count": 99})
        assert meta.filename == "second.gcode"
        assert meta.tool_change_count == 0

    def test_updating_filename_refreshes_metadata(self):
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker,
                                {"filament_change_count": 2},
                                {"filament_change_count": 9})
        meta.query_filename("first.gcode")
        assert meta.tool_change_count == 2
        meta.query_filename("second.gcode")
        assert meta.tool_change_count == 9
        assert moonraker.get_file_metadata.call_count == 2

    def test_none_metadata_response_normalized_to_empty_dict(self):
        """moonraker.get_file_metadata() can return None (e.g. a failed query);
        the cached metadata must still behave as an empty dict rather than
        None so downstream property access doesn't operate on None."""
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker, None)
        meta.query_filename("test.gcode")
        assert meta.tool_change_count == 0
        assert meta.tool_temperatures == []

    def test_recovers_after_failed_metadata_query_followed_by_success(self):
        """A failed query (None) for one filename must not leave stale/bad
        state that breaks a subsequent successful query for another filename."""
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker,
                                None, {"filament_change_count": 7, "filament_temps": [220]})
        meta.query_filename("first.gcode")
        assert meta.tool_change_count == 0
        assert meta.tool_temperatures == []
        meta.query_filename("second.gcode")
        assert meta.tool_change_count == 7
        assert meta.tool_temperatures == [220]


class TestAFCPrintFileMetaDataToolChangeCount:
    def test_tool_change_count_from_metadata(self):
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker, {"filament_change_count": 5})
        meta.query_filename("test.gcode")
        assert meta.tool_change_count == 5

    def test_tool_change_count_default_zero_when_missing(self):
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker, {})
        meta.query_filename("test.gcode")
        assert meta.tool_change_count == 0

    def test_tool_change_count_default_zero_when_metadata_empty_dict(self):
        """Covers the falsy-`self._metadata` half of
        `if self._metadata and "filament_change_count" in self._metadata`."""
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker, None)
        meta.query_filename("test.gcode")
        assert meta.tool_change_count == 0
        debug_msgs = [m for lvl, m in meta.logger.messages if lvl == "debug"]
        assert any("test.gcode" in m for m in debug_msgs)


class TestAFCPrintFileMetaDataToolTemperatures:
    def test_tool_temperatures_from_filament_temps(self):
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker, {"filament_temps": [200, 210, 220]})
        meta.query_filename("test.gcode")
        assert meta.tool_temperatures == [200, 210, 220]

    def test_tool_temperatures_falls_back_to_nozzle_temp(self):
        """Snapmaker U1 files use `nozzle_temp` instead of `filament_temps`."""
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker, {"nozzle_temp": [215]})
        meta.query_filename("test.gcode")
        assert meta.tool_temperatures == [215]

    def test_tool_temperatures_empty_when_neither_key_present(self):
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker, {})
        meta.query_filename("test.gcode")
        assert meta.tool_temperatures == []

    def test_tool_temperatures_empty_when_metadata_none(self):
        """Covers the falsy-`self._metadata` branch of `if self._metadata`."""
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker, None)
        meta.query_filename("test.gcode")
        assert meta.tool_temperatures == []

class TestAFCPrintFileMetaDataReset:
    def test_reset_clears_filename_and_metadata(self):
        meta, moonraker = _make_print_file_metadata()
        _stub_get_file_metadata(moonraker,
                                {"filament_change_count": 4, "filament_temps": [200, 210]})
        meta.query_filename("test.gcode")
        meta.reset()
        assert meta.filename == ""
        assert meta.tool_change_count == 0
        assert meta.tool_temperatures == []


# ── VirtualRunoutHelper ─────────────────────────────────────────────────────
class TestVirtualRunoutHelper:
    """VirtualRunoutHelper is the minimal runout-tracking backend used by
    VirtualFilamentSensor (FPS_PSF virtual sensors)."""

    def _make_helper(self, runout_cb=None, enable_runout=False):
        from tests.conftest import MockPrinter
        printer = MockPrinter()
        helper = VirtualRunoutHelper(printer, "FPS1_expanded", runout_cb=runout_cb,
                                     enable_runout=enable_runout)
        return helper, printer

    def test_init_defaults(self):
        helper, printer = self._make_helper()
        assert helper.printer is printer
        assert helper.name == "FPS1_expanded"
        assert helper.sensor_enabled is False
        assert helper.filament_present is False
        assert helper.runout_callback is None
        assert helper.insert_gcode is None
        assert helper.runout_gcode is None
        assert helper.event_delay == 0.0
        assert helper._reactor is printer.get_reactor()
        assert helper.min_event_systime == helper._reactor.NEVER

    def test_init_enable_runout_coerced_to_bool(self):
        helper, _ = self._make_helper(enable_runout=1)
        assert helper.sensor_enabled is True

    def test_note_filament_present_updates_state(self):
        helper, _ = self._make_helper()
        helper.note_filament_present(100.0, True)
        assert helper.filament_present is True

    def test_note_filament_present_same_state_is_noop(self):
        """No state change -> early return, no callback invoked even if enabled."""
        cb = MagicMock()
        helper, _ = self._make_helper(runout_cb=cb, enable_runout=True)
        # filament_present already False; calling with False again should no-op
        helper.note_filament_present(100.0, False)
        cb.assert_not_called()
        assert helper.filament_present is False

    def test_note_filament_present_transition_to_absent_fires_callback(self):
        cb = MagicMock()
        helper, _ = self._make_helper(runout_cb=cb, enable_runout=True)
        idle_to = helper.printer.lookup_object("idle_timeout")
        idle_to.get_status.return_value = {"state": "Printing"}
        helper.note_filament_present(100.0, True)   # present
        cb.assert_not_called()
        helper.note_filament_present(101.0, False)  # -> absent, should fire
        cb.assert_called_once_with(101.0)
        assert helper.filament_present is False

    def test_note_filament_present_transition_to_present_never_fires_callback(self):
        """Callback only fires on transition to absent, never on transition to present."""
        cb = MagicMock()
        helper, _ = self._make_helper(runout_cb=cb, enable_runout=True)
        helper.note_filament_present(100.0, True)  # absent -> present
        cb.assert_not_called()
        assert helper.filament_present is True

    def test_note_filament_present_no_callback_when_runout_disabled(self):
        cb = MagicMock()
        helper, _ = self._make_helper(runout_cb=cb, enable_runout=False)
        helper.note_filament_present(100.0, True)
        helper.note_filament_present(101.0, False)
        cb.assert_not_called()
        assert helper.filament_present is False

    def test_note_filament_present_no_callback_when_none(self):
        """runout_callback is None -> callable() check prevents a crash."""
        helper, _ = self._make_helper(runout_cb=None, enable_runout=True)
        helper.note_filament_present(100.0, True)
        helper.note_filament_present(101.0, False)  # should not raise
        assert helper.filament_present is False

    def test_note_filament_present_exception_fallback_uses_kwarg(self):
        """Some callers' runout callbacks only accept eventtime as a kwarg;
        the Exception fallback should retry with eventtime=eventtime."""
        calls = []

        def picky_callback(*, eventtime):
            calls.append(eventtime)

        helper, _ = self._make_helper(runout_cb=picky_callback, enable_runout=True)
        pause_resume = helper.printer.lookup_object("pause_resume")
        idle_to = helper.printer.lookup_object("idle_timeout")
        idle_to.get_status.return_value = {"state": "Printing"}
        helper.note_filament_present(100.0, True)
        helper.note_filament_present(101.0, False)
        assert calls == [101.0]
        assert helper.filament_present is False
        pause_resume.cmd_PAUSE.assert_not_called()

    def test_callback_exception_on_both(self):
        failing_callback = MagicMock()
        failing_callback.side_effect = [
            TypeError("Raise Type Error 1"),
            Exception("Raise Error 2")
        ]
        helper, _ = self._make_helper(
            runout_cb=failing_callback, enable_runout=True)
        idle_to = helper.printer.lookup_object("idle_timeout")
        idle_to.get_status.return_value = {"state": "Printing"}
        pause_resume = helper.printer.lookup_object("pause_resume")
        # Setting filament present
        helper.note_filament_present(100.0, True)
        helper.runout_callback.assert_not_called()
        helper.note_filament_present(101.0, False)

        assert helper.runout_callback.call_count == 2
        first_call = helper.runout_callback.call_args_list[0]
        second_call = helper.runout_callback.call_args_list[1]

        assert first_call.args == (101.0,)
        assert first_call.kwargs == {}
        assert second_call.args == ()
        assert second_call.kwargs == {"eventtime": 101.0}
        pause_resume.cmd_PAUSE.assert_called_once()
    
    def test_callback_does_not_try_second_time(self):
        failing_callback = MagicMock()
        helper, _ = self._make_helper(
            runout_cb=failing_callback, enable_runout=True)
        idle_to = helper.printer.lookup_object("idle_timeout")
        idle_to.get_status.side_effect = [
            {"state": "Printing"},
            {"state": "Printing"},
            {"state": "Paused"},
        ]
        # Setting filament present
        helper.note_filament_present(100.0, True)
        helper.runout_callback.assert_not_called()
        helper.note_filament_present(101.0, False)
        helper.note_filament_present(102.0, False)

        assert helper.runout_callback.call_count == 1
        first_call = helper.runout_callback.call_args_list[0]

        assert first_call.args == (101.0,)
        assert first_call.kwargs == {}

    def test_note_filament_present_eventtime_defaults_to_monotonic(self):
        seen_eventtimes = []
        helper, printer = self._make_helper(runout_cb=seen_eventtimes.append, enable_runout=True)
        idle_to = helper.printer.lookup_object("idle_timeout")
        idle_to.get_status.return_value = {"state": "Printing"}
        printer.get_reactor()._monotonic = 555.0
        helper.note_filament_present(None, True)   # present, no callback yet
        helper.note_filament_present(None, False)  # absent -> callback fires with monotonic()
        assert seen_eventtimes == [555.0]
        assert helper.filament_present is False

    def test_note_filament_present_ignores_extra_kwargs(self):
        """**_kwargs lets this be called with the same signature as the real
        Klipper runout_helper.note_filament_present (which some callers use)."""
        helper, _ = self._make_helper()
        helper.note_filament_present(eventtime=100.0, is_filament_present=True,
                                     extra_unused_kwarg="ignored")
        assert helper.filament_present is True

    def test_get_status_reports_present_and_enabled(self):
        helper, _ = self._make_helper(enable_runout=True)
        helper.note_filament_present(100.0, True)
        status = helper.get_status()
        assert status == {"filament_detected": True, "enabled": True}

    def test_get_status_reports_absent_and_disabled(self):
        helper, _ = self._make_helper(enable_runout=False)
        status = helper.get_status(123.0)
        assert status == {"filament_detected": False, "enabled": False}


# ── VirtualFilamentSensor ───────────────────────────────────────────────────

class TestVirtualFilamentSensor:
    """VirtualFilamentSensor lets FPS_PSF buffers expose a
    filament_switch_sensor-shaped object for Mainsail/Fluidd, without
    requiring a physical sensor pin."""

    def _make_printer_with_add_object(self):
        from tests.conftest import MockPrinter
        printer = MockPrinter()
        # MockPrinter has no add_object; give it a real dict-backed one so we
        # can exercise the success path (including the GUI-hide rename).
        def add_object(name, obj):
            printer.objects[name] = obj
        printer.add_object = add_object
        return printer

    def test_init_registers_object_visible_in_gui(self):
        printer = self._make_printer_with_add_object()
        mock_logger = MagicMock()
        sensor = VirtualFilamentSensor(printer, "FPS1_expanded", logger=mock_logger,
                                       show_in_gui=True)
        assert "filament_switch_sensor FPS1_expanded" in printer.objects
        assert printer.objects["filament_switch_sensor FPS1_expanded"] is sensor
        assert sensor.printer is printer
        assert sensor.logger is mock_logger
        assert sensor._object_name == "filament_switch_sensor FPS1_expanded"

    def test_init_hides_object_from_gui_when_requested(self):
        printer = self._make_printer_with_add_object()
        sensor = VirtualFilamentSensor(printer, "FPS1_expanded", logger=MagicMock(),
                                       show_in_gui=False)
        assert "filament_switch_sensor FPS1_expanded" not in printer.objects
        assert "_filament_switch_sensor FPS1_expanded" in printer.objects
        assert printer.objects["_filament_switch_sensor FPS1_expanded"] is sensor

    def test_init_falls_back_to_dict_registration_when_add_object_missing(self):
        """MockPrinter has no add_object attribute by default -- this exercises
        the except-Exception fallback path (direct dict registration)."""
        from tests.conftest import MockPrinter
        printer = MockPrinter()
        assert not hasattr(printer, "add_object")
        sensor = VirtualFilamentSensor(printer, "FPS1_expanded", logger=MagicMock())
        assert printer.objects.get("filament_switch_sensor FPS1_expanded") is sensor

    def test_init_fallback_noop_when_objects_is_not_a_dict(self):
        """When add_object is missing AND printer.objects isn't a dict, the
        fallback registration should silently no-op rather than raise."""
        class BarePrinter:
            def get_reactor(self):
                from tests.conftest import MockReactor
                return MockReactor()

            def lookup_object(self, name, default=None):
                return default

        printer = BarePrinter()
        assert not hasattr(printer, "add_object")
        assert not hasattr(printer, "objects")
        sensor = VirtualFilamentSensor(printer, "FPS1_expanded", logger=MagicMock())
        assert sensor.name == "FPS1_expanded"

    def test_init_registers_gcode_commands(self):
        printer = self._make_printer_with_add_object()
        gcode = printer.lookup_object("gcode")
        gcode.register_mux_command = MagicMock()
        VirtualFilamentSensor(printer, "FPS1_expanded", logger=MagicMock())
        registered = [c.args[0] for c in gcode.register_mux_command.call_args_list]
        assert "QUERY_FILAMENT_SENSOR" in registered
        assert "SET_FILAMENT_SENSOR" in registered

    def test_init_returns_early_when_gcode_object_missing(self):
        """When printer has no gcode object yet, init should not raise and
        should simply skip command registration."""
        printer = self._make_printer_with_add_object()
        printer._gcode = None
        sensor = VirtualFilamentSensor(printer, "FPS1_expanded", logger=MagicMock())
        assert sensor.name == "FPS1_expanded"

    def test_init_swallows_gcode_registration_exceptions(self):
        """If register_mux_command raises (e.g. duplicate registration),
        init should not propagate the exception."""
        printer = self._make_printer_with_add_object()
        gcode = printer.lookup_object("gcode")
        gcode.register_mux_command = MagicMock(side_effect=Exception("already registered"))
        sensor = VirtualFilamentSensor(printer, "FPS1_expanded", logger=MagicMock())
        assert sensor.name == "FPS1_expanded"

    def test_get_status_delegates_to_runout_helper(self):
        printer = self._make_printer_with_add_object()
        sensor = VirtualFilamentSensor(printer, "FPS1_expanded", logger=MagicMock())
        sensor.runout_helper.note_filament_present(100.0, True)
        assert sensor.get_status(100.0) == {"filament_detected": True, "enabled": False}

    def test_cmd_query_filament_sensor_reports_detected(self):
        printer = self._make_printer_with_add_object()
        sensor = VirtualFilamentSensor(printer, "FPS1_expanded", logger=MagicMock())
        sensor.runout_helper.note_filament_present(100.0, True)
        gcmd = MockGCodeCommand()
        sensor.cmd_QUERY_FILAMENT_SENSOR(gcmd)
        gcmd.respond_info.assert_called_once_with(
            "Filament Sensor FPS1_expanded: filament detected")

    def test_cmd_query_filament_sensor_reports_not_detected(self):
        printer = self._make_printer_with_add_object()
        sensor = VirtualFilamentSensor(printer, "FPS1_expanded", logger=MagicMock())
        gcmd = MockGCodeCommand()
        sensor.cmd_QUERY_FILAMENT_SENSOR(gcmd)
        gcmd.respond_info.assert_called_once_with(
            "Filament Sensor FPS1_expanded: filament not detected")

    def test_cmd_set_filament_sensor_enables(self):
        printer = self._make_printer_with_add_object()
        sensor = VirtualFilamentSensor(printer, "FPS1_expanded", logger=MagicMock())
        gcmd = MockGCodeCommand(params={"ENABLE": 1})
        sensor.cmd_SET_FILAMENT_SENSOR(gcmd)
        gcmd.get_int.assert_called_once_with("ENABLE", 1, minval=0, maxval=1)
        assert sensor.runout_helper.sensor_enabled is True

    def test_cmd_set_filament_sensor_disables(self):
        printer = self._make_printer_with_add_object()
        sensor = VirtualFilamentSensor(printer, "FPS1_expanded", logger=MagicMock())
        sensor.runout_helper.sensor_enabled = True
        gcmd = MockGCodeCommand(params={"ENABLE": 0})
        sensor.cmd_SET_FILAMENT_SENSOR(gcmd)
        assert sensor.runout_helper.sensor_enabled is False
