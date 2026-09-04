# Minimal stub for Klipper's klippy/extras/idle_timeout.py -- only the
# members actually used by this project's extras/AFC_error.py and
# extras/AFC_utils.py.
from typing import TypedDict

class _IdleTimeoutStatus(TypedDict):
    state: str
    printing_time: float
    idle_timeout: float

class IdleTimeout:
    idle_timeout: float

    def get_status(self, eventtime: float) -> _IdleTimeoutStatus: ...
