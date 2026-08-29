# Minimal stub for Klipper's klippy/kinematics/extruder.py -- only the
# members actually used by this project's extras/*.py.
from typing import Any, Dict, Optional

from configfile import ConfigWrapper
from stepper import MCU_stepper

class ExtruderStepper:
    stepper: MCU_stepper

    def __init__(self, config: ConfigWrapper) -> None: ...
    def sync_to_extruder(self, extruder_name: Optional[str]) -> None: ...

class PrinterExtruder:
    extruder_stepper: Optional[ExtruderStepper]

    def get_status(self, eventtime: float) -> Dict[str, Any]: ...
    def get_name(self) -> str: ...
    def get_heater(self) -> Any: ...
    def get_trapq(self) -> Any: ...
