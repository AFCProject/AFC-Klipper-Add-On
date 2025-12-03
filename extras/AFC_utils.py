# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# File is used to hold common functions that can be called from anywhere and don't belong to a class

import traceback
import json
import inspect

from datetime import datetime
from urllib.request import (
    Request,
    urlopen
)
from urllib.parse import (
    urlencode,
    urljoin,
    quote
)

from urllib.error import (
    HTTPError
)

ERROR_STR = "Error trying to import {import_lib}, please rerun install-afc.sh script in your AFC-Klipper-Add-On directory then restart klipper\n\n{trace}"

def add_filament_switch( switch_name, switch_pin, printer, show_sensor=True, runout_callback = None, enable_runout=False, debounce_delay=0. ):
    """
    Helper function to register pins as filament switch sensor so it will show up in web guis

    :param switch_name: Name of switch to register, should be in the following format: `filament_switch_sensor <name>`
    :param switch_pin: Pin to add to config for switch
    :param printer: printer object

    :return returns filament_switch_sensor object
    """
    import configparser
    import configfile
    new_switch_name = f"filament_switch_sensor {switch_name}"
    ppins = printer.lookup_object('pins')
    ppins.allow_multi_use_pin(switch_pin.strip("!^"))
    filament_switch_config = configparser.RawConfigParser()
    filament_switch_config.add_section( new_switch_name )
    filament_switch_config.set( new_switch_name, 'switch_pin', switch_pin)
    filament_switch_config.set( new_switch_name, 'pause_on_runout', 'False')
    filament_switch_config.set( new_switch_name, 'debounce_delay', 0.0)

    cfg_wrap = configfile.ConfigWrapper( printer, filament_switch_config, {}, new_switch_name)

    fila = printer.load_object(cfg_wrap, new_switch_name)

    # Commence the hacky stuff for delayed runout
    if not show_sensor:
        # Removing normal switch name from object and adding name with underscore if user does not want
        # sensor showing up in gui. Doing this suppressed the sensor from showing up in gui  since the
        # name is not exactly "filament_switch_sensor"
        printer.objects["_" + new_switch_name] = printer.objects.pop(new_switch_name)

    fila.runout_helper.sensor_enabled = enable_runout
    fila.runout_helper.runout_pause = False                 # AFC will deal with pause

    filament_switch_config.set( new_switch_name, 'debounce_delay', debounce_delay)
    # Using our own DebounceButton so that callback functions can be overridden to work correctly
    debounce_button = DebounceButton(cfg_wrap, fila)

    if runout_callback:
        #fila.runout_helper.event_delay = 0.0                # Setting event delay to zero or total delay will be event_delay + debounce_delay
        fila.runout_helper.insert_gcode = None
        fila.runout_helper.runout_gcode = 1
        fila.runout_helper._runout_event_handler = runout_callback # Overriding filament event handler with AFC handler

    if enable_runout:
        return fila, debounce_button

    return fila


def check_and_return( value_str:str, data_values:dict ) -> str:
    """
    Common function to check if value exists in dictionary and returns value if it does.

    :param value_str: Key string to check if value exists in dictionary
    :param data_values: Dictionary of values to check for key

    :return: Returns string of value if found in dictionary
    """
    value = "0"
    if value_str in data_values:
        value = data_values[value_str]

    return value

# Copied from klipper for kalico and older klipper support
class DebounceButton:
    def __init__(self, config, filament_sensor):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        sig = inspect.signature(filament_sensor.runout_helper.note_filament_present)
        # Saving reference to normal function
        self._old_note_filament_present = filament_sensor.runout_helper.note_filament_present
        # Setting action callback to normal filament sensor not filament present
        self.button_action = self._old_note_filament_present
        # Overriding filament sensor filament present to button handler in this class
        # Checking parameter length since kalico's note_filament_present function is different
        # and also checking for older klipper versions before hash 272e8155
        expected_params = ['eventtime', 'is_filament_present', 'force', 'immediate']
        param_keys = list(sig.parameters.keys())
        if param_keys == expected_params:
            # Exact match for the expected signature
            filament_sensor.runout_helper.note_filament_present = self._button_handler
        elif len(sig.parameters) > 2 or len(sig.parameters) == 1:
            filament_sensor.runout_helper.note_filament_present = self.button_handler
        else:
            filament_sensor.runout_helper.note_filament_present = self._button_handler
        self.debounce_delay = config.getfloat('debounce_delay', 0., minval=0.)
        self.logical_state = None
        self.physical_state = None
        self.latest_eventtime = None

    def button_handler(self, state):
        self._button_handler(self.reactor.monotonic(), state)

    def _button_handler(self, eventtime, state):
        self.physical_state = state
        self.latest_eventtime = eventtime
        # if there would be no state transition, ignore the event:
        if self.logical_state == self.physical_state:
            return
        trigger_time = eventtime + self.debounce_delay
        self.reactor.register_callback(self._debounce_event, trigger_time)

    def _debounce_event(self, eventtime):
        # if there would be no state transition, ignore the event:
        if self.logical_state == self.physical_state:
            return
        # if there were more recent events, they supersede this one:
        if (eventtime - self.debounce_delay) < self.latest_eventtime:
            return
        # enact state transition and trigger action
        self.logical_state = self.physical_state
        # Kalico is different from klipper and eventtime is not passed in
        try:
            self.button_action(is_filament_present=self.logical_state)
        except:
            self.button_action(eventtime, self.logical_state)


class AFC_moonraker:
    """
    This class is used to communicate with moonraker to look up information and post
    data into moonrakers database

    Parameters
    ----------------
    port: String
        Port to connect to moonrakers localhost
    logger: AFC_logger
        AFC logger object to log and print to console
    """
    ERROR_STRING = "Error getting data from moonraker, check AFC.log for more information"
    def __init__(self, host:str, port:str, logger:object):
        self.port           = port
        self.logger         = logger
        self.host           = f'{host.rstrip("/")}:{port}'
        self.database_url   = urljoin(self.host, "server/database/item")
        self.afc_stats_key  = "afc_stats"
        self.afc_stats      = None
        self.last_stats_time= None
        self._lane_data     = False
        self.logger.debug(f"Moonraker url: {self.host}")

    def _get_results(self, url_string, print_error=True):
        """
        Helper function to get results, check for errors and return data if successful

        :param url_string: URL encoded string to fetch/post data to moonraker
        :param print_error: Set to True for error to be displayed in console/mainsail panel, setting
                            to False will still write error to log via debug message

        :returns: Returns result dictionary if data is valid, returns None if and error occurred
        """
        data = None
        # Only print error to console when set, else still print errors bug with debug
        # logger so that messages are still written to log for debugging purposes
        if print_error:
            logger = self.logger.error
        else:
            logger = self.logger.debug

        try:
            resp = urlopen(url_string)
            if resp.status >= 200 and resp.status <= 300:
                data = json.load(resp)
            else:
                logger(self.ERROR_STRING)
                logger(f"Response: {resp.status} Reason: {resp.reason}")
        except:
            logger(self.ERROR_STRING, traceback=traceback.format_exc())
            data = None
        return data['result'] if data is not None else data

    def wait_for_moonraker(self, toolhead, timeout:int=30):
        """
        Function to wait for moonraker to start, times out after passed in timeout value

        :param toolhead: Toolhead object so that non blocking waits can happen
        :param timeout: Timeout out trying after this many seconds

        :return: Returns True if connected to moonraker and a timeout did no occur, returns False if
                 not connected after waiting max timeout value
        """
        self.logger.info(f"Waiting max {timeout}s for moonraker to connect")
        for i in range(0,timeout):
            resp = self._get_results(urljoin(self.host, 'server/info'), print_error=False)
            if resp is not None:
                self.logger.debug(f"Connected to moonraker after {i} tries")
                return True
            else:
                toolhead.dwell(1)
        self.logger.warning(f"Failed to connect to moonraker after {timeout} seconds, check AFC.log for more information")
        return False

    def get_spoolman_server(self)->str:
        """
        Queries moonraker to see if spoolman is configured, returns True when
        spoolman is configured

        :returns: Returns string for Spoolman IP, returns None if it is not configured
        """
        resp = self._get_results(urljoin(self.host, 'server/config'))
        # Check to make sure response is valid and spoolman exists in dictionary
        if resp is not None and 'orig' in resp and 'spoolman' in resp['orig']:
            return resp['orig']['spoolman']['server']     # check for spoolman and grab url
        else:
            self.logger.debug("Spoolman server is not defined")
            return None

    def get_file_filament_change_count(self, filename:str ):
        """
        Queries moonraker for files metadata and returns filament change count

        :param filename: Filename to query moonraker and pull metadata
        :return: Returns number of filament change counts if `filament_change_count` is in metadata.
                 Returns zero if not found in metadata.
        """
        change_count = 0
        resp = self._get_results(urljoin(self.host,
                                    'server/files/metadata?filename={}'.format(quote(filename))))
        if resp is not None and 'filament_change_count' in resp:
            change_count =  resp['filament_change_count']
        else:
            self.logger.debug(f"Filament change count metadata not found for file:{filename}")
        return change_count

    def get_afc_stats(self):
        """
        Queries moonraker database for all `afc_stats` entries and returns results if afc_stats exist.
        Function also caches results and refetches data if cache is older than 60s. This is done to help
        cut down on how much data is fetched from moonraker.

        :return: Dictionary of afc_stats entries, None if afc_stats entry does not exist
        """
        resp = None
        # Initially set to True since first time data always needs to be fetched
        refetch_data = True
        current_time = datetime.now()

        # Check to see if data is older than 60 seconds and refreshes
        if self.last_stats_time is not None:
            refetch_data = False
            delta = current_time - self.last_stats_time
            if delta.seconds > 60:
                refetch_data = True
                self.last_stats_time = current_time
        else:
            self.last_stats_time = datetime.now()

        # Cache results to keep queries to moonraker down
        if self.afc_stats is None or refetch_data:
            resp = self._get_results(urljoin(self.database_url, f"?namespace={self.afc_stats_key}"))
            if resp is not None:
                self.afc_stats = resp
            else:
                self.logger.debug("AFC_stats not in database")

        return self.afc_stats

    def update_afc_stats(self, key, value):
        """
        Updates afc_stats in moonrakers database with key, value pair

        :param key: The key indicating the field where the value should be inserted
        :param value: The value to insert into the database
        """
        resp = None
        post_payload = {
            "request_method": "POST",
            "namespace": self.afc_stats_key,
            "key": key,
            "value": value
        }
        req = Request(self.database_url, urlencode(post_payload).encode())

        resp = self._get_results(req)
        if resp is None:
            self.logger.error(f"Error when trying to update {key} in moonraker, see AFC.log for more info")

    def get_spool(self, id:int):
        """
        Uses moonrakers proxy to query spoolID from spoolman

        :param id: SpoolID to lookup and fetch data from spoolman
        :return: Returns dictionary of spoolID, returns None if error occurred or ID does not exist
        """
        resp = None
        request_payload = {
            "request_method": "GET",
            "path": f"/v1/spool/{id}"
        }
        spool_url = urljoin(self.host, 'server/spoolman/proxy')
        req = Request( spool_url, urlencode(request_payload).encode() )

        resp = self._get_results(req)
        if resp is not None:
            resp = resp
        else:
            self.logger.info(f"SpoolID: {id} not found")
        return resp

    def check_for_td1(self):
        """
        Checks moonrakers server/config endpoint to see if user has `[td1]` and `[lane_data]`
        specified in their moonraker.conf file.

        :returns bool,bool,bool: True if `[td1] is defined,
                                 True if a TD-1 device is connected and found,
                                 True if `[lane_data]` is defined
        """
        td1 = False
        td1_defined = False
        resp = self._get_results(urljoin(self.host, 'server/config'))
        if resp is not None:
            if "td1" in resp['orig']:
                td1_defined = True
                td1_data = self.get_td1_data()
                if td1_data is not None and len(td1_data) > 0:
                    td1 = True

            if "lane_data" in resp['orig']:
                self._lane_data = True
        return td1_defined, td1, self._lane_data

    def get_td1_data(self):
        """
        Fetches TD-1 data from moonrakers `machine/td1/data` endpoint

        :returns dict: Returns dictionary of TD-1 devices by serial numbers with their data,
                       returns None if no TD-1 devices are found
        """
        url = urljoin(self.host, "machine/td1/data")
        req = Request(url=url)
        resp = self._get_results(req)
        if resp is not None and "devices" in resp:
            return resp["devices"]
        else:
            return None

    def reboot_td1(self, serial_number):
        """
        Send's TD-1 serial to moonrakers `machine/td1/reboot` endpoint to force restart TD-1
        device

        :param serial_number: Serial number of TD-1 device to reboot
        :return dict: Status of reboot,
                      "ok"-reboot happened successfully
                      "serial_error"-serial number was not supplied
                      "key_error"-serial number supplied is not correct
        """
        url = urljoin(self.host, "machine/td1/reboot")
        td1_reboot_payload = {
            "request_method": "POST",
            "serial": serial_number
        }
        req = Request( url, urlencode(td1_reboot_payload).encode())
        resp = self._get_results(req)
        return resp

    def send_lane_data(self, data):
        """
        Send lane data to moonrakers `machine/set_lane_data` endpoint so that
        other programs can query moonrakers `machine/lane_data` endpoint to see what lanes
        are loaded and what their colors are.

        :params data: Data to send to endpoint
        """
        # TODO: keeping lane data commented out just incase moonraker wants to add
        # back lane_data module
        # if self._lane_data:
        # url = urljoin( self.host, 'machine/set_lane_data')
        try:
            req = Request( url=self.database_url, data=json.dumps(data).encode(),
                        method="POST", headers={"Content-Type": "application/json"})
            if self._get_results(req) is None:
                self.logger.error("Error sending lane data, check AFC.log for more information")
        except HTTPError as e:
            self.logger.error("Error occurred when trying to send lane data to moonraker database,"+
                              "\nplease check AFC.log for more information.")
            self.logger.debug(f"{e}")

    def delete_lane_data(self):
        """
        Function recursively delete's lane_data namespace from moonrakers database.

        Purpose would be to remove data upon boot just incase someone when from a 8 lane
        system to a 4 lane system, removing and then readding will make sure database has
        current up to date data.
        """
        resp = self._get_results(urljoin(self.database_url, "?namespace=lane_data"), print_error=False)
        if resp is not None:
            value = resp.get("value")
            try:
                for key in value.keys():
                    payload = {
                        "request_method": "DELETE",
                        "namespace":"lane_data",
                        "key": key
                    }
                    req = Request( self.database_url, urlencode(payload).encode(), method="DELETE")
                    urlopen(req)
            except HTTPError as e:
                self.logger.debug("Error occurred when trying to delete lane data")
                self.logger.debug(f"{e}")