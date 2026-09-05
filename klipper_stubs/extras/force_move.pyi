# Minimal stub for Klipper's klippy/extras/force_move.py -- only the
# members actually used by this project's extras/AFC_stepper.py and
# extras/AFC_extruder.py.
from typing import Tuple

def calc_move_time(dist: float, speed: float, accel: float) -> Tuple[float, float, float, float]: ...
