# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import chelper
import traceback

from kinematics import extruder
from configfile import error
from extras.force_move import calc_move_time

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCLane
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

LARGE_TIME_OFFSET = 99999.9

class AFCExtruderStepper(AFCLane):
    def __init__(self, config):
        super().__init__(config)

        self.extruder_stepper   = extruder.ExtruderStepper(config)

        # Check for Klipper new motion queuing update
        try:
            self.motion_queuing = self.printer.load_object(config, "motion_queuing")
        except Exception:
            self.motion_queuing = None

        self.next_cmd_time = 0.

        ffi_main, ffi_lib = chelper.get_ffi()
        self.stepper_kinematics = ffi_main.gc(
            ffi_lib.cartesian_stepper_alloc(b'x'), ffi_lib.free)

        if self.motion_queuing is not None:
            self.trapq          = self.motion_queuing.allocate_trapq()
            self.trapq_append   = self.motion_queuing.lookup_trapq_append()
        else:
            self.trapq                  = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
            self.trapq_append           = ffi_lib.trapq_append
            self.trapq_finalize_moves   = ffi_lib.trapq_finalize_moves

        self.assist_activate=False

        # Current to use while printing, set to a lower current to reduce stepper heat when printing.
        # Defaults to global_print_current, if not specified current is not changed.
        self.tmc_print_current = config.getfloat("print_current", self.afc.global_print_current)
        self.tmc_load_current = None
        if self.tmc_print_current is not None:
            self._get_tmc_values( config )

        # Get and save base rotation dist
        self.base_rotation_dist = self.extruder_stepper.stepper.get_rotation_distance()[0]

    def _get_tmc_values(self, config):
        """
        Searches for TMC driver that corresponds to stepper to get run current that is specified in config
        """
        try:
            self.tmc_driver = next(config.getsection(s) for s in config.fileconfig.sections() if 'tmc' in s and config.get_name() in s)
        except:
            msg = f"Could not find TMC for stepper {self.name},"
            msg += "\nplease add TMC section or disable 'print_current' from config files"
            raise self.gcode.error(msg)

        self.tmc_load_current = self.tmc_driver.getfloat('run_current')

    def _move(self, distance, speed, accel, assist_active=False):
        """
        Helper function to move the specified lane a given distance with specified speed and acceleration.
        This function calculates the movement parameters and commands the stepper motor
        to move the lane accordingly.
        Parameters:
        distance (float): The distance to move.
        speed (float): The speed of the movement.
        accel (float): The acceleration of the movement.
        """


        with self.assist_move(speed, distance < 0, assist_active):
            # Code based off force_move.py manual_move function
            toolhead = self.printer.lookup_object('toolhead')
            toolhead.flush_step_generation()
            prev_sk     = self.extruder_stepper.stepper.set_stepper_kinematics(self.stepper_kinematics)
            prev_trapq  = self.extruder_stepper.stepper.set_trapq(self.trapq)
            self.extruder_stepper.stepper.set_position((0., 0., 0.))
            axis_r, accel_t, cruise_t, cruise_v = calc_move_time(distance, speed, accel)
            print_time = toolhead.get_last_move_time()
            self.trapq_append(self.trapq, print_time, accel_t, cruise_t, accel_t,
                              0., 0., 0., axis_r, 0., 0., 0., cruise_v, accel)
            print_time = print_time + accel_t + cruise_t + accel_t

            if self.motion_queuing is None:
                self.extruder_stepper.stepper.generate_steps(print_time)
                self.trapq_finalize_moves(self.trapq, print_time + LARGE_TIME_OFFSET,
                                        print_time + LARGE_TIME_OFFSET)
                toolhead.note_mcu_movequeue_activity(print_time)
            else:
                self.motion_queuing.note_mcu_movequeue_activity(print_time)

            toolhead.dwell(accel_t + cruise_t + accel_t)
            toolhead.flush_step_generation()
            self.extruder_stepper.stepper.set_trapq(prev_trapq)
            self.extruder_stepper.stepper.set_stepper_kinematics(prev_sk)
            if self.motion_queuing is not None:
                self.motion_queuing.wipe_trapq(self.trapq)
            toolhead.wait_moves()

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
        direction = 1 if distance > 0 else -1
        move_total = abs(distance)
        if direction == -1:
            speed = speed * self.rev_long_moves_speed_factor

        # Breaks up move length to help with TTC errors
        while move_total > 0:
            move_value = self.max_move_dis if move_total > self.max_move_dis else move_total
            move_total -= move_value
            # Adding back direction
            move_value = move_value * direction

            self._move(move_value, speed, accel, assist_active)

    def do_enable(self, enable):
        """
        Helper function to enable/disable stepper motor

        :param enable: Enables/disables stepper motor
        """
        self.sync_print_time()
        stepper_enable = self.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable('AFC_stepper {}'.format(self.name))
        if enable:
            se.motor_enable(self.next_cmd_time)
        else:
            se.motor_disable(self.next_cmd_time)
        self.sync_print_time()

    def sync_print_time(self):
        """
        Helper function to get current print time that compares to previous synced time
        If last print time is greater than current print time, calls a toolhead dwell
        If print time is greater than last, self.new_cmd_time gets updated
        """
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        if self.next_cmd_time > print_time:
            toolhead.dwell(self.next_cmd_time - print_time)
        else:
            self.next_cmd_time = print_time

    def sync_to_extruder(self, update_current=True, extruder_name=None):
        """
        Helper function to sync lane to extruder and set print current if specified.

        :param update_current: Sets current to specified print current when True
        """
        if extruder_name is None:
            extruder_name = self.extruder_name

        self.extruder_stepper.sync_to_extruder(extruder_name)
        if update_current: self.set_print_current()

    def unsync_to_extruder(self, update_current=True):
        """
        Helper function to un-sync lane to extruder and set load current if specified.

        :param update_current: Sets current to specified load current when True
        """
        self.extruder_stepper.sync_to_extruder(None)
        if update_current: self.set_load_current()

    def _set_current(self, current):
        """
        Helper function to update TMC current.

        :param current: Sets TMC current to specified value
        """
        if self.tmc_print_current is not None and current is not None:
            self.gcode.run_script_from_command("SET_TMC_CURRENT STEPPER='{}' CURRENT={}".format(self.name, current))

    def set_load_current(self):
        """
        Helper function to update TMC current to use run current value
        """
        self._set_current( self.tmc_load_current )

    def set_print_current(self):
        """
        Helper function to update TMC current to use print current value
        """
        self._set_current( self.tmc_print_current )

    def update_rotation_distance(self, multiplier):
        """
        Helper function for updating steppers rotation distance

        :param multiplier: Multiplier to set rotation distance. Rotation distance is updated by taking
                          base rotation distance and dividing by multiplier.
        """
        self.extruder_stepper.stepper.set_rotation_distance( self.base_rotation_dist / multiplier )

def load_config_prefix(config):
    return AFCExtruderStepper(config)
