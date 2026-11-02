"""
Runs the AFC_CFS unit (with plain AFC lanes) against a simulated CFS box (tests/cfs_sim.py)
on Klipper's *real* reactor, so the serial I/O, completions, mutex and timer
interplay are exercised for real.

Run as a subprocess (tests/test_AFC_CFS_reactor.py does this) because the AFC
test conftest replaces chelper with a mock, and the real reactor needs the
real one:

    KLIPPER_PATH=~/klipper python3 tests/cfs_reactor_harness.py <scenario>

Prints one JSON object: {"checks": {name: bool}, "info": {...}}.
A reactor watchdog timer runs every 5ms during each scenario; "max_gap"
reports the longest the reactor was ever unable to service it.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from unittest.mock import MagicMock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRMWARE = os.environ["KLIPPER_PATH"]
KLIPPY = os.path.join(FIRMWARE, "klippy")
sys.path.insert(0, REPO)

# Real reactor + chelper. Klipper keeps klippy/ as a flat module directory;
# Kalico makes it a package (klippy.reactor with relative imports).
if os.path.isfile(os.path.join(KLIPPY, "__init__.py")):
    # Register klippy as a bare package so its __init__ (which imports the
    # whole printer stack, pyserial included) doesn't run; we only need
    # klippy.reactor and its relative imports (chelper, util).
    import types  # noqa: E402
    pkg = types.ModuleType("klippy")
    pkg.__path__ = [KLIPPY]
    sys.modules["klippy"] = pkg
    from klippy import reactor as kreactor  # noqa: E402
else:
    sys.path.append(KLIPPY)
    import reactor as kreactor  # noqa: E402
import tests.conftest  # noqa: E402,F401  installs Klipper mocks for everything else
from tests.conftest import MockAFC  # noqa: E402
from tests.cfs_sim import FakeCFS  # noqa: E402
from extras import AFC_CFS as P  # noqa: E402
from extras.AFC_CFS import afcCFS  # noqa: E402
from extras.AFC_lane import AFCLane  # noqa: E402

FAST = {
    "stage_settle_time": 0.02,
    "stage5_poll_interval": 0.03,
    "request_timeout": 0.5,
    "poll_interval": 0.2,
}


class Defaults:
    """Stands in for a Klipper config section: returns each option's real
    default unless overridden."""

    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)

    def getint(self, key, default=None, **kw):
        return int(self.values.get(key, default))

    def getfloat(self, key, default=None, **kw):
        return float(self.values.get(key, default))

    def getboolean(self, key, default=None):
        return bool(self.values.get(key, default))


class Logger:
    def __init__(self):
        self.lines = []

    def _log(self, level, msg):
        self.lines.append("{}: {}".format(level, msg))

    def debug(self, msg):
        self._log("debug", msg)

    def info(self, msg):
        self._log("info", msg)

    def warning(self, msg):
        self._log("warning", msg)

    def error(self, msg):
        self._log("error", msg)


class Watchdog:
    def __init__(self, r, period=0.005):
        self.r = r
        self.period = period
        self.max_gap = 0.0
        self.slow_ticks = 0      # ticks that arrived more than 50ms late
        self.last = None
        self.timer = r.register_timer(self._tick, r.NOW)

    def _tick(self, eventtime):
        now = self.r.monotonic()
        if self.last is not None:
            gap = now - self.last - self.period
            self.max_gap = max(self.max_gap, gap)
            if gap > 0.05:
                self.slow_ticks += 1
        self.last = now
        return now + self.period

    def stop(self):
        self.r.unregister_timer(self.timer)

    @property
    def never_stalled(self):
        """A blocking implementation stalls on every request (dozens of
        100ms+ gaps). A shared test machine adds the odd isolated spike, so
        allow two late ticks but nothing long."""
        return self.slow_ticks <= 2 and self.max_gap < 0.25


class FakeExtruder:
    def __init__(self, sim):
        self.sim = sim
        self.name = "extruder"
        self.tool_start = "pin"
        self.fila_tool_start = None      # newer AFC checks this before tool_start_state
        self.tool_stn = 50.0
        self.tool_load_speed = 25.0
        self.estats = MagicMock()
        self.moved = []
        self.lane_loaded = None

    @property
    def tool_start_state(self):
        return self.sim.toolhead_sensor


class Rig:
    def __init__(self, r, sim, **overrides):
        self.r = r
        self.sim = sim
        self.logger = Logger()

        afc = MockAFC()
        afc.tool_cut = True
        afc.tool_cut_cmd = "AFC_CUT"
        afc.park = False
        afc.park_cmd = None
        afc.form_tip = False
        afc._check_extruder_temp = MagicMock(return_value=False)
        afc.error.handle_lane_failure = MagicMock()
        afc.gcode.run_script_from_command = MagicMock()
        afc.save_vars = MagicMock()
        afc.function.is_printing = MagicMock(return_value=False)
        self.extruder = FakeExtruder(sim)

        def move_e_pos(distance, speed, label="", wait_tool=False):
            self.extruder.moved.append((round(distance, 3), label))
            sim.extruder_move(distance)
            # Scaled-down motion time so slow primes don't make tests slow
            r.pause(r.monotonic() + min(abs(distance) / max(speed, 0.1) * 0.01, 0.2))

        afc.move_e_pos = move_e_pos
        self.afc = afc

        self.unit = self.add_unit("CFS_1", **overrides)

    def add_unit(self, name, **overrides):
        """Another [AFC_CFS] section; with the same serial it joins the chain."""
        unit = afcCFS.__new__(afcCFS)
        unit.reactor = self.r
        unit.logger = self.logger
        unit.name = name
        unit.lanes = {}
        unit.afc = self.afc
        values = dict(FAST)
        values.update(overrides)
        values.setdefault("serial", self.sim.path)
        unit._load_cfs_config(Defaults(values))
        unit._init_cfs_runtime()
        unit.on_filament_insert = MagicMock()
        unit.on_filament_remove = MagicMock()
        unit.lane_tool_loaded = MagicMock()      # LED handling, needs a full lane
        unit.lane_tool_unloaded = MagicMock()
        unit.gcode = MagicMock()                 # per-lane commands register here
        return unit

    def lane(self, index, unit=None, name=None):
        unit = unit or self.unit
        lane = AFCLane.__new__(AFCLane)
        lane.name = name or "lane{}".format(index)
        lane.index = index
        lane.unit_obj = unit
        lane.afc = self.afc
        lane.reactor = self.r
        lane.logger = self.logger
        lane.extruder_obj = self.extruder
        lane.select_lane = MagicMock()
        lane.disable_buffer = MagicMock()
        lane.set_loaded = MagicMock()
        lane.set_unloaded = MagicMock()
        lane.loaded_to_hub = False
        lane.status = None
        lane.tool_loaded = False
        lane.runout_lane = None
        lane.spool_id = None
        lane._afc_prep_done = True
        lane.prep = None
        lane.load = None
        lane.custom_load_cmd = None
        lane.custom_unload_cmd = None
        unit.lanes[lane.name] = lane
        unit._setup_lane(lane)           # what the unit does at connect
        lane.unit_obj.present_last[lane.name] = None
        return lane

    @property
    def failures(self):
        return [c.args[1] for c in self.afc.error.handle_lane_failure.call_args_list]

    def cut_calls(self):
        return [c.args[0] for c in self.afc.gcode.run_script_from_command.call_args_list
                if c.args[0].startswith("AFC_CUT")]


def wait(r, seconds):
    r.pause(r.monotonic() + seconds)


def wait_until(r, cond, timeout=5.0, step=0.05):
    """Run the reactor until cond() is true or timeout passes; cond()'s last value."""
    end = r.monotonic() + timeout
    while not cond() and r.monotonic() < end:
        r.pause(r.monotonic() + step)
    return cond()


def is_subsequence(needle, haystack):
    it = iter(haystack)
    return all(any(x == y for y in it) for x in needle)


# -- scenarios ---------------------------------------------------------------

def sc_connect_addressed(r):
    sim = FakeCFS()
    rig = Rig(r, sim)
    ok = rig.unit.connect()
    sim.close()
    return {
        "online": ok and rig.unit.online,
        "version_read": rig.unit.version_sn == "SIMCFS-0001",
        "skipped_discovery": sim.count(P.CMD_GET_SLAVE_INFO) == 0,
    }, {}


def sc_connect_discovery(r):
    sim = FakeCFS(addressed=False)
    rig = Rig(r, sim)
    ok = rig.unit.connect()
    sim.close()
    return {
        "online": ok and rig.unit.online,
        "box_got_address": sim.addr == 1,
        "used_discovery": sim.count(P.CMD_GET_SLAVE_INFO) >= 1,
    }, {}


def sc_retry_and_offline(r):
    sim = FakeCFS()
    rig = Rig(r, sim, request_timeout=0.15)
    rig.unit.connect()
    sim.drop_replies = 1
    retried = rig.unit.read_buffer_state() == P.BUFFER_MIDDLE
    sim.drop_replies = 1000
    t0 = r.monotonic()
    for _ in range(rig.unit.offline_after):
        rig.unit.read_buffer_state()
    dead_box_seconds = r.monotonic() - t0
    went_offline = not rig.unit.online
    sim.drop_replies = 0
    recovered = rig.unit.read_material_mask() == sim.material_mask and rig.unit.online
    sim.close()
    return {
        "retry_recovers_dropped_reply": retried,
        "offline_after_failures": went_offline,
        "back_online_on_reply": recovered,
        # status queries to a dead box fail fast: no waiting for late replies
        "dead_box_fails_fast": dead_box_seconds < 3 * 2 * 0.15 + 1.0,
    }, {"timeouts": rig.unit.transport.timeouts, "dead_box_seconds": round(dead_box_seconds, 2)}


def sc_garbage_resync(r):
    sim = FakeCFS()
    sim.garbage_before_reply = b"\x00\xf7\xff\x13\xf7"
    rig = Rig(r, sim)
    rig.unit.connect()
    masks = [rig.unit.read_material_mask() for _ in range(5)]
    sim.close()
    return {"parses_through_garbage": all(m == sim.material_mask for m in masks)}, {
        "crc_errors": rig.unit.transport._parser.crc_errors}


def sc_poll_presence(r):
    sim = FakeCFS(material_mask=0b0011)
    rig = Rig(r, sim)
    lanes = [rig.lane(i) for i in (1, 2, 3)]
    def presence():
        return [l.unit_obj.present_last[l.name] for l in lanes]

    # Wait for the readings instead of a fixed time, so a slow test machine
    # doesn't fail the check
    start = r.monotonic()
    rig.unit._cfs_handle_ready()
    wait_until(r, lambda: presence() == [True, True, False])
    initial = presence()
    sim.material_mask = 0b0110
    wait_until(r, lambda: presence() == [False, True, True])
    wait(r, 0.3)                     # let the insert/removal handlers finish
    after = presence()
    elapsed = r.monotonic() - start
    rig.unit._cfs_handle_disconnect()
    heartbeats = sim.count(P.CMD_ONLINE_CHECK)
    polls = sim.count(P.GET_FILAMENT_SENSOR_STATE)
    sim.close()
    return {
        "initial_presence": initial == [True, True, False],
        "presence_follows_box": after == [False, True, True],
        "insert_marks_loaded": lanes[2].set_loaded.called,
        "insert_event_sent": rig.unit.on_filament_insert.called,
        "removal_marks_unloaded": lanes[0].set_unloaded.called,
        # The connect check, then one heartbeat per 1.5 s (the default), not one per poll
        "heartbeat_sent": 1 <= heartbeats <= elapsed / 1.5 + 2,
        "heartbeat_not_every_poll": heartbeats < polls,
    }, {"initial": initial, "after": after, "heartbeats": heartbeats, "polls": polls,
        "seconds": round(elapsed, 2)}


def _prep_lane(rig, lane):
    """Run the unit's PREP hook on a harness lane (AFC's bits mocked)."""
    lane.prep_state = None
    lane._load_state = None
    lane.map = "T{}".format(lane.index - 1)
    lane.send_lane_data = MagicMock()
    lane.do_enable = MagicMock()
    lane.set_afc_prep_done = MagicMock(side_effect=lambda: setattr(lane, "_afc_prep_done", True))
    return rig.unit.system_Test(lane, 0, False, False)


def sc_box_boots_late(r):
    """Box starts answering ~2s into PREP: the first lane waits for it and
    every lane preps normally."""
    sim = FakeCFS(material_mask=0b0011)
    sim.mute = True
    rig = Rig(r, sim, boot_wait=6.0, request_timeout=0.2)
    for name in ("lane_loaded", "lane_not_ready", "lane_tool_loaded", "lane_tool_loaded_idle"):
        setattr(rig.unit, name, MagicMock())
    rig.afc.current = None
    lanes = [rig.lane(i) for i in (1, 2, 3)]
    for l in lanes:
        l._afc_prep_done = False

    def unmute(eventtime):
        sim.mute = False
        return r.NEVER
    r.register_timer(unmute, r.monotonic() + 2.0)
    dog = Watchdog(r)
    started = r.monotonic()
    results = [_prep_lane(rig, l) for l in lanes]
    took = r.monotonic() - started
    dog.stop()
    sim.close()
    return {
        "all_lanes_online": results == [True, True, True],
        "presence_right": [l.prep_state for l in lanes] == [True, True, False],
        # one connect attempt to a silent box takes a few seconds itself
        "waited_for_box": 1.5 < took < 10.0,
        "reactor_never_stalled": dog.never_stalled,
    }, {"took": round(took, 2), "results": results}


def sc_offline_prep_then_online(r):
    """Box never answered during PREP; when it does, lanes sync quietly
    (no insert event) and a tool-loaded lane stays loaded."""
    sim = FakeCFS(material_mask=0b0011)
    sim.mute = True
    rig = Rig(r, sim, boot_wait=0.5, request_timeout=0.2, reconnect_interval=0.5,
              poll_interval=0.2)
    for name in ("lane_loaded", "lane_not_ready", "lane_tool_loaded", "lane_tool_loaded_idle"):
        setattr(rig.unit, name, MagicMock())
    rig.afc.current = None
    lanes = [rig.lane(i) for i in (1, 2, 3)]
    lanes[0].tool_loaded = True
    rig.extruder.lane_loaded = "lane1"
    for l in lanes:
        l._afc_prep_done = False
    results = [_prep_lane(rig, l) for l in lanes]
    rig.unit._cfs_handle_ready()
    sim.mute = False
    wait(r, 3.0)
    rig.unit._cfs_handle_disconnect()
    sim.close()
    return {
        "prep_saw_offline": results == [False, False, False],
        "came_online": rig.unit.online,
        "presence_synced": ([l.unit_obj.present_last[l.name] for l in lanes]
                            == [True, True, False]),
        "no_insert_event": not rig.unit.on_filament_insert.called,
        "tool_loaded_kept": lanes[0].tool_loaded and not lanes[0].set_unloaded.called,
        "tool_led_set": rig.unit.lane_tool_loaded_idle.called,
    }, {"results": results, "online": rig.unit.online,
        "presence": [l.unit_obj.present_last[l.name] for l in lanes]}


def _load(rig, lane):
    lane.unit_obj.cmd_AFC_CFS_TOOL_LOAD(lane, None)


def _unload(rig, lane):
    lane.unit_obj.cmd_AFC_CFS_TOOL_UNLOAD(lane, None)


def sc_load_unload(r):
    # Deliberately slow box replies: the reactor must stay responsive anyway
    sim = FakeCFS(reply_delay=0.05, stage5_delay=0.25)
    rig = Rig(r, sim)
    lane = rig.lane(1)
    dog = Watchdog(r)
    rig.unit.connect()

    _load(rig, lane)
    loaded_sensor = sim.toolhead_sensor
    stages = list(sim.stages_seen)
    modes = [d for f, d in sim.log if f == P.SET_BOX_MODE]
    after_load_position = sim.position
    moves_after_load = list(rig.extruder.moved)
    afc_tooled_after_load = (lane.tool_loaded
                             and getattr(rig.extruder, "lane_loaded", None) == lane.name)
    tool_state_warnings = [m for m in rig.logger.lines
                           if "could not record tool state" in m]

    _unload(rig, lane)
    dog.stop()
    sim.close()
    return {
        "no_failures": rig.failures == [],
        "reached_toolhead": loaded_sensor,
        "stage_order": is_subsequence([0, 4, 5, 6, 7], stages),
        "marked_slot_print": bytes([0x01, P.MODE_PRINT]) in modes,
        # The box feeds during the print only while the slot stays in print mode
        "print_mode_kept_after_load": bool(modes) and modes[-1] == bytes([0x01, P.MODE_PRINT]),
        "cleared_latched_error": sim.latched_error == P.ST_OK,
        "primed_then_tool_stn": [m[0] for m in moves_after_load] == [10.0, 5.0, 35.0],
        "cut_before_unload": len(rig.cut_calls()) == 1,
        "toolhead_clear_after_unload": not sim.toolhead_sensor,
        "box_reeled_back": sim.position == 0.0,
        "start_while_gears_hold": (len(sim.start_positions) == 1
                                   and sim.start_positions[0] >= sim.grip_len),
        "extruder_released_before_reel": (len(sim.reel_positions) == 1
                                          and sim.reel_positions[0] < sim.grip_len),
        "afc_marked_tool_loaded": afc_tooled_after_load and not tool_state_warnings,
        "afc_marked_tool_unloaded": (not lane.tool_loaded
                                     and getattr(rig.extruder, "lane_loaded", None) is None),
        "connection_motor_stopped": sim.conn_action == P.CONN_STOP,
        "reactor_never_stalled": dog.never_stalled,
    }, {"max_gap_ms": round(dog.max_gap * 1000, 2), "stages": stages,
        "position_after_load": after_load_position, "failures": rig.failures,
        "report": lane.unit_obj.load_reports[lane.name],
        "tool_state_warnings": tool_state_warnings,
        "lane_loaded": getattr(rig.extruder, "lane_loaded", None)}


def sc_slot_switch(r):
    sim = FakeCFS(material_mask=0b0011)
    rig = Rig(r, sim)
    l1, l2 = rig.lane(1), rig.lane(2)
    rig.unit.connect()
    _load(rig, l1)
    first = sim.connected_mask
    _unload(rig, l1)
    _load(rig, l2)
    second = sim.connected_mask
    sim.close()
    return {
        "no_failures": rig.failures == [],
        "slot_a_connected": first == 0x01,
        "slot_b_connected": second == 0x02,
        "toolhead_loaded": sim.toolhead_sensor,
    }, {"failures": rig.failures}


def sc_stall_fails_cleanly(r):
    sim = FakeCFS(feed_per_poll=0.0)
    rig = Rig(r, sim, stage5_stall_time=0.3, load_attempts=2)
    lane = rig.lane(1)
    rig.unit.connect()
    _load(rig, lane)
    sim.close()
    return {
        "reported_failure": len(rig.failures) == 1 and "stalled" in rig.failures[0],
        "tried_twice": sim.stages_seen.count(0) == 2,
        "box_left_stopped": sim.conn_action == P.CONN_STOP and not sim.tighten,
        "extruder_untouched": rig.extruder.moved == [],
    }, {"failures": rig.failures}


def sc_empty_slot(r):
    sim = FakeCFS(material_mask=0b0011)
    rig = Rig(r, sim)
    lane = rig.lane(3)
    rig.unit.connect()
    _load(rig, lane)
    sim.close()
    return {
        "reported_empty": len(rig.failures) == 1 and "no filament" in rig.failures[0],
        "never_started_extrude": sim.count(P.EXTRUDE_PROCESS) == 0,
    }, {"failures": rig.failures}


def sc_offline(r):
    sim = FakeCFS()
    path = sim.path
    sim.close()
    rig = Rig(r, sim, serial="/dev/does-not-exist-cfs")
    lane = rig.lane(1)
    dog = Watchdog(r)
    _load(rig, lane)
    dog.stop()
    return {
        "reported_offline": len(rig.failures) == 1 and "offline" in rig.failures[0],
        "reactor_never_stalled": dog.never_stalled,
    }, {"failures": rig.failures, "unused_path": path}


def sc_trace_recording(r):
    import importlib.util
    import tempfile
    root = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
    # tools/ in the standalone repo, utilities/ in AFC
    tool_path = next(p for p in (os.path.join(root, d, "cfs_trace_summary.py")
                                 for d in ("tools", "utilities")) if os.path.isfile(p))
    spec = importlib.util.spec_from_file_location("cfs_trace_summary", tool_path)
    summary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(summary)

    tmp = tempfile.mkdtemp(prefix="cfs-trace-")
    path = os.path.join(tmp, "logs", "cfs_trace.jsonl")
    sim = FakeCFS(material_mask=0b0011)
    rig = Rig(r, sim, trace_file=path)
    lane = rig.lane(1)
    rig.lane(2)
    dog = Watchdog(r)
    rig.unit.connect()
    rig.unit._cfs_handle_ready()           # idle polling, should stay out of the trace
    wait(r, 0.7)
    sim.material_mask = 0b0111             # a spool goes in: recorded as presence
    wait(r, 0.5)
    rig.unit._cfs_handle_disconnect()       # stops polling and flushes the trace
    rig.unit.trace.start()                  # reopen to record the tool change
    _load(rig, lane)
    _unload(rig, lane)
    dog.stop()
    rig.unit.trace.stop()
    sim.close()

    events, files, bad = summary.load(path)
    kinds = [e["ev"] for e in events]
    load_end = [e for e in events if e["ev"] == "load_end"]
    polled_fns = {e["fn"] for e in events if e["ev"] == "tx"} & {"GET_BUFFER_STATE"}
    import io
    buf = io.StringIO()
    summary.summarize(events, out=buf)
    text = buf.getvalue()
    return {
        "file_written": len(files) == 1 and bad == 0 and len(events) > 20,
        "has_settings": any(e["ev"] == "trace_start" and e.get("settings") for e in events),
        "online_with_version": any(e["ev"] == "online" and e.get("version") == "SIMCFS-0001"
                                   for e in events),
        "idle_polls_not_traced": not polled_fns,
        "presence_recorded": any(e["ev"] == "presence" and e.get("slots") == "ABC"
                                 for e in events),
        "load_events": all(k in kinds for k in ("load_start", "stage5_result", "handoff",
                                                 "load_end")),
        "load_timed": bool(load_end) and load_end[0].get("ok") and load_end[0]["seconds"] > 0,
        "unload_events": all(k in kinds for k in ("unload_start", "cut", "retract_step",
                                                   "unload_end")),
        "replies_have_latency": all("ms" in e for e in events if e["ev"] == "rx"),
        "summary_reports_load": "Loads: 1 ok, 0 failed" in text and "Unloads: 1 ok" in text,
        "summary_reports_latency": "EXTRUDE_PROCESS" in text and "p95" in text,
        "reactor_never_stalled": dog.never_stalled,
    }, {"events": len(events), "max_gap_ms": round(dog.max_gap * 1000, 2),
        "summary": text}


def _read_trace(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def sc_slow_stage0(r):
    """Real K1 timing: stage 0 blocks ~3.7s and pushes filament forward."""
    sim = FakeCFS(stage_delays={0: 3.7}, stage0_feed=200.0)
    rig = Rig(r, sim)
    lane = rig.lane(1)
    dog = Watchdog(r)
    rig.unit.connect()
    _load(rig, lane)
    dog.stop()
    sim.close()
    return {
        "no_failures": rig.failures == [],
        "reached_toolhead": sim.toolhead_sensor,
        "no_timeouts": rig.unit.transport.timeouts == 0,
        "reactor_never_stalled": dog.never_stalled,
    }, {"failures": rig.failures, "timeouts": rig.unit.transport.timeouts,
        "max_gap_ms": round(dog.max_gap * 1000, 2)}


def sc_late_reply_resync(r):
    """A stage times out anyway: later commands must wait for its late reply
    instead of queueing behind it or being matched to it."""
    import tempfile
    path = os.path.join(tempfile.mkdtemp(prefix="cfs-resync-"), "t.jsonl")
    sim = FakeCFS(stage_delays={0: 2.5})
    rig = Rig(r, sim, stage_timeout=1.0, load_attempts=1, trace_file=path)
    lane = rig.lane(1)
    dog = Watchdog(r)
    rig.unit.connect()
    _load(rig, lane)
    dog.stop()
    rig.unit.trace.stop()
    sim.close()
    events = _read_trace(path)
    kinds = [e["ev"] for e in events]
    late = [e for e in events if e["ev"] == "late_reply"]
    after_timeout = kinds[kinds.index("timeout") + 1:] if "timeout" in kinds else []
    return {
        "only_stage0_timed_out": rig.unit.transport.timeouts == 1,
        "late_reply_recognised": len(late) == 1 and late[0]["fn"] == "EXTRUDE_PROCESS",
        "waited_before_next_command": bool(after_timeout) and after_timeout[0] in (
            "late_reply", "resync"),
        "nothing_left_outstanding": rig.unit.transport._outstanding == 0,
        "failure_reported": len(rig.failures) == 1 and "stage 0" in rig.failures[0],
        "box_left_stopped": sim.conn_action == P.CONN_STOP and not sim.tighten,
        "no_unsolicited_frames": "rx_unsolicited" not in kinds,
        "reactor_never_stalled": dog.never_stalled,
    }, {"kinds_after_timeout": after_timeout[:6], "failures": rig.failures}


def sc_eject_part_fed(r):
    """Filament stopped at the toolhead sensor, short of the gears (the state a
    failed load can leave): AFC_CFS_EJECT reels it back with the box alone."""
    sim = FakeCFS(reel_delay=2.0)   # stage 1 blocks until the reel-back finishes
    rig = Rig(r, sim)
    lane = rig.lane(1)
    rig.unit.connect()
    sim.position = 610.0            # past the sensor (600), short of the gears (620)
    before = sim.toolhead_sensor
    t0 = r.monotonic()
    ok, detail = rig.unit.reel_back(lane)
    seconds = r.monotonic() - t0
    sim.close()
    return {
        "started_at_sensor": before,
        "eject_ok": ok,
        "toolhead_clear": not sim.toolhead_sensor,
        "box_reeled_back": sim.position == 0.0,
        "latched_error_cleared": sim.latched_error == P.ST_OK,
        # done when stage 1 answers, not after waiting out check-ins
        "finishes_with_stage1": seconds < 4.0,
        "no_checkins_needed": sim.count(P.RETRUDE_PROCESS) == 2,
    }, {"detail": detail, "seconds": round(seconds, 2)}


def sc_unload_still_gripped(r):
    """Retract too short to leave the gears: the reel-back fails and the
    unload reports it instead of claiming success or hanging."""
    sim = FakeCFS()
    rig = Rig(r, sim, unload_retract_steps="5:600", unload_timeout=1.0)
    lane = rig.lane(1)
    dog = Watchdog(r)
    rig.unit.connect()
    _load(rig, lane)
    _unload(rig, lane)
    dog.stop()
    sim.close()
    return {
        "unload_failed": len(rig.failures) == 1,
        "reported_reel_problem": bool(rig.failures) and "unload failed" in rig.failures[0],
        "box_kept_filament_out": sim.position > 0.0,
        "afc_still_tool_loaded": lane.tool_loaded,
        "reactor_never_stalled": dog.never_stalled,
    }, {"failures": rig.failures, "max_gap_ms": round(dog.max_gap * 1000, 2),
        "reel_positions": sim.reel_positions}


UID_X = bytes.fromhex("b0313b4e8411b00645353230")   # first box on the chain
UID_Y = bytes.fromhex("c1424c5f9522c11756464341")   # second box


def sc_two_boxes_by_uid(r):
    """Two unaddressed boxes on one chain. Discovery finds X first, but the
    config puts Y at address 1, so each section must get its own box."""
    sim = FakeCFS(addressed=False, uid=UID_X, material_mask=0b0011)
    box_y = sim.add_box(addressed=False, uid=UID_Y, material_mask=0b1100)
    rig = Rig(r, sim, box_addr=1, box_uid=UID_Y.hex())
    unit2 = rig.add_unit("CFS_2", box_addr=2, box_uid=UID_X.hex())
    shared = rig.unit.transport is unit2.transport
    ok1 = rig.unit.connect()
    ok2 = unit2.connect()
    m1 = rig.unit.read_material_mask()
    m2 = unit2.read_material_mask()
    rig.unit._cfs_handle_disconnect()
    sim.close()
    return {
        "one_port_for_the_chain": shared,
        "both_online": ok1 and ok2,
        "addressed_by_uid": box_y.addr == 1 and sim.addr == 2,
        "each_unit_reads_its_box": m1 == 0b1100 and m2 == 0b0011,
        "uids_recorded": rig.unit.seen_uid == UID_Y and unit2.seen_uid == UID_X,
    }, {"addr_x": sim.addr, "addr_y": box_y.addr, "m1": m1, "m2": m2}


def sc_two_boxes_load_while_polling(r):
    """A load and unload on box 2 while both boxes are polled over the shared
    port: box 1's status polls pause while box 2 is busy (a poll squeezed in
    between stage 5 polls made the real box 2 miss one), then resume; box 1
    stays online and nothing blocks the reactor."""
    sim = FakeCFS(addressed=True, addr=2, uid=UID_X, reply_delay=0.02, stage5_delay=0.2)
    box1 = sim.add_box(addressed=True, addr=1, uid=UID_Y, material_mask=0b0001)
    rig = Rig(r, sim, box_addr=1, box_uid=UID_Y.hex(), poll_interval=0.2)
    unit2 = rig.add_unit("CFS_2", box_addr=2, box_uid=UID_X.hex(), poll_interval=0.2)
    rig.lane(1)                                   # lane1 on box 1 (polled only)
    lane5 = rig.lane(1, unit=unit2, name="lane5")  # slot A of box 2
    dog = Watchdog(r)
    rig.unit.connect()
    unit2.connect()
    rig.unit._cfs_handle_ready()
    unit2._cfs_handle_ready()
    wait(r, 0.5)
    polls_before = box1.count(P.GET_FILAMENT_SENSOR_STATE)
    lane5.unit_obj.cmd_AFC_CFS_TOOL_LOAD(lane5, None)
    loaded = sim.toolhead_sensor
    lane5.unit_obj.cmd_AFC_CFS_TOOL_UNLOAD(lane5, None)
    polls_during = box1.count(P.GET_FILAMENT_SENSOR_STATE) - polls_before
    wait(r, 0.6)
    polls_after = box1.count(P.GET_FILAMENT_SENSOR_STATE) - polls_before - polls_during
    unit2._cfs_handle_disconnect()
    rig.unit._cfs_handle_disconnect()
    dog.stop()
    sim.close()
    return {
        "no_failures": rig.failures == [],
        "loaded_from_box2": loaded,
        "unloaded": sim.position == 0.0 and not sim.toolhead_sensor,
        "box1_never_got_motor_commands": box1.count(P.EXTRUDE_PROCESS) == 0
                                         and box1.count(P.RETRUDE_PROCESS) == 0,
        "box1_not_polled_while_box2_busy": polls_during <= 1,
        "box1_polled_again_after": polls_after > 0,
        "box1_still_online": rig.unit.online,
        "reactor_never_stalled": dog.never_stalled,
    }, {"failures": rig.failures, "max_gap_ms": round(dog.max_gap * 1000, 2),
        "box1_polls": [polls_before, polls_during, polls_after]}


def sc_stage5_reply_lost(r):
    """The box misses one stage 5 poll (seen on a K1 Max with two boxes). The
    load carries on with the next poll instead of waiting out a resync."""
    sim = FakeCFS(addressed=True, reply_delay=0.02, stage5_delay=0.02)
    sim.drop_stage5_reply = 2
    rig = Rig(r, sim)
    lane = rig.lane(1)
    rig.unit.connect()
    start = r.monotonic()
    lane.unit_obj.cmd_AFC_CFS_TOOL_LOAD(lane, None)
    took = r.monotonic() - start
    loaded = sim.toolhead_sensor
    sim.close()
    return {
        "no_failures": rig.failures == [],
        "loaded": loaded,
        "reply_was_dropped": sim.stage5_polls >= 3,
        "no_long_resync_wait": took < 15.0,
    }, {"failures": rig.failures, "seconds": round(took, 2), "stage5_polls": sim.stage5_polls}


def sc_wrong_box_at_address(r):
    """Boxes already addressed the other way round (e.g. by stock firmware):
    a unit must refuse the box at its address rather than drive the wrong one."""
    sim = FakeCFS(addressed=True, addr=1, uid=UID_X)
    sim.add_box(addressed=True, addr=2, uid=UID_Y)
    rig = Rig(r, sim, box_addr=1, box_uid=UID_Y.hex())
    ok = rig.unit.connect()
    sim.close()
    return {
        "refused": not ok and not rig.unit.online,
        "explains": "power-cycle" in (rig.unit.transport.last_error or "") or
                    any("wrong" in l or "expected" in l for l in rig.logger.lines),
    }, {"lines": rig.logger.lines[-3:]}


def sc_single_box_without_uid(r):
    """Existing single-box configs (no box_uid) keep working as before."""
    sim = FakeCFS(addressed=False, uid=UID_X)
    rig = Rig(r, sim)
    ok = rig.unit.connect()
    sim.close()
    return {
        "online": ok,
        "addressed_at_1": sim.addr == 1,
        "uid_recorded": rig.unit.seen_uid == UID_X,
    }, {}


PINK_SILK = "AB1240276A21000020E7A18E0330000001000000"   # read from a real CFS


def sc_rfid_insert_reads_tag(r):
    """rfid_read on: a spool inserted into slot D is read once the delay
    passes, and the lane gets its color, material and weight from the tag.
    Box 1 slot D on a K1 Max returned exactly this record."""
    sim = FakeCFS(addressed=True, material_mask=0b1001)
    sim.tags = {4: PINK_SILK}
    rig = Rig(r, sim, rfid_read=True, rfid_insert_delay=0.1, rfid_retry_interval=0.05)
    rig.lane(1)
    lane4 = rig.lane(4)
    rig.afc.gcode.run_script = MagicMock()
    rig.unit.connect()
    rig.unit.read_material_mask()
    rig.unit.queue_tag_read(lane4, rig.unit.rfid_insert_delay)
    wait(r, 1.0)
    cmds = [c.args[0] for c in rig.afc.gcode.run_script.call_args_list]
    sim.close()
    return {
        "slot_turned_first": sim.preload_runs == [0x08],
        "color_set": "SET_COLOR LANE=lane4 COLOR=E7A18E" in cmds,
        "material_set": "SET_MATERIAL LANE=lane4 MATERIAL=PLA" in cmds,
        "weight_set": "SET_WEIGHT LANE=lane4 WEIGHT=1000" in cmds,
        "tag_remembered": (rig.unit.lane_tags.get("lane4") or {}).get("material_code") == "00002",
        "box_still_online": rig.unit.online,
    }, {"cmds": cmds, "preload_runs": sim.preload_runs}


def sc_rfid_no_tag_box_busy(r):
    """A slot without a tag: the box stays busy after the turn (about a minute
    on a real box). The read waits it out, reports no tag, changes nothing on
    the lane and doesn't mark the box offline."""
    sim = FakeCFS(addressed=True, material_mask=0b0001)
    sim.rfid_busy_delay = 1.5
    rig = Rig(r, sim, rfid_busy_timeout=5.0, rfid_read_attempts=2, rfid_retry_interval=0.05)
    lane1 = rig.lane(1)
    rig.afc.gcode.run_script = MagicMock()
    rig.unit.connect()
    rig.unit.read_material_mask()
    start = r.monotonic()
    tag = rig.unit.read_tag(lane1)
    took = r.monotonic() - start
    sim.close()
    return {
        "no_tag": tag is None,
        "waited_for_the_box": took >= 1.4,
        "box_still_online": rig.unit.online,
        "nothing_applied": rig.afc.gcode.run_script.call_count == 0,
    }, {"seconds": round(took, 2)}


def sc_rfid_box_silent_longer(r):
    """The box stays silent longer than rfid_busy_timeout (seen on a real box
    after turning an untagged slot). The tag request then waits for the late
    reply instead of talking over the busy box, and the box stays online."""
    sim = FakeCFS(addressed=True, material_mask=0b0001)
    sim.rfid_busy_delay = 6.0
    rig = Rig(r, sim, rfid_busy_timeout=5.0, rfid_read_attempts=1, resync_timeout=10.0)
    lane1 = rig.lane(1)
    rig.unit.connect()
    rig.unit.read_material_mask()
    tag = rig.unit.read_tag(lane1)
    trace_ok = rig.unit.transport._outstanding == 0
    runs = list(sim.preload_runs)
    sim.close()
    return {
        "no_tag": tag is None,
        "slot_turned_once": runs == [0x01],
        "late_reply_absorbed": trace_ok,
        "box_still_online": rig.unit.online,
        "consecutive_failures_reset": rig.unit._consecutive_failures == 0,
    }, {"preload_runs": runs}


def sc_rfid_read_at_startup(r):
    """rfid_read_at_startup: after PREP, every slot with a spool is read once
    (one at a time); empty slots aren't turned, and it doesn't repeat."""
    sim = FakeCFS(addressed=True, material_mask=0b1011)
    sim.tags = {4: PINK_SILK}
    rig = Rig(r, sim, rfid_read_at_startup=True, rfid_startup_delay=0.05,
              rfid_read_attempts=1, poll_interval=0.2)
    for i in (1, 2, 3, 4):
        rig.lane(i)
    rig.afc.gcode.run_script = MagicMock()
    rig.unit.connect()
    rig.unit._cfs_handle_ready()
    wait(r, 2.0)
    first = list(sim.preload_runs)
    wait(r, 1.0)
    cmds = [c.args[0] for c in rig.afc.gcode.run_script.call_args_list]
    rig.unit._cfs_handle_disconnect()
    sim.close()
    return {
        "full_slots_read_once": sorted(sim.preload_runs) == [0x01, 0x02, 0x08],
        "empty_slot_not_turned": 0x04 not in sim.preload_runs,
        "not_repeated": sim.preload_runs == first,
        "tagged_lane_set": "SET_COLOR LANE=lane4 COLOR=E7A18E" in cmds,
        "box_still_online": rig.unit.online,
    }, {"preload_runs": sim.preload_runs, "cmds": cmds}


def sc_rfid_startup_off_by_default(r):
    """Without rfid_read_at_startup nothing is turned or read at start."""
    sim = FakeCFS(addressed=True, material_mask=0b1111)
    sim.tags = {4: PINK_SILK}
    rig = Rig(r, sim, poll_interval=0.2)
    for i in (1, 2, 3, 4):
        rig.lane(i)
    rig.unit.connect()
    rig.unit._cfs_handle_ready()
    wait(r, 1.0)
    rig.unit._cfs_handle_disconnect()
    sim.close()
    return {"nothing_read": sim.preload_runs == [] and sim.count(P.GET_RFID) == 0}, {}


def sc_rfid_load_waits_for_tag_read(r):
    """A tool load started while a tag read is running (the box busy and
    silent after turning an untagged slot) waits for the read to finish
    instead of sending load commands into it."""
    sim = FakeCFS(addressed=True, material_mask=0b0011)
    sim.rfid_busy_delay = 1.5
    rig = Rig(r, sim, rfid_busy_timeout=5.0, rfid_read_attempts=3, rfid_retry_interval=0.3)
    lane1 = rig.lane(1)
    lane2 = rig.lane(2)
    rig.unit.connect()
    rig.unit.read_material_mask()
    done = {}

    def reader(eventtime):
        done["tag"] = rig.unit.read_tag(lane2)
        done["at"] = len(sim.log)
    r.register_callback(reader)
    wait(r, 0.2)                       # the read has turned slot B and is waiting
    _load(rig, lane1)
    # The read is RUN [02 02] ... GET_RFID ... CLOSE; nothing from the load
    # may land between the turn and the read
    log = [(f, bytes(d)) for f, d in sim.log]
    start = log.index((P.SET_PRE_LOADING, bytes([0x02, P.PRELOAD_RUN])))
    # (the load also starts with a CLOSE, so look up to the read's last GET_RFID)
    end = max(i for i, (f, d) in enumerate(log) if f == P.GET_RFID)
    # (status polls are paused during the read, so anything else is the load's)
    inside = [P.fn_name(f) for f, d in log[start + 1:end] if f != P.GET_RFID]
    sim.close()
    return {
        "no_failures": rig.failures == [],
        "loaded": sim.toolhead_sensor,
        "no_load_commands_inside_the_read": inside == [],
        "read_finished": "at" in done,
        "box_still_online": rig.unit.online,
    }, {"inside": inside}


def sc_rfid_spoolman_id(r):
    """A tag with a Spoolman spool ID in its reserved field sets the lane's
    spool (AFC then takes color/material/weight from Spoolman)."""
    sim = FakeCFS(addressed=True, material_mask=0b0010)
    sim.tags = {2: PINK_SILK[:34] + "000042"}
    rig = Rig(r, sim)
    rig.afc.spoolman = "http://spoolman"
    lane2 = rig.lane(2)
    rig.unit.connect()
    rig.unit.read_material_mask()
    tag = rig.unit.read_tag(lane2, spin=False)
    cmds = []
    rig.unit.apply_tag(lane2, tag, cmds.append)
    sim.close()
    return {
        "spool_id_decoded": tag is not None and tag["spool_id"] == 42,
        "set_spool_id_only": cmds == ["SET_SPOOL_ID LANE=lane2 SPOOL_ID=42"],
        "no_turn_with_spin_off": sim.preload_runs == [],
    }, {"cmds": cmds}


def sc_rfid_not_during_print(r):
    """A spool inserted during a print isn't read until the print ends."""
    sim = FakeCFS(addressed=True, material_mask=0b0001)
    sim.tags = {1: PINK_SILK}
    rig = Rig(r, sim, rfid_read=True, rfid_insert_delay=0.05)
    lane1 = rig.lane(1)
    rig.afc.gcode.run_script = MagicMock()
    printing = {"on": True}
    rig.unit._job_active = lambda: printing["on"]
    rig.unit.connect()
    rig.unit.read_material_mask()
    rig.unit.queue_tag_read(lane1, 0.05)
    wait(r, 0.5)
    read_while_printing = bool(sim.preload_runs)
    printing["on"] = False
    # retries every 10 s while blocked; pull that forward for the test
    rig.unit._rfid_queue["lane1"] = r.monotonic()
    r.update_timer(rig.unit._rfid_timer, r.NOW)
    wait(r, 0.5)
    sim.close()
    return {
        "not_read_while_printing": not read_while_printing,
        "read_after_print": sim.preload_runs == [0x01],
        "applied_after_print": rig.afc.gcode.run_script.call_count > 0,
    }, {"preload_runs": sim.preload_runs}


SCENARIOS = {name[3:]: fn for name, fn in globals().items() if name.startswith("sc_")}


def run(name):
    r = kreactor.Reactor()
    out = {"checks": {}, "info": {}}

    def body(eventtime):
        try:
            checks, info = SCENARIOS[name](r)
            out["checks"] = checks
            out["info"] = info
        except Exception:
            out["exception"] = traceback.format_exc()
        finally:
            r.end()
        return r.NEVER

    r.register_timer(body, r.NOW)
    r.run()
    return out


if __name__ == "__main__":
    names = sys.argv[1:] or sorted(SCENARIOS)
    results = {n: run(n) for n in names}
    print(json.dumps(results if len(names) > 1 else results[names[0]], default=str))
