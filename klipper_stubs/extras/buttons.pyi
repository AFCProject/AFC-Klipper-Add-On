# Minimal stub for Klipper's klippy/extras/buttons.py -- only the members
# actually used by this project's extras/AFC_button.py.
from typing import Callable, List

class PrinterButtons:
    def register_buttons(self, pins: List[str], callback: Callable[..., None]) -> None: ...
    def register_adc_button(self, pin: str, min_val: float, max_val: float,
                             pullup: float, callback: Callable[..., None]) -> None: ...
    def register_adc_button_with_probe(self, pin: str, min_val: float, max_val: float,
                                        pullup: float, callback: Callable[..., None]) -> None: ...
