import ast
import logging

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

    def _command_error(self):
        self.printer.request_exit("error_exit")
        self.printer.invoke_shutdown("Exception during testing")

    def _unknown_command(self, cmd):
        logging.error(f"Unknown command during test execution: {cmd}")
        self.printer.request_exit("error_exit")
        self.printer.invoke_shutdown(
            f"Unknown command during test execution: {cmd}"
        )

    def cmd_ASSERT(self, gcmd):
        "Evaluate an expression, raising an error if the return value is False"
        # Use raw command line to preserve case for Jinja expressions
        # (the normal params dict is uppercased by Klipper's gcode parser).
        raw = gcmd.get_raw_command_parameters()
        # Extract value after TEST=
        idx = raw.upper().find("TEST=")
        if idx < 0:
            raise gcmd.error("ASSERT: missing TEST parameter")
        expression = raw[idx + 5:].strip()
        # Strip surrounding quotes if present
        if len(expression) >= 2 and expression[0] == '"' and expression[-1] == '"':
            expression = expression[1:-1]

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


def load_config(config):
    return KlippyTestingPlugin(config)
