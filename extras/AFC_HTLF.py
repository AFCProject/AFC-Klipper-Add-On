# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback

from configparser import Error as config_error

from typing import TYPE_CHECKING, Optional

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
    from gcode import GCodeCommand
    from mcu import MCU_endstop
    from extras.AFC_stepper import AFCExtruderStepper

class AFC_HTLF(afcBoxTurtle):
    VALID_CAM_ANGLES = [30,45,60]

    # Redeclaring these here so mypy does not complain about these being none since for HTLF
    # drive stepper and selector steppers are not optional
    drive_stepper_obj: AFCExtruderStepper
    selector_stepper_obj: AFCExtruderStepper

    def __init__(self, config: ConfigWrapper) -> None:
        """
        Parse HTLF configuration, register the home-sensor endstop and
        filament switch, and register the AFC_HOME_UNIT gcode command.
        home_pin is a required config option.

        :param config: Klipper config wrapper for the AFC_HTLF section
        """
        super().__init__(config)
        self.type: str              = config.get('type', 'HTLF')
        self.drive_stepper: str     = config.get("drive_stepper")                                                   # Name of AFC_stepper for drive motor
        self.selector_stepper: str  = config.get("selector_stepper")                                                # Name of AFC_stepper for selector motor
        self.current_selected_lane: Optional[AFCLane] = None
        self.home_state: bool       = False
        self.mm_move_per_rotation: int = config.getint("mm_move_per_rotation", 32)                                     # How many mm moves pulley a full rotation
        self.cam_angle: int         = config.getint("cam_angle")                                                    # CAM lobe angle that is currently installed. 30,45,60 (recommend using 60)
        self.home_pin: str          = config.get("home_pin")                                                       # Pin for homing sensor
        self.MAX_ANGLE_MOVEMENT:int = config.getint("MAX_ANGLE_MOVEMENT", 215)                                      # Max angle to move lobes, this is when lobe 1 is fully engaged with its lane
        self.enable_sensors_in_gui: bool  = config.getboolean("enable_sensors_in_gui", self.afc.enable_sensors_in_gui)    # Set to True to show prep and load sensors switches as filament sensors in mainsail/fluidd gui, overrides value set in AFC.cfg
        self.selector_movement_speed: float = config.getfloat("selector_movement_speed", 50)
        self.selector_movement_accel: float = config.getfloat("selector_movement_accel", 50)
        self.prep_homed: bool       = False
        self.failed_to_home: bool   = False
        self._homed_distance: float = 0.0


        if self.cam_angle not in self.VALID_CAM_ANGLES:
            raise config_error("{} is not a valid cam angle, please choose from the following {}".format(self.cam_angle, self.VALID_CAM_ANGLES))

        self.lobe_current_pos   = 0

        buttons = self.printer.load_object(config, "buttons")
        buttons.register_buttons([self.home_pin], self.home_callback)

        query_endstops              = self.printer.load_object( config, "query_endstops")
        ppins                       = self.printer.lookup_object('pins')
        self.home_endstop: Optional[MCU_endstop] = None
        self.home_endstop_name: str

        self.home_sensor, _ = add_filament_switch(f"{self.name}_home_pin", self.home_pin,
                                                    self.printer, self.enable_sensors_in_gui )

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

    def handle_connect(self) -> None:
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """

        super().handle_connect()

        self.logo = '<span class=success--text>HTLF Ready\n</span>'
        self.logo_error = '<span class=error--text>HTLF Not Ready</span>\n'

    def system_Test(self, cur_lane: AFCLane|AFCExtruderStepper, delay: float,
                    assignTcmd: bool, enable_movement: bool) -> bool:
        """
        Runs the standard system test, homing the selector first (if not
        already prep-homed) and again after the test so the selector always
        ends up back at home.

        :param cur_lane: Lane to run the system test against
        :param delay: Delay amount to wait between forward/backward movements
        :param assignTcmd: When True assigns a tool number to cur_lane
        :param enable_movement: When True movement is enabled during the test
        :return: True if the selector prep-homed successfully and the test succeeded
        """
        cur_lane.prep_state = cur_lane.load_state
        if not self.prep_homed:
            self.return_to_home( prep = True, disable_selector=False)
        status = super().system_Test( cur_lane, delay, assignTcmd, enable_movement)
        self.return_to_home()

        return self.prep_homed and status

    def home_callback(self, eventtime: float, state: int) -> None:
        """
        Callback when home switch is triggered/untriggered

        :param eventtime: Event time from the button press
        :param state: True/1 if the home switch is triggered, False/0 otherwise
        """
        self.home_state = bool(state)

    cmd_AFC_HOME_UNIT_help = "Command to move lane selector back to home position for specified in "\
                             "selector style units that utilizes a home sensor."
    cmd_AFC_HOME_UNIT_options = {"UNIT": {"type":"string", "default":"HTLF_1"}}
    def cmd_AFC_HOME_UNIT(self, gcmd: GCodeCommand) -> None:
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

    def _move_selector_home(self, distance: float) -> None:
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

    def return_to_home(self, prep: bool=False, disable_selector: bool=True) -> bool:
        """
        Moves lobes to home position, if a current lane was selected this function moves back that amount and then performs smaller
        moves until home switch is triggered

        :param prep: Set to True if this function is being called within prep function, once set the fast move back if another lane
                      was selected is bypassed and only move in smaller increments
        :param disable_selector: When True disables the selector stepper motor once homing succeeds
        :return boolean: Returns True if homing was successful
        """
        total_moved: float = 0

        move_distance: float = 200
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

    def calculate_lobe_movement(self, lane_index:int ) -> float:
        """
        Calculates movement in mm to activate lane based off passed in lane index

        :param lane_index: Lane index to calculate movement for
        :return float: Return movement in mm to move lobes
        """
        angle_movement = self.MAX_ANGLE_MOVEMENT - ( (lane_index-1) * self.cam_angle)
        self.logger.debug("HTLF: Lobe Movement angle : {}".format(angle_movement))
        return (self.mm_move_per_rotation/360)*angle_movement

    def select_lane( self, lane: AFCLane, disable_selector: bool=False ) -> tuple[bool, float|int]:
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
                    # Applying selector_cal_dis move if specified in users config
                    self._selector_cal_dis_adjust(lane)

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

    def check_runout(self, cur_lane: AFCLane) -> bool:
        """
        Function to check if runout logic should be triggered

        :param cur_lane: Lane to check runout state for
        :return boolean: Returns true if current lane is loaded and printer is printing but lanes status is not ejecting or calibrating
        """
        return (cur_lane.name == self.afc.function.get_current_lane()
                and self.afc.function.is_printing()
                and cur_lane.status != AFCLaneState.EJECTING
                and cur_lane.status != AFCLaneState.CALIBRATING)

    def prep_load(self, lane: AFCLane) -> None:
        """
        HTLF does not have prep switches, so there is nothing to do here.

        :param lane: Lane prep loading would otherwise apply to
        """
        # HTLF does not have prep switches returning
        return

    def prep_post_load(self, lane: AFCLane) -> None:
        """
        HTLF does not have prep switches, so there is nothing to do here.

        :param lane: Lane prep post-load would otherwise apply to
        """
        # HTLF does not have prep switches returning
        return

def load_config_prefix(config: ConfigWrapper) -> AFC_HTLF:
    """
    Klipper config entry point for AFC_HTLF <name> sections.

    :param config: Klipper config wrapper for the AFC_HTLF <name> section
    :return type: AFC_HTLF instance to register with the printer
    """
    return AFC_HTLF(config)