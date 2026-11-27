# Minimal stub for Klipper's klippy/extras/homing.py -- only the members
# actually used by this project's extras/AFC.py and extras/AFC_stepper.py.
# manual_home's real signature differs across Klipper/Kalico forks (e.g.
# `probe_pos`, a required `gcmd`), which is why callers inspect it via
# inspect.signature rather than calling it with a fixed set of arguments.
from typing import Any

class PrinterHoming:
    def manual_home(self, *args: Any, **kwargs: Any) -> Any: ...
