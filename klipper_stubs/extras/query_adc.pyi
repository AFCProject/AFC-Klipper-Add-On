# Minimal stub for Klipper's klippy/extras/query_adc.py -- only the
# members actually used by this project's extras/AFC_button.py.
from typing import Dict

from mcu import MCU_adc

class QueryADC:
    adc: Dict[str, MCU_adc]
