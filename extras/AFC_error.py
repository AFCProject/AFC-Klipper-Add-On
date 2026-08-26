# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback
import logging
import inspect

from configparser import Error as error

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from gcode import GCodeCommand
    from extras.AFC import afc
    from extras.AFC_lane import AFCLane

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC import State
except: raise error(ERROR_STR.format(import_lib="AFC", trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCLaneState, MoveDirection, SpeedMode
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

class afcError:
    def __init__(self, config: ConfigWrapper) -> None:
        """
        Register for the klippy:connect event; real setup happens in handle_connect
        since AFC and other printer objects aren't available yet at this point.

        :param config: Klipper config wrapper for the AFC_error section
        """
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.errorLog: dict[str, Any] = {}
        self.pause= False

    def handle_connect(self) -> None:
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """
        self.afc: afc       = self.printer.lookup_object('AFC')
        self.pause_resume   = self.printer.lookup_object("pause_resume")
        self.logger         = self.afc.logger
        self.error_timeout  = self.afc.error_timeout
        self.idle_timeout_obj = self.printer.lookup_object("idle_timeout")
        self.idle_timeout_val = self.idle_timeout_obj.idle_timeout

        # Constant variable for renaming RESUME macro
        self.BASE_RESUME_NAME       = 'RESUME'
        self.AFC_RENAME_RESUME_NAME = f'_AFC_RENAMED_{self.BASE_RESUME_NAME}_'
        self.BASE_PAUSE_NAME        = 'PAUSE'
        self.AFC_RENAME_PAUSE_NAME  = f'_AFC_RENAMED_{self.BASE_PAUSE_NAME}_'

        self.afc.gcode.register_command('RESET_FAILURE', self.cmd_RESET_FAILURE, desc=self.cmd_RESET_FAILURE_help)
        self.afc.gcode.register_command('AFC_RESUME', self.cmd_AFC_RESUME, desc=self.cmd_AFC_RESUME_help)

    def fix(self, problem: Optional[str], lane: Optional[str|AFCLane]) -> bool:
        """
        Attempt to resolve a reported lane/toolhead problem automatically,
        falling back to pausing for user intervention when it can't be handled.

        :param problem: identifier for the problem to resolve, or None for an unknown error
        :param lane: lane object, or the name of a lane to look up, that the problem applies to
        :return type: True if the problem was resolved automatically, False otherwise
        """
        self.pause= True
        error_handled = False
        lane_name = lane if isinstance(lane, str) else None
        if isinstance(lane, str):
            lane = self.afc.lanes.get(lane, None)
            if lane is None:
                self.PauseUserIntervention(
                    f"Unknown lane '{lane_name}' reported for problem: {problem}"
                )
                return error_handled

        if problem is None:
            self.PauseUserIntervention('Paused for unknown error')
        elif(problem=='toolhead'
            and lane is not None):
            error_handled = self.ToolHeadFix(lane)
        else:
            self.PauseUserIntervention(problem)
        if (not error_handled
            and lane is not None):
            lane.unit_obj.lane_fault(lane)

        return error_handled

    def ToolHeadFix(self, cur_lane: AFCLane) -> bool:
        """
        Attempt to automatically resolve a toolhead-related lane fault by
        retracting or reloading filament to the lane's load sensor.

        :param cur_lane: lane object to attempt to fix
        :return type: True if the lane was successfully reset, False otherwise
        """
        if cur_lane.get_toolhead_pre_sensor_state():   #toolhead has filament
            if cur_lane.extruder_obj.lane_loaded == cur_lane.name:   #var has right lane loaded
                if not cur_lane.raw_load_state: #Lane has filament
                    self.PauseUserIntervention('Filament not loaded in Lane')
                else:
                    self.PauseUserIntervention('no error detected')
            else:
                self.PauseUserIntervention('laneloaded does not match extruder')

        else: #toolhead empty
            failed_to_retract_msg = f"Failed to retract {cur_lane.name} to load sensor"
            if (cur_lane.raw_load_state
                and not cur_lane.is_direct_hub()
                and cur_lane.extruder_obj.tool_start != "buffer"):
                self.logger.info(f"Retracting {cur_lane.name} back to load switch")
                if self.afc.homing_enabled:
                    num_tries = 0
                    while (cur_lane.raw_load_state):
                        total_move_dist = cur_lane.dist_hub + 500
                        if cur_lane.hub_obj is not None:
                            total_move_dist += cur_lane.hub_obj.afc_bowden_length
                        cur_lane.unit_obj.move_to_load(cur_lane, total_move_dist,
                                                       MoveDirection.NEG, True,
                                                       SpeedMode.SHORT)
                        num_tries += 1
                        if num_tries >= 5:
                            self.PauseUserIntervention(failed_to_retract_msg)
                            return False
                else:
                    max_length = 5000
                    while cur_lane.raw_load_state:  # slowly back filament up to lane extruder
                        cur_lane.move(-5, self.afc.short_moves_speed, self.afc.short_moves_accel, True)
                        if max_length > 0:
                            max_length -= 5
                        else:
                            self.PauseUserIntervention(failed_to_retract_msg)
                            return False
                    max_length = 1000
                    while not cur_lane.raw_load_state:  # reload lane extruder
                        cur_lane.move(5, self.afc.short_moves_speed, self.afc.short_moves_accel, True)
                        if max_length > 0:
                            max_length -= 5
                        else:
                            self.PauseUserIntervention(
                                f"Failed to move back {cur_lane.name} to load sensor"
                            )
                            return False

                cur_lane.set_tool_unloaded()
                cur_lane.loaded_to_hub = False
                cur_lane.unit_obj.prep_load(cur_lane)
                cur_lane.unit_obj.prep_post_load(cur_lane)
                self.afc.save_vars()
                self.pause = False
                self.logger.info(f"Done resetting {cur_lane.name}")
                return True
        return False

    def PauseUserIntervention(self, message: Optional[str]) -> None:
        """
        Log an error message and pause the print for user intervention if the
        printer is homed, not already paused, and a pause has been requested.

        :param message: error message to log
        """
        self.logger.error(message)
        if (self.afc.function.is_homed(for_move=True)
            and not self.afc.function.is_paused()):
            self.afc.save_pos()
            if self.pause:
                self.pause_print()

    def pause_print(self) -> None:
        """
        pause_print function verifies that the printer is homed and not currently paused before calling
        the base pause command
        """
        self.set_error_state( True )
        self.logger.info ('PAUSING')
        self.afc.gcode.run_script_from_command('PAUSE')
        self.logger.debug("After User Pause")
        self.afc.function.log_toolhead_pos()

    def set_error_state(self, state: bool=False) -> None:
        """
        Set the AFC error state, saving the toolhead position on the first
        transition into an error state.

        :param state: True to enter the error state, False to clear it
        """
        logging.warning(f"AFC debug: setting error state {state}")
        # Only save position on first error state call
        if state and not self.afc.error_state:
            self.afc.save_pos()
        self.afc.error_state = state
        self.afc.current_state = State.ERROR if state else State.IDLE

    def AFC_error(self, msg: str, pause: bool=True, stack_name: Optional[str]=None) -> None:
        """
        Log an AFC error and optionally pause the print.

        :param msg: error message to log
        :param pause: True to pause the print after logging
        :param stack_name: name to attribute the error to, defaults to the caller's function name
        """
        # Print to logger since respond_raw does not write to logger
        logging.warning(msg)
        if stack_name is None:
            frame=inspect.currentframe()
            caller_frame = frame.f_back if frame else None
            stack_name = caller_frame.f_code.co_name if caller_frame else ""
        # Handle AFC errors
        self.logger.error(message=msg, stack_name=stack_name)
        if pause: self.pause_print()

    cmd_RESET_FAILURE_help = "CLEAR STATUS ERROR"
    def cmd_RESET_FAILURE(self, gcmd: GCodeCommand) -> None:
        """
        This function clears the error state of the AFC system by setting the error state to False.

        Usage
        -----
        `RESET_FAILURE`

        Example
        -----
        `RESET_FAILURE`
        """
        self.reset_failure()

    def reset_failure(self) -> None:
        """
        Common function to reset error_state, pause, and position_saved variables
        """
        self.logger.debug("Resetting failures")
        self.set_error_state(False)
        self.pause              = False
        self.afc.position_saved = False
        self.afc.in_toolchange  = False

    cmd_AFC_RESUME_help = "Clear error state and restores position before resuming the print"
    def cmd_AFC_RESUME(self, gcmd: GCodeCommand) -> None:
        """
        During the PREP phase of startup, the user's RESUME macro is renamed and replaced with AFC_RESUME.
        This function clears the error state of the AFC system, sets the in_toolchange flag to False,
        runs the resume script, and restores the toolhead position to the last saved position.

        This is not a macro that should normally need to be called by the user.

        Usage
        -----
        `AFC_RESUME`

        Example
        -----
        ```
        AFC_RESUME
        ```
        """
        self.afc.in_toolchange = False
        if not self.afc.function.is_paused():
            self.logger.debug("AFC_RESUME: Printer not paused, not executing resume code")
            return

        # Save current pause state
        temp_is_paused = self.afc.function.is_paused()

        # Verify that printer is in absolute mode
        self.afc.function.check_absolute_mode("AFC_RESUME")

        move_z_pos = self.afc.last_gcode_position[2] + self.afc.z_hop
        # Check if current position is below saved gcode position, if its lower first raise z above last saved
        #   position so that toolhead does not crash into part
        if (self.afc.function.is_homed(for_move=True)
            and self.afc.gcode_move.last_position[2] <= move_z_pos):
            self.afc.move_z_pos(move_z_pos, "AFC_RESUME")
        else:
            self.logger.debug(f"AFC_RESUME: not moving in z cur_pos:{self.afc.gcode_move.last_position} move_z_pos:{move_z_pos}")

        self.logger.debug("AFC_RESUME: Before User Restore")
        self.afc.function.log_toolhead_pos()
        self.afc.gcode.run_script_from_command(
            f"{self.AFC_RENAME_RESUME_NAME} {gcmd.get_raw_command_parameters()}"
        )

        # The only time our resume should restore position is if there was an error that caused the pause
        if self.afc.error_state or temp_is_paused or self.afc.position_saved:
            self.set_error_state(False)
            # Restore position if the printer is homed, otherwise leave it where it is
            if self.afc.function.is_homed(for_move=True):
                self.afc.restore_pos(False)
            self.pause = False

        self.logger.debug(
            f"RESUME-Error State: {self.afc.error_state}, "
            f"Is Paused {self.afc.function.is_paused()}, "
            f"Position_saved {self.afc.position_saved}, "
            f"in toolchange: {self.afc.in_toolchange}"
        )

    cmd_AFC_PAUSE_help = "Pauses print, raises z by z-hop amount, and then calls users pause macro"
    def cmd_AFC_PAUSE(self, gcmd: GCodeCommand) -> None:
        """
        During the PREP phase of startup, the user's PAUSE macro is renamed and replaced with AFC_PAUSE.
        This function pauses the print, raises the Z axis by the z-hop amount, and then calls the user's pause macro.

        This is not a macro that should normally need to be called by the user.

        Usage
        -----
        `AFC_PAUSE`

        Example
        -----
        ```
        AFC_PAUSE
        ```
        """
        # Check to make sure printer is not already paused
        if not self.afc.function.is_paused():
            self.logger.debug("AFC_PAUSE: Pausing")
            # Save position
            self.afc.save_pos()
            # Need to pause as soon as possible to stop more gcode from executing, this needs to be done before movement in Z
            self.pause_resume.send_pause_command()
            # Verify that printer is in absolute mode
            self.afc.function.check_absolute_mode("AFC_PAUSE")
            move_z_pos = self.afc.last_gcode_position[2] + self.afc.z_hop
            # Check to see if current position is less than saved position plus z-hop
            if (self.afc.function.is_homed(for_move=True)
                and self.afc.gcode_move.last_position[2] <= move_z_pos):
                # Move Z up by z-hop value
                self.afc.move_z_pos(move_z_pos, "AFC_PAUSE")
            else:
                self.logger.debug(f"AFC_PAUSE: not moving in z cur_pos:{self.afc.gcode_move.last_position} move_z_pos:{move_z_pos}")
            # Call users PAUSE
            self.afc.gcode.run_script_from_command(
                f"{self.AFC_RENAME_PAUSE_NAME} {gcmd.get_raw_command_parameters()}"
            )

            timeout_to_use = max(self.error_timeout, self.idle_timeout_val)
            self.afc.gcode.run_script_from_command(f"SET_IDLE_TIMEOUT TIMEOUT={timeout_to_use}")

        else:
            self.logger.debug("AFC_PAUSE: Not Pausing")

        self.logger.debug(
            f"PAUSE-Error State: {self.afc.error_state}, "
            f"Is Paused {self.afc.function.is_paused()}, "
            f"Position_saved {self.afc.position_saved}, "
            f"in toolchange: {self.afc.in_toolchange}"
        )


    handle_lane_failure_help = "Get load errors, stop stepper and respond error"
    def handle_lane_failure(self, cur_lane: AFCLane, message: str, pause: bool=True) -> None:
        """
        Disable a lane's stepper, mark it as errored, and report the failure.

        :param cur_lane: lane object that failed
        :param message: description of the failure
        :param pause: True to pause the print after reporting the failure
        """
        # Disable the stepper for this lane
        cur_lane.do_enable(False)
        cur_lane.status = AFCLaneState.ERROR
        msg = f"{cur_lane.name} {message}"
        frame=inspect.currentframe()
        caller_frame = frame.f_back if frame else None
        stack_name = caller_frame.f_code.co_name if caller_frame else ""

        self.AFC_error(msg, pause, stack_name=stack_name)
        cur_lane.unit_obj.lane_fault(cur_lane)

def load_config(config: ConfigWrapper) -> afcError:
    """
    Klipper config entry point for the AFC_error module.

    :param config: Klipper config wrapper for the AFC_error section
    :return type: afcError instance to register with the printer
    """
    return afcError(config)