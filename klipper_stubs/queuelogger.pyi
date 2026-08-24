# Minimal stub for Klipper's klippy/queuelogger.py -- only the members
# actually used by this project's extras/AFC_logger.py.
import logging
import queue
from typing import Any

FILE_SIZE: int

class QueueHandler(logging.Handler):
    def __init__(self, queue: queue.Queue) -> None: ...

class QueueListener(logging.handlers.TimedRotatingFileHandler):
    bg_queue: queue.Queue

    def __init__(self, filename: str) -> None: ...
    def stop(self) -> None: ...
    def set_rollover_info(self, name: str, info: Any) -> None: ...
    def clear_rollover_info(self) -> None: ...
