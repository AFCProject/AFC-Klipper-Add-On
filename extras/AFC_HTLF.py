# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import traceback

from configparser import Error as error

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCLaneState
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try: from extras.AFC_BoxTurtle import afcBoxTurtle
except: raise error(ERROR_STR.format(import_lib="AFC_BoxTurtle", trace=traceback.format_exc()))

try: from extras.AFC_utils import add_filament_switch
except: raise error(ERROR_STR.format(import_lib="AFC_utils", trace=traceback.format_exc()))

class AFC_HTLF(afcBoxTurtle):
    VALID_CAM_ANGLES = [30,45,60]
    def __init__(self, config):
        super().__init__(config)
        self.type                   = config.get('type', 'HTLF')
        self.drive_stepper          = config.get("drive_stepper")                                                   # Name of AFC_stepper for drive motor
        self.selector_stepper       = config.get("selector_stepper")                                                # Name of AFC_stepper for selector motor
        self.drive_stepper_obj      = None
        self.selector_stepper_obj   = None
        self.current_selected_lane  = None
        self.home_state             = False
        self.mm_move_per_rotation   = config.getint("mm_move_per_rotation", 32)                                     # How many mm moves pulley a full rotation
        self.cam_angle              = config.getint("cam_angle")                                                    # CAM lobe angle that is currently installed. 30,45,60 (recommend using 60)
        self.home_pin               = config.get("home_pin")                                                        # Pin for homing sensor
        self.MAX_ANGLE_MOVEMENT     = config.getint("MAX_ANGLE_MOVEMENT", 215)                                      # Max angle to move lobes, this is when lobe 1 is fully engaged with its lane
        self.enable_sensors_in_gui  = config.getboolean("enable_sensors_in_gui", self.afc.enable_sensors_in_gui)    # Set to True to show prep and load sensors switches as filament sensors in mainsail/fluidd gui, overrides value set in AFC.cfg
        self.prep_homed             = False
        self.failed_to_home         = False


        if self.cam_angle not in self.VALID_CAM_ANGLES:
            raise error("{} is not a valid cam angle, please choose from the following {}".format(self.cam_angle, self.VALID_CAM_ANGLES))

        self.lobe_current_pos   = 0

        buttons = self.printer.load_object(config, "buttons")
        buttons.register_buttons([self.home_pin], self.home_callback)

        if self.home_pin is not None:
            self.home_sensor = add_filament_switch(f"{self.name}_home_pin", self.home_pin, self.printer, self.enable_sensors_in_gui )

    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """

        try:
            self.drive_stepper_obj = self.printer.lookup_object('AFC_stepper {}'.format(self.drive_stepper))
        except:
            error_string = 'Error: No config found for drive_stepper: {drive_stepper} in [AFC_HTLF {stepper}]. Please make sure [AFC_stepper {drive_stepper}] section exists in your config'.format(
                drive_stepper=self.drive_stepper, stepper=self.name )
            raise error(error_string)

        try:
            self.selector_stepper_obj = self.printer.lookup_object('AFC_stepper {}'.format(self.selector_stepper))
        except:
            error_string = 'Error: No config found for selector_stepper: {selector_stepper} in [AFC_HTLF {stepper}]. Please make sure [AFC_stepper {selector_stepper}] section exists in your config'.format(
                selector_stepper=self.selector_stepper, stepper=self.name )
            raise error(error_string)

        self.gcode.register_mux_command('HOME_UNIT',     "UNIT", self.name, self.cmd_HOME_UNIT)

        super().handle_connect()

        self.logo = '<span class=success--text>HTLF Ready\n</span>'
        self.logo_error = '<span class=error--text>HTLF Not Ready</span>\n'

    def system_Test(self, cur_lane, delay, assignTcmd, enable_movement):
        cur_lane.prep_state = cur_lane.load_state
        if not self.prep_homed:
            self.return_to_home( prep = True)
        status = super().system_Test( cur_lane, delay, assignTcmd, enable_movement)
        self.return_to_home()

        return self.prep_homed and status

    def home_callback(self, eventtime, state):
        """
        Callback when home switch is triggered/untriggered
        """
        self.home_state = state

    def cmd_HOME_UNIT(self, gcmd):
        """
        Moves unit lane selection back to home position

        Usage
        -----
        `HOME_UNIT UNIT=<unit_name>`

        Example:
        -----
        ```
        HOME_UNIT UNIT=HTLF_1
        ```
        """
        self.return_to_home()

    def return_to_home(self, prep=False):
        """
        Moves lobes to home position, if a current lane was selected this function moves back that amount and then performs smaller
        moves until home switch is triggered

        :param prep: Set to True if this function is being called within prep function, once set the fast move back if another lane
                      was selected is bypassed and only move in smaller increments
        :return boolean: Returns True if homing was successful
        """
        total_moved = 0

        if self.current_selected_lane is not None and not self.home_state and not prep:
            self.selector_stepper_obj.move( self.calculate_lobe_movement(self.current_selected_lane.index) * -1, 20, 20, False)

        while not self.home_state and not self.failed_to_home:
            self.selector_stepper_obj.move(-1, 20, 20, False)
            total_moved += 1
            if total_moved > (self.mm_move_per_rotation/360)*(self.MAX_ANGLE_MOVEMENT+self.cam_angle):
                self.failed_to_home = True
                self.afc.error.AFC_error("Failed to home {}".format(self.name), False)
                return False

        self.prep_homed = True
        self.selector_stepper_obj.do_enable(False)
        self.current_selected_lane = None
        return True

    def calculate_lobe_movement(self, lane_index:int ):
        """
        Calculates movement in mm to activate lane based off passed in lane index

        :param lane_index: Lane index to calculate movement for
        :return float: Return movement in mm to move lobes
        """
        angle_movement = self.MAX_ANGLE_MOVEMENT - ( (lane_index-1) * self.cam_angle)
        self.logger.debug("HTLF: Lobe Movement angle : {}".format(angle_movement))
        return (self.mm_move_per_rotation/360)*angle_movement

    def select_lane( self, lane ):
        """
        Moves lobe selector to specified lane based off lanes index

        :param lane: Lane object to move selector to
        :return boolean: Returns True if movement of selector succeeded
        """
        self.failed_to_home = False
        if self.current_selected_lane != lane:
            self.logger.debug("HTLF: {} Homing to endstop.".format(self.name))
            if self.return_to_home():
                self.selector_stepper_obj.move(self.calculate_lobe_movement( lane.index ), 50, 50, False)
                self.logger.debug("HTLF: {} selected".format(lane))
                self.current_selected_lane = lane
            else:
                return False

    def check_runout(self, cur_lane):
        """
        Function to check if runout logic should be triggered

        :return boolean: Returns true if current lane is loaded and printer is printing but lanes status is not ejecting or calibrating
        """
        return cur_lane.name == self.afc.function.get_current_lane() and self.afc.function.is_printing() and cur_lane.status != AFCLaneState.EJECTING and cur_lane.status != AFCLaneState.CALIBRATING

def load_config_prefix(config):
    return AFC_HTLF(config)