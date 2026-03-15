"""
Wraps klippy.py to prevent it hanging on exit when -l (file logging) is active.

klipper's queuelogger.QueueListener starts a non-daemon background thread.
Non-daemon threads block Python's threading shutdown, which prevents atexit
handlers (including AFC_logger.shutdown) from running, causing a deadlock.
Patching threading.Thread.__init__ before any threads are created resolves it.
"""

import sys
import threading
import runpy
from pathlib import Path

_orig_thread_init = threading.Thread.__init__


def _patched_thread_init(self, *args, **kwargs):
    _orig_thread_init(self, *args, **kwargs)
    self.daemon = True


threading.Thread.__init__ = _patched_thread_init

sys.argv = sys.argv[1:]
# Mirror what Python does when running a script directly: add the script's
# directory to sys.path[0] so klippy's relative imports (util, reactor, etc.)
# resolve correctly.
sys.path.insert(0, str(Path(sys.argv[0]).parent))
runpy.run_path(sys.argv[0], run_name="__main__")
