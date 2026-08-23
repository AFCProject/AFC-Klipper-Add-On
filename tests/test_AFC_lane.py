"""
Unit tests for extras/AFC_lane.py

Covers:
  - SpeedMode enum values
  - AssistActive enum values
  - MoveDirection float constants
  - AFCLaneState string constants
  - AFCHomingPoints string constants
  - AFCLane instantiation and attribute initialization
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch
import pytest

from extras.AFC_lane import (
    SpeedMode,
    AssistActive,
    MoveDirection,
    AFCLaneState,
    AFCHomingPoints,
    AFCLane,
    AFCU1Lane,
    EXCLUDE_TYPES,
    AFCMoveWarning
)
from extras.AFC_stepper import AFCExtruderStepper
from tests.conftest import MockAFC, MockLogger


# ── SpeedMode ─────────────────────────────────────────────────────────────────

class TestSpeedMode:
    def test_none_value_is_python_none(self):
        assert SpeedMode.NONE.value is None

    def test_long_value(self):
        assert SpeedMode.LONG.value == 1

    def test_short_value(self):
        assert SpeedMode.SHORT.value == 2

    def test_hub_value(self):
        assert SpeedMode.HUB.value == 3

    def test_night_value(self):
        assert SpeedMode.NIGHT.value == 4

    def test_calibration_value(self):
        assert SpeedMode.CALIBRATION.value == 5
    
    def test_dist_hub_value(self):
        assert SpeedMode.DIST_HUB.value == 6

    def test_all_members_unique(self):
        values = [m.value for m in SpeedMode if m.value is not None]
        assert len(values) == len(set(values))

    def test_comparison_equal(self):
        assert SpeedMode.LONG == SpeedMode.LONG

    def test_comparison_not_equal(self):
        assert SpeedMode.LONG != SpeedMode.SHORT


# ── AssistActive ──────────────────────────────────────────────────────────────

class TestAssistActive:
    def test_yes_value(self):
        assert AssistActive.YES.value == 1

    def test_no_value(self):
        assert AssistActive.NO.value == 2

    def test_dynamic_value(self):
        assert AssistActive.DYNAMIC.value == 3

    def test_members_count(self):
        assert len(list(AssistActive)) == 3


# ── MoveDirection ─────────────────────────────────────────────────────────────

class TestMoveDirection:
    def test_pos_is_positive_one(self):
        assert MoveDirection.POS == 1.0

    def test_neg_is_negative_one(self):
        assert MoveDirection.NEG == -1.0

    def test_pos_is_float_subclass(self):
        assert isinstance(MoveDirection.POS, float)

    def test_neg_is_float_subclass(self):
        assert isinstance(MoveDirection.NEG, float)

    def test_multiplication_with_distance(self):
        dist = 50.0
        assert dist * MoveDirection.POS == 50.0
        assert dist * MoveDirection.NEG == -50.0


# ── AFCLaneState ──────────────────────────────────────────────────────────────

class TestAFCLaneState:
    def test_none_constant(self):
        assert AFCLaneState.NONE == "None"

    def test_error_constant(self):
        assert AFCLaneState.ERROR == "Error"

    def test_loaded_constant(self):
        assert AFCLaneState.LOADED == "Loaded"

    def test_tooled_constant(self):
        assert AFCLaneState.TOOLED == "Tooled"

    def test_tool_loaded_constant(self):
        assert AFCLaneState.TOOL_LOADED == "Tool Loaded"

    def test_tool_loading_constant(self):
        assert AFCLaneState.TOOL_LOADING == "Tool Loading"

    def test_tool_unloading_constant(self):
        assert AFCLaneState.TOOL_UNLOADING == "Tool Unloading"

    def test_hub_loading_constant(self):
        assert AFCLaneState.HUB_LOADING == "HUB Loading"

    def test_ejecting_constant(self):
        assert AFCLaneState.EJECTING == "Ejecting"

    def test_calibrating_constant(self):
        assert AFCLaneState.CALIBRATING == "Calibrating"

    def test_all_constants_are_strings(self):
        attrs = [a for a in dir(AFCLaneState) if not a.startswith("_")]
        for attr in attrs:
            assert isinstance(getattr(AFCLaneState, attr), str)

    def test_all_constants_unique(self):
        attrs = [a for a in dir(AFCLaneState) if not a.startswith("_")]
        values = [getattr(AFCLaneState, a) for a in attrs]
        assert len(values) == len(set(values))


# ── AFCHomingPoints ───────────────────────────────────────────────────────────

class TestAFCHomingPoints:
    def test_none_constant(self):
        assert AFCHomingPoints.NONE is None

    def test_hub_constant(self):
        assert AFCHomingPoints.HUB == "hub"

    def test_load_constant(self):
        assert AFCHomingPoints.LOAD == "load"

    def test_tool_constant(self):
        assert AFCHomingPoints.TOOL == "tool"

    def test_tool_start_constant(self):
        assert AFCHomingPoints.TOOL_START == "tool_start"

    def test_buffer_constant(self):
        assert AFCHomingPoints.BUFFER == "buffer"

    def test_buffer_trail_constant(self):
        assert AFCHomingPoints.BUFFER_TRAIL == "buffer_trailing"


# ── EXCLUDE_TYPES ──────────────────────────────────────────────────────────────

class TestExcludeTypes:
    def test_htlf_in_exclude_types(self):
        assert "HTLF" in EXCLUDE_TYPES

    def test_vivid_in_exclude_types(self):
        assert "ViViD" in EXCLUDE_TYPES

    def test_openams_in_exclude_types(self):
        assert "OpenAMS" in EXCLUDE_TYPES


# ── AFCLane initialization ────────────────────────────────────────────────────
# AFCLane.__init__ is tightly coupled to Klipper's runtime; we bypass it with
# __new__ and set attributes to their documented initial values.

def _make_afc_lane(fullname="AFC_stepper lane1"):
    """Build an AFCLane bypassing the complex __init__."""
    lane = AFCLane.__new__(AFCLane)
    parts = fullname.split()
    lane.logger = MockLogger()
    lane.fullname = fullname
    lane.name = parts[-1]
    lane.afc = MagicMock()
    lane.unit = "Turtle_1"
    lane.unit_obj = MagicMock()
    lane.hub_obj = None
    lane.buffer_obj = None
    lane.extruder_obj = MagicMock()
    lane.endstops = {}
    lane.espooler = MagicMock()
    lane.espooler.assist.return_value = False
    lane.hub = "PB1"
    lane.get_toolhead_pre_sensor_state = MagicMock()
    lane.weight = 1000
    lane.empty_spool_weight = 200
    lane.filament_density = 1.24
    lane.filament_diameter = 1.75
    lane.inner_diameter = 75
    lane.outer_diameter = 200
    lane.max_motor_rpm = 500
    lane.rwd_speed_multi = 0.5
    lane.fwd_speed_multi = 0.5
    lane.drive_stepper = None
    lane.dist_hub = 900
    lane.remember_spool = False
    lane.short_moves_speed = 50
    lane.short_moves_accel = 100
    lane._load_state = False
    lane.runout_lane = None
    lane.map = ["T0"]
    lane._map = ["T0"]
    lane.current_map = "T0"
    lane._sent_lane_data_keys = []
    lane.gcode = MagicMock()
    lane.need_purge = False
    return lane


class TestAFCLaneInit:
    def test_lane_name_extracted_from_fullname(self):
        lane = _make_afc_lane("AFC_stepper lane1")
        assert lane.name == "lane1"

    def test_lane_fullname_stored(self):
        lane = _make_afc_lane("AFC_stepper lane1")
        assert lane.fullname == "AFC_stepper lane1"

    def test_initial_unit_obj_is_not_none(self):
        lane = _make_afc_lane()
        assert lane.unit_obj is not None

    def test_initial_hub_obj_is_none(self):
        lane = _make_afc_lane()
        assert lane.hub_obj is None

    def test_initial_buffer_obj_is_none(self):
        lane = _make_afc_lane()
        assert lane.buffer_obj is None

    def test_initial_extruder_obj_is_not_none(self):
        lane = _make_afc_lane()
        assert lane.extruder_obj is not None

    def test_update_weight_delay_class_constant(self):
        assert AFCLane.UPDATE_WEIGHT_DELAY == 10.0


# ── selector_cal_dis (real __init__ construction) ────────────────────────────
# The rest of this file bypasses AFCLane.__init__ via __new__, but
# selector_cal_dis's fix (always read, not just when a selector pin is
# configured) only shows up in __init__ itself, so this drives a real
# construction. add_filament_switch does real Klipper config-object wiring
# that isn't worth reproducing under test, so it's patched out; the "unit"
# config option has no default in AFCLane.__init__ and so must be supplied.

def _make_real_afc_lane(name="lane1", values=None):
    """Construct a real AFCLane via its actual __init__."""
    from tests.conftest import MockAFC, MockConfig, MockPrinter

    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    all_values = {"unit": "Turtle_1:1"}
    if values:
        all_values.update(values)
    config = MockConfig(name=f"AFC_stepper {name}", printer=printer, values=all_values)
    with patch("extras.AFC_lane.add_filament_switch",
               return_value=(MagicMock(), MagicMock())):
        return AFCLane(config)


class TestSelectorCalDis:
    def test_defaults_to_zero_when_selector_not_configured(self):
        lane = _make_real_afc_lane()
        assert lane.selector_cal_dis == 0.0

    def test_reads_from_config_when_selector_not_configured(self):
        """This exact case (selector unset, selector_cal_distance set) used
        to leave selector_cal_dis at None, since the read was nested inside
        "if self.selector is not None"."""
        lane = _make_real_afc_lane(values={"selector_cal_distance": 2.5})
        assert lane.selector_cal_dis == 2.5

    def test_reads_from_config_when_selector_also_configured(self):
        lane = _make_real_afc_lane(values={"selector": "PA5", "selector_cal_distance": 1.5})
        assert lane.selector_cal_dis == 1.5


# ── _get_steppers: stepperless unit early return ─────────────────────────────
# New branch: stepperless units (e.g. OpenAMS) have no drive stepper to set up,
# so _get_steppers returns immediately after resolving the unit object.

class TestGetSteppersStepperlessEarlyReturn:
    def _make_config(self, unit_obj):
        lane = _make_afc_lane()
        lane.unit = "Turtle_1"
        lane.printer = MagicMock()
        lane.printer.load_object.return_value = unit_obj
        lane.endstops = {}
        lane.drive_stepper = None

        unit_cfg = MagicMock()
        unit_cfg.get_name.return_value = "AFC_BoxTurtle Turtle_1"
        config = MagicMock()
        config.fileconfig.sections.return_value = ["AFC_BoxTurtle Turtle_1"]
        config.getsection.return_value = unit_cfg
        config.get.return_value = None
        return lane, config

    def test_stepperless_drive_true_returns_before_stepper_setup(self):
        unit_obj = MagicMock(spec=["stepperless_drive"])
        unit_obj.stepperless_drive = True
        lane, config = self._make_config(unit_obj)

        lane._get_steppers(config)

        assert lane.unit_obj is unit_obj
        # only_lane is set further down in the non-stepperless path; its
        # absence proves the early return fired before reaching that code.
        assert not hasattr(lane, "only_lane")

    def test_stepperless_drive_false_continues_past_early_return(self):
        unit_obj = MagicMock(spec=["drive_stepper_obj", "type"])
        unit_obj.drive_stepper_obj = None
        unit_obj.type = "BoxTurtle"
        lane, config = self._make_config(unit_obj)
        config.get.return_value = None  # no step_pin configured

        lane._get_steppers(config)

        assert lane.unit_obj is unit_obj
        assert lane.only_lane is True

    def test_stepperless_drive_missing_attribute_continues_past_early_return(self):
        unit_obj = MagicMock(spec=["type"])
        unit_obj.type = "BoxTurtle"
        lane, config = self._make_config(unit_obj)
        config.get.return_value = None

        lane._get_steppers(config)

        assert not hasattr(unit_obj, "stepperless_drive")
        assert lane.only_lane is True


# ── _set_homing_endstop ──────────────────────────────────────────────────────
# Guard added to skip registering a hub's "virtual" pin as a real endstop.

class TestSetHomingEndstop:
    def test_virtual_pin_returns_without_registering(self):
        lane = _make_afc_lane()
        ppins = MagicMock()
        query_endstops = MagicMock()

        lane._set_homing_endstop(query_endstops, ppins, "virtual", "hub")

        ppins.allow_multi_use_pin.assert_not_called()
        ppins.setup_pin.assert_not_called()
        query_endstops.register_endstop.assert_not_called()

    def test_virtual_pin_case_and_whitespace_insensitive(self):
        lane = _make_afc_lane()
        ppins = MagicMock()
        query_endstops = MagicMock()

        lane._set_homing_endstop(query_endstops, ppins, "  Virtual  ", "hub")

        ppins.allow_multi_use_pin.assert_not_called()

    def test_real_pin_registers_endstop(self):
        lane = _make_afc_lane()
        ppins = MagicMock()
        endstop = MagicMock()
        ppins.setup_pin.return_value = endstop
        query_endstops = MagicMock()

        lane._set_homing_endstop(query_endstops, ppins, "!PA0", "hub")

        ppins.allow_multi_use_pin.assert_called_once_with("PA0")
        ppins.parse_pin.assert_called_once_with("!PA0", True, True)
        ppins.setup_pin.assert_called_once_with("endstop", "!PA0")
        query_endstops.register_endstop.assert_called_once_with(endstop, f"{lane.name}_hub")


# ── move_advanced: stepperless unit delegation ───────────────────────────────
# New branch: stepperless units (e.g. OpenAMS) delegate the move to the unit's
# lane_move instead of driving a (non-existent) stepper directly.

class TestMoveAdvancedStepperlessDelegation:
    def _make(self):
        lane = _make_afc_lane()
        lane.get_speed_accel = MagicMock(return_value=(50, 100))
        lane.get_active_assist = MagicMock(return_value=False)
        lane.move = MagicMock()
        return lane

    def test_stepperless_with_lane_move_delegates_and_skips_move(self):
        lane = self._make()
        lane.unit_obj = MagicMock(spec=["stepperless_drive", "lane_move"])
        lane.unit_obj.stepperless_drive = True

        lane.move_advanced(25.0, SpeedMode.SHORT)

        lane.unit_obj.lane_move.assert_called_once_with(lane, 25.0, SpeedMode.SHORT)
        lane.move.assert_not_called()

    def test_unit_obj_none_does_not_delegate(self):
        lane = self._make()
        lane.unit_obj = None

        lane.move_advanced(25.0, SpeedMode.SHORT)

        lane.move.assert_called_once_with(25.0, 50, 100, False)

    def test_stepperless_false_does_not_delegate(self):
        lane = self._make()
        lane.unit_obj = MagicMock(spec=["stepperless_drive", "lane_move"])
        lane.unit_obj.stepperless_drive = False

        lane.move_advanced(25.0, SpeedMode.SHORT)

        lane.unit_obj.lane_move.assert_not_called()
        lane.move.assert_called_once_with(25.0, 50, 100, False)

    def test_stepperless_true_but_no_lane_move_does_not_delegate(self):
        lane = self._make()
        lane.unit_obj = MagicMock(spec=["stepperless_drive"])
        lane.unit_obj.stepperless_drive = True

        lane.move_advanced(25.0, SpeedMode.SHORT)

        lane.move.assert_called_once_with(25.0, 50, 100, False)


# ── _prep_capture_td1: unit hook interception ────────────────────────────────
# New branch: a unit's prep_capture_td1 hook can intercept TD-1 capture on
# stepperless units. A non-None return means the unit handled it.

class TestPrepCaptureTd1UnitHook:
    def _make(self, td1_when_loaded=True):
        lane = _make_afc_lane()
        lane.td1_when_loaded = td1_when_loaded
        lane.get_td1_data = MagicMock()
        lane.hub_obj = MagicMock()
        lane.hub_obj.state = False
        lane.afc.function.get_current_lane_obj.return_value = None
        return lane

    def test_hook_returns_non_none_skips_default_capture(self):
        lane = self._make()
        lane.unit_obj = MagicMock(spec=["prep_capture_td1"])
        lane.unit_obj.prep_capture_td1.return_value = ("ok", "msg")

        lane._prep_capture_td1()

        lane.unit_obj.prep_capture_td1.assert_called_once_with(lane)
        lane.get_td1_data.assert_not_called()

    def test_hook_returns_none_falls_through_to_default_capture(self):
        lane = self._make()
        lane.unit_obj = MagicMock(spec=["prep_capture_td1"])
        lane.unit_obj.prep_capture_td1.return_value = None

        lane._prep_capture_td1()

        lane.unit_obj.prep_capture_td1.assert_called_once_with(lane)
        lane.get_td1_data.assert_called_once_with()

    def test_no_hook_falls_through_to_default_capture(self):
        lane = self._make()
        lane.unit_obj = MagicMock(spec=[])

        lane._prep_capture_td1()

        lane.get_td1_data.assert_called_once_with()

    def test_td1_when_loaded_false_skips_entire_block(self):
        lane = self._make(td1_when_loaded=False)
        lane.unit_obj = MagicMock(spec=["prep_capture_td1"])
        lane.unit_obj.prep_capture_td1.return_value = ("ok", "msg")

        lane._prep_capture_td1()

        lane.unit_obj.prep_capture_td1.assert_not_called()
        lane.get_td1_data.assert_not_called()


# ── get_td1_data: unit hook interception ─────────────────────────────────────

class TestGetTd1DataUnitHook:
    def _make_no_device(self):
        # Setup so that, if the hook doesn't short-circuit, execution reaches
        # the "not loaded" check (distinguishable from the hook's own result).
        lane = _make_afc_lane()
        lane.td1_device_id = "td1_1"
        lane.td1_bowden_length = 100.0
        lane._load_state = False
        lane.hub_obj = None
        lane.prep_state = False
        return lane

    def test_hook_returns_non_none_short_circuits(self):
        lane = self._make_no_device()
        lane.unit_obj = MagicMock(spec=["capture_td1_data"])
        lane.unit_obj.capture_td1_data.return_value = (True, "captured via unit")

        result = lane.get_td1_data()

        assert result == (True, "captured via unit")
        lane.unit_obj.capture_td1_data.assert_called_once_with(lane)

    def test_hook_returns_none_falls_through_to_default_logic(self):
        lane = self._make_no_device()
        lane.unit_obj = MagicMock(spec=["capture_td1_data"])
        lane.unit_obj.capture_td1_data.return_value = None

        status, msg = lane.get_td1_data()

        lane.unit_obj.capture_td1_data.assert_called_once_with(lane)
        # Default logic reaches the "not loaded" check, proving execution
        # actually continued past the hook instead of using its result.
        assert status is False
        assert "not loaded" in msg.lower()

    def test_no_hook_falls_through_to_default_logic(self):
        lane = self._make_no_device()
        lane.unit_obj = MagicMock(spec=[])

        status, msg = lane.get_td1_data()

        assert status is False
        assert "not loaded" in msg.lower()


# ── handle_load_runout ────────────────────────────────────────────────────────

class TestHandleLoadRunout:
    def _make(self, load_debounce_button=True):
        lane = _make_afc_lane()
        lane.printer = MagicMock()
        lane.printer.state_message = "Printer is ready"
        lane.unit_obj = MagicMock()
        lane.unit_obj.type = "HTLF"
        lane._afc_prep_done = True
        lane.set_loaded = MagicMock()
        lane.set_unloaded = MagicMock()
        lane._post_prep_user_macro = MagicMock()
        lane._prep_capture_td1 = MagicMock()
        lane._perform_infinite_runout = MagicMock()
        lane._perform_pause_runout = MagicMock()
        lane.td1_device_id = None
        lane.tool_loaded = False
        lane.hub = "PB1"
        lane.status = "None"
        lane.runout_lane = None
        lane.afc.function.is_printing = MagicMock(return_value=False)
        lane.afc.TOOL_LOAD = MagicMock()
        lane.afc.error.AFC_error = MagicMock()
        lane.afc.save_vars = MagicMock()
        lane._load_suppressed = False
        lane._afc_staged_spool_id = None
        if load_debounce_button:
            lane.load_debounce_button = MagicMock()
        else:
            if hasattr(lane, "load_debounce_button"):
                del lane.load_debounce_button
        return lane

    # -- debounce button forwarding --

    def test_button_present_try_path_uses_keyword_form(self):
        lane = self._make()
        lane.handle_load_runout(100.0, True)
        lane.load_debounce_button._old_note_filament_present.assert_called_once_with(
            is_filament_present=True
        )

    def test_button_present_except_path_uses_positional_form(self):
        lane = self._make()
        lane.load_debounce_button._old_note_filament_present.side_effect = [
            TypeError("old style"), None
        ]
        lane.handle_load_runout(100.0, True)
        assert lane.load_debounce_button._old_note_filament_present.call_args_list == [
            call(is_filament_present=True),
            call(100.0, True),
        ]

    def test_button_absent_skips_forwarding_entirely(self):
        lane = self._make(load_debounce_button=False)
        # Should not raise despite there being no load_debounce_button attribute.
        lane.handle_load_runout(100.0, True)
        assert not hasattr(lane, "load_debounce_button")

    # -- outer gate: printer state / unit type / prep done (each alone False) --

    def test_outer_gate_wrong_state_message_blocks_processing(self):
        lane = self._make()
        lane.printer.state_message = "Starting"
        lane.handle_load_runout(100.0, True)
        lane.set_loaded.assert_not_called()

    def test_outer_gate_unit_type_not_only_load_blocks_processing(self):
        lane = self._make()
        lane.unit_obj.type = "BoxTurtle"
        lane.handle_load_runout(100.0, True)
        lane.set_loaded.assert_not_called()

    def test_outer_gate_prep_not_done_blocks_processing(self):
        lane = self._make()
        lane._afc_prep_done = False
        lane.handle_load_runout(100.0, True)
        lane.set_loaded.assert_not_called()

    def test_outer_gate_all_true_processes_load(self):
        lane = self._make()
        lane.handle_load_runout(100.0, True)
        lane.set_loaded.assert_called_once_with()

    # -- load_state True: staged spool id stashing --

    def test_staged_spool_id_captured_from_afc_spool(self):
        lane = self._make()
        lane.afc.spool.next_spool_id = "spool-42"
        lane.handle_load_runout(100.0, True)
        assert lane._afc_staged_spool_id == "spool-42"

    # -- load_state True: on_filament_insert suppression --

    def test_load_suppressed_true_skips_insert_and_resets_flag(self):
        lane = self._make()
        lane._load_suppressed = True
        lane.handle_load_runout(100.0, True)
        lane.unit_obj.on_filament_insert.assert_not_called()
        assert lane._load_suppressed is False

    def test_load_suppressed_false_calls_insert_hook(self):
        lane = self._make()
        lane._load_suppressed = False
        lane.handle_load_runout(100.0, True)
        lane.unit_obj.on_filament_insert.assert_called_once_with(lane)

    def test_missing_on_filament_insert_hook_does_not_raise(self):
        lane = self._make()
        lane.unit_obj = MagicMock(spec=["type", "check_runout"])
        lane.unit_obj.type = "HTLF"
        lane.handle_load_runout(100.0, True)  # should not raise
        assert not hasattr(lane.unit_obj, "on_filament_insert")

    # -- load_state True: TD-1 capture gate (each condition alone False) --

    def test_td1_device_id_none_skips_capture(self):
        lane = self._make()
        lane.td1_device_id = None
        lane.tool_loaded = False
        lane.handle_load_runout(100.0, True)
        lane._prep_capture_td1.assert_not_called()

    def test_tool_loaded_true_skips_capture(self):
        lane = self._make()
        lane.td1_device_id = "td1_1"
        lane.tool_loaded = True
        lane.handle_load_runout(100.0, True)
        lane._prep_capture_td1.assert_not_called()

    def test_td1_device_id_set_and_not_tool_loaded_captures(self):
        lane = self._make()
        lane.td1_device_id = "td1_1"
        lane.tool_loaded = False
        lane.handle_load_runout(100.0, True)
        lane._prep_capture_td1.assert_called_once_with()

    # -- load_state True: direct_load hub --

    def test_direct_load_and_printer_moving_errors_without_tool_load(self):
        lane = self._make()
        lane.hub = "direct_load"
        lane.afc.function.is_printing = MagicMock(return_value=True)
        lane.handle_load_runout(100.0, True)
        lane.afc.function.is_printing.assert_called_once_with(check_movement=True)
        lane.afc.error.AFC_error.assert_called_once_with(
            "Cannot load spool to toolhead while printer is actively moving or homing", False
        )
        lane.afc.TOOL_LOAD.assert_not_called()

    def test_direct_load_and_printer_idle_calls_tool_load(self):
        lane = self._make()
        lane.hub = "direct_load"
        lane.afc.function.is_printing = MagicMock(return_value=False)
        lane.afc.TOOL_LOAD.return_value = True
        lane.handle_load_runout(100.0, True)
        lane.afc.TOOL_LOAD.assert_called_once_with(lane)
        lane.afc.error.AFC_error.assert_not_called()
        lane.afc.afc_stats.increase_load_error_count.assert_not_called()

    def test_direct_load_tool_load_failure_increases_load_error_count(self):
        """When TOOL_LOAD fails in the direct_load branch, the failure is
        recorded via increase_load_error_count(self.afc)."""
        lane = self._make()
        lane.hub = "direct_load"
        lane.afc.function.is_printing = MagicMock(return_value=False)
        lane.afc.TOOL_LOAD.return_value = False
        lane.handle_load_runout(100.0, True)
        lane.afc.afc_stats.increase_load_error_count.assert_called_once_with(lane.afc)

    def test_non_direct_load_hub_skips_tool_load_branch(self):
        lane = self._make()
        lane.hub = "PB1"
        lane.handle_load_runout(100.0, True)
        lane.afc.TOOL_LOAD.assert_not_called()
        lane.afc.error.AFC_error.assert_not_called()

    def test_load_state_true_always_runs_post_prep_macro(self):
        lane = self._make()
        lane.handle_load_runout(100.0, True)
        lane._post_prep_user_macro.assert_called_once_with()

    # -- load_state False: on_filament_remove always fires --

    def test_load_state_false_calls_on_filament_remove(self):
        lane = self._make()
        lane.unit_obj.check_runout.return_value = False
        lane.status = "None"
        lane.handle_load_runout(100.0, False)
        lane.unit_obj.on_filament_remove.assert_called_once_with(lane)

    # -- load_state False: disabled-sensor warning gate (each alone False) --

    def test_no_fila_load_attribute_skips_disabled_warning(self):
        lane = self._make()
        assert not hasattr(lane, "fila_load")
        lane.unit_obj.check_runout.return_value = False
        lane.handle_load_runout(100.0, False)
        assert lane.logger.messages == []
        lane.set_unloaded.assert_called_once_with()

    def test_sensor_enabled_true_skips_disabled_warning(self):
        lane = self._make()
        lane.fila_load = MagicMock()
        lane.fila_load.runout_helper.sensor_enabled = True
        lane.afc.function.is_printing = MagicMock(return_value=True)
        lane.unit_obj.check_runout.return_value = False
        lane.handle_load_runout(100.0, False)
        assert lane.logger.messages == []
        lane.set_unloaded.assert_called_once_with()

    def test_not_printing_skips_disabled_warning(self):
        lane = self._make()
        lane.fila_load = MagicMock()
        lane.fila_load.runout_helper.sensor_enabled = False
        lane.afc.function.is_printing = MagicMock(return_value=False)
        lane.unit_obj.check_runout.return_value = False
        lane.handle_load_runout(100.0, False)
        assert lane.logger.messages == []
        lane.set_unloaded.assert_called_once_with()

    def test_disabled_sensor_while_printing_logs_warning_exactly(self):
        lane = self._make()
        lane.fila_load = MagicMock()
        lane.fila_load.runout_helper.sensor_enabled = False
        lane.afc.function.is_printing = MagicMock(return_value=True)
        lane.handle_load_runout(100.0, False)
        assert lane.logger.messages == [
            ("warning", "Load runout has been detected, but pause and runout "
                        "detection has been disabled")
        ]
        lane.unit_obj.check_runout.assert_not_called()

    # -- load_state False: unit-driven runout handling --

    def test_check_runout_true_and_unit_handles_runout_skips_generic_behavior(self):
        lane = self._make()
        lane.unit_obj.check_runout.return_value = True
        lane.unit_obj.handle_runout = MagicMock(return_value=True)
        lane.handle_load_runout(100.0, False)
        lane._perform_infinite_runout.assert_not_called()
        lane._perform_pause_runout.assert_not_called()
        lane.set_unloaded.assert_not_called()

    def test_check_runout_true_unit_declines_and_runout_lane_set_uses_infinite(self):
        lane = self._make()
        lane.unit_obj.check_runout.return_value = True
        lane.unit_obj.handle_runout = MagicMock(return_value=False)
        lane.runout_lane = "lane2"
        lane.handle_load_runout(100.0, False)
        lane._perform_infinite_runout.assert_called_once_with()
        lane._perform_pause_runout.assert_not_called()

    def test_check_runout_true_unit_declines_and_no_runout_lane_uses_pause(self):
        lane = self._make()
        lane.unit_obj.check_runout.return_value = True
        lane.unit_obj.handle_runout = MagicMock(return_value=False)
        lane.runout_lane = None
        lane.handle_load_runout(100.0, False)
        lane._perform_pause_runout.assert_called_once_with()
        lane._perform_infinite_runout.assert_not_called()

    def test_check_runout_true_no_handle_runout_hook_treated_as_undeclined(self):
        lane = self._make()
        lane.unit_obj = MagicMock(spec=["type", "on_filament_remove", "check_runout"])
        lane.unit_obj.type = "HTLF"
        lane.unit_obj.check_runout.return_value = True
        lane.runout_lane = None
        lane.handle_load_runout(100.0, False)
        assert not hasattr(lane.unit_obj, "handle_runout")
        lane._perform_pause_runout.assert_called_once_with()

    def test_check_runout_false_and_not_calibrating_sets_unloaded(self):
        lane = self._make()
        lane.unit_obj.check_runout.return_value = False
        lane.status = "None"
        lane.handle_load_runout(100.0, False)
        lane.set_unloaded.assert_called_once_with()

    def test_check_runout_false_and_calibrating_skips_set_unloaded(self):
        lane = self._make()
        lane.unit_obj.check_runout.return_value = False
        lane.status = "calibrating"
        lane.handle_load_runout(100.0, False)
        lane.set_unloaded.assert_not_called()

    def test_save_vars_called_after_processing(self):
        lane = self._make()
        lane.unit_obj.check_runout.return_value = False
        lane.handle_load_runout(100.0, False)
        lane.afc.save_vars.assert_called_once_with()


# ── handle_prep_runout — TTC (Timer Too Close) cross-lane guard ────────────────
#
# handle_prep_runout() reacts to the PREP switch returning to rest. Its
# set_unloaded() call changes lane state and updates the LED, which can collide
# with real-time step scheduling if a PREP cycle (this lane's own, or another
# lane's) is still actively driving a stepper — tripping Klipper's trsync
# watchdog and shutting the hub MCU down with "Timer too close". The guard
# below is scoped to that one branch only; the mid-print runout/pause branch
# must never be delayed by it (that's a safety-relevant path).

class TestHandlePrepRunout:
    def _make(self, prep_debounce_button=True):
        from tests.conftest import MockReactor
        lane = _make_afc_lane()
        lane.printer = MagicMock()
        lane.printer.state_message = "Printer is ready"
        lane._afc_prep_done = True
        lane.status = "None"
        lane.name = "lane1"
        lane.prep_state = False
        lane.prep_active = False
        lane.prep_recheck_pending = False
        lane.afc.current = "lane2"  # not this lane, so the mid-print branch's name check fails
        lane.afc.function.is_printing = MagicMock(return_value=False)
        lane._load_state = False
        lane.runout_lane = None
        lane.set_unloaded = MagicMock()
        lane._perform_infinite_runout = MagicMock()
        lane._perform_pause_runout = MagicMock()
        lane.afc.save_vars = MagicMock()
        # Real dict, not the MagicMock default — _any_lane_prep_active() iterates it.
        # Starts with just this lane, itself not active.
        lane.afc.lanes = {lane.name: lane}
        lane.afc.last_prep_activity_time = 0.0
        lane.fila_prep = MagicMock()
        lane.fila_prep.runout_helper.sensor_enabled = True
        # Deferred-recheck fix (Jimmy's review on PR #827): a blocked edge now schedules
        # a reactor callback instead of just returning, so tests need a reactor stand-in.
        lane.reactor = MockReactor()
        lane.reactor.register_callback = MagicMock()
        if prep_debounce_button:
            lane.prep_debounce_button = MagicMock()
        else:
            if hasattr(lane, "prep_debounce_button"):
                del lane.prep_debounce_button
        return lane

    def _add_other_active_lane(self, lane):
        """Add a second lane to lane.afc.lanes with prep_active=True."""
        lane.afc.lanes["lane2"] = MagicMock(prep_active=True)

    # -- _any_lane_prep_active() in isolation --

    def test_any_lane_prep_active_true_when_self_active(self):
        lane = self._make()
        lane.prep_active = True
        assert lane._any_lane_prep_active() is True

    def test_any_lane_prep_active_true_when_other_lane_active(self):
        lane = self._make()
        self._add_other_active_lane(lane)
        assert lane._any_lane_prep_active() is True

    def test_any_lane_prep_active_false_when_none_active(self):
        lane = self._make()
        assert lane._any_lane_prep_active() is False

    # -- debounce button forwarding (mirrors TestHandleLoadRunout) --

    def test_button_present_try_path_uses_keyword_form(self):
        lane = self._make()
        lane.handle_prep_runout(100.0, True)
        lane.prep_debounce_button._old_note_filament_present.assert_called_once_with(
            is_filament_present=True
        )

    def test_button_present_except_path_uses_positional_form(self):
        lane = self._make()
        lane.prep_debounce_button._old_note_filament_present.side_effect = [
            TypeError("old style"), None
        ]
        lane.handle_prep_runout(100.0, True)
        assert lane.prep_debounce_button._old_note_filament_present.call_args_list == [
            call(is_filament_present=True),
            call(100.0, True),
        ]

    def test_button_absent_skips_forwarding_entirely(self):
        lane = self._make(prep_debounce_button=False)
        # Should not raise despite there being no prep_debounce_button attribute.
        lane.handle_prep_runout(100.0, False)
        assert not hasattr(lane, "prep_debounce_button")

    # -- TTC guard: any-lane-active signal --

    def test_guard_blocks_set_unloaded_when_another_lane_active(self):
        lane = self._make()
        self._add_other_active_lane(lane)
        lane.afc.last_prep_activity_time = -100.0  # far in the past on its own
        lane.handle_prep_runout(100.0, False)
        lane.set_unloaded.assert_not_called()

    def test_guard_blocks_schedules_a_recheck_instead_of_dropping(self):
        """The deferred-recheck fix (Jimmy's review on PR #827): a blocked
        edge must not just vanish -- it has to be scheduled for a later
        re-evaluation, or a lane's runout can get permanently stuck stale."""
        lane = self._make()
        self._add_other_active_lane(lane)
        lane.handle_prep_runout(100.0, False)
        lane.reactor.register_callback.assert_called_once_with(
            lane._recheck_prep_runout, 100.0 + lane.PREP_GUARD_WINDOW
        )

    def test_guard_allows_set_unloaded_when_no_lane_active_and_outside_window(self):
        lane = self._make()
        lane.afc.last_prep_activity_time = 0.0  # eventtime=100.0 -> delta=100s
        lane.handle_prep_runout(100.0, False)
        lane.set_unloaded.assert_called_once_with()

    def test_guard_allows_does_not_schedule_a_recheck(self):
        lane = self._make()
        lane.afc.last_prep_activity_time = 0.0
        lane.handle_prep_runout(100.0, False)
        lane.reactor.register_callback.assert_not_called()

    # -- TTC guard: last_prep_activity_time window signal --

    def test_guard_blocks_set_unloaded_within_time_window(self):
        lane = self._make()
        lane.afc.last_prep_activity_time = 99.5  # delta=0.5s < 2.0s window
        lane.handle_prep_runout(100.0, False)
        lane.set_unloaded.assert_not_called()

    def test_guard_allows_set_unloaded_at_window_boundary(self):
        lane = self._make()
        lane.afc.last_prep_activity_time = 98.0  # delta=2.0s, not < 2.0
        lane.handle_prep_runout(100.0, False)
        lane.set_unloaded.assert_called_once_with()

    def test_guard_blocks_save_vars_too(self):
        lane = self._make()
        self._add_other_active_lane(lane)
        lane.handle_prep_runout(100.0, False)
        lane.afc.save_vars.assert_not_called()

    # -- TTC guard must never gate the mid-print runout/pause branch --

    def test_mid_print_pause_runout_never_gated_by_ttc_guard(self):
        lane = self._make()
        self._add_other_active_lane(lane)  # deliberately active, would normally gate
        lane.afc.last_prep_activity_time = 99.99  # deliberately inside the window too
        lane.afc.current = "lane1"
        lane.afc.function.is_printing = MagicMock(return_value=True)
        lane._load_state = True
        lane.runout_lane = None
        lane.handle_prep_runout(100.0, False)
        lane._perform_pause_runout.assert_called_once_with()
        lane.set_unloaded.assert_not_called()  # took the printing branch, not the unload one
        lane.reactor.register_callback.assert_not_called()  # never reaches the deferred-recheck logic

    def test_mid_print_infinite_runout_never_gated_by_ttc_guard(self):
        lane = self._make()
        self._add_other_active_lane(lane)
        lane.afc.last_prep_activity_time = 99.99
        lane.afc.current = "lane1"
        lane.afc.function.is_printing = MagicMock(return_value=True)
        lane._load_state = True
        lane.runout_lane = "lane3"
        lane.handle_prep_runout(100.0, False)
        lane._perform_infinite_runout.assert_called_once_with()

    # -- deferred recheck (Jimmy's review on PR #827: an edge silently dropped by
    # the guard could leave a lane's internal status stale forever -- e.g. eject
    # lane A without fully pulling the filament, insert into lane B, then finish
    # removing A's filament; B's activity kept A's release gated and it never got
    # reprocessed). _recheck_prep_runout() is the reactor callback scheduled above. --

    def test_recheck_does_nothing_if_filament_was_reinserted(self):
        lane = self._make()
        lane.prep_state = True  # filament is back -- the original release is moot
        lane._recheck_prep_runout(200.0)
        lane.set_unloaded.assert_not_called()
        lane.reactor.register_callback.assert_not_called()

    def test_recheck_defers_again_if_still_blocked_by_another_lane(self):
        lane = self._make()
        self._add_other_active_lane(lane)  # e.g. a different lane started a new cycle meanwhile
        lane._recheck_prep_runout(200.0)
        lane.set_unloaded.assert_not_called()
        # Rescheduled relative to wall-clock now (reactor.monotonic(), fixed at 100.0 by
        # MockReactor), not the eventtime argument -- matches the production code, which
        # has no reason to anchor off a timestamp from a possibly-stale deferred call.
        lane.reactor.register_callback.assert_called_once_with(
            lane._recheck_prep_runout, 100.0 + lane.PREP_GUARD_WINDOW
        )

    def test_recheck_defers_again_if_still_inside_time_window(self):
        lane = self._make()
        lane.afc.last_prep_activity_time = 199.5  # delta=0.5s < window
        lane._recheck_prep_runout(200.0)
        lane.set_unloaded.assert_not_called()
        lane.reactor.register_callback.assert_called_once()

    def test_recheck_proceeds_once_clear(self):
        lane = self._make()
        lane.afc.last_prep_activity_time = 0.0
        lane._recheck_prep_runout(200.0)
        lane.set_unloaded.assert_called_once_with()
        lane.afc.save_vars.assert_called_once_with()
        lane.reactor.register_callback.assert_not_called()

    def test_deferred_edge_actually_resolves_once_reactor_fires_the_recheck(self):
        """End-to-end: the callback handle_prep_runout schedules is the same
        one that eventually finishes the job -- not just each piece tested
        in isolation against hand-picked constants."""
        lane = self._make()
        self._add_other_active_lane(lane)
        lane.handle_prep_runout(100.0, False)
        lane.set_unloaded.assert_not_called()

        recheck_callback, recheck_time = lane.reactor.register_callback.call_args[0]
        lane.afc.lanes["lane2"].prep_active = False  # the other lane's cycle has since ended
        recheck_callback(recheck_time)

        lane.set_unloaded.assert_called_once_with()

    # -- outer gate / save_vars sanity (mirrors TestHandleLoadRunout) --

    def test_outer_gate_wrong_state_message_blocks_processing(self):
        lane = self._make()
        lane.printer.state_message = "Starting"
        lane.handle_prep_runout(100.0, False)
        lane.set_unloaded.assert_not_called()

    def test_save_vars_called_after_normal_processing(self):
        lane = self._make()
        lane.handle_prep_runout(100.0, False)
        lane.afc.save_vars.assert_called_once_with()


# ── __str__ ───────────────────────────────────────────────────────────────────

class TestAFCLaneStr:
    def test_str_returns_name(self):
        lane = _make_afc_lane("AFC_stepper lane1")
        assert str(lane) == "lane1"

    def test_str_matches_name_attribute(self):
        lane = _make_afc_lane("AFC_stepper myLane")
        assert str(lane) == lane.name


# ── material property ─────────────────────────────────────────────────────────

def _make_lane_with_afc(fullname="AFC_stepper lane1", density_values=None):
    """Build an AFCLane with a minimal MockAFC for material/density tests."""
    from tests.conftest import MockAFC
    lane = _make_afc_lane(fullname)
    lane.afc = MockAFC()
    lane.afc.common_density_values = density_values or [
        "PLA:1.24",
        "PETG:1.27",
        "ABS:1.04",
        "TPU:1.21",
    ]
    lane.filament_density = 1.24
    lane._material = None
    return lane


class TestAFCLaneMaterial:
    def test_getter_returns_material(self):
        lane = _make_lane_with_afc()
        lane._material = "PLA"
        assert lane.material == "PLA"

    def test_getter_returns_none_when_unset(self):
        lane = _make_lane_with_afc()
        lane._material = None
        assert lane.material is None

    def test_setter_stores_value(self):
        lane = _make_lane_with_afc()
        lane.material = "PETG"
        assert lane._material == "PETG"

    def test_setter_none_resets_density_to_default(self):
        lane = _make_lane_with_afc()
        lane.filament_density = 1.27
        lane.material = None
        assert lane.filament_density == 1.24

    def test_setter_empty_string_resets_density(self):
        lane = _make_lane_with_afc()
        lane.filament_density = 1.27
        lane.material = ""
        assert lane.filament_density == 1.24

    def test_setter_known_material_sets_density(self):
        lane = _make_lane_with_afc()
        lane.material = "PETG"
        assert lane.filament_density == 1.27

    def test_setter_abs_sets_correct_density(self):
        lane = _make_lane_with_afc()
        lane.material = "ABS"
        assert lane.filament_density == 1.04

    def test_setter_unknown_material_keeps_existing_density(self):
        lane = _make_lane_with_afc()
        lane.filament_density = 1.24
        lane.material = "NYLONX"  # not in density list
        assert lane.filament_density == 1.24

    def test_setter_partial_match(self):
        """'PLA+' should match 'PLA' entry."""
        lane = _make_lane_with_afc()
        lane.material = "PLA+"
        assert lane.filament_density == 1.24


# ── load_es property ──────────────────────────────────────────────────────────

class TestAFCLaneLoadEs:
    def test_only_lane_false_returns_load_homing_point(self):
        lane = _make_afc_lane()
        lane.only_lane = False
        assert lane.load_es == AFCHomingPoints.LOAD

    def test_only_lane_true_returns_endstop_name(self):
        lane = _make_afc_lane()
        lane.only_lane = True
        lane.endstops.update({AFCHomingPoints.LOAD: {"endstop" : "PIN123", "endstop_name": "lane1_load"}})
        assert lane.load_es == "lane1_load"

    def test_only_lane_true_unique_endstop_per_lane(self):
        lane_a = _make_afc_lane("AFC_stepper laneA")
        lane_a.only_lane = True
        lane_a.endstops.update({AFCHomingPoints.LOAD: {"endstop" : "PIN123", "endstop_name": "laneA_load"}})

        lane_b = _make_afc_lane("AFC_stepper laneB")
        lane_b.only_lane = True
        lane_b.endstops.update({AFCHomingPoints.LOAD: {"endstop" : "PIN123", "endstop_name": "laneB_load"}})

        assert lane_a.load_es != lane_b.load_es


# ── map_to_string ─────────────────────────────────────────────────────────────

class TestMapToString:
    def test_returns_none_when_map_is_empty(self):
        lane = _make_afc_lane()
        lane.map = []
        assert lane.map_to_string() == "NONE"

    def test_joins_single_entry(self):
        lane = _make_afc_lane()
        lane.map = ["T0"]
        assert lane.map_to_string() == "T0"

    def test_sorts_entries_naturally_low_to_high(self):
        lane = _make_afc_lane()
        lane.map = ["T10", "T2", "T0"]
        assert lane.map_to_string() == "T0, T2, T10"


# ── _map_to_string ────────────────────────────────────────────────────────────

class Test_MapToString:
    def test_joins_single_entry(self):
        lane = _make_afc_lane()
        lane._map = ["T0"]
        assert lane._map_to_string() == "T0"

    def test_sorts_entries_naturally_low_to_high(self):
        lane = _make_afc_lane()
        lane._map = ["T10", "T2", "T0"]
        assert lane._map_to_string() == "T0, T2, T10"

    def test_empty_list_returns_none_placeholder(self):
        lane = _make_afc_lane()
        lane._map = []
        assert lane._map_to_string() == "NONE"


# ── get_status: map sorting ───────────────────────────────────────────────────

def _make_lane_for_get_status(map_value):
    """Build a lane with just enough state for get_status() to run."""
    lane = _make_afc_lane()
    lane.connect_done = True
    lane.unit = "Turtle_1"
    lane.hub = "PB1"
    lane.afc_extruder_name = "extruder"
    lane.buffer_name = None
    lane.buffer_status = lambda: "None"
    lane.index = "0"
    lane.map = map_value
    lane.current_map = "T0"
    lane.prep_state = False
    lane._selector_state = None
    lane.tool_loaded = False
    lane.loaded_to_hub = False
    lane.material = ""
    lane.remember_spool = False
    lane.spool_id = None
    lane.color = ""
    lane.filament_name = ""
    lane.spool_vendor = ""
    lane.multi_color = []
    lane.weight = 0
    lane.extruder_temp = None
    lane.bed_temp = None
    lane.runout_lane = None
    lane.status = None
    lane.dist_hub = 900
    lane.td1_data = {}
    lane.afc.function.get_filament_status = MagicMock(return_value="a:b")
    return lane


class TestGetStatusMapSorting:
    def test_map_returns_raw_unsorted_list(self):
        """Unlike map_to_string(), the live (not save_to_file) branch returns
        self.map as-is -- other code relies on map[0] being the first
        assigned mapping, not the naturally-lowest one, so insertion order
        must be preserved here."""
        lane = _make_lane_for_get_status(["T10", "T2", "T0"])
        response = lane.get_status()
        assert response["map"] == ["T10", "T2", "T0"]
        assert lane.map == ["T10", "T2", "T0"]

    def test_save_to_file_uses_map_to_string_instead_of_raw_list(self):
        lane = _make_lane_for_get_status(["T10", "T2", "T0"])
        lane.td1_data = {"td": "", "color": "", "scan_time": ""}
        response = lane.get_status(save_to_file=True)
        assert response["map"] == "T0, T2, T10"


class TestAFCLaneIndexProperty:
    def test_lane_index_property_map_none(self):
        lane = _make_afc_lane()
        lane.current_map = None
        assert lane.lane_index == ""

    def test_lane_index_property_map_set(self):
        lane = _make_afc_lane()
        lane.current_map = "T5"
        assert lane.lane_index == "5"

    def test_lane_index_property_is_str(self):
        lane = _make_afc_lane()
        lane.current_map = "T5"
        assert isinstance(lane.lane_index, str)

class TestAFCLaneExtruderIndexProperty:
    def test_lane_extruder_index_property_is_int(self):
        lane = _make_afc_lane()
        lane.extruder_obj.th_extruder_name = lane.extruder_obj.name = "extruder1"
        assert isinstance(lane.lane_extruder_index, int)

    def test_lane_extruder_index_property_is_none(self):
        lane = _make_afc_lane()
        lane.extruder_obj = None
        assert isinstance(lane.lane_extruder_index, int)
    
    def test_lane_extruder_index_property_extruder(self):
        lane = _make_afc_lane()
        lane.extruder_obj.th_extruder_name = lane.extruder_obj.name = "extruder"
        assert lane.lane_extruder_index == 0
    
    def test_lane_extruder_index_property_extruder1(self):
        lane = _make_afc_lane()
        lane.extruder_obj.th_extruder_name = lane.extruder_obj.name = "extruder1"
        assert lane.lane_extruder_index == 1
    
    def test_lane_extruder_index_property_value_error(self):
        lane = _make_afc_lane()
        lane.extruder_obj.th_extruder_name = lane.extruder_obj.name = "extruderT"
        assert lane.lane_extruder_index == 0    

# ── get_color ─────────────────────────────────────────────────────────────────

class TestAFCLaneGetColor:
    def test_returns_color_when_no_td1_data(self):
        lane = _make_afc_lane()
        lane.color = "#FF0000"
        lane.td1_data = {}
        assert lane.get_color() == "#FF0000"

    def test_returns_td1_color_when_present(self):
        lane = _make_afc_lane()
        lane.color = "#FF0000"
        lane.td1_data = {"color": "00FF00"}
        assert lane.get_color() == "#00FF00"

    def test_none_color_with_no_td1(self):
        lane = _make_afc_lane()
        lane.color = None
        lane.td1_data = {}
        assert lane.get_color() is None

    def test_td1_color_overrides_manual_color(self):
        lane = _make_afc_lane()
        lane.color = "manual_color"
        lane.td1_data = {"color": "TD1Color"}
        assert lane.get_color() == "#TD1Color"


# ── get_active_assist ─────────────────────────────────────────────────────────

class TestAFCLaneGetActiveAssist:
    def test_no_returns_false(self):
        lane = _make_afc_lane()
        assert lane.get_active_assist(100.0, AssistActive.NO) is False

    def test_yes_returns_true(self):
        lane = _make_afc_lane()
        assert lane.get_active_assist(100.0, AssistActive.YES) is True

    def test_yes_returns_true_regardless_of_distance(self):
        lane = _make_afc_lane()
        assert lane.get_active_assist(5.0, AssistActive.YES) is True

    def test_dynamic_short_distance_returns_false(self):
        lane = _make_afc_lane()
        assert lane.get_active_assist(100.0, AssistActive.DYNAMIC) is False

    def test_dynamic_long_distance_returns_true(self):
        lane = _make_afc_lane()
        assert lane.get_active_assist(201.0, AssistActive.DYNAMIC) is True

    def test_dynamic_exactly_200_returns_false(self):
        """200 mm is NOT > 200, so dynamic assist is inactive."""
        lane = _make_afc_lane()
        assert lane.get_active_assist(200.0, AssistActive.DYNAMIC) is False

    def test_dynamic_negative_long_distance_returns_true(self):
        lane = _make_afc_lane()
        assert lane.get_active_assist(-250.0, AssistActive.DYNAMIC) is True


# ── calculate_effective_diameter ──────────────────────────────────────────────

def _make_lane_for_weight(name="lane1"):
    """Build a minimal AFCLane with weight/diameter attributes set."""
    lane = _make_afc_lane(f"AFC_stepper {name}")
    lane.filament_density = 1.24          # g/cm³ → g/1000 mm³
    lane.inner_diameter   = 75.0          # mm
    lane.outer_diameter   = 200.0         # mm
    lane.filament_diameter = 1.75         # mm
    lane.empty_spool_weight = 190.0       # g
    lane.max_motor_rpm    = 465.0
    lane.fwd_speed_multi  = 0.5
    lane.rwd_speed_multi  = 0.5
    lane.weight           = 1000.0        # g remaining filament
    return lane


class TestCalculateEffectiveDiameter:
    def test_returns_float(self):
        lane = _make_lane_for_weight()
        result = lane.calculate_effective_diameter(500)
        assert isinstance(result, float)

    def test_more_filament_gives_larger_diameter(self):
        lane = _make_lane_for_weight()
        d_small = lane.calculate_effective_diameter(100)
        d_large = lane.calculate_effective_diameter(1000)
        assert d_large > d_small

    def test_result_at_least_inner_diameter(self):
        """Effective spool outer diameter should always exceed the inner core."""
        lane = _make_lane_for_weight()
        result = lane.calculate_effective_diameter(1)
        assert result >= lane.inner_diameter

    def test_result_is_positive(self):
        lane = _make_lane_for_weight()
        assert lane.calculate_effective_diameter(250) > 0

    def test_wider_spool_gives_smaller_diameter(self):
        """Same mass spread over a wider spool → smaller outer diameter."""
        lane = _make_lane_for_weight()
        d_narrow = lane.calculate_effective_diameter(500, spool_width_mm=30)
        d_wide   = lane.calculate_effective_diameter(500, spool_width_mm=90)
        assert d_narrow > d_wide


# ── calculate_rpm ─────────────────────────────────────────────────────────────

class TestCalculateRpm:
    def test_returns_float(self):
        lane = _make_lane_for_weight()
        assert isinstance(lane.calculate_rpm(50.0), float)

    def test_higher_feed_rate_gives_higher_rpm(self):
        lane = _make_lane_for_weight()
        rpm_low  = lane.calculate_rpm(10.0)
        rpm_high = lane.calculate_rpm(100.0)
        assert rpm_high > rpm_low

    def test_rpm_clamped_to_max_motor_rpm(self):
        lane = _make_lane_for_weight()
        # Very high feed rate should be clamped
        rpm = lane.calculate_rpm(10_000.0)
        assert rpm == lane.max_motor_rpm

    def test_rpm_positive_for_positive_feed(self):
        lane = _make_lane_for_weight()
        assert lane.calculate_rpm(50.0) > 0


# ── calculate_pwm_value ───────────────────────────────────────────────────────

class TestCalculatePwmValue:
    def test_returns_value_between_0_and_1(self):
        lane = _make_lane_for_weight()
        pwm = lane.calculate_pwm_value(50.0)
        assert 0.0 <= pwm <= 1.0

    def test_higher_feed_rate_gives_higher_pwm(self):
        lane = _make_lane_for_weight()
        pwm_low  = lane.calculate_pwm_value(10.0)
        pwm_high = lane.calculate_pwm_value(200.0)
        assert pwm_high >= pwm_low

    def test_rewind_flag_changes_pwm(self):
        lane = _make_lane_for_weight()
        pwm_fwd = lane.calculate_pwm_value(50.0, rewind=False)
        pwm_rwd = lane.calculate_pwm_value(50.0, rewind=True)
        # They use different formulas; the values should differ
        assert pwm_fwd != pwm_rwd

    def test_clamped_at_zero(self):
        """PWM should never be negative."""
        lane = _make_lane_for_weight()
        assert lane.calculate_pwm_value(0.0) >= 0.0

    def test_clamped_at_one(self):
        """PWM should never exceed 1.0."""
        lane = _make_lane_for_weight()
        assert lane.calculate_pwm_value(100_000.0) <= 1.0


# ── update_remaining_weight ───────────────────────────────────────────────────

import math as _math


class TestUpdateRemainingWeight:
    def test_weight_decreases_after_move(self):
        lane = _make_lane_for_weight()
        lane.weight = 500.0
        lane.update_remaining_weight(100.0)
        assert lane.weight < 500.0

    def test_weight_does_not_go_negative(self):
        lane = _make_lane_for_weight()
        lane.weight = 0.1
        lane.update_remaining_weight(1000.0)
        assert lane.weight == 0.0

    def test_zero_distance_no_weight_change(self):
        lane = _make_lane_for_weight()
        lane.weight = 500.0
        lane.update_remaining_weight(0.0)
        assert lane.weight == 500.0

    def test_weight_change_matches_formula(self):
        lane = _make_lane_for_weight()
        lane.weight = 500.0
        dist = 100.0
        expected_vol = _math.pi * (lane.filament_diameter / 2) ** 2 * dist
        expected_change = expected_vol * lane.filament_density / 1000.0
        lane.update_remaining_weight(dist)
        assert abs(lane.weight - (500.0 - expected_change)) < 1e-9

    def test_larger_diameter_removes_more_weight(self):
        lane1 = _make_lane_for_weight()
        lane1.filament_diameter = 1.75
        lane1.weight = 500.0

        lane2 = _make_lane_for_weight()
        lane2.filament_diameter = 2.85
        lane2.weight = 500.0

        lane1.update_remaining_weight(100.0)
        lane2.update_remaining_weight(100.0)

        assert lane2.weight < lane1.weight


# ── _is_normal_printing_state ─────────────────────────────────────────────────

class TestIsNormalPrintingState:
    def _make_lane(self, status, in_toolchange=False):
        lane = _make_lane_with_afc()
        lane.status = status
        lane.afc.in_toolchange = in_toolchange
        return lane

    def test_tooled_state_returns_true(self):
        from extras.AFC_lane import AFCLaneState
        lane = self._make_lane(AFCLaneState.TOOLED)
        assert lane._is_normal_printing_state() is True

    def test_loaded_state_returns_true(self):
        from extras.AFC_lane import AFCLaneState
        lane = self._make_lane(AFCLaneState.LOADED)
        assert lane._is_normal_printing_state() is True

    def test_error_state_returns_false(self):
        from extras.AFC_lane import AFCLaneState
        lane = self._make_lane(AFCLaneState.ERROR)
        assert lane._is_normal_printing_state() is False

    def test_none_state_returns_false(self):
        from extras.AFC_lane import AFCLaneState
        lane = self._make_lane(AFCLaneState.NONE)
        assert lane._is_normal_printing_state() is False


# ── send_lane_data / clear_lane_data ──────────────────────────────────────────

def _make_lane_for_moonraker(extruder_name="extruder", map_value="T0"):
    """Build a minimal AFCLane wired up for send/clear lane data tests."""
    lane = _make_afc_lane("AFC_stepper lane1")
    lane.map = [map_value] if map_value is not None else []
    lane._sent_lane_data_keys = []
    lane.afc.tool_cmds = {}
    lane.extruder_obj = MagicMock()
    lane.extruder_obj.th_extruder_name = lane.extruder_obj.name = extruder_name
    lane.color = "#FF0000"
    lane._material = "PLA"
    lane.bed_temp = 60
    lane.extruder_temp = 210
    lane.td1_data = {"scan_time": "1.23", "td": "0.95"}
    lane.spool_id = "abc123"
    lane.weight = 750.0
    lane.spool_vendor = "Overture"
    lane.filament_name = "Overture Gray ASA"
    lane.espooler.espooler_values.full_weight = 1000
    return lane


def _get_sent_payload(lane):
    """Call send_lane_data and return the single payload passed to moonraker."""
    lane.send_lane_data()
    lane.afc.moonraker.send_lane_data.assert_called_once()
    return lane.afc.moonraker.send_lane_data.call_args[0][0]


def _get_cleared_payload(lane):
    """Call clear_lane_data and return the single payload passed to moonraker."""
    lane.clear_lane_data()
    lane.afc.moonraker.send_lane_data.assert_called_once()
    return lane.afc.moonraker.send_lane_data.call_args[0][0]


class TestGetStatusSpoolVendor:
    def test_spool_vendor_reported(self):
        lane = _make_lane_for_get_status(["T0"])
        lane.spool_vendor = "Overture"
        response = lane.get_status()
        assert response["spool_vendor"] == "Overture"

    def test_spool_vendor_tracks_the_attribute(self):
        lane = _make_lane_for_get_status(["T0"])
        lane.spool_vendor = ""
        response = lane.get_status()
        assert response["spool_vendor"] == ""

    def test_spool_vendor_omitted_when_saving_to_file(self):
        """Vendor is re-fetched from Spoolman, so it is not persisted -- the
        same treatment filament_name already gets."""
        lane = _make_lane_for_get_status(["T0"])
        lane.spool_vendor = "Overture"
        lane.td1_data = {"td": "", "color": "", "scan_time": ""}
        response = lane.get_status(save_to_file=True)
        assert "spool_vendor" not in response
        assert "filament_name" not in response


class TestSendLaneDataExtruderIndex:
    @pytest.mark.parametrize("extruder_name,expected_index", [
        ("extruder",  0),
        ("extruder1", 1),
        ("extruder2", 2),
        ("extruder3", 3),
    ])
    def test_extruder_index_derivation(self, extruder_name, expected_index):
        lane = _make_lane_for_moonraker(extruder_name=extruder_name, map_value=f"T{expected_index}")
        payload = _get_sent_payload(lane)
        assert payload["value"]["extruder_index"] == expected_index

    def test_extruder_index_is_int(self):
        lane = _make_lane_for_moonraker(extruder_name="extruder1", map_value="T1")
        payload = _get_sent_payload(lane)
        assert isinstance(payload["value"]["extruder_index"], int)

    def test_multiple_lanes_same_extruder_same_index(self):
        lane_a = _make_lane_for_moonraker(extruder_name="extruder", map_value="T0")
        lane_b = _make_lane_for_moonraker(extruder_name="extruder", map_value="T1")
        payload_a = _get_sent_payload(lane_a)
        payload_b = _get_sent_payload(lane_b)
        assert payload_a["value"]["extruder_index"] == payload_b["value"]["extruder_index"]

    def test_payload_namespace_is_lane_data(self):
        lane = _make_lane_for_moonraker()
        payload = _get_sent_payload(lane)
        assert payload["namespace"] == "lane_data"

    def test_key_is_the_t_command_not_the_lane_name(self):
        """The record's "key" is now the T(n) mapping itself, not the lane
        name -- see test_multiple_mappings_send_one_record_each for why."""
        lane = _make_lane_for_moonraker(map_value="T2")
        payload = _get_sent_payload(lane)
        assert payload["key"] == "T2"
        assert payload["value"]["lane"] == "2"

    def test_weight_is_included(self):
        lane = _make_lane_for_moonraker()
        payload = _get_sent_payload(lane)
        assert payload["value"]["weight"] == 750.0

    def test_vendor_published_as_vendor_name(self):
        """`lane_data` is a shared namespace with an established key for this
        value, so the record uses that key rather than the lane attribute's."""
        lane = _make_lane_for_moonraker()
        payload = _get_sent_payload(lane)
        assert payload["value"]["vendor_name"] == "Overture"
        assert "spool_vendor" not in payload["value"]
        assert "vendor" not in payload["value"]

    def test_filament_name_published_as_name(self):
        """Same shared-namespace key rule as the vendor above."""
        lane = _make_lane_for_moonraker()
        payload = _get_sent_payload(lane)
        assert payload["value"]["name"] == "Overture Gray ASA"
        assert "filament_name" not in payload["value"]

    def test_initial_weight_is_included(self):
        lane = _make_lane_for_moonraker()
        payload = _get_sent_payload(lane)
        assert payload["value"]["initial_weight"] == 1000

    def test_vendor_and_name_empty_for_unlinked_lane(self):
        """A lane with no Spoolman link publishes the keys as empty strings
        rather than omitting them."""
        lane = _make_lane_for_moonraker()
        lane.spool_vendor = ""
        lane.filament_name = ""
        payload = _get_sent_payload(lane)
        assert payload["value"]["vendor_name"] == ""
        assert payload["value"]["name"] == ""

    def test_no_send_when_map_is_empty(self):
        lane = _make_lane_for_moonraker(map_value=None)
        lane.send_lane_data()
        lane.afc.moonraker.send_lane_data.assert_not_called()

    def test_no_send_when_map_is_none_placeholder(self):
        lane = _make_lane_for_moonraker(map_value="NONE")
        lane.send_lane_data()
        lane.afc.moonraker.send_lane_data.assert_not_called()

    def test_no_send_when_moonraker_is_falsy(self):
        """Proven independently of map: a valid map alone isn't enough
        without a moonraker connection."""
        lane = _make_lane_for_moonraker()
        lane.afc.moonraker = None
        lane.send_lane_data()
        # afc.moonraker is None here, so there's nothing to assert the call
        # against -- the AttributeError that not-called would otherwise
        # crash into is exactly what the guard is meant to prevent.

    def test_multiple_mappings_send_one_record_each(self):
        lane = _make_lane_for_moonraker(map_value="T0")
        lane.map = ["T0", "T3"]
        lane.send_lane_data()
        calls = lane.afc.moonraker.send_lane_data.call_args_list
        assert len(calls) == 2
        keys = {c.args[0]["key"] for c in calls}
        assert keys == {"T0", "T3"}
        lane_fields = {c.args[0]["value"]["lane"] for c in calls}
        assert lane_fields == {"0", "3"}

    def test_multiple_mappings_share_same_spool_data(self):
        lane = _make_lane_for_moonraker(map_value="T0")
        lane.map = ["T0", "T3"]
        lane.send_lane_data()
        colors = {c.args[0]["value"]["color"] for c in lane.afc.moonraker.send_lane_data.call_args_list}
        assert colors == {"#FF0000"}

    def test_updates_sent_keys_after_send(self):
        lane = _make_lane_for_moonraker(map_value="T0")
        lane.map = ["T0", "T3"]
        lane.send_lane_data()
        assert lane._sent_lane_data_keys == ["T0", "T3"]

    def test_removes_stale_key_no_longer_owned_by_anyone(self):
        """A T(n) that was mapped last call but isn't anymore, and isn't in
        any lane's own map list, gets its record removed."""
        lane = _make_lane_for_moonraker(map_value="T0")
        lane._sent_lane_data_keys = ["T0", "T3"]
        lane.afc.lanes = {lane.name: lane}
        lane.send_lane_data()
        lane.afc.moonraker.remove_database_entry.assert_called_once_with("lane_data", "T3")

    def test_does_not_remove_stale_key_reassigned_to_another_lane(self):
        """A T(n) that moved to a different lane's own map (e.g. during a
        swap) must not be removed -- the new owner's own send_lane_data()
        call is responsible for that key's record."""
        lane = _make_lane_for_moonraker(map_value="T0")
        lane._sent_lane_data_keys = ["T0", "T3"]
        other_lane = MagicMock()
        other_lane.map = ["T3"]
        lane.afc.lanes = {lane.name: lane, "lane2": other_lane}
        lane.send_lane_data()
        lane.afc.moonraker.remove_database_entry.assert_not_called()

    def test_no_stale_removal_when_key_still_mapped(self):
        """Covers the `key not in current_keys` half of the removal
        condition being falsy on its own."""
        lane = _make_lane_for_moonraker(map_value="T0")
        lane._sent_lane_data_keys = ["T0"]
        lane.send_lane_data()
        lane.afc.moonraker.remove_database_entry.assert_not_called()


class TestClearLaneDataExtruderIndex:
    @pytest.mark.parametrize("extruder_name,expected_index", [
        ("extruder",  0),
        ("extruder1", 1),
        ("extruder2", 2),
        ("extruder3", 3),
    ])
    def test_extruder_index_derivation(self, extruder_name, expected_index):
        lane = _make_lane_for_moonraker(extruder_name=extruder_name, map_value=f"T{expected_index}")
        payload = _get_cleared_payload(lane)
        assert payload["value"]["extruder_index"] == expected_index

    def test_extruder_index_is_int(self):
        lane = _make_lane_for_moonraker(extruder_name="extruder1", map_value="T1")
        payload = _get_cleared_payload(lane)
        assert isinstance(payload["value"]["extruder_index"], int)

    def test_key_is_the_t_command(self):
        lane = _make_lane_for_moonraker(map_value="T2")
        payload = _get_cleared_payload(lane)
        assert payload["key"] == "T2"
        assert payload["value"]["lane"] == "2"

    def test_color_is_cleared(self):
        lane = _make_lane_for_moonraker()
        payload = _get_cleared_payload(lane)
        assert payload["value"]["color"] == ""

    def test_weight_is_cleared(self):
        lane = _make_lane_for_moonraker()
        payload = _get_cleared_payload(lane)
        assert payload["value"]["weight"] == 0

    def test_vendor_and_name_are_cleared(self):
        """Cleared independently of the lane attributes, which AFC_spool resets
        after the push rather than before it."""
        lane = _make_lane_for_moonraker()
        payload = _get_cleared_payload(lane)
        assert payload["value"]["vendor_name"] == ""
        assert payload["value"]["name"] == ""

    def test_initial_weight_is_cleared(self):
        lane = _make_lane_for_moonraker()
        payload = _get_cleared_payload(lane)
        assert payload["value"]["initial_weight"] == 0

    def test_clears_one_record_per_mapping(self):
        lane = _make_lane_for_moonraker(map_value="T0")
        lane.map = ["T0", "T3"]
        lane.clear_lane_data()
        calls = lane.afc.moonraker.send_lane_data.call_args_list
        assert len(calls) == 2
        keys = {c.args[0]["key"] for c in calls}
        assert keys == {"T0", "T3"}

    def test_updates_sent_keys_after_clear(self):
        lane = _make_lane_for_moonraker(map_value="T0")
        lane.map = ["T0", "T3"]
        lane.clear_lane_data()
        assert lane._sent_lane_data_keys == ["T0", "T3"]

    def test_no_clear_when_map_is_empty(self):
        lane = _make_lane_for_moonraker(map_value=None)
        lane.clear_lane_data()
        lane.afc.moonraker.send_lane_data.assert_not_called()

    def test_no_clear_when_moonraker_is_falsy(self):
        lane = _make_lane_for_moonraker()
        lane.afc.moonraker = None
        lane.clear_lane_data()
        # afc.moonraker is None here; not raising AttributeError proves the
        # guard short-circuited before reaching moonraker.send_lane_data(...).


# ── _is_normal_printing_state (continued) ─────────────────────────────────────

class TestIsNormalPrintingStateContinued:
    def _make_lane(self, status, in_toolchange=False):
        lane = _make_lane_with_afc()
        lane.status = status
        lane.afc.in_toolchange = in_toolchange
        return lane

    def test_in_toolchange_returns_false_even_if_tooled(self):
        from extras.AFC_lane import AFCLaneState
        lane = self._make_lane(AFCLaneState.TOOLED, in_toolchange=True)
        assert lane._is_normal_printing_state() is False

    def test_tool_loading_state_returns_false(self):
        from extras.AFC_lane import AFCLaneState
        lane = self._make_lane(AFCLaneState.TOOL_LOADING)
        assert lane._is_normal_printing_state() is False


# ── set_afc_prep_done ─────────────────────────────────────────────────────────

class TestSetAfcPrepDone:
    def test_initially_false(self):
        lane = _make_afc_lane()
        lane._afc_prep_done = False
        assert lane._afc_prep_done is False

    def test_set_afc_prep_done_sets_flag(self):
        lane = _make_afc_lane()
        lane._afc_prep_done = False
        lane.set_afc_prep_done()
        assert lane._afc_prep_done is True

    def test_set_afc_prep_done_idempotent(self):
        lane = _make_afc_lane()
        lane._afc_prep_done = True
        lane.set_afc_prep_done()
        assert lane._afc_prep_done is True


# ── load_callback ─────────────────────────────────────────────────────────────

class TestLoadCallback:
    def test_stores_load_state(self):
        lane = _make_afc_lane()
        lane._load_state = False
        lane.unit_obj = MagicMock()
        lane.unit_obj.type = "BoxTurtle"
        lane.printer = MagicMock()
        lane.printer.state_message = "Starting"
        lane.load_callback(100.0, True)
        assert lane._load_state is True

    def test_htlf_sets_prep_state(self):
        lane = _make_afc_lane()
        lane._load_state = False
        lane.prep_state = False
        lane.unit_obj = MagicMock()
        lane.unit_obj.type = "HTLF"
        lane.printer = MagicMock()
        lane.printer.state_message = "Printer is ready"
        lane.load_callback(100.0, True)
        assert lane.prep_state is True

    def test_non_htlf_does_not_set_prep_state(self):
        lane = _make_afc_lane()
        lane._load_state = False
        lane.prep_state = False
        lane.unit_obj = MagicMock()
        lane.unit_obj.type = "BoxTurtle"
        lane.printer = MagicMock()
        lane.printer.state_message = "Printer is ready"
        lane.load_callback(100.0, True)
        assert lane.prep_state is False

# ── move_to ───────────────────────────────────────────────────────────────────

class TestMoveTo:
    def test_no_drive_stepper_no_extruder_stepper(self):
        lane = _make_afc_lane()
        lane.drive_stepper = None
        lane.extruder_stepper = None
        lane.extruder_obj.is_standalone.return_value=False
        homed, dist, warn = lane.move_to(100, SpeedMode.SHORT, AFCHomingPoints.BUFFER,
                                          False, False)
        assert homed == False
        assert dist == 0
        assert warn == AFCMoveWarning.ERROR
    
    def test_no_drive_stepper_no_extruder_stepper_standalone_extruder(self):
        lane = _make_afc_lane()
        lane.drive_stepper = None
        lane.extruder_stepper = None
        lane.extruder_obj.is_standalone.return_value=True
        homed, dist, warn = lane.move_to(100, SpeedMode.SHORT, AFCHomingPoints.BUFFER,
                                          False, False)
        assert homed == True
        assert dist == 0
        assert warn == AFCMoveWarning.NONE
    
    def test_drive_stepper_no_extruder_stepper_no_homing(self):
        lane = _make_afc_lane()
        lane.drive_stepper = AFCExtruderStepper.__new__(AFCExtruderStepper)
        lane.move_advanced = MagicMock()
        homed, dist, warn = lane.move_to(100, SpeedMode.SHORT, AFCHomingPoints.BUFFER,
                                          False, False)
        assert homed == True
        assert dist == 0
        assert warn == AFCMoveWarning.NONE
    
    def test_drive_stepper_no_extruder_stepper_homing_pos_movement(self):
        HOMING_OVERSHOOT = 50
        HOMING_DELTA = 300
        MOVE_DISTANCE = 1000
        ASSIST_ACTIVE = AssistActive.NO
        HOMING = True
        lane = _make_afc_lane()
        lane.homing_overshoot = HOMING_OVERSHOOT
        lane.homing_delta = HOMING_DELTA
        lane.drive_stepper = AFCExtruderStepper.__new__(AFCExtruderStepper)
        home_to = MagicMock()
        lane.drive_stepper.home_to = home_to
        lane.move_advanced = MagicMock()
        home_to.return_value = (True, MOVE_DISTANCE, False)
        homed, dist, warn = lane.move_to(MOVE_DISTANCE, SpeedMode.SHORT, AFCHomingPoints.BUFFER,
                                         ASSIST_ACTIVE, HOMING)
        call_args = home_to.call_args[0]
        kwargs = home_to.call_args[1]
        assert homed == True
        assert dist == abs(MOVE_DISTANCE)
        assert warn == AFCMoveWarning.NONE
        assert call_args[0] == AFCHomingPoints.BUFFER
        assert call_args[1] == MOVE_DISTANCE + HOMING_OVERSHOOT
        assert call_args[2] == lane.short_moves_speed
        assert call_args[3] == lane.short_moves_accel
        assert call_args[4] == bool(MOVE_DISTANCE > 0)
        assert kwargs["assist_active"] == False
    
    def test_drive_stepper_no_extruder_stepper_homing_neg_movement(self):
        HOMING_OVERSHOOT = 50
        HOMING_DELTA = 300
        MOVE_DISTANCE = -1000
        ASSIST_ACTIVE = AssistActive.NO
        HOMING = True
        lane = _make_afc_lane()
        lane.homing_overshoot = HOMING_OVERSHOOT
        lane.homing_delta = HOMING_DELTA
        lane.drive_stepper = AFCExtruderStepper.__new__(AFCExtruderStepper)
        home_to = MagicMock()
        lane.drive_stepper.home_to = home_to
        lane.move_advanced = MagicMock()
        home_to.return_value = (True, abs(MOVE_DISTANCE), False)
        homed, dist, warn = lane.move_to(MOVE_DISTANCE, SpeedMode.SHORT, AFCHomingPoints.BUFFER,
                                         ASSIST_ACTIVE, HOMING)
        call_args = home_to.call_args[0]
        kwargs = home_to.call_args[1]
        assert homed == True
        assert dist == abs(MOVE_DISTANCE)
        assert warn == AFCMoveWarning.NONE
        assert call_args[0] == AFCHomingPoints.BUFFER
        assert call_args[1] == MOVE_DISTANCE - HOMING_OVERSHOOT
        assert call_args[2] == lane.short_moves_speed
        assert call_args[3] == lane.short_moves_accel
        assert call_args[4] == bool(MOVE_DISTANCE > 0)
        assert kwargs["assist_active"] == False

    def test_drive_stepper_no_extruder_stepper_homing_neg_movement_homing_error(self):
        HOMING_OVERSHOOT = 50
        HOMING_DELTA = 300
        MOVE_DISTANCE = -1000
        ASSIST_ACTIVE = AssistActive.NO
        HOMING = True
        lane = _make_afc_lane()
        lane.homing_overshoot = HOMING_OVERSHOOT
        lane.homing_delta = HOMING_DELTA
        lane.drive_stepper = AFCExtruderStepper.__new__(AFCExtruderStepper)
        home_to = MagicMock()
        lane.drive_stepper.home_to = home_to
        lane.move_advanced = MagicMock()
        home_to.return_value = (False, 0, True)
        homed, dist, warn = lane.move_to(MOVE_DISTANCE, SpeedMode.SHORT, AFCHomingPoints.BUFFER,
                                         ASSIST_ACTIVE, HOMING)
        call_args = home_to.call_args[0]
        kwargs = home_to.call_args[1]
        assert homed == False
        assert dist == 0
        assert warn == AFCMoveWarning.ERROR
        assert call_args[0] == AFCHomingPoints.BUFFER
        assert call_args[1] == MOVE_DISTANCE - HOMING_OVERSHOOT
        assert call_args[2] == lane.short_moves_speed
        assert call_args[3] == lane.short_moves_accel
        assert call_args[4] == bool(MOVE_DISTANCE > 0)
        assert kwargs["assist_active"] == False

    def test_drive_stepper_no_extruder_stepper_homing_pos_movement_short(self):
        HOMING_OVERSHOOT = 50
        HOMING_DELTA = 300
        MOVE_DISTANCE = 1000
        ASSIST_ACTIVE = AssistActive.NO
        HOMING = True
        MOVE_SHORT = 300
        lane = _make_afc_lane()
        lane.homing_overshoot = HOMING_OVERSHOOT
        lane.homing_delta = HOMING_DELTA
        lane.drive_stepper = AFCExtruderStepper.__new__(AFCExtruderStepper)
        home_to = MagicMock()
        lane.drive_stepper.home_to = home_to
        lane.move_advanced = MagicMock()
        home_to.return_value = (False, MOVE_SHORT, False)
        homed, dist, warn = lane.move_to(MOVE_DISTANCE, SpeedMode.SHORT, AFCHomingPoints.BUFFER,
                                         ASSIST_ACTIVE, HOMING)
        call_args = home_to.call_args[0]
        kwargs = home_to.call_args[1]
        assert homed == False
        assert dist == MOVE_SHORT
        assert warn == AFCMoveWarning.WARN
        assert call_args[0] == AFCHomingPoints.BUFFER
        assert call_args[1] == MOVE_DISTANCE + HOMING_OVERSHOOT
        assert call_args[2] == lane.short_moves_speed
        assert call_args[3] == lane.short_moves_accel
        assert call_args[4] == HOMING
        assert kwargs["assist_active"] == False
    
    def test_extruder_stepper_no_drive_stepper_homing_pos_movement_short(self):
        HOMING_OVERSHOOT = 50
        HOMING_DELTA = 300
        MOVE_DISTANCE = 1000
        ASSIST_ACTIVE = AssistActive.NO
        HOMING = True
        MOVE_SHORT = 300
        lane = _make_afc_lane()
        lane.homing_overshoot = HOMING_OVERSHOOT
        lane.homing_delta = HOMING_DELTA
        lane.extruder_stepper = AFCExtruderStepper.__new__(AFCExtruderStepper)
        home_to = MagicMock()
        lane.home_to = home_to
        lane.move_advanced = MagicMock()
        home_to.return_value = (False, MOVE_SHORT, False)
        homed, dist, warn = lane.move_to(MOVE_DISTANCE, SpeedMode.SHORT, AFCHomingPoints.BUFFER,
                                         ASSIST_ACTIVE, HOMING)
        call_args = home_to.call_args[0]
        kwargs = home_to.call_args[1]
        assert homed == False
        assert dist == MOVE_SHORT
        assert warn == AFCMoveWarning.WARN
        assert call_args[0] == AFCHomingPoints.BUFFER
        assert call_args[1] == MOVE_DISTANCE + HOMING_OVERSHOOT
        assert call_args[2] == lane.short_moves_speed
        assert call_args[3] == lane.short_moves_accel
        assert call_args[4] == HOMING
        assert kwargs["assist_active"] == False


# ── Auto Spool Switch ────────────────────────────────────────────────────────

def _make_lane_for_auto_switch(weight=20.0, threshold=25.0, enabled=True,
                                current=True, printing=True,
                                error_state=False, runout_lane="lane2"):
    """Build an AFCLane configured for auto spool switch testing."""
    from tests.conftest import MockAFC, MockLogger, MockReactor
    lane = _make_afc_lane("AFC_stepper lane1")
    lane.afc = MockAFC()
    lane.afc.auto_spool_switch = enabled
    lane.afc.auto_spool_switch_threshold = threshold
    lane.afc.current = "lane1" if current else "other_lane"
    lane.afc.error_state = error_state
    lane.afc.function.is_printing.return_value = printing
    lane.afc.function.get_extruder_pos.return_value = 100.0
    lane.reactor = MockReactor()
    lane.reactor.register_callback = MagicMock()
    lane.logger = MockLogger()
    lane.weight = weight
    lane.auto_switch_triggered = False
    lane.filament_diameter = 1.75
    lane.filament_density = 1.24
    lane.past_extruder_position = 50.0
    lane.save_counter = 0
    lane.UPDATE_WEIGHT_DELAY = 10.0
    lane.runout_lane = runout_lane
    lane._perform_infinite_runout = MagicMock()
    lane._perform_pause_runout = MagicMock()
    return lane


class TestAutoSpoolSwitchWeightCheck:
    """Tests for the weight threshold check in update_weight_callback."""

    def test_auto_switch_not_triggered_when_disabled(self):
        lane = _make_lane_for_auto_switch(weight=20.0, enabled=False)
        lane.update_weight_callback(None)
        lane.reactor.register_callback.assert_not_called()
        assert lane.auto_switch_triggered is False

    def testauto_switch_triggered_at_threshold(self):
        lane = _make_lane_for_auto_switch(weight=25.5, threshold=25.0)
        lane.afc.function.get_extruder_pos.return_value = 100.0
        lane.past_extruder_position = -400.0  # 500mm delta to push weight below threshold
        lane.update_weight_callback(None)
        assert lane.auto_switch_triggered is True
        lane.reactor.register_callback.assert_called_once()

    def test_auto_switch_not_triggered_above_threshold(self):
        lane = _make_lane_for_auto_switch(weight=500.0, threshold=25.0)
        lane.update_weight_callback(None)
        lane.reactor.register_callback.assert_not_called()
        assert lane.auto_switch_triggered is False

    def test_auto_switch_debounce_prevents_second_trigger(self):
        lane = _make_lane_for_auto_switch(weight=10.0, threshold=25.0)
        lane.auto_switch_triggered = True
        lane.update_weight_callback(None)
        lane.reactor.register_callback.assert_not_called()

    def test_auto_switch_not_triggered_when_not_current(self):
        lane = _make_lane_for_auto_switch(weight=10.0, current=False)
        lane.update_weight_callback(None)
        lane.reactor.register_callback.assert_not_called()

    def test_auto_switch_not_triggered_when_not_printing(self):
        lane = _make_lane_for_auto_switch(weight=10.0, printing=False)
        lane.update_weight_callback(None)
        lane.reactor.register_callback.assert_not_called()

    def test_auto_switch_not_triggered_when_error_state(self):
        lane = _make_lane_for_auto_switch(weight=10.0, error_state=True)
        lane.update_weight_callback(None)
        lane.reactor.register_callback.assert_not_called()

    def test_auto_switch_not_triggered_when_weight_zero(self):
        lane = _make_lane_for_auto_switch(weight=0.0, threshold=25.0)
        lane.update_weight_callback(None)
        lane.reactor.register_callback.assert_not_called()

    def test_auto_switch_logs_message(self):
        lane = _make_lane_for_auto_switch(weight=10.0, threshold=25.0)
        lane.afc.function.get_extruder_pos.return_value = 51.0
        lane.update_weight_callback(None)
        assert lane.auto_switch_triggered is True
        log_messages = [msg for level, msg in lane.logger.messages if level == "info"]
        assert any("Auto spool switch" in msg and "threshold" in msg for msg in log_messages)


class TestHandleAutoSpoolSwitch:
    """Tests for _handle_auto_spool_switch method."""

    def test_calls_infinite_runout_when_runout_lane_set(self):
        lane = _make_lane_for_auto_switch(runout_lane="lane2")
        lane._handle_auto_spool_switch()
        lane._perform_infinite_runout.assert_called_once()
        lane._perform_pause_runout.assert_not_called()

    def test_calls_pause_runout_when_no_runout_lane(self):
        lane = _make_lane_for_auto_switch(runout_lane=None)
        lane._handle_auto_spool_switch()
        lane._perform_pause_runout.assert_called_once()
        lane._perform_infinite_runout.assert_not_called()

    def test_skips_when_error_state(self):
        lane = _make_lane_for_auto_switch(error_state=True, runout_lane="lane2")
        lane._handle_auto_spool_switch()
        lane._perform_infinite_runout.assert_not_called()
        lane._perform_pause_runout.assert_not_called()

    def test_skips_when_not_printing(self):
        lane = _make_lane_for_auto_switch(printing=False, runout_lane="lane2")
        lane._handle_auto_spool_switch()
        lane._perform_infinite_runout.assert_not_called()
        lane._perform_pause_runout.assert_not_called()


# ── Pause Runout ─────────────────────────────────────────────────────────────

def _make_lane_for_pause_runout(auto_switch_triggered=False, unload_on_runout=False):
    lane = _make_afc_lane("AFC_stepper lane1")
    lane.afc = MagicMock()
    lane.afc.error_state = False
    lane.unit_obj = MagicMock()
    lane.unit_obj.unload_on_runout = unload_on_runout
    lane.auto_switch_triggered = auto_switch_triggered
    return lane


class TestPerformPauseRunout:

    def test_msg_shows_weight_based_when_auto_switch_triggered(self):
        lane = _make_lane_for_pause_runout(auto_switch_triggered=True)
        lane._perform_pause_runout()
        msg = lane.afc.error.AFC_error.call_args[0][0]
        print(msg)
        assert "Minimum weight" in msg
        assert lane.name in msg

    def test_msg_shows_runout_when_not_auto_switch_triggered(self):
        lane = _make_lane_for_pause_runout(auto_switch_triggered=False)
        lane._perform_pause_runout()
        msg = lane.afc.error.AFC_error.call_args[0][0]
        print(msg)
        assert "Runout" in msg
        assert "Minimum weight" not in msg
        assert lane.name in msg

    def test_unload_on_runout_false_skips_tool_unload_entirely(self):
        """When unit_obj.unload_on_runout is False, the whole
        TOOL_UNLOAD/LANE_UNLOAD/error-counting block is skipped."""
        lane = _make_lane_for_pause_runout(unload_on_runout=False)
        lane._perform_pause_runout()
        lane.afc.TOOL_UNLOAD.assert_not_called()
        lane.afc.LANE_UNLOAD.assert_not_called()
        lane.afc.afc_stats.increase_unload_error_count.assert_not_called()

    def test_unload_on_runout_true_and_no_error_ejects_lane(self):
        """When unload_on_runout is True and TOOL_UNLOAD leaves error_state
        False, the lane is ejected via LANE_UNLOAD and no error is counted."""
        lane = _make_lane_for_pause_runout(unload_on_runout=True)
        lane.afc.error_state = False
        lane.afc.TOOL_UNLOAD.return_value = True
        lane._perform_pause_runout()
        lane.afc.TOOL_UNLOAD.assert_called_once_with(lane)
        lane.afc.LANE_UNLOAD.assert_called_once_with(lane)
        lane.afc.afc_stats.increase_unload_error_count.assert_not_called()

    def test_unload_on_runout_true_and_error_state_increases_unload_error_count(self):
        """When unload_on_runout is True and TOOL_UNLOAD leaves error_state
        True, LANE_UNLOAD is skipped and the failure is recorded via
        increase_unload_error_count(self.afc)."""
        lane = _make_lane_for_pause_runout(unload_on_runout=True)
        lane.afc.error_state = True
        lane.afc.TOOL_UNLOAD.return_value = False
        lane._perform_pause_runout()
        lane.afc.TOOL_UNLOAD.assert_called_once_with(lane)
        lane.afc.LANE_UNLOAD.assert_not_called()
        lane.afc.afc_stats.increase_unload_error_count.assert_called_once_with(lane.afc)

# ── cmd_SET_LANE_LOADED ───────────────────────────────────────────────────────
#
# Tests added with help from claude
#
# Method logic summary:
#   1. If lane.load_state is False → call AFC_error with message + pause=False, return early.
#   2. If afc.get_bypass_state() returns True:
#        - Build error message referencing lane name and bypass state.
#        - If 'virtual' in afc.bypass.name → include "virtual" and "disable" in message.
#        - Call logger.error(msg), return early.
#   3. Happy path (load_state True, get_bypass_state() False):
#        unset_lane_loaded() → handle_activate_extruder() → set_tool_loaded()
#        → sync_to_extruder() → save_vars() → unit_obj.select_lane(self)
#        → logger.info(message mentioning lane name)
#
# get_bypass_state() delegates to afc, so tests mock it directly on afc rather
# than wiring the full bypass object internals. This also makes the tests
# independent of the virtual/normal distinction inside get_bypass_state itself.
#
# load_state is a read-only property backed by _load_state; tests set _load_state
# directly to control the guard without going through the real sensor machinery. 
 
def _make_lane_for_set_loaded(
    load_state=True,
    bypass_active=False,
    bypass_name="bypass",
):
    """Build a minimal AFCLane wired for cmd_SET_LANE_LOADED tests.
 
    Args:
        load_state: Value for lane._load_state (read-only property).
        bypass_active: Return value of afc.get_bypass_state(). Mocked directly
            on afc so tests are decoupled from get_bypass_state internals.
        bypass_name: afc.bypass.name — controls virtual vs normal message path.
    """
    from tests.conftest import MockAFC, MockLogger
 
    lane = _make_afc_lane("AFC_stepper lane1")
    lane.afc = MockAFC()
    lane.logger = MockLogger()
 
    # load_state is a read-only property; drive it via its backing attribute.
    lane._load_state = load_state
 
    # Mock get_bypass_state directly — the method under test only calls this;
    # it doesn't inspect the bypass object internals directly.
    lane.afc.get_bypass_state = MagicMock(return_value=bypass_active)
    lane.afc.bypass.name = bypass_name
 
    # Methods called on the happy path
    lane.set_tool_loaded    = MagicMock()
    lane.sync_to_extruder   = MagicMock()
    lane.unit_obj.select_lane = MagicMock()
 
    return lane


# ── set_loaded / set_unloaded ──────────────────────────────────────────────

class TestSetLoaded:
    def test_status_set_to_loaded(self):
        lane = _make_afc_lane()
        lane.set_loaded()
        assert lane.status == AFCLaneState.LOADED

    def test_calls_lane_loaded(self):
        lane = _make_afc_lane()
        lane.set_loaded()
        lane.unit_obj.lane_loaded.assert_called_once_with(lane)

    def test_calls_spool_set_values(self):
        lane = _make_afc_lane()
        lane.set_loaded()
        lane.afc.spool._set_values.assert_called_once_with(lane)


class TestSetUnloaded:
    def _make_lane(self, remember_spool=False):
        lane = _make_afc_lane()
        lane.tool_loaded = True
        lane.status = AFCLaneState.TOOLED
        lane.loaded_to_hub = True
        lane.td1_data = {"scan_time": "1.0"}
        lane.remember_spool = remember_spool
        return lane

    def test_tool_loaded_cleared(self):
        lane = self._make_lane()
        lane.set_unloaded()
        assert lane.tool_loaded is False

    def test_status_set_to_none(self):
        lane = self._make_lane()
        lane.set_unloaded()
        assert lane.status == AFCLaneState.NONE

    def test_loaded_to_hub_cleared(self):
        lane = self._make_lane()
        lane.set_unloaded()
        assert lane.loaded_to_hub is False

    def test_td1_data_cleared(self):
        lane = self._make_lane()
        lane.set_unloaded()
        assert lane.td1_data == {}

    def test_clears_spool_values_when_not_remembering(self):
        lane = self._make_lane(remember_spool=False)
        lane.set_unloaded()
        lane.afc.spool.clear_values.assert_called_once_with(lane)

    def test_does_not_clear_spool_values_when_remembering(self):
        """Covers the `not self.remember_spool` guard's False branch."""
        lane = self._make_lane(remember_spool=True)
        lane.set_unloaded()
        lane.afc.spool.clear_values.assert_not_called()

    def test_calls_lane_not_ready(self):
        """set_unloaded now marks the lane not_ready (not the old
        lane_unloaded), so a freshly-emptied lane and one that was never
        loaded end up in the same LED state."""
        lane = self._make_lane()
        lane.set_unloaded()
        lane.unit_obj.lane_not_ready.assert_called_once_with(lane)


# ── cmd_AFC_RECOVER_LANE ───────────────────────────────────────────────────

class TestCmdAfcRecoverLane:
    def _make_lane(self, remember_spool=False):
        lane = _make_afc_lane()
        lane.tool_loaded = True
        lane.status = AFCLaneState.TOOLED
        lane.loaded_to_hub = True
        lane.td1_data = {"scan_time": "1.0"}
        lane.remember_spool = remember_spool
        return lane

    def test_tool_loaded_cleared(self):
        lane = self._make_lane()
        lane.cmd_AFC_RECOVER_LANE(MagicMock())
        assert lane.tool_loaded is False

    def test_status_set_to_none(self):
        lane = self._make_lane()
        lane.cmd_AFC_RECOVER_LANE(MagicMock())
        assert lane.status == AFCLaneState.NONE

    def test_loaded_to_hub_cleared(self):
        lane = self._make_lane()
        lane.cmd_AFC_RECOVER_LANE(MagicMock())
        assert lane.loaded_to_hub is False

    def test_td1_data_cleared(self):
        lane = self._make_lane()
        lane.cmd_AFC_RECOVER_LANE(MagicMock())
        assert lane.td1_data == {}

    def test_clears_spool_values_when_not_remembering(self):
        lane = self._make_lane(remember_spool=False)
        lane.cmd_AFC_RECOVER_LANE(MagicMock())
        lane.afc.spool.clear_values.assert_called_once_with(lane)

    def test_does_not_clear_spool_values_when_remembering(self):
        """Covers the `not self.remember_spool` guard's False branch."""
        lane = self._make_lane(remember_spool=True)
        lane.cmd_AFC_RECOVER_LANE(MagicMock())
        lane.afc.spool.clear_values.assert_not_called()

    def test_calls_lane_not_ready(self):
        lane = self._make_lane()
        lane.cmd_AFC_RECOVER_LANE(MagicMock())
        lane.unit_obj.lane_not_ready.assert_called_once_with(lane)

    def test_saves_vars(self):
        lane = self._make_lane()
        lane.cmd_AFC_RECOVER_LANE(MagicMock())
        lane.afc.save_vars.assert_called_once_with()

    def test_logs_reset_message_with_lane_name(self):
        lane = self._make_lane()
        lane.cmd_AFC_RECOVER_LANE(MagicMock())
        assert lane.logger.messages == [("info", f"{lane.name} reset")]


class TestCmdSetLaneLoaded:
    """Tests for AFCLane.cmd_SET_LANE_LOADED."""
 
    # ── early-exit: load_state is False ──────────────────────────────────────
 
    def test_afc_error_called_when_not_loaded(self):
        """AFC_error must be called when load_state is False."""
        lane = _make_lane_for_set_loaded(load_state=False)
        lane.cmd_SET_LANE_LOADED(MagicMock())
        lane.afc.error.AFC_error.assert_called_once()
 
    def test_afc_error_message_mentions_lane_name(self):
        """The error message must include the lane name."""
        lane = _make_lane_for_set_loaded(load_state=False)
        lane.cmd_SET_LANE_LOADED(MagicMock())
        msg = lane.afc.error.AFC_error.call_args[0][0]
        assert lane.name in msg
 
    def test_afc_error_called_with_pause_false(self):
        """AFC_error must be called with pause=False so the print is not paused."""
        lane = _make_lane_for_set_loaded(load_state=False)
        lane.cmd_SET_LANE_LOADED(MagicMock())
        _, kwargs = lane.afc.error.AFC_error.call_args
        assert kwargs.get("pause") is False
 
    def test_no_happy_path_calls_when_not_loaded(self):
        """When load_state is False none of the success-path methods must be called."""
        lane = _make_lane_for_set_loaded(load_state=False)
        lane.cmd_SET_LANE_LOADED(MagicMock())
        lane.afc.function.unset_lane_loaded.assert_not_called()
        lane.afc.function.handle_activate_extruder.assert_not_called()
        lane.set_tool_loaded.assert_not_called()
        lane.sync_to_extruder.assert_not_called()
        lane.afc.save_vars.assert_not_called()
        lane.unit_obj.select_lane.assert_not_called()
 
    # ── early-exit: bypass active (normal, non-virtual) ───────────────────────
 
    def test_logger_error_called_when_bypass_active(self):
        """When get_bypass_state() is True, logger.error must be called."""
        lane = _make_lane_for_set_loaded(bypass_active=True, bypass_name="bypass_sensor")
        lane.cmd_SET_LANE_LOADED(MagicMock())
        error_msgs = [msg for level, msg in lane.logger.messages if level == "error"]
        assert len(error_msgs) == 1
 
    def test_bypass_error_message_mentions_lane_name(self):
        """The bypass error message must include the lane name."""
        lane = _make_lane_for_set_loaded(bypass_active=True, bypass_name="bypass_sensor")
        lane.cmd_SET_LANE_LOADED(MagicMock())
        msg = next(msg for level, msg in lane.logger.messages if level == "error")
        assert lane.name in msg
 
    def test_bypass_error_message_mentions_bypass(self):
        """The bypass error message must tell the user bypass is active."""
        lane = _make_lane_for_set_loaded(bypass_active=True, bypass_name="bypass_sensor")
        lane.cmd_SET_LANE_LOADED(MagicMock())
        msg = next(msg for level, msg in lane.logger.messages if level == "error")
        assert "bypass" in msg.lower()
    
    def test_bypass_error_message_mentions_detects_filament(self):
        """The bypass error message must tell the user bypass is active."""
        lane = _make_lane_for_set_loaded(bypass_active=True, bypass_name="bypass_sensor")
        lane.cmd_SET_LANE_LOADED(MagicMock())
        msg = next(msg for level, msg in lane.logger.messages if level == "error")
        assert "detects filament" in msg.lower()
 
    def test_normal_bypass_error_does_not_mention_disable(self):
        """A non-virtual bypass message must NOT include 'disable'."""
        lane = _make_lane_for_set_loaded(bypass_active=True, bypass_name="bypass_sensor")
        lane.cmd_SET_LANE_LOADED(MagicMock())
        msg = next(msg for level, msg in lane.logger.messages if level == "error")
        assert "disable" not in msg.lower()
 
    def test_no_happy_path_calls_when_bypass_active(self):
        """When bypass is active none of the success-path methods must be called."""
        lane = _make_lane_for_set_loaded(bypass_active=True, bypass_name="bypass_sensor")
        lane.cmd_SET_LANE_LOADED(MagicMock())
        lane.afc.function.unset_lane_loaded.assert_not_called()
        lane.afc.function.handle_activate_extruder.assert_not_called()
        lane.set_tool_loaded.assert_not_called()
        lane.sync_to_extruder.assert_not_called()
        lane.afc.save_vars.assert_not_called()
        lane.unit_obj.select_lane.assert_not_called()
 
    def test_get_bypass_state_is_called(self):
        """The guard must delegate to afc.get_bypass_state()."""
        lane = _make_lane_for_set_loaded(bypass_active=False)
        lane.cmd_SET_LANE_LOADED(MagicMock())
        lane.afc.get_bypass_state.assert_called_once()
 
    # ── early-exit: bypass active (virtual) ──────────────────────────────────
 
    def test_virtual_bypass_error_message_includes_virtual(self):
        """When bypass name contains 'virtual' the message must say 'virtual'."""
        lane = _make_lane_for_set_loaded(bypass_active=True, bypass_name="virtual_bypass")
        lane.cmd_SET_LANE_LOADED(MagicMock())
        msg = next(msg for level, msg in lane.logger.messages if level == "error")
        assert "virtual" in msg.lower()
 
    def test_virtual_bypass_error_message_includes_disable(self):
        """When bypass name contains 'virtual' the message must include 'disable'."""
        lane = _make_lane_for_set_loaded(bypass_active=True, bypass_name="virtual_bypass")
        lane.cmd_SET_LANE_LOADED(MagicMock())
        msg = next(msg for level, msg in lane.logger.messages if level == "error")
        assert "disable" in msg.lower()
    
    def test_virtual_bypass_error_message_includes_is_enabled(self):
        """When bypass name contains 'virtual' the message must include 'disable'."""
        lane = _make_lane_for_set_loaded(bypass_active=True, bypass_name="virtual_bypass")
        lane.cmd_SET_LANE_LOADED(MagicMock())
        msg = next(msg for level, msg in lane.logger.messages if level == "error")
        assert "is enabled" in msg.lower()

    def test_virtual_bypass_error_message_does_not_include_detects_filament(self):
        """When bypass name contains 'virtual' the message must include 'disable'."""
        lane = _make_lane_for_set_loaded(bypass_active=True, bypass_name="virtual_bypass")
        lane.cmd_SET_LANE_LOADED(MagicMock())
        msg = next(msg for level, msg in lane.logger.messages if level == "error")
        assert "detects filament" not in msg.lower()
 
    def test_virtual_bypass_no_happy_path_calls(self):
        """Virtual bypass path must also return early with no success-path calls."""
        lane = _make_lane_for_set_loaded(bypass_active=True, bypass_name="virtual_bypass")
        lane.cmd_SET_LANE_LOADED(MagicMock())
        lane.afc.function.unset_lane_loaded.assert_not_called()
        lane.set_tool_loaded.assert_not_called()
 
    # ── happy path ────────────────────────────────────────────────────────────
 
    def test_unset_lane_loaded_called(self):
        lane = _make_lane_for_set_loaded()
        lane.cmd_SET_LANE_LOADED(MagicMock())
        lane.afc.function.unset_lane_loaded.assert_called_once()
 
    def test_handle_activate_extruder_called(self):
        lane = _make_lane_for_set_loaded()
        lane.cmd_SET_LANE_LOADED(MagicMock())
        lane.afc.function.handle_activate_extruder.assert_called_once()
 
    def test_set_tool_loaded_called(self):
        lane = _make_lane_for_set_loaded()
        lane.cmd_SET_LANE_LOADED(MagicMock())
        lane.set_tool_loaded.assert_called_once()
 
    def test_sync_to_extruder_called(self):
        lane = _make_lane_for_set_loaded()
        lane.cmd_SET_LANE_LOADED(MagicMock())
        lane.sync_to_extruder.assert_called_once()
 
    def test_save_vars_called(self):
        lane = _make_lane_for_set_loaded()
        lane.cmd_SET_LANE_LOADED(MagicMock())
        lane.afc.save_vars.assert_called_once()
 
    def test_select_lane_called_with_self(self):
        """unit_obj.select_lane must be called with the lane itself."""
        lane = _make_lane_for_set_loaded()
        lane.cmd_SET_LANE_LOADED(MagicMock())
        lane.unit_obj.select_lane.assert_called_once_with(lane)
 
    def test_logger_info_called_with_lane_name(self):
        """A success info message must be logged that mentions the lane name."""
        lane = _make_lane_for_set_loaded()
        lane.cmd_SET_LANE_LOADED(MagicMock())
        info_msgs = [msg for level, msg in lane.logger.messages if level == "info"]
        assert any(lane.name in msg for msg in info_msgs)
 
    def test_happy_path_call_order(self):
        """Success-path calls must execute in the correct sequence."""
        lane = _make_lane_for_set_loaded()
        order = []
        lane.afc.function.unset_lane_loaded.side_effect        = lambda *a, **kw: order.append("unset_lane_loaded")
        lane.afc.function.handle_activate_extruder.side_effect = lambda *a, **kw: order.append("handle_activate_extruder")
        lane.set_tool_loaded.side_effect                        = lambda *a, **kw: order.append("set_tool_loaded")
        lane.sync_to_extruder.side_effect                       = lambda *a, **kw: order.append("sync_to_extruder")
        lane.afc.save_vars.side_effect                          = lambda *a, **kw: order.append("save_vars")
        lane.unit_obj.select_lane.side_effect                   = lambda *a, **kw: order.append("select_lane")
 
        lane.cmd_SET_LANE_LOADED(MagicMock())
 
        assert order == [
            "unset_lane_loaded",
            "handle_activate_extruder",
            "set_tool_loaded",
            "sync_to_extruder",
            "save_vars",
            "select_lane",
        ]
 
    def test_afc_error_not_called_on_happy_path(self):
        """AFC_error must not be called when all preconditions are satisfied."""
        lane = _make_lane_for_set_loaded()
        lane.cmd_SET_LANE_LOADED(MagicMock())
        lane.afc.error.AFC_error.assert_not_called()
 
    def test_no_logger_error_on_happy_path(self):
        """No error must be logged when all preconditions are satisfied."""
        lane = _make_lane_for_set_loaded()
        lane.cmd_SET_LANE_LOADED(MagicMock())
        error_msgs = [msg for level, msg in lane.logger.messages if level == "error"]
        assert error_msgs == []

class TestHandleToolheadRunout:
    def _make_lane_for_toolhead_runout(self, normal_printing_state=True, printing=True,
                                       on_shuttle=True, prep=True, load=True, hub=True,
                                       runout=None, standalone=False):
        from tests.conftest import MockLogger

        lane = _make_afc_lane("AFC_stepper lane1")
        lane.prep_state = prep
        lane._load_state = load
        if hub is not None:
            lane.hub_obj = MagicMock()
            lane.hub_obj.state = hub
        else:
            lane.hub_obj = None
        lane.logger = MockLogger()
        lane.afc.save_pos = MagicMock()
        lane.afc.save_vars = MagicMock()
        lane.runout_lane = runout
        lane._perform_pause_runout = MagicMock()
        lane._perform_infinite_runout = MagicMock()

        lane._is_normal_printing_state = MagicMock(return_value=normal_printing_state)
        lane.afc.function.is_printing = MagicMock(return_value=printing)
        lane.extruder_obj.on_shuttle = MagicMock(return_value=on_shuttle)
        lane.extruder_obj.is_standalone = MagicMock(return_value=standalone)
        lane.set_tool_unloaded = MagicMock()
        lane.set_unloaded = MagicMock()
        return lane

    def test_not_normal_printing_state(self):
        lane = self._make_lane_for_toolhead_runout(normal_printing_state=False)
        lane.handle_toolhead_runout(sensor="tool_start")
        lane.afc.save_pos.assert_not_called()
        lane.afc.save_vars.assert_not_called()

    def test_not_printing(self):
        lane = self._make_lane_for_toolhead_runout(printing=False)
        lane.handle_toolhead_runout(sensor="tool_start")
        lane.afc.save_pos.assert_not_called()
        lane.afc.save_vars.assert_not_called()
    
    def test_not_on_shuttle(self):
        lane = self._make_lane_for_toolhead_runout(on_shuttle=False)
        lane.handle_toolhead_runout(sensor="tool_start")
        lane.afc.save_pos.assert_not_called()
        lane.afc.save_vars.assert_not_called()
    
    def test_all_sensors_true(self):
        lane = self._make_lane_for_toolhead_runout()
        lane.handle_toolhead_runout(sensor="tool_start")
        lane.afc.save_pos.assert_called_once()
        lane.afc.error.pause_resume.send_pause_command.assert_called_once()
        lane.afc.error.AFC_error.assert_called_once()

    def test_all_sensors_true_check_error_msg(self):
        sensor_name = "tool_start"
        lane = self._make_lane_for_toolhead_runout()
        lane.handle_toolhead_runout(sensor=sensor_name)
        assert f"Toolhead runout detected by {sensor_name} sensor" in \
                lane.afc.error.AFC_error.call_args.args[0]
    
    def test_prep_false(self):
        lane = self._make_lane_for_toolhead_runout(prep=False)
        lane.handle_toolhead_runout(sensor="tool_start")
        lane.afc.error.pause_resume.send_pause_command.assert_not_called()
        lane._perform_pause_runout.assert_called_once()
    
    def test_load_false(self):
        lane = self._make_lane_for_toolhead_runout(load=False)
        lane.handle_toolhead_runout(sensor="tool_start")
        lane.afc.error.pause_resume.send_pause_command.assert_not_called()
        lane._perform_pause_runout.assert_called_once()
    
    def test_hub_false(self):
        lane = self._make_lane_for_toolhead_runout(hub=False)
        lane.handle_toolhead_runout(sensor="tool_start")
        lane.afc.error.pause_resume.send_pause_command.assert_not_called()
        lane._perform_pause_runout.assert_called_once()

    def test_no_hub_obj(self):
        lane = self._make_lane_for_toolhead_runout(hub=False)
        lane.hub_obj = None
        lane.handle_toolhead_runout(sensor="tool_start")
        lane.afc.save_pos.assert_called_once()
        lane.afc.error.pause_resume.send_pause_command.assert_called_once()
        lane.afc.error.AFC_error.assert_called_once()
    
    def test_standalone_runout_no_runout_lane(self):
        lane = self._make_lane_for_toolhead_runout(standalone=True)
        lane.handle_toolhead_runout(sensor="tool_start")
        lane.afc.error.pause_resume.send_pause_command.assert_not_called()
        lane._perform_pause_runout.assert_called_once()
        lane._perform_infinite_runout.assert_not_called()

        lane.set_tool_unloaded.assert_called_once()
        lane.set_unloaded.assert_called_once()
        lane.afc.save_vars.assert_called_once()

    def test_standalone_runout_disabled_tool_start(self):
        sensor = "tool_start"
        lane = self._make_lane_for_toolhead_runout(standalone=True)
        lane.extruder_obj.fila_tool_start.runout_helper.sensor_enabled = False
        lane.handle_toolhead_runout(sensor=sensor)

        warn_msgs = [m for lvl, m in lane.logger.messages if lvl == "warning"]
        assert any(f"{sensor} runout has been detected," in m for m in warn_msgs)
        lane._perform_pause_runout.assert_not_called()
        lane._perform_infinite_runout.assert_not_called()
        lane.afc.error.pause_resume.send_pause_command.assert_not_called()

    def test_standalone_runout_disabled_tool_end(self):
        sensor = "tool_end"
        lane = self._make_lane_for_toolhead_runout(standalone=True)
        lane.extruder_obj.fila_tool_end.runout_helper.sensor_enabled = False
        lane.handle_toolhead_runout(sensor=sensor)

        warn_msgs = [m for lvl, m in lane.logger.messages if lvl == "warning"]
        assert any(f"{sensor} runout has been detected," in m for m in warn_msgs)
    
    def test_standalone_runout_disabled_None(self):
        lane = self._make_lane_for_toolhead_runout(standalone=True)
        lane.extruder_obj.fila_tool_end.runout_helper.sensor_enabled = False
        lane.handle_toolhead_runout()

        warn_msgs = [m for lvl, m in lane.logger.messages if lvl == "warning"]
        assert any("toolhead runout has been detected," in m for m in warn_msgs)
    
    def test_standalone_runout_has_runout_lane(self):
        lane = self._make_lane_for_toolhead_runout(standalone=True, runout="lane1")
        lane.handle_toolhead_runout(sensor="tool_start")
        lane.afc.error.pause_resume.send_pause_command.assert_not_called()
        lane._perform_pause_runout.assert_not_called()
        lane._perform_infinite_runout.assert_called_once()

class TestSetToolLoaded:
    def test_not_normal_toolchange(self):
        obj = _make_afc_lane()
        obj.extruder_obj.on_shuttle.return_value = False
        obj.afc.current_loading = True

        obj.set_tool_loaded()

        assert obj.tool_loaded is True
        assert obj.extruder_obj.lane_loaded is obj.name
        assert obj.status is AFCLaneState.TOOLED
        assert obj.afc.current_loading is True
        obj.afc.spool.set_active_spool.assert_not_called()
    
    def test_not_normal_toolchange_lane_tool_loaded_called(self):
        obj = _make_afc_lane()
        obj.extruder_obj.on_shuttle.return_value = False

        obj.set_tool_loaded()
        obj.unit_obj.lane_tool_loaded.assert_called_once()
        obj.unit_obj.lane_tool_loaded.assert_called_with(obj)
        obj.afc.spool.set_active_spool.assert_not_called()
    
    def test_normal_toolchange(self):
        spool_id = 100
        obj = _make_afc_lane()
        obj.spool_id = spool_id
        obj.extruder_obj.on_shuttle.return_value = True

        obj.set_tool_loaded(normal_toolchange=True)
        obj.afc.spool.set_active_spool.assert_called_once()
        obj.afc.spool.set_active_spool.assert_called_with(spool_id)
        assert obj.afc.current_loading is None

class TestPerformInfiniteRunout:
    def test_no_current_lane_found(self):
        afc = MockAFC()
        afc.lanes = MagicMock(spec=dict)
        afc.lanes.get.return_value = None
        lane = _make_afc_lane()
        lane.afc = afc


        lane._perform_infinite_runout()
        info_msgs = [m for lvl, m in lane.logger.messages if lvl == "info"]

        assert lane.status is AFCLaneState.NONE
        lane.unit_obj.lane_not_ready.assert_not_called()
        assert any(f"Infinite Spool triggered for {lane.name}" in m for m in info_msgs), info_msgs
        afc.lanes.get.assert_called_with(afc.current)
        assert afc.lanes.get.call_count == 1
        afc.error.AFC_error.assert_called_with(f"Error when looking up current lane:{lane.afc.current}")

    def test_current_lane_found_runout_none(self):
        afc = MockAFC()
        afc.lanes = MagicMock(spec=dict)
        lane = _make_afc_lane()
        lane.afc = afc
        afc.current = lane.name
        afc.lanes.get.side_effect = [lane, None]

        lane._perform_infinite_runout()

        afc.lanes.get.assert_any_call(afc.current)
        afc.lanes.get.assert_any_call(None)
        assert afc.lanes.get.call_count == 2
        afc.error.AFC_error.assert_called_with(f"Error when looking up runout lane:{lane.runout_lane} for lane:{lane.name}")

    def test_current_lane_found_runout_lane2_not_found(self):
        afc = MockAFC()
        afc.lanes = MagicMock(spec=dict)
        lane = _make_afc_lane()
        lane2 = _make_afc_lane()
        lane.afc = afc
        lane.runout_lane = lane2.name
        afc.current = lane.name
        afc.lanes.get.side_effect = [lane, None]

        lane._perform_infinite_runout()

        afc.lanes.get.assert_any_call(afc.current)
        afc.lanes.get.assert_any_call(lane2.name)
        assert afc.lanes.get.call_count == 2
        afc.error.AFC_error.assert_called_with(f"Error when looking up runout lane:{lane.runout_lane} for lane:{lane.name}")
    
    def test_current_lane_found_runout_lane2(self):
        afc = MockAFC()
        afc.lanes = MagicMock(spec=dict)
        lane = _make_afc_lane()
        lane2 = _make_afc_lane()
        lane.afc = afc
        lane.runout_lane = lane2.name
        afc.current = lane.name
        afc.lanes.get.side_effect = [lane, lane2]

        lane._perform_infinite_runout()

        afc.lanes.get.assert_any_call(afc.current)
        afc.lanes.get.assert_any_call(lane2.name)
        assert afc.lanes.get.call_count == 2
        assert lane2.status is AFCLaneState.INFINITE_RUNOUT
    
    def test_current_lane_found_runout_lane2_check_calls(self):
        afc = MockAFC()
        afc.lanes = MagicMock(spec=dict)
        lane = _make_afc_lane()
        lane2 = _make_afc_lane()
        lane.afc = afc
        lane.runout_lane = lane2.name
        afc.current = lane.name
        afc.lanes.get.side_effect = [lane, lane2]

        lane._perform_infinite_runout()

        afc.error.pause_resume.send_pause_command.assert_called_once()
        afc.save_pos.assert_called_once()
        afc.CHANGE_TOOL.assert_called_with(lane2, restore_pos=False)
        lane.gcode.run_script_from_command.assert_called_with(f"AFC_SWAP_MAPPING FROM={lane.name} TO={lane2.name}")
        afc.restore_pos.assert_called_once()
        afc.error.pause_resume.send_resume_command.assert_called_once()
        lane.unit_obj.lane_not_ready.assert_called_with(lane)

    def test_current_lane_found_runout_lane2_check_extruder_calls(self):
        afc = MockAFC()
        afc.lanes = MagicMock(spec=dict)
        lane = _make_afc_lane()
        lane2 = _make_afc_lane()
        lane.afc = afc
        lane.runout_lane = lane2.name
        afc.current = lane.name
        afc.lanes.get.side_effect = [lane, lane2]

        lane._perform_infinite_runout()

        lane.extruder_obj.set_print_leds.assert_called_with(0, quiet=True)
        lane2.extruder_obj.set_print_leds.assert_called_with(1, quiet=True)
        lane2.unit_obj.lane_tool_loaded.assert_called_with(lane2)
    
    def test_current_lane_found_runout_lane2_same_extruder(self):
        afc = MockAFC()
        afc.lanes = MagicMock(spec=dict)
        lane = _make_afc_lane()
        lane2 = _make_afc_lane()
        lane.afc = afc
        lane.runout_lane = lane2.name
        lane2.extruder_obj = lane.extruder_obj
        afc.current = lane.name
        afc.lanes.get.side_effect = [lane, lane2]

        lane._perform_infinite_runout()

        lane.extruder_obj.set_print_leds.assert_not_called()
    
    def test_current_lane_found_runout_lane2_error_state(self):
        def change_tool_side_effect(afc):
            afc.error_state = True
            return True
        afc = MockAFC()
        afc.lanes = MagicMock(spec=dict)
        lane = _make_afc_lane()
        lane2 = _make_afc_lane()
        lane.afc = afc
        lane.runout_lane = lane2.name
        afc.current = lane.name
        afc.lanes.get.side_effect = [lane, lane2]
        afc.CHANGE_TOOL.side_effect = lambda *a, **kw: (
            change_tool_side_effect(afc)
        )

        lane._perform_infinite_runout()

        afc.lanes.get.assert_any_call(afc.current)
        afc.lanes.get.assert_any_call(lane2.name)
        assert afc.lanes.get.call_count == 2
        assert lane2.status is AFCLaneState.INFINITE_RUNOUT
        lane.gcode.run_script_from_command.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# AFCU1Lane — external-feeder standalone lane (e.g. U1 side feeders)
# ══════════════════════════════════════════════════════════════════════════════
#
# AFCU1Lane mirrors an external `[filament_feed left|right]` module's per-channel
# filament presence into the lane's prep_state/_load_state so AFC reads the lane
# as loaded-and-ready. Instances below are built through the real __init__ chain
# (MockConfig/MockPrinter/MockAFC). Using "AFC_stepper" as the config section
# name makes AFCLane.__init__ take its early-return path (no stepper/hub/buffer/
# extruder pins configured), which keeps construction lightweight while still
# running every line of real constructor logic AFCU1Lane depends on.

from tests.conftest import MockReactor, MockConfig, MockPrinter


def _make_u1_config(feed_module="left", feed_channel="extruder1",
                    fullname="AFC_stepper lane1", extra_values=None):
    """Build a MockConfig wired for AFCU1Lane's real __init__ chain."""
    values = {"unit": "Turtle_1:0"}
    if feed_module is not None:
        values["feed_module"] = feed_module
    if feed_channel is not None:
        values["feed_channel"] = feed_channel
    if extra_values:
        values.update(extra_values)
    afc = MockAFC()
    afc.load_to_hub = False
    printer = MockPrinter(afc=afc)
    return MockConfig(name=fullname, printer=printer, values=values)


def _make_afcu1_lane(feed_module="left", feed_channel="extruder1",
                     fullname="AFC_stepper lane1", extra_values=None):
    """Build an AFCU1Lane through its real __init__ chain."""
    config = _make_u1_config(feed_module, feed_channel, fullname, extra_values)
    return AFCU1Lane(config)


# ── __init__ ──────────────────────────────────────────────────────────────────

class TestAFCU1LaneInit:
    def test_feed_module_read_from_config(self):
        lane = _make_afcu1_lane(feed_module="left", feed_channel="e1")
        assert lane.feed_module == "left"

    def test_feed_channel_read_from_config(self):
        lane = _make_afcu1_lane(feed_module="right", feed_channel="e2")
        assert lane.feed_channel == "e2"

    def test_feed_obj_starts_none(self):
        lane = _make_afcu1_lane(feed_module="left", feed_channel="e1")
        assert lane._feed_obj is None

    def test_feed_ch_index_starts_none(self):
        lane = _make_afcu1_lane(feed_module="left", feed_channel="e1")
        assert lane._feed_ch_index is None

    def test_feed_staged_last_starts_none(self):
        lane = _make_afcu1_lane(feed_module="left", feed_channel="e1")
        assert lane._feed_staged_last is None

    def test_load_state_forced_false_when_feed_configured(self):
        """A fully-configured feeder lane is driven by the feeder, so the
        (absent) load switch state is seeded False instead of the AFCLane
        default of True (no load pin configured)."""
        lane = _make_afcu1_lane(feed_module="left", feed_channel="e1")
        assert lane._load_state is False

    def test_feed_module_none_when_unconfigured(self):
        lane = _make_afcu1_lane(feed_module=None, feed_channel=None)
        assert lane.feed_module is None

    def test_feed_channel_none_when_unconfigured(self):
        lane = _make_afcu1_lane(feed_module=None, feed_channel=None)
        assert lane.feed_channel is None

    def test_load_state_default_true_when_fully_unconfigured(self):
        """Baseline AFCLane behavior (no load switch pin) is untouched when
        neither feed_module nor feed_channel is set."""
        lane = _make_afcu1_lane(feed_module=None, feed_channel=None)
        assert lane._load_state is True

    def test_load_state_not_overridden_when_only_module_configured(self):
        """feed_module alone must not satisfy the guard."""
        lane = _make_afcu1_lane(feed_module="left", feed_channel=None)
        assert lane._load_state is True

    def test_load_state_not_overridden_when_only_channel_configured(self):
        """feed_channel alone must not satisfy the guard."""
        lane = _make_afcu1_lane(feed_module=None, feed_channel="e1")
        assert lane._load_state is True

    def test_registers_port_event_handler_when_feed_configured(self):
        lane = _make_afcu1_lane(feed_module="left", feed_channel="e1")
        assert lane.printer._event_handlers["filament_feed:port"] == [lane._feed_port_event]

    def test_no_port_event_handler_when_fully_unconfigured(self):
        lane = _make_afcu1_lane(feed_module=None, feed_channel=None)
        assert "filament_feed:port" not in lane.printer._event_handlers

    def test_no_port_event_handler_when_only_module_configured(self):
        lane = _make_afcu1_lane(feed_module="left", feed_channel=None)
        assert "filament_feed:port" not in lane.printer._event_handlers

    def test_no_port_event_handler_when_only_channel_configured(self):
        lane = _make_afcu1_lane(feed_module=None, feed_channel="e1")
        assert "filament_feed:port" not in lane.printer._event_handlers


# ── _apply_staged ─────────────────────────────────────────────────────────────

class TestAFCU1LaneApplyStaged:
    def test_rising_edge_marks_loaded(self):
        lane = _make_afcu1_lane()
        lane._feed_staged_last = False
        lane._apply_staged(True)
        assert lane.prep_state is True
        assert lane._load_state is True

    def test_rising_edge_updates_last(self):
        lane = _make_afcu1_lane()
        lane._feed_staged_last = False
        lane._apply_staged(True)
        assert lane._feed_staged_last is True

    def test_rising_edge_persists(self):
        lane = _make_afcu1_lane()
        lane._feed_staged_last = False
        lane._apply_staged(True)
        lane.afc.save_vars.assert_called_once()

    def test_falling_edge_marks_empty(self):
        lane = _make_afcu1_lane()
        lane._feed_staged_last = True
        lane.prep_state = True
        lane._load_state = True
        lane._apply_staged(False)
        assert lane.prep_state is False
        assert lane._load_state is False

    def test_no_change_does_not_persist(self):
        """Re-applying the same state must not write vars again."""
        lane = _make_afcu1_lane()
        lane._feed_staged_last = True
        lane._apply_staged(True)
        lane.afc.save_vars.assert_not_called()

    def test_no_change_leaves_last_untouched(self):
        lane = _make_afcu1_lane()
        lane._feed_staged_last = True
        lane._apply_staged(True)
        assert lane._feed_staged_last is True

    def test_first_apply_from_none_persists(self):
        """Initial state is None, so even a False first reading is an edge."""
        lane = _make_afcu1_lane()
        lane._feed_staged_last = None
        lane._apply_staged(False)
        assert lane._feed_staged_last is False
        lane.afc.save_vars.assert_called_once()


# ── _feed_channel_present ─────────────────────────────────────────────────────

class TestAFCU1LaneFeedChannelPresent:
    def test_true_when_channel_detected(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_obj = MagicMock()
        lane._feed_obj.get_status.return_value = {
            "extruder1": {"filament_detected": True},
        }
        assert lane._feed_channel_present() is True

    def test_false_when_channel_not_detected(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_obj = MagicMock()
        lane._feed_obj.get_status.return_value = {
            "extruder1": {"filament_detected": False},
        }
        assert lane._feed_channel_present() is False

    def test_false_when_channel_missing(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_obj = MagicMock()
        lane._feed_obj.get_status.return_value = {"extruder0": {"filament_detected": True}}
        assert lane._feed_channel_present() is False

    def test_false_when_detected_key_absent(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_obj = MagicMock()
        lane._feed_obj.get_status.return_value = {"extruder1": {}}
        assert lane._feed_channel_present() is False

    def test_non_true_value_is_false(self):
        """Only a literal True counts as present."""
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_obj = MagicMock()
        lane._feed_obj.get_status.return_value = {"extruder1": {"filament_detected": 1}}
        assert lane._feed_channel_present() is False

    def test_false_when_get_status_raises(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_obj = MagicMock()
        lane._feed_obj.get_status.side_effect = RuntimeError("boom")
        assert lane._feed_channel_present() is False

    def test_false_when_feed_obj_none(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_obj = None
        assert lane._feed_channel_present() is False

    def test_queries_status_at_current_monotonic(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_obj = MagicMock()
        lane._feed_obj.get_status.return_value = {"extruder1": {"filament_detected": True}}
        lane._feed_channel_present()
        lane._feed_obj.get_status.assert_called_once_with(lane.reactor.monotonic())


# ── _feed_reevaluate ──────────────────────────────────────────────────────────

class TestAFCU1LaneFeedReevaluate:
    """_feed_reevaluate is _feed_channel_present() piped straight into
    _apply_staged(); exercised end-to-end via the real feeder-object contract
    rather than mocking either sub-method, so the assertions prove the actual
    prep_state/_load_state outcome, not just that some method was invoked."""

    def test_present_marks_loaded(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_obj = MagicMock()
        lane._feed_obj.get_status.return_value = {"extruder1": {"filament_detected": True}}
        lane._feed_staged_last = False
        lane._feed_reevaluate()
        assert lane.prep_state is True
        assert lane._load_state is True

    def test_absent_marks_empty(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_obj = MagicMock()
        lane._feed_obj.get_status.return_value = {"extruder1": {"filament_detected": False}}
        lane._feed_staged_last = True
        lane.prep_state = True
        lane._load_state = True
        lane._feed_reevaluate()
        assert lane.prep_state is False
        assert lane._load_state is False

    def test_no_feed_obj_marks_empty(self):
        """A missing feeder object is treated as absent (defensive default)."""
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_obj = None
        lane._feed_staged_last = True
        lane.prep_state = True
        lane._load_state = True
        lane._feed_reevaluate()
        assert lane.prep_state is False
        assert lane._load_state is False


# ── _feed_port_event ──────────────────────────────────────────────────────────

class TestAFCU1LaneFeedPortEvent:
    """_feed_port_event filters by channel index, then trusts the event's own
    `detected` flag directly (no _feed_obj lookup — deliberately, so the
    handler works even before _handle_ready has set _feed_obj up; see
    TestAFCU1LaneEventBeforeReady). Tests assert on real prep_state/_load_state
    outcomes so each would fail if the index guard let the wrong event
    through or blocked the right one."""

    def test_applies_when_index_matches(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_ch_index = 1
        lane._feed_staged_last = False
        lane._feed_port_event(1, True)
        assert lane.prep_state is True
        assert lane._load_state is True
        assert lane._feed_staged_last is True

    def test_ignores_when_index_differs(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_ch_index = 1
        lane._feed_staged_last = False
        lane.prep_state = False
        lane._load_state = False
        lane._feed_port_event(0, True)
        assert lane.prep_state is False
        assert lane._load_state is False
        assert lane._feed_staged_last is False
        lane.afc.save_vars.assert_not_called()

    def test_applies_when_index_unknown(self):
        """A None channel index means we can't filter, so every event applies."""
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_ch_index = None
        lane._feed_staged_last = True
        lane.prep_state = True
        lane._load_state = True
        lane._feed_port_event(5, False)
        assert lane.prep_state is False
        assert lane._load_state is False
        assert lane._feed_staged_last is False

    def test_passes_detected_true_through(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_ch_index = 2
        lane._feed_staged_last = False
        lane._feed_port_event(2, True)
        assert lane.prep_state is True
        assert lane._load_state is True

    def test_passes_detected_false_through(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_ch_index = 2
        lane._feed_staged_last = True
        lane.prep_state = True
        lane._load_state = True
        lane._feed_port_event(2, False)
        assert lane.prep_state is False
        assert lane._load_state is False

    def test_logs_debug_message_when_applied(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_ch_index = 1
        lane._feed_staged_last = False
        lane._feed_port_event(1, True)
        assert lane.logger.messages == [("debug", f"AFCU1Lane: feed_port_event: {lane.name} True")]

    def test_no_debug_log_when_index_differs(self):
        """A filtered-out event must return before the log call."""
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane._feed_ch_index = 1
        lane._feed_port_event(0, True)
        assert lane.logger.messages == []


# ── _handle_ready ─────────────────────────────────────────────────────────────

class TestAFCU1LaneHandleReady:
    """_handle_ready is exercised for real, including the AFCLane base-class
    portion (super()._handle_ready()) — the feeder module and AFC_extruder
    lookups are injected via MockPrinter._objects, the same pattern other
    AFC test files already use for wiring lookup_object() dependencies."""

    def _ready_lane(self, feed_module="left", feed_channel="extruder1"):
        lane = _make_afcu1_lane(feed_module=feed_module, feed_channel=feed_channel)
        lane.unit_obj = MagicMock()  # normally set later via handle_unit_connect
        return lane

    def _register_feeder(self, lane, detected=False, channel=None):
        channel = channel or lane.feed_channel
        feeder = MagicMock()
        feeder.get_status.return_value = {channel: {"filament_detected": detected}}
        lane.printer._objects[f"filament_feed {lane.feed_module}"] = feeder
        return feeder

    def test_skips_wiring_when_not_configured(self):
        lane = self._ready_lane(feed_module=None, feed_channel=None)
        lane._handle_ready()
        assert lane._feed_obj is None
        assert "filament_feed:port" not in lane.printer._event_handlers

    def test_skips_wiring_when_only_feed_module_configured(self):
        lane = self._ready_lane(feed_module="left", feed_channel=None)
        lane._handle_ready()
        assert lane._feed_obj is None
        assert lane._feed_ch_index is None
        assert "filament_feed:port" not in lane.printer._event_handlers

    def test_skips_wiring_when_only_feed_channel_configured(self):
        lane = self._ready_lane(feed_module=None, feed_channel="extruder1")
        lane._handle_ready()
        assert lane._feed_obj is None
        assert lane._feed_ch_index is None
        assert "filament_feed:port" not in lane.printer._event_handlers

    def test_feed_ch_indx_none_when_feed_channel_not_have_digit(self):
        lane = self._ready_lane(feed_channel="extruder")
        ext_obj = MagicMock()
        ext_obj.toolhead_extruder.extruder_index = 3
        lane.printer._objects["AFC_extruder e3"] = ext_obj
        self._register_feeder(lane, channel="extruder")
        lane._handle_ready()
        assert lane._feed_ch_index is None

    def test_feed_channel_is_extruder0_when_feed_channel_not_have_digit(self):
        lane = self._ready_lane(feed_channel="extruder")
        ext_obj = MagicMock()
        ext_obj.toolhead_extruder.extruder_index = 0
        lane.printer._objects["AFC_extruder extruder"] = ext_obj
        self._register_feeder(lane, channel="extruder")
        lane._handle_ready()
        assert lane._feed_ch_index == 0
        assert lane.feed_channel == "extruder0"

    def test_base_handle_ready_runs(self):
        """super()._handle_ready() actually executes: with no espooler motor
        pins configured it logs instead of starting a stats timer."""
        lane = self._ready_lane()
        self._register_feeder(lane)
        lane._handle_ready()
        assert lane.logger.messages == [("info", "Not starting timer for lane1")]

    def test_raises_when_feeder_missing(self):
        lane = self._ready_lane()
        with pytest.raises(Exception) as exc:
            lane._handle_ready()
        assert lane.name in str(exc.value)

    def test_raw_channel_key_kept_as_is(self):
        lane = self._ready_lane(feed_channel="extruder2")
        self._register_feeder(lane)
        lane._handle_ready()
        assert lane.feed_channel == "extruder2"

    def test_raw_channel_index_derived(self):
        lane = self._ready_lane(feed_channel="extruder2")
        self._register_feeder(lane)
        lane._handle_ready()
        assert lane._feed_ch_index == 2

    def test_afc_extruder_name_resolved_via_extruder_index(self):
        """A channel given as an AFC extruder name resolves to 'extruder<index>'
        using the toolhead extruder's extruder_index."""
        lane = self._ready_lane(feed_channel="e3")
        ext_obj = MagicMock()
        ext_obj.toolhead_extruder.extruder_index = 3
        lane.printer._objects["AFC_extruder e3"] = ext_obj
        self._register_feeder(lane, channel="extruder3")
        lane._handle_ready()
        assert lane.feed_channel == "extruder3"
        assert lane._feed_ch_index == 3

    def test_afc_extruder_name_resolved_via_th_name_fallback(self):
        """When extruder_index is None, fall back to the th_extruder_name suffix."""
        lane = self._ready_lane(feed_channel="e2")
        ext_obj = MagicMock()
        ext_obj.toolhead_extruder.extruder_index = None
        ext_obj.th_extruder_name = "extruder2"
        lane.printer._objects["AFC_extruder e2"] = ext_obj
        self._register_feeder(lane, channel="extruder2")
        lane._handle_ready()
        assert lane.feed_channel == "extruder2"
        assert lane._feed_ch_index == 2

    def test_afc_extruder_name_th_name_e0_maps_to_extruder0(self):
        """The base klipper extruder ('extruder', no digit) maps to channel 0."""
        lane = self._ready_lane(feed_channel="e0")
        ext_obj = MagicMock()
        ext_obj.toolhead_extruder.extruder_index = None
        ext_obj.th_extruder_name = "extruder"
        lane.printer._objects["AFC_extruder e0"] = ext_obj
        self._register_feeder(lane, channel="extruder0")
        lane._handle_ready()
        assert lane.feed_channel == "extruder0"
        assert lane._feed_ch_index == 0

    def test_registers_port_event_handler(self):
        lane = self._ready_lane()
        self._register_feeder(lane)
        lane._handle_ready()
        assert lane.printer._event_handlers["filament_feed:port"] == [lane._feed_port_event]

    def test_seeds_state_true_from_current_feeder(self):
        lane = self._ready_lane()
        self._register_feeder(lane, detected=True)
        lane._handle_ready()
        assert lane.prep_state is True
        assert lane._load_state is True

    def test_seeds_state_false_from_current_feeder(self):
        lane = self._ready_lane()
        self._register_feeder(lane, detected=False)
        lane._handle_ready()
        assert lane.prep_state is False
        assert lane._load_state is False

    def test_feed_obj_stored(self):
        lane = self._ready_lane()
        feeder = self._register_feeder(lane)
        lane._handle_ready()
        assert lane._feed_obj is feeder


# ── event ordering: __init__ registers before _handle_ready is ready ─────────
#
# __init__ subscribes _feed_port_event to "filament_feed:port" before
# _handle_ready has set up _feed_obj/_feed_ch_index — event registration order
# does not guarantee callback order, so the feeder could fire that event in
# the window between construction and klippy:ready. _feed_port_event is
# deliberately self-contained (it trusts the event's own `detected` flag
# instead of re-querying self._feed_obj), so it never touches the
# not-yet-set feeder object and resolves correctly even this early.

class TestAFCU1LaneEventBeforeReady:
    def test_premature_event_does_not_touch_feed_obj(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        assert lane._feed_obj is None
        lane.printer.send_event("filament_feed:port", 0, True)  # must not raise
        assert lane._feed_obj is None

    def test_premature_event_true_resolves_loaded(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane.printer.send_event("filament_feed:port", 0, True)
        assert lane.prep_state is True
        assert lane._load_state is True

    def test_premature_event_false_resolves_empty(self):
        lane = _make_afcu1_lane(feed_channel="extruder1")
        lane.printer.send_event("filament_feed:port", 0, False)
        assert lane.prep_state is False
        assert lane._load_state is False


# ── prep_callback ──────────────────────────────────────────────────────────

def _make_lane_for_prep_callback(fullname="AFC_stepper lane1"):
    """Build an AFCLane wired for prep_callback tests.

    Defaults every guard to its "satisfied" state (printer ready, prep done,
    status not TOOL_UNLOADING, hub not direct_load, homed, not printing) and
    prep_state/raw_load_state to False, so calling prep_callback with these
    defaults is a harmless no-op unless a test flips a specific condition.
    """
    lane = _make_afc_lane(fullname)
    lane.printer = MagicMock()
    lane.printer.state_message = 'Printer is ready'
    lane.mutex = MagicMock()
    lane._afc_prep_done = True
    lane.status = AFCLaneState.LOADED  # anything but TOOL_UNLOADING
    lane.prep_active = False
    lane.last_prep_time = 0
    # Real dict, not the MagicMock default -- _any_lane_prep_active() iterates it.
    # Starts with just this lane, itself not active.
    lane.afc.lanes = {lane.name: lane}
    lane._load_state = False
    lane.hub = "PB1"
    lane.td1_device_id = None
    lane.afc.auto_home = True
    lane.afc.function.is_homed = MagicMock(return_value=True)
    lane.afc.function.is_printing = MagicMock(return_value=False)
    lane.afc.TOOL_LOAD = MagicMock()
    lane.afc.spool._set_values = MagicMock()
    lane.afc.error.AFC_error = MagicMock()
    lane.afc.save_vars = MagicMock()
    lane.unit_obj.type = "BoxTurtle"
    lane.unit_obj.prep_load = MagicMock()
    lane.unit_obj.prep_post_load = MagicMock()
    lane.set_loaded = MagicMock()
    lane.do_enable = MagicMock()
    lane._post_prep_user_macro = MagicMock()
    lane._prep_capture_td1 = MagicMock()
    return lane


def _make_lane_ready_to_load(fullname="AFC_stepper lane1"):
    """Same as _make_lane_for_prep_callback, but pre-armed so calling
    prep_callback(eventtime=10, state=True) takes the load branch: prep_state
    True, raw_load_state False, delta_time >= 1.0, not printing."""
    lane = _make_lane_for_prep_callback(fullname)
    lane.last_prep_time = 0
    return lane


class TestPrepCallback:
    """Tests for AFCLane.prep_callback."""

    # ── prep_active re-entrancy guard ────────────────────────────────────

    def test_prep_active_returns_early_without_processing(self):
        lane = _make_lane_for_prep_callback()
        lane.prep_active = True
        lane.prep_callback(10, True)
        lane.unit_obj.prep_load.assert_not_called()
        lane.afc.save_vars.assert_not_called()

    def test_prep_active_early_return_still_sets_prep_state(self):
        """prep_state/last_prep_time are set before the prep_active check, so
        they update even when the rest of the method is skipped."""
        lane = _make_lane_for_prep_callback()
        lane.prep_active = True
        lane.last_prep_time = 5
        lane.prep_callback(10, True)
        assert lane.prep_state is True
        assert lane.last_prep_time == 10

    def test_prep_active_early_return_leaves_prep_active_true(self):
        lane = _make_lane_for_prep_callback()
        lane.prep_active = True
        lane.prep_callback(10, True)
        assert lane.prep_active is True
    
    def test_integer_state_is_normalized_to_bool(self):
        lane = _make_lane_for_prep_callback()
        lane.prep_active = True
        lane.prep_callback(10, 1)
        assert lane.prep_state is True
        lane.prep_callback(11, 0)
        assert lane.prep_state is False

    # ── "home printer before direct load" guard (5-way AND) ─────────────
    # Baseline where all 5 conditions are satisfied; each independence test
    # flips exactly one condition back to its "safe" value.

    def _make_lane_at_home_guard_boundary(self):
        lane = _make_lane_for_prep_callback()
        lane.printer.state_message = 'Printer is ready'
        lane._afc_prep_done = True
        lane.hub = "direct_load"
        lane.afc.auto_home = False
        lane.afc.function.is_homed.return_value = False
        return lane

    def test_home_guard_fires_when_all_conditions_met(self):
        lane = self._make_lane_at_home_guard_boundary()
        result = lane.prep_callback(10, True)
        assert result is None
        lane.afc.error.AFC_error.assert_called_once_with(
            "Please home printer before directly loading to toolhead", False
        )

    def test_home_guard_skipped_when_hub_is_none(self):
        """hub is None must not crash on "direct_load" in self.hub, and must
        skip the guard the same as any other unmatched hub value."""
        lane = self._make_lane_at_home_guard_boundary()
        lane.hub = None
        lane.prep_callback(10, True)
        lane.afc.error.AFC_error.assert_not_called()

    def test_home_guard_fire_skips_rest_of_method(self):
        lane = self._make_lane_at_home_guard_boundary()
        lane.prep_callback(10, True)
        lane.unit_obj.prep_load.assert_not_called()
        lane.afc.save_vars.assert_not_called()

    def test_home_guard_skipped_when_printer_not_ready(self):
        lane = self._make_lane_at_home_guard_boundary()
        lane.printer.state_message = 'not ready'
        lane.prep_callback(10, True)
        lane.afc.error.AFC_error.assert_not_called()

    def test_home_guard_skipped_when_prep_not_done(self):
        lane = self._make_lane_at_home_guard_boundary()
        lane._afc_prep_done = False
        lane.prep_callback(10, True)
        lane.afc.error.AFC_error.assert_not_called()

    def test_home_guard_skipped_when_hub_not_direct_load(self):
        lane = self._make_lane_at_home_guard_boundary()
        lane.hub = "PB1"
        lane.prep_callback(10, True)
        lane.afc.error.AFC_error.assert_not_called()

    def test_home_guard_skipped_when_auto_home_enabled(self):
        lane = self._make_lane_at_home_guard_boundary()
        lane.afc.auto_home = True
        lane.prep_callback(10, True)
        lane.afc.error.AFC_error.assert_not_called()

    def test_home_guard_skipped_when_already_homed(self):
        lane = self._make_lane_at_home_guard_boundary()
        lane.afc.function.is_homed.return_value = True
        lane.prep_callback(10, True)
        lane.afc.error.AFC_error.assert_not_called()

    # ── outer processing guard (3-way AND): printer ready, prep done, ───
    # ── status != TOOL_UNLOADING ─────────────────────────────────────────

    def test_outer_guard_skipped_when_printer_not_ready(self):
        lane = _make_lane_ready_to_load()
        lane.printer.state_message = 'not ready'
        lane.prep_callback(10, True)
        lane.unit_obj.prep_load.assert_not_called()

    def test_outer_guard_skipped_when_prep_not_done(self):
        lane = _make_lane_ready_to_load()
        lane._afc_prep_done = False
        lane.prep_callback(10, True)
        lane.unit_obj.prep_load.assert_not_called()

    def test_outer_guard_skipped_when_status_tool_unloading(self):
        lane = _make_lane_ready_to_load()
        lane.status = AFCLaneState.TOOL_UNLOADING
        lane.prep_callback(10, True)
        lane.unit_obj.prep_load.assert_not_called()

    # ── load-branch entry (2-way AND): prep_state, not raw_load_state ───

    def test_load_branch_skipped_when_prep_state_false(self):
        lane = _make_lane_ready_to_load()
        lane.prep_callback(10, False)
        lane.unit_obj.prep_load.assert_not_called()

    def test_load_branch_skipped_when_raw_load_state_true(self):
        lane = _make_lane_ready_to_load()
        lane._load_state = True
        lane.prep_callback(10, True)
        lane.unit_obj.prep_load.assert_not_called()

    # ── debounce: delta_time < 1.0 ───────────────────────────────────────

    def test_debounce_breaks_before_is_printing_check(self):
        lane = _make_lane_ready_to_load()
        lane.last_prep_time = 9.5
        lane.prep_callback(10, True)
        lane.afc.function.is_printing.assert_not_called()
        lane.unit_obj.prep_load.assert_not_called()

    def test_debounce_break_still_resets_prep_active_and_saves_vars(self):
        """break (not return) still falls through to the trailing
        prep_active reset and save_vars call."""
        lane = _make_lane_ready_to_load()
        lane.last_prep_time = 9.5
        lane.prep_callback(10, True)
        assert lane.prep_active is False
        lane.afc.save_vars.assert_called_once()

    def test_no_debounce_when_delta_time_is_one_or_more(self):
        lane = _make_lane_ready_to_load()
        lane.last_prep_time = 0
        lane.prep_callback(10, True)
        lane.afc.function.is_printing.assert_called_once_with(check_movement=True)

    # ── is_printing(check_movement=True) abort ───────────────────────────

    def test_is_printing_while_moving_aborts_load(self):
        lane = _make_lane_ready_to_load()
        lane.afc.function.is_printing.return_value = True
        lane.prep_callback(10, True)
        lane.afc.error.AFC_error.assert_called_once_with(
            "Cannot load lane1 spool while printer is actively moving or homing", False
        )
        lane.unit_obj.prep_load.assert_not_called()

    def test_is_printing_while_moving_sets_prep_active_false(self):
        lane = _make_lane_ready_to_load()
        lane.afc.function.is_printing.return_value = True
        lane.prep_callback(10, True)
        assert lane.prep_active is False

    def test_is_printing_while_moving_does_not_call_save_vars(self):
        """This path returns (not breaks), so it must skip the trailing
        save_vars() call unlike the debounce break path."""
        lane = _make_lane_ready_to_load()
        lane.afc.function.is_printing.return_value = True
        lane.prep_callback(10, True)
        lane.afc.save_vars.assert_not_called()

    # ── TTC guard: another lane already has a PREP cycle in flight ───────

    def test_another_lane_active_blocks_prep_load(self):
        lane = _make_lane_ready_to_load()
        lane.afc.lanes["lane2"] = MagicMock(prep_active=True)
        lane.prep_callback(10, True)
        lane.unit_obj.prep_load.assert_not_called()

    def test_another_lane_active_still_resets_prep_active(self):
        lane = _make_lane_ready_to_load()
        lane.afc.lanes["lane2"] = MagicMock(prep_active=True)
        lane.prep_callback(10, True)
        assert lane.prep_active is False

    def test_another_lane_active_does_not_call_save_vars(self):
        """Same shape as the is_printing guard above: this path returns
        (not breaks), so it must skip the trailing save_vars() call."""
        lane = _make_lane_ready_to_load()
        lane.afc.lanes["lane2"] = MagicMock(prep_active=True)
        lane.prep_callback(10, True)
        lane.afc.save_vars.assert_not_called()

    def test_another_lane_active_shows_error_message(self):
        """Per review feedback: this guard shares the same if statement (and
        message) as the is_printing check above, instead of returning
        silently -- the user must see why the load was refused."""
        lane = _make_lane_ready_to_load()
        lane.afc.lanes["lane2"] = MagicMock(prep_active=True)
        lane.prep_callback(10, True)
        lane.afc.error.AFC_error.assert_called_once_with(
            "Cannot load lane1 spool while printer is actively moving or homing", False
        )

    def test_another_lane_active_checked_together_with_is_printing(self):
        """Both conditions share one if/or now; is_printing being True is
        enough on its own, short-circuiting before the lanes check runs."""
        lane = _make_lane_ready_to_load()
        lane.afc.function.is_printing.return_value = True
        lane.afc.lanes["lane2"] = MagicMock(prep_active=True)
        lane.prep_callback(10, True)
        lane.afc.error.AFC_error.assert_called_once_with(
            "Cannot load lane1 spool while printer is actively moving or homing", False
        )

    def test_only_this_lane_in_afc_lanes_does_not_block_prep_load(self):
        """Baseline: with no other lane present, _any_lane_prep_active() must
        not block the lane's own load -- guards against a helper regression
        that would make every prep_callback test fail closed."""
        lane = _make_lane_ready_to_load()
        assert list(lane.afc.lanes.keys()) == ["lane1"]
        lane.prep_callback(10, True)
        lane.unit_obj.prep_load.assert_called_once_with(lane)

    # ── last_prep_activity_time: only set when a load actually starts ────
    # Per review feedback (CodeRabbit + Jimmy): recording this on every edge,
    # including releases, made handle_prep_runout's guard window always look
    # "recently active" for a release triggering itself -- moved past all the
    # guards above so only a load that's actually about to run counts.

    def test_release_edge_does_not_update_last_prep_activity_time(self):
        lane = _make_lane_ready_to_load()
        lane.afc.last_prep_activity_time = -100.0
        lane.prep_callback(10, False)
        assert lane.afc.last_prep_activity_time == -100.0

    def test_blocked_load_does_not_update_last_prep_activity_time(self):
        lane = _make_lane_ready_to_load()
        lane.afc.last_prep_activity_time = -100.0
        lane.afc.function.is_printing.return_value = True
        lane.prep_callback(10, True)
        assert lane.afc.last_prep_activity_time == -100.0

    def test_successful_load_updates_last_prep_activity_time(self):
        lane = _make_lane_ready_to_load()
        lane.afc.last_prep_activity_time = -100.0
        lane.prep_callback(10, True)
        assert lane.afc.last_prep_activity_time == 10

    # ── successful prep_load call ─────────────────────────────────────────

    def test_successful_load_calls_prep_load(self):
        lane = _make_lane_ready_to_load()
        lane.prep_callback(10, True)
        lane.unit_obj.prep_load.assert_called_once_with(lane)

    def test_successful_load_activates_loading_led_before_prep_load(self):
        lane = _make_lane_ready_to_load()
        order = []
        lane.unit_obj.lane_loading.side_effect = lambda *a, **kw: order.append("lane_loading")
        lane.unit_obj.prep_load.side_effect = lambda *a, **kw: order.append("prep_load")
        lane.prep_callback(10, True)
        lane.unit_obj.lane_loading.assert_called_once_with(lane)
        assert order == ["lane_loading", "prep_load"]

    def test_successful_load_resets_status_to_none(self):
        lane = _make_lane_ready_to_load()
        lane.status = AFCLaneState.LOADED
        lane.prep_callback(10, True)
        assert lane.status == AFCLaneState.NONE

    def test_successful_load_logs_debug_messages(self):
        lane = _make_lane_ready_to_load()
        lane.prep_callback(10, True)
        assert lane.logger.messages == [
            ("debug", "Prep: callback triggered lane1"),
            ("debug", "Prep: Load Done-lane1"),
        ]

    # ── direct_load hub vs normal hub (prep_state is guaranteed True here ─
    # ── by the enclosing load-branch guard, so only hub is independently ──
    # ── testable for this nested condition) ────────────────────────────

    def test_direct_load_hub_calls_tool_load(self):
        lane = _make_lane_ready_to_load()
        lane.hub = "direct_load"
        lane.afc.TOOL_LOAD.return_value = True
        lane.prep_callback(10, True)
        lane.afc.TOOL_LOAD.assert_called_once_with(lane)
        lane.afc.spool._set_values.assert_called_once_with(lane)
        lane.afc.afc_stats.increase_load_error_count.assert_not_called()

    def test_direct_load_hub_tool_load_failure_increases_load_error_count(self):
        """When TOOL_LOAD fails in the direct_load prep_callback branch, the
        failure is recorded via increase_load_error_count(self.afc)."""
        lane = _make_lane_ready_to_load()
        lane.hub = "direct_load"
        lane.afc.TOOL_LOAD.return_value = False
        lane.prep_callback(10, True)
        lane.afc.afc_stats.increase_load_error_count.assert_called_once_with(lane.afc)

    def test_direct_load_hub_breaks_before_prep_post_load(self):
        lane = _make_lane_ready_to_load()
        lane.hub = "direct_load"
        lane.prep_callback(10, True)
        lane.unit_obj.prep_post_load.assert_not_called()
        lane.do_enable.assert_not_called()

    def test_direct_load_hub_logs_debug_messages(self):
        lane = _make_lane_ready_to_load()
        lane.hub = "direct_load"
        lane.prep_callback(10, True)
        assert lane.logger.messages == [
            ("debug", "Prep: callback triggered lane1"),
            ("debug", "Prep: Load Done-lane1"),
            ("debug", "Prep: direct load logic-lane1-direct_load"),
            ("debug", "Prep: direct load logic done-lane1-direct_load"),
        ]

    def test_non_direct_load_hub_calls_prep_post_load_and_do_enable(self):
        lane = _make_lane_ready_to_load()
        lane.hub = "PB1"
        lane.prep_callback(10, True)
        lane.unit_obj.prep_post_load.assert_called_once_with(lane)
        lane.do_enable.assert_called_once_with(False)
        lane.afc.TOOL_LOAD.assert_not_called()

    def test_hub_is_none_does_not_crash_and_skips_tool_load(self):
        """hub is None must not raise on self.hub == 'direct_load', and must
        fall through to the normal prep_post_load path exactly like any
        other non-direct_load hub value."""
        lane = _make_lane_ready_to_load()
        lane.hub = None
        lane.prep_callback(10, True)
        lane.afc.TOOL_LOAD.assert_not_called()
        lane.unit_obj.prep_post_load.assert_called_once_with(lane)
        lane.do_enable.assert_called_once_with(False)

    # ── load_state and prep_state -> set_loaded (prep_state guaranteed ───
    # ── True here too; only load_state is independently testable) ────────

    def test_load_state_true_after_prep_load_calls_set_loaded(self):
        """Simulates prep_load() physically loading filament and the load
        sensor triggering as a result."""
        lane = _make_lane_ready_to_load()
        lane.hub = "PB1"
        lane.unit_obj.prep_load.side_effect = lambda _lane: setattr(_lane, "_load_state", True)
        lane.prep_callback(10, True)
        lane.set_loaded.assert_called_once()
        lane._post_prep_user_macro.assert_called_once()

    def test_load_state_false_after_prep_load_skips_set_loaded(self):
        lane = _make_lane_ready_to_load()
        lane.hub = "PB1"
        lane.prep_callback(10, True)
        lane.set_loaded.assert_not_called()
        lane._post_prep_user_macro.assert_not_called()

    # ── td1_device_id capture ─────────────────────────────────────────────

    def test_td1_device_id_set_captures_td1_data(self):
        lane = _make_lane_ready_to_load()
        lane.hub = "PB1"
        lane.td1_device_id = "td1-abc"
        lane.unit_obj.prep_load.side_effect = lambda _lane: setattr(_lane, "_load_state", True)
        lane.prep_callback(10, True)
        lane._prep_capture_td1.assert_called_once()

    def test_td1_device_id_none_skips_td1_capture(self):
        lane = _make_lane_ready_to_load()
        lane.hub = "PB1"
        lane.td1_device_id = None
        lane.unit_obj.prep_load.side_effect = lambda _lane: setattr(_lane, "_load_state", True)
        lane.prep_callback(10, True)
        lane._prep_capture_td1.assert_not_called()

    # ── elif: sensor stuck triggered (3-way AND). Reaching this elif with ─
    # ── any chance of firing structurally requires prep_state==True and ──
    # ── raw_load_state==True together (otherwise the if branch above ─────
    # ── fires instead, or prep_state==False short-circuits both) ─────────

    def test_sensor_stuck_message_fires_when_all_conditions_met(self):
        lane = _make_lane_ready_to_load()
        lane._load_state = True
        lane.afc.function.is_printing.return_value = False
        lane.prep_callback(10, True)
        lane.afc.error.AFC_error.assert_called_once_with(
            "Cannot load lane1 load sensor is triggered."
            "\n    Make sure filament is not stuck in load sensor or check to make sure "
            "load sensor is not stuck triggered."
            "\n    Once cleared try loading again",
            pause=False,
        )

    def test_sensor_stuck_message_skipped_when_printing(self):
        lane = _make_lane_ready_to_load()
        lane._load_state = True
        lane.afc.function.is_printing.return_value = True
        lane.prep_callback(10, True)
        lane.afc.error.AFC_error.assert_not_called()

    def test_neither_branch_fires_when_prep_state_false(self):
        lane = _make_lane_ready_to_load()
        lane._load_state = True
        lane.prep_callback(10, False)
        lane.unit_obj.prep_load.assert_not_called()
        lane.afc.error.AFC_error.assert_not_called()

    def test_sensor_stuck_message_includes_vivid_recovery_hint(self):
        lane = _make_lane_ready_to_load()
        lane._load_state = True
        lane.afc.function.is_printing.return_value = False
        lane.unit_obj.type = "ViViD"
        lane.prep_callback(10, True)
        lane.afc.error.AFC_error.assert_called_once_with(
            "Cannot load lane1 load sensor is triggered."
            "\n    Make sure filament is not stuck in load sensor or check to make sure "
            "load sensor is not stuck triggered."
            "\n    If filament is not stuck in sensor run AFC_RECOVER_LANE LANE=lane1 "
            "to reset internal AFC state."
            "\n    Once cleared try loading again",
            pause=False,
        )

    def test_sensor_stuck_message_excludes_vivid_hint_for_other_units(self):
        lane = _make_lane_ready_to_load()
        lane._load_state = True
        lane.afc.function.is_printing.return_value = False
        lane.unit_obj.type = "BoxTurtle"
        lane.prep_callback(10, True)
        called_msg = lane.afc.error.AFC_error.call_args[0][0]
        assert "AFC_RECOVER_LANE" not in called_msg

    # ── trailing prep_active reset / save_vars ────────────────────────────

    def test_prep_active_is_true_during_processing_then_reset(self):
        """prep_active must be True while the with-block is doing work
        (observed via a side effect mid-call), then reset to False before
        prep_callback returns."""
        lane = _make_lane_ready_to_load()
        observed = []
        def _record(_lane):
            observed.append(lane.prep_active)
        lane.unit_obj.prep_load.side_effect = _record
        lane.prep_callback(10, True)
        assert observed == [True]
        assert lane.prep_active is False

    def test_normal_completion_saves_vars(self):
        lane = _make_lane_ready_to_load()
        lane.prep_callback(10, True)
        lane.afc.save_vars.assert_called_once()

    def test_outer_guard_false_still_resets_prep_active_and_saves_vars(self):
        """Even when the outer processing guard never enters the with-block,
        the trailing reset/save still runs (it's outside the for/with)."""
        lane = _make_lane_ready_to_load()
        lane.printer.state_message = 'not ready'
        lane.prep_callback(10, True)
        assert lane.prep_active is False
        lane.afc.save_vars.assert_called_once()

    # ── TTC fix: cross-lane interaction via afc.lanes ──

    def test_prep_callback_and_handle_prep_runout_real_interaction(self):
        """End-to-end, no pre-set mock guard values: prove the two methods
        actually cooperate through the shared afc.lanes dict, not just that
        each behaves correctly in isolation against a hand-picked constant
        (as the TestHandlePrepRunout tests above do).

        Sequence: lane A's prep_callback is captured mid-cycle (via
        prep_load's side effect) with a second lane (B) releasing PREP at
        that exact moment — handle_prep_runout must be blocked. Once A's
        prep_callback finishes and self.prep_active is back to False, the
        same release event must now go through.
        """
        lane_a = _make_lane_ready_to_load(fullname="AFC_stepper lane1")

        from tests.conftest import MockReactor
        lane_b = _make_afc_lane("AFC_stepper lane2")
        lane_b.afc = lane_a.afc  # same shared afc object, real cross-lane setup
        lane_b.printer = lane_a.printer
        lane_b._afc_prep_done = True
        lane_b.status = "None"
        lane_b.name = "lane2"
        lane_b.prep_state = False
        lane_b.prep_active = False
        lane_b.prep_recheck_pending = False
        lane_b.afc.current = "lane1"  # not lane2, so mid-print branch's name check fails
        lane_b._load_state = False
        lane_b.runout_lane = None
        lane_b.set_unloaded = MagicMock()
        lane_b.fila_prep = MagicMock()
        lane_b.fila_prep.runout_helper.sensor_enabled = True
        lane_b.prep_debounce_button = MagicMock()
        lane_b.reactor = MockReactor()
        lane_b.reactor.register_callback = MagicMock()
        lane_a.afc.lanes = {lane_a.name: lane_a, lane_b.name: lane_b}

        blocked_result = []
        def _release_lane_b_mid_cycle(_lane):
            lane_b.handle_prep_runout(10.05, False)
            blocked_result.append(lane_b.set_unloaded.called)
        lane_a.unit_obj.prep_load.side_effect = _release_lane_b_mid_cycle

        lane_a.prep_callback(10.0, True)

        # While lane A's cycle was in flight, lane B's release was blocked.
        assert blocked_result == [False]
        # Lane A's cycle has since ended (prep_active back to False) and enough
        # wall-clock time will have passed by a later, unrelated release.
        lane_b.afc.last_prep_activity_time = -100.0
        lane_b.handle_prep_runout(10.1, False)
        lane_b.set_unloaded.assert_called_once()

    # ── TTC fix follow-up (CodeRabbit review on PR #827): exception-safe cleanup ──
    # prep_active is set True before prep_load() runs and was only reset in the
    # normal/early-return exit paths. If prep_load() (or anything else in the
    # active-cycle block) raised, it would stay stuck True permanently — meaning
    # every lane's handle_prep_runout stays gated forever (since it's aggregated
    # across lanes), not just this lane's own prep_callback re-entrancy guard.

    def test_exception_in_prep_load_still_resets_prep_active(self):
        lane = _make_lane_ready_to_load()
        lane.unit_obj.prep_load.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            lane.prep_callback(10, True)
        assert lane.prep_active is False

    def test_exception_in_prep_load_still_propagates(self):
        """The fix guarantees cleanup, not error suppression — the original
        exception must still reach the caller unchanged."""
        lane = _make_lane_ready_to_load()
        lane.unit_obj.prep_load.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError, match="boom"):
            lane.prep_callback(10, True)

    def test_exception_in_prep_load_skips_save_vars(self):
        """Matches the existing is_printing-abort behavior: a path that
        never reaches normal completion must not call save_vars() either."""
        lane = _make_lane_ready_to_load()
        lane.unit_obj.prep_load.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            lane.prep_callback(10, True)
        lane.afc.save_vars.assert_not_called()

    def test_exception_in_prep_load_does_not_block_other_lanes_afterward(self):
        """The concrete regression this whole fix is about: before it, a
        raised exception left prep_active stuck True, so a completely
        unrelated lane's genuine PREP release would stay gated forever.
        This proves that no longer happens."""
        lane_a = _make_lane_ready_to_load(fullname="AFC_stepper lane1")
        lane_a.unit_obj.prep_load.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            lane_a.prep_callback(10, True)

        from tests.conftest import MockReactor
        lane_b = _make_afc_lane("AFC_stepper lane2")
        lane_b.afc = lane_a.afc
        lane_b.printer = lane_a.printer
        lane_b._afc_prep_done = True
        lane_b.status = "None"
        lane_b.name = "lane2"
        lane_b.prep_state = False
        lane_b.prep_active = False
        lane_b.prep_recheck_pending = False
        lane_b.afc.current = "lane1"
        lane_b._load_state = False
        lane_b.runout_lane = None
        lane_b.set_unloaded = MagicMock()
        lane_b.fila_prep = MagicMock()
        lane_b.fila_prep.runout_helper.sensor_enabled = True
        lane_b.prep_debounce_button = MagicMock()
        lane_b.reactor = MockReactor()
        lane_b.reactor.register_callback = MagicMock()
        lane_b.afc.last_prep_activity_time = -100.0
        lane_a.afc.lanes = {lane_a.name: lane_a, lane_b.name: lane_b}

        lane_b.handle_prep_runout(20.0, False)
        lane_b.set_unloaded.assert_called_once()