# Armored Turtle Automated Filament Control
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import logging
import inspect
import re
import os
import atexit
from types import CodeType
from queuelogger import QueueListener, QueueHandler
from pathlib import Path
from webhooks import GCodeHelper

class AFC_QueueListener(QueueListener):
    def __init__(self, filename):
        try:
            # Kalico needs an extra parameter passed in for log rollover
            super().__init__(filename, False)
        except:
            super().__init__(filename)

        logging.handlers.TimedRotatingFileHandler.__init__(
            self, filename, when="S", interval=60 * 60 * 24, backupCount=5
        )

        # Commenting out log rollover for now as it causes more of a hassle when getting users logs
        # and causes information to disappear if a user restart alot
        # logging.handlers.TimedRotatingFileHandler.doRollover(self)

class AFC_logger:
    PADDING_CHAR = ' '
    def __init__(self, printer, afc_obj):
        self.reactor = printer.reactor
        self.afc     = afc_obj
        self.gcode   = printer.lookup_object('gcode')
        self.webhooks = printer.lookup_object('webhooks')

        log_path = printer.start_args['log_file']
        dirname = Path(log_path).parent
        log_file = Path(dirname).joinpath("AFC.log")
        logger_name = os.path.splitext(os.path.basename(log_file))[0]

        self.afc_ql = None
        self.logger = logging.getLogger(logger_name)
        if not any(isinstance(ql, QueueHandler) for ql in self.logger.handlers):
            self.afc_ql = AFC_QueueListener(log_file)
            self.afc_ql.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
            self.afc_queue_handler = QueueHandler(self.afc_ql.bg_queue)
            self.logger.addHandler(self.afc_queue_handler)

        self.logger.propagate = False               # Stops logs from going into klippy.log
        self.logger.setLevel(logging.DEBUG)
        self.print_debug_console = False
        self.adaptive_padding = 0
        atexit.register(self.shutdown)

    def shutdown(self):
        if self.afc_ql is not None:
            self.afc_ql.stop()

    def _add_monotonic(self, message):
        return "{:10.3f} {}".format(self.reactor.monotonic(), message)

    def _remove_tags(self, message):
        return re.sub("<.*?>", "", message)

    def _format(self, message, code: CodeType = None):
        frame_data = ""
        if code is not None:
            file_name = os.path.basename(code.co_filename)
            if getattr(self.afc, "log_frame_data", True):
                frame_data = "{:<{pad}}".format(f"[{file_name}:{code.co_name}():{code.co_firstlineno}] ",
                                                pad=self.adaptive_padding)
            self.adaptive_padding = max(len(frame_data), self.adaptive_padding)
        s = self._remove_tags(message.lstrip())
        return self._add_monotonic(f"{frame_data}- {s}")

    def send_callback(self, msg):
        for cb in self.gcode.output_callbacks:
            if isinstance(cb.__self__, GCodeHelper): cb(msg.lstrip())

    def raw(self, message):
        code = inspect.currentframe().f_back.f_code
        for line in message.lstrip().rstrip().split("\n"):
            self.logger.info(self._format(f"{'RAW:':^7}{line}", code))
        self.send_callback(message)

    def info(self, message, console_only=False):
        code = inspect.currentframe().f_back.f_code
        if not console_only:
            for line in message.lstrip().split("\n"):
                self.logger.info(self._format(f"{'INFO:':^6}{line}", code))
        self.send_callback(message)

    def warning(self, message):
        code = inspect.currentframe().f_back.f_code
        for line in message.lstrip().rstrip().split("\n"):
            self.logger.debug(self._format(f"{'WARN:':^6} {line}", code))

        self.send_callback(f"<span class=warning--text>WARNING: {message}</span>")

        self.afc.message_queue.append((message, "warning"))

    def debug(self, message, only_debug=False, traceback=None):
        code = inspect.currentframe().f_back.f_code
        for line in message.lstrip().rstrip().split("\n"):
            self.logger.debug(self._format(f"{'DEBUG:':^6}{line}", code))

        if self.print_debug_console and not only_debug:
            self.send_callback(message)

        if traceback is not None:
            for line in traceback.lstrip().rstrip().split("\n"):
                self.logger.debug( self._format(f"{'DEBUG:':^6}{line}",code))

    def error(self, message, traceback=None, stack_name=""):
        """
        Prints error to console and log, also adds error to message queue when is then displayed
        in mainsail/fluidd guis

        :param message: Error message to print to console and log
        :param traceback: Trackback to log to AFC.log file
        """
        stack_name = f"{stack_name}: " if stack_name else ""
        code = inspect.currentframe().f_back.f_code
        for line in message.lstrip().rstrip().split("\n"):
            self.logger.error( self._format(f"{'ERROR:':^6}{stack_name}{line}", code) )
        self.send_callback( "!! {}".format(message) )

        self.afc.message_queue.append((message, "error"))

        if traceback is not None:
            for line in traceback.lstrip().rstrip().split("\n"):
                self.logger.error( self._format(f"{'ERROR:':^6}{line}", code))


    def set_debug(self, debug ):
        self.print_debug_console = debug