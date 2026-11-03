"""Unit tests for afcCFS RFID tag reading and lane state (extras/AFC_CFS.py)."""
from __future__ import annotations

import pytest

from extras import AFC_CFS as P
from tests.cfs_helpers import (
    CommandError,
    Recorder,
    RecTrace,
    connect,
    frame,
    gcmd,
    make_lane,
    make_unit,
    reset_buses,
)

# A real tag written for Generic PLA-Silk, pink, read from a K1 Max box
PINK_SILK = "AB1240276A21000020E7A18E0330000001000000"


@pytest.fixture(autouse=True)
def _fresh_buses():
    reset_buses()
    yield
    reset_buses()


def unit_with(values=None, lanes=(1,), printer=None, name="CFS_1"):
    unit = make_unit(name=name, values=values, printer=printer)
    made = [make_lane(unit, i) for i in lanes]
    connect(unit)
    unit.trace = RecTrace()
    unit.online = True
    for lane in made:
        lane._afc_prep_done = True
    return unit, made


def msgs(unit):
    return unit.logger.messages


def tag(**over):
    base = P.decode_rfid_record(PINK_SILK)
    base.update(over)
    return base


class PrintStats:
    def __init__(self, state="standby", raises=False):
        self.state = state
        self.raises = raises

    def get_status(self, eventtime):
        if self.raises:
            raise RuntimeError("not ready")
        return {"state": self.state}


# The command

class TestAfcCFSCmdReadTag:
    def _unit(self, values=None, found=None):
        unit, (lane,) = unit_with(values=values)
        unit.read_tag = Recorder(result=found)
        unit.afc.gcode.run_script_from_command.reset_mock()
        return unit, lane

    def test_tag_reported_and_applied(self):
        unit, lane = self._unit(found=tag())
        cmd = gcmd()
        unit.cmd_AFC_CFS_READ_TAG(lane, cmd)
        assert unit.read_tag.calls == [((lane,), {"spin": True})]
        assert [c.args[0] for c in cmd.respond_info.call_args_list] == [
            f"CFS lane1 (slot A) tag: PLA, #E7A18E, 1000 g\n  record {PINK_SILK}",
            "  SET_COLOR LANE=lane1 COLOR=E7A18E",
            "  SET_MATERIAL LANE=lane1 MATERIAL=PLA",
            "  SET_WEIGHT LANE=lane1 WEIGHT=1000",
        ]
        assert [c.args[0] for c in unit.afc.gcode.run_script_from_command.call_args_list] == [
            "SET_COLOR LANE=lane1 COLOR=E7A18E", "SET_MATERIAL LANE=lane1 MATERIAL=PLA",
            "SET_WEIGHT LANE=lane1 WEIGHT=1000"]

    def test_report_only(self):
        unit, lane = self._unit(found=tag())
        cmd = gcmd(APPLY=0)
        unit.cmd_AFC_CFS_READ_TAG(lane, cmd)
        assert cmd.respond_info.call_count == 1
        assert not unit.afc.gcode.run_script_from_command.called

    def test_no_tag(self):
        unit, lane = self._unit(found=None)
        cmd = gcmd()
        unit.cmd_AFC_CFS_READ_TAG(lane, cmd)
        cmd.respond_info.assert_called_once_with("CFS lane1 (slot A): no tag found")

    def test_spin_off_by_parameter(self):
        unit, lane = self._unit()
        unit.cmd_AFC_CFS_READ_TAG(lane, gcmd(SPIN=0))
        assert unit.read_tag.calls == [((lane,), {"spin": False})]

    def test_spin_off_by_config(self):
        unit, lane = self._unit({"rfid_spin": False})
        cmd = gcmd()
        unit.cmd_AFC_CFS_READ_TAG(lane, cmd)
        assert unit.read_tag.calls == [((lane,), {"spin": False})]
        cmd.get_int.assert_any_call("SPIN", 0, minval=0, maxval=1)

    def test_offline_box_connects(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = lambda: setattr(unit, "online", True) or True
        unit.cmd_AFC_CFS_READ_TAG(lane, gcmd())
        assert unit.read_tag.called

    def test_offline_box(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        with pytest.raises(CommandError) as e:
            unit.cmd_AFC_CFS_READ_TAG(lane, gcmd())
        assert str(e.value) == "CFS lane1: box is offline"
        assert not unit.read_tag.called

    def test_blocked(self):
        unit, lane = self._unit()
        lane.tool_loaded = True
        with pytest.raises(CommandError) as e:
            unit.cmd_AFC_CFS_READ_TAG(lane, gcmd())
        assert str(e.value) == "CFS lane1: can't read the tag now (lane is loaded)"
        assert not unit.read_tag.called


# Insert and remove

class TestAfcCFSOnFilamentInsert:
    def test_afc_event_and_no_read_by_default(self):
        unit, (lane,) = unit_with()
        seen = Recorder()
        unit.printer.register_event_handler("afc:lane_inserted", seen)
        unit.on_filament_insert(lane)
        assert seen.calls == [((lane,), {})]
        assert unit._rfid_queue == {}

    def test_queues_a_read(self):
        unit, (lane,) = unit_with({"rfid_read": True, "rfid_insert_delay": 7.0})
        unit.on_filament_insert(lane)
        assert unit._rfid_queue == {"lane1": 1007.0}


class TestAfcCFSOnFilamentRemove:
    def test_forgets_the_tag_and_queued_read(self):
        unit, (lane1, lane2) = unit_with(lanes=(1, 2))
        unit._rfid_queue = {"lane1": 5.0, "lane2": 6.0}
        unit.lane_tags = {"lane1": tag(), "lane2": None}
        unit.on_filament_remove(lane1)
        assert (unit._rfid_queue, unit.lane_tags) == ({"lane2": 6.0}, {"lane2": None})

    def test_nothing_known(self):
        unit, (lane,) = unit_with()
        unit.on_filament_remove(lane)
        assert (unit._rfid_queue, unit.lane_tags) == ({}, {})


class TestAfcCFSQueueStartupTagReads:
    def _unit(self, **values):
        opts = {"rfid_read_at_startup": True, "rfid_startup_delay": 15.0}
        opts.update(values)
        unit, lanes = unit_with(opts, lanes=(1, 2, 3))
        unit.material_mask = 0b0101
        return unit, lanes

    def test_queues_full_slots_once(self):
        unit, _ = self._unit()
        unit._queue_startup_tag_reads()
        assert unit._rfid_queue == {"lane1": 1015.0, "lane3": 1015.0}
        assert unit._rfid_startup_queued is True
        unit._rfid_queue.clear()
        unit._queue_startup_tag_reads()
        assert unit._rfid_queue == {}

    def test_off_by_default(self):
        unit, _ = self._unit(rfid_read_at_startup=False)
        unit._queue_startup_tag_reads()
        assert (unit._rfid_queue, unit._rfid_startup_queued) == ({}, False)

    def test_waits_for_presence(self):
        unit, _ = self._unit()
        unit.material_mask = None
        unit._queue_startup_tag_reads()
        assert (unit._rfid_queue, unit._rfid_startup_queued) == ({}, False)

    def test_no_lanes(self):
        unit, _ = self._unit()
        unit.lanes = {}
        unit._queue_startup_tag_reads()
        assert unit._rfid_startup_queued is False

    def test_waits_for_prep(self):
        unit, lanes = self._unit()
        lanes[1]._afc_prep_done = False
        unit._queue_startup_tag_reads()
        assert (unit._rfid_queue, unit._rfid_startup_queued) == ({}, False)


class TestAfcCFSQueueTagRead:
    def test_first_read_starts_the_timer(self):
        unit, (lane,) = unit_with()
        unit.queue_tag_read(lane, 10.0)
        timer = unit.reactor.timers[-1]
        assert (timer.callback, timer.waketime) == (unit._rfid_tick, 1010.0)
        assert unit._rfid_timer is timer
        assert unit._rfid_queue == {"lane1": 1010.0}

    def test_later_reads_move_the_timer_to_the_earliest(self):
        unit, (lane1, lane2) = unit_with(lanes=(1, 2))
        unit.queue_tag_read(lane1, 10.0)
        unit.queue_tag_read(lane2, 4.0)
        assert unit.reactor.updated == [(unit._rfid_timer, 1004.0)]
        assert unit._rfid_queue == {"lane1": 1010.0, "lane2": 1004.0}

    def test_no_delay(self):
        unit, (lane,) = unit_with()
        unit.queue_tag_read(lane)
        assert unit._rfid_queue == {"lane1": 1000.0}


class TestAfcCFSJobActive:
    @pytest.mark.parametrize("state,active", [("printing", True), ("paused", True),
                                              ("standby", False), ("complete", False)])
    def test_states(self, state, active):
        unit, _ = unit_with()
        unit.printer._objects["print_stats"] = PrintStats(state)
        assert unit._job_active() is active

    def test_no_print_stats(self):
        unit, _ = unit_with()
        assert unit._job_active() is False

    def test_stats_error(self):
        unit, _ = unit_with()
        unit.printer._objects["print_stats"] = PrintStats(raises=True)
        assert unit._job_active() is False


class TestAfcCFSTagReadBlocker:
    def _unit(self):
        unit, lanes = unit_with(lanes=(1, 2))
        unit.material_mask = 0b0011
        unit.connected_mask = 0
        return unit, lanes

    def test_free(self):
        unit, (lane, _) = self._unit()
        assert unit.tag_read_blocker(lane) is None

    def test_masks_unknown(self):
        unit, (lane, _) = self._unit()
        unit.material_mask = None
        unit.connected_mask = None
        assert unit.tag_read_blocker(lane) is None

    def test_printing(self):
        unit, (lane, _) = self._unit()
        unit.printer._objects["print_stats"] = PrintStats("printing")
        assert unit.tag_read_blocker(lane) == "a print is running"

    def test_offline(self):
        unit, (lane, _) = self._unit()
        unit.online = False
        assert unit.tag_read_blocker(lane) == "box offline"

    def test_box_running_a_sequence(self):
        unit, (lane, _) = self._unit()
        unit._operation_depth = 1
        assert unit.tag_read_blocker(lane) == "box busy"

    def test_line_busy(self):
        unit, (lane, _) = self._unit()
        unit.transport._mutex.locked = True
        assert unit.tag_read_blocker(lane) == "box busy"

    def test_lane_in_the_toolhead(self):
        unit, (lane, _) = self._unit()
        lane.tool_loaded = True
        assert unit.tag_read_blocker(lane) == "lane is loaded"

    def test_lane_past_the_merge(self):
        unit, (lane, _) = self._unit()
        unit.connected_mask = 0b0001
        assert unit.tag_read_blocker(lane) == "lane is loaded"

    def test_other_slot_past_the_merge(self):
        unit, (lane, _) = self._unit()
        unit.connected_mask = 0b0010
        assert unit.tag_read_blocker(lane) == "another slot of this box is loaded"

    def test_other_lane_in_the_toolhead(self):
        unit, (lane, other) = self._unit()
        other.tool_loaded = True
        assert unit.tag_read_blocker(lane) == "another slot of this box is loaded"

    def test_empty_slot(self):
        unit, (lane, _) = self._unit()
        unit.material_mask = 0b0010
        assert unit.tag_read_blocker(lane) == "slot is empty"


class TestAfcCFSRfidTick:
    def _unit(self):
        unit, lanes = unit_with(lanes=(1, 2))
        unit.material_mask = 0b0011
        unit.connected_mask = 0
        unit._rfid_timer = "timer"
        unit.read_tag = Recorder(result=None)
        unit.afc.gcode.run_script = Recorder()
        return unit, lanes

    def test_nothing_queued(self):
        unit, _ = self._unit()
        assert unit._rfid_tick(1000.0) == unit.reactor.NEVER
        assert unit._rfid_timer is None

    def test_not_due_yet(self):
        unit, _ = self._unit()
        unit._rfid_queue = {"lane1": 1010.0, "lane2": 1005.0}
        assert unit._rfid_tick(1000.0) == 1005.0
        assert not unit.read_tag.called

    def test_lane_gone(self):
        unit, _ = self._unit()
        unit._rfid_queue = {"lane9": 900.0, "lane1": 1010.0}
        assert unit._rfid_tick(1000.0) == 1000.0
        assert unit._rfid_queue == {"lane1": 1010.0}

    def test_reads_and_applies_the_tag(self):
        unit, (lane, _) = self._unit()
        unit.read_tag = Recorder(result=tag())
        unit._rfid_queue = {"lane1": 990.0}
        assert unit._rfid_tick(1000.0) == unit.reactor.NEVER
        assert unit.read_tag.calls == [((lane,), {})]
        assert [c[0][0] for c in unit.afc.gcode.run_script.calls] == [
            "SET_COLOR LANE=lane1 COLOR=E7A18E", "SET_MATERIAL LANE=lane1 MATERIAL=PLA",
            "SET_WEIGHT LANE=lane1 WEIGHT=1000"]
        assert (unit._rfid_queue, unit._rfid_timer) == ({}, None)

    def test_no_tag_nothing_applied(self):
        unit, _ = self._unit()
        unit._rfid_queue = {"lane1": 990.0, "lane2": 1020.0}
        assert unit._rfid_tick(1000.0) == 1020.0
        assert not unit.afc.gcode.run_script.called
        assert unit._rfid_timer == "timer"

    @pytest.mark.parametrize("setup", ["empty", "loaded"])
    def test_dropped_when_it_cannot_happen(self, setup):
        unit, (lane, _) = self._unit()
        if setup == "empty":
            unit.material_mask = 0b0010
        else:
            lane.tool_loaded = True
        unit._rfid_queue = {"lane1": 990.0}
        unit._rfid_tick(1000.0)
        assert unit._rfid_queue == {}
        assert not unit.read_tag.called

    def test_retried_later_when_busy(self):
        unit, _ = self._unit()
        unit.printer._objects["print_stats"] = PrintStats("printing")
        unit._rfid_queue = {"lane1": 990.0}
        assert unit._rfid_tick(1000.0) == 1010.0
        assert unit._rfid_queue == {"lane1": 1010.0}

    def test_read_error_is_a_warning(self):
        unit, _ = self._unit()
        unit.read_tag = Recorder(raises=RuntimeError("serial gone"))
        unit._rfid_queue = {"lane1": 990.0}
        unit._rfid_tick(1000.0)
        level, text = msgs(unit)[-1]
        assert level == "warning"
        assert text.startswith("CFS lane1: tag read failed\nTraceback")
        assert text.rstrip().endswith("RuntimeError: serial gone")


class TestAfcCFSReadTag:
    def _unit(self, values=None):
        unit, lanes = unit_with(values=values, lanes=(1, 4))
        unit.wait_for_tag_reads = Recorder()
        return unit, lanes[1]

    def test_turns_the_slot_and_reads(self):
        unit, lane = self._unit()
        busy = []
        unit.box.on(P.SET_PRE_LOADING, lambda req: (busy.append(unit._rfid_busy),
                                                    frame(P.SET_PRE_LOADING))[1])
        unit.box.reply(P.GET_RFID, frame(P.GET_RFID, b"D:" + PINK_SILK.encode() + b";"))
        found = unit.read_tag(lane)
        assert found == P.decode_rfid_record(PINK_SILK)
        assert unit.box.keys() == [(1, P.SET_PRE_LOADING, b"\x08\x02"),
                                   (1, P.GET_RFID, b"\x08"),
                                   (1, P.SET_PRE_LOADING, b"\x0f\x00")]
        assert unit.box.sent[0].kwargs == {"timeout": 150.0, "retries": 0, "motor": True,
                                           "quiet": False}
        assert unit.box.sent[1].kwargs["timeout"] == 2.0
        assert busy == [True, True]
        assert unit._rfid_busy is False
        assert unit.lane_tags["lane4"] == found
        assert unit.trace.events == [("rfid", {"lane": "lane4", "slot": "D", "spin": True,
                                               "replies": [f"D:{PINK_SILK};"], "tag": found})]
        assert msgs(unit) == [("info", "CFS lane4: slot D tag: PLA, #E7A18E, 1000 g")]
        assert unit._operation_depth == 0

    def test_without_turning(self):
        unit, lane = self._unit()
        unit.box.reply(P.GET_RFID, frame(P.GET_RFID, b"D:" + PINK_SILK.encode() + b";"))
        unit.read_tag(lane, spin=False)
        assert unit.box.fns() == [P.GET_RFID]

    def test_turning_off_by_config(self):
        unit, lane = self._unit({"rfid_spin": False})
        unit.read_tag(lane)
        assert P.SET_PRE_LOADING not in unit.box.fns()

    def test_no_tag_after_every_try(self):
        unit, lane = self._unit({"rfid_read_attempts": 3, "rfid_retry_interval": 5.0})
        unit.box.reply(P.GET_RFID, frame(P.GET_RFID, b"D:unknown;"), None,
                       frame(P.GET_RFID, b"D:unknown;"))
        assert unit.read_tag(lane) is None
        assert unit.box.fns().count(P.GET_RFID) == 3
        assert unit.reactor.pauses == [1005.0, 1010.0]          # none after the last try
        assert unit.lane_tags["lane4"] is None
        assert unit.trace.events[0][1]["replies"] == ["D:unknown;", "D:unknown;"]
        assert msgs(unit) == [("debug", "CFS lane4: slot D tag: none")]

    def test_tag_on_a_retry(self):
        unit, lane = self._unit()
        unit.box.reply(P.GET_RFID, frame(P.GET_RFID, b"D:unknown;"),
                       frame(P.GET_RFID, b"D:" + PINK_SILK.encode() + b";"))
        assert unit.read_tag(lane) is not None
        assert unit.box.fns().count(P.GET_RFID) == 2

    def test_extra_material_codes_used(self):
        unit, lane = self._unit({"rfid_materials": "00002=PLA-SILK"})
        unit.box.reply(P.GET_RFID, frame(P.GET_RFID, b"D:" + PINK_SILK.encode() + b";"))
        assert unit.read_tag(lane)["material"] == "PLA-SILK"

    def test_busy_flag_cleared_on_error(self):
        unit, lane = self._unit()
        unit.box.on(P.GET_RFID, lambda req: (_ for _ in ()).throw(RuntimeError("gone")))
        with pytest.raises(RuntimeError):
            unit.read_tag(lane)
        assert unit._rfid_busy is False
        assert unit._operation_depth == 0


class TestAfcCFSTagSummary:
    def test_none(self):
        assert P.afcCFS._tag_summary(None) == "none"

    def test_full(self):
        assert P.afcCFS._tag_summary(tag(spool_id=7)) == "PLA, #E7A18E, 1000 g, Spoolman spool 7"

    def test_unknown_material_and_nothing_else(self):
        assert P.afcCFS._tag_summary(tag(material=None, color=None, weight=None)) == (
            "material code 00002")


class TestAfcCFSApplyTag:
    def _unit(self):
        unit, (lane,) = unit_with()
        return unit, lane, Recorder()

    def test_color_material_weight(self):
        unit, lane, run = self._unit()
        cmds = unit.apply_tag(lane, tag(), run)
        assert cmds == ["SET_COLOR LANE=lane1 COLOR=E7A18E",
                        "SET_MATERIAL LANE=lane1 MATERIAL=PLA",
                        "SET_WEIGHT LANE=lane1 WEIGHT=1000"]
        assert [c[0][0] for c in run.calls] == cmds
        assert msgs(unit) == []

    def test_spoolman_id_wins(self):
        unit, lane, run = self._unit()
        unit.afc.spoolman = "http://spoolman"
        assert unit.apply_tag(lane, tag(spool_id=7), run) == [
            "SET_SPOOL_ID LANE=lane1 SPOOL_ID=7"]

    def test_spool_id_without_spoolman(self):
        unit, lane, run = self._unit()
        assert unit.apply_tag(lane, tag(spool_id=7), run)[0] == "SET_COLOR LANE=lane1 COLOR=E7A18E"

    def test_lane_already_has_a_spool(self):
        unit, lane, run = self._unit()
        lane.spool_id = 1
        assert unit.apply_tag(lane, tag(), run) == []
        assert not run.called
        assert msgs(unit) == [("info", "CFS lane1: lane has Spoolman spool 1; tag not applied")]

    def test_unknown_material_code(self):
        unit, lane, run = self._unit()
        cmds = unit.apply_tag(lane, tag(material=None, material_code="09999"), run)
        assert cmds == ["SET_COLOR LANE=lane1 COLOR=E7A18E", "SET_WEIGHT LANE=lane1 WEIGHT=1000"]
        assert msgs(unit) == [("info", "CFS lane1: material code 09999 isn't known; add it to "
                                       "rfid_materials in [AFC_CFS CFS_1], e.g. rfid_materials: "
                                       "09999=PETG")]

    def test_nothing_usable(self):
        unit, lane, run = self._unit()
        empty = tag(material=None, material_code="", color=None, weight=None)
        assert unit.apply_tag(lane, empty, run) == []
        assert msgs(unit) == []


class TestAfcCFSApplyLaneState:
    def _unit(self):
        unit, (lane,) = unit_with()
        for name in ("lane_not_ready", "lane_loaded", "lane_tool_loaded",
                     "lane_tool_loaded_idle", "ensure_print_mode"):
            setattr(unit, name, Recorder())
        return unit, lane

    def test_empty_slot(self):
        unit, lane = self._unit()
        unit.connected_mask = 0
        assert unit.apply_lane_state(lane, False) == "EMPTY READY FOR SPOOL"
        assert (lane.prep_state, lane.loaded_to_hub, lane._load_state) == (False, False, False)
        assert unit.lane_not_ready.calls == [((lane,), {})]
        assert not unit.lane_loaded.called

    def test_spool_in_the_slot(self):
        unit, lane = self._unit()
        unit.connected_mask = 0b0001
        assert unit.apply_lane_state(lane, True) == "FILAMENT PRESENT"
        assert (lane.prep_state, lane._load_state) == (True, True)
        assert unit.lane_loaded.calls == [((lane,), {})]
        assert not unit.ensure_print_mode.called

    def test_current_tool(self):
        unit, lane = self._unit()
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = "lane1"
        unit.afc.current = "lane1"
        assert unit.apply_lane_state(lane, True) == "FILAMENT PRESENT in ToolHead"
        assert unit.ensure_print_mode.calls == [((lane,), {})]
        assert unit.lane_tool_loaded.called and not unit.lane_tool_loaded_idle.called

    def test_loaded_but_not_the_current_tool(self):
        unit, lane = self._unit()
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = "lane1"
        unit.afc.current = "lane5"
        assert unit.apply_lane_state(lane, True) == "FILAMENT PRESENT in ToolHead"
        assert unit.lane_tool_loaded_idle.called and not unit.lane_tool_loaded.called

    def test_extruder_holds_another_lane(self):
        unit, lane = self._unit()
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = "lane2"
        assert unit.apply_lane_state(lane, True) == "FILAMENT PRESENT"
        assert not unit.ensure_print_mode.called

    def test_no_extruder(self):
        unit, lane = self._unit()
        lane.tool_loaded = True
        lane.extruder_obj = None
        assert unit.apply_lane_state(lane, True) == "FILAMENT PRESENT"
        assert not unit.ensure_print_mode.called
