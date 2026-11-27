"""
Unit tests for extras/AFC_QuattroBox.py

Covers:
  - afcQuattroBox: is subclass of afcNightOwl
  - afcQuattroBox.__init__: reads type from config
  - afcQuattroBox.handle_connect: calls super, sets logo html, sets logo color
  - afcQuattroBox.lane_loading: calls super then set_logo_color with loading color
  - afcQuattroBox.lane_tool_loaded: calls super then set_logo_color with lane color
  - afcQuattroBox.lane_tool_unloaded: calls super then set_logo_color with logo color
  - load_config_prefix: Klipper config entry point
"""

from __future__ import annotations

import configparser
import importlib.util
import sys
from typing import Optional
from unittest.mock import MagicMock, patch
import pytest

from extras.AFC_QuattroBox import afcQuattroBox, load_config_prefix
from extras.AFC_NightOwl import afcNightOwl
from extras.AFC_unit import afcUnit


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_quattro_config(name: str="Quattro_1", values: Optional[dict]=None):
    """Build the MockConfig/MockPrinter/MockAFC trio needed to construct an
    afcQuattroBox via its real __init__ (chained through afcNightOwl.__init__,
    afcBoxTurtle.__init__ and afcUnit.__init__).

    :param name: Unit name to use for the config section
    :param values: Config values to seed, merged over the defaults
    :return tuple: (config, printer, afc) mocks ready to build an afcQuattroBox
    """
    from tests.conftest import MockAFC, MockConfig, MockPrinter

    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    config = MockConfig(name=f"AFC_QuattroBox {name}", printer=printer, values=values or {})
    return config, printer, afc


def _make_quattro(name: str="Quattro_1", values: Optional[dict]=None) -> afcQuattroBox:
    """Build a real afcQuattroBox via its actual __init__.

    :param name: Unit name to use for the config section
    :param values: Config values to seed, merged over the defaults
    :return afcQuattroBox: constructed Quattro Box unit
    """
    config, printer, afc = _make_quattro_config(name=name, values=values)
    return afcQuattroBox(config)


def _make_lane(color: str="#FF0000") -> MagicMock:
    lane = MagicMock()
    lane.name = "lane1"
    lane.color = color
    return lane


# ── Inheritance ───────────────────────────────────────────────────────────────

class TestQuattroBoxInheritance:
    def test_is_subclass_of_night_owl(self) -> None:
        assert issubclass(afcQuattroBox, afcNightOwl)

    def test_instance_is_night_owl(self) -> None:
        unit = _make_quattro()
        assert isinstance(unit, afcNightOwl)


# ── __init__ ──────────────────────────────────────────────────────────────────

class TestQuattroBoxInit:
    def test_type_defaults_to_quattro_box_when_not_set_in_config(self) -> None:
        unit = _make_quattro()
        assert unit.type == "Quattro_Box"

    def test_type_reads_overridden_value_from_config(self) -> None:
        unit = _make_quattro(values={"type": "CustomQuattro"})
        assert unit.type == "CustomQuattro"


# ── handle_connect ────────────────────────────────────────────────────────────

class TestQuattroBoxHandleConnect:
    def test_calls_super_handle_connect_once(self) -> None:
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()
        with patch.object(afcNightOwl, "handle_connect") as mock_super:
            unit.handle_connect()
        mock_super.assert_called_once_with()

    def test_sets_exact_logo_after_super_runs(self) -> None:
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()

        def fake_super_handle_connect(self) -> None:
            self.logo = "sentinel-from-night-owl"
            self.logo_error = "sentinel-error-from-night-owl"

        with patch.object(afcNightOwl, "handle_connect", fake_super_handle_connect):
            unit.handle_connect()

        assert unit.logo == '<span class=success--text>Quattro Box Ready</span>'

    def test_sets_exact_logo_error_after_super_runs(self) -> None:
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()

        def fake_super_handle_connect(self) -> None:
            self.logo = "sentinel-from-night-owl"
            self.logo_error = "sentinel-error-from-night-owl"

        with patch.object(afcNightOwl, "handle_connect", fake_super_handle_connect):
            unit.handle_connect()

        assert unit.logo_error == '<span class=error--text>Quattro Box Not Ready</span>\n'

    def test_calls_set_logo_color_with_led_logo_color(self) -> None:
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()
        with patch.object(afcNightOwl, "handle_connect"):
            unit.handle_connect()
        unit.set_logo_color.assert_called_once_with(unit.led_logo_color)


# ── lane_loading ──────────────────────────────────────────────────────────────

class TestQuattroBoxLaneLoading:
    def test_calls_super_lane_loading_with_lane(self) -> None:
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()
        lane = _make_lane()
        with patch.object(afcUnit, "lane_loading") as mock_super:
            unit.lane_loading(lane, gcmd="callback")
        mock_super.assert_called_once_with(lane, gcmd="callback")

    def test_calls_set_logo_color_with_led_logo_loading(self) -> None:
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()
        lane = _make_lane()
        with patch.object(afcUnit, "lane_loading"):
            unit.lane_loading(lane, gcmd="callback")
        unit.set_logo_color.assert_called_once_with(unit.led_logo_loading)


# ── lane_tool_loaded ──────────────────────────────────────────────────────────

class TestQuattroBoxLaneToolLoaded:
    def test_calls_super_lane_tool_loaded_with_lane(self) -> None:
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()
        lane = _make_lane()
        with patch.object(afcUnit, "lane_tool_loaded") as mock_super:
            unit.lane_tool_loaded(lane, gcmd="callback")
        mock_super.assert_called_once_with(lane, gcmd="callback")

    def test_calls_set_logo_color_with_lane_color(self) -> None:
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()
        lane = _make_lane(color="#00FF00")
        with patch.object(afcUnit, "lane_tool_loaded"):
            unit.lane_tool_loaded(lane, gcmd="callback")
        unit.set_logo_color.assert_called_once_with("#00FF00")


# ── lane_tool_unloaded ────────────────────────────────────────────────────────

class TestQuattroBoxLaneToolUnloaded:
    def test_calls_super_lane_tool_unloaded_with_lane(self) -> None:
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()
        lane = _make_lane()
        with patch.object(afcUnit, "lane_tool_unloaded") as mock_super:
            unit.lane_tool_unloaded(lane, gcmd="callback")
        mock_super.assert_called_once_with(lane, gcmd="callback")

    def test_calls_set_logo_color_with_led_logo_color(self) -> None:
        unit = _make_quattro()
        unit.set_logo_color = MagicMock()
        lane = _make_lane()
        with patch.object(afcUnit, "lane_tool_unloaded"):
            unit.lane_tool_unloaded(lane, gcmd="callback")
        unit.set_logo_color.assert_called_once_with(unit.led_logo_color)


# ── load_config_prefix ────────────────────────────────────────────────────────

class TestQuattroBoxLoadConfigPrefix:
    def test_returns_afc_quattro_box_instance(self) -> None:
        config, printer, afc = _make_quattro_config()

        unit = load_config_prefix(config)

        assert isinstance(unit, afcQuattroBox)

    def test_passes_config_through_to_constructed_unit(self) -> None:
        config, printer, afc = _make_quattro_config(values={"type": "CustomQuattro"})

        unit = load_config_prefix(config)

        assert unit.type == "CustomQuattro"


# ═════════════════════════════════════════════════════════════════════════
# Module-level import guards
# ═════════════════════════════════════════════════════════════════════════

def _exec_afc_quattro_box_with_blocked_dependency(blocked_module_name: str) -> None:
    """Execute a throw-away copy of extras/AFC_QuattroBox.py's module-level
    code with `blocked_module_name` forced to fail import, to exercise the
    file's top-level ``try: from X import Y / except: raise error(...)``
    guards.

    Never touches the real, already-imported ``extras.AFC_QuattroBox``
    module that the rest of this test suite depends on: the copy is loaded
    under a throwaway module name and discarded afterward, whether or not it
    raises. Cleanup restores the exact same pre-existing module object in
    sys.modules so other test files' references stay valid.

    :param blocked_module_name: Dotted module name to force an ImportError for
    """
    import extras.AFC_QuattroBox as real_module
    fresh_name = "extras.AFC_QuattroBox_import_guard_probe"
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


class TestModuleImportGuards:
    """Covers the two module-level `try/except: raise error(...)` guards in
    AFC_QuattroBox.py, one per dependency import."""

    def test_afc_utils_error_str_import_failure_raises_configparser_error(self) -> None:
        """The first guard imports ERROR_STR itself from AFC_utils, so it
        can't use ERROR_STR.format(...) in its own except clause."""
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_quattro_box_with_blocked_dependency("extras.AFC_utils")
        assert str(exc_info.value).startswith(
            "Error when trying to import AFC_utils.ERROR_STR"
        )

    def test_afc_night_owl_import_failure_raises_configparser_error(self) -> None:
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_quattro_box_with_blocked_dependency("extras.AFC_NightOwl")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_NightOwl, please rerun install-afc.sh"
        )
