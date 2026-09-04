# Minimal stub for Klipper's klippy/extras/filament_switch_sensor.py --
# only the members actually used by this project's extras/AFC_utils.py.
from typing import Any, Callable, Optional

class RunoutHelper:
    sensor_enabled: bool
    runout_pause: bool
    runout_gcode: Optional[Any]
    insert_gcode: Optional[Any]
    filament_present: bool

    def note_filament_present(self, eventtime: float, is_filament_present: bool) -> None: ...
    def get_status(self, eventtime: float) -> dict: ...
    # Reassigned by extras/AFC_utils.py.add_filament_switch to override the
    # normal runout behavior with an AFC-supplied callback.
    _runout_event_handler: Callable[..., None]

class SwitchSensor:
    runout_helper: RunoutHelper

    def get_status(self, eventtime: float) -> dict: ...
