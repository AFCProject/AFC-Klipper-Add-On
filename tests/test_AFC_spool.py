"""
Unit tests for extras/AFC_spool.py

Covers:
  - AFCSpool: initialization
  - cmd_SET_MAP: validates lane mapping
  - cmd_SET_COLOR / cmd_SET_MATERIAL / cmd_SET_WEIGHT: attribute updates
  - cmd_SET_SPOOL_ID: spoolman interaction
  - cmd_SET_RUNOUT: runout lane assignment
  - cmd_AFC_RESET_MAPPING: clears mappings
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from extras.AFC_spool import AFCSpool, MAX_UNATTENDED_MAPPING_CHANGES, load_config


# ── Module-level import guard ────────────────────────────────────────────────

class TestNaturalSortKeyImportGuard:
    def test_raises_configfile_error_when_import_fails(self):
        """Covers the module-level try/except around importing natural_sort_key
        from extras.AFC_utils: if that import fails, the module must re-raise
        as configfile.error rather than a raw ImportError.

        Loads a throwaway copy of the module under a separate name instead of
        reloading extras.AFC_spool in place: reload() mutates the canonical
        module's namespace, which would leave every `from extras.AFC_spool
        import AFCSpool` reference already captured elsewhere in this test
        suite pointing at a stale, pre-reload class.
        """
        import sys
        import importlib.util
        from configfile import error as ConfigFileError

        afc_utils = sys.modules["extras.AFC_utils"]
        real_natural_sort_key = afc_utils.natural_sort_key
        del afc_utils.natural_sort_key
        try:
            spec = importlib.util.spec_from_file_location(
                "extras.AFC_spool_import_guard_test",
                sys.modules["extras.AFC_spool"].__file__,
            )
            fresh_module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = fresh_module
            try:
                with pytest.raises(ConfigFileError):
                    spec.loader.exec_module(fresh_module)
            finally:
                del sys.modules[spec.name]
        finally:
            afc_utils.natural_sort_key = real_natural_sort_key


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_spool():
    """Build an AFCSpool instance through the real __init__."""
    from tests.conftest import MockAFC, MockConfig, MockPrinter, MockLogger, MockGcode

    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    afc.logger = MockLogger()
    afc.gcode = MockGcode()
    afc.reactor = MagicMock()
    afc.spoolman = None
    afc.tool_cmds = {}
    afc.lanes = {}
    afc.units = {}

    config = MockConfig(name="AFC_Spool", printer=printer)
    spool = AFCSpool(config)

    spool.afc = afc
    spool.error = afc.error
    spool.reactor = afc.reactor
    spool.gcode = afc.gcode
    spool.logger = afc.logger
    spool.disable_weight_check = False
    spool.enable_multiple_mapping = afc.enable_multiple_mapping
    spool.function = afc.function

    return spool


def _make_lane(name="lane1", extruder_name="extruder", extruder_index=0):
    lane = MagicMock()
    lane.name = name
    lane.map = []
    lane._map = []
    lane.current_map = None
    lane.color = ""
    lane.material = ""
    lane.weight = 0
    lane.spool_id = None
    lane.runout_lane = None
    lane.spool_vendor = ""
    lane.unit_obj = MagicMock()
    lane.hub_obj = MagicMock()
    lane.extruder_obj = MagicMock()
    lane.extruder_obj.name = extruder_name
    lane.lane_extruder_index = extruder_index
    return lane


def _make_gcmd(**kwargs):
    """Build a gcmd mock that returns values from kwargs.

    Backed by MockGCodeCommand, so a key with no default supplied by the
    caller under test (e.g. gcmd.get('LANE') with no second argument) raises
    gcmd.error(...) exactly like real Klipper -- it does not silently
    return None the way a lambda-based mock would.
    """
    from tests.conftest import MockGCodeCommand
    return MockGCodeCommand(params=kwargs)


# ── __init__ ─────────────────────────────────────────────────────────────────

class TestAFCSpoolInit:
    def _make_config(self):
        from tests.conftest import MockAFC, MockConfig, MockPrinter

        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_Spool", printer=printer)
        return config, printer

    def test_stores_printer_from_config(self):
        config, printer = self._make_config()
        spool = AFCSpool(config)
        assert spool.printer is printer

    def test_sets_spoolman_remote_method(self):
        config, _ = self._make_config()
        spool = AFCSpool(config)
        assert spool.SPOOLMAN_REMOTE_METHOD == "spoolman_set_active_spool"

    def test_print_task_config_obj_starts_none(self):
        config, _ = self._make_config()
        spool = AFCSpool(config)
        assert spool.print_task_config_obj is None

    def test_next_spool_id_starts_none(self):
        config, _ = self._make_config()
        spool = AFCSpool(config)
        assert spool.next_spool_id is None

    def test_filament_id_edits_start_empty(self):
        config, _ = self._make_config()
        spool = AFCSpool(config)
        assert spool._filament_id_edits == {}

    def test_registers_klippy_connect_handler_to_handle_connect(self):
        config, printer = self._make_config()
        spool = AFCSpool(config)
        assert printer._event_handlers["klippy:connect"] == [spool.handle_connect]


# ── cmd_SET_MAP ────────────────────────────────────────────────────────────────

class TestSetMap:
    def test_set_map_assigns_lane_mapping(self):
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.map = ["T1"]
        lane1.current_map = "T1"
        lane2 = _make_lane("lane2")
        lane2.map = ["T0"]
        lane2.current_map = "T0"
        # T0 is currently mapped to lane2; we want to remap it to lane1
        spool.afc.tool_cmds = {"T0": "lane2"}
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        gcmd = _make_gcmd(LANE="lane1", MAP="T0")
        spool.cmd_SET_MAP(gcmd)
        assert lane1.current_map == "T0"
        assert spool.afc.tool_cmds.get("T0") == "lane1"
        assert lane2.map == ["T1"]
        assert spool.afc.tool_cmds.get("T1") == "lane2"

    def test_set_map_invalid_lane_logs_error(self):
        spool = _make_spool()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="nonexistent", MAP="T1")
        spool.cmd_SET_MAP(gcmd)
        assert spool.logger.messages == [("error", "Invalid map command: T1")]

    def test_no_lane_param_raises(self):
        """No default is passed to gcmd.get('LANE'), so a missing LANE
        parameter is expected to raise via Klipper's own required-parameter
        handling, not a hand-rolled None check."""
        spool = _make_spool()
        gcmd = _make_gcmd(MAP="T0")  # no LANE key
        with pytest.raises(Exception, match="missing LANE"):
            spool.cmd_SET_MAP(gcmd)

    def test_no_map_param_raises(self):
        """Same as above, for the MAP parameter."""
        spool = _make_spool()
        gcmd = _make_gcmd(LANE="lane1")  # no MAP key
        with pytest.raises(Exception, match="missing MAP"):
            spool.cmd_SET_MAP(gcmd)

    def test_map_already_assigned_to_lane_logs_info_and_returns(self):
        """MAP already resolves to the requested lane (sw_lane is cur_lane)
        -> logs info and returns without touching any state."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.map = ["T0"]
        lane1.current_map = "T0"
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tool_cmds = {"T0": "lane1"}
        gcmd = _make_gcmd(LANE="lane1", MAP="T0")
        spool.cmd_SET_MAP(gcmd)
        assert spool.logger.messages == [
            ("debug", "lane to switch is lane1"),
            ("info", "T0 is already mapped to lane1"),
        ]
        assert lane1.map == ["T0"]

    def test_single_mapping_swap_leaves_sw_lane_current_map_empty_when_map_empty(self):
        """sw_lane.map[0] if sw_lane.map else "" -- covers the falsy branch:
        cur_lane.map is empty, so after the swap sw_lane.map is also empty
        and current_map must fall back to ""."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.map = []
        lane1.current_map = ""
        lane2 = _make_lane("lane2")
        lane2.map = ["T0"]
        lane2.current_map = "T0"
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        spool.afc.tool_cmds = {"T0": "lane2"}
        gcmd = _make_gcmd(LANE="lane1", MAP="T0")
        spool.cmd_SET_MAP(gcmd)
        assert lane2.map == []
        assert lane2.current_map == ""

    def test_single_mapping_swap_skips_none_placeholder_in_tool_cmds(self):
        """Covers the "NONE" continue branch in both tool_cmds rebuild loops
        for the enable_multiple_mapping=False path."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.map = ["NONE"]
        lane1.current_map = None
        lane2 = _make_lane("lane2")
        lane2.map = ["T0"]
        lane2.current_map = "T0"
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        spool.afc.tool_cmds = {"T0": "lane2"}
        gcmd = _make_gcmd(LANE="lane1", MAP="T0")
        spool.cmd_SET_MAP(gcmd)
        assert "NONE" not in spool.afc.tool_cmds
        assert spool.afc.tool_cmds["T0"] == "lane1"

    def test_lane_not_in_lanes_with_valid_tool_cmd_logs_info(self):
        """MAP in tool_cmds, lane not in lanes → log info + return."""
        spool = _make_spool()
        spool.afc.tool_cmds = {"T0": "lane2"}
        spool.afc.lanes = {}  # lane1 not present
        gcmd = _make_gcmd(LANE="lane1", MAP="T0")
        spool.cmd_SET_MAP(gcmd)
        assert spool.logger.messages == [
            ("debug", "lane to switch is lane2"),
            ("info", "lane1 Unknown"),
        ]

    def test_switch_lane_missing_logs_info_and_returns(self):
        """tool_cmds points MAP at a lane name that isn't in afc.lanes (e.g.
        stale save data) -> logs info and returns without touching state."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        spool.afc.tool_cmds = {"T0": "ghost_lane"}
        spool.afc.lanes = {"lane1": lane1}
        gcmd = _make_gcmd(LANE="lane1", MAP="T0")
        spool.cmd_SET_MAP(gcmd)
        assert spool.logger.messages == [
            ("debug", "lane to switch is ghost_lane"),
            ("info", "ghost_lane is not found when swapping mapping."),
        ]
        # Nothing was mutated -- tool_cmds still points at the stale lane
        assert spool.afc.tool_cmds["T0"] == "ghost_lane"
        assert lane1.map == []

    def test_multiple_mapping_moves_single_map_cmd_between_lanes(self):
        """enable_multiple_mapping=True: MAP is popped out of sw_lane's list
        and appended to cur_lane's list, rather than swapping the full list
        (the enable_multiple_mapping=False behavior covered above)."""
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = ["T2"]
        lane2 = _make_lane("lane2")
        lane2.map = ["T0", "T1"]
        lane2.current_map = "T1"
        spool.afc.tool_cmds = {"T0": "lane2", "T1": "lane2"}
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        gcmd = _make_gcmd(LANE="lane1", MAP="T0")
        spool.cmd_SET_MAP(gcmd)
        # Only T0 moved -- T1 (sw_lane's other mapping) is untouched
        assert lane2.map == ["T1"]
        assert lane1.map == ["T2", "T0"]
        assert spool.afc.tool_cmds["T0"] == "lane1"

    def test_multiple_mapping_clears_sw_lane_current_map_when_it_matches(self):
        """sw_lane.current_map == map_cmd -> cleared, since the tool that was
        actively loaded on sw_lane is the one being moved away."""
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = []
        lane2 = _make_lane("lane2")
        lane2.map = ["T0"]
        lane2.current_map = "T0"
        spool.afc.tool_cmds = {"T0": "lane2"}
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        gcmd = _make_gcmd(LANE="lane1", MAP="T0")
        spool.cmd_SET_MAP(gcmd)
        assert lane2.current_map == ""

    def test_multiple_mapping_leaves_sw_lane_current_map_when_it_differs(self):
        """sw_lane.current_map != map_cmd -> left untouched (the tool being
        moved away wasn't the one actively loaded on sw_lane)."""
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = []
        lane2 = _make_lane("lane2")
        lane2.map = ["T0", "T1"]
        lane2.current_map = "T1"
        spool.afc.tool_cmds = {"T0": "lane2", "T1": "lane2"}
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        gcmd = _make_gcmd(LANE="lane1", MAP="T0")
        spool.cmd_SET_MAP(gcmd)
        assert lane2.current_map == "T1"

    def test_multiple_mapping_filters_none_placeholder_from_cur_lane(self):
        """cur_lane starting with the "NONE" placeholder must lose it once a
        real mapping is added, rather than keeping ["NONE", "T0"]."""
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = ["NONE"]
        lane2 = _make_lane("lane2")
        lane2.map = ["T0"]
        lane2.current_map = "T0"
        spool.afc.tool_cmds = {"T0": "lane2"}
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        gcmd = _make_gcmd(LANE="lane1", MAP="T0")
        spool.cmd_SET_MAP(gcmd)
        assert lane1.map == ["T0"]


# ── _set_map ──────────────────────────────────────────────────────────────────

class TestSetMapCore:
    """Direct coverage of _set_map, independent of the cmd_SET_MAP entry point, which is
    fully exercised above, and the extruder_map_table resync call site below."""

    def test_swaps_mapping_between_lanes(self):
        spool = _make_spool()
        spool.afc.snapmaker_printer = False
        lane1 = _make_lane("lane1")
        lane1.map = ["T1"]
        lane2 = _make_lane("lane2")
        lane2.map = ["T0"]
        lane2.current_map = "T0"
        spool.afc.tool_cmds = {"T0": "lane2"}
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        spool._set_map(lane1, lane2, "T0")
        assert lane1.current_map == "T0"
        assert spool.afc.tool_cmds["T0"] == "lane1"
        assert lane2.map == ["T1"]

    def test_calls_sync_extruder_map_table(self):
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.map = ["T1"]
        lane2 = _make_lane("lane2")
        lane2.map = ["T0"]
        spool.afc.tool_cmds = {"T0": "lane2"}
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        spool.sync_extruder_map_table = MagicMock()
        spool._set_map(lane1, lane2, "T0")
        spool.sync_extruder_map_table.assert_called_once()


# ── snapmaker_sync_cb ─────────────────────────────────────────────────────────

class TestSnapmakerSyncCb:
    def test_calls_sync_extruder_map_table_and_sync_toolhead_filament(self):
        spool = _make_spool()
        spool.sync_extruder_map_table = MagicMock()
        spool.sync_toolhead_filament = MagicMock()
        spool.snapmaker_sync_cb()
        spool.sync_extruder_map_table.assert_called_once_with()
        spool.sync_toolhead_filament.assert_called_once_with()

    def test_calls_sync_extruder_map_table_before_sync_toolhead_filament(self):
        spool = _make_spool()
        call_order: list = []
        spool.sync_extruder_map_table = MagicMock(
            side_effect=lambda: call_order.append("extruder_map_table"))
        spool.sync_toolhead_filament = MagicMock(
            side_effect=lambda: call_order.append("toolhead_filament"))
        spool.snapmaker_sync_cb()
        assert call_order == ["extruder_map_table", "toolhead_filament"]


# ── sync_toolhead_filament ────────────────────────────────────────────────────

class TestSyncToolheadFilament:
    def _make_extruder(self, lanes=None, lane_loaded=None, is_standalone=False, tc_lane=None):
        extruder = MagicMock()
        extruder.lanes = lanes if lanes is not None else {}
        extruder.lane_loaded = lane_loaded
        extruder.is_standalone.return_value = is_standalone
        extruder.tc_lane = tc_lane
        return extruder

    def test_loaded_lane_found_is_passed_directly(self):
        """Independent coverage of the `lane is None` operand: once a lane is
        found via extruder.lanes, is_standalone() must not even be consulted."""
        spool = _make_spool()
        spool.set_snapmaker_filament_params = MagicMock()
        lane1 = _make_lane("lane1")
        extruder = self._make_extruder(
            lanes={"lane1": lane1}, lane_loaded="lane1", is_standalone=True)
        spool.afc.tools = {"extruder": extruder}
        spool.sync_toolhead_filament()
        extruder.is_standalone.assert_not_called()
        spool.set_snapmaker_filament_params.assert_called_once_with(lane1)

    def test_lane_not_found_and_standalone_uses_tc_lane(self):
        """Independent coverage of the `extruder.is_standalone()` operand of
        `lane is None and extruder.is_standalone()`: with no lane resolved via
        extruder.lanes, a standalone extruder falls back to tc_lane."""
        spool = _make_spool()
        spool.set_snapmaker_filament_params = MagicMock()
        tc_lane = _make_lane("tc_lane1")
        extruder = self._make_extruder(
            lanes={}, lane_loaded="ghost", is_standalone=True, tc_lane=tc_lane)
        spool.afc.tools = {"extruder": extruder}
        spool.sync_toolhead_filament()
        spool.set_snapmaker_filament_params.assert_called_once_with(tc_lane)

    def test_lane_not_found_and_not_standalone_skips_extruder(self):
        """Independent coverage of the `extruder.is_standalone()` operand's
        falsy side: a non-standalone extruder with nothing resolved must not
        fall back to tc_lane, and with no lane resolved at all must not call
        set_snapmaker_filament_params."""
        spool = _make_spool()
        spool.set_snapmaker_filament_params = MagicMock()
        extruder = self._make_extruder(lanes={}, lane_loaded="ghost", is_standalone=False)
        spool.afc.tools = {"extruder": extruder}
        spool.sync_toolhead_filament()
        spool.set_snapmaker_filament_params.assert_not_called()

    def test_calls_once_per_extruder(self):
        spool = _make_spool()
        spool.set_snapmaker_filament_params = MagicMock()
        lane1 = _make_lane("lane1")
        lane2 = _make_lane("lane2")
        extruder0 = self._make_extruder(lanes={"lane1": lane1}, lane_loaded="lane1")
        extruder1 = self._make_extruder(lanes={"lane2": lane2}, lane_loaded="lane2")
        spool.afc.tools = {"extruder": extruder0, "extruder1": extruder1}
        spool.sync_toolhead_filament()
        assert spool.set_snapmaker_filament_params.call_count == 2
        spool.set_snapmaker_filament_params.assert_any_call(lane1)
        spool.set_snapmaker_filament_params.assert_any_call(lane2)

    def test_no_extruders_does_not_call_set_snapmaker_filament_params(self):
        spool = _make_spool()
        spool.set_snapmaker_filament_params = MagicMock()
        spool.afc.tools = {}
        spool.sync_toolhead_filament()
        spool.set_snapmaker_filament_params.assert_not_called()


# ── sync_extruder_map_table ──────────────────────────────────────────────────

class TestSyncExtruderMapTable:
    def _make(self):
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())

        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = {"extruder_map_table": [0, 0, 0, 0]}
        spool.print_task_config_obj.config_path = "fake_path"
        spool.printer = MagicMock()
        return spool

    def test_not_snapmaker_printer_does_nothing(self):
        spool = self._make()
        spool.afc.snapmaker_printer = False
        spool.sync_extruder_map_table()
        spool.printer.update_snapmaker_config_file.assert_not_called()

    def test_print_task_config_obj_none_does_nothing(self):
        spool = self._make()
        spool.print_task_config_obj = None
        spool.sync_extruder_map_table()
        spool.printer.update_snapmaker_config_file.assert_not_called()

    def test_updates_table_from_tool_cmds(self):
        spool = self._make()
        lane = _make_lane("lane1", extruder_index=2)
        spool.afc.lanes = {"lane1": lane}
        spool.afc.tool_cmds = {"T1": "lane1"}
        spool.sync_extruder_map_table()
        assert spool.print_task_config_obj.print_task_config["extruder_map_table"][1] == 2
        spool.printer.update_snapmaker_config_file.assert_called_once()

    def test_skips_non_integer_t_suffix(self):
        spool = self._make()
        lane = _make_lane("lane1", extruder_index=3)
        spool.afc.lanes = {"lane1": lane}
        spool.afc.tool_cmds = {"TX": "lane1"}
        spool.sync_extruder_map_table()
        assert spool.print_task_config_obj.print_task_config["extruder_map_table"] == [0, 0, 0, 0]

    def test_skips_logical_index_out_of_bounds(self):
        spool = self._make()
        lane = _make_lane("lane1", extruder_index=3)
        spool.afc.lanes = {"lane1": lane}
        spool.afc.tool_cmds = {"T99": "lane1"}
        spool.sync_extruder_map_table()
        assert spool.print_task_config_obj.print_task_config["extruder_map_table"] == [0, 0, 0, 0]

    def test_skips_lane_not_in_lanes(self):
        spool = self._make()
        spool.afc.lanes = {}
        spool.afc.tool_cmds = {"T0": "ghost"}
        spool.sync_extruder_map_table()
        assert spool.print_task_config_obj.print_task_config["extruder_map_table"] == [0, 0, 0, 0]

    def test_exception_logs_error_and_debug(self):
        spool = self._make()
        spool.afc.lanes = {"lane1": _make_lane("lane1")}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.printer.update_snapmaker_config_file.side_effect = RuntimeError("boom")
        spool.sync_extruder_map_table()
        assert spool.logger.messages[0] == (
            "error", "Error when trying to update extruder_map_table for snapmaker print_task_config")
        assert spool.logger.messages[1][0] == "debug"


# ── resync_afc_mapping_from_extruder_map_table ───────────────────────────────

class TestResyncAfcMappingFromExtruderMapTable:
    def _make(self):
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.afc.run_script = MagicMock()
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = {"extruder_map_table": [0, 0, 0, 0]}
        # All slots marked "used" by default so existing tests exercise the same
        # mismatches they did before the used-slot filter was added.
        spool.print_task_config_obj.print_task_config_2 = {
            "filament_used_g": [1.0] * 8, "filament_used_mm": [0.0] * 8}
        spool._set_map = MagicMock()
        return spool

    def test_not_snapmaker_printer_does_nothing(self):
        spool = self._make()
        spool.afc.snapmaker_printer = False
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()

    def test_print_task_config_obj_none_does_nothing(self):
        spool = self._make()
        spool.print_task_config_obj = None
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()

    def test_skips_non_integer_t_suffix(self):
        spool = self._make()
        spool.afc.tool_cmds = {"TX": "lane1"}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()

    def test_skips_logical_index_out_of_bounds(self):
        spool = self._make()
        spool.afc.tool_cmds = {"T99": "lane1"}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()

    def test_skips_lane_not_in_lanes(self):
        spool = self._make()
        spool.afc.lanes = {}
        spool.afc.tool_cmds = {"T0": "ghost"}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()

    def test_skips_when_index_matches_and_no_tool_object_exists(self):
        """No AFC tool object exists for this extruder at all (afc.tools is
        empty), so there's nothing to resolve a specific lane through. Since
        cur_lane's index already matches, this is treated as already correct
        rather than unresolved."""
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 0
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        spool.afc.run_script.assert_not_called()

    def test_no_tool_object_and_index_mismatch_is_unresolved(self):
        """Independent coverage of the `index_already_matches` operand for the
        target_extruder-is-None branch: here the index does NOT already match,
        so with no tool object to resolve through, this must be unresolved."""
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tools = {}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 1
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        spool.afc.run_script.assert_called_once_with("CANCEL_PRINT", gcmd="callback")

    def test_unit_attached_extruder_with_nothing_loaded_and_index_matches_is_skipped(self):
        """Independent coverage of the `index_already_matches` operand for the
        unit-attached/lane_loaded-is-None branch: nothing is currently loaded to
        disambiguate with, but cur_lane already sits on the right extruder, so
        this is treated as already correct rather than unresolved."""
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=1)
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = False
        target_extruder.lane_loaded = None
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tools = {"extruder1": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 1
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        spool.afc.run_script.assert_not_called()

    def test_multiple_lanes_on_same_extruder_resolves_via_currently_loaded_lane(self):
        """Regression test: lane2 and lane3 both feed extruder0. T2 already
        points at lane2, whose index matches the screen's target extruder, but
        lane3 is the one actually loaded there. An index match alone must not
        be trusted to skip for a unit-attached extruder: the currently loaded
        lane must still be resolved and compared."""
        spool = self._make()
        lane2 = _make_lane("lane2", extruder_index=0)
        lane3 = _make_lane("lane3", extruder_index=0)
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = False
        target_extruder.lane_loaded = "lane3"
        spool.afc.lanes = {"lane2": lane2, "lane3": lane3}
        spool.afc.tools = {"extruder": target_extruder}
        spool.afc.tool_cmds = {"T2": "lane2"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][2] = 0
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_called_once_with(lane3, lane2, "T2")

    def _make_mismatch(self, spool):
        """Sets up T0 as a mismatch (lane1 -> extruder1 in AFC, but screen's table
        says extruder 0) that would resolve to a real new_lane if not filtered out."""
        lane1 = _make_lane("lane1", extruder_index=1)
        lane_new = _make_lane("lane_new", extruder_index=0)
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = False
        target_extruder.lane_loaded = "lane_new"
        spool.afc.lanes = {"lane1": lane1, "lane_new": lane_new}
        spool.afc.tools = {"extruder": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 0

    def test_skips_slot_not_marked_used_by_filament_used_g_or_mm(self):
        """SET_PRINT_TASK_PARAMETERS resets unused slots to the identity default;
        a slot this print doesn't use must not be treated as a real remap."""
        spool = self._make()
        self._make_mismatch(spool)
        spool.print_task_config_obj.print_task_config_2 = {
            "filament_used_g": [0.0], "filament_used_mm": [0.0]}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()

    def test_applies_slot_marked_used_via_filament_used_g_alone(self):
        """Independent coverage of the `used_g` operand of `used_g or used_mm`:
        filament_used_mm is zero here, so only filament_used_g being above the
        threshold can mark this slot as used."""
        spool = self._make()
        self._make_mismatch(spool)
        spool.print_task_config_obj.print_task_config_2 = {
            "filament_used_g": [1.0], "filament_used_mm": [0.0]}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_called_once()

    def test_applies_slot_marked_used_via_filament_used_mm_alone(self):
        """Independent coverage of the `used_mm` operand: filament_used_g is zero
        here, so only filament_used_mm being above the threshold can mark this
        slot as used."""
        spool = self._make()
        self._make_mismatch(spool)
        spool.print_task_config_obj.print_task_config_2 = {
            "filament_used_g": [0.0], "filament_used_mm": [1.0]}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_called_once()

    def test_index_out_of_range_for_filament_used_g_still_applies_via_mm(self):
        """Independent coverage of the `logical_index < len(...)` bounds-check operand for
        filament_used_g: an empty filament_used_g list means the bounds check alone, not the
        threshold check, makes used_g False, but filament_used_mm still covers this index and
        marks the slot used."""
        spool = self._make()
        self._make_mismatch(spool)
        spool.print_task_config_obj.print_task_config_2 = {
            "filament_used_g": [], "filament_used_mm": [1.0]}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_called_once()

    def test_index_out_of_range_for_filament_used_mm_still_applies_via_g(self):
        """Independent coverage of the `logical_index < len(...)` bounds-check
        operand for filament_used_mm: an empty filament_used_mm list means the
        bounds check alone makes used_mm False, but filament_used_g still covers
        this index and marks the slot used."""
        spool = self._make()
        self._make_mismatch(spool)
        spool.print_task_config_obj.print_task_config_2 = {
            "filament_used_g": [1.0], "filament_used_mm": []}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_called_once()

    def test_index_out_of_range_for_both_is_skipped(self):
        spool = self._make()
        self._make_mismatch(spool)
        spool.print_task_config_obj.print_task_config_2 = {
            "filament_used_g": [], "filament_used_mm": []}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        # A regressed bounds check would index the empty lists directly, raise
        # IndexError, and get swallowed by the method's own except Exception --
        # still leaving _set_map uncalled above, so that alone can't tell a
        # clean skip apart from a crash. The valid skip logs nothing and
        # never cancels the print.
        spool.afc.run_script.assert_not_called()
        assert spool.logger.messages == []

    def test_missing_print_task_config_2_attribute_is_treated_as_unused(self):
        """getattr's default {} applies when print_task_config_obj doesn't expose
        print_task_config_2 at all, not just when the dict's keys are missing."""
        spool = self._make()
        self._make_mismatch(spool)

        class _NoConfig2:
            print_task_config = spool.print_task_config_obj.print_task_config

        spool.print_task_config_obj = _NoConfig2()
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()

    def test_target_extruder_not_found_cancels_print(self):
        """AFC has no lane-level signal besides what's currently loaded into the
        target extruder, so an unresolvable target (no AFC tool object at all for
        it) can't be guessed at: cancel rather than silently keep the stale map."""
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tools = {}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 1
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        spool.afc.run_script.assert_called_once_with("CANCEL_PRINT", gcmd="callback")
        assert spool.logger.messages == [(
            "error",
            "T0 mapping was changed from the printer's screen or slicer to an extruder with no "
            "lane currently loaded, so AFC can't tell which lane to use. Cancelling "
            "this print, please load the correct filament into that extruder, then "
            "restart the print from the printer's UI."
        )]

    def test_target_extruder_has_no_lane_loaded_cancels_print(self):
        """Independent coverage of the `target_extruder.lane_loaded is None` operand: the target
        extruder exists here, unlike the test above, but nothing is currently loaded into it,
        so AFC still can't tell which lane to use."""
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = False
        target_extruder.lane_loaded = None
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tools = {"extruder1": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 1
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        spool.afc.run_script.assert_called_once_with("CANCEL_PRINT", gcmd="callback")

    def test_loaded_lane_not_found_cancels_print(self):
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = False
        target_extruder.lane_loaded = "ghost"
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tools = {"extruder1": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 1
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        spool.afc.run_script.assert_called_once_with("CANCEL_PRINT", gcmd="callback")

    def test_standalone_extruder_with_no_tc_lane_cancels_print(self):
        """Standalone extruders have exactly one lane hardwired to them (no
        ambiguity), but that lane object itself must still exist."""
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = True
        target_extruder.tc_lane = None
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tools = {"extruder1": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 1
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        spool.afc.run_script.assert_called_once_with("CANCEL_PRINT", gcmd="callback")

    def test_standalone_extruder_not_prepped_or_loaded_cancels_print(self):
        """Independent coverage of the `not (prep_state or load_state)` operand:
        the tc_lane exists here, but neither prep_state nor load_state is set, so
        AFC still can't trust it as the intended lane."""
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        tc_lane = _make_lane("tc_lane1", extruder_index=1)
        tc_lane.prep_state = False
        tc_lane.load_state = False
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = True
        target_extruder.tc_lane = tc_lane
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tools = {"extruder1": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 1
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        spool.afc.run_script.assert_called_once_with("CANCEL_PRINT", gcmd="callback")

    def test_standalone_extruder_already_on_right_extruder_is_skipped(self):
        """Index already matches for a standalone extruder, so it's trusted
        outright: this must not even look at tc_lane's prep_state/load_state."""
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=1)
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = True
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tools = {"extruder1": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 1
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        spool.afc.run_script.assert_not_called()

    def test_standalone_extruder_prepped_applies_remap(self):
        """Independent coverage of the `prep_state` operand of
        `prep_state or load_state`: load_state is False here, so prep_state alone
        must be what lets this resolve and apply."""
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        tc_lane = _make_lane("tc_lane1", extruder_index=1)
        tc_lane.prep_state = True
        tc_lane.load_state = False
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = True
        target_extruder.tc_lane = tc_lane
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tools = {"extruder1": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 1
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool.afc.run_script.assert_not_called()
        spool._set_map.assert_called_once_with(tc_lane, lane1, "T0")

    def test_standalone_extruder_loaded_applies_remap(self):
        """Independent coverage of the `load_state` operand: prep_state is False
        here, so load_state alone must be what lets this resolve and apply."""
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        tc_lane = _make_lane("tc_lane1", extruder_index=1)
        tc_lane.prep_state = False
        tc_lane.load_state = True
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = True
        target_extruder.tc_lane = tc_lane
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tools = {"extruder1": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 1
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool.afc.run_script.assert_not_called()
        spool._set_map.assert_called_once_with(tc_lane, lane1, "T0")

    def test_standalone_extruder_resolving_to_same_lane_is_skipped(self):
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        lane1.prep_state = True
        lane1.load_state = False
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = True
        target_extruder.tc_lane = lane1
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tools = {"extruder1": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 1
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        spool.afc.run_script.assert_not_called()

    def test_multiple_unresolved_mappings_are_all_named_in_the_warning(self):
        spool = self._make()
        lane0 = _make_lane("lane0", extruder_index=0)
        lane1 = _make_lane("lane1", extruder_index=1)
        target_extruder0 = MagicMock()
        target_extruder0.is_standalone.return_value = False
        target_extruder0.lane_loaded = None
        target_extruder1 = MagicMock()
        target_extruder1.is_standalone.return_value = False
        target_extruder1.lane_loaded = None
        spool.afc.lanes = {"lane0": lane0, "lane1": lane1}
        spool.afc.tools = {"extruder2": target_extruder0, "extruder3": target_extruder1}
        spool.afc.tool_cmds = {"T0": "lane0", "T1": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"] = [2, 3, 0, 0]
        spool.print_task_config_obj.print_task_config_2 = {
            "filament_used_g": [1.0, 1.0], "filament_used_mm": [0.0, 0.0]}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        assert spool.logger.messages == [(
            "error",
            "T0, T1 mapping was changed from the printer's screen or slicer to an extruder with "
            "no lane currently loaded, so AFC can't tell which lane to use. Cancelling "
            "this print, please load the correct filament into that extruder, then "
            "restart the print from the printer's UI."
        )]

    def test_unresolved_mapping_takes_priority_over_max_changes_check(self):
        """When both an unresolved mapping and more than MAX_UNATTENDED_MAPPING_CHANGES
        resolved changes are present, the unresolved one is reported: it's the one
        that can never be safely adopted, unlike the resolved changes."""
        spool = self._make()
        count = MAX_UNATTENDED_MAPPING_CHANGES + 1
        self._make_many_changes(spool, count)
        unresolved_lane = _make_lane("unresolved_lane", extruder_index=99)
        spool.afc.lanes["unresolved_lane"] = unresolved_lane
        spool.afc.tool_cmds[f"T{count}"] = "unresolved_lane"
        spool.print_task_config_obj.print_task_config["extruder_map_table"].append(50)
        spool.print_task_config_obj.print_task_config_2["filament_used_g"].append(1.0)
        spool.print_task_config_obj.print_task_config_2["filament_used_mm"].append(0.0)
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        assert spool.logger.messages == [(
            "error",
            f"T{count} mapping was changed from the printer's screen or slicer to an extruder "
            "with no lane currently loaded, so AFC can't tell which lane to use. "
            "Cancelling this print, please load the correct filament into that "
            "extruder, then restart the print from the printer's UI."
        )]

    def test_skips_when_new_lane_is_cur_lane(self):
        """Defensive guard: if the resolved new_lane is the same object as
        cur_lane despite the index mismatch, no swap is attempted."""
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = False
        target_extruder.lane_loaded = "lane1"
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tools = {"extruder1": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 1
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()

    def test_calls_set_map_with_new_lane_when_extruder_0(self):
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=1)
        lane_new = _make_lane("lane_new", extruder_index=0)
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = False
        target_extruder.lane_loaded = "lane_new"
        spool.afc.lanes = {"lane1": lane1, "lane_new": lane_new}
        spool.afc.tools = {"extruder": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 0
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_called_once_with(lane_new, lane1, "T0")

    def test_calls_set_map_with_new_lane_when_extruder_nonzero(self):
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        lane_new = _make_lane("lane_new", extruder_index=2)
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = False
        target_extruder.lane_loaded = "lane_new"
        spool.afc.lanes = {"lane1": lane1, "lane_new": lane_new}
        spool.afc.tools = {"extruder2": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 2
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_called_once_with(lane_new, lane1, "T0")

    def test_logs_warning_message_describing_the_change(self):
        spool = self._make()
        lane1 = _make_lane("lane1", extruder_index=0)
        lane_new = _make_lane("lane_new", extruder_index=2)
        target_extruder = MagicMock()
        target_extruder.is_standalone.return_value = False
        target_extruder.lane_loaded = "lane_new"
        spool.afc.lanes = {"lane1": lane1, "lane_new": lane_new}
        spool.afc.tools = {"extruder2": target_extruder}
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.print_task_config_obj.print_task_config["extruder_map_table"][0] = 2
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        assert spool.logger.messages == [(
            "warning",
            "The following mappings were changed from the printer's screen or slicer:\n"
            "T0 was lane1 (extruder 0), now lane_new (extruder 2).\n"
            "AFC's mapping has been updated to match."
        )]

    def test_logs_one_warning_listing_all_changes_when_multiple_apply(self):
        spool = self._make()
        self._make_many_changes(spool, 2)
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        assert spool.logger.messages == [(
            "warning",
            "The following mappings were changed from the printer's screen or slicer:\n"
            "T0 was lane0 (extruder 1), now lane_new0 (extruder 0).\n"
            "T1 was lane1 (extruder 2), now lane_new1 (extruder 1).\n"
            "AFC's mapping has been updated to match."
        )]

    def test_no_warning_logged_when_there_are_no_changes_to_apply(self):
        spool = self._make()
        spool.afc.tool_cmds = {}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        assert spool.logger.messages == []

    def test_exception_logs_error_and_debug(self):
        spool = self._make()
        spool.afc.lanes = MagicMock()
        spool.afc.lanes.get.side_effect = RuntimeError("boom")
        spool.afc.tool_cmds = {"T0": "lane1"}
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        assert spool.logger.messages[0] == (
            "error", "Error when trying to resync AFC mapping from snapmaker print_task_config")
        assert spool.logger.messages[1][0] == "debug"

    def _make_many_changes(self, spool, count):
        """Build `count` independent T(n) mismatches, each resolving to a distinct
        new_lane, so every one of them lands in `changes`."""
        extruder_map_table = [0] * count
        lanes = {}
        tools = {}
        tool_cmds = {}
        for i in range(count):
            cur_lane = _make_lane(f"lane{i}", extruder_index=i + 1)
            new_lane = _make_lane(f"lane_new{i}", extruder_index=0)
            lanes[cur_lane.name] = cur_lane
            lanes[new_lane.name] = new_lane
            th_name = "extruder" if i == 0 else f"extruder{i}"
            target_extruder = MagicMock()
            target_extruder.is_standalone.return_value = False
            target_extruder.lane_loaded = new_lane.name
            tools[th_name] = target_extruder
            tool_cmds[f"T{i}"] = cur_lane.name
            extruder_map_table[i] = i
        spool.print_task_config_obj.print_task_config = {"extruder_map_table": extruder_map_table}
        spool.print_task_config_obj.print_task_config_2 = {
            "filament_used_g": [1.0] * count, "filament_used_mm": [0.0] * count}
        spool.afc.lanes = lanes
        spool.afc.tools = tools
        spool.afc.tool_cmds = tool_cmds

    def test_more_than_max_changes_warns_and_cancels_print_without_applying(self):
        spool = self._make()
        count = MAX_UNATTENDED_MAPPING_CHANGES + 1
        self._make_many_changes(spool, count)
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool._set_map.assert_not_called()
        spool.afc.run_script.assert_called_once_with("CANCEL_PRINT", gcmd="callback")
        assert spool.logger.messages == [(
            "error",
            f"{count} T(n) mappings were changed from the printer's screen or slicer, more than "
            f"the {MAX_UNATTENDED_MAPPING_CHANGES} AFC will adopt automatically. "
            "Cancelling this print, please restart it from the printer's UI so the "
            "new mapping is picked up correctly."
        )]

    def test_exactly_max_changes_applies_them_instead_of_cancelling(self):
        """MAX_UNATTENDED_MAPPING_CHANGES itself is still within the adopt-automatically
        range: only strictly more than that cancels."""
        spool = self._make()
        count = MAX_UNATTENDED_MAPPING_CHANGES
        self._make_many_changes(spool, count)
        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")
        spool.afc.run_script.assert_not_called()
        assert spool._set_map.call_count == count
        for i in range(count):
            spool._set_map.assert_any_call(
                spool.afc.lanes[f"lane_new{i}"], spool.afc.lanes[f"lane{i}"], f"T{i}")

    def test_two_tool_swap_uses_the_real_set_map_without_corrupting_either_lane(self):
        """Regression test: screen swaps T0 and T1 between lane_a and lane_b in single-mapping
        mode. The first real `_set_map` call already completes the full swap, so the second
        entry's stale pre-batch owner must be re-read and skipped, not reapplied."""
        spool = self._make()
        del spool._set_map
        spool.enable_multiple_mapping = False

        lane_a = _make_lane("lane_a", extruder_name="extruder", extruder_index=0)
        lane_a.map = ["T0"]
        lane_a.current_map = "T0"
        lane_b = _make_lane("lane_b", extruder_name="extruder1", extruder_index=1)
        lane_b.map = ["T1"]
        lane_b.current_map = "T1"
        spool.afc.lanes = {"lane_a": lane_a, "lane_b": lane_b}
        spool.afc.tool_cmds = {"T0": "lane_a", "T1": "lane_b"}

        extruder_a = MagicMock()
        extruder_a.is_standalone.return_value = False
        extruder_a.lane_loaded = "lane_a"
        extruder_b = MagicMock()
        extruder_b.is_standalone.return_value = False
        extruder_b.lane_loaded = "lane_b"
        spool.afc.tools = {"extruder": extruder_a, "extruder1": extruder_b}

        # The screen swapped both: T0 now targets extruder1 (lane_b's extruder), T1 now
        # targets extruder (lane_a's extruder).
        spool.print_task_config_obj.print_task_config = {"extruder_map_table": [1, 0]}
        spool.print_task_config_obj.print_task_config_2 = {
            "filament_used_g": [1.0, 1.0], "filament_used_mm": [0.0, 0.0]}

        spool.resync_afc_mapping_from_extruder_map_table(gcmd="callback")

        assert lane_a.map == ["T1"]
        assert lane_a.current_map == "T1"
        assert lane_b.map == ["T0"]
        assert lane_b.current_map == "T0"
        assert spool.afc.tool_cmds == {"T0": "lane_b", "T1": "lane_a"}


# ── cmd_AFC_SWAP_MAPPING ────────────────────────────────────────────────────────

class TestAfcSwapMapping:
    def test_from_equals_to_raises_and_does_not_swap(self):
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.map = ["T0"]
        lane1.current_map = "T0"
        spool.afc.lanes = {"lane1": lane1}
        gcmd = _make_gcmd(FROM="lane1", TO="lane1")
        with pytest.raises(Exception, match=r"FROM\(lane1\) TO\(lane1\) values are the same, not swapping\."):
            spool.cmd_AFC_SWAP_MAPPING(gcmd)
        assert lane1.map == ["T0"]
        assert lane1.current_map == "T0"

    def test_from_equals_to_case_insensitive_raises(self):
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane1}
        gcmd = _make_gcmd(FROM="Lane1", TO="lane1")
        with pytest.raises(Exception, match=r"FROM\(Lane1\) TO\(lane1\) values are the same, not swapping\."):
            spool.cmd_AFC_SWAP_MAPPING(gcmd)

    def test_from_lane_not_found_raises(self):
        spool = _make_spool()
        spool.afc.lanes = {"lane2": _make_lane("lane2")}
        gcmd = _make_gcmd(FROM="lane1", TO="lane2")
        with pytest.raises(Exception, match="Swapping from lane1 not found"):
            spool.cmd_AFC_SWAP_MAPPING(gcmd)

    def test_to_lane_not_found_raises(self):
        spool = _make_spool()
        spool.afc.lanes = {"lane1": _make_lane("lane1")}
        gcmd = _make_gcmd(FROM="lane1", TO="lane2")
        with pytest.raises(Exception, match="Swapping to lane2 not found"):
            spool.cmd_AFC_SWAP_MAPPING(gcmd)

    def test_swap_exchanges_map_and_current_map(self):
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.map = ["T0"]
        lane1.current_map = "T0"
        lane2 = _make_lane("lane2")
        lane2.map = ["T1", "T2"]
        lane2.current_map = "T1"
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        gcmd = _make_gcmd(FROM="lane1", TO="lane2")
        spool.cmd_AFC_SWAP_MAPPING(gcmd)
        assert lane1.map == ["T1", "T2"]
        assert lane1.current_map == "T1"
        assert lane2.map == ["T0"]
        assert lane2.current_map == "T0"

    def test_swap_updates_tool_cmds_to_lane_names(self):
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.map = ["T0"]
        lane1.current_map = "T0"
        lane2 = _make_lane("lane2")
        lane2.map = ["T1", "T2"]
        lane2.current_map = "T1"
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        spool.afc.tool_cmds = {"T0": "lane1", "T1": "lane2", "T2": "lane2"}
        gcmd = _make_gcmd(FROM="lane1", TO="lane2")
        spool.cmd_AFC_SWAP_MAPPING(gcmd)
        assert spool.afc.tool_cmds["T1"] == "lane1"
        assert spool.afc.tool_cmds["T2"] == "lane1"
        assert spool.afc.tool_cmds["T0"] == "lane2"

    def test_swap_skips_none_placeholder_in_tool_cmds(self):
        """A lane with no real mapping carries map=["NONE"]; that
        placeholder must not be written into tool_cmds."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.map = ["NONE"]
        lane1.current_map = None
        lane2 = _make_lane("lane2")
        lane2.map = ["T1"]
        lane2.current_map = "T1"
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        spool.afc.tool_cmds = {"T1": "lane2"}
        gcmd = _make_gcmd(FROM="lane1", TO="lane2")
        spool.cmd_AFC_SWAP_MAPPING(gcmd)
        assert "NONE" not in spool.afc.tool_cmds

    def test_swap_skips_none_placeholder_landing_on_from_lane(self):
        """Same guard as above, but exercised for the FROM lane's post-swap
        list (i.e. the "NONE" placeholder originated from the TO lane)."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.map = ["T1"]
        lane1.current_map = "T1"
        lane2 = _make_lane("lane2")
        lane2.map = ["NONE"]
        lane2.current_map = None
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        spool.afc.tool_cmds = {"T1": "lane1"}
        gcmd = _make_gcmd(FROM="lane1", TO="lane2")
        spool.cmd_AFC_SWAP_MAPPING(gcmd)
        assert "NONE" not in spool.afc.tool_cmds
        assert spool.afc.tool_cmds["T1"] == "lane2"

    def test_swap_sends_lane_data_for_both_lanes_and_saves(self):
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.map = ["T0"]
        lane2 = _make_lane("lane2")
        lane2.map = ["T1"]
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        gcmd = _make_gcmd(FROM="lane1", TO="lane2")
        spool.cmd_AFC_SWAP_MAPPING(gcmd)
        lane1.send_lane_data.assert_called_once()
        lane2.send_lane_data.assert_called_once()
        spool.afc.save_vars.assert_called_once()


# ── cmd_AFC_ADD_MAPPING ─────────────────────────────────────────────────────────

class TestAfcAddMapping:
    def test_raises_when_multiple_mapping_disabled(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = False
        gcmd = _make_gcmd(LANE="lane1", MAPPING="T1")
        with pytest.raises(Exception, match="Enable multiple mapping needs to be enabled"):
            spool.cmd_AFC_ADD_MAPPING(gcmd)

    def test_raises_when_lane_not_found(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="lane1", MAPPING="T1")
        with pytest.raises(Exception, match="lane1 is not a valid lane"):
            spool.cmd_AFC_ADD_MAPPING(gcmd)

    def test_adds_single_mapping(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = ["T0"]
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tool_cmds = {"T0": "lane1"}
        gcmd = _make_gcmd(LANE="lane1", MAPPING="T1")
        spool.cmd_AFC_ADD_MAPPING(gcmd)
        assert spool.afc.tool_cmds["T1"] == "lane1"
        assert lane1.map == ["T0", "T1"]
        spool.function.register_tool_macro.assert_called_once_with("lane1", "T1")
        lane1.send_lane_data.assert_called_once()
        spool.afc.save_vars.assert_called_once()

    def test_adds_comma_separated_multiple_mappings(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = ["T0"]
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tool_cmds = {"T0": "lane1"}
        gcmd = _make_gcmd(LANE="lane1", MAPPING="T1,T2")
        spool.cmd_AFC_ADD_MAPPING(gcmd)
        assert spool.afc.tool_cmds["T1"] == "lane1"
        assert spool.afc.tool_cmds["T2"] == "lane1"
        assert lane1.map == ["T0", "T1", "T2"]

    def test_mapping_is_uppercased(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = []
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tool_cmds = {}
        gcmd = _make_gcmd(LANE="lane1", MAPPING="t1")
        spool.cmd_AFC_ADD_MAPPING(gcmd)
        assert "T1" in spool.afc.tool_cmds
        assert "t1" not in spool.afc.tool_cmds

    def test_skips_and_logs_error_when_mapping_already_exists(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = ["T0"]
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tool_cmds = {"T0": "lane1", "T1": "lane2"}
        gcmd = _make_gcmd(LANE="lane1", MAPPING="T1")
        spool.cmd_AFC_ADD_MAPPING(gcmd)
        # Existing mapping is untouched, not stolen from lane2
        assert spool.afc.tool_cmds["T1"] == "lane2"
        assert lane1.map == ["T0"]
        spool.afc.error.AFC_error.assert_called_once_with(
            "Cannot map T1 to lane1, as mapping already exists.", False
        , gcmd=gcmd)

    def test_skips_and_logs_error_when_mapping_is_not_t_number_format(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = ["T0"]
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tool_cmds = {"T0": "lane1"}
        gcmd = _make_gcmd(LANE="lane1", MAPPING="CUSTOM_LANE")
        spool.cmd_AFC_ADD_MAPPING(gcmd)
        assert "CUSTOM_LANE" not in spool.afc.tool_cmds
        assert lane1.map == ["T0"]
        spool.afc.error.AFC_error.assert_called_once_with(
            "'CUSTOM_LANE' is not a valid mapping, expect T(n) format.", False
        , gcmd=gcmd)


# ── cmd_AFC_REMOVE_MAPPING ───────────────────────────────────────────────────────

class TestAfcRemoveMapping:
    def test_raises_when_multiple_mapping_disabled(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = False
        gcmd = _make_gcmd(MAPPING="T1")
        with pytest.raises(Exception, match="Enable multiple mapping needs to be enabled"):
            spool.cmd_AFC_REMOVE_MAPPING(gcmd)

    def test_removes_existing_mapping(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = ["T0", "T1"]
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tool_cmds = {"T0": "lane1", "T1": "lane1"}
        gcmd = _make_gcmd(MAPPING="T1")
        spool.cmd_AFC_REMOVE_MAPPING(gcmd)
        assert "T1" not in spool.afc.tool_cmds
        assert lane1.map == ["T0"]
        lane1.send_lane_data.assert_called_once()
        # MockGcode.register_command records into _commands rather than being a Mock
        assert spool.gcode._commands.get("T1") is None
        assert "T1" in spool.gcode._commands
        spool.afc.save_vars.assert_called_once()

    def test_removing_active_mapping_falls_back_to_next_remaining_entry(self):
        """lane_obj.current_map == m -> current_map is refreshed to the
        lane's next remaining mapping."""
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = ["T0", "T1"]
        lane1.current_map = "T1"
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tool_cmds = {"T0": "lane1", "T1": "lane1"}
        gcmd = _make_gcmd(MAPPING="T1")
        spool.cmd_AFC_REMOVE_MAPPING(gcmd)
        assert lane1.current_map == "T0"

    def test_removing_active_mapping_falls_back_to_empty_when_no_mappings_remain(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = ["T0"]
        lane1.current_map = "T0"
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tool_cmds = {"T0": "lane1"}
        gcmd = _make_gcmd(MAPPING="T0")
        spool.cmd_AFC_REMOVE_MAPPING(gcmd)
        assert lane1.current_map == ""

    def test_removing_inactive_mapping_leaves_current_map_unchanged(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = ["T0", "T1"]
        lane1.current_map = "T0"
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tool_cmds = {"T0": "lane1", "T1": "lane1"}
        gcmd = _make_gcmd(MAPPING="T1")
        spool.cmd_AFC_REMOVE_MAPPING(gcmd)
        assert lane1.current_map == "T0"

    def test_removes_multiple_comma_separated_mappings(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        lane1 = _make_lane("lane1")
        lane1.map = ["T0", "T1", "T2"]
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.tool_cmds = {"T0": "lane1", "T1": "lane1", "T2": "lane1"}
        gcmd = _make_gcmd(MAPPING="T1,T2")
        spool.cmd_AFC_REMOVE_MAPPING(gcmd)
        assert "T1" not in spool.afc.tool_cmds
        assert "T2" not in spool.afc.tool_cmds
        assert lane1.map == ["T0"]

    def test_logs_error_and_skips_unregister_for_nonexistent_mapping(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        spool.afc.tool_cmds = {}
        gcmd = _make_gcmd(MAPPING="T9")
        spool.cmd_AFC_REMOVE_MAPPING(gcmd)
        spool.afc.error.AFC_error.assert_called_once_with(
            "Mapping T9 does not exist", False, gcmd=gcmd)
        assert "T9" not in spool.gcode._commands

    def test_missing_lane_object_still_unregisters_command(self):
        """tool_cmds can reference a lane name that's no longer in afc.lanes;
        the gcode command should still be torn down."""
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        spool.afc.lanes = {}
        spool.afc.tool_cmds = {"T1": "missing_lane"}
        gcmd = _make_gcmd(MAPPING="T1")
        spool.cmd_AFC_REMOVE_MAPPING(gcmd)
        assert "T1" not in spool.afc.tool_cmds
        assert spool.gcode._commands.get("T1") is None
        assert "T1" in spool.gcode._commands


# ── cmd_AFC_ENABLE_MULTIPLE_MAPPING ─────────────────────────────────────────────

class TestAfcEnableMultipleMapping:
    def test_enable_sets_flag_true(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = False
        gcmd = _make_gcmd(ENABLE=1)
        spool.cmd_AFC_ENABLE_MULTIPLE_MAPPING(gcmd)
        assert spool.enable_multiple_mapping is True
        spool.function.ConfigRewrite.assert_called_once_with(
            "AFC", "enable_multiple_mapping", True
        )

    def test_disable_sets_flag_false(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        gcmd = _make_gcmd(ENABLE=0)
        spool.cmd_AFC_ENABLE_MULTIPLE_MAPPING(gcmd)
        assert spool.enable_multiple_mapping is False
        spool.function.ConfigRewrite.assert_called_once_with(
            "AFC", "enable_multiple_mapping", False
        )

    def test_default_enable_value_is_disable(self):
        """Covers ENABLE defaulting to 0 when the param is omitted."""
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        gcmd = _make_gcmd()
        spool.cmd_AFC_ENABLE_MULTIPLE_MAPPING(gcmd)
        assert spool.enable_multiple_mapping is False

    def test_disable_resets_mapping(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = True
        spool._reset_mapping = MagicMock()
        gcmd = _make_gcmd(ENABLE=0)
        spool.cmd_AFC_ENABLE_MULTIPLE_MAPPING(gcmd)
        spool._reset_mapping.assert_called_once_with()

    def test_enable_does_not_reset_mapping(self):
        spool = _make_spool()
        spool.enable_multiple_mapping = False
        spool._reset_mapping = MagicMock()
        gcmd = _make_gcmd(ENABLE=1)
        spool.cmd_AFC_ENABLE_MULTIPLE_MAPPING(gcmd)
        spool._reset_mapping.assert_not_called()


# ── register_commands ─────────────────────────────────────────────────────────

def _make_spool_for_register_commands(show_macros=True):
    """Build an AFCSpool through the real __init__, ready for register_commands()."""
    from tests.conftest import MockAFC, MockConfig, MockPrinter

    afc = MockAFC()
    afc.show_macros = show_macros
    printer = MockPrinter(afc=afc)
    config = MockConfig(name="AFC_Spool", printer=printer)
    spool = AFCSpool(config)
    return spool, afc


class TestRegisterCommands:
    def test_stores_afc_function_and_enable_multiple_mapping(self):
        spool, afc = _make_spool_for_register_commands()
        afc.enable_multiple_mapping = True
        spool.register_commands(afc)
        assert spool.afc is afc
        assert spool.function is afc.function
        assert spool.enable_multiple_mapping is True

    def test_afc_swap_mapping_registered_without_show_macros(self):
        """AFC_SWAP_MAPPING is an internal command invoked by
        _perform_infinite_runout, not a user-facing macro, so it must always
        register with show_macros=False regardless of afc.show_macros."""
        spool, afc = _make_spool_for_register_commands(show_macros=True)
        spool.register_commands(afc)
        calls = afc.function.register_commands.call_args_list
        swap_call = next(c for c in calls if c.args[1] == "AFC_SWAP_MAPPING")
        assert swap_call.args[0] is False

    def test_add_and_remove_mapping_use_show_macros_true(self):
        spool, afc = _make_spool_for_register_commands(show_macros=True)
        spool.register_commands(afc)
        calls = afc.function.register_commands.call_args_list
        names = {c.args[1]: c.args[0] for c in calls}
        assert names["AFC_ADD_MAPPING"] is True
        assert names["AFC_REMOVE_MAPPING"] is True
        assert names["AFC_ENABLE_MULTIPLE_MAPPING"] is True

    def test_add_and_remove_mapping_use_show_macros_false(self):
        """Proves afc.show_macros is actually threaded through rather than
        hardcoded, by driving it to the opposite value from the true case."""
        spool, afc = _make_spool_for_register_commands(show_macros=False)
        spool.register_commands(afc)
        calls = afc.function.register_commands.call_args_list
        names = {c.args[1]: c.args[0] for c in calls}
        assert names["AFC_ADD_MAPPING"] is False
        assert names["AFC_REMOVE_MAPPING"] is False
        assert names["AFC_ENABLE_MULTIPLE_MAPPING"] is False


# ── cmd_SET_COLOR ──────────────────────────────────────────────────────────────

class TestSetColor:
    def _run(self, **params):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        spool.set_snapmaker_filament_params = MagicMock()
        spool.cmd_SET_COLOR(_make_gcmd(LANE="lane1", **params))
        self.spool = spool
        return lane

    def test_colors_set_multi_color(self):
        lane = self._run(COLOR="#FFD700", COLORS="#FFD700,#C0C0C0", COLOR_MODE="COAXIAL")
        assert lane.color == "#FFD700"
        assert lane.multi_color == ["FFD700", "C0C0C0"]
        assert lane.color_mode == "coaxial"

    def test_colors_without_color_use_first_entry(self):
        lane = self._run(COLORS="AABBCC, 001122")
        assert lane.color == "#AABBCC"
        assert lane.multi_color == ["AABBCC", "001122"]
        assert lane.color_mode == "gradient"

    def test_explicit_color_kept_with_colors(self):
        lane = self._run(COLOR="112233", COLORS="AABBCC,001122")
        assert lane.color == "#112233"
        assert lane.multi_color == ["AABBCC", "001122"]
        assert lane.color_mode == "gradient"

    def test_unknown_color_mode_falls_back_to_gradient(self):
        lane = self._run(COLORS="AABBCC,001122", COLOR_MODE="striped")
        assert lane.color_mode == "gradient"
        assert self.spool.logger.messages == [("info", "Unknown COLOR_MODE striped, using gradient")]

    def test_single_entry_colors_is_one_color(self):
        lane = self._run(COLORS="AABBCC")
        assert lane.color == "#AABBCC"
        assert lane.multi_color == []
        assert lane.color_mode == ""

    def test_color_alone_returns_to_one_color(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        lane.multi_color = ["AABBCC", "001122"]
        lane.color_mode = "coaxial"
        spool.afc.lanes = {"lane1": lane}
        spool.set_snapmaker_filament_params = MagicMock()
        spool.cmd_SET_COLOR(_make_gcmd(LANE="lane1", COLOR="00FF00"))
        assert lane.color == "#00FF00"
        assert lane.multi_color == []
        assert lane.color_mode == ""

    def test_set_color_updates_lane_color(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        spool.set_snapmaker_filament_params = MagicMock()
        gcmd = _make_gcmd(LANE="lane1", COLOR="#FF0000")
        spool.cmd_SET_COLOR(gcmd)
        assert lane.color == "#FF0000"
        spool.set_snapmaker_filament_params.assert_called_once()
    
    def test_set_led(self):
        spool = _make_spool()
        spool.set_snapmaker_filament_params = MagicMock()
        lane = _make_lane("lane1")
        lane.unit = "test"
        spool.afc.units[lane.unit] = lane.unit_obj
        lane.load_state = True
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1", COLOR="#FF0000")
        spool.cmd_SET_COLOR(gcmd)
        lane.unit_obj.lane_loaded.assert_called_once_with(lane, force=True, gcmd=gcmd)
        

    def test_not_set_led_lane_loaded_not_lane_in_unit(self):
        spool = _make_spool()
        spool.set_snapmaker_filament_params = MagicMock()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        lane.unit = "test"
        lane.load_state = True
        gcmd = _make_gcmd(LANE="lane1", COLOR="#FF0000")
        spool.cmd_SET_COLOR(gcmd)
        spool.afc.function.afc_led.assert_not_called()

    def test_not_set_led_not_lane_loaded_lane_in_unit(self):
        spool = _make_spool()
        spool.set_snapmaker_filament_params = MagicMock()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        lane.unit = "test"
        spool.afc.units[lane.unit] = lane.unit_obj
        lane.load_state = False
        gcmd = _make_gcmd(LANE="lane1", COLOR="#FF0000")
        spool.cmd_SET_COLOR(gcmd)
        spool.afc.function.afc_led.assert_not_called()

    def test_set_color_saves_vars(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        spool.afc.save_vars = MagicMock()
        gcmd = _make_gcmd(LANE="lane1", COLOR="red")
        spool.cmd_SET_COLOR(gcmd)
        spool.afc.save_vars.assert_called()

    def test_no_lane_param_logs_info(self):
        """lane is None → log info + return."""
        spool = _make_spool()
        gcmd = _make_gcmd()  # no LANE key
        spool.cmd_SET_COLOR(gcmd)
        assert spool.logger.messages == [("info", "No LANE Defined")]

    def test_lane_not_in_lanes_logs_info(self):
        """lane not in lanes dict → log info + return."""
        spool = _make_spool()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="ghost", COLOR="FF0000")
        spool.cmd_SET_COLOR(gcmd)
        assert spool.logger.messages == [("info", "ghost Unknown")]


# ── cmd_SET_MATERIAL ───────────────────────────────────────────────────────────

class TestSetMaterial:
    def test_set_material_updates_lane_material(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        spool.set_snapmaker_filament_params = MagicMock()
        gcmd = _make_gcmd(LANE="lane1", MATERIAL="PLA")
        spool.cmd_SET_MATERIAL(gcmd)
        assert lane.material == "PLA"
        spool.set_snapmaker_filament_params.assert_called_once()

    def test_set_material_saves_vars(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        spool.afc.save_vars = MagicMock()
        gcmd = _make_gcmd(LANE="lane1", MATERIAL="ABS")
        spool.cmd_SET_MATERIAL(gcmd)
        spool.afc.save_vars.assert_called()

    def test_no_lane_param_logs_info(self):
        """lane is None → log info + return."""
        spool = _make_spool()
        gcmd = _make_gcmd()  # no LANE key
        spool.cmd_SET_MATERIAL(gcmd)
        assert spool.logger.messages == [("info", "No LANE Defined")]

    def test_lane_not_in_lanes_logs_info(self):
        """lane not in lanes → log info + return."""
        spool = _make_spool()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="ghost", MATERIAL="PLA")
        spool.cmd_SET_MATERIAL(gcmd)
        assert spool.logger.messages == [("info", "ghost Unknown")]

    def test_with_density_sets_filament_density(self):
        """density is not None → sets cur_lane.filament_density."""
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1", MATERIAL="PLA", DENSITY=1.24)
        spool.cmd_SET_MATERIAL(gcmd)
        assert lane.filament_density == 1.24


# ── cmd_SET_FILAMENT_INFO ──────────────────────────────────────────────────────

class TestSetFilamentInfo:
    def _setup(self, vendor="", name=""):
        spool = _make_spool()
        lane = _make_lane("lane1")
        lane.spool_vendor = vendor
        lane.filament_name = name
        spool.afc.lanes = {"lane1": lane}
        spool.afc.save_vars = MagicMock()
        spool.set_snapmaker_filament_params = MagicMock()
        return spool, lane

    def test_sets_vendor_and_name(self):
        spool, lane = self._setup()
        gcmd = _make_gcmd(LANE="lane1", VENDOR="Polymaker", NAME="PolyTerra PLA")
        spool.cmd_SET_FILAMENT_INFO(gcmd)
        assert lane.spool_vendor == "Polymaker"
        assert lane.filament_name == "PolyTerra PLA"
        assert spool.logger.messages == []

    def test_sends_lane_data_saves_vars_and_updates_snapmaker(self):
        spool, lane = self._setup()
        gcmd = _make_gcmd(LANE="lane1", VENDOR="Elegoo", NAME="PLA")
        spool.cmd_SET_FILAMENT_INFO(gcmd)
        lane.send_lane_data.assert_called_once_with()
        spool.afc.save_vars.assert_called_once_with()
        spool.set_snapmaker_filament_params.assert_called_once_with(lane)

    def test_vendor_only_keeps_existing_name(self):
        spool, lane = self._setup(vendor="Old", name="Keep Me")
        gcmd = _make_gcmd(LANE="lane1", VENDOR="Elegoo")
        spool.cmd_SET_FILAMENT_INFO(gcmd)
        assert lane.spool_vendor == "Elegoo"
        assert lane.filament_name == "Keep Me"

    def test_name_only_keeps_existing_vendor(self):
        spool, lane = self._setup(vendor="Keep Me", name="Old")
        gcmd = _make_gcmd(LANE="lane1", NAME="PLA+")
        spool.cmd_SET_FILAMENT_INFO(gcmd)
        assert lane.spool_vendor == "Keep Me"
        assert lane.filament_name == "PLA+"

    def test_empty_values_clear_fields(self):
        spool, lane = self._setup(vendor="Old", name="Old Name")
        gcmd = _make_gcmd(LANE="lane1", VENDOR="", NAME="")
        spool.cmd_SET_FILAMENT_INFO(gcmd)
        assert lane.spool_vendor == ""
        assert lane.filament_name == ""

    def test_sets_filament_id(self):
        spool, lane = self._setup()
        lane.filament_id = ""
        gcmd = _make_gcmd(LANE="lane1", FILAMENT_ID="OFDSrzZ8")
        spool.cmd_SET_FILAMENT_INFO(gcmd)
        assert lane.filament_id == "OFDSrzZ8"
        assert spool.logger.messages == []

    def test_vendor_and_name_keep_existing_filament_id(self):
        spool, lane = self._setup()
        lane.filament_id = "OFDSrzZ8"
        gcmd = _make_gcmd(LANE="lane1", VENDOR="Elegoo", NAME="PLA")
        spool.cmd_SET_FILAMENT_INFO(gcmd)
        assert lane.filament_id == "OFDSrzZ8"

    def test_filament_id_only_keeps_existing_vendor_and_name(self):
        spool, lane = self._setup(vendor="Keep Me", name="Keep Me Too")
        lane.filament_id = "Old"
        gcmd = _make_gcmd(LANE="lane1", FILAMENT_ID="OFDSrzZ8")
        spool.cmd_SET_FILAMENT_INFO(gcmd)
        assert lane.spool_vendor == "Keep Me"
        assert lane.filament_name == "Keep Me Too"
        assert lane.filament_id == "OFDSrzZ8"

    def test_empty_filament_id_clears_it(self):
        spool, lane = self._setup()
        lane.filament_id = "OFDSrzZ8"
        gcmd = _make_gcmd(LANE="lane1", FILAMENT_ID="")
        spool.cmd_SET_FILAMENT_INFO(gcmd)
        assert lane.filament_id == ""

    def test_filament_id_counts_an_edit(self):
        spool, lane = self._setup()
        spool.cmd_SET_FILAMENT_INFO(_make_gcmd(LANE="lane1", FILAMENT_ID="OFDSrzZ8"))
        spool.cmd_SET_FILAMENT_INFO(_make_gcmd(LANE="lane1", FILAMENT_ID=""))
        assert spool._filament_id_edits == {"lane1": 2}

    def test_vendor_and_name_count_no_filament_id_edit(self):
        spool, lane = self._setup()
        spool.cmd_SET_FILAMENT_INFO(_make_gcmd(LANE="lane1", VENDOR="Elegoo", NAME="PLA"))
        assert spool._filament_id_edits == {}

    def test_no_lane_param_raises_gcmd_error(self):
        spool, lane = self._setup(vendor="Old", name="Old Name")
        gcmd = _make_gcmd(VENDOR="Elegoo")
        with pytest.raises(Exception):
            spool.cmd_SET_FILAMENT_INFO(gcmd)
        assert lane.spool_vendor == "Old"
        assert lane.filament_name == "Old Name"
        spool.afc.save_vars.assert_not_called()
        assert spool.logger.messages == []

    def test_lane_not_in_lanes_logs_info(self):
        spool = _make_spool()
        spool.afc.lanes = {}
        spool.afc.save_vars = MagicMock()
        gcmd = _make_gcmd(LANE="ghost", VENDOR="Elegoo", NAME="PLA")
        spool.cmd_SET_FILAMENT_INFO(gcmd)
        assert spool.logger.messages == [("info", "ghost Unknown")]
        spool.afc.save_vars.assert_not_called()


# ── cmd_SET_WEIGHT ─────────────────────────────────────────────────────────────

class TestSetWeight:
    def test_set_weight_updates_lane_weight(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1", WEIGHT=250)
        spool.cmd_SET_WEIGHT(gcmd)
        assert lane.weight == 250

    def test_calls_send_lane_data(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1", WEIGHT=250)
        spool.cmd_SET_WEIGHT(gcmd)
        lane.send_lane_data.assert_called()

    def test_set_weight_invalid_lane_logs_info(self):
        spool = _make_spool()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="missing", WEIGHT=100)
        spool.cmd_SET_WEIGHT(gcmd)
        assert spool.logger.messages == [("info", "missing Unknown")]

    def test_no_lane_param_logs_info(self):
        """lane is None → log info + return."""
        spool = _make_spool()
        gcmd = _make_gcmd()  # no LANE key
        spool.cmd_SET_WEIGHT(gcmd)
        assert spool.logger.messages == [("info", "No LANE Defined")]


# ── cmd_AFC_SET_SPOOL_TEMP ─────────────────────────────────────────────────────

class TestAFCSetSpoolTemp:
    def test_sets_bed_and_extruder_temp(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1", BED_TEMP=70, EXTRUDER_TEMP=230)
        spool.cmd_AFC_SET_SPOOL_TEMP(gcmd)
        assert lane.bed_temp == 70
        assert lane.extruder_temp == 230

    def test_keeps_existing_temps_when_not_provided(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        lane.bed_temp = 60
        lane.extruder_temp = 210
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1")
        spool.cmd_AFC_SET_SPOOL_TEMP(gcmd)
        assert lane.bed_temp == 60
        assert lane.extruder_temp == 210

    def test_saves_vars(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        spool.afc.save_vars = MagicMock()
        gcmd = _make_gcmd(LANE="lane1", BED_TEMP=60, EXTRUDER_TEMP=210)
        spool.cmd_AFC_SET_SPOOL_TEMP(gcmd)
        spool.afc.save_vars.assert_called()

    def test_calls_send_lane_data(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane}
        gcmd = _make_gcmd(LANE="lane1", BED_TEMP=60, EXTRUDER_TEMP=210)
        spool.cmd_AFC_SET_SPOOL_TEMP(gcmd)
        lane.send_lane_data.assert_called()

    def test_no_lane_param_logs_info(self):
        spool = _make_spool()
        gcmd = _make_gcmd()
        spool.cmd_AFC_SET_SPOOL_TEMP(gcmd)
        assert spool.logger.messages == [
            ("info", "No LANE parameter provided, please specify a valid LANE parameter.")
        ]

    def test_unknown_lane_logs_info(self):
        spool = _make_spool()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="ghost", BED_TEMP=60, EXTRUDER_TEMP=210)
        spool.cmd_AFC_SET_SPOOL_TEMP(gcmd)
        assert spool.logger.messages == [("info", "ghost Unknown")]


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
        """lane is None → log info + return."""
        spool = _make_spool()
        gcmd = _make_gcmd()  # no LANE key
        spool.cmd_SET_RUNOUT(gcmd)
        assert spool.logger.messages == [("info", "No LANE Defined")]

    def test_lane_equals_runout_logs_error(self):
        """lane == runout → log error + return."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane1}
        gcmd = _make_gcmd(LANE="lane1", RUNOUT="lane1")
        spool.cmd_SET_RUNOUT(gcmd)
        assert spool.logger.messages == [
            ("error", "Lane(lane1) and runout(lane1) cannot be the same")
        ]

    def test_lane_not_in_lanes_logs_error(self):
        """lane not in lanes → log error + return."""
        spool = _make_spool()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="ghost", RUNOUT="lane2")
        spool.cmd_SET_RUNOUT(gcmd)
        assert spool.logger.messages == [("error", "Unknown lane: ghost")]

    def test_set_runout_none_clears_runout_lane(self):
        """RUNOUT='NONE' (uppercase) → runout_lane set to None."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.runout_lane = "lane2"
        spool.afc.lanes = {"lane1": lane1}
        gcmd = _make_gcmd(LANE="lane1", RUNOUT="NONE")
        spool.cmd_SET_RUNOUT(gcmd)
        assert lane1.runout_lane is None

    def test_set_runout_lowercase_none_clears_runout_lane(self):
        """RUNOUT='none' (lowercase) → runout_lane set to None."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.runout_lane = "lane2"
        spool.afc.lanes = {"lane1": lane1}
        gcmd = _make_gcmd(LANE="lane1", RUNOUT="none")
        spool.cmd_SET_RUNOUT(gcmd)
        assert lane1.runout_lane is None

    def test_set_runout_mixedcase_none_clears_runout_lane(self):
        """RUNOUT='None' (mixed case) → runout_lane set to None."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        lane1.runout_lane = "lane2"
        spool.afc.lanes = {"lane1": lane1}
        gcmd = _make_gcmd(LANE="lane1", RUNOUT="None")
        spool.cmd_SET_RUNOUT(gcmd)
        assert lane1.runout_lane is None

    def test_runout_not_in_lanes_logs_error(self):
        """runout != 'NONE' but not in lanes → log error + return."""
        spool = _make_spool()
        lane1 = _make_lane("lane1")
        spool.afc.lanes = {"lane1": lane1}
        gcmd = _make_gcmd(LANE="lane1", RUNOUT="no_such_lane")
        spool.cmd_SET_RUNOUT(gcmd)
        assert spool.logger.messages == [("error", "Unknown runout lane: no_such_lane")]


# ── cmd_AFC_RESET_MAPPING ─────────────────────────────────────────────────────

class TestResetAFCMapping:
    def _make_reset_gcmd(self, runout="yes"):
        return _make_gcmd(RUNOUT=runout)

    def _make_lane_for_reset(self, name, map_cmd="T0"):
        """Lane mock with explicit _map=[] (not manually assigned) and map=[map_cmd]."""
        lane = _make_lane(name)
        lane.map = [map_cmd]
        lane._map = []  # not manually assigned
        lane.runout_lane = None
        return lane

    def test_reset_saves_vars(self):
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.units = {}  # no units, loop skips mapping reassignment
        gcmd = self._make_reset_gcmd()
        spool.cmd_AFC_RESET_MAPPING(gcmd)
        spool.afc.save_vars.assert_called()

    def test_reset_clears_runout_lanes(self):
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        lane1.runout_lane = "lane2"
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.units = {}
        gcmd = self._make_reset_gcmd(runout="yes")
        spool.cmd_AFC_RESET_MAPPING(gcmd)
        assert lane1.runout_lane is None

    def test_reset_skips_runout_when_no(self):
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        lane1.runout_lane = "lane2"
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.units = {}
        gcmd = self._make_reset_gcmd(runout="no")
        spool.cmd_AFC_RESET_MAPPING(gcmd)
        assert lane1.runout_lane == "lane2"  # not cleared

    def test_reset_reassigns_map_without_manual_mapping(self):
        """lane._map=[] → pops from existing_cmds."""
        spool = _make_spool()
        # Set up a lane in afc.lanes used to build existing_cmds
        lane1 = self._make_lane_for_reset("lane1", "T0")
        spool.afc.lanes = {"lane1": lane1}
        # Unit whose lane also has _map=[] (else branch)
        unit_lane = MagicMock()
        unit_lane.name = "lane1"
        unit_lane._map = []
        unit = MagicMock()
        unit.lanes = {"lane1": unit_lane}
        spool.afc.units = {"unit_1": unit}
        gcmd = self._make_reset_gcmd()
        spool.cmd_AFC_RESET_MAPPING(gcmd)
        # tool_cmds and the unit's lane.current_map must have been updated
        assert spool.afc.tool_cmds.get("T0") == "lane1"
        assert unit_lane.current_map == "T0"

    def test_reset_excludes_none_placeholder_from_existing_cmds(self):
        """"NONE" entries in lane.map must be filtered out by value, not
        object identity -- lane.map values loaded from config/saved vars are
        never the same string object as a "NONE" literal in the source, so
        an identity check silently fails to filter them. Left unfiltered,
        "NONE" also has no digit chars, so the numeric sort right after would
        raise ValueError -- this call must complete without raising."""
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        none_placeholder = "".join(["N", "O", "N", "E"])
        lane2 = self._make_lane_for_reset("lane2", none_placeholder)
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        spool.afc.units = {}  # isolates existing_cmds construction/sort from the reassignment loop
        gcmd = self._make_reset_gcmd()
        spool.cmd_AFC_RESET_MAPPING(gcmd)
        info_msgs = [m for lvl, m in spool.logger.messages if lvl == "info"]
        # "T0" is never consumed since afc.units is empty, so it also shows
        # up in the leftover-mappings cleanup message, which now logs before
        # the final "reset" message since it must run before the
        # send_lane_data() pass for tool_cmds to be fully consistent.
        assert info_msgs == [
            "T0 remain, removing these mappings",
            "Tool mappings reset and runout lanes reset",
        ]

    def test_reset_uses_manual_map_when_set(self):
        """lane._map is not empty → uses it directly."""
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        lane1._map = ["T0"]  # manually assigned
        spool.afc.lanes = {"lane1": lane1}
        unit_lane = MagicMock()
        unit_lane.name = "lane1"
        unit_lane._map = ["T0"]  # manual assignment
        unit = MagicMock()
        unit.lanes = {"lane1": unit_lane}
        spool.afc.units = {"unit_1": unit}
        gcmd = self._make_reset_gcmd()
        spool.cmd_AFC_RESET_MAPPING(gcmd)
        # The manually assigned T0 should be applied
        assert spool.afc.tool_cmds.get("T0") == "lane1"

    def test_reset_renumbers_sequentially_after_unit_removed(self):
        """Simulates a unit having been removed: the 2 remaining lanes still
        carry their old (now-gapped) T4/T5 numbers from when more lanes
        existed. Reset must renumber them down to a clean T0/T1 sequence
        instead of recycling the old high numbers, and unregister the old
        numbers since no lane claims them anymore."""
        spool = _make_spool()
        lane3 = self._make_lane_for_reset("lane3", "T4")
        lane4 = self._make_lane_for_reset("lane4", "T5")
        spool.afc.lanes = {"lane3": lane3, "lane4": lane4}
        unit = MagicMock()
        unit.lanes = {"lane3": lane3, "lane4": lane4}
        spool.afc.units = {"unit2": unit}
        gcmd = self._make_reset_gcmd()
        spool.cmd_AFC_RESET_MAPPING(gcmd)
        assert spool.afc.tool_cmds.get("T0") == "lane3"
        assert spool.afc.tool_cmds.get("T1") == "lane4"
        assert lane3.current_map == "T0"
        assert lane4.current_map == "T1"
        assert spool.gcode._commands.get("T4") is None and "T4" in spool.gcode._commands
        assert spool.gcode._commands.get("T5") is None and "T5" in spool.gcode._commands
        assert "T4" not in spool.afc.tool_cmds
        assert "T5" not in spool.afc.tool_cmds

    def test_reset_registers_newly_assigned_auto_command(self):
        """A unit was added, so this lane's auto-assigned number (T1) was
        never in use before and has no gcode handler registered yet."""
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        lane2 = self._make_lane_for_reset("lane2", "NONE")
        lane2.map = []
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2}
        unit = MagicMock()
        unit.lanes = {"lane1": lane1, "lane2": lane2}
        spool.afc.units = {"unit1": unit}
        gcmd = self._make_reset_gcmd()
        spool.cmd_AFC_RESET_MAPPING(gcmd)
        assert spool.afc.tool_cmds.get("T1") == "lane2"
        spool.function.register_tool_macro.assert_called_once_with("lane2", "T1")

    def test_reset_does_not_reregister_command_already_in_use(self):
        """Covers the `map_cmd not in previously_used_cmds` condition being
        falsy on its own: a command that already existed before the reset
        (recycled onto the same or a different lane) must not be
        re-registered -- proven independently of the newly-added case above."""
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        spool.afc.lanes = {"lane1": lane1}
        unit = MagicMock()
        unit.lanes = {"lane1": lane1}
        spool.afc.units = {"unit1": unit}
        gcmd = self._make_reset_gcmd()
        spool.cmd_AFC_RESET_MAPPING(gcmd)
        spool.function.register_tool_macro.assert_not_called()

    def test_reset_registers_newly_manually_assigned_command(self):
        """A manual mapping (config `map:` value) that's never been used
        before also needs registering, not just auto-assigned ones."""
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "NONE")
        lane1.map = []
        lane1._map = ["T7"]
        spool.afc.lanes = {"lane1": lane1}
        unit = MagicMock()
        unit.lanes = {"lane1": lane1}
        spool.afc.units = {"unit1": unit}
        gcmd = self._make_reset_gcmd()
        spool.cmd_AFC_RESET_MAPPING(gcmd)
        assert spool.afc.tool_cmds.get("T7") == "lane1"
        spool.function.register_tool_macro.assert_called_once_with("lane1", "T7")

    def test_reset_skips_manually_assigned_number_for_auto_lanes(self):
        """A manual mapping in the middle of the sequence (T1) must not be
        reused by an auto-assigned lane -- the auto lanes get T0 and T2."""
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        lane2 = self._make_lane_for_reset("lane2", "T1")
        lane2._map = ["T1"]  # manually assigned
        lane3 = self._make_lane_for_reset("lane3", "T2")
        spool.afc.lanes = {"lane1": lane1, "lane2": lane2, "lane3": lane3}
        unit = MagicMock()
        unit.lanes = {"lane1": lane1, "lane2": lane2, "lane3": lane3}
        spool.afc.units = {"unit1": unit}
        gcmd = self._make_reset_gcmd()
        spool.cmd_AFC_RESET_MAPPING(gcmd)
        assert spool.afc.tool_cmds.get("T0") == "lane1"
        assert spool.afc.tool_cmds.get("T1") == "lane2"
        assert spool.afc.tool_cmds.get("T2") == "lane3"

    def test_reset_removes_multimapping_virtual_tool_extras(self):
        """A lane with 2 T(n)'s mapped (multimapping) collapses back to 1 on
        reset; the extra T(n) no longer assigned to any lane is removed."""
        spool = _make_spool()
        lane1 = self._make_lane_for_reset("lane1", "T0")
        lane1.map = ["T0", "T5"]  # T5 is a virtual-tool extra
        spool.afc.lanes = {"lane1": lane1}
        unit = MagicMock()
        unit.lanes = {"lane1": lane1}
        spool.afc.units = {"unit1": unit}
        gcmd = self._make_reset_gcmd()
        spool.cmd_AFC_RESET_MAPPING(gcmd)
        assert lane1.map == ["T0"]
        assert spool.gcode._commands.get("T5") is None and "T5" in spool.gcode._commands
        assert "T5" not in spool.afc.tool_cmds


# ── cmd_SET_NEXT_SPOOL_ID ─────────────────────────────────────────────────────

class TestSetNextSpoolId:
    def test_stores_next_spool_id(self):
        spool = _make_spool()
        gcmd = _make_gcmd(SPOOL_ID=42)
        spool.cmd_SET_NEXT_SPOOL_ID(gcmd)
        assert spool.next_spool_id == 42

    def test_invalid_spool_id_logs_error_and_clears(self):
        """ValueError → log error, next_spool_id stays None."""
        spool = _make_spool()
        gcmd = _make_gcmd(SPOOL_ID="not_a_number")
        spool.cmd_SET_NEXT_SPOOL_ID(gcmd)
        assert spool.logger.messages == [
            ("error", "Invalid spool ID: not_a_number"),
            ("info", "Spool ID set for next load: 'None'"),
        ]
        assert spool.next_spool_id is None

    def test_empty_spool_id_clears_next(self):
        """SpoolID='' → next_spool_id set to None."""
        spool = _make_spool()
        spool.next_spool_id = 99
        gcmd = _make_gcmd()  # no SPOOL_ID key → default ''
        spool.cmd_SET_NEXT_SPOOL_ID(gcmd)
        assert spool.next_spool_id is None

    def test_overwrites_existing_spool_id_logs_overwrite(self):
        """previous_id is set → log overwrite info message."""
        spool = _make_spool()
        spool.next_spool_id = 10  # existing
        gcmd = _make_gcmd(SPOOL_ID=20)
        spool.cmd_SET_NEXT_SPOOL_ID(gcmd)
        assert spool.next_spool_id == 20
        assert spool.logger.messages == [
            ("info", "Spool ID '10' being overwritten for next load: '20'")
        ]


# ── handle_connect / register_lane_macros ─────────────────────────────────────

class TestHandleConnect:
    def test_handle_connect_stores_afc_references(self):
        from tests.conftest import MockAFC, MockConfig, MockPrinter
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_Spool", printer=printer)
        spool = AFCSpool(config)
        # register_commands() is what normally sets self.afc, ahead of the
        # klippy:connect event that triggers handle_connect()
        spool.register_commands(afc)
        spool.handle_connect()
        assert spool.afc is afc
        assert spool.gcode is afc.gcode
        assert spool.logger is afc.logger

    def test_handle_connect_registers_commands(self):
        from tests.conftest import MockAFC, MockConfig, MockPrinter
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_Spool", printer=printer)
        spool = AFCSpool(config)
        spool.register_commands(afc)
        spool.handle_connect()
        assert "AFC_RESET_MAPPING" in afc.gcode._commands
        assert "SET_NEXT_SPOOL_ID" in afc.gcode._commands

    def test_handle_connect_registers_print_stats_stop_handler(self):
        from tests.conftest import MockAFC, MockConfig, MockPrinter
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_Spool", printer=printer)
        spool = AFCSpool(config)
        spool.register_commands(afc)
        spool.handle_connect()
        assert printer._event_handlers["print_stats:stop"] == [
            spool.snapmaker_sync_cb]


class TestRegisterLaneMacros:
    def test_registers_eight_mux_commands(self):
        spool = _make_spool()
        spool.gcode.register_mux_command = MagicMock()
        lane = _make_lane("lane1")
        spool.register_lane_macros(lane)
        assert spool.gcode.register_mux_command.call_count == 8

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

    def test_calls_webhook_catch_exception(self):
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        spool.printer.lookup_object = MagicMock(return_value=None)
        spool.set_active_spool(7)
        assert spool.logger.messages == [
            (
                "error",
                "Error trying to set active spool \n"
                "'NoneType' object has no attribute 'call_remote_method'",
            )
        ]


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

    def test_clears_filament_id(self):
        spool = _make_spool()
        lane = _make_lane()
        lane.filament_id = "OFDSrzZ8"
        lane.clear_lane_data = MagicMock()
        spool.clear_values(lane)
        assert lane.filament_id == ""


class TestGetSpoolmanExtra:
    def test_json_string_is_decoded(self):
        spool = _make_spool()
        record = {"extra": {"orca_filament_id": '"OFDSrzZ8"'}}
        assert spool._get_spoolman_extra(record, "orca_filament_id") == "OFDSrzZ8"

    def test_text_that_is_not_json_is_returned_as_is(self):
        spool = _make_spool()
        record = {"extra": {"orca_filament_id": "OFDSrzZ8"}}
        assert spool._get_spoolman_extra(record, "orca_filament_id") == "OFDSrzZ8"

    def test_json_non_string_is_empty(self):
        spool = _make_spool()
        record = {"extra": {"orca_filament_id": "42"}}
        assert spool._get_spoolman_extra(record, "orca_filament_id") == ""

    @pytest.mark.parametrize("record", [{}, {"extra": None}, {"extra": {"orca_filament_id": ""}}])
    def test_unset_field_is_empty(self, record):
        spool = _make_spool()
        assert spool._get_spoolman_extra(record, "orca_filament_id") == ""


# ── _set_values ───────────────────────────────────────────────────────────────

class TestSetValues:
    def test_sets_defaults_when_not_remember_spool(self):
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        spool.afc.default_material_type = "PLA"
        spool.set_spoolID = MagicMock()
        spool._set_values(lane, gcmd=gcmd)
        assert lane.material == "PLA"
        assert lane.weight == 1000
        spool.set_spoolID.assert_not_called()

    def test_calls_set_spool_id_when_next_spool_id_set(self):
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        spool.afc.default_material_type = "PLA"
        spool.afc.spoolman = MagicMock()
        spool.next_spool_id = 42
        spool.set_spoolID = MagicMock()
        spool._set_values(lane, gcmd=gcmd)
        spool.set_spoolID.assert_called_once_with(lane, 42, on_done=None, gcmd=gcmd)
        assert spool.next_spool_id is None  # consumed after use

    def test_set_value_material_not_set_remember_spool(self):
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = True
        lane.spool_id = 42
        lane.color = "123456"
        spool.afc.default_material_type = "PLA"
        spool.afc.spoolman = MagicMock()
        spool.next_spool_id = 42
        spool.set_spoolID = MagicMock()
        spool._set_values(lane, gcmd=gcmd)
        assert lane.material == ""
        assert lane.weight == 0

    def test_set_value_material_not_set_no_spool_id(self):
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        lane.color = "123456"
        spool.afc.default_material_type = "PLA"
        spool.afc.spoolman = MagicMock()
        spool.set_spoolID = MagicMock()
        spool._set_values(lane, gcmd=gcmd)
        assert lane.material == ""
        assert lane.weight == 0
    
    def test_set_value_material_not_set_no_color(self):
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        lane.spool_id = 42
        lane.color = ""
        spool.afc.default_material_type = "PLA"
        spool.afc.spoolman = MagicMock()
        spool.next_spool_id = 42
        spool.set_spoolID = MagicMock()
        spool._set_values(lane, gcmd=gcmd)
        assert lane.material == ""
        assert lane.weight == 0

    def test_on_done_forwarded_to_set_spool_id_when_next_spool_id_set(self):
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        spool.afc.spoolman = MagicMock()
        spool.next_spool_id = 42
        spool.set_spoolID = MagicMock()
        on_done = MagicMock()
        spool._set_values(lane, on_done=on_done, gcmd=gcmd)
        spool.set_spoolID.assert_called_once_with(lane, 42, on_done=on_done, gcmd=gcmd)
        on_done.assert_not_called()  # only set_spoolID's async fetch may call it

    def test_on_done_called_immediately_when_no_pending_spool_id(self):
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        spool.afc.spoolman = MagicMock()
        spool.next_spool_id = None
        spool.set_spoolID = MagicMock()
        on_done = MagicMock()
        spool._set_values(lane, on_done=on_done, gcmd=gcmd)
        spool.set_spoolID.assert_not_called()
        on_done.assert_called_once()

    def test_on_done_not_required(self):
        """Existing callers that don't pass on_done keep working unchanged."""
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        spool.afc.spoolman = MagicMock()
        spool.next_spool_id = None
        spool.set_spoolID = MagicMock()
        spool._set_values(lane, gcmd=gcmd)  # must not raise


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


def _stub_get_spool(spool, result):
    """Makes afc.moonraker.get_spool(id, callback) invoke callback
    immediately (as if the async fetch completed inline) with `result`."""
    spool.afc.moonraker.get_spool = MagicMock(
        side_effect=lambda id, callback: callback(result))


class TestSetSpoolID:
    def _make_spool_with_spoolman(self):
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        return spool

    def test_no_spoolman_not_remember_spool_clears_values(self):
        """Covers the `spoolman is not None` half of the combined condition
        being falsy on its own -- _make_spool()'s afc.moonraker defaults to
        set, so this proves moonraker alone isn't enough either."""
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, "", gcmd=gcmd)
        spool.clear_values.assert_called_once_with(lane)
    
    def test_no_spool_remember_spool_clear_spoolid(self):
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = True
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, "", gcmd=gcmd)
        spool.clear_values.assert_not_called()

    def test_spoolman_set_but_no_moonraker_clears_values(self):
        """Covers the `moonraker is not None` half of the combined condition
        being falsy on its own, with spoolman still set -- proves spoolman
        alone isn't enough to enter the lookup branch."""
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        spool.afc.moonraker = None
        lane = _make_lane()
        lane.remember_spool = False
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, "42", gcmd=gcmd)
        spool.clear_values.assert_called_once_with(lane)

    def test_valid_spool_id_sets_lane_attributes(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        spool.set_snapmaker_filament_params = MagicMock()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        assert lane.spool_id == 42
        assert lane.material == "PLA"
        assert lane.color == "#FF0000"
        assert lane.spool_vendor == ""
        spool.set_snapmaker_filament_params.assert_called_once()
        spool.afc.function.afc_led.assert_not_called()

    def test_valid_spool_id_sets_vendor(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        spool.set_snapmaker_filament_params = MagicMock()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        result["filament"]["vendor"] = {}
        result["filament"]["vendor"]["name"] = "Polymaker"
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        assert lane.spool_id == 42
        assert lane.material == "PLA"
        assert lane.color == "#FF0000"
        assert lane.spool_vendor == "Polymaker"
        spool.set_snapmaker_filament_params.assert_called_once()
        spool.afc.function.afc_led.assert_not_called()

    def test_spool_extra_field_does_not_set_filament_id(self):
        spool = self._make_spool_with_spoolman()
        spool.set_snapmaker_filament_params = MagicMock()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        result["extra"] = {"orca_filament_id": '"OFHmPYRy"'}
        result["filament"]["extra"] = {"orca_filament_id": '"OFDSrzZ8"'}
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=_make_gcmd())
        assert lane.filament_id == "OFDSrzZ8"

    def test_filament_extra_field_sets_filament_id(self):
        spool = self._make_spool_with_spoolman()
        spool.set_snapmaker_filament_params = MagicMock()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        result["filament"]["extra"] = {"orca_filament_id": '"OFDSrzZ8"'}
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=_make_gcmd())
        assert lane.filament_id == "OFDSrzZ8"

    def test_spool_without_extra_field_keeps_filament_id(self):
        spool = self._make_spool_with_spoolman()
        spool.set_snapmaker_filament_params = MagicMock()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        lane.filament_id = "GFL03"
        _stub_get_spool(spool, _make_spool_result())
        spool.set_spoolID(lane, 42, gcmd=_make_gcmd())
        assert lane.filament_id == "GFL03"

    def _filament_id_lane(self, spool_id, filament_id="OFDSrzZ8"):
        spool = self._make_spool_with_spoolman()
        spool.set_snapmaker_filament_params = MagicMock()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        lane.spool_id = spool_id
        lane.filament_id = filament_id
        return spool, lane

    def test_unquoted_filament_extra_sets_filament_id(self):
        spool, lane = self._filament_id_lane(None, filament_id="GFL03")
        result = _make_spool_result()
        result["filament"]["extra"] = {"orca_filament_id": "OFDSrzZ8"}
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=_make_gcmd())
        assert lane.filament_id == "OFDSrzZ8"

    def test_non_string_filament_extra_keeps_filament_id(self):
        spool, lane = self._filament_id_lane(None, filament_id="GFL03")
        result = _make_spool_result()
        result["filament"]["extra"] = {"orca_filament_id": "42"}
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=_make_gcmd())
        assert lane.filament_id == "GFL03"

    def test_different_spool_clears_filament_id(self):
        spool, lane = self._filament_id_lane(4)
        _stub_get_spool(spool, _make_spool_result())
        spool.set_spoolID(lane, 5, gcmd=_make_gcmd())
        assert lane.spool_id == 5
        assert lane.filament_id == ""

    def test_different_spool_takes_its_filament_extra(self):
        spool, lane = self._filament_id_lane(4)
        result = _make_spool_result()
        result["filament"]["extra"] = {"orca_filament_id": '"OFHmPYRy"'}
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 5, gcmd=_make_gcmd())
        assert lane.filament_id == "OFHmPYRy"

    def test_same_spool_of_another_type_keeps_filament_id(self):
        spool, lane = self._filament_id_lane(5)
        _stub_get_spool(spool, _make_spool_result())
        spool.set_spoolID(lane, "5", gcmd=_make_gcmd())
        assert lane.filament_id == "OFDSrzZ8"

    @pytest.mark.parametrize("empty_id", [None, ""])
    def test_first_spool_keeps_filament_id(self, empty_id):
        spool, lane = self._filament_id_lane(empty_id)
        _stub_get_spool(spool, _make_spool_result())
        spool.set_spoolID(lane, 5, gcmd=_make_gcmd())
        assert lane.filament_id == "OFDSrzZ8"

    def test_failed_lookup_keeps_spool_and_filament_id(self):
        spool, lane = self._filament_id_lane(4)
        _stub_get_spool(spool, None)
        spool.set_spoolID(lane, 5, gcmd=_make_gcmd())
        assert lane.spool_id == 4
        assert lane.filament_id == "OFDSrzZ8"

    def test_staged_spool_clears_filament_id(self):
        """A spool staged with next_spool_id goes through the same lookup."""
        spool, lane = self._filament_id_lane(4)
        lane.remember_spool = True
        spool.next_spool_id = 5
        _stub_get_spool(spool, _make_spool_result())
        spool._set_values(lane, gcmd=_make_gcmd())
        assert lane.spool_id == 5
        assert lane.filament_id == ""

    def _pending_lookup(self, spool):
        callbacks = []
        spool.afc.moonraker.get_spool = MagicMock(
            side_effect=lambda id, callback: callbacks.append(callback))
        return callbacks

    def test_filament_id_set_while_lookup_pending_survives_the_clear(self):
        spool, lane = self._filament_id_lane(4)
        spool.afc.lanes = {"lane1": lane}
        spool.afc.save_vars = MagicMock()
        callbacks = self._pending_lookup(spool)
        spool.set_spoolID(lane, 5, gcmd=_make_gcmd())
        spool.cmd_SET_FILAMENT_INFO(_make_gcmd(LANE="lane1", FILAMENT_ID="custom-preset"))
        callbacks[0](_make_spool_result())
        assert lane.spool_id == 5
        assert lane.filament_id == "custom-preset"

    def test_filament_id_set_while_lookup_pending_beats_the_extra(self):
        spool, lane = self._filament_id_lane(None, filament_id="")
        spool.afc.lanes = {"lane1": lane}
        spool.afc.save_vars = MagicMock()
        callbacks = self._pending_lookup(spool)
        spool.set_spoolID(lane, 5, gcmd=_make_gcmd())
        spool.cmd_SET_FILAMENT_INFO(_make_gcmd(LANE="lane1", FILAMENT_ID="custom-preset"))
        result = _make_spool_result()
        result["filament"]["extra"] = {"orca_filament_id": '"OFDSrzZ8"'}
        callbacks[0](result)
        assert lane.filament_id == "custom-preset"

    def test_filament_id_set_before_lookup_is_sent_still_clears(self):
        spool, lane = self._filament_id_lane(4)
        spool.afc.lanes = {"lane1": lane}
        spool.afc.save_vars = MagicMock()
        spool.cmd_SET_FILAMENT_INFO(_make_gcmd(LANE="lane1", FILAMENT_ID="custom-preset"))
        callbacks = self._pending_lookup(spool)
        spool.set_spoolID(lane, 5, gcmd=_make_gcmd())
        callbacks[0](_make_spool_result())
        assert lane.filament_id == ""

    def test_valid_spool_id_lane_not_loaded_lane_in_unit(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        lane.load_state = False
        lane.unit = "test"
        spool.afc.units[lane.unit] = lane.unit_obj
        result = _make_spool_result()
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        spool.afc.function.afc_led.assert_not_called()

    def test_valid_spool_id_lane_loaded_lane_in_unit(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.load_state = True
        lane.unit = "test"
        spool.afc.units[lane.unit] = lane.unit_obj
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        lane.unit_obj.lane_loaded.assert_called_once_with(lane, force=True, gcmd="callback")

    def test_valid_spool_id_lane_loaded_lane_not_in_unit(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.load_state = True
        lane.unit = "test"
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        spool.afc.function.afc_led.assert_not_called()

    def test_zero_weight_triggers_error_when_check_enabled(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result(weight=0.0)
        _stub_get_spool(spool, result)
        spool.disable_weight_check = False
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        spool.afc.error.AFC_error.assert_called()
        spool.clear_values.assert_called()

    def test_zero_weight_still_calls_snapmaker_params_save_vars_and_on_done(self):
        """The invalid-weight early return is inside _apply_spool_data's
        finally block, so it must not skip these -- same guarantee as the
        exception and success paths."""
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        spool.set_snapmaker_filament_params = MagicMock()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result(weight=0.0)
        _stub_get_spool(spool, result)
        spool.disable_weight_check = False
        on_done = MagicMock()

        spool.set_spoolID(lane, 42, on_done=on_done, gcmd=gcmd)

        spool.set_snapmaker_filament_params.assert_called_once_with(lane)
        spool.afc.save_vars.assert_called_once_with()
        on_done.assert_called_once_with()

    def test_disable_weight_check_skips_validation(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result(weight=0.0)
        _stub_get_spool(spool, result)
        spool.disable_weight_check = True
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        spool.afc.error.AFC_error.assert_not_called()

    def test_multi_color_takes_first_color(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        result["filament"]["multi_color_hexes"] = "AABBCC,001122,334455"
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        assert lane.color == "#AABBCC"
        assert lane.multi_color == result["filament"]["multi_color_hexes"].split(",")

    def test_multi_color_direction_coaxial(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        result["filament"]["multi_color_hexes"] = "FFD700,C0C0C0"
        result["filament"]["multi_color_direction"] = "coaxial"
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        assert lane.color_mode == "coaxial"
        assert spool.afc.logger.messages == [
            ("debug", "Weight remaining for SpoolID 42: 800.0")]

    def test_multi_color_direction_longitudinal_is_gradient(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        result["filament"]["multi_color_hexes"] = "FFD700,C0C0C0"
        result["filament"]["multi_color_direction"] = "longitudinal"
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        assert lane.color_mode == "gradient"
        assert spool.afc.logger.messages == [
            ("debug", "Weight remaining for SpoolID 42: 800.0")]

    def test_colorless_filament_publishes_no_colors(self):
        from tests.test_AFC_lane import _make_real_lane_for_colors
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_real_lane_for_colors()
        lane.afc.common_density_values = ["PLA:1.24"]
        lane.remember_spool = False
        lane.multi_color = ["AABBCC", "001122"]
        lane.color_mode = "coaxial"
        result = _make_spool_result(color_hex=None)
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        payload = lane.afc.moonraker.send_lane_data.call_args[0][0]
        assert payload["value"]["color"] == ""
        assert payload["value"]["colors"] == []
        assert payload["value"]["color_mode"] == ""
        assert spool.afc.logger.messages == [
            ("debug", "Weight remaining for SpoolID 42: 800.0")]

    def test_colorless_filament_leaves_lane_without_color(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        lane.multi_color = ["AABBCC", "001122"]
        lane.color_mode = "coaxial"
        result = _make_spool_result(color_hex=None)
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        assert lane.color == ""
        assert lane.multi_color == []
        assert lane.color_mode == ""
        assert spool.afc.logger.messages == [
            ("debug", "Weight remaining for SpoolID 42: 800.0")]

    def test_multi_color_is_none(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result(color_hex="AABBCC")
        result["filament"]["multi_color_hexes"] = None
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        assert lane.color == "#AABBCC"
        assert lane.multi_color == []

    def test_ignore_spoolman_material_temps_skips_extruder_temp(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        spool.afc.ignore_spoolman_material_temps = True
        lane = _make_lane()
        lane.remember_spool = False
        lane.extruder_temp = None  # explicitly set before call
        lane.espooler = MagicMock()
        result = _make_spool_result()
        _stub_get_spool(spool, result)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        # extruder_temp should NOT be overwritten when the flag is True
        assert lane.extruder_temp is None

    def test_none_result_from_get_spool_calls_afc_error(self):
        """A failed/not-found lookup delivers None to the callback (get_spool
        already logs its own "not found" message); _apply_spool_data must
        still report it via AFC_error rather than crashing."""
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        _stub_get_spool(spool, None)
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        spool.afc.error.AFC_error.assert_called_once()
        assert "42" in spool.afc.error.AFC_error.call_args[0][0]

    def test_exception_while_applying_spool_data_calls_afc_error(self):
        """An exception raised while processing a successful response (e.g.
        malformed data) must still be caught and reported, not crash."""
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        _stub_get_spool(spool, {"filament": None})  # .get() on None -> AttributeError
        spool.set_spoolID(lane, 42, gcmd=gcmd)
        spool.afc.error.AFC_error.assert_called_once()

    def test_get_spool_int_conversion_failure_calls_afc_error(self):
        """SpoolID that can't convert to int (defensive edge case; normal
        callers already validate) must be reported, not crash, and must not
        dispatch a lookup at all."""
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        spool.afc.moonraker.get_spool = MagicMock()
        spool.set_spoolID(lane, "not-a-number", gcmd=gcmd)
        spool.afc.error.AFC_error.assert_called_once()
        spool.afc.moonraker.get_spool.assert_not_called()

    def test_valid_spool_id_dispatches_async_lookup_without_blocking(self):
        """set_spoolID must not block on the lookup: with a callback that
        never fires, the lane must remain unmodified when set_spoolID returns."""
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        original_spool_id = lane.spool_id
        spool.afc.moonraker.get_spool = MagicMock()  # never invokes its callback

        spool.set_spoolID(lane, 42, gcmd=gcmd)

        spool.afc.moonraker.get_spool.assert_called_once()
        assert spool.afc.moonraker.get_spool.call_args[0][0] == 42
        assert lane.spool_id == original_spool_id
        spool.afc.save_vars.assert_not_called()

    def test_sync_clear_path_calls_on_done(self):
        """Covers the synchronous branch of set_spoolID (no moonraker lookup
        needed) still invoking on_done."""
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        on_done = MagicMock()
        spool.set_spoolID(lane, "", on_done=on_done, gcmd=gcmd)
        on_done.assert_called_once_with()

    def test_async_path_calls_on_done_via_apply_spool_data(self):
        """Covers _apply_spool_data's finally block invoking on_done once
        the async lookup resolves through the real set_spoolID/get_spool wiring."""
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        _stub_get_spool(spool, _make_spool_result())
        on_done = MagicMock()
        spool.set_spoolID(lane, 42, on_done=on_done, gcmd=gcmd)
        on_done.assert_called_once_with()

    def test_empty_spool_id_with_spoolman_not_remember_spool_clears_values(self):
        """spoolman not None + SpoolID='' + not remember_spool → clear_values."""
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, "", gcmd=gcmd)
        spool.clear_values.assert_called_once_with(lane)
    
    def test_remember_spool_clear_spoolid(self):
        gcmd = _make_gcmd()
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = True
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, "", gcmd=gcmd)
        spool.clear_values.assert_not_called()

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
        spool.set_spoolID.assert_called_once()
        call = spool.set_spoolID.call_args
        assert call.args == (lane1, 5)
        assert callable(call.kwargs["on_done"])

    def test_does_not_touch_filament_id_before_the_lookup(self):
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        lane1 = _make_lane("lane1")
        lane1.spool_id = 4
        lane1.filament_id = "OFDSrzZ8"
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.current = None
        spool.set_spoolID = MagicMock()
        spool.cmd_SET_SPOOL_ID(_make_gcmd(LANE="lane1", SPOOL_ID="5"))
        assert lane1.filament_id == "OFDSrzZ8"

    def test_no_spoolid_provided_set_spool_id(self):
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        lane1 = _make_lane("lane1")
        lane1.spool_id = None
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.current = None
        gcmd = _make_gcmd(LANE="lane1")
        spool.set_spoolID = MagicMock()
        spool.cmd_SET_SPOOL_ID(gcmd)
        spool.set_spoolID.assert_called_once()
        call = spool.set_spoolID.call_args
        assert call.args == (lane1, '')
        assert callable(call.kwargs["on_done"])

    def test_no_lane_param_logs_info(self):
        """spoolman not None + lane=None → log info + return."""
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        gcmd = _make_gcmd()  # no LANE key
        spool.cmd_SET_SPOOL_ID(gcmd)
        assert spool.logger.messages == [("info", "No LANE Defined")]

    def test_lane_not_in_lanes_logs_info(self):
        """spoolman not None + lane not in lanes → log info + return."""
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        spool.afc.lanes = {}
        gcmd = _make_gcmd(LANE="ghost", SPOOL_ID="7")
        spool.cmd_SET_SPOOL_ID(gcmd)
        assert spool.logger.messages == [("info", "ghost Unknown")]

    def test_calls_set_active_spool_when_lane_is_current(self):
        """cur_lane.name == afc.current → set_active_spool called once
        set_spoolID's on_done callback fires (deferred, since set_spoolID's
        actual assignment can be async)."""
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

        spool.set_active_spool.assert_not_called()
        on_done = spool.set_spoolID.call_args.kwargs["on_done"]
        lane1.spool_id = 9  # simulate set_spoolID having applied the new id
        on_done()
        spool.set_active_spool.assert_called_once_with(9)

    def test_does_not_call_set_active_spool_when_lane_not_current(self):
        spool = _make_spool()
        spool.afc.spoolman = MagicMock()
        lane1 = _make_lane("lane1")
        lane1.spool_id = None
        spool.afc.lanes = {"lane1": lane1}
        spool.afc.current = "lane2"  # a different lane is active
        gcmd = _make_gcmd(LANE="lane1", SPOOL_ID="9")
        spool.set_spoolID = MagicMock()
        spool.set_active_spool = MagicMock()

        spool.cmd_SET_SPOOL_ID(gcmd)
        on_done = spool.set_spoolID.call_args.kwargs["on_done"]
        on_done()

        spool.set_active_spool.assert_not_called()


# ── Auto switch debounce flag reset ──────────────────────────────────────────

class TestAutoSwitchFlagReset:
    def test_set_values_resets_auto_switch_flag(self):
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane("lane1")
        lane.auto_switch_triggered = True
        lane.remember_spool = False
        spool._set_values(lane, gcmd=gcmd)
        assert lane.auto_switch_triggered is False

    def test_clear_values_resets_auto_switch_flag(self):
        gcmd = _make_gcmd()
        spool = _make_spool()
        lane = _make_lane("lane1")
        lane.auto_switch_triggered = True
        lane.clear_lane_data = MagicMock()
        spool.clear_values(lane)
        assert lane.auto_switch_triggered is False

    def test_set_spoolID_resets_auto_switch_flag(self):
        gcmd = _make_gcmd()
        spool = _make_spool()
        spool.afc.spoolman = "http://spoolman:7912"
        spool.afc.moonraker = MagicMock()
        result = {
            'filament': {
                'material': 'PLA',
                'settings_extruder_temp': 210,
                'settings_bed_temp': 60,
                'density': 1.24,
                'diameter': 1.75,
                'color_hex': 'FF0000',
            },
            'remaining_weight': 800,
            'spool_weight': 190,
            'initial_weight': 1000,
        }
        spool.afc.moonraker.get_spool.side_effect = lambda id, callback: callback(result)
        lane = _make_lane("lane1")
        lane.auto_switch_triggered = True
        lane.espooler = MagicMock()
        lane.espooler.espooler_values = MagicMock()
        spool.set_spoolID(lane, "123", gcmd=gcmd)
        assert lane.auto_switch_triggered is False

class TestSetSnapmakerFilamentParams:
    def create_print_task_config(self):
        config = {
            "filament_vendor": ["NONE"]*4,
            "filament_type": ["NONE"]*4,
            "filament_sub_type": ["NONE"]*4,
            "filament_color": ["NONE"]*4,
            "filament_color_rgba": ["NONE"]*4,
            "filament_color_multi": ["NONE"]*4,
        }
        return config

    def test_no_snapmaker_printer(self):
        spool = _make_spool()
        spool.afc.snapmaker_printer = False  # explicit: default is False
        lane = _make_lane("lane1")
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)
        spool.printer.update_snapmaker_config_file.assert_not_called()
    
    def test_snapmaker_printer_print_task_none(self):
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        lane = _make_lane("lane1")
        spool.printer = MagicMock()
        spool.print_task_config_obj = None
        spool.set_snapmaker_filament_params(lane)
        spool.printer.update_snapmaker_config_file.assert_not_called()
    
    def test_snapmaker_printer_tool_not_loaded(self):
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        lane = _make_lane("lane1")
        lane.tool_loaded = False
        lane.extruder_obj.lane_loaded = lane.name
        lane.extruder_obj.is_standalone.return_value = False
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)
        spool.printer.update_snapmaker_config_file.assert_not_called()

    def test_standalone_lane_not_prepped_or_loaded_does_not_update(self):
        """A standalone lane sitting idle, never prepped or loaded to the buffer, must not update
        print_task_config: only prep_state/load_state being True opts a standalone lane in
        without a full toolhead load."""
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        lane = _make_lane("lane1")
        lane.tool_loaded = False
        lane.extruder_obj.is_standalone.return_value = True
        lane.prep_state = False
        lane.load_state = False
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)
        spool.printer.update_snapmaker_config_file.assert_not_called()

    def test_standalone_lane_prepped_updates_without_toolhead_load(self):
        """Independent coverage of the `prep_state` operand of
        `prep_state or load_state`: load_state is False here, so prep_state alone
        must be what lets a standalone lane with no toolhead load through."""
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG, config_path
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        spool.print_task_config_obj.config_path = config_path
        lane = _make_lane("lane1")
        lane.tool_loaded = False
        lane.extruder_obj.is_standalone.return_value = True
        lane.prep_state = True
        lane.load_state = False
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)
        spool.printer.update_snapmaker_config_file.assert_called_with(
            config_path,
            spool.print_task_config_obj.print_task_config,
            DEFAULT_PRINT_TASK_CONFIG
        )

    def test_standalone_lane_loaded_to_buffer_updates_without_toolhead_load(self):
        """Independent coverage of the `load_state` operand: prep_state is False
        here, so load_state alone must be what lets a standalone lane with no
        toolhead load through."""
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG, config_path
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        spool.print_task_config_obj.config_path = config_path
        lane = _make_lane("lane1")
        lane.tool_loaded = False
        lane.extruder_obj.is_standalone.return_value = True
        lane.prep_state = False
        lane.load_state = True
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)
        spool.printer.update_snapmaker_config_file.assert_called_with(
            config_path,
            spool.print_task_config_obj.print_task_config,
            DEFAULT_PRINT_TASK_CONFIG
        )

    def test_unit_attached_lane_prepped_but_not_loaded_does_not_update(self):
        """Independent coverage of the `is_standalone()` operand: prep_state is
        True here, but is_standalone() is False, so a unit-attached lane still
        needs the full toolhead-load path. Prep/load alone isn't enough for it."""
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        lane = _make_lane("lane1")
        lane.tool_loaded = False
        lane.extruder_obj.is_standalone.return_value = False
        lane.prep_state = True
        lane.load_state = True
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)
        spool.printer.update_snapmaker_config_file.assert_not_called()

    def test_snapmaker_printer_lane_name_not_match(self):
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        lane = _make_lane("lane1")
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = "lane2"
        lane.extruder_obj.is_standalone.return_value = False
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)
        spool.printer.update_snapmaker_config_file.assert_not_called()
    
    def test_snapmaker_printer_exception(self):
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG, config_path
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        spool.print_task_config_obj.config_path = config_path
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = "PLA"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        spool.printer = MagicMock()
        spool.printer.update_snapmaker_config_file.side_effect = RuntimeError("Creating error")
        spool.set_snapmaker_filament_params(lane)
        levels = [lvl for lvl, _ in spool.logger.messages]
        # The debug entry is traceback.format_exc(), whose exact text is
        # environment-dependent (file paths/line numbers) so only its
        # presence is checked; the error message is asserted verbatim.
        assert levels == ["error", "debug"]
        assert spool.logger.messages[0] == (
            "error", "Error when trying to update colors for snapmaker print_task_config"
        )
    
    def test_snapmaker_printer_extruder_update_called(self):
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG, config_path
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        spool.print_task_config_obj.config_path = config_path
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = "PLA"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)
        spool.printer.update_snapmaker_config_file.assert_called_with(
            config_path,
            spool.print_task_config_obj.print_task_config,
            DEFAULT_PRINT_TASK_CONFIG
        )

    def test_snapmaker_printer_extruder_check_material(self):
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = "PLA"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_vendor"][0] == "Generic"
        assert print_task_config["filament_type"][0] == lane.material
        assert print_task_config["filament_sub_type"][0] == "NONE"
    
    def test_snapmaker_printer_extruder_check_vendor(self):
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = "PLA"
        lane.tool_loaded = True
        lane.spool_vendor = "Polymaker"
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_vendor"][0] == lane.spool_vendor
        assert print_task_config["filament_type"][0] == lane.material
        assert print_task_config["filament_sub_type"][0] == "NONE"

    def test_snapmaker_printer_extruder_check_material_NONE(self):
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.afc.default_material_type = None
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = ""
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_vendor"][0] == "Generic"
        assert print_task_config["filament_type"][0] == "NONE"
        assert print_task_config["filament_sub_type"][0] == "NONE"

    def test_snapmaker_printer_extruder_check_material_default(self):
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.afc.default_material_type = "ABS"
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG

        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = ""
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_vendor"][0] == "Generic"
        assert print_task_config["filament_type"][0] == "ABS"
        assert print_task_config["filament_sub_type"][0] == "NONE"

    def test_snapmaker_printer_extruder_recognized_type_with_dash_is_kept_as_is(self):
        """A type that matches FILAMENT_PROTO_MAIN_TYPE_MAPPING, dash included,
        is stored exactly as given: the dash must not be stripped."""
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = "PETG-CF"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_type"][0] == "PETG-CF"
        assert spool.logger.messages == []

    def test_snapmaker_printer_extruder_special_characters_and_digits_are_stripped_first(self):
        """Special characters and digits, but not "-", are stripped before the mapping check,
        so "PETG-CF+1" resolves to the recognized "PETG-CF" rather than warning."""
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = "PETG-CF+1"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_type"][0] == "PETG-CF"
        assert spool.logger.messages == []

    def test_snapmaker_printer_extruder_recognized_type_with_a_digit_is_kept_as_is(self):
        """"PA6-CF" and "PA-CF" are distinct, recognized types. Normalizing an already
        recognized value would strip the "6" and silently store the wrong one."""
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = "PA6-CF"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_type"][0] == "PA6-CF"
        assert spool.logger.messages == []

    def test_snapmaker_printer_extruder_digits_alone_are_stripped(self):
        """Independent coverage of the digit-stripping, separate from the
        dash-preserving/special-character cases above: "PLA2" has no special
        characters, only a trailing digit, which must still be stripped."""
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = "PLA2"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_type"][0] == "PLA"
        assert spool.logger.messages == []

    def test_snapmaker_printer_extruder_all_digit_material_falls_back_to_none(self):
        """A material that's nothing but digits strips down to an empty string,
        which must still fall back to NONE silently (NONE is the expected
        sentinel, not an unrecognized type, so it must not warn)."""
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = "123"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_type"][0] == "NONE"
        assert spool.logger.messages == []

    def test_snapmaker_printer_extruder_unrecognized_type_warns_and_falls_back_to_none(self):
        """A material that doesn't match any key in FILAMENT_PROTO_MAIN_TYPE_MAPPING,
        even after stripping, must warn the user and fall back to NONE rather than
        sending an unrecognized value to snapmaker."""
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = "XYZ123"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_type"][0] == "NONE"
        assert spool.logger.messages == [(
            "warning",
            "Lane lane1: filament type 'XYZ' is not a snapmaker filament "
            "type, setting filament_type to NONE for this toolhead."
        )]

    def test_snapmaker_printer_extruder_no_material_defaults_to_none_without_warning(self):
        """An empty material, with no lane.material and no default_material_type, falls back to
        NONE silently. NONE is the expected sentinel, not an unrecognized type, so it must not
        warn."""
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.afc.default_material_type = None
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = ""
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_type"][0] == "NONE"
        assert spool.logger.messages == []

    def test_snapmaker_printer_extruder_check_single_color(self):
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.color = "#123456"
        lane.material = "PLA"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_color"][0] == int("FF123456", 16) 
        assert print_task_config["filament_color_rgba"][0] == "123456FF"
        assert print_task_config["filament_color_multi"][0]["mode"] == 0
        assert print_task_config["filament_color_multi"][0]["nums"] == 1
        assert print_task_config["filament_color_multi"][0]["colors"] == ["123456"]
    
    def test_snapmaker_printer_extruder_check_no_color(self):
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.color = ""
        lane.material = "PLA"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        lane.multi_color = []
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config

        assert print_task_config["filament_color"][0] == int("FFFFFFFF", 16) 
        assert print_task_config["filament_color_rgba"][0] == "FFFFFFFF"
        assert print_task_config["filament_color_multi"][0]["mode"] == 0
        assert print_task_config["filament_color_multi"][0]["nums"] == 1
        assert print_task_config["filament_color_multi"][0]["colors"] == ["FFFFFF"]

    def test_snapmaker_printer_extruder_check_multi_color(self):
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        lane = _make_lane("lane1")
        lane.multi_color = ["123456", "589465", "725893"]
        lane.color = "#123456"
        lane.material = "PLA"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config
        
        assert print_task_config["filament_color"][0] == int("FF123456", 16) 
        assert print_task_config["filament_color_rgba"][0] == "123456FF"
        assert print_task_config["filament_color_multi"][0]["mode"] == 1
        assert print_task_config["filament_color_multi"][0]["nums"] == 3
        assert print_task_config["filament_color_multi"][0]["colors"] == ["123456", "589465", "725893"]

    def test_snapmaker_printer_extruder3_check_multi_color(self):
        from tests.conftest import _make_print_task_config, _make_filament_protocol_mock
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        sys.modules.setdefault("extras.filament_protocol", _make_filament_protocol_mock())
        from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        spool.print_task_config_obj.print_task_config = DEFAULT_PRINT_TASK_CONFIG
        extruder_num = 3
        lane = _make_lane("lane1", f"extruder{extruder_num}", extruder_num)
        lane.multi_color = ["123456", "589465", "725893"]
        lane.color = "#123456"
        lane.material = "PLA"
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)

        print_task_config = spool.print_task_config_obj.print_task_config
        
        assert print_task_config["filament_color"][extruder_num] == int("FF123456", 16), print_task_config 
        assert print_task_config["filament_color_rgba"][extruder_num] == "123456FF"
        assert print_task_config["filament_color_multi"][extruder_num]["mode"] == 1
        assert print_task_config["filament_color_multi"][extruder_num]["nums"] == 3
        assert print_task_config["filament_color_multi"][extruder_num]["colors"] == ["123456", "589465", "725893"]


# ── load_config ──────────────────────────────────────────────────────────────

class TestLoadConfig:
    def test_returns_afc_spool_instance(self):
        from tests.conftest import MockAFC, MockConfig, MockPrinter

        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_Spool", printer=printer)
        result = load_config(config)
        assert isinstance(result, AFCSpool)

    def test_registers_klippy_connect_handler(self):
        """Proves load_config actually wires the real constructor through,
        not just that some AFCSpool-shaped object comes back."""
        from tests.conftest import MockAFC, MockConfig, MockPrinter

        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_Spool", printer=printer)
        load_config(config)
        assert printer._event_handlers.get("klippy:connect")
    