# Armored Turtle Automated Filament Control
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback

from configparser import Error as error
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from extras.AFC_lane import AFCLane, MoveDirection

try: from extras.AFC_utils import ERROR_STR
except: raise error(
    "Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(
        trace=traceback.format_exc()))

try: from extras.AFC_lane import (
    SpeedMode, AssistActive, MoveDirection, AFCMoveWarning
)
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try: from extras.AFC_BoxTurtle import afcBoxTurtle
except: raise error(ERROR_STR.format(import_lib="AFC_BoxTurtle", trace=traceback.format_exc()))

class AFC_EMU(afcBoxTurtle):
    def __init__(self, config):
        super().__init__(config)
        self.type = config.get('type', 'EMU')

    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.afc`.
        """
        super().handle_connect()

        self.logo = '<span class=success--text>EMU Ready\n</span>'
        self.logo_error = '<span class=error--text>EMU Not Ready</span>\n'

    def prep_post_load(self, lane: AFCLane):
        """
        Helper method to run after prep_load has been successful.

        Checks if hub is setup as a virtual hub, if so filament is moved behind load switch and
        sets loaded_to_hub as True. If normal hub, calls parents prep_post_load method.

        :param lane: AFCLane object for which to perform prep_post_load action on
        """
        if (lane.hub_obj is not None
            and hasattr(lane.hub_obj, 'is_virtual_pin')
            and lane.hub_obj.is_virtual_pin()):
            num_tries = 0
            while( lane.raw_load_state and num_tries < 10 ):
                lane.move_to(lane.hub_obj.hub_clear_move_dis * MoveDirection.NEG,
                            SpeedMode.SHORT, use_homing=False)
                num_tries += 1
            lane.loaded_to_hub = True
        else:
            super().prep_post_load(lane)

    def move_to_hub(self, lane: AFCLane, dist: float,
                    dir: MoveDirection, use_homing: bool=True,
                    speed_mode: SpeedMode=SpeedMode.HUB,
                    assist_active: AssistActive=AssistActive.DYNAMIC
                ) -> tuple[bool, float|int, AFCMoveWarning]:
        """
        Helper method for calling lanes move_to method and passing in lanes load endstop as trigger
        point when homing is enabled.

        :param lane: AFCLane object to move
        :param dist: Distance in mm to move filament
        :param dir: Direction(+/-) to move filament
        :param use_homing: When enabled home_to logic is used, else move_advance logic is used
        :param speed_mode: SpeedMode type to use when moving stepper
        :param assist_active: EMU does not have spoolers, setting this parameter does nothing.

        :return tuple[bool, float|int, AFCMoveWarning]: A tuple containing:

            - Returns True if move was successful
            - Total distance moved
            - AFCMoveWarning.WARN set if movement moved is not within 300mm of total distance,
                  AFCMoveWarning.ERROR when error is detected during homing, AFCMoveWarning.NONE
                  when no error or not full movement detected. When homing is disabled, always returns
                  True, 0, AFCMoveWarning.NONE.
        """
        endstop = lane.hub_endstop_name
        if (lane.hub_obj is not None
            and hasattr(lane.hub_obj, 'is_virtual_pin')
            and lane.hub_obj.is_virtual_pin()):
            endstop = lane.load_es
        homed, distance, warn = lane.move_to(dist * dir, speed_mode, assist_active=assist_active,
                                             endstop=endstop, use_homing=use_homing)
        return homed, distance, warn

    def eject_lane(self, lane: AFCLane):
        """
        Method to select lane and eject spool, uses homing to move spool to prep sensor. Movement
        will stop once prep sensor is no longer triggered if movement has not already stopped.

        After retract movement is done, selector is rotated to loosen grip on filament so it can
        be easily removed.

        :param lane: Lane to eject spool
        """
        if lane.loaded_to_hub:
            lane.move_to(lane.dist_hub * -1, SpeedMode.DIST_HUB,
                            endstop=lane.load_es, assist_active=AssistActive.DYNAMIC,
                            use_homing=self.afc.homing_enabled)
        lane.move_advanced(lane.extruder_clear_dis * -1, SpeedMode.SHORT)
        lane.do_enable(False)

def load_config_prefix(config):
    return AFC_EMU(config)