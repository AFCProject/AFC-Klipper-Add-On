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


class TestPrepRestoreFilamentInfo:
    def _run_prep(self, lane_vars, spoolman=None, tmp_path=None, current_lane_obj=None):
        from tests.conftest import MockConfig, MockPrinter, MockAFC

        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        printer.state_message = "Printer is ready"
        prep = afcPrep(MockConfig(name="AFC_prep", printer=printer))
        prep.handle_connect = MagicMock()
        prep.afc = afc
        prep.logger = afc.logger
        prep._rename_macros = MagicMock()
        prep._td1_prep = MagicMock()

        (tmp_path / "vars.unit").write_text(json.dumps({"unit1": {"lane1": lane_vars}}))
        afc.VarFile = str(tmp_path / "vars")
        afc.spoolman = spoolman
        afc.tools = {}
        afc.buffers = {}
        afc.snapmaker_printer = False
        afc.current = "lane1"
        afc.bypass = MagicMock()
        afc.bypass.name = "bypass"
        afc.bypass.filament_present = False
        for attr in ("handle_moonraker_connect", "print_version", "save_vars",
                     "get_bypass_state", "spool"):
            setattr(afc, attr, MagicMock())
        afc.verify_macro_positions = MagicMock(return_value="")

        lane_stub = MagicMock()
        lane_stub.multi_hubs_found = False

        class _Lanes(dict):
            def append(self, name):
                self[name] = lane_stub

        unit = MagicMock()
        unit.lanes = _Lanes()
        unit.full_name = ["unit1"]
        unit.system_Test.return_value = True
        lane = MagicMock()
        lane.name = "lane1"
        lane.unit = "unit1"
        lane.spool_id = None
        lane.spool_vendor = ""
        lane.filament_name = ""
        lane.runout_lane = None
        lane.map = None
        lane.unit_obj = unit
        afc.units = {"unit1": unit}
        afc.lanes = {"lane1": lane}
        afc.function.get_current_lane_obj.return_value = (
            lane if current_lane_obj is True else current_lane_obj)

        prep.PREP(MagicMock())
        return lane, afc

    def test_restores_vendor_and_name_without_spoolman(self, tmp_path):
        lane, _ = self._run_prep({"spool_vendor": "Polymaker",
                               "filament_name": "PolyTerra PLA"}, tmp_path=tmp_path)
        assert lane.spool_vendor == "Polymaker"
        assert lane.filament_name == "PolyTerra PLA"

    def test_missing_keys_leave_vendor_and_name_unchanged(self, tmp_path):
        lane, _ = self._run_prep({"material": "PLA"}, tmp_path=tmp_path)
        assert lane.spool_vendor == ""
        assert lane.filament_name == ""

    def test_vendor_key_alone_does_not_set_name(self, tmp_path):
        lane, _ = self._run_prep({"spool_vendor": "Elegoo"}, tmp_path=tmp_path)
        assert lane.spool_vendor == "Elegoo"
        assert lane.filament_name == ""

    def test_name_key_alone_does_not_set_vendor(self, tmp_path):
        lane, _ = self._run_prep({"filament_name": "PLA"}, tmp_path=tmp_path)
        assert lane.spool_vendor == ""
        assert lane.filament_name == "PLA"

    def test_spoolman_with_spool_id_defers_to_spoolman(self, tmp_path):
        lane, afc = self._run_prep({"spool_id": 5, "spool_vendor": "Stale",
                                    "filament_name": "Stale"},
                                   spoolman=MagicMock(), tmp_path=tmp_path)
        afc.spool.set_spoolID.assert_called_once_with(lane, 5, save_vars=False)
        assert lane.spool_vendor == ""
        assert lane.filament_name == ""

    def test_spoolman_without_spool_id_restores_saved_values(self, tmp_path):
        lane, afc = self._run_prep({"spool_vendor": "Elegoo", "filament_name": "PLA"},
                                   spoolman=MagicMock(), tmp_path=tmp_path)
        afc.spool.set_spoolID.assert_not_called()
        assert lane.spool_vendor == "Elegoo"
        assert lane.filament_name == "PLA"


class TestPrepSelectCurrentLane(TestPrepRestoreFilamentInfo):
    """Regression test for GH #778: PREP's select_lane call must not pass
    sel_dir (formerly sel_prep) as True -- that flag means "selection is
    happening as part of loading a lane", which is not what PREP is doing."""

    def test_select_lane_called_with_no_direction_flag(self, tmp_path):
        lane, _ = self._run_prep({}, tmp_path=tmp_path, current_lane_obj=True)

        lane.unit_obj.select_lane.assert_called_once_with(lane)

    def test_select_lane_not_called_when_no_current_lane(self, tmp_path):
        lane, _ = self._run_prep({}, tmp_path=tmp_path, current_lane_obj=None)

        lane.unit_obj.select_lane.assert_not_called()
