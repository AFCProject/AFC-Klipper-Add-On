"""
Unit tests for extras/AFC_Claymore.py

Covers:
  - AFC_Claymore: is a subclass of AFC_vivid
  - AFC_Claymore.__init__: reads type from config
  - AFC_Claymore.handle_connect: calls super, sets Claymore logo strings
  - AFC_Claymore._move_lane: returns lane's current load_state
  - AFC_Claymore.system_Test: syncs prep_state, delegates to super with movement disabled
  - AFC_Claymore.unselect_lane: delegates to super with move_distance
  - AFC_Claymore.eject_lane: delegates to afcBoxTurtle.eject_lane
  - AFC_Claymore.move_to_hub: delegates to afcBoxTurtle.move_to_hub
  - AFC_Claymore.calibrate_lane: delegates to afcBoxTurtle.calibrate_lane
  - AFC_Claymore.calibration_lane_message: delegates to afcBoxTurtle.calibration_lane_message
  - load_config_prefix: Klipper config entry point
"""

from __future__ import annotations

import configparser
import importlib.util
import sys
from typing import Optional
from unittest.mock import MagicMock, patch
import pytest

from extras.AFC_Claymore import AFC_Claymore, load_config_prefix
from extras.AFC_vivid import AFC_vivid
from extras.AFC_BoxTurtle import afcBoxTurtle
from extras.AFC_lane import MoveDirection, SpeedMode, AssistActive, AFCMoveWarning


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_claymore_config(name: str="Claymore_1", values: Optional[dict]=None,
                          drive_stepper: str="drive", selector_stepper: str="selector"):
    """Build the MockConfig/MockPrinter/MockAFC trio needed to construct an
    AFC_Claymore via its real __init__ (chained through AFC_vivid.__init__,
    afcBoxTurtle.__init__ and afcUnit.__init__).

    Registers [AFC_stepper <drive_stepper>]/[AFC_stepper <selector_stepper>]
    config sections and matching MagicMock stepper objects in the printer's
    object cache so afcUnit._lookup_objects resolves drive_stepper_obj/
    selector_stepper_obj successfully.

    :param name: Unit name to use for the config section
    :param values: Config values to seed, merged over the defaults
    :param drive_stepper: Name of the drive AFC_stepper section to register
    :param selector_stepper: Name of the selector AFC_stepper section to register
    :return tuple: (config, printer, afc) mocks ready to build an AFC_Claymore
    """
    from tests.conftest import MockAFC, MockConfig, MockPrinter

    afc = MockAFC()
    printer = MockPrinter(afc=afc)

    all_values = {
        "drive_stepper": drive_stepper,
        "selector_stepper": selector_stepper,
    }
    if values:
        all_values.update(values)

    config = MockConfig(name=f"AFC_Claymore {name}", printer=printer, values=all_values)

    config.fileconfig.add_section(f"AFC_stepper {drive_stepper}")
    config.fileconfig.add_section(f"AFC_stepper {selector_stepper}")
    printer._objects[f"AFC_stepper {drive_stepper}"] = MagicMock()
    printer._objects[f"AFC_stepper {selector_stepper}"] = MagicMock()

    return config, printer, afc


def _make_claymore(name: str="Claymore_1", values: Optional[dict]=None) -> AFC_Claymore:
    """Build an AFC_Claymore instance via its real __init__.

    :param name: Unit name to use for the config section
    :param values: Config values to seed, merged over the defaults
    :return AFC_Claymore: constructed Claymore unit
    """
    config, printer, afc = _make_claymore_config(name=name, values=values)
    return AFC_Claymore(config)


# ── Inheritance ───────────────────────────────────────────────────────────────

class TestClaymoreInheritance:
    def test_is_subclass_of_vivid(self) -> None:
        assert issubclass(AFC_Claymore, AFC_vivid)

    def test_instance_is_vivid(self) -> None:
        unit = _make_claymore()
        assert isinstance(unit, AFC_vivid)


# ── __init__ ──────────────────────────────────────────────────────────────────

class TestClaymoreInit:
    def test_type_defaults_to_claymore_when_not_set_in_config(self) -> None:
        unit = _make_claymore()
        assert unit.type == "Claymore"

    def test_type_reads_overridden_value_from_config(self) -> None:
        unit = _make_claymore(values={"type": "CustomClaymore"})
        assert unit.type == "CustomClaymore"


# ── handle_connect ────────────────────────────────────────────────────────────

class TestClaymoreHandleConnect:
    def test_calls_super_handle_connect_once(self) -> None:
        unit = _make_claymore()
        with patch.object(AFC_vivid, "handle_connect") as mock_super:
            unit.handle_connect()
        mock_super.assert_called_once_with()

    def test_sets_logo_with_type_after_super_runs(self) -> None:
        """AFC_Claymore must overwrite whatever AFC_vivid.handle_connect set
        self.logo to, proving the override actually replaces (not just adds
        to) the inherited logo."""
        unit = _make_claymore()

        def fake_super_handle_connect(self) -> None:
            self.logo = "sentinel-from-vivid"
            self.logo_error = "sentinel-error-from-vivid"

        with patch.object(AFC_vivid, "handle_connect", fake_super_handle_connect):
            unit.handle_connect()

        assert unit.logo == f'<span class=success--text>{unit.type} Ready\n</span>'

    def test_sets_logo_error_with_type_after_super_runs(self) -> None:
        unit = _make_claymore()

        def fake_super_handle_connect(self) -> None:
            self.logo = "sentinel-from-vivid"
            self.logo_error = "sentinel-error-from-vivid"

        with patch.object(AFC_vivid, "handle_connect", fake_super_handle_connect):
            unit.handle_connect()

        assert unit.logo_error == f'<span class=error--text>{unit.type} Not Ready</span>\n'


# ── _move_lane ────────────────────────────────────────────────────────────────

class TestClaymoreMoveLane:
    def test_returns_true_when_lane_load_state_true(self) -> None:
        unit = _make_claymore()
        lane = MagicMock()
        lane.load_state = True
        assert unit._move_lane(lane) is True

    def test_returns_false_when_lane_load_state_false(self) -> None:
        unit = _make_claymore()
        lane = MagicMock()
        lane.load_state = False
        assert unit._move_lane(lane) is False


# ── system_Test ───────────────────────────────────────────────────────────────

class TestClaymoreSystemTest:
    def test_sets_prep_state_from_load_state_before_calling_super(self) -> None:
        unit = _make_claymore()
        lane = MagicMock()
        lane.load_state = True
        lane.prep_state = False

        def fake_super_system_test(self, cur_lane, delay, assignTcmd, enable_movement):
            # prep_state must already be synced by the time super() runs.
            assert cur_lane.prep_state is True
            return "ok"

        with patch.object(AFC_vivid, "system_Test", fake_super_system_test):
            unit.system_Test(lane, 0.5, True, True)

    def test_calls_super_with_movement_always_disabled(self) -> None:
        unit = _make_claymore()
        lane = MagicMock()
        lane.load_state = False

        with patch.object(AFC_vivid, "system_Test", return_value="ok") as mock_st:
            result = unit.system_Test(lane, 0.5, True, True)

        mock_st.assert_called_once_with(lane, 0.5, True, False)
        assert result == "ok"


# ── unselect_lane ─────────────────────────────────────────────────────────────

class TestClaymoreUnselectLane:
    def test_delegates_to_super_with_default_distance(self) -> None:
        unit = _make_claymore()
        with patch.object(AFC_vivid, "unselect_lane") as mock_super:
            unit.unselect_lane()
        mock_super.assert_called_once_with(move_distance=5)

    def test_delegates_to_super_with_custom_distance(self) -> None:
        unit = _make_claymore()
        with patch.object(AFC_vivid, "unselect_lane") as mock_super:
            unit.unselect_lane(move_distance=25)
        mock_super.assert_called_once_with(move_distance=25)


# ── eject_lane ────────────────────────────────────────────────────────────────

class TestClaymoreEjectLane:
    def test_delegates_to_box_turtle_eject_lane(self) -> None:
        unit = _make_claymore()
        lane = MagicMock()

        with patch.object(afcBoxTurtle, "eject_lane") as mock_eject:
            unit.eject_lane(lane)

        mock_eject.assert_called_once_with(unit, lane)


# ── move_to_hub ───────────────────────────────────────────────────────────────

class TestClaymoreMoveToHub:
    def test_delegates_to_box_turtle_move_to_hub_with_defaults(self) -> None:
        unit = _make_claymore()
        lane = MagicMock()

        with patch.object(afcBoxTurtle, "move_to_hub",
                          return_value=(True, 100.0, AFCMoveWarning.NONE)) as mock_move:
            result = unit.move_to_hub(lane, 100.0, MoveDirection.POS)

        mock_move.assert_called_once_with(unit, lane, 100.0, MoveDirection.POS, True,
                                          SpeedMode.HUB, AssistActive.DYNAMIC)
        assert result == (True, 100.0, AFCMoveWarning.NONE)

    def test_delegates_to_box_turtle_move_to_hub_with_overrides(self) -> None:
        unit = _make_claymore()
        lane = MagicMock()

        with patch.object(afcBoxTurtle, "move_to_hub",
                          return_value=(False, 50.0, AFCMoveWarning.WARN)) as mock_move:
            result = unit.move_to_hub(lane, 50.0, MoveDirection.NEG, use_homing=False,
                                      speed_mode=SpeedMode.SHORT,
                                      assist_active=AssistActive.NO)

        mock_move.assert_called_once_with(unit, lane, 50.0, MoveDirection.NEG, False,
                                          SpeedMode.SHORT, AssistActive.NO)
        assert result == (False, 50.0, AFCMoveWarning.WARN)


# ── calibrate_lane ────────────────────────────────────────────────────────────

class TestClaymoreCalibrateLane:
    def test_delegates_to_box_turtle_calibrate_lane(self) -> None:
        unit = _make_claymore()
        lane = MagicMock()
        gcmd = MagicMock()

        with patch.object(afcBoxTurtle, "calibrate_lane",
                          return_value=(True, "calibration_lane", 123.0)) as mock_cal:
            result = unit.calibrate_lane(lane, 0.0, gcmd)

        mock_cal.assert_called_once_with(unit, lane, 0.0, gcmd)
        assert result == (True, "calibration_lane", 123.0)


# ── calibration_lane_message ─────────────────────────────────────────────────

class TestClaymoreCalibrationLaneMessage:
    def test_delegates_to_box_turtle_calibration_lane_message(self) -> None:
        unit = _make_claymore()

        with patch.object(afcBoxTurtle, "calibration_lane_message",
                          return_value="box turtle message") as mock_msg:
            result = unit.calibration_lane_message()

        mock_msg.assert_called_once_with(unit)
        assert result == "box turtle message"


# ── load_config_prefix ────────────────────────────────────────────────────────

class TestClaymoreLoadConfigPrefix:
    def test_returns_afc_claymore_instance(self) -> None:
        config, printer, afc = _make_claymore_config()

        unit = load_config_prefix(config)

        assert isinstance(unit, AFC_Claymore)

    def test_passes_config_through_to_constructed_unit(self) -> None:
        config, printer, afc = _make_claymore_config(values={"type": "CustomClaymore"})

        unit = load_config_prefix(config)

        assert unit.type == "CustomClaymore"


# ═════════════════════════════════════════════════════════════════════════
# Module-level import guards
# ═════════════════════════════════════════════════════════════════════════

def _exec_afc_claymore_with_blocked_dependency(blocked_module_name: str) -> None:
    """Execute a throw-away copy of extras/AFC_Claymore.py's module-level
    code with `blocked_module_name` forced to fail import, to exercise the
    file's top-level ``try: from X import Y / except: raise config_error(...)``
    guards.

    Never touches the real, already-imported ``extras.AFC_Claymore`` module
    that the rest of this test suite depends on: the copy is loaded under a
    throwaway module name and discarded afterward, whether or not it raises.
    Cleanup restores the exact same pre-existing module object in
    sys.modules so other test files' references stay valid.

    :param blocked_module_name: Dotted module name to force an ImportError for
    """
    import extras.AFC_Claymore as real_module
    fresh_name = "extras.AFC_Claymore_import_guard_probe"
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
    """Covers the four module-level `try/except: raise config_error(...)`
    guards in AFC_Claymore.py, one per dependency import."""

    def test_afc_utils_error_str_import_failure_raises_configparser_error(self) -> None:
        """The first guard imports ERROR_STR itself from AFC_utils, so it
        can't use ERROR_STR.format(...) in its own except clause."""
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_claymore_with_blocked_dependency("extras.AFC_utils")
        assert str(exc_info.value).startswith(
            "Error when trying to import AFC_utils.ERROR_STR"
        )

    def test_afc_lane_import_failure_raises_configparser_error(self) -> None:
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_claymore_with_blocked_dependency("extras.AFC_lane")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_lane, please rerun install-afc.sh"
        )

    def test_afc_vivid_import_failure_raises_configparser_error(self) -> None:
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_claymore_with_blocked_dependency("extras.AFC_vivid")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_vivid, please rerun install-afc.sh"
        )

    def test_afc_boxturtle_import_failure_raises_configparser_error(self) -> None:
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_claymore_with_blocked_dependency("extras.AFC_BoxTurtle")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_BoxTurtle, please rerun install-afc.sh"
        )
