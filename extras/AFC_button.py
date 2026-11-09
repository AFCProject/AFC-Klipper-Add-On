# AFCProject Automated Filament Changer Software
#
# Copyright (C) 2024-2025 Armored Turtle
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback

from configfile import error
from extras.gcode_button import GCodeButton
from typing import Any, TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from extras.AFC import afc
    from extras.AFC_lane import AFCLane
    from extras.query_adc import QueryADC

class AFCButton(GCodeButton):
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
        press_template = config.get('press_gcode', None)
        if press_template is None:
            config.fileconfig.set(config.section, 'press_gcode', '')

        super().__init__(config)
        self.fullname = config.get_name()
        # self.printer: Printer = config.get_printer()
        # self.gcode: GCodeDispatch = self.printer.load_object(config, 'gcode')
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.reactor = self.printer.get_reactor()

        self.afc: afc = self.printer.load_object(config, 'AFC')
        self.lane_id = config.get_name().split()[-1]
        self._lane_obj: Optional[AFCLane] = None
        self.long_press_duration = config.getfloat('long_press_duration', 1.2)

        # Internal state for press timing
        self._press_time: Optional[float] = None

        # analog_range presence is also what super().__init__ used to decide whether
        # to register this pin as a debounced ADC button instead of a digital one.
        self.is_adc_button: bool = config.get("analog_range", None) is not None
        self.analog_min: float = 0.0
        self.analog_max: float = 0.0
        self.pullup: float = 4700.0
        if self.is_adc_button:
            self.analog_min, self.analog_max = config.getfloatlist("analog_range", count=2)
            self.pullup = config.getfloat("analog_pullup_resistor", 4700.0, above=0.0)

        # Custom short_press_gcode/long_press_gcode take priority over the
        # embedded load/unload/eject logic below when configured, the same
        # way press_gcode overrides nothing being run in gcode_button.py.
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.short_press_template: Any = gcode_macro.load_template(config, 'short_press_gcode', '')
        self.long_press_template: Any = gcode_macro.load_template(config, 'long_press_gcode', '')

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

    def get_resistance(self) -> Optional[float]:
        """
        Read this button's current ADC resistance, calculated the same way
        QUERY_ADC reports it (pin voltage ratio combined with the configured
        pullup resistor). Used by button calibration to find press/release
        thresholds.

        :return Optional[float]: resistance in ohms, or None if this isn't an
            ADC button or no sample has been read yet
        """
        if not self.is_adc_button:
            return None
        query_adc: QueryADC = self.printer.lookup_object("query_adc")
        mcu_adc = query_adc.adc.get(f"adc_button:{self.pin.strip()}")
        if mcu_adc is None:
            return None
        # get_last_value() returns (value, timestamp) on this project's Klipper
        # forks, but (timestamp, value) on mainline. value is always a 0-1 ADC
        # ratio and timestamp is always well above 1 once the printer has been
        # running a moment, so pick whichever of the two is in range instead
        # of assuming a fixed order.
        first, second = mcu_adc.get_last_value()
        value = first if 0.0 <= first <= 1.0 else second
        value = max(0.00001, min(0.99999, value))
        return self.pullup * value / (1.0 - value)

    def button_callback(self, eventtime: float, state: bool) -> None:
        """
        Callback function for button press events. Tracks press duration and
        executes short or long press actions based on the duration.

        Suppressed entirely while ADC button calibration is running, since
        BUTTON_CALIBRATION is reading presses off every button itself and a
        lane load/unload firing mid-calibration would fight it.

        :param eventtime: time of the event
        :param state: state of the button, True for press, False for release
        """
        if self.afc.function.button_calibration_active:
            self._press_time = None
            return
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
            if self._run_custom_press_gcode(self.long_press_template):
                return
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
            if self._run_custom_press_gcode(self.short_press_template):
                return
            self.afc.logger.info(f"{self.lane_id}: Short press detected.")
            if (cur_lane is not None
                and cur_lane.name == self.lane_id):
                self.afc.logger.info(f"Unloading tool from {self.lane_id}.")
                if not self.afc.TOOL_UNLOAD(cur_lane):
                    self.afc.afc_stats.increase_unload_error_count(self.afc)
            else:
                self.afc.logger.info(f"Loading tool to {self.lane_id}.")
                self.afc.CHANGE_TOOL(self.lane_obj)

    def _run_custom_press_gcode(self, template: Any) -> bool:
        """
        Render and run a custom short_press_gcode/long_press_gcode override,
        the same way GCodeButton.button_callback runs press_gcode: render,
        then run it via self.gcode.run_script. A failure is reported through
        AFC_error, naming the lane and the failing macro, with the exception
        itself going only to the debug log.

        :param template: loaded short_press_template or long_press_template
        :return bool: True when the override had non-empty gcode configured
            (ran, or failed trying), so the embedded press logic should be
            skipped; False when nothing was configured
        """
        try:
            commands = template.render()
            if not commands.strip():
                return False
            self.gcode.run_script(commands)
        except Exception:
            error_str = f"{self.lane_id}: error running {template.name}"
            self.afc.logger.debug(error_str, only_debug=True, traceback=traceback.format_exc())
            self.afc.error.AFC_error(error_str, pause=False)
        return True


def load_config_prefix(config: ConfigWrapper) -> AFCButton:
    """
    Klipper config entry point for the AFC_button module.

    :param config: Klipper config wrapper for the AFC_button section
    :return type: AFCButton instance to register with the printer
    """
    return AFCButton(config)
