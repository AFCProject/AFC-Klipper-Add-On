# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

class afc_poop:
    def __init__(self, config):
        self.config     = config
        self.printer    = config.get_printer()
        self.afc        = self.printer.lookup_object('AFC')
        self.reactor    = self.printer.get_reactor()
        self.gcode      = self.printer.lookup_object('gcode')
        self.logger     = self.afc.logger

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

    def poop(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        step = 1
        if self.verbose:
            self.logger.info('AFC_Poop: {} Move To Purge Location'.format(step))
        pooppos = self.afc.gcode_move.last_position
        pooppos[0] = float(self.purge_loc_xy.split(',')[0])
        pooppos[1] = float(self.purge_loc_xy.split(',')[1])
        self.afc.gcode_move.move_with_transform(pooppos, 100)
        pooppos[2] = self.purge_start
        self.afc.gcode_move.move_with_transform(pooppos, 100)

        step +=1
        if self.full_fan:
            if self.verbose:
                self.logger.info('AFC_Poop: {} Set Cooling Fan to Full Speed'.format(step))
            # save fan current speed
            self.gcode.run_script_from_command('M106 S255')
            step += 1
        iteration=0
        while iteration < int(self.purge_length / self.max_iteration_length ):
            if self.verbose:
                self.logger.info('AFC_Poop: {} Purge Iteration {}'.format(step, iteration))
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
            self.afc.gcode_move.move_with_transform(pooppos, speed)
            iteration += 1
        step += 1
        if self.verbose:
            self.logger.info('AFC_Poop: {} Fast Z Lift to keep poop from sticking'.format(step))
        pooppos = self.afc.gcode_move.last_position
        pooppos[2] = self.z_lift
        self.afc.gcode_move.move_with_transform(pooppos, self.fast_z)
        step += 1
        if self.full_fan:
            if self.verbose:
                self.logger.info('AFC_Poop: {} Restore fan speed and feedrate'.format(step))
            self.gcode.run_script_from_command('M106 S0')

def load_config(config):
    return afc_poop(config)