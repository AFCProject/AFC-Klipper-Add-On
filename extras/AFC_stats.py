# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import traceback

from configparser import Error as error
from typing import TYPE_CHECKING, Any, Optional

try: from extras.AFC_utils import check_and_return
except: raise error("Error when trying to import AFC_utils.check_and_return\n{trace}".format(trace=traceback.format_exc()))

if TYPE_CHECKING:
    from extras.AFC_utils import AFC_moonraker
    from extras.AFC_logger import AFC_logger
    from extras.AFC import afc

class AFCStats_var:
    """
    Holds a single value for stat tracking. Also has common functions to
    increment, reset, set time and average time for time values. This class also
    has the ability to update moonrakers database with value.

    Upon initializing this class, value is retrieved from dictionary if it exists
    and sets internal value to this, or zero if it does not exist.

    Parameters
    ----------------
    parent_name : string
        Parents name to store value into moonraker database
    name : string
        Name to store value into moonrakers database
    data : dictionary
        Dictionary of current afc_stat values stored in moonraker database
    moonraker : AFC_moonraker
        AFC_moonraker class to easily post values to moonraker
    """
    def __init__(self, parent_name: str, name: str, data: dict, moonraker: AFC_moonraker,
                 new_parent_name: str='', new_average: bool=False):
        self.parent_name = parent_name
        self.name        = name
        self.moonraker   = moonraker
        self.new_average = new_average
        self._value: Any = 0

        multi_parent = self.parent_name.split('.')
        new_multi_parent = new_parent_name.split('.')
        if data is not None:
            if len(new_multi_parent) > 0 and new_multi_parent[0] in data:
                self._get_value(data, new_multi_parent)
            elif multi_parent[0] in data:
                self._get_value(data, multi_parent)
            else:
                self.moonraker.logger.debug("No data in database for {}.{}:{}".format( self.parent_name, name, bool(data is not None)))
                self._value = 0

        # Check if new parent name has been passed in. If new parent override old parent
        # name and delete key from database if its present.
        if new_parent_name:
            self.parent_name = new_parent_name
            if data is not None and multi_parent[0] in data:
                self.update_database()
                multi_parent.append(name)
                self.moonraker.remove_database_entry(self.moonraker.afc_stats_key, '.'.join(multi_parent))

    def __str__(self):
        return str(self._value)

    @property
    def value(self):
        return self._value
    @value.setter
    def value(self, value):
        self._value = value

    def _get_value(self, data: dict, parent_name: list):
        """
        Helper function to get correct data from dictionary based off parents name

        :param parent_name: List in correct order of parent key name in database.
                            ex. extruder.cut -> ['extruder', 'cut']
        """
        if len(parent_name) == 1:
            value = check_and_return( self.name, data[parent_name[0]])
        elif len(parent_name) == 2:
            if parent_name[1] in data[parent_name[0]]:
                value = check_and_return( self.name, data[parent_name[0]][parent_name[1]])
            else:
                self.moonraker.logger.debug("No data in database for {}.{}:{}".format( '.'.join(parent_name), self.name, bool(data is not None)))
                value = 0
        else:
            self.moonraker.logger.error("Cannot have more than two parent names for stats")
            value = 0
        try:
            self._value = int(value)
        except ValueError:
            try :
                self._value = float(value)
            except:
                self._value = value

    def average_time(self, value: float):
        """
        Helper function for averaging time, moonrakers database is updated after
        averaging numbers. If AFC is using new average calculation, then values are just
        totaled up. Calculating averages will happen when printing out stats.

        :param value: Float value to average or sum into current value
        """
        if self._value > 0:
            self._value += value
            if not self.new_average:
                self._value /= 2
        else:
            self._value = value

        self.update_database()

    def get_average(self, total: int) -> float:
        """
        Helper function for returning average. If AFC is using new calculation, average
        is calculated and returned.

        :param total: Total number of loads, only used with new AFC calculations
        :return float: Average value
        """
        if int(self.new_average):
            if total > 0:
                return self._value/total
            else:
                return self._value
        else:
            return self._value

    def increase_count(self):
        """
        Helper function to easily increment count and updates moonrakers database
        """
        self._value += 1
        self.update_database()

    def reset_count(self):
        """
        Helper function to easily reset count and updates moonrakers database
        """
        self._value = 0
        self.update_database()
        self.new_average = True

    def update_database(self):
        """
        Calls AFC_moonraker update_afc_stats function with correct key, value to update
        value in moonrakers database
        """
        self.moonraker.update_afc_stats(f"{self.parent_name}.{self.name}", self._value)

    def set_current_time(self):
        """
        Grabs current date/time, sets variable and updates in moonrakers database.
        Use only for variables that are dates
        """
        from datetime import datetime
        time = datetime.now()
        self._value = time.strftime("%Y-%m-%d %H:%M")
        self.update_database()

class AFCStats:
    """
    This class holds the following AFC statistics:
        toolchange count: total, unload, load, number of changes without errors,
                            last time error occurred
        cut counts: total, number of cuts since last blade change, date when blade was last changed
        average time: total toolchange, unload, load

    Parameters
    ----------------
    moonraker: AFC_moonraker
        AFC_moonraker class is passed into AFCStats_var class for easily updating values in database
    logger: AFC_logger
        AFC_logger class for logging and printing to console
    """
    def __init__(self, moonraker: AFC_moonraker, logger: AFC_logger, multiple_tools: bool=False):
        self.moonraker  = moonraker
        self.logger     = logger
        self.multiple_tools = multiple_tools
        values       = self.moonraker.get_afc_stats()

        self.tc_without_error   = AFCStats_var("toolchange_count", "changes_without_error", values,
                                               self.moonraker, "error_stats")
        self.tc_last_load_error = AFCStats_var("toolchange_count", "last_load_error", values,
                                               self.moonraker, "error_stats")

        new_average_calc = 1
        if values is not None and "average_time" in values:
            new_average_calc = 0

        self.new_average_calc           = AFCStats_var("average_time", "new_average_calc", values,
                                                       self.moonraker)
        if self.new_average_calc.value == 0:
            self.new_average_calc.value = new_average_calc
            self.new_average_calc.update_database()

        self.average_toolchange_time    = AFCStats_var("average_time", "tool_change", values,
                                                       self.moonraker, new_average=self.new_average_calc.value)
        self.average_tool_unload_time   = AFCStats_var("average_time", "tool_unload", values,
                                                       self.moonraker, new_average=self.new_average_calc.value)
        self.average_tool_load_time     = AFCStats_var("average_time", "tool_load",   values,
                                                       self.moonraker, new_average=self.new_average_calc.value)


        if self.tc_last_load_error.value == 0:
            self.tc_last_load_error.value = "N/A"
            self.tc_last_load_error.update_database()

        self.average_tool_swap_time: Optional[AFCStats_var]     = None
        if self.multiple_tools:
            self.logger.debug("Multiple tools detected for stats")
            self.average_tool_swap_time     = AFCStats_var("average_time", "tool_swap",   values,
                                                           self.moonraker, new_average=self.new_average_calc.value)

    def increase_toolchange_wo_error(self):
        """
        Common function to increase toolchange total value when errors do not occur.
        """
        self.tc_without_error.increase_count()

    def reset_toolchange_wo_error(self):
        """
        Helper function for resetting number of toolchanges without errors and
        sets last error date/time as current
        """
        self.tc_without_error.reset_count()
        self.tc_last_load_error.set_current_time()

    def reset_average_times(self):
        """
        Common function to easily reset all change times. Once resetting AFC will start
        using new calculation function when tracking and printing stats.
        """
        self.average_toolchange_time.reset_count()
        self.average_tool_unload_time.reset_count()
        self.average_tool_load_time.reset_count()
        if self.average_tool_swap_time:
            self.average_tool_swap_time.reset_count()
        self.new_average_calc.value = 1
        self.new_average_calc.update_database()

    def print_stats(self, afc_obj: afc, short: bool=False):
        """
        Prints all stat to console

        :param afc_obj: AFC class that hold lane information so lanes stats can also be printed out
        :param short: When set to True print out skinner version.
        """
        MAX_WIDTH = 87
        SECTIONAL_LENGTH = 42
        MAX_SPAN = SECTIONAL_LENGTH*2+1
        END = "|\n"
        total_selected = 0
        total_unselected = 0
        total_changes = 0
        total_unload = 0
        total_load = 0

        def end_string():
            nonlocal print_str, temp_str
            print_str += f"{temp_str:{' '}<{86}}|\n"
            temp_str = ""

        def add_end():
            nonlocal short
            if short:
                return END
            else:
                return ''

        max_lane_name_size = 0
        for k in afc_obj.lanes.keys():
            max_lane_name_size = max(max_lane_name_size, len(k))

        if short:
            MAX_WIDTH = 44
            MAX_SPAN = MAX_WIDTH - 2

        overall_str  = f"{'':{'-'}<{MAX_WIDTH}}\n"
        overall_str += f"|{'Overall Stats':{' '}^{MAX_WIDTH-2}}|\n"
        overall_str += f"|{'Changes without error':{' '}>22} : {self.tc_without_error.value:{' '}<17}"
        overall_str += add_end()
        overall_str += f"|{'Last error date':{' '}>22} : {self.tc_last_load_error.value:{' '}<17}|\n"


        print_str = ""
        print_str += f"|{'':{'-'}<{MAX_WIDTH-2}}|\n"
        if not short:
            print_str += f"|{'Toolchanges':{' '}^{SECTIONAL_LENGTH}}"
            print_str += f"|{'Cut':{' '}^{SECTIONAL_LENGTH}}|\n"
            print_str += f"|{'':{'-'}<{MAX_WIDTH-2}}|\n"

        for extruder in afc_obj.tools.values():
            cut_str = f"{extruder.name} Cut"
            cut_str = f"|{cut_str:{' '}^{MAX_SPAN}}|\n"
            stats = extruder.estats
            cuts_since_change = f"{stats.cut_total_since_changed.value}/{stats.cut_threshold_for_warning}"

            total_changes += stats.tc_total.value
            total_unload += stats.tc_tool_unload.value
            total_load += stats.tc_tool_load.value
            extruder_lbl = f"{extruder.name} Toolchanges" if short else f"{extruder.name}"
            print_str += f"|{extruder_lbl:{' '}^{MAX_SPAN}}|\n"

            print_str += f"|{'Successful Changes':{' '}>22} : {stats.tc_total.value:{' '}<17}"
            print_str += add_end()
            string = f"|{'Total':{' '}>22} : {stats.cut_total.value:{''}<17}|\n"
            if short: cut_str += string
            else: print_str += string

            print_str += f"|{'Tool Unload':{' '}>22} : {stats.tc_tool_unload.value:{' '}<17}"
            print_str += add_end()
            string = f"|{'Total since changed':{' '}>22} : {cuts_since_change:{''}<17}|\n"
            if short: cut_str += string
            else: print_str += string

            print_str += f"|{'Tool Load':{' '}>22} : {stats.tc_tool_load.value:{' '}<17}"
            print_str += add_end()
            string = f"|{'Blade last changed':{' '}>22} : {stats.last_blade_changed.value:{''}<17}|\n"
            if short: cut_str += string
            else: print_str += string

            if self.multiple_tools:
                total_selected += stats.tool_selected.value
                total_unselected += stats.tool_unselected.value
                print_str += f"|{'Tool Unselected':{' '}>22} : {stats.tool_unselected.value:{' '}<17}"
                if not short: print_str += f"|{' ':{' '}^{SECTIONAL_LENGTH}}|\n"
                else: print_str += add_end()
                print_str += f"|{'Tool Selected':{' '}>22} : {stats.tool_selected.value:{' '}<17}"
                if not short: print_str += f"|{' ':{' '}^{SECTIONAL_LENGTH}}|\n"
                else: print_str += add_end()

            if short:
                print_str += f"|{'':{'-'}<{MAX_WIDTH-2}}|\n"
                print_str += cut_str

            print_str += f"|{'':{'-'}<{MAX_WIDTH-2}}|\n"

        avg_tool_load   = f"{'Avg Tool Load':{' '}>22} : {self.average_tool_load_time.get_average(total_load):4.2f}s"
        avg_tool_unload = f"{'Avg Tool Unload':{' '}>22} : {self.average_tool_unload_time.get_average(total_unload):4.2f}s"
        avg_tool_change = f"{'Avg Tool Change':{' '}>22} : {self.average_toolchange_time.get_average(total_changes):4.2f}s"
        if self.average_tool_swap_time:
            avg_tool_swap = f"{'Avg Tool Swap':{' '}>22} : {self.average_tool_swap_time.get_average(total_selected):4.2f}s"
        else:
            avg_tool_swap = ""


        avg_str = f"|{avg_tool_load:{' '}<{SECTIONAL_LENGTH}}"
        avg_str += add_end()
        avg_str += f"|{avg_tool_unload:{' '}<{SECTIONAL_LENGTH}}|\n"
        avg_str += f"|{avg_tool_change:{' '}<{SECTIONAL_LENGTH}}"
        avg_str += add_end()
        if (not short
            or short and avg_tool_swap):
            avg_str += f"|{avg_tool_swap:{' '}<{SECTIONAL_LENGTH}}|\n"

        if self.multiple_tools:
            avg_str += f"|{'Total Load':{' '}>22} : {total_load:{' '}<{17}}"
            avg_str += add_end()
            avg_str += f"|{'Total Unload':{' '}>{22}} : {total_unload:{' '}<17}|\n"
            avg_str += f"|{'Total Selected':{' '}>22} : {total_selected:{' '}<17}"
            avg_str += add_end()
            avg_str += f"|{'Total Unselected':{' '}>{22}} : {total_unselected:{' '}<17}|\n"


        print_str = overall_str + avg_str + print_str

        strings = []
        for lane in afc_obj.lanes.values():
            espooler_stats = lane.espooler.get_spooler_stats(short)
            string = f"{lane.name:{' '}>{max(max_lane_name_size,7)}} : Lane change count: {lane.lane_load_count.value:{' '}>7}"
            if short: string = f"|{string:{' '}^{MAX_SPAN}}|\n"
            if len(espooler_stats) > 0:
                if short: string += f"|{espooler_stats:{' '}^{MAX_WIDTH}}|\n"
                else: string += f"    {espooler_stats}"
            strings.append(string)

        if short:
            print_str += "".join(strings)
        else:
            temp_str = ""
            for i, s in enumerate(strings):
                if len(temp_str) > 60: end_string()

                if len(s) > 60:
                    if len(temp_str) > 0: end_string()

                    print_str += f"|{s}{'|':{' '}>4}\n"
                else:
                    temp_str += f"|{s:{' '}^{SECTIONAL_LENGTH}}"
            if len(temp_str) > 0: end_string()

        print_str += f"{'':{'-'}<{MAX_WIDTH}}\n"
        self.logger.raw(print_str)