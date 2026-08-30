# AFCProject Automated Filament Changer
#
# Copyright (C) 2024-2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Anycubic ACE Pro unit driver. Protocol knowledge derived from the CosmoACE
# addon (framed JSON-RPC over USB-CDC) and the printers-for-people/ACEResearch
# reverse-engineering effort.
#
# The ACE Pro is a closed 4-slot feeder speaking a proprietary framed JSON-RPC
# protocol over USB serial (115200). It has no Klipper MCU and no steppers AFC
# can drive, so this unit is stepperless (like OpenAMS): the ACE firmware
# moves filament (feed/unwind/feed-assist) and AFC orchestrates sensors,
# toolhead engagement, toolchanges and runout on top of it.
#
# Filament path assumptions:
#   spool -> ACE slot feeder -> 4:1 splitter -> bowden -> [hub sensor,
#   optional] -> [toolhead pre-gear sensor aka tool_start, recommended] ->
#   extruder gears -> nozzle
# At least one sensor (hub or tool_start) is required to home ACE feeds;
# with neither, loads fall back to a blind bowden-length push.

from __future__ import annotations

import glob
import json
import os
import queue
import re
import threading
import time
import traceback
from configparser import Error as error
from typing import Any, Callable, Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from gcode import GCodeCommand
    from extras.AFC_lane import AFCLane, SpeedMode
    from extras.AFC_extruder import AFCExtruder

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCLaneState
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try: from extras.AFC_unit import afcUnit
except: raise error(ERROR_STR.format(import_lib="AFC_unit", trace=traceback.format_exc()))

try: from extras.AFC_OpenAMS import _ams_box_logo, _ams_box_logo_error
except: raise error(ERROR_STR.format(import_lib="AFC_OpenAMS", trace=traceback.format_exc()))

try:
    import serial
except ImportError:
    serial = None

# ACE firmware states that mean "a motor is moving"
ACTIVE_STATES = frozenset(("busy", "feeding", "unwinding", "shifting"))

# The ACE drops its USB link (~0.5s re-enumeration) if it sees no complete
# frame for ~3.5s, and pauses motion mid-feed if status isn't polled roughly
# every second. One heartbeat interval serves both.
HEARTBEAT_S = 0.8


def _crc16_mcrf4xx(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        v = byte ^ (crc & 0xFF)
        v ^= (v & 0x0F) << 4
        crc = (((v << 8) | (crc >> 8)) ^ (v >> 4) ^ (v << 3)) & 0xFFFF
    return crc


def _build_frame(payload: bytes) -> bytes:
    frame = bytearray(b"\xFF\xAA")
    frame.extend(len(payload).to_bytes(2, "little"))
    frame.extend(payload)
    frame.extend(_crc16_mcrf4xx(payload).to_bytes(2, "little"))
    frame.append(0xFE)
    return bytes(frame)


def _usb_path_key(path: str) -> list:
    """Sort key that orders usb path 1-1.4.3 before 1-1.4.10."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", path)]


def scan_ace_ports() -> Dict[str, str]:
    """One snapshot of ACE units: {usb device path: tty device}.

    Found by USB product string, not device name: chained ACEs all report the
    same by-id name and ttyACM numbering swaps between units on every
    watchdog re-enumeration. Only the USB topology path is a stable identity.
    """
    found: Dict[str, str] = {}
    for dev in glob.glob("/sys/bus/usb/devices/*/"):
        try:
            with open(os.path.join(dev, "product"), encoding="utf-8", errors="replace") as f:
                product = f.read()
        except OSError:
            continue
        if "ACE" not in product.upper():
            continue
        for tty in glob.glob(os.path.join(dev, "*", "tty", "tty*")):
            found[dev.rstrip("/")] = "/dev/" + os.path.basename(tty)
    return found


class AceTransport(threading.Thread):
    """Owns the ACE serial port in a background thread.

    RPCs are queued from the klippy reactor thread and answered via a
    threading.Event the caller polls with reactor.pause. When idle, the
    thread sends get_status every HEARTBEAT_S: this feeds the ACE's comms
    watchdog, keeps motors alive during long feeds, and refreshes the status
    cache the unit reads for lane sync and motion waits.
    """

    def __init__(self, serial_port: str, unit_index: int, logger) -> None:
        super().__init__(name="AFC_ACE_serial", daemon=True)
        self.configured_port = serial_port
        self.unit_index = unit_index
        self.logger = logger
        self.baud = 115200
        self._ser = None
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._status_lock = threading.Lock()
        self._status: Optional[Dict[str, Any]] = None
        self._status_time = 0.0
        self._shutdown = threading.Event()
        # Seed request ids from the PID so a klippy restart can't match a
        # stale reply frame from the previous process.
        self._request_id = (os.getpid() * 100) % 300000
        self._pinned_path: Optional[str] = None
        self.last_error: Optional[str] = None
        self.connected = False

    # ── reactor-side API ────────────────────────────────────────────

    def submit(self, method: str, params: Optional[dict] = None):
        """Queue an RPC; returns (result-dict, done-event, cancelled-event).
        Result is filled in place before done is set. Setting cancelled before
        the worker picks the request up abandons it — the command is never
        sent (a caller that timed out must not leave a motion command armed
        in the queue to fire later)."""
        result: Dict[str, Any] = {}
        done = threading.Event()
        cancelled = threading.Event()
        self._queue.put((method, params, result, done, cancelled))
        return result, done, cancelled

    def cached_status(self) -> Tuple[Optional[Dict[str, Any]], float]:
        with self._status_lock:
            return self._status, self._status_time

    def stop(self) -> None:
        self._shutdown.set()

    # ── worker thread ───────────────────────────────────────────────

    def run(self) -> None:
        while not self._shutdown.is_set():
            try:
                method, params, result, done, cancelled = self._queue.get(timeout=HEARTBEAT_S)
            except queue.Empty:
                self._heartbeat()
                continue
            if cancelled.is_set():
                result.update({"ok": False, "error": "request abandoned"})
                done.set()
                continue
            try:
                result.update(self._rpc(method, params))
            except Exception as exc:
                result.update({"ok": False, "error": f"rpc {method} raised: {exc}"})
            finally:
                done.set()
        self._disconnect()

    def _heartbeat(self) -> None:
        res = self._rpc("get_status")
        if res.get("ok"):
            payload = res.get("response", {}).get("result")
            if isinstance(payload, dict):
                with self._status_lock:
                    self._status = payload
                    self._status_time = time.time()

    def _resolve_port(self) -> Optional[str]:
        if self.configured_port and self.configured_port.lower() != "auto":
            return self.configured_port
        # Once a USB path is pinned, only ever return that unit's current tty
        # (the tty name changes on every watchdog re-enumeration; the unit
        # must not silently become a different ACE when a sibling is absent).
        if self._pinned_path is None:
            self._pinned_path = self._discover_unit_path()
            if self._pinned_path is None:
                return None
            self.logger.info(
                f"ACE unit_index {self.unit_index} pinned to USB path {self._pinned_path}")
        return scan_ace_ports().get(self._pinned_path)

    def _discover_unit_path(self) -> Optional[str]:
        """Accumulate ACE USB paths over a full watchdog cycle: an idle ACE
        drops USB every ~3.5s and is invisible ~0.5s, so a single snapshot
        can miss a unit and mis-index the survivors."""
        seen: set = set()
        deadline = time.time() + 6.0
        while time.time() < deadline and not self._shutdown.is_set():
            seen.update(scan_ace_ports().keys())
            time.sleep(0.5)
        ordered = sorted(seen, key=_usb_path_key)
        if self.unit_index < len(ordered):
            return ordered[self.unit_index]
        self.last_error = (f"only {len(ordered)} ACE unit(s) on USB, "
                           f"unit_index {self.unit_index} not found")
        return None

    def _connect(self) -> bool:
        if self._ser is not None and self._ser.is_open:
            return True
        if serial is None:
            self.last_error = "pyserial not available"
            return False
        port = self._resolve_port()
        if port is None:
            self.last_error = f"no ACE found on USB (unit_index {self.unit_index})"
            self.connected = False
            return False
        try:
            self._ser = serial.Serial(port, self.baud, timeout=0.1, write_timeout=2.0)
            self.connected = True
            self.last_error = None
            self.logger.debug(f"ACE connected on {port}"
                              + (f" ({self._pinned_path})" if self._pinned_path else ""))
            return True
        except Exception as exc:
            self.last_error = f"open {port} failed: {exc}"
            self.connected = False
            self._ser = None
            return False

    def _disconnect(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self.connected = False

    def _next_id(self) -> int:
        current = self._request_id
        self._request_id = (self._request_id + 1) % 300000
        return current

    def _rpc(self, method: str, params: Optional[dict] = None) -> Dict[str, Any]:
        # A call landing in the watchdog's re-enumeration gap fails on write
        # before the frame went through, so reconnect once and resend.
        # Read-side failures are NOT retried: the command may already be
        # executing and a resend would double the motion.
        for attempt in range(2):
            if not self._connect():
                if attempt == 0:
                    time.sleep(0.7)
                    continue
                return {"ok": False, "error": self.last_error or "not connected"}
            req: Dict[str, Any] = {"id": self._next_id(), "method": method}
            if params:
                req["params"] = params
            frame = _build_frame(json.dumps(req, separators=(",", ":")).encode())
            try:
                self._ser.write(frame)
                self._ser.flush()
            except Exception as exc:
                self.last_error = f"write failed: {exc}"
                self._disconnect()
                if attempt == 0:
                    time.sleep(0.7)
                    continue
                return {"ok": False, "error": self.last_error}
            response = self._read_matching(req["id"], timeout_s=5.0)
            if not response.get("ok"):
                self.last_error = str(response.get("error"))
                # A bad frame usually means we are mid-stream (partial frame
                # tail still arriving). Drain twice with a gap so the next
                # read starts on a frame boundary.
                try:
                    self._ser.reset_input_buffer()
                    time.sleep(0.15)
                    self._ser.reset_input_buffer()
                except Exception:
                    pass
                return response
            parsed = response["response"]
            code = parsed.get("code")
            if code not in (None, 0, "0"):
                msg = str(parsed.get("msg", "")).strip() or "unknown ACE error"
                self.last_error = f"{method} failed with ACE code {code}: {msg}"
                return {"ok": False, "error": self.last_error, "response": parsed}
            self.last_error = None
            return {"ok": True, "response": parsed}
        return {"ok": False, "error": self.last_error or "rpc failed"}

    def _read_exact(self, count: int, deadline: float) -> bytes:
        out = bytearray()
        while len(out) < count and time.time() < deadline:
            chunk = self._ser.read(count - len(out))
            if chunk:
                out.extend(chunk)
        return bytes(out)

    def _read_frame(self, deadline: float) -> Dict[str, Any]:
        header = bytearray()
        while time.time() < deadline:
            b = self._read_exact(1, deadline)
            if not b:
                continue
            header.append(b[0])
            header = header[-2:]
            if bytes(header) == b"\xFF\xAA":
                break
        else:
            return {"ok": False, "error": "timeout waiting for frame header"}
        raw_len = self._read_exact(2, deadline)
        if len(raw_len) != 2:
            return {"ok": False, "error": "timeout waiting for frame length"}
        payload_len = int.from_bytes(raw_len, "little")
        if not 0 < payload_len <= 4096:
            return {"ok": False, "error": f"invalid frame payload length {payload_len}"}
        payload = self._read_exact(payload_len, deadline)
        if len(payload) != payload_len:
            return {"ok": False, "error": "timeout waiting for frame payload"}
        crc_raw = self._read_exact(2, deadline)
        if len(crc_raw) != 2:
            return {"ok": False, "error": "timeout waiting for frame crc"}
        if int.from_bytes(crc_raw, "little") != _crc16_mcrf4xx(payload):
            return {"ok": False, "error": "crc mismatch"}
        while time.time() < deadline:
            tail = self._read_exact(1, deadline)
            if tail == b"\xFE":
                break
        else:
            return {"ok": False, "error": "timeout waiting for frame terminator"}
        try:
            return {"ok": True, "payload": json.loads(payload.decode("utf-8"))}
        except Exception:
            return {"ok": False, "error": "frame payload is not JSON"}

    def _read_matching(self, request_id: int, timeout_s: float) -> Dict[str, Any]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            frame = self._read_frame(deadline)
            if not frame.get("ok"):
                return frame
            parsed = frame.get("payload")
            if isinstance(parsed, dict) and parsed.get("id") == request_id:
                return {"ok": True, "response": parsed}
            # stale frame from a previous request — keep reading
        return {"ok": False, "error": f"timeout waiting for rpc response id={request_id}"}


class AceDryerHeater:
    """Duck-typed Klipper heater for the ACE's firmware dryer.

    Registered with the heaters registry so SET_HEATER_TEMPERATURE (what UIs
    send — grumpyscreen's AFC panel, fluidd) starts/stops drying, and
    TURN_OFF_HEATERS / M112 shut it off. Temperature/target are synced from
    the ACE status poll.
    """

    def __init__(self, unit: "afcACE") -> None:
        self.unit = unit
        self.temperature = 0.0
        self.target = 0.0

    def set_temp(self, degrees: float) -> None:
        self.unit.set_dryer(int(degrees))

    def get_temp(self, eventtime=None):
        return self.temperature, self.target

    def alter_target(self, target: float) -> None:
        self.target = target

    def check_busy(self, eventtime) -> bool:
        return False

    def get_status(self, eventtime=None):
        return {"temperature": round(self.temperature, 1), "target": self.target}


class AceHumiditySensor:
    """Read-only sensor object exposing the ACE enclosure humidity (%)."""

    def __init__(self) -> None:
        self.humidity = 0.0

    def get_status(self, eventtime=None):
        return {"humidity": round(self.humidity, 1),
                "temperature": round(self.humidity, 1)}


class afcACE(afcUnit):
    """Anycubic ACE Pro unit — stepperless, firmware-managed feeder."""

    def __init__(self, config: ConfigWrapper) -> None:
        super().__init__(config)
        self.type = config.get('type', 'ACE')
        self.stepperless_drive: bool = True

        self.serial_port = config.get("serial", "auto")
        # Which ACE this unit is, in USB-topology order, when serial is auto.
        # The directly connected unit is 0, the first chained unit 1, ...
        self.ace_unit_index = config.getint("unit_index", 0, minval=0)
        # Monitor-only: slot status, RFID and the dryer stay live, but every
        # filament-motion command is refused. For a chained ACE whose filament
        # feeds another printer, or a unit not yet plumbed into the splitter.
        self.monitor_only = config.getboolean("monitor_only", False)
        self.feed_speed = config.getint("feed_speed", 50, minval=10)
        self.retract_speed = config.getint("retract_speed", 75, minval=10)
        # unwind_filament mode: 0 = normal, 1 = "enhanced" (drives the spool
        # take-up harder; believed to pile up slack on some units)
        self.retract_mode = config.getint("retract_mode", 0)
        # Extra mm commanded beyond afc_bowden_length when feeding toward a
        # sensor; the feed is stopped by the sensor, not by this length.
        self.bowden_overshoot = config.getfloat("bowden_overshoot", 300.0, minval=0.0)
        # Extra retract after the homing sensor clears, so filament parks
        # behind the splitter (staged for fast reload) and the next lane can
        # load. Must exceed the sensor -> splitter-merge distance.
        self.hub_clear_mm = config.getfloat("hub_clear_mm", 100.0, minval=0.0)
        # Endless spool: speed (mm/s) for pushing a spent spool's tail through
        # the melt zone with the extruder in step with the ACE.
        self.tail_purge_speed = config.getfloat("tail_purge_speed", 10.0, minval=1.0)
        self.dryer_temp = config.getint("dryer_temp", 45)
        self.dryer_duration = config.getint("dryer_duration", 240)  # minutes
        self.dryer_fan_speed = config.getint("dryer_fan_speed", 7000)
        # Existing [filament_switch_sensor] objects to home feeds against, for
        # printers whose sensors are already declared elsewhere in the config
        # (a pin can only be claimed once). hub_sensor_name sits between the
        # splitter and the toolhead; toolhead_sensor_name is at the extruder
        # inlet. Both are alternatives to [AFC_hub] switch_pin / [AFC_extruder]
        # pin_tool_start, which take priority when set.
        self.hub_sensor_name = config.get("hub_sensor_name", None)
        self.toolhead_sensor_name = config.get("toolhead_sensor_name", None)

        # Expose the firmware dryer as a heater (+ humidity sensor) so UIs
        # control it with SET_HEATER_TEMPERATURE and see live values.
        self.dryer_heater = AceDryerHeater(self)
        self.humidity_sensor = AceHumiditySensor()
        dryer_name = f"ace_dryer_{self.name}"
        hum_name = f"ace_humidity_{self.name}"
        pheaters = self.printer.load_object(config, "heaters")
        pheaters.heaters[dryer_name] = self.dryer_heater
        pheaters.available_heaters.append(dryer_name)
        pheaters.available_sensors.append(hum_name)
        self.printer.add_object(dryer_name, self.dryer_heater)
        self.printer.add_object(hum_name, self.humidity_sensor)
        # Mainline Klipper routes SET_HEATER_TEMPERATURE through
        # heaters.lookup_heater (the dict insert above covers it); Kalico
        # instead registers it as a per-heater mux command, so register ours —
        # on mainline the global command already exists and this raises.
        try:
            self.gcode.register_mux_command(
                'SET_HEATER_TEMPERATURE', "HEATER", dryer_name,
                self._cmd_SET_DRYER_TEMPERATURE,
                desc=f"Set {self.name} ACE dryer temperature")
        except Exception:
            pass

        self.transport: Optional[AceTransport] = None
        self._slot_map: Dict[str, int] = {}
        self._last_prep: Dict[int, Optional[bool]] = {}
        self._operation_active = False
        self._poll_timer = None
        # Endless-spool state: lanes printing out their ungripped tail, and
        # whether a spent tail still occupies the shared bowden (the next
        # load must push it through the nozzle instead of blind-feeding).
        self._tail_pending: set = set()
        self._tail_in_bowden = False

        self.gcode.register_mux_command(
            'AFC_ACE_DRYER_START', "UNIT", self.name, self.cmd_AFC_ACE_DRYER_START,
            desc="Start the ACE dryer: AFC_ACE_DRYER_START UNIT=%s [TEMP=] [DURATION=] [FAN_SPEED=]" % self.name)
        self.gcode.register_mux_command(
            'AFC_ACE_DRYER_STOP', "UNIT", self.name, self.cmd_AFC_ACE_DRYER_STOP,
            desc="Stop the ACE dryer")
        self.gcode.register_mux_command(
            'AFC_ACE_STATUS', "UNIT", self.name, self.cmd_AFC_ACE_STATUS,
            desc="Report raw ACE status")
        self.gcode.register_mux_command(
            'AFC_ACE_STOP', "UNIT", self.name, self.cmd_AFC_ACE_STOP,
            desc="Stop all ACE motion for a lane: AFC_ACE_STOP UNIT=%s LANE=" % self.name)

    # ── lifecycle ───────────────────────────────────────────────────

    def handle_connect(self) -> None:
        super().handle_connect()
        self.logo = _ams_box_logo("ACE PRO", 4, self.name)
        self.logo_error = _ams_box_logo_error("ACE PRO", 4, self.name)

    def handle_ready(self) -> None:
        super().handle_ready()
        # AFC_spool subscribes to afc_stepper:register_macros inside its own
        # klippy:connect handler, which runs AFTER this unit's connect handler
        # has already had the lanes fire that event — so SET_COLOR /
        # SET_MATERIAL / SET_MAP / SET_WEIGHT / SET_SPOOL_ID never register
        # for unit-driven lanes. Register them directly; tolerate versions
        # where the ordering is fixed and they already exist.
        for lane in self.lanes.values():
            try:
                self.afc.spool.register_lane_macros(lane)
            except Exception:
                pass
        for lane in self.lanes.values():
            # Some frontends only look lanes up as "AFC_stepper <name>";
            # register that alias like AFC_canvas does.
            try:
                self.printer.add_object(f"AFC_stepper {lane.name}", lane)
            except Exception:
                pass
            slot = lane.index - 1
            if not 0 <= slot <= 3:
                self.logger.error(
                    f"AFC_ACE {self.name}: lane {lane.name} has unit index {lane.index}, "
                    f"expected 1..4 (e.g. 'unit: {self.name}:1') — lane disabled")
                continue
            self._slot_map[lane.name] = slot
        self.transport = AceTransport(self.serial_port, self.ace_unit_index, self.logger)
        self.transport.start()
        self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)
        self.printer.register_event_handler("klippy:shutdown", self._handle_shutdown)
        self._poll_timer = self.reactor.register_timer(
            self._poll_status, self.reactor.monotonic() + 2.0)

    def _handle_shutdown(self) -> None:
        """On M112/MCU shutdown, halt all ACE motion — a feed in flight would
        otherwise keep pushing up to its commanded length with nobody driving
        the sequence. Fire-and-forget via the transport thread: the reactor
        is not usable for waiting here."""
        if self.transport is None:
            return
        for slot in set(self._slot_map.values()):
            for method in ("stop_feed_filament", "stop_unwind_filament",
                           "stop_feed_assist"):
                self.transport.submit(method, {"index": slot})

    def _handle_disconnect(self) -> None:
        if self.transport:
            self.transport.stop()

    # ── RPC plumbing (reactor side) ─────────────────────────────────

    def _rpc(self, method: str, params: Optional[dict] = None, timeout: float = 8.0) -> Dict[str, Any]:
        if self.transport is None:
            return {"ok": False, "error": "ACE transport not started"}
        result, done, cancelled = self.transport.submit(method, params)
        deadline = self.reactor.monotonic() + timeout
        while not done.is_set():
            if self.reactor.monotonic() > deadline:
                # Abandon the request so a not-yet-sent motion command cannot
                # fire from the queue after we have reported failure
                cancelled.set()
                return {"ok": False, "error": f"timeout waiting for ACE rpc {method}"}
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        return result

    def _get_status(self, max_age: float = 0.0, newer_than: float = 0.0,
                    strict: bool = False) -> Optional[Dict[str, Any]]:
        """ACE status dict, from cache when fresh enough, else via RPC.
        newer_than rejects cache entries fetched before a command was issued —
        age alone can't tell a pre-command snapshot from a live one. strict
        returns None instead of falling back to a stale/pre-command snapshot
        when a fresh read fails, for callers deciding whether motion is safe."""
        status, when = self.transport.cached_status() if self.transport else (None, 0.0)
        if (status is not None and max_age > 0 and when > newer_than
                and (time.time() - when) <= max_age):
            return status
        res = self._rpc("get_status")
        if res.get("ok"):
            payload = res.get("response", {}).get("result")
            if isinstance(payload, dict):
                return payload
        if strict or (status is not None and when <= newer_than):
            return None
        return status

    def _slot_info(self, slot: int, status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        status = status or self._get_status(max_age=HEARTBEAT_S * 2) or {}
        slots = status.get("slots")
        if isinstance(slots, list) and 0 <= slot < len(slots) and isinstance(slots[slot], dict):
            return slots[slot]
        return {}

    def _slot_present(self, slot: int, status: Optional[Dict[str, Any]] = None) -> bool:
        return str(self._slot_info(slot, status).get("status", "")).lower() not in ("", "empty")

    def _motion_active(self, slot: int, status: Dict[str, Any]) -> bool:
        vals = (status.get("status"), status.get("action"),
                self._slot_info(slot, status).get("status"))
        return any(str(v or "").lower() in ACTIVE_STATES for v in vals)

    # ── ACE motion primitives ───────────────────────────────────────

    def _motion_blocked(self, what: str) -> bool:
        if self.monitor_only:
            self.logger.error(
                f"{self.name} is monitor_only — refusing {what}. Remove "
                "monitor_only from its config once its filament path feeds "
                "this printer.")
            return True
        return False

    def _motion_rpc(self, slot: int, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Issue a motion RPC, tolerating a lost or garbled reply frame: the
        command may have landed even though its reply didn't survive. Verify
        via fresh status — motion running means it took; idle means it didn't,
        so resend once. Safe for our motion commands by construction:
        sensor-homed feeds stop on the sensor and are budget-capped; unwinds
        are length-bounded and over-retract parks at the slot."""
        res = self._rpc(method, params)
        if res.get("ok"):
            return res
        self.logger.info(
            f"{method} reply lost/garbled ({res.get('error')}); verifying via status")
        status = self._get_status(strict=True)  # fresh read or nothing
        if status is None:
            # Motion state indeterminate: the command may be executing.
            # Resending non-idempotent motion here could double it — fail.
            return {"ok": False,
                    "error": f"{method} reply lost and motion state indeterminate"}
        if self._motion_active(slot, status):
            return {"ok": True, "assumed_started": True}
        return self._rpc(method, params)

    def _stop_motion(self, slot: int, method: str) -> Dict[str, Any]:
        res = self._rpc(method, {"index": slot})
        # The ACE occasionally eats the reply frame for stop commands while
        # motors wind down; the stop itself still lands. Treat a read timeout
        # as success (verified against hardware by CosmoACE).
        if not res.get("ok") and "timeout" in str(res.get("error", "")).lower():
            self.logger.debug(f"{method} reply timed out; assuming success")
            return {"ok": True, "assumed_success": True}
        return res

    def _wait_motion_idle(self, slot: int, timeout: float) -> bool:
        # The transport heartbeat refreshes status every HEARTBEAT_S, which
        # also keeps the ACE motors alive during the move. Only trust status
        # fetched after this wait began (a snapshot from before the motion
        # command reads idle), and require two consecutive idle reads — the
        # firmware can briefly report idle between command ack and spin-up.
        started = time.time()
        self.reactor.pause(self.reactor.monotonic() + 0.5)
        deadline = self.reactor.monotonic() + timeout
        idle_reads = 0
        while self.reactor.monotonic() < deadline:
            status = self._get_status(max_age=HEARTBEAT_S * 2, newer_than=started, strict=True)
            if status is not None and not self._motion_active(slot, status):
                idle_reads += 1
                if idle_reads >= 2:
                    return True
            else:
                idle_reads = 0
            self.reactor.pause(self.reactor.monotonic() + 0.25)
        return False

    def _feed_until(self, slot: int, sensor_fn: Optional[Callable[[], bool]],
                    length: float, speed: float) -> Tuple[bool, str]:
        """Start an ACE feed and stop it when sensor_fn goes True.

        With a sensor, `length` is the EXPECTED run, not a hard cap: if the
        commanded feed runs out before the sensor trips, the feed is extended
        in chunks. Only a generous safety budget (a dead feeder or wrong path
        would otherwise grind forever) fails the move. With no sensor, the
        exact length is fed and waited out.
        """
        res = self._motion_rpc(slot, "feed_filament", {"index": slot, "length": int(length), "speed": int(speed)})
        if not res.get("ok"):
            return False, f"feed_filament failed: {res.get('error')}"
        if sensor_fn is None:
            if not self._wait_motion_idle(slot, length / max(speed, 1.0) + 30.0):
                self._stop_motion(slot, "stop_feed_filament")
                return False, "timeout waiting for blind feed to finish"
            return True, ""
        budget = length * 2.0 + 500.0
        fed = length
        deadline = self.reactor.monotonic() + budget / max(speed, 1.0) + 60.0
        while self.reactor.monotonic() < deadline:
            if sensor_fn():
                stop = self._stop_motion(slot, "stop_feed_filament")
                self._wait_motion_idle(slot, 5.0)
                if not stop.get("ok"):
                    return False, f"stop_feed_filament failed: {stop.get('error')}"
                # Confirm the sensor held through the stop wind-down
                self.reactor.pause(self.reactor.monotonic() + 0.5)
                if not sensor_fn():
                    return False, "sensor lost after feed stop"
                return True, ""
            # Commanded length exhausted without the sensor: extend the feed
            status = self._get_status(max_age=HEARTBEAT_S * 2)
            if status is not None and not self._motion_active(slot, status):
                if fed >= budget:
                    break
                step = min(200.0, budget - fed)
                res = self._motion_rpc(slot, "feed_filament", {"index": slot, "length": int(step),
                                                                "speed": int(speed)})
                if not res.get("ok"):
                    return False, f"feed extension failed: {res.get('error')}"
                fed += step
                # Give the firmware a moment to report busy again, or the
                # stale-idle cache would machine-gun extension chunks
                self.reactor.pause(self.reactor.monotonic() + 0.6)
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        self._stop_motion(slot, "stop_feed_filament")
        return False, (f"sensor did not trigger within {fed:.0f}mm "
                       f"(expected ~{length:.0f}mm) — check the filament path")

    def _retract(self, slot: int, length: float, speed: float,
                 wait: bool = True) -> Tuple[bool, str]:
        res = self._motion_rpc(slot, "unwind_filament", {"index": slot, "length": int(length),
                                                          "speed": int(speed), "mode": self.retract_mode})
        if not res.get("ok"):
            return False, f"unwind_filament failed: {res.get('error')}"
        if wait and not self._wait_motion_idle(slot, length / max(speed, 1.0) + 30.0):
            self._stop_motion(slot, "stop_unwind_filament")
            return False, "timeout waiting for retract to finish"
        return True, ""

    def _sync_load(self, slot: int, cur_extruder: AFCExtruder, length: float,
                   chunk: float = 5.0) -> None:
        """Thread filament into the extruder gears: fire-and-forget ACE feed
        chunks matched to extruder moves at the same speed, so the ACE pushes
        while the extruder pulls."""
        fed = 0.0
        speed = max(cur_extruder.tool_load_speed, 1.0)
        while fed < length:
            step = min(chunk, length - fed)
            self._motion_rpc(slot, "feed_filament", {"index": slot, "length": int(round(step)),
                                                      "speed": int(speed)})
            self.afc.move_e_pos(step, speed, "sync load", wait_tool=True)
            fed += step
        self._wait_motion_idle(slot, 10.0)

    def _set_assist(self, slot: int, enable: bool) -> None:
        if enable:
            self._rpc("start_feed_assist", {"index": slot})
        else:
            self._stop_motion(slot, "stop_feed_assist")

    # ── sensors ─────────────────────────────────────────────────────

    def _named_sensor_fn(self, sensor_name: str) -> Optional[Callable[[], bool]]:
        """State reader for an existing [filament_switch_sensor <name>]."""
        try:
            sensor = self.printer.lookup_object(f"filament_switch_sensor {sensor_name}")
        except Exception:
            self.logger.error(
                f"{self.name}: filament_switch_sensor '{sensor_name}' not found in config")
            return None
        return lambda: bool(sensor.runout_helper.filament_present)

    def _homing_sensors(self, lane: AFCLane) -> list:
        """Sensor checkpoints between splitter and extruder gears, in path
        order: hub sensor first (AFC_hub pin, else hub_sensor_name), then the
        pre-gear toolhead sensor (pin_tool_start, else toolhead_sensor_name)."""
        sensors = []
        hub = lane.hub_obj
        if hub is not None and not hub.is_virtual_pin():
            sensors.append(("hub", lambda: bool(hub.state)))
        elif self.hub_sensor_name:
            fn = self._named_sensor_fn(self.hub_sensor_name)
            if fn is not None:
                sensors.append(("hub", fn))
        toolhead_fn = self._toolhead_sensor_fn(lane)
        if toolhead_fn is not None:
            sensors.append(("tool_start", toolhead_fn))
        return sensors

    def _toolhead_sensor_fn(self, lane: AFCLane) -> Optional[Callable[[], bool]]:
        """Pre-gear toolhead sensor reader: AFC's pin_tool_start when
        configured, else the named existing sensor, else None."""
        extruder = lane.extruder_obj
        if extruder is not None and extruder.tool_start and extruder.tool_start != "buffer":
            return lane.get_toolhead_pre_sensor_state
        if self.toolhead_sensor_name:
            return self._named_sensor_fn(self.toolhead_sensor_name)
        return None

    # ── status polling / lane sync ──────────────────────────────────

    def _poll_status(self, eventtime: float) -> float:
        if self._operation_active or self.transport is None:
            return eventtime + 2.0
        status, when = self.transport.cached_status()
        if status is None or (time.time() - when) > 10.0:
            return eventtime + 2.0
        # Sync the dryer heater + humidity objects for UIs
        try:
            self.dryer_heater.temperature = float(status.get("temp") or 0.0)
            # ACE firmware reports this as "dryer_status" ("dryer" on some builds)
            dryer = status.get("dryer_status") or status.get("dryer") or {}
            if not isinstance(dryer, dict):
                dryer = {}
            if str(dryer.get("status", "")).lower() in ("stop", "stopped", ""):
                self.dryer_heater.target = 0.0
            else:
                self.dryer_heater.target = float(dryer.get("target_temp") or 0.0)
            if status.get("humidity") is not None:
                self.humidity_sensor.humidity = float(status["humidity"])
        except (TypeError, ValueError):
            pass
        for lane_name, lane in self.lanes.items():
            slot = self._slot_map.get(lane_name)
            if slot is None:
                continue
            present = self._slot_present(slot, status)
            lane.prep_state = present
            # Virtual-hub idiom (same as OpenAMS): raw _load_state is live
            # hub-path occupancy (the virtual hub aggregates it — True only
            # while tool-loaded); loaded_to_hub is the latched "staged and
            # loadable" flag TOOL_LOAD gates on — a present spool is staged.
            lane._load_state = bool(lane.tool_loaded)
            lane.loaded_to_hub = present
            prev = self._last_prep.get(slot)
            if prev is not None and present != prev:
                if present:
                    self._seed_spool_weight(lane)
                    # Fresh physical spool: its RFID tag is ground truth
                    self._apply_rfid(lane, self._slot_info(slot, status), force=True)
                    try:
                        lane.send_lane_data()
                    except Exception:
                        pass
                    self._tail_pending.discard(lane_name)
                    lane.handle_load_runout(eventtime, True)
                elif lane.tool_loaded:
                    # Spool ran out while loaded: an ungripped tail now spans
                    # slot->nozzle, so an immediate swap could not retract it.
                    # Keep consuming the tail; the swap fires when the hub
                    # sensor clears (the extruder still grips the remainder).
                    self.logger.info(
                        f"{self.name}: {lane_name} spool ran out — printing out the "
                        "tail, endless-spool swap fires when the hub sensor clears")
                    self._set_assist(slot, False)
                    self._tail_pending.add(lane_name)
                else:
                    # Spool removed from an idle slot
                    lane.handle_load_runout(eventtime, False)
            self._last_prep[slot] = present

            # Endless-spool stage 2: tail end passed the first checkpoint
            # sensor (hub, or the toolhead sensor standing in for it)
            if lane_name in self._tail_pending:
                sensors = self._homing_sensors(lane)
                checkpoint = sensors[0] if sensors else None
                if checkpoint is None or not checkpoint[1]():
                    self._tail_pending.discard(lane_name)
                    # Below a hub sensor the tail is unsensed and the next
                    # load must push it through; past a toolhead sensor only
                    # ~tool_stn remains, which the normal load engagement
                    # (sync-load) pushes through by itself.
                    if checkpoint is not None and checkpoint[0] == "hub":
                        self._tail_in_bowden = True
                    self.logger.info(
                        f"{self.name}: {lane_name} tail passed the "
                        f"{checkpoint[0] if checkpoint else 'path'} — starting swap")
                    lane.handle_load_runout(eventtime, False)
        return eventtime + 1.5

    def _apply_rfid(self, lane: AFCLane, info: Dict[str, Any], force: bool = False) -> None:
        """Apply a slot's RFID data (material type, color) to its lane.

        On a fresh insert the tag is ground truth (force=True, overwrite); at
        PREP only fill in blanks so user edits persisted in AFC.var win. The
        material setter derives filament density automatically.
        """
        changed = False
        material = str(info.get("type") or "").strip()
        if material and (force or not lane.material):
            lane.material = material
            changed = True
        rgb = info.get("color")
        if (isinstance(rgb, (list, tuple)) and len(rgb) >= 3 and any(rgb)
                and (force or not lane.color)):
            lane.color = "#%02X%02X%02X" % tuple(int(c) & 0xFF for c in rgb[:3])
            changed = True
        if changed:
            try:
                lane.send_lane_data()
            except Exception:
                pass

    def _seed_spool_weight(self, lane: AFCLane) -> None:
        """A spool with weight 0 renders as an empty reel in the UIs (fill
        percent = weight/full) and disables AFC's weight-based runout logic.
        The ACE has no scale, so seed a freshly seen spool with the unit's
        full_weight default; user-set weights are never touched."""
        if not getattr(lane, "weight", 0):
            lane.weight = self.full_weight

    # ── AFC unit hooks ──────────────────────────────────────────────

    def prep_load(self, lane: AFCLane) -> None:
        pass  # ACE firmware stages filament at the slot on insert

    def prep_post_load(self, lane: AFCLane) -> None:
        pass

    def lane_move(self, cur_lane: AFCLane, distance: float, speed_mode: SpeedMode) -> None:
        """LANE_MOVE support: positive feeds toward the toolhead, negative
        retracts toward the spool, firmware-driven."""
        if self._motion_blocked("LANE_MOVE"):
            return
        slot = self._slot_map.get(cur_lane.name)
        if slot is None:
            return
        if distance >= 0:
            ok, msg = self._feed_until(slot, None, distance, self.feed_speed)
        else:
            ok, msg = self._retract(slot, -distance, self.retract_speed)
        if not ok:
            self.logger.error(f"ACE lane_move failed for {cur_lane.name}: {msg}")

    def lane_unload(self, cur_lane: AFCLane) -> Optional[bool]:
        """Reset-path unload (AFC_RESET): retract until the homing sensors
        clear, then park behind the splitter."""
        if self._motion_blocked("lane unload"):
            return None
        slot = self._slot_map.get(cur_lane.name)
        if slot is None:
            return None
        self._operation_active = True
        try:
            ok, msg = self._retract_clear_of_sensors(cur_lane, slot)
            if not ok:
                self.logger.error(f"ACE lane_unload failed for {cur_lane.name}: {msg}")
            cur_lane.loaded_to_hub = False
            cur_lane._load_state = False
        finally:
            self._operation_active = False
        return ok

    def eject_lane(self, lane: AFCLane) -> None:
        """Full retract back to the slot (unlike a toolchange unload, which
        parks the tip just behind the splitter for a fast reload)."""
        slot = self._slot_map.get(lane.name)
        if slot is None or self._motion_blocked("eject"):
            return
        self._operation_active = True
        try:
            bowden = lane.hub_obj.afc_bowden_length if lane.hub_obj else 500.0
            dist_hub = getattr(lane, "dist_hub", 0.0) or 0.0
            # Over-length is safe: the ACE parks the filament at the slot
            ok, msg = self._retract(slot, dist_hub + bowden + self.bowden_overshoot,
                                    self.retract_speed)
            if not ok:
                self.logger.error(f"ACE eject failed for {lane.name}: {msg}")
                return
            lane.loaded_to_hub = False
            lane._load_state = False
        finally:
            self._operation_active = False
        self.logger.info(
            f"Lane {lane.name} retracted to the slot. Press the slot lever on "
            f"{self.name} to release and remove the spool.")

    def get_lane_reset_command(self, lane: AFCLane, dis: float) -> None:
        return None

    # ── load ────────────────────────────────────────────────────────

    def unit_load_lane(self, cur_lane: AFCLane, cur_extruder: AFCExtruder) -> bool:
        """Full spool→nozzle load; replaces AFC's stepper load path. The
        caller then sets tool_loaded, enables the buffer and purges."""
        if self._motion_blocked("TOOL_LOAD"):
            return False
        # Pre-flight: a lit path sensor at load start means untracked filament
        # occupies the bowden (e.g. tool state lost after a killed print) —
        # feeding into it would double-load. A spent tail is the one tracked
        # exception (the push-through branch consumes it). No auto-retract
        # here: the ACE may not even grip whatever is in the tube.
        if not self._tail_in_bowden:
            occupied = [name for name, fn in self._homing_sensors(cur_lane) if fn()]
            if occupied:
                self.afc.error.handle_lane_failure(
                    cur_lane,
                    f"Refusing to load {cur_lane.name}: {'/'.join(occupied)} sensor "
                    "already shows filament but no lane is tool-loaded. Clear the "
                    "path, or if filament is legitimately loaded run "
                    "SET_LANE_LOADED LANE=<lane>.",
                    pause=self.afc.function.in_print())
                return False
        self._operation_active = True
        try:
            if not self._load_inner(cur_lane, cur_extruder):
                # Best-effort rescue: pull whatever was fed back to the slot
                # so the failure leaves a clean path (over-length is safe).
                slot = self._slot_map.get(cur_lane.name)
                if slot is not None:
                    self._stop_motion(slot, "stop_feed_filament")
                    ok, msg = self._retract_clear_of_sensors(cur_lane, slot)
                    if ok:
                        cur_lane.loaded_to_hub = False
                        cur_lane._load_state = False
                    else:
                        self.logger.error(f"ACE post-failure retract: {msg}")
                self.afc.error.handle_lane_failure(
                    cur_lane, f"ACE load failed for {cur_lane.name}",
                    pause=self.afc.function.in_print())
                return False
            return True
        finally:
            self._operation_active = False

    def _load_inner(self, cur_lane: AFCLane, cur_extruder: AFCExtruder) -> bool:
        slot = self._slot_map.get(cur_lane.name)
        if slot is None:
            self.logger.error(f"No ACE slot mapped for {cur_lane.name}")
            return False
        info = self._slot_info(slot, self._get_status())
        state = str(info.get("status", "")).lower()
        if state != "ready":
            self.logger.error(
                f"ACE slot {slot + 1} on {self.name} is not ready (status={state or 'unknown'})")
            return False

        # Path model (standard AFC semantics):
        #   slot --dist_hub--> hub sensor --afc_bowden_length--> toolhead
        # dist_hub and the bowden act as command caps when a sensor stops the
        # feed, and as exact blind distances when a checkpoint has no sensor.
        bowden = cur_lane.hub_obj.afc_bowden_length if cur_lane.hub_obj else 500.0
        dist_hub = getattr(cur_lane, "dist_hub", 0.0) or 0.0
        sensors = dict(self._homing_sensors(cur_lane))
        hub_fn = sensors.get("hub")
        tool_fn = sensors.get("tool_start")

        cur_lane.status = AFCLaneState.TOOL_LOADING
        self.lane_loading(cur_lane)

        # Stage 1: slot -> hub sensor
        if hub_fn is not None and not hub_fn():
            ok, msg = self._feed_until(slot, hub_fn, dist_hub + self.bowden_overshoot,
                                       self.feed_speed)
            if not ok:
                self.logger.error(f"ACE feed to hub sensor failed for {cur_lane.name}: {msg}")
                return False
            self.afc.afcDeltaTime.log_with_time("ACE fed to hub sensor")

        # Stage 2: hub -> toolhead. With no hub sensor, stage 1 was skipped
        # and this feed starts at the slot, so the cap must cover dist_hub too.
        stage2_cap = bowden + (0.0 if hub_fn is not None else dist_hub) + self.bowden_overshoot
        if self._tail_in_bowden:
            # A spent spool's tail occupies hub->nozzle: push it through the
            # melt zone with the extruder in step (this also engages the new
            # filament — its tip ends at the nozzle), purging over the tray.
            cur_lane.sync_to_extruder()
            if not self._tail_push(slot, cur_extruder,
                                   bowden + cur_extruder.tool_stn):
                return False
            self._tail_in_bowden = False
            cur_lane.loaded_to_hub = True
            cur_lane._load_state = True
            self.afc.afcDeltaTime.log_with_time("Tail pushed through, new filament at nozzle")
            self._set_assist(slot, True)
            cur_lane.status = AFCLaneState.TOOL_LOADED
            self.afc.save_vars()
            return True
        if tool_fn is not None:
            if not tool_fn():
                ok, msg = self._feed_until(slot, tool_fn, stage2_cap,
                                           self.feed_speed)
                if not ok:
                    self.logger.error(
                        f"ACE feed to tool_start sensor failed for {cur_lane.name}: {msg}")
                    return False
                self.afc.afcDeltaTime.log_with_time("ACE fed to tool_start sensor")
        else:
            # No toolhead sensor: blind push the tuned hub->toolhead length
            # (plus slot->hub when there is no hub sensor either).
            blind = bowden + (0.0 if hub_fn is not None else dist_hub)
            self.logger.info(f"{self.name}: no tool_start sensor — blind feeding {blind:.0f}mm")
            ok, msg = self._feed_until(slot, None, blind, self.feed_speed)
            if not ok:
                self.logger.error(f"ACE blind feed failed: {msg}")
                return False
        cur_lane.loaded_to_hub = True
        cur_lane._load_state = True  # filament now occupies the hub path

        # Toolhead engagement. The filament tip sits at the extruder inlet and
        # nothing grips it yet, so the extruder cannot pull it in alone: feed
        # the ACE in small chunks in step with the extruder (CosmoACE's
        # sync-load) until the gears have it, covering the tool_stn distance.
        # This extrudes through the hotend, so park over the purge area first.
        if self.afc.park and self.afc.park_cmd:
            self.gcode.run_script_from_command(self.afc.park_cmd)
        cur_lane.sync_to_extruder()
        self._sync_load(slot, cur_extruder, cur_extruder.tool_stn)
        if cur_extruder.tool_end:
            attempts = 0
            while not cur_extruder.tool_end_state:
                attempts += 1
                if attempts > 20:
                    self.logger.error(
                        f"Filament failed to trigger post-gear sensor for {cur_lane.name}")
                    return False
                self.afc.move_e_pos(cur_lane.short_move_dis, cur_extruder.tool_load_speed,
                                    "Tool end", wait_tool=True)
        self.afc.afcDeltaTime.log_with_time("Filament loaded to nozzle")

        # Print-time buffer mode: the ACE feeds on demand so the extruder
        # never fights the slot's rewind clutch. Stays on while tool-loaded.
        self._set_assist(slot, True)

        cur_lane.status = AFCLaneState.TOOL_LOADED
        self.afc.save_vars()
        return True

    # ── unload ──────────────────────────────────────────────────────

    def unit_unload_lane(self, cur_lane: AFCLane, cur_extruder: AFCExtruder) -> bool:
        """Full nozzle→slot unload; replaces AFC's stepper unload path."""
        if self._motion_blocked("TOOL_UNLOAD"):
            return False
        self._operation_active = True
        try:
            slot = self._slot_map.get(cur_lane.name)
            if slot is None:
                return False

            # Spent spool: the slot is empty and only an ungripped tail
            # remains in the path — there is nothing to cut or retract.
            if not self._slot_present(slot, self._get_status()):
                return self._tail_unload(cur_lane, slot)

            # Assist off FIRST — with assist on, the ACE fights every
            # backward move that follows (CosmoACE hardware lesson)
            self._set_assist(slot, False)

            self.afc.move_e_pos(-2, cur_extruder.tool_unload_speed, "Quick Pull",
                                wait_tool=False)
            cur_lane.status = AFCLaneState.TOOL_UNLOADING
            cur_lane.disable_buffer()
            cur_lane.sync_to_extruder()
            cur_lane.select_lane()
            self.afc.do_tool_cut_tip_form(cur_lane, cur_extruder)

            # Extruder-driven retract until the filament is out of the gears
            # (pre-gear sensor clear). The ACE cannot pull through gripping
            # extruder gears.
            if cur_extruder.tool_stn_unload > 0:
                self.afc.move_e_pos(-cur_extruder.tool_stn_unload,
                                    cur_extruder.tool_unload_speed, "tool stn unload",
                                    wait_tool=True)
            toolhead_fn = self._toolhead_sensor_fn(cur_lane) or (lambda: False)
            attempts = 0
            while toolhead_fn() or cur_extruder.tool_end_state:
                attempts += 1
                if attempts > self.tool_max_unload_attempts:
                    self.afc.error.handle_lane_failure(
                        cur_lane,
                        f"Failed to clear toolhead sensors unloading {cur_lane.name}; "
                        "filament may be stuck in the toolhead",
                        pause=self.afc.function.in_print())
                    return False
                self.afc.move_e_pos(-cur_lane.short_move_dis,
                                    cur_extruder.tool_unload_speed, "clear sensors",
                                    wait_tool=True)
            cur_lane.unsync_to_extruder()
            self.afc.afcDeltaTime.log_with_time("Unloaded from toolhead")

            # ACE-driven retract: pull the bowden empty and park the filament
            # behind the splitter.
            ok, msg = self._unload_retract_exact(cur_lane, slot)
            if not ok:
                self.afc.error.handle_lane_failure(
                    cur_lane, f"ACE retract failed for {cur_lane.name}: {msg}",
                    pause=self.afc.function.in_print())
                return False
            cur_lane.loaded_to_hub = False
            cur_lane._load_state = False
            self.afc.afcDeltaTime.log_with_time("ACE retract complete")

            if self.afc.post_unload_macro is not None:
                self.gcode.run_script_from_command(self.afc.post_unload_macro)
            cur_lane.set_tool_unloaded(normal_toolchange=True)
            cur_lane.status = AFCLaneState.NONE
            self.lane_tool_unloaded(cur_lane)
            self.afc.save_vars()
            return True
        finally:
            self._operation_active = False

    def _tail_unload(self, cur_lane: AFCLane, slot: int) -> bool:
        """Book-keeping unload for a spent spool: the tail stays in the path
        (below the hub) and is pushed through the nozzle by the next load."""
        cur_lane.status = AFCLaneState.TOOL_UNLOADING
        cur_lane.disable_buffer()
        self._set_assist(slot, False)
        cur_lane.unsync_to_extruder()
        self._tail_pending.discard(cur_lane.name)
        # Only a hub-sensor topology leaves unsensed tail below the sensor
        # that the next load must push through; with a toolhead sensor the
        # normal load homing + sync-load consumes the remnant.
        sensor_names = [name for name, _ in self._homing_sensors(cur_lane)]
        if "hub" in sensor_names:
            self._tail_in_bowden = True
        cur_lane.loaded_to_hub = False
        cur_lane._load_state = False
        if self.afc.post_unload_macro is not None:
            self.gcode.run_script_from_command(self.afc.post_unload_macro)
        cur_lane.set_tool_unloaded(normal_toolchange=True)
        cur_lane.status = AFCLaneState.NONE
        self.lane_tool_unloaded(cur_lane)
        self.afc.save_vars()
        self.logger.info(
            f"{cur_lane.name} spent-spool unload: tail left in bowden, "
            "next load pushes it through the nozzle")
        return True

    def _tail_push(self, slot: int, cur_extruder: AFCExtruder, total: float) -> bool:
        """Drive a spent tail through the melt zone: park over the purge
        area, then feed the ACE in chunks matched to extruder moves (the new
        filament pushes from behind while the extruder pulls the tail
        through), kicking the purge blob away after each chunk."""
        if self.afc.park and self.afc.park_cmd:
            self.gcode.run_script_from_command(self.afc.park_cmd)
        speed = self.tail_purge_speed
        pushed = 0.0
        while pushed < total:
            step = min(100.0, total - pushed)
            res = self._motion_rpc(slot, "feed_filament", {"index": slot, "length": int(round(step)),
                                                            "speed": int(speed)})
            if not res.get("ok"):
                self.logger.error(f"Tail push feed failed: {res.get('error')}")
                return False
            self.afc.move_e_pos(step, speed, "tail push", wait_tool=True)
            pushed += step
            if self.afc.kick_cmd:
                self.gcode.run_script_from_command(self.afc.kick_cmd)
        self._wait_motion_idle(slot, 15.0)
        return True

    def _unload_retract_exact(self, cur_lane: AFCLane, slot: int) -> Tuple[bool, str]:
        """Toolchange retract, sensor-referenced but built ONLY from completed
        unwinds (the ACE only respools while an unwind runs to completion —
        stopping mid-move skips the take-up and piles slack inside the unit):

          1. one completed unwind to just short of the hub sensor
          2. short completed steps, checking the sensor between moves, until
             it clears (absorbs any afc_bowden_length inaccuracy)
          3. one completed hub_clear_mm park move past the sensor

        With only a toolhead sensor (already cleared by the extruder moves)
        there is no reference below it: park blind at bowden + hub_clear_mm.
        """
        hub = cur_lane.hub_obj
        bowden = 500.0
        if hub is not None:
            bowden = getattr(hub, "afc_unload_bowden_length", 0) or hub.afc_bowden_length
        sensors = self._homing_sensors(cur_lane)
        hub_ref = sensors[0] if sensors and sensors[0][0] == "hub" else None

        if hub_ref is None:
            # No hub sensor to reference — blind completed park
            ok, msg = self._retract(slot, bowden + self.hub_clear_mm, self.retract_speed)
            if not ok:
                return ok, msg
        else:
            _, hub_fn = hub_ref
            # 1. Approach: stop short so the sensor is still covered
            approach = max(bowden - 60.0, 0.0)
            if approach > 0:
                ok, msg = self._retract(slot, approach, self.retract_speed)
                if not ok:
                    return ok, msg
            # 2. Step past the sensor with completed short moves
            stepped = 0.0
            while hub_fn():
                if stepped >= 300.0:
                    return False, ("hub sensor still triggered after stepping "
                                   f"{stepped:.0f}mm past the expected position — "
                                   "check the filament path")
                ok, msg = self._retract(slot, 25.0, self.retract_speed)
                if not ok:
                    return ok, msg
                stepped += 25.0
                self.reactor.pause(self.reactor.monotonic() + 0.2)
            # 3. Park a known margin behind the sensor
            ok, msg = self._retract(slot, self.hub_clear_mm, self.retract_speed)
            if not ok:
                return ok, msg

        for sensor_name, sensor_fn in sensors:
            # Short debounce grace before declaring stuck filament
            deadline = self.reactor.monotonic() + 2.0
            while sensor_fn() and self.reactor.monotonic() < deadline:
                self.reactor.pause(self.reactor.monotonic() + 0.2)
            if sensor_fn():
                return False, f"{sensor_name} sensor still triggered after retract"
        return True, ""

    def _retract_clear_of_sensors(self, cur_lane: AFCLane, slot: int) -> Tuple[bool, str]:
        """Recovery retract from an UNKNOWN position (AFC_RESET, failed-load
        rescue): home against the sensors, then park behind the splitter.
        The mid-move stop skips the ACE's spool take-up (slack builds inside
        the unit), so this is for error paths only — toolchanges use
        _unload_retract_exact."""
        bowden = cur_lane.hub_obj.afc_bowden_length if cur_lane.hub_obj else 500.0
        dist_hub = getattr(cur_lane, "dist_hub", 0.0) or 0.0
        cap = dist_hub + bowden + self.bowden_overshoot
        sensors = self._homing_sensors(cur_lane)
        if not sensors:
            # No sensors to home against: blind full retract; over-length is
            # safe, the ACE parks the filament at the slot.
            return self._retract(slot, cap + self.hub_clear_mm, self.retract_speed)

        # Home against the first checkpoint in the path — the last sensor to
        # clear on the way back. With a hub sensor the tip is then already at
        # the splitter; homing against a toolhead sensor leaves the whole
        # bowden still occupied, so the park move must clear it too.
        hub_name, hub_fn = sensors[0]
        park = self.hub_clear_mm + (0.0 if hub_name == "hub" else bowden)
        res = self._motion_rpc(slot, "unwind_filament", {"index": slot, "length": int(cap),
                                                          "speed": int(self.retract_speed),
                                                          "mode": self.retract_mode})
        if not res.get("ok"):
            return False, f"unwind_filament failed: {res.get('error')}"
        deadline = self.reactor.monotonic() + cap / max(self.retract_speed, 1.0) + 30.0
        while self.reactor.monotonic() < deadline:
            if not hub_fn():
                break
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        else:
            self._stop_motion(slot, "stop_unwind_filament")
            return False, f"{hub_name} sensor did not clear during retract"
        stop = self._stop_motion(slot, "stop_unwind_filament")
        self._wait_motion_idle(slot, 5.0)
        if not stop.get("ok"):
            return False, f"stop_unwind_filament failed: {stop.get('error')}"
        # Park just behind the splitter so the next lane can load
        ok, msg = self._retract(slot, park, self.retract_speed)
        if not ok:
            return ok, msg
        for sensor_name, sensor_fn in sensors:
            if sensor_fn():
                return False, f"{sensor_name} sensor re-triggered after retract"
        return True, ""

    # ── PREP / system test ──────────────────────────────────────────

    def system_Test(self, cur_lane: AFCLane, delay: float, assignTcmd: bool,
                    enable_movement: bool) -> bool:
        msg = ''
        succeeded = True
        status = self._get_status()
        slot = self._slot_map.get(cur_lane.name, -1)

        if status is None:
            msg = '<span class=error--text>ACE NOT CONNECTED</span>'
            if self.transport and self.transport.last_error:
                msg += f' ({self.transport.last_error})'
            succeeded = False
        else:
            present = self._slot_present(slot, status)
            cur_lane.prep_state = present
            cur_lane._load_state = bool(cur_lane.tool_loaded)
            cur_lane.loaded_to_hub = present
            self._last_prep[slot] = present
            if present:
                self._seed_spool_weight(cur_lane)
                # Fill blanks from RFID; AFC.var-persisted user edits win
                self._apply_rfid(cur_lane, self._slot_info(slot, status))
                # Re-publish to moonraker's lane_data namespace — AFC wipes it
                # on every moonraker connect and units must push their lanes
                # back (Orca's filament sync reads this)
                try:
                    cur_lane.send_lane_data()
                except Exception:
                    pass
                self.lane_loaded(cur_lane)
                cur_lane.status = AFCLaneState.LOADED
                msg += "<span class=success--text>SPOOL READY</span>"
                if (cur_lane.tool_loaded
                        and cur_lane.extruder_obj.lane_loaded == cur_lane.name):
                    cur_lane.sync_to_extruder()
                    if self.afc.current == cur_lane.name:
                        self.afc.spool.set_active_spool(cur_lane.spool_id)
                        self.lane_tool_loaded(cur_lane)
                        cur_lane.status = AFCLaneState.TOOLED
                        cur_lane.enable_buffer()
                        # Re-arm print-time feed assist for the loaded lane
                        if not self.monitor_only:
                            self._set_assist(slot, True)
                    else:
                        self.lane_tool_loaded_idle(cur_lane)
                    self.printer.send_event("afc:tool_loaded", cur_lane)
            else:
                if cur_lane.tool_loaded:
                    # Booted mid-tail: spool spent but its filament is still
                    # loaded — with a hub sensor the next load must push the
                    # unsensed tail through
                    if any(n == "hub" for n, _ in self._homing_sensors(cur_lane)):
                        self._tail_in_bowden = True
                    msg += "<span class=warning--text>SPENT SPOOL (tail loaded) </span>"
                if not cur_lane.remember_spool:
                    self.afc.spool.clear_values(cur_lane)
                self.afc.function.afc_led(cur_lane.led_not_ready, cur_lane.led_index)
                msg += 'EMPTY READY FOR SPOOL'

        if assignTcmd:
            self.afc.function.TcmdAssign(cur_lane)
        cur_lane.do_enable(False)
        self.logger.info('{lane_name} tool cmd: {tcmd:3} {msg}'.format(
            lane_name=cur_lane.name, tcmd=cur_lane.map, msg=msg))
        cur_lane.set_afc_prep_done()
        return succeeded

    # ── calibration ─────────────────────────────────────────────────

    def calibrate_bowden(self, cur_lane: AFCLane, dis, tol):
        """Measure afc_bowden_length by feeding in steps until the first
        homing sensor triggers. The ACE has no odometry, so commanded step
        lengths are the measurement."""
        if self._motion_blocked("bowden calibration"):
            return
        slot = self._slot_map.get(cur_lane.name)
        sensors = self._homing_sensors(cur_lane)
        if slot is None or not sensors:
            self.logger.error("Bowden calibration needs a hub or tool_start sensor")
            return
        sensor_name, sensor_fn = sensors[0]
        if sensor_fn():
            self.logger.error(
                f"{sensor_name} sensor already triggered — unload first")
            return
        self._operation_active = True
        try:
            step, fed = 50.0, 0.0
            while not sensor_fn() and fed < 3000.0:
                ok, msg = self._feed_until(slot, None, step, self.feed_speed)
                if not ok:
                    self.logger.error(f"Calibration feed failed: {msg}")
                    return
                fed += step
            if not sensor_fn():
                self.logger.error("Sensor never triggered within 3000mm")
                return
            knob = (f"dist_hub: {fed:.0f} under [AFC_lane {cur_lane.name}]"
                    if sensor_name == "hub"
                    else f"afc_bowden_length: {fed:.0f} under [AFC_hub {cur_lane.hub}]")
            self.logger.info(
                f"ACE fed ~{fed:.0f}mm (±{step:.0f}) to the {sensor_name} sensor. Set {knob}")
            ok, msg = self._retract(slot, fed + self.hub_clear_mm, self.retract_speed)
            if not ok:
                self.logger.error(f"Calibration retract failed: {msg}")
        finally:
            self._operation_active = False

    def calibration_lane_message(self) -> str:
        return ("ACE lanes have no length calibration; run UNIT_BOW_CALIBRATION "
                "to measure the bowden length.")

    # ── gcode commands ──────────────────────────────────────────────

    def set_dryer(self, temp: int, duration: Optional[int] = None,
                  fan_speed: Optional[int] = None) -> None:
        """Start (temp > 0) or stop (temp <= 0) the ACE dryer."""
        if self.printer.is_shutdown():
            # turn_off_all_heaters during M112 lands here; the reactor can't
            # be waited on, so stop fire-and-forget via the transport thread
            if self.transport is not None and temp <= 0:
                self.transport.submit("drying_stop")
                self.dryer_heater.target = 0.0
            return
        if temp > 0:
            temp = min(temp, 65)  # ACE firmware limit
            res = self._rpc("drying", {"temp": temp,
                                       "fan_speed": fan_speed or self.dryer_fan_speed,
                                       "duration": duration or self.dryer_duration})
            if res.get("ok"):
                self.dryer_heater.target = float(temp)
                self.logger.info(f"{self.name} dryer set to {temp}C "
                                 f"for {duration or self.dryer_duration} minutes")
            else:
                self.logger.error(f"Dryer start failed: {res.get('error')}")
        else:
            res = self._rpc("drying_stop")
            if res.get("ok"):
                self.dryer_heater.target = 0.0
                self.logger.info(f"{self.name} dryer stopped")
            else:
                self.logger.error(f"Dryer stop failed: {res.get('error')}")

    def _cmd_SET_DRYER_TEMPERATURE(self, gcmd: GCodeCommand) -> None:
        self.set_dryer(int(gcmd.get_float("TARGET", 0.0)))

    def cmd_AFC_ACE_DRYER_START(self, gcmd: GCodeCommand) -> None:
        self.set_dryer(gcmd.get_int("TEMP", self.dryer_temp),
                       gcmd.get_int("DURATION", self.dryer_duration),
                       gcmd.get_int("FAN_SPEED", self.dryer_fan_speed))

    def cmd_AFC_ACE_DRYER_STOP(self, gcmd: GCodeCommand) -> None:
        self.set_dryer(0)

    def cmd_AFC_ACE_STATUS(self, gcmd: GCodeCommand) -> None:
        status = self._get_status()
        if status is None:
            err = self.transport.last_error if self.transport else "transport not started"
            self.logger.info(f"{self.name}: not connected ({err})")
        else:
            self.logger.info(f"{self.name}: {json.dumps(status, indent=1)}")

    def cmd_AFC_ACE_STOP(self, gcmd: GCodeCommand) -> None:
        lane_name = gcmd.get("LANE")
        slot = self._slot_map.get(lane_name)
        if slot is None:
            self.logger.error(f"Unknown lane {lane_name}")
            return
        self._stop_motion(slot, "stop_feed_filament")
        self._stop_motion(slot, "stop_unwind_filament")
        self._stop_motion(slot, "stop_feed_assist")

    # ── status for UI ───────────────────────────────────────────────

    def get_status(self, eventtime=None):
        response = super().get_status(eventtime)
        status, _ = self.transport.cached_status() if self.transport else (None, 0.0)
        response["connected"] = bool(self.transport and self.transport.connected)
        if status is not None:
            response["ace"] = {
                "temp": status.get("temp"),
                "humidity": status.get("humidity"),
                "dryer": status.get("dryer_status") or status.get("dryer"),
                "slots": status.get("slots"),
            }
        return response


def load_config_prefix(config: ConfigWrapper) -> afcACE:
    return afcACE(config)
