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

from unittest.mock import MagicMock
import pytest

from extras.AFC_lane import (
    SpeedMode,
    AssistActive,
    MoveDirection,
    AFCLaneState,
    AFCHomingPoints,
    AFCLane,
    EXCLUDE_TYPES,
    AFCMoveWarning
)
from extras.AFC_stepper import AFCExtruderStepper


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


# ── AFCLane initialization ────────────────────────────────────────────────────
# AFCLane.__init__ is tightly coupled to Klipper's runtime; we bypass it with
# __new__ and set attributes to their documented initial values.

def _make_afc_lane(fullname="AFC_stepper lane1"):
    """Build an AFCLane bypassing the complex __init__."""
    lane = AFCLane.__new__(AFCLane)
    parts = fullname.split()
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
        lane_a.endstops.update({AFCHomingPoints.LOAD: {"endstop" : "PIN123", "endstop_name": "laneB_load"}})

        assert lane_a.load_es != lane_b.load_es


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
        homed, dist, warn = lane.move_to(100, SpeedMode.SHORT, AFCHomingPoints.BUFFER,
                                          False, False)
        assert homed == False
        assert dist == 0
        assert warn == AFCMoveWarning.ERROR
    
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