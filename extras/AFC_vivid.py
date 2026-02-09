# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback

from configparser import Error as config_error

from typing import TYPE_CHECKING, Optional, Any

if TYPE_CHECKING:
    from gcode import GCodeCommand
    from extras.AFC_stepper import AFCExtruderStepper
    from extras.AFC_lane import AFCLane, MoveDirection

try: from extras.AFC_utils import ERROR_STR, section_in_config
except:
    err_str = "Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc())
    raise config_error(err_str)

try: from extras.AFC_BoxTurtle import afcBoxTurtle
except:
    err_str = ERROR_STR.format(import_lib="AFC_BoxTurtle", trace=traceback.format_exc())
    raise config_error(err_str)

try: from extras.AFC_lane import SpeedMode, AssistActive, AFCHomingPoints
except:
    err_str = ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc())
    raise config_error(err_str)

class AFC_vivid(afcBoxTurtle):
    VALID_CAM_ANGLES = [30,45,60]
    def __init__(self, config):
        super().__init__(config)
        self.type:str               = config.get('type', 'ViViD')
        self.drive_stepper:str      = config.get("drive_stepper")                                                   # Name of AFC_stepper for drive motor
        self.selector_stepper:str   = config.get("selector_stepper")                                                # Name of AFC_stepper for selector motor
        self.drive_stepper_obj: AFCExtruderStepper = None
        self.selector_stepper_obj: AFCExtruderStepper = None
        self.current_selected_lane  = None
        self.home_state             = False
        self.enable_sensors_in_gui  = config.getboolean("enable_sensors_in_gui", self.afc.enable_sensors_in_gui)    # Set to True to show prep and load sensors switches as filament sensors in mainsail/fluidd gui, overrides value set in AFC.cfg
        self.prep_homed             = False
        self.failed_to_home         = False
        self.selector_homing_speed  = config.getfloat("selector_homing_speed", 150)
        self.selector_homing_accel  = config.getfloat("selector_homing_accel", 150)

        self.function.register_commands(self.afc.show_macros, "AFC_SELECT_LANE",
                                        self.cmd_AFC_SELECT_LANE,
                                        description=self.cmd_AFC_SELECT_LANE_help,
                                        options=self.cmd_AFC_SELECT_LANE_options)

        self._lookup_objects(config)
    
    def _lookup_objects(self, config):
        error_string = ""
        error_bool   = False
        config_name = f'AFC_stepper {self.drive_stepper}'
        if section_in_config(config, config_name):
            self.drive_stepper_obj: Optional[AFCExtruderStepper] = \
                self.printer.load_object(config, config_name, None)
        error, rtn_str = self._check_and_errorout(self.drive_stepper_obj,
                                                  config_name,
                                                  "drive_stepper")
        error_string += rtn_str
        error_bool |= error
        
        config_name = f'AFC_stepper {self.selector_stepper}'
        if section_in_config(config, config_name):
            self.selector_stepper_obj: Optional[AFCExtruderStepper] = \
                self.printer.load_object(config, config_name, None)

        error, rtn_str = self._check_and_errorout(self.drive_stepper_obj,
                                                  config_name,
                                                  "drive_stepper")
        error_string += rtn_str
        error_bool |= error
        if error_bool:
            raise config_error(error_string)

    # def handle_connect(self):
    #     super().handle_connect()

    def system_Test(self, cur_lane, delay, assignTcmd, enable_movement):

        return super().system_Test( cur_lane, delay, assignTcmd, enable_movement=False)
        # self.lane_unloaded(cur_lane)
        # self.logger.info(f"{cur_lane.name} selector state {self._get_lane_selector_state(cur_lane)}")

        # if assignTcmd: self.afc.function.TcmdAssign(cur_lane)
        # cur_lane.send_lane_data()
        # cur_lane.set_afc_prep_done()
        # return True

    def _get_lane_selector_state(self, lane: AFCLane):
        state = False
        if hasattr(lane, "fila_selector"):
            fila_selector_status = lane.fila_selector.get_status(0)
            state = fila_selector_status["filament_detected"]
        return state

    def select_lane( self, lane: AFCLane ):
        if lane.selector_endstop:
            if self._get_lane_selector_state(lane):
                self.logger.debug(f"{lane.name} already selected")
                return True, 0.0
            else:
                self.logger.debug(f"ViViD: Homing to {lane.name}")
                homed, distance= self.selector_stepper_obj.do_homing_move(
                    movepos=800,
                    speed=self.selector_homing_speed,
                    accel=self.selector_homing_accel,
                    endstop_spec=lane.selector_endstop_name,
                    assist_active=False)
                self.logger.debug(f"ViViD: Homing done, success:{homed}, distance:{distance}")
                return homed, round(distance, 2)

    def check_runout(self, lane):
        pass

    def prep_load(self, lane: AFCLane):
        self.lane_loading(lane)
        self.select_lane(lane)
        if not lane.calibrated_lane:
            distance = 5000
            move_speed = SpeedMode.SHORT
        else:
            distance = lane.dist_hub
            move_speed = SpeedMode.LONG

        homed, distance = lane.move_to(distance, move_speed,
                                       assist_active=AssistActive.NO,
                                       endstop=lane.load_endstop_name,
                                       use_homing=True)
        if homed:
            lane.loaded_to_hub = True
            if not lane.calibrated_lane:
                lane.calibrated_lane = True
                lane.dist_hub = round(distance, 2) + 200
                self.afc.function.ConfigRewrite(lane.fullname, "dist_hub", lane.dist_hub,
                                                f"{lane.name} calibrated, updating dist_hub")
                self.afc.function.ConfigRewrite(lane.fullname, "calibrated_lane",
                                                lane.calibrated_lane, "")
            # Retract a bit so load sensor is not triggered
            lane.move_to( -10, SpeedMode.SHORT, use_homing=False)
            self.lane_loaded(lane)
                

        self.selector_stepper_obj.do_enable(False)
        self.drive_stepper_obj.do_enable(False)
        self.afc.function.select_loaded_lane()
        return

    def prep_post_load(self, lane: AFCLane):
        # Do nothing and return
        return
    
    def unselect_lane(self):
        self.selector_stepper_obj.move(50, 100, 100, False)
    
    def eject_lane(self, lane: AFCLane):
        self.select_lane(lane)
        lane.move_to( (lane.dist_hub-100) * -1, SpeedMode.LONG,
                    endstop=lane.prep_endstop_name,
                    assist_active=AssistActive.NO,
                    use_homing=True)
        self.unselect_lane()
        self.selector_stepper_obj.do_enable(False)
        self.drive_stepper_obj.do_enable(False)
    
    def move_to_hub(self, lane: AFCLane, dist: float,
                    dir:MoveDirection, use_homing=True,
                    speedMode=SpeedMode.HUB,
                    assist_active=AssistActive.DYNAMIC) -> bool:
        homed, distance = lane.move_to(dist * dir, speedMode,
                                       assist_active=assist_active,
                                       endstop=lane.load_es,
                                       use_homing=use_homing)
        return homed, distance
    
    cmd_AFC_SELECT_LANE_help = "Command to home to lane selector for specified lane"
    cmd_AFC_SELECT_LANE_options = {"LANE": {"type":"string", "default":"lane1"}}
    def cmd_AFC_SELECT_LANE(self, gcmd: GCodeCommand):
        lane = gcmd.get("LANE")
        lane_obj = self.afc.lanes.get(lane, None)
        if lane_obj:
            homed, distance = self.select_lane(lane_obj)
            if homed:
                self.logger.info(f"Successfully homed to {lane_obj.name} selector after {distance}mm")
            else:
                self.logger.error(f"Failed to home to {lane_obj.name}")
                # TODO: add pause?
        else:
            error_string = f"Invalid lane {lane}"
            gcmd.error(error_string)

def load_config_prefix(config):
    return AFC_vivid(config)