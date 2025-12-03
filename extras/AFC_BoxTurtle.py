# Armored Turtle Automated Filament Control
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import traceback

from configparser import Error as error
from datetime import datetime

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCLaneState
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try: from extras.AFC_unit import afcUnit
except: raise error(ERROR_STR.format(import_lib="AFC_unit", trace=traceback.format_exc()))

class afcBoxTurtle(afcUnit):
    def __init__(self, config):
        super().__init__(config)
        self.type = config.get('type', 'Box_Turtle')

    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.afc`.
        """
        super().handle_connect()

        firstLeg = '<span class=warning--text>|</span><span class=error--text>_</span>'
        secondLeg = firstLeg + '<span class=warning--text>|</span>'
        self.logo ='<span class=success--text>R  _____     ____\n'
        self.logo+='E /      \  |  </span><span class=info--text>o</span><span class=success--text> | \n'
        self.logo+='A |       |/ ___/ \n'
        self.logo+='D |_________/     \n'
        self.logo+='Y {first}{second} {first}{second}\n'.format(first=firstLeg, second=secondLeg)
        self.logo+= '  ' + self.name + '\n'

        self.logo_error ='<span class=error--text>E  _ _   _ _\n'
        self.logo_error+='R |_|_|_|_|_|\n'
        self.logo_error+='R |         \____\n'
        self.logo_error+='O |              \ \n'
        self.logo_error+='R |          |\ <span class=secondary--text>X</span> |\n'
        self.logo_error+='! \_________/ |___|</span>\n'
        self.logo_error+= '  ' + self.name + '\n'

    def system_Test(self, cur_lane, delay, assignTcmd, enable_movement):
        msg = ''
        succeeded = True

        # Run test reverse/forward on each lane
        cur_lane.unsync_to_extruder(False)
        if enable_movement:
            cur_lane.move(5, self.afc.short_moves_speed, self.afc.short_moves_accel, True)
            self.afc.reactor.pause(self.afc.reactor.monotonic() + delay)
            cur_lane.move(-5, self.afc.short_moves_speed, self.afc.short_moves_accel, True)
        else:
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.7)

        if not cur_lane.prep_state:
            if not cur_lane.load_state:
                self.afc.function.afc_led(cur_lane.led_not_ready, cur_lane.led_index)
                msg += 'EMPTY READY FOR SPOOL'
            else:
                self.afc.function.afc_led(cur_lane.led_fault, cur_lane.led_index)
                msg +="<span class=error--text> NOT READY</span>"
                cur_lane.do_enable(False)
                msg = '<span class=error--text>CHECK FILAMENT Prep: False - Load: True</span>'
                succeeded = False

        else:
            self.afc.function.afc_led(cur_lane.led_ready, cur_lane.led_index)
            msg +="<span class=success--text>LOCKED</span>"
            if not cur_lane.load_state:
                msg +="<span class=error--text> NOT LOADED</span>"
                self.afc.function.afc_led(cur_lane.led_not_ready, cur_lane.led_index)
                succeeded = False
            else:
                cur_lane.status = AFCLaneState.LOADED
                msg +="<span class=success--text> AND LOADED</span>"
                self.afc.function.afc_led(cur_lane.led_spool_illum, cur_lane.led_spool_index)

                if cur_lane.tool_loaded:
                    if cur_lane.get_toolhead_pre_sensor_state() == True or cur_lane.extruder_obj.tool_start == "buffer" or cur_lane.extruder_obj.tool_end_state:
                        if cur_lane.extruder_obj.lane_loaded == cur_lane.name:
                            self.afc.current = cur_lane.name
                            cur_lane.sync_to_extruder()
                            msg +="<span class=primary--text> in ToolHead</span>"
                            if cur_lane.extruder_obj.tool_start == "buffer":
                                msg += "<span class=warning--text>\n Ram sensor enabled, confirm tool is loaded</span>"

                            if self.afc.function.get_current_lane() == cur_lane.name:
                                self.afc.spool.set_active_spool(cur_lane.spool_id)
                                cur_lane.unit_obj.lane_tool_loaded( cur_lane )
                                cur_lane.status = AFCLaneState.TOOLED

                            cur_lane.enable_buffer()
                        else:
                            if cur_lane.get_toolhead_pre_sensor_state() == True or cur_lane.extruder_obj.tool_end_state:
                                msg +="<span class=error--text> error in ToolHead. \nLane identified as loaded \n but not identified as loaded in extruder</span>"
                                succeeded = False
                    else:
                        lane_check=self.afc.error.fix('toolhead', cur_lane)  #send to error handling
                        if not lane_check:
                            return False

        if assignTcmd: self.afc.function.TcmdAssign(cur_lane)

        # Now that a T command is assigned, send lane data to moonraker
        cur_lane.send_lane_data()

        cur_lane.do_enable(False)
        self.logger.info( '{lane_name} tool cmd: {tcmd:3} {msg}'.format(lane_name=cur_lane.name, tcmd=cur_lane.map, msg=msg))
        cur_lane.set_afc_prep_done()

        return succeeded

    def calibrate_bowden(self, cur_lane, dis, tol):
        cur_extruder = cur_lane.extruder_obj
        cur_hub = cur_lane.hub_obj
        self.logger.raw('Calibrating Bowden Length with {}'.format(cur_lane.name))
        # move to hub and retrieve that distance, the checkpoint returned and if successful
        hub_pos, checkpoint, success = self.move_until_state(cur_lane, lambda: cur_hub.state, cur_hub.move_dis, tol,
                                                             cur_lane.short_move_dis, 0, cur_lane.dist_hub + 200, "Moving to hub")

        if not success:
            # if movement does not succeed fault and return values to calibration macro
            msg = 'Failed {} after {}mm'.format(checkpoint, hub_pos)
            return False, msg, hub_pos

        bow_pos = 0
        if cur_extruder.tool_start:
            # if tool_start is defined move and confirm distance
            while not cur_lane.get_toolhead_pre_sensor_state():
                fault_dis = cur_hub.afc_bowden_length + 500
                cur_lane.move(dis, self.short_moves_speed, self.short_moves_accel)
                bow_pos += dis
                self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.1)
                if bow_pos >= fault_dis:
                    # fault if move to bowden length does not reach toolhead sensor return to calibration macro
                    msg = 'while moving to toolhead. Failed after {}mm'.format(bow_pos)
                    msg += '\n if filament stopped short of the toolhead sensor/ramming during calibration'
                    msg += '\n use the following command to increase bowden length'
                    msg += '\n SET_BOWDEN_LENGTH HUB={} LENGTH=+(distance the filament was short from the toolhead)'.format(cur_hub.name)
                    return False, msg, bow_pos

            if cur_extruder.tool_start != 'buffer':
                # is using ramming, only use first trigger of sensor
                bow_pos, checkpoint, success = self.calc_position(cur_lane, lambda: cur_lane.get_toolhead_pre_sensor_state(), bow_pos,
                                                                  cur_lane.short_move_dis, tol, 100, "retract from toolhead sensor")

            if not success:
                # fault if check is not successful
                msg = 'Failed {} after {}mm'.format(checkpoint, bow_pos)
                return False, msg, bow_pos

            cur_lane.move(bow_pos * -1, cur_lane.long_moves_speed, cur_lane.long_moves_accel, True)

            success, message, hub_dis = self.calibrate_hub(cur_lane, tol)

            if not success:
                return False, message, hub_dis

            if cur_hub.state:
                # reset at hub
                cur_lane.move(cur_hub.move_dis * -1, cur_lane.short_moves_speed, cur_lane.short_moves_accel, True)

            bowden_dist = 0
            if cur_extruder.tool_start == 'buffer':
                bowden_dist = bow_pos - (cur_lane.short_move_dis * 2)
            else:
                bowden_dist = bow_pos - cur_lane.short_move_dis

            unload_dist = bowden_dist

            cal_msg = '\n afc_bowden_length: New: {} Old: {}'.format(bowden_dist, cur_lane.hub_obj.afc_bowden_length)
            unload_cal_msg = '\n afc_unload_bowden_length: New: {} Old: {}'.format(unload_dist, cur_lane.hub_obj.afc_unload_bowden_length)
            cur_lane.hub_obj.afc_bowden_length = bowden_dist
            cur_lane.hub_obj.afc_unload_bowden_length = unload_dist

            if bowden_dist < 0:
                self.afc.error.AFC_error(
                    "'{}' is not a valid length. Please check your setup and re-run calibration.".format(bowden_dist),
                    pause=False)
                return False, "Invalid bowden length", bowden_dist

            if unload_dist < 0:
                self.afc.error.AFC_error(
                    "'{}' is not a valid unload length. Please check your setup and re-run calibration.".format(unload_dist),
                    pause=False)
                return False, "Invalid unload bowden length", unload_dist

            self.afc.function.ConfigRewrite(cur_hub.fullname, "afc_bowden_length", bowden_dist, cal_msg)
            self.afc.function.ConfigRewrite(cur_hub.fullname, "afc_unload_bowden_length", unload_dist, unload_cal_msg)
            cur_lane.loaded_to_hub  = True
            cur_lane.do_enable(False)
            self.afc.save_vars()
            return True, "afc_bowden_length successful", bowden_dist
        else:
            self.logger.info('CALIBRATE_AFC is not currently supported without tool start sensor')
            return False, "CALIBRATE_AFC is not currently supported without tool start sensor", 0

    def calibrate_td1(self, cur_lane, dis, tol):
        """
        Calibration function for automatically determining td1_bowden_length

        :param cur_lane: Lane to use for calibration
        :param dis: Distance step to move when calibrating
        :param tol: Tolerance for fine adjustments during calibration, only used when moving filament to hub

        :return success,message,length: Returns tuple, when successful returns True,message, and length of bowden to TD-1
                                        When error occurs, returns False,message, and length of current bowden before error occurred
        """
        bow_pos = 0
        cur_hub = cur_lane.hub_obj

        # Verify TD-1 is still connected before trying to get data
        if not self.afc.td1_present:
            msg = "TD-1 device not detected anymore, please check before continuing to calibrate TD-1 bowden length"
            return False, msg, 0

        if cur_lane.td1_device_id:
            valid, msg = self.afc.function.check_for_td1_id(cur_lane.td1_device_id)
            if not valid:
                return valid, msg, 0

        self.logger.raw(f"Calibrating bowden length to TD-1 device with {cur_lane.name}")
        hub_pos, checkpoint, success = self.move_until_state(cur_lane, lambda: cur_hub.state, cur_hub.move_dis, tol,
                                                             cur_lane.short_move_dis, 0, cur_lane.dist_hub + cur_lane.hub_obj.move_dis + 200, "Moving to hub")

        if not success:
            # if movement does not succeed fault and return values to calibration macro
            msg = 'Failed {} after {}mm'.format(checkpoint, hub_pos)
            cur_lane.do_enable(False)
            return False, msg, hub_pos

        compare_time = datetime.now()
        while not self.get_td1_data(cur_lane, compare_time):
            if bow_pos > cur_hub.afc_bowden_length:
                # fault if move to TD-1 is not detected
                msg = 'TD-1 failed to detect filament after moving {}mm'.format(bow_pos)
                cur_lane.do_enable(False)
                return False, msg, bow_pos

            compare_time = datetime.now()
            bow_pos += dis

            cur_lane.move(dis, self.short_moves_speed, self.short_moves_accel)
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 5)

        cur_lane.move(bow_pos * -1, cur_lane.long_moves_speed, cur_lane.long_moves_accel, True)

        # Reset to hub
        self.calc_position(cur_lane, lambda: cur_lane.hub_obj.state, 0,
                           cur_lane.short_move_dis, tol, 200, checkpoint)

        cur_lane.move(cur_hub.hub_clear_move_dis * -1, cur_lane.short_moves_speed, cur_lane.short_moves_accel, True)

        cal_msg = f"\n td1_bowden_length: New: {bow_pos} Old: {cur_hub.td1_bowden_length}"
        cur_hub.td1_bowden_length = bow_pos
        self.afc.function.ConfigRewrite(cur_hub.fullname, "td1_bowden_length", bow_pos, cal_msg)

        cur_lane.do_enable(False)
        self.afc.save_vars()
        # self.logger.info(f"td1_bowden_length: {bow_pos}")
        return True, "td1_bowden_length calibration successful", bow_pos

    # Helper functions for movement and calibration
    def calibrate_hub(self, cur_lane, tol):
        hub_pos = 0
        msg = ''
        hub_fault_dis = cur_lane.dist_hub + 150
        checkpoint = 'hub calibration {}'.format(cur_lane.name)
        # move until hub sensor is triggered and get information
        hub_pos, checkpoint, success = self.move_until_state(cur_lane, lambda: cur_lane.hub_obj.state, cur_lane.hub_obj.move_dis,
                                                             tol, cur_lane.short_move_dis, hub_pos, hub_fault_dis, checkpoint)

        if not success:
            # fault if check is not successful
            msg = 'Failed to calibrate dist_hub for {}. Failed after {}mm'.format(cur_lane.name, hub_fault_dis)
            msg += '\n if filament stopped short of the hub during calibration use the following command to increase dist_hub value'
            msg += '\n SET_HUB_DIST LANE={} LENGTH=+(distance the filament was short from the hub)'.format(cur_lane.name)
            return False, msg, hub_pos

        hub_dist = cur_lane.dist_hub + 500
        # verify hub distance
        tuned_hub_pos, checkpoint, success = self.calc_position(cur_lane, lambda: cur_lane.hub_obj.state, hub_pos,
                                                                cur_lane.short_move_dis, tol, hub_dist, checkpoint)

        if not success:
            # fault if check is not successful
            msg = 'failed {} after {}mm'.format(checkpoint, tuned_hub_pos)
            return False, msg, tuned_hub_pos

        # when successful return values to calibration macro
        return True, msg, tuned_hub_pos

    def move_until_state(self, cur_lane, state, move_dis, tolerance, short_move, pos=0, fault_dis=250, checkpoint=None):
        # moves filament until specified sensor, returns values for further calibration
        while not state():
            cur_lane.move(move_dis, cur_lane.short_moves_speed, cur_lane.short_moves_accel)
            pos += move_dis
            if pos >= fault_dis:
                # return if pos exceeds fault_dis
                return fault_dis, checkpoint, False
        self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.1)

        state_retracts = 0
        while state():
            # retract off of sensor
            state_retracts =+ 1
            cur_lane.move(short_move * -1, cur_lane.short_moves_speed, cur_lane.short_moves_accel, True)
            pos -= short_move
            check_p = '{} switch did not go false, reset lane and check switch'.format(checkpoint)
            if state_retracts >= 4:
                # fault if it takes more than 4 attempts
                f_dis = short_move * 4
                return f_dis, check_p, False
        self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.1)

        tol_checks = 0
        while not state():
            # move back to sensor in short steps
            tol_checks += 1
            cur_lane.move(tolerance, cur_lane.short_moves_speed, cur_lane.short_moves_accel)
            pos += tolerance
            check_p = '{} switch failed to become true during tolerance check, reset lane and check switch'.format(checkpoint)
            if tol_checks >= 15:
                # fault if tol_checks exceed 15
                return fault_dis, check_p, False

        return pos, checkpoint, True

    def calc_position(self, cur_lane, state, pos, short_move, tolerance, fault_dis=250, checkpoint=None):
        # move off and back on to sensor to calculate end position of calibration
        check_pos = 0
        while state():
            # retract from sensor
            cur_lane.move(short_move * -1, cur_lane.short_moves_speed, cur_lane.short_moves_accel, True)
            pos -= short_move
            check_pos -= short_move
            if abs(check_pos) >= fault_dis:
                # fault if absolute value you check_pos exceeds fault_dis
                return fault_dis, checkpoint, False
        self.afc.reactor.pause(self.afc.reactor.monotonic() + 0.1)

        checkpoint += ', tolerance check,'
        tol_checks = 0
        while not state():
            #move back to sensor to confirm distance
            tol_checks += 1
            cur_lane.move(tolerance, cur_lane.short_moves_speed, cur_lane.short_moves_accel)
            pos += tolerance

            if tol_checks >= 15:
                # fault if tol_checks exceeds 15
                return pos, checkpoint, False

        return pos, checkpoint, True

    def calibrate_lane(self, cur_lane, tol):
        # function to calibrate distance from secondary extruder to hub
        cur_hub = cur_lane.hub_obj
        if cur_hub.state:
            msg = 'Hub is not clear, check before calibration'
            return False, msg, 0
        if not cur_lane.load_state:
            msg = '{} not loaded, load before calibration'.format(cur_lane.name)
            return False, msg, 0
        if not cur_lane.prep_state:
            msg = '{} is loaded but not prepped, check prep before calibration'.format(cur_lane.name)
            return False, msg, 0

        self.logger.info('Calibrating {}'.format(cur_lane.name))
        cur_lane.status = AFCLaneState.CALIBRATING
        # reset to extruder
        pos, checkpoint, success = self.calc_position(cur_lane, lambda: cur_lane.load_state, 0, cur_lane.short_move_dis,
                                                      tol, cur_lane.dist_hub + 100, "retract to extruder")

        if not success:
            if checkpoint == "retract to extruder":
                msg = (
                    """\n{} failed during calibration after {}mm. Check position of filament and
                    reset filament using BT_LANE_MOVE macro if necessary. If filament is between
                    the extruder and the hub, and is moving smoothly, you may need to increase the
                    dist_hub value. Once adjusted, please try again. This can be adjusted by
                    using the SET_HUB_DIST LANE=<lane> LENGTH=<+/- distance> macro. Once you are
                    satisfied, you can save the values with SAVE_HUB_DIST LANE=<lane> macro.\n""".format(cur_lane.name, pos)
                )
            else:
                msg = 'Lane failed to calibrate {} after {}mm'.format(checkpoint, pos)
            cur_lane.status = AFCLaneState.NONE
            cur_lane.unit_obj.return_to_home()
            return False, msg, 0

        else:
            success, message, hub_pos = self.calibrate_hub(cur_lane, tol)

            if not success:
                cur_lane.status = AFCLaneState.NONE
                cur_lane.unit_obj.return_to_home()
                return False, message, hub_pos

            if cur_hub.state:
                cur_lane.move(cur_hub.move_dis * -1, cur_lane.short_moves_speed, cur_lane.short_moves_accel, True)

            cal_dist = hub_pos - cur_hub.hub_clear_move_dis
            cal_msg = "\n{} dist_hub: New: {} Old: {}".format(cur_lane.name, cal_dist, cur_lane.dist_hub)
            cur_lane.loaded_to_hub  = True
            cur_lane.do_enable(False)
            cur_lane.dist_hub = cal_dist
            self.afc.function.ConfigRewrite(cur_lane.fullname, "dist_hub", cal_dist, cal_msg)
            cur_lane.status = AFCLaneState.NONE
            cur_lane.unit_obj.return_to_home()
            return True, cal_msg, cal_dist

def load_config_prefix(config):
    return afcBoxTurtle(config)