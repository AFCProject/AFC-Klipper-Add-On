"""
Unit tests for extras/AFC_HTLF.py

Covers:
  - AFC_HTLF: class constants
  - AFC_HTLF.home_callback: sets home_state from button event
  - AFC_HTLF: is subclass of afcBoxTurtle
"""

from __future__ import annotations

import sys
import importlib.util
import configparser
from unittest.mock import MagicMock, patch, call
import pytest

from extras.AFC_HTLF import AFC_HTLF
from extras.AFC_BoxTurtle import afcBoxTurtle
from extras.AFC_lane import AFCLaneState


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_htlf_config(name="HTLF_1", values=None, drive_stepper="drive",
                       selector_stepper="selector", add_stepper_sections=True):
    """Build the MockConfig/MockPrinter/MockAFC trio needed to construct an
    AFC_HTLF via its real __init__ (chained through afcBoxTurtle.__init__
    and afcUnit.__init__), without actually constructing it -- lets a test
    drive AFC_HTLF(config) itself, e.g. to assert a config_error is raised.

    Registers [AFC_stepper <drive_stepper>]/[AFC_stepper <selector_stepper>]
    config sections and matching MagicMock stepper objects in the printer's
    object cache so afcUnit._lookup_objects resolves drive_stepper_obj/
    selector_stepper_obj successfully; selector_stepper_obj._endstops is a
    real dict since AFC_HTLF.__init__ does `selector_stepper_obj._endstops
    [name] = ...` (item assignment, which a bare MagicMock doesn't support).
    """
    from tests.conftest import MockAFC, MockConfig, MockPrinter

    afc = MockAFC()
    printer = MockPrinter(afc=afc)

    all_values = {
        "drive_stepper": drive_stepper,
        "selector_stepper": selector_stepper,
        "cam_angle": 60,
        "home_pin": "PA1",
    }
    if values:
        all_values.update(values)

    config = MockConfig(name=f"AFC_HTLF {name}", printer=printer, values=all_values)

    if add_stepper_sections:
        config.fileconfig.add_section(f"AFC_stepper {drive_stepper}")
        config.fileconfig.add_section(f"AFC_stepper {selector_stepper}")
        selector_obj = MagicMock()
        selector_obj._endstops = {}
        printer._objects[f"AFC_stepper {drive_stepper}"] = MagicMock()
        printer._objects[f"AFC_stepper {selector_stepper}"] = selector_obj

    return config, printer, afc


def _make_htlf(name="HTLF_1", values=None, **kwargs):
    """Build a real AFC_HTLF via its actual __init__. See
    _make_htlf_config for what's pre-wired; defaults reproduce a normal,
    fully-successful construction (cam_angle=60, home_pin="PA1", drive/
    selector steppers resolved)."""
    config, printer, afc = _make_htlf_config(name=name, values=values, **kwargs)
    return AFC_HTLF(config)


# ── Inheritance ───────────────────────────────────────────────────────────────

class TestHTLFInheritance:
    def test_is_subclass_of_box_turtle(self):
        assert issubclass(AFC_HTLF, afcBoxTurtle)


# ── Class constants ───────────────────────────────────────────────────────────

class TestHTLFConstants:
    def test_valid_cam_angles(self):
        assert AFC_HTLF.VALID_CAM_ANGLES == [30, 45, 60]

    def test_home_unit_cmd_option_default(self):
        assert AFC_HTLF.cmd_AFC_HOME_UNIT_options == {
            "UNIT": {"type": "string", "default": "HTLF_1"}
        }


# ── __init__ ──────────────────────────────────────────────────────────────────

class TestHTLFInit:
    """Covers AFC_HTLF's own __init__ logic: reading its config variables,
    the cam_angle validation, and the home-pin-dependent endstop/filament
    switch setup. afcUnit/afcBoxTurtle's own __init__ bodies are pre-existing
    behavior out of scope here."""

    # -- config variable passthrough --

    def test_defaults_when_not_set_in_config(self):
        """Every config value AFC_HTLF.__init__ reads with a fallback default
        (mm_move_per_rotation, MAX_ANGLE_MOVEMENT, selector_movement_speed/
        accel, type), plus the fixed/derived attributes it always sets up
        front (current_selected_lane, home_state, prep_homed,
        failed_to_home, _homed_distance, lobe_current_pos) -- verified
        together in one construction since none of them depend on each
        other, so a single _make_htlf() call is enough to exercise all of
        them at once."""
        unit = _make_htlf()
        assert unit.type == "HTLF"
        assert unit.mm_move_per_rotation == 32
        assert unit.MAX_ANGLE_MOVEMENT == 215
        assert unit.selector_movement_speed == 50.0
        assert unit.selector_movement_accel == 50.0
        assert unit.current_selected_lane is None
        assert unit.home_state is False
        assert unit.prep_homed is False
        assert unit.failed_to_home is False
        assert unit._homed_distance == 0.0
        assert unit.lobe_current_pos == 0

    def test_reads_overridden_values_from_config(self):
        """Mirror of test_defaults_when_not_set_in_config: the same set of
        config-backed variables (plus drive_stepper/selector_stepper/
        cam_angle/home_pin, which have no fallback default to compare
        against), each given a distinct non-default value in one config and
        verified to come back unchanged -- proving each is actually read
        from its own config key, not several all reading the same one."""
        unit = _make_htlf(
            drive_stepper="drive_a",
            selector_stepper="selector_b",
            values={
                "type": "CustomHTLF",
                "mm_move_per_rotation": 40,
                "cam_angle": 45,
                "home_pin": "PB3",
                "MAX_ANGLE_MOVEMENT": 180,
                "selector_movement_speed": 75.0,
                "selector_movement_accel": 90.0,
                "enable_sensors_in_gui": True,
            },
        )
        assert unit.type == "CustomHTLF"
        assert unit.drive_stepper == "drive_a"
        assert unit.selector_stepper == "selector_b"
        assert unit.mm_move_per_rotation == 40
        assert unit.cam_angle == 45
        assert unit.home_pin == "PB3"
        assert unit.MAX_ANGLE_MOVEMENT == 180
        assert unit.selector_movement_speed == 75.0
        assert unit.selector_movement_accel == 90.0
        assert unit.enable_sensors_in_gui is True

    def test_enable_sensors_in_gui_falls_back_to_afc_value(self):
        """When not set in this unit's own config, enable_sensors_in_gui
        falls back to afc.enable_sensors_in_gui rather than a fixed
        default -- proven by setting the afc-level value and confirming it
        propagates with no per-unit override present. Kept separate from
        the defaults/overrides tests above since it needs its own afc
        object rather than the config values dict."""
        config, printer, afc = _make_htlf_config()
        afc.enable_sensors_in_gui = True
        unit = AFC_HTLF(config)
        assert unit.enable_sensors_in_gui is True

    # -- cam_angle validation --

    @pytest.mark.parametrize("valid_angle", [30, 45, 60])
    def test_valid_cam_angle_does_not_raise(self, valid_angle):
        unit = _make_htlf(values={"cam_angle": valid_angle})
        assert unit.cam_angle == valid_angle

    def test_invalid_cam_angle_raises_config_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _make_htlf(values={"cam_angle": 90})
        assert str(exc_info.value) == (
            "90 is not a valid cam angle, please choose from the following [30, 45, 60]"
        )

    # -- buttons / command registration (always run, independent of home_pin) --

    def test_registers_home_callback_button(self):
        config, printer, afc = _make_htlf_config(values={"home_pin": "PA1"})
        unit = AFC_HTLF(config)
        buttons_mock = printer._objects["buttons"]
        buttons_mock.register_buttons.assert_called_once_with(["PA1"], unit.home_callback)

    def test_registers_afc_home_unit_command(self):
        unit = _make_htlf()
        unit.afc.function.register_commands.assert_called_once_with(
            unit.afc.show_macros, "AFC_HOME_UNIT",
            unit.cmd_AFC_HOME_UNIT,
            description=AFC_HTLF.cmd_AFC_HOME_UNIT_help,
            options=AFC_HTLF.cmd_AFC_HOME_UNIT_options,
        )

    # -- required config options have no fallback default --

    @pytest.mark.parametrize("missing_option", [
        "drive_stepper", "selector_stepper", "cam_angle", "home_pin",
    ])
    def test_required_option_missing_from_config_raises(self, missing_option):
        """drive_stepper, selector_stepper, cam_angle and home_pin are all
        read via config.get(...)/getint(...) with no default -- real
        Klipper's ConfigWrapper raises immediately when an option like that
        isn't in the user's config, rather than silently falling back to
        None/0. Each is tested with the other three left valid, isolating
        that this specific option is the one enforced as required."""
        from tests.conftest import MockAFC, MockConfig, MockPrinter

        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        values = {
            "drive_stepper": "drive", "selector_stepper": "selector",
            "cam_angle": 60, "home_pin": "PA1",
        }
        del values[missing_option]
        config = MockConfig(name="AFC_HTLF HTLF_1", printer=printer, values=values)

        with pytest.raises(configparser.Error) as exc_info:
            AFC_HTLF(config)
        assert str(exc_info.value) == (
            f"Option '{missing_option}' in section 'Test' must be specified"
        )

    # -- home_pin-dependent endstop/filament-switch setup --

    def test_home_pin_set_creates_home_sensor(self):
        # enable_sensors_in_gui=True avoids add_filament_switch's
        # show_sensor=False rename (which prefixes the cached object's key
        # with "_"), keeping this test focused on home_sensor's identity.
        unit = _make_htlf(values={"home_pin": "PA1", "enable_sensors_in_gui": True})
        expected_name = f"filament_switch_sensor {unit.name}_home_pin"
        assert unit.home_sensor is unit.printer._objects[expected_name]

    def test_home_pin_set_strips_prefix_chars_for_allow_multi_use_pin(self):
        """allow_multi_use_pin is called with the stripped pin twice: once
        from inside add_filament_switch's own setup, once from AFC_HTLF's
        own endstop setup right after."""
        config, printer, afc = _make_htlf_config(values={"home_pin": "!PA1^"})
        AFC_HTLF(config)
        ppins = printer._objects["pins"]
        assert ppins.allow_multi_use_pin.call_args_list == [call("PA1"), call("PA1")]

    def test_home_pin_set_calls_parse_pin(self):
        config, printer, afc = _make_htlf_config(values={"home_pin": "PA1"})
        AFC_HTLF(config)
        ppins = printer._objects["pins"]
        ppins.parse_pin.assert_called_once_with("PA1", True, True)

    def test_home_pin_set_sets_home_endstop_name(self):
        unit = _make_htlf(name="HTLF_2", values={"home_pin": "PA1"})
        assert unit.home_endstop_name == "HTLF_2_home"

    def test_home_pin_set_registers_endstop_with_query_endstops(self):
        config, printer, afc = _make_htlf_config(values={"home_pin": "PA1"})
        unit = AFC_HTLF(config)
        query_endstops = printer._objects["query_endstops"]
        query_endstops.register_endstop.assert_called_once_with(
            unit.home_endstop, unit.home_endstop_name
        )

    def test_home_pin_set_adds_stepper_to_home_endstop(self):
        unit = _make_htlf(values={"home_pin": "PA1"})
        unit.home_endstop.add_stepper.assert_called_once_with(
            unit.selector_stepper_obj.extruder_stepper.stepper
        )

    def test_home_pin_set_registers_endstop_on_selector_stepper(self):
        unit = _make_htlf(values={"home_pin": "PA1"})
        assert unit.selector_stepper_obj._endstops[unit.home_endstop_name] == \
            (unit.home_endstop, unit.home_endstop_name)

    def test_falsy_home_endstop_skips_adding_stepper(self):
        """If ppins.setup_pin(...) itself returns a falsy value, self.home_
        endstop stays falsy and the add_stepper/_endstops wiring is skipped
        entirely -- defensive code for a setup_pin failure, independent of
        home_pin's own required-ness."""
        config, printer, afc = _make_htlf_config(values={"home_pin": "PA1"})
        printer.lookup_object("pins").setup_pin = MagicMock(return_value=None)
        unit = AFC_HTLF(config)
        assert unit.home_endstop is None
        assert unit.selector_stepper_obj._endstops == {}

    # -- endstop registration failure --

    def test_endstop_registration_failure_raises_config_error(self):
        config, printer, afc = _make_htlf_config(values={"home_pin": "PA1"})
        query_endstops = MagicMock()
        query_endstops.register_endstop.side_effect = Exception("boom")
        printer._objects["query_endstops"] = query_endstops
        with pytest.raises(configparser.Error) as exc_info:
            AFC_HTLF(config)
        assert str(exc_info.value) == (
            "Error trying to register home endstop for HTLF_1.\n Error:boom"
        )


# ── home_callback ─────────────────────────────────────────────────────────────

class TestHomeCallback:
    def test_home_callback_sets_home_state_true(self):
        unit = _make_htlf()
        unit.home_callback(eventtime=100.0, state=True)
        assert unit.home_state is True

    def test_home_callback_sets_home_state_false(self):
        unit = _make_htlf()
        unit.home_state = True
        unit.home_callback(eventtime=100.0, state=False)
        assert unit.home_state is False

    def test_home_callback_state_reflects_input(self):
        unit = _make_htlf()
        for state in [True, False, True]:
            unit.home_callback(eventtime=0.0, state=state)
            assert unit.home_state is state

    def test_home_callback_coerces_raw_int_state_to_bool(self):
        """Klipper's buttons.register_buttons callback contract passes raw
        int (0/1), not bool -- matches AFC_hub.switch_pin_callback and
        AFCLane.prep_callback, which both need the same bool() coercion."""
        unit = _make_htlf()
        unit.home_callback(eventtime=0.0, state=1)
        assert unit.home_state is True
        unit.home_callback(eventtime=0.0, state=0)
        assert unit.home_state is False


# ── handle_connect ────────────────────────────────────────────────────────────

class TestHandleConnect:
    """afcBoxTurtle.handle_connect (and afcUnit.handle_connect beneath it) is
    real, pre-existing behavior out of scope here -- isolated via
    patch.object so only AFC_HTLF's own override logic is exercised."""

    def test_calls_super_handle_connect(self):
        unit = _make_htlf()
        super_mock = MagicMock()
        with patch.object(afcBoxTurtle, "handle_connect", super_mock):
            unit.handle_connect()
        super_mock.assert_called_once_with()

    def test_sets_htlf_specific_logo(self):
        unit = _make_htlf()
        with patch.object(afcBoxTurtle, "handle_connect", MagicMock()):
            unit.handle_connect()
        assert unit.logo == '<span class=success--text>HTLF Ready\n</span>'

    def test_sets_htlf_specific_logo_error(self):
        unit = _make_htlf()
        with patch.object(afcBoxTurtle, "handle_connect", MagicMock()):
            unit.handle_connect()
        assert unit.logo_error == '<span class=error--text>HTLF Not Ready</span>\n'


# ── system_Test ───────────────────────────────────────────────────────────────

class TestSystemTest:
    """afcBoxTurtle.system_Test is real, pre-existing behavior out of scope
    here -- isolated via patch.object so only AFC_HTLF's own wrapper logic
    (prep-homing, return_to_home bookkeeping, combined return value) is
    exercised."""

    def _make(self, prep_homed=False, super_return=True):
        unit = _make_htlf()
        unit.prep_homed = prep_homed
        unit.return_to_home = MagicMock()
        cur_lane = MagicMock()
        cur_lane.load_state = True
        cur_lane.prep_state = False
        return unit, cur_lane, super_return

    def test_sets_prep_state_from_load_state(self):
        unit, cur_lane, _ = self._make()
        with patch.object(afcBoxTurtle, "system_Test", MagicMock(return_value=True)):
            unit.system_Test(cur_lane, 1.0, False, True)
        assert cur_lane.prep_state is True

    def test_calls_return_to_home_prep_first_when_not_prep_homed(self):
        unit, cur_lane, _ = self._make(prep_homed=False)
        with patch.object(afcBoxTurtle, "system_Test", MagicMock(return_value=True)):
            unit.system_Test(cur_lane, 1.0, False, True)
        assert unit.return_to_home.call_args_list[0] == \
            call(prep=True, disable_selector=False)

    def test_skips_prep_return_to_home_when_already_prep_homed(self):
        """When prep_homed is already True, only the final unconditional
        return_to_home() call happens -- proving the guard actually skipped
        the prep=True call rather than it happening to look the same."""
        unit, cur_lane, _ = self._make(prep_homed=True)
        with patch.object(afcBoxTurtle, "system_Test", MagicMock(return_value=True)):
            unit.system_Test(cur_lane, 1.0, False, True)
        assert unit.return_to_home.call_args_list == [call()]

    def test_always_calls_return_to_home_unconditionally_after_super(self):
        unit, cur_lane, _ = self._make(prep_homed=False)
        with patch.object(afcBoxTurtle, "system_Test", MagicMock(return_value=True)):
            unit.system_Test(cur_lane, 1.0, False, True)
        assert unit.return_to_home.call_args_list[-1] == call()

    def test_calls_super_system_test_with_correct_args(self):
        unit, cur_lane, _ = self._make(prep_homed=True)
        super_mock = MagicMock(return_value=True)
        with patch.object(afcBoxTurtle, "system_Test", super_mock):
            unit.system_Test(cur_lane, 2.5, True, False)
        super_mock.assert_called_once_with(cur_lane, 2.5, True, False)

    def test_returns_true_when_prep_homed_true_and_status_true(self):
        unit, cur_lane, _ = self._make(prep_homed=True)
        with patch.object(afcBoxTurtle, "system_Test", MagicMock(return_value=True)):
            result = unit.system_Test(cur_lane, 1.0, False, True)
        assert result is True

    def test_returns_false_when_prep_homed_false_even_if_status_true(self):
        """Proves the "and" actually depends on prep_homed, not just status --
        status alone is True here yet the result must be False."""
        unit, cur_lane, _ = self._make(prep_homed=False)
        # return_to_home is mocked out, so it won't flip prep_homed to True.
        with patch.object(afcBoxTurtle, "system_Test", MagicMock(return_value=True)):
            result = unit.system_Test(cur_lane, 1.0, False, True)
        assert result is False

    def test_returns_false_when_status_false_even_if_prep_homed_true(self):
        """Proves the "and" actually depends on status, not just prep_homed --
        prep_homed alone is True here yet the result must be False."""
        unit, cur_lane, _ = self._make(prep_homed=True)
        with patch.object(afcBoxTurtle, "system_Test", MagicMock(return_value=False)):
            result = unit.system_Test(cur_lane, 1.0, False, True)
        assert result is False


# ── cmd_AFC_HOME_UNIT ─────────────────────────────────────────────────────────

class TestCmdAfcHomeUnit:
    def test_calls_return_to_home(self):
        unit = _make_htlf()
        unit.return_to_home = MagicMock()
        unit.cmd_AFC_HOME_UNIT(MagicMock())
        unit.return_to_home.assert_called_once_with()


# ── return_to_home ───────────────────────────────────────────────────────────

def _convergent_move_selector_home(unit, iterations=1, homed_distance=5.0):
    """Side effect for a mocked _move_selector_home: sets unit.home_state to
    True once called `iterations` times, and stashes homed_distance into
    unit._homed_distance on every call (mirrors what the real method does
    when homing_enabled). Used to make return_to_home's while loop converge
    deterministically instead of spinning forever on a fully-mocked stepper."""
    state = {"n": 0}
    def _side_effect(distance):
        state["n"] += 1
        unit._homed_distance = homed_distance
        if state["n"] >= iterations:
            unit.home_state = True
    return _side_effect


class TestReturnToHome:
    # -- fast-move block: "current_selected_lane is not None and not
    #    home_state and not prep" (each condition tested independently) --

    def test_fast_move_skipped_when_current_selected_lane_none(self):
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.home_state = True  # also short-circuits the loop
        unit._move_selector_home = MagicMock()
        unit.return_to_home()
        unit._move_selector_home.assert_not_called()

    def test_fast_move_skipped_when_home_state_already_true(self):
        unit = _make_htlf()
        unit.current_selected_lane = MagicMock(index=1)
        unit.home_state = True
        unit._move_selector_home = MagicMock()
        unit.return_to_home()
        unit._move_selector_home.assert_not_called()

    def test_fast_move_skipped_when_prep_true(self):
        """Distinguished from "fast-move ran" via call count: if the fast
        move (skipped here) had run, _move_selector_home would be called
        twice (fast-move + one loop iteration) instead of once."""
        unit = _make_htlf()
        unit.current_selected_lane = MagicMock(index=2)
        unit.home_state = False
        unit.afc.homing_enabled = True
        unit._move_selector_home = MagicMock(
            side_effect=_convergent_move_selector_home(unit, iterations=1)
        )
        unit.return_to_home(prep=True)
        assert unit._move_selector_home.call_count == 1

    def test_fast_move_runs_with_200_when_all_conditions_true_and_homing_enabled(self):
        unit = _make_htlf()
        unit.current_selected_lane = MagicMock(index=2)
        unit.home_state = False
        unit.afc.homing_enabled = True
        # iterations=2: the fast-move call is the 1st, so home_state only
        # flips true on the 2nd (first loop) call -- proving both happened.
        unit._move_selector_home = MagicMock(
            side_effect=_convergent_move_selector_home(unit, iterations=2)
        )
        unit.return_to_home(prep=False)
        assert unit._move_selector_home.call_args_list[0] == call(200)
        assert unit._move_selector_home.call_count == 2  # fast-move + 1 loop iteration

    def test_fast_move_uses_calculated_distance_when_homing_disabled(self):
        unit = _make_htlf()
        lane = MagicMock(index=3)
        unit.current_selected_lane = lane
        unit.home_state = False
        unit.afc.homing_enabled = False
        unit.calculate_lobe_movement = MagicMock(return_value=17.5)
        unit._move_selector_home = MagicMock(
            side_effect=_convergent_move_selector_home(unit, iterations=1)
        )
        unit.return_to_home(prep=False)
        unit.calculate_lobe_movement.assert_called_once_with(3)
        assert unit._move_selector_home.call_args_list[0] == call(17.5)

    # -- main while loop --

    def test_loop_skipped_when_home_state_already_true(self):
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.home_state = True
        unit.prep_homed = False
        unit._move_selector_home = MagicMock()
        result = unit.return_to_home()
        unit._move_selector_home.assert_not_called()
        assert result is True
        assert unit.prep_homed is True

    def test_loop_runs_until_home_state_becomes_true(self):
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.home_state = False
        unit.afc.homing_enabled = True
        unit._move_selector_home = MagicMock(
            side_effect=_convergent_move_selector_home(unit, iterations=3, homed_distance=2.0)
        )
        result = unit.return_to_home()
        assert unit._move_selector_home.call_count == 3
        assert result is True

    def test_loop_uses_move_distance_1_when_homing_disabled(self):
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.home_state = False
        unit.afc.homing_enabled = False
        unit._move_selector_home = MagicMock(
            side_effect=_convergent_move_selector_home(unit, iterations=1)
        )
        unit.return_to_home()
        unit._move_selector_home.assert_called_once_with(1)

    def test_total_moved_accumulates_homed_distance_when_homing_enabled(self):
        """Verified via the failed_to_home threshold: with homing_enabled
        True, each iteration adds self._homed_distance (30, already over the
        ~24.44 threshold for the default mm_move_per_rotation/MAX_ANGLE_
        MOVEMENT/cam_angle) to total_moved, tripping failure on the very
        first iteration -- proving total_moved is driven by _homed_distance,
        not move_distance, in this branch."""
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.home_state = False
        unit.afc.homing_enabled = True
        unit._move_selector_home = MagicMock(
            side_effect=lambda d: setattr(unit, "_homed_distance", 30.0)
        )
        result = unit.return_to_home()
        assert result is False
        assert unit.failed_to_home is True
        assert unit._move_selector_home.call_count == 1
        unit.afc.error.AFC_error.assert_called_once_with(
            f"Failed to home {unit.name}", False
        )

    def test_total_moved_accumulates_move_distance_when_homing_disabled(self):
        """With homing_enabled False, move_distance is fixed at 1 per
        iteration, so the ~24.44 threshold isn't crossed until the 25th
        call -- computed independently here rather than mirroring the
        source's own formula."""
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.home_state = False
        unit.afc.homing_enabled = False
        unit._move_selector_home = MagicMock()  # never sets home_state
        result = unit.return_to_home()
        assert result is False
        assert unit.failed_to_home is True
        assert unit._move_selector_home.call_count == 25

    def test_prep_homed_not_changed_when_failed_to_home(self):
        """The early-return failure path exits before "self.prep_homed =
        True", so prep_homed must remain whatever it was set to before."""
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.home_state = False
        unit.prep_homed = False
        unit.afc.homing_enabled = True
        unit._move_selector_home = MagicMock(
            side_effect=lambda d: setattr(unit, "_homed_distance", 30.0)
        )
        unit.return_to_home()
        assert unit.prep_homed is False

    # -- success-path bookkeeping --

    def test_prep_homed_set_true_on_success(self):
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.home_state = True
        unit.prep_homed = False
        unit.return_to_home()
        assert unit.prep_homed is True

    def test_current_selected_lane_reset_to_none_on_success(self):
        unit = _make_htlf()
        unit.current_selected_lane = MagicMock(index=1)
        unit.home_state = True
        unit.return_to_home()
        assert unit.current_selected_lane is None

    def test_disable_selector_true_calls_do_enable_false_on_success(self):
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.home_state = True
        unit.return_to_home(disable_selector=True)
        unit.selector_stepper_obj.do_enable.assert_called_once_with(False)

    def test_disable_selector_false_skips_do_enable_on_success(self):
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.home_state = True
        unit.return_to_home(disable_selector=False)
        unit.selector_stepper_obj.do_enable.assert_not_called()

    def test_returns_true_on_success(self):
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.home_state = True
        assert unit.return_to_home() is True


# ── calculate_lobe_movement ──────────────────────────────────────────────────

class TestCalculateLobeMovement:
    def test_lane_index_one_uses_full_max_angle(self):
        unit = _make_htlf()
        unit.MAX_ANGLE_MOVEMENT = 215
        unit.cam_angle = 60
        unit.mm_move_per_rotation = 32
        # Computed independently: angle = 215 - (0 * 60) = 215;
        # (32/360)*215 = 19.111...
        result = unit.calculate_lobe_movement(1)
        assert result == pytest.approx(19.111111111111114)

    def test_higher_lane_index_reduces_angle_by_cam_angle_per_step(self):
        unit = _make_htlf()
        unit.MAX_ANGLE_MOVEMENT = 215
        unit.cam_angle = 60
        unit.mm_move_per_rotation = 32
        # Computed independently: angle = 215 - (2 * 60) = 95;
        # (32/360)*95 = 8.444...
        result = unit.calculate_lobe_movement(3)
        assert result == pytest.approx(8.444444444444445)

    def test_logs_debug_with_angle(self):
        unit = _make_htlf()
        unit.MAX_ANGLE_MOVEMENT = 215
        unit.cam_angle = 60
        unit.calculate_lobe_movement(2)
        # angle = 215 - (1 * 60) = 155
        assert unit.logger.messages == [("debug", "HTLF: Lobe Movement angle : 155")]


# ── select_lane ───────────────────────────────────────────────────────────────

class TestSelectLane:
    def test_stepper_in_fullname_returns_false_immediately(self):
        """"stepper" in lane.fullname.lower() short-circuits before the try
        block, so the finally's do_enable(False) must not fire either, even
        with disable_selector=True."""
        unit = _make_htlf()
        lane = MagicMock()
        lane.fullname = "AFC_stepper lane1"
        result = unit.select_lane(lane, disable_selector=True)
        assert result == (False, 0.0)
        unit.selector_stepper_obj.do_enable.assert_not_called()

    def test_different_lane_and_home_succeeds_moves_and_selects(self):
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.return_to_home = MagicMock(return_value=True)
        unit._homed_distance = 3.5
        unit.calculate_lobe_movement = MagicMock(return_value=12.0)
        unit._selector_cal_dis_adjust = MagicMock()
        lane = MagicMock()
        lane.fullname = "AFC_HTLF lane2"
        lane.index = 2
        lane.name = "lane2"
        result = unit.select_lane(lane)
        unit.selector_stepper_obj.move.assert_called_once_with(
            12.0, unit.selector_movement_speed, unit.selector_movement_accel, False
        )
        unit._selector_cal_dis_adjust.assert_called_once_with(lane)
        assert unit.current_selected_lane is lane
        assert result == (True, 3.5)

    def test_different_lane_and_home_fails_logs_error_and_returns_false(self):
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.return_to_home = MagicMock(return_value=False)
        lane = MagicMock()
        lane.fullname = "AFC_HTLF lane2"
        lane.name = "lane2"
        result = unit.select_lane(lane)
        assert result == (False, 0.0)
        assert unit.current_selected_lane is None
        assert unit.logger.messages == [
            ("debug", "HTLF: HTLF_1 Homing to endstop."),
            ("error", "HTLF: failed to home when selecting lane2"),
        ]

    def test_home_called_with_disable_selector_false(self):
        """return_to_home is always called with disable_selector=False from
        here, independent of select_lane's own disable_selector param."""
        unit = _make_htlf()
        unit.current_selected_lane = None
        unit.return_to_home = MagicMock(return_value=True)
        lane = MagicMock()
        lane.fullname = "AFC_HTLF lane2"
        lane.index = 1
        unit.select_lane(lane, disable_selector=True)
        unit.return_to_home.assert_called_once_with(disable_selector=False)

    def test_already_selected_lane_skips_homing_and_returns_true(self):
        unit = _make_htlf()
        lane = MagicMock()
        lane.fullname = "AFC_HTLF lane2"
        unit.current_selected_lane = lane
        unit.return_to_home = MagicMock()
        result = unit.select_lane(lane)
        unit.return_to_home.assert_not_called()
        assert result == (True, 0.0)

    def test_disable_selector_true_calls_do_enable_in_finally(self):
        unit = _make_htlf()
        lane = MagicMock()
        lane.fullname = "AFC_HTLF lane2"
        unit.current_selected_lane = lane  # already-selected path
        unit.select_lane(lane, disable_selector=True)
        unit.selector_stepper_obj.do_enable.assert_called_once_with(False)

    def test_disable_selector_false_skips_do_enable_in_finally(self):
        unit = _make_htlf()
        lane = MagicMock()
        lane.fullname = "AFC_HTLF lane2"
        unit.current_selected_lane = lane  # already-selected path
        unit.select_lane(lane, disable_selector=False)
        unit.selector_stepper_obj.do_enable.assert_not_called()


# ── check_runout ──────────────────────────────────────────────────────────────

class TestCheckRunout:
    def _make(self):
        unit = _make_htlf()
        cur_lane = MagicMock()
        cur_lane.name = "lane1"
        cur_lane.status = AFCLaneState.LOADED
        unit.afc.function.get_current_lane.return_value = "lane1"
        unit.afc.function.is_printing.return_value = True
        return unit, cur_lane

    def test_true_when_all_conditions_met(self):
        unit, cur_lane = self._make()
        assert unit.check_runout(cur_lane) is True

    def test_false_when_lane_name_does_not_match_current_lane(self):
        unit, cur_lane = self._make()
        unit.afc.function.get_current_lane.return_value = "lane_other"
        assert unit.check_runout(cur_lane) is False

    def test_false_when_not_printing(self):
        unit, cur_lane = self._make()
        unit.afc.function.is_printing.return_value = False
        assert unit.check_runout(cur_lane) is False

    def test_false_when_status_is_ejecting(self):
        unit, cur_lane = self._make()
        cur_lane.status = AFCLaneState.EJECTING
        assert unit.check_runout(cur_lane) is False

    def test_false_when_status_is_calibrating(self):
        unit, cur_lane = self._make()
        cur_lane.status = AFCLaneState.CALIBRATING
        assert unit.check_runout(cur_lane) is False


# ── prep_load / prep_post_load ───────────────────────────────────────────────

class TestPrepLoad:
    def test_returns_none(self):
        unit = _make_htlf()
        assert unit.prep_load(MagicMock()) is None


class TestPrepPostLoad:
    def test_returns_none(self):
        unit = _make_htlf()
        assert unit.prep_post_load(MagicMock()) is None


# ═════════════════════════════════════════════════════════════════════════
# Module-level import guards
# ═════════════════════════════════════════════════════════════════════════

def _exec_afc_htlf_with_blocked_dependency(blocked_module_name):
    """Execute a throw-away copy of extras/AFC_HTLF.py's module-level code
    with `blocked_module_name` forced to fail import, to exercise the file's
    top-level ``try: from X import Y / except: raise config_error(...)``
    guards.

    Never touches the real, already-imported ``extras.AFC_HTLF`` module that
    the rest of this test suite depends on: the copy is loaded under a
    throwaway module name and discarded afterward, whether or not it raises.
    Cleanup restores the exact same pre-existing module object in
    sys.modules so other test files' references stay valid.
    """
    import extras.AFC_HTLF as real_module
    fresh_name = "extras.AFC_HTLF_import_guard_probe"
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


def _exec_afc_htlf_with_missing_attr(real_module_name, missing_attr_name):
    """Execute a throw-away copy of extras/AFC_HTLF.py's module-level code
    with a single attribute (`missing_attr_name`) hidden from
    `real_module_name`, to exercise a guard whose `except:` can't be reached
    by blocking the whole dependency module.

    AFC_HTLF.py has two separate guards that both import from
    extras.AFC_utils (ERROR_STR, then add_filament_switch); blocking that
    module outright always trips the first of the two guards before
    execution ever reaches the second. This swaps in a proxy that forwards
    every attribute lookup to the real module except the one being hidden,
    which raises AttributeError -- exactly what `from module import name`
    converts into ImportError when the name is genuinely missing from an
    otherwise-importable module.
    """
    import extras.AFC_HTLF as real_module
    real_dep_module = sys.modules[real_module_name]

    class _ProxyModule:
        def __getattr__(self, attr_name):
            if attr_name == missing_attr_name:
                raise AttributeError(attr_name)
            return getattr(real_dep_module, attr_name)

    fresh_name = "extras.AFC_HTLF_import_guard_probe"
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
    """Covers the four module-level `try/except: raise config_error(...)`
    guards in AFC_HTLF.py, one per dependency import."""

    def test_afc_utils_error_str_import_failure_raises_configparser_error(self):
        """The very first guard imports ERROR_STR itself from AFC_utils, so
        it can't use ERROR_STR.format(...) in its own except clause."""
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_htlf_with_blocked_dependency("extras.AFC_utils")
        assert str(exc_info.value).startswith(
            "Error when trying to import AFC_utils.ERROR_STR"
        )

    def test_afc_lane_import_failure_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_htlf_with_blocked_dependency("extras.AFC_lane")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_lane, please rerun install-afc.sh"
        )

    def test_afc_boxturtle_import_failure_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_htlf_with_blocked_dependency("extras.AFC_BoxTurtle")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_BoxTurtle, please rerun install-afc.sh"
        )

    def test_afc_utils_add_filament_switch_import_failure_raises_configparser_error(self):
        """The fourth guard imports add_filament_switch from the same
        AFC_utils module as the first guard's ERROR_STR, so it needs a
        specific missing attribute rather than the whole module blocked."""
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_htlf_with_missing_attr("extras.AFC_utils", "add_filament_switch")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_utils, please rerun install-afc.sh"
        )


# ── _move_selector_home ──────────────────────────────────────────────────────

class TestMoveSelectorHome:
    def test_homing_enabled_calls_do_homing_move_with_negative_distance(self):
        unit = _make_htlf()
        unit.afc.homing_enabled = True
        unit.selector_movement_speed = 50.0
        unit.selector_movement_accel = 75.0
        unit.home_endstop_name = "HTLF_1_home"
        unit.selector_stepper_obj.do_homing_move.return_value = (True, 12.5)
        unit._move_selector_home(10.0)
        unit.selector_stepper_obj.do_homing_move.assert_called_once_with(
            -10.0, 50.0, 75.0, "HTLF_1_home", assist_active=False
        )

    def test_homing_enabled_stores_homed_distance(self):
        """Verifies the class variable self._homed_distance is actually
        updated from do_homing_move's return, not just that the method ran."""
        unit = _make_htlf()
        unit.afc.homing_enabled = True
        unit.selector_stepper_obj.do_homing_move.return_value = (True, 42.5)
        unit._move_selector_home(10.0)
        assert unit._homed_distance == 42.5

    def test_homing_enabled_logs_success_and_distance(self):
        unit = _make_htlf()
        unit.afc.homing_enabled = True
        unit.selector_stepper_obj.do_homing_move.return_value = (False, 7.0)
        unit._move_selector_home(10.0)
        assert unit.logger.messages == [
            ("debug", "HTLF: Homing done, success:False, distance:7.0")
        ]

    def test_homing_disabled_calls_move_with_negative_distance(self):
        unit = _make_htlf()
        unit.afc.homing_enabled = False
        unit.selector_movement_speed = 50.0
        unit.selector_movement_accel = 75.0
        unit._move_selector_home(10.0)
        unit.selector_stepper_obj.move.assert_called_once_with(-10.0, 50.0, 75.0, False)

    def test_homing_disabled_does_not_call_do_homing_move(self):
        unit = _make_htlf()
        unit.afc.homing_enabled = False
        unit._move_selector_home(10.0)
        unit.selector_stepper_obj.do_homing_move.assert_not_called()


# ── load_config_prefix ───────────────────────────────────────────────────────

class TestLoadConfigPrefix:
    def test_returns_afc_htlf_instance(self):
        from extras.AFC_HTLF import load_config_prefix
        config, printer, afc = _make_htlf_config()
        result = load_config_prefix(config)
        assert isinstance(result, AFC_HTLF)
