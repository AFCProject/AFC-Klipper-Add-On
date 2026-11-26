# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback

from configparser import Error as error
from configfile import error as config_error

from typing import TYPE_CHECKING, Any, Dict, List, Optional

try: from extras.AFC_utils import ERROR_STR
except:
    trace = traceback.format_exc()
    err_str = f"Error when trying to import AFC_utils.ERROR_STR\n{trace}"
    raise error(err_str)

try: from extras.AFC import State
except: raise error(ERROR_STR.format(import_lib="AFC", trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCMoveWarning, MoveDirection
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try: from extras.AFC_BoxTurtle import afcBoxTurtle
except: raise error(ERROR_STR.format(import_lib="AFC_BoxTurtle", trace=traceback.format_exc()))

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from gcode import GCodeCommand
    from extras.AFC_lane import AFCLane
    from extras.AFC_utils import AFCGcmd

# QIDI's own box config caps the dryer target at 90°C (`target_max_temp: 90` in box1.cfg)
QIDI_DRYER_TARGET_MAX_TEMP = 90.0


class afcQidiBox(afcBoxTurtle):
    # The stock box has only one sensor per slot, so a lane without prep uses its slot sensor
    # as load and the lane is ready as soon as filament is detected there
    load_only_without_prep = True

    def __init__(self, config: ConfigWrapper) -> None:
        """
        Initialize the QIDI Box unit, reading its type and dryer settings from config.

        :param config: Klipper config wrapper for the AFC_QidiBox section
        """
        super().__init__(config)
        self.type: str = config.get('type', 'QIDI_Box')
        # Prep, eject and fallback loads give up after this many short_move_dis moves. Raise it
        # when the lane load sensor sits far from the box, e.g. a printer side sensor.
        self.MAX_NUM_MOVES = config.getint('max_short_moves', afcBoxTurtle.MAX_NUM_MOVES,
                                           minval=1)
        # Hub mode: after a new spool reaches the hub it is parked this far before it, which
        # keeps the shared hub free for the other lanes
        self.preload_park_dist: float = config.getfloat('preload_park_dist', 80.0, minval=0.0)

        self.dryer_heater_name: Optional[str] = config.get('dryer_heater', None)
        self.dryer_humidity_sensor_name: Optional[str] = config.get('dryer_humidity_sensor',
                                                                    None)
        self.dryer_max_temp: float = config.getfloat('dryer_max_temp', 65.0, minval=0.0,
                                                     maxval=QIDI_DRYER_TARGET_MAX_TEMP)
        self.dryer_default_temp: float = config.getfloat('dryer_default_temp', 50.0,
                                                         above=0.0)
        self.dryer_default_duration: float = config.getfloat('dryer_default_duration', 240.0,
                                                             above=0.0)
        self.dry_rotate_length: float = config.getfloat('dry_rotate_length', 50.0, above=0.0)
        self.dry_rotate_speed: float = config.getfloat('dry_rotate_speed', 80.0, above=0.0)
        self.dry_rotate_interval: float = config.getfloat('dry_rotate_interval', 300.0,
                                                          minval=30.0)
        if self.dryer_default_temp > self.dryer_max_temp:
            error_str = (f"dryer_default_temp ({self.dryer_default_temp}) in "
                         f"[{config.get_name()}] cannot be above dryer_max_temp "
                         f"({self.dryer_max_temp})")
            raise config_error(error_str)

        self.dryer_heater: Any = None
        self.dryer_humidity_sensor: Any = None
        self._dry_timer: Any = None
        self._dry_target: float = 0.0
        self._dry_end_time: float = 0.0
        self._dry_next_rotate: Optional[float] = None

    def handle_connect(self) -> None:
        """
        Handle the connection event: set the logos, look up the dryer objects and register the
        dryer commands when a dryer heater is configured.
        """
        super().handle_connect()

        self.logo = '<span class=success--text>QIDI Box Ready</span>\n'
        self.logo += f'  {self.name}\n'
        self.logo_error = '<span class=error--text>QIDI Box Not Ready</span>\n'
        self.logo_error += f'  {self.name}\n'

        if self.dryer_heater_name is None:
            return
        self._lookup_dryer()
        self.afc.function.register_mux_command(self.afc.show_macros, 'AFC_QIDIBOX_DRY', "UNIT",
                                               self.name, self.cmd_AFC_QIDIBOX_DRY,
                                               self.cmd_AFC_QIDIBOX_DRY_help,
                                               self.cmd_AFC_QIDIBOX_DRY_options)
        self.afc.function.register_mux_command(self.afc.show_macros, 'AFC_QIDIBOX_DRY_STOP',
                                               "UNIT", self.name, self.cmd_AFC_QIDIBOX_DRY_STOP,
                                               self.cmd_AFC_QIDIBOX_DRY_STOP_help,
                                               self.cmd_AFC_QIDIBOX_DRY_STOP_options)

    def _lookup_dryer(self) -> None:
        """
        Look up the dryer heater and optional humidity sensor, and check that dryer_max_temp
        is inside the heater's own max_temp.
        """
        pheaters = self.printer.lookup_object('heaters')
        self.dryer_heater = pheaters.lookup_heater(self.dryer_heater_name)
        if self.dryer_max_temp > self.dryer_heater.max_temp:
            error_str = (f"dryer_max_temp ({self.dryer_max_temp}) for [AFC_QidiBox {self.name}] "
                         f"is above max_temp ({self.dryer_heater.max_temp}) of "
                         f"[heater_generic {self.dryer_heater_name}]")
            raise config_error(error_str)

        if self.dryer_humidity_sensor_name is None:
            return
        self.dryer_humidity_sensor = self.printer.lookup_object(self.dryer_humidity_sensor_name,
                                                                None)
        if self.dryer_humidity_sensor is None:
            error_str = (f"dryer_humidity_sensor '{self.dryer_humidity_sensor_name}' for "
                         f"[AFC_QidiBox {self.name}] not found, use the sensor's object name, "
                         f"e.g. 'aht10 {self.name}_dryer_air'")
            raise config_error(error_str)

    def system_Test(self, cur_lane: AFCLane, delay: float, assignTcmd: bool,
                    enable_movement: bool, gcmd: AFCGcmd) -> bool:
        """
        PREP lane test. A lane whose slot sensor is its load sensor takes prep from it first,
        since AFC only mirrors load into prep once the printer is ready.

        :param cur_lane: lane to test
        :param delay: delay between the test moves
        :param assignTcmd: True to assign the lane a T command
        :param enable_movement: True to move the lane during the test
        :param gcmd: command context the test is run through
        :return bool: True when the lane passed
        """
        if cur_lane.prep is None:
            cur_lane.prep_state = cur_lane.raw_load_state
        return super().system_Test(cur_lane, delay, assignTcmd, enable_movement, gcmd)

    def on_filament_insert(self, lane: AFCLane, gcmd: AFCGcmd) -> None:
        """
        Hub mode: feed a newly inserted spool to the hub and park it before the hub, like the
        stock firmware. Skipped for direct lanes, when the lane's load_to_hub is off, while the
        printer is busy and while other filament is in the hub.

        :param lane: lane whose slot sensor detected new filament
        :param gcmd: command context the moves are run through
        """
        super().on_filament_insert(lane, gcmd)
        if (lane.prep is not None
            or lane.is_direct_hub()
            or not lane.load_to_hub):
            return
        if (self.afc.function.is_printing(check_movement=True)
            or lane.hub_obj.state):
            self.logger.info(f"{lane.name}: printer or hub {lane.hub_obj.name} is busy, filament "
                             "is fed to the hub on its next load")
            return
        self._preload_to_hub(lane, gcmd)

    def _preload_to_hub(self, lane: AFCLane, gcmd: AFCGcmd) -> None:
        """
        Feed the lane until the hub sensor triggers, then park it preload_park_dist before the
        hub.

        :param lane: lane to preload
        :param gcmd: command context the moves are run through
        """
        hub = lane.hub_obj
        try:
            _, _, warn = self.move_to_hub(lane, lane.dist_hub + hub.move_dis, MoveDirection.POS,
                                          self.afc.homing_enabled, gcmd=gcmd)
            tries = 0
            while (warn != AFCMoveWarning.ERROR
                   and not hub.state
                   and tries < self.MAX_NUM_MOVES):
                lane.move(self.short_move_dis, self.short_moves_speed, self.short_moves_accel,
                          gcmd=gcmd)
                self.reactor.pause(self.reactor.monotonic() + 0.1)
                tries += 1
            if not hub.state:
                msg = f"{lane.name} filament did not reach hub {hub.name}, check the filament path"
                self.afc.error.AFC_error(msg, False, gcmd=gcmd)
                return
            lane.move(-self.preload_park_dist, self.short_moves_speed, self.short_moves_accel,
                      gcmd=gcmd)
            self.reactor.pause(self.reactor.monotonic() + 0.1)
            if hub.state:
                msg = (f"{lane.name} filament still at hub {hub.name} after parking "
                       f"{self.preload_park_dist:.0f}mm, raise preload_park_dist")
                self.afc.error.AFC_error(msg, False, gcmd=gcmd)
                return
            lane.loaded_to_hub = False
            self.logger.info(f"{lane.name} filament parked {self.preload_park_dist:.0f}mm "
                             f"before hub {hub.name}")
        finally:
            lane.do_enable(False)

    @property
    def dry_active(self) -> bool:
        """Whether a drying cycle is running on this unit."""
        return self._dry_timer is not None

    def get_status(self, eventtime: Optional[float]=None) -> Dict[str, Any]:
        """
        Unit status with the dryer state added.

        :param eventtime: reactor time of the status request, current time when None
        :return Dict[str, Any]: unit status
        """
        response = super().get_status(eventtime)
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        temperature = None
        humidity = None
        if self.dryer_heater is not None:
            temperature = self.dryer_heater.get_status(eventtime).get('temperature')
        if self.dryer_humidity_sensor is not None:
            humidity = self.dryer_humidity_sensor.get_status(eventtime).get('humidity')
        remaining = max(0.0, self._dry_end_time - eventtime) if self.dry_active else 0.0
        response['dryer'] = {
            'active': self.dry_active,
            'target': self._dry_target,
            'temperature': temperature,
            'humidity': humidity,
            'remaining': round(remaining),
            'rotate': self._dry_next_rotate is not None,
        }
        return response

    cmd_AFC_QIDIBOX_DRY_help = "Start QIDI Box filament dryer"
    cmd_AFC_QIDIBOX_DRY_options = {
        "UNIT": {"type": "string", "default": ""},
        "TEMP": {"type": "float", "default": 50.0},
        "DURATION": {"type": "float", "default": 240.0},
        "ROTATE": {"type": "int", "default": 0},
    }
    def cmd_AFC_QIDIBOX_DRY(self, gcmd: GCodeCommand) -> None:
        """
        Start the QIDI Box filament dryer. TEMP is capped to dryer_max_temp and the heater
        turns off after DURATION minutes. Defaults come from dryer_default_temp and
        dryer_default_duration.

        ROTATE=1 also turns the spools while drying, like QIDI's stock firmware: every
        dry_rotate_interval seconds each lane with no filament at its prep sensor retracts
        dry_rotate_length mm, which turns the spool on its rollers. Rotation holds while
        printing or while AFC is busy. Unload the lanes (tip secured to the spool) to dry with
        rotation.

        Usage
        -------
        `AFC_QIDIBOX_DRY UNIT=<unit> TEMP=<C> DURATION=<minutes> [ROTATE=<0|1>]`

        Example
        -------
        ```
        AFC_QIDIBOX_DRY UNIT=QidiBox_1 TEMP=55 DURATION=480 ROTATE=1
        ```
        """
        temp = gcmd.get_float('TEMP', self.dryer_default_temp, above=0.0)
        duration = gcmd.get_float('DURATION', self.dryer_default_duration, above=0.0)
        rotate = gcmd.get_int('ROTATE', 0, minval=0, maxval=1)
        if temp > self.dryer_max_temp:
            self.logger.info(f"{self.name} dryer: TEMP {temp:.0f}°C capped to dryer_max_temp "
                             f"{self.dryer_max_temp:.0f}°C")
            temp = self.dryer_max_temp
        self._start_drying(temp, duration, bool(rotate))

    cmd_AFC_QIDIBOX_DRY_STOP_help = "Stop QIDI Box filament dryer"
    cmd_AFC_QIDIBOX_DRY_STOP_options = {
        "UNIT": {"type": "string", "default": ""}
    }
    def cmd_AFC_QIDIBOX_DRY_STOP(self, gcmd: GCodeCommand) -> None:
        """
        Stop the QIDI Box filament dryer and any spool rotation. The heater target is set to
        0 even when no drying cycle is running.

        Usage
        -------
        `AFC_QIDIBOX_DRY_STOP UNIT=<unit>`

        Example
        -------
        ```
        AFC_QIDIBOX_DRY_STOP UNIT=QidiBox_1
        ```
        """
        self._stop_drying(f"{self.name} dryer stopped")

    def _start_drying(self, temp: float, duration: float, rotate: bool) -> None:
        """
        Set the heater target and arm the cycle timer, replacing a running cycle.

        :param temp: heater target in °C, already capped to dryer_max_temp
        :param duration: cycle length in minutes
        :param rotate: True to turn free spools every dry_rotate_interval seconds
        """
        self._cancel_dry_timer()
        self.dryer_heater.set_temp(temp)
        now = self.reactor.monotonic()
        self._dry_target = temp
        self._dry_end_time = now + duration * 60.0
        self._dry_next_rotate = now + self.dry_rotate_interval if rotate else None
        self._dry_timer = self.reactor.register_timer(self._dry_tick, self._next_dry_wake())
        msg = f"{self.name} dryer started: {temp:.0f}°C for {duration:.0f} min"
        if rotate:
            msg += f", turning free spools every {self.dry_rotate_interval:.0f}s"
        self.logger.info(msg)

    def _stop_drying(self, message: str) -> None:
        """
        Turn the heater off and clear the drying cycle.

        :param message: info message to log
        """
        self._cancel_dry_timer()
        if self.dryer_heater is not None:
            self.dryer_heater.set_temp(0.0)
        self._dry_target = 0.0
        self._dry_end_time = 0.0
        self._dry_next_rotate = None
        self.logger.info(message)

    def _cancel_dry_timer(self) -> None:
        """Unregister the cycle timer, if armed."""
        timer = self._dry_timer
        if timer is None:
            return
        self._dry_timer = None
        self.reactor.unregister_timer(timer)

    def _next_dry_wake(self) -> float:
        """
        The next reactor time the cycle timer needs to run.

        :return float: the cycle end, or the next rotation when that comes first
        """
        if self._dry_next_rotate is None:
            return self._dry_end_time
        return min(self._dry_end_time, self._dry_next_rotate)

    def _dry_tick(self, eventtime: float) -> float:
        """
        Reactor timer: end the cycle when its time is up, otherwise turn free spools when
        rotation is due.

        :param eventtime: reactor time of this tick
        :return float: the next tick, or reactor.NEVER when the cycle ended
        """
        if eventtime >= self._dry_end_time:
            # The reactor drops this timer when NEVER is returned
            self._dry_timer = None
            self._stop_drying(f"{self.name} dryer finished drying cycle")
            return self.reactor.NEVER
        if (self._dry_next_rotate is not None
            and eventtime >= self._dry_next_rotate):
            self._rotate_spools(gcmd="callback")
            self._dry_next_rotate = eventtime + self.dry_rotate_interval
        return self._next_dry_wake()

    def _rotate_spools(self, gcmd: AFCGcmd) -> None:
        """
        Retract every lane with no filament at its sensors, which turns its spool.

        :param gcmd: command context the lane moves are run through
        """
        if (self.afc.function.is_printing(check_movement=True)
            or self.afc.current_state != State.IDLE):
            self.logger.debug(f"{self.name} dryer: spool rotation skipped, printer busy")
            return
        free: List[str] = []
        for lane in self.lanes.values():
            if (lane.prep_state
                or lane.load_state
                or lane.tool_loaded):
                continue
            lane.move(-self.dry_rotate_length, self.dry_rotate_speed, self.short_moves_accel,
                      gcmd=gcmd)
            lane.do_enable(False)
            free.append(lane.name)
        self.logger.debug(f"{self.name} dryer: turned spools for {', '.join(free) or 'no lanes'}")


def load_config_prefix(config: ConfigWrapper) -> afcQidiBox:
    """
    Klipper config entry point for AFC_QidiBox <name> sections.

    :param config: Klipper config wrapper for the AFC_QidiBox section
    :return afcQidiBox: constructed QIDI Box unit
    """
    return afcQidiBox(config)
