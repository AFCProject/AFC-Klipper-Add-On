"""Unit tests for CFSTrace, CFSTransport, CFSBus and set_lane_sensors (extras/AFC_CFS.py)."""
from __future__ import annotations

import errno
import json
import os
import queue
import termios
import threading

import pytest
from tests.conftest import MockLogger

from extras import AFC_CFS as P
from extras.AFC_CFS import CFSBus, CFSTrace, CFSTransport
from tests.cfs_helpers import ScriptedBox, frame, reset_buses

# Fakes

class FakeCompletion:
    """Reactor completion; wait() first runs the reactor's on_wait hooks (a box replying)."""

    def __init__(self, reactor):
        self.reactor = reactor
        self.result = None
        self.done = False

    def test(self):
        return self.done

    def complete(self, value):
        self.result = value
        self.done = True

    def wait(self, waketime=None, waketime_result=None):
        self.reactor.waits.append(waketime)
        for hook in list(self.reactor.on_wait):
            hook(self)
        if self.done:
            return self.result
        if waketime is not None:
            self.reactor.now = max(self.reactor.now, waketime)
        return waketime_result


class FakeMutex:
    def __init__(self):
        self.locked = False

    def test(self):
        return self.locked

    def __enter__(self):
        self.locked = True
        return self

    def __exit__(self, *exc):
        self.locked = False
        return False


class FakeReactor:
    def __init__(self, now=100.0):
        self.now = now
        self.pauses = []
        self.waits = []
        self.on_wait = []
        self.fds = {}
        self.unregistered = []

    def monotonic(self):
        return self.now

    def pause(self, until):
        self.pauses.append(until)
        self.now = max(self.now, until)
        return until

    def mutex(self):
        return FakeMutex()

    def completion(self):
        return FakeCompletion(self)

    def register_fd(self, fd, callback):
        handle = ("fd", fd)
        self.fds[handle] = callback
        return handle

    def unregister_fd(self, handle):
        self.unregistered.append(handle)
        self.fds.pop(handle, None)


class RecTrace:
    """Stands in for CFSTrace on a transport: records events."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.events = []

    def event(self, ev, **fields):
        self.events.append((ev, fields))

    def names(self):
        return [e for e, _ in self.events]


def make_transport(debug=False, path="/dev/fake_cfs"):
    t = CFSTransport(FakeReactor(), path, 230400, MockLogger(), "CFS bus x", debug=debug)
    t.trace = RecTrace()
    return t


def reply_with(t, fr):
    """on_wait hook: the box answers the pending request with fr."""
    def hook(completion):
        if t._pending is not None and t._pending["completion"] is completion:
            completion.complete(fr)
    return hook


# CFSTrace

class TestCFSTraceInit:
    def test_path_expanded(self):
        trace = CFSTrace("~/cfs.jsonl", 100, 2, MockLogger())
        assert trace.path == os.path.expanduser("~/cfs.jsonl")
        assert (trace.max_bytes, trace.backups) == (100, 2)
        assert trace.enabled is False
        assert (trace.dropped, trace.written, trace.last_error) == (0, 0, None)
        assert trace._thread is None

    def test_no_path(self):
        assert CFSTrace(None, 100, 2, MockLogger()).path is None

    def test_empty_path(self):
        assert CFSTrace("", 100, 2, MockLogger()).path is None


class TestCFSTraceStart:
    def test_no_path_doesnt_record(self):
        trace = CFSTrace(None, 100, 0, MockLogger())
        assert trace.start() is False
        assert trace.enabled is False
        assert trace._thread is None

    def test_starts_writer(self, tmp_path):
        trace = CFSTrace(str(tmp_path / "t.jsonl"), 10000, 0, MockLogger())
        assert trace.start() is True
        try:
            assert trace.enabled is True
            assert trace._thread.is_alive()
            assert trace._thread.name == "cfs-trace"
        finally:
            trace.stop()

    def test_running_writer_reused(self, tmp_path):
        trace = CFSTrace(str(tmp_path / "t.jsonl"), 10000, 0, MockLogger())
        trace.start()
        first = trace._thread
        try:
            assert trace.start() is True
            assert trace._thread is first
        finally:
            trace.stop()

    def test_dead_writer_replaced(self, tmp_path):
        trace = CFSTrace(str(tmp_path / "t.jsonl"), 10000, 0, MockLogger())
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        trace._thread = dead
        try:
            trace.start()
            assert trace._thread is not dead
            assert trace._thread.is_alive()
        finally:
            trace.stop()


class TestCFSTraceStop:
    def test_never_started(self):
        trace = CFSTrace(None, 100, 0, MockLogger())
        trace.enabled = True
        trace.stop()
        assert trace.enabled is False
        assert trace._thread is None

    def test_writes_queued_events_then_stops(self, tmp_path):
        path = tmp_path / "t.jsonl"
        trace = CFSTrace(str(path), 10000, 0, MockLogger())
        trace.start()
        thread = trace._thread
        trace.event("one", x=1)
        trace.stop()
        assert not thread.is_alive()
        assert trace._thread is None
        assert [json.loads(line)["ev"] for line in path.read_text().splitlines()] == ["one"]

    def test_dead_thread_just_cleared(self):
        trace = CFSTrace(None, 100, 0, MockLogger())
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        trace._thread = dead
        trace.stop()
        assert trace._thread is None
        assert trace._queue.empty()             # no stop marker sent to a dead thread

    def test_full_queue_still_joins(self):
        trace = CFSTrace(None, 100, 0, MockLogger())
        release = threading.Event()
        alive = threading.Thread(target=release.wait)
        alive.start()
        trace._thread = alive
        trace._queue = queue.Queue(maxsize=1)
        trace._queue.put("x")
        trace.stop(timeout=0.01)                # put(None) times out: Full is swallowed
        release.set()
        alive.join()
        assert trace._thread is None
        assert list(trace._queue.queue) == ["x"]


class TestCFSTraceEvent:
    def test_disabled_records_nothing(self):
        trace = CFSTrace(None, 100, 0, MockLogger())
        trace.event("x")
        assert trace._queue.empty()

    def test_queues_one_json_line(self):
        trace = CFSTrace(None, 100, 0, MockLogger())
        trace.enabled = True
        trace.event("rx", fn="GET_BOX_STATE", data=b"\x01")
        record = json.loads(trace._queue.get_nowait())
        assert set(record) == {"ts", "t", "ev", "fn", "data"}
        assert (record["ev"], record["fn"]) == ("rx", "GET_BOX_STATE")
        assert record["data"] == str(b"\x01")   # not JSON: written with str()

    def test_full_queue_counts_a_drop(self):
        trace = CFSTrace(None, 100, 0, MockLogger())
        trace.enabled = True
        trace._queue = queue.Queue(maxsize=1)
        trace.event("a")
        trace.event("b")
        assert trace.dropped == 1
        assert trace._queue.qsize() == 1

    def test_unserializable_counts_a_drop(self):
        trace = CFSTrace(None, 100, 0, MockLogger())
        trace.enabled = True
        loop = {}
        loop["self"] = loop                      # json.dumps: circular reference, ValueError
        trace.event("x", loop=loop)
        assert trace.dropped == 1
        assert trace._queue.empty()

    def test_type_error_counts_a_drop(self):
        trace = CFSTrace(None, 100, 0, MockLogger())
        trace.enabled = True
        trace.event("x", **{"k": {1: 2, "a": 3}})   # mixed key types: sort-free, still fine
        assert trace.dropped == 0

        class Bad:
            def __str__(self):
                raise TypeError("no str")
        trace.event("y", bad=Bad())
        assert trace.dropped == 1


class TestCFSTraceOpen:
    def test_creates_folder_and_reads_size(self, tmp_path):
        path = tmp_path / "logs" / "t.jsonl"
        trace = CFSTrace(str(path), 100, 0, MockLogger())
        handle = trace._open()
        handle.write("abc")
        handle.close()
        assert path.read_text() == "abc"
        assert trace._size == 0

    def test_existing_file_appended(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text("12345")
        trace = CFSTrace(str(path), 100, 0, MockLogger())
        trace._open().close()
        assert trace._size == 5

    def test_bare_file_name(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        trace = CFSTrace("t.jsonl", 100, 0, MockLogger())
        trace._open().close()
        assert (tmp_path / "t.jsonl").exists()


class TestCFSTraceRotate:
    def test_shifts_backups(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text("new")
        (tmp_path / "t.jsonl.1").write_text("old")
        trace = CFSTrace(str(path), 100, 2, MockLogger())
        handle = trace._rotate(open(path, "a"))
        handle.close()
        assert (tmp_path / "t.jsonl.1").read_text() == "new"
        assert (tmp_path / "t.jsonl.2").read_text() == "old"
        assert path.read_text() == ""

    def test_missing_backup_skipped(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text("new")
        trace = CFSTrace(str(path), 100, 2, MockLogger())
        trace._rotate(open(path, "a")).close()
        assert (tmp_path / "t.jsonl.1").read_text() == "new"
        assert not (tmp_path / "t.jsonl.2").exists()

    def test_no_backups_truncates(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text("new")
        trace = CFSTrace(str(path), 100, 0, MockLogger())
        trace._rotate(open(path, "a")).close()
        assert path.read_text() == ""
        assert sorted(p.name for p in tmp_path.iterdir()) == ["t.jsonl"]

    def test_no_backups_file_already_gone(self, tmp_path):
        path = tmp_path / "t.jsonl"
        handle = open(path, "a")
        path.unlink()
        trace = CFSTrace(str(path), 100, 0, MockLogger())
        trace._rotate(handle).close()
        assert path.exists()


class TestCFSTraceRun:
    def _trace(self, path, max_bytes=10000, backups=0):
        trace = CFSTrace(str(path), max_bytes, backups, MockLogger())
        trace.enabled = True
        return trace

    def test_open_failure(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("")
        trace = self._trace(blocker / "t.jsonl")    # a file where the folder should be
        trace._run()
        assert trace.enabled is False
        assert trace.last_error.startswith("cannot open trace file: ")

    def test_writes_until_stop_marker(self, tmp_path):
        path = tmp_path / "t.jsonl"
        trace = self._trace(path)
        for line in ('{"ev":"a"}', '{"ev":"b"}', None, '{"ev":"after"}'):
            trace._queue.put(line)
        trace._run()
        assert path.read_text() == '{"ev":"a"}\n{"ev":"b"}\n'
        assert trace.written == 2
        assert trace._size == len('{"ev":"a"}\n{"ev":"b"}\n')

    def test_rotates_at_max_bytes(self, tmp_path):
        path = tmp_path / "t.jsonl"
        trace = self._trace(path, max_bytes=6, backups=1)
        for line in ("12345", "abc", None):
            trace._queue.put(line)
        trace._run()
        assert (tmp_path / "t.jsonl.1").read_text() == "12345\n"
        assert path.read_text() == "abc\n"

    def test_flushes_when_queue_runs_dry(self, tmp_path):
        path = tmp_path / "t.jsonl"
        trace = self._trace(path)
        seen = []

        def get():
            if not seen:
                seen.append(1)
                return "x"
            # The writer flushed before asking for the next line
            seen.append(path.read_text())
            return None
        trace._queue.get = get
        trace._queue.empty = lambda: len(seen) == 1
        trace._run()
        assert seen == [1, "x\n"]

    def test_no_flush_while_more_is_queued(self, tmp_path):
        path = tmp_path / "t.jsonl"
        trace = self._trace(path)
        lines = iter(["x", None])
        seen = []

        def get():
            line = next(lines)
            if line is None:
                seen.append(path.read_text())
            return line
        trace._queue.get = get
        trace._queue.empty = lambda: False
        trace._run()
        assert seen == [""]                     # still buffered when the marker came

    def test_write_failure(self, tmp_path, monkeypatch):
        path = tmp_path / "t.jsonl"
        trace = self._trace(path)

        class FailingHandle:
            closed = False

            def write(self, s):
                raise OSError(28, "No space left on device")

            def flush(self):
                pass

            def close(self):
                FailingHandle.closed = True
        monkeypatch.setattr(trace, "_open", lambda: FailingHandle())
        trace._queue.put("x")
        trace._run()
        assert trace.enabled is False
        assert trace.last_error == "trace write failed: [Errno 28] No space left on device"
        assert FailingHandle.closed is True

    def test_close_failure_swallowed(self, tmp_path, monkeypatch):
        trace = self._trace(tmp_path / "t.jsonl")

        class BadClose:
            def flush(self):
                raise OSError("gone")

            def close(self):
                pass
        monkeypatch.setattr(trace, "_open", lambda: BadClose())
        trace._queue.put(None)
        trace._run()                            # no exception escapes
        assert trace.last_error is None


# CFSTransport

class TestCFSTransportInit:
    def test_defaults(self):
        reactor = FakeReactor()
        logger = MockLogger()
        t = CFSTransport(reactor, "/dev/x", 230400, logger, "CFS bus /dev/x", debug=True)
        assert (t.reactor, t.path, t.baud, t.logger, t.name, t.debug) == (
            reactor, "/dev/x", 230400, logger, "CFS bus /dev/x", True)
        assert (t.fd, t._fd_handle, t._pending, t.last_error, t.trace) == (
            None, None, None, None, None)
        assert (t.tx_count, t.rx_count, t.timeouts, t._outstanding) == (0, 0, 0, 0)
        assert t.resync_timeout == 15.0
        assert t._drain is None
        assert isinstance(t._parser, P.FrameParser)


class TestCFSTransportTrace:
    def test_no_trace_attached(self):
        t = make_transport()
        t.trace = None
        t._trace("x", a=1)                      # nothing to record into, no error

    def test_records_event(self):
        t = make_transport()
        t._trace("port_open", path="/dev/x")
        assert t.trace.events == [("port_open", {"path": "/dev/x"})]


class TestCFSTransportIsOpen:
    def test_closed(self):
        assert make_transport().is_open is False

    def test_open(self):
        t = make_transport()
        t.fd = 7
        assert t.is_open is True


class TestCFSTransportOpen:
    def test_already_open(self):
        t = make_transport()
        t.fd = 7
        assert t.open() is True
        assert t.trace.events == []

    def test_unsupported_baud(self):
        t = make_transport()
        t.baud = 12345
        assert t.open() is False
        assert t.last_error == "unsupported baud 12345"
        assert t.fd is None

    def test_open_failure(self, tmp_path):
        missing = str(tmp_path / "nope")
        t = make_transport(path=missing)
        assert t.open() is False
        expected = f"open {missing} failed: [Errno 2] No such file or directory: '{missing}'"
        assert t.last_error == expected
        assert t.trace.events == [("port_error", {"error": expected})]

    def test_opens_a_pty(self):
        master, slave = os.openpty()
        try:
            t = make_transport(path=os.ttyname(slave))
            t.last_error = "old"
            t._parser.feed(b"\xf7\x01")
            assert t.open() is True
            assert t.fd is not None
            assert t.reactor.fds == {t._fd_handle: t._on_readable}
            assert bytes(t._parser._buf) == b""
            assert t.last_error is None
            assert t.trace.events == [("port_open", {"path": os.ttyname(slave), "baud": 230400})]
            attrs = termios.tcgetattr(t.fd)
            assert attrs[4] == termios.B230400 and attrs[5] == termios.B230400
            assert attrs[2] & termios.CSIZE == termios.CS8
            assert t.logger.messages == []
            t.close()
        finally:
            os.close(master)
            os.close(slave)

    def test_termios_refusal_is_only_logged(self, tmp_path, monkeypatch):
        dev = tmp_path / "dev"
        dev.write_text("")
        t = make_transport(path=str(dev))
        monkeypatch.setattr(P.termios, "tcgetattr",
                            lambda fd: (_ for _ in ()).throw(termios.error(25, "not a tty")))
        assert t.open() is True
        assert t.logger.messages == [
            ("debug", "CFS bus x: termios setup warning: (25, 'not a tty')")]
        t.close()


class TestCFSTransportClose:
    def test_nothing_open(self):
        t = make_transport()
        t._outstanding = 2
        t.close()
        assert t.reactor.unregistered == []
        assert t._outstanding == 0

    def test_unregisters_and_closes(self, monkeypatch):
        t = make_transport()
        closed = []
        monkeypatch.setattr(P.os, "close", lambda fd: closed.append(fd))
        t.fd = 9
        t._fd_handle = ("fd", 9)
        t.close()
        assert t.reactor.unregistered == [("fd", 9)]
        assert closed == [9]
        assert (t.fd, t._fd_handle) == (None, None)

    def test_unregister_and_close_errors_swallowed(self, monkeypatch):
        t = make_transport()
        monkeypatch.setattr(t.reactor, "unregister_fd",
                            lambda h: (_ for _ in ()).throw(RuntimeError("x")))
        monkeypatch.setattr(P.os, "close", lambda fd: (_ for _ in ()).throw(OSError("x")))
        t.fd = 9
        t._fd_handle = ("fd", 9)
        t.close()
        assert (t.fd, t._fd_handle) == (None, None)

    def test_completes_waiting_request_with_no_reply(self):
        t = make_transport()
        c = t.reactor.completion()
        t._pending = {"completion": c}
        t.close()
        assert (c.done, c.result) == (True, None)

    def test_finished_request_left_alone(self):
        t = make_transport()
        c = t.reactor.completion()
        c.complete("reply")
        t._pending = {"completion": c}
        t.close()
        assert c.result == "reply"

    def test_releases_a_drain_wait(self):
        t = make_transport()
        t._drain = t.reactor.completion()
        t.close()
        assert (t._drain.done, t._drain.result) == (True, False)

    def test_finished_drain_left_alone(self):
        t = make_transport()
        t._drain = t.reactor.completion()
        t._drain.complete(True)
        t.close()
        assert t._drain.result is True


class TestCFSTransportOnReadable:
    def _reads(self, monkeypatch, *results):
        seq = list(results)

        def read(fd, n):
            item = seq.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        monkeypatch.setattr(P.os, "read", read)

    def _pending(self, t, fn, addr=1, quiet=False):
        c = t.reactor.completion()
        t._pending = {"fn": fn, "addr": addr, "quiet": quiet, "completion": c,
                      "sent": t.reactor.now - 0.0123}
        return c

    def test_port_closed(self):
        t = make_transport()
        t._on_readable(0)
        assert t.rx_count == 0

    def test_nothing_to_read(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        self._reads(monkeypatch, BlockingIOError(errno.EAGAIN, "again"))
        t._on_readable(0)
        assert t.rx_count == 0
        assert t.trace.events == []

    def test_end_of_file(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        self._reads(monkeypatch, b"")
        t._on_readable(0)
        assert t.rx_count == 0

    def test_read_error_closes(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        monkeypatch.setattr(P.os, "close", lambda fd: None)
        self._reads(monkeypatch, OSError(errno.EIO, "Input/output error"))
        t._on_readable(0)
        assert t.last_error == "read error: [Errno 5] Input/output error"
        assert t.logger.messages == [("warning",
                                      "CFS bus x: serial read error: [Errno 5] Input/output error")]
        assert t.trace.events == [("port_error",
                                   {"error": "read error: [Errno 5] Input/output error"})]
        assert t.fd is None

    def test_reply_completes_pending_request(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        raw = bytes.fromhex("f70103000a8b")       # GET_BOX_STATE, status OK, no data
        self._reads(monkeypatch, raw[:3], raw[3:], BlockingIOError(errno.EAGAIN, ""))
        c = self._pending(t, P.GET_BOX_STATE)
        t._on_readable(0)
        assert c.result == P.Frame(1, 0x00, 0x0A, b"")
        assert t.rx_count == 1
        assert t.trace.events == [("rx", {"fn": "GET_BOX_STATE", "addr": 1, "st": "OK",
                                          "data": "", "ms": 12.3})]

    def test_quiet_reply_not_traced(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        self._reads(monkeypatch, P.encode_frame(1, 0, P.GET_BOX_STATE), b"")
        c = self._pending(t, P.GET_BOX_STATE, quiet=True)
        t._on_readable(0)
        assert c.done is True
        assert t.trace.events == []

    def test_broadcast_request_takes_any_address(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        self._reads(monkeypatch, P.encode_frame(3, 0, P.CMD_GET_SLAVE_INFO), b"")
        c = self._pending(t, P.CMD_GET_SLAVE_INFO, addr=P.BROADCAST_BOXES, quiet=True)
        t._on_readable(0)
        assert c.result.addr == 3

    def test_other_address_is_unsolicited(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        self._reads(monkeypatch, P.encode_frame(2, 0, P.GET_BOX_STATE), b"")
        c = self._pending(t, P.GET_BOX_STATE, addr=1)
        t._on_readable(0)
        assert c.done is False
        assert t.trace.names() == ["rx_unsolicited"]

    def test_other_function_is_unsolicited(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        self._reads(monkeypatch, P.encode_frame(1, 0, P.GET_BUFFER_STATE), b"")
        c = self._pending(t, P.GET_BOX_STATE)
        t._on_readable(0)
        assert c.done is False
        assert t.trace.events == [("rx_unsolicited", {"fn": "GET_BUFFER_STATE", "addr": 1,
                                                      "st": "OK", "data": ""})]

    def test_second_reply_to_answered_request_is_unsolicited(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        self._reads(monkeypatch, P.encode_frame(1, 0, P.GET_BOX_STATE, b"\x07"), b"")
        c = self._pending(t, P.GET_BOX_STATE)
        c.complete("first")
        t._on_readable(0)
        assert c.result == "first"
        assert t.trace.names() == ["rx_unsolicited"]

    def test_no_pending_request_is_unsolicited(self, monkeypatch):
        t = make_transport(debug=True)
        t.fd = 5
        raw = P.encode_frame(1, 0, P.GET_BOX_STATE)
        self._reads(monkeypatch, raw, b"")
        t._on_readable(0)
        assert t.logger.messages == [
            ("debug", "CFS bus x: RX GET_BOX_STATE OK "),
            ("debug", f"CFS bus x: unsolicited frame {raw.hex()}"),
        ]

    def test_late_reply_counted_down(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        t._outstanding = 2
        t._drain = t.reactor.completion()
        self._reads(monkeypatch, P.encode_frame(1, 0, P.SET_PRE_LOADING), b"")
        t._on_readable(0)
        assert t._outstanding == 1
        assert t._drain.done is False            # still one to go
        assert t.trace.events == [("late_reply", {"fn": "SET_PRE_LOADING", "addr": 1,
                                                  "st": "OK", "data": "",
                                                  "still_outstanding": 1})]

    def test_last_late_reply_releases_drain(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        t._outstanding = 1
        t._drain = t.reactor.completion()
        self._reads(monkeypatch, P.encode_frame(1, 0, P.SET_PRE_LOADING), b"")
        t._on_readable(0)
        assert t._outstanding == 0
        assert (t._drain.done, t._drain.result) == (True, True)

    def test_last_late_reply_without_drain(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        t._outstanding = 1
        self._reads(monkeypatch, P.encode_frame(1, 0, P.SET_PRE_LOADING), b"")
        t._on_readable(0)
        assert (t._outstanding, t._drain) == (0, None)

    def test_last_late_reply_drain_already_done(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        t._outstanding = 1
        t._drain = t.reactor.completion()
        t._drain.complete(False)
        self._reads(monkeypatch, P.encode_frame(1, 0, P.SET_PRE_LOADING), b"")
        t._on_readable(0)
        assert t._drain.result is False

    def test_debug_logs_received_frames(self, monkeypatch):
        t = make_transport(debug=True)
        t.fd = 5
        self._reads(monkeypatch, P.encode_frame(1, 3, P.GET_BOX_STATE, b"\xab"), b"")
        self._pending(t, P.GET_BOX_STATE)
        t._on_readable(0)
        assert t.logger.messages == [("debug", "CFS bus x: RX GET_BOX_STATE STATE_ERR ab")]

    def test_new_crc_errors_traced_once(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        bad = bytes.fromhex("f70103ff1407")
        self._reads(monkeypatch, bad, b"", bad, b"", b"\x00", b"")
        t._on_readable(0)
        t._on_readable(0)
        t._on_readable(0)                        # no new CRC error this time
        assert t.trace.events == [("crc_error", {"total": 1, "new": 1}),
                                  ("crc_error", {"total": 2, "new": 1})]
        assert t._traced_crc_errors == 2


class TestCFSTransportWriteAll:
    def test_writes_in_pieces(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        out = []

        def write(fd, view):
            out.append(bytes(view[:2]))
            return min(2, len(view))
        monkeypatch.setattr(P.os, "write", write)
        assert t._write_all(b"abcde") is True
        assert out == [b"ab", b"cd", b"e"]

    def test_empty_data(self):
        assert make_transport()._write_all(b"") is True

    def test_port_closed(self):
        assert make_transport()._write_all(b"x") is False

    def test_busy_port_retries(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        results = [BlockingIOError(errno.EAGAIN, ""), 1]

        def write(fd, view):
            item = results.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        monkeypatch.setattr(P.os, "write", write)
        assert t._write_all(b"x") is True
        assert t.reactor.pauses == [100.002]

    def test_busy_too_long(self, monkeypatch):
        t = make_transport()
        t.fd = 5

        def write(fd, view):
            t.reactor.now += 0.6
            raise BlockingIOError(errno.EWOULDBLOCK, "")
        monkeypatch.setattr(P.os, "write", write)
        assert t._write_all(b"x") is False
        assert t.last_error == "write timeout"
        assert t.reactor.pauses == []

    def test_write_error_closes(self, monkeypatch):
        t = make_transport()
        t.fd = 5
        monkeypatch.setattr(P.os, "close", lambda fd: None)
        monkeypatch.setattr(P.os, "write",
                            lambda fd, v: (_ for _ in ()).throw(OSError(errno.EIO, "gone")))
        assert t._write_all(b"x") is False
        assert t.last_error == "write error: [Errno 5] gone"
        assert t.fd is None


class TestCFSTransportRequest:
    def _open(self, t, writes=None):
        t.fd = 5
        t._write_all = lambda data: (writes.append(bytes(data)) if writes is not None else None) \
            or True

    def test_port_wont_open(self):
        t = make_transport()
        t.open = lambda: False
        assert t.request(1, P.GET_BOX_STATE) is None
        assert t.tx_count == 0

    def test_opens_then_sends(self):
        t = make_transport()
        writes = []

        def fake_open():
            self._open(t, writes)
            return True
        t.open = fake_open
        reply = frame(P.GET_BOX_STATE)
        t.reactor.on_wait.append(reply_with(t, reply))
        assert t.request(1, P.GET_BOX_STATE) == reply
        assert writes == [bytes.fromhex("f70103ff0a5c")]

    def test_reply(self):
        t = make_transport()
        writes = []
        self._open(t, writes)
        reply = frame(P.SET_BOX_MODE, b"\x01")
        t.reactor.on_wait.append(reply_with(t, reply))
        assert t.request(1, P.SET_BOX_MODE, b"\x00\x01", timeout=0.8) == reply
        assert writes == [bytes.fromhex("f70105ff04000190")]
        assert t.tx_count == 1
        assert t._pending is None
        assert t.reactor.waits == [100.8]
        assert t.trace.events == [("tx", {"fn": "SET_BOX_MODE", "addr": 1, "data": "0001",
                                          "attempt": 1})]

    def test_quiet_request_not_traced(self):
        t = make_transport()
        self._open(t)
        t.reactor.on_wait.append(reply_with(t, frame(P.GET_BOX_STATE)))
        t.request(1, P.GET_BOX_STATE, quiet=True)
        assert t.trace.events == []

    def test_debug_logs_sent_frame(self):
        t = make_transport(debug=True)
        self._open(t)
        t.reactor.on_wait.append(reply_with(t, frame(P.GET_BOX_STATE)))
        t.request(1, P.GET_BOX_STATE)
        assert t.logger.messages == [("debug", "CFS bus x: TX GET_BOX_STATE f70103ff0a5c")]

    def test_timeout_then_retry_answers(self):
        t = make_transport()
        self._open(t)
        answers = [None, frame(P.GET_BOX_STATE)]

        def hook(completion):
            item = answers.pop(0)
            if item is not None:
                completion.complete(item)
        t.reactor.on_wait.append(hook)
        assert t.request(1, P.GET_BOX_STATE, timeout=1.0, retries=1) == frame(P.GET_BOX_STATE)
        assert t.timeouts == 1
        assert t._outstanding == 0                # not a motor command
        assert t.trace.names() == ["tx", "timeout", "tx"]
        assert t.trace.events[1] == ("timeout", {"fn": "GET_BOX_STATE", "addr": 1, "data": "",
                                                 "attempt": 1, "of": 2, "timeout_s": 1.0})

    def test_every_attempt_times_out(self):
        t = make_transport(debug=True)
        self._open(t)
        assert t.request(1, P.GET_BOX_STATE, retries=1) is None
        assert t.timeouts == 2
        assert [m for m in t.logger.messages if "timeout" in m[1]] == [
            ("debug", "CFS bus x: timeout GET_BOX_STATE (attempt 1/2)"),
            ("debug", "CFS bus x: timeout GET_BOX_STATE (attempt 2/2)"),
        ]

    def test_motor_timeout_counts_as_outstanding(self):
        t = make_transport()
        self._open(t)
        assert t.request(1, P.SET_PRE_LOADING, b"\x01\x02", retries=0, motor=True) is None
        assert t._outstanding == 1

    def test_waits_for_late_replies_first(self):
        t = make_transport()
        self._open(t)
        t._outstanding = 1
        waited = []
        t._wait_for_late_replies = lambda: waited.append(t._outstanding)
        t.reactor.on_wait.append(reply_with(t, frame(P.GET_BOX_STATE)))
        t.request(1, P.GET_BOX_STATE)
        assert waited == [1]

    def test_failed_write_tries_again(self):
        t = make_transport()
        t.fd = 5
        results = [False, True]
        t._write_all = lambda data: results.pop(0)
        t.last_error = "write timeout"
        t.reactor.on_wait.append(reply_with(t, frame(P.GET_BOX_STATE)))
        assert t.request(1, P.GET_BOX_STATE, retries=1) == frame(P.GET_BOX_STATE)
        assert t.trace.events[0] == ("tx_failed", {"fn": "GET_BOX_STATE", "error": "write timeout"})
        assert t.tx_count == 1

    def test_failed_write_every_attempt(self):
        t = make_transport()
        t.fd = 5
        t._write_all = lambda data: False
        assert t.request(1, P.GET_BOX_STATE, retries=0) is None
        assert t._pending is None
        assert t.trace.names() == ["tx_failed"]

    def test_holds_the_mutex(self):
        t = make_transport()
        self._open(t)
        seen = []
        t.reactor.on_wait.append(lambda c: seen.append(t.busy))
        t.request(1, P.GET_BOX_STATE, retries=0)
        assert seen == [True]
        assert t.busy is False


class TestCFSTransportWaitForLateReplies:
    def test_replies_arrive(self):
        t = make_transport()
        t._outstanding = 2
        t.resync_timeout = 5.0

        def hook(completion):
            t.reactor.now += 1.25
            completion.complete(True)
        t.reactor.on_wait.append(hook)
        t._wait_for_late_replies()
        assert t.reactor.waits == [105.0]
        assert t._drain is None
        assert t.trace.events == [("resync", {"waited_s": 1.25, "late_replies": 2})]
        assert t.logger.messages == []

    def test_box_never_answers(self):
        t = make_transport()
        t._outstanding = 2
        t.resync_timeout = 5.0
        t._wait_for_late_replies()
        assert t._outstanding == 0
        assert t.trace.events == [("resync_gave_up", {"waited_s": 5.0, "still_outstanding": 2})]
        assert t.logger.messages == [
            ("warning", "CFS bus x: box never answered 2 request(s); continuing")]


class TestCFSTransportBusy:
    def test_idle(self):
        assert make_transport().busy is False

    def test_in_a_request(self):
        t = make_transport()
        t._mutex.locked = True
        assert t.busy is True


# CFSBus

class FakeUnit:
    def __init__(self, name, box_addr, box_uid=None, depth=0):
        self.name = name
        self.box_addr = box_addr
        self.box_uid = box_uid
        self._operation_depth = depth


UID_A = bytes.fromhex("67913b482422b00236353434")
UID_B = bytes.fromhex("b0313b4e8411b00645353230")


@pytest.fixture(autouse=True)
def _fresh_buses():
    reset_buses()
    yield
    reset_buses()


def slave_info(uid, addr=0):
    """CMD_GET_SLAVE_INFO reply: two bytes then the 12-byte uid."""
    return frame(P.CMD_GET_SLAVE_INFO, b"\x00\x00" + uid, addr=addr)


class TestCFSBusGet:
    def test_creates_one_per_port(self):
        reactor = FakeReactor()
        bus = CFSBus.get(reactor, "/dev/a", 230400, MockLogger())
        assert CFSBus.get(reactor, "/dev/a", 230400, MockLogger()) is bus
        assert CFSBus.get(reactor, "/dev/b", 230400, MockLogger()) is not bus
        assert bus.transport.path == "/dev/a"
        assert bus.transport.name == "CFS bus /dev/a"

    def test_new_reactor_gets_a_new_bus(self):
        old = CFSBus.get(FakeReactor(), "/dev/a", 230400, MockLogger())
        reactor = FakeReactor()
        new = CFSBus.get(reactor, "/dev/a", 230400, MockLogger())
        assert new is not old
        assert new.reactor is reactor
        assert CFSBus._buses["/dev/a"] is new

    def test_baud_mismatch(self):
        reactor = FakeReactor()
        CFSBus.get(reactor, "/dev/a", 230400, MockLogger())
        with pytest.raises(ValueError) as e:
            CFSBus.get(reactor, "/dev/a", 115200, MockLogger())
        assert str(e.value) == "serial /dev/a: every CFS on one chain must use the same baud"

    def test_debug_passed_to_transport(self):
        bus = CFSBus.get(FakeReactor(), "/dev/a", 230400, MockLogger(), debug=True)
        assert bus.transport.debug is True


class TestCFSBusInit:
    def test_fields(self):
        reactor = FakeReactor()
        logger = MockLogger()
        bus = CFSBus(reactor, "/dev/a", 230400, logger, False)
        assert (bus.reactor, bus.logger, bus.units) == (reactor, logger, [])
        assert (bus.transport.baud, bus.transport.debug) == (230400, False)


class TestCFSBusRegister:
    def _bus(self):
        return CFSBus(FakeReactor(), "/dev/a", 230400, MockLogger(), False)

    def test_first_unit(self):
        bus = self._bus()
        unit = FakeUnit("CFS_1", 1)
        bus.register(unit)
        assert bus.units == [unit]
        assert bus.logger.messages == []

    def test_address_clash(self):
        bus = self._bus()
        bus.register(FakeUnit("CFS_1", 1))
        with pytest.raises(ValueError) as e:
            bus.register(FakeUnit("CFS_2", 1))
        assert str(e.value) == ("CFS CFS_1 and CFS_2 are both box_addr 1 on /dev/a; give each "
                                "box on a chain its own box_addr (1-4)")

    def test_uid_clash(self):
        bus = self._bus()
        bus.register(FakeUnit("CFS_1", 1, UID_A))
        with pytest.raises(ValueError) as e:
            bus.register(FakeUnit("CFS_2", 2, UID_A))
        assert str(e.value) == "CFS CFS_1 and CFS_2 have the same box_uid"

    def test_both_without_uid_not_a_clash(self):
        bus = self._bus()
        bus.register(FakeUnit("CFS_1", 1))
        bus.register(FakeUnit("CFS_2", 2))
        assert len(bus.units) == 2

    def test_second_unit_without_uid_warns(self):
        bus = self._bus()
        bus.register(FakeUnit("CFS_1", 1, UID_A))
        bus.register(FakeUnit("CFS_2", 2))
        assert bus.logger.messages == [("warning",
            "CFS CFS_2: more than one box on /dev/a without box_uid set; boxes are then "
            "addressed in discovery order, which can change. Set box_uid on each "
            "(AFC_CFS_STATUS shows it).")]

    def test_earlier_unit_without_uid_warns(self):
        bus = self._bus()
        bus.register(FakeUnit("CFS_1", 1))
        bus.register(FakeUnit("CFS_2", 2, UID_B))
        assert [m[0] for m in bus.logger.messages] == ["warning"]

    def test_every_unit_with_uid_is_quiet(self):
        bus = self._bus()
        bus.register(FakeUnit("CFS_1", 1, UID_A))
        bus.register(FakeUnit("CFS_2", 2, UID_B))
        assert bus.logger.messages == []


class TestCFSBusOperationActive:
    def test_idle(self):
        bus = CFSBus(FakeReactor(), "/dev/a", 230400, MockLogger(), False)
        bus.units = [FakeUnit("CFS_1", 1), FakeUnit("CFS_2", 2)]
        assert bus.operation_active is False

    def test_one_box_busy(self):
        bus = CFSBus(FakeReactor(), "/dev/a", 230400, MockLogger(), False)
        bus.units = [FakeUnit("CFS_1", 1), FakeUnit("CFS_2", 2, depth=1)]
        assert bus.operation_active is True


class TestCFSBusUnitForUid:
    def test_matches(self):
        bus = CFSBus(FakeReactor(), "/dev/a", 230400, MockLogger(), False)
        b = FakeUnit("CFS_2", 2, UID_B)
        bus.units = [FakeUnit("CFS_1", 1, UID_A), b]
        assert bus.unit_for_uid(UID_B) is b

    def test_unit_without_uid_never_matches(self):
        bus = CFSBus(FakeReactor(), "/dev/a", 230400, MockLogger(), False)
        bus.units = [FakeUnit("CFS_1", 1)]
        assert bus.unit_for_uid(b"") is None

    def test_no_match(self):
        bus = CFSBus(FakeReactor(), "/dev/a", 230400, MockLogger(), False)
        bus.units = [FakeUnit("CFS_1", 1, UID_A)]
        assert bus.unit_for_uid(UID_B) is None


class TestCFSBusDiscover:
    def _bus(self, *units):
        bus = CFSBus(FakeReactor(), "/dev/a", 230400, MockLogger(), False)
        bus.units = list(units)
        bus.box = ScriptedBox()
        bus.box.default_ok = False
        bus.transport.request = bus.box.request
        return bus

    def test_no_box_answers(self):
        bus = self._bus(FakeUnit("CFS_1", 1))
        assert bus.discover() == []
        assert bus.box.keys() == [(P.BROADCAST_BOXES, P.CMD_GET_SLAVE_INFO, b"\xfe\xfe")]
        assert bus.box.sent[0].kwargs == {"status": P.REQ_STATUS_ADDRESSING, "timeout": 1.5,
                                          "retries": 1}

    def test_reply_too_short(self):
        bus = self._bus(FakeUnit("CFS_1", 1))
        bus.box.reply(P.CMD_GET_SLAVE_INFO, frame(P.CMD_GET_SLAVE_INFO, b"\x00" * 13))
        assert bus.discover() == []
        assert bus.box.fns() == [P.CMD_GET_SLAVE_INFO]

    def test_box_by_uid(self):
        bus = self._bus(FakeUnit("CFS_1", 1, UID_A), FakeUnit("CFS_2", 2, UID_B))
        bus.box.reply(P.CMD_GET_SLAVE_INFO, slave_info(UID_B), slave_info(UID_A))
        assert bus.discover(timeout=0.5) == [(UID_B, 2), (UID_A, 1)]
        assert bus.box.keys() == [
            (P.BROADCAST_BOXES, P.CMD_GET_SLAVE_INFO, b"\xfe\xfe"),
            (P.BROADCAST_BOXES, P.CMD_SET_SLAVE_ADDR, b"\x02" + UID_B),
            (P.BROADCAST_BOXES, P.CMD_GET_SLAVE_INFO, b"\xfe\xfe"),
            (P.BROADCAST_BOXES, P.CMD_SET_SLAVE_ADDR, b"\x01" + UID_A),
            (P.BROADCAST_BOXES, P.CMD_GET_SLAVE_INFO, b"\xfe\xfe"),
        ]
        assert bus.box.sent[1].kwargs == {"status": P.REQ_STATUS_ADDRESSING, "timeout": 0.5}
        assert bus.logger.messages == [
            ("debug", f"CFS bus /dev/a: box uid {UID_B.hex()} -> address 2 (CFS_2)"),
            ("debug", f"CFS bus /dev/a: box uid {UID_A.hex()} -> address 1 (CFS_1)"),
        ]

    def test_single_box_without_uid(self):
        bus = self._bus(FakeUnit("CFS_1", 3))
        bus.box.reply(P.CMD_GET_SLAVE_INFO, slave_info(UID_A))
        assert bus.discover() == [(UID_A, 3)]

    def test_free_units_used_in_order(self):
        bus = self._bus(FakeUnit("CFS_1", 1), FakeUnit("CFS_2", 2))
        bus.box.reply(P.CMD_GET_SLAVE_INFO, slave_info(UID_A), slave_info(UID_B))
        assert bus.discover() == [(UID_A, 1), (UID_B, 2)]

    def test_unknown_box_parked_at_spare_address(self):
        bus = self._bus(FakeUnit("CFS_1", 1, UID_A))
        bus.box.reply(P.CMD_GET_SLAVE_INFO, slave_info(UID_B))
        assert bus.discover() == [(UID_B, 2)]
        assert bus.logger.messages == [
            ("warning", f"CFS bus /dev/a: box uid {UID_B.hex()} isn't in any [AFC_CFS] section "
                        f"(box_uid); parked at address 2"),
            ("debug", f"CFS bus /dev/a: box uid {UID_B.hex()} -> address 2"),
        ]

    def test_no_spare_address(self):
        units = [FakeUnit(f"CFS_{a}", a, bytes([a]) * 12) for a in (1, 2, 3, 4)]
        bus = self._bus(*units)
        bus.box.reply(P.CMD_GET_SLAVE_INFO, slave_info(UID_B))
        assert bus.discover() == []
        assert bus.logger.messages == [
            ("warning", f"CFS bus /dev/a: no free address for box uid {UID_B.hex()}")]
        assert bus.box.fns() == [P.CMD_GET_SLAVE_INFO]

    def test_at_most_four_boxes(self):
        bus = self._bus()
        uids = [bytes([i]) * 12 for i in range(1, 6)]
        bus.box.reply(P.CMD_GET_SLAVE_INFO, *[slave_info(u) for u in uids])
        assigned = bus.discover()
        assert [a for _, a in assigned] == [1, 2, 3, 4]
        assert bus.box.fns().count(P.CMD_GET_SLAVE_INFO) == 4


# set_lane_sensors

class Lane:
    def __init__(self, index):
        self.index = index
        self.prep_state = None
        self.loaded_to_hub = None
        self._load_state = None


class TestSetLaneSensors:
    def test_present_and_in_the_tube(self):
        lane = Lane(2)
        P.set_lane_sensors(lane, True, 0b0010)
        assert (lane.prep_state, lane.loaded_to_hub, lane._load_state) == (True, True, True)

    def test_present_not_in_the_tube(self):
        lane = Lane(2)
        P.set_lane_sensors(lane, True, 0b0001)
        assert (lane.prep_state, lane.loaded_to_hub, lane._load_state) == (True, True, False)

    def test_empty(self):
        lane = Lane(1)
        P.set_lane_sensors(lane, False, 0)
        assert (lane.prep_state, lane.loaded_to_hub, lane._load_state) == (False, False, False)

    def test_connections_unknown(self):
        lane = Lane(1)
        P.set_lane_sensors(lane, True, None)
        assert lane._load_state is False
