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
  - afc.cmd_CHANGE_TOOL: NEW_EXTRUDER_TEMP parameter parsing
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from extras.AFC import afc, State, AFC_VERSION
from extras.AFC_lane import AFCLaneState


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
    obj._check_bypass = MagicMock(return_value=False)
    obj._heat_next_extruder = MagicMock()
    obj._cooldown_last_extruder = MagicMock()
    obj._wait_for_temp_within_tolerance = MagicMock()
    obj.error = MagicMock()

    # Current (old) lane/extruder
    current_extruder = MagicMock()
    current_extruder.name = current_extruder_name
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
    next_extruder.name = next_extruder_name
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
        obj._cooldown_last_extruder.side_effect = lambda *a, **kw: call_order.append("cool")
        obj.CHANGE_TOOL(cur_lane, new_extruder_temp=200.0)
        assert call_order == ["heat", "cool"], f"Wrong call order: {call_order}"

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



