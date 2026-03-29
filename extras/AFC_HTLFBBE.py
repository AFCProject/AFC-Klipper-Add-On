# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback

from configparser import Error as config_error

from typing import TYPE_CHECKING, Union

try: from extras.AFC_utils import ERROR_STR
except:
    trace=traceback.format_exc()
    err_str = f"Error when trying to import AFC_utils.ERROR_STR\n{trace}"
    raise config_error(err_str)

try: from extras.AFC_lane import AFCLaneState, MoveDirection, AFCLane, SpeedMode, AssistActive
except: raise config_error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try: from extras.AFC_vivid import AFC_vivid
except: raise config_error(ERROR_STR.format(import_lib="AFC_vivid", trace=traceback.format_exc()))

try: from extras.AFC_BoxTurtle import afcBoxTurtle
except:
    err_str = ERROR_STR.format(import_lib="AFC_BoxTurtle", trace=traceback.format_exc())
    raise config_error(err_str)

try: from extras.AFC_utils import add_filament_switch
except: raise config_error(ERROR_STR.format(import_lib="AFC_utils", trace=traceback.format_exc()))

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from extras.AFC_lane import AFCLane, AFCMoveWarning

class AFC_HTLFBBE(AFC_vivid):
    CALIBRATION_DISTANCE = 5000
    LANE_OVERSHOOT = 200
    def __init__(self, config: ConfigWrapper):
        super().__init__(config)
        self.type: str = config.get('type', 'HTLFBBE')

    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """

        super().handle_connect()

        self.logo = '<span class=success--text>HTLF BBE Ready\n</span>'
        self.logo_error = '<span class=error--text>HTLF BBE Not Ready</span>\n'
    
    def _move_lane(self, lane: AFCLane, delay: float=1,
            enable_movement: bool=True) -> bool:
        return lane.load_state

    def system_Test(self, cur_lane, delay, assignTcmd, enable_movement):
        cur_lane.prep_state = cur_lane.load_state
        return super().system_Test( cur_lane, delay, assignTcmd, False)

    def unselect_lane(self, move_distance: float=5):
        """
        Move the selector by a configurable distance to free filament in a lane, e.g. when
        ejecting filament.

        :param move_distance: Distance in millimeters to move the selector. Defaults to 50 mm.
        """
        super().unselect_lane(move_distance=move_distance)
    
    def eject_lane(self, lane: AFCLane):
        """
        Method to select lane and eject spool, uses homing to move spool to prep sensor. Movement
        will stop once prep sensor is no longer triggered if movement has not already stopped.

        After retract movement is done, selector is rotated to loosen grip on filament so it can
        be easily removed.

        :param lane: Lane to eject spool
        """
        afcBoxTurtle.eject_lane(self, lane)

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
        :param assist_active: ViViD does not have spoolers, setting this parameter does nothing.

        :return tuple[bool, float|int, AFCMoveWarning]: A tuple containing:

            - Returns True if move was successful
            - Total distance moved
            - AFCMoveWarning.WARN set if movement moved is not within 300mm of total distance,
                  AFCMoveWarning.ERROR when error is detected during homing, AFCMoveWarning.NONE
                  when no error or not full movement detected. When homing is disabled, always returns
                  True, 0, AFCMoveWarning.NONE.
        """
        return afcBoxTurtle.move_to_hub(self, lane, dist, dir, use_homing,
                                        speed_mode, assist_active)

    def calibrate_lane(
            self, cur_lane: AFCLane, tol: Union[float|int]
        ) -> tuple[bool, str, Union[float|int]]:
        """
        Method to calibrate lane for ViViD units. Since ViViD units are unique, this method ejects
        filament and sets calibration flag to False. Once user reinserts filament lane will be
        automatically calibrated.

        :param cur_lane: Lane object to eject
        :param tol: Unused for this method
        :return tuple: Always returns True, lane name, 0
        """
        return afcBoxTurtle.calibrate_lane(self, cur_lane, tol)

    def calibration_lane_message(self) -> str:
        return afcBoxTurtle.calibration_lane_message(self)

def load_config_prefix(config):
    return AFC_HTLFBBE(config)