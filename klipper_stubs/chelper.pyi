# Minimal stub for Klipper's klippy/chelper.py -- only the member actually
# used by this project's extras/*.py. The FFI objects it returns are left
# as Any; they're C-extension handles this project only ever passes through
# to trapq/stepper alloc calls, not something worth typing precisely.
from typing import Any, Tuple

def get_ffi() -> Tuple[Any, Any]: ...
