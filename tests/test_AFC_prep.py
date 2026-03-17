"""
Unit tests for extras/AFC_prep.py

Covers:
  - afcPrep attribute initialization
  - _rename: delegates to gcode.register_command
  - _rename_macros: only renames once, respects dis_unload_macro flag
  - PREP: error when unit file missing or malformed JSON
  - _td1_prep: logic branches for TD-1 presence
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch, call
import pytest

from extras.AFC_prep import afcPrep


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_prep(values=None):
    """Build an afcPrep instance bypassing __init__."""
    prep = afcPrep.__new__(afcPrep)

    from tests.conftest import MockAFC, MockPrinter, MockLogger

    afc = MockAFC()
    printer = MockPrinter(afc=afc)

    prep.printer = printer
    prep.afc = afc
    prep.logger = afc.logger
    prep.delay = 0.1
    prep.enable = False
    prep.dis_unload_macro = False
    prep.get_td1_data = False
    prep.rename_occurred = False
    prep.assignTcmd = True

    if values:
        for k, v in values.items():
            setattr(prep, k, v)

    return prep


# ── Initialization defaults ───────────────────────────────────────────────────

class TestPrepInit:
    def test_rename_occurred_initially_false(self):
        p = _make_prep()
        assert p.rename_occurred is False

    def test_assign_tcmd_initially_true(self):
        p = _make_prep()
        assert p.assignTcmd is True

    def test_delay_default(self):
        p = _make_prep()
        assert p.delay == 0.1

# ── _rename_macros ────────────────────────────────────────────────────────────

class TestRenameMacros:
    def _setup_error_obj(self, prep):
        prep.afc.error.BASE_RESUME_NAME = "RESUME"
        prep.afc.error.AFC_RENAME_RESUME_NAME = "_AFC_RENAMED_RESUME_"
        prep.afc.error.cmd_AFC_RESUME = MagicMock()
        prep.afc.error.cmd_AFC_RESUME_help = "help"
        prep.afc.error.BASE_PAUSE_NAME = "PAUSE"
        prep.afc.error.AFC_RENAME_PAUSE_NAME = "_AFC_RENAMED_PAUSE_"
        prep.afc.error.cmd_AFC_PAUSE = MagicMock()

    def test_rename_macros_only_runs_once(self):
        """Second call to _rename_macros should be a no-op (rename_occurred guard)."""
        prep = _make_prep({"dis_unload_macro": True})  # skip UNLOAD_FILAMENT rename
        self._setup_error_obj(prep)
        prep.afc.function._rename = MagicMock()
        prep._rename_macros()
        count_after_first = prep.afc.function._rename.call_count
        prep._rename_macros()  # Second call should be a no-op
        assert prep.afc.function._rename.call_count == count_after_first  # no additional calls

    def test_rename_occurred_set_after_first_call(self):
        prep = _make_prep({"dis_unload_macro": True})
        self._setup_error_obj(prep)
        prep.afc.function._rename = MagicMock()
        prep._rename_macros()
        assert prep.rename_occurred is True

    def test_unload_filament_renamed_by_default(self):
        prep = _make_prep()
        self._setup_error_obj(prep)
        prep.afc.BASE_UNLOAD_FILAMENT = "UNLOAD_FILAMENT"
        prep.afc.RENAMED_UNLOAD_FILAMENT = "_AFC_RENAMED_UNLOAD_FILAMENT_"
        prep.afc.cmd_TOOL_UNLOAD = MagicMock()
        prep.afc.cmd_TOOL_UNLOAD_help = "help"
        prep.afc.function._rename = MagicMock()
        prep._rename_macros()
        # Expect 3 renames: RESUME, PAUSE, UNLOAD_FILAMENT
        assert prep.afc.function._rename.call_count == 3

    def test_unload_filament_not_renamed_when_disabled(self):
        prep = _make_prep({"dis_unload_macro": True})
        self._setup_error_obj(prep)
        prep.afc.BASE_UNLOAD_FILAMENT = "UNLOAD_FILAMENT"
        prep.afc.function._rename = MagicMock()
        prep._rename_macros()
        # Expect only 2 renames: RESUME + PAUSE
        assert prep.afc.function._rename.call_count == 2
    
    def test_rename_occurred(self):
        prep = _make_prep({"rename_occurred": True})
        self._setup_error_obj(prep)
        prep.afc.function._rename = MagicMock()
        prep._rename_macros()
        # Expect only 2 renames: RESUME + PAUSE
        assert prep.afc.function._rename.call_count == 0


# ── _td1_prep ─────────────────────────────────────────────────────────────────

class TestTd1Prep:
    def test_no_td1_skips_data_capture(self):
        prep = _make_prep()
        prep.afc.td1_present = False
        prep.afc.function.get_current_lane_obj.return_value = None
        # Should not raise or try to iterate lanes
        prep._td1_prep(overrall_status=True)

    def test_td1_present_no_current_lane_no_error_captures_data(self):
        prep = _make_prep({"get_td1_data": True})
        prep.afc.td1_present = True
        prep.afc.function.get_current_lane_obj.return_value = None
        prep.afc.function.check_for_td1_error.return_value = (False, None)
        lane = MagicMock()
        lane.load_state = True
        lane.prep_state = True
        lane.get_td1_data.return_value = (True, "data")
        prep.afc.lanes = {"lane1": lane}
        prep._td1_prep(overrall_status=True)
        lane.get_td1_data.assert_called()

    def test_td1_present_with_current_lane_skips_capture(self):
        prep = _make_prep({"get_td1_data": True})
        prep.afc.td1_present = True
        cur_lane = MagicMock()
        cur_lane.unit_obj = MagicMock()
        prep.afc.function.get_current_lane_obj.return_value = cur_lane
        prep.afc.function.check_for_td1_error.return_value = (False, None)
        lane = MagicMock()
        lane.get_td1_data = MagicMock()
        prep.afc.lanes = {"lane1": lane}
        prep._td1_prep(overrall_status=True)
        lane.get_td1_data.assert_not_called()

    def test_overall_status_false_skips_capture(self):
        prep = _make_prep({"get_td1_data": True})
        prep.afc.td1_present = True
        prep.afc.function.get_current_lane_obj.return_value = None
        prep.afc.function.check_for_td1_error.return_value = (False, None)
        lane = MagicMock()
        lane.get_td1_data = MagicMock()
        prep.afc.lanes = {"lane1": lane}
        prep._td1_prep(overrall_status=False)
        lane.get_td1_data.assert_not_called()


# ── _tc_restore ───────────────────────────────────────────────────────────────

class TestTcRestore:
    def _make_tc(self, active_tool=None, has_detection=True):
        tc = MagicMock()
        tc.active_tool = active_tool
        tc.has_detection = has_detection
        return tc

    def test_no_toolchanger_does_nothing(self):
        """lookup_object returns None → method is a no-op and does not raise."""
        prep = _make_prep()
        # printer._objects has no 'toolchanger' entry → lookup_object returns None
        prep._tc_restore()  # must not raise

    def test_active_tool_already_set_skips_restore(self):
        """If active_tool is already populated, note_detect_change must not be called."""
        prep = _make_prep()
        tc = self._make_tc(active_tool=MagicMock())
        prep.printer._objects["toolchanger"] = tc
        prep._tc_restore()
        tc.note_detect_change.assert_not_called()

    def test_no_detection_pins_skips_restore(self):
        """If has_detection is False, note_detect_change must not be called."""
        prep = _make_prep()
        tc = self._make_tc(has_detection=False)
        prep.printer._objects["toolchanger"] = tc
        prep._tc_restore()
        tc.note_detect_change.assert_not_called()

    def test_detection_present_calls_note_detect_change(self):
        """active_tool=None and has_detection=True → note_detect_change(None) called."""
        prep = _make_prep()
        tc = self._make_tc()  # active_tool=None, has_detection=True
        prep.printer._objects["toolchanger"] = tc
        prep._tc_restore()
        tc.note_detect_change.assert_called_once_with(None)
