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
    lane.map = "T0"
    lane.gcode = MagicMock()
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
        lane_b.endstops.update({AFCHomingPoints.LOAD: {"endstop" : "PIN123", "endstop_name": "laneB_load"}})

        assert lane_a.load_es != lane_b.load_es

class TestAFCLaneIndexProperty:
    def test_lane_index_property_map_none(self):
        lane = _make_afc_lane()
        lane.map = None
        assert lane.lane_index == ""
    
    def test_lane_index_property_map_set(self):
        lane = _make_afc_lane()
        lane.map = "T5"
        assert lane.lane_index == "5"
    
    def test_lane_index_property_is_str(self):
        lane = _make_afc_lane()
        lane.map = "T5"
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
    lane.map = map_value
    lane.extruder_obj = MagicMock()
    lane.extruder_obj.th_extruder_name = lane.extruder_obj.name = extruder_name
    lane.color = "#FF0000"
    lane._material = "PLA"
    lane.bed_temp = 60
    lane.extruder_temp = 210
    lane.td1_data = {"scan_time": "1.23", "td": "0.95"}
    lane.spool_id = "abc123"
    lane.weight = 750.0
    return lane


def _get_sent_payload(lane):
    """Call send_lane_data and return the payload passed to moonraker."""
    lane.send_lane_data()
    lane.afc.moonraker.send_lane_data.assert_called_once()
    return lane.afc.moonraker.send_lane_data.call_args[0][0]


def _get_cleared_payload(lane):
    """Call clear_lane_data and return the payload passed to moonraker."""
    lane.clear_lane_data()
    lane.afc.moonraker.send_lane_data.assert_called_once()
    return lane.afc.moonraker.send_lane_data.call_args[0][0]


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

    def test_no_send_when_map_is_none(self):
        lane = _make_lane_for_moonraker()
        lane.map = None
        lane.send_lane_data()
        lane.afc.moonraker.send_lane_data.assert_not_called()

    def test_no_send_when_map_has_no_T(self):
        lane = _make_lane_for_moonraker()
        lane.map = "0"
        lane.send_lane_data()
        lane.afc.moonraker.send_lane_data.assert_not_called()


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

    def test_color_is_cleared(self):
        lane = _make_lane_for_moonraker()
        payload = _get_cleared_payload(lane)
        assert payload["value"]["color"] == ""

    def test_no_clear_when_map_is_none(self):
        lane = _make_lane_for_moonraker()
        lane.map = None
        lane.clear_lane_data()
        lane.afc.moonraker.send_lane_data.assert_not_called()


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
        lane.unit_obj.lane_not_ready.assert_called_with(lane)
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
        lane.gcode.run_script_from_command.assert_called_with(f"SET_MAP LANE={lane2.name} MAP={lane.map}")
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