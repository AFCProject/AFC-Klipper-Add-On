# AFCProject Automated Filament Changer Software
#
# Copyright (C) 2024-2026 Armored Turtle
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

from typing import Optional, Union, Any, Callable, TYPE_CHECKING
import copy
import traceback
import re

from configfile import error

try: from extras.AFC_utils import natural_sort_key
except: raise error("Error when trying to import AFC_utils.natural_sort_key\n{trace}".format(trace=traceback.format_exc()))

if TYPE_CHECKING:
    from gcode import GCodeCommand
    from configfile import ConfigWrapper
    from extras.AFC import afc
    from extras.AFC_lane import AFCLane

class AFCSpool:
    def __init__(self, config: ConfigWrapper) -> None:
        """
        Basic setup, actual initialization happens in handle_connect/register_commands
        once the AFC object is available.

        :param config: Klipper config wrapper for this section
        """
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.SPOOLMAN_REMOTE_METHOD = 'spoolman_set_active_spool'
        self.print_task_config_obj = None


        # Temporary status variables
        self.next_spool_id: Optional[int] = None

    def register_commands(self, afc: afc) -> None:
        """
        Method to register macros, this should be called after class has been initialized during the
        initialization phase.

        :param afc: AFC object to use
        """
        self.afc = afc
        self.function = afc.function
        self.enable_multiple_mapping = self.afc.enable_multiple_mapping
        self.function.register_commands(self.afc.show_macros, "AFC_ADD_MAPPING",
                                        self.cmd_AFC_ADD_MAPPING, self.cmd_AFC_ADD_MAPPING_help,
                                        self.cmd_AFC_ADD_MAPPING_options)
        self.function.register_commands(self.afc.show_macros, "AFC_REMOVE_MAPPING",
                                        self.cmd_AFC_REMOVE_MAPPING, self.cmd_AFC_REMOVE_MAPPING_help,
                                        self.cmd_AFC_REMOVE_MAPPING_options)
        self.function.register_commands(self.afc.show_macros, "AFC_ENABLE_MULTIPLE_MAPPING",
                                        self.cmd_AFC_ENABLE_MULTIPLE_MAPPING,
                                        self.cmd_AFC_ENABLE_MULTIPLE_MAPPING_help,
                                        self.cmd_AFC_ENABLE_MULTIPLE_MAPPING_options)
        self.function.register_commands(False, "AFC_SWAP_MAPPING",
                                        self.cmd_AFC_SWAP_MAPPING, self.cmd_AFC_SWAP_MAPPING_help)

    def handle_connect(self) -> None:
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up the AFC object
        and assigns it to the instance variable `self.AFC`.
        """
        self.error      = self.afc.error
        self.reactor    = self.afc.reactor
        self.gcode      = self.afc.gcode
        self.logger     = self.afc.logger
        self.print_task_config_obj = self.printer.lookup_object('print_task_config', None)

        self.disable_weight_check = self.afc.disable_weight_check

        # Registering stepper callback so that mux macro can be set properly with valid lane names
        self.printer.register_event_handler("afc_stepper:register_macros",self.register_lane_macros)

        self.gcode.register_command("AFC_RESET_MAPPING", self.cmd_AFC_RESET_MAPPING, desc=self.cmd_AFC_RESET_MAPPING_help)
        self.gcode.register_command("SET_NEXT_SPOOL_ID", self.cmd_SET_NEXT_SPOOL_ID, desc=self.cmd_SET_NEXT_SPOOL_ID_help)

    def register_lane_macros(self, lane_obj: AFCLane) -> None:
        """
        Callback function to register macros with proper lane names so that klipper errors out correctly when users supply lanes that
        are not valid

        :param lane_obj: object for lane to register
        """
        self.gcode.register_mux_command('SET_COLOR',            "LANE", lane_obj.name, self.cmd_SET_COLOR,              desc=self.cmd_SET_COLOR_help)
        self.gcode.register_mux_command('SET_WEIGHT',           "LANE", lane_obj.name, self.cmd_SET_WEIGHT,             desc=self.cmd_SET_WEIGHT_help)
        self.gcode.register_mux_command('SET_MATERIAL',         "LANE", lane_obj.name, self.cmd_SET_MATERIAL,           desc=self.cmd_SET_MATERIAL_help)
        self.gcode.register_mux_command('SET_SPOOL_ID',         "LANE", lane_obj.name, self.cmd_SET_SPOOL_ID,           desc=self.cmd_SET_SPOOL_ID_help)
        self.gcode.register_mux_command('SET_RUNOUT',           "LANE", lane_obj.name, self.cmd_SET_RUNOUT,             desc=self.cmd_SET_RUNOUT_help)
        self.gcode.register_mux_command('SET_MAP',              "LANE", lane_obj.name, self.cmd_SET_MAP,                desc=self.cmd_SET_MAP_help)
        self.gcode.register_mux_command('AFC_SET_SPOOL_TEMP',   "LANE", lane_obj.name, self.cmd_AFC_SET_SPOOL_TEMP,     desc=self.cmd_AFC_SET_SPOOL_TEMP_help)

    def set_snapmaker_filament_params(self, lane: AFCLane) -> None:
        """
        This method is only for snapmaker printers, this method updates filament color, material,
        vendor into snapmakers print_task_config object. This is needed so that the proper filament
        reflects correctly per toolhead on the screen UI and its needed to be able to properly resume
        a print. Without updating these values into print_task_config object snapmakers internal
        python logic will not resume once paused.

        Logic is based off this file in u1-klipper repo:
            https://github.com/Snapmaker/u1-klipper/blob/main/klippy/extras/print_task_config.py

        :param lane: AFC lane to update correct toolhead with color, material, vendor, etc information.
        """
        if (self.afc.snapmaker_printer
            and self.print_task_config_obj is not None
            and lane.tool_loaded
            and lane.name == lane.extruder_obj.lane_loaded):
            try:
                # Below could also be done with the following macro for multi color spools
                # SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=3 COLORS=123456,654321 COLOR_NUMS=2 MULTI_MODE=1
                from extras.print_task_config import DEFAULT_PRINT_TASK_CONFIG
                extruder_num = lane.lane_extruder_index

                tmp_config = copy.deepcopy(self.print_task_config_obj.print_task_config)

                tmp_config['filament_vendor'][extruder_num] = (lane.spool_vendor or "Generic")
                tmp_config['filament_type'][extruder_num] = (
                    lane.material
                    or getattr(self.afc, "default_material_type", None)
                    or "NONE"
                )
                tmp_config['filament_sub_type'][extruder_num] = "NONE"

                tmp_config['filament_color'][extruder_num] = int("FFFFFFFF", 16)
                tmp_config['filament_color_rgba'][extruder_num] = "FFFFFFFF"
                tmp_config['filament_color_multi'][extruder_num]["colors"] = ["FFFFFF"]
                tmp_config['filament_color_multi'][extruder_num]["mode"] = 0
                tmp_config['filament_color_multi'][extruder_num]["nums"] = 1

                if lane.color:
                    tmp_config['filament_color'][extruder_num] = int(lane.color.replace('#', ''), 16) | 0xFF000000
                    tmp_config['filament_color_rgba'][extruder_num] = lane.color.replace('#', '') + "FF"
                    tmp_config['filament_color_multi'][extruder_num]["colors"] = [f"{lane.color.replace('#', '')}"]

                if lane.multi_color:
                    tmp_config['filament_color_multi'][extruder_num]["nums"] = len(lane.multi_color)
                    tmp_config['filament_color_multi'][extruder_num]["colors"] = lane.multi_color
                    tmp_config['filament_color_multi'][extruder_num]["mode"] = 1

                self.print_task_config_obj.print_task_config = tmp_config
                self.printer.update_snapmaker_config_file(self.print_task_config_obj.config_path,
                                                          self.print_task_config_obj.print_task_config,
                                                          DEFAULT_PRINT_TASK_CONFIG)
            except Exception:
                self.logger.error("Error when trying to update colors for snapmaker print_task_config")
                self.logger.debug(traceback.format_exc())

    cmd_AFC_SET_SPOOL_TEMP_help = "Set spool temperatures for a lane"
    def cmd_AFC_SET_SPOOL_TEMP(self, gcmd: GCodeCommand) -> None:
        """
        This function handles setting the bed and extruder temperatures for a specified lane's spool.

        Usage
        -----
        `AFC_SET_SPOOL_TEMP LANE=<lane> BED_TEMP=<temp> EXTRUDER_TEMP=<temp>`

        Example
        -----
        ```
        AFC_SET_SPOOL_TEMP LANE=lane1 BED_TEMP=60 EXTRUDER_TEMP=210
        ```
        """
        lane = gcmd.get('LANE', None)
        if lane is None:
            self.logger.info("No LANE parameter provided, please specify a valid LANE parameter.")
            return
        cur_lane = self.afc.lanes.get(lane)
        if cur_lane is None:
            self.logger.info(f"{lane} Unknown")
            return
        cur_lane.bed_temp = gcmd.get_int('BED_TEMP', cur_lane.bed_temp, minval=0)
        cur_lane.extruder_temp = gcmd.get_int('EXTRUDER_TEMP', cur_lane.extruder_temp, minval=0)
        cur_lane.send_lane_data()
        self.afc.save_vars()

    cmd_SET_MAP_help = "Changes T(n) mapping for a lane"
    def cmd_SET_MAP(self, gcmd: GCodeCommand) -> None:
        """
        This function handles changing the GCODE tool change command for a Lane.

        Usage
        -----
        `SET_MAP LANE=<lane> MAP=<cmd>`

        Example
        -----
        ```
        SET_MAP LANE=lane1 MAP=T1
        ```
        """
        lane = gcmd.get('LANE')
        map_cmd = gcmd.get('MAP')
        map_cmd = map_cmd.upper()

        if map_cmd not in self.afc.tool_cmds:
            self.logger.error(f"Invalid map command: {map_cmd}")
            return

        lane_switch = self.afc.tool_cmds[map_cmd]
        self.logger.debug(f"lane to switch is {lane_switch}")

        cur_lane = self.afc.lanes.get(lane, None)
        if cur_lane is None:
            self.logger.info(f"{lane} Unknown")
            return

        sw_lane = self.afc.lanes.get(lane_switch, None)
        if sw_lane is None:
            self.logger.info(f"{lane_switch} is not found when swapping mapping.")
            return

        if sw_lane is cur_lane:
            self.logger.info(f"{map_cmd} is already mapped to {lane}")
            return

        if self.enable_multiple_mapping:
            sw_lane.map = [c for c in sw_lane.map if c != map_cmd]
            if sw_lane.current_map == map_cmd:
                sw_lane.current_map = ""
            # Reassigning (not appending/removing in place) so Klipper's status
            # diff sees a new list and pushes the update
            cur_lane.map = cur_lane.map + [map_cmd]
            cur_lane.map = [m for m in cur_lane.map if m != "NONE"]
        else:
            sw_lane.map = list(cur_lane.map)
            sw_lane.current_map = sw_lane.map[0] if sw_lane.map else ""

            cur_lane.map = [map_cmd]
            cur_lane.current_map = map_cmd

        for m in sw_lane.map:
            if m == "NONE": continue
            self.afc.tool_cmds[m] = sw_lane.name
        # cur_lane.map can't contain "NONE" here: the enable_multiple_mapping
        # branch filters it explicitly, and the else branch sets it to
        # [map_cmd], which is always a real mapping (already validated
        # against tool_cmds above), so no guard is needed for this loop.
        for m in cur_lane.map:
            self.afc.tool_cmds[m] = cur_lane.name
        sw_lane.send_lane_data()

        cur_lane.send_lane_data()

        self.afc.save_vars()

    cmd_AFC_SWAP_MAPPING_help = "Swaps mapping between two lanes as provided with the FROM/TO variables"
    def cmd_AFC_SWAP_MAPPING(self, gcmd: GCodeCommand) -> None:
        """
        This macro allows a way to easily swap mappings between two lanes, this macro is used during
        infinite spool runout so that a lanes current mappings correctly moves to the runout lane mapping.

        Usage
        -----
        `AFC_SWAP_MAPPING FROM=<lane name> TO=<lane name>`

        Example
        -----
        ```
        AFC_SWAP_MAPPING FROM=lane1 TO=lane2
        ```
        """
        lane_from = gcmd.get("FROM")
        lane_to   = gcmd.get("TO")

        if lane_from.lower() == lane_to.lower():
            raise gcmd.error(f"FROM({lane_from}) TO({lane_to}) values are the same, not swapping.")

        lane_from_obj = self.afc.lanes.get(lane_from)
        lane_to_obj   = self.afc.lanes.get(lane_to)

        if not lane_from_obj:
            raise gcmd.error(f"Swapping from {lane_from} not found")
        if not lane_to_obj:
            raise gcmd.error(f"Swapping to {lane_to} not found")

        lane_from_mapping = lane_from_obj.map
        lane_to_mapping = lane_to_obj.map
        lane_from_current = lane_from_obj.current_map

        lane_from_obj.current_map = lane_to_obj.current_map
        lane_from_obj.map = lane_to_mapping

        lane_to_obj.current_map = lane_from_current
        lane_to_obj.map = lane_from_mapping

        for map_str in lane_from_obj.map:
            if map_str == "NONE": continue
            self.afc.tool_cmds.update({map_str: lane_from_obj.name})
        for map_str in lane_to_obj.map:
            if map_str == "NONE": continue
            self.afc.tool_cmds.update({map_str: lane_to_obj.name})

        lane_from_obj.send_lane_data()
        lane_to_obj.send_lane_data()
        self.afc.save_vars()
        self.logger.info(f"Swapping mapping from({lane_from}) to({lane_to}) successful.")

    cmd_AFC_ADD_MAPPING_help = (
        "Adds a new T(n) macro that currently does not exist to a lane. Mapping can be passed in "
        "a comma separated list to add multiple to a single lane"
    )
    cmd_AFC_ADD_MAPPING_options = {"LANE": {"type": "string", "default": "lane1"},
                                   "MAPPING": {"type": "string", "default": "T0"}}
    def cmd_AFC_ADD_MAPPING(self, gcmd: GCodeCommand) -> None:
        """
        This macro adds passed in mapping to specified lane, allows the ability to create "virtual tools".
        The new maps will be registered into klipper so that they can be called correctly. Mappings
        can be passed in as a comma separated list to add multiple to a single lane.

        Multiple lane mappings need to be enabled first with `AFC_ENABLE_MULTIPLE_MAPPING ENABLE=1
        before running this command.

        Usage
        -----
        `AFC_ADD_MAPPING MAPPING=<mappings to be add>`

        Example
        -----
        ```
        AFC_ADD_MAPPING LANE=lane1 MAPPING=T5,T6
        ```
        """
        if not self.enable_multiple_mapping:
            raise gcmd.error("Enable multiple mapping needs to be enabled to add mappings,"
                                " enable by running AFC_ENABLE_MULTIPLE_MAPPING ENABLE=1")
        lane = gcmd.get("LANE")
        mapping: str = gcmd.get("MAPPING")
        mapping_list: list = mapping.upper().split(",")

        lane_obj = self.afc.lanes.get(lane, None)
        if not lane_obj:
            raise gcmd.error(f"{lane} is not a valid lane")

        for m in mapping_list:
            if not re.fullmatch(r"T\d+", m):
                self.afc.error.AFC_error(f"'{m}' is not a valid mapping, expect T(n) format.", False)
                continue

            if m in self.afc.tool_cmds:
                self.afc.error.AFC_error(f"Cannot map {m} to {lane}, as mapping already exists.", False)
                continue
            self.logger.info(f"Adding {m} to {lane}")
            self.afc.tool_cmds.update({m: lane})
            lane_obj.map = lane_obj.map + [m]
            self.function.register_tool_macro(lane, m)
        lane_obj.send_lane_data()
        self.afc.save_vars()

    cmd_AFC_REMOVE_MAPPING_help = (
        "Removes T(n) macro from specified lane, mapping can be passed in as comma separated list "
        "to remove multiple mappings from a lane"
    )
    cmd_AFC_REMOVE_MAPPING_options = {"MAPPING": {"type": "string", "default": "T0"}}
    def cmd_AFC_REMOVE_MAPPING(self, gcmd: GCodeCommand) -> None:
        """
        This macro removes specified mapping from a lanes and deregisters it in klipper so it cannot
        be called. Comma separated list can be passed in for mappings to be removed.

        Multiple lane mappings need to be enabled first with `AFC_ENABLE_MULTIPLE_MAPPING ENABLE=1
        before running this command.

        Usage
        -----
        `AFC_REMOVE_MAPPING MAPPING=<mappings to be removed>`

        Example
        -----
        ```
        AFC_REMOVE_MAPPING MAPPING=T5,T6
        ```
        """
        if not self.enable_multiple_mapping:
            raise gcmd.error("Enable multiple mapping needs to be enabled to remove mappings,"
                             " enable by running AFC_ENABLE_MULTIPLE_MAPPING ENABLE=1")
        mapping: str = gcmd.get("MAPPING")
        mapping_list: list = mapping.upper().split(",")

        for m in mapping_list:
            if m not in self.afc.tool_cmds:
                self.afc.error.AFC_error(f"Mapping {m} does not exist", False)
                continue
            lane = self.afc.tool_cmds.pop(m)
            lane_obj = self.afc.lanes.get(lane)
            if lane_obj:
                self.logger.info(f"Removing {m} macro from {lane}")
                lane_obj.map = [ c for c in lane_obj.map if c != m]
                if lane_obj.current_map == m:
                    lane_obj.current_map = lane_obj.map[0] if lane_obj.map else ""
                lane_obj.send_lane_data()
            self.gcode.register_command(m, None)
        self.afc.save_vars()

    cmd_AFC_ENABLE_MULTIPLE_MAPPING_help = (
        "Enable ability to have multiple T(n) macros mapped to a single lane and enables use"
        " of virtual tools"
    )
    cmd_AFC_ENABLE_MULTIPLE_MAPPING_options ={"ENABLE":{"type": "int", "default": 0}}
    def cmd_AFC_ENABLE_MULTIPLE_MAPPING(self, gcmd: GCodeCommand) -> None:
        """
        This macro enables the ability to have T(n) macros mapped to multiple lanes. Enabling
        multiple mapping also adds the ability to add virtual tools with AFC_ADD_MAPPING and
        AFC_REMOVE_MAPPING macros.

        When disabling multiple mapping, lanes mappings are also reset via AFC_RESET_MAPPING command.

        Usage
        -----
        `AFC_ENABLE_MULTIPLE_MAPPING ENABLE=<0/1>`

        Example
        -----
        ```
        AFC_ENABLE_MULTIPLE_MAPPING ENABLE=1
        ```
        """
        enable = bool(gcmd.get_int("ENABLE", 0))
        if enable:
            self.enable_multiple_mapping = True
            self.logger.info("Multiple T(n) mapping per lane has been enabled along with ability to "
                             "add virtual tools with AFC_ADD_MAPPING macro")
        else:
            self.enable_multiple_mapping = False
            self.logger.info("Multiple T(n) mapping per lane and virtual tools has been disabled")
            self._reset_mapping()

        self.afc.enable_multiple_mapping = self.enable_multiple_mapping
        self.function.ConfigRewrite("AFC", "enable_multiple_mapping", self.enable_multiple_mapping)

    cmd_SET_COLOR_help = "Set filaments color for a lane"
    def cmd_SET_COLOR(self, gcmd: GCodeCommand) -> None:
        """
        This function handles changing the color of a specified lane. It retrieves the lane
        specified by the 'LANE' parameter and sets its color to the value provided by the 'COLOR' parameter.
        The 'COLOR' parameter should be a hex color code.

        Usage
        -----
        `SET_COLOR LANE=<lane> COLOR=<color>`

        Example
        -----
        ```
        SET_COLOR LANE=lane1 COLOR=FF0000
        ```
        """
        lane = gcmd.get('LANE', None)
        if lane is None:
            self.logger.info("No LANE Defined")
            return
        color = gcmd.get('COLOR', '#000000')
        if lane not in self.afc.lanes:
            self.logger.info(f"{lane} Unknown")
            return
        cur_lane = self.afc.lanes[lane]
        cur_lane.color = f"#{color.replace('#', '')}"
        cur_lane.multi_color = []
        cur_lane.send_lane_data()
        # Refresh LED only if filament is loaded — empty lanes keep their state color
        if (cur_lane.load_state
            and cur_lane.unit in self.afc.units):
            cur_lane.unit_obj.lane_loaded(cur_lane,force=True)
        self.afc.save_vars()
        self.set_snapmaker_filament_params(cur_lane)

    cmd_SET_WEIGHT_help = "Sets filaments weight for a lane"
    def cmd_SET_WEIGHT(self, gcmd: GCodeCommand) -> None:
        """
        This function handles changing the weight remaining of a spool loaded in a specified lane. It retrieves the lane
        specified by the 'LANE' parameter and sets its weight to the value provided by the 'WEIGHT' parameter.

        Usage
        -----
        `SET_WEIGHT LANE=<lane> WEIGHT=<weight>`

        Example
        -----
        ```
        SET_WEIGHT LANE=lane1 WEIGHT=850
        ```
        """
        lane = gcmd.get('LANE', None)
        if lane is None:
            self.logger.info("No LANE Defined")
            return
        weight = gcmd.get_float('WEIGHT', 1000.0)
        if lane not in self.afc.lanes:
            self.logger.info(f"{lane} Unknown")
            return
        cur_lane = self.afc.lanes[lane]
        cur_lane.weight = weight
        cur_lane.send_lane_data()
        self.afc.save_vars()

    cmd_SET_MATERIAL_help = "Sets filaments material for a lane"
    def cmd_SET_MATERIAL(self, gcmd: GCodeCommand) -> None:
        """
        This function handles changing the material of a specified lane. It retrieves the lane
        specified by the 'LANE' parameter and sets its material to the value provided by the 'MATERIAL' parameter.

        Values
        ----
        MATERIAL - Material type to set to lane. eg. PLA, ASA, ABS, PETG etc.

        Optional Values
        ----
        DENSITY - Density value to assign to lane. If this is not provided then a default value will be selected based
                   of material. Current default values: PLA: 1.24, PETG:1.23, ABS:1.04, ASA:1.07
        DIAMETER - Diameter of filament, defaults to 1.75
        EMPTY_SPOOL_WEIGHT - Weight of spool once its empty. Defaults to 190.

        Usage
        -----
        `SET_MATERIAL LANE=<lane> MATERIAL=<material>`

        Example
        -----
        ```
        SET_MATERIAL LANE=lane1 MATERIAL=ABS
        ```
        """
        lane = gcmd.get('LANE', None)
        if lane is None:
            self.logger.info("No LANE Defined")
            return
        if lane not in self.afc.lanes:
            self.logger.info(f"{lane} Unknown")
            return
        cur_lane = self.afc.lanes[lane]
        density = gcmd.get_float('DENSITY', None)

        cur_lane.material = gcmd.get('MATERIAL')
        cur_lane.filament_diameter = gcmd.get_float('DIAMETER', cur_lane.filament_diameter)
        cur_lane.empty_spool_weight = gcmd.get_float('EMPTY_SPOOL_WEIGHT', cur_lane.empty_spool_weight)

        # Setting density if its not none, doing this after setting material as material setter
        # automatically sets density based on material name
        if density is not None:
            cur_lane.filament_density = density

        cur_lane.send_lane_data()
        self.afc.save_vars()
        self.set_snapmaker_filament_params(cur_lane)

    def set_active_spool(self, ID: Optional[Union[int, str]]) -> None:
        """
        Notifies Spoolman of the spool that is now active on the toolhead.

        :param ID: Spool ID to set as active, pass None to clear the active spool
        """
        webhooks = self.printer.lookup_object('webhooks')
        if self.afc.spoolman is not None:
            if (ID
                and ID is not None):
                id = int(ID)
            else:
                id = None

            args = {'spool_id' : id }
            try:
                webhooks.call_remote_method(self.SPOOLMAN_REMOTE_METHOD, **args)
            except self.printer.command_error as e:
                self.logger.error(f"Error trying to set active spool \n{e}")

    cmd_SET_SPOOL_ID_help = "Set lanes spoolman ID"
    def cmd_SET_SPOOL_ID(self, gcmd: GCodeCommand) -> None:
        """
        This function handles setting the spool ID for a specified lane. It retrieves the lane
        specified by the 'LANE' parameter and updates its spool ID, material, color, and weight
        based on the information retrieved from the Spoolman API.

        Usage
        -----
        `SET_SPOOL_ID LANE=<lane> SPOOL_ID=<spool_id>`

        Example
        -----
        ```
        SET_SPOOL_ID LANE=lane1 SPOOL_ID=12345
        ```
        """
        if self.afc.spoolman is not None:
            lane = gcmd.get('LANE', None)
            if lane is None:
                self.logger.info("No LANE Defined")
                return
            SpoolID: Union[int, str] = gcmd.get('SPOOL_ID', '')
            if lane not in self.afc.lanes:
                self.logger.info(f"{lane} Unknown")
                return

            cur_lane = self.afc.lanes[lane]
            # Check if spool id is already assigned to a different lane, don't assign to current lane if id
            # is already assigned
            if SpoolID != '':
                try:
                    SpoolID = int(SpoolID)
                except ValueError:
                    self.logger.error(f"Invalid spool ID: {SpoolID}")
                    return

                if (cur_lane.spool_id != SpoolID
                    and any(SpoolID == lane.spool_id for lane in self.afc.lanes.values())):
                    self.logger.error(f"SpoolId {SpoolID} already assigned to a lane, cannot assign to {lane}.")
                    return

            # Updating the active spool in Spoolman needs to wait for set_spoolID's
            # async fetch to actually apply the new spool_id to the lane first
            self.set_spoolID(cur_lane, SpoolID,
                             on_done=lambda: self._update_active_spool_if_current(cur_lane))

    def _update_active_spool_if_current(self, cur_lane: AFCLane) -> None:
        """
        Pushes a lane's spool ID to Spoolman as the active spool if this lane
        is currently loaded to the toolhead. Runs as set_spoolID()'s on_done
        callback, once the spool data has been fetched and assignment has finished.

        :param cur_lane: Lane that was just assigned/cleared a spool ID
        """
        if cur_lane.name == self.afc.current:
            self.set_active_spool(cur_lane.spool_id)

    def _get_filament_values( self, filament: dict, field: str, default: Any=None) -> Any:
        """
        Helper function for checking if field is set and returns value if it exists,
        otherwise returns None

        :param filament: Dictionary for filament values
        :param field:    Field name to check for in dictionary
        :return:         Returns value if field exists or None if field does not exist
        """
        value = default
        if field in filament:
            value = filament[field]
        return value

    def _set_values(self, cur_lane: AFCLane, on_done: Optional[Callable[[], None]] = None) -> None:
        """
        Helper function for setting lane spool values

        :param on_done: Called once this lane's spool values have actually settled, called
                        immediately if there's no next_spool_id to apply, or once set_spoolID's
                        async fetch resolves
        """
        # Always reset debounce on spool change
        cur_lane.auto_switch_triggered = False
        # Only apply defaults (material type, 1000g weight) when no spool data has been
        # assigned to this lane. If SET_SPOOL_ID or SET_COLOR was called before loading
        # (e.g. from an NFC tag scan), the spool_id and/or color will already be set with
        # real values — don't overwrite them with defaults during the load sequence.
        if (not cur_lane.remember_spool
            and cur_lane.spool_id is None
            and not cur_lane.color):
            cur_lane.material = self.afc.default_material_type
            cur_lane.weight = 1000 # Defaulting weight to 1000 upon load

        if (self.afc.spoolman is not None
            and self.next_spool_id is not None):
            spool_id = self.next_spool_id
            self.next_spool_id = None
            self.set_spoolID(cur_lane, spool_id, on_done=on_done)
        elif on_done is not None:
            on_done()

    def clear_values(self, cur_lane: AFCLane) -> None:
        """
        Helper function for clearing out lane spool values
        """
        cur_lane.spool_id = None
        cur_lane.material = ''
        cur_lane.color = ''
        cur_lane.multi_color = []
        cur_lane.weight = 0
        cur_lane.auto_switch_triggered = False
        cur_lane.extruder_temp = None
        cur_lane.bed_temp = None
        cur_lane.clear_lane_data()
        cur_lane.spool_vendor = ""
        cur_lane.filament_name = ""

    def set_spoolID(self, cur_lane: AFCLane, SpoolID: Optional[Union[int, str]],
                    save_vars: bool = True, on_done: Optional[Callable[[], None]] = None) -> None:
        """
        Assigns a spool from Spoolman to a lane, pulling material, color, weight,
        and temperature values from Spoolman and updating the lane accordingly.
        Clears the lane's values instead when SpoolID is empty/None and the lane
        isn't set to remember its spool. Fetching Spoolman data is async (see
        _apply_spool_data) since this can be called from a load sequence and
        shouldn't block on the HTTP round trip.

        :param cur_lane:  AFC lane to assign the spool to
        :param SpoolID:   Spoolman spool ID to assign, or None/'' to clear
        :param save_vars: Whether to save AFC vars after assigning
        :param on_done:   Called once this lane's spool data has actually been
                          applied (immediately for the synchronous branches
                          below, or once the async Spoolman fetch resolves)
        """
        if (self.afc.spoolman is not None
            and self.afc.moonraker is not None):
            if (SpoolID is not None
                and SpoolID != ''):
                spool_id_value: Union[int, str] = SpoolID
                try:
                    spool_id_int = int(SpoolID)
                except Exception as e:
                    self.afc.error.AFC_error(f"Error when trying to get Spoolman data for ID:{SpoolID}, Error: {e}", False)
                else:
                    self.afc.moonraker.get_spool(
                        spool_id_int,
                        lambda result: self._apply_spool_data(cur_lane, spool_id_value, result, save_vars, on_done))
                    return
            elif not cur_lane.remember_spool:
                self.clear_values(cur_lane)
        elif not cur_lane.remember_spool:
            # Clears out values if users are not using spoolman and lane isn't set to remember spool,
            # this is to cover this function being called from LANE UNLOAD and clearing out
            # Manually entered information
            self.clear_values(cur_lane)

        self.set_snapmaker_filament_params(cur_lane)

        if save_vars: self.afc.save_vars()
        if on_done is not None:
            on_done()

    def _apply_spool_data(self, cur_lane: AFCLane, SpoolID: Union[int, str],
                          result: Optional[dict], save_vars: bool,
                          on_done: Optional[Callable[[], None]] = None) -> None:
        """
        Applies a Spoolman spool lookup result to a lane: material, color,
        weight, temperatures, etc. Runs as the completion callback for
        set_spoolID()'s async fetch, once moonraker's response arrives.

        :param cur_lane:  AFC lane to assign the spool to
        :param SpoolID:   Spoolman spool ID that was queried
        :param result:    Spool data dict returned by moonraker, None on failure
        :param save_vars: Whether to save AFC vars after assigning
        :param on_done:   Called once this attempt to apply spool data is finished,
                          on every exit path (success, invalid weight, or error)
        """
        try:
            try:
                if result is None:
                    raise ValueError(f"No spool data returned for SpoolID {SpoolID}")

                cur_lane.spool_id = SpoolID
                cur_lane.auto_switch_triggered = False

                cur_lane.material           = self._get_filament_values(result['filament'], 'material')
                if not self.afc.ignore_spoolman_material_temps:
                    cur_lane.extruder_temp      = self._get_filament_values(result['filament'], 'settings_extruder_temp')
                cur_lane.bed_temp           = self._get_filament_values(result['filament'], 'settings_bed_temp')
                cur_lane.filament_density   = self._get_filament_values(result['filament'], 'density')
                cur_lane.filament_diameter  = self._get_filament_values(result['filament'], 'diameter')
                cur_lane.empty_spool_weight = self._get_filament_values(result, 'spool_weight', default=190)
                cur_lane.weight             = self._get_filament_values(result, 'remaining_weight')
                full_weight                 = self._get_filament_values(result, 'initial_weight', default=1000)
                cur_lane.espooler.espooler_values.full_weight = full_weight
                cur_lane.filament_name      = self._get_filament_values(result['filament'], 'name', default="")

                vendor_result  = result["filament"].get("vendor", None)
                cur_lane.spool_vendor       = ""
                if vendor_result:
                    cur_lane.spool_vendor   = self._get_filament_values(vendor_result, "name", None)

                weight_check = self.disable_weight_check

                self.afc.logger.debug(f"Weight remaining for SpoolID {SpoolID}: {cur_lane.weight}")

                if not weight_check:
                    if (cur_lane.weight is None
                        or cur_lane.weight <= 0):
                        error_str = (f"Invalid weight for spoolID: {SpoolID}. "
                                    "Please check remaining weight before assigning.")
                        self.afc.error.AFC_error(error_str, False)
                        self.clear_values(cur_lane)
                        return

                # Check to see if filament is defined as multi-color and take the first color for now
                # Once support for multicolor is added this needs to be updated
                multi_color_hex = self._get_filament_values(result['filament'], 'multi_color_hexes')
                if multi_color_hex:
                    cur_lane.multi_color = multi_color_hex.split(",")
                    cur_lane.color = f"#{cur_lane.multi_color[0]}"
                else:
                    cur_lane.color = f"#{self._get_filament_values(result['filament'], 'color_hex')}"
                    cur_lane.multi_color = []

                cur_lane.send_lane_data()
                # Refresh LED only if filament is loaded — empty lanes keep their state color
                if (cur_lane.load_state
                    and cur_lane.unit in self.afc.units):
                        cur_lane.unit_obj.lane_loaded(cur_lane, force=True)

            except Exception as e:
                self.afc.error.AFC_error(f"Error when trying to get Spoolman data for ID:{SpoolID}, Error: {e}", False)
        finally:
            self.set_snapmaker_filament_params(cur_lane)
            if save_vars: self.afc.save_vars()
            # Runs on every exit path (early return, exception, or the happy
            # path) so on_done fires exactly once regardless of which branch
            # was taken -- matching the original synchronous behavior where
            # callers resumed right after set_spoolID() returned either way.
            if on_done is not None:
                on_done()

    cmd_SET_RUNOUT_help = "Set runout lane"
    def cmd_SET_RUNOUT(self, gcmd: GCodeCommand) -> None:
        """
        This function handles setting the runout lane (infinite spool) for a specified lane. It retrieves the lane
        specified by the 'LANE' parameter and updates it's the lane to use if filament runs out by un-triggering prep sensor.

        Usage
        -----
        `SET_RUNOUT LANE=<lane> RUNOUT=<lane>`

        Example
        -----
        ```
        SET_RUNOUT LANE=lane1 RUNOUT=lane4
        ```
        """
        lane = gcmd.get('LANE', None)
        if lane is None:
            self.logger.info("No LANE Defined")
            return

        runout = gcmd.get('RUNOUT', 'NONE')
        is_none = runout.upper() == 'NONE'
        # Check to make sure runout does not equal lane
        if lane == runout:
            self.logger.error(f"Lane({lane}) and runout({runout}) cannot be the same")
            return
        # Check to make sure specified lane exists
        if lane not in self.afc.lanes:
            self.logger.error(f"Unknown lane: {lane}")
            return
        # Check to make sure specified runout lane exists as long as runout is not set as 'NONE'
        if (not is_none
            and runout not in self.afc.lanes):
            self.logger.error(f"Unknown runout lane: {runout}")
            return

        cur_lane = self.afc.lanes[lane]
        cur_lane.runout_lane = None if is_none else runout
        self.afc.save_vars()

    cmd_AFC_RESET_MAPPING_help = "Resets all lane mapping in AFC"
    def cmd_AFC_RESET_MAPPING(self, gcmd: GCodeCommand) -> None:
        """
        Resets all tool lane mapping to the order set up in the configuration. If multiple tool
        mapping is enabled, when reset is called and virtual tools have been added, the virtual
        tools will be removed since reset will always revert back to a 1 to 1 mapping.

        Optionally resets runout lanes unless RUNOUT=no is specified.

        Useful to put in your PRINT_END macro to reset mapping

        Usage
        -----
        `AFC_RESET_MAPPING [RUNOUT=yes|no]`

        Example
        -----
        ```
        AFC_RESET_MAPPING RUNOUT=no
        ```
        """
        # Resetting runout lanes to None
        runout_opt = gcmd.get('RUNOUT', 'yes').lower()
        self._reset_mapping(runout_opt)

    def _reset_mapping(self, runout_opt: str = 'no') -> None:
        """
        Resets all lane mapping back to a 1:1 T(n) assignment, renumbering lanes
        sequentially so adding or removing a unit doesn't leave gaps or stale
        high numbers behind.

        :param runout_opt: "no" leaves runout lanes untouched, anything else clears them
        """
        # Commands in use before the reset, used further down to spot leftovers
        previously_used_cmds = {
            cmd for lane in self.afc.lanes.values() for cmd in lane.map if cmd != "NONE"
        }
        # Gather manually assigned mappings and add to list
        manually_assigned = {cmd for lane in self.afc.lanes.values() for cmd in lane._map}
        # Fresh T0..T(n-1) sequence for auto lanes, skipping manually assigned numbers
        total_lanes = sum(len(unit.lanes) for unit in self.afc.units.values())
        # Generate enough candidates that reserved manual numbers cannot starve
        # the auto lanes
        auto_cmds = [f"T{i}" for i in range(total_lanes + len(manually_assigned))
                     if f"T{i}" not in manually_assigned][:total_lanes]
        for unit in self.afc.units.values():
            for lane in unit.lanes.values():
                lane.map = []
                # Reassigning manually assigned mapping to lane
                if lane._map:
                    map_cmd = lane._map[0]
                else:
                    map_cmd = auto_cmds.pop(0)

                # New commands not already registered need to be registered in klipper
                if map_cmd not in previously_used_cmds:
                    self.function.register_tool_macro(lane.name, map_cmd)

                self.afc.tool_cmds[map_cmd] = lane.name
                lane.current_map = map_cmd
                lane.map = [map_cmd]

        # Remove commands no longer assigned to a lane (virtual-tool extras, or
        # leftovers from a removed unit), popped before send_lane_data() below
        # so it sees them as gone rather than still owned by this lane.
        new_cmds = {lane.map[0] for unit in self.afc.units.values() for lane in unit.lanes.values()}
        stale_cmds = sorted(previously_used_cmds - new_cmds, key=natural_sort_key)
        if stale_cmds:
            self.logger.info(f"{', '.join(stale_cmds)} remain, removing these mappings")
            for cmd in stale_cmds:
                self.afc.tool_cmds.pop(cmd, None)
                self.gcode.register_command(cmd, None)

        # Run after every lane is reassigned so send_lane_data() sees the final mapping
        for unit in self.afc.units.values():
            for lane in unit.lanes.values():
                lane.send_lane_data()

        if runout_opt != 'no':
            for lane in self.afc.lanes.values():
                lane.runout_lane = None

        self.afc.save_vars()
        self.logger.info("Tool mappings reset" + ("" if runout_opt == "no" else " and runout lanes reset"))

    cmd_SET_NEXT_SPOOL_ID_help = "Set the spool id to be loaded next into AFC"
    def cmd_SET_NEXT_SPOOL_ID(self, gcmd: GCodeCommand) -> None:
        """
        Sets the spool ID to be loaded next into the AFC.

        This can be used in a scanning macro to prepare the AFC for the next spool to be loaded.

        Omit the SPOOL_ID parameter to clear the next spool ID.

        Usage
        -----
        `SET_NEXT_SPOOL_ID SPOOL_ID=<spool_id>`

        Example
        -----
        ```
        SET_NEXT_SPOOL_ID SPOOL_ID=12345
        ```
        """
        SpoolID = gcmd.get('SPOOL_ID', '')
        previous_id = self.next_spool_id
        if SpoolID != '':
            try:
                self.next_spool_id = int(SpoolID)
            except ValueError:
                self.logger.error(f"Invalid spool ID: {SpoolID}")
                self.next_spool_id = None
        else:
            self.next_spool_id = None
        if previous_id:
            self.logger.info(f"Spool ID '{previous_id}' being overwritten for next load: '{self.next_spool_id}'")
        else:
            self.logger.info(f"Spool ID set for next load: '{self.next_spool_id}'")

def load_config(config: ConfigWrapper) -> AFCSpool:
    """
    Klipper config entry point for this module.

    :param config: Klipper config wrapper for this section
    """
    return AFCSpool(config)
