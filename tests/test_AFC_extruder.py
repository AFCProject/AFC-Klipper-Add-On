"""
Unit tests for extras/AFC_extruder.py

Covers:
  - AFCExtruderStats: cut threshold warning/error logic
  - AFCExtruderStats.increase_cut_total: increments counts
  - AFCExtruderStats.increase_toolcount_change: increments total
  - AFCExtruderStats.reset_stats: resets all counts
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from extras.AFC_extruder import AFCExtruderStats, AFCExtruder


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_extruder_obj(name="extruder"):
    """Minimal AFCExtruder-like mock."""
    from tests.conftest import MockAFC, MockLogger
    afc = MockAFC()
    afc.afc_stats = MagicMock()
    obj = MagicMock()
    obj.name = name
    obj.afc = afc
    obj.logger = MockLogger()
    return obj


def _make_stats(extruder_name="extruder", cut_threshold=200, cut_since_changed=0):
    """Build an AFCExtruderStats bypassing heavy __init__ logic."""
    obj = _make_extruder_obj(extruder_name)
    stats = AFCExtruderStats.__new__(AFCExtruderStats)
    stats.name = extruder_name
    stats.obj = obj
    logger = obj.logger
    # AFC_extruder accesses self.logger.afc.message_queue; wire it up
    from tests.conftest import MockAFC
    afc_for_logger = MockAFC()
    logger.afc = afc_for_logger
    stats.logger = logger
    stats.cut_threshold_for_warning = cut_threshold
    stats.threshold_warning_sent = False
    stats.threshold_error_sent = False

    from tests.conftest import MockMoonraker
    mr = MockMoonraker()
    mr.update_afc_stats = MagicMock()
    stats.moonraker = mr

    # Create AFCStats_var mocks for the various stats
    def _make_var(value=0):
        v = MagicMock()
        v.value = value
        return v

    stats.cut_total = _make_var(0)
    stats.cut_total_since_changed = _make_var(cut_since_changed)
    stats.last_blade_changed = _make_var(0)
    stats.tc_total = _make_var(0)
    stats.tc_tool_unload = _make_var(0)
    stats.tc_tool_load = _make_var(0)
    stats.tool_selected = _make_var(0)
    stats.tool_unselected = _make_var(0)

    return stats


# ── AFCExtruderStats: initialization ──────────────────────────────────────────

class TestAFCExtruderStatsInit:
    def test_name_stored(self):
        stats = _make_stats("extruder")
        assert stats.name == "extruder"

    def test_cut_threshold_stored(self):
        stats = _make_stats(cut_threshold=300)
        assert stats.cut_threshold_for_warning == 300

    def test_threshold_warning_not_sent_initially(self):
        stats = _make_stats()
        assert stats.threshold_warning_sent is False

    def test_threshold_error_not_sent_initially(self):
        stats = _make_stats()
        assert stats.threshold_error_sent is False


# ── check_cut_threshold ───────────────────────────────────────────────────────

class TestCheckCutThreshold:
    def test_no_message_well_below_threshold(self):
        """No message when cuts < threshold - 1000."""
        stats = _make_stats(cut_threshold=2000, cut_since_changed=500)
        stats.check_cut_threshold()
        assert stats.threshold_warning_sent is False
        assert stats.threshold_error_sent is False

    def test_warning_sent_near_threshold(self):
        """Warning sent when cuts >= threshold - 1000."""
        stats = _make_stats(cut_threshold=2000, cut_since_changed=1001)
        stats.check_cut_threshold()
        assert stats.threshold_warning_sent is True

    def test_warning_logged_near_threshold(self):
        stats = _make_stats(cut_threshold=2000, cut_since_changed=1001)
        stats.check_cut_threshold()
        raw_msgs = [m for lvl, m in stats.logger.messages if lvl == "raw"]
        assert len(raw_msgs) >= 1

    def test_error_sent_at_threshold(self):
        """Error logged when cuts >= threshold."""
        stats = _make_stats(cut_threshold=200, cut_since_changed=200)
        stats.check_cut_threshold()
        assert stats.threshold_error_sent is True

    def test_error_sent_above_threshold(self):
        stats = _make_stats(cut_threshold=200, cut_since_changed=250)
        stats.check_cut_threshold()
        assert stats.threshold_error_sent is True

    def test_warning_not_resent_when_already_sent(self):
        """Warning should not spam logger if already sent."""
        stats = _make_stats(cut_threshold=2000, cut_since_changed=1001)
        stats.threshold_warning_sent = True
        stats.check_cut_threshold()
        raw_msgs = [m for lvl, m in stats.logger.messages if lvl == "raw"]
        assert len(raw_msgs) == 0  # no new messages

    def test_error_not_resent_when_already_sent(self):
        stats = _make_stats(cut_threshold=200, cut_since_changed=250)
        stats.threshold_error_sent = True
        stats.check_cut_threshold()
        raw_msgs = [m for lvl, m in stats.logger.messages if lvl == "raw"]
        assert len(raw_msgs) == 0


# ── increase_cut_total ────────────────────────────────────────────────────────

class TestIncreaseCutTotal:
    def test_increments_cut_total(self):
        stats = _make_stats()
        stats.check_cut_threshold = MagicMock()
        stats.increase_cut_total()
        stats.cut_total.increase_count.assert_called_once()

    def test_increments_cut_total_since_changed(self):
        stats = _make_stats()
        stats.check_cut_threshold = MagicMock()
        stats.increase_cut_total()
        stats.cut_total_since_changed.increase_count.assert_called_once()

    def test_calls_check_cut_threshold(self):
        stats = _make_stats()
        stats.check_cut_threshold = MagicMock()
        stats.increase_cut_total()
        stats.check_cut_threshold.assert_called_once()


# ── increase_toolcount_change ──────────────────────────────────────────────────

class TestIncreaseToolcountChange:
    def test_increments_tc_total(self):
        stats = _make_stats()
        stats.increase_toolcount_change()
        stats.tc_total.increase_count.assert_called_once()

    def test_increments_toolchange_wo_error_on_afc_stats(self):
        stats = _make_stats()
        stats.increase_toolcount_change()
        stats.obj.afc.afc_stats.increase_toolchange_wo_error.assert_called_once()


# ── reset_stats ────────────────────────────────────────────────────────────────

class TestResetStats:
    def test_resets_tc_total(self):
        stats = _make_stats()
        stats.reset_stats()
        stats.tc_total.reset_count.assert_called_once()

    def test_resets_tc_tool_unload(self):
        stats = _make_stats()
        stats.reset_stats()
        stats.tc_tool_unload.reset_count.assert_called_once()

    def test_resets_tc_tool_load(self):
        stats = _make_stats()
        stats.reset_stats()
        stats.tc_tool_load.reset_count.assert_called_once()

    def test_resets_tool_selected(self):
        stats = _make_stats()
        stats.reset_stats()
        stats.tool_selected.reset_count.assert_called_once()

    def test_resets_tool_unselected(self):
        stats = _make_stats()
        stats.reset_stats()
        stats.tool_unselected.reset_count.assert_called_once()


# ── AFCExtruder helpers ────────────────────────────────────────────────────────

def _make_afc_extruder(name="extruder"):
    """Build an AFCExtruder bypassing __init__."""
    from tests.conftest import MockAFC, MockPrinter, MockLogger, MockReactor

    afc = MockAFC()
    reactor = MockReactor()
    printer = MockPrinter(afc=afc)
    printer._reactor = reactor

    ext = AFCExtruder.__new__(AFCExtruder)
    ext.printer = printer
    ext.afc = afc
    ext.logger = MockLogger()
    ext.reactor = reactor
    ext.fullname = f"AFC_extruder {name}"
    ext.name = name
    ext.lane_loaded = None
    ext.lanes = {}
    ext.tool_start = None
    ext.tool_end = None
    ext.tool_start_state = False
    ext.tool_end_state = False
    ext.buffer_trailing = False
    ext.tool_stn = 72.0
    ext.tool_stn_unload = 100.0
    ext.tool_sensor_after_extruder = 0.0
    ext.tool_unload_speed = 25.0
    ext.tool_load_speed = 25.0
    ext.buffer_name = None
    ext.common_save_msg = f"\nRun SAVE_EXTRUDER_VALUES EXTRUDER={name} once done."
    ext.estats = MagicMock()
    ext.function = afc.function

    # Toolchanger stuff
    ext.tool_obj = None
    ext.tc_unit_name = None
    ext.tool = None
    ext.toolhead_leds = None
    ext.mutex = MagicMock()
    return ext


# ── AFCExtruder.__str__ ────────────────────────────────────────────────────────

class TestAFCExtruderStr:
    def test_str_returns_name(self):
        ext = _make_afc_extruder("my_extruder")
        assert str(ext) == "my_extruder"


# ── handle_connect ─────────────────────────────────────────────────────────────

class TestAFCExtruderHandleConnect:
    def test_handle_connect_stores_self_in_afc_tools(self):
        ext = _make_afc_extruder("extruder")
        ext.handle_connect()
        assert ext.afc.tools["extruder"] is ext

    def test_handle_connect_sets_reactor(self):
        ext = _make_afc_extruder()
        ext.reactor = None
        ext.handle_connect()
        assert ext.reactor is ext.afc.reactor


# ── handle_moonraker_connect ───────────────────────────────────────────────────

class TestAFCExtruderHandleMoonrakerConnect:
    def test_delegates_to_estats(self):
        ext = _make_afc_extruder()
        ext.handle_moonraker_connect()
        ext.estats.handle_moonraker_stats.assert_called_once()


# ── _handle_toolhead_sensor_runout ─────────────────────────────────────────────

class TestHandleToolheadSensorRunout:
    def test_no_runout_when_state_true(self):
        ext = _make_afc_extruder()
        lane = MagicMock()
        ext.lanes = {"lane1": lane}
        ext.lane_loaded = "lane1"
        ext._handle_toolhead_sensor_runout(True, "tool_start")
        lane.handle_toolhead_runout.assert_not_called()

    def test_no_runout_when_no_lane_loaded(self):
        ext = _make_afc_extruder()
        lane = MagicMock()
        ext.lanes = {"lane1": lane}
        ext.lane_loaded = None
        ext._handle_toolhead_sensor_runout(False, "tool_start")
        lane.handle_toolhead_runout.assert_not_called()

    def test_no_runout_when_lane_not_in_lanes_dict(self):
        ext = _make_afc_extruder()
        ext.lanes = {}
        ext.lane_loaded = "lane1"
        ext._handle_toolhead_sensor_runout(False, "tool_start")
        # no KeyError, just silently skips

    def test_runout_calls_lane_handle_toolhead_runout(self):
        ext = _make_afc_extruder()
        lane = MagicMock()
        ext.lanes = {"lane1": lane}
        ext.lane_loaded = "lane1"
        ext._handle_toolhead_sensor_runout(False, "tool_start")
        lane.handle_toolhead_runout.assert_called_once_with(sensor="tool_start")

    def test_runout_passes_sensor_name(self):
        ext = _make_afc_extruder()
        lane = MagicMock()
        ext.lanes = {"lane1": lane}
        ext.lane_loaded = "lane1"
        ext._handle_toolhead_sensor_runout(False, "tool_end")
        lane.handle_toolhead_runout.assert_called_once_with(sensor="tool_end")


# ── tool_start_callback ────────────────────────────────────────────────────────

class TestToolStartCallback:
    def test_updates_state_true(self):
        ext = _make_afc_extruder()
        ext.tool_start_callback(100.0, True)
        assert ext.tool_start_state is True

    def test_updates_state_false(self):
        ext = _make_afc_extruder()
        ext.tool_start_state = True
        ext.tool_start_callback(101.0, False)
        assert ext.tool_start_state is False


# ── tool_end_callback ──────────────────────────────────────────────────────────

class TestToolEndCallback:
    def test_updates_state_true(self):
        ext = _make_afc_extruder()
        ext.tool_end_callback(100.0, True)
        assert ext.tool_end_state is True

    def test_updates_state_false(self):
        ext = _make_afc_extruder()
        ext.tool_end_state = True
        ext.tool_end_callback(101.0, False)
        assert ext.tool_end_state is False


# ── buffer_trailing_callback ───────────────────────────────────────────────────

class TestBufferTrailingCallback:
    def test_updates_buffer_trailing_true(self):
        ext = _make_afc_extruder()
        ext.buffer_trailing_callback(100.0, True)
        assert ext.buffer_trailing is True

    def test_updates_buffer_trailing_false(self):
        ext = _make_afc_extruder()
        ext.buffer_trailing = True
        ext.buffer_trailing_callback(101.0, False)
        assert ext.buffer_trailing is False


# ── handle_start_runout ────────────────────────────────────────────────────────

class TestHandleStartRunout:
    def test_updates_min_event_systime(self):
        ext = _make_afc_extruder()
        ext._handle_toolhead_sensor_runout = MagicMock()
        ext.fila_tool_start = MagicMock()
        ext.fila_tool_start.runout_helper.min_event_systime = 0.0
        ext.fila_tool_start.runout_helper.event_delay = 0.5
        ext.handle_start_runout(100.0)
        assert ext.fila_tool_start.runout_helper.min_event_systime != 0.0

    def test_calls_handle_toolhead_sensor_runout_with_tool_start(self):
        ext = _make_afc_extruder()
        ext._handle_toolhead_sensor_runout = MagicMock()
        ext.fila_tool_start = MagicMock()
        ext.fila_tool_start.runout_helper.filament_present = False
        ext.fila_tool_start.runout_helper.event_delay = 0.5
        ext.handle_start_runout(100.0)
        ext._handle_toolhead_sensor_runout.assert_called_once_with(False, "tool_start")


# ── handle_end_runout ──────────────────────────────────────────────────────────

class TestHandleEndRunout:
    def test_updates_min_event_systime(self):
        ext = _make_afc_extruder()
        ext._handle_toolhead_sensor_runout = MagicMock()
        ext.fila_tool_end = MagicMock()
        ext.fila_tool_end.runout_helper.min_event_systime = 0.0
        ext.fila_tool_end.runout_helper.event_delay = 0.5
        ext.handle_end_runout(100.0)
        assert ext.fila_tool_end.runout_helper.min_event_systime != 0.0

    def test_calls_handle_toolhead_sensor_runout_with_tool_end(self):
        ext = _make_afc_extruder()
        ext._handle_toolhead_sensor_runout = MagicMock()
        ext.fila_tool_end = MagicMock()
        ext.fila_tool_end.runout_helper.filament_present = True
        ext.fila_tool_end.runout_helper.event_delay = 0.5
        ext.handle_end_runout(100.0)
        ext._handle_toolhead_sensor_runout.assert_called_once_with(True, "tool_end")


# ── _update_tool_stn ──────────────────────────────────────────────────────────

class TestUpdateToolStn:
    def test_positive_value_updates_tool_stn(self):
        ext = _make_afc_extruder()
        ext._update_tool_stn(80.0)
        assert ext.tool_stn == 80.0

    def test_zero_value_logs_error(self):
        ext = _make_afc_extruder()
        ext._update_tool_stn(0.0)
        error_msgs = [m for lvl, m in ext.logger.messages if lvl == "error"]
        assert any("greater than zero" in m for m in error_msgs)

    def test_zero_value_does_not_update(self):
        ext = _make_afc_extruder()
        ext._update_tool_stn(0.0)
        assert ext.tool_stn == 72.0  # unchanged

    def test_positive_value_logs_info(self):
        ext = _make_afc_extruder()
        ext._update_tool_stn(90.0)
        info_msgs = [m for lvl, m in ext.logger.messages if lvl == "info"]
        assert any("tool_stn" in m for m in info_msgs)


# ── _update_tool_stn_unload ───────────────────────────────────────────────────

class TestUpdateToolStnUnload:
    def test_zero_value_is_accepted(self):
        ext = _make_afc_extruder()
        ext._update_tool_stn_unload(0.0)
        assert ext.tool_stn_unload == 0.0

    def test_positive_value_updates(self):
        ext = _make_afc_extruder()
        ext._update_tool_stn_unload(50.0)
        assert ext.tool_stn_unload == 50.0

    def test_negative_value_logs_error(self):
        ext = _make_afc_extruder()
        ext._update_tool_stn_unload(-1.0)
        error_msgs = [m for lvl, m in ext.logger.messages if lvl == "error"]
        assert any("greater than or equal to zero" in m for m in error_msgs)

    def test_negative_value_does_not_update(self):
        ext = _make_afc_extruder()
        ext._update_tool_stn_unload(-5.0)
        assert ext.tool_stn_unload == 100.0  # unchanged


# ── _update_tool_after_extr ───────────────────────────────────────────────────

class TestUpdateToolAfterExtr:
    def test_positive_value_updates(self):
        ext = _make_afc_extruder()
        ext._update_tool_after_extr(10.0)
        assert ext.tool_sensor_after_extruder == 10.0

    def test_zero_value_logs_error(self):
        ext = _make_afc_extruder()
        ext._update_tool_after_extr(0.0)
        error_msgs = [m for lvl, m in ext.logger.messages if lvl == "error"]
        assert any("greater than zero" in m for m in error_msgs)

    def test_positive_value_logs_info(self):
        ext = _make_afc_extruder()
        ext._update_tool_after_extr(15.0)
        info_msgs = [m for lvl, m in ext.logger.messages if lvl == "info"]
        assert any("tool_sensor_after_extruder" in m for m in info_msgs)


# ── cmd_UPDATE_TOOLHEAD_SENSORS ───────────────────────────────────────────────

class TestCmdUpdateToolheadSensors:
    def _make_gcmd(self, tool_stn=None, tool_stn_unload=None, tool_after=None,
                   ext=None):
        gcmd = MagicMock()
        gcmd.get_float.side_effect = lambda key, default: {
            "TOOL_STN": tool_stn if tool_stn is not None else (ext.tool_stn if ext else default),
            "TOOL_STN_UNLOAD": tool_stn_unload if tool_stn_unload is not None else (ext.tool_stn_unload if ext else default),
            "TOOL_AFTER_EXTRUDER": tool_after if tool_after is not None else (ext.tool_sensor_after_extruder if ext else default),
        }[key]
        return gcmd

    def test_changed_tool_stn_calls_update(self):
        ext = _make_afc_extruder()
        ext._update_tool_stn = MagicMock()
        gcmd = self._make_gcmd(tool_stn=90.0, tool_stn_unload=100.0, tool_after=0.0, ext=ext)
        ext.cmd_UPDATE_TOOLHEAD_SENSORS(gcmd)
        ext._update_tool_stn.assert_called_once_with(90.0)

    def test_unchanged_tool_stn_skips_update(self):
        ext = _make_afc_extruder()
        ext._update_tool_stn = MagicMock()
        gcmd = self._make_gcmd(tool_stn=72.0, tool_stn_unload=100.0, tool_after=0.0, ext=ext)
        ext.cmd_UPDATE_TOOLHEAD_SENSORS(gcmd)
        ext._update_tool_stn.assert_not_called()

    def test_changed_tool_stn_unload_calls_update(self):
        ext = _make_afc_extruder()
        ext._update_tool_stn_unload = MagicMock()
        gcmd = self._make_gcmd(tool_stn=72.0, tool_stn_unload=50.0, tool_after=0.0, ext=ext)
        ext.cmd_UPDATE_TOOLHEAD_SENSORS(gcmd)
        ext._update_tool_stn_unload.assert_called_once_with(50.0)

    def test_changed_tool_after_extr_calls_update(self):
        ext = _make_afc_extruder()
        ext._update_tool_after_extr = MagicMock()
        gcmd = self._make_gcmd(tool_stn=72.0, tool_stn_unload=100.0, tool_after=5.0, ext=ext)
        ext.cmd_UPDATE_TOOLHEAD_SENSORS(gcmd)
        ext._update_tool_after_extr.assert_called_once_with(5.0)


# ── cmd_SAVE_EXTRUDER_VALUES ──────────────────────────────────────────────────

class TestCmdSaveExtruderValues:
    def test_saves_all_three_values(self):
        ext = _make_afc_extruder()
        ext.afc.function.ConfigRewrite = MagicMock()
        ext.cmd_SAVE_EXTRUDER_VALUES(MagicMock())
        calls = ext.afc.function.ConfigRewrite.call_args_list
        keys = [c[0][1] for c in calls]
        assert "tool_stn" in keys
        assert "tool_stn_unload" in keys
        assert "tool_sensor_after_extruder" in keys

    def test_saves_with_correct_fullname(self):
        ext = _make_afc_extruder("extruder")
        ext.afc.function.ConfigRewrite = MagicMock()
        ext.cmd_SAVE_EXTRUDER_VALUES(MagicMock())
        for call in ext.afc.function.ConfigRewrite.call_args_list:
            assert call[0][0] == "AFC_extruder extruder"


# ── get_status ────────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_returns_dict(self):
        ext = _make_afc_extruder()
        result = ext.get_status()
        assert isinstance(result, dict)

    def test_contains_tool_stn(self):
        ext = _make_afc_extruder()
        ext.tool_stn = 80.0
        result = ext.get_status()
        assert result["tool_stn"] == 80.0

    def test_contains_tool_stn_unload(self):
        ext = _make_afc_extruder()
        ext.tool_stn_unload = 120.0
        result = ext.get_status()
        assert result["tool_stn_unload"] == 120.0

    def test_contains_tool_sensor_after_extruder(self):
        ext = _make_afc_extruder()
        ext.tool_sensor_after_extruder = 5.0
        result = ext.get_status()
        assert result["tool_sensor_after_extruder"] == 5.0

    def test_contains_speeds(self):
        ext = _make_afc_extruder()
        result = ext.get_status()
        assert "tool_unload_speed" in result
        assert "tool_load_speed" in result

    def test_contains_sensor_states(self):
        ext = _make_afc_extruder()
        ext.tool_start_state = True
        ext.tool_end_state = False
        result = ext.get_status()
        assert result["tool_start_status"] is True
        assert result["tool_end_status"] is False

    def test_contains_lane_loaded(self):
        ext = _make_afc_extruder()
        ext.lane_loaded = "lane1"
        result = ext.get_status()
        assert result["lane_loaded"] == "lane1"

    def test_lanes_list_contains_lane_names(self):
        ext = _make_afc_extruder()
        lane = MagicMock()
        lane.name = "lane1"
        ext.lanes = {"lane1": lane}
        result = ext.get_status()
        assert "lane1" in result["lanes"]
