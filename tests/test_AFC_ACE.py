"""
Unit tests for extras/AFC_ACE.py (Anycubic ACE Pro unit driver).

Covers:
  - frame building / CRC16-MCRF4XX and frame round-trip through the reader
  - stale-frame skipping and CRC rejection in _read_matching
  - USB topology port ordering
  - status parsing (_slot_info / _slot_present / _motion_active)
  - _stop_motion timeout-assumes-success behavior
  - _feed_until sensor homing flow (stop sent, sensor confirmed)
  - unit_load_lane slot-not-ready guard
  - system_Test spool present / empty paths
"""

from __future__ import annotations

from unittest.mock import MagicMock

from extras.AFC_ACE import (
    AceTransport,
    afcACE,
    _build_frame,
    _crc16_mcrf4xx,
    _usb_path_key,
)
from extras.AFC_lane import AFCLaneState
from tests.conftest import MockAFC, MockLogger, MockPrinter, MockReactor


# ── Helpers ───────────────────────────────────────────────────────────────────

class FakeSerial:
    """In-memory serial: read() serves a preloaded rx buffer."""

    def __init__(self, rx=b""):
        self.rx = bytearray(rx)
        self.tx = bytearray()
        self.is_open = True
        self.timeout = 0.1

    def read(self, count):
        chunk = bytes(self.rx[:count])
        del self.rx[:count]
        return chunk

    def write(self, data):
        self.tx.extend(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.rx.clear()

    def close(self):
        self.is_open = False


class SteppingReactor(MockReactor):
    """Reactor whose clock advances on every pause, so deadline loops end."""

    def pause(self, until):
        self._monotonic = max(self._monotonic + 0.1, until)


def _make_transport(rx=b"") -> AceTransport:
    transport = AceTransport("auto", 0, MockLogger())
    transport._ser = FakeSerial(rx)
    transport.connected = True
    return transport


def _frame_for(payload_obj) -> bytes:
    import json
    return _build_frame(json.dumps(payload_obj).encode())


def _make_ace(name="ACE_1") -> afcACE:
    unit = afcACE.__new__(afcACE)
    afc = MockAFC()
    afc.function = MagicMock()
    afc.error = MagicMock()
    afc.spool = MagicMock()
    afc.afcDeltaTime = MagicMock()
    afc.save_vars = MagicMock()
    afc.move_e_pos = MagicMock()
    afc.current = None

    unit.printer = MockPrinter(afc=afc)
    unit.afc = afc
    unit.logger = MockLogger()
    unit.reactor = SteppingReactor()
    unit.gcode = afc.gcode
    unit.function = afc.function
    unit.name = name
    unit.full_name = ["AFC_ACE", name]
    unit.type = "ACE"
    unit.lanes = {}
    unit.hub_obj = None
    unit.extruder_obj = None
    unit.buffer_obj = None
    unit.stepperless_drive = True
    unit.transport = None
    unit._slot_map = {}
    unit._last_prep = {}
    unit._operation_active = False
    unit.feed_speed = 50
    unit.retract_speed = 75
    unit.retract_mode = 0
    unit.bowden_overshoot = 300.0
    unit.hub_clear_mm = 60.0
    unit.tool_max_unload_attempts = 4
    unit.hub_sensor_name = None
    unit.toolhead_sensor_name = None
    unit.monitor_only = False
    unit._tail_pending = set()
    unit._tail_in_bowden = False
    unit.tail_purge_speed = 10.0
    unit.dryer_temp = 45
    unit.dryer_duration = 240
    unit.dryer_fan_speed = 7000
    from extras.AFC_ACE import AceDryerHeater, AceHumiditySensor
    unit.dryer_heater = AceDryerHeater(unit)
    unit.humidity_sensor = AceHumiditySensor()
    return unit


def _status(slot_states, overall="ready", action=""):
    return {
        "status": overall,
        "action": action,
        "temp": 30,
        "slots": [{"index": i, "status": s} for i, s in enumerate(slot_states)],
    }


# ── Framing ───────────────────────────────────────────────────────────────────

class TestFraming:
    def test_crc_known_value(self):
        # Self-consistency plus a couple of algebraic properties
        assert _crc16_mcrf4xx(b"") == 0xFFFF
        assert 0 <= _crc16_mcrf4xx(b"hello ace") <= 0xFFFF

    def test_frame_layout(self):
        payload = b'{"id":1,"method":"get_status"}'
        frame = _build_frame(payload)
        assert frame[:2] == b"\xFF\xAA"
        assert int.from_bytes(frame[2:4], "little") == len(payload)
        assert frame[4:4 + len(payload)] == payload
        crc = int.from_bytes(frame[4 + len(payload):6 + len(payload)], "little")
        assert crc == _crc16_mcrf4xx(payload)
        assert frame[-1] == 0xFE

    def test_frame_round_trip(self):
        transport = _make_transport(_frame_for({"id": 7, "code": 0}))
        result = transport._read_matching(7, timeout_s=1.0)
        assert result["ok"] is True
        assert result["response"]["id"] == 7

    def test_stale_frames_are_skipped(self):
        rx = _frame_for({"id": 3}) + _frame_for({"id": 4})
        transport = _make_transport(rx)
        result = transport._read_matching(4, timeout_s=1.0)
        assert result["ok"] is True
        assert result["response"]["id"] == 4

    def test_crc_mismatch_rejected(self):
        frame = bytearray(_frame_for({"id": 9}))
        frame[-3] ^= 0xFF  # corrupt one CRC byte
        transport = _make_transport(bytes(frame))
        result = transport._read_matching(9, timeout_s=0.3)
        assert result["ok"] is False
        assert "crc" in result["error"]

    def test_rpc_ace_error_code(self):
        transport = _make_transport(_frame_for({"id": 0, "code": 5, "msg": "no filament"}))
        transport._request_id = 0
        result = transport._rpc("feed_filament", {"index": 0})
        assert result["ok"] is False
        assert "code 5" in result["error"]


# ── USB port ordering ─────────────────────────────────────────────────────────

class TestPortOrdering:
    def test_numeric_segments_sort_numerically(self):
        # Chained ACEs enumerate as same-depth siblings; digits must compare
        # numerically so .3 sorts before .10 (lexicographic would invert them).
        paths = ["1-1.4.10/", "1-1.4.2/", "1-1.4.3/"]
        ordered = sorted(paths, key=_usb_path_key)
        assert ordered == ["1-1.4.2/", "1-1.4.3/", "1-1.4.10/"]


# ── Port pinning (watchdog re-enumeration safety) ─────────────────────────────

class TestPortPinning:
    """An idle ACE drops USB every ~3.5s, tty names swap between units, and a
    snapshot scan can miss a unit entirely — discovery must union scans over
    a full cycle, and a resolved unit must stay pinned to its USB path."""

    def _clock(self, monkeypatch):
        from extras import AFC_ACE as mod
        clock = {"v": 0.0}
        monkeypatch.setattr(mod.time, "time", lambda: clock["v"])
        monkeypatch.setattr(mod.time, "sleep",
                            lambda s: clock.__setitem__("v", clock["v"] + max(s, 0.1)))
        return mod

    def test_discovery_unions_flaky_scans(self, monkeypatch):
        mod = self._clock(monkeypatch)
        # First snapshot only sees the CHAINED unit (direct one mid-re-enum):
        # a naive scan would wrongly make it unit_index 0.
        scans = iter([{"2-1.4.4.3": "/dev/ttyACM0"}])
        both = {"2-1.4.3": "/dev/ttyACM1", "2-1.4.4.3": "/dev/ttyACM0"}
        monkeypatch.setattr(mod, "scan_ace_ports", lambda: next(scans, both))
        transport = AceTransport("auto", 0, MockLogger())
        port = transport._resolve_port()
        assert transport._pinned_path == "2-1.4.3"  # direct unit, despite flaky scan
        assert port == "/dev/ttyACM1"

    def test_pinned_unit_absent_never_falls_back_to_sibling(self, monkeypatch):
        mod = self._clock(monkeypatch)
        transport = AceTransport("auto", 0, MockLogger())
        transport._pinned_path = "2-1.4.3"
        monkeypatch.setattr(mod, "scan_ace_ports",
                            lambda: {"2-1.4.4.3": "/dev/ttyACM0"})  # only the sibling
        assert transport._resolve_port() is None

    def test_pinned_unit_follows_tty_rename(self, monkeypatch):
        mod = self._clock(monkeypatch)
        transport = AceTransport("auto", 0, MockLogger())
        transport._pinned_path = "2-1.4.3"
        monkeypatch.setattr(mod, "scan_ace_ports",
                            lambda: {"2-1.4.3": "/dev/ttyACM7"})
        assert transport._resolve_port() == "/dev/ttyACM7"


# ── Status parsing ────────────────────────────────────────────────────────────

class TestStatusParsing:
    def test_slot_present(self):
        unit = _make_ace()
        status = _status(["ready", "empty", "ready", "empty"])
        assert unit._slot_present(0, status) is True
        assert unit._slot_present(1, status) is False
        assert unit._slot_present(9, status) is False  # out of range

    def test_motion_active_from_any_field(self):
        unit = _make_ace()
        assert unit._motion_active(0, _status(["ready"] * 4, overall="feeding")) is True
        assert unit._motion_active(0, _status(["ready"] * 4, action="unwinding")) is True
        assert unit._motion_active(1, _status(["ready", "busy", "ready", "ready"])) is True
        assert unit._motion_active(0, _status(["ready"] * 4)) is False


# ── Motion primitives ─────────────────────────────────────────────────────────

class TestStopMotion:
    def test_timeout_assumes_success(self):
        unit = _make_ace()
        unit._rpc = MagicMock(return_value={"ok": False, "error": "timeout waiting for frame header"})
        result = unit._stop_motion(0, "stop_feed_filament")
        assert result["ok"] is True
        assert result.get("assumed_success") is True

    def test_real_error_propagates(self):
        unit = _make_ace()
        unit._rpc = MagicMock(return_value={"ok": False, "error": "ACE code 3: jam"})
        result = unit._stop_motion(0, "stop_feed_filament")
        assert result["ok"] is False


class TestFeedUntil:
    def test_sensor_stops_feed(self):
        unit = _make_ace()
        calls = []
        idle = _status(["ready"] * 4)

        def rpc(method, params=None, timeout=8.0):
            calls.append(method)
            if method == "get_status":
                return {"ok": True, "response": {"result": idle}}
            return {"ok": True, "response": {}}

        unit._rpc = rpc
        # Sensor triggers on the third poll
        states = iter([False, False, True, True, True, True])
        ok, msg = unit._feed_until(0, lambda: next(states, True), 1000, 50)
        assert ok is True, msg
        assert "feed_filament" in calls
        assert "stop_feed_filament" in calls

    def test_feed_extends_past_expected_distance(self):
        # 'length' is the expected run, not a cap: when the commanded feed
        # goes idle without the sensor, extension chunks keep it moving
        unit = _make_ace()
        feeds = []
        idle = _status(["ready"] * 4)  # always reads idle -> forces extensions

        def rpc(method, params=None, timeout=8.0):
            if method == "feed_filament":
                feeds.append(params["length"])
            if method == "get_status":
                return {"ok": True, "response": {"result": idle}}
            return {"ok": True, "response": {}}

        unit._rpc = rpc
        # Sensor trips only after two extensions have been issued
        state = {"n": 0}

        def sensor():
            state["n"] += 1
            return sum(feeds) > 1000 + 300  # after ~2 extension chunks

        ok, msg = unit._feed_until(0, sensor, 1000, 50)
        assert ok is True, msg
        assert feeds[0] == 1000 and all(f <= 200 for f in feeds[1:])
        assert len(feeds) >= 3

    def test_feed_gives_up_past_safety_budget(self):
        unit = _make_ace()
        idle = _status(["ready"] * 4)
        fed = []

        def rpc(method, params=None, timeout=8.0):
            if method == "feed_filament":
                fed.append(params["length"])
            if method == "get_status":
                return {"ok": True, "response": {"result": idle}}
            return {"ok": True, "response": {}}

        unit._rpc = rpc
        ok, msg = unit._feed_until(0, lambda: False, 1000, 50)
        assert ok is False
        assert sum(fed) <= 1000 * 2 + 500
        assert "did not trigger within" in msg

    def test_feed_rpc_failure(self):
        unit = _make_ace()
        unit._rpc = MagicMock(return_value={"ok": False, "error": "not connected"})
        ok, msg = unit._feed_until(0, lambda: False, 100, 50)
        assert ok is False
        assert "feed_filament failed" in msg


# ── homing sensor selection ───────────────────────────────────────────────────

class TestHomingSensors:
    def _lane(self, hub=None, extruder=None):
        lane = MagicMock()
        lane.hub_obj = hub
        lane.extruder_obj = extruder
        return lane

    def test_named_sensors_used_when_no_pins(self):
        unit = _make_ace()
        unit.hub_sensor_name = "filament_sensor"
        unit.toolhead_sensor_name = "toolhead_runout_sensor"
        sensor_obj = MagicMock()
        sensor_obj.runout_helper.filament_present = True
        unit.printer.lookup_object = MagicMock(return_value=sensor_obj)
        hub = MagicMock()
        hub.is_virtual_pin.return_value = True
        extruder = MagicMock()
        extruder.tool_start = None
        sensors = unit._homing_sensors(self._lane(hub=hub, extruder=extruder))
        assert [name for name, _ in sensors] == ["hub", "tool_start"]
        assert all(fn() is True for _, fn in sensors)

    def test_real_hub_pin_beats_named_sensor(self):
        unit = _make_ace()
        unit.hub_sensor_name = "filament_sensor"
        hub = MagicMock()
        hub.is_virtual_pin.return_value = False
        hub.state = True
        sensors = unit._homing_sensors(self._lane(hub=hub))
        assert len(sensors) == 1 and sensors[0][0] == "hub"
        assert sensors[0][1]() is True

    def test_missing_named_sensor_is_skipped(self):
        unit = _make_ace()
        unit.hub_sensor_name = "ghost_sensor"
        unit.printer.lookup_object = MagicMock(side_effect=Exception("not found"))
        assert unit._homing_sensors(self._lane()) == []


# ── two-stage load ────────────────────────────────────────────────────────────

class TestLoadStages:
    def _wire(self, unit, hub_state=None, tool_state=None):
        """Wire a lane with optional hub/tool sensors; record ACE feeds."""
        unit._slot_map = {"lane0": 0}
        unit._get_status = MagicMock(return_value=_status(["ready"] * 4))
        unit._set_assist = MagicMock()
        feeds = []

        def feed_until(slot, sensor_fn, length, speed):
            feeds.append((("homed" if sensor_fn else "blind"), length))
            return True, ""

        unit._feed_until = feed_until
        lane = MagicMock()
        lane.name = "lane0"
        lane.dist_hub = 1200.0
        lane.hub_obj.afc_bowden_length = 730.0
        lane.short_move_dis = 10
        sensors = []
        if hub_state is not None:
            states = iter(hub_state)
            sensors.append(("hub", lambda: next(states, True)))
        if tool_state is not None:
            tstates = iter(tool_state)
            sensors.append(("tool_start", lambda: next(tstates, True)))
        unit._homing_sensors = MagicMock(return_value=sensors)
        unit._sync_load = MagicMock()
        extruder = MagicMock()
        extruder.tool_end = None
        extruder.tool_stn = 43.5
        extruder.tool_load_speed = 8.0
        return lane, extruder, feeds

    def test_hub_then_toolhead_sensor_homed(self):
        unit = _make_ace()
        lane, extruder, feeds = self._wire(unit, hub_state=[False], tool_state=[False])
        assert unit._load_inner(lane, extruder) is True
        assert feeds == [("homed", 1200.0 + unit.bowden_overshoot),
                         ("homed", 730.0 + unit.bowden_overshoot)]
        unit._sync_load.assert_called_once_with(0, extruder, 43.5)

    def test_hub_only_blind_pushes_bowden(self):
        unit = _make_ace()
        lane, extruder, feeds = self._wire(unit, hub_state=[False], tool_state=None)
        assert unit._load_inner(lane, extruder) is True
        assert feeds == [("homed", 1200.0 + unit.bowden_overshoot), ("blind", 730.0)]

    def test_toolhead_only_cap_covers_full_path(self):
        # No hub sensor: stage 2 starts at the slot, so the homed feed's cap
        # must include dist_hub or long paths time out short of the sensor
        unit = _make_ace()
        lane, extruder, feeds = self._wire(unit, hub_state=None, tool_state=[False])
        assert unit._load_inner(lane, extruder) is True
        assert feeds == [("homed", 1200.0 + 730.0 + unit.bowden_overshoot)]

    def test_no_sensors_blind_pushes_full_path(self):
        unit = _make_ace()
        lane, extruder, feeds = self._wire(unit, hub_state=None, tool_state=None)
        assert unit._load_inner(lane, extruder) is True
        assert feeds == [("blind", 1200.0 + 730.0)]


# ── garbled-reply tolerance ───────────────────────────────────────────────────

class TestMotionRpc:
    def test_garbled_reply_with_motion_running_assumed_started(self):
        unit = _make_ace()
        calls = []

        def rpc(method, params=None, timeout=8.0):
            calls.append(method)
            if method == "feed_filament":
                return {"ok": False, "error": "crc mismatch"}
            return {"ok": True, "response": {"result": _status(["ready"] * 4, overall="feeding")}}

        unit._rpc = rpc
        res = unit._motion_rpc(0, "feed_filament", {"index": 0, "length": 100, "speed": 50})
        assert res["ok"] is True and res.get("assumed_started")
        assert calls.count("feed_filament") == 1  # never resent while moving

    def test_garbled_reply_with_idle_motion_resends_once(self):
        unit = _make_ace()
        calls = []

        def rpc(method, params=None, timeout=8.0):
            calls.append(method)
            if method == "feed_filament":
                # first attempt garbled, resend succeeds
                return ({"ok": False, "error": "crc mismatch"}
                        if calls.count("feed_filament") == 1 else {"ok": True})
            return {"ok": True, "response": {"result": _status(["ready"] * 4)}}

        unit._rpc = rpc
        res = unit._motion_rpc(0, "feed_filament", {"index": 0, "length": 100, "speed": 50})
        assert res["ok"] is True
        assert calls.count("feed_filament") == 2

    def test_clean_reply_passes_through(self):
        unit = _make_ace()
        unit._rpc = MagicMock(return_value={"ok": True})
        res = unit._motion_rpc(0, "unwind_filament", {"index": 0})
        assert res["ok"] is True
        unit._rpc.assert_called_once()


# ── unit_load_lane guards ─────────────────────────────────────────────────────

class TestUnitLoadLane:
    def test_slot_not_ready_fails(self):
        unit = _make_ace()
        unit._slot_map = {"lane0": 0}
        unit._homing_sensors = MagicMock(return_value=[])
        unit._get_status = MagicMock(return_value=_status(["empty", "ready", "ready", "ready"]))
        lane = MagicMock()
        lane.name = "lane0"
        extruder = MagicMock()
        assert unit.unit_load_lane(lane, extruder) is False
        unit.afc.error.handle_lane_failure.assert_called_once()

    def test_occupied_path_refuses_load(self):
        # Untracked filament in the bowden (lit sensor, nothing tool-loaded)
        # must refuse the load instead of double-feeding into it
        unit = _make_ace()
        unit._slot_map = {"lane0": 0}
        unit._homing_sensors = MagicMock(return_value=[("hub", lambda: True)])
        unit._load_inner = MagicMock()
        lane = MagicMock()
        lane.name = "lane0"
        assert unit.unit_load_lane(lane, MagicMock()) is False
        unit._load_inner.assert_not_called()
        unit.afc.error.handle_lane_failure.assert_called_once()
        assert "SET_LANE_LOADED" in unit.afc.error.handle_lane_failure.call_args[0][1]

    def test_tail_in_bowden_bypasses_occupancy_guard(self):
        unit = _make_ace()
        unit._tail_in_bowden = True
        unit._slot_map = {"lane0": 0}
        unit._homing_sensors = MagicMock(return_value=[("hub", lambda: True)])
        unit._load_inner = MagicMock(return_value=True)
        lane = MagicMock()
        lane.name = "lane0"
        assert unit.unit_load_lane(lane, MagicMock()) is True
        unit._load_inner.assert_called_once()

    def test_unmapped_lane_fails(self):
        unit = _make_ace()
        lane = MagicMock()
        lane.name = "mystery"
        assert unit.unit_load_lane(lane, MagicMock()) is False

    def test_monitor_only_refuses_all_motion(self):
        unit = _make_ace()
        unit.monitor_only = True
        unit._slot_map = {"lane0": 0}
        unit._rpc = MagicMock()
        lane = MagicMock()
        lane.name = "lane0"
        assert unit.unit_load_lane(lane, MagicMock()) is False
        assert unit.unit_unload_lane(lane, MagicMock()) is False
        unit.lane_move(lane, 100, None)
        assert unit.lane_unload(lane) is None
        unit._rpc.assert_not_called()  # no command ever reached the ACE

    def test_monitor_only_dryer_still_works(self):
        unit = _make_ace()
        unit.monitor_only = True
        unit._rpc = MagicMock(return_value={"ok": True})
        unit.set_dryer(50)
        unit._rpc.assert_called_once()


# ── endless spool (two-stage tail handling) ───────────────────────────────────

class TestEndlessSpool:
    def _wire_poll(self, unit, slot_states, tool_loaded=True, hub_lit=True):
        import time as _time
        unit._slot_map = {"lane0": 0}
        unit.transport = MagicMock()
        unit.transport.cached_status.return_value = (_status(slot_states), _time.time())
        unit._set_assist = MagicMock()
        lane = MagicMock()
        lane.name = "lane0"
        lane.tool_loaded = tool_loaded
        unit.lanes = {"lane0": lane}
        hub_state = {"lit": hub_lit}
        unit._homing_sensors = MagicMock(return_value=[("hub", lambda: hub_state["lit"])])
        return lane, hub_state

    def test_spool_end_defers_runout_until_hub_clears(self):
        unit = _make_ace()
        lane, hub = self._wire_poll(unit, ["empty", "ready", "ready", "ready"])
        unit._last_prep = {0: True}  # was present, now empty
        unit._poll_status(100.0)
        # Stage 1: tail mode — no runout yet, assist stopped
        lane.handle_load_runout.assert_not_called()
        unit._set_assist.assert_called_once_with(0, False)
        assert "lane0" in unit._tail_pending
        # Stage 2: hub clears -> runout fires, tail flagged for next load
        hub["lit"] = False
        unit._poll_status(101.5)
        lane.handle_load_runout.assert_called_once_with(101.5, False)
        assert "lane0" not in unit._tail_pending
        assert unit._tail_in_bowden is True

    def test_idle_spool_removal_fires_runout_immediately(self):
        unit = _make_ace()
        lane, _ = self._wire_poll(unit, ["empty"] * 4, tool_loaded=False)
        unit._last_prep = {0: True}
        unit._poll_status(100.0)
        lane.handle_load_runout.assert_called_once_with(100.0, False)
        assert not unit._tail_pending

    def test_unload_of_spent_spool_skips_cut_and_retract(self):
        unit = _make_ace()
        unit.afc.post_unload_macro = None
        unit.afc.do_tool_cut_tip_form = MagicMock()
        unit.gcode = MagicMock()
        unit._slot_map = {"lane0": 0}
        unit._get_status = MagicMock(return_value=_status(["empty"] * 4))
        unit._rpc = MagicMock(return_value={"ok": True})
        unit._set_assist = MagicMock()
        unit._homing_sensors = MagicMock(return_value=[("hub", lambda: True)])
        lane = MagicMock()
        lane.name = "lane0"
        extruder = MagicMock()
        assert unit.unit_unload_lane(lane, extruder) is True
        assert unit._tail_in_bowden is True
        unit.afc.do_tool_cut_tip_form.assert_not_called()
        unit._rpc.assert_not_called()  # no unwind commands sent
        lane.set_tool_unloaded.assert_called_once_with(normal_toolchange=True)

    def test_load_with_tail_pushes_through(self):
        unit = _make_ace()
        unit._tail_in_bowden = True
        unit._slot_map = {"lane0": 0}
        unit._get_status = MagicMock(return_value=_status(["ready"] * 4))
        unit._homing_sensors = MagicMock(return_value=[("hub", lambda: True)])
        unit._tail_push = MagicMock(return_value=True)
        unit._set_assist = MagicMock()
        unit._sync_load = MagicMock()
        lane = MagicMock()
        lane.name = "lane0"
        lane.dist_hub = 1200.0
        lane.hub_obj.afc_bowden_length = 730.0
        extruder = MagicMock()
        extruder.tool_stn = 40.0
        assert unit._load_inner(lane, extruder) is True
        unit._tail_push.assert_called_once_with(0, extruder, 770.0)
        unit._sync_load.assert_not_called()  # engagement handled by the push
        assert unit._tail_in_bowden is False


# ── RFID application ──────────────────────────────────────────────────────────

class TestRfid:
    def _lane(self, material="", color=""):
        lane = MagicMock()
        lane.material = material
        lane.color = color
        return lane

    def test_rfid_fills_blank_lane(self):
        unit = _make_ace()
        lane = self._lane()
        unit._apply_rfid(lane, {"type": "PLA", "color": [61, 84, 170]})
        assert lane.material == "PLA"
        assert lane.color == "#3D54AA"
        lane.send_lane_data.assert_called_once()

    def test_prep_does_not_clobber_user_values(self):
        unit = _make_ace()
        lane = self._lane(material="PETG", color="#112233")
        unit._apply_rfid(lane, {"type": "PLA", "color": [61, 84, 170]})
        assert lane.material == "PETG"
        assert lane.color == "#112233"

    def test_insert_forces_rfid_over_old_values(self):
        unit = _make_ace()
        lane = self._lane(material="PETG", color="#112233")
        unit._apply_rfid(lane, {"type": "PLA", "color": [61, 84, 170]}, force=True)
        assert lane.material == "PLA"
        assert lane.color == "#3D54AA"

    def test_tagless_spool_changes_nothing(self):
        unit = _make_ace()
        lane = self._lane()
        unit._apply_rfid(lane, {"type": "", "color": [0, 0, 0]}, force=True)
        assert lane.material == ""
        assert lane.color == ""
        lane.send_lane_data.assert_not_called()


# ── dryer as heater ───────────────────────────────────────────────────────────

class TestDryer:
    def test_heater_set_temp_starts_drying(self):
        unit = _make_ace()
        calls = []
        unit._rpc = lambda m, p=None, timeout=8.0: (calls.append((m, p)), {"ok": True})[1]
        unit.dryer_heater.set_temp(55)  # SET_HEATER_TEMPERATURE path
        assert calls == [("drying", {"temp": 55, "fan_speed": 7000, "duration": 240})]
        assert unit.dryer_heater.target == 55.0

    def test_heater_set_temp_zero_stops(self):
        unit = _make_ace()
        calls = []
        unit._rpc = lambda m, p=None, timeout=8.0: (calls.append((m, p)), {"ok": True})[1]
        unit.dryer_heater.target = 55.0
        unit.dryer_heater.set_temp(0)
        assert calls == [("drying_stop", None)]
        assert unit.dryer_heater.target == 0.0

    def test_temp_clamped_to_firmware_limit(self):
        unit = _make_ace()
        calls = []
        unit._rpc = lambda m, p=None, timeout=8.0: (calls.append((m, p)), {"ok": True})[1]
        unit.set_dryer(90)
        assert calls[0][1]["temp"] == 65


# ── system_Test ───────────────────────────────────────────────────────────────

class TestSystemTest:
    def _lane(self, name="lane0"):
        lane = MagicMock()
        lane.name = name
        lane.map = "T0"
        lane.tool_loaded = False
        lane.remember_spool = False
        lane.led_not_ready = "1,0,0,0"
        lane.led_index = None
        return lane

    def test_not_connected_fails(self):
        unit = _make_ace()
        unit._get_status = MagicMock(return_value=None)
        lane = self._lane()
        assert unit.system_Test(lane, 0.1, False, True) is False
        lane.set_afc_prep_done.assert_called_once()

    def test_spool_present_marks_loaded(self):
        unit = _make_ace()
        unit._slot_map = {"lane0": 0}
        unit._get_status = MagicMock(return_value=_status(["ready", "empty", "empty", "empty"]))
        lane = self._lane()
        assert unit.system_Test(lane, 0.1, False, True) is True
        assert lane.prep_state is True
        assert lane.status == AFCLaneState.LOADED
        # Not tool-loaded: raw hub occupancy clear (virtual hub aggregates it,
        # True would block TOOL_LOAD as "hub not clear") but staged/loadable
        # (load_state -> loaded_to_hub gates TOOL_LOAD's load trigger check)
        assert lane._load_state is False
        assert lane.loaded_to_hub is True

    def test_present_spool_seeds_default_weight(self):
        # weight 0 renders as an empty reel in the UIs; a present spool with
        # no recorded weight gets the unit full_weight default
        unit = _make_ace()
        unit.full_weight = 1000.0
        unit._slot_map = {"lane0": 0}
        unit._get_status = MagicMock(return_value=_status(["ready"] * 4))
        lane = self._lane()
        lane.weight = 0
        unit.system_Test(lane, 0.1, False, True)
        assert lane.weight == 1000.0

    def test_user_weight_never_clobbered(self):
        unit = _make_ace()
        unit.full_weight = 1000.0
        unit._slot_map = {"lane0": 0}
        unit._get_status = MagicMock(return_value=_status(["ready"] * 4))
        lane = self._lane()
        lane.weight = 340.0
        unit.system_Test(lane, 0.1, False, True)
        assert lane.weight == 340.0

    def test_empty_slot_clears_spool(self):
        unit = _make_ace()
        unit._slot_map = {"lane0": 0}
        unit._get_status = MagicMock(return_value=_status(["empty"] * 4))
        lane = self._lane()
        assert unit.system_Test(lane, 0.1, False, True) is True
        assert lane.prep_state is False
        unit.afc.spool.clear_values.assert_called_once_with(lane)
