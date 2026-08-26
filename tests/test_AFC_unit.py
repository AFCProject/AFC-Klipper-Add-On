"""
Unit tests for extras/AFC_unit.py

Covers:
  - afcUnit.__str__: returns name
  - afcUnit._check_and_errorout: None vs non-None object
  - afcUnit.get_status: correct keys and content
  - afcUnit.check_runout: returns False
  - afcUnit.return_to_home: returns None
  - afcUnit.lane_loaded/unloaded/loading/tool_loaded/tool_unloaded: call afc_led
  - afcUnit.set_logo_color: calls afc_led when color present, skips when None/empty
"""

from __future__ import annotations

from unittest.mock import MagicMock, call
import pytest

from extras.AFC_unit import afcUnit


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_unit(name="Turtle_1"):
    """Build an afcUnit bypassing the complex __init__."""
    unit = afcUnit.__new__(afcUnit)

    from tests.conftest import MockAFC, MockPrinter, MockLogger
    from extras.AFC_error import afcError
    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    afc.logger = MockLogger()
    afc.error = afcError.__new__(afcError)
    afc.error.logger = afc.logger
    unit.printer = printer
    unit.afc = afc
    unit.logger = afc.logger
    unit.name = name
    unit.full_name = ["AFC_BoxTurtle", name]
    unit.lanes = {}
    unit.hub_obj = None
    unit.extruder_obj = None
    unit.buffer_obj = None
    unit.hub = None
    unit.extruder = None
    unit.buffer_name = None
    unit.td1_defined = False
    unit.type = "Box_Turtle"
    unit.gcode = afc.gcode
    unit.led_logo_index = None
    unit.enable_buffer_tool_check = True

    return unit


def _make_lane(name="lane1", hub="hub1", extruder="ext1", buffer_name="buf1"):
    from extras.AFC_lane import AFCLane
    lane = AFCLane.__new__(AFCLane)
    lane.unit_obj = MagicMock()
    lane.name = name
    lane.hub = hub
    lane.afc_extruder_name = extruder
    lane.buffer_name = buffer_name
    lane.led_ready = "0,1,0,0"
    lane.led_not_ready = "0,0,0,0.25"
    lane.led_loading = "0,0,1,0"
    lane.led_unloading = "0,0,1,0.5"
    lane.led_tool_loaded = "0,0,1,1"
    lane.led_tool_unloaded = "1,0,1,0"
    lane.led_tool_loaded_idle = "0,0.5,0,0"
    lane.led_fault = "1,0,0,0"
    lane.led_index = "1"
    lane.led_spool_illum = "1,1,1,0"
    lane.led_use_filament_color = False
    lane._load_state = True
    lane.short_moves_speed = 50
    lane.short_moves_accel = 50
    lane.led_spool_index = None
    lane.current_led_state = ""
    return lane


# ── __str__ ───────────────────────────────────────────────────────────────────

class TestStr:
    def test_str_returns_name(self):
        unit = _make_unit("Turtle_1")
        assert str(unit) == "Turtle_1"

    def test_str_reflects_different_name(self):
        unit = _make_unit("NightOwl_2")
        assert str(unit) == "NightOwl_2"


# ── _check_and_errorout ────────────────────────────────────────────────────────

class TestCheckAndErrorOut:
    def test_returns_true_when_obj_is_none(self):
        unit = _make_unit()
        error, msg = unit._check_and_errorout(None, "AFC_hub testname", "hub")
        assert error is True

    def test_error_msg_not_empty_when_obj_none(self):
        unit = _make_unit()
        _, msg = unit._check_and_errorout(None, "AFC_hub testname", "hub")
        assert len(msg) > 0

    def test_error_msg_contains_config_name(self):
        unit = _make_unit()
        _, msg = unit._check_and_errorout(None, "AFC_hub testname", "hub")
        assert "AFC_hub testname" in msg

    def test_returns_false_when_obj_present(self):
        unit = _make_unit()
        obj = MagicMock()
        error, msg = unit._check_and_errorout(obj, "AFC_hub", "hub")
        assert error is False

    def test_msg_empty_when_obj_present(self):
        unit = _make_unit()
        obj = MagicMock()
        _, msg = unit._check_and_errorout(obj, "AFC_hub", "hub")
        assert msg == ""


# ── get_status ────────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_has_lanes_key(self):
        unit = _make_unit()
        status = unit.get_status()
        assert "lanes" in status

    def test_has_extruders_key(self):
        unit = _make_unit()
        status = unit.get_status()
        assert "extruders" in status

    def test_has_hubs_key(self):
        unit = _make_unit()
        status = unit.get_status()
        assert "hubs" in status

    def test_has_buffers_key(self):
        unit = _make_unit()
        status = unit.get_status()
        assert "buffers" in status

    def test_lanes_list_contains_lane_name(self):
        unit = _make_unit()
        lane = _make_lane("lane1")
        unit.lanes = {"lane1": lane}
        status = unit.get_status()
        assert "lane1" in status["lanes"]

    def test_extruder_name_collected_from_lanes(self):
        unit = _make_unit()
        lane = _make_lane("lane1", extruder="my_extruder")
        unit.lanes = {"lane1": lane}
        status = unit.get_status()
        assert "my_extruder" in status["extruders"]

    def test_hub_name_collected_from_lanes(self):
        unit = _make_unit()
        lane = _make_lane("lane1", hub="my_hub")
        unit.lanes = {"lane1": lane}
        status = unit.get_status()
        assert "my_hub" in status["hubs"]

    def test_hub_name_collected_from_lanes_direct_hub(self):
        unit = _make_unit()
        lane = _make_lane("lane1", hub="direct")
        unit.lanes = {"lane1": lane}
        status = unit.get_status()
        assert "direct" not in status["hubs"]

    def test_hub_name_collected_from_lanes_direct_load_hub(self):
        unit = _make_unit()
        lane = _make_lane("lane1", hub="direct_load")
        unit.lanes = {"lane1": lane}
        status = unit.get_status()
        assert "direct_load" not in status["hubs"]

    def test_buffer_name_collected_from_lanes(self):
        unit = _make_unit()
        lane = _make_lane("lane1", buffer_name="my_buffer")
        unit.lanes = {"lane1": lane}
        status = unit.get_status()
        assert "my_buffer" in status["buffers"]

    def test_duplicate_extruder_not_repeated(self):
        """Two lanes sharing the same extruder should appear only once."""
        unit = _make_unit()
        lane1 = _make_lane("lane1", extruder="shared_ext")
        lane2 = _make_lane("lane2", extruder="shared_ext")
        unit.lanes = {"lane1": lane1, "lane2": lane2}
        status = unit.get_status()
        assert status["extruders"].count("shared_ext") == 1

    def test_empty_lanes_returns_empty_lists(self):
        unit = _make_unit()
        unit.lanes = {}
        status = unit.get_status()
        assert status["lanes"] == []
        assert status["extruders"] == []
        assert status["hubs"] == []
        assert status["buffers"] == []


# ── check_runout ──────────────────────────────────────────────────────────────

class TestCheckRunout:
    def test_returns_false(self):
        unit = _make_unit()
        assert unit.check_runout("lane") is False


# ── _selector_cal_dis_adjust ─────────────────────────────────────────────────

class TestSelectorCalDisAdjust:
    def test_moves_selector_when_configured(self):
        unit = _make_unit()
        unit.selector_stepper_obj = MagicMock()
        lane = _make_lane("lane1")
        lane.selector_cal_dis = 2.5
        unit._selector_cal_dis_adjust(lane)
        unit.selector_stepper_obj.move.assert_called_once_with(2.5, 50, 50, False)

    def test_skipped_when_no_selector_stepper_obj(self):
        """Proven independently of selector_cal_dis's own value: a valid,
        non-zero selector_cal_dis alone isn't enough without a selector
        stepper object."""
        unit = _make_unit()
        unit.selector_stepper_obj = None
        lane = _make_lane("lane1")
        lane.selector_cal_dis = 2.5
        unit._selector_cal_dis_adjust(lane)
        # No selector_stepper_obj to assert on; move never gets a chance to
        # be called since the object itself is None here.

    def test_skipped_when_selector_cal_dis_is_none(self):
        unit = _make_unit()
        unit.selector_stepper_obj = MagicMock()
        lane = _make_lane("lane1")
        lane.selector_cal_dis = None
        unit._selector_cal_dis_adjust(lane)
        unit.selector_stepper_obj.move.assert_not_called()

    def test_skipped_when_selector_cal_dis_is_zero(self):
        unit = _make_unit()
        unit.selector_stepper_obj = MagicMock()
        lane = _make_lane("lane1")
        lane.selector_cal_dis = 0.0
        unit._selector_cal_dis_adjust(lane)
        unit.selector_stepper_obj.move.assert_not_called()


# ── return_to_home ────────────────────────────────────────────────────────────

class TestReturnToHome:
    def test_returns_none(self):
        unit = _make_unit()
        result = unit.return_to_home()
        assert result is None


# ── on_filament_insert / on_filament_remove ──────────────────────────────────

class TestOnFilamentInsert:
    def test_sends_afc_lane_inserted_event_with_lane(self):
        unit = _make_unit()
        unit.printer.send_event = MagicMock()
        lane = MagicMock()

        unit.on_filament_insert(lane)

        unit.printer.send_event.assert_called_once_with("afc:lane_inserted", lane)


class TestOnFilamentRemove:
    def test_returns_none_and_has_no_side_effects(self):
        unit = _make_unit()
        unit.printer.send_event = MagicMock()
        lane = MagicMock()

        result = unit.on_filament_remove(lane)

        assert result is None
        unit.printer.send_event.assert_not_called()


# ── LED helpers ───────────────────────────────────────────────────────────────

class TestLaneStatusLeds:
    def test_lane_not_ready_calls_afc_led_with_not_ready_color(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.lane_not_ready(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_not_ready, lane.led_index)

    def test_lane_not_ready_turns_off_spool_led_when_spool_index_set(self):
        """Covers the `lane.led_spool_index is not None` True branch."""
        unit = _make_unit()
        lane = _make_lane()
        lane.led_spool_index = "2"
        unit.lane_not_ready(lane)
        unit.afc.function.afc_led.assert_any_call(unit.afc.led_off, "2")

    def test_lane_not_ready_skips_spool_led_when_spool_index_none(self):
        """Covers the `lane.led_spool_index is not None` False branch."""
        unit = _make_unit()
        lane = _make_lane()
        lane.led_spool_index = None
        unit.lane_not_ready(lane)
        for c in unit.afc.function.afc_led.call_args_list:
            assert c[0][0] != unit.afc.led_off

    def test_lane_loaded_calls_afc_led_with_ready_color(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.lane_loaded(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_ready, lane.led_index)

    def test_lane_loaded_illuminates_spool_led_when_spool_index_set(self):
        """Covers the `lane.led_spool_index is not None` True branch."""
        unit = _make_unit()
        lane = _make_lane()
        lane.led_spool_index = "2"
        unit.lane_loaded(lane)
        unit.afc.function.afc_led.assert_any_call(lane.led_spool_illum, "2")

    def test_lane_unloading_calls_afc_led_with_unloading_color(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.lane_unloading(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_unloading, lane.led_index)

    def test_lane_unloaded_calls_afc_led_with_not_ready_color(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.lane_unloaded(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_not_ready, lane.led_index)

    def test_lane_unloaded_does_not_touch_spool_led(self):
        """lane_unloaded no longer manages the spool illumination LED --
        that responsibility moved to lane_not_ready."""
        unit = _make_unit()
        lane = _make_lane()
        lane.led_spool_index = "2"
        unit.lane_unloaded(lane)
        for c in unit.afc.function.afc_led.call_args_list:
            assert c[0][0] != unit.afc.led_off

    def test_lane_loading_calls_afc_led_with_loading_color(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.lane_loading(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_loading, lane.led_index)

    def test_lane_tool_loaded_gears_does_not_set_a_static_color(self):
        """Unlike its siblings, lane_tool_loaded_gears passes no static_color
        to _trigger_led_state -- it only overlays the effect."""
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit.lane_tool_loaded_gears(lane)
        unit.afc.function.afc_led.assert_not_called()
        lane.extruder_obj.set_status_led.assert_not_called()

    def test_lane_tool_loaded_calls_afc_led_with_tool_loaded_color(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit.lane_tool_loaded(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_tool_loaded, lane.led_index)
        lane.extruder_obj.set_status_led.assert_called_once_with(lane.led_tool_loaded)

    def test_lane_tool_unloaded_calls_afc_led_with_ready_color(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit.lane_tool_unloaded(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_ready, lane.led_index)
        lane.extruder_obj.set_status_led.assert_called_once_with(lane.led_tool_unloaded)

    def test_lane_tool_loaded_idle_calls_afc_led_with_idle_color(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit.lane_tool_loaded_idle(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_tool_loaded_idle, lane.led_index)
        lane.extruder_obj.set_status_led.assert_called_once_with(lane.led_tool_loaded_idle)

    def test_lane_fault_calls_afc_led_with_fault_color(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.lane_fault(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_fault, lane.led_index)


# ── _check_led_state ─────────────────────────────────────────────────────────

class TestCheckLedState:
    def test_new_state_returns_true(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.current_led_state = "loaded"
        assert unit._check_led_state(lane, "not_ready") is True

    def test_new_state_updates_current_led_state(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.current_led_state = "loaded"
        unit._check_led_state(lane, "not_ready")
        assert lane.current_led_state == "not_ready"

    def test_matching_state_returns_false(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.current_led_state = "loaded"
        assert unit._check_led_state(lane, "loaded") is False

    def test_matching_state_leaves_current_led_state_unchanged(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.current_led_state = "loaded"
        unit._check_led_state(lane, "loaded")
        assert lane.current_led_state == "loaded"

    def test_empty_initial_state_is_not_a_match(self):
        """The AFCLane default ("") must not collide with a real state name,
        so the very first lane_* call for a lane isn't accidentally deduped."""
        unit = _make_unit()
        lane = _make_lane()
        lane.current_led_state = ""
        assert unit._check_led_state(lane, "not_ready") is True


# ── lane_* LED-state dedup guards ──────────────────────────────────────────────

class TestLedStateDedup:
    """Each lane_* method must skip re-triggering the LED when called again
    with the lane already in that state, and must proceed when the state
    actually changes -- verified via _trigger_led_state's call count, which
    would differ between the two branches, rather than just "no error"."""

    def test_lane_not_ready_skips_when_already_not_ready(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_not_ready(lane)
        unit.lane_not_ready(lane)
        assert unit._trigger_led_state.call_count == 1

    def test_lane_not_ready_runs_again_after_state_changes(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_loaded(lane)
        unit.lane_not_ready(lane)
        assert unit._trigger_led_state.call_count == 2

    def test_lane_loaded_skips_when_already_loaded(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_loaded(lane)
        unit.lane_loaded(lane)
        assert unit._trigger_led_state.call_count == 1

    def test_lane_loaded_force_true_reapplies_even_when_already_loaded(self):
        """force=True bypasses the dedup skip, used when only the filament
        color changed while the lane stayed in the "loaded" state."""
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_loaded(lane)
        unit.lane_loaded(lane, force=True)
        assert unit._trigger_led_state.call_count == 2

    def test_lane_loaded_force_false_default_still_skips_when_already_loaded(self):
        """Covers force's default value (False) taking the normal dedup path."""
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_loaded(lane)
        unit.lane_loaded(lane, force=False)
        assert unit._trigger_led_state.call_count == 1

    def test_lane_unloading_skips_when_already_unloading(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_unloading(lane)
        unit.lane_unloading(lane)
        assert unit._trigger_led_state.call_count == 1

    def test_lane_unloaded_skips_when_already_unloaded(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_unloaded(lane)
        unit.lane_unloaded(lane)
        assert unit._trigger_led_state.call_count == 1

    def test_lane_loading_skips_when_already_loading(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_loading(lane)
        unit.lane_loading(lane)
        assert unit._trigger_led_state.call_count == 1

    def test_lane_tool_loaded_gears_skips_when_already_tool_loaded_gears(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit._trigger_led_state = MagicMock()
        unit.lane_tool_loaded_gears(lane)
        unit.lane_tool_loaded_gears(lane)
        assert unit._trigger_led_state.call_count == 1

    def test_lane_tool_loaded_skips_when_already_tool_loaded(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit._trigger_led_state = MagicMock()
        unit.lane_tool_loaded(lane)
        unit.lane_tool_loaded(lane)
        assert unit._trigger_led_state.call_count == 1

    def test_lane_tool_unloaded_skips_when_already_tool_unloaded(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit._trigger_led_state = MagicMock()
        unit.lane_tool_unloaded(lane)
        unit.lane_tool_unloaded(lane)
        assert unit._trigger_led_state.call_count == 1

    def test_lane_tool_loaded_idle_skips_when_already_tool_loaded_idle(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit._trigger_led_state = MagicMock()
        unit.lane_tool_loaded_idle(lane)
        unit.lane_tool_loaded_idle(lane)
        assert unit._trigger_led_state.call_count == 1

    def test_lane_fault_skips_when_already_fault(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_fault(lane)
        unit.lane_fault(lane)
        assert unit._trigger_led_state.call_count == 1

    def test_different_lanes_are_deduped_independently(self):
        """current_led_state lives on the lane, not the unit, so one lane's
        state must not suppress another lane's first LED update."""
        unit = _make_unit()
        lane1 = _make_lane("lane1")
        lane2 = _make_lane("lane2")
        unit._trigger_led_state = MagicMock()
        unit.lane_loaded(lane1)
        unit.lane_loaded(lane2)
        assert unit._trigger_led_state.call_count == 2


# ── _stop_led_effects ────────────────────────────────────────────────────────

class TestStopLedEffects:
    def test_no_active_effects_does_not_call_gcode(self):
        unit = _make_unit()
        unit.afc.active_led_effects = []
        unit._stop_led_effects()
        unit.gcode.run_script_from_command.assert_not_called()

    def test_stops_each_active_effect(self):
        unit = _make_unit()
        unit.afc.active_led_effects = ["lane1_loaded", "lane2_fault"]
        unit._stop_led_effects()
        unit.gcode.run_script_from_command.assert_has_calls([
            call("SET_LED_EFFECT EFFECT=lane1_loaded STOP=1"),
            call("SET_LED_EFFECT EFFECT=lane2_fault STOP=1"),
        ])
        assert unit.gcode.run_script_from_command.call_count == 2

    def test_clears_active_led_effects_after_stopping(self):
        unit = _make_unit()
        unit.afc.active_led_effects = ["lane1_loaded", "lane2_fault"]
        unit._stop_led_effects()
        assert unit.afc.active_led_effects == []

    def test_exception_from_gcode_is_swallowed(self):
        unit = _make_unit()
        unit.afc.active_led_effects = ["lane1_loaded"]
        unit.gcode.run_script_from_command.side_effect = Exception("boom")
        unit._stop_led_effects()  # must not raise

    def test_exception_on_one_effect_does_not_skip_remaining_effects(self):
        """The try/except is now per-iteration, not around the whole loop:
        a failure stopping one effect must not prevent the rest from being
        attempted too."""
        unit = _make_unit()
        unit.afc.active_led_effects = ["lane1_loaded", "lane2_fault"]
        unit.gcode.run_script_from_command.side_effect = [Exception("boom"), None]
        unit._stop_led_effects()
        unit.gcode.run_script_from_command.assert_has_calls([
            call("SET_LED_EFFECT EFFECT=lane1_loaded STOP=1"),
            call("SET_LED_EFFECT EFFECT=lane2_fault STOP=1"),
        ])
        assert unit.gcode.run_script_from_command.call_count == 2

    def test_clears_active_led_effects_even_when_stopping_raised(self):
        unit = _make_unit()
        unit.afc.active_led_effects = ["lane1_loaded"]
        unit.gcode.run_script_from_command.side_effect = Exception("boom")
        unit._stop_led_effects()
        assert unit.afc.active_led_effects == []

    # ── targets ────────────────────────────────────────────────────────────────

    def test_targets_only_stops_matching_effects(self):
        unit = _make_unit()
        unit.afc.active_led_effects = ["lane1_loaded", "lane2_fault"]
        unit._stop_led_effects(["lane1"])
        unit.gcode.run_script_from_command.assert_called_once_with(
            "SET_LED_EFFECT EFFECT=lane1_loaded STOP=1")

    def test_targets_leaves_non_matching_effects_in_active_list(self):
        unit = _make_unit()
        unit.afc.active_led_effects = ["lane1_loaded", "lane2_fault"]
        unit._stop_led_effects(["lane1"])
        assert unit.afc.active_led_effects == ["lane2_fault"]

    def test_targets_matches_multiple_names(self):
        unit = _make_unit()
        unit.afc.active_led_effects = ["lane1_loaded", "lane2_fault", "extruder1_tool_loaded"]
        unit._stop_led_effects(["lane1", "extruder1"])
        assert unit.afc.active_led_effects == ["lane2_fault"]

    def test_targets_no_match_stops_nothing(self):
        unit = _make_unit()
        unit.afc.active_led_effects = ["lane2_fault"]
        unit._stop_led_effects(["lane1"])
        unit.gcode.run_script_from_command.assert_not_called()
        assert unit.afc.active_led_effects == ["lane2_fault"]

    def test_targets_empty_list_stops_nothing(self):
        """An empty target list is distinct from targets=None -- neither
        matches any effect, so everything is preserved rather than cleared."""
        unit = _make_unit()
        unit.afc.active_led_effects = ["lane1_loaded"]
        unit._stop_led_effects([])
        unit.gcode.run_script_from_command.assert_not_called()
        assert unit.afc.active_led_effects == ["lane1_loaded"]

    def test_targets_does_not_prefix_match_across_similar_names(self):
        """Target "lane1" must not match "lane10_loaded" -- the match requires
        the full "<target>_" prefix, not just a substring."""
        unit = _make_unit()
        unit.afc.active_led_effects = ["lane10_loaded"]
        unit._stop_led_effects(["lane1"])
        unit.gcode.run_script_from_command.assert_not_called()
        assert unit.afc.active_led_effects == ["lane10_loaded"]


# ── _trigger_led_state ───────────────────────────────────────────────────────

class TestTriggerLedState:
    def test_stops_only_lane_effects_when_only_lane_given(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._stop_led_effects = MagicMock()
        unit._trigger_led_state(lane, static_color=lane.led_ready)
        unit._stop_led_effects.assert_called_once_with([lane.name])

    def test_stops_only_extruder_effects_when_only_extruder_given(self):
        unit = _make_unit()
        extruder = MagicMock()
        extruder.name = "extruder1"
        unit._stop_led_effects = MagicMock()
        unit._trigger_led_state(extruder=extruder, static_color="0,1,0,0")
        unit._stop_led_effects.assert_called_once_with(["extruder1"])

    def test_stops_both_lane_and_extruder_effects_when_both_given(self):
        unit = _make_unit()
        lane = _make_lane()
        extruder = MagicMock()
        extruder.name = "extruder1"
        unit._stop_led_effects = MagicMock()
        unit._trigger_led_state(lane, extruder=extruder, static_color=lane.led_ready)
        unit._stop_led_effects.assert_called_once_with([lane.name, "extruder1"])

    def test_applies_static_color(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state(lane, static_color=lane.led_ready)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_ready, lane.led_index)

    def test_none_static_color_skips_afc_led(self):
        """Covers the `static_color is not None` guard's False branch."""
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state(lane, static_color=None)
        unit.afc.function.afc_led.assert_not_called()

    def test_no_effect_suffix_does_not_call_call_led_effect(self):
        """Covers the `if effect_suffix:` guard's False branch (default None)."""
        unit = _make_unit()
        lane = _make_lane()
        unit._call_led_effect = MagicMock()
        unit._trigger_led_state(lane, static_color=lane.led_ready)
        unit._call_led_effect.assert_not_called()

    def test_empty_string_effect_suffix_does_not_call_call_led_effect(self):
        """Covers `if effect_suffix:` treating "" as falsy, distinct from None."""
        unit = _make_unit()
        lane = _make_lane()
        unit._call_led_effect = MagicMock()
        unit._trigger_led_state(lane, static_color=lane.led_ready, effect_suffix="")
        unit._call_led_effect.assert_not_called()

    def test_effect_suffix_calls_call_led_effect_with_lane_name(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._call_led_effect = MagicMock()
        unit._trigger_led_state(lane, static_color=lane.led_ready, effect_suffix="loaded")
        unit._call_led_effect.assert_called_once_with(lane.name, "loaded")

    # ── lane is None / extruder is None combinations ──────────────────────────

    def test_lane_none_and_extruder_none_returns_early(self):
        """Covers the new `lane is None and extruder is None` guard."""
        unit = _make_unit()
        unit._trigger_led_state(static_color="0,1,0,0", effect_suffix="loaded")
        unit.afc.function.afc_led.assert_not_called()
        unit.gcode.run_script_from_command.assert_not_called()

    def test_lane_none_and_extruder_none_does_not_stop_effects(self):
        """The early return now happens before _stop_led_effects() runs, so
        neither lane nor extruder being given must skip it entirely --
        there's nothing to scope the stop to."""
        unit = _make_unit()
        unit._stop_led_effects = MagicMock()
        unit._trigger_led_state()
        unit._stop_led_effects.assert_not_called()

    def test_extruder_only_sets_extruder_led_not_lane_led(self):
        unit = _make_unit()
        extruder = MagicMock()
        unit._trigger_led_state(extruder=extruder, static_color="0,1,0,0")
        unit.afc.function.afc_led.assert_not_called()
        extruder.set_status_led.assert_called_once_with("0,1,0,0")

    def test_lane_and_extruder_both_get_static_color_when_no_override(self):
        unit = _make_unit()
        lane = _make_lane()
        extruder = MagicMock()
        unit._trigger_led_state(lane, extruder=extruder, static_color=lane.led_ready)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_ready, lane.led_index)
        extruder.set_status_led.assert_called_once_with(lane.led_ready)

    def test_extruder_static_color_overrides_static_color_for_extruder_only(self):
        unit = _make_unit()
        lane = _make_lane()
        extruder = MagicMock()
        unit._trigger_led_state(lane, extruder=extruder, static_color=lane.led_ready,
                                extruder_static_color=lane.led_tool_unloaded)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_ready, lane.led_index)
        extruder.set_status_led.assert_called_once_with(lane.led_tool_unloaded)

    def test_extruder_static_color_ignored_when_extruder_not_given(self):
        """extruder_static_color must not affect the lane's own LED."""
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state(lane, static_color=lane.led_ready,
                                extruder_static_color=lane.led_tool_unloaded)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_ready, lane.led_index)

    # ── extruder LED effect delegation ─────────────────────────────────────────

    def test_effect_suffix_calls_call_led_effect_with_extruder_name(self):
        unit = _make_unit()
        extruder = MagicMock(name="extruder")
        extruder.name = "extruder1"
        unit._call_led_effect = MagicMock()
        unit._trigger_led_state(extruder=extruder, static_color="0,1,0,0",
                                effect_suffix="tool_loaded")
        unit._call_led_effect.assert_called_once_with("extruder1", "tool_loaded")

    def test_lane_none_only_calls_call_led_effect_for_extruder(self):
        unit = _make_unit()
        extruder = MagicMock(name="extruder")
        extruder.name = "extruder1"
        unit._call_led_effect = MagicMock()
        unit._trigger_led_state(extruder=extruder, static_color="0,1,0,0",
                                effect_suffix="tool_loaded")
        unit._call_led_effect.assert_called_once_with("extruder1", "tool_loaded")

    def test_extruder_none_only_calls_call_led_effect_for_lane(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._call_led_effect = MagicMock()
        unit._trigger_led_state(lane, static_color=lane.led_ready, effect_suffix="loaded")
        unit._call_led_effect.assert_called_once_with(lane.name, "loaded")

    def test_lane_and_extruder_both_call_call_led_effect_independently(self):
        unit = _make_unit()
        lane = _make_lane()
        extruder = MagicMock(name="extruder")
        extruder.name = "extruder1"
        unit._call_led_effect = MagicMock()
        unit._trigger_led_state(lane, extruder=extruder, static_color=lane.led_tool_loaded,
                                effect_suffix="tool_loaded")
        assert unit._call_led_effect.call_args_list == [
            call(lane.name, "tool_loaded"),
            call("extruder1", "tool_loaded"),
        ]


# ── _call_led_effect ─────────────────────────────────────────────────────────

class TestCallLedEffect:
    def test_effect_present_calls_set_led_effect_with_constructed_name(self):
        unit = _make_unit()
        unit.printer.objects["led_effect lane1_loaded"] = MagicMock()
        unit._call_led_effect("lane1", "loaded")
        unit.gcode.run_script_from_command.assert_called_once_with(
            "SET_LED_EFFECT EFFECT=lane1_loaded")

    def test_effect_present_appends_constructed_name_to_active_led_effects(self):
        unit = _make_unit()
        unit.printer.objects["led_effect lane1_loaded"] = MagicMock()
        unit._call_led_effect("lane1", "loaded")
        assert unit.afc.active_led_effects == ["lane1_loaded"]

    def test_effect_present_appends_to_existing_active_led_effects_without_clearing(self):
        unit = _make_unit()
        unit.printer.objects["led_effect lane1_loaded"] = MagicMock()
        unit.afc.active_led_effects = ["lane2_fault"]
        unit._call_led_effect("lane1", "loaded")
        assert unit.afc.active_led_effects == ["lane2_fault", "lane1_loaded"]

    def test_effect_not_present_skips_set_led_effect(self):
        unit = _make_unit()
        # printer.objects has no "led_effect lane1_loaded" entry
        unit._call_led_effect("lane1", "loaded")
        unit.gcode.run_script_from_command.assert_not_called()

    def test_effect_not_present_does_not_append_to_active_led_effects(self):
        unit = _make_unit()
        # printer.objects has no "led_effect lane1_loaded" entry
        unit._call_led_effect("lane1", "loaded")
        assert unit.afc.active_led_effects == []

    def test_exception_does_not_raise(self):
        unit = _make_unit()
        unit.printer.objects["led_effect lane1_loaded"] = MagicMock()
        unit.gcode.run_script_from_command.side_effect = Exception("boom")
        unit._call_led_effect("lane1", "loaded")  # must not raise

    def test_exception_prevents_append_to_active_led_effects(self):
        unit = _make_unit()
        unit.printer.objects["led_effect lane1_loaded"] = MagicMock()
        unit.gcode.run_script_from_command.side_effect = Exception("boom")
        unit._call_led_effect("lane1", "loaded")
        assert unit.afc.active_led_effects == []


# ── lane_* -> _trigger_led_state suffix wiring ────────────────────────────────

class TestLedEffectSuffixMapping:
    """Each lane_* status method must delegate to _trigger_led_state with its
    own distinct effect suffix, so a `led_effect <lane>_<suffix>` macro can
    target that specific state independently of the others."""

    def test_lane_not_ready_uses_not_ready_suffix(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_not_ready(lane)
        unit._trigger_led_state.assert_called_once_with(
            lane, static_color=lane.led_not_ready, effect_suffix="not_ready")

    def test_lane_loaded_uses_loaded_suffix(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_loaded(lane)
        unit._trigger_led_state.assert_called_once_with(
            lane, static_color=lane.led_ready, effect_suffix="loaded")

    def test_lane_unloading_uses_unloading_suffix(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_unloading(lane)
        unit._trigger_led_state.assert_called_once_with(
            lane, static_color=lane.led_unloading, effect_suffix="unloading")

    def test_lane_unloaded_uses_unloaded_suffix_not_not_ready(self):
        """lane_unloaded used to just call lane_not_ready() (suffix
        "not_ready"); it now applies its own "unloaded" suffix so the two
        states can trigger independent LED effect macros."""
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_unloaded(lane)
        unit._trigger_led_state.assert_called_once_with(
            lane, static_color=lane.led_not_ready, effect_suffix="unloaded")

    def test_lane_loading_uses_loading_suffix(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_loading(lane)
        unit._trigger_led_state.assert_called_once_with(
            lane, static_color=lane.led_loading, effect_suffix="loading")

    def test_lane_tool_loaded_gears_uses_tool_loaded_gears_suffix(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit._trigger_led_state = MagicMock()
        unit.lane_tool_loaded_gears(lane)
        unit._trigger_led_state.assert_called_once_with(
            lane, lane.extruder_obj, effect_suffix="tool_loaded_gears")

    def test_lane_tool_loaded_uses_tool_loaded_suffix(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit._trigger_led_state = MagicMock()
        unit.lane_tool_loaded(lane)
        unit._trigger_led_state.assert_called_once_with(
            lane, lane.extruder_obj, static_color=lane.led_tool_loaded,
            effect_suffix="tool_loaded")

    def test_lane_tool_unloaded_uses_tool_unloaded_suffix(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit._trigger_led_state = MagicMock()
        unit.lane_tool_unloaded(lane)
        unit._trigger_led_state.assert_called_once_with(
            lane, lane.extruder_obj, static_color=lane.led_ready,
            extruder_static_color=lane.led_tool_unloaded, effect_suffix="tool_unloaded")

    def test_lane_tool_loaded_idle_uses_tool_loaded_idle_suffix(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.extruder_obj = MagicMock()
        unit._trigger_led_state = MagicMock()
        unit.lane_tool_loaded_idle(lane)
        unit._trigger_led_state.assert_called_once_with(
            lane, static_color=lane.led_tool_loaded_idle, extruder=lane.extruder_obj,
            effect_suffix="tool_loaded_idle")

    def test_lane_fault_uses_fault_suffix(self):
        unit = _make_unit()
        lane = _make_lane()
        unit._trigger_led_state = MagicMock()
        unit.lane_fault(lane)
        unit._trigger_led_state.assert_called_once_with(
            lane, static_color=lane.led_fault, effect_suffix="fault")


# ── lane_illuminate_spool ────────────────────────────────────────────────────

class TestLaneIlluminateSpool:
    def test_illuminates_spool_led_when_spool_index_set(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.led_spool_index = "2"
        unit.lane_illuminate_spool(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_spool_illum, "2")

    def test_no_call_when_spool_index_is_none(self):
        """Covers the `lane.led_spool_index is not None` False branch."""
        unit = _make_unit()
        lane = _make_lane()
        lane.led_spool_index = None
        unit.lane_illuminate_spool(lane)
        unit.afc.function.afc_led.assert_not_called()


# ── led_use_filament_color ─────────────────────────────────────────────────────

class TestLedUseFilamentColor:
    def test_filament_color_used_when_enabled_and_color_set(self):
        """When led_use_filament_color=True and lane has a color, LED uses the filament color."""
        unit = _make_unit()
        lane = _make_lane()
        lane.led_use_filament_color = True
        lane.color = "#FF0000"
        unit.afc.function.HexToLedString.return_value = "1,0,0,0"
        unit.lane_loaded(lane)
        unit.afc.function.HexToLedString.assert_called_once_with("FF0000")
        unit.afc.function.afc_led.assert_called_once_with("1,0,0,0", lane.led_index)

    def test_fallback_to_state_color_when_enabled_but_no_color(self):
        """When led_use_filament_color=True but lane has no color, falls back to state color."""
        unit = _make_unit()
        lane = _make_lane()
        lane.led_use_filament_color = True
        lane.color = ""
        unit.lane_loaded(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_ready, lane.led_index)

    def test_state_color_used_when_disabled(self):
        """When led_use_filament_color=False, LED uses the configured state color."""
        unit = _make_unit()
        lane = _make_lane()
        lane.led_use_filament_color = False
        lane.color = "#FF0000"
        unit.lane_loaded(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_ready, lane.led_index)

    def test_filament_color_with_hash_prefix(self):
        """Color with # prefix is handled correctly."""
        unit = _make_unit()
        lane = _make_lane()
        lane.led_use_filament_color = True
        lane.color = "#00FF00"
        unit.afc.function.HexToLedString.return_value = "0,1,0,0"
        unit.lane_loaded(lane)
        unit.afc.function.HexToLedString.assert_called_once_with("00FF00")

    def test_filament_color_invalid_hex_falls_back(self):
        """Invalid hex color falls back to state color."""
        unit = _make_unit()
        lane = _make_lane()
        lane.led_use_filament_color = True
        lane.color = "not-a-color"
        unit.lane_loaded(lane)
        unit.afc.function.afc_led.assert_called_once_with(lane.led_ready, lane.led_index)


# ── set_logo_color ────────────────────────────────────────────────────────────

class TestSetLogoColor:
    def test_calls_afc_led_when_color_present(self):
        unit = _make_unit()
        unit.led_logo_index = "0"
        unit.afc.function.HexToLedString = MagicMock(return_value="0,0,0,0")
        unit.set_logo_color("FF0000")
        unit.afc.function.afc_led.assert_called()

    def test_no_call_when_color_is_none(self):
        unit = _make_unit()
        unit.set_logo_color(None)
        unit.afc.function.afc_led.assert_not_called()

    def test_no_call_when_color_omitted(self):
        """Covers color's new default value (None) when called with no argument."""
        unit = _make_unit()
        unit.set_logo_color()
        unit.afc.function.afc_led.assert_not_called()

    def test_no_call_when_color_is_empty_string(self):
        unit = _make_unit()
        unit.set_logo_color("")
        unit.afc.function.afc_led.assert_not_called()


# ── _buffer_toolhead_load_check ────────────────────────────────────────────────────────────
class TestBufferToolheadLoadCheck:
    def test_homing_not_enabled_not_loaded(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.afc.homing_enabled = False
        lane.tool_loaded = False
        result = unit._buffer_toolhead_load_check(lane)
        assert result is False
    
    def test_homing_not_enabled_loaded(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.afc.homing_enabled = False
        lane.tool_loaded = True
        result = unit._buffer_toolhead_load_check(lane)
        assert result is True

    def test_homing_enabled_disable_buffer_tool_check_loaded(self):
        unit = _make_unit()
        lane = _make_lane()
        unit.afc.homing_enabled = True
        lane.tool_loaded = True
        unit.enable_buffer_tool_check = False
        result = unit._buffer_toolhead_load_check(lane)
        assert result is True

    def test_homing_enabled_loaded_fail_extend(self):
        unit = _make_unit()
        lane = _make_lane()
        lane.buffer_obj = MagicMock()
        unit.afc.homing_enabled = True
        lane.tool_loaded = True
        lane.buffer_obj.advance_state = False
        lane.move_to = MagicMock()
        lane.move_to.return_value = (False, 200, False)
        result = unit._buffer_toolhead_load_check(lane)
        assert result is False
        error_msgs = [m for lvl, m in unit.logger.messages if lvl == "error"]
        assert any(f"Buffer toolhead loaded check failed for {lane.name}. Please verify" in m for m in error_msgs)
    
    def test_homing_enabled_loaded_extended(self):
        from extras.AFC_lane import MoveDirection
        SIDE_EFFECT_DIST = 200
        unit = _make_unit()
        lane = _make_lane()
        lane.buffer_obj = MagicMock()
        unit.afc.homing_enabled = True
        lane.tool_loaded = True
        lane.buffer_obj.advance_state = False
        lane.move_to = MagicMock()
        lane.move = MagicMock()
        lane.move_to.side_effect = [(True, SIDE_EFFECT_DIST, False)]
        result = unit._buffer_toolhead_load_check(lane)
        call_args = lane.move.call_args[0]
        assert result is True
        assert call_args[0] == (SIDE_EFFECT_DIST * MoveDirection.NEG)


class TestLaneOrdering:
    def test_lane_in_correct_order(self):
        # Tests to verify that lanes are in correct natural number order
        unit = _make_unit()
        lane = _make_lane()

        unit.lanes = {"lane2": lane, "lane1": lane, "custom_name4":lane, "lane3": lane, "lane10": lane}
        unit.handle_ready()

        assert list(unit.lanes.keys()) == ["lane1", "lane2", "lane3", "custom_name4", "lane10"]