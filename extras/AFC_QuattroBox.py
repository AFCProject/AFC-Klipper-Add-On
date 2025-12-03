# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from configparser import Error as error
try:
    from extras.AFC_NightOwl import afcNightOwl
except:
    raise error("Error trying to import AFC_NightOwl, please rerun install-afc.sh script in your AFC-Klipper-Add-On directory then restart klipper")

class afcQuattroBox(afcNightOwl):
    def __init__(self, config):
        super().__init__(config)
        self.type = config.get('type', 'Quattro_Box')

    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """
        super().handle_connect()

        self.logo = '<span class=success--text>Quattro Box Ready</span>'

        self.logo_error = '<span class=error--text>Quattro Box Not Ready</span>\n'
        self.set_logo_color(self.led_logo_color)

    def lane_loaded(self, lane):
        """
        Sets QuattroBox lanes led when lane is loaded and illuminates spool led's
        once a spool is loaded

        :param lane: Lane object to set led
        """
        super().lane_loaded(lane)
        self.afc.function.afc_led(lane.led_spool_illum, lane.led_spool_index)

    def lane_unloaded(self, lane):
        """
        Sets QuattroBox lanes led when lane is unloaded, and turns off spool
        illumination once a spool is ejected

        :param lane: Lane object to set led
        """
        super().lane_loaded(lane)
        self.afc.function.afc_led(self.afc.led_off, lane.led_spool_index)

    def lane_loading(self, lane):
        """
        Sets QuattroBox lanes led when lane is loading, and sets logo led's to
        `led_logo_loading` color

        :param lane: Lane object to set led
        """
        super().lane_loading(lane)
        self.set_logo_color( self.led_logo_loading )

    def lane_tool_loaded(self, lane):
        """
        Sets QuattroBox lanes led when lane is tool loaded, and sets logo to
        lanes color

        :param lane: Lane object to set led
        """
        super().lane_tool_loaded(lane)
        self.set_logo_color(lane.color)

    def lane_tool_unloaded(self, lane):
        """
        Sets QuattroBox lanes led when lane is tool unloaded, and sets logo
        color back to `led_logo_color` color

        :param lane: Lane object to set led
        """
        super().lane_tool_unloaded(lane)
        self.set_logo_color(self.led_logo_color)

def load_config_prefix(config):
    return afcQuattroBox(config)