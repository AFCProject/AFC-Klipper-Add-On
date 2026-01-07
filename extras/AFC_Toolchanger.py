# Armored Turtle Automated Filament Control
#
# Copyright (C) 2025 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# Toolchanging code is inspired and based off klipper-toolchanger project https://github.com/viesturz/klipper-toolchanger
# Originally authored by Viesturs Zariņš and licensed under the GNU General Public License v3.0.
#
# Full license text available at: https://www.gnu.org/licenses/gpl-3.0.html

from __future__ import annotations

import traceback
from configparser import Error as error

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from gcode import GCodeCommand
    from extras.AFC_lane import AFCLane
    from extras.AFC_functions import afcFunction

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_unit import afcUnit
except: raise error(ERROR_STR.format(import_lib="AFC_unit", trace=traceback.format_exc()))

try: from extras.AFC import State
except: raise error(ERROR_STR.format(import_lib="AFC", trace=traceback.format_exc()))

class AfcToolchanger(afcUnit):
    def __init__(self, config: ConfigWrapper) -> None:
        super().__init__(config)
        self.type = config.get("type", "Toolchanger")
        self.logo       = '<span class=success--text>Toolchanger Ready\n</span>'
        self.logo_error = '<span class=error--text>Toolchanger Not Ready</span>\n'
        self.functions: afcFunction = self.printer.load_object(config, 'AFC_functions')

        self.functions.register_commands(self.afc.show_macros, "AFC_SELECT_TOOL",
                                         self.cmd_AFC_SELECT_TOOL, self.cmd_AFC_SELECT_TOOL_help,
                                         self.cmd_AFC_SELECT_TOOL_options )

        self.functions.register_commands(self.afc.show_macros, "AFC_UNSELECT_TOOL",
                                         self.cmd_AFC_UNSELECT_TOOL, self.cmd_AFC_UNSELECT_TOOL_help)

        self.functions.register_commands(self.afc.show_macros, "AFC_SET_TOOLHEAD_LED",
                                         self.cmd_AFC_SET_TOOLHEAD_LED,
                                         self.cmd_AFC_SET_TOOLHEAD_LED_help,
                                         self.cmd_AFC_SET_TOOLHEAD_LED_options)

    def system_Test(self, cur_lane: AFCLane, delay: float, assignTcmd: str, enable_movement: bool):
        if assignTcmd: self.afc.function.TcmdAssign(cur_lane)
        # Now that a T command is assigned, send lane data to moonraker
        cur_lane.send_lane_data()
        self.logger.info( '{lane_name} tool cmd: {tcmd:3} {msg}'.format(lane_name=cur_lane.name, tcmd=cur_lane.map, msg=""))
        cur_lane.set_afc_prep_done()
        return True

    cmd_AFC_SELECT_TOOL_help = "Select specified tool"
    cmd_AFC_SELECT_TOOL_options = {
        "TOOL": {"type": "string", "default": "extruder"}
    }
    def cmd_AFC_SELECT_TOOL(self, gcmd: GCodeCommand):
        """
        Select's tool based off passed in extruder name

        Usage
        -----
        `AFC_SELECT_TOOL TOOL=<extruder_name>`

        Example
        -----
        ```
        AFC_SELECT_TOOL TOOL=extruder1
        ```
        """
        tool_key = gcmd.get("TOOL")
        tool = self.afc.tools.get(tool_key)

        if tool:
            if hasattr(tool, 'tc_lane') and tool.tc_lane is not None:
                self.tool_swap(tool.tc_lane)
            else:
                self.logger.error(f"Tool '{tool_key}' does not have a valid 'tc_lane' attribute.")
        else:
            self.logger.error(f"Key:{tool_key} invalid for TOOL")

    cmd_AFC_UNSELECT_TOOL_help = "Unselects and docks current tool on shuttle"
    def cmd_AFC_UNSELECT_TOOL(self, gcmd: GCodeCommand):
        """
        Unselects current tool loaded in shuttle by calling klipper-toolchanger UNSELECT_TOOL

        TODO: Update text once moved away from KTC

        Usage
        -----
        `AFC_UNSELECT_TOOL`

        Example
        -----
        ```
        AFC_UNSELECT_TOOL
        ```
        """
        self.afc.gcode.run_script_from_command("UNSELECT_TOOL")
        lane_obj = self.afc.function.get_current_lane_obj()
        if lane_obj:
            if (lane_obj.prep_state
                and lane_obj.load_state):
                if lane_obj.tool_loaded:
                    lane_obj.unit_obj.lane_tool_loaded_idle(lane_obj)
                else:
                    lane_obj.unit_obj.lane_tool_unloaded(lane_obj)
            else:
                lane_obj.extruder_obj.set_status_led(lane_obj.led_tool_unloaded)

        self.afc.spool.set_active_spool('')

    def tool_swap(self, lane):
        """
        Perform a tool swap operation for the specified lane.

        This function handles switching the active extruder/toolhead to the one associated with the given lane.
        It saves the current toolhead position, updates internal state, and issues a SELECT_TOOL command
        to switch to the correct extruder. This is primarily used in multi-extruder/toolchanger setups.

        :param lane: The lane object whose extruder/toolhead should be activated.

        :return: None
        """
        self.afc.current_state = State.TOOL_SWAP
        self.afc.function.log_toolhead_pos("Before toolswap: ")
        # Save the current position before switching tools and subtract offsets
        for i in range(0, 3):
            self.afc.last_gcode_position[i] -= self.afc.gcode_move.base_position[i]
        # Perform a tool swap by selecting the appropriate extruder based on the lane's extruder object.
        self.afc.afcDeltaTime.log_with_time("Performing tool swap")
        name = lane.extruder_obj.name
        tool_index = 0 if name == "extruder" else int(name.replace("extruder", ""))
        self.afc.gcode.run_script_from_command('SELECT_TOOL T={}'.format(tool_index))

        # Switching toolhead extruders, this is mainly for setups with multiple extruders
        lane.activate_toolhead_extruder()
        # Need to call again since KTC activate callback happens before switching to new extruder
        # Take double call out once transitioned away from KTC
        self.afc.function._handle_activate_extruder(0)

        self.afc.afcDeltaTime.log_with_time("Tool swap done")
        self.afc.current_state = State.IDLE
        # Update the base position and homing position after the tool swap.
        self.afc.base_position   = list(self.afc.gcode_move.base_position)
        self.afc.homing_position = list(self.afc.gcode_move.homing_position)
        # Update the last_gcode_position to reflect the new base position after the tool swap.
        for i in range(0, 3):
            self.afc.last_gcode_position[i] += self.afc.gcode_move.base_position[i]

        self.afc.function.log_toolhead_pos("After toolswap: ")

    cmd_AFC_SET_TOOLHEAD_LED_help = "Turns on leds for toolhead specified by mapping, does not affect status led if status_led_idx variable is provided"
    cmd_AFC_SET_TOOLHEAD_LED_options = {
        "MAP": {"type": "string", "default": "T0"},
        "TURN_ON": {"type": "int", "default": 1}
    }
    def cmd_AFC_SET_TOOLHEAD_LED(self, gcmd: GCodeCommand):
        """
        Macro call to set nozzle led in toolhead based on lane/tool mapping. Led config name needs to be
        set to AFC_extruder `led_name` variable. Status led in toolhead will not be affected if `status_led_idx`
        is set in AFC_extruder config. If `nozzle_led_idx` is set in AFC_extruder configuration then just
        those leds will be turned on. If `nozzle_led_inx` is not provided then all leds not in defined in
        `status_led_idx` will be turned on.

        `MAP` - is the mapped toolhead to set. ex(T0,T1,etc)

        `TURN_ON` - set to 1 to turn on leds, set to 0 to turn off leds. If not supplied, defaults to 1

        Usage
        -----
        `AFC_SET_TOOLHEAD_LED MAP=<T(n) mapping> TURN_ON=<0/1>`

        Example
        -----
        ```
        AFC_SET_TOOLHEAD_LED MAP=T2 TURN_ON=1
        ```

        """
        mapping = gcmd.get("MAP")
        state = gcmd.get_int("TURN_ON", 1)

        lane_obj = self.afc.function.get_lane_by_map(mapping)
        if lane_obj is None:
            error_string = f"{mapping} mapping not found when trying to set toolhead led"
            self.logger.error(error_string)
            raise gcmd.error(error_string)

        success, error_string = lane_obj.extruder_obj.set_print_leds(state)

        if not success:
            raise gcmd.error(error_string)

def load_config_prefix(config: ConfigWrapper):
    return AfcToolchanger(config)

def load_config(config: ConfigWrapper):
    return load_config_prefix(config)