"""
Unit tests for extras/AFC_OpenAMS.py

Scope
-----
Fully covered (self-contained, hardware-agnostic classes/functions):
  - _ams_box_logo / _ams_box_logo_error
  - AMSEventBus (singleton, subscribe ordering, publish, history pruning)
  - LaneInfo / LaneRegistry (register/unregister/lookup, singleton)
  - AMSHardwareService (status caching, snapshots, event publishing,
    lane resolution)
  - FollowerState / FollowerController (follower motor control, LED error
    dedup, rate-limited MCU command queue)
  - FPSLoadState / FPSState (dataclass-ish state container)
  - OAMSMonitor (lifecycle, notify_*, stuck-spool + clog detection)

Selectively covered (afcAMS unit driver -- built via its real constructor,
same as everything else in this file; afcUnit.__init__ runs for real too):
  - __init__ config parsing
  - calibration_lane_message / calibrate_lane / calibrate_bowden
  - _toolhead_sensor_triggered / get_engagement_params / _is_virtual_hub
  - _get_oams_index / _get_openams_spool_index / _resolve_lane_reference
  - _should_block_sensor_for_runout / _is_same_extruder / _get_monitor_state
  - _calibrate_hub_hes_spool / _wait_for_idle / check_runout
  - unit_load_lane / unit_unload_lane / lane_unloading / prepare_unload
  - handle_ready / _init_follower_and_monitor / _sync_lanes_from_hardware
  - _poll_oams_sensors
  - cmd_AFC_OAMS_CALIBRATE_PTFE / _HUB_HES / _HUB_HES_ALL / _CLEAR_ERRORS
  - _on_stuck_spool_detected / _on_clog_detected / _on_stuck_spool_cleared
  - on_filament_insert / on_filament_remove / _clear_oams_state_for_bay
  - handle_runout / handle_same_fps_reload
  - system_Test

"""

from __future__ import annotations

import sys
import threading
import time
import types
import itertools
import importlib.util
import configparser
from unittest.mock import MagicMock, patch
import pytest

# AFC_OpenAMS.py -> AFC_unit.py chain does not import `mcu`, but AFC_OAMS.py
# (imported indirectly by nothing here, kept for parity/safety) does; stub it
# defensively the same way the other OAMS test modules do.
_mcu_stub = types.ModuleType("mcu")
_mcu_stub.get_printer_mcu = MagicMock()
sys.modules.setdefault("mcu", _mcu_stub)

# afcAMS.__init__ opportunistically imports extras.temperature_oams to
# register its sensor factory; that module needs `extras.bus` (Klipper core,
# not vendored here) stubbed the same way test_temperature_oams.py does, or
# the import silently no-ops and that code path never gets exercised.
_bus_stub = types.ModuleType("extras.bus")
_bus_stub.MCU_I2C_from_config = MagicMock()
sys.modules.setdefault("extras.bus", _bus_stub)
try:
    import extras  # noqa: E402
    if not hasattr(extras, "bus"):
        extras.bus = _bus_stub
except ImportError:
    pass

import extras.AFC_OpenAMS as AFC_OpenAMS_module  # noqa: E402
from extras.AFC_OpenAMS import (  # noqa: E402
    afcAMS,
    OAMSStatus,
    AMSEventBus,
    LaneInfo,
    LaneRegistry,
    AMSHardwareService,
    FollowerState,
    FollowerController,
    FPSLoadState,
    FPSState,
    OAMSMonitor,
    _ams_box_logo,
    _ams_box_logo_error,
    load_config_prefix,
)
from extras.AFC_lane import AFCLaneState  # noqa: E402
from extras.AFC_unit import afcUnit  # noqa: E402
from tests.conftest import MockAFC, MockPrinter, MockReactor, MockLogger, MockConfig


def _exec_afc_openams_with_blocked_dependency(blocked_module_name):
    """Execute a throw-away copy of extras/AFC_OpenAMS.py's module-level code
    with `blocked_module_name` forced to fail import, to exercise the file's
    top-level ``try: from X import Y / except: raise error(...)`` guards.

    This never touches the real, already-imported ``extras.AFC_OpenAMS``
    module that the rest of this test suite (and extras.AFC_OAMS, which
    imports AMSHardwareService from it) depends on: the copy is loaded under
    a throwaway module name and discarded afterward, whether or not it
    raises. Blocking an import via ``sys.modules[name] = None`` is a standard
    Python mechanism -- it makes any ``import``/``from ... import`` of that
    name raise ImportError immediately, without touching the module itself.

    Critically, cleanup restores the *exact same* pre-existing module object
    in sys.modules (not just removes the block) -- simply deleting the entry
    would let it get re-imported fresh the next time anything touches it,
    producing new, distinct class objects that no longer match what other
    test files already imported and bound references to (this broke
    test_AFC_utils.py's own AFC_moonraker tests when first tried).
    """
    import extras.AFC_OpenAMS as real_module
    fresh_name = "extras.AFC_OpenAMS_import_guard_probe"
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


class TestModuleImportGuards:
    """Covers the three module-level `try/except: raise error(...)` guards
    around AFC_OpenAMS.py's imports of AFC_utils, AFC_lane, and AFC_unit."""

    def test_afc_utils_import_failure_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_openams_with_blocked_dependency("extras.AFC_utils")
        assert str(exc_info.value).startswith(
            "Error when trying to import AFC_utils.ERROR_STR")

    def test_afc_lane_import_failure_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_openams_with_blocked_dependency("extras.AFC_lane")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_lane, please rerun install-afc.sh "
            "script in your AFC-Klipper-Add-On directory then restart klipper")

    def test_afc_unit_import_failure_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_openams_with_blocked_dependency("extras.AFC_unit")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_unit, please rerun install-afc.sh "
            "script in your AFC-Klipper-Add-On directory then restart klipper")


# ═════════════════════════════════════════════════════════════════════════
# _ams_box_logo / _ams_box_logo_error
# ═════════════════════════════════════════════════════════════════════════

class TestAmsBoxLogo:
    def test_contains_title_and_name(self):
        logo = _ams_box_logo("OpenAMS", 4, "ams1")
        assert "OpenAMS" in logo
        assert "ams1" in logo

    def test_uses_success_styling(self):
        logo = _ams_box_logo("OpenAMS", 4, "ams1")
        assert "success--text" in logo

    def test_single_slot_minimum(self):
        logo = _ams_box_logo("X", 0, "ams1")
        # n_slots falsy (0) clamps to exactly 1 bay -- distinct from the
        # n_slots=4 case below, which must draw 4 bays.
        assert logo.count("O") == 1

    def test_four_slots_draws_four_bays(self):
        # Title "X" (no "O") keeps the "O" count purely a spool-bay count.
        logo = _ams_box_logo("X", 4, "ams1")
        # n_slots truthy (4) uses int(n_slots) directly, not the fallback of 1.
        assert logo.count("O") == 4

    def test_widens_bays_for_long_title(self):
        logo = _ams_box_logo("VERYLONGTITLE", 2, "ams1")
        header_line = logo.splitlines()[1]
        assert "VERYLONGTITLE" in header_line


class TestAmsBoxLogoError:
    def test_contains_error_banner(self):
        logo = _ams_box_logo_error("OpenAMS", 4, "ams1")
        assert "ERROR" in logo

    def test_uses_error_styling(self):
        logo = _ams_box_logo_error("OpenAMS", 4, "ams1")
        assert "error--text" in logo

    def test_contains_name(self):
        logo = _ams_box_logo_error("OpenAMS", 4, "ams1")
        assert "ams1" in logo

    def test_four_slots_widens_box(self):
        # n_slots=4 with a short title sizes the box off the 4 bays, wider
        # than the n_slots=0 (clamped to 1 bay) case below.
        logo = _ams_box_logo_error("X", 4, "ams1")
        header_line = logo.splitlines()[0]
        wide_dashes = header_line.count("-")

        logo_narrow = _ams_box_logo_error("X", 0, "ams1")
        narrow_dashes = logo_narrow.splitlines()[0].count("-")

        assert wide_dashes > narrow_dashes
        assert narrow_dashes > 0

    def test_single_slot_minimum(self):
        # n_slots falsy (0) clamps to exactly 1 bay -- verified indirectly
        # above by being narrower than the n_slots=4 box; here just confirm
        # it still renders a well-formed box.
        logo = _ams_box_logo_error("X", 0, "ams1")
        assert "ERROR" in logo

    def test_widens_bays_for_long_title(self):
        logo = _ams_box_logo_error("VERYLONGTITLE", 2, "ams1")
        header_line = logo.splitlines()[1]
        assert "VERYLONGTITLE" in header_line


# ═════════════════════════════════════════════════════════════════════════
# AMSEventBus
# ═════════════════════════════════════════════════════════════════════════

class TestAMSEventBus:
    def setup_method(self):
        # The bus is a process-wide singleton; reset it between tests so
        # subscribers/history don't leak across test cases.
        AMSEventBus._instance = None

    def test_get_instance_returns_singleton(self):
        bus1 = AMSEventBus.get_instance()
        bus2 = AMSEventBus.get_instance()
        assert bus1 is bus2

    def test_get_instance_attaches_logger_once(self):
        logger1 = MockLogger()
        logger2 = MockLogger()
        bus = AMSEventBus.get_instance(logger=logger1)
        AMSEventBus.get_instance(logger=logger2)
        assert bus.logger is logger1

    def test_publish_with_no_subscribers_returns_zero(self):
        bus = AMSEventBus()
        count = bus.publish("some_event", value=1)
        assert count == 0

    def test_publish_calls_subscriber_with_event_type_and_kwargs(self):
        bus = AMSEventBus()
        received = {}

        def handler(event_type, **kwargs):
            received["event_type"] = event_type
            received["kwargs"] = kwargs

        bus.subscribe("f1s_changed", handler)
        count = bus.publish("f1s_changed", bay=2, value=True)

        assert count == 1
        assert received["event_type"] == "f1s_changed"
        assert received["kwargs"]["bay"] == 2
        assert received["kwargs"]["value"] is True

    def test_publish_higher_priority_runs_first(self):
        bus = AMSEventBus()
        order = []
        bus.subscribe("evt", lambda event_type, **kw: order.append("low"), priority=0)
        bus.subscribe("evt", lambda event_type, **kw: order.append("high"), priority=10)
        bus.publish("evt")
        assert order == ["high", "low"]

    def test_publish_swallows_subscriber_exceptions(self):
        bus = AMSEventBus()

        def bad_handler(event_type, **kwargs):
            raise RuntimeError("boom")

        good = MagicMock()
        bus.subscribe("evt", bad_handler)
        bus.subscribe("evt", good)

        count = bus.publish("evt")

        assert count == 1  # only the good handler counted
        good.assert_called_once()

    def test_publish_records_history(self):
        bus = AMSEventBus()
        bus.publish("evt", foo="bar")
        assert len(bus._event_history) == 1
        assert bus._event_history[0][0] == "evt"

    def test_history_trimmed_beyond_max(self):
        bus = AMSEventBus()
        bus._MAX_HISTORY = 5
        for i in range(10):
            bus.publish("evt", i=i)
        assert len(bus._event_history) <= 5

    def test_history_ttl_pruning_can_drop_below_max_without_hard_trim(self):
        bus = AMSEventBus()
        bus._MAX_HISTORY = 2
        # Pre-seed with old (TTL-expired) entries already at the max.
        bus._event_history = [("old1", -1e9, {}), ("old2", -1e9, {})]
        bus.publish("evt")  # appends a fresh entry -> len=3 > MAX triggers cutoff
        # The TTL cutoff removes both old entries, leaving just the new one,
        # so the length is back under MAX_HISTORY without needing the final
        # hard slice.
        assert len(bus._event_history) == 1


# ═════════════════════════════════════════════════════════════════════════
# LaneInfo / LaneRegistry
# ═════════════════════════════════════════════════════════════════════════

class TestLaneInfo:
    def test_stores_all_fields(self):
        info = LaneInfo(
            "lane1", "ams1", 0, "extruder1",
            fps_name="fps1", hub_name="hub1", led_index=3
        )
        assert info.lane_name == "lane1"
        assert info.unit_name == "ams1"
        assert info.spool_index == 0
        assert info.extruder == "extruder1"
        assert info.fps_name == "fps1"
        assert info.hub_name == "hub1"
        assert info.led_index == 3

    def test_optional_fields_default_none(self):
        info = LaneInfo("lane1", "ams1", 0, "extruder1")
        assert info.fps_name is None
        assert info.hub_name is None
        assert info.led_index is None


class TestLaneRegistry:
    def setup_method(self):
        LaneRegistry._instances = {}

    def _registry(self):
        printer = MockPrinter(afc=MockAFC())
        return LaneRegistry(printer, logger=MockLogger())

    def test_init_sets_printer_and_event_bus(self):
        printer = MockPrinter(afc=MockAFC())
        logger = MockLogger()
        reg = LaneRegistry(printer, logger=logger)
        assert reg.printer is printer
        assert reg.event_bus is AMSEventBus.get_instance()

    def test_for_printer_returns_same_instance_for_same_printer(self):
        printer = MockPrinter(afc=MockAFC())
        r1 = LaneRegistry.for_printer(printer)
        r2 = LaneRegistry.for_printer(printer)
        assert r1 is r2

    def test_for_printer_updates_logger_on_cached_instance(self):
        printer = MockPrinter(afc=MockAFC())
        r1 = LaneRegistry.for_printer(printer)
        new_logger = MockLogger()
        r2 = LaneRegistry.for_printer(printer, logger=new_logger)
        assert r1 is r2
        assert r2.logger is new_logger

    def test_for_printer_returns_different_instance_for_different_printer(self):
        r1 = LaneRegistry.for_printer(MockPrinter(afc=MockAFC()))
        r2 = LaneRegistry.for_printer(MockPrinter(afc=MockAFC()))
        assert r1 is not r2

    def test_register_lane_creates_lookup_entries(self):
        reg = self._registry()
        info = reg.register_lane("lane1", "ams1", 0, "extruder1")
        assert reg.get_by_spool("ams1", 0) is info
        assert reg.resolve_lane_name("ams1", 0) == "lane1"
        assert reg._by_lane_name["lane1"] is info
        assert reg._by_lane_name_lower["lane1"] is info
        assert info in reg._by_extruder["extruder1"]

    def test_unregister_lane_removes_all_lookup_entries(self):
        reg = self._registry()
        info = reg.register_lane("lane1", "ams1", 0, "extruder1")

        reg._unregister_lane(info)

        assert "lane1" not in reg._by_lane_name
        assert "lane1" not in reg._by_lane_name_lower
        assert "extruder1" not in reg._by_extruder

    def test_register_lane_replaces_existing_registration(self):
        reg = self._registry()
        reg.register_lane("lane1", "ams1", 0, "extruder1")
        new_info = reg.register_lane("lane1", "ams1", 1, "extruder1")
        assert reg.get_by_spool("ams1", 0) is None
        assert reg.get_by_spool("ams1", 1) is new_info
        assert len(reg._lanes) == 1

    def test_unregister_lane_clears_extruder_index_when_empty(self):
        reg = self._registry()
        reg.register_lane("lane1", "ams1", 0, "extruder1")
        reg.register_lane("lane1", "ams1", 1, "extruder1")  # re-register removes old
        assert "extruder1" in reg._by_extruder

    def test_get_by_spool_missing_returns_none(self):
        reg = self._registry()
        assert reg.get_by_spool("ams1", 5) is None

    def test_resolve_lane_name_missing_returns_none(self):
        reg = self._registry()
        assert reg.resolve_lane_name("ams1", 5) is None

    def test_multiple_lanes_same_extruder_indexed_together(self):
        reg = self._registry()
        reg.register_lane("lane1", "ams1", 0, "extruder1")
        reg.register_lane("lane2", "ams1", 1, "extruder1")
        assert len(reg._by_extruder["extruder1"]) == 2

    def test_unregister_lane_not_in_lanes_list_is_noop_for_that_step(self):
        reg = self._registry()
        info = LaneInfo("lane1", "ams1", 0, "extruder1")
        # Never added to reg._lanes -- exercises the "not present" branch.
        reg._unregister_lane(info)  # must not raise

    def test_unregister_lane_not_in_extruder_index_is_noop_for_that_step(self):
        reg = self._registry()
        info = LaneInfo("lane1", "ams1", 0, "extruder1")
        reg._lanes.append(info)
        reg._by_lane_name["lane1"] = info
        # Deliberately not added to reg._by_extruder, exercising the
        # "info not in extruder_lanes" branch.
        reg._unregister_lane(info)  # must not raise
        assert info not in reg._lanes

    def test_reregistering_one_lane_leaves_extruder_index_populated(self):
        """When two lanes share an extruder and one is re-registered, the
        extruder's lookup list must stay populated (not popped) since the
        other lane is still indexed there."""
        reg = self._registry()
        reg.register_lane("lane1", "ams1", 0, "extruder1")
        reg.register_lane("lane2", "ams1", 1, "extruder1")

        reg.register_lane("lane1", "ams1", 2, "extruder1")  # re-register lane1

        assert "extruder1" in reg._by_extruder
        assert len(reg._by_extruder["extruder1"]) == 2


# ═════════════════════════════════════════════════════════════════════════
# AMSHardwareService
# ═════════════════════════════════════════════════════════════════════════

class TestAMSHardwareService:
    def setup_method(self):
        LaneRegistry._instances = {}
        AMSHardwareService._instances = {}
        AMSEventBus._instance = None

    def _service(self):
        printer = MockPrinter(afc=MockAFC())
        return AMSHardwareService(printer, "ams1", logger=MockLogger()), printer

    def test_init_default_state(self):
        service, printer = self._service()
        assert service._status_callbacks == []
        assert service._polling_timer is None
        assert service._polling_interval == 2.0
        assert service._polling_interval_idle == 4.0
        assert service._consecutive_idle_polls == 0
        assert service._idle_poll_threshold == 3
        assert service._last_encoder_clicks is None
        assert service._last_f1s_hes == [None, None, None, None]
        assert service._last_hub_hes == [None, None, None, None]
        assert service._last_fps_value is None
        assert service._polling_enabled is False
        assert service._reactor is None
        assert service._controller is None
        assert service._status == {}
        assert service._lane_snapshots == {}

    def test_for_printer_returns_cached_instance(self):
        printer = MockPrinter(afc=MockAFC())
        s1 = AMSHardwareService.for_printer(printer, "ams1")
        s2 = AMSHardwareService.for_printer(printer, "ams1")
        assert s1 is s2

    def test_for_printer_different_name_creates_new_instance(self):
        printer = MockPrinter(afc=MockAFC())
        s1 = AMSHardwareService.for_printer(printer, "ams1")
        s2 = AMSHardwareService.for_printer(printer, "ams2")
        assert s1 is not s2

    def test_for_printer_updates_logger_on_cached_instance(self):
        printer = MockPrinter(afc=MockAFC())
        s1 = AMSHardwareService.for_printer(printer, "ams1")
        new_logger = MockLogger()
        s2 = AMSHardwareService.for_printer(printer, "ams1", logger=new_logger)
        assert s1 is s2
        assert s2.logger is new_logger

    def test_attach_controller_seeds_status(self):
        service, printer = self._service()
        controller = MagicMock()
        controller.get_status.return_value = {"current_spool": 1}
        service.attach_controller(controller)
        assert service._status == {"current_spool": 1}

    def test_attach_controller_get_status_exception_leaves_status_empty(self):
        service, printer = self._service()
        controller = MagicMock()
        controller.get_status.side_effect = Exception("boom")
        service.attach_controller(controller)  # must not raise
        assert service._status == {}

    def test_attach_controller_none_skips_status_seed(self):
        service, printer = self._service()
        service.attach_controller(None)  # must not raise
        assert service._controller is None
        assert service._status == {}

    def test_resolve_controller_uses_cached_controller(self):
        service, printer = self._service()
        controller = MagicMock()
        service.attach_controller(controller)
        printer.lookup_object = MagicMock()
        result = service.resolve_controller()
        assert result is controller
        printer.lookup_object.assert_not_called()

    def test_resolve_controller_looks_up_when_uncached(self):
        service, printer = self._service()
        controller = MagicMock()
        controller.get_status.return_value = {}
        printer._objects["AFC_OAMS ams1"] = controller
        result = service.resolve_controller()
        assert result is controller
        assert service._controller is controller

    def test_resolve_controller_returns_none_when_missing(self):
        service, printer = self._service()
        result = service.resolve_controller()
        assert result is None

    def test_resolve_controller_lookup_exception_returns_none(self):
        service, printer = self._service()
        printer.lookup_object = MagicMock(side_effect=Exception("boom"))
        result = service.resolve_controller()
        assert result is None

    def test_poll_status_no_controller_returns_none(self):
        service, printer = self._service()
        assert service.poll_status() is None

    def test_poll_status_uses_controller_get_status(self):
        service, printer = self._service()
        controller = MagicMock()
        # Distinct values for the attach-time seed vs. the actual poll, so
        # the assertion below can only pass if poll_status's own call to
        # _update_status runs (attach_controller seeds its own value first).
        controller.get_status.side_effect = [
            {"fps_value": 0.1}, {"fps_value": 0.5}]
        service.attach_controller(controller)
        status = service.poll_status()
        assert status == {"fps_value": 0.5}
        assert service._status == {"fps_value": 0.5}

    def test_poll_status_falls_back_to_attribute_reads_on_exception(self):
        service, printer = self._service()
        controller = MagicMock()
        controller.get_status.side_effect = Exception("no get_status")
        controller.current_spool = 2
        controller.f1s_hes_value = [1, 0, 0, 0]
        controller.hub_hes_value = [0, 1, 0, 0]
        controller.fps_value = 0.3
        controller.encoder_clicks = 77
        service.attach_controller(controller)

        status = service.poll_status()

        assert status["current_spool"] == 2
        assert status["f1s_hes_value"] == [1, 0, 0, 0]
        assert status["encoder_clicks"] == 77

    def test_poll_status_fallback_without_encoder_clicks_attr(self):
        service, printer = self._service()
        controller = MagicMock(spec=["get_status", "current_spool"])
        controller.get_status.side_effect = Exception("no get_status")
        controller.current_spool = None
        service.attach_controller(controller)

        status = service.poll_status()

        assert "encoder_clicks" not in status

    def test_update_lane_snapshot_stores_snapshot(self):
        service, printer = self._service()
        service.update_lane_snapshot(
            "ams1", "lane1", True, False, 10.0, spool_index=0)
        key = "ams1:lane1"
        assert service._lane_snapshots[key]["lane_state"] is True
        assert service._lane_snapshots[key]["spool_index"] == 0
        # hub_state=False (not None) is coerced through bool(), not left as-is.
        assert service._lane_snapshots[key]["hub_state"] is False

    def test_update_lane_snapshot_hub_state_none_stays_none(self):
        """hub_state=None must be stored as None, not bool(None) (=False) --
        the two are distinguishable downstream (unknown vs known-absent)."""
        service, printer = self._service()
        service.update_lane_snapshot(
            "ams1", "lane1", True, None, 10.0, spool_index=0)
        key = "ams1:lane1"
        assert service._lane_snapshots[key]["hub_state"] is None

    def test_update_lane_snapshot_hub_state_truthy_non_bool_is_coerced(self):
        service, printer = self._service()
        service.update_lane_snapshot(
            "ams1", "lane1", True, 1, 10.0, spool_index=0)
        key = "ams1:lane1"
        assert service._lane_snapshots[key]["hub_state"] is True

    def test_update_lane_snapshot_negative_spool_index_ignored(self):
        service, printer = self._service()
        service.update_lane_snapshot(
            "ams1", "lane1", True, False, 10.0, spool_index=-1)
        assert "spool_index" not in service._lane_snapshots["ams1:lane1"]

    def test_update_lane_snapshot_unparseable_spool_index_ignored(self):
        service, printer = self._service()
        service.update_lane_snapshot(
            "ams1", "lane1", True, False, 10.0, spool_index="not-a-number")
        assert "spool_index" not in service._lane_snapshots["ams1:lane1"]

    def test_update_lane_snapshot_publishes_spool_loaded_on_transition(self):
        service, printer = self._service()
        received = []
        service.event_bus.subscribe(
            "spool_loaded", lambda event_type, **kw: received.append(kw))
        service.update_lane_snapshot(
            "ams1", "lane1", False, False, 1.0, spool_index=0)
        service.update_lane_snapshot(
            "ams1", "lane1", True, True, 2.0, spool_index=0)
        assert len(received) == 1
        assert received[0]["lane_name"] == "lane1"
        assert received[0]["spool_index"] == 0

    def test_update_lane_snapshot_event_falls_back_to_old_spool_index(self):
        """When the triggering call omits spool_index, the published event's
        spool_index must come from the previously-stored snapshot rather than
        being None."""
        service, printer = self._service()
        received = []
        service.event_bus.subscribe(
            "spool_loaded", lambda event_type, **kw: received.append(kw))
        # Establish spool_index=3 in the snapshot while lane_state is False.
        service.update_lane_snapshot("ams1", "lane1", False, False, 1.0, spool_index=3)
        # Transition to loaded without passing spool_index this time.
        service.update_lane_snapshot("ams1", "lane1", True, True, 2.0)
        assert len(received) == 1
        assert received[0]["spool_index"] == 3

    def test_update_lane_snapshot_publishes_spool_unloaded_on_transition(self):
        service, printer = self._service()
        received = []
        service.event_bus.subscribe(
            "spool_unloaded", lambda event_type, **kw: received.append(kw))
        service.update_lane_snapshot(
            "ams1", "lane1", True, True, 1.0, spool_index=0)
        service.update_lane_snapshot(
            "ams1", "lane1", False, False, 2.0, spool_index=0)
        assert len(received) == 1

    def test_update_lane_snapshot_no_event_when_suppressed(self):
        service, printer = self._service()
        received = []
        service.event_bus.subscribe(
            "spool_loaded", lambda event_type, **kw: received.append(kw))
        service.update_lane_snapshot(
            "ams1", "lane1", False, False, 1.0, spool_index=0)
        service.update_lane_snapshot(
            "ams1", "lane1", True, True, 2.0, spool_index=0, emit_spool_event=False)
        assert received == []

    def test_update_lane_snapshot_carries_forward_spool_index(self):
        service, printer = self._service()
        service.update_lane_snapshot("ams1", "lane1", True, True, 1.0, spool_index=2)
        service.update_lane_snapshot("ams1", "lane1", False, False, 2.0)
        assert service._lane_snapshots["ams1:lane1"]["spool_index"] == 2

    def test_update_lane_snapshot_records_tool_state_when_given(self):
        service, printer = self._service()
        service.update_lane_snapshot(
            "ams1", "lane1", True, True, 1.0, spool_index=0, tool_state=True)
        assert service._lane_snapshots["ams1:lane1"]["tool_state"] is True

    def test_resolve_lane_for_spool_none_index_returns_none(self):
        service, printer = self._service()
        assert service.resolve_lane_for_spool("ams1", None) is None

    def test_resolve_lane_for_spool_invalid_index_returns_none(self):
        service, printer = self._service()
        assert service.resolve_lane_for_spool("ams1", "not-a-number") is None

    def test_resolve_lane_for_spool_uses_registry(self):
        service, printer = self._service()
        service.registry.register_lane("lane1", "ams1", 0, "extruder1")
        assert service.resolve_lane_for_spool("ams1", 0) == "lane1"

    def test_resolve_lane_for_spool_with_afc_uses_registry_when_available(self):
        service, printer = self._service()
        service.registry.register_lane("lane1", "ams1", 0, "extruder1")
        service._resolve_lane_name_from_afc = MagicMock()

        result = service.resolve_lane_for_spool_with_afc("ams1", 0)

        assert result == "lane1"
        service._resolve_lane_name_from_afc.assert_not_called()

    def test_resolve_lane_for_spool_with_afc_falls_back_to_afc_scan(self):
        service, printer = self._service()
        afc = printer._afc
        unit_obj = MagicMock()
        unit_obj.oams_name = "ams1"
        lane_obj = MagicMock()
        lane_obj.unit = "ams1:1"  # slot = spool_index(0) + 1
        unit_obj.lanes = {"lane1": lane_obj}
        afc.units = {"unit1": unit_obj}
        printer._objects["AFC"] = afc

        result = service.resolve_lane_for_spool_with_afc("ams1", 0)

        assert result == "lane1"

    def test_resolve_lane_for_spool_with_afc_no_afc_object_returns_none(self):
        service, printer = self._service()
        printer.lookup_object = MagicMock(return_value=None)
        assert service.resolve_lane_for_spool_with_afc("ams1", 0) is None

    def test_resolve_lane_name_from_afc_none_spool_index_returns_none(self):
        service, printer = self._service()
        assert service._resolve_lane_name_from_afc("ams1", None) is None

    def test_resolve_lane_name_from_afc_bad_spool_index_returns_none(self):
        service, printer = self._service()
        assert service._resolve_lane_name_from_afc("ams1", "nope") is None

    def test_resolve_lane_name_from_afc_lookup_exception_returns_none(self):
        service, printer = self._service()
        printer.lookup_object = MagicMock(side_effect=Exception("boom"))
        assert service._resolve_lane_name_from_afc("ams1", 0) is None

    def test_resolve_lane_name_from_afc_no_units_attr_returns_none(self):
        service, printer = self._service()
        afc = MagicMock(spec=[])  # no `units` attribute
        printer._objects["AFC"] = afc
        assert service._resolve_lane_name_from_afc("ams1", 0) is None

    def test_resolve_lane_name_from_afc_skips_unit_without_oams_name(self):
        service, printer = self._service()
        afc = printer._afc
        unit_no_name = MagicMock(spec=["lanes"])
        afc.units = {"u1": unit_no_name}
        printer._objects["AFC"] = afc
        assert service._resolve_lane_name_from_afc("ams1", 0) is None

    def test_resolve_lane_name_from_afc_skips_mismatched_unit_name(self):
        service, printer = self._service()
        afc = printer._afc
        other_unit = MagicMock()
        other_unit.oams_name = "other_ams"
        matching_unit = MagicMock()
        matching_unit.oams_name = "ams1"
        lane_obj = MagicMock()
        lane_obj.unit = "ams1:1"
        matching_unit.lanes = {"lane1": lane_obj}
        # `other_unit` is scanned (and skipped) before the matching one.
        afc.units = {"u_other": other_unit, "u_match": matching_unit}
        printer._objects["AFC"] = afc

        result = service._resolve_lane_name_from_afc("ams1", 0)

        assert result == "lane1"

    def test_resolve_lane_name_from_afc_lane_without_colon_is_skipped(self):
        service, printer = self._service()
        afc = printer._afc
        unit_obj = MagicMock()
        unit_obj.oams_name = "ams1"
        bad_lane = MagicMock()
        bad_lane.unit = "no-colon-here"
        unit_obj.lanes = {"bad_lane": bad_lane}
        afc.units = {"u1": unit_obj}
        printer._objects["AFC"] = afc

        assert service._resolve_lane_name_from_afc("ams1", 0) is None

    def test_resolve_lane_name_from_afc_unparseable_slot_is_skipped(self):
        service, printer = self._service()
        afc = printer._afc
        unit_obj = MagicMock()
        unit_obj.oams_name = "ams1"
        bad_lane = MagicMock()
        bad_lane.unit = "ams1:not-a-number"
        unit_obj.lanes = {"bad_lane": bad_lane}
        afc.units = {"u1": unit_obj}
        printer._objects["AFC"] = afc

        assert service._resolve_lane_name_from_afc("ams1", 0) is None

    def test_resolve_lane_name_from_afc_slot_mismatch_is_skipped(self):
        service, printer = self._service()
        afc = printer._afc
        unit_obj = MagicMock()
        unit_obj.oams_name = "ams1"
        lane_obj = MagicMock()
        lane_obj.unit = "ams1:99"  # doesn't match target_slot (0+1=1)
        unit_obj.lanes = {"lane1": lane_obj}
        afc.units = {"u1": unit_obj}
        printer._objects["AFC"] = afc

        assert service._resolve_lane_name_from_afc("ams1", 0) is None

    def test_resolve_lane_name_from_afc_no_match_anywhere_returns_none(self):
        service, printer = self._service()
        afc = printer._afc
        afc.units = {}
        printer._objects["AFC"] = afc
        assert service._resolve_lane_name_from_afc("ams1", 0) is None

    def test_polling_callback_disabled_returns_never(self):
        service, printer = self._service()
        service._polling_enabled = False
        service._reactor = MockReactor()
        result = service._polling_callback(0.0)
        assert result == MockReactor.NEVER

    def test_polling_callback_disabled_lazily_caches_reactor(self):
        service, printer = self._service()
        service._polling_enabled = False
        assert service._reactor is None  # not yet cached

        result = service._polling_callback(0.0)

        assert service._reactor is printer._reactor
        assert result == MockReactor.NEVER

    def test_polling_callback_publishes_f1s_and_hub_changes(self):
        service, printer = self._service()
        service._polling_enabled = True
        service._reactor = MockReactor()
        controller = MagicMock()
        controller.get_status.return_value = {
            "f1s_hes_value": [1, 0, 0, 0],
            "hub_hes_value": [0, 1, 0, 0],
            "encoder_clicks": 5,
        }
        service.attach_controller(controller)

        f1s_events = []
        hub_events = []
        service.event_bus.subscribe(
            "f1s_changed", lambda event_type, **kw: f1s_events.append(kw))
        service.event_bus.subscribe(
            "hub_changed", lambda event_type, **kw: hub_events.append(kw))

        service._polling_callback(0.0)

        assert len(f1s_events) == 4  # all 4 bays go from unseen(None) to a value
        assert len(hub_events) == 4
        bay0 = next(e for e in f1s_events if e["bay"] == 0)
        assert bay0["value"] is True
        assert service._last_f1s_hes == [True, False, False, False]
        assert service._last_hub_hes == [False, True, False, False]

    def test_polling_callback_no_status_backs_off(self):
        service, printer = self._service()
        service._polling_enabled = True
        service._reactor = MockReactor()
        result = service._polling_callback(100.0)
        assert result == 100.0 + service._polling_interval_idle

    def test_polling_callback_exception_backs_off(self):
        service, printer = self._service()
        service._polling_enabled = True
        service._reactor = MockReactor()
        service.poll_status = MagicMock(side_effect=Exception("boom"))
        result = service._polling_callback(100.0)
        assert result == 100.0 + service._polling_interval_idle

    def test_polling_callback_idle_backoff_after_threshold(self):
        service, printer = self._service()
        service._polling_enabled = True
        service._reactor = MockReactor()
        service._idle_poll_threshold = 1
        service._consecutive_idle_polls = 5
        controller = MagicMock()
        controller.get_status.return_value = {
            "f1s_hes_value": [0, 0, 0, 0], "hub_hes_value": [0, 0, 0, 0],
        }
        service.attach_controller(controller)
        result = service._polling_callback(100.0)
        assert result == 100.0 + service._polling_interval_idle

    def test_polling_callback_no_change_skips_publish(self):
        service, printer = self._service()
        service._polling_enabled = True
        service._reactor = MockReactor()
        # Pre-seed last-seen values identical to the upcoming poll so neither
        # loop's "changed" branch is taken.
        service._last_f1s_hes = [False, False, False, False]
        service._last_hub_hes = [False, False, False, False]
        controller = MagicMock()
        controller.get_status.return_value = {
            "f1s_hes_value": [0, 0, 0, 0], "hub_hes_value": [0, 0, 0, 0],
        }
        service.attach_controller(controller)
        events = []
        service.event_bus.subscribe(
            "f1s_changed", lambda event_type, **kw: events.append(kw))
        service.event_bus.subscribe(
            "hub_changed", lambda event_type, **kw: events.append(kw))

        service._polling_callback(0.0)

        assert events == []

    def test_polling_callback_encoder_progress_resets_idle_counter(self):
        service, printer = self._service()
        service._polling_enabled = True
        service._reactor = MockReactor()
        service._consecutive_idle_polls = 2
        service._last_encoder_clicks = 10
        controller = MagicMock()
        controller.get_status.return_value = {
            "f1s_hes_value": [0, 0, 0, 0], "hub_hes_value": [0, 0, 0, 0],
            "encoder_clicks": 15,
        }
        service.attach_controller(controller)

        service._polling_callback(0.0)

        assert service._last_encoder_clicks == 15
        # consecutive_idle_polls was reset to 0 then incremented once
        assert service._consecutive_idle_polls == 1


# ═════════════════════════════════════════════════════════════════════════
# FollowerState / FollowerController
# ═════════════════════════════════════════════════════════════════════════

class TestFollowerState:
    def test_defaults(self):
        state = FollowerState()
        assert state.coasting is False
        assert state.last_state is None


def _make_follower_controller(oams_dict=None):
    reactor = MockReactor()
    logger = MockLogger()
    return FollowerController(oams_dict or {}, reactor, logger), reactor, logger


class TestFollowerControllerBasics:
    def test_get_follower_state_creates_on_first_access(self):
        fc, reactor, logger = _make_follower_controller()
        state = fc.get_follower_state("ams1")
        assert isinstance(state, FollowerState)
        assert fc.follower_state["ams1"] is state

    def test_get_follower_state_returns_same_instance(self):
        fc, reactor, logger = _make_follower_controller()
        s1 = fc.get_follower_state("ams1")
        s2 = fc.get_follower_state("ams1")
        assert s1 is s2

    def test_is_mcu_ready_none_oams_false(self):
        fc, reactor, logger = _make_follower_controller()
        assert fc.is_mcu_ready(None) is False

    def test_is_mcu_ready_no_mcu_attribute_false(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock(spec=[])
        assert fc.is_mcu_ready(oams) is False

    def test_is_mcu_ready_uses_is_shutdown(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        oams.mcu.is_shutdown.return_value = False
        assert fc.is_mcu_ready(oams) is True
        oams.mcu.is_shutdown.return_value = True
        assert fc.is_mcu_ready(oams) is False

    def test_is_mcu_ready_falls_back_to_last_clock(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock(spec=["mcu"])
        oams.mcu = MagicMock(spec=["get_last_clock"])
        oams.mcu.get_last_clock.return_value = 5
        assert fc.is_mcu_ready(oams) is True
        oams.mcu.get_last_clock.return_value = None
        assert fc.is_mcu_ready(oams) is False

    def test_is_mcu_ready_no_recognized_attrs_defaults_true(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock(spec=["mcu"])
        oams.mcu = MagicMock(spec=[])
        assert fc.is_mcu_ready(oams) is True

    def test_is_mcu_ready_exception_returns_false(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        type(oams).mcu = property(lambda self: (_ for _ in ()).throw(Exception("boom")))
        assert fc.is_mcu_ready(oams) is False


class TestEnableFollower:
    def test_none_oams_is_noop(self):
        fc, reactor, logger = _make_follower_controller()
        fc.enable_follower(None, None, 1, "ctx")  # must not raise

    def test_direction_out_of_range_defaults_to_1(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        oams.name = "ams1"
        fc.enable_follower(None, oams, 5, "ctx")
        oams.set_oams_follower.assert_called_once_with(1, 1)

    def test_sends_enable_1_with_direction(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        oams.name = "ams1"
        fc.enable_follower(None, oams, 0, "ctx")
        oams.set_oams_follower.assert_called_once_with(1, 0)

    def test_no_oams_name_is_noop(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        oams.name = None
        fc.enable_follower(None, oams, 1, "ctx")
        oams.set_oams_follower.assert_not_called()


class TestSetFollowerState:
    def test_none_oams_is_noop(self):
        fc, reactor, logger = _make_follower_controller()
        fc.set_follower_state(None, None, 1, 1, "ctx")  # must not raise

    def test_sends_enable_and_direction(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        oams.name = "ams1"
        fc.set_follower_state(None, oams, 0, 0, "ctx")
        oams.set_oams_follower.assert_called_once_with(0, 0)

    def test_direction_out_of_range_defaults_to_1(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        oams.name = "ams1"
        fc.set_follower_state(None, oams, 1, 9, "ctx")
        oams.set_oams_follower.assert_called_once_with(1, 1)

    def test_empty_oams_name_is_noop(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        oams.name = ""
        fc.set_follower_state(None, oams, 1, 1, "ctx")
        oams.set_oams_follower.assert_not_called()


class TestSetFollowerIfChanged:
    def test_skips_when_state_unchanged_and_not_forced(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        fc._set_follower_if_changed("ams1", oams, 1, 1, "ctx")
        oams.set_oams_follower.reset_mock()
        fc._set_follower_if_changed("ams1", oams, 1, 1, "ctx")
        oams.set_oams_follower.assert_not_called()

    def test_sends_when_state_changed(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        fc._set_follower_if_changed("ams1", oams, 1, 1, "ctx")
        fc._set_follower_if_changed("ams1", oams, 0, 0, "ctx")
        assert oams.set_oams_follower.call_count == 2
        assert any(
            lvl == "debug"
            and "Follower command for ams1: enable=0 direction=0" in m
            for lvl, m in logger.messages)
        assert (
            "debug", "Follower disabled for ams1 (ctx)"
        ) in logger.messages
        # The first call (enable=1) should log "enabled", not "disabled".
        assert (
            "debug", "Follower enabled for ams1 (ctx)"
        ) in logger.messages

    def test_forced_sends_even_when_unchanged(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        fc._set_follower_if_changed("ams1", oams, 1, 1, "ctx")
        fc._set_follower_if_changed("ams1", oams, 1, 1, "ctx", force=True)
        assert oams.set_oams_follower.call_count == 2

    def test_updates_last_state_on_success(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        fc._set_follower_if_changed("ams1", oams, 1, 0, "ctx")
        assert fc.get_follower_state("ams1").last_state == (1, 0)

    def test_mcu_command_failure_is_logged(self):
        fc, reactor, logger = _make_follower_controller()
        oams = MagicMock()
        oams.set_oams_follower.side_effect = Exception("mcu error")
        fc._set_follower_if_changed("ams1", oams, 1, 0, "ctx")
        assert (
            "error", "Failed to set follower on ams1: mcu error"
        ) in logger.messages
        # last_state not updated on failure
        assert fc.get_follower_state("ams1").last_state is None


class TestLedErrorControl:
    def _ready_oams(self):
        oams = MagicMock()
        oams.name = "ams1"
        oams.mcu.is_shutdown.return_value = False  # mcu must be "ready"
        oams.action_status = None  # must not appear "busy"
        return oams

    def test_set_led_error_if_changed_sends_and_dedups(self):
        fc, reactor, logger = _make_follower_controller()
        oams = self._ready_oams()
        fc.oams = {"ams1": oams}

        fc.set_led_error_if_changed(oams, "ams1", 0, 1, "ctx")
        # Simulate the first MCU command completing so the *in-flight* queue
        # guard can't be what's masking a duplicate send here -- only the
        # _led_error_state dedup itself should prevent the second call.
        fc._mcu_command_in_flight["ams1"] = False
        fc.set_led_error_if_changed(oams, "ams1", 0, 1, "ctx")  # duplicate, skipped

        oams.set_led_error.assert_called_once_with(0, 1)
        assert (
            "debug", "LED error set for ams1 spool 0 (ctx)"
        ) in logger.messages

    def test_set_led_error_if_changed_sends_again_when_state_differs(self):
        fc, reactor, logger = _make_follower_controller()
        oams = self._ready_oams()
        fc.oams = {"ams1": oams}

        fc.set_led_error_if_changed(oams, "ams1", 0, 1, "ctx")
        # Simulate the reactor timer marking the first command complete
        # (MockReactor.register_timer is a no-op, so the real completion
        # timer never fires on its own).
        fc._mcu_command_in_flight["ams1"] = False
        fc.set_led_error_if_changed(oams, "ams1", 0, 0, "ctx")

        assert oams.set_led_error.call_count == 2
        assert (
            "debug", "LED error cleared for ams1 spool 0 (ctx)"
        ) in logger.messages

    def test_clear_error_led_calls_set_with_zero(self):
        fc, reactor, logger = _make_follower_controller()
        oams = self._ready_oams()
        fc.oams = {"ams1": oams}

        fc.clear_error_led(oams, "ams1", 2, "ctx")

        oams.set_led_error.assert_called_once_with(2, 0)

    def test_set_led_error_command_failure_logged(self):
        fc, reactor, logger = _make_follower_controller()
        oams = self._ready_oams()
        oams.set_led_error.side_effect = Exception("mcu offline")
        fc.oams = {"ams1": oams}

        fc.set_led_error_if_changed(oams, "ams1", 0, 1, "ctx")

        assert (
            "error", "MCU command failed for ams1: mcu offline"
        ) in logger.messages

    def test_set_led_error_if_changed_dispatch_failure_logged(self):
        """Covers the outer try/except in set_led_error_if_changed itself
        (as opposed to the MCU-command-level failure above)."""
        fc, reactor, logger = _make_follower_controller()
        oams = self._ready_oams()
        fc.oams = {"ams1": oams}
        fc.rate_limited_mcu_command = MagicMock(side_effect=Exception("dispatch failed"))

        fc.set_led_error_if_changed(oams, "ams1", 0, 1, "ctx")

        assert (
            "error", "Failed to set LED error on ams1: dispatch failed"
        ) in logger.messages


class TestRateLimitedMcuCommandQueue:
    def test_no_oams_is_noop(self):
        fc, reactor, logger = _make_follower_controller()
        cmd = MagicMock()
        fc.rate_limited_mcu_command("missing", cmd)
        cmd.assert_not_called()

    def test_mcu_not_ready_is_noop(self):
        oams = MagicMock()
        oams.mcu = None
        fc, reactor, logger = _make_follower_controller({"ams1": oams})
        cmd = MagicMock()
        fc.rate_limited_mcu_command("ams1", cmd)
        cmd.assert_not_called()

    def test_ready_mcu_processes_command_immediately(self):
        oams = MagicMock()
        oams.mcu.is_shutdown.return_value = False
        oams.action_status = None
        fc, reactor, logger = _make_follower_controller({"ams1": oams})
        cmd = MagicMock()
        fc.rate_limited_mcu_command("ams1", cmd, 1, key="value")
        cmd.assert_called_once_with(1, key="value")
        assert fc._mcu_command_in_flight["ams1"] is True

    def test_command_exception_is_logged_not_raised(self):
        oams = MagicMock()
        oams.mcu.is_shutdown.return_value = False
        oams.action_status = None
        fc, reactor, logger = _make_follower_controller({"ams1": oams})
        cmd = MagicMock(side_effect=Exception("boom"))
        fc.rate_limited_mcu_command("ams1", cmd)  # must not raise
        assert ("error", "MCU command failed for ams1: boom") in logger.messages

    def test_busy_oams_defers_via_timer(self):
        oams = MagicMock()
        oams.mcu.is_shutdown.return_value = False
        oams.action_status = OAMSStatus.LOADING  # busy
        fc, reactor, logger = _make_follower_controller({"ams1": oams})
        cmd = MagicMock()

        fc.rate_limited_mcu_command("ams1", cmd)

        cmd.assert_not_called()
        assert "ams1" in fc._mcu_command_poll_timers
        # Seeded when the queue is first created for this oams -- distinct
        # from just reading it later via .get(name, False), which would
        # look identical whether or not the key was ever actually set.
        assert "ams1" in fc._mcu_command_in_flight

    def test_second_command_while_in_flight_is_queued_not_run(self):
        oams = MagicMock()
        oams.mcu.is_shutdown.return_value = False
        oams.action_status = None
        fc, reactor, logger = _make_follower_controller({"ams1": oams})
        cmd1 = MagicMock()
        cmd2 = MagicMock()

        fc.rate_limited_mcu_command("ams1", cmd1)
        fc.rate_limited_mcu_command("ams1", cmd2)

        cmd1.assert_called_once()
        cmd2.assert_not_called()  # still queued behind in-flight cmd1

    def test_cleanup_clears_all_queue_state(self):
        oams = MagicMock()
        oams.mcu.is_shutdown.return_value = False
        oams.action_status = OAMSStatus.LOADING
        fc, reactor, logger = _make_follower_controller({"ams1": oams})
        fc.rate_limited_mcu_command("ams1", MagicMock())  # creates a poll timer

        fc.cleanup()

        assert fc._mcu_command_poll_timers == {}
        assert fc._mcu_command_queue == {}
        assert fc._mcu_command_in_flight == {}

    def test_process_queue_unknown_oams_name_is_noop(self):
        fc, reactor, logger = _make_follower_controller()
        fc._process_mcu_command_queue("missing")  # must not raise

    def test_process_queue_empty_is_noop(self):
        oams = MagicMock()
        fc, reactor, logger = _make_follower_controller({"ams1": oams})
        fc._mcu_command_queue["ams1"] = []
        fc._mcu_command_in_flight["ams1"] = False
        fc._process_mcu_command_queue("ams1")  # must not raise

    def test_retry_timer_callback_reprocesses_when_no_longer_busy(self):
        oams = MagicMock()
        oams.mcu.is_shutdown.return_value = False
        oams.action_status = OAMSStatus.LOADING  # busy -> schedules retry
        fc, reactor, logger = _make_follower_controller({"ams1": oams})
        cmd = MagicMock()
        captured = {}
        reactor.register_timer = lambda cb, waketime=None: captured.setdefault("retry_cb", cb)

        fc.rate_limited_mcu_command("ams1", cmd)
        cmd.assert_not_called()

        oams.action_status = None  # now idle
        fc._mcu_command_in_flight["ams1"] = True  # seed so the reset below is proven
        result = captured["retry_cb"](0.0)

        cmd.assert_called_once()
        assert result == reactor.NEVER

    def test_retry_replaces_existing_poll_timer(self):
        oams = MagicMock()
        oams.mcu.is_shutdown.return_value = False
        oams.action_status = OAMSStatus.LOADING
        fc, reactor, logger = _make_follower_controller({"ams1": oams})
        old_timer = MagicMock()
        fc._mcu_command_poll_timers["ams1"] = old_timer
        reactor.unregister_timer = MagicMock()

        fc.rate_limited_mcu_command("ams1", MagicMock())

        reactor.unregister_timer.assert_called_once_with(old_timer)

    def test_retry_unregister_failure_is_swallowed(self):
        oams = MagicMock()
        oams.mcu.is_shutdown.return_value = False
        oams.action_status = OAMSStatus.LOADING
        fc, reactor, logger = _make_follower_controller({"ams1": oams})
        fc._mcu_command_poll_timers["ams1"] = MagicMock()
        reactor.unregister_timer = MagicMock(side_effect=Exception("boom"))

        fc.rate_limited_mcu_command("ams1", MagicMock())  # must not raise

    def test_done_timer_callback_processes_next_queued_command(self):
        oams = MagicMock()
        oams.mcu.is_shutdown.return_value = False
        oams.action_status = None
        fc, reactor, logger = _make_follower_controller({"ams1": oams})
        captured = {}
        reactor.register_timer = lambda cb, waketime=None: captured.setdefault("done_cb", cb)
        cmd = MagicMock()
        cmd2 = MagicMock()

        fc.rate_limited_mcu_command("ams1", cmd)
        cmd.assert_called_once()
        assert fc._mcu_command_in_flight["ams1"] is True
        fc.rate_limited_mcu_command("ams1", cmd2)  # queued behind cmd (still in-flight)
        cmd2.assert_not_called()

        result = captured["done_cb"](0.0)

        assert result == reactor.NEVER
        # Proves _done's call to _process_mcu_command_queue actually dequeued
        # and ran the second command (rather than just resetting the flag) --
        # in_flight ends up True again because cmd2 is now the one running.
        cmd2.assert_called_once()
        assert fc._mcu_command_in_flight["ams1"] is True

    def test_cleanup_unregister_failure_is_swallowed(self):
        oams = MagicMock()
        oams.mcu.is_shutdown.return_value = False
        oams.action_status = OAMSStatus.LOADING
        fc, reactor, logger = _make_follower_controller({"ams1": oams})
        fc.rate_limited_mcu_command("ams1", MagicMock())
        reactor.unregister_timer = MagicMock(side_effect=Exception("boom"))

        fc.cleanup()  # must not raise

        assert fc._mcu_command_poll_timers == {}
        reactor.unregister_timer.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════
# FPSLoadState / FPSState
# ═════════════════════════════════════════════════════════════════════════

class TestFPSLoadState:
    def test_values(self):
        assert FPSLoadState.UNLOADED == 0
        assert FPSLoadState.LOADED == 1
        assert FPSLoadState.LOADING == 2
        assert FPSLoadState.UNLOADING == 3


class TestFPSState:
    def test_defaults(self):
        st = FPSState()
        assert st.state == FPSLoadState.UNLOADED
        assert st.current_lane is None
        assert st.current_oams is None
        assert st.current_spool_idx is None
        assert st.since is None
        assert st.toolhead_confirmed is False
        assert st.last_encoder is None
        assert st.stuck_active is False
        assert st.stuck_start_time is None
        assert st.clog_active is False
        assert st.clog_start_time is None
        assert st.clog_start_extruder is None
        assert st.clog_start_extruder_obj is None
        assert st.clog_start_encoder is None
        assert st.engagement_in_progress is False
        assert st.engagement_checked_at is None
        assert st.last_lane_change_time is None

    def test_reset_restores_defaults_after_mutation(self):
        st = FPSState()
        st.state = FPSLoadState.LOADED
        st.current_lane = "lane1"
        st.current_oams = "ams1"
        st.current_spool_idx = 2
        st.since = 10.0
        st.toolhead_confirmed = True
        st.stuck_active = True
        st.stuck_start_time = 20.0
        st.clog_active = True
        st.clog_start_time = 30.0
        st.clog_start_extruder = 40.0
        st.clog_start_extruder_obj = MagicMock()
        st.clog_start_encoder = 50
        st.engagement_in_progress = True

        st.reset()

        assert st.state == FPSLoadState.UNLOADED
        assert st.current_lane is None
        assert st.current_oams is None
        assert st.current_spool_idx is None
        assert st.since is None
        assert st.toolhead_confirmed is False
        assert st.stuck_active is False
        assert st.stuck_start_time is None
        assert st.clog_active is False
        assert st.clog_start_time is None
        assert st.clog_start_extruder is None
        assert st.clog_start_extruder_obj is None
        assert st.clog_start_encoder is None
        assert st.engagement_in_progress is False

    def test_clear_encoder_samples(self):
        st = FPSState()
        st.last_encoder = 42
        st.clear_encoder_samples()
        assert st.last_encoder is None


# ═════════════════════════════════════════════════════════════════════════
# OAMSMonitor
# ═════════════════════════════════════════════════════════════════════════

def _make_monitor(**overrides):
    reactor = MockReactor()
    logger = MockLogger()
    fps_obj = MagicMock()
    fps_obj.fps_value = 0.5
    kwargs = dict(
        fps_name="FPS_buffer1", fps_obj=fps_obj, reactor=reactor, logger=logger,
        on_stuck_spool=MagicMock(), on_clog=MagicMock(), on_stuck_cleared=MagicMock(),
        clog_sensitivity="medium", is_printing_fn=lambda: True,
        is_lane_loaded_fn=lambda: True,
    )
    kwargs.update(overrides)
    monitor = OAMSMonitor(**kwargs)
    return monitor, reactor, fps_obj


class TestOAMSMonitorInit:
    def test_clog_sensitivity_medium_multiplier(self):
        monitor, reactor, fps = _make_monitor(clog_sensitivity="medium")
        assert monitor.clog_multiplier == 1.0
        assert monitor.enable_clog is True

    def test_clog_sensitivity_off_disables_clog(self):
        monitor, reactor, fps = _make_monitor(clog_sensitivity="off")
        assert monitor.clog_multiplier is None
        assert monitor.enable_clog is False

    def test_unknown_sensitivity_defaults_to_1(self):
        monitor, reactor, fps = _make_monitor(clog_sensitivity="bogus")
        assert monitor.clog_multiplier == 1.0

    def test_custom_thresholds_override_defaults(self):
        monitor, reactor, fps = _make_monitor(
            stuck_pressure_low=0.05, stuck_load_grace=3.0)
        assert monitor.stuck_pressure_low == 0.05
        assert monitor.stuck_load_grace == 3.0

    def test_default_runtime_state_and_thresholds(self):
        monitor, reactor, fps = _make_monitor(clog_sensitivity="medium")
        assert monitor._timer is None
        assert monitor._running is False
        assert monitor._oams is None
        assert monitor.stuck_pressure_clear == 0.12
        assert monitor.stuck_dwell == 2.0
        assert monitor.stuck_min_encoder == 3
        assert monitor.clog_extrusion_window == 24.0
        assert monitor.clog_post_load_grace == 12.0
        # clog_dwell scales by the sensitivity multiplier (1.0 for "medium")
        assert monitor.clog_dwell == 10.0
        # stuck_pressure_low/stuck_load_grace omitted (None) -> fall back to
        # the module-level constants, distinct from the overridden values
        # exercised in test_custom_thresholds_override_defaults above.
        assert monitor.stuck_pressure_low == AFC_OpenAMS_module.STUCK_PRESSURE_LOW
        assert monitor.stuck_load_grace == AFC_OpenAMS_module.STUCK_LOAD_GRACE

    def test_clog_dwell_scales_with_clog_multiplier(self):
        monitor, reactor, fps = _make_monitor(clog_sensitivity="high")
        assert monitor.clog_dwell == 10.0 * monitor.clog_multiplier
        assert monitor.clog_dwell != 10.0  # proves the multiplier was actually applied


class TestOAMSMonitorLifecycle:
    def test_start_sets_running_and_oams(self):
        monitor, reactor, fps = _make_monitor()
        reactor.register_timer = MagicMock(wraps=reactor.register_timer)
        oams = MagicMock()
        monitor.start(oams)
        assert monitor._running is True
        assert monitor._oams is oams
        assert monitor._timer is not None
        reactor.register_timer.assert_called_once()
        assert (
            "debug", "Monitor started for FPS_buffer1"
        ) in monitor.logger.messages

    def test_start_resets_detection_state(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.stuck_start_time = 5.0
        monitor.state.clog_start_time = 5.0
        monitor.state.last_encoder = 42  # seed so clear_encoder_samples is proven
        monitor.start(MagicMock())
        assert monitor.state.stuck_start_time is None
        assert monitor.state.clog_start_time is None
        assert monitor.state.last_encoder is None

    def test_stop_clears_running_and_timer(self):
        monitor, reactor, fps = _make_monitor()
        monitor.start(MagicMock())
        reactor.unregister_timer = MagicMock()
        monitor.stop()
        assert monitor._running is False
        assert monitor._timer is None
        reactor.unregister_timer.assert_called_once()
        assert (
            "debug", "Monitor stopped for FPS_buffer1"
        ) in monitor.logger.messages

    def test_start_when_already_running_does_not_replace_timer(self):
        monitor, reactor, fps = _make_monitor()
        monitor.start(MagicMock())
        first_timer = monitor._timer
        monitor.start(MagicMock())
        assert monitor._timer is first_timer

    def test_stop_when_not_running_is_a_noop(self):
        monitor, reactor, fps = _make_monitor()
        monitor.stop()  # never started -- must not raise
        assert monitor._timer is None

    def test_notify_load_complete_sets_loaded_state(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.last_encoder = 42  # seed so clear_encoder_samples is proven
        monitor.notify_load_complete("lane1", "ams1", 2)
        assert monitor.state.state == FPSLoadState.LOADED
        assert monitor.state.current_lane == "lane1"
        assert monitor.state.current_oams == "ams1"
        assert monitor.state.current_spool_idx == 2
        assert monitor.state.toolhead_confirmed is False
        assert monitor.state.last_encoder is None

    def test_notify_unload_complete_resets_state(self):
        monitor, reactor, fps = _make_monitor()
        monitor.notify_load_complete("lane1", "ams1", 0)
        monitor.notify_unload_complete()
        assert monitor.state.state == FPSLoadState.UNLOADED
        assert monitor.state.current_lane is None

    def test_notify_engagement_start_and_end(self):
        monitor, reactor, fps = _make_monitor()
        monitor.notify_engagement_start()
        assert monitor.state.engagement_in_progress is True
        monitor.notify_engagement_end()
        assert monitor.state.engagement_in_progress is False
        assert monitor.state.engagement_checked_at is not None


class TestOAMSMonitorTick:
    def test_not_running_returns_never(self):
        monitor, reactor, fps = _make_monitor()
        monitor._running = False
        assert monitor._monitor_tick(0.0) == MockReactor.NEVER

    def test_no_oams_returns_never(self):
        monitor, reactor, fps = _make_monitor()
        monitor._running = True
        monitor._oams = None
        assert monitor._monitor_tick(0.0) == MockReactor.NEVER

    def test_not_loaded_state_reschedules_idle(self):
        monitor, reactor, fps = _make_monitor()
        monitor._running = True
        monitor._oams = MagicMock()
        result = monitor._monitor_tick(100.0)
        from extras.AFC_OpenAMS import MONITOR_INTERVAL_IDLE
        assert result == 100.0 + MONITOR_INTERVAL_IDLE

    def test_no_is_lane_loaded_fn_skips_toolhead_confirmation(self):
        monitor, reactor, fps = _make_monitor(is_lane_loaded_fn=None)
        monitor._running = True
        oams = MagicMock()
        oams.encoder_clicks = 0
        monitor._oams = oams
        monitor.state.state = FPSLoadState.LOADED
        monitor.state.since = 0.0
        monitor._check_stuck_spool = MagicMock()
        monitor._check_clog = MagicMock()

        monitor._monitor_tick(100.0)

        # No toolhead-confirmation gate: detection runs straight away.
        monitor._check_stuck_spool.assert_called_once()

    def test_lane_not_confirmed_yet_waits(self):
        monitor, reactor, fps = _make_monitor(is_lane_loaded_fn=lambda: False)
        monitor._running = True
        monitor._oams = MagicMock()
        monitor.state.state = FPSLoadState.LOADED
        monitor.state.toolhead_confirmed = False
        from extras.AFC_OpenAMS import MONITOR_INTERVAL
        result = monitor._monitor_tick(100.0)
        assert result == 100.0 + MONITOR_INTERVAL

    def test_desync_stops_monitor(self):
        monitor, reactor, fps = _make_monitor(is_lane_loaded_fn=lambda: False)
        monitor._running = True
        monitor._oams = MagicMock()
        monitor.state.state = FPSLoadState.LOADED
        monitor.state.toolhead_confirmed = True  # was confirmed, now gone
        monitor.state.current_lane = "lane1"  # seed so reset() below is proven
        result = monitor._monitor_tick(100.0)
        assert result == MockReactor.NEVER
        assert monitor._running is False
        assert monitor.state.current_lane is None
        assert monitor.state.state == FPSLoadState.UNLOADED
        assert (
            "debug", "FPS_buffer1: lane no longer loaded to toolhead, stopping monitor"
        ) in monitor.logger.messages

    def test_not_printing_resets_clog_and_idles(self):
        monitor, reactor, fps = _make_monitor(is_printing_fn=lambda: False)
        monitor._running = True
        monitor._oams = MagicMock()
        monitor.state.state = FPSLoadState.LOADED
        monitor.state.toolhead_confirmed = True
        monitor.state.clog_start_time = 5.0
        from extras.AFC_OpenAMS import MONITOR_INTERVAL_IDLE
        result = monitor._monitor_tick(100.0)
        assert result == 100.0 + MONITOR_INTERVAL_IDLE
        assert monitor.state.clog_start_time is None

    def test_engagement_in_progress_skips_detection(self):
        monitor, reactor, fps = _make_monitor()
        monitor._running = True
        monitor._oams = MagicMock()
        monitor.state.state = FPSLoadState.LOADED
        monitor.state.toolhead_confirmed = True
        monitor.state.engagement_in_progress = True
        monitor._check_stuck_spool = MagicMock()
        from extras.AFC_OpenAMS import MONITOR_INTERVAL
        result = monitor._monitor_tick(100.0)
        assert result == 100.0 + MONITOR_INTERVAL
        monitor._check_stuck_spool.assert_not_called()

    def test_load_grace_period_skips_detection(self):
        monitor, reactor, fps = _make_monitor()
        monitor._running = True
        monitor._oams = MagicMock()
        monitor.state.state = FPSLoadState.LOADED
        monitor.state.toolhead_confirmed = True
        monitor.state.since = 99.0  # within grace of 100.0
        monitor._check_stuck_spool = MagicMock()
        result = monitor._monitor_tick(100.0)
        monitor._check_stuck_spool.assert_not_called()

    def test_runs_stuck_and_clog_checks(self):
        monitor, reactor, fps = _make_monitor()
        monitor._running = True
        oams = MagicMock()
        oams.encoder_clicks = 10
        monitor._oams = oams
        monitor.state.state = FPSLoadState.LOADED
        monitor.state.toolhead_confirmed = True
        monitor.state.since = 0.0
        monitor._check_stuck_spool = MagicMock()
        monitor._check_clog = MagicMock()

        monitor._monitor_tick(100.0)

        monitor._check_stuck_spool.assert_called_once()
        monitor._check_clog.assert_called_once()

    def test_clog_check_skipped_when_disabled(self):
        monitor, reactor, fps = _make_monitor(clog_sensitivity="off")
        monitor._running = True
        oams = MagicMock()
        oams.encoder_clicks = 10
        monitor._oams = oams
        monitor.state.state = FPSLoadState.LOADED
        monitor.state.toolhead_confirmed = True
        monitor.state.since = 0.0
        monitor._check_stuck_spool = MagicMock()
        monitor._check_clog = MagicMock()

        monitor._monitor_tick(100.0)

        monitor._check_clog.assert_not_called()

    def test_encoder_delta_computed_from_last_reading(self):
        monitor, reactor, fps = _make_monitor()
        monitor._running = True
        oams = MagicMock()
        oams.encoder_clicks = 20
        monitor._oams = oams
        monitor.state.state = FPSLoadState.LOADED
        monitor.state.toolhead_confirmed = True
        monitor.state.since = 0.0
        monitor.state.last_encoder = 15
        captured = {}
        monitor._check_stuck_spool = lambda et, delta, pressure: captured.update(delta=delta)

        monitor._monitor_tick(100.0)

        assert captured["delta"] == 5

    def test_exception_during_checks_is_logged(self):
        monitor, reactor, fps = _make_monitor()
        monitor._running = True
        oams = MagicMock()
        type(oams).encoder_clicks = property(
            lambda self: (_ for _ in ()).throw(Exception("boom")))
        monitor._oams = oams
        monitor.state.state = FPSLoadState.LOADED
        monitor.state.toolhead_confirmed = True
        monitor.state.since = 0.0

        result = monitor._monitor_tick(100.0)

        assert (
            "error", "Monitor error on FPS_buffer1: boom"
        ) in monitor.logger.messages
        from extras.AFC_OpenAMS import MONITOR_INTERVAL
        assert result == 100.0 + MONITOR_INTERVAL


class TestCheckStuckSpool:
    def test_no_movement_low_pressure_starts_timer(self):
        monitor, reactor, fps = _make_monitor()
        monitor._check_stuck_spool(100.0, encoder_delta=0, pressure=0.05)
        assert monitor.state.stuck_start_time == 100.0
        assert monitor.state.stuck_active is False
        assert any(
            lvl == "debug" and "stuck spool timer started" in m
            for lvl, m in monitor.logger.messages)

    def test_dwell_exceeded_fires_callback(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.stuck_start_time = 90.0
        monitor.stuck_dwell = 2.0
        monitor._check_stuck_spool(100.0, encoder_delta=0, pressure=0.05)
        assert monitor.state.stuck_active is True
        monitor._on_stuck_spool.assert_called_once()
        assert any(
            lvl == "info"
            and "Stuck spool on FPS_buffer1: encoder stopped (0 clicks), "
                "FPS pressure 0.05" in m
            for lvl, m in monitor.logger.messages)

    def test_moving_encoder_does_not_trigger(self):
        monitor, reactor, fps = _make_monitor()
        monitor._check_stuck_spool(100.0, encoder_delta=10, pressure=0.05)
        assert monitor.state.stuck_start_time is None

    def test_pressure_recovered_clears_active_and_calls_cleared_callback(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.stuck_active = True
        monitor.state.stuck_start_time = 90.0
        monitor._check_stuck_spool(100.0, encoder_delta=0, pressure=0.5)
        assert monitor.state.stuck_active is False
        assert monitor.state.stuck_start_time is None
        monitor._on_stuck_cleared.assert_called_once_with("FPS_buffer1")

    def test_encoder_moving_clears_pending_timer_without_active(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.stuck_start_time = 90.0
        monitor._check_stuck_spool(100.0, encoder_delta=10, pressure=0.05)
        assert monitor.state.stuck_start_time is None
        monitor._on_stuck_cleared.assert_not_called()  # was never "active"

    def test_already_active_skips_refiring(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.stuck_active = True
        monitor._check_stuck_spool(100.0, encoder_delta=0, pressure=0.05)
        # Still stuck, already flagged active -- no new callback fired.
        monitor._on_stuck_spool.assert_not_called()

    def test_dwell_not_yet_exceeded_keeps_waiting(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.stuck_start_time = 99.0
        monitor.stuck_dwell = 5.0
        monitor._check_stuck_spool(100.0, encoder_delta=0, pressure=0.05)
        assert monitor.state.stuck_active is False
        assert monitor.state.stuck_start_time == 99.0  # unchanged
        monitor._on_stuck_spool.assert_not_called()

    def test_no_callback_configured_is_safe(self):
        monitor, reactor, fps = _make_monitor(on_stuck_spool=None)
        monitor.state.stuck_start_time = 90.0
        monitor.stuck_dwell = 2.0
        monitor._check_stuck_spool(100.0, encoder_delta=0, pressure=0.05)  # must not raise
        assert monitor.state.stuck_active is True

    def test_pressure_between_low_and_clear_thresholds_leaves_state_pending(self):
        """Between STUCK_PRESSURE_LOW (0.08) and STUCK_PRESSURE_CLEAR (0.12),
        the condition is neither "stuck" nor "cleared" -- state stays as-is."""
        monitor, reactor, fps = _make_monitor()
        monitor.state.stuck_start_time = 90.0
        monitor._check_stuck_spool(100.0, encoder_delta=0, pressure=0.10)
        assert monitor.state.stuck_start_time == 90.0  # untouched
        monitor._on_stuck_cleared.assert_not_called()

    def test_engagement_grace_period_skips_check(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.engagement_checked_at = 98.0  # within 6s of 100.0
        monitor._check_stuck_spool(100.0, encoder_delta=0, pressure=0.01)
        assert monitor.state.stuck_start_time is None


class TestCheckClog:
    def test_grace_period_after_load_skips(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.last_lane_change_time = 95.0  # within post-load grace
        fps.extruder = MagicMock(last_position=10.0)
        monitor._check_clog(100.0, encoder_delta=0, pressure=0.5)
        assert monitor.state.clog_start_time is None

    def test_no_extruder_position_skips(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.last_lane_change_time = 0.0
        fps.extruder = MagicMock(last_position=None)
        monitor._check_clog(100.0, encoder_delta=0, pressure=0.5)
        assert monitor.state.clog_start_time is None

    def test_fps_without_extruder_attribute_skips(self):
        """hasattr(self.fps, 'extruder') is False (no such attribute at all,
        as opposed to the attribute existing but being None) -- must still
        safely no-op rather than raising AttributeError."""
        monitor, reactor, fps = _make_monitor()
        monitor.fps = MagicMock(spec=[])  # no 'extruder' attribute
        monitor.state.last_lane_change_time = 0.0
        monitor._check_clog(100.0, encoder_delta=0, pressure=0.5)  # must not raise
        assert monitor.state.clog_start_time is None

    def test_fps_extruder_attribute_is_none_skips(self):
        """The attribute exists but is None -- distinct from the "no
        attribute at all" case above; both must fall through to
        extruder_pos is None and return early."""
        monitor, reactor, fps = _make_monitor()
        monitor.state.last_lane_change_time = 0.0
        fps.extruder = None
        monitor._check_clog(100.0, encoder_delta=0, pressure=0.5)  # must not raise
        assert monitor.state.clog_start_time is None

    def test_starts_dwell_window_on_target_pressure_and_stuck_encoder(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.last_lane_change_time = 0.0
        monitor.state.last_encoder = 100
        fps.extruder = MagicMock(last_position=10.0)
        monitor._check_clog(100.0, encoder_delta=0, pressure=0.50)
        assert monitor.state.clog_start_time == 100.0
        assert monitor.state.clog_start_extruder == 10.0
        assert monitor.state.clog_start_extruder_obj is fps.extruder

    def test_toolchange_mid_window_resets_start_time(self):
        """A different extruder object than the one the window started with
        means a toolchange happened mid-window -- the phantom advance from
        comparing two extruders' position counters must not count, so the
        window resets instead of confirming."""
        monitor, reactor, fps = _make_monitor()
        old_extruder = MagicMock(last_position=0.0)
        monitor.state.last_lane_change_time = 0.0
        monitor.state.clog_start_time = 80.0
        monitor.state.clog_start_extruder = 0.0
        monitor.state.clog_start_extruder_obj = old_extruder
        monitor.state.clog_start_encoder = 100
        monitor.state.last_encoder = 102  # within slack -- would confirm if not reset
        monitor.clog_dwell = 5.0
        fps.extruder = MagicMock(last_position=30.0)  # a different extruder object now

        monitor._check_clog(100.0, encoder_delta=0, pressure=0.50)

        assert monitor.state.clog_active is False
        monitor._on_clog.assert_not_called()
        # Window restarted fresh against the new extruder, not left at the old start time.
        assert monitor.state.clog_start_time == 100.0
        assert monitor.state.clog_start_extruder_obj is fps.extruder

    def test_toolchange_mid_window_does_not_reset_when_no_window_open(self):
        """clog_start_time is already None (no window in progress) -- an
        extruder mismatch must not do anything odd in that case."""
        monitor, reactor, fps = _make_monitor()
        monitor.state.last_lane_change_time = 0.0
        monitor.state.clog_start_time = None
        monitor.state.clog_start_extruder_obj = MagicMock(last_position=0.0)
        monitor.state.last_encoder = 100
        fps.extruder = MagicMock(last_position=10.0)  # different object, but no window open

        monitor._check_clog(100.0, encoder_delta=0, pressure=0.50)

        # Falls through to the normal fresh-start path, same as any other tick.
        assert monitor.state.clog_start_time == 100.0
        assert monitor.state.clog_start_extruder_obj is fps.extruder

    def test_confirms_clog_after_dwell_and_extrusion_window(self):
        monitor, reactor, fps = _make_monitor()
        fps.extruder = MagicMock(last_position=30.0)  # >= extrusion window (24mm)
        monitor.state.last_lane_change_time = 0.0
        monitor.state.clog_start_time = 80.0
        monitor.state.clog_start_extruder = 0.0
        monitor.state.clog_start_extruder_obj = fps.extruder  # same extruder throughout
        monitor.state.clog_start_encoder = 100
        monitor.state.last_encoder = 102  # within CLOG_ENCODER_SLACK of start
        monitor.clog_dwell = 5.0

        monitor._check_clog(100.0, encoder_delta=0, pressure=0.50)

        assert monitor.state.clog_active is True
        monitor._on_clog.assert_called_once()
        assert any(
            lvl == "info" and m == (
                "Clog detected on FPS_buffer1: extruder advanced 30.0mm, "
                "encoder moved 2 clicks, FPS pressure 0.50 (dwell 20.0s)")
            for lvl, m in monitor.logger.messages)

    def test_encoder_progress_restarts_window_instead_of_firing(self):
        monitor, reactor, fps = _make_monitor()
        fps.extruder = MagicMock(last_position=30.0)
        monitor.state.last_lane_change_time = 0.0
        monitor.state.clog_start_time = 80.0
        monitor.state.clog_start_extruder = 0.0
        monitor.state.clog_start_extruder_obj = fps.extruder  # same extruder throughout
        monitor.state.clog_start_encoder = 0
        monitor.state.last_encoder = 100  # well beyond slack -> real movement

        monitor._check_clog(100.0, encoder_delta=0, pressure=0.50)

        assert monitor.state.clog_start_time == 100.0  # window restarted
        monitor._on_clog.assert_not_called()

    def test_pressure_off_target_resets_tracking(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.last_lane_change_time = 0.0
        monitor.state.clog_start_time = 80.0
        monitor.state.clog_active = True
        fps.extruder = MagicMock(last_position=30.0)

        monitor._check_clog(100.0, encoder_delta=0, pressure=0.9)  # far from target

        assert monitor.state.clog_start_time is None
        assert monitor.state.clog_active is False

    def test_does_not_refire_when_already_active(self):
        monitor, reactor, fps = _make_monitor()
        fps.extruder = MagicMock(last_position=30.0)
        monitor.state.last_lane_change_time = 0.0
        monitor.state.clog_start_time = 80.0
        monitor.state.clog_start_extruder = 0.0
        monitor.state.clog_start_extruder_obj = fps.extruder  # same extruder throughout
        monitor.state.clog_start_encoder = 100
        monitor.state.last_encoder = 102
        monitor.state.clog_active = True  # already fired
        monitor.clog_dwell = 5.0

        monitor._check_clog(100.0, encoder_delta=0, pressure=0.50)

        monitor._on_clog.assert_not_called()

    def test_extruder_position_read_exception_treated_as_missing(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.last_lane_change_time = 0.0
        type(fps).extruder = property(
            lambda self: (_ for _ in ()).throw(Exception("boom")))

        monitor._check_clog(100.0, encoder_delta=0, pressure=0.50)  # must not raise

        assert monitor.state.clog_start_time is None

    def test_dwell_window_open_but_not_yet_confirmed(self):
        monitor, reactor, fps = _make_monitor()
        fps.extruder = MagicMock(last_position=30.0)
        monitor.state.last_lane_change_time = 0.0
        monitor.state.clog_start_time = 99.0  # just started
        monitor.state.clog_start_extruder = 0.0
        monitor.state.clog_start_extruder_obj = fps.extruder  # same extruder throughout
        monitor.state.clog_start_encoder = 100
        monitor.state.last_encoder = 102  # within slack, no restart
        monitor.clog_dwell = 5.0

        monitor._check_clog(100.0, encoder_delta=0, pressure=0.50)

        assert monitor.state.clog_active is False
        assert monitor.state.clog_start_time == 99.0  # left running, not reset
        monitor._on_clog.assert_not_called()

    def test_no_callback_configured_is_safe(self):
        monitor, reactor, fps = _make_monitor(on_clog=None)
        fps.extruder = MagicMock(last_position=30.0)
        monitor.state.last_lane_change_time = 0.0
        monitor.state.clog_start_time = 80.0
        monitor.state.clog_start_extruder = 0.0
        monitor.state.clog_start_extruder_obj = fps.extruder  # same extruder throughout
        monitor.state.clog_start_encoder = 100
        monitor.state.last_encoder = 102
        monitor.clog_dwell = 5.0

        monitor._check_clog(100.0, encoder_delta=0, pressure=0.50)  # must not raise

        assert monitor.state.clog_active is True

    def test_pressure_off_target_when_already_inactive_is_noop(self):
        monitor, reactor, fps = _make_monitor()
        monitor.state.last_lane_change_time = 0.0
        monitor.state.clog_active = False  # already inactive
        fps.extruder = MagicMock(last_position=30.0)

        monitor._check_clog(100.0, encoder_delta=0, pressure=0.9)  # far from target

        assert monitor.state.clog_active is False


class TestLoadConfigPrefix:
    def test_constructs_afcams_unit(self):
        with patch("extras.AFC_OpenAMS.afcAMS") as mock_cls:
            mock_cls.return_value = "unit_instance"
            result = load_config_prefix("cfg")
        mock_cls.assert_called_once_with("cfg")
        assert result == "unit_instance"


# ═════════════════════════════════════════════════════════════════════════
# afcAMS — built via its real constructor. Now that MockAFC (conftest.py)
# carries every attribute afcUnit.__init__ reads, the whole inheritance
# chain runs for real: afcAMS(config) executes both afcUnit.__init__ and
# afcAMS.__init__ with no substitution or bypass of any kind.
# ═════════════════════════════════════════════════════════════════════════

def _make_lane(name, index=1, **overrides):
    lane = MagicMock()
    lane.name = name
    lane.index = index
    lane.led_index = index
    lane.led_spool_index = None
    lane.remember_spool = False
    lane.tool_loaded = False
    lane.load_state = False
    lane.map = "T0"
    lane._oams_runout_detected = False
    for k, v in overrides.items():
        setattr(lane, k, v)
    return lane


def _make_ams(oams=None, lanes=None, config_values=None):
    """Build an afcAMS instance via its real constructor (afcUnit.__init__
    included -- no stand-in, no bypass)."""
    afc = MockAFC()
    reactor = MockReactor()
    printer = MockPrinter(afc=afc)
    printer._reactor = reactor
    afc.reactor = reactor

    values = {"oams": "oams1"}
    if config_values:
        values.update(config_values)
    config = MockConfig(name="AFC_OpenAMS ams1", printer=printer, values=values)

    ams = afcAMS(config)

    ams.oams = oams
    ams.lanes = lanes or {}
    ams.logo = ""
    ams.logo_error = ""

    return ams, afc, printer, reactor


class TestAfcAMSInit:
    def _config(self, values=None):
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(
            name="AFC_OpenAMS ams1", printer=printer, values=values or {}
        )
        return config, printer, afc

    def _build(self, config):
        return afcAMS(config)

    def test_default_oams_name(self):
        config, printer, afc = self._config()
        ams = self._build(config)
        assert ams.oams_name == "oams1"

    def test_custom_oams_name(self):
        config, printer, afc = self._config(values={"oams": "oams2"})
        ams = self._build(config)
        assert ams.oams_name == "oams2"

    def test_default_type_and_flags(self):
        config, printer, afc = self._config()
        ams = self._build(config)
        assert ams.type == "OpenAMS"
        assert ams.stepperless_drive is True
        assert ams.auto_spoolman_create is False
        # Note: afcAMS.__init__ re-fetches gcode via `self.printer.lookup_object
        # ('gcode')` right after super().__init__(config) already set the same
        # attribute via `self.printer.load_object(config, 'gcode')`. Both
        # resolve to the identical object, so this line is fully redundant
        # with the parent's own assignment -- there is no observable
        # behavioral difference if it's removed, in this or any other test.
        assert ams.gcode is printer._gcode

    def test_custom_type_and_auto_spoolman_create(self):
        config, printer, afc = self._config(
            values={"type": "CustomAMS", "auto_spoolman_create": True})
        ams = self._build(config)
        assert ams.type == "CustomAMS"
        assert ams.auto_spoolman_create is True

    def test_default_engagement_params(self):
        config, printer, afc = self._config()
        ams = self._build(config)
        assert ams._engagement_length == 20.0
        assert ams._engagement_speed == 300.0
        assert ams._defer_engagement is False

    def test_custom_engagement_params(self):
        config, printer, afc = self._config(
            values={"engagement_length": 15.0, "engagement_speed": 200.0,
                    "defer_engagement": True})
        ams = self._build(config)
        assert ams._engagement_length == 15.0
        assert ams._engagement_speed == 200.0
        assert ams._defer_engagement is True

    def test_clog_sensitivity_lowercased(self):
        config, printer, afc = self._config(values={"clog_sensitivity": "HIGH"})
        ams = self._build(config)
        assert ams.clog_sensitivity == "high"

    def test_stuck_spool_defaults(self):
        config, printer, afc = self._config()
        ams = self._build(config)
        assert ams.stuck_spool_auto_recovery is False
        assert ams.stuck_spool_load_grace == 8.0
        assert ams.stuck_spool_pressure_threshold == 0.08

    def test_runtime_state_initialized_empty(self):
        config, printer, afc = self._config()
        ams = self._build(config)
        assert ams.oams is None
        assert ams._follower is None
        assert ams._monitor is None
        assert ams._spool_map == {}
        assert ams._hub_load_suppressed == set()

    def test_stuck_recovery_command_already_registered_is_ignored(self):
        """Multiple afcAMS units share the printer gcode namespace, so a
        second unit's registration of the shared stuck-recovery command
        raising (e.g. AlreadyRegistered) must be swallowed, not raised."""
        config, printer, afc = self._config()
        gcode = MagicMock()
        gcode.register_command.side_effect = Exception("already registered")
        printer._gcode = gcode
        ams = self._build(config)  # must not raise
        assert ams is not None


    def test_temperature_sensor_factory_registration_failure_is_swallowed(self):
        config, printer, afc = self._config()
        real_load_object = printer.load_object

        def load_object(cfg, name):
            if name == "heaters":
                raise Exception("no heaters object")
            return real_load_object(cfg, name)
        printer.load_object = load_object

        ams = self._build(config)  # must not raise
        assert ams is not None

    def test_registers_temperature_oams_sensor_factory(self):
        from extras.temperature_oams import TemperatureOAMS
        config, printer, afc = self._config()
        heaters = MagicMock()
        printer._objects["heaters"] = heaters
        self._build(config)
        heaters.add_sensor_factory.assert_called_once_with(
            "temperature_oams", TemperatureOAMS)

    def test_registers_mux_commands(self):
        config, printer, afc = self._config()
        gcode = MagicMock()
        printer._gcode = gcode
        ams = self._build(config)
        names = [c[0][0] for c in gcode.register_mux_command.call_args_list]
        assert "AFC_OAMS_CALIBRATE_PTFE" in names
        assert "AFC_OAMS_CALIBRATE_HUB_HES" in names
        assert "AFC_OAMS_CALIBRATE_HUB_HES_ALL" in names
        assert "AFC_OAMS_CLEAR_ERRORS" in names
        gcode.register_command.assert_called_once_with(
            "_AFC_OAMS_STUCK_RECOVERY", ams._cmd_stuck_spool_recovery,
            desc="Internal: auto-recover from a stuck spool via unload+reload")


# ── Small pure-logic helpers ──────────────────────────────────────────────

class TestCalibrationLaneMessage:
    def test_returns_template_with_lanes_placeholder(self):
        ams, afc, printer, reactor = _make_ams()
        msg = ams.calibration_lane_message()
        assert "{lanes}" in msg
        assert "HUB HES" in msg


class TestGetEngagementParams:
    def test_uses_unit_defaults_when_no_override(self):
        ams, afc, printer, reactor = _make_ams()
        assert ams.get_engagement_params("lane1") == (20.0, 300.0)

    def test_uses_lane_override_when_present(self):
        ams, afc, printer, reactor = _make_ams()
        ams._engagement_params["lane1"] = (10.0, 150.0)
        assert ams.get_engagement_params("lane1") == (10.0, 150.0)


class TestVerifyEngagement:
    def test_notifies_monitor_start_and_end(self):
        ams, afc, printer, reactor = _make_ams()
        monitor = MagicMock()
        ams._monitor = monitor
        lane = _make_lane("lane1")
        ams._oams_extrude = MagicMock()

        ams._verify_engagement(lane)

        monitor.notify_engagement_start.assert_called_once()
        monitor.notify_engagement_end.assert_called_once()
        assert any(
            lvl == "info" and "Verifying engagement for lane1" in m
            for lvl, m in ams.logger.messages)

    def test_no_monitor_is_safe(self):
        ams, afc, printer, reactor = _make_ams()
        ams._monitor = None
        lane = _make_lane("lane1")
        ams._oams_extrude = MagicMock()
        ams._verify_engagement(lane)  # must not raise

    def test_enables_follower_forward_when_present(self):
        oams = MagicMock()
        oams.encoder_clicks = 0
        ams, afc, printer, reactor = _make_ams(oams=oams)
        follower = MagicMock()
        ams._follower = follower
        ams._oams_extrude = MagicMock()
        lane = _make_lane("lane1")

        ams._verify_engagement(lane)

        follower.enable_follower.assert_called_once()
        args = follower.enable_follower.call_args[0]
        assert args[2] == 1  # forward
        assert follower.enable_follower.call_args[1]["force"] is True

    def test_no_follower_skips_enable(self):
        oams = MagicMock()
        oams.encoder_clicks = 0
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._follower = None
        ams._oams_extrude = MagicMock()
        lane = _make_lane("lane1")
        ams._verify_engagement(lane)  # must not raise

    def test_encoder_moved_enough_on_first_check_returns_true(self):
        class _FakeOams:
            """A plain object (not MagicMock) with a normal mutable
            attribute -- safe against the source reading it twice per
            checkpoint (once via `hasattr`, once for the value)."""
            def __init__(self):
                self.encoder_clicks = 0

        oams = _FakeOams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        # Simulate the encoder advancing once the extrude commands are sent.
        ams._oams_extrude = MagicMock(
            side_effect=lambda *a, **k: setattr(oams, "encoder_clicks", 5))
        lane = _make_lane("lane1")

        result = ams._verify_engagement(lane)

        assert result is True
        assert any(
            lvl == "info" and "Engagement verified: encoder moved 5 clicks" in m
            for lvl, m in ams.logger.messages)

    def test_encoder_moved_enough_on_retry_returns_true(self):
        class _FakeOams:
            def __init__(self):
                self.encoder_clicks = 0

        oams = _FakeOams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._oams_extrude = MagicMock()  # no movement from the extrude itself
        pause_calls = {"n": 0}

        def pause(t):
            pause_calls["n"] += 1
            if pause_calls["n"] == 2:
                # Simulate the encoder catching up during the brief retry pause.
                oams.encoder_clicks = 3
        reactor.pause = pause
        lane = _make_lane("lane1")

        result = ams._verify_engagement(lane)

        assert result is True
        assert any(
            lvl == "info"
            and "Engagement verified on retry: encoder moved 3 clicks" in m
            for lvl, m in ams.logger.messages)

    def test_no_encoder_movement_returns_false(self):
        oams = MagicMock()
        oams.encoder_clicks = 0  # never changes
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._oams_extrude = MagicMock()
        lane = _make_lane("lane1")

        result = ams._verify_engagement(lane)

        assert result is False
        assert (
            "error", "Engagement verification failed: encoder moved only 0 clicks"
        ) in ams.logger.messages

    def test_no_oams_returns_false_without_reading_encoder(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        ams._oams_extrude = MagicMock()
        lane = _make_lane("lane1")

        result = ams._verify_engagement(lane)

        assert result is False

    def test_oams_without_encoder_clicks_attr_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=MagicMock(spec=[]))
        ams._oams_extrude = MagicMock()
        lane = _make_lane("lane1")

        result = ams._verify_engagement(lane)

        assert result is False

    def test_two_phase_extrude_for_long_engagement(self):
        ams, afc, printer, reactor = _make_ams()
        ams._engagement_length = 20.0
        ams._engagement_speed = 300.0
        ams._oams_extrude = MagicMock()
        lane = _make_lane("lane1")

        ams._verify_engagement(lane)

        calls = ams._oams_extrude.call_args_list
        assert calls[0][0][0] == 5.0  # prime = min(5.0, 20.0)
        assert calls[1][0][0] == 15.0  # remaining = 20.0 - 5.0

    def test_single_phase_extrude_for_short_engagement(self):
        ams, afc, printer, reactor = _make_ams()
        ams._engagement_length = 3.0  # shorter than the 5mm prime cap
        ams._oams_extrude = MagicMock()
        lane = _make_lane("lane1")

        ams._verify_engagement(lane)

        assert ams._oams_extrude.call_count == 1
        assert ams._oams_extrude.call_args_list[0][0][0] == 3.0


class TestOamsExtrude:
    def test_runs_extrude_gcode(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_extrude(12.5, 300.0, "test")
        afc.gcode.run_script_from_command.assert_called_once_with(
            "G92 E0\nG1 E12.500 F300\nM400")


class TestAdvanceToolStnToNozzle:
    def test_no_remaining_distance_skips_extrude(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_extrude = MagicMock()
        ext = MagicMock()
        ext.tool_stn = 10.0
        lane = _make_lane("lane1", extruder_obj=ext)

        ams._advance_tool_stn_to_nozzle(lane, already_advanced=10.0)

        ams._oams_extrude.assert_not_called()

    def test_negative_remaining_distance_skips_extrude(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_extrude = MagicMock()
        ext = MagicMock()
        ext.tool_stn = 10.0
        lane = _make_lane("lane1", extruder_obj=ext)

        ams._advance_tool_stn_to_nozzle(lane, already_advanced=20.0)

        ams._oams_extrude.assert_not_called()

    def test_advances_remaining_distance(self):
        ams, afc, printer, reactor = _make_ams()
        afc.afcDeltaTime = MagicMock()
        ams._oams_extrude = MagicMock()
        ext = MagicMock()
        ext.tool_stn = 30.0
        ext.tool_load_speed = 25.0
        lane = _make_lane("lane1", extruder_obj=ext)

        ams._advance_tool_stn_to_nozzle(lane, already_advanced=10.0)

        ams._oams_extrude.assert_called_once_with(20.0, 25.0 * 60.0, "tool_stn_to_nozzle")
        afc.afcDeltaTime.log_with_time.assert_called_once()
        assert any(
            lvl == "info" and "advancing 20.0mm to nozzle" in m and "lane1" in m
            for lvl, m in ams.logger.messages)

    def test_default_already_advanced_is_zero(self):
        ams, afc, printer, reactor = _make_ams()
        afc.afcDeltaTime = MagicMock()
        ams._oams_extrude = MagicMock()
        ext = MagicMock()
        ext.tool_stn = 15.0
        ext.tool_load_speed = 25.0
        lane = _make_lane("lane1", extruder_obj=ext)

        ams._advance_tool_stn_to_nozzle(lane)

        ams._oams_extrude.assert_called_once_with(15.0, 25.0 * 60.0, "tool_stn_to_nozzle")

    def test_missing_tool_stn_attribute_defaults_to_zero(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_extrude = MagicMock()
        ext = MagicMock(spec=[])  # no tool_stn attribute
        lane = _make_lane("lane1", extruder_obj=ext)

        ams._advance_tool_stn_to_nozzle(lane)

        ams._oams_extrude.assert_not_called()


class TestIsVirtualHub:
    def test_no_hub_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", hub_obj=None)
        assert ams._is_virtual_hub(lane) is False

    def test_hub_without_is_virtual_pin_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        hub = MagicMock(spec=[])
        lane = _make_lane("lane1", hub_obj=hub)
        assert ams._is_virtual_hub(lane) is False

    def test_hub_reporting_virtual_pin_true(self):
        ams, afc, printer, reactor = _make_ams()
        hub = MagicMock()
        hub.is_virtual_pin.return_value = True
        lane = _make_lane("lane1", hub_obj=hub)
        assert ams._is_virtual_hub(lane) is True

    def test_hub_reporting_not_virtual_pin_false(self):
        ams, afc, printer, reactor = _make_ams()
        hub = MagicMock()
        hub.is_virtual_pin.return_value = False
        lane = _make_lane("lane1", hub_obj=hub)
        assert ams._is_virtual_hub(lane) is False


class TestGetOamsIndex:
    def test_parses_numeric_suffix(self):
        ams, afc, printer, reactor = _make_ams()
        ams.oams_name = "oams3"
        assert ams._get_oams_index() == 3

    def test_unparseable_name_defaults_to_1(self):
        ams, afc, printer, reactor = _make_ams()
        ams.oams_name = "not-numeric-suffix-xyz"
        assert ams._get_oams_index() == 1


class TestGetOpenAmsSpoolIndex:
    def test_mapped_lane_returns_index(self):
        ams, afc, printer, reactor = _make_ams()
        ams._spool_map["lane1"] = 2
        lane = _make_lane("lane1")
        assert ams._get_openams_spool_index(lane) == 2

    def test_unmapped_lane_returns_zero(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("unmapped")
        assert ams._get_openams_spool_index(lane) == 0


class TestResolveLaneReference:
    def test_none_name_returns_none(self):
        ams, afc, printer, reactor = _make_ams()
        assert ams._resolve_lane_reference(None) is None

    def test_exact_match(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        afc.lanes = {"lane1": lane}
        assert ams._resolve_lane_reference("lane1") is lane

    def test_case_insensitive_fallback(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("Lane1")
        afc.lanes = {"Lane1": lane}
        assert ams._resolve_lane_reference("lane1") is lane

    def test_case_insensitive_fallback_skips_non_matching_lanes_first(self):
        ams, afc, printer, reactor = _make_ams()
        other = _make_lane("Other")
        target = _make_lane("Lane2")
        afc.lanes = {"Other": other, "Lane2": target}
        assert ams._resolve_lane_reference("lane2") is target

    def test_no_match_returns_none(self):
        ams, afc, printer, reactor = _make_ams()
        afc.lanes = {}
        assert ams._resolve_lane_reference("missing") is None


class TestIsSameExtruder:
    def test_both_have_matching_extruder_names(self):
        ams, afc, printer, reactor = _make_ams()
        ext = MagicMock()
        ext.name = "Extruder1"
        source = _make_lane("lane1", extruder_obj=ext)
        target = _make_lane("lane2", extruder_obj=ext)
        assert ams._is_same_extruder(source, target) is True

    def test_case_and_whitespace_insensitive(self):
        ams, afc, printer, reactor = _make_ams()
        ext1 = MagicMock(name="e1")
        ext1.name = " Extruder1 "
        ext2 = MagicMock(name="e2")
        ext2.name = "extruder1"
        source = _make_lane("lane1", extruder_obj=ext1)
        target = _make_lane("lane2", extruder_obj=ext2)
        assert ams._is_same_extruder(source, target) is True

    def test_different_extruders_false(self):
        ams, afc, printer, reactor = _make_ams()
        ext1 = MagicMock()
        ext1.name = "Extruder1"
        ext2 = MagicMock()
        ext2.name = "Extruder2"
        source = _make_lane("lane1", extruder_obj=ext1)
        target = _make_lane("lane2", extruder_obj=ext2)
        assert ams._is_same_extruder(source, target) is False

    def test_missing_extruder_obj_false(self):
        ams, afc, printer, reactor = _make_ams()
        source = _make_lane("lane1", extruder_obj=None)
        target = _make_lane("lane2", extruder_obj=MagicMock())
        assert ams._is_same_extruder(source, target) is False

    def test_empty_extruder_name_false(self):
        ams, afc, printer, reactor = _make_ams()
        ext1 = MagicMock()
        ext1.name = ""
        ext2 = MagicMock()
        ext2.name = "Extruder1"
        source = _make_lane("lane1", extruder_obj=ext1)
        target = _make_lane("lane2", extruder_obj=ext2)
        assert ams._is_same_extruder(source, target) is False


class TestGetMonitorState:
    def test_no_monitor_returns_none(self):
        ams, afc, printer, reactor = _make_ams()
        ams._monitor = None
        assert ams._get_monitor_state() is None

    def test_returns_monitor_state(self):
        ams, afc, printer, reactor = _make_ams()
        monitor = MagicMock()
        monitor.state = "the-state"
        ams._monitor = monitor
        assert ams._get_monitor_state() == "the-state"


class TestCalibrateHubHesSpool:
    def test_success_returns_true(self):
        ams, afc, printer, reactor = _make_ams()
        ams._get_oams_index = MagicMock(return_value=1)
        assert ams._calibrate_hub_hes_spool(2) is True
        afc.gcode.run_script_from_command.assert_called_once_with(
            "OAMS_CALIBRATE_HUB_HES OAMS=1 SPOOL=2")

    def test_failure_returns_false_and_logs(self):
        ams, afc, printer, reactor = _make_ams()
        ams._get_oams_index = MagicMock(return_value=1)
        afc.gcode.run_script_from_command.side_effect = Exception("boom")
        assert ams._calibrate_hub_hes_spool(0) is False
        assert (
            "error", "Hub HES calibration failed for spool 0: boom"
        ) in ams.logger.messages


class TestToolheadSensorTriggered:
    def test_uses_filament_sensor_obj_button_state(self):
        ams, afc, printer, reactor = _make_ams()
        sensor = MagicMock()
        sensor.runout_buttun_state = True
        ext = MagicMock()
        ext.filament_sensor_obj = sensor
        lane = _make_lane("lane1", extruder_obj=ext)
        assert ams._toolhead_sensor_triggered(lane) is True

    def test_falls_back_to_fila_tool_start(self):
        ams, afc, printer, reactor = _make_ams()
        sensor = MagicMock()
        sensor.runout_buttun_state = False
        ext = MagicMock(spec=["fila_tool_start"])
        ext.fila_tool_start = sensor
        lane = _make_lane("lane1", extruder_obj=ext)
        assert ams._toolhead_sensor_triggered(lane) is False

    def test_no_sensor_falls_back_to_lane_pre_sensor_state(self):
        ams, afc, printer, reactor = _make_ams()
        ext = MagicMock(spec=[])
        lane = _make_lane("lane1", extruder_obj=ext)
        lane.get_toolhead_pre_sensor_state.return_value = True
        assert ams._toolhead_sensor_triggered(lane) is True


class TestShouldBlockSensorForRunout:
    def test_no_runout_detected_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", _oams_runout_detected=False)
        assert ams._should_block_sensor_for_runout(lane, True) is False

    def test_active_runout_blocks_true_value(self):
        ams, afc, printer, reactor = _make_ams()
        afc.function.is_printing.return_value = True
        lane = _make_lane(
            "lane1", _oams_runout_detected=True, tool_loaded=True,
            status=AFCLaneState.INFINITE_RUNOUT)
        assert ams._should_block_sensor_for_runout(lane, True) is True
        assert any(
            lvl == "debug"
            and "Blocked sensor True for lane1" in m
            for lvl, m in ams.logger.messages)

    def test_active_runout_allows_false_value_and_clears_flag(self):
        ams, afc, printer, reactor = _make_ams()
        afc.function.is_printing.return_value = True
        lane = _make_lane(
            "lane1", _oams_runout_detected=True, tool_loaded=True,
            status=AFCLaneState.INFINITE_RUNOUT)
        result = ams._should_block_sensor_for_runout(lane, False)
        assert result is False
        assert lane._oams_runout_detected is False

    def test_not_printing_clears_flag_and_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        afc.function.is_printing.return_value = False
        lane = _make_lane(
            "lane1", _oams_runout_detected=True, tool_loaded=True,
            status=AFCLaneState.INFINITE_RUNOUT)
        assert ams._should_block_sensor_for_runout(lane, True) is False
        assert lane._oams_runout_detected is False

    def test_wrong_status_clears_flag_and_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        afc.function.is_printing.return_value = True
        lane = _make_lane(
            "lane1", _oams_runout_detected=True, tool_loaded=True,
            status=AFCLaneState.NONE)
        assert ams._should_block_sensor_for_runout(lane, True) is False

    def test_exception_checking_state_treated_as_inactive(self):
        ams, afc, printer, reactor = _make_ams()
        afc.function.is_printing.side_effect = Exception("boom")
        lane = _make_lane("lane1", _oams_runout_detected=True)
        assert ams._should_block_sensor_for_runout(lane, True) is False


class TestWaitForIdle:
    def test_no_oams_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        assert ams._wait_for_idle() is False

    def test_idle_immediately_returns_true(self):
        oams = MagicMock()
        oams.action_status = None
        ams, afc, printer, reactor = _make_ams(oams=oams)
        assert ams._wait_for_idle() is True

    def test_timeout_returns_false(self):
        oams = MagicMock()
        oams.action_status = OAMSStatus.LOADING
        ams, afc, printer, reactor = _make_ams(oams=oams)
        times = iter([0.0, 0.0, 40.0])

        def monotonic():
            return next(times, 40.0)
        reactor.monotonic = monotonic
        reactor.pause = MagicMock()

        assert ams._wait_for_idle(timeout=30.0) is False
        reactor.pause.assert_called_once()
        assert ("error", "OAMS idle timeout") in ams.logger.messages


class TestPrepCaptureTd1:
    def test_not_configured_for_td1_returns_none(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", td1_when_loaded=False)
        assert ams.prep_capture_td1(lane) is None

    def test_lane_already_active_returns_none(self):
        ams, afc, printer, reactor = _make_ams()
        afc.function.get_current_lane_obj.return_value = MagicMock()
        lane = _make_lane("lane1", td1_when_loaded=True)
        assert ams.prep_capture_td1(lane) is None

    def test_delegates_to_capture_helper(self):
        ams, afc, printer, reactor = _make_ams()
        afc.function.get_current_lane_obj.return_value = None
        ams._capture_td1_with_oams = MagicMock(return_value=(True, "ok"))
        lane = _make_lane("lane1", td1_when_loaded=True)

        result = ams.prep_capture_td1(lane)

        assert result == (True, "ok")
        ams._capture_td1_with_oams.assert_called_once_with(
            lane, require_loaded=True, require_enabled=False)


class TestCaptureTd1Data:
    def test_delegates_to_capture_helper(self):
        ams, afc, printer, reactor = _make_ams()
        ams._capture_td1_with_oams = MagicMock(return_value=(False, "no data"))
        lane = _make_lane("lane1")

        result = ams.capture_td1_data(lane)

        assert result == (False, "no data")
        ams._capture_td1_with_oams.assert_called_once_with(
            lane, require_loaded=True, require_enabled=False)


class TestWaitForHubSettle:
    @staticmethod
    def _clock(step=0.2):
        """A monotonic() stand-in that always advances -- unlike a short
        fixed list, it can't strand the polling loop in _wait_for_hub_settle
        with a value that never reaches the deadline or stable_time."""
        counter = itertools.count(step, step)
        return lambda: next(counter)

    def test_no_oams_returns_true_immediately(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        assert ams._wait_for_hub_settle(0) is True

    def test_already_clear_and_stable_returns_true(self):
        oams = MagicMock()
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        reactor.monotonic = self._clock(step=0.2)

        result = ams._wait_for_hub_settle(0, timeout=4.0, stable_time=0.3)

        assert result is True

    def test_still_present_resets_clear_since_and_eventually_times_out(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]  # never clears
        ams, afc, printer, reactor = _make_ams(oams=oams)
        reactor.monotonic = self._clock(step=1.0)  # reaches the 4s deadline quickly
        reactor.pause = MagicMock()

        result = ams._wait_for_hub_settle(0, timeout=4.0)

        assert result is False
        assert (
            "warning",
            "OAMS hub HES did not settle clear within 4.0s (spool 0); proceeding",
        ) in ams.logger.messages

    def test_reading_exception_treated_as_not_present(self):
        class _RaisingOams:
            @property
            def hub_hes_value(self):
                raise Exception("boom")

        ams, afc, printer, reactor = _make_ams(oams=_RaisingOams())
        reactor.monotonic = self._clock(step=0.2)

        result = ams._wait_for_hub_settle(0, timeout=4.0, stable_time=0.3)

        # hub_present is always treated as False (exception path), so it
        # settles "clear" immediately, same as the empty-array case.
        assert result is True

    def test_spool_index_out_of_range_treated_as_not_present(self):
        oams = MagicMock()
        oams.hub_hes_value = [1]  # length 1, index 2 out of range
        ams, afc, printer, reactor = _make_ams(oams=oams)
        reactor.monotonic = self._clock(step=0.2)

        result = ams._wait_for_hub_settle(2, timeout=4.0, stable_time=0.3)

        assert result is True

    def test_polls_while_waiting_for_stability(self):
        oams = MagicMock()
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        reactor.monotonic = self._clock(step=0.1)  # needs >=3 loops to reach 0.3s
        reactor.pause = MagicMock()

        result = ams._wait_for_hub_settle(0, timeout=4.0, stable_time=0.3)

        assert result is True
        assert reactor.pause.call_count >= 1


class TestCheckRunout:
    def test_none_lane_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        assert ams.check_runout(None) is False

    def test_not_printing_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        afc.function.is_printing.return_value = False
        lane = _make_lane("lane1", tool_loaded=True)
        assert ams.check_runout(lane) is False

    def test_not_tool_loaded_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        afc.function.is_printing.return_value = True
        lane = _make_lane("lane1", tool_loaded=False)
        assert ams.check_runout(lane) is False

    def test_extruder_loaded_lane_mismatch_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        afc.function.is_printing.return_value = True
        ext = MagicMock()
        ext.lane_loaded = "other_lane"
        lane = _make_lane("lane1", tool_loaded=True, extruder_obj=ext)
        assert ams.check_runout(lane) is False

    def test_matching_lane_returns_true(self):
        ams, afc, printer, reactor = _make_ams()
        afc.function.is_printing.return_value = True
        ext = MagicMock()
        ext.lane_loaded = "lane1"
        lane = _make_lane("lane1", tool_loaded=True, extruder_obj=ext)
        assert ams.check_runout(lane) is True

    def test_is_printing_exception_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        afc.function.is_printing.side_effect = Exception("boom")
        lane = _make_lane("lane1")
        assert ams.check_runout(lane) is False


class TestHandleSameFpsReload:
    def test_success_switches_active_lane(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_load = MagicMock(return_value=True)
        ams.lane_not_ready = MagicMock()
        ams.lane_tool_loaded = MagicMock()
        ams.gcode = MagicMock()
        source = _make_lane("lane1", map=None)
        target = _make_lane("lane2")

        result = ams.handle_same_fps_reload(source, target)

        assert result is True
        assert source.status == AFCLaneState.NONE
        ams.lane_not_ready.assert_called_once_with(source)
        target.set_tool_loaded.assert_called_once()
        ams.lane_tool_loaded.assert_called_once_with(target)
        afc.save_vars.assert_called_once()
        assert (
            "info", "Same-FPS infinite runout: lane1 -> lane2"
        ) in ams.logger.messages
        assert (
            "info", "Same-FPS reload complete: lane2 now active"
        ) in ams.logger.messages

    def test_remaps_source_map_when_present(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_load = MagicMock(return_value=True)
        ams.lane_not_ready = MagicMock()
        ams.lane_tool_loaded = MagicMock()
        ams.gcode = MagicMock()
        source = _make_lane("lane1", map="T0")
        target = _make_lane("lane2")

        ams.handle_same_fps_reload(source, target)

        ams.gcode.run_script_from_command.assert_called_once_with(
            "SET_MAP LANE=lane2 MAP=T0")
        assert (
            "info", "Remapped T0 from lane1 to lane2"
        ) in ams.logger.messages

    def test_no_source_map_skips_remap(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_load = MagicMock(return_value=True)
        ams.lane_not_ready = MagicMock()
        ams.lane_tool_loaded = MagicMock()
        ams.gcode = MagicMock()
        source = _make_lane("lane1", map=None)
        target = _make_lane("lane2")

        ams.handle_same_fps_reload(source, target)

        ams.gcode.run_script_from_command.assert_not_called()

    def test_hardware_load_failure_pauses_and_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_load = MagicMock(return_value=False)
        ams.lane_not_ready = MagicMock()
        source = _make_lane("lane1")
        target = _make_lane("lane2")

        result = ams.handle_same_fps_reload(source, target)

        assert result is False
        afc.error.AFC_error.assert_called_once()
        assert afc.error.AFC_error.call_args[1]["pause"] is True
        target.set_tool_loaded.assert_not_called()
        assert (
            "error", "Same-FPS reload failed for lane2"
        ) in ams.logger.messages

    def test_hardware_load_exception_pauses_and_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_load = MagicMock(side_effect=Exception("mcu fault"))
        ams.lane_not_ready = MagicMock()
        source = _make_lane("lane1")
        target = _make_lane("lane2")

        result = ams.handle_same_fps_reload(source, target)

        assert result is False
        msg = afc.error.AFC_error.call_args[0][0]
        assert "mcu fault" in msg
        assert (
            "error", "Same-FPS reload exception: mcu fault"
        ) in ams.logger.messages


class TestHandleRunout:
    def test_no_runout_lane_configured_pauses(self):
        ams, afc, printer, reactor = _make_ams()
        ams.lane_not_ready = MagicMock()
        lane = _make_lane("lane1", runout_lane=None)

        result = ams.handle_runout(lane)

        assert result is True
        assert lane.status == AFCLaneState.NONE
        ams.lane_not_ready.assert_called_once_with(lane)
        afc.error.AFC_error.assert_called_once()
        assert afc.error.AFC_error.call_args[1]["pause"] is True

    def test_runout_lane_not_found_pauses(self):
        ams, afc, printer, reactor = _make_ams()
        ams.lane_not_ready = MagicMock()
        ams._resolve_lane_reference = MagicMock(return_value=None)
        lane = _make_lane("lane1", runout_lane="missing_lane")

        result = ams.handle_runout(lane)

        assert result is True
        ams.lane_not_ready.assert_called_once_with(lane)
        afc.error.AFC_error.assert_called_once()

    def test_same_extruder_triggers_seamless_reload(self):
        ams, afc, printer, reactor = _make_ams()
        target = _make_lane("lane2")
        ams._resolve_lane_reference = MagicMock(return_value=target)
        ams._is_same_extruder = MagicMock(return_value=True)
        ams.handle_same_fps_reload = MagicMock()
        lane = _make_lane("lane1", runout_lane="lane2")

        result = ams.handle_runout(lane)

        assert result is True
        assert lane._oams_runout_detected is True
        ams.handle_same_fps_reload.assert_called_once_with(lane, target)
        assert (
            "info", "OAMS same-FPS runout: lane1 -> lane2, seamless reload"
        ) in ams.logger.messages

    def test_cross_extruder_defers_to_generic_infinite_runout(self):
        ams, afc, printer, reactor = _make_ams()
        target = _make_lane("lane2")
        ams._resolve_lane_reference = MagicMock(return_value=target)
        ams._is_same_extruder = MagicMock(return_value=False)
        lane = _make_lane("lane1", runout_lane="lane2")

        result = ams.handle_runout(lane)

        assert result is False
        assert lane._oams_runout_empty is True
        assert any(
            lvl == "info" and "OAMS cross-extruder runout: lane1 -> lane2" in m
            for lvl, m in ams.logger.messages)


# ── Simple unit-interface overrides ───────────────────────────────────────

class TestSimpleOverrides:
    def test_prep_load_is_noop(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        assert ams.prep_load(lane) is None

    def test_prep_post_load_is_noop(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        assert ams.prep_post_load(lane) is None

    def test_eject_lane_logs_info(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        ams.eject_lane(lane)
        assert (
            "info",
            "Eject not supported for OpenAMS lane lane1. "
            "Remove spool physically or use TOOL_UNLOAD.",
        ) in ams.logger.messages

    def test_lane_move_logs_info(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        ams.lane_move(lane, 10, "short")
        assert (
            "info",
            "Lane move not supported for OpenAMS lane lane1. "
            "OpenAMS firmware controls filament movement.",
        ) in ams.logger.messages

    def test_get_lane_reset_command_returns_none(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        assert ams.get_lane_reset_command(lane, 100) is None

    def test_lane_unload_no_oams_returns_none(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        assert ams.lane_unload(_make_lane("lane1")) is None

    def test_lane_unload_success_returns_true(self):
        oams = MagicMock()
        oams.action_status = None
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        assert ams.lane_unload(_make_lane("lane1")) is True
        oams.unload_spool_with_retry.assert_called_once()
        assert ams._wait_for_idle.call_count == 2

    def test_lane_unload_exception_logged_still_returns_true(self):
        oams = MagicMock()
        oams.action_status = None
        oams.unload_spool_with_retry.side_effect = Exception("boom")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        assert ams.lane_unload(_make_lane("lane1")) is True
        assert (
            "error", "OpenAMS lane_unload failed: boom"
        ) in ams.logger.messages


# ── calibrate_lane / calibrate_bowden ─────────────────────────────────────

class TestCalibrateLane:
    def test_no_oams_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        lane = _make_lane("lane1", load_state=True)
        success, msg, dist = ams.calibrate_lane(lane, 0.1)
        assert success is False
        assert "not available" in msg

    def test_not_loaded_returns_false(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        lane = _make_lane("lane1", load_state=False)
        success, msg, dist = ams.calibrate_lane(lane, 0.1)
        assert success is False
        assert "not loaded" in msg

    def test_success_returns_true(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._calibrate_hub_hes_spool = MagicMock(return_value=True)
        lane = _make_lane("lane1", load_state=True)
        success, msg, dist = ams.calibrate_lane(lane, 0.1)
        assert success is True
        assert msg == "calibration_lane"
        assert (
            "info", "Running HUB HES calibration for lane1"
        ) in ams.logger.messages

    def test_hardware_calibration_failure_returns_false(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._calibrate_hub_hes_spool = MagicMock(return_value=False)
        lane = _make_lane("lane1", load_state=True)
        success, msg, dist = ams.calibrate_lane(lane, 0.1)
        assert success is False
        assert "failed" in msg


class TestCalibrateBowden:
    def test_no_oams_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        lane = _make_lane("lane1", load_state=True)
        success, msg, dist = ams.calibrate_bowden(lane, 100, 0.1)
        assert success is False

    def test_not_loaded_returns_false(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        lane = _make_lane("lane1", load_state=False)
        success, msg, dist = ams.calibrate_bowden(lane, 100, 0.1)
        assert success is False
        assert "not loaded" in msg

    def test_success_runs_gcode(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._get_oams_index = MagicMock(return_value=1)
        ams._get_openams_spool_index = MagicMock(return_value=2)
        lane = _make_lane("lane1", load_state=True)
        success, msg, dist = ams.calibrate_bowden(lane, 100, 0.1)
        assert success is True
        afc.gcode.run_script_from_command.assert_called_once_with(
            "OAMS_CALIBRATE_PTFE_LENGTH OAMS=1 SPOOL=2")
        assert (
            "info", "Running PTFE calibration for lane1"
        ) in ams.logger.messages

    def test_gcode_exception_returns_false(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.gcode.run_script_from_command.side_effect = Exception("boom")
        lane = _make_lane("lane1", load_state=True)
        success, msg, dist = ams.calibrate_bowden(lane, 100, 0.1)
        assert success is False
        assert "failed" in msg


# ── unit_load_lane / unit_unload_lane ──────────────────────────────────────

class TestUnitLoadLane:
    def test_success_returns_true(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_load_sequence = MagicMock(return_value=True)
        lane = _make_lane("lane1")
        assert ams.unit_load_lane(lane, MagicMock()) is True
        afc.error.handle_lane_failure.assert_not_called()

    def test_failure_calls_handle_lane_failure_and_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_load_sequence = MagicMock(return_value=False)
        afc.function.in_print.return_value = True
        lane = _make_lane("lane1")
        result = ams.unit_load_lane(lane, MagicMock())
        assert result is False
        afc.error.handle_lane_failure.assert_called_once()
        _, kwargs = afc.error.handle_lane_failure.call_args
        assert kwargs["pause"] is True


class TestUnitUnloadLane:
    def test_success_saves_vars_and_returns_true(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_unload_sequence = MagicMock(return_value=True)
        afc.post_unload_macro = None
        afc.move_e_pos = MagicMock()
        afc.do_tool_cut_tip_form = MagicMock()
        extruder = MagicMock()
        extruder.tool_unload_speed = 25.0
        lane = _make_lane("lane1")
        result = ams.unit_unload_lane(lane, extruder)
        assert result is True
        afc.save_vars.assert_called_once()
        assert lane.status == AFCLaneState.NONE
        afc.move_e_pos.assert_called_once_with(-2, 25.0, "Quick Pull", wait_tool=False)
        lane.disable_buffer.assert_called_once()
        lane.sync_to_extruder.assert_called_once()
        lane.select_lane.assert_called_once()
        afc.do_tool_cut_tip_form.assert_called_once_with(lane, extruder)
        lane.set_tool_unloaded.assert_called_once_with(normal_toolchange=True)

    def test_failure_returns_false_without_saving(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_unload_sequence = MagicMock(return_value=False)
        afc.move_e_pos = MagicMock()
        afc.do_tool_cut_tip_form = MagicMock()
        lane = _make_lane("lane1")
        result = ams.unit_unload_lane(lane, MagicMock())
        assert result is False
        afc.save_vars.assert_not_called()

    def test_runs_post_unload_macro_when_configured(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_unload_sequence = MagicMock(return_value=True)
        afc.post_unload_macro = "MY_MACRO"
        afc.move_e_pos = MagicMock()
        afc.do_tool_cut_tip_form = MagicMock()
        ams.gcode = MagicMock()
        lane = _make_lane("lane1")
        ams.unit_unload_lane(lane, MagicMock())
        ams.gcode.run_script_from_command.assert_called_once_with("MY_MACRO")


class TestLaneUnloadingAndPrepareUnload:
    def test_prepare_unload_stops_follower(self):
        ams, afc, printer, reactor = _make_ams()
        follower = MagicMock()
        ams._follower = follower
        lane = _make_lane("lane1")
        ams.prepare_unload(lane, None, None)
        follower.set_follower_state.assert_called_once()
        args = follower.set_follower_state.call_args[0]
        assert args[2] == 0 and args[3] == 0  # enable=0, direction=0

    def test_prepare_unload_no_follower_is_noop(self):
        ams, afc, printer, reactor = _make_ams()
        ams._follower = None
        lane = _make_lane("lane1")
        ams.prepare_unload(lane, None, None)  # must not raise

    def test_lane_unloading_calls_prepare_unload(self):
        ams, afc, printer, reactor = _make_ams()
        ams.prepare_unload = MagicMock()
        lane = _make_lane("lane1")
        super_mock = MagicMock()
        with patch.object(afcUnit, "lane_unloading", super_mock):
            ams.lane_unloading(lane)
        ams.prepare_unload.assert_called_once()
        super_mock.assert_called_once_with(lane)

    def test_lane_unloading_prepare_unload_exception_logged(self):
        ams, afc, printer, reactor = _make_ams()
        ams.prepare_unload = MagicMock(side_effect=Exception("boom"))
        lane = _make_lane("lane1")
        with patch.object(afcUnit, "lane_unloading", MagicMock()):
            ams.lane_unloading(lane)  # must not raise
        assert (
            "warning", "OAMS: lane_unloading follower-stop error for lane1: boom"
        ) in ams.logger.messages


class TestOamsLoadSequence:
    def test_wraps_operation_active_flag_around_inner_call(self):
        ams, afc, printer, reactor = _make_ams()
        seen_active = {}

        def fake_inner(lane, ext):
            seen_active["during"] = ams._operation_active
            return True
        ams._oams_load_inner = fake_inner
        lane = _make_lane("lane1")

        result = ams._oams_load_sequence(lane, MagicMock())

        assert result is True
        assert seen_active["during"] is True
        assert ams._operation_active is False
        assert ams._prev_states_stale is True

    def test_clears_operation_active_even_on_exception(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_load_inner = MagicMock(side_effect=Exception("boom"))
        lane = _make_lane("lane1")

        with pytest.raises(Exception):
            ams._oams_load_sequence(lane, MagicMock())

        assert ams._operation_active is False
        assert ams._prev_states_stale is True

    def test_propagates_inner_failure(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_load_inner = MagicMock(return_value=False)
        lane = _make_lane("lane1")
        assert ams._oams_load_sequence(lane, MagicMock()) is False


class TestOamsLoadInner:
    def test_clears_hub_load_suppression_for_lane(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_load = MagicMock(return_value=True)
        ams._hub_load_suppressed.add("lane1")
        lane = _make_lane("lane1")

        ams._oams_load_inner(lane, MagicMock())

        assert "lane1" not in ams._hub_load_suppressed

    def test_hardware_load_failure_returns_false(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_load = MagicMock(return_value=False)
        lane = _make_lane("lane1")

        result = ams._oams_load_inner(lane, MagicMock())

        assert result is False
        afc.save_vars.assert_not_called()

    def test_hardware_load_success_finalizes_lane_state(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_load = MagicMock(return_value=True)
        lane = _make_lane("lane1")

        result = ams._oams_load_inner(lane, MagicMock())

        assert result is True
        assert lane.loaded_to_hub is True
        assert lane.status == AFCLaneState.TOOL_LOADED
        afc.save_vars.assert_called_once()


class TestOamsUnloadSequence:
    def test_wraps_operation_active_flag_around_inner_call(self):
        ams, afc, printer, reactor = _make_ams()
        seen_active = {}

        def fake_inner(lane, ext):
            seen_active["during"] = ams._operation_active
            return True
        ams._oams_unload_inner = fake_inner
        lane = _make_lane("lane1")

        result = ams._oams_unload_sequence(lane, MagicMock())

        assert result is True
        assert seen_active["during"] is True
        assert ams._operation_active is False
        assert ams._prev_states_stale is True

    def test_clears_operation_active_even_on_exception(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_unload_inner = MagicMock(side_effect=Exception("boom"))
        lane = _make_lane("lane1")

        with pytest.raises(Exception):
            ams._oams_unload_sequence(lane, MagicMock())

        assert ams._operation_active is False
        assert ams._prev_states_stale is True


class TestOamsUnloadInner:
    def test_hardware_unload_failure_calls_handle_lane_failure(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_unload = MagicMock(return_value=False)
        afc.function.in_print.return_value = True
        lane = _make_lane("lane1")

        result = ams._oams_unload_inner(lane, MagicMock())

        assert result is False
        afc.error.handle_lane_failure.assert_called_once()
        assert afc.error.handle_lane_failure.call_args[1]["pause"] is True

    def test_hardware_unload_failure_pause_reflects_in_print_false(self):
        ams, afc, printer, reactor = _make_ams()
        ams._oams_unload = MagicMock(return_value=False)
        afc.function.in_print.return_value = False
        lane = _make_lane("lane1")

        ams._oams_unload_inner(lane, MagicMock())

        assert afc.error.handle_lane_failure.call_args[1]["pause"] is False

    def test_hardware_unload_success_finalizes_state(self):
        ams, afc, printer, reactor = _make_ams()
        afc.afcDeltaTime = MagicMock()
        ams._oams_unload = MagicMock(return_value=True)
        ams.lane_tool_unloaded = MagicMock()
        lane = _make_lane("lane1")

        result = ams._oams_unload_inner(lane, MagicMock())

        assert result is True
        ams.lane_tool_unloaded.assert_called_once_with(lane)
        assert "lane1" in ams._hub_load_suppressed
        afc.afcDeltaTime.log_with_time.assert_called_once()


# ── handle_ready / _init_follower_and_monitor / _sync_lanes_from_hardware ──

class TestAfcAMSHandleConnect:
    def test_builds_logos(self):
        ams, afc, printer, reactor = _make_ams()
        with patch.object(afcUnit, "handle_connect", MagicMock()):
            ams.handle_connect()
        assert ams.logo != ""
        assert ams.logo_error != ""

    def test_calls_super(self):
        ams, afc, printer, reactor = _make_ams()
        super_mock = MagicMock()
        with patch.object(afcUnit, "handle_connect", super_mock):
            ams.handle_connect()
        super_mock.assert_called_once()

    def test_builds_spool_map_from_lane_index(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", index=3)
        ams.lanes = {"lane1": lane}
        with patch.object(afcUnit, "handle_connect", MagicMock()):
            ams.handle_connect()
        assert ams._spool_map["lane1"] == 2  # index(3) - 1

    def test_negative_slot_clamped_to_zero(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", index=0)  # slot would be -1
        ams.lanes = {"lane1": lane}
        with patch.object(afcUnit, "handle_connect", MagicMock()):
            ams.handle_connect()
        assert ams._spool_map["lane1"] == 0

    def test_engagement_length_override_uses_lane_speed(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", index=1, engagement_length=15.0, engagement_speed=250.0)
        ams.lanes = {"lane1": lane}
        with patch.object(afcUnit, "handle_connect", MagicMock()):
            ams.handle_connect()
        assert ams._engagement_params["lane1"] == (15.0, 250.0)

    def test_engagement_length_override_falls_back_to_unit_speed(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", index=1, engagement_length=15.0, engagement_speed=None)
        ams.lanes = {"lane1": lane}
        with patch.object(afcUnit, "handle_connect", MagicMock()):
            ams.handle_connect()
        assert ams._engagement_params["lane1"] == (15.0, ams._engagement_speed)

    def test_no_engagement_length_override_skips_params_entry(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", index=1, engagement_length=None)
        ams.lanes = {"lane1": lane}
        with patch.object(afcUnit, "handle_connect", MagicMock()):
            ams.handle_connect()
        assert "lane1" not in ams._engagement_params

    def test_seeds_virtual_sensor_state_false(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", index=1)
        ams.lanes = {"lane1": lane}
        with patch.object(afcUnit, "handle_connect", MagicMock()):
            ams.handle_connect()
        assert lane.prep_state is False
        assert lane._load_state is False
        assert lane.loaded_to_hub is False
        assert lane.status == AFCLaneState.NONE
        assert lane._oams_runout_detected is False



class TestInitFollowerAndMonitor:
    def test_no_oams_is_noop(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        ams._init_follower_and_monitor()
        assert ams._follower is None
        assert ams._monitor is None

    def test_creates_follower_when_oams_present(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._init_follower_and_monitor()
        assert isinstance(ams._follower, FollowerController)

    def test_creates_monitor_when_fps_buffer_found(self):
        oams = MagicMock()
        buf = MagicMock()
        buf.get_fps_value = MagicMock()
        buf.name = "fps1"
        lane = _make_lane("lane1", buffer_obj=buf)
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._init_follower_and_monitor()
        assert isinstance(ams._monitor, OAMSMonitor)

    def test_no_monitor_when_no_fps_buffer(self):
        oams = MagicMock()
        lane = _make_lane("lane1", buffer_obj=None)
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._init_follower_and_monitor()
        assert ams._monitor is None

    def test_follower_creation_failure_logged(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        with patch("extras.AFC_OpenAMS.FollowerController", side_effect=Exception("boom")):
            ams._init_follower_and_monitor()
        assert ams._follower is None
        assert ("error", "Failed to init follower: boom") in ams.logger.messages

    def test_follower_controller_class_unavailable_skips_creation(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        with patch("extras.AFC_OpenAMS.FollowerController", None):
            ams._init_follower_and_monitor()
        assert ams._follower is None

    def test_monitor_creation_failure_logged(self):
        oams = MagicMock()
        buf = MagicMock()
        buf.get_fps_value = MagicMock()
        buf.name = "fps1"
        lane = _make_lane("lane1", buffer_obj=buf)
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        with patch("extras.AFC_OpenAMS.OAMSMonitor", side_effect=Exception("boom")):
            ams._init_follower_and_monitor()
        assert ams._monitor is None
        assert ("error", "Failed to init monitor: boom") in ams.logger.messages


class TestHandleReady:
    def test_super_call_exception_swallowed(self):
        ams, afc, printer, reactor = _make_ams()
        printer._objects[f"AFC_OAMS {ams.oams_name}"] = None
        with patch.object(afcUnit, "handle_ready", MagicMock(side_effect=Exception("x"))):
            ams.handle_ready()  # must not raise
        assert ("debug", "afcUnit.handle_ready: x") in ams.logger.messages

    def test_oams_not_found_logs_warning_and_returns(self):
        ams, afc, printer, reactor = _make_ams()
        with patch.object(afcUnit, "handle_ready", MagicMock()):
            ams.handle_ready()
        assert ams.oams is None
        assert (
            "warning",
            "OpenAMS hardware '[AFC_OAMS oams1]' not found for 'ams1'. "
            "Sensor state will not update.",
        ) in ams.logger.messages
        assert ams._poll_timer is None

    def test_oams_found_initializes_and_starts_polling(self):
        ams, afc, printer, reactor = _make_ams()
        oams = MagicMock()
        printer._objects[f"AFC_OAMS {ams.oams_name}"] = oams
        ams._init_follower_and_monitor = MagicMock()
        ams._sync_lanes_from_hardware = MagicMock()
        with patch.object(afcUnit, "handle_ready", MagicMock()):
            ams.handle_ready()
        assert ams.oams is oams
        ams._init_follower_and_monitor.assert_called_once()
        ams._sync_lanes_from_hardware.assert_called_once()
        assert ams._poll_timer is not None


class TestSyncLanesFromHardware:
    def test_no_oams_is_noop(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        ams._sync_lanes_from_hardware()  # must not raise

    def test_unmapped_lane_skipped(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        lane = _make_lane("lane1")
        lane.prep_state = False
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._sync_lanes_from_hardware()
        assert not hasattr(lane, "prep_state") or lane.prep_state != True

    def test_seeds_prep_and_load_state_from_sensors(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1, 0, 0, 0]
        oams.hub_hes_value = [0, 1, 0, 0]
        lane = _make_lane("lane1")
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._spool_map["lane1"] = 0
        ams._sync_lanes_from_hardware()
        assert lane.prep_state is True
        assert lane._load_state is False  # slot 0 hub value is 0
        assert lane.loaded_to_hub is True
        assert ams._last_f1s[0] is True
        assert ams._last_hub[0] is False

    def test_slot_out_of_range_for_sensor_arrays_skips_last_seen_update(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1]  # only 1 entry
        oams.hub_hes_value = [1]
        lane = _make_lane("lane1")
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._spool_map["lane1"] = 2  # within _last_f1s/_last_hub bounds (len 4)
        # but out of range for the 1-entry sensor arrays above

        ams._sync_lanes_from_hardware()

        assert lane.prep_state is False
        assert lane._load_state is False
        assert ams._last_f1s[2] is None  # never touched
        assert ams._last_hub[2] is None


class TestPollOamsSensors:
    def test_no_oams_returns_never(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        assert ams._poll_oams_sensors(0.0) == reactor.NEVER

    def test_operation_active_defers(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._operation_active = True
        assert ams._poll_oams_sensors(100.0) == 102.0

    def test_f1s_change_triggers_handle_load_runout(self):
        oams = MagicMock()
        oams.f1s_hes_value = [0, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        lane = _make_lane("lane1")
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._spool_map["lane1"] = 0
        ams._last_f1s[0] = True  # was present, now gone
        ams._should_block_sensor_for_runout = MagicMock(return_value=False)

        ams._poll_oams_sensors(100.0)

        lane.handle_load_runout.assert_called_once_with(100.0, False)
        assert ams._last_f1s[0] is False

    def test_f1s_change_blocked_by_runout_guard_skips_handle(self):
        oams = MagicMock()
        oams.f1s_hes_value = [0, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        lane = _make_lane("lane1")
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._spool_map["lane1"] = 0
        ams._last_f1s[0] = True
        ams._should_block_sensor_for_runout = MagicMock(return_value=True)

        ams._poll_oams_sensors(100.0)

        lane.handle_load_runout.assert_not_called()
        assert ams._last_f1s[0] is False

    def test_f1s_lost_clears_loaded_to_hub(self):
        oams = MagicMock()
        oams.f1s_hes_value = [0, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        lane = _make_lane("lane1", loaded_to_hub=True)
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._spool_map["lane1"] = 0

        ams._poll_oams_sensors(100.0)

        assert lane.loaded_to_hub is False

    def test_resync_prev_skips_runout_handling_but_updates_cache(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        lane = _make_lane("lane1")
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._spool_map["lane1"] = 0
        ams._last_f1s[0] = False
        ams._prev_states_stale = True

        ams._poll_oams_sensors(100.0)

        lane.handle_load_runout.assert_not_called()
        assert ams._last_f1s[0] is True
        assert ams._prev_states_stale is False

    def test_suppressed_lane_clears_suppression_flag(self):
        oams = MagicMock()
        oams.f1s_hes_value = [0, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        lane = _make_lane("lane1")
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._spool_map["lane1"] = 0
        ams._last_f1s[0] = True
        ams._hub_load_suppressed.add("lane1")
        ams._should_block_sensor_for_runout = MagicMock(return_value=False)

        ams._poll_oams_sensors(100.0)

        assert lane._load_suppressed is True
        assert "lane1" not in ams._hub_load_suppressed

    def test_hub_change_updates_raw_load_state(self):
        oams = MagicMock()
        oams.f1s_hes_value = [0, 0, 0, 0]
        oams.hub_hes_value = [1, 0, 0, 0]
        lane = _make_lane("lane1")
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._spool_map["lane1"] = 0

        ams._poll_oams_sensors(100.0)

        assert lane._load_state is True
        assert ams._last_hub[0] is True

    def test_reschedules_two_seconds_later(self):
        oams = MagicMock()
        oams.f1s_hes_value = []
        oams.hub_hes_value = []
        ams, afc, printer, reactor = _make_ams(oams=oams)
        result = ams._poll_oams_sensors(50.0)
        assert result == 52.0

    def test_unmapped_lane_is_skipped(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        lane = _make_lane("lane1")
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        # No entry in ams._spool_map for "lane1" -> slot defaults to -1 -> skip
        ams._poll_oams_sensors(100.0)
        lane.handle_load_runout.assert_not_called()

    def test_slot_out_of_range_for_f1s_array_skips_f1s_handling(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1]  # length 1
        oams.hub_hes_value = [1, 1, 1]  # in range for slot 2
        lane = _make_lane("lane1")
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._spool_map["lane1"] = 2

        ams._poll_oams_sensors(100.0)

        lane.handle_load_runout.assert_not_called()
        # hub handling still runs since slot 2 is in range for hub_hes_value
        assert lane._load_state is True

    def test_slot_out_of_range_for_hub_array_skips_hub_handling(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1, 1, 1]  # in range for slot 2
        oams.hub_hes_value = [1]  # length 1
        lane = _make_lane("lane1", _load_state="unchanged")
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams._spool_map["lane1"] = 2

        ams._poll_oams_sensors(100.0)

        assert lane._load_state == "unchanged"
        assert ams._last_hub[2] is None


def _make_gcmd(values=None):
    """Minimal gcmd stand-in matching AFC's GCodeCommand-ish interface."""
    values = values or {}
    gcmd = MagicMock()

    def get_float(name, default=None, **kwargs):
        return values.get(name, default)

    def get_int(name, default=None, **kwargs):
        return values.get(name, default)

    gcmd.get_float = MagicMock(side_effect=get_float)
    gcmd.get_int = MagicMock(side_effect=get_int)
    gcmd.error = MagicMock(side_effect=lambda msg: Exception(msg))
    gcmd.respond_info = MagicMock()
    return gcmd


# ── Stuck spool / clog callbacks ───────────────────────────────────────────

class TestOnStuckSpoolDetected:
    def test_pauses_print_when_auto_recovery_disabled(self):
        ams, afc, printer, reactor = _make_ams()
        ams.stuck_spool_auto_recovery = False
        ams._on_stuck_spool_detected(fps_name="fps1", message="Stuck")
        afc.error.AFC_error.assert_called_once()
        args, kwargs = afc.error.AFC_error.call_args
        assert "Print paused" in args[0]
        assert kwargs["pause"] is True

    def test_sets_led_error_on_stuck_bay(self):
        ams, afc, printer, reactor = _make_ams()
        monitor = MagicMock()
        monitor.state.current_lane = "lane1"
        monitor.state.current_spool_idx = 2
        ams._monitor = monitor
        follower = MagicMock()
        ams._follower = follower
        ams.stuck_spool_auto_recovery = False

        ams._on_stuck_spool_detected(fps_name="fps1")

        follower.set_led_error_if_changed.assert_called_once()

    def test_auto_recovery_enabled_triggers_recovery_not_pause(self):
        ams, afc, printer, reactor = _make_ams()
        monitor = MagicMock()
        monitor.state.current_lane = "lane1"
        monitor.state.current_spool_idx = None
        ams._monitor = monitor
        ams.stuck_spool_auto_recovery = True
        ams._on_stuck_spool_recovery_needed = MagicMock()

        ams._on_stuck_spool_detected(fps_name="fps1")

        ams._on_stuck_spool_recovery_needed.assert_called_once_with("fps1", "lane1")
        afc.error.AFC_error.assert_not_called()

    def test_auto_recovery_enabled_but_no_lane_falls_back_to_pause(self):
        ams, afc, printer, reactor = _make_ams()
        ams._monitor = None
        ams.stuck_spool_auto_recovery = True

        ams._on_stuck_spool_detected(fps_name="fps1")

        afc.error.AFC_error.assert_called_once()

    def test_message_includes_fps_name(self):
        ams, afc, printer, reactor = _make_ams()
        ams.stuck_spool_auto_recovery = False
        ams._on_stuck_spool_detected(fps_name="fps1", message="Custom stuck message")
        msg = afc.error.AFC_error.call_args[0][0]
        assert "fps1" in msg

    def test_no_fps_name_skips_fps_suffix(self):
        ams, afc, printer, reactor = _make_ams()
        ams.stuck_spool_auto_recovery = False
        ams._on_stuck_spool_detected(fps_name=None, message="Custom stuck message")
        msg = afc.error.AFC_error.call_args[0][0]
        assert "FPS:" not in msg

    def test_message_already_mentioning_paused_is_not_duplicated(self):
        ams, afc, printer, reactor = _make_ams()
        ams.stuck_spool_auto_recovery = False
        ams._on_stuck_spool_detected(
            fps_name=None, message="Already paused for stuck spool")
        msg = afc.error.AFC_error.call_args[0][0]
        assert msg.count("paused") == 1

    def test_led_set_failure_is_logged_as_debug(self):
        ams, afc, printer, reactor = _make_ams()
        monitor = MagicMock()
        monitor.state.current_lane = "lane1"
        monitor.state.current_spool_idx = 2
        ams._monitor = monitor
        follower = MagicMock()
        follower.set_led_error_if_changed.side_effect = Exception("mcu offline")
        ams._follower = follower
        ams.stuck_spool_auto_recovery = False

        ams._on_stuck_spool_detected(fps_name="fps1")  # must not raise

        assert ("debug", "stuck LED set failed: mcu offline") in ams.logger.messages


class TestOnClogDetected:
    def test_pauses_print(self):
        ams, afc, printer, reactor = _make_ams()
        ams._on_clog_detected(fps_name="fps1")
        afc.error.AFC_error.assert_called_once()
        args, kwargs = afc.error.AFC_error.call_args
        assert "clog" in args[0].lower()
        assert kwargs["pause"] is True

    def test_uses_default_message_when_none_given(self):
        ams, afc, printer, reactor = _make_ams()
        ams._on_clog_detected()
        args, kwargs = afc.error.AFC_error.call_args
        assert "OpenAMS clog detected" in args[0]

    def test_message_already_mentioning_paused_is_not_duplicated(self):
        ams, afc, printer, reactor = _make_ams()
        ams._on_clog_detected(message="Print already paused for a clog")
        msg = afc.error.AFC_error.call_args[0][0]
        assert msg.count("paused") == 1


class TestOnStuckSpoolCleared:
    def test_logs_info_with_fps_name(self):
        ams, afc, printer, reactor = _make_ams()
        ams._on_stuck_spool_cleared(fps_name="fps1")
        assert ("info", "Stuck spool cleared on fps1") in ams.logger.messages

    def test_logs_info_without_fps_name(self):
        ams, afc, printer, reactor = _make_ams()
        ams._on_stuck_spool_cleared()
        assert ("info", "Stuck spool cleared") in ams.logger.messages


class TestOnStuckSpoolRecoveryNeeded:
    def test_schedules_recovery_gcode(self):
        ams, afc, printer, reactor = _make_ams()
        ams.gcode = MagicMock()
        ams._on_stuck_spool_recovery_needed("fps1", "lane1")
        ams.gcode.run_script_from_command.assert_called_once_with(
            "_AFC_OAMS_STUCK_RECOVERY LANE=lane1 FPS=fps1")
        assert (
            "info", "Stuck spool recovery scheduled: fps=fps1, lane=lane1"
        ) in ams.logger.messages

    def test_none_fps_name_uses_empty_string(self):
        ams, afc, printer, reactor = _make_ams()
        ams.gcode = MagicMock()
        ams._on_stuck_spool_recovery_needed(None, "lane1")
        ams.gcode.run_script_from_command.assert_called_once_with(
            "_AFC_OAMS_STUCK_RECOVERY LANE=lane1 FPS=")

    def test_schedule_failure_falls_back_to_pause(self):
        ams, afc, printer, reactor = _make_ams()
        ams.gcode = MagicMock()
        ams.gcode.run_script_from_command.side_effect = [Exception("boom"), None]
        ams._on_stuck_spool_recovery_needed("fps1", "lane1")
        assert ams.gcode.run_script_from_command.call_args_list[1][0][0] == "PAUSE"
        assert (
            "error", "Failed to schedule stuck spool recovery for lane1: boom"
        ) in ams.logger.messages

    def test_pause_fallback_also_failing_is_logged(self):
        ams, afc, printer, reactor = _make_ams()
        ams.gcode = MagicMock()
        ams.gcode.run_script_from_command.side_effect = Exception("boom")
        ams._on_stuck_spool_recovery_needed("fps1", "lane1")  # must not raise
        error_msgs = [m for lvl, m in ams.logger.messages if lvl == "error"]
        assert len(error_msgs) == 2


class TestStuckSpoolRecoveryFallback:
    def test_raises_afc_error_with_pause(self):
        ams, afc, printer, reactor = _make_ams()
        ams._stuck_spool_recovery_fallback("fps1", "lane1", "some reason")
        afc.error.AFC_error.assert_called_once()
        args, kwargs = afc.error.AFC_error.call_args
        assert "some reason" in args[0]
        assert kwargs["pause"] is True
        assert any(
            lvl == "error" and "Stuck spool auto-recovery failed for lane1" in m
            for lvl, m in ams.logger.messages)

    def test_afc_error_failure_falls_back_to_gcode_pause(self):
        ams, afc, printer, reactor = _make_ams()
        afc.error.AFC_error.side_effect = Exception("no error handler")
        ams.gcode = MagicMock()
        ams._stuck_spool_recovery_fallback("fps1", "lane1", "reason")
        ams.gcode.run_script_from_command.assert_called_once_with("PAUSE")
        assert (
            "error",
            "Failed to raise AFC error for stuck fallback: no error handler",
        ) in ams.logger.messages

    def test_gcode_pause_failure_is_swallowed(self):
        ams, afc, printer, reactor = _make_ams()
        afc.error.AFC_error.side_effect = Exception("no error handler")
        ams.gcode = MagicMock()
        ams.gcode.run_script_from_command.side_effect = Exception("gcode dead")
        ams._stuck_spool_recovery_fallback("fps1", "lane1", "reason")  # must not raise

    def test_uses_fps_name_when_lane_name_missing(self):
        ams, afc, printer, reactor = _make_ams()
        ams._stuck_spool_recovery_fallback("fps1", None, "reason")
        msg = afc.error.AFC_error.call_args[0][0]
        assert "fps1" in msg


class TestStuckSpoolRecoveryClearOamsState:
    def test_no_monitor_state_is_noop(self):
        ams, afc, printer, reactor = _make_ams()
        ams._get_monitor_state = MagicMock(return_value=None)
        ams._stuck_spool_recovery_clear_oams_state("fps1", "lane1")  # must not raise

    def test_clears_led_when_spool_idx_and_follower_present(self):
        ams, afc, printer, reactor = _make_ams()
        state = MagicMock()
        state.current_spool_idx = 2
        ams._get_monitor_state = MagicMock(return_value=state)
        follower = MagicMock()
        ams._follower = follower

        ams._stuck_spool_recovery_clear_oams_state("fps1", "lane1")

        follower.clear_error_led.assert_called_once()
        assert state.stuck_active is False
        assert state.stuck_start_time is None

    def test_no_spool_idx_skips_led_clear(self):
        ams, afc, printer, reactor = _make_ams()
        state = MagicMock()
        state.current_spool_idx = None
        ams._get_monitor_state = MagicMock(return_value=state)
        follower = MagicMock()
        ams._follower = follower

        ams._stuck_spool_recovery_clear_oams_state("fps1", "lane1")

        follower.clear_error_led.assert_not_called()
        assert state.stuck_active is False

    def test_no_follower_skips_led_clear(self):
        ams, afc, printer, reactor = _make_ams()
        state = MagicMock()
        state.current_spool_idx = 2
        ams._get_monitor_state = MagicMock(return_value=state)
        ams._follower = None

        ams._stuck_spool_recovery_clear_oams_state("fps1", "lane1")  # must not raise
        assert state.stuck_active is False

    def test_clear_error_led_failure_is_logged_as_debug(self):
        ams, afc, printer, reactor = _make_ams()
        state = MagicMock()
        state.current_spool_idx = 2
        ams._get_monitor_state = MagicMock(return_value=state)
        follower = MagicMock()
        follower.clear_error_led.side_effect = Exception("boom")
        ams._follower = follower

        ams._stuck_spool_recovery_clear_oams_state("fps1", "lane1")  # must not raise

        assert ("debug", "clear_error_led failed: boom") in ams.logger.messages

    def test_get_monitor_state_exception_is_logged_as_warning(self):
        ams, afc, printer, reactor = _make_ams()
        ams._get_monitor_state = MagicMock(side_effect=Exception("boom"))
        ams._stuck_spool_recovery_clear_oams_state("fps1", "lane1")  # must not raise
        assert (
            "warning", "Failed to clear stuck spool state: boom"
        ) in ams.logger.messages


class TestCmdStuckSpoolRecovery:
    def _gcmd(self, lane="lane1", fps="fps1"):
        gcmd = MagicMock()
        gcmd.get = MagicMock(side_effect=lambda k, default=None: {"LANE": lane, "FPS": fps}.get(k, default))
        return gcmd

    def test_lane_not_found_falls_back(self):
        ams, afc, printer, reactor = _make_ams()
        afc.lanes = {}
        ams._stuck_spool_recovery_fallback = MagicMock()
        ams._cmd_stuck_spool_recovery(self._gcmd())
        ams._stuck_spool_recovery_fallback.assert_called_once_with(
            "fps1", "lane1", "lane not found")
        assert (
            "error", "Stuck spool recovery: lane 'lane1' not found, pausing"
        ) in ams.logger.messages

    def test_none_lane_name_falls_back(self):
        ams, afc, printer, reactor = _make_ams()
        ams._stuck_spool_recovery_fallback = MagicMock()
        ams._cmd_stuck_spool_recovery(self._gcmd(lane=None))
        ams._stuck_spool_recovery_fallback.assert_called_once()

    def test_success_path_resumes_print(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        afc.lanes = {"lane1": lane}
        afc.gcode_move.last_position = [0.0, 0.0, 5.0, 0.0]
        afc.function.is_paused.return_value = False
        afc.TOOL_UNLOAD = MagicMock(return_value=True)
        afc.TOOL_LOAD = MagicMock(return_value=True)
        ams._stuck_spool_recovery_clear_oams_state = MagicMock()
        ams.gcode = MagicMock()

        ams._cmd_stuck_spool_recovery(self._gcmd())

        afc.TOOL_UNLOAD.assert_called_once_with(lane, set_start_time=True)
        afc.TOOL_LOAD.assert_called_once_with(lane, set_start_time=True)
        ams._stuck_spool_recovery_clear_oams_state.assert_called_once()
        ams.gcode.run_script_from_command.assert_any_call("AFC_RESUME")
        afc.save_pos.assert_called_once()
        afc.move_z_pos.assert_called_once_with(5.5, "stuck_spool_recovery_zhop")
        afc.error.reset_failure.assert_called_once()
        assert (
            "info",
            "Stuck spool auto-recovery starting: unload then reload for lane1",
        ) in ams.logger.messages
        assert (
            "info", "Stuck spool recovery: unloading lane1"
        ) in ams.logger.messages
        assert (
            "info", "Stuck spool recovery: reloading lane1"
        ) in ams.logger.messages
        assert (
            "info",
            "Stuck spool auto-recovery SUCCEEDED for lane1, resuming print",
        ) in ams.logger.messages

    def test_already_paused_skips_pause_resume_send(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        afc.lanes = {"lane1": lane}
        afc.gcode_move.last_position = [0.0, 0.0, 5.0, 0.0]
        afc.function.is_paused.return_value = True
        afc.TOOL_UNLOAD = MagicMock(return_value=True)
        afc.TOOL_LOAD = MagicMock(return_value=True)
        pause_resume = MagicMock()
        printer._objects["pause_resume"] = pause_resume
        ams._stuck_spool_recovery_clear_oams_state = MagicMock()
        ams.gcode = MagicMock()

        ams._cmd_stuck_spool_recovery(self._gcmd())

        pause_resume.send_pause_command.assert_not_called()

    def test_not_paused_sends_pause_command(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        afc.lanes = {"lane1": lane}
        afc.gcode_move.last_position = [0.0, 0.0, 5.0, 0.0]
        afc.function.is_paused.return_value = False
        afc.TOOL_UNLOAD = MagicMock(return_value=True)
        afc.TOOL_LOAD = MagicMock(return_value=True)
        pause_resume = MagicMock()
        printer._objects["pause_resume"] = pause_resume
        ams._stuck_spool_recovery_clear_oams_state = MagicMock()
        ams.gcode = MagicMock()

        ams._cmd_stuck_spool_recovery(self._gcmd())

        pause_resume.send_pause_command.assert_called_once()

    def test_zhop_failure_is_logged_as_warning_and_continues(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        afc.lanes = {"lane1": lane}
        afc.function.is_paused.return_value = True
        type(afc.gcode_move).last_position = property(
            lambda self: (_ for _ in ()).throw(Exception("no position")))
        afc.TOOL_UNLOAD = MagicMock(return_value=True)
        afc.TOOL_LOAD = MagicMock(return_value=True)
        ams._stuck_spool_recovery_clear_oams_state = MagicMock()
        ams.gcode = MagicMock()

        ams._cmd_stuck_spool_recovery(self._gcmd())

        assert (
            "warning", "Stuck spool recovery: Z-hop failed: no position"
        ) in ams.logger.messages
        afc.TOOL_UNLOAD.assert_called_once()

    def test_tool_unload_returns_false_triggers_fallback(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        afc.lanes = {"lane1": lane}
        afc.gcode_move.last_position = [0.0, 0.0, 5.0, 0.0]
        afc.function.is_paused.return_value = True
        afc.TOOL_UNLOAD = MagicMock(return_value=False)
        ams._stuck_spool_recovery_fallback = MagicMock()

        ams._cmd_stuck_spool_recovery(self._gcmd())

        ams._stuck_spool_recovery_fallback.assert_called_once()
        assert "lane1" in ams._stuck_spool_recovery_fallback.call_args[0][1]

    def test_tool_load_returns_false_triggers_fallback(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        afc.lanes = {"lane1": lane}
        afc.gcode_move.last_position = [0.0, 0.0, 5.0, 0.0]
        afc.function.is_paused.return_value = True
        afc.TOOL_UNLOAD = MagicMock(return_value=True)
        afc.TOOL_LOAD = MagicMock(return_value=False)
        ams._stuck_spool_recovery_fallback = MagicMock()

        ams._cmd_stuck_spool_recovery(self._gcmd())

        ams._stuck_spool_recovery_fallback.assert_called_once()

    def test_hub_still_active_after_unload_polls_until_clear(self):
        ams, afc, printer, reactor = _make_ams()
        hub = MagicMock()
        hub.state = True
        lane = _make_lane("lane1", hub_obj=hub)
        afc.lanes = {"lane1": lane}
        afc.gcode_move.last_position = [0.0, 0.0, 5.0, 0.0]
        afc.function.is_paused.return_value = True
        afc.TOOL_UNLOAD = MagicMock(return_value=True)
        afc.TOOL_LOAD = MagicMock(return_value=True)
        ams._stuck_spool_recovery_clear_oams_state = MagicMock()
        ams.gcode = MagicMock()

        pause_calls = {"n": 0}

        def pause(t):
            pause_calls["n"] += 1
            hub.state = False  # clears on the first poll
        reactor.pause = pause

        ams._cmd_stuck_spool_recovery(self._gcmd())

        afc.TOOL_LOAD.assert_called_once()
        assert pause_calls["n"] == 1

    def test_hub_never_clears_logs_warning_but_proceeds(self):
        ams, afc, printer, reactor = _make_ams()
        hub = MagicMock()
        hub.state = True  # never clears
        lane = _make_lane("lane1", hub_obj=hub)
        afc.lanes = {"lane1": lane}
        afc.gcode_move.last_position = [0.0, 0.0, 5.0, 0.0]
        afc.function.is_paused.return_value = True
        afc.TOOL_UNLOAD = MagicMock(return_value=True)
        afc.TOOL_LOAD = MagicMock(return_value=True)
        ams._stuck_spool_recovery_clear_oams_state = MagicMock()
        ams.gcode = MagicMock()
        reactor.pause = MagicMock()  # no-op, hub.state never changes

        ams._cmd_stuck_spool_recovery(self._gcmd())

        assert any(
            lvl == "warning" and "hub still active" in m
            for lvl, m in ams.logger.messages)
        afc.TOOL_LOAD.assert_called_once()

    def test_no_hub_obj_skips_polling(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", hub_obj=None)
        afc.lanes = {"lane1": lane}
        afc.gcode_move.last_position = [0.0, 0.0, 5.0, 0.0]
        afc.function.is_paused.return_value = True
        afc.TOOL_UNLOAD = MagicMock(return_value=True)
        afc.TOOL_LOAD = MagicMock(return_value=True)
        ams._stuck_spool_recovery_clear_oams_state = MagicMock()
        ams.gcode = MagicMock()
        reactor.pause = MagicMock()

        ams._cmd_stuck_spool_recovery(self._gcmd())

        reactor.pause.assert_not_called()
        afc.TOOL_LOAD.assert_called_once()

    def test_unexpected_exception_during_unload_triggers_fallback(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        afc.lanes = {"lane1": lane}
        afc.gcode_move.last_position = [0.0, 0.0, 5.0, 0.0]
        afc.function.is_paused.return_value = True
        afc.TOOL_UNLOAD = MagicMock(side_effect=Exception("mcu fault"))
        ams._stuck_spool_recovery_fallback = MagicMock()

        ams._cmd_stuck_spool_recovery(self._gcmd())

        ams._stuck_spool_recovery_fallback.assert_called_once()
        assert "mcu fault" in ams._stuck_spool_recovery_fallback.call_args[0][2]
        assert (
            "error", "Stuck spool auto-recovery FAILED for lane1: mcu fault"
        ) in ams.logger.messages

    def test_afc_resume_failure_is_logged(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        afc.lanes = {"lane1": lane}
        afc.gcode_move.last_position = [0.0, 0.0, 5.0, 0.0]
        afc.function.is_paused.return_value = True
        afc.TOOL_UNLOAD = MagicMock(return_value=True)
        afc.TOOL_LOAD = MagicMock(return_value=True)
        ams._stuck_spool_recovery_clear_oams_state = MagicMock()
        ams.gcode = MagicMock()
        ams.gcode.run_script_from_command.side_effect = Exception("resume failed")

        ams._cmd_stuck_spool_recovery(self._gcmd())  # must not raise

        assert (
            "error", "Stuck spool recovery: AFC_RESUME failed: resume failed"
        ) in ams.logger.messages

    def test_forces_is_paused_true_before_resume_when_not_paused(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        afc.lanes = {"lane1": lane}
        afc.gcode_move.last_position = [0.0, 0.0, 5.0, 0.0]
        afc.function.is_paused.return_value = False
        afc.TOOL_UNLOAD = MagicMock(return_value=True)
        afc.TOOL_LOAD = MagicMock(return_value=True)
        ams._stuck_spool_recovery_clear_oams_state = MagicMock()
        ams.gcode = MagicMock()
        pause_resume = MagicMock()
        printer._objects["pause_resume"] = pause_resume

        ams._cmd_stuck_spool_recovery(self._gcmd())

        assert pause_resume.is_paused is True

    def test_no_pause_resume_object_before_resume_is_safe(self):
        """MockPrinter.lookup_object("pause_resume") always returns a
        MagicMock by default; force a genuine None here to exercise the
        real-world case where the pause_resume module isn't loaded."""
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1")
        afc.lanes = {"lane1": lane}
        afc.gcode_move.last_position = [0.0, 0.0, 5.0, 0.0]
        afc.function.is_paused.return_value = False
        afc.TOOL_UNLOAD = MagicMock(return_value=True)
        afc.TOOL_LOAD = MagicMock(return_value=True)
        ams._stuck_spool_recovery_clear_oams_state = MagicMock()
        ams.gcode = MagicMock()

        real_lookup = printer.lookup_object

        def lookup_object(name, default=None):
            if name == "pause_resume":
                return None
            return real_lookup(name, default)
        printer.lookup_object = lookup_object

        ams._cmd_stuck_spool_recovery(self._gcmd())  # must not raise
        ams.gcode.run_script_from_command.assert_any_call("AFC_RESUME")


# ── cmd_AFC_OAMS_* gcode commands ─────────────────────────────────────────

class TestCmdCalibratePtfe:
    def test_no_oams_responds_unavailable(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CALIBRATE_PTFE(gcmd)
        gcmd.respond_info.assert_called_once_with("OAMS hardware not available")

    def test_success_runs_gcode_and_responds(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._get_oams_index = MagicMock(return_value=1)
        gcmd = _make_gcmd({"SPOOL": 2})
        ams.cmd_AFC_OAMS_CALIBRATE_PTFE(gcmd)
        afc.gcode.run_script_from_command.assert_called_once_with(
            "OAMS_CALIBRATE_PTFE_LENGTH OAMS=1 SPOOL=2")
        gcmd.respond_info.assert_called_once()

    def test_failure_reports_error_message(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.gcode.run_script_from_command.side_effect = Exception("boom")
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CALIBRATE_PTFE(gcmd)
        msg = gcmd.respond_info.call_args[0][0]
        assert "failed" in msg


class TestCmdCalibrateHubHes:
    def test_no_oams_responds_unavailable(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CALIBRATE_HUB_HES(gcmd)
        gcmd.respond_info.assert_called_once_with("OAMS hardware not available")

    def test_missing_spool_shows_usage(self):
        ams, afc, printer, reactor = _make_ams(oams=MagicMock())
        gcmd = _make_gcmd({})
        ams.cmd_AFC_OAMS_CALIBRATE_HUB_HES(gcmd)
        msg = gcmd.respond_info.call_args[0][0]
        assert "Usage" in msg

    def test_success_reports_successful(self):
        ams, afc, printer, reactor = _make_ams(oams=MagicMock())
        ams._calibrate_hub_hes_spool = MagicMock(return_value=True)
        gcmd = _make_gcmd({"SPOOL": 1})
        ams.cmd_AFC_OAMS_CALIBRATE_HUB_HES(gcmd)
        msg = gcmd.respond_info.call_args[0][0]
        assert "successful" in msg

    def test_failure_reports_failed(self):
        ams, afc, printer, reactor = _make_ams(oams=MagicMock())
        ams._calibrate_hub_hes_spool = MagicMock(return_value=False)
        gcmd = _make_gcmd({"SPOOL": 1})
        ams.cmd_AFC_OAMS_CALIBRATE_HUB_HES(gcmd)
        msg = gcmd.respond_info.call_args[0][0]
        assert "failed" in msg


class TestCmdCalibrateHubHesAll:
    def test_no_oams_responds_unavailable(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CALIBRATE_HUB_HES_ALL(gcmd)
        gcmd.respond_info.assert_called_once_with("OAMS hardware not available")

    def test_only_loaded_lanes_calibrated(self):
        loaded_lane = _make_lane("lane1", load_state=True)
        unloaded_lane = _make_lane("lane2", load_state=False)
        ams, afc, printer, reactor = _make_ams(
            oams=MagicMock(), lanes={"lane1": loaded_lane, "lane2": unloaded_lane})
        ams._calibrate_hub_hes_spool = MagicMock(return_value=True)

        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CALIBRATE_HUB_HES_ALL(gcmd)

        ams._calibrate_hub_hes_spool.assert_called_once()
        msg = gcmd.respond_info.call_args[0][0]
        assert "Calibrated 1" in msg

    def test_counts_only_successes(self):
        lane1 = _make_lane("lane1", load_state=True)
        lane2 = _make_lane("lane2", load_state=True)
        ams, afc, printer, reactor = _make_ams(
            oams=MagicMock(), lanes={"lane1": lane1, "lane2": lane2})
        ams._calibrate_hub_hes_spool = MagicMock(side_effect=[True, False])

        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CALIBRATE_HUB_HES_ALL(gcmd)

        msg = gcmd.respond_info.call_args[0][0]
        assert "Calibrated 1" in msg


class TestCmdClearErrors:
    def test_no_oams_responds_unavailable(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CLEAR_ERRORS(gcmd)
        gcmd.respond_info.assert_called_once_with("OAMS hardware not available")

    def test_stops_monitor_before_clearing(self):
        oams = MagicMock()
        monitor = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._monitor = monitor
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CLEAR_ERRORS(gcmd)
        monitor.stop.assert_called_once()

    def test_clears_hardware_errors(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.reactor.pause = MagicMock()
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CLEAR_ERRORS(gcmd)
        oams.abort_current_action.assert_called_once()
        oams.clear_errors.assert_called_once()
        assert oams.current_spool is None
        afc.reactor.pause.assert_called_once()

    def test_hardware_clear_failure_logged(self):
        oams = MagicMock()
        oams.abort_current_action.side_effect = Exception("boom")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CLEAR_ERRORS(gcmd)  # must not raise
        assert ("error", "Error clearing OAMS errors: boom") in ams.logger.messages

    def test_unsyncs_tool_loaded_lanes(self):
        oams = MagicMock()
        lane = _make_lane("lane1", tool_loaded=True)
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CLEAR_ERRORS(gcmd)
        lane.unsync_to_extruder.assert_called_once()
        lane.set_tool_unloaded.assert_called_once()

    def test_lane_clear_failure_logged_as_warning(self):
        oams = MagicMock()
        lane = _make_lane("lane1", tool_loaded=True)
        lane.unsync_to_extruder.side_effect = Exception("boom")
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CLEAR_ERRORS(gcmd)  # must not raise
        assert (
            "warning", "Failed to clear lane_loaded for lane1: boom"
        ) in ams.logger.messages

    def test_restores_leds_tool_loaded_branch(self):
        oams = MagicMock()
        lane = _make_lane("lane1", tool_loaded=True)
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams.lane_tool_loaded = MagicMock()
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CLEAR_ERRORS(gcmd)
        ams.lane_tool_loaded.assert_called_once_with(lane)

    def test_restores_leds_loaded_branch(self):
        oams = MagicMock()
        lane = _make_lane("lane1", tool_loaded=False, load_state=True)
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams.lane_loaded = MagicMock()
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CLEAR_ERRORS(gcmd)
        ams.lane_loaded.assert_called_once_with(lane)

    def test_restores_leds_unloaded_branch(self):
        oams = MagicMock()
        lane = _make_lane("lane1", tool_loaded=False, load_state=False)
        ams, afc, printer, reactor = _make_ams(oams=oams, lanes={"lane1": lane})
        ams.lane_unloaded = MagicMock()
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CLEAR_ERRORS(gcmd)
        ams.lane_unloaded.assert_called_once_with(lane)

    def test_led_restore_failure_for_one_lane_does_not_stop_others(self):
        oams = MagicMock()
        bad_lane = _make_lane("lane1", tool_loaded=False, load_state=False)
        good_lane = _make_lane("lane2", tool_loaded=False, load_state=False)
        ams, afc, printer, reactor = _make_ams(
            oams=oams, lanes={"lane1": bad_lane, "lane2": good_lane})
        ams.lane_unloaded = MagicMock(side_effect=[Exception("led error"), None])
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CLEAR_ERRORS(gcmd)  # must not raise
        assert ams.lane_unloaded.call_count == 2

    def test_restarts_monitor_after_clear(self):
        oams = MagicMock()
        monitor = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._monitor = monitor
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CLEAR_ERRORS(gcmd)
        monitor.state.reset.assert_called_once()
        monitor.start.assert_called_once_with(oams)

    def test_responds_with_completion_message(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        gcmd = _make_gcmd()
        ams.cmd_AFC_OAMS_CLEAR_ERRORS(gcmd)
        gcmd.respond_info.assert_called_once_with(
            "OpenAMS errors cleared and state resynced")


# ── on_filament_insert / on_filament_remove / _clear_oams_state_for_bay ────

class TestOnFilamentInsert:
    def test_sets_loaded_to_hub_and_illuminates(self):
        ams, afc, printer, reactor = _make_ams()
        ams.lane_loaded = MagicMock()
        ams.lane_illuminate_spool = MagicMock()
        lane = _make_lane("lane1")
        with patch.object(afcUnit, "on_filament_insert", MagicMock()):
            ams.on_filament_insert(lane)
        assert lane.loaded_to_hub is True
        ams.lane_loaded.assert_called_once_with(lane)
        ams.lane_illuminate_spool.assert_called_once_with(lane)

    def test_updates_hardware_service_snapshot_when_mapped(self):
        ams, afc, printer, reactor = _make_ams()
        ams.lane_loaded = MagicMock()
        ams.lane_illuminate_spool = MagicMock()
        ams._spool_map["lane1"] = 0
        lane = _make_lane("lane1")
        with patch.object(afcUnit, "on_filament_insert", MagicMock()), \
             patch("extras.AFC_OpenAMS.AMSHardwareService.for_printer") as mock_for_printer:
            hw = MagicMock()
            mock_for_printer.return_value = hw
            ams.on_filament_insert(lane)
        hw.update_lane_snapshot.assert_called_once()

    def test_no_snapshot_update_when_unmapped(self):
        ams, afc, printer, reactor = _make_ams()
        ams.lane_loaded = MagicMock()
        ams.lane_illuminate_spool = MagicMock()
        lane = _make_lane("unmapped")
        with patch.object(afcUnit, "on_filament_insert", MagicMock()), \
             patch("extras.AFC_OpenAMS.AMSHardwareService.for_printer") as mock_for_printer:
            ams.on_filament_insert(lane)
        mock_for_printer.assert_not_called()

    def test_calls_super(self):
        ams, afc, printer, reactor = _make_ams()
        ams.lane_loaded = MagicMock()
        ams.lane_illuminate_spool = MagicMock()
        lane = _make_lane("lane1")
        super_call = MagicMock()
        with patch.object(afcUnit, "on_filament_insert", super_call):
            ams.on_filament_insert(lane)
        super_call.assert_called_once_with(lane)


class TestOnFilamentRemove:
    def test_clears_loaded_to_hub_and_unloads(self):
        ams, afc, printer, reactor = _make_ams()
        ams.lane_unloaded = MagicMock()
        ams._clear_lane_info = MagicMock()
        ams._clear_oams_state_for_bay = MagicMock()
        lane = _make_lane("lane1", loaded_to_hub=True, tool_loaded=False)
        ams.on_filament_remove(lane)
        assert lane.loaded_to_hub is False
        ams.lane_unloaded.assert_called_once_with(lane)

    def test_cancels_pending_spool_loaded_timer(self):
        ams, afc, printer, reactor = _make_ams()
        ams.lane_unloaded = MagicMock()
        ams._clear_lane_info = MagicMock()
        ams._clear_oams_state_for_bay = MagicMock()
        timer = MagicMock()
        reactor.unregister_timer = MagicMock()
        ams._pending_spool_loaded_timers["lane1"] = timer
        lane = _make_lane("lane1", tool_loaded=False)
        ams.on_filament_remove(lane)
        reactor.unregister_timer.assert_called_once_with(timer)
        assert "lane1" not in ams._pending_spool_loaded_timers

    def test_timer_unregister_failure_is_swallowed(self):
        ams, afc, printer, reactor = _make_ams()
        ams.lane_unloaded = MagicMock()
        ams._clear_lane_info = MagicMock()
        ams._clear_oams_state_for_bay = MagicMock()
        reactor.unregister_timer = MagicMock(side_effect=Exception("boom"))
        ams._pending_spool_loaded_timers["lane1"] = MagicMock()
        lane = _make_lane("lane1", tool_loaded=False)
        ams.on_filament_remove(lane)  # must not raise
        assert "lane1" not in ams._pending_spool_loaded_timers

    def test_clears_lane_info_when_not_tool_loaded(self):
        ams, afc, printer, reactor = _make_ams()
        ams.lane_unloaded = MagicMock()
        ams._clear_lane_info = MagicMock()
        ams._clear_oams_state_for_bay = MagicMock()
        lane = _make_lane("lane1", tool_loaded=False)
        ams.on_filament_remove(lane)
        ams._clear_lane_info.assert_called_once_with(lane)
        ams._clear_oams_state_for_bay.assert_called_once()

    def test_skips_clear_lane_info_when_tool_loaded_runout(self):
        ams, afc, printer, reactor = _make_ams()
        ams.lane_unloaded = MagicMock()
        ams._clear_lane_info = MagicMock()
        ams._clear_oams_state_for_bay = MagicMock()
        lane = _make_lane("lane1", tool_loaded=True)
        ams.on_filament_remove(lane)
        ams._clear_lane_info.assert_not_called()
        ams._clear_oams_state_for_bay.assert_not_called()

    def test_updates_hardware_snapshot_when_mapped(self):
        ams, afc, printer, reactor = _make_ams()
        ams.lane_unloaded = MagicMock()
        ams._clear_lane_info = MagicMock()
        ams._clear_oams_state_for_bay = MagicMock()
        ams._spool_map["lane1"] = 0
        lane = _make_lane("lane1", tool_loaded=False)
        with patch("extras.AFC_OpenAMS.AMSHardwareService.for_printer") as mock_for_printer:
            hw = MagicMock()
            mock_for_printer.return_value = hw
            ams.on_filament_remove(lane)
        hw.update_lane_snapshot.assert_called_once()


class TestClearOamsStateForBay:
    def test_none_spool_index_is_noop(self):
        ams, afc, printer, reactor = _make_ams(oams=MagicMock())
        ams._clear_oams_state_for_bay(None, _make_lane("lane1"))  # must not raise

    def test_no_oams_is_noop(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        ams._clear_oams_state_for_bay(0, _make_lane("lane1"))  # must not raise

    def test_clears_current_spool_when_matching(self):
        oams = MagicMock()
        oams.current_spool = 2
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._clear_oams_state_for_bay(2, _make_lane("lane1"))
        assert oams.current_spool is None

    def test_does_not_clear_current_spool_when_not_matching(self):
        oams = MagicMock()
        oams.current_spool = 3
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._clear_oams_state_for_bay(2, _make_lane("lane1"))
        assert oams.current_spool == 3

    def test_stops_follower_when_bay_was_current(self):
        oams = MagicMock()
        oams.current_spool = 2
        follower = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._follower = follower
        ams._clear_oams_state_for_bay(2, _make_lane("lane1"))
        follower.set_follower_state.assert_called_once()

    def test_follower_exception_is_swallowed(self):
        oams = MagicMock()
        oams.current_spool = 2
        follower = MagicMock()
        follower.set_follower_state.side_effect = Exception("boom")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._follower = follower
        ams._clear_oams_state_for_bay(2, _make_lane("lane1"))  # must not raise

    def test_resets_monitor_when_tracking_bay(self):
        oams = MagicMock()
        oams.current_spool = 2
        monitor = MagicMock()
        monitor.state.current_spool_idx = 2
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._monitor = monitor
        ams._get_monitor_state = MagicMock(return_value=monitor.state)

        ams._clear_oams_state_for_bay(2, _make_lane("lane1"))

        monitor.notify_unload_complete.assert_called_once()
        monitor.stop.assert_called_once()

    def test_monitor_not_reset_when_tracking_different_bay(self):
        oams = MagicMock()
        oams.current_spool = 2
        monitor = MagicMock()
        monitor.state.current_spool_idx = 5
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._monitor = monitor
        ams._get_monitor_state = MagicMock(return_value=monitor.state)

        ams._clear_oams_state_for_bay(2, _make_lane("lane1"))

        monitor.notify_unload_complete.assert_not_called()

    def test_monitor_stop_exception_is_swallowed(self):
        oams = MagicMock()
        oams.current_spool = 2
        monitor = MagicMock()
        monitor.state.current_spool_idx = 2
        monitor.stop.side_effect = Exception("boom")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._monitor = monitor
        ams._get_monitor_state = MagicMock(return_value=monitor.state)

        ams._clear_oams_state_for_bay(2, _make_lane("lane1"))  # must not raise


class TestClearLaneInfo:
    def test_clears_material_and_color(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", material="PLA", color="#FF0000", spool_id=None)
        ams._clear_lane_info(lane)
        assert lane.material == ""
        assert lane.color == ""

    def test_no_spool_id_skips_spoolman_clear(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", spool_id=None)
        ams._clear_lane_info(lane)
        afc.spool.set_spoolID.assert_not_called()

    def test_zero_spool_id_skips_spoolman_clear(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", spool_id=0)
        ams._clear_lane_info(lane)
        afc.spool.set_spoolID.assert_not_called()

    def test_real_spool_id_clears_spoolman_record(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", spool_id=42)
        ams._clear_lane_info(lane)
        afc.spool.set_spoolID.assert_called_once_with(lane, "")

    def test_spoolman_clear_failure_logged_as_warning(self):
        ams, afc, printer, reactor = _make_ams()
        afc.spool.set_spoolID.side_effect = Exception("moonraker offline")
        lane = _make_lane("lane1", spool_id=42)
        ams._clear_lane_info(lane)  # must not raise
        assert (
            "warning", "OAMS: failed to clear spool_id on lane1: moonraker offline"
        ) in ams.logger.messages

    def test_calls_send_lane_data(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", spool_id=None)
        ams._clear_lane_info(lane)
        lane.send_lane_data.assert_called_once()

    def test_send_lane_data_failure_is_swallowed(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", spool_id=None)
        lane.send_lane_data.side_effect = Exception("boom")
        ams._clear_lane_info(lane)  # must not raise


class TestCancelAndMarkLoaded:
    def test_idle_immediately_sets_current_spool(self):
        oams = MagicMock()
        oams.action_status = None
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._cancel_and_mark_loaded(2, "lane1")
        oams.load_spool_cancel.assert_called_once()
        assert oams.current_spool == 2

    def test_waits_for_action_status_to_clear(self):
        oams = MagicMock()
        oams.action_status = OAMSStatus.LOADING
        ams, afc, printer, reactor = _make_ams(oams=oams)

        def pause(t):
            oams.action_status = None
        reactor.pause = pause

        ams._cancel_and_mark_loaded(1, "lane1")

        assert oams.current_spool == 1

    def test_timeout_forces_action_status_clear(self):
        oams = MagicMock()
        oams.action_status = OAMSStatus.LOADING
        ams, afc, printer, reactor = _make_ams(oams=oams)
        times = iter([0.0, 0.0, 6.0, 6.0])

        def monotonic():
            return next(times, 6.0)
        reactor.monotonic = monotonic
        reactor.pause = MagicMock()

        ams._cancel_and_mark_loaded(3, "lane1")

        assert oams.action_status is None
        assert (
            "warning", "Cancel response timeout - forcing action_status clear"
        ) in ams.logger.messages

    def test_updates_monitor_state_when_present(self):
        oams = MagicMock()
        oams.action_status = None
        ams, afc, printer, reactor = _make_ams(oams=oams)
        state = MagicMock()
        ams._get_monitor_state = MagicMock(return_value=state)

        ams._cancel_and_mark_loaded(2, "lane1")

        assert state.state == FPSLoadState.LOADED

    def test_no_monitor_state_is_safe(self):
        oams = MagicMock()
        oams.action_status = None
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._get_monitor_state = MagicMock(return_value=None)
        ams._cancel_and_mark_loaded(2, "lane1")  # must not raise

    def test_monitor_state_update_failure_is_swallowed(self):
        oams = MagicMock()
        oams.action_status = None
        ams, afc, printer, reactor = _make_ams(oams=oams)
        state = MagicMock()
        type(state).state = property(
            lambda self: None,
            lambda self, v: (_ for _ in ()).throw(Exception("boom")))
        ams._get_monitor_state = MagicMock(return_value=state)
        ams._cancel_and_mark_loaded(2, "lane1")  # must not raise


class TestClearLaneStateAfterTd1:
    def test_tool_loaded_unloads_tool(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", tool_loaded=True)
        ams._clear_lane_state_after_td1(lane)
        lane.set_tool_unloaded.assert_called_once()
        afc.save_vars.assert_called_once()

    def test_matching_extruder_lane_loaded_cleared(self):
        ams, afc, printer, reactor = _make_ams()
        ext = MagicMock()
        ext.lane_loaded = "lane1"
        lane = _make_lane("lane1", tool_loaded=False, extruder_obj=ext)
        ams._clear_lane_state_after_td1(lane)
        assert ext.lane_loaded is None

    def test_non_matching_extruder_lane_loaded_untouched(self):
        ams, afc, printer, reactor = _make_ams()
        ext = MagicMock()
        ext.lane_loaded = "other_lane"
        lane = _make_lane("lane1", tool_loaded=False, extruder_obj=ext)
        ams._clear_lane_state_after_td1(lane)
        assert ext.lane_loaded == "other_lane"

    def test_no_extruder_obj_is_safe(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", tool_loaded=False, extruder_obj=None)
        ams._clear_lane_state_after_td1(lane)  # must not raise
        afc.save_vars.assert_called_once()

    def test_exception_during_state_clear_is_swallowed(self):
        ams, afc, printer, reactor = _make_ams()
        lane = _make_lane("lane1", tool_loaded=True)
        lane.set_tool_unloaded.side_effect = Exception("boom")
        ams._clear_lane_state_after_td1(lane)  # must not raise
        afc.save_vars.assert_called_once()


class TestUnloadAfterTd1:
    def test_success_on_first_attempt(self):
        ams, afc, printer, reactor = _make_ams()
        oams = MagicMock()
        oams.unload_spool.return_value = (True, "unloaded")
        ams.oams = oams
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = _make_lane("lane1")

        ams._unload_after_td1(lane, 0)

        assert oams.unload_spool.call_count == 1
        oams.clear_errors.assert_called_once()
        ams._wait_for_idle.assert_called_once()
        assert (
            "info", "TD-1 unload completed for lane1"
        ) in ams.logger.messages

    def test_retries_up_to_three_times_then_gives_up(self):
        ams, afc, printer, reactor = _make_ams()
        oams = MagicMock()
        oams.unload_spool.return_value = (False, "busy")
        ams.oams = oams
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = _make_lane("lane1")

        ams._unload_after_td1(lane, 0)

        assert oams.unload_spool.call_count == 3

    def test_exception_during_attempt_is_logged_and_retried(self):
        ams, afc, printer, reactor = _make_ams()
        oams = MagicMock()
        oams.unload_spool.side_effect = [
            Exception("busy"), (True, "unloaded")]
        ams.oams = oams
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = _make_lane("lane1")

        ams._unload_after_td1(lane, 0)

        assert oams.unload_spool.call_count == 2
        assert (
            "debug", "TD-1 unload attempt 1 failed: busy"
        ) in ams.logger.messages

    def test_clear_errors_failure_is_swallowed(self):
        ams, afc, printer, reactor = _make_ams()
        oams = MagicMock()
        oams.unload_spool.return_value = (True, "unloaded")
        oams.clear_errors.side_effect = Exception("boom")
        ams.oams = oams
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = _make_lane("lane1")

        ams._unload_after_td1(lane, 0)  # must not raise

    def test_always_clears_lane_state_after_td1(self):
        ams, afc, printer, reactor = _make_ams()
        oams = MagicMock()
        oams.unload_spool.return_value = (False, "busy")
        ams.oams = oams
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._clear_lane_state_after_td1 = MagicMock()
        lane = _make_lane("lane1")

        ams._unload_after_td1(lane, 0)

        ams._clear_lane_state_after_td1.assert_called_once_with(lane)


class TestCancelAndCleanupTd1:
    def test_full_cleanup_sequence(self):
        ams, afc, printer, reactor = _make_ams()
        oams = MagicMock()
        ams.oams = oams
        ams._cancel_and_mark_loaded = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._clear_lane_state_after_td1 = MagicMock()
        lane = _make_lane("lane1")

        ams._cancel_and_cleanup_td1(lane, 1)

        ams._cancel_and_mark_loaded.assert_called_once_with(1, "lane1")
        oams.set_oams_follower.assert_called_once_with(0, 0)
        oams.unload_spool.assert_called_once()
        oams.clear_errors.assert_called_once()
        ams._clear_lane_state_after_td1.assert_called_once_with(lane)
        ams._wait_for_idle.assert_called_once()

    def test_each_step_failure_is_independently_swallowed(self):
        ams, afc, printer, reactor = _make_ams()
        oams = MagicMock()
        oams.set_oams_follower.side_effect = Exception("boom1")
        oams.unload_spool.side_effect = Exception("boom2")
        oams.clear_errors.side_effect = Exception("boom3")
        ams.oams = oams
        ams._cancel_and_mark_loaded = MagicMock(side_effect=Exception("boom0"))
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._clear_lane_state_after_td1 = MagicMock()
        lane = _make_lane("lane1")

        ams._cancel_and_cleanup_td1(lane, 1)  # must not raise

        ams._clear_lane_state_after_td1.assert_called_once_with(lane)


class TestGetTd1Snapshot:
    def test_no_moonraker_returns_none(self):
        ams, afc, printer, reactor = _make_ams()
        afc.moonraker = None
        lane = _make_lane("lane1", td1_device_id="td1_a")
        assert ams._get_td1_snapshot(lane) is None

    def test_no_moonraker_data_returns_none(self):
        ams, afc, printer, reactor = _make_ams()
        afc.moonraker.get_td1_data = MagicMock(return_value=None)
        lane = _make_lane("lane1", td1_device_id="td1_a")
        assert ams._get_td1_snapshot(lane) is None

    def test_moonraker_exception_returns_none(self):
        ams, afc, printer, reactor = _make_ams()
        afc.moonraker.get_td1_data = MagicMock(side_effect=Exception("boom"))
        lane = _make_lane("lane1", td1_device_id="td1_a")
        assert ams._get_td1_snapshot(lane) is None

    def test_device_id_missing_from_data_returns_none(self):
        ams, afc, printer, reactor = _make_ams()
        afc.moonraker.get_td1_data = MagicMock(return_value={"td1_b": {}})
        lane = _make_lane("lane1", td1_device_id="td1_a")
        assert ams._get_td1_snapshot(lane) is None

    def test_missing_scan_time_returns_none(self):
        ams, afc, printer, reactor = _make_ams()
        afc.moonraker.get_td1_data = MagicMock(
            return_value={"td1_a": {"td": 1.75, "color": "#FF0000"}})
        lane = _make_lane("lane1", td1_device_id="td1_a")
        assert ams._get_td1_snapshot(lane) is None

    def test_valid_data_returns_tuple(self):
        ams, afc, printer, reactor = _make_ams()
        afc.moonraker.get_td1_data = MagicMock(return_value={
            "td1_a": {"scan_time": "2024-01-01T00:00:00Z", "td": 1.75, "color": "#FF0000"}
        })
        lane = _make_lane("lane1", td1_device_id="td1_a")
        result = ams._get_td1_snapshot(lane)
        assert result == ("2024-01-01T00:00:00Z", 1.75, "#FF0000")


class TestInterpolateEncoderAtScan:
    def test_unparseable_timestamp_returns_fallback(self):
        ams, afc, printer, reactor = _make_ams()
        result = ams._interpolate_encoder_at_scan("not-a-timestamp", [], 42)
        assert result == 42

    def test_no_history_returns_fallback(self):
        ams, afc, printer, reactor = _make_ams()
        result = ams._interpolate_encoder_at_scan(
            "2024-01-01T00:00:00Z", [], 99)
        assert result == 99

    def test_picks_closest_sample(self):
        from datetime import datetime, timezone
        ams, afc, printer, reactor = _make_ams()
        t0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2024, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
        t2 = datetime(2024, 1, 1, 0, 0, 20, tzinfo=timezone.utc)
        history = [(t0, 100), (t1, 200), (t2, 300)]

        result = ams._interpolate_encoder_at_scan(
            "2024-01-01T00:00:04+00:00", history, 0)

        assert result == 200  # t1 (00:00:05) is only 1s away vs t0's 4s, t2's 16s

    def test_sample_with_unparseable_time_is_skipped(self):
        ams, afc, printer, reactor = _make_ams()
        bad_sample = ("not-a-datetime", 500)
        result = ams._interpolate_encoder_at_scan(
            "2024-01-01T00:00:00Z", [bad_sample], 0)
        assert result == 0

    def test_plain_z_suffix_timestamp_parses(self):
        from datetime import datetime, timezone
        ams, afc, printer, reactor = _make_ams()
        t0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        history = [(t0, 100)]
        result = ams._interpolate_encoder_at_scan("2024-01-01T00:00:00Z", history, 0)
        assert result == 100

    def test_offset_z_suffix_timestamp_parses(self):
        from datetime import datetime, timezone
        ams, afc, printer, reactor = _make_ams()
        t0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        history = [(t0, 100)]
        result = ams._interpolate_encoder_at_scan(
            "2024-01-01T00:00:00+00:00Z", history, 0)
        assert result == 100

    def test_naive_timestamp_without_offset_parses(self):
        from datetime import datetime, timezone
        ams, afc, printer, reactor = _make_ams()
        t0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        history = [(t0, 100)]
        result = ams._interpolate_encoder_at_scan("2024-01-01T00:00:00", history, 0)
        assert result == 100


# ── system_Test ────────────────────────────────────────────────────────────

class TestOamsLoad:
    """Covers the _oams_load hardware state machine. Every collaborator
    (self.oams, self._follower, self._monitor, self._verify_engagement,
    self._advance_tool_stn_to_nozzle, self._wait_for_idle) is mocked at the
    method boundary rather than simulating the underlying MCU exchange --
    this file's other test classes already cover each of those methods on
    their own."""

    def _lane(self, **overrides):
        ext = MagicMock()
        ext.tool_stn_unload = 0
        ext.tool_unload_speed = 25.0
        lane = _make_lane("lane1", extruder_obj=ext, **overrides)
        return lane

    def test_no_oams_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        lane = self._lane()
        assert ams._oams_load(lane) is False
        assert ("error", "OAMS hardware not available") in ams.logger.messages

    def test_determine_current_spool_exception_falls_through_to_real_load(self):
        oams = MagicMock()
        oams.determine_current_spool.side_effect = Exception("mcu busy")
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._verify_engagement = MagicMock(return_value=True)
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        result = ams._oams_load(lane)

        assert result is True
        oams.load_spool_with_retry.assert_called_once()
        # Called once at the very top and once before the load attempt.
        assert ams._wait_for_idle.call_count == 2
        assert any(
            lvl == "debug" and "Could not query OAMS current spool: mcu busy" in m
            for lvl, m in ams.logger.messages)

    def test_already_loaded_short_circuit_success(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = 0
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._toolhead_sensor_triggered = MagicMock(return_value=True)
        follower = MagicMock()
        monitor = MagicMock()
        ams._follower = follower
        ams._monitor = monitor
        lane = self._lane()

        result = ams._oams_load(lane)

        assert result is True
        assert oams.current_spool == 0
        follower.enable_follower.assert_called_once()
        monitor.notify_load_complete.assert_called_once_with("lane1", "oams1", 0)
        monitor.start.assert_called_once_with(oams)
        assert lane.loaded_to_hub is True
        oams.load_spool_with_retry.assert_not_called()
        assert any(
            lvl == "info"
            and m.startswith("OAMS spool 0 already loaded to the toolhead")
            for lvl, m in ams.logger.messages)

    def test_already_loaded_short_circuit_without_follower_or_monitor(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = 0
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._toolhead_sensor_triggered = MagicMock(return_value=True)
        ams._follower = None
        ams._monitor = None
        lane = self._lane()

        result = ams._oams_load(lane)  # must not raise

        assert result is True
        assert lane.loaded_to_hub is True

    def test_matching_spool_but_toolhead_not_triggered_runs_real_load(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = 0
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._toolhead_sensor_triggered = MagicMock(return_value=False)
        ams._verify_engagement = MagicMock(return_value=True)
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        result = ams._oams_load(lane)

        assert result is True
        oams.load_spool_with_retry.assert_called_once()
        assert any(
            lvl == "info"
            and "toolhead sensor does not see filament for lane1" in m
            for lvl, m in ams.logger.messages)

    def test_toolhead_sensor_check_exception_runs_real_load(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = 0
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._toolhead_sensor_triggered = MagicMock(side_effect=Exception("boom"))
        ams._verify_engagement = MagicMock(return_value=True)
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        result = ams._oams_load(lane)

        assert result is True

    def test_mismatched_spool_index_runs_real_load(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = 3  # doesn't match spool 0
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._verify_engagement = MagicMock(return_value=True)
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        result = ams._oams_load(lane)

        assert result is True

    def test_stops_monitor_and_advances_latch_before_load(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        monitor = MagicMock()
        ams._monitor = monitor
        ams._verify_engagement = MagicMock(return_value=True)
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        buf = MagicMock()
        lane = self._lane(buffer_obj=buf)

        ams._oams_load(lane)

        monitor.stop.assert_called_once()
        buf.enable_advance_latch.assert_called_once()

    def test_buffer_without_advance_latch_attr_is_skipped(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._verify_engagement = MagicMock(return_value=True)
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        buf = MagicMock(spec=[])  # no enable_advance_latch
        lane = self._lane(buffer_obj=buf)

        ams._oams_load(lane)  # must not raise

    def test_follower_stopped_then_enabled_before_load(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        follower = MagicMock()
        ams._follower = follower
        ams._verify_engagement = MagicMock(return_value=True)
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        afc.reactor.pause = MagicMock()
        lane = self._lane()

        ams._oams_load(lane)

        # First call stops (enable=0), second enables forward (enable=1)
        calls = follower.set_follower_state.call_args_list
        assert calls[0][0][2] == 0
        follower.enable_follower.assert_any_call(
            ams._get_monitor_state(), oams, 1, "before load", force=True)
        # One pause after the stop, one after the enable-forward.
        assert afc.reactor.pause.call_count == 2

    def test_load_spool_with_retry_failure_retries(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.side_effect = [
            (False, "busy"), (True, "ok")]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._verify_engagement = MagicMock(return_value=True)
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        result = ams._oams_load(lane, max_retries=3)

        assert result is True
        assert oams.load_spool_with_retry.call_count == 2
        assert (
            "error", "OAMS load attempt 1 failed: busy"
        ) in ams.logger.messages

    def test_deferred_engagement_skips_verify_engagement(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._defer_engagement = True
        ams._verify_engagement = MagicMock()
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        result = ams._oams_load(lane)

        assert result is True
        ams._verify_engagement.assert_not_called()
        ams._advance_tool_stn_to_nozzle.assert_called_once_with(
            lane, already_advanced=0.0)

    def test_non_deferred_engagement_advances_remaining_distance(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._defer_engagement = False
        ams._verify_engagement = MagicMock(return_value=True)
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        ams._oams_load(lane)

        ams._advance_tool_stn_to_nozzle.assert_called_once_with(
            lane, already_advanced=ams._engagement_length)

    def test_success_enables_follower_and_starts_monitor(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        follower = MagicMock()
        monitor = MagicMock()
        ams._follower = follower
        ams._monitor = monitor
        ams._verify_engagement = MagicMock(return_value=True)
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        result = ams._oams_load(lane)

        assert result is True
        follower.enable_follower.assert_any_call(
            ams._get_monitor_state(), oams, 1, "load complete", force=True)
        monitor.notify_load_complete.assert_called_once_with("lane1", "oams1", 0)
        monitor.start.assert_called_once_with(oams)
        assert lane.loaded_to_hub is True

    def test_engagement_failure_retracts_and_cleans_up(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        follower = MagicMock()
        ams._follower = follower
        ams._verify_engagement = MagicMock(return_value=False)
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._oams_extrude = MagicMock()
        afc.reactor.pause = MagicMock()
        lane = self._lane()
        lane.extruder_obj.tool_stn_unload = 15.0

        result = ams._oams_load(lane, max_retries=1)

        assert result is False
        ams._oams_extrude.assert_called_once()
        args = ams._oams_extrude.call_args[0]
        assert args[0] == -25.0  # -(15.0 + 10.0)
        oams.abort_current_action.assert_called_once_with(wait=True)
        oams.unload_spool_with_retry.assert_called_once()
        oams.clear_errors.assert_called_once()
        assert any(
            lvl == "info" and "Engagement failed attempt 1, cleaning up" in m
            for lvl, m in ams.logger.messages)
        # Follower is stopped 3x: before the load attempt starts, during
        # engagement cleanup, and again before hardware cleanup.
        cleanup_calls = follower.set_follower_state.call_args_list
        assert len(cleanup_calls) == 3
        assert all(c[0][2] == 0 for c in cleanup_calls)
        # wait_for_idle: start, before-attempt, then 4x during cleanup.
        assert ams._wait_for_idle.call_count == 6
        # "stop before load" + "before load" enable + "stop before cleanup"
        # (no retry-reenable pause since max_retries=1 means no next attempt).
        assert afc.reactor.pause.call_count == 3

    def test_engagement_failure_no_retract_when_tool_stn_unload_zero(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._verify_engagement = MagicMock(return_value=False)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._oams_extrude = MagicMock()
        lane = self._lane()  # tool_stn_unload=0 by default

        ams._oams_load(lane, max_retries=1)

        ams._oams_extrude.assert_not_called()

    def test_clear_errors_failure_after_engagement_failure_is_logged(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (True, "ok")
        oams.clear_errors.side_effect = Exception("boom")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._verify_engagement = MagicMock(return_value=False)
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        ams._oams_load(lane, max_retries=1)  # must not raise

        assert (
            "debug", "clear_errors after failed load: boom"
        ) in ams.logger.messages

    def test_retries_reenable_follower_between_attempts(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        follower = MagicMock()
        ams._follower = follower
        # Fail engagement on attempt 1, succeed on attempt 2
        ams._verify_engagement = MagicMock(side_effect=[False, True])
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        afc.reactor.pause = MagicMock()
        lane = self._lane()

        result = ams._oams_load(lane, max_retries=2)

        assert result is True
        follower.enable_follower.assert_any_call(
            ams._get_monitor_state(), oams, 1, "before retry", force=True)
        # "stop before load" + "before load" enable (pre-loop) + "stop
        # before cleanup" + retry-delay + post-re-enable (attempt-1 cleanup).
        assert afc.reactor.pause.call_count == 5

    def test_retries_without_follower_between_attempts(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._follower = None
        ams._verify_engagement = MagicMock(side_effect=[False, True])
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        result = ams._oams_load(lane, max_retries=2)  # must not raise

        assert result is True

    def test_last_attempt_engagement_failure_skips_retry_reenable(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        follower = MagicMock()
        ams._follower = follower
        ams._verify_engagement = MagicMock(return_value=False)
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        result = ams._oams_load(lane, max_retries=1)

        assert result is False
        reenable_calls = [
            c for c in follower.enable_follower.call_args_list
            if c[0][3] == "before retry"
        ]
        assert reenable_calls == []

    def test_unhandled_exception_during_attempt_is_logged_and_retried(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.side_effect = [
            Exception("mcu fault"), (True, "ok")]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._verify_engagement = MagicMock(return_value=True)
        ams._advance_tool_stn_to_nozzle = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        result = ams._oams_load(lane, max_retries=2)

        assert result is True
        assert any(
            lvl == "error" and "mcu fault" in m for lvl, m in ams.logger.messages)

    def test_all_attempts_exhausted_returns_false(self):
        oams = MagicMock()
        oams.determine_current_spool.return_value = None
        oams.load_spool_with_retry.return_value = (False, "busy")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        lane = self._lane()

        result = ams._oams_load(lane, max_retries=2)

        assert result is False
        assert any("failed after 2 attempts" in m for _, m in ams.logger.messages)


class TestOamsUnload:
    def _lane(self, **overrides):
        ext = MagicMock()
        ext.tool_stn_unload = 0
        ext.tool_unload_speed = 25.0
        merged = {"_oams_runout_empty": False}
        merged.update(overrides)
        lane = _make_lane("lane1", extruder_obj=ext, **merged)
        return lane

    def _ready_oams(self, hw_spool=None):
        oams = MagicMock()
        oams.determine_current_spool.return_value = hw_spool
        oams.unload_spool_with_retry.return_value = (True, "ok")
        oams.f1s_hes_value = [0, 0, 0, 0]
        return oams

    def test_no_oams_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        assert ams._oams_unload(self._lane()) is False
        assert ("error", "OAMS hardware not available") in ams.logger.messages

    def test_stops_monitor(self):
        oams = self._ready_oams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        monitor = MagicMock()
        ams._monitor = monitor

        ams._oams_unload(self._lane())

        monitor.stop.assert_called_once()
        monitor.notify_unload_complete.assert_called_once()
        assert ams._wait_for_idle.call_count == 2

    def test_no_monitor_is_safe(self):
        oams = self._ready_oams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        ams._monitor = None

        assert ams._oams_unload(self._lane()) is True

    def test_follower_reversed_before_retract(self):
        oams = self._ready_oams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        follower = MagicMock()
        ams._follower = follower
        afc.reactor.pause = MagicMock()

        ams._oams_unload(self._lane())

        follower.enable_follower.assert_any_call(
            ams._get_monitor_state(), oams, 0,
            "reverse before unload retract", force=True)
        follower.set_follower_state.assert_any_call(
            ams._get_monitor_state(), oams, 0, 0,
            "stop after unload", force=True)
        # One pause after the reverse-enable, one after stop-after-unload.
        assert afc.reactor.pause.call_count == 2

    def test_follower_reverse_failure_logged_as_warning(self):
        oams = self._ready_oams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        follower = MagicMock()
        follower.enable_follower.side_effect = Exception("mcu offline")
        ams._follower = follower

        ams._oams_unload(self._lane())  # must not raise

        assert (
            "warning",
            "OAMS: follower reverse before unload retract failed: mcu offline",
        ) in ams.logger.messages

    def test_no_follower_is_safe(self):
        oams = self._ready_oams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        ams._follower = None

        assert ams._oams_unload(self._lane()) is True

    def test_pre_retract_runs_when_tool_stn_unload_set(self):
        oams = self._ready_oams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        ams._oams_extrude = MagicMock()
        lane = self._lane()
        lane.extruder_obj.tool_stn_unload = 15.0

        ams._oams_unload(lane)

        ams._oams_extrude.assert_called_once()
        assert ams._oams_extrude.call_args[0][0] == -25.0  # -(15+10)
        assert any(
            lvl == "debug" and "Retracting 25.0mm from extruder" in m
            for lvl, m in ams.logger.messages)

    def test_pre_retract_skipped_when_tool_stn_unload_zero(self):
        oams = self._ready_oams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        ams._oams_extrude = MagicMock()

        ams._oams_unload(self._lane())  # tool_stn_unload=0 by default

        ams._oams_extrude.assert_not_called()

    def test_pre_retract_failure_logged_as_warning(self):
        oams = self._ready_oams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        ams._oams_extrude = MagicMock(side_effect=Exception("boom"))
        lane = self._lane()
        lane.extruder_obj.tool_stn_unload = 15.0

        ams._oams_unload(lane)  # must not raise

        assert (
            "warning", "Extruder retract before OAMS unload failed: boom"
        ) in ams.logger.messages

    def test_runout_empty_skips_hardware_unload(self):
        oams = self._ready_oams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        lane = self._lane(_oams_runout_empty=True)

        result = ams._oams_unload(lane)

        assert result is True
        assert lane._oams_runout_empty is False
        oams.unload_spool_with_retry.assert_not_called()
        oams.determine_current_spool.assert_not_called()
        assert any(
            lvl == "info"
            and "Skipping OAMS hardware unload for lane1" in m
            for lvl, m in ams.logger.messages)

    def test_no_spool_loaded_skips_redundant_unload(self):
        oams = self._ready_oams(hw_spool=None)
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)

        result = ams._oams_unload(self._lane())

        assert result is True
        assert oams.current_spool is None
        oams.unload_spool_with_retry.assert_not_called()
        assert any(
            lvl == "info"
            and "OAMS reports no spool loaded; skipping redundant" in m
            for lvl, m in ams.logger.messages)

    def test_spool_present_runs_hardware_unload(self):
        oams = self._ready_oams(hw_spool=0)
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)

        result = ams._oams_unload(self._lane())

        assert result is True
        oams.unload_spool_with_retry.assert_called_once()

    def test_hardware_unload_failure_returns_false(self):
        oams = self._ready_oams(hw_spool=0)
        oams.unload_spool_with_retry.return_value = (False, "busy")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)

        result = ams._oams_unload(self._lane())

        assert result is False

    def test_determine_current_spool_exception_treated_as_none(self):
        oams = self._ready_oams()
        oams.determine_current_spool.side_effect = Exception("busy")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)

        result = ams._oams_unload(self._lane())

        assert result is True
        assert oams.current_spool is None
        assert any(
            lvl == "debug" and "Could not query OAMS current spool: busy" in m
            for lvl, m in ams.logger.messages)

    def test_concurrent_retract_gcode_sent(self):
        oams = self._ready_oams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        afc.gcode = MagicMock()

        ams._oams_unload(self._lane())

        afc.gcode.run_script_from_command.assert_any_call("M83")
        afc.gcode.run_script_from_command.assert_any_call("G1 E-20.00 F1500")

    def test_concurrent_retract_uses_configured_tool_stn_unload(self):
        """When tool_stn_unload is a positive value, the concurrent retract
        distance must use it directly rather than falling back to 20mm."""
        oams = self._ready_oams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        ams._oams_extrude = MagicMock()  # skip the separate pre-retract path
        afc.gcode = MagicMock()
        lane = self._lane()
        lane.extruder_obj.tool_stn_unload = 15.0

        ams._oams_unload(lane)

        afc.gcode.run_script_from_command.assert_any_call("G1 E-15.00 F1500")

    def test_concurrent_retract_failure_logged_as_warning(self):
        oams = self._ready_oams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        afc.gcode = MagicMock()
        afc.gcode.run_script_from_command.side_effect = Exception("gcode busy")

        ams._oams_unload(self._lane())  # must not raise

        assert (
            "warning", "Concurrent retract failed: gcode busy"
        ) in ams.logger.messages

    def test_f1s_present_updates_prep_and_hub_state(self):
        oams = self._ready_oams(hw_spool=0)
        oams.f1s_hes_value = [1, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        lane = self._lane()

        ams._oams_unload(lane)

        assert lane.prep_state is True
        assert lane.loaded_to_hub is True

    def test_no_f1s_hes_value_attr_skips_state_update(self):
        oams = MagicMock(spec=["determine_current_spool", "unload_spool_with_retry"])
        oams.determine_current_spool.return_value = 0
        oams.unload_spool_with_retry.return_value = (True, "ok")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        lane = self._lane(prep_state="untouched")

        ams._oams_unload(lane)

        assert lane.prep_state == "untouched"

    def test_empty_f1s_array_skips_state_update(self):
        oams = self._ready_oams(hw_spool=0)
        oams.f1s_hes_value = []
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        lane = self._lane(prep_state="untouched")

        ams._oams_unload(lane)

        assert lane.prep_state == "untouched"

    def test_spool_index_out_of_range_skips_state_update(self):
        oams = self._ready_oams(hw_spool=0)
        oams.f1s_hes_value = [1]  # length 1
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._spool_map["lane1"] = 2  # out of range
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        lane = self._lane(prep_state="untouched")

        ams._oams_unload(lane)

        assert lane.prep_state == "untouched"

    def test_hub_settled_updates_load_state_and_last_hub(self):
        oams = self._ready_oams(hw_spool=0)
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        lane = self._lane()

        ams._oams_unload(lane)

        assert lane._load_state is False
        assert ams._last_hub[0] is False

    def test_hub_not_settled_skips_state_update(self):
        oams = self._ready_oams(hw_spool=0)
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=False)
        lane = self._lane(_load_state="untouched")

        ams._oams_unload(lane)

        assert lane._load_state == "untouched"

    def test_hub_settled_but_spool_index_beyond_last_hub_skips_index_update(self):
        oams = self._ready_oams(hw_spool=0)
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._spool_map["lane1"] = 99  # out of range for ams._last_hub (len 4)
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._wait_for_hub_settle = MagicMock(return_value=True)
        lane = self._lane()

        ams._oams_unload(lane)  # must not raise

        assert lane._load_state is False

    def test_unexpected_exception_returns_false(self):
        oams = self._ready_oams(hw_spool=0)
        oams.unload_spool_with_retry.side_effect = Exception("mcu fault")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams._wait_for_idle = MagicMock(return_value=True)

        result = ams._oams_unload(self._lane())

        assert result is False
        assert any(
            lvl == "error" and "mcu fault" in m for lvl, m in ams.logger.messages)


class TestCalibrateTd1:
    def _lane(self, **overrides):
        merged = {"td1_device_id": "td1_a", "td1_bowden_length": 500}
        merged.update(overrides)
        lane = _make_lane("lane1", **merged)
        lane.unit_obj = MagicMock()
        lane.fullname = "lane1"
        return lane

    def _clock(self, reactor, step=1.0):
        counter = itertools.count(step, step)
        reactor.monotonic = lambda: next(counter)

    def test_no_td1_device_id_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=MagicMock())
        lane = self._lane(td1_device_id=None)
        success, msg, delta = ams.calibrate_td1(lane, 0, 0)
        assert success is False
        assert "td1_device_id" in msg

    def test_no_oams_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        lane = self._lane()
        success, msg, delta = ams.calibrate_td1(lane, 0, 0)
        assert success is False
        assert "not available" in msg

    def test_invalid_td1_id_returns_false(self):
        oams = MagicMock()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (False, "bad id")
        lane = self._lane()
        success, msg, delta = ams.calibrate_td1(lane, 0, 0)
        assert success is False
        assert msg == "bad id"

    def test_load_spool_send_failure_returns_false(self):
        oams = MagicMock()
        oams.oams_load_spool_cmd.send.side_effect = Exception("mcu down")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (True, "")
        lane = self._lane()

        success, msg, delta = ams.calibrate_td1(lane, 0, 0)

        assert success is False
        assert "Failed to start load" in msg
        assert oams.action_status is None

    def test_hub_never_triggers_times_out(self):
        oams = MagicMock()
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (True, "")
        self._clock(reactor, step=20.0)  # exceeds the 15s hub timeout on iter 1
        ams._cancel_and_cleanup_td1 = MagicMock()
        lane = self._lane()

        success, msg, delta = ams.calibrate_td1(lane, 0, 0)

        assert success is False
        assert "did not trigger" in msg
        ams._cancel_and_cleanup_td1.assert_called_once()

    def test_hub_check_exception_is_swallowed_and_keeps_polling(self):
        oams = MagicMock()
        type(oams).hub_hes_value = property(
            lambda self: (_ for _ in ()).throw(Exception("boom")))
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (True, "")
        self._clock(reactor, step=1.0)  # small step: loop body must actually run
        ams._cancel_and_cleanup_td1 = MagicMock()
        lane = self._lane()

        success, msg, delta = ams.calibrate_td1(lane, 0, 0)  # must not raise

        assert success is False
        assert "did not trigger" in msg

    def test_hub_polls_a_few_times_before_triggering(self):
        oams = MagicMock()
        oams.hub_hes_value = [0, 0, 0, 0]
        oams.encoder_clicks = 100
        oams.fps_value = 0.9  # stops the TD-1 loop immediately once reached
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (True, "")
        self._clock(reactor, step=1.0)  # small step -> pause() actually runs

        pause_calls = {"n": 0}

        def pause(t):
            pause_calls["n"] += 1
            if pause_calls["n"] >= 2:
                oams.hub_hes_value = [1, 0, 0, 0]  # triggers starting iteration 2
        reactor.pause = pause
        ams._get_td1_snapshot = MagicMock(return_value=None)
        ams._cancel_and_mark_loaded = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        success, msg, delta = ams.calibrate_td1(lane, 0, 0)

        assert success is False  # FPS stop before TD-1, but hub did trigger
        assert "did not detect filament" in msg

    def test_hub_triggers_but_fps_pressure_stops_before_td1(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]
        oams.encoder_clicks = 100
        oams.fps_value = 0.9  # already at/above FPS_STOP_THRESHOLD
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (True, "")
        self._clock(reactor, step=1.0)
        ams._get_td1_snapshot = MagicMock(return_value=None)
        ams._cancel_and_mark_loaded = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        success, msg, delta = ams.calibrate_td1(lane, 0, 0)

        assert success is False
        assert "did not detect filament" in msg
        ams._unload_after_td1.assert_called_once_with(lane, ams._get_openams_spool_index(lane))
        assert any(
            lvl == "info" and m.startswith("TD-1 cal: FPS pressure 0.90")
            for lvl, m in ams.logger.messages)

    def test_td1_loop_times_out_without_detection(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]
        oams.encoder_clicks = 100
        oams.fps_value = 0.0  # never reaches stop threshold
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (True, "")
        # Small step for the hub-wait loop, then a huge jump once inside the
        # TD-1 loop to blow through its 120s deadline on the first check.
        calls = {"n": 0}

        def monotonic():
            calls["n"] += 1
            return calls["n"] * (1.0 if calls["n"] < 4 else 200.0)
        reactor.monotonic = monotonic
        ams._get_td1_snapshot = MagicMock(return_value=None)
        ams._cancel_and_mark_loaded = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        success, msg, delta = ams.calibrate_td1(lane, 0, 0)

        assert success is False
        assert "did not detect filament" in msg

    def test_td1_detected_completes_calibration_successfully(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]
        oams.encoder_clicks = 150
        oams.fps_value = 0.0
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (True, "")
        self._clock(reactor, step=1.0)
        # First snapshot call is the baseline (None); every call after that
        # inside the detection loop reports a changed snapshot.
        ams._get_td1_snapshot = MagicMock(
            side_effect=[None, ("2024-01-01T00:00:00Z", 1.75, "#fff")])
        ams._interpolate_encoder_at_scan = MagicMock(return_value=250)
        ams._cancel_and_mark_loaded = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._unload_after_td1 = MagicMock()
        reactor.pause = MagicMock()
        lane = self._lane()

        success, msg, delta = ams.calibrate_td1(lane, 0, 0)

        assert success is True
        assert delta == 100  # |encoder_at_td1(250) - encoder_at_hub(150)|
        # At least one pause in the hub-wait loop and one in the TD-1 poll loop.
        assert reactor.pause.call_count >= 2
        afc.function.ConfigRewrite.assert_called_once()
        lane.unit_obj.return_to_home.assert_called_once()
        afc.save_vars.assert_called_once()
        lane.do_enable.assert_called_once_with(False)
        ams._cancel_and_mark_loaded.assert_called_once_with(0, "lane1")
        ams._wait_for_idle.assert_called_once()
        assert (
            "raw", "TD-1 calibration: continuous load for lane1"
        ) in ams.logger.messages
        assert (
            "info", "TD-1 cal: hub triggered, encoder=150"
        ) in ams.logger.messages
        assert any(
            lvl == "info" and m.startswith("TD-1 cal: DETECTED!")
            for lvl, m in ams.logger.messages)
        assert any(
            lvl == "info" and m.startswith(
                "TD-1 calibration for lane1: hub=150, td1=250, distance=100")
            for lvl, m in ams.logger.messages)
        # Proves the poll loop actually iterated (pausing each time) and
        # accumulated encoder samples into the history list passed through.
        history_arg = ams._interpolate_encoder_at_scan.call_args[0][1]
        assert len(history_arg) >= 1

    def test_td1_snapshot_unchanged_keeps_polling_then_detects(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]
        oams.encoder_clicks = 150
        oams.fps_value = 0.0
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (True, "")
        self._clock(reactor, step=1.0)
        # baseline=None, then unchanged (None again) for one poll, then changed.
        ams._get_td1_snapshot = MagicMock(
            side_effect=[None, None, ("2024-01-01T00:00:00Z", 1.75, "#fff")])
        ams._interpolate_encoder_at_scan = MagicMock(return_value=250)
        ams._cancel_and_mark_loaded = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        success, msg, delta = ams.calibrate_td1(lane, 0, 0)

        assert success is True

    def test_hub_capture_exception_treated_as_not_triggered(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]  # would trigger...
        type(oams).encoder_clicks = property(
            lambda self: (_ for _ in ()).throw(Exception("boom")))  # ...but capture fails
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (True, "")
        self._clock(reactor, step=20.0)  # times out on the next check
        ams._cancel_and_cleanup_td1 = MagicMock()
        lane = self._lane()

        success, msg, delta = ams.calibrate_td1(lane, 0, 0)  # must not raise

        assert success is False
        assert "did not trigger" in msg

    def test_td1_loop_encoder_and_fps_read_exceptions_are_swallowed(self):
        class _FlakyOams:
            """Hub capture succeeds normally; encoder_clicks/fps_value only
            start raising once inside the TD-1 detection loop, so both of
            that loop's own try/except reads get exercised."""
            def __init__(self):
                self.hub_hes_value = [1, 0, 0, 0]
                self.action_status = None
                self.oams_load_spool_cmd = MagicMock()
                self._in_td1_phase = False

            @property
            def encoder_clicks(self):
                if self._in_td1_phase:
                    raise Exception("boom")
                return 150

            @property
            def fps_value(self):
                raise Exception("boom")

        oams = _FlakyOams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (True, "")
        self._clock(reactor, step=1.0)  # small step: hub-wait loop must actually run
        ams._get_td1_snapshot = MagicMock(return_value=None)
        ams._cancel_and_mark_loaded = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        pause_calls = {"n": 0}

        def pause(t):
            pause_calls["n"] += 1
            if pause_calls["n"] >= 2:
                oams._in_td1_phase = True  # flip once we're past the hub-wait loop
        reactor.pause = pause

        success, msg, delta = ams.calibrate_td1(lane, 0, 0)  # must not raise

        assert success is False
        assert "did not detect filament" in msg

    def test_encoder_history_trimmed_once_over_600_entries(self):
        """encoder_history is local to calibrate_td1, so the only way to
        exercise the `len(encoder_history) > 600` branch is to actually let
        the TD-1 polling loop run past 600 iterations. A tiny clock step
        relative to the 120s timeout gets well past that with no real
        delay -- each iteration is just a few mock calls.

        Note: the `encoder_history.pop(0)` trim itself has no externally
        observable effect (the list is local and never returned/exposed),
        so this test can only prove the branch's *condition* is reached,
        not that the trim itself ran -- accepted the same way as the one
        fully-redundant line documented in TestAfcAMSInit.
        """
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]
        oams.encoder_clicks = 100
        oams.fps_value = 0.0  # stays under FPS_STOP_THRESHOLD throughout
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (True, "")
        # ~2 monotonic() calls per loop iteration; step small enough that
        # >600 iterations elapse before the 120s TD-1 timeout is reached.
        self._clock(reactor, step=0.05)
        ams._get_td1_snapshot = MagicMock(return_value=None)  # never detected
        ams._cancel_and_mark_loaded = MagicMock()
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        success, msg, delta = ams.calibrate_td1(lane, 0, 0)

        assert success is False
        assert "did not detect filament" in msg

    def test_final_cancel_and_mark_loaded_failure_is_swallowed(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]
        oams.encoder_clicks = 150
        oams.fps_value = 0.9  # stops the TD-1 loop right away
        ams, afc, printer, reactor = _make_ams(oams=oams)
        afc.function.check_for_td1_id.return_value = (True, "")
        self._clock(reactor, step=1.0)
        ams._get_td1_snapshot = MagicMock(return_value=None)
        ams._cancel_and_mark_loaded = MagicMock(side_effect=Exception("boom"))
        ams._wait_for_idle = MagicMock(return_value=True)
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        success, msg, delta = ams.calibrate_td1(lane, 0, 0)  # must not raise

        assert success is False


class TestCaptureTd1WithOams:
    def _lane(self, **overrides):
        merged = dict(
            td1_device_id="td1_a", td1_bowden_length=500, tool_loaded=False,
            load_state=True, prep_state=True, td1_when_loaded=True,
        )
        merged.update(overrides)
        return _make_lane("lane1", **merged)

    def _clock(self, reactor, step=1.0):
        counter = itertools.count(step, step)
        reactor.monotonic = lambda: next(counter)

    def _capture(self, ams, lane, **kwargs):
        kwargs.setdefault("require_loaded", True)
        kwargs.setdefault("require_enabled", False)
        return ams._capture_td1_with_oams(lane, **kwargs)

    def test_settle_delay_pauses_when_within_window(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        ams._td1_last_capture_time = 0.0
        reactor.monotonic = lambda: 1.0  # 4.2 - (1.0 - 0.0) = 3.2 > 0
        reactor.pause = MagicMock()
        lane = self._lane(td1_device_id=None)  # fails right after, cheaply

        self._capture(ams, lane)

        reactor.pause.assert_called_once()

    def test_no_settle_delay_when_window_elapsed(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        ams._td1_last_capture_time = 0.0
        reactor.monotonic = lambda: 10.0  # window already elapsed
        reactor.pause = MagicMock()
        lane = self._lane(td1_device_id=None)

        self._capture(ams, lane)

        reactor.pause.assert_not_called()

    def test_require_enabled_and_disabled_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        lane = self._lane(td1_when_loaded=False)
        success, msg = self._capture(ams, lane, require_enabled=True)
        assert success is False
        assert "disabled" in msg

    def test_no_device_id_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        lane = self._lane(td1_device_id=None)
        success, msg = self._capture(ams, lane)
        assert success is False
        assert "device ID" in msg

    def test_tool_loaded_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        lane = self._lane(tool_loaded=True)
        success, msg = self._capture(ams, lane)
        assert success is False
        assert "Toolhead" in msg

    def test_no_bowden_length_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        lane = self._lane(td1_bowden_length=None)
        success, msg = self._capture(ams, lane)
        assert success is False
        assert "bowden_length" in msg

    def test_require_loaded_and_lane_not_loaded_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        lane = self._lane(load_state=False, prep_state=False)
        success, msg = self._capture(ams, lane, require_loaded=True)
        assert success is False
        assert "not loaded" in msg

    def test_no_oams_returns_false(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        lane = self._lane()
        success, msg = self._capture(ams, lane)
        assert success is False
        assert "not available" in msg

    def test_other_hub_loaded_and_never_clears_returns_false(self):
        oams = MagicMock()
        oams.hub_hes_value = [0, 1, 0, 0]  # bay 1 loaded, we want bay 0
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=10.0)  # blow through the 5s settle deadline
        lane = self._lane()

        success, msg = self._capture(ams, lane)

        assert success is False
        assert "already loaded" in msg

    def test_empty_hub_values_skips_conflict_check(self):
        oams = MagicMock()
        oams.hub_hes_value = []
        oams.oams_load_spool_cmd.send.side_effect = Exception("stop here")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        lane = self._lane()

        success, msg = self._capture(ams, lane)

        # Got past the (skipped) conflict check straight to the load-send step
        assert "Failed to start spool load" in msg

    def test_hub_values_containing_unbooleanable_entry_is_swallowed(self):
        class _Cursed:
            def __bool__(self):
                raise Exception("boom")

        oams = MagicMock()
        oams.hub_hes_value = [0, _Cursed(), 0, 0]  # bay 1 raises when checked
        oams.oams_load_spool_cmd.send.side_effect = Exception("stop here")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        lane = self._lane()

        success, msg = self._capture(ams, lane)  # must not raise

        assert "Failed to start spool load" in msg

    def test_settle_loop_read_exception_stops_waiting(self):
        oams = MagicMock()
        oams.hub_hes_value = [0, 1, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=0.5)
        calls = {"n": 0}

        def flaky_hub_values():
            calls["n"] += 1
            if calls["n"] >= 2:
                raise Exception("boom")
            return [0, 1, 0, 0]
        # getattr(self.oams, "hub_hes_value", None) needs a real attribute
        # error path, so drive it through a side-effecting property instead.
        type(oams).hub_hes_value = property(lambda self: flaky_hub_values())
        lane = self._lane()

        success, msg = self._capture(ams, lane)  # must not raise

        assert success is False
        assert "already loaded" in msg

    def test_other_hub_loaded_clears_during_settle_then_proceeds(self):
        oams = MagicMock()
        oams.hub_hes_value = [0, 1, 0, 0]
        oams.oams_load_spool_cmd.send.side_effect = Exception("stop here")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=0.5)

        def pause(t):
            oams.hub_hes_value = [0, 0, 0, 0]  # clears on first pause
        reactor.pause = pause
        lane = self._lane()

        success, msg = self._capture(ams, lane)

        # Got past the "other hub loaded" gate and reached the load-send step
        assert "Failed to start spool load" in msg

    def test_load_spool_send_failure_returns_false(self):
        oams = MagicMock()
        oams.hub_hes_value = [0, 0, 0, 0]
        oams.oams_load_spool_cmd.send.side_effect = Exception("mcu down")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        lane = self._lane()

        success, msg = self._capture(ams, lane)

        assert success is False
        assert "Failed to start spool load" in msg
        assert oams.action_status is None

    def test_hub_never_detected_runs_full_cleanup(self):
        oams = MagicMock()
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=1.0)  # small step: loop body must actually run
        reactor.pause = MagicMock()
        ams._cancel_and_mark_loaded = MagicMock()
        ams._clear_lane_state_after_td1 = MagicMock()
        lane = self._lane()

        success, msg = self._capture(ams, lane)

        assert success is False
        assert "Hub sensor did not trigger" in msg
        oams.unload_spool.assert_called_once()
        oams.clear_errors.assert_called_once()
        ams._clear_lane_state_after_td1.assert_called_once_with(lane)
        ams._cancel_and_mark_loaded.assert_called_once_with(0, "lane1")
        oams.set_oams_follower.assert_called_once_with(0, 0)
        assert reactor.pause.call_count >= 1

    def test_hub_wait_check_exception_is_swallowed(self):
        class _PhasedOams:
            """hub_hes_value reads safely for the (unprotected) initial
            conflict check, then starts raising once we're in the
            try/except-guarded hub-wait loop."""
            def __init__(self):
                self.action_status = None
                self.oams_load_spool_cmd = MagicMock()
                self.set_oams_follower = MagicMock()
                self.unload_spool = MagicMock()
                self.clear_errors = MagicMock()
                self._reads = 0

            @property
            def hub_hes_value(self):
                self._reads += 1
                if self._reads <= 1:
                    return [0, 0, 0, 0]
                raise Exception("boom")

        oams = _PhasedOams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=1.0)
        ams._cancel_and_mark_loaded = MagicMock()
        ams._clear_lane_state_after_td1 = MagicMock()
        lane = self._lane()

        success, msg = self._capture(ams, lane)  # must not raise

        assert success is False
        assert "Hub sensor did not trigger" in msg

    def test_hub_never_detected_cleanup_step_failures_are_all_swallowed(self):
        oams = MagicMock()
        oams.hub_hes_value = [0, 0, 0, 0]
        oams.set_oams_follower.side_effect = Exception("boom1")
        oams.unload_spool.side_effect = Exception("boom2")
        oams.clear_errors.side_effect = Exception("boom3")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=20.0)
        ams._cancel_and_mark_loaded = MagicMock(side_effect=Exception("boom0"))
        ams._clear_lane_state_after_td1 = MagicMock()
        lane = self._lane()

        success, msg = self._capture(ams, lane)  # must not raise

        assert success is False
        ams._clear_lane_state_after_td1.assert_called_once_with(lane)

    def test_encoder_before_read_failure_runs_cleanup(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]  # detected immediately
        type(oams).encoder_clicks = property(
            lambda self: (_ for _ in ()).throw(Exception("boom")))
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=1.0)
        ams._cancel_and_mark_loaded = MagicMock()
        ams._clear_lane_state_after_td1 = MagicMock()
        lane = self._lane()

        success, msg = self._capture(ams, lane)

        assert success is False
        assert "Unable to read encoder" in msg
        ams._cancel_and_mark_loaded.assert_called_once_with(0, "lane1")
        oams.unload_spool.assert_called_once()
        oams.clear_errors.assert_called_once()
        ams._clear_lane_state_after_td1.assert_called_once_with(lane)

    def test_encoder_before_failure_cleanup_step_failures_are_all_swallowed(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]
        type(oams).encoder_clicks = property(
            lambda self: (_ for _ in ()).throw(Exception("boom")))
        oams.unload_spool.side_effect = Exception("boom2")
        oams.clear_errors.side_effect = Exception("boom3")
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=1.0)
        ams._cancel_and_mark_loaded = MagicMock(side_effect=Exception("boom0"))
        ams._clear_lane_state_after_td1 = MagicMock()
        lane = self._lane()

        success, msg = self._capture(ams, lane)  # must not raise

        assert success is False
        ams._clear_lane_state_after_td1.assert_called_once_with(lane)

    def test_td1_never_detected_after_target_clicks_reached(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]
        oams.encoder_clicks = 1000  # already past target_clicks (bowden 500)
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=1.0)
        ams._get_td1_snapshot = MagicMock(return_value=None)  # never changes
        ams._cancel_and_mark_loaded = MagicMock()
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        success, msg = self._capture(ams, lane)

        assert success is False
        assert "not captured" in msg
        ams._unload_after_td1.assert_called_once()
        assert ams._td1_last_capture_time is not None

    def test_click_tracking_loop_breaks_once_target_reached(self):
        class _MovingOams:
            """encoder_clicks starts at 0 for the hub capture, then advances
            past the bowden-length target once the click-tracking loop
            begins polling."""
            def __init__(self):
                self.hub_hes_value = [1, 0, 0, 0]
                self.action_status = None
                self.oams_load_spool_cmd = MagicMock()
                self.set_oams_follower = MagicMock()
                self.unload_spool = MagicMock()
                self.clear_errors = MagicMock()
                self._clicks = 0

            @property
            def encoder_clicks(self):
                return self._clicks

        oams = _MovingOams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=1.0)
        ams._get_td1_snapshot = MagicMock(return_value=None)
        ams._cancel_and_mark_loaded = MagicMock()
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        def pause(t):
            oams._clicks = 600  # past the 500-click target on the next check
        reactor.pause = pause

        success, msg = self._capture(ams, lane)

        assert success is False  # TD-1 still never detected, just tracking coverage
        assert "not captured" in msg

    def test_click_tracking_loop_read_exception_falls_back_to_before(self):
        class _FlakyClicksOams:
            def __init__(self):
                self.hub_hes_value = [1, 0, 0, 0]
                self.action_status = None
                self.oams_load_spool_cmd = MagicMock()
                self.unload_spool = MagicMock()
                self.clear_errors = MagicMock()
                self._captured_before = False

            @property
            def encoder_clicks(self):
                if not self._captured_before:
                    self._captured_before = True
                    return 100  # value used for encoder_before
                raise Exception("boom")  # every later read fails

        oams = _FlakyClicksOams()
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=1.0)  # hub-wait loop must actually detect first
        ams._get_td1_snapshot = MagicMock(return_value=None)
        ams._cancel_and_mark_loaded = MagicMock()
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        success, msg = self._capture(ams, lane)  # must not raise

        assert success is False
        assert "not captured" in msg

    def test_td1_detected_captures_data_successfully(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]
        oams.encoder_clicks = 1000
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=1.0)
        reactor.pause = MagicMock()
        ams._get_td1_snapshot = MagicMock(
            # baseline (None), one unchanged poll (still None), then changed.
            side_effect=[None, None, ("2024-01-01T00:00:00Z", 1.75, "#fff")])
        afc.moonraker.get_td1_data = MagicMock(
            return_value={"td1_a": {"td": 1.75, "color": "#fff"}})
        ams._cancel_and_mark_loaded = MagicMock()
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        success, msg = self._capture(ams, lane)

        assert success is True
        assert msg == "TD-1 data captured"
        assert lane.td1_data == {"td": 1.75, "color": "#fff"}
        assert (
            "info", "lane1 TD-1 data captured: td=1.75 color=#fff"
        ) in ams.logger.messages
        afc.save_vars.assert_called_once()
        assert reactor.pause.call_count >= 1
        ams._cancel_and_mark_loaded.assert_called_once_with(0, "lane1")

    def test_td1_detected_but_moonraker_read_fails(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]
        oams.encoder_clicks = 1000
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=1.0)
        ams._get_td1_snapshot = MagicMock(
            side_effect=[None, ("2024-01-01T00:00:00Z", 1.75, "#fff")])
        afc.moonraker.get_td1_data = MagicMock(side_effect=Exception("moonraker down"))
        ams._cancel_and_mark_loaded = MagicMock()
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        success, msg = self._capture(ams, lane)

        assert success is False
        assert "not captured" in msg
        assert (
            "error", "TD-1 capture failed for lane1: moonraker down"
        ) in ams.logger.messages

    def test_td1_detected_but_moonraker_is_none(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]
        oams.encoder_clicks = 1000
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=1.0)
        ams._get_td1_snapshot = MagicMock(
            side_effect=[None, ("2024-01-01T00:00:00Z", 1.75, "#fff")])
        afc.moonraker = None
        ams._cancel_and_mark_loaded = MagicMock()
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        messages_before = list(ams.logger.messages)
        success, msg = self._capture(ams, lane)

        assert success is False
        assert "not captured" in msg
        assert ams.logger.messages[len(messages_before):] == [
            ("error", "TD-1 capture failed for lane1: moonraker not connected"),
        ]

    def test_cancel_and_mark_loaded_failure_is_swallowed(self):
        oams = MagicMock()
        oams.hub_hes_value = [1, 0, 0, 0]
        oams.encoder_clicks = 1000
        ams, afc, printer, reactor = _make_ams(oams=oams)
        self._clock(reactor, step=1.0)
        ams._get_td1_snapshot = MagicMock(return_value=None)
        ams._cancel_and_mark_loaded = MagicMock(side_effect=Exception("boom"))
        ams._unload_after_td1 = MagicMock()
        lane = self._lane()

        success, msg = self._capture(ams, lane)  # must not raise

        assert success is False


class TestSystemTest:
    def test_no_oams_reports_not_connected(self):
        ams, afc, printer, reactor = _make_ams(oams=None)
        lane = _make_lane("lane1")
        result = ams.system_Test(lane, 0, False, True)
        assert result is False

    def test_empty_bay_reports_ready_for_spool(self):
        oams = MagicMock()
        oams.f1s_hes_value = [0, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams.lane_loaded = MagicMock()
        lane = _make_lane("lane1", remember_spool=False)
        ams._spool_map["lane1"] = 0

        result = ams.system_Test(lane, 0, False, True)

        assert result is True
        afc.spool.clear_values.assert_called_once_with(lane)
        ams.lane_loaded.assert_not_called()
        afc.function.afc_led.assert_called_once_with(lane.led_not_ready, lane.led_index)
        assert any(
            lvl == "info" and m.startswith("lane1 tool cmd: T0 ")
            for lvl, m in ams.logger.messages)

    def test_unmapped_lane_treated_as_empty_despite_sensor_values(self):
        """A lane with no _spool_map entry gets slot=-1; the
        '0 <= slot < len(...)' guard must force f1s/hub_present to False
        rather than Python-wrap to index -1 (which would read all-True
        sensor values here and wrongly report the bay as loaded)."""
        oams = MagicMock()
        oams.f1s_hes_value = [1, 1, 1, 1]
        oams.hub_hes_value = [1, 1, 1, 1]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams.lane_loaded = MagicMock()
        lane = _make_lane("lane1", remember_spool=False)
        # No ams._spool_map["lane1"] entry -> slot defaults to -1.

        result = ams.system_Test(lane, 0, False, True)

        assert result is True
        assert lane.prep_state is False
        assert lane._load_state is False
        assert lane.loaded_to_hub is False
        ams.lane_loaded.assert_not_called()

    def test_remembered_spool_skips_clear_values(self):
        oams = MagicMock()
        oams.f1s_hes_value = [0, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        lane = _make_lane("lane1", remember_spool=True)
        ams._spool_map["lane1"] = 0

        ams.system_Test(lane, 0, False, True)

        afc.spool.clear_values.assert_not_called()

    def test_loaded_bay_not_tool_loaded_sets_loaded_state(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams.lane_loaded = MagicMock()
        ams.lane_illuminate_spool = MagicMock()
        lane = _make_lane("lane1", tool_loaded=False)
        ams._spool_map["lane1"] = 0

        result = ams.system_Test(lane, 0, False, True)

        assert result is True
        assert lane.prep_state is True
        assert lane.loaded_to_hub is True
        assert lane.status == AFCLaneState.LOADED
        ams.lane_loaded.assert_called_once_with(lane)
        ams.lane_illuminate_spool.assert_called_once_with(lane)

    def test_tool_loaded_active_lane_sets_tooled_state(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams.lane_loaded = MagicMock()
        ams.lane_illuminate_spool = MagicMock()
        ams.lane_tool_loaded = MagicMock()
        ext = MagicMock()
        ext.lane_loaded = "lane1"
        ext.prep_on_shuttle_check.return_value = ""
        lane = _make_lane("lane1", tool_loaded=True, extruder_obj=ext)
        ams._spool_map["lane1"] = 0
        afc.current = "lane1"

        result = ams.system_Test(lane, 0, False, True)

        assert result is True
        assert lane.status == AFCLaneState.TOOLED
        afc.spool.set_active_spool.assert_called_once_with(lane.spool_id)
        ams.lane_tool_loaded.assert_called_once_with(lane)
        lane.enable_buffer.assert_called_once()
        lane.sync_to_extruder.assert_called_once()

    def test_tool_loaded_active_lane_fires_tool_loaded_event(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams.lane_loaded = MagicMock()
        ams.lane_illuminate_spool = MagicMock()
        ams.lane_tool_loaded = MagicMock()
        printer.send_event = MagicMock(wraps=printer.send_event)
        ext = MagicMock()
        ext.lane_loaded = "lane1"
        ext.prep_on_shuttle_check.return_value = ""
        lane = _make_lane("lane1", tool_loaded=True, extruder_obj=ext)
        ams._spool_map["lane1"] = 0
        afc.current = "lane1"

        ams.system_Test(lane, 0, False, True)

        printer.send_event.assert_called_once_with("afc:tool_loaded", lane)

    def test_tool_loaded_inactive_lane_sets_idle_state(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams.lane_loaded = MagicMock()
        ams.lane_illuminate_spool = MagicMock()
        ams.lane_tool_loaded_idle = MagicMock()
        ext = MagicMock()
        ext.lane_loaded = "lane1"
        ext.prep_on_shuttle_check.return_value = ""
        lane = _make_lane("lane1", tool_loaded=True, extruder_obj=ext)
        ams._spool_map["lane1"] = 0
        afc.current = "some_other_lane"

        ams.system_Test(lane, 0, False, True)

        ams.lane_tool_loaded_idle.assert_called_once_with(lane)

    def test_tool_loaded_enables_follower_and_monitor(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams.lane_loaded = MagicMock()
        ams.lane_illuminate_spool = MagicMock()
        ams.lane_tool_loaded_idle = MagicMock()
        follower = MagicMock()
        monitor = MagicMock()
        ams._follower = follower
        ams._monitor = monitor
        ext = MagicMock()
        ext.lane_loaded = "lane1"
        ext.prep_on_shuttle_check.return_value = ""
        lane = _make_lane("lane1", tool_loaded=True, extruder_obj=ext)
        ams._spool_map["lane1"] = 0
        afc.current = "some_other_lane"

        ams.system_Test(lane, 0, False, True)

        follower.enable_follower.assert_called_once()
        monitor.notify_load_complete.assert_called_once()
        monitor.start.assert_called_once_with(oams)

    def test_extruder_lane_mismatch_skips_tool_loaded_branch(self):
        oams = MagicMock()
        oams.f1s_hes_value = [1, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        ams.lane_loaded = MagicMock()
        ams.lane_illuminate_spool = MagicMock()
        ams.lane_tool_loaded = MagicMock()
        ext = MagicMock()
        ext.lane_loaded = "different_lane"
        lane = _make_lane("lane1", tool_loaded=True, extruder_obj=ext)
        ams._spool_map["lane1"] = 0

        ams.system_Test(lane, 0, False, True)

        ams.lane_tool_loaded.assert_not_called()

    def test_assign_tcmd_true_calls_tcmd_assign(self):
        oams = MagicMock()
        oams.f1s_hes_value = [0, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        lane = _make_lane("lane1")
        ams._spool_map["lane1"] = 0

        ams.system_Test(lane, 0, True, True)

        afc.function.TcmdAssign.assert_called_once_with(lane)

    def test_assign_tcmd_false_skips_tcmd_assign(self):
        oams = MagicMock()
        oams.f1s_hes_value = [0, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        lane = _make_lane("lane1")
        ams._spool_map["lane1"] = 0

        ams.system_Test(lane, 0, False, True)

        afc.function.TcmdAssign.assert_not_called()

    def test_always_disables_stepper_and_marks_prep_done(self):
        oams = MagicMock()
        oams.f1s_hes_value = [0, 0, 0, 0]
        oams.hub_hes_value = [0, 0, 0, 0]
        ams, afc, printer, reactor = _make_ams(oams=oams)
        lane = _make_lane("lane1")
        ams._spool_map["lane1"] = 0

        ams.system_Test(lane, 0, False, True)

        lane.do_enable.assert_called_once_with(False)
        lane.set_afc_prep_done.assert_called_once()
