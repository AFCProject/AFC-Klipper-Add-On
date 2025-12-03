# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import traceback

from configparser import Error as error

try: from extras.AFC_utils import check_and_return
except: raise error("Error when trying to import AFC_utils.check_and_return\n{trace}".format(trace=traceback.format_exc()))

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
    def __init__(self, parent_name:str, name:str, data:dict, moonraker:object):
        self.parent_name = parent_name
        self.name        = name
        self.moonraker   = moonraker

        if data is not None and self.parent_name in data:
            value = check_and_return( self.name, data[self.parent_name])
            try:
                self._value = int(value)
            except ValueError:
                try :
                    self._value = float(value)
                except:
                    self._value = value
        else:
            self.moonraker.logger.debug("No data in database for {}.{}:{}".format( self.parent_name, name, bool(data is not None)))
            self._value = 0

    def __str__(self):
        return str(self._value)

    @property
    def value(self):
        return self._value
    @value.setter
    def value(self, value):
        self._value = value

    def average_time(self, value:float):
        """
        Helper function for averaging time, moonrakers database is updated after
        averaging numbers

        :param value: Float value to average into current value
        """
        if self._value > 0:
            self._value += value
            self._value /= 2
        else:
            self._value = value
        self.update_database()

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
    cut_threshold: integer
        cut threshold set by user in AFC config
    """
    def __init__(self, moonraker, logger, cut_threshold):
        self.moonraker  = moonraker
        self.logger     = logger
        afc_stats       = self.moonraker.get_afc_stats()

        if afc_stats is not None:
            values = afc_stats["value"]
        else:
            values = None

        self.tc_total           = AFCStats_var("toolchange_count", "total",                 values, self.moonraker)
        self.tc_tool_unload     = AFCStats_var("toolchange_count", "tool_unload",           values, self.moonraker)
        self.tc_tool_load       = AFCStats_var("toolchange_count", "tool_load",             values, self.moonraker)
        self.tc_without_error   = AFCStats_var("toolchange_count", "changes_without_error", values, self.moonraker)
        self.tc_last_load_error = AFCStats_var("toolchange_count", "last_load_error",       values, self.moonraker)

        self.cut_total                  = AFCStats_var("cut", "cut_total",                  values, self.moonraker)
        self.cut_total_since_changed    = AFCStats_var("cut", "cut_total_since_changed",    values, self.moonraker)
        self.last_blade_changed         = AFCStats_var("cut", "last_blade_changed",         values, self.moonraker)
        self.cut_threshold_for_warning  = cut_threshold
        self.threshold_warning_sent     = False
        self.threshold_error_sent       = False

        self.average_toolchange_time    = AFCStats_var("average_time", "tool_change", values, self.moonraker)
        self.average_tool_unload_time   = AFCStats_var("average_time", "tool_unload", values, self.moonraker)
        self.average_tool_load_time     = AFCStats_var("average_time", "tool_load",   values, self.moonraker)

        if self.tc_last_load_error.value == 0:
            self.tc_last_load_error.value = "N/A"
            self.tc_last_load_error.update_database()

        if self.last_blade_changed.value == 0:
            self.last_blade_changed.value = "N/A"
            self.last_blade_changed.update_database()

    def check_cut_threshold(self):
        """
        Function checks current cut value against users threshold value, outputs warning when cut is within
        1k cuts of threshold. Outputs errors once number of cuts exceed threshold
        """
        send_message = False
        message_type = None
        blade_changed_date_string = self.last_blade_changed
        span_start = "<span class=warning--text>"
        if 0 == self.last_blade_changed.value:
            blade_changed_date_string = "N/A"

        if self.cut_total_since_changed.value >= self.cut_threshold_for_warning:
            warning_msg_time        = "Time"
            warning_msg_threshold   = "have exceeded"
            span_start              = "<span class=error--text>"
            message_type            = "error"
            if not self.threshold_error_sent:
                self.threshold_error_sent = send_message = True

        elif self.cut_total_since_changed.value >= (self.cut_threshold_for_warning - 1000):
            warning_msg_time        = "Almost time"
            warning_msg_threshold   = "is about to exceeded"
            span_start              = "<span class=warning--text>"
            message_type            = "warning"
            if not self.threshold_warning_sent:
                self.threshold_warning_sent = send_message = True
        else:
            return

        warning_msg = f"{warning_msg_time} to change cutting blade as your blade has performed {self.cut_total_since_changed} cuts\n"
        warning_msg += f"since changed on {blade_changed_date_string}. Number of cuts {warning_msg_threshold} set threshold of {self.cut_threshold_for_warning}.\n"
        warning_msg +=  "Once blade is changed, execute AFC_CHANGE_BLADE macro to reset count and date changed.\n"
        if send_message:
            self.logger.raw( f"{span_start}{warning_msg}</span>")
            self.logger.afc.message_queue.append((warning_msg, message_type))

    def increase_cut_total(self):
        """
        Helper function for increasing all cut counts
        """
        self.cut_total.increase_count()
        self.cut_total_since_changed.increase_count()
        self.check_cut_threshold()

    def increase_toolcount_change(self):
        """
        Helper function for increasing total toolchange count and number of toolchanges with
        error count.
        """
        self.tc_total.increase_count()
        self.tc_without_error.increase_count()

    def reset_toolchange_wo_error(self):
        """
        Helper function for resetting number of toolchanges without errors and
        sets last error date/time as current
        """
        self.tc_without_error.reset_count()
        self.tc_last_load_error.set_current_time()

    def print_stats(self, afc_obj, short:bool=False):
        """
        Prints all stat to console

        :param afc_obj: AFC class that hold lane information so lanes stats can also be printed out
        :param short: When set to True calls print_stats_skinny function.
        """
        MAX_WIDTH = 87

        def end_string():
            nonlocal print_str, temp_str
            print_str += f"{temp_str:{' '}<{86}}|\n"
            temp_str = ""

        if short:
            return self.print_stats_skinny(afc_obj)

        avg_tool_load   = f"Avg Tool Load: {self.average_tool_load_time.value:4.2f}s"
        avg_tool_unload = f"Avg Tool Unload: {self.average_tool_unload_time.value:4.2f}s"
        avg_tool_change = f"Avg Tool Change: {self.average_toolchange_time.value:4.2f}s"

        cuts_since_change = f"{self.cut_total_since_changed.value}/{self.cut_threshold_for_warning}"

        print_str  = f"{'':{'-'}<{MAX_WIDTH}}\n"
        print_str += f"|{'Toolchanges':{' '}^42}|{'Cut':{' '}^42}|\n"
        print_str += f"|{'':{'-'}<{MAX_WIDTH-2}}|\n"
        print_str += f"|{'Total':{' '}>22} : {self.tc_total.value:{' '}<17}|{'Total':{' '}>22} : {self.cut_total.value:{''}<17}|\n"
        print_str += f"|{'Tool Unload':{' '}>22} : {self.tc_tool_unload.value:{' '}<17}|{'Total since changed':{' '}>22} : {cuts_since_change:{''}<17}|\n"
        print_str += f"|{'Tool Load':{' '}>22} : {self.tc_tool_load.value:{' '}<17}|{'Blade last changed':{' '}>22} : {self.last_blade_changed.value:{''}<17}|\n"
        print_str += f"|{'Changes without error':{' '}>22} : {self.tc_without_error.value:{' '}<17}|{'':{''}<42}|\n"
        print_str += f"|{'Last error date':{' '}>22} : {self.tc_last_load_error.value:{' '}<17}|{'':{''}<42}|\n"
        print_str += f"{'':{'-'}<{MAX_WIDTH}}\n"
        print_str += f"|{avg_tool_load:{' '}^28}|{avg_tool_unload:{' '}^27}|{avg_tool_change:{' '}^28}|\n"
        print_str += f"{'':{'-'}<{MAX_WIDTH}}\n"

        strings = []
        for lane in afc_obj.lanes.values():
            espooler_stats = lane.espooler.get_spooler_stats()
            string = f"{lane.name:{' '}>7} : Lane change count: {lane.lane_load_count.value:{' '}>7}"
            if len(espooler_stats) > 0:
                string += f"    {espooler_stats}"
            strings.append(string)

        temp_str = ""
        for i, s in enumerate(strings):
            if len(temp_str) > 60:
                end_string()

            if len(s) > 60:
                if len(temp_str) > 0:
                    end_string()
                print_str += f"|{s}{'|':{' '}>4}\n"
            else:
                temp_str += f"|{s:{' '}<42}"
        if len(temp_str) > 0:
            end_string()

        print_str += f"{'':{'-'}<{MAX_WIDTH}}\n"
        self.logger.raw(print_str)

    def print_stats_skinny(self, afc_obj):
        """
        Prints all stats to console, this is different as its skinner so its easier to view on phones, Klipperscreen, etc.

        :param afc_obj: AFC class that hold lane information so lanes stats can also be printed out
        """
        MAX_WIDTH = 42

        avg_tool_load   = f"{'Avg Tool Load':{' '}>22} : {self.average_tool_load_time.value:4.2f}s"
        avg_tool_unload = f"{'Avg Tool Unload':{' '}>22} : {self.average_tool_unload_time.value:4.2f}s"
        avg_tool_change = f"{'Avg Tool Change':{' '}>22} : {self.average_toolchange_time.value:4.2f}s"

        cuts_since_change = f"{self.cut_total_since_changed.value}/{self.cut_threshold_for_warning}"

        print_str  = f"{'':{'-'}<{MAX_WIDTH+2}}\n"
        print_str += f"|{'Toolchanges':{' '}^42}|\n"
        print_str += f"|{'':{'-'}<{MAX_WIDTH}}|\n"
        print_str += f"|{'Total':{' '}>22} : {self.tc_total.value:{' '}<17}|\n"
        print_str += f"|{'Tool Unload':{' '}>22} : {self.tc_tool_unload.value:{' '}<17}|\n"
        print_str += f"|{'Tool Load':{' '}>22} : {self.tc_tool_load.value:{' '}<17}|\n"
        print_str += f"|{'Changes without error':{' '}>22} : {self.tc_without_error.value:{' '}<17}|\n"
        print_str += f"|{'Last error date':{' '}>22} : {self.tc_last_load_error.value:{' '}<17}|\n"
        print_str += f"|{'':{'-'}<{MAX_WIDTH}}|\n"
        print_str += f"|{'Cut':{' '}^42}|\n"
        print_str += f"|{'':{'-'}<{MAX_WIDTH}}|\n"
        print_str += f"|{'Total':{' '}>22} : {self.cut_total.value:{''}<17}|\n"
        print_str += f"|{'Total since changed':{' '}>22} : {cuts_since_change:{''}<17}|\n"
        print_str += f"|{'Blade last changed':{' '}>22} : {self.last_blade_changed.value:{''}<17}|\n"
        print_str += f"|{'':{'-'}<{MAX_WIDTH}}|\n"
        print_str += f"|{avg_tool_load:{' '}<42}|\n|{avg_tool_unload:{' '}<42}|\n|{avg_tool_change:{' '}<42}|\n"
        print_str += f"|{'':{'-'}<{MAX_WIDTH}}|\n"

        for lane in afc_obj.lanes.values():
            espooler_stats = lane.espooler.get_spooler_stats(short=True)
            str = f"{lane.name:{' '}>7} : Lane change count: {lane.lane_load_count.value:{' '}>7}"
            str = f"|{str:{' '}^{MAX_WIDTH}}|\n"
            if len(espooler_stats) > 0:
                str += f"|{espooler_stats:{' '}^{MAX_WIDTH}}|\n"
            print_str += str

        print_str += f"{'':{'-'}<{MAX_WIDTH+2}}\n"
        self.logger.raw(print_str)