"""
Unit tests for extras/AFC_QidiBox.py
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

from configfile import error as config_error
from extras.AFC_BoxTurtle import afcBoxTurtle
from extras.AFC_QidiBox import QIDI_DRYER_TARGET_MAX_TEMP, afcQidiBox, load_config_prefix
from extras.AFC_lane import AFCMoveWarning, MoveDirection
from extras.AFC_unit import afcUnit
from tests.conftest import MockAFC, MockConfig, MockGCodeCommand, MockPrinter

NEVER = 9_999_999_999.0


def _make_box(values: Optional[dict] = None, name: str = "QidiBox_1") -> afcQidiBox:
    afc = MockAFC()
    printer = MockPrinter(afc=afc)
    config = MockConfig(name=f"AFC_QidiBox {name}", printer=printer, values=values or {})
    return afcQidiBox(config)


def _with_reactor(box: afcQidiBox, now: float = 100.0) -> MagicMock:
    reactor = MagicMock()
    reactor.monotonic.return_value = now
    reactor.NEVER = NEVER
    box.reactor = reactor
    return reactor


def _with_heater(box: afcQidiBox, max_temp: float = 100.0) -> MagicMock:
    heater = MagicMock()
    heater.max_temp = max_temp
    box.dryer_heater = heater
    return heater


def _lane(name: str = "lane1", prep_state: bool = False, load_state: bool = False,
          tool_loaded: bool = False) -> MagicMock:
    lane = MagicMock()
    lane.name = name
    lane.prep_state = prep_state
    lane.load_state = load_state
    lane.tool_loaded = tool_loaded
    return lane


def _hub_lane(hub_state: bool = False, dist_hub: float = 200.0, move_dis: float = 50.0):
    hub = SimpleNamespace(name="QidiBox_1", state=hub_state, move_dis=move_dis)
    lane = MagicMock()
    lane.name = "lane1"
    lane.prep = None
    lane.dist_hub = dist_hub
    lane.hub_obj = hub
    lane.load_to_hub = True
    lane.loaded_to_hub = True
    lane.is_direct_hub.return_value = False
    return lane, hub


class TestQidiBoxInheritance:
    def test_is_box_turtle_subclass(self):
        assert issubclass(afcQidiBox, afcBoxTurtle)

    def test_sets_load_only_without_prep(self):
        assert afcQidiBox.load_only_without_prep is True


class TestQidiBoxInit:
    def test_defaults(self):
        box = _make_box()
        assert box.type == "QIDI_Box"
        assert box.MAX_NUM_MOVES == 40
        assert box.preload_park_dist == 80.0
        assert box.dryer_heater_name is None
        assert box.dryer_humidity_sensor_name is None
        assert box.dryer_max_temp == 65.0
        assert box.dryer_default_temp == 50.0
        assert box.dryer_default_duration == 240.0
        assert box.dry_rotate_length == 50.0
        assert box.dry_rotate_speed == 80.0
        assert box.dry_rotate_interval == 300.0
        assert box.dryer_heater is None
        assert box.dryer_humidity_sensor is None
        assert box.dry_active is False

    def test_reads_overrides(self):
        box = _make_box({
            "type": "Custom", "max_short_moves": 120, "preload_park_dist": 60,
            "dryer_heater": "QidiBox_1_dryer", "dryer_humidity_sensor": "aht10 air",
            "dryer_max_temp": 70, "dryer_default_temp": 55, "dryer_default_duration": 480,
            "dry_rotate_length": 30, "dry_rotate_speed": 40, "dry_rotate_interval": 600,
        })
        assert box.type == "Custom"
        assert box.MAX_NUM_MOVES == 120
        assert afcBoxTurtle.MAX_NUM_MOVES == 40
        assert box.preload_park_dist == 60.0
        assert box.dryer_heater_name == "QidiBox_1_dryer"
        assert box.dryer_humidity_sensor_name == "aht10 air"
        assert box.dryer_max_temp == 70.0
        assert box.dryer_default_temp == 55.0
        assert box.dryer_default_duration == 480.0
        assert box.dry_rotate_length == 30.0
        assert box.dry_rotate_speed == 40.0
        assert box.dry_rotate_interval == 600.0

    def test_dryer_max_temp_limit_is_qidis_target_max_temp(self):
        config = MagicMock()
        config.get_name.return_value = "AFC_QidiBox QidiBox_1"
        config.get.side_effect = lambda option, default=None: default
        config.getboolean.side_effect = lambda option, default=None, **kw: default
        config.getint.side_effect = lambda option, default=None, **kw: default
        config.getfloat.side_effect = lambda option, default=None, **kw: default
        config.get_printer.return_value = MockPrinter()
        afcQidiBox(config)
        assert call('dryer_max_temp', 65.0, minval=0.0,
                    maxval=QIDI_DRYER_TARGET_MAX_TEMP) in config.getfloat.call_args_list
        assert QIDI_DRYER_TARGET_MAX_TEMP == 90.0

    def test_default_temp_above_max_temp_is_a_config_error(self):
        with pytest.raises(config_error) as excinfo:
            _make_box({"dryer_max_temp": 60, "dryer_default_temp": 61})
        assert str(excinfo.value) == ("dryer_default_temp (61.0) in [AFC_QidiBox QidiBox_1] "
                                      "cannot be above dryer_max_temp (60.0)")

    def test_default_temp_equal_to_max_temp_is_allowed(self):
        box = _make_box({"dryer_max_temp": 60, "dryer_default_temp": 60})
        assert box.dryer_default_temp == 60.0


class TestQidiBoxHandleConnect:
    def test_without_dryer_sets_logos_and_registers_nothing(self):
        box = _make_box()
        box._lookup_dryer = MagicMock()
        with patch.object(afcBoxTurtle, "handle_connect") as mock_super:
            box.handle_connect()
        mock_super.assert_called_once_with()
        assert box.logo == '<span class=success--text>QIDI Box Ready</span>\n  QidiBox_1\n'
        assert box.logo_error == ('<span class=error--text>QIDI Box Not Ready</span>\n'
                                  '  QidiBox_1\n')
        box._lookup_dryer.assert_not_called()
        box.afc.function.register_mux_command.assert_not_called()

    def test_with_dryer_looks_up_dryer_and_registers_commands(self):
        box = _make_box({"dryer_heater": "QidiBox_1_dryer"})
        box._lookup_dryer = MagicMock()
        with patch.object(afcBoxTurtle, "handle_connect"):
            box.handle_connect()
        box._lookup_dryer.assert_called_once_with()
        assert box.afc.function.register_mux_command.call_args_list == [
            call(True, 'AFC_QIDIBOX_DRY', "UNIT", "QidiBox_1", box.cmd_AFC_QIDIBOX_DRY,
                 "Start QIDI Box filament dryer", box.cmd_AFC_QIDIBOX_DRY_options),
            call(True, 'AFC_QIDIBOX_DRY_STOP', "UNIT", "QidiBox_1",
                 box.cmd_AFC_QIDIBOX_DRY_STOP, "Stop QIDI Box filament dryer",
                 box.cmd_AFC_QIDIBOX_DRY_STOP_options),
        ]


class TestQidiBoxLookupDryer:
    def _box(self, values: dict, heater_max_temp: float = 100.0):
        box = _make_box({"dryer_heater": "QidiBox_1_dryer", **values})
        heater = MagicMock()
        heater.max_temp = heater_max_temp
        pheaters = box.printer.lookup_object("heaters")
        pheaters.lookup_heater.return_value = heater
        return box, heater, pheaters

    def test_heater_only(self):
        box, heater, pheaters = self._box({})
        box._lookup_dryer()
        pheaters.lookup_heater.assert_called_once_with("QidiBox_1_dryer")
        assert box.dryer_heater is heater
        assert box.dryer_humidity_sensor is None

    def test_max_temp_above_heater_max_temp_is_a_config_error(self):
        box, _, _ = self._box({"dryer_max_temp": 65}, heater_max_temp=60.0)
        with pytest.raises(config_error) as excinfo:
            box._lookup_dryer()
        assert str(excinfo.value) == ("dryer_max_temp (65.0) for [AFC_QidiBox QidiBox_1] is "
                                      "above max_temp (60.0) of [heater_generic "
                                      "QidiBox_1_dryer]")

    def test_max_temp_equal_to_heater_max_temp_is_allowed(self):
        box, heater, _ = self._box({"dryer_max_temp": 65, "dryer_default_temp": 50},
                                   heater_max_temp=65.0)
        box._lookup_dryer()
        assert box.dryer_heater is heater

    def test_humidity_sensor_found(self):
        box, _, _ = self._box({"dryer_humidity_sensor": "aht10 QidiBox_1_dryer_air"})
        sensor = MagicMock()
        box.printer._objects["aht10 QidiBox_1_dryer_air"] = sensor
        box._lookup_dryer()
        assert box.dryer_humidity_sensor is sensor

    def test_humidity_sensor_missing_is_a_config_error(self):
        box, _, _ = self._box({"dryer_humidity_sensor": "QidiBox_1_dryer_air"})
        with pytest.raises(config_error) as excinfo:
            box._lookup_dryer()
        assert str(excinfo.value) == ("dryer_humidity_sensor 'QidiBox_1_dryer_air' for "
                                      "[AFC_QidiBox QidiBox_1] not found, use the sensor's "
                                      "object name, e.g. 'aht10 QidiBox_1_dryer_air'")
        assert box.dryer_humidity_sensor is None


class TestQidiBoxSystemTest:
    def test_prep_less_lane_takes_prep_from_load(self):
        box = _make_box()
        lane = MagicMock()
        lane.prep = None
        lane.prep_state = False
        lane.raw_load_state = True
        gcmd = MockGCodeCommand()
        with patch.object(afcBoxTurtle, "system_Test", return_value=True) as mock_super:
            result = box.system_Test(lane, 0.7, True, False, gcmd)
        assert lane.prep_state is True
        assert result is True
        mock_super.assert_called_once_with(lane, 0.7, True, False, gcmd)

    def test_prep_less_empty_lane_clears_prep(self):
        box = _make_box()
        lane = MagicMock()
        lane.prep = None
        lane.prep_state = True
        lane.raw_load_state = False
        with patch.object(afcBoxTurtle, "system_Test", return_value=False) as mock_super:
            result = box.system_Test(lane, 0.7, False, True, "callback")
        assert lane.prep_state is False
        assert result is False
        mock_super.assert_called_once_with(lane, 0.7, False, True, "callback")

    def test_lane_with_prep_keeps_its_prep_state(self):
        box = _make_box()
        lane = MagicMock()
        lane.prep = "QidiBox_1:TRG1"
        lane.prep_state = False
        lane.raw_load_state = True
        with patch.object(afcBoxTurtle, "system_Test", return_value=True):
            box.system_Test(lane, 0.7, True, True, "callback")
        assert lane.prep_state is False


class TestQidiBoxOnFilamentInsert:
    def _box(self, printing: bool = False):
        box = _make_box()
        box._preload_to_hub = MagicMock()
        box.afc.function.is_printing = MagicMock(return_value=printing)
        return box

    def test_hub_lane_is_preloaded_and_event_sent(self):
        box = self._box()
        lane, _ = _hub_lane()
        box.printer.send_event = MagicMock()
        box.on_filament_insert(lane, "callback")
        box.printer.send_event.assert_called_once_with("afc:lane_inserted", lane)
        box.afc.function.is_printing.assert_called_once_with(check_movement=True)
        box._preload_to_hub.assert_called_once_with(lane, "callback")
        assert box.logger.messages == []

    def test_lane_with_prep_is_not_preloaded(self):
        box = self._box()
        lane, _ = _hub_lane()
        lane.prep = "QidiBox_1:TRG1"
        box.on_filament_insert(lane, "callback")
        box._preload_to_hub.assert_not_called()
        box.afc.function.is_printing.assert_not_called()

    def test_direct_lane_is_not_preloaded(self):
        box = self._box()
        lane, _ = _hub_lane()
        lane.is_direct_hub.return_value = True
        box.on_filament_insert(lane, "callback")
        box._preload_to_hub.assert_not_called()
        box.afc.function.is_printing.assert_not_called()

    def test_load_to_hub_off_is_not_preloaded(self):
        box = self._box()
        lane, _ = _hub_lane()
        lane.load_to_hub = False
        box.on_filament_insert(lane, "callback")
        box._preload_to_hub.assert_not_called()
        box.afc.function.is_printing.assert_not_called()

    def test_printer_busy_skips_preload(self):
        box = self._box(printing=True)
        lane, _ = _hub_lane()
        box.on_filament_insert(lane, "callback")
        box._preload_to_hub.assert_not_called()
        assert box.logger.messages == [
            ("info", "lane1: printer or hub QidiBox_1 is busy, filament is fed to the hub on "
                     "its next load")]

    def test_occupied_hub_skips_preload(self):
        box = self._box()
        lane, _ = _hub_lane(hub_state=True)
        box.on_filament_insert(lane, "callback")
        box._preload_to_hub.assert_not_called()
        assert box.logger.messages == [
            ("info", "lane1: printer or hub QidiBox_1 is busy, filament is fed to the hub on "
                     "its next load")]


class TestQidiBoxPreloadToHub:
    def _box(self, warn=AFCMoveWarning.NONE, hub_after_home: bool = True):
        box = _make_box({"max_short_moves": 3})
        _with_reactor(box)
        lane, hub = _hub_lane()

        def fake_move_to_hub(*args, **kwargs):
            hub.state = hub_after_home
            return True, 250.0, warn

        box.move_to_hub = MagicMock(side_effect=fake_move_to_hub)
        box.afc.error.AFC_error = MagicMock()
        return box, lane, hub

    def test_reaches_hub_on_homing_move_and_parks(self):
        box, lane, hub = self._box()

        def park(distance, speed, accel, gcmd):
            hub.state = False

        lane.move.side_effect = park
        box._preload_to_hub(lane, "callback")
        box.move_to_hub.assert_called_once_with(lane, 250.0, MoveDirection.POS, True,
                                                gcmd="callback")
        assert lane.move.call_args_list == [call(-80.0, 50.0, 400.0, gcmd="callback")]
        assert lane.loaded_to_hub is False
        lane.do_enable.assert_called_once_with(False)
        box.afc.error.AFC_error.assert_not_called()
        assert box.logger.messages == [("info", "lane1 filament parked 80mm before hub "
                                                "QidiBox_1")]

    def test_homing_disabled_is_passed_through(self):
        box, lane, hub = self._box()
        box.afc.homing_enabled = False
        lane.move.side_effect = lambda *a, **kw: setattr(hub, "state", False)
        box._preload_to_hub(lane, "callback")
        box.move_to_hub.assert_called_once_with(lane, 250.0, MoveDirection.POS, False,
                                                gcmd="callback")

    def test_short_moves_until_hub_triggers(self):
        box, lane, hub = self._box(hub_after_home=False)
        moves = []

        def move(distance, speed, accel, gcmd):
            moves.append(distance)
            hub.state = len(moves) == 2

        lane.move.side_effect = move
        box._preload_to_hub(lane, "callback")
        assert moves == [10, 10, -80.0]
        assert lane.loaded_to_hub is False
        box.afc.error.AFC_error.assert_not_called()

    def test_never_reaching_hub_is_an_error_after_max_short_moves(self):
        box, lane, _ = self._box(hub_after_home=False)
        box._preload_to_hub(lane, "callback")
        assert lane.move.call_args_list == [call(10, 50.0, 400.0, gcmd="callback")] * 3
        box.afc.error.AFC_error.assert_called_once_with(
            "lane1 filament did not reach hub QidiBox_1, check the filament path", False,
            gcmd="callback")
        assert lane.loaded_to_hub is True
        lane.do_enable.assert_called_once_with(False)
        assert box.logger.messages == []

    def test_homing_error_stops_without_short_moves(self):
        box, lane, _ = self._box(warn=AFCMoveWarning.ERROR, hub_after_home=False)
        box._preload_to_hub(lane, "callback")
        lane.move.assert_not_called()
        box.afc.error.AFC_error.assert_called_once_with(
            "lane1 filament did not reach hub QidiBox_1, check the filament path", False,
            gcmd="callback")
        lane.do_enable.assert_called_once_with(False)

    def test_homing_error_with_hub_triggered_still_parks(self):
        box, lane, hub = self._box(warn=AFCMoveWarning.ERROR, hub_after_home=True)
        lane.move.side_effect = lambda *a, **kw: setattr(hub, "state", False)
        box._preload_to_hub(lane, "callback")
        assert lane.move.call_args_list == [call(-80.0, 50.0, 400.0, gcmd="callback")]
        box.afc.error.AFC_error.assert_not_called()

    def test_still_at_hub_after_park_is_an_error(self):
        box, lane, _ = self._box()
        box._preload_to_hub(lane, "callback")
        box.afc.error.AFC_error.assert_called_once_with(
            "lane1 filament still at hub QidiBox_1 after parking 80mm, raise "
            "preload_park_dist", False, gcmd="callback")
        assert lane.loaded_to_hub is True
        lane.do_enable.assert_called_once_with(False)
        assert box.logger.messages == []

    def test_disables_lane_when_a_move_raises(self):
        box, lane, _ = self._box()
        lane.move.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            box._preload_to_hub(lane, "callback")
        lane.do_enable.assert_called_once_with(False)


class TestQidiBoxDryActive:
    def test_false_without_timer(self):
        box = _make_box()
        assert box.dry_active is False

    def test_true_with_timer(self):
        box = _make_box()
        box._dry_timer = object()
        assert box.dry_active is True


class TestQidiBoxGetStatus:
    def test_without_dryer(self):
        box = _make_box()
        status = box.get_status(50.0)
        assert status["dryer"] == {"active": False, "target": 0.0, "temperature": None,
                                   "humidity": None, "remaining": 0, "rotate": False}
        assert status["lanes"] == []

    def test_with_dryer_running(self):
        box = _make_box()
        heater = _with_heater(box)
        heater.get_status.return_value = {"temperature": 48.25, "target": 55.0}
        sensor = MagicMock()
        sensor.get_status.return_value = {"temperature": 48.0, "humidity": 12}
        box.dryer_humidity_sensor = sensor
        box._dry_timer = object()
        box._dry_target = 55.0
        box._dry_end_time = 1000.4
        box._dry_next_rotate = 300.0
        status = box.get_status(100.0)
        heater.get_status.assert_called_once_with(100.0)
        sensor.get_status.assert_called_once_with(100.0)
        assert status["dryer"] == {"active": True, "target": 55.0, "temperature": 48.25,
                                   "humidity": 12, "remaining": 900, "rotate": True}

    def test_remaining_never_negative(self):
        box = _make_box()
        box._dry_timer = object()
        box._dry_end_time = 90.0
        assert box.get_status(100.0)["dryer"]["remaining"] == 0

    def test_remaining_is_zero_when_not_drying(self):
        box = _make_box()
        box._dry_end_time = 1000.0
        assert box.get_status(100.0)["dryer"]["remaining"] == 0

    def test_eventtime_none_uses_reactor_time(self):
        box = _make_box()
        reactor = _with_reactor(box, now=200.0)
        heater = _with_heater(box)
        heater.get_status.return_value = {"temperature": 20.0}
        box.get_status()
        reactor.monotonic.assert_called_once_with()
        heater.get_status.assert_called_once_with(200.0)


class TestQidiBoxCmdDry:
    def _box(self):
        box = _make_box({"dryer_default_temp": 45, "dryer_default_duration": 120})
        box._start_drying = MagicMock()
        return box

    def test_defaults_from_config(self):
        box = self._box()
        box.cmd_AFC_QIDIBOX_DRY(MockGCodeCommand({}))
        box._start_drying.assert_called_once_with(45.0, 120.0, False)
        assert box.logger.messages == []

    def test_params(self):
        box = self._box()
        box.cmd_AFC_QIDIBOX_DRY(MockGCodeCommand({"TEMP": 55, "DURATION": 480, "ROTATE": 1}))
        box._start_drying.assert_called_once_with(55.0, 480.0, True)

    def test_temp_at_max_is_not_capped(self):
        box = self._box()
        box.cmd_AFC_QIDIBOX_DRY(MockGCodeCommand({"TEMP": 65}))
        box._start_drying.assert_called_once_with(65.0, 120.0, False)
        assert box.logger.messages == []

    def test_temp_above_max_is_capped(self):
        box = self._box()
        box.cmd_AFC_QIDIBOX_DRY(MockGCodeCommand({"TEMP": 80}))
        box._start_drying.assert_called_once_with(65.0, 120.0, False)
        assert box.logger.messages == [
            ("info", "QidiBox_1 dryer: TEMP 80°C capped to dryer_max_temp 65°C")]

    def test_invalid_rotate_is_rejected(self):
        box = self._box()
        gcmd = MockGCodeCommand({"ROTATE": 2})
        with pytest.raises(Exception):
            box.cmd_AFC_QIDIBOX_DRY(gcmd)
        box._start_drying.assert_not_called()

    def test_zero_temp_is_rejected(self):
        box = self._box()
        with pytest.raises(Exception):
            box.cmd_AFC_QIDIBOX_DRY(MockGCodeCommand({"TEMP": 0}))
        box._start_drying.assert_not_called()


class TestQidiBoxCmdDryStop:
    def test_stops_drying(self):
        box = _make_box()
        box._stop_drying = MagicMock()
        box.cmd_AFC_QIDIBOX_DRY_STOP(MockGCodeCommand({}))
        box._stop_drying.assert_called_once_with("QidiBox_1 dryer stopped")


class TestQidiBoxStartDrying:
    def test_starts_cycle_without_rotation(self):
        box = _make_box()
        reactor = _with_reactor(box, now=100.0)
        heater = _with_heater(box)
        timer = MagicMock()
        reactor.register_timer.return_value = timer
        box._start_drying(55.0, 240.0, False)
        heater.set_temp.assert_called_once_with(55.0)
        assert box._dry_target == 55.0
        assert box._dry_end_time == 100.0 + 240.0 * 60
        assert box._dry_next_rotate is None
        reactor.register_timer.assert_called_once_with(box._dry_tick, 14500.0)
        assert box._dry_timer is timer
        reactor.unregister_timer.assert_not_called()
        assert box.logger.messages == [("info", "QidiBox_1 dryer started: 55°C for 240 min")]

    def test_starts_cycle_with_rotation(self):
        box = _make_box()
        reactor = _with_reactor(box, now=100.0)
        _with_heater(box)
        box._start_drying(50.0, 60.0, True)
        assert box._dry_next_rotate == 400.0
        reactor.register_timer.assert_called_once_with(box._dry_tick, 400.0)
        assert box.logger.messages == [
            ("info", "QidiBox_1 dryer started: 50°C for 60 min, turning free spools every "
                     "300s")]

    def test_replaces_running_cycle(self):
        box = _make_box()
        reactor = _with_reactor(box)
        _with_heater(box)
        old_timer = MagicMock()
        box._dry_timer = old_timer
        box._start_drying(50.0, 60.0, False)
        reactor.unregister_timer.assert_called_once_with(old_timer)
        assert box._dry_timer is reactor.register_timer.return_value


class TestQidiBoxStopDrying:
    def test_turns_heater_off_and_clears_cycle(self):
        box = _make_box()
        reactor = _with_reactor(box)
        heater = _with_heater(box)
        timer = MagicMock()
        box._dry_timer = timer
        box._dry_target = 55.0
        box._dry_end_time = 500.0
        box._dry_next_rotate = 400.0
        box._stop_drying("done")
        reactor.unregister_timer.assert_called_once_with(timer)
        heater.set_temp.assert_called_once_with(0.0)
        assert box._dry_timer is None
        assert box._dry_target == 0.0
        assert box._dry_end_time == 0.0
        assert box._dry_next_rotate is None
        assert box.logger.messages == [("info", "done")]

    def test_without_heater_only_logs(self):
        box = _make_box()
        _with_reactor(box)
        box._stop_drying("stopped")
        assert box.dryer_heater is None
        assert box.logger.messages == [("info", "stopped")]


class TestQidiBoxCancelDryTimer:
    def test_no_timer(self):
        box = _make_box()
        reactor = _with_reactor(box)
        box._cancel_dry_timer()
        reactor.unregister_timer.assert_not_called()

    def test_unregisters_timer(self):
        box = _make_box()
        reactor = _with_reactor(box)
        timer = MagicMock()
        box._dry_timer = timer
        box._cancel_dry_timer()
        reactor.unregister_timer.assert_called_once_with(timer)
        assert box._dry_timer is None


class TestQidiBoxNextDryWake:
    def test_without_rotation_is_cycle_end(self):
        box = _make_box()
        box._dry_end_time = 1000.0
        box._dry_next_rotate = None
        assert box._next_dry_wake() == 1000.0

    def test_rotation_before_end(self):
        box = _make_box()
        box._dry_end_time = 1000.0
        box._dry_next_rotate = 400.0
        assert box._next_dry_wake() == 400.0

    def test_end_before_rotation(self):
        box = _make_box()
        box._dry_end_time = 1000.0
        box._dry_next_rotate = 1100.0
        assert box._next_dry_wake() == 1000.0


class TestQidiBoxDryTick:
    def _box(self, end: float = 1000.0, next_rotate: Optional[float] = None):
        box = _make_box()
        reactor = _with_reactor(box)
        heater = _with_heater(box)
        box._dry_timer = MagicMock()
        box._dry_end_time = end
        box._dry_next_rotate = next_rotate
        box._dry_target = 55.0
        box._rotate_spools = MagicMock()
        return box, reactor, heater

    def test_cycle_end_turns_heater_off(self):
        box, reactor, heater = self._box(end=1000.0, next_rotate=900.0)
        assert box._dry_tick(1000.0) == NEVER
        heater.set_temp.assert_called_once_with(0.0)
        reactor.unregister_timer.assert_not_called()
        assert box._dry_timer is None
        assert box._dry_next_rotate is None
        box._rotate_spools.assert_not_called()
        assert box.logger.messages == [("info", "QidiBox_1 dryer finished drying cycle")]

    def test_rotation_due_turns_spools_and_reschedules(self):
        box, _, heater = self._box(end=1000.0, next_rotate=400.0)
        assert box._dry_tick(410.0) == 710.0
        box._rotate_spools.assert_called_once_with(gcmd="callback")
        assert box._dry_next_rotate == 710.0
        heater.set_temp.assert_not_called()

    def test_rotation_not_due_yet(self):
        box, _, _ = self._box(end=1000.0, next_rotate=400.0)
        assert box._dry_tick(399.0) == 400.0
        box._rotate_spools.assert_not_called()
        assert box._dry_next_rotate == 400.0

    def test_without_rotation_waits_for_end(self):
        box, _, heater = self._box(end=1000.0, next_rotate=None)
        assert box._dry_tick(500.0) == 1000.0
        box._rotate_spools.assert_not_called()
        heater.set_temp.assert_not_called()


class TestQidiBoxRotateSpools:
    def _box(self, printing: bool = False, state: str = "Idle"):
        box = _make_box()
        box.afc.function.is_printing = MagicMock(return_value=printing)
        box.afc.current_state = state
        return box

    def test_printing_skips_rotation(self):
        box = self._box(printing=True)
        lane = _lane()
        box.lanes = {"lane1": lane}
        box._rotate_spools(gcmd="callback")
        box.afc.function.is_printing.assert_called_once_with(check_movement=True)
        lane.move.assert_not_called()
        assert box.logger.messages == [
            ("debug", "QidiBox_1 dryer: spool rotation skipped, printer busy")]

    def test_busy_afc_skips_rotation(self):
        box = self._box(state="Loading")
        lane = _lane()
        box.lanes = {"lane1": lane}
        box._rotate_spools(gcmd="callback")
        lane.move.assert_not_called()
        assert box.logger.messages == [
            ("debug", "QidiBox_1 dryer: spool rotation skipped, printer busy")]

    def test_turns_only_free_lanes(self):
        box = self._box()
        free = _lane("lane1")
        prep = _lane("lane2", prep_state=True)
        load = _lane("lane3", load_state=True)
        tool = _lane("lane4", tool_loaded=True)
        box.lanes = {"lane1": free, "lane2": prep, "lane3": load, "lane4": tool}
        box._rotate_spools(gcmd="callback")
        free.move.assert_called_once_with(-50.0, 80.0, 400.0, gcmd="callback")
        free.do_enable.assert_called_once_with(False)
        for lane in (prep, load, tool):
            lane.move.assert_not_called()
            lane.do_enable.assert_not_called()
        assert box.logger.messages == [("debug", "QidiBox_1 dryer: turned spools for lane1")]

    def test_no_free_lanes(self):
        box = self._box()
        box.lanes = {"lane1": _lane("lane1", prep_state=True, load_state=True)}
        box._rotate_spools(gcmd="callback")
        assert box.logger.messages == [("debug", "QidiBox_1 dryer: turned spools for no lanes")]


class TestQidiBoxLoadConfigPrefix:
    def test_returns_qidi_box(self):
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_QidiBox QidiBox_2", printer=printer)
        unit = load_config_prefix(config)
        assert isinstance(unit, afcQidiBox)
        assert isinstance(unit, afcUnit)
        assert unit.name == "QidiBox_2"
