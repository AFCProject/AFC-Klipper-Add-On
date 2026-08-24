# Minimal stub for Klipper's klippy/extras/query_endstops.py -- only the
# member actually used by this project's extras/*.py.
from mcu import MCU_endstop

class QueryEndstops:
    def register_endstop(self, mcu_endstop: MCU_endstop, name: str) -> None: ...
