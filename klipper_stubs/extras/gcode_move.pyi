# Minimal stub for Klipper's klippy/extras/gcode_move.py -- only the
# members actually used by this project's extras/AFC.py and
# extras/AFC_functions.py.
from typing import Callable, List, Optional

class GCodeMove:
    absolute_coord: bool
    absolute_extrude: bool
    # Only present on newer (post-PR-7349) Klipper; accessed via hasattr()
    # guards in extras/AFC_functions.py, so declared optional here too.
    allow_absolute_extrude: bool
    base_position: List[float]
    last_position: List[float]
    homing_position: List[float]
    speed: float
    speed_factor: float
    extrude_factor: float
    move_with_transform: Callable[..., None] # Not setting as optional since this will be set during ready phase
