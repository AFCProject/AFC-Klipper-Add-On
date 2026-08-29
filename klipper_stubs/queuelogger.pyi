# Minimal stub for Klipper's klippy/queuelogger.py -- only the members
# actually used by this project's extras/AFC_logger.py.
import logging
import logging.handlers
import queue
from typing import Any

FILE_SIZE: int

class QueueHandler(logging.Handler):
    def __init__(self, queue: queue.Queue) -> None: ...

class QueueListener(logging.handlers.TimedRotatingFileHandler):
    bg_queue: queue.Queue

    # Klipper's signature only takes filename; Kalico's requires the extra
    # rotate_log_at_restart bool (this project supports both -- see
    # extras/AFC_logger.py's fallback call).
    def __init__(self, filename: str, rotate_log_at_restart: bool = ...) -> None: ...
    def stop(self) -> None: ...
    def set_rollover_info(self, name: str, info: Any) -> None: ...
    def clear_rollover_info(self) -> None: ...
