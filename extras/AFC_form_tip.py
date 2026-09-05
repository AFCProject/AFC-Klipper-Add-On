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
    from gcode import GCodeCommand, GCodeDispatch
    from extras.AFC import afc
    from extras.AFC_logger import AFC_logger
    from extras.heaters import PrinterHeaters

class afc_tip_form:
    def __init__(self, config: ConfigWrapper) -> None:
        """
        Load tip forming settings from the config and register the tip
        forming gcode commands.

        :param config: Klipper config wrapper for the AFC_form_tip section
        """
        self.printer        = config.get_printer()
        self.reactor        = self.printer.get_reactor()
        self.afc: afc       = self.printer.load_object(config, 'AFC')
        self.gcode: GCodeDispatch = self.printer.load_object(config, 'gcode')
        self.logger: AFC_logger = self.afc.logger

         # TIP FORMING
        self.ramming_volume         = config.getfloat("ramming_volume", 0)
        self.toolchange_temp        = config.getfloat("toolchange_temp", 0)
        self.unloading_speed_start  = config.getfloat("unloading_speed_start", 80)
        self.unloading_speed        = config.getfloat("unloading_speed", 18)
        self.cooling_tube_position  = config.getfloat("cooling_tube_position", 35)
        self.cooling_tube_length    = config.getfloat("cooling_tube_length", 10)
        self.initial_cooling_speed  = config.getfloat("initial_cooling_speed", 10)
        self.final_cooling_speed    = config.getfloat("final_cooling_speed", 50)
        self.cooling_moves          = config.getint("cooling_moves", 4)
        self.use_skinnydip          = config.getboolean("use_skinnydip", False)
        self.skinnydip_distance     = config.getfloat("skinnydip_distance", 4)
        self.dip_insertion_speed    = config.getfloat("dip_insertion_speed", 4)
        self.dip_extraction_speed   = config.getfloat("dip_extraction_speed", 4)
        self.melt_zone_pause        = config.getfloat("melt_zone_pause", 4)
        self.cooling_zone_pause     = config.getfloat("cooling_zone_pause", 4)
        self.gcode.register_command("TEST_AFC_TIP_FORMING", self.cmd_TEST_AFC_TIP_FORMING, desc=self.cmd_TEST_AFC_TIP_FORMING_help)
        self.gcode.register_command("GET_TIP_FORMING", self.cmd_GET_TIP_FORMING, desc=self.cmd_GET_TIP_FORMING_help)
        self.gcode.register_command("SET_TIP_FORMING", self.cmd_SET_TIP_FORMING, desc=self.cmd_SET_TIP_FORMING_help)


    def afc_extrude(self, distance: float, speed: float) -> None:
        """
        Extrude/retract filament at the extruder by the given distance and speed.

        :param distance: Distance in mm to move, negative retracts
        :param speed: Speed in mm/s to move at
        """
        self.afc.move_e_pos( distance, speed, "form tip")

    cmd_TEST_AFC_TIP_FORMING_help = "Gives ability to test AFC tip forming without doing a tool change"
    def cmd_TEST_AFC_TIP_FORMING(self, gcmd: GCodeCommand) -> None:
        """
        Gives ability to test AFC tip forming without doing a tool change.

        Usage
        -----
        `TEST_AFC_TIP_FORMING`

        Example
        -----
        ```
        TEST_AFC_TIP_FORMING
        ```
        """
        self.tip_form()


    cmd_GET_TIP_FORMING_help = "Shows the tip forming configuration"
    def cmd_GET_TIP_FORMING(self, gcmd: GCodeCommand) -> None:
        """
        Shows the tip forming configuration

        Usage
        -----
        `GET_TIP_FORMING`

        Example
        -----
        ```
        GET_TIP_FORMING
        ```
        """
        status_msg = "Tip Forming Configuration:\n"
        status_msg += f"ramming_volume:        {self.ramming_volume}\n"
        status_msg += f"toolchange_temp:       {self.toolchange_temp}\n"
        status_msg += f"unloading_speed_start: {self.unloading_speed_start}\n"
        status_msg += f"unloading_speed:       {self.unloading_speed}\n"
        status_msg += f"cooling_tube_position: {self.cooling_tube_position}\n"
        status_msg += f"cooling_tube_length:   {self.cooling_tube_length}\n"
        status_msg += f"initial_cooling_speed: {self.initial_cooling_speed}\n"
        status_msg += f"final_cooling_speed:   {self.final_cooling_speed}\n"
        status_msg += f"cooling_moves:         {self.cooling_moves}\n"
        status_msg += f"use_skinnydip:         {self.use_skinnydip}\n"
        status_msg += f"skinnydip_distance:    {self.skinnydip_distance}\n"
        status_msg += f"dip_insertion_speed:   {self.dip_insertion_speed}\n"
        status_msg += f"dip_extraction_speed:  {self.dip_extraction_speed}\n"
        status_msg += f"melt_zone_pause:       {self.melt_zone_pause}\n"
        status_msg += f"cooling_zone_pause:    {self.cooling_zone_pause}\n"

        self.logger.raw(status_msg)


    cmd_SET_TIP_FORMING_help = "Sets tip forming configuration"
    def cmd_SET_TIP_FORMING(self, gcmd: GCodeCommand) -> None:
        """
        Sets the tip forming configuration.

        Unspecified ones are left unchanged. True boolean values (use_skinnydip) are specified as "true"
        (case-insensitive); every other value is considered as "false".

        Note: this will not update the configuration file. To make settings permanent, update the configuration file
        manually.

        Usage
        -----
        `SET_TIP_FORMING PARAMETER=VALUE ...`

        Example
        -----
        ```
        SET_TIP_FORMING ramming_volume=20 toolchange_temp=220
        ```
        """

        self.ramming_volume = gcmd.get_float("RAMMING_VOLUME", self.ramming_volume)
        self.toolchange_temp = gcmd.get_float("TOOLCHANGE_TEMP", self.toolchange_temp)
        self.unloading_speed_start = gcmd.get_float("UNLOADING_SPEED_START", self.unloading_speed_start)
        self.unloading_speed = gcmd.get_float("UNLOADING_SPEED", self.unloading_speed)
        self.cooling_tube_position = gcmd.get_float("COOLING_TUBE_POSITION", self.cooling_tube_position)
        self.cooling_tube_length = gcmd.get_float("COOLING_TUBE_LENGTH", self.cooling_tube_length)
        self.initial_cooling_speed = gcmd.get_float("INITIAL_COOLING_SPEED", self.initial_cooling_speed)
        self.final_cooling_speed = gcmd.get_float("FINAL_COOLING_SPEED", self.final_cooling_speed)
        self.cooling_moves = gcmd.get_int("COOLING_MOVES", self.cooling_moves)
        self.use_skinnydip = gcmd.get("USE_SKINNYDIP", str(self.use_skinnydip)).lower() == "true"
        self.skinnydip_distance = gcmd.get_float("SKINNYDIP_DISTANCE", self.skinnydip_distance)
        self.dip_insertion_speed = gcmd.get_float("DIP_INSERTION_SPEED", self.dip_insertion_speed)
        self.dip_extraction_speed = gcmd.get_float("DIP_EXTRACTION_SPEED", self.dip_extraction_speed)
        self.melt_zone_pause = gcmd.get_float("MELT_ZONE_PAUSE", self.melt_zone_pause)
        self.cooling_zone_pause = gcmd.get_float("COOLING_ZONE_PAUSE", self.cooling_zone_pause)


    def tip_form(self) -> None:
        """
        Runs the full tip forming sequence: ramming, retraction/nozzle separation,
        cooling moves, and an optional skinny dip, waiting on the toolchange
        temperature and restoring the original extruder temperature when done.
        """
        step = 1
        extruder = self.afc.toolhead.get_extruder()
        pheaters: PrinterHeaters = self.printer.lookup_object('heaters')
        current_temp = extruder.get_heater().target_temp     # Saving current temp so it can be set back when done if toolchange_temp is not zero
        if self.ramming_volume > 0:
            self.logger.info(f'AFC-TIP-FORM: Step {step}: Ramming')
            ratio = self.ramming_volume / 23
            self.afc_extrude(0.5784 * ratio, 299 / 60)
            self.afc_extrude(0.5834 * ratio, 302 / 60)
            self.afc_extrude(0.5918 * ratio, 306 / 60)
            self.afc_extrude(0.6169 * ratio, 319 / 60)
            self.afc_extrude(0.3393 * ratio, 350 / 60)
            self.afc_extrude(0.3363 * ratio, 350 / 60)
            self.afc_extrude(0.7577 * ratio, 392 / 60)
            self.afc_extrude(0.8382 * ratio, 434 / 60)
            self.afc_extrude(0.7776 * ratio, 469 / 60)
            self.afc_extrude(0.1293 * ratio, 469 / 60)
            self.afc_extrude(0.9673 * ratio, 501 / 60)
            self.afc_extrude(1.0176 * ratio, 527 / 60)
            self.afc_extrude(0.5956 * ratio, 544 / 60)
            self.afc_extrude(1.0662 * ratio, 552 / 60)
            step +=1
        self.logger.info(f'AFC-TIP-FORM: Step {step}: Retraction & Nozzle Separation')
        total_retraction_distance = self.cooling_tube_position + self.cooling_tube_length - 15
        self.afc_extrude(-15, self.unloading_speed_start)
        if total_retraction_distance > 0:
            self.afc_extrude(-.7 * total_retraction_distance, 1.0 * self.unloading_speed)
            self.afc_extrude(-.2 * total_retraction_distance, 0.5 * self.unloading_speed)
            self.afc_extrude(-.1 * total_retraction_distance, 0.3 * self.unloading_speed)
        if self.toolchange_temp > 0:
            if self.use_skinnydip:
                wait = False
            else:
                wait =  True

            self.logger.info(f"AFC-TIP-FORM: Waiting for temperature to get to {self.toolchange_temp}")
            pheaters.set_temperature(extruder.get_heater(), self.toolchange_temp, wait)
        step +=1
        self.logger.info(f'AFC-TIP-FORM: Step {step}: Cooling Moves')
        speed_inc = (self.final_cooling_speed - self.initial_cooling_speed) / (2 * self.cooling_moves - 1)
        for move in range(self.cooling_moves):
            speed = self.initial_cooling_speed + speed_inc * move * 2
            self.afc_extrude(self.cooling_tube_length, speed)
            self.afc_extrude(self.cooling_tube_length * -1, (speed + speed_inc))
        step += 1
        if self.use_skinnydip:
            self.logger.info(f'AFC-TIP-FORM: Step {step}: Skinny Dipping')
            self.afc_extrude(self.skinnydip_distance, self.dip_insertion_speed)
            self.reactor.pause(self.reactor.monotonic() + self.melt_zone_pause)
            self.afc_extrude(self.skinnydip_distance * -1, self.dip_extraction_speed)
            self.reactor.pause(self.reactor.monotonic() + self.cooling_zone_pause)

        if extruder.get_heater().target_temp != current_temp:
            self.logger.info(f'AFC-TIP-FORM: Setting temperature back to {current_temp}')
            pheaters.set_temperature(extruder.get_heater(), current_temp)

        self.logger.info('AFC-TIP-FORM: Done')

def load_config(config: ConfigWrapper) -> afc_tip_form:
    """
    Klipper config entry point for the AFC_form_tip module.

    :param config: Klipper config wrapper for the AFC_form_tip section
    :return type: afc_tip_form instance to register with the printer
    """
    return afc_tip_form(config)
