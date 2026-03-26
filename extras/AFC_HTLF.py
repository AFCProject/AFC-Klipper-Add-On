# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback

from configparser import Error as config_error

from typing import TYPE_CHECKING

try: from extras.AFC_utils import ERROR_STR
except:
    trace=traceback.format_exc()
    err_str = f"Error when trying to import AFC_utils.ERROR_STR\n{trace}"
    raise config_error(err_str)

try: from extras.AFC_lane import AFCLaneState, MoveDirection, AFCLane
except: raise config_error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try: from extras.AFC_BoxTurtle import afcBoxTurtle
except: raise config_error(ERROR_STR.format(import_lib="AFC_BoxTurtle", trace=traceback.format_exc()))

try: from extras.AFC_utils import add_filament_switch
except: raise config_error(ERROR_STR.format(import_lib="AFC_utils", trace=traceback.format_exc()))

if TYPE_CHECKING:
    from configfile import ConfigWrapper

class AFC_HTLF(afcBoxTurtle):
    VALID_CAM_ANGLES = [30,45,60]
    def __init__(self, config: ConfigWrapper):
        super().__init__(config)
        self.type: str              = config.get('type', 'HTLF')
        self.drive_stepper: str     = config.get("drive_stepper")                                                   # Name of AFC_stepper for drive motor
        self.selector_stepper: str  = config.get("selector_stepper")                                                # Name of AFC_stepper for selector motor
        self.current_selected_lane  = None
        self.home_state             = False
        self.mm_move_per_rotation   = config.getint("mm_move_per_rotation", 32)                                     # How many mm moves pulley a full rotation
        self.cam_angle              = config.getint("cam_angle")                                                    # CAM lobe angle that is currently installed. 30,45,60 (recommend using 60)
        self.home_pin               = config.get("home_pin")                                                        # Pin for homing sensor
        self.MAX_ANGLE_MOVEMENT     = config.getint("MAX_ANGLE_MOVEMENT", 215)                                      # Max angle to move lobes, this is when lobe 1 is fully engaged with its lane
        self.enable_sensors_in_gui  = config.getboolean("enable_sensors_in_gui", self.afc.enable_sensors_in_gui)    # Set to True to show prep and load sensors switches as filament sensors in mainsail/fluidd gui, overrides value set in AFC.cfg
        self.selector_movement_speed: float = config.getfloat("selector_movement_speed", 50)
        self.selector_movement_accel: float = config.getfloat("selector_movement_accel", 50)
        self.prep_homed             = False
        self.failed_to_home         = False
        self._homed_distance        = 0.0


        if self.cam_angle not in self.VALID_CAM_ANGLES:
            raise config_error("{} is not a valid cam angle, please choose from the following {}".format(self.cam_angle, self.VALID_CAM_ANGLES))

        self.lobe_current_pos   = 0

        buttons = self.printer.load_object(config, "buttons")
        buttons.register_buttons([self.home_pin], self.home_callback)

        query_endstops              = self.printer.load_object( config, "query_endstops")
        ppins                       = self.printer.lookup_object('pins')
        self.home_endstop           = None
        self.home_endstop_name      = None
        if self.home_pin is not None:
            self.home_sensor = add_filament_switch(f"{self.name}_home_pin", self.home_pin, self.printer, self.enable_sensors_in_gui )

            ppins.allow_multi_use_pin(self.home_pin.strip("!^"))
            ppins.parse_pin(self.home_pin, True, True)
            self.home_endstop = ppins.setup_pin('endstop', self.home_pin)
            self.home_endstop_name = f"{self.name}_home"
            try:
                query_endstops.register_endstop(self.home_endstop,
                                                self.home_endstop_name)
            except Exception as e:
                err_msg = f"Error trying to register home endstop for {self.name}.\n Error:{e}"
                raise config_error(err_msg)

        self._lookup_objects(config)

        if self.home_endstop:
            # Adding home endstop to selector
            self.home_endstop.add_stepper(self.selector_stepper_obj.extruder_stepper.stepper)
            self.selector_stepper_obj._endstops[self.home_endstop_name] = (self.home_endstop, self.home_endstop_name)

        self.function.register_commands(self.afc.show_macros, "AFC_HOME_UNIT",
                                        self.cmd_AFC_HOME_UNIT,
                                        description=self.cmd_AFC_HOME_UNIT_help,
                                        options=self.cmd_AFC_HOME_UNIT_options)

    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """

        super().handle_connect()

        self.logo = '<span class=success--text>HTLF Ready\n</span>'
        self.logo_error = '<span class=error--text>HTLF Not Ready</span>\n'

    def system_Test(self, cur_lane, delay, assignTcmd, enable_movement):
        cur_lane.prep_state = cur_lane.load_state
        if not self.prep_homed:
            self.return_to_home( prep = True, disable_selector=False)
        status = super().system_Test( cur_lane, delay, assignTcmd, enable_movement)
        self.return_to_home()

        return self.prep_homed and status

    def home_callback(self, eventtime, state):
        """
        Callback when home switch is triggered/untriggered
        """
        self.home_state = state

    cmd_AFC_HOME_UNIT_help = "Command to move lane selector back to home position for specified in "\
                             "selector style units that utilizes a home sensor."
    cmd_AFC_HOME_UNIT_options = {"UNIT": {"type":"string", "default":"HTLF_1"}}
    def cmd_AFC_HOME_UNIT(self, gcmd):
        """
        Moves units lane selector back to home position

        Usage
        -----
        `AFC_HOME_UNIT UNIT=<unit_name>`

        Example:
        -----
        ```
        AFC_HOME_UNIT UNIT=HTLF_1
        ```
        """
        self.return_to_home()

    def _move_selector_home(self, distance: float):
        """
        Helper function to move stepper with correct function call depending on if homing is enabled
        or not

        :param distance: Distance to move selector stepper
        """
        if self.afc.homing_enabled:
            homed, self._homed_distance = self.selector_stepper_obj.do_homing_move(
                distance *MoveDirection.NEG,
                self.selector_movement_speed,
                self.selector_movement_accel,
                self.home_endstop_name,
                assist_active=False
            )
            self.logger.debug(f"HTLF: Homing done, success:{homed}, distance:{self._homed_distance}")
        else:
            self.selector_stepper_obj.move(distance * MoveDirection.NEG,
                                           self.selector_movement_speed,
                                           self.selector_movement_accel,
                                           False)

    def return_to_home(self, prep=False, disable_selector=True):
        """
        Moves lobes to home position, if a current lane was selected this function moves back that amount and then performs smaller
        moves until home switch is triggered

        :param prep: Set to True if this function is being called within prep function, once set the fast move back if another lane
                      was selected is bypassed and only move in smaller increments
        :return boolean: Returns True if homing was successful
        """
        total_moved = 0

        move_distance = 200
        if self.current_selected_lane is not None and not self.home_state and not prep:
            if not self.afc.homing_enabled:
                move_distance = self.calculate_lobe_movement(self.current_selected_lane.index)

            self._move_selector_home(move_distance)

        while not self.home_state and not self.failed_to_home:
            if not self.afc.homing_enabled:
                move_distance = 1
            self._move_selector_home(move_distance)
            if self.afc.homing_enabled:
                total_moved += self._homed_distance
            else:
                total_moved += move_distance
            if total_moved > (self.mm_move_per_rotation/360)*(self.MAX_ANGLE_MOVEMENT+self.cam_angle):
                self.failed_to_home = True
                self.afc.error.AFC_error("Failed to home {}".format(self.name), False)
                return False

        self.prep_homed = True
        # Adding delay or disabling stepper motor will crash klipper with newest
        # motion queuing changes
        # self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.1)
        if disable_selector:
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

    def select_lane( self, lane, disable_selector: bool=False ) -> tuple[bool, float|int]:
        """
        Moves lobe selector to specified lane based off lanes index

        :param lane: Lane object to move selector to
        :param disable_selector: When True disables selectors motor after selecting a lane
        :return boolean: Returns True if movement of selector succeeded
        """
        if "stepper" in lane.fullname.lower():
            return False, 0.0
        try:
            if self.current_selected_lane != lane:
                self.logger.debug("HTLF: {} Homing to endstop.".format(self.name))
                if self.return_to_home( disable_selector=False ):
                    self.selector_stepper_obj.move(self.calculate_lobe_movement( lane.index ),
                                                   self.selector_movement_speed,
                                                   self.selector_movement_accel,
                                                   False)
                    self.logger.debug("HTLF: Selecting {}".format(lane))
                    self.current_selected_lane = lane
                    return True, self._homed_distance
                else:
                    self.logger.error(f"HTLF: failed to home when selecting {lane.name}")
                    return False, 0.0
        finally:
            if disable_selector:
                self.selector_stepper_obj.do_enable(False)

        return True, 0.0

    def check_runout(self, cur_lane):
        """
        Function to check if runout logic should be triggered

        :return boolean: Returns true if current lane is loaded and printer is printing but lanes status is not ejecting or calibrating
        """
        return (cur_lane.name == self.afc.function.get_current_lane()
                and self.afc.function.is_printing()
                and cur_lane.status != AFCLaneState.EJECTING
                and cur_lane.status != AFCLaneState.CALIBRATING)

    def prep_load(self, lane: AFCLane):
        # HTLF does not have prep switches returning
        return

    def prep_post_load(self, lane: AFCLane):
        # HTLF does not have prep switches returning
        return

def load_config_prefix(config):
    return AFC_HTLF(config)