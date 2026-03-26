"""
Unit tests for extras/AFC_vivid.py

Covers:
  - AFC_vivid: class constants
  - _get_lane_selector_state: returns correct bool from fila_selector
  - _get_selector_enabled: returns stepper enabled status
  - calibration_lane_message: returns informative string
  - cmd_AFC_SELECT_LANE: dispatches to select_lane or gcmd.error
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from extras.AFC_vivid import AFC_vivid
from extras.AFC_BoxTurtle import afcBoxTurtle


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_vivid(name="ViViD_1"):
    """Build an AFC_vivid bypassing the complex __init__."""
    unit = AFC_vivid.__new__(AFC_vivid)

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
    unit.full_name = ["AFC_vivid", name]
    unit.lanes = {}
    unit.hub_obj = None
    unit.extruder_obj = None
    unit.buffer_obj = None
    unit.hub = None
    unit.extruder = None
    unit.buffer_name = None
    unit.td1_defined = False
    unit.type = "ViViD"
    unit.gcode = afc.gcode
    unit.drive_stepper = "drive_stepper"
    unit.selector_stepper = "selector_stepper"
    unit.drive_stepper_obj = MagicMock()
    unit.selector_stepper_obj = MagicMock()
    unit.current_selected_lane = None
    unit.home_state = False
    unit.prep_homed = False
    unit.failed_to_home = False
    unit.selector_homing_speed = 150
    unit.selector_homing_accel = 150
    unit.max_selector_movement = 800

    return unit


def _make_lane(name="lane1", has_selector=True):
    lane = MagicMock()
    lane.name = name
    lane.selector_endstop = "selector_pin" if has_selector else None
    lane.selector_endstop_name = "lane1_selector"
    lane.load_endstop_name = "lane1_load"
    lane.prep_endstop_name = "lane1_prep"
    lane.dist_hub = 200
    lane.calibrated_lane = True
    if has_selector:
        lane.fila_selector = MagicMock()
        lane.fila_selector.get_status.return_value = {"filament_detected": False}
    else:
        del lane.fila_selector  # make attribute not exist
    return lane


# ── Class constants ───────────────────────────────────────────────────────────

class TestVivdConstants:
    def test_valid_cam_angles(self):
        assert AFC_vivid.VALID_CAM_ANGLES == [30, 45, 60]

    def test_calibration_distance(self):
        assert AFC_vivid.CALIBRATION_DISTANCE == 5000

    def test_lane_overshoot(self):
        assert AFC_vivid.LANE_OVERSHOOT == 200

    def test_is_subclass_of_box_turtle(self):
        assert issubclass(AFC_vivid, afcBoxTurtle)

# ── _move_lane ──────────────────────────────────────────────────
class Test_MoveLane:
    def test_returns_prep_true_filament_loaded(self):
        unit = _make_vivid()
        lane = _make_lane(has_selector=True)
        lane.prep_state = True
        lane.spool_id = 10
        lane.remember_spool = True
        lane.tool_loaded = False
        lane.move_to.return_value = (True, 100.0, False)
        result = unit._move_lane(lane, 1, True)
        assert result is True
        assert lane.spool_id == 10
    
    def test_returns_prep_true_filament_not_loaded(self):
        unit = _make_vivid()
        lane = _make_lane(has_selector=True)
        lane.prep_state = True
        lane.loaded_to_hub = True
        lane.spool_id = 10
        lane.remember_spool = True
        lane.tool_loaded = False
        lane.move_to.return_value = (False, 100.0, False)
        result = unit._move_lane(lane, 1, True)
        assert result is False
        assert lane.tool_loaded is False
        assert lane.loaded_to_hub is False
        assert lane.spool_id == 10
    
    def test_returns_prep_false_filament_not_loaded(self):
        from extras.AFC_spool import AFCSpool
        unit = _make_vivid()
        lane = _make_lane(has_selector=True)
        lane.afc.spool = AFCSpool.__new__(AFCSpool)
        lane.prep_state = False
        lane.loaded_to_hub = True
        lane.spool_id = 10
        lane.remember_spool = True
        lane.tool_loaded = False
        lane.move_to.return_value = (False, 100.0, False)
        result = unit._move_lane(lane, 1, True)
        assert result is False
        assert lane.tool_loaded is False
        assert lane.loaded_to_hub is False
        assert lane.spool_id == 10
    
    def test_returns_prep_false_filament_not_loaded_not_remember_spool(self):
        from extras.AFC_spool import AFCSpool
        unit = _make_vivid()
        lane = _make_lane(has_selector=True)
        lane.afc.spool = AFCSpool.__new__(AFCSpool)
        lane.prep_state = False
        lane.loaded_to_hub = True
        lane.spool_id = 10
        lane.remember_spool = False
        lane.tool_loaded = False
        lane.move_to.return_value = (False, 100.0, False)
        result = unit._move_lane(lane, 1, True)
        assert result is False
        assert lane.tool_loaded is False
        assert lane.loaded_to_hub is False
        assert lane.spool_id is None

# ── _get_lane_selector_state ──────────────────────────────────────────────────

class TestGetLaneSelectorState:
    def test_returns_filament_detected_when_selector_present(self):
        unit = _make_vivid()
        lane = _make_lane(has_selector=True)
        lane.fila_selector.get_status.return_value = {"filament_detected": True}
        result = unit._get_lane_selector_state(lane)
        assert result is True

    def test_returns_false_when_selector_not_detected(self):
        unit = _make_vivid()
        lane = _make_lane(has_selector=True)
        lane.fila_selector.get_status.return_value = {"filament_detected": False}
        result = unit._get_lane_selector_state(lane)
        assert result is False

    def test_returns_false_when_no_selector_attribute(self):
        unit = _make_vivid()
        lane = MagicMock(spec=[])  # No attributes
        result = unit._get_lane_selector_state(lane)
        assert result is False


# ── _get_selector_enabled ─────────────────────────────────────────────────────

class TestGetSelectorEnabled:
    def test_returns_true_when_stepper_enabled(self):
        unit = _make_vivid()
        stepper_enable = MagicMock()
        stepper_enable.get_status.return_value = {
            "steppers": {f"AFC_stepper {unit.selector_stepper}": True}
        }
        unit.printer._objects["stepper_enable"] = stepper_enable
        result = unit._get_selector_enabled()
        assert result is True

    def test_returns_false_when_stepper_not_found(self):
        unit = _make_vivid()
        # No stepper_enable in printer objects → returns False
        unit.printer._objects = {}
        result = unit._get_selector_enabled()
        assert result is False


# ── calibration_lane_message ──────────────────────────────────────────────────

class TestCalibrationLaneMessage:
    def test_returns_non_empty_string(self):
        unit = _make_vivid()
        msg = unit.calibration_lane_message()
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_message_mentions_reinsert(self):
        unit = _make_vivid()
        msg = unit.calibration_lane_message()
        assert "reinsert" in msg.lower() or "insert" in msg.lower()

    def test_message_mentions_vivid(self):
        unit = _make_vivid()
        msg = unit.calibration_lane_message()
        assert "vivid" in msg.lower() or "ViViD" in msg


# ── cmd_AFC_SELECT_LANE ────────────────────────────────────────────────────────

class TestCmdAfcSelectLane:
    def test_calls_select_lane_when_lane_found(self):
        unit = _make_vivid()
        lane = _make_lane("lane1")
        unit.afc.lanes = {"lane1": lane}
        unit.select_lane = MagicMock(return_value=(True, 15.0))
        gcmd = MagicMock()
        gcmd.get.return_value = "lane1"
        unit.cmd_AFC_SELECT_LANE(gcmd)
        unit.select_lane.assert_called_once_with(lane)

    def test_logs_success_when_homed(self):
        unit = _make_vivid()
        lane = _make_lane("lane1")
        unit.afc.lanes = {"lane1": lane}
        unit.select_lane = MagicMock(return_value=(True, 15.0))
        gcmd = MagicMock()
        gcmd.get.return_value = "lane1"
        unit.cmd_AFC_SELECT_LANE(gcmd)
        info_msgs = [m for lvl, m in unit.logger.messages if lvl == "info"]
        assert any("lane1" in m for m in info_msgs)

    def test_logs_error_when_homing_fails(self):
        unit = _make_vivid()
        lane = _make_lane("lane1")
        unit.afc.lanes = {"lane1": lane}
        unit.select_lane = MagicMock(return_value=(False, 0))
        gcmd = MagicMock()
        gcmd.get.return_value = "lane1"
        unit.cmd_AFC_SELECT_LANE(gcmd)
        error_msgs = [m for lvl, m in unit.logger.messages if lvl == "error"]
        assert any("lane1" in m for m in error_msgs)

    def test_calls_gcmd_error_when_lane_not_found(self):
        unit = _make_vivid()
        unit.afc.lanes = {}
        gcmd = MagicMock()
        gcmd.get.return_value = "missing_lane"
        unit.cmd_AFC_SELECT_LANE(gcmd)
        gcmd.error.assert_called()


# ── handle_connect ────────────────────────────────────────────────────────────

class TestVividHandleConnect:
    def test_handle_connect_sets_logo(self):
        unit = _make_vivid()
        unit.set_logo_color = MagicMock()
        unit.handle_connect()
        assert "ViViD Ready" in unit.logo

    def test_handle_connect_sets_logo_error(self):
        unit = _make_vivid()
        unit.set_logo_color = MagicMock()
        unit.handle_connect()
        assert "ViViD Not Ready" in unit.logo_error

    def test_handle_connect_registers_unit_in_afc(self):
        unit = _make_vivid(name="vivid_1")
        unit.set_logo_color = MagicMock()
        unit.handle_connect()
        assert unit.afc.units.get("vivid_1") is unit


# ── system_Test ───────────────────────────────────────────────────────────────

class TestVividSystemTest:
    def test_system_test_calls_super_with_movement_disabled(self):
        unit = _make_vivid()
        lane = MagicMock()
        with MagicMock() as super_mock:
            from extras.AFC_BoxTurtle import afcBoxTurtle
            with MagicMock() as patch_target:
                from unittest.mock import patch
                with patch.object(afcBoxTurtle, 'system_Test', return_value="ok") as mock_st:
                    result = unit.system_Test(lane, 0.5, True, True)
                    mock_st.assert_called_once_with(lane, 0.5, True, enable_movement=False)


# ── prep_post_load ────────────────────────────────────────────────────────────

class TestPrepPostLoad:
    def test_returns_none(self):
        unit = _make_vivid()
        lane = MagicMock()
        result = unit.prep_post_load(lane)
        assert result is None


# ── unselect_lane ─────────────────────────────────────────────────────────────

class TestUnselectLane:
    def test_calls_selector_stepper_move(self):
        unit = _make_vivid()
        unit.unselect_lane()
        unit.selector_stepper_obj.move.assert_called_once_with(50, 100, 100, False)


# ── move_to_hub ───────────────────────────────────────────────────────────────

class TestMoveToHub:
    def test_delegates_to_lane_move_to(self):
        from extras.AFC_lane import SpeedMode, MoveDirection, AssistActive, AFCMoveWarning
        unit = _make_vivid()
        lane = MagicMock()
        lane.move_to.return_value = (True, 100.0, AFCMoveWarning.NONE)
        lane.load_es = "load_endstop"
        result = unit.move_to_hub(lane, 100.0, MoveDirection.POS)
        lane.move_to.assert_called_once()
        assert result == (True, 100.0, AFCMoveWarning.NONE)

    def test_returns_homed_distance_warn_tuple(self):
        from extras.AFC_lane import SpeedMode, MoveDirection, AFCMoveWarning
        unit = _make_vivid()
        lane = MagicMock()
        lane.move_to.return_value = (False, 50.0, AFCMoveWarning.WARN)
        lane.load_es = "load_endstop"
        homed, dist, warn = unit.move_to_hub(lane, 50.0, MoveDirection.NEG)
        assert homed is False
        assert warn is AFCMoveWarning.WARN


# ── select_lane ───────────────────────────────────────────────────────────────

class TestSelectLane:
    def test_returns_none_when_no_selector_endstop(self):
        unit = _make_vivid()
        lane = _make_lane("lane1", has_selector=False)
        lane.selector_endstop_name = None
        result = unit.select_lane(lane)
        assert result is None

    def test_returns_true_and_zero_when_already_selected_and_enabled(self):
        unit = _make_vivid()
        lane = _make_lane("lane1", has_selector=True)
        lane.fila_selector.get_status.return_value = {"filament_detected": True}
        # stepper enabled
        stepper_enable = MagicMock()
        stepper_enable.get_status.return_value = {
            "steppers": {f"AFC_stepper {unit.selector_stepper}": True}
        }
        unit.printer._objects["stepper_enable"] = stepper_enable
        result = unit.select_lane(lane)
        assert result == (True, 0.0)

    def test_calls_homing_when_not_selected(self):
        unit = _make_vivid()
        lane = _make_lane("lane1", has_selector=True)
        lane.fila_selector.get_status.return_value = {"filament_detected": False}
        unit.printer._objects = {}  # no stepper_enable → enabled=False
        unit.selector_stepper_obj.do_homing_move.return_value = (True, 15.0)
        homed, dist = unit.select_lane(lane)
        assert homed is True
        assert dist == 15.0
        unit.selector_stepper_obj.do_homing_move.assert_called_once()

    def test_calls_unselect_lane_when_disabled_but_selector_triggered(self):
        """When stepper not enabled but selector triggered, unselect_lane is called first."""
        unit = _make_vivid()
        lane = _make_lane("lane1", has_selector=True)
        lane.fila_selector.get_status.return_value = {"filament_detected": True}
        # stepper disabled (no stepper_enable object)
        unit.printer._objects = {}
        unit.unselect_lane = MagicMock()
        unit.selector_stepper_obj.do_homing_move.return_value = (True, 10.0)
        unit.select_lane(lane)
        unit.unselect_lane.assert_called_once()


# ── calibrate_lane ────────────────────────────────────────────────────────────

class TestCalibrateLane:
    def test_returns_correct_tuple(self):
        from extras.AFC_lane import AFCLaneState
        unit = _make_vivid()
        lane = MagicMock()
        unit.eject_lane = MagicMock()
        result = unit.calibrate_lane(lane, 0)
        assert result == (True, "calibration_lane", 0)

    def test_sets_loaded_to_hub_false(self):
        unit = _make_vivid()
        lane = MagicMock()
        unit.eject_lane = MagicMock()
        unit.calibrate_lane(lane, 0)
        assert lane.loaded_to_hub is False

    def test_sets_calibrated_lane_false(self):
        unit = _make_vivid()
        lane = MagicMock()
        unit.eject_lane = MagicMock()
        unit.calibrate_lane(lane, 0)
        assert lane.calibrated_lane is False

    def test_calls_eject_lane(self):
        unit = _make_vivid()
        lane = MagicMock()
        unit.eject_lane = MagicMock()
        unit.calibrate_lane(lane, 5.0)
        unit.eject_lane.assert_called_once_with(lane)


# ── _get_selector_enabled except branch ───────────────────────────────────────

class TestGetSelectorEnabledExceptBranch:
    def test_returns_false_when_stepper_key_missing_from_steppers(self):
        """Covers the except branch when the specific stepper key is absent."""
        unit = _make_vivid()
        stepper_enable = MagicMock()
        stepper_enable.get_status.return_value = {"steppers": {}}  # key not present
        unit.printer._objects["stepper_enable"] = stepper_enable
        result = unit._get_selector_enabled()
        assert result is False


# ── prep_load ─────────────────────────────────────────────────────────────────

class TestPrepLoad:
    def test_calibrated_lane_sets_loaded_to_hub(self):
        from unittest.mock import PropertyMock
        unit = _make_vivid()
        lane = MagicMock()
        lane.calibrated_lane = True
        lane.dist_hub = 200.0
        lane.move_to.return_value = (True, 200.0, False)
        unit.lane_loading = MagicMock()
        unit.select_lane = MagicMock()
        unit.lane_loaded = MagicMock()
        type(lane).raw_load_state = PropertyMock(side_effect=[False, True])

        unit.prep_load(lane)

        assert lane.loaded_to_hub is True
        unit.lane_loading.assert_called_once_with(lane)
        unit.select_lane.assert_called_once_with(lane, sel_prep=True)
        unit.lane_loaded.assert_called_once_with(lane)

    def test_disables_steppers_and_selects_loaded_lane(self):
        unit = _make_vivid()
        lane = MagicMock()
        lane.calibrated_lane = True
        lane.dist_hub = 200.0
        lane.move_to.return_value = (True, 200.0, False)
        unit.lane_loading = MagicMock()
        unit.select_lane = MagicMock()
        unit.lane_loaded = MagicMock()

        unit.prep_load(lane)

        unit.selector_stepper_obj.do_enable.assert_called_with(False)
        unit.drive_stepper_obj.do_enable.assert_called_with(False)
        unit.afc.function.select_loaded_lane.assert_called_once()

    def test_uncalibrated_lane_updates_dist_hub_and_config(self):
        from unittest.mock import PropertyMock
        unit = _make_vivid()
        lane = MagicMock()
        lane.calibrated_lane = False
        lane.prep_state = True
        lane.move_to.return_value = (True, 300.0, False)
        unit.lane_loading = MagicMock()
        unit.select_lane = MagicMock()
        unit.lane_loaded = MagicMock()
        type(lane).raw_load_state = PropertyMock(side_effect=[False, True])

        unit.prep_load(lane)

        assert lane.calibrated_lane is True
        assert lane.dist_hub == round(300.0, 2) + AFC_vivid.LANE_OVERSHOOT
        unit.afc.function.ConfigRewrite.assert_called()
    
    def test_uncalibrated_lane_updates_dist_hub_and_config_two_tries(self):
        from unittest.mock import PropertyMock
        unit = _make_vivid()
        lane = MagicMock()
        lane.calibrated_lane = False
        lane.prep_state = True
        lane.move_to.return_value = (True, 300.0, False)
        unit.lane_loading = MagicMock()
        unit.select_lane = MagicMock()
        unit.lane_loaded = MagicMock()
        type(lane).raw_load_state = PropertyMock(side_effect=[False, False, True])

        unit.prep_load(lane)

        assert lane.calibrated_lane is True
        assert lane.dist_hub == round(300.0, 2) + AFC_vivid.LANE_OVERSHOOT
        unit.afc.function.ConfigRewrite.assert_called()
    
    def test_uncalibrated_lane_updates_dist_hub_and_config_failed(self):
        from unittest.mock import PropertyMock
        unit = _make_vivid()
        lane = MagicMock()
        lane.calibrated_lane = False
        lane.prep_state = True
        lane.move_to.return_value = (False, 300.0, False)
        lane.dist_hub = 0.0
        unit.lane_loading = MagicMock()
        unit.select_lane = MagicMock()
        unit.lane_loaded = MagicMock()
        type(lane).raw_load_state = PropertyMock(side_effect=[False, False, False])

        unit.prep_load(lane)

        assert lane.calibrated_lane is False
        assert lane.dist_hub == 0.0
        unit.afc.function.ConfigRewrite.assert_not_called()
        # Failure should be reported/logged
        error_msgs = [m for lvl, m in unit.logger.messages if lvl == "error"]
        assert error_msgs
    
    def test_uncalibrated_lane_updates_dist_hub_no_prep(self):
        from unittest.mock import PropertyMock
        unit = _make_vivid()
        lane = MagicMock()
        lane.calibrated_lane = False
        lane.prep_state = False
        lane.move_to.return_value = (True, 300.0, False)
        unit.lane_loading = MagicMock()
        unit.select_lane = MagicMock()
        unit.lane_loaded = MagicMock()
        type(lane).raw_load_state = PropertyMock(side_effect=[False])

        unit.prep_load(lane)

        assert lane.calibrated_lane is False

    def test_not_homed_skips_lane_loaded(self):
        unit = _make_vivid()
        lane = MagicMock()
        lane.calibrated_lane = True
        lane.dist_hub = 200.0
        lane.move_to.return_value = (False, 0.0, False)
        unit.lane_loading = MagicMock()
        unit.select_lane = MagicMock()
        unit.lane_loaded = MagicMock()

        unit.prep_load(lane)

        unit.lane_loaded.assert_not_called()
        # Steppers are disabled regardless of homing result
        unit.selector_stepper_obj.do_enable.assert_called_with(False)


# ── eject_lane ────────────────────────────────────────────────────────────────

class TestEjectLane:
    def test_calls_select_and_unselect(self):
        unit = _make_vivid()
        lane = MagicMock()
        lane.dist_hub = 200.0
        unit.select_lane = MagicMock()
        unit.unselect_lane = MagicMock()

        unit.eject_lane(lane)

        unit.select_lane.assert_called_once_with(lane)
        unit.unselect_lane.assert_called_once()

    def test_disables_steppers(self):
        unit = _make_vivid()
        lane = MagicMock()
        lane.dist_hub = 200.0
        unit.select_lane = MagicMock()
        unit.unselect_lane = MagicMock()

        unit.eject_lane(lane)

        unit.selector_stepper_obj.do_enable.assert_called_with(False)
        unit.drive_stepper_obj.do_enable.assert_called_with(False)

    def test_adjusts_distance_when_dist_hub_over_400(self):
        from extras.AFC_lane import MoveDirection
        unit = _make_vivid()
        lane = MagicMock()
        lane.hub_obj = MagicMock()
        lane.hub_obj.hub_clear_move_dis = 65
        lane.dist_hub = 600.0  # > 400 → should subtract LANE_OVERSHOOT+100
        unit.select_lane = MagicMock()
        unit.unselect_lane = MagicMock()

        unit.eject_lane(lane)

        expected_dist = (600.0 - (AFC_vivid.LANE_OVERSHOOT + 100) - \
                         lane.hub_obj.hub_clear_move_dis - lane.homing_overshoot) * MoveDirection.NEG
        call_args = lane.move_to.call_args[0]
        assert call_args[0] == expected_dist

    def test_does_not_adjust_distance_when_dist_hub_at_or_below_400(self):
        from extras.AFC_lane import MoveDirection
        unit = _make_vivid()
        lane = MagicMock()
        lane.dist_hub = 300.0  # <= 400 → full dist used
        unit.select_lane = MagicMock()
        unit.unselect_lane = MagicMock()

        unit.eject_lane(lane)

        expected_dist = 300.0 * MoveDirection.NEG
        call_args = lane.move_to.call_args[0]
        assert call_args[0] == expected_dist
