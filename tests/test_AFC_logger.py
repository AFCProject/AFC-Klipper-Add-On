"""
Unit tests for extras/AFC_logger.py

Covers:
  - AFC_logger instantiation (with and without log_file start arg)
  - _remove_tags: strips HTML-like tags
  - _add_monotonic: prepends reactor time
  - set_debug: toggles print_debug_console
  - send_callback: only calls GCodeHelper callbacks
  - info / warning / debug / error / raw: verify log routing
  - shutdown: stops queue listener
"""

from __future__ import annotations

import logging
from unittest.mock import ANY, MagicMock, patch, call
import pytest

from extras.AFC_logger import AFC_logger


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_afc_obj(message_queue=None):
    """Minimal afc stand-in for AFC_logger."""
    afc = MagicMock()
    afc.message_queue = message_queue if message_queue is not None else []
    afc.log_frame_data = True
    return afc


def _make_printer(monotonic_val=42.0, log_file=None):
    from tests.conftest import MockPrinter, MockReactor

    printer = MockPrinter()
    reactor = MockReactor(monotonic_value=monotonic_val)
    printer._reactor = reactor
    printer.reactor = reactor  # keep public attribute in sync
    printer.start_args = {"log_file": log_file} if log_file else {}
    # gcode.output_callbacks is used by send_callback
    printer._gcode.output_callbacks = []
    return printer


def _make_logger(log_file=None):
    printer = _make_printer(log_file=log_file)
    afc = _make_afc_obj()
    return AFC_logger(printer, afc), afc, printer


# ── Instantiation ─────────────────────────────────────────────────────────────

class TestAFCLoggerInit:
    def test_init_without_log_file(self):
        logger, _, _ = _make_logger(log_file=None)
        assert logger.afc_ql is None
        assert logger.print_debug_console is False
        assert logger.adaptive_padding == 0

    def test_init_sets_reactor(self):
        printer = _make_printer(monotonic_val=77.0)
        afc = _make_afc_obj()
        lg = AFC_logger(printer, afc)
        assert lg.reactor is printer._reactor

    def test_set_debug_true(self):
        logger, _, _ = _make_logger()
        logger.set_debug(True)
        assert logger.print_debug_console is True

    def test_set_debug_false(self):
        logger, _, _ = _make_logger()
        logger.set_debug(True)
        logger.set_debug(False)
        assert logger.print_debug_console is False


# ── _remove_tags ──────────────────────────────────────────────────────────────

class TestRemoveTags:
    def test_removes_span_tags(self):
        logger, _, _ = _make_logger()
        result = logger._remove_tags("<span class=error--text>hello</span>")
        assert result == "hello"

    def test_removes_nested_tags(self):
        logger, _, _ = _make_logger()
        result = logger._remove_tags("<b><i>bold italic</i></b>")
        assert result == "bold italic"

    def test_no_tags_unchanged(self):
        logger, _, _ = _make_logger()
        result = logger._remove_tags("plain text")
        assert result == "plain text"

    def test_empty_string(self):
        logger, _, _ = _make_logger()
        assert logger._remove_tags("") == ""


# ── _add_monotonic ────────────────────────────────────────────────────────────

class TestAddMonotonic:
    def test_prepends_monotonic_time(self):
        printer = _make_printer(monotonic_val=123.456)
        afc = _make_afc_obj()
        lg = AFC_logger(printer, afc)
        result = lg._add_monotonic("hello")
        assert "123.456" in result
        assert "hello" in result


# ── _format ───────────────────────────────────────────────────────────────────

class TestFormat:
    def _make_code(self, co_filename="afc_test.py", co_name="some_func", co_firstlineno=42):
        code = MagicMock()
        code.co_filename = co_filename
        code.co_name = co_name
        code.co_firstlineno = co_firstlineno
        return code

    def test_no_code_skips_frame_prefix(self):
        """When code is None there's nothing to build a prefix from, so the
        message is formatted with no file/function/line prefix at all."""
        logger, afc, _ = _make_logger()
        result = logger._format("hello", code=None)
        assert result.endswith("- hello")
        assert "afc_test.py" not in result

    def test_code_given_adds_frame_prefix(self):
        logger, afc, _ = _make_logger()
        code = self._make_code()
        result = logger._format("hello", code=code)
        assert "afc_test.py:some_func():42" in result

    def test_code_given_but_log_frame_data_false_skips_prefix(self):
        """log_frame_data=False on the afc object suppresses the file/
        function/line prefix even though a code object was given."""
        logger, afc, _ = _make_logger()
        afc.log_frame_data = False
        code = self._make_code()
        result = logger._format("hello", code=code)
        assert "afc_test.py" not in result
        assert result.endswith("- hello")

    def test_adaptive_padding_grows_with_longer_prefix(self):
        logger, afc, _ = _make_logger()
        assert logger.adaptive_padding == 0
        code = self._make_code(co_filename="afc_test.py", co_name="some_func")
        logger._format("hello", code=code)
        assert logger.adaptive_padding == len("[afc_test.py:some_func():42] ")


# ── send_callback ──────────────────────────────────────────────────────────────

class TestSendCallback:
    def test_send_callback_calls_gcode_helper(self):
        """Only GCodeHelper instances should receive the callback."""
        from webhooks import GCodeHelper

        printer = _make_printer()
        afc = _make_afc_obj()
        lg = AFC_logger(printer, afc)

        # Attach a mock callback that looks like a GCodeHelper method
        mock_cb = MagicMock()
        mock_cb.__self__ = GCodeHelper()  # Simulate being bound to a GCodeHelper
        printer._gcode.output_callbacks.append(mock_cb)

        lg.send_callback("test message")
        mock_cb.assert_called_once_with("test message")

    def test_send_callback_skips_non_gcode_helper(self):
        """Non-GCodeHelper callbacks must be ignored."""
        printer = _make_printer()
        afc = _make_afc_obj()
        lg = AFC_logger(printer, afc)

        other_cb = MagicMock()
        other_cb.__self__ = object()  # NOT a GCodeHelper
        printer._gcode.output_callbacks.append(other_cb)

        lg.send_callback("test")
        other_cb.assert_not_called()


# ── Logging methods ───────────────────────────────────────────────────────────

class TestLoggingMethods:
    def _make_lg_with_mocked_logger(self):
        logger, afc, printer = _make_logger()
        logger.logger = MagicMock()  # replace stdlib logger with mock
        return logger, afc

    def test_info_calls_logger_info(self):
        lg, afc = self._make_lg_with_mocked_logger()
        lg.send_callback = MagicMock()
        lg.info("Hello info")
        lg.logger.info.assert_called()

    def test_info_console_only_skips_logger(self):
        lg, afc = self._make_lg_with_mocked_logger()
        lg.send_callback = MagicMock()
        lg.info("Console only", console_only=True)
        lg.logger.info.assert_not_called()

    def test_warning_calls_logger_debug(self):
        lg, afc = self._make_lg_with_mocked_logger()
        lg.send_callback = MagicMock()
        lg.warning("Watch out")
        lg.logger.debug.assert_called()

    def test_warning_appends_to_message_queue(self):
        lg, afc = self._make_lg_with_mocked_logger()
        lg.send_callback = MagicMock()
        lg.warning("A warning")
        warnings = [m for m, lvl in afc.message_queue if lvl == "warning"]
        assert len(warnings) == 1

    def test_debug_calls_logger_debug(self):
        lg, afc = self._make_lg_with_mocked_logger()
        lg.debug("Debug info")
        lg.logger.debug.assert_called()

    def test_debug_with_traceback_logs_traceback(self):
        lg, afc = self._make_lg_with_mocked_logger()
        lg.debug("Uh-oh", traceback="Traceback line 1\nTraceback line 2")
        # logger.debug must have been called at least twice
        assert lg.logger.debug.call_count >= 2

    def test_error_calls_logger_error(self):
        lg, afc = self._make_lg_with_mocked_logger()
        lg.send_callback = MagicMock()
        lg.error("Something broke")
        lg.logger.error.assert_called()

    def test_error_appends_to_message_queue(self):
        lg, afc = self._make_lg_with_mocked_logger()
        lg.send_callback = MagicMock()
        lg.error("Fatal error")
        errors = [m for m, lvl in afc.message_queue if lvl == "error"]
        assert len(errors) == 1

    def test_error_with_stack_name_formats_message(self):
        lg, afc = self._make_lg_with_mocked_logger()
        lg.send_callback = MagicMock()
        lg.error("Broke", stack_name="my_func")
        formatted = lg.logger.error.call_args[0][0]
        assert "my_func" in formatted

    def test_error_with_traceback_logs_each_line(self):
        lg, afc = self._make_lg_with_mocked_logger()
        lg.send_callback = MagicMock()
        lg.error("Something broke", traceback="Traceback line 1\nTraceback line 2")
        # Called once for the message + once per traceback line (2)
        assert lg.logger.error.call_count >= 3

    def test_debug_with_print_debug_sends_callback(self):
        """Independent coverage of the `not only_debug` operand of
        `self.print_debug_console and not only_debug`: only_debug defaults
        to False here, so print_debug_console alone drives the callback."""
        lg, afc = self._make_lg_with_mocked_logger()
        lg.print_debug_console = True
        lg.send_callback = MagicMock()
        lg.debug("debug message")
        lg.send_callback.assert_called()

    def test_debug_without_print_debug_skips_callback(self):
        """Independent coverage of the `self.print_debug_console` operand:
        only_debug is False (would allow the callback), but
        print_debug_console defaults to False from __init__."""
        lg, afc = self._make_lg_with_mocked_logger()
        lg.send_callback = MagicMock()
        lg.debug("debug message", only_debug=False)
        lg.send_callback.assert_not_called()

    def test_debug_with_only_debug_skips_callback_even_when_print_debug_enabled(self):
        """Independent coverage of the `not only_debug` operand failing:
        print_debug_console is True (would otherwise allow the callback),
        but only_debug=True suppresses it."""
        lg, afc = self._make_lg_with_mocked_logger()
        lg.print_debug_console = True
        lg.send_callback = MagicMock()
        lg.debug("debug message", only_debug=True)
        lg.send_callback.assert_not_called()

    def test_raw_sends_callback(self):
        lg, _ = self._make_lg_with_mocked_logger()
        lg.send_callback = MagicMock()
        lg.raw("raw output")
        lg.send_callback.assert_called()

    def test_raw_calls_logger_info(self):
        lg, _ = self._make_lg_with_mocked_logger()
        lg.send_callback = MagicMock()
        lg.raw("raw output")
        lg.logger.info.assert_called()


# ── shutdown ──────────────────────────────────────────────────────────────────

class TestShutdown:
    def test_shutdown_stops_queue_listener(self):
        logger, _, _ = _make_logger()
        logger.afc_ql = MagicMock()
        logger.shutdown()
        logger.afc_ql.stop.assert_called_once()

    def test_shutdown_no_queue_listener_does_not_raise(self):
        logger, _, _ = _make_logger()
        assert logger.afc_ql is None
        logger.shutdown()  # should not raise


# ── AFC_QueueListener ─────────────────────────────────────────────────────────

class TestAFCQueueListener:
    def test_queue_listener_init_calls_file_handler_init(self, tmp_path):
        from extras.AFC_logger import AFC_QueueListener
        filename = str(tmp_path / "afc_test.log")
        with patch("logging.handlers.TimedRotatingFileHandler.__init__", return_value=None):
            ql = AFC_QueueListener(filename)
        assert ql is not None

    def test_queue_listener_has_bg_queue_after_init(self, tmp_path):
        import queue as _queue
        from extras.AFC_logger import AFC_QueueListener
        filename = str(tmp_path / "afc_test.log")
        with patch("logging.handlers.TimedRotatingFileHandler.__init__", return_value=None):
            ql = AFC_QueueListener(filename)
        assert hasattr(ql, "bg_queue")
        assert isinstance(ql.bg_queue, _queue.Queue)

    def test_uses_single_arg_super_init_when_file_size_present(self, tmp_path):
        """When queuelogger has FILE_SIZE (upstream Klipper), the parent is
        constructed with just the filename, no Kalico-style extra arg."""
        import queuelogger as _queuelogger
        from extras.AFC_logger import AFC_QueueListener
        filename = str(tmp_path / "afc_test.log")
        with patch.object(_queuelogger, "FILE_SIZE", 100, create=True):
            with patch("queuelogger.QueueListener.__init__", return_value=None) as mock_init:
                AFC_QueueListener(filename)
        mock_init.assert_called_once_with(filename)

    def test_falls_back_to_single_arg_when_kalico_style_init_raises(self, tmp_path):
        """When queuelogger has no FILE_SIZE (Kalico), the extra rollover arg
        is tried first; if the parent rejects it, retry with just filename."""
        from extras.AFC_logger import AFC_QueueListener

        def fake_init(self, *args, **kwargs):
            if len(args) >= 2:
                raise TypeError("no extra arg accepted")

        filename = str(tmp_path / "afc_test.log")
        with patch("queuelogger.QueueListener.__init__", fake_init):
            ql = AFC_QueueListener(filename)
        assert ql is not None

    def test_calls_timed_rotating_file_handler_init_when_queuelistener_is_subclass(self, tmp_path):
        """When queuelogger.QueueListener is itself a TimedRotatingFileHandler
        subclass, that handler's own __init__ must also run so rollover works."""
        from extras.AFC_logger import AFC_QueueListener

        class FakeSubclass(logging.handlers.TimedRotatingFileHandler):
            def __init__(self, *args, **kwargs):
                pass

        filename = str(tmp_path / "afc_test.log")
        with patch("extras.AFC_logger.QueueListener", FakeSubclass):
            with patch("logging.handlers.TimedRotatingFileHandler.__init__",
                       return_value=None) as mock_trfh_init:
                AFC_QueueListener(filename)
        mock_trfh_init.assert_called_once_with(
            ANY, filename, when="S", interval=60 * 60 * 24, backupCount=5)


# ── AFC_logger init with log_file ─────────────────────────────────────────────

class TestAFCLoggerInitWithLogFile:
    def test_init_with_log_file_creates_afc_ql(self, tmp_path):
        import queue as _queue
        log_path = str(tmp_path / "klippy.log")
        printer = _make_printer(log_file=log_path)
        afc = _make_afc_obj()
        with patch("extras.AFC_logger.AFC_QueueListener") as mock_ql_cls:
            instance = MagicMock()
            instance.bg_queue = _queue.Queue()
            mock_ql_cls.return_value = instance
            lg = AFC_logger(printer, afc)
        assert lg.afc_ql is instance
        mock_ql_cls.assert_called_once()

    def test_init_with_log_file_adds_queue_handler(self, tmp_path):
        import queue as _queue
        import logging
        # Clear any handlers added by previous tests on the singleton "AFC" logger
        logging.getLogger("AFC").handlers.clear()
        log_path = str(tmp_path / "klippy.log")
        printer = _make_printer(log_file=log_path)
        afc = _make_afc_obj()
        with patch("extras.AFC_logger.AFC_QueueListener") as mock_ql_cls:
            instance = MagicMock()
            instance.bg_queue = _queue.Queue()
            mock_ql_cls.return_value = instance
            lg = AFC_logger(printer, afc)
        assert hasattr(lg, "afc_queue_handler")

    def test_init_with_log_file_does_not_create_ql_when_handler_exists(self, tmp_path):
        """When logger already has a QueueHandler, afc_ql is not created again."""
        import queue as _queue
        from queuelogger import QueueHandler
        log_path = str(tmp_path / "klippy.log")
        printer = _make_printer(log_file=log_path)
        afc = _make_afc_obj()
        # Pre-add a QueueHandler so the `if not any(...)` branch is False
        q = _queue.Queue()
        existing_handler = QueueHandler(q)
        logger_name = "AFC"
        import logging
        logging.getLogger(logger_name).addHandler(existing_handler)
        try:
            with patch("extras.AFC_logger.AFC_QueueListener") as mock_ql_cls:
                lg = AFC_logger(printer, afc)
            # afc_ql should remain None since handler already existed
            assert lg.afc_ql is None
            mock_ql_cls.assert_not_called()
        finally:
            logging.getLogger(logger_name).removeHandler(existing_handler)
