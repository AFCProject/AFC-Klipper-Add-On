# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2025 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from configfile import error

class AFCButton:
    """
    This class is used for lane based button controls.
    """
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.reactor = self.printer.get_reactor()

        self.afc = self.printer.lookup_object('AFC')
        self.lane_id = config.get_name().split()[-1]
        self.lane_obj = None
        self.long_press_duration = config.getfloat('long_press_duration', 1.2)
        pin_name = config.get('pin')

        # Internal state for press timing
        self._press_time = None

        # Register the button callback
        buttons = self.printer.load_object(config, 'buttons')
        buttons.register_buttons([pin_name], self._button_callback)

        self.afc.logger.info(f"AFC_button for {self.lane_id} initialized on pin: {pin_name}")

    def _handle_ready(self):
        """
        Handle ready callback check to make sure lane is found within AFC lanes, if lanes
        is not found and error is raised.
        """
        self.lane_obj = self.afc.lanes.get(self.lane_id)
        if not self.lane_obj:
            raise error(f"Lane {self.lane_id} is not defined/found in your configuration file. Please define lane or verify lane name is correct.")

    def _button_callback(self, eventtime, state):
        """
        Callback function for button press events.

        Args:
            eventtime: The time of the event.
            state: The state of the button (True for press, False for release).

        Behavior:
            - Tracks press duration.
            - Executes short or long press actions based on the duration.
        """
        if state:
            self._press_time = eventtime
            return
        if self._press_time is None:
            return

        if self.afc.function.is_printing(check_movement=True):
            self.afc.error.AFC_error("Cannot use buttons while printer is actively moving or homing", False)
            return

        held_time = eventtime - self._press_time
        self._press_time = None

        if held_time < 0.05:
            return

        cur_lane = self.afc.function.get_current_lane_obj()

        # Long Press
        if held_time >= self.long_press_duration:
            self.afc.logger.info(f"{self.lane_id}: Long press detected.")
            if cur_lane is not None and cur_lane.name == self.lane_id:
                self.afc.logger.info(f"Unloading {self.lane_id} before ejecting.")
                if self.afc.TOOL_UNLOAD(self.lane_obj):
                    self.afc.LANE_UNLOAD(self.lane_obj)
            else:
                # If another lane is active, just eject this one
                self.afc.logger.info(f"Ejecting {self.lane_id}.")
                self.afc.LANE_UNLOAD(self.lane_obj)
        # Short Press
        else:
            self.afc.logger.info(f"{self.lane_id}: Short press detected.")
            if cur_lane is not None and cur_lane.name == self.lane_id:
                self.afc.logger.info(f"Unloading tool from {self.lane_id}.")
                self.afc.TOOL_UNLOAD(cur_lane)
            else:
                self.afc.logger.info(f"Loading tool to {self.lane_id}.")
                self.afc.CHANGE_TOOL(self.lane_obj)


def load_config_prefix(config):
    return AFCButton(config)