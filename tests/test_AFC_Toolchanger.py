# Unit tests for extras/AFC_Toolchanger.py

from __future__ import annotations

from unittest.mock import MagicMock

from extras.AFC_Toolchanger import AfcToolchanger
from tests.conftest import MockLogger
from tests.test_AFC_extruder import _make_extruder_obj

def _make_toolchanger():
    obj = AfcToolchanger.__new__(AfcToolchanger)
    obj.tool_swap = MagicMock()
    obj.logger = MockLogger()
    obj.afc = MagicMock()
    obj.afc.tools = {}
    return obj

def _make_extruder_for_toolchanger(toolchanger, afc_name="e0", extruder_name="extruder"):
    extruder = _make_extruder_obj(afc_name)
    extruder.th_extruder_name = extruder_name
    extruder.tc_lane = MagicMock()
    toolchanger.afc.tools[extruder.th_extruder_name] = extruder
    return extruder

class TestcmdAFCSelectTool:
    def test_dict_tool_name(self):
        tool = _make_toolchanger()
        tool.tool_swap = MagicMock()
        extruder = _make_extruder_for_toolchanger(tool)
        extruder1 = _make_extruder_for_toolchanger(tool, "e1", "extruder1")

        key_tool = "extruder"
        gcmd = MagicMock()
        gcmd.get.side_effect = lambda key, default=None: {
            "TOOL": key_tool
        }.get(key, default)

        tool.cmd_AFC_SELECT_TOOL(gcmd)
        tool.tool_swap.assert_called_once_with(extruder.tc_lane)
    
    def test_afc_extruder_tool_name(self):
        tool = _make_toolchanger()
        tool.tool_swap = MagicMock()
        extruder = _make_extruder_for_toolchanger(tool)
        extruder1 = _make_extruder_for_toolchanger(tool, "e1", "extruder1")
        
        key_tool = "e1"
        gcmd = MagicMock()
        gcmd.get.side_effect = lambda key, default=None: {
            "TOOL": key_tool
        }.get(key, default)

        tool.cmd_AFC_SELECT_TOOL(gcmd)
        tool.tool_swap.assert_called_once_with(extruder1.tc_lane)
    
    def test_invalid_tool_name(self):
        tool = _make_toolchanger()
        tool.tool_swap = MagicMock()
        extruder = _make_extruder_for_toolchanger(tool)
        extruder1 = _make_extruder_for_toolchanger(tool, "e1", "extruder1")
        
        key_tool = "e2"
        gcmd = MagicMock()
        gcmd.get.side_effect = lambda key, default=None: {
            "TOOL": key_tool
        }.get(key, default)

        tool.cmd_AFC_SELECT_TOOL(gcmd)
        tool.tool_swap.assert_not_called()
        error_msg = [m for lvl, m in tool.logger.messages if lvl == "error"]
        assert any(f"Key:{key_tool} invalid for TOOL" in m for m in error_msg)

    def test_invalid_tc_lane(self):
        tool = _make_toolchanger()
        tool.tool_swap = MagicMock()
        extruder = _make_extruder_for_toolchanger(tool)
        extruder1 = _make_extruder_for_toolchanger(tool, "e1", "extruder1")
        extruder1.tc_lane = None
        
        key_tool = "e1"
        gcmd = MagicMock()
        gcmd.get.side_effect = lambda key, default=None: {
            "TOOL": key_tool
        }.get(key, default)

        tool.cmd_AFC_SELECT_TOOL(gcmd)
        tool.tool_swap.assert_not_called()
        error_msg = [m for lvl, m in tool.logger.messages if lvl == "error"]
        assert any(f"Tool '{key_tool}' does not have a valid 'tc_lane' attribute." in m for m in error_msg)
