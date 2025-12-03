# Armored Turtle Automated Filament Control
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import traceback

from configfile import error
from datetime import datetime, timedelta

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_respond import AFCprompt
except: raise error(ERROR_STR.format(import_lib="AFC_respond", trace=traceback.format_exc()))

class afcUnit:
    def __init__(self, config):
        self.printer        = config.get_printer()
        self.gcode          = self.printer.lookup_object('gcode')
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.printer.register_event_handler("afc:moonraker_connect", self.handle_moonraker_connect)
        self.afc            = self.printer.lookup_object('AFC')
        self.logger         = self.afc.logger

        self.lanes      = {}

        # Objects
        self.buffer_obj     = None
        self.hub_obj        = None
        self.extruder_obj   = None

        # Config get section
        self.full_name                   = config.get_name().split()
        self.name                        = self.full_name[-1]
        self.screen_mac                  = config.get('screen_mac', None)
        self.hub                         = config.get("hub", None)                                           # Hub name(AFC_hub) that belongs to this unit, can be overridden in AFC_stepper section
        self.extruder                    = config.get("extruder", None)                                      # Extruder name(AFC_extruder) that belongs to this unit, can be overridden in AFC_stepper section
        self.buffer_name                 = config.get('buffer', None)                                        # Buffer name(AFC_buffer) that belongs to this unit, can be overridden in AFC_stepper section
        self.led_fault                   = config.get('led_fault', self.afc.led_fault)                       # LED color to set when faults occur in lane        (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in AFC.cfg file
        self.led_ready                   = config.get('led_ready', self.afc.led_ready)                       # LED color to set when lane is ready               (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in AFC.cfg file
        self.led_not_ready               = config.get('led_not_ready', self.afc.led_not_ready)               # LED color to set when lane not ready              (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in AFC.cfg file
        self.led_loading                 = config.get('led_loading', self.afc.led_loading)                   # LED color to set when lane is loading             (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in AFC.cfg file
        self.led_prep_loaded             = config.get('led_loading', self.afc.led_loading)                   # LED color to set when lane is loaded              (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in AFC.cfg file
        self.led_unloading               = config.get('led_unloading', self.afc.led_unloading)               # LED color to set when lane is unloading           (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in AFC.cfg file
        self.led_tool_loaded             = config.get('led_tool_loaded', self.afc.led_tool_loaded)           # LED color to set when lane is loaded into tool    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in AFC.cfg file
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
        self.debug                       = config.getboolean("debug",            False)                      # Turns on/off debug messages to console
        self.rev_long_moves_speed_factor = config.getfloat("rev_long_moves_speed_factor", self.afc.rev_long_moves_speed_factor)

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

        # Time in seconds to enable spooler at full speed to help with getting the spool to spin
        self.kick_start_time        = config.getfloat("kick_start_time",        0.070)
        # Cycles per rotation in milliseconds
        self.cycles_per_rotation    = config.getfloat("cycles_per_rotation",    800)
        # PWM cycle time
        self.pwm_value              = config.getfloat("pwm_value",              0.6706)
        # Delta amount in mm from last move to trigger assist
        self.delta_movement         = config.getfloat("delta_movement",         150)
        # Amount to move in mm once filament has moved by delta movement amount
        self.mm_movement            = config.getfloat("mm_movement",            150)
        # Scaling factor for the following variables: kick_start_time, spool_outer_diameter, cycles_per_rotation, pwm_value, delta_movement, mm_movement
        self.scaling                = config.getfloat("spoolrate",              1.0)

        # If True, the unload retract is assisted to prevent loose windings, especially on full spools. This can prevent loops from slipping off the spool. Setting value here overrides values set in AFC.cfg file
        self.assisted_unload    = config.getboolean("assisted_unload", self.afc.assisted_unload)
        # When True AFC will unload lane and then pause when runout is triggered and spool to swap to is not set(infinite spool). Setting value here overrides values set in AFC.cfg file
        self.unload_on_runout   = config.getboolean("unload_on_runout", self.afc.unload_on_runout)

        # TD-1 variables
        self.td1_when_loaded    = config.getboolean("capture_td1_when_loaded", self.afc.td1_when_loaded)
        self.td1_device_id      = config.get("td1_device_id", None)

    def __str__(self):
        return self.name

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
                raise error(error_string)

        # Error checking for extruder
        if self.extruder is not None:
            try:
                self.extruder_obj = self.printer.lookup_object("AFC_extruder {}".format(self.extruder))
            except:
                error_string = 'Error: No config found for extruder: {extruder} in [AFC_{unit_type} {unit_name}]. Please make sure [AFC_extruder {extruder}] section exists in your config'.format(
                    extruder=self.extruder, unit_type=self.type.replace("_", ""), unit_name=self.name )
                raise error(error_string)

        # Error checking for buffer
        if self.buffer_name is not None:
            try:
                self.buffer_obj = self.printer.lookup_object('AFC_buffer {}'.format(self.buffer_name))
            except:
                error_string = 'Error: No config found for buffer: {buffer} in [AFC_{unit_type} {unit_name}]. Please make sure [AFC_buffer {buffer}] section exists in your config'.format(
                    buffer=self.buffer_name, unit_type=self.type.replace("_", ""), unit_name=self.name )
                raise error(error_string)

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
            if lane.hub is not None and lane.hub not in response["hubs"]: response["hubs"].append(lane.hub)
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
        prompt = AFCprompt(gcmd, self.logger)
        buttons = []
        title = '{} Calibration'.format(self.name)
        text = 'Select to calibrate the distance from extruder to hub or bowden length'
        # Selection buttons
        buttons.append(("Calibrate Lanes", "UNIT_LANE_CALIBRATION UNIT={}".format(self.name), "primary"))
        buttons.append(("Calibrate afc_bowden_length", "UNIT_BOW_CALIBRATION UNIT={}".format(self.name), "secondary"))

        # Add button for TD-1 calibration if user has one connected and defined
        if self.afc.td1_defined:
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
        text  = ('Select a loaded lane from {} to calibrate length from extruder to hub. '
                 'Config option: dist_hub').format(self.name)

        # Create buttons for each lane and group every 4 lanes together
        for lane in self.lanes.values():
            if lane.load_state:
                button_label = "{}".format(lane)
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
            if lane.load_state:
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
            if lane.load_state:
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

    def lane_loaded(self, lane):
        """
        Common function for setting a lanes led when lane is loaded

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_ready, lane.led_index)

    def lane_unloaded(self, lane):
        """
        Common function for setting a lanes led when lane is unloaded

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_not_ready, lane.led_index)

    def lane_loading(self, lane):
        """
        Common function for setting a lanes led when lane is loading

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_loading, lane.led_index)

    def lane_tool_loaded(self, lane):
        """
        Common function for setting a lanes led when lane is tool loaded

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_tool_loaded, lane.led_index)

    def lane_tool_unloaded(self, lane):
        """
        Common function for setting a lanes led when lane is tool unloaded

        :param lane: Lane object to set led
        """
        self.afc.function.afc_led(lane.led_ready, lane.led_index)
        return

    def select_lane( self, lane ):
        """
        Function to select lane
        """
        return

    def return_to_home(self ):
        """
        Function to home unit if unit has homing sensor
        """
        return

    def check_runout(self):
        """
        Function to check if runout logic should be triggered, override in specific unit
        """
        return False

    # Functions are below are placeholders so the function exists for all units, override these function in your unit files
    def _print_function_not_defined(self, name):
        self.afc.gcode("{} function not defined for {}".format(name, self.name))

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

    def get_td1_data(self, cur_lane, compare_time):
        """
        Queries moonrakers endpoint to get td1 data and check to see if data is valid and time
        in data is greater than passed in time as this is how determination is made that filament
        made it TD-1 device. Once filament is detected and valued, information is save to passed in lane.

        :param cur_lane: Current lane to apply TD-1 data to, also check's to see if lane has a specific TD-1 ID
                         assigned to the lane.
        :param compare_time: Time to compare returned data to, which helps verify that the data is valid and
                             filament has reached TD-1 device

        :return boolean: True once filament is detected in TD-1 device
        """
        td1_data = self.afc.moonraker.get_td1_data()
        t_delta = timedelta(seconds = 10)
        valid_data = False

        if len(td1_data) > 0:
            self.logger.debug(f"Data: {td1_data}, Compare_time: {compare_time}")
            data = list(td1_data.values())[0]

            if cur_lane.td1_device_id is not None:
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

            if valid_data and data['td'] is not None and data['color'] is not None:
                cur_lane.td1_data = data
                self.logger.info(f"{cur_lane.name} TD-1 data captured")
                self.afc.save_vars()
                return True
        return False