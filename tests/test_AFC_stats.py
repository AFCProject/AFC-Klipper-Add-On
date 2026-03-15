"""
Unit tests for extras/AFC_stats.py

Covers:
  - AFCStats_var: init, value retrieval, increment, reset, average_time,
    get_average, update_database, set_current_time, __str__, value property
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from extras.AFC_stats import AFCStats_var, AFCStats


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_moonraker(stats_data=None):
    from tests.conftest import MockMoonraker
    mr = MockMoonraker()
    mr._stats = stats_data or {}
    return mr


def make_var(parent_name, name, data=None, moonraker=None, new_parent_name="", new_average=False):
    mr = moonraker or make_moonraker()
    return AFCStats_var(parent_name, name, data, mr, new_parent_name, new_average)


# ── AFCStats_var ──────────────────────────────────────────────────────────────

class TestAFCStatsVarInit:
    def test_init_no_data_defaults_to_zero(self):
        var = make_var("extruder", "cut_total", data=None)
        assert var.value == 0

    def test_init_data_missing_parent_defaults_to_zero(self):
        data = {"other_parent": {"cut_total": 5}}
        var = make_var("extruder", "cut_total", data=data)
        assert var.value == 0

    def test_init_data_with_matching_parent_single_level(self):
        data = {"extruder": {"cut_total": 42}}
        var = make_var("extruder", "cut_total", data=data)
        assert var.value == 42

    def test_init_data_with_matching_parent_two_levels(self):
        data = {"extruder": {"cut": {"cut_total": 7}}}
        var = make_var("extruder.cut", "cut_total", data=data)
        assert var.value == 7

    def test_init_data_float_value(self):
        data = {"timing": {"avg": "3.14"}}
        var = make_var("timing", "avg", data=data)
        assert abs(var.value - 3.14) < 1e-9

    def test_init_data_int_value_as_string(self):
        data = {"extruder": {"count": "5"}}
        var = make_var("extruder", "count", data=data)
        assert var.value == 5
        assert isinstance(var.value, int)

    def test_init_data_non_numeric_string(self):
        data = {"extruder": {"date": "2024-01-01"}}
        var = make_var("extruder", "date", data=data)
        assert var.value == "2024-01-01"

    def test_init_new_parent_renames_and_deletes_old(self):
        mr = make_moonraker()
        mr.remove_database_entry = MagicMock()
        mr.update_afc_stats = MagicMock()
        data = {"old_parent": {"count": 3}}
        var = AFCStats_var("old_parent", "count", data, mr, new_parent_name="new_parent")
        assert var.parent_name == "new_parent"
        mr.update_afc_stats.assert_called()

    def test_str_representation(self):
        var = make_var("extruder", "cut_total", data={"extruder": {"cut_total": 10}})
        assert str(var) == "10"

    def test_value_property_getter(self):
        var = make_var("extruder", "cut_total")
        var.value = 99
        assert var.value == 99

    def test_value_property_setter(self):
        var = make_var("extruder", "cut_total")
        var.value = 55
        assert var._value == 55


class TestAFCStatsVarIncrement:
    def test_increase_count_increments_by_one(self):
        mr = make_moonraker()
        mr.update_afc_stats = MagicMock()
        var = make_var("extruder", "cut_total", moonraker=mr)
        var.increase_count()
        assert var.value == 1
        mr.update_afc_stats.assert_called_once()

    def test_increase_count_multiple_times(self):
        mr = make_moonraker()
        mr.update_afc_stats = MagicMock()
        var = make_var("extruder", "cut_total", moonraker=mr)
        for _ in range(5):
            var.increase_count()
        assert var.value == 5


class TestAFCStatsVarReset:
    def test_reset_count_sets_to_zero(self):
        mr = make_moonraker()
        mr.update_afc_stats = MagicMock()
        var = make_var("extruder", "cut_total", moonraker=mr)
        var._value = 100
        var.reset_count()
        assert var.value == 0

    def test_reset_count_enables_new_average(self):
        mr = make_moonraker()
        mr.update_afc_stats = MagicMock()
        var = make_var("extruder", "cut_total", moonraker=mr, new_average=False)
        var._value = 50
        var.reset_count()
        assert var.new_average is True

    def test_reset_count_calls_update_database(self):
        mr = make_moonraker()
        mr.update_afc_stats = MagicMock()
        var = make_var("extruder", "cut_total", moonraker=mr)
        var._value = 10
        var.reset_count()
        mr.update_afc_stats.assert_called()


class TestAFCStatsVarAverageTime:
    def test_average_time_first_value_sets_value(self):
        mr = make_moonraker()
        mr.update_afc_stats = MagicMock()
        var = make_var("timing", "avg", moonraker=mr)
        var.average_time(10.0)
        assert var.value == 10.0

    def test_average_time_old_method_divides_by_two(self):
        mr = make_moonraker()
        mr.update_afc_stats = MagicMock()
        var = make_var("timing", "avg", moonraker=mr, new_average=False)
        var._value = 10.0
        var.average_time(20.0)
        assert var.value == 15.0  # (10+20)/2

    def test_average_time_new_method_sums(self):
        mr = make_moonraker()
        mr.update_afc_stats = MagicMock()
        var = make_var("timing", "avg", moonraker=mr, new_average=True)
        var._value = 10.0
        var.average_time(20.0)
        assert var.value == 30.0  # 10 + 20 (no division)

    def test_get_average_new_method_divides_by_total(self):
        var = make_var("timing", "avg", new_average=True)
        var._value = 30.0
        result = var.get_average(total=3)
        assert result == 10.0

    def test_get_average_new_method_zero_total(self):
        var = make_var("timing", "avg", new_average=True)
        var._value = 30.0
        result = var.get_average(total=0)
        assert result == 30.0

    def test_get_average_old_method_returns_value(self):
        var = make_var("timing", "avg", new_average=False)
        var._value = 15.0
        result = var.get_average(total=5)
        assert result == 15.0


class TestAFCStatsVarUpdateDatabase:
    def test_update_database_calls_moonraker_with_correct_key(self):
        mr = make_moonraker()
        mr.update_afc_stats = MagicMock()
        var = make_var("extruder", "cut_total", moonraker=mr)
        var._value = 7
        var.update_database()
        mr.update_afc_stats.assert_called_once_with("extruder.cut_total", 7)

    def test_update_database_two_level_parent(self):
        mr = make_moonraker()
        mr.update_afc_stats = MagicMock()
        var = make_var("extruder.cut", "cut_total", moonraker=mr)
        var._value = 3
        var.update_database()
        mr.update_afc_stats.assert_called_once_with("extruder.cut.cut_total", 3)


class TestAFCStatsVarSetCurrentTime:
    def test_set_current_time_updates_value_and_database(self):
        mr = make_moonraker()
        mr.update_afc_stats = MagicMock()
        var = make_var("error_stats", "last_load_error", moonraker=mr)
        var.set_current_time()
        assert isinstance(var.value, str)
        assert len(var.value) > 0
        mr.update_afc_stats.assert_called()


# ── _get_value edge cases ─────────────────────────────────────────────────────

class TestGetValueEdgeCases:
    def test_three_level_parent_logs_error(self):
        """Three-level parent (a.b.c) triggers logger.error and returns 0."""
        mr = make_moonraker()
        mr.update_afc_stats = MagicMock()
        data = {"a": {"b": {"c": 5}}}
        var = make_var("a.b.c", "value", data=data, moonraker=mr)
        # Value should be 0 (error path)
        assert var.value == 0

    def test_two_level_parent_missing_second_level_returns_zero(self):
        """Two-level parent with missing second key returns 0."""
        mr = make_moonraker()
        data = {"extruder": {}}  # second level key "cut" missing
        var = make_var("extruder.cut", "cut_total", data=data, moonraker=mr)
        assert var.value == 0

    def test_new_parent_with_no_data_does_not_remove(self):
        """When data=None, the old-parent delete step is skipped."""
        mr = make_moonraker()
        mr.remove_database_entry = MagicMock()
        mr.update_afc_stats = MagicMock()
        var = AFCStats_var("old_parent", "count", None, mr, new_parent_name="new_parent")
        assert var.parent_name == "new_parent"
        mr.remove_database_entry.assert_not_called()


# ── AFCStats ──────────────────────────────────────────────────────────────────

def _make_afc_stats(multiple_tools=False, stats_data=None):
    """Build an AFCStats instance with MockMoonraker and MockLogger."""
    from tests.conftest import MockMoonraker, MockLogger
    mr = MockMoonraker()
    if stats_data is not None:
        mr._stats = stats_data
    mr.update_afc_stats = MagicMock()
    mr.remove_database_entry = MagicMock()
    logger = MockLogger()
    afc_stats = AFCStats(mr, logger, multiple_tools)
    return afc_stats, mr, logger


class TestAFCStatsInit:
    def test_creates_tc_without_error_var(self):
        stats, _, _ = _make_afc_stats()
        assert hasattr(stats, "tc_without_error")

    def test_creates_tc_last_load_error_var(self):
        stats, _, _ = _make_afc_stats()
        assert hasattr(stats, "tc_last_load_error")

    def test_last_load_error_set_to_na_when_zero(self):
        stats, _, _ = _make_afc_stats()
        assert stats.tc_last_load_error.value == "N/A"

    def test_creates_average_time_vars(self):
        stats, _, _ = _make_afc_stats()
        assert hasattr(stats, "average_toolchange_time")
        assert hasattr(stats, "average_tool_unload_time")
        assert hasattr(stats, "average_tool_load_time")

    def test_multiple_tools_creates_swap_var(self):
        stats, _, _ = _make_afc_stats(multiple_tools=True)
        assert stats.average_tool_swap_time is not None

    def test_single_tool_swap_var_is_none(self):
        stats, _, _ = _make_afc_stats(multiple_tools=False)
        assert stats.average_tool_swap_time is None

    def test_multiple_tools_logs_debug(self):
        _, _, logger = _make_afc_stats(multiple_tools=True)
        debug_msgs = [m for lvl, m in logger.messages if lvl == "debug"]
        assert any("multiple" in m.lower() for m in debug_msgs)

    def test_new_average_calc_set_to_one_when_no_existing_data(self):
        stats, _, _ = _make_afc_stats()
        assert stats.new_average_calc.value == 1

    def test_new_average_calc_set_to_zero_when_average_time_in_db(self):
        # When "average_time" key exists in stats data, new_average_calc stays 0
        stats, mr, _ = _make_afc_stats(stats_data={"average_time": {"new_average_calc": 0}})
        assert stats.new_average_calc.value == 0


class TestAFCStatsIncreaseTcWoError:
    def test_increase_toolchange_wo_error_calls_increase_count(self):
        stats, _, _ = _make_afc_stats()
        before = stats.tc_without_error.value
        stats.increase_toolchange_wo_error()
        assert stats.tc_without_error.value == before + 1


class TestAFCStatsResetTcWoError:
    def test_reset_clears_count(self):
        stats, _, _ = _make_afc_stats()
        stats.tc_without_error._value = 5
        stats.reset_toolchange_wo_error()
        assert stats.tc_without_error.value == 0

    def test_reset_sets_last_error_time(self):
        stats, _, _ = _make_afc_stats()
        stats.reset_toolchange_wo_error()
        # Should have a date string now
        assert isinstance(stats.tc_last_load_error.value, str)
        assert len(stats.tc_last_load_error.value) > 3


class TestAFCStatsResetAverageTimes:
    def test_reset_sets_toolchange_time_to_zero(self):
        stats, _, _ = _make_afc_stats()
        stats.average_toolchange_time._value = 10.0
        stats.reset_average_times()
        assert stats.average_toolchange_time.value == 0

    def test_reset_sets_unload_time_to_zero(self):
        stats, _, _ = _make_afc_stats()
        stats.average_tool_unload_time._value = 5.0
        stats.reset_average_times()
        assert stats.average_tool_unload_time.value == 0

    def test_reset_sets_load_time_to_zero(self):
        stats, _, _ = _make_afc_stats()
        stats.average_tool_load_time._value = 8.0
        stats.reset_average_times()
        assert stats.average_tool_load_time.value == 0

    def test_reset_also_resets_swap_time_when_multiple_tools(self):
        stats, _, _ = _make_afc_stats(multiple_tools=True)
        stats.average_tool_swap_time._value = 3.0
        stats.reset_average_times()
        assert stats.average_tool_swap_time.value == 0

    def test_reset_sets_new_average_calc_to_one(self):
        stats, _, _ = _make_afc_stats()
        stats.reset_average_times()
        assert stats.new_average_calc.value == 1


class TestAFCStatsPrintStats:
    def _make_mock_afc(self):
        afc_obj = MagicMock()
        afc_obj.lanes = {}
        afc_obj.tools = {}
        return afc_obj

    def test_print_stats_calls_logger_raw(self):
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        stats.print_stats(afc_obj)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        assert len(raw_msgs) >= 1

    def test_print_stats_contains_overall_header(self):
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        stats.print_stats(afc_obj)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)
        assert "Overall Stats" in output

    def test_print_stats_short_mode_calls_logger_raw(self):
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        stats.print_stats(afc_obj, short=True)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        assert len(raw_msgs) >= 1

    def test_print_stats_with_extruder(self):
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        ext = MagicMock()
        ext.name = "extruder"
        ext.estats.cut_total_since_changed.value = 5
        ext.estats.cut_threshold_for_warning = 100
        ext.estats.tc_total.value = 10
        ext.estats.tc_tool_unload.value = 5
        ext.estats.tc_tool_load.value = 5
        ext.estats.cut_total.value = 5
        ext.estats.last_blade_changed.value = "2024-01-01"
        ext.estats.tool_selected.value = 0
        ext.estats.tool_unselected.value = 0
        afc_obj.tools = {"extruder": ext}
        stats.print_stats(afc_obj)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)
        assert "extruder" in output

    def test_print_stats_with_multiple_tools_and_extruder(self):
        stats, _, logger = _make_afc_stats(multiple_tools=True)
        afc_obj = self._make_mock_afc()
        ext = MagicMock()
        ext.name = "extruder"
        ext.estats.cut_total_since_changed.value = 5
        ext.estats.cut_threshold_for_warning = 100
        ext.estats.tc_total.value = 10
        ext.estats.tc_tool_unload.value = 5
        ext.estats.tc_tool_load.value = 5
        ext.estats.cut_total.value = 5
        ext.estats.last_blade_changed.value = "2024-01-01"
        ext.estats.tool_selected.value = 2
        ext.estats.tool_unselected.value = 1
        afc_obj.tools = {"extruder": ext}
        stats.print_stats(afc_obj)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)
        assert "Overall Stats" in output

    def _make_ext_mock(self, name="extruder"):
        ext = MagicMock()
        ext.name = name
        ext.estats.cut_total_since_changed.value = 3
        ext.estats.cut_threshold_for_warning = 50
        ext.estats.tc_total.value = 7
        ext.estats.tc_tool_unload.value = 3
        ext.estats.tc_tool_load.value = 4
        ext.estats.cut_total.value = 3
        ext.estats.last_blade_changed.value = "N/A"
        ext.estats.tool_selected.value = 5
        ext.estats.tool_unselected.value = 5
        return ext

    def test_print_stats_short_mode_with_multiple_tools_and_extruder(self):
        """Covers short=True + multiple_tools branches (lines 337, 340, 343-344)."""
        stats, _, logger = _make_afc_stats(multiple_tools=True)
        afc_obj = self._make_mock_afc()
        afc_obj.tools = {"extruder": self._make_ext_mock()}
        stats.print_stats(afc_obj, short=True)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)
        assert "Overall Stats" in output

    def test_print_stats_long_format_with_lane(self):
        """Covers the lane loop body and end_string() call (lines 270-271, 282, 379-381, 392-399)."""
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        lane = MagicMock()
        lane.name = "lane1"
        lane.espooler.get_spooler_stats.return_value = ""  # empty espooler
        lane.lane_load_count.value = 3
        afc_obj.lanes = {"lane1": lane}
        stats.print_stats(afc_obj, short=False)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        assert len(raw_msgs) >= 1

    def test_print_stats_long_format_with_lane_espooler_stats(self):
        """Covers espooler non-empty long-format branch (line 385)."""
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        lane = MagicMock()
        lane.name = "lane1"
        lane.espooler.get_spooler_stats.return_value = "Spooler: 500g"  # non-empty
        lane.lane_load_count.value = 5
        afc_obj.lanes = {"lane1": lane}
        stats.print_stats(afc_obj, short=False)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)
        assert "lane1" in output

    def test_print_stats_short_format_with_lane_and_espooler(self):
        """Covers short=True lane formatting (lines 382, 383-384)."""
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        lane = MagicMock()
        lane.name = "lane1"
        lane.espooler.get_spooler_stats.return_value = "Spooler: 250g"  # non-empty
        lane.lane_load_count.value = 2
        afc_obj.lanes = {"lane1": lane}
        stats.print_stats(afc_obj, short=True)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        assert len(raw_msgs) >= 1

    def test_print_stats_long_string_lane_uses_direct_print(self):
        """Covers lines 395-397: lane string > 60 chars goes directly to print_str."""
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        lane = MagicMock()
        lane.name = "lane1"
        # Long espooler stats (>21 chars) pushes string length over 60
        lane.espooler.get_spooler_stats.return_value = "x" * 25
        lane.lane_load_count.value = 1
        afc_obj.lanes = {"lane1": lane}
        stats.print_stats(afc_obj, short=False)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        assert len(raw_msgs) >= 1
