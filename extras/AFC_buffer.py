# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback

from configparser import Error as error

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from extras.AFC_lane import AFCLane
    from extras.AFC_stepper import AFCExtruderStepper
    from gcode import GCodeCommand

try: from extras.AFC_utils import add_filament_switch
except: raise error("Error when trying to import AFC_utils.add_filament_switch\n{trace}".format(trace=traceback.format_exc()))

TRAILING_STATE_NAME = "Trailing"
ADVANCING_STATE_NAME = "Advancing"
CHECK_RUNOUT_TIMEOUT = .5

class AFCTrigger:

    def __init__(self, config):
        self.printer    = config.get_printer()
        self.afc        = self.printer.load_object(config, 'AFC')
        self.reactor    = self.afc.reactor
        self.gcode      = self.afc.gcode
        self.logger     = self.afc.logger

        self.name       = config.get_name().split(' ')[-1]
        self.lanes      = {}
        self.last_state = "Unknown"
        self.enable     = False
        self.current_lane: Optional[AFCLane|AFCExtruderStepper] = None
        self.advance_state = False
        self.trailing_state = False

        self.debug                  = config.getboolean("debug", False)
        self.enable_sensors_in_gui  = config.getboolean("enable_sensors_in_gui", self.afc.enable_sensors_in_gui)  # Set to True toolhead sensors switches as filament sensors in mainsail/fluidd gui, overrides value set in AFC.cfg
        self.buttons                = self.printer.load_object(config, "buttons")

        # Fault detection settings
        self.min_event_systime      = self.reactor.NEVER
        # error sensitivity, 0 disables, 1 is the least, 10 is the most
        self.error_sensitivity      = config.getfloat("filament_error_sensitivity", default=0, minval=0, maxval=10)
        self.fault_sensitivity      = self.get_fault_sensitivity(self.error_sensitivity)
        self.filament_error_pos     = None
        self.past_extruder_position = None
        self.extruder_pos_timer     = None
        self.fault_timer            = None

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
        self.gcode.register_mux_command("ENABLE_BUFFER",         "BUFFER", self.name, self.cmd_ENABLE_BUFFER)
        self.gcode.register_mux_command("DISABLE_BUFFER",        "BUFFER", self.name, self.cmd_DISABLE_BUFFER)
        self.gcode.register_mux_command("AFC_SET_ERROR_SENSITIVITY", "BUFFER", self.name, self.cmd_AFC_SET_ERROR_SENSITIVITY, desc=self.cmd_AFC_SET_ERROR_SENSITIVITY_help)

        # Turtleneck Buffer
        self.buttons.register_buttons([self.advance_pin], self.advance_callback)
        self.buttons.register_buttons([self.trailing_pin], self.trailing_callback)

        self.gcode.register_mux_command("SET_ROTATION_FACTOR",      "BUFFER", self.name, self.cmd_SET_ROTATION_FACTOR,  desc=self.cmd_LANE_ROT_FACTOR_help)
        self.gcode.register_mux_command("SET_BUFFER_MULTIPLIER",    "BUFFER", self.name, self.cmd_SET_BUFFER_MULTIPLIER,desc=self.cmd_SET_BUFFER_MULTIPLIER_help)

        self.afc.buffers[self.name] = self

    def __str__(self):
        """
        Return the buffer name as a string.

        :return string: Buffer name
        """
        return self.name

    def _handle_ready(self):
        """
        Handle Klipper ready event, initialize toolhead and setup fault detection if enabled.
        """
        self.min_event_systime = self.reactor.monotonic() + 2.
        self.toolhead   = self.printer.lookup_object('toolhead')

        if self.error_sensitivity > 0:
            self.setup_fault_timer()

        if self.led_index is not None:
            # Verify that LED config is found
            error_string, led = self.afc.function.verify_led_object(self.led_index)
            if led is None:
                raise error(error_string)

    # Fault detection
    # Sets up timers to check if the buffer has moved based on the distance the primary extruder has traveled

    def get_fault_sensitivity(self, sensitivity):
        """
        Get the fault sensitivity level for filament error detection.

        :param sensitivity: Float value between 0 and 10 (0 disables fault detection)
        :return float: Calculated fault sensitivity distance in mm
        """
        if sensitivity > 0:
            return (11 - sensitivity) * 10
        else:
            return 0

    def disable_fault_sensitivity(self):
        """
        Helper function to easily disable fault detection
        """
        self.fault_sensitivity = 0

    def restore_fault_sensitivity(self):
        """
        Helper function to easily set fault sensitivity back based on passed in
        filament_error_sensitivity variable
        """
        self.fault_sensitivity = self.get_fault_sensitivity(self.error_sensitivity)

    def setup_fault_timer(self):
        """
        Set up the fault detection timer and initialize error position tracking.
        """
        self.update_filament_error_pos()
        # register timer that will run to check buffer state changes
        if self.extruder_pos_timer is None:
            self.extruder_pos_timer = self.reactor.register_timer(self.extruder_pos_update_event)

    def start_fault_timer(self, print_time):
        """
        Start the fault detection timer to begin monitoring for errors.

        :param print_time: Current print time for timer scheduling
        """
        self.fault_timer = "Running"
        self.reactor.update_timer(self.extruder_pos_timer, self.reactor.NOW)

    def stop_fault_timer(self, print_time):
        """
        Stop the fault detection timer.

        :param print_time: Current print time for timer scheduling
        """
        self.fault_timer = "Stopped"
        self.reactor.update_timer(self.extruder_pos_timer, self.reactor.NEVER)

    def fault_detection_enabled(self):
        """
        Check if fault detection should be active (printing with movement and sensitivity > 0).

        :return boolean: True if printer is printing with movement and sensitivity > 0
        """
        if self.fault_sensitivity > 0:
            return True
        else:
            return False

    def start_fault_detection(self, eventtime, multiplier):
        """
        Start fault detection with the specified multiplier and reset error tracking position.

        :param eventtime: Current event time from reactor
        :param multiplier: Rotation distance multiplier to apply
        """
        self.set_multiplier( multiplier )
        self.update_filament_error_pos()
        self.start_fault_timer(eventtime)

    def get_extruder_pos(self):
        """
        Get the current extruder position, tracking only forward movement.

        :return float: Current extruder position or past position if current is less
        """
        current_pos = self.afc.function.get_extruder_pos(past_extruder_position=self.past_extruder_position)
        if current_pos is not None:
            self.past_extruder_position = current_pos
        return current_pos

    def update_filament_error_pos(self):
        """
        Update the filament error position threshold based on current extruder position and sensitivity.
        """
        current_pos = self.get_extruder_pos()
        if current_pos is not None:
            self.filament_error_pos = current_pos + self.fault_sensitivity

    def extruder_pos_update_event(self, eventtime):
        """
        Timer callback to check extruder position and trigger fault detection if threshold exceeded.

        :param eventtime: Current event time from reactor
        :return float: Next scheduled event time (eventtime + CHECK_RUNOUT_TIMEOUT)
        """
        extruder_pos = self.get_extruder_pos()
        # Check for filament problems
        if (self.afc.function.is_printing(check_movement=True)
            and extruder_pos is not None
            and self.filament_error_pos is not None):
            if extruder_pos > self.filament_error_pos:
                msg = "AFC filament fault detected! Take necessary action."
                self.pause_on_error(msg, True)
                # Update error position after triggering to prevent repeated triggers
                self.update_filament_error_pos()

        return eventtime + CHECK_RUNOUT_TIMEOUT

    def pause_on_error(self, msg, pause=False):
        """
        Pause the print with an error message if fault is detected, prevents duplicate triggers.

        :param msg: Error message to display
        :param pause: Boolean, if True triggers pause with error message
        """
        eventtime = self.reactor.monotonic()
        if eventtime < self.min_event_systime or not self.enable or self.afc.function.is_paused():
            return
        if pause:
            if self.last_state == TRAILING_STATE_NAME:
                msg += '\nCLOG DETECTED'
            if self.last_state == ADVANCING_STATE_NAME:
                msg += '\nAFC NOT FEEDING'
            self.afc.error.AFC_error( msg, True )

    def enable_buffer(self, lane: AFCLane):
        """
        Enable the buffer and set appropriate multiplier based on current state.
        """
        self.current_lane = lane
        if self.led:
            self.afc.function.afc_led(self.led_buffer_disabled, self.led_index)
        self.enable = True
        multiplier = 1.0
        if self.last_state == TRAILING_STATE_NAME:
            multiplier = self.multiplier_low
            if self.fault_detection_enabled():
                multiplier = (multiplier * 2) / 5
        else:
            multiplier = self.multiplier_high
            if self.fault_detection_enabled():
                multiplier = multiplier * 1.5

        if self.fault_detection_enabled():
            self.start_fault_detection(0, multiplier)
        else:
            self.set_multiplier( multiplier )
        self.logger.debug(f"{self.name} buffer enabled for {self.current_lane.name}")

    def disable_buffer(self):
        """
        Disable the buffer, reset multiplier, and stop fault detection if running.
        """
        self.enable = False
        if self.current_lane is None: return
        self.logger.debug(f"{self.name} buffer disabled for {self.current_lane.name}")
        if self.led:
            self.afc.function.afc_led(self.led_buffer_disabled, self.led_index)
        self.reset_multiplier()
        if self.error_sensitivity > 0 and self.extruder_pos_timer is not None:
            eventtime = self.reactor.monotonic()
            self.stop_fault_timer(eventtime)
        self.current_lane = None

    def set_multiplier(self, multiplier):
        """
        Set the rotation distance multiplier for the current lane and update LED state.

        :param multiplier: Float value to multiply rotation distance (>1 advances, <1 trails)
        """
        if not self.enable: return
        if self.current_lane is None: return
        if self.current_lane.extruder_stepper is None: return

        cur_stepper = self.current_lane.extruder_stepper.stepper

        self.current_lane.update_rotation_distance( multiplier )
        if multiplier > 1:
            self.last_state = ADVANCING_STATE_NAME
            if self.led:
                self.afc.function.afc_led(self.led_trailing, self.led_index)
        elif multiplier < 1:
            self.last_state = TRAILING_STATE_NAME
            if self.led:
                self.afc.function.afc_led(self.led_advancing, self.led_index)
        self.logger.debug("New rotation distance for {} after applying factor: {:.4f}".format(
                            self.current_lane.name,
                            cur_stepper.get_rotation_distance()[0])
                         )

    def reset_multiplier(self):
        """
        Reset the rotation distance multiplier back to 1.0 (base value).
        """
        self.logger.debug("Buffer multiplier reset")

        if self.current_lane is None: return
        if self.current_lane.extruder_stepper is None: return

        cur_stepper = self.current_lane.extruder_stepper.stepper

        self.current_lane.update_rotation_distance( 1 )
        self.logger.info(
            "Rotation distance reset for {} : {:.4f}".format(
                self.current_lane.name,
                cur_stepper.get_rotation_distance()[0])
            )

    def advance_callback(self, eventtime, state):
        """
        Handle advance buffer switch trigger, adjust multiplier and start/stop fault detection.

        :param eventtime: Current event time from reactor
        :param state: Boolean, True when switch is triggered (buffer expanded)
        """
        self.advance_state = state
        if self.printer.state_message == 'Printer is ready' and self.enable:

            if self.current_lane is not None and state:
                if self.fault_detection_enabled():
                    multiplier_extra_low = (self.multiplier_low * 2) / 5
                    self.start_fault_detection(eventtime, multiplier_extra_low)
                else:
                    self.set_multiplier( self.multiplier_low )
                    self.logger.debug("Buffer Triggered State: Advanced")
            else:
                if self.fault_detection_enabled():
                    self.stop_fault_timer(eventtime)
                    self.set_multiplier( self.multiplier_low )
                    self.logger.debug("Buffer Triggered State: Advanced")

        self.last_state = TRAILING_STATE_NAME

    def trailing_callback(self, eventtime, state):
        """
        Handle trailing buffer switch trigger, adjust multiplier and start/stop fault detection.

        :param eventtime: Current event time from reactor
        :param state: Boolean, True when switch is triggered (buffer compressed)
        """
        self.trailing_state = state
        if self.printer.state_message == 'Printer is ready' and self.enable:

            if self.current_lane is not None and state:
                if self.fault_detection_enabled():
                    multiplier_extra_high = (self.multiplier_high * 1.5)
                    self.start_fault_detection(eventtime, multiplier_extra_high)
                else:
                    self.set_multiplier( self.multiplier_high )
                    self.logger.debug("Buffer Triggered State: Trailing")
            else:
                if self.fault_detection_enabled():
                    self.stop_fault_timer(eventtime)
                    self.set_multiplier( self.multiplier_high )
                    self.logger.debug("Buffer Triggered State: Trailing")
        self.last_state = ADVANCING_STATE_NAME

    def buffer_status(self):
        """
        Return the current buffer state (Trailing or Advancing).

        :return string: Current buffer state
        """
        return self.last_state

    cmd_AFC_SET_ERROR_SENSITIVITY_help = "Set filament error sensitivity (0-10, 0=disabled)"
    def cmd_AFC_SET_ERROR_SENSITIVITY(self, gcmd):
        """
        Sets the filament error sensitivity for fault detection during printing.

        Parameters:
        - SENSITIVITY: Float value between 0 and 10 (0 disables fault detection)

        Usage
        -----
        `AFC_SET_ERROR_SENSITIVITY BUFFER=<buffer_name> SENSITIVITY=<0-10>`

        Example
        -----
        ```
        AFC_SET_ERROR_SENSITIVITY BUFFER=TN SENSITIVITY=5.0
        ```
        """
        sensitivity = gcmd.get_float('SENSITIVITY', minval=0, maxval=10)

        old_sensitivity = self.error_sensitivity
        self.error_sensitivity = sensitivity
        self.fault_sensitivity = self.get_fault_sensitivity(self.error_sensitivity)

        # Update fault detection state based on new sensitivity
        if old_sensitivity == 0 and sensitivity > 0:
            # Fault detection was disabled, now enabling
            if self.fault_detection_enabled():
                self.setup_fault_timer()
                eventtime = self.reactor.monotonic()
                if self.last_state == TRAILING_STATE_NAME:
                    multiplier = self.multiplier_low
                else:
                    multiplier = self.multiplier_high
                self.start_fault_detection(eventtime, multiplier)
        elif old_sensitivity > 0 and sensitivity == 0:
            # Fault detection was enabled, now disabling
            eventtime = self.reactor.monotonic()
            self.stop_fault_timer(eventtime)
        elif sensitivity > 0:
            # Update the error position with new sensitivity
            self.update_filament_error_pos()

        self.logger.info("Error sensitivity set to {} (fault sensitivity: {})".format(
            self.error_sensitivity, self.fault_sensitivity))

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
        if self.current_lane is not None and self.enable:
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
        if self.current_lane is not None and self.enable:
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
            if self.current_lane is not None:
                stepper = self.current_lane.extruder_stepper.stepper
                rotation_dist = stepper.get_rotation_distance()[0]
                state_info += ("\n{} Rotation distance: {:.4f}".format(self.current_lane.name,
                                                                       rotation_dist))
            if self.error_sensitivity > 0:
                state_info += "\nFault detection enabled, sensitivity {}".format(self.error_sensitivity)

        self.logger.info("{} : {}".format(self.name, state_info))

    def cmd_ENABLE_BUFFER(self, gcmd: GCodeCommand):
        """
        Manually enables the buffer for passed in lane. This command is useful for debugging and
        testing purposes.

        Usage
        -----
        `ENABLE_BUFFER BUFFER=<buffer_name> LANE=<lane_name>`

        Example
        ------
        ```
        ENABLE_BUFFER BUFFER=Turtle_1 LANE=lane2
        ```
        """
        lane = gcmd.get("LANE")
        lane_obj = self.lanes.get(lane, None)
        if not lane_obj:
            raise gcmd.error(f"{lane} not assigned to {self.name} buffer")

        self.enable_buffer(lane_obj)

    def cmd_DISABLE_BUFFER(self, gcmd):
        """
        Manually disables the buffer for currently active lane. This command is useful for debugging
        and testing purposes.

        Usage
        -----
        `DISABLE_BUFFER BUFFER=<buffer_name>`

        Example
        ------
        ```
        DISABLE_BUFFER BUFFER=Turtle_1
        ```
        """
        self.disable_buffer()

    def get_status(self, eventtime=None):
        self.response = {}
        self.response['state'] = self.last_state
        self.response['lanes'] = [lane.name for lane in self.lanes.values()]
        self.response['enabled'] = self.enable

        # Add current rotation distance if buffer is enabled and lane is loaded
        if self.enable:
            if (self.current_lane is not None
                and self.current_lane.name in self.lanes):
                stepper = self.current_lane.extruder_stepper.stepper
                self.response['rotation_distance'] = stepper.get_rotation_distance()[0]
                self.response['active_lane'] = self.current_lane.name
        else:
            self.response['rotation_distance'] = None
            self.response['active_lane'] = None

        # Add fault detection information
        self.response['fault_detection_enabled'] = self.error_sensitivity > 0
        self.response['error_sensitivity'] = self.error_sensitivity
        self.response['fault_timer'] = self.fault_timer
        # Add current extruder position and error threshold only when actively tracking
        # (tracking starts when a buffer switch is triggered during printing)
        if self.error_sensitivity > 0 and self.filament_error_pos is not None:
            current_pos = self.get_extruder_pos()
            if current_pos is not None:
                self.response['distance_to_fault'] = self.filament_error_pos - current_pos
                self.response['filament_error_pos'] = self.filament_error_pos
                self.response['current_pos'] = current_pos
            else:
                self.response['distance_to_fault'] = None
        else:
            self.response['distance_to_fault'] = None

        return self.response

def load_config_prefix(config):
    return AFCTrigger(config)