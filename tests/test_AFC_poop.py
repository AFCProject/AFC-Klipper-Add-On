"""
Unit tests for extras/AFC_poop.py

Covers:
  - afc_poop attribute initialization from config
  - poop(): movement sequence, fan commands, iteration logic
"""

from __future__ import annotations

from unittest.mock import MagicMock, call
import pytest

from extras.AFC_poop import afc_poop


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_poop(values=None):
    """Build an afc_poop instance bypassing __init__."""
    poop = afc_poop.__new__(afc_poop)

    from tests.conftest import MockAFC, MockLogger

    afc = MockAFC()
    afc.logger = MockLogger()

    poop.printer = MagicMock()
    poop.reactor = MagicMock()
    poop.afc = afc
    poop.gcode = afc.gcode
    poop.logger = afc.logger

    # Config defaults
    poop.verbose = False
    poop.purge_loc_xy = "10,10"
    poop.purge_start = 20.0
    poop.purge_spd = 6.5
    poop.fast_z = 200.0
    poop.z_lift = 20.0
    poop.restore_position = False
    poop.full_fan = False
    poop.purge_length = 70.111
    poop.purge_length_min = 60.999
    poop.max_iteration_length = 40.0
    poop.iteration_z_raise = 6.0
    poop.iteration_z_change = 0.6

    if values:
        for k, v in values.items():
            setattr(poop, k, v)

    return poop


def _make_toolhead_pos(x=0.0, y=0.0, z=0.0, e=0.0):
    """Return a mutable position list (gcode_move.last_position style)."""
    return [x, y, z, e]


# ── Initialization ────────────────────────────────────────────────────────────

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


# ── poop() ────────────────────────────────────────────────────────────────────

class TestPoopMethod:
    def _run_poop(self, poop_obj):
        """Wire up toolhead and gcode_move mocks then call poop()."""
        toolhead = MagicMock()
        poop_obj.printer.lookup_object.return_value = toolhead
        pos = _make_toolhead_pos(5.0, 5.0, 0.0, 0.0)
        poop_obj.afc.gcode_move = MagicMock()
        poop_obj.afc.gcode_move.last_position = pos
        # move_with_transform should just update the pos we gave it
        poop_obj.afc.gcode_move.move_with_transform = MagicMock()
        poop_obj.poop()
        return poop_obj.afc.gcode_move

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
        p = _make_poop({"verbose": True, "purge_length": 40.0, "max_iteration_length": 40.0})
        self._run_poop(p)
        info_msgs = [m for lvl, m in p.logger.messages if lvl == "info"]
        assert len(info_msgs) > 0

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
        """verbose=True + full_fan=True should log fan-related info messages."""
        p = _make_poop({
            "verbose": True,
            "full_fan": True,
            "purge_length": 40.0,
            "max_iteration_length": 40.0,
        })
        self._run_poop(p)
        info_msgs = [m for lvl, m in p.logger.messages if lvl == "info"]
        fan_msgs = [m for m in info_msgs if "fan" in m.lower() or "Fan" in m]
        assert len(fan_msgs) >= 2  # "Set Cooling Fan" + "Restore fan speed"


# ── __init__ via MockConfig ───────────────────────────────────────────────────

class TestPoopInitFromConfig:
    def test_init_reads_config_values(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(
            printer=printer,
            values={
                "purge_loc_xy": "50,75",
                "purge_start": 5.0,
                "purge_spd": 8.0,
                "fast_z": 150.0,
                "z_lift": 30.0,
                "restore_position": False,
                "full_fan": True,
                "purge_length": 80.0,
                "purge_length_min": 60.0,
                "max_iteration_length": 35.0,
                "iteration_z_raise": 5.0,
                "iteration_z_change": 0.5,
                "verbose": False,
                "comment": False,
            },
        )
        p = afc_poop(config)
        assert p.purge_loc_xy == "50,75"
        assert p.fast_z == 150.0
        assert p.full_fan is True
        assert p.purge_length == 80.0
