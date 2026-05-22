"""
Unit tests for extras/AFC_EMU.py

Covers every method defined in AFC_EMU.py:
  - AFC_EMU.__init__: sets self.type from config (defaults to 'EMU')
  - AFC_EMU.handle_connect: sets EMU-specific logo strings
  - AFC_EMU.prep_post_load:
      Virtual-pin path requires lane.hub_obj non-None AND hasattr(self.hub_obj,
      'is_virtual_pin') AND lane.hub_obj.is_virtual_pin() True.  When all three
      hold, move_to is retried while lane.raw_load_state is True, capped at 10
      tries, then loaded_to_hub is set True.  Any other combination delegates to
      super().prep_post_load().
  - AFC_EMU.move_to_hub: same three-part virtual-pin guard selects lane.load_es
      as endstop; normal/fallback path uses lane.hub_endstop_name; passes through
      dist*dir, speed_mode, use_homing, assist_active; returns lane.move_to tuple.
  - AFC_EMU.eject_lane: conditionally retracts past hub; always calls
      move_advanced and do_enable(False).
  - load_config_prefix: returns an AFC_EMU instance.
  - Module-level import guards: each missing dependency raises configparser.Error
      with the correct message and embedded traceback.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from extras.AFC_EMU import AFC_EMU, load_config_prefix
from extras.AFC_BoxTurtle import afcBoxTurtle
from extras.AFC_lane import SpeedMode, AssistActive, MoveDirection, AFCMoveWarning


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_emu(name="EMU_1"):
    """Build an AFC_EMU instance bypassing the complex __init__."""
    unit = AFC_EMU.__new__(AFC_EMU)

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
    unit.full_name = ["AFC_EMU", name]
    unit.lanes = {}
    # unit.hub_obj defaults to None; set to a MagicMock in tests that need the
    # virtual-pin path (hasattr(self.hub_obj, 'is_virtual_pin') must be True).
    unit.hub_obj = None
    unit.extruder_obj = None
    unit.buffer_obj = None
    unit.hub = None
    unit.extruder = None
    unit.buffer_name = None
    unit.td1_defined = False
    unit.type = "EMU"
    unit.gcode = afc.gcode
    unit.logo = ""
    unit.logo_error = ""

    return unit


def _make_lane(
    name="lane1",
    loaded_to_hub=False,
    hub_endstop_name="hub_endstop",
    dist_hub=100.0,
    extruder_clear_dis=50.0,
    raw_load_state=False,
):
    """Build a minimal mock AFCLane for use in EMU tests."""
    lane = MagicMock()
    lane.name = name
    lane.loaded_to_hub = loaded_to_hub
    lane.hub_endstop_name = hub_endstop_name
    lane.load_es = "load_endstop"
    lane.dist_hub = dist_hub
    lane.extruder_clear_dis = extruder_clear_dis
    lane.raw_load_state = raw_load_state
    lane.move_to = MagicMock(return_value=(True, 0, AFCMoveWarning.NONE))
    lane.move_advanced = MagicMock()
    lane.do_enable = MagicMock()
    return lane


def _make_virtual_hub():
    """Return a lane hub mock whose is_virtual_pin() returns True."""
    hub = MagicMock()
    hub.is_virtual_pin.return_value = True
    hub.hub_clear_move_dis = 20.0
    return hub


def _make_normal_hub():
    """Return a lane hub mock whose is_virtual_pin() returns False."""
    hub = MagicMock()
    hub.is_virtual_pin.return_value = False
    return hub


# ── handle_connect ────────────────────────────────────────────────────────────

class TestHandleConnect:
    def test_logo_contains_emu_ready(self):
        """After handle_connect, the success logo must reference EMU Ready."""
        unit = _make_emu()
        unit.handle_connect()
        assert "EMU Ready" in unit.logo

    def test_logo_error_contains_emu_not_ready(self):
        """After handle_connect, the error logo must reference EMU Not Ready."""
        unit = _make_emu()
        unit.handle_connect()
        assert "EMU Not Ready" in unit.logo_error

    def test_logo_uses_success_span(self):
        unit = _make_emu()
        unit.handle_connect()
        assert "success--text" in unit.logo

    def test_logo_error_uses_error_span(self):
        unit = _make_emu()
        unit.handle_connect()
        assert "error--text" in unit.logo_error


# ── prep_post_load: virtual-pin path ─────────────────────────────────────────
#
# The virtual-pin path fires only when ALL THREE of these hold:
#   1. lane.hub_obj is not None
#   2. hasattr(lane.hub_obj, 'is_virtual_pin')
#   3. lane.hub_obj.is_virtual_pin() is True

class TestPrepPostLoadVirtualPin:
    def test_move_to_called_with_correct_args_while_load_state_true(self):
        """move_to receives NEG hub_clear_move_dis and SpeedMode.SHORT."""
        unit = _make_emu()
        lane = _make_lane(raw_load_state=True)
        lane.hub_obj = _make_virtual_hub()

        # Stop the while loop after the first call.
        def _stop_after_first(*a, **kw):
            lane.raw_load_state = False
        lane.move_to.side_effect = _stop_after_first

        unit.prep_post_load(lane)

        lane.move_to.assert_called_once_with(
            lane.hub_obj.hub_clear_move_dis * MoveDirection.NEG,
            SpeedMode.SHORT,
            use_homing=False,
        )

    def test_move_to_not_called_when_raw_load_state_already_false(self):
        """When raw_load_state is False the while loop never executes."""
        unit = _make_emu()
        lane = _make_lane(raw_load_state=False)
        lane.hub_obj = _make_virtual_hub()

        unit.prep_post_load(lane)

        lane.move_to.assert_not_called()

    def test_move_to_capped_at_10_tries(self):
        """While loop executes at most 10 times regardless of raw_load_state."""
        unit = _make_emu()
        lane = _make_lane(raw_load_state=True)  # stays True → always retry
        lane.hub_obj = _make_virtual_hub()

        unit.prep_post_load(lane)

        assert lane.move_to.call_count == 10

    def test_move_to_stops_when_raw_load_state_clears(self):
        """While loop stops early once raw_load_state becomes False."""
        unit = _make_emu()
        lane = _make_lane(raw_load_state=True)
        lane.hub_obj = _make_virtual_hub()

        call_count = [0]
        def _flip_after_3(*a, **kw):
            call_count[0] += 1
            if call_count[0] >= 3:
                lane.raw_load_state = False
        lane.move_to.side_effect = _flip_after_3

        unit.prep_post_load(lane)

        assert lane.move_to.call_count == 3

    def test_sets_loaded_to_hub_true(self):
        """loaded_to_hub is set True after the retry loop."""
        unit = _make_emu()
        lane = _make_lane(raw_load_state=True)
        lane.hub_obj = _make_virtual_hub()

        unit.prep_post_load(lane)

        assert lane.loaded_to_hub is True

    def test_sets_loaded_to_hub_true_when_no_moves_needed(self):
        """loaded_to_hub is still set True even when raw_load_state starts False."""
        unit = _make_emu()
        lane = _make_lane(raw_load_state=False)
        lane.hub_obj = _make_virtual_hub()

        unit.prep_post_load(lane)

        assert lane.loaded_to_hub is True

    def test_does_not_call_super_prep_post_load(self):
        """Virtual-pin path must NOT delegate to the parent implementation."""
        unit = _make_emu()
        lane = _make_lane(raw_load_state=False)
        lane.hub_obj = _make_virtual_hub()

        with patch.object(afcBoxTurtle, "prep_post_load") as mock_super:
            unit.prep_post_load(lane)
            mock_super.assert_not_called()


# ── prep_post_load: normal / fallback path ───────────────────────────────────

class TestPrepPostLoadNormalHub:
    def test_delegates_to_super_when_hub_lacks_is_virtual_pin(self):
        """When hub object has no is_virtual_pin attr, fallback path calls parent."""
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = object()  # no is_virtual_pin attribute

        with patch.object(afcBoxTurtle, "prep_post_load") as mock_super:
            unit.prep_post_load(lane)
            mock_super.assert_called_once_with(lane)
            lane.move_to.assert_not_called()

    def test_delegates_to_super_when_lane_hub_obj_is_none(self):
        """When lane.hub_obj is None the guard short-circuits; super() is called."""
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = None

        with patch.object(afcBoxTurtle, "prep_post_load") as mock_super:
            unit.prep_post_load(lane)
            mock_super.assert_called_once_with(lane)

    def test_delegates_to_super_when_hub_not_virtual_pin(self):
        """When lane.hub_obj.is_virtual_pin() is False the parent is called."""
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = _make_normal_hub()

        with patch.object(afcBoxTurtle, "prep_post_load") as mock_super:
            unit.prep_post_load(lane)
            mock_super.assert_called_once_with(lane)

    def test_does_not_call_move_to_on_normal_path(self):
        """Fallback path must not call lane.move_to."""
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = _make_normal_hub()

        with patch.object(afcBoxTurtle, "prep_post_load"):
            unit.prep_post_load(lane)
            lane.move_to.assert_not_called()


# ── move_to_hub: normal / fallback path (uses hub_endstop_name) ──────────────

class TestMoveToHubNormalHub:
    def test_uses_hub_endstop_name_when_lane_hub_obj_is_none(self):
        """lane.hub_obj=None → endstop is hub_endstop_name."""
        unit = _make_emu()
        lane = _make_lane(hub_endstop_name="hub_es")
        lane.hub_obj = None

        unit.move_to_hub(lane, dist=100.0, dir=MoveDirection.POS)

        _, kwargs = lane.move_to.call_args
        assert kwargs.get("endstop") == "hub_es"


    def test_uses_hub_endstop_name_when_lane_hub_not_virtual(self):
        """lane.hub_obj.is_virtual_pin()=False → endstop is hub_endstop_name."""
        unit = _make_emu()
        lane = _make_lane(hub_endstop_name="hub_es_normal")
        lane.hub_obj = _make_normal_hub()

        unit.move_to_hub(lane, dist=100.0, dir=MoveDirection.POS)

        _, kwargs = lane.move_to.call_args
        assert kwargs.get("endstop") == "hub_es_normal"

    def test_passes_dist_times_dir_as_first_arg(self):
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = _make_normal_hub()

        unit.move_to_hub(lane, dist=150.0, dir=MoveDirection.NEG)

        args, _ = lane.move_to.call_args
        assert args[0] == pytest.approx(150.0 * MoveDirection.NEG)

    def test_passes_speed_mode_arg(self):
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = _make_normal_hub()

        unit.move_to_hub(lane, dist=50.0, dir=MoveDirection.POS, speed_mode=SpeedMode.LONG)

        args, _ = lane.move_to.call_args
        assert args[1] == SpeedMode.LONG

    def test_default_speed_mode_is_hub(self):
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = _make_normal_hub()

        unit.move_to_hub(lane, dist=50.0, dir=MoveDirection.POS)

        args, _ = lane.move_to.call_args
        assert args[1] == SpeedMode.HUB

    def test_passes_use_homing_kwarg(self):
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = _make_normal_hub()

        unit.move_to_hub(lane, dist=50.0, dir=MoveDirection.POS, use_homing=False)

        _, kwargs = lane.move_to.call_args
        assert kwargs.get("use_homing") is False

    def test_default_use_homing_is_true(self):
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = _make_normal_hub()

        unit.move_to_hub(lane, dist=50.0, dir=MoveDirection.POS)

        _, kwargs = lane.move_to.call_args
        assert kwargs.get("use_homing") is True

    def test_passes_assist_active_kwarg(self):
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = _make_normal_hub()

        unit.move_to_hub(lane, dist=50.0, dir=MoveDirection.POS,
                         assist_active=AssistActive.YES)

        _, kwargs = lane.move_to.call_args
        assert kwargs.get("assist_active") == AssistActive.YES

    def test_default_assist_active_is_dynamic(self):
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = _make_normal_hub()

        unit.move_to_hub(lane, dist=50.0, dir=MoveDirection.POS)

        _, kwargs = lane.move_to.call_args
        assert kwargs.get("assist_active") == AssistActive.DYNAMIC


# ── move_to_hub: virtual-pin path (uses load_es) ─────────────────────────────

class TestMoveToHubVirtualPin:
    def test_uses_load_es_when_hub_is_virtual(self):
        """All three guard conditions met → endstop is lane.load_es."""
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = _make_virtual_hub()

        unit.move_to_hub(lane, dist=80.0, dir=MoveDirection.POS)

        _, kwargs = lane.move_to.call_args
        assert kwargs.get("endstop") == lane.load_es

    def test_does_not_use_hub_endstop_name_for_virtual_pin(self):
        """Virtual-pin path must not fall back to hub_endstop_name."""
        unit = _make_emu()
        lane = _make_lane(hub_endstop_name="must_not_be_used")
        lane.hub_obj = _make_virtual_hub()

        unit.move_to_hub(lane, dist=80.0, dir=MoveDirection.POS)

        _, kwargs = lane.move_to.call_args
        assert kwargs.get("endstop") != "must_not_be_used"


# ── move_to_hub: return value ─────────────────────────────────────────────────

class TestMoveToHubReturnValue:
    def test_returns_tuple_from_lane_move_to(self):
        """move_to_hub passes through the (homed, distance, warn) tuple."""
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = _make_normal_hub()
        lane.move_to.return_value = (True, 75.5, AFCMoveWarning.WARN)

        result = unit.move_to_hub(lane, dist=80.0, dir=MoveDirection.POS)

        assert result == (True, 75.5, AFCMoveWarning.WARN)

    def test_returns_false_result_on_failure(self):
        unit = _make_emu()
        lane = _make_lane()
        lane.hub_obj = _make_normal_hub()
        lane.move_to.return_value = (False, 0, AFCMoveWarning.ERROR)

        homed, distance, warn = unit.move_to_hub(lane, dist=80.0, dir=MoveDirection.POS)

        assert homed is False
        assert warn == AFCMoveWarning.ERROR


# ── eject_lane: loaded_to_hub True ───────────────────────────────────────────

class TestEjectLaneLoadedToHub:
    def test_move_to_called_when_loaded_to_hub(self):
        """When loaded_to_hub is True, move_to retracts past hub with correct args."""
        unit = _make_emu()
        lane = _make_lane(loaded_to_hub=True)
        unit.afc.homing_enabled = True

        unit.eject_lane(lane)

        lane.move_to.assert_called_once_with(
            lane.dist_hub * -1,
            SpeedMode.DIST_HUB,
            endstop=lane.load_es,
            assist_active=AssistActive.DYNAMIC,
            use_homing=True,
        )

    def test_move_to_use_homing_follows_afc_flag(self):
        """use_homing is taken from afc.homing_enabled."""
        unit = _make_emu()
        lane = _make_lane(loaded_to_hub=True)
        unit.afc.homing_enabled = False

        unit.eject_lane(lane)

        _, kwargs = lane.move_to.call_args
        assert kwargs.get("use_homing") is False

    def test_move_to_not_called_when_not_loaded_to_hub(self):
        """When loaded_to_hub is False, move_to must not be called."""
        unit = _make_emu()
        lane = _make_lane(loaded_to_hub=False)

        unit.eject_lane(lane)

        lane.move_to.assert_not_called()


# ── eject_lane: move_advanced always called ───────────────────────────────────

class TestEjectLaneMoveAdvanced:
    def test_move_advanced_called_with_negative_extruder_clear_dis(self):
        unit = _make_emu()
        lane = _make_lane(loaded_to_hub=False, extruder_clear_dis=30.0)

        unit.eject_lane(lane)

        lane.move_advanced.assert_called_once_with(
            lane.extruder_clear_dis * -1,
            SpeedMode.SHORT,
        )

    def test_move_advanced_called_even_when_loaded_to_hub(self):
        unit = _make_emu()
        lane = _make_lane(loaded_to_hub=True)
        unit.afc.homing_enabled = False

        unit.eject_lane(lane)

        lane.move_advanced.assert_called_once()


# ── eject_lane: do_enable always called ──────────────────────────────────────

class TestEjectLaneDoEnable:
    def test_do_enable_false_when_not_loaded_to_hub(self):
        unit = _make_emu()
        lane = _make_lane(loaded_to_hub=False)

        unit.eject_lane(lane)

        lane.do_enable.assert_called_once_with(False)

    def test_do_enable_false_when_loaded_to_hub(self):
        unit = _make_emu()
        lane = _make_lane(loaded_to_hub=True)
        unit.afc.homing_enabled = False

        unit.eject_lane(lane)

        lane.do_enable.assert_called_once_with(False)

    def test_eject_order_move_to_then_move_advanced_then_do_enable(self):
        """Operations must execute in the correct sequence."""
        unit = _make_emu()
        lane = _make_lane(loaded_to_hub=True)
        unit.afc.homing_enabled = False
        call_order = []
        lane.move_to.side_effect = lambda *a, **kw: call_order.append("move_to")
        lane.move_advanced.side_effect = lambda *a, **kw: call_order.append("move_advanced")
        lane.do_enable.side_effect = lambda *a, **kw: call_order.append("do_enable")

        unit.eject_lane(lane)

        assert call_order == ["move_to", "move_advanced", "do_enable"]


# ── Import failure handling ───────────────────────────────────────────────────
#
# Each try/except block at the top of AFC_EMU.py raises configparser.Error when
# a dependency cannot be imported.  Because the guards run at module scope we
# cannot just call a function; we must force the module to be re-executed.
#
# Pattern:
#   1. Pop extras.AFC_EMU from sys.modules so it is re-imported fresh.
#   2. Set the target dependency to None — Python treats a None entry as
#      "blocked", raising ModuleNotFoundError on import.
#   3. importlib.import_module('extras.AFC_EMU') re-executes the module body.
#   4. Restore sys.modules in a finally block so nothing leaks.

import importlib
import sys
from configparser import Error as ConfigParserError


def _blocked_import(blocked_module: str):
    """Context manager: block *blocked_module* and reload extras.AFC_EMU.

    Yields the pytest.ExceptionInfo produced by pytest.raises.
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        saved_emu     = sys.modules.pop("extras.AFC_EMU", None)
        saved_blocked = sys.modules.pop(blocked_module, None)
        sys.modules[blocked_module] = None  # type: ignore[assignment]
        try:
            with pytest.raises(ConfigParserError) as exc_info:
                importlib.import_module("extras.AFC_EMU")
            yield exc_info
        finally:
            if saved_blocked is not None:
                sys.modules[blocked_module] = saved_blocked
            else:
                sys.modules.pop(blocked_module, None)
            if saved_emu is not None:
                sys.modules["extras.AFC_EMU"] = saved_emu
            else:
                sys.modules.pop("extras.AFC_EMU", None)

    return _ctx()


class TestImportFailures:
    """Verify that each module-level try/except guard raises with the right message."""

    # ── extras.AFC_utils ─────────────────────────────────────────────────────

    def test_missing_afc_utils_raises_config_error(self):
        """Blocking extras.AFC_utils must raise configparser.Error."""
        with _blocked_import("extras.AFC_utils"):
            pass

    def test_missing_afc_utils_error_message_references_module(self):
        """The AFC_utils error message must name AFC_utils.ERROR_STR."""
        with _blocked_import("extras.AFC_utils") as exc_info:
            assert "AFC_utils.ERROR_STR" in str(exc_info.value)

    def test_missing_afc_utils_error_includes_traceback(self):
        """The AFC_utils error message must embed traceback text."""
        with _blocked_import("extras.AFC_utils") as exc_info:
            msg = str(exc_info.value)
            assert any(kw in msg for kw in ("Traceback", "ModuleNotFoundError", "ImportError"))

    # ── extras.AFC_lane ──────────────────────────────────────────────────────

    def test_missing_afc_lane_raises_config_error(self):
        """Blocking extras.AFC_lane must raise configparser.Error."""
        with _blocked_import("extras.AFC_lane"):
            pass

    def test_missing_afc_lane_error_message_references_module(self):
        """The AFC_lane error message must reference 'AFC_lane'."""
        with _blocked_import("extras.AFC_lane") as exc_info:
            assert "AFC_lane" in str(exc_info.value)

    def test_missing_afc_lane_error_includes_traceback(self):
        """The AFC_lane error message must embed traceback text."""
        with _blocked_import("extras.AFC_lane") as exc_info:
            msg = str(exc_info.value)
            assert any(kw in msg for kw in ("Traceback", "ModuleNotFoundError", "ImportError"))

    # ── extras.AFC_BoxTurtle ─────────────────────────────────────────────────

    def test_missing_afc_box_turtle_raises_config_error(self):
        """Blocking extras.AFC_BoxTurtle must raise configparser.Error."""
        with _blocked_import("extras.AFC_BoxTurtle"):
            pass

    def test_missing_afc_box_turtle_error_message_references_module(self):
        """The AFC_BoxTurtle error message must reference 'AFC_BoxTurtle'."""
        with _blocked_import("extras.AFC_BoxTurtle") as exc_info:
            assert "AFC_BoxTurtle" in str(exc_info.value)

    def test_missing_afc_box_turtle_error_includes_traceback(self):
        """The AFC_BoxTurtle error message must embed traceback text."""
        with _blocked_import("extras.AFC_BoxTurtle") as exc_info:
            msg = str(exc_info.value)
            assert any(kw in msg for kw in ("Traceback", "ModuleNotFoundError", "ImportError"))


# ── load_config_prefix ────────────────────────────────────────────────────────

class TestLoadConfigPrefix:
    def test_returns_afc_emu_instance(self):
        """load_config_prefix must return an AFC_EMU object."""
        from tests.conftest import MockConfig, MockPrinter, MockAFC

        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_EMU my_emu", printer=printer)

        with patch.object(afcBoxTurtle, "__init__", lambda self, cfg: None):
            instance = load_config_prefix(config)

        assert isinstance(instance, AFC_EMU)

    def test_type_defaults_to_emu(self):
        """When config does not supply 'type', AFC_EMU.__init__ must default to 'EMU'."""
        from tests.conftest import MockConfig, MockPrinter, MockAFC

        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_EMU my_emu", printer=printer)

        with patch.object(afcBoxTurtle, "__init__", lambda self, cfg: None):
            instance = load_config_prefix(config)

        assert instance.type == "EMU"