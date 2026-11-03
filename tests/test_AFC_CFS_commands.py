"""Unit tests for afcCFS commands, status, eject and AFC's unit hooks (extras/AFC_CFS.py)."""
from __future__ import annotations

import importlib.util
import sys

import pytest
from extras.AFC_lane import AFCMoveWarning
from tests.conftest import MockConfig

from extras import AFC_CFS as P
from extras.AFC_CFS import afcCFS, load_config_prefix
from tests.cfs_helpers import (
    SERIAL,
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

UID_A = bytes.fromhex("67913b482422b00236353434")
UID_B = bytes.fromhex("b0313b4e8411b00645353230")
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
    unit.wait_for_tag_reads = Recorder()
    return unit, made


def msgs(unit):
    return unit.logger.messages


def raw(unit):
    return [m for lvl, m in msgs(unit) if lvl == "raw"]


# Module import guards

def _exec_with_blocked_dependency(blocked: str) -> None:
    """Run a throw-away copy of AFC_CFS.py with one import forced to fail."""
    fresh_name = "extras.AFC_CFS_import_guard_probe"
    original = sys.modules.get(blocked)
    sys.modules[blocked] = None
    try:
        spec = importlib.util.spec_from_file_location(fresh_name, P.__file__)
        fresh = importlib.util.module_from_spec(spec)
        sys.modules[fresh_name] = fresh
        try:
            spec.loader.exec_module(fresh)
        finally:
            sys.modules.pop(fresh_name, None)
    finally:
        if original is not None:
            sys.modules[blocked] = original
        else:
            sys.modules.pop(blocked, None)


class TestModuleImportGuards:
    def test_afc_utils(self):
        with pytest.raises(P.CONFIG_ERROR) as e:
            _exec_with_blocked_dependency("extras.AFC_utils")
        assert str(e.value).startswith("Error when trying to import AFC_utils.ERROR_STR")

    @pytest.mark.parametrize("module", ["AFC_unit", "AFC_lane", "AFC_respond"])
    def test_afc_modules(self, module):
        with pytest.raises(P.CONFIG_ERROR) as e:
            _exec_with_blocked_dependency(f"extras.{module}")
        assert str(e.value).startswith(f"Error trying to import {module}, please rerun "
                                       f"install-afc.sh")


# Unit hooks AFC calls

class TestAfcCFSPrepLoad:
    def test_nothing_to_do(self):
        unit, (lane,) = unit_with()
        assert unit.prep_load(lane) is None
        assert unit.box.sent == []


class TestAfcCFSPrepPostLoad:
    def test_nothing_to_do(self):
        unit, (lane,) = unit_with()
        assert unit.prep_post_load(lane) is None
        assert unit.box.sent == []


class TestAfcCFSEjectLane:
    def test_done(self):
        unit, (lane,) = unit_with()
        unit.reel_back = Recorder(result=(True, "retrude stages OK/OK, box finished"))
        unit.eject_lane(lane)
        assert msgs(unit) == [("info", "CFS CFS_1: eject lane1: retrude stages OK/OK, box "
                                       "finished")]

    def test_failed(self):
        unit, (lane,) = unit_with()
        unit.reel_back = Recorder(result=(False, "lane is loaded in the toolhead"))
        unit.eject_lane(lane)
        assert msgs(unit) == [("error", "CFS CFS_1: eject lane1: lane is loaded in the toolhead")]


class TestAfcCFSReelBack:
    def _unit(self):
        unit, (_, lane) = unit_with(lanes=(1, 2))
        lane._load_state = True
        return unit, lane

    def test_reels_back(self):
        unit, lane = self._unit()
        unit.reel_slot = Recorder(result=(True, ["OK", "OK"]))
        assert unit.reel_back(lane) == (
            True, "retrude stages OK/OK, box finished, toolhead sensor clear")
        assert unit.reel_slot.calls == [((0x02,), {})]
        assert lane._load_state is False
        assert unit.trace.events == [
            ("eject_start", {"lane": "lane2", "slot": "B"}),
            ("eject_end", {"lane": "lane2", "ok": True, "seconds": 0.0,
                           "detail": "retrude stages OK/OK, box finished, toolhead sensor clear"}),
        ]
        assert unit._operation_depth == 0

    def test_box_did_not_confirm(self):
        unit, lane = self._unit()
        unit.reel_slot = Recorder(result=(False, ["NO_REPLY"]))
        assert unit.reel_back(lane) == (
            False, "retrude stages NO_REPLY, box did not confirm, toolhead sensor clear")
        assert lane._load_state is True

    def test_filament_still_at_the_toolhead(self):
        unit, lane = self._unit()
        lane.extruder_obj.tool_start_state = True
        unit.reel_slot = Recorder(result=(True, ["OK", "OK"]))
        assert unit.reel_back(lane) == (
            False, "retrude stages OK/OK, box finished, toolhead sensor STILL TRIGGERED")
        assert lane._load_state is False

    def test_loaded_lane_refused(self):
        unit, lane = self._unit()
        lane.tool_loaded = True
        unit.reel_slot = Recorder()
        assert unit.reel_back(lane) == (
            False, "lane is loaded in the toolhead; use AFC_CFS_TOOL_UNLOAD")
        assert not unit.reel_slot.called
        assert unit.trace.events == []


class TestAfcCFSReelSlot:
    def test_reels_back(self):
        unit, _ = unit_with()
        assert unit.reel_slot(0x04) == (True, ["OK", "OK"])
        assert unit.box.keys() == [(1, P.SET_PRE_LOADING, b"\x0f\x00"),
                                   (1, P.SET_BOX_MODE, b"\x00\x01"),
                                   (1, P.RETRUDE_PROCESS, b"\x04\x00"),
                                   (1, P.RETRUDE_PROCESS, b"\x04\x01"),
                                   (1, P.SET_BOX_MODE, b"\x00\x01")]
        assert unit.reactor.pauses == pytest.approx([1000.3, 1000.6])

    def test_start_not_answered(self):
        unit, _ = unit_with()
        unit.box.reply(P.RETRUDE_PROCESS, None)
        assert unit.reel_slot(0x04) == (False, ["NO_REPLY"])
        assert unit.box.fns().count(P.RETRUDE_PROCESS) == 1
        assert unit.box.fns()[-1] == P.SET_BOX_MODE

    def test_failed_start_still_finishes(self):
        unit, _ = unit_with()
        unit.box.reply(P.RETRUDE_PROCESS, frame(P.RETRUDE_PROCESS, status=P.ST_RETRUDE_ERR2))
        assert unit.reel_slot(0x04) == (True, ["RETRUDE_ERR2", "OK"])

    def test_finish_not_answered(self):
        unit, _ = unit_with()
        unit.box.reply(P.RETRUDE_PROCESS, frame(P.RETRUDE_PROCESS), None)
        assert unit.reel_slot(0x04) == (False, ["OK", "NO_REPLY"])

    def test_finish_error(self):
        unit, _ = unit_with()
        unit.box.reply(P.RETRUDE_PROCESS, frame(P.RETRUDE_PROCESS),
                       frame(P.RETRUDE_PROCESS, status=P.ST_RETRUDE_ERR7))
        assert unit.reel_slot(0x04) == (False, ["OK", "RETRUDE_ERR7"])


class TestAfcCFSMoveToHub:
    def test_not_used(self):
        unit, (lane,) = unit_with()
        assert unit.move_to_hub(lane, 100.0, None) == (False, 0, AFCMoveWarning.ERROR)
        assert msgs(unit) == [("debug", "CFS CFS_1: move_to_hub not supported, box handles "
                                        "feeding")]


class TestAfcCFSLoadThenHome:
    def test_not_used(self):
        unit, (lane,) = unit_with()
        assert unit.load_then_home(lane, 100.0, None, None) == (False, 0, AFCMoveWarning.ERROR)
        assert msgs(unit) == [("debug", "CFS CFS_1: load_then_home not supported, box handles "
                                        "feeding")]


NOTHING = "CFS lanes are fed by the box itself; there is nothing to calibrate."


class TestAfcCFSCalibrateLane:
    def test_nothing_to_calibrate(self):
        unit, (lane,) = unit_with()
        assert unit.calibrate_lane(lane, 1.0) == (False, NOTHING, 0.0)


class TestAfcCFSCalibrateBowden:
    def test_nothing_to_calibrate(self):
        unit, (lane,) = unit_with()
        assert unit.calibrate_bowden(lane, 900.0, 1.0) == (False, NOTHING, 0.0)


class TestAfcCFSCalibrateTd1:
    def test_nothing_to_calibrate(self):
        unit, (lane,) = unit_with()
        assert unit.calibrate_td1(lane, 900.0, 1.0) == (False, NOTHING, 0.0)


PROMPT = ["// action:prompt_begin CFS_1 Calibration",
          f"// action:prompt_text {NOTHING}",
          "// action:prompt_footer_button Cancel|RESPOND TYPE=command "
          "MSG=action:prompt_end|warning",
          "// action:prompt_show"]


class TestAfcCFSCmdUnitCalibration:
    def test_prompt(self):
        unit, _ = unit_with()
        unit.cmd_UNIT_CALIBRATION(gcmd())
        assert raw(unit) == PROMPT


class TestAfcCFSCmdUnitLaneCalibration:
    def test_prompt(self):
        unit, _ = unit_with()
        unit.cmd_UNIT_LANE_CALIBRATION(gcmd())
        assert raw(unit) == PROMPT


class TestAfcCFSCmdUnitBowCalibration:
    def test_prompt(self):
        unit, _ = unit_with()
        unit.cmd_UNIT_BOW_CALIBRATION(gcmd())
        assert raw(unit) == PROMPT


class TestAfcCFSCmdAfcUnitTdOneCalibration:
    def test_prompt(self):
        unit, _ = unit_with()
        unit.cmd_AFC_UNIT_TD_ONE_CALIBRATION(gcmd())
        assert raw(unit) == PROMPT


# Status

class TestAfcCFSCmdAfcCfsStatus:
    def _unit(self, values=None, lanes=(1, 2)):
        unit, made = unit_with(values=values, lanes=lanes)
        unit.version_sn = "11350000232175B625DGIB"
        unit.seen_uid = UID_A
        sensor_banks = {b"\x00": b"\x01", b"\x01": b"\x01"}
        unit.box.on(P.GET_FILAMENT_SENSOR_STATE,
                    lambda req: frame(P.GET_FILAMENT_SENSOR_STATE, sensor_banks[req.data]))
        unit.box.reply(P.GET_BUFFER_STATE, frame(P.GET_BUFFER_STATE, b"\x01"))
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE))
        unit.box.reply(P.GET_MEASURING_WHEEL, frame(P.GET_MEASURING_WHEEL,
                                                    bytes.fromhex("c5afa071")))
        return unit, made

    def _lines(self, cmd):
        (call,) = cmd.respond_info.call_args_list
        return call.args[0].split("\n")

    def test_full_report(self):
        unit, _ = self._unit()
        unit.lane_tags = {"lane1": P.decode_rfid_record(PINK_SILK)}
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        assert self._lines(cmd) == [
            f"CFS CFS_1 @ {SERIAL} addr 1",
            "  version: 11350000232175B625DGIB",
            f"  box uid: {UID_A.hex()}",
            "  box state: OK",
            "  filament present: A",
            "  connected slot: A",
            "  buffer: full",
            "  measuring wheel: -5620.06 mm",
            "  cutter sensor: not configured",
            "  link: tx 0 rx 0 timeouts 0 crc errors 0",
            "  trace: off (set trace_file to enable)",
            "  lane1 -> slot A: present, tag: PLA, #E7A18E, 1000 g",
            "  lane2 -> slot B: empty",
        ]
        assert unit._operation_depth == 0

    def test_unknown_values(self):
        unit, _ = self._unit(values={"box_uid": UID_B.hex()}, lanes=(1,))
        unit.version_sn = None
        unit.seen_uid = None
        unit.box._handlers.clear()
        unit.box._queues.clear()
        unit.box.default_ok = False
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        lines = self._lines(cmd)
        assert lines[1:9] == [
            "  version: unknown",
            f"  box uid: ?{f' (box_uid is {UID_B.hex()})'}",
            "  box state: NO_REPLY",
            "  filament present: ?",
            "  connected slot: ?",
            "  buffer: NO_REPLY",
            "  measuring wheel: ?",
            "  cutter sensor: not configured",
        ]
        assert lines[-1] == "  lane1 -> slot A: empty"

    def test_matching_box_uid_has_no_note(self):
        unit, _ = self._unit(values={"box_uid": UID_A.hex()})
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        assert self._lines(cmd)[2] == f"  box uid: {UID_A.hex()}"

    def test_no_slots_and_odd_buffer_value(self):
        unit, _ = self._unit()
        unit.box._handlers.clear()
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, frame(P.GET_FILAMENT_SENSOR_STATE, b"\x00"),
                       frame(P.GET_FILAMENT_SENSOR_STATE, b"\x00"))
        unit.box._queues[P.GET_BUFFER_STATE] = [frame(P.GET_BUFFER_STATE, b"\x07")]
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        lines = self._lines(cmd)
        assert lines[4:7] == ["  filament present: none", "  connected slot: none",
                              "  buffer: 0x07"]

    @pytest.mark.parametrize("state,text", [(True, "triggered"), (False, "open")])
    def test_cutter_sensor(self, state, text):
        unit, _ = self._unit(values={"cutter_sensor_pin": "PA1"})
        unit.cutter_sensor_state = state
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        assert self._lines(cmd)[8] == f"  cutter sensor: {text}"

    def test_offline_tries_to_connect(self):
        unit, _ = self._unit()
        unit.online = False
        unit.connect = lambda: setattr(unit, "online", True)
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        assert self._lines(cmd)[0] == f"CFS CFS_1 @ {SERIAL} addr 1"

    def test_offline(self):
        unit, _ = self._unit()
        unit.online = False
        unit.connect = Recorder()
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        cmd.respond_info.assert_called_once_with("CFS CFS_1: offline (no reply)")
        assert unit.box.sent == []
        assert unit._operation_depth == 0

    def test_offline_with_error_text(self):
        unit, _ = self._unit()
        unit.online = False
        unit.connect = Recorder()
        unit.transport.last_error = "open /dev/fake_cfs failed: gone"
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        cmd.respond_info.assert_called_once_with(
            "CFS CFS_1: offline (open /dev/fake_cfs failed: gone)")


class TestAfcCFSCmdAfcCfsReconnect:
    def test_single_box_reopens_the_port(self):
        unit, _ = unit_with()
        unit.transport.close = Recorder()
        seen = []
        unit.connect = lambda: (seen.append(unit.online), True)[1]
        cmd = gcmd()
        unit.cmd_AFC_CFS_RECONNECT(cmd)
        assert unit.transport.close.called
        assert seen == [False]
        cmd.respond_info.assert_called_once_with("CFS CFS_1: online")
        assert unit._operation_depth == 0

    def test_chain_keeps_the_port_open(self):
        printer = make_printer()
        unit, _ = unit_with(printer=printer)
        make_unit("CFS_2", printer=printer, values={"box_addr": 2})
        unit.transport.close = Recorder()
        unit.connect = Recorder(result=True)
        unit.cmd_AFC_CFS_RECONNECT(gcmd())
        assert not unit.transport.close.called

    def test_still_offline(self):
        unit, _ = unit_with()
        unit.transport.close = Recorder()
        unit.connect = Recorder(result=False)
        cmd = gcmd()
        unit.cmd_AFC_CFS_RECONNECT(cmd)
        cmd.respond_info.assert_called_once_with("CFS CFS_1: offline (no reply)")

    def test_still_offline_with_error_text(self):
        unit, _ = unit_with()
        unit.transport.close = Recorder()
        unit.connect = Recorder(result=False)
        unit.transport.last_error = "read error: gone"
        cmd = gcmd()
        unit.cmd_AFC_CFS_RECONNECT(cmd)
        cmd.respond_info.assert_called_once_with("CFS CFS_1: offline (read error: gone)")


class TestAfcCFSTraceSummary:
    def test_no_trace_file(self):
        unit, _ = unit_with()
        assert unit._trace_summary() == "off (set trace_file to enable)"

    def test_paused(self):
        unit, _ = unit_with(values={"trace_file": "/tmp/cfs_trace.jsonl"})
        unit.trace = RecTrace(enabled=False)
        assert unit._trace_summary() == "paused (/tmp/cfs_trace.jsonl)"

    def test_stopped_by_an_error(self):
        unit, _ = unit_with(values={"trace_file": "/tmp/cfs_trace.jsonl"})
        unit.trace = RecTrace(enabled=False)
        unit.trace.last_error = "cannot open trace file: denied"
        assert unit._trace_summary() == "paused (cannot open trace file: denied)"

    def test_recording(self):
        unit, _ = unit_with(values={"trace_file": "/tmp/cfs_trace.jsonl"})
        unit.trace.written = 12
        assert unit._trace_summary() == "recording to /tmp/cfs_trace.jsonl (12 events)"

    def test_recording_with_drops(self):
        unit, _ = unit_with(values={"trace_file": "/tmp/cfs_trace.jsonl"})
        unit.trace.written = 12
        unit.trace.dropped = 3
        assert unit._trace_summary() == "recording to /tmp/cfs_trace.jsonl (12 events, 3 dropped)"


class TestAfcCFSCmdAfcCfsTrace:
    def _unit(self, trace_file="/tmp/cfs_trace.jsonl"):
        values = {"trace_file": trace_file} if trace_file else None
        unit, _ = unit_with(values=values)
        return unit

    def test_report_only(self):
        unit = self._unit()
        cmd = gcmd()
        unit.cmd_AFC_CFS_TRACE(cmd)
        assert unit.trace.events == []
        cmd.respond_info.assert_called_once_with(
            "CFS CFS_1 trace: recording to /tmp/cfs_trace.jsonl (0 events)")

    def test_resume(self):
        unit = self._unit()
        unit.trace.enabled = False
        unit.cmd_AFC_CFS_TRACE(gcmd(ENABLE=1))
        assert unit.trace.enabled is True
        assert unit.trace.events == [("trace_resume", {})]

    def test_resume_without_a_file(self):
        unit = self._unit(trace_file=None)
        with pytest.raises(CommandError) as e:
            unit.cmd_AFC_CFS_TRACE(gcmd(ENABLE=1))
        assert str(e.value) == "CFS CFS_1: set trace_file in [AFC_CFS CFS_1] first"

    def test_pause(self):
        unit = self._unit()
        cmd = gcmd(ENABLE=0)
        unit.cmd_AFC_CFS_TRACE(cmd)
        assert unit.trace.enabled is False
        assert unit.trace.events == [("trace_pause", {})]
        cmd.respond_info.assert_called_once_with("CFS CFS_1 trace: paused (/tmp/cfs_trace.jsonl)")

    def test_marker(self):
        unit = self._unit()
        unit.cmd_AFC_CFS_TRACE(gcmd(MARK="step 4 load A"))
        assert unit.trace.events == [("mark", {"text": "step 4 load A"})]


class TestAfcCFSCmdAfcCfsRaw:
    def test_reply(self):
        unit, _ = unit_with()
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, b"\x11\x22"))
        cmd = gcmd(FN="0A")
        unit.cmd_AFC_CFS_RAW(cmd)
        (req,) = unit.box.sent
        assert req.key == (1, 0x0A, b"")
        assert req.kwargs == {"status": 0xFF, "timeout": 2.0, "retries": 0}
        # TX matches captured stock traffic for GET_BOX_STATE to box 1
        reply_hex = P.encode_frame(1, 0x00, 0x0A, b"\x11\x22").hex()
        cmd.respond_info.assert_called_once_with(
            f"TX f70103ff0a5c\nRX {reply_hex} status=OK data=1122")
        assert unit._operation_depth == 0

    def test_options_and_no_reply(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        cmd = gcmd(FN="a2", DATA="", ADDR=2, STATUS="00", TIMEOUT=0.5)
        unit.cmd_AFC_CFS_RAW(cmd)
        (req,) = unit.box.sent
        assert req.key == (2, 0xA2, b"")
        assert req.kwargs == {"status": 0x00, "timeout": 0.5, "retries": 0}
        # Captured stock ONLINE_CHECK to box 2 is f7020300a2..; box 1's is f7010300a2da
        expected_tx = P.encode_frame(2, 0x00, 0xA2).hex()
        cmd.respond_info.assert_called_once_with(f"TX {expected_tx}\nRX (no reply)")

    def test_payload(self):
        unit, _ = unit_with()
        cmd = gcmd(FN="04", DATA="0001")
        unit.cmd_AFC_CFS_RAW(cmd)
        assert unit.box.sent[0].key == (1, 0x04, b"\x00\x01")
        assert cmd.respond_info.call_args.args[0].startswith("TX f70105ff04000190\n")


class TestAfcCFSGetStatus:
    def test_reports_box_and_lanes(self):
        unit, (lane1, lane2) = unit_with(lanes=(1, 2))
        unit.version_sn = "113500"
        unit.material_mask = 0b0011
        unit.connected_mask = 0b0001
        unit.buffer_state = P.BUFFER_MIDDLE
        unit.box_status = P.ST_EXTRUDE_ERR8
        unit.present_last = {"lane1": True, "lane2": None}
        unit.load_reports = {"lane1": {"slot": "A", "result": "loaded"}}
        unit.lane_tags = {"lane2": None}
        status = unit.get_status()
        assert status["lanes"] == ["lane1", "lane2"]
        assert status["cfs"] == {
            "online": True, "box_addr": 1, "version": "113500", "material_mask": 3,
            "connected_mask": 1, "buffer_state": 0, "box_status": "EXTRUDE_ERR8",
            "cutter_sensor": None, "trace": "/tmp/cfs_trace.jsonl",
            "lanes": {
                "lane1": {"slot": "A", "present": True,
                          "last_load": {"slot": "A", "result": "loaded"}, "tag": None},
                "lane2": {"slot": "B", "present": None, "last_load": {}, "tag": None},
            },
        }
        # A copy, so the report can't be changed through the status
        assert status["cfs"]["lanes"]["lane1"]["last_load"] is not unit.load_reports["lane1"]

    def test_nothing_known_yet(self):
        unit, _ = unit_with(values={"cutter_sensor_pin": "PA1"})
        unit.trace = RecTrace(enabled=False)
        unit.cutter_sensor_state = True
        cfs = unit.get_status()["cfs"]
        assert (cfs["box_status"], cfs["cutter_sensor"], cfs["trace"]) == (None, True, None)


class TestLoadConfigPrefix:
    def test_builds_a_unit(self):
        printer = make_printer()
        printer._objects["AFC_hub CFS_1"] = object()
        config = MockConfig(name="AFC_CFS CFS_1", printer=printer,
                            values={"serial": SERIAL, "hub": "CFS_1", "extruder": "extruder"})
        unit = load_config_prefix(config)
        assert isinstance(unit, afcCFS)
        assert unit.name == "CFS_1"
