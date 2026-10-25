# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# This file includes an implementation of ACE commands adapted from the Kobra-S1/ACEPRO project. https://github.com/Kobra-S1/ACEPRO
# Originally authored by Kobra-S1 and licensed under the GNU General Public License v3.0.
# Full license text available at: https://www.gnu.org/licenses/gpl-3.0.html

"""AFC unit driver for Anycubic ACE PRO filament changers."""

# Stuck spool detection (see _check_stuck) is our own implementation of an idea
# from the Kobra-S1/ACEPRO project: watch the ACE firmware's continuous
# feed-assist run time instead of an encoder to spot a jam.
#   https://github.com/Kobra-S1/ACEPRO

from __future__ import annotations

import atexit
import collections
import contextlib
import glob
import json
import logging
import logging.handlers
import os
import queue
import struct
import threading
import time
import traceback
from configparser import Error as error
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from gcode import GCodeCommand, GCodeDispatch
    from reactor import SelectReactor as Reactor
    from extras.AFC_lane import AFCLane
    from extras.heaters import PrinterHeaters

try: from extras.AFC_utils import ERROR_STR
except:
    error_str = f"Error when trying to import AFC_utils.ERROR_STR\n{traceback.format_exc()}"
    raise error(error_str)

try: from extras.AFC_lane import AFCLaneState
except:
    error_str = ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc())
    raise error(error_str)

try: from extras.AFC_unit import afcUnit
except:
    error_str = ERROR_STR.format(import_lib="AFC_unit", trace=traceback.format_exc())
    raise error(error_str)
# Uncomment below for real RFID integration
apply_filament_defaults = None
rgb_array_to_hex = None
# Uncomment below once RFID and spoolman creation support is added
# try:
#     from extras.AFC_RFID import (
#         rgb_array_to_hex, apply_filament_defaults,
#         sync_rfid_to_spoolman, get_auto_spoolman_create,
#         log_new_filament, log_new_spool,
#     )
# except Exception:
#     # AFC_RFID is optional. Provide self-contained fallbacks for the lane-info
#     # path; the Spoolman helpers stay None so their guarded paths no-op.
#     sync_rfid_to_spoolman = None
#     get_auto_spoolman_create = None
#     log_new_filament = None
#     log_new_spool = None

#     def rgb_array_to_hex(color_array: Any) -> str:
#         """
#         [r, g, b] -> '#rrggbb' (fallback when AFC_RFID isn't present).

#         :param color_array: [r, g, b] values
#         :return str: '#rrggbb', or '' when the input is not a usable colour
#         """
#         if isinstance(color_array, (list, tuple)) and len(color_array) >= 3:
#             try:
#                 r, g, b = (int(color_array[0]), int(color_array[1]),
#                            int(color_array[2]))
#                 return f"#{r & 0xFF:02x}{g & 0xFF:02x}{b & 0xFF:02x}"
#             except (TypeError, ValueError):
#                 return ""
#         return ""

#     def apply_filament_defaults(lane: Any, slot_info: Any,
#                                 color_converter: Optional[Any] = None,
#                                 afc_defaults: Optional[dict] = None) -> None:
#         """
#         Apply the ACE's RFID material/color/temps to a lane when not already
#         set, a dependency-free copy of AFC_RFID.apply_filament_defaults so the
#         ACE can populate the lane (shows in Mainsail) with no AFC_RFID present.

#         :param lane: lane to populate
#         :param slot_info: the slot's inventory dict
#         :param color_converter: optional callable turning a raw colour into hex
#         :param afc_defaults: optional default material/colour to fill gaps
#         """
#         info = slot_info or {}
#         has_material = getattr(lane, "material", None) not in (None, "")
#         has_color = getattr(lane, "color", None) not in (None, "", "#000000")
#         has_ext_temp = getattr(lane, "extruder_temp", None) is not None
#         has_bed_temp = getattr(lane, "bed_temp", None) is not None

#         material = (info.get("material", "") or "")
#         if material.lower() == "unknown":
#             material = ""
#         color_hex = info.get("color_hex", "") or ""
#         if not color_hex and color_converter is not None:
#             raw = info.get("color", [0, 0, 0])
#             if raw != [0, 0, 0]:
#                 color_hex = color_converter(raw)

#         if not has_material and material:
#             lane.material = material
#         if not has_color and color_hex:
#             lane.color = color_hex if color_hex.startswith("#") else "#" + color_hex
#         if not has_ext_temp and info.get("extruder_temp") is not None:
#             try:
#                 lane.extruder_temp = float(info.get("extruder_temp"))
#             except (TypeError, ValueError):
#                 pass
#         if not has_bed_temp and info.get("bed_temp") is not None:
#             try:
#                 lane.bed_temp = float(info.get("bed_temp"))
#             except (TypeError, ValueError):
#                 pass

#         if afc_defaults is not None:
#             if not getattr(lane, "material", None):
#                 dm = afc_defaults.get("default_material_type")
#                 if dm:
#                     lane.material = dm
#             if not getattr(lane, "color", None):
#                 dc = afc_defaults.get("default_color")
#                 if dc:
#                     lane.color = dc

MODE_COMBINED = "combined"
MODE_DIRECT = "direct"

# Stock ACE2 firmware rejects a feed/unwind speed above 100; the ace2_rfid speed
# patch raises that (e.g. 140) via max_feed_speed. Time bounds stay pinned to this
# stock ceiling, since over-estimating is safe and under-estimating times out.
FEED_SPEED_CEILING = 100.0
# Toolhead sensor poll period while a load feed runs (s): 20 ms is under 3 mm
# at 140 mm/s and 0.5 mm at the approach speed.
SENSOR_POLL_S = 0.02
# The unit drops a stop sent within ~250 ms of the move it ends (its setup
# delay), so a stop is confirmed against the status and resent.
STOP_CONFIRM_TRIES = 3
# Times a long load feed that ends in feed_error is resent for what it has left.
LOAD_FEED_ERROR_RESENDS = 2

def _ams_box_logo(title: str, n_slots: int, name: str) -> str:
    """
    AMS-style unit logo: a titled box with one spool bay per slot, fronted by
    the R/E/A/D/Y banner (prep console output). ASCII borders for portability.

    :param title: Box title (e.g. the unit model name).
    :param n_slots: Number of slot bays to draw.
    :param name: Unit name shown under the box.
    :return str: HTML-tagged multi-line logo string.
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
    body = "\n".join(f"{L}  {r}" for L, r in zip("READY", rows))
    return f"<span class=success--text>{body}</span>\n   {name}\n"


def _ams_box_logo_error(title: str, n_slots: int, name: str) -> str:
    """
    Error variant of the AMS-style logo (red box, ERROR banner).

    :param title: Box title (e.g. the unit model name).
    :param n_slots: Number of slot bays sizing the box width.
    :param name: Unit name shown under the box.
    :return str: HTML-tagged multi-line error logo string.
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
    body = "\n".join(f"{L}  {r}" for L, r in zip("ERROR", rows))
    return f"<span class=error--text>{body}</span>\n   {name}\n"


def _derive_buffer_state(slot_sensor: Optional[Dict[str, Any]]) -> str:
    """
    Map a slot's decoded buffer switches to a per-lane buffer state.

    BUF_BACK = buffer retracted (tension, "feed me"),
    BUF_RST = buffer at rest/home, and neither-set = buffer extended, which is
    the normal loaded/feeding position (the shared BUF_FEED sensor). Only
    BUF_RST/BUF_BACK are per-slot, so 'neutral' is derived from their absence
    rather than the shared bit.

    :param slot_sensor: A slot dict from GET_SENSOR_STATE's ``slot_sensors``
        (``{insert, empty, buf_rst, buf_back}``), or None/{}.
    :return str: 'advancing' (buf_back), 'rest' (buf_rst), 'neutral' (extended /
        normal), or '' when no sensor data is available.
    """
    if not slot_sensor:
        return ''
    if slot_sensor.get('buf_back'):
        return 'advancing'
    if slot_sensor.get('buf_rst'):
        return 'rest'
    return 'neutral'


class afcACE(afcUnit):
    """
    AFC unit driver for Anycubic ACE PRO filament changers.

    Drives a 4-slot ACE PRO over a USB serial connection (see ACEConnection
    and the module docstring) rather than physical AFC steppers/sensors. All
    filament transport (feed, unwind, feed assist) is issued as serial commands
    to the ACE firmware; lane prep/load state, the virtual hub, RFID inventory
    and Spoolman sync are derived from the ACE's reported slot status. Supports
    'combined' and 'direct' toolhead modes, dryer control, stuck-spool
    detection, feed/hub/TD-1 calibration, and a feed-assist watchdog that keeps
    assist reconciled to the active lane.
    """
    SLOTS_PER_UNIT = 4
    _LOGO_TITLE = "ACE PRO"   # AFC_ACE2 overrides this for the Pro 2 prep logo
    _CONNECT_MAX_RETRIES = 5
    _CONNECT_RETRY_DELAYS = [2.0, 3.0, 5.0, 8.0, 10.0]
    # After the startup retries give up, keep looking for the unit this often,
    # so one switched on (or power cycled) later still connects.
    _CONNECT_WATCH_INTERVAL = 15.0
    _SERIAL_LOG_MAX_BYTES = 10 * 1024 * 1024
    # V1 ACE preloads filament to the hub on insert, so it is loaded_to_hub at
    # once. AFC_ACE2 sets this False, as its preload only grips at the slot.
    _preloads_to_hub_on_insert = True
    # V1 ACE reads tags through the firmware (get_filament_info); AFC_ACE2 sets
    # this False since it reads them host-side (AFC_ACE2_rfid).
    _uses_firmware_rfid = True

    def __init__(self, config: ConfigWrapper) -> None:
        """
        Configure the ACE unit and register its gcode commands.

        :param config: Klipper ConfigWrapper for this unit section. Reads the
            serial port, mode, feed/retract speeds, dryer and stuck-detection
            limits, feed-assist options, calibration step sizes and FPS
            thresholds, then registers the per-unit and base AFC_ACE_* gcode
            commands and the temperature_ace sensor factory.
        """
        super().__init__(config)
        self.type = config.get('type', 'ACE')
        self.stepperless_drive: bool = True

        # Multiple ACEs share one by-id descriptor, so pin each by USB port with
        # /dev/serial/by-path/..., or use serial_port: auto to bind the unit whose
        # firmware id (cable order) matches ace_index, re-resolved on reconnect.
        self.serial_port = config.get("serial_port")
        # Which ACE in cable order this section is (= the firmware's get_info
        # id). Used only when serial_port: auto; harmless otherwise. Defaults to
        # 1 (the head/only unit), matching the firmware's 1-based numbering.
        self.ace_index = config.getint("ace_index", 1, minval=1)
        mode = config.get("mode", MODE_COMBINED).lower().strip()
        if mode not in (MODE_COMBINED, MODE_DIRECT):
            error_str = (f"[{config.get_name()}] invalid mode '{mode}'. "
                         f"Use '{MODE_COMBINED}' or '{MODE_DIRECT}'.")
            raise error(error_str)
        self.mode = mode

        # Command cap. Stock firmware rejects speed > 100; raise it only on a unit
        # with the ace2_rfid speed patch, or the firmware NAKs the feed.
        self.max_feed_speed = config.getfloat("max_feed_speed", FEED_SPEED_CEILING, minval=1.0)
        # Default to 100, the stock cap / near the measured rate plateau. Past
        # ~90 the real throughput gain tapers and encoder-slip risk grows; drop
        # feed_speed/retract_speed in config if a unit reports slip.
        self.feed_speed = min(config.getfloat("feed_speed", 100.0), self.max_feed_speed)
        self.retract_speed = min(config.getfloat("retract_speed", 100.0), self.max_feed_speed)
        # Max seconds to wait for the unit to finish its own load-to-toolhead-
        # and-back cycle (on connect and on insert) before we issue commands.
        # Scales with PTFE / hub distance, so make it tunable per unit.
        self.prep_ready_timeout = config.getfloat("prep_ready_timeout", 90.0, minval=1.0)
        # How long to wait for a feed/unwind to START moving before treating it
        # as a no-start (slow units / long PTFE want a longer window).
        self.feed_departure_timeout = config.getfloat("feed_departure_timeout", 3.0, minval=0.5)
        # Post-feed load recovery: pulse this many mm at a time, up to this many
        # seconds, to nudge filament onto the toolhead sensor if the main feed
        # under-shot it.
        self.load_retry_pulse = config.getfloat("load_retry_pulse", 100.0, minval=1.0)
        self.load_retry_timeout = config.getfloat("load_retry_timeout", 10.0, minval=1.0)
        # The last load_approach_length mm of a toolhead feed (and retry pulses) run at
        # load_approach_speed and stop on the toolhead sensor, so the rollers do not slip
        # on stopped filament and grind a divot. 0 feeds the whole way at feed_speed.
        self.load_approach_length = config.getfloat("load_approach_length", 30.0, minval=0.0)
        self.load_approach_speed = config.getfloat("load_approach_speed", 50.0, minval=1.0)
        # Extra mm (beyond dist_hub) to retract when ejecting a hub-staged lane,
        # so the filament clears the hub and pulls fully back into the unit.
        self.eject_buffer = config.getfloat("eject_buffer", 475.0, minval=0.0)
        # Safety cap on the dryer set-point, AFC_ACE_DRY clamps the commanded temp
        # to this to avoid cooking filament / over-driving the heater.
        self.max_dryer_temperature = config.getfloat("max_dryer_temperature", 55.0, minval=0.0)
        # AFC_ACE_DRY ROTATE=1 turns free spools while drying: every
        # dry_rotate_interval seconds each lane whose slot has nothing in the
        # feed gears gets a slow dry_rotate_length mm rollback. The spool and
        # the gears share the slot's motor, so the spool turns and no filament
        # moves. See _dry_rotate_tick for when it holds off.
        self.dry_rotate_length = config.getfloat("dry_rotate_length", 150.0, minval=10.0,
                                                 maxval=1000.0)
        self.dry_rotate_speed = config.getfloat("dry_rotate_speed", 15.0, minval=5.0, maxval=50.0)
        self.dry_rotate_interval = config.getfloat("dry_rotate_interval", 300.0, minval=30.0)
        self._unit_load_to_hub = config.getboolean("load_to_hub", None)
        self._default_feed_assist = config.getboolean("use_feed_assist", True)
        # Watchdog: on each heartbeat (when idle) make sure the lane on the
        # active tool actually has its feed assist running, in case a pickup /
        # tool-change event didn't enable it.
        self._assist_watchdog = config.getboolean("assist_watchdog", True)
        # Stuck-spool detection (opt-in): pause when the firmware's continuous
        # feed-assist run time (cont_assist_time) passes stuck_time while printing.
        self._stuck_detection = config.getboolean("stuck_spool_detection", False)
        self._stuck_time = config.getfloat("stuck_time", 4.0, minval=2.0)
        self.calibration_step = config.getfloat("calibration_step", 50.0)
        self.baud_rate = config.getint("baud_rate", 115200)

        # Uncomment below once RFID and spoolman creation support is added
        # self.auto_spoolman_create = config.getboolean("auto_spoolman_create", False)

        self._ace: Optional[ACEConnection] = None
        self._slot_map: dict[str, int] = {}
        self._feed_assist_active: set[int] = set()
        # Slots manually stopped via AFC_ACE_FEED_ASSIST ENABLE=0. The watchdog and
        # event reconcile won't restart these; any explicit start (the command
        # with ENABLE=1 or a load/toolchange path) clears the suppression.
        self._assist_suppressed: set[int] = set()
        # One-shot latch so a sustained jam pauses once, not every heartbeat.
        self._stuck_tripped = False
        self._slot_inventory: List[Dict[str, Any]] = [{} for _ in range(self.SLOTS_PER_UNIT)]
        self._operation_active = False
        # Drying spool rotation (AFC_ACE_DRY ROTATE=1): the reactor timer, when the
        # cycle was started, and the slot being turned right now.
        self._dry_rotate_timer: Any = None
        self._dry_rotate_started = 0.0
        self._dry_rotate_slot: Optional[int] = None
        self._dry_rotate_hold = ""
        self._cached_hw_status: Dict[str, Any] = {}
        # GET_TEMP channels (ACE2 only), polled each heartbeat and read by
        # temperature_ace sensors configured with a specific channel.
        self._cached_temp_info: Dict[str, Any] = {}
        # Last GET_SENSOR_STATE decode (ACE2 only), polled each heartbeat and
        # read by get_status for each lane's buffer state.
        self._cached_sensor_state: Dict[str, Any] = {}
        # Last-seen "current action" (preload/feeding/rollback/…) derived from
        # the hardware status, for transition logging + AFC_ACE_STATUS. '' = idle.
        self._current_action = ""
        self._prev_states_stale = False
        self._prev_slot_states: dict[str, bool] = {}
        self._hub_load_suppressed: set[str] = set()
        # Error the unit reported during the last _wait_for_feed_complete (e.g.
        # 'feed_error' when its encoder saw the filament stop), or None.
        self._last_move_error: Optional[str] = None
        # Monotonic timestamp of the last hardware-status update, so a stale
        # cache (callback wedged while RX still alive) is detectable. None until
        # the first heartbeat lands.
        self._hw_status_time: Optional[float] = None

        self.gcode: GCodeDispatch = self.printer.lookup_object('gcode')

        # Defer serial connection until reactor is running (klippy:ready)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        # Reconcile feed assist to the active lane on every tool load.
        self.printer.register_event_handler("afc:tool_loaded", self._handle_tool_loaded)
        # A tool pickup may not fire afc:tool_loaded, so also reconcile assist
        # whenever the active extruder changes.
        self.printer.register_event_handler("extruder:activate_extruder",
                                            self._handle_extruder_activated)

        # Hand the serial port and its autodetect claim back on every teardown:
        # a FIRMWARE_RESTART keeps the process, so _ACE_CLAIMED_PORTS survives.
        self.printer.register_event_handler("klippy:disconnect", self._handle_klippy_disconnect)

        self.gcode.register_mux_command('AFC_ACE_CALIBRATE', "UNIT", self.name,
                                        self.cmd_AFC_ACE_CALIBRATE,
                                        desc=self.cmd_AFC_ACE_CALIBRATE_help)
        self.gcode.register_mux_command('AFC_ACE_BUFFER_STATE', "UNIT", self.name,
                                        self.cmd_AFC_ACE_BUFFER_STATE,
                                        desc=self.cmd_AFC_ACE_BUFFER_STATE_help)
        self.gcode.register_mux_command('AFC_ACE_CALIBRATE_HUB', "UNIT", self.name,
                                        self.cmd_AFC_ACE_CALIBRATE_HUB,
                                        desc=self.cmd_AFC_ACE_CALIBRATE_HUB_help)
        self.gcode.register_mux_command('AFC_ACE_STATUS', "UNIT", self.name,
                                        self.cmd_AFC_ACE_STATUS, desc=self.cmd_AFC_ACE_STATUS_help)
        self.gcode.register_mux_command('AFC_ACE_LANE_RESET', "UNIT", self.name,
                                        self.cmd_AFC_ACE_LANE_RESET,
                                        desc=self.cmd_AFC_ACE_LANE_RESET_help)
        self.gcode.register_mux_command('AFC_ACE_FEED_ASSIST', "UNIT", self.name,
                                        self.cmd_AFC_ACE_FEED_ASSIST,
                                        desc=self.cmd_AFC_ACE_FEED_ASSIST_help)
        self.gcode.register_mux_command('AFC_ACE_CMD', "UNIT", self.name, self.cmd_AFC_ACE_CMD,
                                        desc=self.cmd_AFC_ACE_CMD_help)
        self.gcode.register_mux_command('AFC_ACE_MATERIAL_INFO', "UNIT", self.name,
                                        self.cmd_AFC_ACE_MATERIAL_INFO,
                                        desc=self.cmd_AFC_ACE_MATERIAL_INFO_help)
        self.gcode.register_mux_command('AFC_ACE_SET_MATERIAL', "UNIT", self.name,
                                        self.cmd_AFC_ACE_SET_MATERIAL,
                                        desc=self.cmd_AFC_ACE_SET_MATERIAL_help)
        self.gcode.register_mux_command('AFC_ACE_SENSOR_STATE', "UNIT", self.name,
                                        self.cmd_AFC_ACE_SENSOR_STATE,
                                        desc=self.cmd_AFC_ACE_SENSOR_STATE_help)
        self.gcode.register_mux_command('AFC_ACE_FEED_TEST', "UNIT", self.name,
                                        self.cmd_AFC_ACE_FEED_TEST,
                                    desc=self.cmd_AFC_ACE_FEED_TEST_help)
        self.gcode.register_mux_command('AFC_ACE_STUCK_SPOOL_DETECTION', "UNIT", self.name,
                                        self.cmd_AFC_ACE_STUCK_SPOOL_DETECTION,
                                        desc=self.cmd_AFC_ACE_STUCK_SPOOL_DETECTION_help)
        self.gcode.register_mux_command('AFC_ACE_FEED_INFO', "UNIT", self.name,
                                        self.cmd_AFC_ACE_FEED_INFO,
                                        desc=self.cmd_AFC_ACE_FEED_INFO_help)
        self.gcode.register_mux_command('AFC_ACE_RFID_DUMP', "UNIT", self.name,
                                        self.cmd_AFC_ACE_RFID_DUMP,
                                        desc=self.cmd_AFC_ACE_RFID_DUMP_help)
        self.gcode.register_mux_command('AFC_ACE_RFID_RESCAN', "UNIT", self.name,
                                        self.cmd_AFC_ACE_RFID_RESCAN,
                                        desc=self.cmd_AFC_ACE_RFID_RESCAN_help)
        self.gcode.register_mux_command('AFC_ACE_FAN', "UNIT", self.name, self.cmd_AFC_ACE_FAN,
                                        desc=self.cmd_AFC_ACE_FAN_help)
        self.gcode.register_mux_command('AFC_ACE_TEMP_INFO', "UNIT", self.name,
                                        self.cmd_AFC_ACE_TEMP_INFO,
                                        desc=self.cmd_AFC_ACE_TEMP_INFO_help)
        self.afc.function.register_mux_command(self.afc.show_macros, 'AFC_ACE_DRY', "UNIT",
                                               self.name, self.cmd_AFC_ACE_DRY,
                                               self.cmd_AFC_ACE_DRY_help,
                                               self.cmd_AFC_ACE_DRY_options)
        self.afc.function.register_mux_command(self.afc.show_macros, 'AFC_ACE_DRY_STOP', "UNIT",
                                               self.name, self.cmd_AFC_ACE_DRY_STOP,
                                               self.cmd_AFC_ACE_DRY_STOP_help)

        # Register the temperature_ace sensor factory for [temperature_sensor]
        # sections using sensor_type: temperature_ace.
        try:
            from extras.temperature_ace import TemperatureACE
            pheaters: PrinterHeaters = self.printer.load_object(config, "heaters")
            pheaters.add_sensor_factory("temperature_ace", TemperatureACE)
            # Fluidd alias (aht2x -> aht10) so Fluidd shows ACE 2 humidity; distinct
            # from OpenAMS's aht3x so the factories don't clobber each other.
            pheaters.add_sensor_factory("aht2x", TemperatureACE)
        except Exception as e:
            logging.info("AFC_ACE: temperature_ace factory not registered (%s); "
                         "use [temperature_ace <name>] config sections instead", e)

    def handle_connect(self) -> None:
        """
        Build the prep logos, map lanes to ACE slots, and register the
        deferred serial connect and feed-assist reconciliation event handlers.

        Sets each lane's custom load/unload commands and seeds its loadless
        ``_load_state`` to False so the native virtual hub reads clear until a
        sync or load drives the loaded-to-hub state.
        """
        super().handle_connect()

        self.logo = _ams_box_logo(self._LOGO_TITLE, len(self.lanes), self.name)
        self.logo_error = _ams_box_logo_error(self._LOGO_TITLE, len(self.lanes), self.name)

        # Build (and validate) the slot map: 1-based config index → 0-based slot.
        self._slot_map.update(self._build_slot_map())
        # ACE lanes have no load pin, so _load_state would default True. Start it
        # False so the virtual hub reads clear until a sync or load sets it.
        for lane in self.lanes.values():
            lane._load_state = False

    def _handle_klippy_disconnect(self) -> None:
        """
        Release the ACE serial port and its autodetect claim on teardown.

        The claim lives in a process-global set that a soft restart does not
        reset, so the rebuilt unit cannot re-probe its own port until the old
        one hands it back. Safe with no live connection.
        """
        self._stop_dry_rotate()
        ace = self._ace
        if ace is not None:
            with contextlib.suppress(Exception):
                ace.disconnect()
        self._ace = None

    def _handle_tool_loaded(self, lane: Any) -> None:
        """
        Event handler for ``afc:tool_loaded``, reconcile feed assist to the
        newly loaded lane.

        :param lane: Event payload, usually the loaded lane but sometimes the
            extruder object; the active lane name is resolved from it (with
            mode-specific fallbacks) and feed assist is reconciled on a
            background reactor callback.
        """
        # The payload is usually the lane, but the toolchanger can send the
        # extruder object: use its loaded lane, then fall back to AFC's current.
        name = lane.name
        if self.mode == MODE_DIRECT:
            if name not in self._slot_map:
                loaded = getattr(lane, 'lane_loaded', None)  # extruder payload
                if loaded in self._slot_map:
                    name = loaded
                else:
                    name = self.afc.current
        else:
            # Combined mode: one shared toolhead, so only act when the event
            # is directly for one of our lanes (a lane load or a manual
            # SET_LANE_LOADED recovery). Don't fall back to afc.current.
            if name not in self._slot_map:
                return
        # Defer the ACE handshake off the toolhead's gcode greenlet so it
        # does not hold position over the wipe tower (and ooze) while the
        # ACE acks the feed-assist start.
        self.afc.reactor.register_callback(lambda et, n=name: self._reconcile_feed_assist(n))

    def _reconcile_feed_assist(self, name: Optional[str]) -> None:
        """
        Switch ACE feed assist to the active lane (background callback).

        Stop assist on the previously-active slot(s) and wait for the ACE to
        ack, then start assist on the new slot. Both run in this background
        reactor callback, off the toolhead's gcode greenlet, so the acks
        never hold the toolhead over the wipe tower.

        :param name: Name of the lane that should now have feed assist, or a
            lane on another unit (ours is then stopped), or None to leave
            assist untouched.
        """
        if name is None:
            return
        active_slot = self._slot_map.get(name)
        if active_slot is not None:
            # Stop the previously-active slot(s) first (stop_feed_assist_sync
            # waits for the ACE ack), then start the new slot, so the ACE is
            # never asked to feed two slots at once.
            for slot in list(self._feed_assist_active):
                if slot != active_slot:
                    self._stop_feed_assist(slot)
            active_lane = self.afc.lanes.get(name)
            # Only start assist once the toolhead sensor is triggered; starting
            # earlier fights the load feed, which enables assist itself.
            try:
                at_toolhead = (active_lane is not None
                               and active_lane.get_toolhead_pre_sensor_state())
            except Exception:
                at_toolhead = False
            if (active_lane is not None
                and self._use_feed_assist(active_lane)
                and active_slot not in self._feed_assist_active
                and active_slot not in self._assist_suppressed
                and at_toolhead):
                self._start_feed_assist(active_slot)
        elif name in self.afc.lanes:
            # The active tool is a real lane on another unit, stop ours.
            for slot in list(self._feed_assist_active):
                self._stop_feed_assist(slot)
        # else: couldn't resolve the active tool, leave assist untouched
        # rather than risk stopping a valid one.

    def _create_serial_logger(self) -> Optional[Any]:
        """
        Dedicated logger for ACE serial comms -> AFC_ACE_serial.log.

        Size-capped: a RotatingFileHandler rolls over at _SERIAL_LOG_MAX_BYTES
        with one backup. Records go through a queue so the serial thread never
        blocks on file I/O; the listener is stopped at interpreter exit.

        :return Optional[Any]: the logger, or None when no log file is configured
        """
        logger = logging.getLogger("AFC_ACE_serial_file")
        if logger.handlers:
            return logger
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            log_path = self.printer.start_args.get("log_file", None)
            if not log_path:
                return None
            log_file = Path(log_path).parent / "AFC_ACE_serial.log"
            handler = logging.handlers.RotatingFileHandler(str(log_file),
                                                           maxBytes=self._SERIAL_LOG_MAX_BYTES,
                                                           backupCount=1)
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
            log_q: "queue.Queue[logging.LogRecord]" = queue.Queue(-1)
            ql = logging.handlers.QueueListener(log_q, handler, respect_handler_level=False)
            ql.start()
            atexit.register(ql.stop)
            logger.addHandler(logging.handlers.QueueHandler(log_q))
            self._serial_ql = ql
            return logger
        except Exception:
            return None

    def _handle_ready(self) -> None:
        """
        ``klippy:ready`` handler: seed each lane's virtual-hub load state, then
        defer the ACE serial connect onto a reactor callback so it runs once the
        reactor is fully up.
        """
        # Seed each lane's virtual-hub state now, so the hub is right even if the
        # ACE never connects. No-op for physical hubs.
        for lane in self.lanes.values():
            self._set_hub_state(lane, lane.loaded_to_hub)
        # PREP waits on this (_wait_first_connect): the connect's autodetect
        # runs off the reactor, so PREP would otherwise reach the lanes first.
        self._first_connect_pending = True
        self.afc.reactor.register_callback(self._deferred_ace_connect)

    def _make_connection(self, reactor: Reactor, serial_port: str, logger: Any,
                          baud_rate: int) -> "ACEConnection":
        """
        Create the ACE serial transport. AFC_ACE2 (Pro 2) overrides this to
        return a V2 (binary protobuf) connection instead of the V1 JSON one.

        :param reactor: Klipper reactor for scheduling I/O
        :param serial_port: serial device path, or "auto"
        :param logger: logger passed to the connection
        :param baud_rate: serial baud rate
        :return ACEConnection: the new connection
        """
        return ACEConnection(reactor=reactor, serial_port=serial_port, logger=logger,
                             baud_rate=baud_rate, ace_index=self.ace_index)

    def _deferred_ace_connect(self, eventtime: float) -> None:
        """
        Connect to ACE hardware after reactor is fully running.

        :param eventtime: reactor event time (unused)
        """
        serial_logger = self._create_serial_logger() or self.logger
        last_err = None
        for attempt in range(self._CONNECT_MAX_RETRIES):
            try:
                self._ace = self._make_connection(self.afc.reactor, self.serial_port, serial_logger,
                                                  self.baud_rate)
                try:
                    self._ace.connect()
                finally:
                    self._first_connect_pending = False
                if attempt > 0:
                    self.logger.info(f"ACE {self.name}: connected on attempt {attempt + 1}")
                break
            except Exception as e:
                last_err = e
                self._ace = None
                if attempt < self._CONNECT_MAX_RETRIES - 1:
                    delay = self._CONNECT_RETRY_DELAYS[attempt]
                    self.logger.info(f"ACE {self.name}: serial port not available, "
                                     f"retrying in {delay:.0f}s "
                                     f"(attempt {attempt + 1}/{self._CONNECT_MAX_RETRIES})")
                    self.afc.reactor.pause(self.afc.reactor.monotonic() + delay)
        else:
            self.logger.error(f"ACE {self.name}: failed to connect at {self.serial_port} "
                              f"after {self._CONNECT_MAX_RETRIES} attempts: {last_err}")
            self.logger.info(f"ACE {self.name}: will keep looking for it every "
                             f"{self._CONNECT_WATCH_INTERVAL:.0f}s")
            waketime = self.afc.reactor.monotonic() + self._CONNECT_WATCH_INTERVAL
            self.afc.reactor.register_callback(self._watch_for_ace, waketime)
            return
        self._finish_connect()

    def _watch_for_ace(self, eventtime: float) -> None:
        """
        Try one connect to a unit that was missing at startup, and look again
        later while it stays missing.

        :param eventtime: reactor event time (unused)
        """
        if (self._ace is not None
            and self._ace.connected):
            return
        try:
            self._ace = self._make_connection(self.afc.reactor, self.serial_port,
                                              self._create_serial_logger() or self.logger,
                                              self.baud_rate)
            self._ace.connect()
        except Exception as e:
            self._ace = None
            self.logger.debug(f"ACE {self.name}: still not found: {e}")
            waketime = self.afc.reactor.monotonic() + self._CONNECT_WATCH_INTERVAL
            self.afc.reactor.register_callback(self._watch_for_ace, waketime)
            return
        self.logger.info(f"ACE {self.name}: found it, connecting late")
        self._finish_connect()

    def _finish_connect(self) -> None:
        """
        Set the unit up once its serial link is connected: RFID on, feed-check
        tuning, slot state, and the heartbeat and reconnect callbacks. Then
        fires ``afc_ace:connected`` so listeners can redo their own setup.
        """
        if self._ace is None:
            return

        self.logger.info(f"ACE {self.name}: connected, mode={self.mode}, "
                         f"port={self.serial_port}, slots={self.SLOTS_PER_UNIT}")

        # Enable RFID reader so get_filament_info returns spool data
        try:
            self._ace.enable_rfid()
            self.logger.debug(f"ACE {self.name}: RFID enabled")
        except Exception as e:
            self.logger.warning(f"ACE {self.name}: enable_rfid failed (non-fatal): {e}")

        # Apply encoder feed-check tuning (ACE2 only; base is a no-op).
        self._apply_feed_check()

        # The ACE runs its own detection cycle after connect and won't answer
        # queries until it finishes, so wait for it to report ready.
        self._wait_for_ace_ready(timeout=self.prep_ready_timeout)

        # Seed slot status from a single get_status (covers all 4 slots, which is
        # all we need for prep/loaded state). Per-slot RFID detail is pulled by
        # _sync_inventory below, now that the unit is idle.
        try:
            hw_status = self._ace.get_status(timeout=3.0)
            if isinstance(hw_status, dict):
                self._cached_hw_status = hw_status
                for i, slot_data in enumerate(hw_status.get("slots", [])):
                    if (i < self.SLOTS_PER_UNIT
                        and isinstance(slot_data, dict)):
                        self._slot_inventory[i]["status"] = slot_data.get("status", "")
        except Exception as e:
            self.logger.debug(f"ACE initial get_status failed: {e}")

        # Sync RFID data and lane loaded states
        self._sync_inventory()
        self._sync_slot_loaded_state()

        # Re-baseline on the first heartbeat, or every 'ready' slot would read as
        # a fresh insert and (on V1) be auto-staged to the hub.
        self._prev_states_stale = True

        # Register callback for heartbeat status updates
        self._ace.status_callback = self._on_hw_status_callback
        # Re-establish feed assist after a USB drop/reconnect (common on this
        # hardware). The ACE firmware resets on reconnect and forgets any
        # running feed assist, so resync it back to the active lane.
        self._ace.reconnect_callback = self._on_ace_reconnect
        self.printer.send_event("afc_ace:connected", self, False)

    def _on_ace_reconnect(self) -> None:
        """
        Reconnect callback: the ACE reset and forgot its feed-assist state,
        so drop our stale tracking and defer a resync of assist for the lane
        currently loaded on the active toolhead.
        """
        # Drop stale tracking so the watchdog's "already active" guard re-issues
        # assist for the lane loaded on the active toolhead.
        if self._feed_assist_active:
            self.logger.info("ACE reconnected, re-establishing feed assist for the active lane")
        self._feed_assist_active.clear()
        # After a reset the unit reports slots 'empty' for several polls, so force
        # a re-baseline to avoid a false runout on a still-present spool.
        self._prev_states_stale = True
        # Defer off the reconnect/serial path so the ACE acks don't block it.
        self.afc.reactor.register_callback(self._resync_assist_after_reconnect)
        # The reset also undid any setup listeners made (AFC_ACE2_rfid's
        # identify off), so tell them, off the serial path too.
        self.afc.reactor.register_callback(lambda et: self.printer.send_event(
                                           "afc_ace:connected", self, True))

    def _apply_feed_check(self) -> None:
        """
        Push encoder feed-check tuning to the unit. No-op for V1 ACE;
        afcACE2 overrides it to send the V2 SET_FEED_CHECK command.
        """
        pass

    def _resync_assist_after_reconnect(self, eventtime: float) -> None:
        """
        Reactor callback that re-issues feed assist for the active lane after
        a reconnect, unless a load/unload is in progress (which sets assist
        itself on completion).

        :param eventtime: Reactor event time (unused).
        """
        # Don't fight an in-flight load/unload, which sets assist itself.
        if not self._operation_active:
            # The firmware may still hold assist on a slot after a quick
            # reconnect, so stop every slot first to avoid the single-assist
            # limit (error_2), then reconcile the active lane.
            if (self._ace is not None
                and self._ace.connected):
                for s in range(self.SLOTS_PER_UNIT):
                    try:
                        self._ace.stop_feed_assist_sync(s)
                    except Exception:
                        pass
                self._feed_assist_active.clear()
            self._maybe_assist_watchdog()
        # The ACE forgets its feed-check window across a reset too, re-apply it.
        self._apply_feed_check()

    def _on_hw_status_callback(self, response: Dict[str, Any]) -> None:
        """
        Process heartbeat status from ACE, keep lane states in sync.

        :param response: Raw status dict from the ACE heartbeat. Its ``result``
            payload (or the dict itself) is cached, and while no operation is
            active it drives slot-state sync, the assist watchdog, and stuck
            spool detection.
        """
        if not isinstance(response, dict):
            return
        result = response.get("result", response)
        if not isinstance(result, dict):
            return
        # ACE2 get_temp/get_sensor_state replies also land here; only get_status
        # carries 'slots', so route the others to their own caches.
        if ("slots" not in result
            and any(k in result for k in ("ptc1_temp", "box1_temp", "env_temp"))):
            self._cached_temp_info = result
            return
        if ("slots" not in result
            and "sensor_bitmask" in result):
            self._cached_sensor_state = result
            return
        if not isinstance(result.get("slots"), list):
            # Ack/raw-fields reply to an async command (stop_feed_filament,
            # stop_feed_assist, update_feeding_speed, ...), not a status
            # frame. Ignore it rather than wiping the real cached status.
            return
        self._cached_hw_status = result
        # Stamp the update so a wedged callback shows up as a stale cache.
        try:
            self._hw_status_time = self.afc.reactor.monotonic()
        except Exception:
            pass
        # Track the unit's live action before the operation-active gate, since it
        # is most informative during a load/unload.
        action = self._derive_action(result)
        if action != self._current_action:
            self.logger.debug(
                f'ACE {self.name}: {(self._current_action or "idle")} -> {(action or "idle")}')
            self._current_action = action
        if self._operation_active:
            return
        self._sync_slot_states(result)
        self._maybe_assist_watchdog()
        self._check_stuck(result)

    @staticmethod
    def _derive_action(result: Dict[str, Any]) -> str:
        """
        Uniform 'current action' string for both V1 and ACE2 status dicts.

        Reports what the unit is doing right now, ``preload`` / ``feeding`` /
        ``rollback`` / … tagged with the slot, or ``''`` when idle. V1 slots
        carry the state in ``status`` and also expose a top-level ``action``
        while busy; ACE2 carries it per-slot in ``slot_status``. Checking both
        keys gives one consistent value across the two transports.

        :param result: An ACE status dict (``get_status`` result payload).
        :return str: The current action string, or '' when idle.
        """
        if not isinstance(result, dict):
            return ""
        busy = ("feeding", "rollback", "preloading", "preload", "unwinding")
        for slot in result.get("slots", []) or []:
            if not isinstance(slot, dict):
                continue
            st = str(slot.get("slot_status") or slot.get("status") or "").strip()
            if st in busy:
                idx = slot.get("index")
                return f"{st}(slot {idx})" if idx is not None else st
        # No busy slot, fall back to the V1 top-level action field if present.
        return str(result.get("action") or "").strip()

    def get_status(self, eventtime: Optional[float] = None) -> Dict[str, Any]:
        """
        Extend the base unit status with the ACE's live hardware state so it
        is visible to Moonraker/Mainsail/Fluidd, not just the logs.

        Adds (on top of the base lanes/extruders/hubs/buffers):
          ace_connected, ace_status (ready/busy), ace_action (the derived
          current action), ace_temp, ace_humidity (ACE2 only), ace_dryer, and a
          compact per-slot inventory (status, rfid, sku, material, uid, color).

        :param eventtime: Reactor event time (unused; Klipper status interface).
        :return dict: The status dict.
        """
        response: Dict[str, Any] = super().get_status(eventtime)
        hw = self._cached_hw_status or {}
        temp_info = self._cached_temp_info or {}
        response["ace_connected"] = bool(self._ace and self._ace.connected)
        response["ace_status"] = hw.get("status", "")
        response["ace_action"] = self._current_action
        # V1 carries temp/humidity in the status payload; the ACE 2 falls back to
        # the GET_TEMP cache (env channel).
        response["ace_temp"] = hw.get("temp")
        if (response["ace_temp"] is None
            and temp_info.get("env_temp")):
            response["ace_temp"] = temp_info.get("env_temp")
        if "humidity" in hw:
            response["ace_humidity"] = hw.get("humidity")
        elif temp_info.get("env_humidity"):
            response["ace_humidity"] = temp_info.get("env_humidity")
        # dryer_status also carries target_temp, duration and remain_time.
        dryer = hw.get("dryer_status") or {}
        response["ace_dryer"] = dryer.get("status", "")
        response["ace_dryer_target"] = dryer.get("target_temp")
        response["ace_dryer_remain"] = dryer.get("remain_time")
        response["ace_dryer_duration"] = dryer.get("duration")
        response["ace_dry_rotate"] = self._dry_rotating
        # Expose the cache age and a stale flag (past ~3 heartbeats), since a
        # throwing callback with RX alive would otherwise freeze this silently.
        age = None
        conn = self._ace
        if (self._hw_status_time is not None
            and conn is not None):
            try:
                age = max(0.0, self.afc.reactor.monotonic() - self._hw_status_time)
            except Exception:
                age = None
        response["ace_status_age"] = age
        response["ace_status_stale"] = bool(response["ace_connected"] and age is not None
                                            and age > HEARTBEAT_INTERVAL * 3)
        slots = []
        inv_list = self._slot_inventory or []
        # Per-lane buffer state from the cached GET_SENSOR_STATE poll (ACE2).
        slot_sensors = (self._cached_sensor_state or {}).get("slot_sensors") or []
        for i in range(self.SLOTS_PER_UNIT):
            inv = inv_list[i] if i < len(inv_list) else {}
            ss = slot_sensors[i] if i < len(slot_sensors) else None
            slots.append({
                "index": i,
                "status": inv.get("status", ""),
                "rfid": inv.get("rfid"),
                "sku": inv.get("sku", ""),
                "material": inv.get("material", ""),
                "uid": inv.get("uid", ""),
                "color": inv.get("color", [0, 0, 0]),
                # Buffer: 'advancing' (extruder pulling), 'rest' (home),
                # 'neutral' (extended = normal feeding), '' (no sensor data).
                "buffer": _derive_buffer_state(ss),
            })
        response["ace_slots"] = slots
        return response

    def _is_virtual_hub(self, lane: AFCLane) -> bool:
        """
        Return whether the lane's hub is a virtual (pinless) hub.

        :param lane: Lane whose hub to inspect.
        :return bool: True when the lane has a hub that reports is_virtual_pin().
        """
        hub = lane.hub_obj
        return (hub is not None and hasattr(hub, 'is_virtual_pin') and hub.is_virtual_pin())

    def _set_hub_state(self, lane: AFCLane, state: bool) -> None:
        """
        Reflect live hub-switch occupancy on the lane's raw_load_state.

        Staged filament parks just before the hub switch, so raw_load_state
        tracks the live switch (True only while threaded through to the
        toolhead), not the latched loaded_to_hub. Latching it for a staged lane
        would make TOOL_LOAD see "hub not clear" and block the lane's own load.

        - Virtual (pinless) hub: no physical sensor, so synthesize the live signal
          from tool_loaded (occupied when loaded to toolhead, clear when only
          staged or empty).
        - Real hub switch: its own pin callback drives raw_load_state, so we must
          not override it here.

        :param lane: Lane whose hub-presence signal to drive.
        :param state: Unused for the live signal (kept for call-site symmetry);
            occupancy is derived from tool_loaded, not the staged flag.
        """
        if self._is_virtual_hub(lane):
            lane._load_state = lane.tool_loaded

    def _sync_slot_states(self, hw_status: Dict[str, Any]) -> None:
        """
        Sync lane prep/load state from ACE hardware slot status.

        Uses _prev_slot_states (per-lane dict) and _prev_states_stale to
        avoid false insert/remove detection after load/unload operations.

        :param hw_status: ACE hardware status dict containing the per-slot
            ``slots`` list whose ``status`` field drives each lane's prep/load
            state, insert/remove runout handling, and tooled-state restore.
        """
        slots = hw_status.get("slots", [])

        # Per-slot 'empty' reads are unreliable while the unit reports 'busy', so
        # they must not clear a staged lane or fire a runout then.
        unit_busy = hw_status.get("status") == "busy"

        resync_prev = self._prev_states_stale
        if resync_prev:
            self._prev_states_stale = False

        for i, slot_data in enumerate(slots):
            if (i < self.SLOTS_PER_UNIT
                and isinstance(slot_data, dict)):
                self._slot_inventory[i]["status"] = slot_data.get("status", "")

        for lane in self.lanes.values():
            slot = self._get_slot(lane.name)
            if (slot >= len(slots)
                or not isinstance(slots[slot], dict)):
                continue

            # Live virtual-hub occupancy from tool_loaded (see _set_hub_state).
            if self._is_virtual_hub(lane):
                lane._load_state = lane.tool_loaded

            slot_status = slots[slot].get("status", "")
            slot_ready = slot_status == "ready"
            slot_transient = slot_status in ("shifting", "feeding", "unwinding")

            if not slot_transient:
                # Vivid-style: slot "ready" means filament inserted -> prep_state.
                lane.prep_state = slot_ready
                if not slot_ready:
                    # Only clear the staged state on a genuine ready -> not-ready
                    # removal; the ACE 2 reports 'empty' for a few polls at power-up.
                    if (self._prev_slot_states.get(lane.name)
                        and not resync_prev
                        and not unit_busy):
                        lane.loaded_to_hub = False
                        self._set_hub_state(lane, False)
                elif (self._preloads_to_hub_on_insert
                      and not resync_prev
                      and not self._prev_slot_states.get(lane.name)
                      and not lane.tool_loaded):
                    # empty -> ready: a fresh insert, which V1 preloads to the hub.
                    # Only on the transition, so an unloaded spool is not re-staged.
                    if (lane.load_to_hub
                        and not lane.loaded_to_hub):
                        lane.loaded_to_hub = True
                        self._set_hub_state(lane, True)

            prep_done = lane._afc_prep_done

            # Slot ready but lane stuck in NONE, restore loaded state.
            if (slot_ready
                and not slot_transient
                and prep_done
                and lane.status == AFCLaneState.NONE):
                if (lane.tool_loaded
                    and hasattr(lane, 'extruder_obj')
                    and lane.extruder_obj.lane_loaded == lane.name):
                    self.logger.info(f"ACE: {lane.name} restoring TOOLED state from saved vars")
                    lane.loaded_to_hub = True
                    self._set_hub_state(lane, True)
                    lane.sync_to_extruder()
                    if self.afc.current == lane.name:
                        self.afc.spool.set_active_spool(lane.spool_id)
                        self.lane_tool_loaded(lane)
                        lane.status = AFCLaneState.TOOLED
                    else:
                        self.lane_tool_loaded_idle(lane)
                        # Mark idle tool-loaded lanes TOOLED too, otherwise
                        # status stays NONE and this restore block re-fires on
                        # every hardware poll (log spam / wasted work).
                        lane.status = AFCLaneState.TOOLED
                    lane.enable_buffer()
                    if self._use_feed_assist(lane):
                        self._start_feed_assist(slot)
                else:
                    if lane.name in self._hub_load_suppressed:
                        lane._load_suppressed = True
                    eventtime = self.afc.reactor.monotonic()
                    self._dispatch_load_runout(lane, eventtime, True)

            # Filament removed, skip on first callback after operation
            # (stale _prev_slot_states) and while the unit is busy (its own
            # cycle flickers slots 'empty', which would fire a false runout).
            if (not slot_ready
                and not slot_transient
                and not resync_prev
                and not unit_busy):
                self._hub_load_suppressed.discard(lane.name)
                prev_ready = self._prev_slot_states.get(lane.name)
                if prev_ready:
                    eventtime = self.afc.reactor.monotonic()
                    self._dispatch_load_runout(lane, eventtime, False)

            if not slot_transient:
                self._prev_slot_states[lane.name] = slot_ready

    def _dispatch_load_runout(self, lane: AFCLane, eventtime: float, load_state: bool) -> None:
        """
        Run a lane's load/runout handler with its failures made visible.

        This runs inside the serial transport's frame dispatch, which would
        swallow any exception into a debug line. Catching it here logs it to AFC
        and raises an AFC error so a failed unload is visible and recoverable.

        :param lane: Lane whose load switch changed.
        :param eventtime: Reactor event time of the change.
        :param load_state: True when filament is present at the load switch.
        """
        try:
            lane.handle_load_runout(eventtime, load_state)
        except Exception as e:
            self.logger.error(f"{lane.name}: {'load' if load_state else 'runout'} handling "
                              f"failed: {e}", traceback=traceback.format_exc())
            # Leave AFC in a state the user can act on: the unload aborted
            # part-way, so the toolhead still holds filament.
            try:
                self.afc.error.AFC_error(f"Lane {lane.name} {'load' if load_state else 'runout'} "
                                         f"handling failed: {e}\n"
                                         "Filament may still be loaded in the toolhead, check the "
                                         "toolhead before resuming.",
                                         pause=self.afc.function.in_print())
            except Exception:
                self.logger.debug("Unable to raise AFC error for the failed runout handler",
                                  traceback=traceback.format_exc())

    # ── Lane parameter helpers ──
    def _get_bowden_length(self, cur_lane: AFCLane) -> float:
        """
        Total feed distance: dist_hub (lane→hub) + afc_bowden_length (hub→toolhead).

        :param cur_lane: Lane to compute the feed distance for.
        :return float: dist_hub plus the hub's afc_bowden_length.
        """
        dist = cur_lane.dist_hub
        hub = cur_lane.hub_obj
        if hub is not None:
            dist += getattr(hub, 'afc_bowden_length', 0)
        return dist

    def _get_unload_length(self, cur_lane: AFCLane) -> float:
        """
        Total retract distance: dist_hub + afc_unload_bowden_length.

        :param cur_lane: Lane to compute the retract distance for.
        :return float: dist_hub plus the hub's afc_unload_bowden_length
            (falling back to afc_bowden_length).
        """
        dist = cur_lane.dist_hub
        hub = cur_lane.hub_obj
        if hub is not None:
            dist += getattr(hub, 'afc_unload_bowden_length', getattr(hub, 'afc_bowden_length', 0))
        return dist

    def _get_eject_length(self, cur_lane: AFCLane) -> float:
        """
        Retract distance for an eject/unload. When the lane is loaded to the
        toolhead, retract the full path (dist_hub + bowden). When it's only
        staged at the hub, the filament just spans the lane->hub gap, so retract
        dist_hub + eject_buffer (config, default 475mm) to clear the hub and
        pull it into the unit.

        :param cur_lane: Lane to compute the eject distance for.
        :return float: Full unload length when tool-loaded, else
            dist_hub + eject_buffer.
        """
        if cur_lane.tool_loaded:
            return self._get_unload_length(cur_lane)
        return cur_lane.dist_hub + self.eject_buffer

    def _use_feed_assist(self, cur_lane: AFCLane) -> bool:
        """
        Resolve whether feed assist should run for a lane.

        :param cur_lane: Lane to check.
        :return bool: The lane's per-lane use_feed_assist when set, else the unit
            default, AND only for the single active tool. The ACE/ACE2 can
            feed-assist just one lane at a time, so assisting idle loaded lanes
            only churns against that limit (the unit rejects the extras with
            error_2).
        """
        feed_assist = getattr(cur_lane, 'use_feed_assist', None)
        if feed_assist is None:
            feed_assist = self._default_feed_assist
        if not feed_assist:
            return False
        return self._lane_is_active_tool(cur_lane)

    def _lane_is_active_tool(self, cur_lane: AFCLane) -> bool:
        """
        True only for the lane that is the current active tool.

        The ACE/ACE2 can only feed-assist one lane at a time, so feed assist
        follows the engaged tool: for toolchanger units the tool on the shuttle
        (on_shuttle() is False for docked-but-loaded tools, True for the active
        one and for single-toolhead setups); for combined-mode units the lane
        loaded into the toolhead (afc.current).

        :param cur_lane: Lane to test.
        :return bool: True if this lane is the active tool.
        """
        extruder_obj = getattr(cur_lane, 'extruder_obj', None)
        if (extruder_obj is not None
            and getattr(extruder_obj, 'tc_unit_name', None) is not None):
            on_shuttle = getattr(extruder_obj, 'on_shuttle', None)
            return bool(on_shuttle()) if callable(on_shuttle) else True
        cur = self.afc.current
        return cur is None or cur == cur_lane.name

    def _build_slot_map(self) -> Dict[str, Any]:
        """
        Map each lane name to its 0-based slot from the 1-based config index,
        validating range and uniqueness. An out-of-range or duplicate index would
        otherwise silently collide two lanes onto one slot (or map to a
        nonexistent slot the sync loop skips), so raise a clear config error.

        :return dict: lane name -> 0-based slot index.
        :raises error: On an out-of-range or duplicate lane index.
        """
        slot_map = {}
        owner: Dict[int, str] = {}
        for lane_name, lane in self.lanes.items():
            idx = lane.index
            if not (1 <= idx <= self.SLOTS_PER_UNIT):
                error_str = (
                    f"ACE {self.name}: lane '{lane_name}' has index {idx}, outside this unit's "
                    f"slots 1..{self.SLOTS_PER_UNIT}, fix the lane's index in the config.")
                raise error(error_str)
            slot = idx - 1
            if slot in owner:
                error_str = (
                    f"ACE {self.name}: lanes '{owner[slot]}' and '{lane_name}' both map to slot "
                    f"{slot} (index {idx}), each lane needs a unique index.")
                raise error(error_str)
            owner[slot] = lane_name
            slot_map[lane_name] = slot
        return slot_map

    def _get_slot(self, lane_name: str) -> int:
        """
        Map a lane name to its 0-based ACE slot index.

        An unknown lane falls back to slot 0 (preserving caller expectations),
        which would drive the wrong physical spool, so it is logged as a warning
        once per lane. A correctly configured unit never hits this.

        :param lane_name: Name of the lane.
        :return int: The mapped slot, or 0 if the lane is unknown.
        """
        slot = self._slot_map.get(lane_name)
        if slot is None:
            warned = getattr(self, "_slot_warned", None)
            if warned is None:
                self._slot_warned: Set[str] = set()
                warned = self._slot_warned
            if lane_name not in warned:
                warned.add(lane_name)
                self.logger.warning(f"ACE {self.name}: lane {lane_name!r} is not in this "
                                    f"unit's slot map {sorted(self._slot_map)}, defaulting "
                                    f"to slot 0; check the unit/lane configuration")
            return 0
        return slot

    # ── Unit interface overrides ──
    def prep_load(self, lane: AFCLane) -> None:
        """
        No-op for ACE: filament transport to the hub is handled in
        prep_post_load via serial feed, not during prep_load.

        :param lane: Lane being prepped (unused).
        """
        pass

    def check_runout(self, cur_lane: AFCLane) -> bool:
        """
        ACE supports runout detection during printing.

        :param cur_lane: Lane to check (unused beyond context).
        :return bool: True when AFC reports a print in progress, else False.
        """
        try:
            return bool(self.afc.function.is_printing())
        except Exception:
            return False

    def on_filament_insert(self, lane: AFCLane) -> None:
        """
        ACE-specific insertion: refresh RFID inventory, apply spool defaults, sync to spoolman.

        :param lane: Lane whose slot had filament inserted; its RFID inventory
            is refreshed, filament defaults/Spoolman are applied, a freshly
            staged or remembered spool id is restored, and the base
            on_filament_insert is then called.
        """
        slot = self._get_slot(lane.name)
        if slot >= self.SLOTS_PER_UNIT:
            return
        saved_spool_id = lane.spool_id
        # A spool_id present at insert is a freshly-staged scan or a remembered
        # spool; the staged id lets a fresh scan survive remember_spool off.
        staged_spool_id = getattr(lane, '_afc_staged_spool_id', None)
        lane._afc_staged_spool_id = None
        self.afc.spool.clear_values(lane)
        # V1 ACE identifies the spool through the firmware here; the ACE 2 reads
        # the tag host-side (AFC_ACE2_rfid) on the post_insert event instead.
        if self._uses_firmware_rfid:
            self._refresh_slot_inventory(slot)
            slot_info = self._slot_inventory[slot]
            if apply_filament_defaults is not None:
                apply_filament_defaults(
                    lane, slot_info,
                    color_converter=rgb_array_to_hex,
                    afc_defaults={
                        "default_material_type": self.afc.default_material_type,
                        "default_color": getattr(self.afc, "default_color", None),
                    })
            # V1 ACE does not create/match Spoolman: SKUs aren't unique and the
            # reader has no per-tag UID, so only the tag values are applied.
        # If the ACE slot had no RFID identity of its own (lane.spool_id still
        # unset after the inventory read), restore the spool assigned before
        # insertion when it was freshly scanned/staged or remember_spool is on.
        if (saved_spool_id
            and not lane.spool_id
            and (staged_spool_id is not None
                 or lane.remember_spool)):
            self.afc.spool.set_spoolID(lane, saved_spool_id)
        else:
            # No saved spool to restore, and apply_filament_defaults is disabled
            # (see top of file): fall back to _set_values' material/weight
            # defaulting until real RFID/Spoolman identification is wired back in.
            self.afc.spool._set_values(lane)
        self.lane_illuminate_spool(lane)
        self._hub_load_suppressed.discard(lane.name)
        self.afc.save_vars()
        try:
            self.prep_post_load(lane)
        except Exception as e:
            self.logger.error(f"ACE on_filament_insert: prep_post_load error for {lane.name}: {e}")
        # The spool has just spun (preload/feed-to-hub swept the tag past the
        # reader). Notify integrations (e.g. ACE2 RFID) so they can read the tag
        # now, while it's freshly positioned. Fire-and-forget; never fatal.
        try:
            self.printer.send_event("afc_ace:post_insert", lane)
        except Exception as e:
            self.logger.error(
                f"ACE on_filament_insert: post_insert event error for {lane.name}: {e}")
        super().on_filament_insert(lane)

    def on_filament_remove(self, lane: AFCLane) -> None:
        """
        ACE-specific removal: clear slot inventory and hub state.

        :param lane: Lane whose slot was emptied; its cached RFID inventory and
            loaded-to-hub state are cleared.
        """
        slot = self._get_slot(lane.name)
        if slot < self.SLOTS_PER_UNIT:
            self._clear_slot_inventory(slot)
        self._hub_load_suppressed.discard(lane.name)
        lane.loaded_to_hub = False
        self._set_hub_state(lane, False)

    # ── TD-1 support ──
    def calibrate_td1(self, cur_lane: AFCLane, dis: float, tol: float) -> Tuple[bool, str, float]:
        """
        Calibrate TD-1 bowden length by feeding until TD-1 device detects filament.

        Wraps _calibrate_td1_inner with the _operation_active guard.

        :param cur_lane: Lane to calibrate.
        :param dis: Step size in mm for incremental feeding.
        :param tol: Tolerance (unused for ACE).
        :return tuple: (success, message, distance) from _calibrate_td1_inner.
        """
        self._operation_active = True
        try:
            return self._calibrate_td1_inner(cur_lane, dis, tol)
        finally:
            self._operation_active = False
            self._prev_states_stale = True

    def prep_capture_td1(self, cur_lane: AFCLane) -> Optional[Tuple[bool, str]]:
        """
        ACE TD-1 capture is triggered from prep_post_load (after hub feed).

        Return non-None to prevent the base _prep_capture_td1 from running
        the stepper-based path.

        :param cur_lane: Lane being prepped.
        :return Optional[tuple]: (True, message) when TD-1 capture is handled elsewhere, else
            None to fall through to the base stepper path.
        """
        if (cur_lane.td1_when_loaded
            and cur_lane.loaded_to_hub):
            return True, "TD-1 capture handled by prep_post_load"
        return None

    def capture_td1_data(self, cur_lane: AFCLane) -> Tuple[bool, str]:
        """
        ACE TD-1 data capture using feed_filament instead of stepper moves.

        Wraps _capture_td1_data_inner with the _operation_active guard after
        validating the connection and TD-1 device id.

        :param cur_lane: Lane to capture TD-1 data for.
        :return tuple: (success, message) tuple, (False, reason) when prerequisites
            are unmet, else the result from _capture_td1_data_inner.
        """
        if (self._ace is None
            or not self._ace.connected):
            return False, "ACE not connected"
        if cur_lane.td1_device_id is None:
            return False, "td1_device_id not set for lane"
        if cur_lane.td1_bowden_length is None:
            return False, "td1_bowden_length not set, run TD-1 calibration first"

        slot = self._get_slot(cur_lane.name)
        valid, msg = self.afc.function.check_for_td1_id(cur_lane.td1_device_id)
        if not valid:
            return False, msg

        self._operation_active = True
        try:
            return self._capture_td1_data_inner(cur_lane, slot)
        finally:
            self._operation_active = False

    def _capture_td1_data_inner(self, cur_lane: AFCLane, slot: int) -> Tuple[bool, str]:
        """
        Feed to the hub (if needed) and on to the TD-1 device, read the TD-1
        data, then retract back. Implementation of capture_td1_data.

        :param cur_lane: Lane to capture TD-1 data for.
        :param slot: 0-based ACE slot index for the lane.
        :return tuple: (True, message) if TD-1 data was captured, else
            (False, message).
        """
        if self._ace is None:
            return False, "ACE not connected"
        dist_hub = cur_lane.dist_hub

        if not cur_lane.loaded_to_hub:
            self.logger.info(f"ACE TD-1 capture: feeding {dist_hub}mm to hub for {cur_lane.name}")
            self._wait_for_ace_ready()
            self._ace.feed_filament(slot, dist_hub, self.feed_speed)
            self._wait_for_ace_ready()

        feed_dist = cur_lane.td1_bowden_length
        if feed_dist is None:
            return False, f"td1_bowden_length not set for {cur_lane.name}"
        self.logger.info(f"ACE TD-1 capture: feeding {feed_dist}mm to TD-1 for {cur_lane.name}")

        compare_time = datetime.now()
        self._wait_for_ace_ready()
        self._ace.feed_filament(slot, feed_dist, self.feed_speed)
        self._wait_for_ace_ready()

        self.afc.reactor.pause(self.afc.reactor.monotonic() + 5.0)

        success = self.get_td1_data(cur_lane, compare_time)
        if not success:
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 3.0)
            success = self.get_td1_data(cur_lane, compare_time)

        retract_dist = feed_dist + (0 if cur_lane.loaded_to_hub else dist_hub)
        self.logger.info(f"ACE TD-1 capture: retracting {retract_dist}mm for {cur_lane.name}")
        try:
            self._wait_for_ace_ready()
            self._ace.unwind_filament(slot, retract_dist, self.retract_speed)
            self._wait_for_feed_complete(slot, retract_dist, self.retract_speed)
        except Exception as e:
            self.logger.error(f"ACE TD-1 capture retract failed: {e}")

        if success:
            return True, f"TD-1 data captured for {cur_lane.name}"
        return False, "TD-1 data not captured (unload completed)"

    def _calibrate_td1_inner(self, cur_lane: AFCLane, dis: float,
                             tol: float) -> Tuple[bool, str, float]:
        """
        Measure td1_bowden_length by feeding to the hub then incrementally on
        from the hub until the TD-1 device detects filament, then save the
        result to config. Implementation of calibrate_td1.

        :param cur_lane: Lane to calibrate.
        :param dis: Step size in mm (defaults to 50 when not positive).
        :param tol: Tolerance (unused).
        :return tuple: (success, message, measured_distance).
        """
        if (self._ace is None
            or not self._ace.connected):
            return False, "ACE not connected", 0

        if cur_lane.td1_device_id is None:
            return False, (f"Cannot calibrate TD-1 for {cur_lane.name}, td1_device_id "
                           "is a required field in AFC_hub or per AFC_lane"), 0

        valid, msg = self.afc.function.check_for_td1_id(cur_lane.td1_device_id)
        if not valid:
            return False, msg, 0

        slot = self._get_slot(cur_lane.name)
        step_size = dis if dis > 0 else 50.0
        max_bowden_length = 6000
        cur_hub = cur_lane.hub_obj
        dist_hub = cur_lane.dist_hub

        self.logger.info(f"ACE calibrate_td1: feeding slot {slot} in {step_size}mm steps, "
                         f"max {max_bowden_length}mm, TD-1 device={cur_lane.td1_device_id}")

        self._wait_for_ace_ready()

        # Phase 1: Get filament to hub position
        has_real_hub = (cur_hub is not None and hasattr(cur_hub, 'switch_pin')
                        and getattr(cur_hub, 'switch_pin', 'virtual').lower() != 'virtual')
        if cur_lane.loaded_to_hub:
            self.logger.info("ACE calibrate_td1: filament already at hub, skipping hub feed")
        elif (has_real_hub
              and cur_hub is not None):
            self.logger.info("ACE calibrate_td1: feeding to hub sensor")
            self._ace.feed_filament(slot, dist_hub + 200, self.feed_speed)
            hub_timeout = self.afc.reactor.monotonic() + 15.0
            while self.afc.reactor.monotonic() < hub_timeout:
                if cur_hub.state:
                    break
                self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.1)
            self._wait_for_ace_ready()
            if not cur_hub.state:
                self._ace.unwind_filament(slot, dist_hub + 200, self.retract_speed)
                self._wait_for_ace_ready()
                return False, f"Hub sensor did not trigger for {cur_lane.name}", 0
        elif dist_hub > 0:
            self.logger.info(f"ACE calibrate_td1: feeding {dist_hub}mm to virtual hub")
            self._ace.feed_filament(slot, dist_hub, self.feed_speed)
            self._wait_for_ace_ready()

        # Phase 2: Feed incrementally from hub, checking TD-1 after each step
        bow_pos = 0.0
        compare_time = datetime.now()
        while not self.get_td1_data(cur_lane, compare_time):
            if bow_pos > max_bowden_length:
                retract = bow_pos + (0 if cur_lane.loaded_to_hub else dist_hub)
                self._ace.unwind_filament(slot, retract, self.retract_speed)
                self._wait_for_ace_ready()
                return False, f"TD-1 failed to detect filament after {bow_pos:.0f}mm", bow_pos

            compare_time = datetime.now()
            bow_pos += step_size
            self._ace.feed_filament(slot, step_size, self.feed_speed)
            self._wait_for_ace_ready()
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 5.0)

        self.logger.info(f"ACE calibrate_td1: TD-1 detected filament at {bow_pos:.1f}mm")

        # Retract back
        if cur_lane.loaded_to_hub:
            retract_dist = bow_pos
        else:
            retract_dist = bow_pos + dist_hub
        try:
            self._wait_for_ace_ready()
            self._ace.unwind_filament(slot, retract_dist, self.retract_speed)
            self._wait_for_feed_complete(slot, retract_dist, self.retract_speed)
        except Exception as e:
            self.logger.error(f"ACE calibrate_td1: retract failed: {e}")

        # Save td1_bowden_length
        old_td1 = cur_lane.td1_bowden_length
        cur_lane.td1_bowden_length = bow_pos
        cal_msg = f"\n td1_bowden_length: New: {bow_pos} Old: {old_td1}"
        self.afc.function.ConfigRewrite(cur_lane.fullname, "td1_bowden_length", bow_pos, cal_msg)
        self.afc.save_vars()

        return True, (f"ACE TD-1 calibration: filament detected at {bow_pos:.1f}mm.\n"
                      f"td1_bowden_length: {bow_pos:.0f} (was {old_td1})\n"
                      f"Value saved to config."), bow_pos

    # ── Prep and post-load ──
    @contextlib.contextmanager
    def _operation(self) -> Any:
        """
        Mark a host-driven ACE motion in progress: the heartbeat gate
        (`_on_hw_status_callback`) then skips slot-state sync and the feed-assist
        watchdog for the duration, so a background poll can't fire a runout, re-
        stage a lane, or re-enable assist against an in-flight feed/unwind.
        Saves/restores the previous flag so nested operations compose, and marks
        prev-states stale on exit so the next poll re-baselines cleanly.

        :return Any: a context manager that holds the operation flag
        """
        prev = self._operation_active
        self._operation_active = True
        try:
            yield
        finally:
            self._operation_active = prev
            self._prev_states_stale = True

    def _feed_to_hub_probing(self, lane: AFCLane, slot: int, dist_hub: float) -> bool:
        """
        Feed the filament into the unit and on to the hub, giving an RFID
        integration a chance to read the tag *during* the load.

        A stage-read listener (``afc_ace:stage_probe_begin``) runs the whole RFID
        scan on a continuous feed and reports how far it fed via ``ctx['fed']``
        plus a mandatory ``ctx['initial']`` load. The remainder (``initial +
        dist_hub - fed``) is then staged in a single move at full feed speed. With
        no listener this is just one ``initial + dist_hub`` feed.

        :param lane: lane being staged
        :param slot: 0-based ACE slot index for the lane
        :param dist_hub: lane-to-hub distance in mm
        :return bool: False when the spool was removed mid-stage, else True
        """
        # prep_post_load (the only caller) already checked self._ace is
        # connected before calling in; treat a connection lost in the meantime
        # the same as a removed spool so this feed is skipped, not crashed.
        if self._ace is None:
            return False
        ctx = {"active": False, "done": False, "initial": 0.0, "fed": 0.0, "removed": False}
        try:
            self.printer.send_event("afc_ace:stage_probe_begin", lane, ctx)
        except Exception as e:
            self.logger.error(f"ACE prep_post_load: stage_probe_begin error: {e}")
        # Mandatory initial load (replaces the bypassed factory staging), plus the
        # distance the RFID scan already fed. Net load stays initial + dist_hub.
        initial = max(float(ctx.get("initial") or 0.0), 0.0)
        total = initial + dist_hub
        fed = max(float(ctx.get("fed") or 0.0), 0.0)
        removed = bool(ctx.get("removed"))
        try:
            # Bail out if the spool was pulled back out mid-stage; feeding an empty
            # slot trips the ACE's stuck-spool error.
            if (not removed
                and self._slot_reports_empty(slot)):
                removed = True
            if not removed:
                # Stage the remainder in one move at full feed speed. The scan read
                # pauses for seconds and can leave the unit "busy", so wait ready.
                remaining = total - fed
                if remaining > 0.5:
                    self._wait_for_ace_ready(timeout=self.prep_ready_timeout)
                    self._ace.feed_filament(slot, remaining, self.feed_speed)
                    self._wait_for_feed_complete(slot, remaining, self.feed_speed)
                    fed += remaining
        finally:
            try:
                self.printer.send_event("afc_ace:stage_probe_end", lane, ctx)
            except Exception as e:
                self.logger.error(f"ACE prep_post_load: stage_probe_end error: {e}")
        if removed:
            # Spool gone: don't unwind or mark it staged. Signal the abort so the
            # caller resets prep state instead of reporting the lane loaded.
            self.logger.debug(f"ACE prep_post_load: {lane.name} filament removed during staging, "
                              f"aborting (fed {fed:.0f}/{total:.0f}mm)")
            return False
        return True

    def prep_post_load(self, lane: AFCLane) -> None:
        """
        Stage filament to hub via ACE serial feed.

        Feeds dist_hub from the slot when load_to_hub is enabled and the slot
        is prepped but not yet staged, marking the lane loaded_to_hub and
        optionally capturing TD-1 data. Retries the feed up to three times.

        :param lane: Lane to stage to the hub.
        """
        if self._unit_load_to_hub is not None:
            load_to_hub = self._unit_load_to_hub
        else:
            load_to_hub = lane.load_to_hub
        if (not load_to_hub
            or lane.loaded_to_hub):
            return
        if not lane.prep_state:
            return
        slot = self._get_slot(lane.name)
        if (not self._ace
            or not self._ace.connected):
            return

        # Do not short-circuit when dist_hub <= 0: the stage-read listener's
        # initial load is what pulls the filament in, and dist_hub adds to it.
        dist_hub = lane.dist_hub

        # Guard the whole staging feed, which yields to the reactor, against
        # heartbeat re-staging or runouts.
        with self._operation():
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    # Wait out the unit's own insert cycle, or it rejects the
                    # staging feed with error_2.
                    self._wait_for_ace_ready(timeout=self.prep_ready_timeout)
                    if self._feed_to_hub_probing(lane, slot, dist_hub) is False:
                        # Spool pulled back out mid-stage, don't mark it staged
                        # and don't retry; reset prep so a fresh insert stages clean.
                        lane.loaded_to_hub = False
                        self._set_hub_state(lane, False)
                        lane.prep_state = False
                        self.afc.save_vars()
                        return
                    lane.loaded_to_hub = True
                    self._set_hub_state(lane, True)
                    self.afc.save_vars()
                    self.logger.info(f"ACE prep_post_load: {lane.name} staged at hub "
                                     f"(dist_hub={dist_hub:.0f}mm)")
                    # Capture TD-1 data now that filament is at hub
                    if (lane.td1_when_loaded
                        and lane.td1_device_id
                        and self.afc.td1_present):
                        self.logger.info(f"ACE prep_post_load: capturing TD-1 data for {lane.name}")
                        self.capture_td1_data(lane)
                    return
                except Exception as e:
                    if attempt < max_attempts - 1:
                        wait = 5.0 * (attempt + 1)
                        self.logger.warning(
                            f"ACE prep_post_load: attempt {attempt + 1}/{max_attempts} "
                            f"failed, retrying in {wait:.0f}s: {e}")
                        self.afc.reactor.pause(self.afc.reactor.monotonic() + wait)
                        continue
                    self.logger.error(f"ACE prep_post_load failed for {lane.name} after "
                                      f"{max_attempts} attempts: {e}")

    def eject_lane(self, lane: AFCLane) -> None:
        """
        Retract filament back into the ACE unit.

        From the hub-staged state the filament only spans the lane->hub gap, so
        unwind dist_hub + eject_buffer (config, default 475mm) to clear the hub and pull
        the filament back into the unit. If it's loaded past the hub (to the
        toolhead) fall back to the full unload length (dist_hub + bowden).

        :param lane: Lane to eject/retract back into the unit.
        """
        try:
            self._eject_lane_inner(lane)
        finally:
            # prepare_unload set _operation_active for an unload sequence that an
            # eject never runs, so clear it here or slot sync stays stopped.
            self._operation_active = False
            self._prev_states_stale = True

    def _eject_lane_inner(self, lane: AFCLane) -> None:
        """
        eject_lane's body; see eject_lane.

        :param lane: Lane to eject/retract back into the unit.
        """
        slot = self._get_slot(lane.name)
        if (self._ace
            and self._ace.connected):
            # Guard the retract: it stops assist then unwinds for seconds; without
            # this the watchdog re-enables assist on the still-loaded lane and
            # feeds forward against the retract.
            with self._operation():
                try:
                    self._stop_feed_assist(slot)
                    self._wait_for_ace_ready()
                    dist = self._get_eject_length(lane)
                    self.logger.info(f"ACE eject {lane.name}: unwinding {dist:.0f}mm "
                                     f"(dist_hub={lane.dist_hub:.0f}mm)")
                    self._ace.unwind_filament(slot, dist, self.retract_speed)
                    self._wait_for_feed_complete(slot, dist, self.retract_speed)
                    # The filament is back past the hub, so clear loaded_to_hub or
                    # the next load lands ~dist_hub short of the toolhead.
                    lane.loaded_to_hub = False
                    self._set_hub_state(lane, False)
                    # Suppress the heartbeat's auto reload-to-hub until the spool
                    # is removed or explicitly loaded.
                    self._hub_load_suppressed.add(lane.name)
                except Exception as e:
                    self.logger.error(f"ACE eject failed for {lane.name}: {e}")

    def lane_move(self, cur_lane: AFCLane, distance: float, speed_mode: Any) -> None:
        """
        Move filament via ACE serial instead of stepper.

        :param cur_lane: Lane to move.
        :param distance: Signed distance in mm; positive feeds, negative unwinds.
        :param speed_mode: AFC speed mode (unused; ACE uses its own feed/retract
            speeds).
        """
        slot = self._get_slot(cur_lane.name)
        if (not self._ace
            or not self._ace.connected):
            self.logger.error("ACE not connected for lane_move")
            return
        # Guard the move so a heartbeat poll can't re-stage the lane or re-enable
        # assist mid-move.
        with self._operation():
            try:
                self._wait_for_ace_ready()
                if distance > 0:
                    self._ace.feed_filament(slot, abs(distance), self.feed_speed)
                else:
                    self._ace.unwind_filament(slot, abs(distance), self.retract_speed)
                self._wait_for_feed_complete(slot, abs(distance), self.feed_speed if distance > 0
                                             else self.retract_speed)
            except Exception as e:
                self.logger.error(f"ACE lane_move failed: {e}")

    def lane_unload(self, cur_lane: AFCLane) -> bool:
        """
        Custom lane unload, retract via ACE serial.

        :param cur_lane: Lane to unload.
        :return bool: Always True.
        """
        slot = self._get_slot(cur_lane.name)
        if (self._ace
            and self._ace.connected):
            # Guard the retract (see eject_lane): stop assist + unwind for seconds
            # without the watchdog re-enabling assist against the move.
            with self._operation():
                try:
                    self._stop_feed_assist(slot)
                    self._wait_for_ace_ready()
                    dist = self._get_eject_length(cur_lane)
                    self.logger.info(f"ACE unload {cur_lane.name}: unwinding {dist:.0f}mm "
                                     f"(dist_hub={cur_lane.dist_hub:.0f}mm, "
                                     f"tool_loaded={cur_lane.tool_loaded})")
                    self._ace.unwind_filament(slot, dist, self.retract_speed)
                    self._wait_for_feed_complete(slot, dist, self.retract_speed)
                    # Pulled back into the unit (past the hub): clear loaded_to_hub
                    # so the next load feeds the full dist_hub + bowden path.
                    cur_lane.loaded_to_hub = False
                    self._set_hub_state(cur_lane, False)
                except Exception as e:
                    self.logger.error(f"ACE lane_unload failed for {cur_lane.name}: {e}")
        return True

    def get_lane_reset_command(self, lane: AFCLane, dis: float) -> str:
        """
        Return the gcode command used to reset/retract a lane.

        :param lane: Lane to reset.
        :param dis: Reset distance (unused; the AFC_ACE_LANE_RESET handler computes
            the retract distance itself).
        :return str: The AFC_ACE_LANE_RESET command for this unit and lane.
        """
        return f"AFC_ACE_LANE_RESET UNIT={self.name} LANE={lane.name}"

    def _wait_first_connect(self) -> None:
        """
        Let the first connect attempt after ready finish before PREP reads
        the unit. Its autodetect runs on a worker thread (run_off_reactor), so
        the reactor reaches PREP while it is still probing, and every lane
        would read ACE NOT CONNECTED. Bounded by the autodetect's own cap; a unit
        that is really missing still fails PREP after that first attempt, as
        before.
        """
        reactor = self.afc.reactor
        deadline = reactor.monotonic() + AUTODETECT_WAIT_MAX + 5.0
        said = False
        while (getattr(self, "_first_connect_pending", False)
               and reactor.monotonic() < deadline):
            if not said:
                said = True
                self.logger.debug(f"ACE {self.name}: PREP waiting for the serial connect")
            reactor.pause(reactor.monotonic() + 0.1)

    def system_Test(self, cur_lane: AFCLane, delay: float, assignTcmd: bool,
                     enable_movement: bool) -> bool:
        """
        ACE system test, query hardware slot status instead of physical switches.

        Drives a lane's prep step: reads the ACE slot status, sets prep/load
        and tooled state accordingly, applies RFID defaults, optionally starts
        feed assist, and reports a status message.

        :param cur_lane: Lane being prep-tested.
        :param delay: Unused (kept for interface compatibility).
        :param assignTcmd: When True, assign the lane's tool (Tn) command.
        :param enable_movement: Unused (kept for interface compatibility).
        :return bool: True if the lane passed the prep checks, else False.
        """
        msg = ''
        succeeded = True

        cur_lane.unsync_to_extruder(False)
        self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.7)
        self._wait_first_connect()

        # ACE has no physical prep/load switches, determine state from hardware
        if (self._ace is not None
            and self._ace.connected):
            slot = self._get_slot(cur_lane.name)
            try:
                hw_status = self._ace.get_status(timeout=2.0)
                if isinstance(hw_status, dict):
                    slots = hw_status.get("slots", [])
                    if (slot < len(slots)
                        and isinstance(slots[slot], dict)):
                        slot_ready = slots[slot].get("status", "") == "ready"
                        cur_lane.prep_state = slot_ready
                        if not slot_ready:
                            cur_lane.loaded_to_hub = False
                            self._set_hub_state(cur_lane, False)
            except Exception as e:
                self.logger.debug(f"ACE get_status failed during PREP: {e}")
        else:
            msg = '<span class=error--text>ACE NOT CONNECTED</span>'
            succeeded = False

        if succeeded:
            if not cur_lane.prep_state:
                if not cur_lane.raw_load_state:
                    self.lane_not_ready(cur_lane)
                    msg += '<span class=success--text>EMPTY READY FOR SPOOL</span>'
                else:
                    self.lane_fault(cur_lane)
                    msg += '<span class=error--text>CHECK FILAMENT Prep: False - Load: True</span>'
                    succeeded = False
            else:
                self.lane_loaded(cur_lane)
                msg += "<span class=success--text>LOCKED</span>"
                # The slot "ready" status is the ACE's load indicator, so don't
                # gate LOADED on raw_load_state (the virtual hub signal).
                cur_lane.status = AFCLaneState.LOADED
                msg += "<span class=success--text> AND LOADED</span>"
                self.lane_illuminate_spool(cur_lane)

                # Apply RFID data if available and not already set
                if apply_filament_defaults is not None:
                    slot_info = self._slot_inventory[slot] if slot < self.SLOTS_PER_UNIT else {}
                    apply_filament_defaults(
                        cur_lane, slot_info,
                        color_converter=rgb_array_to_hex,
                        afc_defaults={
                            "default_material_type": self.afc.default_material_type,
                            "default_color": getattr(self.afc, "default_color", None),
                        })

                # Assume filament staged at hub on startup
                if (not cur_lane.tool_loaded
                    and not cur_lane.loaded_to_hub):
                    if cur_lane.load_to_hub:
                        cur_lane.loaded_to_hub = True
                        self._set_hub_state(cur_lane, True)

                if (cur_lane.tool_loaded
                    and cur_lane.extruder_obj.lane_loaded == cur_lane.name):
                    cur_lane.loaded_to_hub = True
                    self._set_hub_state(cur_lane, True)
                    cur_lane.sync_to_extruder()
                    msg += cur_lane.extruder_obj.prep_on_shuttle_check(cur_lane)

                    if self.afc.current == cur_lane.name:
                        self.afc.spool.set_active_spool(cur_lane.spool_id)
                        self.lane_tool_loaded(cur_lane)
                        cur_lane.status = AFCLaneState.TOOLED
                        self.printer.send_event("afc:tool_loaded", cur_lane)
                    else:
                        self.lane_tool_loaded_idle(cur_lane)
                    cur_lane.enable_buffer()

                    # Start assist for any lane confirmed in the toolhead
                    # (afc.current may be None on toolchangers during prep);
                    # explicit=True clears any stale manual suppression.
                    if self._use_feed_assist(cur_lane):
                        self._start_feed_assist(slot, explicit=True)

        if assignTcmd:
            self.afc.function.TcmdAssign(cur_lane)
        cur_lane.send_lane_data()
        cur_lane.do_enable(False)
        # lane.map is a list, so render it with map_to_string().
        self.logger.info(f'{cur_lane.name} tool cmd: {cur_lane.map_to_string():3} {msg}')
        cur_lane.set_afc_prep_done()
        return succeeded

    def _feed_until_sensor(self, slot: int, cur_lane: AFCLane, max_distance: float,
                           step_size: Optional[float] = None) -> Tuple[float, bool]:
        """
        Feed filament in steps until toolhead sensor triggers.

        Returns (distance_fed, sensor_triggered).

        :param slot: 0-based ACE slot index to feed.
        :param cur_lane: Lane whose toolhead sensor is polled after each step.
        :param max_distance: Maximum total distance in mm to feed.
        :param step_size: Per-step feed distance in mm (defaults to
            calibration_step).
        :return tuple: (total_fed, sensor_triggered).
        """
        # Callers already check self._ace is connected before reaching here.
        if self._ace is None:
            return 0.0, False
        if step_size is None:
            step_size = self.calibration_step
        total_fed = 0.0

        while total_fed < max_distance:
            step = min(step_size, max_distance - total_fed)
            self._wait_for_ace_ready()
            try:
                self._ace.feed_filament(slot, step, self.feed_speed)
            except Exception as e:
                self.logger.warning(
                    f"ACE calibration: feed failed at {total_fed:.0f}mm, retrying: {e}")
                self._wait_for_ace_ready(timeout=15.0)
                self._ace.feed_filament(slot, step, self.feed_speed)
            self._wait_for_feed_complete(slot, step, self.feed_speed)
            total_fed += step

            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.3)
            if cur_lane.get_toolhead_pre_sensor_state():
                self.logger.info(f"ACE calibration: sensor triggered at {total_fed:.1f}mm")
                return total_fed, True

        return total_fed, False

    cmd_AFC_ACE_BUFFER_STATE_help = "Report each lane's ACE buffer sensors (buf_rst/buf_back)"
    def cmd_AFC_ACE_BUFFER_STATE(self, gcmd: GCodeCommand) -> None:
        """
        Report each lane's ACE buffer sensors (buf_rst / buf_back) and
        insert/empty, plus the shared buffer-feed sensor, decoded from
        GET_SENSOR_STATE. Read-only. ACE2 only.

        Usage
        -------
        `AFC_ACE_BUFFER_STATE UNIT=<unit>`

        Example
        -------
        ```
        AFC_ACE_BUFFER_STATE UNIT=Ace2_1
        ```
        """
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        try:
            state = self._ace.get_sensor_state()
        except Exception as e:
            gcmd.respond_info(f"AFC_ACE_BUFFER_STATE: {e}")
            return
        slot_sensors = (state.get('slot_sensors') if isinstance(state, dict) else None)
        if not slot_sensors:
            gcmd.respond_info("AFC_ACE_BUFFER_STATE: no per-slot sensor data (firmware without "
                              "GET_SENSOR_STATE, or unexpected reply).")
            return
        slot_to_lane = {v: k for k, v in self._slot_map.items()}
        lines = ["ACE buffer/sensor state (slot: insert empty | buf_rst buf_back):"]
        for slot, s in enumerate(slot_sensors):
            lane = slot_to_lane.get(slot, '-')
            lines.append((
                f"  slot {slot} ({lane}): insert={int(s['insert'])} empty={int(s['empty'])} | "
                f"buf_rst={int(s['buf_rst'])} buf_back={int(s['buf_back'])}"))
        lines.append(f"  shared buf_feed={int(state.get('buf_feed', False))}")
        gcmd.respond_info("\n".join(lines))

    def calibrate_bowden(self, cur_lane: AFCLane, dis: float,
                         tol: float) -> Tuple[bool, str, float]:
        """
        Calibrate afc_bowden_length (hub→toolhead) by feeding until
        the toolhead sensor triggers.

        Wraps _calibrate_bowden_inner with the _operation_active guard.

        :param cur_lane: Lane to calibrate.
        :param dis: Step size in mm (passed by the caller; the inner method uses
            calibration_step).
        :param tol: Tolerance (unused).
        :return tuple: (success, message, measured_distance).
        """
        slot = self._get_slot(cur_lane.name)
        if (not self._ace
            or not self._ace.connected):
            return False, "ACE not connected", 0

        cur_hub = cur_lane.hub_obj
        if cur_hub is None:
            return False, "Lane has no hub configured", 0

        self._operation_active = True
        try:
            return self._calibrate_bowden_inner(cur_lane, cur_hub, slot)
        finally:
            self._operation_active = False
            self._prev_states_stale = True

    def _calibrate_bowden_inner(self, cur_lane: AFCLane, cur_hub: Any,
                                slot: int) -> Tuple[bool, str, float]:
        """
        Feed in calibration_step increments until the toolhead sensor trips,
        record that distance as afc_bowden_length (and afc_unload_bowden_length),
        retract, and rewrite the values to config. Implementation of
        calibrate_bowden.

        :param cur_lane: Lane to calibrate.
        :param cur_hub: Hub object the measured bowden length is saved to.
        :param slot: 0-based ACE slot index for the lane.
        :return tuple: (success, message, measured_distance).
        """
        # calibrate_bowden (the only caller) already checked self._ace is
        # connected before calling in.
        if self._ace is None:
            return False, "ACE not connected", 0.0
        cur_lane.extruder_obj.clear_stale_sensor_state(cur_lane)

        if cur_lane.get_toolhead_pre_sensor_state():
            return False, "Toolhead sensor already triggered, unload first", 0

        old_bowden = getattr(cur_hub, 'afc_bowden_length', 0)
        max_distance = 6000

        self.logger.raw(f'Calibrating afc_bowden_length for {cur_lane.name} '
                        f'(max {max_distance}mm in {self.calibration_step}mm steps)')

        # Coarse pass
        coarse_distance, triggered = self._feed_until_sensor(slot, cur_lane, max_distance)

        if not triggered:
            retract_dist = coarse_distance
            self.logger.info(f"ACE calibrate: retracting {retract_dist:.0f}mm")
            try:
                self._wait_for_ace_ready()
                self._ace.unwind_filament(slot, retract_dist, self.retract_speed)
                self._wait_for_feed_complete(slot, retract_dist, self.retract_speed)
                self._wait_for_ace_ready(timeout=15.0)
            except Exception as e:
                self.logger.error(f"ACE calibrate: retract failed: {e}")
            cur_lane.extruder_obj.clear_stale_sensor_state(cur_lane)
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 2.0)
            return False, (f"Toolhead sensor did not trigger after {coarse_distance:.0f}mm. "
                           "Check filament path and sensor wiring."), coarse_distance

        # Use the first trigger distance directly (no fine re-feed pass).
        distance = coarse_distance
        self.logger.info(f"ACE calibrate: trigger at {distance:.1f}mm")

        # Retract
        retract_dist = distance + 5
        self.logger.info(f"ACE calibrate: retracting {retract_dist:.0f}mm")
        try:
            self._wait_for_ace_ready()
            self._ace.unwind_filament(slot, retract_dist, self.retract_speed)
            self._wait_for_feed_complete(slot, retract_dist, self.retract_speed)
            self._wait_for_ace_ready(timeout=15.0)
        except Exception as e:
            self.logger.error(f"ACE calibrate: retract failed: {e}")

        # Clear sensor state after retract
        cur_lane.extruder_obj.clear_stale_sensor_state(cur_lane)
        self.afc.reactor.pause(self.afc.reactor.monotonic() + 2.0)

        # Add 100mm extra to account for movement not being as accurate
        unload_distance = distance + 100
        # Account for filament already at hub
        if cur_lane.loaded_to_hub:
            dist_hub = cur_lane.dist_hub
            if dist_hub > 0:
                self.logger.info(f"ACE calibrate: filament at hub, adding dist_hub="
                                 f"{dist_hub:.0f}mm to measured {distance:.1f}mm")
                distance += dist_hub

        bowden_dist = round(distance, 2)
        cal_msg = f'\n afc_bowden_length: New: {bowden_dist} Old: {old_bowden}'
        cur_hub.afc_bowden_length = bowden_dist
        cur_hub.afc_unload_bowden_length = unload_distance
        self.afc.function.ConfigRewrite(cur_hub.fullname, "afc_bowden_length", bowden_dist, cal_msg)
        self.afc.function.ConfigRewrite(cur_hub.fullname, "afc_unload_bowden_length", unload_distance,
                                        f'\n afc_unload_bowden_length: {unload_distance}')

        return (True, f"afc_bowden_length calibration: {bowden_dist}mm (was {old_bowden}mm)",
                bowden_dist)

    def calibrate_lane(self, cur_lane: AFCLane, tol: float) -> Tuple[bool, str, float]:
        """
        Calibrate dist_hub from ACE slot to hub sensor.

        Wraps _calibrate_hub_inner with the _operation_active guard.

        :param cur_lane: Lane to calibrate.
        :param tol: Tolerance (unused).
        :return tuple: (success, message, measured_distance).
        """
        self._operation_active = True
        try:
            return self._calibrate_hub_inner(cur_lane)
        finally:
            self._operation_active = False
            self._prev_states_stale = True

    # ── Custom load/unload gcode handlers ──
    def _log_delta(self, msg: str, debug: bool = True) -> None:
        """
        Call AFC's delta-time logger defensively.

        afcDeltaTime.log_with_time raises when called before set_start_time(),
        which an ACE load/unload outside a normal TOOL_LOAD can do, so start the
        clock first and never let a timing-log failure break the operation.

        :param msg: message to log
        :param debug: forwarded to log_with_time
        """
        dt = self.afc.afcDeltaTime
        if dt is None:
            return
        try:
            if dt.start_time is None:
                dt.set_start_time()
            dt.log_with_time(msg, debug=debug)
        except Exception:
            pass

    def unit_load_lane(self, cur_lane: AFCLane, cur_extruder: Any) -> bool:
        """
        Full toolhead load for a stepperless ACE lane (AFC.load_sequence's
        unit_load_lane hook). Runs the ACE serial transport directly via
        _ace_load_sequence (which verifies the toolhead sensor and returns False
        on failure), then finalizes the lane state.

        :param cur_lane: Lane to load.
        :param cur_extruder: Extruder the lane loads into.
        :return bool: True on a verified load, False on failure.
        """
        afc = self.afc
        if not self._ace_load_sequence(cur_lane, cur_extruder):
            return False
        cur_lane.status = AFCLaneState.TOOL_LOADED
        afc.save_vars()
        return True

    def unit_unload_lane(self, cur_lane: AFCLane, cur_extruder: Any) -> bool:
        """
        Full toolhead unload for a stepperless ACE lane (AFC.unload_sequence's
        unit_unload_lane hook). Runs the shared toolhead phase (quick-pull,
        buffer/sync/select, cut/tip-form), the ACE serial unwind directly via
        _ace_unload_sequence (returns False on failure), the post-unload macro,
        then finalizes the lane state.

        :param cur_lane: Lane to unload.
        :param cur_extruder: Extruder the lane is synced to on entry.
        :return bool: True on success, False on failure.
        """
        afc = self.afc
        cur_lane.status = AFCLaneState.TOOL_UNLOADING
        # prepare_unload set _operation_active; cut/tip-form runs before the
        # unwind's own finally, so clear it in a finally spanning the whole unload.
        try:
            # Shared toolhead phase. do_tool_cut_tip_form self-gates on
            # tool_cut/form_tip, so it's a no-op when both are disabled.
            afc.move_e_pos(-2, cur_extruder.tool_unload_speed, "Quick Pull", wait_tool=False)
            cur_lane.disable_buffer()
            cur_lane.sync_to_extruder()
            cur_lane.select_lane()
            afc.do_tool_cut_tip_form(cur_lane, cur_extruder)
            # ACE serial unwind (direct call; returns False on failure).
            if not self._ace_unload_sequence(cur_lane, cur_extruder):
                return False
            if afc.post_unload_macro is not None:
                self.gcode.run_script_from_command(afc.post_unload_macro)
            cur_lane.set_tool_unloaded(normal_toolchange=True)
            cur_lane.status = AFCLaneState.NONE
            # Refresh the virtual hub now that tool_loaded is cleared, or a
            # lane->lane toolchange can bail "Hub not clear" before the next poll.
            self._set_hub_state(cur_lane, False)
            afc.save_vars()
            return True
        finally:
            self._operation_active = False
            self._prev_states_stale = True

    def _ace_load_sequence(self, cur_lane: AFCLane, cur_extruder: Any) -> bool:
        """
        ACE load transport: serial feed to toolhead area + feed assist.

        Wraps _ace_load_inner with the _operation_active guard.

        :param cur_lane: Lane to load.
        :param cur_extruder: Extruder the lane loads into.
        :return bool: True on a successful load transport.
        """
        self._operation_active = True
        try:
            return self._ace_load_inner(cur_lane, cur_extruder)
        finally:
            self._operation_active = False
            self._prev_states_stale = True

    def _ace_feed_attempt(self, slot: int, dist: float, speed: float,
                          cur_lane: AFCLane) -> Tuple[bool, bool]:
        """
        Run one feed_filament + wait_for_feed_complete attempt for
        _ace_load_inner.

        :param slot: 0-based ACE slot index.
        :param dist: Feed distance in mm for this attempt.
        :param speed: Feed speed in mm/s for this attempt.
        :param cur_lane: Lane being loaded.
        :return tuple: (errored, success) - errored is True when the error
            was a feed_error (stall).
        """
        if self._ace is None:
            return False, False
        self._wait_for_ace_ready()
        self._ace.feed_filament(slot, dist, speed)
        success = self._wait_for_feed_complete(slot, dist, speed, cur_lane)
        # The unit's encoder saw the filament stop moving; the retries in
        # _ace_load_inner still run, but a failure is then reported as a
        # stall, not a bowden length problem.
        errored = self._last_move_error == "feed_error"
        return errored, success

    def _ace_load_inner(self, cur_lane: AFCLane, cur_extruder: Any) -> bool:
        """
        ACE custom load, filament transport only.

        AFC's load_sequence handles the shared toolhead engagement
        (sync_to_extruder, tool_end, tool_stn, sensor verification,
        buffer ram).

        :param cur_lane: Lane to load.
        :param cur_extruder: Extruder the lane loads into.
        :return bool: True once filament reaches the toolhead sensor; False on
            any failure (handled via handle_lane_failure).
        """
        afc = self.afc
        slot = self._get_slot(cur_lane.name)

        if (self._ace is None
            or not self._ace.connected):
            afc.error.handle_lane_failure(cur_lane, f"ACE not connected ({self.serial_port})",
                                          pause=afc.function.in_print())
            return False

        # The ACE drives one slot at a time; a feed while another slot runs assist
        # is never acked, so stop assist on every other slot first (all modes).
        for other_slot in list(self._feed_assist_active):
            if other_slot != slot:
                self._stop_feed_assist(other_slot)

        # Calculate feed distance
        if cur_lane.loaded_to_hub:
            hub = cur_lane.hub_obj
            feed_dist = getattr(hub, 'afc_bowden_length', 0) if hub else 0
            feed_dist = max(feed_dist, 50.0)
        else:
            feed_dist = self._get_bowden_length(cur_lane)

        self._set_hub_state(cur_lane, True)
        self._hub_load_suppressed.discard(cur_lane.name)

        # Enable FPS advance latch so the toolhead sensor stays triggered
        # even when pressure drops briefly after ACE feed completes.
        buffer_obj = cur_lane.buffer_obj
        if (buffer_obj is not None
            and hasattr(buffer_obj, 'enable_advance_latch')):
            buffer_obj.enable_advance_latch()

        # Pre-feed check: if the toolhead sensor detects filament before
        # we start feeding, there's stuck filament from a previous load.
        # Don't push more filament into an occupied toolhead.
        if cur_lane.get_toolhead_pre_sensor_state():
            message = (f"Toolhead sensor detects filament before ACE feed for "
                       f"{cur_lane.name}.\nFilament may be stuck from a previous "
                       f"load, clear the toolhead before loading.\n"
                       f"To resolve, manually retract filament from the toolhead "
                       f"or run AFC_RESET for {cur_lane.name}.")
            if afc.function.in_print():
                message += "\nOnce cleared, click resume to continue printing"
            afc.error.handle_lane_failure(cur_lane, message, pause=afc.function.in_print())
            return False

        # ACE serial feed to toolhead area
        stalled = False
        approach = min(max(self.load_approach_length, 0.0), feed_dist)
        approach_speed = self.load_approach_speed
        fast = feed_dist - approach
        try:
            success = True
            # Most of the way at full speed, then the last stretch slowly, so
            # the tip meets the toolhead gently. Either feed stops at the
            # toolhead sensor.
            for step, (dist, speed) in enumerate(((fast, self.feed_speed),
                                                  (approach, approach_speed))):
                if (dist < 0.5
                    or (step
                        and cur_lane.get_toolhead_pre_sensor_state())):
                    continue
                # A long feed that trips the unit's slip check early (seen on the
                # first load after an unload) is resent for what it has left,
                # rather than leaving the rest to the short retry pulses.
                tries = 1 + (LOAD_FEED_ERROR_RESENDS if not step else 0)

                # The retriable attempts (all but the last): bail out early on
                # success/sensor-trigger, otherwise shrink dist to what's left
                # and resend. The final attempt is unconditional, below, so
                # this loop only ever exits by break, never by exhaustion.
                for _ in range(tries - 1):
                    errored, success = self._ace_feed_attempt(slot, dist, speed, cur_lane)
                    stalled = stalled or errored
                    if (not errored
                        or cur_lane.get_toolhead_pre_sensor_state()):
                        break
                    fed = self._last_feed_length(slot)
                    if (fed is not None
                        and 0 < fed < dist):
                        dist -= fed
                    if dist < 0.5:
                        break
                    self.logger.info(f"ACE load: {cur_lane.name} hit a feed error" +
                                     (f" after {fed:.0f}mm" if fed else "") +
                                     f", feeding the remaining {dist:.0f}mm again")
                else:
                    # Either tries == 1 (no retries configured) or every
                    # retriable attempt above errored out: this is the last
                    # chance, win or lose.
                    errored, success = self._ace_feed_attempt(slot, dist, speed, cur_lane)
                    stalled = stalled or errored
                # A failed fast feed goes to the full-length resends below;
                # the short approach would pass and hide it.
                if not success:
                    break

            if not success:
                if cur_lane.get_toolhead_pre_sensor_state():
                    self.logger.info("Feed error but sensor triggered, continuing")
                else:
                    success = self._smart_load_retry(cur_lane, slot, feed_dist)
                    stalled = stalled or self._last_move_error == "feed_error"
                    if not success:
                        afc.error.handle_lane_failure(cur_lane,
                            f"ACE feed failed for {cur_lane.name}" +
                            (self._stall_hint(cur_lane) if stalled else ""),
                            pause=afc.function.in_print())
                        return False
        except Exception as e:
            afc.error.handle_lane_failure(cur_lane, f"ACE load feed error: {e}",
                                          pause=afc.function.in_print())
            return False

        # If the sensor has not triggered yet, pulse load_retry_pulse mm until it
        # does or load_retry_timeout elapses.
        if not cur_lane.get_toolhead_pre_sensor_state():
            pulse = self.load_retry_pulse
            deadline = self.afc.reactor.monotonic() + self.load_retry_timeout
            pulse_num = 0
            reached = False
            while self.afc.reactor.monotonic() < deadline:
                pulse_num += 1
                self.logger.info(f"Sensor not triggered after feed for {cur_lane.name}, "
                                 f"retry pulse {pulse_num} ({pulse:.0f}mm)")
                try:
                    self._wait_for_ace_ready()
                    self._ace.feed_filament(slot, pulse, approach_speed)
                    self._wait_for_feed_complete(slot, pulse, approach_speed, cur_lane)
                    stalled = stalled or self._last_move_error == "feed_error"
                except Exception:
                    pass
                self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.3)
                if cur_lane.get_toolhead_pre_sensor_state():
                    self.logger.info(
                        f"Sensor triggered on retry pulse {pulse_num} for {cur_lane.name}")
                    reached = True
                    break
            if not reached:
                afc.error.handle_lane_failure(cur_lane,
                    f"Filament did not reach toolhead sensor after feed + "
                    f"{pulse_num} retry pulses "
                    f"({self.load_retry_timeout:.0f}s timeout) for {cur_lane.name}."
                    + (self._stall_hint(cur_lane) if stalled
                    else "\nCheck filament path and bowden length calibration."),
                    pause=afc.function.in_print())
                return False

        # Set loaded_to_hub AFTER successful feed
        cur_lane.loaded_to_hub = True
        self._set_hub_state(cur_lane, True)

        # Enable feed assist. A completed load is a deliberate operation, so it
        # clears any stale manual suppression (explicit=True).
        if self._use_feed_assist(cur_lane):
            self._start_feed_assist(slot, explicit=True)

        # Advance from the toolhead sensor to the nozzle, mirroring AFC core's
        # tool_stn move that the unit_load_lane branch never reaches.
        tool_stn = cur_extruder.tool_stn or 0
        if tool_stn > 0:
            self.logger.info(
                f"ACE load: advancing tool_stn {tool_stn:.0f}mm to nozzle for {cur_lane.name}")
            cur_lane.activate_toolhead_extruder()
            afc.move_e_pos(tool_stn, cur_extruder.tool_load_speed, "tool stn")
            self._log_delta("ACE load: tool_stn to nozzle")

        self._log_delta("ACE load transport complete")
        return True

    def lane_unloading(self, lane: AFCLane) -> None:
        """
        Unload-start hook the upstream core actually calls.

        Upstream's unload sequence invokes ``lane_unloading`` (for LEDs) but
        never ``prepare_unload``, so we trigger the ACE feed-assist stop here as
        well. Keeps the watchdog from fighting the retract on the upstream core.

        :param lane: Lane that is starting to unload.
        """
        super().lane_unloading(lane)
        try:
            self.prepare_unload(lane, lane.hub_obj, lane.extruder_obj)
        except Exception as e:
            self.logger.warning(
                f"ACE: lane_unloading assist-stop error for {lane.name}: {e}")

    def prepare_unload(self, cur_lane: AFCLane, cur_hub: Any, cur_extruder: Any) -> None:
        """
        Stop ACE feed assist during filament tip forming.

        Assist should stop before cut/park/tip since it only conflicts with
        the serial unwind. This prevents inadvertent assist state on resume
        if an error during tip-forming pauses the print.

        :param cur_lane: Lane being unloaded; its slot's feed assist is stopped.
        :param cur_hub: Hub object (unused here).
        :param cur_extruder: Extruder object (unused here).
        """
        # Suppress the assist watchdog for the whole unload, including quick-pull
        # and tip-forming, or it re-enables assist against the retract.
        self._operation_active = True
        slot = self._get_slot(cur_lane.name)
        self._stop_feed_assist(slot)

    def _ace_unload_sequence(self, cur_lane: AFCLane, cur_extruder: Any) -> bool:
        """
        ACE unload transport, ACE serial unwind back to hub.

        Wraps _ace_unload_inner with the _operation_active guard.

        :param cur_lane: Lane to unload.
        :param cur_extruder: Extruder the lane is synced to.
        :return bool: True on a successful unload transport.
        """
        self._operation_active = True
        try:
            return self._ace_unload_inner(cur_lane, cur_extruder)
        finally:
            self._operation_active = False
            self._prev_states_stale = True

    def _ace_unload_inner(self, cur_lane: AFCLane, cur_extruder: Any) -> bool:
        """
        ACE custom unload, filament transport only.

        AFC's unload_sequence handles the shared toolhead operations
        (LED, heat, quick pull, buffer disable, sync, cut/park/tip).
        The lane is still synced to the extruder on entry so we can
        do extruder moves.

        :param cur_lane: Lane to unload.
        :param cur_extruder: Extruder the lane is synced to; used for the
            tool_stn_unload retract before the serial unwind.
        :return bool: True once filament is staged near the hub; False on
            failure (handled via handle_lane_failure).
        """
        afc = self.afc
        slot = self._get_slot(cur_lane.name)

        if (not self._ace
            or not self._ace.connected):
            afc.error.handle_lane_failure(cur_lane, f"ACE not connected ({self.serial_port})",
                                          pause=afc.function.in_print())
            return False

        cur_lane.unsync_to_extruder()

        # ACE serial unwind, retract to hub staging point
        hub = cur_lane.hub_obj
        bowden = (getattr(hub, 'afc_unload_bowden_length', getattr(hub, 'afc_bowden_length', 0))
                  if hub else 0)
        # Retract the hub->toolhead distance to leave the tip staged at the hub,
        # symmetric with the reload feed; do not subtract dist_hub.
        retract_dist = bowden
        try:
            self._wait_for_ace_ready()
            # Clear the tip from the gears in two stages: a blocking half
            # tool_stn_unload into the slack freed by stopping assist, then a full
            # one issued async so it overlaps the ACE unwind. Re-assert the assist
            # stop first in case the watchdog re-armed it.
            self._stop_feed_assist(slot)
            if cur_extruder.tool_stn_unload > 0:
                self._log_delta('ACE unload: retracting tool_stn_unload/2 (pre-unwind)')
                afc.move_e_pos(cur_extruder.tool_stn_unload * -0.5, cur_extruder.tool_unload_speed,
                               "ACE STN unload", wait_tool=True)
                self._log_delta('ACE unload: retracting tool_stn_unload (overlaps unwind)')
                afc.move_e_pos(cur_extruder.tool_stn_unload * -1, cur_extruder.tool_unload_speed,
                               "ACE STN unload", wait_tool=False)
            self._ace.unwind_filament(slot, retract_dist, self.retract_speed)
            self._wait_for_feed_complete(slot, retract_dist, self.retract_speed)
            if cur_extruder.tool_stn_unload > 0:
                afc.function.log_toolhead_pos("ACE STN unload after ")
        except Exception as e:
            afc.error.handle_lane_failure(cur_lane, f"ACE unwind failed for {cur_lane.name}: {e}",
                                          pause=afc.function.in_print())
            return False

        self._log_delta("ACE unwind complete")

        # Filament staged near hub, ready for fast reload.
        self._set_hub_state(cur_lane, False)
        cur_lane.loaded_to_hub = True
        self.lane_tool_unloaded(cur_lane)
        self._hub_load_suppressed.add(cur_lane.name)

        return True

    # ── Calibration commands ──
    cmd_AFC_ACE_CALIBRATE_help = "Calibrate ACE feed distance to toolhead"
    cmd_AFC_ACE_CALIBRATE_options = {
        "UNIT": {"type": "string", "default": ""},
        "LANE": {"type": "string", "default": ""},
    }

    def cmd_AFC_ACE_CALIBRATE(self, gcmd: GCodeCommand) -> None:
        """
        Calibrate ACE feed distance to toolhead for a lane.

        Usage
        -------
        `AFC_ACE_CALIBRATE UNIT=<unit> LANE=<lane>`

        Example
        -------
        ```
        AFC_ACE_CALIBRATE UNIT=Ace_1 LANE=lane1
        ```
        """
        lane_name = gcmd.get('LANE', '')
        if (not lane_name
            or lane_name not in self.afc.lanes):
            gcmd.respond_info("Usage: AFC_ACE_CALIBRATE LANE=<lane_name>")
            return
        cur_lane = self.afc.lanes[lane_name]
        success, msg, dist = self.calibrate_bowden(cur_lane, self.calibration_step, 1.0)
        gcmd.respond_info(msg)

    cmd_AFC_ACE_CALIBRATE_HUB_help = "Calibrate ACE feed distance to hub"
    cmd_AFC_ACE_CALIBRATE_HUB_options = {
        "UNIT": {"type": "string", "default": ""},
        "LANE": {"type": "string", "default": ""},
    }
    def cmd_AFC_ACE_CALIBRATE_HUB(self, gcmd: GCodeCommand) -> None:
        """
        Calibrate ACE feed distance to hub sensor. Requires a physical
        (non-virtual) hub sensor.

        Usage
        -------
        `AFC_ACE_CALIBRATE_HUB UNIT=<unit> LANE=<lane>`

        Example
        -------
        ```
        AFC_ACE_CALIBRATE_HUB UNIT=Ace_1 LANE=lane1
        ```
        """
        lane_name = gcmd.get('LANE', '')
        if (not lane_name
            or lane_name not in self.afc.lanes):
            gcmd.respond_info("Usage: AFC_ACE_CALIBRATE_HUB LANE=<lane_name>")
            return
        cur_lane = self.afc.lanes[lane_name]
        if (cur_lane.hub_obj
            and cur_lane.hub_obj.is_virtual_pin()):
            gcmd.respond_info("Hub calibration requires a physical hub sensor, not virtual")
            return
        success, msg, dist = self.calibrate_lane(cur_lane, 1.0)
        gcmd.respond_info(msg)

    cmd_AFC_ACE_STATUS_help = "Query ACE hardware status"
    def cmd_AFC_ACE_STATUS(self, gcmd: GCodeCommand) -> None:
        """
        Query and display ACE hardware status: the derived current action and
        the raw status dict.

        Usage
        -------
        `AFC_ACE_STATUS UNIT=<unit>`

        Example
        -------
        ```
        AFC_ACE_STATUS UNIT=Ace_1
        ```
        """
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        try:
            status = self._ace.get_status()
            action = self._derive_action(status) or "idle"
            gcmd.respond_info(f"ACE action: {action}\nACE Status: {status}")
        except Exception as e:
            gcmd.respond_info(f"Error querying ACE: {e}")

    cmd_AFC_ACE_FEED_INFO_help = (
        "Per-slot feed diagnostics (steps/length/encoder) for feed-check tuning")
    def cmd_AFC_ACE_FEED_INFO(self, gcmd: GCodeCommand) -> None:
        """
        Query per-slot feed diagnostics (steps / commanded length / encoder
        reading) for tuning feed_check_length / feed_error_length. The ACE2
        encoder is expected to read ~1.2342x the commanded length; an encoder
        reading well below that ratio indicates slip (lower feed_check_length to
        widen tolerance). ACE2 only.

        Usage
        -------
        `AFC_ACE_FEED_INFO UNIT=<unit>`

        Example
        -------
        ```
        AFC_ACE_FEED_INFO UNIT=Ace2_1
        ```
        """
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        try:
            result = self._ace.send_command('get_feed_info')
        except Exception as e:
            gcmd.respond_info(f"Error querying ACE feed info: {e}")
            return
        feeds = (result or {}).get('feed_info') or []
        if not feeds:
            gcmd.respond_info((
                f"ACE feed info: no data (unsupported on this firmware, or nothing fed yet). "
                f"raw_fields={(result or {}).get('raw_fields')}"))
            return
        lines = ["ACE feed info (slot: steps / length_mm / encoder_mm / ratio):"]
        for i, fi in enumerate(feeds):
            length = fi.get('length', 0)
            decoder = fi.get('decoder', 0)
            ratio = (decoder / length) if length else 0.0
            lines.append(f"  slot {i}: {fi.get('steps', 0)} / {length} / {decoder} / {ratio:.3f}")
        gcmd.respond_info("\n".join(lines))

    cmd_AFC_ACE_RFID_DUMP_help = "Dump full raw RFID/filament-info read for a slot"
    def cmd_AFC_ACE_RFID_DUMP(self, gcmd: GCodeCommand) -> None:
        """
        Dump the full raw RFID/filament-info read for a slot, every protobuf
        field, not just the sku/type/color we normally use. The ACE 2 carries
        more than the ACE Pro: e.g. diameter (field 8, 0.01mm units), remaining
        length (field 11), and the raw tag strings. Defaults to slot 0.

        Usage
        -------
        `AFC_ACE_RFID_DUMP UNIT=<unit> [SLOT=<0-based slot>] [LANE=<lane>]`

        Example
        -------
        ```
        AFC_ACE_RFID_DUMP UNIT=Ace2_1 SLOT=1
        ```
        """
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        slot = gcmd.get_int('SLOT', None)
        if slot is None:
            lane_name = gcmd.get('LANE', None)
            slot = (self._get_slot(lane_name) if lane_name and lane_name in self.afc.lanes else 0)
        try:
            result = self._ace.send_command('get_filament_info', {'index': slot})
        except Exception as e:
            gcmd.respond_info(f"Error reading ACE RFID slot {slot}: {e}")
            return
        result = result or {}
        raw = result.get('raw')
        parsed = {k: v for k, v in result.items() if k != 'raw'}
        lines = [f"ACE RFID slot {slot}:", f"  parsed: {parsed}"]
        if raw is not None:
            lines.append(f"  raw protobuf fields (field#: value): {raw}")
        else:
            lines.append("  (no raw field map, V1 ACE Pro or empty read)")
        gcmd.respond_info("\n".join(lines))

    #: How long a V1 rescan waits for the firmware to finish recognizing a tag.
    RESCAN_RECOGNIZE_S = 5.0
    cmd_AFC_ACE_RFID_RESCAN_help = (
        "Read a lane's spool tag again and apply it to the lane (ACE 2: turns the "
        "spool past the reader; V1: asks the firmware)")
    def cmd_AFC_ACE_RFID_RESCAN(self, gcmd: GCodeCommand) -> None:
        """
        Read a lane's spool tag again and apply it to the lane.

        ACE 2: the host drives the MFRC522 reader, so [AFC_ACE2_rfid] winds the
        filament back, feeds it past the reader, winds back to where the tag
        went past, reads it at rest and feeds the lane back where it was.
        Needs the lane staged and not in the toolhead, and no print running.

        V1 ACE: the firmware owns the reader and the host cannot poll it, so
        nothing moves: the slot's tag is asked of the firmware
        (get_filament_info) and, when recognized, replaces the lane's material,
        color and temperatures. Allowed on the lane in the toolhead.

        Usage
        -------
        `AFC_ACE_RFID_RESCAN UNIT=<unit> LANE=<lane> [DIST=<mm>] [SPEED=<mm/s>]`

        Example
        -------
        ```
        AFC_ACE_RFID_RESCAN UNIT=Ace2_1 LANE=lane2
        ```
        """
        lane_name = gcmd.get('LANE')
        lane = self.lanes.get(lane_name)
        if lane is None:
            error_str = f"{lane_name} is not a lane of {self.name}"
            raise gcmd.error(error_str)
        if not self._uses_firmware_rfid:
            rfid: Any = self.printer.lookup_object("AFC_ACE2_rfid", None)
            if (rfid is None
                or getattr(rfid, "ace2", None) is not self):
                error_str = (f"AFC_ACE_RFID_RESCAN on {self.name} needs "
                             f"[AFC_ACE2_rfid] bound to this unit")
                raise gcmd.error(error_str)
            rfid.rescan_lane(lane_name, gcmd)
            return
        if (self._ace is None
            or not self._ace.connected):
            error_str = "ACE not connected"
            raise gcmd.error(error_str)
        slot = self._get_slot(lane_name)
        if not 0 <= slot < self.SLOTS_PER_UNIT:
            error_str = f"{lane_name} has no ACE slot"
            raise gcmd.error(error_str)
        # rfid 3 is "recognizing": give the firmware a moment to finish.
        deadline = self.reactor.monotonic() + self.RESCAN_RECOGNIZE_S
        while True:
            self._refresh_slot_inventory(slot)
            inv = self._slot_inventory[slot]
            if (inv.get("rfid") != 3
                or self.reactor.monotonic() >= deadline):
                break
            self.reactor.pause(self.reactor.monotonic() + 0.5)
        if inv.get("rfid") != 2:
            rfid_code = inv.get("rfid")
            state_names = {0: "no tag found", 1: "tag not recognized", 3: "still recognizing"}
            state = (state_names.get(rfid_code, "no tag read") if isinstance(rfid_code, int)
                     else "no tag read")
            gcmd.respond_info(f"ACE {self.name}: {state} on {lane_name} (slot {slot})")
            return
        # The tag the firmware reads now wins over what the lane showed.
        lane.material = ""
        lane.color = ""
        lane.extruder_temp = None
        lane.bed_temp = None
        if apply_filament_defaults is not None:
            apply_filament_defaults(lane, inv, color_converter=rgb_array_to_hex)
        self.afc.save_vars()
        gcmd.respond_info(f"ACE {self.name}: read {lane_name}'s tag: "
                          f"{inv.get('brand') or ''} {lane.material or '?'} " f"{lane.color or ''}"
                          .replace("  ", " "))

    @staticmethod
    def _parse_ace_params(raw: Optional[str]) -> Optional[Dict[str, Any]]:
        """
        Parse the PARAMS argument into a dict. Accepts real JSON, but the
        gcode/console parser strips JSON double-quotes, so also accept the
        quote-less form {key:val,key:val} with bare keys, [a,b] lists, and
        int/float/bool/string value inference.

        :param raw: The PARAMS argument string.
        :return Optional[dict]: A params dict, or None when empty/unparseable.
        """
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        def conv(v: str) -> Any:
            """
            Coerce a token to a list, int, float, bool, or string.

            :param v: Raw token (possibly quoted or a [a,b] list).
            :return Any: The inferred Python value.
            """
            v = v.strip().strip('"').strip("'")
            if (v.startswith('[')
                and v.endswith(']')):
                return [conv(x) for x in v[1:-1].split(',') if x.strip()]
            for cast in (int, float):
                try:
                    return cast(v)
                except ValueError:
                    pass
            if v.lower() in ('true', 'false'):
                return v.lower() == 'true'
            return v

        s = raw.lstrip('{').rstrip('}')
        pairs, cur, depth = [], '', 0
        for ch in s:
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
            if (ch == ','
                and depth == 0):
                pairs.append(cur)
                cur = ''
            else:
                cur += ch
        if cur.strip():
            pairs.append(cur)
        out = {}
        for p in pairs:
            sep = ':' if ':' in p else ('=' if '=' in p else None)
            if sep is None:
                continue
            k, _, val = p.partition(sep)
            out[k.strip().strip('"').strip("'")] = conv(val)
        return out or None

    cmd_AFC_ACE_CMD_help = "Send a raw ACE protocol command and print the reply"
    def cmd_AFC_ACE_CMD(self, gcmd: GCodeCommand) -> None:
        """
        Send a raw ACE protocol command and print the reply, for probing
        which protocol methods this firmware actually supports.

        PARAMS has no spaces (gcode splits on whitespace); quotes are optional
        (the console strips them). A supported method replies code=0; an
        unsupported one replies code=400 InvalidCommand (shown as an error
        here) and is harmless.

        Usage
        -------
        `AFC_ACE_CMD UNIT=<unit> METHOD=<method> [PARAMS=<json-ish>]`

        Example
        -------
        ```
        AFC_ACE_CMD UNIT=Ace_1 METHOD=get_status
        AFC_ACE_CMD UNIT=Ace_1 METHOD=set_filament_info PARAMS={index:0,type:PLA,color:[255,0,0]}
        ```
        """
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        method = gcmd.get('METHOD', '')
        if not method:
            gcmd.respond_info("AFC_ACE_CMD: METHOD=<method> required")
            return
        try:
            params = self._parse_ace_params(gcmd.get('PARAMS', ''))
        except Exception as e:
            gcmd.respond_info(
                f"AFC_ACE_CMD: bad PARAMS ({e}), e.g. PARAMS={{index:0,type:PLA}}")
            return
        try:
            resp = self._ace.send_command(method, params=params)
            gcmd.respond_info(f"AFC_ACE_CMD {method}: OK -> {resp}")
        except Exception as e:
            gcmd.respond_info(f"AFC_ACE_CMD {method}: {e}")

    cmd_AFC_ACE_TEMP_INFO_help = "Report ACE box/PTC/environment temperatures and humidity"
    def cmd_AFC_ACE_TEMP_INFO(self, gcmd: GCodeCommand) -> None:
        """
        Report the ACE unit's temperature and humidity channels, box heater
        sensors, PTC (dryer) sensors and the enclosure environment. On the ACE2
        this reads the GET_TEMP opcode; the V1 ACE has no such reading and
        reports it as unsupported. Read-only diagnostic.

        Usage
        -------
        `AFC_ACE_TEMP_INFO UNIT=<unit>`

        Example
        -------
        ```
        AFC_ACE_TEMP_INFO UNIT=Ace2_1
        ```
        """
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        try:
            temp = self._ace.get_temp()
        except Exception as e:
            gcmd.respond_info(f"AFC_ACE_TEMP_INFO: {e}")
            return
        if not isinstance(temp, dict):
            gcmd.respond_info(f"AFC_ACE_TEMP_INFO: unexpected reply {temp}")
            return
        gcmd.respond_info("ACE temperatures:\n"
            f"  box1={temp.get('box1_temp', 'n/a')}  box2={temp.get('box2_temp', 'n/a')}\n"
            f"  ptc1={temp.get('ptc1_temp', 'n/a')}  ptc2={temp.get('ptc2_temp', 'n/a')}\n"
            f"  env={temp.get('env_temp', 'n/a')}  humidity={temp.get('env_humidity', 'n/a')}")

    cmd_AFC_ACE_MATERIAL_INFO_help = "Read a slot's stored (NVM) material name and status"
    def cmd_AFC_ACE_MATERIAL_INFO(self, gcmd: GCodeCommand) -> None:
        """
        Read a slot's stored material name + status (ACE2 GET_MATERIAL_INFO).
        This is the NVM-persisted name written by AFC_ACE_SET_MATERIAL, distinct
        from the live RFID tag read. Read-only. SLOT defaults to 0.

        Usage
        -------
        `AFC_ACE_MATERIAL_INFO UNIT=<unit> [SLOT=<n>]`

        Example
        -------
        ```
        AFC_ACE_MATERIAL_INFO UNIT=Ace2_1 SLOT=0
        ```
        """
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        slot = gcmd.get_int('SLOT', 0, minval=0)
        try:
            info = self._ace.get_material_info(slot)
        except Exception as e:
            gcmd.respond_info(f"AFC_ACE_MATERIAL_INFO: {e}")
            return
        if not isinstance(info, dict):
            gcmd.respond_info(f"AFC_ACE_MATERIAL_INFO: unexpected reply {info}")
            return
        name = info.get('material_name', '')
        gcmd.respond_info(
            f"ACE material info slot {slot}: name={name!r} status={info.get('status', 0)}")

    cmd_AFC_ACE_SET_MATERIAL_help = "Write a slot's stored material name (ACE2; persists to NVM)"
    def cmd_AFC_ACE_SET_MATERIAL(self, gcmd: GCodeCommand) -> None:
        """
        Write a slot's stored material name (ACE2 SET_MATERIAL_NAME).
        Persists to the unit's NVM and clears the slot status byte, then reads
        the value back for confirmation.

        Usage
        -------
        `AFC_ACE_SET_MATERIAL UNIT=<unit> SLOT=<n> NAME=<text>`

        Example
        -------
        ```
        AFC_ACE_SET_MATERIAL UNIT=Ace2_1 SLOT=0 NAME=PLA
        ```
        """
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        if not hasattr(self._ace, "set_material_name"):
            gcmd.respond_info("AFC_ACE_SET_MATERIAL: requires an ACE 2 Pro unit")
            return
        slot = gcmd.get_int('SLOT', None, minval=0)
        if slot is None:
            gcmd.respond_info("AFC_ACE_SET_MATERIAL: SLOT=<n> required")
            return
        name = gcmd.get('NAME', None)
        if name is None:
            gcmd.respond_info("AFC_ACE_SET_MATERIAL: NAME=<text> required")
            return
        try:
            self._ace.set_material_name(slot, name)
        except Exception as e:
            gcmd.respond_info(f"AFC_ACE_SET_MATERIAL: {e}")
            return
        # Read back so the operator sees exactly what the unit stored.
        try:
            info = self._ace.get_material_info(slot)
            stored = info.get('material_name', '') if isinstance(info, dict) else info
            gcmd.respond_info(f"AFC_ACE_SET_MATERIAL slot {slot}: stored name={stored!r}")
        except Exception as e:
            gcmd.respond_info(
                f"AFC_ACE_SET_MATERIAL slot {slot}: wrote {name!r} (read-back failed: {e})")

    cmd_AFC_ACE_SENSOR_STATE_help = "Read the ACE2 17-channel filament-sensor bitmask"
    def cmd_AFC_ACE_SENSOR_STATE(self, gcmd: GCodeCommand) -> None:
        """
        Read the ACE2 17-channel filament-sensor bitmask (GET_SENSOR_STATE) and
        report the raw mask and triggered channels. Read-only diagnostic.

        Usage
        -------
        `AFC_ACE_SENSOR_STATE UNIT=<unit>`

        Example
        -------
        ```
        AFC_ACE_SENSOR_STATE UNIT=Ace2_1
        ```
        """
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        try:
            state = self._ace.get_sensor_state()
        except Exception as e:
            gcmd.respond_info(f"AFC_ACE_SENSOR_STATE: {e}")
            return
        if not isinstance(state, dict):
            gcmd.respond_info(f"AFC_ACE_SENSOR_STATE: unexpected reply {state}")
            return
        mask = state.get('sensor_bitmask', 0)
        sensors = state.get('sensors', [])
        triggered = [i for i, on in enumerate(sensors) if on]
        gcmd.respond_info(
            f"ACE sensor state: mask=0x{mask:X} ({mask}) triggered channels={triggered}")

    def _pick_test_slot(self) -> Optional[int]:
        """
        First slot that reports a recognized tag / SKU (a loaded spool).

        :return Optional[int]: The slot index of the first loaded spool, or None if none.
        """
        for slot in range(self.SLOTS_PER_UNIT):
            inv = self._slot_inventory[slot]
            if (inv.get("rfid") in (2, 3)
                or inv.get("sku")):
                return slot
        return None

    def _poll_until_status(self, want_ready: bool, timeout: float) -> bool:
        """
        Poll get_status until status is (want_ready=True) / isn't ('ready').
        Returns True if the wanted condition was seen before timeout.

        :param want_ready: Target condition, True waits for 'ready', False
            waits for a non-'ready' (busy) status.
        :param timeout: Maximum time in seconds to poll.
        :return bool: True if the wanted condition was seen before timeout.
        """
        if self._ace is None:
            return False
        reactor = self.afc.reactor
        elapsed = 0.0
        while elapsed < timeout:
            try:
                hw = self._ace.get_status(timeout=2.0)
                if isinstance(hw, dict):
                    is_ready = hw.get("status", "") == "ready"
                    if is_ready == want_ready:
                        return True
            except Exception:
                pass
            reactor.pause(reactor.monotonic() + 0.2)
            elapsed += 0.2
        return False

    cmd_AFC_ACE_FEED_TEST_help = "Sweep feed speed to test whether it changes feed rate"
    cmd_AFC_ACE_FEED_TEST_options = {
        "SLOT":   {"type": "int",   "default": -1},
        "LENGTH": {"type": "float", "default": 100.0},
        "START":  {"type": "int",   "default": 10},
        "END":    {"type": "int",   "default": 250},
        "STEP":   {"type": "int",   "default": 20},
    }
    def cmd_AFC_ACE_FEED_TEST(self, gcmd: GCodeCommand) -> None:
        """
        Sweep the ACE feed/unwind speed to see whether the speed param
        actually changes the feed rate. For each speed it feeds LENGTH mm, times
        it (waiting for the ACE to finish), then unwinds LENGTH mm so the net
        travel is zero. Waits for 'ready' between every move so nothing returns
        FORBIDDEN. Prints speed -> elapsed and derived mm/s.

        Run with the test slot not loaded to the toolhead; the spool feeds toward
        the hub and returns each pass. Keep LENGTH below your dist_hub.
        This ties up the console for the duration of the sweep.

        Usage
        -------
        `AFC_ACE_FEED_TEST UNIT=<unit> [SLOT=<n>] [LENGTH=100] [START=10] [END=250] [STEP=20]`

        Example
        -------
        ```
        AFC_ACE_FEED_TEST UNIT=Ace_1 SLOT=0 LENGTH=100
        ```
        """
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        length = gcmd.get_float('LENGTH', 100.0, minval=1.0)
        start = gcmd.get_int('START', 10, minval=1)
        end = gcmd.get_int('END', 250, minval=start)
        step = gcmd.get_int('STEP', 20, minval=1)
        slot = gcmd.get_int('SLOT', -1)
        if slot < 0:
            picked_slot = self._pick_test_slot()
            slot = picked_slot if picked_slot is not None else -1
        if (slot < 0
            or slot >= self.SLOTS_PER_UNIT):
            gcmd.respond_info("AFC_ACE_FEED_TEST: no loaded slot detected, pass SLOT=<n>")
            return
        reactor = self.afc.reactor
        gcmd.respond_info(f"AFC_ACE_FEED_TEST: slot {slot}, {length:.0f}mm, speeds "
                          f"{start}..{end} step {step} (feed + net-zero unwind each pass)")
        results = []
        speed = start
        try:
            while speed <= end:
                self._wait_for_ace_ready()
                # feed and time to completion
                t0 = reactor.monotonic()
                self._ace.feed_filament(slot, length, speed)
                self._poll_until_status(False, timeout=5.0)   # wait until busy
                self._poll_until_status(True, timeout=max(30.0, length / 2))
                elapsed = reactor.monotonic() - t0
                rate = length / elapsed if elapsed > 0 else 0
                results.append((speed, elapsed))
                gcmd.respond_info(f"  speed={speed:>3}: {elapsed:5.2f}s  (~{rate:4.0f} mm/s)")
                # net-zero return at the same speed
                self._wait_for_ace_ready()
                self._ace.unwind_filament(slot, length, speed, "normal")
                self._poll_until_status(False, timeout=5.0)
                self._poll_until_status(True, timeout=max(30.0, length / 2))
                speed += step
        except Exception as e:
            gcmd.respond_info(f"AFC_ACE_FEED_TEST aborted at speed {speed}: {e}")
            self._wait_for_ace_ready()
            return
        if len(results) >= 2:
            rates = [(spd, length / el if el > 0 else 0.0) for spd, el in results]
            peak = max((r for _, r in rates), default=0.0) or 1.0
            t_slow, t_fast = results[0][1], results[-1][1]
            if (t_slow > 0
                and abs(t_slow - t_fast) / t_slow < 0.15):
                verdict = ("times ~constant -> speed param appears "
                           "CLAMPED/IGNORED (like the fan)")
            else:
                verdict = "times scale with speed -> speed param WORKS"
            # Max effective speed: the lowest commanded speed that reaches ~95%
            # of the best measured rate.
            knee = next((spd for spd, r in rates if r >= 0.95 * peak), rates[-1][0])
            gcmd.respond_info(f"AFC_ACE_FEED_TEST done. {verdict}\n"
                              f"  peak measured rate ~{peak:.0f} mm/s\n"
                              f"  max effective commanded speed ~{knee} "
                              f"(higher stops improving the rate)")
        else:
            gcmd.respond_info("AFC_ACE_FEED_TEST done.")

    cmd_AFC_ACE_DRY_help = "Start ACE filament dryer"
    cmd_AFC_ACE_DRY_options = {
        "UNIT": {"type": "string", "default": ""},
        "TEMP": {"type": "float", "default": 50.0},
        "DURATION": {"type": "float", "default": 90.0},
        "FAN": {"type": "int", "default": 7000},
        "ROTATE": {"type": "int", "default": 0},
    }
    def cmd_AFC_ACE_DRY(self, gcmd: GCodeCommand) -> None:
        """
        Start ACE filament dryer. TEMP is capped to max_dryer_temperature.
        The ACE Pro firmware ignores the fan speed and always runs its fan at
        7000, so FAN is kept for compatibility but has no effect.

        ROTATE=1 also turns the spools while drying, under the Bambu units'
        rule: it is refused while any lane on this unit has filament loaded to
        the hub or toolhead, and only lanes with nothing in the feed gears turn.
        Unthread the lanes (tip secured to the spool) to dry with rotation.

        Usage
        -------
        `AFC_ACE_DRY UNIT=<unit> TEMP=<C> DURATION=<minutes> [FAN=<rpm>] [ROTATE=<0|1>]`

        Example
        -------
        ```
        AFC_ACE_DRY UNIT=Ace_1 TEMP=50 DURATION=240 ROTATE=1
        ```
        """
        temp = gcmd.get_float('TEMP', 50.0)
        duration = gcmd.get_float('DURATION', 90.0)
        fan = gcmd.get_int('FAN', 7000)
        rotate = gcmd.get_int('ROTATE', 0, minval=0, maxval=1)
        if temp > self.max_dryer_temperature:
            gcmd.respond_info(f"ACE dryer: TEMP {temp:.0f}°C capped to "
                              f"max_dryer_temperature {self.max_dryer_temperature:.0f}°C")
            temp = self.max_dryer_temperature
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        try:
            self._ace.start_drying(temp, fan, duration)
            gcmd.respond_info(f"ACE dryer started: {temp}°C for {duration} min")
        except Exception as e:
            gcmd.respond_info(f"Error starting dryer: {e}")
            return
        # A new cycle replaces the old one's rotation, whatever it was.
        self._stop_dry_rotate()
        if rotate:
            committed = self._dry_rotate_committed()
            if committed:
                gcmd.respond_info(f"AFC_ACE_DRY: ROTATE disabled for {self.name} -- "
                                  f"{', '.join(committed)} still has filament loaded, and "
                                  f"turning another spool would take the motor off it. "
                                  f"Unload the lanes and leave the spools free to spin to "
                                  f"dry with rotation. Drying without it.")
            else:
                self._start_dry_rotate(gcmd)

    cmd_AFC_ACE_DRY_STOP_help = "Stop ACE filament dryer"
    def cmd_AFC_ACE_DRY_STOP(self, gcmd: GCodeCommand) -> None:
        """
        Stop ACE filament dryer.

        Usage
        -------
        `AFC_ACE_DRY_STOP UNIT=<unit>`

        Example
        -------
        ```
        AFC_ACE_DRY_STOP UNIT=Ace_1
        ```
        """
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        self._stop_dry_rotate()
        try:
            self._ace.stop_drying()
            gcmd.respond_info("ACE dryer stopped")
        except Exception as e:
            gcmd.respond_info(f"Error stopping dryer: {e}")

    # ── Drying spool rotation (AFC_ACE_DRY ROTATE=1) ──
    #
    # The Bambu units' rule, per lane on the ACE's own hardware. A slot's spool
    # and its feed gears share one motor, so a rollback on a slot with nothing
    # in the gears turns only the spool. A slot that reads 'ready' has filament
    # in the gears, and the same rollback would pull it out of the feeder, so
    # only 'empty' slots turn. Lanes loaded to the hub or toolhead stop it for
    # the whole unit, as on a Bambu unit: the unit has to drive their motor.

    #: dryer_status values that mean no cycle is running.
    _DRYER_IDLE = ("", "stop", "stopped", "stopping", "idle", "off", "none")
    #: seconds after AFC_ACE_DRY before an idle dryer reading counts as "finished",
    #: so the heartbeat has time to report the new cycle.
    _DRY_ROTATE_GRACE = 60.0
    #: seconds between retries while rotation is held off.
    _DRY_ROTATE_RETRY = 60.0

    @property
    def _dry_rotating(self) -> bool:
        """Whether AFC_ACE_DRY ROTATE=1 is active on this unit."""
        return self._dry_rotate_timer is not None

    def _dry_rotate_committed(self) -> List[str]:
        """
        Lanes whose filament is loaded to the hub or toolhead.

        Any one of them stops rotation on the whole unit, as on a Bambu unit.

        :return List[str]: names of the committed lanes, empty when none.
        """
        return [lane.name for lane in self.lanes.values()
                if lane.tool_loaded or lane.loaded_to_hub]

    def _dry_rotate_free(self, hw: Dict[str, Any]) -> List[Tuple[int, str]]:
        """
        The lanes free to turn: those whose slot has nothing in the feed gears.

        :param hw: a get_status payload.
        :return List[tuple]: (slot, lane name) pairs, in slot order.
        """
        slots = hw.get("slots") or []
        free = []
        for name, slot in self._slot_map.items():
            if (0 <= slot < len(slots)
                and (slots[slot] or {}).get("status") == "empty"):
                free.append((slot, name))
        return sorted(free)

    def _start_dry_rotate(self, gcmd: GCodeCommand) -> None:
        """
        Arm the rotation timer and say which lanes will turn.

        :param gcmd: the AFC_ACE_DRY command, for the reply.
        """
        reactor = self.afc.reactor
        now = reactor.monotonic()
        self._dry_rotate_started = now
        self._dry_rotate_hold = ""
        self._dry_rotate_timer = reactor.register_timer(self._dry_rotate_tick, now + 5.0)
        free = [name for _, name in self._dry_rotate_free(self._cached_hw_status or {})]
        threaded = sorted(set(self._slot_map) - set(free))
        msg = (f"AFC_ACE_DRY: rotating free spools on {self.name} every "
               f"{self.dry_rotate_interval:.0f}s")
        if free:
            msg += f" ({', '.join(free)})"
        if threaded:
            msg += (f". Not turning {', '.join(threaded)}: filament is in the "
                    f"feed gears, unthread it to dry with rotation")
        gcmd.respond_info(msg)

    def _stop_dry_rotate(self, reason: str = "") -> None:
        """
        Disarm the rotation timer and stop a spool that is turning now.

        Safe to call when rotation is not active.

        :param reason: logged when rotation was active, empty for none.
        """
        timer = self._dry_rotate_timer
        if timer is None:
            return
        self._dry_rotate_timer = None
        with contextlib.suppress(Exception):
            self.afc.reactor.unregister_timer(timer)
        slot = self._dry_rotate_slot
        if (slot is not None
            and self._ace is not None
            and self._ace.connected):
            with contextlib.suppress(Exception):
                self._ace.stop_unwind_filament(slot)
        self._dry_rotate_slot = None
        if reason:
            self.logger.info(f"ACE {self.name}: drying rotation stopped, {reason}")

    def _dry_rotate_hold_reason(self) -> str:
        """
        Why rotation cannot run right now, or '' when it can.

        Re-checked every tick, since lanes are loaded and prints start while
        the dryer runs.

        :return str: the reason, empty when nothing holds it off.
        """
        if (self._ace is None
            or not self._ace.connected):
            return "ACE not connected"
        try:
            if self.afc.function.is_printing():
                return "printing"
        except Exception:
            pass
        if self._operation_active:
            return "another ACE operation is running"
        committed = self._dry_rotate_committed()
        if committed:
            return f"{', '.join(committed)} has filament loaded"
        if (self._cached_hw_status or {}).get("status", "ready") != "ready":
            return "ACE busy"
        return ""

    def _dry_rotate_tick(self, eventtime: float) -> float:
        """
        Reactor timer: turn each free spool once, then wait an interval.

        Ends itself when the dryer reports its cycle over.

        :param eventtime: reactor time of this tick.
        :return float: the next tick, or reactor.NEVER when finished.
        """
        reactor = self.afc.reactor
        if not self._dry_rotating:
            return reactor.NEVER
        dryer = ((self._cached_hw_status or {}).get("dryer_status") or {})
        dryer_idle = str(dryer.get("status") or "").strip().lower() in self._DRYER_IDLE
        if (eventtime - self._dry_rotate_started > self._DRY_ROTATE_GRACE
            and dryer_idle):
            self._stop_dry_rotate("the drying cycle ended")
            return reactor.NEVER
        hold = self._dry_rotate_hold_reason()
        if hold != self._dry_rotate_hold:
            self._dry_rotate_hold = hold
            if hold:
                self.logger.info(f"ACE {self.name}: drying rotation holding, {hold}")
            else:
                self.logger.info(f"ACE {self.name}: drying rotation resumed")
        if hold:
            return eventtime + self._DRY_ROTATE_RETRY
        timer = self._dry_rotate_timer
        self._dry_rotate_spin(timer)
        if self._dry_rotate_timer is not timer:
            return reactor.NEVER
        return reactor.monotonic() + self.dry_rotate_interval

    def _dry_rotate_spin(self, timer: Any) -> None:
        """
        Turn every free spool once, one slot at a time.

        Each slot is checked again just before it moves, so filament threaded
        since the last tick is never pulled back. This does not claim
        _operation_active: a load started meanwhile owns that flag, and this
        holds off whenever it is set instead.

        :param timer: the rotation timer this pass belongs to; the pass ends
            as soon as rotation is stopped or re-armed under it.
        """
        if self._ace is None:
            return
        length, speed = self.dry_rotate_length, self.dry_rotate_speed
        try:
            for slot, name in self._dry_rotate_free(self._cached_hw_status or {}):
                if (self._dry_rotate_timer is not timer
                    or self._operation_active
                    or self._dry_rotate_committed()):
                    return
                hw = self._ace.get_status(timeout=2.0)
                if (not isinstance(hw, dict)
                    or hw.get("status") != "ready"
                    or (slot, name) not in self._dry_rotate_free(hw)):
                    continue
                self._dry_rotate_slot = slot
                self._ace.unwind_filament(slot, length, speed)
                self._poll_until_status(False, timeout=5.0)
                self._poll_until_status(True, timeout=length / speed + 15.0)
        except Exception as e:
            self.logger.warning(f"ACE {self.name}: drying rotation failed: {e}")
        finally:
            if self._dry_rotate_timer is timer:
                self._dry_rotate_slot = None

    cmd_AFC_ACE_FAN_help = "Set ACE unit fan speed (ACE2: 0-100%; V1: ignored by firmware)"
    def cmd_AFC_ACE_FAN(self, gcmd: GCodeCommand) -> None:
        """
        Set the ACE unit fan speed.

        On the ACE2 this drives SET_FAN (cmd 71); the firmware maps 0-100% to 5
        internal levels. On the V1 ACE the firmware ignores fan speed, so the
        command is accepted but has no effect. This controls the unit fan, which
        is separate from the dryer cycle's own on/off fan (set via AFC_ACE_DRY FAN=).

        Usage
        -------
        `AFC_ACE_FAN UNIT=<unit> SPEED=<0-100>`

        Example
        -------
        ```
        AFC_ACE_FAN UNIT=Ace2_1 SPEED=50
        ```
        """
        speed = gcmd.get_int('SPEED', 100, minval=0, maxval=100)
        if (not self._ace
            or not self._ace.connected):
            gcmd.respond_info("ACE not connected")
            return
        try:
            # 'speed' is the ACE2 encoder key; 'fan_speed' covers the V1 method.
            self._ace.send_command("set_fan_speed", params={"speed": speed, "fan_speed": speed})
            gcmd.respond_info(f"ACE fan set to {speed}%")
        except Exception as e:
            gcmd.respond_info(f"Error setting fan: {e}")

    cmd_AFC_ACE_LANE_RESET_help = "Retract ACE lane filament back into unit"
    def cmd_AFC_ACE_LANE_RESET(self, gcmd: GCodeCommand) -> None:
        """
        Retract lane filament back into ACE unit.

        Usage
        -------
        `AFC_ACE_LANE_RESET UNIT=<unit> LANE=<lane>`

        Example
        -------
        ```
        AFC_ACE_LANE_RESET UNIT=Ace_1 LANE=lane1
        ```
        """
        lane_name = gcmd.get('LANE', '')
        if (not lane_name
            or lane_name not in self.afc.lanes):
            gcmd.respond_info("Usage: AFC_ACE_LANE_RESET LANE=<lane_name>")
            return
        cur_lane = self.afc.lanes[lane_name]
        self.eject_lane(cur_lane)
        gcmd.respond_info(f"Lane {lane_name} reset")

    cmd_AFC_ACE_FEED_ASSIST_help = (
        "Start/stop feed assist for a lane/slot (stop overrides watchdog until "
        "started again)")
    def cmd_AFC_ACE_FEED_ASSIST(self, gcmd: GCodeCommand) -> None:
        """
        Manually start/stop feed assist for a lane or slot.

        ENABLE=0 stops assist and suppresses the assist watchdog for that slot
        so it stays off; ENABLE=1 (or any load/toolchange) starts assist and
        clears the suppression. Either LANE or SLOT (0-based) is required.

        Usage
        -------
        `AFC_ACE_FEED_ASSIST UNIT=<unit> ENABLE=<0|1> [LANE=<lane>] [SLOT=<n>]`

        Example
        -------
        ```
        AFC_ACE_FEED_ASSIST UNIT=Ace_1 LANE=lane1 ENABLE=0
        ```
        """
        enable = gcmd.get_int('ENABLE', None)
        if enable is None:
            error_str = "ENABLE is required (0 or 1)"
            raise gcmd.error(error_str)
        lane_name = gcmd.get('LANE', None)
        slot = gcmd.get_int('SLOT', None)
        if lane_name is not None:
            slot = self._slot_map.get(lane_name)
            if slot is None:
                error_str = f"Unknown lane for this unit: {lane_name}"
                raise gcmd.error(error_str)
        if slot is None:
            error_str = "LANE or SLOT is required"
            raise gcmd.error(error_str)
        if enable:
            # Stop other assisting slots first; the ACE assists one slot at a time
            # and refuses further starts with error_2.
            for other in list(self._feed_assist_active):
                if other != slot:
                    self._stop_feed_assist(other)
            self._start_feed_assist(slot, explicit=True)  # clears suppression
            gcmd.respond_info(f"Feed assist started on slot {slot}")
        else:
            self._assist_suppressed.add(slot)
            if slot in self._feed_assist_active:
                self._stop_feed_assist(slot)
            elif (self._ace
                  and self._ace.connected):
                # Not tracked as assisting, but send the stop anyway in case
                # firmware state drifted from our tracking.
                try:
                    self._ace.stop_feed_assist(slot)
                except Exception:
                    pass
            gcmd.respond_info(f"Feed assist stopped on slot {slot}, watchdog suppressed "
                              f"until assist is started again")

    # ── Hardware interaction helpers ──
    def _start_feed_assist(self, slot: int, explicit: bool = False) -> None:
        """
        Enable ACE feed assist on a slot, tracking it as active.

        Idempotent and retried on timeout; FORBIDDEN replies are left for the
        watchdog to retry. No-op when the slot is already active or the ACE is
        disconnected.

        :param slot: 0-based ACE slot index.
        :param explicit: True only for a user AFC_ACE_FEED_ASSIST ENABLE=1, the only
            caller allowed to clear a manual ENABLE=0 suppression; automatic
            callers must leave it in place.
        """
        if explicit:
            self._assist_suppressed.discard(slot)
        elif slot in self._assist_suppressed:
            return
        if not (self._ace and self._ace.connected):
            return
        # The ACE assists one slot at a time, so stop any other active slot first;
        # this must hold at every call site.
        for other in list(self._feed_assist_active):
            if other != slot:
                self._stop_feed_assist(other)
        if slot in self._feed_assist_active:
            return
        # start_feed_assist is idempotent, so retry on timeout rather than leave
        # the loaded lane without assist after a serial hiccup.
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                self._wait_for_ace_ready()
                self._ace.start_feed_assist(slot)
                self._feed_assist_active.add(slot)
                return
            except ACETimeoutError as e:
                if attempt < attempts:
                    self.logger.debug(f"start feed assist slot {slot} timed out "
                                      f"(attempt {attempt}/{attempts}), retrying: {e}")
                    continue
                self.logger.error(f"Failed to start feed assist slot {slot} after "
                                  f"{attempts} attempts: {e}")
            except Exception as e:
                msg = str(e)
                # error_2 is the ACE refusing to start assist (not transient): the
                # slot already assists, or another slot holds the single assist.
                # Track it if it is assisting, else accept the refusal quietly.
                if ('error_2' in msg
                    or 'code=2' in msg):
                    try:
                        slots = (self._cached_hw_status or {}).get('slots', [])
                        already = (slot < len(slots) and slots[slot].get('slot_status') ==
                                   'assisting')
                    except Exception:
                        already = False
                    if already:
                        self._feed_assist_active.add(slot)
                        self.logger.debug(f"Feed assist slot {slot} already assisting (error_2); "
                                          f"marked tracked")
                    else:
                        self.logger.debug(f"Feed assist slot {slot} refused (error_2, concurrent-"
                                          f"assist limit or slot state); leaving off")
                    return
                # FORBIDDEN is transient (slot state still settling); the watchdog
                # re-issues it on the next tick, so log at debug.
                if 'FORBIDDEN' in msg.upper():
                    self.logger.debug(f"Feed assist slot {slot} not permitted yet "
                                      f"(FORBIDDEN); watchdog will retry: {e}")
                else:
                    self.logger.error(f"Failed to start feed assist slot {slot}: {e}")
                return

    def _stop_feed_assist(self, slot: int) -> None:
        """
        Disable ACE feed assist on a slot, clearing it from active tracking.

        Idempotent and retried on timeout. No-op when the slot is not tracked
        active or the ACE is disconnected.

        :param slot: 0-based ACE slot index.
        """
        if slot not in self._feed_assist_active:
            return
        if not (self._ace and self._ace.connected):
            return
        # Retry on timeout for the same reason as start: a late ACK shouldn't
        # leave us believing assist is still running on a slot we meant to
        # stop. stop_feed_assist is idempotent too.
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                self._wait_for_ace_ready()
                self._ace.stop_feed_assist_sync(slot)
                self._feed_assist_active.discard(slot)
                return
            except ACETimeoutError as e:
                if attempt < attempts:
                    self.logger.debug(f"stop feed assist slot {slot} timed out "
                                      f"(attempt {attempt}/{attempts}), retrying: {e}")
                    continue
                self.logger.error(f"Failed to stop feed assist slot {slot} after "
                                  f"{attempts} attempts: {e}")
            except Exception as e:
                self.logger.error(f"Failed to stop feed assist slot {slot}: {e}")
                return

    def _active_assist_lane(self) -> Optional[str]:
        """
        Name of the lane that should currently have feed assist.

        :return Optional[str]: The name of this unit's lane that is tool_loaded on the
            extruder currently on the shuttle, or None if there isn't one.
        """
        try:
            active_ext = self.printer.lookup_object('toolhead').get_extruder().get_name()
        except Exception:
            return None
        # Prefer the lane the extruder records as loaded, falling back to the
        # tool_loaded lane on that extruder, since lane_loaded can lag at print start.
        candidate = None
        for lane in self.lanes.values():
            ext_obj = lane.extruder_obj
            if (ext_obj is None
                or not lane.tool_loaded):
                continue
            # The toolhead reports the Klipper extruder name, which can differ
            # from the [AFC_extruder] section name, so match either.
            ext_names = {ext_obj.name, ext_obj.th_extruder_name}
            if active_ext not in ext_names:
                continue
            if ext_obj.lane_loaded == lane.name:
                return lane.name
            if candidate is None:
                candidate = lane.name
        return candidate

    def _maybe_assist_watchdog(self) -> None:
        """
        Heartbeat watchdog: reconcile feed assist to the active lane.

        If the active tool's lane should be assisted but isn't (or the wrong
        slot is), schedule a reconcile. Idempotent, a no-op once assist is
        correct. Disabled when assist_watchdog is off.
        """
        if not self._assist_watchdog:
            return
        name = self._active_assist_lane()
        if name is None:
            return
        slot = self._slot_map.get(name)
        lane = self.afc.lanes.get(name)
        if (slot is not None
            and slot in self._assist_suppressed):
            return  # manually stopped via AFC_ACE_FEED_ASSIST, leave it off
        if (slot is not None
            and lane is not None
            and self._use_feed_assist(lane)
            and self._feed_assist_active != {slot}):
            self.logger.info(f"ACE assist watchdog: enabling feed assist for {name} (slot {slot})")
            self.afc.reactor.register_callback(lambda et, n=name: self._reconcile_feed_assist(n))

    def _check_stuck(self, result: Dict[str, Any]) -> None:
        """
        Spot a stuck/tangled spool from the ACE's continuous assist time.

        The firmware reports cont_assist_time, how long the feed-assist has
        been running without stopping. A healthy buffer refill is a brief
        blip; a jam keeps the feed running. If that time climbs past stuck_time
        while we're printing with assist on, the spool is stuck: stop driving
        the jam and pause. Runs only on idle heartbeats (the caller skips it
        during load/unload, where long feeds are normal).

        :param result: ACE status dict; its ``cont_assist_time`` is compared
            against stuck_time to decide whether to trip a one-shot pause.
        """
        if not self._stuck_detection:
            return
        # Only meaningful during a real print with assist actually running.
        # Outside those conditions, clear the latch so it re-arms cleanly.
        if (not self.afc.function.in_print()
            or self.afc.function.is_paused()
            or not self._feed_assist_active):
            self._stuck_tripped = False
            return
        cont = result.get("cont_assist_time")
        if cont is None:
            return
        try:
            cont = float(cont)
        except (TypeError, ValueError):
            return
        if cont < self._stuck_time:
            # Pump cycled normally, the previous jam (if any) cleared.
            self._stuck_tripped = False
            return
        if self._stuck_tripped:
            return  # already handled this jam; don't re-pause every heartbeat
        self._stuck_tripped = True
        # Defer the pause off the serial/heartbeat path onto the reactor.
        self.afc.reactor.register_callback(lambda et, c=cont: self._handle_stuck(c))

    def _handle_stuck(self, cont_time: float) -> None:
        """
        Stop feed assist on the active lane and pause the print for a stuck
        spool (deferred onto the reactor from _check_stuck).

        :param cont_time: Continuous feed-assist run time in seconds reported
            by the ACE, included in the pause message.
        """
        name = self._active_assist_lane()
        slot = self._slot_map.get(name) if name else None
        # Stop driving the feed into the blockage before we pause.
        if slot is not None:
            try:
                self._stop_feed_assist(slot)
            except Exception:
                pass
        lane_str = f" lane {name}" if name else ""
        msg = (f"ACE stuck spool detected on {self.name}{lane_str}: feed assist ran "
               f"continuously for {cont_time:.1f}s (>= {self._stuck_time:.1f}s threshold). "
               "The spool is likely tangled or jammed at the unit. Clear the snag, then "
               "resume. Run AFC_ACE_STUCK_SPOOL_DETECTION ENABLE=0 to disable this check.")
        try:
            # Route through AFC so it snapshots position and uses the AFC
            # pause/resume path (z-hop, restore on AFC_RESUME).
            self.afc.error.AFC_error(msg, pause=True)
        except Exception:
            self.logger.error(msg)
            self.gcode.run_script_from_command("PAUSE")

    cmd_AFC_ACE_STUCK_SPOOL_DETECTION_help = "Enable/disable ACE stuck spool detection"
    def cmd_AFC_ACE_STUCK_SPOOL_DETECTION(self, gcmd: GCodeCommand) -> None:
        """
        Toggle stuck spool detection or adjust its threshold at runtime.

        Usage
        -------
        `AFC_ACE_STUCK_SPOOL_DETECTION UNIT=<unit> [ENABLE=0|1] [STUCK_TIME=<seconds>]`

        Example
        -------
        ```
        AFC_ACE_STUCK_SPOOL_DETECTION UNIT=Ace_1 ENABLE=1 STUCK_TIME=6
        ```
        """
        enable = gcmd.get_int('ENABLE', None, minval=0, maxval=1)
        if enable is not None:
            self._stuck_detection = bool(enable)
            if not self._stuck_detection:
                self._stuck_tripped = False
        stuck_time = gcmd.get_float('STUCK_TIME', None, minval=2.0)
        if stuck_time is not None:
            self._stuck_time = stuck_time
        state = "ON" if self._stuck_detection else "OFF"
        self.logger.info(f"ACE stuck spool detection {state}: stuck_time={self._stuck_time:.1f}s")

    def _handle_extruder_activated(self) -> None:
        """
        ``extruder:activate_extruder`` handler: when one of our lanes is
        loaded on the now-active extruder, schedule a feed-assist reconcile to
        switch assist to it. Mode-agnostic.
        """
        name = self._active_assist_lane()
        if name is None:
            return
        lane = self.afc.lanes.get(name)
        if (lane is not None
            and self._use_feed_assist(lane)):
            self.afc.reactor.register_callback(lambda et, n=name: self._reconcile_feed_assist(n))

    def _wait_for_ace_ready(self, timeout: float = 30.0) -> bool:
        """
        Wait for the overall ACE status to be 'ready' before sending commands.

        After a retract/feed completes, the slot may report 'ready'/'empty'
        before the ACE controller finishes internal housekeeping. Sending
        a new feed_filament while the ACE is still busy returns FORBIDDEN.

        Returns True once the unit reports ready. On timeout it still returns
        (the caller's own feed/unwind then surfaces a FORBIDDEN if the unit is
        genuinely busy) but returns False so a caller can choose to bail; on a
        missing/disconnected link it returns False immediately.

        :param timeout: Maximum time in seconds to wait for 'ready' before
            logging a warning and proceeding anyway.
        :return bool: True if the unit reached 'ready', else False.
        """
        if (self._ace is None
            or not self._ace.connected):
            return False
        poll_interval = 0.5
        elapsed = 0.0
        while elapsed < timeout:
            try:
                hw_status = self._ace.get_status(timeout=2.0)
                if isinstance(hw_status, dict):
                    if hw_status.get("status", "") == "ready":
                        return True
                    self.logger.debug(f"ACE: waiting for ready "
                                      f"(status={hw_status.get('status', '?')}, "
                                      f"{elapsed:.1f}s/{timeout:.0f}s)")
            except Exception:
                pass
            self.afc.reactor.pause(self.afc.reactor.monotonic() + poll_interval)
            elapsed += poll_interval
        self.logger.warning(f"ACE: did not become ready within {timeout:.0f}s, proceeding anyway")
        return False

    def _slot_reports_empty(self, slot_index: int, confirm: int = 3, pause: float = 0.15) -> bool:
        """
        True when the ACE reports this slot empty on ``confirm`` consecutive
        idle polls, i.e. the spool was really pulled back out, not a one-poll
        flicker.

        Only an explicit 'empty' on a not-busy unit counts, and any other poll
        returns False at once, since the ACE 2 can briefly report a present slot
        'empty' mid-stage.

        :param slot_index: 0-based ACE slot index to check.
        :param confirm: consecutive empty polls required to confirm removal.
        :param pause: seconds between confirmation polls.
        :return bool: True only when every poll shows the settled slot empty.
        """
        if (self._ace is None
            or not self._ace.connected):
            return False
        for i in range(max(1, confirm)):
            if i:
                self.afc.reactor.pause(self.afc.reactor.monotonic() + pause)
            try:
                hw = self._ace.get_status(timeout=2.0)
            except Exception:
                return False
            if (not isinstance(hw, dict)
                or hw.get("status") == "busy"):
                return False
            slots = hw.get("slots", [])
            if not (0 <= slot_index < len(slots)):
                return False
            slot_data = slots[slot_index]
            if not isinstance(slot_data, dict):
                return False
            if slot_data.get("status") != "empty":
                return False
        return True

    def _slot_is_moving(self, hw_status: Dict[str, Any], slot_index: int) -> bool:
        """
        True while the ACE is physically moving filament for ``slot_index``.

        Protocol-agnostic: V1 exposes the motor state in the slot's 'status'
        field, while V2 keeps 'status' at 'ready'/'empty' and reports motion via
        the overall 'busy' status and the slot's raw 'slot_status'. Used to track
        feed/unwind start and completion so an operation isn't reported done the
        moment its command is sent.

        :param hw_status: status dict from the ACE get_status call.
        :param slot_index: 0-based slot index being moved.
        :return bool: True if the slot/unit is actively moving filament.
        """
        if not isinstance(hw_status, dict):
            return False
        if hw_status.get("status") == "busy":
            return True
        slots = hw_status.get("slots", [])
        if (0 <= slot_index < len(slots)
            and isinstance(slots[slot_index], dict)):
            slot_data = slots[slot_index]
            if slot_data.get("status") not in ("ready", "empty", "", None):
                return True
            if slot_data.get("slot_status") in ("feeding", "rollback", "preloading", "winding",
                             "unwinding"):
                return True
        return False

    def _last_feed_length(self, slot_index: int) -> Optional[float]:
        """
        How far the slot's last move ran, from the unit's feed info.

        :param slot_index: 0-based slot index
        :return Optional[float]: mm moved, or None when the unit can't say
        """
        if self._ace is None:
            return None
        try:
            feeds = (self._ace.send_command("get_feed_info") or {}).get("feed_info") or []
            return float(feeds[slot_index].get("length"))
        except Exception:
            return None

    def _stop_feed_at_sensor(self, slot_index: int) -> None:
        """
        Stop the slot's feed once the toolhead sensor has the filament.

        A feed_filament runs its full length unless stopped, so without this
        the unit keeps pushing into the extruder gears until its own slip check
        gives up, grinding the filament at its rollers.

        :param slot_index: 0-based slot index being fed
        """
        if self._ace is None:
            return
        for _ in range(STOP_CONFIRM_TRIES):
            try:
                self._ace.stop_feed_filament(slot_index)
                self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.3)
                if not self._slot_is_moving(self._ace.get_status(timeout=2.0), slot_index):
                    self.logger.debug(f"ACE {self.name}: feed on slot {slot_index} stopped at "
                                      f"the toolhead sensor")
                    return
            except Exception as e:
                self.logger.debug(f"ACE {self.name}: stop at the toolhead sensor on slot "
                                  f"{slot_index}: {e}")
        self.logger.warning(f"ACE {self.name}: the feed on slot {slot_index} did not confirm "
                            f"stopped at the toolhead sensor")

    @staticmethod
    def _stall_hint(lane: AFCLane) -> str:
        """
        Error text for a load the unit reported as stalled (feed_error).

        :param lane: lane that failed to load
        :return str: the hint, starting with a newline
        """
        return (f"\nThe ACE reported a feed error: its encoder saw {lane.name}'s "
                f"filament stop moving in the tube. Check for a kink, a damaged "
                f"spot on the filament or a blockage, clear it, then load again.")

    def _note_move_error(self, hw_status: Dict[str, Any], slot_index: int) -> None:
        """
        Record and log the error a slot reported during a move, once per move.

        :param hw_status: status dict from the ACE get_status call
        :param slot_index: 0-based slot index being moved
        """
        if self._last_move_error is not None:
            return
        slot_data = (hw_status.get("slots") or [])[slot_index]
        err = next((v for v in (slot_data.get("slot_status"), slot_data.get("status"))
                   if isinstance(v, str) and v.endswith("_error")), "error")
        self._last_move_error = err
        self.logger.warning(f"ACE {self.name}: slot {slot_index} reported {err}")

    def _slot_in_error(self, hw_status: Dict[str, Any], slot_index: int) -> bool:
        """
        True if the slot reports any firmware error state (feed/rollback/
        assist/preload/stuck/tangled/motor error). Protocol-agnostic: checks both
        the slot 'status' and 'slot_status' fields for an '_error' verdict.

        :param hw_status: status dict from the ACE get_status call
        :param slot_index: 0-based slot index to check
        :return bool: True if the slot reports an error state
        """
        if not isinstance(hw_status, dict):
            return False
        slots = hw_status.get("slots", [])
        if (0 <= slot_index < len(slots)
            and isinstance(slots[slot_index], dict)):
            slot_data = slots[slot_index]
            for key in ("slot_status", "status"):
                val = slot_data.get(key)
                if (isinstance(val, str)
                    and val.endswith("_error")):
                    return True
        return False

    def _wait_for_feed_complete(self, slot_index: int, length_mm: float, speed_mm_s: float,
                                lane: Optional[AFCLane] = None, poll_interval: float = 0.5) -> bool:
        """
        Wait for ACE feed/unwind movement to complete by polling slot status.

        :param slot_index: 0-based ACE slot index being moved.
        :param length_mm: Move length in mm, used to bound the wait.
        :param speed_mm_s: Move speed in mm/s, used to bound the wait.
        :param lane: Optional lane; if its toolhead sensor triggers the wait
            returns early as successful.
        :param poll_interval: Status poll interval in seconds.
        :return bool: True if the slot started then returned to ready/empty (or
            the lane sensor triggered); False on no-start or timeout.
        """
        self._last_move_error = None
        move_start = self.afc.reactor.monotonic()
        if (self._ace is None
            or not self._ace.connected):
            return False

        # Bound the wait against the firmware speed ceiling, not the commanded
        # speed, padded for the unit's real rate lagging.
        eff_speed = min(max(speed_mm_s, 1), FEED_SPEED_CEILING)
        expected = length_mm / eff_speed
        max_wait = expected * 1.5 + 15.0
        deadline = self.afc.reactor.monotonic() + max_wait

        # Phase 0: wait for the slot to start moving (see _slot_is_moving, it
        # handles both V1's per-slot 'status' and V2's overall 'busy' signal).
        departure_deadline = (self.afc.reactor.monotonic() + self.feed_departure_timeout)
        motor_started = False
        while self.afc.reactor.monotonic() < departure_deadline:
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.2)
            if (lane is not None
                and lane.get_toolhead_pre_sensor_state()):
                self._stop_feed_at_sensor(slot_index)
                return True
            try:
                if self._slot_is_moving(self._ace.get_status(timeout=2.0), slot_index):
                    motor_started = True
                    break
            except Exception:
                pass
        if not motor_started:
            # A short move can start and finish between polls, so treat it as done
            # if the unit is now idle without error; longer moves are no-starts.
            if expected <= 2.0:
                try:
                    st = self._ace.get_status(timeout=2.0)
                    if (not self._slot_is_moving(st, slot_index)
                        and not self._slot_in_error(st, slot_index)):
                        self.logger.debug(f"ACE wait: slot {slot_index} short move "
                                          f"({length_mm:.0f}mm) completed before motion was "
                                          f"observed, treating as done")
                        return True
                except Exception:
                    pass
            self.logger.debug(f"ACE wait: slot {slot_index} never reported motion after "
                              f"feed/unwind command, motor may not have started")
            return False

        # Phase 1: wait for movement to finish. Require two consecutive idle
        # reads so a transient status frame doesn't report completion early.
        idle_reads = 0
        while self.afc.reactor.monotonic() < deadline:
            if lane is None:
                self.afc.reactor.pause(self.afc.reactor.monotonic() + poll_interval)
            else:
                # Watch the sensor between status polls, so the feed is stopped
                # within a few mm of the trigger rather than up to a poll later.
                reached = False
                for _ in range(max(1, int(poll_interval / SENSOR_POLL_S))):
                    self.afc.reactor.pause(self.afc.reactor.monotonic() + SENSOR_POLL_S)
                    if lane.get_toolhead_pre_sensor_state():
                        reached = True
                        break
                if reached:
                    self.logger.debug(f"ACE wait: toolhead sensor triggered for slot {slot_index}")
                    self._stop_feed_at_sensor(slot_index)
                    return True

            try:
                st = self._ace.get_status(timeout=2.0)
                # The error can sit on a single frame; the heartbeat's copy
                # covers one this poll missed, once it is newer than the move.
                seen_frames = [st]
                if (self._hw_status_time or 0) > move_start:
                    seen_frames.append(self._cached_hw_status)
                for seen in seen_frames:
                    if self._slot_in_error(seen, slot_index):
                        self._note_move_error(seen, slot_index)
                if self._slot_is_moving(st, slot_index):
                    idle_reads = 0
                else:
                    idle_reads += 1
                    if idle_reads >= 2:
                        return True
            except Exception:
                pass

        self.logger.debug(f"ACE wait: timeout waiting for slot {slot_index} "
                          f"movement ({max_wait:.1f}s)")
        return False

    def _smart_load_retry(self, cur_lane: AFCLane, slot: int, feed_dist: float,
                          max_retries: int = 3) -> bool:
        """
        Resend the full feed command when the initial feed stalls or times out.

        :param cur_lane: Lane being loaded.
        :param slot: 0-based ACE slot index.
        :param feed_dist: Feed distance in mm to resend each attempt.
        :param max_retries: Maximum number of feed retries.
        :return bool: True once the toolhead sensor triggers, else False.
        """
        if self._ace is None:
            return False
        for attempt in range(max_retries):
            self.logger.info(f"Feed retry {attempt + 1}/{max_retries} for {cur_lane.name} "
                             f"({feed_dist:.0f}mm)")
            try:
                self._wait_for_ace_ready()
                self._ace.feed_filament(slot, feed_dist, self.feed_speed)
                self._wait_for_feed_complete(slot, feed_dist, self.feed_speed, cur_lane)
                if cur_lane.get_toolhead_pre_sensor_state():
                    return True
            except Exception:
                pass
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 1.0)
        return False

    def _calibrate_hub_inner(self, cur_lane: AFCLane) -> Tuple[bool, str, float]:
        """
        Calibrate dist_hub by incrementally feeding until the hub sensor
        triggers; the first-detection distance is the measurement.

        Requires a physical hub sensor; rewrites dist_hub to config on success.

        :param cur_lane: Lane to calibrate.
        :return tuple: (success, message, measured_distance).
        """
        slot = self._get_slot(cur_lane.name)
        if (not self._ace
            or not self._ace.connected):
            return False, "ACE not connected", 0

        hub = cur_lane.hub_obj
        if (hub is None
            or hub.is_virtual_pin()):
            return False, "Physical hub sensor required for calibration", 0

        if hub.state:
            return False, "Hub sensor already triggered, clear the hub first", 0

        max_distance = 4000
        step = self.calibration_step

        self.logger.raw(f'Calibrating dist_hub for {cur_lane.name} '
                        f'(max {max_distance}mm in {step}mm steps)')

        # Coarse pass
        coarse_fed = 0.0
        triggered = False
        while coarse_fed < max_distance:
            feed_step = min(step, max_distance - coarse_fed)
            try:
                self._wait_for_ace_ready()
                self._ace.feed_filament(slot, feed_step, self.feed_speed)
                self._wait_for_feed_complete(slot, feed_step, self.feed_speed)
            except Exception as e:
                self.logger.error(f"ACE hub calibrate: feed failed: {e}")
                break
            coarse_fed += feed_step

            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.2)
            if hub.state:
                self.logger.info(f"ACE hub calibrate: coarse trigger at {coarse_fed:.1f}mm")
                triggered = True
                break

        if not triggered:
            if coarse_fed > 0:
                try:
                    self._wait_for_ace_ready()
                    self._ace.unwind_filament(slot, coarse_fed, self.retract_speed)
                    self._wait_for_feed_complete(slot, coarse_fed, self.retract_speed)
                except Exception as e:
                    self.logger.error(f"ACE hub calibrate: retract failed: {e}")
            return False, (f"Hub sensor did not trigger after {coarse_fed:.0f}mm. "
                           "Check filament path and hub sensor wiring."), coarse_fed

        # Use the first trigger distance directly (no fine re-feed pass).
        distance = coarse_fed
        self.logger.info(f"ACE hub calibrate: trigger at {distance:.1f}mm")

        # Retract
        retract_dist = max(distance - 50, 0)
        if retract_dist > 0:
            try:
                self._wait_for_ace_ready()
                self._ace.unwind_filament(slot, retract_dist, self.retract_speed)
                self._wait_for_feed_complete(slot, retract_dist, self.retract_speed)
            except Exception as e:
                self.logger.error(f"ACE hub calibrate: retract failed: {e}")

        old_dist = cur_lane.dist_hub
        new_dist = round(distance, 2)
        cal_msg = f'\n dist_hub: New: {new_dist} Old: {old_dist}'
        cur_lane.dist_hub = new_dist
        self.afc.function.ConfigRewrite(cur_lane.fullname, "dist_hub", new_dist, cal_msg)

        return True, f"dist_hub calibration: {new_dist}mm (was {old_dist}mm)", new_dist

    # ── RFID / Spoolman helpers ──
    def _sync_inventory(self) -> None:
        """
        Pull RFID/NFC filament data from ACE hardware into slot cache.

        V1 ACE only. The ACE 2 reads tags host-side over the MFRC522
        passthrough (AFC_ACE2_rfid), so it skips the firmware get_filament_info
        sweep entirely (_uses_firmware_rfid is False).
        """
        if not self._uses_firmware_rfid:
            return
        if (self._ace is None
            or not self._ace.connected):
            return
        for slot in range(self.SLOTS_PER_UNIT):
            try:
                info = self._ace.get_filament_info(slot)
                if isinstance(info, dict):
                    self._store_slot_rfid(slot, info)
            except Exception as e:
                self.logger.debug(f"ACE {self.name}: slot {slot} inventory query failed: {e}")

    def _store_slot_rfid(self, slot: int, info: Dict[str, Any]) -> None:
        """
        Store RFID fields from a get_filament_info response into slot cache.

        The FilamentInfo payload carries index, sku, brand, type, color[],
        extruder_temp{min,max}, hotbed_temp{min,max}, diameter, rfid and source,
        plus weight (total/current). It has no per-tag UID, so V1 identity comes
        from the SKU. The full raw dict is kept (and debug-logged).

        :param slot: 0-based ACE slot index to store data for.
        :param info: get_filament_info response dict for the slot.
        """
        inv = self._slot_inventory[slot]
        prev_raw = inv.get("raw")
        inv["raw"] = info
        changed = info != prev_raw
        if changed:
            self.logger.debug(f"ACE {self.name}: slot {slot} get_filament_info -> {info}")
        inv["material"] = info.get("material", info.get("type", ""))
        inv["color"] = info.get("color", [0, 0, 0])
        inv["sku"] = info.get("sku", "")
        inv["brand"] = info.get("brand", "")
        inv["diameter"] = info.get("diameter", 1.75)
        inv["total_weight"] = info.get("total", 0)
        inv["current_weight"] = info.get("current", 0)
        # RFID recognition state (RfidStatus):
        #   rfid:   0 not-found, 1 unrecognized, 2 recognized, 3 recognizing
        # 'source' is in the documented protocol but not returned by this firmware.
        inv["rfid"] = info.get("rfid")
        inv["source"] = info.get("source")
        # Keep the full min/max temperature ranges AND a derived single value.
        ext_temp = info.get("extruder_temp", {})
        bed_temp = info.get("hotbed_temp", {})
        if isinstance(ext_temp, dict):
            inv["extruder_temp_min"] = ext_temp.get("min")
            inv["extruder_temp_max"] = ext_temp.get("max")
            if (ext_temp.get("min")
                and ext_temp.get("max")):
                inv["extruder_temp"] = (ext_temp["min"] + ext_temp["max"]) // 2
            elif ext_temp.get("max"):
                inv["extruder_temp"] = ext_temp["max"]
            else:
                inv["extruder_temp"] = None
        else:
            inv["extruder_temp"] = None
            inv["extruder_temp_min"] = inv["extruder_temp_max"] = None
        if isinstance(bed_temp, dict):
            inv["bed_temp_min"] = bed_temp.get("min")
            inv["bed_temp_max"] = bed_temp.get("max")
            if (bed_temp.get("min")
                and bed_temp.get("max")):
                inv["bed_temp"] = (bed_temp["min"] + bed_temp["max"]) // 2
            elif bed_temp.get("max"):
                inv["bed_temp"] = bed_temp["max"]
            else:
                inv["bed_temp"] = None
        else:
            inv["bed_temp"] = inv["bed_temp_min"] = inv["bed_temp_max"] = None
        # Log a real tag read once per change, not every poll.
        if (changed
            and (inv.get("rfid") in (2, 3)
                 or inv.get("sku"))):
            self.logger.info(f"ACE {self.name}: slot {slot} RFID read, "
                f"sku={inv.get('sku')!r} brand={inv.get('brand')!r} "
                f"type={inv.get('material')!r} rfid={inv.get('rfid')} "
                f"nozzle={inv.get('extruder_temp_min')}-{inv.get('extruder_temp_max')}C")

    def _refresh_slot_inventory(self, slot: int) -> None:
        """
        Fetch fresh RFID data for a single slot from ACE hardware.
        (get_filament_info already returns current tag data on this firmware.)

        :param slot: 0-based ACE slot index to refresh.
        """
        if (self._ace is None
            or not self._ace.connected):
            return
        if (slot < 0
            or slot >= self.SLOTS_PER_UNIT):
            return
        try:
            info = self._ace.get_filament_info(slot)
            if isinstance(info, dict):
                self._store_slot_rfid(slot, info)
        except Exception as e:
            self.logger.debug(f"ACE {self.name}: slot {slot} RFID refresh failed: {e}")

    def _clear_slot_inventory(self, slot: int) -> None:
        """
        Clear cached RFID data for a slot.

        :param slot: 0-based ACE slot index to clear.
        """
        if 0 <= slot < self.SLOTS_PER_UNIT:
            self._slot_inventory[slot]["material"] = ""
            self._slot_inventory[slot]["color"] = [0, 0, 0]
            # Defensively drop any per-spool UID so a removed spool's can't linger.
            self._slot_inventory[slot].pop("uid", None)

    def _reader_sibling_slot(self, slot: int) -> Optional[int]:
        """
        Physical slot sharing this slot's RFID reader, or None. The base ACE
        assumes a per-slot reader (no sharing); subclasses whose readers cover a
        slot pair override this.

        :param slot: 0-based slot index
        :return Optional[int]: the sibling slot, always None here
        """
        return None

    def _shared_rfid_ambiguous(self, slot: int) -> bool:
        """
        True when a slot's static RFID read can't be trusted because its
        shared-reader sibling reports the same tag. Only a spinning read (on
        insert) can disambiguate, so a static startup read must not apply it.

        :param slot: 0-based slot index
        :return bool: True if the read is ambiguous
        """
        sib = self._reader_sibling_slot(slot)
        if (sib is None
            or not (0 <= sib < self.SLOTS_PER_UNIT)):
            return False
        me = self._slot_inventory[slot]
        other = self._slot_inventory[sib]
        sku = (me.get("sku") or "").strip()
        osku = (other.get("sku") or "").strip()
        if (sku
            and sku == osku):
            return True
        uid, ouid = me.get("uid"), other.get("uid")
        return bool(uid and uid == ouid)

    def _sync_slot_loaded_state(self) -> None:
        """
        Sync ACE slot status to lane load/prep state at startup.
        """
        if (self._ace is None
            or not self._ace.connected):
            return
        for lane in self.lanes.values():
            slot = self._get_slot(lane.name)
            if 0 <= slot < self.SLOTS_PER_UNIT:
                slot_info = self._slot_inventory[slot]
                slot_loaded = bool(slot_info and slot_info.get("status", "") == "ready")
                lane.prep_state = slot_loaded
                if not slot_loaded:
                    lane.loaded_to_hub = False
                    self._set_hub_state(lane, False)

                # A shared-reader slot's static read can be the neighbour's tag, so
                # leave identification to the persisted spool or an insert read.
                if self._shared_rfid_ambiguous(slot):
                    self.logger.info(f"ACE {self.name}: slot {slot} RFID read is ambiguous "
                                     f"(shared reader sibling reports the same tag), not "
                                     f"applying to {lane.name} at startup")
                    continue

                if apply_filament_defaults is not None:
                    apply_filament_defaults(
                        lane, slot_info,
                        color_converter=rgb_array_to_hex,
                        afc_defaults={
                            "default_material_type": self.afc.default_material_type,
                            "default_color": getattr(self.afc, "default_color", None),
                        })

                # V1 ACE does not create/match Spoolman (see on_filament_insert).

    # V1 ACE does not sync to Spoolman: its SKUs aren't unique per spool. ACE2
    # syncs by unique tag UID via AFC_ACE2_rfid.

    # Uncomment below once RFID and spoolman creation support is added
    # def _get_auto_spoolman_create(self, lane: AFCLane) -> bool:
    #     """
    #     Check if auto Spoolman spool creation is enabled for a lane.
    #
    #     :param lane: Lane to check.
    #     :return bool: True if auto-creation is enabled (per-lane, else the unit
    #         default).
    #     """
    #     if get_auto_spoolman_create is not None:
    #         return get_auto_spoolman_create(lane, self.auto_spoolman_create)
    #     return self.auto_spoolman_create




# ══════════════════════════════
# ACE serial communication layer (kept here so the ACE unit is one file).
# JSON-RPC over USB serial in binary frames with CRC-16/CCITT (reflected):
#   [0xFF 0xAA][len_le16][json][crc_le16][0xFE]
# Includes reconnection + health monitoring. ACEConnection and its errors
# are used by afcACE above.
# ══════════════════════════════

_logger = logging.getLogger("AFC_ACE_serial")

# Frame constants
FRAME_HEADER = b'\xff\xaa'
FRAME_FOOTER = b'\xfe'
MAX_PAYLOAD_SIZE = 1024
DEFAULT_BAUD = 115200
REQUEST_TIMEOUT = 5.0

# Reconnection constants
RECONNECT_BACKOFF_MIN = 5.0
RECONNECT_BACKOFF_MAX = 30.0
RECONNECT_BACKOFF_FACTOR = 1.5

# Health monitoring constants
HEARTBEAT_INTERVAL = 2.0  # Must be < 3s to prevent ACE USB autosuspend
COMM_SUPERVISION_WINDOW = 30.0
COMM_TIMEOUT_THRESHOLD = 15
COMM_UNSOLICITED_THRESHOLD = 15
SUPERVISION_CHECK_INTERVAL = 10.0


def crc16_ccitt_reflected(data: bytes) -> int:
    """
    Calculate CRC-16/CCITT-FALSE (reflected) checksum.

    Compatible with ACEPRO's _calc_crc implementation.
    Uses polynomial 0x8408 (bit-reflected 0x1021).

    :param data: Bytes to checksum.
    :return int: 16-bit CRC value.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc & 0xFFFF


# ACE auto-discovery: serial_port: auto + ace_index
# Chained V1 ACEs get ttyACMs that shuffle on re-enumeration, so a section binds to
# the unit whose get_info id (cable order) matches its ace_index, re-resolved on each
# connect. An explicit serial_port skips this.
ACE_BYID_GLOB = "/dev/serial/by-id/*ANYCUBIC*-if00"

# Real tty paths already claimed by a live ACEConnection, so autodetect never
# probes (double-opens) a port another unit is using. Keyed by realpath.
_ACE_CLAIMED_PORTS: Set[str] = set()


def _ace_scan_candidates() -> List[str]:
    """
    Anycubic ACE serial devices currently present, as real tty paths.

    :return List[str]: resolved tty paths, sorted by their by-id link
    """
    out: List[str] = []
    for p in sorted(glob.glob(ACE_BYID_GLOB)):
        try:
            out.append(os.path.realpath(p))
        except Exception:
            out.append(p)
    return out


def _ace_extract_get_info_id(buf: bytes) -> Optional[int]:
    """
    Pull result.id out of the first complete ACE frame in buf.

    :param buf: bytes read from the port so far
    :return Optional[int]: the unit id, or None if not found yet
    """
    i = buf.find(FRAME_HEADER)
    if (i < 0
        or len(buf) < i + 4):
        return None
    length = struct.unpack_from('<H', buf, i + 2)[0]
    end = i + 4 + length + 3           # header2 + len2 + payload + crc2 + footer1
    if len(buf) < end:
        return None
    payload = buf[i + 4:i + 4 + length]
    try:
        obj = json.loads(payload.decode('utf-8', 'replace'))
        res = obj.get('result') or {}
        rid = res.get('id')
        return int(rid) if rid is not None else None
    except Exception:
        return None


def probe_ace_get_info_id(port: str, baud: int = DEFAULT_BAUD,
                          timeout: float = 1.5) -> Optional[int]:
    """
    Open `port`, send one get_info, and return the firmware's self-reported
    unit id (result.id). Standalone (no reactor) so it can bind a section to
    a unit before the real connection exists. Never raises.

    :param port: serial device path to probe
    :param baud: baud rate to open it at
    :param timeout: seconds to wait for the reply
    :return Optional[int]: the unit id, or None
    """
    try:
        import serial as pyserial
    except ImportError:
        return None
    ser = None
    try:
        ser = pyserial.Serial(port=port, baudrate=baud, timeout=0.2, write_timeout=0.5)
        with contextlib.suppress(Exception):
            ser.reset_input_buffer()
        payload = json.dumps({'id': 0, 'method': 'get_info'}, separators=(',', ':')).encode('utf-8')
        crc = crc16_ccitt_reflected(payload)
        frame = (FRAME_HEADER + struct.pack('<H', len(payload)) + payload + struct.pack('<H', crc) +
                 FRAME_FOOTER)
        ser.write(frame)
        buf = b''
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = ser.read(256)
            if chunk:
                buf += chunk
                rid = _ace_extract_get_info_id(buf)
                if rid is not None:
                    return rid
            else:
                time.sleep(0.02)
        return None
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            if ser is not None:
                ser.close()


def resolve_ace_port_by_index(ace_index: int, baud: int = DEFAULT_BAUD, settle: float = 6.0,
                              logger: Optional[Any] = None) -> Optional[str]:
    """
    Return the tty whose ACE reports get_info id == ace_index, probing every
    Anycubic serial device over a settle window (to ride the ~3.5s watchdog
    re-enumeration). Ports already claimed by other live units are left alone.

    :param ace_index: 1-based unit id to look for
    :param baud: baud rate for the probe
    :param settle: seconds to keep rescanning for devices
    :param logger: optional logger for each probed port
    :return Optional[str]: the port, or None if no unit matched
    """
    deadline = time.time() + max(settle, 0.5)
    while True:
        for port in _ace_scan_candidates():
            if port in _ACE_CLAIMED_PORTS:
                continue
            rid = probe_ace_get_info_id(port, baud)
            if (rid is not None
                and logger is not None):
                logger.info(f"ACE autodetect: {port} reports id {rid}")
            if rid == ace_index:
                return port
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


class ACESerialError(Exception):
    """Raised when ACE serial communication fails."""
    pass


class ACETimeoutError(ACESerialError):
    """Raised when an ACE command times out."""
    pass


# Autodetect probes are blocking pyserial I/O that can take 8 s or more, long
# enough to lose MCU clock sync, so they run on a worker thread. One at a time,
# so two units cannot steal each other's replies on the same tty.
_ACE_AUTODETECT_LOCK = threading.Lock()
#: Longest the reactor waits for a worker autodetect before giving up on it.
AUTODETECT_WAIT_MAX = 60.0


def run_off_reactor(reactor: Any, fn: Any, what: str = "ACE autodetect",
                    timeout: float = AUTODETECT_WAIT_MAX) -> Any:
    """
    Run the blocking fn() on a worker thread and wait for it without
    blocking the reactor, returning its result or re-raising its exception.
    fn must not touch the reactor. Falls back to calling fn() inline on a
    reactor without register_async_callback.

    :param reactor: Klipper reactor to wait on
    :param fn: blocking callable to run
    :param what: description used in the timeout error
    :param timeout: seconds to wait for fn
    :return Any: fn's result
    :raises ACESerialError: when fn has not finished within timeout.
    """
    post = getattr(reactor, "register_async_callback", None)
    if post is None:
        return fn()
    completion = reactor.completion()
    box: Dict[str, Any] = {}

    def _worker() -> None:
        """
        Run fn under the autodetect lock and signal the waiting reactor.
        """
        try:
            with _ACE_AUTODETECT_LOCK:
                box["result"] = fn()
        except BaseException as e:
            box["error"] = e
        post(lambda eventtime: completion.complete(True))

    threading.Thread(target=_worker, name="ace-autodetect", daemon=True).start()
    if completion.wait(reactor.monotonic() + timeout) is None:
        error_str = f"{what} did not finish within {timeout:.0f}s"
        raise ACESerialError(error_str)
    if "error" in box:
        raise box["error"]
    return box.get("result")


class _HeldLog:
    """Logger stand-in for a worker thread: holds each line so the reactor can
    write it afterwards (AFC's logger also echoes to the console, which is
    not safe from another thread)."""

    def __init__(self) -> None:
        """
        Start with no held lines.
        """
        self._lines: List[Tuple[str, str]] = []

    def __getattr__(self, level: str) -> Any:
        """
        Return a logging method for ``level`` that holds the line.

        :param level: debug, info, warning or error
        :return Any: a callable taking the message
        """
        if level not in ("debug", "info", "warning", "error"):
            raise AttributeError(level)
        return lambda msg, *args, **kw: self._lines.append((level, msg))

    def replay(self, logger: Any) -> None:
        """
        Write the held lines to logger, oldest first.

        :param logger: logger to write the held lines to
        """
        for level, msg in self._lines:
            with contextlib.suppress(Exception):
                getattr(logger, level)(msg)
        self._lines.clear()


class ACEConnection:
    """Serial connection to a single ACE PRO hardware unit.

    Integrates with Klipper's reactor for non-blocking I/O. Commands are
    sent as JSON-RPC over a binary-framed USB serial protocol. Responses
    are matched by request ID and delivered via reactor completions.

    Features (modelled after Kobra-S1/ACEPRO serial_manager.py):
    - Automatic reconnection with exponential backoff
    - Periodic heartbeat to detect dead connections
    - Communication health supervision (timeout/unsolicited tracking)
    """

    def __init__(self, reactor: Reactor, serial_port: str, logger: Optional[Any] = None,
                 baud_rate: int = DEFAULT_BAUD, ace_index: int = 1,
                 ace_uid: Optional[Tuple[int, int, int]] = None) -> None:
        """
        Initialize the serial connection state (no port is opened yet).

        :param reactor: Klipper reactor used for fd registration, timers and
            command completions.
        :param serial_port: Path to the ACE serial device, or the literal
            string 'auto' to resolve the port by firmware id (see ace_index).
        :param logger: Optional logger; defaults to the module serial logger.
        :param baud_rate: Serial baud rate.
        :param ace_index: When serial_port is 'auto', bind to the unit whose
            get_info id equals this (1-based, cable order).
        :param ace_uid: optional STM32 UID pin (ACE2 only)
        """
        # 'auto' resolves to a real tty on every connect via the unit's
        # firmware id; _resolved_port holds the last-resolved path for logs.
        self._auto = (isinstance(serial_port, str) and serial_port.strip().lower() == "auto")
        self._ace_index = ace_index
        # Optional per-box pin (ACE2 only: the STM32 UID tuple from discover).
        self._ace_uid = ace_uid
        self._resolved_port: Optional[str] = None
        self._reactor = reactor
        self._serial_port = serial_port
        self._baud_rate = baud_rate
        self._logger = logger or _logger

        self._serial: Optional[Any] = None
        self._fd_handle = None
        self._connected = False
        # True while connect() waits for autodetect on its worker thread.
        self._connecting = False

        # Request tracking
        self._next_request_id = 0
        self._pending: Dict[int, Any] = {}  # request_id -> reactor.completion()
        # request_id -> command opcode, used by the V2 transport to reject a
        # reply whose opcode doesn't match the pending request (id-reuse guard).
        # Unused by V1 (full-width ids don't recycle).
        self._pending_cmd: Dict[int, int] = {}
        # Async request IDs we've sent, whose replies go to status_callback.
        # Bounded so lost responses can't grow it forever.
        self._async_ids: collections.deque[int] = collections.deque(maxlen=256)

        # Read buffer for frame reassembly
        self._read_buffer = b''

        # Device info cache (populated on connect)
        self.device_info: Dict[str, Any] = {}
        self.slot_count = 4  # ACE PRO always has 4 slots

        # Reconnection state
        self._reconnect_timer = None
        self._reconnect_backoff = RECONNECT_BACKOFF_MIN
        self._reconnect_enabled = True

        # Heartbeat state
        self._heartbeat_timer = None
        self._last_rx_time = 0.0

        # Health supervision
        self._timeout_timestamps: List[float] = []
        self._unsolicited_timestamps: List[float] = []
        self._last_supervision_check = 0.0

        # Callback for status updates (set by unit)
        self.status_callback: Optional[Callable[[Dict[str, Any]], None]] = None

        # Called after a successful reconnect (not initial connect)
        self.reconnect_callback: Optional[Callable[[], None]] = None

    @property
    def connected(self) -> bool:
        """
        Whether the serial link is up.

        :return bool: True while the serial port is open and registered.
        """
        return self._connected

    def _pre_info_handshake(self) -> None:
        """
        Hook run on connect, just before the get_info query. No-op for V1;
        ACE2Connection overrides it to send the V2 discover handshake.
        """
        pass

    def connect(self) -> None:
        """
        Open the serial port and register with Klipper's reactor for reads.
        """
        if self._connected:
            return

        try:
            import serial as pyserial
        except ImportError:
            error_str = "pyserial not installed. Install with: pip install pyserial"
            raise ACESerialError(error_str)

        # Resolve serial_port: auto on every (re)connect, off the reactor; a
        # second connect arriving while one waits is refused.
        if self._connecting:
            error_str = "ACE connect already in progress"
            raise ACESerialError(error_str)
        self._connecting = True
        held = _HeldLog()
        try:
            port = run_off_reactor(self._reactor, lambda: self._resolve_connect_port(held))
        finally:
            self._connecting = False
            held.replay(self._logger)
        if self._connected:
            return

        try:
            self._serial = pyserial.Serial(
                port=port,
                baudrate=self._baud_rate,
                timeout=0,           # Non-blocking reads
                write_timeout=0.5,
            )
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except Exception as e:
            error_str = f"Failed to open ACE serial port {port}: {e}"
            raise ACESerialError(error_str)

        # Claim the resolved tty so another unit's autodetect never double-opens
        # it. Tracked by realpath; released in disconnect().
        self._resolved_port = port
        with contextlib.suppress(Exception):
            _ACE_CLAIMED_PORTS.add(os.path.realpath(port))

        # Register fd with reactor for incoming data
        self._fd_handle = self._reactor.register_fd(self._serial.fileno(), self._handle_read)
        self._connected = True
        self._last_rx_time = self._reactor.monotonic()
        self._logger.info(f"ACE serial connected: {port} @ {self._baud_rate}" +
                          (f" (auto, ace_index {self._ace_index})" if self._auto else ""))

        # Query device info (V2 needs a discover handshake first, see the
        # ACE2Connection._pre_info_handshake override; the base is a no-op).
        try:
            self._pre_info_handshake()
            self.device_info = self.send_command("get_info", timeout=3.0)
            self._logger.info(f"ACE device info: {self.device_info}")
        except Exception as e:
            self._logger.debug(f"ACE get_info failed (non-fatal): {e}")

        # Reset backoff on successful connect
        self._reconnect_backoff = RECONNECT_BACKOFF_MIN

        # Start heartbeat
        self._start_heartbeat()

    def _resolve_connect_port(self, logger: Optional[Any] = None) -> str:
        """
        The tty to open. Explicit serial_port is returned as-is; 'auto'
        resolves to the unit whose firmware id == ace_index. Runs on a worker
        thread (see connect), so it logs to `logger`, not the reactor's.

        Fast path on reconnect: re-check the last-resolved port first (one quick
        probe) so the common case doesn't run the full settle-window scan; only
        fall back to the scan if that unit has moved/renumbered.

        :param logger: logger to use from the worker thread
        :return str: the serial port to open
        """
        if not self._auto:
            return self._serial_port
        if self._resolved_port:
            with contextlib.suppress(Exception):
                if probe_ace_get_info_id(self._resolved_port, self._baud_rate) == self._ace_index:
                    return self._resolved_port
        port = resolve_ace_port_by_index(self._ace_index, self._baud_rate,
                                         logger=logger or self._logger)
        if port is None:
            error_str = (f"ACE autodetect: no unit reporting id {self._ace_index} "
                         f"found (serial_port: auto). Is it plugged in / powered?")
            raise ACESerialError(error_str)
        return port

    def disconnect(self) -> None:
        """
        Close serial port and clean up reactor registrations.
        """
        was_connected = self._connected
        self._connected = False

        # Release the claimed tty so autodetect can probe it again.
        if self._resolved_port is not None:
            with contextlib.suppress(Exception):
                _ACE_CLAIMED_PORTS.discard(os.path.realpath(self._resolved_port))

        self._stop_heartbeat()

        if self._fd_handle is not None:
            try:
                self._reactor.unregister_fd(self._fd_handle)
            except Exception:
                pass
            self._fd_handle = None

        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

        # Fail any pending requests
        for req_id, completion in list(self._pending.items()):
            try:
                completion.complete(None)
            except Exception:
                pass
        self._pending.clear()
        self._pending_cmd.clear()
        self._async_ids.clear()
        self._read_buffer = b''

        if was_connected:
            self._logger.info("ACE serial disconnected")

    def reconnect(self) -> None:
        """
        Disconnect and schedule automatic reconnection.
        """
        if not self._reconnect_enabled:
            return

        self.disconnect()

        delay = self._reconnect_backoff
        self._reconnect_backoff = min(self._reconnect_backoff * RECONNECT_BACKOFF_FACTOR,
                                      RECONNECT_BACKOFF_MAX)

        self._logger.info(f"ACE scheduling reconnect in {delay:.0f}s "
                          f"(next backoff: {self._reconnect_backoff:.0f}s)")

        def _reconnect_callback(eventtime: float) -> float:
            """
            Reactor timer: attempt reconnect, firing reconnect_callback on
            success or rescheduling with backoff on failure.

            :param eventtime: Reactor event time.
            :return float: reactor.NEVER when done/disabled, else the next retry time.
            """
            if not self._reconnect_enabled:
                return self._reactor.NEVER
            try:
                self.connect()
                self._logger.info("ACE reconnected successfully")
                if self.reconnect_callback:
                    try:
                        self.reconnect_callback()
                    except Exception as e:
                        self._logger.warning(f"ACE reconnect callback failed: {e}")
                return self._reactor.NEVER
            except Exception as e:
                self._logger.warning(f"ACE reconnect failed: {e}")
                next_delay = self._reconnect_backoff
                self._reconnect_backoff = min(self._reconnect_backoff * RECONNECT_BACKOFF_FACTOR,
                                              RECONNECT_BACKOFF_MAX)
                return eventtime + next_delay

        if self._reconnect_timer is not None:
            try:
                self._reactor.unregister_timer(self._reconnect_timer)
            except Exception:
                pass

        self._reconnect_timer = self._reactor.register_timer(_reconnect_callback,
                                                              self._reactor.monotonic() + delay)

    def _quick_reconnect(self) -> None:
        """
        Fast reconnect for USB autosuspend - no backoff delay.
        """
        if not self._reconnect_enabled:
            return

        self.disconnect()

        def _quick_reconnect_cb(eventtime: float) -> float:
            """
            Reactor timer: fast reconnect after USB autosuspend, falling back
            to the backoff reconnect on failure.

            :param eventtime: Reactor event time.
            :return float: Always reactor.NEVER.
            """
            if not self._reconnect_enabled:
                return self._reactor.NEVER
            try:
                self.connect()
                self._logger.debug("ACE quick reconnect succeeded")
                if self.reconnect_callback:
                    try:
                        self.reconnect_callback()
                    except Exception as e:
                        self._logger.warning(f"ACE reconnect callback failed: {e}")
                return self._reactor.NEVER
            except Exception as e:
                self._logger.warning(f"ACE quick reconnect failed: {e}")
                # Fall back to normal reconnect with backoff
                self.reconnect()
                return self._reactor.NEVER

        if self._reconnect_timer is not None:
            try:
                self._reactor.unregister_timer(self._reconnect_timer)
            except Exception:
                pass

        # Reconnect after just 0.5s - enough for USB to re-enumerate
        self._reconnect_timer = self._reactor.register_timer(_quick_reconnect_cb,
                                                              self._reactor.monotonic() + 0.5)

    def send_command(self, method: str, params: Optional[Dict[str, Any]] = None,
                     timeout: float = REQUEST_TIMEOUT) -> Any:
        """
        Send a JSON-RPC command and wait for the response.

        Uses Klipper's reactor completion mechanism to block the calling
        greenlet while still allowing other reactor events to be processed.

        :param method: ACE protocol method name.
        :param params: Optional params dict for the request.
        :param timeout: Seconds to wait for the matching response.
        :return Any: The response's ``result`` payload (or the full dict).
        :raises ACESerialError: On write failure or an error-code reply.
        :raises ACETimeoutError: When no response arrives before timeout.
        """
        if (not self._connected
            or self._serial is None):
            error_str = "ACE not connected"
            raise ACESerialError(error_str)

        request_id = self._next_request_id
        self._next_request_id += 1

        request = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params

        payload = json.dumps(request, separators=(',', ':')).encode('utf-8')
        if len(payload) > MAX_PAYLOAD_SIZE:
            error_str = f"ACE payload too large ({len(payload)} > {MAX_PAYLOAD_SIZE})"
            raise ACESerialError(error_str)

        completion = self._reactor.completion()
        self._pending[request_id] = completion

        frame = self._build_frame(payload)
        try:
            self._serial.write(frame)
            self._serial.flush()
        except Exception as e:
            self._pending.pop(request_id, None)
            self._track_timeout()
            # A write failure is a strong disconnect signal (cable/USB drop),
            # trigger reconnect now instead of limping until the ~8s
            # heartbeat-silence check notices.
            self.reconnect()
            error_str = f"ACE write failed: {e}"
            raise ACESerialError(error_str)

        self._logger.debug(f"ACE TX: {request}")

        # try/finally so the pending entry is always removed, even if wait() is
        # interrupted.
        deadline = self._reactor.monotonic() + timeout
        try:
            result = completion.wait(deadline)
        finally:
            self._pending.pop(request_id, None)

        if result is None:
            self._track_timeout()
            error_str = f"ACE command '{method}' (id={request_id}) timed out after {timeout}s"
            raise ACETimeoutError(error_str)

        # Check for error response
        if isinstance(result, dict):
            code = result.get("code", 0)
            msg = result.get("msg", "")
            if (code != 0
                or (msg
                    and msg.upper() == "FORBIDDEN")):
                if not msg:
                    msg = "unknown error"
                error_str = f"ACE command '{method}' failed: code={code}, msg={msg}"
                raise ACESerialError(error_str)
            return result.get("result", result)

        return result

    def send_command_async(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """
        Send a JSON-RPC command without waiting for a response.

        :param method: ACE protocol method name.
        :param params: Optional params dict for the request.
        """
        if (not self._connected
            or self._serial is None):
            return

        request_id = self._next_request_id
        self._next_request_id += 1
        self._async_ids.append(request_id)

        request = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params

        payload = json.dumps(request, separators=(',', ':')).encode('utf-8')
        frame = self._build_frame(payload)

        try:
            self._serial.write(frame)
            self._serial.flush()
        except Exception as e:
            self._logger.debug(f"ACE async write failed: {e}")
            # Same disconnect signal as the sync path, reconnect rather than
            # wait for the heartbeat-silence timeout.
            self.reconnect()
            return

        self._logger.debug(f"ACE TX (async): {request}")

    # ---- Heartbeat ----

    def _start_heartbeat(self) -> None:
        """
        Start periodic heartbeat to detect dead connections.
        """
        if self._heartbeat_timer is not None:
            return
        self._heartbeat_timer = self._reactor.register_timer(self._heartbeat_tick,
            self._reactor.monotonic() + HEARTBEAT_INTERVAL)

    def _stop_heartbeat(self) -> None:
        """
        Stop heartbeat timer.
        """
        if self._heartbeat_timer is not None:
            try:
                self._reactor.unregister_timer(self._heartbeat_timer)
            except Exception:
                pass
            self._heartbeat_timer = None

    def _heartbeat_tick(self, eventtime: float) -> float:
        """
        Send periodic get_status to verify connection is alive.

        :param eventtime: Reactor event time.
        :return float: reactor.NEVER if disconnected/reconnecting, else the next tick
            time.
        """
        if not self._connected:
            return self._reactor.NEVER

        # Check if we've received anything recently
        silence = eventtime - self._last_rx_time
        if silence > HEARTBEAT_INTERVAL * 4:
            self._logger.warning(f"ACE no data received for {silence:.0f}s, reconnecting")
            self.reconnect()
            return self._reactor.NEVER

        # Send heartbeat status request
        try:
            self.send_command_async("get_status")
        except Exception:
            pass

        # Transport-specific extra polls (ACE2 caches get_temp here); no-op on V1.
        try:
            self._poll_extras()
        except Exception:
            pass

        # Run health supervision check
        self._supervision_check()

        return eventtime + HEARTBEAT_INTERVAL

    def _poll_extras(self) -> None:
        """
        Hook for transport-specific periodic polls issued from the heartbeat.
        Base connection has nothing extra to poll; ACE2 overrides this to cache
        GET_TEMP. Kept fire-and-forget so the heartbeat never blocks.
        """
        pass

    # ---- Health Supervision ----

    def _track_timeout(self) -> None:
        """
        Record a timeout event.
        """
        now = self._reactor.monotonic()
        self._timeout_timestamps.append(now)
        cutoff = now - COMM_SUPERVISION_WINDOW
        self._timeout_timestamps = [t for t in self._timeout_timestamps if t > cutoff]

    def _track_unsolicited(self) -> None:
        """
        Record an unsolicited message event.
        """
        now = self._reactor.monotonic()
        self._unsolicited_timestamps.append(now)
        cutoff = now - COMM_SUPERVISION_WINDOW
        self._unsolicited_timestamps = [t for t in self._unsolicited_timestamps if t > cutoff]

    def _supervision_check(self) -> None:
        """
        Check communication health; force reconnect if degraded.
        """
        now = self._reactor.monotonic()
        if now - self._last_supervision_check < SUPERVISION_CHECK_INTERVAL:
            return
        self._last_supervision_check = now

        if not self._connected:
            return

        cutoff = now - COMM_SUPERVISION_WINDOW
        self._timeout_timestamps = [t for t in self._timeout_timestamps if t > cutoff]
        self._unsolicited_timestamps = [t for t in self._unsolicited_timestamps if t > cutoff]

        timeout_count = len(self._timeout_timestamps)
        unsolicited_count = len(self._unsolicited_timestamps)

        if (timeout_count >= COMM_TIMEOUT_THRESHOLD
            and unsolicited_count >= COMM_UNSOLICITED_THRESHOLD):
            self._logger.warning(f"ACE communication unhealthy: {timeout_count} timeouts + "
                                 f"{unsolicited_count} unsolicited in {COMM_SUPERVISION_WINDOW}s, "
                                 "forcing reconnect")
            self._timeout_timestamps.clear()
            self._unsolicited_timestamps.clear()
            self.reconnect()

    # ---- Frame Protocol ----

    def _build_frame(self, payload: bytes) -> bytes:
        """
        Build a binary frame wrapping the JSON payload.

        :param payload: JSON-encoded request bytes.
        :return bytes: Header + length + payload + CRC + footer frame.
        """
        length = len(payload)
        crc = crc16_ccitt_reflected(payload)
        return (FRAME_HEADER + struct.pack('<H', length) + payload + struct.pack('<H', crc) +
                FRAME_FOOTER)

    def _handle_read(self, eventtime: float) -> None:
        """
        Reactor callback invoked when data is available on the serial fd.

        Reads available bytes, appends them to the reassembly buffer and parses
        complete frames; handles USB autosuspend/disconnect by reconnecting.

        :param eventtime: Reactor event time (records last receive time).
        """
        if self._serial is None:
            return
        try:
            data = self._serial.read(4096)
        except Exception as e:
            err_msg = str(e)
            if ("returned no data" in err_msg
                or "disconnected" in err_msg):
                # USB autosuspend - device briefly disappeared.
                # Quick reconnect without backoff since it's not a real failure.
                self._logger.debug("ACE USB idle disconnect detected, quick reconnect")
                self._reconnect_backoff = RECONNECT_BACKOFF_MIN
                self._quick_reconnect()
            else:
                self._logger.error(f"ACE read error: {e}")
                self.reconnect()
            return

        if not data:
            return

        self._last_rx_time = eventtime
        self._read_buffer += data
        self._parse_frames()

    # Maximum sane payload size to reject false headers with garbage lengths
    _MAX_PAYLOAD = 8192

    def _parse_frames(self) -> None:
        """
        Extract and process complete frames from the read buffer.
        """
        while True:
            header_pos = self._read_buffer.find(FRAME_HEADER)
            if header_pos < 0:
                self._read_buffer = b''
                return
            if header_pos > 0:
                self._read_buffer = self._read_buffer[header_pos:]

            if len(self._read_buffer) < 7:
                return

            payload_length = struct.unpack_from('<H', self._read_buffer, 2)[0]

            # Reject impossibly large payloads (likely a false header in data)
            if payload_length > self._MAX_PAYLOAD:
                self._logger.debug(f"ACE frame: payload length {payload_length} exceeds max, "
                                   "skipping false header")
                self._read_buffer = self._read_buffer[2:]
                continue

            frame_size = 2 + 2 + payload_length + 2 + 1
            if len(self._read_buffer) < frame_size:
                return

            payload = self._read_buffer[4:4 + payload_length]
            crc_received = struct.unpack_from('<H', self._read_buffer, 4 + payload_length)[0]
            footer = self._read_buffer[4 + payload_length + 2]

            # Validate footer and CRC BEFORE consuming the frame.
            # On failure, skip past just the header bytes and rescan,
            # since a false header in data would produce garbage framing.
            if footer != FRAME_FOOTER[0]:
                self._logger.debug(f"ACE frame: invalid footer byte 0x{footer:02x}, "
                                   "rescanning for next header")
                self._read_buffer = self._read_buffer[2:]
                continue

            crc_calculated = crc16_ccitt_reflected(payload)
            if crc_received != crc_calculated:
                self._logger.debug(f"ACE frame: CRC mismatch (recv=0x{crc_received:04x}, "
                                   f"calc=0x{crc_calculated:04x}), rescanning")
                self._read_buffer = self._read_buffer[2:]
                continue

            # Frame is valid - consume it from the buffer
            self._read_buffer = self._read_buffer[frame_size:]

            try:
                response = json.loads(payload.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._logger.warning(f"ACE frame: JSON parse error: {e}")
                continue

            self._logger.debug(f"ACE RX: {response}")
            self._handle_response(response)

    def _response_matches_pending(self, response_id: Any, response: Dict[str, Any]) -> bool:
        """
        Whether a reply keyed to a pending request id is genuinely that
        request's reply. V1's JSON ids span the full 32-bit space and don't
        recycle in any realistic window, so a matched id is always the right
        reply. The V2 transport (16-bit wire ids) overrides this to also require
        the response opcode to match the request's, guarding the id-reuse race.

        :param response_id: the id echoed by the unit
        :param response: the decoded response
        :return bool: always True for V1
        """
        return True

    def _handle_response(self, response: Dict[str, Any]) -> None:
        """
        Route a parsed response to its pending request completion.

        Unsolicited notifications and async/heartbeat replies are forwarded to
        status_callback; id-matched replies complete their pending request.

        :param response: Parsed JSON response dict from the ACE.
        """
        response_id = response.get("id")

        if response_id is None:
            # Unsolicited notification
            self._track_unsolicited()
            if self.status_callback:
                try:
                    self.status_callback(response)
                except Exception as e:
                    # Never silent: a runout's TOOL_UNLOAD chain runs under this.
                    self._logger.error(f"ACE status_callback error: {e}\n"
                                       f"{traceback.format_exc()}")
            return

        completion = self._pending.get(response_id)
        if completion is not None:
            if not self._response_matches_pending(response_id, response):
                # id matched a pending request but the reply is for a different
                # command, a stale/late reply landing on a reused id. Drop it
                # rather than complete the request with the wrong data.
                self._logger.debug(f"ACE dropping mismatched reply for id={response_id}")
                return
            try:
                completion.complete(response)
            except Exception:
                pass
            return

        # No pending completion, check if this matches an async request
        # we fired earlier (heartbeat, etc.). Those are expected.
        try:
            self._async_ids.remove(response_id)
            is_async = True
        except ValueError:
            is_async = False

        # Only replies not matching an async request count as unsolicited, or
        # heartbeat replies would trip the health check on a healthy link.
        if not is_async:
            self._track_unsolicited()

        # Forward to the status callback so the unit can process slot data
        # in near-real-time.
        if self.status_callback:
            try:
                self.status_callback(response)
            except Exception as e:
                # Never silent: see the note on the unsolicited path above.
                self._logger.error(f"ACE status_callback error: {e}\n" f"{traceback.format_exc()}")

        if not is_async:
            self._logger.debug(f"ACE response for unknown request id={response_id}")

    # ---- High-Level Hardware Commands ----

    def get_status(self, timeout: float = 3.0) -> Any:
        """
        Query current device status (temperatures, sensors, slots).

        :param timeout: Seconds to wait for the response.
        :return Any: The status result payload.
        """
        return self.send_command("get_status", timeout=timeout)

    def get_temp(self, timeout: float = 3.0) -> Any:
        """
        Query the unit's temperature/humidity channels.

        On the ACE2 this maps to the GET_TEMP opcode and returns the box/PTC/env
        temperatures plus humidity; the V1 ACE firmware has no such method and
        will report it as unsupported.

        :param timeout: Seconds to wait for the response.
        :return Any: The temperature result payload.
        """
        return self.send_command("get_temp", timeout=timeout)

    def get_filament_info(self, slot_index: int, timeout: float = 3.0) -> Any:
        """
        Get RFID/NFC filament data for a specific slot (0-based).

        :param slot_index: 0-based slot index.
        :param timeout: Seconds to wait for the response.
        :return Any: The filament-info result payload.
        """
        return self.send_command("get_filament_info", params={"index": slot_index}, timeout=timeout)

    def get_material_info(self, slot_index: int, timeout: float = 3.0) -> Any:
        """
        Read a slot's stored material name + status (ACE2 GET_MATERIAL_INFO).

        This is the name written by set_material_name and persisted to the
        unit's NVM, distinct from the live RFID tag read (get_filament_info).

        :param slot_index: 0-based slot index.
        :param timeout: Seconds to wait for the response.
        :return Any: The material-info result payload.
        """
        return self.send_command("get_material_info", params={"index": slot_index}, timeout=timeout)

    def set_material_name(self, slot_index: int, name: str, timeout: float = 3.0) -> Any:
        """
        Write a slot's stored material name (ACE2 SET_MATERIAL_NAME).

        Persists to the unit's NVM and clears the slot status byte.
        Variable-length names are accepted.

        :param slot_index: 0-based slot index.
        :param name: Material-name string to store.
        :param timeout: Seconds to wait for the response.
        :return Any: The command result payload.
        """
        return self.send_command("set_material_name", params={"index": slot_index, "name": name},
                                 timeout=timeout)

    def get_sensor_state(self, timeout: float = 3.0) -> Any:
        """
        Read the ACE2 17-channel filament-sensor bitmask (GET_SENSOR_STATE).

        :param timeout: Seconds to wait for the response.
        :return Any: A dict with ``sensor_bitmask`` and a per-channel ``sensors``
            boolean list.
        """
        return self.send_command("get_sensor_state", timeout=timeout)

    def feed_filament(self, slot_index: int, length_mm: float, speed_mm_s: float) -> Any:
        """
        Feed filament forward from the specified slot.

        :param slot_index: Slot index (0-based)
        :param length_mm: Distance to feed in mm
        :param speed_mm_s: Feed speed in mm/s (ACE firmware native unit)
        :return Any: The response result payload.
        """
        return self.send_command(
            "feed_filament",
            params={
                "index": slot_index,
                "length": length_mm,
                "speed": speed_mm_s,
            },
            timeout=max(REQUEST_TIMEOUT, (length_mm / max(speed_mm_s, 1)) + 10),
        )

    def stop_feed_filament(self, slot_index: int) -> None:
        """
        Stop an active feed operation on the specified slot.

        :param slot_index: 0-based slot index.
        """
        self.send_command_async("stop_feed_filament", params={"index": slot_index})

    def unwind_filament(self, slot_index: int, length_mm: float, speed_mm_s: float,
                        mode: str = "normal") -> Any:
        """
        Retract/unwind filament back into the specified slot.

        :param slot_index: Slot index (0-based)
        :param length_mm: Distance to retract in mm
        :param speed_mm_s: Retract speed in mm/s (ACE firmware native unit)
        :param mode: Unwind mode ("normal" or other firmware-supported modes)
        :return Any: The response result payload.
        """
        return self.send_command(
            "unwind_filament",
            params={
                "index": slot_index,
                "length": length_mm,
                "speed": speed_mm_s,
                "mode": mode,
            },
            timeout=max(REQUEST_TIMEOUT, (length_mm / max(speed_mm_s, 1)) + 10),
        )

    def stop_unwind_filament(self, slot_index: int) -> None:
        """
        Stop an active unwind/retract operation.

        :param slot_index: 0-based slot index.
        """
        self.send_command_async("stop_unwind_filament", params={"index": slot_index})

    def start_feed_assist(self, slot_index: int, timeout: float = 2.0) -> Any:
        """
        Enable continuous motorized feed assist for a slot and wait for ACK.

        :param slot_index: 0-based slot index.
        :param timeout: Seconds to wait for the ACK.
        :return Any: The response result payload.
        """
        return self.send_command("start_feed_assist", params={"index": slot_index}, timeout=timeout)

    def stop_feed_assist(self, slot_index: int) -> None:
        """
        Disable feed assist for a slot.

        :param slot_index: 0-based slot index.
        """
        self.send_command_async("stop_feed_assist", params={"index": slot_index})

    def stop_feed_assist_sync(self, slot_index: int, timeout: float = 2.0) -> Any:
        """
        Disable feed assist for a slot and wait for ACK.

        :param slot_index: 0-based slot index.
        :param timeout: Seconds to wait for the ACK.
        :return Any: The response result payload.
        """
        return self.send_command("stop_feed_assist", params={"index": slot_index}, timeout=timeout)

    def update_feeding_speed(self, slot_index: int, speed_mm_s: float) -> None:
        """
        Update the speed of an active feed operation.

        Speed is mm/s, matching feed_filament/unwind_filament, not mm/min.

        :param slot_index: 0-based slot index.
        :param speed_mm_s: New feed speed in mm/s.
        """
        self.send_command_async("update_feeding_speed",
                                params={"index": slot_index, "speed": speed_mm_s})

    def update_unwinding_speed(self, slot_index: int, speed_mm_s: float) -> None:
        """
        Update the speed of an active unwind operation.

        Speed is mm/s (see update_feeding_speed).

        :param slot_index: 0-based slot index.
        :param speed_mm_s: New unwind speed in mm/s.
        """
        self.send_command_async(
            "update_feeding_speed",   # UPDATE_SPEED opcode; direction is per-slot
            params={"index": slot_index, "speed": speed_mm_s},
        )

    def start_drying(self, temp_c: float, fan_speed_rpm: int, duration_min: float) -> Any:
        """
        Start a filament drying cycle.
        NOTE: the firmware ignores fan_speed and runs the fan fixed at 7000.

        :param temp_c: Target dryer temperature in degrees Celsius.
        :param fan_speed_rpm: Requested fan speed (ignored by firmware).
        :param duration_min: Drying duration in minutes.
        :return Any: The response result payload.
        """
        return self.send_command(
            "drying",
            params={
                "temp": temp_c,
                "fan_speed": fan_speed_rpm,
                "duration": duration_min,
            },
        )

    def stop_drying(self) -> Any:
        """
        Stop an active drying cycle.

        :return Any: The response result payload.
        """
        return self.send_command("drying_stop")

    def enable_rfid(self) -> Any:
        """
        Enable RFID/NFC tag detection.

        :return Any: The response result payload.
        """
        return self.send_command("enable_rfid")

    def disable_rfid(self) -> Any:
        """
        Disable RFID/NFC tag detection.

        :return Any: The response result payload.
        """
        return self.send_command("disable_rfid")

    # ---- Commands from Anycubic's ACE protocol ----
    # set_filament_info works (code 0); other protocol methods return 400
    # InvalidCommand on this firmware, so probe with AFC_ACE_CMD before adding wrappers.

    def set_filament_info(self, slot_index: int, filament_type: str, color_rgb: Any) -> Any:
        """
        Write filament type + colour to a slot (SetFilamentInfo). Accepted by
        the firmware (code 0) but has no effect on an empty slot
        (get_filament_info still reads blank). Not wired into any flow.

        :param slot_index: 0-based slot index.
        :param filament_type: Material type string to write.
        :param color_rgb: [r, g, b] (0-255).
        :return Any: The response result payload.
        """
        return self.send_command("set_filament_info", params={"index": slot_index,
                                 "type": filament_type, "color": list(color_rgb)})


def load_config_prefix(config: ConfigWrapper) -> afcACE:
    """
    Klipper entry point: construct the ACE unit from a config section.

    :param config: ConfigWrapper for the ``[AFC_ACE ...]`` section.
    :return afcACE: The configured ACE unit instance.
    """
    return afcACE(config)
