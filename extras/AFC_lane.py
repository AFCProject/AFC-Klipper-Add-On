# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import math
import traceback

from contextlib import contextmanager
from configfile import error
from datetime import datetime
from enum import Enum

try: from extras.AFC_utils import ERROR_STR, add_filament_switch
except: raise error("Error when trying to import AFC_utils.ERROR_STR, add_filament_switch\n{trace}".format(trace=traceback.format_exc()))

try: from extras import AFC_assist
except: raise error(ERROR_STR.format(import_lib="AFC_assist", trace=traceback.format_exc()))

try: from extras.AFC_stats import AFCStats_var
except: raise error(ERROR_STR.format(import_lib="AFC_stats", trace=traceback.format_exc()))

# Class for holding different states so its clear what all valid states are

class AssistActive(Enum):
    YES = 1
    NO = 2
    DYNAMIC = 3
class SpeedMode(Enum):
    NONE = None
    LONG = 1
    SHORT = 2
    HUB = 3
    NIGHT = 4

class AFCLaneState:
    NONE             = "None"
    ERROR            = "Error"
    LOADED           = "Loaded"
    TOOLED           = "Tooled"
    TOOL_LOADED      = "Tool Loaded"
    TOOL_LOADING     = "Tool Loading"
    TOOL_UNLOADING   = "Tool Unloading"
    HUB_LOADING      = "HUB Loading"
    EJECTING         = "Ejecting"
    CALIBRATING      = "Calibrating"

class AFCLane:
    UPDATE_WEIGHT_DELAY = 10.0
    def __init__(self, config):
        self.printer            = config.get_printer()
        self.afc                = self.printer.lookup_object('AFC')
        self.gcode              = self.printer.lookup_object('gcode')
        self.reactor            = self.printer.get_reactor()
        self.extruder_stepper   = None
        self.logger             = self.afc.logger
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("afc:moonraker_connect", self.handle_moonraker_connect)
        self.cb_update_weight   = self.reactor.register_timer( self.update_weight_callback )

        self.unit_obj           = None
        self.hub_obj            = None
        self.buffer_obj         = None
        self.extruder_obj       = None

        #stored status variables
        self.fullname           = config.get_name()
        self.name               = self.fullname.split()[-1]
        # TODO: Put these variables into a common class or something so they are easier to clear out
        # when lanes are unloaded
        self.tool_loaded        = False
        self.loaded_to_hub      = False
        self.spool_id           = None
        self.color              = None
        self.weight             = 0
        self._material          = None
        self.extruder_temp      = None
        self.bed_temp           = None
        self.td1_data           = {}
        self.runout_lane        = None
        self.status             = AFCLaneState.NONE
        # END TODO

        self.multi_hubs_found   = False
        self.drive_stepper      = None
        unit                    = config.get('unit')                                    # Unit name(AFC_BoxTurtle/NightOwl/etc) that belongs to this stepper.
        # Overrides buffers set at the unit level
        self.hub                = config.get('hub',None)                                # Hub name(AFC_hub) that belongs to this stepper, overrides hub that is set in unit(AFC_BoxTurtle/NightOwl/etc) section.
        # Overrides buffers set at the unit and extruder level
        self.buffer_name        = config.get("buffer", None)                            # Buffer name(AFC_buffer) that belongs to this stepper, overrides buffer that is set in extruder(AFC_extruder) or unit(AFC_BoxTurtle/NightOwl/etc) sections.
        self.unit               = unit.split(':')[0]
        try:
            self.index              = int(unit.split(':')[1])
        except:
            self.index              = 0
            pass

        self.extruder_name      = config.get('extruder', None)                          # Extruder name(AFC_extruder) that belongs to this stepper, overrides extruder that is set in unit(AFC_BoxTurtle/NightOwl/etc) section.
        self.map                = config.get('cmd', None)                               # Keeping this in so it does not break others config that may have used this, use map instead
        # Saving to self._map so that if a user has it defined it will be reset back to this when
        # the calling RESET_AFC_MAPPING macro.
        self._map = self.map    = config.get('map', self.map)
        self.led_index          = config.get('led_index', None)                         # LED index of lane in chain of lane LEDs
        self.led_fault          = config.get('led_fault',None)                          # LED color to set when faults occur in lane        (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.led_ready          = config.get('led_ready',None)                          # LED color to set when lane is ready               (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.led_not_ready      = config.get('led_not_ready',None)                      # LED color to set when lane not ready              (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.led_loading        = config.get('led_loading',None)                        # LED color to set when lane is loading             (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.led_prep_loaded    = config.get('led_loading',None)                        # LED color to set when lane is loaded              (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.led_unloading      = config.get('led_unloading',None)                      # LED color to set when lane is unloading           (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.led_tool_loaded    = config.get('led_tool_loaded',None)                    # LED color to set when lane is loaded into tool    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.led_spool_index    = config.get('led_spool_index', None)                   # LED index to illuminate under spool
        self.led_spool_illum    = config.get('led_spool_illuminate', None)              # LED color to illuminate under spool

        self.long_moves_speed   = config.getfloat("long_moves_speed", None)             # Speed in mm/s to move filament when doing long moves. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.long_moves_accel   = config.getfloat("long_moves_accel", None)             # Acceleration in mm/s squared when doing long moves. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.short_moves_speed  = config.getfloat("short_moves_speed", None)            # Speed in mm/s to move filament when doing short moves. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.short_moves_accel  = config.getfloat("short_moves_accel", None)            # Acceleration in mm/s squared when doing short moves. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.short_move_dis     = config.getfloat("short_move_dis", None)               # Move distance in mm for failsafe moves. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.max_move_dis       = config.getfloat("max_move_dis", None)                 # Maximum distance to move filament. AFC breaks filament moves over this number into multiple moves. Useful to lower this number if running into timer too close errors when doing long filament moves. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.n20_break_delay_time= config.getfloat("n20_break_delay_time", None)        # Time to wait between breaking n20 motors(nSleep/FWD/RWD all 1) and then releasing the break to allow coasting. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section

        self.rev_long_moves_speed_factor = config.getfloat("rev_long_moves_speed_factor", None)     # scalar speed factor when reversing filamentalist

        self.dist_hub           = config.getfloat('dist_hub', 60)                       # Bowden distance between Box Turtle extruder and hub
        self.park_dist          = config.getfloat('park_dist', 10)                      # Currently unused

        self.load_to_hub        = config.getboolean("load_to_hub", self.afc.load_to_hub) # Fast loads filament to hub when inserted, set to False to disable. Setting here overrides global setting in AFC.cfg
        self.enable_sensors_in_gui  = config.getboolean("enable_sensors_in_gui",    self.afc.enable_sensors_in_gui) # Set to True to show prep and load sensors switches as filament sensors in mainsail/fluidd gui, overrides value set in AFC.cfg
        self.debounce_delay         = config.getfloat("debounce_delay",             self.afc.debounce_delay)
        self.enable_runout          = config.getboolean("enable_hub_runout",        self.afc.enable_hub_runout)
        self.sensor_to_show         = config.get("sensor_to_show", None)                # Set to prep to only show prep sensor, set to load to only show load sensor. Do not add if you want both prep and load sensors to show in web gui

        self.assisted_unload    = config.getboolean("assisted_unload", None) # If True, the unload retract is assisted to prevent loose windings, especially on full spools. This can prevent loops from slipping off the spool. Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section
        self.td1_when_loaded    = config.getboolean("capture_td1_when_loaded", None)
        self.td1_device_id      = config.get("td1_device_id", None)


        self.printer.register_event_handler("AFC_unit_{}:connect".format(self.unit),self.handle_unit_connect)

        self.config_dist_hub = self.dist_hub

        # lane triggers
        buttons = self.printer.load_object(config, "buttons")
        self.prep = config.get('prep', None)                                    # MCU pin for prep trigger
        self.prep_state = False
        if self.prep is not None:
            buttons.register_buttons([self.prep], self.prep_callback)

        self.load = config.get('load', None)                                    # MCU pin load trigger
        self.load_state = False
        if self.load is not None:
            buttons.register_buttons([self.load], self.load_callback)
        else: self.load_state = True

        self.espooler = AFC_assist.Espooler(self.name, config)
        self.lane_load_count = None

        self.filament_diameter  = config.getfloat("filament_diameter", 1.75)    # Diameter of filament being used
        self.filament_density   = config.getfloat("filament_density", 1.24)     # Density of filament being used
        self.inner_diameter     = config.getfloat("spool_inner_diameter", 100)  # Inner diameter in mm
        self.outer_diameter     = config.getfloat("spool_outer_diameter", 200)  # Outer diameter in mm
        self.empty_spool_weight = config.getfloat("empty_spool_weight", 190)    # Empty spool weight in g
        self.max_motor_rpm      = config.getfloat("assist_max_motor_rpm", 500)  # Max motor RPM
        self.rwd_speed_multi    = config.getfloat("rwd_speed_multiplier", 0.5)  # Multiplier to apply to rpm
        self.fwd_speed_multi    = config.getfloat("fwd_speed_multiplier", 0.5)  # Multiplier to apply to rpm
        self.diameter_range     = self.outer_diameter - self.inner_diameter     # Range for effective diameter
        self.past_extruder_position = -1
        self.save_counter       = -1

        # Defaulting to false so that extruder motors to not move until PREP has been called
        self._afc_prep_done = False

        if self.prep is not None:
            show_sensor = True
            if not self.enable_sensors_in_gui or (self.sensor_to_show is not None and 'prep' not in self.sensor_to_show):
                show_sensor = False
            self.fila_prep, self.prep_debounce_button = add_filament_switch(f"{self.name}_prep", self.prep, self.printer,
                                                                            show_sensor, enable_runout=self.enable_runout,
                                                                            debounce_delay=self.debounce_delay )
            self.prep_debounce_button.button_action = self.handle_prep_runout
            self.prep_debounce_button.debounce_delay = 0 # Delay will be set once klipper is ready

        if self.load is not None:
            show_sensor = True
            if not self.enable_sensors_in_gui or (self.sensor_to_show is not None and 'load' not in self.sensor_to_show):
                show_sensor = False
            self.fila_load, self.load_debounce_button = add_filament_switch(f"{self.name}_load", self.load, self.printer,
                                                                            show_sensor, enable_runout=self.enable_runout,
                                                                            debounce_delay=self.debounce_delay )
            self.load_debounce_button.button_action = self.handle_load_runout
            self.load_debounce_button.debounce_delay = 0 # Delay will be set once klipper is ready

        self.connect_done = False
        self.prep_active = False
        self.last_prep_time = 0

        self.show_macros = self.afc.show_macros
        self.function = self.printer.load_object(config, 'AFC_functions')
        self.function.register_mux_command(self.show_macros, 'SET_LANE_LOADED', 'LANE', self.name,
                                           self.cmd_SET_LANE_LOADED, self.cmd_SET_LANE_LOADED_help,
                                           self.cmd_SET_LANE_LOAD_options )

    def __str__(self):
        return self.name

    @property
    def material(self):
        """
        Returns lanes filament material type
        """
        return self._material

    @material.setter
    def material(self, value):
        """
        Sets filament material type and sets filament density based off material type.
        To use custom density, set density after setting material
        """
        self._material = value
        if not value:
            self.filament_density = 1.24 # Setting to a default value
            return

        for density in self.afc.common_density_values:
            v = density.split(":")
            if v[0] in value:
                self.filament_density = float(v[1])
                break

    def _handle_ready(self):
        """
        Handles klippy:ready callback and verifies that steppers have units defined in their config
        """
        if self.unit_obj is None:
            raise error("Unit {unit} is not defined in your configuration file. Please defined unit ex. [AFC_BoxTurtle {unit}]".format(unit=self.unit))

        if self.led_index is not None:
            # Verify that LED config is found
            error_string, led = self.afc.function.verify_led_object(self.led_index)
            if led is None:
                raise error(error_string)
        self.espooler.handle_ready()

        # Setting debounce delay after ready so that callback does not get triggered when initially loading
        if hasattr(self, "prep_debounce_button"):
            self.prep_debounce_button.debounce_delay = self.debounce_delay
        if hasattr(self, "load_debounce_button"):
            self.load_debounce_button.debounce_delay = self.debounce_delay

    def handle_moonraker_connect(self):
        """
        Function that should be called at the beginning of PREP so that moonraker has
        enough time to start before AFC tries to connect. This fixes a race condition that can
        happen between klipper and moonraker when first starting up.
        """
        if self.unit_obj.type != "HTLF" or (self.unit_obj.type == "HTLF" and "AFC_lane" in self.fullname):
            values = None
            if self.afc.moonraker.afc_stats is not None:
                values = self.afc.moonraker.afc_stats["value"]
            self.lane_load_count = AFCStats_var(self.name, "load_count", values, self.afc.moonraker)
            self.espooler.handle_moonraker_connect()

            # Update boolean and check to make sure a TD-1 device is detected
            self.td1_when_loaded = self.td1_when_loaded and self.afc.td1_defined

    def handle_unit_connect(self, unit_obj):
        """
        Callback from <unit_name>:connect to verify units/hub/buffer/extruder object. Errors out if user specified names and they do not exist in their configuration
        """
        # Saving reference to unit
        self.unit_obj = unit_obj
        self.buffer_obj = self.unit_obj.buffer_obj
        add_to_other_obj = False

        # Register all lanes if their type is not HTLF or only register lanes that are HTLF and have AFC_lane
        # in the name so that HTLF stepper names do not get added since they are not a lane for this unit type
        if self.unit_obj.type != "HTLF" or (self.unit_obj.type == "HTLF" and "AFC_lane" in self.fullname):
            add_to_other_obj = True
            # Registering lane name in unit
            self.unit_obj.lanes[self.name] = self
            self.afc.lanes[self.name] = self


        self.hub_obj = self.unit_obj.hub_obj

        if self.hub != 'direct':
            if self.hub is not None:
                try:
                    self.hub_obj = self.printer.lookup_object("AFC_hub {}".format(self.hub))
                except:
                    error_string = 'Error: No config found for hub: {hub} in [AFC_stepper {stepper}]. Please make sure [AFC_hub {hub}] section exists in your config'.format(
                    hub=self.hub, stepper=self.name )
                    raise error(error_string)
            elif self.hub_obj is None:
                # Check to make sure at least 1 hub exists in config, if not error out with message
                if len(self.afc.hubs) == 0:
                    error_string = "Error: AFC_hub not found in configuration please make sure there is a [AFC_hub <hub_name>] defined in your configuration"
                    raise error(error_string)
                # Setting hub to first hub in AFC hubs dictionary
                if len(self.afc.hubs) > 0:
                    self.hub_obj = next(iter(self.afc.hubs.values()))
                # Set flag to warn during prep that multiple hubs were found
                if len(self.afc.hubs) > 1:
                    self.multi_hubs_found = True

            # Assigning hub name just in case stepper is using hub defined in units config
            self.hub = self.hub_obj.name
            if add_to_other_obj:
                self.hub_obj.lanes[self.name] = self
        else:
            self.hub_obj = lambda: None
            self.hub_obj.state = False

        self.extruder_obj = self.unit_obj.extruder_obj
        if self.extruder_name is not None:
            try:
                self.extruder_obj = self.printer.lookup_object('AFC_extruder {}'.format(self.extruder_name))
            except:
                error_string = 'Error: No config found for extruder: {extruder} in [AFC_stepper {stepper}]. Please make sure [AFC_extruder {extruder}] section exists in your config'.format(
                    extruder=self.extruder_name, stepper=self.name )
                raise error(error_string)
        elif self.extruder_obj is None:
            error_string = "Error: Extruder has not been configured for stepper {name}, please add extruder variable to either [AFC_stepper {name}] or [AFC_{unit_type} {unit_name}] in your config file".format(
                        name=self.name, unit_type=self.unit_obj.type.replace("_", ""), unit_name=self.unit_obj.name)
            raise error(error_string)

        # Assigning extruder name just in case stepper is using extruder defined in units config
        self.extruder_name = self.extruder_obj.name
        if add_to_other_obj:
            self.extruder_obj.lanes[self.name] = self

        # Use buffer defined in stepper and override buffers that maybe set at the UNIT or extruder levels
        self.buffer_obj = self.unit_obj.buffer_obj
        if self.buffer_name is not None:
            try:
                self.buffer_obj = self.printer.lookup_object("AFC_buffer {}".format(self.buffer_name))
            except:
                error_string = 'Error: No config found for buffer: {buffer} in [AFC_stepper {stepper}]. Please make sure [AFC_buffer {buffer}] section exists in your config'.format(
                    buffer=self.buffer_name, stepper=self.name )
                raise error(error_string)

        # Checking if buffer was defined in extruder if not defined in unit/stepper
        elif self.buffer_obj is None and self.extruder_obj.tool_start == "buffer":
            if self.extruder_obj.buffer_name is not None:
                self.buffer_obj = self.printer.lookup_object("AFC_buffer {}".format(self.extruder_obj.buffer_name))
            else:
                error_string = 'Error: Buffer was defined as tool_start in [AFC_extruder {extruder}] config, but buffer variable has not been configured. Please add buffer variable to either [AFC_extruder {extruder}], [AFC_stepper {name}] or [AFC_{unit_type} {unit_name}] section in your config file'.format(
                    extruder=self.extruder_obj.name, name=self.name, unit_type=self.unit_obj.type.replace("_", ""), unit_name=self.unit_obj.name )
                raise error(error_string)

        # Valid to not have a buffer defined, check to make sure object exists before adding lane to buffer
        if self.buffer_obj is not None and add_to_other_obj:
            self.buffer_obj.lanes[self.name] = self
            # Assigning buffer name just in case stepper is using buffer defined in units/extruder config
            self.buffer_name = self.buffer_obj.name

        self.get_steppers()

        if self.led_fault           is None: self.led_fault         = self.unit_obj.led_fault
        if self.led_ready           is None: self.led_ready         = self.unit_obj.led_ready
        if self.led_not_ready       is None: self.led_not_ready     = self.unit_obj.led_not_ready
        if self.led_loading         is None: self.led_loading       = self.unit_obj.led_loading
        if self.led_prep_loaded     is None: self.led_prep_loaded   = self.unit_obj.led_prep_loaded
        if self.led_unloading       is None: self.led_unloading     = self.unit_obj.led_unloading
        if self.led_tool_loaded     is None: self.led_tool_loaded   = self.unit_obj.led_tool_loaded
        if self.led_spool_illum     is None: self.led_spool_illum   = self.unit_obj.led_spool_illum

        if self.rev_long_moves_speed_factor is None: self.rev_long_moves_speed_factor  = self.unit_obj.rev_long_moves_speed_factor
        if self.long_moves_speed            is None: self.long_moves_speed  = self.unit_obj.long_moves_speed
        if self.long_moves_accel            is None: self.long_moves_accel  = self.unit_obj.long_moves_accel
        if self.short_moves_speed           is None: self.short_moves_speed = self.unit_obj.short_moves_speed
        if self.short_moves_accel           is None: self.short_moves_accel = self.unit_obj.short_moves_accel
        if self.short_move_dis              is None: self.short_move_dis    = self.unit_obj.short_move_dis
        if self.max_move_dis                is None: self.max_move_dis      = self.unit_obj.max_move_dis
        if self.td1_when_loaded             is None: self.td1_when_loaded   = self.unit_obj.td1_when_loaded
        if self.td1_device_id               is None: self.td1_device_id     = self.unit_obj.td1_device_id

        if self.rev_long_moves_speed_factor < 0.5: self.rev_long_moves_speed_factor = 0.5
        if self.rev_long_moves_speed_factor > 1.2: self.rev_long_moves_speed_factor = 1.2

        self.espooler.handle_connect(self)

        # Set hub loading speed depending on distance between extruder and hub
        self.dist_hub_move_speed = self.long_moves_speed if self.dist_hub >= 200 else self.short_moves_speed
        self.dist_hub_move_accel = self.long_moves_accel if self.dist_hub >= 200 else self.short_moves_accel

        # Register macros
        # TODO: add check so that HTLF stepper lanes do not get registered here
        self.afc.gcode.register_mux_command('SET_LONG_MOVE_SPEED',   "LANE", self.name, self.cmd_SET_LONG_MOVE_SPEED, desc=self.cmd_SET_LONG_MOVE_SPEED_help)
        self.afc.gcode.register_mux_command('SET_SPEED_MULTIPLIER',  "LANE", self.name, self.cmd_SET_SPEED_MULTIPLIER, desc=self.cmd_SET_SPEED_MULTIPLIER_help)
        self.afc.gcode.register_mux_command('SAVE_SPEED_MULTIPLIER', "LANE", self.name, self.cmd_SAVE_SPEED_MULTIPLIER, desc=self.cmd_SAVE_SPEED_MULTIPLIER_help)
        self.afc.gcode.register_mux_command('SET_HUB_DIST',          "LANE", self.name, self.cmd_SET_HUB_DIST, desc=self.cmd_SET_HUB_DIST_help)
        self.afc.gcode.register_mux_command('SAVE_HUB_DIST',         "LANE", self.name, self.cmd_SAVE_HUB_DIST, desc=self.cmd_SAVE_HUB_DIST_help)

        if self.assisted_unload is None: self.assisted_unload = self.unit_obj.assisted_unload

        # Send out event so that macros and be registered properly with valid lane names
        self.printer.send_event("afc_stepper:register_macros", self)

        self.connect_done = True

    def get_steppers(self):
        """
        Helper function to get steppers for lane
        """
        if self.unit_obj.type == "HTLF" and "AFC_lane" in self.fullname:
            self.drive_stepper      = self.unit_obj.drive_stepper_obj
            self.extruder_stepper   = self.drive_stepper.extruder_stepper

    def get_color(self):
        """
        Helper function for returning current color

        :return str: If TD-1 device is present, returns scanned color. If its not present, returns
                     manually entered or color from spoolman
        """
        color = self.color
        if "color" in self.td1_data:
            color = f"#{self.td1_data['color']}"
        return color

    @contextmanager
    def assist_move(self, speed, rewind, assist_active=True):
        """
        Starts an assist move and returns a context manager that turns off the assist move when it exist.
        :param speed:         The speed of the move
        :param rewind:        True for a rewind, False for a forward assist
        :param assist_active: Whether to assist
        :return:              the Context manager
        """
        if assist_active:
            if rewind:
                # Calculate Rewind Speed
                value = self.calculate_pwm_value(speed, True) * -1
            else:
                # Calculate Forward Assist Speed
                value = self.calculate_pwm_value(speed)

            # Clamp value to a maximum of 1
            if value > 1:
                value = 1

            self.espooler.assist(value)
        try:
            yield
        finally:
            if assist_active:
                self.espooler.assist(0)

    def move_auto_speed(self, distance):
        """
        Helper function for determining speed and accel from passed in distance

        :param distance: Distance to move filament
        """
        dist_hub_move_speed, dist_hub_move_accel, assist_active = self.get_speed_accel(mode=SpeedMode.NONE,
                                                                                       distance=distance)
        self.move(distance, dist_hub_move_speed, dist_hub_move_accel, assist_active)

    def get_speed_accel(self, mode: SpeedMode, distance=None) -> float:
        """
        Helper function to allow selecting the right speed and acceleration of movements
        mode (Enum SpeedMode): Identifies which speed to use.
        """
        if distance is not None and mode is SpeedMode.NONE:
            if abs(distance) > 200:
                return self.long_moves_speed, self.long_moves_accel, True
            else:
                return self.short_moves_speed, self.long_moves_accel, False
        else:
            if self.afc._get_quiet_mode() == True:
                return self.afc.quiet_moves_speed, self.short_moves_accel
            elif mode == SpeedMode.LONG:
                return self.long_moves_speed, self.long_moves_accel
            elif mode == SpeedMode.SHORT:
                return self.short_moves_speed, self.short_moves_accel
            else:
                return self.dist_hub_move_speed, self.dist_hub_move_accel

    def move(self, distance, speed, accel, assist_active=False):
        """
        Move the specified lane a given distance with specified speed and acceleration.
        This function calculates the movement parameters and commands the stepper motor
        to move the lane accordingly.
        Parameters:
        distance (float): The distance to move.
        speed (float): The speed of the movement.
        accel (float): The acceleration of the movement.
        """
        self.unit_obj.select_lane( self )
        with self.assist_move( speed, distance < 0, assist_active):
            if self.drive_stepper is not None:
                self.drive_stepper.move(distance, speed, accel, assist_active)

    def move_advanced(self, distance, speed_mode: SpeedMode, assist_active: AssistActive = AssistActive.NO):
        """
        Wrapper for move function and is used to compute several arguments
        to move the lane accordingly.
        Parameters:
        distance (float): The distance to move.
        speed_mode (Enum SpeedMode): Identifies which speed to use.
        assist_active (Enum AssistActive): Determines to force assist or to dynamically determine.
        """
        speed, accel = self.get_speed_accel(speed_mode)

        assist = False
        if assist_active == AssistActive.YES:
            assist = True
        elif assist_active == AssistActive.DYNAMIC:
            assist = abs(distance) > 200

        self.move(distance, speed, accel, assist)

    def set_afc_prep_done(self):
        """
        set_afc_prep_done function should only be called once AFC PREP function is done. Once this
            function is called it sets afc_prep_done to True. Once this is done the prep_callback function will
            now load once filament is inserted.
        """
        self._afc_prep_done = True

    def _perform_infinite_runout(self):
        """
        Common function for infinite spool runout
            - Unloads current lane and loads the next lane as specified by runout variable.
            - Swaps mapping between current lane and runout lane so correct lane is loaded with T(n) macro
            - Once changeover is successful print is automatically resumed
        """
        self.status = AFCLaneState.NONE
        self.afc.function.afc_led(self.afc.led_not_ready, self.led_index)
        self.logger.info("Infinite Spool triggered for {}".format(self.name))
        empty_lane = self.afc.lanes[self.afc.current]
        change_lane = self.afc.lanes[self.runout_lane]
        # Pause printer with manual command
        self.afc.error.pause_resume.send_pause_command()
        # Saving position after printer is paused
        self.afc.save_pos()
        # Change Tool and don't restore position. Position will be restored after lane is unloaded
        #  so that nozzle does not sit on print while lane is unloading
        self.afc.CHANGE_TOOL(change_lane, restore_pos=False)
        # Change Mapping
        self.gcode.run_script_from_command('SET_MAP LANE={} MAP={}'.format(change_lane.name, empty_lane.map))
        # Only continue if a error did not happen
        if not self.afc.error_state:
            # Eject lane from BT
            self.gcode.run_script_from_command('LANE_UNLOAD LANE={}'.format(empty_lane.name))
            # Resume pos
            self.afc.restore_pos()
            # Resume with manual issued command
            self.afc.error.pause_resume.send_resume_command()
            # Set LED to not ready
            self.afc.function.afc_led(self.led_not_ready, self.led_index)

    def _perform_pause_runout(self):
        """
        Common function to pause print when runout occurs, fully unloads and ejects spool if specified by user
        """
        # Unload if user has set AFC to unload on runout
        if self.unit_obj.unload_on_runout:
            # Pause printer
            self.afc.error.pause_resume.send_pause_command()
            self.afc.save_pos()
            # self.gcode.run_script_from_command('PAUSE')
            self.afc.TOOL_UNLOAD(self)
            if not self.afc.error_state:
                self.afc.LANE_UNLOAD(self)
        # Pause print
        self.status = AFCLaneState.NONE
        msg = "Runout triggered for lane {} and runout lane is not setup to switch to another lane".format(self.name)
        msg += "\nPlease manually load next spool into toolhead and then hit resume to continue"
        self.afc.function.afc_led(self.afc.led_not_ready, self.led_index)
        self.afc.error.AFC_error(msg)

    def _prep_capture_td1(self):
        """
        Common function to grab TD-1 data once user inserts filament into a lane. Only happens if user has specified
        this by setting `capture_td1_when_loaded: True` and if hub is clear and toolhead is not loaded.
        """
        if self.td1_when_loaded:
            if not self.hub_obj.state and self.afc.function.get_current_lane_obj() is None:
                self.get_td1_data()
            else:
                self.logger.info(f"Cannot get TD-1 data for {self.name}, either toolhead is loaded or hub shows filament in path")


    def load_callback(self, eventtime, state):
        self.load_state = state
        if self.printer.state_message == 'Printer is ready' and self.unit_obj.type == "HTLF":
            self.prep_state = state

    def handle_load_runout(self, eventtime, load_state):
        """
        Callback function for load switch runout/loading for HTLF, this is different than `load_callback`
        function as this function can be delayed and is called from filament_switch_sensor class when it detects a runout event.

        Before exiting `min_event_systime` is updated as this mimics how its done in `_exec_gcode` function in RunoutHelper class
        as AFC overrides `_runout_event_handler` function with this function callback. If `min_event_systime` does not get
        updated then future switch changes will not be detected.

        :param eventtime: Event time from the button press
        """
        # Call filament sensor callback so that state is registered
        try:
            self.load_debounce_button._old_note_filament_present(is_filament_present=load_state)
        except:
            self.load_debounce_button._old_note_filament_present(eventtime, load_state)

        if (self.printer.state_message == 'Printer is ready' and
            self.unit_obj.type == "HTLF" and
            True == self._afc_prep_done):
            if load_state:
                self.status = AFCLaneState.LOADED
                self.unit_obj.lane_loaded(self)
                self.afc.spool._set_values(self)
                # Check if user wants to get TD-1 data when loading
                if not self.tool_loaded:
                    self._prep_capture_td1()
            else:
                # Don't run if user disabled sensor in gui
                if not self.fila_load.runout_helper.sensor_enabled and self.afc.function.is_printing():
                    self.logger.warning("Load runout has been detected, but pause and runout detection has been disabled")
                elif self.unit_obj.check_runout(self):
                    # Checking to make sure runout_lane is set
                    if self.runout_lane is not None:
                        self._perform_infinite_runout()
                    else:
                        self._perform_pause_runout()
                elif self.status != "calibrating":
                    self.tool_loaded = False
                    self.afc.function.afc_led(self.led_not_ready, self.led_index)
                    self.status = AFCLaneState.NONE
                    self.loaded_to_hub = False
                    self.td1_data = {}
                    self.afc.spool.clear_values(self)
                    self.afc.function.afc_led(self.afc.led_not_ready, self.led_index)

        self.afc.save_vars()

    def prep_callback(self, eventtime, state):
        self.prep_state = state

        delta_time = eventtime - self.last_prep_time
        self.last_prep_time = eventtime

        if self.prep_active:
            return

        if self.hub =='direct' and not self.afc.function.is_homed():
            self.afc.error.AFC_error("Please home printer before directly loading to toolhead", False)
            return False

        self.prep_active = True

        # Checking to make sure printer is ready and making sure PREP has been called before trying to load anything
        for i in range(1):
            # Hacky way for do{}while(0) loop, DO NOT return from this for loop, use break instead so that self.prep_state variable gets sets correctly
            #  before exiting function
            if self.printer.state_message == 'Printer is ready' and True == self._afc_prep_done and self.status != AFCLaneState.TOOL_UNLOADING:
                # Only try to load when load state trigger is false
                if self.prep_state == True and self.load_state == False:
                    x = 0
                    # Checking to make sure last time prep switch was activated was less than 1 second, returning to keep is printing message from spamming
                    # the console since it takes klipper some time to transition to idle when idle_resume=printing
                    if delta_time < 1.0:
                        break

                    # Check to see if the printer is printing or moving, as trying to load while printer is doing something will crash klipper
                    if self.afc.function.is_printing(check_movement=True):
                        self.afc.error.AFC_error("Cannot load spools while printer is actively moving or homing", False)
                        break

                    while self.load_state == False and self.prep_state == True and self.load is not None:
                        x += 1
                        self.move(10,500,400)
                        self.reactor.pause(self.reactor.monotonic() + 0.1)
                        if x> 40:
                            msg = ' FAILED TO LOAD, CHECK FILAMENT AT TRIGGER\n||==>--||----||------||\nTRG   LOAD   HUB    TOOL'
                            self.afc.error.AFC_error(msg, False)
                            self.afc.function.afc_led(self.afc.led_fault, self.led_index)
                            self.status = AFCLaneState.NONE
                            break
                    self.status = AFCLaneState.NONE

                    # Verify that load state is still true as this would still trigger if prep sensor was triggered and then filament was removed
                    #   This is only really a issue when using direct and still using load sensor
                    if self.hub == 'direct' and self.prep_state:
                        self.afc.afcDeltaTime.set_start_time()
                        self.afc.TOOL_LOAD(self)
                        self.material = self.afc.default_material_type
                        break

                    # Checking if loaded to hub(it should not be since filament was just inserted), if false load to hub. Does a fast load if hub distance is over 200mm
                    if self.load_to_hub and not self.loaded_to_hub and self.load_state and self.prep_state:
                        self.move(self.dist_hub, self.dist_hub_move_speed, self.dist_hub_move_accel, self.dist_hub > 200)
                        self.loaded_to_hub = True

                    self.do_enable(False)
                    if self.load_state == True and self.prep_state == True:
                        self.status = AFCLaneState.LOADED
                        self.unit_obj.lane_loaded(self)
                        self.afc.spool._set_values(self)
                        # Check if user wants to get TD-1 data when loading
                        # TODO: When implementing multi-extruder this could still happen if a lane is loaded for a
                        # different extruder/hub
                        self._prep_capture_td1()

                elif self.prep_state == True and self.load_state == True and not self.afc.function.is_printing():
                    message = 'Cannot load {} load sensor is triggered.'.format(self.name)
                    message += '\n    Make sure filament is not stuck in load sensor or check to make sure load sensor is not stuck triggered.'
                    message += '\n    Once cleared try loading again'
                    self.afc.error.AFC_error(message, pause=False)
        self.prep_active = False
        self.afc.save_vars()

    def handle_prep_runout(self, eventtime, prep_state):
        """
        Callback function for prep switch runout, this is different than `prep_callback`
        function as this function can be delayed and is called from filament_switch_sensor class when it detects a runout event.

        Before exiting `min_event_systime` is updated as this mimics how its done in `_exec_gcode` function in RunoutHelper class
        as AFC overrides `_runout_event_handler` function with this function callback. If `min_event_systime` does not get
        updated then future switch changes will not be detected.

        :param eventtime: Event time from the button press
        """
        # Call filament sensor callback so that state is registered
        try:
            self.prep_debounce_button._old_note_filament_present(is_filament_present=prep_state)
        except:
            self.prep_debounce_button._old_note_filament_present(eventtime, prep_state)

        if self.printer.state_message == 'Printer is ready' and True == self._afc_prep_done and self.status != AFCLaneState.TOOL_UNLOADING:
            if prep_state == False and self.name == self.afc.current and self.afc.function.is_printing() and self.load_state and self.status != AFCLaneState.EJECTING:
                # Don't run if user disabled sensor in gui
                if not self.fila_prep.runout_helper.sensor_enabled:
                    self.logger.warning("Prep runout has been detected, but pause and runout detection has been disabled")
                # Checking to make sure runout_lane is set
                elif self.runout_lane is not None:
                    self._perform_infinite_runout()
                else:
                    self._perform_pause_runout()
            elif not prep_state:
                # Filament is unloaded
                self.tool_loaded = False
                self.status = AFCLaneState.NONE
                self.loaded_to_hub = False
                self.td1_data = {}
                self.afc.spool.clear_values(self)
                self.unit_obj.lane_unloaded(self)

        self.afc.save_vars()

    def do_enable(self, enable):
        if self.drive_stepper is not None:
            self.drive_stepper.do_enable(enable)

    def sync_print_time(self):
        return

    def sync_to_extruder(self, update_current=True):
        """
        Helper function to sync lane to extruder and set print current if specified.

        :param update_current: Sets current to specified print current when True
        """
        if self.drive_stepper is not None:
            self.drive_stepper.sync_to_extruder(update_current, extruder_name=self.extruder_name)

    def unsync_to_extruder(self, update_current=True):
        """
        Helper function to un-sync lane to extruder and set load current if specified.

        :param update_current: Sets current to specified load current when True
        """
        if self.drive_stepper is not None:
            self.drive_stepper.unsync_to_extruder(update_current)

    def _set_current(self, current):
        return

    def set_load_current(self):
        """
        Helper function to update TMC current to use run current value
        """
        if self.drive_stepper is not None:
            self.drive_stepper.set_load_current()

    def set_print_current(self):
        """
        Helper function to update TMC current to use print current value
        """
        if self.drive_stepper is not None:
            self.drive_stepper.set_print_current()

    def update_rotation_distance(self, multiplier):
        if self.drive_stepper is not None:
            self.drive_stepper.update_rotation_distance( multiplier )

    def calculate_effective_diameter(self, weight_g, spool_width_mm=60):

        # Calculate the cross-sectional area of the filament
        density_g_mm3 = self.filament_density / 1000.0
        filament_volume_mm3 = weight_g / density_g_mm3
        package_corrected_volume_mm3 = filament_volume_mm3 / 0.785
        filament_area_mm2 = package_corrected_volume_mm3 / spool_width_mm
        spool_outer_diameter_mm2 = (4 * filament_area_mm2 / 3.14159) + self.inner_diameter ** 2
        spool_outer_diameter_mm = spool_outer_diameter_mm2 ** 0.5

        return spool_outer_diameter_mm

    def calculate_rpm(self, feed_rate):
        """
        Calculate the RPM for the assist motor based on the filament feed rate.

        :param feed_rate: Filament feed rate in mm/s
        :return: Calculated RPM for the assist motor
        """
        # Figure in weight of empty spool
        weight = self.weight + self.empty_spool_weight

        # Calculate the effective diameter
        effective_diameter = self.calculate_effective_diameter(weight)

        # Calculate RPM
        rpm = (feed_rate * 60) / (math.pi * effective_diameter)
        return min(rpm, self.max_motor_rpm)  # Clamp to max motor RPM

    def calculate_pwm_value(self, feed_rate, rewind=False):
        """
        Calculate the PWM value for the assist motor based on the feed rate.

        :param feed_rate: Filament feed rate in mm/s
        :return: PWM value between 0 and 1
        """
        rpm = self.calculate_rpm(feed_rate)
        if not rewind:
            pwm_value = rpm / (self.max_motor_rpm / (1 + 9 * self.fwd_speed_multi))
        else:
            pwm_value = rpm / (self.max_motor_rpm / (15 + 15 * self.rwd_speed_multi))
        return max(0.0, min(pwm_value, 1.0))  # Clamp the value between 0 and 1

    def enable_weight_timer(self):
        """
        Helper function to enable weight callback timer, should be called once a lane is loaded
        to extruder or extruder is switched for multi-toolhead setups.
        """
        self.past_extruder_position = self.afc.function.get_extruder_pos( None, self.past_extruder_position )
        self.reactor.update_timer( self.cb_update_weight, self.reactor.monotonic() + self.UPDATE_WEIGHT_DELAY)

    def disable_weight_timer(self):
        """
        Helper function to disable weight callback timer for lane and save variables
        to file. Should only be called when lane is unloaded from extruder or when
        swapping extruders for multi-toolhead setups.
        """
        self.update_weight_callback( None ) # get final movement before disabling timer
        self.reactor.update_timer( self.cb_update_weight, self.reactor.NEVER)
        self.past_extruder_position = -1
        self.save_counter = -1
        self.afc.save_vars()

    def update_weight_callback(self, eventtime):
        """
        Callback function for updating weight based on how much filament has been extruded

        :param eventtime: Current eventtime for timer callback
        :return int: Next time to call timer callback. Current time + UPDATE_WEIGHT_DELAY
        """
        extruder_pos = self.afc.function.get_extruder_pos( eventtime, self.past_extruder_position )
        delta_length = extruder_pos - self.past_extruder_position

        if -1 == self.past_extruder_position:
            self.past_extruder_position = extruder_pos

        self.save_counter += 1
        if extruder_pos > self.past_extruder_position:
            self.update_remaining_weight(delta_length)
            self.past_extruder_position = extruder_pos

            # self.logger.debug(f"{self.name} Weight Timer Callback: New weight {self.weight}")

            # Save vars every 2 minutes
            if self.save_counter > 120/self.UPDATE_WEIGHT_DELAY:
                self.afc.save_vars()
                self.save_counter = 0

        return self.reactor.monotonic() + self.UPDATE_WEIGHT_DELAY

    def update_remaining_weight(self, distance_moved):
        """
        Update the remaining filament weight based on the filament distance moved.

        :param distance_moved: Distance of filament moved in mm.
        """
        filament_volume_mm3 = math.pi * (self.filament_diameter / 2) ** 2 * distance_moved
        filament_weight_change = filament_volume_mm3 * self.filament_density / 1000  # Convert mm cubed to g
        self.weight -= filament_weight_change

        # Weight cannot be negative, force back to zero if it's below zero
        if self.weight < 0:
            self.weight = 0

    def set_loaded(self):
        """
        Helper function for setting multiple variables when lane is loaded
        """
        self.tool_loaded = True
        self.afc.current = self.extruder_obj.lane_loaded = self.name
        self.afc.current_loading = None
        self.status = AFCLaneState.TOOLED
        self.afc.spool.set_active_spool(self.spool_id)

        self.unit_obj.lane_tool_loaded(self)

    def set_unloaded(self):
        """
        Helper function for setting multiple variables when lane is unloaded
        """
        self.tool_loaded = False
        self.extruder_obj.lane_loaded = ""
        self.status = AFCLaneState.NONE
        self.afc.current = None
        self.afc.current_loading = None
        self.afc.spool.set_active_spool(None)
        self.unit_obj.lane_tool_unloaded(self)

    def enable_buffer(self):
        """
        Enable the buffer if `buffer_name` is set.
        Retrieves the buffer object and calls its `enable_buffer()` method to activate it.
        """
        if self.buffer_obj is not None:
            self.buffer_obj.enable_buffer()
        self.espooler.enable_timer()
        self.enable_weight_timer()

    def disable_buffer(self):
        """
        Disable the buffer if `buffer_name` is set.
        Calls the buffer's `disable_buffer()` method to deactivate it.
        """
        if self.buffer_obj is not None:
            self.buffer_obj.disable_buffer()
        self.espooler.disable_timer()
        self.disable_weight_timer()

    def buffer_status(self):
        """
        Retrieve the current status of the buffer.
        If `buffer_name` is set, returns the buffer's status using `buffer_status()`.
        Otherwise, returns None.
        """
        if self.buffer_obj is not None:
            return self.buffer_obj.buffer_status()

        else: return None

    def get_toolhead_pre_sensor_state(self):
        """
        Helper function that returns current state of toolhead pre sensor or buffer if user has extruder setup for ramming

        returns Status of toolhead pre sensor or the current buffer advance state
        """
        if self.extruder_obj.tool_start == "buffer":
            return self.buffer_obj.advance_state
        else:
            return self.extruder_obj.tool_start_state

    def get_trailing(self):
        """
        Helper function to get trailing status, returns none if buffer is not defined
        """
        if self.buffer_obj is not None:
            return self.buffer_obj.trailing_state
        else: return None


    def _is_normal_printing_state(self):
        """
        Returns True if the lane is in a normal printing state (TOOLED or LOADED).
        Prevents runout logic from triggering during transitions or maintenance.
        """
        if self.afc.in_toolchange:
            return False
        return self.status in (AFCLaneState.TOOLED, AFCLaneState.LOADED)

    def handle_toolhead_runout(self, sensor=None):
        """
        Handles runout detection at the toolhead sensor.
        If all upstream sensors (prep, load, hub) still detect filament, this indicates a break or jam at the toolhead.
        Otherwise, triggers normal runout handling logic. Only triggers during normal printing states and when printing.
        :param sensor: Optional name of the triggering sensor for user notification.
        """
        # Only trigger runout logic if in a normal printing state AND printer is actively printing
        if not (self._is_normal_printing_state() and self.afc.function.is_printing()):
            return

        # Check upstream sensors: prep, load, hub
        prep_ok = self.prep_state
        load_ok = self.load_state
        hub_ok = self.hub_obj.state if self.hub_obj is not None else True

        # If all upstream sensors are still True, this is a break/jam at the toolhead
        if prep_ok and load_ok and hub_ok:
            msg = (
                f"Toolhead runout detected by {sensor} sensor, but upstream sensors still detect filament.\n"
                "Possible filament break or jam at the toolhead. Please clear the jam and reload filament manually, then resume the print."
            )
            self.afc.error.pause_resume.send_pause_command()
            self.afc.save_pos()
            self.afc.error.AFC_error(msg)
        # No else: do not trigger infinite runout or pause runout here

    def handle_hub_runout(self, sensor=None):
        """
        Handles runout detection at the hub sensor.
        If both upstream sensors (prep, load) still detect filament but hub does not, this indicates a break or jam at the hub.
        Otherwise, triggers normal runout handling logic. Only triggers during normal printing states and when printing.
        :param sensor: Optional name of the triggering sensor for user notification.
        """
        # Only trigger runout logic if in a normal printing state AND printer is actively printing
        if not (self._is_normal_printing_state() and self.afc.function.is_printing()):
            return

        # Check upstream sensors: prep, load
        prep_ok = self.prep_state
        load_ok = self.load_state
        hub_ok = self.hub_obj.state if self.hub_obj is not None else False

        # If both upstream sensors are still True, but hub is not, this is a break/jam at the hub
        if prep_ok and load_ok and not hub_ok:
            msg = (
                f"Hub runout detected by {sensor or 'hub'} sensor, but upstream sensors still detect filament.\n"
                "Possible filament break or jam at the hub. Please clear the jam and reload filament manually, then resume the print."
            )
            self.afc.error.pause_resume.send_pause_command()
            self.afc.save_pos()
            self.afc.error.AFC_error(msg)
        # No else: do not trigger infinite runout or pause runout here


    def send_lane_data(self):
        """
        Sends lane data to moonrakers `machine/set_lane_data` endpoint
        """
        if self.map is not None and "T" in self.map:
            scan_time = self.td1_data['scan_time'] if 'scan_time' in self.td1_data else ""
            td        = self.td1_data['td']        if 'td'        in self.td1_data else ""

            lane_number = self.map.replace("T", "")
            lane_data = {
                "namespace": "lane_data",
                "key": self.name,
                "value": {
                    "color"         : self.color,
                    "material"      : self.material,
                    "bed_temp"      : self.bed_temp,
                    "nozzle_temp"   : self.extruder_temp,
                    "scan_time"     : scan_time,
                    "td"            : td,
                    "lane"          : lane_number
                }
            }
            self.afc.moonraker.send_lane_data(lane_data)

    def clear_lane_data(self):
        """
        Clears lane data that is currently stored at moonrakers `machine/set_lane_data` endpoint
        """
        if self.map is not None and "T" in self.map:
            lane_number = self.map.replace("T", "")
            lane_data = {
                "namespace": "lane_data",
                "key": self.name,
                "value": {
                    "color"         :  "",
                    "material"      : "",
                    "bed_temp"      : "",
                    "nozzle_temp"   : "",
                    "scan_time"     : "",
                    "td"            : "",
                    "lane"          : lane_number
                }
            }
            self.afc.moonraker.send_lane_data(lane_data)

    def get_td1_data(self):
        """
        Captures TD-1 data for lane. Has error checking to verify that lane is loaded, hub is not blocked
        and that TD-1 device is still detected before trying to capture data.
        """
        max_move_tries = 0
        status = True
        msg = ""
        if not self.load_state and not self.prep_state:
            msg = f"{self.name} not loaded, cannot capture TD-1 data for lane"
            self.afc.error.AFC_error(msg, pause=False)
            return False, msg

        if self.hub_obj.state:
            msg = f"Hub for {self.name} detects filament, cannot capture TD-1 data for lane"
            self.afc.error.AFC_error(msg, pause=False)
            return False, msg

        # Verify TD-1 is still connected before trying to get data
        if not self.afc.td1_present:
            msg = "TD-1 device not detected anymore, please check before continuing to capture TD-1 data"
            self.afc.error.AFC_error(msg, pause=False)
            return False, msg

        # If user has specified a specific ID, verify that its connected and found
        if self.td1_device_id:
            valid, msg = self.afc.function.check_for_td1_id(self.td1_device_id)
            if not valid:
                self.afc.error.AFC_error(msg, pause=False)
                return False, msg
        else:
            error, msg = self.afc.function.check_for_td1_error()
            if error:
                return False, msg

        if not self.hub_obj.state:
            if not self.loaded_to_hub:
                self.move_auto_speed(self.dist_hub)

            while not self.hub_obj.state:
                if max_move_tries >= self.afc.max_move_tries:
                    fail_message = f"Failed to trigger hub {self.hub_obj.name} for {self.name}\n"
                    fail_message += "Cannot capture TD-1 data, verify that hub switch is properly working before continuing"
                    self.afc.error.AFC_error(fail_message, pause=False)
                    self.do_enable(False)
                    return False, fail_message

                if max_move_tries == 0:
                    self.move_auto_speed(self.hub_obj.move_dis)
                else:
                    self.move_auto_speed(self.short_move_dis)
                max_move_tries += 1

            compare_time = datetime.now()
            self.move_auto_speed(self.hub_obj.td1_bowden_length)
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 5)

            success = self.unit_obj.get_td1_data(self, compare_time)
            if not success:
                msg = f"Not able to gather TD-1 data after moving {self.hub_obj.td1_bowden_length}mm"
                self.afc.error.AFC_error(msg, pause=False)
                status = False

            self.move_auto_speed(self.hub_obj.td1_bowden_length * -1)
            if success:
                self.send_lane_data()

            max_move_tries = 0
            while( self.hub_obj.state ):
                if max_move_tries >= self.afc.max_move_tries:
                    fail_message = f"Failed to un-trigger hub {self.hub_obj.name} for {self.name}\n"
                    fail_message += "Verify that hub switch is properly working before continuing"
                    self.afc.error.AFC_error(fail_message, pause=False)
                    self.do_enable(False)
                    return False, fail_message

                self.move_auto_speed(self.short_move_dis * -1)
                max_move_tries += 1

            self.move_auto_speed(self.hub_obj.hub_clear_move_dis * -1)
            self.do_enable(False)

        else:
            msg = "Cannot gather TD-1 data, hub sensor not clear. Please clear hub and try again."
            self.afc.error.AFC_error(msg, pause=False)
            status = False
        return status, msg

    cmd_SET_LANE_LOADED_help = "Sets current lane as loaded to toolhead, useful when manually loading lanes during prints if AFC detects an error when trying to unload/load a lane"
    cmd_SET_LANE_LOAD_options = {"LANE": {"type": "string", "default": "lane1"}}
    def cmd_SET_LANE_LOADED(self, gcmd):
        """
        This macro handles manually setting a lane loaded into the toolhead. This is useful when manually loading lanes
        during prints after AFC detects an error when loading/unloading and pauses.

        If there is a lane already loaded this macro will also desync that lane extruder from the toolhead extruder
        and set its values and led appropriately.

        Retrieves the lane specified by the 'LANE' parameter and sets the appropriate values in AFC to continue using the lane.

        Usage
        -----
        `SET_LANE_LOADED LANE=<lane>`

        Example
        -------
        ```
        SET_LANE_LOADED LANE=lane1
        ```
        """
        if not self.load_state:
            self.afc.error.AFC_error("Lane:{} is not loaded, ensure that the LOAD switch is properly configured and filament is detected. "
                                     "Switch status can be checked with the AFC_STATUS command.".format(self.name), pause=False)
            return

        # Do not set lane as loaded if virtual bypass or normal bypass is enabled/triggered
        if self.afc.bypass.sensor_enabled:
            disable_msg = ""
            msg = f"Cannot set {self.name} as loaded, "

            if 'virtual' in self.afc.bypass.name:
                msg += "virtual "
                disable_msg = " and disable"
            msg += f"bypass is enabled.\nPlease unload{disable_msg} before trying to set lanes as loaded."
            self.logger.error(msg)
            return

        self.afc.function.unset_lane_loaded()

        self.set_loaded()
        self.sync_to_extruder()
        self.afc.function.handle_activate_extruder()
        self.afc.save_vars()
        self.unit_obj.select_lane(self)
        self.logger.info("Manually set {} loaded to toolhead".format(self.name))

    cmd_SET_LONG_MOVE_SPEED_help = "Gives ability to set long_moves_speed or rev_long_moves_speed_factor values without having to update config and restart"
    def cmd_SET_LONG_MOVE_SPEED(self, gcmd):
        """
        Macro call to update long_moves_speed or rev_long_moves_speed_factor values without having to set in config and restart klipper. This macro allows adjusting
        these values while printing. Multiplier values must be between 0.5 - 1.2

        Use `FWD_SPEED` variable to set forward speed in mm/sec, use `RWD_FACTOR` to set reverse multiplier

        Usage
        -----
        `SET_LONG_MOVE_SPEED LANE=<lane_name> FWD_SPEED=<fwd_speed> RWD_FACTOR=<rwd_multiplier> SAVE=<0 or 1>`

        Example
        -----
        ```
        SET_LONG_MOVE_SPEED LANE=lane1 RWD_FACTOR=0.9 SAVE=1
        ```
        """
        update = gcmd.get_int("SAVE", 0, minval=0, maxval=2)
        old_long_moves_speed = self.long_moves_speed
        old_rev_long_moves_speed_factor= self.rev_long_moves_speed_factor

        self.long_moves_speed = gcmd.get_float("FWD_SPEED", self.long_moves_speed, minval=50, maxval=500)
        self.rev_long_moves_speed_factor = gcmd.get_float("RWD_FACTOR", self.rev_long_moves_speed_factor, minval=0.0, maxval=1.2)

        if self.rev_long_moves_speed_factor < 0.5: self.rev_long_moves_speed_factor = 0.5
        if self.rev_long_moves_speed_factor > 1.2: self.rev_long_moves_speed_factor = 1.2

        if self.long_moves_speed != old_long_moves_speed:
            self.logger.info("{name} forward speed set, New: {new}, Old: {old}".format(name=self.name, new=self.long_moves_speed, old=old_long_moves_speed))
        else:
            self.logger.info("{name} forward speed currently set to {new}".format(name=self.name, new=self.long_moves_speed))


        if self.rev_long_moves_speed_factor != old_rev_long_moves_speed_factor:
            self.logger.info("{name} reverse speed multiplier set, New: {new}, Old: {old}".format(name=self.name, new=self.rev_long_moves_speed_factor, old=old_rev_long_moves_speed_factor))
        else:
            self.logger.info("{name} reverse speed multiplier currently set to {new}".format(name=self.name, new=self.rev_long_moves_speed_factor))

        if update == 1:
            self.afc.function.ConfigRewrite(self.fullname, 'long_moves_speed',  self.long_moves_speed, '')
            self.afc.function.ConfigRewrite(self.fullname, 'rev_long_moves_speed_factor',  self.rev_long_moves_speed_factor, '')


    cmd_SET_SPEED_MULTIPLIER_help = "Gives ability to set fwd_speed_multiplier or rwd_speed_multiplier values without having to update config and restart"
    def cmd_SET_SPEED_MULTIPLIER(self, gcmd):
        """
        Macro call to update fwd_speed_multiplier or rwd_speed_multiplier values without having to set in config and restart klipper. This macro allows adjusting
        these values while printing.

        Use `FWD` variable to set forward multiplier, use `RWD` to set reverse multiplier

        After running this command run `SAVE_SPEED_MULTIPLIER LANE=<lane_name>` to save value to config file

        Usage
        -----
        `SET_SPEED_MULTIPLIER LANE=<lane_name> FWD=<fwd_multiplier> RWD=<rwd_multiplier>`

        Example
        -----
        ```
        SET_SPEED_MULTIPLIER LANE=lane1 RWD=0.9
        ```
        """
        updated = False
        old_fwd_value = self.fwd_speed_multi
        old_rwd_value = self.rwd_speed_multi

        self.fwd_speed_multi = gcmd.get_float("FWD", self.fwd_speed_multi, minval=0.0)
        self.rwd_speed_multi = gcmd.get_float("RWD", self.rwd_speed_multi, minval=0.0)

        if self.fwd_speed_multi != old_fwd_value:
            self.logger.info("{name} forward speed multiplier set, New: {new}, Old: {old}".format(name=self.name, new=self.fwd_speed_multi, old=old_fwd_value))
            updated = True

        if self.rwd_speed_multi != old_rwd_value:
            self.logger.info("{name} reverse speed multiplier set, New: {new}, Old: {old}".format(name=self.name, new=self.rwd_speed_multi, old=old_rwd_value))
            updated = True

        if updated:
            self.logger.info("Run SAVE_SPEED_MULTIPLIER LANE={} to save values to config file".format(self.name))

    cmd_SAVE_SPEED_MULTIPLIER_help = "Saves fwd_speed_multiplier and rwd_speed_multiplier values to config file "
    def cmd_SAVE_SPEED_MULTIPLIER(self, gcmd):
        """
        Macro call to write fwd_speed_multiplier and rwd_speed_multiplier variables to config file for specified lane.

        Usage
        -----
        `SAVE_SPEED_MULTIPLIER LANE=<lane_name>`

        Example
        -----
        ```
        SAVE_SPEED_MULTIPLIER LANE=lane1
        ```
        """
        self.afc.function.ConfigRewrite(self.fullname, 'fwd_speed_multiplier', self.fwd_speed_multi, '')
        self.afc.function.ConfigRewrite(self.fullname, 'rwd_speed_multiplier', self.rwd_speed_multi, '')

    cmd_SET_HUB_DIST_help = "Helper to dynamically set distance between a lanes extruder and hub"
    def cmd_SET_HUB_DIST(self, gcmd):
        """
        This function adjusts the distance between a lanes extruder and hub. Adding +/- in front of the length will
        increase/decrease length by that amount. To reset length back to config value, pass in `reset` for length to
        reset to value in config file.

        Usage
        -----
        `SET_HUB_DIST LANE=<lane_name> LENGTH=+/-<fwd_multiplier>`

        Example
        -----
        ```
        SET_HUB_DIST LANE=lane1 LENGTH=+100
        ```
        """
        old_dist_hub = self.dist_hub

        length = gcmd.get("LENGTH", self.dist_hub)

        if length != old_dist_hub:
            self.dist_hub = self.afc.function._calc_length(self.config_dist_hub, self.dist_hub, length)
        msg =  "//{} dist_hub:\n".format(self.name)
        msg += '//   Config Length:   {}\n'.format(self.config_dist_hub)
        msg += '//   Previous Length: {}\n'.format(old_dist_hub)
        msg += '//   New Length:      {}\n'.format(self.dist_hub)
        self.logger.raw(msg)
        self.logger.info("Run SAVE_HUB_DIST LANE={} to save value to config file".format(self.name))

    cmd_SAVE_HUB_DIST_help = "Saves dist_hub value to config file "
    def cmd_SAVE_HUB_DIST(self, gcmd):
        """
        Macro call to write dist_hub variable to config file for specified lane.

        Usage
        -----
        `SAVE_HUB_DIST LANE=<lane_name>`

        Example
        -----
        ```
        SAVE_HUB_DIST LANE=lane1
        ```
        """
        self.afc.function.ConfigRewrite(self.fullname, 'dist_hub', self.dist_hub, '')

    def get_status(self, eventtime=None, save_to_file=False):
        response = {}
        if not self.connect_done: return response
        response['name'] = self.name
        response['unit'] = self.unit
        response['hub'] = self.hub
        response['extruder'] = self.extruder_name
        response['buffer'] = self.buffer_name
        response['buffer_status'] = self.buffer_status()
        response['lane'] = self.index
        response['map'] = self.map
        response['load'] = bool(self.load_state)
        response["prep"] =bool(self.prep_state)
        response["tool_loaded"] = self.tool_loaded
        response["loaded_to_hub"] = self.loaded_to_hub
        response["material"]=self.material
        if save_to_file:
            response["density"]=self.filament_density
            response["diameter"]=self.filament_diameter
            response["empty_spool_weight"]=self.empty_spool_weight

        response["spool_id"]= int(self.spool_id) if self.spool_id else None
        response["color"]=self.color
        response["weight"]=self.weight
        response["extruder_temp"] = self.extruder_temp
        response["runout_lane"]=self.runout_lane
        filament_stat=self.afc.function.get_filament_status(self).split(':')
        response['filament_status'] = filament_stat[0]
        response['filament_status_led'] = filament_stat[1]
        response['status']          = self.status
        response['dist_hub']        = self.dist_hub

        if save_to_file:
            response['td1_data']        = self.td1_data
        else:
            response['td1_td']          = self.td1_data['td'] if "td" in self.td1_data else ''
            response['td1_color']       = self.td1_data['color'] if "color" in self.td1_data else ''
            response['td1_scan_time']   = self.td1_data['scan_time'] if "scan_time" in self.td1_data else ''
        return response



def load_config_prefix(config):
    return AFCLane(config)