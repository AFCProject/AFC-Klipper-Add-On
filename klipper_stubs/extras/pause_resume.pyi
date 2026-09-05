# Minimal stub for Klipper's klippy/extras/pause_resume.py -- only the
# members actually used by this project's extras/AFC_error.py and
# extras/AFC_utils.py.
from gcode import GCodeCommand

class PauseResume:
    is_paused: bool

    def send_pause_command(self) -> None: ...
    def cmd_PAUSE(self, gcmd: GCodeCommand) -> None: ...
