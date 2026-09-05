# AFCProject Automated Filament Changer Software
#
# Copyright (C) 2025 Armored Turtle
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

from configfile import error
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from klippy import Printer
    from gcode import GCodeDispatch
    from extras.AFC import afc
    from extras.AFC_lane import AFCLane
    from extras.buttons import PrinterButtons

class AFCButton:
    """
    This class is used for lane based button controls.
    """
    def __init__(self, config: ConfigWrapper) -> None:
        """
        Load button settings from the config, register the klippy:ready
        handler used to resolve this button's lane, and wire the physical
        button pin up to the press/release callback.

        :param config: Klipper config wrapper for the AFC_button section
        """
        self.printer: Printer = config.get_printer()
        self.gcode: GCodeDispatch = self.printer.load_object(config, 'gcode')
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.reactor = self.printer.get_reactor()

        self.afc: afc = self.printer.load_object(config, 'AFC')
        self.lane_id = config.get_name().split()[-1]
        self._lane_obj: Optional[AFCLane] = None
        self.long_press_duration = config.getfloat('long_press_duration', 1.2)
        pin_name = config.get('pin')

        # Internal state for press timing
        self._press_time: Optional[float] = None

        # Register the button callback
        buttons: PrinterButtons = self.printer.load_object(config, 'buttons')
        buttons.register_buttons([pin_name], self._button_callback)

        self.afc.logger.info(f"AFC_button for {self.lane_id} initialized on pin: {pin_name}")

    @property
    def lane_obj(self) -> AFCLane:
        """
        This button's lane, resolved once klippy:ready fires. _handle_ready
        always either sets this or raises, so by the time anything but
        _handle_ready itself can run, it's guaranteed to be set.

        :return type: resolved lane object for this button
        """
        if self._lane_obj is None:
            error_str = f"Lane {self.lane_id} has not been resolved yet"
            raise error(error_str)
        return self._lane_obj

    def _handle_ready(self) -> None:
        """
        Handle ready callback check to make sure lane is found within AFC lanes, if lanes
        is not found and error is raised.
        """
        self._lane_obj = self.afc.lanes.get(self.lane_id)
        if not self._lane_obj:
            error_str = (
                f"Lane {self.lane_id} is not defined/found in your configuration file. "
                "Please define lane or verify lane name is correct."
            )
            raise error(error_str)

    def _button_callback(self, eventtime: float, state: bool) -> None:
        """
        Callback function for button press events. Tracks press duration and
        executes short or long press actions based on the duration.

        :param eventtime: time of the event
        :param state: state of the button, True for press, False for release
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
            if (cur_lane is not None
                and cur_lane.name == self.lane_id):
                self.afc.logger.info(f"Unloading {self.lane_id} before ejecting.")
                if self.afc.TOOL_UNLOAD(self.lane_obj):
                    self.afc.LANE_UNLOAD(self.lane_obj)
                else:
                    self.afc.afc_stats.increase_unload_error_count(self.afc)
            else:
                # If another lane is active, just eject this one
                self.afc.logger.info(f"Ejecting {self.lane_id}.")
                self.afc.LANE_UNLOAD(self.lane_obj)
        # Short Press
        else:
            self.afc.logger.info(f"{self.lane_id}: Short press detected.")
            if (cur_lane is not None
                and cur_lane.name == self.lane_id):
                self.afc.logger.info(f"Unloading tool from {self.lane_id}.")
                if not self.afc.TOOL_UNLOAD(cur_lane):
                    self.afc.afc_stats.increase_unload_error_count(self.afc)
            else:
                self.afc.logger.info(f"Loading tool to {self.lane_id}.")
                self.afc.CHANGE_TOOL(self.lane_obj)


def load_config_prefix(config: ConfigWrapper) -> AFCButton:
    """
    Klipper config entry point for the AFC_button module.

    :param config: Klipper config wrapper for the AFC_button section
    :return type: AFCButton instance to register with the printer
    """
    return AFCButton(config)
