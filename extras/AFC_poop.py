# AFCProject Automated Filament Changer Software
#
# Copyright (C) 2024-2026 Armored Turtle
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from klippy import Printer
    from gcode import GCodeDispatch
    from toolhead import ToolHead
    from extras.AFC import afc
    from extras.AFC_logger import AFC_logger

class afc_poop:
    def __init__(self, config: ConfigWrapper) -> None:
        """
        Load purge/poop settings from the config and store the objects
        needed to drive the purge movement sequence.

        :param config: Klipper config wrapper for the AFC_poop section
        """
        self.config     = config
        self.printer: Printer = config.get_printer()
        self.afc: afc   = self.printer.load_object(config, 'AFC')
        self.reactor    = self.printer.get_reactor()
        self.gcode: GCodeDispatch = self.printer.load_object(config, 'gcode')
        self.logger: AFC_logger = self.afc.logger

        self.verbose = config.getboolean('verbose', False)
        self.purge_loc_xy = config.get('purge_loc_xy')
        self.purge_start = config.getfloat('purge_start', 0)
        self.purge_spd = (config.getfloat('purge_spd', 6.5))
        self.fast_z = (config.getfloat('fast_z', 200))
        self.z_lift = config.getfloat('z_lift', 20)
        self.restore_position = config.getboolean('restore_position', False)
        self.purge_start = config.getfloat('purge_start', 20)
        self.full_fan = config.getboolean('full_fan', False)
        self.purge_length = config.getfloat('purge_length', 70.111)
        self.purge_length_min = config.getfloat('purge_length_min', 60.999)
        self.max_iteration_length = config.getfloat('max_iteration_length', 40)
        self.iteration_z_raise = config.getfloat('iteration_z_raise', 6)
        self.iteration_z_change = config.getfloat('iteration_z_change', 0.6)
        self.verbose = config.getboolean('comment', False)

    def poop(self) -> None:
        """
        Runs the purge sequence: moves to the configured purge location,
        optionally spins the cooling fan to full speed, extrudes filament in
        stepped iterations while raising Z, then lifts fast to break away
        from the purge and restores the fan speed.
        """
        self.toolhead: ToolHead = self.printer.lookup_object('toolhead')
        if self.afc.gcode_move is None:
            return
        move_with_transform = self.afc.gcode_move.move_with_transform
        step = 1
        if self.verbose:
            self.logger.info(f'AFC_Poop: {step} Move To Purge Location')
        pooppos = self.afc.gcode_move.last_position
        pooppos[0] = float(self.purge_loc_xy.split(',')[0])
        pooppos[1] = float(self.purge_loc_xy.split(',')[1])
        move_with_transform(pooppos, 100)
        pooppos[2] = self.purge_start
        move_with_transform(pooppos, 100)

        step +=1
        if self.full_fan:
            if self.verbose:
                self.logger.info(f'AFC_Poop: {step} Set Cooling Fan to Full Speed')
            # save fan current speed
            self.gcode.run_script_from_command('M106 S255')
            step += 1
        iteration=0
        while iteration < int(self.purge_length / self.max_iteration_length ):
            if self.verbose:
                self.logger.info(f'AFC_Poop: {step} Purge Iteration {iteration}')
            purge_amount_left = self.purge_length - (self.max_iteration_length * iteration)
            extrude_amount = purge_amount_left / self.max_iteration_length
            extrude_ratio = extrude_amount / self.max_iteration_length
            step_triangular = iteration * (iteration + 1) / 2
            z_raise_substract = self.purge_start if iteration == 0 else step_triangular * self.iteration_z_change
            raise_z = (self.iteration_z_raise - z_raise_substract) * extrude_ratio
            duration = extrude_amount / self.purge_spd
            speed = raise_z / duration
            pooppos = self.afc.gcode_move.last_position
            pooppos[2] += raise_z
            pooppos[3] += extrude_amount
            move_with_transform(pooppos, speed)
            iteration += 1
        step += 1
        if self.verbose:
            self.logger.info(f'AFC_Poop: {step} Fast Z Lift to keep poop from sticking')
        pooppos = self.afc.gcode_move.last_position
        pooppos[2] = self.z_lift
        move_with_transform(pooppos, self.fast_z)
        step += 1
        if self.full_fan:
            if self.verbose:
                self.logger.info(f'AFC_Poop: {step} Restore fan speed and feedrate')
            self.gcode.run_script_from_command('M106 S0')

def load_config(config: ConfigWrapper) -> afc_poop:
    """
    Klipper config entry point for the AFC_poop module.

    :param config: Klipper config wrapper for the AFC_poop section
    :return type: afc_poop instance to register with the printer
    """
    return afc_poop(config)
