# AFCProject Automated Filament Changer Software
#
# Copyright (C) 2024-2025 Armored Turtle
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback

from configparser import Error as error

from typing import TYPE_CHECKING

try: from extras.AFC_utils import ERROR_STR
except:
    trace = traceback.format_exc()
    err_str = f"Error when trying to import AFC_utils.ERROR_STR\n{trace}"
    raise error(err_str)

try: from extras.AFC_NightOwl import afcNightOwl
except: raise error(ERROR_STR.format(import_lib="AFC_NightOwl", trace=traceback.format_exc()))

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from extras.AFC_lane import AFCLane
    from extras.AFC_utils import AFCGcmd

class afcQuattroBox(afcNightOwl):
    def __init__(self, config: ConfigWrapper) -> None:
        """
        Initialize the Quattro Box unit, reading its type from config.

        :param config: Klipper config wrapper for the AFC_QuattroBox section
        """
        super().__init__(config)
        self.type: str = config.get('type', 'Quattro_Box')

    def handle_connect(self) -> None:
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """
        super().handle_connect()

        self.logo = '<span class=success--text>Quattro Box Ready</span>'

        self.logo_error = '<span class=error--text>Quattro Box Not Ready</span>\n'
        self.set_logo_color(self.led_logo_color)

    def lane_loading(self, lane: AFCLane, gcmd: AFCGcmd) -> None:
        """
        Sets QuattroBox lanes led when lane is loading, and sets logo led's to
        `led_logo_loading` color

        :param lane: Lane object to set led
        :param gcmd: command context the led macro is run through, if any
        """
        super().lane_loading(lane, gcmd=gcmd)
        self.set_logo_color( self.led_logo_loading )

    def lane_tool_loaded(self, lane: AFCLane, gcmd: AFCGcmd) -> None:
        """
        Sets QuattroBox lanes led when lane is tool loaded, and sets logo to
        lanes color

        :param lane: Lane object to set led
        :param gcmd: command context the led macro is run through, if any
        """
        super().lane_tool_loaded(lane, gcmd=gcmd)
        self.set_logo_color(lane.color)

    def lane_tool_unloaded(self, lane: AFCLane, gcmd: AFCGcmd) -> None:
        """
        Sets QuattroBox lanes led when lane is tool unloaded, and sets logo
        color back to `led_logo_color` color

        :param lane: Lane object to set led
        :param gcmd: command context the led macro is run through, if any
        """
        super().lane_tool_unloaded(lane, gcmd=gcmd)
        self.set_logo_color(self.led_logo_color)

def load_config_prefix(config: ConfigWrapper) -> afcQuattroBox:
    """
    Klipper config entry point for AFC_QuattroBox <name> sections.

    :param config: Klipper config wrapper for the AFC_QuattroBox section
    :return afcQuattroBox: constructed Quattro Box unit
    """
    return afcQuattroBox(config)
