# AFCProject Automated Filament Changer Software
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# This file include code inspired/modified from OpenAms Project. https://github.com/OpenAMSOrg/klipper_openams
# Originally authored by JR Lomas(aka KnightRadiant) and licensed under the MIT license
# Full license text available at: https://mit-license.org/

# This code was updated and contributed by lindnjoe(aka J0eB0l)

"""
AFC unit driver for OpenAMS filament changers with stuck spool,
clog detection, and engagement verification.
"""

from __future__ import annotations

import time
import threading
import traceback
from datetime import datetime
from configparser import Error as error
from enum import IntEnum
from typing import Any, Callable, Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from gcode import GCodeCommand
    from klippy import Printer
    from reactor import SelectReactor as Reactor
    from extras.AFC_lane import AFCLane, SpeedMode
    from extras.AFC_extruder import AFCExtruder
    from extras.AFC_hub import afc_hub
    from extras.AFC_OAMS import AFC_OAMS
    from extras.AFC_logger import AFC_logger
    from extras.AFC_buffer import AFCFPSBuffer
    from kinematics.extruder import PrinterExtruder

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCLaneState
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try: from extras.AFC_unit import afcUnit
except: raise error(ERROR_STR.format(import_lib="AFC_unit", trace=traceback.format_exc()))

# FollowerController, OAMSMonitor and FPSLoadState/FPSState are defined inline
# at the bottom of this file so the OpenAMS unit is self-contained. The [oams]
# hardware controller lives in its own AFC_OAMS.py because Klipper resolves the
# [AFC_OAMS ...] config section to that module name.

class OAMSStatus(IntEnum):
    """
    Enumeration of firmware action/status codes reported by the OAMS MCU.

    These values mirror the ``action`` field of ``oams_action_status`` MCU
    messages and the local ``action_status`` used to track in-flight operations.
    """
    LOADING = 0
    UNLOADING = 1
    FORWARD_FOLLOWING = 2
    REVERSE_FOLLOWING = 3
    COASTING = 4
    STOPPED = 5
    CALIBRATING = 6
    ERROR = 7

class AMSEventBus:
    """
    Process-wide singleton publish/subscribe bus for OpenAMS events.

    Subscribers are stored per event type ordered by descending priority and
    invoked synchronously on ``publish``. A bounded, TTL-pruned history of
    recent events is retained for diagnostics.
    """
    _instance = None
    _lock = threading.RLock()
    _MAX_HISTORY = 500
    _HISTORY_TTL = 3600.0

    def __init__(self) -> None:
        """
        Initialize an empty subscriber map, event history, and logger slot.
        """
        self._subscribers: Dict[str, list[Tuple[Callable, int]]] = {}
        self._event_history: list[Tuple[str, float, dict]] = []
        self.logger: Optional[AFC_logger] = None

    @classmethod
    def get_instance(cls, logger: Optional[AFC_logger] = None) -> AMSEventBus:
        """
        Return the shared event bus singleton, creating it on first call.

        :param logger: optional logger attached if the singleton has none yet.
        :return AMSEventBus: the process-wide event bus instance.
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            if logger is not None and cls._instance.logger is None:
                cls._instance.logger = logger
            return cls._instance

    def subscribe(self, event_type: str, callback: Callable, *, priority: int = 0) -> None:
        """
        Register a callback for an event type, ordered by priority.

        :param event_type: event name to listen for.
        :param callback: callable invoked as ``callback(event_type=..., **kwargs)``.
        :param priority: higher priority callbacks run earlier (default 0).
        """
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            subscribers = self._subscribers[event_type]
            insert_idx = 0
            for i, (existing_callback, existing_priority) in enumerate(subscribers):
                if priority > existing_priority:
                    insert_idx = i
                    break
                insert_idx = i + 1
            subscribers.insert(insert_idx, (callback, priority))

    def publish(self, event_type: str, **kwargs: Any) -> int:
        """
        Record an event in history and dispatch it to all subscribers.
        Exceptions raised by individual subscribers are swallowed so one bad
        callback cannot block the others.

        :param event_type: event name to publish.
        :param kwargs: event payload; ``eventtime`` defaults to ``time.time()``.
        :return int: number of subscribers that handled the event successfully.
        """
        eventtime = kwargs.get('eventtime', time.time())
        with self._lock:
            self._event_history.append((event_type, eventtime, dict(kwargs)))
            if len(self._event_history) > self._MAX_HISTORY:
                cutoff = time.monotonic() - self._HISTORY_TTL
                self._event_history = [e for e in self._event_history if e[1] > cutoff]
                if len(self._event_history) > self._MAX_HISTORY:
                    self._event_history = self._event_history[-self._MAX_HISTORY:]
            subscribers = list(self._subscribers.get(event_type, []))
        if not subscribers:
            return 0
        success_count = 0
        for callback, priority in subscribers:
            try:
                callback(event_type=event_type, **kwargs)
                success_count += 1
            except Exception:
                pass
        return success_count


class LaneInfo:
    """
    Immutable-ish record describing one registered OpenAMS lane.

    Bundles the identifiers and optional hardware associations (FPS, hub, LED,
    custom load/unload commands) used by ``LaneRegistry`` for lookups.
    """
    def __init__(self, lane_name: str, unit_name: str, spool_index: int, extruder: str,
                 fps_name: Optional[str] = None, hub_name: Optional[str] = None,
                 led_index: Optional[int] = None) -> None:
        """
        Store the lane's identifiers and optional hardware associations.

        :param lane_name: AFC lane name.
        :param unit_name: owning OpenAMS unit name.
        :param spool_index: zero-based spool bay index within the unit.
        :param extruder: extruder name the lane feeds.
        :param fps_name: optional FPS buffer name.
        :param hub_name: optional hub name.
        :param led_index: optional LED index for the lane.
        """
        self.lane_name = lane_name
        self.unit_name = unit_name
        self.spool_index = spool_index
        self.extruder = extruder
        self.fps_name = fps_name
        self.hub_name = hub_name
        self.led_index = led_index

class LaneRegistry:
    """
    Per-printer registry mapping OpenAMS lanes to spools and extruders.

    Maintains lookup indexes (by lane name, by (unit, spool) pair, by extruder)
    so the hardware service can resolve lane identities from firmware events.
    One instance exists per printer object.
    """
    _instances: Dict[int, LaneRegistry] = {}
    _lock = threading.RLock()

    def __init__(self, printer: Printer, logger: Optional[AFC_logger] = None) -> None:
        """
        Initialize empty lane lists and lookup indexes.

        :param printer: Klipper printer object this registry serves.
        :param logger: optional AFC logger instance.
        """
        self.printer: Printer = printer
        self.logger: Optional[AFC_logger] = logger
        self._lanes: list[LaneInfo] = []
        self._by_lane_name: Dict[str, LaneInfo] = {}
        self._by_lane_name_lower: Dict[str, LaneInfo] = {}
        self._by_spool: Dict[Tuple[str, int], LaneInfo] = {}
        self._by_extruder: Dict[str, list[LaneInfo]] = {}
        self.event_bus = AMSEventBus.get_instance(logger=self.logger)

    @classmethod
    def for_printer(cls, printer: Printer, logger: Optional[AFC_logger] = None) -> LaneRegistry:
        """
        Return the registry for a printer, creating it on first use.

        :param printer: Klipper printer object used as the instance key.
        :param logger: optional logger attached/updated on the instance.
        :return LaneRegistry: the per-printer registry singleton.
        """
        with cls._lock:
            key = id(printer)
            if key not in cls._instances:
                cls._instances[key] = cls(printer, logger=logger)
            elif logger is not None:
                cls._instances[key].logger = logger
            return cls._instances[key]

    def register_lane(self, lane_name: str, unit_name: str, spool_index: int, extruder: str, *,
                      fps_name: Optional[str] = None, hub_name: Optional[str] = None,
                      led_index: Optional[int] = None) -> LaneInfo:
        """
        Register (or re-register) a lane and index it for fast lookup.
        Any existing registration for ``lane_name`` is removed first.

        :param lane_name: AFC lane name.
        :param unit_name: owning OpenAMS unit name.
        :param spool_index: zero-based spool bay index.
        :param extruder: extruder name the lane feeds.
        :param fps_name: optional FPS buffer name.
        :param hub_name: optional hub name.
        :param led_index: optional LED index.
        :return LaneInfo: the newly created lane record.
        """
        with self._lock:
            existing = self._by_lane_name.get(lane_name)
            if existing is not None:
                self._unregister_lane(existing)
            info = LaneInfo(
                lane_name=lane_name, unit_name=unit_name,
                spool_index=spool_index, extruder=extruder,
                fps_name=fps_name, hub_name=hub_name, led_index=led_index
            )
            self._lanes.append(info)
            self._by_lane_name[lane_name] = info
            self._by_lane_name_lower[lane_name.lower()] = info
            self._by_spool[(unit_name, spool_index)] = info
            if extruder not in self._by_extruder:
                self._by_extruder[extruder] = []
            self._by_extruder[extruder].append(info)
            return info

    def _unregister_lane(self, info: LaneInfo) -> None:
        """
        Remove a lane record from all lookup indexes.

        :param info: the LaneInfo record to remove.
        """
        if info in self._lanes:
            self._lanes.remove(info)
        self._by_lane_name.pop(info.lane_name, None)
        self._by_lane_name_lower.pop(info.lane_name.lower(), None)
        self._by_spool.pop((info.unit_name, info.spool_index), None)
        extruder_lanes = self._by_extruder.get(info.extruder, [])
        if info in extruder_lanes:
            extruder_lanes.remove(info)
            if not extruder_lanes:
                self._by_extruder.pop(info.extruder, None)

    def get_by_spool(self, unit_name: str, spool_index: int) -> Optional[LaneInfo]:
        """
        Return the LaneInfo for a (unit, spool) pair, or None.

        :param unit_name: owning OpenAMS unit name.
        :param spool_index: spool bay index.
        :return LaneInfo: matching record or None.
        """
        with self._lock:
            return self._by_spool.get((unit_name, spool_index))

    def resolve_lane_name(self, unit_name: str, spool_index: int) -> Optional[str]:
        """
        Return the lane name for a (unit, spool) pair, or None.

        :param unit_name: owning OpenAMS unit name.
        :param spool_index: spool bay index.
        :return str: lane name or None.
        """
        info = self.get_by_spool(unit_name, spool_index)
        return info.lane_name if info else None


class AMSHardwareService:
    """
    Per-(printer, unit) façade over the [oams] hardware controller.

    Resolves and caches the ``[AFC_OAMS <name>]`` controller, polls its sensors on
    a reactor timer, caches the latest status and per-lane snapshots, and
    publishes f1s/hub/spool change events on the shared ``AMSEventBus``. One
    instance exists per (printer, unit name) pair.
    """
    _instances: Dict[Tuple[int, str], AMSHardwareService] = {}

    def __init__(self, printer: Printer, name: str, logger: Optional[AFC_logger] = None) -> None:
        """
        Initialize service state, lookup the AFC logger, and wire shared singletons.

        :param printer: Klipper printer object.
        :param name: OpenAMS unit name (matches the ``[AFC_OAMS <name>]`` section).
        :param logger: optional logger; falls back to the AFC object's logger.
        """
        self.printer: Printer = printer
        self.name = name
        self.logger: AFC_logger
        if logger is not None:
            self.logger = logger
        else:
            self.logger = printer.lookup_object("AFC").logger
        self._controller: Optional[AFC_OAMS] = None
        self._lock = threading.RLock()
        self._status: Dict[str, Any] = {}
        self._lane_snapshots: Dict[str, dict] = {}
        self._status_callbacks: list[Callable] = []
        self.registry = LaneRegistry.for_printer(printer, logger=self.logger)
        self.event_bus = AMSEventBus.get_instance(logger=self.logger)
        self._reactor: Optional[Reactor] = None
        self._polling_timer = None
        self._polling_interval = 2.0
        self._polling_interval_idle = 4.0
        self._consecutive_idle_polls = 0
        self._idle_poll_threshold = 3
        self._last_encoder_clicks = None
        self._last_f1s_hes: list[Optional[bool]] = [None, None, None, None]
        self._last_hub_hes: list[Optional[bool]] = [None, None, None, None]
        self._last_fps_value = None
        self._polling_enabled = False

    @classmethod
    def for_printer(
        cls, printer: Printer, name: str = "default", logger: Optional[AFC_logger] = None
    ) -> AMSHardwareService:
        """
        Return the cached service for a (printer, name) pair, creating it if needed.

        :param printer: Klipper printer object.
        :param name: OpenAMS unit name.
        :param logger: optional logger attached/updated on the instance.
        :return AMSHardwareService: the per-(printer, name) service singleton.
        """
        key = (id(printer), name)
        try:
            service = cls._instances[key]
        except KeyError:
            service = cls(printer, name, logger)
            cls._instances[key] = service
        else:
            if logger is not None:
                service.logger = logger
        return service

    def attach_controller(self, controller: Optional[AFC_OAMS]) -> None:
        """
        Bind an [oams] controller and seed the status cache from it.

        :param controller: the resolved ``[oams]`` controller object, or None.
        """
        with self._lock:
            self._controller = controller
        if controller is not None:
            try:
                status = controller.get_status(self._monotonic())
            except Exception:
                status = None
            if status:
                self._update_status(status)

    def resolve_controller(self) -> Optional[AFC_OAMS]:
        """
        Return the [oams] controller, looking it up and caching it if needed.

        :return object: the ``[AFC_OAMS <name>]`` controller object, or None if not found.
        """
        with self._lock:
            controller = self._controller
        if controller is not None:
            return controller
        lookup_name = f"AFC_OAMS {self.name}"
        try:
            controller = self.printer.lookup_object(lookup_name, None)
        except Exception:
            controller = None
        if controller is not None:
            self.attach_controller(controller)
        return controller

    def _monotonic(self) -> float:
        """
        Return the reactor monotonic time, caching the reactor on first use.

        :return float: current reactor monotonic timestamp.
        """
        if self._reactor is None:
            self._reactor = self.printer.get_reactor()
        return self._reactor.monotonic()

    def _polling_callback(self, eventtime: float) -> float:
        """
        Reactor timer callback: poll status and publish sensor-change events.
        Publishes ``f1s_changed``/``hub_changed`` events on transitions and
        backs the poll interval off to the idle rate when nothing changes.

        :param eventtime: reactor time the timer fired.
        :return float: next scheduled reactor time, or ``NEVER`` if disabled.
        """
        if not self._polling_enabled:
            if self._reactor is None:
                self._reactor = self.printer.get_reactor()
            return self._reactor.NEVER
        try:
            status = self.poll_status()
            if not status:
                return eventtime + self._polling_interval_idle
            f1s_values = status.get("f1s_hes_value", [])
            for bay in range(min(len(f1s_values), 4)):
                new_val = bool(f1s_values[bay])
                old_val = self._last_f1s_hes[bay]
                if old_val is None or new_val != old_val:
                    self.event_bus.publish(
                        "f1s_changed", unit_name=self.name, bay=bay,
                        value=new_val, eventtime=eventtime)
                self._last_f1s_hes[bay] = new_val
            hub_values = status.get("hub_hes_value", [])
            for bay in range(min(len(hub_values), 4)):
                new_val = bool(hub_values[bay])
                old_val = self._last_hub_hes[bay]
                if old_val is None or new_val != old_val:
                    self.event_bus.publish(
                        "hub_changed", unit_name=self.name, bay=bay,
                        value=new_val, eventtime=eventtime)
                self._last_hub_hes[bay] = new_val
            encoder_clicks = status.get("encoder_clicks")
            if encoder_clicks is not None:
                if self._last_encoder_clicks is not None and encoder_clicks != self._last_encoder_clicks:
                    self._consecutive_idle_polls = 0
                self._last_encoder_clicks = encoder_clicks
            self._consecutive_idle_polls += 1
            if self._consecutive_idle_polls > self._idle_poll_threshold:
                return eventtime + self._polling_interval_idle
            return eventtime + self._polling_interval
        except Exception:
            return eventtime + self._polling_interval_idle

    def poll_status(self) -> Optional[Dict[str, Any]]:
        """
        Read the controller status (falling back to attribute reads) and cache it.

        :return dict: the latest status mapping, or None if no controller.
        """
        controller = self.resolve_controller()
        if controller is None:
            return None
        eventtime = self._monotonic()
        try:
            status = controller.get_status(eventtime)
        except Exception:
            status = {
                "current_spool": getattr(controller, "current_spool", None),
                "f1s_hes_value": list(getattr(controller, "f1s_hes_value", []) or []),
                "hub_hes_value": list(getattr(controller, "hub_hes_value", []) or []),
                "fps_value": getattr(controller, "fps_value", 0.0),
            }
            encoder = getattr(controller, "encoder_clicks", None)
            if encoder is not None:
                status["encoder_clicks"] = encoder
        self._update_status(status)
        return status

    def _update_status(self, status: Dict[str, Any]) -> None:
        """
        Replace the cached status snapshot under lock.

        :param status: status mapping to copy into the cache.
        """
        with self._lock:
            self._status = dict(status)

    def update_lane_snapshot(self, unit_name: str, lane_name: str, lane_state: bool,
                             hub_state: Optional[bool], eventtime: float, *,
                             spool_index: Optional[int] = None,
                             tool_state: Optional[bool] = None,
                             emit_spool_event: bool = True) -> None:
        """
        Update the cached snapshot for a lane and emit spool load/unload events.
        On a lane_state transition (with a known spool index) a
        ``spool_loaded``/``spool_unloaded`` event is published unless suppressed.

        :param unit_name: owning OpenAMS unit name.
        :param lane_name: AFC lane name.
        :param lane_state: truthy if filament is present in the lane.
        :param hub_state: hub occupancy (None leaves it unset).
        :param eventtime: timestamp recorded with the snapshot.
        :param spool_index: optional spool index; negatives are ignored.
        :param tool_state: optional tool-loaded flag to record.
        :param emit_spool_event: set False to skip publishing spool events.
        """
        key = f"{unit_name}:{lane_name}"
        normalized_index = None
        if spool_index is not None:
            try:
                normalized_index = int(spool_index)
            except (TypeError, ValueError):
                pass
            else:
                if normalized_index < 0:
                    normalized_index = None
        with self._lock:
            old_snapshot = self._lane_snapshots.get(key, {})
            self._lane_snapshots[key] = {
                "unit": unit_name, "lane": lane_name,
                "lane_state": bool(lane_state),
                "hub_state": None if hub_state is None else bool(hub_state),
                "timestamp": eventtime,
            }
            if normalized_index is not None:
                self._lane_snapshots[key]["spool_index"] = normalized_index
            elif "spool_index" in old_snapshot:
                self._lane_snapshots[key]["spool_index"] = old_snapshot["spool_index"]
            if tool_state is not None:
                self._lane_snapshots[key]["tool_state"] = bool(tool_state)
        event_spool_index = normalized_index if normalized_index is not None else old_snapshot.get("spool_index")
        old_lane_state = old_snapshot.get("lane_state")
        new_lane_state = bool(lane_state)
        if (emit_spool_event
            and old_lane_state is not None
            and old_lane_state != new_lane_state
            and event_spool_index is not None):
            event_type = "spool_loaded" if new_lane_state else "spool_unloaded"
            self.event_bus.publish(event_type, unit_name=unit_name,
                                  lane_name=lane_name, spool_index=event_spool_index,
                                  eventtime=eventtime)

    def resolve_lane_for_spool(self, unit_name: str, spool_index: int) -> Optional[str]:
        """
        Resolve a lane name for a (unit, spool) pair via the registry.

        :param unit_name: owning OpenAMS unit name.
        :param spool_index: spool index (coerced to int).
        :return str: lane name or None.
        """
        if spool_index is None:
            return None
        try:
            normalized = int(spool_index)
        except (TypeError, ValueError):
            return None
        return self.registry.resolve_lane_name(unit_name, normalized)

    def resolve_lane_for_spool_with_afc(
        self, unit_name: str, spool_index: int
    ) -> Optional[str]:
        """
        Resolve a lane name for a spool, falling back to scanning AFC units.

        :param unit_name: owning OpenAMS unit name.
        :param spool_index: spool index.
        :return str: lane name or None.
        """
        lane_name = self.resolve_lane_for_spool(unit_name, spool_index)
        if lane_name is not None:
            return lane_name
        return self._resolve_lane_name_from_afc(unit_name, spool_index)

    def _resolve_lane_name_from_afc(
        self, unit_name: str, spool_index: int
    ) -> Optional[str]:
        """
        Find a lane name by scanning AFC units when the registry has no entry.
        Matches the unit by ``oams_name`` and the lane by its ``unit:slot``
        suffix (slot = spool_index + 1).

        :param unit_name: owning OpenAMS unit name.
        :param spool_index: spool index.
        :return str: lane name or None.
        """
        if spool_index is None:
            return None
        try:
            normalized_index = int(spool_index)
        except (TypeError, ValueError):
            return None
        try:
            afc = self.printer.lookup_object("AFC", None)
        except Exception:
            afc = None
        if afc is None or not hasattr(afc, 'units'):
            return None
        for unit_obj in afc.units.values():
            if not hasattr(unit_obj, 'oams_name'):
                continue
            if unit_obj.oams_name != unit_name:
                continue
            target_slot = normalized_index + 1
            for lane_name, lane_obj in getattr(unit_obj, 'lanes', {}).items():
                lane_unit = getattr(lane_obj, 'unit', None)
                if lane_unit and ':' in lane_unit:
                    try:
                        slot = int(lane_unit.split(':', 1)[1])
                    except (ValueError, IndexError):
                        continue
                    if slot == target_slot:
                        return lane_name
        return None



def _ams_box_logo(title: str, n_slots: int, name: str) -> str:
    """
    AMS-style unit logo: a titled box with one spool bay per slot, fronted by
    the R/E/A/D/Y banner (prep console output). ASCII borders for portability.

    :param title: text centered in the box header.
    :param n_slots: number of spool bays to draw.
    :param name: unit name appended below the box.
    :return str: HTML/markup logo string (success styling).
    """
    n = max(1, int(n_slots) if n_slots else 1)
    bay_w = 3
    while n * bay_w + (n - 1) < len(title):
        bay_w += 1
    inner = n * bay_w + (n - 1)
    bar = "-" * bay_w
    spool = "O".center(bay_w)
    rows = [
        "+" + "-" * inner + "+",
        "|" + title.center(inner) + "|",
        "+" + "+".join([bar] * n) + "+",
        "|" + "|".join([spool] * n) + "|",
        "+" + "+".join([bar] * n) + "+",
    ]
    body = "\n".join("%s  %s" % (L, r) for L, r in zip("READY", rows))
    return "<span class=success--text>%s</span>\n   %s\n" % (body, name)


def _ams_box_logo_error(title: str, n_slots: int, name: str) -> str:
    """
    Error variant of the AMS-style logo (red box, ERROR banner).

    :param title: text centered in the box header.
    :param n_slots: number of spool bays to size the box for.
    :param name: unit name appended below the box.
    :return str: HTML/markup logo string (error styling).
    """
    n = max(1, int(n_slots) if n_slots else 1)
    bay_w = 3
    while n * bay_w + (n - 1) < len(title):
        bay_w += 1
    inner = max(n * bay_w + (n - 1), len("X ERROR"))
    rows = [
        "+" + "-" * inner + "+",
        "|" + title.center(inner) + "|",
        "+" + "-" * inner + "+",
        "|" + "X ERROR".center(inner) + "|",
        "+" + "-" * inner + "+",
    ]
    body = "\n".join("%s  %s" % (L, r) for L, r in zip("ERROR", rows))
    return "<span class=error--text>%s</span>\n   %s\n" % (body, name)


class afcAMS(afcUnit):
    """
    OpenAMS unit type — supports engagement verification, stuck spool
    and clog detection via FPS + encoder monitoring.
    """

    def __init__(self, config: ConfigWrapper) -> None:
        """
        Initialize an OpenAMS unit from its config section.
        Reads the hardware name, stuck-spool/clog/engagement options, registers
        the unit's custom load/unload and calibration gcode commands, and wires
        up the ``temperature_oams`` sensor factory.

        :param config: ConfigWrapper for this unit's ``[AFC_OpenAMS ...]`` section.
        """
        super().__init__(config)
        self.type = config.get('type', 'OpenAMS')
        self.stepperless_drive: bool = True

        # Hardware identifier
        self.oams_name = config.get("oams", "oams1")

        # Auto-create matching spools in Spoolman on RFID scan for this unit's
        # lanes (read by AFC_RFID.get_auto_spoolman_create via lane.unit_obj).
        # Default off — lane reads match-only unless explicitly enabled.
        self.auto_spoolman_create = config.getboolean("auto_spoolman_create", False)

        # Stuck spool detection
        self.stuck_spool_auto_recovery = config.getboolean("stuck_spool_auto_recovery", False)
        self.stuck_spool_load_grace = config.getfloat("stuck_spool_load_grace", 8.0, minval=0.0)
        self.stuck_spool_pressure_threshold = config.getfloat(
            "stuck_spool_pressure_threshold", 0.08, minval=0.0)

        # Clog detection
        self.clog_sensitivity = config.get("clog_sensitivity", "medium").lower()

        # Engagement params — unit-level defaults, per-lane overrides from lane config
        self._engagement_length = config.getfloat("engagement_length", 20.0, minval=1.0)
        self._engagement_speed = config.getfloat("engagement_speed", 300.0, minval=10.0)
        self._defer_engagement = config.getboolean("defer_engagement", False)
        self._engagement_params: Dict[str, tuple[float, float]] = {}

        # Runtime state
        self.oams: AFC_OAMS = None  # type: ignore[assignment]  # resolved in handle_ready()
        self._follower: Optional[FollowerController] = None
        self._monitor: Optional[OAMSMonitor] = None
        self._spool_map: Dict[str, int] = {}

        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_mux_command(
            'AFC_OAMS_CALIBRATE_PTFE', "UNIT", self.name, self.cmd_AFC_OAMS_CALIBRATE_PTFE,
            desc="Calibrate OpenAMS PTFE length")
        self.gcode.register_mux_command(
            'AFC_OAMS_CALIBRATE_HUB_HES', "UNIT", self.name,
            self.cmd_AFC_OAMS_CALIBRATE_HUB_HES,
            desc="Calibrate OpenAMS hub HES for a spool")
        self.gcode.register_mux_command(
            'AFC_OAMS_CALIBRATE_HUB_HES_ALL', "UNIT", self.name,
            self.cmd_AFC_OAMS_CALIBRATE_HUB_HES_ALL,
            desc="Calibrate all loaded OpenAMS hub HES sensors")
        self.gcode.register_mux_command(
            'AFC_OAMS_CLEAR_ERRORS', "UNIT", self.name, self.cmd_AFC_OAMS_CLEAR_ERRORS,
            desc="Clear OpenAMS errors and resync state")
        # Internal global command for stuck-spool auto-recovery. Multiple afcAMS
        # units share the printer gcode namespace, so ignore AlreadyRegistered.
        try:
            self.gcode.register_command(
                '_AFC_OAMS_STUCK_RECOVERY',
                self._cmd_stuck_spool_recovery,
                desc="Internal: auto-recover from a stuck spool via unload+reload")
        except Exception:
            pass

        # Sensor polling state
        self._last_f1s: list[Optional[bool]] = [None] * 4
        self._last_hub: list[Optional[bool]] = [None] * 4
        self._poll_timer = None

        # Operation guard — prevents polling from corrupting state during load/unload
        self._operation_active = False
        self._prev_states_stale = False
        self._hub_load_suppressed: set[str] = set()
        self._pending_spool_loaded_timers: Dict[str, Any] = {}
        self._td1_last_capture_time = None

        # Register temperature_oams sensor factory during config parsing
        try:
            from extras.temperature_oams import TemperatureOAMS
            pheaters = self.printer.load_object(config, "heaters")
            pheaters.add_sensor_factory("temperature_oams", TemperatureOAMS)
        except Exception:
            pass

        # klippy:ready is registered by afcUnit.__init__ (upstream core) and
        # dispatches to our handle_ready override below — don't register it
        # again here or it would run twice (double poll timer / monitor init).

    def handle_connect(self) -> None:
        """
        Build logos, the lane->slot spool map, and per-lane engagement params.
        Runs at klippy:connect after lanes exist: assigns each lane its custom
        load/unload commands and seeds its virtual sensor/runout state to False
        until ``handle_ready`` syncs from hardware.
        """
        super().handle_connect()

        self.logo = _ams_box_logo("OpenAMS", len(self.lanes), self.name)
        self.logo_error = _ams_box_logo_error("OpenAMS", len(self.lanes), self.name)

        # Build spool map, set custom commands, read per-lane engagement params
        for lane_name, lane in self.lanes.items():
            slot = getattr(lane, 'index', 0) - 1
            if slot < 0:
                slot = 0
            self._spool_map[lane_name] = slot
            # Load and unload both go through the unit_load_lane / unit_unload_lane
            # hooks (below), so the shared toolhead phase lives in the unit driver
            # and AFC.py needs no fork.
            eng_len = getattr(lane, 'engagement_length', None)
            if eng_len is not None:
                eng_speed = getattr(lane, 'engagement_speed', None) or self._engagement_speed
                self._engagement_params[lane_name] = (eng_len, eng_speed)

            # OpenAMS lanes use virtual sensors driven by hardware polling;
            # initialize to False until handle_ready syncs from hardware
            lane.prep_state = False
            lane._load_state = False
            lane.loaded_to_hub = False
            lane.status = AFCLaneState.NONE

            # Same-FPS runout tracking — blocks sensor noise during reload
            setattr(lane, '_oams_runout_detected', False)

    def _init_follower_and_monitor(self) -> None:
        """
        Set up follower motor controller and stuck/clog monitor.
        """
        if self.oams is None:
            return

        fps_obj: Optional[AFCFPSBuffer] = None
        fps_name: Optional[str] = None
        # Look for FPS buffer attached to any lane in this unit
        for lane in self.lanes.values():
            buf = getattr(lane, 'buffer_obj', None)
            if buf and hasattr(buf, 'get_fps_value'):
                fps_obj = buf
                fps_name = getattr(buf, 'name', 'fps')
                break

        if FollowerController is not None and self.oams is not None:
            try:
                self._follower = FollowerController(
                    {self.oams_name: self.oams}, self.afc.reactor, self.logger)
            except Exception as e:
                self.logger.error(f"Failed to init follower: {e}")

        if OAMSMonitor is not None and fps_obj is not None and fps_name is not None:
            try:
                self._monitor = OAMSMonitor(
                    fps_name, fps_obj,
                    self.afc.reactor, self.logger,
                    stuck_pressure_low=self.stuck_spool_pressure_threshold,
                    stuck_load_grace=self.stuck_spool_load_grace,
                    clog_sensitivity=self.clog_sensitivity,
                    on_stuck_spool=self._on_stuck_spool_detected,
                    on_clog=self._on_clog_detected,
                    on_stuck_cleared=self._on_stuck_spool_cleared,
                    is_printing_fn=lambda: self.afc.function.in_print(),
                    is_lane_loaded_fn=lambda: any(
                        getattr(lane, 'tool_loaded', False)
                        and getattr(lane, 'extruder_obj', None) is not None
                        and lane.extruder_obj.on_shuttle()
                        for lane in self.lanes.values()))
            except Exception as e:
                self.logger.error(f"Failed to init monitor: {e}")

    def handle_ready(self) -> None:
        """
        Resolve OAMS hardware and start sensor polling once reactor is running.
        """
        # Let the upstream afcUnit ready handler run first (natural-orders the
        # lanes for the Mainsail/Fluidd panels).
        try:
            super().handle_ready()
        except Exception as e:
            self.logger.debug(f"afcUnit.handle_ready: {e}")
        # Vivid-style hub: each lane's raw_load_state carries the hub HES and the
        # native AFC_hub reports any(lane.raw_load_state). The hub stays
        # non-driven (_state_driven False) — no set_state_driven needed.
        self.oams = self.printer.lookup_object(f"AFC_OAMS {self.oams_name}", None)

        if self.oams is None:
            self.logger.warning(
                f"OpenAMS hardware '[AFC_OAMS {self.oams_name}]' not found for "
                f"'{self.name}'. Sensor state will not update.")
            return

        self._init_follower_and_monitor()

        # Seed initial state from hardware sensors
        self._sync_lanes_from_hardware()

        # Start periodic polling (every 2s) for sensor changes
        self._poll_timer = self.afc.reactor.register_timer(
            self._poll_oams_sensors,
            self.afc.reactor.monotonic() + 1.0)

    def _is_virtual_hub(self, lane: AFCLane) -> bool:
        """
        Return whether a lane's hub is a virtual (pin-backed) hub.

        :param lane: the AFCLane whose hub is inspected.
        :return bool: True if the lane has a hub that reports a virtual pin.
        """
        hub = lane.hub_obj
        return (hub is not None
                and hasattr(hub, 'is_virtual_pin')
                and hub.is_virtual_pin())

    def _sync_lanes_from_hardware(self) -> None:
        """
        Read current OAMS sensor values and seed lane state.
        Vivid-style virtual hub: F1S (feeder) -> prep_state (filament inserted
        in the lane); hub HES -> raw_load_state, which the native AFC_hub
        aggregates as any(lane.raw_load_state). No switch_pin_callback /
        set_state_driven needed.

        NOTE: assumes hub HES is a steady-state "filament at hub" indicator. An
        earlier revision treated it as transit-only and used F1S for the hub;
        validate against hardware.
        """
        if self.oams is None:
            return

        f1s_values = getattr(self.oams, 'f1s_hes_value', None) or []
        hub_values = getattr(self.oams, 'hub_hes_value', None) or []

        for lane_name, lane in self.lanes.items():
            slot = self._spool_map.get(lane_name, -1)
            if slot < 0:
                continue

            f1s_present = bool(f1s_values[slot]) if slot < len(f1s_values) else False
            hub_present = bool(hub_values[slot]) if slot < len(hub_values) else False

            # Vivid model: prep_state = F1S (filament inserted in lane);
            # raw_load_state = live hub HES (hub-junction occupancy, trips while
            # filament is at the junction, 0 at idle). loaded_to_hub is a LATCHED
            # logical flag — seeded here from F1S (a present spool is staged at
            # the hub), then held until load/unload or the spool is removed; it
            # is never clobbered by the transient hub HES.
            lane.prep_state = f1s_present
            lane._load_state = hub_present
            lane.loaded_to_hub = f1s_present

            if slot < len(f1s_values):
                self._last_f1s[slot] = f1s_present
            if slot < len(hub_values):
                self._last_hub[slot] = hub_present

    def _poll_oams_sensors(self, eventtime: float) -> float:
        """
        Periodic timer callback — detect sensor changes and update lane state.
        """
        if self.oams is None:
            return self.afc.reactor.NEVER

        if self._operation_active:
            return eventtime + 2.0

        resync_prev = self._prev_states_stale
        self._prev_states_stale = False

        f1s_values = getattr(self.oams, 'f1s_hes_value', None) or []
        hub_values = getattr(self.oams, 'hub_hes_value', None) or []

        for lane_name, lane in self.lanes.items():
            slot = self._spool_map.get(lane_name, -1)
            if slot < 0:
                continue

            # F1S -> prep_state (filament inserted in lane). When the spool is
            # removed (F1S lost) the lane can no longer be loaded to hub, so clear
            # the latched loaded_to_hub (mirrors Vivid clearing it when the prep
            # sensor is lost). loaded_to_hub is otherwise NOT sensor-derived.
            if slot < len(f1s_values):
                new_f1s = bool(f1s_values[slot])

                lane.prep_state = new_f1s
                if not new_f1s:
                    lane.loaded_to_hub = False

                if resync_prev:
                    self._last_f1s[slot] = new_f1s
                else:
                    old_f1s = self._last_f1s[slot] if slot < len(self._last_f1s) else None

                    if old_f1s is not None and new_f1s != old_f1s:
                        if self._should_block_sensor_for_runout(lane, new_f1s):
                            self._last_f1s[slot] = new_f1s
                            continue
                        if lane_name in self._hub_load_suppressed:
                            lane._load_suppressed = True
                            self._hub_load_suppressed.discard(lane_name)
                        lane.handle_load_runout(eventtime, new_f1s)

                    self._last_f1s[slot] = new_f1s

            # Hub HES -> raw_load_state: live hub-junction occupancy aggregated by
            # the native virtual hub (any(lane.raw_load_state)). 0 at idle is
            # correct — filament is staged clear of the junction.
            if slot < len(hub_values):
                new_hub = bool(hub_values[slot])
                lane._load_state = new_hub
                self._last_hub[slot] = new_hub

        return eventtime + 2.0

    # ── Engagement verification ─────────────────────────────────────

    def get_engagement_params(self, lane_name: str) -> tuple:
        """
        Return (engagement_length, engagement_speed) for a lane.
        Falls back to the unit-level defaults when the lane has no override.

        :param lane_name: lane name to look up.
        :return tuple: (engagement_length, engagement_speed).
        """
        if lane_name in self._engagement_params:
            return self._engagement_params[lane_name]
        return (self._engagement_length, self._engagement_speed)

    def _verify_engagement(self, cur_lane: AFCLane) -> bool:
        """
        Verify filament engagement by extruding and checking encoder/FPS movement.
        Keeps the follower feeding forward, extrudes the configured engagement
        length in two phases, and confirms the encoder advanced (with one brief
        retry).

        :param cur_lane: the lane being loaded/verified.
        :return bool: True if encoder movement confirmed engagement.
        """
        engagement_length, engagement_speed = self.get_engagement_params(cur_lane.name)

        self.logger.info(
            f"Verifying engagement for {cur_lane.name}: "
            f"{engagement_length:.1f}mm at {engagement_speed:.0f}mm/min")

        if self._monitor:
            self._monitor.notify_engagement_start()

        try:
            # Keep the follower enabled FORWARD during engagement — it must
            # always run forward to feed filament through the buffer so the
            # encoder registers movement. Without this the follower may be
            # idle and the encoder won't move, producing a false engagement
            # failure even after a physically successful load.
            if self._follower and self.oams:
                self._follower.enable_follower(
                    self._get_monitor_state(), self.oams, 1,
                    "engagement verification", force=True)

            # Record encoder before
            encoder_before = 0
            if self.oams and hasattr(self.oams, 'encoder_clicks'):
                encoder_before = self.oams.encoder_clicks or 0

            # Wait for FPS pressure to normalize
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 1.5)

            # Two-phase extrude: prime then main
            prime_length = min(5.0, engagement_length)
            remaining = max(0.0, engagement_length - prime_length)

            self._oams_extrude(prime_length, engagement_speed, "engagement_prime")
            if remaining > 0:
                self._oams_extrude(remaining, engagement_speed, "engagement_main")

            # Check encoder movement
            encoder_after = 0
            if self.oams and hasattr(self.oams, 'encoder_clicks'):
                encoder_after = self.oams.encoder_clicks or 0

            encoder_delta = abs(encoder_after - encoder_before)
            min_movement = 2

            if encoder_delta >= min_movement:
                self.logger.info(
                    f"Engagement verified: encoder moved {encoder_delta} clicks")
                return True

            # Brief retry
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.3)
            encoder_retry = 0
            if self.oams and hasattr(self.oams, 'encoder_clicks'):
                encoder_retry = abs((self.oams.encoder_clicks or 0) - encoder_before)
            if encoder_retry >= min_movement:
                self.logger.info(
                    f"Engagement verified on retry: encoder moved {encoder_retry} clicks")
                return True

            self.logger.error(
                f"Engagement verification failed: encoder moved only {encoder_delta} clicks")
            return False

        finally:
            if self._monitor:
                self._monitor.notify_engagement_end()

    def _oams_extrude(self, length: float, speed: float, label: str = "") -> None:
        """
        Extrude filament via gcode for engagement/retraction.

        :param length: extrusion distance in mm (negative to retract).
        :param speed: feedrate in mm/min.
        :param label: optional descriptive label (unused in the move itself).
        """
        self.afc.gcode.run_script_from_command(
            f"G92 E0\nG1 E{length:.3f} F{speed:.0f}\nM400")

    def _advance_tool_stn_to_nozzle(
        self, cur_lane: AFCLane, already_advanced: float = 0.0
    ) -> None:
        """
        Push filament from the toolhead sensor to the nozzle tip via tool_stn.
        AFC's native load does a tool_stn (sensor->nozzle) extruder move, but the
        unit_load_lane branch skips it, so a no-poop load would leave the tip at
        the sensor. Extrude the sensor->nozzle distance minus whatever a prior
        move (engagement verification) already advanced past the sensor.

        :param cur_lane: the lane just loaded.
        :param already_advanced: mm already extruded past the sensor (e.g. the
            engagement length), subtracted from tool_stn.
        """
        ext = cur_lane.extruder_obj
        tool_stn = getattr(ext, 'tool_stn', 0) or 0
        remaining = tool_stn - (already_advanced or 0.0)
        if remaining <= 0:
            return
        speed = getattr(ext, 'tool_load_speed', 25.0) * 60.0
        self.logger.info(
            f"OAMS load: advancing {remaining:.1f}mm to nozzle "
            f"(tool_stn {tool_stn:.0f} - {already_advanced:.0f} already fed) for {cur_lane.name}")
        self._oams_extrude(remaining, speed, "tool_stn_to_nozzle")
        self.afc.afcDeltaTime.log_with_time("OAMS load: tool_stn to nozzle")

    # ── Stuck spool / clog detection callbacks ──────────────────────

    def _on_stuck_spool_detected(
        self, fps_name: Optional[str] = None, message: Optional[str] = None
    ) -> None:
        """
        Called by OAMSMonitor when stuck spool detected during print.
        Attempts auto-recovery if enabled, otherwise pauses the print.

        :param fps_name: FPS channel name reporting the condition.
        :param message: optional pre-formatted message.
        """
        msg = message or "OpenAMS stuck spool detected"
        if fps_name and fps_name not in msg:
            msg = f"{msg} (FPS: {fps_name})"

        st = self._get_monitor_state()
        lane_name = st.current_lane if st else None

        # Error LED on the stuck bay.
        if st and self._follower and st.current_spool_idx is not None:
            try:
                self._follower.set_led_error_if_changed(
                    self.oams, self.oams_name, st.current_spool_idx, 1,
                    "stuck spool detected")
            except Exception as e:
                self.logger.debug(f"stuck LED set failed: {e}")

        # Auto-recover (unload + reload + resume) when enabled and the lane is
        # known; otherwise pause for the user.
        if self.stuck_spool_auto_recovery and lane_name:
            self._on_stuck_spool_recovery_needed(fps_name, lane_name)
        else:
            if "paused" not in msg.lower():
                msg += ". Print paused — check spool and resume."
            self.afc.error.AFC_error(msg, pause=True)

    def _on_clog_detected(
        self, fps_name: Optional[str] = None, message: Optional[str] = None
    ) -> None:
        """
        Called by OAMSMonitor when clog detected during print.
        Pauses the print with an explanatory message.

        :param fps_name: FPS channel name reporting the condition.
        :param message: optional pre-formatted message.
        """
        msg = message or "OpenAMS clog detected"
        if fps_name and fps_name not in msg:
            msg = f"{msg} (FPS: {fps_name})"
        if "paused" not in msg.lower():
            msg += ". Print paused — check filament path."
        self.afc.error.AFC_error(msg, pause=True)

    def _on_stuck_spool_cleared(self, fps_name: Optional[str] = None) -> None:
        """
        Called by OAMSMonitor when stuck spool condition clears.

        :param fps_name: FPS channel name whose stuck condition cleared.
        """
        self.logger.info(f"Stuck spool cleared{' on ' + fps_name if fps_name else ''}")

    def _on_stuck_spool_recovery_needed(self, fps_name: Optional[str], lane_name: str) -> None:
        """
        Trigger stuck spool recovery (unload + reload + resume).
        Called from reactor timer context (non-blocking): schedules the recovery
        gcode command, which runs in the gcode thread where blocking is safe.

        :param fps_name: FPS buffer name that reported the stuck spool.
        :param lane_name: lane currently loaded on that FPS.
        """
        self.logger.info(
            f"Stuck spool recovery scheduled: fps={fps_name}, lane={lane_name}")
        try:
            self.gcode.run_script_from_command(
                f"_AFC_OAMS_STUCK_RECOVERY LANE={lane_name} FPS={fps_name or ''}")
        except Exception as e:
            self.logger.error(
                f"Failed to schedule stuck spool recovery for {lane_name}: {e}")
            try:
                self.gcode.run_script_from_command("PAUSE")
            except Exception as pe:
                self.logger.error(
                    f"Failed to pause after recovery-schedule failure: {pe}")

    def _cmd_stuck_spool_recovery(self, gcmd: GCodeCommand) -> None:
        """
        Internal gcode command: full unload + reload + resume for a stuck spool.
        Runs in the gcode thread so the blocking TOOL_UNLOAD/TOOL_LOAD are safe.

        :param gcmd: gcode command supplying LANE and FPS.
        """
        lane_name = gcmd.get('LANE', None)
        fps_name = gcmd.get('FPS', None)
        lane = self.afc.lanes.get(lane_name) if lane_name else None
        if lane is None:
            self.logger.error(
                f"Stuck spool recovery: lane '{lane_name}' not found, pausing")
            self._stuck_spool_recovery_fallback(fps_name, lane_name, "lane not found")
            return

        self.logger.info(
            f"Stuck spool auto-recovery starting: unload then reload for {lane_name}")

        # Save toolhead position so AFC_RESUME can restore it afterwards.
        self.afc.save_pos()

        # Pause the print queue so no more gcode feeds from the sdcard.
        pause_resume = self.printer.lookup_object("pause_resume", None)
        if pause_resume is not None and not self.afc.function.is_paused():
            pause_resume.send_pause_command()

        # Z-hop to clear the part.
        try:
            pos = self.afc.gcode_move.last_position
            self.afc.move_z_pos(pos[2] + self.afc.z_hop, "stuck_spool_recovery_zhop")
        except Exception as e:
            self.logger.warning(f"Stuck spool recovery: Z-hop failed: {e}")

        try:
            self.logger.info(f"Stuck spool recovery: unloading {lane_name}")
            if not self.afc.TOOL_UNLOAD(lane, set_start_time=True):
                raise RuntimeError(f"TOOL_UNLOAD returned False for {lane_name}")

            # The unload retracts past the hub sensor, but filament inertia can
            # briefly re-trigger it; TOOL_LOAD reads hub.state at the start and
            # bails "Hub not clear" if set. Poll until it settles.
            HUB_CLEAR_TIMEOUT = 5.0
            HUB_POLL_INTERVAL = 0.1
            hub = getattr(lane, 'hub_obj', None)
            if hub is not None and hub.state:
                reactor = self.printer.get_reactor()
                waited = 0.0
                while hub.state and waited < HUB_CLEAR_TIMEOUT:
                    reactor.pause(reactor.monotonic() + HUB_POLL_INTERVAL)
                    waited += HUB_POLL_INTERVAL
                if hub.state:
                    self.logger.warning(
                        f"Stuck spool recovery: hub still active {waited:.1f}s after "
                        f"unload of {lane_name} - proceeding with TOOL_LOAD anyway")

            self.logger.info(f"Stuck spool recovery: reloading {lane_name}")
            if not self.afc.TOOL_LOAD(lane, set_start_time=True):
                raise RuntimeError(f"TOOL_LOAD returned False for {lane_name}")
        except Exception as e:
            self.logger.error(f"Stuck spool auto-recovery FAILED for {lane_name}: {e}")
            self._stuck_spool_recovery_fallback(fps_name, lane_name, str(e))
            return

        self.logger.info(
            f"Stuck spool auto-recovery SUCCEEDED for {lane_name}, resuming print")
        self._stuck_spool_recovery_clear_oams_state(fps_name, lane_name)
        try:
            self.afc.error.reset_failure()
            # AFC_RESUME guards on is_paused()==True before restoring the toolhead
            # and restarting the SD card; a stray resume during recovery can clear
            # the flag, so force it back so AFC_RESUME takes the full path.
            if not self.afc.function.is_paused():
                pr = self.printer.lookup_object("pause_resume", None)
                if pr is not None:
                    pr.is_paused = True
            self.gcode.run_script_from_command("AFC_RESUME")
        except Exception as e:
            self.logger.error(f"Stuck spool recovery: AFC_RESUME failed: {e}")

    def _stuck_spool_recovery_fallback(
        self, fps_name: Optional[str], lane_name: Optional[str], reason: str
    ) -> None:
        """
        Fall back to pausing when auto-recovery is not possible or fails.

        :param fps_name: FPS buffer name.
        :param lane_name: lane that was being recovered.
        :param reason: human-readable failure reason.
        """
        msg = (
            f"Stuck spool auto-recovery failed for {lane_name or fps_name}: {reason}.\n"
            f"Please manually correct the issue, then run:\n"
            f"  SET_LANE_LOADED LANE={lane_name}\n"
            f"followed by AFC_OAMS_CLEAR_ERRORS")
        self.logger.error(msg)
        try:
            self.afc.error.AFC_error(msg, pause=True)
        except Exception as e:
            self.logger.error(f"Failed to raise AFC error for stuck fallback: {e}")
            try:
                self.gcode.run_script_from_command("PAUSE")
            except Exception:
                pass

    def _stuck_spool_recovery_clear_oams_state(
        self, fps_name: Optional[str], lane_name: Optional[str]
    ) -> None:
        """
        Clear stuck-spool error state (LED + monitor) after a successful recovery.

        :param fps_name: FPS buffer name (unused; kept for symmetry).
        :param lane_name: lane that recovered (unused; kept for symmetry).
        """
        try:
            st = self._get_monitor_state()
            if st and st.current_spool_idx is not None and self._follower:
                try:
                    self._follower.clear_error_led(
                        self.oams, self.oams_name, st.current_spool_idx,
                        "stuck spool auto-recovery succeeded")
                except Exception as e:
                    self.logger.debug(f"clear_error_led failed: {e}")
            if st:
                st.stuck_active = False
                st.stuck_start_time = None
        except Exception as e:
            self.logger.warning(f"Failed to clear stuck spool state: {e}")

    # ── Unit interface overrides ────────────────────────────────────

    def prep_load(self, lane: AFCLane) -> None:
        """
        No-op: OpenAMS firmware drives filament to load sensor directly.

        :param lane: the lane being prepped (unused).
        """
        pass

    def prep_post_load(self, lane: AFCLane) -> None:
        """
        No-op: OpenAMS handles hub staging internally.

        :param lane: the lane just loaded (unused).
        """
        pass

    def eject_lane(self, lane: AFCLane) -> None:
        """
        OpenAMS does not support stepper-based eject.
        Logs guidance to remove the spool physically.

        :param lane: the lane requested for eject.
        """
        self.logger.info(
            f"Eject not supported for OpenAMS lane {lane.name}. "
            "Remove spool physically or use TOOL_UNLOAD.")

    def lane_move(self, cur_lane: AFCLane, distance: float, speed_mode: SpeedMode) -> None:
        """
        OpenAMS has no stepper — log warning.

        :param cur_lane: the lane requested to move.
        :param distance: requested move distance (ignored).
        :param speed_mode: requested speed mode (ignored).
        """
        self.logger.info(
            f"Lane move not supported for OpenAMS lane {cur_lane.name}. "
            "OpenAMS firmware controls filament movement.")

    def lane_unload(self, cur_lane: AFCLane) -> Optional[bool]:
        """
        Custom lane unload via OAMS hardware.

        :param cur_lane: the lane to unload.
        :return bool: True after the hardware unload, or None if no OAMS hardware.
        """
        if self.oams is None:
            return None
        try:
            # Wait for the AMS to ack the previous action before sending — a
            # command sent while it is still busy is silently ignored.
            self._wait_for_idle()
            self.oams.unload_spool_with_retry()
            self._wait_for_idle()
        except Exception as e:
            self.logger.error(f"OpenAMS lane_unload failed: {e}")
        return True

    def get_lane_reset_command(self, lane: AFCLane, dis: float) -> None:
        """
        OpenAMS lanes don't support distance-based reset.

        :param lane: the lane (unused).
        :param dis: requested reset distance (unused).
        :return None: always None (no reset command available).
        """
        return None

    def system_Test(
        self, cur_lane: AFCLane, delay: float, assignTcmd: bool, enable_movement: bool
    ) -> bool:
        """
        OpenAMS system test — query hardware sensors for lane state.
        Reads the lane's F1S/hub sensors, updates lane/tool state and LEDs, and
        re-establishes the follower/monitor when the lane is tool-loaded.

        :param cur_lane: the lane to test.
        :param delay: prep delay (unused by OpenAMS).
        :param assignTcmd: when True, (re)assign the lane's T-command.
        :param enable_movement: movement-enable flag (unused; no stepper).
        :return bool: True if the test succeeded (OAMS connected).
        """
        msg = ''
        succeeded = True

        if self.oams is None:
            msg = '<span class=error--text>OAMS NOT CONNECTED</span>'
            succeeded = False
        else:
            slot = self._spool_map.get(cur_lane.name, -1)

            # Read OAMS sensors. prep_state = F1S (inserted); raw_load_state =
            # live hub HES. loaded_to_hub is latched: seed it from F1S (a present
            # spool is staged at the hub), force True if the lane is tool-loaded.
            f1s_values = getattr(self.oams, 'f1s_hes_value', None) or []
            hub_values = getattr(self.oams, 'hub_hes_value', None) or []
            f1s_present = bool(f1s_values[slot]) if 0 <= slot < len(f1s_values) else False
            hub_present = bool(hub_values[slot]) if 0 <= slot < len(hub_values) else False

            cur_lane.prep_state = f1s_present
            cur_lane._load_state = hub_present
            cur_lane.loaded_to_hub = f1s_present

            if f1s_present:
                self.lane_loaded(cur_lane)
                msg += "<span class=success--text>LOCKED</span>"
                cur_lane.status = AFCLaneState.LOADED
                msg += "<span class=success--text> AND LOADED</span>"
                self.lane_illuminate_spool(cur_lane)

                if (cur_lane.tool_loaded
                    and cur_lane.extruder_obj.lane_loaded == cur_lane.name):
                    cur_lane.sync_to_extruder()
                    msg += cur_lane.extruder_obj.prep_on_shuttle_check(cur_lane)
                    if self.afc.current == cur_lane.name:
                        self.afc.spool.set_active_spool(cur_lane.spool_id)
                        self.lane_tool_loaded(cur_lane)
                        cur_lane.status = AFCLaneState.TOOLED
                        cur_lane.enable_buffer()
                    else:
                        self.lane_tool_loaded_idle(cur_lane)

                    # Enable follower forward so OAMS maintains buffer
                    # tension for filament already in the toolhead.
                    if self._follower and self.oams:
                        self._follower.enable_follower(
                            self._get_monitor_state(), self.oams, 1,
                            "prep tool-loaded", force=True)

                    if self._monitor:
                        spool_index = self._spool_map.get(cur_lane.name, 0)
                        self._monitor.notify_load_complete(
                            cur_lane.name, self.oams_name, spool_index)
                        self._monitor.start(self.oams)

                    self.printer.send_event("afc:tool_loaded", cur_lane)
            else:
                if not cur_lane.remember_spool:
                    self.afc.spool.clear_values(cur_lane)
                cur_lane.unit_obj.lane_not_ready(cur_lane)
                msg += 'EMPTY READY FOR SPOOL'

        if assignTcmd:
            self.afc.function.TcmdAssign(cur_lane)
        cur_lane.do_enable(False)
        self.logger.info('{lane_name} tool cmd: {tcmd:3} {msg}'.format(
            lane_name=cur_lane.name, tcmd=cur_lane.map_to_string(), msg=msg))
        cur_lane.set_afc_prep_done()
        return succeeded

    def calibrate_lane(self, cur_lane: AFCLane, tol: float) -> Tuple[bool, str, int]:
        """
        Run HUB HES calibration for an OpenAMS lane.

        :param cur_lane: the lane to calibrate (must be loaded).
        :param tol: calibration tolerance (unused by OpenAMS).
        :return tuple: (success bool, message str, distance int).
        """
        if self.oams is None:
            return False, "OpenAMS hardware not available", 0

        spool_index = self._get_openams_spool_index(cur_lane)
        if not cur_lane.load_state:
            return False, f"{cur_lane.name} not loaded, load before calibration", 0

        self.logger.info(f"Running HUB HES calibration for {cur_lane.name}")
        success = self._calibrate_hub_hes_spool(spool_index)
        if success:
            return True, "calibration_lane", 0
        return False, f"HUB HES calibration failed for {cur_lane.name}", 0

    def calibration_lane_message(self) -> str:
        """
        Return the summary message template for completed lane calibration.

        :return str: format string with a ``{lanes}`` placeholder.
        """
        return "\nHUB HES calibration complete for lanes: {lanes}\n"

    def calibrate_bowden(
        self, cur_lane: AFCLane, dis: float, tol: float
    ) -> Tuple[bool, str, int]:
        """
        Run PTFE length calibration for an OpenAMS lane.

        :param cur_lane: the lane to calibrate (must be loaded).
        :param dis: requested distance (unused; firmware drives calibration).
        :param tol: calibration tolerance (unused by OpenAMS).
        :return tuple: (success bool, message str, distance int).
        """
        if self.oams is None:
            return False, "OpenAMS hardware not available", 0

        spool_index = self._get_openams_spool_index(cur_lane)
        if not cur_lane.load_state:
            return False, f"{cur_lane.name} not loaded, load before calibration", 0

        self.logger.info(f"Running PTFE calibration for {cur_lane.name}")
        try:
            oams_idx = self._get_oams_index()
            self.afc.gcode.run_script_from_command(
                f"OAMS_CALIBRATE_PTFE_LENGTH OAMS={oams_idx} SPOOL={spool_index}")
            return True, f"PTFE calibration complete for {cur_lane.name}", 0
        except Exception as e:
            return False, f"PTFE calibration failed for {cur_lane.name}: {e}", 0

    def _toolhead_sensor_triggered(self, cur_lane: AFCLane) -> bool:
        """
        Check if the toolhead sensor is triggered, using the raw hardware
        button state for U1 motion sensors (which need encoder rotation for
        filament_present but have a physical switch for static detection).

        :param cur_lane: the lane whose extruder sensor is checked.
        :return bool: True if the toolhead sensor sees filament.
        """
        # The U1 toolhead motion sensor is stored by AFC_extruder as
        # 'fila_tool_start' (when u1_filament_sensor_name is configured);
        # 'filament_sensor_obj' is never assigned, so fall back to it.
        sensor_obj = (getattr(cur_lane.extruder_obj, 'filament_sensor_obj', None)
                      or getattr(cur_lane.extruder_obj, 'fila_tool_start', None))
        if sensor_obj is not None and hasattr(sensor_obj, 'runout_buttun_state'):
            return bool(sensor_obj.runout_buttun_state)
        return cur_lane.get_toolhead_pre_sensor_state()

    # ── Custom load/unload gcode handlers ───────────────────────────
    def unit_load_lane(self, cur_lane: AFCLane, cur_extruder: AFCExtruder) -> bool:
        """
        Load hook called by AFC's load_sequence for OpenAMS lanes, replacing the
        stepper-based load path with the OAMS transport.

        :param cur_lane: the lane being loaded.
        :param cur_extruder: the lane's extruder object.
        :return bool: True on success, False if the load failed (pauses if printing).
        """
        if not self._oams_load_sequence(cur_lane, cur_extruder):
            # Mirror the ACE: on total load failure (after _oams_load's retries)
            # pause the print when printing, so a failed toolchange doesn't
            # continue with no filament loaded. Returning False alone only resets
            # toolchange stats in the core and never pauses.
            self.afc.error.handle_lane_failure(
                cur_lane, f"OpenAMS load failed for {cur_lane.name}",
                pause=self.afc.function.in_print())
            return False
        return True

    def unit_unload_lane(self, cur_lane: AFCLane, cur_extruder: AFCExtruder) -> bool:
        """
        Unload hook called by AFC's unload_sequence for OpenAMS lanes, replacing
        the stepper-based unload path with the OAMS transport.

        :param cur_lane: the lane being unloaded.
        :param cur_extruder: the lane's extruder object.
        :return bool: True on success, False if the OAMS unload transport failed.
        """
        self.afc.move_e_pos(-2, cur_extruder.tool_unload_speed, "Quick Pull",
                        wait_tool=False)
        cur_lane.status = AFCLaneState.TOOL_UNLOADING
        cur_lane.disable_buffer()
        cur_lane.sync_to_extruder()
        cur_lane.select_lane()
        self.afc.do_tool_cut_tip_form(cur_lane, cur_extruder)
        success = self._oams_unload_sequence(cur_lane, cur_extruder)
        if not success:
            return success

        if self.afc.post_unload_macro is not None:
            self.gcode.run_script_from_command(self.afc.post_unload_macro)

        cur_lane.set_tool_unloaded(normal_toolchange=True)
        cur_lane.status = AFCLaneState.NONE
        self.afc.save_vars()
        return True

    def _oams_load_sequence(self, cur_lane: AFCLane, cur_extruder: AFCExtruder) -> bool:
        """
        OAMS load transport: push filament to toolhead area.
        Guards polling against state corruption for the duration of the load.

        :param cur_lane: the lane being loaded.
        :param cur_extruder: the lane's extruder object.
        :return bool: True on successful transport.
        """
        self._operation_active = True
        try:
            return self._oams_load_inner(cur_lane, cur_extruder)
        finally:
            self._operation_active = False
            self._prev_states_stale = True

    def _oams_load_inner(self, cur_lane: AFCLane, cur_extruder: AFCExtruder) -> bool:
        """
        OAMS custom load — filament transport only.
        AFC's load_sequence handles the shared toolhead engagement
        (sync_to_extruder, tool_end, tool_stn, sensor verification,
        buffer ram).

        :param cur_lane: the lane being loaded.
        :param cur_extruder: the lane's extruder object.
        :return bool: True on successful transport.
        """
        # Clear suppression — this lane is being intentionally loaded
        self._hub_load_suppressed.discard(cur_lane.name)

        # OAMS hardware load (pushes filament to toolhead area)
        if not self._oams_load(cur_lane):
            return False

        # Latch loaded_to_hub True; raw_load_state (live hub HES) is updated by
        # the sensor poll as filament passes/clears the hub junction.
        cur_lane.loaded_to_hub = True
        cur_lane.status = AFCLaneState.TOOL_LOADED
        self.afc.save_vars()

        return True

    def _oams_unload_sequence(self, cur_lane: AFCLane, cur_extruder: AFCExtruder) -> bool:
        """
        OAMS unload transport — OAMS hardware unload back to spool bay.
        Guards polling against state corruption for the duration of the unload.

        :param cur_lane: the lane being unloaded.
        :param cur_extruder: the lane's extruder object.
        :return bool: True on successful unload transport.
        """
        self._operation_active = True
        try:
            return self._oams_unload_inner(cur_lane, cur_extruder)
        finally:
            self._operation_active = False
            self._prev_states_stale = True

    def lane_unloading(self, lane: AFCLane) -> None:
        """
        Unload-start hook the upstream core actually calls.
        Upstream's unload invokes ``lane_unloading`` (for LEDs) but never
        ``prepare_unload``, so stop the OAMS follower here too — otherwise it
        keeps feeding forward against the cut/park/tip before the hardware
        unload reverses it.

        :param lane: the lane beginning to unload.
        """
        super().lane_unloading(lane)
        try:
            self.prepare_unload(lane, getattr(lane, 'hub_obj', None), lane.extruder_obj)
        except Exception as e:
            self.logger.warning(
                "OAMS: lane_unloading follower-stop error for %s: %s"
                % (getattr(lane, 'name', '?'), e))

    def prepare_unload(
        self, cur_lane: AFCLane, cur_hub: Optional[afc_hub], cur_extruder: AFCExtruder
    ) -> None:
        """
        Stop the OAMS follower before AFC's unload begins.
        We only STOP the forward-feeding follower here — the hardware unload
        (unload_spool) drives the follower in reverse itself during the rewind.
        Reversing it this early would pull filament back during the toolhead
        cut/park/tip operations, before the tip is even formed.

        :param cur_lane: the lane being unloaded.
        :param cur_hub: the lane's hub object.
        :param cur_extruder: the lane's extruder object.
        """
        if self._follower:
            self._follower.set_follower_state(
                self._get_monitor_state(), self.oams, 0, 0,
                "stop before unload", force=True)

    def _oams_unload_inner(self, cur_lane: AFCLane, cur_extruder: AFCExtruder) -> bool:
        """
        OAMS custom unload — filament transport only.
        AFC's unload_sequence handles the shared toolhead operations
        (LED, heat, quick pull, buffer disable, sync, cut/park/tip)
        and unsync_to_extruder.

        :param cur_lane: the lane being unloaded.
        :param cur_extruder: the lane's extruder object.
        :return bool: True on successful hardware unload.
        """
        afc = self.afc

        # OAMS hardware unload
        if not self._oams_unload(cur_lane):
            afc.error.handle_lane_failure(
                cur_lane, f"OAMS unload failed for {cur_lane.name}",
                pause=afc.function.in_print())
            return False

        # Finalize state. _oams_unload() already latched loaded_to_hub from the
        # live F1S (spool present in bay = still staged; spool yanked = not
        # staged) and settled the hub HES, so do NOT force it True here.
        self.lane_tool_unloaded(cur_lane)
        self._hub_load_suppressed.add(cur_lane.name)

        afc.afcDeltaTime.log_with_time("OAMS unload complete")
        return True

    def _oams_load(self, cur_lane: AFCLane, max_retries: int = 3) -> bool:
        """
        Load filament via OpenAMS hardware with engagement verification.
        Short-circuits if the firmware already has the spool loaded to the
        toolhead; otherwise drives the follower, sends the hardware load,
        verifies engagement, and retries with cleanup on failure.

        :param cur_lane: the lane to load.
        :param max_retries: number of load attempts before giving up.
        :return bool: True if the spool loaded and engaged.
        """
        if self.oams is None:
            self.logger.error("OAMS hardware not available")
            return False

        spool_index = self._spool_map.get(cur_lane.name, 0)

        # Wait for OAMS idle
        self._wait_for_idle()

        # Guard against a firmware/Klipper state desync (e.g. after a
        # Klipper or OAMS restart): if the firmware already has this spool
        # loaded, sending another load_spool() produces NO action_status —
        # the firmware has nothing to do — so load_spool() blocks the full
        # 45s and returns "MCU unresponsive", which then cascades into
        # auto-unload "OAMS is busy". Query the hardware directly (the spool
        # query IS answered even when load is not) and treat an
        # already-loaded spool as a successful load.
        #
        # determine_current_spool() queries the firmware, which reports a bay
        # from its feeder (f1s) sensor. On insertion the OpenAMS hardware
        # auto-stages filament (feeds to the hub sensor, then backs off), so a
        # freshly inserted spool ALSO makes the query report that bay even
        # though nothing is loaded through to the toolhead — and the hub sensor
        # can't tell "staged at hub" (momentarily hub=1 during staging, or
        # hub=0 after backoff) from "loaded to the toolhead". The only reliable
        # proof of an actual toolhead load is the toolhead sensor itself (the
        # U1 physical switch). Require it before short-circuiting: if the
        # toolhead sensor does NOT see filament we run a real load, so a
        # staged/inserted spool is never mistaken for already-loaded (which
        # would skip the load — transport completes instantly, the toolhead
        # never sees filament, engagement verification pauses).
        hw_spool = None
        try:
            hw_spool = self.oams.determine_current_spool()
        except Exception as e:
            self.logger.debug(f"Could not query OAMS current spool: {e}")
        toolhead_loaded = False
        if hw_spool is not None and hw_spool == spool_index:
            try:
                toolhead_loaded = bool(self._toolhead_sensor_triggered(cur_lane))
            except Exception:
                toolhead_loaded = False
            if not toolhead_loaded:
                self.logger.info(
                    f"OAMS reports spool {spool_index} present in hardware but "
                    f"the toolhead sensor does not see filament for "
                    f"{cur_lane.name}; treating as not loaded and running a "
                    f"real load")
        if hw_spool is not None and hw_spool == spool_index and toolhead_loaded:
            self.logger.info(
                f"OAMS spool {spool_index} already loaded to the toolhead "
                f"(firmware current_spool={hw_spool}, toolhead sensor "
                f"triggered); skipping redundant load for {cur_lane.name}")
            self.oams.current_spool = spool_index
            # Re-establish follower + monitor exactly as a successful load
            if self._follower:
                self._follower.enable_follower(
                    self._get_monitor_state(), self.oams, 1,
                    "already loaded", force=True)
            if self._monitor:
                self._monitor.notify_load_complete(
                    cur_lane.name, self.oams_name, spool_index)
                self._monitor.start(self.oams)
            cur_lane.loaded_to_hub = True
            return True

        # Stop monitor during load to prevent false positives
        if self._monitor:
            self._monitor.stop()

        # Enable FPS advance latch so the toolhead sensor stays triggered
        # even when pressure drops briefly after OAMS feed completes.
        buffer_obj = getattr(cur_lane, 'buffer_obj', None)
        if buffer_obj is not None and hasattr(buffer_obj, 'enable_advance_latch'):
            buffer_obj.enable_advance_latch()

        # Stop follower before switching direction — the MCU considers
        # an active follower as "busy" and will reject load commands.
        if self._follower:
            self._follower.set_follower_state(
                self._get_monitor_state(), self.oams, 0, 0,
                "stop before load", force=True)
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.5)

        # Enable follower forward before load
        if self._follower:
            self._follower.enable_follower(
                self._get_monitor_state(), self.oams, 1, "before load",
                force=True)
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.5)

        for attempt in range(max_retries):
            try:
                # Confirm the AMS has acked the follower change and is idle
                # before sending the load — otherwise it may ignore the command.
                self._wait_for_idle()
                success, msg = self.oams.load_spool_with_retry(spool_index)
                if not success:
                    self.logger.error(f"OAMS load attempt {attempt+1} failed: {msg}")
                    continue

                # Verify engagement (skip if deferred to AFC's Phase 2)
                engaged = True if self._defer_engagement else self._verify_engagement(cur_lane)
                if engaged:
                    # Advance filament to the nozzle via tool_stn so a no-poop
                    # load ends at the nozzle (poop, if any, purges from there).
                    # When engagement ran it already extruded engagement_length
                    # past the sensor, so push only the remainder; when deferred
                    # it extruded nothing, so push the full tool_stn.
                    already = (0.0 if self._defer_engagement
                               else self.get_engagement_params(cur_lane.name)[0])
                    self._advance_tool_stn_to_nozzle(cur_lane, already_advanced=already)

                    # Enable follower and start monitor
                    if self._follower:
                        self._follower.enable_follower(
                            self._get_monitor_state(), self.oams, 1,
                            "load complete", force=True)

                    if self._monitor:
                        self._monitor.notify_load_complete(
                            cur_lane.name, self.oams_name, spool_index)
                        self._monitor.start(self.oams)

                    # Set virtual hub sensor
                    cur_lane.loaded_to_hub = True

                    return True

                # Engagement failed — clean up and retry
                self.logger.info(
                    f"Engagement failed attempt {attempt+1}, cleaning up")

                # Disable the follower (enable=0) — do NOT run it in reverse.
                # The extruder still grips the loaded filament; driving the
                # follower backward here makes the two motors fight and grinds
                # the gears (constant tension). The original oams_manager used
                # set_oams_follower(0, 0) here. The extruder retract below and
                # the hardware unload_spool_with_retry() handle backing the
                # filament out, not the follower.
                if self._follower:
                    self._follower.set_follower_state(
                        self._get_monitor_state(), self.oams, 0, 0,
                        "engagement cleanup — stop follower", force=True)

                # Retract from extruder using tool_stn distance so
                # filament clears the extruder gears before AMS unload.
                retract_dist = getattr(cur_lane.extruder_obj, 'tool_stn_unload', 0)
                if retract_dist and retract_dist > 0:
                    unload_length = retract_dist + 10.0
                    retract_speed = getattr(cur_lane.extruder_obj, 'tool_unload_speed', 25.0) * 60.0
                    self._oams_extrude(
                        -unload_length, retract_speed, "engagement_cleanup_retract")

                # Stop follower before hardware cleanup — MCU rejects
                # commands while follower is active.
                if self._follower:
                    self._follower.set_follower_state(
                        self._get_monitor_state(), self.oams, 0, 0,
                        "stop before cleanup", force=True)
                    self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.5)

                # Hardware cleanup — gate each command with idle wait
                self._wait_for_idle()
                self.oams.abort_current_action(wait=True)
                self._wait_for_idle()
                self.oams.unload_spool_with_retry()
                self._wait_for_idle()
                # Reset the OAMS firmware/host state after a failed attempt so
                # a stuck "OAMS is busy" doesn't poison the next load. Clears
                # action_status + LED errors and re-reads current_spool.
                try:
                    self.oams.clear_errors()
                except Exception as e:
                    self.logger.debug(f"clear_errors after failed load: {e}")
                self._wait_for_idle()

                if attempt < max_retries - 1:
                    self.afc.reactor.pause(self.afc.reactor.monotonic() + 2.0)
                    # Re-enable follower forward for next attempt
                    if self._follower:
                        self._follower.enable_follower(
                            self._get_monitor_state(), self.oams, 1,
                            "before retry", force=True)
                        self.afc.reactor.pause(
                            self.afc.reactor.monotonic() + 0.5)

            except Exception as e:
                self.logger.error(f"OAMS load error attempt {attempt+1}: {e}")

        self.logger.error(f"OAMS load failed after {max_retries} attempts")
        return False

    def _oams_unload(self, cur_lane: AFCLane) -> bool:
        """
        Unload filament via OpenAMS hardware.
        Reverses the follower, retracts the extruder, runs the hardware unload
        (skipped for runout-empty spools), then waits for the hub HES to settle
        clear and latches the post-unload lane state.

        :param cur_lane: the lane to unload.
        :return bool: True if the unload succeeded.
        """
        if self.oams is None:
            self.logger.error("OAMS hardware not available")
            return False

        spool_index = self._spool_map.get(cur_lane.name, 0)

        # prepare_unload() only STOPS the follower; the hardware unload
        # (unload_spool, below) drives it in reverse itself during the rewind.

        # Wait for idle
        self._wait_for_idle()

        # Stop monitor during unload
        if self._monitor:
            self._monitor.stop()

        # Drive the follower in REVERSE before retracting the extruder so it
        # pulls filament back toward the spool IN SYNC with the retract. The
        # follower was stopped by prepare_unload / the form-tip purge; left
        # stopped (or feeding forward) it fights the extruder pull-back and
        # shoves filament back into the gears. The brief reactor pause lets the
        # follower actually spin up reverse before the extruder pulls (an M400
        # only waits on toolhead moves, not the follower MCU). The hardware
        # unload (unload_spool, below) keeps driving it reverse.
        if self._follower and self.oams:
            try:
                self._follower.enable_follower(
                    self._get_monitor_state(), self.oams, 0,
                    "reverse before unload retract", force=True)
                self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.5)
            except Exception as e:
                self.logger.warning(
                    f"OAMS: follower reverse before unload retract failed: {e}")

        # Blocking pre-retract: clear filament from extruder gears before
        # OAMS hardware starts rewinding. Without this the hardware fights
        # against the extruder grip and can't pull the filament back.
        retract_dist = getattr(cur_lane.extruder_obj, 'tool_stn_unload', 0)
        if retract_dist and retract_dist > 0:
            unload_length = retract_dist + 10.0
            retract_speed = getattr(cur_lane.extruder_obj, 'tool_unload_speed', 25.0) * 60.0
            self.logger.debug(
                f"Retracting {unload_length:.1f}mm from extruder before OAMS unload")
            try:
                self._oams_extrude(-unload_length, retract_speed, "pre_oams_unload_retract")
            except Exception as e:
                self.logger.warning(f"Extruder retract before OAMS unload failed: {e}")

        # Check if this is a runout-empty lane — spool ran out so
        # OAMS hardware can't rewind. Skip hardware unload but keep
        # the extruder retract and follower/monitor cleanup.
        runout_empty = getattr(cur_lane, '_oams_runout_empty', False)
        if runout_empty:
            setattr(cur_lane, '_oams_runout_empty', False)

        try:
            # Queue an extruder retract WITHOUT waiting — it runs concurrently
            # with the OAMS hardware unload so the extruder helps pull filament
            # back as the spool motor rewinds. Uses the configured
            # tool_stn_unload (falls back to 20mm if it's unset).
            concurrent_retract = retract_dist if retract_dist and retract_dist > 0 else 20.0
            concurrent_speed = getattr(
                cur_lane.extruder_obj, 'tool_unload_speed', 25.0) * 60.0
            try:
                self.afc.gcode.run_script_from_command("M83")
                self.afc.gcode.run_script_from_command(
                    "G1 E-%.2f F%d" % (concurrent_retract, int(concurrent_speed)))
            except Exception as e:
                self.logger.warning(f"Concurrent retract failed: {e}")

            if runout_empty:
                self.logger.info(
                    f"Skipping OAMS hardware unload for {cur_lane.name} "
                    f"— spool empty from runout")
                success = True
            else:
                # State-desync guard (mirror of the load guard): if the
                # firmware reports nothing loaded, an unload_spool() may
                # never be acked and would block 40s ("MCU unresponsive").
                # Query the hardware directly and skip the redundant unload.
                hw_spool = None
                try:
                    hw_spool = self.oams.determine_current_spool()
                except Exception as e:
                    self.logger.debug(
                        f"Could not query OAMS current spool: {e}")
                if hw_spool is None:
                    self.logger.info(
                        f"OAMS reports no spool loaded; skipping redundant "
                        f"hardware unload for {cur_lane.name}")
                    self.oams.current_spool = None
                    success = True
                else:
                    success, msg = self.oams.unload_spool_with_retry()
            self._wait_for_idle()

            # Stop follower after unload so MCU is idle for the next
            # operation (e.g. load on a different lane).
            if self._follower:
                self._follower.set_follower_state(
                    self._get_monitor_state(), self.oams, 0, 0,
                    "stop after unload", force=True)
                self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.5)

            if self._monitor:
                self._monitor.notify_unload_complete()

            # After unload the spool stays staged in the bay: latch loaded_to_hub
            # to whether the spool is still present (F1S). raw_load_state (live
            # hub HES) is refreshed by the sensor poll.
            if hasattr(self.oams, 'f1s_hes_value'):
                f1s_values = self.oams.f1s_hes_value
                if f1s_values and spool_index < len(f1s_values):
                    f1s_present = bool(f1s_values[spool_index])
                    cur_lane.prep_state = f1s_present
                    cur_lane.loaded_to_hub = f1s_present

            # The OAMS retract-past/push-back/back-off settle makes the hub HES
            # bounce; wait for it to settle clear so the shared virtual hub reads
            # clear before AFC's next-lane "hub clear?" load check — otherwise the
            # swap errors "Hub not clear" on a transient reading. Reflect the
            # settled value immediately instead of waiting for the 2s sensor poll.
            if self._wait_for_hub_settle(spool_index):
                cur_lane._load_state = False
                if spool_index < len(self._last_hub):
                    self._last_hub[spool_index] = False

            return success

        except Exception as e:
            self.logger.error(f"OAMS unload error: {e}")
            return False

    # ── Calibration commands ────────────────────────────────────────

    def cmd_AFC_OAMS_CALIBRATE_PTFE(self, gcmd: GCodeCommand) -> None:
        """
        Calibrate PTFE length for the unit specified. Optional SPOOL selects
        the bay to calibrate (default 0).

        Usage
        -------
        `AFC_OAMS_CALIBRATE_PTFE UNIT=<unit> SPOOL=<index>`

        Example
        -------
        ```
        AFC_OAMS_CALIBRATE_PTFE UNIT=OpenAMS_1 SPOOL=0
        ```
        """
        if self.oams is None:
            gcmd.respond_info("OAMS hardware not available")
            return
        spool = gcmd.get_int('SPOOL', 0)
        try:
            oams_idx = self._get_oams_index()
            self.afc.gcode.run_script_from_command(
                f"OAMS_CALIBRATE_PTFE_LENGTH OAMS={oams_idx} SPOOL={spool}")
            gcmd.respond_info("PTFE calibration complete — check OAMS config for new value")
        except Exception as e:
            gcmd.respond_info(f"PTFE calibration failed: {e}")

    def cmd_AFC_OAMS_CALIBRATE_HUB_HES(self, gcmd: GCodeCommand) -> None:
        """
        Calibrate hub HES sensor for a specific spool bay. Requires SPOOL.

        Usage
        -------
        `AFC_OAMS_CALIBRATE_HUB_HES UNIT=<unit> SPOOL=<index>`

        Example
        -------
        ```
        AFC_OAMS_CALIBRATE_HUB_HES UNIT=OpenAMS_1 SPOOL=0
        ```
        """
        if self.oams is None:
            gcmd.respond_info("OAMS hardware not available")
            return
        spool = gcmd.get_int('SPOOL', None)
        if spool is None:
            gcmd.respond_info("Usage: AFC_OAMS_CALIBRATE_HUB_HES SPOOL=<index>")
            return
        success = self._calibrate_hub_hes_spool(spool)
        gcmd.respond_info(
            f"Hub HES calibration {'successful' if success else 'failed'} for spool {spool}")

    def cmd_AFC_OAMS_CALIBRATE_HUB_HES_ALL(self, gcmd: GCodeCommand) -> None:
        """
        Calibrate hub HES for all loaded spool bays on the unit specified.

        Usage
        -------
        `AFC_OAMS_CALIBRATE_HUB_HES_ALL UNIT=<unit>`

        Example
        -------
        ```
        AFC_OAMS_CALIBRATE_HUB_HES_ALL UNIT=OpenAMS_1
        ```
        """
        if self.oams is None:
            gcmd.respond_info("OAMS hardware not available")
            return
        count = 0
        for lane_name, lane in self.lanes.items():
            if lane.load_state:
                spool_idx = self._spool_map.get(lane_name, 0)
                if self._calibrate_hub_hes_spool(spool_idx):
                    count += 1
        gcmd.respond_info(f"Calibrated {count} hub HES sensor(s)")

    def cmd_AFC_OAMS_CLEAR_ERRORS(self, gcmd: GCodeCommand) -> None:
        """
        Clear OpenAMS errors and resync state with hardware. Aborts any
        in-flight action, clears firmware errors, resets the specified
        unit's lane tool-loaded state and LEDs, and restarts the monitor.

        Usage
        -------
        `AFC_OAMS_CLEAR_ERRORS UNIT=<unit>`

        Example
        -------
        ```
        AFC_OAMS_CLEAR_ERRORS UNIT=OpenAMS_1
        ```
        """
        if self.oams is None:
            gcmd.respond_info("OAMS hardware not available")
            return

        if self._monitor:
            self._monitor.stop()

        try:
            self.oams.abort_current_action()
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.1)
            self.oams.clear_errors()
            self.oams.current_spool = None
        except Exception as e:
            self.logger.error(f"Error clearing OAMS errors: {e}")

        # Clear lane_loaded state for this unit's lanes only
        for lane_name, lane in self.lanes.items():
            try:
                if getattr(lane, "tool_loaded", False):
                    lane.unsync_to_extruder()
                    lane.set_tool_unloaded()
            except Exception as e:
                self.logger.warning(f"Failed to clear lane_loaded for {lane_name}: {e}")

        # Restore LEDs based on current state
        for lane_name, lane in self.lanes.items():
            try:
                if getattr(lane, 'tool_loaded', False):
                    self.lane_tool_loaded(lane)
                elif getattr(lane, 'load_state', False):
                    self.lane_loaded(lane)
                else:
                    self.lane_unloaded(lane)
            except Exception:
                pass

        if self._monitor:
            self._monitor.state.reset()
            self._monitor.start(self.oams)

        gcmd.respond_info("OpenAMS errors cleared and state resynced")

    # ── Same-FPS runout handling ───────────────────────────────────

    def _should_block_sensor_for_runout(self, lane: AFCLane, new_val: bool) -> bool:
        """
        Block sensor updates during active same-FPS runout reload.
        Returns True if the update should be suppressed. Automatically
        clears the flag once the runout handling window has passed.

        :param lane: the lane whose sensor update is being evaluated.
        :param new_val: the new sensor value about to be applied.
        :return bool: True if the update should be suppressed.
        """
        if not getattr(lane, '_oams_runout_detected', False):
            return False

        try:
            is_printing = self.afc.function.is_printing()
            is_tool_loaded = getattr(lane, 'tool_loaded', False)
            lane_status = getattr(lane, 'status', None)
            active_runout = (is_printing and is_tool_loaded
                             and lane_status in (AFCLaneState.INFINITE_RUNOUT,
                                                 AFCLaneState.TOOL_UNLOADING))
        except Exception:
            active_runout = False

        if not active_runout:
            setattr(lane, '_oams_runout_detected', False)
            return False

        if new_val:
            self.logger.debug(
                f"Blocked sensor True for {lane.name} — same-FPS runout active")
            return True

        setattr(lane, '_oams_runout_detected', False)
        return False

    def _is_same_extruder(self, source_lane: AFCLane, target_lane: AFCLane) -> bool:
        """
        Check if two lanes share the same physical extruder.

        :param source_lane: the first lane.
        :param target_lane: the second lane.
        :return bool: True if both resolve to the same extruder name.
        """
        src_ext = getattr(source_lane, 'extruder_obj', None)
        tgt_ext = getattr(target_lane, 'extruder_obj', None)
        if src_ext is None or tgt_ext is None:
            return False
        src_name = getattr(src_ext, 'name', None)
        tgt_name = getattr(tgt_ext, 'name', None)
        if not src_name or not tgt_name:
            return False
        return src_name.strip().lower() == tgt_name.strip().lower()

    def handle_same_fps_reload(self, source_lane: AFCLane, target_lane: AFCLane) -> bool:
        """
        Same-FPS infinite runout reload — no pause, no unload.
        The old filament coasts through the shared PTFE. OAMS pushes
        the new spool's filament forward to meet it at the extruder
        gears seamlessly.

        :param source_lane: the runout (source) lane.
        :param target_lane: the runout_lane to reload from.
        :return bool: True if the reload completed, False on failure (print paused).
        """
        afc = self.afc
        source_name = source_lane.name
        target_name = target_lane.name

        self.logger.info(
            f"Same-FPS infinite runout: {source_name} -> {target_name}")

        source_lane.status = AFCLaneState.NONE
        self.lane_unloaded(source_lane)

        try:
            success = self._oams_load(target_lane)
            if not success:
                self.logger.error(f"Same-FPS reload failed for {target_name}")
                afc.error.AFC_error(
                    f"Same-FPS reload failed for {target_name}", pause=True)
                return False
        except Exception as e:
            self.logger.error(f"Same-FPS reload exception: {e}")
            afc.error.AFC_error(
                f"Same-FPS reload failed for {target_name}: {e}", pause=True)
            return False

        self.gcode.run_script_from_command(f'AFC_SWAP_MAPPING FROM={source_name} TO={target_name}')

        target_lane.set_tool_loaded()
        self.lane_tool_loaded(target_lane)
        afc.save_vars()

        self.logger.info(
            f"Same-FPS reload complete: {target_name} now active")
        return True

    def check_runout(self, lane: Optional[AFCLane] = None) -> bool:
        """
        OAMS runout: only trigger when printing AND this lane is loaded to its extruder.

        :param lane: the lane to test for an actionable runout.
        :return bool: True if a runout should be acted on for this lane.
        """
        if lane is None:
            return False
        try:
            is_printing = self.afc.function.is_printing()
        except Exception:
            return False
        if not is_printing:
            return False
        if not getattr(lane, 'tool_loaded', False):
            return False
        ext = getattr(lane, 'extruder_obj', None)
        if ext is not None and getattr(ext, 'lane_loaded', None) != lane.name:
            return False
        return True

    def handle_runout(self, lane: AFCLane) -> bool:
        """
        OAMS-specific runout — OAMS cannot retract once F1S goes empty.
        Called by handle_load_runout before the generic _perform_infinite_runout.
        Returns True if handled, False to fall through to generic behavior.

        :param lane: the lane that ran out.
        :return bool: True if fully handled here, False to defer to generic logic.
        """
        runout_lane_name = getattr(lane, 'runout_lane', None)

        if not runout_lane_name:
            lane.status = AFCLaneState.NONE
            self.lane_unloaded(lane)
            self.afc.error.AFC_error(
                f"Runout detected on OAMS {lane.name}. "
                f"No runout lane configured.\n"
                f"OAMS cannot retract filament past the point of no return. "
                f"Please manually clear filament and load a new spool.",
                pause=True)
            return True

        target_lane = self._resolve_lane_reference(runout_lane_name)
        if target_lane is None:
            lane.status = AFCLaneState.NONE
            self.lane_unloaded(lane)
            self.afc.error.AFC_error(
                f"Runout on OAMS {lane.name}: "
                f"runout lane '{runout_lane_name}' not found",
                pause=True)
            return True

        if self._is_same_extruder(lane, target_lane):
            setattr(lane, '_oams_runout_detected', True)
            self.logger.info(
                f"OAMS same-FPS runout: {lane.name} -> {target_lane.name}, "
                f"seamless reload")
            self.handle_same_fps_reload(lane, target_lane)
            return True

        # Cross-extruder: mark lane so _oams_unload skips hardware
        # unload (empty spool can't be rewound), then let AFC's
        # generic infinite runout handle the tool swap.
        setattr(lane, '_oams_runout_empty', True)
        self.logger.info(
            f"OAMS cross-extruder runout: {lane.name} -> {target_lane.name}, "
            f"deferring to AFC infinite spool (hardware unload will be skipped)")
        return False

    def on_filament_insert(self, lane: AFCLane) -> None:
        """
        OAMS insert: update lane state and publish event.

        :param lane: the lane a spool was inserted into.
        """
        lane.loaded_to_hub = True
        self.lane_loaded(lane)
        self.lane_illuminate_spool(lane)
        spool_index = self._spool_map.get(lane.name)
        if spool_index is not None:
            hw = AMSHardwareService.for_printer(self.printer, self.oams_name, logger=self.logger)
            hw.update_lane_snapshot(
                self.oams_name, lane.name, True, lane.loaded_to_hub,
                self.afc.reactor.monotonic(), spool_index=spool_index)
        super().on_filament_insert(lane)

    def on_filament_remove(self, lane: AFCLane) -> None:
        """
        OAMS removal: update lane state, cancel pending TD-1 timers, publish event.

        :param lane: the lane a spool was removed from.
        """
        if lane.name in self._pending_spool_loaded_timers:
            try:
                timer = self._pending_spool_loaded_timers[lane.name]
                self.afc.reactor.unregister_timer(timer)
            except Exception:
                pass
            del self._pending_spool_loaded_timers[lane.name]
        lane.loaded_to_hub = False
        self.lane_not_ready(lane)
        spool_index = self._spool_map.get(lane.name)
        if spool_index is not None:
            hw = AMSHardwareService.for_printer(self.printer, self.oams_name, logger=self.logger)
            hw.update_lane_snapshot(
                self.oams_name, lane.name, False, False,
                self.afc.reactor.monotonic(), spool_index=spool_index)
        # Clear stale spool/filament info so a newly inserted spool starts
        # fresh. Skip if the spool is currently loaded to a toolhead (a print
        # runout) — the runout handler still needs the lane's data.
        if not getattr(lane, 'tool_loaded', False):
            self._clear_lane_info(lane)
            self._clear_oams_state_for_bay(spool_index, lane)

    def _clear_oams_state_for_bay(self, spool_index: Optional[int], lane: AFCLane) -> None:
        """
        Reset OAMS host/monitor/follower state when a bay's f1s goes empty.
        A physically removed spool is never unloaded through the AMS, so the
        OAMS-side state (host current_spool, the follower feeding that bay, and
        the load monitor) can be left stale pointing at a now-empty bay. Clear
        it so a fresh insert starts clean and nothing keeps feeding/monitoring
        an empty lane. NOTE: determine_current_spool() re-queries the firmware
        (authoritative), so the _oams_load "already loaded" guard relies on the
        toolhead sensor, not this value — this is hygiene, not the load guard.

        :param spool_index: bay index that went empty (None is a no-op).
        :param lane: the lane associated with the bay.
        """
        if spool_index is None or self.oams is None:
            return
        was_current = getattr(self.oams, 'current_spool', None) == spool_index
        if was_current:
            self.oams.current_spool = None
        # Stop the follower for this OAMS so it isn't feeding a removed spool.
        if was_current and self._follower:
            try:
                self._follower.set_follower_state(
                    self._get_monitor_state(), self.oams, 0, 0,
                    "spool removed — f1s empty", force=True)
            except Exception:
                pass
        # Reset the load monitor if it was tracking this bay.
        if self._monitor:
            try:
                monitor_state = self._get_monitor_state()
                if (monitor_state is not None and getattr(
                        monitor_state, 'current_spool_idx', None) == spool_index):
                    self._monitor.notify_unload_complete()
                    self._monitor.stop()
            except Exception:
                pass

    def _clear_lane_info(self, lane: AFCLane) -> None:
        """
        Clear a lane's spool/filament data so a new insert starts fresh
        (mirrors the U1 RFID _clear_lane).

        :param lane: the lane whose spool/filament info is cleared.
        """
        lane.material = ""
        lane.color = ""
        if getattr(lane, 'spool_id', None) not in (None, "", 0):
            try:
                self.afc.spool.set_spoolID(lane, "")
            except Exception as e:
                self.logger.warning(
                    f"OAMS: failed to clear spool_id on {lane.name}: {e}")
        try:
            lane.send_lane_data()
        except Exception:
            pass

    # ── TD-1 support ────────────────────────────────────────────────

    def _cancel_and_mark_loaded(
        self, spool_index: int, lane_name: Optional[str] = None
    ) -> None:
        """
        Cancel an in-progress load and mark the spool as loaded.
        The firmware cancel command stops the follower motor and considers the
        spool loaded at its current position.

        :param spool_index: bay index to record as the current spool.
        :param lane_name: optional lane name for context (unused).
        """
        self.oams.load_spool_cancel()
        deadline = self.afc.reactor.monotonic() + 5.0
        while self.oams.action_status is not None:
            if self.afc.reactor.monotonic() > deadline:
                self.logger.warning("Cancel response timeout - forcing action_status clear")
                self.oams.action_status = None
                break
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.2)
        self.oams.current_spool = spool_index
        monitor_state = self._get_monitor_state()
        if monitor_state is not None:
            try:
                monitor_state.state = FPSLoadState.LOADED
            except Exception:
                pass

    def _clear_lane_state_after_td1(self, cur_lane: AFCLane) -> None:
        """
        Clear AFC-level lane loaded state that background detection may have set
        during a temporary TD-1 load.

        :param cur_lane: the lane whose transient TD-1 loaded state is cleared.
        """
        try:
            if getattr(cur_lane, "tool_loaded", False):
                cur_lane.set_tool_unloaded()
            elif getattr(cur_lane, "extruder_obj", None) is not None:
                if getattr(cur_lane.extruder_obj, "lane_loaded", None) == cur_lane.name:
                    cur_lane.extruder_obj.lane_loaded = None
        except Exception:
            pass
        self.afc.save_vars()

    def _unload_after_td1(self, cur_lane: AFCLane, spool_index: int) -> None:
        """
        Unload filament after TD-1 operation using firmware unload_spool().
        Retries up to three times, clears firmware errors, and resets lane state.

        :param cur_lane: the lane being unloaded.
        :param spool_index: bay index being unloaded.
        """
        success = False
        for attempt in range(3):
            try:
                # Wait for the AMS to ack the prior action before each attempt.
                self._wait_for_idle()
                success, msg = self.oams.unload_spool()
            except Exception as e:
                success = False
                self.logger.debug(f"TD-1 unload attempt {attempt+1} failed: {e}")
            if success:
                self.logger.info(f"TD-1 unload completed for {cur_lane.name}")
                break
        try:
            self.oams.clear_errors()
        except Exception:
            pass
        self._clear_lane_state_after_td1(cur_lane)

    def _cancel_and_cleanup_td1(self, cur_lane: AFCLane, spool_index: int) -> None:
        """
        Cancel load and clean up after a failed TD-1 calibration.

        :param cur_lane: the lane involved in the failed calibration.
        :param spool_index: bay index to cancel/unload.
        """
        try:
            self._cancel_and_mark_loaded(spool_index, cur_lane.name)
        except Exception:
            pass
        try:
            self.oams.set_oams_follower(0, 0)
        except Exception:
            pass
        try:
            self._wait_for_idle()
            self.oams.unload_spool()
        except Exception:
            pass
        try:
            self.oams.clear_errors()
        except Exception:
            pass
        self._clear_lane_state_after_td1(cur_lane)

    def _get_td1_snapshot(self, cur_lane: AFCLane) -> Optional[Tuple[str, Any, Any]]:
        """
        Get a hashable snapshot of TD-1 data for change detection.

        :param cur_lane: the lane whose TD-1 device data is read.
        :return tuple: (scan_time, td, color), or None if unavailable.
        """
        if self.afc.moonraker is None:
            return None
        try:
            td1_data = self.afc.moonraker.get_td1_data()
        except Exception:
            return None
        if not td1_data or cur_lane.td1_device_id not in td1_data:
            return None
        data = td1_data[cur_lane.td1_device_id]
        scan_time = data.get("scan_time")
        td = data.get("td")
        color = data.get("color")
        if scan_time is None:
            return None
        return (scan_time, td, color)

    def _interpolate_encoder_at_scan(
        self, scan_time_str: str, encoder_history: list, fallback: float
    ) -> float:
        """
        Find encoder position at the TD-1 scan_time by interpolating history.
        Picks the recorded encoder value whose wall-clock time is closest to the
        scan timestamp.

        :param scan_time_str: TD-1 scan timestamp string (ISO-ish).
        :param encoder_history: list of (wall_time, encoder) samples.
        :param fallback: value returned when the time can't be parsed/matched.
        :return float: the best-matching encoder value, or ``fallback``.
        """
        try:
            st = scan_time_str
            if st.endswith("+00:00Z"):
                st = st[:-1]
            elif st.endswith("Z"):
                st = st[:-1] + "+00:00"
            elif not ('+' in st[-6:] or '-' in st[-6:]):
                st = st + "+00:00"
            scan_dt = datetime.fromisoformat(st).astimezone()
        except Exception:
            return fallback

        best_encoder = fallback
        best_delta = float('inf')
        for wall_time, encoder in encoder_history:
            try:
                delta = abs((wall_time.astimezone() - scan_dt).total_seconds())
            except Exception:
                continue
            if delta < best_delta:
                best_delta = delta
                best_encoder = encoder
        return best_encoder

    def calibrate_td1(self, cur_lane: AFCLane, dis: float, tol: float) -> Tuple[bool, str, float]:
        """
        Calibrate td1_bowden_length using continuous load from hub.
        Loads spool, waits for hub trigger, captures encoder reference,
        then lets the load continue while polling TD-1 every 2 seconds.
        When TD-1 reads data, captures encoder delta from hub = bowden length.

        :param cur_lane: the lane to calibrate (needs a td1_device_id).
        :param dis: requested distance (unused; load is continuous).
        :param tol: calibration tolerance (unused).
        :return tuple: (success bool, message str, encoder_delta float).
        """
        if cur_lane.td1_device_id is None:
            msg = (f"Cannot calibrate TD-1 for {cur_lane.name}, td1_device_id "
                   "is a required field in AFC_hub or per AFC_lane")
            return False, msg, 0

        if self.oams is None:
            return False, "OpenAMS hardware not available", 0

        spool_index = self._get_openams_spool_index(cur_lane)

        valid, msg = self.afc.function.check_for_td1_id(cur_lane.td1_device_id)
        if not valid:
            return False, msg, 0

        self.logger.raw(f"TD-1 calibration: continuous load for {cur_lane.name}")

        FPS_STOP_THRESHOLD = 0.45

        self.oams.action_status = OAMSStatus.LOADING
        try:
            self.oams.oams_load_spool_cmd.send([spool_index])
        except Exception as e:
            self.oams.action_status = None
            return False, f"Failed to start load: {e}", 0

        hub_timeout = self.afc.reactor.monotonic() + 15.0
        encoder_at_hub = None
        while self.afc.reactor.monotonic() < hub_timeout:
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.1)
            try:
                if bool(self.oams.hub_hes_value[spool_index]):
                    encoder_at_hub = int(self.oams.encoder_clicks)
                    self.logger.info(
                        f"TD-1 cal: hub triggered, encoder={encoder_at_hub}")
                    break
            except Exception:
                pass

        if encoder_at_hub is None:
            self._cancel_and_cleanup_td1(cur_lane, spool_index)
            return False, f"Hub sensor did not trigger for {cur_lane.name}", 0

        baseline_td1 = self._get_td1_snapshot(cur_lane)
        td1_detected = False
        encoder_at_td1 = None
        td1_timeout = self.afc.reactor.monotonic() + 120.0
        encoder_history = []

        while self.afc.reactor.monotonic() < td1_timeout:
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.2)

            try:
                encoder_now = int(self.oams.encoder_clicks)
            except Exception:
                encoder_now = encoder_at_hub
            encoder_history.append((datetime.now(), encoder_now))
            if len(encoder_history) > 600:
                encoder_history.pop(0)

            try:
                fps_pressure = float(self.oams.fps_value)
            except Exception:
                fps_pressure = 0.0
            if fps_pressure >= FPS_STOP_THRESHOLD:
                self.logger.info(
                    f"TD-1 cal: FPS pressure {fps_pressure:.2f}, "
                    f"filament at extruder — stopping")
                break

            total_clicks = abs(encoder_now - encoder_at_hub)
            current_td1 = self._get_td1_snapshot(cur_lane)
            if current_td1 is not None and current_td1 != baseline_td1:
                td1_detected = True
                scan_time_str = current_td1[0]
                encoder_at_td1 = self._interpolate_encoder_at_scan(
                    scan_time_str, encoder_history, encoder_now)
                actual_clicks = abs(encoder_at_td1 - encoder_at_hub)
                self.logger.info(
                    f"TD-1 cal: DETECTED! scan_time={scan_time_str}, "
                    f"interpolated encoder={actual_clicks} clicks from hub "
                    f"(current encoder={total_clicks})")
                break

        try:
            self._cancel_and_mark_loaded(spool_index, cur_lane.name)
        except Exception:
            pass

        self._wait_for_idle()
        self._unload_after_td1(cur_lane, spool_index)

        if not td1_detected or encoder_at_td1 is None:
            return False, f"TD-1 did not detect filament for {cur_lane.name}", 0

        encoder_delta = abs(encoder_at_td1 - encoder_at_hub)
        self.logger.info(
            f"TD-1 calibration for {cur_lane.name}: hub={encoder_at_hub}, "
            f"td1={encoder_at_td1}, distance={encoder_delta} clicks")

        cal_msg = (f"\n td1_bowden_length: New: {encoder_delta} "
                   f"Old: {cur_lane.td1_bowden_length}")
        cur_lane.td1_bowden_length = encoder_delta
        self.afc.function.ConfigRewrite(
            cur_lane.fullname, "td1_bowden_length", encoder_delta, cal_msg)
        cur_lane.do_enable(False)
        cur_lane.unit_obj.return_to_home()
        self.afc.save_vars()

        return True, "td1_bowden_length calibration successful", encoder_delta

    def _capture_td1_with_oams(
        self, cur_lane: AFCLane, *, require_loaded: bool, require_enabled: bool
    ) -> Tuple[bool, str]:
        """
        Capture TD-1 data using OAMS hardware load/unload cycle.
        Validates preconditions, loads the spool to the hub, advances by the
        configured bowden length, reads TD-1, then unloads.

        :param cur_lane: the lane to capture from.
        :param require_loaded: require the lane to be loaded/prepped first.
        :param require_enabled: require ``td1_when_loaded`` to be enabled.
        :return tuple: (success bool, status message str).
        """
        last_capture_time = self._td1_last_capture_time
        if last_capture_time is not None:
            settle_delay = 4.2 - (self.afc.reactor.monotonic() - last_capture_time)
            if settle_delay > 0:
                self.afc.reactor.pause(self.afc.reactor.monotonic() + settle_delay)
        if require_enabled and not cur_lane.td1_when_loaded:
            return False, "TD-1 capture disabled"
        if cur_lane.td1_device_id is None:
            return False, "TD-1 device ID not configured"
        if getattr(cur_lane, "tool_loaded", False):
            return False, "Toolhead is loaded"
        if cur_lane.td1_bowden_length is None:
            return False, "td1_bowden_length not set"
        if require_loaded and not (cur_lane.load_state or cur_lane.prep_state):
            return False, "Lane is not loaded"
        if self.oams is None:
            return False, "OpenAMS hardware not available"

        spool_index = self._get_openams_spool_index(cur_lane)

        # Check for conflicts with other loaded hubs
        hub_values = getattr(self.oams, "hub_hes_value", None)
        other_hub_loaded = False
        try:
            if hub_values and spool_index < len(hub_values):
                other_hub_loaded = any(
                    bool(value) for idx, value in enumerate(hub_values) if idx != spool_index)
        except Exception:
            pass

        if other_hub_loaded:
            settle_deadline = self.afc.reactor.monotonic() + 5.0
            while self.afc.reactor.monotonic() < settle_deadline:
                try:
                    hub_values = getattr(self.oams, "hub_hes_value", None)
                    if hub_values and all(not bool(value) for value in hub_values):
                        other_hub_loaded = False
                        break
                except Exception:
                    break
                self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.1)

        if other_hub_loaded:
            return False, "Another OpenAMS hub already loaded"

        # Load the spool to move filament to hub
        self.oams.action_status = OAMSStatus.LOADING
        try:
            self.oams.oams_load_spool_cmd.send([spool_index])
        except Exception as e:
            self.oams.action_status = None
            return False, f"Failed to start spool load: {e}"

        hub_timeout = self.afc.reactor.monotonic() + 10.0
        hub_detected = False
        while self.afc.reactor.monotonic() < hub_timeout:
            try:
                hub_detected = bool(self.oams.hub_hes_value[spool_index])
            except Exception:
                hub_detected = False
            if hub_detected:
                break
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.1)

        if not hub_detected:
            try:
                self._cancel_and_mark_loaded(spool_index, cur_lane.name)
            except Exception:
                pass
            try:
                self.oams.set_oams_follower(0, 0)
            except Exception:
                pass
            try:
                self.oams.unload_spool()
            except Exception:
                pass
            try:
                self.oams.clear_errors()
            except Exception:
                pass
            self._clear_lane_state_after_td1(cur_lane)
            return False, "Hub sensor did not trigger"

        try:
            encoder_before = int(self.oams.encoder_clicks)
        except Exception:
            encoder_before = None

        if encoder_before is None:
            try:
                self._cancel_and_mark_loaded(spool_index, cur_lane.name)
            except Exception:
                pass
            try:
                self.oams.unload_spool()
            except Exception:
                pass
            try:
                self.oams.clear_errors()
            except Exception:
                pass
            self._clear_lane_state_after_td1(cur_lane)
            return False, "Unable to read encoder before capture"

        target_clicks = max(0, int(cur_lane.td1_bowden_length))
        td1_timeout = self.afc.reactor.monotonic() + 30.0
        baseline_td1 = self._get_td1_snapshot(cur_lane)

        while self.afc.reactor.monotonic() < td1_timeout:
            try:
                encoder_now = int(self.oams.encoder_clicks)
            except Exception:
                encoder_now = encoder_before
            clicks_moved = abs(encoder_now - encoder_before)
            if clicks_moved >= target_clicks:
                break
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.1)

        td1_detected = False
        td1_wait_deadline = self.afc.reactor.monotonic() + 5.0
        while self.afc.reactor.monotonic() < td1_wait_deadline:
            current_td1 = self._get_td1_snapshot(cur_lane)
            if current_td1 is not None and current_td1 != baseline_td1:
                try:
                    moonraker = self.afc.moonraker
                    if moonraker is None:
                        raise RuntimeError("moonraker not connected")
                    td1_data = moonraker.get_td1_data()
                    data = td1_data[cur_lane.td1_device_id]
                    cur_lane.td1_data = data
                    self.logger.info(
                        f"{cur_lane.name} TD-1 data captured: "
                        f"td={data.get('td')} color={data.get('color')}")
                    self.afc.save_vars()
                    td1_detected = True
                except Exception as e:
                    self.logger.error(f"TD-1 capture failed for {cur_lane.name}: {e}")
                break
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.1)

        try:
            self._cancel_and_mark_loaded(spool_index, cur_lane.name)
        except Exception:
            pass

        self._unload_after_td1(cur_lane, spool_index)
        self._td1_last_capture_time = self.afc.reactor.monotonic()

        if not td1_detected:
            return False, "TD-1 data not captured (unload completed)"
        return True, "TD-1 data captured"

    def prep_capture_td1(self, cur_lane: AFCLane) -> Optional[Tuple[bool, str]]:
        """
        OpenAMS prep TD-1 capture — only when capture_td1_when_loaded is True.

        :param cur_lane: the lane to capture during prep.
        :return tuple: (success, message) tuple, or None if capture is skipped.
        """
        if not cur_lane.td1_when_loaded:
            return None
        if self.afc.function.get_current_lane_obj() is not None:
            return None
        return self._capture_td1_with_oams(
            cur_lane, require_loaded=True, require_enabled=False)

    def capture_td1_data(self, cur_lane: AFCLane) -> Tuple[bool, str]:
        """
        Capture TD-1 data for a lane via the OAMS load/unload cycle.

        :param cur_lane: the lane to capture from.
        :return tuple: (success bool, status message str).
        """
        return self._capture_td1_with_oams(
            cur_lane, require_loaded=True, require_enabled=False)

    # ── Internal helpers ────────────────────────────────────────────

    def _get_oams_index(self) -> int:
        """
        Return numeric OAMS index for gcode commands.

        :return int: the numeric suffix of ``oams_name`` (1 on parse failure).
        """
        try:
            return int(self.oams_name.replace("oams", ""))
        except (ValueError, AttributeError):
            return 1

    def _get_openams_spool_index(self, lane: AFCLane) -> int:
        """
        Return the spool/bay index mapped to a lane.

        :param lane: the lane to look up.
        :return int: the spool index, or 0 if unmapped.
        """
        return self._spool_map.get(getattr(lane, 'name', ''), 0)

    def _resolve_lane_reference(self, lane_name: Optional[str]) -> Optional[AFCLane]:
        """
        Resolve lane name to lane object, with case-insensitive fallback.

        :param lane_name: lane name to resolve.
        :return object: the matching AFCLane object, or None.
        """
        if not lane_name:
            return None
        if lane_name in self.afc.lanes:
            return self.afc.lanes[lane_name]
        lower = lane_name.lower()
        for name, lane in self.afc.lanes.items():
            if name.lower() == lower:
                return lane
        return None

    def _wait_for_idle(self, timeout: float = 30.0) -> bool:
        """
        Wait for OAMS hardware to become idle.

        :param timeout: maximum seconds to wait.
        :return bool: True if idle, False on timeout or no hardware.
        """
        if self.oams is None:
            return False
        deadline = self.afc.reactor.monotonic() + timeout
        while self.afc.reactor.monotonic() < deadline:
            status = getattr(self.oams, 'action_status', None)
            if status is None:
                return True
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.5)
        self.logger.error("OAMS idle timeout")
        return False

    def _wait_for_hub_settle(self, spool_index: int, timeout: float = 4.0,
                             stable_time: float = 0.3) -> bool:
        """
        Wait for a spool's hub HES to settle CLEAR after an unload.
        The OAMS unload retracts filament past the hub HES, pushes it back to
        the sensor, then backs off to idle — so the hub HES bounces before it
        settles clear. Because the shared virtual hub reports
        any(lane.raw_load_state), if AFC's next-lane "hub clear?" check runs
        mid-bounce it falsely sees "Hub not clear". Poll the LIVE hub HES until
        it has read clear continuously for ``stable_time`` (up to ``timeout``).

        :param spool_index: the spool/bay whose hub HES is watched.
        :param timeout: maximum seconds to wait for a stable-clear reading.
        :param stable_time: seconds the HES must read clear continuously.
        :return bool: True if it settled clear, False on timeout (a real obstruction).
        """
        if self.oams is None:
            return True
        reactor = self.afc.reactor
        deadline = reactor.monotonic() + timeout
        clear_since = None
        while True:
            now = reactor.monotonic()
            try:
                hub_values = getattr(self.oams, 'hub_hes_value', None) or []
                hub_present = (bool(hub_values[spool_index])
                               if spool_index < len(hub_values) else False)
            except Exception:
                hub_present = False
            if not hub_present:
                if clear_since is None:
                    clear_since = now
                elif now - clear_since >= stable_time:
                    return True
            else:
                clear_since = None
            if now >= deadline:
                self.logger.warning(
                    f"OAMS hub HES did not settle clear within {timeout}s "
                    f"(spool {spool_index}); proceeding")
                return False
            reactor.pause(now + 0.05)

    def _get_monitor_state(self) -> Optional[FPSState]:
        """
        Get FPS state for follower/monitor interactions.

        :return object: the monitor's ``FPSState``, or None if no monitor exists.
        """
        if self._monitor:
            return self._monitor.state
        return None

    def _calibrate_hub_hes_spool(self, spool_index: int) -> bool:
        """
        Run hub HES calibration for a single spool bay.

        :param spool_index: the spool/bay index to calibrate.
        :return bool: True if the calibration command was dispatched without error.
        """
        try:
            oams_idx = self._get_oams_index()
            self.afc.gcode.run_script_from_command(
                f"OAMS_CALIBRATE_HUB_HES OAMS={oams_idx} SPOOL={spool_index}")
            return True
        except Exception as e:
            self.logger.error(f"Hub HES calibration failed for spool {spool_index}: {e}")
            return False




# ══════════════════════════════════════════════════════════════════════
# Helpers for the OpenAMS unit, kept here so it's self-contained: the follower
# motor controller and the real-time stuck/clog monitor. The [oams] hardware
# controller lives in AFC_OAMS.py because Klipper maps the [AFC_OAMS ...] config
# section to that module name.
# ══════════════════════════════════════════════════════════════════════


# ── Follower motor controller ─────────────────────────────────────────

class FollowerState:
    """Track follower motor state for a single OAMS unit."""
    def __init__(self) -> None:
        """
        Initialize follower tracking with no last-sent command.
        """
        self.coasting   = False
        # (enable, direction) to avoid redundant MCU commands
        self.last_state: Optional[Tuple[int, int]] = None


class FollowerController:
    """
    Controls OAMS follower motors, LED errors, and rate-limited MCU commands.

    Created and owned by AFC_OpenAMS. Operates on OAMS hardware objects
    directly — no AFC policy decisions, just hardware control.
    """

    def __init__(
        self, oams_dict: Dict[str, AFC_OAMS], reactor: Reactor, logger: AFC_logger
    ) -> None:
        """
        Initialize follower/LED/MCU-command tracking state for all OAMS units.

        :param oams_dict: Dict of OAMS hardware objects keyed by name
        :param reactor: Klipper reactor for timers
        :param logger: AFC logger instance
        """
        self.oams: Dict[str, AFC_OAMS] = oams_dict
        self.reactor: Reactor = reactor
        self.logger: AFC_logger = logger
        self.follower_state: Dict[str, FollowerState] = {}
        self._mcu_command_queue: Dict[str, list] = {}
        self._mcu_command_in_flight: Dict[str, bool] = {}
        self._mcu_command_poll_timers: Dict[str, Any] = {}
        self._led_error_state: Dict[str, int] = {}

    def get_follower_state(self, oams_name: str) -> FollowerState:
        """
        Get or create follower state tracking for an OAMS unit.

        :param oams_name: OAMS hardware name.
        :return FollowerState: the (possibly newly created) state record.
        """
        if oams_name not in self.follower_state:
            self.follower_state[oams_name] = FollowerState()
        return self.follower_state[oams_name]

    def is_mcu_ready(self, oams: Optional[AFC_OAMS]) -> bool:
        """
        Check if OAMS MCU is ready for commands.

        :param oams: OAMS hardware object.
        :return bool: True if its MCU is connected and not shut down.
        """
        if oams is None:
            return False
        try:
            mcu = getattr(oams, 'mcu', None)
            if mcu is None:
                return False
            if hasattr(mcu, 'is_shutdown'):
                return not mcu.is_shutdown()
            if hasattr(mcu, 'get_last_clock'):
                last_clock = mcu.get_last_clock()
                return last_clock is not None and last_clock > 0
            return True
        except Exception:
            return False

    # ---- Follower motor control ----

    def enable_follower(
        self, fps_state: Optional[Any], oams: Optional[AFC_OAMS], direction: int,
        context: str, force: bool = False
    ) -> None:
        """
        Enable follower motor in the given direction.

        :param fps_state: FPS state (unused; kept for interface symmetry).
        :param oams: OAMS hardware object to drive.
        :param direction: 0 or 1 (defaults to 1 if out of range).
        :param context: short description logged with the command.
        :param force: send even if the state appears unchanged.
        """
        if oams is None:
            return
        direction = direction if direction in (0, 1) else 1
        oams_name = getattr(oams, 'name', None)
        if oams_name:
            self._set_follower_if_changed(oams_name, oams, 1, direction, context, force=force)

    def set_follower_state(
        self, fps_state: Optional[Any], oams: Optional[AFC_OAMS], enable: int, direction: int,
        context: str, force: bool = False
    ) -> None:
        """
        Set follower state directly.

        :param fps_state: FPS state (unused; kept for interface symmetry).
        :param oams: OAMS hardware object to drive.
        :param enable: enable flag for the follower motor.
        :param direction: 0 or 1 (defaults to 1 if out of range).
        :param context: short description logged with the command.
        :param force: send even if the state appears unchanged.
        """
        if oams is None:
            return
        oams_name = getattr(oams, 'name', None)
        if not oams_name:
            return
        direction = direction if direction in (0, 1) else 1
        self._set_follower_if_changed(oams_name, oams, enable, direction, context, force=force)

    def _set_follower_if_changed(
        self, oams_name: str, oams: AFC_OAMS, enable: int, direction: int,
        context: str, force: bool = False
    ) -> None:
        """
        Send follower MCU command only if state changed (or force=True).

        :param oams_name: OAMS hardware name (state key).
        :param oams: OAMS hardware object to drive.
        :param enable: enable flag for the follower motor.
        :param direction: follower direction.
        :param context: short description logged with the command.
        :param force: send even if the (enable, direction) state is unchanged.
        """
        state = self.get_follower_state(oams_name)
        new_state = (enable, direction)

        if not force and state.last_state == new_state:
            return

        self.logger.debug(
            f"Follower command for {oams_name}: enable={enable} direction={direction} "
            f"context={context or 'no context'} forced={force}")

        try:
            oams.set_oams_follower(enable, direction)
            state.last_state = new_state
            self.logger.debug(f"Follower {'enabled' if enable else 'disabled'} for {oams_name} ({context})")
        except Exception as e:
            self.logger.error(f"Failed to set follower on {oams_name}: {e}")

    # ---- LED error control ----

    def set_led_error_if_changed(
        self, oams: AFC_OAMS, oams_name: str, spool_idx: int, error_state: int, context: str = ""
    ) -> None:
        """
        Set LED error state on hardware, deduplicating repeated calls.

        :param oams: OAMS hardware object.
        :param oams_name: OAMS hardware name (dedup key prefix).
        :param spool_idx: spool/bay index whose LED is affected.
        :param error_state: target error state to set.
        :param context: short description logged with the command.
        """
        key = f"{oams_name}_{spool_idx}"
        current = self._led_error_state.get(key)
        if current == error_state:
            return
        self._led_error_state[key] = error_state
        try:
            self.rate_limited_mcu_command(oams_name, oams.set_led_error, spool_idx, error_state)
            self.logger.debug(
                f"LED error {'set' if error_state else 'cleared'} for {oams_name} "
                f"spool {spool_idx} ({context})")
        except Exception as e:
            self.logger.error(f"Failed to set LED error on {oams_name}: {e}")

    def clear_error_led(
        self, oams: AFC_OAMS, oams_name: str, spool_idx: int, context: str = ""
    ) -> None:
        """
        Clear LED error state.

        :param oams: OAMS hardware object.
        :param oams_name: OAMS hardware name.
        :param spool_idx: spool/bay index whose LED is cleared.
        :param context: short description logged with the command.
        """
        self.set_led_error_if_changed(oams, oams_name, spool_idx, 0, context)

    # ---- Rate-limited MCU command queue ----

    def rate_limited_mcu_command(
        self, oams_name: str, command_fn: Callable, *args: Any, **kwargs: Any
    ) -> None:
        """
        Queue an MCU command with completion-aware rate limiting.

        :param oams_name: OAMS hardware name (queue key).
        :param command_fn: callable to invoke on the MCU.
        :param args: positional arguments forwarded to ``command_fn``.
        :param kwargs: keyword arguments forwarded to ``command_fn``.
        """
        oams = self.oams.get(oams_name)
        if oams is None or not self.is_mcu_ready(oams):
            return

        if oams_name not in self._mcu_command_queue:
            self._mcu_command_queue[oams_name] = []
            self._mcu_command_in_flight[oams_name] = False

        self._mcu_command_queue[oams_name].append((command_fn, args, kwargs))
        self._process_mcu_command_queue(oams_name)

    def _process_mcu_command_queue(self, oams_name: str) -> None:
        """
        Process queued MCU commands, one at a time.
        Defers while a command is in flight or the OAMS reports a busy
        ``action_status``, retrying via a short reactor timer.

        :param oams_name: OAMS hardware name whose queue is processed.
        """
        oams = self.oams.get(oams_name)
        if oams is None:
            return
        if self._mcu_command_in_flight.get(oams_name, False):
            return

        queue = self._mcu_command_queue.get(oams_name, [])
        if not queue:
            return

        if getattr(oams, "action_status", None) is not None:
            def _retry(eventtime: float) -> float:
                """
                Timer callback: re-attempt the queue once the OAMS is no longer busy.

                :param eventtime: reactor time the timer fired.
                :return: ``reactor.NEVER`` (one-shot timer).
                """
                self._mcu_command_in_flight[oams_name] = False
                self._process_mcu_command_queue(oams_name)
                return self.reactor.NEVER

            if oams_name in self._mcu_command_poll_timers:
                try:
                    self.reactor.unregister_timer(self._mcu_command_poll_timers[oams_name])
                except Exception:
                    pass
            self._mcu_command_poll_timers[oams_name] = self.reactor.register_timer(
                _retry, self.reactor.NOW + 0.1)
            return

        command_fn, args, kwargs = queue.pop(0)
        self._mcu_command_in_flight[oams_name] = True
        try:
            command_fn(*args, **kwargs)
        except Exception as e:
            self.logger.error(f"MCU command failed for {oams_name}: {e}")

        def _done(eventtime: float) -> float:
            """
            Timer callback: mark the command complete and process the next one.

            :param eventtime: reactor time the timer fired.
            :return: ``reactor.NEVER`` (one-shot timer).
            """
            self._mcu_command_in_flight[oams_name] = False
            self._process_mcu_command_queue(oams_name)
            return self.reactor.NEVER
        self.reactor.register_timer(_done, self.reactor.NOW + 0.05)

    def cleanup(self) -> None:
        """
        Clean up timers on disconnect.
        """
        for oams_name, timer in list(self._mcu_command_poll_timers.items()):
            try:
                self.reactor.unregister_timer(timer)
            except Exception:
                pass
        self._mcu_command_poll_timers.clear()
        self._mcu_command_queue.clear()
        self._mcu_command_in_flight.clear()


# ── Real-time stuck/clog monitor ──────────────────────────────────────

# ---- Constants ----

MONITOR_INTERVAL = 2.0          # Seconds between monitoring checks
MONITOR_INTERVAL_IDLE = 4.0     # Slower interval when not printing

# Stuck spool detection
STUCK_PRESSURE_LOW = 0.08       # FPS below this + encoder stopped = stuck
STUCK_PRESSURE_CLEAR = 0.12     # FPS must rise above this to clear stuck
STUCK_DWELL = 2.0               # Seconds condition must persist before firing
STUCK_LOAD_GRACE = 8.0          # Grace period after load completes
STUCK_MIN_ENCODER = 3           # Minimum encoder clicks per check to be "moving"

# Clog detection
CLOG_PRESSURE_TARGET = 0.50     # Expected FPS pressure during normal printing
CLOG_PRESSURE_BAND = 0.06       # Deadband around target (no clog if pressure varies)
CLOG_EXTRUSION_WINDOW = 24.0    # mm of extrusion before checking for clog
CLOG_DWELL = 10.0               # Seconds clog condition must persist
CLOG_POST_LOAD_GRACE = 12.0     # Grace after load before clog detection starts
CLOG_ENCODER_SLACK = 8          # Encoder clicks of slack allowed

# Sensitivity presets
CLOG_SENSITIVITY = {
    'off': None,
    'low': 1.5,
    'medium': 1.0,
    'high': 0.7,
}


class FPSLoadState:
    """Load state for an FPS buffer channel."""
    UNLOADED = 0
    LOADED = 1
    LOADING = 2
    UNLOADING = 3


class FPSState:
    """Tracking state for one FPS buffer channel."""
    def __init__(self) -> None:
        """
        Initialize all FPS tracking fields to the unloaded/idle baseline.
        """
        # Core state
        self.state = FPSLoadState.UNLOADED
        self.current_lane: Optional[str] = None
        self.current_oams: Optional[str] = None
        self.current_spool_idx: Optional[int] = None
        self.since: Optional[float] = None

        # Set True once the lane is actually confirmed loaded to the toolhead.
        # Used to distinguish "load sequence still finishing" (never confirmed
        # yet) from a real desync (was confirmed, then silently unloaded).
        self.toolhead_confirmed = False

        # Encoder tracking
        self.last_encoder: Optional[int] = None

        # Stuck spool detection
        self.stuck_active = False
        self.stuck_start_time: Optional[float] = None

        # Clog detection
        self.clog_active = False
        self.clog_start_time: Optional[float] = None
        self.clog_start_extruder: Optional[float] = None
        self.clog_start_extruder_obj: Optional[PrinterExtruder] = None
        self.clog_start_encoder: Optional[int] = None

        # Engagement tracking (suppress detection during engagement)
        self.engagement_in_progress = False
        self.engagement_checked_at: Optional[float] = None

        # Lane change tracking
        self.last_lane_change_time: Optional[float] = None

    def reset(self) -> None:
        """
        Reset to unloaded state.
        """
        self.state = FPSLoadState.UNLOADED
        self.current_lane = None
        self.current_oams = None
        self.current_spool_idx = None
        self.since = None
        self.toolhead_confirmed = False
        self.stuck_active = False
        self.stuck_start_time = None
        self.clog_active = False
        self.clog_start_time = None
        self.clog_start_extruder = None
        self.clog_start_extruder_obj = None
        self.clog_start_encoder = None
        self.engagement_in_progress = False

    def clear_encoder_samples(self) -> None:
        """
        Forget the last encoder reading so the next delta starts fresh.
        """
        self.last_encoder = None


class OAMSMonitor:
    """
    Real-time filament monitoring for a single OAMS/FPS channel.

    Runs on a reactor timer during printing. Checks encoder movement
    and FPS pressure to detect stuck spools and clogs.

    Reports problems via callbacks — does NOT pause or modify AFC state.
    """

    def __init__(self, fps_name: str, fps_obj: AFCFPSBuffer, reactor: Reactor, logger: AFC_logger,
                 on_stuck_spool: Optional[Callable] = None, on_clog: Optional[Callable] = None,
                 on_stuck_cleared: Optional[Callable] = None,
                 clog_sensitivity: str = 'medium', is_printing_fn: Optional[Callable] = None,
                 is_lane_loaded_fn: Optional[Callable] = None,
                 stuck_pressure_low: Optional[float] = None,
                 stuck_load_grace: Optional[float] = None) -> None:
        """
        Initialize the monitor's tracking state and callbacks for one FPS buffer.

        :param fps_name: FPS buffer name (e.g. 'FPS_buffer1')
        :param fps_obj: AFC_FPS buffer object (for ADC readings)
        :param reactor: Klipper reactor for timers
        :param logger: AFC logger
        :param on_stuck_spool: Callback(fps_name, message) when stuck detected
        :param on_clog: Callback(fps_name, message) when clog detected
        :param on_stuck_cleared: Callback(fps_name) when stuck condition clears
        :param clog_sensitivity: 'off', 'low', 'medium', 'high'
        :param is_printing_fn: Callable returning True when printer is actively printing
        :param is_lane_loaded_fn: Callable returning True when a lane is loaded to toolhead
        :param stuck_pressure_low: Pressure threshold below which a spool is considered stuck
        :param stuck_load_grace: Seconds after load to ignore stuck-spool detection
        """
        self.fps_name = fps_name
        self.fps: AFCFPSBuffer = fps_obj
        self.reactor: Reactor = reactor
        self.logger: AFC_logger = logger

        self._on_stuck_spool = on_stuck_spool
        self._on_clog = on_clog
        self._on_stuck_cleared = on_stuck_cleared
        self._is_printing = is_printing_fn
        self._is_lane_loaded = is_lane_loaded_fn

        self.clog_multiplier = CLOG_SENSITIVITY.get(clog_sensitivity, 1.0)
        self.enable_clog = self.clog_multiplier is not None

        self.state = FPSState()
        self._timer = None
        self._running = False
        self._oams: Optional[AFC_OAMS] = None  # Set when lane loads

        # Configurable thresholds (can be overridden)
        self.stuck_pressure_low = stuck_pressure_low if stuck_pressure_low is not None else STUCK_PRESSURE_LOW
        self.stuck_pressure_clear = STUCK_PRESSURE_CLEAR
        self.stuck_dwell = STUCK_DWELL
        self.stuck_load_grace = stuck_load_grace if stuck_load_grace is not None else STUCK_LOAD_GRACE
        self.stuck_min_encoder = STUCK_MIN_ENCODER
        self.clog_dwell = CLOG_DWELL * (self.clog_multiplier or 1.0)
        self.clog_extrusion_window = CLOG_EXTRUSION_WINDOW
        self.clog_post_load_grace = CLOG_POST_LOAD_GRACE

    # ---- Lifecycle ----

    def start(self, oams_obj: AFC_OAMS) -> None:
        """
        Start monitoring for the given OAMS hardware object.

        :param oams_obj: the OAMS hardware object to monitor.
        """
        self._oams = oams_obj
        self._running = True
        # Reset detection state to prevent stale triggers
        self.state.stuck_start_time = None
        self.state.clog_start_time = None
        self.state.clog_start_extruder = None
        self.state.clog_start_encoder = None
        self.state.clear_encoder_samples()
        if self._timer is None:
            self._timer = self.reactor.register_timer(
                self._monitor_tick, self.reactor.NOW + MONITOR_INTERVAL)
        self.logger.debug(f"Monitor started for {self.fps_name}")

    def stop(self) -> None:
        """
        Stop monitoring.
        """
        self._running = False
        if self._timer is not None:
            self.reactor.unregister_timer(self._timer)
            self._timer = None
        self.logger.debug(f"Monitor stopped for {self.fps_name}")

    def notify_load_complete(self, lane_name: str, oams_name: str, spool_idx: int) -> None:
        """
        Called by AFC_OpenAMS after successful load.
        Marks the channel LOADED (toolhead not yet confirmed) and resets the
        detection state.

        :param lane_name: lane now loaded.
        :param oams_name: OAMS hardware name.
        :param spool_idx: spool/bay index loaded.
        """
        self.state.state = FPSLoadState.LOADED
        self.state.current_lane = lane_name
        self.state.current_oams = oams_name
        self.state.current_spool_idx = spool_idx
        self.state.since = self.reactor.monotonic()
        self.state.last_lane_change_time = self.state.since
        # Not yet confirmed at the toolhead — AFC's load_sequence (tool_stn +
        # purge) is still finishing after the OAMS transport completes, so
        # is_lane_loaded() will read False for a while. Don't auto-stop the
        # monitor for that; only stop if the lane was confirmed loaded and
        # then disappeared (a real desync).
        self.state.toolhead_confirmed = False
        self.state.stuck_active = False
        self.state.clog_active = False
        self.state.clear_encoder_samples()

    def notify_unload_complete(self) -> None:
        """
        Called by AFC_OpenAMS after successful unload.
        """
        self.state.reset()

    def notify_engagement_start(self) -> None:
        """
        Suppress detection during engagement verification.
        """
        self.state.engagement_in_progress = True

    def notify_engagement_end(self) -> None:
        """
        Resume detection after engagement verification.
        """
        self.state.engagement_in_progress = False
        self.state.engagement_checked_at = self.reactor.monotonic()

    # ---- Main monitoring loop ----

    def _monitor_tick(self, eventtime: float) -> float:
        """
        Reactor timer callback — runs every MONITOR_INTERVAL.
        Skips while not loaded/printing or during engagement/grace, then runs
        the stuck-spool and (optionally) clog checks.

        :param eventtime: reactor time the timer fired.
        :return float: next scheduled reactor time, or ``NEVER`` to stop.
        """
        if not self._running or self._oams is None:
            return self.reactor.NEVER

        st = self.state
        if st.state != FPSLoadState.LOADED:
            return eventtime + MONITOR_INTERVAL_IDLE

        # Verify a lane is ACTUALLY loaded to toolhead. The monitor is started
        # as soon as the OAMS transport completes, but AFC's load_sequence
        # (tool_stn + purge) hasn't set tool_loaded yet, so is_lane_loaded()
        # reads False for ~20s during the load. Only auto-stop on a real
        # desync — the lane was confirmed loaded once and then disappeared
        # (e.g. an unload that didn't go through _oams_unload). Until the first
        # confirmation, just wait; don't stop mid-load.
        if self._is_lane_loaded:
            if self._is_lane_loaded():
                st.toolhead_confirmed = True
            elif st.toolhead_confirmed:
                self.logger.debug(
                    f"{self.fps_name}: lane no longer loaded to toolhead, "
                    f"stopping monitor")
                self._running = False
                self.state.reset()
                return self.reactor.NEVER
            else:
                # Load still finishing — keep the monitor alive but idle.
                return eventtime + MONITOR_INTERVAL

        # Only run detection while actively printing — skip during
        # idle, PRINT_START, homing, probing, manual moves, etc.
        if self._is_printing and not self._is_printing():
            # Reset clog tracking so it doesn't accumulate during non-print
            st.clog_start_time = None
            st.clog_start_extruder = None
            return eventtime + MONITOR_INTERVAL_IDLE

        # Don't monitor during engagement verification
        if st.engagement_in_progress:
            return eventtime + MONITOR_INTERVAL

        # Grace period after load
        if st.since and (eventtime - st.since) < self.stuck_load_grace:
            return eventtime + MONITOR_INTERVAL

        try:
            encoder = self._oams.encoder_clicks
            pressure = float(getattr(self.fps, 'fps_value', 0.5))

            # Calculate encoder delta
            encoder_delta = 0
            if st.last_encoder is not None:
                encoder_delta = abs(encoder - st.last_encoder)
            st.last_encoder = encoder

            # Run detection checks
            self._check_stuck_spool(eventtime, encoder_delta, pressure)

            if self.enable_clog:
                self._check_clog(eventtime, encoder_delta, pressure)

        except Exception as e:
            self.logger.error(f"Monitor error on {self.fps_name}: {e}")

        return eventtime + MONITOR_INTERVAL

    # ---- Stuck spool detection ----

    def _check_stuck_spool(self, eventtime: float, encoder_delta: int, pressure: float) -> None:
        """
        Detect stuck spool: encoder stopped + FPS pressure low.
        When the spool is jammed, the follower can't push filament so
        encoder stops and FPS pressure drops (no tension in buffer).

        :param eventtime: current reactor time.
        :param encoder_delta: encoder clicks moved since the last check.
        :param pressure: current FPS pressure reading.
        """
        st = self.state

        # Grace after engagement check
        if st.engagement_checked_at and (eventtime - st.engagement_checked_at) < 6.0:
            return

        encoder_moving = encoder_delta >= self.stuck_min_encoder
        pressure_low = pressure <= self.stuck_pressure_low

        if not encoder_moving and pressure_low:
            # Condition met — start or continue dwell timer
            if not st.stuck_active:
                if st.stuck_start_time is None:
                    st.stuck_start_time = eventtime
                    self.logger.debug(
                        f"{self.fps_name}: stuck spool timer started "
                        f"(pressure={pressure:.2f}, encoder_delta={encoder_delta})")
                elif (eventtime - st.stuck_start_time) >= self.stuck_dwell:
                    # Dwell exceeded — fire callback
                    st.stuck_active = True
                    msg = (
                        f"Stuck spool on {self.fps_name}: encoder stopped "
                        f"({encoder_delta} clicks), FPS pressure {pressure:.2f}")
                    self.logger.info(msg)
                    if self._on_stuck_spool:
                        self._on_stuck_spool(self.fps_name, msg)
        else:
            # Condition cleared
            if st.stuck_start_time is not None or st.stuck_active:
                if pressure >= self.stuck_pressure_clear or encoder_moving:
                    if st.stuck_active and self._on_stuck_cleared:
                        self._on_stuck_cleared(self.fps_name)
                    st.stuck_active = False
                    st.stuck_start_time = None

    # ---- Clog detection ----

    def _check_clog(self, eventtime: float, encoder_delta: int, pressure: float) -> None:
        """
        Detect clog: extruder advancing but encoder not tracking.
        When filament is clogged between the extruder and spool, the
        extruder keeps pushing (pressure stays at target) but the encoder
        doesn't move (filament isn't flowing through the buffer).

        :param eventtime: current reactor time.
        :param encoder_delta: encoder clicks moved since the last check.
        :param pressure: current FPS pressure reading.
        """
        st = self.state

        # Grace after load
        if st.last_lane_change_time and \
           (eventtime - st.last_lane_change_time) < self.clog_post_load_grace:
            return

        # Need extruder position to detect clog
        try:
            extruder_obj = self.fps.extruder if hasattr(self.fps, 'extruder') else None
            extruder_pos = extruder_obj.last_position if extruder_obj is not None else None
        except Exception:
            extruder_obj = None
            extruder_pos = None
        if extruder_pos is None:
            return

        # fps.extruder is the ACTIVE toolhead extruder, and each extruder keeps
        # its own position counter. A toolchange mid-window swaps the object, so
        # comparing last_position across two extruders yields a phantom advance
        # (e.g. a manual T-command while paused fired a false clog with the
        # buffer parked at neutral). Restart the window on the new extruder.
        if (st.clog_start_time is not None
            and extruder_obj is not st.clog_start_extruder_obj):
            st.clog_start_time = None

        # Pressure near target (extruder is pushing normally)
        pressure_near_target = abs(pressure - CLOG_PRESSURE_TARGET) <= CLOG_PRESSURE_BAND

        # Encoder not moving despite extrusion
        encoder_stuck = encoder_delta <= CLOG_ENCODER_SLACK

        if pressure_near_target and encoder_stuck:
            if st.clog_start_time is None:
                st.clog_start_time = eventtime
                st.clog_start_extruder = extruder_pos
                st.clog_start_extruder_obj = extruder_obj
                st.clog_start_encoder = st.last_encoder
            else:
                elapsed = eventtime - st.clog_start_time
                extrusion_delta = abs(extruder_pos - st.clog_start_extruder)
                encoder_total = abs((st.last_encoder or 0) - (st.clog_start_encoder or 0))

                # The FPS buffer decouples the feeder encoder from the extruder:
                # at low flow the follower feeds in small bursts, so each check's
                # encoder_delta stays under the per-check slack even though
                # filament IS flowing. Only a *cumulative* lack of encoder
                # movement over the window is a real (downstream/nozzle) clog. If
                # the encoder has made real cumulative progress, the filament is
                # moving — restart the window instead of firing a false clog.
                if encoder_total > CLOG_ENCODER_SLACK:
                    st.clog_start_time = eventtime
                    st.clog_start_extruder = extruder_pos
                    st.clog_start_extruder_obj = extruder_obj
                    st.clog_start_encoder = st.last_encoder
                elif elapsed >= self.clog_dwell and extrusion_delta >= self.clog_extrusion_window:
                    # Clog confirmed: extruder advanced past the window while the
                    # encoder stayed essentially stationary the whole time.
                    if not st.clog_active:
                        st.clog_active = True
                        msg = (
                            f"Clog detected on {self.fps_name}: "
                            f"extruder advanced {extrusion_delta:.1f}mm, "
                            f"encoder moved {encoder_total} clicks, "
                            f"FPS pressure {pressure:.2f} "
                            f"(dwell {elapsed:.1f}s)")
                        self.logger.info(msg)
                        if self._on_clog:
                            self._on_clog(self.fps_name, msg)
        else:
            # Reset clog tracking
            st.clog_start_time = None
            st.clog_start_extruder = None
            st.clog_start_encoder = None
            if st.clog_active:
                st.clog_active = False


def load_config_prefix(config: ConfigWrapper) -> afcAMS:
    """
    Klipper entry point: construct an OpenAMS unit for an ``[AFC_OpenAMS ...]`` section.

    :param config: ConfigWrapper for the unit's config section.
    :return afcAMS: the constructed OpenAMS unit object.
    """
    return afcAMS(config)
