"""Unit tests for the CFS unit's lane logic (runout routing, refusals, cuts).
Lanes are plain AFC lanes; the box I/O methods of a real afcCFS object are
mocked here. tests/test_AFC_CFS_reactor.py covers the box I/O for real."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from extras import AFC_CFS as P
from extras.AFC_CFS import afcCFS
from extras.AFC_lane import AFCLane, AFCLaneState
from tests.conftest import MockAFC, MockLogger, MockReactor


class AdvancingReactor(MockReactor):
    """MockReactor whose pause() moves the clock, so timeout loops end."""

    def pause(self, until):
        self._monotonic = max(self._monotonic, until)
        return until


class _Cfg:
    """Stands in for a config section: every option at its real default."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def getint(self, key, default=None, **kw):
        return int(self.values.get(key, default))

    def getfloat(self, key, default=None, **kw):
        return float(self.values.get(key, default))

    def getboolean(self, key, default=None):
        return bool(self.values.get(key, default))


BOX_IO = ("on_filament_insert", "on_filament_remove", "pre_loading", "set_box_mode",
          "connection_motor", "tighten_up", "extrude_stage", "retrude_stage",
          "read_box_status", "clear_latched_error", "reel_slot", "connect",
          "apply_lane_state")


def _make_unit(afc):
    unit = afcCFS.__new__(afcCFS)
    unit.name = "CFS_1"
    unit._load_cfs_config(_Cfg({"serial": "/dev/null"}))
    unit.afc = afc
    unit.reactor = AdvancingReactor()
    unit.logger = MockLogger()
    unit.printer = MagicMock()
    unit.gcode = MagicMock()
    unit.trace = MagicMock()
    unit.transport = MagicMock()
    unit.lanes = {}
    unit.online = True
    unit.cutter_sensor_pin = None
    unit.cutter_sensor_state = False
    unit.cutter_trigger_count = 0
    unit.cutter_wait_time = 0.01
    unit.cutter_require_trigger = True
    unit.cutter_source = None
    unit.load_attempts = 2
    unit._operation_depth = 0
    unit._rfid_busy = False
    unit.lane_tags = {}
    unit._lane_commands = set()
    unit.present_last = {}
    unit.load_reports = {}
    for name in BOX_IO:
        setattr(unit, name, MagicMock())
    unit.read_material_mask = MagicMock(return_value=0b1111)
    unit.material_mask = 0b1111
    unit.connected_mask = 0
    return unit


def _make_lane(index=1, printing=False, tool_loaded=False, runout_lane=None):
    lane = AFCLane.__new__(AFCLane)
    afc = MockAFC()
    afc.tool_cut = True
    afc.tool_cut_cmd = "AFC_CUT"
    afc.park = False
    afc.form_tip = False
    afc._check_extruder_temp = MagicMock(return_value=False)
    afc.error.handle_lane_failure = MagicMock()
    afc.gcode.run_script_from_command = MagicMock()
    afc.save_vars = MagicMock()
    afc.function.is_printing = MagicMock(return_value=printing)

    unit = _make_unit(afc)

    lane.name = "lane{}".format(index)
    lane.index = index
    lane.afc = afc
    lane.unit_obj = unit
    lane.reactor = AdvancingReactor()
    lane.logger = MockLogger()
    lane.extruder_obj = MagicMock()
    lane.extruder_obj.name = "extruder"
    lane.extruder_obj.tool_start = "pin"
    lane.extruder_obj.tool_start_state = False
    lane.extruder_obj.fila_tool_start = None   # newer AFC checks this first
    lane.select_lane = MagicMock()
    lane.disable_buffer = MagicMock()
    lane.set_loaded = MagicMock()
    lane.set_unloaded = MagicMock()
    lane._perform_pause_runout = MagicMock()
    lane._perform_infinite_runout = MagicMock()
    lane._afc_prep_done = True
    unit.present_last[lane.name] = True
    lane.status = AFCLaneState.LOADED
    lane.tool_loaded = tool_loaded
    lane.runout_lane = runout_lane
    unit.load_reports[lane.name] = {}
    unit.lanes[lane.name] = lane
    return lane


class TestPresence:
    def test_first_report_only_mirrors_state(self):
        lane = _make_lane()
        lane.unit_obj.present_last[lane.name] = None
        lane.unit_obj.update_lane_presence(lane, True)
        assert lane.prep_state is True and lane.loaded_to_hub is True
        lane.set_loaded.assert_not_called()

    def test_unchanged_state_is_ignored(self):
        lane = _make_lane()
        lane.unit_obj.update_lane_presence(lane, True)
        lane.afc.save_vars.assert_not_called()

    def test_insert_marks_lane_loaded(self):
        lane = _make_lane()
        lane.unit_obj.present_last[lane.name] = False
        lane.status = AFCLaneState.NONE
        lane.unit_obj.update_lane_presence(lane, True)
        lane.set_loaded.assert_called_once()
        lane.unit_obj.on_filament_insert.assert_called_once_with(lane)

    def test_removal_when_idle_unloads_lane(self):
        lane = _make_lane()
        lane.unit_obj.update_lane_presence(lane, False)
        lane.set_unloaded.assert_called_once()
        lane._perform_pause_runout.assert_not_called()

    def test_runout_while_printing_pauses(self):
        lane = _make_lane(printing=True, tool_loaded=True)
        lane.unit_obj.update_lane_presence(lane, False)
        lane._perform_pause_runout.assert_called_once()
        lane.set_unloaded.assert_not_called()

    def test_runout_with_runout_lane_uses_infinite_spool(self):
        lane = _make_lane(printing=True, tool_loaded=True, runout_lane="lane2")
        lane.unit_obj.update_lane_presence(lane, False)
        lane._perform_infinite_runout.assert_called_once()
        lane._perform_pause_runout.assert_not_called()

    def test_runout_ignored_when_sensor_disabled(self):
        lane = _make_lane(printing=True, tool_loaded=True)
        lane.fila_load = MagicMock()
        lane.fila_load.runout_helper.sensor_enabled = False
        lane.unit_obj.update_lane_presence(lane, False)
        lane._perform_pause_runout.assert_not_called()
        lane._perform_infinite_runout.assert_not_called()


class TestLoadRefusals:
    def test_refuses_when_toolhead_already_loaded(self):
        lane = _make_lane()
        lane.extruder_obj.tool_start_state = True
        lane.unit_obj.cmd_AFC_CFS_TOOL_LOAD(lane, None)
        msg = lane.afc.error.handle_lane_failure.call_args.args[1]
        assert "already sees filament" in msg
        lane.unit_obj.extrude_stage.assert_not_called()

    def test_refuses_when_cutter_engaged(self):
        lane = _make_lane()
        lane.unit_obj.cutter_sensor_pin = "PA1"
        lane.unit_obj.cutter_sensor_state = True
        lane.unit_obj.cmd_AFC_CFS_TOOL_LOAD(lane, None)
        assert "cutter" in lane.afc.error.handle_lane_failure.call_args.args[1]

    def test_refuses_empty_slot(self):
        lane = _make_lane(index=3)
        lane.unit_obj.read_material_mask = MagicMock(return_value=0b0011)
        lane.unit_obj.cmd_AFC_CFS_TOOL_LOAD(lane, None)
        assert "no filament in slot C" in lane.afc.error.handle_lane_failure.call_args.args[1]

    def test_refuses_when_box_offline(self):
        lane = _make_lane()
        lane.unit_obj.online = False
        lane.unit_obj.connect = MagicMock(return_value=False)
        lane.unit_obj.transport.last_error = "open failed"
        lane.unit_obj.cmd_AFC_CFS_TOOL_LOAD(lane, None)
        assert "offline" in lane.afc.error.handle_lane_failure.call_args.args[1]


class TestUnloadCutter:
    def test_fails_when_cutter_never_releases(self):
        lane = _make_lane(tool_loaded=True)
        lane.unit_obj.cutter_sensor_pin = "PA1"
        calls = {"n": 0}

        def cut(script):
            calls["n"] += 1
            lane.unit_obj.cutter_sensor_state = True   # blade stays down

        lane.afc.gcode.run_script_from_command = MagicMock(side_effect=cut)
        lane.unit_obj.cmd_AFC_CFS_TOOL_UNLOAD(lane, None)
        assert "did not release" in lane.afc.error.handle_lane_failure.call_args.args[1]
        lane.unit_obj.retrude_stage.assert_not_called()
        lane.unit_obj.reel_slot.assert_not_called()

    def test_no_cutter_switch_trusts_macro(self):
        lane = _make_lane(tool_loaded=True)
        assert lane.unit_obj._run_cutter_sequence(lane) is None
        lane.afc.gcode.run_script_from_command.assert_called_once_with("AFC_CUT EXTRUDER=extruder")

    def _switch_lane(self, fires=True, require=True):
        lane = _make_lane(tool_loaded=True)
        unit = lane.unit_obj
        unit.cutter_sensor_pin = "nozzle_mcu:PA8"
        unit.cutter_require_trigger = require
        cuts = {"n": 0}

        def cut(script):
            cuts["n"] += 1
            if fires:       # blade reached end of travel and came back
                unit.cutter_trigger_count += 1
            unit.cutter_sensor_state = False

        lane.afc.gcode.run_script_from_command = MagicMock(side_effect=cut)
        return lane, cuts

    def test_switch_fired_and_released_is_a_good_cut(self):
        lane, cuts = self._switch_lane(fires=True)
        assert lane.unit_obj._run_cutter_sequence(lane) is None
        assert cuts["n"] == 1
        lane.afc.toolhead.wait_moves.assert_called()

    def test_switch_never_fired_retries_then_fails(self):
        lane, cuts = self._switch_lane(fires=False)
        reason = lane.unit_obj._run_cutter_sequence(lane)
        assert "never fired" in reason
        assert cuts["n"] == lane.unit_obj.load_attempts + 1

    def test_unload_stops_before_retract_when_cut_unconfirmed(self):
        lane, _ = self._switch_lane(fires=False)
        lane.unit_obj.cmd_AFC_CFS_TOOL_UNLOAD(lane, None)
        assert "never fired" in lane.afc.error.handle_lane_failure.call_args.args[1]
        lane.unit_obj.retrude_stage.assert_not_called()
        lane.unit_obj.reel_slot.assert_not_called()

    def test_trigger_not_required_accepts_release_only(self):
        lane, cuts = self._switch_lane(fires=False, require=False)
        assert lane.unit_obj._run_cutter_sequence(lane) is None
        assert cuts["n"] == 1


class TestUnitConfig:
    def test_parse_retract_steps(self):
        assert afcCFS._parse_steps("15:90, 15:500,") == [(15.0, 90.0), (15.0, 500.0)]
        assert afcCFS._parse_steps("-15:90") == [(15.0, 90.0)]

    def test_parse_retract_steps_rejects_garbage(self):
        with pytest.raises(Exception):
            afcCFS._parse_steps("15-90")


def test_slot_mapping_for_lane():
    lane = _make_lane(index=4)
    assert P.slot_bit(lane.index) == P.SLOT_BITS[3]
    assert P.slot_letter(lane.index) == "D"


class TestSystemTest:
    """PREP-time lane restore. Must work on AFCProject main (lane.map is a
    string, no map_to_string) and on newer AFC (map list + map_to_string)."""

    def _unit(self, mask=0b0001, online=True):
        unit = afcCFS.__new__(afcCFS)
        unit.name = "CFS_1"
        unit.logger = MagicMock()
        unit.afc = MockAFC()
        unit.afc.current = None
        unit.online = online
        unit.material_mask = mask
        unit.connect = MagicMock(return_value=online)
        unit.read_material_mask = MagicMock(return_value=mask)
        unit.connected_mask = 0
        unit.read_connected_mask = MagicMock(return_value=0)
        unit.lane_loaded = MagicMock()
        unit.lane_not_ready = MagicMock()
        unit.lane_tool_loaded = MagicMock()
        unit.lane_tool_loaded_idle = MagicMock()
        unit.boot_wait = 0.0
        unit._boot_wait_done = False
        unit.present_last = {}
        unit.load_reports = {}
        unit.reactor = AdvancingReactor()
        return unit

    def _lane(self, index=1, with_map_to_string=True):
        lane = MagicMock()
        lane.name = "lane{}".format(index)
        lane.index = index
        lane.tool_loaded = False
        lane.map = "T0" if not with_map_to_string else ["T0"]
        if with_map_to_string:
            lane.map_to_string = MagicMock(return_value="T0")
        else:
            del lane.map_to_string
        return lane

    @pytest.mark.parametrize("with_map_to_string", [True, False])
    def test_present_slot_marks_loaded(self, with_map_to_string):
        unit = self._unit(mask=0b0001)
        lane = self._lane(1, with_map_to_string)
        assert unit.system_Test(lane, 0, True, False) is True
        assert lane.prep_state is True and lane.loaded_to_hub is True
        # Present but not in the shared path: AFC's virtual hub must read clear
        assert lane._load_state is False
        unit.lane_loaded.assert_called_once_with(lane)
        lane.set_afc_prep_done.assert_called_once()
        assert "lane1 tool cmd: T0  FILAMENT PRESENT" in unit.logger.info.call_args.args[0]

    def test_connected_slot_sets_load_sensor(self):
        unit = self._unit(mask=0b0011)
        unit.connected_mask = 0b0010
        lane1, lane2 = self._lane(1), self._lane(2)
        unit.system_Test(lane1, 0, False, False)
        unit.system_Test(lane2, 0, False, False)
        assert lane1._load_state is False and lane2._load_state is True
        assert lane1.loaded_to_hub is True and lane2.loaded_to_hub is True

    def _tooled(self, unit, lane, box_b3):
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = lane.name
        unit.request = MagicMock(return_value=P.Frame(1, P.ST_OK, P.GET_BOX_STATE,
                                                      bytes([0x18, 0x1c, 0x00, box_b3])))
        unit.set_box_mode = MagicMock()
        unit.trace = MagicMock()

    def test_tooled_lane_restores_lost_print_mode(self):
        unit = self._unit(mask=0b0001)
        lane = self._lane(1)
        self._tooled(unit, lane, 0x00)
        unit.system_Test(lane, 0, False, False)
        unit.set_box_mode.assert_called_once_with(P.MODE_PRINT, 0b0001)

    def test_tooled_lane_keeps_existing_print_mode(self):
        unit = self._unit(mask=0b0001)
        lane = self._lane(1)
        self._tooled(unit, lane, P.BOX_STATE_PRINT_LOCKED)
        unit.system_Test(lane, 0, False, False)
        unit.set_box_mode.assert_not_called()

    def test_empty_slot_marks_not_ready(self):
        unit = self._unit(mask=0b0001)
        lane = self._lane(2, with_map_to_string=False)
        unit.system_Test(lane, 0, False, False)
        assert lane.prep_state is False
        unit.lane_not_ready.assert_called_once_with(lane)

    def test_offline_box_reports_not_responding(self):
        unit = self._unit(online=False)
        unit.material_mask = None
        lane = self._lane(1)
        assert unit.system_Test(lane, 0, False, False) is False
        assert "NOT RESPONDING" in unit.logger.info.call_args.args[0]
        lane.set_afc_prep_done.assert_called_once()
        # Unknown rather than empty, so the first poll doesn't see an insert
        assert unit.present_last[lane.name] is None

    def test_waits_for_booting_box_once(self):
        unit = self._unit(online=False)
        unit.material_mask = None
        unit.boot_wait = 10.0
        attempts = {"n": 0}

        def connect():
            attempts["n"] += 1
            if attempts["n"] >= 4:          # answers on the 4th try
                unit.online = True
                unit.material_mask = 0b0001
            return unit.online

        unit.connect = MagicMock(side_effect=connect)
        assert unit.system_Test(self._lane(1), 0, False, False) is True
        assert attempts["n"] == 4
        unit.lane_loaded.assert_called_once()

    def test_boot_wait_only_for_first_lane(self):
        unit = self._unit(online=False)
        unit.material_mask = None
        unit.boot_wait = 3.0
        unit.system_Test(self._lane(1), 0, False, False)
        first = unit.connect.call_count
        unit.system_Test(self._lane(2), 0, False, False)
        assert unit.connect.call_count == first + 1


class TestFirstReadingAfterOfflinePrep:
    def test_first_reading_is_not_an_insert(self):
        lane = _make_lane(tool_loaded=True)
        lane.unit_obj.present_last[lane.name] = None
        lane.unit_obj.apply_lane_state = MagicMock(return_value="FILAMENT PRESENT in ToolHead")
        lane.unit_obj.update_lane_presence(lane, True)
        lane.unit_obj.apply_lane_state.assert_called_once_with(lane, True)
        lane.unit_obj.on_filament_insert.assert_not_called()
        lane.set_loaded.assert_not_called()
        assert lane.unit_obj.present_last[lane.name] is True

    def test_first_reading_empty_is_not_a_runout(self):
        lane = _make_lane(printing=True, tool_loaded=False)
        lane.unit_obj.present_last[lane.name] = None
        lane.unit_obj.apply_lane_state = MagicMock(return_value="EMPTY READY FOR SPOOL")
        lane.unit_obj.update_lane_presence(lane, False)
        lane.unit_obj.on_filament_remove.assert_not_called()
        lane.set_unloaded.assert_called_once()


class TestVirtualHubGate:
    """AFC's TOOL_LOAD only proceeds when `lane.load_state and not hub.state`.
    With switch_pin: virtual, hub.state is any(lane.raw_load_state) and
    lane.load_state is lane.loaded_to_hub. Check that with AFC's real code."""

    def _hub(self, lanes):
        from extras.AFC_hub import afc_hub
        hub = afc_hub.__new__(afc_hub)
        hub.switch_pin = "virtual"
        hub._state = False
        hub.lanes = {l.name: l for l in lanes}
        return hub

    def _real_lane(self, index, hub):
        from extras.AFC_CFS import set_lane_sensors
        lane = AFCLane.__new__(AFCLane)
        lane.name = "lane{}".format(index)
        lane.index = index
        lane.hub_obj = hub
        return lane, set_lane_sensors

    def test_all_spools_present_hub_clear_and_lane_ready(self):
        hub = self._hub([])
        lanes = []
        for i in (1, 2, 3, 4):
            lane, set_sensors = self._real_lane(i, hub)
            set_sensors(lane, True, 0b0000)
            lanes.append(lane)
        hub.lanes = {l.name: l for l in lanes}
        assert hub.state is False
        assert all(l.load_state for l in lanes)

    def test_connected_slot_occupies_hub(self):
        hub = self._hub([])
        lanes = []
        for i in (1, 2):
            lane, set_sensors = self._real_lane(i, hub)
            set_sensors(lane, True, 0b0001)
            lanes.append(lane)
        hub.lanes = {l.name: l for l in lanes}
        assert hub.state is True

    def test_empty_slot_not_ready(self):
        hub = self._hub([])
        lane, set_sensors = self._real_lane(1, hub)
        set_sensors(lane, False, 0)
        hub.lanes = {lane.name: lane}
        assert lane.load_state is False


class TestSharedCutter:
    def test_box_without_pin_uses_the_chains_cutter(self):
        lane = _make_lane(tool_loaded=True)
        lane.unit_obj.cutter_sensor_pin = None
        other = MagicMock()
        other.cutter_sensor_pin = "nozzle_mcu:PA8"
        other.cutter_sensor_state = False
        other.cutter_trigger_count = 0
        other.cutter_wait_time = 0.01
        other.cutter_require_trigger = True
        lane.unit_obj.cutter_source = other

        def cut(script):
            other.cutter_trigger_count += 1      # switch fired and released

        lane.afc.gcode.run_script_from_command = MagicMock(side_effect=cut)
        assert lane.unit_obj._run_cutter_sequence(lane) is None
        assert lane.unit_obj._cutter() is other


class TestLaneSetup:
    """At connect the unit turns plain [AFC_lane] sections into CFS lanes."""

    def _plain_lane(self, unit, index=1, **attrs):
        lane = AFCLane.__new__(AFCLane)
        lane.name = "lane{}".format(index)
        lane.index = index
        lane.unit_obj = unit
        lane.prep = None
        lane.load = None
        lane.custom_load_cmd = None
        lane.custom_unload_cmd = None
        lane._load_state = True          # AFC's default for a lane without a load switch
        for key, value in attrs.items():
            setattr(lane, key, value)
        unit.lanes[lane.name] = lane
        return lane

    def test_sets_load_unload_and_registers_commands(self):
        unit = _make_unit(MockAFC())
        lane = self._plain_lane(unit, 2)
        unit._setup_lanes()
        assert lane.custom_load_cmd == "AFC_CFS_TOOL_LOAD LANE=lane2"
        assert lane.custom_unload_cmd == "AFC_CFS_TOOL_UNLOAD LANE=lane2"
        assert lane._load_state is False          # the box's connection sensor drives it
        assert unit.present_last[lane.name] is None
        assert unit.load_reports[lane.name] == {}
        registered = {c.args[0] for c in unit.gcode.register_mux_command.call_args_list
                      if c.args[2] == "lane2"}
        assert registered == {"AFC_CFS_TOOL_LOAD", "AFC_CFS_TOOL_UNLOAD",
                              "AFC_CFS_EJECT", "AFC_CFS_READ_TAG"}

    def test_registered_command_runs_for_its_lane(self):
        unit = _make_unit(MockAFC())
        lane = self._plain_lane(unit, 3)
        calls = []
        unit.cmd_AFC_CFS_EJECT = lambda l, g: calls.append((l, g))
        unit._setup_lanes()
        handler = next(c.args[3] for c in unit.gcode.register_mux_command.call_args_list
                       if c.args[0] == "AFC_CFS_EJECT" and c.args[2] == "lane3")
        handler("gcmd")
        assert calls == [(lane, "gcmd")]

    def test_keeps_a_custom_load_cmd_from_the_config(self):
        unit = _make_unit(MockAFC())
        lane = self._plain_lane(unit, 1, custom_load_cmd="MY_LOAD")
        unit._setup_lanes()
        assert lane.custom_load_cmd == "MY_LOAD"

    def test_registers_commands_once(self):
        unit = _make_unit(MockAFC())
        self._plain_lane(unit, 1)
        unit._setup_lanes()
        unit._setup_lanes()
        assert unit.gcode.register_mux_command.call_count == 4

    def test_rejects_prep_or_load_pins(self):
        unit = _make_unit(MockAFC())
        self._plain_lane(unit, 1, prep="PA1")
        with pytest.raises(Exception, match="remove prep/load pins"):
            unit._setup_lanes()

    def test_rejects_slot_index_out_of_range(self):
        unit = _make_unit(MockAFC())
        self._plain_lane(unit, 5)
        with pytest.raises(Exception, match="slot A-D"):
            unit._setup_lanes()


class TestRealAfcLaneSection:
    """A real [AFC_lane laneN] section, built through AFC's own constructor,
    becomes a CFS lane at connect: no custom lane class needed."""

    def _real_lane(self, name="lane2", unit_value="CFS_1:2"):
        from tests.conftest import MockConfig, MockPrinter
        afc = MockAFC()
        afc.load_to_hub = False
        printer = MockPrinter(afc=afc)
        config = MockConfig(name="AFC_lane {}".format(name), printer=printer,
                            values={"unit": unit_value})
        return AFCLane(config)

    def test_plain_section_has_no_pins_and_parses_the_slot(self):
        lane = self._real_lane()
        assert lane.prep is None and lane.load is None
        assert lane.index == 2
        assert lane._load_state is True       # AFC's default with no load switch

    def test_unit_turns_it_into_a_cfs_lane(self):
        lane = self._real_lane()
        unit = _make_unit(MockAFC())
        unit.lanes[lane.name] = lane
        unit._setup_lanes()
        assert lane.custom_load_cmd == "AFC_CFS_TOOL_LOAD LANE=lane2"
        assert lane.custom_unload_cmd == "AFC_CFS_TOOL_UNLOAD LANE=lane2"
        assert lane._load_state is False
        assert lane.load_state is False       # empty shared path: virtual hub reads clear
