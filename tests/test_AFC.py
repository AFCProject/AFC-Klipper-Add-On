"""
Unit tests for extras/AFC.py

Covers:
  - State: string constants
  - AFC_VERSION: version string format
  - afc._remove_after_last: string helper
  - afc._get_message: message queue peek and pop
  - afc.get_status: returns required keys
  - afc._cooldown_last_extruder: old-extruder temp-drop logic
  - afc._heat_next_extruder: explicit next_temp path
  - afc.CHANGE_TOOL: adjusting_temperature with new_extruder_temp
  - afc.CHANGE_TOOL: early-exit paths (bypass active, _heat_next_extruder failure)
  - afc.CHANGE_TOOL: same-lane branch (_handle_activate_extruder, current_toolchange)
  - afc.CHANGE_TOOL: unload path (current=None, prep_done=False, unknown lane, TOOL_UNLOAD failure)
  - afc.CHANGE_TOOL: load path (restore_pos flag, in_toolchange, stats callbacks, error_state guard)
  - afc.CHANGE_TOOL: state management (next_lane_load lifecycle, in_toolchange flag, toolchange counter)
  - afc.CHANGE_TOOL: infinite runout (adjusting_temperature flag, status reset, heat with next_temp=None)
  - afc.CHANGE_TOOL: exception handling (bare except, error.AFC_error, finally guarantees)
  - afc.TOOL_LOAD: unload when destination extruder already has a different lane loaded
  - afc.cmd_CHANGE_TOOL: NEW_EXTRUDER_TEMP parameter parsing
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from extras.AFC import afc, State, AFC_VERSION
from extras.AFC_lane import AFCLaneState
from klippy import Printer


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
    obj.gcode = MagicMock()
    obj.message_queue = []
    # obj.current = MagicMock()
    obj.current_loading = None
    obj.next_lane_load = None
    obj.current_state = State.IDLE
    obj.error_state = False
    obj.position_saved = False
    obj.spoolman = None
    obj._td1_present = False
    obj._last_td1_query = 0.0
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
    obj.in_toolchange = False
    obj.get_bypass_state = MagicMock(return_value=False)
    obj._get_quiet_mode = MagicMock(return_value=False)
    obj.park = False
    obj.park_cmd = None
    obj.wipe = False
    obj.wipe_cmd = None
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


# ── _cooldown_last_extruder ───────────────────────────────────────────────────

def _make_afc_for_cooldown(target_temp=220.0, toolchange_temp_drop=0.0, is_infinite_runout=False):
    """Build an afc instance wired up for _cooldown_last_extruder tests."""
    obj = _make_afc()

    last_heater = MagicMock()
    last_heater.target_temp = target_temp

    last_extruder = MagicMock()
    last_extruder.name = "extruder"
    last_extruder.get_heater.return_value = last_heater
    last_extruder.toolchange_temp_drop = toolchange_temp_drop

    pheaters = MagicMock()
    obj.printer._objects["heaters"] = pheaters

    return obj, last_extruder, last_heater, pheaters


class TestCooldownLastExtruder:
    """Tests for afc._cooldown_last_extruder()."""

    def test_infinite_runout_always_sets_zero(self):
        """When is_infinite_runout=True, target is always 0 regardless of drop setting."""
        obj, ext, heater, pheaters = _make_afc_for_cooldown(
            target_temp=220.0, toolchange_temp_drop=30.0, is_infinite_runout=True
        )
        obj._cooldown_last_extruder(ext, is_infinite_runout=True)
        pheaters.set_temperature.assert_called_once_with(heater, 0, False)

    def test_no_drop_configured_keeps_current_target(self):
        """When toolchange_temp_drop=0, temperature is unchanged (drop of 0)."""
        obj, ext, heater, pheaters = _make_afc_for_cooldown(
            target_temp=220.0, toolchange_temp_drop=0.0
        )
        obj._cooldown_last_extruder(ext, is_infinite_runout=False)
        pheaters.set_temperature.assert_called_once_with(heater, 220.0, False)

    def test_drop_reduces_temperature_by_configured_amount(self):
        """Normal drop: target_temp - toolchange_temp_drop."""
        obj, ext, heater, pheaters = _make_afc_for_cooldown(
            target_temp=220.0, toolchange_temp_drop=40.0
        )
        obj._cooldown_last_extruder(ext, is_infinite_runout=False)
        pheaters.set_temperature.assert_called_once_with(heater, 180.0, False)

    def test_drop_larger_than_target_clamps_to_zero(self):
        """Drop larger than current target is clamped to 0 (no negative temps)."""
        obj, ext, heater, pheaters = _make_afc_for_cooldown(
            target_temp=30.0, toolchange_temp_drop=50.0
        )
        obj._cooldown_last_extruder(ext, is_infinite_runout=False)
        pheaters.set_temperature.assert_called_once_with(heater, 0, False)

    def test_drop_equal_to_target_sets_zero(self):
        """Drop exactly equal to target → 0."""
        obj, ext, heater, pheaters = _make_afc_for_cooldown(
            target_temp=50.0, toolchange_temp_drop=50.0
        )
        obj._cooldown_last_extruder(ext, is_infinite_runout=False)
        pheaters.set_temperature.assert_called_once_with(heater, 0, False)

    def test_logs_cooldown_message(self):
        """A log message is emitted describing the cooldown action."""
        obj, ext, heater, pheaters = _make_afc_for_cooldown(
            target_temp=220.0, toolchange_temp_drop=20.0
        )
        obj._cooldown_last_extruder(ext, is_infinite_runout=False)
        logged = [msg for level, msg in obj.logger.messages if "Cooling down" in msg]
        assert len(logged) == 1
        assert "extruder" in logged[0]


# ── _heat_next_extruder with explicit next_temp ───────────────────────────────

def _make_afc_for_heat_next(current_target=220.0, next_extruder_name="extruder1"):
    """Build an afc instance wired up for _heat_next_extruder tests."""
    from tests.test_AFC_lane import _make_afc_lane

    obj = _make_afc()

    # Current toolhead extruder (the one that ran out)
    current_heater = MagicMock()
    current_heater.target_temp = current_target
    current_heater.get_temp = MagicMock(return_value=(current_target - 2, current_target))
    current_extruder_mock = MagicMock()
    current_extruder_mock.get_heater.return_value = current_heater
    obj.toolhead = MagicMock()
    obj.toolhead.get_extruder.return_value = current_extruder_mock

    # Next extruder
    next_heater = MagicMock()
    next_extruder_obj = MagicMock()
    next_extruder_obj.name = next_extruder_name
    next_extruder_obj.get_heater.return_value = next_heater
    next_extruder_obj.deadband = 3.0

    # Lane pointing at next extruder
    lane = _make_afc_lane("AFC_stepper lane2")
    lane.extruder_obj = next_extruder_obj

    obj.next_lane_load = "lane2"
    obj.lanes["lane2"] = lane

    pheaters = MagicMock()
    obj.printer._objects["heaters"] = pheaters

    # get_current_extruder returns a different name to trigger heating path
    obj.function.get_current_extruder.return_value = "extruder0"

    obj._wait_for_temp_within_tolerance = MagicMock()

    return obj, next_extruder_obj, next_heater, current_heater, pheaters


class TestHeatNextExtruderWithExplicitTemp:
    """Tests for the new next_temp parameter of _heat_next_extruder."""

    def test_explicit_temp_used_instead_of_current_heater(self):
        """When next_temp is given, the next heater is set to that value, not the current target."""
        obj, next_ext, next_heater, current_heater, pheaters = _make_afc_for_heat_next(
            current_target=220.0
        )
        result = obj._heat_next_extruder(wait=False, next_temp=190.0)
        pheaters.set_temperature.assert_called_once_with(next_heater, 190.0, False)

    def test_explicit_temp_returns_extruder_and_temp(self):
        """Return value is (AFCExtruder object, set_temp) — not the heater."""
        obj, next_ext, next_heater, current_heater, pheaters = _make_afc_for_heat_next()
        result = obj._heat_next_extruder(wait=False, next_temp=200.0)
        assert result[0] is next_ext
        assert result[1] == 200.0

    def test_none_next_temp_reads_current_heater_target(self):
        """When next_temp=None (infinite runout path), target is read from current heater."""
        obj, next_ext, next_heater, current_heater, pheaters = _make_afc_for_heat_next(
            current_target=215.0
        )
        result = obj._heat_next_extruder(wait=False, next_temp=None)
        pheaters.set_temperature.assert_called_once_with(next_heater, 215.0, False)
        assert result[1] == 215.0

    def test_current_heater_not_touched_when_next_temp_given(self):
        """Providing next_temp must NOT set or reset the current heater temperature."""
        obj, next_ext, next_heater, current_heater, pheaters = _make_afc_for_heat_next(
            current_target=220.0
        )
        obj._heat_next_extruder(wait=False, next_temp=200.0)
        # set_temperature must only be called for the next heater, never the current one
        for call in pheaters.set_temperature.call_args_list:
            assert call.args[0] is next_heater, "set_temperature was called on unexpected heater"

    def test_wait_skipped_when_next_extruder_is_already_current(self):
        """
        When the next extruder is the same as the current one, wait should be skipped.
        """
        obj, next_ext, next_heater, current_heater, pheaters = _make_afc_for_heat_next(
            next_extruder_name="extruder0",  # same as current (get_current_extruder returns "extruder0")
        )
        obj._heat_next_extruder(wait=True, next_temp=200.0)
        obj._wait_for_temp_within_tolerance.assert_not_called()

    def test_wait_triggered_when_next_extruder_differs_from_current(self):
        """
        When the next extruder differs from the current one, _wait_for_temp_within_tolerance
        must be called with the next heater and the target temp.
        """
        obj, next_ext, next_heater, current_heater, pheaters = _make_afc_for_heat_next(
            next_extruder_name="extruder1",  # different from current ("extruder0")
        )
        obj._heat_next_extruder(wait=True, next_temp=200.0)
        obj._wait_for_temp_within_tolerance.assert_called_once_with(next_heater, 200.0, 3.0)


# ── CHANGE_TOOL: new_extruder_temp integration ────────────────────────────────

def _make_afc_for_change_tool(lane_name="lane2", next_extruder_name="extruder1",
                               current_lane_name="lane1", current_extruder_name="extruder0"):
    """Build a minimal afc suitable for driving CHANGE_TOOL with new_extruder_temp."""
    from tests.test_AFC_lane import _make_afc_lane

    obj = _make_afc()
    obj.afcDeltaTime = MagicMock()
    obj.afc_stats = MagicMock()
    obj.afc_stats.average_toolchange_time = MagicMock()
    obj.testing = True
    obj.save_pos = MagicMock()
    obj.restore_pos = MagicMock()
    obj.TOOL_LOAD = MagicMock(return_value=True)
    obj.TOOL_UNLOAD = MagicMock(return_value=True)
    obj.LANE_UNLOAD = MagicMock(return_value=True)
    obj._check_bypass = MagicMock(return_value=False)
    obj._heat_next_extruder = MagicMock()
    obj._cooldown_last_extruder = MagicMock()
    obj._wait_for_temp_within_tolerance = MagicMock()
    obj.error = MagicMock()
    obj.printer = Printer

    # Current (old) lane/extruder
    current_extruder = MagicMock()
    current_extruder.th_extruder_name = current_extruder.name = current_extruder_name
    current_extruder.get_heater = MagicMock(return_value=MagicMock())
    current_extruder.deadband = 2.0
    current_extruder.estats = MagicMock()
    current_lane = _make_afc_lane(f"AFC_stepper {current_lane_name}")
    current_lane.extruder_obj = current_extruder
    current_lane._afc_prep_done = True
    obj.lanes[current_lane_name] = current_lane
    obj.function.get_current_lane.return_value = current_lane_name

    # Next (new) lane/extruder
    next_extruder = MagicMock()
    next_extruder.th_extruder_name = next_extruder.name = next_extruder_name
    next_extruder.get_heater = MagicMock(return_value=MagicMock())
    next_extruder.deadband = 2.0
    next_extruder.estats = MagicMock()
    cur_lane = _make_afc_lane(f"AFC_stepper {lane_name}")
    cur_lane.extruder_obj = next_extruder
    cur_lane._afc_prep_done = True
    cur_lane.status = AFCLaneState.LOADED
    obj.lanes[lane_name] = cur_lane

    obj.function.get_current_extruder.return_value = current_extruder_name
    obj.function.in_print.return_value = False
    obj.function.is_paused.return_value = False
    obj.function.log_toolhead_pos = MagicMock()
    obj.function._handle_activate_extruder = MagicMock()

    # _heat_next_extruder stub: return (next_extruder_obj, set_temp)
    obj._heat_next_extruder.return_value = (next_extruder, 200.0)

    return obj, cur_lane, current_lane


class TestChangeTool_NewExtruderTemp:
    """Tests for CHANGE_TOOL with new_extruder_temp (non-infinite-runout path)."""

    def test_heat_next_called_with_explicit_temp(self):
        """Providing new_extruder_temp causes _heat_next_extruder to receive that value."""
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj._heat_next_extruder.assert_called_once_with(wait=False, next_temp=200.0)

    def test_heat_next_called_with_explicit_temp_current_lane_None(self):
        """Providing new_extruder_temp causes _heat_next_extruder to receive that value."""
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.function.get_current_lane.return_value = None
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj._heat_next_extruder.assert_called_once_with(wait=False, next_temp=200.0)
        obj._cooldown_last_extruder.assert_not_called()

    def test_cooldown_called_for_old_extruder(self):
        """Old extruder is cooled down when new_extruder_temp is provided."""
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj._cooldown_last_extruder.assert_called_once()
        called_extruder = obj._cooldown_last_extruder.call_args.args[0]
        assert called_extruder is current_lane.extruder_obj

    def test_heat_called_before_cooldown(self):
        """
        _heat_next_extruder must be called before _cooldown_last_extruder.

        Note that if this ordering behavior changes in the future, ensure that the infinite runout
        case is properly setting the new extruder temperature, because currently this order is
        required to read the current target temp before adjustment.
        """
        call_order = []
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj._heat_next_extruder.side_effect = lambda **kw: (
            call_order.append("heat"),
            obj._heat_next_extruder.return_value
        )[1]
        obj.TOOL_UNLOAD.side_effect = lambda *a, **kw: (
            call_order.append("unload"),
            obj.TOOL_UNLOAD.return_value
        )[1]
        obj._cooldown_last_extruder.side_effect = lambda *a, **kw: call_order.append("cool")
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        assert call_order == ["heat", "cool", "unload"], f"Wrong call order: {call_order}"

    def test_wait_for_temp_called_after_unload(self):
        """_wait_for_temp_within_tolerance is called (after unload) when adjusting temps."""
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj._wait_for_temp_within_tolerance.assert_called_once()

    def test_no_adjusting_temperature_without_param(self):
        """Without new_extruder_temp, _heat_next_extruder and _cooldown are not called."""
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane)  # no new_extruder_temp
        obj._heat_next_extruder.assert_not_called()
        obj._cooldown_last_extruder.assert_not_called()

    def test_lane_status_not_set_to_loaded_for_normal_toolchange(self):
        """For a normal toolchange (not infinite runout), cur_lane.status is NOT forced to LOADED."""
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        cur_lane.status = AFCLaneState.LOADED  # already loaded
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        # status should not have been touched by the infinite_runout branch
        assert cur_lane.status == AFCLaneState.LOADED

    def test_no_cooldown_when_same_extruder(self):
        """If cur_lane uses the same extruder as current, cooldown is not called."""
        obj, cur_lane, current_lane = _make_afc_for_change_tool(
            next_extruder_name="extruder0",   # same as current
            current_extruder_name="extruder0",
        )
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj._cooldown_last_extruder.assert_not_called()

class TestChangeTool_NewExtruderTemp_Park_Wipe: 
    def test_no_park_called(self):
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj.gcode.run_script_from_command.assert_not_called()
        info_msgs = [m for lvl, m in obj.logger.messages if lvl == "info"]
        assert not any("Parking while waiting for extruder to heat." in m for m in info_msgs)
    
    def test_park_called_infinite_runout(self):
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.park = True
        obj.park_cmd = "AFC_PARK"
        cur_lane.status = AFCLaneState.INFINITE_RUNOUT
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj.gcode.run_script_from_command.assert_called_once()
        info_msgs = [m for lvl, m in obj.logger.messages if lvl == "info"]
        assert any("Parking while waiting for extruder to heat." in m for m in info_msgs)
        assert obj.gcode.run_script_from_command.call_args.args[0] == \
            f"{obj.park_cmd} EXTRUDER={current_lane.extruder_obj.name}"
    
    def test_park_set_not_infinite_runout(self):
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.park = True
        obj.park_cmd = "AFC_PARK"
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj.gcode.run_script_from_command.assert_not_called()
    
    def test_park_bool_set_cmd_not_set(self):
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.park = True
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj.gcode.run_script_from_command.assert_not_called()
        info_msgs = [m for lvl, m in obj.logger.messages if lvl == "info"]
        assert not any("Parking while waiting for extruder to heat." in m for m in info_msgs)
    
    def test_park_bool_not_set_cmd_set(self):
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.park_cmd = "AFC_PARK"
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj.gcode.run_script_from_command.assert_not_called()
        info_msgs = [m for lvl, m in obj.logger.messages if lvl == "info"]
        assert not any("Parking while waiting for extruder to heat." in m for m in info_msgs)

    def test_no_wipe_called(self):
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj.gcode.run_script_from_command.assert_not_called()
        info_msgs = [m for lvl, m in obj.logger.messages if lvl == "info"]
        assert not any("Wiping ooze..." in m for m in info_msgs)
    
    def test_wipe_called_infinite_runout(self):
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.wipe = True
        obj.wipe_cmd = "AFC_WIPE"
        cur_lane.status = AFCLaneState.INFINITE_RUNOUT
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj.gcode.run_script_from_command.assert_called_once()
        info_msgs = [m for lvl, m in obj.logger.messages if lvl == "info"]
        assert any("Wiping ooze..." in m for m in info_msgs)
        assert obj.gcode.run_script_from_command.call_args.args[0] == \
            f"{obj.wipe_cmd} EXTRUDER={current_lane.extruder_obj.name}"
    
    def test_wipe_set_not_infinite_runout(self):
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.wipe = True
        obj.wipe_cmd = "AFC_WIPE"
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj.gcode.run_script_from_command.assert_not_called()
    
    def test_wipe_bool_set_cmd_not_set(self):
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.wipe = True
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj.gcode.run_script_from_command.assert_not_called()
        info_msgs = [m for lvl, m in obj.logger.messages if lvl == "info"]
        assert not any("Wiping ooze..." in m for m in info_msgs)
    
    def test_wipe_bool_not_set_cmd_set(self):
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.wipe_cmd = "AFC_WIPE"
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj.gcode.run_script_from_command.assert_not_called()
        info_msgs = [m for lvl, m in obj.logger.messages if lvl == "info"]
        assert not any("Wiping ooze..." in m for m in info_msgs)


# ── CHANGE_TOOL: early-exit paths ─────────────────────────────────────────────

class TestChangeTool_EarlyExits:
    """
    CHANGE_TOOL returns before doing any real work in two situations:
      1. The bypass sensor is active (_check_bypass returns True).
      2. _heat_next_extruder returns a falsy result.

    NOTE: MockLogger (from conftest.py) is a real class whose methods append
    (level, msg) tuples to self.messages.  Logger assertions throughout all
    CHANGE_TOOL tests use obj.logger.messages, NOT MagicMock-style assertions.
    """

    def test_bypass_active_does_not_call_save_pos(self):
        """When _check_bypass returns True the method returns without calling save_pos."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj._check_bypass.return_value = True
        obj.CHANGE_TOOL(cur_lane)
        obj.save_pos.assert_not_called()

    def test_bypass_active_does_not_call_tool_load(self):
        """When bypass is active TOOL_LOAD is never reached."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj._check_bypass.return_value = True
        obj.CHANGE_TOOL(cur_lane)
        obj.TOOL_LOAD.assert_not_called()

    def test_bypass_active_does_not_call_tool_unload(self):
        """When bypass is active TOOL_UNLOAD is never reached."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj._check_bypass.return_value = True
        obj.CHANGE_TOOL(cur_lane)
        obj.TOOL_UNLOAD.assert_not_called()

    def test_bypass_active_still_resets_next_lane_load(self):
        """next_lane_load is reset to None in the finally block even after bypass return."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj._check_bypass.return_value = True
        obj.CHANGE_TOOL(cur_lane)
        assert obj.next_lane_load is None

    def test_heat_next_extruder_failure_calls_error_fix(self):
        """When _heat_next_extruder returns falsy, error.fix is called and execution stops."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj._heat_next_extruder.return_value = None
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj.error.fix.assert_called_once()

    def test_heat_next_extruder_failure_does_not_call_tool_load(self):
        """When _heat_next_extruder returns falsy, TOOL_LOAD is never reached."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj._heat_next_extruder.return_value = None
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj.TOOL_LOAD.assert_not_called()

    def test_heat_next_extruder_failure_passes_next_lane_name_to_error(self):
        """error.fix second arg is the requested lane name (self.next_lane_load)."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj._heat_next_extruder.return_value = None
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        assert obj.error.fix.call_args.args[1] == cur_lane.name

    def test_heat_next_extruder_failure_still_resets_next_lane_load(self):
        """next_lane_load is reset to None in finally even after heat failure return."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj._heat_next_extruder.return_value = None
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        assert obj.next_lane_load is None


# ── CHANGE_TOOL: same-lane-already-loaded branch ──────────────────────────────

class TestChangeTool_SameLane:
    """
    When cur_lane.name == self.current the else-branch runs:
    it re-syncs the extruder and conditionally bumps current_toolchange.
    """

    def _make(self, error_state=False, current_toolchange=0):
        obj, cur_lane, _ = _make_afc_for_change_tool()
        # self.current is a property wrapping get_current_lane().
        # Point it at the requested lane so the else-branch fires.
        obj.function.get_current_lane.return_value = cur_lane.name
        obj.error_state = error_state
        obj.current_toolchange = current_toolchange
        return obj, cur_lane

    def test_handle_activate_extruder_called_with_zero(self):
        """_handle_activate_extruder(0) is called to re-sync the lane."""
        obj, cur_lane = self._make()
        obj.CHANGE_TOOL(cur_lane)
        obj.function._handle_activate_extruder.assert_called_once_with(0)

    def test_save_pos_not_called(self):
        """save_pos is NOT called when the lane is already loaded."""
        obj, cur_lane = self._make()
        obj.CHANGE_TOOL(cur_lane)
        obj.save_pos.assert_not_called()

    def test_tool_unload_not_called(self):
        """TOOL_UNLOAD is NOT called when the lane is already loaded."""
        obj, cur_lane = self._make()
        obj.CHANGE_TOOL(cur_lane)
        obj.TOOL_UNLOAD.assert_not_called()

    def test_tool_load_not_called(self):
        """TOOL_LOAD is NOT called when the lane is already loaded."""
        obj, cur_lane = self._make()
        obj.CHANGE_TOOL(cur_lane)
        obj.TOOL_LOAD.assert_not_called()

    def test_logs_already_loaded(self):
        """An 'already loaded' message is appended to logger.messages at info level."""
        obj, cur_lane = self._make()
        obj.CHANGE_TOOL(cur_lane)
        msgs = [m for lvl, m in obj.logger.messages if lvl == "info"]
        assert any("already loaded" in m for m in msgs)

    def test_current_toolchange_incremented_from_minus_one(self):
        """current_toolchange increments from -1 to 0 when not in error_state."""
        obj, cur_lane = self._make(error_state=False, current_toolchange=-1)
        obj.CHANGE_TOOL(cur_lane)
        assert obj.current_toolchange == 0

    def test_current_toolchange_not_incremented_when_not_minus_one(self):
        """current_toolchange is left unchanged when it is already 0."""
        obj, cur_lane = self._make(error_state=False, current_toolchange=0)
        obj.CHANGE_TOOL(cur_lane)
        assert obj.current_toolchange == 0

    def test_current_toolchange_not_incremented_in_error_state(self):
        """current_toolchange is NOT incremented when error_state is True, even from -1."""
        obj, cur_lane = self._make(error_state=True, current_toolchange=-1)
        obj.CHANGE_TOOL(cur_lane)
        assert obj.current_toolchange == -1

    def test_next_lane_load_reset_to_none(self):
        """next_lane_load is reset to None by the finally block on the same-lane path."""
        obj, cur_lane = self._make()
        obj.CHANGE_TOOL(cur_lane)
        assert obj.next_lane_load is None


# ── CHANGE_TOOL: unload path ──────────────────────────────────────────────────

class TestChangeTool_UnloadPath:
    """Tests for every variation of the unload decision inside CHANGE_TOOL."""

    def test_tool_unload_skipped_when_nothing_loaded(self):
        """When self.current is None (nothing loaded) TOOL_UNLOAD is not called."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.function.get_current_lane.return_value = None
        obj.CHANGE_TOOL(cur_lane)
        obj.TOOL_UNLOAD.assert_not_called()

    def test_tool_load_still_called_when_nothing_loaded(self):
        """When self.current is None the toolchange still proceeds to TOOL_LOAD."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.function.get_current_lane.return_value = None
        obj.CHANGE_TOOL(cur_lane)
        obj.TOOL_LOAD.assert_called_once()

    def test_tool_unload_skipped_when_prep_not_done(self):
        """When cur_lane._afc_prep_done is False the whole unload block is skipped."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        cur_lane._afc_prep_done = False
        obj.CHANGE_TOOL(cur_lane)
        obj.TOOL_UNLOAD.assert_not_called()

    def test_tool_load_still_called_when_prep_not_done(self):
        """Even with _afc_prep_done=False, TOOL_LOAD is still attempted."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        cur_lane._afc_prep_done = False
        obj.CHANGE_TOOL(cur_lane)
        obj.TOOL_LOAD.assert_called_once()

    def test_afc_error_called_when_current_lane_not_in_lanes_dict(self):
        """AFC_error is called when lanes.get(self.current) returns None."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        del obj.lanes["lane1"]
        obj.CHANGE_TOOL(cur_lane)
        obj.error.AFC_error.assert_called_once()

    def test_afc_error_message_contains_missing_lane_name(self):
        """The AFC_error message contains the name of the unresolvable current lane."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        del obj.lanes["lane1"]
        obj.CHANGE_TOOL(cur_lane)
        error_msg = obj.error.AFC_error.call_args.args[0]
        assert "lane1" in error_msg

    def test_tool_load_not_called_when_current_lane_unknown(self):
        """TOOL_LOAD is never reached when the current lane cannot be resolved."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        del obj.lanes["lane1"]
        obj.CHANGE_TOOL(cur_lane)
        obj.TOOL_LOAD.assert_not_called()

    def test_tool_unload_called_with_correct_lane_object(self):
        """TOOL_UNLOAD receives the lane object for the snapshot of self.current.
        Because current_lane_name = self.current is captured before TOOL_UNLOAD
        is invoked, the reference is stable even if get_current_lane() changes later."""
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane)
        assert obj.TOOL_UNLOAD.call_args.args[0] is current_lane

    def test_tool_unload_called_with_set_start_time_false(self):
        """TOOL_UNLOAD is always called with set_start_time=False during a toolchange."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane)
        assert obj.TOOL_UNLOAD.call_args.kwargs["set_start_time"] is False

    def test_error_fix_called_when_tool_unload_returns_false(self):
        """When TOOL_UNLOAD returns False, error.fix is called to signal the failure."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.TOOL_UNLOAD.return_value = False
        obj.CHANGE_TOOL(cur_lane)
        obj.error.fix.assert_called_once()

    def test_error_fix_receives_unload_lane_object(self):
        """error.fix second arg is the pre-resolved unload_lane, not None or cur_lane."""
        obj, cur_lane, current_lane = _make_afc_for_change_tool()
        obj.TOOL_UNLOAD.return_value = False
        obj.CHANGE_TOOL(cur_lane)
        assert obj.error.fix.call_args.args[1] is current_lane

    def test_tool_load_not_called_when_tool_unload_fails(self):
        """Execution aborts after failed TOOL_UNLOAD; TOOL_LOAD is never called."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.TOOL_UNLOAD.return_value = False
        obj.CHANGE_TOOL(cur_lane)
        obj.TOOL_LOAD.assert_not_called()

    def test_next_lane_load_reset_after_unload_failure(self):
        """The finally block resets next_lane_load to None even after TOOL_UNLOAD failure."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.TOOL_UNLOAD.return_value = False
        obj.CHANGE_TOOL(cur_lane)
        assert obj.next_lane_load is None


# ── CHANGE_TOOL: load path outcomes ───────────────────────────────────────────

class TestChangeTool_LoadPath:
    """Tests for the TOOL_LOAD call and its success/failure outcomes."""

    def test_tool_load_called_with_correct_lane(self):
        """TOOL_LOAD receives cur_lane as its first positional argument."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane)
        assert obj.TOOL_LOAD.call_args.args[0] is cur_lane

    def test_tool_load_called_with_purge_length(self):
        """TOOL_LOAD receives the purge_length value passed to CHANGE_TOOL."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane, purge_length=50.0)
        assert obj.TOOL_LOAD.call_args.args[1] == 50.0

    def test_tool_load_called_with_set_start_time_false(self):
        """TOOL_LOAD is called with set_start_time=False during a toolchange."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane)
        assert obj.TOOL_LOAD.call_args.kwargs["set_start_time"] is False

    def test_restore_pos_called_on_success_with_default_restore_pos(self):
        """restore_pos() is called when TOOL_LOAD succeeds and restore_pos=True (default)."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane)
        obj.restore_pos.assert_called_once()

    def test_restore_pos_not_called_with_restore_pos_false(self):
        """restore_pos() is NOT called when restore_pos=False even on a successful load."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane, restore_pos=False)
        obj.restore_pos.assert_not_called()

    def test_restore_pos_suppressed_when_error_state_set_during_load(self):
        """If TOOL_LOAD sets error_state=True, restore_pos is not called."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        def _load_sets_error(*args, **kwargs):
            obj.error_state = True
            return True
        obj.TOOL_LOAD.side_effect = _load_sets_error
        obj.CHANGE_TOOL(cur_lane)
        obj.restore_pos.assert_not_called()

    def test_in_toolchange_cleared_after_successful_load(self):
        """in_toolchange is False after a successful TOOL_LOAD."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane)
        assert obj.in_toolchange is False

    def test_increase_toolcount_change_called_on_success(self):
        """cur_lane.extruder_obj.estats.increase_toolcount_change() fires on success.
        increase_toolcount_change lives on AFCExtruderStats (confirmed from AFC_extruder.py)."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane)
        cur_lane.extruder_obj.estats.increase_toolcount_change.assert_called_once()

    def test_average_toolchange_time_recorded_on_success(self):
        """afc_stats.average_toolchange_time.average_time() is called with the elapsed time."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.afcDeltaTime.log_total_time.return_value = 7.5
        obj.CHANGE_TOOL(cur_lane)
        obj.afc_stats.average_toolchange_time.average_time.assert_called_once_with(7.5)

    def test_reset_toolchange_wo_error_called_on_load_failure_not_testing(self):
        """When TOOL_LOAD fails and testing=False, reset_toolchange_wo_error() is called."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.testing = False
        obj.TOOL_LOAD.return_value = False
        obj.CHANGE_TOOL(cur_lane)
        obj.afc_stats.reset_toolchange_wo_error.assert_called_once()

    def test_reset_toolchange_wo_error_skipped_on_load_failure_when_testing(self):
        """When TOOL_LOAD fails and testing=True, reset_toolchange_wo_error() is skipped."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.testing = True
        obj.TOOL_LOAD.return_value = False
        obj.CHANGE_TOOL(cur_lane)
        obj.afc_stats.reset_toolchange_wo_error.assert_not_called()

    def test_wait_for_temp_called_with_heater_target_and_deadband(self):
        """_wait_for_temp_within_tolerance(heater, target_temp, deadband) is called.
        deadband defaults to 2.0 on AFCExtruder (confirmed from AFC_extruder.py)."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        next_extruder_obj, target_temp = obj._heat_next_extruder.return_value
        next_extruder_obj.deadband = 2.0
        expected_heater = next_extruder_obj.get_heater()
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        obj._wait_for_temp_within_tolerance.assert_called_once_with(
            expected_heater, target_temp, 2.0
        )


# ── CHANGE_TOOL: state management ────────────────────────────────────────────

class TestChangeTool_StateManagement:
    """Tests for flags and counters managed across a full CHANGE_TOOL call."""

    def test_next_lane_load_set_to_cur_lane_name_before_save_pos(self):
        """next_lane_load is set to cur_lane.name before save_pos is called."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        captured = {}
        original_save = obj.save_pos
        def _capture(*a, **kw):
            captured["next_lane_load"] = obj.next_lane_load
            return original_save(*a, **kw)
        obj.save_pos = _capture
        obj.CHANGE_TOOL(cur_lane)
        assert captured["next_lane_load"] == cur_lane.name

    def test_next_lane_load_reset_to_none_after_successful_change(self):
        """next_lane_load is None at the end of a successful toolchange (finally block)."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane)
        assert obj.next_lane_load is None

    def test_next_lane_load_reset_to_none_after_unload_failure(self):
        """next_lane_load is None in the finally block even when TOOL_UNLOAD fails."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.TOOL_UNLOAD.return_value = False
        obj.CHANGE_TOOL(cur_lane)
        assert obj.next_lane_load is None

    def test_in_toolchange_true_when_tool_unload_is_called(self):
        """in_toolchange is True by the time TOOL_UNLOAD fires (set before the prep block)."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        captured = {}
        original_unload = obj.TOOL_UNLOAD
        def _capture(*args, **kwargs):
            captured["in_toolchange"] = obj.in_toolchange
            return original_unload(*args, **kwargs)
        obj.TOOL_UNLOAD = _capture
        obj.CHANGE_TOOL(cur_lane)
        assert captured["in_toolchange"] is True

    def test_save_pos_called_when_switching_lanes(self):
        """save_pos() is called when the requested lane differs from self.current."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.CHANGE_TOOL(cur_lane)
        obj.save_pos.assert_called_once()

    def test_save_pos_not_called_for_same_lane(self):
        """save_pos() is NOT called when the requested lane is already current."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.function.get_current_lane.return_value = cur_lane.name
        obj.CHANGE_TOOL(cur_lane)
        obj.save_pos.assert_not_called()

    def test_current_toolchange_incremented_when_within_total(self):
        """current_toolchange increments when error_state=False, number_of_toolchanges != 0,
        and current_toolchange < number_of_toolchanges."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.error_state = False
        obj.number_of_toolchanges = 5
        obj.current_toolchange = 2
        obj.CHANGE_TOOL(cur_lane)
        assert obj.current_toolchange == 3

    def test_current_toolchange_not_incremented_in_error_state(self):
        """current_toolchange is NOT incremented when error_state is True."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.error_state = True
        obj.number_of_toolchanges = 5
        obj.current_toolchange = 2
        obj.CHANGE_TOOL(cur_lane)
        assert obj.current_toolchange == 2

    def test_current_toolchange_not_incremented_when_number_of_toolchanges_zero(self):
        """current_toolchange is NOT incremented when number_of_toolchanges == 0."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.error_state = False
        obj.number_of_toolchanges = 0
        obj.current_toolchange = 0
        obj.CHANGE_TOOL(cur_lane)
        assert obj.current_toolchange == 0

    def test_current_toolchange_not_incremented_when_already_at_max(self):
        """current_toolchange is NOT incremented when it already equals number_of_toolchanges."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.error_state = False
        obj.number_of_toolchanges = 3
        obj.current_toolchange = 3
        obj.CHANGE_TOOL(cur_lane)
        assert obj.current_toolchange == 3


# ── CHANGE_TOOL: infinite runout ──────────────────────────────────────────────

class TestChangeTool_InfiniteRunout:
    """
    Tests for the infinite_runout sub-path.

    AFCLaneState is a plain class with string constants (NOT an Enum) —
    confirmed from AFC_lane.py.  AFCLaneState.INFINITE_RUNOUT == "Infinite Runout".
    """

    def _make_infinite_runout(self, same_extruder=False):
        """
        Fixture where cur_lane is in INFINITE_RUNOUT state.
        same_extruder=True: next and current extruders share a name so that
        adjusting_temperature evaluates to False even though infinite_runout is True.
        """
        next_ext = "extruder0" if same_extruder else "extruder1"
        obj, cur_lane, current_lane = _make_afc_for_change_tool(
            next_extruder_name=next_ext,
            current_extruder_name="extruder0",
        )
        cur_lane.status = AFCLaneState.INFINITE_RUNOUT
        return obj, cur_lane, current_lane

    def test_adjusting_temperature_true_when_extruder_changes(self):
        """adjusting_temperature is True (heat path entered) when the extruder differs."""
        call_order = []
        obj, cur_lane, _ = self._make_infinite_runout(same_extruder=False)
        obj._heat_next_extruder.side_effect = lambda **kw: (
            call_order.append("heat"),
            obj._heat_next_extruder.return_value
        )[1]
        obj.TOOL_UNLOAD.side_effect = lambda *a, **kw: (
            call_order.append("unload"),
            obj.TOOL_UNLOAD.return_value
        )[1]
        obj._cooldown_last_extruder.side_effect = lambda *a, **kw: call_order.append("cool")
        obj.CHANGE_TOOL(cur_lane)
        obj._heat_next_extruder.assert_called_once()
        assert call_order == ["heat", "unload", "cool"], f"Wrong call order: {call_order}"

    def test_adjusting_temperature_false_when_same_extruder(self):
        """adjusting_temperature is False when infinite_runout but the extruder is unchanged."""
        obj, cur_lane, _ = self._make_infinite_runout(same_extruder=True)
        obj.CHANGE_TOOL(cur_lane)
        obj._heat_next_extruder.assert_not_called()

    def test_heat_next_extruder_called_with_next_temp_none(self):
        """_heat_next_extruder is called with next_temp=None for infinite_runout
        so that it reads the current target temp rather than forcing a new value."""
        obj, cur_lane, _ = self._make_infinite_runout(same_extruder=False)
        obj.CHANGE_TOOL(cur_lane)
        obj._heat_next_extruder.assert_called_once_with(wait=False, next_temp=None)

    def test_cur_lane_status_set_to_loaded(self):
        """cur_lane.status is forced to AFCLaneState.LOADED during infinite_runout."""
        obj, cur_lane, _ = self._make_infinite_runout(same_extruder=False)
        assert cur_lane.status == AFCLaneState.INFINITE_RUNOUT
        obj.CHANGE_TOOL(cur_lane)
        assert cur_lane.status == AFCLaneState.LOADED

    def test_normal_toolchange_does_not_alter_cur_lane_status(self):
        """A normal toolchange (not infinite_runout) leaves cur_lane.status untouched."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        cur_lane.status = AFCLaneState.LOADED
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        assert cur_lane.status == AFCLaneState.LOADED
    
    def test_force_unload_not_standalone_lane(self):
        obj, cur_lane, current_lane = self._make_infinite_runout(same_extruder=False)
        current_lane.extruder_obj.is_standalone.return_value = False
        obj.CHANGE_TOOL(cur_lane)
        obj.LANE_UNLOAD.assert_called_once()
    
    def test_force_unload_standalone_lane(self):
        obj, cur_lane, current_lane = self._make_infinite_runout(same_extruder=False)
        current_lane.extruder_obj.is_standalone.return_value = True
        obj.CHANGE_TOOL(cur_lane)
        obj.LANE_UNLOAD.assert_not_called()
    
    def test_force_unload_direct_lane(self):
        obj, cur_lane, current_lane = self._make_infinite_runout(same_extruder=False)
        current_lane.is_direct_hub = MagicMock(return_value = True)
        obj.CHANGE_TOOL(cur_lane)
        obj.LANE_UNLOAD.assert_not_called()


# ── CHANGE_TOOL: exception handling ──────────────────────────────────────────

class TestChangeTool_ExceptionHandling:
    """
    Tests for the try/except/finally wrapper added around CHANGE_TOOL.

    The bare 'except Exception' block:
      - swallows the exception (does not re-raise)
      - appends an ("error", ...) entry to logger.messages via MockLogger
      - calls self.error.AFC_error(...)

    The 'finally' block:
      - always resets self.next_lane_load = None
      - always calls self.function.log_toolhead_pos(...)
    """

    def _make_raising_fixture(self, exc=RuntimeError("simulated failure")):
        """Return a fixture where TOOL_LOAD raises an unexpected exception."""
        obj, cur_lane, _ = _make_afc_for_change_tool()
        obj.TOOL_LOAD.side_effect = exc
        return obj, cur_lane

    def test_unexpected_exception_is_swallowed(self):
        """An unexpected exception from TOOL_LOAD must not propagate to the caller."""
        obj, cur_lane = self._make_raising_fixture()
        try:
            obj.CHANGE_TOOL(cur_lane)   # must not raise
        except Exception as exc:
            pytest.fail(f"CHANGE_TOOL propagated an unexpected exception: {exc}")

    def test_unexpected_exception_calls_afc_error(self):
        """error.AFC_error is called when an unexpected exception is caught."""
        obj, cur_lane = self._make_raising_fixture()
        obj.CHANGE_TOOL(cur_lane)
        obj.error.AFC_error.assert_called_once()

    def test_unexpected_exception_logs_error_message(self):
        """An error-level entry is appended to logger.messages on unexpected exception."""
        obj, cur_lane = self._make_raising_fixture()
        obj.CHANGE_TOOL(cur_lane)
        error_msgs = [m for lvl, m in obj.logger.messages if lvl == "error"]
        assert any("CHANGE_TOOL" in m for m in error_msgs)

    def test_next_lane_load_always_reset_on_exception(self):
        """The finally block resets next_lane_load to None even when an exception fires."""
        obj, cur_lane = self._make_raising_fixture()
        obj.CHANGE_TOOL(cur_lane)
        assert obj.next_lane_load is None

    def test_afc_error_pause_arg_reflects_in_print(self):
        """error.AFC_error is called with pause= matching function.in_print()."""
        obj, cur_lane = self._make_raising_fixture()
        obj.function.in_print.return_value = True
        obj.CHANGE_TOOL(cur_lane)
        call_kwargs = obj.error.AFC_error.call_args.kwargs
        assert call_kwargs.get("pause") is True


# ── cmd_CHANGE_TOOL: NEW_EXTRUDER_TEMP parameter parsing ─────────────────────

class TestCmdChangeTool_NewExtruderTempParsing:
    """Tests for NEW_EXTRUDER_TEMP parameter parsing in cmd_CHANGE_TOOL."""

    def _make_gcmd(self, new_extruder_temp_str, lane="lane1", purge_length=None):
        gcmd = MagicMock()
        # Use a T0 command line so cmd_CHANGE_TOOL takes the simple else-branch
        # (no "CHANGE" in command) and Tcmd = "T0" directly.
        gcmd.get_commandline.return_value = "T0"
        gcmd.get.side_effect = lambda key, default=None: {
            "PURGE_LENGTH": purge_length,
            "NEW_EXTRUDER_TEMP": new_extruder_temp_str,
        }.get(key, default)
        return gcmd

    def _make_afc_for_cmd(self):
        obj = _make_afc()
        obj.CHANGE_TOOL = MagicMock()
        obj.error = MagicMock()
        obj.function.check_homed.return_value = True
        obj._check_bypass = MagicMock(return_value=False)
        lane = MagicMock()
        obj.lanes["lane1"] = lane
        obj.tool_cmds["T0"] = "lane1"
        return obj

    def test_plain_numeric_string_parsed_to_float(self):
        """'220' → 220.0"""
        obj = self._make_afc_for_cmd()
        gcmd = self._make_gcmd("220")
        obj.cmd_CHANGE_TOOL(gcmd)
        _, kwargs = obj.CHANGE_TOOL.call_args
        assert kwargs["new_extruder_temp"] == 220.0

    def test_equals_prefixed_string_stripped_and_parsed(self):
        """'=220' → 220.0 (Klipper T0 macro compat)"""
        obj = self._make_afc_for_cmd()
        gcmd = self._make_gcmd("=220")
        obj.cmd_CHANGE_TOOL(gcmd)
        _, kwargs = obj.CHANGE_TOOL.call_args
        assert kwargs["new_extruder_temp"] == 220.0

    def test_none_new_extruder_temp_passes_none(self):
        """When the parameter is absent (None), None is forwarded to CHANGE_TOOL."""
        obj = self._make_afc_for_cmd()
        gcmd = self._make_gcmd(None)
        obj.cmd_CHANGE_TOOL(gcmd)
        _, kwargs = obj.CHANGE_TOOL.call_args
        assert kwargs["new_extruder_temp"] is None

    def test_float_string_parsed_correctly(self):
        """'215.5' parses to 215.5"""
        obj = self._make_afc_for_cmd()
        gcmd = self._make_gcmd("215.5")
        obj.cmd_CHANGE_TOOL(gcmd)
        _, kwargs = obj.CHANGE_TOOL.call_args
        assert kwargs["new_extruder_temp"] == 215.5

    def test_invalid_new_extruder_temp_reports_error_and_does_not_call_change_tool(self):
        """A non-numeric NEW_EXTRUDER_TEMP triggers AFC_error and aborts without calling CHANGE_TOOL."""
        obj = self._make_afc_for_cmd()
        gcmd = self._make_gcmd("notanumber")
        obj.cmd_CHANGE_TOOL(gcmd)
        obj.error.AFC_error.assert_called_once()
        error_msg = obj.error.AFC_error.call_args.args[0]
        assert "NEW_EXTRUDER_TEMP" in error_msg
        obj.CHANGE_TOOL.assert_not_called()

    def test_invalid_purge_length_reports_error_and_does_not_call_change_tool(self):
        """A non-numeric PURGE_LENGTH triggers AFC_error and aborts without calling CHANGE_TOOL."""
        obj = self._make_afc_for_cmd()
        gcmd = MagicMock()
        gcmd.get_commandline.return_value = "T0"
        gcmd.get.side_effect = lambda key, default=None: {
            "PURGE_LENGTH": "notanumber",
            "NEW_EXTRUDER_TEMP": None,
        }.get(key, default)
        obj.cmd_CHANGE_TOOL(gcmd)
        obj.error.AFC_error.assert_called_once()
        error_msg = obj.error.AFC_error.call_args.args[0]
        assert "PURGE_LENGTH" in error_msg
        obj.CHANGE_TOOL.assert_not_called()

class TestCmdChange_ToolCheckBypass_CheckHomed():
    def _make_gcmd(self):
        gcmd = MagicMock()
        # Use a T0 command line so cmd_CHANGE_TOOL takes the simple else-branch
        # (no "CHANGE" in command) and Tcmd = "T0" directly.
        gcmd.get_commandline.return_value = "T0"
        return gcmd

    def test_check_bypass_True(self):
        obj, _, _ = _make_afc_for_change_tool()
        gcmd = self._make_gcmd()
        obj._check_bypass.return_value = True

        ret = obj.cmd_CHANGE_TOOL(gcmd)
        assert not ret

    def test_check_homed_False(self):
        obj, _, _ = _make_afc_for_change_tool()
        gcmd = self._make_gcmd()
        obj.function.check_homed.return_value = False

        ret = obj.cmd_CHANGE_TOOL(gcmd)
        assert not ret

class TestCmdChangeTool_SnapmakerPath:
    def _make_gcmd(self, tcmd="T0"):
        gcmd = MagicMock()
        # Use a T0 command line so cmd_CHANGE_TOOL takes the simple else-branch
        # (no "CHANGE" in command) and Tcmd = "T0" directly.
        gcmd.get_commandline.return_value = f"{tcmd} A0"
        gcmd.get.side_effect = lambda key, default=None: {
            "A": "0"
        }.get(key, default)
        return gcmd
    def get_snapmaker_config_dir():
            pass

    def test_setting_A_param(self, monkeypatch):
        obj, _, _ = _make_afc_for_change_tool()
        monkeypatch.setattr(Printer, "get_snapmaker_config_dir", True, raising=False)
        gcmd = self._make_gcmd()
        obj.gcode = MagicMock()
        obj.gcode.ready_gcode_handlers = {"_T0": MagicMock()}
        ret = obj.cmd_CHANGE_TOOL(gcmd)
        obj.gcode.run_script_from_command.assert_called_once()
    
    def test_setting_A_param_snapmaker_false(self):
        obj, _, _ = _make_afc_for_change_tool()
        obj.function.check_homed.return_value = False
        gcmd = self._make_gcmd()
        obj.gcode = MagicMock()
        obj.gcode.ready_gcode_handlers = {"_T0": MagicMock()}
        ret = obj.cmd_CHANGE_TOOL(gcmd)
        obj.gcode.run_script_from_command.assert_not_called()
        assert not ret

    def test_setting_A_param_not_in_ready_gcode_handlers(self, monkeypatch):
        obj, _, _ = _make_afc_for_change_tool()
        obj.function.check_homed.return_value = False
        monkeypatch.setattr(Printer, "get_snapmaker_config_dir", True, raising=False)
        obj.function.check_homed.return_value = False
        gcmd = self._make_gcmd("T5")
        obj.gcode = MagicMock()
        obj.gcode.ready_gcode_handlers = {"_T0": MagicMock()}
        ret = obj.cmd_CHANGE_TOOL(gcmd)
        obj.gcode.run_script_from_command.assert_not_called()
        assert not ret

# ── TOOL_LOAD: destination extruder already has a different lane loaded ───────

def _make_afc_for_dest_extruder_loaded():
    """
    Build an afc instance simulating a post-restart state where:
      - The active extruder is 'extruder1' (already current, no swap needed)
      - 'extruder1' still has lane4 marked as loaded
      - The print wants to load lane2 (also on extruder1)

    afc.current is a property backed by get_current_lane(); stubbing
    get_current_lane() to return 'lane2' makes self.current equal the
    target so that after the pre-load unload check TOOL_LOAD
    short-circuits without needing to mock the full load sequence.
    """
    from tests.test_AFC_lane import _make_afc_lane

    obj = _make_afc()
    obj.afcDeltaTime = MagicMock()
    obj.afc_stats = MagicMock()
    obj.testing = True
    obj.TOOL_UNLOAD = MagicMock(return_value=True)
    obj._check_bypass = MagicMock(return_value=False)
    obj.error = MagicMock()
    obj.verify_macro_positions = MagicMock(return_value="")

    # Destination extruder — has lane4 already loaded
    dest_extruder = MagicMock()
    dest_extruder.name = "extruder1"
    dest_extruder.lane_loaded = "lane4"
    dest_extruder.estats = MagicMock()

    # The lane that is already loaded on the extruder
    loaded_lane = _make_afc_lane("AFC_stepper lane4")
    loaded_lane.extruder_obj = dest_extruder
    obj.lanes["lane4"] = loaded_lane

    # The target lane on the same extruder
    target_lane = _make_afc_lane("AFC_stepper lane2")
    target_lane.extruder_obj = dest_extruder
    obj.lanes["lane2"] = target_lane

    # Current extruder is already extruder1 — no tool swap
    obj.function.get_current_extruder.return_value = "extruder1"
    # self.current == target lane so the full load body is skipped
    obj.function.get_current_lane.return_value = "lane2"
    obj.function.check_homed.return_value = True
    obj.function.in_print.return_value = False
    obj.function.is_paused.return_value = False
    obj.function.log_toolhead_pos = MagicMock()

    return obj, target_lane, loaded_lane, dest_extruder


class TestToolLoad_DestExtruderAlreadyLoaded:
    """
    Tests for the pre-load unload check in TOOL_LOAD.

    Reproduces the post-restart scenario where the destination extruder still
    has a lane marked as loaded while a different lane is being requested.
    """

    def test_aborts_with_clear_error_when_stale_lane_not_in_lanes(self):
        """If the stale loaded lane name is not in self.lanes, report an explicit error and abort."""
        obj, target_lane, loaded_lane, dest_extruder = _make_afc_for_dest_extruder_loaded()
        # Remove lane4 from lanes so it is unmapped
        del obj.lanes["lane4"]
        result = obj.TOOL_LOAD(target_lane)
        assert result is False
        obj.error.AFC_error.assert_called_once()
        error_msg = obj.error.AFC_error.call_args.args[0]
        assert "extruder1" in error_msg
        assert "lane4" in error_msg
        obj.TOOL_UNLOAD.assert_not_called()

    def test_unloads_when_extruder_already_has_different_lane_loaded(self):
        """TOOL_UNLOAD is called for the already-loaded lane before proceeding."""
        obj, target_lane, loaded_lane, dest_extruder = _make_afc_for_dest_extruder_loaded()
        obj.TOOL_LOAD(target_lane)
        obj.TOOL_UNLOAD.assert_called_once_with(loaded_lane, set_start_time=False)

    def test_aborts_if_unload_fails(self):
        """If TOOL_UNLOAD returns False, TOOL_LOAD returns False immediately."""
        obj, target_lane, loaded_lane, dest_extruder = _make_afc_for_dest_extruder_loaded()
        obj.TOOL_UNLOAD.return_value = False
        result = obj.TOOL_LOAD(target_lane)
        assert result is False

    def test_no_unload_when_extruder_already_has_target_lane_loaded(self):
        """If the extruder already has the target lane loaded, no unload is triggered."""
        obj, target_lane, loaded_lane, dest_extruder = _make_afc_for_dest_extruder_loaded()
        dest_extruder.lane_loaded = "lane2"   # already the target
        obj.TOOL_LOAD(target_lane)
        obj.TOOL_UNLOAD.assert_not_called()

    def test_no_unload_when_extruder_has_nothing_loaded(self):
        """If the extruder has lane_loaded=None, no unload is triggered."""
        obj, target_lane, loaded_lane, dest_extruder = _make_afc_for_dest_extruder_loaded()
        dest_extruder.lane_loaded = None
        obj.TOOL_LOAD(target_lane)
        obj.TOOL_UNLOAD.assert_not_called()

    def test_unloads_stale_lane_after_tool_swap(self):
        """
        The realistic restart scenario: active extruder is different from the
        destination, so tool_swap() fires first, and only THEN the stale-lane
        check runs.

        Specifically:
          - The printer restarts with extruder1 having lane4 still saved as loaded.
          - The active toolhead comes up as 'extruder' (primary, nothing loaded).
          - TOOL_LOAD is called for lane2 (on extruder1).
          - get_current_extruder() != extruder1 → tool_swap() fires.
          - extruder1.lane_loaded = 'lane4' != 'lane2' → stale unload triggers.

        This path is distinct from the other stale-lane tests which start with
        the active extruder already being extruder1 (no swap needed).
        """
        obj, target_lane, loaded_lane, dest_extruder = _make_afc_for_dest_extruder_loaded()

        # Override: active extruder is 'extruder', not 'extruder1' — swap needed
        obj.function.get_current_extruder.return_value = "extruder"
        target_lane.tool_swap = MagicMock()

        call_order = []
        target_lane.tool_swap.side_effect = lambda: call_order.append("swap")
        obj.TOOL_UNLOAD.side_effect = lambda *a, **kw: (call_order.append("unload"), True)[1]

        obj.TOOL_LOAD(target_lane)

        assert "swap" in call_order, "tool_swap was not called"
        assert "unload" in call_order, "TOOL_UNLOAD was not called for the stale lane"
        assert call_order.index("swap") < call_order.index("unload"), (
            f"tool_swap must precede TOOL_UNLOAD; order was {call_order}"
        )
        obj.TOOL_UNLOAD.assert_called_once_with(loaded_lane, set_start_time=False)


# ── td1_present property ─────────────────────────────────────────────────────

def _make_afc_for_td1(*, td1_present=False, last_td1_query=0.0,
                       current_time=100.0, printer_ready=True,
                       moonraker=True, is_printing=False,
                       moonraker_td1_result=True):
    """
    Build an afc instance pre-configured for td1_present property tests.

    Parameters
    ----------
    td1_present        : initial value of _td1_present
    last_td1_query     : initial value of _last_td1_query (seconds, monotonic)
    current_time       : value reactor.monotonic() will return
    printer_ready      : if True, printer.state_message == 'Printer is ready'
    moonraker          : if True, attach a MagicMock moonraker; if False leave None
    is_printing        : return value of function.is_printing(check_movement=True)
    moonraker_td1_result : value moonraker.check_for_td1() returns as [1] index
    """
    obj = _make_afc()
    obj._td1_present = td1_present
    obj._last_td1_query = last_td1_query
    obj.reactor.monotonic = MagicMock(return_value=current_time)
    obj.printer.state_message = 'Printer is ready' if printer_ready else 'Startup'
    obj.function.is_printing = MagicMock(return_value=is_printing)

    if moonraker:
        obj.moonraker = MagicMock()
        obj.moonraker.check_for_td1.return_value = (None, moonraker_td1_result)
    else:
        obj.moonraker = None

    return obj


class TestTd1Present:
    """Tests for the afc.td1_present property."""

    # ── cached-value paths (no moonraker call) ────────────────────────────────

    def test_returns_cached_when_printer_not_ready(self):
        """Returns _td1_present without querying moonraker when printer is not ready."""
        obj = _make_afc_for_td1(td1_present=True, printer_ready=False)
        assert obj.td1_present is True

    def test_returns_cached_when_moonraker_is_none(self):
        """Returns _td1_present without error when moonraker is None."""
        obj = _make_afc_for_td1(td1_present=False, moonraker=False)
        assert obj.td1_present is False

    def test_returns_cached_when_query_interval_too_short(self):
        """Returns _td1_present when less than 30 s have elapsed since last query."""
        # current_time - last_td1_query == 10 s (< 30 s threshold)
        obj = _make_afc_for_td1(td1_present=True, current_time=100.0, last_td1_query=90.0)
        assert obj.td1_present is True
    
    def test_returns_cached_when_query_interval_too_short_td1_false(self):
        """Does not refresh when printer is ready and interval has not passed."""
        obj = _make_afc_for_td1(
            td1_present=False,
            current_time=100.0,
            last_td1_query=90.0,
            is_printing=False,
            moonraker_td1_result=True,
        )
        assert obj.td1_present is False

    def test_returns_cached_when_exactly_30s_elapsed(self):
        """Interval must be *greater than* 30 s; exactly 30 s still returns cached value."""
        obj = _make_afc_for_td1(td1_present=True, current_time=100.0, last_td1_query=70.0)
        assert obj.td1_present is True

    def test_returns_cached_when_is_printing(self):
        """Does not refresh when printer is ready and interval has passed but a print is active."""
        obj = _make_afc_for_td1(
            td1_present=False,
            current_time=100.0,
            last_td1_query=0.0,
            is_printing=True,
            moonraker_td1_result=True,
        )
        assert obj.td1_present is False

    # ── moonraker not called paths ────────────────────────────────────────────

    def test_moonraker_not_called_when_printer_not_ready(self):
        """moonraker.check_for_td1 is never invoked when the printer is not ready."""
        obj = _make_afc_for_td1(printer_ready=False)
        _ = obj.td1_present
        obj.moonraker.check_for_td1.assert_not_called()

    def test_moonraker_not_called_when_interval_too_short(self):
        """moonraker.check_for_td1 is never invoked when the cooldown has not expired."""
        obj = _make_afc_for_td1(current_time=100.0, last_td1_query=90.0)
        _ = obj.td1_present
        obj.moonraker.check_for_td1.assert_not_called()

    def test_moonraker_not_called_when_is_printing(self):
        """moonraker.check_for_td1 is never invoked during an active print."""
        obj = _make_afc_for_td1(current_time=100.0, last_td1_query=0.0, is_printing=True)
        _ = obj.td1_present
        obj.moonraker.check_for_td1.assert_not_called()

    # ── refresh paths ─────────────────────────────────────────────────────────

    def test_returns_moonraker_value_when_all_conditions_met(self):
        """Returns the fresh value from moonraker when printer is ready, interval > 30 s, not printing."""
        obj = _make_afc_for_td1(
            td1_present=False,
            current_time=100.0,
            last_td1_query=0.0,
            is_printing=False,
            moonraker_td1_result=True,
        )
        assert obj.td1_present is True

    def test_updates_internal_td1_present_after_refresh(self):
        """_td1_present is updated to the moonraker result after a successful refresh."""
        obj = _make_afc_for_td1(
            td1_present=False,
            current_time=100.0,
            last_td1_query=0.0,
            moonraker_td1_result=True,
        )
        _ = obj.td1_present
        assert obj._td1_present is True

    def test_updates_last_query_time_after_refresh(self):
        """_last_td1_query is updated to current_time after a successful refresh."""
        obj = _make_afc_for_td1(current_time=100.0, last_td1_query=0.0)
        _ = obj.td1_present
        assert obj._last_td1_query == 100.0

    def test_last_query_time_unchanged_when_not_refreshed(self):
        """_last_td1_query is not modified when the refresh is skipped (interval too short)."""
        obj = _make_afc_for_td1(current_time=100.0, last_td1_query=90.0)
        _ = obj.td1_present
        assert obj._last_td1_query == 90.0

    def test_moonraker_called_once_per_refresh(self):
        """moonraker.check_for_td1 is called exactly once when a refresh is due."""
        obj = _make_afc_for_td1(current_time=100.0, last_td1_query=0.0)
        _ = obj.td1_present
        obj.moonraker.check_for_td1.assert_called_once()

    def test_is_printing_called_with_check_movement(self):
        """is_printing is invoked with check_movement=True when the refresh gate is reached."""
        obj = _make_afc_for_td1(current_time=100.0, last_td1_query=0.0)
        _ = obj.td1_present
        obj.function.is_printing.assert_called_once_with(check_movement=True)

    def test_refresh_stores_false_from_moonraker(self):
        """A False result from moonraker is correctly stored and returned."""
        obj = _make_afc_for_td1(
            td1_present=True,
            current_time=100.0,
            last_td1_query=0.0,
            moonraker_td1_result=False,
        )
        assert obj.td1_present is False
        assert obj._td1_present is False


# ── cmd_TOOL_LOAD: lane_loaded guard ─────────────────────────────────────────

class TestCmdToolLoad_LaneLoadedGuard:
    """
    Tests for the cmd_TOOL_LOAD GCode handler guard.

    The guard should only block when the extruder already has the *target* lane
    loaded (already done). If a *different* lane is loaded, cmd_TOOL_LOAD should
    pass through to TOOL_LOAD which handles the auto-unload.
    """

    def _make_cmd_afc(self):
        from tests.test_AFC_lane import _make_afc_lane
        obj = _make_afc()
        obj.TOOL_LOAD = MagicMock(return_value=True)
        obj.error = MagicMock()
        obj.function.in_print.return_value = False

        extruder = MagicMock()
        extruder.name = "extruder"

        lane = _make_afc_lane("AFC_stepper lane1")
        lane.extruder_obj = extruder
        obj.lanes["lane1"] = lane
        return obj, lane, extruder

    def test_blocks_when_same_lane_already_loaded(self):
        """If the target lane is already loaded, report an error and do not call TOOL_LOAD."""
        obj, lane, extruder = self._make_cmd_afc()
        extruder.lane_loaded = "lane1"  # same as target

        gcmd = MagicMock()
        gcmd.get = lambda key, default=None: {"LANE": "lane1", "PURGE_LENGTH": None}.get(key, default)

        obj.cmd_TOOL_LOAD(gcmd)

        obj.error.AFC_error.assert_called_once()
        obj.TOOL_LOAD.assert_not_called()

    def test_passes_through_when_different_lane_loaded(self):
        """If a different lane is already loaded, cmd_TOOL_LOAD should call TOOL_LOAD (not error)."""
        obj, lane, extruder = self._make_cmd_afc()
        extruder.lane_loaded = "lane2"  # different lane — stale, let TOOL_LOAD handle it

        gcmd = MagicMock()
        gcmd.get = lambda key, default=None: {"LANE": "lane1", "PURGE_LENGTH": None}.get(key, default)

        obj.cmd_TOOL_LOAD(gcmd)

        obj.error.AFC_error.assert_not_called()
        obj.TOOL_LOAD.assert_called_once_with(lane, None)

    def test_passes_through_when_nothing_loaded(self):
        """Normal case: nothing loaded, cmd_TOOL_LOAD proceeds."""
        obj, lane, extruder = self._make_cmd_afc()
        extruder.lane_loaded = None

        gcmd = MagicMock()
        gcmd.get = lambda key, default=None: {"LANE": "lane1", "PURGE_LENGTH": None}.get(key, default)

        obj.cmd_TOOL_LOAD(gcmd)

        obj.error.AFC_error.assert_not_called()
        obj.TOOL_LOAD.assert_called_once_with(lane, None)


# ── capture_toolhead_temp ─────────────────────────────────────────────────────

def _make_afc_for_capture_restore(
    *,
    restore_enabled: bool = True,
    is_printing: bool = False,
    target_temp: float = 200.0,
):
    """
    Build an afc instance pre-configured for capture_toolhead_temp /
    restore_toolhead_temp tests.

    Parameters
    ----------
    restore_enabled : value of restore_extruder_temp_on_load_or_unload
    is_printing     : return value of function.is_printing()
    target_temp     : heater.target_temp on the mock extruder
    """
    obj = _make_afc()
    obj.restore_extruder_temp_on_load_or_unload = restore_enabled
    obj.function.is_printing = MagicMock(return_value=is_printing)
    obj.logger = MagicMock()

    # Build a mock extruder with a heater whose target_temp is controllable
    mock_heater = MagicMock()
    mock_heater.target_temp = target_temp

    mock_extruder = MagicMock()
    mock_extruder.name = "extruder"
    mock_extruder.get_heater.return_value = mock_heater

    # toolhead.get_extruder() returns the same mock extruder by default
    mock_toolhead = MagicMock()
    mock_toolhead.get_extruder.return_value = mock_extruder
    obj.toolhead = mock_toolhead

    # printer.lookup_object('heaters') returns a mock pheaters
    mock_pheaters = MagicMock()
    obj.printer.lookup_object = MagicMock(return_value=mock_pheaters)

    return obj, mock_extruder, mock_heater, mock_pheaters


class TestCaptureToolheadTemp:
    """Tests for afc.capture_toolhead_temp."""

    # ── early-return paths ────────────────────────────────────────────────────

    def test_returns_none_when_restore_disabled(self):
        """Returns None immediately when restore_extruder_temp_on_load_or_unload is False."""
        obj, extruder, heater, _ = _make_afc_for_capture_restore(restore_enabled=False)
        assert obj.capture_toolhead_temp() is None

    def test_returns_none_when_printing_and_not_async(self):
        """Returns None when is_printing() is True and async_capture is False (default)."""
        obj, extruder, heater, _ = _make_afc_for_capture_restore(is_printing=True)
        assert obj.capture_toolhead_temp() is None

    def test_returns_none_when_printing_explicit_async_false(self):
        """Returns None when printing and async_capture is explicitly False."""
        obj, extruder, heater, _ = _make_afc_for_capture_restore(is_printing=True)
        assert obj.capture_toolhead_temp(async_capture=False) is None

    # ── successful capture paths ──────────────────────────────────────────────

    def test_returns_dict_when_not_printing(self):
        """Returns a dict (not None) when the printer is idle."""
        obj, extruder, heater, _ = _make_afc_for_capture_restore(is_printing=False)
        result = obj.capture_toolhead_temp()
        assert result is not None

    def test_returns_dict_when_printing_and_async_capture(self):
        """Returns a dict when printing but async_capture=True bypasses the guard."""
        obj, extruder, heater, _ = _make_afc_for_capture_restore(is_printing=True)
        result = obj.capture_toolhead_temp(async_capture=True)
        assert result is not None

    def test_returned_dict_has_extruder_key(self):
        """The returned dict contains the 'extruder' key."""
        obj, extruder, heater, _ = _make_afc_for_capture_restore()
        result = obj.capture_toolhead_temp()
        assert "extruder" in result

    def test_returned_dict_has_target_temp_key(self):
        """The returned dict contains the 'target_temp' key."""
        obj, extruder, heater, _ = _make_afc_for_capture_restore()
        result = obj.capture_toolhead_temp()
        assert "target_temp" in result

    def test_target_temp_matches_heater(self):
        """target_temp in the returned dict equals heater.target_temp."""
        obj, extruder, heater, _ = _make_afc_for_capture_restore(target_temp=215.0)
        result = obj.capture_toolhead_temp()
        assert result["target_temp"] == 215.0

    def test_uses_toolhead_extruder_when_none_passed(self):
        """When no extruder is passed, uses toolhead.get_extruder()."""
        obj, extruder, heater, _ = _make_afc_for_capture_restore()
        result = obj.capture_toolhead_temp()
        obj.toolhead.get_extruder.assert_called_once()
        assert result["extruder"] is extruder

    def test_uses_passed_extruder_over_toolhead(self):
        """When an extruder is passed explicitly, it is used instead of toolhead.get_extruder()."""
        obj, _, _, _ = _make_afc_for_capture_restore()

        custom_heater = MagicMock()
        custom_heater.target_temp = 240.0
        custom_extruder = MagicMock()
        custom_extruder.get_heater.return_value = custom_heater

        result = obj.capture_toolhead_temp(extruder=custom_extruder)

        obj.toolhead.get_extruder.assert_not_called()
        assert result["extruder"] is custom_extruder
        assert result["target_temp"] == 240.0

    def test_restore_disabled_skips_is_printing_check(self):
        """When restore is disabled, is_printing is never consulted."""
        obj, _, _, _ = _make_afc_for_capture_restore(restore_enabled=False)
        obj.capture_toolhead_temp()
        obj.function.is_printing.assert_not_called()


# ── restore_toolhead_temp ─────────────────────────────────────────────────────

class TestRestoreToolheadTemp:
    """Tests for afc.restore_toolhead_temp."""

    def _make_valid_temp_state(self, target_temp: float = 200.0):
        """Return a minimal temp_state dict with a mock extruder."""
        mock_heater = MagicMock()
        mock_extruder = MagicMock()
        mock_extruder.name = "extruder"
        mock_extruder.get_heater.return_value = mock_heater
        return {"extruder": mock_extruder, "target_temp": target_temp}

    # ── early-return paths ────────────────────────────────────────────────────

    def test_returns_early_when_restore_disabled(self):
        """Does nothing when restore_extruder_temp_on_load_or_unload is False."""
        obj, _, _, pheaters = _make_afc_for_capture_restore(restore_enabled=False)
        temp_state = self._make_valid_temp_state()
        obj.restore_toolhead_temp(temp_state)
        pheaters.set_temperature.assert_not_called()

    def test_returns_early_when_temp_state_is_none(self):
        """Does nothing when temp_state is None."""
        obj, _, _, pheaters = _make_afc_for_capture_restore()
        obj.restore_toolhead_temp(None)
        pheaters.set_temperature.assert_not_called()

    def test_returns_early_when_temp_state_is_empty_dict(self):
        """Does nothing when temp_state is an empty dict (falsy)."""
        obj, _, _, pheaters = _make_afc_for_capture_restore()
        obj.restore_toolhead_temp({})
        pheaters.set_temperature.assert_not_called()

    def test_returns_early_when_printing_and_not_async(self):
        """Does nothing when is_printing() is True and async_restore is False (default)."""
        obj, _, _, pheaters = _make_afc_for_capture_restore(is_printing=True)
        temp_state = self._make_valid_temp_state()
        obj.restore_toolhead_temp(temp_state)
        pheaters.set_temperature.assert_not_called()

    def test_returns_early_when_printing_explicit_async_false(self):
        """Does nothing when printing and async_restore is explicitly False."""
        obj, _, _, pheaters = _make_afc_for_capture_restore(is_printing=True)
        temp_state = self._make_valid_temp_state()
        obj.restore_toolhead_temp(temp_state, async_restore=False)
        pheaters.set_temperature.assert_not_called()

    # ── successful restore paths ──────────────────────────────────────────────

    def test_calls_set_temperature_when_not_printing(self):
        """Calls pheaters.set_temperature when all conditions allow a restore."""
        obj, _, _, pheaters = _make_afc_for_capture_restore(is_printing=False)
        temp_state = self._make_valid_temp_state(target_temp=210.0)
        obj.restore_toolhead_temp(temp_state)
        pheaters.set_temperature.assert_called_once()

    def test_restores_correct_temperature(self):
        """set_temperature is called with the target_temp from temp_state."""
        obj, _, _, pheaters = _make_afc_for_capture_restore()
        temp_state = self._make_valid_temp_state(target_temp=225.0)
        obj.restore_toolhead_temp(temp_state)
        _, call_kwargs = pheaters.set_temperature.call_args
        assert call_kwargs.get("wait") is False
        positional = pheaters.set_temperature.call_args.args
        assert positional[1] == 225.0

    def test_restores_using_extruder_heater(self):
        """set_temperature receives the heater obtained from temp_state['extruder']."""
        obj, _, _, pheaters = _make_afc_for_capture_restore()
        temp_state = self._make_valid_temp_state()
        mock_heater = temp_state["extruder"].get_heater()
        obj.restore_toolhead_temp(temp_state)
        positional = pheaters.set_temperature.call_args.args
        assert positional[0] is mock_heater

    def test_restores_when_printing_and_async_restore(self):
        """Restores temperature when printing but async_restore=True bypasses the guard."""
        obj, _, _, pheaters = _make_afc_for_capture_restore(is_printing=True)
        temp_state = self._make_valid_temp_state()
        obj.restore_toolhead_temp(temp_state, async_restore=True)
        pheaters.set_temperature.assert_called_once()

    def test_logs_info_after_restore(self):
        """logger.info is called with extruder name and target temp after a successful restore."""
        obj, _, _, _ = _make_afc_for_capture_restore()
        temp_state = self._make_valid_temp_state(target_temp=200.0)
        obj.restore_toolhead_temp(temp_state)
        obj.logger.info.assert_called_once()
        log_msg = obj.logger.info.call_args.args[0]
        assert "extruder" in log_msg
        assert "200" in log_msg

    def test_restore_disabled_skips_is_printing_check(self):
        """When restore is disabled, is_printing is never consulted."""
        obj, _, _, _ = _make_afc_for_capture_restore(restore_enabled=False)
        temp_state = self._make_valid_temp_state()
        obj.restore_toolhead_temp(temp_state)
        obj.function.is_printing.assert_not_called()

    # ── exception handling ────────────────────────────────────────────────────

    def test_logs_debug_on_exception(self):
        """If set_temperature raises, logger.debug is called and no exception propagates."""
        obj, _, _, pheaters = _make_afc_for_capture_restore()
        pheaters.set_temperature.side_effect = RuntimeError("heater fault")
        temp_state = self._make_valid_temp_state()
        obj.restore_toolhead_temp(temp_state)   # must not raise
        obj.logger.debug.assert_called_once()

    def test_no_exception_propagated_on_lookup_failure(self):
        """If printer.lookup_object raises, the exception is swallowed."""
        obj, _, _, _ = _make_afc_for_capture_restore()
        obj.printer.lookup_object.side_effect = KeyError("heaters")
        temp_state = self._make_valid_temp_state()
        obj.restore_toolhead_temp(temp_state)   # must not raise
        obj.logger.debug.assert_called_once()


# ── _get_default_material_temps ───────────────────────────────────────────────

def _make_lane_for_material_temps(
    extruder_temp=None,
    material=None,
):
    """Build a minimal lane mock for _get_default_material_temps tests."""
    lane = MagicMock()
    lane.extruder_temp = extruder_temp
    lane.material = material
    return lane


def _make_afc_for_material_temps(
    default_material_temps=None,
    min_extrude_temp=170.0,
):
    """Build an afc instance wired up for _get_default_material_temps tests."""
    obj = _make_afc()

    if default_material_temps is None:
        default_material_temps = ["default: 235", "PLA:210", "PETG:235", "ABS:240", "ASA:245"]
    obj.default_material_temps = default_material_temps

    heater = MagicMock()
    heater.min_extrude_temp = min_extrude_temp
    obj.heater = heater

    return obj


class TestGetDefaultMaterialTemps:
    """Tests for afc._get_default_material_temps."""

    # ── return type ──────────────────────────────────────────────────────────

    def test_returns_tuple(self):
        """Return value is a 2-tuple."""
        obj = _make_afc_for_material_temps()
        lane = _make_lane_for_material_temps()
        result = obj._get_default_material_temps(lane)
        assert isinstance(result, tuple) and len(result) == 2

    def test_first_element_is_float(self):
        """First tuple element (temperature) is always a float."""
        obj = _make_afc_for_material_temps()
        lane = _make_lane_for_material_temps()
        temp, _ = obj._get_default_material_temps(lane)
        assert isinstance(temp, float)

    def test_second_element_is_bool(self):
        """Second tuple element (using_min_value flag) is always a bool."""
        obj = _make_afc_for_material_temps()
        lane = _make_lane_for_material_temps()
        _, using_min = obj._get_default_material_temps(lane)
        assert isinstance(using_min, bool)

    # ── default entry in list ────────────────────────────────────────────────

    def test_no_lane_data_returns_default_list_temp(self):
        """With no extruder_temp and no material, falls back to the 'default:' entry."""
        obj = _make_afc_for_material_temps(default_material_temps=["default: 235", "PLA:210"])
        lane = _make_lane_for_material_temps(extruder_temp=None, material=None)
        temp, using_min = obj._get_default_material_temps(lane)
        assert temp == 235.0

    def test_no_lane_data_sets_using_min_value_true(self):
        """using_min_value is True when falling back to the 'default:' list entry."""
        obj = _make_afc_for_material_temps(default_material_temps=["default: 235", "PLA:210"])
        lane = _make_lane_for_material_temps(extruder_temp=None, material=None)
        _, using_min = obj._get_default_material_temps(lane)
        assert using_min is True

    def test_default_entry_with_spaces_is_parsed_correctly(self):
        """Spaces around the colon in 'default: 200' are handled correctly."""
        obj = _make_afc_for_material_temps(default_material_temps=["default: 200"])
        lane = _make_lane_for_material_temps()
        temp, _ = obj._get_default_material_temps(lane)
        assert temp == 200.0

    # ── fallback to min_extrude_temp + 5 ────────────────────────────────────

    def test_missing_default_entry_falls_back_to_min_extrude_temp(self):
        """When no 'default:' entry exists in the list, heater.min_extrude_temp + 5 is used."""
        obj = _make_afc_for_material_temps(
            default_material_temps=["PLA:210", "PETG:235"],
            min_extrude_temp=170.0,
        )
        lane = _make_lane_for_material_temps(extruder_temp=None, material=None)
        temp, _ = obj._get_default_material_temps(lane)
        assert temp == 175.0  # 170 + 5

    def test_missing_default_entry_using_min_value_true(self):
        """using_min_value is True when the min_extrude_temp fallback is used."""
        obj = _make_afc_for_material_temps(
            default_material_temps=["PLA:210"],
            min_extrude_temp=170.0,
        )
        lane = _make_lane_for_material_temps(extruder_temp=None, material=None)
        _, using_min = obj._get_default_material_temps(lane)
        assert using_min is True

    def test_none_default_material_temps_falls_back_to_min_extrude_temp(self):
        """When default_material_temps is None the heater fallback is used."""
        obj = _make_afc_for_material_temps(min_extrude_temp=165.0)
        obj.default_material_temps = None  # Force None so except branch fires
        lane = _make_lane_for_material_temps(extruder_temp=None, material=None)
        temp, using_min = obj._get_default_material_temps(lane)
        assert temp == 170.0  # 165 + 5
        assert using_min is True

    def test_empty_default_material_temps_list_falls_back_to_min_extrude_temp(self):
        """An empty list also triggers the min_extrude_temp + 5 fallback."""
        obj = _make_afc_for_material_temps(default_material_temps=[], min_extrude_temp=180.0)
        lane = _make_lane_for_material_temps(extruder_temp=None, material=None)
        temp, _ = obj._get_default_material_temps(lane)
        assert temp == 185.0  # 180 + 5

    # ── extruder_temp set (non-None, non-zero) ───────────────────────────────

    def test_extruder_temp_overrides_default(self):
        """A valid extruder_temp above heater.min_extrude_temp is used directly and overrides everything else"""
        obj = _make_afc_for_material_temps(default_material_temps=["default: 235", "PLA:210"])
        lane = _make_lane_for_material_temps(extruder_temp=220.0, material=None)
        temp, _ = obj._get_default_material_temps(lane)
        assert temp == 220.0

    def test_extruder_temp_sets_using_min_value_false(self):
        """using_min_value is False when extruder_temp is provided."""
        obj = _make_afc_for_material_temps()
        lane = _make_lane_for_material_temps(extruder_temp=220.0, material=None)
        _, using_min = obj._get_default_material_temps(lane)
        assert using_min is False

    def test_extruder_temp_overrides_material_match(self):
        """extruder_temp takes priority over a matching material entry."""
        obj = _make_afc_for_material_temps(default_material_temps=["default: 235", "PLA:210"])
        lane = _make_lane_for_material_temps(extruder_temp=250.0, material="PLA")
        temp, _ = obj._get_default_material_temps(lane)
        assert temp == 250.0

    # ── extruder_temp set to zero (critical edge case) ───────────────────────

    def test_extruder_temp_zero_returns_default(self):
        """Return default temp since extruder_temp=0 is not a valid temperature."""
        obj = _make_afc_for_material_temps(default_material_temps=["default: 235", "PLA:210"])
        lane = _make_lane_for_material_temps(extruder_temp=0, material=None)
        temp, _ = obj._get_default_material_temps(lane)
        assert temp == 235.0

    def test_extruder_temp_zero_sets_using_min_value_true(self):
        """using_min_value is True when extruder_temp is exactly zero."""
        obj = _make_afc_for_material_temps()
        lane = _make_lane_for_material_temps(extruder_temp=0, material=None)
        _, using_min = obj._get_default_material_temps(lane)
        assert using_min is True

    # ── material matching ────────────────────────────────────────────────────

    def test_exact_material_match_returns_material_temp(self):
        """Exact material name match returns the configured material temperature."""
        obj = _make_afc_for_material_temps(default_material_temps=["default: 235", "PLA:210"])
        lane = _make_lane_for_material_temps(extruder_temp=None, material="PLA")
        temp, _ = obj._get_default_material_temps(lane)
        assert temp == 210.0

    def test_material_match_sets_using_min_value_false(self):
        """using_min_value is False when a matching material entry is found."""
        obj = _make_afc_for_material_temps(default_material_temps=["default: 235", "PLA:210"])
        lane = _make_lane_for_material_temps(extruder_temp=None, material="PLA")
        _, using_min = obj._get_default_material_temps(lane)
        assert using_min is False

    def test_material_match_is_case_insensitive(self):
        """Material matching ignores case ('pla' matches 'PLA:210')."""
        obj = _make_afc_for_material_temps(default_material_temps=["default: 235", "PLA:210"])
        lane = _make_lane_for_material_temps(extruder_temp=None, material="pla")
        temp, using_min = obj._get_default_material_temps(lane)
        assert temp == 210.0
        assert using_min is False

    def test_material_substring_match(self):
        """A material key is matched as a substring (e.g. 'PLA' key matches 'PLA+' material)."""
        obj = _make_afc_for_material_temps(default_material_temps=["default: 235", "PLA:215"])
        lane = _make_lane_for_material_temps(extruder_temp=None, material="PLA+")
        temp, using_min = obj._get_default_material_temps(lane)
        assert temp == 215.0
        assert using_min is False

    def test_unmatched_material_falls_back_to_default_entry(self):
        """An unrecognised material name falls back to the 'default:' list entry."""
        obj = _make_afc_for_material_temps(default_material_temps=["default: 235", "PLA:210"])
        lane = _make_lane_for_material_temps(extruder_temp=None, material="TPU")
        temp, using_min = obj._get_default_material_temps(lane)
        assert temp == 235.0
        assert using_min is True

    def test_material_none_skips_material_lookup(self):
        """When material is None the material-matching loop is skipped."""
        obj = _make_afc_for_material_temps(default_material_temps=["default: 235", "PLA:210"])
        lane = _make_lane_for_material_temps(extruder_temp=None, material=None)
        temp, using_min = obj._get_default_material_temps(lane)
        assert temp == 235.0
        assert using_min is True

    def test_first_matching_material_wins(self):
        """When multiple entries could match the first one in the list wins."""
        obj = _make_afc_for_material_temps(
            default_material_temps=["default: 235", "PLA:210", "PLA:999"]
        )
        lane = _make_lane_for_material_temps(extruder_temp=None, material="PLA")
        temp, _ = obj._get_default_material_temps(lane)
        assert temp == 210.0

    def test_multiple_materials_each_return_correct_temp(self):
        """Each material in the list returns its own configured temperature."""
        temps_cfg = ["default: 235", "PLA:210", "PETG:230", "ABS:240"]
        obj = _make_afc_for_material_temps(default_material_temps=temps_cfg)
        for material, expected in [("PLA", 210.0), ("PETG", 230.0), ("ABS", 240.0)]:
            lane = _make_lane_for_material_temps(extruder_temp=None, material=material)
            temp, using_min = obj._get_default_material_temps(lane)
            assert temp == expected, f"Expected {expected} for {material}, got {temp}"
            assert using_min is False


# in_print_reactor_timer: moonraker None guard

class TestInPrintReactorTimer:
    """
    Tests for in_print_reactor_timer guarding against an uninitialized
    moonraker object. If PREP has not been run, self.moonraker is None and
    calling methods on it would raise AttributeError and crash klipper.
    """

    def _make(self):
        obj = _make_afc()
        obj.in_print_timer = MagicMock()
        return obj

    def test_skips_moonraker_call_when_moonraker_is_none(self):
        """When in_print is True but moonraker is None, do not deref moonraker."""
        obj = self._make()
        obj.moonraker = None
        obj.function.in_print.return_value = (True, "test.gcode")
        # Must not raise AttributeError on NoneType.
        result = obj.in_print_reactor_timer(0.0)
        assert result == obj.reactor.NEVER

    def test_calls_moonraker_when_in_print_and_moonraker_set(self):
        """Happy path: moonraker is queried when both in_print and moonraker are set."""
        obj = self._make()
        obj.moonraker = MagicMock()
        obj.moonraker.get_file_filament_change_count.return_value = 7
        obj.function.in_print.return_value = (True, "test.gcode")
        obj.function.get_current_lane_obj.return_value = None
        obj.in_print_reactor_timer(0.0)
        obj.moonraker.get_file_filament_change_count.assert_called_once_with("test.gcode")
        assert obj.number_of_toolchanges == 7
        assert obj.current_toolchange == -1

    def test_does_not_call_moonraker_when_not_in_print(self):
        """When not in a print, moonraker should not be queried."""
        obj = self._make()
        obj.moonraker = MagicMock()
        obj.function.in_print.return_value = (False, None)
        obj.in_print_reactor_timer(0.0)
        obj.moonraker.get_file_filament_change_count.assert_not_called()


# cmd_LANE_MOVE

def _make_afc_for_lane_move(is_printing=False):
    """Build an afc instance wired up for cmd_LANE_MOVE tests."""

    obj = _make_afc()
    obj.function.is_printing.return_value = is_printing
    obj.error = MagicMock()

    lane = MagicMock()
    lane.unit_obj = MagicMock()
    obj.lanes["lane1"] = lane

    return obj, lane


def _make_gcmd(lane="lane1", distance=10.0, force=0):
    gcmd = MagicMock()
    gcmd.get.side_effect = lambda key, default=None: {"LANE": lane}.get(key, default)
    gcmd.get_float.side_effect = lambda key, default=0: {"DISTANCE": distance}.get(key, default)
    gcmd.get_int.side_effect = lambda key, default=0: {"FORCE": force}.get(key, default)
    return gcmd


class TestCmdLaneMove:
    # ── printing guard ────────────────────────────────────────────────────────

    def test_blocks_when_printing_and_no_force(self):
        """Move is rejected while printing when FORCE is not 1."""
        obj, lane = _make_afc_for_lane_move(is_printing=True)
        gcmd = _make_gcmd(force=0)
        obj.cmd_LANE_MOVE(gcmd)
        obj.error.AFC_error.assert_called_once()
        lane.move_advanced.assert_not_called()

    def test_allows_when_printing_and_force_is_1(self):
        """FORCE=1 bypasses the is_printing guard."""
        obj, lane = _make_afc_for_lane_move(is_printing=True)
        gcmd = _make_gcmd(force=1)
        obj.cmd_LANE_MOVE(gcmd)
        obj.error.AFC_error.assert_not_called()
        lane.move_advanced.assert_called_once()

    def test_force_2_does_not_bypass_guard(self):
        """FORCE=2 is not equal to 1, so the guard still fires."""
        obj, lane = _make_afc_for_lane_move(is_printing=True)
        gcmd = _make_gcmd(force=2)
        obj.cmd_LANE_MOVE(gcmd)
        obj.error.AFC_error.assert_called_once()
        lane.move_advanced.assert_not_called()

    def test_force_0_does_not_bypass_guard(self):
        """FORCE=0 (default) does not bypass the guard."""
        obj, lane = _make_afc_for_lane_move(is_printing=True)
        gcmd = _make_gcmd(force=0)
        obj.cmd_LANE_MOVE(gcmd)
        obj.error.AFC_error.assert_called_once()
        lane.move_advanced.assert_not_called()

    def test_no_guard_when_not_printing(self):
        """Guard is not triggered when the printer is idle, regardless of FORCE."""
        obj, lane = _make_afc_for_lane_move(is_printing=False)
        gcmd = _make_gcmd(force=0)
        obj.cmd_LANE_MOVE(gcmd)
        obj.error.AFC_error.assert_not_called()
        lane.move_advanced.assert_called_once()

    # ── zero distance guard ───────────────────────────────────────────────────

    def test_blocks_zero_distance(self):
        """A distance of zero is always rejected."""
        obj, lane = _make_afc_for_lane_move(is_printing=False)
        gcmd = _make_gcmd(distance=0.0)
        obj.cmd_LANE_MOVE(gcmd)
        obj.error.AFC_error.assert_called_once()
        lane.move_advanced.assert_not_called()

    # ── unknown lane guard ────────────────────────────────────────────────────

    def test_blocks_unknown_lane(self):
        """An unknown lane name is logged and the move is skipped."""
        obj, lane = _make_afc_for_lane_move(is_printing=False)
        gcmd = _make_gcmd(lane="unknown_lane")
        obj.cmd_LANE_MOVE(gcmd)
        lane.move_advanced.assert_not_called()

    # ── normal move ───────────────────────────────────────────────────────────

    def test_move_advanced_called_with_distance(self):
        """move_advanced is called with the requested distance."""
        from extras.AFC_lane import SpeedMode, AssistActive
        obj, lane = _make_afc_for_lane_move(is_printing=False)
        gcmd = _make_gcmd(distance=50.0)
        obj.cmd_LANE_MOVE(gcmd)
        args = lane.move_advanced.call_args.args
        assert args[0] == 50.0

    def test_short_speed_mode_for_small_distance(self):
        """Distances under 200 use SHORT speed mode."""
        from extras.AFC_lane import SpeedMode, AssistActive
        obj, lane = _make_afc_for_lane_move(is_printing=False)
        gcmd = _make_gcmd(distance=100.0)
        obj.cmd_LANE_MOVE(gcmd)
        args = lane.move_advanced.call_args.args
        assert args[1] == SpeedMode.SHORT

    def test_long_speed_mode_for_large_distance(self):
        """Distances of 200 or more use LONG speed mode."""
        from extras.AFC_lane import SpeedMode, AssistActive
        obj, lane = _make_afc_for_lane_move(is_printing=False)
        gcmd = _make_gcmd(distance=200.0)
        obj.cmd_LANE_MOVE(gcmd)
        args = lane.move_advanced.call_args.args
        assert args[1] == SpeedMode.LONG

class TestCheckForSnapmakerSignature:
    def test_check_snapmaker_printer_property_false(self):
        obj = _make_afc()
        obj.printer = Printer
        assert not obj.snapmaker_printer
    
    def test_check_snapmaker_printer_property_true(self, monkeypatch):
        obj = _make_afc()
        obj.printer = Printer
        monkeypatch.setattr(Printer, "get_snapmaker_config_dir", True, raising=False)
        assert obj.snapmaker_printer

class TestUnitOrdering:
    def _make_unit(self, name, afc):
        unit = MagicMock()
        unit.name = name
        unit.lanes = {}


        afc.units.update({name: unit})
        return unit
        
    def add_lane_to_unit(self, unit, lane_name, extruder_name="extruder"):
        lane = MagicMock()
        lane.name = lane_name
        lane.extruder_obj.th_extruder_name = lane.extruder_obj.name = extruder_name
        unit.lanes.update({lane_name: lane})
        
    def test_unit_lane_ordering(self):
        obj = _make_afc()
        claymore_unit = self._make_unit("HTLF_Claymore_1", obj)
        self.add_lane_to_unit(claymore_unit, "lane11", "extruder")
        self.add_lane_to_unit(claymore_unit, "lane12", "extruder")
        self.add_lane_to_unit(claymore_unit, "lane13", "extruder")
        self.add_lane_to_unit(claymore_unit, "lane14", "extruder")

        emu_unit = self._make_unit("EMU_1", obj)
        self.add_lane_to_unit(emu_unit, "lane9", "extruder3")
        self.add_lane_to_unit(emu_unit, "lane10", "extruder3")
        
        tools_unit = self._make_unit("Tools", obj)
        self.add_lane_to_unit(tools_unit, "extruder1", "extruder1")
        self.add_lane_to_unit(tools_unit, "extruder2", "extruder2")

        bt_unit = self._make_unit("Turtle_1", obj)
        self.add_lane_to_unit(bt_unit, "lane4", "extruder")
        self.add_lane_to_unit(bt_unit, "lane2", "extruder")
        self.add_lane_to_unit(bt_unit, "lane1", "extruder")
        self.add_lane_to_unit(bt_unit, "lane3", "extruder")

        vivid_unit = self._make_unit("Vivid_1", obj)
        self.add_lane_to_unit(vivid_unit, "lane8", "extruder3")
        self.add_lane_to_unit(vivid_unit, "lane5", "extruder3")
        self.add_lane_to_unit(vivid_unit, "lane7", "extruder3")
        self.add_lane_to_unit(vivid_unit, "lane6", "extruder3")


        obj.handle_ready()

        key_order = list(obj.units.keys())

        assert key_order == ["Turtle_1", "Vivid_1", "EMU_1", "HTLF_Claymore_1", "Tools"], key_order

    def test_inf_unit_lane_ordering(self):
        obj = _make_afc()
        claymore_unit = self._make_unit("HTLF_Claymore_1", obj)
        self.add_lane_to_unit(claymore_unit, "lane11", "extruder")
        self.add_lane_to_unit(claymore_unit, "lane12", "extruder")
        self.add_lane_to_unit(claymore_unit, "lane13", "extruder")
        self.add_lane_to_unit(claymore_unit, "lane14", "extruder")

        emu_unit = self._make_unit("EMU_1", obj)
        
        tools_unit = self._make_unit("Tools", obj)
        self.add_lane_to_unit(tools_unit, "extruder1", "extruder1")
        self.add_lane_to_unit(tools_unit, "extruder2", "extruder2")

        bt_unit = self._make_unit("Turtle_1", obj)
        self.add_lane_to_unit(bt_unit, "lane4", "extruder")
        self.add_lane_to_unit(bt_unit, "lane2", "extruder")
        self.add_lane_to_unit(bt_unit, "lane1", "extruder")
        self.add_lane_to_unit(bt_unit, "lane3", "extruder")

        vivid_unit = self._make_unit("Vivid_1", obj)
        self.add_lane_to_unit(vivid_unit, "lane8", "extruder3")
        self.add_lane_to_unit(vivid_unit, "lane5", "extruder3")
        self.add_lane_to_unit(vivid_unit, "lane7", "extruder3")
        self.add_lane_to_unit(vivid_unit, "lane6", "extruder3")

        obj.handle_ready()

        key_order = list(obj.units.keys())

        assert key_order == ["Turtle_1", "Vivid_1", "HTLF_Claymore_1", "Tools", "EMU_1"], key_order
        