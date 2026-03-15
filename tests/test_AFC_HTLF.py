"""
Unit tests for extras/AFC_HTLF.py

Covers:
  - AFC_HTLF: class constants
  - AFC_HTLF.home_callback: sets home_state from button event
  - AFC_HTLF: is subclass of afcBoxTurtle
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from extras.AFC_HTLF import AFC_HTLF
from extras.AFC_BoxTurtle import afcBoxTurtle


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_htlf(name="HTLF_1"):
    """Build an AFC_HTLF bypassing the complex __init__."""
    unit = AFC_HTLF.__new__(AFC_HTLF)

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
    unit.full_name = ["AFC_HTLF", name]
    unit.lanes = {}
    unit.hub_obj = None
    unit.extruder_obj = None
    unit.buffer_obj = None
    unit.hub = None
    unit.extruder = None
    unit.buffer_name = None
    unit.td1_defined = False
    unit.type = "HTLF"
    unit.gcode = afc.gcode
    unit.drive_stepper = "drive"
    unit.selector_stepper = "selector"
    unit.drive_stepper_obj = MagicMock()
    unit.selector_stepper_obj = MagicMock()
    unit.current_selected_lane = None
    unit.home_state = False
    unit.mm_move_per_rotation = 32
    unit.cam_angle = 60
    unit.home_pin = "PA1"
    unit.MAX_ANGLE_MOVEMENT = 215
    unit.enable_sensors_in_gui = False
    unit.prep_homed = False
    unit.failed_to_home = False
    unit.lobe_current_pos = 0

    return unit


# ── Inheritance ───────────────────────────────────────────────────────────────

class TestHTLFInheritance:
    def test_is_subclass_of_box_turtle(self):
        assert issubclass(AFC_HTLF, afcBoxTurtle)


# ── Class constants ───────────────────────────────────────────────────────────

class TestHTLFConstants:
    def test_valid_cam_angles(self):
        assert AFC_HTLF.VALID_CAM_ANGLES == [30, 45, 60]


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
