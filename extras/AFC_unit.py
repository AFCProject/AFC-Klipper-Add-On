# Armored Turtle Automated Filament Control
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback

from configfile import error as config_error
from datetime import datetime, timedelta

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from extras.AFC_lane import afc, AFCLane, AFCMoveWarning
    from extras.AFC_stepper import AFCExtruderStepper
    from extras.AFC_buffer import AFCTrigger
    from extras.AFC_hub import afc_hub
    from extras.AFC_extruder import AFCExtruder
    from gcode import GCodeCommand
    from configfile import ConfigWrapper

try: from extras.AFC_utils import ERROR_STR, section_in_config
except:
    trace=traceback.format_exc()
    err_str = f"Error when trying to import AFC_utils.ERROR_STR\n{trace}"
    raise config_error(err_str)

try: from extras.AFC_respond import AFCprompt
except: raise config_error(ERROR_STR.format(import_lib="AFC_respond", trace=traceback.format_exc()))

try: from extras.AFC_lane import SpeedMode, AssistActive, AFCHomingPoints, MoveDirection
except:
    err_str = ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc())
    raise config_error(err_str)

# Common message to be used when and displayed when lanes need to be ejected to calibrate dist_hub
CALI_WARN = "The following lanes ({lanes}) have already been calibrated, if you continue "
CALI_WARN += "with calibration then the filament for selected lanes will be ejected. "
CALI_WARN += "Once filament is reinserted, then lanes will be calibrated.\n"

class afcUnit:
    HOMING_DELTA = 300  # Delta for which to warn if homing move delta is not within this amount from
                        # command move distance.
    def __init__(self, config: ConfigWrapper) -> None:
        self.printer        = config.get_printer()
        self.gcode          = self.printer.load_object(config, 'gcode')
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.printer.register_event_handler("afc:moonraker_connect", self.handle_moonraker_connect)
        self.afc: afc       = self.printer.load_object(config, 'AFC')
        self.reactor        = self.printer.get_reactor()
        self.function       = self.afc.function
        self.logger         = self.afc.logger
        self.type           = None

        self.lanes: Dict[str, AFCLane] = {}
        self._eject_to_calibrate = False

        # Objects
        self.buffer_obj: Optional[AFCTrigger|None] = None
        self.hub_obj: Optional[afc_hub|None]       = None
        self.extruder_obj: Optional[AFCExtruder|None] = None
        self.drive_stepper_obj: AFCExtruderStepper = None
        self.selector_stepper_obj: AFCExtruderStepper = None

        # Config get section
        self.full_name                   = config.get_name().split()
        self.name                        = self.full_name[-1]
        self.screen_mac                  = config.get('screen_mac', None)
        self.hub                         = config.get("hub", None)                                           # Hub name(AFC_hub) that belongs to this unit, can be overridden in AFC_stepper section
        self.extruder                    = config.get("extruder", None)                                      # Extruder name(AFC_extruder) that belongs to this unit, can be overridden in AFC_stepper section
        self.buffer_name                 = config.get('buffer', None)                                        # Buffer name(AFC_buffer) that belongs to this unit, can be overridden in AFC_stepper section

        self.remember_spool              = config.get('remember_spool', False)                               # Turns on/off ability to remember last ejected spool values for all lanes in this unit, can be overridden in AFC_stepper section
        # LED SETTINGS
        # All variables use: (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in AFC.cfg file
        self.led_fault                   = config.get('led_fault', self.afc.led_fault)                       # LED color to set when faults occur in lane
        self.led_ready                   = config.get('led_ready', self.afc.led_ready)                       # LED color to set when lane is ready
        self.led_not_ready               = config.get('led_not_ready', self.afc.led_not_ready)               # LED color to set when lane not ready
        self.led_loading                 = config.get('led_loading', self.afc.led_loading)                   # LED color to set when lane is loading
        self.led_prep_loaded             = config.get('led_loading', self.afc.led_loading)                   # LED color to set when lane is loaded
        self.led_unloading               = config.get('led_unloading', self.afc.led_unloading)               # LED color to set when lane is unloading
        self.led_tool_loaded             = config.get('led_tool_loaded', self.afc.led_tool_loaded)           # LED color to set when lane is loaded into tool
        self.led_tool_loaded_idle        = config.get('led_tool_loaded_idle', self.afc.led_tool_loaded_idle) # LED color to set when lane is loaded into tool and idle
        self.led_tool_unloaded           = config.get('led_tool_unloaded', self.afc.led_tool_unloaded)       # LED color to set when lanes extruder is unloaded
        self.led_spool_illum             = config.get('led_spool_illuminate', self.afc.led_spool_illum)      # LED color to illuminate under spool
        self.led_logo_index              = config.get('led_logo_index', None)                                # LED Logo index
        self.led_logo_color              = self.afc.function.HexConvert(config.get('led_logo_color', '0,0,0,0'))# Default logo color when nothing is loaded
        self.led_logo_loading            = self.afc.function.HexConvert(config.get('led_logo_loading', self.led_loading ))

        self.long_moves_speed            = config.getfloat("long_moves_speed", self.afc.long_moves_speed)   # Speed in mm/s to move filament when doing long moves. Setting value here overrides values set in AFC.cfg file
        self.long_moves_accel            = config.getfloat("long_moves_accel", self.afc.long_moves_accel)   # Acceleration in mm/s squared when doing long moves. Setting value here overrides values set in AFC.cfg file
        self.short_moves_speed           = config.getfloat("short_moves_speed", self.afc.short_moves_speed) # Speed in mm/s to move filament when doing short moves. Setting value here overrides values set in AFC.cfg file
        self.short_moves_accel           = config.getfloat("short_moves_accel", self.afc.short_moves_accel) # Acceleration in mm/s squared when doing short moves. Setting value here overrides values set in AFC.cfg file
        self.short_move_dis              = config.getfloat("short_move_dis", self.afc.short_move_dis)       # Move distance in mm for failsafe moves. Setting value here overrides values set in AFC.cfg file
        self.max_move_dis                = config.getfloat("max_move_dis", self.afc.max_move_dis)            # Maximum distance to move filament. AFC breaks filament moves over this number into multiple moves. Useful to lower this number if running into timer too close errors when doing long filament moves. Setting value here overrides values set in AFC.cfg file
        self.homing_overshoot            = config.getfloat("homing_overshoot", 50)                           # Amount to add to homing distance so that distance is long enough to actually hit endstop
        self.homing_delta                = config.getfloat("homing_delta", self.HOMING_DELTA)                # Delta for which to warn if homing move delta is not within this amount from command move distance.
        self.load_then_home_var          = config.getboolean("load_then_home", self.afc.load_then_home_var)
        self.load_undershoot             = config.getfloat("load_undershoot", self.afc.load_undershoot)
        self.debug                       = config.getboolean("debug",            False)                      # Turns on/off debug messages to console
        self.rev_long_moves_speed_factor = config.getfloat("rev_long_moves_speed_factor", self.afc.rev_long_moves_speed_factor)
        self.extruder_clear_dis          = config.getfloat("extruder_clear_dis", 50)                        # Amount to move to clear extruder gears when ejecting filament
        self.enable_buffer_tool_check    = config.getboolean("enable_buffer_tool_check", False)

        # Espooler defines
        # Time in seconds to wait between breaking n20 motors(nSleep/FWD/RWD all 1) and then releasing the break to allow coasting. Setting value here overrides values set in AFC.cfg file
        self.n20_break_delay_time   = config.getfloat("n20_break_delay_time",   self.afc.n20_break_delay_time)
        # Setting to True enables espooler assist while printing
        self.enable_assist          = config.getboolean("enable_assist",        self.afc.enable_assist)
        # Weight spool has to be below to activate print assist
        self.enable_assist_weight   = config.getfloat("enable_assist_weight",   self.afc.enable_assist_weight)
        # Number of seconds to wait before checking filament movement for espooler assist
        self.timer_delay            = config.getfloat("timer_delay",            5)
        # Setting to True enables full speed espoolers for kick_start_time amount
        self.enable_kick_start      = config.getboolean("enable_kick_start",    True)
        self.spool_ratio            = config.getfloat("spool_ratio",2) #gear ratio for printed gearbox between N20 and spooler wheels
        self.full_weight            = config.getfloat("full_weight",1000)           # full weight of filament spool (no counting spool itself)
        self.espool_rot_dist        = config.getfloat("espool_rot_dist",132.9)

        # Time in seconds to enable spooler at full speed to help with getting the spool to spin
        self.kick_start_time        = config.getfloat("kick_start_time",        0.070)
        # Delta amount in mm from last move to trigger assist
        self.delta_movement         = config.getfloat("delta_movement",         150)
        # Scaling factor for the following variables: kick_start_time, spool_outer_diameter, delta_movement
        self.scaling                = config.getfloat("spoolrate",              1.0)

        # If True, the unload retract is assisted to prevent loose windings, especially on full spools. This can prevent loops from slipping off the spool. Setting value here overrides values set in AFC.cfg file
        self.assisted_unload    = config.getboolean("assisted_unload", self.afc.assisted_unload)
        # When True AFC will unload lane and then pause when runout is triggered and spool to swap to is not set(infinite spool). Setting value here overrides values set in AFC.cfg file
        self.unload_on_runout   = config.getboolean("unload_on_runout", self.afc.unload_on_runout)

        # TD-1 variables
        self.td1_when_loaded    = config.getboolean("capture_td1_when_loaded", self.afc.td1_when_loaded)
        self.td1_device_id      = config.get("td1_device_id", None)

        self.post_prep_macro    = config.get("post_prep_macro", "AFC_POST_PREP")  # Macro to call after loading filament during prep callback

    def __str__(self):
        return self.name

    def _check_and_errorout(
            self, check_obj: Any, config_name: str, variable_name:str) -> tuple[bool, str]:
        """
        Helper method for checking if object was loaded correctly for specific variable

        :param check_obj: Object to check if it's None
        :param config_name: Config name to add into error string
        :param variable_name: Variable name in config that caused error

        :return tuple: Returns True of object is None and string is a common error string with
                       config name, variable name and full name of current unit to aid in easily
                       fixing a config issue.
        """
        error_string = f"Error: [{config_name}] config not found for {variable_name} in "\
                       f"{self.full_name} config section. Please make sure [{config_name}] " \
                       "section exists in your config.\n"
        if check_obj is None:
            return True, error_string
        return False, ""


    def _lookup_objects(self, config: ConfigWrapper) -> None:
        """
        Helper method for looking up drive and selector stepper config sections in config file
        and loading their respective object. If config is not found and error is raised. This should
        only be called if Unit relies on drive stepper and selector steppers.

        :param config: Config object to search for config sections
        """
        error_string = ""
        error_bool   = False
        config_name = f'AFC_stepper {self.drive_stepper}'
        if section_in_config(config, config_name):
            self.drive_stepper_obj: Optional[AFCExtruderStepper] = \
                self.printer.load_object(config, config_name, None)
        error, rtn_str = self._check_and_errorout(self.drive_stepper_obj, config_name,
                                                  "drive_stepper")
        error_string += rtn_str
        error_bool |= error

        config_name = f'AFC_stepper {self.selector_stepper}'
        if section_in_config(config, config_name):
            self.selector_stepper_obj: Optional[AFCExtruderStepper] = \
                self.printer.load_object(config, config_name, None)

        error, rtn_str = self._check_and_errorout(self.selector_stepper_obj, config_name,
                                                  "selector_stepper")
        error_string += rtn_str
        error_bool |= error
        if error_bool:
            raise config_error(error_string)

    def handle_connect(self):
        """
        Handles klippy:connect event, and does error checking to make sure users have hub/extruder/buffers sections if these variables are defined at the unit level
        """
        self.afc = self.printer.lookup_object('AFC')
        self.afc.units[self.name] = self

        # Error checking for hub
        # TODO: once supported add check if users is not using a hub
        if self.hub is not None:
            try:
                self.hub_obj = self.printer.lookup_object("AFC_hub {}".format(self.hub))
            except:
                error_string = 'Error: No config found for hub: {hub} in [AFC_{unit_type} {unit_name}]. Please make sure [AFC_hub {hub}] section exists in your config'.format(
                hub=self.hub, unit_type=self.type.replace("_", ""), unit_name=self.name )
                raise config_error(error_string)

        # Error checking for extruder
        if self.extruder is not None:
            try:
                self.extruder_obj = self.printer.lookup_object("AFC_extruder {}".format(self.extruder))
            except:
                error_string = 'Error: No config found for extruder: {extruder} in [AFC_{unit_type} {unit_name}]. Please make sure [AFC_extruder {extruder}] section exists in your config'.format(
                    extruder=self.extruder, unit_type=self.type.replace("_", ""), unit_name=self.name )
                raise config_error(error_string)

        # Error checking for buffer
        if self.buffer_name is not None:
            try:
                self.buffer_obj = self.printer.lookup_object('AFC_buffer {}'.format(self.buffer_name))
            except:
                error_string = 'Error: No config found for buffer: {buffer} in [AFC_{unit_type} {unit_name}]. Please make sure [AFC_buffer {buffer}] section exists in your config'.format(
                    buffer=self.buffer_name, unit_type=self.type.replace("_", ""), unit_name=self.name )
                raise config_error(error_string)

        # Send out event so lanes can store units object
        self.printer.send_event("AFC_unit_{}:connect".format(self.name), self)

        self.gcode.register_mux_command('UNIT_CALIBRATION', "UNIT", self.name, self.cmd_UNIT_CALIBRATION, desc=self.cmd_UNIT_CALIBRATION_help)
        self.gcode.register_mux_command('UNIT_LANE_CALIBRATION', "UNIT", self.name, self.cmd_UNIT_LANE_CALIBRATION, desc=self.cmd_UNIT_LANE_CALIBRATION_help)
        self.gcode.register_mux_command('UNIT_BOW_CALIBRATION', "UNIT", self.name, self.cmd_UNIT_BOW_CALIBRATION, desc=self.cmd_UNIT_BOW_CALIBRATION_help)

    def handle_moonraker_connect(self):
        """
        Registers macros commands after moonrakers connection has been established so that endpoint can be queried successfully
        to check if TD-1 is defined in users moonrakers.conf file.
        """
        if self.afc.td1_defined:
            self.gcode.register_mux_command('AFC_UNIT_TD_ONE_CALIBRATION', "UNIT", self.name, self.cmd_AFC_UNIT_TD_ONE_CALIBRATION, desc=self.cmd_AFC_UNIT_TD_ONE_CALIBRATION_help)

    def get_status(self, eventtime=None):
        response = {}
        response['lanes'] = [lane.name for lane in self.lanes.values()]
        response["extruders"]=[]
        response["hubs"] = []
        response["buffers"] = []

        for lane in self.lanes.values():
            if lane.hub is not None and not lane.is_direct_hub() and lane.hub not in response["hubs"]: response["hubs"].append(lane.hub)
            if lane.extruder_name is not None and lane.extruder_name not in response["extruders"]: response["extruders"].append(lane.extruder_name)
            if lane.buffer_name is not None and lane.buffer_name not in response["buffers"]: response["buffers"].append(lane.buffer_name)

        return response

    cmd_UNIT_CALIBRATION_help = 'open prompt to calibrate the dist hub for lanes in selected unit'
    def cmd_UNIT_CALIBRATION(self, gcmd):
        """
        Open a prompt to calibrate either the distance between the extruder and the hub or the Bowden length
        for the selected unit. Provides buttons for lane calibration, Bowden length calibration, and a back option.

        Usage
        -----
        `UNIT_CALIBRATION UNIT=<unit>`

        Example
        -----
        ```
        UNIT_CALIBRATION UNIT=Turtle_1
        ```
        """
        if len(self.lanes) == 0:
            self.logger.warning(f"No lanes to calibrate for {self.name}")
            return

        prompt = AFCprompt(gcmd, self.logger)
        buttons = []
        title = '{} Calibration'.format(self.name)
        text = 'Select to calibrate the distance from extruder to hub or bowden length'
        # Selection buttons
        buttons.append(("Calibrate Lanes", "UNIT_LANE_CALIBRATION UNIT={}".format(self.name), "primary"))

        direct_hubs = any( lane.is_direct_hub() for lane in self.afc.lanes.values())
        lanes_loaded = any( lane.load_state and not lane.is_direct_hub() for lane in self.afc.lanes.values())
        any_lane_has_td1_ids = any( lane.td1_device_id for lane in self.afc.lanes.values())

        if not direct_hubs or lanes_loaded:
            buttons.append(("Calibrate afc_bowden_length", "UNIT_BOW_CALIBRATION UNIT={}".format(self.name), "secondary"))

        # Add button for TD-1 calibration if user has one connected and defined
        if self.afc.td1_defined and any_lane_has_td1_ids:
            buttons.append(("Calibrate TD-1 Length", "AFC_UNIT_TD_ONE_CALIBRATION UNIT={}".format(self.name), "primary"))

        # Button back to previous step
        back = [('Back to unit selection', 'AFC_CALIBRATION', 'info')]

        prompt.create_custom_p(title, text, buttons, True, None, back)

    cmd_UNIT_LANE_CALIBRATION_help = 'open prompt to calibrate the length from extruder to hub'
    def cmd_UNIT_LANE_CALIBRATION(self, gcmd):
        """
        Open a prompt to calibrate the extruder-to-hub distance for each lane in the selected unit. Creates buttons
        for each lane, grouped in sets of two, and allows calibration for all lanes or individual lanes.

        Usage
        -----
        `UNIT_LANE_CALIBRATION UNIT=<unit>`

        Example
        -----
        ```
        UNIT_LANE_CALIBRATION UNIT=Turtle_1
        ```
        """
        prompt = AFCprompt(gcmd, self.logger)
        buttons = []
        group_buttons = []
        index = 0
        title = '{} Lane Calibration'.format(self.name)

        text = ""
        # Check to see if lanes that need to be ejected have already been calibrated and add
        # message so user knows which lanes will be ejected and then calibrated on next filament
        # load.
        lanes = self.get_calibrated_lanes()
        if lanes is not None:
            lanes = ", ".join(lanes)
            text = CALI_WARN.format(lanes=lanes)
        text += ('Select a loaded lane from {} to calibrate length from extruder to hub. '
                 'Config option: dist_hub').format(self.name)

        # Create buttons for each lane and group every 4 lanes together
        for lane in self.lanes.values():
            if (lane.load_state
                and not lane.tool_loaded):
                button_label = "{}".format(lane)
                # Do a bowden length calibration for direct hubs, dist_hub length gets set properly this way
                if lane.is_direct_hub():
                    button_command = "CALIBRATE_AFC BOWDEN={}".format(lane)
                else:
                    button_command = "CALIBRATE_AFC LANE={}".format(lane)
                button_style = "primary" if index % 2 == 0 else "secondary"
                group_buttons.append((button_label, button_command, button_style))

                # Add group to buttons list after every 4 lanes
                if (index + 1) % 2 == 0 or index == len(self.lanes) - 1:
                    buttons.append(list(group_buttons))
                    group_buttons = []
                index += 1

        if group_buttons:
            buttons.append(list(group_buttons))

        total_buttons = sum(len(group) for group in buttons)
        if total_buttons > 1:
            all_lanes = [('All lanes', 'CALIBRATE_AFC LANE=all UNIT={}'.format(self.name), 'default')]
        else:
            all_lanes = None
        if total_buttons == 0:
            text = 'No lanes are loaded, please load before calibration'

        # 'Back' button
        back = [('Back', 'UNIT_CALIBRATION UNIT={}'.format(self.name), 'info')]

        prompt.create_custom_p(title, text, all_lanes,
                               True, buttons, back)

    cmd_UNIT_BOW_CALIBRATION_help = 'open prompt to calibrate the afc_bowden_length from a lane in the unit'
    def cmd_UNIT_BOW_CALIBRATION(self, gcmd):
        """
        Open a prompt to calibrate the Bowden length for a specific lane in the selected unit. Provides buttons
        for each lane, with a note to only calibrate one lane per unit.

        Usage
        -----
        `UNIT_BOW_CALIBRATION UNIT=<unit>`

        Example
        -----
        ```
        UNIT_BOW_CALIBRATION UNIT=Turtle_1
        ```
        """
        prompt = AFCprompt(gcmd, self.logger)
        buttons = []
        group_buttons = []
        index = 0
        title = 'Bowden Calibration {}'.format(self.name)
        text = ('Select a loaded lane from {} to measure Bowden length. '
                'ONLY CALIBRATE BOWDEN USING 1 LANE PER UNIT/hub.'
                'Config option: afc_bowden_length').format(self.name)

        for lane in self.lanes.values():
            if lane.load_state and not lane.is_direct_hub():
                # Create a button for each lane
                button_label = "{}".format(lane)
                button_command = "CALIBRATE_AFC BOWDEN={}".format(lane)
                button_style = "primary" if index % 2 == 0 else "secondary"
                group_buttons.append((button_label, button_command, button_style))

                # Add group to buttons list after every 4 lanes
                if (index + 1) % 2 == 0 or index == len(self.lanes) - 1:
                    buttons.append(list(group_buttons))
                    group_buttons = []
                index += 1

        if group_buttons:
            buttons.append(list(group_buttons))

        total_buttons = sum(len(group) for group in buttons)
        if total_buttons == 0:
            text = 'No lanes are loaded, please load before calibration'

        back = [('Back', 'UNIT_CALIBRATION UNIT={}'.format(self.name), 'info')]

        prompt.create_custom_p(title, text, None,
                               True, buttons, back)

    cmd_AFC_UNIT_TD_ONE_CALIBRATION_help = 'open prompt to calibrate the td1_bowden_length from a lane in the unit'
    def cmd_AFC_UNIT_TD_ONE_CALIBRATION(self, gcmd):
        """
        Open a prompt to calibrate the Bowden length to a TD-1 device for a specific lane in the selected unit. Provides buttons
        for each lane, with a note to only calibrate one lane per unit.

        Usage
        -----
        `AFC_UNIT_TD_ONE_CALIBRATION UNIT=<unit>`

        Example
        -----
        ```
        AFC_UNIT_TD_ONE_CALIBRATION UNIT=Turtle_1
        ```
        """
        prompt = AFCprompt(gcmd, self.logger)
        buttons = []
        group_buttons = []
        index = 0
        title = 'TD-1 Bowden Calibration {}'.format(self.name)
        text = ('Select a loaded lane from {} to measure Bowden length to your TD-1 Device. '
                'ONLY CALIBRATE BOWDEN USING 1 LANE PER UNIT/hub. '
                'WARNING: This could take some time to complete. '
                'Config option: td1_bowden_length').format(self.name)

        for lane in self.lanes.values():
            if (lane.td1_device_id
                and lane.load_state):
                # Create a button for each lane
                button_label = "{}".format(lane)
                button_command = "CALIBRATE_AFC TD1={} DISTANCE=50".format(lane)
                button_style = "primary" if index % 2 == 0 else "secondary"
                group_buttons.append((button_label, button_command, button_style))

                # Add group to buttons list after every 4 lanes
                if (index + 1) % 2 == 0 or index == len(self.lanes) - 1:
                    buttons.append(list(group_buttons))
                    group_buttons = []
                index += 1

        if group_buttons:
            buttons.append(list(group_buttons))

        total_buttons = sum(len(group) for group in buttons)
        if total_buttons == 0:
            text = 'No lanes are loaded, please load before calibration'

        back = [('Back', 'UNIT_CALIBRATION UNIT={}'.format(self.name), 'info')]

        prompt.create_custom_p(title, text, None,
                               True, buttons, back)

    def set_logo_color(self, color):
        """
        Common function for setting a units logo led's

        :param color: Color to set logo led's, can be hex value or comma seperated list
        """
        if color is not None and color:
            led_color = self.afc.function.HexToLedString(color.replace("#", ""))
            self.afc.function.afc_led( led_color, self.led_logo_index )

    def lane_not_ready(self, lane):
        """
        Common function for setting a lanes led when a lane is not ready

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_not_ready, lane.led_index)

    def lane_loaded(self, lane: AFCLane):
        """
        Common function for setting a lanes led when lane is loaded

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_ready, lane.led_index)

    def lane_unloading(self, lane):
        """
        Common function for setting a lanes led when lane is unloading

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_unloading, lane.led_index)

    def lane_unloaded(self, lane: AFCLane):
        """
        Common function for setting a lanes led when lane is unloaded

        :param lane: Lane object to set led
        """
        self.lane_not_ready(lane)

    def lane_loading(self, lane: AFCLane):
        """
        Common function for setting a lanes led when lane is loading

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_loading, lane.led_index)

    def lane_tool_loaded(self, lane: AFCLane):
        """
        Common function for setting a lanes led when lane is tool loaded,
        also sets toolheads led status color

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_tool_loaded, lane.led_index)
        lane.extruder_obj.set_status_led(lane.led_tool_loaded)

    def lane_tool_unloaded(self, lane: AFCLane):
        """
        Common function for setting a lanes led when lane is tool unloaded,
        also sets toolheads led status color

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_ready, lane.led_index)
        lane.extruder_obj.set_status_led(lane.led_tool_unloaded)

    def lane_tool_loaded_idle(self, lane):
        """
        Common function for setting a lanes led when its loaded into a tool
        and tool is docked(for toolchangers). Function also sets toolheads led
        status color.

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_tool_loaded_idle, lane.led_index)
        lane.extruder_obj.set_status_led(lane.led_tool_loaded_idle)

    def lane_illuminate_spool(self, lane):
        """
        Common function for setting lane illumination leds

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_spool_illum, lane.led_spool_index)

    def lane_fault(self, lane):
        """
        Common function for setting a lanes led when a fault happens

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_fault, lane.led_index)

    def select_lane( self, lane: AFCLane, sel_prep:bool=False ) -> tuple[bool, float|int]:
        """
        Common method to select a units lane.

        Override in unit specific file if needed

        :param lane: AFCLane object for lane to select
        :param sel_prep: Set to true is selection is happing during prep macro
        """
        return True, 0.0

    def return_to_home(self, prep=False):
        """
        Function to home unit if unit has homing sensor
        """
        return

    def check_runout(self, cur_lane):
        """
        Function to check if runout logic should be triggered, override in specific unit
        """
        return False

    # Functions are below are placeholders so the function exists for all units, override these function in your unit files
    def _print_function_not_defined(self, name):
        self.afc.logger.error("{} function not defined for {}".format(name, self.name))

    # Function that other units can create so that they are specific to the unit
    def system_Test(self, cur_lane, delay, assignTcmd, enable_movement):
        self._print_function_not_defined(self.system_test.__name__)

    def calibrate_bowden(self, cur_lane, dis, tol):
        self._print_function_not_defined(self.calibrate_bowden.__name__)

    def calibrate_td1(self, cur_lane, dis, tol):
        self._print_function_not_defined(self.calibrate_td1.__name__)

    def calibrate_hub(self, cur_lane, tol):
        self._print_function_not_defined(self.calibrate_hub.__name__)

    def move_until_state(self, cur_lane, state, move_dis, tolerance, short_move, pos=0):
        self._print_function_not_defined(self.move_until_state.__name__)

    def calc_position(self, cur_lane, state, pos, short_move, tolerance):
        self._print_function_not_defined(self.calc_position.__name__)

    def calibrate_lane(self, cur_lane, tol):
        self._print_function_not_defined(self.calibrate_lane.__name__)

    def unselect_lane(self, move_distance: float=50):
        self._print_function_not_defined(self.unselect_lane.__name__)

    def _move_lane(self, lane: AFCLane|AFCExtruderStepper, delay: float,
                enable_movement: bool=True) -> bool:
        self._print_function_not_defined(self._move_lane.__name__)

    def calibration_lane_message(self) -> str:
        return ""

    def get_calibrated_lanes(self) -> Optional[list[str]]:
        """
        Helper method to return lanes in a unit that have already been calibrated and require
        lane to be ejected to be calibrated correctly.

        :return list: List of lanes that have already been calibrated
        """
        lanes = None
        if self._eject_to_calibrate:
            lanes = [ lane.name for lane in self.lanes.values() if (lane.load_state
                                                                    and lane.calibrated_lane
                                                                    and not lane.tool_loaded)]
        return lanes

    def get_td1_data(self, cur_lane: AFCLane, compare_time: datetime,
                     ignore_time: bool=False) -> bool:
        """
        Queries moonrakers endpoint to get td1 data and check to see if data is valid and time
        in data is greater than passed in time as this is how determination is made that filament
        made it TD-1 device. Once filament is detected and valued, information is save to passed in lane.

        :param cur_lane: Current lane to apply TD-1 data to, also check's to see if lane has a specific TD-1 ID
                         assigned to the lane.
        :param compare_time: Time to compare returned data to, which helps verify that the data is valid and
                             filament has reached TD-1 device
        :param ignore_time: Override to just capture TD-1 data anyways, useful when loading filament to toolhead
                            and want to capture data once loaded.

        :return boolean: True once filament is detected in TD-1 device
        """
        td1_data = self.afc.moonraker.get_td1_data()
        t_delta = timedelta(seconds = 10)
        valid_data = False

        if len(td1_data) > 0:
            self.logger.debug(f"Data: {td1_data}, Compare_time: {compare_time}")

            if cur_lane.td1_device_id in td1_data:
                data = td1_data[cur_lane.td1_device_id]
            else:
                self.afc.error.AFC_error(f"TD-1 Device ID ({cur_lane.td1_device_id}) supplied, but ID not found.", pause=False)
                return False

            if data["scan_time"] is None:
                return False

            if data["scan_time"].endswith("+00:00Z"):
                scan_time = data["scan_time"][:-1]
            else:
                scan_time = data["scan_time"][:-1]+"+00:00"
            try:
                scan_time = datetime.fromisoformat( scan_time ).astimezone()
            except (AttributeError, ValueError) as e:
                self.afc.logger.error("Error trying to format TD-1 scan time, check AFC.log for more information", f"{e}")
                return False

            if scan_time > compare_time.astimezone():
                valid_data = True
            elif ( compare_time.astimezone() - scan_time ) < t_delta:
                valid_data = True

            if ( (valid_data or ignore_time)
                 and data['td'] is not None
                 and data['color'] is not None ):
                cur_lane.td1_data = data
                self.logger.info(f"{cur_lane.name} TD-1 data captured")
                self.afc.save_vars()
                return True

    def prep_load(self, lane: AFCLane):
        """
        Helper method for initially loading spools when prep sensor is triggered.
        Need to override in specific unit

        See unit specific function for how this is unique per unit type.

        :param lane: AFCLane object for which to activate and load filament to load sensor
        """
        self._print_function_not_defined(self.eject_lane.__name__)

    def prep_post_load(self, lane: AFCLane):
        """
        Helper method to run after prep_load has been successful for a specific unit.
        Need to override in specific unit

        See unit specific function for how this is unique per unit type.

        :param lane: AFCLane object for which to perform prep_post_load action on
        """
        self._print_function_not_defined(self.prep_post_load.__name__)

    def eject_lane(self, lane: AFCLane):
        """
        Method to eject spool from Unit/lane.
        Need to override in specific unit

        :param lane: AFCLane object for which to perform eject action on
        """
        self._print_function_not_defined(self.eject_lane.__name__)

    def move_to_hub(self, lane: AFCLane, dist: float,
                    dir:MoveDirection, use_homing=True,
                    speed_mode=SpeedMode.HUB,
                    assist_active=AssistActive.DYNAMIC) -> tuple[bool, float|int, AFCMoveWarning]:
        """
        Helper method to move filament to hub sensor, calls lanes move_to method with HUB as trigger
        point when homing is enabled.

        This can be overridden if needed for unit specific functionality

        :param lane: AFCLane object to move
        :param dist: Distance in mm to move filament
        :param dir: Direction(+/-) to move filament
        :param use_homing: When enabled home_to logic is used, else move_advance logic is used
        :param speed_mode: SpeedMode type to use when moving stepper
        :param assist_active: Set appropriate to enabled/disable or use Dynamic logic to enabled/disable
                              spoolers based off move distance.

        :return tuple[bool, float|int, AFCMoveWarning]: A tuple containing:

            - Returns True if move was successful
            - Total distance moved
            - AFCMoveWarning.WARN set if movement moved is not within 300mm of total distance,
                  AFCMoveWarning.ERROR when error is detected during homing, AFCMoveWarning.NONE
                  when no error or not full movement detected. When homing is disabled, always returns
                  True, 0, AFCMoveWarning.NONE.
        """
        return lane.move_to(dist * dir, speed_mode, assist_active=assist_active,
                            endstop=lane.hub_endstop_name, use_homing=use_homing)

    def move_to_load(self, lane: AFCLane, dist: float,
                     dir: MoveDirection, use_homing=True,
                     speed_mode:SpeedMode=SpeedMode.LONG) -> tuple[bool, float|int, AFCMoveWarning]:
        """
        Helper method to move filament to load sensor, calls lane's move_to method with the load
        sensor endpoint (lane.load_es) as trigger point when homing is enabled.

        This can be overridden if needed for unit specific functionality

        Active assist is set to Dynamic and SpeedMode is set to Long by default when calling lanes
        move_to method.

        :param lane: AFCLane object to move
        :param dist: Distance in mm to move filament
        :param dir: Direction(+/-) to move filament
        :param use_homing: When enabled home_to logic is used, else move_advance logic is used
        :param speed_mode: SpeedMode type to use when moving stepper

        :return tuple[bool, float|int, AFCMoveWarning]: A tuple containing:

            - Returns True if move was successful
            - Total distance moved
            - AFCMoveWarning.WARN set if movement moved is not within 300mm of total distance,
                  AFCMoveWarning.ERROR when error is detected during homing, AFCMoveWarning.NONE
                  when no error or not full movement detected. When homing is disabled, always returns
                  True, 0, AFCMoveWarning.NONE.
        """
        return lane.move_to(dist * dir, speed_mode, endstop=lane.load_es,
                            assist_active=AssistActive.DYNAMIC, use_homing=use_homing)

    def load_then_home(self, lane: AFCLane|AFCExtruderStepper, distance: float,
                       assist_active: AssistActive, endstop: AFCHomingPoints) -> tuple[bool, float|int, AFCMoveWarning]:
        """
        Helper method to move filament to toolhead. If load_then_home boolean is set, AFC will do
        a normal move without homing enabled for a distance of: distance - load_undershoot.
        Once this move is done, AFC will them home the rest of the way to toolhead sensor or buffer for ramming.

        If load_then_home is set false, then AFC will do a homing move from hub to toolhead.

        :param lane: AFCLane object to move
        :param distance: Total distance in mm to move filament
        :param assist_active: Set appropriate to enabled/disable or use Dynamic logic to enabled/disable
                              spoolers based off move distance.
        :param endstop: Endstop to use when loading to toolhead

        :return tuple: Returns if move was successful, distance moved, and boolean set to true if
                       movement moved is not within 300mm of total distance. When homing is
                       disabled, always returns True, 0, False.
        """
        total_distance = distance
        speed_mode = SpeedMode.LONG
        # Only do load then home if user has buffer defined and load then home enabled
        home_to_tool = self.afc.homing_enabled and self.afc.home_to_tool
        # Adding in check to make sure distance is greater than load_undershoot value for lane
        # when load_then_home is enabled
        load_and_home = lane.load_then_home_var and distance > lane.load_undershoot
        if (load_and_home
            and lane.extruder_obj.tool_start == "buffer"):
            load_length = distance - lane.load_undershoot
            home, dist, warn = lane.move_to(load_length, speed_mode, assist_active=assist_active,
                                            endstop=endstop, use_homing=False)
            total_distance -= load_length
            speed_mode = SpeedMode.SHORT
        home, dist, warn = lane.move_to(total_distance, speed_mode, assist_active=assist_active,
                                        endstop=endstop, use_homing=home_to_tool)

        return home, dist, warn

    def _buffer_toolhead_load_check(self, lane: AFCLane|AFCExtruderStepper):
        """
        Method for checking if filament is loaded to toolhead when using buffer in ramming mode.
        If homing is not enabled or enable_buffer_tool_check is not enabled, lanes tool_loaded status
        is returned.

        When homing is enabled and enable_buffer_tool_check is enabled, this method will try to
        move the filament so that both advance and trail sensor in buffer are hit. If this is
        successful, then it's deemed that filament is actually loaded to the toolhead.

        :param lane: Lane to check if filament is loaded to toolhead.
        :return: Returns true if check is successful.
        """
        loaded = False
        homed = False
        if (not self.afc.homing_enabled
            or not self.enable_buffer_tool_check):
            loaded = lane.tool_loaded
        else:
            homed, move_dis, _ = lane.move_to(200, SpeedMode.SHORT, AFCHomingPoints.BUFFER)

            if not homed:
                msg = f"Buffer toolhead loaded check failed for {lane.name}. Please verify"
                msg +=f" that {lane.name} is loaded to toolhead. If lane is not loaded to "
                msg +=f"toolhead then run AFC_RESET and choose {lane.name} to reset back to hub."
                msg +=" Once lane is reset run UNSET_LANE_LOADED macro."
                self.afc.error.AFC_error(msg, False)
            else:
                lane.move(move_dis * MoveDirection.NEG, lane.short_moves_speed,
                          lane.short_moves_accel, False)
            loaded = homed
        return loaded

    cmd_AFC_SELECT_LANE_help = "Command to home to lane selector for specified lane in selector style units."
    cmd_AFC_SELECT_LANE_options = {"LANE": {"type":"string", "default":"lane1"}}
    def cmd_AFC_SELECT_LANE(self, gcmd: GCodeCommand):
        """
        Macro handles selecting specific lane for selector style units.

        Usage
        -----
        `AFC_SELECT_LANE LANE=<lane>`

        Example
        -----
        ```
        AFC_SELECT_LANE LANE=lane1`
        ```
        """
        lane = gcmd.get("LANE")
        lane_obj = self.afc.lanes.get(lane, None)
        if lane_obj:
            homed, distance = self.select_lane(lane_obj)
            if homed:
                self.logger.info(f"Successfully homed to {lane_obj.name} selector after {distance}mm")
            else:
                self.logger.error(f"Failed to home to {lane_obj.name}")
        else:
            error_string = f"Invalid lane {lane}"
            gcmd.error(error_string)

    cmd_AFC_UNSELECT_LANE_help = "Command to move selector to free filament from currently selected lane." \
                               " Selector only moves if any lanes selector is currently triggered unless" \
                               " force is passed in."
    cmd_AFC_UNSELECT_LANE_options = {"UNIT": {"type":"string", "default":""},
                                     "FORCE" : {"type":"int", "default":0},
                                     "MOVE_DIST" : {"type":"int", "default":100}}
    def cmd_AFC_UNSELECT_LANE(self, gcmd: GCodeCommand):
        """
        Command to move selector to free filament from currently selected lane. Selector only moves
        if any lanes selector is currently triggered unless force is passed in.

        Optional Parameter:
        FORCE: pass in 1 to always move selector
        MOVE_DIST: amount to move selector, defaults to 100mm

        Usage
        -----
        `AFC_UNSELECT_LANE UNIT=<unit_name> FORCE=<0|1> MOVE_DIST=<amount to move selector>`

        Example
        -----
        ```
        AFC_UNSELECT_LANE UNIT=ViViD_1`
        ```
        Example
        -----
        ```
        AFC_UNSELECT_LANE UNIT=ViViD_1 FORCE=1`
        ```
        """
        force: int = gcmd.get_int("FORCE", 0)
        move_dist: float = gcmd.get_float("MOVE_DIST", 100.0)
        force = force != 0
        any_selected = any(True if lane._selector_state else False for lane in self.lanes.values())
        if any_selected or force:
            self.unselect_lane(move_distance=move_dist)
            self.logger.info(f"{self.name} selector moved")