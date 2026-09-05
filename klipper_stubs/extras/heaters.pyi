# Minimal stub for Klipper's klippy/extras/heaters.py -- only the members
# actually used by this project's extras/AFC_extruder.py and
# extras/AFC_form_tip.py.

class Heater:
    target_temp: float

class PrinterHeaters:
    def set_temperature(self, heater: Heater, temp: float, wait: bool = False) -> None: ...
