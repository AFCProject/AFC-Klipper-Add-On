"""Unit tests for extras/AFC_ACE.py."""

from __future__ import annotations

import configparser
from datetime import datetime
import importlib.util
import json
import logging
import logging.handlers
import queue
import struct
import sys
import threading
import time
import types
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest

from extras.AFC_ACE import (
    _ace_extract_get_info_id,
    _ams_box_logo,
    _ams_box_logo_error,
    _derive_buffer_state,
    ACEConnection,
    ACESerialError,
    ACETimeoutError,
    afcACE,
    crc16_ccitt_reflected,
    load_config_prefix,
    resolve_ace_port_by_index,
    run_off_reactor,
)
import extras.AFC_ACE as afc_ace_module
from extras.AFC_ACE2 import ACE2Connection, Cmd, pb_uint32, v2_response_to_v1
import extras.AFC_ACE2 as afc_ace2_module
from extras.AFC_lane import AFCLaneState
from tests.ace_helpers import (
    ace_error,
    ace_status,
    AceConfig,
    AceGcmd,
    AceLogger,
    AcePrinter,
    capture_log,
    FakeSerial,
    Hook,
    LaneSpec,
    make_ace2_unit,
    make_ace_connection,
    make_ace_printer,
    make_ace_unit,
    make_gcmd,
    Recorder,
    reset_ace_globals,
    TIMEOUT,
)


def ace_p1_unit(*lanes: Any, printer: Optional[AcePrinter] = None, **kwargs: Any) -> afcACE:
    """
    A V1 ACE unit "Ace_1" with its lanes, on a new printer unless one is given.

    :param lanes: lane names (slot = position) or LaneSpecs
    :param printer: shared printer, a new one when None
    :param kwargs: make_ace_unit keywords
    :return afcACE: the unit
    """
    return make_ace_unit(lanes=lanes, printer=printer, **kwargs)


class AceP1Wire:
    """
    Drives a unit's scripted link like a unit moving filament: a feed or
    unwind shows its slot busy on the next get_status poll, unless a stop was
    sent first, and idle after that. fail() queues a refused move; on_move
    runs on each accepted one; stall() makes accepted moves report a
    feed_error until the next move.
    """
    _STOPS = ("stop_feed_filament", "stop_unwind_filament")
    _MOVES = ("feed_filament", "unwind_filament")

    def __init__(self, unit: afcACE, slots: Tuple[str, ...] = ("ready",) * 4,
                 on_move: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> None:
        """
        :param unit: unit whose scripted link to drive
        :param slots: each slot's idle status
        :param on_move: called with (method, params) for each accepted move
        """
        self.link = unit._ace
        self.slots = list(slots)
        self.on_move = on_move
        self.failures: Dict[str, List[BaseException]] = {}
        self.stalls: List[bool] = []
        self._moving: Optional[Tuple[str, int, int]] = None
        self._error_slot: Optional[int] = None
        for method in self._MOVES:
            self.link.set_reply(method, self._mover(method))
        self.link.set_reply("get_status", self._status)

    def fail(self, method: str, *errors: BaseException) -> None:
        """
        :param method: "feed_filament" or "unwind_filament"
        :param errors: raised by the next calls of that method, in order
        """
        self.failures.setdefault(method, []).extend(errors)

    def stall(self, *flags: bool) -> None:
        """
        :param flags: per accepted move, in order, whether its slot then
            reports slot_status "feed_error" (the encoder saw a stall)
        """
        self.stalls.extend(flags)

    def moves(self) -> List[Tuple[str, Dict[str, Any]]]:
        """
        :return list: the feed and unwind commands sent, accepted or not
        """
        return [(m, p) for m, p in self.link.commands if m in self._MOVES]

    def _mover(self, method: str) -> Callable[[Dict[str, Any]], Any]:
        """
        :param method: the move method the reply answers
        :return Callable: the scripted reply
        """
        def reply(params: Dict[str, Any]) -> Any:
            """
            :param params: the move's params
            :return Any: an empty result
            """
            pending = self.failures.get(method)
            if pending:
                raise pending.pop(0)
            self._moving = (method, params["index"], len(self.link.commands))
            stalled = self.stalls.pop(0) if self.stalls else False
            self._error_slot = params["index"] if stalled else None
            if self.on_move is not None:
                self.on_move(method, params)
            return {}
        return reply

    def _status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        :param params: get_status params, unused
        :return dict: the unit's status now
        """
        slots: List[Dict[str, Any]] = [{"index": i, "status": s}
                                       for i, s in enumerate(self.slots)]
        if self._error_slot is not None:
            slots[self._error_slot]["slot_status"] = "feed_error"
        moving, self._moving = self._moving, None
        if moving is not None:
            method, index, mark = moving
            since = [m for m, _ in self.link.commands[mark:-1]]
            if not any(m in self._STOPS for m in since):
                slots[index]["status"] = ("feeding" if method == "feed_filament"
                                          else "unwinding")
                return {"status": "busy", "slots": slots}
        return {"status": "ready", "slots": slots}


class AceP1BrokenLink:
    """A serial link whose connected read raises, failing whatever checks it first."""

    def __init__(self, message: str) -> None:
        """
        :param message: the RuntimeError text
        """
        self.message = message

    @property
    def connected(self) -> bool:
        """
        :return bool: never returns
        """
        raise RuntimeError(self.message)


class AceP1FixedNow(datetime):
    """datetime whose now() is fixed, so TD-1 compare times are stable."""

    @classmethod
    def now(cls, tz: Any = None) -> "AceP1FixedNow":
        """
        :param tz: ignored
        :return AceP1FixedNow: 2026-01-01 12:00:00 local time
        """
        return cls(2026, 1, 1, 12, 0, 0)


def ace_p1_td1_unit(monkeypatch: pytest.MonkeyPatch, *, loaded_to_hub: bool,
                    **values: Any) -> Tuple[afcACE, Any, Recorder]:
    """
    A unit whose TD-1 sees lane0's filament on the first read.

    :param monkeypatch: pins the module's clock to AceP1FixedNow
    :param loaded_to_hub: lane0 is staged at the hub
    :param values: lane0's [AFC_lane] options
    :return tuple: the unit, lane0 and the moonraker TD-1 fetch Recorder
    """
    monkeypatch.setattr(afc_ace_module, "datetime", AceP1FixedNow)
    unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=loaded_to_hub,
                                values=dict({"td1_device_id": "td1"}, **values)))
    fetch = Recorder(result={"td1": {"scan_time": "2099-01-01T00:00:00Z", "td": 1.5,
                                     "color": "#FF0000"}})
    unit.afc.moonraker = types.SimpleNamespace(get_td1_data=fetch)
    return unit, unit.lanes["lane0"], fetch


#: The TD-1 data line AFC logs on a read: the fetched devices and the compare time.
ACE_P1_TD1_DATA_LINE = ("Data: {'td1': {'scan_time': '2099-01-01T00:00:00Z', 'td': 1.5, "
                        "'color': '#FF0000'}}, Compare_time: 2026-01-01 12:00:00")


def ace_p2_unit(*lanes: Any, printer: Optional[AcePrinter] = None, **kwargs: Any) -> afcACE:
    """
    A V1 ACE unit "Ace_1" with its lanes, on a new printer unless one is given.

    :param lanes: lane names (slot = position) or LaneSpecs
    :param printer: shared printer, a new one when None
    :param kwargs: make_ace_unit keywords
    :return afcACE: the unit
    """
    return make_ace_unit(lanes=lanes, printer=printer, **kwargs)


class AceP2Wire:
    """
    Scripts a unit's link like a unit moving filament: the get_status poll
    after each accepted feed or unwind shows the unit busy, later polls ready.
    on_move runs on each accepted move.
    """
    _MOVES = ("feed_filament", "unwind_filament")

    def __init__(self, unit: afcACE,
                 on_move: Optional[Callable[[str, Dict[str, Any]], Any]] = None) -> None:
        """
        :param unit: unit whose scripted link to drive
        :param on_move: called with (method, params) for each move; it may raise
            to refuse the move
        """
        self.link = unit._ace
        self.on_move = on_move
        self._moving = False
        for method in self._MOVES:
            self.link.set_reply(method, self._mover(method))
        self.link.set_reply("get_status", self._status)

    def moves(self) -> List[Tuple[str, Dict[str, Any]]]:
        """
        :return list: the feed and unwind commands sent
        """
        return [(m, p) for m, p in self.link.commands if m in self._MOVES]

    def _mover(self, method: str) -> Callable[[Dict[str, Any]], Any]:
        """
        :param method: the move method the reply answers
        :return Callable: the scripted reply
        """
        def reply(params: Dict[str, Any]) -> Any:
            """
            :param params: the move's params
            :return Any: an empty result
            """
            if self.on_move is not None:
                self.on_move(method, params)
            self._moving = True
            return {}
        return reply

    def _status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        :param params: get_status params, unused
        :return dict: busy once after a move, ready otherwise
        """
        moving, self._moving = self._moving, False
        return ace_status("ready", "ready", "ready", "ready",
                          status="busy" if moving else "ready")


class AceP2ThreadedCompletion:
    """A reactor completion whose wait keeps the reactor turning until done."""

    def __init__(self, reactor: "AceP2ThreadedReactor") -> None:
        """
        :param reactor: reactor that turns while this is waited on
        """
        self._reactor = reactor
        self._done = False
        self._result: Any = None
        self.completed_on: Optional[int] = None

    def complete(self, result: Any) -> None:
        """
        :param result: value wait() returns; the completing thread is recorded
        """
        self.completed_on = threading.get_ident()
        self._result = result
        self._done = True

    def wait(self, waketime: Optional[float] = None, waketime_result: Any = None) -> Any:
        """
        :param waketime: deadline on the monotonic clock
        :param waketime_result: value returned on timeout
        :return Any: the completed result or waketime_result
        """
        self._reactor.run_until(lambda: self._done, waketime)
        return self._result if self._done else waketime_result


class AceP2ThreadedReactor:
    """
    Reactor with Klipper's async-callback contract: a callback posted from a
    worker thread runs on the reactor thread, and a completion wait keeps the
    reactor turning (counted in .ticks, on_tick runs each turn) until it
    completes or its deadline passes. Timers are only recorded.
    """
    NEVER = 9999999999999999.0

    def __init__(self) -> None:
        """
        Start with nothing posted.
        """
        self._posted: "queue.Queue[Callable[[float], Any]]" = queue.Queue()
        self.ticks = 0
        self.on_tick: Optional[Callable[[], None]] = None
        self.completions: List[AceP2ThreadedCompletion] = []
        self.timers: List[Tuple[Callable[[float], float], float]] = []

    def monotonic(self) -> float:
        """
        :return float: the wall monotonic clock
        """
        return time.monotonic()

    def register_async_callback(self, callback: Callable[[float], Any],
                                waketime: Optional[float] = None) -> None:
        """
        :param callback: run on the reactor thread
        :param waketime: ignored
        """
        self._posted.put(callback)

    def completion(self) -> AceP2ThreadedCompletion:
        """
        :return AceP2ThreadedCompletion: a new completion
        """
        completion = AceP2ThreadedCompletion(self)
        self.completions.append(completion)
        return completion

    def register_fd(self, fd: int, callback: Callable[[float], None]) -> Tuple[str, int]:
        """
        :param fd: file descriptor
        :param callback: read handler
        :return tuple: the handle
        """
        return ("fd", fd)

    def unregister_fd(self, handle: Any) -> None:
        """
        :param handle: handle from register_fd
        """

    def register_timer(self, callback: Callable[[float], float],
                       waketime: float) -> Tuple[Callable[[float], float], float]:
        """
        :param callback: timer function
        :param waketime: first due time
        :return tuple: the handle
        """
        self.timers.append((callback, waketime))
        return self.timers[-1]

    def run_until(self, done: Callable[[], bool], waketime: Optional[float]) -> None:
        """
        Turn the reactor until done() or the deadline.

        :param done: stop condition
        :param waketime: deadline, None for none
        """
        while not done():
            if waketime is not None and self.monotonic() >= waketime:
                return
            self.ticks += 1
            if self.on_tick is not None:
                self.on_tick()
            try:
                callback = self._posted.get(timeout=0.01)
            except queue.Empty:
                continue
            callback(self.monotonic())


def ace_p2_answer(conn: ACEConnection, serial: FakeSerial, *responses: Dict[str, Any]) -> None:
    """
    Make the unit behind serial answer each request conn sends and waits on,
    with the next response (the last repeats), its id filled in. A write with
    no request pending (an async send) is not answered.

    :param conn: connection whose pending requests are answered
    :param serial: the connection's fake port
    :param responses: response dicts without their id; none answers nothing
    """
    script = list(responses)

    def on_write(frame: bytes) -> None:
        """
        :param frame: the frame written, unused
        """
        if not conn._pending or not script:
            return
        response = dict(script.pop(0) if len(script) > 1 else script[0])
        response["id"] = max(conn._pending)
        conn._handle_response(response)
    serial.on_write = on_write


class AceP2SerialPort:
    """
    The pyserial module, for sys.modules["serial"]: Serial(...) records the
    port, then opens a FakeSerial whose unit answers conn's requests with the
    given responses (see ace_p2_answer), or raises open_error.
    """

    def __init__(self, *responses: Dict[str, Any],
                 open_error: Optional[BaseException] = None) -> None:
        """
        :param responses: answers to the requests sent once open
        :param open_error: raised by Serial() when set
        """
        self.conn: Optional[ACEConnection] = None
        self.responses = responses
        self.open_error = open_error
        self.opened: List[str] = []
        self.ports: List[FakeSerial] = []

    def Serial(self, port: str, baudrate: int, timeout: float,
               write_timeout: float) -> FakeSerial:
        """
        :param port: tty path
        :param baudrate: baud rate
        :param timeout: read timeout
        :param write_timeout: write timeout
        :return FakeSerial: the opened port
        """
        self.opened.append(port)
        if self.open_error is not None:
            raise self.open_error
        serial = FakeSerial()
        if self.conn is not None:
            ace_p2_answer(self.conn, serial, *self.responses)
        self.ports.append(serial)
        return serial


def ace_p2_connection(**kwargs: Any) -> ACEConnection:
    """
    A real V1 ACEConnection on /dev/ttyACM0, on a new printer's reactor and
    logger (conn._logger.messages), connected to a FakeSerial unless told not.

    :param kwargs: make_ace_connection keywords
    :return ACEConnection: the connection
    """
    return make_ace_connection(printer=make_ace_printer(), **kwargs)


def ace_p2_payloads(conn: ACEConnection) -> List[bytes]:
    """
    The JSON payloads conn has written, each frame's header, length, CRC and
    footer stripped.

    :param conn: a connection on a FakeSerial
    :return list: one payload per frame, in write order
    """
    return [frame[4:-3] for frame in conn._serial.frames]


# ═════════════════════════════════════════════════════════════════════════
# Module-level import guards
# ═════════════════════════════════════════════════════════════════════════

def _exec_afc_ace_with_blocked_dependency(blocked_module_name: str) -> None:
    """Execute a throw-away copy of extras/AFC_ACE.py's module-level code with
    `blocked_module_name` forced to fail import, to exercise the file's top-level
    ``try: from X import Y / except: raise error(...)`` guards.

    This never touches the real, already-imported ``extras.AFC_ACE`` module that
    the rest of this test suite depends on: the copy is loaded under a throwaway
    module name and discarded afterward, whether or not it raises. Blocking an
    import via ``sys.modules[name] = None`` makes any import of that name raise
    ImportError immediately, without touching the module itself.

    :param blocked_module_name: dotted module name to block
    """
    import extras.AFC_ACE as real_module
    fresh_name = "extras.AFC_ACE_import_guard_probe"
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
    """Covers the three module-level `try/except: raise error(...)` guards in
    AFC_ACE.py, one per dependency import."""

    def test_afc_utils_error_str_import_failure_raises_configparser_error(self):
        """The very first guard imports ERROR_STR itself from AFC_utils, so it
        can't use ERROR_STR.format(...) in its own except clause."""
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_ace_with_blocked_dependency("extras.AFC_utils")
        assert str(exc_info.value).startswith(
            "Error when trying to import AFC_utils.ERROR_STR")

    def test_afc_lane_import_failure_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_ace_with_blocked_dependency("extras.AFC_lane")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_lane, please rerun install-afc.sh")

    def test_afc_unit_import_failure_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            _exec_afc_ace_with_blocked_dependency("extras.AFC_unit")
        assert str(exc_info.value).startswith(
            "Error trying to import AFC_unit, please rerun install-afc.sh")


class TestAfcACEInit:
    def test_invalid_mode_raises_configparser_error(self):
        with pytest.raises(configparser.Error) as exc_info:
            ace_p1_unit("lane0", values={"mode": "bogus"})
        assert str(exc_info.value) == (
            "[AFC_ACE Ace_1] invalid mode 'bogus'. Use 'combined' or 'direct'.")

    def test_temperature_ace_registration_failure_is_logged(self, caplog):
        printer = make_ace_printer()
        printer.heaters.add_sensor_factory = Recorder(raises=RuntimeError("heaters boom"))

        with caplog.at_level(logging.INFO):
            ace_p1_unit("lane0", printer=printer)

        assert [r.getMessage() for r in caplog.records] == [
            "AFC_ACE: temperature_ace factory not registered (heaters boom); "
            "use [temperature_ace <name>] config sections instead"]


class TestAmsBoxLogo:
    def test_short_title_uses_min_bay_width(self):
        logo = _ams_box_logo("ACE", 4, "myace")

        assert logo == ("<span class=success--text>"
                        "R  +---------------+\n"
                        "E  |      ACE      |\n"
                        "A  +---+---+---+---+\n"
                        "D  | O | O | O | O |\n"
                        "Y  +---+---+---+---+"
                        "</span>\n   myace\n")

    def test_long_title_grows_bay_width(self):
        logo = _ams_box_logo("SUPERLONGTITLE", 1, "u")

        assert logo == ("<span class=success--text>"
                        "R  +--------------+\n"
                        "E  |SUPERLONGTITLE|\n"
                        "A  +--------------+\n"
                        "D  |      O       |\n"
                        "Y  +--------------+"
                        "</span>\n   u\n")

    def test_zero_slots_defaults_to_one(self):
        logo = _ams_box_logo("X", 0, "u")

        assert logo == ("<span class=success--text>"
                        "R  +---+\n"
                        "E  | X |\n"
                        "A  +---+\n"
                        "D  | O |\n"
                        "Y  +---+"
                        "</span>\n   u\n")


class TestAmsBoxLogoError:
    def test_error_banner_and_min_width(self):
        logo = _ams_box_logo_error("ACE", 4, "myace")

        assert logo == ("<span class=error--text>"
                        "E  +---------------+\n"
                        "R  |      ACE      |\n"
                        "R  +---------------+\n"
                        "O  |    X ERROR    |\n"
                        "R  +---------------+"
                        "</span>\n   myace\n")

    def test_error_width_floors_at_error_banner(self):
        logo = _ams_box_logo_error("A", 1, "u")

        assert logo == ("<span class=error--text>"
                        "E  +-------+\n"
                        "R  |   A   |\n"
                        "R  +-------+\n"
                        "O  |X ERROR|\n"
                        "R  +-------+"
                        "</span>\n   u\n")


class TestDeriveBufferState:
    def test_state_advancing_on_buf_back(self):
        state = _derive_buffer_state(
            {"insert": True, "empty": False, "buf_rst": False, "buf_back": True})

        assert state == "advancing"

    def test_state_rest_on_buf_rst(self):
        state = _derive_buffer_state(
            {"insert": True, "empty": False, "buf_rst": True, "buf_back": False})

        assert state == "rest"

    def test_state_neutral_when_extended(self):
        state = _derive_buffer_state(
            {"insert": True, "empty": False, "buf_rst": False, "buf_back": False})

        assert state == "neutral"

    def test_state_buf_back_wins_over_rst(self):
        assert _derive_buffer_state({"buf_rst": True, "buf_back": True}) == "advancing"

    def test_state_empty_without_data(self):
        assert _derive_buffer_state(None) == ""
        assert _derive_buffer_state({}) == ""

    def test_state_decode_roundtrip(self):
        # Bit 3 is slot 0's buf_back in the GET_SENSOR_STATE mask.
        decoded = v2_response_to_v1(Cmd.GET_SENSOR_STATE, 1, pb_uint32(1, 1 << 3))
        slot_sensors = decoded["result"]["slot_sensors"]

        assert _derive_buffer_state(slot_sensors[0]) == "advancing"
        assert _derive_buffer_state(slot_sensors[1]) == "neutral"


class TestAfcACEHandleToolLoaded:
    @staticmethod
    def _unit(mode: str) -> afcACE:
        """
        A unit with lane0/lane1 whose toolhead sensor sees filament.

        :param mode: "combined" or "direct"
        :return afcACE: the unit, no lane current
        """
        unit = ace_p1_unit("lane0", "lane1", values={"mode": mode}, current=None)
        unit.lanes["lane0"].extruder_obj.tool_start_state = True
        return unit

    def test_combined_ignores_unknown_lane(self):
        unit = self._unit("combined")
        other = ace_p1_unit("other", printer=make_ace_printer()).lanes["other"]

        unit._handle_tool_loaded(other)

        assert unit.afc.reactor.register_callback.calls == []
        assert unit.afc.reactor.pending == []
        assert unit.logger.messages == []

    def test_combined_schedules_for_our_lane(self):
        unit = self._unit("combined")

        unit._handle_tool_loaded(unit.lanes["lane1"])
        assert len(unit.afc.reactor.pending) == 1
        assert unit._ace.commands == []
        unit.afc.reactor.run_callbacks()

        assert unit._ace.commands == [("get_status", {}), ("start_feed_assist", {"index": 1})]
        assert unit._feed_assist_active == {1}
        assert unit.logger.messages == []

    def test_direct_uses_our_lane_payload(self):
        unit = self._unit("direct")
        unit.afc.current = "lane0"

        unit._handle_tool_loaded(unit.lanes["lane1"])
        # Deferred to a reactor callback, which still targets the payload lane.
        assert len(unit.afc.reactor.pending) == 1
        assert unit._ace.commands == []
        unit.afc.current = None
        unit.afc.reactor.run_callbacks()

        assert unit._ace.commands == [("get_status", {}), ("start_feed_assist", {"index": 1})]
        assert unit.logger.messages == []

    def test_direct_falls_back_to_extruder_lane_loaded(self):
        unit = self._unit("direct")
        extruder = unit.lanes["lane1"].extruder_obj
        extruder.lane_loaded = "lane1"

        unit._handle_tool_loaded(extruder)
        assert len(unit.afc.reactor.pending) == 1
        assert unit._ace.commands == []
        unit.afc.reactor.run_callbacks()

        assert unit._ace.commands == [("get_status", {}), ("start_feed_assist", {"index": 1})]
        assert unit._feed_assist_active == {1}
        assert unit.logger.messages == []

    def test_direct_falls_back_to_afc_current(self):
        unit = self._unit("direct")
        extruder = unit.lanes["lane1"].extruder_obj
        extruder.lane_loaded = None
        unit.afc.current = "lane1"

        unit._handle_tool_loaded(extruder)
        assert len(unit.afc.reactor.pending) == 1
        assert unit._ace.commands == []
        unit.afc.reactor.run_callbacks()

        assert unit._ace.commands == [("get_status", {}), ("start_feed_assist", {"index": 1})]
        assert unit._feed_assist_active == {1}
        assert unit.logger.messages == []


class TestAfcACEReconcileFeedAssist:
    @staticmethod
    def _unit(**kwargs: Any) -> afcACE:
        """
        A unit with lane0 (slot 0) and lane1 (slot 1); lane0's toolhead
        sensor sees filament and no lane is current.

        :param kwargs: make_ace_unit keywords
        :return afcACE: the unit
        """
        unit = ace_p1_unit("lane0", "lane1", current=None, **kwargs)
        unit.lanes["lane0"].extruder_obj.tool_start_state = True
        return unit

    def test_reconcile_stops_other_slots_then_starts_target(self):
        unit = self._unit(feed_assist_active=[1])

        unit._reconcile_feed_assist("lane0")

        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 1}),
                                      ("get_status", {}), ("start_feed_assist", {"index": 0})]
        assert unit._feed_assist_active == {0}
        assert unit.logger.messages == []

    def test_reconcile_does_not_start_before_filament_at_toolhead(self):
        unit = self._unit()
        unit.lanes["lane0"].extruder_obj.tool_start_state = False

        unit._reconcile_feed_assist("lane0")

        assert unit._ace.commands == []
        assert unit._feed_assist_active == set()
        assert unit.logger.messages == []

    def test_reconcile_sensor_exception_treated_as_not_at_toolhead(self):
        unit = self._unit()
        # A buffer toolhead sensor with no buffer raises on the read.
        unit.lanes["lane0"].extruder_obj.tool_start = "buffer"
        unit.lanes["lane0"].buffer_obj = None

        unit._reconcile_feed_assist("lane0")

        assert unit._ace.commands == []
        assert unit._feed_assist_active == set()
        assert unit.logger.messages == []

    def test_reconcile_respects_suppression(self):
        unit = self._unit()
        unit._assist_suppressed = {0}

        unit._reconcile_feed_assist("lane0")

        assert unit._ace.commands == []
        assert unit._feed_assist_active == set()
        assert unit._assist_suppressed == {0}
        assert unit.logger.messages == []

    def test_reconcile_already_active_does_not_restart(self):
        unit = self._unit(feed_assist_active=[0])

        unit._reconcile_feed_assist("lane0")

        assert unit._ace.commands == []
        assert unit._feed_assist_active == {0}
        assert unit.logger.messages == []

    def test_reconcile_assist_disabled_for_lane(self):
        unit = self._unit(values={"use_feed_assist": False})

        unit._reconcile_feed_assist("lane0")

        assert unit._ace.commands == []
        assert unit._feed_assist_active == set()
        assert unit.logger.messages == []

    def test_reconcile_lane_missing_from_afc_does_not_start(self):
        unit = self._unit()
        del unit.afc.lanes["lane0"]

        unit._reconcile_feed_assist("lane0")

        assert unit._ace.commands == []
        assert unit._feed_assist_active == set()
        assert unit.logger.messages == []

    def test_reconcile_lane_on_other_unit_stops_ours(self):
        unit = self._unit(feed_assist_active=[0])
        ace_p1_unit("other_lane", printer=unit.printer, name="Ace_2")
        unit._ace.clear()

        unit._reconcile_feed_assist("other_lane")

        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 0})]
        assert unit._feed_assist_active == set()
        assert unit.logger.messages == []

    def test_reconcile_unresolvable_name_leaves_assist_untouched(self):
        unit = self._unit(feed_assist_active=[0])

        unit._reconcile_feed_assist("ghost")

        assert unit._ace.commands == []
        assert unit._feed_assist_active == {0}
        assert unit.logger.messages == []

    def test_reconcile_none_name_returns_before_any_slot_map_lookup(self):
        """None name must hit the dedicated early return, not just happen to
        fall through to the same no-op as an unresolvable name string: spy on
        _slot_map.get to prove it is never even called."""
        class _SpyDict(dict):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.get_calls: list = []

            def get(self, *args: Any, **kwargs: Any) -> Any:
                self.get_calls.append((args, kwargs))
                return super().get(*args, **kwargs)

        unit = self._unit(feed_assist_active=[0])
        spy_slot_map = _SpyDict(unit._slot_map)
        unit._slot_map = spy_slot_map

        unit._reconcile_feed_assist(None)

        assert spy_slot_map.get_calls == []
        assert unit._ace.commands == []
        assert unit._feed_assist_active == {0}
        assert unit.logger.messages == []


class TestAfcACEHandleReady:
    def test_seeds_hub_state_and_schedules_deferred_connect(self):
        unit = ace_p1_unit(LaneSpec("lane0", tool_loaded=True), LaneSpec("lane1"))
        unit.lanes["lane0"]._load_state = False
        unit.lanes["lane1"]._load_state = True
        unit._first_connect_pending = False
        unit.afc.reactor.pending.clear()

        unit._handle_ready()

        # Virtual hub occupancy is derived from tool_loaded, not the stored flag.
        assert unit.lanes["lane0"]._load_state is True
        assert unit.lanes["lane1"]._load_state is False
        assert unit._first_connect_pending is True
        assert [e.callback for e in unit.afc.reactor.pending] == [unit._deferred_ace_connect]
        assert unit.logger.messages == []


class TestAfcACEHandleKlippyDisconnect:
    def test_disconnects_and_clears_an_active_link(self):
        unit = ace_p1_unit("lane0")
        link = unit._ace

        unit._handle_klippy_disconnect()

        assert link.lifecycle[-1] == "disconnect"
        assert unit._ace is None
        assert unit.logger.messages == []

    def test_no_link_is_a_no_op(self):
        unit = ace_p1_unit("lane0")
        unit._ace = None

        unit._handle_klippy_disconnect()

        assert unit._ace is None
        assert unit.logger.messages == []

    def test_disconnect_exception_is_suppressed(self):
        unit = ace_p1_unit("lane0")
        unit._ace.disconnect = Recorder(raises=RuntimeError("boom"))

        unit._handle_klippy_disconnect()

        assert unit._ace is None
        assert unit.logger.messages == []


class TestAfcACECreateSerialLogger:
    @pytest.fixture(autouse=True)
    def _isolated(self) -> Iterator[None]:
        """Give each test a fresh process-wide serial-file logger."""
        reset_ace_globals()
        yield
        reset_ace_globals()

    def test_returns_none_without_a_log_file(self):
        unit = ace_p1_unit("lane1")
        file_logger = logging.getLogger("AFC_ACE_serial_file")

        assert unit._create_serial_logger() is None
        assert file_logger.handlers == []
        assert file_logger.propagate is False
        assert file_logger.level == logging.DEBUG
        assert "_serial_ql" not in vars(unit)

    def test_it_rotates_at_10mb_with_one_backup(self, tmp_path):
        printer = make_ace_printer(log_file=str(tmp_path / "klippy.log"))
        unit = ace_p1_unit("lane1", printer=printer)

        file_logger = unit._create_serial_logger()

        assert file_logger is logging.getLogger("AFC_ACE_serial_file")
        assert len(file_logger.handlers) == 1
        assert isinstance(file_logger.handlers[0], logging.handlers.QueueHandler)
        file_handler = unit._serial_ql.handlers[0]
        assert isinstance(file_handler, logging.handlers.RotatingFileHandler)
        assert file_handler.baseFilename == str(tmp_path / "AFC_ACE_serial.log")
        assert file_handler.maxBytes == 10485760
        assert file_handler.backupCount == 1

    def test_no_log_file_arg_yields_no_logger(self):
        printer = make_ace_printer(log_file="")
        unit = ace_p1_unit("lane1", printer=printer)

        assert unit._create_serial_logger() is None
        assert logging.getLogger("AFC_ACE_serial_file").handlers == []

    def test_an_existing_logger_is_handed_back(self):
        unit = ace_p1_unit("lane1")

        with capture_log("AFC_ACE_serial_file") as capture:
            file_logger = unit._create_serial_logger()
            handlers = list(file_logger.handlers)

        assert file_logger is logging.getLogger("AFC_ACE_serial_file")
        assert handlers == [capture]
        assert "_serial_ql" not in vars(unit)

    def test_returns_none_when_handler_setup_raises(self, tmp_path, monkeypatch):
        printer = make_ace_printer(log_file=str(tmp_path / "klippy.log"))
        unit = ace_p1_unit("lane1", printer=printer)
        monkeypatch.setattr(logging.handlers, "RotatingFileHandler",
                            Recorder(raises=OSError("disk full")))

        assert unit._create_serial_logger() is None
        assert logging.getLogger("AFC_ACE_serial_file").handlers == []
        assert "_serial_ql" not in vars(unit)


class TestAfcACEDeferredAceConnect:
    @pytest.fixture(autouse=True)
    def _isolated(self) -> Iterator[None]:
        """Give each test a fresh process-wide serial-file logger."""
        reset_ace_globals()
        yield
        reset_ace_globals()

    @staticmethod
    def _connect(unit: afcACE) -> None:
        """
        Run the deferred connect with the unit's connections routed to scripted links.

        :param unit: unit to connect
        """
        with unit.printer.connections.routed():
            unit._deferred_ace_connect(unit.printer.reactor.monotonic())

    #: A first-try connect to four empty slots: RFID on, then each slot's tag read.
    CONNECTED = [
        ("info", "ACE Ace_1: connected, mode=combined, port=/dev/ttyACM0, slots=4"),
        ("debug", "ACE Ace_1: RFID enabled"),
        ("debug", "ACE Ace_1: slot 0 get_filament_info -> {}"),
        ("debug", "ACE Ace_1: slot 1 get_filament_info -> {}"),
        ("debug", "ACE Ace_1: slot 2 get_filament_info -> {}"),
        ("debug", "ACE Ace_1: slot 3 get_filament_info -> {}")]
    #: Logged once the startup retries give up.
    WATCHING = ("info", "ACE Ace_1: will keep looking for it every 15s")
    #: The four retries of a connect that never succeeds, 2s to 8s apart.
    RETRIES = [
        ("info", "ACE Ace_1: serial port not available, retrying in 2s (attempt 1/5)"),
        ("info", "ACE Ace_1: serial port not available, retrying in 3s (attempt 2/5)"),
        ("info", "ACE Ace_1: serial port not available, retrying in 5s (attempt 3/5)"),
        ("info", "ACE Ace_1: serial port not available, retrying in 8s (attempt 4/5)")]

    @pytest.mark.parametrize("ok", [True, False], ids=["connected", "failed"])
    def test_the_first_attempt_clears_the_wait_either_way(self, ok):
        unit = ace_p1_unit("lane1")
        unit._first_connect_pending = True
        seen: List[bool] = []
        unit.printer.connections.on_connect = (
            lambda conn: seen.append(unit._first_connect_pending))
        if not ok:
            unit.printer.connections.connect_raises = RuntimeError("no unit reporting id 1")

        self._connect(unit)

        assert seen == ([True] if ok else [True, False, False, False, False])
        assert unit._first_connect_pending is False
        failed = self.RETRIES + [
            ("error", "ACE Ace_1: failed to connect at /dev/ttyACM0 after 5 attempts: "
                      "no unit reporting id 1"), self.WATCHING]
        assert unit.logger.messages == (self.CONNECTED if ok else failed)

    def test_success_first_try(self):
        unit = ace_p1_unit("lane0", LaneSpec("lane1", prep=True))
        old_link = unit._ace

        self._connect(unit)

        link = unit._ace
        assert link is not old_link
        assert link.connected is True
        assert link.lifecycle == ["connect"]
        assert link.commands == [("enable_rfid", {}), ("get_status", {}), ("get_status", {}),
                                 ("get_filament_info", {"index": 0}),
                                 ("get_filament_info", {"index": 1}),
                                 ("get_filament_info", {"index": 2}),
                                 ("get_filament_info", {"index": 3})]
        assert unit._cached_hw_status == {"status": "ready", "slots": [
            {"index": 0, "status": "empty"}, {"index": 1, "status": "ready"},
            {"index": 2, "status": "empty"}, {"index": 3, "status": "empty"}]}
        assert [slot["status"] for slot in unit._slot_inventory] == [
            "empty", "ready", "empty", "empty"]
        assert unit._prev_states_stale is True
        assert link.status_callback == unit._on_hw_status_callback
        assert link.reconnect_callback == unit._on_ace_reconnect
        assert unit.logger.messages == self.CONNECTED

    def test_all_attempts_fail_logs_error(self):
        unit = ace_p1_unit("lane1")
        unit.printer.connections.connect_raises = RuntimeError("no port")

        self._connect(unit)

        assert unit._ace is None
        assert [call[0] for call in unit.printer.reactor.pause.calls] == [
            (102.0,), (105.0,), (110.0,), (118.0,)]
        assert len(unit.printer.connections.created) == 5
        assert unit.logger.messages == self.RETRIES + [
            ("error", "ACE Ace_1: failed to connect at /dev/ttyACM0 after 5 attempts: no port"),
            self.WATCHING]
        assert [(e.callback, e.waketime) for e in unit.afc.reactor.pending] == [
            (unit._watch_for_ace, 133.0)]

    def test_success_fires_connected(self):
        unit = ace_p1_unit("lane1")

        self._connect(unit)

        assert ("afc_ace:connected", (unit, False)) in unit.printer.events

    def test_a_unit_switched_on_later_still_connects(self):
        unit = ace_p1_unit("lane1")
        unit.printer.connections.connect_raises = [RuntimeError("no port")] * 6 + [None]
        self._connect(unit)
        unit.logger.messages.clear()

        with unit.printer.connections.routed():
            unit.printer.reactor.advance(15.0)          # still off: look again later
            assert unit._ace is None
            assert unit.logger.messages == [
                ("debug", "ACE Ace_1: still not found: no port")]
            unit.logger.messages.clear()
            unit.printer.reactor.advance(15.0)          # switched on

        assert unit._ace is not None and unit._ace.connected is True
        assert unit._ace.reconnect_callback == unit._on_ace_reconnect
        assert unit.logger.messages[:2] == [
            ("info", "ACE Ace_1: found it, connecting late"), self.CONNECTED[0]]
        assert ("afc_ace:connected", (unit, False)) in unit.printer.events
        assert not any(e.callback == unit._watch_for_ace
                       for e in unit.afc.reactor.pending)

    def test_logs_connected_on_attempt_when_a_retry_succeeds(self):
        unit = ace_p1_unit("lane1")
        unit.printer.connections.connect_raises = [RuntimeError("no port"), None]

        self._connect(unit)

        assert unit.logger.messages == (
            [self.RETRIES[0], ("info", "ACE Ace_1: connected on attempt 2")]
            + self.CONNECTED)


class TestAfcACEWatchForAce:
    def test_returns_immediately_when_already_connected(self):
        unit = ace_p1_unit("lane0")
        unit._ace.connected = True
        old_link = unit._ace

        unit._watch_for_ace(unit.printer.reactor.monotonic())

        assert unit._ace is old_link
        assert unit.afc.reactor.pending == []
        assert unit.logger.messages == []

    def test_still_not_found_reschedules_the_watch(self):
        unit = ace_p1_unit("lane0")
        unit._ace = None
        unit.printer.connections.connect_raises = RuntimeError("no port")

        with unit.printer.connections.routed():
            unit._watch_for_ace(unit.printer.reactor.monotonic())

        assert unit._ace is None
        assert unit.logger.messages == [("debug", "ACE Ace_1: still not found: no port")]
        assert [(e.callback, e.waketime) for e in unit.afc.reactor.pending] == [
            (unit._watch_for_ace, 115.0)]

    def test_found_it_connects_late(self):
        unit = ace_p1_unit("lane0")
        unit._ace = None

        with unit.printer.connections.routed():
            unit._watch_for_ace(unit.printer.reactor.monotonic())

        assert unit._ace is not None and unit._ace.connected is True
        assert unit.logger.messages == (
            [("info", "ACE Ace_1: found it, connecting late")]
            + TestAfcACEDeferredAceConnect.CONNECTED)
        assert unit.printer.events == [("afc_ace:connected", (unit, False))]


class TestAfcACEFinishConnect:
    #: _finish_connect's log sequence for an unprepped lane1 with no failures.
    CONNECTED = TestAfcACEDeferredAceConnect.CONNECTED

    def test_no_connection_returns_before_logging_or_firing_connected_event(self):
        unit = ace_p1_unit("lane1")
        unit._ace = None

        unit._finish_connect()

        assert unit.logger.messages == []
        assert unit.printer.events == []

    def test_enable_rfid_failure_is_logged_and_connect_continues(self):
        unit = ace_p1_unit("lane1")
        unit._ace.enable_rfid = Recorder(raises=RuntimeError("rfid boom"))

        unit._finish_connect()

        assert unit.logger.messages == [
            self.CONNECTED[0],
            ("warning", "ACE Ace_1: enable_rfid failed (non-fatal): rfid boom"),
        ] + self.CONNECTED[2:]
        assert unit.printer.events == [("afc_ace:connected", (unit, False))]

    def test_non_dict_get_status_result_is_ignored(self):
        unit = ace_p1_unit("lane1")
        unit._ace.set_reply("get_status", ace_status("ready"), "oops")

        unit._finish_connect()

        assert unit._cached_hw_status == {}
        assert unit.logger.messages == self.CONNECTED

    def test_get_status_exception_is_logged_as_debug(self):
        unit = ace_p1_unit("lane1")
        unit._ace.set_reply("get_status", ace_status("ready"), RuntimeError("status boom"))

        unit._finish_connect()

        assert unit._cached_hw_status == {}
        assert unit.logger.messages == [
            self.CONNECTED[0], self.CONNECTED[1],
            ("debug", "ACE initial get_status failed: status boom"),
        ] + self.CONNECTED[2:]

    def test_extra_slot_entries_beyond_capacity_are_skipped(self):
        unit = ace_p1_unit("lane1")
        hw = ace_status("ready", "ready", "ready", "ready", "ready")
        unit._ace.set_reply("get_status", ace_status("ready"), hw)

        unit._finish_connect()

        assert [slot.get("status") for slot in unit._slot_inventory] == ["ready"] * 4
        assert unit._cached_hw_status == hw
        assert unit.logger.messages == self.CONNECTED

    def test_non_dict_slot_entry_is_skipped_without_aborting_the_rest(self):
        unit = ace_p1_unit("lane1")
        hw = {"status": "ready", "slots": [
            {"index": 0, "status": "ready"}, "garbage",
            {"index": 2, "status": "ready"}, {"index": 3, "status": "ready"}]}
        unit._ace.set_reply("get_status", ace_status("ready"), hw)

        unit._finish_connect()

        assert [slot.get("status") for slot in unit._slot_inventory] == [
            "ready", None, "ready", "ready"]
        assert unit.logger.messages == self.CONNECTED


class TestAfcACEOnAceReconnect:
    def test_logs_and_clears_when_assist_active(self):
        unit = ace_p1_unit("lane1", feed_assist_active=[1, 2])

        unit._on_ace_reconnect()

        assert unit._feed_assist_active == set()
        assert unit._prev_states_stale is True
        assert [entry.callback for entry in unit.afc.reactor.pending][0] == (
            unit._resync_assist_after_reconnect)
        assert len(unit.afc.reactor.pending) == 2
        assert unit.logger.messages == [
            ("info", "ACE reconnected, re-establishing feed assist for the active lane")]

    def test_no_log_when_no_assist(self):
        unit = ace_p1_unit("lane1")

        unit._on_ace_reconnect()

        assert unit._feed_assist_active == set()
        assert unit._prev_states_stale is True
        assert [entry.callback for entry in unit.afc.reactor.pending][0] == (
            unit._resync_assist_after_reconnect)
        assert unit.logger.messages == []

    def test_tells_listeners_after_the_reset(self):
        unit = ace_p1_unit("lane1")

        unit._on_ace_reconnect()
        unit.afc.reactor.pending[1].callback(unit.afc.reactor.monotonic())

        assert ("afc_ace:connected", (unit, True)) in unit.printer.events


class TestAfcACEResyncAssistAfterReconnect:
    def test_operation_active_only_reapplies_feed_check(self):
        # An ACE 2 unit, whose feed-check push is observable on its link.
        unit = make_ace2_unit(lanes=[LaneSpec("lane0", prep=True, tool_loaded=True)],
                              operation_active=True, feed_assist_active=[0])
        unit.lanes["lane0"].extruder_obj.lane_loaded = "lane0"

        unit._resync_assist_after_reconnect(unit.printer.reactor.monotonic())

        assert unit._ace.commands == [("set_feed_check",
                                       {"check_length": 200, "error_length": 185})]
        assert unit._feed_assist_active == {0}
        assert unit.afc.reactor.pending == []
        assert unit.logger.messages == [
            ("info", "ACE2 Ace2_1: feed check set check_length=200 error_length=185")]

    def test_clears_all_slots_then_reconciles(self):
        # An ACE 2 unit, so the feed-check push that ends the resync is visible.
        unit = make_ace2_unit(lanes=[LaneSpec("lane0", prep=True, tool_loaded=True)],
                              feed_assist_active=[0, 1])
        unit.lanes["lane0"].extruder_obj.lane_loaded = "lane0"

        unit._resync_assist_after_reconnect(unit.printer.reactor.monotonic())

        assert unit._ace.commands == [("stop_feed_assist", {"index": 0}),
                                      ("stop_feed_assist", {"index": 1}),
                                      ("stop_feed_assist", {"index": 2}),
                                      ("stop_feed_assist", {"index": 3}),
                                      ("set_feed_check",
                                       {"check_length": 200, "error_length": 185})]
        assert unit._feed_assist_active == set()
        # The watchdog then queued the reconcile for the active lane.
        assert len(unit.afc.reactor.pending) == 1
        assert unit.logger.messages == [
            ("info", "ACE assist watchdog: enabling feed assist for lane0 (slot 0)"),
            ("info", "ACE2 Ace2_1: feed check set check_length=200 error_length=185")]

    def test_a_failed_stop_does_not_end_the_sweep(self):
        unit = ace_p1_unit("lane0", feed_assist_active=[2])
        unit._ace.set_reply("stop_feed_assist", RuntimeError("serial down"), {})

        unit._resync_assist_after_reconnect(unit.printer.reactor.monotonic())

        assert unit._ace.commands == [("stop_feed_assist", {"index": 0}),
                                      ("stop_feed_assist", {"index": 1}),
                                      ("stop_feed_assist", {"index": 2}),
                                      ("stop_feed_assist", {"index": 3})]
        assert unit._feed_assist_active == set()
        assert unit.logger.messages == []

    def test_no_link_skips_the_sweep(self):
        unit = ace_p1_unit("lane0", connection=None, feed_assist_active=[1])

        unit._resync_assist_after_reconnect(unit.printer.reactor.monotonic())

        assert unit._feed_assist_active == {1}
        assert unit.logger.messages == []

    def test_a_down_link_skips_the_sweep(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, tool_loaded=True),
                           feed_assist_active=[0, 1])
        unit.lanes["lane0"].extruder_obj.lane_loaded = "lane0"
        unit._ace.connected = False

        unit._resync_assist_after_reconnect(unit.printer.reactor.monotonic())

        # A sweep would have cleared the set even with every stop refused.
        assert unit._ace.commands == []
        assert unit._feed_assist_active == {0, 1}
        # The watchdog still queued the reconcile for the active lane.
        assert len(unit.afc.reactor.pending) == 1
        assert unit.logger.messages == [
            ("info", "ACE assist watchdog: enabling feed assist for lane0 (slot 0)")]


class TestAfcACEOnHwStatusCallback:
    V1_BUSY = {"status": "busy", "action": "preload",
               "slots": [{"index": 0, "status": "preload"},
                         {"index": 1, "status": "ready"}]}
    V1_IDLE = {"status": "ready", "slots": [{"index": 0, "status": "ready"}]}
    ACE2_BUSY = {"status": "busy",
                 "slots": [{"index": 0, "slot_status": "feeding", "status": "ready"},
                           {"index": 1, "slot_status": "ready", "status": "ready"}]}

    @staticmethod
    def _unit(**kwargs: Any) -> afcACE:
        """
        A unit with stuck detection on and its latch set, so a run of
        _check_stuck (not printing) visibly clears it.

        :param kwargs: make_ace_unit keywords
        :return afcACE: the unit with lane0 prepped in slot 0
        """
        unit = ace_p1_unit(LaneSpec("lane0", prep=True),
                           values={"stuck_spool_detection": True}, **kwargs)
        unit._stuck_tripped = True
        return unit

    def test_callback_logs_transition_and_tracks_action(self):
        unit = self._unit()

        unit._on_hw_status_callback({"result": self.V1_BUSY})
        assert unit._current_action == "preload(slot 0)"
        unit._on_hw_status_callback({"result": self.V1_IDLE})

        assert unit._current_action == ""
        assert unit.logger.messages == [("debug", "ACE Ace_1: idle -> preload(slot 0)"),
                                        ("debug", "ACE Ace_1: preload(slot 0) -> idle")]

    def test_callback_no_duplicate_log_when_action_unchanged(self):
        unit = self._unit()

        unit._on_hw_status_callback({"result": self.ACE2_BUSY})
        unit._on_hw_status_callback({"result": self.ACE2_BUSY})

        assert unit._current_action == "feeding(slot 0)"
        assert unit.logger.messages == [("debug", "ACE Ace_1: idle -> feeding(slot 0)")]

    def test_callback_tracks_action_even_during_operation(self):
        unit = self._unit(operation_active=True)

        unit._on_hw_status_callback({"result": self.ACE2_BUSY})

        assert unit._current_action == "feeding(slot 0)"
        assert unit._cached_hw_status == self.ACE2_BUSY
        # Slot sync and the stuck check were skipped.
        assert unit._slot_inventory[0] == {}
        assert unit._stuck_tripped is True
        assert unit.logger.messages == [("debug", "ACE Ace_1: idle -> feeding(slot 0)")]

    def test_callback_temp_reply_does_not_touch_action(self):
        unit = self._unit()
        unit._current_action = "feeding(slot 0)"

        unit._on_hw_status_callback({"result": {"ptc1_temp": 55.0}})

        assert unit._current_action == "feeding(slot 0)"
        assert unit._cached_temp_info == {"ptc1_temp": 55.0}
        assert unit.logger.messages == []

    def test_a_preload_start_is_only_logged(self):
        unit = self._unit()
        status = {"status": "busy", "slots": [{"index": 2, "status": "preloading"}]}

        unit._on_hw_status_callback({"result": status})

        assert unit.printer.events == []
        assert unit.logger.messages == [("debug", "ACE Ace_1: idle -> preloading(slot 2)")]

    def test_status_reply_updates_status_cache_and_syncs(self):
        unit = self._unit()
        unit.printer.reactor.now = 123.0
        status = {"status": "ready", "slots": [{"status": "ready"}]}

        unit._on_hw_status_callback({"result": status})

        assert unit._cached_hw_status == status
        assert unit._cached_temp_info == {}
        assert unit._hw_status_time == 123.0
        # _sync_slot_states copied the slot status and _check_stuck cleared the latch.
        assert unit._slot_inventory[0] == {"status": "ready"}
        assert unit._prev_slot_states == {"lane0": True}
        assert unit._stuck_tripped is False
        assert unit.logger.messages == []

    def test_status_reply_runs_the_assist_watchdog(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, tool_loaded=True))
        unit.lanes["lane0"].extruder_obj.lane_loaded = "lane0"

        unit._on_hw_status_callback({"result": {"status": "ready",
                                                "slots": [{"status": "ready"}]}})

        assert len(unit.afc.reactor.pending) == 1
        assert unit.logger.messages == [
            ("info", "ACE assist watchdog: enabling feed assist for lane0 (slot 0)")]

    def test_temp_reply_updates_temp_cache_only(self):
        unit = self._unit()
        prev_status = {"status": "ready", "slots": []}
        unit._cached_hw_status = prev_status
        temp = {"box1_temp": 0.0, "ptc1_temp": 55.0, "env_temp": 27.0,
                "env_humidity": 30.0}

        unit._on_hw_status_callback({"result": temp})

        assert unit._cached_temp_info == temp
        assert unit._cached_hw_status is prev_status
        assert unit._slot_inventory[0] == {}
        assert unit._stuck_tripped is True
        assert unit.logger.messages == []

    def test_temp_reply_detected_by_box_or_env_only(self):
        unit = self._unit()

        unit._on_hw_status_callback({"result": {"box1_temp": 24.0}})
        assert unit._cached_temp_info == {"box1_temp": 24.0}
        unit._on_hw_status_callback({"result": {"env_temp": 21.0}})

        assert unit._cached_temp_info == {"env_temp": 21.0}
        assert unit._cached_hw_status == {}
        assert unit._slot_inventory[0] == {}
        assert unit.logger.messages == []

    def test_a_sensor_state_reply_goes_to_its_own_cache(self):
        unit = self._unit()
        sensors = {"sensor_bitmask": 8, "slot_sensors": []}

        unit._on_hw_status_callback({"result": sensors})

        assert unit._cached_sensor_state == sensors
        assert unit._cached_hw_status == {}
        assert unit._stuck_tripped is True
        assert unit.logger.messages == []

    def test_status_reply_with_operation_active_caches_but_skips_sync(self):
        unit = self._unit(operation_active=True)
        status = {"status": "busy", "slots": [{"status": "ready"}]}

        unit._on_hw_status_callback({"result": status})

        assert unit._cached_hw_status == status
        assert unit._slot_inventory[0] == {}
        assert unit._prev_slot_states == {}
        assert unit._stuck_tripped is True
        assert unit.logger.messages == []

    def test_non_dict_response_ignored(self):
        unit = self._unit()

        unit._on_hw_status_callback("not a dict")
        unit._on_hw_status_callback({"result": "not a dict"})

        assert unit._cached_hw_status == {}
        assert unit._cached_temp_info == {}
        assert unit._hw_status_time is None
        assert unit._stuck_tripped is True
        assert unit.logger.messages == []

    def test_bare_status_without_result_wrapper(self):
        unit = self._unit()
        status = {"slots": [{"status": "empty"}], "status": "ready"}

        unit._on_hw_status_callback(status)

        assert unit._cached_hw_status == status
        assert unit._slot_inventory[0] == {"status": "empty"}
        assert unit._stuck_tripped is False
        assert unit.logger.messages == []

    def test_v1_async_ack_reply_does_not_wipe_cached_status(self):
        """An ack to a fire-and-forget async command (stop_feed_filament,
        stop_feed_assist, ...) has no 'slots' key at all on V1. It must be
        ignored rather than overwriting the real cached status/age/action."""
        unit = self._unit()
        prev_status = {"status": "ready", "slots": [{"index": 0, "status": "ready"}]}
        unit._cached_hw_status = prev_status
        unit._hw_status_time = 100.0
        unit._current_action = "feeding(slot 0)"
        unit.printer.reactor.now = 200.0

        unit._on_hw_status_callback({"result": {}})

        assert unit._cached_hw_status is prev_status
        assert unit._hw_status_time == 100.0
        assert unit._current_action == "feeding(slot 0)"
        assert unit._slot_inventory[0] == {}
        assert unit._stuck_tripped is True
        assert unit.logger.messages == []

    def test_ace2_async_ack_reply_does_not_wipe_cached_status(self):
        """ACE2 acks the same async commands with {'raw_fields': {...}},
        also lacking 'slots'; must be ignored for the same reason."""
        unit = self._unit()
        prev_status = {"status": "ready", "slots": [{"index": 0, "status": "ready"}]}
        unit._cached_hw_status = prev_status
        unit._hw_status_time = 100.0

        unit._on_hw_status_callback({"result": {"raw_fields": {"opcode": 7}}})

        assert unit._cached_hw_status is prev_status
        assert unit._hw_status_time == 100.0
        assert unit._slot_inventory[0] == {}
        assert unit._stuck_tripped is True
        assert unit.logger.messages == []

    def test_non_list_slots_value_does_not_wipe_cached_status(self):
        """Covers the isinstance check itself, not just key absence: a
        'slots' key present but not a list must also be ignored."""
        unit = self._unit()
        prev_status = {"status": "ready", "slots": [{"index": 0, "status": "ready"}]}
        unit._cached_hw_status = prev_status

        unit._on_hw_status_callback({"result": {"slots": "not a list"}})

        assert unit._cached_hw_status is prev_status
        assert unit._stuck_tripped is True
        assert unit.logger.messages == []

    def test_a_clock_error_leaves_the_stamp_but_still_syncs(self):
        unit = self._unit()

        def broken_clock() -> float:
            """
            :return float: never returns
            """
            error_str = "clock"
            raise RuntimeError(error_str)
        unit.afc.reactor.monotonic = broken_clock
        status = {"status": "ready", "slots": [{"index": 0, "status": "empty"}]}

        unit._on_hw_status_callback({"result": status})

        assert unit._cached_hw_status == status
        assert unit._hw_status_time is None
        assert unit._slot_inventory[0] == {"status": "empty"}
        assert unit._stuck_tripped is False
        assert unit.logger.messages == []


class TestAfcACEDeriveAction:
    def test_derive_action_v1_busy_slot_tagged(self):
        status = {"status": "busy", "action": "preload",
                  "slots": [{"index": 0, "status": "preload"},
                            {"index": 1, "status": "ready"}]}

        assert afcACE._derive_action(status) == "preload(slot 0)"

    def test_derive_action_ace2_busy_slot_uses_slot_status(self):
        status = {"status": "busy",
                  "slots": [{"index": 0, "slot_status": "ready", "status": "ready"},
                            {"index": 1, "slot_status": "feeding", "status": "ready"}]}

        assert afcACE._derive_action(status) == "feeding(slot 1)"

    def test_derive_action_top_level_fallback_when_no_busy_slot(self):
        status = {"action": " drying ", "slots": ["garbage", {"index": 0, "status": "ready"}]}

        assert afcACE._derive_action(status) == "drying"

    def test_derive_action_idle_returns_empty(self):
        assert afcACE._derive_action({"status": "ready",
                                      "slots": [{"index": 0, "status": "ready"}]}) == ""
        assert afcACE._derive_action(
            {"status": "ready",
             "slots": [{"index": 0, "slot_status": "ready", "status": "ready"}]}) == ""

    def test_derive_action_no_slot_index_untagged(self):
        assert afcACE._derive_action({"slots": [{"slot_status": "rollback"}]}) == "rollback"

    def test_derive_action_non_dict_and_empty(self):
        assert afcACE._derive_action(None) == ""
        assert afcACE._derive_action("nope") == ""
        assert afcACE._derive_action({}) == ""
        assert afcACE._derive_action({"slots": None, "action": None}) == ""


class TestAfcACEGetStatus:
    @staticmethod
    def _ace_fields(status: dict) -> dict:
        """
        :param status: a get_status result
        :return dict: only the ace_* keys this unit adds
        """
        return {key: value for key, value in status.items() if key.startswith("ace_")}

    @staticmethod
    def _slots(*first: dict) -> List[dict]:
        """
        :param first: the leading slots' entries, the rest are empty
        :return list: four ace_slots entries
        """
        empty = {"status": "", "rfid": None, "sku": "", "material": "", "uid": "",
                 "color": [0, 0, 0], "buffer": ""}
        slots = [dict(empty, **entry) for entry in first]
        slots += [dict(empty) for _ in range(4 - len(slots))]
        return [dict(slot, index=index) for index, slot in enumerate(slots)]

    def test_get_status_adds_ace_state(self):
        hw = {"status": "busy", "temp": 28,
              "dryer_status": {"status": "stop", "target_temp": 45, "remain_time": 30,
                               "duration": 240}}
        unit = ace_p1_unit("lane0", "lane1", hw_status=hw, inventory={0: {
            "status": "ready", "rfid": 2, "sku": "HPL19-107", "material": "PLA",
            "uid": "BB2613B0102474", "color": [137, 168, 79]}})
        unit._current_action = "feeding(slot 0)"
        unit._cached_sensor_state = {"slot_sensors": [{"buf_back": True}]}

        status = unit.get_status()

        # The base unit status is kept: both lanes share one extruder and hub.
        assert {key: value for key, value in status.items()
                if not key.startswith("ace_")} == {
            "lanes": ["lane0", "lane1"], "extruders": ["extruder"], "hubs": ["Ace_1_hub"],
            "buffers": []}
        assert self._ace_fields(status) == {
            "ace_connected": True, "ace_status": "busy", "ace_action": "feeding(slot 0)",
            "ace_temp": 28, "ace_dryer": "stop", "ace_dryer_target": 45,
            "ace_dryer_remain": 30, "ace_dryer_duration": 240, "ace_dry_rotate": False,
            "ace_status_age": 0.0, "ace_status_stale": False,
            "ace_slots": self._slots({"status": "ready", "rfid": 2, "sku": "HPL19-107",
                                      "material": "PLA", "uid": "BB2613B0102474",
                                      "color": [137, 168, 79], "buffer": "advancing"})}

    def test_get_status_humidity_only_when_present(self):
        ace2 = ace_p1_unit(hw_status={"status": "ready", "temp": 26, "humidity": 31})
        v1 = ace_p1_unit(hw_status={"status": "ready", "temp": 26})

        assert ace2.get_status()["ace_humidity"] == 31
        assert "ace_humidity" not in v1.get_status()

    def test_get_status_disconnected_and_empty(self):
        unit = ace_p1_unit()
        unit._ace.connected = False

        status = unit.get_status()

        assert self._ace_fields(status) == {
            "ace_connected": False, "ace_status": "", "ace_action": "", "ace_temp": None,
            "ace_dryer": "", "ace_dryer_target": None, "ace_dryer_remain": None,
            "ace_dryer_duration": None, "ace_dry_rotate": False, "ace_status_age": None, "ace_status_stale": False,
            "ace_slots": self._slots()}

    def test_get_status_falls_back_to_temp_cache_for_ace2(self):
        unit = ace_p1_unit(hw_status={"status": "ready"},
                           temp_info={"env_temp": 24.5, "env_humidity": 38})

        status = unit.get_status()

        assert status["ace_temp"] == 24.5
        assert status["ace_humidity"] == 38

    def test_get_status_reports_stale_when_cache_ages(self):
        unit = ace_p1_unit(hw_status={"status": "ready"})
        unit._hw_status_time = 100.0
        assert unit.get_status()["ace_status_stale"] is False
        unit.printer.reactor.now = 106.0
        assert unit.get_status()["ace_status_stale"] is False

        unit.printer.reactor.now = 120.0
        status = unit.get_status()

        assert status["ace_status_stale"] is True
        assert status["ace_status_age"] == 20.0

    def test_get_status_an_old_cache_is_not_stale_while_disconnected(self):
        unit = ace_p1_unit(hw_status={"status": "ready"})
        unit._ace.connected = False
        unit._hw_status_time = 100.0
        unit.printer.reactor.now = 120.0

        status = unit.get_status()

        assert status["ace_connected"] is False
        assert status["ace_status_age"] == 20.0
        assert status["ace_status_stale"] is False

    def test_monotonic_exception_leaves_age_none(self):
        unit = ace_p1_unit(hw_status={"status": "ready"})
        unit._hw_status_time = 50.0
        unit.afc.reactor.monotonic = Recorder(raises=RuntimeError("clock"))

        status = unit.get_status()

        assert status["ace_status_age"] is None
        assert status["ace_status_stale"] is False


class TestAfcACEIsVirtualHub:
    def test_is_virtual_hub_all_branches(self):
        unit = ace_p1_unit("lane0")
        lane = unit.lanes["lane0"]

        assert unit._is_virtual_hub(lane) is True
        lane.hub_obj = None
        assert unit._is_virtual_hub(lane) is False
        # A hub object with no is_virtual_pin.
        lane.hub_obj = types.SimpleNamespace(switch_pin="virtual")
        assert unit._is_virtual_hub(lane) is False
        lane.hub_obj = unit.printer.add_hub("real_hub", switch_pin="PA1")
        assert unit._is_virtual_hub(lane) is False


class TestAfcACESetHubState:
    def test_virtual_hub_occupancy_derives_from_tool_loaded(self):
        unit = ace_p1_unit("lane0", LaneSpec("lane1", prep=True, tool_loaded=True))
        staged = unit.lanes["lane0"]
        staged._load_state = True
        loaded = unit.lanes["lane1"]
        loaded._load_state = False

        # The staged flag passed in is not the live signal.
        unit._set_hub_state(staged, True)
        unit._set_hub_state(loaded, False)

        assert staged._load_state is False
        assert loaded._load_state is True

    def test_real_hub_left_alone(self):
        unit = ace_p1_unit(LaneSpec("lane0", tool_loaded=True))
        lane = unit.lanes["lane0"]
        lane.hub_obj = unit.printer.add_hub("real_hub", switch_pin="PA1")
        lane._load_state = False

        unit._set_hub_state(lane, True)

        assert lane._load_state is False


class TestAfcACESyncSlotStates:
    @staticmethod
    def _unit(prev_ready: Optional[bool] = None, tool_loaded: bool = False,
              status: Optional[AFCLaneState] = None, stale: bool = False,
              current: Any = None, **kwargs: Any) -> Tuple[afcACE, Any, Recorder]:
        """
        A unit with lane0 in slot 0, its state as the last poll left it.

        :param prev_ready: last poll's ready for lane0 (None: never seen); also
            whether lane0 is prepped and staged
        :param tool_loaded: lane0 is loaded into the toolhead
        :param status: lane0's status, defaulted from its state when None
        :param stale: _prev_states_stale
        :param current: afc.current
        :param kwargs: make_ace_unit keywords
        :return tuple: the unit, lane0 and a Recorder on lane0.handle_load_runout
        """
        spec = LaneSpec("lane0", prep=bool(prev_ready), load=bool(prev_ready),
                        tool_loaded=tool_loaded, status=status)
        prev = None if prev_ready is None else {"lane0": prev_ready}
        unit = ace_p1_unit(spec, prev_slot_states=prev, current=current, **kwargs)
        unit._prev_states_stale = stale
        lane = unit.lanes["lane0"]
        lane.handle_load_runout = Recorder()
        return unit, lane, lane.handle_load_runout

    @staticmethod
    def _hw(slot_status: str, unit_status: str = "ready") -> dict:
        """
        :param slot_status: slot 0's status
        :param unit_status: the unit's status
        :return dict: a get_status result with one slot
        """
        return {"status": unit_status, "slots": [{"status": slot_status}]}

    def test_inventory_status_copied_per_slot(self):
        unit, lane, runout = self._unit(prev_ready=True)

        unit._sync_slot_states({"status": "ready", "slots": [
            {"status": "ready"}, {"status": "empty"}, "garbage"]})

        assert unit._slot_inventory[0] == {"status": "ready"}
        assert unit._slot_inventory[1] == {"status": "empty"}
        assert unit._slot_inventory[2] == {}
        assert runout.calls == []
        assert unit.logger.messages == []

    def test_lane_beyond_reported_slots_skipped(self):
        unit, lane, runout = self._unit(prev_ready=True)

        unit._sync_slot_states({"status": "ready", "slots": []})

        assert lane.prep_state is True
        assert unit._prev_slot_states == {"lane0": True}
        assert runout.calls == []
        assert unit.logger.messages == []

    def test_malformed_slot_entry_skipped(self):
        unit, lane, runout = self._unit(prev_ready=True)

        unit._sync_slot_states({"status": "ready", "slots": ["garbage"]})

        assert lane.prep_state is True
        assert unit._prev_slot_states == {"lane0": True}
        assert runout.calls == []
        assert unit.logger.messages == []

    def test_virtual_hub_refreshed_from_tool_loaded(self):
        unit, lane, runout = self._unit(prev_ready=True, tool_loaded=True, current="lane0")
        lane._load_state = False

        unit._sync_slot_states(self._hw("ready"))

        assert lane._load_state is True
        assert runout.calls == []
        assert unit.logger.messages == []

    def test_real_hub_load_state_untouched(self):
        unit, lane, runout = self._unit(prev_ready=True, tool_loaded=True, current="lane0")
        lane.hub_obj = unit.printer.add_hub("real_hub", switch_pin="PA1")
        lane._load_state = False

        unit._sync_slot_states(self._hw("ready"))

        assert lane._load_state is False
        assert unit.logger.messages == []

    def test_transient_status_leaves_everything_alone(self):
        unit, lane, runout = self._unit(prev_ready=True)

        for status in ("shifting", "feeding", "unwinding"):
            unit._sync_slot_states(self._hw(status))
            assert lane.prep_state is True
            assert lane.loaded_to_hub is True
            assert unit._prev_slot_states == {"lane0": True}
            assert unit._slot_inventory[0] == {"status": status}

        assert runout.calls == []
        assert unit.logger.messages == []

    def test_ready_to_empty_fires_runout_and_clears_staging(self):
        unit, lane, runout = self._unit(prev_ready=True)
        unit._hub_load_suppressed = {"lane0"}
        unit.printer.reactor.now = 150.0

        unit._sync_slot_states(self._hw("empty"))

        assert lane.prep_state is False
        assert lane.loaded_to_hub is False
        assert lane._load_state is False
        assert unit._hub_load_suppressed == set()
        assert runout.calls == [((150.0, False), {})]
        assert unit._prev_slot_states == {"lane0": False}
        assert unit.logger.messages == []

    def test_unit_busy_suppresses_removal(self):
        unit, lane, runout = self._unit(prev_ready=True)
        unit._hub_load_suppressed = {"lane0"}

        unit._sync_slot_states(self._hw("empty", unit_status="busy"))

        assert runout.calls == []
        assert lane.loaded_to_hub is True
        assert lane.prep_state is False
        assert unit._hub_load_suppressed == {"lane0"}
        assert unit._prev_slot_states == {"lane0": False}
        assert unit.logger.messages == []

    def test_stale_prev_states_resync_without_events(self):
        unit, lane, runout = self._unit(prev_ready=True, stale=True)

        unit._sync_slot_states(self._hw("empty"))

        assert runout.calls == []
        assert unit._prev_slot_states == {"lane0": False}
        assert unit._prev_states_stale is False
        assert lane.loaded_to_hub is True
        assert unit.logger.messages == []

    def test_empty_stays_empty_no_event(self):
        unit, lane, runout = self._unit(prev_ready=False)
        unit._hub_load_suppressed = {"lane0"}

        unit._sync_slot_states(self._hw("empty"))

        assert runout.calls == []
        assert unit._hub_load_suppressed == set()
        assert unit._prev_slot_states == {"lane0": False}
        assert unit.logger.messages == []

    def test_fresh_insert_v1_preloads_to_hub(self):
        unit, lane, runout = self._unit(prev_ready=False, status=AFCLaneState.LOADED)

        unit._sync_slot_states(self._hw("ready"))

        assert lane.prep_state is True
        assert lane.loaded_to_hub is True
        assert lane._load_state is False
        assert unit._prev_slot_states == {"lane0": True}
        assert runout.calls == []
        assert unit.logger.messages == []

    def test_fresh_insert_respects_load_to_hub_off(self):
        unit, lane, runout = self._unit(prev_ready=False, status=AFCLaneState.LOADED)
        lane.load_to_hub = False

        unit._sync_slot_states(self._hw("ready"))

        assert lane.prep_state is True
        assert lane.loaded_to_hub is False
        assert unit.logger.messages == []

    def test_fresh_insert_ace2_does_not_preload(self):
        unit, lane, runout = self._unit(prev_ready=False, status=AFCLaneState.LOADED)
        # The ACE 2 stages through prep_post_load instead of its own preload.
        unit._preloads_to_hub_on_insert = False

        unit._sync_slot_states(self._hw("ready"))

        assert lane.prep_state is True
        assert lane.loaded_to_hub is False
        assert unit.logger.messages == []

    def test_insert_seen_on_a_resync_does_not_preload(self):
        unit, lane, runout = self._unit(prev_ready=False, status=AFCLaneState.LOADED,
                                        stale=True)

        unit._sync_slot_states(self._hw("ready"))

        assert lane.prep_state is True
        assert lane.loaded_to_hub is False
        assert unit.logger.messages == []

    def test_tool_loaded_lane_never_preloaded(self):
        unit, lane, runout = self._unit(prev_ready=False, tool_loaded=True, current="lane0")
        lane.loaded_to_hub = False

        unit._sync_slot_states(self._hw("ready"))

        assert lane.loaded_to_hub is False
        assert lane.prep_state is True
        assert unit.logger.messages == []

    def test_ready_untooled_lane_fires_insert_path(self):
        unit, lane, runout = self._unit(prev_ready=True, status=AFCLaneState.NONE)
        unit.printer.reactor.now = 150.0

        unit._sync_slot_states(self._hw("ready"))

        assert runout.calls == [((150.0, True), {})]
        assert lane._load_suppressed is False
        assert unit.logger.messages == []

    def test_ready_untooled_suppressed_marks_lane(self):
        unit, lane, runout = self._unit(prev_ready=True, status=AFCLaneState.NONE)
        unit._hub_load_suppressed = {"lane0"}
        unit.printer.reactor.now = 150.0

        unit._sync_slot_states(self._hw("ready"))

        assert lane._load_suppressed is True
        assert runout.calls == [((150.0, True), {})]
        assert unit.logger.messages == []

    def test_prep_not_done_skips_insert_path(self):
        unit, lane, runout = self._unit(prev_ready=True, status=AFCLaneState.NONE)
        lane._afc_prep_done = False

        unit._sync_slot_states(self._hw("ready"))

        assert runout.calls == []
        assert unit.logger.messages == []

    def test_ready_tooled_current_lane_restores_full_state(self):
        unit, lane, runout = self._unit(prev_ready=True, tool_loaded=True,
                                        status=AFCLaneState.NONE, current="lane0")
        lane.loaded_to_hub = False
        lane.spool_id = 42
        lane.sync_to_extruder = Hook(lane.sync_to_extruder)
        lane.enable_buffer = Hook(lane.enable_buffer)

        unit._sync_slot_states(self._hw("ready"))

        assert lane.sync_to_extruder.calls == [((), {})]
        assert lane.enable_buffer.calls == [((), {})]
        assert lane.loaded_to_hub is True
        assert lane.status == AFCLaneState.TOOLED
        assert unit.afc.spool.set_active_spool.calls == [((42,), {})]
        # lane_tool_loaded set the tool-loaded LEDs.
        assert lane.current_led_state == "tool_loaded"
        assert lane.extruder_obj.set_status_led.calls == [(("0,0,1,0",), {})]
        assert unit._ace.commands == [("get_status", {}), ("start_feed_assist", {"index": 0})]
        assert unit._feed_assist_active == {0}
        assert runout.calls == []
        assert unit.logger.messages == [
            ("info", "ACE: lane0 restoring TOOLED state from saved vars")]

    def test_ready_tooled_idle_lane_restores_idle_state(self):
        unit, lane, runout = self._unit(prev_ready=True, tool_loaded=True,
                                        status=AFCLaneState.NONE, current="other_lane")
        lane.sync_to_extruder = Hook(lane.sync_to_extruder)
        lane.enable_buffer = Hook(lane.enable_buffer)

        unit._sync_slot_states(self._hw("ready"))

        assert lane.sync_to_extruder.calls == [((), {})]
        assert lane.enable_buffer.calls == [((), {})]
        assert lane.current_led_state == "tool_loaded_idle"
        assert unit.afc.spool.set_active_spool.calls == []
        assert lane.status == AFCLaneState.TOOLED
        # Not the active tool, so no assist.
        assert unit._ace.commands == []
        assert unit.logger.messages == [
            ("info", "ACE: lane0 restoring TOOLED state from saved vars")]

    def test_restore_does_not_refire_once_tooled(self):
        unit, lane, runout = self._unit(prev_ready=True, tool_loaded=True, current="lane0")
        lane.loaded_to_hub = False

        unit._sync_slot_states(self._hw("ready"))

        assert lane.loaded_to_hub is False
        assert lane.current_led_state == ""
        assert unit.afc.spool.set_active_spool.calls == []
        assert unit.logger.messages == []


class TestAfcACEDispatchLoadRunout:
    @staticmethod
    def _unit(raises: Optional[BaseException] = None) -> Tuple[afcACE, Any]:
        """
        :param raises: what lane0's handle_load_runout raises, if anything
        :return tuple: a printing unit and lane0, whose handler is a Recorder
        """
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, tool_loaded=True), printing=True)
        lane = unit.lanes["lane0"]
        lane.handle_load_runout = Recorder(raises=raises)
        return unit, lane

    @staticmethod
    def _error_call(unit: afcACE) -> Tuple[str, str, dict]:
        """
        The one logged call, its traceback checked and dropped.

        :param unit: the unit
        :return tuple: (level, message, other keywords)
        """
        assert len(unit.logger.calls) == 1
        level, message, kwargs = unit.logger.calls[0]
        traceback_text = kwargs.pop("traceback")
        assert traceback_text.startswith("Traceback (most recent call last):\n")
        return level, message, kwargs

    def test_insert_reaches_the_lane_handler(self):
        unit, lane = self._unit()

        unit._dispatch_load_runout(lane, 12.0, True)

        assert lane.handle_load_runout.calls == [((12.0, True), {})]
        assert unit.afc.error.AFC_error.calls == []
        assert unit.logger.messages == []

    def test_runout_reaches_the_lane_handler(self):
        unit, lane = self._unit()

        unit._dispatch_load_runout(lane, 12.0, False)

        assert lane.handle_load_runout.calls == [((12.0, False), {})]
        assert unit.afc.error.AFC_error.calls == []
        assert unit.logger.messages == []

    def test_handler_exception_is_logged_and_raised_as_an_afc_error(self):
        unit, lane = self._unit(raises=RuntimeError("Must home X axis first"))
        unit.afc.function.in_print_flag = True

        unit._dispatch_load_runout(lane, 12.0, False)

        assert self._error_call(unit) == (
            "error", "lane0: runout handling failed: Must home X axis first",
            {"stack_name": ""})
        assert unit.afc.error.AFC_error.calls == [(
            ("Lane lane0 runout handling failed: Must home X axis first\n"
             "Filament may still be loaded in the toolhead, check the toolhead "
             "before resuming.",), {"pause": True})]

    def test_insert_failure_is_reported_as_a_load_not_a_runout(self):
        unit, lane = self._unit(raises=RuntimeError("boom"))

        unit._dispatch_load_runout(lane, 12.0, True)

        assert self._error_call(unit) == (
            "error", "lane0: load handling failed: boom", {"stack_name": ""})
        assert unit.afc.error.AFC_error.calls == [(
            ("Lane lane0 load handling failed: boom\n"
             "Filament may still be loaded in the toolhead, check the toolhead "
             "before resuming.",), {"pause": False})]

    def test_handler_exception_does_not_escape_to_the_transport(self):
        unit, lane = self._unit(raises=RuntimeError("boom"))
        unit.afc.error.AFC_error = Recorder(raises=RuntimeError("no error obj"))

        unit._dispatch_load_runout(lane, 12.0, False)

        assert unit.logger.messages == [
            ("error", "lane0: runout handling failed: boom"),
            ("debug", "Unable to raise AFC error for the failed runout handler")]
        assert unit.afc.error.AFC_error.call_count == 1


class TestAfcACEGetBowdenLength:
    def test_adds_hub_bowden_length(self):
        unit = ace_p1_unit(LaneSpec("lane0", values={"dist_hub": 100}))

        # dist_hub 100 + the hub's afc_bowden_length 900.
        assert unit._get_bowden_length(unit.lanes["lane0"]) == 1000.0

    def test_without_hub_is_dist_hub_only(self):
        unit = ace_p1_unit(LaneSpec("lane0", values={"dist_hub": 120}))
        lane = unit.lanes["lane0"]
        lane.hub_obj = None

        assert unit._get_bowden_length(lane) == 120.0


class TestAfcACEGetUnloadLength:
    def test_unload_length_prefers_the_unload_bowden_length(self):
        unit = ace_p1_unit(LaneSpec("lane0", values={"dist_hub": 300}))
        lane = unit.lanes["lane0"]
        lane.hub_obj.afc_unload_bowden_length = 1000.0

        assert unit._get_unload_length(lane) == 1300.0

    def test_unload_length_falls_back_to_bowden_length(self):
        unit = ace_p1_unit(LaneSpec("lane0", values={"dist_hub": 300}))
        lane = unit.lanes["lane0"]
        # A hub with no afc_unload_bowden_length.
        lane.hub_obj = types.SimpleNamespace(afc_bowden_length=900.0)

        assert unit._get_unload_length(lane) == 1200.0

    def test_unload_length_without_hub(self):
        unit = ace_p1_unit(LaneSpec("lane0", values={"dist_hub": 300}))
        lane = unit.lanes["lane0"]
        lane.hub_obj = None

        assert unit._get_unload_length(lane) == 300.0


class TestAfcACEGetEjectLength:
    def test_eject_length_staged_at_hub(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, values={"dist_hub": 300}))

        # dist_hub 300 + the default eject_buffer 475.
        assert unit._get_eject_length(unit.lanes["lane0"]) == 775.0

    def test_eject_length_tool_loaded_uses_full_unload_path(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, tool_loaded=True,
                                    values={"dist_hub": 300}))
        lane = unit.lanes["lane0"]
        lane.hub_obj.afc_unload_bowden_length = 1000.0

        assert unit._get_eject_length(lane) == 1300.0


class TestAfcACEUseFeedAssist:
    def test_per_lane_disabled_returns_false(self):
        unit = ace_p1_unit("lane0", current=None)
        lane = unit.lanes["lane0"]
        lane.use_feed_assist = False

        assert unit._use_feed_assist(lane) is False

    def test_default_used_when_lane_unset_and_active(self):
        enabled = ace_p1_unit("lane0", current=None)
        disabled = ace_p1_unit("lane0", current=None, values={"use_feed_assist": False})

        assert enabled._use_feed_assist(enabled.lanes["lane0"]) is True
        assert disabled._use_feed_assist(disabled.lanes["lane0"]) is False

    def test_enabled_but_not_active_tool_returns_false(self):
        unit = ace_p1_unit("lane0", current="other")
        lane = unit.lanes["lane0"]
        lane.use_feed_assist = True

        assert unit._use_feed_assist(lane) is False

    def test_per_lane_enabled_overrides_a_disabled_default(self):
        unit = ace_p1_unit("lane0", current=None, values={"use_feed_assist": False})
        lane = unit.lanes["lane0"]
        lane.use_feed_assist = True

        assert unit._use_feed_assist(lane) is True


class TestAfcACELaneIsActiveTool:
    @staticmethod
    def _toolchanger_lane(on_shuttle: Any) -> Tuple[afcACE, Any]:
        """
        :param on_shuttle: the extruder's on_shuttle
        :return tuple: a unit whose current lane is another one, and lane0 on a
            toolchanger extruder
        """
        unit = ace_p1_unit("lane0", current="other")
        lane = unit.lanes["lane0"]
        lane.extruder_obj.tc_unit_name = "tc"
        lane.extruder_obj.on_shuttle = on_shuttle
        return unit, lane

    def test_toolchanger_on_shuttle_true(self):
        unit, lane = self._toolchanger_lane(lambda: True)

        assert unit._lane_is_active_tool(lane) is True

    def test_toolchanger_off_shuttle_false(self):
        unit, lane = self._toolchanger_lane(lambda: False)
        unit.afc.current = None

        assert unit._lane_is_active_tool(lane) is False

    def test_toolchanger_on_shuttle_not_callable_true(self):
        unit, lane = self._toolchanger_lane(None)

        assert unit._lane_is_active_tool(lane) is True

    def test_combined_matches_current(self):
        unit = ace_p1_unit("lane0", current="lane0")
        lane = unit.lanes["lane0"]

        assert unit._lane_is_active_tool(lane) is True
        # A lane with no extruder object follows afc.current too.
        lane.extruder_obj = None
        assert unit._lane_is_active_tool(lane) is True

    def test_combined_no_current_is_active(self):
        unit = ace_p1_unit("lane0", current=None)

        assert unit._lane_is_active_tool(unit.lanes["lane0"]) is True

    def test_combined_other_current_false(self):
        unit = ace_p1_unit("lane0", current="other")
        lane = unit.lanes["lane0"]

        assert unit._lane_is_active_tool(lane) is False
        lane.extruder_obj = None
        assert unit._lane_is_active_tool(lane) is False


class TestAfcACEBuildSlotMap:
    def test_build_slot_map_maps_index_to_zero_based_slot(self):
        unit = ace_p1_unit(LaneSpec("lane1", slot=0), LaneSpec("lane2", slot=2))

        assert unit._build_slot_map() == {"lane1": 0, "lane2": 2}

    def test_build_slot_map_rejects_duplicate_index(self):
        unit = ace_p1_unit("a", "b")
        unit.lanes["b"].index = 1

        with pytest.raises(configparser.Error) as raised:
            unit._build_slot_map()

        assert str(raised.value) == ("ACE Ace_1: lanes 'a' and 'b' both map to slot 0 "
                                     "(index 1), each lane needs a unique index.")

    def test_build_slot_map_rejects_out_of_range_index(self):
        unit = ace_p1_unit("a")
        unit.lanes["a"].index = 5

        with pytest.raises(configparser.Error) as too_high:
            unit._build_slot_map()
        unit.lanes["a"].index = 0
        with pytest.raises(configparser.Error) as too_low:
            unit._build_slot_map()

        assert str(too_high.value) == (
            "ACE Ace_1: lane 'a' has index 5, outside this unit's slots 1..4, fix the "
            "lane's index in the config.")
        assert str(too_low.value) == (
            "ACE Ace_1: lane 'a' has index 0, outside this unit's slots 1..4, fix the "
            "lane's index in the config.")


class TestAfcACEGetSlot:
    def test_get_slot_returns_mapped_slot(self):
        unit = ace_p1_unit(LaneSpec("lane1", slot=2))

        assert unit._get_slot("lane1") == 2
        assert unit.logger.messages == []

    def test_get_slot_unknown_lane_defaults_zero_and_warns(self):
        unit = ace_p1_unit(LaneSpec("lane1", slot=2))

        assert unit._get_slot("ghost") == 0
        assert unit._get_slot("ghost") == 0

        assert unit._slot_warned == {"ghost"}
        assert unit.logger.messages == [
            ("warning", "ACE Ace_1: lane 'ghost' is not in this unit's slot map ['lane1'], "
                        "defaulting to slot 0; check the unit/lane configuration")]


class TestAfcACEPrepLoad:
    def test_is_noop(self):
        unit = ace_p1_unit("lane0")

        assert unit.prep_load(unit.lanes["lane0"]) is None
        assert unit._ace.commands == []
        assert unit.printer.timeline == []
        assert unit.logger.messages == []


class TestAfcACECheckRunout:
    def test_check_runout_true_while_printing(self):
        unit = ace_p1_unit("lane0", printing=True)

        assert unit.check_runout(unit.lanes["lane0"]) is True

    def test_check_runout_false_when_idle(self):
        unit = ace_p1_unit("lane0", printing=False)

        assert unit.check_runout(unit.lanes["lane0"]) is False

    def test_check_runout_false_on_exception(self):
        unit = ace_p1_unit("lane0", printing=True)
        unit.afc.function.raise_on_is_printing = RuntimeError("boom")

        assert unit.check_runout(unit.lanes["lane0"]) is False


class TestAfcACEOnFilamentInsert:
    def test_restores_saved_spool_and_stages(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True))
        lane = unit.lanes["lane0"]
        lane.spool_id = 5
        lane.remember_spool = True
        # AFC's clear_values drops the lane's spool id.
        unit.afc.spool.clear_values = Hook(lambda cleared: setattr(cleared, "spool_id", None))
        wire = AceP1Wire(unit)

        unit.on_filament_insert(lane)

        assert unit.afc.spool.clear_values.calls == [((lane,), {})]
        assert unit.afc.spool.set_spoolID.calls == [((lane, 5), {})]
        # The restore branch fired, so the no-identity default-values branch must not.
        assert unit.afc.spool._set_values.calls == []
        assert unit._slot_inventory[0]["raw"] == {}
        assert wire.moves() == [("feed_filament", {"index": 0, "length": 60.0, "speed": 100.0})]
        assert lane.loaded_to_hub is True
        # Once here, once from prep_post_load's staging.
        assert unit.afc.save_vars.call_count == 2
        assert [event for event, _ in unit.printer.events] == [
            "afc_ace:stage_probe_begin", "afc_ace:stage_probe_end", "afc_ace:post_insert",
            "afc:lane_inserted"]
        assert unit.printer.events[2:] == [("afc_ace:post_insert", (lane,)),
                                           ("afc:lane_inserted", (lane,))]
        assert unit.logger.messages == [
            ("debug", "ACE Ace_1: slot 0 get_filament_info -> {}"),
            ("info", "ACE prep_post_load: lane0 staged at hub (dist_hub=60mm)")]

    def test_spool_not_restored_without_a_staged_id_or_remember_spool(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        lane.spool_id = 5
        lane.remember_spool = False
        unit.afc.spool.clear_values = Hook(lambda cleared: setattr(cleared, "spool_id", None))

        unit.on_filament_insert(lane)

        assert unit.afc.spool.set_spoolID.calls == []
        # No spool was restored, so the no-identity default-values branch must fire.
        assert unit.afc.spool._set_values.calls == [((lane,), {})]
        assert lane._afc_staged_spool_id is None
        assert unit.logger.messages == [("debug", "ACE Ace_1: slot 0 get_filament_info -> {}")]

    def test_a_staged_spool_is_restored_without_remember_spool(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        lane.spool_id = 5
        lane.remember_spool = False
        lane._afc_staged_spool_id = 5
        unit.afc.spool.clear_values = Hook(lambda cleared: setattr(cleared, "spool_id", None))

        unit.on_filament_insert(lane)

        assert unit.afc.spool.set_spoolID.calls == [((lane, 5), {})]
        # The restore branch fired, so the no-identity default-values branch must not.
        assert unit.afc.spool._set_values.calls == []
        assert lane._afc_staged_spool_id is None
        assert unit.logger.messages == [("debug", "ACE Ace_1: slot 0 get_filament_info -> {}")]

    def test_spool_not_restored_when_lane_had_no_spool_id(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        lane.spool_id = None
        lane.remember_spool = True
        lane._afc_staged_spool_id = 5

        unit.on_filament_insert(lane)

        assert unit.afc.spool.set_spoolID.calls == []
        # saved_spool_id was falsy, so the no-identity default-values branch must fire.
        assert unit.afc.spool._set_values.calls == [((lane,), {})]
        assert lane._afc_staged_spool_id is None
        assert unit.logger.messages == [("debug", "ACE Ace_1: slot 0 get_filament_info -> {}")]

    def test_spool_not_restored_when_lane_keeps_its_spool_id(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        lane.spool_id = 5
        lane.remember_spool = True
        lane._afc_staged_spool_id = 5

        unit.on_filament_insert(lane)

        # The default clear_values leaves spool_id set.
        assert unit.afc.spool.clear_values.calls == [((lane,), {})]
        assert unit.afc.spool.set_spoolID.calls == []
        # lane.spool_id never went falsy, so the no-identity default-values branch must fire.
        assert unit.afc.spool._set_values.calls == [((lane,), {})]
        assert lane.spool_id == 5
        assert unit.logger.messages == [("debug", "ACE Ace_1: slot 0 get_filament_info -> {}")]

    def test_prep_post_load_error_logged(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True))
        unit._uses_firmware_rfid = False
        unit._ace = AceP1BrokenLink("stage boom")
        lane = unit.lanes["lane0"]

        unit.on_filament_insert(lane)

        assert unit._slot_inventory[0] == {}
        assert unit.printer.events == [("afc_ace:post_insert", (lane,)),
                                       ("afc:lane_inserted", (lane,))]
        assert unit.logger.messages == [
            ("error", "ACE on_filament_insert: prep_post_load error for lane0: stage boom")]

    def test_post_insert_listener_error_logged(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        unit.printer.register_event_handler(
            "afc_ace:post_insert", Recorder(raises=RuntimeError("reader down")))

        unit.on_filament_insert(lane)

        assert unit.printer.events == [("afc_ace:post_insert", (lane,)),
                                       ("afc:lane_inserted", (lane,))]
        assert unit.logger.messages == [
            ("debug", "ACE Ace_1: slot 0 get_filament_info -> {}"),
            ("error", "ACE on_filament_insert: post_insert event error for lane0: reader down")]

    def test_slot_beyond_unit_capacity_is_a_no_op(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True))
        lane = unit.lanes["lane0"]
        # Force an out-of-range slot (a correctly configured unit never does this).
        unit._slot_map["lane0"] = 99

        unit.on_filament_insert(lane)

        assert unit.afc.spool.clear_values.calls == []
        assert unit.printer.events == []
        assert unit.logger.messages == []

    def test_applies_filament_defaults_when_rfid_hook_is_available(self, monkeypatch):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        lane.spool_id = None
        lane.remember_spool = False
        apply_defaults = Recorder()
        monkeypatch.setattr(afc_ace_module, "apply_filament_defaults", apply_defaults)

        unit.on_filament_insert(lane)

        assert apply_defaults.calls == [((lane, unit._slot_inventory[0]), {
            "color_converter": None,
            "afc_defaults": {"default_material_type": None, "default_color": None}})]
        assert unit.logger.messages == [("debug", "ACE Ace_1: slot 0 get_filament_info -> {}")]


class TestAfcACEOnFilamentRemove:
    def test_clears_inventory_and_hub_state(self):
        unit = ace_p1_unit(LaneSpec("lane0", slot=1, prep=True, load=True),
                           inventory={1: {"material": "PLA", "color": [1, 2, 3], "uid": "U1"}})
        lane = unit.lanes["lane0"]
        lane._load_state = True
        unit._hub_load_suppressed = {"lane0"}

        unit.on_filament_remove(lane)

        assert unit._slot_inventory[1] == {"material": "", "color": [0, 0, 0]}
        assert lane.loaded_to_hub is False
        assert lane._load_state is False
        assert unit._hub_load_suppressed == set()
        assert unit.logger.messages == []

    def test_slot_beyond_unit_capacity_skips_inventory_clear(self):
        unit = ace_p1_unit(LaneSpec("lane0", slot=1, prep=True, load=True),
                           inventory={1: {"material": "PLA"}})
        lane = unit.lanes["lane0"]
        # Force an out-of-range slot (a correctly configured unit never does this).
        unit._slot_map["lane0"] = 99

        unit.on_filament_remove(lane)

        assert unit._slot_inventory[1] == {"material": "PLA"}
        assert lane.loaded_to_hub is False
        assert unit.logger.messages == []


class TestAfcACECalibrateTd1:
    def test_wraps_inner_and_clears_flag(self):
        unit = ace_p1_unit(LaneSpec("lane0", values={"td1_device_id": "td1"}))
        seen: List[bool] = []
        unit.afc.function.check_for_td1_id = Hook(
            lambda device: (seen.append(unit._operation_active), (False, "bad td1"))[1])

        result = unit.calibrate_td1(unit.lanes["lane0"], 50.0, 1.0)

        assert result == (False, "bad td1", 0)
        assert seen == [True]
        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.logger.messages == []


class TestAfcACEPrepCaptureTd1:
    @staticmethod
    def _lane(when_loaded: bool, staged: bool) -> Tuple[afcACE, Any]:
        """
        :param when_loaded: the lane's capture_td1_when_loaded
        :param staged: the lane is staged at the hub
        :return tuple: the unit and lane0
        """
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=staged))
        lane = unit.lanes["lane0"]
        lane.td1_when_loaded = when_loaded
        return unit, lane

    def test_returns_handled_when_when_loaded_and_staged(self):
        unit, lane = self._lane(True, True)

        assert unit.prep_capture_td1(lane) == (True, "TD-1 capture handled by prep_post_load")

    def test_returns_none_when_not_staged(self):
        unit, lane = self._lane(True, False)

        assert unit.prep_capture_td1(lane) is None

    def test_returns_none_when_not_when_loaded(self):
        unit, lane = self._lane(False, True)

        assert unit.prep_capture_td1(lane) is None


class TestAfcACECaptureTd1Data:
    @staticmethod
    def _unit(**values: Any) -> Tuple[afcACE, Any]:
        """
        :param values: lane0's [AFC_lane] options
        :return tuple: the unit and lane0
        """
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, values=values))
        return unit, unit.lanes["lane0"]

    def test_not_connected(self):
        unit, lane = self._unit(td1_device_id="td1", td1_bowden_length=300)
        unit._ace.connected = False

        assert unit.capture_td1_data(lane) == (False, "ACE not connected")
        assert unit.afc.function.check_for_td1_id.calls == []
        assert unit.logger.messages == []

    def test_missing_device_id(self):
        unit, lane = self._unit()
        lane.td1_device_id = None

        assert unit.capture_td1_data(lane) == (False, "td1_device_id not set for lane")
        assert unit.logger.messages == []

    def test_missing_bowden_length(self):
        unit, lane = self._unit(td1_device_id="td1")
        lane.td1_bowden_length = None

        assert unit.capture_td1_data(lane) == (
            False, "td1_bowden_length not set, run TD-1 calibration first")
        assert unit.logger.messages == []

    def test_invalid_td1_id_surfaced(self):
        unit, lane = self._unit(td1_device_id="td1", td1_bowden_length=300)
        unit.afc.function.check_for_td1_id = Recorder(result=(False, "bad td1"))

        assert unit.capture_td1_data(lane) == (False, "bad td1")
        assert unit.afc.function.check_for_td1_id.calls == [(("td1",), {})]
        assert unit._ace.commands == []
        assert unit._operation_active is False
        assert unit.logger.messages == []

    def test_delegates_to_inner_with_operation_flag_set(self):
        unit, lane = self._unit(td1_device_id="td1", td1_bowden_length=300)
        seen: List[bool] = []
        unit._capture_td1_data_inner = Hook(
            lambda cur_lane, slot: (seen.append(unit._operation_active),
                                    (True, "captured"))[1])

        result = unit.capture_td1_data(lane)

        assert result == (True, "captured")
        assert unit._capture_td1_data_inner.calls == [((lane, 0), {})]
        assert seen == [True]
        assert unit._operation_active is False
        assert unit.logger.messages == []


class TestAfcACECaptureTd1DataInner:
    def test_no_connection_returns_before_any_feed(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True,
                                    values={"td1_bowden_length": 300}))
        lane = unit.lanes["lane0"]
        unit._ace = None

        ok, msg = unit._capture_td1_data_inner(lane, 0)

        assert (ok, msg) == (False, "ACE not connected")
        assert unit.logger.messages == []

    def test_missing_bowden_length_returns_before_any_feed(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        lane.td1_bowden_length = None
        wire = AceP1Wire(unit)

        ok, msg = unit._capture_td1_data_inner(lane, 0)

        assert (ok, msg) == (False, "td1_bowden_length not set for lane0")
        assert wire.moves() == []
        assert unit.logger.messages == []

    def test_captures_when_get_td1_succeeds(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=True,
                                            td1_bowden_length=300)
        wire = AceP1Wire(unit)

        ok, msg = unit._capture_td1_data_inner(lane, 0)

        assert (ok, msg) == (True, "TD-1 data captured for lane0")
        assert fetch.call_count == 1
        assert lane.td1_data == {"scan_time": "2099-01-01T00:00:00Z", "td": 1.5,
                                 "color": "#FF0000"}
        assert wire.moves() == [
            ("feed_filament", {"index": 0, "length": 300.0, "speed": 100.0}),
            ("unwind_filament", {"index": 0, "length": 300.0, "speed": 100.0,
                                 "mode": "normal"})]
        assert unit.logger.messages == [
            ("info", "ACE TD-1 capture: feeding 300.0mm to TD-1 for lane0"),
            ("debug", "ACE: waiting for ready (status=busy, 0.0s/30s)"),
            ("debug", ACE_P1_TD1_DATA_LINE),
            ("info", "lane0 TD-1 data captured"),
            ("info", "ACE TD-1 capture: retracting 300.0mm for lane0")]

    def test_feeds_to_hub_first_when_not_already_staged(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=False,
                                            td1_bowden_length=300)
        wire = AceP1Wire(unit)

        ok, msg = unit._capture_td1_data_inner(lane, 0)

        assert (ok, msg) == (True, "TD-1 data captured for lane0")
        assert wire.moves() == [
            ("feed_filament", {"index": 0, "length": 60.0, "speed": 100.0}),
            ("feed_filament", {"index": 0, "length": 300.0, "speed": 100.0}),
            ("unwind_filament", {"index": 0, "length": 360.0, "speed": 100.0,
                                 "mode": "normal"})]
        assert unit.logger.messages == [
            ("info", "ACE TD-1 capture: feeding 60mm to hub for lane0"),
            ("debug", "ACE: waiting for ready (status=busy, 0.0s/30s)"),
            ("info", "ACE TD-1 capture: feeding 300.0mm to TD-1 for lane0"),
            ("debug", "ACE: waiting for ready (status=busy, 0.0s/30s)"),
            ("debug", ACE_P1_TD1_DATA_LINE),
            ("info", "lane0 TD-1 data captured"),
            ("info", "ACE TD-1 capture: retracting 360.0mm for lane0")]

    def test_retries_get_td1_data_once_then_succeeds(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=True,
                                            td1_bowden_length=300)
        AceP1Wire(unit)
        attempts: List[int] = []
        real_apply = unit._apply_td1_data

        def flaky_apply(cur_lane, compare_time, td1_data, ignore_time=False):
            """First call reports no detection yet; later calls behave normally."""
            attempts.append(1)
            if len(attempts) == 1:
                return False
            return real_apply(cur_lane, compare_time, td1_data, ignore_time)
        unit._apply_td1_data = flaky_apply

        ok, msg = unit._capture_td1_data_inner(lane, 0)

        assert (ok, msg) == (True, "TD-1 data captured for lane0")
        assert fetch.call_count == 2
        assert len(attempts) == 2

    def test_retract_failure_is_logged_and_still_reports_capture_result(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=True,
                                            td1_bowden_length=300)
        AceP1Wire(unit)
        unit._ace.unwind_filament = Recorder(raises=RuntimeError("unwind boom"))

        ok, msg = unit._capture_td1_data_inner(lane, 0)

        assert (ok, msg) == (True, "TD-1 data captured for lane0")
        assert unit.logger.messages == [
            ("info", "ACE TD-1 capture: feeding 300.0mm to TD-1 for lane0"),
            ("debug", "ACE: waiting for ready (status=busy, 0.0s/30s)"),
            ("debug", ACE_P1_TD1_DATA_LINE),
            ("info", "lane0 TD-1 data captured"),
            ("info", "ACE TD-1 capture: retracting 300.0mm for lane0"),
            ("error", "ACE TD-1 capture retract failed: unwind boom")]

    def test_unsuccessful_capture_reports_unload_completed(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=True,
                                            td1_bowden_length=300)
        AceP1Wire(unit)
        unit._apply_td1_data = Recorder(result=False)

        ok, msg = unit._capture_td1_data_inner(lane, 0)

        assert (ok, msg) == (False, "TD-1 data not captured (unload completed)")
        assert unit.logger.messages == [
            ("info", "ACE TD-1 capture: feeding 300.0mm to TD-1 for lane0"),
            ("debug", "ACE: waiting for ready (status=busy, 0.0s/30s)"),
            ("info", "ACE TD-1 capture: retracting 300.0mm for lane0")]


class TestAfcACECalibrateTd1Inner:
    def test_success_at_hub_writes_config(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=True)
        lane.td1_bowden_length = None
        AceP1Wire(unit)

        ok, msg, dist = unit._calibrate_td1_inner(lane, 50.0, 1.0)

        assert (ok, dist) == (True, 0.0)
        assert msg == ("ACE TD-1 calibration: filament detected at 0.0mm.\n"
                       "td1_bowden_length: 0 (was None)\n"
                       "Value saved to config.")
        assert lane.td1_bowden_length == 0.0
        assert unit.afc.function.ConfigRewrite.calls == [(
            ("AFC_lane lane0", "td1_bowden_length", 0.0,
             "\n td1_bowden_length: New: 0.0 Old: None"), {})]

    def test_not_connected_returns_early(self):
        unit = ace_p1_unit(LaneSpec("lane0", values={"td1_device_id": "td1"}))
        lane = unit.lanes["lane0"]
        unit._ace.connected = False

        result = unit._calibrate_td1_inner(lane, 50.0, 1.0)

        assert result == (False, "ACE not connected", 0)
        assert unit.logger.messages == []

    def test_missing_device_id_returns_early(self):
        unit = ace_p1_unit(LaneSpec("lane0"))
        lane = unit.lanes["lane0"]
        lane.td1_device_id = None

        result = unit._calibrate_td1_inner(lane, 50.0, 1.0)

        assert result == (False,
            "Cannot calibrate TD-1 for lane0, td1_device_id is a required "
            "field in AFC_hub or per AFC_lane", 0)
        assert unit.logger.messages == []

    def test_virtual_hub_feeds_dist_hub_when_not_staged(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=False)
        wire = AceP1Wire(unit)

        ok, msg, dist = unit._calibrate_td1_inner(lane, 50.0, 1.0)

        assert (ok, dist) == (True, 0.0)
        # Not staged at hub: retract_dist = bow_pos (0.0) + dist_hub (60).
        assert wire.moves() == [
            ("feed_filament", {"index": 0, "length": 60.0, "speed": 100.0}),
            ("unwind_filament", {"index": 0, "length": 60.0, "speed": 100.0,
                                 "mode": "normal"})]
        assert unit.logger.messages == [
            ("info", "ACE calibrate_td1: feeding slot 0 in 50.0mm steps, max 6000mm, "
                     "TD-1 device=td1"),
            ("info", "ACE calibrate_td1: feeding 60mm to virtual hub"),
            ("debug", "ACE: waiting for ready (status=busy, 0.0s/30s)"),
            ("debug", ACE_P1_TD1_DATA_LINE),
            ("info", "lane0 TD-1 data captured"),
            ("info", "ACE calibrate_td1: TD-1 detected filament at 0.0mm")]

    def test_virtual_hub_with_zero_dist_hub_skips_phase1_feed(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=False,
                                            dist_hub=0)
        wire = AceP1Wire(unit)

        ok, msg, dist = unit._calibrate_td1_inner(lane, 50.0, 1.0)

        assert (ok, dist) == (True, 0.0)
        assert wire.moves() == [
            ("unwind_filament", {"index": 0, "length": 0.0, "speed": 100.0,
                                 "mode": "normal"})]

    def test_real_hub_sensor_triggers_then_phase2_runs(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=False)
        hub = unit.printer.add_hub("realhub", switch_pin="PA1")
        lane.hub_obj = hub
        hub.state = True
        wire = AceP1Wire(unit)

        ok, msg, dist = unit._calibrate_td1_inner(lane, 50.0, 1.0)

        assert (ok, dist) == (True, 0.0)
        # Real hub, filament not yet staged: feed dist_hub + 200 then detect at hub.
        assert wire.moves()[0] == (
            "feed_filament", {"index": 0, "length": 260.0, "speed": 100.0})
        assert unit.logger.messages == [
            ("info", "ACE calibrate_td1: feeding slot 0 in 50.0mm steps, max 6000mm, "
                     "TD-1 device=td1"),
            ("info", "ACE calibrate_td1: feeding to hub sensor"),
            ("debug", "ACE: waiting for ready (status=busy, 0.0s/30s)"),
            ("debug", ACE_P1_TD1_DATA_LINE),
            ("info", "lane0 TD-1 data captured"),
            ("info", "ACE calibrate_td1: TD-1 detected filament at 0.0mm")]

    def test_real_hub_sensor_never_triggers_aborts(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=False)
        hub = unit.printer.add_hub("realhub", switch_pin="PA1")
        lane.hub_obj = hub
        hub.state = False
        AceP1Wire(unit)

        ok, msg, dist = unit._calibrate_td1_inner(lane, 50.0, 1.0)

        assert (ok, dist) == (False, 0)
        assert msg == "Hub sensor did not trigger for lane0"
        assert fetch.call_count == 0

    def test_phase2_retries_before_detecting(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=True)
        wire = AceP1Wire(unit)
        attempts: List[int] = []
        real_apply = unit._apply_td1_data

        def flaky_apply(cur_lane, compare_time, td1_data, ignore_time=False):
            """First call reports no detection yet; the retry behaves normally."""
            attempts.append(1)
            if len(attempts) == 1:
                return False
            return real_apply(cur_lane, compare_time, td1_data, ignore_time)
        unit._apply_td1_data = flaky_apply

        ok, msg, dist = unit._calibrate_td1_inner(lane, 50.0, 1.0)

        assert ok is True
        assert dist == 50.0
        assert len(attempts) == 2
        assert [m for m, _ in wire.moves()].count("feed_filament") == 1

    def test_phase2_gives_up_past_max_bowden_length(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=True)
        wire = AceP1Wire(unit)
        unit._apply_td1_data = Recorder(result=False)

        # One 7000mm step clears the 6000mm cap, so the very next pass aborts.
        ok, msg, dist = unit._calibrate_td1_inner(lane, 7000.0, 1.0)

        assert ok is False
        assert dist == 7000.0
        assert msg == "TD-1 failed to detect filament after 7000mm"
        assert [m for m, _ in wire.moves()].count("feed_filament") == 1

    def test_retract_failure_is_logged_and_still_reports_calibration(self, monkeypatch):
        unit, lane, fetch = ace_p1_td1_unit(monkeypatch, loaded_to_hub=True)
        AceP1Wire(unit)
        unit._ace.unwind_filament = Recorder(raises=RuntimeError("unwind boom"))

        ok, msg, dist = unit._calibrate_td1_inner(lane, 50.0, 1.0)

        assert ok is True
        assert unit.logger.messages == [
            ("info", "ACE calibrate_td1: feeding slot 0 in 50.0mm steps, max 6000mm, "
                     "TD-1 device=td1"),
            ("info", "ACE calibrate_td1: filament already at hub, skipping hub feed"),
            ("debug", ACE_P1_TD1_DATA_LINE),
            ("info", "lane0 TD-1 data captured"),
            ("info", "ACE calibrate_td1: TD-1 detected filament at 0.0mm"),
            ("error", "ACE calibrate_td1: retract failed: unwind boom")]


class TestAfcACEFeedToHubProbing:
    @staticmethod
    def _unit(begin: Optional[Callable[[Any, Dict[str, Any]], None]] = None,
              slot_status: str = "ready") -> Tuple[afcACE, Any, AceP1Wire]:
        """
        A unit with lane3 in slot 3 and, when given, a stage-read listener.

        :param begin: the afc_ace:stage_probe_begin handler
        :param slot_status: slot 3's status between moves
        :return tuple: the unit, lane3 and its wire
        """
        unit = ace_p1_unit(LaneSpec("lane3", slot=3, prep=True))
        if begin is not None:
            unit.printer.register_event_handler("afc_ace:stage_probe_begin", begin)
        wire = AceP1Wire(unit, slots=("ready", "ready", "ready", slot_status))
        return unit, unit.lanes["lane3"], wire

    @staticmethod
    def _scan(fed: float = 0.0, initial: float = 100.0, done: bool = False,
              removed: bool = False) -> Callable[[Any, Dict[str, Any]], None]:
        """
        :param fed: mm the scan fed
        :param initial: the mandatory initial load, mm
        :param done: the scan read a tag
        :param removed: the spool was pulled mid-scan
        :return Callable: a stage-read listener reporting that scan
        """
        def begin(lane: Any, ctx: Dict[str, Any]) -> None:
            """
            :param lane: lane being staged
            :param ctx: the stage context to fill in
            """
            ctx.update(active=True, initial=initial, fed=fed)
            if done:
                ctx["done"] = True
            if removed:
                ctx["removed"] = True
        return begin

    @staticmethod
    def _feed(length: float) -> Tuple[str, Dict[str, Any]]:
        """
        :param length: feed length, mm
        :return tuple: the feed command to slot 3 at full speed
        """
        return ("feed_filament", {"index": 3, "length": length, "speed": 100.0})

    def test_no_connection_returns_false_before_any_event_or_feed(self):
        unit, lane, wire = self._unit()
        unit._ace = None

        assert unit._feed_to_hub_probing(lane, 3, dist_hub=200.0) is False

        assert wire.moves() == []
        assert unit.printer.events == []
        assert unit.logger.messages == []

    def test_no_listener_is_plain_dist_hub_feed(self):
        unit, lane, wire = self._unit()

        assert unit._feed_to_hub_probing(lane, 3, dist_hub=200.0) is True

        assert wire.moves() == [self._feed(200.0)]
        assert [event for event, _ in unit.printer.events] == [
            "afc_ace:stage_probe_begin", "afc_ace:stage_probe_end"]
        assert unit.logger.messages == []

    def test_inactive_with_initial_is_one_plain_feed(self):
        def begin(lane: Any, ctx: Dict[str, Any]) -> None:
            """
            :param lane: lane being staged
            :param ctx: the stage context; only the initial load is set
            """
            ctx["initial"] = 100.0
        unit, lane, wire = self._unit(begin)

        unit._feed_to_hub_probing(lane, 3, dist_hub=200.0)

        assert wire.moves() == [self._feed(300.0)]
        assert unit.logger.messages == []

    def test_read_during_scan_feeds_remainder_in_one_move(self):
        unit, lane, wire = self._unit(self._scan(fed=150.0, done=True))

        unit._feed_to_hub_probing(lane, 3, dist_hub=200.0)

        assert wire.moves() == [self._feed(150.0)]
        assert unit.logger.messages == []

    def test_tagless_feeds_remainder_after_full_scan(self):
        unit, lane, wire = self._unit(self._scan(fed=200.0))

        unit._feed_to_hub_probing(lane, 3, dist_hub=200.0)

        assert wire.moves() == [self._feed(100.0)]
        assert unit.logger.messages == []

    def test_scan_fed_full_load_needs_no_remainder(self):
        unit, lane, wire = self._unit(self._scan(fed=300.0, done=True))

        assert unit._feed_to_hub_probing(lane, 3, dist_hub=200.0) is True

        assert wire.moves() == []
        assert unit.logger.messages == []

    def test_dist_hub_zero_still_does_initial_load(self):
        unit, lane, wire = self._unit(self._scan())

        unit._feed_to_hub_probing(lane, 3, dist_hub=0.0)

        assert wire.moves() == [self._feed(100.0)]
        assert unit.logger.messages == []

    def test_completed_stage_returns_true(self):
        unit, lane, wire = self._unit(self._scan())

        result = unit._feed_to_hub_probing(lane, 3, dist_hub=200.0)

        assert result is True
        assert wire.moves() == [self._feed(300.0)]
        assert unit.logger.messages == []

    def test_stage_aborts_when_scan_reports_removed(self):
        unit, lane, wire = self._unit(self._scan(fed=120.0, removed=True))

        result = unit._feed_to_hub_probing(lane, 3, dist_hub=200.0)

        assert result is False
        assert wire.moves() == []
        # The removal was reported, so the slot was never polled.
        assert unit._ace.commands == []
        assert unit.logger.messages == [
            ("debug", "ACE prep_post_load: lane3 filament removed during staging, "
                      "aborting (fed 120/300mm)")]

    def test_stage_aborts_when_slot_already_empty(self):
        unit, lane, wire = self._unit(self._scan(), slot_status="empty")

        result = unit._feed_to_hub_probing(lane, 3, dist_hub=200.0)

        assert result is False
        assert wire.moves() == []
        assert unit._ace.commands == [("get_status", {})] * 3
        assert unit.logger.messages == [
            ("debug", "ACE prep_post_load: lane3 filament removed during staging, "
                      "aborting (fed 0/300mm)")]

    def test_recovery_unwind_skipped_when_no_read(self):
        unit, lane, wire = self._unit(self._scan(fed=200.0))

        unit._feed_to_hub_probing(lane, 3, dist_hub=200.0)

        assert [method for method, _ in wire.moves()] == ["feed_filament"]
        assert unit.logger.messages == []

    def test_listener_errors_are_logged_and_the_stage_goes_on(self):
        unit, lane, wire = self._unit(Recorder(raises=RuntimeError("scan down")))
        unit.printer.register_event_handler(
            "afc_ace:stage_probe_end", Recorder(raises=RuntimeError("end down")))

        assert unit._feed_to_hub_probing(lane, 3, dist_hub=200.0) is True

        assert wire.moves() == [self._feed(200.0)]
        assert unit.logger.messages == [
            ("error", "ACE prep_post_load: stage_probe_begin error: scan down"),
            ("error", "ACE prep_post_load: stage_probe_end error: end down")]


class TestAfcACEPrepPostLoad:
    def test_prep_post_load_runs_probe_when_dist_hub_zero(self):
        unit = ace_p1_unit(LaneSpec("lane3", slot=3, prep=True, values={"dist_hub": 0}))
        lane = unit.lanes["lane3"]
        unit.printer.register_event_handler(
            "afc_ace:stage_probe_begin", lambda staged, ctx: ctx.update(initial=100.0))
        wire = AceP1Wire(unit)

        unit.prep_post_load(lane)

        # The listener's initial load ran although dist_hub is 0.
        assert wire.moves() == [("feed_filament", {"index": 3, "length": 100.0,
                                                   "speed": 100.0})]
        assert lane.loaded_to_hub is True
        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.afc.save_vars.call_count == 1
        assert unit.logger.messages == [
            ("info", "ACE prep_post_load: lane3 staged at hub (dist_hub=0mm)")]

    def test_unit_level_load_to_hub_override_is_honored(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True), values={"load_to_hub": True})
        lane = unit.lanes["lane0"]
        lane.load_to_hub = False  # a per-lane False is overridden by the unit setting.
        AceP1Wire(unit)

        unit.prep_post_load(lane)

        assert lane.loaded_to_hub is True

    def test_not_prepped_returns_early(self):
        unit = ace_p1_unit(LaneSpec("lane0"))
        lane = unit.lanes["lane0"]

        unit.prep_post_load(lane)

        assert lane.loaded_to_hub is False
        assert unit._ace.commands == []
        assert unit.logger.messages == []

    def test_not_connected_returns_early(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True))
        lane = unit.lanes["lane0"]
        unit._ace.connected = False

        unit.prep_post_load(lane)

        assert lane.loaded_to_hub is False
        assert unit.logger.messages == []

    def test_removed_during_stage_resets_prep_without_retrying(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True))
        lane = unit.lanes["lane0"]
        unit._feed_to_hub_probing = Recorder(result=False)

        unit.prep_post_load(lane)

        assert lane.loaded_to_hub is False
        # prep_state started True (LaneSpec prep=True); the abort path clears it.
        assert lane.prep_state is False
        assert unit.afc.save_vars.call_count == 1
        assert unit.logger.messages == []

    def test_td1_capture_runs_after_staging_when_configured(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, values={"td1_device_id": "td1"}))
        lane = unit.lanes["lane0"]
        lane.td1_when_loaded = True
        unit.afc.td1_present = True
        unit.capture_td1_data = Recorder(result=(True, "captured"))
        AceP1Wire(unit)

        unit.prep_post_load(lane)

        assert unit.capture_td1_data.calls == [((lane,), {})]
        assert lane.loaded_to_hub is True
        assert unit.logger.messages == [
            ("info", "ACE prep_post_load: lane0 staged at hub (dist_hub=60mm)"),
            ("info", "ACE prep_post_load: capturing TD-1 data for lane0")]

    def test_all_retries_fail_logs_warnings_then_a_final_error(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True))
        lane = unit.lanes["lane0"]
        unit._feed_to_hub_probing = Recorder(raises=RuntimeError("stage boom"))

        unit.prep_post_load(lane)

        assert lane.loaded_to_hub is False
        assert unit.logger.messages == [
            ("warning", "ACE prep_post_load: attempt 1/3 failed, retrying in 5s: "
                       "stage boom"),
            ("warning", "ACE prep_post_load: attempt 2/3 failed, retrying in 10s: "
                       "stage boom"),
            ("error", "ACE prep_post_load failed for lane0 after 3 attempts: "
                     "stage boom")]
        assert [call[0][0] for call in unit.afc.reactor.pause.calls] == [105.0, 115.0]


class TestAfcACEEjectLane:
    @staticmethod
    def _unit(**kwargs: Any) -> Tuple[afcACE, Any, AceP1Wire]:
        """
        A unit (retract_speed 50) with lane3 staged at its hub in slot 3.

        :param kwargs: make_ace_unit keywords
        :return tuple: the unit, lane3 and its wire
        """
        unit = ace_p1_unit(LaneSpec("lane3", slot=3, prep=True, load=True,
                                    values={"dist_hub": 100}),
                           values={"retract_speed": 50}, **kwargs)
        return unit, unit.lanes["lane3"], AceP1Wire(unit)

    #: dist_hub 100 + eject_buffer 475 at retract_speed 50.
    UNWIND = ("unwind_filament", {"index": 3, "length": 575.0, "speed": 50.0,
                                  "mode": "normal"})
    UNWIND_LOG = ("info", "ACE eject lane3: unwinding 575mm (dist_hub=100mm)")

    def test_disconnected_noop(self):
        unit, lane, wire = self._unit()
        unit._ace.connected = False

        unit.eject_lane(lane)

        assert lane.loaded_to_hub is True
        assert unit._ace.commands == []
        assert unit.logger.messages == []

    def test_success_clears_staging_and_suppresses(self):
        unit, lane, wire = self._unit()

        unit.eject_lane(lane)

        assert wire.moves() == [self.UNWIND]
        assert lane.loaded_to_hub is False
        assert unit._hub_load_suppressed == {"lane3"}
        assert unit.logger.messages == [self.UNWIND_LOG]

    def test_exception_logged(self):
        unit, lane, wire = self._unit()
        wire.fail("unwind_filament", RuntimeError("bad"))

        unit.eject_lane(lane)

        assert lane.loaded_to_hub is True
        assert unit._hub_load_suppressed == set()
        assert unit.logger.messages == [self.UNWIND_LOG,
                                        ("error", "ACE eject failed for lane3: bad")]

    def test_eject_suppresses_auto_reload(self):
        unit, lane, wire = self._unit()

        unit.eject_lane(lane)

        assert unit._hub_load_suppressed == {"lane3"}
        assert unit.logger.messages == [self.UNWIND_LOG]

    def test_eject_clears_hub_state(self):
        unit, lane, wire = self._unit()
        lane._load_state = True

        unit.eject_lane(lane)

        assert lane.loaded_to_hub is False
        assert lane._load_state is False
        assert unit.logger.messages == [self.UNWIND_LOG]

    def test_eject_retracts_hub_stage_distance(self):
        unit, lane, wire = self._unit()

        unit.eject_lane(lane)

        assert wire.moves() == [self.UNWIND]
        assert unit.logger.messages == [self.UNWIND_LOG]

    def test_eject_stops_the_lanes_assist_first(self):
        unit, lane, wire = self._unit(feed_assist_active=[3])

        unit.eject_lane(lane)

        # Each get_status is a ready check: the stop's, the eject's, then the
        # polls until the unwind is done.
        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 3}),
                                      ("get_status", {}), self.UNWIND, ("get_status", {}),
                                      ("get_status", {}), ("get_status", {})]
        assert unit._feed_assist_active == set()
        assert unit.logger.messages == [self.UNWIND_LOG]

    def test_eject_noop_when_disconnected(self):
        unit, lane, wire = self._unit()
        unit._ace.connected = False

        unit.eject_lane(lane)

        assert unit._hub_load_suppressed == set()
        assert wire.moves() == []
        assert lane.loaded_to_hub is True
        assert unit.logger.messages == []

    def test_eject_ends_the_unload_prepare_unload_started(self):
        unit, lane, wire = self._unit(operation_active=True)
        unit._prev_states_stale = False

        unit.eject_lane(lane)

        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.logger.messages == [self.UNWIND_LOG]

    def test_eject_clears_the_flag_when_disconnected(self):
        unit, lane, wire = self._unit(operation_active=True)
        unit._ace.connected = False

        unit.eject_lane(lane)

        assert unit._operation_active is False
        assert unit.logger.messages == []

    def test_eject_clears_the_flag_when_the_unwind_fails(self):
        unit, lane, wire = self._unit(operation_active=True)
        wire.fail("unwind_filament", RuntimeError("unwind refused"))

        unit.eject_lane(lane)

        assert unit._operation_active is False
        assert unit.logger.messages == [
            self.UNWIND_LOG, ("error", "ACE eject failed for lane3: unwind refused")]

    def test_slot_sync_runs_again_after_prepare_unload_and_eject(self):
        unit, lane, wire = self._unit()

        unit.prepare_unload(lane, None, None)
        assert unit._operation_active is True
        unit.eject_lane(lane)

        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.logger.messages == [self.UNWIND_LOG]


class TestAfcACELaneMove:
    @staticmethod
    def _unit() -> Tuple[afcACE, Any, AceP1Wire]:
        """
        :return tuple: a unit (feed 100, retract 80), lane0 and its wire
        """
        unit = ace_p1_unit(LaneSpec("lane0", prep=True), values={"retract_speed": 80})
        return unit, unit.lanes["lane0"], AceP1Wire(unit)

    def test_disconnected_logs_error(self):
        unit, lane, wire = self._unit()
        unit._ace.connected = False

        unit.lane_move(lane, 100.0, None)

        assert unit._ace.commands == []
        assert unit._prev_states_stale is False
        assert unit.logger.messages == [("error", "ACE not connected for lane_move")]

    def test_positive_feeds(self):
        unit, lane, wire = self._unit()
        seen: List[bool] = []
        wire.on_move = lambda method, params: seen.append(unit._operation_active)

        unit.lane_move(lane, 50.0, None)

        assert wire.moves() == [("feed_filament", {"index": 0, "length": 50.0,
                                                   "speed": 100.0})]
        assert seen == [True]
        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.logger.messages == []

    def test_negative_unwinds(self):
        unit, lane, wire = self._unit()

        unit.lane_move(lane, -50.0, None)

        assert wire.moves() == [("unwind_filament", {"index": 0, "length": 50.0,
                                                     "speed": 80.0, "mode": "normal"})]
        assert unit.logger.messages == []

    def test_exception_logged(self):
        unit, lane, wire = self._unit()
        wire.fail("feed_filament", RuntimeError("x"))

        unit.lane_move(lane, 50.0, None)

        assert unit._operation_active is False
        assert unit.logger.messages == [("error", "ACE lane_move failed: x")]


class TestAfcACELaneUnload:
    def test_disconnected_returns_true(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        unit._ace.connected = False

        assert unit.lane_unload(unit.lanes["lane0"]) is True
        assert unit._ace.commands == []
        assert unit.lanes["lane0"].loaded_to_hub is True
        assert unit.logger.messages == []

    def test_success_clears_hub_state(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, tool_loaded=True,
                                    values={"dist_hub": 100}))
        lane = unit.lanes["lane0"]
        lane.hub_obj.afc_unload_bowden_length = 700.0
        wire = AceP1Wire(unit)

        assert unit.lane_unload(lane) is True

        # The full path for a tool-loaded lane: dist_hub 100 + 700.
        assert wire.moves() == [("unwind_filament", {"index": 0, "length": 800.0,
                                                     "speed": 100.0, "mode": "normal"})]
        assert lane.loaded_to_hub is False
        assert unit._operation_active is False
        assert unit.logger.messages == [
            ("info", "ACE unload lane0: unwinding 800mm (dist_hub=100mm, tool_loaded=True)")]

    def test_failed_unwind_is_logged(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, values={"dist_hub": 100}))
        lane = unit.lanes["lane0"]
        wire = AceP1Wire(unit)
        wire.fail("unwind_filament", RuntimeError("jam"))

        assert unit.lane_unload(lane) is True

        assert lane.loaded_to_hub is True
        assert unit.logger.messages == [
            ("info", "ACE unload lane0: unwinding 575mm (dist_hub=100mm, tool_loaded=False)"),
            ("error", "ACE lane_unload failed for lane0: jam")]


class TestAfcACEGetLaneResetCommand:
    def test_builds_command(self):
        unit = ace_p1_unit("lane0")

        command = unit.get_lane_reset_command(unit.lanes["lane0"], 0.0)

        assert command == "AFC_ACE_LANE_RESET UNIT=Ace_1 LANE=lane0"


class TestAfcACEWaitFirstConnect:
    def test_prep_waits_until_the_first_connect_has_finished(self):
        unit = ace_p1_unit("lane0")
        reactor = unit.printer.reactor
        unit._first_connect_pending = True

        def land(now: float) -> None:
            """
            :param now: the clock after a pause; the connect lands at the 30th
            """
            if reactor.pause.call_count == 30:
                unit._first_connect_pending = False
        reactor.on_pause = land

        unit._wait_first_connect()

        assert reactor.pause.call_count == 30
        assert unit._first_connect_pending is False
        assert unit.logger.messages == [
            ("debug", "ACE Ace_1: PREP waiting for the serial connect")]

    def test_prep_does_not_wait_with_no_connect_pending(self):
        unit = ace_p1_unit("lane0")

        unit._wait_first_connect()
        unit._first_connect_pending = False
        unit._wait_first_connect()

        assert unit.printer.reactor.pause.call_count == 0
        assert unit.logger.messages == []

    def test_prep_gives_up_on_a_connect_that_never_returns(self):
        unit = ace_p1_unit("lane0")
        unit._first_connect_pending = True

        unit._wait_first_connect()

        # 100s start + 60s autodetect cap + 5s grace, in 0.1s pauses.
        assert 165.0 <= unit.printer.reactor.now < 165.1
        assert unit._first_connect_pending is True
        assert unit.logger.messages == [
            ("debug", "ACE Ace_1: PREP waiting for the serial connect")]


class TestAfcACESystemTest:
    def test_not_connected_reports_error(self):
        unit = ace_p1_unit(LaneSpec("lane0"), prep_done=False)
        unit._ace.connected = False
        lane = unit.lanes["lane0"]

        ok = unit.system_Test(lane, 0.0, True, False)

        assert ok is False
        assert lane.current_led_state == ""
        assert unit.afc.function.TcmdAssign.calls == [((lane,), {})]
        assert lane._afc_prep_done is True
        assert unit.logger.messages == [
            ("info", "lane0 tool cmd: T0  <span class=error--text>ACE NOT CONNECTED</span>")]

    def test_empty_ready_for_spool(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        unit._ace.set_reply("get_status", ace_status("empty"))

        ok = unit.system_Test(lane, 0.0, False, False)

        assert ok is True
        assert lane.prep_state is False
        assert lane.loaded_to_hub is False
        assert lane.current_led_state == "not_ready"
        assert unit.afc.function.TcmdAssign.calls == []
        assert unit.printer.reactor.pause.calls == [((100.7,), {})]
        assert unit.logger.messages == [
            ("info", "lane0 tool cmd: T0  <span class=success--text>EMPTY READY FOR "
                     "SPOOL</span>")]

    def test_present_spool_marks_loaded(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True))
        lane = unit.lanes["lane0"]

        ok = unit.system_Test(lane, 0.0, False, False)

        assert ok is True
        assert lane.prep_state is True
        assert lane.status == AFCLaneState.LOADED
        # Staged at the hub on startup (load_to_hub is on).
        assert lane.loaded_to_hub is True
        assert lane.current_led_state == "loaded"
        assert unit.afc.spool.set_active_spool.calls == []
        assert unit.logger.messages == [
            ("info", "lane0 tool cmd: T0  <span class=success--text>LOCKED</span>"
                     "<span class=success--text> AND LOADED</span>")]

    def test_present_spool_without_load_to_hub_stays_unstaged(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True))
        lane = unit.lanes["lane0"]
        lane.load_to_hub = False

        ok = unit.system_Test(lane, 0.0, False, False)

        assert ok is True
        assert lane.status == AFCLaneState.LOADED
        assert lane.loaded_to_hub is False
        assert lane._load_state is False
        assert unit.logger.messages == [
            ("info", "lane0 tool cmd: T0  <span class=success--text>LOCKED</span>"
                     "<span class=success--text> AND LOADED</span>")]

    def test_tooled_lane_without_feed_assist_starts_none(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, tool_loaded=True),
                           current="lane0", values={"use_feed_assist": False})
        lane = unit.lanes["lane0"]

        ok = unit.system_Test(lane, 0.0, False, False)

        assert ok is True
        assert lane.status == AFCLaneState.TOOLED
        assert unit._ace.commands == [("get_status", {})]
        assert unit._feed_assist_active == set()
        assert unit.printer.events == [("afc:tool_loaded", (lane,))]
        assert unit.logger.messages == [
            ("info", "lane0 tool cmd: T0  <span class=success--text>LOCKED</span>"
                     "<span class=success--text> AND LOADED</span>"
                     "<span class=primary--text> in ToolHead</span>")]

    def test_tooled_current_lane_sets_active_spool(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, tool_loaded=True),
                           current="lane0")
        lane = unit.lanes["lane0"]
        lane.spool_id = 7
        lane.sync_to_extruder = Hook(lane.sync_to_extruder)
        lane.enable_buffer = Hook(lane.enable_buffer)

        ok = unit.system_Test(lane, 0.0, False, False)

        assert ok is True
        assert lane.sync_to_extruder.calls == [((), {})]
        assert lane.enable_buffer.calls == [((), {})]
        assert lane.status == AFCLaneState.TOOLED
        assert unit.afc.spool.set_active_spool.calls == [((7,), {})]
        assert lane.current_led_state == "tool_loaded"
        assert unit.printer.events == [("afc:tool_loaded", (lane,))]
        assert unit._ace.commands == [("get_status", {}), ("get_status", {}),
                                      ("start_feed_assist", {"index": 0})]
        assert unit.logger.messages == [
            ("info", "lane0 tool cmd: T0  <span class=success--text>LOCKED</span>"
                     "<span class=success--text> AND LOADED</span>"
                     "<span class=primary--text> in ToolHead</span>")]

    def test_non_dict_status_result_leaves_prep_state_unchanged(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        unit._ace.set_reply("get_status", "oops")

        ok = unit.system_Test(lane, 0.0, False, False)

        assert ok is True
        assert lane.prep_state is True
        assert lane.status == AFCLaneState.LOADED

    def test_empty_slots_list_leaves_prep_state_unchanged(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        unit._ace.set_reply("get_status", {"status": "ready", "slots": []})

        ok = unit.system_Test(lane, 0.0, False, False)

        assert ok is True
        assert lane.prep_state is True

    def test_non_dict_slot_entry_leaves_prep_state_unchanged(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        unit._ace.set_reply("get_status", {"status": "ready", "slots": ["garbage"]})

        ok = unit.system_Test(lane, 0.0, False, False)

        assert ok is True
        assert lane.prep_state is True

    def test_get_status_exception_is_logged_as_debug(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        unit._ace.set_reply("get_status", RuntimeError("status boom"))

        ok = unit.system_Test(lane, 0.0, False, False)

        assert ok is True
        assert lane.prep_state is True
        assert unit.logger.messages[0] == (
            "debug", "ACE get_status failed during PREP: status boom")

    def test_fault_when_tool_loaded_but_slot_reports_empty(self):
        # tool_loaded drives the virtual hub's raw_load_state True even though
        # the slot read below reports empty, surfacing a prep/load mismatch.
        unit = ace_p1_unit(LaneSpec("lane0", tool_loaded=True))
        lane = unit.lanes["lane0"]
        unit._ace.set_reply("get_status", ace_status("empty"))

        ok = unit.system_Test(lane, 0.0, False, False)

        assert ok is False
        assert lane.prep_state is False
        assert lane.current_led_state == "fault"
        assert unit.logger.messages == [
            ("info", "lane0 tool cmd: T0  <span class=error--text>CHECK FILAMENT Prep: "
                     "False - Load: True</span>")]

    def test_applies_filament_defaults_when_available(self, monkeypatch):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True))
        lane = unit.lanes["lane0"]
        apply_defaults = Recorder()
        monkeypatch.setattr(afc_ace_module, "apply_filament_defaults", apply_defaults)

        ok = unit.system_Test(lane, 0.0, False, False)

        assert ok is True
        assert apply_defaults.calls == [((lane, unit._slot_inventory[0]), {
            "color_converter": None,
            "afc_defaults": {"default_material_type": None, "default_color": None}})]

    def test_tooled_lane_not_current_goes_idle(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, tool_loaded=True),
                           current="other")
        lane = unit.lanes["lane0"]

        ok = unit.system_Test(lane, 0.0, False, False)

        assert ok is True
        # afc.current != "lane0", so the TOOLED status assignment is skipped.
        assert lane.status == AFCLaneState.LOADED
        assert lane.current_led_state == "tool_loaded_idle"
        assert unit.afc.spool.set_active_spool.calls == []
        assert unit.printer.events == []


class TestAfcACEFeedUntilSensor:
    @staticmethod
    def _unit(trigger_after: Optional[int]) -> Tuple[afcACE, Any, AceP1Wire]:
        """
        :param trigger_after: feeds after which the toolhead sensor sees
            filament, None for never
        :return tuple: the unit, lane0 and its wire
        """
        unit = ace_p1_unit("lane0")
        lane = unit.lanes["lane0"]

        def on_move(method: str, params: Dict[str, Any]) -> None:
            """
            :param method: the move
            :param params: its params
            """
            fed = len([m for m, _ in wire.moves() if m == "feed_filament"])
            if trigger_after is not None and fed >= trigger_after:
                lane.extruder_obj.tool_start_state = True
        wire = AceP1Wire(unit, on_move=on_move)
        return unit, lane, wire

    @staticmethod
    def _feed(length: float) -> Tuple[str, Dict[str, Any]]:
        """
        :param length: step, mm
        :return tuple: the feed command to slot 0
        """
        return ("feed_filament", {"index": 0, "length": length, "speed": 100.0})

    def test_no_connection_returns_zero_fed_and_not_triggered(self):
        unit, lane, wire = self._unit(trigger_after=1)
        unit._ace = None

        result = unit._feed_until_sensor(0, lane, 100.0, step_size=50.0)

        assert result == (0.0, False)
        assert wire.moves() == []
        assert unit.logger.messages == []

    def test_triggers_on_first_step(self):
        unit, lane, wire = self._unit(trigger_after=1)

        result = unit._feed_until_sensor(0, lane, 100.0, step_size=50.0)

        assert result == (50.0, True)
        assert wire.moves() == [self._feed(50.0)]
        assert unit.logger.messages == [("info", "ACE calibration: sensor triggered at 50.0mm")]

    def test_reaches_max_without_trigger(self):
        unit, lane, wire = self._unit(trigger_after=None)

        result = unit._feed_until_sensor(0, lane, 120.0, step_size=50.0)

        assert result == (120.0, False)
        # The last step is cut to what is left of max_distance.
        assert wire.moves() == [self._feed(50.0), self._feed(50.0), self._feed(20.0)]
        assert unit.logger.messages == []

    def test_default_step_is_calibration_step(self):
        unit, lane, wire = self._unit(trigger_after=1)

        assert unit._feed_until_sensor(0, lane, 100.0) == (50.0, True)
        assert wire.moves() == [self._feed(50.0)]
        assert unit.logger.messages == [("info", "ACE calibration: sensor triggered at 50.0mm")]

    def test_feed_exception_retries(self):
        unit, lane, wire = self._unit(trigger_after=1)
        wire.fail("feed_filament", RuntimeError("first fails"))

        result = unit._feed_until_sensor(0, lane, 100.0, step_size=50.0)

        assert result == (50.0, True)
        assert wire.moves() == [self._feed(50.0), self._feed(50.0)]
        assert unit.logger.messages == [
            ("warning", "ACE calibration: feed failed at 0mm, retrying: first fails"),
            ("info", "ACE calibration: sensor triggered at 50.0mm")]


class TestAfcACECmdAFCACEBufferState:
    def test_not_connected(self):
        unit = ace_p2_unit()
        unit._ace.connected = False
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_BUFFER_STATE(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit._ace.commands == []

    def test_get_sensor_state_error_surfaced(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_sensor_state", RuntimeError("nope"))
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_BUFFER_STATE(gcmd)

        assert gcmd.messages == [("info", "AFC_ACE_BUFFER_STATE: nope")]
        assert unit._ace.commands == [("get_sensor_state", {})]

    def test_no_slot_sensor_data(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_sensor_state", {"sensor_bitmask": 0})
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_BUFFER_STATE(gcmd)

        assert gcmd.messages == [
            ("info", "AFC_ACE_BUFFER_STATE: no per-slot sensor data (firmware without "
                     "GET_SENSOR_STATE, or unexpected reply).")]

    def test_non_dict_reply_also_reports_no_data(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_sensor_state", None)
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_BUFFER_STATE(gcmd)

        assert gcmd.messages == [
            ("info", "AFC_ACE_BUFFER_STATE: no per-slot sensor data (firmware without "
                     "GET_SENSOR_STATE, or unexpected reply).")]

    def test_reports_each_slot_and_shared_feed_sensor(self):
        unit = ace_p2_unit(LaneSpec("lane0", slot=0))
        slot_sensors = [
            {"insert": True, "empty": False, "buf_rst": True, "buf_back": False},
            {"insert": False, "empty": True, "buf_rst": False, "buf_back": True}]
        unit._ace.set_reply("get_sensor_state",
                            {"slot_sensors": slot_sensors, "buf_feed": True})
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_BUFFER_STATE(gcmd)

        assert gcmd.messages == [("info",
            "ACE buffer/sensor state (slot: insert empty | buf_rst buf_back):\n"
            "  slot 0 (lane0): insert=1 empty=0 | buf_rst=1 buf_back=0\n"
            "  slot 1 (-): insert=0 empty=1 | buf_rst=0 buf_back=1\n"
            "  shared buf_feed=1")]


class TestAfcACECalibrateBowden:
    def test_not_connected(self):
        unit = ace_p1_unit("lane0")
        unit._ace.connected = False

        assert unit.calibrate_bowden(unit.lanes["lane0"], 50.0, 1.0) == (
            False, "ACE not connected", 0)
        assert unit._prev_states_stale is False
        assert unit.logger.messages == []

    def test_no_hub(self):
        unit = ace_p1_unit("lane0")
        lane = unit.lanes["lane0"]
        lane.hub_obj = None

        assert unit.calibrate_bowden(lane, 50.0, 1.0) == (
            False, "Lane has no hub configured", 0)
        assert unit._prev_states_stale is False
        assert unit.logger.messages == []

    def test_delegates_to_inner(self):
        unit = ace_p1_unit("lane0")
        lane = unit.lanes["lane0"]
        # A U1 sensor already holding filament: the inner method bails at once.
        lane.extruder_obj.fila_tool_start = types.SimpleNamespace(
            runout_buttun_state=1, runout_helper=types.SimpleNamespace(filament_present=True))
        seen: List[bool] = []
        unit.printer.reactor.on_pause = lambda now: seen.append(unit._operation_active)

        result = unit.calibrate_bowden(lane, 50.0, 1.0)

        assert result == (False, "Toolhead sensor already triggered, unload first", 0)
        assert seen == [True]
        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.logger.messages == []


class TestAfcACECalibrateBowdenInner:
    @staticmethod
    def _unit(trigger_at: Optional[float], **values: Any) -> Tuple[afcACE, Any, AceP1Wire]:
        """
        A unit whose U1 toolhead sensor sees filament once trigger_at mm are fed.

        :param trigger_at: mm fed when the sensor triggers, None for never
        :param values: [AFC_ACE] options
        :return tuple: the unit, lane0 and its wire
        """
        unit = ace_p1_unit("lane0", values=values)
        lane = unit.lanes["lane0"]
        lane.hub_obj.fullname = "AFC_hub Ace_1_hub"
        sensor = types.SimpleNamespace(
            runout_buttun_state=0, runout_helper=types.SimpleNamespace(filament_present=False))
        lane.extruder_obj.fila_tool_start = sensor

        def on_move(method: str, params: Dict[str, Any]) -> None:
            """
            :param method: the move
            :param params: its params
            """
            fed = sum(p["length"] for m, p in wire.moves() if m == "feed_filament")
            if trigger_at is not None and fed >= trigger_at:
                sensor.runout_buttun_state = 1
        wire = AceP1Wire(unit, on_move=on_move)
        return unit, lane, wire

    #: One default 50mm calibration step at feed_speed 100.
    STEP = ("feed_filament", {"index": 0, "length": 50.0, "speed": 100.0})

    def test_already_triggered_bails(self):
        unit, lane, wire = self._unit(trigger_at=None)
        lane.extruder_obj.fila_tool_start.runout_buttun_state = 1

        result = unit._calibrate_bowden_inner(lane, lane.hub_obj, 0)

        assert result == (False, "Toolhead sensor already triggered, unload first", 0)
        assert wire.moves() == []
        assert unit.logger.messages == []

    def test_no_connection_returns_before_clearing_sensor_state(self):
        unit, lane, wire = self._unit(trigger_at=None)
        unit._ace = None

        result = unit._calibrate_bowden_inner(lane, lane.hub_obj, 0)

        assert result == (False, "ACE not connected", 0.0)
        assert wire.moves() == []
        assert unit.logger.messages == []

    def test_success_writes_config(self):
        unit, lane, wire = self._unit(trigger_at=500.0)
        hub = lane.hub_obj

        ok, msg, dist = unit._calibrate_bowden_inner(lane, hub, 0)

        assert (ok, dist) == (True, 500.0)
        assert msg == "afc_bowden_length calibration: 500.0mm (was 900.0mm)"
        assert hub.afc_bowden_length == 500.0
        assert hub.afc_unload_bowden_length == 600.0
        # Ten 50mm steps reach the sensor, then 5mm past it is retracted.
        assert wire.moves() == [self.STEP] * 10 + [
            ("unwind_filament", {"index": 0, "length": 505.0, "speed": 100.0,
                                 "mode": "normal"})]
        assert unit.afc.function.ConfigRewrite.calls == [
            (("AFC_hub Ace_1_hub", "afc_bowden_length", 500.0,
              "\n afc_bowden_length: New: 500.0 Old: 900.0"), {}),
            (("AFC_hub Ace_1_hub", "afc_unload_bowden_length", 600.0,
              "\n afc_unload_bowden_length: 600.0"), {})]
        assert unit.logger.messages == [
            ("raw", "Calibrating afc_bowden_length for lane0 (max 6000mm in 50.0mm steps)"),
            ("info", "ACE calibration: sensor triggered at 500.0mm"),
            ("info", "ACE calibrate: trigger at 500.0mm"),
            ("info", "ACE calibrate: retracting 505mm")]

    def test_staged_lane_adds_dist_hub(self):
        unit, lane, wire = self._unit(trigger_at=500.0)
        hub = lane.hub_obj
        lane.loaded_to_hub = True

        ok, msg, dist = unit._calibrate_bowden_inner(lane, hub, 0)

        # 500 measured from the hub + dist_hub 60.
        assert (ok, dist) == (True, 560.0)
        assert msg == "afc_bowden_length calibration: 560.0mm (was 900.0mm)"
        assert hub.afc_bowden_length == 560.0
        assert hub.afc_unload_bowden_length == 600.0
        # The retract is the measured 500mm + 5, not the staged total.
        assert wire.moves() == [self.STEP] * 10 + [
            ("unwind_filament", {"index": 0, "length": 505.0, "speed": 100.0,
                                 "mode": "normal"})]
        assert unit.afc.function.ConfigRewrite.calls == [
            (("AFC_hub Ace_1_hub", "afc_bowden_length", 560.0,
              "\n afc_bowden_length: New: 560.0 Old: 900.0"), {}),
            (("AFC_hub Ace_1_hub", "afc_unload_bowden_length", 600.0,
              "\n afc_unload_bowden_length: 600.0"), {})]
        assert unit.logger.messages == [
            ("raw", "Calibrating afc_bowden_length for lane0 (max 6000mm in 50.0mm steps)"),
            ("info", "ACE calibration: sensor triggered at 500.0mm"),
            ("info", "ACE calibrate: trigger at 500.0mm"),
            ("info", "ACE calibrate: retracting 505mm"),
            ("info", "ACE calibrate: filament at hub, adding dist_hub=60mm to measured "
                     "500.0mm")]

    def test_no_trigger_retracts_and_fails(self):
        unit, lane, wire = self._unit(trigger_at=None, calibration_step=3000)
        hub = lane.hub_obj

        ok, msg, dist = unit._calibrate_bowden_inner(lane, hub, 0)

        assert (ok, dist) == (False, 6000.0)
        assert msg == ("Toolhead sensor did not trigger after 6000mm. Check filament path "
                       "and sensor wiring.")
        # Two 3000mm steps reach the 6000mm cap, then all of it is retracted.
        step = ("feed_filament", {"index": 0, "length": 3000.0, "speed": 100.0})
        assert wire.moves() == [step, step, (
            "unwind_filament", {"index": 0, "length": 6000.0, "speed": 100.0,
                                "mode": "normal"})]
        assert hub.afc_bowden_length == 900.0
        assert unit.afc.function.ConfigRewrite.calls == []
        assert unit.logger.messages == [
            ("raw", "Calibrating afc_bowden_length for lane0 (max 6000mm in 3000.0mm steps)"),
            ("info", "ACE calibrate: retracting 6000mm")]

    def test_no_trigger_retract_failure_is_logged(self):
        unit, lane, wire = self._unit(trigger_at=None, calibration_step=3000)
        hub = lane.hub_obj
        unit._ace.unwind_filament = Recorder(raises=RuntimeError("unwind boom"))

        ok, msg, dist = unit._calibrate_bowden_inner(lane, hub, 0)

        assert (ok, dist) == (False, 6000.0)
        assert unit.logger.messages == [
            ("raw", "Calibrating afc_bowden_length for lane0 (max 6000mm in 3000.0mm steps)"),
            ("info", "ACE calibrate: retracting 6000mm"),
            ("error", "ACE calibrate: retract failed: unwind boom")]

    def test_success_retract_failure_is_logged(self):
        unit, lane, wire = self._unit(trigger_at=500.0)
        hub = lane.hub_obj
        unit._ace.unwind_filament = Recorder(raises=RuntimeError("unwind boom"))

        ok, msg, dist = unit._calibrate_bowden_inner(lane, hub, 0)

        assert (ok, dist) == (True, 500.0)
        assert unit.logger.messages == [
            ("raw", "Calibrating afc_bowden_length for lane0 (max 6000mm in 50.0mm steps)"),
            ("info", "ACE calibration: sensor triggered at 500.0mm"),
            ("info", "ACE calibrate: trigger at 500.0mm"),
            ("info", "ACE calibrate: retracting 505mm"),
            ("error", "ACE calibrate: retract failed: unwind boom")]

    def test_staged_lane_with_zero_dist_hub_skips_the_addition(self):
        unit, lane, wire = self._unit(trigger_at=500.0)
        hub = lane.hub_obj
        lane.loaded_to_hub = True
        lane.dist_hub = 0

        ok, msg, dist = unit._calibrate_bowden_inner(lane, hub, 0)

        # dist_hub is 0, so nothing is added to the measured distance.
        assert (ok, dist) == (True, 500.0)
        assert unit.logger.messages == [
            ("raw", "Calibrating afc_bowden_length for lane0 (max 6000mm in 50.0mm steps)"),
            ("info", "ACE calibration: sensor triggered at 500.0mm"),
            ("info", "ACE calibrate: trigger at 500.0mm"),
            ("info", "ACE calibrate: retracting 505mm")]


class TestAfcACECalibrateLane:
    def test_wraps_hub_inner(self):
        unit = ace_p1_unit("lane0")
        lane = unit.lanes["lane0"]
        hub = unit.printer.add_hub("real_hub", switch_pin="PA1")
        lane.hub_obj = hub
        seen: List[bool] = []

        def on_move(method: str, params: Dict[str, Any]) -> None:
            """
            The hub switch sees the filament on the first feed.

            :param method: the move
            :param params: its params
            """
            seen.append(unit._operation_active)
            hub.state = True
        AceP1Wire(unit, on_move=on_move)

        result = unit.calibrate_lane(lane, 1.0)

        assert result == (True, "dist_hub calibration: 50.0mm (was 60mm)", 50.0)
        assert lane.dist_hub == 50.0
        assert seen == [True]
        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.afc.function.ConfigRewrite.calls == [
            (("AFC_lane lane0", "dist_hub", 50.0, "\n dist_hub: New: 50.0 Old: 60"), {})]
        assert unit.logger.messages == [
            ("raw", "Calibrating dist_hub for lane0 (max 4000mm in 50.0mm steps)"),
            ("info", "ACE hub calibrate: coarse trigger at 50.0mm"),
            ("info", "ACE hub calibrate: trigger at 50.0mm")]


class TestAfcACELogDelta:
    def test_log_delta_starts_clock_when_unstarted(self):
        unit = ace_p1_unit("lane0")
        delta = unit.afc.afcDeltaTime

        unit._log_delta("hello")

        assert delta.start_time == 0.0
        assert delta.messages == [("debug", "hello")]

    def test_log_delta_keeps_a_started_clock(self):
        unit = ace_p1_unit("lane0")
        delta = unit.afc.afcDeltaTime
        delta.start_time = 5.0

        unit._log_delta("hello", debug=False)

        assert delta.start_time == 5.0
        assert delta.messages == [("info", "hello")]

    def test_log_delta_swallows_upstream_error(self):
        unit = ace_p1_unit("lane0")
        delta = unit.afc.afcDeltaTime
        delta.start_time = 5.0
        delta.log_with_time = Recorder(
            raises=TypeError("unsupported operand type(s) for -: datetime vs None"))

        unit._log_delta("hello")

        assert delta.log_with_time.calls == [(("hello",), {"debug": True})]
        assert unit.logger.messages == []

    def test_log_delta_without_a_delta_timer_is_a_no_op(self):
        unit = ace_p1_unit("lane0")
        unit.afc.afcDeltaTime = None

        unit._log_delta("hello")

        assert unit.logger.messages == []


class TestAfcACEUnitLoadLane:
    def test_failure_returns_false(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        status = lane.status
        unit._ace.connected = False

        assert unit.unit_load_lane(lane, lane.extruder_obj) is False

        assert lane.status == status
        assert unit.afc.save_vars.calls == []
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, "ACE not connected (/dev/ttyACM0)"), {"pause": False})]
        assert unit.logger.messages == []
        assert unit.afc.afcDeltaTime.messages == []

    def test_success_sets_status(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        AceP1Wire(unit, on_move=lambda method, params: setattr(
            lane.extruder_obj, "tool_start_state", True))

        assert unit.unit_load_lane(lane, lane.extruder_obj) is True

        assert lane.status == AFCLaneState.TOOL_LOADED
        assert unit.afc.save_vars.call_count == 1
        assert unit.afc.error.handle_lane_failure.calls == []
        assert unit.logger.messages == [
            ("debug", "ACE Ace_1: feed on slot 0 stopped at the toolhead sensor"),
            ("info", "ACE load: advancing tool_stn 72mm to nozzle for lane0")]
        assert unit.afc.afcDeltaTime.messages == [
            ("debug", "ACE load: tool_stn to nozzle"), ("debug", "ACE load transport complete")]


class TestAfcACEUnitUnloadLane:
    @staticmethod
    def _unit(operation_active: bool = True) -> Tuple[afcACE, Any, AceP1Wire]:
        """
        lane0 loaded in the toolhead, its unload already begun by prepare_unload.

        :param operation_active: the flag prepare_unload leaves set
        :return tuple: the unit, lane0 and its wire
        """
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, tool_loaded=True),
                           operation_active=operation_active)
        unit._prev_states_stale = False
        return unit, unit.lanes["lane0"], AceP1Wire(unit)

    #: The quick pull, then half and all of tool_stn_unload 100 at tool_unload_speed 25.
    MOVES = [((-2, 25.0, "Quick Pull"), {"wait_tool": False}),
             ((-50.0, 25.0, "ACE STN unload"), {"wait_tool": True}),
             ((-100.0, 25.0, "ACE STN unload"), {"wait_tool": False})]
    #: The hub's afc_unload_bowden_length 900 at retract_speed 100.
    UNWIND = ("unwind_filament", {"index": 0, "length": 900.0, "speed": 100.0,
                                  "mode": "normal"})
    #: The two tool_stn_unload retracts, timed before the unwind.
    RETRACT_DELTAS = [("debug", "ACE unload: retracting tool_stn_unload/2 (pre-unwind)"),
                      ("debug", "ACE unload: retracting tool_stn_unload (overlaps unwind)")]
    UNWOUND_DELTA = ("debug", "ACE unwind complete")

    def test_success_full_sequence(self):
        unit, lane, wire = self._unit()
        extruder = lane.extruder_obj
        assert lane._load_state is True

        assert unit.unit_unload_lane(lane, extruder) is True

        assert unit.afc.move_e_pos.calls == self.MOVES
        assert unit.afc.do_tool_cut_tip_form.calls == [((lane, extruder), {})]
        assert wire.moves() == [self.UNWIND]
        assert lane.status == AFCLaneState.NONE
        assert lane.tool_loaded is False
        assert extruder.lane_loaded is None
        assert unit.afc.spool.set_active_spool.calls == [((None,), {})]
        assert lane.loaded_to_hub is True
        # The virtual hub reads clear once the tool is unloaded.
        assert lane._load_state is False
        assert unit._hub_load_suppressed == {"lane0"}
        assert unit.afc.gcode.run_script_from_command.calls == []
        # One from the lane's disable_buffer (its weight timer), one at the end.
        assert unit.afc.save_vars.call_count == 2
        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.afc.afcDeltaTime.messages == self.RETRACT_DELTAS + [self.UNWOUND_DELTA]
        assert unit.logger.messages == []
        assert unit.afc.error.handle_lane_failure.calls == []

    def test_post_unload_macro_runs(self):
        unit, lane, wire = self._unit()
        unit.afc.post_unload_macro = "POST_UNLOAD"

        assert unit.unit_unload_lane(lane, lane.extruder_obj) is True

        assert unit.afc.gcode.run_script_from_command.calls == [(("POST_UNLOAD",), {})]
        assert unit.afc.afcDeltaTime.messages == self.RETRACT_DELTAS + [self.UNWOUND_DELTA]
        assert unit.logger.messages == []
        assert unit.afc.error.handle_lane_failure.calls == []

    def test_unload_sequence_failure_returns_false(self):
        unit, lane, wire = self._unit()
        wire.fail("unwind_filament", RuntimeError("jam"))

        assert unit.unit_unload_lane(lane, lane.extruder_obj) is False

        assert lane.status == AFCLaneState.TOOL_UNLOADING
        assert lane.tool_loaded is True
        assert unit.afc.spool.set_active_spool.calls == []
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, "ACE unwind failed for lane0: jam"), {"pause": False})]
        # Only the lane's disable_buffer saved.
        assert unit.afc.save_vars.call_count == 1
        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        # The unwind failed, so it was never timed as complete.
        assert unit.afc.afcDeltaTime.messages == self.RETRACT_DELTAS
        assert unit.logger.messages == []

    def test_cut_exception_still_clears_operation_flag(self):
        unit, lane, wire = self._unit()
        unit.afc.do_tool_cut_tip_form = Recorder(raises=RuntimeError("cut boom"))

        with pytest.raises(RuntimeError, match="cut boom"):
            unit.unit_unload_lane(lane, lane.extruder_obj)

        assert unit.afc.move_e_pos.calls == self.MOVES[:1]
        assert wire.moves() == []
        assert lane.status == AFCLaneState.TOOL_UNLOADING
        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.afc.afcDeltaTime.messages == []
        assert unit.logger.messages == []
        assert unit.afc.error.handle_lane_failure.calls == []


class TestAfcACEAceLoadSequence:
    def test_wraps_inner_and_clears_flag(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True))
        lane = unit.lanes["lane0"]
        unit._prev_states_stale = False
        seen: List[bool] = []

        def on_move(method: str, params: Dict[str, Any]) -> None:
            """
            Note the flag mid-feed; the toolhead sensor then sees the filament.

            :param method: the move
            :param params: its params
            """
            seen.append(unit._operation_active)
            lane.extruder_obj.tool_start_state = True
        AceP1Wire(unit, on_move=on_move)

        assert unit._ace_load_sequence(lane, lane.extruder_obj) is True

        assert seen == [True]
        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.logger.messages == [
            ("debug", "ACE Ace_1: feed on slot 0 stopped at the toolhead sensor"),
            ("info", "ACE load: advancing tool_stn 72mm to nozzle for lane0")]
        assert unit.afc.afcDeltaTime.messages == [
            ("debug", "ACE load: tool_stn to nozzle"), ("debug", "ACE load transport complete")]
        assert unit.afc.error.handle_lane_failure.calls == []

    def test_a_failed_load_still_clears_flag(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True), operation_active=True)
        lane = unit.lanes["lane0"]
        unit._prev_states_stale = False
        unit._ace.connected = False

        assert unit._ace_load_sequence(lane, lane.extruder_obj) is False

        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, "ACE not connected (/dev/ttyACM0)"), {"pause": False})]
        assert unit.logger.messages == []
        assert unit.afc.afcDeltaTime.messages == []


class TestAfcACEAceLoadInner:
    #: The refusal when the toolhead already holds filament, for lane0.
    PRE_FEED = ("Toolhead sensor detects filament before ACE feed for lane0.\n"
                "Filament may be stuck from a previous load, clear the toolhead before "
                "loading.\nTo resolve, manually retract filament from the toolhead or run "
                "AFC_RESET for lane0.")
    #: A 1s load_retry_timeout leaves room for one retry pulse.
    NO_SENSOR = ("Filament did not reach toolhead sensor after feed + 1 retry pulses "
                 "(1s timeout) for lane3.")
    STALL_HINT = ("\nThe ACE reported a feed error: its encoder saw lane3's filament stop "
                  "moving in the tube. Check for a kink, a damaged spot on the filament or "
                  "a blockage, clear it, then load again.")
    BOWDEN_HINT = "\nCheck filament path and bowden length calibration."
    STALL_LOG = ("warning", "ACE Ace_1: slot 3 reported feed_error")
    PULSE_LOG = ("info", "Sensor not triggered after feed for lane3, retry pulse 1 (100mm)")
    AT_SENSOR = ("debug", "ACE Ace_1: feed on slot 3 stopped at the toolhead sensor")
    TO_NOZZLE = ("info", "ACE load: advancing tool_stn 72mm to nozzle for lane3")
    #: The timing lines of a load that reached the toolhead and moved on to the nozzle.
    DELTAS = [("debug", "ACE load: tool_stn to nozzle"),
              ("debug", "ACE load transport complete")]

    @staticmethod
    def _staged(**values: Any) -> Tuple[afcACE, Any]:
        """
        lane3 (slot 3) staged at a hub 3900mm of bowden short of the toolhead,
        on a unit feeding at 80mm/s, so a feed is told apart from the 100mm/s
        retract_speed.

        :param values: [AFC_ACE] options over a 1s load_retry_timeout
        :return tuple: the unit and lane3
        """
        unit = ace_p1_unit(LaneSpec("lane3", slot=3, prep=True, load=True),
                           values={"load_retry_timeout": 1.0, "feed_speed": 80, **values})
        lane = unit.lanes["lane3"]
        lane.hub_obj.afc_bowden_length = 3900.0
        return unit, lane

    @staticmethod
    def _wire(unit: afcACE, lane: Any, sensor_on: Optional[int]) -> AceP1Wire:
        """
        :param unit: unit to drive
        :param lane: lane whose toolhead sensor to trip
        :param sensor_on: move (1-based) on which the toolhead sensor sees
            filament, None for never
        :return AceP1Wire: the unit's wire
        """
        def on_move(method: str, params: Dict[str, Any]) -> None:
            """
            :param method: the move
            :param params: its params
            """
            if len(wire.moves()) == sensor_on:
                lane.extruder_obj.tool_start_state = True
        wire = AceP1Wire(unit, on_move=on_move)
        return wire

    @staticmethod
    def _feed(length: float, speed: float, slot: int = 3) -> Tuple[str, Dict[str, Any]]:
        """
        :param length: mm
        :param speed: mm/s
        :param slot: 0-based slot
        :return tuple: the feed command
        """
        return ("feed_filament", {"index": slot, "length": length, "speed": speed})

    def test_ace_load_stops_other_assist_before_feed_in_direct_mode(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True),
                           LaneSpec("lane2", slot=2, prep=True, load=True),
                           values={"mode": "direct"}, feed_assist_active=[2])
        lane = unit.lanes["lane0"]
        # Bail at the pre-feed check, right after the stop.
        lane.extruder_obj.tool_start_state = True

        assert unit._ace_load_inner(lane, lane.extruder_obj) is False

        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 2})]
        assert unit._feed_assist_active == set()
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, self.PRE_FEED), {"pause": False})]
        assert unit.logger.messages == []
        assert unit.afc.afcDeltaTime.messages == []

    def test_own_slot_assist_is_left_running(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True), feed_assist_active=[0])
        lane = unit.lanes["lane0"]
        lane.extruder_obj.tool_start_state = True

        assert unit._ace_load_inner(lane, lane.extruder_obj) is False

        assert unit._ace.commands == []
        assert unit._feed_assist_active == {0}
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, self.PRE_FEED), {"pause": False})]
        assert unit.logger.messages == []
        assert unit.afc.afcDeltaTime.messages == []

    def test_not_connected_fails(self):
        unit, lane = self._staged()
        unit._ace.connected = False

        assert unit._ace_load_inner(lane, lane.extruder_obj) is False

        assert unit._ace.commands == []
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, "ACE not connected (/dev/ttyACM0)"), {"pause": False})]
        assert unit.logger.messages == []
        assert unit.afc.afcDeltaTime.messages == []

    def test_pre_feed_sensor_triggered_fails(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True), in_print=True)
        lane = unit.lanes["lane0"]
        unit._hub_load_suppressed.add("lane0")
        lane.extruder_obj.tool_start_state = True

        assert unit._ace_load_inner(lane, lane.extruder_obj) is False

        assert unit._ace.commands == []
        # The load already lifted the auto-reload hold before the check.
        assert unit._hub_load_suppressed == set()
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, f"{self.PRE_FEED}\nOnce cleared, click resume to continue printing"),
             {"pause": True})]
        assert unit.logger.messages == []
        assert unit.afc.afcDeltaTime.messages == []

    def test_buffer_advance_latch_is_enabled_before_the_feed(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True))
        lane = unit.lanes["lane0"]
        lane.buffer_obj = types.SimpleNamespace(enable_advance_latch=Recorder())
        lane.extruder_obj.tool_start_state = True

        assert unit._ace_load_inner(lane, lane.extruder_obj) is False

        assert lane.buffer_obj.enable_advance_latch.calls == [((), {})]
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, self.PRE_FEED), {"pause": False})]
        assert unit.logger.messages == []
        assert unit.afc.afcDeltaTime.messages == []

    def test_success_marks_loaded_and_returns_true(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True))
        lane = unit.lanes["lane0"]
        self._wire(unit, lane, sensor_on=1)

        assert unit._ace_load_inner(lane, lane.extruder_obj) is True

        # Unstaged: dist_hub 60 + bowden 900, less the 30mm slow approach,
        # which the sensor makes unnecessary; the feed stops at the sensor.
        assert unit._ace.commands == [
            ("get_status", {}), self._feed(930.0, 100.0, slot=0),
            ("stop_feed_filament", {"index": 0}), ("get_status", {}),
            ("get_status", {}), ("start_feed_assist", {"index": 0})]
        assert lane.loaded_to_hub is True
        assert unit._feed_assist_active == {0}
        assert unit.afc.move_e_pos.calls == [((72.0, 25.0, "tool stn"), {})]
        assert unit.afc.afcDeltaTime.messages == self.DELTAS
        assert unit.afc.error.handle_lane_failure.calls == []
        assert unit.logger.messages == [
            ("debug", "ACE Ace_1: feed on slot 0 stopped at the toolhead sensor"),
            ("info", "ACE load: advancing tool_stn 72mm to nozzle for lane0")]

    def test_no_assist_and_no_tool_stn_skip_both(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True), values={"use_feed_assist": False})
        lane = unit.lanes["lane0"]
        lane.extruder_obj.tool_stn = 0
        self._wire(unit, lane, sensor_on=1)

        assert unit._ace_load_inner(lane, lane.extruder_obj) is True

        # No start_feed_assist (nor its ready check) after the feed stopped at the sensor.
        assert unit._ace.commands == [
            ("get_status", {}), self._feed(930.0, 100.0, slot=0),
            ("stop_feed_filament", {"index": 0}), ("get_status", {})]
        assert unit._feed_assist_active == set()
        assert unit.afc.move_e_pos.calls == []
        assert unit.afc.afcDeltaTime.messages == [("debug", "ACE load transport complete")]
        # No tool_stn advance line.
        assert unit.logger.messages == [
            ("debug", "ACE Ace_1: feed on slot 0 stopped at the toolhead sensor")]
        assert unit.afc.error.handle_lane_failure.calls == []

    def test_refused_feed_is_reported(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True))
        lane = unit.lanes["lane0"]
        wire = AceP1Wire(unit)
        wire.fail("feed_filament", RuntimeError("FORBIDDEN"))

        assert unit._ace_load_inner(lane, lane.extruder_obj) is False

        assert lane.loaded_to_hub is False
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, "ACE load feed error: FORBIDDEN"), {"pause": False})]
        assert unit.afc.afcDeltaTime.messages == []
        assert unit.logger.messages == []

    #: Logged when a long feed never shows motion on slot 0.
    NO_START = ("debug", "ACE wait: slot 0 never reported motion after feed/unwind command, "
                         "motor may not have started")

    def test_a_feed_that_never_starts_is_retried_then_fails(self):
        # No wire: the slot never shows motion, so every long feed is a no-start.
        unit = ace_p1_unit(LaneSpec("lane0", prep=True), values={"load_approach_length": 0})
        lane = unit.lanes["lane0"]
        no_start = self.NO_START

        assert unit._ace_load_inner(lane, lane.extruder_obj) is False

        feeds = [(m, p) for m, p in unit._ace.commands if m == "feed_filament"]
        # dist_hub 60 + bowden 900, sent once and resent three times.
        assert feeds == [self._feed(960.0, 100.0, slot=0)] * 4
        assert lane.loaded_to_hub is False
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, "ACE feed failed for lane0"), {"pause": False})]
        assert unit.logger.messages == [
            no_start,
            ("info", "Feed retry 1/3 for lane0 (960mm)"), no_start,
            ("info", "Feed retry 2/3 for lane0 (960mm)"), no_start,
            ("info", "Feed retry 3/3 for lane0 (960mm)"), no_start]
        assert unit.afc.afcDeltaTime.messages == []

    def test_a_no_start_fast_feed_is_resent_not_left_to_the_approach(self):
        # The approach feed is skipped: its short move would pass as "completed
        # before motion was observed" and hide the fast feed's no-start.
        unit = ace_p1_unit(LaneSpec("lane0", prep=True), values={"load_retry_timeout": 1.0})
        lane = unit.lanes["lane0"]
        no_start = self.NO_START

        assert unit._ace_load_inner(lane, lane.extruder_obj) is False

        feeds = [(m, p) for m, p in unit._ace.commands if m == "feed_filament"]
        assert feeds == [self._feed(930.0, 100.0, slot=0)] + [self._feed(960.0, 100.0, slot=0)] * 3
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, "ACE feed failed for lane0"), {"pause": False})]
        assert unit.logger.messages == [
            no_start,
            ("info", "Feed retry 1/3 for lane0 (960mm)"), no_start,
            ("info", "Feed retry 2/3 for lane0 (960mm)"), no_start,
            ("info", "Feed retry 3/3 for lane0 (960mm)"), no_start]
        assert unit.afc.afcDeltaTime.messages == []

    def test_stalled_load_still_kicks_then_reports_the_stall(self):
        unit, lane = self._staged(load_approach_length=0)
        wire = self._wire(unit, lane, sensor_on=None)
        wire.stall(*[True] * 4)

        assert unit._ace_load_inner(lane, lane.extruder_obj) is False

        # The full 3900mm and its two resends, then the 100mm retry pulse.
        assert wire.moves() == [self._feed(3900.0, 80.0)] * 3 + [self._feed(100.0, 50.0)]
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, self.NO_SENSOR + self.STALL_HINT), {"pause": False})]
        resend = ("info", "ACE load: lane3 hit a feed error, feeding the remaining 3900mm again")
        assert unit.logger.messages == [
            self.STALL_LOG, resend, self.STALL_LOG, resend, self.STALL_LOG,
            self.PULSE_LOG, self.STALL_LOG]
        assert unit.afc.afcDeltaTime.messages == []

    def test_short_load_without_a_stall_keeps_the_bowden_hint(self):
        unit, lane = self._staged(load_approach_length=0)
        wire = self._wire(unit, lane, sensor_on=None)

        assert unit._ace_load_inner(lane, lane.extruder_obj) is False

        assert wire.moves() == [self._feed(3900.0, 80.0), self._feed(100.0, 50.0)]
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, self.NO_SENSOR + self.BOWDEN_HINT), {"pause": False})]
        assert unit.logger.messages == [self.PULSE_LOG]
        assert unit.afc.afcDeltaTime.messages == []

    def test_load_feeds_fast_then_approaches_slowly(self):
        unit, lane = self._staged(load_approach_length=150, load_approach_speed=25)
        wire = self._wire(unit, lane, sensor_on=2)

        assert unit._ace_load_inner(lane, lane.extruder_obj) is True

        assert wire.moves() == [self._feed(3750.0, 80.0), self._feed(150.0, 25.0)]
        assert unit.logger.messages == [self.AT_SENSOR, self.TO_NOZZLE]
        assert unit.afc.afcDeltaTime.messages == self.DELTAS
        assert unit.afc.error.handle_lane_failure.calls == []

    def test_load_skips_the_approach_once_the_sensor_has_it(self):
        unit, lane = self._staged(load_approach_length=150, load_approach_speed=25)
        wire = self._wire(unit, lane, sensor_on=1)

        assert unit._ace_load_inner(lane, lane.extruder_obj) is True

        assert wire.moves() == [self._feed(3750.0, 80.0)]
        assert unit.logger.messages == [self.AT_SENSOR, self.TO_NOZZLE]
        assert unit.afc.afcDeltaTime.messages == self.DELTAS
        assert unit.afc.error.handle_lane_failure.calls == []

    def test_early_feed_error_resends_the_rest_of_the_long_feed(self):
        unit, lane = self._staged(load_approach_length=150, load_approach_speed=25)
        wire = self._wire(unit, lane, sensor_on=3)
        wire.stall(True, False)
        unit._ace.set_reply("get_feed_info", {"feed_info": [{}, {}, {}, {"length": 300.0}]})

        assert unit._ace_load_inner(lane, lane.extruder_obj) is True

        assert wire.moves() == [self._feed(3750.0, 80.0), self._feed(3450.0, 80.0),
                                self._feed(150.0, 25.0)]
        assert unit.logger.messages == [
            self.STALL_LOG,
            ("info", "ACE load: lane3 hit a feed error after 300mm, feeding the remaining "
                     "3450mm again"),
            self.AT_SENSOR, self.TO_NOZZLE]
        assert unit.afc.afcDeltaTime.messages == self.DELTAS
        assert unit.afc.error.handle_lane_failure.calls == []

    def test_resend_without_feed_info_sends_the_full_length(self):
        unit, lane = self._staged(load_approach_length=150, load_approach_speed=25)
        wire = self._wire(unit, lane, sensor_on=2)
        wire.stall(True, False)

        assert unit._ace_load_inner(lane, lane.extruder_obj) is True

        assert wire.moves() == [self._feed(3750.0, 80.0)] * 2
        assert unit.logger.messages == [
            self.STALL_LOG,
            ("info", "ACE load: lane3 hit a feed error, feeding the remaining 3750mm again"),
            self.AT_SENSOR, self.TO_NOZZLE]
        assert unit.afc.afcDeltaTime.messages == self.DELTAS
        assert unit.afc.error.handle_lane_failure.calls == []

    def test_resends_are_capped(self):
        unit, lane = self._staged(load_approach_length=150, load_approach_speed=25)
        wire = self._wire(unit, lane, sensor_on=None)
        wire.stall(*[True] * 5)
        unit._ace.set_reply("get_feed_info", {"feed_info": [{}, {}, {}, {"length": 100.0}]})

        assert unit._ace_load_inner(lane, lane.extruder_obj) is False

        # Two resends, each 100mm shorter, then the approach and one retry pulse.
        assert wire.moves() == [self._feed(3750.0, 80.0), self._feed(3650.0, 80.0),
                                self._feed(3550.0, 80.0), self._feed(150.0, 25.0),
                                self._feed(100.0, 25.0)]
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, self.NO_SENSOR + self.STALL_HINT), {"pause": False})]
        assert unit.logger.messages == [
            self.STALL_LOG,
            ("info", "ACE load: lane3 hit a feed error after 100mm, feeding the remaining "
                     "3650mm again"),
            self.STALL_LOG,
            ("info", "ACE load: lane3 hit a feed error after 100mm, feeding the remaining "
                     "3550mm again"),
            self.STALL_LOG, self.STALL_LOG, self.PULSE_LOG, self.STALL_LOG]
        assert unit.afc.afcDeltaTime.messages == []

    def test_feed_error_with_near_complete_fed_length_stops_without_a_resend(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True),
                           values={"load_approach_length": 0, "load_retry_timeout": 1.0})
        lane = unit.lanes["lane0"]
        wire = AceP1Wire(unit)
        wire.stall(True)
        unit._ace.set_reply("get_feed_info", {"feed_info": [{"length": 959.6}]})

        assert unit._ace_load_inner(lane, lane.extruder_obj) is False

        feeds = [(m, p) for m, p in unit._ace.commands if m == "feed_filament"]
        # dist_hub 60 + bowden 900 = 960mm; the near-complete fed length (959.6mm)
        # leaves under 0.5mm remaining, so the 960mm fast feed is sent exactly
        # once, with no resend of it (the wait still reports success since the
        # unit went idle, so the single stall alone never fails the move).
        assert feeds.count(self._feed(960.0, 100.0, slot=0)) == 1
        assert not any("feeding the remaining" in msg for _, msg in unit.logger.messages)

    def test_sensor_triggers_during_a_retry_pulse(self):
        unit, lane = self._staged(load_approach_length=0, load_retry_timeout=5.0)
        wire = self._wire(unit, lane, sensor_on=2)

        assert unit._ace_load_inner(lane, lane.extruder_obj) is True

        assert wire.moves() == [self._feed(3900.0, 80.0), self._feed(100.0, 50.0)]
        assert unit.logger.messages == [
            self.PULSE_LOG,
            self.AT_SENSOR,
            ("info", "Sensor triggered on retry pulse 1 for lane3"),
            self.TO_NOZZLE]
        assert unit.afc.afcDeltaTime.messages == self.DELTAS
        assert unit.afc.error.handle_lane_failure.calls == []

    def test_feed_failure_with_sensor_already_triggered_continues(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True), values={"load_approach_length": 0})
        lane = unit.lanes["lane0"]
        AceP1Wire(unit)

        def fake_wait(*args: Any, **kwargs: Any) -> bool:
            """The sensor trips during this failed wait, as a real one can."""
            lane.extruder_obj.tool_start_state = True
            return False
        unit._wait_for_feed_complete = fake_wait

        assert unit._ace_load_inner(lane, lane.extruder_obj) is True

        assert unit.logger.messages == [
            ("info", "Feed error but sensor triggered, continuing"),
            ("debug", "ACE: waiting for ready (status=busy, 0.0s/30s)"),
            ("info", "ACE load: advancing tool_stn 72mm to nozzle for lane0")]
        assert unit.afc.error.handle_lane_failure.calls == []

    def test_smart_load_retry_recovers_then_continues(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True), values={"load_approach_length": 0})
        lane = unit.lanes["lane0"]
        AceP1Wire(unit)
        unit._wait_for_feed_complete = Recorder(result=False)

        def fake_retry(cur_lane: Any, slot: int, feed_dist: float) -> bool:
            """Recovers on retry, as a real smart_load_retry that found the sensor."""
            lane.extruder_obj.tool_start_state = True
            return True
        unit._smart_load_retry = fake_retry

        assert unit._ace_load_inner(lane, lane.extruder_obj) is True

        assert unit.afc.error.handle_lane_failure.calls == []

    def test_pulse_exception_is_swallowed_and_retried(self):
        unit, lane = self._staged(load_approach_length=0, load_retry_timeout=2.0)
        wire = self._wire(unit, lane, sensor_on=2)
        original_feed = unit._ace.feed_filament
        calls: List[int] = []

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            """The first retry pulse raises, as a transient serial write can."""
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("pulse boom")
            return original_feed(*args, **kwargs)
        unit._ace.feed_filament = wrapped

        assert unit._ace_load_inner(lane, lane.extruder_obj) is True

        assert len(calls) == 3
        assert wire.moves() == [self._feed(3900.0, 80.0), self._feed(100.0, 50.0)]
        assert unit.afc.error.handle_lane_failure.calls == []


class TestAfcACELaneUnloading:
    def test_calls_prepare_unload(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, tool_loaded=True),
                           feed_assist_active=[0])
        lane = unit.lanes["lane0"]

        unit.lane_unloading(lane)

        assert lane.current_led_state == "unloading"
        assert unit.afc.function.afc_led.calls == [((lane.led_unloading, lane.led_index), {})]
        assert unit._operation_active is True
        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 0})]
        assert unit._feed_assist_active == set()
        assert unit.logger.messages == []

    def test_swallows_prepare_error(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, tool_loaded=True),
                           feed_assist_active=[0])
        lane = unit.lanes["lane0"]
        unit._ace = AceP1BrokenLink("boom")

        unit.lane_unloading(lane)

        assert lane.current_led_state == "unloading"
        assert unit._operation_active is True
        assert unit._feed_assist_active == {0}
        assert unit.logger.messages == [
            ("warning", "ACE: lane_unloading assist-stop error for lane0: boom")]


class TestAfcACEPrepareUnload:
    def test_sets_operation_active_and_stops_assist(self):
        unit = ace_p1_unit(LaneSpec("lane1", slot=1, prep=True, load=True),
                           feed_assist_active=[1])

        unit.prepare_unload(unit.lanes["lane1"], None, None)

        assert unit._operation_active is True
        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 1})]
        assert unit._feed_assist_active == set()
        assert unit.logger.messages == []

    def test_no_assist_running_sends_nothing(self):
        unit = ace_p1_unit(LaneSpec("lane1", slot=1, prep=True, load=True))

        unit.prepare_unload(unit.lanes["lane1"], None, None)

        assert unit._operation_active is True
        assert unit._ace.commands == []
        assert unit.logger.messages == []


class TestAfcACEAceUnloadSequence:
    #: The two tool_stn_unload retracts, timed before the unwind.
    RETRACT_DELTAS = [("debug", "ACE unload: retracting tool_stn_unload/2 (pre-unwind)"),
                      ("debug", "ACE unload: retracting tool_stn_unload (overlaps unwind)")]

    def test_wraps_inner_and_clears_flag(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, tool_loaded=True))
        lane = unit.lanes["lane0"]
        unit._prev_states_stale = False
        seen: List[bool] = []
        AceP1Wire(unit, on_move=lambda method, params: seen.append(unit._operation_active))

        assert unit._ace_unload_sequence(lane, lane.extruder_obj) is True

        assert seen == [True]
        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.afc.afcDeltaTime.messages == self.RETRACT_DELTAS + [
            ("debug", "ACE unwind complete")]
        assert unit.afc.error.handle_lane_failure.calls == []
        assert unit.logger.messages == []

    def test_a_failed_unwind_still_clears_flag(self):
        unit = ace_p1_unit(LaneSpec("lane0", prep=True, load=True, tool_loaded=True),
                           operation_active=True)
        lane = unit.lanes["lane0"]
        unit._prev_states_stale = False
        AceP1Wire(unit).fail("unwind_filament", RuntimeError("jam"))

        assert unit._ace_unload_sequence(lane, lane.extruder_obj) is False

        assert unit._operation_active is False
        assert unit._prev_states_stale is True
        assert unit.afc.afcDeltaTime.messages == self.RETRACT_DELTAS
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, "ACE unwind failed for lane0: jam"), {"pause": False})]
        assert unit.logger.messages == []


class TestAfcACEAceUnloadInner:
    @staticmethod
    def _unit(**kwargs: Any) -> Tuple[afcACE, Any, AceP1Wire]:
        """
        :param kwargs: make_ace_unit keywords
        :return tuple: a unit with lane3 (slot 3) in the toolhead, lane3 and its wire
        """
        unit = ace_p1_unit(LaneSpec("lane3", slot=3, prep=True, load=True, tool_loaded=True),
                           **kwargs)
        return unit, unit.lanes["lane3"], AceP1Wire(unit)

    #: The hub's afc_unload_bowden_length 900 at retract_speed 100.
    UNWIND = ("unwind_filament", {"index": 3, "length": 900.0, "speed": 100.0,
                                  "mode": "normal"})

    def test_not_connected_fails(self):
        unit, lane, wire = self._unit()
        unit._ace.connected = False

        assert unit._ace_unload_inner(lane, lane.extruder_obj) is False

        assert unit._ace.commands == []
        assert unit.afc.move_e_pos.calls == []
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, "ACE not connected (/dev/ttyACM0)"), {"pause": False})]
        assert unit.afc.afcDeltaTime.messages == []
        assert unit.logger.messages == []

    def test_success_stages_at_hub(self):
        unit, lane, wire = self._unit()
        # Cleared so the restaging is visible.
        lane.loaded_to_hub = False

        assert unit._ace_unload_inner(lane, lane.extruder_obj) is True

        assert wire.moves() == [self.UNWIND]
        assert lane.loaded_to_hub is True
        assert unit._hub_load_suppressed == {"lane3"}
        assert lane.current_led_state == "tool_unloaded"
        assert unit.afc.function.log_toolhead_pos.calls == [(("ACE STN unload after ",), {})]
        assert unit.afc.afcDeltaTime.messages == [
            ("debug", "ACE unload: retracting tool_stn_unload/2 (pre-unwind)"),
            ("debug", "ACE unload: retracting tool_stn_unload (overlaps unwind)"),
            ("debug", "ACE unwind complete")]
        assert unit.afc.error.handle_lane_failure.calls == []
        assert unit.logger.messages == []

    def test_unwind_failure_returns_false(self):
        unit, lane, wire = self._unit()
        lane.extruder_obj.tool_stn_unload = 0
        lane.loaded_to_hub = False
        wire.fail("unwind_filament", RuntimeError("wind boom"))

        assert unit._ace_unload_inner(lane, lane.extruder_obj) is False

        assert lane.loaded_to_hub is False
        assert unit._hub_load_suppressed == set()
        assert unit.afc.move_e_pos.calls == []
        assert unit.afc.function.log_toolhead_pos.calls == []
        assert unit.afc.error.handle_lane_failure.calls == [
            ((lane, "ACE unwind failed for lane3: wind boom"), {"pause": False})]
        # No retract to time, and the unwind never completed.
        assert unit.afc.afcDeltaTime.messages == []
        assert unit.logger.messages == []

    def test_two_retracts_first_blocks_second_overlaps_unwind(self):
        unit, lane, wire = self._unit(feed_assist_active=[3])
        lane.extruder_obj.tool_stn_unload = 60.0

        assert unit._ace_unload_inner(lane, lane.extruder_obj) is True

        # Exactly two retracts: half of tool_stn_unload blocking, then all of it async.
        assert unit.afc.move_e_pos.calls == [
            ((-30.0, 25.0, "ACE STN unload"), {"wait_tool": True}),
            ((-60.0, 25.0, "ACE STN unload"), {"wait_tool": False})]
        # The ready check and the assist stop come first, and both retracts are
        # sent before the unwind, so the async one overlaps it. The last three
        # get_status are the unwind's completion polls.
        assert unit.printer.timeline == [
            ("send_command", ("get_status", {})),
            ("send_command", ("get_status", {})),
            ("send_command", ("stop_feed_assist", {"index": 3})),
            ("move_e_pos", (-30.0, 25.0, "ACE STN unload", True)),
            ("move_e_pos", (-60.0, 25.0, "ACE STN unload", False)),
            ("send_command", self.UNWIND),
            ("send_command", ("get_status", {})),
            ("send_command", ("get_status", {})),
            ("send_command", ("get_status", {}))]
        assert unit._feed_assist_active == set()
        assert unit._hub_load_suppressed == {"lane3"}
        assert unit.afc.function.log_toolhead_pos.calls == [(("ACE STN unload after ",), {})]
        assert unit.afc.afcDeltaTime.messages == [
            ("debug", "ACE unload: retracting tool_stn_unload/2 (pre-unwind)"),
            ("debug", "ACE unload: retracting tool_stn_unload (overlaps unwind)"),
            ("debug", "ACE unwind complete")]
        assert unit.afc.error.handle_lane_failure.calls == []
        assert unit.logger.messages == []

    def test_no_retract_move_when_tool_stn_unload_zero_but_still_unwinds(self):
        unit, lane, wire = self._unit()
        lane.extruder_obj.tool_stn_unload = 0

        assert unit._ace_unload_inner(lane, lane.extruder_obj) is True

        assert unit.afc.move_e_pos.calls == []
        assert wire.moves() == [self.UNWIND]
        assert unit._hub_load_suppressed == {"lane3"}
        assert unit.afc.function.log_toolhead_pos.calls == []
        assert unit.afc.afcDeltaTime.messages == [("debug", "ACE unwind complete")]
        assert unit.afc.error.handle_lane_failure.calls == []
        assert unit.logger.messages == []


class TestAfcACECmdAFCACECalibrate:
    def test_usage_on_unknown_lane(self):
        unit = ace_p1_unit("lane0")
        gcmd = make_gcmd(LANE="ghost")

        unit.cmd_AFC_ACE_CALIBRATE(gcmd)

        assert gcmd.responses == ["Usage: AFC_ACE_CALIBRATE LANE=<lane_name>"]
        assert unit._ace.commands == []
        assert unit.logger.messages == []

    def test_usage_without_a_lane(self):
        unit = ace_p1_unit("lane0")
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_CALIBRATE(gcmd)

        assert gcmd.responses == ["Usage: AFC_ACE_CALIBRATE LANE=<lane_name>"]
        assert unit._ace.commands == []
        assert unit.logger.messages == []

    def test_runs_calibration(self):
        unit = ace_p1_unit("lane0")
        lane = unit.lanes["lane0"]
        lane.hub_obj.fullname = "AFC_hub Ace_1_hub"
        # A U1 toolhead sensor that sees the filament on the first 50mm step.
        sensor = types.SimpleNamespace(
            runout_buttun_state=0, runout_helper=types.SimpleNamespace(filament_present=False))
        lane.extruder_obj.fila_tool_start = sensor
        AceP1Wire(unit, on_move=lambda method, params: setattr(sensor, "runout_buttun_state", 1))
        gcmd = make_gcmd(LANE="lane0")

        unit.cmd_AFC_ACE_CALIBRATE(gcmd)

        assert gcmd.responses == ["afc_bowden_length calibration: 50.0mm (was 900.0mm)"]
        assert lane.hub_obj.afc_bowden_length == 50.0
        assert lane.hub_obj.afc_unload_bowden_length == 150.0
        assert unit.afc.function.ConfigRewrite.calls == [
            (("AFC_hub Ace_1_hub", "afc_bowden_length", 50.0,
              "\n afc_bowden_length: New: 50.0 Old: 900.0"), {}),
            (("AFC_hub Ace_1_hub", "afc_unload_bowden_length", 150.0,
              "\n afc_unload_bowden_length: 150.0"), {})]
        # The first 50mm step trips the sensor, then 5mm past it is retracted.
        assert unit.logger.messages == [
            ("raw", "Calibrating afc_bowden_length for lane0 (max 6000mm in 50.0mm steps)"),
            ("info", "ACE calibration: sensor triggered at 50.0mm"),
            ("info", "ACE calibrate: trigger at 50.0mm"),
            ("info", "ACE calibrate: retracting 55mm")]


class TestAfcACECmdAFCACECalibrateHub:
    def test_usage_on_unknown_lane(self):
        unit = ace_p1_unit("lane0")
        gcmd = make_gcmd(LANE="ghost")

        unit.cmd_AFC_ACE_CALIBRATE_HUB(gcmd)

        assert gcmd.responses == ["Usage: AFC_ACE_CALIBRATE_HUB LANE=<lane_name>"]
        assert unit._ace.commands == []
        assert unit.logger.messages == []

    def test_rejects_virtual_hub(self):
        unit = ace_p1_unit("lane0")
        gcmd = make_gcmd(LANE="lane0")

        unit.cmd_AFC_ACE_CALIBRATE_HUB(gcmd)

        assert gcmd.responses == ["Hub calibration requires a physical hub sensor, not virtual"]
        assert unit._ace.commands == []
        assert unit.logger.messages == []

    def test_no_hub_is_left_to_the_calibration(self):
        unit = ace_p1_unit("lane0")
        unit.lanes["lane0"].hub_obj = None
        gcmd = make_gcmd(LANE="lane0")

        unit.cmd_AFC_ACE_CALIBRATE_HUB(gcmd)

        assert gcmd.responses == ["Physical hub sensor required for calibration"]
        assert unit.logger.messages == []

    def test_runs_hub_calibration(self):
        unit = ace_p1_unit("lane0")
        lane = unit.lanes["lane0"]
        hub = unit.printer.add_hub("real_hub", switch_pin="PA1")
        lane.hub_obj = hub
        # The hub switch sees the filament on the first 50mm step.
        AceP1Wire(unit, on_move=lambda method, params: setattr(hub, "state", True))
        gcmd = make_gcmd(LANE="lane0")

        unit.cmd_AFC_ACE_CALIBRATE_HUB(gcmd)

        assert gcmd.responses == ["dist_hub calibration: 50.0mm (was 60mm)"]
        assert lane.dist_hub == 50.0
        assert unit.afc.function.ConfigRewrite.calls == [
            (("AFC_lane lane0", "dist_hub", 50.0, "\n dist_hub: New: 50.0 Old: 60"), {})]
        assert unit.logger.messages == [
            ("raw", "Calibrating dist_hub for lane0 (max 4000mm in 50.0mm steps)"),
            ("info", "ACE hub calibrate: coarse trigger at 50.0mm"),
            ("info", "ACE hub calibrate: trigger at 50.0mm")]


class TestAfcACECmdAFCACEStatus:
    def test_cmd_ace_status_reports_busy_action(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_status", {
            "status": "busy",
            "slots": [{"index": 0, "slot_status": "feeding", "status": "ready"},
                      {"index": 1, "slot_status": "ready", "status": "ready"}]})
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_STATUS(gcmd)

        assert gcmd.responses == [
            "ACE action: feeding(slot 0)\nACE Status: {'status': 'busy', 'slots': "
            "[{'index': 0, 'slot_status': 'feeding', 'status': 'ready'}, "
            "{'index': 1, 'slot_status': 'ready', 'status': 'ready'}]}"]

    def test_cmd_ace_status_reports_idle(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_status", {"status": "ready",
                                           "slots": [{"index": 0, "status": "ready"}]})
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_STATUS(gcmd)

        assert gcmd.responses == [
            "ACE action: idle\nACE Status: {'status': 'ready', 'slots': "
            "[{'index': 0, 'status': 'ready'}]}"]

    def test_cmd_ace_status_not_connected(self):
        unit = ace_p1_unit("lane0")
        unit._ace.connected = False
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_STATUS(gcmd)

        assert gcmd.responses == ["ACE not connected"]
        assert unit._ace.commands == []

    def test_query_error_is_reported(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_status", RuntimeError("timeout"))
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_STATUS(gcmd)

        assert gcmd.responses == ["Error querying ACE: timeout"]


class TestAfcACECmdAFCACEFeedInfo:
    def test_not_connected(self):
        unit = ace_p1_unit("lane0")
        unit._ace.connected = False
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_FEED_INFO(gcmd)

        assert gcmd.responses == ["ACE not connected"]
        assert unit._ace.commands == []

    def test_query_error(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_feed_info", RuntimeError("bad"))
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_FEED_INFO(gcmd)

        assert gcmd.responses == ["Error querying ACE feed info: bad"]

    def test_no_data(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_feed_info", {"raw_fields": {"a": 1}})
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_FEED_INFO(gcmd)

        assert unit._ace.commands == [("get_feed_info", {})]
        assert gcmd.responses == [
            "ACE feed info: no data (unsupported on this firmware, or "
            "nothing fed yet). raw_fields={'a': 1}"]

    def test_reports_ratio(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_feed_info", {
            "feed_info": [{"steps": 10, "length": 100, "decoder": 123}]})
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_FEED_INFO(gcmd)

        # 123 / 100.
        assert gcmd.responses == [
            "ACE feed info (slot: steps / length_mm / encoder_mm / ratio):\n"
            "  slot 0: 10 / 100 / 123 / 1.230"]

    def test_a_slot_with_no_length_reads_ratio_zero(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_feed_info", {"feed_info": [{}, {"length": 50, "decoder": 60}]})
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_FEED_INFO(gcmd)

        assert gcmd.responses == [
            "ACE feed info (slot: steps / length_mm / encoder_mm / ratio):\n"
            "  slot 0: 0 / 0 / 0 / 0.000\n"
            "  slot 1: 0 / 50 / 60 / 1.200"]


class TestAfcACECmdAFCACERFIDDump:
    def test_not_connected(self):
        unit = ace_p1_unit("lane0")
        unit._ace.connected = False
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_RFID_DUMP(gcmd)

        assert gcmd.responses == ["ACE not connected"]
        assert unit._ace.commands == []

    def test_slot_from_lane_and_raw_present(self):
        unit = ace_p1_unit("lane0", LaneSpec("lane2", slot=2))
        unit._ace.set_reply("get_filament_info", {"sku": "S1", "raw": {"1": "x"}})
        gcmd = make_gcmd(LANE="lane2")

        unit.cmd_AFC_ACE_RFID_DUMP(gcmd)

        assert unit._ace.commands == [("get_filament_info", {"index": 2})]
        assert gcmd.responses == [
            "ACE RFID slot 2:\n  parsed: {'sku': 'S1'}\n"
            "  raw protobuf fields (field#: value): {'1': 'x'}"]
        assert unit.logger.messages == []

    def test_default_slot_zero_no_raw(self):
        unit = ace_p1_unit("lane0", LaneSpec("lane2", slot=2))
        unit._ace.set_reply("get_filament_info", {"sku": ""})
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_RFID_DUMP(gcmd)

        assert unit._ace.commands == [("get_filament_info", {"index": 0})]
        assert gcmd.responses == [
            "ACE RFID slot 0:\n  parsed: {'sku': ''}\n"
            "  (no raw field map, V1 ACE Pro or empty read)"]
        assert unit.logger.messages == []

    def test_unknown_lane_reads_slot_zero(self):
        unit = ace_p1_unit("lane0", LaneSpec("lane2", slot=2))
        gcmd = make_gcmd(LANE="ghost")

        unit.cmd_AFC_ACE_RFID_DUMP(gcmd)

        assert unit._ace.commands == [("get_filament_info", {"index": 0})]
        assert gcmd.responses == [
            "ACE RFID slot 0:\n  parsed: {}\n  (no raw field map, V1 ACE Pro or empty read)"]
        # Slot 0 without asking _get_slot, which would warn about the unknown lane.
        assert unit.logger.messages == []

    def test_error(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_filament_info", RuntimeError("z"))
        gcmd = make_gcmd(SLOT=1)

        unit.cmd_AFC_ACE_RFID_DUMP(gcmd)

        assert unit._ace.commands == [("get_filament_info", {"index": 1})]
        assert gcmd.responses == ["Error reading ACE RFID slot 1: z"]


class TestAfcACECmdAFCACERFIDRescan:
    class _Rfid:
        """[AFC_ACE2_rfid] stand-in: the unit it is bound to, and its rescans."""

        def __init__(self, ace2: Any) -> None:
            """
            :param ace2: the bound unit
            """
            self.ace2 = ace2
            self.rescan_lane = Recorder()

    #: A recognized PETG tag (rfid 2).
    PETG = {"rfid": 2, "type": "PETG", "brand": "Anycubic", "color": [255, 0, 0],
           "extruder_temp": {"min": 230, "max": 250},
           "hotbed_temp": {"min": 70, "max": 80}}
    PETG_LOGS = [
        ("debug", "ACE Ace_1: slot 0 get_filament_info -> {'rfid': 2, 'type': 'PETG', "
                  "'brand': 'Anycubic', 'color': [255, 0, 0], 'extruder_temp': "
                  "{'min': 230, 'max': 250}, 'hotbed_temp': {'min': 70, 'max': 80}}"),
        ("info", "ACE Ace_1: slot 0 RFID read, sku='' brand='Anycubic' type='PETG' rfid=2 "
                 "nozzle=230-250C")]

    @staticmethod
    def _unit(*replies: Dict[str, Any]) -> Tuple[afcACE, Any]:
        """
        :param replies: the slot's get_filament_info replies, the last repeating
        :return tuple: a V1 unit and lane1 (slot 0, in the toolhead, showing white PLA)
        """
        unit = ace_p1_unit(LaneSpec("lane1", slot=0, prep=True, load=True, tool_loaded=True),
                           LaneSpec("lane2", slot=1))
        lane = unit.lanes["lane1"]
        lane.material, lane.color = "PLA", "#FFFFFF"
        lane.extruder_temp, lane.bed_temp = 210.0, 60.0
        unit._ace.set_reply("get_filament_info", *replies)
        return unit, lane

    def test_v1_recognized_tag_clears_the_lane_values(self):
        # apply_filament_defaults is disabled (module-level None, see top of
        # AFC_ACE.py), so the recognized tag only clears the lane's old values.
        unit, lane = self._unit(self.PETG)
        gcmd = make_gcmd(LANE="lane1")

        unit.cmd_AFC_ACE_RFID_RESCAN(gcmd)

        assert (lane.material, lane.color) == ("", "")
        assert (lane.extruder_temp, lane.bed_temp) == (None, None)
        assert unit._ace.commands == [("get_filament_info", {"index": 0})]
        assert unit.afc.save_vars.call_count == 1
        assert gcmd.responses == ["ACE Ace_1: read lane1's tag: Anycubic ? "]
        assert unit.logger.messages == self.PETG_LOGS

    def test_v1_recognized_tag_applies_filament_defaults_when_available(self, monkeypatch):
        unit, lane = self._unit(self.PETG)
        apply_defaults = Recorder()
        monkeypatch.setattr(afc_ace_module, "apply_filament_defaults", apply_defaults)
        gcmd = make_gcmd(LANE="lane1")

        unit.cmd_AFC_ACE_RFID_RESCAN(gcmd)

        assert apply_defaults.calls == [
            ((lane, unit._slot_inventory[0]), {"color_converter": None})]

    def test_v1_waits_out_recognizing(self):
        unit, lane = self._unit({"rfid": 3}, {"rfid": 3}, self.PETG)
        gcmd = make_gcmd(LANE="lane1")

        unit.cmd_AFC_ACE_RFID_RESCAN(gcmd)

        assert unit._ace.commands == [("get_filament_info", {"index": 0})] * 3
        # Two half-second waits.
        assert unit.printer.reactor.now == 101.0
        assert lane.material == ""
        assert gcmd.responses == ["ACE Ace_1: read lane1's tag: Anycubic ? "]
        assert unit.logger.messages == [
            ("debug", "ACE Ace_1: slot 0 get_filament_info -> {'rfid': 3}"),
            ("info", "ACE Ace_1: slot 0 RFID read, sku='' brand='' type='' rfid=3 "
                     "nozzle=None-NoneC")] + self.PETG_LOGS

    @pytest.mark.parametrize("reply,state,reads,logs", [
        ({"rfid": 0}, "no tag found", 1,
         [("debug", "ACE Ace_1: slot 0 get_filament_info -> {'rfid': 0}")]),
        ({"rfid": 1}, "tag not recognized", 1,
         [("debug", "ACE Ace_1: slot 0 get_filament_info -> {'rfid': 1}")]),
        # Read every half second until the 5s recognize window closes; only the
        # first read is new, so only it is logged.
        ({"rfid": 3}, "still recognizing", 11,
         [("debug", "ACE Ace_1: slot 0 get_filament_info -> {'rfid': 3}"),
          ("info", "ACE Ace_1: slot 0 RFID read, sku='' brand='' type='' rfid=3 "
                   "nozzle=None-NoneC")]),
        ({}, "no tag read", 1, [("debug", "ACE Ace_1: slot 0 get_filament_info -> {}")])])
    def test_v1_no_read_leaves_the_lane_alone(self, reply: Dict[str, Any], state: str,
                                              reads: int, logs: List[Tuple[str, str]]):
        unit, lane = self._unit(reply)
        gcmd = make_gcmd(LANE="lane1")

        unit.cmd_AFC_ACE_RFID_RESCAN(gcmd)

        assert (lane.material, lane.color) == ("PLA", "#FFFFFF")
        assert (lane.extruder_temp, lane.bed_temp) == (210.0, 60.0)
        assert unit._ace.commands == [("get_filament_info", {"index": 0})] * reads
        assert gcmd.responses == [f"ACE Ace_1: {state} on lane1 (slot 0)"]
        assert unit.afc.save_vars.calls == []
        assert unit.logger.messages == logs

    def test_a_lane_of_another_unit_is_refused(self):
        unit, lane = self._unit({"rfid": 0})
        gcmd = make_gcmd(LANE="lane9")

        with pytest.raises(gcmd.error) as refused:
            unit.cmd_AFC_ACE_RFID_RESCAN(gcmd)

        assert str(refused.value) == "lane9 is not a lane of Ace_1"
        assert unit._ace.commands == []
        assert gcmd.responses == []
        assert unit.logger.messages == []

    def test_v1_not_connected_is_refused(self):
        unit, lane = self._unit({"rfid": 0})
        unit._ace.connected = False
        gcmd = make_gcmd(LANE="lane1")

        with pytest.raises(gcmd.error) as refused:
            unit.cmd_AFC_ACE_RFID_RESCAN(gcmd)

        assert str(refused.value) == "ACE not connected"
        assert lane.material == "PLA"
        assert unit._ace.commands == []
        assert gcmd.responses == []
        assert unit.logger.messages == []

    def test_v1_lane_with_no_ace_slot_is_refused(self):
        unit, lane = self._unit({"rfid": 0})
        # Force an out-of-range slot (a correctly configured unit never does this).
        unit._slot_map["lane1"] = 99
        gcmd = make_gcmd(LANE="lane1")

        with pytest.raises(gcmd.error) as refused:
            unit.cmd_AFC_ACE_RFID_RESCAN(gcmd)

        assert str(refused.value) == "lane1 has no ACE slot"
        assert unit._ace.commands == []
        assert gcmd.responses == []
        assert unit.logger.messages == []

    def test_ace2_hands_the_rescan_to_its_rfid_module(self):
        unit = make_ace2_unit(lanes=[LaneSpec("lane1", slot=0, prep=True, load=True)])
        rfid = self._Rfid(unit)
        unit.printer.objects["AFC_ACE2_rfid"] = rfid
        gcmd = make_gcmd(LANE="lane1")

        unit.cmd_AFC_ACE_RFID_RESCAN(gcmd)

        assert rfid.rescan_lane.calls == [(("lane1", gcmd), {})]
        # No firmware read on an ACE 2.
        assert unit._ace.commands == []
        assert gcmd.responses == []
        assert unit.logger.messages == []

    def test_ace2_without_its_rfid_module_is_refused(self):
        unit = make_ace2_unit(lanes=[LaneSpec("lane1", slot=0, prep=True, load=True)])
        gcmd = make_gcmd(LANE="lane1")

        with pytest.raises(gcmd.error) as refused:
            unit.cmd_AFC_ACE_RFID_RESCAN(gcmd)

        assert str(refused.value) == (
            "AFC_ACE_RFID_RESCAN on Ace2_1 needs [AFC_ACE2_rfid] bound to this unit")
        assert unit._ace.commands == []
        assert gcmd.responses == []
        assert unit.logger.messages == []

    def test_ace2_with_a_module_bound_elsewhere_is_refused(self):
        unit = make_ace2_unit(lanes=[LaneSpec("lane1", slot=0, prep=True, load=True)])
        rfid = self._Rfid(object())
        unit.printer.objects["AFC_ACE2_rfid"] = rfid
        gcmd = make_gcmd(LANE="lane1")

        with pytest.raises(gcmd.error) as refused:
            unit.cmd_AFC_ACE_RFID_RESCAN(gcmd)

        assert str(refused.value) == (
            "AFC_ACE_RFID_RESCAN on Ace2_1 needs [AFC_ACE2_rfid] bound to this unit")
        assert rfid.rescan_lane.calls == []
        assert unit._ace.commands == []
        assert gcmd.responses == []
        assert unit.logger.messages == []


class TestAfcACEParseAceParams:
    def test_parse_params_real_json(self):
        assert afcACE._parse_ace_params('{"index": 0, "type": "PLA"}') == {
            "index": 0, "type": "PLA"}

    def test_parse_params_console_stripped_quotes(self):
        # The gcode console strips JSON's double quotes.
        result = afcACE._parse_ace_params("{index:0,length:50.5,type:PLA,dry:true}")

        assert result == {"index": 0, "length": 50.5, "type": "PLA", "dry": True}

    def test_parse_params_false_bool(self):
        assert afcACE._parse_ace_params("{dry:false}") == {"dry": False}

    def test_parse_params_list_values(self):
        result = afcACE._parse_ace_params("{index:0,color:[255,0,0]}")

        assert result == {"index": 0, "color": [255, 0, 0]}

    def test_parse_params_empty_returns_none(self):
        assert afcACE._parse_ace_params("") is None
        assert afcACE._parse_ace_params(None) is None
        assert afcACE._parse_ace_params("   ") is None

    def test_equals_pairs_and_quoted_values_parse_and_bare_tokens_drop(self):
        result = afcACE._parse_ace_params("{index=1,junk,'name':'x'}")

        assert result == {"index": 1, "name": "x"}

    def test_nothing_parseable_returns_none(self):
        assert afcACE._parse_ace_params("{junk}") is None

    def test_trailing_comma_leaves_no_empty_pair(self):
        assert afcACE._parse_ace_params("{index:0,}") == {"index": 0}

    def test_valid_json_non_dict_falls_through_to_bare_key_parser(self):
        """A JSON list is valid JSON but not a params dict, so send_command's
        params=... contract requires falling through to the bare-key parser
        (which finds no k:v pairs here) instead of returning the list as-is."""
        assert afcACE._parse_ace_params("[1,2,3]") is None


class TestAfcACECmdAFCACECmd:
    def test_not_connected(self):
        unit = ace_p1_unit("lane0")
        unit._ace.connected = False
        gcmd = make_gcmd(METHOD="get_status")

        unit.cmd_AFC_ACE_CMD(gcmd)

        assert gcmd.responses == ["ACE not connected"]
        assert unit._ace.commands == []

    def test_requires_method(self):
        unit = ace_p1_unit("lane0")
        gcmd = make_gcmd(METHOD="")

        unit.cmd_AFC_ACE_CMD(gcmd)

        assert gcmd.responses == ["AFC_ACE_CMD: METHOD=<method> required"]
        assert unit._ace.commands == []

    def test_bad_params_reports_parse_error(self):
        unit = ace_p1_unit("lane0")
        unit._parse_ace_params = Recorder(raises=ValueError("boom"))
        gcmd = make_gcmd(METHOD="get_status", PARAMS="whatever")

        unit.cmd_AFC_ACE_CMD(gcmd)

        assert gcmd.responses == [
            "AFC_ACE_CMD: bad PARAMS (boom), e.g. PARAMS={index:0,type:PLA}"]
        assert unit._ace.commands == []

    def test_success_with_params(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("set_fan_speed", {"code": 0})
        gcmd = make_gcmd(METHOD="set_fan_speed", PARAMS="{fan_speed:7000}")

        unit.cmd_AFC_ACE_CMD(gcmd)

        assert unit._ace.commands == [("set_fan_speed", {"fan_speed": 7000})]
        assert gcmd.responses == ["AFC_ACE_CMD set_fan_speed: OK -> {'code': 0}"]

    def test_command_error(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("bad", RuntimeError("400"))
        gcmd = make_gcmd(METHOD="bad", PARAMS="")

        unit.cmd_AFC_ACE_CMD(gcmd)

        assert unit._ace.commands == [("bad", {})]
        assert gcmd.responses == ["AFC_ACE_CMD bad: 400"]


class TestAfcACECmdAFCACETempInfo:
    def test_temp_info_not_connected(self):
        unit = ace_p1_unit("lane0")
        unit._ace.connected = False
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_TEMP_INFO(gcmd)

        assert gcmd.responses == ["ACE not connected"]
        assert unit._ace.commands == []

    def test_temp_info_get_temp_raises(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_temp", RuntimeError("unsupported"))
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_TEMP_INFO(gcmd)

        assert gcmd.responses == ["AFC_ACE_TEMP_INFO: unsupported"]

    def test_temp_info_non_dict_reply(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_temp", None)
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_TEMP_INFO(gcmd)

        assert gcmd.responses == ["AFC_ACE_TEMP_INFO: unexpected reply None"]

    def test_temp_info_success_formats_all_channels(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_temp", {
            "box1_temp": 30.5, "box2_temp": 31.0, "ptc1_temp": 55.0, "ptc2_temp": 60.0,
            "env_temp": 24.0, "env_humidity": 41.0})
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_TEMP_INFO(gcmd)

        assert unit._ace.commands == [("get_temp", {})]
        assert gcmd.responses == [
            "ACE temperatures:\n"
            "  box1=30.5  box2=31.0\n"
            "  ptc1=55.0  ptc2=60.0\n"
            "  env=24.0  humidity=41.0"]

    def test_temp_info_missing_channels_render_na(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_temp", {"box1_temp": 30.5})
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_TEMP_INFO(gcmd)

        assert gcmd.responses == [
            "ACE temperatures:\n"
            "  box1=30.5  box2=n/a\n"
            "  ptc1=n/a  ptc2=n/a\n"
            "  env=n/a  humidity=n/a"]


class TestAfcACECmdAFCACEMaterialInfo:
    def test_material_info_not_connected(self):
        unit = ace_p1_unit("lane0")
        unit._ace.connected = False
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_MATERIAL_INFO(gcmd)

        assert gcmd.responses == ["ACE not connected"]
        assert unit._ace.commands == []

    def test_material_info_default_slot_zero(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_material_info", {
            "index": 0, "material_name": "S0395MB251230046650C3", "status": 0})
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_MATERIAL_INFO(gcmd)

        assert unit._ace.commands == [("get_material_info", {"index": 0})]
        assert gcmd.responses == [
            "ACE material info slot 0: name='S0395MB251230046650C3' status=0"]

    def test_material_info_explicit_slot(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_material_info", {
            "index": 3, "material_name": "PETG", "status": 1})
        gcmd = make_gcmd(SLOT=3)

        unit.cmd_AFC_ACE_MATERIAL_INFO(gcmd)

        assert unit._ace.commands == [("get_material_info", {"index": 3})]
        assert gcmd.responses == ["ACE material info slot 3: name='PETG' status=1"]

    def test_material_info_error_surfaced(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_material_info", RuntimeError("timeout"))
        gcmd = make_gcmd(SLOT=0)

        unit.cmd_AFC_ACE_MATERIAL_INFO(gcmd)

        assert gcmd.responses == ["AFC_ACE_MATERIAL_INFO: timeout"]

    def test_material_info_non_dict_reply(self):
        unit = ace_p1_unit("lane0")
        unit._ace.set_reply("get_material_info", None)
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_MATERIAL_INFO(gcmd)

        assert gcmd.responses == ["AFC_ACE_MATERIAL_INFO: unexpected reply None"]


class TestAfcACECmdAFCACESetMaterial:
    class _LinkWithoutMaterialWrite:
        """A connected link that has no set_material_name (an older connection)."""
        connected = True

    def test_set_material_not_connected(self):
        unit = ace_p2_unit()
        unit._ace.connected = False
        gcmd = make_gcmd(SLOT=0, NAME="X")

        unit.cmd_AFC_ACE_SET_MATERIAL(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_set_material_without_link(self):
        unit = ace_p2_unit(connection=None)
        gcmd = make_gcmd(SLOT=0, NAME="X")

        unit.cmd_AFC_ACE_SET_MATERIAL(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit.printer.logger.messages == []

    def test_set_material_link_without_write_support(self):
        unit = ace_p2_unit(connection=None)
        unit._ace = self._LinkWithoutMaterialWrite()
        gcmd = make_gcmd(SLOT=0, NAME="X")

        unit.cmd_AFC_ACE_SET_MATERIAL(gcmd)

        assert gcmd.messages == [("info", "AFC_ACE_SET_MATERIAL: requires an ACE 2 Pro unit")]
        assert unit.printer.logger.messages == []

    def test_set_material_requires_slot(self):
        unit = ace_p2_unit()
        gcmd = make_gcmd(NAME="X")

        unit.cmd_AFC_ACE_SET_MATERIAL(gcmd)

        assert gcmd.messages == [("info", "AFC_ACE_SET_MATERIAL: SLOT=<n> required")]
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_set_material_requires_name(self):
        unit = ace_p2_unit()
        gcmd = make_gcmd(SLOT=0)

        unit.cmd_AFC_ACE_SET_MATERIAL(gcmd)

        assert gcmd.messages == [("info", "AFC_ACE_SET_MATERIAL: NAME=<text> required")]
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_set_material_writes_and_reads_back(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_material_info", {"index": 2, "material_name": "PLA_X"})
        gcmd = make_gcmd(SLOT=2, NAME="PLA_X")

        unit.cmd_AFC_ACE_SET_MATERIAL(gcmd)

        assert unit._ace.commands == [("set_material_name", {"index": 2, "name": "PLA_X"}),
                                      ("get_material_info", {"index": 2})]
        assert gcmd.messages == [("info", "AFC_ACE_SET_MATERIAL slot 2: stored name='PLA_X'")]
        assert unit.printer.logger.messages == []

    def test_set_material_non_dict_read_back_shown_as_is(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_material_info", "RAW")
        gcmd = make_gcmd(SLOT=1, NAME="PETG")

        unit.cmd_AFC_ACE_SET_MATERIAL(gcmd)

        assert gcmd.messages == [("info", "AFC_ACE_SET_MATERIAL slot 1: stored name='RAW'")]
        assert unit._ace.commands == [("set_material_name", {"index": 1, "name": "PETG"}),
                                      ("get_material_info", {"index": 1})]
        assert unit.printer.logger.messages == []

    def test_set_material_write_error_surfaced(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("set_material_name", RuntimeError("boom"))
        gcmd = make_gcmd(SLOT=0, NAME="X")

        unit.cmd_AFC_ACE_SET_MATERIAL(gcmd)

        assert gcmd.messages == [("info", "AFC_ACE_SET_MATERIAL: boom")]
        # The read-back is never reached.
        assert unit._ace.commands == [("set_material_name", {"index": 0, "name": "X"})]
        assert unit.printer.logger.messages == []

    def test_set_material_readback_failure_still_reports_write(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_material_info", RuntimeError("readfail"))
        gcmd = make_gcmd(SLOT=0, NAME="X")

        unit.cmd_AFC_ACE_SET_MATERIAL(gcmd)

        assert gcmd.messages == [
            ("info", "AFC_ACE_SET_MATERIAL slot 0: wrote 'X' (read-back failed: readfail)")]
        assert unit._ace.commands == [("set_material_name", {"index": 0, "name": "X"}),
                                      ("get_material_info", {"index": 0})]
        assert unit.printer.logger.messages == []


class TestAfcACECmdAFCACESensorState:
    def test_sensor_state_not_connected(self):
        unit = ace_p2_unit()
        unit._ace.connected = False
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_SENSOR_STATE(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_sensor_state_without_link(self):
        unit = ace_p2_unit(connection=None)
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_SENSOR_STATE(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit.printer.logger.messages == []

    def test_sensor_state_reports_mask_and_triggered(self):
        unit = ace_p2_unit()
        sensors = [False] * 17
        sensors[0] = sensors[4] = True
        unit._ace.set_reply("get_sensor_state", {"sensor_bitmask": 17, "sensors": sensors})
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_SENSOR_STATE(gcmd)

        assert unit._ace.commands == [("get_sensor_state", {})]
        assert gcmd.messages == [
            ("info", "ACE sensor state: mask=0x11 (17) triggered channels=[0, 4]")]
        assert unit.printer.logger.messages == []

    def test_sensor_state_non_dict_reply(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_sensor_state", None)
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_SENSOR_STATE(gcmd)

        assert gcmd.messages == [("info", "AFC_ACE_SENSOR_STATE: unexpected reply None")]
        assert unit._ace.commands == [("get_sensor_state", {})]
        assert unit.printer.logger.messages == []

    def test_sensor_state_error_surfaced(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_sensor_state", RuntimeError("nope"))
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_SENSOR_STATE(gcmd)

        assert gcmd.messages == [("info", "AFC_ACE_SENSOR_STATE: nope")]
        assert unit._ace.commands == [("get_sensor_state", {})]
        assert unit.printer.logger.messages == []


class TestAfcACEPickTestSlot:
    def test_returns_first_recognized_slot(self):
        # rfid 1 is an unrecognized tag, so slot 0 is skipped.
        unit = ace_p2_unit(inventory={0: {"rfid": 1}, 2: {"rfid": 2}, 3: {"rfid": 3}})

        assert unit._pick_test_slot() == 2

    def test_returns_recognizing_slot(self):
        unit = ace_p2_unit(inventory={3: {"rfid": 3}})

        assert unit._pick_test_slot() == 3

    def test_returns_slot_with_sku(self):
        unit = ace_p2_unit(inventory={1: {"sku": "S1"}})

        assert unit._pick_test_slot() == 1

    def test_none_when_empty(self):
        unit = ace_p2_unit(inventory={0: {"rfid": 0, "sku": ""}})

        assert unit._pick_test_slot() is None


class TestAfcACEPollUntilStatus:
    def test_no_connection_returns_false_without_polling(self):
        unit = ace_p2_unit()
        unit._ace = None

        assert unit._poll_until_status(True, timeout=1.0) is False
        assert unit.printer.reactor.now == 100.0
        assert unit.printer.logger.messages == []

    def test_matches_ready(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", {"status": "ready"})

        assert unit._poll_until_status(True, timeout=1.0) is True
        # Matched on the first poll: no pause.
        assert unit._ace.requests == [(0, "get_status", {}, 2.0)]
        assert unit.printer.reactor.now == 100.0
        assert unit.printer.logger.messages == []

    def test_matches_busy_when_waiting_for_not_ready(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", {"status": "ready"}, {"status": "busy"})

        assert unit._poll_until_status(False, timeout=1.0) is True
        assert unit._ace.commands == [("get_status", {}), ("get_status", {})]
        assert unit.printer.reactor.now == pytest.approx(100.2)
        assert unit.printer.logger.messages == []

    def test_times_out_when_never_matches(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", {"status": "busy"})

        assert unit._poll_until_status(True, timeout=0.4) is False
        # Two polls, 0.2s apart, fill the 0.4s budget.
        assert unit._ace.commands == [("get_status", {}), ("get_status", {})]
        assert unit.printer.reactor.now == pytest.approx(100.4)
        assert unit.printer.logger.messages == []

    def test_failed_and_non_dict_polls_are_skipped(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", RuntimeError("down"), "junk", {"status": "ready"})

        assert unit._poll_until_status(True, timeout=1.0) is True
        assert len(unit._ace.commands) == 3
        assert unit.printer.reactor.now == pytest.approx(100.4)
        assert unit.printer.logger.messages == []


class TestAfcACECmdAFCACEFeedTest:
    @staticmethod
    def _pass(busy_polls: int) -> List[Dict[str, str]]:
        """
        get_status replies for one feed-and-return pass of the sweep.

        :param busy_polls: polls the feed reports busy before it is ready
        :return list: replies in the order the sweep polls
        """
        ready, busy = {"status": "ready"}, {"status": "busy"}
        # ready check, feed starts, feed runs, ready check, unwind starts, unwind done
        return [ready, busy] + [busy] * busy_polls + [ready, ready, busy, ready]

    def test_not_connected(self):
        unit = ace_p2_unit()
        unit._ace.connected = False
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_FEED_TEST(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_without_link(self):
        unit = ace_p2_unit(connection=None)
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_FEED_TEST(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit.printer.logger.messages == []

    def test_no_loaded_slot(self):
        unit = ace_p2_unit()
        gcmd = make_gcmd(SLOT=-1, LENGTH=100.0, START=10, END=250, STEP=20)

        unit.cmd_AFC_ACE_FEED_TEST(gcmd)

        assert gcmd.messages == [
            ("info", "AFC_ACE_FEED_TEST: no loaded slot detected, pass SLOT=<n>")]
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_slot_out_of_range(self):
        unit = ace_p2_unit()
        gcmd = make_gcmd(SLOT=4)

        unit.cmd_AFC_ACE_FEED_TEST(gcmd)

        assert gcmd.messages == [
            ("info", "AFC_ACE_FEED_TEST: no loaded slot detected, pass SLOT=<n>")]
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_runs_sweep_and_reports_verdict(self):
        unit = ace_p2_unit()
        # The slow pass runs 5 polls (1.0s), the fast one 2 polls (0.4s).
        unit._ace.set_reply("get_status", *self._pass(5), *self._pass(2))
        gcmd = make_gcmd(SLOT=0, LENGTH=100.0, START=10, END=30, STEP=20)

        unit.cmd_AFC_ACE_FEED_TEST(gcmd)

        moves = [c for c in unit._ace.commands if c[0] != "get_status"]
        assert moves == [
            ("feed_filament", {"index": 0, "length": 100.0, "speed": 10}),
            ("unwind_filament", {"index": 0, "length": 100.0, "speed": 10, "mode": "normal"}),
            ("feed_filament", {"index": 0, "length": 100.0, "speed": 30}),
            ("unwind_filament", {"index": 0, "length": 100.0, "speed": 30, "mode": "normal"}),
        ]
        assert gcmd.messages == [
            ("info", "AFC_ACE_FEED_TEST: slot 0, 100mm, speeds 10..30 step 20 "
                     "(feed + net-zero unwind each pass)"),
            ("info", "  speed= 10:  1.00s  (~ 100 mm/s)"),
            ("info", "  speed= 30:  0.40s  (~ 250 mm/s)"),
            ("info", "AFC_ACE_FEED_TEST done. times scale with speed -> speed param WORKS\n"
                     "  peak measured rate ~250 mm/s\n"
                     "  max effective commanded speed ~30 (higher stops improving the rate)"),
        ]
        assert unit.printer.logger.messages == []

    def test_constant_times_report_clamped(self):
        unit = ace_p2_unit(inventory={1: {"rfid": 2}})
        unit._ace.set_reply("get_status", *self._pass(4), *self._pass(4))
        gcmd = make_gcmd(LENGTH=40.0, START=20, END=40, STEP=20)

        unit.cmd_AFC_ACE_FEED_TEST(gcmd)

        assert gcmd.messages == [
            ("info", "AFC_ACE_FEED_TEST: slot 1, 40mm, speeds 20..40 step 20 "
                     "(feed + net-zero unwind each pass)"),
            ("info", "  speed= 20:  0.80s  (~  50 mm/s)"),
            ("info", "  speed= 40:  0.80s  (~  50 mm/s)"),
            ("info", "AFC_ACE_FEED_TEST done. times ~constant -> speed param appears "
                     "CLAMPED/IGNORED (like the fan)\n"
                     "  peak measured rate ~50 mm/s\n"
                     "  max effective commanded speed ~20 (higher stops improving the rate)"),
        ]
        assert unit.printer.logger.messages == []

    def test_zero_elapsed_passes_fall_back_to_defaults(self):
        # Every poll matches at once, so no time passes and each rate is 0: the
        # peak falls back to 1, the zero slow time skips the clamp check and no
        # speed reaches the peak, so the last one is reported.
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", *self._pass(0), *self._pass(0))
        gcmd = make_gcmd(SLOT=0, LENGTH=100.0, START=10, END=30, STEP=20)

        unit.cmd_AFC_ACE_FEED_TEST(gcmd)

        assert gcmd.messages == [
            ("info", "AFC_ACE_FEED_TEST: slot 0, 100mm, speeds 10..30 step 20 "
                     "(feed + net-zero unwind each pass)"),
            ("info", "  speed= 10:  0.00s  (~   0 mm/s)"),
            ("info", "  speed= 30:  0.00s  (~   0 mm/s)"),
            ("info", "AFC_ACE_FEED_TEST done. times scale with speed -> speed param WORKS\n"
                     "  peak measured rate ~1 mm/s\n"
                     "  max effective commanded speed ~30 (higher stops improving the rate)"),
        ]
        assert unit.printer.reactor.now == 100.0
        assert unit.printer.logger.messages == []

    def test_single_pass_reports_done_only(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", *self._pass(1))
        gcmd = make_gcmd(SLOT=3, LENGTH=50.0, START=25, END=25, STEP=10)

        unit.cmd_AFC_ACE_FEED_TEST(gcmd)

        assert gcmd.messages == [
            ("info", "AFC_ACE_FEED_TEST: slot 3, 50mm, speeds 25..25 step 10 "
                     "(feed + net-zero unwind each pass)"),
            ("info", "  speed= 25:  0.20s  (~ 250 mm/s)"),
            ("info", "AFC_ACE_FEED_TEST done."),
        ]
        moves = [c for c in unit._ace.commands if c[0] != "get_status"]
        assert moves == [
            ("feed_filament", {"index": 3, "length": 50.0, "speed": 25}),
            ("unwind_filament", {"index": 3, "length": 50.0, "speed": 25, "mode": "normal"}),
        ]
        assert unit.printer.logger.messages == []

    def test_failed_move_aborts_the_sweep(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", {"status": "ready"})
        unit._ace.set_reply("feed_filament", RuntimeError("FORBIDDEN"))
        gcmd = make_gcmd(SLOT=0, START=10, END=30, STEP=20)

        unit.cmd_AFC_ACE_FEED_TEST(gcmd)

        assert gcmd.messages == [
            ("info", "AFC_ACE_FEED_TEST: slot 0, 100mm, speeds 10..30 step 20 "
                     "(feed + net-zero unwind each pass)"),
            ("info", "AFC_ACE_FEED_TEST aborted at speed 10: FORBIDDEN"),
        ]
        # A ready check before the feed, and one after the abort.
        assert unit._ace.commands == [("get_status", {}),
                                      ("feed_filament", {"index": 0, "length": 100.0,
                                                         "speed": 10}),
                                      ("get_status", {})]
        assert unit.printer.logger.messages == []


class TestAfcACECmdAFCACEDry:
    def test_temp_capped_to_max(self):
        unit = ace_p2_unit(values={"max_dryer_temperature": 55.0})
        gcmd = make_gcmd(TEMP=80.0, DURATION=90.0, FAN=7000)

        unit.cmd_AFC_ACE_DRY(gcmd)

        assert unit._ace.commands == [
            ("drying", {"temp": 55.0, "fan_speed": 7000, "duration": 90.0})]
        assert gcmd.messages == [
            ("info", "ACE dryer: TEMP 80°C capped to max_dryer_temperature 55°C"),
            ("info", "ACE dryer started: 55.0°C for 90.0 min")]
        assert unit.printer.logger.messages == []

    def test_not_connected(self):
        unit = ace_p2_unit()
        unit._ace.connected = False
        gcmd = make_gcmd(TEMP=40.0)

        unit.cmd_AFC_ACE_DRY(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_without_link(self):
        unit = ace_p2_unit(connection=None)
        gcmd = make_gcmd(TEMP=40.0)

        unit.cmd_AFC_ACE_DRY(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit.printer.logger.messages == []

    def test_success_uncapped(self):
        unit = ace_p2_unit()
        gcmd = make_gcmd(TEMP=40.0, DURATION=30.0, FAN=7000)

        unit.cmd_AFC_ACE_DRY(gcmd)

        assert unit._ace.commands == [
            ("drying", {"temp": 40.0, "fan_speed": 7000, "duration": 30.0})]
        assert gcmd.messages == [("info", "ACE dryer started: 40.0°C for 30.0 min")]
        assert unit.printer.logger.messages == []

    def test_defaults_used_when_params_absent(self):
        unit = ace_p2_unit()
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_DRY(gcmd)

        assert unit._ace.commands == [
            ("drying", {"temp": 50.0, "fan_speed": 7000, "duration": 90.0})]
        assert gcmd.messages == [("info", "ACE dryer started: 50.0°C for 90.0 min")]
        assert unit.printer.logger.messages == []

    def test_error_surfaced(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("drying", RuntimeError("nope"))
        gcmd = make_gcmd(TEMP=40.0)

        unit.cmd_AFC_ACE_DRY(gcmd)

        assert gcmd.messages == [("info", "Error starting dryer: nope")]
        assert unit._ace.commands == [
            ("drying", {"temp": 40.0, "fan_speed": 7000, "duration": 90.0})]
        assert unit.printer.logger.messages == []


class TestAfcACECmdAFCACEDryStop:
    def test_not_connected(self):
        unit = ace_p2_unit()
        unit._ace.connected = False
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_DRY_STOP(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_without_link(self):
        unit = ace_p2_unit(connection=None)
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_DRY_STOP(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit.printer.logger.messages == []

    def test_success(self):
        unit = ace_p2_unit()
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_DRY_STOP(gcmd)

        assert unit._ace.commands == [("drying_stop", {})]
        assert gcmd.messages == [("info", "ACE dryer stopped")]
        assert unit.printer.logger.messages == []

    def test_error(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("drying_stop", RuntimeError("x"))
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_DRY_STOP(gcmd)

        assert gcmd.messages == [("info", "Error stopping dryer: x")]
        assert unit._ace.commands == [("drying_stop", {})]
        assert unit.printer.logger.messages == []


class AceDryRotateWire:
    """
    Scripts a drying unit's link: slots as given, the dryer running, the unit
    busy for the one poll after each accepted unwind.
    """

    def __init__(self, unit: afcACE, *slots: str, dryer: str = "drying") -> None:
        """
        :param unit: unit whose scripted link to drive
        :param slots: per-slot status text
        :param dryer: dryer_status.status
        """
        self.link = unit._ace
        self.slots = list(slots)
        self.dryer = dryer
        self._moving = False
        self.link.set_reply("unwind_filament", self._unwind)
        self.link.set_reply("get_status", self._status)
        unit._cached_hw_status = self.status()

    def status(self, unit_status: str = "ready") -> Dict[str, Any]:
        """
        :param unit_status: the unit's overall status
        :return dict: a get_status result
        """
        return ace_status(*self.slots, status=unit_status,
                          dryer_status={"status": self.dryer})

    def unwinds(self) -> List[Tuple[int, float, float]]:
        """
        :return list: (slot, length, speed) of each unwind sent
        """
        return [(p["index"], p["length"], p["speed"])
                for m, p in self.link.commands if m == "unwind_filament"]

    def _unwind(self, params: Dict[str, Any]) -> Any:
        """
        :param params: the unwind's params
        :return Any: an empty result
        """
        self._moving = True
        return {}

    def _status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        :param params: get_status params, unused
        :return dict: busy once after an unwind, ready otherwise
        """
        moving, self._moving = self._moving, False
        return self.status("busy" if moving else "ready")


class TestAfcACEDryRotate:
    LANES = ("lane1", "lane2", LaneSpec("lane3", prep=True), "lane4")

    def _unit(self, *lanes: Any, slots=("empty", "empty", "ready", "empty"),
              **kwargs: Any) -> Tuple[afcACE, AceDryRotateWire]:
        """
        :param lanes: the unit's lanes, LANES when none
        :param slots: per-slot status the link reports
        :param kwargs: make_ace_unit keywords
        :return tuple: the unit and its scripted wire
        """
        unit = ace_p2_unit(*(lanes or self.LANES), **kwargs)
        return unit, AceDryRotateWire(unit, *slots)

    def _armed(self, *lanes: Any, **kwargs: Any) -> Tuple[afcACE, AceDryRotateWire]:
        """
        :return tuple: a unit drying with ROTATE=1, and its wire
        """
        unit, wire = self._unit(*lanes, **kwargs)
        unit.cmd_AFC_ACE_DRY(make_gcmd(TEMP=50.0, DURATION=240.0, ROTATE=1))
        wire.link.commands.clear()
        return unit, wire

    def _tick(self, unit: afcACE, dt: float = 0.0) -> float:
        """
        :param dt: seconds past now the tick runs at
        :return float: the next waketime it asks for
        """
        return unit._dry_rotate_tick(unit.afc.reactor.monotonic() + dt)

    def test_config_defaults(self):
        unit, _ = self._unit()
        assert (unit.dry_rotate_length, unit.dry_rotate_speed,
                unit.dry_rotate_interval) == (150.0, 15.0, 300.0)

    def test_rotate_off_arms_nothing(self):
        unit, _ = self._unit()
        unit.cmd_AFC_ACE_DRY(make_gcmd(TEMP=50.0))
        assert not unit._dry_rotating
        assert unit.get_status()["ace_dry_rotate"] is False

    def test_rotate_arms_and_names_the_threaded_lane(self):
        unit, _ = self._unit()
        gcmd = make_gcmd(TEMP=50.0, DURATION=240.0, ROTATE=1)
        unit.cmd_AFC_ACE_DRY(gcmd)
        assert unit._dry_rotating
        assert unit.get_status()["ace_dry_rotate"] is True
        assert gcmd.messages[-1] == (
            "info", "AFC_ACE_DRY: rotating free spools on Ace_1 every 300s "
                    "(lane1, lane2, lane4). Not turning lane3: filament is in "
                    "the feed gears, unthread it to dry with rotation")

    def test_a_loaded_lane_refuses_rotation_like_bambu(self):
        unit, wire = self._unit("lane1", LaneSpec("lane2", prep=True, load=True),
                                slots=("empty", "ready", "empty", "empty"))
        gcmd = make_gcmd(TEMP=50.0, ROTATE=1)
        unit.cmd_AFC_ACE_DRY(gcmd)
        assert not unit._dry_rotating
        assert gcmd.messages[-1][1].startswith(
            "AFC_ACE_DRY: ROTATE disabled for Ace_1 -- lane2 still has filament loaded")
        assert [m for m, _ in wire.link.commands] == ["drying"]

    def test_a_failed_dryer_start_does_not_rotate(self):
        unit, wire = self._unit()
        wire.link.set_reply("drying", RuntimeError("nope"))
        unit.cmd_AFC_ACE_DRY(make_gcmd(TEMP=50.0, ROTATE=1))
        assert not unit._dry_rotating

    def test_turns_only_empty_slots_slowly(self):
        unit, wire = self._armed()
        nxt = self._tick(unit)
        assert wire.unwinds() == [(0, 150.0, 15.0), (1, 150.0, 15.0),
                                  (3, 150.0, 15.0)]
        assert nxt == unit.afc.reactor.monotonic() + 300.0
        assert unit._operation_active is False

    def test_a_slot_threaded_since_the_last_tick_is_left_alone(self):
        unit, wire = self._armed()
        wire.slots[0] = "ready"
        self._tick(unit)
        assert [s for s, _, _ in wire.unwinds()] == [1, 3]

    def test_holds_while_printing_then_resumes(self):
        unit, wire = self._armed(printing=True)
        assert self._tick(unit) == unit.afc.reactor.monotonic() + 60.0
        assert wire.unwinds() == []
        assert unit._dry_rotating
        unit.afc.function.printing = False
        self._tick(unit)
        assert len(wire.unwinds()) == 3
        assert unit.printer.logger.messages[-2:] == [
            ("info", "ACE Ace_1: drying rotation holding, printing"),
            ("info", "ACE Ace_1: drying rotation resumed")]

    def test_holds_while_a_lane_is_loaded(self):
        unit, wire = self._armed()
        unit.lanes["lane3"].loaded_to_hub = True
        self._tick(unit)
        assert wire.unwinds() == []

    def test_holds_while_the_ace_is_busy(self):
        unit, wire = self._armed()
        unit._cached_hw_status = wire.status("busy")
        self._tick(unit)
        assert wire.unwinds() == []

    def test_ends_with_the_drying_cycle(self):
        unit, wire = self._armed()
        wire.dryer = "stop"
        unit._cached_hw_status = wire.status()
        assert self._tick(unit, 120.0) == unit.afc.reactor.NEVER
        assert not unit._dry_rotating
        assert wire.unwinds() == []

    def test_ends_when_an_ace_2_reports_stopping(self):
        # An ACE 2's dryer reads 'stopping' once its heater is off.
        unit, wire = self._armed()
        wire.dryer = "stopping"
        unit._cached_hw_status = wire.status()
        assert self._tick(unit, 120.0) == unit.afc.reactor.NEVER
        assert not unit._dry_rotating
        assert wire.unwinds() == []

    def test_an_idle_reading_right_after_start_is_not_the_end(self):
        unit, wire = self._armed()
        wire.dryer = "stop"
        unit._cached_hw_status = wire.status()
        self._tick(unit, 5.0)
        assert unit._dry_rotating

    def test_a_failed_move_is_logged_and_retried(self):
        unit, wire = self._armed()
        wire.link.set_reply("unwind_filament", RuntimeError("FORBIDDEN"))
        assert self._tick(unit) == unit.afc.reactor.monotonic() + 300.0
        assert unit._dry_rotating and unit._operation_active is False
        assert unit.printer.logger.messages[-1][0] == "warning"

    def test_a_load_starting_mid_pass_ends_the_pass(self):
        unit, wire = self._armed()
        def unwind(params):
            unit._operation_active = True     # a load took the unit
            return {}
        wire.link.set_reply("unwind_filament", unwind)
        self._tick(unit)
        assert [s for s, _, _ in wire.unwinds()] == [0]
        assert unit._operation_active is True   # still the load's

    def test_re_arming_mid_pass_ends_the_old_pass(self):
        unit, wire = self._armed()
        def unwind(params):
            unit.cmd_AFC_ACE_DRY(make_gcmd(TEMP=50.0, ROTATE=1))
            return {}
        wire.link.set_reply("unwind_filament", unwind)
        assert self._tick(unit) == unit.afc.reactor.NEVER
        assert [s for s, _, _ in wire.unwinds()] == [0]
        assert unit._dry_rotating

    def test_dry_stop_disarms(self):
        unit, _ = self._armed()
        unit.cmd_AFC_ACE_DRY_STOP(make_gcmd())
        assert not unit._dry_rotating

    def test_stop_mid_turn_stops_the_motor(self):
        unit, wire = self._armed()
        unit._dry_rotate_slot = 1
        unit._stop_dry_rotate("test")
        assert ("stop_unwind_filament", {"index": 1}) in wire.link.commands

    def test_klippy_disconnect_disarms(self):
        unit, _ = self._armed()
        unit._handle_klippy_disconnect()
        assert not unit._dry_rotating

    def test_tick_without_being_armed_ends_immediately(self):
        unit, _ = self._unit()

        assert self._tick(unit) == unit.afc.reactor.NEVER

    def test_arm_with_nothing_free_omits_the_free_list(self):
        unit, _ = self._unit(LaneSpec("lane1", prep=True), LaneSpec("lane2", prep=True),
                             LaneSpec("lane3", prep=True), LaneSpec("lane4", prep=True),
                             slots=("ready", "ready", "ready", "ready"))
        gcmd = make_gcmd(TEMP=50.0, DURATION=240.0, ROTATE=1)

        unit.cmd_AFC_ACE_DRY(gcmd)

        assert unit._dry_rotating
        assert gcmd.messages[-1] == (
            "info", "AFC_ACE_DRY: rotating free spools on Ace_1 every 300s. Not turning "
                    "lane1, lane2, lane3, lane4: filament is in the feed gears, "
                    "unthread it to dry with rotation")

    def test_arm_with_everything_free_omits_the_threaded_sentence(self):
        unit, _ = self._unit("lane1", "lane2", "lane3", "lane4",
                             slots=("empty", "empty", "empty", "empty"))
        gcmd = make_gcmd(TEMP=50.0, DURATION=240.0, ROTATE=1)

        unit.cmd_AFC_ACE_DRY(gcmd)

        assert unit._dry_rotating
        assert gcmd.messages[-1] == (
            "info", "AFC_ACE_DRY: rotating free spools on Ace_1 every 300s "
                    "(lane1, lane2, lane3, lane4)")


class TestAfcACEDryRotateSpin:
    def test_no_connection_returns_before_any_unwind(self):
        unit = ace_p2_unit("lane1", "lane2", "lane4")
        wire = AceDryRotateWire(unit, "empty", "empty", "empty")
        unit.cmd_AFC_ACE_DRY(make_gcmd(TEMP=50.0, DURATION=240.0, ROTATE=1))
        wire.link.commands.clear()
        unit._ace = None

        unit._dry_rotate_spin(unit._dry_rotate_timer)

        assert wire.unwinds() == []
        assert unit.logger.messages == []


class TestAfcACEDryRotateHoldReason:
    def test_not_connected(self):
        unit = ace_p2_unit("lane1")
        unit._ace.connected = False

        assert unit._dry_rotate_hold_reason() == "ACE not connected"

    def test_is_printing_exception_is_swallowed(self):
        unit = ace_p2_unit("lane1")
        unit.afc.function.raise_on_is_printing = RuntimeError("boom")

        assert unit._dry_rotate_hold_reason() == ""

    def test_operation_active_holds(self):
        unit = ace_p2_unit("lane1")
        unit._operation_active = True

        assert unit._dry_rotate_hold_reason() == "another ACE operation is running"


class TestAfcACECmdAFCACEFan:
    def test_not_connected(self):
        unit = ace_p2_unit()
        unit._ace.connected = False
        gcmd = make_gcmd(SPEED=50)

        unit.cmd_AFC_ACE_FAN(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_without_link(self):
        unit = ace_p2_unit(connection=None)
        gcmd = make_gcmd(SPEED=50)

        unit.cmd_AFC_ACE_FAN(gcmd)

        assert gcmd.messages == [("info", "ACE not connected")]
        assert unit.printer.logger.messages == []

    def test_success_sends_both_keys(self):
        unit = ace_p2_unit()
        gcmd = make_gcmd(SPEED=40)

        unit.cmd_AFC_ACE_FAN(gcmd)

        assert unit._ace.commands == [("set_fan_speed", {"speed": 40, "fan_speed": 40})]
        assert gcmd.messages == [("info", "ACE fan set to 40%")]
        assert unit.printer.logger.messages == []

    def test_error(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("set_fan_speed", RuntimeError("f"))
        gcmd = make_gcmd(SPEED=40)

        unit.cmd_AFC_ACE_FAN(gcmd)

        assert gcmd.messages == [("info", "Error setting fan: f")]
        assert unit._ace.commands == [("set_fan_speed", {"speed": 40, "fan_speed": 40})]
        assert unit.printer.logger.messages == []


class TestAfcACECmdAFCACELaneReset:
    def test_usage_on_unknown_lane(self):
        unit = ace_p2_unit("lane1", operation_active=True)
        gcmd = make_gcmd(LANE="ghost")

        unit.cmd_AFC_ACE_LANE_RESET(gcmd)

        assert gcmd.messages == [("info", "Usage: AFC_ACE_LANE_RESET LANE=<lane_name>")]
        # eject_lane never ran, so its operation flag is untouched.
        assert unit._operation_active is True
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_usage_without_lane(self):
        unit = ace_p2_unit("lane1", operation_active=True)
        gcmd = make_gcmd()

        unit.cmd_AFC_ACE_LANE_RESET(gcmd)

        assert gcmd.messages == [("info", "Usage: AFC_ACE_LANE_RESET LANE=<lane_name>")]
        assert unit._operation_active is True
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_ejects_known_lane(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, load=True, values={"dist_hub": 100}),
                           operation_active=True)
        lane = unit.lanes["lane1"]
        ready, busy = {"status": "ready"}, {"status": "busy"}
        # Ready check, the unwind starts, then two idle polls end it.
        unit._ace.set_reply("get_status", ready, busy, ready, ready)
        gcmd = make_gcmd(LANE="lane1")

        unit.cmd_AFC_ACE_LANE_RESET(gcmd)

        # dist_hub plus the default 475mm eject_buffer.
        assert [c for c in unit._ace.commands if c[0] != "get_status"] == [
            ("unwind_filament", {"index": 0, "length": 575.0, "speed": 100.0,
                                 "mode": "normal"})]
        assert lane.loaded_to_hub is False
        assert unit._hub_load_suppressed == {"lane1"}
        assert unit._operation_active is False
        assert gcmd.messages == [("info", "Lane lane1 reset")]
        assert unit.printer.logger.messages == [
            ("info", "ACE eject lane1: unwinding 575mm (dist_hub=100mm)")]


class TestAfcACECmdAFCACEFeedAssist:
    _STOPPED = ("Feed assist stopped on slot 2, watchdog suppressed until assist is "
                "started again")

    @staticmethod
    def _unit(**kwargs: Any) -> afcACE:
        """
        :param kwargs: make_ace_unit keywords
        :return afcACE: a unit with lane1..lane3 on slots 0..2
        """
        return ace_p2_unit("lane1", "lane2", "lane3", **kwargs)

    def test_feed_assist_cmd_requires_enable(self):
        unit = self._unit()
        gcmd = make_gcmd(LANE="lane1")

        with pytest.raises(AceGcmd.error, match=r"^ENABLE is required \(0 or 1\)$"):
            unit.cmd_AFC_ACE_FEED_ASSIST(gcmd)

        assert unit._assist_suppressed == set()
        assert unit._ace.commands == []
        assert gcmd.messages == []
        assert unit.printer.logger.messages == []

    def test_feed_assist_cmd_requires_lane_or_slot(self):
        unit = self._unit()
        gcmd = make_gcmd(ENABLE=0)

        with pytest.raises(AceGcmd.error, match=r"^LANE or SLOT is required$"):
            unit.cmd_AFC_ACE_FEED_ASSIST(gcmd)

        assert unit._assist_suppressed == set()
        assert unit._ace.commands == []
        assert gcmd.messages == []
        assert unit.printer.logger.messages == []

    def test_feed_assist_cmd_unknown_lane(self):
        unit = self._unit()
        gcmd = make_gcmd(ENABLE=0, LANE="nope")

        with pytest.raises(AceGcmd.error, match=r"^Unknown lane for this unit: nope$"):
            unit.cmd_AFC_ACE_FEED_ASSIST(gcmd)

        assert unit._assist_suppressed == set()
        assert unit._ace.commands == []
        assert gcmd.messages == []
        assert unit.printer.logger.messages == []

    def test_feed_assist_stop_suppresses_and_stops_tracked_slot(self):
        unit = self._unit(feed_assist_active=[2])
        gcmd = make_gcmd(ENABLE=0, LANE="lane3")

        unit.cmd_AFC_ACE_FEED_ASSIST(gcmd)

        assert unit._assist_suppressed == {2}
        assert unit._feed_assist_active == set()
        # The tracked stop waits for the unit, then stops and waits for the ack.
        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 2})]
        assert unit._ace.async_commands == []
        assert gcmd.messages == [("info", self._STOPPED)]
        assert unit.printer.logger.messages == []

    def test_feed_assist_stop_sends_firmware_stop_on_tracking_drift(self):
        # Not tracked as assisting, but the firmware might be: the stop still goes out.
        unit = self._unit()
        gcmd = make_gcmd(ENABLE=0, LANE="lane3")

        unit.cmd_AFC_ACE_FEED_ASSIST(gcmd)

        assert unit._assist_suppressed == {2}
        assert unit._ace.commands == [("stop_feed_assist", {"index": 2})]
        assert unit._ace.async_commands == [("stop_feed_assist", {"index": 2})]
        assert gcmd.messages == [("info", self._STOPPED)]
        assert unit.printer.logger.messages == []

    def test_feed_assist_stop_firmware_stop_exception_is_swallowed(self):
        unit = self._unit()
        unit._ace.stop_feed_assist = Recorder(raises=RuntimeError("boom"))
        gcmd = make_gcmd(ENABLE=0, LANE="lane3")

        unit.cmd_AFC_ACE_FEED_ASSIST(gcmd)

        assert unit._assist_suppressed == {2}
        assert unit._ace.stop_feed_assist.calls == [((2,), {})]
        assert gcmd.messages == [("info", self._STOPPED)]
        assert unit.printer.logger.messages == []

    def test_feed_assist_stop_untracked_without_hardware_only_suppresses(self):
        unit = self._unit(connection=None)
        gcmd = make_gcmd(ENABLE=0, LANE="lane3")

        unit.cmd_AFC_ACE_FEED_ASSIST(gcmd)

        assert unit._assist_suppressed == {2}
        assert unit._feed_assist_active == set()
        assert gcmd.messages == [("info", self._STOPPED)]
        assert unit.printer.logger.messages == []

    def test_feed_assist_stop_untracked_on_down_link_only_suppresses(self):
        unit = self._unit()
        unit._ace.connected = False
        gcmd = make_gcmd(ENABLE=0, SLOT=2)

        unit.cmd_AFC_ACE_FEED_ASSIST(gcmd)

        assert unit._assist_suppressed == {2}
        assert unit._ace.commands == []
        assert gcmd.messages == [("info", self._STOPPED)]
        assert unit.printer.logger.messages == []

    def test_feed_assist_start_stops_other_slots_first(self):
        # The unit assists one slot at a time: the other slot is stopped first.
        unit = self._unit(feed_assist_active=[1])
        gcmd = make_gcmd(ENABLE=1, LANE="lane1")

        unit.cmd_AFC_ACE_FEED_ASSIST(gcmd)

        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 1}),
                                      ("get_status", {}), ("start_feed_assist", {"index": 0})]
        assert unit._feed_assist_active == {0}
        assert gcmd.messages == [("info", "Feed assist started on slot 0")]
        assert unit.printer.logger.messages == []

    def test_feed_assist_start_accepts_slot_param(self):
        # A user ENABLE=1 is an explicit start, so it clears a manual suppression.
        unit = self._unit()
        unit._assist_suppressed.add(3)
        gcmd = make_gcmd(ENABLE=1, SLOT=3)

        unit.cmd_AFC_ACE_FEED_ASSIST(gcmd)

        assert unit._assist_suppressed == set()
        assert unit._feed_assist_active == {3}
        assert unit._ace.commands == [("get_status", {}), ("start_feed_assist", {"index": 3})]
        assert gcmd.messages == [("info", "Feed assist started on slot 3")]
        assert unit.printer.logger.messages == []

    def test_feed_assist_start_keeps_the_slot_already_assisting(self):
        # Only other slots are stopped: the requested slot keeps assisting and
        # is not started again.
        unit = self._unit(feed_assist_active=[1])
        gcmd = make_gcmd(ENABLE=1, LANE="lane2")

        unit.cmd_AFC_ACE_FEED_ASSIST(gcmd)

        assert unit._feed_assist_active == {1}
        assert unit._ace.commands == []
        assert gcmd.messages == [("info", "Feed assist started on slot 1")]
        assert unit.printer.logger.messages == []


class TestAfcACEStartFeedAssist:
    _START_FAILED = "ACE command 'start_feed_assist' failed: "

    def test_explicit_start_clears_suppression(self):
        # With no link the method returns right after dropping the suppression.
        unit = ace_p2_unit(connection=None)
        unit._assist_suppressed.add(2)

        unit._start_feed_assist(2, explicit=True)

        assert unit._assist_suppressed == set()
        assert unit._feed_assist_active == set()
        assert unit.printer.logger.messages == []

    def test_explicit_start_on_live_link_starts_the_slot(self):
        unit = ace_p2_unit()
        unit._assist_suppressed.add(2)

        unit._start_feed_assist(2, explicit=True)

        assert unit._assist_suppressed == set()
        assert unit._feed_assist_active == {2}
        assert unit._ace.commands == [("get_status", {}), ("start_feed_assist", {"index": 2})]
        assert unit.printer.logger.messages == []

    def test_non_explicit_start_respects_suppression(self):
        # A watchdog or load start must not undo a manual ENABLE=0.
        unit = ace_p2_unit()
        unit._assist_suppressed.add(2)

        unit._start_feed_assist(2)

        assert unit._assist_suppressed == {2}
        assert unit._ace.commands == []
        assert unit._feed_assist_active == set()
        assert unit.printer.logger.messages == []

    def test_down_link_starts_nothing(self):
        unit = ace_p2_unit(feed_assist_active=[1])
        unit._ace.connected = False

        unit._start_feed_assist(0)

        assert unit._feed_assist_active == {1}
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_start_already_active_is_noop(self):
        unit = ace_p2_unit(feed_assist_active=[2])

        unit._start_feed_assist(2)

        assert unit._ace.commands == []
        assert unit._feed_assist_active == {2}
        assert unit.printer.logger.messages == []

    def test_start_feed_assist_stops_other_active_slot_first(self):
        unit = ace_p2_unit(feed_assist_active=[2])

        unit._start_feed_assist(0)

        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 2}),
                                      ("get_status", {}), ("start_feed_assist", {"index": 0})]
        assert unit._feed_assist_active == {0}
        assert unit.printer.logger.messages == []

    def test_start_feed_assist_clears_stale_second_assist_when_already_active(self):
        unit = ace_p2_unit(feed_assist_active=[0, 2])

        unit._start_feed_assist(0)

        # Slot 2 is stopped; slot 0 already assists, so no start is sent.
        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 2})]
        assert unit._feed_assist_active == {0}
        assert unit.printer.logger.messages == []

    def test_start_feed_assist_error_2_logged_debug_not_error(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("start_feed_assist", ace_error(2, "error_2"))

        unit._start_feed_assist(2)

        assert unit._feed_assist_active == set()
        assert unit.printer.logger.messages == [
            ("debug", "Feed assist slot 2 refused (error_2, concurrent-assist limit or "
                      "slot state); leaving off")]
        assert unit._ace.commands == [("get_status", {}), ("start_feed_assist", {"index": 2})]

    def test_error_2_text_alone_is_a_refusal(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("start_feed_assist", RuntimeError("error_2"))

        unit._start_feed_assist(1)

        assert unit._feed_assist_active == set()
        assert unit.printer.logger.messages == [
            ("debug", "Feed assist slot 1 refused (error_2, concurrent-assist limit or "
                      "slot state); leaving off")]

    def test_code_2_alone_is_a_refusal(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("start_feed_assist", ace_error(2, "busy"))

        unit._start_feed_assist(1)

        assert unit._feed_assist_active == set()
        assert unit.printer.logger.messages == [
            ("debug", "Feed assist slot 1 refused (error_2, concurrent-assist limit or "
                      "slot state); leaving off")]

    def test_error_2_on_idle_cached_slot_is_a_refusal(self):
        # The cached status shows the slot idle, so it is not taken as assisting.
        unit = ace_p2_unit(hw_status={"slots": [{"slot_status": "ready"}]})
        unit._ace.set_reply("start_feed_assist", ace_error(2, "error_2"))

        unit._start_feed_assist(0)

        assert unit._feed_assist_active == set()
        assert unit.printer.logger.messages == [
            ("debug", "Feed assist slot 0 refused (error_2, concurrent-assist limit or "
                      "slot state); leaving off")]

    def test_garbled_cached_slot_is_not_assisting(self):
        # A cached slot entry that is not a dict cannot say it assists.
        unit = ace_p2_unit(hw_status={"slots": [{}, {}, "garbled"]})
        unit._ace.set_reply("start_feed_assist", ace_error(2, "error_2"))

        unit._start_feed_assist(2)

        assert unit._feed_assist_active == set()
        assert unit.printer.logger.messages == [
            ("debug", "Feed assist slot 2 refused (error_2, concurrent-assist limit or "
                      "slot state); leaving off")]

    def test_start_feed_assist_unexpected_error_stays_error(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("start_feed_assist", RuntimeError("something genuinely unexpected"))

        unit._start_feed_assist(2)

        assert unit._feed_assist_active == set()
        assert unit.printer.logger.messages == [
            ("error", "Failed to start feed assist slot 2: something genuinely unexpected")]

    def test_timeout_retries_then_errors(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("start_feed_assist", TIMEOUT)

        unit._start_feed_assist(0)

        # Each attempt polls get_status (even ids) before the start (odd ids).
        assert unit._ace.commands == [("get_status", {}), ("start_feed_assist", {"index": 0})] * 3
        assert unit._feed_assist_active == set()
        assert unit.printer.logger.messages == [
            ("debug", "start feed assist slot 0 timed out (attempt 1/3), retrying: "
                      "ACE command 'start_feed_assist' (id=1) timed out after 2.0s"),
            ("debug", "start feed assist slot 0 timed out (attempt 2/3), retrying: "
                      "ACE command 'start_feed_assist' (id=3) timed out after 2.0s"),
            ("error", "Failed to start feed assist slot 0 after 3 attempts: "
                      "ACE command 'start_feed_assist' (id=5) timed out after 2.0s"),
        ]

    def test_timeout_then_success_tracks_slot(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("start_feed_assist", TIMEOUT, {})

        unit._start_feed_assist(0)

        assert unit._feed_assist_active == {0}
        assert unit.printer.logger.messages == [
            ("debug", "start feed assist slot 0 timed out (attempt 1/3), retrying: "
                      "ACE command 'start_feed_assist' (id=1) timed out after 2.0s")]

    def test_error_2_already_assisting_marks_tracked(self):
        unit = ace_p2_unit(hw_status={"slots": [{"slot_status": "assisting"}]})
        unit._ace.set_reply("start_feed_assist", ace_error(2, "error_2"))

        unit._start_feed_assist(0)

        assert unit._feed_assist_active == {0}
        assert unit.printer.logger.messages == [
            ("debug", "Feed assist slot 0 already assisting (error_2); marked tracked")]

    def test_forbidden_logged_debug(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("start_feed_assist", ace_error(0, "FORBIDDEN"))

        unit._start_feed_assist(0)

        assert unit._feed_assist_active == set()
        assert unit.printer.logger.messages == [
            ("debug", "Feed assist slot 0 not permitted yet (FORBIDDEN); watchdog will "
                      f"retry: {self._START_FAILED}code=0, msg=FORBIDDEN")]


class TestAfcACEStopFeedAssist:
    def test_not_tracked_noop(self):
        unit = ace_p2_unit()

        unit._stop_feed_assist(0)

        assert unit._ace.commands == []
        assert unit._feed_assist_active == set()
        assert unit.printer.logger.messages == []

    def test_disconnected_noop(self):
        unit = ace_p2_unit(feed_assist_active=[0])
        unit._ace.connected = False

        unit._stop_feed_assist(0)

        assert unit._ace.commands == []
        assert unit._feed_assist_active == {0}
        assert unit.printer.logger.messages == []

    def test_no_link_noop(self):
        unit = ace_p2_unit(connection=None, feed_assist_active=[0])

        unit._stop_feed_assist(0)

        assert unit._feed_assist_active == {0}
        assert unit.printer.logger.messages == []

    def test_success_discards(self):
        unit = ace_p2_unit(feed_assist_active=[0, 1])

        unit._stop_feed_assist(0)

        assert unit._feed_assist_active == {1}
        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 0})]
        assert unit._ace.async_commands == []
        assert unit.printer.logger.messages == []

    def test_timeout_retries_then_errors(self):
        unit = ace_p2_unit(feed_assist_active=[0])
        unit._ace.set_reply("stop_feed_assist", TIMEOUT)

        unit._stop_feed_assist(0)

        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 0})] * 3
        assert unit._feed_assist_active == {0}
        assert unit.printer.logger.messages == [
            ("debug", "stop feed assist slot 0 timed out (attempt 1/3), retrying: "
                      "ACE command 'stop_feed_assist' (id=1) timed out after 2.0s"),
            ("debug", "stop feed assist slot 0 timed out (attempt 2/3), retrying: "
                      "ACE command 'stop_feed_assist' (id=3) timed out after 2.0s"),
            ("error", "Failed to stop feed assist slot 0 after 3 attempts: "
                      "ACE command 'stop_feed_assist' (id=5) timed out after 2.0s"),
        ]

    def test_generic_error_logged_once(self):
        unit = ace_p2_unit(feed_assist_active=[0])
        unit._ace.set_reply("stop_feed_assist", RuntimeError("boom"))

        unit._stop_feed_assist(0)

        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 0})]
        assert unit._feed_assist_active == {0}
        assert unit.printer.logger.messages == [
            ("error", "Failed to stop feed assist slot 0: boom")]


class TestAfcACEActiveAssistLane:
    def test_active_assist_lane_matches_section_name(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True))

        assert unit.lanes["lane1"].extruder_obj.lane_loaded == "lane1"
        assert unit._active_assist_lane() == "lane1"
        assert unit.printer.logger.messages == []

    def test_active_assist_lane_matches_physical_name(self):
        # [AFC_extruder e0] drives the Klipper "extruder"; the toolhead reports that name.
        printer = make_ace_printer()
        printer.add_extruder("e0", th_extruder_name="extruder")
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True),
                           printer=printer, values={"extruder": "e0"})

        assert unit.lanes["lane1"].extruder_obj.name == "e0"
        assert unit._active_assist_lane() == "lane1"
        assert unit.printer.logger.messages == []

    def test_active_assist_lane_toolhead_lookup_failure_returns_none(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True),
                           active_extruder=None)

        assert unit._active_assist_lane() is None
        assert unit.printer.logger.messages == []

    def test_active_assist_lane_skips_lane_without_extruder(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True))
        unit.lanes["lane1"].extruder_obj = None

        assert unit._active_assist_lane() is None
        assert unit.printer.logger.messages == []

    def test_active_assist_lane_skips_unloaded_lanes(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, load=True))

        assert unit._active_assist_lane() is None
        assert unit.printer.logger.messages == []

    def test_active_assist_lane_ignores_other_extruders(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True),
                           active_extruder="extruder4")

        assert unit._active_assist_lane() is None
        assert unit.printer.logger.messages == []

    def test_active_assist_lane_fallback_when_lane_loaded_lags(self):
        # lane_loaded lags tool_loaded at print start; the loaded lane still wins.
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True))
        unit.lanes["lane1"].extruder_obj.lane_loaded = None

        assert unit._active_assist_lane() == "lane1"
        assert unit.printer.logger.messages == []

    def test_active_assist_lane_exact_match_beats_candidate(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True),
                           LaneSpec("lane2", prep=True, tool_loaded=True),
                           LaneSpec("lane3", prep=True, tool_loaded=True))
        unit.lanes["lane1"].extruder_obj.lane_loaded = "lane2"

        assert unit._active_assist_lane() == "lane2"
        assert unit.printer.logger.messages == []

    def test_active_assist_lane_keeps_first_candidate_when_none_matches_exactly(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True),
                           LaneSpec("lane2", prep=True, tool_loaded=True))
        # Neither lane is the shared extruder's exact lane_loaded; the first
        # candidate found still wins once the loop runs out of lanes.
        unit.lanes["lane1"].extruder_obj.lane_loaded = "someone_else"

        assert unit._active_assist_lane() == "lane1"
        assert unit.printer.logger.messages == []


class TestAfcACEMaybeAssistWatchdog:
    _ENABLING = ("info", "ACE assist watchdog: enabling feed assist for lane1 (slot 0)")

    @staticmethod
    def _unit(**kwargs: Any) -> afcACE:
        """
        :param kwargs: make_ace_unit keywords
        :return afcACE: a unit whose lane1 is loaded in the active tool, filament
            at the toolhead sensor
        """
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True), "lane2", **kwargs)
        unit.lanes["lane1"].extruder_obj.tool_start_state = True
        return unit

    def test_watchdog_schedules_reconcile_when_assist_missing(self):
        unit = self._unit()

        unit._maybe_assist_watchdog()

        assert unit.printer.logger.messages == [self._ENABLING]
        # Deferred: nothing is sent until the reactor runs the reconcile.
        assert unit._ace.commands == []
        assert len(unit.printer.reactor.pending) == 1
        unit.printer.reactor.run_callbacks()
        assert unit._feed_assist_active == {0}
        assert unit._ace.commands == [("get_status", {}), ("start_feed_assist", {"index": 0})]

    def test_watchdog_fires_when_wrong_slot_assisting(self):
        unit = self._unit(feed_assist_active=[3])

        unit._maybe_assist_watchdog()

        assert unit.printer.logger.messages == [self._ENABLING]
        assert len(unit.printer.reactor.pending) == 1
        # The reconcile moves assist from slot 3 to the active lane's slot 0.
        unit.printer.reactor.run_callbacks()
        assert unit._feed_assist_active == {0}
        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 3}),
                                      ("get_status", {}), ("start_feed_assist", {"index": 0})]

    def test_watchdog_noop_when_assist_already_correct(self):
        unit = self._unit(feed_assist_active=[0])

        unit._maybe_assist_watchdog()

        assert unit.printer.logger.messages == []
        assert unit.printer.reactor.pending == []

    def test_watchdog_respects_manual_suppression(self):
        unit = self._unit()
        unit._assist_suppressed.add(0)

        unit._maybe_assist_watchdog()

        assert unit.printer.logger.messages == []
        assert unit.printer.reactor.pending == []

    def test_watchdog_disabled_by_config(self):
        unit = self._unit(values={"assist_watchdog": False})

        unit._maybe_assist_watchdog()

        assert unit._assist_watchdog is False
        assert unit.printer.logger.messages == []
        assert unit.printer.reactor.pending == []

    def test_watchdog_noop_without_active_lane(self):
        unit = ace_p2_unit("lane1")

        unit._maybe_assist_watchdog()

        assert unit.printer.logger.messages == []
        assert unit.printer.reactor.pending == []

    def test_watchdog_noop_when_assist_disabled_for_lane(self):
        unit = self._unit(values={"use_feed_assist": False})

        unit._maybe_assist_watchdog()

        assert unit.printer.logger.messages == []
        assert unit.printer.reactor.pending == []

    def test_watchdog_noop_when_lane_unmapped(self):
        unit = self._unit()
        del unit._slot_map["lane1"]

        unit._maybe_assist_watchdog()

        assert unit.printer.logger.messages == []
        assert unit.printer.reactor.pending == []

    def test_watchdog_noop_when_lane_unknown_to_afc(self):
        unit = self._unit()
        del unit.afc.lanes["lane1"]

        unit._maybe_assist_watchdog()

        assert unit.printer.logger.messages == []
        assert unit.printer.reactor.pending == []


class TestAfcACECheckStuck:
    @staticmethod
    def _unit(**kwargs: Any) -> afcACE:
        """
        :param kwargs: make_ace_unit keywords over the defaults
        :return afcACE: a unit printing with detection on and slot 0 assisting
        """
        options: Dict[str, Any] = {"values": {"stuck_spool_detection": True},
                                   "feed_assist_active": [0], "in_print": True}
        options.update(kwargs)
        return ace_p2_unit(**options)

    def test_disabled_returns_early(self):
        unit = self._unit(values={})
        assert unit._stuck_detection is False
        assert unit._stuck_tripped is False

        unit._check_stuck({"cont_assist_time": 99})

        # Printing with assist on and past stuck_time: only the guard stops the trip.
        assert unit._stuck_tripped is False
        assert unit.printer.reactor.pending == []
        assert unit.printer.afc.error.AFC_error.calls == []
        assert unit.printer.logger.messages == []

    def test_not_printing_clears_latch(self):
        unit = self._unit(in_print=False)
        unit._stuck_tripped = True

        unit._check_stuck({"cont_assist_time": 99})

        assert unit._stuck_tripped is False
        assert unit.printer.reactor.pending == []
        assert unit.printer.afc.error.AFC_error.calls == []
        assert unit.printer.logger.messages == []

    def test_paused_clears_latch(self):
        unit = self._unit(paused=True)
        unit._stuck_tripped = True

        unit._check_stuck({"cont_assist_time": 99})

        assert unit._stuck_tripped is False
        assert unit.printer.reactor.pending == []
        assert unit.printer.afc.error.AFC_error.calls == []
        assert unit.printer.logger.messages == []

    def test_no_assist_running_clears_latch(self):
        unit = self._unit(feed_assist_active=[])
        unit._stuck_tripped = True

        unit._check_stuck({"cont_assist_time": 99})

        assert unit._stuck_tripped is False
        assert unit.printer.reactor.pending == []
        assert unit.printer.afc.error.AFC_error.calls == []
        assert unit.printer.logger.messages == []

    def test_no_cont_field_returns(self):
        unit = self._unit()
        unit._stuck_tripped = True

        unit._check_stuck({})

        # Returned before the threshold check, which would have cleared the latch.
        assert unit._stuck_tripped is True
        assert unit.printer.reactor.pending == []
        assert unit.printer.afc.error.AFC_error.calls == []
        assert unit.printer.logger.messages == []

    def test_non_numeric_cont_returns(self):
        unit = self._unit()
        unit._stuck_tripped = True

        unit._check_stuck({"cont_assist_time": "bad"})

        assert unit._stuck_tripped is True
        assert unit.printer.reactor.pending == []
        assert unit.printer.afc.error.AFC_error.calls == []
        assert unit.printer.logger.messages == []

    def test_unconvertible_cont_returns(self):
        # A value float() rejects with TypeError rather than ValueError.
        unit = self._unit()
        unit._stuck_tripped = True

        unit._check_stuck({"cont_assist_time": [5.0]})

        assert unit._stuck_tripped is True
        assert unit.printer.reactor.pending == []
        assert unit.printer.afc.error.AFC_error.calls == []
        assert unit.printer.logger.messages == []

    def test_below_threshold_clears_latch(self):
        unit = self._unit()
        unit._stuck_tripped = True

        unit._check_stuck({"cont_assist_time": 1.0})

        assert unit._stuck_tripped is False
        assert unit.printer.reactor.pending == []
        assert unit.printer.afc.error.AFC_error.calls == []
        assert unit.printer.logger.messages == []

    def test_trips_once_and_defers(self):
        unit = self._unit()

        unit._check_stuck({"cont_assist_time": "5.0"})

        assert unit._stuck_tripped is True
        # The pause is deferred onto the reactor, not raised from the heartbeat.
        assert unit.printer.afc.error.AFC_error.calls == []
        assert len(unit.printer.reactor.pending) == 1
        unit.printer.reactor.run_callbacks()
        assert unit.printer.afc.error.AFC_error.calls == [(
            ("ACE stuck spool detected on Ace_1: feed assist ran continuously for 5.0s "
             "(>= 4.0s threshold). The spool is likely tangled or jammed at the unit. "
             "Clear the snag, then resume. Run AFC_ACE_STUCK_SPOOL_DETECTION ENABLE=0 to "
             "disable this check.",), {"pause": True})]
        assert unit.printer.logger.messages == []

    def test_already_tripped_does_not_re_defer(self):
        unit = self._unit()
        unit._stuck_tripped = True

        unit._check_stuck({"cont_assist_time": 5.0})

        assert unit._stuck_tripped is True
        assert unit.printer.reactor.pending == []
        assert unit.printer.afc.error.AFC_error.calls == []
        assert unit.printer.logger.messages == []


class TestAfcACEHandleStuck:
    def test_stops_assist_and_pauses_via_afc(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True),
                           feed_assist_active=[0])

        unit._handle_stuck(7.5)

        assert unit._feed_assist_active == set()
        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 0})]
        assert unit.printer.afc.error.AFC_error.calls == [(
            ("ACE stuck spool detected on Ace_1 lane lane1: feed assist ran continuously "
             "for 7.5s (>= 4.0s threshold). The spool is likely tangled or jammed at the "
             "unit. Clear the snag, then resume. Run AFC_ACE_STUCK_SPOOL_DETECTION ENABLE=0 "
             "to disable this check.",), {"pause": True})]
        assert unit.printer.gcode.run_script_from_command.calls == []
        assert unit.printer.logger.messages == []

    def test_fallback_to_gcode_pause_when_afc_raises(self):
        unit = ace_p2_unit(values={"stuck_time": 6.0}, active_extruder=None)
        unit.printer.afc.error.AFC_error.raises = RuntimeError("nope")

        unit._handle_stuck(9.0)

        message = ("ACE stuck spool detected on Ace_1: feed assist ran continuously for "
                   "9.0s (>= 6.0s threshold). The spool is likely tangled or jammed at "
                   "the unit. Clear the snag, then resume. Run AFC_ACE_STUCK_SPOOL_DETECTION "
                   "ENABLE=0 to disable this check.")
        assert unit._ace.commands == []
        # The AFC pause was tried first; when it raised, the console pause took over.
        assert unit.printer.afc.error.AFC_error.calls == [((message,), {"pause": True})]
        assert unit.printer.gcode.run_script_from_command.calls == [(("PAUSE",), {})]
        assert unit.printer.logger.messages == [("error", message)]

    def test_unmapped_active_lane_is_named_but_not_stopped(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True),
                           feed_assist_active=[0])
        del unit._slot_map["lane1"]

        unit._handle_stuck(5.0)

        assert unit._feed_assist_active == {0}
        assert unit._ace.commands == []
        assert unit.printer.afc.error.AFC_error.calls == [(
            ("ACE stuck spool detected on Ace_1 lane lane1: feed assist ran continuously "
             "for 5.0s (>= 4.0s threshold). The spool is likely tangled or jammed at the "
             "unit. Clear the snag, then resume. Run AFC_ACE_STUCK_SPOOL_DETECTION ENABLE=0 "
             "to disable this check.",), {"pause": True})]
        assert unit.printer.gcode.run_script_from_command.calls == []
        assert unit.printer.logger.messages == []

    def test_stop_feed_assist_exception_is_swallowed(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True),
                           feed_assist_active=[0])
        unit._stop_feed_assist = Recorder(raises=RuntimeError("boom"))

        unit._handle_stuck(7.5)

        assert unit._stop_feed_assist.calls == [((0,), {})]
        assert unit.printer.afc.error.AFC_error.calls == [(
            ("ACE stuck spool detected on Ace_1 lane lane1: feed assist ran continuously "
             "for 7.5s (>= 4.0s threshold). The spool is likely tangled or jammed at the "
             "unit. Clear the snag, then resume. Run AFC_ACE_STUCK_SPOOL_DETECTION ENABLE=0 "
             "to disable this check.",), {"pause": True})]


class TestAfcACECmdAFCACEStuckSpoolDetection:
    def test_enable_on(self):
        unit = ace_p2_unit()
        unit._stuck_tripped = True

        unit.cmd_AFC_ACE_STUCK_SPOOL_DETECTION(make_gcmd(ENABLE=1))

        assert unit._stuck_detection is True
        # Enabling leaves the latch as it was.
        assert unit._stuck_tripped is True
        assert unit._stuck_time == 4.0
        assert unit.printer.logger.messages == [
            ("info", "ACE stuck spool detection ON: stuck_time=4.0s")]

    def test_disable_clears_latch(self):
        unit = ace_p2_unit(values={"stuck_spool_detection": True})
        unit._stuck_tripped = True

        unit.cmd_AFC_ACE_STUCK_SPOOL_DETECTION(make_gcmd(ENABLE=0))

        assert unit._stuck_detection is False
        assert unit._stuck_tripped is False
        assert unit.printer.logger.messages == [
            ("info", "ACE stuck spool detection OFF: stuck_time=4.0s")]

    def test_sets_stuck_time_only(self):
        unit = ace_p2_unit(values={"stuck_spool_detection": True})
        unit._stuck_tripped = True

        unit.cmd_AFC_ACE_STUCK_SPOOL_DETECTION(make_gcmd(STUCK_TIME=6.0))

        assert unit._stuck_time == 6.0
        assert unit._stuck_detection is True
        assert unit._stuck_tripped is True
        assert unit.printer.logger.messages == [
            ("info", "ACE stuck spool detection ON: stuck_time=6.0s")]


class TestAfcACEHandleExtruderActivated:
    def test_no_active_lane_noop(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True),
                           active_extruder=None)

        unit._handle_extruder_activated()

        assert unit.printer.reactor.pending == []
        assert unit.printer.logger.messages == []

    def test_schedules_reconcile_for_active_lane(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True))
        unit.lanes["lane1"].extruder_obj.tool_start_state = True

        unit._handle_extruder_activated()

        assert len(unit.printer.reactor.pending) == 1
        assert unit._ace.commands == []
        unit.printer.reactor.run_callbacks()
        assert unit._feed_assist_active == {0}
        assert unit.printer.logger.messages == []

    def test_no_schedule_when_assist_disabled(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True),
                           values={"use_feed_assist": False})

        unit._handle_extruder_activated()

        assert unit.printer.reactor.pending == []
        assert unit.printer.logger.messages == []

    def test_no_schedule_when_lane_unknown_to_afc(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, tool_loaded=True))
        del unit.afc.lanes["lane1"]

        unit._handle_extruder_activated()

        assert unit.printer.reactor.pending == []
        assert unit.printer.logger.messages == []


class TestAfcACEWaitForAceReady:
    def test_disconnected_false(self):
        unit = ace_p2_unit()
        unit._ace.connected = False

        assert unit._wait_for_ace_ready() is False
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_no_link_false(self):
        unit = ace_p2_unit(connection=None)

        assert unit._wait_for_ace_ready() is False
        assert unit.printer.logger.messages == []

    def test_ready_immediately_true(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", {"status": "ready"})

        assert unit._wait_for_ace_ready() is True
        assert unit._ace.requests == [(0, "get_status", {}, 2.0)]
        assert unit.printer.reactor.now == 100.0
        assert unit.printer.logger.messages == []

    def test_timeout_warns_and_returns_false(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", {"status": "busy"})

        assert unit._wait_for_ace_ready(timeout=1.0) is False
        assert unit._ace.commands == [("get_status", {}), ("get_status", {})]
        assert unit.printer.reactor.now == 101.0
        assert unit.printer.logger.messages == [
            ("debug", "ACE: waiting for ready (status=busy, 0.0s/1s)"),
            ("debug", "ACE: waiting for ready (status=busy, 0.5s/1s)"),
            ("warning", "ACE: did not become ready within 1s, proceeding anyway")]

    def test_failed_and_non_dict_polls_keep_waiting(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", RuntimeError("down"), "junk", {"busy": 1},
                            {"status": "ready"})

        assert unit._wait_for_ace_ready() is True
        assert unit.printer.reactor.now == 101.5
        assert unit.printer.logger.messages == [
            ("debug", "ACE: waiting for ready (status=?, 1.0s/30s)")]


class TestAfcACESlotReportsEmpty:
    EMPTY = {"status": "ready", "slots": [{"status": "empty"}]}
    PRESENT = {"status": "ready", "slots": [{"status": "ready"}]}

    def test_slot_reports_empty_true_when_stably_empty(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self.EMPTY)

        assert unit._slot_reports_empty(0) is True
        # Three polls, 0.15s apart.
        assert len(unit._ace.commands) == 3
        assert unit.printer.reactor.now == pytest.approx(100.3)
        assert unit.printer.logger.messages == []

    def test_slot_reports_empty_false_on_single_flicker(self):
        # Empty once, then present: a one-poll flicker is not a removal.
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self.EMPTY, self.PRESENT)

        assert unit._slot_reports_empty(0) is False
        assert len(unit._ace.commands) == 2
        assert unit.printer.logger.messages == []

    def test_slot_reports_empty_false_when_slot_ready(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self.PRESENT)

        assert unit._slot_reports_empty(0) is False
        assert len(unit._ace.commands) == 1
        assert unit.printer.logger.messages == []

    def test_slot_reports_empty_false_when_unit_busy(self):
        # A busy unit's slots can flicker empty mid-motion.
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", {"status": "busy", "slots": [{"status": "empty"}]})

        assert unit._slot_reports_empty(0) is False
        assert len(unit._ace.commands) == 1
        assert unit.printer.logger.messages == []

    def test_slot_reports_empty_false_on_non_dict_status(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", "junk")

        assert unit._slot_reports_empty(0) is False
        assert len(unit._ace.commands) == 1
        assert unit.printer.logger.messages == []

    def test_slot_reports_empty_false_on_non_dict_slot(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", {"status": "ready", "slots": ["empty"]})

        assert unit._slot_reports_empty(0) is False
        assert len(unit._ace.commands) == 1
        assert unit.printer.logger.messages == []

    def test_slot_reports_empty_false_when_query_fails(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", RuntimeError("status timeout"))

        assert unit._slot_reports_empty(0) is False
        assert len(unit._ace.commands) == 1
        assert unit.printer.logger.messages == []

    def test_slot_reports_empty_false_when_disconnected(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self.EMPTY)
        unit._ace.connected = False

        assert unit._slot_reports_empty(0) is False
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_slot_reports_empty_false_when_no_ace(self):
        unit = ace_p2_unit(connection=None)

        assert unit._slot_reports_empty(0) is False
        assert unit.printer.logger.messages == []

    def test_slot_reports_empty_false_when_slot_index_out_of_range(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self.EMPTY)

        assert unit._slot_reports_empty(5) is False
        assert len(unit._ace.commands) == 1
        assert unit.printer.logger.messages == []

    def test_slot_reports_empty_false_for_negative_slot_index(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self.EMPTY)

        assert unit._slot_reports_empty(-1) is False
        assert len(unit._ace.commands) == 1
        assert unit.printer.logger.messages == []

    def test_slot_reports_empty_polls_at_least_once(self):
        # confirm=0 still takes one poll, with no pause before it.
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self.EMPTY)

        assert unit._slot_reports_empty(0, confirm=0) is True
        assert len(unit._ace.commands) == 1
        assert unit.printer.reactor.now == 100.0
        assert unit.printer.logger.messages == []


class TestAfcACESlotIsMoving:
    def test_non_dict_false(self):
        assert ace_p2_unit()._slot_is_moving("x", 0) is False

    def test_unit_busy_true(self):
        hw = {"status": "busy", "slots": [{"status": "ready", "slot_status": "ready"}]}

        assert ace_p2_unit()._slot_is_moving(hw, 0) is True

    def test_slot_status_field_moving(self):
        hw = {"status": "ready", "slots": [{"status": "feeding"}]}

        assert ace_p2_unit()._slot_is_moving(hw, 0) is True

    def test_slot_status_key_moving(self):
        hw = {"status": "ready", "slots": [{"status": "ready", "slot_status": "rollback"}]}

        assert ace_p2_unit()._slot_is_moving(hw, 0) is True

    def test_idle_slot_false(self):
        hw = {"status": "ready", "slots": [{"status": "ready", "slot_status": "ready"}]}

        assert ace_p2_unit()._slot_is_moving(hw, 0) is False

    def test_slot_out_of_range_false(self):
        hw = {"status": "ready", "slots": [{"status": "feeding"}]}

        assert ace_p2_unit()._slot_is_moving(hw, 1) is False

    def test_negative_slot_false(self):
        hw = {"status": "ready", "slots": [{"status": "feeding"}]}

        assert ace_p2_unit()._slot_is_moving(hw, -1) is False

    def test_non_dict_slot_entry_false(self):
        hw = {"status": "ready", "slots": ["feeding"]}

        assert ace_p2_unit()._slot_is_moving(hw, 0) is False


class TestAfcACELastFeedLength:
    def test_no_connection_returns_none(self):
        unit = ace_p2_unit()
        unit._ace = None

        assert unit._last_feed_length(3) is None

    def test_returns_length_for_slot(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_feed_info", {"feed_info": [
            {"length": 10.0}, {"length": 20.0}, {"length": 30.0}, {"length": 40.0}]})

        assert unit._last_feed_length(3) == 40.0

    def test_slot_out_of_range_returns_none(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_feed_info", {"feed_info": [{"length": 10.0}]})

        assert unit._last_feed_length(3) is None

    def test_send_command_exception_returns_none(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_feed_info", RuntimeError("down"))

        assert unit._last_feed_length(3) is None


class TestAfcACEFeedAttempt:
    def test_no_connection_returns_before_any_feed(self):
        unit = ace_p2_unit()
        unit._ace = None

        assert unit._ace_feed_attempt(3, 100.0, 25.0, None) == (False, False)

    def test_feed_without_error_returns_not_errored_and_success(self):
        unit = ace_p2_unit()
        AceP2Wire(unit)

        errored, success = unit._ace_feed_attempt(3, 100.0, 25.0, None)

        assert (errored, success) == (False, True)


class TestAfcACEStopFeedAtSensor:
    FEEDING = {"status": "busy", "slots": [{}, {}, {}, {"status": "feeding"}]}
    IDLE = {"status": "ready", "slots": [{}, {}, {}, {"status": "ready"}]}

    def test_no_connection_returns_before_any_command(self):
        unit = ace_p2_unit()
        unit._ace = None

        unit._stop_feed_at_sensor(3)

        assert unit.printer.logger.messages == []

    def test_stop_at_the_sensor_is_resent_until_confirmed(self):
        # A stop inside the unit's ~250 ms setup window is dropped, so it is resent.
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self.FEEDING, self.IDLE)

        unit._stop_feed_at_sensor(3)

        assert unit._ace.async_commands == [("stop_feed_filament", {"index": 3})] * 2
        assert unit.printer.reactor.now == pytest.approx(100.6)
        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: feed on slot 3 stopped at the toolhead sensor")]

    def test_warns_when_the_stop_never_confirms(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self.FEEDING)

        unit._stop_feed_at_sensor(3)

        assert unit._ace.async_commands == [("stop_feed_filament", {"index": 3})] * 3
        assert unit.printer.logger.messages == [
            ("warning", "ACE Ace_1: the feed on slot 3 did not confirm stopped at the "
                        "toolhead sensor")]

    def test_failed_status_poll_is_logged_and_retried(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", RuntimeError("down"), self.IDLE)

        unit._stop_feed_at_sensor(3)

        assert unit._ace.async_commands == [("stop_feed_filament", {"index": 3})] * 2
        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: stop at the toolhead sensor on slot 3: down"),
            ("debug", "ACE Ace_1: feed on slot 3 stopped at the toolhead sensor")]


class TestAfcACESlotInError:
    def test_non_dict_false(self):
        assert ace_p2_unit()._slot_in_error(None, 0) is False

    def test_status_error_true(self):
        hw = {"slots": [{"status": "feed_error"}]}

        assert ace_p2_unit()._slot_in_error(hw, 0) is True

    def test_slot_status_error_true(self):
        hw = {"slots": [{"status": "ready", "slot_status": "assist_error"}]}

        assert ace_p2_unit()._slot_in_error(hw, 0) is True

    def test_no_error_false(self):
        hw = {"slots": [{"status": "ready", "slot_status": "ready"}]}

        assert ace_p2_unit()._slot_in_error(hw, 0) is False

    def test_slot_out_of_range_false(self):
        hw = {"slots": [{"status": "feed_error"}]}

        assert ace_p2_unit()._slot_in_error(hw, 2) is False

    def test_negative_slot_false(self):
        hw = {"slots": [{"status": "feed_error"}]}

        assert ace_p2_unit()._slot_in_error(hw, -1) is False

    def test_non_dict_slot_entry_false(self):
        hw = {"slots": ["feed_error"]}

        assert ace_p2_unit()._slot_in_error(hw, 0) is False

    def test_non_text_status_false(self):
        hw = {"slots": [{"status": 7, "slot_status": None}]}

        assert ace_p2_unit()._slot_in_error(hw, 0) is False


class TestAfcACEWaitForFeedComplete:
    IDLE = {"status": "ready", "slots": [{"status": "ready"}]}
    BUSY = {"status": "busy"}

    @staticmethod
    def _frame(slot_status: str = "ready", busy: bool = False) -> Dict[str, Any]:
        """
        :param slot_status: slot 3's slot_status
        :param busy: the unit's overall status is busy
        :return dict: a four-slot status frame
        """
        slots = [{"index": i, "status": "ready", "slot_status": "ready"} for i in range(4)]
        slots[3]["slot_status"] = slot_status
        return {"status": "busy" if busy else "ready", "slots": slots}

    def test_disconnected_false(self):
        unit = ace_p2_unit()
        unit._last_move_error = "feed_error"
        unit._ace.connected = False

        assert unit._wait_for_feed_complete(0, 100.0, 100.0) is False
        # The previous move's error is dropped before the link check.
        assert unit._last_move_error is None
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_no_link_false(self):
        unit = ace_p2_unit(connection=None)
        unit._last_move_error = "feed_error"

        assert unit._wait_for_feed_complete(0, 100.0, 100.0) is False
        assert unit._last_move_error is None
        assert unit.printer.logger.messages == []

    def test_lane_sensor_early_return(self):
        unit = ace_p2_unit("lane1")
        lane = unit.lanes["lane1"]
        lane.extruder_obj.tool_start_state = True

        assert unit._wait_for_feed_complete(0, 100.0, 100.0, lane) is True
        assert unit._ace.async_commands == [("stop_feed_filament", {"index": 0})]
        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: feed on slot 0 stopped at the toolhead sensor")]

    def test_short_move_completed_before_motion(self):
        unit = ace_p2_unit(values={"feed_departure_timeout": 0.5})
        unit._ace.set_reply("get_status", self.IDLE)

        assert unit._wait_for_feed_complete(0, 1.0, 100.0) is True
        assert unit.printer.logger.messages == [
            ("debug", "ACE wait: slot 0 short move (1mm) completed before motion was "
                      "observed, treating as done")]

    def test_short_move_in_error_is_a_no_start(self):
        unit = ace_p2_unit(values={"feed_departure_timeout": 0.5})
        unit._ace.set_reply("get_status", {"status": "ready", "slots": [
            {"status": "ready", "slot_status": "feed_error"}]})

        assert unit._wait_for_feed_complete(0, 1.0, 100.0) is False
        assert unit.printer.logger.messages == [
            ("debug", "ACE wait: slot 0 never reported motion after feed/unwind command, "
                      "motor may not have started")]

    def test_short_move_moving_at_the_last_poll_is_a_no_start(self):
        unit = ace_p2_unit(values={"feed_departure_timeout": 0.5})
        # Three idle departure polls, then motion only on the final check.
        unit._ace.set_reply("get_status", self.IDLE, self.IDLE, self.IDLE, self.BUSY)

        assert unit._wait_for_feed_complete(0, 1.0, 100.0) is False
        assert len(unit._ace.commands) == 4
        assert unit.printer.logger.messages == [
            ("debug", "ACE wait: slot 0 never reported motion after feed/unwind command, "
                      "motor may not have started")]

    def test_genuine_no_start_returns_false(self):
        unit = ace_p2_unit(values={"feed_departure_timeout": 0.5})
        unit._ace.set_reply("get_status", self.IDLE)

        assert unit._wait_for_feed_complete(0, 500.0, 100.0) is False
        # A long move gets no final short-move check: only the departure polls.
        assert len(unit._ace.commands) == 3
        assert unit.printer.logger.messages == [
            ("debug", "ACE wait: slot 0 never reported motion after feed/unwind command, "
                      "motor may not have started")]

    def test_moves_then_completes(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self.BUSY, self.IDLE, self.IDLE)

        assert unit._wait_for_feed_complete(0, 100.0, 100.0) is True
        # One departure poll, then two idle reads 0.5s apart.
        assert len(unit._ace.commands) == 3
        assert unit.printer.reactor.now == pytest.approx(101.2)
        assert unit._last_move_error is None
        assert unit.printer.logger.messages == []

    def test_moving_until_the_deadline_times_out(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self.BUSY)

        # 100mm at 100mm/s: 1s expected, so 1.5 * 1 + 15 = 16.5s allowed.
        assert unit._wait_for_feed_complete(0, 100.0, 100.0) is False
        assert unit.printer.logger.messages == [
            ("debug", "ACE wait: timeout waiting for slot 0 movement (16.5s)")]

    def test_wait_records_the_feed_error_frame(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self._frame("feeding", busy=True),
                            self._frame("feed_error"), self._frame(), self._frame())

        assert unit._wait_for_feed_complete(3, 3900.0, 140.0) is True
        assert unit._last_move_error == "feed_error"
        assert unit.printer.logger.messages == [
            ("warning", "ACE Ace_1: slot 3 reported feed_error")]

    def test_wait_ignores_a_heartbeat_error_older_than_the_move(self):
        unit = ace_p2_unit(hw_status=self._frame("feed_error"))
        unit._hw_status_time = 50.0
        unit._ace.set_reply("get_status", self._frame("feeding", busy=True), self._frame())

        assert unit._wait_for_feed_complete(3, 100.0, 140.0) is True
        assert unit._last_move_error is None
        assert unit.printer.logger.messages == []

    def test_wait_uses_a_heartbeat_error_newer_than_the_move(self):
        unit = ace_p2_unit(hw_status=self._frame("feed_error"))
        unit._hw_status_time = 1000.0
        unit._ace.set_reply("get_status", self._frame("feeding", busy=True), self._frame())

        assert unit._wait_for_feed_complete(3, 100.0, 140.0) is True
        assert unit._last_move_error == "feed_error"
        # Logged once, though later polls see the cached error again.
        assert unit.printer.logger.messages == [
            ("warning", "ACE Ace_1: slot 3 reported feed_error")]

    def test_wait_stops_the_feed_at_the_toolhead_sensor(self):
        unit = ace_p2_unit("lane1", "lane2", "lane3", "lane4")
        lane = unit.lanes["lane4"]
        reactor = unit.printer.reactor

        def sensor(now: float) -> None:
            """The filament reaches the toolhead sensor a little after departure."""
            lane.extruder_obj.tool_start_state = now >= 100.25
        reactor.on_pause = sensor
        unit._ace.set_reply("get_status", self._frame("feeding", busy=True), self._frame())

        assert unit._wait_for_feed_complete(3, 150.0, 25.0, lane) is True
        assert unit._ace.async_commands == [("stop_feed_filament", {"index": 3})]
        # Caught by the 20ms sensor polls, well before the next status poll.
        assert len(unit._ace.commands) == 3
        assert unit.printer.logger.messages == [
            ("debug", "ACE wait: toolhead sensor triggered for slot 3"),
            ("debug", "ACE Ace_1: feed on slot 3 stopped at the toolhead sensor")]

    def test_short_move_check_exception_falls_back_to_no_start(self):
        # Every get_status call raises: both the departure-wait polls and the
        # short-move completion check fall through their except: pass.
        unit = ace_p2_unit(values={"feed_departure_timeout": 0.5})
        unit._ace.set_reply("get_status", RuntimeError("boom"))

        assert unit._wait_for_feed_complete(0, 1.0, 100.0) is False
        assert unit.printer.logger.messages == [
            ("debug", "ACE wait: slot 0 never reported motion after feed/unwind command, "
                      "motor may not have started")]

    def test_phase1_get_status_exception_is_retried(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_status", self.BUSY, RuntimeError("boom"),
                            self.IDLE, self.IDLE)

        assert unit._wait_for_feed_complete(0, 100.0, 100.0) is True
        assert len(unit._ace.commands) == 4
        assert unit.printer.logger.messages == []


class TestAfcACESmartLoadRetry:
    def test_no_connection_returns_false_without_feeding(self):
        unit = ace_p2_unit("lane1")
        lane = unit.lanes["lane1"]
        wire = AceP2Wire(unit)
        unit._ace = None

        assert unit._smart_load_retry(lane, 0, 100.0) is False
        assert wire.moves() == []
        assert unit.printer.logger.messages == []

    def test_succeeds_when_sensor_triggers(self):
        unit = ace_p2_unit("lane1")
        lane = unit.lanes["lane1"]

        def reach_sensor(method: str, params: Dict[str, Any]) -> None:
            """The feed carries the filament onto the toolhead sensor."""
            lane.extruder_obj.tool_start_state = True
        wire = AceP2Wire(unit, on_move=reach_sensor)

        assert unit._smart_load_retry(lane, 0, 100.0) is True
        assert wire.moves() == [("feed_filament", {"index": 0, "length": 100.0,
                                                   "speed": 100.0})]
        assert unit.printer.logger.messages == [
            ("info", "Feed retry 1/3 for lane1 (100mm)"),
            ("debug", "ACE Ace_1: feed on slot 0 stopped at the toolhead sensor")]

    def test_exhausts_retries(self):
        unit = ace_p2_unit("lane1")
        wire = AceP2Wire(unit)

        assert unit._smart_load_retry(unit.lanes["lane1"], 0, 100.0, max_retries=2) is False
        assert wire.moves() == [("feed_filament", {"index": 0, "length": 100.0,
                                                   "speed": 100.0})] * 2
        assert unit.printer.logger.messages == [
            ("info", "Feed retry 1/2 for lane1 (100mm)"),
            ("info", "Feed retry 2/2 for lane1 (100mm)")]

    def test_refused_feed_is_retried(self):
        unit = ace_p2_unit("lane1")
        lane = unit.lanes["lane1"]
        refusals = [RuntimeError("FORBIDDEN")]

        def move(method: str, params: Dict[str, Any]) -> None:
            """Refuse the first feed; the next one reaches the sensor."""
            if refusals:
                raise refusals.pop()
            lane.extruder_obj.tool_start_state = True
        wire = AceP2Wire(unit, on_move=move)

        assert unit._smart_load_retry(lane, 0, 80.0) is True
        assert len(wire.moves()) == 2
        assert unit.printer.logger.messages == [
            ("info", "Feed retry 1/3 for lane1 (80mm)"),
            ("info", "Feed retry 2/3 for lane1 (80mm)"),
            ("debug", "ACE Ace_1: feed on slot 0 stopped at the toolhead sensor")]


class TestAfcACECalibrateHubInner:
    @staticmethod
    def _unit(**kwargs: Any) -> afcACE:
        """
        :param kwargs: make_ace_unit keywords
        :return afcACE: a unit whose lane1 (dist_hub 10) sits on a hub with a
            real switch pin
        """
        printer = make_ace_printer()
        printer.add_hub("hub1", switch_pin="PA1")
        return ace_p2_unit(LaneSpec("lane1", values={"hub": "hub1", "dist_hub": 10}),
                           printer=printer, **kwargs)

    def test_not_connected(self):
        unit = self._unit()
        unit._ace.connected = False

        assert unit._calibrate_hub_inner(unit.lanes["lane1"]) == (
            False, "ACE not connected", 0)
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_without_link(self):
        unit = self._unit(connection=None)

        assert unit._calibrate_hub_inner(unit.lanes["lane1"]) == (
            False, "ACE not connected", 0)
        assert unit.printer.logger.messages == []

    def test_no_hub_rejected(self):
        unit = self._unit()
        lane = unit.lanes["lane1"]
        lane.hub_obj = None

        assert unit._calibrate_hub_inner(lane) == (
            False, "Physical hub sensor required for calibration", 0)
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_virtual_hub_rejected(self):
        unit = ace_p2_unit("lane1")

        assert unit._calibrate_hub_inner(unit.lanes["lane1"]) == (
            False, "Physical hub sensor required for calibration", 0)
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_hub_already_triggered(self):
        unit = self._unit()
        lane = unit.lanes["lane1"]
        lane.hub_obj.state = True

        assert unit._calibrate_hub_inner(lane) == (
            False, "Hub sensor already triggered, clear the hub first", 0)
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_success_first_step(self):
        unit = self._unit()
        lane = unit.lanes["lane1"]

        def trip_hub(method: str, params: Dict[str, Any]) -> None:
            """The first feed reaches the hub switch."""
            lane.hub_obj.state = True
        wire = AceP2Wire(unit, on_move=trip_hub)

        result = unit._calibrate_hub_inner(lane)

        assert result == (True, "dist_hub calibration: 50.0mm (was 10.0mm)", 50.0)
        assert lane.dist_hub == 50.0
        # A 50mm trigger leaves nothing to retract.
        assert wire.moves() == [("feed_filament", {"index": 0, "length": 50.0,
                                                   "speed": 100.0})]
        assert unit.printer.afc.function.ConfigRewrite.calls == [
            (("AFC_lane lane1", "dist_hub", 50.0, "\n dist_hub: New: 50.0 Old: 10.0"), {})]
        assert unit.printer.logger.messages == [
            ("raw", "Calibrating dist_hub for lane1 (max 4000mm in 50.0mm steps)"),
            ("info", "ACE hub calibrate: coarse trigger at 50.0mm"),
            ("info", "ACE hub calibrate: trigger at 50.0mm")]

    def test_trigger_after_two_steps_retracts_past_the_hub(self):
        unit = self._unit(values={"calibration_step": 60.0})
        lane = unit.lanes["lane1"]

        def trip_hub(method: str, params: Dict[str, Any]) -> None:
            """The second feed (already recorded when this runs) reaches the hub."""
            if len(wire.moves()) == 2:
                lane.hub_obj.state = True
        wire = AceP2Wire(unit, on_move=trip_hub)

        result = unit._calibrate_hub_inner(lane)

        assert result == (True, "dist_hub calibration: 120.0mm (was 10.0mm)", 120.0)
        # The saved distance is the sum of both steps, not the last one.
        assert lane.dist_hub == 120.0
        assert unit.printer.afc.function.ConfigRewrite.calls == [
            (("AFC_lane lane1", "dist_hub", 120.0, "\n dist_hub: New: 120.0 Old: 10.0"), {})]
        # Back off 50mm short of the trigger point.
        assert wire.moves() == [
            ("feed_filament", {"index": 0, "length": 60.0, "speed": 100.0}),
            ("feed_filament", {"index": 0, "length": 60.0, "speed": 100.0}),
            ("unwind_filament", {"index": 0, "length": 70.0, "speed": 100.0,
                                 "mode": "normal"})]
        assert unit.printer.logger.messages == [
            ("raw", "Calibrating dist_hub for lane1 (max 4000mm in 60.0mm steps)"),
            ("info", "ACE hub calibrate: coarse trigger at 120.0mm"),
            ("info", "ACE hub calibrate: trigger at 120.0mm")]

    def test_no_trigger_fails(self):
        unit = self._unit(values={"calibration_step": 4000.0})
        lane = unit.lanes["lane1"]
        wire = AceP2Wire(unit)

        result = unit._calibrate_hub_inner(lane)

        assert result == (False, "Hub sensor did not trigger after 4000mm. Check filament "
                                 "path and hub sensor wiring.", 4000.0)
        assert lane.dist_hub == 10.0
        # The whole coarse feed is pulled back.
        assert wire.moves() == [
            ("feed_filament", {"index": 0, "length": 4000.0, "speed": 100.0}),
            ("unwind_filament", {"index": 0, "length": 4000.0, "speed": 100.0,
                                 "mode": "normal"})]
        assert unit.printer.afc.function.ConfigRewrite.calls == []
        assert unit.printer.logger.messages == [
            ("raw", "Calibrating dist_hub for lane1 (max 4000mm in 4000.0mm steps)")]

    def test_failed_feed_ends_the_pass(self):
        unit = self._unit()
        lane = unit.lanes["lane1"]

        def refuse(method: str, params: Dict[str, Any]) -> None:
            """The unit refuses the feed."""
            raise RuntimeError("FORBIDDEN")
        wire = AceP2Wire(unit, on_move=refuse)

        result = unit._calibrate_hub_inner(lane)

        assert result == (False, "Hub sensor did not trigger after 0mm. Check filament "
                                 "path and hub sensor wiring.", 0.0)
        # Nothing was fed, so nothing is retracted.
        assert len(wire.moves()) == 1
        assert unit.printer.logger.messages == [
            ("raw", "Calibrating dist_hub for lane1 (max 4000mm in 50.0mm steps)"),
            ("error", "ACE hub calibrate: feed failed: FORBIDDEN")]

    def test_no_trigger_retract_failure_is_logged(self):
        unit = self._unit(values={"calibration_step": 4000.0})
        lane = unit.lanes["lane1"]
        AceP2Wire(unit)
        unit._ace.unwind_filament = Recorder(raises=RuntimeError("unwind boom"))

        result = unit._calibrate_hub_inner(lane)

        assert result[0] is False
        assert unit.printer.logger.messages == [
            ("raw", "Calibrating dist_hub for lane1 (max 4000mm in 4000.0mm steps)"),
            ("error", "ACE hub calibrate: retract failed: unwind boom")]

    def test_triggered_retract_failure_is_still_a_success(self):
        unit = self._unit(values={"calibration_step": 60.0})
        lane = unit.lanes["lane1"]

        def trip_hub(method: str, params: Dict[str, Any]) -> None:
            """The second feed reaches the hub, leaving 70mm to retract."""
            if len(wire.moves()) == 2:
                lane.hub_obj.state = True
        wire = AceP2Wire(unit, on_move=trip_hub)
        unit._ace.unwind_filament = Recorder(raises=RuntimeError("unwind boom"))

        result = unit._calibrate_hub_inner(lane)

        assert result == (True, "dist_hub calibration: 120.0mm (was 10.0mm)", 120.0)
        assert unit.printer.logger.messages == [
            ("raw", "Calibrating dist_hub for lane1 (max 4000mm in 60.0mm steps)"),
            ("info", "ACE hub calibrate: coarse trigger at 120.0mm"),
            ("info", "ACE hub calibrate: trigger at 120.0mm"),
            ("error", "ACE hub calibrate: retract failed: unwind boom")]


class TestAfcACESyncInventory:
    def test_v1_sync_inventory_reads_every_slot(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_filament_info", lambda params: {"index": params["index"]})

        unit._sync_inventory()

        assert unit._ace.commands == [("get_filament_info", {"index": 0}),
                                      ("get_filament_info", {"index": 1}),
                                      ("get_filament_info", {"index": 2}),
                                      ("get_filament_info", {"index": 3})]
        assert [inv["raw"] for inv in unit._slot_inventory] == [
            {"index": 0}, {"index": 1}, {"index": 2}, {"index": 3}]
        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: slot 0 get_filament_info -> {'index': 0}"),
            ("debug", "ACE Ace_1: slot 1 get_filament_info -> {'index': 1}"),
            ("debug", "ACE Ace_1: slot 2 get_filament_info -> {'index': 2}"),
            ("debug", "ACE Ace_1: slot 3 get_filament_info -> {'index': 3}")]

    def test_v1_sync_inventory_stores_payload(self):
        unit = ace_p2_unit()
        tagged = {"index": 0, "sku": "HPL19-107", "type": "PLA"}
        unit._ace.set_reply("get_filament_info", lambda params: (
            tagged if params["index"] == 0 else "junk"))

        unit._sync_inventory()

        assert unit._slot_inventory[0]["sku"] == "HPL19-107"
        assert unit._slot_inventory[0]["material"] == "PLA"
        # A non-dict reply is not stored.
        assert unit._slot_inventory[1:] == [{}, {}, {}]
        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: slot 0 get_filament_info -> "
                      "{'index': 0, 'sku': 'HPL19-107', 'type': 'PLA'}"),
            ("info", "ACE Ace_1: slot 0 RFID read, sku='HPL19-107' brand='' type='PLA' "
                     "rfid=None nozzle=None-NoneC")]

    def test_failed_slot_query_is_logged_and_the_sweep_goes_on(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_filament_info", lambda params: (
            RuntimeError("boom") if params["index"] == 1 else "junk"))

        unit._sync_inventory()

        assert len(unit._ace.commands) == 4
        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: slot 1 inventory query failed: boom")]

    def test_v1_sync_inventory_skips_when_disconnected(self):
        unit = ace_p2_unit()
        unit._ace.connected = False

        unit._sync_inventory()

        assert unit._ace.commands == []
        assert unit._slot_inventory == [{}, {}, {}, {}]
        assert unit.printer.logger.messages == []

    def test_v1_sync_inventory_skips_when_no_conn(self):
        unit = ace_p2_unit(connection=None)

        unit._sync_inventory()

        assert unit._slot_inventory == [{}, {}, {}, {}]
        assert unit.printer.logger.messages == []

    def test_v2_sync_inventory_skips_firmware_even_when_connected(self):
        # The ACE 2 reads tags host-side, so the firmware sweep never runs.
        unit = make_ace2_unit()
        unit._ace.set_reply("get_filament_info", {"index": 0, "sku": "HPL19-107"})

        unit._sync_inventory()

        assert unit._ace.commands == []
        assert unit._slot_inventory[0] == {}
        assert unit.printer.logger.messages == []


class TestAfcACEStoreSlotRfid:
    def test_full_payload_derives_temps_and_logs_read(self):
        unit = ace_p2_unit()
        info = {"sku": "HPL-1", "brand": "AC", "type": "PLA", "rfid": 2,
                "color": [1, 2, 3], "diameter": 1.75,
                "extruder_temp": {"min": 200, "max": 220},
                "hotbed_temp": {"min": 50, "max": 60}}

        unit._store_slot_rfid(0, info)

        assert unit._slot_inventory[0] == {
            "raw": info, "material": "PLA", "color": [1, 2, 3], "sku": "HPL-1",
            "brand": "AC", "diameter": 1.75, "total_weight": 0, "current_weight": 0,
            "rfid": 2, "source": None,
            "extruder_temp_min": 200, "extruder_temp_max": 220, "extruder_temp": 210,
            "bed_temp_min": 50, "bed_temp_max": 60, "bed_temp": 55}
        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: slot 0 get_filament_info -> {'sku': 'HPL-1', "
                      "'brand': 'AC', 'type': 'PLA', 'rfid': 2, 'color': [1, 2, 3], "
                      "'diameter': 1.75, 'extruder_temp': {'min': 200, 'max': 220}, "
                      "'hotbed_temp': {'min': 50, 'max': 60}}"),
            ("info", "ACE Ace_1: slot 0 RFID read, sku='HPL-1' brand='AC' type='PLA' "
                     "rfid=2 nozzle=200-220C")]

    def test_max_only_temps_and_no_change_no_log(self):
        # A recognized tag that reads the same as last time logs nothing.
        unit = ace_p2_unit()
        info = {"material": "PETG", "sku": "S", "rfid": 2, "total": 1000, "current": 600,
                "extruder_temp": {"max": 240}, "hotbed_temp": {"max": 70}}
        unit._slot_inventory[0]["raw"] = dict(info)

        unit._store_slot_rfid(0, info)

        inv = unit._slot_inventory[0]
        assert (inv["material"], inv["total_weight"], inv["current_weight"]) == (
            "PETG", 1000, 600)
        assert (inv["extruder_temp_min"], inv["extruder_temp"]) == (None, 240)
        assert (inv["bed_temp_min"], inv["bed_temp"]) == (None, 70)
        assert unit.printer.logger.messages == []

    def test_non_dict_temps_yield_none(self):
        unit = ace_p2_unit()

        unit._store_slot_rfid(0, {"extruder_temp": "bad", "hotbed_temp": 5})

        inv = unit._slot_inventory[0]
        assert (inv["extruder_temp"], inv["extruder_temp_min"], inv["extruder_temp_max"]) == (
            None, None, None)
        assert (inv["bed_temp"], inv["bed_temp_min"], inv["bed_temp_max"]) == (None, None, None)
        assert (inv["material"], inv["color"], inv["diameter"]) == ("", [0, 0, 0], 1.75)
        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: slot 0 get_filament_info -> "
                      "{'extruder_temp': 'bad', 'hotbed_temp': 5}")]

    def test_min_without_max_temp_is_none(self):
        unit = ace_p2_unit()

        unit._store_slot_rfid(0, {"extruder_temp": {"min": 200}, "hotbed_temp": {"min": 50}})

        inv = unit._slot_inventory[0]
        assert (inv["extruder_temp_min"], inv["extruder_temp"]) == (200, None)
        assert (inv["bed_temp_min"], inv["bed_temp"]) == (50, None)
        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: slot 0 get_filament_info -> "
                      "{'extruder_temp': {'min': 200}, 'hotbed_temp': {'min': 50}}")]

    def test_unrecognized_tag_change_logs_debug_only(self):
        unit = ace_p2_unit()

        unit._store_slot_rfid(1, {"rfid": 1})

        assert unit._slot_inventory[1]["rfid"] == 1
        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: slot 1 get_filament_info -> {'rfid': 1}")]

    def test_recognizing_tag_without_sku_logs_read(self):
        unit = ace_p2_unit()

        unit._store_slot_rfid(2, {"rfid": 3, "brand": "B"})

        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: slot 2 get_filament_info -> {'rfid': 3, 'brand': 'B'}"),
            ("info", "ACE Ace_1: slot 2 RFID read, sku='' brand='B' type='' rfid=3 "
                     "nozzle=None-NoneC")]


class TestAfcACERefreshSlotInventory:
    def test_disconnected_noop(self):
        unit = ace_p2_unit(connection=None)

        unit._refresh_slot_inventory(0)

        assert unit._slot_inventory == [{}, {}, {}, {}]
        assert unit.printer.logger.messages == []

    def test_down_link_noop(self):
        unit = ace_p2_unit()
        unit._ace.connected = False

        unit._refresh_slot_inventory(0)

        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_out_of_range_noop(self):
        unit = ace_p2_unit()

        unit._refresh_slot_inventory(9)
        unit._refresh_slot_inventory(-1)

        assert unit._ace.commands == []
        assert unit._slot_inventory == [{}, {}, {}, {}]
        assert unit.printer.logger.messages == []

    def test_success_stores(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_filament_info", {"sku": "S1"})

        unit._refresh_slot_inventory(2)

        assert unit._ace.commands == [("get_filament_info", {"index": 2})]
        assert unit._slot_inventory[2]["sku"] == "S1"
        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: slot 2 get_filament_info -> {'sku': 'S1'}"),
            ("info", "ACE Ace_1: slot 2 RFID read, sku='S1' brand='' type='' rfid=None "
                     "nozzle=None-NoneC")]

    def test_non_dict_reply_not_stored(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_filament_info", None)

        unit._refresh_slot_inventory(2)

        assert unit._slot_inventory[2] == {}
        assert unit.printer.logger.messages == []

    def test_exception_logged_debug(self):
        unit = ace_p2_unit()
        unit._ace.set_reply("get_filament_info", RuntimeError("boom"))

        unit._refresh_slot_inventory(0)

        assert unit._slot_inventory[0] == {}
        assert unit.printer.logger.messages == [
            ("debug", "ACE Ace_1: slot 0 RFID refresh failed: boom")]


class TestAfcACEClearSlotInventory:
    def test_clears_fields_and_drops_uid(self):
        unit = ace_p2_unit(inventory={1: {"material": "PLA", "color": [1, 2, 3], "uid": "AA",
                                          "sku": "S"}})

        unit._clear_slot_inventory(1)

        # Only the identity fields go; the rest of the cache stays.
        assert unit._slot_inventory[1] == {"material": "", "color": [0, 0, 0], "sku": "S"}
        assert unit.printer.logger.messages == []

    def test_out_of_range_noop(self):
        unit = ace_p2_unit(inventory={0: {"material": "PLA"}, 3: {"material": "ABS"}})

        unit._clear_slot_inventory(99)
        unit._clear_slot_inventory(-1)

        # -1 would otherwise wrap to the last slot.
        assert unit._slot_inventory == [{"material": "PLA"}, {}, {}, {"material": "ABS"}]
        assert unit.printer.logger.messages == []


class TestAfcACEReaderSiblingSlot:
    def test_base_ace_has_no_shared_reader_sibling(self):
        unit = ace_p2_unit(inventory={0: {"sku": "X"}, 1: {"sku": "X"}})

        assert [unit._reader_sibling_slot(slot) for slot in range(4)] == [None] * 4
        # A per-slot reader is never shared, so the same tag twice is not ambiguous.
        assert unit._shared_rfid_ambiguous(0) is False


class TestAfcACESharedRfidAmbiguous:
    def test_ace2_ambiguous_when_sibling_reports_same_sku(self):
        # Slot 1 read slot 0's tag through the reader they share.
        unit = make_ace2_unit(inventory={0: {"sku": "HPL19-107"}, 1: {"sku": " HPL19-107 "}})

        assert unit._shared_rfid_ambiguous(1) is True
        assert unit._shared_rfid_ambiguous(0) is True

    def test_ace2_ambiguous_when_sibling_reports_same_uid(self):
        unit = make_ace2_unit(inventory={2: {"sku": "", "uid": "deadbeef"},
                                         3: {"sku": "", "uid": "deadbeef"}})

        assert unit._shared_rfid_ambiguous(2) is True

    def test_ace2_not_ambiguous_when_sibling_differs(self):
        unit = make_ace2_unit(inventory={2: {"sku": "BAMBU-A", "uid": "aa"},
                                         3: {"sku": "BAMBU-B", "uid": "bb"}})

        assert unit._shared_rfid_ambiguous(2) is False
        assert unit._shared_rfid_ambiguous(3) is False

    def test_ace2_not_ambiguous_when_sibling_empty(self):
        unit = make_ace2_unit(inventory={0: {"sku": "HPL19-107", "uid": "aa"}})

        assert unit._shared_rfid_ambiguous(0) is False


class TestAfcACESyncSlotLoadedState:
    def test_disconnected_noop(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True), connection=None)

        unit._sync_slot_loaded_state()

        # An empty inventory would have cleared prep had the sync run.
        assert unit.lanes["lane1"].prep_state is True
        assert unit.printer.logger.messages == []

    def test_down_link_noop(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True))
        unit._ace.connected = False

        unit._sync_slot_loaded_state()

        assert unit.lanes["lane1"].prep_state is True
        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []

    def test_ready_slot_marks_prep_state(self):
        unit = ace_p2_unit("lane1", inventory={0: {"status": "ready", "material": "PETG"}})
        lane = unit.lanes["lane1"]

        unit._sync_slot_loaded_state()

        assert lane.prep_state is True
        # apply_filament_defaults is stubbed to None until real RFID/Spoolman
        # identification is wired back in, so the inventory's material isn't
        # applied to the lane yet.
        assert lane.material is None
        assert unit.printer.logger.messages == []

    def test_empty_slot_clears_hub_state(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True, load=True),
                           inventory={0: {"status": "empty"}})
        lane = unit.lanes["lane1"]
        lane._load_state = True

        unit._sync_slot_loaded_state()

        assert lane.prep_state is False
        assert lane.loaded_to_hub is False
        # The virtual hub reads clear again: the lane is not in the tool.
        assert lane._load_state is False
        assert unit.printer.logger.messages == []

    def test_slot_beyond_unit_capacity_is_skipped(self):
        unit = ace_p2_unit(LaneSpec("lane1", prep=True), inventory={0: {"status": "empty"}})
        lane = unit.lanes["lane1"]
        # Force an out-of-range slot (a correctly configured unit never does this).
        unit._slot_map["lane1"] = 99

        unit._sync_slot_loaded_state()

        # An in-range read would have cleared prep_state; this lane is left alone.
        assert lane.prep_state is True
        assert unit.printer.logger.messages == []

    def test_applies_filament_defaults_when_available(self, monkeypatch):
        unit = ace_p2_unit("lane1", inventory={0: {"status": "ready", "material": "PETG"}})
        lane = unit.lanes["lane1"]
        apply_defaults = Recorder()
        monkeypatch.setattr(afc_ace_module, "apply_filament_defaults", apply_defaults)

        unit._sync_slot_loaded_state()

        assert apply_defaults.calls == [((lane, unit._slot_inventory[0]), {
            "color_converter": None,
            "afc_defaults": {"default_material_type": None, "default_color": None}})]

    def test_ambiguous_shared_read_is_not_applied(self):
        unit = make_ace2_unit(lanes=["lane1", "lane2"], inventory={
            0: {"status": "ready", "sku": "S", "material": "PLA"},
            1: {"status": "ready", "sku": "S", "material": "PLA"}})

        unit._sync_slot_loaded_state()

        assert [unit.lanes[n].prep_state for n in ("lane1", "lane2")] == [True, True]
        assert [unit.lanes[n].material for n in ("lane1", "lane2")] == [None, None]
        assert unit.printer.logger.messages == [
            ("info", "ACE Ace2_1: slot 0 RFID read is ambiguous (shared reader sibling "
                     "reports the same tag), not applying to lane1 at startup"),
            ("info", "ACE Ace2_1: slot 1 RFID read is ambiguous (shared reader sibling "
                     "reports the same tag), not applying to lane2 at startup")]



# Uncomment below once RFID and spoolman creation support is added
# class TestAfcACEGetAutoSpoolmanCreate:
    # def test_fallback_when_helper_absent(self, monkeypatch):
        # monkeypatch.setattr(afc_ace_module, "get_auto_spoolman_create", None)
        # unit = ace_p2_unit("lane1", values={"auto_spoolman_create": True})

        # assert unit._get_auto_spoolman_create(unit.lanes["lane1"]) is True

    # def test_fallback_ignores_extruder_opt_in(self, monkeypatch):
        # monkeypatch.setattr(afc_ace_module, "get_auto_spoolman_create", None)
        # unit = ace_p2_unit("lane1")
        # lane = unit.lanes["lane1"]
        # lane.extruder_obj.auto_spoolman_create = True

        # assert unit._get_auto_spoolman_create(lane) is False

    # def test_delegates_to_helper_when_present(self):
        # # The AFC_RFID helper honours the extruder's opt-in over the unit default.
        # unit = ace_p2_unit("lane1")
        # lane = unit.lanes["lane1"]
        # lane.extruder_obj.auto_spoolman_create = True

        # assert unit.auto_spoolman_create is False
        # assert unit._get_auto_spoolman_create(lane) is True

    # def test_helper_falls_back_to_unit_default_when_nothing_opts_in(self):
        # # Neither unit nor extruder opts in, so the helper returns the unit default it is given.
        # unit = ace_p2_unit("lane1")
        # lane = unit.lanes["lane1"]

        # assert afc_ace_module.get_auto_spoolman_create is not None
        # assert unit.auto_spoolman_create is False
        # assert not hasattr(lane.extruder_obj, "auto_spoolman_create")
        # assert unit._get_auto_spoolman_create(lane) is False



class TestCrc16CcittReflected:
    def test_empty_is_init_value(self):
        assert crc16_ccitt_reflected(b"") == 0xFFFF

    def test_known_check_vector(self):
        # The published CRC-16/MCRF4XX check value for "123456789".
        assert crc16_ccitt_reflected(b"123456789") == 0x6F91


class TestAceExtractGetInfoId:
    @staticmethod
    def _frame(payload: bytes) -> bytes:
        """
        :param payload: JSON bytes
        :return bytes: the payload framed as the unit sends it
        """
        return (b"\xff\xaa" + struct.pack("<H", len(payload)) + payload
                + struct.pack("<H", crc16_ccitt_reflected(payload)) + b"\xfe")

    def _get_info_frame(self, unit_id: int) -> bytes:
        """
        :param unit_id: id the unit reports
        :return bytes: a real unit's framed get_info reply
        """
        return self._frame(json.dumps(
            {"id": 0, "code": 0, "result": {"id": unit_id, "slots": 4,
                                             "model": "Anycubic Color Engine Pro",
                                             "firmware": "V1.3.856"},
             "msg": "success"}).encode("utf-8"))

    @pytest.mark.parametrize("unit_id", [1, 2, 3, 4])
    def test_extract_id_whole_frame(self, unit_id):
        assert _ace_extract_get_info_id(self._get_info_frame(unit_id)) == unit_id

    def test_extract_id_tolerates_garbage_prefix(self):
        # A partial previous frame or noise ahead of the real header.
        assert _ace_extract_get_info_id(b"\x00\x11ff" + self._get_info_frame(2)) == 2

    def test_extract_id_without_header_returns_none(self):
        assert _ace_extract_get_info_id(b"\x00\x11ff") is None

    def test_extract_id_header_without_length_returns_none(self):
        assert _ace_extract_get_info_id(b"\xff\xaa\x10") is None

    def test_extract_id_truncated_returns_none(self):
        assert _ace_extract_get_info_id(self._get_info_frame(1)[:9]) is None

    def test_extract_id_no_result_returns_none(self):
        assert _ace_extract_get_info_id(self._frame(b"{}")) is None

    def test_extract_id_bad_json_returns_none(self):
        assert _ace_extract_get_info_id(self._frame(b"not json")) is None


class TestAceScanCandidates:
    def test_returns_sorted_realpaths(self, monkeypatch):
        monkeypatch.setattr(afc_ace_module.glob, "glob", lambda pattern: ["/dev/b", "/dev/a"])
        monkeypatch.setattr(afc_ace_module.os.path, "realpath", lambda p: p.upper())

        assert afc_ace_module._ace_scan_candidates() == ["/DEV/A", "/DEV/B"]

    def test_realpath_failure_falls_back_to_the_raw_path(self, monkeypatch):
        monkeypatch.setattr(afc_ace_module.glob, "glob", lambda pattern: ["/dev/a"])

        def raise_realpath(p: str) -> str:
            """A path the OS can't resolve (e.g. a dangling symlink)."""
            error_str = "boom"
            raise OSError(error_str)
        monkeypatch.setattr(afc_ace_module.os.path, "realpath", raise_realpath)

        assert afc_ace_module._ace_scan_candidates() == ["/dev/a"]


class TestProbeAceGetInfoId:
    @staticmethod
    def _frame(obj: Dict[str, Any]) -> bytes:
        """
        :param obj: the JSON-decodable reply body
        :return bytes: the reply framed as a real unit sends it
        """
        payload = json.dumps(obj, separators=(',', ':')).encode('utf-8')
        crc = crc16_ccitt_reflected(payload)
        return (b"\xff\xaa" + struct.pack('<H', len(payload)) + payload
                + struct.pack('<H', crc) + b"\xfe")

    @staticmethod
    def _serial_module(fake_port: FakeSerial) -> Any:
        """
        :param fake_port: the FakeSerial Serial() should hand back
        :return Any: a sys.modules["serial"] stand-in
        """
        module = types.SimpleNamespace()
        module.Serial = (lambda port="", baudrate=0, timeout=0.0, write_timeout=0.0,
                         _fake=fake_port: _fake)
        return module

    def test_import_error_returns_none(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "serial", None)

        assert afc_ace_module.probe_ace_get_info_id("/dev/ttyACM0") is None

    def test_open_failure_returns_none(self, monkeypatch):
        module = types.SimpleNamespace()

        def raise_open(port: str, baudrate: int, timeout: float,
                       write_timeout: float) -> FakeSerial:
            """The OS refuses to open the device."""
            error_str = "busy"
            raise OSError(error_str)
        module.Serial = raise_open
        monkeypatch.setitem(sys.modules, "serial", module)

        assert afc_ace_module.probe_ace_get_info_id("/dev/ttyACM0") is None

    def test_returns_the_reported_id(self, monkeypatch):
        port = FakeSerial()
        port.on_write = lambda frame: port.rx.extend(self._frame({"result": {"id": 3}}))
        monkeypatch.setitem(sys.modules, "serial", self._serial_module(port))

        assert afc_ace_module.probe_ace_get_info_id("/dev/ttyACM0", timeout=1.0) == 3
        assert port.closed is True

    def test_reply_arriving_after_an_empty_read_still_resolves(self, monkeypatch):
        port = FakeSerial()
        frame = self._frame({"result": {"id": 5}})
        real_read = port.read
        state = {"n": 0}

        def flaky_read(size: int = 1) -> bytes:
            """The first poll finds nothing; the reply lands on the second."""
            state["n"] += 1
            if state["n"] == 1:
                return b""
            if state["n"] == 2:
                port.rx.extend(frame)
            return real_read(size)
        port.read = flaky_read
        monkeypatch.setitem(sys.modules, "serial", self._serial_module(port))

        assert afc_ace_module.probe_ace_get_info_id("/dev/ttyACM0", timeout=1.0) == 5

    def test_reply_split_across_two_chunks_still_resolves(self, monkeypatch):
        port = FakeSerial()
        frame = self._frame({"result": {"id": 7}})
        split = len(frame) // 2
        chunks = [frame[:split], frame[split:]]

        def chunked_read(size: int = 1) -> bytes:
            """The frame arrives in two pieces, neither alone parseable."""
            return chunks.pop(0) if chunks else b""
        port.read = chunked_read
        monkeypatch.setitem(sys.modules, "serial", self._serial_module(port))

        assert afc_ace_module.probe_ace_get_info_id("/dev/ttyACM0", timeout=1.0) == 7

    def test_timeout_with_no_reply_returns_none(self, monkeypatch):
        port = FakeSerial()
        monkeypatch.setitem(sys.modules, "serial", self._serial_module(port))

        assert afc_ace_module.probe_ace_get_info_id("/dev/ttyACM0", timeout=0.05) is None
        assert port.closed is True


class TestResolveAcePortByIndex:
    class _Clock:
        """time stand-in: sleep moves the clock instead of waiting."""

        def __init__(self) -> None:
            """
            Start at 1000s.
            """
            self.now = 1000.0
            self.sleeps: List[float] = []

        def time(self) -> float:
            """
            :return float: the clock
            """
            return self.now

        def sleep(self, seconds: float) -> None:
            """
            :param seconds: how far to move the clock
            """
            self.sleeps.append(seconds)
            self.now += seconds

    @pytest.fixture(autouse=True)
    def _isolate(self) -> Iterator[None]:
        """Clear the claimed-port set around each test."""
        reset_ace_globals()
        yield
        reset_ace_globals()

    def _bus(self, monkeypatch: pytest.MonkeyPatch,
             id_by_port: Dict[str, Optional[int]]) -> List[str]:
        """
        Fake the Anycubic devices present, each reporting its id.

        :param monkeypatch: pytest monkeypatch
        :param id_by_port: tty path to the id it reports (None: not an ACE)
        :return list: the ports probed, in order
        """
        probed: List[str] = []

        def probe(port: str, baud: int = 115200, timeout: float = 1.5) -> Optional[int]:
            """Record the probe and answer with the port's id."""
            probed.append(port)
            return id_by_port.get(port)
        monkeypatch.setattr(afc_ace_module, "_ace_scan_candidates", lambda: list(id_by_port))
        monkeypatch.setattr(afc_ace_module, "probe_ace_get_info_id", probe)
        monkeypatch.setattr(afc_ace_module, "time", self._Clock())
        return probed

    def test_resolve_binds_by_reported_id(self, monkeypatch):
        self._bus(monkeypatch, {"/dev/ttyACM5": 1, "/dev/ttyACM2": 2, "/dev/ttyACM9": 3})

        # Each index binds to the device reporting that id, not the nth tty by name.
        assert resolve_ace_port_by_index(2, settle=0.0) == "/dev/ttyACM2"
        assert resolve_ace_port_by_index(1, settle=0.0) == "/dev/ttyACM5"
        assert resolve_ace_port_by_index(3, settle=0.0) == "/dev/ttyACM9"

    def test_logs_each_answering_port(self, monkeypatch):
        self._bus(monkeypatch, {"/dev/ttyACM0": None, "/dev/ttyACM5": 1, "/dev/ttyACM2": 2})
        logger = AceLogger()

        assert resolve_ace_port_by_index(2, settle=0.0, logger=logger) == "/dev/ttyACM2"
        # A port that does not answer is not logged.
        assert logger.messages == [("info", "ACE autodetect: /dev/ttyACM5 reports id 1"),
                                   ("info", "ACE autodetect: /dev/ttyACM2 reports id 2")]

    def test_resolve_missing_index_returns_none(self, monkeypatch):
        probed = self._bus(monkeypatch, {"/dev/ttyACM5": 1})

        assert resolve_ace_port_by_index(2, settle=0.0) is None
        # The 0.5s minimum settle window allows one rescan.
        assert probed == ["/dev/ttyACM5", "/dev/ttyACM5"]
        assert afc_ace_module.time.sleeps == [0.5]

    def test_resolve_skips_claimed_ports(self, monkeypatch):
        # ttyACM5 reports id 1 but another live unit holds it: never probed.
        probed = self._bus(monkeypatch, {"/dev/ttyACM5": 1, "/dev/ttyACM7": 1})
        afc_ace_module._ACE_CLAIMED_PORTS.add("/dev/ttyACM5")

        assert resolve_ace_port_by_index(1, settle=0.0) == "/dev/ttyACM7"
        assert probed == ["/dev/ttyACM7"]

    def test_resolve_ignores_non_ace_devices(self, monkeypatch):
        # A device that does not answer get_info (probe None) is skipped.
        self._bus(monkeypatch, {"/dev/ttyACM0": None, "/dev/ttyACM3": 1})

        assert resolve_ace_port_by_index(1, settle=0.0) == "/dev/ttyACM3"


class TestRunOffReactor:
    def test_runs_on_a_worker_while_the_reactor_turns(self):
        reactor = AceP2ThreadedReactor()
        main = threading.get_ident()
        seen: Dict[str, int] = {}

        def slow() -> str:
            """A blocking probe."""
            seen["thread"] = threading.get_ident()
            time.sleep(0.3)
            return "/dev/ttyACM2"

        assert run_off_reactor(reactor, slow) == "/dev/ttyACM2"
        assert seen["thread"] != main
        # The reactor kept turning for the whole probe ...
        assert reactor.ticks >= 10
        # ... and the wake-up ran on the reactor thread, not the worker.
        assert reactor.completions[0].completed_on == main

    def test_worker_exception_is_raised_on_the_reactor(self):
        reactor = AceP2ThreadedReactor()
        failure = ACESerialError("no unit found")

        def boom() -> None:
            """A probe that fails."""
            raise failure

        with pytest.raises(ACESerialError) as excinfo:
            run_off_reactor(reactor, boom)
        assert excinfo.value is failure

    def test_gives_up_after_the_timeout(self):
        reactor = AceP2ThreadedReactor()
        gate = threading.Event()
        try:
            with pytest.raises(ACESerialError, match=r"^probe did not finish within 0s$"):
                run_off_reactor(reactor, lambda: gate.wait(5), what="probe", timeout=0.2)
        finally:
            gate.set()

    def test_reactor_without_async_callbacks_runs_inline(self):
        class _Bare:
            """A reactor with no register_async_callback."""

        assert run_off_reactor(_Bare(), threading.get_ident) == threading.get_ident()


class TestHeldLog:
    def test_holds_lines_then_replays_them_in_order(self):
        held = afc_ace_module._HeldLog()
        held.debug("d")
        held.info("i")
        held.warning("w")
        held.error("e")
        logger = AceLogger()

        held.replay(logger)

        assert logger.messages == [("debug", "d"), ("info", "i"),
                                   ("warning", "w"), ("error", "e")]
        assert held._lines == []

    def test_unsupported_attribute_raises(self):
        held = afc_ace_module._HeldLog()

        with pytest.raises(AttributeError):
            held.respond_info("nope")

    def test_replay_swallows_a_logger_exception(self):
        held = afc_ace_module._HeldLog()
        held.info("boom")
        logger = AceLogger()
        logger.info = Recorder(raises=RuntimeError("down"))

        held.replay(logger)

        assert held._lines == []


class TestACEConnectionConnected:
    def test_default_disconnected(self):
        conn = ace_p2_connection(connected=False)

        assert conn.connected is False
        # __init__'s defaults: an ACE PRO has four slots.
        assert conn.slot_count == 4

    def test_connected_true(self):
        conn = ace_p2_connection()

        assert conn.connected is True
        conn._connected = False
        assert conn.connected is False


class TestACEConnectionPreInfoHandshake:
    def test_base_is_noop(self):
        conn = ace_p2_connection()

        assert conn._pre_info_handshake() is None
        # The V1 unit needs no discover: nothing is sent.
        assert conn._serial.frames == []
        assert conn._next_request_id == 0


class TestACEConnectionResolveConnectPort:
    def test_explicit_port_is_returned_as_is(self):
        conn = ace_p2_connection(serial_port="/dev/ttyACM4", connected=False)

        assert conn._resolve_connect_port() == "/dev/ttyACM4"

    def test_auto_with_no_resolved_port_runs_the_full_scan(self, monkeypatch):
        conn = ace_p2_connection(serial_port="auto", connected=False, ace_index=2)
        monkeypatch.setattr(afc_ace_module, "resolve_ace_port_by_index",
                            Recorder(result="/dev/ttyACM9"))

        assert conn._resolve_connect_port() == "/dev/ttyACM9"
        assert afc_ace_module.resolve_ace_port_by_index.calls == [
            ((2, conn._baud_rate), {"logger": conn._logger})]

    def test_auto_fast_path_reuses_a_still_matching_resolved_port(self, monkeypatch):
        conn = ace_p2_connection(serial_port="auto", connected=False, ace_index=2)
        conn._resolved_port = "/dev/ttyACM9"
        monkeypatch.setattr(afc_ace_module, "probe_ace_get_info_id",
                            Recorder(result=2))
        full_scan = Recorder()
        monkeypatch.setattr(afc_ace_module, "resolve_ace_port_by_index", full_scan)

        assert conn._resolve_connect_port() == "/dev/ttyACM9"
        assert full_scan.calls == []

    def test_auto_fast_path_falls_back_when_the_probe_mismatches(self, monkeypatch):
        conn = ace_p2_connection(serial_port="auto", connected=False, ace_index=2)
        conn._resolved_port = "/dev/ttyACM9"
        monkeypatch.setattr(afc_ace_module, "probe_ace_get_info_id",
                            Recorder(result=1))
        monkeypatch.setattr(afc_ace_module, "resolve_ace_port_by_index",
                            Recorder(result="/dev/ttyACM3"))

        assert conn._resolve_connect_port() == "/dev/ttyACM3"

    def test_auto_fast_path_falls_back_when_the_probe_raises(self, monkeypatch):
        conn = ace_p2_connection(serial_port="auto", connected=False, ace_index=2)
        conn._resolved_port = "/dev/ttyACM9"
        monkeypatch.setattr(afc_ace_module, "probe_ace_get_info_id",
                            Recorder(raises=RuntimeError("boom")))
        monkeypatch.setattr(afc_ace_module, "resolve_ace_port_by_index",
                            Recorder(result="/dev/ttyACM3"))

        assert conn._resolve_connect_port() == "/dev/ttyACM3"

    def test_auto_scan_finding_nothing_raises(self, monkeypatch):
        conn = ace_p2_connection(serial_port="auto", connected=False, ace_index=3)
        monkeypatch.setattr(afc_ace_module, "resolve_ace_port_by_index",
                            Recorder(result=None))

        with pytest.raises(ACESerialError, match=(
                r"^ACE autodetect: no unit reporting id 3 found \(serial_port: auto\)\. "
                r"Is it plugged in / powered\?$")):
            conn._resolve_connect_port()


class TestACEConnectionConnect:
    class _ThreadLog:
        """Logger recording (level, message) and the thread that wrote each line."""

        def __init__(self) -> None:
            """
            Start with no lines.
            """
            self.messages: List[Tuple[str, str]] = []
            self.threads: List[int] = []

        def _record(self, level: str, msg: str) -> None:
            """
            :param level: log level
            :param msg: message
            """
            self.messages.append((level, msg))
            self.threads.append(threading.get_ident())

        def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
            """
            :param msg: message
            """
            self._record("debug", msg)

        def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
            """
            :param msg: message
            """
            self._record("info", msg)

        def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
            """
            :param msg: message
            """
            self._record("warning", msg)

        def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
            """
            :param msg: message
            """
            self._record("error", msg)

    @pytest.fixture(autouse=True)
    def _isolate(self) -> Iterator[None]:
        """Clear the claimed-port set around each test."""
        reset_ace_globals()
        yield
        reset_ace_globals()

    def _auto_conn(self, monkeypatch: pytest.MonkeyPatch, cls: type = ACEConnection,
                   *responses: Dict[str, Any]) -> Tuple[Any, AceP2ThreadedReactor, Any,
                                                        AceP2SerialPort]:
        """
        A serial_port: auto connection on a threaded reactor, pyserial faked.

        :param monkeypatch: pytest monkeypatch
        :param cls: connection class
        :param responses: the unit's answers once open
        :return tuple: (connection, reactor, thread log, serial module)
        """
        reactor = AceP2ThreadedReactor()
        log = self._ThreadLog()
        conn = cls(reactor, "auto", logger=log, ace_index=1)
        port = AceP2SerialPort(*responses)
        port.conn = conn
        monkeypatch.setitem(sys.modules, "serial", port)
        return conn, reactor, log, port

    def test_connect_resolves_off_reactor_and_logs_on_it(self, monkeypatch):
        conn, reactor, log, port = self._auto_conn(
            monkeypatch, ACEConnection, {"code": 0, "result": {"model": "x"}})
        main = threading.get_ident()
        probe: Dict[str, int] = {}

        def resolve(ace_index: int, baud: int, settle: float = 6.0,
                    logger: Any = None) -> str:
            """A slow autodetect that logs what it found."""
            probe["thread"] = threading.get_ident()
            logger.info("ACE autodetect: /dev/ttyACM7 reports id 1")
            time.sleep(0.3)
            return "/dev/ttyACM7"
        monkeypatch.setattr(afc_ace_module, "resolve_ace_port_by_index", resolve)

        conn.connect()

        assert conn.connected is True
        assert port.opened == ["/dev/ttyACM7"]
        assert conn.device_info == {"model": "x"}
        assert conn._resolved_port == "/dev/ttyACM7"
        assert afc_ace_module._ACE_CLAIMED_PORTS == {"/dev/ttyACM7"}
        assert probe["thread"] != main
        assert reactor.ticks >= 10
        # The worker's line is held and written afterwards, from the reactor thread.
        assert log.messages == [
            ("info", "ACE autodetect: /dev/ttyACM7 reports id 1"),
            ("info", "ACE serial connected: /dev/ttyACM7 @ 115200 (auto, ace_index 1)"),
            ("debug", "ACE TX: {'id': 0, 'method': 'get_info'}"),
            ("info", "ACE device info: {'model': 'x'}")]
        assert set(log.threads) == {main}
        assert [cb for cb, _ in reactor.timers] == [conn._heartbeat_tick]

    def test_already_connected_after_resolve_returns_early(self, monkeypatch):
        # A concurrent reconnect landed while this connect's port resolve was
        # still running off the reactor; this one must not re-open the port.
        conn, reactor, log, port = self._auto_conn(monkeypatch)

        def resolve(ace_index: int, baud: int, settle: float = 6.0,
                   logger: Any = None) -> str:
            """Another connect finishes while this autodetect "runs"."""
            conn._connected = True
            return "/dev/ttyACM7"
        monkeypatch.setattr(afc_ace_module, "resolve_ace_port_by_index", resolve)

        conn.connect()

        assert port.opened == []
        assert conn.device_info == {}

    def test_second_connect_while_one_waits_is_refused(self, monkeypatch):
        conn, reactor, log, port = self._auto_conn(
            monkeypatch, ACEConnection, {"code": 0, "result": {}})
        refused: List[str] = []

        def resolve(ace_index: int, baud: int, settle: float = 6.0,
                    logger: Any = None) -> str:
            """A slow autodetect."""
            time.sleep(0.3)
            return "/dev/ttyACM7"

        def try_again() -> None:
            """A second connect while the first one waits."""
            if not refused:
                with pytest.raises(ACESerialError) as excinfo:
                    conn.connect()
                refused.append(str(excinfo.value))
        monkeypatch.setattr(afc_ace_module, "resolve_ace_port_by_index", resolve)
        reactor.on_tick = try_again

        conn.connect()

        assert refused == ["ACE connect already in progress"]
        assert port.opened == ["/dev/ttyACM7"]
        assert conn.connected is True
        # The refused call logs nothing: only the one connect is narrated.
        assert log.messages == [
            ("info", "ACE serial connected: /dev/ttyACM7 @ 115200 (auto, ace_index 1)"),
            ("debug", "ACE TX: {'id': 0, 'method': 'get_info'}"),
            ("info", "ACE device info: {}")]

    def test_failed_autodetect_clears_the_flag_and_keeps_its_log(self, monkeypatch):
        conn, reactor, log, port = self._auto_conn(
            monkeypatch, ACEConnection, {"code": 0, "result": {}})

        def resolve(ace_index: int, baud: int, settle: float = 6.0,
                    logger: Any = None) -> None:
            """An autodetect that finds only another unit."""
            logger.info("ACE autodetect: /dev/ttyACM3 reports id 2")
            return None
        monkeypatch.setattr(afc_ace_module, "resolve_ace_port_by_index", resolve)

        with pytest.raises(ACESerialError, match=(
                r"^ACE autodetect: no unit reporting id 1 found \(serial_port: auto\)\. "
                r"Is it plugged in / powered\?$")):
            conn.connect()

        assert conn.connected is False
        assert conn._connecting is False
        assert port.opened == []
        assert log.messages == [("info", "ACE autodetect: /dev/ttyACM3 reports id 2")]
        # The next attempt is not refused as already in progress.
        monkeypatch.setattr(afc_ace_module, "resolve_ace_port_by_index",
                            lambda *args, **kwargs: "/dev/ttyACM3")
        conn.connect()
        assert conn.connected is True
        assert port.opened == ["/dev/ttyACM3"]

    def test_ace2_connect_resolves_off_reactor(self, monkeypatch):
        # ACE2Connection resolves through its own probe but connects the same way.
        conn, reactor, log, port = self._auto_conn(
            monkeypatch, ACE2Connection, {"code": 0, "result": {"model": "ACE 2 Pro"}})
        main = threading.get_ident()
        probe: Dict[str, int] = {}

        def resolve(ace_index: int, baud: int, ace_uid: Any = None, settle: float = 6.0,
                    logger: Any = None) -> str:
            """A slow ACE 2 autodetect that logs what it found."""
            probe["thread"] = threading.get_ident()
            logger.info("ACE2 autodetect: /dev/ttyACM2 -> uid (1, 2, 3)")
            time.sleep(0.3)
            return "/dev/ttyACM2"
        monkeypatch.setattr(afc_ace2_module, "resolve_ace2_port", resolve)

        conn.connect()

        assert conn.connected is True
        assert port.opened == ["/dev/ttyACM2"]
        assert probe["thread"] != main
        assert reactor.ticks >= 10
        assert log.messages == [
            ("info", "ACE2 autodetect: /dev/ttyACM2 -> uid (1, 2, 3)"),
            ("info", "ACE serial connected: /dev/ttyACM2 @ 115200 (auto, ace_index 1)"),
            ("debug", "ACE2 TX: id=0 discover_device {}"),
            ("debug", "ACE2 TX: id=1 get_info {}"),
            ("info", "ACE device info: {'model': 'ACE 2 Pro'}")]
        assert set(log.threads) == {main}

    def test_already_connected_noop(self, monkeypatch):
        conn = ace_p2_connection()
        serial = conn._serial
        # pyserial is not even imported for a link that is up.
        monkeypatch.setitem(sys.modules, "serial", None)

        conn.connect()

        assert conn._serial is serial
        assert conn._logger.messages == []

    def test_missing_pyserial_raises(self, monkeypatch):
        conn = ace_p2_connection(connected=False)
        monkeypatch.setitem(sys.modules, "serial", None)

        with pytest.raises(ACESerialError, match=(
                r"^pyserial not installed\. Install with: pip install pyserial$")):
            conn.connect()

        assert conn.connected is False
        assert conn._connecting is False
        assert conn._logger.messages == []

    def test_success_opens_and_starts_heartbeat(self, monkeypatch):
        conn = ace_p2_connection(connected=False)
        conn._reconnect_backoff = 20.0
        port = AceP2SerialPort({"code": 0, "result": {"fw": "1.0"}})
        port.conn = conn
        monkeypatch.setitem(sys.modules, "serial", port)
        reactor = conn._reactor

        conn.connect()

        assert conn.connected is True
        assert port.opened == ["/dev/ttyACM0"]
        assert conn._serial is port.ports[0]
        assert conn.device_info == {"fw": "1.0"}
        assert conn._reconnect_backoff == 5.0
        assert reactor.fds == [(3, conn._handle_read)]
        assert conn._fd_handle == (3, conn._handle_read)
        assert [(t.callback, t.waketime) for t in reactor.timers] == [
            (conn._heartbeat_tick, 102.0)]
        assert conn._last_rx_time == 100.0
        assert conn._logger.messages == [
            ("info", "ACE serial connected: /dev/ttyACM0 @ 115200"),
            ("debug", "ACE TX: {'id': 0, 'method': 'get_info'}"),
            ("info", "ACE device info: {'fw': '1.0'}")]

    def test_unanswered_get_info_is_not_fatal(self, monkeypatch):
        conn = ace_p2_connection(connected=False)
        port = AceP2SerialPort()
        port.conn = conn
        monkeypatch.setitem(sys.modules, "serial", port)

        conn.connect()

        assert conn.connected is True
        assert conn.device_info == {}
        assert conn._logger.messages == [
            ("info", "ACE serial connected: /dev/ttyACM0 @ 115200"),
            ("debug", "ACE TX: {'id': 0, 'method': 'get_info'}"),
            ("debug", "ACE get_info failed (non-fatal): ACE command 'get_info' (id=0) "
                      "timed out after 3.0s")]

    def test_port_that_will_not_open_raises(self, monkeypatch):
        conn = ace_p2_connection(connected=False)
        monkeypatch.setitem(sys.modules, "serial", AceP2SerialPort(open_error=OSError("busy")))

        with pytest.raises(ACESerialError, match=(
                r"^Failed to open ACE serial port /dev/ttyACM0: busy$")):
            conn.connect()

        assert conn.connected is False
        assert conn._resolved_port is None
        assert conn._logger.messages == []


class TestACEConnectionDisconnect:
    @pytest.fixture(autouse=True)
    def _isolate(self) -> Iterator[None]:
        """Clear the claimed-port set around each test."""
        reset_ace_globals()
        yield
        reset_ace_globals()

    def test_closes_and_fails_pending(self):
        conn = ace_p2_connection()
        reactor = conn._reactor
        serial = conn._serial
        pending = reactor.completion()
        conn._pending[5] = pending
        conn._pending_cmd[5] = 1
        conn._async_ids.append(6)
        conn._read_buffer = b"\xff\xaa"
        conn._fd_handle = ("fd", 3)
        conn._resolved_port = "/dev/ttyACM0"
        afc_ace_module._ACE_CLAIMED_PORTS.add("/dev/ttyACM0")
        conn._start_heartbeat()
        heartbeat = conn._heartbeat_timer

        conn.disconnect()

        assert conn._connected is False
        assert serial.closed is True
        assert conn._serial is None
        assert conn._fd_handle is None
        assert reactor.unregister_fd.calls == [((("fd", 3),), {})]
        assert conn._heartbeat_timer is None
        assert reactor.unregister_timer.calls == [((heartbeat,), {})]
        # A waiting request wakes with no reply.
        assert (pending.done, pending.result) == (True, None)
        assert (conn._pending, conn._pending_cmd, list(conn._async_ids)) == ({}, {}, [])
        assert conn._read_buffer == b""
        assert afc_ace_module._ACE_CLAIMED_PORTS == set()
        assert conn._logger.messages == [("info", "ACE serial disconnected")]

    def test_down_link_logs_nothing(self):
        conn = ace_p2_connection(connected=False)

        conn.disconnect()

        assert conn._connected is False
        assert conn._logger.messages == []

    def test_unregister_fd_exception_is_swallowed(self):
        conn = ace_p2_connection()
        conn._fd_handle = ("fd", 3)
        conn._reactor.unregister_fd.raises = RuntimeError("boom")

        conn.disconnect()

        assert conn._fd_handle is None
        assert conn._connected is False

    def test_serial_close_exception_is_swallowed(self):
        conn = ace_p2_connection()
        conn._serial.close = Recorder(raises=RuntimeError("boom"))

        conn.disconnect()

        assert conn._serial is None
        assert conn._connected is False

    def test_pending_completion_exception_is_swallowed(self):
        conn = ace_p2_connection()
        pending = conn._reactor.completion()
        pending.complete = Recorder(raises=RuntimeError("boom"))
        conn._pending[5] = pending

        conn.disconnect()

        assert conn._pending == {}


class TestACEConnectionReconnect:
    _SCHEDULED = ("info", "ACE scheduling reconnect in 5s (next backoff: 8s)")

    def test_disabled_noop(self):
        conn = ace_p2_connection()

        conn.reconnect()

        assert conn._connected is True
        assert conn._reactor.timers == []
        assert conn._reconnect_backoff == 5.0
        assert conn._logger.messages == []

    def test_schedules_timer_with_backoff(self):
        conn = ace_p2_connection(reconnect_enabled=True)

        conn.reconnect()

        assert conn._connected is False
        assert [t.waketime for t in conn._reactor.timers] == [105.0]
        assert conn._reconnect_timer is conn._reactor.timers[0]
        # 5s now, then 5 * 1.5 next time.
        assert conn._reconnect_backoff == 7.5
        assert conn._logger.messages == [("info", "ACE serial disconnected"), self._SCHEDULED]

    def test_backoff_is_capped(self):
        conn = ace_p2_connection(reconnect_enabled=True)
        conn._reconnect_backoff = 25.0

        conn.reconnect()

        assert conn._reconnect_backoff == 30.0
        assert [t.waketime for t in conn._reactor.timers] == [125.0]
        assert conn._logger.messages == [
            ("info", "ACE serial disconnected"),
            ("info", "ACE scheduling reconnect in 25s (next backoff: 30s)")]

    def test_reconnect_callback_success_fires_hook(self, monkeypatch):
        conn = ace_p2_connection(reconnect_enabled=True)
        hook = Recorder()
        conn.reconnect_callback = hook
        port = AceP2SerialPort({"code": 0, "result": {}})
        port.conn = conn
        monkeypatch.setitem(sys.modules, "serial", port)
        conn.reconnect()
        timer = conn._reconnect_timer

        conn._reactor.advance(5.0)

        assert conn._connected is True
        assert hook.calls == [((), {})]
        # Done: the timer is parked and the backoff is back to its minimum.
        assert timer.waketime == conn._reactor.NEVER
        assert conn._reconnect_backoff == 5.0
        assert conn._logger.messages == [
            ("info", "ACE serial disconnected"), self._SCHEDULED,
            ("info", "ACE serial connected: /dev/ttyACM0 @ 115200"),
            ("debug", "ACE TX: {'id': 0, 'method': 'get_info'}"),
            ("info", "ACE device info: {}"),
            ("info", "ACE reconnected successfully")]

    def test_reconnect_callback_failure_reschedules(self, monkeypatch):
        conn = ace_p2_connection(reconnect_enabled=True)
        monkeypatch.setitem(sys.modules, "serial", None)
        conn.reconnect()
        timer = conn._reconnect_timer

        conn._reactor.advance(5.0)

        assert conn._connected is False
        # Retried 7.5s after the failed attempt, the backoff grown again.
        assert timer.waketime == 112.5
        assert conn._reconnect_backoff == 11.25
        assert conn._logger.messages == [
            ("info", "ACE serial disconnected"), self._SCHEDULED,
            ("warning", "ACE reconnect failed: pyserial not installed. Install with: "
                        "pip install pyserial")]

    def test_reconnect_callback_success_without_hook(self, monkeypatch):
        conn = ace_p2_connection(reconnect_enabled=True)
        port = AceP2SerialPort({"code": 0, "result": {}})
        port.conn = conn
        monkeypatch.setitem(sys.modules, "serial", port)
        conn.reconnect()

        conn._reactor.advance(5.0)

        assert conn.reconnect_callback is None
        assert conn._connected is True
        assert conn._logger.messages == [
            ("info", "ACE serial disconnected"), self._SCHEDULED,
            ("info", "ACE serial connected: /dev/ttyACM0 @ 115200"),
            ("debug", "ACE TX: {'id': 0, 'method': 'get_info'}"),
            ("info", "ACE device info: {}"),
            ("info", "ACE reconnected successfully")]

    def test_reconnect_callback_failure_is_logged(self, monkeypatch):
        conn = ace_p2_connection(reconnect_enabled=True)
        conn.reconnect_callback = Recorder(raises=RuntimeError("hook broke"))
        port = AceP2SerialPort({"code": 0, "result": {}})
        port.conn = conn
        monkeypatch.setitem(sys.modules, "serial", port)
        conn.reconnect()

        conn._reactor.advance(5.0)

        assert conn._connected is True
        assert conn._logger.messages == [
            ("info", "ACE serial disconnected"), self._SCHEDULED,
            ("info", "ACE serial connected: /dev/ttyACM0 @ 115200"),
            ("debug", "ACE TX: {'id': 0, 'method': 'get_info'}"),
            ("info", "ACE device info: {}"),
            ("info", "ACE reconnected successfully"),
            ("warning", "ACE reconnect callback failed: hook broke")]

    def test_disabled_before_the_timer_fires_is_a_noop(self):
        conn = ace_p2_connection(reconnect_enabled=True)
        conn.reconnect()
        conn._reconnect_enabled = False

        conn._reactor.advance(5.0)

        assert conn._connected is False
        assert conn._logger.messages == [
            ("info", "ACE serial disconnected"), self._SCHEDULED]

    def test_replacing_an_existing_timer_swallows_unregister_failure(self):
        conn = ace_p2_connection(reconnect_enabled=True)
        conn.reconnect()
        conn._reactor.unregister_timer.raises = RuntimeError("boom")

        conn.reconnect()

        # The old timer's unregister failed, so it is never removed; a second
        # one is still registered alongside it.
        assert len(conn._reactor.timers) == 2


class TestACEConnectionQuickReconnect:
    def test_quick_reconnect_schedules_fast_timer(self):
        conn = ace_p2_connection(reconnect_enabled=True)

        conn._quick_reconnect()

        assert conn._connected is False
        assert [t.waketime for t in conn._reactor.timers] == [100.5]
        assert conn._reconnect_timer is conn._reactor.timers[0]
        # No backoff for a USB idle drop.
        assert conn._reconnect_backoff == 5.0
        assert conn._logger.messages == [("info", "ACE serial disconnected")]

    def test_quick_reconnect_callback_success(self, monkeypatch):
        conn = ace_p2_connection(reconnect_enabled=True)
        hook = Recorder()
        conn.reconnect_callback = hook
        port = AceP2SerialPort({"code": 0, "result": {}})
        port.conn = conn
        monkeypatch.setitem(sys.modules, "serial", port)
        conn._quick_reconnect()
        timer = conn._reconnect_timer

        conn._reactor.advance(0.5)

        assert conn._connected is True
        assert hook.calls == [((), {})]
        assert timer.waketime == conn._reactor.NEVER
        assert conn._logger.messages == [
            ("info", "ACE serial disconnected"),
            ("info", "ACE serial connected: /dev/ttyACM0 @ 115200"),
            ("debug", "ACE TX: {'id': 0, 'method': 'get_info'}"),
            ("info", "ACE device info: {}"),
            ("debug", "ACE quick reconnect succeeded")]

    def test_quick_reconnect_success_without_hook(self, monkeypatch):
        conn = ace_p2_connection(reconnect_enabled=True)
        port = AceP2SerialPort({"code": 0, "result": {}})
        port.conn = conn
        monkeypatch.setitem(sys.modules, "serial", port)
        conn._quick_reconnect()
        timer = conn._reconnect_timer

        conn._reactor.advance(0.5)

        assert conn.reconnect_callback is None
        assert conn._connected is True
        assert port.opened == ["/dev/ttyACM0"]
        assert timer.waketime == conn._reactor.NEVER
        assert conn._logger.messages == [
            ("info", "ACE serial disconnected"),
            ("info", "ACE serial connected: /dev/ttyACM0 @ 115200"),
            ("debug", "ACE TX: {'id': 0, 'method': 'get_info'}"),
            ("info", "ACE device info: {}"),
            ("debug", "ACE quick reconnect succeeded")]

    def test_hook_failure_is_logged(self, monkeypatch):
        conn = ace_p2_connection(reconnect_enabled=True)
        conn.reconnect_callback = Recorder(raises=RuntimeError("hook broke"))
        port = AceP2SerialPort({"code": 0, "result": {}})
        port.conn = conn
        monkeypatch.setitem(sys.modules, "serial", port)
        conn._quick_reconnect()
        timer = conn._reconnect_timer

        conn._reactor.advance(0.5)

        assert conn._connected is True
        assert timer.waketime == conn._reactor.NEVER
        assert conn._logger.messages == [
            ("info", "ACE serial disconnected"),
            ("info", "ACE serial connected: /dev/ttyACM0 @ 115200"),
            ("debug", "ACE TX: {'id': 0, 'method': 'get_info'}"),
            ("info", "ACE device info: {}"),
            ("debug", "ACE quick reconnect succeeded"),
            ("warning", "ACE reconnect callback failed: hook broke")]

    def test_failure_falls_back_to_backoff_reconnect(self, monkeypatch):
        conn = ace_p2_connection(reconnect_enabled=True)
        monkeypatch.setitem(sys.modules, "serial", None)
        conn._quick_reconnect()
        quick = conn._reconnect_timer
        reactor = conn._reactor

        reactor.advance(0.5)

        assert conn._connected is False
        assert quick.waketime == reactor.NEVER
        # The quick timer is replaced by a backoff one, 5s after the failure.
        assert reactor.unregister_timer.calls == [((quick,), {})]
        assert [t.waketime for t in reactor.timers] == [105.5]
        assert conn._reconnect_timer is reactor.timers[0]
        assert conn._logger.messages == [
            ("info", "ACE serial disconnected"),
            ("warning", "ACE quick reconnect failed: pyserial not installed. Install with: "
                        "pip install pyserial"),
            ("info", "ACE scheduling reconnect in 5s (next backoff: 8s)")]

    def test_disabled_is_a_noop(self):
        conn = ace_p2_connection()

        conn._quick_reconnect()

        assert conn._connected is True
        assert conn._reactor.timers == []

    def test_disabled_before_the_timer_fires_is_a_noop(self):
        conn = ace_p2_connection(reconnect_enabled=True)
        conn._quick_reconnect()
        conn._reconnect_enabled = False

        conn._reactor.advance(0.5)

        assert conn._connected is False
        assert conn._logger.messages == [("info", "ACE serial disconnected")]

    def test_replacing_an_existing_timer_swallows_unregister_failure(self):
        conn = ace_p2_connection(reconnect_enabled=True)
        conn._quick_reconnect()
        conn._reactor.unregister_timer.raises = RuntimeError("boom")

        conn._quick_reconnect()

        assert len(conn._reactor.timers) == 2


class TestACEConnectionSendCommand:
    def test_not_connected_raises(self):
        conn = ace_p2_connection(connected=False)

        with pytest.raises(ACESerialError, match=r"^ACE not connected$"):
            conn.send_command("get_status")

        assert conn._next_request_id == 0
        assert conn._logger.messages == []

    def test_payload_too_large_raises(self):
        conn = ace_p2_connection()

        with pytest.raises(ACESerialError, match=r"^ACE payload too large \(2041 > 1024\)$"):
            conn.send_command("m", params={"big": "x" * 2000})

        assert conn._serial.frames == []
        assert conn._pending == {}
        assert conn._logger.messages == []

    def test_success_returns_result(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"ok": 1}})

        assert conn.send_command("get_status") == {"ok": 1}
        assert [f[4:-3] for f in conn._serial.frames] == [b'{"id":0,"method":"get_status"}']
        assert conn._pending == {}
        assert conn._next_request_id == 1
        assert conn._logger.messages == [("debug", "ACE TX: {'id': 0, 'method': 'get_status'}")]

    def test_params_are_sent(self):
        conn = ace_p2_connection(next_id=7)
        ace_p2_answer(conn, conn._serial, {"code": 0})

        # A reply with no result returns the whole reply.
        assert conn.send_command("feed_filament", {"index": 1}) == {"code": 0, "id": 7}
        assert [f[4:-3] for f in conn._serial.frames] == [
            b'{"id":7,"method":"feed_filament","params":{"index":1}}']
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 7, 'method': 'feed_filament', 'params': {'index': 1}}")]

    def test_error_code_raises(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 2, "msg": "error_2"})

        with pytest.raises(ACESerialError, match=(
                r"^ACE command 'start_feed_assist' failed: code=2, msg=error_2$")):
            conn.send_command("start_feed_assist")
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'start_feed_assist'}")]

    def test_forbidden_with_code_zero_raises(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "msg": "forbidden"})

        with pytest.raises(ACESerialError, match=(
                r"^ACE command 'feed_filament' failed: code=0, msg=forbidden$")):
            conn.send_command("feed_filament")
        assert conn._logger.messages == [("debug", "ACE TX: {'id': 0, 'method': 'feed_filament'}")]

    def test_error_without_message_says_unknown(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 3})

        with pytest.raises(ACESerialError, match=(
                r"^ACE command 'get_info' failed: code=3, msg=unknown error$")):
            conn.send_command("get_info")
        assert conn._logger.messages == [("debug", "ACE TX: {'id': 0, 'method': 'get_info'}")]

    def test_timeout_raises(self):
        conn = ace_p2_connection()

        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'get_status' \(id=0\) timed out after 5\.0s$")):
            conn.send_command("get_status")

        assert conn._timeout_timestamps == [105.0]
        assert conn._pending == {}
        assert conn._logger.messages == [("debug", "ACE TX: {'id': 0, 'method': 'get_status'}")]

    def test_write_failure_reconnects(self):
        conn = ace_p2_connection(serial=FakeSerial(write_error=OSError("cable")),
                                 reconnect_enabled=True)

        with pytest.raises(ACESerialError, match=r"^ACE write failed: cable$"):
            conn.send_command("get_status")

        assert conn._connected is False
        assert conn._pending == {}
        assert conn._timeout_timestamps == [100.0]
        assert conn._logger.messages == [
            ("info", "ACE serial disconnected"),
            ("info", "ACE scheduling reconnect in 5s (next backoff: 8s)")]

    def test_non_dict_result_is_returned_as_is(self):
        # _handle_response only ever completes a pending request with a dict
        # (or disconnect() with None); this exercises the defensive fallback
        # for any other value a completion could carry.
        conn = ace_p2_connection()

        def respond(frame: bytes) -> None:
            """Complete the one pending request with a non-dict result."""
            next(iter(conn._pending.values())).complete("pong")
        conn._serial.on_write = respond

        assert conn.send_command("get_status") == "pong"


class TestACEConnectionSendCommandAsync:
    def test_not_connected_noop(self):
        conn = ace_p2_connection(connected=False)

        conn.send_command_async("get_status")

        assert conn._next_request_id == 0
        assert list(conn._async_ids) == []
        assert conn._logger.messages == []

    def test_success_records_async_id(self):
        conn = ace_p2_connection()

        conn.send_command_async("get_status")

        assert list(conn._async_ids) == [0]
        assert [f[4:-3] for f in conn._serial.frames] == [b'{"id":0,"method":"get_status"}']
        assert conn._logger.messages == [
            ("debug", "ACE TX (async): {'id': 0, 'method': 'get_status'}")]

    def test_params_are_sent(self):
        conn = ace_p2_connection()

        conn.send_command_async("stop_feed_filament", {"index": 2})

        assert [f[4:-3] for f in conn._serial.frames] == [
            b'{"id":0,"method":"stop_feed_filament","params":{"index":2}}']
        assert conn._logger.messages == [
            ("debug", "ACE TX (async): {'id': 0, 'method': 'stop_feed_filament', "
                      "'params': {'index': 2}}")]

    def test_write_failure_reconnects(self):
        conn = ace_p2_connection(serial=FakeSerial(write_error=OSError("x")),
                                 reconnect_enabled=True)

        conn.send_command_async("get_status")

        assert conn._connected is False
        assert conn._logger.messages == [
            ("debug", "ACE async write failed: x"),
            ("info", "ACE serial disconnected"),
            ("info", "ACE scheduling reconnect in 5s (next backoff: 8s)")]


class TestACEConnectionStartHeartbeat:
    def test_start_registers_once(self):
        conn = ace_p2_connection()
        reactor = conn._reactor

        conn._start_heartbeat()
        first = conn._heartbeat_timer
        conn._start_heartbeat()

        assert conn._heartbeat_timer is first
        assert [(t.callback, t.waketime) for t in reactor.timers] == [
            (conn._heartbeat_tick, 102.0)]
        assert conn._logger.messages == []


class TestACEConnectionStopHeartbeat:
    def test_stop_unregisters(self):
        conn = ace_p2_connection()
        conn._start_heartbeat()
        timer = conn._heartbeat_timer

        conn._stop_heartbeat()

        assert conn._heartbeat_timer is None
        assert conn._reactor.timers == []
        assert conn._reactor.unregister_timer.calls == [((timer,), {})]
        assert conn._logger.messages == []

    def test_stop_without_heartbeat_is_noop(self):
        conn = ace_p2_connection()

        conn._stop_heartbeat()

        assert conn._reactor.unregister_timer.calls == []
        assert conn._logger.messages == []

    def test_unregister_exception_is_swallowed(self):
        conn = ace_p2_connection()
        conn._start_heartbeat()
        conn._reactor.unregister_timer.raises = RuntimeError("boom")

        conn._stop_heartbeat()

        assert conn._heartbeat_timer is None


class TestACEConnectionHeartbeatTick:
    def test_tick_sends_and_reschedules(self):
        conn = ace_p2_connection()
        conn._last_rx_time = 95.0

        assert conn._heartbeat_tick(100.0) == 102.0
        assert [f[4:-3] for f in conn._serial.frames] == [b'{"id":0,"method":"get_status"}']
        # The health check ran too.
        assert conn._last_supervision_check == 100.0
        assert conn._logger.messages == [
            ("debug", "ACE TX (async): {'id': 0, 'method': 'get_status'}")]

    def test_tick_reconnects_on_silence(self):
        conn = ace_p2_connection(reconnect_enabled=True)
        conn._last_rx_time = 0.0

        assert conn._heartbeat_tick(100.0) == conn._reactor.NEVER
        assert conn._connected is False
        assert conn._serial is None
        assert conn._logger.messages == [
            ("warning", "ACE no data received for 100s, reconnecting"),
            ("info", "ACE serial disconnected"),
            ("info", "ACE scheduling reconnect in 5s (next backoff: 8s)")]

    def test_tick_disconnected_returns_never(self):
        conn = ace_p2_connection(connected=False)

        assert conn._heartbeat_tick(0.0) == conn._reactor.NEVER
        assert conn._last_supervision_check == 0.0
        assert conn._logger.messages == []

    def test_async_send_exception_is_swallowed(self):
        conn = ace_p2_connection()
        conn._last_rx_time = 95.0
        conn.send_command_async = Recorder(raises=RuntimeError("boom"))

        assert conn._heartbeat_tick(100.0) == 102.0
        assert conn._last_supervision_check == 100.0

    def test_poll_extras_exception_is_swallowed(self):
        conn = ace_p2_connection()
        conn._last_rx_time = 95.0
        conn._poll_extras = Recorder(raises=RuntimeError("boom"))

        assert conn._heartbeat_tick(100.0) == 102.0
        assert conn._last_supervision_check == 100.0


class TestACEConnectionPollExtras:
    def test_base_is_noop(self):
        # The V1 firmware has no get_temp: nothing is sent.
        conn = ace_p2_connection()

        assert conn._poll_extras() is None
        assert conn._serial.frames == []
        assert conn._next_request_id == 0
        assert list(conn._async_ids) == []
        assert conn._logger.messages == []


class TestACEConnectionTrackTimeout:
    def test_track_timeout_prunes_old(self):
        conn = ace_p2_connection()
        # 30s window: 10.0 has aged out, 75.0 is still inside it.
        conn._timeout_timestamps = [10.0, 75.0]

        conn._track_timeout()

        assert conn._timeout_timestamps == [75.0, 100.0]
        assert conn._logger.messages == []


class TestACEConnectionSupervisionCheck:
    @staticmethod
    def _conn(timeouts: int, unsolicited: int) -> ACEConnection:
        """
        :param timeouts: timeouts seen a second ago
        :param unsolicited: unsolicited replies seen a second ago
        :return ACEConnection: a connection due its health check
        """
        conn = ace_p2_connection(reconnect_enabled=True)
        conn._timeout_timestamps = [99.0] * timeouts
        conn._unsolicited_timestamps = [99.0] * unsolicited
        return conn

    def test_supervision_forces_reconnect_when_unhealthy(self):
        conn = self._conn(15, 15)
        conn._timeout_timestamps.append(60.0)

        conn._supervision_check()

        assert conn._last_supervision_check == 100.0
        assert conn._connected is False
        assert (conn._timeout_timestamps, conn._unsolicited_timestamps) == ([], [])
        assert conn._logger.messages == [
            ("warning", "ACE communication unhealthy: 15 timeouts + 15 unsolicited in "
                        "30.0s, forcing reconnect"),
            ("info", "ACE serial disconnected"),
            ("info", "ACE scheduling reconnect in 5s (next backoff: 8s)")]

    def test_timeouts_alone_do_not_reconnect(self):
        conn = self._conn(15, 14)

        conn._supervision_check()

        assert conn._connected is True
        assert len(conn._timeout_timestamps) == 15
        assert conn._logger.messages == []

    def test_unsolicited_alone_do_not_reconnect(self):
        conn = self._conn(14, 15)

        conn._supervision_check()

        assert conn._connected is True
        assert conn._logger.messages == []

    def test_supervision_skips_when_recent(self):
        conn = self._conn(15, 15)
        conn._last_supervision_check = 95.0

        conn._supervision_check()

        assert conn._last_supervision_check == 95.0
        assert conn._connected is True
        assert len(conn._timeout_timestamps) == 15
        assert conn._logger.messages == []

    def test_down_link_is_not_checked(self):
        conn = self._conn(15, 15)
        conn._connected = False

        conn._supervision_check()

        assert conn._last_supervision_check == 100.0
        assert len(conn._timeout_timestamps) == 15
        assert conn._logger.messages == []


class TestACEConnectionBuildFrame:
    def test_frame_layout(self):
        conn = ace_p2_connection(connected=False)

        # Header, little-endian length, payload, the published check CRC
        # (0x6F91) little-endian, footer.
        assert conn._build_frame(b"123456789") == (
            b"\xff\xaa\x09\x00" + b"123456789" + b"\x91\x6f" + b"\xfe")


class TestACEConnectionHandleRead:
    class _FailingSerial(FakeSerial):
        """A port whose read raises."""

        def __init__(self, error: BaseException) -> None:
            """
            :param error: raised by read()
            """
            super().__init__()
            self.error = error

        def read(self, size: int = 1) -> bytes:
            """
            :param size: unused
            :return bytes: never returns
            """
            raise self.error

    def test_no_serial_returns_before_reading(self):
        conn = ace_p2_connection()
        conn._last_rx_time = 50.0
        conn._serial = None

        conn._handle_read(101.0)

        assert conn._read_buffer == b""
        assert conn._last_rx_time == 50.0
        assert conn._logger.messages == []

    def test_no_data_returns(self):
        conn = ace_p2_connection()
        conn._last_rx_time = 50.0

        conn._handle_read(101.0)

        assert conn._read_buffer == b""
        assert conn._last_rx_time == 50.0
        assert conn._logger.messages == []

    def test_data_buffered(self):
        conn = ace_p2_connection()
        conn._serial.rx.extend(b"\xff\xaa\x02")

        conn._handle_read(101.0)

        assert conn._last_rx_time == 101.0
        # Too short to parse yet, so it waits in the buffer.
        assert conn._read_buffer == b"\xff\xaa\x02"
        assert conn._logger.messages == []

    def test_read_error_reconnects(self):
        conn = ace_p2_connection(serial=self._FailingSerial(OSError("device error")),
                                 reconnect_enabled=True)

        conn._handle_read(101.0)

        assert conn._connected is False
        assert [t.waketime for t in conn._reactor.timers] == [105.0]
        assert conn._logger.messages == [
            ("error", "ACE read error: device error"),
            ("info", "ACE serial disconnected"),
            ("info", "ACE scheduling reconnect in 5s (next backoff: 8s)")]

    def test_usb_autosuspend_quick_reconnects(self):
        conn = ace_p2_connection(serial=self._FailingSerial(OSError(
            "device reports readiness to read but returned no data")), reconnect_enabled=True)
        conn._reconnect_backoff = 20.0

        conn._handle_read(101.0)

        assert conn._connected is False
        # Quick reconnect: backoff reset and a 0.5s timer.
        assert conn._reconnect_backoff == 5.0
        assert [t.waketime for t in conn._reactor.timers] == [100.5]
        assert conn._logger.messages == [
            ("debug", "ACE USB idle disconnect detected, quick reconnect"),
            ("info", "ACE serial disconnected")]

    def test_device_disconnected_quick_reconnects(self):
        conn = ace_p2_connection(serial=self._FailingSerial(OSError("device disconnected")),
                                 reconnect_enabled=True)

        conn._handle_read(101.0)

        assert [t.waketime for t in conn._reactor.timers] == [100.5]
        assert conn._logger.messages == [
            ("debug", "ACE USB idle disconnect detected, quick reconnect"),
            ("info", "ACE serial disconnected")]


class TestACEConnectionParseFrames:
    @staticmethod
    def _frame(payload: bytes) -> bytes:
        """
        Frame a payload as the unit sends it: header, length, payload, CRC, footer.

        :param payload: JSON bytes
        :return bytes: the frame
        """
        return (b"\xff\xaa" + struct.pack("<H", len(payload)) + payload
                + struct.pack("<H", crc16_ccitt_reflected(payload)) + b"\xfe")

    def test_no_header_clears_buffer(self):
        conn = ace_p2_connection()
        conn._read_buffer = b"garbage"

        conn._parse_frames()

        assert conn._read_buffer == b""
        assert conn._logger.messages == []

    def test_partial_frame_waits(self):
        conn = ace_p2_connection()
        conn._read_buffer = b"\xff\xaa\x02"

        conn._parse_frames()

        assert conn._read_buffer == b"\xff\xaa\x02"
        assert conn._logger.messages == []

    def test_frame_shorter_than_its_length_waits(self):
        conn = ace_p2_connection()
        conn._read_buffer = self._frame(b'{"result":1}')[:-1]

        conn._parse_frames()

        assert conn._read_buffer == self._frame(b'{"result":1}')[:-1]
        assert conn._logger.messages == []

    def test_valid_frame_routes_to_callback(self):
        conn = ace_p2_connection()
        received: List[Dict[str, Any]] = []
        conn.status_callback = received.append
        # Noise ahead of the header is dropped.
        conn._read_buffer = b"\x00\x01" + self._frame(b'{"result":1}')

        conn._parse_frames()

        assert received == [{"result": 1}]
        assert conn._read_buffer == b""
        assert conn._logger.messages == [("debug", "ACE RX: {'result': 1}")]

    def test_footer_mismatch_rescans(self):
        conn = ace_p2_connection()
        frame = bytearray(self._frame(b'{"a":1}'))
        frame[-1] = 0x00
        conn._read_buffer = bytes(frame)

        conn._parse_frames()

        # Past the bad header there is no other header to find.
        assert conn._read_buffer == b""
        assert conn._logger.messages == [
            ("debug", "ACE frame: invalid footer byte 0x00, rescanning for next header")]

    def test_crc_mismatch_rescans(self):
        conn = ace_p2_connection()
        received: List[Dict[str, Any]] = []
        conn.status_callback = received.append
        conn._read_buffer = b"\xff\xaa\x02\x00{}\x34\x12\xfe"

        conn._parse_frames()

        assert received == []
        assert conn._read_buffer == b""
        # 0x4cb6 is the CRC-16/MCRF4XX of b"{}".
        assert conn._logger.messages == [
            ("debug", "ACE frame: CRC mismatch (recv=0x1234, calc=0x4cb6), rescanning")]

    def test_oversized_length_skips_false_header(self):
        conn = ace_p2_connection()
        conn._read_buffer = b"\xff\xaa\xff\xff" + b"\x00" * 4

        conn._parse_frames()

        assert conn._read_buffer == b""
        assert conn._logger.messages == [
            ("debug", "ACE frame: payload length 65535 exceeds max, skipping false header")]

    def test_bad_json_warns(self):
        conn = ace_p2_connection()
        received: List[Dict[str, Any]] = []
        conn.status_callback = received.append
        conn._read_buffer = self._frame(b"not json")

        conn._parse_frames()

        assert received == []
        assert conn._read_buffer == b""
        assert conn._logger.messages == [
            ("warning", "ACE frame: JSON parse error: Expecting value: line 1 column 1 "
                        "(char 0)")]


class TestACEConnectionResponseMatchesPending:
    def test_base_always_true(self):
        conn = ace_p2_connection()

        # V1 ids never recycle, so even an id with no pending request matches.
        assert conn._response_matches_pending(1, {"id": 1}) is True
        assert conn._response_matches_pending(2, {"id": 2, "_cmd": 99}) is True


class TestACEConnectionHandleResponse:
    def test_unsolicited_forwarded(self):
        conn = ace_p2_connection()
        callback = Recorder()
        conn.status_callback = callback

        conn._handle_response({"result": 1})

        assert callback.calls == [(({"result": 1},), {})]
        assert conn._unsolicited_timestamps == [100.0]
        assert conn._logger.messages == []

    def test_pending_completed(self):
        conn = ace_p2_connection()
        callback = Recorder()
        conn.status_callback = callback
        completion = conn._reactor.completion()
        conn._pending[7] = completion

        conn._handle_response({"id": 7, "result": 1})

        assert (completion.done, completion.result) == (True, {"id": 7, "result": 1})
        # A reply to a waiting request goes to the waiter only.
        assert callback.calls == []
        assert conn._unsolicited_timestamps == []
        assert conn._logger.messages == []

    def test_async_id_not_counted_unsolicited(self):
        conn = ace_p2_connection()
        conn._async_ids.extend([8, 9])
        callback = Recorder()
        conn.status_callback = callback

        conn._handle_response({"id": 9, "result": 2})

        assert callback.calls == [(({"id": 9, "result": 2},), {})]
        assert conn._unsolicited_timestamps == []
        assert list(conn._async_ids) == [8]
        assert conn._logger.messages == []

    def test_unknown_id_counted_and_logged(self):
        conn = ace_p2_connection()

        conn._handle_response({"id": 999})

        assert conn._unsolicited_timestamps == [100.0]
        assert conn._logger.messages == [("debug", "ACE response for unknown request id=999")]

    def test_unsolicited_without_a_callback_is_a_noop(self):
        conn = ace_p2_connection()

        conn._handle_response({"result": 1})

        assert conn._unsolicited_timestamps == [100.0]
        assert conn._logger.messages == []

    def test_unsolicited_callback_exception_is_logged(self):
        conn = ace_p2_connection()
        conn.status_callback = Recorder(raises=RuntimeError("boom"))

        conn._handle_response({"result": 1})

        assert len(conn._logger.messages) == 1
        level, msg = conn._logger.messages[0]
        assert level == "error"
        assert msg.startswith("ACE status_callback error: boom\n")

    def test_mismatched_pending_reply_is_dropped(self):
        conn = ace_p2_connection()
        completion = conn._reactor.completion()
        conn._pending[7] = completion
        conn._response_matches_pending = Recorder(result=False)

        conn._handle_response({"id": 7, "result": 1})

        assert completion.done is False
        assert conn._logger.messages == [("debug", "ACE dropping mismatched reply for id=7")]

    def test_pending_completion_exception_is_swallowed(self):
        conn = ace_p2_connection()
        completion = conn._reactor.completion()
        completion.complete = Recorder(raises=RuntimeError("boom"))
        conn._pending[7] = completion

        conn._handle_response({"id": 7, "result": 1})

        assert conn._logger.messages == []

    def test_async_reply_callback_exception_is_logged(self):
        conn = ace_p2_connection()
        conn._async_ids.append(9)
        conn.status_callback = Recorder(raises=RuntimeError("boom"))

        conn._handle_response({"id": 9, "result": 1})

        assert len(conn._logger.messages) == 1
        level, msg = conn._logger.messages[0]
        assert level == "error"
        assert msg.startswith("ACE status_callback error: boom\n")


class TestACEConnectionGetStatus:
    def test_get_status(self):
        conn = ace_p2_connection(next_id=4)
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"status": "ready"}})

        assert conn.get_status() == {"status": "ready"}
        assert ace_p2_payloads(conn) == [b'{"id":4,"method":"get_status"}']
        assert conn._logger.messages == [("debug", "ACE TX: {'id': 4, 'method': 'get_status'}")]

    def test_get_status_waits_the_given_timeout(self):
        conn = ace_p2_connection()

        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'get_status' \(id=0\) timed out after 5\.0s$")):
            conn.get_status(timeout=5.0)

        assert conn._timeout_timestamps == [105.0]
        assert conn._logger.messages == [("debug", "ACE TX: {'id': 0, 'method': 'get_status'}")]

    def test_get_status_times_out_after_3s(self):
        conn = ace_p2_connection()

        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'get_status' \(id=0\) timed out after 3\.0s$")):
            conn.get_status()

        assert conn._timeout_timestamps == [103.0]
        assert conn._logger.messages == [("debug", "ACE TX: {'id': 0, 'method': 'get_status'}")]


class TestACEConnectionGetTemp:
    def test_get_temp(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"box": 31.5}})

        assert conn.get_temp() == {"box": 31.5}
        assert ace_p2_payloads(conn) == [b'{"id":0,"method":"get_temp"}']
        assert conn._logger.messages == [("debug", "ACE TX: {'id': 0, 'method': 'get_temp'}")]

    def test_get_temp_times_out_after_3s(self):
        conn = ace_p2_connection()

        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'get_temp' \(id=0\) timed out after 3\.0s$")):
            conn.get_temp()

        assert conn._timeout_timestamps == [103.0]
        assert conn._logger.messages == [("debug", "ACE TX: {'id': 0, 'method': 'get_temp'}")]


class TestACEConnectionGetFilamentInfo:
    def test_get_filament_info(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"sku": "HPL-1"}})

        assert conn.get_filament_info(2) == {"sku": "HPL-1"}
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"get_filament_info","params":{"index":2}}']
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'get_filament_info', "
                      "'params': {'index': 2}}")]

    def test_get_filament_info_times_out_after_3s(self):
        conn = ace_p2_connection()

        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'get_filament_info' \(id=0\) timed out after 3\.0s$")):
            conn.get_filament_info(2)

        assert conn._timeout_timestamps == [103.0]
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'get_filament_info', 'params': {'index': 2}}")]


class TestACEConnectionGetMaterialInfo:
    def test_get_material_info(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial,
                      {"code": 0, "result": {"index": 1, "material_name": "PETG"}})

        assert conn.get_material_info(1) == {"index": 1, "material_name": "PETG"}
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"get_material_info","params":{"index":1}}']
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'get_material_info', "
                      "'params': {'index': 1}}")]

    def test_get_material_info_times_out_after_3s(self):
        conn = ace_p2_connection()

        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'get_material_info' \(id=0\) timed out after 3\.0s$")):
            conn.get_material_info(1)

        assert conn._timeout_timestamps == [103.0]
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'get_material_info', 'params': {'index': 1}}")]


class TestACEConnectionSetMaterialName:
    def test_set_material_name(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"stored": True}})

        assert conn.set_material_name(0, "PLA") == {"stored": True}
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"set_material_name","params":{"index":0,"name":"PLA"}}']
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'set_material_name', "
                      "'params': {'index': 0, 'name': 'PLA'}}")]

    def test_set_material_name_times_out_after_3s(self):
        conn = ace_p2_connection()

        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'set_material_name' \(id=0\) timed out after 3\.0s$")):
            conn.set_material_name(0, "PLA")

        assert conn._timeout_timestamps == [103.0]
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'set_material_name', "
                      "'params': {'index': 0, 'name': 'PLA'}}")]


class TestACEConnectionGetSensorState:
    def test_get_sensor_state(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"sensor_bitmask": 5}})

        assert conn.get_sensor_state() == {"sensor_bitmask": 5}
        assert ace_p2_payloads(conn) == [b'{"id":0,"method":"get_sensor_state"}']
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'get_sensor_state'}")]

    def test_get_sensor_state_times_out_after_3s(self):
        conn = ace_p2_connection()

        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'get_sensor_state' \(id=0\) timed out after 3\.0s$")):
            conn.get_sensor_state()

        assert conn._timeout_timestamps == [103.0]
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'get_sensor_state'}")]


class TestACEConnectionFeedFilament:
    def test_feed_filament(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"accepted": 1}})

        assert conn.feed_filament(0, 100.0, 50.0) == {"accepted": 1}
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"feed_filament","params":'
            b'{"index":0,"length":100.0,"speed":50.0}}']
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'feed_filament', "
                      "'params': {'index': 0, 'length': 100.0, 'speed': 50.0}}")]

    def test_feed_waits_the_move_time_plus_10s(self):
        conn = ace_p2_connection()

        # 100mm at 50mm/s is a 2s move, so the request waits 12s.
        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'feed_filament' \(id=0\) timed out after 12\.0s$")):
            conn.feed_filament(0, 100.0, 50.0)

        assert conn._timeout_timestamps == [112.0]
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'feed_filament', "
                      "'params': {'index': 0, 'length': 100.0, 'speed': 50.0}}")]

    def test_feed_speed_below_one_counts_as_one(self):
        conn = ace_p2_connection()

        # 30mm at 0.5mm/s is costed at 1mm/s: 30s plus 10s, not 70s.
        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'feed_filament' \(id=0\) timed out after 40\.0s$")):
            conn.feed_filament(1, 30.0, 0.5)

        assert conn._timeout_timestamps == [140.0]
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'feed_filament', "
                      "'params': {'index': 1, 'length': 30.0, 'speed': 0.5}}")]


class TestACEConnectionStopFeedFilament:
    def test_stop_feed_filament(self):
        conn = ace_p2_connection()

        assert conn.stop_feed_filament(0) is None
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"stop_feed_filament","params":{"index":0}}']
        # Sent without waiting: its reply goes to the status callback.
        assert list(conn._async_ids) == [0]
        assert conn._pending == {}
        assert conn._logger.messages == [
            ("debug", "ACE TX (async): {'id': 0, 'method': 'stop_feed_filament', "
                      "'params': {'index': 0}}")]


class TestACEConnectionUnwindFilament:
    def test_unwind_filament(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"accepted": 1}})

        assert conn.unwind_filament(0, 100.0, 50.0) == {"accepted": 1}
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"unwind_filament","params":'
            b'{"index":0,"length":100.0,"speed":50.0,"mode":"normal"}}']
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'unwind_filament', "
                      "'params': {'index': 0, 'length': 100.0, 'speed': 50.0, "
                      "'mode': 'normal'}}")]

    def test_unwind_mode_is_sent(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {}})

        conn.unwind_filament(2, 40.0, 20.0, mode="fast")

        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"unwind_filament","params":'
            b'{"index":2,"length":40.0,"speed":20.0,"mode":"fast"}}']
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'unwind_filament', 'params': "
                      "{'index': 2, 'length': 40.0, 'speed': 20.0, 'mode': 'fast'}}")]

    def test_unwind_waits_the_move_time_plus_10s(self):
        conn = ace_p2_connection()

        # 100mm at 25mm/s is a 4s move, so the request waits 14s.
        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'unwind_filament' \(id=0\) timed out after 14\.0s$")):
            conn.unwind_filament(0, 100.0, 25.0)

        assert conn._timeout_timestamps == [114.0]
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'unwind_filament', 'params': "
                      "{'index': 0, 'length': 100.0, 'speed': 25.0, 'mode': 'normal'}}")]

    def test_unwind_speed_below_one_counts_as_one(self):
        conn = ace_p2_connection()

        # 20mm at 0.25mm/s is costed at 1mm/s: 20s plus 10s, not 90s.
        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'unwind_filament' \(id=0\) timed out after 30\.0s$")):
            conn.unwind_filament(3, 20.0, 0.25)

        assert conn._timeout_timestamps == [130.0]
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'unwind_filament', 'params': "
                      "{'index': 3, 'length': 20.0, 'speed': 0.25, 'mode': 'normal'}}")]


class TestACEConnectionStopUnwindFilament:
    def test_stop_unwind_filament(self):
        conn = ace_p2_connection()

        assert conn.stop_unwind_filament(1) is None
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"stop_unwind_filament","params":{"index":1}}']
        assert list(conn._async_ids) == [0]
        assert conn._logger.messages == [
            ("debug", "ACE TX (async): {'id': 0, 'method': 'stop_unwind_filament', "
                      "'params': {'index': 1}}")]


class TestACEConnectionStartFeedAssist:
    def test_start_feed_assist(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"assist": 3}})

        assert conn.start_feed_assist(3) == {"assist": 3}
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"start_feed_assist","params":{"index":3}}']
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'start_feed_assist', "
                      "'params': {'index': 3}}")]

    def test_start_feed_assist_times_out_after_2s(self):
        conn = ace_p2_connection()

        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'start_feed_assist' \(id=0\) timed out after 2\.0s$")):
            conn.start_feed_assist(3)

        assert conn._timeout_timestamps == [102.0]
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'start_feed_assist', 'params': {'index': 3}}")]


class TestACEConnectionStopFeedAssist:
    def test_stop_feed_assist(self):
        conn = ace_p2_connection()

        assert conn.stop_feed_assist(3) is None
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"stop_feed_assist","params":{"index":3}}']
        assert list(conn._async_ids) == [0]
        assert conn._pending == {}
        assert conn._logger.messages == [
            ("debug", "ACE TX (async): {'id': 0, 'method': 'stop_feed_assist', "
                      "'params': {'index': 3}}")]


class TestACEConnectionStopFeedAssistSync:
    def test_stop_feed_assist_sync(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"assist": None}})

        assert conn.stop_feed_assist_sync(3) == {"assist": None}
        # The same firmware method as the async stop, waited on.
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"stop_feed_assist","params":{"index":3}}']
        assert list(conn._async_ids) == []
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'stop_feed_assist', "
                      "'params': {'index': 3}}")]

    def test_stop_feed_assist_sync_times_out_after_2s(self):
        conn = ace_p2_connection()

        with pytest.raises(ACETimeoutError, match=(
                r"^ACE command 'stop_feed_assist' \(id=0\) timed out after 2\.0s$")):
            conn.stop_feed_assist_sync(3)

        assert conn._timeout_timestamps == [102.0]
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'stop_feed_assist', 'params': {'index': 3}}")]


class TestACEConnectionUpdateFeedingSpeed:
    def test_update_feeding_speed(self):
        conn = ace_p2_connection()

        assert conn.update_feeding_speed(0, 80.0) is None
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"update_feeding_speed","params":{"index":0,"speed":80.0}}']
        assert list(conn._async_ids) == [0]
        assert conn._logger.messages == [
            ("debug", "ACE TX (async): {'id': 0, 'method': 'update_feeding_speed', "
                      "'params': {'index': 0, 'speed': 80.0}}")]


class TestACEConnectionUpdateUnwindingSpeed:
    def test_update_unwinding_speed(self):
        conn = ace_p2_connection()

        assert conn.update_unwinding_speed(1, 60.0) is None
        # The firmware has one speed-update method; the slot knows its direction.
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"update_feeding_speed","params":{"index":1,"speed":60.0}}']
        assert list(conn._async_ids) == [0]
        assert conn._logger.messages == [
            ("debug", "ACE TX (async): {'id': 0, 'method': 'update_feeding_speed', "
                      "'params': {'index': 1, 'speed': 60.0}}")]


class TestACEConnectionStartDrying:
    def test_start_drying(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"drying": True}})

        assert conn.start_drying(50.0, 7000, 90.0) == {"drying": True}
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"drying","params":{"temp":50.0,"fan_speed":7000,'
            b'"duration":90.0}}']
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'drying', "
                      "'params': {'temp': 50.0, 'fan_speed': 7000, 'duration': 90.0}}")]


class TestACEConnectionStopDrying:
    def test_stop_drying(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"drying": False}})

        assert conn.stop_drying() == {"drying": False}
        assert ace_p2_payloads(conn) == [b'{"id":0,"method":"drying_stop"}']
        assert conn._logger.messages == [("debug", "ACE TX: {'id': 0, 'method': 'drying_stop'}")]


class TestACEConnectionEnableRfid:
    def test_enable_rfid(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"rfid": 1}})

        assert conn.enable_rfid() == {"rfid": 1}
        assert ace_p2_payloads(conn) == [b'{"id":0,"method":"enable_rfid"}']
        assert conn._logger.messages == [("debug", "ACE TX: {'id': 0, 'method': 'enable_rfid'}")]


class TestACEConnectionDisableRfid:
    def test_disable_rfid(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {"rfid": 0}})

        assert conn.disable_rfid() == {"rfid": 0}
        assert ace_p2_payloads(conn) == [b'{"id":0,"method":"disable_rfid"}']
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'disable_rfid'}")]


class TestACEConnectionSetFilamentInfo:
    def test_set_filament_info(self):
        conn = ace_p2_connection()
        ace_p2_answer(conn, conn._serial, {"code": 0, "result": {}})

        # A colour tuple goes out as a list.
        assert conn.set_filament_info(0, "PLA", (1, 2, 3)) == {}
        assert ace_p2_payloads(conn) == [
            b'{"id":0,"method":"set_filament_info","params":'
            b'{"index":0,"type":"PLA","color":[1,2,3]}}']
        assert conn._logger.messages == [
            ("debug", "ACE TX: {'id': 0, 'method': 'set_filament_info', "
                      "'params': {'index': 0, 'type': 'PLA', 'color': [1, 2, 3]}}")]


class TestAfcACEUsesFirmwareRfid:
    def test_v1_uses_firmware_rfid_true(self):
        # A V1 unit reads tags through its firmware, so the startup sweep asks it.
        unit = ace_p2_unit()
        unit._ace.set_reply("get_filament_info", "no tag")

        unit._sync_inventory()

        assert unit._uses_firmware_rfid is True
        assert unit._ace.commands == [("get_filament_info", {"index": 0}),
                                      ("get_filament_info", {"index": 1}),
                                      ("get_filament_info", {"index": 2}),
                                      ("get_filament_info", {"index": 3})]
        assert unit.printer.logger.messages == []

    def test_cleared_flag_skips_the_firmware(self):
        # The flag alone gates the sweep on an otherwise identical connected unit.
        unit = ace_p2_unit()
        unit._uses_firmware_rfid = False

        unit._sync_inventory()

        assert unit._ace.commands == []
        assert unit.printer.logger.messages == []


class TestLoadConfigPrefix:
    def test_constructs_the_unit_from_its_config_section(self):
        printer = make_ace_printer()
        config = AceConfig("AFC_ACE Ace_1", printer, {"serial_port": "/dev/ttyACM0"})

        unit = load_config_prefix(config)

        assert isinstance(unit, afcACE)
        assert unit.name == "Ace_1"
        assert unit.serial_port == "/dev/ttyACM0"
