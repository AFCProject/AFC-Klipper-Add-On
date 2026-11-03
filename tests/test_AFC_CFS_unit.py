"""Unit tests for afcCFS setup, connection, polling, PREP, lane setup and presence."""
from __future__ import annotations

import pytest
from extras.AFC_lane import AFCLaneState

from extras import AFC_CFS as P
from extras.AFC_CFS import CFSTrace, afcCFS
from tests.cfs_helpers import (
    SERIAL,
    Recorder,
    RecTrace,
    connect,
    frame,
    make_lane,
    make_printer,
    make_unit,
    reset_buses,
)

UID_A = bytes.fromhex("67913b482422b00236353434")
UID_B = bytes.fromhex("b0313b4e8411b00645353230")


@pytest.fixture(autouse=True)
def _fresh_buses():
    reset_buses()
    yield
    reset_buses()


def unit_with(values=None, lanes=(1,), do_connect=True, printer=None, name="CFS_1"):
    """A connected unit with real lanes and a recording trace."""
    unit = make_unit(name=name, values=values, printer=printer)
    made = [make_lane(unit, i) for i in lanes]
    if do_connect:
        connect(unit)
    unit.trace = RecTrace()
    return unit, made


def online_check(uid=None):
    """CMD_ONLINE_CHECK reply, with the box uid at data[2:14] when given."""
    return frame(P.CMD_ONLINE_CHECK, (b"\x00\x00" + uid) if uid else b"")


def msgs(unit):
    return unit.logger.messages


def tool_cmd(lane):
    """The lane's T command as the PREP line shows it: AFC DEV's map_to_string(), or
    AFC main's plain string map."""
    return lane.map_to_string() if hasattr(lane, "map_to_string") else str(lane.map)


# Construction and config

class TestAfcCFSInit:
    def test_builds_through_afc_unit(self):
        printer = make_printer()
        printer._gcode.register_mux_command = Recorder()
        unit = make_unit(printer=printer)
        assert (unit.name, unit.type) == ("CFS_1", "CFS")
        assert unit.reactor is printer.get_reactor()
        assert unit.transport is unit.bus.transport
        assert unit.bus.units == [unit]
        assert printer._event_handlers["klippy:ready"][-1] == unit._cfs_handle_ready
        assert printer._event_handlers["klippy:disconnect"][-1] == unit._cfs_handle_disconnect
        assert printer._gcode.register_mux_command.calls == [
            (("AFC_CFS_STATUS", "UNIT", "CFS_1", unit.cmd_AFC_CFS_STATUS),
             {"desc": unit.cmd_AFC_CFS_STATUS_help}),
            (("AFC_CFS_RECONNECT", "UNIT", "CFS_1", unit.cmd_AFC_CFS_RECONNECT),
             {"desc": unit.cmd_AFC_CFS_RECONNECT_help}),
            (("AFC_CFS_RAW", "UNIT", "CFS_1", unit.cmd_AFC_CFS_RAW),
             {"desc": unit.cmd_AFC_CFS_RAW_help}),
            (("AFC_CFS_TRACE", "UNIT", "CFS_1", unit.cmd_AFC_CFS_TRACE),
             {"desc": unit.cmd_AFC_CFS_TRACE_help}),
        ]

    def test_type_can_be_set(self):
        assert make_unit(values={"type": "CFS2"}).type == "CFS2"

    def test_cutter_pin_registers_a_button(self):
        printer = make_printer()
        unit = make_unit(printer=printer, values={"cutter_sensor_pin": "PA1"})
        printer._objects["buttons"].register_buttons.assert_called_once_with(
            ["PA1"], unit._cutter_callback)

    def test_no_cutter_pin_no_button(self):
        printer = make_printer()
        make_unit(printer=printer)
        assert "buttons" not in printer._objects

    def test_clashing_chain_is_a_config_error(self):
        printer = make_printer()
        make_unit("CFS_1", printer=printer)
        with pytest.raises(Exception) as e:
            make_unit("CFS_2", printer=printer)              # also box_addr 1
        assert str(e.value) == (f"[AFC_CFS CFS_2]: CFS CFS_1 and CFS_2 are both box_addr 1 on "
                                f"{SERIAL}; give each box on a chain its own box_addr (1-4)")


class TestAfcCFSLoadCfsConfig:
    def test_defaults(self):
        unit = make_unit()
        expected = {
            "serial_path": SERIAL, "baud": 230400, "box_addr": 1, "box_uid": None,
            "poll_interval": 1.0, "heartbeat_interval": 1.5, "heartbeat_interval_printing": 10.0,
            "request_timeout": 0.8, "stage_timeout": 30.0, "checkin_timeout": 5.0,
            "resync_timeout": 60.0, "offline_after": 3, "reconnect_interval": 5.0,
            "boot_wait": 20.0, "protocol_debug": False, "trace_file": None,
            "trace_max_size": 5.0, "trace_backups": 1, "trace_polls": False,
            "load_attempts": 2, "stage_settle_time": 0.3, "stage5_poll_interval": 0.4,
            "stage5_reply_timeout": 2.0, "stage5_timeout": 60.0, "stage5_stall_time": 8.0,
            "stage5_stall_distance": 2.0, "prime1_length": 10.0, "prime1_speed": 300.0,
            "prime2_length": 5.0, "prime2_speed": 600.0, "stage7_arg": 3,
            "clear_mode_after_load": False, "unload_retract_steps": [(15.0, 600.0), (45.0, 1800.0)],
            "unload_tip_wiggle": False, "unload_timeout": 30.0, "cutter_wait_time": 3.0,
            "cutter_require_trigger": True, "cutter_sensor_pin": None, "rfid_read": False,
            "rfid_insert_delay": 10.0, "rfid_spin": True, "rfid_busy_timeout": 150.0,
            "rfid_read_attempts": 3, "rfid_retry_interval": 5.0, "rfid_read_at_startup": False,
            "rfid_startup_delay": 15.0, "rfid_materials": {},
        }
        assert {k: getattr(unit, k) for k in expected} == expected

    def test_uid_with_separators_and_case(self):
        unit = make_unit(values={"box_uid": "67:91:3B:48 24:22:B0:02 36:35:34:34"})
        assert unit.box_uid == UID_A

    def test_uid_wrong_length(self):
        with pytest.raises(Exception) as e:
            make_unit(values={"box_uid": "6791"})
        assert str(e.value) == "box_uid must be 24 hex digits, got '6791'"

    def test_uid_not_hex(self):
        with pytest.raises(Exception) as e:
            make_unit(values={"box_uid": "zz913b482422b00236353434"})
        assert str(e.value) == "box_uid must be 24 hex digits, got 'zz913b482422b00236353434'"

    def test_uid_none_value(self):
        assert make_unit(values={"box_uid": None}).box_uid is None

    def test_materials_and_steps_parsed(self):
        unit = make_unit(values={"rfid_materials": "00003=PETG",
                                 "unload_retract_steps": "10:600, 10:1800"})
        assert unit.rfid_materials == {"00003": "PETG"}
        assert unit.unload_retract_steps == [(10.0, 600.0), (10.0, 1800.0)]


class TestAfcCFSInitCfsRuntime:
    def test_box_state_starts_unknown(self):
        unit = make_unit()
        state = {k: getattr(unit, k) for k in (
            "online", "version_sn", "material_mask", "connected_mask", "buffer_state",
            "box_status", "seen_uid", "cutter_source", "cutter_sensor_state",
            "cutter_trigger_count", "_consecutive_failures", "_last_reconnect_attempt",
            "_last_heartbeat", "_boot_wait_done", "_operation_depth", "_quiet_requests",
            "_poll_timer", "_offline_warned", "_rfid_timer", "_rfid_busy",
            "_rfid_startup_queued", "_traced_mask", "_traced_print_state")}
        assert state == {
            "online": False, "version_sn": None, "material_mask": None, "connected_mask": None,
            "buffer_state": None, "box_status": None, "seen_uid": None, "cutter_source": None,
            "cutter_sensor_state": False, "cutter_trigger_count": 0, "_consecutive_failures": 0,
            "_last_reconnect_attempt": 0.0, "_last_heartbeat": 0.0, "_boot_wait_done": False,
            "_operation_depth": 0, "_quiet_requests": False, "_poll_timer": None,
            "_offline_warned": False, "_rfid_timer": None, "_rfid_busy": False,
            "_rfid_startup_queued": False, "_traced_mask": None, "_traced_print_state": None}
        assert (unit._lane_commands, unit.present_last, unit.load_reports) == (set(), {}, {})
        assert (unit.lane_tags, unit._rfid_queue) == ({}, {})

    def test_resync_timeout_raised_to_the_units(self):
        assert make_unit().transport.resync_timeout == 60.0

    def test_shorter_resync_timeout_keeps_the_transports(self):
        assert make_unit(values={"resync_timeout": 10.0}).transport.resync_timeout == 15.0

    def test_no_trace_file(self):
        unit = make_unit()
        assert isinstance(unit.trace, CFSTrace)
        assert unit.trace.enabled is False
        assert unit.transport.trace is None

    def test_trace_file_records_wire_and_settings(self, tmp_path):
        path = tmp_path / "t.jsonl"
        unit = make_unit(values={"trace_file": str(path), "trace_max_size": 0.5,
                                 "trace_backups": 2})
        try:
            assert unit.trace.enabled is True
            assert unit.transport.trace is unit.trace
            assert (unit.trace.max_bytes, unit.trace.backups) == (524288, 2)
        finally:
            unit.trace.stop()
        first = path.read_text().splitlines()[0]
        assert '"ev":"trace_start"' in first and '"unit":"CFS_1"' in first

    def test_second_trace_leaves_the_wire_to_the_first(self, tmp_path):
        printer = make_printer()
        a = make_unit("CFS_1", printer=printer, values={"trace_file": str(tmp_path / "a")})
        b = make_unit("CFS_2", printer=printer, values={"box_addr": 2,
                                                       "trace_file": str(tmp_path / "b")})
        try:
            assert b.transport.trace is a.trace
        finally:
            a.trace.stop()
            b.trace.stop()

    def test_trace_takes_the_wire_from_a_stopped_one(self, tmp_path):
        printer = make_printer()
        a = make_unit("CFS_1", printer=printer, values={"trace_file": str(tmp_path / "a")})
        a.trace.stop()
        b = make_unit("CFS_2", printer=printer, values={"box_addr": 2,
                                                       "trace_file": str(tmp_path / "b")})
        try:
            assert b.transport.trace is b.trace
        finally:
            b.trace.stop()

    def test_unit_without_trace_file_leaves_the_wire_alone(self, tmp_path):
        printer = make_printer()
        a = make_unit("CFS_1", printer=printer, values={"trace_file": str(tmp_path / "a")})
        try:
            make_unit("CFS_2", printer=printer, values={"box_addr": 2})
            assert a.transport.trace is a.trace
        finally:
            a.trace.stop()


class TestAfcCFSParseMaterials:
    def test_empty(self):
        assert afcCFS._parse_materials("") == {}

    def test_none(self):
        assert afcCFS._parse_materials(None) == {}

    def test_several_with_spaces_and_blanks(self):
        assert afcCFS._parse_materials(" 00003 = PETG , ,04001=ABS") == {
            "00003": "PETG", "04001": "ABS"}

    @pytest.mark.parametrize("item", ["00003", "0003=PETG", "0000A=PETG", "00003= "])
    def test_bad_item(self, item):
        with pytest.raises(P.CONFIG_ERROR) as e:
            afcCFS._parse_materials(f"00001=PLA, {item}")
        assert str(e.value) == (f"rfid_materials: expected 'CODE=MATERIAL' with a 5-digit code, "
                                f"got '{item.strip()}'")


class TestAfcCFSParseSteps:
    def test_pairs(self):
        assert afcCFS._parse_steps("15:600, -45:1800") == [(15.0, 600.0), (45.0, 1800.0)]

    def test_blank_items_skipped(self):
        assert afcCFS._parse_steps(" , 10:90 ,") == [(10.0, 90.0)]

    @pytest.mark.parametrize("item", ["15", "15:600:1", "x:600"])
    def test_bad_item(self, item):
        with pytest.raises(P.CONFIG_ERROR) as e:
            afcCFS._parse_steps(f"10:90, {item}")
        assert str(e.value) == (f"unload_retract_steps: expected 'distance:speed' pairs, "
                                f"got '{item}'")


# Lifecycle

class TestAfcCFSHandleConnect:
    def test_sets_up_lanes_and_logo(self):
        unit, (lane,) = unit_with()
        assert lane.custom_load_cmd == "AFC_CFS_TOOL_LOAD LANE=lane1"
        assert unit.logo == '<span class=success--text>CFS Ready</span>\n'
        assert unit.logo_error == '<span class=error--text>CFS Not Ready</span>\n'

    def test_box_without_pin_uses_the_chains_cutter(self):
        printer = make_printer()
        a, _ = unit_with(name="CFS_1", printer=printer, values={"cutter_sensor_pin": "PA1"})
        b, _ = unit_with(name="CFS_2", printer=printer, values={"box_addr": 2}, lanes=(1,))
        assert b.cutter_source is a
        assert a.cutter_source is None                     # has its own pin

    def test_no_cutter_on_the_chain(self):
        unit, _ = unit_with()
        assert unit.cutter_source is None


class TestAfcCFSCfsHandleReady:
    def test_starts_polling(self):
        unit, _ = unit_with()
        unit._cfs_handle_ready()
        timer = unit.reactor.timers[-1]
        assert (timer.callback, timer.waketime) == (unit._poll, unit.reactor.NOW)
        assert unit._poll_timer is timer


class TestAfcCFSCfsHandleDisconnect:
    def test_stops_timers_port_and_trace(self):
        unit, _ = unit_with()
        unit._poll_timer = "poll"
        unit._rfid_timer = "rfid"
        unit.transport.close = Recorder()
        unit._cfs_handle_disconnect()
        assert unit.reactor.unregistered == ["poll", "rfid"]
        assert (unit._poll_timer, unit._rfid_timer) == (None, None)
        assert unit.transport.close.called
        assert unit.trace.events == [("trace_stop", {"reason": "klippy disconnect"})]
        assert unit.trace.stopped is True

    def test_no_timers_running(self):
        unit, _ = unit_with()
        unit.transport.close = Recorder()
        unit._cfs_handle_disconnect()
        assert unit.reactor.unregistered == []
        assert unit.transport.close.called


class TestAfcCFSCutterCallback:
    def test_counts_each_closing(self):
        unit, _ = unit_with()
        for state in (1, 1, 0, 1):
            unit._cutter_callback(0.0, state)
        assert unit.cutter_trigger_count == 2
        assert unit.cutter_sensor_state is True
        assert unit.trace.events == [
            ("cutter_sensor", {"triggered": True, "count": 1}),
            ("cutter_sensor", {"triggered": True, "count": 1}),
            ("cutter_sensor", {"triggered": False, "count": 1}),
            ("cutter_sensor", {"triggered": True, "count": 2}),
        ]

    def test_opening(self):
        unit, _ = unit_with()
        unit.cutter_sensor_state = True
        unit._cutter_callback(0.0, 0)
        assert (unit.cutter_sensor_state, unit.cutter_trigger_count) == (False, 0)


class TestAfcCFSTraceSettings:
    def test_settings(self):
        unit = make_unit()
        assert unit._trace_settings() == {
            "poll_interval": 1.0, "request_timeout": 0.8, "load_attempts": 2,
            "stage_settle_time": 0.3, "stage_timeout": 30.0, "checkin_timeout": 5.0,
            "resync_timeout": 60.0, "stage5_poll_interval": 0.4, "stage5_reply_timeout": 2.0,
            "stage5_timeout": 60.0, "stage5_stall_time": 8.0, "stage5_stall_distance": 2.0,
            "prime1_length": 10.0, "prime1_speed": 300.0, "prime2_length": 5.0,
            "prime2_speed": 600.0, "stage7_arg": 3, "clear_mode_after_load": False,
            "unload_retract_steps": [(15.0, 600.0), (45.0, 1800.0)],
            "unload_tip_wiggle": False, "unload_timeout": 30.0}


class TestAfcCFSHeartbeat:
    def test_answer_resets_failures(self):
        unit, _ = unit_with()
        unit._consecutive_failures = 2
        unit._quiet_requests = True
        assert unit.heartbeat() == frame(P.CMD_ONLINE_CHECK)
        assert unit._consecutive_failures == 0
        (req,) = unit.box.sent
        assert req.key == (1, P.CMD_ONLINE_CHECK, b"")
        assert req.kwargs == {"status": P.REQ_STATUS_ADDRESSING, "timeout": 0.8, "quiet": True}

    def test_missed_answer_counts(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.reply(P.CMD_ONLINE_CHECK, None)
        assert unit.heartbeat() is None
        assert (unit._consecutive_failures, unit.online) == (1, True)
        assert msgs(unit) == []

    def test_too_many_misses_go_offline(self):
        unit, _ = unit_with()
        unit.online = True
        unit._consecutive_failures = 2
        unit.box.reply(P.CMD_ONLINE_CHECK, None)
        unit.heartbeat()
        assert unit.online is False
        assert msgs(unit) == [("warning", "CFS CFS_1: offline (no reply to heartbeat)")]


class PrintStats:
    def __init__(self, state="standby", filename=None, raises=False):
        self.state = state
        self.filename = filename
        self.raises = raises

    def get_status(self, eventtime):
        if self.raises:
            raise RuntimeError("stats not ready")
        return {"state": self.state, "filename": self.filename}


class TestAfcCFSIsPrinting:
    def test_no_print_stats(self):
        unit, _ = unit_with()
        assert unit._is_printing() is False

    def test_printing(self):
        unit, _ = unit_with()
        unit.printer._objects["print_stats"] = PrintStats("printing")
        assert unit._is_printing() is True

    def test_paused_is_not_printing(self):
        unit, _ = unit_with()
        unit.printer._objects["print_stats"] = PrintStats("paused")
        assert unit._is_printing() is False

    def test_stats_error(self):
        unit, _ = unit_with()
        unit.printer._objects["print_stats"] = PrintStats(raises=True)
        assert unit._is_printing() is False


class TestAfcCFSTracePrintState:
    def test_trace_off(self):
        unit, _ = unit_with()
        unit.trace.enabled = False
        unit.printer._objects["print_stats"] = PrintStats("printing")
        unit._trace_print_state()
        assert unit._traced_print_state is None

    def test_no_print_stats(self):
        unit, _ = unit_with()
        unit._trace_print_state()
        assert unit.trace.events == []

    def test_stats_error(self):
        unit, _ = unit_with()
        unit.printer._objects["print_stats"] = PrintStats(raises=True)
        unit._trace_print_state()
        assert unit.trace.events == []

    def test_records_each_change_once(self):
        unit, _ = unit_with()
        stats = PrintStats("printing", "coin.gcode")
        unit.printer._objects["print_stats"] = stats
        unit._trace_print_state()
        unit._trace_print_state()
        stats.state = "complete"
        unit._trace_print_state()
        assert unit.trace.events == [
            ("print_state", {"state": "printing", "filename": "coin.gcode"}),
            ("print_state", {"state": "complete", "filename": "coin.gcode"}),
        ]
        assert unit._traced_print_state == "complete"


# Connection

class TestAfcCFSConnect:
    def _unit(self, values=None):
        unit, _ = unit_with(values=values)
        unit.transport.open = Recorder(result=True)
        unit.bus.discover = Recorder(result=[])
        return unit

    def test_port_wont_open(self):
        unit = self._unit()
        unit.online = True
        unit.transport.open = Recorder(result=False)
        unit.transport.last_error = "open /dev/fake_cfs failed: gone"
        assert unit.connect() is False
        assert msgs(unit) == [("warning", "CFS CFS_1: offline (open /dev/fake_cfs failed: gone)")]
        assert unit.box.sent == []

    def test_box_answers_at_its_address(self):
        unit = self._unit()
        unit.box.reply(P.GET_VERSION_SN, frame(P.GET_VERSION_SN, b"113500"))
        assert unit.connect() is True
        assert unit.online is True
        assert unit.version_sn == "113500"
        assert not unit.bus.discover.called
        assert unit.seen_uid is None                       # short reply: no uid

    def test_records_the_uid_seen(self):
        unit = self._unit()
        unit.box.reply(P.CMD_ONLINE_CHECK, online_check(UID_A))
        assert unit.connect() is True
        assert unit.seen_uid == UID_A

    def test_matching_box_uid(self):
        unit = self._unit({"box_uid": UID_A.hex()})
        unit.box.reply(P.CMD_ONLINE_CHECK, online_check(UID_A))
        assert unit.connect() is True

    def test_wrong_box_at_the_address(self):
        unit = self._unit({"box_uid": UID_A.hex()})
        unit.online = True
        unit.box.reply(P.CMD_ONLINE_CHECK, online_check(UID_B))
        assert unit.connect() is False
        reason = (f"box at address 1 is uid {UID_B.hex()}, expected {UID_A.hex()} (box_uid); "
                  f"power-cycle the CFS chain so the boxes are addressed again")
        assert msgs(unit) == [("error", f"CFS CFS_1: {reason}"),
                              ("warning", f"CFS CFS_1: offline ({reason})")]
        assert unit.online is False

    def test_discovery_addresses_the_box(self):
        unit = self._unit()
        unit.box.reply(P.CMD_ONLINE_CHECK, None, None, online_check())
        unit.bus.discover = Recorder(result=[(UID_B, 2), (UID_A, 1)])
        assert unit.connect() is True
        assert unit.bus.discover.calls == [((), {"timeout": 1.5})]
        assert msgs(unit)[:2] == [
            ("debug", "CFS CFS_1: no reply at address 1, trying discovery"),
            ("debug", f"CFS CFS_1: addressed box uid {UID_A.hex()} at 1"),
        ]

    def test_discovery_timeout_follows_request_timeout(self):
        unit = self._unit({"request_timeout": 2.5})
        unit.box.default_ok = False
        unit.connect()
        assert unit.bus.discover.calls == [((), {"timeout": 2.5})]

    def test_nothing_found(self):
        unit = self._unit()
        unit.online = True
        unit.box.default_ok = False
        assert unit.connect() is False
        assert msgs(unit) == [
            ("debug", "CFS CFS_1: no reply at address 1, trying discovery"),
            ("warning", "CFS CFS_1: offline (box did not answer discovery)"),
        ]
        assert unit.box.fns() == [P.CMD_ONLINE_CHECK, P.CMD_ONLINE_CHECK]

    def test_silent_after_addressing(self):
        unit = self._unit()
        unit.online = True
        unit.box.default_ok = False
        unit.bus.discover = Recorder(result=[(UID_A, 1)])
        assert unit.connect() is False
        assert msgs(unit)[-1] == ("warning",
                                  "CFS CFS_1: offline (box did not answer after addressing)")
        assert unit.box.fns() == [P.CMD_ONLINE_CHECK] * 4


class TestAfcCFSProbe:
    def test_answers_addressing_status(self):
        unit, _ = unit_with()
        assert unit._probe() == frame(P.CMD_ONLINE_CHECK)
        assert [r.kwargs["status"] for r in unit.box.sent] == [P.REQ_STATUS_ADDRESSING]
        assert unit.box.sent[0].kwargs["timeout"] == 0.8

    def test_answers_normal_status(self):
        unit, _ = unit_with()
        unit.box.reply(P.CMD_ONLINE_CHECK, None, frame(P.CMD_ONLINE_CHECK))
        assert unit._probe() == frame(P.CMD_ONLINE_CHECK)
        assert [r.kwargs["status"] for r in unit.box.sent] == [P.REQ_STATUS_ADDRESSING,
                                                               P.REQ_STATUS_NORMAL]

    def test_no_answer(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        assert unit._probe() is None


class TestAfcCFSSetOnline:
    def test_already_online(self):
        unit, _ = unit_with()
        unit.online = True
        assert unit._set_online() is True
        assert unit.box.sent == []

    def test_reads_version(self):
        unit, _ = unit_with()
        unit._consecutive_failures = 2
        unit.box.reply(P.GET_VERSION_SN, frame(P.GET_VERSION_SN, b"11350000232175B625DGIB"))
        assert unit._set_online() is True
        assert (unit.online, unit._consecutive_failures) == (True, 0)
        assert unit.box.sent[0].kwargs == {"timeout": 0.8}
        assert unit.trace.events == [("online", {"box_addr": 1,
                                                 "version": "11350000232175B625DGIB"})]
        assert msgs(unit) == [("debug", "CFS CFS_1: online (addr 1, version "
                                        "11350000232175B625DGIB)")]

    def test_no_version(self):
        unit, _ = unit_with()
        unit.box.reply(P.GET_VERSION_SN, None)
        unit._set_online()
        assert unit.version_sn is None
        assert msgs(unit) == [("debug", "CFS CFS_1: online (addr 1, version unknown)")]

    def test_after_an_offline_warning_it_is_shown(self):
        unit, _ = unit_with()
        unit._offline_warned = True
        unit.box.reply(P.GET_VERSION_SN, None)
        unit._set_online()
        assert msgs(unit) == [("info", "CFS CFS_1: online (addr 1, version unknown)")]
        assert unit._offline_warned is False


class TestAfcCFSSetOffline:
    def test_going_offline(self):
        unit, _ = unit_with()
        unit.online = True
        unit._set_offline("no reply")
        assert (unit.online, unit._offline_warned) == (False, True)
        assert msgs(unit) == [("warning", "CFS CFS_1: offline (no reply)")]
        assert unit.trace.events == [("offline", {"reason": "no reply"})]

    def test_already_offline(self):
        unit, _ = unit_with()
        unit._set_offline("no reply")
        assert unit._offline_warned is False
        assert msgs(unit) == []
        assert unit.trace.events == []


# Box requests

class TestAfcCFSRequest:
    def test_reply_resets_failures(self):
        unit, _ = unit_with()
        unit.online = True
        unit._consecutive_failures = 2
        assert unit.request(P.GET_BOX_STATE) == frame(P.GET_BOX_STATE)
        assert unit._consecutive_failures == 0
        (req,) = unit.box.sent
        assert req.kwargs == {"timeout": 0.8, "retries": 1, "motor": False, "quiet": False}

    def test_passes_options(self):
        unit, _ = unit_with()
        unit.online = True
        unit._quiet_requests = True
        unit.request(P.EXTRUDE_PROCESS, b"\x01\x00\x00", timeout=12.0, retries=0, motor=True)
        assert unit.box.sent[0].kwargs == {"timeout": 12.0, "retries": 0, "motor": True,
                                           "quiet": True}

    def test_reply_while_offline_brings_it_online(self):
        unit, _ = unit_with()
        unit.request(P.GET_BOX_STATE)
        assert unit.online is True
        assert unit.box.fns() == [P.GET_BOX_STATE, P.GET_VERSION_SN]

    def test_missed_reply_counts(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.default_ok = False
        assert unit.request(P.GET_BOX_STATE) is None
        assert (unit._consecutive_failures, unit.online) == (1, True)

    def test_too_many_misses_go_offline(self):
        unit, _ = unit_with()
        unit.online = True
        unit._consecutive_failures = 2
        unit.box.default_ok = False
        unit.request(P.GET_BOX_STATE)
        assert unit.online is False
        assert msgs(unit) == [("warning", "CFS CFS_1: offline (no reply to GET_BOX_STATE)")]

    def test_silence_during_a_tag_read_is_not_counted(self):
        unit, _ = unit_with()
        unit.online = True
        unit._rfid_busy = True
        unit._consecutive_failures = 2
        unit.box.default_ok = False
        unit.request(P.GET_RFID, b"\x01")
        assert (unit._consecutive_failures, unit.online) == (2, True)


class TestAfcCFSSetBoxMode:
    def test_print_mode_for_a_slot(self):
        unit, _ = unit_with()
        unit.online = True
        unit.set_box_mode(P.MODE_PRINT, 0x04)
        assert unit.box.keys() == [(1, P.SET_BOX_MODE, b"\x04\x00")]

    def test_idle_default_slot(self):
        unit, _ = unit_with()
        unit.online = True
        unit.set_box_mode(P.MODE_IDLE)
        assert unit.box.keys() == [(1, P.SET_BOX_MODE, b"\x00\x01")]


class TestAfcCFSConnectionMotor:
    def test_action(self):
        unit, _ = unit_with()
        unit.online = True
        unit.connection_motor(P.CONN_RETRUDE)
        assert unit.box.keys() == [(1, P.CTRL_CONNECTION_MOTOR_ACTION, b"\x02")]


class TestAfcCFSTightenUp:
    def test_on(self):
        unit, _ = unit_with()
        unit.online = True
        unit.tighten_up(True)
        assert unit.box.keys() == [(1, P.TIGHTEN_UP_ENABLE, b"\x01")]

    def test_off(self):
        unit, _ = unit_with()
        unit.online = True
        unit.tighten_up(False)
        assert unit.box.keys() == [(1, P.TIGHTEN_UP_ENABLE, b"\x00")]


class TestAfcCFSPreLoading:
    def test_run_is_a_motor_command(self):
        unit, _ = unit_with()
        unit.online = True
        unit.pre_loading(P.PRELOAD_RUN, 0x02)
        (req,) = unit.box.sent
        assert req.key == (1, P.SET_PRE_LOADING, b"\x02\x02")
        assert req.kwargs == {"timeout": 30.0, "retries": 1, "motor": True, "quiet": False}

    def test_tight_is_a_motor_command(self):
        unit, _ = unit_with()
        unit.online = True
        unit.pre_loading(P.PRELOAD_TIGHT)
        assert unit.box.sent[0].kwargs["motor"] is True

    def test_close_all_slots(self):
        unit, _ = unit_with()
        unit.online = True
        unit.pre_loading(P.PRELOAD_CLOSE)
        (req,) = unit.box.sent
        assert req.key == (1, P.SET_PRE_LOADING, b"\x0f\x00")
        assert req.kwargs == {"timeout": 0.8, "retries": 1, "motor": False, "quiet": False}

    def test_explicit_timeout_and_retries(self):
        unit, _ = unit_with()
        unit.online = True
        unit.pre_loading(P.PRELOAD_RUN, 0x01, timeout=150.0, retries=0)
        assert unit.box.sent[0].kwargs == {"timeout": 150.0, "retries": 0, "motor": True,
                                           "quiet": False}


class TestAfcCFSExtrudeStage:
    def test_stage_5_is_a_quick_poll(self):
        unit, _ = unit_with()
        unit.online = True
        unit.extrude_stage(0x01, 0x05)
        (req,) = unit.box.sent
        assert req.key == (1, P.EXTRUDE_PROCESS, b"\x01\x05\x00")
        assert req.kwargs == {"timeout": 2.0, "retries": 0, "motor": False, "quiet": False}

    def test_stage_5_explicit_timeout(self):
        unit, _ = unit_with()
        unit.online = True
        unit.extrude_stage(0x01, 0x05, timeout=0.5)
        assert unit.box.sent[0].kwargs["timeout"] == 0.5

    def test_other_stages_wait_for_the_motion(self):
        unit, _ = unit_with()
        unit.online = True
        unit.extrude_stage(0x08, 0x07, 0x03)
        (req,) = unit.box.sent
        assert req.key == (1, P.EXTRUDE_PROCESS, b"\x08\x07\x03")
        assert req.kwargs == {"timeout": 30.0, "retries": 0, "motor": True, "quiet": False}

    def test_other_stage_explicit_timeout(self):
        unit, _ = unit_with()
        unit.online = True
        unit.extrude_stage(0x08, 0x00, timeout=3.0)
        assert unit.box.sent[0].kwargs["timeout"] == 3.0


class TestAfcCFSRetrudeStage:
    def test_slot_stage_waits_for_the_motion(self):
        unit, _ = unit_with()
        unit.online = True
        unit.retrude_stage(0x02, 0x01)
        (req,) = unit.box.sent
        assert req.key == (1, P.RETRUDE_PROCESS, b"\x02\x01")
        assert req.kwargs == {"timeout": 30.0, "retries": 0, "motor": True, "quiet": False}

    def test_check_in(self):
        unit, _ = unit_with()
        unit.online = True
        unit.retrude_stage(0x00, 0x01)
        assert unit.box.sent[0].kwargs == {"timeout": 5.0, "retries": 0, "motor": False,
                                           "quiet": False}

    def test_explicit_timeout(self):
        unit, _ = unit_with()
        unit.online = True
        unit.retrude_stage(0x02, 0x00, timeout=1.0)
        assert unit.box.sent[0].kwargs["timeout"] == 1.0


class TestAfcCFSReadMaterialMask:
    def test_reads_slot_bits(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, frame(P.GET_FILAMENT_SENSOR_STATE, b"\xf5"))
        assert unit.read_material_mask() == 0x05
        assert unit.material_mask == 0x05
        assert unit.box.keys() == [(1, P.GET_FILAMENT_SENSOR_STATE, b"\x00")]

    def test_empty_reply(self):
        unit, _ = unit_with()
        unit.online = True
        unit.material_mask = 0x03
        assert unit.read_material_mask() is None
        assert unit.material_mask == 0x03

    def test_no_reply(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        assert unit.read_material_mask() is None


class TestAfcCFSReadConnectedMask:
    def test_reads_slot_bits(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, frame(P.GET_FILAMENT_SENSOR_STATE, b"\x12"))
        assert unit.read_connected_mask() == 0x02
        assert unit.connected_mask == 0x02
        assert unit.box.keys() == [(1, P.GET_FILAMENT_SENSOR_STATE, b"\x01")]

    def test_empty_reply(self):
        unit, _ = unit_with()
        unit.online = True
        assert unit.read_connected_mask() is None
        assert unit.connected_mask is None

    def test_no_reply(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        assert unit.read_connected_mask() is None


class TestAfcCFSReadBufferState:
    def test_reads_state(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.reply(P.GET_BUFFER_STATE, frame(P.GET_BUFFER_STATE, b"\x02"))
        assert unit.read_buffer_state() == P.BUFFER_EMPTY
        assert unit.buffer_state == P.BUFFER_EMPTY

    def test_empty_reply(self):
        unit, _ = unit_with()
        unit.online = True
        assert unit.read_buffer_state() is None
        assert unit.buffer_state is None

    def test_no_reply(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        assert unit.read_buffer_state() is None


class TestAfcCFSReadMeasuringWheel:
    def test_reads_position(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.reply(P.GET_MEASURING_WHEEL, frame(P.GET_MEASURING_WHEEL,
                                                    bytes.fromhex("c5afa071")))
        assert unit.read_measuring_wheel() == pytest.approx(-5620.055, abs=1e-3)
        assert unit.box.keys() == [(1, P.GET_MEASURING_WHEEL, b"\x01")]

    def test_no_reply(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        assert unit.read_measuring_wheel() is None


class TestAfcCFSReadBoxStatus:
    def test_reads_status(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, status=P.ST_EXTRUDE_ERR8))
        assert unit.read_box_status() == P.ST_EXTRUDE_ERR8
        assert unit.box_status == P.ST_EXTRUDE_ERR8

    def test_no_reply(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        assert unit.read_box_status() is None
        assert unit.box_status is None


class TestAfcCFSClearLatchedError:
    def test_sends_idle(self):
        unit, _ = unit_with()
        unit.online = True
        unit.clear_latched_error()
        assert unit.box.keys() == [(1, P.SET_BOX_MODE, b"\x00\x01")]


# Operations

class TestAfcCFSOperationInit:
    def test_keeps_the_unit(self):
        unit, _ = unit_with()
        assert afcCFS._Operation(unit).unit is unit


class TestAfcCFSOperationEnter:
    def test_waits_for_tag_reads_then_counts(self):
        unit, _ = unit_with()
        order = []
        unit.wait_for_tag_reads = lambda: order.append(unit._operation_depth)
        assert afcCFS._Operation(unit).__enter__() is unit
        assert order == [0]
        assert unit._operation_depth == 1


class TestAfcCFSOperationExit:
    def test_counts_down_and_lets_exceptions_through(self):
        unit, _ = unit_with()
        unit.wait_for_tag_reads = lambda: None
        with pytest.raises(RuntimeError):
            with unit.operation():
                assert unit.bus.operation_active is True
                raise RuntimeError("load failed")
        assert unit._operation_depth == 0
        assert unit.bus.operation_active is False


class TestAfcCFSOperation:
    def test_returns_a_context_for_this_unit(self):
        unit, _ = unit_with()
        op = unit.operation()
        assert isinstance(op, afcCFS._Operation)
        assert op.unit is unit


class TestAfcCFSWaitForTagReads:
    def test_nothing_running(self):
        unit, _ = unit_with()
        unit.wait_for_tag_reads()
        assert unit.reactor.pauses == []
        assert msgs(unit) == []

    def test_waits_for_another_box_on_the_chain(self):
        printer = make_printer()
        a, _ = unit_with(name="CFS_1", printer=printer)
        b, _ = unit_with(name="CFS_2", printer=printer, values={"box_addr": 2})
        b._rfid_busy = True
        a.reactor.on_pause.append(
            lambda now: setattr(b, "_rfid_busy", len(a.reactor.pauses) < 3))
        a.wait_for_tag_reads()
        assert a.reactor.pauses == pytest.approx([1000.2, 1000.4, 1000.6])
        assert msgs(a) == [("info", "CFS CFS_1: waiting for an RFID tag read to finish")]
        assert a.trace.events == [("wait_for_tag_read", {}),
                                  ("tag_read_done", {"waited_s": 0.6})]

    def test_gives_up_after_the_limit(self):
        unit, _ = unit_with()
        unit._rfid_busy = True
        unit.wait_for_tag_reads()
        # rfid_busy_timeout + resync_timeout + attempts * (2 + retry interval) + 10
        limit = 150.0 + 60.0 + 3 * (2.0 + 5.0) + 10.0
        assert msgs(unit)[-1] == ("warning", f"CFS CFS_1: tag read still running after "
                                             f"{limit:.0f}s; continuing")
        assert unit.reactor.now == pytest.approx(1000.0 + limit + 0.2, abs=0.25)


# Polling

class TestAfcCFSPoll:
    def _unit(self, values=None):
        unit, lanes = unit_with(values=values, lanes=(1, 2))
        unit.online = True
        unit._queue_startup_tag_reads = Recorder()
        unit._trace_print_state = Recorder()
        unit.box.on(P.GET_FILAMENT_SENSOR_STATE,
                    lambda req: frame(P.GET_FILAMENT_SENSOR_STATE,
                                      b"\x01" if req.data == b"\x00" else b"\x02"))
        unit.box.reply(P.GET_BUFFER_STATE, frame(P.GET_BUFFER_STATE, b"\x00"))
        return unit, lanes

    def test_skipped_while_a_box_runs_a_sequence(self):
        unit, _ = self._unit()
        unit._operation_depth = 1
        assert unit._poll(500.0) == 501.0
        assert unit.box.sent == []

    def test_skipped_while_the_line_is_busy(self):
        unit, _ = self._unit()
        unit.transport._mutex.locked = True
        assert unit._poll(500.0) == 501.0
        assert unit.box.sent == []

    def test_full_poll(self):
        unit, lanes = self._unit()
        quiet = []
        orig = unit.box.request

        def spy(addr, fn, data=b"", **kw):
            quiet.append(unit._quiet_requests)
            return orig(addr, fn, data, **kw)
        unit.transport.request = spy
        assert unit._poll(500.0) == unit.reactor.monotonic() + 1.0
        assert unit.box.keys() == [
            (1, P.CMD_ONLINE_CHECK, b""),
            (1, P.GET_FILAMENT_SENSOR_STATE, b"\x00"),
            (1, P.GET_FILAMENT_SENSOR_STATE, b"\x01"),
            (1, P.GET_BUFFER_STATE, b""),
        ]
        assert set(quiet) == {True}
        assert unit._quiet_requests is False
        assert unit._last_heartbeat == 500.0
        assert unit.present_last == {"lane1": True, "lane2": False}
        assert (lanes[0]._load_state, lanes[1]._load_state) == (False, True)
        assert unit.buffer_state == P.BUFFER_MIDDLE
        assert unit._queue_startup_tag_reads.called
        assert unit._trace_print_state.called

    def test_trace_polls_keeps_requests_traced(self):
        unit, _ = self._unit({"trace_polls": True})
        seen = []
        orig = unit.box.request

        def spy(addr, fn, data=b"", **kw):
            seen.append(unit._quiet_requests)
            return orig(addr, fn, data, **kw)
        unit.transport.request = spy
        unit._poll(500.0)
        assert set(seen) == {False}

    def test_heartbeat_not_due(self):
        unit, _ = self._unit()
        unit._last_heartbeat = 499.0
        unit._poll(500.0)
        assert P.CMD_ONLINE_CHECK not in unit.box.fns()

    def test_heartbeat_interval_while_printing(self):
        unit, _ = self._unit()
        unit.printer._objects["print_stats"] = PrintStats("printing")
        unit._last_heartbeat = 492.0                        # 8 s ago: under the 10 s
        unit._poll(500.0)
        assert P.CMD_ONLINE_CHECK not in unit.box.fns()
        unit._poll(502.0)
        assert unit.box.fns().count(P.CMD_ONLINE_CHECK) == 1

    def test_no_sensor_replies(self):
        unit, _ = self._unit()
        unit.box.on(P.GET_FILAMENT_SENSOR_STATE, lambda req: None)
        unit._poll(500.0)
        assert unit.present_last == {"lane1": None, "lane2": None}

    def test_error_is_logged_and_polling_goes_on(self):
        unit, _ = self._unit()
        unit.read_buffer_state = Recorder(raises=RuntimeError("boom"))
        assert unit._poll(500.0) == unit.reactor.monotonic() + 1.0
        level, text = msgs(unit)[-1]
        assert level == "debug"
        assert text.startswith("CFS CFS_1: poll failed\nTraceback")
        assert text.rstrip().endswith("RuntimeError: boom")
        assert unit._quiet_requests is False
        assert unit._trace_print_state.called

    def test_offline_reconnects(self):
        unit, _ = self._unit()
        unit.online = False
        calls = []
        unit.connect = lambda: (calls.append(1), setattr(unit, "online", True))
        unit._poll(500.0)
        assert calls == [1]
        assert unit._last_reconnect_attempt == 500.0
        assert P.GET_BUFFER_STATE in unit.box.fns()          # went on with the poll

    def test_offline_reconnect_fails(self):
        unit, _ = self._unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        assert unit._poll(500.0) == unit.reactor.monotonic() + 1.0
        assert unit.connect.called
        assert unit.box.sent == []

    def test_offline_reconnect_raises(self):
        unit, _ = self._unit()
        unit.online = False
        unit.connect = Recorder(raises=OSError("port gone"))
        unit._poll(500.0)
        level, text = msgs(unit)[-1]
        assert level == "debug"
        assert text.startswith("CFS CFS_1: reconnect failed\nTraceback")
        assert text.rstrip().endswith("OSError: port gone")

    def test_offline_waits_between_reconnects(self):
        unit, _ = self._unit()
        unit.online = False
        unit._last_reconnect_attempt = 497.0
        unit.connect = Recorder()
        unit._poll(500.0)
        assert not unit.connect.called


class TestAfcCFSMirrorPresence:
    def test_traces_changes_and_updates_lanes(self):
        unit, lanes = unit_with(lanes=(1, 2, 3))
        seen = []
        unit.update_lane_presence = lambda lane, present: seen.append((lane.name, present))
        unit._mirror_presence(0b0101)
        unit._mirror_presence(0b0101)
        assert seen == [("lane1", True), ("lane2", False), ("lane3", True)] * 2
        assert unit.trace.events == [("presence", {"mask": 5, "slots": "AC", "previous": None})]
        assert unit._traced_mask == 5

    def test_lane_with_a_bad_slot_is_skipped(self):
        unit, (lane1, lane2) = unit_with(lanes=(1, 2))
        lane1.index = 7
        seen = []
        unit.update_lane_presence = lambda lane, present: seen.append(lane.name)
        unit._mirror_presence(0b0011)
        assert seen == ["lane2"]


class TestAfcCFSMirrorConnections:
    def test_sets_load_sensors(self):
        unit, lanes = unit_with(lanes=(1, 2))
        unit._mirror_connections(0b0010)
        assert [lane._load_state for lane in lanes] == [False, True]


# PREP

class TestAfcCFSSystemTest:
    def _unit(self, online=True, material=0b0001, connected=0):
        unit, (lane,) = unit_with()
        unit.online = online
        unit.material_mask = material
        unit.connected_mask = connected
        unit.apply_lane_state = Recorder(result="<span>READY</span>")
        unit.lane_not_ready = Recorder()
        unit.connect = Recorder()
        unit._wait_for_box = Recorder()
        unit.afc.function.TcmdAssign.reset_mock()
        return unit, lane

    def test_online_lane_with_spool(self):
        unit, lane = self._unit()
        assert unit.system_Test(lane, 0, True, False) is True
        assert unit.apply_lane_state.calls == [((lane, True), {})]
        assert unit.present_last["lane1"] is True
        unit.afc.function.TcmdAssign.assert_called_once_with(lane)
        assert lane._afc_prep_done is True
        assert unit._boot_wait_done is True
        assert msgs(unit) == [("info", f"lane1 tool cmd: {tool_cmd(lane):3} "
                                       f"<span>READY</span>")]
        assert not unit.connect.called
        assert unit.box.sent == []

    def test_online_empty_slot(self):
        unit, lane = self._unit(material=0b0010)
        unit.system_Test(lane, 0, False, False)
        assert unit.apply_lane_state.calls == [((lane, False), {})]
        assert not unit.afc.function.TcmdAssign.called

    def test_reads_unknown_masks(self):
        unit, lane = self._unit(material=None, connected=None)
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE,
                       frame(P.GET_FILAMENT_SENSOR_STATE, b"\x01"),
                       frame(P.GET_FILAMENT_SENSOR_STATE, b"\x00"))
        unit.system_Test(lane, 0, False, False)
        assert unit.box.keys() == [(1, P.GET_FILAMENT_SENSOR_STATE, b"\x00"),
                                   (1, P.GET_FILAMENT_SENSOR_STATE, b"\x01")]
        assert (unit.material_mask, unit.connected_mask) == (1, 0)

    def test_mask_still_unknown(self):
        unit, lane = self._unit(material=None)
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, None)
        unit.system_Test(lane, 0, False, False)
        assert unit.apply_lane_state.calls == [((lane, False), {})]

    def test_offline_connects_and_waits_once(self):
        unit, lane = self._unit(online=False)
        assert unit.system_Test(lane, 0, False, False) is False
        assert unit.connect.called
        assert unit._wait_for_box.call_count == 1
        assert unit.present_last["lane1"] is None
        assert unit.lane_not_ready.calls == [((lane,), {})]
        assert msgs(unit) == [("info", f"lane1 tool cmd: {tool_cmd(lane):3} "
                                       f"<span class=error--text>CFS NOT RESPONDING</span>")]
        unit.system_Test(lane, 0, False, False)
        assert unit._wait_for_box.call_count == 1        # boot wait only once

    def test_box_comes_up_during_connect(self):
        unit, lane = self._unit(online=False)
        unit.connect = lambda: setattr(unit, "online", True)
        assert unit.system_Test(lane, 0, False, False) is True
        assert not unit._wait_for_box.called
        assert unit.apply_lane_state.called

    def test_map_as_a_plain_string(self):
        # AFCProject main keeps lane.map as a string, without map_to_string()
        unit, lane = self._unit()
        lane.map_to_string = None
        lane.map = "T0"
        lane.send_lane_data = Recorder()
        unit.system_Test(lane, 0, False, False)
        assert msgs(unit) == [("info", "lane1 tool cmd: T0  <span>READY</span>")]


class TestAfcCFSEnsurePrintMode:
    def test_offline(self):
        unit, (lane,) = unit_with()
        unit.ensure_print_mode(lane)
        assert unit.box.sent == []

    def test_no_reply(self):
        unit, (lane,) = unit_with()
        unit.online = True
        unit.box.reply(P.GET_BOX_STATE, None)
        unit.ensure_print_mode(lane)
        assert unit.box.fns() == [P.GET_BOX_STATE]

    def test_short_reply(self):
        unit, (lane,) = unit_with()
        unit.online = True
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, b"\x11\x22\x08"))
        unit.ensure_print_mode(lane)
        assert unit.box.fns() == [P.GET_BOX_STATE]

    def test_already_in_print_mode(self):
        unit, (lane,) = unit_with()
        unit.online = True
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, b"\x11\x22\x08\x02"))
        unit.ensure_print_mode(lane)
        assert unit.box.fns() == [P.GET_BOX_STATE]

    def test_restores_print_mode(self):
        unit, (_, lane3) = unit_with(lanes=(1, 3))
        unit.online = True
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, b"\x11\x22\x08\x00"))
        unit.ensure_print_mode(lane3)
        assert unit.box.keys() == [(1, P.GET_BOX_STATE, b""), (1, P.SET_BOX_MODE, b"\x04\x00")]
        assert unit.trace.events == [("print_mode_restored", {"lane": "lane3", "slot": "C",
                                                              "box_state": "11220800"})]
        assert msgs(unit) == [("debug", "CFS CFS_1: slot C is loaded in the toolhead; set print "
                                        "mode again (box state was 11220800)")]


class TestAfcCFSWaitForBox:
    def test_no_boot_wait(self):
        unit, _ = unit_with(values={"boot_wait": 0})
        unit.connect = Recorder()
        unit._wait_for_box()
        assert not unit.connect.called
        assert msgs(unit) == []

    def test_box_comes_up(self):
        unit, _ = unit_with()
        tries = []

        def connect():
            tries.append(unit.reactor.now)
            unit.online = len(tries) == 2
        unit.connect = connect
        unit._wait_for_box()
        assert tries == [1001.0, 1002.0]
        assert msgs(unit) == [("info", "CFS CFS_1: waiting up to 20s for the box to start")]

    def test_gives_up_at_the_deadline(self):
        unit, _ = unit_with(values={"boot_wait": 3.0})
        unit.connect = Recorder()
        unit._wait_for_box()
        assert unit.connect.call_count == 3
        assert unit.reactor.now == 1003.0


# Lanes

class TestAfcCFSSetupLanes:
    def test_each_lane(self):
        unit, lanes = unit_with(lanes=(1, 2), do_connect=False)
        unit.lanes = {lane.name: lane for lane in lanes}
        seen = []
        unit._setup_lane = lambda lane: seen.append(lane.name)
        unit._setup_lanes()
        assert seen == ["lane1", "lane2"]


class TestAfcCFSSetupLane:
    def _unit(self):
        unit, (lane,) = unit_with(do_connect=False)
        unit.gcode.register_mux_command = Recorder()
        return unit, lane

    def test_plain_lane_becomes_a_cfs_lane(self):
        unit, lane = self._unit()
        lane._load_state = True
        unit._setup_lane(lane)
        assert lane._load_state is False
        assert (unit.present_last, unit.load_reports) == ({"lane1": None}, {"lane1": {}})
        assert lane.custom_load_cmd == "AFC_CFS_TOOL_LOAD LANE=lane1"
        assert lane.custom_unload_cmd == "AFC_CFS_TOOL_UNLOAD LANE=lane1"
        assert [c[0][:3] for c in unit.gcode.register_mux_command.calls] == [
            ("AFC_CFS_TOOL_LOAD", "LANE", "lane1"), ("AFC_CFS_TOOL_UNLOAD", "LANE", "lane1"),
            ("AFC_CFS_EJECT", "LANE", "lane1"), ("AFC_CFS_READ_TAG", "LANE", "lane1")]
        assert [c[1]["desc"] for c in unit.gcode.register_mux_command.calls] == [
            "Load a CFS lane into the toolhead",
            "Unload a CFS lane from the toolhead back to the box",
            "Reel a CFS lane's filament back into the box (box motors only)",
            "Read a CFS lane's RFID tag and set its color/material"]

    def test_registered_commands_call_the_handlers(self):
        unit, lane = self._unit()
        handlers = {}
        for name in ("TOOL_LOAD", "TOOL_UNLOAD", "EJECT", "READ_TAG"):
            rec = Recorder()
            handlers[name] = rec
            setattr(unit, f"cmd_AFC_CFS_{name}", rec)
        unit._setup_lane(lane)
        for (args, _), name in zip(unit.gcode.register_mux_command.calls, handlers):
            args[3]("gcmd")
            assert handlers[name].calls == [((lane, "gcmd"), {})]

    def test_users_own_commands_kept(self):
        unit, lane = self._unit()
        lane.custom_load_cmd = "MY_LOAD"
        lane.custom_unload_cmd = "MY_UNLOAD"
        unit._setup_lane(lane)
        assert (lane.custom_load_cmd, lane.custom_unload_cmd) == ("MY_LOAD", "MY_UNLOAD")

    def test_commands_registered_once(self):
        unit, lane = self._unit()
        unit._setup_lane(lane)
        unit._setup_lane(lane)
        assert unit.gcode.register_mux_command.call_count == 4
        assert unit._lane_commands == {"lane1"}

    def test_prep_pin_refused(self):
        unit, lane = self._unit()
        lane.prep = "PA0"
        with pytest.raises(P.CONFIG_ERROR) as e:
            unit._setup_lane(lane)
        assert str(e.value) == ("[AFC_lane lane1]: CFS lanes take filament presence from the box; "
                                "remove prep/load pins")

    def test_load_pin_refused(self):
        unit, lane = self._unit()
        lane.load = "PA1"
        with pytest.raises(P.CONFIG_ERROR) as e:
            unit._setup_lane(lane)
        assert str(e.value).endswith("remove prep/load pins")

    @pytest.mark.parametrize("index", [0, 5])
    def test_slot_out_of_range(self, index):
        unit, lane = self._unit()
        lane.index = index
        with pytest.raises(P.CONFIG_ERROR) as e:
            unit._setup_lane(lane)
        assert str(e.value) == (f"[AFC_lane lane1]: unit must be 'CFS_1:<1-4>' (slot A-D), "
                                f"got index {index}")


class TestAfcCFSPause:
    def test_pauses(self):
        unit, _ = unit_with()
        unit._pause(0.3)
        assert unit.reactor.pauses == [1000.3]

    def test_zero_is_no_pause(self):
        unit, _ = unit_with()
        unit._pause(0)
        assert unit.reactor.pauses == []


class TestAfcCFSEMove:
    def test_speed_converted_to_mm_per_s(self):
        unit, _ = unit_with()
        unit.afc.move_e_pos = Recorder()
        unit._e_move(-15, 600, "retract 1")
        assert unit.afc.move_e_pos.calls == [((-15, 10.0, "CFS retract 1"), {"wait_tool": True})]


class TestAfcCFSToolheadSensor:
    def test_triggered(self):
        unit, (lane,) = unit_with()
        lane.extruder_obj.tool_start_state = True
        assert afcCFS._toolhead_sensor(lane) is True

    def test_clear(self):
        unit, (lane,) = unit_with()
        assert afcCFS._toolhead_sensor(lane) is False


class TestAfcCFSCutter:
    def test_own_switch(self):
        unit, _ = unit_with()
        assert unit._cutter() is unit

    def test_chains_switch(self):
        printer = make_printer()
        a, _ = unit_with(name="CFS_1", printer=printer, values={"cutter_sensor_pin": "PA1"})
        b, _ = unit_with(name="CFS_2", printer=printer, values={"box_addr": 2})
        assert b._cutter() is a


class TestAfcCFSCutterEngaged:
    def test_no_switch(self):
        unit, _ = unit_with()
        unit.cutter_sensor_state = True
        assert unit._cutter_engaged() is False

    def test_closed(self):
        unit, _ = unit_with(values={"cutter_sensor_pin": "PA1"})
        unit.cutter_sensor_state = True
        assert unit._cutter_engaged() is True

    def test_open(self):
        unit, _ = unit_with(values={"cutter_sensor_pin": "PA1"})
        assert unit._cutter_engaged() is False


class TestAfcCFSLaneTrace:
    def test_adds_lane_and_slot(self):
        unit, (_, lane) = unit_with(lanes=(1, 2))
        unit._lane_trace(lane, "load_start", toolhead_sensor=False)
        assert unit.trace.events == [("load_start", {"lane": "lane2", "slot": "B",
                                                     "toolhead_sensor": False})]


class TestAfcCFSTraceBoxState:
    def test_reads_and_records(self):
        unit, (_, lane) = unit_with(lanes=(1, 2))
        unit.online = True
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, status=P.ST_EXTRUDE_ERR8))
        lane.extruder_obj.tool_start_state = True
        unit._trace_box_state(lane, "prime1")
        assert unit.box.keys() == [(1, P.GET_BOX_STATE, b"")]
        assert unit.trace.events == [("box_state", {"lane": "lane2", "slot": "B",
                                                    "point": "prime1", "status": "EXTRUDE_ERR8",
                                                    "toolhead_sensor": True})]

    def test_no_reply(self):
        unit, (lane,) = unit_with()
        unit.online = True
        unit.box.default_ok = False
        unit._trace_box_state(lane, "stage7")
        assert unit.trace.events == [("box_state", {"lane": "lane1", "slot": "A",
                                                    "point": "stage7", "status": "NO_REPLY",
                                                    "toolhead_sensor": False})]

    def test_nothing_sent_without_a_trace(self):
        unit, (lane,) = unit_with()
        unit.online = True
        unit.trace = RecTrace(enabled=False)
        unit._trace_box_state(lane, "toolhead")
        assert unit.box.sent == []
        assert unit.trace.events == []


class TestAfcCFSLaneFail:
    def test_logs_and_hands_to_afc(self):
        unit, (lane,) = unit_with()
        unit._lane_fail(lane, "stage 5 timed out")
        assert unit.trace.events == [("fail", {"lane": "lane1", "slot": "A",
                                               "message": "stage 5 timed out",
                                               "toolhead_sensor": False})]
        assert msgs(unit) == [("error", "CFS lane1 (slot A): stage 5 timed out")]
        unit.afc.error.handle_lane_failure.assert_called_once_with(
            lane, "CFS lane1: stage 5 timed out")


# Presence

class TestAfcCFSUpdateLanePresence:
    def _unit(self, previous=False, tool_loaded=False, printing=False):
        unit, (lane,) = unit_with()
        lane._afc_prep_done = True
        unit.present_last["lane1"] = previous
        lane.tool_loaded = tool_loaded
        unit.afc.function.is_printing.return_value = printing
        unit.afc.save_vars.reset_mock()
        unit.on_filament_insert = Recorder()
        unit.on_filament_remove = Recorder()
        unit.apply_lane_state = Recorder(result="msg")
        for name in ("set_loaded", "set_unloaded", "_perform_infinite_runout",
                     "_perform_pause_runout"):
            setattr(lane, name, Recorder())
        return unit, lane

    def test_unchanged(self):
        unit, lane = self._unit(previous=True)
        lane.prep_state = "untouched"
        unit.update_lane_presence(lane, True)
        assert lane.prep_state == "untouched"
        assert not unit.afc.save_vars.called

    def test_before_prep_only_saves(self):
        unit, lane = self._unit()
        lane._afc_prep_done = False
        unit.update_lane_presence(lane, True)
        assert (lane.prep_state, lane.loaded_to_hub) == (True, True)
        assert unit.present_last["lane1"] is True
        unit.afc.save_vars.assert_called_once_with()
        assert not unit.on_filament_insert.called
        assert msgs(unit) == []

    def test_first_reading_present(self):
        unit, lane = self._unit(previous=None)
        unit.update_lane_presence(lane, True)
        assert unit.apply_lane_state.calls == [((lane, True), {})]
        assert not lane.set_unloaded.called
        assert not unit.on_filament_insert.called
        assert msgs(unit) == [("debug", "CFS lane1: box online, slot A: msg")]
        unit.afc.save_vars.assert_called_once_with()

    def test_first_reading_empty(self):
        unit, lane = self._unit(previous=None)
        unit.update_lane_presence(lane, False)
        assert lane.set_unloaded.called
        assert not unit.on_filament_remove.called

    def test_first_reading_empty_but_in_the_toolhead(self):
        unit, lane = self._unit(previous=None, tool_loaded=True)
        unit.update_lane_presence(lane, False)
        assert not lane.set_unloaded.called

    def test_insert(self):
        unit, lane = self._unit()
        unit.update_lane_presence(lane, True)
        assert lane.set_loaded.called
        assert unit.on_filament_insert.calls == [((lane,), {})]
        assert msgs(unit) == [("debug", "CFS lane1: spool detected in slot A")]
        unit.afc.save_vars.assert_called_once_with()

    @pytest.mark.parametrize("state", [AFCLaneState.LOADED, AFCLaneState.TOOLED,
                                       AFCLaneState.TOOL_LOADED])
    def test_insert_already_loaded(self, state):
        unit, lane = self._unit()
        lane.status = state
        unit.update_lane_presence(lane, True)
        assert not lane.set_loaded.called
        assert unit.on_filament_insert.called

    def test_remove_idle_lane(self):
        unit, lane = self._unit(previous=True)
        unit.update_lane_presence(lane, False)
        assert unit.on_filament_remove.calls == [((lane,), {})]
        assert lane.set_unloaded.called
        assert msgs(unit) == [("debug", "CFS lane1: slot A is empty")]
        unit.afc.save_vars.assert_called_once_with()

    def test_remove_loaded_lane_not_printing(self):
        unit, lane = self._unit(previous=True, tool_loaded=True, printing=False)
        unit.update_lane_presence(lane, False)
        assert not lane.set_unloaded.called
        assert not lane._perform_pause_runout.called
        assert unit.trace.events == []

    def test_printing_but_lane_not_loaded(self):
        unit, lane = self._unit(previous=True, tool_loaded=False, printing=True)
        unit.update_lane_presence(lane, False)
        assert lane.set_unloaded.called
        assert not lane._perform_pause_runout.called

    def test_runout_pauses(self):
        unit, lane = self._unit(previous=True, tool_loaded=True, printing=True)
        unit.update_lane_presence(lane, False)
        assert lane._perform_pause_runout.called
        assert not lane._perform_infinite_runout.called
        assert unit.trace.events == [("runout", {"lane": "lane1", "slot": "A",
                                                 "runout_lane": None})]

    def test_runout_to_the_next_spool(self):
        unit, lane = self._unit(previous=True, tool_loaded=True, printing=True)
        lane.runout_lane = "lane2"
        unit.update_lane_presence(lane, False)
        assert lane._perform_infinite_runout.called
        assert not lane._perform_pause_runout.called
