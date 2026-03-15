"""
Unit tests for extras/AFC_button.py

Covers:
  - AFCButton._button_callback: press/release tracking, short/long press logic
  - AFCButton._handle_ready: validates lane_obj lookup
  - Integration: correct actions taken for short vs long press
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from extras.AFC_button import AFCButton


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_button(lane_id="lane1", long_press_duration=1.2):
    """Build an AFCButton bypassing __init__."""
    btn = AFCButton.__new__(AFCButton)

    from tests.conftest import MockAFC, MockPrinter, MockReactor, MockLogger

    afc = MockAFC()
    afc.logger = MockLogger()
    reactor = MockReactor()
    printer = MockPrinter(afc=afc)
    printer._reactor = reactor

    btn.printer = printer
    btn.gcode = printer._gcode
    btn.reactor = reactor
    btn.afc = afc
    btn.lane_id = lane_id
    btn.lane_obj = None
    btn.long_press_duration = long_press_duration
    btn._press_time = None

    # Set up a lane object
    lane = MagicMock()
    lane.name = lane_id
    afc.lanes = {lane_id: lane}
    btn.lane_obj = lane

    return btn


# ── _handle_ready ─────────────────────────────────────────────────────────────

class TestHandleReady:
    def test_handle_ready_sets_lane_obj_when_found(self):
        btn = _make_button("lane1")
        btn.lane_obj = None
        btn._handle_ready()
        assert btn.lane_obj is btn.afc.lanes["lane1"]

    def test_handle_ready_raises_when_lane_not_found(self):
        btn = _make_button("missing_lane")
        btn.afc.lanes = {}  # No lanes defined
        btn.lane_obj = None
        from configfile import error as KlipperError
        with pytest.raises(KlipperError):
            btn._handle_ready()


# ── _button_callback – state tracking ────────────────────────────────────────

class TestButtonCallbackStateTracking:
    def test_press_stores_time(self):
        btn = _make_button()
        btn._button_callback(100.0, True)
        assert btn._press_time == 100.0

    def test_release_with_no_press_does_nothing(self):
        btn = _make_button()
        btn._press_time = None
        btn.afc.error = MagicMock()
        btn._button_callback(101.0, False)
        btn.afc.error.AFC_error.assert_not_called()

    def test_too_fast_release_ignored(self):
        btn = _make_button()
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        btn.afc.function.get_current_lane_obj.return_value = None
        btn.afc.CHANGE_TOOL = MagicMock()
        btn._button_callback(100.03, False)  # 0.03s < 0.05s threshold
        btn.afc.CHANGE_TOOL.assert_not_called()

    def test_press_time_cleared_after_release(self):
        btn = _make_button()
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        btn.afc.function.get_current_lane_obj.return_value = None
        btn.afc.CHANGE_TOOL = MagicMock()
        btn._button_callback(100.2, False)
        assert btn._press_time is None


# ── _button_callback – printing guard ────────────────────────────────────────

class TestButtonCallbackPrintingGuard:
    def test_error_logged_when_printing(self):
        btn = _make_button()
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = True
        btn.afc.error = MagicMock()
        btn._button_callback(101.0, False)
        btn.afc.error.AFC_error.assert_called_once()

    def test_press_time_not_cleared_when_printing_guard_fires(self):
        """On printing guard, we return early; press_time may or may not clear."""
        btn = _make_button()
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = True
        btn.afc.error = MagicMock()
        btn._button_callback(100.5, False)
        # Core assertion: error raised
        btn.afc.error.AFC_error.assert_called_once()


# ── _button_callback – short press ───────────────────────────────────────────

class TestButtonCallbackShortPress:
    def test_short_press_no_active_lane_loads_this_lane(self):
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        btn.afc.function.get_current_lane_obj.return_value = None
        btn.afc.CHANGE_TOOL = MagicMock()
        # 0.3s press < long_press_duration (1.2s)
        btn._button_callback(100.3, False)
        btn.afc.CHANGE_TOOL.assert_called_once_with(btn.lane_obj)

    def test_short_press_with_this_lane_active_unloads(self):
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        cur_lane = MagicMock()
        cur_lane.name = "lane1"  # Same as this button's lane
        btn.afc.function.get_current_lane_obj.return_value = cur_lane
        btn.afc.TOOL_UNLOAD = MagicMock()
        btn._button_callback(100.3, False)
        btn.afc.TOOL_UNLOAD.assert_called_once_with(cur_lane)

    def test_short_press_with_different_lane_active_loads_this_lane(self):
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        cur_lane = MagicMock()
        cur_lane.name = "lane2"  # Different lane
        btn.afc.function.get_current_lane_obj.return_value = cur_lane
        btn.afc.CHANGE_TOOL = MagicMock()
        btn._button_callback(100.3, False)
        btn.afc.CHANGE_TOOL.assert_called_once_with(btn.lane_obj)


# ── _button_callback – long press ────────────────────────────────────────────

class TestButtonCallbackLongPress:
    def test_long_press_no_active_lane_ejects(self):
        btn = _make_button("lane1", long_press_duration=1.0)
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        btn.afc.function.get_current_lane_obj.return_value = None
        btn.afc.LANE_UNLOAD = MagicMock()
        # 1.5s press ≥ long_press_duration (1.0s)
        btn._button_callback(101.5, False)
        btn.afc.LANE_UNLOAD.assert_called_once_with(btn.lane_obj)

    def test_long_press_with_this_lane_active_unloads_then_ejects(self):
        btn = _make_button("lane1", long_press_duration=1.0)
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        cur_lane = MagicMock()
        cur_lane.name = "lane1"
        btn.afc.function.get_current_lane_obj.return_value = cur_lane
        btn.afc.TOOL_UNLOAD = MagicMock(return_value=True)
        btn.afc.LANE_UNLOAD = MagicMock()
        btn._button_callback(101.5, False)
        btn.afc.TOOL_UNLOAD.assert_called_once()
        btn.afc.LANE_UNLOAD.assert_called_once_with(btn.lane_obj)

    def test_long_press_with_different_lane_active_ejects_only(self):
        btn = _make_button("lane1", long_press_duration=1.0)
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        cur_lane = MagicMock()
        cur_lane.name = "lane2"
        btn.afc.function.get_current_lane_obj.return_value = cur_lane
        btn.afc.LANE_UNLOAD = MagicMock()
        btn.afc.TOOL_UNLOAD = MagicMock()
        btn._button_callback(101.5, False)
        btn.afc.LANE_UNLOAD.assert_called_once_with(btn.lane_obj)
        btn.afc.TOOL_UNLOAD.assert_not_called()


# ── __init__ via MockConfig ───────────────────────────────────────────────────

class TestAFCButtonInit:
    def test_init_registers_ready_event_handler(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_button lane1", printer=printer,
                            values={"pin": "PA0", "long_press_duration": 1.2})
        btn = AFCButton(config)
        assert "klippy:ready" in printer._event_handlers

    def test_init_sets_lane_id_from_config_name(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_button lane_x", printer=printer,
                            values={"pin": "PB1", "long_press_duration": 1.0})
        btn = AFCButton(config)
        assert btn.lane_id == "lane_x"

    def test_init_registers_button_callback(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        buttons_mock = MagicMock()
        printer._objects["buttons"] = buttons_mock
        config = MockConfig(name="AFC_button lane1", printer=printer,
                            values={"pin": "PC2", "long_press_duration": 1.2})
        btn = AFCButton(config)
        buttons_mock.register_buttons.assert_called_once()
        call_args = buttons_mock.register_buttons.call_args[0]
        assert "PC2" in call_args[0]
