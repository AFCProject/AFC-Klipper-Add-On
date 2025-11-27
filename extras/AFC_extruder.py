# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback
import chelper
from extras.force_move import calc_move_time

try:
    from printer import message_ready as READY # type: ignore
except ImportError:
    from klippy import message_ready as READY
from configparser import Error as error
from math import ceil

from typing import TYPE_CHECKING, Optional, Union, Dict

if TYPE_CHECKING:
    from klippy import Printer
    from reactor import PollReactor, SelectReactor
    from configfile import ConfigWrapper
    from kinematics.extruder import PrinterExtruder
    from toolhead import ToolHead
    from extras.heaters import Heater
    from extras.AFC import afc
    from extras.AFC_Toolchanger import AfcToolchanger
    from extras.AFC_functions import afcFunction

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_utils import add_filament_switch
except: raise error(ERROR_STR.format(import_lib="AFC_utils", trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCLane
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try: from extras.AFC import AFCLaneState
except: raise error(ERROR_STR.format(import_lib="AFC", trace=traceback.format_exc()))

LARGE_TIME_OFFSET = 99999.9

class AFCExtruder:
    def __init__(self, config: ConfigWrapper) -> None:
        self.printer:Printer = config.get_printer()
        buttons         = self.printer.load_object(config, "buttons")
        self.afc: afc   = self.printer.lookup_object('AFC')
        self.gcode      = self.printer.lookup_object('gcode')
        self.logger     = self.afc.logger
        self.reactor: Union[PollReactor, SelectReactor] = self.printer.get_reactor()
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

        self.extruder_move_timer= self.reactor.register_timer(self.extruder_move_cb)
        self.temp_check_timer   = self.reactor.register_timer(self.temp_check_cb)

        self.toolhead_extruder: PrinterExtruder
        self.fullname                   = config.get_name()
        self.mutex                      = self.reactor.mutex()

        self.name: str                  = self.fullname.split(' ')[-1]
        self.tool_start                 = config.get('pin_tool_start', None)                                            # Pin for sensor before(pre) extruder gears
        self.tool_end                   = config.get('pin_tool_end', None)                                              # Pin for sensor after(post) extruder gears (optional)
        self.tool_stn                   = config.getfloat("tool_stn", 72)                                               # Distance in mm from the toolhead sensor to the tip of the nozzle in mm, if `tool_end` is defined then distance is from this sensor
        self.tool_stn_unload            = config.getfloat("tool_stn_unload", 100)                                       # Distance to move in mm while unloading toolhead
        self.tool_sensor_after_extruder = config.getfloat("tool_sensor_after_extruder", 0)                              # Extra distance to move in mm once the pre- / post-sensors are clear. Useful for when only using post sensor, so this distance can be the amount to move to clear extruder gears
        self.tool_unload_speed          = config.getfloat("tool_unload_speed", 25)                                      # Unload speed in mm/s when unloading toolhead. Default is 25mm/s.
        self.tool_load_speed            = config.getfloat("tool_load_speed", 25)                                        # Load speed in mm/s when loading toolhead. Default is 25mm/s.
        self.buffer_name                = config.get('buffer', None)                                                    # Buffer to use for extruder, this variable can be overridden per lane
        self.enable_sensors_in_gui      = config.getboolean("enable_sensors_in_gui",    self.afc.enable_sensors_in_gui) # Set to True toolhead sensors switches as filament sensors in mainsail/fluidd gui, overrides value set in AFC.cfg
        self.enable_runout              = config.getboolean("enable_tool_runout",       self.afc.enable_tool_runout)
        self.debounce_delay             = config.getfloat("debounce_delay",             self.afc.debounce_delay)
        self.deadband                   = config.getfloat("deadband", 2)                                                # Deadband for extruder heater, default is 2 degrees Celsius

        self.tc_unit_name: Optional[str] = config.get("toolchanger_unit", None)
        self.tc_unit_obj: Optional[AfcToolchanger|None] = None
        self.tc_lane: Optional[AFCLane|None]            = None
        self.tool: Optional[str]        = config.get('tool', None)
        self.tool_obj                   = None
        self.map: Optional[str]         = config.get('map', None)
        self.no_lanes                   = False

        self.lane_loaded: Optional[str] = None
        self.lanes: Dict                = {}
        self.load_active                = False
        self.current_move_distance: float = 0

        self.tool_start_state = False
        if self.tool_start is not None:
            if "unknown" == self.tool_start.lower():
                raise error(f"Unknown is not valid for pin_tool_start in [{self.fullname}] config.")

            if self.tool_start == "buffer":
                self.logger.info("Setting up as buffer")
            else:
                buttons.register_buttons([self.tool_start], self.tool_start_callback)
                self.fila_tool_start, self.debounce_button_start = add_filament_switch(f"{self.name}_tool_start", self.tool_start, self.printer,
                                                                                        self.enable_sensors_in_gui, self.handle_start_runout, self.enable_runout,
                                                                                        self.debounce_delay )

        self.tool_end_state = False
        if self.tool_end is not None:
            if "unknown" == self.tool_end.lower():
                raise error(f"Unknown is not valid for pin_tool_end in [{self.fullname}] config.")

            buttons.register_buttons([self.tool_end], self.tool_end_callback)
            self.fila_tool_end, self.debounce_button_end = add_filament_switch(f"{self.name}_tool_end", self.tool_end, self.printer,
                                                                                self.enable_sensors_in_gui, self.handle_end_runout, self.enable_runout,
                                                                                self.debounce_delay )

        self.common_save_msg = "\nRun SAVE_EXTRUDER_VALUES EXTRUDER={} once done to update values in config".format(self.name)

        if self.tc_unit_name:
            config.fileconfig.set(config.section, "unit", self.tc_unit_name.split()[-1])
            config.fileconfig.set(config.section, "extruder", self.name)
            config.fileconfig.set(config.section, "hub", "direct")
            self.tc_lane = AFCLane(config)
            self.printer.objects[f"AFC_lane {self.name}"] = self.tc_lane
            # TODO: Once homing is in create common function for this and AFC_stepper

            # Check for Klipper new motion queuing update
            try:
                self.motion_queuing = self.printer.load_object(config, "motion_queuing")
            except Exception:
                self.motion_queuing = None

            ffi_main, ffi_lib = chelper.get_ffi()
            self.stepper_kinematics = ffi_main.gc(
                ffi_lib.cartesian_stepper_alloc(b'x'), ffi_lib.free)

            if self.motion_queuing is not None:
                self.trapq          = self.motion_queuing.allocate_trapq()
                self.trapq_append   = self.motion_queuing.lookup_trapq_append()
            else:
                self.trapq                  = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
                self.trapq_append           = ffi_lib.trapq_append
                self.trapq_finalize_moves   = ffi_lib.trapq_finalize_moves

        self.show_macros = self.afc.show_macros
        self.function: afcFunction = self.printer.load_object(config, 'AFC_functions')

        self.function.register_mux_command(self.show_macros, 'UPDATE_TOOLHEAD_SENSORS', "EXTRUDER", self.name,
                                           self.cmd_UPDATE_TOOLHEAD_SENSORS, self.cmd_UPDATE_TOOLHEAD_SENSORS_help,
                                           self.cmd_UPDATE_TOOLHEAD_SENSORS_options)
        self.function.register_mux_command(self.show_macros, 'SAVE_EXTRUDER_VALUES', "EXTRUDER", self.name,
                                           self.cmd_SAVE_EXTRUDER_VALUES, self.cmd_SAVE_EXTRUDER_VALUES_help,
                                           self.cmd_SAVE_EXTRUDER_VALUES_options)

    def __str__(self):
        return self.name

    def check_lanes(self):
        # Checks to see if there are multiple lanes per toolhead, remove self created lane if
        # there are more than 1 lanes registered
        if self.tc_lane is None:
            return

        if len(self.lanes) > 1 and self.lanes.get(self.tc_lane.name):
            self.tc_lane.unit_obj.lanes.pop(self.tc_lane.name)
            self.lanes.pop(self.tc_lane.name)
            self.afc.lanes.pop(self.tc_lane.name)
            self.printer.objects.pop(f"AFC_lane {self.name}")

    def handle_ready(self):
        # Check to see if extruder name is currently in `self.lanes`, if it is then that means that
        # no other lanes are setup for this extruder, and that this is a "standalone" toolhead
        if self.name in self.lanes:
            self.no_lanes = True
            self.logger.info(f"{self.name} no lanes")


    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """
        self.afc.tools[self.name] = self

        try:
            self.toolhead_extruder = self.printer.lookup_object(self.name)
        except:
            raise error("[{}] not found in config file".format(self.name))

        try:
            if self.tool:
                self.tool_obj = self.printer.lookup_object(self.tool)
        except:
            raise error(f'[{self.tool}] not found in config file for {self.name}')

        try:
            if self.tc_unit_name:
                self.tc_unit_obj = self.printer.lookup_object(
                    f"AFC_Toolchanger {self.tc_unit_name}"
                )
                self.tc_lane.unit_obj = self.tc_unit_obj

        except:
            raise error(
                f'AFC_Toolchanger {self.tc_unit_name} not found in config file for {self.name}'
            )

    def _handle_toolhead_sensor_runout(self, state, sensor_name):
        """
        Handles runout detection at the toolhead sensors (tool_start or tool_end).
        Notifies the currently loaded lane if filament is missing at the toolhead sensor.
        :param state: Boolean indicating sensor state (True = filament present, False = runout)
        :param sensor_name: Name of the triggering sensor ("tool_start" or "tool_end")
        """
        # Notify the currently loaded lane if filament is missing at toolhead
        if not state and self.lane_loaded and self.lane_loaded in self.lanes:
            lane = self.lanes[self.lane_loaded]
            if hasattr(lane, "handle_toolhead_runout"):
                lane.handle_toolhead_runout(sensor=sensor_name)

    def handle_start_runout( self, eventtime):
        """
        Callback function for tool start runout, this is different than `tool_start_callback` function as this function
        can be delayed and is called from filament_switch_sensor class when it detects a runout event.

        Before exiting `min_event_systime` is updated as this mimics how its done in `_exec_gcode` function in RunoutHelper class
        as AFC overrides `_runout_event_handler` function with this function callback. If `min_event_systime` does not get
        updated then future switch changes will not be detected.

        :param eventtime: Event time from the button press
        """
        self._handle_toolhead_sensor_runout(self.fila_tool_start.runout_helper.filament_present, "tool_start")
        self.fila_tool_start.runout_helper.min_event_systime = self.reactor.monotonic() + self.fila_tool_start.runout_helper.event_delay

    def tool_start_callback(self, eventtime, state):
        """
        Callback for the tool_start (pre-extruder) filament sensor.
        Updates the sensor state and triggers runout handling if filament is missing.

        If extruder is its own lane(no BoxTurtle, HTLF, etc connected to this lane) then an async
        automatic load sequence is performed.

        :param eventtime: Event time from the button press
        :param state: Boolean indicating sensor state (True = filament present, False = runout)
        """
        with self.mutex:
            if state != self.tool_start_state:
                if self.tc_unit_name and self.no_lanes:
                    self.tc_lane.load_state = state
                    self.tc_lane.prep_state = state

                    if (self.printer.state_message == READY and
                        self.tc_lane._afc_prep_done):
                        if state:
                            if not self.load_active:
                                self.load_unload_sequence(self.tool_stn)
                        else:
                            self.tc_lane.set_tool_unloaded()
                            self.tc_lane.set_unloaded()

                        self.afc.save_vars()
            else:
                self.logger.info("Not loading State matches tool_start_state")

            self.tool_start_state = state


    def buffer_trailing_callback(self, eventtime, state):
        self.buffer_trailing = state

    def handle_end_runout( self, eventtime):
        """
        Callback function for tool end runout, this is different than `tool_end_callback` function as this function
        can be delayed and is called from filament_switch_sensor class when it detects a runout event.

        Before exiting `min_event_systime` is updated as this mimics how its done in `_exec_gcode` function in RunoutHelper class
        as AFC overrides `_runout_event_handler` function with this function callback. If `min_event_systime` does not get
        updated then future switch changes will not be detected.

        :param eventtime: Event time from the button press
        """

        # TODO: Need to figure out correct runout for toolheads without units attached (toolchanger setups)
        self._handle_toolhead_sensor_runout(self.fila_tool_end.runout_helper.filament_present, "tool_end")
        self.fila_tool_end.runout_helper.min_event_systime = self.reactor.monotonic() + self.fila_tool_end.runout_helper.event_delay

    def tool_end_callback(self, eventtime, state):
        """
        Callback for the tool_end (post-extruder) filament sensor.
        Updates the sensor state and triggers runout handling if filament is missing.

        :param eventtime: Event time from the button press
        :param state: Boolean indicating sensor state (True = filament present, False = runout)
        """
        self.tool_end_state = state

    def get_heater(self) -> Heater:
        """
        Helper function for returning extruders Heater object
        """

        return self.toolhead_extruder.get_heater()

    def load_unload_sequence(self, distance: float) -> None:
        """
        Starts unloading/loading sequence for extruders, currently only for toolheads without lanes
        attached to a toolhead

        Unloading/Loading sequence:
        - Check extruder temp, starts heating if not up to temp
        - Starts temperature callback timer
        - Once up to temperature move_extruder function is called and callback timer is started for
          the ceiling of the time it takes to move the specified distance.

        This sequence has been setup so that this can happen during a print without causing TTC's

        :param distance: distance to load filament, this is set to `self.current_move_distance` so
                         that distance is saved and used in move_extruder function once extruder is
                         up to temperature
        """
        self.logger.info(f"Loading {self.name}")
        self.load_active = True
        self.current_move_distance = distance
        # TODO: maybe make this so this same function can be called normally when lanes are assigned
        # to extruders...
        if distance > 0:
            self.tc_lane.unit_obj.lane_loading(self.tc_lane)
            self.tc_lane.status = AFCLaneState.TOOL_LOADING
        else:
            self.tc_lane.status = AFCLaneState.TOOL_UNLOADING

        self.afc._check_extruder_temp(self.tc_lane, no_wait=True)
        self.reactor.update_timer(self.temp_check_timer,
                                self.reactor.monotonic() +1 )

    def move_extruder(self, distance: float, sync: bool=False) -> None:
        """
        Moves toolhead extruder by specified distance

        Once move is scheduled, timer is updated for extruder move callback for the ceiling of
        the time it takes to move specified distance(dwell_time)

        :param distance: Distance to move extruder, distance of zero will immediately return
        """
        if distance == 0:
            return

        # TODO: put a guard here as well to check if loading is active
        # TODO: make sync work correctly

        self.load_active = True
        toolhead: ToolHead = self.printer.lookup_object("toolhead")
        stepper = self.toolhead_extruder.extruder_stepper.stepper

        self.prev_sk = stepper.set_stepper_kinematics(self.stepper_kinematics)
        self.prev_trapq = stepper.set_trapq(self.trapq)
        stepper.set_position((0., 0., 0.))

        axis_r, accel_t, cruise_t, cruise_v = calc_move_time(distance, self.tool_load_speed, 5)
        print_time = toolhead.get_last_move_time()
        self.trapq_append(self.trapq, print_time, accel_t, cruise_t, accel_t,
                            0., 0., 0., axis_r, 0., 0., 0., cruise_v, 5)
        print_time = print_time + accel_t + cruise_t + accel_t

        if self.motion_queuing is None:
            stepper.generate_steps(print_time)
            self.trapq_finalize_moves(self.trapq, print_time + LARGE_TIME_OFFSET,
                                    print_time + LARGE_TIME_OFFSET)
            toolhead.note_mcu_movequeue_activity(print_time)            # type: ignore
        else:
            self.motion_queuing.note_mcu_movequeue_activity(print_time)

        dwell_time = accel_t + cruise_t + accel_t

        self.reactor.update_timer(self.extruder_move_timer,
                                  self.reactor.monotonic() + ceil(dwell_time))

    def extruder_move_cb(self, eventtime: float) -> float:
        """
        Extruder move callback that is called after scheduling moves in `move_extruder` function,
        this function put back extruder steppers motion queue and disables extruder stepper

        :param eventtime: Event time when callback function is called, currently not used
        :return float: Always returns reactor NEVER to stop function from being called again
        """
        # TODO: set a flag so that AFC knows to purge properly when switched to a toolhead that
        # was asynchronously loaded
        toolhead: ToolHead = self.printer.lookup_object("toolhead")
        stepper = self.toolhead_extruder.extruder_stepper.stepper
        toolhead.flush_step_generation()
        stepper.set_trapq(self.prev_trapq)
        stepper.set_stepper_kinematics(self.prev_sk)
        if self.motion_queuing is not None:
            self.motion_queuing.wipe_trapq(self.trapq)

        self.function.do_enable(False, self.name)
        self.load_active = False
        info_str = "loading" if self.current_move_distance > 0 else "unloading"
        self.logger.info(f"{self.name} {info_str} done")
        self.tc_lane.status = AFCLaneState.NONE
        self.current_move_distance = 0
        return self.reactor.NEVER

    def temp_check_cb(self, eventtime:float) -> float:
        """
        Callback timer to check if extruder is up to temperature, once up to temperature filament
        is moved by `self.current_move_distance` by calling `move_extruder` function

        :param eventtime: Event time when callback function is called
        :return float: Returns current time + 1s if extruder is not up to temperature, returns
                       reactors NEVER if extruder is up to temperature to stop callback timer.
        """
        heater = self.get_heater()
        current_temp, target_temp = heater.get_temp(eventtime)

        if (current_temp >= target_temp - self.afc.temp_wait_tolerance
            and current_temp <= target_temp + self.afc.temp_wait_tolerance):
            if self.tool_start_state:
                info_str = "loading to" if self.current_move_distance > 0 else "unloading from"
                self.logger.info(f"{self.name} temp within range, {info_str} nozzle")
                self.move_extruder(self.current_move_distance)
                if self.current_move_distance > 0:
                    self.tc_lane.set_loaded()
                    self.tc_lane.set_tool_loaded()
                else:
                    self.tc_lane.set_tool_unloaded()
            else:
                self.load_active = False
                self.tc_lane.set_unloaded()
                self.afc.save_vars()
                self.logger.error(
                    f"Filament is no longer detected at tool start sensor for {self.name}.\n"
                    "Not loading filament to nozzle."
                )
            return self.reactor.NEVER
        else:
            self.logger.debug(f"{self.name}: waiting for temp: {current_temp}")

        return self.reactor.monotonic() + 1

    def _update_tool_stn(self, length):
        """
        Helper function to set tool_stn length

        :param length: Length to set to tool_stn parameter
        """
        if length > 0:
            msg = "tool_stn updated old: {}, new: {}".format(self.tool_stn, length)
            msg += self.common_save_msg
            self.tool_stn = length
            self.logger.info(msg)
        else:
            self.logger.error("tool_stn length should be greater than zero")

    def _update_tool_stn_unload(self, length):
        """
        Helper function to set tool_stn_unload length

        :param length: Length to set to tool_stn_unload parameter
        """
        if length >= 0:
            msg = "tool_stn_unload updated old: {}, new: {}".format(self.tool_stn_unload, length)
            msg += self.common_save_msg
            self.tool_stn_unload = length
            self.logger.info(msg)
        else:
            self.logger.error("tool_stn_unload length should be greater than or equal to zero")

    def _update_tool_after_extr(self, length):
        """
        Helper function to set tool_sensor_after_extruder length

        :param length: Length to set to tool_sensor_after_extruder parameter
        """
        if length > 0:
            msg = "tool_sensor_after_extruder updated old: {}, new: {}".format(self.tool_sensor_after_extruder, length)
            msg += self.common_save_msg
            self.tool_sensor_after_extruder = length
            self.logger.info(msg)
        else:
            self.logger.error("tool_sensor_after_extruder length should be greater than zero")

    cmd_UPDATE_TOOLHEAD_SENSORS_help = "Gives ability to update tool_stn, tool_stn_unload, tool_sensor_after_extruder values without restarting klipper"
    cmd_UPDATE_TOOLHEAD_SENSORS_options = {
        "EXTRUDER": {"type": "string", "default": "extruder"},
        "TOOL_STN": {"type": "float", "default": 0},
        "TOOL_STN_UNLOAD": {"type": "float", "default": 0},
        "TOOL_AFTER_EXTRUDER": {"type": "float", "default": 0}
    }

    def cmd_UPDATE_TOOLHEAD_SENSORS(self, gcmd):
        """
        Macro call to adjust `tool_stn` `tool_stn_unload` `tool_sensor_after_extruder` lengths for specified extruder without having to
        update config file and restart klipper.

        `tool_stn length` is the length from the sensor before extruder gears (tool_start) to nozzle. If sensor after extruder gears(tool_end)
        is set then the value if from tool_end sensor.

        `tool_stn_unload` length is the length to unload so that filament is not in extruder gears anymore. Set this value to `0` if you
        have a cutter above the extruder gears.

        `tool_sensor_after_extruder` length is mainly used for those that have a filament sensor after extruder gears, target this
        length to retract filament enough so that it's not in the extruder gears anymore.  <nl>

        Please pause print if you need to adjust this value while printing

        Usage
        -----
        `UPDATE_TOOLHEAD_SENSORS EXTRUDER=<extruder> TOOL_STN=<length> TOOL_STN_UNLOAD=<length> TOOL_AFTER_EXTRUDER=<length>`

        Example
        -----
        ```
        UPDATE_TOOLHEAD_SENSORS EXTRUDER=extruder TOOL_STN=100
        ```

        """
        tool_stn                    = gcmd.get_float("TOOL_STN",            self.tool_stn)
        tool_stn_unload             = gcmd.get_float("TOOL_STN_UNLOAD",     self.tool_stn_unload)
        tool_sensor_after_extruder  = gcmd.get_float("TOOL_AFTER_EXTRUDER", self.tool_sensor_after_extruder)

        if tool_stn != self.tool_stn:
            self._update_tool_stn( tool_stn )
        if tool_stn_unload != self.tool_stn_unload:
            self._update_tool_stn_unload( tool_stn_unload )
        if tool_sensor_after_extruder != self.tool_sensor_after_extruder:
            self._update_tool_after_extr( tool_sensor_after_extruder )

    cmd_SAVE_EXTRUDER_VALUES_help = ("Saves tool_stn, tool_stn_unload and tool_sensor_after_extruder values to config "
                                     "file.")
    cmd_SAVE_EXTRUDER_VALUES_options = {"EXTRUDER": {"type": "string", "default": "extruder"}}
    def cmd_SAVE_EXTRUDER_VALUES(self, gcmd):
        """
        Macro call to write tool_stn, tool_stn_unload and tool_sensor_after_extruder variables to config file for specified extruder.

        Usage
        -----
        `SAVE_EXTRUDER_VALUES EXTRUDER=<extruder>`

        Example
        -----
        ```
        SAVE_EXTRUDER_VALUES EXTRUDER=extruder
        ```
        """
        self.afc.function.ConfigRewrite(self.fullname, 'tool_stn', self.tool_stn, '')
        self.afc.function.ConfigRewrite(self.fullname, 'tool_stn_unload', self.tool_stn_unload, '')
        self.afc.function.ConfigRewrite(self.fullname, 'tool_sensor_after_extruder', self.tool_sensor_after_extruder, '')

    def get_status(self, eventtime=None):
        self.response = {}
        self.response['tool_stn'] = self.tool_stn
        self.response['tool_stn_unload'] = self.tool_stn_unload
        self.response['tool_sensor_after_extruder'] = self.tool_sensor_after_extruder
        self.response['tool_unload_speed'] = self.tool_unload_speed
        self.response['tool_load_speed'] = self.tool_load_speed
        self.response['buffer'] = self.buffer_name
        self.response['lane_loaded'] = self.lane_loaded
        self.response['tool_start'] = self.tool_start
        self.response['tool_start_status'] = bool(self.tool_start_state)
        self.response['tool_end'] = self.tool_end
        self.response['tool_end_status'] = bool(self.tool_end_state)
        self.response['lanes'] = [lane.name for lane in self.lanes.values()]
        return self.response

def load_config_prefix(config):
    return AFCExtruder(config)
