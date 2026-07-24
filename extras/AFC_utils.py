# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# File is used to hold common functions that can be called from anywhere and don't belong to a class
from __future__ import annotations

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

from typing import TYPE_CHECKING, Optional, Callable, List

if TYPE_CHECKING:
    from extras.AFC_logger import AFC_logger
    from configfile import ConfigWrapper
    from extras.filament_switch_sensor import SwitchSensor
    from klippy import Printer
    from reactor import SelectReactor as Reactor
    from gcode import GCodeCommand, GCodeDispatch
    from extras.pause_resume import PauseResume

ERROR_STR = "Error trying to import {import_lib}, please rerun install-afc.sh script in your AFC-Klipper-Add-On directory then restart klipper\n\n{trace}"

def add_filament_switch(switch_name: str, switch_pin: str, printer: Printer,
                        show_sensor: bool=True, runout_callback: Callable = None,
                        enable_runout: bool=False, debounce_delay: float=0.
                        ) -> tuple[SwitchSensor, DebounceButton]:
    """
    Helper function to register pins as filament switch sensor so it will show up in web guis

    :param switch_name: Name of switch to register, should be in the following format: `filament_switch_sensor <name>`
    :param switch_pin: Pin to add to config for switch
    :param printer: printer object
    :param show_sensor: Controls weather or not this sensor will show up in Fluidd/Mainsail UI,
                        True to show sensor, False to hide sensor from showing up.
    :param runout_callback: Pass in method to replace existing _runout_event_handler in klippers
                            runout_helper class.
    :param enable_runout: If True automatically turns off runout, user can always reenable from UI
                          if sensor is showing or from klipper macro.
    :param debounce_delay: A period of time in seconds to debounce switches prior to detecting
                           runouts

    :return tuple: filament_switch_sensor object and DebounceButton object
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

    # Following needs to be added for Snapmaker U1 klipper version, does not hurt to always
    # have here for non U1 klipper versions.
    filament_switch_config.set( new_switch_name, "extruder")

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

    return fila, debounce_button


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

def section_in_config(config: ConfigWrapper, name: str):
    """
    Helper function for searching through config file to see if a config section exists

    :param config: Config file object to search through
    :param name: Config section name to search for

    :return bool: Returns True if config section name is found in config file
    """
    in_cfg = False
    for s in config.fileconfig.sections():
        if name in s:
            in_cfg = True
            break
    return in_cfg

# Copied from klipper for kalico and older klipper support
class DebounceButton:
    def __init__(self, config, filament_sensor):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode: GCodeDispatch = self.printer.lookup_object('gcode')
        sig = inspect.signature(filament_sensor.runout_helper.note_filament_present)
        # Saving reference to normal function
        self._old_note_filament_present = filament_sensor.runout_helper.note_filament_present
        # Setting action callback to normal filament sensor not filament present
        self.button_action = self._old_note_filament_present
        # Overriding filament sensor filament present to button handler in this class
        # Checking parameter length since kalico's note_filament_present function is different
        # and also checking for older klipper versions before hash 272e8155
        expected_params = ['eventtime', 'is_filament_present', 'force', 'immediate']
        snapmaker_expected = ['is_filament_present', 'force']
        param_keys = list(sig.parameters.keys())
        if param_keys == expected_params:
            # Exact match for the expected signature
            filament_sensor.runout_helper.note_filament_present = self._button_handler
        elif param_keys == snapmaker_expected:
            filament_sensor.runout_helper.note_filament_present = self.button_handler
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
            try:
                self.button_action(is_filament_present=self.logical_state)
            except TypeError:
                self.button_action(eventtime, self.logical_state)
        # Catching error here since klipper can also throw and error and don't want this
        # to actually crash klipper
        except Exception:
            # Last ditch effort to call klipper pause since something bad happened
            pause_resume: PauseResume = self.printer.lookup_object("pause_resume")
            pause_cmd: GCodeCommand = self.gcode.create_gcode_command("PAUSE", "PAUSE", {})
            # Calling Pause command directly since user/plugins could have overridden this command
            pause_resume.cmd_PAUSE(pause_cmd)


class VirtualRunoutHelper:
    """Minimal runout helper used by FPS_PSF virtual sensors."""

    def __init__(self, printer: Printer, name: str, runout_cb: Optional[Callable] = None,
                 enable_runout: bool = False) -> None:
        """
        Initialize the minimal runout helper.

        :param printer: Klipper printer object.
        :param name: Sensor name.
        :param runout_cb: Optional callable invoked on a runout transition.
        :param enable_runout: Whether runout callbacks are enabled.
        """
        self.printer: Printer = printer
        self.gcode: GCodeDispatch = self.printer.lookup_object('gcode')
        self._reactor: Reactor = printer.get_reactor()
        self.name: str = name
        self.runout_callback: Optional[Callable] = runout_cb
        self.sensor_enabled: bool = bool(enable_runout)
        self.filament_present: bool = False
        self.insert_gcode: Optional[str] = None
        self.runout_gcode: Optional[str] = None
        self.event_delay: float = 0.0
        self.min_event_systime: float = self._reactor.NEVER

    def note_filament_present(self, eventtime: Optional[float] = None,
                              is_filament_present: bool = False, **_kwargs) -> None:
        """
        Update the tracked filament-present state and fire runout if needed.

        Only acts on a state change; invokes the runout callback when filament
        transitions to absent and runout is enabled.

        :param eventtime: Reactor event time; defaults to now when None.
        :param is_filament_present: New filament-present state.
        :param _kwargs: Ignored extra keyword arguments for API compatibility.
        """
        if eventtime is None:
            eventtime = self._reactor.monotonic()

        new_state: bool = bool(is_filament_present)
        if new_state == self.filament_present:
            return

        self.filament_present = new_state
        idle_timeout = self.printer.lookup_object("idle_timeout")
        is_printing = idle_timeout.get_status(eventtime)["state"] == "Printing"

        if (not new_state
            and self.sensor_enabled
            and callable(self.runout_callback)
            and is_printing):
            try:
                try:
                    self.runout_callback(eventtime)
                except TypeError:
                    self.runout_callback(eventtime=eventtime)
            # Catching error here since klipper can also throw and error and don't want this
            # to actually crash klipper
            except Exception:
                # Last ditch effort to call klipper pause since something bad happened
                pause_resume: PauseResume = self.printer.lookup_object("pause_resume")
                pause_cmd: GCodeCommand = self.gcode.create_gcode_command("PAUSE", "PAUSE", {})
                # Calling Pause command directly since user/plugins could have overridden this command
                pause_resume.cmd_PAUSE(pause_cmd)

    def get_status(self, _eventtime: Optional[float] = None) -> dict:
        """
        Return the sensor status.

        :param _eventtime: Reactor event time (unused).
        :return: Dict with `filament_detected` and `enabled` booleans.
        """
        return {
            "filament_detected": bool(self.filament_present),
            "enabled": bool(self.sensor_enabled),
        }

class VirtualFilamentSensor:
    """Lightweight filament sensor placeholder for FPS virtual pins."""

    QUERY_HELP = "Query the status of the Filament Sensor"
    SET_HELP = "Sets the filament sensor on/off"

    def __init__(self, printer: Printer, name: str, logger: AFC_logger,
                show_in_gui: bool = True, runout_cb: Optional[Callable] = None,
                enable_runout: bool = False) -> None:
        """
        Register a lightweight virtual filament sensor.

        Adds the object under the `filament_switch_sensor` namespace (hiding it
        from the GUI by underscore-prefixing when requested) and registers the
        QUERY/SET filament-sensor G-code commands.

        :param printer: Klipper printer object.
        :param name: Sensor name.
        :param show_in_gui: When False, hide the sensor from the GUI.
        :param runout_cb: Optional runout callback passed to the runout helper.
        :param enable_runout: Whether runout callbacks are enabled.
        """
        self.printer: Printer = printer
        self.name: str = name
        self.logger: AFC_logger = logger
        self._object_name: str = f"filament_switch_sensor {name}"
        self._object_name = self._object_name if show_in_gui else "_" + self._object_name
        self.runout_helper: VirtualRunoutHelper = VirtualRunoutHelper(
            printer, name, runout_cb=runout_cb, enable_runout=enable_runout)

        try:
            printer.add_object(self._object_name, self)
        except Exception:
            # Fallback: direct dict registration
            objects = getattr(printer, "objects", None)
            if isinstance(objects, dict):
                objects.setdefault(self._object_name, self)

        gcode = printer.lookup_object("gcode", None)
        if gcode is None:
            return
        try:
            gcode.register_mux_command("QUERY_FILAMENT_SENSOR", "SENSOR", name,
                                       self.cmd_QUERY_FILAMENT_SENSOR, desc=self.QUERY_HELP)
        except Exception:
            pass
        try:
            gcode.register_mux_command("SET_FILAMENT_SENSOR", "SENSOR", name,
                                       self.cmd_SET_FILAMENT_SENSOR, desc=self.SET_HELP)
        except Exception:
            pass

    def get_status(self, eventtime: Optional[float]) -> dict:
        """
        Return the sensor status from the runout helper.

        :param eventtime: Reactor event time passed through to the helper.
        :return: Dict with `filament_detected` and `enabled` booleans.
        """
        return self.runout_helper.get_status(eventtime)

    def cmd_QUERY_FILAMENT_SENSOR(self, gcmd: GCodeCommand) -> None:
        """
        G-code handler that reports whether filament is detected.

        Usage: `QUERY_FILAMENT_SENSOR SENSOR=<name>`

        :param gcmd: The parsed G-code command.
        """
        status = self.runout_helper.get_status(None)
        if status["filament_detected"]:
            msg = f"Filament Sensor {self.name}: filament detected"
        else:
            msg = f"Filament Sensor {self.name}: filament not detected"
        gcmd.respond_info(msg)

    def cmd_SET_FILAMENT_SENSOR(self, gcmd: GCodeCommand) -> None:
        """
        G-code handler that enables or disables the virtual sensor.

        Usage: `SET_FILAMENT_SENSOR SENSOR=<name> ENABLE=<0|1>`

        :param gcmd: The parsed G-code command.
        """
        self.runout_helper.sensor_enabled = bool(gcmd.get_int("ENABLE", 1, minval=0, maxval=1))

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
    def __init__(self, host: str, port: str, logger: AFC_logger):
        self.port           = port
        self.logger         = logger
        self.host           = f'{host.rstrip("/")}:{port}'
        self.database_url   = urljoin(self.host, "server/database/item")
        self.afc_stats_key  = "afc_stats"
        self.afc_stats      = None
        self.last_stats_time= None
        self._lane_data     = False
        self.logger.debug(f"Moonraker url: {self.host}")
        self.FILENAME_PATH: str = "server/files/metadata?filename="

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

    def get_file_metadata(self, filename: str) -> dict:
        """
        Queries moonraker for a print file's metadata.

        :param filename: Filename to query moonraker and pull metadata
        :return dict: Metadata dictionary returned by moonraker, None if the query fails
        """
        resp: dict = self._get_results(urljoin(self.host, f"{self.FILENAME_PATH}{quote(filename)}"))
        return resp

    def get_afc_stats(self) -> Optional[dict]:
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
        values = None
        if self.afc_stats is not None:
            values = self.afc_stats['value']

        return values

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

    def remove_database_entry(self, namespace, key):
        """
        Common function for removing entries in moonrakers database

        :param namespace: Namespace for moonrakers database
        :param key: Key to delete from namespace
        """
        try:
            payload = {
                "request_method": "DELETE",
                "namespace": namespace,
                "key": key
            }
            req = Request( self.database_url, urlencode(payload).encode(), method="DELETE")
            urlopen(req)
            self.logger.debug(f"Removing {key} from {namespace}")
        except HTTPError as e:
            self.logger.debug(
                f"Error occurred when trying to delete {key} from {namespace} namespace"
            )
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
                    self.remove_database_entry("lane_data", key)
            except HTTPError as e:
                self.logger.debug("Error occurred when trying to delete lane data")
                self.logger.debug(f"{e}")

    def trigger_db_backup(self) -> bool:
        """
        Triggers moonrakers database backup with moonrakers default naming scheme
        """
        error = False
        try:
            req = Request( urljoin(self.host, 'server/database/backup'), method="POST",
                          headers={"Content-Type": "application/json"})
            resp = self._get_results(req)
            if resp is None:
                self.logger.error("Error trying to backup moonraker database, check AFC.log for more information")
                error = True
            else:
                self.logger.info(f"Moonrakers database backed up to {resp['backup_path']}")
        except HTTPError as e:
            self.logger.error("Error occurred when trying to backup moonraker database,"+
                              "\nplease check AFC.log for more information.")
            self.logger.debug(f"{e}")
            error = True
        return error

class AFC_PrintFileMetaData:
    """
    Wraps moonraker's file metadata lookup for the file currently being printed,
    caching the result so tool change count/temperatures can be read repeatedly
    without re-querying moonraker.
    """
    def __init__(self, moonraker: AFC_moonraker, logger: AFC_logger):
        """
        :param moonraker: Moonraker object to query file metadata from
        :param logger: Logger object to print debug/info messages to
        """
        self._moonraker = moonraker
        self.logger = logger
        self._filename: str = ""
        self._metadata: dict = {}

    @property
    def filename(self) -> str:
        """
        :return str: Filename that metadata is currently cached for
        """
        return self._filename
    @filename.setter
    def filename(self, value: str) -> None:
        """
        Sets current filename and queries moonraker for its metadata, caching
        the result for the `tool_change_count`/`tool_temperatures` properties.

        :param value: Filename to query moonraker and pull metadata for
        """
        self._filename = value
        if (self._moonraker
            and value):
            self._metadata = self._moonraker.get_file_metadata(self._filename) or {}

    @property
    def tool_change_count(self) -> int:
        """
        :return int: Number of filament change counts if `filament_change_count` is in
                     cached metadata, zero if not found
        """
        change_count = 0
        if (self._metadata
            and "filament_change_count" in self._metadata):
            change_count = self._metadata.get("filament_change_count", 0)
        else:
            self.logger.debug(f"Filament change count metadata not found for file:{self._filename}")
        return change_count

    @property
    def tool_temperatures(self) -> List[int]:
        """
        :return List[int]: Per-tool temperatures from cached metadata, empty list if not found
        """
        temperature_list = []
        if self._metadata:
            temperature_list = self._metadata.get("filament_temps", [])
            if not temperature_list:
                # Try and get variable thats used for snapmaker U1
                temperature_list = self._metadata.get("nozzle_temp", [])
        return temperature_list

    def reset(self) -> None:
        """
        Clears cached filename and metadata, used when a print ends/is reset so
        stale tool change/temperature data isn't reused for the next print.
        """
        self._filename = ""
        self._metadata = {}
