# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import traceback

from configparser import Error as error

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_utils import add_filament_switch
except: raise error(ERROR_STR.format(import_lib="AFC_utils", trace=traceback.format_exc()))

class AFCExtruder:
    def __init__(self, config):
        self.printer    = config.get_printer()
        buttons         = self.printer.load_object(config, "buttons")
        self.afc        = self.printer.lookup_object('AFC')
        self.gcode      = self.printer.lookup_object('gcode')
        self.logger     = self.afc.logger
        self.reactor    = None
        self.printer.register_event_handler("klippy:connect", self.handle_connect)

        self.fullname                   = config.get_name()
        self.name                       = self.fullname.split(' ')[-1]
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

        self.lane_loaded                = None
        self.lanes                      = {}

        self.tool_start_state = False
        if self.tool_start is not None:
            if "unknown" == self.tool_start.lower():
                raise error(f"Unknown is not valid for pin_tool_start in [{self.fullname}] config.")

            if self.tool_start == "buffer":
                self.logger.info("Setting up as buffer")
            else:
                buttons.register_buttons([self.tool_start], self.tool_start_callback)
                self.fila_tool_start, self.debounce_button_start = add_filament_switch("tool_start", self.tool_start, self.printer,
                                                                                        self.enable_sensors_in_gui, self.handle_start_runout, self.enable_runout,
                                                                                        self.debounce_delay )

        self.tool_end_state = False
        if self.tool_end is not None:
            if "unknown" == self.tool_end.lower():
                raise error(f"Unknown is not valid for pin_tool_end in [{self.fullname}] config.")

            buttons.register_buttons([self.tool_end], self.tool_end_callback)
            self.fila_tool_end, self.debounce_button_end = add_filament_switch("tool_end", self.tool_end, self.printer,
                                                                                self.enable_sensors_in_gui, self.handle_end_runout, self.enable_runout,
                                                                                self.debounce_delay )

        self.common_save_msg = "\nRun SAVE_EXTRUDER_VALUES EXTRUDER={} once done to update values in config".format(self.name)

        self.show_macros = self.afc.show_macros
        self.function = self.printer.load_object(config, 'AFC_functions')

        self.function.register_mux_command(self.show_macros, 'UPDATE_TOOLHEAD_SENSORS', "EXTRUDER", self.name,
                                           self.cmd_UPDATE_TOOLHEAD_SENSORS, self.cmd_UPDATE_TOOLHEAD_SENSORS_help,
                                           self.cmd_UPDATE_TOOLHEAD_SENSORS_options)
        self.function.register_mux_command(self.show_macros, 'SAVE_EXTRUDER_VALUES', "EXTRUDER", self.name,
                                           self.cmd_SAVE_EXTRUDER_VALUES, self.cmd_SAVE_EXTRUDER_VALUES_help,
                                           self.cmd_SAVE_EXTRUDER_VALUES_options)

    def __str__(self):
        return self.name

    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """
        self.reactor = self.afc.reactor
        self.afc.tools[self.name] = self

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
        :param eventtime: Event time from the button press
        :param state: Boolean indicating sensor state (True = filament present, False = runout)
        """
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
