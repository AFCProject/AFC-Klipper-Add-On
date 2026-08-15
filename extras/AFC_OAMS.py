# AFCProject Automated Filament Changer Software
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# This file include code inspired/modified from OpenAms Project. https://github.com/OpenAMSOrg/klipper_openams
# Originally authored by JR Lomas(aka KnightRadiant) and licensed under the MIT license
# Full license text available at: https://mit-license.org/

# This code was updated and contributed by lindnjoe(aka J0eB0l)
from __future__ import annotations

import mcu
import struct
from enum import IntEnum
from math import pi
from typing import Any, Callable, Dict, Optional, Tuple, TYPE_CHECKING

from extras.AFC_OpenAMS import AMSHardwareService

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from gcode import GCodeCommand
    from extras.AFC_logger import AFC_logger
    from klippy import Printer
    from reactor import SelectReactor as Reactor

# Pre-compiled struct formats for float conversions
_FLOAT_STRUCT = struct.Struct("f")
_U32_STRUCT = struct.Struct("I")

class OAMSStatus(IntEnum):
    """
    Enumeration of firmware action/status codes reported by the OAMS MCU.

    These values mirror the ``action`` field of ``oams_action_status`` MCU
    messages and the local ``action_status`` used to track in-flight operations.
    """
    LOADING = 0
    UNLOADING = 1
    FORWARD_FOLLOWING = 2
    REVERSE_FOLLOWING = 3
    COASTING = 4
    STOPPED = 5
    CALIBRATING = 6
    ERROR = 7

class OAMSOpCode(IntEnum):
    """
    Enumeration of result/operation codes returned by OAMS firmware actions.

    These values appear in the ``code`` field of ``oams_action_status`` messages
    and are returned from load/unload helpers to describe success or the kind of
    failure encountered.
    """
    SUCCESS = 0
    ERROR_UNSPECIFIED = 1
    ERROR_BUSY = 2
    SPOOL_ALREADY_IN_BAY = 3
    NO_SPOOL_IN_BAY = 4
    ERROR_KLIPPER_CALL = 5
    CANCEL = 6


def _oams_enum_name(cls: type, value: int, kind: str) -> str:
    """
    Human-readable name for an OAMSStatus/OAMSOpCode value, derived from the
    matching class attribute (e.g. STOPPED -> 'stopped', NO_SPOOL_IN_BAY ->
    'no spool in bay'). Falls back to '<kind> <value>' for unknown values.
    """
    return next(
        (k.replace('_', ' ').lower()
         for k, v in vars(cls).items()
         if isinstance(v, int) and v == value),
        f"{kind} {value}")


class RetryState:
    """
    Per-spool bookkeeping for load-retry attempts.

    Tracks how many attempts have been made, when the last attempt occurred,
    and whether the most recent successful load required a retry.
    """
    def __init__(self) -> None:
        """
        Initialize the retry counters to their unused/zero state.
        """
        self.count        = 0
        self.last_attempt = None
        self.was_retry    = False

    def reset(self) -> None:
        """
        Reset all retry bookkeeping back to the initial state.
        """
        self.count        = 0
        self.last_attempt = None
        self.was_retry    = False


class AFC_OAMS:
    """
    Klipper hardware controller for a single OpenAMS ([AFC_OAMS ...]) unit.

    Wraps the OAMS MCU: it configures the pressure (FPS) buffer, the first-stage
    (f1s) and hub Hall-effect sensors, and the pressure/current PID loops; sends
    load/unload/follower/calibration commands; receives sensor and status
    updates from firmware; and provides retry wrappers and the ``OAMS_*`` G-code
    commands. One instance is created per ``[AFC_OAMS ...]`` config section.
    """
    def __init__(self, config: ConfigWrapper) -> None:
        """
        Construct the controller from its Klipper config section.
        Resolves the printer, MCU, reactor and AFC objects; reads pressure
        thresholds, Hall-effect sensor calibration, PTFE length, PID gains,
        targets and retry tuning; registers MCU response callbacks and the
        ``OAMS_*`` G-code commands; and, when available, registers itself with
        the shared :class:`AMSHardwareService` so AFC can reach this controller.

        :param config: Klipper ``ConfigWrapper`` for this ``[AFC_OAMS ...]`` section.
        """
        # Core printer interface
        self.printer: Printer = config.get_printer()
        self.section_name = config.get_name().split()[-1]
        self.mcu = mcu.get_printer_mcu(self.printer, config.get("mcu", "mcu"))
        self.reactor: Reactor = self.printer.get_reactor()
        afc_obj = self.printer.load_object(config, "AFC")
        self.afc = afc_obj
        self.logger: AFC_logger = afc_obj.logger
        self.gcode = self.printer.lookup_object('gcode')

        # Pressure sensor thresholds
        self.fps_upper_threshold = config.getfloat("fps_upper_threshold")
        self.fps_lower_threshold = config.getfloat("fps_lower_threshold")
        self.fps_is_reversed     = config.getboolean("fps_is_reversed")

        # Current state
        self.current_spool: Optional[int] = None
        self.encoder_clicks = 0
        self.i_value        = 0.0

        # Hall Effect Sensor thresholds
        self.f1s_hes_on = list(
            map(lambda x: float(x.strip()), config.get("f1s_hes_on").split(","))
        )
        self.f1s_hes_is_above = config.getboolean("f1s_hes_is_above")
        self.hub_hes_on = list(
            map(lambda x: float(x.strip()), config.get("hub_hes_on").split(","))
        )
        self.hub_hes_is_above = config.getboolean("hub_hes_is_above")

        # Physical configuration
        self.filament_path_length = config.getfloat("ptfe_length")
        self.oams_idx             = config.getint("oams_idx")

        # PID control - pressure
        self.kd = config.getfloat("kd", 0.0)
        self.ki = config.getfloat("ki", 0.0)
        self.kp = config.getfloat("kp", 6.0)

        # PID control - current
        self.current_kp = config.getfloat("current_kp", 0.375)
        self.current_ki = config.getfloat("current_ki", 0.0)
        self.current_kd = config.getfloat("current_kd", 0.0)

        # Target values
        self.fps_target = config.getfloat(
            "fps_target",
            0.5,
            minval=0.0,
            maxval=1.0,
            above=self.fps_lower_threshold,
            below=self.fps_upper_threshold,
        )
        self.current_target = config.getfloat(
            "current_target", 0.3, minval=0.1, maxval=0.4
        )

        # Hardware state arrays (updated by firmware)
        self.fps_value: float = 0
        self.f1s_hes_value  = [0, 0, 0, 0]
        self.hub_hes_value  = [0, 0, 0, 0]

        # Action status tracking
        # Optional[int] rather than the OAMSStatus/OAMSOpCode enums directly: this
        # attribute is also written from AFC_OpenAMS.py, which keeps its own
        # separate-but-value-identical enum to avoid a circular import with this
        # module. Both are IntEnum, so only the underlying int value matters here.
        self.action_status: Optional[int] = None
        self.action_status_code: Optional[int] = None
        self.action_status_value: Optional[int] = None

        # MCU communication
        self._register_mcu_response(self._oams_action_status, "oams_action_status")
        self._register_mcu_response(self._oams_cmd_stats, "oams_cmd_stats")
        self._register_mcu_response(self._oams_cmd_current_status, "oams_cmd_current_status")
        self.mcu.register_config_callback(self._build_config)

        self.config_name = config.get_name()   # full Klipper section name, e.g. "oams oams1"
        self.name = self.config_name.split()[-1]  # short name, e.g. "oams1"
        self.register_commands(self.name)

        # Retry configuration
        self.load_retry_max  = config.getint("load_retry_max", 3, minval=1, maxval=5)
        self.unload_retry_max = config.getint("unload_retry_max", 2, minval=1, maxval=3)
        self.retry_delay     = config.getfloat("retry_delay", 3.0, above=0.0)
        self.auto_unload_on_failed_load = config.getboolean(
            "auto_unload_on_failed_load", True
        )

        # Early stall detection during load: if the encoder stops advancing for
        # load_stall_dwell seconds after an initial load_stall_grace spin-up, the
        # spool is stuck -- bail out early instead of blocking the full 45s MCU
        # timeout. Set load_stall_dwell to 0 to disable and keep old behaviour.
        self.load_stall_grace = config.getfloat("load_stall_grace", 3.0, minval=0.0)
        self.load_stall_dwell = config.getfloat("load_stall_dwell", 5.0, minval=0.0)

        # Retry state tracking
        self._load_retry_state: Dict[int, RetryState] = {}
        self._unload_retry_count     = 0
        self._last_unload_attempt    = 0.0
        self._last_successful_load: Dict[int, float] = {}
        self._load_retry_failures    = 0
        self._unload_retry_failures  = 0
        self._last_load_failure_time   = None
        self._last_unload_failure_time = None
        self.hardware_service = None

        # MCU command handles, resolved and set dynamically in handle_connect()
        self.oams_load_spool_cmd: mcu.CommandWrapper
        self.oams_unload_spool_cmd: mcu.CommandWrapper
        self.oams_follower_cmd: mcu.CommandWrapper
        self.oams_calibrate_ptfe_length_cmd: mcu.CommandWrapper
        self.oams_calibrate_hub_hes_cmd: mcu.CommandWrapper
        self.oams_pid_cmd: mcu.CommandWrapper
        self.oams_set_led_error_cmd: mcu.CommandWrapper
        self.oams_load_spool_cancel_cmd: Optional[mcu.CommandWrapper]
        self.oams_spool_query_spool_cmd: mcu.CommandQueryWrapper

        # Expose the underlying hardware controller to AFC when available
        if AMSHardwareService is not None:
            try:
                service = AMSHardwareService.for_printer(
                    self.printer, self.section_name
                )
                service.attach_controller(self)
                self.hardware_service = service
            except Exception as e:
                self.logger.error(
                    f"Failed to register OAMS controller with AMSHardwareService: {e}"
                )
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

    def _register_mcu_response(
        self, callback: Callable, message_name: str, oid: Optional[int] = None
    ) -> None:
        """
        Register MCU message callbacks across Klipper API versions.
        Tries the modern ``mcu.register_response`` first, then falls back to the
        serial object and legacy ``register_serial_response`` so the same code
        works on differing Klipper builds.

        :param callback: callable invoked with the parsed message params.
        :param message_name: name of the MCU response message to listen for.
        :param oid: optional object id to scope the registration to.
        :raises: ``config_error`` if the MCU exposes no supported registration.
        """
        if hasattr(self.mcu, "register_response"):
            self.mcu.register_response(callback, message_name, oid)
            return
        serial = getattr(self.mcu, "_serial", None)
        if serial is not None and hasattr(serial, "register_response"):
            serial.register_response(callback, message_name, oid)
            return
        if hasattr(self.mcu, "register_serial_response"):
            self.mcu.register_serial_response(callback, message_name, oid)
            return
        raise self.printer.config_error(
            f"MCU '{self.mcu.get_name()}' does not support response registration"
        )

    def _resolve_lane_name(self, spool_idx: int) -> Optional[str]:
        """
        Resolve the AFC lane name bound to a spool/bay index, if any.

        :param spool_idx: zero-based spool/bay index on this OAMS unit.
        :return str: the lane name, or ``None`` when no hardware service is
            attached or no lane maps to the spool.
        """
        if self.hardware_service is None:
            return None
        return self.hardware_service.resolve_lane_for_spool_with_afc(self.section_name, spool_idx)

    def get_status(self, eventtime: float) -> Dict[str, Any]:
        """
        Return the controller status dict for Klipper's status API.

        :param eventtime: reactor event time of the status query (unused).
        :return dict: current spool, f1s/hub Hall-effect sensor values, FPS
            value, and load/unload retry-failure statistics.
        """
        return {
            "current_spool": self.current_spool,
            "f1s_hes_value": list(self.f1s_hes_value),
            "hub_hes_value": list(self.hub_hes_value),
            "fps_value": self.fps_value,
            # Retry failure statistics
            "load_retry_failures": self._load_retry_failures,
            "unload_retry_failures": self._unload_retry_failures,
            "last_load_failure_time": self._last_load_failure_time,
            "last_unload_failure_time": self._last_unload_failure_time,
        }

    def is_bay_ready(self, bay_index: int) -> bool:
        """
        Report whether a bay has filament present at its first-stage sensor.

        :param bay_index: zero-based bay index to query.
        :return bool: ``True`` if the f1s Hall-effect sensor for the bay is
            triggered; ``False`` if not triggered or the index is out of range.
        """
        if not (0 <= bay_index < len(self.f1s_hes_value)):
            self.logger.error(f"Invalid bay_index {bay_index}, must be 0-{len(self.f1s_hes_value)-1}")
            return False
        return bool(self.f1s_hes_value[bay_index])

    def is_bay_loaded(self, bay_index: int) -> bool:
        """
        Report whether a bay's filament has reached the hub sensor.

        :param bay_index: zero-based bay index to query.
        :return bool: ``True`` if the hub Hall-effect sensor for the bay is
            triggered; ``False`` if not triggered or the index is out of range.
        """
        if not (0 <= bay_index < len(self.hub_hes_value)):
            self.logger.error(f"Invalid bay_index {bay_index}, must be 0-{len(self.hub_hes_value)-1}")
            return False
        return bool(self.hub_hes_value[bay_index])

    def stats(self, eventtime: float) -> Tuple[bool, str]:
        """
        Build the periodic stats line for Klipper's status log.

        :param eventtime: reactor event time of the stats query (unused).
        :return tuple: ``(False, message)`` where ``message`` summarises spool,
            FPS, sensor values, PID gains, encoder clicks and current draw.
        """
        return (
            False,
            """
OAMS[%s]: current_spool=%s fps_value=%s f1s_hes_value_0=%d f1s_hes_value_1=%d f1s_hes_value_2=%d f1s_hes_value_3=%d hub_hes_value_0=%d hub_hes_value_1=%d hub_hes_value_2=%d hub_hes_value_3=%d kp=%d ki=%d kd=%d encoder_clicks=%d i_value=%.2f
"""
            % ( self.oams_idx,
                self.current_spool,
                self.fps_value,
                self.f1s_hes_value[0],
                self.f1s_hes_value[1],
                self.f1s_hes_value[2],
                self.f1s_hes_value[3],
                self.hub_hes_value[0],
                self.hub_hes_value[1],
                self.hub_hes_value[2],
                self.hub_hes_value[3],
                self.kp,
                self.ki,
                self.kd,
                self.encoder_clicks,
                self.i_value,
            ),
        )

    def handle_connect(self) -> None:
        """
        Look up and cache OAMS MCU command objects on ``klippy:connect``.
        Resolves the load/unload/follower/calibration/PID/LED command handles,
        the optional cancel command (warning if the firmware lacks it), and the
        spool-query command, then clears any latched LED error state. Lookup
        failures are logged rather than raised.
        """
        command_defs = {
            'load_spool': "oams_cmd_load_spool spool=%c",
            'unload_spool': "oams_cmd_unload_spool",
            'follower': "oams_cmd_follower enable=%c direction=%c",
            'calibrate_ptfe_length': "oams_cmd_calibrate_ptfe_length spool=%c",
            'calibrate_hub_hes': "oams_cmd_calibrate_hub_hes spool=%c",
            'pid': "oams_cmd_pid kp=%u ki=%u kd=%u target=%u",
            'set_led_error': "oams_set_led_error idx=%c value=%c",
        }

        try:
            for cmd_name, cmd_string in command_defs.items():
                cmd_obj = self.mcu.lookup_command(cmd_string)
                setattr(self, f'oams_{cmd_name}_cmd', cmd_obj)

            try:
                self.oams_load_spool_cancel_cmd = self.mcu.lookup_command(
                    "oams_cmd_load_spool_cancel"
                )
            except Exception as e:
                self.oams_load_spool_cancel_cmd = None
                self.logger.warning(
                    f"Failed to initialize OAMS load filament cancel command: {e}\n"
                    "Most likely the firmware needs to be updated to support this command."
                )

            cmd_queue = self.mcu.alloc_command_queue()
            self.oams_spool_query_spool_cmd = self.mcu.lookup_query_command(
                "oams_cmd_query_spool",
                "oams_query_response_spool spool=%u",
                cq=cmd_queue,
            )

            self.clear_errors()

        except Exception as e:
            self.logger.error(f"Failed to initialize OAMS commands: {e}")

    def handle_ready(self) -> None:
        """
        Clear in-flight action state on ``klippy:ready``.
        Resets ``action_status``/code/value so a restart never inherits a stale
        load/unload/calibration status from a previous session.
        """
        self.action_status = None
        self.action_status_code = None
        self.action_status_value = None
        self.logger.info(f"OAMS[{self.oams_idx}]: Cleared software error states on ready")

    def get_spool_status(self, bay_index: int) -> int:
        """
        Return the raw first-stage Hall-effect value for a bay.

        :param bay_index: zero-based bay index to query.
        :return int: the f1s sensor value for the bay, or ``0`` if out of range.
        """
        if not (0 <= bay_index < len(self.f1s_hes_value)):
            self.logger.error(f"Invalid bay_index {bay_index}, must be 0-{len(self.f1s_hes_value)-1}")
            return 0
        return self.f1s_hes_value[bay_index]

    def clear_errors(self) -> None:
        """
        Clear LED error indicators and reset cached action/spool state.
        Turns off the error LED for all four bays, clears the action status
        fields, and re-queries the firmware for the currently loaded spool.
        Individual failures are logged but do not stop the cleanup.
        """
        for i in range(4):
            try:
                self.set_led_error(i, 0)
            except Exception as e:
                self.logger.error(f"Failed to clear LED error for bay {i} on {getattr(self, 'name', 'unknown')}: {e}")

        self.action_status = None
        self.action_status_code = None
        self.action_status_value = None

        try:
            self.current_spool = self.determine_current_spool()
        except Exception as e:
            self.logger.error(f"Failed to determine current spool during clear_errors on {getattr(self, 'name', 'unknown')}: {e}")

    def set_led_error(self, idx: int, value: int) -> None:
        """
        Set the error LED state for one bay via the MCU.

        :param idx: zero-based bay index whose LED to change.
        :param value: LED state to send (non-zero lights the error LED).
        """
        self.logger.debug(f"Setting LED {idx} to {value}")
        self.oams_set_led_error_cmd.send([idx, value])

    def determine_current_spool(self) -> Optional[int]:
        """
        Query the firmware for the currently loaded spool index.

        :return int: the loaded spool index (0-3), or ``None`` when no spool is
            loaded (firmware returns 255), the response is missing, or the
            value is unexpected.
        """
        params = self.oams_spool_query_spool_cmd.send()
        if params is None:
            self.logger.warning(f"OAMS[{self.oams_idx}]: Failed to query current spool - no response from MCU")
            return None

        if "spool" not in params:
            self.logger.warning(f"OAMS[{self.oams_idx}]: Spool query response missing 'spool' field")
            return None

        spool_val = params["spool"]
        if 0 <= spool_val <= 3:
            return spool_val

        if spool_val != 255:
            self.logger.warning(
                f"OAMS[{self.oams_idx}]: Unexpected spool index {spool_val} from hardware (expected 0-3 or 255); treating as no spool loaded"
            )
        else:
            self.logger.debug(
                f"OAMS[{self.oams_idx}]: No spool loaded (hardware returned 255)"
            )
        return None

    def register_commands(self, name: str) -> None:
        """
        Register this unit's ``OAMS_*`` G-code commands as mux commands.
        Each command is keyed on the ``OAMS`` index so multiple units coexist.

        :param name: short unit name (unused; index is used for muxing).
        """
        oams_id = str(self.oams_idx)
        gcode = self.gcode

        commands = [
            ("OAMS_LOAD_SPOOL", self.cmd_OAMS_LOAD_SPOOL, self.cmd_OAMS_LOAD_SPOOL_help),
            ("OAMS_UNLOAD_SPOOL", self.cmd_OAMS_UNLOAD_SPOOL, self.cmd_OAMS_UNLOAD_SPOOL_help),
            ("OAMS_FOLLOWER", self.cmd_OAMS_FOLLOWER, self.cmd_OAMS_FOLLOWER_help),
            ("OAMS_CALIBRATE_PTFE_LENGTH", self.cmd_OAMS_CALIBRATE_PTFE_LENGTH, self.cmd_OAMS_CALIBRATE_PTFE_LENGTH_help),
            ("OAMS_CALIBRATE_HUB_HES", self.cmd_OAMS_CALIBRATE_HUB_HES, self.cmd_OAMS_CALIBRATE_HUB_HES_help),
            ("OAMS_PID_AUTOTUNE", self.cmd_OAMS_PID_AUTOTUNE, self.cmd_OAMS_PID_AUTOTUNE_help),
            ("OAMS_PID_SET", self.cmd_OAMS_PID_SET, self.cmd_OAMS_PID_SET_help),
            ("OAMS_CURRENT_PID_SET", self.cmd_OAMS_CURRENT_PID_SET, self.cmd_OAMS_CURRENT_PID_SET_help),
            ("OAMS_ABORT_ACTION", self.cmd_OAMS_ABORT_ACTION, self.cmd_OAMS_ABORT_ACTION_help),
            ("OAMS_RETRY_STATUS", self.cmd_OAMS_RETRY_STATUS, self.cmd_OAMS_RETRY_STATUS_help),
            (
                "OAMS_RESET_RETRY_COUNTS",
                self.cmd_OAMS_RESET_RETRY_COUNTS,
                self.cmd_OAMS_RESET_RETRY_COUNTS_help,
            ),
        ]

        for cmd_name, handler, help_text in commands:
            gcode.register_mux_command(cmd_name, "OAMS", oams_id, handler, desc=help_text)

        self.afc.function.register_mux_command(
            self.afc.show_macros, 'AFC_STOP_OAMS_FOLLOWER', "OAMS", oams_id,
            self.cmd_AFC_STOP_OAMS_FOLLOWER, self.cmd_AFC_STOP_OAMS_FOLLOWER_help)
        self.afc.function.register_mux_command(
            self.afc.show_macros, 'AFC_START_OAMS_FOLLOWER', "OAMS", oams_id,
            self.cmd_AFC_START_OAMS_FOLLOWER, self.cmd_AFC_START_OAMS_FOLLOWER_help)

    cmd_OAMS_RETRY_STATUS_help = "Display retry configuration and state"

    def cmd_OAMS_RETRY_STATUS(self, gcmd: GCodeCommand) -> None:
        """
        Report retry configuration and live retry counters for the unit specified.

        Usage
        -------
        `OAMS_RETRY_STATUS OAMS=<oams_name>`

        Example
        -------
        ```
        OAMS_RETRY_STATUS OAMS=oams1
        ```
        """
        msg_lines = [
            f"OAMS[{self.oams_idx}] Retry Status:",
            f"  Load retry max: {self.load_retry_max}",
            f"  Unload retry max: {self.unload_retry_max}",
            f"  Retry delay: {self.retry_delay:.1f}s",
            f"  Auto-unload on failed load: {self.auto_unload_on_failed_load}",
            f"  Current unload retry count: {self._unload_retry_count}",
        ]

        if self._load_retry_state:
            msg_lines.append("  Load retry counts:")
            for spool_idx, retry in sorted(self._load_retry_state.items()):
                msg_lines.append(
                    f"    Spool {spool_idx}: {retry.count}/{self.load_retry_max}"
                )
        else:
            msg_lines.append("  No active load retries")

        gcmd.respond_info("\n".join(msg_lines))

    cmd_OAMS_RESET_RETRY_COUNTS_help = "Reset retry counters"

    def cmd_OAMS_RESET_RETRY_COUNTS(self, gcmd: GCodeCommand) -> None:
        """
        Clear all load/unload retry state for the unit specified.

        Usage
        -------
        `OAMS_RESET_RETRY_COUNTS OAMS=<oams_name>`

        Example
        -------
        ```
        OAMS_RESET_RETRY_COUNTS OAMS=oams1
        ```
        """
        self._load_retry_state.clear()
        self._unload_retry_count = 0
        self._last_unload_attempt = 0.0
        self._last_successful_load.clear()
        gcmd.respond_info(f"OAMS[{self.oams_idx}]: Reset all retry counters")

    def _calculate_retry_delay(self, attempt_number: int) -> float:
        """
        Return the delay to wait before a given retry attempt.
        Currently a fixed delay regardless of attempt number.

        :param attempt_number: zero-based index of the upcoming retry (unused).
        :return float: seconds to pause before the next attempt.
        """
        return self.retry_delay

    def _reset_load_retry_count(self, spool_idx: int) -> None:
        """
        Drop any stored load-retry state for a spool.

        :param spool_idx: spool index whose retry state to discard.
        """
        self._load_retry_state.pop(spool_idx, None)

    def _reset_unload_retry_count(self) -> None:
        """
        Reset the unload retry counter and last-attempt timestamp.
        """
        self._unload_retry_count  = 0
        self._last_unload_attempt = 0.0

    def load_spool_with_retry(
        self, spool_idx: int, max_retries: Optional[int] = None
    ) -> Tuple[bool, str]:
        """
        Load a spool, retrying on failure up to the configured limit.
        Between attempts it waits the retry delay, aborts the current action,
        and (when ``auto_unload_on_failed_load`` is set) unloads back to the AMS
        before retrying; a failed pre-retry unload aborts the whole load.
        Updates retry/failure bookkeeping and emits progress logs.

        :param spool_idx: zero-based spool index to load.
        :param max_retries: override for the load retry limit; ``None`` uses the
            configured ``load_retry_max``.
        :return tuple: ``(success, message)`` where ``success`` is ``True`` on a
            successful (or cancelled) load and ``message`` is a human-readable
            result, including attempt history on failure.
        """
        retry = self._load_retry_state.setdefault(spool_idx, RetryState())
        retry_count     = retry.count
        attempt_history = []
        retry_limit     = max_retries if (max_retries is not None
                                          and max_retries > 0 ) else self.load_retry_max

        while retry_count < retry_limit: # pragma: no branch  While loop is always true, suppress coverage warning about always being True
            if retry_count > 0:
                delay = self._calculate_retry_delay(retry_count)
                lane_name = self._resolve_lane_name(spool_idx)
                lane_label = f"lane {lane_name}" if lane_name else f"lane (spool {spool_idx})"
                self.logger.info(
                    f"OAMS[{self.oams_idx}]: Load retry {retry_count + 1}/{retry_limit} for {lane_label}, waiting {delay:.1f}s"
                )
                self.reactor.pause(self.reactor.monotonic() + delay)

                self.abort_current_action(wait=True)
                self.reactor.pause(self.reactor.monotonic() + 1.0)

            retry.count = retry_count + 1
            retry.last_attempt = self.reactor.monotonic()

            code, message = self.load_spool(spool_idx)

            if code == OAMSOpCode.SUCCESS or code == OAMSOpCode.CANCEL:
                self._last_successful_load[spool_idx] = self.reactor.monotonic()
                retry.was_retry = retry_count > 0
                self._reset_load_retry_count(spool_idx)
                lane_name  = self._resolve_lane_name(spool_idx)
                lane_label = f"lane {lane_name}" if lane_name else f"lane (spool {spool_idx})"
                self.logger.info(
                    f"OAMS[{self.oams_idx}]: Successfully loaded {lane_label} on attempt {retry_count + 1}"
                )
                return True, message

            attempt_history.append(f"Attempt {retry_count + 1}: {message}")
            lane_name  = self._resolve_lane_name(spool_idx)
            lane_label = f"lane {lane_name}" if lane_name else f"lane (spool {spool_idx})"

            if retry_count + 1 < retry_limit:
                self.logger.warning(
                    f"OAMS[{self.oams_idx}]: Load failed for {lane_label}: {message}. Attempt {retry_count + 1}/{retry_limit}"
                )

                if self.auto_unload_on_failed_load:
                    self.logger.info(f"OAMS[{self.oams_idx}]: Auto-unloading before retry")
                    unload_success, unload_msg = self.unload_spool_with_retry()

                    if not unload_success:
                        self.logger.error(
                            f"OAMS[{self.oams_idx}]: Failed to unload before retry: {unload_msg}"
                        )
                        self._reset_load_retry_count(spool_idx)
                        self._load_retry_failures += 1
                        self._last_load_failure_time = self.reactor.monotonic()
                        return False, (
                            f"Failed to unload {lane_label} back to AMS before retry. "
                            f"Load aborted after {retry_count + 1} attempts. {unload_msg}"
                        )

                retry_count += 1
            else:
                break

        self._reset_load_retry_count(spool_idx)
        self._load_retry_failures      += 1
        self._last_load_failure_time    = self.reactor.monotonic()
        lane_name  = self._resolve_lane_name(spool_idx)
        lane_label = f"lane {lane_name}" if lane_name else f"lane (spool {spool_idx})"
        history_str = "; ".join(attempt_history)
        return False, (
            f"Failed to load {lane_label} after {retry_limit} attempts. "
            f"Attempt history: {history_str}"
        )

    def get_last_load_attempt_time(self, spool_idx: int) -> Optional[float]:
        """
        Return the monotonic time of the last load attempt for a spool.

        :param spool_idx: spool index to query.
        :return float: monotonic timestamp of the last attempt, or ``None`` if none.
        """
        retry = self._load_retry_state.get(spool_idx)
        return retry.last_attempt if retry is not None else None

    def get_last_successful_load_time(self, spool_idx: int) -> Optional[float]:
        """
        Return the monotonic time of the last successful load for a spool.

        :param spool_idx: spool index to query.
        :return float: monotonic timestamp of the last success, or ``None`` if none.
        """
        return self._last_successful_load.get(spool_idx)

    def last_load_was_retry(self, spool_idx: int) -> bool:
        """
        Report whether the last successful load of a spool needed a retry.

        :param spool_idx: spool index to query.
        :return bool: ``True`` if the last success followed at least one retry.
        """
        retry = self._load_retry_state.get(spool_idx)
        return retry.was_retry if retry is not None else False

    def unload_spool_with_retry(self, max_retries: Optional[int] = None) -> Tuple[bool, str]:
        """
        Unload the current spool, retrying on failure up to the limit.
        Between attempts it waits the retry delay, aborts the current action,
        and retracts the extruder a short distance before retrying. Updates
        unload retry/failure bookkeeping and emits progress logs.

        :param max_retries: override for the unload retry limit; ``None`` uses
            the configured ``unload_retry_max``.
        :return tuple: ``(success, message)`` where ``success`` is ``True`` on a
            successful unload and ``message`` is a human-readable result,
            including attempt history on failure.
        """
        attempt_history = []
        retry_limit     = max_retries if (max_retries is not None
                                          and max_retries > 0) else self.unload_retry_max

        while self._unload_retry_count < retry_limit: # pragma: no branch  While loop is always true, suppress coverage warning about always being True
            if self._unload_retry_count > 0:
                delay = self._calculate_retry_delay(self._unload_retry_count)
                self.logger.info(
                    f"OAMS[{self.oams_idx}]: Unload retry {self._unload_retry_count + 1}/{retry_limit}, waiting {delay:.1f}s"
                )
                self.reactor.pause(self.reactor.monotonic() + delay)

                self.abort_current_action(wait=True)
                self.reactor.pause(self.reactor.monotonic() + 1.0)

                try:
                    self.gcode.run_script_from_command("M83")
                    self.gcode.run_script_from_command("G92 E0")
                    self.gcode.run_script_from_command("G1 E-5.00 F1200")
                    self.gcode.run_script_from_command("M400")
                except Exception as e:
                    self.logger.warning(
                        f"OAMS[{self.oams_idx}]: Failed to retract extruder before unload retry: {e}"
                    )

            self._unload_retry_count += 1
            attempt_number = self._unload_retry_count
            self._last_unload_attempt = self.reactor.monotonic()

            success, message = self.unload_spool()

            if success:
                self._reset_unload_retry_count()
                lane_name  = self._resolve_lane_name(self.current_spool) if self.current_spool is not None else None
                lane_label = f"lane {lane_name}" if lane_name else "filament"
                self.logger.info(
                    f"OAMS[{self.oams_idx}]: Successfully unloaded {lane_label} on attempt {attempt_number}"
                )
                return True, message

            attempt_history.append(f"Attempt {self._unload_retry_count}: {message}")

            if self._unload_retry_count < retry_limit:
                self.logger.warning(
                    f"OAMS[{self.oams_idx}]: Unload failed: {message}. Attempt {self._unload_retry_count}/{retry_limit}"
                )
            else:
                break

        self._reset_unload_retry_count()
        self._unload_retry_failures    += 1
        self._last_unload_failure_time  = self.reactor.monotonic()
        history_str = "; ".join(attempt_history)
        return False, (
            f"Failed to unload after {retry_limit} attempts. "
            f"Attempt history: {history_str}"
        )

    def load_spool_cancel(self) -> str:
        """
        Send the cancel command for an in-progress spool load.

        :return str: a status message indicating the cancel was sent, or that
            the command is unavailable on the current firmware.
        """
        if self.oams_load_spool_cancel_cmd is not None:
            self.oams_load_spool_cancel_cmd.send()
            return "OAMS load spool operation cancelled"
        return "OAMS load spool cancel command not available on this firmware"

    cmd_OAMS_CURRENT_PID_SET_help = "Set the PID values for the current sensor"
    def cmd_OAMS_CURRENT_PID_SET(self, gcmd: GCodeCommand) -> None:
        """
        Set the current-loop PID gains. Requires P, I and D; TARGET defaults to
        the configured current target. Pushes the values (as u32-encoded
        floats) to firmware and updates the cached gains.

        Usage
        -------
        `OAMS_CURRENT_PID_SET OAMS=<oams_name> P=<p> I=<i> D=<d> TARGET=<target>`

        Example
        -------
        ```
        OAMS_CURRENT_PID_SET OAMS=oams1 P=0.5 I=0.1 D=0.01 TARGET=500
        ```
        """
        p = gcmd.get_float("P", None)
        i = gcmd.get_float("I", None)
        d = gcmd.get_float("D", None)
        t = gcmd.get_float("TARGET", None)
        if p is None:
            raise gcmd.error("P value is required")
        if i is None:
            raise gcmd.error("I value is required")
        if d is None:
            raise gcmd.error("D value is required")
        if t is None:
            t = self.current_target
        kp = self.float_to_u32(p)
        ki = self.float_to_u32(i)
        kd = self.float_to_u32(d)
        kt = self.float_to_u32(t)
        self.oams_pid_cmd.send([kp, ki, kd, kt])
        self.current_kp = p
        self.current_ki = i
        self.current_kd = d
        self.current_target = t
        gcmd.respond_info(
            "Current PID values set to P=%f I=%f D=%f TARGET=%f" % (p, i, d, t)
        )

    cmd_OAMS_PID_SET_help = "Set the PID values for the OAMS"
    def cmd_OAMS_PID_SET(self, gcmd: GCodeCommand) -> None:
        """
        Set the pressure (FPS) loop PID gains. Requires P, I and D; TARGET
        defaults to the configured FPS target. Pushes the values (as
        u32-encoded floats) to firmware and updates the cached gains.

        Usage
        -------
        `OAMS_PID_SET OAMS=<oams_name> P=<p> I=<i> D=<d> TARGET=<target>`

        Example
        -------
        ```
        OAMS_PID_SET OAMS=oams1 P=0.5 I=0.1 D=0.01 TARGET=500
        ```
        """
        p = gcmd.get_float("P", None)
        i = gcmd.get_float("I", None)
        d = gcmd.get_float("D", None)
        t = gcmd.get_float("TARGET", None)
        if p is None:
            raise gcmd.error("P value is required")
        if i is None:
            raise gcmd.error("I value is required")
        if d is None:
            raise gcmd.error("D value is required")
        if t is None:
            t = self.fps_target
        kp = self.float_to_u32(p)
        ki = self.float_to_u32(i)
        kd = self.float_to_u32(d)
        kt = self.float_to_u32(t)
        self.oams_pid_cmd.send([kp, ki, kd, kt])
        self.kp = p
        self.ki = i
        self.kd = d
        self.fps_target = t
        gcmd.respond_info("PID values set to P=%f I=%f D=%f TARGET=%f" % (p, i, d, t))

    cmd_OAMS_PID_AUTOTUNE_help = "Run PID autotune"
    def cmd_OAMS_PID_AUTOTUNE(self, gcmd: GCodeCommand) -> None:
        """
        Drive a flow for PID autotuning. Requires TARGET_FLOW (mm^3/s) and
        TARGET_TEMP (degrees C). Heats the hotend and extrudes for ~30 s at
        the speed implied by the requested volumetric flow through 1.75 mm
        filament.

        Usage
        -------
        `OAMS_PID_AUTOTUNE OAMS=<oams_name> TARGET_FLOW=<mm^3/s> TARGET_TEMP=<degrees C>`

        Example
        -------
        ```
        OAMS_PID_AUTOTUNE OAMS=oams1 TARGET_FLOW=10 TARGET_TEMP=230
        ```
        """
        target_flow = gcmd.get_float("TARGET_FLOW", None)
        target_temp = gcmd.get_float("TARGET_TEMP", None)

        if target_flow is None:
            raise gcmd.error("TARGET flowrate in mm^3/s is required")
        if target_temp is None:
            raise gcmd.error("TARGET temperature in degrees C is required")

        extrusion_speed_per_min = (60 * target_flow / (pi * (1.75 / 2) ** 2))
        extrusion_length = (extrusion_speed_per_min / 60 * 30)

        self.gcode.run_script_from_command("M104 S%f" % target_temp)
        self.gcode.run_script_from_command(
            "G1 E%f F%f" % (extrusion_length, extrusion_speed_per_min))

    cmd_OAMS_CALIBRATE_HUB_HES_help = "Calibrate the range of a single hub HES"
    def cmd_OAMS_CALIBRATE_HUB_HES(self, gcmd: GCodeCommand) -> None:
        """
        Calibrate one hub Hall sensor. Requires SPOOL (0-3). Runs the firmware
        calibration, waits for it to finish, and on success stores the
        measured threshold into hub_hes_on and rewrites it to the config file.

        Usage
        -------
        `OAMS_CALIBRATE_HUB_HES OAMS=<oams_name> SPOOL=<0-3>`

        Example
        -------
        ```
        OAMS_CALIBRATE_HUB_HES OAMS=oams1 SPOOL=0
        ```
        """
        spool_idx = gcmd.get_int("SPOOL", None)
        if spool_idx is None:
            raise gcmd.error("SPOOL index is required")
        if spool_idx < 0 or spool_idx > 3:
            raise gcmd.error("Invalid SPOOL index")
        self.action_status = OAMSStatus.CALIBRATING

        self.oams_calibrate_hub_hes_cmd.send([spool_idx])
        while self.action_status is not None:
            self.reactor.pause(self.reactor.monotonic() + 0.5)
        if self.action_status_code == OAMSOpCode.SUCCESS:
            value = self.u32_to_float(self.action_status_value)
            gcmd.respond_info("Calibrated HES %d to %f threshold" % (spool_idx, value))

            self.hub_hes_on[spool_idx] = value
            values = ", ".join(map(str, self.hub_hes_on))
            cal_msg = "\n%s hub_hes_on: %s" % (self.config_name, values)
            self.afc.function.ConfigRewrite(self.config_name, "hub_hes_on", values, cal_msg)
            gcmd.respond_info("HES calibration complete: hub_hes_on index %d = %f saved to config" % (spool_idx, value))
        else:
            raise gcmd.error("Calibration of HES %d failed" % spool_idx)

    cmd_OAMS_CALIBRATE_PTFE_LENGTH_help = "Calibrate the length of the PTFE tube"
    def cmd_OAMS_CALIBRATE_PTFE_LENGTH(self, gcmd: GCodeCommand) -> None:
        """
        Measure the PTFE tube length. Requires SPOOL. Runs the firmware
        calibration, waits for it to finish, and on success rewrites the
        measured ptfe_length to config.

        Usage
        -------
        `OAMS_CALIBRATE_PTFE_LENGTH OAMS=<oams_name> SPOOL=<index>`

        Example
        -------
        ```
        OAMS_CALIBRATE_PTFE_LENGTH OAMS=oams1 SPOOL=0
        ```
        """
        self.action_status = OAMSStatus.CALIBRATING
        spool = gcmd.get_int("SPOOL", None)
        if spool is None:
            raise gcmd.error("SPOOL index is required")

        self.oams_calibrate_ptfe_length_cmd.send([spool])
        while self.action_status is not None:
            self.reactor.pause(self.reactor.monotonic() + 0.5)
        if self.action_status_code == OAMSOpCode.SUCCESS:
            ptfe_val = "%d" % self.action_status_value
            gcmd.respond_info("Calibrated PTFE length to %s" % ptfe_val)

            cal_msg = "\n%s ptfe_length: %s" % (self.config_name, ptfe_val)
            self.afc.function.ConfigRewrite(self.config_name, "ptfe_length", ptfe_val, cal_msg)
            gcmd.respond_info("PTFE calibration complete: ptfe_length %s saved to config" % ptfe_val)
        else:
            raise gcmd.error("Calibration of PTFE length failed")

    def load_spool(self, spool_idx: int) -> Tuple[int, str]:
        """
        Send a single load command and block until firmware reports a result.
        Times out after 45 s if the MCU stops responding. On success updates
        ``current_spool``.

        :param spool_idx: zero-based spool index to load.
        :return tuple: ``(code, message)`` where ``code`` is an
            :class:`OAMSOpCode` and ``message`` describes the outcome.
        """
        self.action_status = OAMSStatus.LOADING
        self.oams_load_spool_cmd.send([spool_idx])
        start          = self.reactor.monotonic()
        timeout        = start + 45.0
        stall_enabled  = self.load_stall_dwell > 0.0
        last_clicks    = self.encoder_clicks
        last_move_time = start

        while self.action_status is not None:
            now = self.reactor.monotonic()

            # Early stall detection: during a load the encoder ticks continuously
            # as filament feeds. If it goes flat past the spin-up grace window the
            # spool is stuck -- cancel and bail out rather than sitting the full
            # 45s MCU timeout, which is a poor experience and delays recovery.
            if stall_enabled and self.encoder_clicks != last_clicks:
                last_clicks    = self.encoder_clicks
                last_move_time = now
            if (stall_enabled
                    and now - start > self.load_stall_grace
                    and now - last_move_time > self.load_stall_dwell):
                self.logger.error(
                    f"OAMS[{self.oams_idx}]: Load stalled - encoder stopped advancing "
                    f"for {self.load_stall_dwell:.0f}s (spool stuck)"
                )
                try:
                    self.load_spool_cancel()
                except Exception as e:
                    self.logger.warning(
                        f"OAMS[{self.oams_idx}]: Failed to cancel stalled load: {e}"
                    )
                self.action_status      = None
                self.action_status_code = OAMSOpCode.ERROR_UNSPECIFIED
                return OAMSOpCode.ERROR_UNSPECIFIED, "OAMS load stalled (spool stuck, no encoder movement)"

            if now > timeout:
                self.logger.error(f"OAMS[{self.oams_idx}]: Load operation timed out after 45 seconds")
                # The firmware is still running its load routine (e.g. a stuck
                # spool that never trips the hub sensor). Clearing only the
                # host-side action_status leaves the MCU wedged and it rejects
                # every subsequent command with ERROR_BUSY until reboot, so send
                # the firmware cancel to actually release it before we give up.
                try:
                    self.load_spool_cancel()
                except Exception as e:
                    self.logger.warning(
                        f"OAMS[{self.oams_idx}]: Failed to cancel stuck load after timeout: {e}"
                    )
                self.action_status      = None
                self.action_status_code = OAMSOpCode.ERROR_UNSPECIFIED
                return OAMSOpCode.ERROR_UNSPECIFIED, "OAMS load operation timed out (MCU unresponsive)"
            self.reactor.pause(self.reactor.monotonic() + 0.2)

        if self.action_status_code == OAMSOpCode.SUCCESS:
            self.current_spool = spool_idx
            return self.action_status_code, "Spool loaded successfully"
        elif self.action_status_code == OAMSOpCode.ERROR_KLIPPER_CALL:
            return self.action_status_code, "Spool loading stopped by klipper monitor"
        elif self.action_status_code == OAMSOpCode.ERROR_BUSY:
            return self.action_status_code, "OAMS is busy"
        elif self.action_status_code == OAMSOpCode.CANCEL:
            return self.action_status_code, "Spool loading cancelled"
        else:
            return self.action_status_code, "Unknown error from OAMS with code %d" % self.action_status_code

    cmd_OAMS_LOAD_SPOOL_help = "Load a new spool of filament"
    def cmd_OAMS_LOAD_SPOOL(self, gcmd: GCodeCommand) -> None:
        """
        Load a spool with retry. Requires SPOOL (0-3); QUIET suppresses the
        success message. Delegates to load_spool_with_retry and reports the
        result.

        Usage
        -------
        `OAMS_LOAD_SPOOL OAMS=<oams_name> SPOOL=<0-3> QUIET=<0 or 1>`

        Example
        -------
        ```
        OAMS_LOAD_SPOOL OAMS=oams1 SPOOL=0 QUIET=0
        ```
        """
        self.action_status = OAMSStatus.LOADING
        spool_idx = gcmd.get_int("SPOOL", None)
        if spool_idx is None:
            raise gcmd.error("SPOOL index is required")
        if spool_idx < 0 or spool_idx > 3:
            raise gcmd.error("Invalid SPOOL index")

        quiet = gcmd.get_int("QUIET", 0)
        success, message = self.load_spool_with_retry(spool_idx)

        if success and not quiet:
            gcmd.respond_info(message)
        elif not success:
            raise gcmd.error(message)

    def unload_spool(self) -> Tuple[bool, str]:
        """
        Send a single unload command and block until firmware reports back.
        Times out after 40 s if the MCU stops responding. Treats both success
        and ``NO_SPOOL_IN_BAY`` as unloaded and clears ``current_spool``.

        :return tuple: ``(success, message)`` where ``success`` is ``True`` when
            the bay ends up empty and ``message`` describes the outcome.
        """
        self.action_status = OAMSStatus.UNLOADING
        self.oams_unload_spool_cmd.send()
        timeout = self.reactor.monotonic() + 40.0

        while self.action_status is not None:
            if self.reactor.monotonic() > timeout:
                self.logger.error(f"OAMS[{self.oams_idx}]: Unload operation timed out after 40 seconds")
                self.action_status      = None
                self.action_status_code = OAMSOpCode.ERROR_UNSPECIFIED
                return False, "OAMS unload operation timed out (MCU unresponsive)"
            self.reactor.pause(self.reactor.monotonic() + 0.2)

        if self.action_status_code == OAMSOpCode.SUCCESS:
            self.current_spool = None
            return True, "Spool unloaded successfully"
        elif self.action_status_code == OAMSOpCode.ERROR_KLIPPER_CALL:
            return False, "Spool unloading stopped by klipper monitor"
        elif self.action_status_code == OAMSOpCode.ERROR_BUSY:
            return False, "OAMS is busy"
        elif self.action_status_code == OAMSOpCode.NO_SPOOL_IN_BAY:
            self.current_spool = None
            return True, "Spool already unloaded (NO_SPOOL_IN_BAY)"
        elif self.action_status_code == OAMSOpCode.CANCEL:
            return False, "Unload was cancelled (stale cancel response interfered)"
        else:
            return False, (
                "Unknown error from OAMS (status_code=%s)"
                % self.action_status_code
            )

    cmd_OAMS_UNLOAD_SPOOL_help = "Unload a spool of filament"
    def cmd_OAMS_UNLOAD_SPOOL(self, gcmd: GCodeCommand) -> None:
        """
        Unload the current spool with retry. Optional MAX_RETRIES overrides
        the configured limit. Delegates to unload_spool_with_retry and
        reports the result.

        Usage
        -------
        `OAMS_UNLOAD_SPOOL OAMS=<oams_name> MAX_RETRIES=<count>`

        Example
        -------
        ```
        OAMS_UNLOAD_SPOOL OAMS=oams1 MAX_RETRIES=3
        ```
        """
        max_retries = gcmd.get_int("MAX_RETRIES", None)
        success, message = self.unload_spool_with_retry(max_retries=max_retries)
        if success:
            gcmd.respond_info(message)
        else:
            raise gcmd.error(message)

    cmd_OAMS_ABORT_ACTION_help = "Abort the current OAMS action"
    def cmd_OAMS_ABORT_ACTION(self, gcmd: GCodeCommand) -> None:
        """
        Abort the in-flight OAMS action. Optional CODE sets the result code
        recorded for the aborted action (default ERROR_KLIPPER_CALL); WAIT
        controls whether to wait for firmware to settle.

        Usage
        -------
        `OAMS_ABORT_ACTION OAMS=<oams_name> CODE=<code> WAIT=<0 or 1>`

        Example
        -------
        ```
        OAMS_ABORT_ACTION OAMS=oams1 WAIT=1
        ```
        """
        code = gcmd.get_int("CODE", OAMSOpCode.ERROR_KLIPPER_CALL)
        wait = gcmd.get_int("WAIT", 1)
        self.abort_current_action(code=code, wait=bool(wait))

    def set_oams_follower(self, enable: int, direction: int) -> None:
        """
        Enable/disable the follower motor and set its direction via the MCU.

        :param enable: 1 to enable the follower, 0 to disable.
        :param direction: 1 for forward, 0 for reverse.
        """
        self.oams_follower_cmd.send([enable, direction])

    def abort_current_action(
        self, code: OAMSOpCode = OAMSOpCode.ERROR_KLIPPER_CALL, wait: bool = True
    ) -> None:
        """
        Clear the in-flight action, optionally waiting for firmware to settle.
        Returns immediately if no action is in progress. When ``wait`` is set it
        polls (with backoff) for up to 5 s before force-clearing the status.

        :param code: result code to record for the aborted action.
        :param wait: whether to wait for the current action to drain first.
        """
        if self.action_status is None:
            return

        # Tell the firmware to actually stop the in-flight action. Clearing only
        # the host-side action_status leaves the MCU wedged in its load routine,
        # which then rejects every subsequent command with ERROR_BUSY until the
        # unit is power-cycled.
        try:
            self.load_spool_cancel()
        except Exception as e:
            self.logger.warning(
                f"OAMS[{self.oams_idx}]: Failed to send firmware cancel during abort: {e}"
            )

        if wait:
            self.logger.debug(
                f"OAMS[{self.oams_idx}]: Aborting current action {self.action_status} with code {code}"
            )
            timeout     = self.reactor.monotonic() + 5.0
            pause_delay = 0.5
            while self.action_status is not None:
                if self.reactor.monotonic() > timeout:
                    self.logger.debug(f"OAMS[{self.oams_idx}]: Abort timeout - forcing clear")
                    break
                self.reactor.pause(self.reactor.monotonic() + pause_delay)
                pause_delay = min(pause_delay + 0.25, 1.5)

            self.action_status_code  = code
            self.action_status_value = None
            self.action_status       = None
            self.logger.info(f"OAMS[{self.oams_idx}]: Abort complete")
        else:
            self.action_status_code  = code
            self.action_status_value = None
            self.action_status       = None
            self.logger.debug(f"OAMS[{self.oams_idx}]: Abort without waiting - status cleared")

    cmd_OAMS_FOLLOWER_help = "Enable or disable follower and set its direction"
    def cmd_OAMS_FOLLOWER(self, gcmd: GCodeCommand) -> None:
        """
        Enable/disable the follower and set its direction. Requires ENABLE
        and DIRECTION; responds describing the resulting follower state.

        Usage
        -------
        `OAMS_FOLLOWER OAMS=<oams_name> ENABLE=<0 or 1> DIRECTION=<0 or 1>`

        Example
        -------
        ```
        OAMS_FOLLOWER OAMS=oams1 ENABLE=1 DIRECTION=1
        ```
        """
        enable = gcmd.get_int("ENABLE", None)
        if enable is None:
            raise gcmd.error("ENABLE is required")
        direction = gcmd.get_int("DIRECTION", None)
        if direction is None:
            raise gcmd.error("DIRECTION is required")

        self.set_oams_follower(enable, direction)
        if enable == 1 and direction == 0:
            gcmd.respond_info("Follower enable in reverse direction")
        elif enable == 1 and direction == 1:
            gcmd.respond_info("Follower enable in forward direction")
        elif enable == 0:
            gcmd.respond_info("Follower disabled")

    cmd_AFC_STOP_OAMS_FOLLOWER_help = "Stop the OAMS follower motor"
    def cmd_AFC_STOP_OAMS_FOLLOWER(self, gcmd: GCodeCommand) -> None:
        """
        Stop this unit's follower motor by disabling it.

        Usage
        -------
        `AFC_STOP_OAMS_FOLLOWER OAMS=<oams_name>`

        Example
        -------
        ```
        AFC_STOP_OAMS_FOLLOWER OAMS=oams1
        ```
        """
        self.gcode.run_script_from_command(
            f"OAMS_FOLLOWER OAMS={self.oams_idx} ENABLE=0 DIRECTION=1")

    cmd_AFC_START_OAMS_FOLLOWER_help = "Start the OAMS follower motor"
    def cmd_AFC_START_OAMS_FOLLOWER(self, gcmd: GCodeCommand) -> None:
        """
        Start this unit's follower motor, enabled in the forward direction.

        Usage
        -------
        `AFC_START_OAMS_FOLLOWER OAMS=<oams_name>`

        Example
        -------
        ```
        AFC_START_OAMS_FOLLOWER OAMS=oams1
        ```
        """
        self.gcode.run_script_from_command(
            f"OAMS_FOLLOWER OAMS={self.oams_idx} ENABLE=1 DIRECTION=1")

    def _oams_cmd_stats(self, params: Dict[str, Any]) -> None:
        """
        MCU ``oams_cmd_stats`` callback: cache sensor and encoder values.
        Decodes the FPS pressure value and stores the four f1s and four hub
        Hall-effect readings plus the encoder click count.

        :param params: parsed MCU message fields.
        """
        self.fps_value = self.u32_to_float(params["fps_value"])
        self.f1s_hes_value[0] = params["f1s_hes_value_0"]
        self.f1s_hes_value[1] = params["f1s_hes_value_1"]
        self.f1s_hes_value[2] = params["f1s_hes_value_2"]
        self.f1s_hes_value[3] = params["f1s_hes_value_3"]
        self.hub_hes_value[0] = params["hub_hes_value_0"]
        self.hub_hes_value[1] = params["hub_hes_value_1"]
        self.hub_hes_value[2] = params["hub_hes_value_2"]
        self.hub_hes_value[3] = params["hub_hes_value_3"]
        self.encoder_clicks = params["encoder_clicks"]

    def _oams_cmd_current_status(self, params: Dict[str, Any]) -> None:
        """
        MCU ``oams_cmd_current_status`` callback: cache the motor current.

        :param params: parsed MCU message fields containing ``current_value``.
        """
        self.i_value = self.u32_to_float(params["current_value"])

    def get_current(self) -> float:
        """
        Return the most recent motor current reading.

        :return float: the last reported current value (amps).
        """
        return self.i_value

    def _oams_action_status(self, params: Dict[str, Any]) -> None:
        """
        MCU ``oams_action_status`` callback: update in-flight action state.
        Clears ``action_status`` and records the result code for load, unload,
        error and calibration actions (capturing the calibration value), and
        logs follower/coasting/stopped and unhandled updates.

        :param params: parsed MCU message fields with ``action``/``code`` and,
            for calibration, ``value``.
        """
        self.logger.debug("OAMS status received")
        action = params["action"]
        code   = params["code"]

        if action in (OAMSStatus.LOADING, OAMSStatus.UNLOADING, OAMSStatus.ERROR):
            self.action_status = None
            self.action_status_code = code
        elif action == OAMSStatus.CALIBRATING:
            self.action_status = None
            self.action_status_code = code
            self.action_status_value = params["value"]
        elif code == OAMSOpCode.ERROR_KLIPPER_CALL:
            self.action_status = None
            self.action_status_code = code
        elif action in (
            OAMSStatus.FORWARD_FOLLOWING,
            OAMSStatus.REVERSE_FOLLOWING,
            OAMSStatus.COASTING,
            OAMSStatus.STOPPED,
        ):
            self.logger.debug(
                f"OAMS status update (non-action): "
                f"{_oams_enum_name(OAMSStatus, action, 'action')} "
                f"({_oams_enum_name(OAMSOpCode, code, 'code')})"
            )
        else:
            self.logger.debug(
                f"OAMS status update (unhandled): "
                f"{_oams_enum_name(OAMSStatus, action, 'action')} "
                f"({_oams_enum_name(OAMSOpCode, code, 'code')})"
            )

    def float_to_u32(self, f: float) -> int:
        """
        Reinterpret a float's raw IEEE-754 bits as an unsigned 32-bit int.
        Used to ship float values through integer MCU command arguments.

        :param f: the float value to encode.
        :return int: the float's bit pattern as a u32.
        """
        return _U32_STRUCT.unpack(_FLOAT_STRUCT.pack(f))[0]

    def u32_to_float(self, i: int) -> float:
        """
        Reinterpret an unsigned 32-bit int's bits as an IEEE-754 float.
        Inverse of :meth:`float_to_u32`, used to decode floats from u32 MCU
        message fields.

        :param i: the u32 bit pattern to decode.
        :return float: the decoded float value.
        """
        return _FLOAT_STRUCT.unpack(_U32_STRUCT.pack(i))[0]

    def _build_config(self) -> None:
        """
        Emit the firmware configuration commands on MCU config build.
        Sends the buffer thresholds, f1s and hub Hall-effect calibration, the
        pressure and current PID gains/targets, the PTFE length and the logger
        index so firmware matches this controller's configuration.
        """
        self.mcu.add_config_cmd(
            "config_oams_buffer upper=%u lower=%u is_reversed=%u"
            % (
                self.float_to_u32(self.fps_upper_threshold),
                self.float_to_u32(self.fps_lower_threshold),
                self.fps_is_reversed,
            )
        )

        self.mcu.add_config_cmd(
            "config_oams_f1s_hes on1=%u on2=%u on3=%u on4=%u is_above=%u"
            % (
                self.float_to_u32(self.f1s_hes_on[0]),
                self.float_to_u32(self.f1s_hes_on[1]),
                self.float_to_u32(self.f1s_hes_on[2]),
                self.float_to_u32(self.f1s_hes_on[3]),
                self.f1s_hes_is_above,
            )
        )

        self.mcu.add_config_cmd(
            "config_oams_hub_hes on1=%u on2=%u on3=%u on4=%u is_above=%u"
            % (
                self.float_to_u32(self.hub_hes_on[0]),
                self.float_to_u32(self.hub_hes_on[1]),
                self.float_to_u32(self.hub_hes_on[2]),
                self.float_to_u32(self.hub_hes_on[3]),
                self.hub_hes_is_above,
            )
        )

        self.mcu.add_config_cmd(
            "config_oams_pid kp=%u ki=%u kd=%u target=%u"
            % (
                self.float_to_u32(self.kp),
                self.float_to_u32(self.ki),
                self.float_to_u32(self.kd),
                self.float_to_u32(self.fps_target),
            )
        )

        self.mcu.add_config_cmd(
            "config_oams_ptfe length=%u" % (self.filament_path_length)
        )

        self.mcu.add_config_cmd(
            "config_oams_current_pid kp=%u ki=%u kd=%u target=%u"
            % (
                self.float_to_u32(self.current_kp),
                self.float_to_u32(self.current_ki),
                self.float_to_u32(self.current_kd),
                self.float_to_u32(self.current_target),
            )
        )

        self.mcu.add_config_cmd("config_oams_logger idx=%u" % (self.oams_idx))


def load_config_prefix(config: ConfigWrapper) -> AFC_OAMS:
    """
    Klipper entry point for ``[AFC_OAMS ...]`` config sections.

    :param config: the ``ConfigWrapper`` for the section being loaded.
    :return AFC_OAMS: the constructed AFC_OAMS controller instance.
    """
    return AFC_OAMS(config)
