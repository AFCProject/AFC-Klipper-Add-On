# Minimal stub for Klipper's klippy/extras/gcode_button.py -- only the
# members actually used by this project's extras/AFC_button.py.
from typing import Dict, Optional

from configfile import ConfigWrapper
from gcode import GCodeDispatch
from klippy import Printer

class GCodeButton:
    printer: Printer
    gcode: GCodeDispatch
    name: str
    pin: str
    last_state: int

    def __init__(self, config: ConfigWrapper) -> None: ...
    def button_callback(self, eventtime: float, state: bool) -> None: ...
    def get_status(self, eventtime: Optional[float] = None) -> Dict[str, str]: ...
