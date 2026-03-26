# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import chelper
import traceback

from kinematics import extruder
from configfile import error
from extras.force_move import calc_move_time

from typing import Optional, TYPE_CHECKING, Dict

try: from extras.AFC_utils import ERROR_STR
except:
    raise_string = "Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc())
    raise error(raise_string)

try: from extras.AFC_lane import AFCLane, AFCHomingPoints
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

if TYPE_CHECKING:
    from toolhead import Toolhead
    from stepper import PrinterStepper
    from mcu import MCU_endstop


LARGE_TIME_OFFSET = 99999.9
# Manually adding since these are in different files between klipper and kalico
DRIP_TIME=0.100
STEPCOMPRESS_FLUSH_TIME=0.050
BUFFER_TIME_HIGH=1.0
DRIP_SEGMENT_TIME=0.050

class AFCExtruderStepper(AFCLane):
    def __init__(self, config):
        super().__init__(config)

        self.extruder_stepper   = extruder.ExtruderStepper(config)
        self.stepper_enable     = self.printer.lookup_object('stepper_enable')

        # Homing-related configuration
        self.default_homing_endstop = config.get('default_homing_endstop', 'load')
        self._homing_accel          = self.short_moves_accel # Should only be updated in do_homing_move so accel can be used in drip_move callback
        self._manual_axis_pos       = 0.0

        # Optional explicit hub endstop pin override in stepper section
        self.hub_endstop = config.get('hub_endstop', None)

        self._extra_homing_pins = set(config.getlist('homing_endstop_pins', default="", sep=',')) # Additional homing endstops to add to stepper, comma-seperated list
        # Pre-create MCU endstops now so MCU config callbacks run before ready
        self._endstops: Dict[str, tuple[MCU_endstop,str]] = {}
        self._init_endstops()

        # Register AFC_HOME mux command for this lane name
        stepper_name = config.get_name().split()[-1]
        self.function.register_mux_command(self.show_macros, "AFC_STEPPER_HOME", "STEPPER",
                                           stepper_name, self.cmd_AFC_STEPPER_HOME,
                                           description=self.cmd_AFC_STEPPER_HOME_help,
                                           options=self.cmd_AFC_STEPPER_HOME_options)

        # Check for Klipper new motion queuing update
        self.motion_queuing = self.printer.load_object(config, "motion_queuing", None)

        self.next_cmd_time = 0.

        ffi_main, ffi_lib = chelper.get_ffi()
        self.stepper_kinematics = ffi_main.gc(
            ffi_lib.cartesian_stepper_alloc(b'x'), ffi_lib.free)

        if self.motion_queuing is not None:
            self.trapq          = self.motion_queuing.allocate_trapq()
            self.trapq_append   = self.motion_queuing.lookup_trapq_append()
        else:
            self.trapq                  = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
            self.trapq_append           = ffi_lib.trapq_append
            self.trapq_finalize_moves   = ffi_lib.trapq_finalize_moves

        self.assist_activate=False

        # Current to use while printing, set to a lower current to reduce stepper heat when printing.
        # Defaults to global_print_current, if not specified current is not changed.
        self.tmc_print_current = config.getfloat("print_current", self.afc.global_print_current)
        self.tmc_load_current = None
        if self.tmc_print_current is not None:
            self._get_tmc_values( config )

        # Get and save base rotation dist
        self.base_rotation_dist = self.extruder_stepper.stepper.get_rotation_distance()[0]

    def _get_tmc_values(self, config):
        """
        Searches for TMC driver that corresponds to stepper to get run current that is specified in config
        """
        try:
            self.tmc_driver = next(config.getsection(s) for s in config.fileconfig.sections() if 'tmc' in s and config.get_name() in s)
        except:
            msg = f"Could not find TMC for stepper {self.name},"
            msg += "\nplease add TMC section or disable 'print_current' from config files"
            raise self.gcode.error(msg)

        self.tmc_load_current = self.tmc_driver.getfloat('run_current')

    def _submit_move( self, movetime: float, distance: float, speed: float, accel: float) -> float:
        """
        Helper function for calculating moves and adding to trapq

        :param movetime: Base time to start moves when adding to trapq
        :param distance: Distance to move stepper
        :param speed: Speed to use when calculating moves
        :param accel: Acceleration to use when calculating moves

        :return float: Returns total time move will take for specified distance at specified speed/accel
        """
        self.extruder_stepper.stepper.set_position((0., 0., 0.))
        axis_r, accel_t, cruise_t, cruise_v = calc_move_time(distance, speed, accel)
        self.trapq_append(self.trapq, movetime, accel_t, cruise_t, accel_t,
                              0., 0., 0., axis_r, 0., 0., 0., cruise_v, accel)
        return accel_t + cruise_t + accel_t

    def _move(self, distance: float, speed: float, accel: float, assist_active: bool=False,
              sync: bool=True):
        """
        Helper function to move the specified lane a given distance with specified speed and acceleration.
        This function calculates the movement parameters and commands the stepper motor
        to move the lane accordingly.
        Parameters:
        distance (float): The distance to move.
        speed (float): The speed of the movement.
        accel (float): The acceleration of the movement.
        """
        if sync: self.sync_print_time()

        with self.assist_move(speed, distance < 0, assist_active):
            # Code based off force_move.py manual_move function
            toolhead = self.printer.lookup_object('toolhead')
            toolhead.flush_step_generation()
            prev_sk     = self.extruder_stepper.stepper.set_stepper_kinematics(self.stepper_kinematics)
            prev_trapq  = self.extruder_stepper.stepper.set_trapq(self.trapq)
            print_time = toolhead.get_last_move_time()

            dwell_time = self._submit_move( print_time, distance, speed, accel)
            print_time += dwell_time

            if self.motion_queuing is None:
                self.extruder_stepper.stepper.generate_steps(print_time)
                self.trapq_finalize_moves(self.trapq, print_time + LARGE_TIME_OFFSET,
                                        print_time + LARGE_TIME_OFFSET)
                toolhead.note_mcu_movequeue_activity(print_time)
            else:
                self.motion_queuing.note_mcu_movequeue_activity(print_time)

            if sync:
                toolhead.dwell( dwell_time )
                toolhead.flush_step_generation()
            self.extruder_stepper.stepper.set_trapq(prev_trapq)
            self.extruder_stepper.stepper.set_stepper_kinematics(prev_sk)
            if self.motion_queuing is not None:
                self.motion_queuing.wipe_trapq(self.trapq)
            if sync: toolhead.wait_moves()

    def move(self, distance: float, speed: float, accel: float, assist_active=False):
        """
        Move the specified lane a given distance with specified speed and acceleration.
        This function calculates the movement parameters and commands the stepper motor
        to move the lane accordingly.
        Parameters:
        distance (float): The distance to move.
        speed (float): The speed of the movement.
        accel (float): The acceleration of the movement.
        """
        direction = 1 if distance > 0 else -1
        move_total = abs(distance)
        if direction == -1:
            speed = speed * self.rev_long_moves_speed_factor

        # Breaks up move length to help with TTC errors
        while move_total > 0:
            move_value = self.max_move_dis if move_total > self.max_move_dis else move_total
            move_total -= move_value
            # Adding back direction
            move_value = move_value * direction

            self._move(move_value, speed, accel, assist_active)

    def do_enable(self, enable):
        """
        Helper function to enable/disable stepper motor

        :param enable: Enables/disables stepper motor
        """
        if hasattr(self.stepper_enable, "set_motors_enable"):
            # New klipper enable function
            self.stepper_enable.set_motors_enable([f"AFC_stepper {self.name}"], enable)
        else:
            # Old klipper and kalico enable function
            self.stepper_enable.motor_debug_enable(f"AFC_stepper {self.name}", enable)

    def sync_print_time(self):
        """
        Helper function to get current print time that compares to previous synced time
        If last print time is greater than current print time, calls a toolhead dwell
        If print time is greater than last, self.new_cmd_time gets updated
        """
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        if self.next_cmd_time > print_time:
            toolhead.dwell(self.next_cmd_time - print_time)
        else:
            self.next_cmd_time = print_time

    def sync_to_extruder(self, update_current=True, extruder_name=None):
        """
        Helper function to sync lane to extruder and set print current if specified.

        :param update_current: Sets current to specified print current when True
        """
        if extruder_name is None:
            extruder_name = self.extruder_name

        self.extruder_stepper.sync_to_extruder(extruder_name)
        if update_current: self.set_print_current()

    def unsync_to_extruder(self, update_current=True):
        """
        Helper function to un-sync lane to extruder and set load current if specified.

        :param update_current: Sets current to specified load current when True
        """
        self.extruder_stepper.sync_to_extruder(None)
        if update_current: self.set_load_current()

    def _set_current(self, current):
        """
        Helper function to update TMC current.

        :param current: Sets TMC current to specified value
        """
        if self.tmc_print_current is not None and current is not None:
            command = "SET_TMC_CURRENT STEPPER='{}' CURRENT={}".format(self.name, current)
            self.logger.debug(f"Running macro: {command}")
            self.gcode.run_script_from_command(command)

    def set_load_current(self):
        """
        Helper function to update TMC current to use run current value
        """
        self._set_current( self.tmc_load_current )

    def set_print_current(self):
        """
        Helper function to update TMC current to use print current value
        """
        self._set_current( self.tmc_print_current )

    def update_rotation_distance(self, multiplier):
        """
        Helper function for updating steppers rotation distance

        :param multiplier: Multiplier to set rotation distance. Rotation distance is updated by taking
                          base rotation distance and dividing by multiplier.
        """
        self.extruder_stepper.stepper.set_rotation_distance( self.base_rotation_dist / multiplier )

    # ------------------ ManualStepper compatibility shims ------------------
    # These wrappers allow using Klipper's homing flow with our existing stepper
    def flush_step_generation(self):
        """
        Only syncs print time, is needed for homing flow
        """
        self.sync_print_time()

    def get_position(self) -> list[float]:
        """
        Returns current steppers commanded position in xyze

        :return list[float]: steppers commanded position, x index is steppers current position
        """
        # 1D position along filament path
        return [self.extruder_stepper.stepper.get_commanded_position(), 0., 0., 0.]

    def set_position(self, newpos=0, homing_axes=""):
        """
        Manually set current steppers position, always sets to zero regardless of whats
        passed in. Needed for homing flow.

        :param newpos: Position to set axis
        :param homing_axis: Axis to set position
        """
        self._manual_axis_pos = 0.0

    def get_last_move_time(self) -> float:
        """
        Syncs print time and returns last commanded position time. Needed for homing flow.

        :return float: Last commanded position time.
        """
        self.sync_print_time()
        return self.next_cmd_time

    def dwell(self, delay):
        """
        Needed for homing flow, done in the same way as manual_stepper.py
        """
        # Advance internal time to satisfy homing scheduler
        self.next_cmd_time += max(0., delay)

    def _drip_update_time(self, toolhead: Toolhead, max_time: float, drip_completion) -> None:
        """
        Helper function for mimicking how klipper toolhead does drip moves

        Same as mainline klipper that using motion queue, this is only called if motion
        queue object is not found

        :param toolhead: Klipper toolhead object
        :param max_time: Max time move should take to home to endstop, break out once
                         this time is reached
        :param drip_completion: Endstop callback to test if endstop has been reached or not
        """
        toolhead.special_queuing_state = "Drip"
        toolhead.need_check_pause = self.reactor.NEVER
        self.reactor.update_timer(toolhead.flush_timer, self.reactor.NEVER)
        toolhead.do_kick_flush_timer = False
        toolhead.lookahead.set_flush_time(BUFFER_TIME_HIGH)
        toolhead.check_stall_time = 0.
        flush_delay: float = DRIP_TIME + STEPCOMPRESS_FLUSH_TIME + toolhead.kin_flush_delay

        while toolhead.print_time < max_time:
            if drip_completion.test():
                break
            curtime: float = self.reactor.monotonic()
            est_print_time: float = toolhead.mcu.estimated_print_time(curtime)
            wait_time: float = toolhead.print_time - est_print_time - flush_delay
            if wait_time > 0. and toolhead.can_pause:
                drip_completion.wait(curtime + wait_time)
                continue
            npt = min(toolhead.print_time + DRIP_SEGMENT_TIME, max_time)
            toolhead.note_mcu_movequeue_activity(npt + toolhead.kin_flush_delay)
            toolhead._advance_move_time(npt)

    def drip_move(self, newpos: list[float], speed: float, drip_completion):
        """
        Drip move callback, needed for homing flow. Creates and add moves to trapq then
        waits for homing to complete or exits once max time is hit.

        :param newpos: Position to move when homing to endstop
        :param speed: Speed to move stepper
        :pram drip_completion: Endstop callback to test if move has completed or not
        """
        # Queue the homing move quickly; do not block or dwell here. Homing
        # controller will wait for the endstop and handle final positioning.
        self.sync_print_time()
        target = float(newpos[0])
        delta = target - self._manual_axis_pos

        start_time = self.next_cmd_time

        prev_sk     = self.extruder_stepper.stepper.set_stepper_kinematics(self.stepper_kinematics)
        prev_trapq  = self.extruder_stepper.stepper.set_trapq(self.trapq)

        dwell_time = self._submit_move( start_time, delta, speed, self._homing_accel)
        max_time = start_time + dwell_time

        toolhead: Toolhead = self.printer.lookup_object('toolhead')

        # TODO: add a check for toolhead.drip_update_time and test with https://github.com/KalicoCrew/kalico/pull/825 PR
        if self.motion_queuing is None:
            self._drip_update_time(toolhead, max_time, drip_completion)
            self.reactor.update_timer(toolhead.flush_timer, self.reactor.NOW)
            toolhead.flush_step_generation()
            self.trapq_finalize_moves(self.trapq, self.reactor.NEVER, 0)
            self.extruder_stepper.stepper.set_position([delta, 0., 0.])
        else:
            self.motion_queuing.drip_update_time( start_time, max_time, drip_completion)

        self.extruder_stepper.stepper.set_trapq(prev_trapq)
        self.extruder_stepper.stepper.set_stepper_kinematics(prev_sk)
        if self.motion_queuing is not None:
            self.motion_queuing.wipe_trapq(self.trapq)

        self.sync_print_time()

    def get_kinematics(self) -> AFCExtruderStepper:
        # Homing expects an object with kinematics-like API; we self-provide
        return self

    def get_steppers(self) -> list[PrinterStepper]:
        # Return underlying single stepper for homing code
        return [self.extruder_stepper.stepper]

    def calc_position(self, stepper_positions) -> list[float]:
        # Map stepper positions into kinematic position; unused for filament homing
        return [stepper_positions[self.extruder_stepper.stepper.get_name()], 0., 0.]

    # ------------------ Endstop plumbing ------------------
    def _resolve_endstop_pin(self, endstop_spec) -> tuple[MCU_endstop, str]:
        """
        Resolve an endstop spec into an MCU endstop and a human name.
        - endstop_spec can be 'load', 'hub', 'tool'|'tool_start'|'tool_end',
          'buffer'|'buffer_advance'|'buffer_trailing', or a raw MCU pin string.

        :param endstop_spec: Name of endstop to search for in local mapping
        :return tuple: Returns MCU_endstop and endstop name found in local dictionary
        """
        # Normalize aliases
        alias_map = {
            'tool': 'tool_start',
            'buffer': 'buffer_advance',
        }
        key = alias_map.get(endstop_spec, endstop_spec)
        # Look up a pre-created endstop; do not create at runtime (MCU cb won't run)
        mcu_endstop, name = self._endstops.get(key, (None, None))
        if mcu_endstop is None:
            raise_string = f"Unknown ENDSTOP '{endstop_spec}' for lane '{self.name}'. "\
                           f"Declare it in [AFC_stepper {self.name}] using 'homing_endstop_pins: "\
                           f"{endstop_spec}' or use ENDSTOP=load/hub/tool/buffer"
            raise error(raise_string)
        return (mcu_endstop, name)

    def _init_endstops(self):
        """
        Create and register endstops at config time so MCU callbacks run.
        """
        printer = self.printer
        ppins = printer.lookup_object('pins')
        qes = printer.load_object(self._config, 'query_endstops')

        # Keep handles for use in helper
        self._ppins = ppins
        self._qes = qes

        # Built-ins from AFCLane config
        self._add_endstop('load', self.load, 'load')
        # Extra raw pins declared in config
        for raw_pin in list(self._extra_homing_pins):
            r = raw_pin.strip()
            if not r:
                continue
            # Derive a suffix from the pin token for display; keep key as exact raw string
            suffix = 'raw'
            try:
                token = r.split(':')[-1]
                suffix = token.replace('/', '_').replace('^', '').replace('!', '')
            except Exception:
                pass
            self._add_endstop(r, r, suffix)

        # Hub endstop (register at config time so MCU setup is complete)
        hub_pin = self.hub_endstop
        if hub_pin is None:
            hub_name = getattr(self, 'hub', None)
            if not hub_name or 'direct' in hub_name:
                hub_name = self._inherit_from_unit('hub')
            hub_pin = self._get_section_value('AFC_hub', hub_name, 'switch_pin')

        # Toolhead endstops from AFC_extruder (tool_start/tool_end)
        extruder_name = getattr(self, 'extruder_name', None) or self._inherit_from_unit('extruder')
        tool_start_pin = self._get_section_value('AFC_extruder', extruder_name, 'pin_tool_start')
        tool_end_pin   = self._get_section_value('AFC_extruder', extruder_name, 'pin_tool_end')

        # Buffer endstops from AFC_buffer (advance/trailing), inherit from extruder or unit if needed
        buffer_name = getattr(self, 'buffer_name', None)
        if not buffer_name:
            buffer_name = self._get_section_value('AFC_extruder', extruder_name, 'buffer') or self._inherit_from_unit('buffer')
        buffer_adv_pin   = self._get_section_value('AFC_buffer', buffer_name, 'advance_pin')
        buffer_trail_pin = self._get_section_value('AFC_buffer', buffer_name, 'trailing_pin')

        # Check to verify that hub is not a virtual sensor
        if (hub_pin
            and hub_pin.lower() != "virtual"):
            self._add_endstop('hub', hub_pin, 'hub')
        if tool_start_pin != 'buffer':
            self._add_endstop('tool_start', tool_start_pin, 'tool_start')
        else:
            self._add_endstop('tool_start', buffer_adv_pin, 'tool_start')
        self._add_endstop('tool_end', tool_end_pin, 'tool_end')
        self._add_endstop('buffer_advance', buffer_adv_pin, 'buffer_adv')
        self._add_endstop('buffer_trailing', buffer_trail_pin, 'buffer_trailing')

    def _inherit_from_unit(self, target_key: str) -> Optional[str]:
        """
        Lookup and return value (e.g., 'hub', 'extruder', etc.) from the unit section
        matching this object's unit.

        :param target_key: Key to lookup in unit config section
        :return str: If key is found in unit, returns config value
        """
        unit = getattr(self, 'unit', None)
        if not unit:
            return None

        ignore_prefixes = (
            'AFC_hub ', 'AFC_extruder ', 'AFC_buffer ',
            'AFC_stepper ', 'AFC_functions ', 'AFC_stats ', 'AFC_respond '
        )

        try:
            for sec in self._config.fileconfig.sections():
                if not sec.startswith('AFC_'):
                    continue
                if sec.startswith(ignore_prefixes):
                    continue
                # Match on final token == unit name
                tokens = sec.split()
                if tokens and tokens[-1] == unit:
                    unit_cfg = self._config.getsection(sec)
                    value = unit_cfg.get(target_key, None)
                    if value is not None:
                        self.logger.debug(f"Inherited '{target_key}'='{value}' from section '{sec}' for unit '{unit}' {self.name}")
                    return value
        except Exception as e:
            err_str = f"_inherit_from_unit({target_key}) failed: {e}"
            self.logger.debug(err_str)

        return None

    def _get_section_value(self, section_prefix: str, name: str, key: str, default=None) -> Optional[str]:
        """
        Safely fetch a variable key from a named section like 'AFC_extruder my_extruder'.

        :param section_prefix: Config section prefix, eg. AFC_extruder, AFC_hub, etc
        :param name: Config section name
        :param key: Config section variable name
        :param default: Default value to use if config variable is not found
        :return str: If variable found in config section, returns variable value
        """
        if not name:
            return default
        section = f"{section_prefix} {name}"
        try:
            cfg = self._config.getsection(section)
            return cfg.get(key, default)
        except Exception as e:
            self.logger.debug(f"Missing or invalid section '{section}': {e}")
            return default

    def _add_endstop(self, key: str, pin: str, suffix: str, fullname:str=None):
        """
        Helper to create/register an endstop and bind it to the drive stepper.

        :param key: Endstop key name to register for stepper
        :param pin: Pin to register as endstop for stepper
        :param suffix: String to append at end of endstop key
        :param fullname: Fullname to register endstop name as
        """
        if pin is None:
            self.logger.info(f"Pin for {key} is none for {self.name}")
            return
        # Normalize and create endstop
        try:
            self._ppins.allow_multi_use_pin(pin.strip("!^"))
            self._ppins.parse_pin(pin, True, True)
            mcu_endstop = self._ppins.setup_pin('endstop', pin)
        except Exception as e:
            self.logger.info(f"Error parsing pin for {key} is none for {self.name}")
            self.logger.info(f"{e}")
            return

        single_key_aliases = {'hub', 'tool_start', 'tool_end', 'buffer_advance', 'buffer_trailing'}
        if fullname:
            name = fullname
        elif key in single_key_aliases:
            name = '{}'.format(suffix)
        else:
            name = '{}_{}'.format(self.name, suffix)

        try:
            self._qes.register_endstop(mcu_endstop, name)
        except Exception:
            self.logger.info(f"Error when registering {name} as endstop for {self.lane}")
            pass
        try:
            mcu_endstop.add_stepper(self.extruder_stepper.stepper)
        except Exception:
            self.logger.info(f"Error when registering stepper {self.lane}")
            pass
        self.logger.debug(f"{self.name} adding endstop {key}:{name}:{pin}") # TODO:remove once fully tested on toolchanger
        self._endstops[key] = (mcu_endstop, name)

    def do_homing_move(self, movepos: int, speed: int, accel: int, endstop_spec:str,
                       triggered=True, check_trigger=True, assist_active=True) -> tuple[bool, float]:
        """
        Perform's a homing move using the specified endstop, speed/accel and distance.

        :param movepos: target absolute position along the filament axis
        :param speed: The speed of the movement.
        :param accel: The acceleration of the movement.
        :param endstop_spec: 'load' | raw pin string
        :param triggered/check_trigger: same semantics as manual_stepper
        :param assist_active: When true espoolers(if configured) activate during homing move
        :return tuple: When homing is success returns True and distance moved.
                       If homing failed returns False and move pos as distance moved.
        """
        reactor = self.printer.get_reactor()
        start_ts = reactor.monotonic()

        try:
            self.logger.debug(f"[AFC_stepper:{self.name}] Homing start endstop={endstop_spec} movepos={movepos} speed={speed} accel={accel}")
        except Exception:
            pass
        if accel is None: accel = self.short_moves_accel
        if speed is None: speed = self.short_moves_speed

        # Setting private variable so accel can be used in drip_move callback
        self._homing_accel = accel
        # Avoid zero-distance drip sequences which can confuse stepcompress
        if movepos == self._manual_axis_pos:
            self.logger.debug(f"AFC stepper '{self.name}' homing target equals current position; skipping move")
            return False, 0.
        pos = [movepos, 0., 0., 0.]

        phoming = self.printer.lookup_object('homing')
        # Try to get a real MCU-backed endstop
        try:
            endstop = self._resolve_endstop_pin(endstop_spec)
        except Exception as e_resolve:
            raise_string = f"ENDSTOP '{endstop_spec}' could not be resolved ({e_resolve})"
            self.logger.debug(raise_string)
            raise self.gcode.error(raise_string)

        # Try MCU-based homing first
        # Start/stop assist exactly with homing lifetime
        rewind = movepos < self._manual_axis_pos
        try:
            start_mcu_pos = self.extruder_stepper.stepper.get_mcu_position()
            with self.assist_move(speed, rewind, assist_active=assist_active):
                if self.afc.manual_home_has_probe_pos_param:
                    # Explicitly set probe_pos=False to use normal homing coordinates even when
                    # homing is initiated from a "probing" context. AFC lanes are not bed/Z
                    # probes, and we do not want Klipper's probe-position bookkeeping here.
                    # This argument is passed explicitly to remain compatible with newer
                    # Klipper versions that added the probe_pos parameter.
                    phoming.manual_home(toolhead=self, endstops=[endstop], pos=pos, speed=speed,
                                        probe_pos=False, triggered=triggered,
                                        check_triggered=check_trigger)
                else:
                    phoming.manual_home(toolhead=self, endstops=[endstop], pos=pos, speed=speed,
                                        triggered=triggered, check_triggered=check_trigger)
            end_ts = reactor.monotonic()
            try:
                # Log distance at trigger using homing trigger positions
                trig_mcu_pos = self.extruder_stepper.stepper.get_mcu_position()
                # Distance in steps (commanded frame) is trig - start of homing move
                # We don't have the start position directly; approximate via last commanded 0 with our local kinematics:
                # Use delta from current commanded to trigger pos as step delta
                step_dist = self.extruder_stepper.stepper.get_step_dist()
                # In Klipper, get_mcu_position() returns at current commanded pos; we can't read 'start' cleanly here.
                # However, the Homing manager's trigger_mcu_pos is absolute; compute delta from current mcu pos as a proxy for short homing moves.
                steps_moved = abs(trig_mcu_pos - start_mcu_pos)
                dist_mm = steps_moved * step_dist
            except Exception as e:
                self.logger.debug(f"Exception {e}")
                pass
            self.logger.debug(f"Homed lane {self.name}'to ENDSTOP={endstop_spec} trigger after "\
                              f"{dist_mm:.3f}mm (steps={steps_moved} dt={(end_ts-start_ts):.3f}s")

            return True, dist_mm
        except Exception as e:
            msg = str(e).lower()
            if "no trigger on" in msg:
                if not self.printer.is_shutdown():
                    try:
                        _, ffi_lib = chelper.get_ffi()
                        self.logger.debug(f"[{self.name}] Homing: {e}; continuing because homing_ignore_no_trigger is enabled {ffi_lib.itersolve_get_commanded_pos(self.stepper_kinematics)}")
                    except Exception:
                        pass
                    return False, movepos
                raise self.gcode.error(str(e))
            if ("communication timeout during homing" in msg) or ("endstop" in msg and "still triggered" in msg):
                self.logger.info(f"{e}")
                self.logger.debug(f"{traceback.format_exc()}")
                raise self.gcode.error(str(e))
            # Other MCU homing issues: surface message and re-raise as gcode error
            try:
                self.logger.debug(f"MCU homing issue ({e}); not proceeding with homing move")
            except Exception:
                pass
            raise self.gcode.error(str(e))

    # ------------------ Convenience homing helpers ------------------
    def home_to(self, endstop_spec:AFCHomingPoints, distance:Optional[float], speed: float, accel: float,
                triggered=True, check_trigger=True, assist_active=True) -> tuple[bool, float, bool]:
        """
        Home towards an endstop relative to current position by distance (mm).
        If 'distance' is None, callers should prefer the typed helpers which pick a
        sensible default direction and magnitude for the chosen endstop.

        :param endstop_spec: The target endstop to home to. Can be a raw pin identifier
                             or a logical name such as 'load', 'toolhead', etc.
        :param distance: Relative distance to move toward the endstop in millimeters.
                         Required unless using a helper method.
        :param speed: The speed of the movement.
        :param accel: The acceleration of the movement.
        :param speed_mode: Enum or configuration selecting the speed and acceleration
                           profile to use for this move. Defaults to `SpeedMode`.
        :param triggered: If True, movement stops when the endstop triggers. Defaults to True.
        :param check_trigger: If True, verify that the endstop is actually triggered at the
                              end of the move. Defaults to True.
        :return :return tuple: 3-tuple consisting of:
                        bool: indicated if homing was successful or not.
                        float: indicated movement wheather homing was successful or not. When not
                               successful distance will equal max move distance.
                        bool: indicate if an error happened while homing
        """
        error = False

        if distance is None:
            raise self.gcode.error("home_to requires an explicit distance; use home_to_hub/toolhead/buffer for sensible defaults")
        # Compute an absolute target along our 1D axis
        target = float(self._manual_axis_pos + float(distance))
        homed = False
        move_distance = 0
        try:
            homed, move_distance = self.do_homing_move(target, speed, accel,
                                                       endstop_spec,
                                                       triggered=triggered,
                                                       check_trigger=check_trigger,
                                                       assist_active=assist_active)
        except Exception:
            error = True
            msg = f"Error occurred when trying to home to {endstop_spec}, PAUSING!"
            self.afc.error.AFC_error(msg, self.afc.function.in_print())
            self.logger.debug(f"{traceback.format_exc()}")
        self.sync_print_time()
        return homed, move_distance, error

    cmd_AFC_STEPPER_HOME_help = "Command a manually home stepper to specified endstop"
    cmd_AFC_STEPPER_HOME_options = {"STEPPER": {"type":"string", "default":"lane1"},
                            "ENABLE": {"type": "int", "default": 1},
                            "SPEED": {"type": "float", "default": 100},
                            "ACCEL": {"type": "float", "default": 100},
                            "STOP_ON_ENDSTOP": {"type": "int", "default": 1},
                            "DIST": {"type": "float", "default": 100},
                            "ENDSTOP": {"type": "string", "default": "load"},
                            "SYNC": {"type": "int", "default": 1}}
    def cmd_AFC_STEPPER_HOME(self, gcmd):
        """
        Macro to manually home/move stepper to specified endstop, provided distance needs
        to be large so that filament can reach specified endstop. If distance is not large enough
        then stepped will only be moved by distance amount.

        Optional Values
        ----
        ENABLE - Use the ENABLE parameter to enable/disable the stepper.<br>
        SPEED - If SPEED is specified then the given value will be used instead of the default specified in the config file.<br>
        ACCEL - If ACCEL is specified then the given value will be used instead of the default specified in the config file.<br>
        STOP_ON_ENDSTOP - If STOP_ON_ENDSTOP=1 is specified then the move will end early should the endstop report as
        triggered (use STOP_ON_ENDSTOP=2 to complete the move without error even if the endstop does not trigger,
        use -1 or -2 to stop when the endstop reports not triggered).<br>
        DIST - Distance to move stepper<br>
        ENDSTOP - Endstop to try and home to, default names to use are: load, hub, tool_start, buffer_advance, buffer_trailing<br>
        SYNC - Only valid when not using STOP_ON_ENDSTOP, setting sync 0 will allow the stepper
        to be moved in paralled with other stepper movement.

        Usage
        ----
        AFC_STEPPER_HOME STEPPER=<lane_name> DIST=<move distance> SPEED=<speed> ACCEL=<acceleration> STOP_ON_ENDSTOP=<-2,-1,0,1,2> ENDSTOP=<endstop name> SYNC=<0/1>

        Example
        ----
        ```
        AFC_STEPPER_HOME STEPPER=lane1 ENABLE=0
        ```
        Example
        ----
        ```
        AFC_STEPPER_HOME STEPPER=lane1 DIST=10 SPEED=5
        ```
        Example
        ----
        ```
        AFC_STEPPER_HOME STEPPER=lane1 STOP_ON_ENDSTOP=1 DIST=200 SPEED=5 ENDSTOP=hub
        ```
        Example
        ----
        ```
        AFC_STEPPER_HOME STEPPER=lane1 STOP_ON_ENDSTOP=-1 DIST=-200 SPEED=5 ENDSTOP=hub
        ```
        """
        enable = gcmd.get_int('ENABLE', None)
        if enable is not None:
            self.do_enable(enable)
        speed = gcmd.get_float('SPEED', self.short_moves_speed or 5., above=0.)
        accel = gcmd.get_float('ACCEL', self.short_moves_accel or 0., minval=0.)
        homing_move = gcmd.get_int('STOP_ON_ENDSTOP', 0)
        if homing_move:
            movepos = gcmd.get_float('DIST')
            endstop_spec = gcmd.get('ENDSTOP', self.default_homing_endstop)
            self.do_homing_move(movepos, speed, accel,
                                endstop_spec,
                                triggered = homing_move > 0,
                                check_trigger = abs(homing_move) == 1)
        elif gcmd.get_float('DIST', None) is not None:
            movepos = gcmd.get_float('DIST')
            sync = gcmd.get_int('SYNC', 1)
            # Implement simple absolute-position move using our pipeline
            delta = movepos - self._manual_axis_pos
            if delta:
                self._move(delta, speed, accel, assist_active=True)
                self._manual_axis_pos = movepos
            if sync:
                self.sync_print_time()
        elif gcmd.get_int('SYNC', 0):
            self.sync_print_time()

def load_config_prefix(config):
    return AFCExtruderStepper(config)
