import ast
import logging
import re

from extras import gcode_macro

# Klipper uses TemplateWrapper, Kalico uses Template
_TemplateClass = getattr(gcode_macro, 'TemplateWrapper', None) or gcode_macro.Template


class KlippyTestingPlugin:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode_macro = self.printer.load_object(config, "gcode_macro")

        self.printer.register_event_handler(
            "gcode:command_error", self._command_error
        )
        self.printer.register_event_handler(
            "gcode:unknown_command", self._unknown_command
        )

        self.gcode.register_command("ASSERT", self.cmd_ASSERT)
        self.gcode.register_command("SIMULATE_CALLBACK", self.cmd_SIMULATE_CALLBACK)
        self.gcode.register_command("SET_ATTR", self.cmd_SET_ATTR)
        self.gcode.register_command("REACTOR_PAUSE", self.cmd_REACTOR_PAUSE)

    def _command_error(self):
        self.printer.request_exit("error_exit")
        self.printer.invoke_shutdown("Exception during testing")

    def _unknown_command(self, cmd):
        logging.error(f"Unknown command during test execution: {cmd}")
        self.printer.request_exit("error_exit")
        self.printer.invoke_shutdown(
            f"Unknown command during test execution: {cmd}"
        )

    # Match a leading TEST= token (case-insensitive key), capturing a quoted
    # or unquoted value while stopping before subsequent KEY= parameters.
    _test_re = re.compile(
        r'(?:^|\s)TEST='            # word boundary + key
        r'(?:"((?:[^"\\]|\\.)*)"|'  # quoted value  -> group 1
        r'(\S+))',                   # unquoted value -> group 2
        re.IGNORECASE,
    )

    def cmd_ASSERT(self, gcmd):
        "Evaluate an expression, raising an error if the return value is False"
        # Use raw command line to preserve case for Jinja expressions
        # (the normal params dict is uppercased by Klipper's gcode parser).
        raw = gcmd.get_raw_command_parameters()
        m = self._test_re.search(raw)
        if not m:
            raise gcmd.error("ASSERT: missing TEST parameter")
        expression = (m.group(1) if m.group(1) is not None else m.group(2)).strip()

        try:
            template = _TemplateClass(
                self.printer,
                self.gcode_macro.env,
                "ASSERT:runtime_expression",
                expression,
            )
        except:
            raise gcmd.error(f"ASSERT: Failed to parse '{expression}'")

        context = self.gcode_macro.create_template_context()
        statement = template.render(context)
        value = ast.literal_eval(statement) if statement else None

        if not value:
            raise gcmd.error(f"ASSERT: {expression} == {value}")

    def cmd_SIMULATE_CALLBACK(self, gcmd):
        "Invoke a printer object's button/sensor callback directly, faking a hardware pin edge"
        obj_name = gcmd.get('OBJECT')
        method_name = gcmd.get('METHOD')
        # STATE is omitted for callbacks that only take eventtime (e.g. a
        # reactor timer callback), rather than a button-style (eventtime, state).
        raw_state = gcmd.get('STATE', None)
        state = None if raw_state is None else bool(int(raw_state))
        delta = gcmd.get_float('DELTA', 0.0)
        # DEFER schedules the callback to fire later via the reactor instead of
        # immediately, so it can land in the middle of a later, still-running
        # gcode command (e.g. a homing move it's blocked on), mimicking a real
        # sensor firing mid-move rather than only between commands.
        defer = gcmd.get_float('DEFER', None)

        obj = self.printer.lookup_object(obj_name, None)
        if obj is None:
            raise gcmd.error(f"SIMULATE_CALLBACK: object '{obj_name}' not found")

        method = getattr(obj, method_name, None)
        if method is None:
            raise gcmd.error(
                f"SIMULATE_CALLBACK: object '{obj_name}' has no method '{method_name}'")

        reactor = self.printer.get_reactor()

        if defer is None:
            eventtime = reactor.monotonic() + delta
            call = (lambda: method(eventtime)) if state is None else (lambda: method(eventtime, state))

            # On Kalico, this command's own GCodeContextRoot otherwise holds
            # the gcode mutex through the call below; without_lock()
            # (Kalico-only) releases it first so a callback that does a
            # fresh-acquire run_script(..., "callback") doesn't deadlock
            # waiting on a mutex this command itself still holds.
            without_lock = getattr(gcmd, 'without_lock', None)
            if without_lock is not None:
                with without_lock():
                    call()
            else:
                call()
        else:
            if state is None:
                callback = lambda et: method(et)
            else:
                callback = lambda et: method(et, state)
            reactor.register_callback(callback, reactor.monotonic() + defer)

    def cmd_REACTOR_PAUSE(self, gcmd):
        "Pause the reactor for DELAY seconds, letting deferred callbacks/timers fire"
        delay = gcmd.get_float('DELAY', 0.01)

        # HOLD_MUTEX=1 keeps the gcode mutex held for the whole pause instead
        # of releasing it, modeling a real command that's physically blocked
        # mid-move (e.g. toolhead.wait_moves()), which also holds the mutex
        # the whole time it's paused.
        hold_mutex = gcmd.get_int('HOLD_MUTEX', 0) == 1
        reactor = self.printer.get_reactor()
        
        # On Kalico, this command's own GCodeContextRoot otherwise holds the
        # gcode mutex for the whole pause (ReactorMutex.test() stays True
        # while this greenlet is merely paused, not released), which masks
        # any deferred callback's mutex-ownership check. without_lock()
        # (Kalico-only) genuinely releases it so a deferred callback fires
        # the same way a real idle-reactor timer callback would.
        without_lock = getattr(gcmd, 'without_lock', None)
        if without_lock is not None and not hold_mutex:
            with without_lock():
                reactor.pause(reactor.monotonic() + delay)
        else:
            reactor.pause(reactor.monotonic() + delay)

    def cmd_SET_ATTR(self, gcmd):
        "Directly set an attribute on a printer object, for test-only setup with no gcode equiv"
        obj_name = gcmd.get('OBJECT')
        # ATTR may be a dotted path (e.g. "heater.target_temp") to reach an
        # attribute on an object the target object holds, not just a direct one.
        attr_path = gcmd.get('ATTR').split('.')
        raw_value = gcmd.get('VALUE')

        obj = self.printer.lookup_object(obj_name, None)
        if obj is None:
            raise gcmd.error(f"SET_ATTR: object '{obj_name}' not found")

        target = obj
        for attr_name in attr_path[:-1]:
            if not hasattr(target, attr_name):
                raise gcmd.error(f"SET_ATTR: '{obj_name}' has no attribute '{attr_name}'")
            target = getattr(target, attr_name)

        final_attr = attr_path[-1]
        if not hasattr(target, final_attr):
            raise gcmd.error(
                f"SET_ATTR: '{obj_name}' ({'.'.join(attr_path)}) has no attribute '{final_attr}'")

        try:
            value = ast.literal_eval(raw_value)
        except (ValueError, SyntaxError):
            raise gcmd.error(f"SET_ATTR: could not parse VALUE '{raw_value}'")

        setattr(target, final_attr, value)


def load_config(config):
    return KlippyTestingPlugin(config)
