# Minimal stub for Klipper's klippy/extras/stepper_enable.py -- only the
# members actually used by this project's extras/AFC_vivid.py.
from typing import TypedDict

class _StepperEnableStatus(TypedDict):
    steppers: dict

class PrinterStepperEnable:
    def get_status(self, eventtime: float) -> _StepperEnableStatus: ...
