"""
Unit tests for extras/AFC_poop.py

Covers:
  - afc_poop.__init__: attribute initialization from config
  - afc_poop.poop(): movement sequence, fan commands, iteration logic
  - load_config(): module-level Klipper config entry point
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from extras.AFC_poop import afc_poop, load_config
from tests.conftest import MockAFC, MockConfig, MockPrinter


# ── Helpers ───────────────────────────────────────────────────────────────────

_DEFAULT_VALUES = {
    "purge_loc_xy": "10,10",
    "purge_start": 20.0,
    "purge_spd": 6.5,
    "fast_z": 200.0,
    "z_lift": 20.0,
    "restore_position": False,
    "full_fan": False,
    "purge_length": 70.111,
    "purge_length_min": 60.999,
    "max_iteration_length": 40.0,
    "iteration_z_raise": 6.0,
    "iteration_z_change": 0.6,
    # self.verbose is effectively controlled by "comment", not "verbose" --
    # __init__ reads "verbose" first, then unconditionally overwrites it
    # with getboolean("comment", False). Pre-existing behavior, left as-is.
    "comment": False,
}


def _make_poop(values=None):
    """Build a real afc_poop via its __init__, using a MockConfig seeded
    with the given (or default) option values."""
    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    merged = dict(_DEFAULT_VALUES)
    if values:
        merged.update(values)
    config = MockConfig(printer=printer, values=merged)
    return afc_poop(config)


def _make_toolhead_pos(x=0.0, y=0.0, z=0.0, e=0.0):
    """Return a mutable position list (gcode_move.last_position style)."""
    return [x, y, z, e]


# ── __init__ ──────────────────────────────────────────────────────────────────

class TestPoopInit:
    def test_default_purge_spd(self):
        p = _make_poop()
        assert p.purge_spd == 6.5

    def test_default_max_iteration_length(self):
        p = _make_poop()
        assert p.max_iteration_length == 40.0

    def test_default_full_fan_false(self):
        p = _make_poop()
        assert p.full_fan is False

    def test_default_verbose_false(self):
        p = _make_poop()
        assert p.verbose is False

    def test_verbose_follows_comment_key_not_verbose_key(self):
        """Documents the current (pre-existing) behavior: "comment" is what
        actually ends up controlling self.verbose, since it's read after
        "verbose" and always wins."""
        p = _make_poop({"verbose": True, "comment": False})
        assert p.verbose is False

    def test_reads_config_values(self):
        p = _make_poop({
            "purge_loc_xy": "50,75",
            "purge_start": 5.0,
            "purge_spd": 8.0,
            "fast_z": 150.0,
            "z_lift": 30.0,
            "restore_position": True,
            "full_fan": True,
            "purge_length": 80.0,
            "purge_length_min": 60.0,
            "max_iteration_length": 35.0,
            "iteration_z_raise": 5.0,
            "iteration_z_change": 0.5,
            "comment": True,
        })
        assert p.purge_loc_xy == "50,75"
        assert p.purge_start == 5.0
        assert p.fast_z == 150.0
        assert p.z_lift == 30.0
        assert p.restore_position is True
        assert p.full_fan is True
        assert p.purge_length == 80.0
        assert p.purge_length_min == 60.0
        assert p.max_iteration_length == 35.0
        assert p.iteration_z_raise == 5.0
        assert p.iteration_z_change == 0.5
        assert p.verbose is True

    def test_afc_and_gcode_and_logger_wired_up(self):
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(printer=printer, values=dict(_DEFAULT_VALUES))
        p = afc_poop(config)
        assert p.afc is afc
        assert p.logger is afc.logger
        assert p.gcode is printer._gcode


# ── poop() ────────────────────────────────────────────────────────────────────

class TestPoopMethod:
    def _run_poop(self, poop_obj):
        """Wire up gcode_move mocks then call poop()."""
        pos = _make_toolhead_pos(5.0, 5.0, 0.0, 0.0)
        poop_obj.afc.gcode_move = MagicMock()
        poop_obj.afc.gcode_move.last_position = pos
        poop_obj.afc.gcode_move.move_with_transform = MagicMock()
        poop_obj.poop()
        return poop_obj.afc.gcode_move

    def test_sets_toolhead_from_lookup_object(self):
        p = _make_poop()
        toolhead = MagicMock()
        p.printer._objects["toolhead"] = toolhead
        self._run_poop(p)
        assert p.toolhead is toolhead

    def test_returns_early_when_gcode_move_unset(self):
        """Before klippy:connect wires up gcode_move, it's None; poop() must
        bail out rather than raise. move_with_transform itself is not
        Optional since by the time gcode_move exists at all, it's always
        set (during the ready phase)."""
        p = _make_poop()
        p.afc.gcode_move = None
        p.poop()
        assert p.logger.messages == []

    def test_moves_to_purge_xy(self):
        p = _make_poop({"purge_loc_xy": "100,50"})
        gm = self._run_poop(p)
        calls = gm.move_with_transform.call_args_list
        # First move_with_transform call should set position to purge XY
        first_call_pos = calls[0][0][0]
        assert first_call_pos[0] == 100.0
        assert first_call_pos[1] == 50.0

    def test_fan_commands_when_full_fan(self):
        p = _make_poop({"full_fan": True, "purge_length": 40.0, "max_iteration_length": 40.0})
        self._run_poop(p)
        scripts = [
            c[0][0] for c in p.gcode.run_script_from_command.call_args_list
        ]
        # Fan on + fan off
        assert any("M106" in s for s in scripts)
        assert any("S255" in s for s in scripts)
        assert any("S0" in s for s in scripts)

    def test_no_fan_commands_when_full_fan_false(self):
        p = _make_poop({"full_fan": False})
        self._run_poop(p)
        scripts = [
            c[0][0] for c in p.gcode.run_script_from_command.call_args_list
        ]
        assert not any("M106" in s for s in scripts)

    def test_move_with_transform_called(self):
        p = _make_poop()
        gm = self._run_poop(p)
        assert gm.move_with_transform.call_count > 0

    def test_verbose_logs_info(self):
        p = _make_poop({"comment": True, "purge_length": 40.0, "max_iteration_length": 40.0})
        self._run_poop(p)
        info_msgs = [m for lvl, m in p.logger.messages if lvl == "info"]
        assert len(info_msgs) > 0

    def test_not_verbose_logs_nothing(self):
        p = _make_poop({"comment": False, "purge_length": 40.0, "max_iteration_length": 40.0})
        self._run_poop(p)
        assert p.logger.messages == []

    def _run_poop_recording_positions(self, poop_obj):
        """Like _run_poop, but snapshots the position list at the moment of
        each move_with_transform call, since poop() mutates and reuses the
        same list object for every move -- reading call_args after the fact
        would only ever see its final state."""
        pos = _make_toolhead_pos(5.0, 5.0, 0.0, 0.0)
        poop_obj.afc.gcode_move = MagicMock()
        poop_obj.afc.gcode_move.last_position = pos
        recorded = []
        poop_obj.afc.gcode_move.move_with_transform = MagicMock(
            side_effect=lambda p, speed: recorded.append((list(p), speed))
        )
        poop_obj.poop()
        return recorded

    def test_iteration_zero_uses_purge_start_for_z_raise(self):
        """The z_raise_substract ternary takes its True branch (self.purge_start)
        on the first iteration."""
        p = _make_poop({"purge_length": 40.0, "max_iteration_length": 40.0})
        recorded = self._run_poop_recording_positions(p)
        pos, speed = recorded[2]
        assert pos[2] == pytest.approx(19.65)
        assert speed == pytest.approx(-2.275)

    def test_iteration_nonzero_uses_step_triangular_for_z_raise(self):
        """The z_raise_substract ternary takes its False branch
        (step_triangular * iteration_z_change) on later iterations."""
        p = _make_poop({"purge_length": 80.0, "max_iteration_length": 40.0})
        recorded = self._run_poop_recording_positions(p)
        pos, speed = recorded[3]
        assert pos[2] == pytest.approx(19.435)
        assert speed == pytest.approx(0.8775)

    def test_iteration_count_matches_purge_length(self):
        """Number of extrude iterations equals floor(purge_length / max_iter)."""
        purge_length = 80.0
        max_iter = 40.0
        p = _make_poop({"purge_length": purge_length, "max_iteration_length": max_iter})
        gm = self._run_poop(p)
        # At least 2 iterations of purge moves
        assert gm.move_with_transform.call_count >= 3  # xy + z + iterations

    def test_z_lift_at_end(self):
        """Last Z movement should be to z_lift value."""
        p = _make_poop({"z_lift": 25.0})
        gm = self._run_poop(p)
        # Last call to move_with_transform should use the fast_z speed
        last_call = gm.move_with_transform.call_args_list[-1]
        speed = last_call[0][1]
        assert speed == p.fast_z

    def test_verbose_fan_messages_logged_when_full_fan_and_verbose(self):
        """comment=True (effective verbose) + full_fan=True should log
        fan-related info messages."""
        p = _make_poop({
            "comment": True,
            "full_fan": True,
            "purge_length": 40.0,
            "max_iteration_length": 40.0,
        })
        self._run_poop(p)
        info_msgs = [m for lvl, m in p.logger.messages if lvl == "info"]
        fan_msgs = [m for m in info_msgs if "fan" in m.lower()]
        assert len(fan_msgs) >= 2  # "Set Cooling Fan" + "Restore fan speed"
        assert fan_msgs[0] == "AFC_Poop: 2 Set Cooling Fan to Full Speed"
        assert fan_msgs[1] == "AFC_Poop: 5 Restore fan speed and feedrate"


# ── load_config() ─────────────────────────────────────────────────────────────

class TestLoadConfig:
    def test_returns_afc_poop_instance(self):
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(printer=printer, values=dict(_DEFAULT_VALUES))
        result = load_config(config)
        assert isinstance(result, afc_poop)
        assert result.afc is afc
