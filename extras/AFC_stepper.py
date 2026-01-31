# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import chelper
import traceback

from kinematics import extruder
from configfile import error
from extras.force_move import calc_move_time

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCLane
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

LARGE_TIME_OFFSET = 99999.9

class AFCExtruderStepper(AFCLane):
    def __init__(self, config):
        super().__init__(config)

        self.extruder_stepper   = extruder.ExtruderStepper(config)
        self.stepper_enable     = self.printer.lookup_object('stepper_enable')

        # Homing-related configuration (optional)
        # Choose which sensor to use by default when doing STOP_ON_ENDSTOP moves
        # Allowed values: 'load', or a raw MCU pin string (e.g. '^AFC:TRG1')
        self.default_homing_endstop = config.get('homing_endstop', 'load')
        # Optional overrides similar to manual_stepper
        self.homing_velocity = config.getfloat('homing_velocity', None)
        self.homing_accel = config.getfloat('homing_accel', None)
        self._manual_axis_pos = 0.0

        # Optional explicit hub endstop pin override in stepper section
        self.hub_endstop = config.get('hub_endstop', None)

        # Optional: allow declaring extra raw endstop pins that should be
        # created at config time (comma-separated list)
        self._extra_homing_pins = set(config.getlist('homing_endstop_pins', default="", sep=','))
        # Pre-create MCU endstops now so MCU config callbacks run before ready
        self._endstops = {}
        self._init_endstops()

        # Register AFC_HOME mux command for this lane name
        stepper_name = config.get_name().split()[-1]
        self.gcode.register_mux_command('AFC_HOME', 'STEPPER',
                                        stepper_name, self.cmd_AFC_HOME,
                                        desc=self.cmd_AFC_HOME_help)

        # Check for Klipper new motion queuing update
        try:
            self.motion_queuing = self.printer.load_object(config, "motion_queuing")
        except Exception:
            self.motion_queuing = None

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
        # Track last homing event for synchronization with lane logic
        self._last_home_endstop = None
        self._last_home_time = 0.0

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

    def _move(self, distance, speed, accel, assist_active=False):
        """
        Helper function to move the specified lane a given distance with specified speed and acceleration.
        This function calculates the movement parameters and commands the stepper motor
        to move the lane accordingly.
        Parameters:
        distance (float): The distance to move.
        speed (float): The speed of the movement.
        accel (float): The acceleration of the movement.
        """


        with self.assist_move(speed, distance < 0, assist_active):
            # Code based off force_move.py manual_move function
            toolhead = self.printer.lookup_object('toolhead')
            toolhead.flush_step_generation()
            prev_sk     = self.extruder_stepper.stepper.set_stepper_kinematics(self.stepper_kinematics)
            prev_trapq  = self.extruder_stepper.stepper.set_trapq(self.trapq)
            self.extruder_stepper.stepper.set_position((0., 0., 0.))
            axis_r, accel_t, cruise_t, cruise_v = calc_move_time(distance, speed, accel)
            print_time = toolhead.get_last_move_time()
            self.trapq_append(self.trapq, print_time, accel_t, cruise_t, accel_t,
                              0., 0., 0., axis_r, 0., 0., 0., cruise_v, accel)
            print_time = print_time + accel_t + cruise_t + accel_t

            if self.motion_queuing is None:
                self.extruder_stepper.stepper.generate_steps(print_time)
                self.trapq_finalize_moves(self.trapq, print_time + LARGE_TIME_OFFSET,
                                        print_time + LARGE_TIME_OFFSET)
                toolhead.note_mcu_movequeue_activity(print_time)
            else:
                self.motion_queuing.note_mcu_movequeue_activity(print_time)

            toolhead.dwell(accel_t + cruise_t + accel_t)
            toolhead.flush_step_generation()
            self.extruder_stepper.stepper.set_trapq(prev_trapq)
            self.extruder_stepper.stepper.set_stepper_kinematics(prev_sk)
            if self.motion_queuing is not None:
                self.motion_queuing.wipe_trapq(self.trapq)
            toolhead.wait_moves()

    def move(self, distance, speed, accel, assist_active=False):
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
        # Do not dwell the main toolhead waiting for our private time cursor.
        # If our next_cmd_time is ahead of the toolhead's time, trim it instead
        # of blocking. This prevents a post-trigger hesitation when homing
        # stops early before the commanded full distance.
        try:
            toolhead = self.printer.lookup_object('toolhead')
            th_time = toolhead.get_last_move_time()
            if self.next_cmd_time > th_time:
                self.next_cmd_time = th_time
        except Exception:
            # Fallback to previous behavior if toolhead is unavailable
            self.sync_print_time()

    def get_position(self):
        # 1D position along filament path
        return [self._manual_axis_pos, 0., 0., 0.]

    def set_position(self, newpos, homing_axes=""):
        # Set our internal commanded position (1D)
        try:
            self._manual_axis_pos = float(newpos[0])
        except Exception:
            self._manual_axis_pos = 0.0

    def get_last_move_time(self):
        self.sync_print_time()
        return self.next_cmd_time

    def dwell(self, delay):
        # Advance internal time to satisfy homing scheduler
        self.next_cmd_time += max(0., delay)

    def drip_move(self, newpos, speed, drip_completion):
        # Queue the homing move quickly; do not block or dwell here. Homing
        # controller will wait for the endstop and handle final positioning.
        target = float(newpos[0])
        delta = target - self._manual_axis_pos
        accel = self.homing_accel if self.homing_accel is not None else (self.short_moves_accel or 0.)
        v = self.homing_velocity if self.homing_velocity is not None else speed

        toolhead = self.printer.lookup_object('toolhead')

        toolhead.flush_step_generation()
        self.sync_print_time()
        prev_sk = self.extruder_stepper.stepper.set_stepper_kinematics(self.stepper_kinematics)
        prev_trapq = self.extruder_stepper.stepper.set_trapq(self.trapq)
        try:
            self.extruder_stepper.stepper.set_position((0., 0., 0.))
            axis_r, accel_t, cruise_t, cruise_v = calc_move_time(delta, v, accel)
            self.trapq_append(self.trapq, self.next_cmd_time, accel_t, cruise_t, accel_t,
                              0., 0., 0., axis_r, 0., 0., 0., cruise_v, accel)
            end_time = self.next_cmd_time + accel_t + cruise_t + accel_t
            self.extruder_stepper.stepper.generate_steps(end_time)
            # Finalize near end_time; no far-future finalize so host can trim
            self.trapq_finalize_moves(self.trapq, end_time + 0.001, end_time + 0.001)
            self.next_cmd_time = end_time
            # toolhead.note_mcu_movequeue_activity(end_time)
        finally:
            self.extruder_stepper.stepper.set_trapq(prev_trapq)
            self.extruder_stepper.stepper.set_stepper_kinematics(prev_sk)

    def get_kinematics(self):
        # Homing expects an object with kinematics-like API; we self-provide
        return self

    def get_steppers(self):
        # Return underlying single stepper for homing code
        return [self.extruder_stepper.stepper]

    def calc_position(self, stepper_positions):
        # Map stepper positions into kinematic position; unused for filament homing
        return [stepper_positions[self.extruder_stepper.stepper.get_name()], 0., 0.]

    # ------------------ Endstop plumbing ------------------
    def _resolve_endstop_pin(self, endstop_spec):
        """
        Resolve an endstop spec into an MCU endstop and a human name.
        - endstop_spec can be 'load', 'hub', 'tool'|'tool_start'|'tool_end',
          'buffer'|'buffer_advance'|'buffer_trailing', or a raw MCU pin string.
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
            raise error("Unknown ENDSTOP '{}' for lane '{}'. Declare it in [AFC_stepper {}] using 'homing_endstop_pins: {}' or use ENDSTOP=load/hub/tool/buffer".format(
                endstop_spec, self.name, self.name, endstop_spec))
        return (mcu_endstop, name)

    def _init_endstops(self):
        """Create and register endstops at config time so MCU callbacks run."""
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
            if not hub_name or hub_name == 'direct':
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

        self._add_endstop('hub', hub_pin, 'hub')
        if tool_start_pin != 'buffer':
            self._add_endstop('tool_start', tool_start_pin, 'tool_start')
        else:
            self._add_endstop('tool_start', buffer_adv_pin, 'tool_start')
        self._add_endstop('tool_end', tool_end_pin, 'tool_end')
        self._add_endstop('buffer_advance', buffer_adv_pin, 'buffer_adv')
        self._add_endstop('buffer_trailing', buffer_trail_pin, 'buffer_trailing')

    def _inherit_from_unit(self, target_key):
        """Return value (e.g., 'hub', 'extruder', etc.) from the unit section matching this object's unit."""
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
                        logging.debug(f"Inherited '{target_key}'='{value}' from section '{sec}' for unit '{unit}'")
                    return value
        except Exception as e:
            logging.debug(f"_inherit_from_unit({target_key}) failed: {e}", exc_info=True)

        return None

    def _get_section_value(self, section_prefix, name, key, default=None):
        """Safely fetch a key from a named section like 'AFC_extruder my_extruder'."""
        if not name:
            return default
        section = f"{section_prefix} {name}"
        try:
            cfg = self._config.getsection(section)
            return cfg.get(key, default)
        except Exception as e:
            logging.debug(f"Missing or invalid section '{section}': {e}")
            return default

    def _add_endstop(self, key, pin, suffix):
        """Helper to create/register an endstop and bind it to the drive stepper."""
        if pin is None:
            return
        # Normalize and create endstop
        try:
            self._ppins.parse_pin(pin, True, True)
            mcu_endstop = self._ppins.setup_pin('endstop', pin)
        except Exception:
            return
        
        single_key_aliases = {'hub', 'tool_start', 'tool_end', 'buffer_advance', 'buffer_trailing'}
        if key in single_key_aliases:
            name = '{}'.format(suffix)
        else:
            name = '{}_{}'.format(self.name, suffix)

        try:
            self._qes.register_endstop(mcu_endstop, name)
        except Exception:
            pass
        try:
            mcu_endstop.add_stepper(self.extruder_stepper.stepper)
        except Exception:
            pass
        self._endstops[key] = (mcu_endstop, name)

    def handle_unit_connect(self, unit_obj):
        """Extend AFCLane hook. Hub endstop is already registered at config time."""
        super().handle_unit_connect(unit_obj)

    def do_homing_move(self, movepos, speed, accel, endstop_spec, triggered=True, check_trigger=True):
        """Perform a homing-like move using the specified endstop.
        movepos: target absolute position along the filament axis
        speed/accel: motion params (fallbacks applied if None)
        endstop_spec: 'load' | raw pin string
        triggered/check_trigger: same semantics as manual_stepper
        """
        reactor = self.printer.get_reactor()
        start_ts = reactor.monotonic()
        try:
            self.logger.debug(f"[AFC_stepper:{self.name}] Homing start endstop={endstop_spec} movepos={movepos} speed={speed} accel={accel}")
        except Exception:
            pass
        if accel is None:
            accel = self.homing_accel if self.homing_accel is not None else (self.short_moves_accel or 50.)
        if speed is None:
            speed = self.homing_velocity if self.homing_velocity is not None else (self.short_moves_speed or 50.)
        # Avoid zero-distance drip sequences which can confuse stepcompress
        if movepos == self._manual_axis_pos:
            self.logger.debug(f"AFC stepper '{self.name}' homing target equals current position; skipping move")
            return
        pos = [movepos, 0., 0., 0.]

        phoming = self.printer.lookup_object('homing')

        # Try to get a real MCU-backed endstop
        try:
            endstop = self._resolve_endstop_pin(endstop_spec)
        except Exception as e_resolve:
            # TODO Fallback
            self._info(f"ENDSTOP '{endstop_spec}' could not be resolved ({e_resolve}); using software homing.")
            raise self.gcode.error(f"ENDSTOP '{endstop_spec}' could not be resolved ({e_resolve})")

        # Try MCU-based homing first
        # Start/stop assist exactly with homing lifetime
        rewind = movepos < self._manual_axis_pos
        try:
            with self.assist_move(speed, rewind, assist_active=True):
                phoming.manual_home(self, [endstop], pos, speed, triggered, check_trigger)
            end_ts = reactor.monotonic()
            self._last_home_endstop = endstop_spec
            self._last_home_time = end_ts
            # Log distance at trigger using homing trigger positions
            try:
                homing_mgr = self.printer.lookup_object('homing')
                stepper_name = self.extruder_stepper.stepper.get_name()
                trig_mcu_pos = homing_mgr.get_trigger_position(stepper_name)
                start_mcu_pos = self.extruder_stepper.stepper.get_mcu_position()
                # Distance in steps (commanded frame) is trig - start of homing move
                # We don't have the start position directly; approximate via last commanded 0 with our local kinematics:
                # Use delta from current commanded to trigger pos as step delta
                step_dist = self.extruder_stepper.stepper.get_step_dist()
                # In Klipper, get_mcu_position() returns at current commanded pos; we can't read 'start' cleanly here.
                # However, the Homing manager's trigger_mcu_pos is absolute; compute delta from current mcu pos as a proxy for short homing moves.
                steps_moved = trig_mcu_pos - start_mcu_pos
                dist_mm = steps_moved * step_dist
                self.logger.debug(f"AFC lane '{self.name}': endstop '{endstop_spec}' trigger after {dist_mm:.3f} mm (steps={steps_moved})")
            except Exception:
                pass
            self.logger.debug(f"Homed lane '{self.name}' to ENDSTOP='{endstop_spec}' at position {self._manual_axis_pos:.2f}mm (dt={(end_ts-start_ts):.3f}s)")
            return True
        except Exception as e:
            # TODO Fallback
            msg = str(e).lower()
            if "no trigger on" in msg:
                if not self.printer.is_shutdown():
                    try:
                        self.logger.debug(f"[{self.name}] Homing: {e}; continuing because homing_ignore_no_trigger is enabled")
                    except Exception:
                        pass
                    return False
                raise self.gcode.error(str(e))
            if ("communication timeout during homing" in msg) or ("endstop" in msg and "still triggered" in msg):
                raise self.gcode.error(str(e))
            # Other MCU homing issues: surface message and re-raise as gcode error
            try:
                self.logger.debug(f"MCU homing issue ({e}); not proceeding with homing move")
            except Exception:
                pass
            raise self.gcode.error(str(e))

    # ------------------ Convenience homing helpers ------------------
    def home_to(self, endstop_spec, distance=None, speed_mode=SpeedMode, triggered=True, check_trigger=True):
        """
        Home towards an endstop relative to current position by distance (mm).
        If 'distance' is None, callers should prefer the typed helpers which pick a
        sensible default direction and magnitude for the chosen endstop.

        Parameters:
        endstop_spec (str): The target endstop to home to. Can be a raw pin identifier or a logical
            name such as 'load', 'toolhead', etc.
        distance (float, optional): Relative distance to move toward the endstop in millimeters.
            Required unless using a helper method.
        speed_mode (SpeedMode, optional): Enum or configuration selecting the speed and acceleration
            profile to use for this move. Defaults to `SpeedMode`.
        triggered (bool, optional): If True, movement stops when the endstop triggers. Defaults to True.
        check_trigger (bool, optional): If True, verify that the endstop is actually triggered at the
            end of the move. Defaults to True.
        """
        speed, accel = self.get_speed_accel(speed_mode)

        if distance is None:
            raise self.gcode.error("home_to requires an explicit distance; use home_to_hub/toolhead/buffer for sensible defaults")
        # Compute an absolute target along our 1D axis
        target = float(self._manual_axis_pos + float(distance))
        self.logger.debug(f"Homing lane '{self.name}' to ENDSTOP='{endstop_spec}' at position {distance:.2f}mm")
        homed = self.do_homing_move(target, speed, accel, endstop_spec,
                            triggered=triggered, check_trigger=check_trigger)
        self.sync_print_time()
        return homed

    # ------------------ Command shim (AFC_HOME) ------------------
    cmd_AFC_HOME_help = "Command a manually controlled stepper (AFC shim)"
    def cmd_AFC_HOME(self, gcmd):
        """
        Shim to mirror Klipper's AFC_HOME for AFC lanes, including STOP_ON_ENDSTOP.
        Usage examples:
          AFC_HOME STEPPER={name} ENABLE=1
          AFC_HOME STEPPER={name} MOVE=10 SPEED=5
          AFC_HOME STEPPER={name} STOP_ON_ENDSTOP=1 MOVE=200 SPEED=5 [ENDSTOP=load|^AFC:TRG1]
        """
        enable = gcmd.get_int('ENABLE', None)
        if enable is not None:
            self.do_enable(enable)
        setpos = gcmd.get_float('SET_POSITION', None)
        if setpos is not None:
            self.set_position([setpos, 0., 0., 0.])
        speed = gcmd.get_float('SPEED', self.short_moves_speed or 5., above=0.)
        accel = gcmd.get_float('ACCEL', self.short_moves_accel or 0., minval=0.)
        homing_move = gcmd.get_int('STOP_ON_ENDSTOP', 0)
        if homing_move:
            movepos = gcmd.get_float('MOVE')
            endstop_spec = gcmd.get('ENDSTOP', self.default_homing_endstop)
            self.do_homing_move(movepos, speed, accel,
                                endstop_spec,
                                triggered = homing_move > 0,
                                check_trigger = abs(homing_move) == 1)
        elif gcmd.get_float('MOVE', None) is not None:
            movepos = gcmd.get_float('MOVE')
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
