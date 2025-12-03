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

class afc_hub:
    def __init__(self, config):
        self.printer    = config.get_printer()
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.afc        = self.printer.lookup_object('AFC')
        self.reactor    = self.printer.get_reactor()

        self.fullname   = config.get_name()
        self.name       = self.fullname.split()[-1]

        self.unit = None
        self.lanes = {}
        self.state = False

        # HUB Cut variables
        # Next two variables are used in AFC
        self.switch_pin             = config.get('switch_pin')                      # Pin hub sensor it connected to
        self.hub_clear_move_dis     = config.getfloat("hub_clear_move_dis", 25)     # How far to move filament so that it's not block the hub exit
        self.afc_bowden_length      = config.getfloat("afc_bowden_length", 900)     # Length of the Bowden tube from the hub to the toolhead sensor in mm.
        self.td1_bowden_length      = config.getfloat("td1_bowden_length", self.afc_bowden_length-50)     # Length of the Bowden tube from the hub to a TD-1 device in mm.
        self.afc_unload_bowden_length= config.getfloat("afc_unload_bowden_length", self.afc_bowden_length) # Length to unload when retracting back from toolhead to hub in mm. Defaults to afc_bowden_length
        self.assisted_retract       = config.getboolean("assisted_retract", False)  # if True, retracts are assisted to prevent loose windings on the spool
        self.move_dis               = config.getfloat("move_dis", 50)               # Distance to move the filament within the hub in mm.
        # Servo settings
        self.cut                    = config.getboolean("cut", False)               # Set True if Hub cutter installed (e.g. Snappy)
        self.cut_cmd                = config.get('cut_cmd', None)                   # Macro to use for cut.
        self.cut_servo_name         = config.get('cut_servo_name', 'cut')           # Name of servo to use for cutting
        self.cut_dist               = config.getfloat("cut_dist", 50)               # How much filament to cut off (in mm).
        self.cut_clear              = config.getfloat("cut_clear", 120)             # How far the filament should retract back from the hub (in mm).
        self.cut_min_length         = config.getfloat("cut_min_length", 200)        # Minimum length of filament to cut off
        self.cut_servo_pass_angle   = config.getfloat("cut_servo_pass_angle", 0)    # Servo angle to align the Bowden tube with the hole for loading the toolhead.
        self.cut_servo_clip_angle   = config.getfloat("cut_servo_clip_angle", 160)  # Servo angle for cutting the filament.
        self.cut_servo_prep_angle   = config.getfloat("cut_servo_prep_angle", 75)   # Servo angle to prepare the filament for cutting (aligning the exit hole).
        self.cut_confirm            = config.getboolean("cut_confirm", 0)           # Set True to cut filament twice

        self.config_bowden_length   = self.afc_bowden_length                        # Used by SET_BOWDEN_LENGTH macro
        self.config_unload_bowden_length = self.afc_unload_bowden_length
        self.enable_sensors_in_gui  = config.getboolean("enable_sensors_in_gui",    self.afc.enable_sensors_in_gui) # Set to True to show hub sensor switches as filament sensor in mainsail/fluidd gui, overrides value set in AFC.cfg
        self.debounce_delay         = config.getfloat("debounce_delay",             self.afc.debounce_delay)
        self.enable_runout          = config.getboolean("enable_hub_runout",        self.afc.enable_hub_runout)

        buttons = self.printer.load_object(config, "buttons")
        if self.switch_pin is not None:
            self.state = False
            buttons.register_buttons([self.switch_pin], self.switch_pin_callback)

        self.fila, self.debounce_button = add_filament_switch( f"{self.name}_Hub", self.switch_pin, self.printer,
                                                                self.enable_sensors_in_gui, self.handle_runout, self.enable_runout,
                                                                self.debounce_delay)

        # Adding self to AFC hubs
        self.afc.hubs[self.name]=self

    def __str__(self):
        return self.name

    def handle_runout(self, eventtime):
        """
        Callback function for hub runout, this is different than `switch_pin_callback` function as this function
        can be delayed and is called from filament_switch_sensor class when it detects a runout event.

        Before exiting `min_event_systime` is updated as this mimics how its done in `_exec_gcode` function in RunoutHelper class
        as AFC overrides `_runout_event_handler` function with this function callback. If `min_event_systime` does not get
        updated then future switch changes will not be detected.

        :param eventtime: Event time from the button press
        """
        # Only trigger runout for the currently loaded lane (in the toolhead) if it belongs to this hub
        current_lane_name = getattr(self.afc, 'current', None)
        if current_lane_name and current_lane_name in self.lanes:
            lane = self.lanes[current_lane_name]
            lane.handle_hub_runout(sensor=self.name)
        self.fila.runout_helper.min_event_systime = self.reactor.monotonic() + self.fila.runout_helper.event_delay

    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """
        self.gcode = self.afc.gcode
        self.reactor = self.afc.reactor

        self.printer.send_event("afc_hub:register_macros", self)

    def switch_pin_callback(self, eventtime, state):
        self.state = state

    def hub_cut(self, cur_lane):
        servo_string = 'SET_SERVO SERVO={servo} ANGLE={{angle}}'.format(servo=self.cut_servo_name)

        # Prep the servo for cutting.
        self.gcode.run_script_from_command(servo_string.format(angle=self.cut_servo_prep_angle))
        # Load the lane until the hub is triggered.
        while not self.state:
            cur_lane.move(self.move_dis, cur_lane.short_moves_speed, cur_lane.short_moves_accel)

        # To have an accurate reference position for `hub_cut_dist`, move back and forth in smaller steps
        # to find the point where the hub just triggers.
        while self.state:
            cur_lane.move(-10, cur_lane.short_moves_speed, cur_lane.short_moves_accel, self.assisted_retract)
        while not self.state:
            cur_lane.move(2, cur_lane.short_moves_speed, cur_lane.short_moves_accel)

        # Feed the `hub_cut_dist` amount.
        cur_lane.move(self.cut_dist, cur_lane.short_moves_speed, cur_lane.short_moves_accel)
        # Have a snooze
        self.reactor.pause(self.reactor.monotonic() + 0.5)


        # Choppy Chop
        self.gcode.run_script_from_command(servo_string.format(angle=self.cut_servo_clip_angle))
        if self.cut_confirm:
            self.reactor.pause(self.reactor.monotonic() + 0.5)
            # ReChop, To be doubly choppy sure.
            self.gcode.run_script_from_command(servo_string.format(angle=self.cut_servo_prep_angle))

            self.reactor.pause(self.reactor.monotonic() + 1)
            self.gcode.run_script_from_command(servo_string.format(angle=self.cut_servo_clip_angle))
        # Longer Snooze
        self.reactor.pause(self.reactor.monotonic() + 1)
        # Align bowden tube (reset)
        self.gcode.run_script_from_command(servo_string.format(angle=self.cut_servo_pass_angle))

        # Retract lane by `hub_cut_clear`.
        cur_lane.move(-self.cut_clear, cur_lane.short_moves_speed, cur_lane.short_moves_accel, self.assisted_retract)

    def get_status(self, eventtime=None):
        self.response = {}
        self.response['state'] = bool(self.state)
        self.response['cut'] = self.cut
        self.response['cut_cmd'] = self.cut_cmd
        self.response['cut_dist'] = self.cut_dist
        self.response['cut_clear'] = self.cut_clear
        self.response['cut_min_length'] = self.cut_min_length
        self.response['cut_servo_pass_angle'] = self.cut_servo_pass_angle
        self.response['cut_servo_clip_angle'] = self.cut_servo_clip_angle
        self.response['cut_servo_prep_angle'] = self.cut_servo_prep_angle
        self.response['lanes'] = [lane.name for lane in self.lanes.values()]
        self.response['afc_bowden_length'] = self.afc_bowden_length

        return self.response

def load_config_prefix(config):
    return afc_hub(config)