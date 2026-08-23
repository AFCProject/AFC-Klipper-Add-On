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

from extras.AFC_spool import AFCSpool, load_config


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
        )

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
        )


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
        spool.afc.error.AFC_error.assert_called_once_with("Mapping T9 does not exist", False)
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
        lane.unit_obj.lane_loaded.assert_called_once_with(lane, force=True)
        

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


class TestRegisterLaneMacros:
    def test_registers_seven_mux_commands(self):
        spool = _make_spool()
        spool.gcode.register_mux_command = MagicMock()
        lane = _make_lane("lane1")
        spool.register_lane_macros(lane)
        assert spool.gcode.register_mux_command.call_count == 7

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

    def test_set_value_material_not_set_remember_spool(self):
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = True
        lane.spool_id = 42
        lane.color = "123456"
        spool.afc.default_material_type = "PLA"
        spool.afc.spoolman = MagicMock()
        spool.next_spool_id = 42
        spool.set_spoolID = MagicMock()
        spool._set_values(lane)
        assert lane.material == ""
        assert lane.weight == 0

    def test_set_value_material_not_set_no_spool_id(self):
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        lane.color = "123456"
        spool.afc.default_material_type = "PLA"
        spool.afc.spoolman = MagicMock()
        spool.set_spoolID = MagicMock()
        spool._set_values(lane)
        assert lane.material == ""
        assert lane.weight == 0
    
    def test_set_value_material_not_set_no_color(self):
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        lane.spool_id = 42
        lane.color = ""
        spool.afc.default_material_type = "PLA"
        spool.afc.spoolman = MagicMock()
        spool.next_spool_id = 42
        spool.set_spoolID = MagicMock()
        spool._set_values(lane)
        assert lane.material == ""
        assert lane.weight == 0


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
        """Covers the `spoolman is not None` half of the combined condition
        being falsy on its own -- _make_spool()'s afc.moonraker defaults to
        set, so this proves moonraker alone isn't enough either."""
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = False
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, "")
        spool.clear_values.assert_called_once_with(lane)
    
    def test_no_spool_remember_spool_clear_spoolid(self):
        spool = _make_spool()
        lane = _make_lane()
        lane.remember_spool = True
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, "")
        spool.clear_values.assert_not_called()

    def test_spoolman_set_but_no_moonraker_clears_values(self):
        """Covers the `moonraker is not None` half of the combined condition
        being falsy on its own, with spoolman still set -- proves spoolman
        alone isn't enough to enter the lookup branch."""
        spool = self._make_spool_with_spoolman()
        spool.afc.moonraker = None
        lane = _make_lane()
        lane.remember_spool = False
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, "42")
        spool.clear_values.assert_called_once_with(lane)

    def test_valid_spool_id_sets_lane_attributes(self):
        spool = self._make_spool_with_spoolman()
        spool.set_snapmaker_filament_params = MagicMock()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        spool.afc.moonraker.get_spool = MagicMock(return_value=result)
        spool.set_spoolID(lane, 42)
        assert lane.spool_id == 42
        assert lane.material == "PLA"
        assert lane.color == "#FF0000"
        assert lane.spool_vendor == ""
        spool.set_snapmaker_filament_params.assert_called_once()
        spool.afc.function.afc_led.assert_not_called()

    def test_valid_spool_id_sets_vendor(self):
        spool = self._make_spool_with_spoolman()
        spool.set_snapmaker_filament_params = MagicMock()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        result["filament"]["vendor"] = {}
        result["filament"]["vendor"]["name"] = "Polymaker"
        spool.afc.moonraker.get_spool = MagicMock(return_value=result)
        spool.set_spoolID(lane, 42)
        assert lane.spool_id == 42
        assert lane.material == "PLA"
        assert lane.color == "#FF0000"
        assert lane.spool_vendor == "Polymaker"
        spool.set_snapmaker_filament_params.assert_called_once()
        spool.afc.function.afc_led.assert_not_called()
    
    def test_valid_spool_id_lane_not_loaded_lane_in_unit(self):
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        lane.load_state = False
        lane.unit = "test"
        spool.afc.units[lane.unit] = lane.unit_obj
        result = _make_spool_result()
        spool.afc.moonraker.get_spool = MagicMock(return_value=result)
        spool.set_spoolID(lane, 42)
        spool.afc.function.afc_led.assert_not_called()

    def test_valid_spool_id_lane_loaded_lane_in_unit(self):
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.load_state = True
        lane.unit = "test"
        spool.afc.units[lane.unit] = lane.unit_obj
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        spool.afc.moonraker.get_spool = MagicMock(return_value=result)
        spool.set_spoolID(lane, 42)
        lane.unit_obj.lane_loaded.assert_called_once_with(lane, force=True)
    
    def test_valid_spool_id_lane_loaded_lane_not_in_unit(self):
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.load_state = True
        lane.unit = "test"
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result()
        spool.afc.moonraker.get_spool = MagicMock(return_value=result)
        spool.set_spoolID(lane, 42)
        spool.afc.function.afc_led.assert_not_called()

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
        assert lane.multi_color == result["filament"]["multi_color_hexes"].split(",")

    def test_multi_color_is_none(self):
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        lane.espooler = MagicMock()
        result = _make_spool_result(color_hex="AABBCC")
        result["filament"]["multi_color_hexes"] = None
        spool.afc.moonraker.get_spool = MagicMock(return_value=result)
        spool.set_spoolID(lane, 42)
        assert lane.color == "#AABBCC"
        assert lane.multi_color == []

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
        """exception in get_spool → AFC_error called."""
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        spool.afc.moonraker.get_spool = MagicMock(
            side_effect=Exception("Connection refused")
        )
        spool.set_spoolID(lane, 42)
        spool.afc.error.AFC_error.assert_called()

    def test_empty_spool_id_with_spoolman_not_remember_spool_clears_values(self):
        """spoolman not None + SpoolID='' + not remember_spool → clear_values."""
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = False
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, "")
        spool.clear_values.assert_called_once_with(lane)
    
    def test_remember_spool_clear_spoolid(self):
        spool = self._make_spool_with_spoolman()
        lane = _make_lane()
        lane.remember_spool = True
        spool.clear_values = MagicMock()
        spool.set_spoolID(lane, "")
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
        spool.set_spoolID.assert_called_once_with(lane1, 5)

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
        spool.set_spoolID.assert_called_once_with(lane1, '')

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
        """cur_lane.name == afc.current → set_active_spool called."""
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


# ── Auto switch debounce flag reset ──────────────────────────────────────────

class TestAutoSwitchFlagReset:
    def test_set_values_resets_auto_switch_flag(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        lane.auto_switch_triggered = True
        lane.remember_spool = False
        spool._set_values(lane)
        assert lane.auto_switch_triggered is False

    def test_clear_values_resets_auto_switch_flag(self):
        spool = _make_spool()
        lane = _make_lane("lane1")
        lane.auto_switch_triggered = True
        lane.clear_lane_data = MagicMock()
        spool.clear_values(lane)
        assert lane.auto_switch_triggered is False

    def test_set_spoolID_resets_auto_switch_flag(self):
        spool = _make_spool()
        spool.afc.spoolman = "http://spoolman:7912"
        spool.afc.moonraker = MagicMock()
        spool.afc.moonraker.get_spool.return_value = {
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
        lane = _make_lane("lane1")
        lane.auto_switch_triggered = True
        lane.espooler = MagicMock()
        lane.espooler.espooler_values = MagicMock()
        spool.set_spoolID(lane, "123")
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
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        lane = _make_lane("lane1")
        lane.tool_loaded = False
        lane.extruder_obj.lane_loaded = lane.name
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)
        spool.printer.update_snapmaker_config_file.assert_not_called()

    def test_snapmaker_printer_lane_name_not_match(self):
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
        spool = _make_spool()
        spool.afc.snapmaker_printer = True
        spool.print_task_config_obj = MagicMock()
        lane = _make_lane("lane1")
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = "lane2"
        spool.printer = MagicMock()
        spool.set_snapmaker_filament_params(lane)
        spool.printer.update_snapmaker_config_file.assert_not_called()
    
    def test_snapmaker_printer_exception(self):
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
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
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
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
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
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
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
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
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
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
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
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

    def test_snapmaker_printer_extruder_check_single_color(self):
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
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
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
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
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
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
        from tests.conftest import _make_print_task_config
        import sys
        sys.modules.setdefault("extras.print_task_config", _make_print_task_config())
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
    