"""
Unit tests for extras/AFC_hub.py

Covers:
  - afc_hub.get_status: returns dict with expected keys and correct values
  - afc_hub.state property: physical vs virtual hub sensor
  - afc_hub.switch_pin_callback: updates internal _state
  - afc_hub.__str__: returns name
  - afc_hub.handle_runout: only triggers for the currently-loaded lane
"""

from __future__ import annotations

import sys
import importlib.util
import configparser
from unittest.mock import MagicMock, patch, call
import pytest

from extras.AFC_hub import afc_hub, load_config_prefix


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_hub(switch_pin="PA0", name="test_hub", extra_values=None):
    """Build an afc_hub instance through its real __init__ and handle_connect
    (fired via the klippy:connect event), mocking only the Klipper
    collaborators (config/printer/AFC) and add_filament_switch, which does
    real pin/hardware registration unrelated to afc_hub's own logic.

    All of __init__'s config-driven numeric/boolean attributes (bowden
    lengths, servo angles, cut settings, etc.) fall through to their real
    source-code defaults automatically, since MockConfig returns the
    caller's own default whenever a value isn't present in `values` --
    matching what this helper used to hardcode by hand.
    """
    from tests.conftest import MockAFC, MockPrinter, MockConfig

    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    config = MockConfig(name=f"AFC_hub {name}", printer=printer,
                        values={"switch_pin": switch_pin})

    with patch("extras.AFC_hub.add_filament_switch") as mock_afs:
        mock_afs.return_value = (MagicMock(), MagicMock())
        hub = afc_hub(config)
    printer.send_event("klippy:connect")

    if switch_pin and switch_pin.lower() != "virtual":
        # add_filament_switch is mocked above (it does real pin/hardware
        # setup), so the fila/runout_helper it would normally wire up need
        # their own defaults set by hand for handle_runout's tests.
        hub.fila.runout_helper.min_event_systime = 0.0
        hub.fila.runout_helper.event_delay = 0.5

    if extra_values:
        for k, v in extra_values.items():
            setattr(hub, k, v)

    return hub


# ── __str__ ───────────────────────────────────────────────────────────────────

class TestAFCHubStr:
    def test_str_returns_name(self):
        hub = _make_hub(name="hub1")
        assert str(hub) == "hub1"


# ── is_virtual_pin ────────────────────────────────────────────────────────────

class TestIsVirtualPin:
    def test_true_when_switch_pin_is_virtual(self):
        hub = _make_hub(switch_pin="virtual")
        assert hub.is_virtual_pin() is True

    def test_true_is_case_insensitive(self):
        hub = _make_hub(switch_pin="VIRTUAL")
        assert hub.is_virtual_pin() is True

    def test_false_for_a_physical_pin_name(self):
        hub = _make_hub(switch_pin="PA0")
        assert hub.is_virtual_pin() is False

    def test_false_when_switch_pin_is_none(self):
        """switch_pin is Optional[str] (unset when the config option is
        missing); the ternary's else branch must return False rather than
        crash on None.lower()."""
        hub = _make_hub(switch_pin=None)
        assert hub.is_virtual_pin() is False


# ── switch_pin_callback ───────────────────────────────────────────────────────

class TestSwitchPinCallback:
    def test_callback_sets_state_true(self):
        hub = _make_hub()
        hub.switch_pin_callback(100.0, True)
        assert hub._state is True

    def test_callback_sets_state_false(self):
        hub = _make_hub()
        hub._state = True
        hub.switch_pin_callback(101.0, False)
        assert hub._state is False


# ── state property ────────────────────────────────────────────────────────────

class TestStateProperty:
    def test_physical_switch_returns_internal_state(self):
        hub = _make_hub(switch_pin="PA0")
        hub._state = True
        assert hub.state is True

    def test_physical_switch_false(self):
        hub = _make_hub(switch_pin="PA0")
        hub._state = False
        assert hub.state is False

    def test_virtual_hub_true_when_any_lane_load_state_true(self):
        hub = _make_hub(switch_pin="virtual")
        lane1 = MagicMock()
        lane1.raw_load_state = False
        lane2 = MagicMock()
        lane2.raw_load_state = True
        hub.lanes = {"lane1": lane1, "lane2": lane2}
        assert hub.state is True

    def test_virtual_hub_false_when_all_lanes_not_loaded(self):
        hub = _make_hub(switch_pin="virtual")
        lane1 = MagicMock()
        lane1.raw_load_state = False
        lane2 = MagicMock()
        lane2.raw_load_state = False
        hub.lanes = {"lane1": lane1, "lane2": lane2}
        assert hub.state is False

    def test_virtual_hub_false_when_no_lanes(self):
        hub = _make_hub(switch_pin="virtual")
        hub.lanes = {}
        assert hub.state is False


# ── get_status ────────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_get_status_returns_dict(self):
        hub = _make_hub()
        result = hub.get_status()
        assert isinstance(result, dict)

    def test_get_status_contains_state(self):
        hub = _make_hub()
        hub._state = False
        result = hub.get_status()
        assert "state" in result
        assert result["state"] is False

    def test_get_status_cut_flag(self):
        hub = _make_hub()
        hub.cut = True
        result = hub.get_status()
        assert result["cut"] is True

    def test_get_status_cut_cmd_default_none(self):
        hub = _make_hub()
        result = hub.get_status()
        assert result["cut_cmd"] is None

    def test_get_status_bowden_length(self):
        hub = _make_hub()
        hub.afc_bowden_length = 1200.0
        result = hub.get_status()
        assert result["afc_bowden_length"] == 1200.0

    def test_get_status_lanes_list(self):
        hub = _make_hub()
        lane1 = MagicMock()
        lane1.name = "lane1"
        lane2 = MagicMock()
        lane2.name = "lane2"
        hub.lanes = {"lane1": lane1, "lane2": lane2}
        result = hub.get_status()
        assert set(result["lanes"]) == {"lane1", "lane2"}

    def test_get_status_servo_angles(self):
        hub = _make_hub()
        hub.cut_servo_pass_angle = 10.0
        hub.cut_servo_clip_angle = 170.0
        hub.cut_servo_prep_angle = 80.0
        result = hub.get_status()
        assert result["cut_servo_pass_angle"] == 10.0
        assert result["cut_servo_clip_angle"] == 170.0
        assert result["cut_servo_prep_angle"] == 80.0

    def test_get_status_cut_distances(self):
        hub = _make_hub()
        hub.cut_dist = 60.0
        hub.cut_clear = 130.0
        hub.cut_min_length = 250.0
        result = hub.get_status()
        assert result["cut_dist"] == 60.0
        assert result["cut_clear"] == 130.0
        assert result["cut_min_length"] == 250.0


# ── handle_runout ─────────────────────────────────────────────────────────────

class TestHandleRunout:
    def test_runout_triggers_current_lane_in_hub(self):
        hub = _make_hub()
        lane = MagicMock()
        hub.lanes = {"lane1": lane}
        hub.afc.current = "lane1"
        hub.handle_runout(100.0)
        lane.handle_hub_runout.assert_called_once_with(sensor=hub.name)

    def test_runout_does_not_trigger_if_current_lane_not_in_hub(self):
        hub = _make_hub()
        lane = MagicMock()
        hub.lanes = {"lane1": lane}
        hub.afc.current = "lane2"  # Different lane
        hub.handle_runout(100.0)
        lane.handle_hub_runout.assert_not_called()

    def test_runout_does_not_trigger_when_no_current(self):
        hub = _make_hub()
        lane = MagicMock()
        hub.lanes = {"lane1": lane}
        hub.afc.current = None
        hub.handle_runout(100.0)
        lane.handle_hub_runout.assert_not_called()

    def test_runout_updates_min_event_systime(self):
        # MockReactor's default monotonic() is 100.0; _make_hub sets
        # event_delay to 0.5, so the expected result is 100.5 -- computed
        # by hand here rather than re-deriving the source's own formula.
        hub = _make_hub()
        hub.lanes = {}
        hub.afc.current = None
        hub.handle_runout(150.0)
        assert hub.fila.runout_helper.min_event_systime == 100.5


# ── handle_connect ────────────────────────────────────────────────────────────

class TestHandleConnect:
    def test_physical_hub_handle_connect_does_not_raise(self):
        hub = _make_hub(switch_pin="PA0")
        hub.handle_connect()
        assert hub.gcode is hub.afc.gcode
        assert hub.reactor is hub.afc.reactor

    def test_handle_connect_sends_register_macros_event(self):
        hub = _make_hub(switch_pin="PA0")
        hub.printer.send_event = MagicMock(wraps=hub.printer.send_event)
        hub.handle_connect()
        hub.printer.send_event.assert_called_once_with("afc_hub:register_macros", hub)

# ── handle_ready ────────────────────────────────────────────────────────────

class TestHandleReady:

    def test_virtual_hub_raises_when_lanes_have_no_load_sensor(self):
        from configparser import Error as config_error
        hub = _make_hub(switch_pin="virtual")
        lane = MagicMock()
        lane.fullname = "AFC_stepper lane1"
        lane.load = None  # no load sensor
        hub.lanes = {"lane1": lane}
        with pytest.raises(config_error):
            hub.handle_ready()

    def test_virtual_hub_no_error_when_all_lanes_have_load_sensor(self):
        hub = _make_hub(switch_pin="virtual")
        lane = MagicMock()
        lane.fullname = "AFC_stepper lane1"
        lane.load = MagicMock()  # has load sensor
        hub.lanes = {"lane1": lane}
        hub.handle_ready()  # should not raise

    def test_physical_hub_handle_ready_skips_lane_check_entirely(self):
        # A physical hub (switch_pin != "virtual") must never evaluate the
        # per-lane load-sensor check. Prove it by giving a lane that WOULD
        # raise if the virtual-hub body ran (load is None, prep is not None,
        # and unit_obj.type is not a sensorless unit).
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(
            name="AFC_hub phys_hub", printer=printer,
            values={"switch_pin": "PA0"}
        )
        with patch("extras.AFC_hub.add_filament_switch") as mock_afs:
            mock_afs.return_value = (MagicMock(), MagicMock())
            hub = afc_hub(config)
        lane = MagicMock()
        lane.fullname = "AFC_stepper lane1"
        lane.load = None
        lane.prep = MagicMock()
        lane.unit_obj.type = "BoxTurtle"
        hub.lanes = {"lane1": lane}
        hub.handle_ready()  # should not raise, is_virtual_pin() is False

    def test_virtual_hub_skips_sensorless_unit_lanes(self):
        # A lane whose unit type is in SENSORLESS_UNITS (e.g. OpenAMS) is
        # skipped via `continue` even though it has no load sensor, so no
        # error should be raised.
        hub = _make_hub(switch_pin="virtual")
        lane = MagicMock()
        lane.fullname = "AFC_stepper lane1"
        lane.load = None
        lane.prep = MagicMock()
        lane.unit_obj.type = "OpenAMS"
        hub.lanes = {"lane1": lane}
        hub.handle_ready()  # should not raise, lane is skipped via continue

    def test_virtual_hub_no_error_when_prep_is_none(self):
        # Condition is `lane.load is None and lane.prep is not None`. With
        # load missing but prep also None, the second operand alone must be
        # enough to prevent the report, independent of the first.
        hub = _make_hub(switch_pin="virtual")
        lane = MagicMock()
        lane.fullname = "AFC_stepper lane1"
        lane.load = None
        lane.prep = None
        lane.unit_obj.type = "BoxTurtle"
        hub.lanes = {"lane1": lane}
        hub.handle_ready()  # should not raise

# ── afc_hub.__init__ ──────────────────────────────────────────────────────────

class TestAFCHubInit:
    def test_virtual_hub_init_does_not_call_add_filament_switch(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(
            name="AFC_hub test_hub", printer=printer,
            values={"switch_pin": "virtual", "afc_bowden_length": 900.0}
        )
        with patch("extras.AFC_hub.add_filament_switch") as mock_afs:
            hub = afc_hub(config)
        mock_afs.assert_not_called()

    def test_no_switch_pin_configured_does_not_crash_or_call_add_filament_switch(self):
        """switch_pin defaults to None when the config option is missing
        entirely (config.get('switch_pin', None)). __init__ must not crash
        on None.lower() and must skip add_filament_switch, the same as it
        does for an explicit "virtual" pin -- proving the `self.switch_pin`
        truthiness check matters independently of the "virtual" comparison,
        since None never equals "virtual" either."""
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(
            name="AFC_hub test_hub", printer=printer,
            values={}  # switch_pin not configured -> defaults to None
        )
        with patch("extras.AFC_hub.add_filament_switch") as mock_afs:
            hub = afc_hub(config)
        mock_afs.assert_not_called()
        assert hub.switch_pin is None

    def test_virtual_hub_init_registers_hub_in_afc(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(
            name="AFC_hub my_hub", printer=printer,
            values={"switch_pin": "virtual"}
        )
        hub = afc_hub(config)
        assert "my_hub" in afc.hubs

    def test_virtual_hub_sets_name_from_config(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(
            name="AFC_hub hub_one", printer=printer,
            values={"switch_pin": "virtual"}
        )
        hub = afc_hub(config)
        assert hub.name == "hub_one"

    def test_physical_hub_init_calls_add_filament_switch(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(
            name="AFC_hub phys_hub", printer=printer,
            values={"switch_pin": "PA0"}
        )
        with patch("extras.AFC_hub.add_filament_switch") as mock_afs:
            mock_afs.return_value = (MagicMock(), MagicMock())
            hub = afc_hub(config)
        mock_afs.assert_called_once()

    def test_physical_hub_registers_button_callback(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        buttons_mock = MagicMock()
        printer._objects["buttons"] = buttons_mock
        config = MockConfig(
            name="AFC_hub phys_hub", printer=printer,
            values={"switch_pin": "PA0"}
        )
        with patch("extras.AFC_hub.add_filament_switch") as mock_afs:
            mock_afs.return_value = (MagicMock(), MagicMock())
            hub = afc_hub(config)
        buttons_mock.register_buttons.assert_called_once()


# ── hub_cut ───────────────────────────────────────────────────────────────────

class TestHubCut:
    def test_hub_cut_no_confirm_runs_exactly_three_servo_commands(self):
        from unittest.mock import PropertyMock
        hub = _make_hub(switch_pin="PA0")
        hub.cut_confirm = False
        cur_lane = MagicMock()
        # Sequence: loop1 enter(F), loop1 exit(T), loop2 enter(T), loop2 exit(F),
        #           loop3 enter(F), loop3 exit(T)
        with patch.object(type(hub), "state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect=[False, True, True, False, False, True]
            hub.hub_cut(cur_lane)
        # cut_confirm=False must skip the extra prep/clip pair, leaving
        # exactly prep + clip + pass -- not just "at least" 3, since a bug
        # that accidentally also ran the confirm branch would still pass a
        # >= 3 check.
        assert hub.gcode.run_script_from_command.call_count == 3

    def test_hub_cut_no_confirm_calls_correct_servo_angles(self):
        from unittest.mock import PropertyMock
        hub = _make_hub(switch_pin="PA0")
        hub.cut_confirm = False
        cur_lane = MagicMock()
        with patch.object(type(hub), "state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect=[False, True, True, False, False, True]
            hub.hub_cut(cur_lane)
        # cut_servo_name="cut", prep=75.0, clip=160.0, pass=0.0 (from
        # _make_hub's defaults) computed by hand, not via the source's own
        # format-string construction.
        assert hub.gcode.run_script_from_command.call_args_list == [
            call("SET_SERVO SERVO=cut ANGLE=75.0"),
            call("SET_SERVO SERVO=cut ANGLE=160.0"),
            call("SET_SERVO SERVO=cut ANGLE=0.0"),
        ]

    def test_hub_cut_with_confirm_runs_exactly_five_servo_commands(self):
        from unittest.mock import PropertyMock
        hub = _make_hub(switch_pin="PA0")
        hub.cut_confirm = True
        cur_lane = MagicMock()
        with patch.object(type(hub), "state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect=[False, True, True, False, False, True]
            hub.hub_cut(cur_lane)
        # cut_confirm=True adds an extra prep+clip pair before the final
        # pass angle: prep, clip, prep, clip, pass.
        assert hub.gcode.run_script_from_command.call_args_list == [
            call("SET_SERVO SERVO=cut ANGLE=75.0"),
            call("SET_SERVO SERVO=cut ANGLE=160.0"),
            call("SET_SERVO SERVO=cut ANGLE=75.0"),
            call("SET_SERVO SERVO=cut ANGLE=160.0"),
            call("SET_SERVO SERVO=cut ANGLE=0.0"),
        ]

    def test_hub_cut_retracts_filament_after_cut(self):
        from unittest.mock import PropertyMock
        hub = _make_hub(switch_pin="PA0")
        hub.cut_confirm = False
        cur_lane = MagicMock()
        with patch.object(type(hub), "state", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect=[False, True, True, False, False, True]
            hub.hub_cut(cur_lane)
        # Final move must retract by exactly cut_clear (120.0 from
        # _make_hub's defaults), not merely "some negative distance".
        last_call = cur_lane.move.call_args_list[-1]
        assert last_call[0][0] == -120.0


# ── load_config_prefix ──────────────────────────────────────────────────────

class TestLoadConfigPrefix:
    def test_returns_afc_hub_instance(self):
        from tests.conftest import MockAFC, MockPrinter, MockConfig
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_hub test_hub", printer=printer,
                             values={"switch_pin": "virtual"})
        result = load_config_prefix(config)
        assert isinstance(result, afc_hub)

    def test_registers_self_in_afc_hubs(self):
        from tests.conftest import MockAFC, MockPrinter, MockConfig
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_hub test_hub", printer=printer,
                             values={"switch_pin": "virtual"})
        result = load_config_prefix(config)
        assert afc.hubs["test_hub"] is result


# ═════════════════════════════════════════════════════════════════════════
# Module-level import guards
# ═════════════════════════════════════════════════════════════════════════

def _exec_afc_hub_with_blocked_dependency(blocked_module_name):
    """Execute a throw-away copy of extras/AFC_hub.py's module-level code
    with `blocked_module_name` forced to fail import, to exercise the file's
    top-level ``try: from X import Y / except: raise config_error(...)``
    guards.

    This never touches the real, already-imported ``extras.AFC_hub`` module
    that the rest of this test suite depends on: the copy is loaded under a
    throwaway module name and discarded afterward, whether or not it raises.
    Blocking an import via ``sys.modules[name] = None`` is a standard Python
    mechanism -- it makes any ``import``/``from ... import`` of that name
    raise ImportError immediately, without touching the module itself.

    Cleanup restores the *exact same* pre-existing module object in
    sys.modules (not just removes the block) -- simply deleting the entry
    would let it get re-imported fresh the next time anything touches it,
    producing new, distinct class objects that no longer match what other
    test files already imported and bound references to.
    """
    import extras.AFC_hub as real_module
    fresh_name = "extras.AFC_hub_import_guard_probe"
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


def _exec_afc_hub_with_missing_attr(real_module_name, missing_attr_name):
    """Execute a throw-away copy of extras/AFC_hub.py's module-level code
    with a single attribute (`missing_attr_name`) hidden from
    `real_module_name`, to exercise a guard whose `except:` can't be reached
    by blocking the whole dependency module.

    AFC_hub.py has two separate guards that both import from
    extras.AFC_utils (ERROR_STR, then add_filament_switch); blocking that
    module outright (via `sys.modules[name] = None`, as
    `_exec_afc_hub_with_blocked_dependency` does) always trips the first of
    the two guards before execution ever reaches the second, since both
    imports run top-to-bottom against the same blocked module. This swaps in
    a proxy that forwards every attribute lookup to the real module except
    the one being hidden, which raises AttributeError -- exactly what
    `from module import name` converts into ImportError when the name is
    genuinely missing from an otherwise-importable module.
    """
    import extras.AFC_hub as real_module
    real_dep_module = sys.modules[real_module_name]

    class _ProxyModule:
        def __getattr__(self, attr_name):
            if attr_name == missing_attr_name:
                raise AttributeError(attr_name)
            return getattr(real_dep_module, attr_name)

    fresh_name = "extras.AFC_hub_import_guard_probe"
    sys.modules[real_module_name] = _ProxyModule()
    try:
        spec = importlib.util.spec_from_file_location(fresh_name, real_module.__file__)
        fresh = importlib.util.module_from_spec(spec)
        sys.modules[fresh_name] = fresh
        try:
            spec.loader.exec_module(fresh)
        finally:
            sys.modules.pop(fresh_name, None)
    finally:
        sys.modules[real_module_name] = real_dep_module


class TestModuleImportGuards:
    """Covers the three module-level `try/except: raise config_error(...)`
    guards in AFC_hub.py, one per dependency import."""

    def test_afc_utils_error_str_import_failure_raises_configparser_error(self):
        """The very first guard imports ERROR_STR itself from AFC_utils, so
        it can't use ERROR_STR.format(...) in its own except clause."""
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_hub_with_blocked_dependency("extras.AFC_utils")
        assert str(exc_info.value).startswith(
            "Error when trying to import AFC_utils.ERROR_STR"
        )

    def test_afc_utils_add_filament_switch_import_failure_raises_configparser_error(self):
        """The second guard imports add_filament_switch from the same
        AFC_utils module as the first guard's ERROR_STR, so it needs a
        specific missing attribute rather than the whole module blocked."""
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_hub_with_missing_attr("extras.AFC_utils", "add_filament_switch")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_utils, please rerun install-afc.sh"
        )

    def test_afc_unit_import_failure_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_hub_with_blocked_dependency("extras.AFC_unit")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_unit, please rerun install-afc.sh"
        )
