# AFCProject Automated Filament Changer
#
# Copyright (C) 2024-2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# This file include code inspired/modified from OpenAms Project. https://github.com/OpenAMSOrg/klipper_openams
# Originally authored by JR Lomas(aka KnightRadiant) and licensed under the MIT license
# Full license text available at: https://mit-license.org/

# AFCFPSBuffer code was contributed by lindnjoe(aka J0eB0l)

from __future__ import annotations

import traceback
import inspect

from configparser import Error as error

from typing import TYPE_CHECKING, Optional, Callable, Union

if TYPE_CHECKING:
    from extras.AFC import afc
    from extras.AFC_lane import AFCLane
    from extras.AFC_stepper import AFCExtruderStepper
    from gcode import GCodeCommand
    from configfile import ConfigWrapper
    from reactor import SelectReactor as Reactor, ReactorCompletion
    from mcu import MCU, MCU_adc

try: from extras.AFC_utils import add_filament_switch, VirtualFilamentSensor
except: raise error("Error when trying to import AFC_utils.add_filament_switch\n{trace}".format(trace=traceback.format_exc()))

TRAILING_STATE_NAME = "Trailing"
ADVANCING_STATE_NAME = "Advancing"
NEUTRAL_STATE_NAME = "Neutral"
CHECK_RUNOUT_TIMEOUT = 0.5
FPS_ENDSTOP_POLL_TIME = 0.01  # 10ms poll interval for software endstop

class AFCBuffer:
    def __init__(self, config):
        self.printer    = config.get_printer()
        self.afc: afc    = self.printer.load_object(config, 'AFC')
        self.reactor    = self.afc.reactor
        self.gcode      = self.afc.gcode
        self.logger     = self.afc.logger
        self.type       = config.get("type", "switched")

        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.function = self.printer.load_object(config, 'AFC_functions')

        self.name       = config.get_name().split(' ')[-1]
        self.lanes      = {}
        self.last_state = "Unknown"
        self.enable     = False
        self.current_lane: Optional[AFCLane|AFCExtruderStepper] = None
        self.advance_state = False
        self.trailing_state = False
        self.toolhead = None

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
        self.led_advancing          = config.get('led_buffer_advancing', self.afc.led_buffer_advancing)
        self.led_trailing           = config.get('led_buffer_trailing', self.afc.led_buffer_trailing)
        self.led_neutral            = config.get('led_buffer_neutral', self.afc.led_buffer_neutral)
        self.led_buffer_disabled    = config.get('led_buffer_disable', self.afc.led_buffer_disabled)

        if self.led_index is not None:
            self.led = True
            self.led_index = config.get('led_index')

        self.show_macros = self.afc.show_macros

        # Try and get one of each pin to see how user has configured buffer
        self.advance_pin: str   = None
        self.trailing_pin: str  = None
        self.buffer_distance    = config.getfloat('distance', None)
        self.multiplier_high    = config.getfloat("multiplier_high", default=1.1, minval=1.0)
        self.multiplier_low     = config.getfloat("multiplier_low", default=0.9, minval=0.0, maxval=1.0)
        self._last_multiplier   = 1

        if self.type == "switched":
            # Pull config for Turtleneck style buffer (advance and training switches)
            self.advance_pin        = config.get('advance_pin') # Advance pin for buffer
            self.trailing_pin       = config.get('trailing_pin') # Trailing pin for buffer

            self.adv_filament_switch_name = "{}_{}".format(self.name, "expanded")
            self.fila_adv, _ = add_filament_switch(self.adv_filament_switch_name, self.advance_pin,
                                                   self.printer, show_sensor=self.enable_sensors_in_gui)

            self.trail_filament_switch_name = "{}_{}".format(self.name, "compressed")
            self.fila_trail, _ = add_filament_switch(self.trail_filament_switch_name, self.trailing_pin,
                                                     self.printer, show_sensor=self.enable_sensors_in_gui)

            # Turtleneck Buffer
            self.buttons.register_buttons([self.advance_pin], self.advance_callback)
            self.buttons.register_buttons([self.trailing_pin], self.trailing_callback)

        # Register Macros
        self.function.register_mux_command(self.show_macros, "QUERY_BUFFER", "BUFFER", self.name,
                                           self.cmd_QUERY_BUFFER, self.cmd_QUERY_BUFFER_help,
                                           self.cmd_QUERY_BUFFER_options)
        self.gcode.register_mux_command("ENABLE_BUFFER",         "BUFFER", self.name, self.cmd_ENABLE_BUFFER)
        self.gcode.register_mux_command("DISABLE_BUFFER",        "BUFFER", self.name, self.cmd_DISABLE_BUFFER)
        self.gcode.register_mux_command("AFC_SET_ERROR_SENSITIVITY", "BUFFER", self.name, self.cmd_AFC_SET_ERROR_SENSITIVITY, desc=self.cmd_AFC_SET_ERROR_SENSITIVITY_help)


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

    @property
    def extruder(self):
        """Return the active toolhead extruder.

        The native fps.py stored a direct extruder reference.  OAMSMonitor
        accesses `fps.extruder.last_position` for clog detection.
        This property provides the same interface without requiring an
        extruder config option — it simply returns whatever extruder is
        currently active on the toolhead.
        """
        if self.toolhead is not None:
            return self.toolhead.get_extruder()
        return None

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
        if self.extruder_pos_timer:
            self.fault_timer = "Running"
            self.reactor.update_timer(self.extruder_pos_timer, self.reactor.NOW)

    def stop_fault_timer(self, print_time):
        """
        Stop the fault detection timer.

        :param print_time: Current print time for timer scheduling
        """
        if self.extruder_pos_timer:
            self.fault_timer = "Stopped"
            self.reactor.update_timer(self.extruder_pos_timer, self.reactor.NEVER)

    def fault_detection_enabled(self):
        """
        Check if fault detection should be active (printing with movement and sensitivity > 0).

        :return boolean: True if printer is printing with movement and sensitivity > 0
        """
        return bool(self.fault_sensitivity > 0)

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
        current_pos = self.afc.function.get_extruder_pos(
            past_extruder_position=self.past_extruder_position
        )

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

        stepper_less_drive = False
        if (self.current_lane is not None
            and getattr(self.current_lane.unit_obj, "stepperless_drive", None)):
            stepper_less_drive = True
        # Check for filament problems
        if (not stepper_less_drive
            and self.afc.function.is_printing(check_movement=True)
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
        if (eventtime < self.min_event_systime
            or not self.enable
            or self.afc.function.is_paused()):
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
        if (self.error_sensitivity > 0
            and self.extruder_pos_timer is not None):
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

        self.current_lane.update_rotation_distance(multiplier)
        self._last_multiplier = multiplier
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
                if self.type == "switched":
                    if self.last_state == TRAILING_STATE_NAME:
                        multiplier = self.multiplier_low
                    else:
                        multiplier = self.multiplier_high
                    self.start_fault_detection(eventtime, multiplier)
                else:
                    self.start_fault_detection(0, 1.0)
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
            chg_multiplier = chg_multiplier.upper()
            chg_factor = gcmd.get_float('FACTOR')
            if chg_factor <= 0:
                self.logger.info("FACTOR must be greater than 0")
                return
            if chg_multiplier == "HIGH" and chg_factor > 1:
                self.multiplier_high = chg_factor
                if self.type == "switched": self.set_multiplier(chg_factor)
                self.logger.info("multiplier_high set to {}".format(chg_factor))
                self.logger.info('multiplier_high: {} MUST be updated under buffer config for value to be saved'.format(chg_factor))
            elif chg_multiplier == "LOW" and chg_factor < 1:
                self.multiplier_low = chg_factor
                if self.type == "switched": self.set_multiplier(chg_factor)
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
        if (self.enable
            and self.current_lane):
            if (self.current_lane.extruder_stepper is not None
                and self.current_lane.extruder_stepper.stepper is not None):
                stepper = self.current_lane.extruder_stepper.stepper
                self.response['rotation_distance'] = stepper.get_rotation_distance()[0]
            self.response['active_lane'] = self.current_lane.name
        else:
            self.response['rotation_distance'] = None
            self.response['active_lane'] = None

        # Add in multiplier information for automated testing
        self.response['multiplier_high'] = self.multiplier_high
        self.response['multiplier_low'] = self.multiplier_low
        self.response['multiplier'] = getattr(self, '_last_multiplier', 1.0)

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

class FPSEndstopWrapper:
    """
    Software endstop that triggers based on FPS reading threshold.

    Implements the MCU endstop interface so klipper's homing/drip_move system
    can use the FPS analog reading as a buffer endstop — just like a turtleneck
    switch, but triggered at a configurable threshold.

    For advance endstop: triggers when smoothed_fps >= high_point (buffer compressed)
    For trailing endstop: triggers when smoothed_fps <= low_point (buffer stretched)
    """

    def __init__(self, fps_buffer: AFCFPSBuffer, trigger_func: Callable[[], bool]) -> None:
        """
        Initialize the software endstop wrapper.

        :param fps_buffer: Owning :class:`AFCFPSBuffer` providing the ADC/reactor.
        :param trigger_func: Callable returning True when the FPS threshold for
                             this endstop is reached.
        """
        self._fps_buffer: AFCFPSBuffer = fps_buffer
        self._reactor: Reactor = fps_buffer.reactor
        self._trigger_func: Callable[[], bool] = trigger_func
        self._steppers: list = []
        self._trigger_time: float = 0.
        self._completion: Optional[ReactorCompletion] = None
        self._poll_timer: Optional[object] = None

    def get_mcu(self) -> MCU:
        """
        Return the MCU that owns the FPS ADC pin.

        :return: The MCU object backing the FPS ADC.
        """
        return self._fps_buffer.adc.get_mcu()

    def add_stepper(self, stepper) -> None:
        """
        Register a stepper as homed by this endstop.

        :param stepper: Stepper object to associate with the endstop.
        """
        if stepper in self._steppers:
            return
        self._steppers.append(stepper)

    def get_steppers(self) -> list:
        """
        Return the steppers registered on this endstop.

        :return: A new list of the associated stepper objects.
        """
        return list(self._steppers)

    def home_start(self, print_time: float, sample_time: float, sample_count: int,
                   rest_time: float, triggered: bool = True) -> ReactorCompletion:
        """
        Begin a homing move, polling the FPS reading for the trigger condition.

        If the trigger condition is already met, completes immediately;
        otherwise starts a reactor timer that polls until it is.

        :param print_time: Print time at which homing starts (unused).
        :param sample_time: Endstop sample time (unused).
        :param sample_count: Endstop sample count (unused).
        :param rest_time: Endstop rest time (unused).
        :param triggered: Trigger state to home toward; defaults to True.
        :return: A reactor completion that resolves when triggered.
        """
        self._trigger_time = 0.
        self._completion = self._reactor.completion()
        # Check if already triggered before starting poll timer
        if self._trigger_func() == triggered:
            mcu = self.get_mcu()
            self._trigger_time = mcu.estimated_print_time(
                self._reactor.monotonic())
            self._completion.complete(True)
        else:
            self._poll_timer = self._reactor.register_timer(
                self._poll_fps, self._reactor.NOW)
        return self._completion

    def _poll_fps(self, eventtime: float) -> float:
        """
        Reactor timer callback that checks the FPS trigger condition.

        :param eventtime: Reactor event time of the poll.
        :return: `reactor.NEVER` once triggered, otherwise the next poll time.
        """
        if self._trigger_func():
            mcu = self.get_mcu()
            self._trigger_time = mcu.estimated_print_time(eventtime)
            self._completion.complete(True)
            return self._reactor.NEVER
        return eventtime + FPS_ENDSTOP_POLL_TIME

    def home_wait(self, home_end_time: float) -> float:
        """
        Finish a homing move, stopping the poll timer.

        :param home_end_time: Latest print time the move may run to (unused).
        :return: The print time at which the endstop triggered, or 0 if not.
        """
        if self._poll_timer is not None:
            self._reactor.unregister_timer(self._poll_timer)
            self._poll_timer = None
        return self._trigger_time

    def query_endstop(self, print_time: float) -> int:
        """
        Report the instantaneous triggered state of the endstop.

        :param print_time: Print time of the query (unused).
        :return: 1 when the FPS trigger condition is met, otherwise 0.
        """
        return 1 if self._trigger_func() else 0

# Works with any analog filament-position sensor reporting a 0.0-1.0 signal,
# including:
# - The native OpenAMS FPS (Filament Pressure Sensor)
#
# FPS reading semantics:
#   0.1 (low)  -> buffer stretched / tension -> increase feed
#   0.5 (mid)  -> buffer centered / ideal state
#   0.9 (high) -> buffer compressed / pushing -> decrease feed
#
# PSF users: this driver accepts PSF-compatible config aliases:
#   neutral_point  -> set_point    (default 0.5)
#   max_tension    -> low_point    (default 0.1)
#   max_compression -> high_point  (default 0.9)
#
# For units WITHOUT stepper motors (OpenAMS, etc.), the driver simply
# provides the ADC reading. The unit's own code (AFC_OpenAMS) manages
# tool_start_state and virtual sensors exactly as it already does, this
# driver just acts as the FPS hardware interface and config entry point.
class AFCFPSBuffer(AFCBuffer):
    """
    FPS-based buffer driver for AFC.

    Reads an analog filament position sensor and proportionally adjusts the
    lane's stepper rotation distance to keep the FPS reading at the configured
    set_point.
    """

    def __init__(self, config: ConfigWrapper) -> None:
        """
        Initialize the FPS buffer from its config section.

        Sets up the ADC sensor and sampling/callbacks, validates the tuning
        points (`low_point` < `set_point` < `high_point` and deadband),
        creates the virtual filament sensors, registers G-code commands and the
        software endstops, and registers the buffer with AFC.

        :param config: ConfigWrapper providing the ADC pin, buffer tuning
                       parameters, fault/LED options and GUI settings.
        """
        super().__init__(config)
        self._advance_latched: bool = False
        self._latch_enabled: bool = False  # Only latch during active loads

        # ---- ADC / FPS sensor configuration ----
        self.ppins = self.printer.lookup_object('pins')
        adc_pin: str = config.get('adc_pin')
        self.adc: MCU_adc = self.ppins.setup_pin('adc', adc_pin)

        self.sample_count: int = config.getint('sample_count', 5)
        self.sample_time: float = config.getfloat('sample_time', 0.005)
        self.report_time: float = config.getfloat('report_time', 0.100)
        self.reversed: bool = config.getboolean('reversed', False)

        # Setup ADC sampling — handle both Klipper and Kalico APIs
        if hasattr(self.adc, "setup_minmax"): # Kalico has this
            self.logger.info("Kalico setup ADC")
            self.adc.setup_minmax(self.sample_time, self.sample_count)
        else:
            adc_sig = inspect.signature(self.adc.setup_adc_sample)
            sig_keys = list(adc_sig.parameters.keys())
            if "report_time" in sig_keys: # Newer Klipper has report_time key
                self.adc.setup_adc_sample(self.report_time, self.sample_time, self.sample_count)
            else:
                self.adc.setup_adc_sample(self.sample_time, self.sample_count)

        # Register ADC callback
        adc_callback_sig = inspect.signature(self.adc.setup_adc_callback)
        sig_keys = list(adc_callback_sig.parameters.keys())
        if "report_time" in sig_keys: # Newer Klipper has report time key
            self.adc.setup_adc_callback(self.report_time, self._adc_callback)
        else: # Kalico/Older Klipper versions
            self.adc.setup_adc_callback(self._adc_callback)

        # ---- Buffer tuning parameters ----
        # PSF-compatible aliases: neutral_point/max_tension/max_compression
        # are accepted alongside set_point/low_point/high_point. If both
        # are specified, the primary name takes precedence.
        self.fps_value: float = 0.0
        _sp_default = config.getfloat('neutral_point', 0.5, minval=0.1, maxval=0.9)
        self.set_point: float = config.getfloat('set_point', _sp_default, minval=0.1, maxval=0.9)
        _lp_default = config.getfloat('max_tension', 0.1, minval=0.0, maxval=0.5)
        self.low_point: float = config.getfloat('low_point', _lp_default, minval=0.0, maxval=0.5)
        _hp_default = config.getfloat('max_compression', 0.9, minval=0.5, maxval=1.0)
        self.high_point: float = config.getfloat('high_point', _hp_default, minval=0.5, maxval=1.0)
        self._homing_high_point: float = config.getfloat("homing_high_point", 0.7,
                                                         minval=0.6, maxval=1.0)

        # Multiplier range — how aggressively the buffer corrects
        self.multiplier_high: float = config.getfloat('multiplier_high', 1.15, minval=1.0)
        self.multiplier_low: float = config.getfloat('multiplier_low', 0.85, minval=0.0, maxval=1.0)
        self.trailing_min_multiplier: float = config.getfloat('trailing_min_multiplier', 1.00, minval=1.0)

        # Hysteresis on the applied multiplier: a correction is only sent to the stepper
        # (update_rotation_distance) when it differs from the last *applied* multiplier by more
        # than this absolute amount (e.g. 0.003 means a multiplier of 1.050 -> 1.052 is
        # suppressed, but 1.050 -> 1.054 is not).
        self.multiplier_hysteresis: float = config.getfloat('multiplier_hysteresis', 0.003,
                                                            minval=0.0, maxval=1.0)

        # Deadband — neutral window centered on set_point where no correction
        # is applied, giving headroom for retractions/tool changes.
        self.deadband: float = config.getfloat('deadband', 0.30, minval=0.0, maxval=0.6)

        # Validate that low_point < set_point < high_point
        if self.low_point >= self.set_point:
            error_msg = (f"AFC_FPS {self.name}: low_point ({self.low_point}) "
                f"must be less than set_point ({self.set_point})")
            raise error(error_msg)

        if self.high_point <= self.set_point:
            error_msg = (f"AFC_FPS {self.name}: high_point ({self.high_point}) "
                f"must be greater than set_point ({self.set_point})")
            raise error(error_msg)

        # Validate deadband doesn't exceed the available range
        error_msg = self._check_deadband(self.set_point, self.deadband)
        if error_msg:
            raise error(error_msg)

        # Smoothing factor for exponential moving average (0 = no smoothing, 1 = max)
        self.smoothing: float = config.getfloat('smoothing', 0.3, minval=0.0, maxval=0.95)
        self.smoothed_fps: float = 0.5

        # Timer interval for applying corrections
        self.update_interval: float = config.getfloat('update_interval', 0.25, minval=0.05)

        # Slow integral term added on top of the proportional multiplier so a
        # persistent bias (e.g. a slightly-off rotation_distance) doesn't
        # force the buffer to sit off set_point forever. Opt-in.
        self.enable_integral_correction: bool = config.getboolean(
            'enable_integral_correction', False)

        # Multiplier-units nudge per second of active extrusion per unit of
        # deviation.
        self.integral_gain: float = config.getfloat('integral_gain', 0.004, minval=0.0, maxval=0.2)

        # lane_name -> accumulated integral bias, kept per-lane so tool
        # changes don't cross-contaminate one lane's learned bias into another's.
        self._integral_terms: dict = {}

        # Last integral value written to the debug log, so it only logs on change.
        self._last_logged_integral: float = 0.0

        # Only integrate while filament is actually moving, so idle time
        # (paused, between prints) doesn't wind up the integral for no reason.
        # Kept tiny so slow printing (first layers, fine detail) still counts --
        # this should catch any real movement, not just fast movement.
        self.integral_extrusion_threshold: float = config.getfloat(
            'integral_extrusion_threshold', 0.02, minval=0.0)
        self._integral_last_extruder_pos: Optional[float] = None

        # ---- Fault detection ----
        self.min_event_systime: float = self.reactor.NEVER

        # ---- Register virtual filament sensors for GUI display ----
        # Lets Mainsail show buffer state (grey = ramming) instead of red (no sensor).
        self.adv_filament_switch_name: str = f"{self.name}_expanded"
        self.fila_adv: VirtualFilamentSensor = VirtualFilamentSensor(
            self.printer, self.adv_filament_switch_name,
            logger=self.logger, show_in_gui=self.enable_sensors_in_gui)
        self.trail_filament_switch_name: str = f"{self.name}_compressed"
        self.fila_trail: VirtualFilamentSensor = VirtualFilamentSensor(
            self.printer, self.trail_filament_switch_name,
            logger=self.logger, show_in_gui=self.enable_sensors_in_gui)

        # ---- Correction timer ----
        self.correction_timer = self.reactor.register_timer(self._correction_event)
        self._correction_running: bool = False

        # Track the last applied multiplier for the active lane so we can
        # restore it on tool changes. Key: extruder name → (lane_name, multiplier)
        self._last_multiplier: float = 1.0
        self._saved_multipliers: dict = {}

        # Software endstop wrappers so homing can use FPS thresholds like
        # turtleneck switches (advance triggers at high_point, trailing at low_point).
        self.fps_endstop: FPSEndstopWrapper = FPSEndstopWrapper(
            self, lambda: self.buffer_triggered)
        self.fps_trailing_endstop: FPSEndstopWrapper = FPSEndstopWrapper(
            self, lambda: self.buffer_trailing_triggered)

        # Register macros
        self.gcode.register_mux_command("AFC_SET_FPS_SET_POINT", "BUFFER", self.name,
                                        self.cmd_AFC_SET_FPS_SET_POINT,
                                        desc=self.cmd_AFC_SET_FPS_SET_POINT_help)

    def _check_deadband(self, set_point: float, deadband: float) -> str:
        """
        Helper method for checking deadband to verify that its not greater/lower than high/low
        set points.

        :param set_point: Set point to calculate deadband from
        :param deadband: Deadband value to calculate range with
        :return str: If deadband is high/lower than high/low set points an error message is returned.
        """
        half_db = deadband / 2.0
        deadband_low = set_point - half_db
        deadband_high = set_point + half_db
        error_msg = ""

        if deadband_low <= self.low_point:
            error_msg = (f"AFC_FPS {self.name}: deadband ({deadband}) too wide - "
                f"neutral_low ({deadband_low:.3f}) must be above low_point ({self.low_point})")

        if deadband_high >= self.high_point:
            if error_msg: error_msg += "\n"
            error_msg += (f"AFC_FPS {self.name}: deadband ({deadband}) too wide - "
                f"neutral_high ({deadband_high:.3f}) must be below high_point ({self.high_point})")
        return error_msg

    def get_fps_value(self) -> float:
        """
        Get current FPS pressure value (0.0-1.0).
        """
        return self.fps_value

    @property
    def buffer_triggered(self) -> bool:
        """
        True when FPS reading indicates the buffer is compressed (at high_point).

        This is the FPS equivalent of the turtleneck advance switch being
        triggered — used by tool_loaded_check to verify filament is loaded
        into the toolhead without requiring a hardware endstop.
        """
        return self.smoothed_fps >= self._homing_high_point

    @property
    def buffer_trailing_triggered(self) -> bool:
        """
        True when FPS reading indicates the buffer is stretched (at low_point).

        This is the FPS equivalent of the turtleneck trailing switch being
        triggered — indicates the spool is not feeding fast enough or is stuck.
        """
        return self.smoothed_fps <= self.low_point

    # ------------------------------------------------------------------
    # ADC callback — runs at report_time intervals
    # ------------------------------------------------------------------
    def _adc_callback(self, read_time: Union[float, list],
                      read_value: Optional[float] = None) -> None:
        """
        Process ADC reading from FPS sensor.
        """
        if isinstance(read_time, list):
            if not read_time:
                return
            read_time, read_value = read_time[-1]
        elif read_value is None:
            read_value = read_time
            read_time = self.reactor.monotonic()

        if self.reversed:
            read_value = 1.0 - read_value

        self.fps_value = read_value

        # Apply exponential moving average
        self.smoothed_fps = (
            self.smoothing * self.smoothed_fps
            + (1.0 - self.smoothing) * read_value
        )

        # Keep last_state/advance/trailing current even when the correction
        # loop isn't running, so load/calibration can still detect filament arrival.
        has_stepper: bool = self._lane_has_rotation_control(self.current_lane)

        # If buffer was enabled before lane stepper wiring was ready,
        # lazily start correction once the lane exposes rotation control.
        if (self.enable and has_stepper
            and not getattr(self, "_correction_running", False)):
            self.reactor.update_timer(self.correction_timer, self.reactor.NOW)
            self._correction_running = True
        correction_active = (self.enable and has_stepper
                             and getattr(self, "_correction_running", False))
        if (not correction_active):
            half_db = self.deadband / 2.0
            if self.smoothed_fps > self.set_point + half_db:
                self.last_state = TRAILING_STATE_NAME
                self.advance_state = True
                if self._latch_enabled:
                    self._advance_latched = True
                self.trailing_state = False
            elif self._advance_latched:
                # Latched during load: keep advance_state True even if
                # pressure drops briefly between motor pulses.
                self.advance_state = True
            elif self.smoothed_fps < self.set_point - half_db:
                self.last_state = ADVANCING_STATE_NAME
                self.advance_state = False
                self.trailing_state = True
            else:
                self.last_state = NEUTRAL_STATE_NAME
                self.advance_state = False
                self.trailing_state = False
        # Update virtual filament sensors for GUI display
        self._update_virtual_sensors(read_time)

    # ------------------------------------------------------------------
    # Correction timer — proportional adjustment loop
    # ------------------------------------------------------------------
    def _update_virtual_sensors(self, eventtime: float) -> None:
        """
        Push buffer state into virtual filament sensors for GUI display.

        The advance sensor reports filament present whenever the advance state is set.
        The trailing sensor reports filament present whenever the trailing state is set.
        """
        try:
            if (hasattr(self, 'fila_adv')
                and self.fila_adv is not None):
                self.fila_adv.runout_helper.note_filament_present(
                    eventtime, self.advance_state)
        except TypeError:
            self.fila_adv.runout_helper.note_filament_present(self.advance_state)
        except Exception:
            pass
        try:
            if (hasattr(self, 'fila_trail')
                and self.fila_trail is not None):
                self.fila_trail.runout_helper.note_filament_present(
                    eventtime, self.trailing_state)
        except TypeError:
            self.fila_trail.runout_helper.note_filament_present(self.trailing_state)
        except Exception:
            pass

    def _correction_event(self, eventtime: float) -> float:
        """
        Periodically adjust rotation distance based on FPS reading.

        Uses continuous proportional correction across the full range.
        The further from set_point, the stronger the correction — no dead
        zone where the buffer can sit uncorrected.
        """
        if not self.enable or self.current_lane is None:
            self._correction_running = False
            return self.reactor.NEVER
        # Non-stepper lanes (ACE/OpenAMS) don't need rotation correction
        if not self._lane_has_rotation_control(self.current_lane):
            self._correction_running = False
            return self.reactor.NEVER

        reading = self.smoothed_fps
        deviation = reading - self.set_point  # positive = high/trailing, negative = low/advancing
        half_db = self.deadband / 2.0

        # Proportional multiplier across the full range: slow down above
        # set_point, speed up below it, neutral (1.0) right at set_point.
        if deviation > 0:
            range_size = max(self.high_point - self.set_point, 0.01)
            fraction = min(deviation / range_size, 1.0)
            multiplier = 1.0 - fraction * (1.0 - self.multiplier_low)
        elif deviation < 0:
            range_size = max(self.set_point - self.low_point, 0.01)
            fraction = min(-deviation / range_size, 1.0)
            multiplier = 1.0 + fraction * (self.multiplier_high - 1.0)
            trailing_floor = min(self.trailing_min_multiplier, self.multiplier_high)
            multiplier = max(multiplier, trailing_floor)
        else:
            multiplier = 1.0

        # Determine state for LED indication and fault reporting
        if reading < self.set_point - half_db:
            target_direction = ADVANCING_STATE_NAME
        elif reading > self.set_point + half_db:
            target_direction = TRAILING_STATE_NAME
        else:
            target_direction = NEUTRAL_STATE_NAME

        integral = (self._integral_terms.get(self.current_lane.name, 0.0)
                    if self.current_lane else 0.0)
        if (self.enable_integral_correction
            and self.current_lane is not None):
            lane_name = self.current_lane.name
            if self._is_extruding():
                integral -= self.integral_gain * deviation * self.update_interval
                # Anti-windup: clamp so a stuck sensor or real clog can't wind it up forever.
                integral = max(self.multiplier_low - 1.0,
                                min(self.multiplier_high - 1.0, integral))
                self._integral_terms[lane_name] = integral
            multiplier += integral
            # Clamp the combined P+I output to the configured band.
            multiplier = max(self.multiplier_low, min(self.multiplier_high, multiplier))


        # Apply multiplier, gated by hysteresis when set so tiny, insignificant
        # corrections don't trigger an update_rotation_distance call on
        # every tick.
        log_event = False
        last_applied = self._last_multiplier
        if self.multiplier_hysteresis <= 0.0:
            self.set_multiplier(multiplier)
            if abs(last_applied - multiplier) > 0.001:
                log_event = True
            if integral != self._last_logged_integral:
                log_event = True
        else:
            if abs(multiplier - last_applied) > self.multiplier_hysteresis:
                self.set_multiplier(multiplier)
                log_event = True

        # Update state flags
        if target_direction == ADVANCING_STATE_NAME:
            if self.last_state != ADVANCING_STATE_NAME:
                log_event = True
                if self.led:
                    self.afc.function.afc_led(self.led_advancing, self.led_index)
            self.last_state = ADVANCING_STATE_NAME
            self.advance_state = False
            self.trailing_state = True
        elif target_direction == TRAILING_STATE_NAME:
            if self.last_state != TRAILING_STATE_NAME:
                log_event = True
                if self.led:
                    self.afc.function.afc_led(self.led_trailing, self.led_index)
            self.last_state = TRAILING_STATE_NAME
            self.advance_state = True
            self.trailing_state = False
        else:
            if self.last_state != NEUTRAL_STATE_NAME:
                log_event = True
                if self.led:
                    self.afc.function.afc_led(self.led_neutral, self.led_index)
            self.last_state = NEUTRAL_STATE_NAME
            self.advance_state = False
            self.trailing_state = False

        if (log_event
            and self.debug):
            self.logger.debug(
                f"FPS_buffer {self.name}: fps={self.fps_value:.3f} smoothed={self.smoothed_fps:.3f} "
                f"integral={integral:+.4f} multiplier={self._last_multiplier:.4f} state={self.last_state}"
            )
            self._last_logged_integral = integral

        # Update error position while correction is active; stop only when
        # stuck at an extreme despite correction (real clog/feed failure).
        if self.fault_detection_enabled():
            if (reading < self.high_point
                and reading > self.low_point):
                self.update_filament_error_pos()

        self._update_virtual_sensors(eventtime)
        return eventtime + self.update_interval

    # ------------------------------------------------------------------
    # Buffer enable / disable  (interface expected by AFCLane)
    # ------------------------------------------------------------------
    def enable_advance_latch(self) -> None:
        """
        Enable latching so advance_state stays True once triggered.

        Call before a load sequence. The latch prevents brief pressure
        drops between motor pulses from clearing the sensor state.
        """
        self._latch_enabled = True
        self._advance_latched = False

    def clear_advance_latch(self) -> None:
        """
        Disable latching and reset advance_state to real-time pressure.
        """
        self._latch_enabled = False
        self._advance_latched = False

    def enable_buffer(self, lane: AFCLane) -> None:
        """
        Enable the FPS buffer for the given lane.

        For stepper-based units (BoxTurtle, etc.) this also starts the
        proportional correction timer that adjusts rotation distance.
        For non-stepper units (OpenAMS, ACE) the correction loop is skipped
        but the buffer is still marked as enabled/active.
        """
        self.current_lane = lane
        self.enable = True
        self._latch_enabled = False
        self._advance_latched = False
        has_stepper = self._lane_has_rotation_control(lane)

        if self.led:
            self.afc.function.afc_led(self.led_neutral, self.led_index)

        # Reset smoothed value to current reading
        self.smoothed_fps = self.fps_value

        if has_stepper:
            # Restore the last multiplier if the same lane is coming back;
            # a different lane on this extruder means a new spool, start at 1.0.
            extruder_name = getattr(getattr(lane, 'extruder_obj', None),
                                    'th_extruder_name', None)
            multiplier_key = (extruder_name, lane.name)
            saved = self._saved_multipliers.get(multiplier_key)
            if (saved is not None
                and saved[0] == lane.name
                and saved[1] != 1.0):
                self.set_multiplier(saved[1])
                self.logger.debug(
                    f"{self.name} restored multiplier {saved[1]:.4f} "
                    f"for {lane.name} on {extruder_name}"
                )
            else:
                self._last_multiplier = 1.0

            # Start the proportional correction timer
            self.reactor.update_timer(self.correction_timer, self.reactor.NOW)
            self._correction_running = True

            # Only for stepper lanes -- non-stepper lanes have no correction
            # timer to keep extruder position in sync. OpenAMS uses its own OAMSMonitor.
            if self.fault_detection_enabled():
                self.start_fault_detection(0, 1.0)
        else:
            self._correction_running = False

        self.logger.debug((f"{self.name} FPS buffer enabled for {self.current_lane.name} "
                           f"(correction={'active' if has_stepper else 'off/adc-only'})"))

    def disable_buffer(self) -> None:
        """
        Disable the FPS buffer, reset multiplier, stop timers.
        """
        self.enable = False
        self._latch_enabled = False
        self._advance_latched = False
        if self.current_lane is None:
            return

        self.logger.debug(f"{self.name} {self.type} buffer disabled for {self.current_lane.name}")

        if self.led:
            self.afc.function.afc_led(self.led_buffer_disabled, self.led_index)

        # Save the last multiplier for this extruder/lane so it can be
        # restored on the next tool change back to this same lane.
        if self._lane_has_rotation_control(self.current_lane):
            extruder_name = getattr(getattr(self.current_lane, 'extruder_obj', None),
                                    'th_extruder_name', None)
            if extruder_name:
                self._saved_multipliers[(extruder_name, self.current_lane.name)] = (
                    self.current_lane.name, self._last_multiplier
                )
                self.logger.debug(
                    f"{self.name} saved multiplier {self._last_multiplier:.4f} "
                    f"for {self.current_lane.name} on {extruder_name}"
                )

        self.reset_multiplier()

        # Stop correction timer
        self.reactor.update_timer(self.correction_timer, self.reactor.NEVER)
        self._correction_running = False

        # Stop fault detection
        if self.error_sensitivity > 0 and self.extruder_pos_timer is not None:
            eventtime = self.reactor.monotonic()
            self.stop_fault_timer(eventtime)

        self.current_lane = None

    # ------------------------------------------------------------------
    # Multiplier control  (same interface as AFCBuffer)
    # ------------------------------------------------------------------
    def set_multiplier(self, multiplier: float) -> None:
        """
        Apply rotation distance multiplier to current lane's stepper.
        """
        if not self.enable: return
        if self.current_lane is None: return
        if not self._lane_has_rotation_control(self.current_lane): return

        self._last_multiplier = multiplier
        self.current_lane.update_rotation_distance(multiplier)

    def _is_extruding(self) -> bool:
        """
        Checks if lanes extruder has actually moved in either direction.

        :return boolean: Return True when the extruder has actually moved (either direction)
                         by more than integral_extrusion_threshold since the last correction
                         tick.
        """
        try:
            current_pos = self.afc.function.get_extruder_pos()
        except Exception:
            return False

        if current_pos is None:
            return False

        previous = self._integral_last_extruder_pos
        self._integral_last_extruder_pos = current_pos
        if previous is None:
            return False  # first sample this session -- no delta to compare yet

        result = abs(current_pos - previous) >= self.integral_extrusion_threshold

        return result

    def reset_multiplier(self) -> None:
        """
        Reset rotation distance back to base value.
        """
        if self.current_lane is None: return
        if not self._lane_has_rotation_control(self.current_lane): return

        self._last_multiplier = 1.0
        self.current_lane.update_rotation_distance(1)
        self.logger.debug("FPS buffer multiplier reset for {}".format(self.current_lane.name))

    def _lane_has_rotation_control(self, lane: Optional[AFCLane]=None) -> bool:
        """
        Return True when lane supports AFC stepper rotation adjustments.
        """
        if lane is None: return False

        drive_stepper = getattr(lane, 'drive_stepper', None)
        extruder_stepper = getattr(lane, 'extruder_stepper', None)
        update_fn = getattr(lane, 'update_rotation_distance', None)
        has_stepper = (drive_stepper is not None) or (extruder_stepper is not None)
        return has_stepper and callable(update_fn)

    def extruder_pos_update_event(self, eventtime: float) -> float:
        """
        Reactor timer callback that watches for filament feed faults.

        Skips non-stepper lanes and lanes on a different active extruder. While
        printing, keeps advancing the trip position when the FPS reading is in
        the correctable range, and raises an error if the extruder advances past
        the trip position while the reading is stuck at an extreme.

        :param eventtime: Reactor event time of the check.
        :return: The reactor time at which the timer should next fire.
        """
        cur_lane = self.current_lane

        if cur_lane is not None:
            # This may be needed to be updated for IDEX machines
            active_extruder = self.afc.toolhead.get_extruder()
            lane_extruder_name = getattr(getattr(cur_lane, 'extruder_obj', None),
                                         'th_extruder_name', None)
            if (lane_extruder_name
                and hasattr(active_extruder, 'name')
                and active_extruder.name != lane_extruder_name):
                return eventtime + CHECK_RUNOUT_TIMEOUT

        extruder_pos = self.get_extruder_pos()
        if (self.afc.function.is_printing(check_movement=True)
            and extruder_pos is not None
            and self.filament_error_pos is not None):
            # Keep updating while FPS is in the correctable range; only let
            # it expire when stuck at an extreme.
            if self.low_point <= self.smoothed_fps <= self.high_point:
                self.update_filament_error_pos()
                return eventtime + CHECK_RUNOUT_TIMEOUT
            if extruder_pos > self.filament_error_pos:
                msg = "AFC FPS buffer filament fault detected! Take necessary action."
                self.pause_on_error(msg, True)
                self.update_filament_error_pos()

        return eventtime + CHECK_RUNOUT_TIMEOUT

    # ------------------------------------------------------------------
    # G-code commands
    # ------------------------------------------------------------------
    cmd_QUERY_BUFFER_help = "Report FPS buffer sensor state"
    cmd_QUERY_BUFFER_options = {"BUFFER": {"type": "string", "default": ""}}
    def cmd_QUERY_BUFFER(self, gcmd: GCodeCommand) -> None:
        """
        Reports the current state of the FPS buffer sensor including the
        current FPS reading, smoothed value, and rotation distance.

        Usage
        -----
        `QUERY_BUFFER BUFFER=<buffer_name>`

        Example
        -----
        `QUERY_BUFFER BUFFER=FPS_Buffer1`
        """
        state_mapping = {
            ADVANCING_STATE_NAME: ' (buffer is compressed - increasing feed)',
            TRAILING_STATE_NAME: ' (buffer is expanded - reducing feed)',
            NEUTRAL_STATE_NAME: ' (buffer is centered)',
        }

        buffer_status = self.buffer_status()
        state_info = "{}{}".format(buffer_status, state_mapping.get(buffer_status, ''))
        state_info += "\n{} raw: {:.3f}  smoothed: {:.3f}  set_point: {:.2f}".format(
            self.type, self.fps_value, self.smoothed_fps, self.set_point
        )

        if self.enable and self.current_lane is not None:
            if self.current_lane.extruder_stepper is not None:
                stepper = self.current_lane.extruder_stepper.stepper
                rotation_dist = stepper.get_rotation_distance()[0]
                state_info += "\n{} Rotation distance: {:.4f}".format(
                    self.current_lane.name, rotation_dist
                )

        if self.error_sensitivity > 0:
            state_info += "\nFault detection enabled, sensitivity {}".format(
                self.error_sensitivity
            )

        self.logger.info("{} : {}".format(self.name, state_info))

    cmd_AFC_SET_FPS_SET_POINT_help = "Live adjust FPS buffer set point target"
    def cmd_AFC_SET_FPS_SET_POINT(self, gcmd: GCodeCommand) -> None:
        """
        Adjust the FPS target set point and deadband while running.

        Usage
        -----
        `AFC_SET_FPS_SET_POINT BUFFER=<name> SET_POINT=<0.1-0.9> [DEADBAND=<0.0-0.6>]`

        Example
        -----
        `AFC_SET_FPS_SET_POINT BUFFER=FPS_Buffer1 SET_POINT=0.1 DEADBAND=0.5`
        """
        new_set_point = gcmd.get_float('SET_POINT', self.set_point, minval=0.1, maxval=0.9)
        new_deadband = gcmd.get_float('DEADBAND', self.deadband, minval=0.0, maxval=0.6)
        half_db = new_deadband / 2.0

        if new_set_point <= self.low_point or new_set_point >= self.high_point:
            error_msg = (f"SET_POINT must be between low_point({self.low_point}) "
                         f"and high_point({self.high_point})")
            raise gcmd.error(error_msg)

        error_msg = self._check_deadband(new_set_point, new_deadband)
        if error_msg:
            raise gcmd.error(error_msg)

        self.set_point = new_set_point
        self.deadband = new_deadband
        self.logger.info("FPS set_point={:.2f} deadband={:.2f} (neutral window {:.2f}-{:.2f})".format(
            self.set_point, self.deadband,
            self.set_point - half_db, self.set_point + half_db
        ))

    def get_status(self, eventtime: Optional[float] = None) -> dict:
        """
        Return a status dict describing the buffer for the GUI/API.

        :param eventtime: Reactor event time (unused).
        :return: Dict with state, lanes, enabled flag, raw/smoothed FPS values,
                 set point, active lane, rotation distance, and fault-detection
                 details.
        """
        response = {}

        response = super().get_status(eventtime)

        response['fps_value'] = round(self.fps_value, 3)
        response['smoothed_fps'] = round(self.smoothed_fps, 3)
        response['set_point'] = self.set_point

        return response

def load_config_prefix(config):
    buffer_type = config.get("type", "switched")
    if buffer_type == "switched":
        return AFCBuffer(config)

    if buffer_type == "FPS_PSF":
        return AFCFPSBuffer(config)

    msg = f"{buffer_type} not valid, only switched(turtleneck style) or FPS_PSF are valid options"
    raise error(msg)