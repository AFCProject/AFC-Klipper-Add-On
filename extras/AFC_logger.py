# AFCProject Automated Filament Changer Software
#
# Copyright (C) 2024-2026 Armored Turtle
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import logging
import logging.handlers
import inspect
import re
import os
import atexit
import queuelogger

from types import CodeType
from queuelogger import QueueListener, QueueHandler
from pathlib import Path
from webhooks import GCodeHelper

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from klippy import Printer
    from gcode import GCodeDispatch
    from webhooks import WebHooks
    from extras.AFC import afc

class AFC_QueueListener(QueueListener):
    def __init__(self, filename: str) -> None:
        """
        Set up the background queue listener that writes AFC's log messages
        to disk, handling the differing constructor signatures between
        upstream Klipper's queuelogger and Kalico's.

        :param filename: Path to the log file to write to
        """

        # Checking to see if parent queuelogger has a FILE_SIZE define, this is mainly for
        # logger working correctly for Snapmaker U1, if this check does not happen then False will
        # be passed in and can cause the AFC log to grow without rolling.
        if hasattr(queuelogger, "FILE_SIZE"):
            super().__init__(filename)
        else:
            try:
                # Kalico needs an extra parameter passed in for log rollover
                super().__init__(filename, False)
            except:
                super().__init__(filename)

        if issubclass(QueueListener, logging.handlers.TimedRotatingFileHandler):
            logging.handlers.TimedRotatingFileHandler.__init__(
                self, filename, when="S", interval=60 * 60 * 24, backupCount=5
            )

        # Commenting out log rollover for now as it causes more of a hassle when getting users logs
        # and causes information to disappear if a user restart alot
        # logging.handlers.TimedRotatingFileHandler.doRollover(self)

class AFC_logger:
    PADDING_CHAR = ' '
    def __init__(self, printer: Printer, afc_obj: afc) -> None:
        """
        Set up the stdlib logger AFC writes to, wiring it to a background
        queue listener writing AFC.log when Klipper was started with a log
        file, or the root logger otherwise.

        :param printer: Klipper printer object
        :param afc_obj: afc instance this logger belongs to
        """
        self.reactor = printer.reactor
        self.afc     = afc_obj
        self.gcode: GCodeDispatch = printer.lookup_object('gcode')
        self.webhooks: WebHooks = printer.lookup_object('webhooks')

        self.afc_ql: Optional[AFC_QueueListener] = None
        log_path = printer.start_args.get('log_file', None)
        if log_path:
            dirname = Path(log_path).parent
            log_file = Path(dirname).joinpath("AFC.log")
            logger_name = os.path.splitext(os.path.basename(log_file))[0]

            self.logger = logging.getLogger(logger_name)

            if not any(isinstance(ql, QueueHandler) for ql in self.logger.handlers):
                self.afc_ql = AFC_QueueListener(str(log_file))
                self.afc_ql.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
                self.afc_queue_handler = QueueHandler(self.afc_ql.bg_queue)
                self.logger.addHandler(self.afc_queue_handler)
        else:
            self.logger = logging.getLogger()

        self.logger.propagate = False               # Stops logs from going into klippy.log
        self.logger.setLevel(logging.DEBUG)
        self.print_debug_console = False
        self.adaptive_padding = 0
        atexit.register(self.shutdown)

    def shutdown(self) -> None:
        """
        Stops the background queue listener, if one was started, so its
        thread doesn't outlive the process. Registered with atexit.
        """
        if self.afc_ql is not None:
            self.afc_ql.stop()

    def _add_monotonic(self, message: str) -> str:
        """
        Prepends the reactor's current monotonic time to a message.

        :param message: message to prepend the timestamp to
        :return type: message with the monotonic timestamp prepended
        """
        return f"{self.reactor.monotonic():10.3f} {message}"

    def _remove_tags(self, message: str) -> str:
        """
        Strips HTML-like tags (e.g. `<span class=...>`) from a message before
        it's written to the log file.

        :param message: message to strip tags from
        :return type: message with any tags removed
        """
        return re.sub("<.*?>", "", message)

    def _format(self, message: str, code: Optional[CodeType] = None) -> str:
        """
        Builds the final line written to the log file: an optional
        file/function/line prefix (padded to the widest one seen so far),
        the message with tags stripped, and a leading monotonic timestamp.

        :param message: message to format
        :param code: caller's code object, used to build the file/function/line prefix
        :return type: fully formatted log line
        """
        frame_data = ""
        if code is not None:
            file_name = os.path.basename(code.co_filename)
            if getattr(self.afc, "log_frame_data", True):
                prefix = f"[{file_name}:{code.co_name}():{code.co_firstlineno}] "
                frame_data = f"{prefix:<{self.adaptive_padding}}"
            self.adaptive_padding = max(len(frame_data), self.adaptive_padding)
        s = self._remove_tags(message.lstrip())
        return self._add_monotonic(f"{frame_data}- {s}")

    def send_callback(self, msg: str) -> None:
        """
        Forwards a message to any registered gcode output callbacks that
        belong to a GCodeHelper, so it shows up in the console/gui.

        :param msg: message to forward
        """
        for cb in self.gcode.output_callbacks:
            if isinstance(cb.__self__, GCodeHelper): cb(msg.lstrip())

    def raw(self, message: str) -> None:
        """
        Logs a message verbatim (prefixed with "RAW:") and forwards it to the
        console/gui, without any level-specific handling.

        :param message: message to log
        """
        frame = inspect.currentframe()
        code = frame.f_back.f_code if frame and frame.f_back else None
        for line in message.lstrip().rstrip().split("\n"):
            self.logger.info(self._format(f"{'RAW:':^7}{line}", code))
        self.send_callback(message)

    def info(self, message: str, console_only: bool=False) -> None:
        """
        Logs an info-level message and forwards it to the console/gui.

        :param message: message to log
        :param console_only: True to only forward to the console/gui, skipping AFC.log
        """
        frame = inspect.currentframe()
        code = frame.f_back.f_code if frame and frame.f_back else None
        if not console_only:
            for line in message.lstrip().split("\n"):
                self.logger.info(self._format(f"{'INFO:':^6}{line}", code))
        self.send_callback(message)

    def warning(self, message: str) -> None:
        """
        Logs a warning-level message, forwards it to the console/gui styled
        as a warning, and appends it to the afc message queue so it shows up
        in mainsail/fluidd.

        :param message: message to log
        """
        frame = inspect.currentframe()
        code = frame.f_back.f_code if frame and frame.f_back else None
        for line in message.lstrip().rstrip().split("\n"):
            self.logger.debug(self._format(f"{'WARN:':^6} {line}", code))

        self.send_callback(f"<span class=warning--text>WARNING: {message}</span>")

        self.afc.message_queue.append((message, "warning"))

    def debug(self, message: str, only_debug: bool=False, traceback: Optional[str]=None) -> None:
        """
        Logs a debug-level message, optionally forwarding it to the
        console/gui, and logs an accompanying traceback if one is given.

        :param message: message to log
        :param only_debug: True to skip forwarding to the console/gui even
                            when the console debug printout is enabled
        :param traceback: traceback text to log alongside the message
        """
        frame = inspect.currentframe()
        code = frame.f_back.f_code if frame and frame.f_back else None
        for line in message.lstrip().rstrip().split("\n"):
            self.logger.debug(self._format(f"{'DEBUG:':^6}{line}", code))

        if (self.print_debug_console
            and not only_debug):
            self.send_callback(message)

        if traceback is not None:
            for line in traceback.lstrip().rstrip().split("\n"):
                self.logger.debug( self._format(f"{'DEBUG:':^6}{line}",code))

    def error(self, message: str, traceback: Optional[str]=None, stack_name: str="") -> None:
        """
        Prints error to console and log, also adds error to message queue when is then displayed
        in mainsail/fluidd guis

        :param message: Error message to print to console and log
        :param traceback: Trackback to log to AFC.log file
        :param stack_name: name to attribute the error to, prefixed onto each logged line
        """
        stack_name = f"{stack_name}: " if stack_name else ""
        frame = inspect.currentframe()
        code = frame.f_back.f_code if frame and frame.f_back else None
        for line in message.lstrip().rstrip().split("\n"):
            self.logger.error( self._format(f"{'ERROR:':^6}{stack_name}{line}", code) )
        self.send_callback( f"!! {message}" )

        self.afc.message_queue.append((message.lstrip(), "error"))

        if traceback is not None:
            for line in traceback.lstrip().rstrip().split("\n"):
                self.logger.error( self._format(f"{'ERROR:':^6}{line}", code))


    def set_debug(self, debug: bool) -> None:
        """
        Toggles whether debug() forwards messages to the console/gui.

        :param debug: True to enable console debug printout
        """
        self.print_debug_console = debug
