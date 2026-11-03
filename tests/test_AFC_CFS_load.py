"""Unit tests for afcCFS tool load, cut, unload and eject (extras/AFC_CFS.py)."""
from __future__ import annotations

import struct

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
    make_printer,
    make_unit,
    reset_buses,
)


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
    unit.wait_for_tag_reads = Recorder()
    return unit, made


def wheel(mm):
    """Stage-5 reply payload: the measuring wheel as a big-endian float."""
    return struct.pack(">f", mm)


def msgs(unit):
    return unit.logger.messages


def sensor(lane, state=True):
    lane.extruder_obj.tool_start_state = state


# Stage 5

class TestAfcCFSStage5Feed:
    def _unit(self, values=None):
        unit, (lane,) = unit_with(values=values)
        unit.load_reports["lane1"] = {}
        return unit, lane

    def test_sensor_already_triggered(self):
        unit, lane = self._unit()
        sensor(lane)
        assert unit._stage5_feed(lane) == (True, "toolhead sensor triggered after 0 polls")
        assert unit.box.sent == []

    def test_feeds_until_the_sensor_triggers(self):
        unit, lane = self._unit()
        positions = iter([10.0, 30.0, 50.0])
        unit.box.on(P.EXTRUDE_PROCESS,
                    lambda req: frame(P.EXTRUDE_PROCESS, wheel(next(positions))))
        unit.reactor.on_pause.append(lambda now: sensor(lane, len(unit.box.sent) >= 3))
        assert unit._stage5_feed(lane) == (True, "toolhead sensor triggered after 3 polls")
        assert unit.box.keys() == [(1, P.EXTRUDE_PROCESS, b"\x01\x05\x00")] * 3
        assert unit.reactor.pauses == pytest.approx([1000.4, 1000.8, 1001.2])
        assert unit.load_reports["lane1"] == {"stage5_polls": 3, "wheel": 50.0}
        assert msgs(unit) == []

    def test_times_out(self):
        unit, lane = self._unit({"stage5_timeout": 5})
        assert unit._stage5_feed(lane) == (
            False, "timed out after 5s without reaching the toolhead sensor")
        assert len(unit.box.sent) == 13                     # polls every 0.4 s until > 5 s

    def test_error_status_logged(self):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, frame(P.EXTRUDE_PROCESS, status=P.ST_EXTRUDE_ERR4))
        unit.reactor.on_pause.append(lambda now: sensor(lane))
        unit._stage5_feed(lane)
        assert msgs(unit) == [("debug", "CFS lane1: stage 5 status EXTRUDE_ERR4")]

    def test_update_state_status_not_logged(self):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, frame(P.EXTRUDE_PROCESS, status=P.ST_UPDATE_STATE))
        unit.reactor.on_pause.append(lambda now: sensor(lane))
        unit._stage5_feed(lane)
        assert msgs(unit) == []

    def test_missed_reply_skipped(self):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, None)
        unit.reactor.on_pause.append(lambda now: sensor(lane))
        assert unit._stage5_feed(lane) == (True, "toolhead sensor triggered after 1 polls")
        assert unit.load_reports["lane1"] == {"stage5_polls": 1, "wheel": None}

    def test_wheel_stall(self):
        unit, lane = self._unit()
        unit.box.on(P.EXTRUDE_PROCESS, lambda req: frame(P.EXTRUDE_PROCESS, wheel(42.0)))
        assert unit._stage5_feed(lane) == (False, "measuring wheel stalled at 42.0mm")
        # The stall clock starts at the first reading; it fails once > 8 s pass without movement
        assert unit.reactor.now - 1000.0 == pytest.approx(8.4)

    def test_small_creep_is_still_a_stall(self):
        unit, lane = self._unit()
        # 0.05 mm per 0.4 s poll: under stage5_stall_distance (2 mm) for longer than 8 s
        creep = iter(42.0 + 0.05 * i for i in range(1000))
        unit.box.on(P.EXTRUDE_PROCESS, lambda req: frame(P.EXTRUDE_PROCESS, wheel(next(creep))))
        ok, detail = unit._stage5_feed(lane)
        assert ok is False
        assert detail.startswith("measuring wheel stalled at ")

    def test_stall_but_sensor_just_triggered(self):
        unit, lane = self._unit()
        replied = []

        def poll(req):
            replied.append(True)
            return frame(P.EXTRUDE_PROCESS, wheel(42.0))
        unit.box.on(P.EXTRUDE_PROCESS, poll)
        unit.reactor.on_pause.append(lambda now: replied.clear())
        # The filament reaches the sensor just as the wheel looks stalled: seen only at the
        # stall check, after that poll's reply and before the next pause
        unit._toolhead_sensor = (lambda checked_lane: bool(replied)
                                 and unit.reactor.now - 1000.0 > 8.3)
        assert unit._stage5_feed(lane) == (True, "toolhead sensor triggered after 22 polls")


class TestAfcCFSBoxCleanup:
    def test_stops_the_motors(self):
        unit, _ = unit_with()
        unit._box_cleanup()
        assert unit.box.keys() == [(1, P.TIGHTEN_UP_ENABLE, b"\x00"),
                                   (1, P.CTRL_CONNECTION_MOTOR_ACTION, b"\x00"),
                                   (1, P.SET_BOX_MODE, b"\x00\x01")]


# One load attempt

LOAD_START = [
    (1, P.SET_PRE_LOADING, b"\x0f\x00"),
    (1, P.GET_BOX_STATE, b""),
    (1, P.SET_BOX_MODE, b"\x00\x01"),
    (1, P.CTRL_CONNECTION_MOTOR_ACTION, b"\x01"),
    (1, P.TIGHTEN_UP_ENABLE, b"\x01"),
]
CLEANUP = [
    (1, P.TIGHTEN_UP_ENABLE, b"\x00"),
    (1, P.CTRL_CONNECTION_MOTOR_ACTION, b"\x00"),
    (1, P.SET_BOX_MODE, b"\x00\x01"),
]


class TestAfcCFSLoadAttempt:
    def _unit(self, values=None, reached=(True, "toolhead sensor triggered after 4 polls")):
        unit, (lane,) = unit_with(values=values)
        unit.load_reports["lane1"] = {"stage5_polls": 4, "wheel": 87.5}

        def stage5(feeding_lane):
            sensor(lane, reached[0])
            return reached
        unit._stage5_feed = stage5
        return unit, lane

    def test_full_sequence(self):
        unit, lane = self._unit()
        assert unit._load_attempt(lane, 0) == (True, "loaded")
        state = (1, P.GET_BOX_STATE, b"")
        assert unit.box.keys() == LOAD_START + [
            (1, P.EXTRUDE_PROCESS, b"\x01\x00\x00"),
            (1, P.EXTRUDE_PROCESS, b"\x01\x04\x00"),
            state,
            state,
            (1, P.EXTRUDE_PROCESS, b"\x01\x06\x00"),
            state,
            state,
            (1, P.EXTRUDE_PROCESS, b"\x01\x07\x03"),
            state,
            (1, P.SET_BOX_MODE, b"\x01\x00"),
            (1, P.TIGHTEN_UP_ENABLE, b"\x00"),
            (1, P.CTRL_CONNECTION_MOTOR_ACTION, b"\x00"),
            state,
        ]
        # Prime 1 (10 mm at 300 mm/min) before stage 6, prime 2 (5 at 600) before stage 7
        assert unit.afc.move_e_pos.calls == [
            ((10.0, 300.0 / 60, "CFS prime 1"), {"wait_tool": True}),
            ((5.0, 600.0 / 60, "CFS prime 2"), {"wait_tool": True}),
        ]
        assert unit.load_reports["lane1"]["stage6"] == "OK"
        assert unit.load_reports["lane1"]["stage7"] == "OK"
        assert unit.trace.events == [
            ("stage5_result", {"lane": "lane1", "slot": "A", "reached": True,
                               "detail": "toolhead sensor triggered after 4 polls", "attempt": 1,
                               "polls": 4, "wheel": 87.5, "seconds": 0.0}),
        ] + [
            ("box_state", {"lane": "lane1", "slot": "A", "point": point, "status": "OK",
                           "toolhead_sensor": True})
            for point in ("toolhead", "prime1", "stage6", "prime2", "stage7")
        ] + [
            ("handoff", {"lane": "lane1", "slot": "A", "attempt": 1, "stage6": "OK",
                         "stage7": "OK", "stage7_arg": 3, "toolhead_sensor": True}),
            ("box_state", {"lane": "lane1", "slot": "A", "point": "print_mode", "status": "OK",
                           "toolhead_sensor": True}),
        ]
        assert msgs(unit) == [("debug", "CFS lane1: stage 5 toolhead sensor triggered after 4 "
                                        "polls")]

    def test_latched_error_before_load_logged(self):
        unit, lane = self._unit()
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, status=P.ST_EXTRUDE_ERR8))
        unit._load_attempt(lane, 0)
        assert msgs(unit)[0] == ("debug", "CFS lane1: box reported EXTRUDE_ERR8 before load, "
                                          "clearing")

    @pytest.mark.parametrize("status", [None, P.ST_UPDATE_STATE])
    def test_quiet_box_states_not_logged(self, status):
        unit, lane = self._unit()
        unit.box.reply(P.GET_BOX_STATE,
                       None if status is None else frame(P.GET_BOX_STATE, status=status))
        unit._load_attempt(lane, 0)
        assert not any("before load" in m for _, m in msgs(unit))

    def test_connection_motor_silent(self):
        unit, lane = self._unit()
        unit.box.reply(P.CTRL_CONNECTION_MOTOR_ACTION, None)
        assert unit._load_attempt(lane, 0) == (
            False, "box did not answer CTRL_CONNECTION_MOTOR_ACTION")
        assert unit.box.fns()[-1] == P.CTRL_CONNECTION_MOTOR_ACTION

    @pytest.mark.parametrize("stage,before", [(0x00, 0), (0x04, 1)])
    def test_stage_no_reply(self, stage, before):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, *([frame(P.EXTRUDE_PROCESS)] * before + [None]))
        assert unit._load_attempt(lane, 0) == (False, f"no reply to EXTRUDE_PROCESS stage {stage}")
        assert unit.box.keys()[-3:] == CLEANUP

    @pytest.mark.parametrize("stage,before", [(0x00, 0), (0x04, 1)])
    def test_stage_error(self, stage, before):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, *([frame(P.EXTRUDE_PROCESS)] * before
                                            + [frame(P.EXTRUDE_PROCESS, status=P.ST_STATE_ERR)]))
        assert unit._load_attempt(lane, 0) == (
            False, f"EXTRUDE_PROCESS stage {stage} returned STATE_ERR")
        assert unit.box.keys()[-3:] == CLEANUP

    def test_stage5_short_pulls_the_feed_back(self):
        unit, lane = self._unit(reached=(False, "measuring wheel stalled at 42.0mm"))
        assert unit._load_attempt(lane, 1) == (False, "measuring wheel stalled at 42.0mm")
        assert unit.box.keys()[-6:] == CLEANUP + [
            (1, P.RETRUDE_PROCESS, b"\x01\x00"),
            (1, P.RETRUDE_PROCESS, b"\x01\x01"),
            (1, P.SET_BOX_MODE, b"\x00\x01"),
        ]
        assert unit.trace.events[0][1]["attempt"] == 2
        assert not unit.afc.move_e_pos.called

    def test_no_prime_moves(self):
        unit, lane = self._unit({"prime1_length": 0, "prime2_length": 0})
        unit._load_attempt(lane, 0)
        assert not unit.afc.move_e_pos.called

    def test_stage6_and_7_silent(self):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, frame(P.EXTRUDE_PROCESS), frame(P.EXTRUDE_PROCESS),
                       None, None)
        assert unit._load_attempt(lane, 0) == (True, "loaded")
        assert (unit.load_reports["lane1"]["stage6"], unit.load_reports["lane1"]["stage7"]) == (
            "NO_REPLY", "NO_REPLY")
        assert ("warning", "CFS lane1: stage 7 returned NO_REPLY") in msgs(unit)

    def test_stage7_error_status(self):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, frame(P.EXTRUDE_PROCESS), frame(P.EXTRUDE_PROCESS),
                       frame(P.EXTRUDE_PROCESS), frame(P.EXTRUDE_PROCESS,
                                                       status=P.ST_EXTRUDE_ERR10))
        unit._load_attempt(lane, 0)
        assert msgs(unit)[-1] == ("warning", "CFS lane1: stage 7 returned EXTRUDE_ERR10")

    def test_clear_mode_after_load(self):
        unit, lane = self._unit({"clear_mode_after_load": True})
        unit._load_attempt(lane, 0)
        assert unit.box.keys()[-2:] == [(1, P.SET_BOX_MODE, b"\x00\x01"),
                                         (1, P.GET_BOX_STATE, b"")]

    def test_sensor_lost_after_handoff(self):
        unit, lane = self._unit()
        orig = unit.set_box_mode

        def drop_sensor(mode, bits=0):
            if mode == P.MODE_PRINT:
                sensor(lane, False)
            return orig(mode, bits)
        unit.set_box_mode = drop_sensor
        assert unit._load_attempt(lane, 0) == (False, "toolhead sensor cleared after the handoff")


# The load command

class TestAfcCFSCmdToolLoad:
    def _unit(self, values=None, lanes=(1,)):
        unit, made = unit_with(values=values, lanes=lanes)
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, frame(P.GET_FILAMENT_SENSOR_STATE, b"\x0f"))
        lane = made[0]
        lane.select_lane = Recorder()
        unit.afc._check_extruder_temp.reset_mock()
        unit._mark_tool_state = Recorder()
        unit.attempts = []

        def attempt(loading_lane, n):
            result = unit.attempt_results.pop(0)
            unit.attempts.append(n)
            if isinstance(result, BaseException):
                raise result
            if result[0]:
                sensor(loading_lane)
            return result
        unit.attempt_results = [(True, "loaded")]
        unit._load_attempt = attempt
        return unit, lane

    def test_loads(self):
        unit, lane = self._unit()
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.wait_for_tag_reads.called
        assert lane.select_lane.called
        unit.afc._check_extruder_temp.assert_called_once_with(lane)
        assert unit.attempts == [0]
        # tool_stn 60 minus the 15 mm the primes already pushed
        assert unit.afc.move_e_pos.calls == [((45.0, 10.0, "CFS tool_stn"), {"wait_tool": True})]
        assert lane._load_state is True
        assert unit._mark_tool_state.calls == [((lane,), {"loaded": True})]
        assert unit.load_reports["lane1"] == {"slot": "A", "result": "loaded"}
        assert unit.trace.events[0] == ("load_start", {"lane": "lane1", "slot": "A",
                                                       "toolhead_sensor": False})
        assert unit.trace.events[-1] == ("load_end", {"lane": "lane1", "slot": "A", "ok": True,
                                                      "detail": "loaded", "attempts": 1,
                                                      "tool_stn_push": 45.0, "seconds": 0.0})
        assert msgs(unit) == [("debug", "CFS lane1: loading slot A, try 1/2")]
        assert unit._operation_depth == 0
        assert not unit.afc.error.handle_lane_failure.called

    def test_short_tool_stn_no_push(self):
        unit, lane = self._unit()
        lane.extruder_obj.tool_stn = 12.0
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert not unit.afc.move_e_pos.called
        assert unit.trace.events[-1][1]["tool_stn_push"] == 0

    def test_parks_first(self):
        unit, lane = self._unit()
        unit.afc.park = True
        unit.afc.park_cmd = "AFC_CFS_PARK"
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        unit.afc.gcode.run_script_from_command.assert_called_once_with(
            "AFC_CFS_PARK EXTRUDER=extruder")

    def test_park_on_without_a_macro(self):
        unit, lane = self._unit()
        unit.afc.park = True
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert not unit.afc.gcode.run_script_from_command.called

    def test_park_macro_but_park_off(self):
        unit, lane = self._unit()
        unit.afc.park_cmd = "AFC_CFS_PARK"
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert not unit.afc.gcode.run_script_from_command.called

    def _failed(self, unit, lane, message):
        unit.afc.error.handle_lane_failure.assert_called_once_with(lane, f"CFS lane1: {message}")
        assert msgs(unit)[-1] == ("error", f"CFS lane1 (slot A): {message}")

    def test_offline_box_connects(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=True)
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.connect.called
        assert unit.attempts == [0]

    def test_offline_box_wont_answer(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        unit.transport.last_error = "open /dev/fake_cfs failed: gone"
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        self._failed(unit, lane, "box is offline (open /dev/fake_cfs failed: gone)")
        assert unit.attempts == []

    def test_offline_box_no_error_text(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        self._failed(unit, lane, "box is offline (no reply)")

    def test_cutter_engaged(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1"})
        unit.cutter_sensor_state = True
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        self._failed(unit, lane, "cutter sensor is engaged; reset the cutter before loading")

    def test_toolhead_already_loaded(self):
        unit, lane = self._unit()
        sensor(lane)
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        self._failed(unit, lane, "toolhead sensor already sees filament; unload the other lane "
                                 "first")

    def test_empty_slot(self):
        unit, lane = self._unit(lanes=(2,))
        unit.box._queues.clear()
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, frame(P.GET_FILAMENT_SENSOR_STATE, b"\x01"))
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        unit.afc.error.handle_lane_failure.assert_called_once_with(
            lane, "CFS lane2: no filament in slot B")
        assert unit.attempts == []

    def test_presence_unknown_still_loads(self):
        unit, lane = self._unit()
        unit.box._queues.clear()
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, None)
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.attempts == [0]

    def test_retries_then_loads(self):
        unit, lane = self._unit()
        unit.attempt_results = [(False, "measuring wheel stalled at 42.0mm"), (True, "loaded")]
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.attempts == [0, 1]
        assert ("warning", "CFS lane1: load attempt 1 failed: measuring wheel stalled at "
                           "42.0mm") in msgs(unit)
        assert ("load_attempt_failed", {"lane": "lane1", "slot": "A", "attempt": 1,
                                        "detail": "measuring wheel stalled at 42.0mm",
                                        "toolhead_sensor": False}) in unit.trace.events
        assert unit._mark_tool_state.called

    def test_stuck_in_the_toolhead_stops_retrying(self):
        unit, lane = self._unit()

        def stuck(loading_lane, n):
            unit.attempts.append(n)
            sensor(loading_lane)
            return False, "toolhead sensor cleared after the handoff"
        unit._load_attempt = stuck
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.attempts == [0]
        self._failed(unit, lane, "load failed after 1 attempt(s): toolhead sensor cleared after "
                                 "the handoff")

    def test_every_attempt_fails(self):
        unit, lane = self._unit()
        unit.attempt_results = [(False, "a"), (False, "timed out after 60s")]
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.attempts == [0, 1]
        assert unit.load_reports["lane1"]["result"] == "timed out after 60s"
        assert unit.trace.events[-2] == ("load_end", {"lane": "lane1", "slot": "A", "ok": False,
                                                      "detail": "timed out after 60s",
                                                      "attempts": 2, "seconds": 0.0})
        self._failed(unit, lane, "load failed after 2 attempt(s): timed out after 60s")
        assert not unit._mark_tool_state.called
        assert lane._load_state is False

    def test_attempt_error_cleans_up(self):
        unit, lane = self._unit({"load_attempts": 1})
        unit.attempt_results = [RuntimeError("serial gone")]
        unit.box.clear()
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.box.keys()[-3:] == CLEANUP
        debug = [m for lvl, m in msgs(unit) if lvl == "debug" and m.startswith("Traceback")]
        assert len(debug) == 1 and debug[0].rstrip().endswith("RuntimeError: serial gone")
        self._failed(unit, lane, "load failed after 1 attempt(s): error: serial gone")


# Cut

class TestAfcCFSRunCutterSequence:
    def _unit(self, values=None, printer=None, name="CFS_1"):
        unit, (lane,) = unit_with(values=values, printer=printer, name=name)
        unit.afc.tool_cut = True
        unit.afc.toolhead.wait_moves.reset_mock()
        return unit, lane

    @staticmethod
    def _cut_events(unit):
        return [e for e in unit.trace.events if e[0] == "cut"]

    def _cuts(self, unit, *switch_states):
        """Each cut moves the cutter switch through these states."""
        cutter = unit._cutter()
        plans = list(switch_states)

        def cut(cmd):
            for state in plans.pop(0) if plans else ():
                cutter._cutter_callback(0.0, state)
        unit.afc.gcode.run_script_from_command.side_effect = cut

    def test_cutting_off(self):
        unit, lane = self._unit()
        unit.afc.tool_cut = False
        assert unit._run_cutter_sequence(lane) is None
        assert not unit.afc.gcode.run_script_from_command.called

    def test_no_switch_cut_once_unverified(self):
        unit, lane = self._unit()
        assert unit._run_cutter_sequence(lane) is None
        unit.afc.gcode.run_script_from_command.assert_called_once_with("AFC_CUT EXTRUDER=extruder")
        assert lane.extruder_obj.estats.increase_cut_total.call_count == 1
        assert unit.trace.events == [("cut", {"lane": "lane1", "slot": "A", "attempt": 1,
                                              "verified": False})]
        assert not unit.afc.toolhead.wait_moves.called

    def test_switch_fired_and_released(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1"})
        self._cuts(unit, (1, 0))
        assert unit._run_cutter_sequence(lane) is None
        assert unit.afc.toolhead.wait_moves.called
        assert self._cut_events(unit) == [("cut", {"lane": "lane1", "slot": "A", "attempt": 1,
                                              "verified": True, "fired": True, "released": True,
                                              "seconds": 0.1})]
        assert msgs(unit) == []

    def test_switch_releases_while_waiting(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1"})
        self._cuts(unit, (1,))
        unit.reactor.on_pause.append(
            lambda now: unit._cutter_callback(0.0, 0) if len(unit.reactor.pauses) == 3 else None)
        assert unit._run_cutter_sequence(lane) is None
        assert self._cut_events(unit)[-1][1]["released"] is True

    def test_switch_never_released(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1", "cutter_wait_time": 0.2})
        self._cuts(unit, (1,))
        reason = "cutter sensor did not release after cutting"
        assert unit._run_cutter_sequence(lane) == reason
        # load_attempts (2) + 1 cuts, each one warned
        assert msgs(unit) == [("warning", f"CFS lane1: cut {n} not confirmed: {reason}")
                              for n in (1, 2, 3)]
        assert unit.afc.gcode.run_script_from_command.call_count == 3

    def test_switch_never_fired(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1"})
        reason = ("cutter switch never fired during the cut (blade did not reach the end of its "
                  "travel; check pin_loc_xy / cut_move_dist)")
        assert unit._run_cutter_sequence(lane) == reason
        assert msgs(unit)[0] == ("warning", f"CFS lane1: cut 1 not confirmed: {reason}")

    def test_fired_on_a_retry(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1"})
        self._cuts(unit, (), (1, 0))
        assert unit._run_cutter_sequence(lane) is None
        assert [e[1]["fired"] for e in self._cut_events(unit)] == [False, True]

    def test_trigger_not_required(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1", "cutter_require_trigger": False})
        assert unit._run_cutter_sequence(lane) is None
        assert self._cut_events(unit)[-1][1]["fired"] is False

    def test_uses_the_chains_switch(self):
        printer = make_printer()
        a, _ = self._unit({"cutter_sensor_pin": "PA1"}, printer=printer)
        b, lane = self._unit({"box_addr": 2}, printer=printer, name="CFS_2")
        b.cutter_source = a
        self._cuts(b, (1, 0))
        assert b._run_cutter_sequence(lane) is None
        assert a.cutter_trigger_count == 1


class TestAfcCFSRunPostCutMacros:
    def test_nothing_enabled(self):
        unit, (lane,) = unit_with()
        unit._run_post_cut_macros(lane)
        assert not unit.afc.gcode.run_script_from_command.called

    def test_park(self):
        unit, (lane,) = unit_with()
        unit.afc.park = True
        unit.afc.park_cmd = "AFC_CFS_PARK"
        unit._run_post_cut_macros(lane)
        unit.afc.gcode.run_script_from_command.assert_called_once_with(
            "AFC_CFS_PARK EXTRUDER=extruder")

    def test_park_without_macro(self):
        unit, (lane,) = unit_with()
        unit.afc.park = True
        unit._run_post_cut_macros(lane)
        assert not unit.afc.gcode.run_script_from_command.called

    def test_afc_tip_forming(self):
        unit, (lane,) = unit_with()
        unit.afc.form_tip = True
        unit.afc.form_tip_cmd = "AFC"
        tip = type("Tip", (), {"tip_form": Recorder()})()
        unit.printer._objects["AFC_form_tip"] = tip
        unit._run_post_cut_macros(lane)
        assert tip.tip_form.called
        assert not unit.afc.gcode.run_script_from_command.called

    def test_custom_tip_macro(self):
        unit, (lane,) = unit_with()
        unit.afc.form_tip = True
        unit.afc.form_tip_cmd = "MY_TIP"
        unit._run_post_cut_macros(lane)
        unit.afc.gcode.run_script_from_command.assert_called_once_with("MY_TIP")

    def test_tip_forming_without_macro(self):
        unit, (lane,) = unit_with()
        unit.afc.form_tip = True
        unit._run_post_cut_macros(lane)
        assert not unit.afc.gcode.run_script_from_command.called


# Retract to the box

RETRACT_START = [(1, P.SET_PRE_LOADING, b"\x0f\x00"), (1, P.SET_BOX_MODE, b"\x00\x01"),
                 (1, P.RETRUDE_PROCESS, b"\x01\x00")]


class TestAfcCFSRetractToBox:
    def _unit(self, values=None):
        unit, (lane,) = unit_with(values=values)
        sensor(lane)

        def finish(req):
            if req.data == b"\x01\x01":
                unit.reactor.now += 12.4                  # the reel-back takes a while
                sensor(lane, False)
            return frame(P.RETRUDE_PROCESS)
        unit.box.on(P.RETRUDE_PROCESS, finish)
        return unit, lane

    def test_unloads(self):
        unit, lane = self._unit()
        assert unit._retract_to_box(lane) == (True, "reeled back in 12.4s")
        assert unit.box.keys() == RETRACT_START + [(1, P.RETRUDE_PROCESS, b"\x01\x01"),
                                                   (1, P.SET_BOX_MODE, b"\x00\x01")]
        assert unit.afc.move_e_pos.calls == [
            ((-15.0, 10.0, "CFS unload retract 1"), {"wait_tool": True}),
            ((-45.0, 30.0, "CFS unload retract 2"), {"wait_tool": True}),
        ]
        assert unit.trace.events == [
            ("retrude_start", {"lane": "lane1", "slot": "A", "status": "OK", "seconds": 0.0,
                               "toolhead_sensor": True}),
            ("retract_step", {"lane": "lane1", "slot": "A", "step": 1, "mm": 15.0,
                              "speed": 600.0, "toolhead_sensor": True}),
            ("retract_step", {"lane": "lane1", "slot": "A", "step": 2, "mm": 45.0,
                              "speed": 1800.0, "toolhead_sensor": True}),
            ("reel", {"lane": "lane1", "slot": "A", "done": True, "replies": ["OK", "OK"],
                      "seconds": 12.4, "toolhead_sensor": False}),
        ]
        assert msgs(unit) == []

    def test_tip_wiggle(self):
        unit, lane = self._unit({"unload_tip_wiggle": True})
        unit._retract_to_box(lane)
        wiggle = [c[0] for c in unit.afc.move_e_pos.calls[:6]]
        assert wiggle == [(0.5, 10.0, "CFS tip wiggle"), (-5, 10.0, "CFS tip wiggle"),
                          (2.5, 10.0, "CFS tip wiggle"), (-1.25, 10.0, "CFS tip wiggle"),
                          (1.75, 10.0, "CFS tip wiggle"), (1, 1.0, "CFS tip wiggle")]

    def test_start_not_answered(self):
        unit, lane = self._unit()
        unit.box._handlers.clear()
        unit.box.reply(P.RETRUDE_PROCESS, None)
        assert unit._retract_to_box(lane) == (False, "box did not answer RETRUDE start")
        assert unit.trace.events[0][1]["status"] == "NO_REPLY"
        assert not unit.afc.move_e_pos.called

    def test_start_error_carries_on(self):
        unit, lane = self._unit()
        unit.box.reply(P.RETRUDE_PROCESS, frame(P.RETRUDE_PROCESS, status=P.ST_RETRUDE_ERR2))
        assert unit._retract_to_box(lane) == (True, "reeled back in 12.4s")
        assert msgs(unit) == [("warning", "CFS lane1: RETRUDE start returned RETRUDE_ERR2")]

    def test_finish_not_confirmed(self):
        unit, lane = self._unit()
        unit.box._handlers.clear()
        unit.box.reply(P.RETRUDE_PROCESS, frame(P.RETRUDE_PROCESS),
                       frame(P.RETRUDE_PROCESS, status=P.ST_RETRUDE_ERR7))
        sensor(lane, False)
        assert unit._retract_to_box(lane) == (
            False, "box did not confirm reel-back (start OK, finish RETRUDE_ERR7)")

    def test_finish_not_answered(self):
        unit, lane = self._unit()
        unit.box._handlers.clear()
        unit.box.reply(P.RETRUDE_PROCESS, frame(P.RETRUDE_PROCESS), None)
        sensor(lane, False)
        ok, detail = unit._retract_to_box(lane)
        assert (ok, detail) == (False, "box did not confirm reel-back (start OK, finish NO_REPLY)")
        assert unit.trace.events[-1][1]["done"] is False

    def test_sensor_clears_after_a_wait(self):
        unit, lane = self._unit()
        unit.box._handlers.clear()
        unit.reactor.on_pause.append(
            lambda now: sensor(lane, False) if len(unit.reactor.pauses) >= 3 else None)
        assert unit._retract_to_box(lane)[0] is True

    def test_sensor_never_clears(self):
        unit, lane = self._unit({"unload_timeout": 2.0})
        unit.box._handlers.clear()
        assert unit._retract_to_box(lane) == (
            False, "toolhead sensor still triggered 2s after reel-back (box start OK, finish OK)")


class TestAfcCFSCmdToolUnload:
    def _unit(self, values=None):
        unit, (lane,) = unit_with(values=values)
        lane.select_lane = Recorder()
        lane.disable_buffer = Recorder()
        unit.afc._check_extruder_temp.reset_mock()
        unit._run_cutter_sequence = Recorder(result=None)
        unit._run_post_cut_macros = Recorder()
        unit._retract_to_box = Recorder(result=(True, "reeled back in 12.4s"))
        unit._mark_tool_state = Recorder()
        return unit, lane

    def _failed(self, unit, lane, message):
        unit.afc.error.handle_lane_failure.assert_called_once_with(lane, f"CFS lane1: {message}")
        assert msgs(unit)[-1] == ("error", f"CFS lane1 (slot A): {message}")

    def test_unloads(self):
        unit, lane = self._unit()
        lane._load_state = True
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        assert unit.wait_for_tag_reads.called
        assert lane.select_lane.called and lane.disable_buffer.called
        unit.afc._check_extruder_temp.assert_called_once_with(lane)
        assert unit._run_cutter_sequence.calls == [((lane,), {})]
        assert unit._run_post_cut_macros.calls == [((lane,), {})]
        assert lane._load_state is False
        assert unit._mark_tool_state.calls == [((lane,), {"loaded": False})]
        assert unit.trace.events == [
            ("unload_start", {"lane": "lane1", "slot": "A", "toolhead_sensor": False}),
            ("unload_end", {"lane": "lane1", "slot": "A", "ok": True,
                            "detail": "reeled back in 12.4s", "seconds": 0.0}),
        ]
        assert msgs(unit) == [("debug", "CFS lane1: unload reeled back in 12.4s")]
        assert unit._operation_depth == 0

    def test_offline_box_connects(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=True)
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        assert unit._retract_to_box.called

    def test_offline_box_wont_answer(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        self._failed(unit, lane, "box is offline (no reply)")
        assert not unit._run_cutter_sequence.called

    def test_offline_box_error_text(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        unit.transport.last_error = "read error: gone"
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        self._failed(unit, lane, "box is offline (read error: gone)")

    def test_cutter_engaged_before_cutting(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1"})
        unit.cutter_sensor_state = True
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        self._failed(unit, lane, "cutter sensor is engaged before cutting; check the cutter lever")
        assert not unit._run_cutter_sequence.called

    def test_cut_failed(self):
        unit, lane = self._unit()
        unit._run_cutter_sequence = Recorder(result="cutter sensor did not release after cutting")
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        self._failed(unit, lane, "cutter sensor did not release after cutting")
        assert not unit._retract_to_box.called

    def test_retract_failed(self):
        unit, lane = self._unit()
        lane._load_state = True
        unit._retract_to_box = Recorder(result=(False, "box did not answer RETRUDE start"))
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        self._failed(unit, lane, "unload failed: box did not answer RETRUDE start")
        assert lane._load_state is True
        assert not unit._mark_tool_state.called

    def test_retract_error(self):
        unit, lane = self._unit()
        unit._retract_to_box = Recorder(raises=RuntimeError("serial gone"))
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        assert unit.box.keys() == [(1, P.SET_BOX_MODE, b"\x00\x01")]
        debug = [m for lvl, m in msgs(unit) if lvl == "debug"]
        assert debug[0].startswith("Traceback") and debug[0].rstrip().endswith(
            "RuntimeError: serial gone")
        assert debug[1] == "CFS lane1: unload error: serial gone"
        self._failed(unit, lane, "unload failed: error: serial gone")
        assert unit._operation_depth == 0


class TestAfcCFSMarkToolState:
    def test_loaded(self):
        unit, (lane,) = unit_with()
        unit.afc.save_vars.reset_mock()
        unit._mark_tool_state(lane, loaded=True)
        assert lane.tool_loaded is True
        unit.afc.save_vars.assert_called_once_with()

    def test_unloaded(self):
        unit, (lane,) = unit_with()
        lane.set_tool_loaded(normal_toolchange=True)
        unit._mark_tool_state(lane, loaded=False)
        assert lane.tool_loaded is False

    def test_failure_is_a_warning(self):
        unit, (lane,) = unit_with()
        lane.set_tool_loaded = Recorder(raises=RuntimeError("no extruder"))
        unit._mark_tool_state(lane, loaded=True)
        assert msgs(unit) == [("warning", "CFS lane1: could not record tool state: no extruder")]


class TestAfcCFSCmdEject:
    def test_reels_back(self):
        unit, (_, lane) = unit_with(lanes=(1, 2))
        unit.reel_back = Recorder(result=(True, "reeled back"))
        cmd = gcmd(LANE="lane2")
        unit.cmd_AFC_CFS_EJECT(lane, cmd)
        assert unit.wait_for_tag_reads.called
        assert unit.reel_back.calls == [((lane,), {})]
        cmd.respond_info.assert_called_once_with("CFS lane2 (slot B) eject: reeled back")

    def test_offline_box_connects(self):
        unit, (lane,) = unit_with()
        unit.online = False
        unit.connect = Recorder(result=True)
        unit.reel_back = Recorder(result=(True, "reeled back"))
        unit.cmd_AFC_CFS_EJECT(lane, gcmd())
        assert unit.reel_back.called

    def test_offline_box(self):
        unit, (lane,) = unit_with()
        unit.online = False
        unit.connect = Recorder(result=False)
        unit.reel_back = Recorder()
        with pytest.raises(CommandError) as e:
            unit.cmd_AFC_CFS_EJECT(lane, gcmd())
        assert str(e.value) == "CFS lane1: box is offline"
        assert not unit.reel_back.called

    def test_not_clean(self):
        unit, (lane,) = unit_with()
        unit.reel_back = Recorder(result=(False, "box answered RETRUDE_ERR7"))
        cmd = gcmd()
        with pytest.raises(CommandError) as e:
            unit.cmd_AFC_CFS_EJECT(lane, cmd)
        cmd.respond_info.assert_called_once_with("CFS lane1 (slot A) eject: box answered "
                                                 "RETRUDE_ERR7")
        assert str(e.value) == "CFS lane1: eject did not finish cleanly: box answered RETRUDE_ERR7"
