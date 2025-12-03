# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import traceback

from configparser import Error as error

try: from extras.AFC_utils import add_filament_switch
except: raise error("Error when trying to import AFC_utils.add_filament_switch\n{trace}".format(trace=traceback.format_exc()))

TRAILING_STATE_NAME = "Trailing"
ADVANCING_STATE_NAME = "Advancing"

class AFCTrigger:

    def __init__(self, config):
        self.printer    = config.get_printer()
        self.afc        = self.printer.lookup_object('AFC')
        self.reactor    = self.afc.reactor
        self.gcode      = self.afc.gcode
        self.logger     = self.afc.logger

        self.name       = config.get_name().split(' ')[-1]
        self.lanes      = {}
        self.last_state = "Unknown"
        self.enable     = False
        self.current    = ''
        self.advance_state = False
        self.trailing_state = False

        self.debug                  = config.getboolean("debug", False)
        self.enable_sensors_in_gui  = config.getboolean("enable_sensors_in_gui", self.afc.enable_sensors_in_gui)  # Set to True toolhead sensors switches as filament sensors in mainsail/fluidd gui, overrides value set in AFC.cfg
        self.buttons                = self.printer.load_object(config, "buttons")

        # LED SETTINGS
        self.led                    = False
        self.led_index              = config.get('led_index', None)
        self.led_advancing          = config.get('led_buffer_advancing','0,0,1,0')
        self.led_trailing           = config.get('led_buffer_trailing','0,1,0,0')
        self.led_buffer_disabled    = config.get('led_buffer_disable', '0,0,0,0.25')

        if self.led_index is not None:
            self.led = True
            self.led_index = config.get('led_index')

        # Try and get one of each pin to see how user has configured buffer
        self.advance_pin        = config.get('advance_pin', None)
        self.buffer_distance    = config.getfloat('distance', None)

        # Pull config for Turtleneck style buffer (advance and training switches)
        self.advance_pin        = config.get('advance_pin') # Advance pin for buffer
        self.trailing_pin       = config.get('trailing_pin') # Trailing pin for buffer
        self.multiplier_high    = config.getfloat("multiplier_high", default=1.1, minval=1.0)
        self.multiplier_low     = config.getfloat("multiplier_low", default=0.9, minval=0.0, maxval=1.0)

        self.adv_filament_switch_name = "{}_{}".format(self.name, "expanded")
        self.fila_avd = add_filament_switch(self.adv_filament_switch_name, self.advance_pin, self.printer, show_sensor=self.enable_sensors_in_gui )

        self.trail_filament_switch_name = "{}_{}".format(self.name, "compressed")
        self.fila_trail = add_filament_switch(self.trail_filament_switch_name, self.trailing_pin, self.printer, show_sensor=self.enable_sensors_in_gui )

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        self.function = self.printer.load_object(config, 'AFC_functions')
        self.show_macros = self.afc.show_macros

        self.function.register_mux_command(self.show_macros, "QUERY_BUFFER", "BUFFER", self.name,
                                            self.cmd_QUERY_BUFFER,
                                        self.cmd_QUERY_BUFFER_help, self.cmd_QUERY_BUFFER_options)
        self.gcode.register_mux_command("ENABLE_BUFFER",        "BUFFER", self.name, self.cmd_ENABLE_BUFFER)
        self.gcode.register_mux_command("DISABLE_BUFFER",       "BUFFER", self.name, self.cmd_DISABLE_BUFFER)

        # Turtleneck Buffer
        self.buttons.register_buttons([self.advance_pin], self.advance_callback)
        self.buttons.register_buttons([self.trailing_pin], self.trailing_callback)

        self.gcode.register_mux_command("SET_ROTATION_FACTOR",      "BUFFER", self.name, self.cmd_SET_ROTATION_FACTOR,  desc=self.cmd_LANE_ROT_FACTOR_help)
        self.gcode.register_mux_command("SET_BUFFER_MULTIPLIER",    "BUFFER", self.name, self.cmd_SET_BUFFER_MULTIPLIER,desc=self.cmd_SET_BUFFER_MULTIPLIER_help)

        self.afc.buffers[self.name] = self

    def __str__(self):
        return self.name

    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2.

        if self.led_index is not None:
            # Verify that LED config is found
            error_string, led = self.afc.function.verify_led_object(self.led_index)
            if led is None:
                raise error(error_string)

    def enable_buffer(self):
        if self.led:
            self.afc.function.afc_led(self.led_buffer_disabled, self.led_index)
        self.enable = True
        multiplier = 1.0
        if self.last_state == TRAILING_STATE_NAME:
            multiplier = self.multiplier_low
        else:
            multiplier = self.multiplier_high
        self.set_multiplier( multiplier )
        self.logger.debug("{} buffer enabled".format(self.name))

    def disable_buffer(self):
        self.enable = False
        self.logger.debug("{} buffer disabled".format(self.name))
        if self.led:
            self.afc.function.afc_led(self.led_buffer_disabled, self.led_index)
        self.reset_multiplier()

    # Turtleneck commands
    def set_multiplier(self, multiplier):
        if not self.enable: return
        cur_stepper = self.afc.function.get_current_lane_obj()
        if cur_stepper is None: return

        cur_stepper.update_rotation_distance( multiplier )
        if multiplier > 1:
            self.last_state = ADVANCING_STATE_NAME
            if self.led:
                self.afc.function.afc_led(self.led_trailing, self.led_index)
        elif multiplier < 1:
            self.last_state = TRAILING_STATE_NAME
            if self.led:
                self.afc.function.afc_led(self.led_advancing, self.led_index)
        self.logger.debug("New rotation distance after applying factor: {:.4f}".format(cur_stepper.extruder_stepper.stepper.get_rotation_distance()[0]))

    def reset_multiplier(self):
        self.logger.debug("Buffer multiplier reset")

        cur_stepper = self.afc.function.get_current_lane_obj()
        if cur_stepper is None: return

        cur_stepper.update_rotation_distance( 1 )
        self.logger.info("Rotation distance reset : {:.4f}".format(cur_stepper.extruder_stepper.stepper.get_rotation_distance()[0]))

    def advance_callback(self, eventime, state):
        self.advance_state = state
        if self.printer.state_message == 'Printer is ready' and self.enable:
            cur_lane = self.afc.function.get_current_lane_obj()

            if cur_lane is not None and state:
                self.set_multiplier( self.multiplier_low )
                self.logger.debug("Buffer Triggered State: Advanced")
        self.last_state = TRAILING_STATE_NAME

    def trailing_callback(self, eventime, state):
        self.trailing_state = state
        if self.printer.state_message == 'Printer is ready' and self.enable:
            cur_lane = self.afc.function.get_current_lane_obj()

            if cur_lane is not None and state:
                self.set_multiplier( self.multiplier_high )
                self.logger.debug("Buffer Triggered State: Trailing")
        self.last_state = ADVANCING_STATE_NAME

    def buffer_status(self):
        return self.last_state

    cmd_SET_BUFFER_MULTIPLIER_help = "live adjust buffer high and low multiplier"
    def cmd_SET_BUFFER_MULTIPLIER(self, gcmd):
        """
        This function handles the adjustment of the buffer multipliers for the turtleneck buffer.
        It retrieves the multiplier type ('HIGH' or 'LOW') and the factor to be applied. The function
        ensures that the factor is valid and updates the corresponding multiplier.

        Usage
        -----
        `SET_BUFFER_MULTIPLIER BUFFER=<buffer_name> MULTIPLIER=<HIGH/LOW> FACTOR=<factor>`

        Example
        -----
        ```
        SET_BUFFER_MULTIPLIER BUFFER=TN MULTIPLIER=HIGH FACTOR=1.2
        ```
        """
        cur_stepper = self.afc.function.get_current_lane_obj()
        if cur_stepper is not None and self.enable:
            chg_multiplier = gcmd.get('MULTIPLIER', None)
            if chg_multiplier is None:
                self.logger.info("Multiplier must be provided, HIGH or LOW")
                return
            chg_factor = gcmd.get_float('FACTOR')
            if chg_factor <= 0:
                self.logger.info("FACTOR must be greater than 0")
                return
            if chg_multiplier == "HIGH" and chg_factor > 1:
                self.multiplier_high = chg_factor
                self.set_multiplier(chg_factor)
                self.logger.info("multiplier_high set to {}".format(chg_factor))
                self.logger.info('multiplier_high: {} MUST be updated under buffer config for value to be saved'.format(chg_factor))
            elif chg_multiplier == "LOW" and chg_factor < 1:
                self.multiplier_low = chg_factor
                self.set_multiplier(chg_factor)
                self.logger.info("multiplier_low set to {}".format(chg_factor))
                self.logger.info('multiplier_low: {} MUST be updated under buffer config for value to be saved'.format(chg_factor))
            else:
                self.logger.info('multiplier_high must be greater than 1, multiplier_low must be less than 1')

    cmd_LANE_ROT_FACTOR_help = "change rotation distance by factor specified"
    def cmd_SET_ROTATION_FACTOR(self, gcmd):
        """
        Adjusts the rotation distance of the current AFC stepper motor by applying a
        specified factor. If no factor is provided, it defaults to 1.0, which resets
        the rotation distance to the base value.

        Behavior:

        - The `FACTOR` must be greater than 0.
        - If the buffer is enabled and active, and a valid factor is provided, the function adjusts the rotation
          distance for the current AFC stepper.
        - If `FACTOR` is 1.0, the rotation distance is reset to the base value.
        - If `FACTOR` is a valid non-zero number, the rotation distance is updated by the provided factor.
        - If `FACTOR` is 0 or AFC is not enabled, an appropriate message is sent back through the G-code interface.

        Usage
        -----
        `SET_ROTATION_FACTOR BUFFER=<buffer_name> FACTOR=<factor>`

        Example
        -----
        ```
        SET_ROTATION_FACTOR BUFFER=TN FACTOR=1.2
        ```
        """
        cur_stepper = self.afc.function.get_current_lane_obj()
        if cur_stepper is not None and self.enable:
            change_factor = gcmd.get_float('FACTOR', 1.0)
            if change_factor <= 0:
                self.logger.info("FACTOR must be greater than 0")
                return
            elif change_factor == 1.0:
                self.set_multiplier ( 1 )
                self.logger.info("Rotation distance reset to base value")
            else:
                self.set_multiplier( change_factor )
        else:
            self.logger.info("BUFFER {} NOT ENABLED".format(self.name))

    cmd_QUERY_BUFFER_help = "Report Buffer sensor state"
    cmd_QUERY_BUFFER_options = {"BUFFER": {"type": "string", "default": "Turtle_1"}}
    def cmd_QUERY_BUFFER(self, gcmd):
        """
        Reports the current state of the buffer sensor and, if applicable, the rotation
        distance of the current AFC stepper motor.

        Behavior

        - If the `turtleneck` feature is enabled and a tool is loaded, the rotation
          distance of the current AFC stepper motor is reported, along with the
          current state of the buffer sensor.
        - If the `turtleneck` feature is not enabled, only the buffer state is reported.
        - Both the buffer state and, if applicable, the stepper motor's rotation
          distance are sent back as G-code responses.

        Usage
        -----
        `QUERY_BUFFER BUFFER=<buffer_name>`

        Example
        ------
        ```
        QUERY_BUFFER BUFFER=TN
        ```
        """
        state_mapping = {
            TRAILING_STATE_NAME: ' (buffer is compressing)',
            ADVANCING_STATE_NAME: ' (buffer is expanding)',
        }

        buffer_status = self.buffer_status()
        state_info = "{}{}".format(buffer_status, state_mapping.get(buffer_status, ''))

        if self.enable:
            lane = self.afc.function.get_current_lane_obj()
            stepper = lane.extruder_stepper.stepper
            rotation_dist = stepper.get_rotation_distance()[0]
            state_info += ("\n{} Rotation distance: {:.4f}".format(lane.name, rotation_dist))

        self.logger.info("{} : {}".format(self.name, state_info))

    def cmd_ENABLE_BUFFER(self, gcmd):
        """
        Manually enables the buffer. This command is useful for debugging and testing purposes.

        Usage
        -----
        `ENABLE_BUFFER`
        """
        self.enable_buffer()

    def cmd_DISABLE_BUFFER(self, gcmd):
        """
        Manually disables the buffer. This command is useful for debugging and testing purposes.

        Usage
        -----
        `DISABLE_BUFFER`
        """
        self.disable_buffer()

    def get_status(self, eventtime=None):
        self.response = {}
        self.response['state'] = self.last_state
        self.response['lanes'] = [lane.name for lane in self.lanes.values()]
        self.response['enabled'] = self.enable
        return self.response

def load_config_prefix(config):
    return AFCTrigger(config)