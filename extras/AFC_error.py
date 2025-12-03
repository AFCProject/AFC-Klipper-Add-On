# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import traceback
import logging
import inspect

from configparser import Error as error

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC import State
except: raise error(ERROR_STR.format(import_lib="AFC", trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCLaneState
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

def load_config(config):
    return afcError(config)

class afcError:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.errorLog= {}
        self.pause= False

    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """
        self.afc            = self.printer.lookup_object('AFC')
        self.pause_resume   = self.printer.lookup_object("pause_resume")
        self.logger         = self.afc.logger
        self.error_timeout  = self.afc.error_timeout
        self.idle_timeout_obj = self.printer.lookup_object("idle_timeout")
        self.idle_timeout_val = self.idle_timeout_obj.idle_timeout

        # Constant variable for renaming RESUME macro
        self.BASE_RESUME_NAME       = 'RESUME'
        self.AFC_RENAME_RESUME_NAME = '_AFC_RENAMED_{}_'.format(self.BASE_RESUME_NAME)
        self.BASE_PAUSE_NAME        = 'PAUSE'
        self.AFC_RENAME_PAUSE_NAME  = '_AFC_RENAMED_{}_'.format(self.BASE_PAUSE_NAME)

        self.afc.gcode.register_command('RESET_FAILURE', self.cmd_RESET_FAILURE, desc=self.cmd_RESET_FAILURE_help)
        self.afc.gcode.register_command('AFC_RESUME', self.cmd_AFC_RESUME, desc=self.cmd_AFC_RESUME_help)

    def fix(self, problem, LANE):
        self.pause= True
        self.afc = self.printer.lookup_object('AFC')
        error_handled = False
        if problem is None:
            self.PauseUserIntervention('Paused for unknown error')
        if problem=='toolhead':
            error_handled = self.ToolHeadFix(LANE)
        else:
            self.PauseUserIntervention(problem)
        if not error_handled:
            self.afc.function.afc_led(self.afc.led_fault, LANE.led_index)

        return error_handled

    def ToolHeadFix(self, cur_lane):
        if cur_lane.get_toolhead_pre_sensor_state():   #toolhead has filament
            if cur_lane.extruder_obj.lane_loaded == cur_lane.name:   #var has right lane loaded
                if not cur_lane.load_state: #Lane has filament
                    self.PauseUserIntervention('Filament not loaded in Lane')
                else:
                    self.PauseUserIntervention('no error detected')
            else:
                self.PauseUserIntervention('laneloaded does not match extruder')

        else: #toolhead empty
            if cur_lane.load_state: #Lane has filament
                while cur_lane.load_state:  # slowly back filament up to lane extruder
                    cur_lane.move(-5, self.afc.short_moves_speed, self.afc.short_moves_accel, True)
                while not cur_lane.load_state:  # reload lane extruder
                    cur_lane.move(5, self.afc.short_moves_speed, self.afc.short_moves_accel, True)

                cur_lane.tool_load = False
                cur_lane.loaded_to_hub = False
                cur_lane.extruder_obj.lane_loaded = ''
                self.afc.save_vars()
                self.pause = False
                return True

            else:
                self.PauseUserIntervention('Filament not loaded in Lane')

    def PauseUserIntervention(self,message):
        #pause for user intervention
        self.logger.error(message)
        if self.afc.function.is_homed() and not self.afc.function.is_paused():
            self.afc.save_pos()
            if self.pause:
                self.pause_print()

    def pause_print(self):
        """
        pause_print function verifies that the printer is homed and not currently paused before calling
        the base pause command
        """
        self.set_error_state( True )
        self.logger.info ('PAUSING')
        self.afc.gcode.run_script_from_command('PAUSE')
        self.logger.debug("After User Pause")
        self.afc.function.log_toolhead_pos()

    def set_error_state(self, state=False):
        logging.warning("AFC debug: setting error state {}".format(state))
        # Only save position on first error state call
        if state and not self.afc.error_state:
            self.afc.save_pos()
        self.afc.error_state = state
        self.afc.current_state = State.ERROR if state else State.IDLE

    def AFC_error(self, msg, pause=True, level=1):
        # Print to logger since respond_raw does not write to logger
        logging.warning(msg)
        # Handle AFC errors
        self.logger.error( message=msg, stack_name=inspect.stack()[level].function )
        if pause: self.pause_print()

    cmd_RESET_FAILURE_help = "CLEAR STATUS ERROR"
    def cmd_RESET_FAILURE(self, gcmd):
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

    def reset_failure(self):
        """
        Common function to reset error_state, pause, and position_saved variables
        """
        self.logger.debug("Resetting failures")
        self.set_error_state(False)
        self.pause              = False
        self.afc.position_saved = False
        self.afc.in_toolchange  = False

    cmd_AFC_RESUME_help = "Clear error state and restores position before resuming the print"
    def cmd_AFC_RESUME(self, gcmd):
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
        if self.afc.gcode_move.last_position[2] <= move_z_pos:
            self.afc.move_z_pos(move_z_pos, "AFC_RESUME")
        else:
            self.logger.debug(f"AFC_RESUME: not moving in z cur_pos:{self.afc.gcode_move.last_position} move_z_pos:{move_z_pos}")

        self.logger.debug("AFC_RESUME: Before User Restore")
        self.afc.function.log_toolhead_pos()
        self.afc.gcode.run_script_from_command("{macro_name} {user_params}".format(macro_name=self.AFC_RENAME_RESUME_NAME, user_params=gcmd.get_raw_command_parameters()))

        # The only time our resume should restore position is if there was an error that caused the pause
        if self.afc.error_state or temp_is_paused or self.afc.position_saved:
            self.set_error_state(False)
            self.afc.restore_pos(False)
            self.pause = False

        self.logger.debug("RESUME-Error State: {}, Is Paused {}, Position_saved {}, in toolchange: {}".format(
            self.afc.error_state, self.afc.function.is_paused(), self.afc.position_saved, self.afc.in_toolchange ))

    cmd_AFC_PAUSE_help = "Pauses print, raises z by z-hop amount, and then calls users pause macro"
    def cmd_AFC_PAUSE(self, gcmd):
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
            if self.afc.gcode_move.last_position[2] <= move_z_pos:
                # Move Z up by z-hop value
                self.afc.move_z_pos(move_z_pos, "AFC_PAUSE")
            else:
                self.logger.debug(f"AFC_PAUSE: not moving in z cur_pos:{self.afc.gcode_move.last_position} move_z_pos:{move_z_pos}")
            # Call users PAUSE
            self.afc.gcode.run_script_from_command("{macro_name} {user_params}".format(macro_name=self.AFC_RENAME_PAUSE_NAME, user_params=gcmd.get_raw_command_parameters()))

            timeout_to_use = max(self.error_timeout, self.idle_timeout_val)
            self.afc.gcode.run_script_from_command(f"SET_IDLE_TIMEOUT TIMEOUT={timeout_to_use}")

        else:
            self.logger.debug("AFC_PAUSE: Not Pausing")

        self.logger.debug("PAUSE-Error State: {}, Is Paused {}, Position_saved {}, in toolchange: {}".format(
            self.afc.error_state, self.afc.function.is_paused(), self.afc.position_saved, self.afc.in_toolchange ))


    handle_lane_failure_help = "Get load errors, stop stepper and respond error"
    def handle_lane_failure(self, cur_lane, message, pause=True):
        # Disable the stepper for this lane
        cur_lane.do_enable(False)
        cur_lane.status = AFCLaneState.ERROR
        msg = "{} {}".format(cur_lane.name, message)
        self.AFC_error(msg, pause, level=2)
        self.afc.function.afc_led(self.afc.led_fault, cur_lane.led_index)
