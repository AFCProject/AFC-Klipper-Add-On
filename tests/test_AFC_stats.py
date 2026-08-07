"""
Unit tests for extras/AFC_stats.py

Covers:
  - AFCStats_var: init, value retrieval, increment, reset, average_time,
    get_average, update_database, set_current_time, __str__, value property
"""

from __future__ import annotations

import sys
import importlib.util
import configparser
from unittest.mock import MagicMock, patch
import pytest

from extras.AFC import afc
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
        mr = make_moonraker()
        data = {"other_parent": {"cut_total": 5}}
        var = make_var("extruder", "cut_total", data=data, moonraker=mr)
        assert var.value == 0
        assert mr.logger.messages == [
            ("debug", "No data in database for extruder.cut_total:True"),
        ]

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

    def test_init_new_parent_already_has_data_loads_from_new_parent(self):
        """When new_parent_name's own key already exists in data (the stat
        was already migrated in an earlier run), the value must load from
        the new parent's key -- checked first, before old_parent is even
        looked at, and old_parent isn't present here at all."""
        mr = make_moonraker()
        data = {"new_parent": {"count": 8}}
        var = AFCStats_var("old_parent", "count", data, mr, new_parent_name="new_parent")
        assert var.value == 8

    def test_init_new_parent_rename_skipped_when_old_parent_key_absent(self):
        """The old-parent migration step (update_database + remove_database_
        entry) requires BOTH data is not None AND the old parent's key
        being present in data -- proven independently of
        test_init_new_parent_renames_and_deletes_old, which has both true.
        Here data is not None but old_parent's key isn't in it (only
        new_parent's is), so the migration step must be skipped."""
        mr = make_moonraker()
        mr.remove_database_entry = MagicMock()
        data = {"new_parent": {"count": 8}}
        AFCStats_var("old_parent", "count", data, mr, new_parent_name="new_parent")
        mr.remove_database_entry.assert_not_called()

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
        assert var.value == 0
        assert mr.logger.messages == [
            ("error", "Cannot have more than two parent names for stats"),
        ]

    def test_two_level_parent_missing_second_level_returns_zero(self):
        """Two-level parent with missing second key returns 0."""
        mr = make_moonraker()
        data = {"extruder": {}}  # second level key "cut" missing
        var = make_var("extruder.cut", "cut_total", data=data, moonraker=mr)
        assert var.value == 0
        assert mr.logger.messages == [
            ("debug", "No data in database for extruder.cut.cut_total:True"),
        ]

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
        assert logger.messages == [("debug", "Multiple tools detected for stats")]

    def test_new_average_calc_set_to_one_when_no_existing_data(self):
        stats, _, _ = _make_afc_stats()
        assert stats.new_average_calc.value == 1

    def test_new_average_calc_set_to_one_when_values_present_but_no_average_time_key(self):
        """new_average_calc defaulting to 1 requires `values is not None and
        "average_time" in values` to be False -- proven independently of
        the "no existing data at all" case (values is None there) by
        supplying non-empty stats data that simply lacks an "average_time"
        key."""
        stats, _, _ = _make_afc_stats(stats_data={"error_stats": {"last_load_error": "N/A"}})
        assert stats.new_average_calc.value == 1

    def test_new_average_calc_set_to_zero_when_average_time_in_db(self):
        # When "average_time" key exists in stats data, new_average_calc stays 0
        stats, mr, _ = _make_afc_stats(stats_data={"average_time": {"new_average_calc": 0}})
        assert stats.new_average_calc.value == 0

    def test_new_average_calc_not_reset_when_already_nonzero(self):
        """When new_average_calc already resolved to a non-zero value from
        the DB (already migrated in an earlier run), __init__ must not
        overwrite it or push another update to the database for it."""
        stats, mr, _ = _make_afc_stats(stats_data={"average_time": {"new_average_calc": 1}})
        assert stats.new_average_calc.value == 1
        matching_calls = [
            c for c in mr.update_afc_stats.call_args_list
            if c.args[0] == "average_time.new_average_calc"
        ]
        assert matching_calls == []

    def test_last_load_error_not_overwritten_when_already_set(self):
        """When error_stats.last_load_error already has a real value in the
        DB, __init__ must not overwrite it with the "N/A" placeholder."""
        stats, mr, _ = _make_afc_stats(
            stats_data={"error_stats": {"last_load_error": "2024-01-01 10:00"}}
        )
        assert stats.tc_last_load_error.value == "2024-01-01 10:00"

    def test_creates_total_load_errors_var(self):
        stats, _, _ = _make_afc_stats()
        assert hasattr(stats, "total_load_errors")

    def test_creates_total_unload_errors_var(self):
        stats, _, _ = _make_afc_stats()
        assert hasattr(stats, "total_unload_errors")

    def test_total_load_errors_defaults_to_zero(self):
        stats, _, _ = _make_afc_stats()
        assert stats.total_load_errors.value == 0

    def test_total_unload_errors_defaults_to_zero(self):
        stats, _, _ = _make_afc_stats()
        assert stats.total_unload_errors.value == 0

    def test_total_load_errors_loads_from_existing_data(self):
        stats, _, _ = _make_afc_stats(
            stats_data={"error_stats": {"total_load_errors": 5}}
        )
        assert stats.total_load_errors.value == 5

    def test_total_unload_errors_loads_from_existing_data(self):
        stats, _, _ = _make_afc_stats(
            stats_data={"error_stats": {"total_unload_errors": 3}}
        )
        assert stats.total_unload_errors.value == 3


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


class TestAFCStatsIncreaseLoadErrorCount:
    def test_increments_total_load_errors(self):
        stats, _, _ = _make_afc_stats()
        before = stats.total_load_errors.value
        stats.increase_load_error_count(MagicMock(testing=False))
        assert stats.total_load_errors.value == before + 1

    def test_resets_toolchange_wo_error_count(self):
        stats, _, _ = _make_afc_stats()
        stats.tc_without_error._value = 7
        stats.increase_load_error_count(MagicMock(testing=False))
        assert stats.tc_without_error.value == 0

    def test_sets_last_load_error_time(self):
        stats, _, _ = _make_afc_stats()
        stats.increase_load_error_count(MagicMock(testing=False))
        assert isinstance(stats.tc_last_load_error.value, str)
        assert len(stats.tc_last_load_error.value) > 3

    def test_does_not_increment_total_unload_errors(self):
        """Proves increase_load_error_count only touches the load counter,
        not the unload one -- distinguishes it from
        increase_unload_error_count, whose implementation is otherwise
        nearly identical."""
        stats, _, _ = _make_afc_stats()
        before = stats.total_unload_errors.value
        stats.increase_load_error_count(MagicMock(testing=False))
        assert stats.total_unload_errors.value == before

    def test_skipped_when_afc_testing_true(self):
        """When afc.testing is True, the function returns before touching
        any counters -- neither total_load_errors nor the
        reset_toolchange_wo_error side effects fire."""
        stats, _, _ = _make_afc_stats()
        stats.tc_without_error._value = 7
        before_total = stats.total_load_errors.value
        stats.increase_load_error_count(MagicMock(testing=True))
        assert stats.total_load_errors.value == before_total
        assert stats.tc_without_error.value == 7


class TestAFCStatsIncreaseUnloadErrorCount:
    def test_increments_total_unload_errors(self):
        stats, _, _ = _make_afc_stats()
        before = stats.total_unload_errors.value
        stats.increase_unload_error_count(MagicMock(testing=False))
        assert stats.total_unload_errors.value == before + 1

    def test_resets_toolchange_wo_error_count(self):
        stats, _, _ = _make_afc_stats()
        stats.tc_without_error._value = 7
        stats.increase_unload_error_count(MagicMock(testing=False))
        assert stats.tc_without_error.value == 0

    def test_sets_last_load_error_time(self):
        stats, _, _ = _make_afc_stats()
        stats.increase_unload_error_count(MagicMock(testing=False))
        assert isinstance(stats.tc_last_load_error.value, str)
        assert len(stats.tc_last_load_error.value) > 3

    def test_does_not_increment_total_load_errors(self):
        """Proves increase_unload_error_count only touches the unload
        counter, not the load one -- distinguishes it from
        increase_load_error_count, whose implementation is otherwise
        nearly identical."""
        stats, _, _ = _make_afc_stats()
        before = stats.total_load_errors.value
        stats.increase_unload_error_count(MagicMock(testing=False))
        assert stats.total_load_errors.value == before

    def test_skipped_when_afc_testing_true(self):
        """When afc.testing is True, the function returns before touching
        any counters -- neither total_unload_errors nor the
        reset_toolchange_wo_error side effects fire."""
        stats, _, _ = _make_afc_stats()
        stats.tc_without_error._value = 7
        before_total = stats.total_unload_errors.value
        stats.increase_unload_error_count(MagicMock(testing=True))
        assert stats.total_unload_errors.value == before_total
        assert stats.tc_without_error.value == 7


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
        afc_obj.units = {}
        return afc_obj

    def test_print_stats_calls_logger_raw(self):
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        stats.print_stats(afc_obj)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        # logger.raw(print_str) is called exactly once, unconditionally, at
        # the very end of print_stats.
        assert len(raw_msgs) == 1

    def test_print_stats_contains_overall_header(self):
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        stats.print_stats(afc_obj)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)
        assert "Overall Stats" in output

    def test_print_stats_contains_total_load_and_unload_error_counts(self):
        stats, _, logger = _make_afc_stats()
        stats.total_load_errors._value = 4
        stats.total_unload_errors._value = 2
        afc_obj = self._make_mock_afc()
        stats.print_stats(afc_obj)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)
        assert "Total Load Errors" in output
        assert "Total Unload Errors" in output
        # Confirm the actual counter values flow through into the output,
        # not just the static labels.
        assert f"{'Total Load Errors':{' '}>22} : {4:{' '}<17}" in output
        assert f"{'Total Unload Errors':{' '}>22} : {2:{' '}<17}" in output

    def test_print_stats_short_mode_calls_logger_raw(self):
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        stats.print_stats(afc_obj, short=True)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        # logger.raw(print_str) is called exactly once, unconditionally, at
        # the very end of print_stats.
        assert len(raw_msgs) == 1

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
        # short=False -> extruder_lbl is just the name, no " Toolchanges" suffix
        assert "extruder Toolchanges" not in output

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
        """short=True combined with multiple_tools exercises the tool
        selected/unselected rows and the short-mode cut-string append."""
        stats, _, logger = _make_afc_stats(multiple_tools=True)
        afc_obj = self._make_mock_afc()
        afc_obj.tools = {"extruder": self._make_ext_mock()}
        stats.print_stats(afc_obj, short=True)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)
        assert "Overall Stats" in output
        # short=True -> extruder_lbl gets a " Toolchanges" suffix
        assert "extruder Toolchanges" in output

    def test_print_stats_long_format_with_lane(self):
        """A single short lane string (<=60 chars) accumulates into temp_str
        and flushes via end_string() at the end of the lane loop."""
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        lane = MagicMock()
        lane.name = "lane1"
        lane.espooler.get_spooler_stats.return_value = ""  # empty espooler
        lane.lane_load_count.value = 3
        unit = self._make_unit("Turtle_1")
        unit.lanes = {"lane1": lane}
        afc_obj.lanes = {"lane1": lane}
        afc_obj.units = {"Turtle_1": unit}
        stats.print_stats(afc_obj, short=False)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        # logger.raw(print_str) is called exactly once, unconditionally, at
        # the very end of print_stats.
        assert len(raw_msgs) == 1

    def test_print_stats_long_format_with_lane_espooler_stats(self):
        """Non-empty espooler stats in long (non-short) format are appended
        inline with a leading four-space indent, not boxed like short mode."""
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        unit = MagicMock()
        unit.name = "Turtle_1"
        lane = MagicMock()
        lane.name = "lane1"
        lane.espooler.get_spooler_stats.return_value = "Spooler: 500g"  # non-empty
        lane.lane_load_count.value = 5
        unit.lanes = {"lane1": lane}
        afc_obj.units = {"Turtle_1": unit}
        afc_obj.lanes = {"lane1": lane}
        stats.print_stats(afc_obj, short=False)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)
        assert "lane1" in output

    def test_print_stats_short_format_with_lane_and_espooler(self):
        """short=True boxes the lane string and, when espooler stats are
        non-empty, boxes and appends those on their own line too."""
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        lane = MagicMock()
        lane.name = "lane1"
        lane.espooler.get_spooler_stats.return_value = "Spooler: 250g"  # non-empty
        lane.lane_load_count.value = 2
        unit = self._make_unit("Turtle_1")
        unit.lanes = {"lane1": lane}
        afc_obj.lanes = {"lane1": lane}
        afc_obj.units = {"Turtle_1": unit}
        stats.print_stats(afc_obj, short=True)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)
        # short mode (MAX_WIDTH=44) boxes the non-empty espooler stats on
        # their own line, centered within MAX_WIDTH-3=41 columns -- computed
        # by hand rather than re-deriving the source's own format spec.
        assert "|              Spooler: 250g              |\n" in output

    def test_print_stats_long_string_lane_uses_direct_print(self):
        """A lane string longer than 60 chars bypasses the temp_str
        accumulator and goes directly into print_str on its own line."""
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        lane = MagicMock()
        lane.name = "lane1"
        # Long espooler stats (>21 chars) pushes string length over 60
        lane.espooler.get_spooler_stats.return_value = "x" * 25
        lane.lane_load_count.value = 1
        unit = self._make_unit("Turtle_1")
        unit.lanes = {"lane1": lane}
        afc_obj.lanes = {"lane1": lane}
        afc_obj.units = {"Turtle_1": unit}
        stats.print_stats(afc_obj, short=False)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)
        # Computed by hand (not by re-deriving the source's own format
        # spec): max_lane_name_size is max(len("lane1"), 9) = 9, so the
        # name/count portion is "    lane1 : Lane change count:       1",
        # plus a fixed 2-space centering offset ((42 - 38) // 2) baked on
        # front so this label starts at the same column a short (no-
        # espooler) lane's would -- giving 6 leading spaces total -- then
        # the 25 "x"s appended with a 1-space indent, pushing the combined
        # string over 60 chars and into the direct-print branch (left-
        # justified within MAX_WIDTH-4=83, then "  |\n") instead of the
        # temp_str accumulator.
        assert (
            "|      lane1 : Lane change count:       1 "
            + "x" * 25 + " " * 17 + "  |\n"
        ) in output

    @staticmethod
    def _make_lane(name):
        lane = MagicMock()
        lane.name = name
        lane.espooler.get_spooler_stats.return_value = ""
        lane.lane_load_count.value = 1
        return lane
    
    @staticmethod
    def _make_unit(name):
        unit = MagicMock()
        unit.name = name
        unit.lanes = {}
        return unit

    def test_print_stats_short_format_lanes_print_in_insertion_order(self):
        """afc_obj.lanes is a plain dict; print_stats must iterate it in
        insertion order (dict iteration order in Python 3.7+), not re-sort
        lane names alphabetically -- lanes inserted out of numeric order
        must come out in that same out-of-order sequence."""
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        # Inserted out of numeric order on purpose.
        afc_obj.lanes = {
            "lane3": self._make_lane("lane3"),
            "lane4": self._make_lane("lane4"),
            "lane1": self._make_lane("lane1"),
            "lane2": self._make_lane("lane2"),
        }
        # Simulating where units and unit lanes will be in order and AFC lanes will not be in order
        afc_obj.units = {
            "Turtle_1": self._make_unit("Turtle_1"),
            "Turtle_2": self._make_unit("Turtle_2"),
        }
        afc_obj.units["Turtle_1"].lanes = {
            "lane1" : afc_obj.lanes["lane1"],
            "lane2" : afc_obj.lanes["lane2"]
        }
        afc_obj.units["Turtle_2"].lanes = {
            "lane3" : afc_obj.lanes["lane3"],
            "lane4" : afc_obj.lanes["lane4"]
        }
        stats.print_stats(afc_obj, short=True)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)

        positions = [output.index(name) for name in ("lane1", "lane2", "lane3", "lane4")]
        assert positions == sorted(positions)

    def test_print_stats_long_format_lanes_print_in_insertion_order(self):
        """Same as the short-format case, but through the long-format
        temp_str accumulator/end_string() path instead of a direct join, to
        prove that accumulation logic doesn't reorder lanes either."""
        stats, _, logger = _make_afc_stats()
        afc_obj = self._make_mock_afc()
        afc_obj.lanes = {
            "lane3": self._make_lane("lane3"),
            "lane4": self._make_lane("lane4"),
            "lane1": self._make_lane("lane1"),
            "lane2": self._make_lane("lane2"),
        }
        # Simulating where units and unit lanes will be in order and AFC lanes will not be in order
        afc_obj.units = {
            "Turtle_1": self._make_unit("Turtle_1"),
            "Turtle_2": self._make_unit("Turtle_2"),
        }
        afc_obj.units["Turtle_1"].lanes = {
            "lane1" : afc_obj.lanes["lane1"],
            "lane2" : afc_obj.lanes["lane2"]
        }
        afc_obj.units["Turtle_2"].lanes = {
            "lane3" : afc_obj.lanes["lane3"],
            "lane4" : afc_obj.lanes["lane4"]
        }
        stats.print_stats(afc_obj, short=False)
        raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
        output = "".join(raw_msgs)

        positions = [output.index(name) for name in ("lane1", "lane2", "lane3", "lane4")]
        assert positions == sorted(positions)

    @staticmethod
    def _spooler_stats_side_effect(fwd: str|None, rwd: str|None):
        """Calls the real, unmodified Espooler.get_spooler_stats (unbound,
        against a minimal `self` stand-in) instead of reimplementing its
        formatting, so this can't drift out of sync with the source again.
        fwd/rwd may each be None, mirroring a lane with only one motor pin
        defined."""
        from extras.AFC_assist import Espooler
        fake_self = MagicMock()
        fake_self.afc_motor_fwd = MagicMock() if fwd is not None else None
        fake_self.afc_motor_rwd = MagicMock() if rwd is not None else None
        fake_self.stats.n20_runtime_fwd = fwd
        fake_self.stats.n20_runtime_rwd = rwd

        def _side_effect(short):
            return Espooler.get_spooler_stats(fake_self, short)
        return _side_effect

    def _make_preview_afc(self):
        """Build a realistic-ish AFC mock: two units with two lanes each,
        two extruders, some non-zero/non-default stat values so the printed
        table actually has varied content to look at."""
        afc_obj = self._make_mock_afc()

        lane1 = self._make_lane("lane1")
        lane1.lane_load_count.value = 42
        lane1.espooler.get_spooler_stats.side_effect = self._spooler_stats_side_effect(
            "1123.45s", "187.32s"
        )
        lane2 = self._make_lane("lane2")
        lane2.lane_load_count.value = 17
        lane2.espooler.get_spooler_stats.side_effect = self._spooler_stats_side_effect(
            "123.45s", "87.32s"
        )
        lane3 = self._make_lane("lane3")
        lane3.lane_load_count.value = 5
        lane4 = self._make_lane("lane4")
        lane4.lane_load_count.value = 130
        lane5 = self._make_lane("lane5")
        lane5.lane_load_count.value = 130
        lane5.espooler.get_spooler_stats.side_effect = self._spooler_stats_side_effect(
            None, "187.32s"
        )
        lane6 = self._make_lane("lane6")
        lane6.lane_load_count.value = 130
        lane6.espooler.get_spooler_stats.side_effect = self._spooler_stats_side_effect(
            "1123.45s", None
        )

        unit1 = self._make_unit("Turtle_1")
        unit1.lanes = {"lane1": lane1, "lane2": lane2, "lane3": lane3}
        unit2 = self._make_unit("Turtle_2")
        unit2.lanes = {"lane4": lane4, "lane5": lane5, "lane6": lane6}
        afc_obj.units = {"Turtle_1": unit1, "Turtle_2": unit2}
        afc_obj.lanes = {"lane1": lane1, "lane2": lane2, "lane3": lane3, "lane4": lane4,
                        "lane5": lane5, "lane6": lane6}

        extruder = self._make_ext_mock("extruder")
        extruder.estats.tc_total.value = 214
        extruder.estats.tc_tool_unload.value = 108
        extruder.estats.tc_tool_load.value = 106
        extruder.estats.cut_total.value = 89
        extruder.estats.cut_total_since_changed.value = 12
        extruder.estats.cut_threshold_for_warning = 50
        extruder.estats.last_blade_changed.value = "2025-11-02 08:15"
        extruder.estats.tool_selected.value = 60
        extruder.estats.tool_unselected.value = 58
        afc_obj.tools = {"extruder": extruder}

        return afc_obj

    @pytest.mark.manual
    def test_print_stats_visual_preview(self, capsys):
        """Not a correctness check (the other tests in this class cover
        that) -- prints what print_stats actually renders so you can eyeball
        the formatting without needing a printer running. Marked "manual" so
        it's excluded from the normal test run (see addopts in pyproject.toml);
        run explicitly with:

            pytest tests/test_AFC_stats.py -k print_stats_visual_preview -s -m manual

        (the -s is required, otherwise pytest swallows the printed output).
        """
        stats, _, logger = _make_afc_stats(multiple_tools=True)
        stats.tc_without_error._value = 358
        stats.tc_last_load_error._value = "2025-10-15 14:32"

        with capsys.disabled():
            for label, short in (("LONG FORMAT (short=False)", False),
                                  ("SHORT FORMAT (short=True)", True)):
                afc_obj = self._make_preview_afc()
                stats.print_stats(afc_obj, short=short)
                raw_msgs = [m for lvl, m in logger.messages if lvl == "raw"]
                print(f"\n{'=' * 20} {label} {'=' * 20}")
                print(raw_msgs[-1])


# ═════════════════════════════════════════════════════════════════════════
# Module-level import guard
# ═════════════════════════════════════════════════════════════════════════

def _exec_afc_stats_with_blocked_dependency(blocked_module_name):
    """Execute a throw-away copy of extras/AFC_stats.py's module-level code
    with `blocked_module_name` forced to fail import, to exercise the file's
    top-level ``try: from X import Y / except: raise error(...)`` guard.

    This never touches the real, already-imported ``extras.AFC_stats``
    module that the rest of this test suite depends on: the copy is loaded
    under a throwaway module name and discarded afterward, whether or not it
    raises. Blocking an import via ``sys.modules[name] = None`` is a standard
    Python mechanism -- it makes any ``import``/``from ... import`` of that
    name raise ImportError immediately, without touching the module itself.

    Cleanup restores the *exact same* pre-existing module object in
    sys.modules (not just removes the block) -- simply deleting the entry
    would let it get re-imported fresh the next time anything touches it,
    producing new, distinct class objects that no longer match what other
    test files already imported and bound references to.
    """
    import extras.AFC_stats as real_module
    fresh_name = "extras.AFC_stats_import_guard_probe"
    original_blocked_module = sys.modules.get(blocked_module_name)
    sys.modules[blocked_module_name] = None
    try:
        spec = importlib.util.spec_from_file_location(fresh_name, real_module.__file__)
        fresh = importlib.util.module_from_spec(spec)
        sys.modules[fresh_name] = fresh
        try:
            spec.loader.exec_module(fresh)
        finally:
            sys.modules.pop(fresh_name, None)
    finally:
        if original_blocked_module is not None:
            sys.modules[blocked_module_name] = original_blocked_module
        else:
            sys.modules.pop(blocked_module_name, None)


class TestModuleImportGuard:
    """Covers the single module-level `try/except: raise error(...)` guard
    in AFC_stats.py, around its import of AFC_utils.check_and_return."""

    def test_afc_utils_import_failure_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_stats_with_blocked_dependency("extras.AFC_utils")
        assert str(exc_info.value).startswith(
            "Error when trying to import AFC_utils.check_and_return"
        )
