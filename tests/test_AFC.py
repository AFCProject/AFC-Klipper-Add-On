"""
Unit tests for extras/AFC.py

Covers:
  - State: string constants
  - AFC_VERSION: version string format
  - afc._remove_after_last: string helper
  - afc._get_message: message queue peek and pop
  - afc.get_status: returns required keys
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from extras.AFC import afc, State, AFC_VERSION


# ── State constants ───────────────────────────────────────────────────────────

class TestStateConstants:
    def test_init_value(self):
        assert State.INIT == "Initialized"

    def test_idle_value(self):
        assert State.IDLE == "Idle"

    def test_error_value(self):
        assert State.ERROR == "Error"

    def test_loading_value(self):
        assert State.LOADING == "Loading"

    def test_unloading_value(self):
        assert State.UNLOADING == "Unloading"

    def test_ejecting_lane_value(self):
        assert State.EJECTING_LANE == "Ejecting"

    def test_moving_lane_value(self):
        assert State.MOVING_LANE == "Moving"

    def test_restoring_pos_value(self):
        assert State.RESTORING_POS == "Restoring"

    def test_all_constants_are_strings(self):
        attrs = [a for a in dir(State) if not a.startswith("_")]
        for attr in attrs:
            assert isinstance(getattr(State, attr), str)

    def test_all_constants_unique(self):
        attrs = [a for a in dir(State) if not a.startswith("_")]
        values = [getattr(State, a) for a in attrs]
        assert len(values) == len(set(values))


# ── AFC_VERSION ───────────────────────────────────────────────────────────────

class TestAfcVersion:
    def test_version_is_string(self):
        assert isinstance(AFC_VERSION, str)

    def test_version_has_dots(self):
        assert "." in AFC_VERSION

    def test_version_parts_are_numeric(self):
        parts = AFC_VERSION.split(".")
        for part in parts:
            assert part.isdigit(), f"Non-numeric version part: {part!r}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_afc():
    """Build an afc instance bypassing __init__."""
    obj = afc.__new__(afc)

    from tests.conftest import MockAFC, MockLogger, MockPrinter

    inner = MockAFC()
    printer = MockPrinter(afc=inner)
    obj.printer = printer
    obj.logger = MockLogger()
    obj.reactor = inner.reactor
    obj.moonraker = None
    obj.function = MagicMock()
    obj.message_queue = []
    # obj.current = MagicMock()
    obj.current_loading = None
    obj.next_lane_load = None
    obj.current_state = State.IDLE
    obj.error_state = False
    obj.position_saved = False
    obj.spoolman = None
    obj._td1_present = False
    obj.lane_data_enabled = False
    obj.units = {}
    obj.lanes = {}
    obj.tools = {}
    obj.hubs = {}
    obj.buffers = {}
    obj.tool_cmds = {}
    obj.led_state = True
    obj.current_toolchange = 0
    obj.number_of_toolchanges = 0
    obj.temp_wait_tolerance = 5
    obj._get_bypass_state = MagicMock(return_value=False)
    obj._get_quiet_mode = MagicMock(return_value=False)
    return obj


# ── _remove_after_last ────────────────────────────────────────────────────────

class TestRemoveAfterLast:
    def test_removes_after_last_slash(self):
        obj = _make_afc()
        result = obj._remove_after_last("/home/user/file.txt", "/")
        assert result == "/home/user/"

    def test_no_char_returns_original(self):
        obj = _make_afc()
        result = obj._remove_after_last("nodots", ".")
        assert result == "nodots"

    def test_char_at_end(self):
        obj = _make_afc()
        result = obj._remove_after_last("trailing/", "/")
        assert result == "trailing/"

    def test_single_char_string(self):
        obj = _make_afc()
        result = obj._remove_after_last("/", "/")
        assert result == "/"

    def test_multiple_occurrences_uses_last(self):
        obj = _make_afc()
        result = obj._remove_after_last("a/b/c/d", "/")
        assert result == "a/b/c/"


# ── _get_message ──────────────────────────────────────────────────────────────

class TestGetMessage:
    def test_empty_queue_returns_empty_strings(self):
        obj = _make_afc()
        msg = obj._get_message()
        assert msg["message"] == ""
        assert msg["type"] == ""

    def test_peek_does_not_remove(self):
        obj = _make_afc()
        obj.message_queue = [("hello", "info")]
        obj._get_message(clear=False)
        assert len(obj.message_queue) == 1

    def test_peek_returns_message(self):
        obj = _make_afc()
        obj.message_queue = [("hello", "info")]
        msg = obj._get_message(clear=False)
        assert msg["message"] == "hello"
        assert msg["type"] == "info"

    def test_clear_removes_first_item(self):
        obj = _make_afc()
        obj.message_queue = [("first", "error"), ("second", "warning")]
        obj._get_message(clear=True)
        assert len(obj.message_queue) == 1
        assert obj.message_queue[0][0] == "second"

    def test_clear_returns_popped_message(self):
        obj = _make_afc()
        obj.message_queue = [("popped", "error")]
        msg = obj._get_message(clear=True)
        assert msg["message"] == "popped"
        assert msg["type"] == "error"

    def test_clear_on_empty_returns_empty(self):
        obj = _make_afc()
        msg = obj._get_message(clear=True)
        assert msg["message"] == ""
        assert msg["type"] == ""


# ── get_status ────────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_returns_required_keys(self):
        obj = _make_afc()
        status = obj.get_status()
        required = {
            "current_load", "current_state", "error_state",
            "lanes", "extruders", "hubs", "buffers", "units",
            "message", "position_saved",
        }
        for key in required:
            assert key in status, f"Missing key: {key}"

    def test_lanes_is_list(self):
        obj = _make_afc()
        assert isinstance(obj.get_status()["lanes"], list)

    def test_extruders_is_list(self):
        obj = _make_afc()
        assert isinstance(obj.get_status()["extruders"], list)

    def test_hubs_is_list(self):
        obj = _make_afc()
        assert isinstance(obj.get_status()["hubs"], list)

    def test_units_is_list(self):
        obj = _make_afc()
        assert isinstance(obj.get_status()["units"], list)

    def test_error_state_reflects_attribute(self):
        obj = _make_afc()
        obj.error_state = True
        assert obj.get_status()["error_state"] is True

    def test_current_load_none_when_nothing_loaded(self):
        obj = _make_afc()
        obj.function.get_current_lane.return_value = None
        assert obj.get_status()["current_load"] is None

    def test_message_from_queue(self):
        obj = _make_afc()
        obj.message_queue = [("test msg", "warning")]
        msg = obj.get_status()["message"]
        assert msg["message"] == "test msg"


# ── _check_extruder_temp ──────────────────────────────────────────────────────

def _make_afc_for_check_extruder_temp(
    heater_target_temp,
    actual_temp,
    target_material_temp,
    lower_extruder_temp_on_change=True,
    using_min_value=False,
):
    from tests.test_AFC_lane import _make_afc_lane
    """Build an afc instance wired up for _check_extruder_temp tests."""
    obj = _make_afc()
    obj.lower_extruder_temp_on_change = lower_extruder_temp_on_change
    obj._wait_for_temp_within_tolerance = MagicMock()

    heater = MagicMock()
    heater.target_temp = heater_target_temp
    heater.can_extrude = False
    heater.get_temp = MagicMock(return_value=(actual_temp, 0.0))

    extruder = MagicMock()
    extruder.get_heater.return_value = heater

    lane = _make_afc_lane()
    lane.extruder_obj.toolhead_extruder = MagicMock()
    lane.extruder_obj.toolhead_extruder = extruder

    obj.toolhead = MagicMock()
    obj.toolhead.get_extruder.return_value = extruder

    pheaters = MagicMock()
    obj.printer._objects["heaters"] = pheaters

    obj.function.is_printing.return_value = False
    obj._get_default_material_temps = MagicMock(
        return_value=(float(target_material_temp), using_min_value)
    )

    return obj, heater, extruder, pheaters, lane


class TestCheckExtruderTemp:
    """Tests for afc._check_extruder_temp().

    Covers the two-action logic:
      need_lower: set temp > target+5 → lower without waiting
      need_heat:  set temp < target-5 → heat and wait
      skip_lower: need_lower AND lower_extruder_temp_on_change=False
                  AND actual temp already sufficient → skip the lower call
    """

    # ── Default behaviour (lower_extruder_temp_on_change=True) ───────────────

    def test_lowers_to_target_when_above(self):
        
        """Set temp more than 5° above target → lower to target, no wait."""
        obj, heater, extruder, pheaters, lane = _make_afc_for_check_extruder_temp(
            heater_target_temp=250, actual_temp=248, target_material_temp=210
        )
        result = obj._check_extruder_temp(lane)
        pheaters.set_temperature.assert_called_once_with(heater, 210.0)
        obj._wait_for_temp_within_tolerance.assert_not_called()
        assert result is False

    def test_heats_to_target_when_below(self):
        """Set temp more than 5° below target → heat to target and wait."""
        obj, heater, extruder, pheaters, lane = _make_afc_for_check_extruder_temp(
            heater_target_temp=150, actual_temp=148, target_material_temp=210
        )
        result = obj._check_extruder_temp(lane)
        pheaters.set_temperature.assert_called_once_with(heater, 210.0)
        obj._wait_for_temp_within_tolerance.assert_called_once_with(obj.heater, 210,
                                                                    obj.temp_wait_tolerance*2)
        assert result is True

    def test_no_change_when_within_range(self):
        """Set temp within ±5° of target → no set_temperature call."""
        obj, heater, extruder, pheaters, lane = _make_afc_for_check_extruder_temp(
            heater_target_temp=212, actual_temp=210, target_material_temp=210
        )
        result = obj._check_extruder_temp(lane)
        pheaters.set_temperature.assert_not_called()
        obj._wait_for_temp_within_tolerance.assert_not_called()
        assert result is False

    def test_no_lower_when_using_min_extrude_temp(self):
        """When using min_extrude_temp fallback, do not lower even if above target+5."""
        obj, heater, extruder, pheaters, lane = _make_afc_for_check_extruder_temp(
            heater_target_temp=250, actual_temp=248, target_material_temp=210,
            using_min_value=True,
        )
        result = obj._check_extruder_temp(lane)
        pheaters.set_temperature.assert_not_called()
        obj._wait_for_temp_within_tolerance.assert_not_called()
        assert result is False

    # ── lower_extruder_temp_on_change=False ──────────────────────────────────

    def test_skip_lower_when_disabled_and_actual_sufficient(self):
        """lower=False, actual ≥ target-5 → lowering is skipped entirely."""
        obj, heater, extruder, pheaters, lane = _make_afc_for_check_extruder_temp(
            heater_target_temp=250, actual_temp=248, target_material_temp=210,
            lower_extruder_temp_on_change=False,
        )
        result = obj._check_extruder_temp(lane)
        pheaters.set_temperature.assert_not_called()
        obj._wait_for_temp_within_tolerance.assert_not_called()
        assert result is False

    def test_lower_not_skipped_when_actual_insufficient(self):
        """lower=False but actual < target-5 → skip_lower stays False, lower fires."""
        obj, heater, extruder, pheaters, lane = _make_afc_for_check_extruder_temp(
            heater_target_temp=250, actual_temp=200, target_material_temp=210,
            lower_extruder_temp_on_change=False,
        )
        result = obj._check_extruder_temp(lane)
        pheaters.set_temperature.assert_any_call(heater, 210.0)
        obj._wait_for_temp_within_tolerance.assert_not_called()
        assert result is False

    def test_heating_unaffected_by_lower_flag(self):
        """lower=False does not suppress heating up to a higher target."""
        obj, heater, extruder, pheaters, lane = _make_afc_for_check_extruder_temp(
            heater_target_temp=150, actual_temp=148, target_material_temp=210,
            lower_extruder_temp_on_change=False,
        )
        result = obj._check_extruder_temp(lane)
        pheaters.set_temperature.assert_called_once_with(heater, 210.0)
        obj._wait_for_temp_within_tolerance.assert_called_once_with(obj.heater, 210,
                                                                    obj.temp_wait_tolerance*2)
        assert result is True

    # ── Early-return guard ────────────────────────────────────────────────────

    def test_no_change_when_printing(self):
        """can_extrude=True + is_printing=True → early return, no temp change."""
        obj, heater, extruder, pheaters, lane = _make_afc_for_check_extruder_temp(
            heater_target_temp=150, actual_temp=148, target_material_temp=210
        )
        heater.can_extrude = True
        obj.function.is_printing.return_value = True
        result = obj._check_extruder_temp(lane)
        pheaters.set_temperature.assert_not_called()
        obj._wait_for_temp_within_tolerance.assert_not_called()
        assert result is None
    # TODO: add passing in a no_wait variable