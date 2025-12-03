# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import json

class afcPrep:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.delay              = config.getfloat('delay_time', 0.1, minval=0.0)                # Time to delay when moving extruders and spoolers during PREP routine
        self.enable             = config.getboolean("enable", False)                            # Set True to disable PREP checks
        self.dis_unload_macro   = config.getboolean("disable_unload_filament_remapping", False) # Set to True to disable remapping UNLOAD_FILAMENT macro to TOOL_UNLOAD macro
        self.get_td1_data       = config.getboolean("capture_td1_data", False)                  # Set to True to capture TD-1 data for all lanes during prep

        # Flag to set once resume rename as occurred for the first time
        self.rename_occurred = False
        # Value gets set to false once prep has been run for the first time after restarting klipper
        self.assignTcmd = True

    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """
        self.afc = self.printer.lookup_object('AFC')
        self.afc.gcode.register_command('PREP', self.PREP, desc=None)
        self.logger = self.afc.logger

    def _rename(self, base_name, rename_name, rename_macro, rename_help):
        """
        Helper function to get stock macros, rename to something and replace stock macro with AFC functions
        """
        # Renaming users Resume macro so that RESUME calls AFC_Resume function instead
        prev_cmd = self.afc.gcode.register_command(base_name, None)
        if prev_cmd is not None:
            pdesc = "Renamed builtin of '%s'" % (base_name,)
            self.afc.gcode.register_command(rename_name, prev_cmd, desc=pdesc)
        else:
            self.logger.debug("{}Existing command {} not found in gcode_macros{}".format("<span class=warning--text>", base_name, "</span>",))
        self.logger.debug("PREP-renaming macro {}".format(base_name))
        self.afc.gcode.register_command(base_name, rename_macro, desc=rename_help)

    def _rename_macros(self):
        """
        Helper function to rename multiple macros and substitute with AFC macros.
        - Replaces stock RESUME macro and reassigns to AFC_RESUME function
        - Replaces stock UNLOAD macro and reassigns to TOOL_UNLOAD function. This can be disabled in AFC_prep config
        - Replaces stock/users PAUSE macro and reassigns to AFC_PAUSE function.
        """
        # Checking to see if rename has already been done, don't want to rename again if prep was already ran
        if not self.rename_occurred:
            self.rename_occurred = True
            self._rename(self.afc.error.BASE_RESUME_NAME, self.afc.error.AFC_RENAME_RESUME_NAME, self.afc.error.cmd_AFC_RESUME, self.afc.error.cmd_AFC_RESUME_help)
            self._rename(self.afc.error.BASE_PAUSE_NAME, self.afc.error.AFC_RENAME_PAUSE_NAME, self.afc.error.cmd_AFC_PAUSE, self.afc.error.cmd_AFC_RESUME_help)

            # Check to see if the user does not want to rename UNLOAD_FILAMENT macro
            if not self.dis_unload_macro:
                self._rename(self.afc.BASE_UNLOAD_FILAMENT, self.afc.RENAMED_UNLOAD_FILAMENT, self.afc.cmd_TOOL_UNLOAD, self.afc.cmd_TOOL_UNLOAD_help)

    def _td1_prep(self, overrall_status):
        '''
        Helper function to perform TD-1 data capture during PREP

        :prep overrall_status: Status of all the lanes, if error occurred during lane prep TD-1 data
                               will not be captured
        '''
        capture_td1_data = self.get_td1_data and self.afc.td1_present
        any_td1_error = False
        if self.afc.td1_present:
            self.logger.info("Found TD-1 device connected to printer")
            any_td1_error, _ = self.afc.function.check_for_td1_error()

        # look up what current lane should be a call select lane, this is more for units that
        # have selectors to make sure the selector is on the correct lane
        current_lane = self.afc.function.get_current_lane_obj()
        if current_lane is not None:
            current_lane.unit_obj.select_lane(current_lane)
            if capture_td1_data:
                self.logger.info("Cannot capture TD-1 data during PREP since toolhead is loaded")
        elif capture_td1_data:
            if not overrall_status:
                self.logger.info("Cannot capture TD-1 data, not all of PREP succeeded")
            else:
                if any_td1_error:
                    self.logger.error("Error with a TD-1 device, not collecting data during prep")
                else:
                    self.logger.info("Capturing TD-1 data for all loaded lanes")
                    for lane in self.afc.lanes.values():
                        if lane.load_state and lane.prep_state:
                            return_status, msg = lane.get_td1_data()
                            if not return_status:
                                break
                    self.logger.info("Done capturing TD-1 data")

    def PREP(self, gcmd):
        overrall_status = True
        while self.printer.state_message != 'Printer is ready':
            self.afc.reactor.pause(self.afc.reactor.monotonic() + 1)

        self._rename_macros()
        self.afc.print_version(console_only=True)

        # Try and connect to moonraker
        self.afc.handle_moonraker_connect()

        ## load Unit stored variables
        units={}
        if os.path.exists('{}.unit'.format(self.afc.VarFile)) and os.stat('{}.unit'.format(self.afc.VarFile)).st_size > 0:
            try:
                units=json.load(open('{}.unit'.format(self.afc.VarFile)))
            except json.JSONDecodeError as e:
                # Displaying error for user to fix, do not want to continue just in case
                # there is actual data in this file as we do not want to overwrite and put
                # users boxturtles into a weird state if prep continues.
                self.afc.error.AFC_error(f"Error when trying to open and decode {self.afc.VarFile}.unit file.\n" + \
                                          "Please fix file or delete if file is empty, then restart klipper.", False)
                self.logger.error("", traceback=f"{e}")
                return

        else:
            error_string = 'Error: {}.unit file not found. Please check the path in the '.format(self.afc.VarFile)
            error_string += 'AFC.cfg file and make sure the file and path exists.'
            self.afc.error.AFC_error(error_string, False)

        # check if Lane is supposed to be loaded in tool head from saved file
        for extruder in self.afc.tools.keys():
            PrinterObject=self.afc.tools[extruder]
            self.afc.tools[PrinterObject.name]=PrinterObject
            if 'system' in units and "extruders" in units["system"]:
                # Check to see if lane_loaded is in dictionary and its not an empty string
                if PrinterObject.name in units["system"]["extruders"] and \
                  'lane_loaded' in units["system"]["extruders"][PrinterObject.name] and \
                  units["system"]["extruders"][PrinterObject.name]['lane_loaded']:
                    PrinterObject.lane_loaded = units["system"]["extruders"][PrinterObject.name]['lane_loaded']
                    self.afc.current = PrinterObject.lane_loaded

        for lane in self.afc.lanes.keys():
            cur_lane = self.afc.lanes[lane]


            cur_lane.unit_obj = self.afc.units[cur_lane.unit]
            if cur_lane.name not in cur_lane.unit_obj.lanes: cur_lane.unit_obj.lanes.append(cur_lane.name)    #add lanes to units list
            # If units section exists in vars file add currently stored data to AFC.units array
            if cur_lane.unit in units:
                if cur_lane.name in units[cur_lane.unit]:
                    if 'spool_id' in units[cur_lane.unit][cur_lane.name]: cur_lane.spool_id = units[cur_lane.unit][cur_lane.name]['spool_id']
                    if self.afc.spoolman is not None and cur_lane.spool_id:
                        self.afc.spool.set_spoolID(cur_lane, cur_lane.spool_id, save_vars=False)
                    else:
                        if 'material' in units[cur_lane.unit][cur_lane.name]:
                            cur_lane.material = units[cur_lane.unit][cur_lane.name]['material']
                        if 'color' in units[cur_lane.unit][cur_lane.name]:
                            cur_lane.color = units[cur_lane.unit][cur_lane.name]['color']
                        if 'weight' in units[cur_lane.unit][cur_lane.name]:
                            cur_lane.weight = units[cur_lane.unit][cur_lane.name]['weight']
                        if 'density' in units[cur_lane.unit][cur_lane.name]:
                            cur_lane.filament_density= units[cur_lane.unit][cur_lane.name]["density"]
                        if 'diameter' in units[cur_lane.unit][cur_lane.name]:
                            cur_lane.filament_diameter= units[cur_lane.unit][cur_lane.name]["diameter"]
                        if 'empty_spool_weight' in units[cur_lane.unit][cur_lane.name]:
                            cur_lane.empty_spool_weight= units[cur_lane.unit][cur_lane.name]["empty_spool_weight"]

                        if not isinstance(cur_lane.weight, int):
                            if cur_lane.weight:
                                cur_lane.weight = int(cur_lane.weight)
                            else:
                                cur_lane.weight = 0

                    if 'runout_lane' in units[cur_lane.unit][cur_lane.name]: cur_lane.runout_lane = units[cur_lane.unit][cur_lane.name]['runout_lane']
                    if cur_lane.runout_lane == '' or cur_lane.runout_lane == 'NONE': cur_lane.runout_lane = None
                    if 'map' in units[cur_lane.unit][cur_lane.name]: cur_lane.map = units[cur_lane.unit][cur_lane.name]['map']
                    if cur_lane.map != None:
                        self.afc.tool_cmds[cur_lane.map] = cur_lane.name
                    # Check first for hub_loaded as this was the old name in software with version <= 1030
                    if 'hub_loaded' in units[cur_lane.unit][cur_lane.name]: lane.loaded_to_hub = units[cur_lane.unit][cur_lane.name]['hub_loaded']
                    # Check for loaded_to_hub as this is how its being saved version > 1030
                    if 'loaded_to_hub' in units[cur_lane.unit][cur_lane.name]: cur_lane.loaded_to_hub = units[cur_lane.unit][cur_lane.name]['loaded_to_hub']
                    if 'tool_loaded' in units[cur_lane.unit][cur_lane.name]: cur_lane.tool_loaded = units[cur_lane.unit][cur_lane.name]['tool_loaded']
                    if 'td1_data' in units[cur_lane.unit][cur_lane.name]: cur_lane.td1_data = units[cur_lane.unit][cur_lane.name]['td1_data']
                    # Commenting out until there is better handling of this variable as it could cause someone to not be able to load their lane if klipper crashes
                    # if 'status' in units[cur_lane.unit][cur_lane.name]: cur_lane.status = units[cur_lane.unit][cur_lane.name]['status']

        for unit in self.afc.units.keys():
            try: cur_unit = self.afc.units[unit]
            except:
                error_string = 'Error: {} Unit not found in  config section.'.format(unit)
                self.afc.error.AFC_error(error_string, False)
                return
            self.logger.info('{} {} Prepping lanes'.format(cur_unit.type, unit))
            lanes_for_first_hub = []
            hub_name = ""
            LaneCheck = True
            for lane in cur_unit.lanes.values():
                # Used to print out warning message that multiple hubs are found
                if lane.multi_hubs_found:
                    lanes_for_first_hub.append(lane.name)
                    hub_name = lane.hub_obj.fullname

                if not cur_unit.system_Test(lane, self.delay, self.assignTcmd, self.enable):
                    LaneCheck = False
            # Warn user if multiple hubs were found and hub was not assigned to unit/stepper
            if len(lanes_for_first_hub) != 0:
                self.logger.raw("<span class=warning--text>No hub defined in lanes or unit for {unit}. Defaulting to {hub}</span>".format(
                                            unit=" ".join(cur_unit.full_name), hub=hub_name))

            if LaneCheck:
                self.logger.raw(cur_unit.logo)
            else:
                self.logger.raw(cur_unit.logo_error)
            overrall_status = overrall_status and LaneCheck
        try:
            if self.afc._get_bypass_state():
                self.logger.info("Filament loaded in bypass, toolchanges deactivated")
        except:
            pass

        self._td1_prep(overrall_status)
        # look up what current lane should be a call select lane, this is more for units that
        # have selectors to make sure the selector is on the correct lane
        current_lane = self.afc.function.get_current_lane_obj()
        if current_lane is not None:
            current_lane.unit_obj.select_lane(current_lane)
            current_lane.sync_to_extruder()

        # Restore previous bypass state if virtual bypass is active
        bypass_name = "Bypass"
        if 'virtual' in self.afc.bypass.name:
            bypass_name = "Virtual bypass"
            if "system" in units and 'bypass' in units["system"]:
                self.afc.bypass.filament_present = self.afc.bypass.sensor_enabled = units["system"]["bypass"]["enabled"]
            else:
                self.afc.bypass.filament_present = self.afc.bypass.sensor_enabled = False
        # Add warning message so users know that either bypass or virtual bypass is enabled
        if self.afc.bypass.filament_present:
            self.logger.raw(f"<span class=warning--text>{bypass_name} enabled</span>")

        self.afc.afc_stats.check_cut_threshold()

        # Defaulting to no active spool, putting at end so endpoint has time to register
        if self.afc.current is None:
            self.afc.spool.set_active_spool(None)
        # Setting value to False so the T commands don't try to get reassigned when users manually
        # run PREP after it has already been run once upon boot
        self.assignTcmd = False
        self.afc.prep_done = True
        self.afc.save_vars()

        if self.afc.buffers:
            for buffer_name, buffer_obj in self.afc.buffers.items():
                if buffer_obj.advance_state and buffer_obj.trailing_state:
                    self.logger.raw("<span class=warning--text>Warning: Both advance and trailing "
                                    "switches are triggered on Buffer {}. "
                                    "Please check your buffer switches or configuration.</span>".format(buffer_name))

def load_config(config):
    return afcPrep(config)

