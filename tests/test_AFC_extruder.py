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
from configparser import Error as KlipperError
import pytest
import sys
import types

from extras.AFC_extruder import AFCExtruderStats, AFCExtruder
from extras.AFC_utils import PollingEndstopWrapper
from extras.AFC import State
from tests.conftest import MockGCodeCommand
from tests.test_AFC_lane import _make_afc_lane, AFCLaneState
# ── Helpers ─────────────────────────────────────────────────────────

def _make_extruder_obj(name="extruder"):
    """Minimal AFCExtruder-like mock."""
    from tests.conftest import MockAFC, MockLogger
    afc = MockAFC()
    afc.afc_stats = MagicMock()
    obj = MagicMock()
    obj.th_extruder_name = obj.name = name
    obj.afc = afc
    obj.logger = MockLogger()
    obj.park_detector_obj = None
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
    from tests.conftest import MockAFC, MockPrinter, MockLogger, MockReactor, MockConfig

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
    ext.th_extruder_name = ext.name = name
    # Toolchanger fields – default mirrors single-toolhead state
    ext.tool_obj     = None
    ext.tc_unit_name = None
    ext.tool         = None
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
    ext.enable_runout = True
    ext.enable_runout_in_bypass = False
    ext.common_save_msg = f"\nRun SAVE_EXTRUDER_VALUES EXTRUDER={name} once done."
    ext.estats = MagicMock()
    ext.function = afc.function
    ext.park_detector = None
    ext.park_detector_obj = None
    ext.tc_name = None
    ext.no_lanes = False
    ext.next_pickup = False
    ext.status = State.IDLE
    
    # Toolchanger stuff
    ext.tool_obj = None
    ext.tc_unit_name = None
    ext.tool = None
    ext.toolhead_leds = None
    ext.mutex = MagicMock()
    return ext

def _make_afc_extruder_as_standalone(name="extruder", extruder_values=None, afc_values=None):
    """Build an AFCExtruder bypassing __init__."""
    from extras import AFC
    from tests.conftest import MockAFC, MockPrinter, MockLogger, MockReactor, MockConfig

    extruder_values = extruder_values or {}
    afc_values = afc_values or {}

    afc_config = MockConfig("AFC", MockPrinter())
    afc = AFC.load_config(afc_config)
    for key, value in afc_values.items():
        if hasattr(afc, key):
            setattr(afc, key, value)

    reactor = MockReactor()
    printer = MockPrinter(afc=afc)
    printer._reactor = reactor
    
    config = MockConfig(f"AFC_extruder {name}", printer, extruder_values)

    ext = AFCExtruder(config)

    ext.logger = MockLogger()
    return ext

# ── AFCExtruder.__str__ ────────────────────────────────────────────────────────

# -- pin_tool_start: virtual -----------------------------------------------------

class TestVirtualToolStart:
    def _make_virtual_extruder(self):
        # A virtual tool_start sensor means there is no physical AFC feeder at this
        # toolhead -- the GUI switch is the whole state, so standalone_auto_load_unload
        # is disabled: the state-toggle path completes synchronously instead of trying
        # to heat/move filament that no hardware will ever act on.
        return _make_afc_extruder_as_standalone(
            "extruder", extruder_values={"pin_tool_start": "virtual",
                                         "standalone_auto_load_unload": False})

    def _make_standalone_virtual(self):
        """Virtual-sensor extruder wired like a standalone toolchanger lane."""
        ext = self._make_virtual_extruder()
        ext.lanes.update({"extruder": ext})
        ext.tc_unit_name = "AFC_Toolchanger tc"
        ext.tc_lane = _make_afc_lane()
        ext.tc_lane.extruder_obj = ext
        # PREP has already run and no print is active, so tool_start_callback's auto
        # load/unload logic engages instead of being a no-op for the sensor-toggle
        # tests below. afc.function is a bare MagicMock here (a real afc's function
        # section isn't loaded by MockPrinter), so is_printing() must be pinned down
        # -- otherwise it returns a truthy Mock and actively_printing short-circuits
        # the load/unload path for every one of these tests.
        ext.tc_lane._afc_prep_done = True
        ext.afc.function.is_printing.return_value = False
        ext.handle_ready()
        assert ext.no_lanes is True
        return ext

    def test_virtual_tool_start_starts_unloaded_and_disabled(self):
        ext = self._make_virtual_extruder()
        assert ext.tool_start_state is False
        assert ext.fila_tool_start is not None
        helper = ext.fila_tool_start.runout_helper
        assert helper.filament_present is False
        assert helper.sensor_enabled is False

    def test_virtual_tool_start_registers_no_debounce_button(self):
        ext = self._make_virtual_extruder()
        assert getattr(ext, "debounce_button_start", None) is None

    def test_virtual_tool_start_wires_enable_callback(self):
        ext = self._make_virtual_extruder()
        assert ext.fila_tool_start.enable_cb == ext._virtual_tool_start_toggle

    def test_standalone_ready_keeps_lane_unloaded_with_virtual_sensor(self):
        ext = self._make_standalone_virtual()
        assert ext.tc_lane._load_state is False
        assert ext.tc_lane.prep_state is False

    def test_set_filament_sensor_enable_loads_lane(self):
        ext = self._make_standalone_virtual()
        ext.afc.prep_done = True
        ext.afc.save_vars = MagicMock()

        ext.fila_tool_start.cmd_SET_FILAMENT_SENSOR(MockGCodeCommand(params={"ENABLE": 1}))

        assert ext.tool_start_state is True
        helper = ext.fila_tool_start.runout_helper
        assert helper.sensor_enabled is True
        assert helper.filament_present is True
        assert ext.tc_lane._load_state is True
        assert ext.tc_lane.prep_state is True
        assert ext.tc_lane.extruder_obj.lane_loaded == ext.tc_lane.name
        # Saved twice: once by tool_start_callback's own auto-load-triggered save,
        # once more by _virtual_tool_start_toggle's own `if self.afc.prep_done` save.
        assert ext.afc.save_vars.call_count == 2

    def test_set_filament_sensor_disable_unloads_lane(self):
        ext = self._make_standalone_virtual()
        ext.restore_virtual_tool_start(True)

        ext.fila_tool_start.cmd_SET_FILAMENT_SENSOR(MockGCodeCommand(params={"ENABLE": 0}))

        assert ext.tool_start_state is False
        helper = ext.fila_tool_start.runout_helper
        assert helper.sensor_enabled is False
        assert helper.filament_present is False
        assert ext.tc_lane._load_state is False
        assert ext.tc_lane.prep_state is False

    def test_toggle_same_state_is_noop_for_tool_start_callback(self):
        """tool_start_callback itself is a no-op when the sensor state doesn't
        change (no auto load/unload triggered), but _virtual_tool_start_toggle
        still persists once on its own since afc.prep_done is True here."""
        ext = self._make_standalone_virtual()
        ext.afc.prep_done = True
        ext.afc.save_vars = MagicMock()

        ext.fila_tool_start.cmd_SET_FILAMENT_SENSOR(MockGCodeCommand(params={"ENABLE": 0}))

        assert ext.tool_start_state is False
        ext.afc.save_vars.assert_called_once()

    def test_toggle_skips_save_vars_before_lane_prep_done(self):
        """tool_start_callback (reached via the toggle) gates its whole
        auto load/unload chain -- save_vars included -- on the lane's own
        _afc_prep_done, not the global afc.prep_done. Before that lane's
        prep has completed, toggling the switch is pure bookkeeping."""
        ext = self._make_standalone_virtual()
        ext.tc_lane._afc_prep_done = False
        ext.afc.save_vars = MagicMock()

        ext.fila_tool_start.cmd_SET_FILAMENT_SENSOR(MockGCodeCommand(params={"ENABLE": 1}))

        assert ext.tool_start_state is True
        ext.afc.save_vars.assert_not_called()

    def test_restore_from_vars_enables_sensor_and_lane(self):
        ext = self._make_standalone_virtual()

        ext.restore_virtual_tool_start(True)

        assert ext.tool_start_state is True
        assert ext.fila_tool_start.runout_helper.filament_present is True
        assert ext.fila_tool_start.runout_helper.sensor_enabled is True
        assert ext.tc_lane._load_state is True
        assert ext.tc_lane.prep_state is True

    def test_restore_defaults_to_disabled_noop(self):
        ext = self._make_standalone_virtual()

        ext.restore_virtual_tool_start(False)

        assert ext.tool_start_state is False
        assert ext.tc_lane._load_state is False

    def test_temp_check_cb_load_completes_and_syncs_virtual_sensor(self):
        ext = self._make_standalone_virtual()
        # A load is in progress: the switch was already toggled on (tool_start_state
        # True), and temp_check_cb is now firing once the hotend reaches temp.
        ext.tool_start_state = True
        heater = MagicMock()
        heater.get_temp.return_value = (200.0, 200.0)
        ext.get_heater = MagicMock(return_value=heater)
        ext.move_extruder = MagicMock()
        ext.current_move_distance = 72.0

        ret = ext.temp_check_cb(0.0)

        assert ret == ext.reactor.NEVER
        ext.move_extruder.assert_called_once_with(72.0)
        assert ext.tool_start_state is True
        assert ext.fila_tool_start.runout_helper.filament_present is True
        assert ext.tc_lane._load_state is True

    def test_temp_check_cb_unload_clears_virtual_sensor(self):
        ext = self._make_standalone_virtual()
        ext.restore_virtual_tool_start(True)
        heater = MagicMock()
        heater.get_temp.return_value = (200.0, 200.0)
        ext.get_heater = MagicMock(return_value=heater)
        ext.move_extruder = MagicMock()
        ext.current_move_distance = -100.0

        ext.temp_check_cb(0.0)

        assert ext.tool_start_state is False
        assert ext.fila_tool_start.runout_helper.filament_present is False
        assert ext.tc_lane._load_state is False


# -- u1_filament_sensor_name: a U1-style motion sensor -------------------------

class TestU1ToolStartEndstop:
    def _make_u1_extruder(self):
        return _make_afc_extruder_as_standalone(
            extruder_values={"u1_filament_sensor_name": "e0_filament"})

    def test_builds_a_polling_endstop(self):
        ext = self._make_u1_extruder()
        assert ext.tool_start is None
        assert isinstance(ext.tool_start_endstop, PollingEndstopWrapper)

    def test_endstop_trigger_reads_runout_buttun_state(self):
        ext = self._make_u1_extruder()
        ext.fila_tool_start.runout_buttun_state = True
        assert ext.tool_start_endstop._trigger_func() is True
        ext.fila_tool_start.runout_buttun_state = False
        assert ext.tool_start_endstop._trigger_func() is False

    def test_endstop_get_mcu_uses_printers_mcu(self):
        ext = self._make_u1_extruder()
        mcu = ext.printer.lookup_object('mcu')
        assert ext.tool_start_endstop.get_mcu() is mcu

    def test_no_endstop_when_no_tool_start_configured(self):
        ext = _make_afc_extruder_as_standalone()
        assert ext.tool_start is None
        assert ext.fila_tool_start is None
        assert ext.tool_start_endstop is None

    def test_no_endstop_for_plain_switch_pin(self):
        ext = _make_afc_extruder_as_standalone(
            extruder_values={"pin_tool_start": "PD3"})
        assert ext.tool_start_endstop is None

    def _make_u1_extruder_with_query_endstops(self, query_endstops):
        from extras import AFC
        from tests.conftest import MockPrinter, MockConfig

        afc_config = MockConfig("AFC", MockPrinter())
        afc = AFC.load_config(afc_config)
        printer = MockPrinter(afc=afc)
        printer.objects["query_endstops"] = query_endstops
        config = MockConfig(
            "AFC_extruder extruder", printer,
            {"u1_filament_sensor_name": "e0_filament"})
        return AFCExtruder(config)

    def test_registers_with_query_endstops(self):
        query_endstops = MagicMock()

        ext = self._make_u1_extruder_with_query_endstops(query_endstops)

        query_endstops.register_endstop.assert_called_once_with(
            ext.tool_start_endstop, "extruder_tool_start")

    def test_registration_failure_raises_wrapped_error(self):
        query_endstops = MagicMock()
        query_endstops.register_endstop.side_effect = RuntimeError("boom")

        with pytest.raises(Exception):
            self._make_u1_extruder_with_query_endstops(query_endstops)


class TestGetToolStartEndstopForLane:
    """Covers get_tool_start_endstop_for_lane, the fix for lanes sharing one
    extruder's U1 sensor piling every lane's stepper onto one shared
    PollingEndstopWrapper: each lane name must get its own clone."""

    def _make_extruder_with_endstop(self, query_endstops=None):
        ext = _make_afc_extruder()
        ext.tool_start_endstop = PollingEndstopWrapper(
            ext.reactor, lambda: MagicMock(), lambda: False)
        ext._lane_tool_start_endstops = {}
        ext.printer.objects["query_endstops"] = query_endstops or MagicMock()
        return ext

    def test_returns_none_when_extruder_has_no_u1_endstop(self):
        ext = _make_afc_extruder()
        ext.tool_start_endstop = None
        ext._lane_tool_start_endstops = {}

        assert ext.get_tool_start_endstop_for_lane("lane1") is None

    def test_creates_and_registers_a_per_lane_clone(self):
        query_endstops = MagicMock()
        ext = self._make_extruder_with_endstop(query_endstops)

        result = ext.get_tool_start_endstop_for_lane("lane1")

        assert isinstance(result, PollingEndstopWrapper)
        assert result is not ext.tool_start_endstop
        query_endstops.register_endstop.assert_called_once_with(result, "lane1_tool_start")

    def test_caches_the_clone_per_lane_name(self):
        query_endstops = MagicMock()
        ext = self._make_extruder_with_endstop(query_endstops)

        first = ext.get_tool_start_endstop_for_lane("lane1")
        second = ext.get_tool_start_endstop_for_lane("lane1")

        assert first is second
        query_endstops.register_endstop.assert_called_once_with(first, "lane1_tool_start")

    def test_different_lanes_get_distinct_clones_with_isolated_steppers(self):
        """The actual bug this fixes: two lanes sharing one extruder's U1
        sensor must not end up with each other's stepper in get_steppers()."""
        ext = self._make_extruder_with_endstop()

        lane1_endstop = ext.get_tool_start_endstop_for_lane("lane1")
        lane2_endstop = ext.get_tool_start_endstop_for_lane("lane2")
        lane1_stepper = MagicMock()
        lane1_endstop.add_stepper(lane1_stepper)

        assert lane1_endstop is not lane2_endstop
        assert lane1_endstop.get_steppers() == [lane1_stepper]
        assert lane2_endstop.get_steppers() == []


class TestHasToolheadSensor:
    def test_plain_switch_pin_is_true(self):
        ext = _make_afc_extruder()
        ext.tool_start = "PD3"
        ext.fila_tool_start = None
        assert ext.has_toolhead_sensor is True

    def test_buffer_is_true(self):
        ext = _make_afc_extruder()
        ext.tool_start = "buffer"
        ext.fila_tool_start = None
        assert ext.has_toolhead_sensor is True

    def test_virtual_is_false(self):
        ext = _make_afc_extruder()
        ext.tool_start = "virtual"
        ext.fila_tool_start = MagicMock()  # virtual mode also sets this
        assert ext.has_toolhead_sensor is False

    def test_u1_sensor_is_true(self):
        ext = _make_afc_extruder()
        ext.tool_start = None
        ext.fila_tool_start = MagicMock()
        assert ext.has_toolhead_sensor is True

    def test_nothing_configured_is_false(self):
        ext = _make_afc_extruder()
        ext.tool_start = None
        ext.fila_tool_start = None
        assert ext.has_toolhead_sensor is False


class TestAFCExtruderStr:
    def test_str_returns_name(self):
        ext = _make_afc_extruder("my_extruder")
        assert str(ext) == "my_extruder"

class TestAFCExtruderHandleReady:
    def _make_extruder_with_lane(self, name):
        ext = _make_afc_extruder(name)
        ext.tc_lane = _make_afc_lane()
        ext.tc_lane.set_tool_loaded = MagicMock()
        ext.tc_lane.set_loaded = MagicMock()
        return ext

    def test_handle_ready_lanes(self):
        extruder_name = "extruder"
        ext = self._make_extruder_with_lane(extruder_name)

        ext.handle_ready()

        assert ext.no_lanes is False
        warn_msgs = [m for lvl, m in ext.logger.messages if lvl == "info"]
        assert not any(f"{extruder_name} no lanes" in m for m in warn_msgs)

    def test_handle_ready_no_lanes(self):
        extruder_name = "extruder"
        ext = self._make_extruder_with_lane(extruder_name)
        ext.lanes.update({extruder_name: ext})

        ext.handle_ready()
        warn_msgs = [m for lvl, m in ext.logger.messages if lvl == "info"]

        assert ext.no_lanes is True
        assert any(f"{extruder_name} no lanes" in m for m in warn_msgs)
        assert ext.tc_lane._load_state == ext.tool_start_state
        assert ext.tc_lane.prep_state  == ext.tool_start_state

    def test_handle_ready_no_lanes_tool_start_True(self):
        extruder_name = "extruder"
        ext = self._make_extruder_with_lane(extruder_name)
        ext.tool_start_state = True
        ext.lanes.update({extruder_name: ext})

        ext.handle_ready()

        assert ext.no_lanes is True
        assert ext.tc_lane._load_state == ext.tool_start_state
        assert ext.tc_lane.prep_state  == ext.tool_start_state
        ext.tc_lane.set_tool_loaded.assert_called_once()
        ext.tc_lane.set_loaded.assert_called_once()
    
    def test_handle_ready_no_lanes_tool_start_buffer(self):
        from configparser import Error as error
        extruder_name = "extruder"
        ext = self._make_extruder_with_lane(extruder_name)
        ext.tool_start = "buffer"
        ext.lanes.update({extruder_name: ext})

        with pytest.raises(error) as exc:
            ext.handle_ready()

        assert f"buffer is not valid config for pin_tool_start when using {extruder_name} as a standalone extruder" in str(exc.value)

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
    
    def test_handel_connect_duplicate_entry(self):
        ext1 = _make_afc_extruder("extruder")
        ext2 = _make_afc_extruder("extruder")
        ext2.afc = ext1.afc
        ext1.afc.tools[ext1.th_extruder_name] = ext1
        with pytest.raises(KlipperError) as exc:
            ext2.handle_connect()
        assert "Duplicate toolhead extruder mapping" in str(exc.value)


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

    def test_bypass_runout_does_not_pause_when_disabled_by_default(self):
        ext = _make_afc_extruder()
        ext.lane_loaded = None
        ext.enable_runout_in_bypass = False
        ext.afc.function.is_printing.return_value = True
        ext._handle_toolhead_sensor_runout(False, "tool_start")
        ext.afc.error.AFC_error.assert_not_called()

    def test_bypass_runout_pauses_when_enabled(self):
        ext = _make_afc_extruder()
        ext.lane_loaded = None
        ext.enable_runout_in_bypass = True
        ext.afc.function.is_printing.return_value = True
        ext._handle_toolhead_sensor_runout(False, "tool_start")
        ext.afc.error.AFC_error.assert_called_once_with(
            "Toolhead runout detected by tool_start sensor in bypass/manual mode."
        )

    def test_bypass_runout_does_not_pause_during_toolchange(self):
        ext = _make_afc_extruder()
        ext.lane_loaded = None
        ext.enable_runout_in_bypass = True
        ext.afc.in_toolchange = True
        ext.afc.function.is_printing.return_value = True
        ext._handle_toolhead_sensor_runout(False, "tool_start")
        ext.afc.error.AFC_error.assert_not_called()

    def test_bypass_runout_does_not_pause_during_error_state(self):
        ext = _make_afc_extruder()
        ext.lane_loaded = None
        ext.enable_runout_in_bypass = True
        ext.afc.error_state = True
        ext.afc.function.is_printing.return_value = True
        ext._handle_toolhead_sensor_runout(False, "tool_start")
        ext.afc.error.AFC_error.assert_not_called()

    def test_bypass_runout_does_not_pause_when_not_on_shuttle(self):
        ext = _make_afc_extruder()
        ext.lane_loaded = None
        ext.enable_runout_in_bypass = True
        ext.on_shuttle = MagicMock(return_value=False)
        ext.afc.function.is_printing.return_value = True
        ext._handle_toolhead_sensor_runout(False, "tool_start")
        ext.afc.error.AFC_error.assert_not_called()

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
    
    def test_runout_no_handle_toolhead_runout_attr(self):
        ext = _make_afc_extruder()
        lane = MagicMock(spec=[])
        ext.lanes = {"lane1": lane}
        ext.lane_loaded = "lane1"
        ext._handle_toolhead_sensor_runout(False, "tool_end")
        assert not hasattr(lane, "handle_toolhead_runout")


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
        """Values left as None fall through to cmd_UPDATE_TOOLHEAD_SENSORS's
        own default (gcmd.get_float(..., self.tool_stn) etc.), so there's no
        need to duplicate that fallback here -- ext is unused now but kept
        as a parameter so existing call sites don't need to change."""
        params = {}
        if tool_stn is not None:
            params["TOOL_STN"] = tool_stn
        if tool_stn_unload is not None:
            params["TOOL_STN_UNLOAD"] = tool_stn_unload
        if tool_after is not None:
            params["TOOL_AFTER_EXTRUDER"] = tool_after
        return MockGCodeCommand(params=params)

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

    def test_contains_next_pickup(self):
        ext = _make_afc_extruder()
        ext.next_pickup = True
        result = ext.get_status()
        assert result["next_pickup"] is True

    def test_contains_status(self):
        ext = _make_afc_extruder()
        ext.status = State.TOOL_PICKUP
        result = ext.get_status()
        assert result["status"] == State.TOOL_PICKUP

    def test_contains_is_standalone_true(self):
        ext = _make_afc_extruder()
        ext.no_lanes = True
        result = ext.get_status()
        assert result["is_standalone"] is True

    def test_contains_is_standalone_false(self):
        """Covers the other half of is_standalone(), proven independently of
        the True case above."""
        ext = _make_afc_extruder()
        ext.no_lanes = False
        result = ext.get_status()
        assert result["is_standalone"] is False

# ── on_shuttle ────────────────────────────────────────────────────────────────
# Sentinel value that mirrors the real DETECT_PRESENT constant
_DETECT_PRESENT = "mounted"
_DETECT_ABSENT  = "absent"
_DETECT_UNAVAILABLE = "unavailable"

_DETECT_PRESENT_OLD = 1
_DETECT_ABSENT_OLD  = 0
_DETECT_UNAVAILABLE_OLD = -1

def _make_toolchanger_module():
    """Return a minimal fake extras.toolchanger module."""
    mod = types.ModuleType("extras.toolchanger")
    mod.DETECT_PRESENT = _DETECT_PRESENT
    mod.DETECT_ABSENT = _DETECT_ABSENT
    mod.DETECT_UNAVAILABLE = _DETECT_UNAVAILABLE
    return mod

def _make_toolchanger_module_old():
    """Return a minimal fake extras.toolchanger module."""
    mod = types.ModuleType("extras.toolchanger")
    mod.DETECT_PRESENT = _DETECT_PRESENT_OLD
    mod.DETECT_ABSENT = _DETECT_ABSENT_OLD
    mod.DETECT_UNAVAILABLE = _DETECT_UNAVAILABLE_OLD
    return mod


def _tool_with_detect_state(detect_state_value, is_selected=False):
    """Mock tool object that has detect_state."""
    tool = MagicMock()
    tool.detect_state = detect_state_value
    selected = tool if is_selected else MagicMock()
    tool.main_toolchanger.get_selected_tool.return_value = selected
    return tool


def _tool_without_detect_state():
    """Mock tool object that does NOT have detect_state (spec=[] prevents any attr)."""
    return MagicMock(spec=[])


# ── Branch 1: single toolhead (both None) ────────────────────────────────────

class TestOnShuttle_SingleToolhead:
    """tool_obj=None AND tc_unit_name=None → always returns True."""

    def test_returns_true_when_both_none(self):
        ext = _make_afc_extruder()
        assert ext.on_shuttle() is True

    def test_still_true_after_repeated_calls(self):
        ext = _make_afc_extruder()
        assert ext.on_shuttle() is True
        assert ext.on_shuttle() is True


# ── Branch 2: toolchanger unit set, no tool object (custom macros) ────────────

class TestOnShuttle_CustomMacros:
    """tc_unit_name is set but tool_obj=None → returns True."""

    def test_returns_true_with_unit_name_no_tool(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "toolchanger_0"
        assert ext.on_shuttle() is True

    def test_returns_true_for_various_unit_names(self):
        for name in ["unit_1", "my_toolchanger", "T0"]:
            ext = _make_afc_extruder()
            ext.tc_unit_name = name
            assert ext.on_shuttle() is True, f"Expected True for tc_unit_name={name!r}"


# ── Branch 3: tool_obj has detect_state ──────────────────────────────────────

class TestOnShuttle_WithDetectState:
    """tool_obj carries detect_state; result depends on sensor or tool selection."""

    @pytest.fixture(autouse=True)
    def patch_toolchanger_module(self):
        """Inject a fake extras.toolchanger so the in-method import resolves."""
        fake_mod = _make_toolchanger_module()
        with patch.dict(sys.modules, {"extras.toolchanger": fake_mod}):
            yield

    def test_returns_true_when_detect_state_is_present(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_with_detect_state("mounted", is_selected=False)
        assert ext.on_shuttle() is True

    def test_returns_true_when_tool_is_selected_but_not_present(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_with_detect_state("absent", is_selected=True)
        assert ext.on_shuttle() is True

    def test_returns_true_when_both_present_and_selected(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_with_detect_state("mounted", is_selected=True)
        assert ext.on_shuttle() is True

    def test_returns_false_when_not_present_and_not_selected(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_with_detect_state("absent", is_selected=False)
        assert ext.on_shuttle() is False
    
    def test_returns_false_when_not_present_and_not_selected_unavailable(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_with_detect_state("unavailable", is_selected=False)
        assert ext.on_shuttle() is False

    def test_get_selected_tool_called_once_per_invocation(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_with_detect_state("absent", is_selected=False)
        ext.on_shuttle()
        ext.tool_obj.main_toolchanger.get_selected_tool.assert_called_once()

class TestOnShuttle_WithDetectStateOld:
    """tool_obj carries detect_state; result depends on sensor or tool selection."""

    @pytest.fixture(autouse=True)
    def patch_toolchanger_module(self):
        """Inject a fake extras.toolchanger so the in-method import resolves."""
        fake_mod = _make_toolchanger_module_old()
        with patch.dict(sys.modules, {"extras.toolchanger": fake_mod}):
            yield

    def test_returns_true_when_detect_state_is_present(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_with_detect_state(1, is_selected=False)
        assert ext.on_shuttle() is True

    def test_returns_true_when_tool_is_selected_but_not_present(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_with_detect_state(0, is_selected=True)
        assert ext.on_shuttle() is True

    def test_returns_true_when_both_present_and_selected(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_with_detect_state(1, is_selected=True)
        assert ext.on_shuttle() is True

    def test_returns_false_when_not_present_and_not_selected(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_with_detect_state(0, is_selected=False)
        assert ext.on_shuttle() is False
    
    def test_returns_false_when_not_present_and_not_selected_unavailable(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_with_detect_state(-1, is_selected=False)
        assert ext.on_shuttle() is False

    def test_get_selected_tool_called_once_per_invocation(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_with_detect_state(0, is_selected=False)
        ext.on_shuttle()
        ext.tool_obj.main_toolchanger.get_selected_tool.assert_called_once()


# ── Branch 4: tool_obj exists but lacks detect_state ─────────────────────────

class TestOnShuttle_WithoutDetectState:
    """tool_obj present but no detect_state attr → returns False."""

    def test_returns_false_when_no_detect_state(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = "unit_0"
        ext.tool_obj = _tool_without_detect_state()
        assert ext.on_shuttle() is False

    def test_returns_false_with_no_tc_unit_name(self):
        """tool_obj set but tc_unit_name=None still reaches detect_state check."""
        ext = _make_afc_extruder()
        ext.tc_unit_name = None
        ext.tool_obj = _tool_without_detect_state()
        assert ext.on_shuttle() is False

class TestOnShuttle_WithParkDetector:
    def test_returns_true_active_state(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = None
        ext.park_detector_obj = MagicMock()
        ext.park_detector_obj.get_park_detector_status.return_value = {"state":"ACTIVATE"}

        assert ext.on_shuttle() is True
    
    def test_returns_false_active_state(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = None
        ext.park_detector_obj = MagicMock()
        ext.park_detector_obj.get_park_detector_status.return_value = {"state":"NOT_ACTIVATE"}

        assert ext.on_shuttle() is False

# ── tool_start_callback helpers ───────────────────────────────────────────────

def _make_ext_for_tool_start(name="extruder"):
    """Extend _make_afc_extruder with the extra fields tool_start_callback touches."""
    ext = _make_afc_extruder(name)
    ext.no_lanes          = False
    ext.load_active       = False
    ext.load_unload_sequence = MagicMock()

    # tc_lane mock with the attributes the method writes/reads
    tc_lane               = MagicMock()
    tc_lane._load_state   = False
    tc_lane.prep_state    = False
    tc_lane._afc_prep_done = False
    tc_lane.custom_load_cmd = None
    ext.tc_lane           = tc_lane

    return ext

# ── tool_start_callback: state unchanged ──────────────────────────────────────

class TestToolStartCallback_StateUnchanged:
    """state == tool_start_state: logs, still updates tool_start_state."""

    def test_logs_info_when_state_unchanged_standalone_toolhead(self):
        ext = _make_ext_for_tool_start()
        ext.tool_start_state = False
        ext.tc_unit_name = "extruder"
        ext.no_lanes = True
        ext.tool_start_callback(100.0, False)
        info_msgs = [m for lvl, m in ext.logger.messages if lvl == "info"]
        assert any("Not loading" in m for m in info_msgs)
    
    def test_logs_info_when_state_unchanged_not_standalone_toolhead(self):
        ext = _make_ext_for_tool_start()
        ext.tool_start_state = False
        ext.tool_start_callback(100.0, False)
        info_msgs = [m for lvl, m in ext.logger.messages if lvl == "info"]
        assert not any("Not loading" in m for m in info_msgs)

    def test_state_still_set_when_unchanged(self):
        ext = _make_ext_for_tool_start()
        ext.tool_start_state = True
        ext.tool_start_callback(100.0, True)
        assert ext.tool_start_state is True

    def test_load_unload_sequence_not_called_when_state_unchanged(self):
        ext = _make_ext_for_tool_start()
        ext.tool_start_state = False
        ext.tool_start_callback(100.0, False)
        ext.load_unload_sequence.assert_not_called()
    
    def test_load_unload_sequence_not_called_when_lane_custom_load_cmd_is_not_none(self):
        ext = _make_ext_for_tool_start()
        ext.tc_lane.custom_load_cmd = "CUSTOM_COMMAND"
        ext.tool_start_callback(100.0, False)
        ext.load_unload_sequence.assert_not_called()


# ── tool_start_callback: state changed, no toolchanger / no_lanes ─────────────

class TestToolStartCallback_StateChanged_NoToolchanger:
    """State changes but tc_unit_name=None or no_lanes=False: just updates state."""

    def test_updates_state_to_true_no_tc_unit(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = None
        ext.tool_start_state = False
        ext.tool_start_callback(100.0, True)
        assert ext.tool_start_state is True

    def test_updates_state_to_false_no_tc_unit(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = None
        ext.tool_start_state = True
        ext.tool_start_callback(100.0, False)
        assert ext.tool_start_state is False

    def test_no_sequence_called_without_tc_unit(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = None
        ext.tool_start_state = False
        ext.tool_start_callback(100.0, True)
        ext.load_unload_sequence.assert_not_called()

    def test_no_sequence_called_when_no_lanes_false(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = "unit_0"
        ext.no_lanes         = False          # toolchanger set but multi-lane
        ext.tool_start_state = False
        ext.tool_start_callback(100.0, True)
        ext.load_unload_sequence.assert_not_called()

    def test_tc_lane_not_updated_when_no_lanes_false(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = "unit_0"
        ext.no_lanes         = False
        ext.tool_start_state = False
        ext.tool_start_callback(100.0, True)
        assert ext.tc_lane._load_state is False  # untouched


# ── tool_start_callback: state changed, toolchanger active ────────────────────

class TestToolStartCallback_StateChanged_WithToolchanger:
    """tc_unit_name set + no_lanes=True: tc_lane state is always updated."""

    def _make_tc_ext(self, printer_ready=False, prep_done=False,
                     state=True, load_active=False, on_shuttle=False,
                     printing=False):
        from klippy import message_ready as READY
        ext                  = _make_ext_for_tool_start()
        ext.tc_unit_name     = "unit_0"
        ext.no_lanes         = True
        ext.tool_start_state = not state      # ensure state != tool_start_state
        ext.load_active      = load_active
        ext.printer.state_message = READY if printer_ready else "startup"
        ext.tc_lane._afc_prep_done = prep_done
        ext.on_shuttle = MagicMock(return_value=on_shuttle)
        ext.afc.function.is_printing.return_value = printing
        return ext

    def test_tc_lane_load_state_updated(self):
        ext = self._make_tc_ext(state=True)
        ext.tool_start_callback(100.0, True)
        assert ext.tc_lane._load_state is True

    def test_tc_lane_prep_state_updated(self):
        ext = self._make_tc_ext(state=False)
        ext.tool_start_callback(100.0, False)
        assert ext.tc_lane.prep_state is False

    def test_tool_start_state_updated(self):
        ext = self._make_tc_ext(state=True)
        ext.tool_start_callback(100.0, True)
        assert ext.tool_start_state is True

    # printer not ready ─────────────────────────────────────────────────────

    def test_no_sequence_when_printer_not_ready(self):
        ext = self._make_tc_ext(printer_ready=False, prep_done=True, state=True)
        ext.tool_start_callback(100.0, True)
        ext.load_unload_sequence.assert_not_called()

    def test_save_vars_not_called_when_printer_not_ready(self):
        ext = self._make_tc_ext(printer_ready=False, prep_done=True, state=True)
        ext.tool_start_callback(100.0, True)
        ext.afc.save_vars.assert_not_called()

    # prep not done ─────────────────────────────────────────────────────────

    def test_no_sequence_when_prep_not_done(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=False, state=True)
        ext.tool_start_callback(100.0, True)
        ext.load_unload_sequence.assert_not_called()

    def test_save_vars_not_called_when_prep_not_done(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=False, state=True)
        ext.tool_start_callback(100.0, True)
        ext.afc.save_vars.assert_not_called()

    # ready + prep done + filament present ──────────────────────────────────

    def test_load_sequence_called_when_ready_and_filament_present(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=True,
                                state=True, load_active=False)
        ext.tool_start_callback(100.0, True)
        ext.load_unload_sequence.assert_called_once_with(ext.tool_stn)

    def test_load_sequence_not_called_when_load_already_active(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=True,
                                state=True, load_active=True)
        ext.tool_start_callback(100.0, True)
        ext.load_unload_sequence.assert_not_called()

    def test_save_vars_called_after_load_sequence(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=True,
                                state=True, load_active=False)
        ext.tool_start_callback(100.0, True)
        ext.afc.save_vars.assert_called_once()

    # ready + prep done + filament absent (runout) ──────────────────────────

    def test_set_tool_unloaded_called_on_runout(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=True, state=False)
        ext.tool_start_callback(100.0, False)
        ext.tc_lane.set_tool_unloaded.assert_called_once()
    
    def test_set_tool_unloaded_not_called_on_runout(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=True, state=False,
                                on_shuttle=True, printing=True)
        ext.tool_start_callback(100.0, False)
        ext.tc_lane.set_tool_unloaded.assert_not_called()

    def test_set_unloaded_called_on_runout(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=True, state=False)
        ext.tool_start_callback(100.0, False)
        ext.tc_lane.set_unloaded.assert_called_once()
    
    def test_set_unloaded_not_called_on_runout(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=True, state=False,
                                on_shuttle=True, printing=True)
        ext.tool_start_callback(100.0, False)
        ext.tc_lane.set_unloaded.assert_not_called()

    def test_save_vars_called_after_runout(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=True, state=False,
                                on_shuttle=False, printing=False)
        ext.tool_start_callback(100.0, False)
        ext.afc.save_vars.assert_called_once()

    def test_load_sequence_not_called_on_runout(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=True, state=False)
        ext.tool_start_callback(100.0, False)
        ext.load_unload_sequence.assert_not_called()
    
    def test_load_sequence_info_called(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=True, state=False,
                                on_shuttle=True, printing=True)
        ext.tool_start_callback(100.0, False)
        ext.load_unload_sequence.assert_not_called()
        info_msgs = [m for lvl, m in ext.logger.messages if lvl == "info"]
        assert any("Cannot trigger auto load/unload" in m for m in info_msgs)
    
    def test_load_sequence_info_not_called_not_printing(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=True, state=False,
                                on_shuttle=True, printing=False)
        ext.tool_start_callback(100.0, False)
        ext.load_unload_sequence.assert_not_called()
        info_msgs = [m for lvl, m in ext.logger.messages if lvl == "info"]
        assert not any("Cannot trigger auto load/unload" in m for m in info_msgs)
    
    def test_load_sequence_info_not_called_not_on_shuttle(self):
        ext = self._make_tc_ext(printer_ready=True, prep_done=True, state=False,
                                on_shuttle=False, printing=True)
        ext.tool_start_callback(100.0, False)
        ext.load_unload_sequence.assert_not_called()
        info_msgs = [m for lvl, m in ext.logger.messages if lvl == "info"]
        assert not any("Cannot trigger auto load/unload" in m for m in info_msgs)

class TestNoteToolStartCallback:
    def test_orig_note_filament_present_called(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = "unit_0"
        ext.no_lanes         = False
        ext.tool_start_state = False
        ext.orig_note_filament_present = MagicMock()
        ext.note_tool_start_callback(True)
        ext.orig_note_filament_present.assert_called_once()
    
    def test_orig_note_filament_present_check_state_true(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = "unit_0"
        ext.no_lanes         = False
        ext.tool_start_state = False
        ext.orig_note_filament_present = MagicMock()
        ext.note_tool_start_callback(True)
        args = ext.orig_note_filament_present.call_args.args
        assert args[0]
    
    def test_orig_note_filament_present_check_state_false(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = "unit_0"
        ext.no_lanes         = False
        ext.tool_start_state = False
        ext.orig_note_filament_present = MagicMock()
        ext.note_tool_start_callback(False)
        args = ext.orig_note_filament_present.call_args.args
        assert not args[0]
    
    def test_orig_note_filament_present_check_default_force(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = "unit_0"
        ext.no_lanes         = False
        ext.tool_start_state = False
        ext.orig_note_filament_present = MagicMock()
        ext.note_tool_start_callback(True)
        args = ext.orig_note_filament_present.call_args.args
        assert not args[1]
    
    def test_orig_note_filament_present_check_force_true(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = "unit_0"
        ext.no_lanes         = False
        ext.tool_start_state = False
        ext.orig_note_filament_present = MagicMock()
        ext.note_tool_start_callback(True, True)
        args = ext.orig_note_filament_present.call_args.args
        assert args[1]

    def test_tool_start_callback_called(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = "unit_0"
        ext.no_lanes         = False
        ext.tool_start_state = False
        ext.orig_note_filament_present = MagicMock()
        ext.tool_start_callback = MagicMock()
        ext.note_tool_start_callback(True)
        ext.tool_start_callback.assert_called_once()

    def test_tool_start_callback_check_arg0(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = "unit_0"
        ext.no_lanes         = False
        ext.tool_start_state = False
        ext.orig_note_filament_present = MagicMock()
        ext.tool_start_callback = MagicMock()
        ext.note_tool_start_callback(True)
        args = ext.tool_start_callback.call_args.args
        assert args[0] == 0
        assert isinstance(args[0], int)
    
    def test_tool_start_callback_check_state(self):
        ext = _make_ext_for_tool_start()
        ext.tc_unit_name     = "unit_0"
        ext.no_lanes         = False
        ext.tool_start_state = False
        ext.orig_note_filament_present = MagicMock()
        ext.tool_start_callback = MagicMock()
        ext.note_tool_start_callback(True)
        args = ext.tool_start_callback.call_args.args
        assert args[1]

class TestCheckExtruderName:
    def test_no_extruder_in_config_name(self):
        ext = _make_afc_extruder(name="e0")
        with pytest.raises(KlipperError) as exc:
            ext._check_extruder_name()
        assert "Missing extruder reference" in str(exc.value)

    def test_no_extruder_in_extruder_name_variable(self):
        ext = _make_afc_extruder(name="extruder")
        ext.th_extruder_name = "e0"
        with pytest.raises(KlipperError) as exc:
            ext._check_extruder_name()
        assert "Missing extruder reference" in str(exc.value)
    
    def test_extruder_in_config_name(self):
        ext = _make_afc_extruder(name="extruder")
        ext._check_extruder_name()

    def test_extruder_in_extruder_name_variable(self):
        ext = _make_afc_extruder(name="e0")
        ext.th_extruder_name = "extruder1"
        ext._check_extruder_name()

class TestPrepOnShuttleCheck:

    @pytest.fixture(autouse=True)
    def patch_toolchanger_module(self):
        """Inject a fake extras.toolchanger so the in-method import resolves."""
        fake_mod = _make_toolchanger_module()
        with patch.dict(sys.modules, {"extras.toolchanger": fake_mod}):
            yield

    def test_in_toolhead(self):
        ext = _make_afc_extruder()
        lane = MagicMock()
        msg = ext.prep_on_shuttle_check(lane)

        assert "<span class=primary--text> in ToolHead</span>" in msg
        lane.unit_obj.lane_tool_loaded.assert_not_called()
        lane.unit_obj.lane_tool_loaded_idle.assert_not_called()
    
    def test_in_toolhead_tool_obj(self):
        ext = _make_afc_extruder()
        ext.tool_obj = MagicMock()
        lane = MagicMock()
        msg = ext.prep_on_shuttle_check(lane)

        lane.unit_obj.lane_tool_loaded.assert_not_called()
        lane.unit_obj.lane_tool_loaded_idle.assert_not_called()

    def test_in_toolhead_tc_unit_name(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = MagicMock()
        lane = MagicMock()
        msg = ext.prep_on_shuttle_check(lane)

        lane.unit_obj.lane_tool_loaded.assert_not_called()
        lane.unit_obj.lane_tool_loaded_idle.assert_not_called()
    
    def test_in_toolhead_tc_unit_name_tool_obj_not_on_shuttle(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = MagicMock()
        ext.tool_obj = MagicMock()
        lane = MagicMock()
        msg = ext.prep_on_shuttle_check(lane)

        lane.unit_obj.lane_tool_loaded.assert_not_called()
        lane.unit_obj.lane_tool_loaded_idle.assert_called_once_with(lane)
    
    def test_in_toolhead_tc_unit_name_tool_obj_on_shuttle(self):
        ext = _make_afc_extruder()
        ext.tc_unit_name = MagicMock()
        ext.tool_obj = MagicMock()
        ext.tool_obj.detect_state = "mounted"
        lane = MagicMock()
        msg = ext.prep_on_shuttle_check(lane)

        lane.unit_obj.lane_tool_loaded.assert_called_once_with(lane)
        lane.unit_obj.lane_tool_loaded_idle.assert_called_once_with(lane)
        assert "<span class=primary--text> in ToolHead and toolhead on shuttle</span>" in msg


class TestClearStaleSensorState:
    def test_clears_sensor_and_buffer_latch(self):
        ext = _make_afc_extruder()
        ext.fila_tool_start = types.SimpleNamespace(
            runout_helper=types.SimpleNamespace(filament_present=True))
        ext.tool_start_state = True
        ext.reactor.pause = MagicMock()
        lane = MagicMock()
        lane.buffer_obj = types.SimpleNamespace(clear_advance_latch=MagicMock())

        ext.clear_stale_sensor_state(lane)

        assert ext.fila_tool_start.runout_helper.filament_present is False
        assert ext.tool_start_state is False
        lane.buffer_obj.clear_advance_latch.assert_called_once()
        ext.reactor.pause.assert_called_once_with(ext.reactor.monotonic() + 0.5)

    def test_handles_missing_objects(self):
        ext = _make_afc_extruder()
        ext.fila_tool_start = None
        ext.reactor.pause = MagicMock()
        lane = MagicMock()
        # A buffer with no advance latch.
        lane.buffer_obj = types.SimpleNamespace()

        ext.clear_stale_sensor_state(lane)

        assert ext.tool_start_state is False
        ext.reactor.pause.assert_called_once()


class TestExtruderMoveCB:
    def _make_afc_extruder_for_move_cb(self, extruder_values={}, afc_values={}):
        values = {
            "toolchanger_unit": "Tools"
        }
        values = values | extruder_values
        ext = _make_afc_extruder_as_standalone(extruder_values=values, afc_values=afc_values)
        ext.function = MagicMock()
        ext.afc.restore_toolhead_temp = MagicMock()
        ext.printer.lookup_object = MagicMock()
        ext.toolhead_extruder = MagicMock()
        ext.afc.save_vars = MagicMock()
        ext.tc_lane = _make_afc_lane(fullname="AFC_stepper extruder")
        return ext

    def test_motion_queue_current_move_is_none(self):
        ext = self._make_afc_extruder_for_move_cb()
        ext.motion_queuing = None

        assert ext.reactor.NEVER == ext.extruder_move_cb(100)
        assert AFCLaneState.NONE == ext.tc_lane.status
        assert None == ext.motion_queuing
    
    def test_motion_queue_current_move_is_none_check_asserts(self):
        ext = self._make_afc_extruder_for_move_cb()
        ext.motion_queuing = None
        ext.current_move_distance = 0
        ext.prev_trapq = MagicMock()
        ext.prev_sk = MagicMock()
        toolhead = ext.printer.lookup_object("toolhead")
        stepper = ext.toolhead_extruder.extruder_stepper.stepper

        assert ext.reactor.NEVER == ext.extruder_move_cb(100)
        ext.toolhead_extruder.extruder_stepper.stepper.set_trapq.assert_called_once_with(ext.prev_trapq)
        toolhead.flush_step_generation.assert_called_once()
        stepper.set_trapq.assert_called_once_with(ext.prev_trapq)
        stepper.set_stepper_kinematics.assert_called_once_with(ext.prev_sk)
        ext.function.do_enable.assert_called_once_with(False, ext.th_extruder_name)
        ext.afc.restore_toolhead_temp.assert_called_once_with(temp_state=ext._captured_toolhead_temp,
                                                              async_restore=True)
        assert ext.tc_lane.status == AFCLaneState.NONE
        assert ext.tc_lane.need_purge == False
        ext.afc.save_vars.assert_called_once()

        info_msgs = [m for lvl, m in ext.logger.messages if lvl == "info"]
        assert any(f"{ext.name} unloading done" in m for m in info_msgs)
    
    def test_motion_queue_not_none(self):
        ext = self._make_afc_extruder_for_move_cb()

        assert ext.reactor.NEVER == ext.extruder_move_cb(100)
        ext.motion_queuing.wipe_trapq.assert_called_once_with(ext.trapq)
    
    def test_load_active(self):
        ext = self._make_afc_extruder_for_move_cb()
        ext.load_active = True

        assert ext.reactor.NEVER == ext.extruder_move_cb(100)
        assert not ext.load_active
    
    def test_captured_toolhead_temp(self):
        ext = self._make_afc_extruder_for_move_cb()
        capture_toolhead = True
        ext._captured_toolhead_temp = capture_toolhead

        assert ext.reactor.NEVER == ext.extruder_move_cb(100)
        ext.afc.restore_toolhead_temp.assert_called_once_with(temp_state=capture_toolhead,
                                                              async_restore=True)
        assert None == ext._captured_toolhead_temp
    
    def test_current_move_not_zero_loading(self):
        ext = self._make_afc_extruder_for_move_cb()
        ext.current_move_distance = 100

        assert ext.reactor.NEVER == ext.extruder_move_cb(100)

        assert AFCLaneState.TOOLED == ext.tc_lane.status
        assert ext.tc_lane.need_purge
        assert 0 == ext.current_move_distance

        info_msgs = [m for lvl, m in ext.logger.messages if lvl == "info"]
        assert any(f"{ext.name} loading done" in m for m in info_msgs)

    def test_standalone_purge_disabled(self):
        extruder_values = {"enable_standalone_purge": False}
        ext = self._make_afc_extruder_for_move_cb(extruder_values=extruder_values)
        ext.current_move_distance = 100

        assert ext.reactor.NEVER == ext.extruder_move_cb(100)

        assert AFCLaneState.TOOLED == ext.tc_lane.status
        assert not ext.tc_lane.need_purge
        assert 0 == ext.current_move_distance
    
    def test_standalone_purge_disabled_from_afc(self):
        afc_values = {"enable_standalone_purge": False}
        ext = self._make_afc_extruder_for_move_cb(afc_values=afc_values)
        ext.current_move_distance = 100

        assert ext.reactor.NEVER == ext.extruder_move_cb(100)

        assert AFCLaneState.TOOLED == ext.tc_lane.status
        assert not ext.tc_lane.need_purge
        assert 0 == ext.current_move_distance


# ── load_unload_sequence ───────────────────────────────────────────────────────

class TestLoadUnloadSequence:
    def _make(self, standalone_auto_load_unload=True, extruder_values=None):
        values = {"toolchanger_unit": "Tools",
                  "standalone_auto_load_unload": standalone_auto_load_unload}
        values.update(extruder_values or {})
        ext = _make_afc_extruder_as_standalone(extruder_values=values)
        ext.afc.capture_toolhead_temp = MagicMock(return_value="captured")
        ext.afc._check_extruder_temp = MagicMock()
        ext.afc.save_vars = MagicMock()
        ext.reactor.update_timer = MagicMock()
        ext.tc_lane = _make_afc_lane(fullname="AFC_stepper extruder")
        ext.tc_lane.unit_obj = MagicMock()
        ext._set_standalone_lane_states = MagicMock()
        ext._apply_virtual_tool_start_state = MagicMock()
        return ext

    def test_enabled_starts_heating(self):
        ext = self._make(standalone_auto_load_unload=True)
        ext.load_unload_sequence(100)
        ext.afc._check_extruder_temp.assert_called_once_with(ext.tc_lane, no_wait=True)
        ext.afc.capture_toolhead_temp.assert_called_once_with(extruder=ext, async_capture=True)
        ext.reactor.update_timer.assert_called_once()

    def test_enabled_sets_load_active_and_move_distance(self):
        ext = self._make(standalone_auto_load_unload=True)
        ext.load_unload_sequence(100)
        assert ext.load_active is True
        assert ext.current_move_distance == 100

    def test_enabled_loading_sets_tool_loading_status_and_led(self):
        ext = self._make(standalone_auto_load_unload=True)
        ext.load_unload_sequence(100)
        assert ext.tc_lane.status == AFCLaneState.TOOL_LOADING
        ext.tc_lane.unit_obj.lane_loading.assert_called_once_with(ext.tc_lane)

    def test_enabled_unloading_sets_tool_unloading_status(self):
        ext = self._make(standalone_auto_load_unload=True)
        ext.load_unload_sequence(-100)
        assert ext.tc_lane.status == AFCLaneState.TOOL_UNLOADING
        ext.tc_lane.unit_obj.lane_loading.assert_not_called()

    def test_enabled_does_not_touch_bookkeeping_helpers(self):
        """The skip-path bookkeeping (final status/need_purge/lane-state helpers)
        is only for the disabled branch -- the enabled path leaves that to the
        temp/move callbacks instead."""
        ext = self._make(standalone_auto_load_unload=True)
        ext.load_unload_sequence(100)
        ext._set_standalone_lane_states.assert_not_called()
        ext._apply_virtual_tool_start_state.assert_not_called()
        ext.afc.save_vars.assert_not_called()

    def test_disabled_does_not_start_heating_or_timer(self):
        ext = self._make(standalone_auto_load_unload=False)
        ext.load_unload_sequence(100)
        ext.afc._check_extruder_temp.assert_not_called()
        ext.afc.capture_toolhead_temp.assert_not_called()
        ext.reactor.update_timer.assert_not_called()

    def test_disabled_does_not_set_load_active_or_move_distance(self):
        ext = self._make(standalone_auto_load_unload=False)
        ext.load_unload_sequence(100)
        assert ext.load_active is False
        assert ext.current_move_distance == 0

    def test_disabled_loading_sets_final_tooled_status(self):
        ext = self._make(standalone_auto_load_unload=False)
        ext.load_unload_sequence(100)
        assert ext.tc_lane.status == AFCLaneState.TOOLED
        ext.tc_lane.unit_obj.lane_loading.assert_not_called()
        ext._set_standalone_lane_states.assert_called_once_with(True)
        ext._apply_virtual_tool_start_state.assert_called_once_with(True)

    def test_disabled_unloading_sets_final_none_status(self):
        ext = self._make(standalone_auto_load_unload=False)
        ext.load_unload_sequence(-100)
        assert ext.tc_lane.status == AFCLaneState.NONE
        ext._set_standalone_lane_states.assert_called_once_with(False)
        ext._apply_virtual_tool_start_state.assert_called_once_with(False)

    def test_disabled_clears_need_purge(self):
        ext = self._make(standalone_auto_load_unload=False)
        ext.tc_lane.need_purge = True
        ext.load_unload_sequence(100)
        assert ext.tc_lane.need_purge is False

    def test_disabled_does_not_save_vars(self):
        """Persisting is the caller's job (tool_start_callback / AFC.LANE_EJECT),
        same as the enabled/heating path which only saves later via
        extruder_move_cb -- load_unload_sequence itself never saves synchronously
        for either branch."""
        ext = self._make(standalone_auto_load_unload=False)
        ext.load_unload_sequence(100)
        ext.afc.save_vars.assert_not_called()

    def test_disabled_logs_skip_message(self):
        ext = self._make(standalone_auto_load_unload=False)
        ext.load_unload_sequence(100)
        info_msgs = [m for lvl, m in ext.logger.messages if lvl == "info"]
        assert any("standalone_auto_load_unload is disabled" in m for m in info_msgs)
        assert any("hotend heating and filament move were not performed" in m for m in info_msgs)

    def test_default_config_value_is_enabled(self):
        """standalone_auto_load_unload defaults to True (mirroring afc.standalone_auto_load_unload)
        when not set in either the extruder or AFC config sections."""
        ext = self._make(standalone_auto_load_unload=True)
        assert ext.standalone_auto_load_unload is True

    def test_falls_back_to_afc_level_default(self):
        """Regression test: the config default expression must reference
        afc.standalone_auto_load_unload (not the nonexistent afc.standalone_auto_load),
        or constructing the extruder raises AttributeError."""
        ext = _make_afc_extruder_as_standalone(
            extruder_values={"toolchanger_unit": "Tools"},
            afc_values={"standalone_auto_load_unload": False})
        assert ext.standalone_auto_load_unload is False


# ── _set_standalone_lane_states ─────────────────────────────────────────────────

class TestSetStandaloneLaneStates:
    def _make(self, tool_start=None):
        ext = _make_afc_extruder()
        ext.tool_start = tool_start
        ext.tc_lane = MagicMock()
        return ext

    def test_none_lane_returns_without_error(self):
        ext = self._make()
        ext.tc_lane = None
        ext._set_standalone_lane_states(True)  # must not raise

    def test_enabled_calls_set_tool_loaded_and_set_loaded(self):
        ext = self._make()
        ext._set_standalone_lane_states(True)
        ext.tc_lane.set_tool_loaded.assert_called_once_with()
        ext.tc_lane.set_loaded.assert_called_once_with()

    def test_enabled_does_not_call_unload_helpers(self):
        ext = self._make()
        ext._set_standalone_lane_states(True)
        ext.tc_lane.set_tool_unloaded.assert_not_called()
        ext.tc_lane.set_unloaded.assert_not_called()

    def test_enabled_not_on_shuttle_calls_lane_tool_loaded_idle(self):
        """Covers the `not self.on_shuttle()` guard's True branch."""
        ext = self._make()
        ext.on_shuttle = MagicMock(return_value=False)
        ext._set_standalone_lane_states(True)
        ext.tc_lane.unit_obj.lane_tool_loaded_idle.assert_called_once_with(ext.tc_lane)

    def test_enabled_on_shuttle_skips_lane_tool_loaded_idle(self):
        """Covers the `not self.on_shuttle()` guard's False branch."""
        ext = self._make()
        ext.on_shuttle = MagicMock(return_value=True)
        ext._set_standalone_lane_states(True)
        ext.tc_lane.unit_obj.lane_tool_loaded_idle.assert_not_called()

    def test_disabled_does_not_call_lane_tool_loaded_idle(self):
        """The on_shuttle/idle-led check only runs on the enabled path."""
        ext = self._make(tool_start="virtual")
        ext.on_shuttle = MagicMock(return_value=False)
        ext._set_standalone_lane_states(False)
        ext.tc_lane.unit_obj.lane_tool_loaded_idle.assert_not_called()

    def test_disabled_real_sensor_calls_set_tool_unloaded_only(self):
        """Covers the `self.tool_start == "virtual"` guard's False branch."""
        ext = self._make(tool_start="^PD3")
        ext._set_standalone_lane_states(False)
        ext.tc_lane.set_tool_unloaded.assert_called_once_with()
        ext.tc_lane.set_unloaded.assert_not_called()

    def test_disabled_virtual_sensor_also_calls_set_unloaded(self):
        """Covers the `self.tool_start == "virtual"` guard's True branch."""
        ext = self._make(tool_start="virtual")
        ext._set_standalone_lane_states(False)
        ext.tc_lane.set_tool_unloaded.assert_called_once_with()
        ext.tc_lane.set_unloaded.assert_called_once_with()

    def test_disabled_does_not_call_load_helpers(self):
        ext = self._make(tool_start="virtual")
        ext._set_standalone_lane_states(False)
        ext.tc_lane.set_tool_loaded.assert_not_called()
        ext.tc_lane.set_loaded.assert_not_called()


# ── _apply_virtual_tool_start_state ──────────────────────────────────────────────

class TestApplyVirtualToolStartState:
    def _make(self, tool_start="virtual"):
        ext = _make_afc_extruder()
        ext.tool_start = tool_start
        ext.fila_tool_start = MagicMock()
        ext.tool_start_state = None
        return ext

    def test_non_virtual_sensor_is_noop(self):
        """Covers the `self.tool_start != "virtual"` guard's True branch."""
        ext = self._make(tool_start="^PD3")
        ext._apply_virtual_tool_start_state(True)
        assert ext.tool_start_state is None
        assert ext.fila_tool_start.mock_calls == []

    def test_virtual_enabled_sets_sensor_flags_true(self):
        ext = self._make()
        ext._apply_virtual_tool_start_state(True)
        assert ext.fila_tool_start.runout_helper.sensor_enabled is True
        assert ext.fila_tool_start.runout_helper.filament_present is True

    def test_virtual_enabled_sets_tool_start_state_true(self):
        ext = self._make()
        ext._apply_virtual_tool_start_state(True)
        assert ext.tool_start_state is True

    def test_virtual_disabled_sets_sensor_flags_false(self):
        ext = self._make()
        ext._apply_virtual_tool_start_state(False)
        assert ext.fila_tool_start.runout_helper.sensor_enabled is False
        assert ext.fila_tool_start.runout_helper.filament_present is False

    def test_virtual_disabled_sets_tool_start_state_false(self):
        ext = self._make()
        ext._apply_virtual_tool_start_state(False)
        assert ext.tool_start_state is False

    def test_non_bool_truthy_value_coerced_to_true(self):
        """Covers `enabled = bool(enabled)` -- a truthy non-bool must behave like True."""
        ext = self._make()
        ext._apply_virtual_tool_start_state(1)
        assert ext.tool_start_state is True
        assert ext.fila_tool_start.runout_helper.sensor_enabled is True


# ── _virtual_tool_start_toggle ────────────────────────────────────────────────

class TestVirtualToolStartToggle:
    def _make(self, prep_done=False):
        ext = _make_afc_extruder()
        ext.name = "extruder"
        ext.note_tool_start_callback = MagicMock()
        ext._apply_virtual_tool_start_state = MagicMock()
        ext.afc.prep_done = prep_done
        ext.afc.save_vars = MagicMock()
        return ext

    def test_calls_note_tool_start_callback(self):
        ext = self._make()
        ext._virtual_tool_start_toggle(True)
        ext.note_tool_start_callback.assert_called_once_with(True)

    def test_calls_apply_virtual_tool_start_state(self):
        ext = self._make()
        ext._virtual_tool_start_toggle(False)
        ext._apply_virtual_tool_start_state.assert_called_once_with(False)

    def test_logs_enabled_message(self):
        """Covers the `'enabled.' if enabled else 'disabled'` ternary's True side."""
        ext = self._make()
        ext._virtual_tool_start_toggle(True)
        assert ext.logger.messages == [
            ("info", "Virtual toolhead sensor for extruder enabled.")]

    def test_logs_disabled_message(self):
        """Covers the `'enabled.' if enabled else 'disabled'` ternary's False side."""
        ext = self._make()
        ext._virtual_tool_start_toggle(False)
        assert ext.logger.messages == [
            ("info", "Virtual toolhead sensor for extruder disabled")]

    def test_saves_vars_when_prep_done(self):
        """Covers the `if self.afc.prep_done` guard's True branch."""
        ext = self._make(prep_done=True)
        ext._virtual_tool_start_toggle(True)
        ext.afc.save_vars.assert_called_once_with()

    def test_skips_save_vars_when_prep_not_done(self):
        """Covers the `if self.afc.prep_done` guard's False branch."""
        ext = self._make(prep_done=False)
        ext._virtual_tool_start_toggle(True)
        ext.afc.save_vars.assert_not_called()


# ── restore_virtual_tool_start ────────────────────────────────────────────────

class TestRestoreVirtualToolStart:
    def _make(self, tool_start_state=False):
        ext = _make_afc_extruder()
        ext.name = "extruder"
        ext.tool_start_state = tool_start_state
        ext.tc_lane = MagicMock()
        ext._apply_virtual_tool_start_state = MagicMock()
        ext._set_standalone_lane_states = MagicMock()
        return ext

    def test_noop_when_state_already_matches(self):
        """Covers the `enabled == self.tool_start_state` guard's True branch."""
        ext = self._make(tool_start_state=True)
        ext.restore_virtual_tool_start(True)
        ext._apply_virtual_tool_start_state.assert_not_called()
        ext._set_standalone_lane_states.assert_not_called()
        assert ext.logger.messages == []

    def test_noop_when_tc_lane_is_none(self):
        """Covers the `self.tc_lane is None` guard's True branch."""
        ext = self._make(tool_start_state=False)
        ext.tc_lane = None
        ext.restore_virtual_tool_start(True)
        ext._apply_virtual_tool_start_state.assert_not_called()
        assert ext.logger.messages == []

    def test_applies_virtual_tool_start_state(self):
        ext = self._make(tool_start_state=False)
        ext.restore_virtual_tool_start(True)
        ext._apply_virtual_tool_start_state.assert_called_once_with(True)

    def test_sets_standalone_lane_states(self):
        ext = self._make(tool_start_state=False)
        ext.restore_virtual_tool_start(True)
        ext._set_standalone_lane_states.assert_called_once_with(True)

    def test_enabled_sets_load_state_and_prep_state_true(self):
        ext = self._make(tool_start_state=False)
        ext.restore_virtual_tool_start(True)
        assert ext.tc_lane._load_state is True
        assert ext.tc_lane.prep_state is True

    def test_disabled_sets_load_state_and_prep_state_false(self):
        ext = self._make(tool_start_state=True)
        ext.restore_virtual_tool_start(False)
        assert ext.tc_lane._load_state is False
        assert ext.tc_lane.prep_state is False

    def test_logs_enabled_message(self):
        """Covers the restore-message ternary's True side."""
        ext = self._make(tool_start_state=False)
        ext.restore_virtual_tool_start(True)
        assert ext.logger.messages == [
            ("info", "Virtual toolhead sensor for extruder restored as enabled, filament loaded")]

    def test_logs_disabled_message(self):
        """Covers the restore-message ternary's False side."""
        ext = self._make(tool_start_state=True)
        ext.restore_virtual_tool_start(False)
        assert ext.logger.messages == [
            ("info", "Virtual toolhead sensor for extruder restored as disabled")]

    def test_non_bool_truthy_value_coerced_to_true(self):
        """Covers `enabled = bool(enabled)` -- a truthy non-bool must behave like True."""
        ext = self._make(tool_start_state=False)
        ext.restore_virtual_tool_start(1)
        assert ext.tc_lane._load_state is True
        ext._apply_virtual_tool_start_state.assert_called_once_with(True)