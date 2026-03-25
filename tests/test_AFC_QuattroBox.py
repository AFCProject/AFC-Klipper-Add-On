"""
Unit tests for extras/AFC_QuattroBox.py

Covers:
  - afcQuattroBox: is subclass of afcNightOwl
  - lane_loaded: calls super().lane_loaded() then sets spool illumination LED
  - lane_unloaded: calls afc_led with led_off for spool index
  - lane_loading: calls super().lane_loading() then set_logo_color
  - lane_tool_loaded: calls super().lane_tool_loaded() then set_logo_color with lane color
  - lane_tool_unloaded: calls super().lane_tool_unloaded() then resets logo color
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch
import pytest

from extras.AFC_QuattroBox import afcQuattroBox
from extras.AFC_NightOwl import afcNightOwl


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_quattro(name="Quattro_1"):
    """Build an afcQuattroBox bypassing the complex __init__."""
    unit = afcQuattroBox.__new__(afcQuattroBox)

    from tests.conftest import MockAFC, MockPrinter, MockLogger, MockReactor

    afc = MockAFC()
    reactor = MockReactor()
    afc.reactor = reactor
    afc.logger = MockLogger()
    printer = MockPrinter(afc=afc)

    unit.printer = printer
    unit.afc = afc
    unit.logger = afc.logger
    unit.reactor = reactor
    unit.name = name
    unit.full_name = ["AFC_QuattroBox", name]
    unit.lanes = {}
    unit.hub_obj = None
    unit.extruder_obj = None
    unit.buffer_obj = None
    unit.hub = None
    unit.extruder = None
    unit.buffer_name = None
    unit.td1_defined = False
    unit.type = "Quattro_Box"
    unit.gcode = afc.gcode
    unit.led_logo_index = "0"
    unit.led_logo_color = "0,0,0,0"
    unit.led_logo_loading = "0,0,1,0"

    return unit


def _make_lane(name="lane1", color="#FF0000"):
    lane = MagicMock()
    lane.name = name
    lane.color = color
    lane.led_ready = "0,1,0,0"
    lane.led_not_ready = "0,0,0,0.25"
    lane.led_fault = "1,0,0,0"
    lane.led_loading = "0,0,1,0"
    lane.led_spool_illum = "1,1,1,0"
    lane.led_tool_loaded = "0,1,0.5,0"
    lane.led_index = "1"
    lane.led_spool_index = "2"
    lane.led_use_filament_color = False
    return lane


# ── Inheritance ───────────────────────────────────────────────────────────────

class TestQuattroBoxInheritance:
    def test_is_subclass_of_night_owl(self):
        assert issubclass(afcQuattroBox, afcNightOwl)


# ── lane_loaded ───────────────────────────────────────────────────────────────

class TestLaneLoaded:
    def test_calls_spool_illum_led(self):
        """lane_loaded should set spool illumination LED in addition to lane LED."""
        unit = _make_quattro()
        lane = _make_lane()
        unit.lane_loaded(lane)
        unit.afc.function.afc_led.assert_any_call(lane.led_spool_illum, lane.led_spool_index)

    def test_calls_lane_ready_led(self):
        unit = _make_quattro()
        lane = _make_lane()
        unit.lane_loaded(lane)
        unit.afc.function.afc_led.assert_any_call(lane.led_ready, lane.led_index)


# ── lane_unloaded ─────────────────────────────────────────────────────────────

class TestLaneUnloaded:
    def test_calls_led_off_for_spool(self):
        """lane_unloaded should turn off spool LEDs."""
        unit = _make_quattro()
        lane = _make_lane()
        unit.lane_unloaded(lane)
        unit.afc.function.afc_led.assert_any_call(unit.afc.led_off, lane.led_spool_index)


# ── lane_loading ──────────────────────────────────────────────────────────────

class TestLaneLoading:
    def test_calls_set_logo_color_with_loading_color(self):
        unit = _make_quattro()
        lane = _make_lane()
        unit.set_logo_color = MagicMock()
        unit.lane_loading(lane)
        unit.set_logo_color.assert_called_with(unit.led_logo_loading)

    def test_calls_afc_led_for_lane(self):
        unit = _make_quattro()
        lane = _make_lane()
        unit.lane_loading(lane)
        unit.afc.function.afc_led.assert_any_call(lane.led_loading, lane.led_index)


# ── lane_tool_loaded ──────────────────────────────────────────────────────────

class TestLaneToolLoaded:
    def test_calls_set_logo_color_with_lane_color(self):
        unit = _make_quattro()
        lane = _make_lane(color="#FF0000")
        unit.set_logo_color = MagicMock()
        unit.lane_tool_loaded(lane)
        unit.set_logo_color.assert_called_with(lane.color)

    def test_calls_afc_led_for_lane(self):
        unit = _make_quattro()
        lane = _make_lane()
        unit.lane_tool_loaded(lane)
        unit.afc.function.afc_led.assert_any_call(lane.led_tool_loaded, lane.led_index)


# ── lane_tool_unloaded ────────────────────────────────────────────────────────

class TestLaneToolUnloaded:
    def test_calls_set_logo_color_with_logo_color(self):
        unit = _make_quattro()
        lane = _make_lane()
        unit.set_logo_color = MagicMock()
        unit.lane_tool_unloaded(lane)
        unit.set_logo_color.assert_called_with(unit.led_logo_color)

    def test_calls_afc_led_for_lane(self):
        unit = _make_quattro()
        lane = _make_lane()
        unit.lane_tool_unloaded(lane)
        # lane_tool_unloaded calls super() which calls afc_led with led_ready
        unit.afc.function.afc_led.assert_any_call(lane.led_ready, lane.led_index)


# ── handle_connect ────────────────────────────────────────────────────────────

class TestHandleConnect:
    def test_handle_connect_sets_logo_html(self):
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()
        unit.handle_connect()
        assert "Quattro Box Ready" in unit.logo

    def test_handle_connect_sets_logo_error_html(self):
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()
        unit.handle_connect()
        assert "Quattro Box Not Ready" in unit.logo_error

    def test_handle_connect_calls_set_logo_color_with_logo_color(self):
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()
        unit.handle_connect()
        unit.set_logo_color.assert_called_once_with(unit.led_logo_color)

    def test_handle_connect_registers_unit_in_afc_units(self):
        unit = _make_quattro(name="quattro_test")
        unit.set_logo_color = MagicMock()
        unit.handle_connect()
        assert unit.afc.units.get("quattro_test") is unit
