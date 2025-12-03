# Armored Turtle Automated Filament Control
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# This file includes code modified from the Shaketune Project. https://github.com/Frix-x/klippain-shaketune
# Originally authored by Félix Boisselier and licensed under the GNU General Public License v3.0.
#
# Full license text available at: https://www.gnu.org/licenses/gpl-3.0.html

import os
import random
import re
import traceback
import configparser

from configfile import error
from datetime import datetime
from pathlib import Path

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_respond import AFCprompt
except: raise error(ERROR_STR.format(import_lib="AFC_respond", trace=traceback.format_exc()))

def load_config(config):
    return afcFunction(config)

class afcFunction:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.printer.register_event_handler("afc_stepper:register_macros",self.register_lane_macros)
        self.printer.register_event_handler("afc_hub:register_macros",self.register_hub_macros)
        self.printer.register_event_handler("afc:moonraker_connect", self.handle_moonraker_connect)
        self.auto_var_file = None
        self.errorLog = {}
        self.pause    = False
        self.afc      = None
        self.logger   = None
        self.mcu      = None

        self.show_macros = True
        self.register_commands(self.show_macros, 'AFC_CALIBRATION', self.cmd_AFC_CALIBRATION,   self.cmd_AFC_CALIBRATION_help)
        self.register_commands(self.show_macros, 'AFC_RESET',       self.cmd_AFC_RESET,         self.cmd_AFC_RESET_help,
                               self.cmd_AFC_RESET_options)
        self.register_commands(self.show_macros, 'AFC_LANE_RESET',  self.cmd_AFC_LANE_RESET,
                               self.cmd_AFC_LANE_RESET_help, self.cmd_AFC_LANE_RESET_options)
        self.register_commands(self.show_macros, 'AFC_TEST_LANES', self.cmd_AFC_TEST_LANES,
                               self.cmd_AFC_TEST_LANES_help)
        # Always adding this so it will show up as a button in guis
        self.register_commands(self.show_macros, 'AFC_GET_TD_ONE_DATA', self.cmd_AFC_GET_TD_ONE_DATA,   self.cmd_AFC_GET_TD_ONE_DATA_help)

    def register_lane_macros(self, lane_obj):
        """
        Callback function to register macros with proper lane names so that klipper errors out correctly when users supply lanes names that
        are not valid

        :param lane_obj: object for lane to register
        """
        self.afc.gcode.register_mux_command('TEST', "LANE", lane_obj.name, self.cmd_TEST, desc=self.cmd_TEST_help)
        self.afc.gcode.register_mux_command('HUB_CUT_TEST', "LANE", lane_obj.name, self.cmd_HUB_CUT_TEST, desc=self.cmd_HUB_CUT_TEST_help)

    def register_hub_macros(self, hub_obj):
        """
        Callback function to register macros with proper hub names so that klipper errors out correctly when users supply hub names that
        are not valid

        :param hub_obj: object for hub to register
        """
        self.afc.gcode.register_mux_command('SET_BOWDEN_LENGTH', 'HUB', hub_obj.name, self.cmd_SET_BOWDEN_LENGTH, desc=self.cmd_SET_BOWDEN_LENGTH_help)

    def handle_moonraker_connect(self):
        """
        Registers macros commands after moonrakers connection has been established so that endpoint can be queried successfully
        to check if TD-1 is defined in users moonrakers.conf file.
        """
        if self.afc.td1_defined:
            self.afc.gcode.register_command('AFC_GET_TD_ONE_LANE_DATA', self.cmd_AFC_GET_TD_ONE_LANE_DATA,  desc=self.cmd_AFC_GET_TD_ONE_LANE_DATA_help)
            self.afc.gcode.register_command('AFC_RESET_TD1',            self.cmd_AFC_RESET_TD1,             desc=self.cmd_AFC_RESET_TD1_help)

    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.afc`.
        """
        self.afc = self.printer.lookup_object('AFC')
        self.logger = self.afc.logger
        self.mcu = self.printer.lookup_object('mcu')

        self.auto_var_file = Path(self.afc.VarFile).parent.joinpath("AFC_auto_vars.cfg")

        self.afc.gcode.register_command('CALIBRATE_AFC',   self.cmd_CALIBRATE_AFC,   desc=self.cmd_CALIBRATE_AFC_help)
        self.afc.gcode.register_command('ALL_CALIBRATION', self.cmd_ALL_CALIBRATION, desc=self.cmd_ALL_CALIBRATION_help)
        self.afc.gcode.register_command('AFC_CALI_COMP',   self.cmd_AFC_CALI_COMP,   desc=self.cmd_AFC_CALI_COMP_help)
        self.afc.gcode.register_command('AFC_CALI_FAIL',   self.cmd_AFC_CALI_FAIL,   desc=self.cmd_AFC_CALI_FAIL_help)
        self.afc.gcode.register_command('AFC_HAPPY_P',     self.cmd_AFC_HAPPY_P,     desc=self.cmd_AFC_HAPPY_P_help)
        self.afc.gcode.register_command('TEST_LANE', self.cmd_TEST_LANE, desc=self.cmd_TEST_LANE_help)
        self.afc.gcode.register_command('TEST_LANE_ITERATIONS', self.cmd_TEST_LANE_ITERATIONS, desc=self.cmd_TEST_LANE_ITERATIONS_help)

    auto_save_top_comment = "# This file is autogenerated and updated when variables are not in your normal AFC config files\n\n"
    def write_auto_variables(self, section_name, value_name, value):
        """
        Function writes variables to a separate file defined by `auto_var_file` variable.
        First checks if section_name exists, if section does not exist its added
        and then key,value is added to section and then config is saved to disk.

        :param section_name: Name of section to add key/value to
        :param value_name: Name of key to add to section
        :param value: Value to assign to key
        """
        config = configparser.RawConfigParser( delimiters=(':', '=') )

        if not os.path.exists(self.auto_var_file):
            open(self.auto_var_file, 'a').close()

        # Read in old config
        with open(self.auto_var_file) as fp:
            config.read_file(fp)

        if not config.has_section( section_name ):
            config.add_section( section_name )

        config.set( section_name, value_name, value )

        with open(self.auto_var_file, 'w') as fp:
            fp.write(self.auto_save_top_comment)
            config.write(fp)

    def ConfigRewrite(self, rawsection, rawkey, rawvalue, msg=""):
        taskdone = False
        sectionfound = False
        # Creating regex pattern based off rawsection
        pattern = re.compile("^\[\s*{}\s*\]".format(rawsection))
        for filename in os.listdir(self.afc.cfgloc):
            file_path = os.path.join(self.afc.cfgloc, filename)
            if os.path.isfile(file_path) and filename.endswith(".cfg"):
                with open(file_path, 'r') as f:
                    dataout = ''
                    for line in f:
                        # If previous section found and line starts with bracket, means that this line is another section
                        # need to set section found to false in order to not update wrong sections if raw key is not
                        # found
                        if sectionfound and line.startswith("["): sectionfound = False

                        if re.match(pattern, line) is not None: sectionfound = True
                        if sectionfound == True and line.startswith(rawkey):
                            comments = ""
                            comment_index = 0
                            try:
                                comment_index = line.index('#')
                                comments = line[comment_index:-1]
                            except ValueError:
                                pass
                            line = "{}: {}".format(rawkey, rawvalue )
                            # Left justifying comment with spaces so its in original position
                            line = line.ljust(comment_index - 1, " ")

                            line = "{} {}\n".format(line, comments)
                            sectionfound = False
                            taskdone = True
                        dataout += line
                if taskdone:
                    f=open(file_path, 'w')
                    f.write(dataout)
                    f.close()
                    taskdone = False
                    msg +='\n<span class=info--text>Saved {}:{} in {} section to configuration file</span>'.format(rawkey, rawvalue, rawsection)
                    self.logger.info(msg)
                    return
        # Variables not found, write section and key to a separate file
        self.write_auto_variables( rawsection, rawkey, rawvalue )
        msg +='\n<span class=info--text>Key {} not found in section {} added to AFC_auto_vars.cfg file</span>'.format(rawkey, rawsection)
        self.logger.info(msg)

    def TcmdAssign(self, cur_lane):
        """
        Function automatically tries to generate T(n) macros for lanes. If user has already assigned mapping to `map`
        variable in their configs, this is used instead of an auto assigned command. Before assigning command, checks
        happen to make sure auto generated macro has not already be manually assigned in gcode or by something else.

        Auto generated macro and manually assigned macro is then added to a dictionary to easily look up which lane
        to switch to when T(n) command is called.

        :param cur_lane: Lane to assign auto generated T(n) macro
        """
        if cur_lane.map == None :
            for x in range(99):
                cmd = 'T{}'.format(x)
                # Checking to see if cmd exists in lanes that have manually assigned mapping
                # skip cmd and generate next if cmd is manually assigned by user
                manually_assigned = any( cmd == lane._map for lane in self.afc.lanes.values() )
                if not manually_assigned and cmd not in self.afc.tool_cmds:
                    # Checking if macro already exists, generate next valid cmd if current generated cmd exists
                    existing = self.afc.gcode.ready_gcode_handlers.get(cmd)
                    if not existing:
                        cur_lane._map = cur_lane.map = cmd
                        break
        self.afc.tool_cmds[cur_lane.map]=cur_lane.name
        try:
            self.afc.gcode.register_command(cur_lane.map, self.afc.cmd_CHANGE_TOOL, desc=self.afc.cmd_CHANGE_TOOL_help)
        except:
            self.logger.info("Error trying to map lane {lane} to {tool_macro}, please make sure there are no macros already setup for {tool_macro}".format(lane=[cur_lane.name], tool_macro=cur_lane.map), )
        self.afc.save_vars()

    def check_macro_present(self, macro_name):
        """
        Helper function to check if a macro is present in the printer config
        Args:
            macro_name: Name of macro to check for

        Returns:
            boolean: True if macro is present
        """
        try:
            existing_macros = getattr(self.afc.gcode, "ready_gcode_handlers", {})
            if macro_name in existing_macros:
                return True
            return False
        except Exception:
            return False


    def check_homed(self):
        """
        Helper function to determine if printer is currently homed, if not, then apply G28

        :return boolean: True if xyz is homed
        """
        if not self.is_homed():
            if self.afc.auto_home:
                self.afc.gcode.run_script_from_command("G28")
                self.afc.toolhead.wait_moves()
                if self.afc.auto_level_macro is not None:
                    if self.check_macro_present(self.afc.auto_level_macro):
                        self.afc.gcode.run_script_from_command(self.afc.auto_level_macro)
                        self.afc.toolhead.wait_moves()
                    else:
                        self.afc.error.AFC_error("Auto level macro defined, but not found in printer configuration.", False, level=2)
                        return False
                return True
            else:
                self.afc.error.AFC_error("Please home printer before doing a tool load", False, level=2)
                return False
        else:
            return True

    def is_homed(self):
        """
        Helper function to determine if printer is currently homed

        :return boolean: True if xyz is homed
        """
        curtime = self.afc.reactor.monotonic()
        kin_status = self.afc.toolhead.get_kinematics().get_status(curtime)
        if ('x' not in kin_status['homed_axes'] or 'y' not in kin_status['homed_axes'] or 'z' not in kin_status['homed_axes']):
            return False
        else:
            return True

    def is_moving(self):
        """
        Helper function to return if the printer is moving or not. This is different from `is_printing` as it will return true if anything in the printer is moving.

        :return boolean: True if anything in the printer is moving
        """
        eventtime = self.afc.reactor.monotonic()
        idle_timeout = self.printer.lookup_object("idle_timeout")
        return idle_timeout.get_status(eventtime)["state"] == "Printing"

    def in_print(self, return_file=False):
        """
        Helper function to help determine if printer is in a print by checking print_stats object. Printer is printing if state is not in standby or error

        :param return_file: Set to True to return current print filename if printer is in a print
        :return boolean: True if state is not standby or error
        """
        print_stats_idle_states = ['standby', 'error', 'complete', 'cancelled']
        eventtime = self.afc.reactor.monotonic()
        print_stats = self.printer.lookup_object("print_stats")
        print_state = print_stats.get_status(eventtime)["state"]

        in_print = print_state not in print_stats_idle_states
        if return_file:
            return in_print, print_stats.get_status(eventtime)["filename"]
        else:
            return in_print

    def is_printing(self, check_movement=False):
        """
        Helper function to return if the printer is printing an object.

        :param check_movement: When set to True will also return True if anything in the printer is also moving

        :return boolean: True if printer is printing an object or if printer is moving when `check_movement` is True
        """
        eventtime = self.afc.reactor.monotonic()
        print_stats = self.printer.lookup_object("print_stats")
        moving = False

        if check_movement:
            moving = self.is_moving()

        return print_stats.get_status(eventtime)["state"] == "printing" or moving

    def is_paused(self):
        """
        Helper function that returns true if printer is currently paused

        :return boolean: True when printer is paused
        """
        eventtime = self.afc.reactor.monotonic()
        pause_resume = self.printer.lookup_object("pause_resume")
        return bool(pause_resume.get_status(eventtime)["is_paused"])

    def get_current_lane(self):
        """
        Helper function to lookup current lane name loaded into active toolhead

        :return string: Current lane name that is loaded, None if nothing is loaded
        """
        if self.printer.state_message == 'Printer is ready':
            current_extruder = self.afc.toolhead.get_extruder().name
            if current_extruder in self.afc.tools:
                return self.afc.tools[current_extruder].lane_loaded
        return None

    def get_current_lane_obj(self):
        """
        Helper function to lookup and return current lane object that is loaded into the active toolhead

        :return object: None if nothing is loaded, AFC_stepper object if a lane is currently loaded
        """
        curr_lane_obj = None
        curr_lane = self.get_current_lane()
        if curr_lane in self.afc.lanes:
            curr_lane_obj = self.afc.lanes[curr_lane]
        return curr_lane_obj

    def verify_led_object(self, led_name):
        """
        Helper function to lookup AFC_led object.

        :params led_name: name of AFC_led object to lookup

        :return (string, object): error_string if AFC_led object is not found, led object if found
        """
        error_string = ""
        led = None
        afc_object = 'AFC_led {}'.format(led_name.split(':')[0])
        try:
            led = self.printer.lookup_object(afc_object)
        except:
            error_string = "Error: Cannot find [{}] in config, make sure led_index in config is correct for AFC_stepper {}".format(afc_object, led_name.split(':')[-1])
        return error_string, led

    def _get_led_indexes(self, index_values):
        """
        Helper function for creating a list for index values that have dashes and commas
        so the led's can be set correctly.

        eg. 1-4,9,10 would turn in to [1,2,3,4,9,10]

        :params index_value: String of index values to turn into a proper list
        :return list: list of index values for led's
        """
        led_indexes = []
        for idx in index_values.split(","):
            if "-" not in idx:
                led_indexes.append(int(idx))
            else:
                low, high = map(int, idx.split("-"))
                led_indexes += range(low, high+1)
        return led_indexes

    def afc_led (self, status, idx=None):
        if idx is None:
            return

        error_string, led = self.verify_led_object(idx)
        if led is not None:
            led_indexes = idx.split(":")[1]
            range_index = self._get_led_indexes(led_indexes)
            led.led_change(range_index, status)
        else:
            self.logger.info( error_string )

    def get_filament_status(self, cur_lane):
        if cur_lane.prep_state:
            if cur_lane.load_state:
                if cur_lane.extruder_obj is not None and cur_lane.extruder_obj.lane_loaded == cur_lane.name:
                    return 'In Tool:{}'.format(self.HexConvert(cur_lane.led_tool_loaded).split(':')[-1])
                return "Ready:{}".format(self.HexConvert(cur_lane.led_ready).split(':')[-1])
            return 'Prep:{}'.format(self.HexConvert(cur_lane.led_prep_loaded).split(':')[-1])
        return 'Not Ready:{}'.format(self.HexConvert(cur_lane.led_not_ready).split(':')[-1])

    def handle_activate_extruder(self):
        """
        Function used to deactivate lanes motors and buffers, then enables current extruders lane

        This will also be tied to a callback once multiple extruders are implemented
        """
        cur_lane_loaded = self.get_current_lane_obj()

        # Disable extruder steppers for non active lanes
        for key, obj in self.afc.lanes.items():
            if cur_lane_loaded is None or key != cur_lane_loaded.name:
                obj.do_enable(False)
                obj.disable_buffer()
                if obj.prep_state and obj.load_state:
                    self.afc_led(obj.led_ready, obj.led_index)
                else:
                    self.afc_led(obj.led_not_ready, obj.led_index)

        # Exit early if lane is None
        if cur_lane_loaded is None:
            self.afc.spool.set_active_spool('')
            return

        # Switch spoolman ID
        self.afc.spool.set_active_spool(cur_lane_loaded.spool_id)
        # Set lanes tool loaded led
        # TODO: Add check to see if users want to change status led to spool color if set
        # if cur_lane_loaded.color is not None and cur_lane_loaded.color:
        #     led_color = self.HexToLedString(cur_lane_loaded.color.replace("#", ""))
        #     self.afc_led( led_color, cur_lane_loaded.led_index )
        # else:
        cur_lane_loaded.unit_obj.lane_tool_loaded( cur_lane_loaded )
        # Enable stepper
        cur_lane_loaded.do_enable(True)
        # Enable buffer
        cur_lane_loaded.enable_buffer()

    def unset_lane_loaded(self):
        """
        Helper function to get current lane and unsync lane from toolhead extruder
        """
        cur_lane_loaded = self.get_current_lane_obj()
        if cur_lane_loaded is not None:
            cur_lane_loaded.unsync_to_extruder()
            cur_lane_loaded.set_unloaded()
            cur_lane_loaded.unit_obj.return_to_home()
            self.afc.function.handle_activate_extruder()
            self.logger.info("Manually removing {} loaded from toolhead".format(cur_lane_loaded.name))
            self.afc.save_vars()

    def select_loaded_lane(self):
        """
        Function looks up what the current lane loaded is and calls a common `select_lane` function so
        that units that have selectors this makes sure the correct lane is selected if user moves other
        lanes outside of printing
        """
        current_lane = self.get_current_lane_obj()
        if current_lane is not None:
            current_lane.unit_obj.select_lane(current_lane)

    def log_toolhead_pos(self, move_pre=""):
        """
        Helper function for printing position data to log

        :param move_pre: String that get appended before the position data
        """
        msg = "{}Position: {}".format(move_pre, self.afc.toolhead.get_position())
        msg += " base_position: {}".format(self.afc.gcode_move.base_position)
        msg += " last_position: {}".format(self.afc.gcode_move.last_position)
        msg += " speed: {}".format(self.afc.gcode_move.speed)
        msg += " speed_factor: {}".format(self.afc.gcode_move.speed_factor)
        msg += " extrude_factor: {}".format(self.afc.gcode_move.extrude_factor)
        msg += " absolute_coord: {}".format(self.afc.gcode_move.absolute_coord)
        msg += " absolute_extrude: {}\n".format(self.afc.gcode_move.absolute_extrude)
        self.logger.debug(msg, only_debug=True)

    def check_absolute_mode( self, func_name:str="" ):
        """
        Function to verifies that coordinates and extruder is in absolute mode, sets back to absolute mode
        if relative mode is set

        :params func_name: String for name of function that function is being called from,
                           this is added to debug log to aid in debugging
        """
        # Verify that printer is in absolute mode, and set True if in relative mode to prevent out of bound moves
        self.log_toolhead_pos("{}: check absolute mode, POS:".format(func_name))
        if not self.afc.gcode_move.absolute_coord:
            self.logger.debug("Printer coords not in absolute mode, setting to absolute mode")
            self.afc.gcode_move.absolute_coord = True
        if not self.afc.gcode_move.absolute_extrude:
            self.logger.debug("Printer extruder not in absolute mode, setting to absolute mode")
            self.afc.gcode_move.absolute_extrude = True

    def get_extruder_pos(self, eventtime=None, past_extruder_position=None):
        """
        This function find the last position of the filament and only returns a value if it greater than
        the previous passed in position.

        :param eventtime: Current eventtime to calculate the position from, if time is not passed in uses current eventtime
        :param past_extruder_position: Previous extruder position to compare current position against.
        :return float: Returns current extruder position if its greater than previous position, else returns previous position
        """
        if eventtime is None:
            eventtime = self.afc.reactor.monotonic()
        print_time = self.mcu.estimated_print_time(eventtime)
        extruder = self.afc.toolhead.get_extruder()
        last_extruder_position = extruder.find_past_position(print_time)

        if past_extruder_position is None or last_extruder_position > past_extruder_position:
            past_extruder_position = last_extruder_position
            # if last_extruder_position > 0: self.logger.debug("Extruder last position: {}".format(last_extruder_position))
            return last_extruder_position
        else:
            return past_extruder_position

    def check_for_td1_error(self, serial_number=None, print_error=True):
        '''
        Function checks to see if all or specific TD-1 devices found have any reported errors

        If specific serial number is provided, this function will only check that specific number for an error

        :param serial_number: Specific serial number to check for error
        :param print_error: Prints error message to logger if set to True
        :return bool,str: Returns tuple of True/False, error message if error occurred
        '''
        error_occurred = False
        error_message = ""
        td1_data = self.afc.moonraker.get_td1_data()
        for serial in td1_data:
            error = td1_data[serial].get("error")
            if error is not None:
                if serial_number is None or serial == serial_number:
                    error_message = f"Error with TD-1 Serial: {serial}, please fix error with TD-1 and run 'AFC_RESET_TD1 SERIAL={serial}' macro.\n"
                    error_message += "Some errors can occur when first booting machine and filament is in TD-1 device\n"
                    error_message += f"Reported Error: {td1_data[serial]['error']}"
                    error_occurred = True
                    if print_error:
                        self.logger.error(error_message)
        return error_occurred, error_message

    def check_for_td1_id(self, serial_number):
        """
        Function checks if specific serial number exists, if serial number exists checks to make sure
        that specific device does not have any errors.

        :param serial_number: Serial number to check for
        :return bool,str: bool: True if device exists/False if device does not exist or error with device
                          str: error message if device does not exist or an error with TD-1 device
        """
        td1_data = self.afc.moonraker.get_td1_data()
        if serial_number not in td1_data:
            return False, f"TD-1 Device ID ({serial_number}) supplied but ID not found."

        no_error, error_message = self.check_for_td1_error(serial_number, print_error=False)
        return not no_error, error_message

    def gcode_get_value( self, gcmd, get_attr, variable, variable_name, section_name, key_name=None, cast_to_bool=False ):
        """
        Helper type function to get values for macros. This function will also use passed in variable
        as default value. If user passed in a new value for variable_name, then config file is updated
        with new value. Do not call this function if a macro variable is required.

        :param gcmd: Klipper gcode command
        :param get_attr: gcode command get function type. Can only be the following: get, get_int, get_float
        :param variable: Current variable value to use as default
        :param variable_name: Variable name to get from gcode command
        :param section_name: Section name to save variable to if new value is different from old value
        :param key_name: Key name to save value to
        :param cast_to_bool: Set to True to case int to boolean
        :return value: New or current value from gcode command
        """
        if not hasattr(gcmd, get_attr):
            self.logger.error(f"{get_attr} is not a value GCodeCommand function")
            return variable

        old_value = variable
        new_value = getattr( gcmd, get_attr)(variable_name, variable)

        if key_name is None:
            key_name = variable_name.lower()

        if cast_to_bool: new_value = bool(new_value)

        if old_value != new_value:
            self.logger.info(f"Updating {key_name}, New: {new_value} Old: {old_value}")
            self.ConfigRewrite(section_name, key_name, new_value)

        return new_value

    def HexConvert(self,tmp):
        led=tmp.split(',')
        if float(led[0])>0:
            led[0]=int(255*float(led[0]))
        else:
            led[0]=0
        if float(led[1])>0:
            led[1]=int(255*float(led[1]))
        else:
            led[1]=0
        if float(led[2])>0:
            led[2]=int(255*float(led[2]))
        else:
            led[2]=0

        return '#{:02x}{:02x}{:02x}'.format(*led)

    def HexToLedString(self, led_value):
        """
        Helper function for turning a hex value into a comma seperated list

        :param led_value: Hex color value
        :return list: List of comma seperated float values ranging from 0.0 to 1.0
        """
        n = 2
        new_value = [ int(led_value[i:i+n], base=16)/255.0 for i in range(0, len(led_value), n)]
        if led_value == "FFFFFF":
            new_value.append(1.0)
        else:
            new_value.append(0.0)
        return new_value

    def _create_options(self, macro_name, options):
        option_str = ""

        for key, value in options.items():
            option_str += f"{{%set dummy=params.{key}|default('{value['default']}')|{value['type']}%}}\n"

        option_str += f"_{macro_name} {{rawparams}}"
        return option_str

    def _create_no_options(self, macro_name):
        return f"_{macro_name}"

    # Modified from the ShakeTune project
    def register_mux_command(self, show_macros, macro_name, key, value, command, description, options=None):
        gcode = self.printer.lookup_object('gcode')

        # Register AFC macro commands using the official Klipper API (gcode.register_command)
        # Doing this makes the commands available in Klipper, but they are not shown in the web interfaces
        # and are only available by typing the full name in the console (like all the other Klipper commands)
        # for name, command, description in afc_commands:
        gcode.register_mux_command(f'_{macro_name}' if show_macros else macro_name, key, value, command,
                                   desc=description)
        self._register_klipper(show_macros, macro_name, command, description, options)

    # Modified from the ShakeTune project
    def register_commands(self, show_macros, macro_name, command, description, options=None):
        gcode = self.printer.lookup_object('gcode')

        # Register AFC macro commands using the official Klipper API (gcode.register_command)
        # Doing this makes the commands available in Klipper, but they are not shown in the web interfaces
        # and are only available by typing the full name in the console (like all the other Klipper commands)
        # for name, command, description in afc_commands:
        gcode.register_command(f'_{macro_name}' if show_macros else macro_name, command, desc=description)

        self._register_klipper(show_macros, macro_name, command, description, options)

    # Modified from the ShakeTune project
    def _register_klipper(self, show_macros, macro_name, command, description, options=None):
        # Then, a hack to inject the macros into Klipper's config system in order to show them in the web
        # interfaces. This is not a good way to do it, but it's the only way to do it for now to get
        # a good user experience while using AFC (it's indeed easier to just click a macro button)
        if show_macros:
            name = f'gcode_macro {macro_name}'
            if not self.config.fileconfig.has_section(name):
                self.config.fileconfig.add_section(name)
                self.config.fileconfig.set(name, 'description', description)
                if options is not None:
                    self.config.fileconfig.set(name, 'gcode', self._create_options(macro_name, options))
                else:
                    self.config.fileconfig.set(name, 'gcode', self._create_no_options(macro_name))

                for option in self.config.fileconfig.options(name):
                    self.config.access_tracking[(name.lower(), option.lower())] = 1
            self.printer.load_object(self.config, name)

    def _safe_extrude(self, amount_mm, feedrate=100):
        """
        Helper function to safely extrude a given amount of filament, checking if the lane is loaded and
        if the toolhead is in absolute mode.

        :param amount_mm: Amount of filament to extrude in mm
        :param feedrate: Feedrate for extrusion in mm/min
        """
        self.afc.gcode.run_script_from_command("M400")  # Finish queued moves
        self.afc.gcode.run_script_from_command("M83")  # Relative extrusion mode
        self.afc.gcode.run_script_from_command("G92 E0")  # Zero extruder
        self.afc.logger.info(f"Extruding {amount_mm}mm")
        self.afc.gcode.run_script_from_command(f"G1 E{amount_mm} F{feedrate}")
        self.afc.gcode.run_script_from_command("M82")

    cmd_AFC_TEST_LANES_help = 'Run load/unload tests on specified lanes'
    def cmd_AFC_TEST_LANES(self, gcmd):
        """
        Run load/unload tests on specified lanes. This command allows users to test the loading and unloading
        mechanisms to serve as a stress test / diagnostic tool for the AFC system with an interactive prompt.

        Usage
        -----
        `AFC_TEST_LANES`

        Example
        -----
        ```
        AFC_TEST_LANES
        ```
        """

        no_lanes_loaded = not any(lane.load_state for lane in self.afc.lanes.values())

        if no_lanes_loaded:
            self.logger.info('No lanes loaded, please load lanes before running tests.')
            return

        if self.afc.current is not None:
            self.logger.info('Toolhead must be unloaded to run tests.')
            return

        prompt = AFCprompt(gcmd, self.logger)
        buttons = []
        title = 'AFC Test Lanes'
        text = ('The following prompts will allow you to test the loading and unloading of your lanes.'
                ' Lanes must be loaded before running tests.')

        for index, (key, item) in enumerate(self.afc.lanes.items()):
            if item.load_state and item.prep_state:
                button_label = '{}'.format(key)
                button_command = 'TEST_LANE_ITERATIONS LANE={}'.format(key)
                button_style = "primary" if index % 2 == 0 else "secondary"
                buttons.append((button_label, button_command, button_style))

        bow_footer = [("All Lanes", "TEST_LANE_ITERATIONS LANE=all", "primary")]
        prompt.create_custom_p(title, text, buttons,
                               True, None, bow_footer)

    cmd_TEST_LANE_ITERATIONS_help = 'Specify number of iterations for lane tests'
    def cmd_TEST_LANE_ITERATIONS(self, gcmd):
        """
        This function creates a prompt to specify the number of iterations for lane tests.
        """

        prompt = AFCprompt(gcmd, self.logger)
        buttons = []
        lane = gcmd.get('LANE', None)
        title = 'Iteration Count'
        text = ('How many iterations would you like to run?\n'
                'Note: This will run # of iterations * each lane selected.\n'
                'Please let all iterations run to completion before using any commands.')
        iteration_max = 5
        for iteration in range(1, iteration_max + 1):
            button_label = "{}".format(iteration)
            button_style = "primary" if iteration % 2 == 0 else "secondary"
            button_command = "TEST_LANE LANE={} ITERATION={}".format(lane, iteration)
            buttons.append((button_label, button_command, button_style))

        prompt.create_custom_p(title, text, buttons, True, None, None)

    cmd_TEST_LANE_help = 'Run load/unload tests on specified lane(s)'
    def cmd_TEST_LANE(self, gcmd):
        """
        This function runs a load/unload test on a specified lane or all lanes.

        Usage
        -----
        `TEST_LANE LANE=<lane_name> ITERATION=<number_of_iterations>`

        Example
        -----
        ```
        TEST_LANE LANE=lane1 ITERATION=3
        ```
        ```
        TEST_LANE LANE=all ITERATION=5
        ```
        """

        iterations = gcmd.get_int('ITERATION', 1)
        lane = gcmd.get('LANE', None)

        prompt = AFCprompt(gcmd, self.logger)
        prompt.p_end()

        if lane is not None:
            self.afc.gcode.run_script_from_command('AFC_PARK')
            self.logger.info('Starting test for lane(s): {}'.format(lane))
            lane_obj = self.afc.lanes.get(lane)
            if lane != 'all':
                self.afc.logger.info('Running {} iterations for lane: {}'.format(iterations, lane))
                for _ in range(iterations):
                    self.afc.logger.info('Loading lane: {}'.format(lane))
                    self.afc.CHANGE_TOOL(lane_obj)
                    if not self.afc.error_state:
                        self.afc.logger.info("Lane {} loaded successfully".format(lane))
                    else:
                        self.afc.logger.error("Failed to load lane {}".format(lane))
                        self.afc.error.reset_failure()
                        break
                    self._safe_extrude(self.afc.test_extrude_amt)
                    self.logger.info("Unloading lane {}".format(lane))
                    self.afc.TOOL_UNLOAD(lane_obj)
                    if not self.afc.error_state:
                        self.afc.logger.info("Lane {} unloaded successfully".format(lane))
                    else:
                        self.afc.logger.error("Failed to unload lane {}".format(lane))
                        self.afc.error.reset_failure()
                        return


            else:
                loaded_lanes = [item for key, item in self.afc.lanes.items() if item.load_state and item.prep_state]
                self.afc.logger.info('Running {} iterations for all loaded lanes'.format(iterations))
                for i in range(iterations):
                    random.shuffle(loaded_lanes)
                    for lane_obj in loaded_lanes:
                        self.afc.logger.info('Loading lane: {}'.format(lane_obj))
                        self.afc.CHANGE_TOOL(lane_obj)
                        if not self.afc.error_state:
                            self.afc.logger.info("Lane {} loaded successfully".format(lane_obj))
                        else:
                            self.afc.logger.error("Failed to load lane {}".format(lane_obj))
                            self.afc.error.reset_failure()
                            return
                        self._safe_extrude(self.afc.test_extrude_amt)
                        self.logger.info("Unloading lane {}".format(lane_obj))
                    if i == iterations - 1:
                        self.afc.logger.info(
                            "Finished testing with {} iterations for all loaded lanes".format(iterations)
                        )
                        self.afc.TOOL_UNLOAD(lane_obj)
        prompt.p_end()


    cmd_AFC_CALIBRATION_help = 'Open prompt to begin calibration by selecting Unit to calibrate'
    def cmd_AFC_CALIBRATION(self, gcmd):
        """
        Open a prompt to start AFC calibration by selecting a unit to calibrate. Creates buttons for each unit and
        allows the option to calibrate all lanes across all units.

        Usage
        -----
        `AFC_CALIBRATION`

        Example
        -----
        ```
        AFC_CALIBRATION
        ```
        """
        prompt = AFCprompt(gcmd, self.logger)
        buttons = []
        title = 'AFC Calibration'
        text = ('The following prompts will lead you through the calibration of your AFC unit(s).'
                ' First, select a unit to calibrate.'
                ' *All values will be automatically updated in the appropriate config sections.')
        for index, (key, item) in enumerate(self.afc.units.items()):
            # Create a button for each unit
            button_label = "{}".format(key)
            button_command = 'UNIT_CALIBRATION UNIT={}'.format(key)
            button_style = "primary" if index % 2 == 0 else "secondary"
            buttons.append((button_label, button_command, button_style))

        bow_footer = [("All Lanes in all units", "ALL_CALIBRATION", "secondary")]
        prompt.create_custom_p(title, text, buttons,
                               True, None, bow_footer)

    cmd_ALL_CALIBRATION_help = 'open prompt to begin calibration to confirm calibrating all lanes'
    def cmd_ALL_CALIBRATION(self, gcmd):
        """
        Open a prompt to confirm calibration of all lanes in all units. Provides 'Yes' to confirm and 'Back' to
        return to the previous menu.

        Usage
        -----
        `ALL_CALIBRATION`

        Example
        -----
        ```
        ALL_CALIBRATION
        ```
        """
        prompt = AFCprompt(gcmd, self.logger)
        footer = []
        title = 'Calibrate all'
        text = 'Press Yes to confirm calibrating all lanes in all units'
        footer.append(('Back', 'AFC_CALIBRATION', 'info'))
        footer.append(("Yes", "CALIBRATE_AFC LANE=all", "error"))

        prompt.create_custom_p(title, text, None,
                               True, None, footer)


    cmd_CALIBRATE_AFC_help = 'calibrate the dist hub for lane and then afc_bowden_length'
    def cmd_CALIBRATE_AFC(self, gcmd):
        """
        This function performs the calibration of the hub and Bowden length for one or more lanes within an AFC
        (Automated Filament Control) system. The function uses precise movements to adjust the positions of the
        steppers, check the state of the hubs and tools, and calculate distances for calibration based on the
        user-provided input. If no specific lane is provided, the function defaults to notifying the user that no lane has been selected. The function also includes
        the option to calibrate the Bowden length for a particular lane, if specified.

        Parameters:
        - LANES: Specifies the lane to calibrate. If not provided, calibrates no lanes.
        - DISTANCE: The distance to move during calibration (optional, defaults to 25mm).
        - TOLERANCE: The tolerance for fine adjustments during calibration (optional, defaults to 5mm).
        - BOWDEN: Specifies the lane to perform Bowden length calibration (optional).
        - UNIT: Specifies the unit to be used in calibration (optional)

        Usage
        -----
        `CALIBRATE_AFC LANE=<lane> DISTANCE=<distance> TOLERANCE=<tolerance> BOWDEN=<lane>`

        Example
        -----
        ```
        CALIBRATE_AFC LANE=all Bowden=lane1 DISTANCE=30 TOLERANCE=3
        ```
        ```
        CALIBRATE_AFC BOWDEN=lane1` (Calibrates the Bowden length for 'lane1')
        ```
        """
        prompt = AFCprompt(gcmd, self.logger)
        dis    = gcmd.get_float('DISTANCE' , 25)
        tol    = gcmd.get_float('TOLERANCE', 5)
        afc_bl = gcmd.get(      'BOWDEN'   , None)
        lanes  = gcmd.get(      'LANE'     , None)
        unit   = gcmd.get(      'UNIT'     , None)
        td1    = gcmd.get(      'TD1'      , None)

        prompt.p_end()

        if self.afc.current is not None:
            self.logger.info('Tool must be unloaded to calibrate system')
            return

        calibrated  = []
        checked     = False
        title       = "AFC Calibration"

        # Check to make sure lane and unit is valid
        if lanes is not None and lanes != 'all' and lanes not in self.afc.lanes:
            self.afc.error.AFC_error("'{}' is not a valid lane".format(lanes), pause=False)
            return

        if unit is not None and unit not in self.afc.units:
            self.afc.error.AFC_error("'{}' is not a valid unit".format(unit), pause=False)
            return

        if afc_bl is not None and afc_bl not in self.afc.lanes:
            self.afc.error.AFC_error("'{}' is not a valid lane to calibrate bowden length".format(afc_bl), pause=False)
            return

        if td1 is not None and not self.afc.td1_present:
            self.afc.error.AFC_error("TD-1 is not present, will not be able to calibrate bowden length for TD-1", pause=False)

        # Determine if a specific lane is provided
        if lanes is not None:
            self.logger.info('Starting AFC distance Calibrations')
            if unit is None:
                if lanes != 'all':
                    cur_lane = self.afc.lanes[lanes]
                    checked, msg, pos = cur_lane.unit_obj.calibrate_lane(cur_lane, tol)
                    if(not checked):
                        self.afc.error.AFC_error(msg, False)
                        if pos > 0:
                            self.afc.gcode.run_script_from_command('AFC_CALI_FAIL FAIL={} DISTANCE={}'.format(cur_lane, pos))
                        return
                    else: calibrated.append(lanes)
                else:
                    # Calibrate all lanes if no specific lane is provided
                    for cur_lane in self.afc.lanes.values():
                        if not cur_lane.load_state or not cur_lane.prep_state:
                            self.logger.info("{} not loaded skipping to next loaded lane".format(cur_lane.name))
                            continue
                        # Calibrate the specific lane
                        checked, msg, pos = cur_lane.unit_obj.calibrate_lane(cur_lane, tol)
                        if(not checked):
                            self.afc.error.AFC_error(msg, False)
                            self.afc.gcode.run_script_from_command('AFC_CALI_FAIL FAIL={} DISTANCE={}'.format(cur_lane, pos))
                            return
                        else: calibrated.append(cur_lane.name)
            else:
                if lanes != 'all':
                    cur_lane = self.afc.lanes[lanes]
                    checked, msg, pos = cur_lane.unit_obj.calibrate_lane(cur_lane, tol)
                    if(not checked):
                        self.afc.error.AFC_error(msg, False)
                        self.afc.gcode.run_script_from_command('AFC_CALI_FAIL FAIL={} DISTANCE={}'.format(cur_lane, pos))
                        return
                    else: calibrated.append(lanes)
                else:
                    CUR_UNIT = self.afc.units[unit]
                    self.logger.info('{}'.format(CUR_UNIT.name))
                    # Calibrate all lanes if no specific lane is provided
                    for cur_lane in CUR_UNIT.lanes.values():
                        if not cur_lane.load_state or  not cur_lane.prep_state:
                            self.logger.info("{} not loaded skipping to next loaded lane".format(cur_lane.name))
                            continue
                        # Calibrate the specific lane
                        checked, msg, pos = CUR_UNIT.calibrate_lane(cur_lane, tol)
                        if(not checked):
                            self.afc.error.AFC_error(msg, False)
                            self.afc.gcode.run_script_from_command('AFC_CALI_FAIL FAIL={} DISTANCE={}'.format(cur_lane, pos))
                            return
                        else: calibrated.append(cur_lane.name)

            self.logger.info("Lane calibration Done!")

        else:
            self.logger.info('No lanes selected to calibrate dist_hub')

        # Calibrate Bowden length with specified lane
        if afc_bl is not None:
            set_tool_start_back_to_none = False
            cur_lane=self.afc.lanes[afc_bl]

            # Setting tool start to buffer if only tool_end is set and user has buffer so calibration can run
            if cur_lane.extruder_obj.tool_start is None:
                if cur_lane.extruder_obj.tool_end is not None and cur_lane.buffer_obj is not None:
                    self.logger.info("Cannot run calibration using post extruder sensor, using buffer to calibrate bowden length")
                    cur_lane.extruder_obj.tool_start = "buffer"
                    set_tool_start_back_to_none = True
                else:
                    # Cannot calibrate
                    self.afc.error.AFC_error("Cannot calibrate with only post extruder sensor and no turtleneck buffer defined in config", pause=False)
                    return

            self.logger.info('Starting AFC distance Calibrations')

            checked, msg, pos = cur_lane.unit_obj.calibrate_bowden(cur_lane, dis, tol)
            if not checked:
                self.afc.error.AFC_error('{} failed to calibrate bowden length {}'.format(afc_bl, msg), pause=False)
                self.afc.gcode.run_script_from_command('AFC_CALI_FAIL FAIL={} DISTANCE={}'.format(afc_bl, pos))
                return
            else: calibrated.append('Bowden_length:_{}'.format(afc_bl))

            self.logger.info("Bowden length calibration Done!")

            if set_tool_start_back_to_none:
                cur_lane.extruder_obj.tool_start = None

        # Calibration for TD-1 bowden length
        if td1 is not None:
            title = "TD-1 Calibration"
            td1_lane = self.afc.lanes[td1]
            if td1_lane.hub_obj.state:
                msg = f"{td1_lane.hub_obj.name} hub is triggered, make sure hub is clear before trying to calibrate TD-1 bowden length"
                self.afc.error.AFC_error(msg, pause=False)
                self.afc.gcode.run_script_from_command(f"AFC_CALI_FAIL TITLE='{title} Failed' FAIL={td1} DISTANCE=0 msg='{msg}' RESET=0")
                return

            checked, msg, pos = td1_lane.unit_obj.calibrate_td1( td1_lane, dis, tol)
            if not checked:
                fail_string = f"{td1} failed to calibrate bowden length {msg}"
                self.afc.error.AFC_error(fail_string, pause=False)
                self.afc.gcode.run_script_from_command(f"AFC_CALI_FAIL TITLE='{title} Failed' FAIL={td1} DISTANCE={pos} msg='{fail_string}' RESET=1")
                return
            else:
                calibrated.append(f"'TD1_Bowden_length: {td1}'")

        if checked:
            lanes_calibrated = ','.join(calibrated)
            self.afc.gcode.run_script_from_command(f"AFC_CALI_COMP TITLE='{title} Completed' CALI={lanes_calibrated}")

    cmd_AFC_CALI_COMP_help = 'Opens prompt after calibration is complete'
    def cmd_AFC_CALI_COMP(self, gcmd):
        """
        This function handles the completion of the AFC calibration process by displaying a prompt to the user, asking
        whether they want to perform more calibrations.

        Usage
        -----
        `AFC_CALI_COMP CALI=<calibration context>`

        Examples
        -----
        ```
        AFC_CALI_COMP CALI=lane1
        ```
        (Shows a prompt indicating that calibration for 'lane1' has been completed)
        """

        cali = gcmd.get("CALI", None)
        title = gcmd.get("TITLE", "AFC Calibration Completed")

        prompt = AFCprompt(gcmd, self.logger)
        buttons = []
        text = 'Calibration was completed for {}, would you like to do more calibrations?'.format(cali)
        buttons.append(("Yes", "AFC_Calibration", "primary"))
        buttons.append(("No", "AFC_HAPPY_P STEP='AFC Calibration'", "info"))

        prompt.create_custom_p(title, text, buttons,
                               True, None)

    cmd_AFC_HAPPY_P_help = 'Opens prompt after calibration is complete'
    def cmd_AFC_HAPPY_P(self, gcmd):
        """
        This function opens a prompt after calibration is complete, displaying a message to the user that the calibration
        step has been successfully completed.

        Usage: `AFC_HAPPY_P STEP=<step>`

        Examples:
            - `AFC_HAPPY_P STEP='AFC Calibration lane3'` (Shows the completion message for AFC Calibration)

        Args:
            gcmd: The G-code command object containing the parameters for the command.
                Parameters:
                - STEP: Specifies the step that has been completed (e.g., AFC Calibration, Extruder Calibration).

        Returns:
            None
        NO_DOC: True
        """

        step = gcmd.get("STEP", None)

        prompt = AFCprompt(gcmd, self.logger)
        buttons = None
        footer = []
        title = '{} Completed'.format(step)
        text = 'Happy Printing!'
        prompt.create_custom_p(title, text, buttons,
                               False, None, footer)
        self.afc.reactor.pause(self.afc.reactor.monotonic() + 3)
        self.afc.gcode.respond_raw("// action:prompt_end")

    cmd_AFC_CALI_FAIL_help = 'Opens prompt after calibration fails'
    def cmd_AFC_CALI_FAIL(self, gcmd):
        """
        This function opens a prompt after an AFC calibration failure. It informs the user about the failure and provides
        instructions to reset the lane and review the error messages in the console. The user is prompted to take corrective
        action and re-run the calibration.

        Usage: `AFC_CALI_FAIL FAIL=<lane> DISTANCE=<distance>`

        Examples:
            - `AFC_CALI_FAIL FAIL=lane1 DISTANCE=30` (Indicates that the calibration for lane1 failed at a 30mm distance)

        Args:
            gcmd: The G-code command object containing the parameters for the command.
                Parameters:
                - FAIL: Specifies the lane where the calibration failed.
                - DISTANCE: The distance value that caused the failure (optional).
                - RESET: Set to 1 to display reset lane message
                - TITLE: Dynamic title to set in prompt
                - MSG: Dynamic message to display in prompt

        Returns:
            None
        NO_DOC: True
        """

        cali            = gcmd.get("FAIL", None)
        dis             = gcmd.get("DISTANCE", None)
        reset_lane      = bool(gcmd.get_int("RESET", 1))
        title           = gcmd.get("TITLE", "AFC Calibration Failed")
        fail_message    = gcmd.get("MSG", "")

        prompt = AFCprompt(gcmd, self.logger)
        buttons = []
        footer = []
        text = f'{title} for {cali}. '
        if reset_lane:
            text += 'First: reset lane, Second: review messages in console and take necessary action and re-run calibration.'
            buttons.append(("Reset lane", "AFC_LANE_RESET LANE={} DISTANCE={}".format(cali, dis), "primary"))

        if fail_message:
            text += f" Fail message: {fail_message}"

        footer.append(('EXIT', 'prompt_end', 'info'))

        prompt.create_custom_p(title, text, buttons,
                               True, None)

    cmd_AFC_RESET_help = 'Opens prompt to select lane to reset.'
    cmd_AFC_RESET_options = {"DISTANCE": {"default": "30", "type": "float"}}
    def cmd_AFC_RESET(self, gcmd):
        """
        This function opens a prompt allowing the user to select a loaded lane for reset. It displays a list of loaded lanes
        and provides a reset button for each lane. If no lanes are loaded, an informative message is displayed indicating
        that a lane must be loaded to proceed with resetting.

        Usage
        -----
        `AFC_RESET DISTANCE=<distance>`

        Example
        -----
        (Shows the prompt for resetting lanes with a distance value of 30mm)
        ```
        AFC_RESET DISTANCE=30
        ```
        (Shows the prompt for resetting lanes without specifying a distance)
        ```
        AFC_RESET
        ```
        """

        prompt = AFCprompt(gcmd, self.logger)
        dis = gcmd.get("DISTANCE", None)
        buttons = []
        title = 'AFC RESET'
        text = 'Select a loaded lane to reset'

        # Create buttons for each loaded lane
        for index, LANE in enumerate(self.afc.lanes.values()):
            if LANE.load_state:
                button_label = "{}".format(LANE.name)
                if dis is not None:
                    button_command = "AFC_LANE_RESET LANE={} DISTANCE={}".format(LANE.name, dis)
                else:
                    button_command = "AFC_LANE_RESET LANE={}".format(LANE.name)

                button_style = "primary" if index % 2 == 0 else "secondary"
                buttons.append((button_label, button_command, button_style))

        total_buttons = sum(len(group) for group in buttons)
        if total_buttons == 0:
            text = 'No lanes are loaded, a lane must be loaded to be reset'

        prompt.create_custom_p(title, text, buttons,
                        True, None)

    cmd_AFC_LANE_RESET_help = 'reset a loaded lane to hub'
    cmd_AFC_LANE_RESET_options = {"DISTANCE": {"default": "50", "type": "float"},
                                  "LANE": {"default": "lane1", "type": "string"}}
    def cmd_AFC_LANE_RESET(self, gcmd):
        """
        This function resets a specified lane to the hub position in the AFC system. It checks for various error conditions,
        such as whether the toolhead is loaded or whether the hub is already clear. The function moves the lane back to the
        hub based on the specified or default distances, ensuring the lane's correct state before completing the reset.

        Usage
        -----
        `AFC_LANE_RESET LANE=<lane> DISTANCE=<distance>`

        Example
        -----
        (Resets lane1 to the hub with a move of 50mm)
        ```
        AFC_LANE_RESET LANE=lane1 DISTANCE=50
        ```
        (Resets lane2 to the hub using default settings)
        ```
        AFC_LANE_RESET LANE=lane2
        ```
        """

        prompt = AFCprompt(gcmd, self.logger)
        lane = gcmd.get('LANE', None)
        long_dis = gcmd.get('DISTANCE', None)

        if not lane:
            prompt.p_end()
            self.afc.error.AFC_error("No lane selected to reset, please provide a lane to reset.", pause=False)
            return

        if lane not in self.afc.lanes:
            prompt.p_end()
            self.afc.error.AFC_error("'{}' is not a valid lane".format(lane), pause=False)
            return

        if long_dis is not None:
            try:
                long_dis = float(long_dis)
                if long_dis <= 0:
                    raise ValueError("DISTANCE must be a positive number.")
            except (ValueError, TypeError):
                prompt.p_end()
                self.afc.error.AFC_error("DISTANCE must be a valid number.", pause=False)
                return

        cur_lane = self.afc.lanes[lane]
        CUR_HUB = cur_lane.hub_obj
        short_move = cur_lane.short_move_dis * 2

        if not CUR_HUB.state:
            prompt.p_end()
            self.afc.error.AFC_error("Hub is already clear while trying to reset '{}'".format(lane), pause=False)
            return

        if (tool_load := self.get_current_lane_obj()) is not None:
            prompt.p_end()
            self.afc.error.AFC_error("Toolhead is loaded with '{}', unload or check sensor before resetting lane".format(tool_load.name), pause=False)

        prompt.p_end()
        self.afc.gcode.respond_info('Resetting {} to hub'.format(lane))
        pos = 0
        fail_state_msg = "'{}' failed to reset to hub, {} switch became false during reset"

        if long_dis is not None:
            cur_lane.move(float(long_dis) * -1, cur_lane.long_moves_speed, cur_lane.long_moves_accel, True)

        while CUR_HUB.state:
            cur_lane.move(short_move * -1, cur_lane.short_moves_speed, cur_lane.short_moves_accel, True)
            pos -= short_move

            if not cur_lane.load_state:
                self.afc.error.AFC_error(fail_state_msg.format(cur_lane, "load"), pause=False)
                return

            if not cur_lane.prep_state:
                self.afc.error.AFC_error(fail_state_msg.format(cur_lane, "prep"), pause=False)
                return

            if abs(pos) >= CUR_HUB.afc_bowden_length:
                self.afc.error.AFC_error("'{}' failed to reset to hub".format(cur_lane), pause=False)
                return

        cur_lane.move(CUR_HUB.move_dis * -1, cur_lane.short_moves_speed, cur_lane.short_moves_accel, True)
        cur_lane.loaded_to_hub = True
        cur_lane.do_enable(False)

        self.afc.gcode.respond_info('{} reset to hub, take necessary action'.format(lane))

    cmd_AFC_GET_TD_ONE_DATA_help = "Display's prompt to easily get TD-1 data for lanes"
    def cmd_AFC_GET_TD_ONE_DATA(self, gcmd):
        """
        This macro displays a prompt to select which lane or all lanes to gather TD-1 data from.

        Calibrating TD-1 bowden length with AFC_CALIBRATION should be ran first before calling this macro.

        Usage
        -----
        `AFC_GET_TD_ONE_DATA`

        Example
        -----
        ```
        AFC_GET_TD_ONE_DATA
        ```
        """
        prompt = AFCprompt(gcmd, self.logger)
        buttons = []
        group_buttons = []
        index = 0
        title = 'Capture TD-1 Data'
        text  = ('Select a loaded lane to capture TD-1 data, or select ALL. ')

        # Create buttons for each lane and group every 4 lanes together
        for lane in self.afc.lanes.values():
            if lane.load_state:
                button_label = "{}".format(lane)
                button_command = "AFC_GET_TD_ONE_LANE_DATA LANE={}".format(lane)
                button_style = "primary" if index % 2 == 0 else "secondary"
                group_buttons.append((button_label, button_command, button_style))

                # Add group to buttons list after every 4 lanes
                if (index + 1) % 2 == 0 or index == len(self.afc.lanes) - 1:
                    buttons.append(list(group_buttons))
                    group_buttons = []
                index += 1

        if group_buttons:
            buttons.append(list(group_buttons))

        total_buttons = sum(len(group) for group in buttons)
        if total_buttons > 1:
            all_lanes = [('All lanes', 'AFC_GET_TD_ONE_LANE_DATA LANE=all', "default")]
        else:
            all_lanes = None
        if total_buttons == 0:
            text = 'No lanes are loaded, please load before calibration'

        prompt.create_custom_p(title, text, all_lanes,
                               True, buttons, None)

    cmd_AFC_GET_TD_ONE_LANE_DATA_help = "Captures TD-1 for specified LANE"
    def cmd_AFC_GET_TD_ONE_LANE_DATA(self, gcmd):
        """
        This macro gets TD-1 data for the specified lane by moving filament the calibrated TD-1 length.
        Calibrating TD-1 bowden length with AFC_CALIBRATION should be ran first before calling this macro.

        Usage
        -----
        `AFC_GET_TD_ONE_LANE_DATA LANE=<lane>`

        Example
        -----
        ```
        AFC_GET_TD_ONE_LANE_DATA LANE=lane1
        ```

        NO_DOC: True
        """
        prompt = AFCprompt(gcmd, self.logger)
        lane = gcmd.get("LANE")
        lanes_captured = []
        prompt.p_end()
        if lane.lower() == "all":
            self.logger.info("Capturing TD-1 data for all lanes")
            for cur_lane in self.afc.lanes.values():
                if cur_lane.load_state and cur_lane.prep_state:
                    success, msg = cur_lane.get_td1_data()
                    if not success:
                        self.afc.gcode.run_script_from_command(f"AFC_CALI_FAIL TITLE='Get TD-1 data Failed' FAIL={cur_lane} DISTANCE=0 msg='{msg}' RESET=0")
                        return

            lanes_captured = "'TD-1 Data captured for all lanes'"
        else:
            if lane in self.afc.lanes:
                self.logger.info(f"Capturing TD-1 data for {lane}")
                cur_lane = self.afc.lanes[lane]
                success, msg = cur_lane.get_td1_data()
                if not success:
                    msg = msg.replace("\n", " ")
                    self.afc.gcode.run_script_from_command(f"AFC_CALI_FAIL TITLE='Get TD-1 data Failed' FAIL={cur_lane} DISTANCE=0 msg='{msg}' RESET=0")
                    return
                lanes_captured = f"'TD-1 Data captured for {cur_lane.name}'"
            else:
                fail_message = f"'{lane} not valid lane, cannot capture TD-1 data'"
                self.afc.error.AFC_error(fail_message, pause=False)
                self.afc.gcode.run_script_from_command(f"AFC_CALI_FAIL TITLE='Get TD-1 data Completed' FAIL={lane} msg='{fail_message}' RESET=0")
                return
        self.afc.gcode.run_script_from_command('AFC_HAPPY_P STEP={}'.format(lanes_captured))

    cmd_AFC_RESET_TD1_help = "Sends reboot command to specified TD-1 device"
    def cmd_AFC_RESET_TD1(self, gcmd):
        """
        This macro calls moonrakers api endpoint to reset specified TD-1 device by supplied serial number.

        Usage
        -----
        `AFC_RESET_TD1 SERIAL=<serial_number>`

        Example
        -----
        ```
        AFC_RESET_TD1 SERIAL=E6625877D318C430
        ```
        """
        serial_number = gcmd.get("SERIAL")
        timeout_count = 0

        self.logger.info(f"Sending reboot command to TD-1 serial: {serial_number}")
        status = self.afc.moonraker.reboot_td1(serial_number)
        if "status" in status:
            if status['status'] == "ok":
                self.logger.info("Waiting up to 30 seconds for device to reboot")
                while True:
                    data = self.afc.moonraker.get_td1_data()
                    if data is not None and serial_number in data:
                        error = data[serial_number].get("error")
                        if error is not None:
                            self.logger.error(f"Error still not fixed\nReported error: {data[serial_number]['error']}")
                            break
                        else:
                            self.logger.info(f"TD-1 serial:{serial_number} rebooted successfully without error")
                            break
                    elif timeout_count > 30:
                        self.logger.error("Timeout occurred when trying to verify TD-1 rebooted successfully")
                        break
                    timeout_count+= 1
                    self.afc.toolhead.dwell(1)
            elif status['status'] == "key_error":
                self.logger.error("An incorrect serial number was provided")
        else:
            self.logger.error("An error occurred when trying to send reboot command")

    def _calc_length(self, config_length, current_length, new_length):
        """
        Common function to calculate length for afc_bowden_length, afc_unload_bowden_length, and hub_dist

        :param config_length: Current configuration length that's in config file
        :param current_length: Current length for bowden or hub_dist
        :param new_length: New length to set, increase(+), decrease(-), or reset to config value

        :returns length: Calculated length value
        """
        length = 0.0

        if new_length.lower() == 'reset':
            length = config_length
        else:
            if new_length[0] in ('+', '-'):
                try:
                    bowden_value = float(new_length)
                    length = current_length + bowden_value
                except ValueError:
                    length = current_length
                    self.logger.error("Invalid length: {}".format(new_length))
            else:
                length = float(new_length)

        return length

    cmd_SET_BOWDEN_LENGTH_help = "Helper to dynamically set length of bowden between hub and toolhead. Pass in HUB if using multiple box turtles"
    def cmd_SET_BOWDEN_LENGTH(self, gcmd):
        """
        This function adjusts the length of the Bowden tube between the hub and the toolhead.
        It retrieves the hub specified by the 'HUB' parameter and the length adjustment specified
        by the 'LENGTH' parameter. UNLOAD_LENGTH adjusts unload Bowden length. If the hub is not specified
        and a lane is currently loaded, it uses the hub of the current lane. To reset length back to config
        value, pass in `reset` for each length to reset to value in config file. Adding +/- in front of the
        length will increase/decrease bowden length by that amount.

        Usage
        -----
        `SET_BOWDEN_LENGTH HUB=<hub> LENGTH=<length> UNLOAD_LENGTH=<length>`

        Example
        -----
        ```
        SET_BOWDEN_LENGTH HUB=Turtle_1 LENGTH=+100 UNLOAD_LENGTH=-100
        ```

        Args:
        """
        hub           = gcmd.get("HUB", None )
        length_param  = gcmd.get('LENGTH', None)
        unload_length = gcmd.get('UNLOAD_LENGTH', None)

        # If hub is not passed in try and get hub if a lane is currently loaded
        if hub is None and self.afc.current is not None:
            cur_lane = self.afc.lanes[self.current]
            hub     = cur_lane.hub_obj.name
        elif hub is None and self.current is None:
            self.logger.info("A lane is not loaded please specify hub to adjust bowden length")
            return

        CUR_HUB                 = self.afc.hubs[hub]
        cur_bowden_len          = CUR_HUB.afc_bowden_length
        cur_unload_bowden_len   = CUR_HUB.afc_unload_bowden_length

        if length_param is not None:
            CUR_HUB.afc_bowden_length = self._calc_length(CUR_HUB.config_bowden_length, cur_bowden_len, length_param)

        if unload_length is not None:
            CUR_HUB.afc_unload_bowden_length = self._calc_length(CUR_HUB.config_unload_bowden_length, cur_unload_bowden_len, unload_length)

        msg =  '// Hub : {}\n'.format( hub )
        msg += '// afc_bowden_length:\n'
        msg += '//   Config Bowden Length:   {}\n'.format(CUR_HUB.config_bowden_length)
        msg += '//   Previous Bowden Length: {}\n'.format(cur_bowden_len)
        msg += '//   New Bowden Length:      {}\n'.format(CUR_HUB.afc_bowden_length)
        msg += '// afc_unload_bowden_length:\n'
        msg += '//   Config Bowden Length:   {}\n'.format(CUR_HUB.config_unload_bowden_length)
        msg += '//   Previous Bowden Length: {}\n'.format(cur_unload_bowden_len)
        msg += '//   New Bowden Length:      {}\n'.format(CUR_HUB.afc_unload_bowden_length)
        msg += '\n// TO SAVE BOWDEN LENGTH afc_bowden_length MUST BE UPDATED IN AFC_Turtle_(n).cfg for each AFC_hub if there are multiple'
        self.logger.raw(msg)

    cmd_HUB_CUT_TEST_help = "Test the cutting sequence of the hub cutter, expects LANE=laneN"
    def cmd_HUB_CUT_TEST(self, gcmd):
        """
        This function tests the cutting sequence of the hub cutter for a specified lane.
        It retrieves the lane specified by the 'LANE' parameter, performs the hub cut,
        and responds with the status of the operation.

        Usage
        -----
        `HUB_CUT_TEST LANE=<lane>`

        Example
        -----
        ```
        HUB_CUT_TEST LANE=lane1
        ```
        """
        lane = gcmd.get('LANE', None)
        self.logger.info('Testing Hub Cut on Lane: {}'.format(lane))
        if lane not in self.afc.lanes:
            self.logger.info('{} Unknown'.format(lane))
            return
        cur_lane = self.afc.lanes[lane]
        CUR_HUB = cur_lane.hub_obj
        CUR_HUB.hub_cut(cur_lane)
        self.logger.info('Hub cut Done!')

    cmd_TEST_help = "Test Assist Motors, spins spoolers like rewinding spool"
    def cmd_TEST(self, gcmd):
        """
        This function tests the assist motors of a specified lane at various speeds.
        Spins the spoolers in reverse like trying to rewind the spool.<br>
        It performs the following steps:<br>
        1. Retrieves the lane specified by the 'LANE' parameter.<br>
        2. Tests the assist motor at full speed, 50%, 30%, and 10% speeds.<br>
        3. Reports the status of each test step.<br>

        Usage
        -----
        `TEST LANE=<lane>`

        Example
        -----
        ```
        TEST LANE=lane1
        ```
        """
        lane = gcmd.get('LANE', None)
        if lane is None:
            self.afc.error.AFC_error('Must select LANE', False)
            return

        if lane not in self.afc.lanes:
            self.logger.info('{} Unknown'.format(lane))
            return

        cur_lane = self.afc.lanes[lane]
        if cur_lane.espooler.afc_motor_rwd is None:
            message = "afc_motor_rwd is not defined in config for {}, cannot perform test.\n".format(lane)
            message += "If your unit does not have spooler motors then you can ignore this message.\n"
            message += "If your unit has spooler motors please verify your config is setup properly"
            self.logger.info(message)
            return

        self.logger.info('Testing at full speed')
        cur_lane.espooler.assist(-1)
        self.afc.reactor.pause(self.afc.reactor.monotonic() + 1)
        if cur_lane.espooler.afc_motor_rwd.is_pwm:
            self.logger.info('Testing at 50 percent speed')
            cur_lane.espooler.assist(-.5)
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 1)
            self.logger.info('Testing at 30 percent speed')
            cur_lane.espooler.assist(-.3)
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 1)
            self.logger.info('Testing at 10 percent speed')
            cur_lane.espooler.assist(-.1)
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 1)
        self.logger.info('Test routine complete')
        cur_lane.espooler.assist(0)

class afcDeltaTime:
    def __init__(self, AFC):
        self.logger = AFC.logger
        self.start_time = None
        self.last_time  = None

    def set_start_time(self):
        self.major_delta_time = self.last_time = self.start_time = datetime.now()

    def log_with_time(self, msg, debug=True):
        try:
            curr_time = datetime.now()
            delta_time = (curr_time - self.last_time ).total_seconds()
            total_time = (curr_time - self.start_time).total_seconds()
            msg = "{} (Δt:{:.3f}s, t:{:.3f})".format( msg, delta_time, total_time )
            if debug:
                self.logger.debug( msg )
            else:
                self.logger.info( msg )
            self.last_time = curr_time
        except Exception as e:
            self.logger.debug("Error in log_with_time function {}".format(e))

    def log_major_delta(self, msg, debug=True):
        delta_time = 0
        try:
            curr_time = datetime.now()
            delta_time = (curr_time - self.major_delta_time ).total_seconds()
            msg = "{} t:{:.3f}".format( msg, delta_time )
            self.logger.info( msg )
            self.major_delta_time = curr_time
        except Exception as e:
            self.logger.debug("Error in log_major_delta function {}".format(e))

        return delta_time

    def log_total_time(self, msg):
        total_time = 0
        try:
            total_time = (datetime.now() - self.start_time).total_seconds()
            msg = "{} t:{:.3f}".format( msg, total_time )

            self.logger.info( msg )
        except Exception as e:
            self.logger.debug("Error in log_total_time function {}".format(e))

        return total_time
