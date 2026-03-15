"""
Unit tests for extras/AFC_spool.py

Covers:
  - AFCSpool: initialization
  - cmd_SET_MAP: validates lane mapping
  - cmd_SET_COLOR / cmd_SET_MATERIAL / cmd_SET_WEIGHT: attribute updates
  - cmd_SET_SPOOL_ID: spoolman interaction
  - cmd_SET_RUNOUT: runout lane assignment
  - cmd_RESET_AFC_MAPPING: clears mappings
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from extras.AFC_spool import AFCSpool


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_spool():
    """Build an AFCSpool instance bypassing __init__."""
    spool = AFCSpool.__new__(AFCSpool)

    from tests.conftest import MockAFC, MockPrinter, MockLogger, MockGcode

    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    afc.logger = MockLogger()
    afc.gcode = MockGcode()
    afc.reactor = MagicMock()
    afc.spoolman = None
    afc.tool_cmds = {}
    afc.lanes = {}

    spool.printer = printer
    spool.afc = afc
    spool.error = afc.error
    spool.reactor = afc.reactor
    spool.gcode = afc.gcode
    spool.logger = afc.logger
    spool.disable_weight_check = False
    spool.next_spool_id = None
    spool.SPOOLMAN_REMOTE_METHOD = 'spoolman_set_active_spool'

    return spool


def _make_lane(name="lane1"):
    lane = MagicMock()
    lane.name = name
    lane.map = None
    lane.color = ""
    lane.material = ""
    lane.weight = 0
    lane.spool_id = None
    lane.runout_lane = None
    lane.unit_obj = MagicMock()
    lane.hub_obj = MagicMock()
    lane.extruder_obj = MagicMock()
    return lane


def _make_gcmd(**kwargs):
    """Build a gcmd mock that returns values from kwargs."""
    gcmd = MagicMock()
    gcmd.get = lambda key, default=None: kwargs.get(key, default)
    def _get_float(key, default=0.0, **kw):
        val = kwargs.get(key, default)
        return float(val) if val is not None else None
    gcmd.get_float = _get_float
    gcmd.get_int = lambda key, default=0, **kw: int(kwargs.get(key, default))
    return gcmd


# ── cmd_SET_MAP ────────────────────────────────────────────────────────────────

class TestSetMap:
    def test_set_map_assigns_lane_mapping(self):
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane2 = _make_lane("lane2")
        lane2.map = "T0"
        # T0 is currently mapped to lane2; we want to remap it to lane1
        spool.afc.tool_cmds = {"T0": "lane2"}
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        gcmd = _make_gcmd(LANE="lane1", MAP="T0")
        spool.cmd_SET_MAP(gcmd)
        assert lane1.map == "T0"
        assert spool.afc.tool_cmds.get("T0") == "lane1"

    def test_set_map_invalid_lane_logs_error(self):
        spool = _make_spool()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="nonexistent", MAP="T1")
        spool.cmd_SET_MAP(gcmd)
        # Should log an error about invalid lane
        error_msgs = [m for lvl, m in spool.logger.messages if lvl == "error"]
        assert len(error_msgs) > 0

    def test_no_lane_param_logs_info(self):
        """Covers lines 66-68: lane is None → log info + return."""
        spool = _make_spool()
        gcmd = _make_gcmd()  # no LANE key → returns None
        spool.cmd_SET_MAP(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert any("LANE" in m for m in info_msgs)

    def test_no_map_param_logs_info(self):
        """Covers lines 72-74: map_cmd is None → log info + return."""
        spool = _make_spool()
        gcmd = _make_gcmd(LANE="lane1")  # no MAP key → returns None
        spool.cmd_SET_MAP(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert any("MAP" in m for m in info_msgs)

    def test_lane_not_in_lanes_with_valid_tool_cmd_logs_info(self):
        """Covers lines 84-86: MAP in tool_cmds, lane not in lanes → log info + return."""
        spool = _make_spool()
        spool.afc.tool_cmds = {"T0": "lane2"}
        spool.afc.lanes = {}  # lane1 not present
        gcmd = _make_gcmd(LANE="lane1", MAP="T0")
        spool.cmd_SET_MAP(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert any("lane1" in m for m in info_msgs)


# ── cmd_SET_COLOR ──────────────────────────────────────────────────────────────

class TestSetColor:
    def test_set_color_updates_lane_color(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1", COLOR="#FF0000")
        spool.cmd_SET_COLOR(gcmd)
        assert lane.color == "#FF0000"

    def test_set_color_saves_vars(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        spool.afc.save_vars = MagicMock()
        gcmd = _make_gcmd(LANE="lane1", COLOR="red")
        spool.cmd_SET_COLOR(gcmd)
        spool.afc.save_vars.assert_called()

    def test_no_lane_param_logs_info(self):
        """Covers lines 117-119: lane is None → log info + return."""
        spool = _make_spool()
        gcmd = _make_gcmd()  # no LANE key
        spool.cmd_SET_COLOR(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert any("LANE" in m for m in info_msgs)

    def test_lane_not_in_lanes_logs_info(self):
        """Covers lines 121-123: lane not in lanes dict → log info + return."""
        spool = _make_spool()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="ghost", COLOR="FF0000")
        spool.cmd_SET_COLOR(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert any("ghost" in m for m in info_msgs)


# ── cmd_SET_MATERIAL ───────────────────────────────────────────────────────────

class TestSetMaterial:
    def test_set_material_updates_lane_material(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1", MATERIAL="PLA")
        spool.cmd_SET_MATERIAL(gcmd)
        assert lane.material == "PLA"

    def test_set_material_saves_vars(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        spool.afc.save_vars = MagicMock()
        gcmd = _make_gcmd(LANE="lane1", MATERIAL="ABS")
        spool.cmd_SET_MATERIAL(gcmd)
        spool.afc.save_vars.assert_called()

    def test_no_lane_param_logs_info(self):
        """Covers lines 185-187: lane is None → log info + return."""
        spool = _make_spool()
        gcmd = _make_gcmd()  # no LANE key
        spool.cmd_SET_MATERIAL(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert any("LANE" in m for m in info_msgs)

    def test_lane_not_in_lanes_logs_info(self):
        """Covers lines 188-190: lane not in lanes → log info + return."""
        spool = _make_spool()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="ghost", MATERIAL="PLA")
        spool.cmd_SET_MATERIAL(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert any("ghost" in m for m in info_msgs)

    def test_with_density_sets_filament_density(self):
        """Covers lines 200-201: density is not None → sets cur_lane.filament_density."""
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1", MATERIAL="PLA", DENSITY=1.24)
        spool.cmd_SET_MATERIAL(gcmd)
        assert lane.filament_density == 1.24


# ── cmd_SET_WEIGHT ─────────────────────────────────────────────────────────────

class TestSetWeight:
    def test_set_weight_updates_lane_weight(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1", WEIGHT=250)
        spool.cmd_SET_WEIGHT(gcmd)
        assert lane.weight == 250

    def test_set_weight_invalid_lane_logs_info(self):
        spool = _make_spool()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="missing", WEIGHT=100)
        spool.cmd_SET_WEIGHT(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert len(info_msgs) > 0

    def test_no_lane_param_logs_info(self):
        """Covers lines 146-148: lane is None → log info + return."""
        spool = _make_spool()
        gcmd = _make_gcmd()  # no LANE key
        spool.cmd_SET_WEIGHT(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert any("LANE" in m for m in info_msgs)


# ── cmd_SET_RUNOUT ─────────────────────────────────────────────────────────────

class TestSetRunout:
    def test_set_runout_assigns_runout_lane(self):
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane2 = _make_lane("lane2")
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        gcmd = _make_gcmd(LANE="lane1", RUNOUT="lane2")
        spool.cmd_SET_RUNOUT(gcmd)
        assert lane1.runout_lane == "lane2"

    def test_set_runout_saves_vars(self):
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane2 = _make_lane("lane2")
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        spool.afc.save_vars = MagicMock()
        gcmd = _make_gcmd(LANE="lane1", RUNOUT="lane2")
        spool.cmd_SET_RUNOUT(gcmd)
        spool.afc.save_vars.assert_called()

    def test_no_lane_param_logs_info(self):
        """Covers lines 373-375: lane is None → log info + return."""
        spool = _make_spool()
        gcmd = _make_gcmd()  # no LANE key
        spool.cmd_SET_RUNOUT(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert any("LANE" in m for m in info_msgs)

    def test_lane_equals_runout_logs_error(self):
        """Covers lines 379-381: lane == runout → log error + return."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane1}
        gcmd = _make_gcmd(LANE="lane1", RUNOUT="lane1")
        spool.cmd_SET_RUNOUT(gcmd)
        error_msgs = [m for lvl, m in spool.logger.messages if lvl == "error"]
        assert any("lane1" in m for m in error_msgs)

    def test_lane_not_in_lanes_logs_error(self):
        """Covers lines 383-385: lane not in lanes → log error + return."""
        spool = _make_spool()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="ghost", RUNOUT="lane2")
        spool.cmd_SET_RUNOUT(gcmd)
        error_msgs = [m for lvl, m in spool.logger.messages if lvl == "error"]
        assert any("ghost" in m for m in error_msgs)

    def test_runout_not_in_lanes_logs_error(self):
        """Covers lines 387-389: runout != 'NONE' but not in lanes → log error + return."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane1}
        gcmd = _make_gcmd(LANE="lane1", RUNOUT="no_such_lane")
        spool.cmd_SET_RUNOUT(gcmd)
        error_msgs = [m for lvl, m in spool.logger.messages if lvl == "error"]
        assert any("no_such_lane" in m for m in error_msgs)


# ── cmd_RESET_AFC_MAPPING ─────────────────────────────────────────────────────

class TestResetAFCMapping:
    def _make_reset_gcmd(self, runout="yes"):
        gcmd = MagicMock()
        gcmd.get = lambda key, default=None: runout if key == "RUNOUT" else default
        return gcmd

    def _make_lane_for_reset(self, name, map_cmd="T0"):
        """Lane mock with explicit _map=None and map=map_cmd."""
        lane = _make_lane(name)
        lane.map = map_cmd
        lane._map = None  # not manually assigned
        lane.runout_lane = None
        return lane

    def test_reset_saves_vars(self):
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.units = {}  # no units, loop skips mapping reassignment
        gcmd = self._make_reset_gcmd()
        spool.cmd_RESET_AFC_MAPPING(gcmd)
        spool.afc.save_vars.assert_called()

    def test_reset_clears_runout_lanes(self):
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        lane1.runout_lane = "lane2"
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.units = {}
        gcmd = self._make_reset_gcmd(runout="yes")
        spool.cmd_RESET_AFC_MAPPING(gcmd)
        assert lane1.runout_lane is None

    def test_reset_skips_runout_when_no(self):
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        lane1.runout_lane = "lane2"
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.units = {}
        gcmd = self._make_reset_gcmd(runout="no")
        spool.cmd_RESET_AFC_MAPPING(gcmd)
        assert lane1.runout_lane == "lane2"  # not cleared

    def test_reset_reassigns_map_without_manual_mapping(self):
        """Covers lines 422-431 else branch: lane._map=None → pops from existing_cmds."""
        spool = _make_spool()
        # Set up a lane in afc.lanes used to build existing_cmds
        lane1 = self._make_lane_for_reset("lane1", "T0")
        spool.afc.lanes = {"lane1": lane1}
        # Unit whose lane also has _map=None (else branch)
        unit_lane = MagicMock()
        unit_lane.name = "lane1"
        unit_lane._map = None
        unit = MagicMock()
        unit.lanes = {"lane1": unit_lane}
        spool.afc.units = {"unit_1": unit}
        gcmd = self._make_reset_gcmd()
        spool.cmd_RESET_AFC_MAPPING(gcmd)
        # tool_cmds and lane.map must have been updated
        assert spool.afc.tool_cmds.get("T0") == "lane1"
        assert spool.afc.lanes["lane1"].map == "T0"

    def test_reset_uses_manual_map_when_set(self):
        """Covers lines 425-427 if branch: lane._map is not None → uses it directly."""
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        lane1._map = "T0"  # manually assigned
        spool.afc.lanes = {"lane1": lane1}
        unit_lane = MagicMock()
        unit_lane.name = "lane1"
        unit_lane._map = "T0"  # manual assignment
        unit = MagicMock()
        unit.lanes = {"lane1": unit_lane}
        spool.afc.units = {"unit_1": unit}
        gcmd = self._make_reset_gcmd()
        spool.cmd_RESET_AFC_MAPPING(gcmd)
        # The manually assigned T0 should be applied
        assert spool.afc.tool_cmds.get("T0") == "lane1"


# ── cmd_SET_NEXT_SPOOL_ID ─────────────────────────────────────────────────────

class TestSetNextSpoolId:
    def test_stores_next_spool_id(self):
        spool = _make_spool()
        gcmd = _make_gcmd(SPOOL_ID=42)
        spool.cmd_SET_NEXT_SPOOL_ID(gcmd)
        assert spool.next_spool_id == 42

    def test_invalid_spool_id_logs_error_and_clears(self):
        """Covers lines 466-468: ValueError → log error, next_spool_id stays None."""
        spool = _make_spool()
        gcmd = _make_gcmd(SPOOL_ID="not_a_number")
        spool.cmd_SET_NEXT_SPOOL_ID(gcmd)
        error_msgs = [m for lvl, m in spool.logger.messages if lvl == "error"]
        assert len(error_msgs) > 0
        assert spool.next_spool_id is None

    def test_empty_spool_id_clears_next(self):
        """Covers lines 469-470: SpoolID='' → next_spool_id set to None."""
        spool = _make_spool()
        spool.next_spool_id = 99
        gcmd = _make_gcmd()  # no SPOOL_ID key → default ''
        spool.cmd_SET_NEXT_SPOOL_ID(gcmd)
        assert spool.next_spool_id is None

    def test_overwrites_existing_spool_id_logs_overwrite(self):
        """Covers lines 471-472: previous_id is set → log overwrite info message."""
        spool = _make_spool()
        spool.next_spool_id = 10  # existing
        gcmd = _make_gcmd(SPOOL_ID=20)
        spool.cmd_SET_NEXT_SPOOL_ID(gcmd)
        assert spool.next_spool_id == 20
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert any("10" in m for m in info_msgs)


# ── handle_connect / register_lane_macros ─────────────────────────────────────

class TestHandleConnect:
    def test_handle_connect_stores_afc_references(self):
        from tests.conftest import MockAFC, MockPrinter
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        spool = AFCSpool.__new__(AFCSpool)
        spool.printer = printer
        spool.next_spool_id = None
        spool.handle_connect()
        assert spool.afc is afc
        assert spool.gcode is afc.gcode
        assert spool.logger is afc.logger

    def test_handle_connect_registers_commands(self):
        from tests.conftest import MockAFC, MockPrinter
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        spool = AFCSpool.__new__(AFCSpool)
        spool.printer = printer
        spool.next_spool_id = None
        spool.handle_connect()
        assert "RESET_AFC_MAPPING" in afc.gcode._commands
        assert "SET_NEXT_SPOOL_ID" in afc.gcode._commands


class TestRegisterLaneMacros:
    def test_registers_six_mux_commands(self):
        spool = _make_spool()
        spool.gcode.register_mux_command = MagicMock()
        lane = _make_lane("lane1")
        spool.register_lane_macros(lane)
        assert spool.gcode.register_mux_command.call_count == 6

    def test_all_commands_use_correct_lane_name(self):
        spool = _make_spool()
        calls_received = []
        spool.gcode.register_mux_command = MagicMock(
            side_effect=lambda cmd, key, val, *a, **kw: calls_received.append((cmd, val))
        )
        lane = _make_lane("lane_x")
        spool.register_lane_macros(lane)
        for _cmd, val in calls_received:
            assert val == "lane_x"


# ── set_active_spool ──────────────────────────────────────────────────────────

class TestSetActiveSpool:
    def test_no_op_when_spoolman_is_none(self):
        spool = _make_spool()
        spool.afc.spoolman = None
        webhooks = MagicMock()
        spool.printer._objects["webhooks"] = webhooks
        spool.set_active_spool(42)
        webhooks.call_remote_method.assert_not_called()

    def test_calls_webhook_with_spool_id(self):
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        webhooks = MagicMock()
        spool.printer.lookup_object = MagicMock(return_value=webhooks)
        spool.set_active_spool(7)
        webhooks.call_remote_method.assert_called_once_with(
            spool.SPOOLMAN_REMOTE_METHOD, spool_id=7
        )

    def test_calls_webhook_with_none_when_id_is_none(self):
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        webhooks = MagicMock()
        spool.printer.lookup_object = MagicMock(return_value=webhooks)
        spool.set_active_spool(None)
        webhooks.call_remote_method.assert_called_once_with(
            spool.SPOOLMAN_REMOTE_METHOD, spool_id=None
        )


# ── _get_filament_values ──────────────────────────────────────────────────────

class TestGetFilamentValues:
    def test_returns_value_when_field_present(self):
        spool = _make_spool()
        filament = {"material": "PLA", "density": 1.24}
        assert spool._get_filament_values(filament, "material") == "PLA"

    def test_returns_default_when_field_missing(self):
        spool = _make_spool()
        assert spool._get_filament_values({}, "density", default=1.0) == 1.0

    def test_returns_none_as_default_when_not_specified(self):
        spool = _make_spool()
        assert spool._get_filament_values({}, "missing_field") is None


# ── clear_values ──────────────────────────────────────────────────────────────

class TestClearValues:
    def test_clears_all_spool_fields(self):
        spool = _make_spool()
        lane = _make_lane()
        lane.spool_id = 42
        lane.material = "PLA"
        lane.color = "#FF0000"
        lane.weight = 500
        lane.extruder_temp = 210
        lane.bed_temp = 60
        lane.clear_lane_data = MagicMock()
        spool.clear_values(lane)
        assert lane.spool_id is None
        assert lane.material == ""
        assert lane.color == ""
        assert lane.weight == 0
        assert lane.extruder_temp is None
        assert lane.bed_temp is None
        lane.clear_lane_data.assert_called_once()


# ── _set_values ───────────────────────────────────────────────────────────────

class TestSetValues:
    def test_sets_defaults_when_not_remember_spool(self):
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        spool.afc.default_material_type = "PLA"
        spool.set_spoolID = MagicMock()
        spool._set_values(lane)
        assert lane.material == "PLA"
        assert lane.weight == 1000
        spool.set_spoolID.assert_not_called()

    def test_calls_set_spool_id_when_next_spool_id_set(self):
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        spool.afc.default_material_type = "PLA"
        spool.afc.spoolman = MagicMock()
        spool.next_spool_id = 42
        spool.set_spoolID = MagicMock()
        spool._set_values(lane)
        spool.set_spoolID.assert_called_once_with(lane, 42)
        assert spool.next_spool_id is None  # consumed after use


# ── set_spoolID ───────────────────────────────────────────────────────────────

def _make_spool_result(material="PLA", color_hex="FF0000", weight=800.0):
    return {
        "filament": {
            "material": material,
            "settings_extruder_temp": 210,
            "settings_bed_temp": 60,
            "density": 1.24,
            "diameter": 1.75,
            "color_hex": color_hex,
        },
        "spool_weight": 190,
        "remaining_weight": weight,
        "initial_weight": 1000.0,
    }


class TestSetSpoolID:
    def _make_spool_with_spoolman(self):
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        return spool

    def test_no_spoolman_not_remember_spool_clears_values(self):
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, "")
        spool.clear_values.assert_called_once_with(lane)

    def test_valid_spool_id_sets_lane_attributes(self):
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        spool.afc.moonraker.get_spool = MagicMock(return_value=result)
        spool.set_spoolID(lane, 42)
        assert lane.spool_id == 42
        assert lane.material == "PLA"
        assert lane.color == "#FF0000"

    def test_zero_weight_triggers_error_when_check_enabled(self):
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result(weight=0.0)
        spool.afc.moonraker.get_spool = MagicMock(return_value=result)
        spool.disable_weight_check = False
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, 42)
        spool.afc.error.AFC_error.assert_called()
        spool.clear_values.assert_called()

    def test_disable_weight_check_skips_validation(self):
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result(weight=0.0)
        spool.afc.moonraker.get_spool = MagicMock(return_value=result)
        spool.disable_weight_check = True
        spool.set_spoolID(lane, 42)
        spool.afc.error.AFC_error.assert_not_called()

    def test_multi_color_takes_first_color(self):
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        result["filament"]["multi_color_hexes"] = "AABBCC,001122,334455"
        spool.afc.moonraker.get_spool = MagicMock(return_value=result)
        spool.set_spoolID(lane, 42)
        assert lane.color == "#AABBCC"

    def test_ignore_spoolman_material_temps_skips_extruder_temp(self):
        spool = self._make_spool_with_spoolman()
        spool.afc.ignore_spoolman_material_temps = True
        lane = _make_lane()
        lane.remember_spool = False
        lane.extruder_temp = None  # explicitly set before call
        lane.espooler = MagicMock()
        result = _make_spool_result()
        spool.afc.moonraker.get_spool = MagicMock(return_value=result)
        spool.set_spoolID(lane, 42)
        # extruder_temp should NOT be overwritten when the flag is True
        assert lane.extruder_temp is None

    def test_exception_in_get_spool_calls_afc_error(self):
        """Covers lines 346-348: exception in get_spool → AFC_error called."""
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        spool.afc.moonraker.get_spool = MagicMock(
            side_effect=Exception("Connection refused")
        )
        spool.set_spoolID(lane, 42)
        spool.afc.error.AFC_error.assert_called()

    def test_empty_spool_id_with_spoolman_not_remember_spool_clears_values(self):
        """Covers lines 348-349: spoolman not None + SpoolID='' + not remember_spool → clear_values."""
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, "")
        spool.clear_values.assert_called_once_with(lane)


# ── cmd_SET_SPOOL_ID ──────────────────────────────────────────────────────────

class TestCmdSetSpoolID:
    def test_no_op_when_spoolman_is_none(self):
        spool = _make_spool()
        spool.afc.spoolman = None
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1", SPOOL_ID="42")
        spool.set_spoolID = MagicMock()
        spool.cmd_SET_SPOOL_ID(gcmd)
        spool.set_spoolID.assert_not_called()

    def test_invalid_spool_id_string_logs_error(self):
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1", SPOOL_ID="not_a_number")
        spool.set_spoolID = MagicMock()
        spool.cmd_SET_SPOOL_ID(gcmd)
        errors = [m for lvl, m in spool.logger.messages if lvl == "error"]
        assert len(errors) > 0
        spool.set_spoolID.assert_not_called()

    def test_spool_already_assigned_to_other_lane_logs_error(self):
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        lane1 = _make_lane("lane1")
        lane1.spool_id = None
        lane2 = _make_lane("lane2")
        lane2.spool_id = 42
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        gcmd = _make_gcmd(LANE="lane1", SPOOL_ID="42")
        spool.set_spoolID = MagicMock()
        spool.cmd_SET_SPOOL_ID(gcmd)
        errors = [m for lvl, m in spool.logger.messages if lvl == "error"]
        assert len(errors) > 0
        spool.set_spoolID.assert_not_called()

    def test_valid_assignment_calls_set_spool_id(self):
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        lane1 = _make_lane("lane1")
        lane1.spool_id = None
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.current = None
        gcmd = _make_gcmd(LANE="lane1", SPOOL_ID="5")
        spool.set_spoolID = MagicMock()
        spool.cmd_SET_SPOOL_ID(gcmd)
        spool.set_spoolID.assert_called_once_with(lane1, 5)

    def test_no_lane_param_logs_info(self):
        """Covers lines 239-241: spoolman not None + lane=None → log info + return."""
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        gcmd = _make_gcmd()  # no LANE key
        spool.cmd_SET_SPOOL_ID(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert any("LANE" in m for m in info_msgs)

    def test_lane_not_in_lanes_logs_info(self):
        """Covers lines 243-245: spoolman not None + lane not in lanes → log info + return."""
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="ghost", SPOOL_ID="7")
        spool.cmd_SET_SPOOL_ID(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        assert any("ghost" in m for m in info_msgs)

    def test_calls_set_active_spool_when_lane_is_current(self):
        """Covers lines 264-265: cur_lane.name == afc.current → set_active_spool called."""
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        lane1 = _make_lane("lane1")
        lane1.spool_id = None
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.current = "lane1"  # lane1 is the active lane
        gcmd = _make_gcmd(LANE="lane1", SPOOL_ID="9")
        spool.set_spoolID = MagicMock()
        spool.set_active_spool = MagicMock()
        spool.cmd_SET_SPOOL_ID(gcmd)
        spool.set_active_spool.assert_called_once_with(lane1.spool_id)
