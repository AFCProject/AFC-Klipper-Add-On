# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback

from configparser import Error as config_error

from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from extras.AFC_lane import AFCLane, AFCMoveWarning

try: from extras.AFC_utils import ERROR_STR
except:
    trace=traceback.format_exc()
    err_str = f"Error when trying to import AFC_utils.ERROR_STR\n{trace}"
    raise config_error(err_str)

try: from extras.AFC_BoxTurtle import afcBoxTurtle
except:
    err_str = ERROR_STR.format(import_lib="AFC_BoxTurtle", trace=traceback.format_exc())
    raise config_error(err_str)

try: from extras.AFC_lane import SpeedMode, AssistActive, AFCLaneState, MoveDirection
except:
    err_str = ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc())
    raise config_error(err_str)

class AFC_vivid(afcBoxTurtle):
    """
    ViViD style AFC unit implementation.

    This class provides all selector based behavior for ViViD units, including:
    - Loading drive/selector steppers and validating configuration.
    - Selector cam homing and lane selection (`select_lane`).
    - Filament loading, initial calibration, and hub distance updates (`prep_load`).
    - Lane ejection routines (`eject_lane`).
    - Standardized hub movement helpers (`move_to_hub`).
    - Calibration reset workflow (`calibrate_lane`).
    - Registration of the `AFC_SELECT_LANE` Gcode command.

    ViViD units use a rotating cam to select lanes and rely on prep/load sensors
    for homing based filament movement. This class encapsulates all hardware specific
    logic needed to operate those units within the AFC framework.
    """
    VALID_CAM_ANGLES = [30,45,60]
    CALIBRATION_DISTANCE = 5000
    LANE_OVERSHOOT = 200
    def __init__(self, config: ConfigWrapper):
        """
        Initialize a ViViD style AFC unit.

        Parameters
        ----------
        config : ConfigWrapper
            Configuration for this unit, including stepper names, homing
            parameters, GUI sensor settings, and other AFC specific options.
        """
        super().__init__(config)
        self.type:str               = config.get('type', 'ViViD')
        self.drive_stepper:str      = config.get("drive_stepper")                                                   # Name of AFC_stepper for drive motor
        self.selector_stepper:str   = config.get("selector_stepper")                                                # Name of AFC_stepper for selector motor
        self.current_selected_lane  = None
        self.home_state             = False
        self.enable_sensors_in_gui  = config.getboolean("enable_sensors_in_gui",
                                                        self.afc.enable_sensors_in_gui)    # Set to True to show prep and load sensors switches as filament sensors in mainsail/fluidd gui, overrides value set in AFC.cfg
        self.prep_homed             = False
        self.failed_to_home         = False
        self.selector_homing_speed  = config.getfloat("selector_homing_speed", 150)
        self.selector_homing_accel  = config.getfloat("selector_homing_accel", 150)
        self.max_selector_movement  = config.getfloat("max_selector_movement", 800)
        self._eject_to_calibrate    = True

        self._lookup_objects(config)

        self.function.register_mux_command(self.afc.show_macros, 'AFC_UNSELECT_LANE', 'UNIT', self.name,
                                           self.cmd_AFC_UNSELECT_LANE, self.cmd_AFC_UNSELECT_LANE_help,
                                           self.cmd_AFC_UNSELECT_LANE_options )

    def handle_connect(self):
        super().handle_connect()
        self.logo = '<span class=success--text>ViViD Ready\n</span>'
        self.logo_error = '<span class=error--text>ViViD Not Ready</span>\n'

    def _move_lane(self, lane: AFCLane, delay: float=1,
                enable_movement: bool=True) -> bool:
        """
        Method to check and see if filament is loaded into lane. This method tries to move filament
        to load sensor, once successful lane is retracted by hubs `hub_clear_move_dis`. If homing
        is not successful, internal lane states are reset.

        :param lane: Lane to move and check if filament is present
        :param delay: Not used
        :param enable_movement: Not used
        :return: Returns True if homing was successful, False when homing is not successful
        """
        loaded = False
        homed = False
        if lane.prep_state:
            homed, _, _ = self.move_to_load(lane, lane.dist_hub, MoveDirection.POS,
                                            True, SpeedMode.SHORT)
            if homed:
                loaded = True
                lane.loaded_to_hub = True
                if not lane.tool_loaded:
                    lane.move_to(lane.hub_obj.hub_clear_move_dis * MoveDirection.NEG,
                                 SpeedMode.SHORT, use_homing=False)
        # Resetting internal states when prep sensor is not triggered but loaded to hub is still
        # set, or when failed to home to load sensor
        if ((not lane.prep_state
             and lane.loaded_to_hub)
             or not homed):
            self.lane_unloaded(lane)
            lane.tool_loaded = False
            lane.status = AFCLaneState.NONE
            lane.loaded_to_hub = False
            lane.td1_data = {}
            if not lane.remember_spool:
                lane.afc.spool.clear_values(lane)
        return loaded

    def system_Test(self, cur_lane, delay, assignTcmd, enable_movement):
        return super().system_Test( cur_lane, delay, assignTcmd, enable_movement=False)

    def _get_lane_selector_state(self, lane: AFCLane) -> bool:
        """
        Helper method to return status for lanes selector

        :param lane: ViViD lane to get selector status from.
        :return bool: Returns True if cam is triggering lanes selector.
        """
        state = False
        if hasattr(lane, "fila_selector"):
            fila_selector_status = lane.fila_selector.get_status(0)
            state = fila_selector_status["filament_detected"]
        return state

    def _get_selector_enabled(self) -> bool:
        """
        Helper method to lookup if lanes selector is enabled.

        :return bool: Returns True if lanes selector is enabled.
        """
        stepper_enable = self.printer.lookup_object("stepper_enable", None)
        enabled = False
        if stepper_enable:
            status = stepper_enable.get_status(0)
            try:
                enabled = status["steppers"][f"AFC_stepper {self.selector_stepper}"]
            except:
                pass
        return enabled

    def select_lane( self, lane: AFCLane, sel_prep:bool=False ) -> tuple[bool, float|int]:
        """
        Helper method to use homing to rotate cam until lanes selector
        is triggered.

        :param lane: Lane to select for moving filament
        :param sel_prep: When set to True, cam is rotated in the negative direction
                         this is useful when loading filament so the filament properly
                         goes into the PTFE
        :return tuple: Returns tuple of homed(True/False) and distance moved
        """
        if lane.selector_endstop_name:
            sel_dir = MoveDirection.NEG if sel_prep else MoveDirection.POS
            selector_state = self._get_lane_selector_state(lane)
            selector_enabled= self._get_selector_enabled()

            if (selector_state
                and selector_enabled):
                self.logger.debug(f"{lane.name} already selected")
                return True, 0.0
            else:
                if (not selector_enabled
                    and selector_state):
                    self.unselect_lane()

                self.logger.info(f"ViViD: Selecting {lane.name}")
                homed, distance = self.selector_stepper_obj.do_homing_move(
                    movepos=self.max_selector_movement * sel_dir,
                    speed=self.selector_homing_speed,
                    accel=self.selector_homing_accel,
                    endstop_spec=lane.selector_endstop_name,
                    assist_active=False
                )

                if (lane.selector_cal_dis is not None
                    and lane.selector_cal_dis != 0.0):
                    self.selector_stepper_obj.move(lane.selector_cal_dis, lane.short_moves_speed,
                                                   lane.short_moves_accel, False)

                self.logger.debug(f"ViViD: Homing done, success:{homed}, distance:{distance}")
                return homed, round(distance, 2)

    def prep_load(self, lane: AFCLane):
        """
        Helper method for initially loading spools when prep sensor is triggered.
        Upon first load, if lanes do not have calibrated_lanes variable set, filament
        will move slowly until load sensor is triggered. This is value is set as
        dist_hub variable and calibration variable is set in config file.

        Lane selector is first rotated in the negative distance until selector is
        triggered, then filament is moved to load sensor via homing routine. Once sensor
        is triggered lane is backed up 10mm so that filament is not triggering load
        sensor anymore since VVD uses all load sensors as a virtual hub, which means if
        one load sensor is triggered then the virtual hub state is triggered.

        After lane is loaded selector will select current lane if a lane is loaded into
        toolhead.

        :param lane: AFCLane object for which to activate and load filament to load sensor
        """
        self.lane_loading(lane)
        self.select_lane(lane, sel_prep=True)
        num_tries = 0
        if not lane.calibrated_lane:
            distance = self.CALIBRATION_DISTANCE
            move_speed = SpeedMode.SHORT
        else:
            distance = lane.dist_hub
            move_speed = SpeedMode.LONG
        homed = False
        while (num_tries < 2
               and lane.prep_state
               and not lane.raw_load_state):
            homed, distance, _ = lane.move_to(distance, move_speed, assist_active=AssistActive.NO,
                                              endstop=lane.load_endstop_name, use_homing=True)
            num_tries += 1
        if homed:
            lane.loaded_to_hub = True
            if not lane.calibrated_lane:
                lane.calibrated_lane = True
                lane.dist_hub = round(distance, 2) + self.LANE_OVERSHOOT
                self.afc.function.ConfigRewrite(lane.fullname, "dist_hub", lane.dist_hub,
                                                f"{lane.name} calibrated, updating dist_hub")
                self.afc.function.ConfigRewrite(lane.fullname, "calibrated_lane",
                                                lane.calibrated_lane, "")
            # Retract so load sensor is not triggered
            lane.move_to(lane.hub_obj.hub_clear_move_dis * MoveDirection.NEG,
                         SpeedMode.SHORT, use_homing=False)
            self.lane_loaded(lane)
        else:
            self.logger.error(f"Failed to move {lane.name} to load sensor.")

        self.selector_stepper_obj.do_enable(False)
        self.drive_stepper_obj.do_enable(False)
        self.afc.function.select_loaded_lane()

    def prep_post_load(self, lane: AFCLane):
        """
        This method does nothing and just returns for ViViD units
        """
        # Do nothing and return
        return

    def unselect_lane(self, move_distance: float=50):
        """
        Move the selector by a configurable distance to free filament in a lane, e.g. when
        ejecting filament.

        :param move_distance: Distance in millimeters to move the selector. Defaults to 50 mm.
        """
        self.selector_stepper_obj.move(move_distance, 100, 100, False)

    def eject_lane(self, lane: AFCLane):
        """
        Method to select lane and eject spool, uses homing to move spool to prep sensor. Movement
        will stop once prep sensor is no longer triggered if movement has not already stopped.

        After retract movement is done, selector is rotated to loosen grip on filament so it can
        be easily removed.

        :param lane: Lane to eject spool
        """
        self.select_lane(lane)
        move_dis = lane.dist_hub
        if move_dis > 400:
            move_dis = lane.dist_hub - (self.LANE_OVERSHOOT+100) - \
                       lane.hub_obj.hub_clear_move_dis - lane.homing_overshoot
        lane.move_to(move_dis * MoveDirection.NEG, SpeedMode.LONG,
                     endstop=lane.prep_endstop_name,
                     assist_active=AssistActive.NO, use_homing=True)
        self.unselect_lane()
        self.selector_stepper_obj.do_enable(False)
        self.drive_stepper_obj.do_enable(False)

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
        homed, distance, warn = lane.move_to(dist * dir, speed_mode, assist_active=assist_active,
                                             endstop=lane.load_es, use_homing=use_homing)
        return homed, distance, warn

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
        self.eject_lane(cur_lane)
        cur_lane.loaded_to_hub = False
        cur_lane.status = AFCLaneState.NONE
        cur_lane.calibrated_lane = False
        self.afc.function.afc_led(cur_lane.led_not_ready, cur_lane.led_index)
        return True, "calibration_lane", 0

    def calibration_lane_message(self) -> str:
        """
        Method for returning calibration message informing user to reinsert filament to calibrate
        lane.

        :return str: Message informing user to reinsert to calibrate lane.
        """
        msg = "\nThe following lanes ({lanes}) were ejected and calibration flag set to false. "
        msg += "Please reinsert filament into ViViD to automatically calibrate distance.\n"
        return msg

def load_config_prefix(config):
    return AFC_vivid(config)