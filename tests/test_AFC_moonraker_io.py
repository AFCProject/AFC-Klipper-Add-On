"""Real-worker persistence contracts, using only an event-controlled loopback server."""

from __future__ import annotations

import json
import logging
import re
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from unittest.mock import patch

import pytest

from extras.AFC_stats import AFCStats_var
from extras.AFC_utils import AFC_moonraker
from tests.conftest import MockLogger, MockReactor


WAIT = 5


class EventCompletion:
    """Bounded unit bridge. Real cooperative scheduling is tested by the smoke script."""

    def __init__(self):
        self.event = threading.Event()
        self.result = None

    def complete(self, result):
        assert not self.event.is_set(), "completion delivered twice"
        self.result = result
        self.event.set()

    def test(self):
        return self.event.is_set()

    def wait(self):
        assert self.event.wait(WAIT), "worker did not settle result"
        return self.result


class DeferredReactor(MockReactor):
    def __init__(self):
        super().__init__()
        self.callbacks = Queue()

    def completion(self):
        return EventCompletion()

    def async_complete(self, completion, result):
        completion.complete(result)

    def register_async_callback(self, callback, waketime=None):
        self.callbacks.put(callback)

    def register_callback(self, callback, waketime=None):
        self.callbacks.put(callback)

    def run_callbacks(self):
        while True:
            try:
                callback = self.callbacks.get_nowait()
            except Empty:
                return
            callback(0.0)


class RecordHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname.lower(), record.getMessage(), record.thread))


class ThreadLogger(MockLogger):
    def __init__(self):
        super().__init__()
        self.threads = []
        self.details = []
        self.handler = RecordHandler()
        self.logger = logging.Logger("afc-loopback-test", logging.DEBUG)
        self.logger.addHandler(self.handler)

    def _record(self, level, message, kwargs):
        self.messages.append((level, message))
        self.threads.append(threading.get_ident())
        self.details.append(kwargs)

    def debug(self, message, **kwargs):
        self._record("debug", message, kwargs)

    def info(self, message, **kwargs):
        self._record("info", message, kwargs)

    def error(self, message, **kwargs):
        self._record("error", message, kwargs)


class LoopbackIO:
    def __init__(self):
        self.records = []
        self.requests = []
        self.contents = {}
        self.statuses = {}
        self.responses = {}
        self.raw_responses = {}
        self.gates = {}
        self.escaped = []
        self.condition = threading.Condition()
        self.reactor = DeferredReactor()
        self.logger = ThreadLogger()
        # get_afc_stats returns None while Moonraker holds no afc_stats namespace
        self.startup_values = None
        self.startup_reads = 0
        self.workers = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self._respond()

            def do_DELETE(self):
                self._respond()

            def do_GET(self):
                self._respond()

            def log_message(self, *args):
                pass

            def _respond(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                if self.headers.get("Content-Type") == "application/json":
                    form = {}
                    data = json.loads(body) if body else {}
                else:
                    form = parse_qs(body)
                    data = None
                namespace = form.get("namespace", [""])[0]
                key = form.get("key", [""])[0]
                value = form.get("value", [None])[0]
                with owner.condition:
                    index = len(owner.records)
                    owner.records.append((self.command, namespace, key, value))
                    owner.requests.append((self.path, form, data))
                    owner.condition.notify_all()
                gate = owner.gates.get(index)
                if gate is not None:
                    if not gate.wait(WAIT / 2):
                        owner.escaped.append(index)
                status = owner.statuses.get(index, 200)
                if status == 200:
                    if self.command == "POST":
                        owner.contents[(namespace, key)] = value
                    elif self.command == "DELETE":
                        owner.contents.pop((namespace, key), None)
                if index in owner.responses:
                    result = owner.responses[index]
                elif (self.command == "GET"
                      and parse_qs(urlsplit(self.path).query).get("namespace") == ["afc_stats"]):
                    result = {"namespace": "afc_stats", "value": owner.stored_stats()}
                else:
                    result = {"value": value}
                payload = owner.raw_responses.get(
                    index, json.dumps({"result": result}).encode())
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.client = self.new_client()
        self.logger.messages.clear()
        self.logger.threads.clear()
        self.logger.details.clear()

    def new_client(self):
        client = AFC_moonraker("http://127.0.0.1", str(self.server.server_port),
                              self.logger, self.reactor)
        self.workers.append(client)
        return client

    def startup_stats(self):
        self.startup_reads += 1
        return self.startup_values

    def stored_stats(self):
        """Nest the stored afc_stats keys as Moonraker returns the namespace."""
        stats = {}
        for (namespace, key), value in self.contents.items():
            if namespace == "afc_stats":
                *parents, name = key.split(".")
                node = stats
                for parent in parents:
                    node = node.setdefault(parent, {})
                node[name] = value
        return stats

    def external_edit(self, key, value):
        """Change a stored statistic over HTTP as another Moonraker client would."""
        body = urlencode({"namespace": "afc_stats", "key": key, "value": value}).encode()
        with urlopen(Request(self.client.database_url, body), timeout=WAIT):
            pass

    def wait_requests(self, count):
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.records) >= count, timeout=WAIT)

    def wait_idle(self):
        completed = threading.Event()
        self.client._write_queue.put_nowait((completed.set, ()))
        assert completed.wait(WAIT), "worker did not reach the completion barrier"

    def close(self):
        for gate in self.gates.values():
            gate.set()
        for client in self.workers:
            client.join_thread()
            assert not client._write_thread.is_alive()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(WAIT)
        assert not self.server_thread.is_alive()


@pytest.fixture
def moonraker_io():
    io = LoopbackIO()
    try:
        yield io
    finally:
        io.close()


@contextmanager
def joining(client):
    """Expose the point where shutdown admission has finished, without timing sleeps."""
    entered = threading.Event()
    completed = threading.Event()
    errors = []
    original_join = client._write_thread.join

    def observed_join(*args, **kwargs):
        entered.set()
        return original_join(*args, **kwargs)

    def close():
        try:
            client.join_thread()
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    client._write_thread.join = observed_join
    helper = threading.Thread(target=close)
    helper.start()
    try:
        assert entered.wait(WAIT)
        yield completed
    finally:
        # The caller must release its HTTP gate before this context exits.
        helper.join(WAIT + 1)
        client._write_thread.join = original_join
        assert not helper.is_alive()
        assert errors == []


class TestAFCMoonrakerUpdateAfcStats:
    def test_increments_and_reset_return_before_response(self, moonraker_io):
        io = moonraker_io
        gate = io.gates[0] = threading.Event()
        stat = AFCStats_var("lane1", "load_count", None, io.client)
        stat.increase_count()
        io.wait_requests(1)
        stat.increase_count()
        stat.reset_count()
        stat.increase_count()
        try:
            assert not gate.is_set()
            assert io.escaped == []
            assert stat.value == 1
        finally:
            gate.set()
        io.wait_idle()
        assert io.records == [
            ("POST", "afc_stats", "lane1.load_count", "1"),
            ("POST", "afc_stats", "lane1.load_count", "2"),
            ("POST", "afc_stats", "lane1.load_count", "0"),
            ("POST", "afc_stats", "lane1.load_count", "1"),
        ]
        assert io.contents == {("afc_stats", "lane1.load_count"): "1"}
        assert io.logger.messages == []


class TestAFCMoonrakerRemoveDatabaseEntry:
    def test_migration_precedes_increment_and_removes_legacy_key(self, moonraker_io):
        io = moonraker_io
        io.contents[("afc_stats", "legacy.count")] = "7"
        stat = AFCStats_var("legacy", "count", {"legacy": {"count": 7}},
                            io.client, "lane1")
        stat.increase_count()
        io.wait_idle()
        io.reactor.run_callbacks()
        assert stat.value == 8
        assert io.records == [
            ("POST", "afc_stats", "lane1.count", "7"),
            ("DELETE", "afc_stats", "legacy.count", None),
            ("POST", "afc_stats", "lane1.count", "8"),
        ]
        assert io.contents == {("afc_stats", "lane1.count"): "8"}
        assert io.logger.messages == [("debug", "Removing legacy.count from afc_stats")]
        assert io.logger.threads == [threading.get_ident()]


class TestAFCMoonrakerWriteWorker:
    @pytest.mark.parametrize("submit, failure_message", [
        pytest.param(lambda client: client.update_afc_stats("failed", 1),
                     "Error when trying to update failed in moonraker, see AFC.log for more info",
                     id="stats"),
        pytest.param(lambda client: client.send_lane_data({"lane1": {"color": "red"}}),
                     "Error sending lane data, check AFC.log for more information",
                     id="lane-data"),
    ])
    def test_http_error_does_not_stop_later_persistence(self, moonraker_io, submit,
                                                        failure_message):
        io = moonraker_io
        io.statuses[0] = 500
        submit(io.client)
        io.client.update_afc_stats("later", 2)
        io.wait_idle()
        assert io.contents == {("afc_stats", "later"): "2"}
        assert io.logger.messages == []
        io.reactor.run_callbacks()
        assert io.logger.messages == [
            ("error", io.client.ERROR_STRING),
            ("error", failure_message),
        ]
        assert "HTTP Error 500" in io.logger.details[0]["traceback"]
        assert io.logger.threads == [threading.get_ident(), threading.get_ident()]


class TestAFCMoonrakerJoinThread:
    def test_disconnect_attempts_all_admitted_operations_in_order(self, moonraker_io):
        io = moonraker_io
        gate = io.gates[0] = threading.Event()
        io.client.update_afc_stats("first", 1)
        io.wait_requests(1)
        io.client.update_afc_stats("second", 2)
        io.client.remove_database_entry("afc_stats", "legacy.count")
        io.client.update_afc_stats("last", 3)
        with joining(io.client) as completed:
            try:
                assert not completed.is_set()
            finally:
                gate.set()
        assert completed.is_set()
        assert io.records == [
            ("POST", "afc_stats", "first", "1"),
            ("POST", "afc_stats", "second", "2"),
            ("DELETE", "afc_stats", "legacy.count", None),
            ("POST", "afc_stats", "last", "3"),
        ]
        assert io.contents == {
            ("afc_stats", "first"): "1",
            ("afc_stats", "second"): "2",
            ("afc_stats", "last"): "3",
        }

    def test_expired_budget_skips_only_operations_not_yet_started(self, moonraker_io):
        io = moonraker_io
        first_gate = io.gates[0] = threading.Event()
        second_gate = io.gates[1] = threading.Event()
        io.client.update_afc_stats("first", 1)
        io.wait_requests(1)
        io.client.update_afc_stats("second", 2)
        io.client.remove_database_entry("afc_stats", "third")
        io.client.update_afc_stats("fourth", 4)
        # Idle time before disconnect must not consume the shutdown budget
        io.reactor._monotonic = 1000.0
        try:
            with joining(io.client):
                # The second update starts just inside the 15 s budget
                io.reactor._monotonic = 1014.999
                first_gate.set()
                io.wait_requests(2)
                # At the budget while the second update is still in flight
                io.reactor._monotonic = 1015.0
                # A repeated disconnect must not restart the budget
                with joining(io.client):
                    second_gate.set()
        finally:
            first_gate.set()
            second_gate.set()
        assert io.records == [
            ("POST", "afc_stats", "first", "1"),
            ("POST", "afc_stats", "second", "2"),
        ]
        assert io.contents == {
            ("afc_stats", "first"): "1",
            ("afc_stats", "second"): "2",
        }
        assert io.escaped == []
        assert io.logger.messages == []
        [(_, message, _)] = io.logger.handler.records
        assert re.search(r"\b2\b", message)


class TestAFCMoonrakerEnqueue:
    @pytest.mark.parametrize("producer", [
        lambda client: client.remove_database_entry("afc_stats", "key"),
        lambda client: client.get_file_metadata("file.gcode", lambda result: None),
        lambda client: client.get_spool(1, lambda result: None),
        lambda client: client.get_td1_data_async(lambda result: None),
        lambda client: client.send_lane_data({"lane1": {}}),
        lambda client: client.get_td1_data(),
    ])
    def test_closed_worker_rejects_every_producer(self, moonraker_io, producer):
        io = moonraker_io
        io.client.join_thread()
        with pytest.raises(RuntimeError, match="^AFC Moonraker worker is closed$"):
            producer(io.client)
        assert io.records == []


class TestAFCMoonrakerDrainLogs:
    def test_shutdown_preserves_diagnostics_without_ui_or_duplicates(self, moonraker_io):
        io = moonraker_io
        gate = io.gates[0] = threading.Event()
        io.statuses[0] = 500
        io.client._log_async(io.logger.info, "pending info")
        io.client._log_async(io.logger.debug, "pending debug", traceback="supplied traceback")
        io.client.update_afc_stats("failed", 1)
        io.wait_requests(1)
        io.client.update_afc_stats("later", 2)
        with joining(io.client):
            gate.set()
        assert io.logger.messages == []
        assert io.records == [
            ("POST", "afc_stats", "failed", "1"),
            ("POST", "afc_stats", "later", "2"),
        ]
        assert io.contents == {("afc_stats", "later"): "2"}
        records = io.logger.handler.records
        assert [level for level, _, _ in records] == ["info", "debug", "error", "error"]
        assert records[0][1] == "pending info"
        assert "pending debug" in records[1][1]
        assert "supplied traceback" in records[1][1]
        assert io.client.ERROR_STRING in records[2][1]
        assert "HTTP Error 500" in records[2][1]
        assert records[3][1] == (
            "Error when trying to update failed in moonraker, see AFC.log for more info")
        assert len({thread for _, _, thread in records}) == 1
        before = list(records)
        io.reactor.run_callbacks()
        io.client.join_thread()
        assert io.logger.messages == []
        assert records == before


class TestAFCMoonrakerGetResults:
    def test_post_timeout_reports_error_and_next_request_persists(self, moonraker_io):
        io = moonraker_io
        io.client.REQUEST_TIMEOUT = 0.05
        gate = io.gates[0] = threading.Event()
        io.client.update_afc_stats("stalled", 1)
        io.wait_requests(1)
        io.client.update_afc_stats("later", 2)
        try:
            io.wait_idle()
            assert io.contents == {("afc_stats", "later"): "2"}
            assert io.escaped == []
        finally:
            gate.set()
        assert io.logger.messages == []
        io.reactor.run_callbacks()
        assert io.logger.messages == [
            ("error", io.client.ERROR_STRING),
            ("error", "Error when trying to update stalled in moonraker, see AFC.log for more info"),
        ]
        assert "timed out" in io.logger.details[0]["traceback"]
        assert io.logger.threads == [threading.get_ident(), threading.get_ident()]


class TestAFCMoonrakerRemoveDatabaseEntrySync:
    @pytest.mark.parametrize("failure, leading, detail", [
        # The timeout escapes the DELETE helper's HTTPError handler into the worker catch
        pytest.param("timeout",
                     [("error", "Unexpected error in moonraker background writer")],
                     "timed out", id="timeout"),
        pytest.param("http-error",
                     [("debug", "Error occurred when trying to delete failed from "
                                "afc_stats namespace")],
                     "HTTP Error 500", id="http-error"),
    ])
    def test_delete_failure_is_contained_and_later_write_persists(self, moonraker_io, failure,
                                                                  leading, detail):
        io = moonraker_io
        gate = threading.Event()
        if failure == "timeout":
            io.client.REQUEST_TIMEOUT = 0.05
            io.gates[0] = gate
        else:
            io.statuses[0] = 500
        io.client.remove_database_entry("afc_stats", "failed")
        io.client.update_afc_stats("later", 2)
        try:
            io.wait_idle()
            assert io.contents == {("afc_stats", "later"): "2"}
            assert io.escaped == []
        finally:
            gate.set()
        assert io.logger.messages == []
        io.reactor.run_callbacks()
        *messages, (level, message) = io.logger.messages
        assert messages == leading
        assert level == "debug"
        assert detail in message
        assert io.logger.threads == [threading.get_ident(), threading.get_ident()]


class TestAFCMoonrakerResultBridge:
    def test_undecodable_body_settles_callback_and_later_work(self, moonraker_io):
        io = moonraker_io
        io.raw_responses[0] = b"not-json"
        results = []
        io.client.get_file_metadata("bad.gcode", results.append)
        io.client.update_afc_stats("after", 2)
        io.wait_idle()
        io.reactor.run_callbacks()
        assert results == [None]
        assert io.contents[("afc_stats", "after")] == "2"
        assert io.logger.messages == [("error", io.client.ERROR_STRING)]

    def test_worker_exception_is_raised_to_waiter_and_queue_continues(self, moonraker_io):
        io = moonraker_io

        def fail():
            raise ValueError("worker failure")

        with pytest.raises(ValueError, match="^worker failure$"):
            io.client._call_worker(fail, ())
        io.client.update_afc_stats("after", 3)
        io.wait_idle()
        assert io.contents == {("afc_stats", "after"): "3"}
        assert io.logger.messages == []

    @pytest.mark.parametrize("ready", [
        pytest.param({"ready": True}, id="mapping"),
        # wait_for_moonraker treats any non-None result as connected
        pytest.param({}, id="empty-mapping"),
        pytest.param(None, id="null"),
    ])
    def test_mixed_reads_and_writes_share_fifo_without_self_wait(self, moonraker_io, ready):
        io = moonraker_io
        io.responses[2] = {"filament_change_count": 3}
        io.responses[3] = {"id": 42}
        io.responses[4] = {"devices": {"serial": {"color": "red"}}}
        io.responses[5] = ready
        callbacks = []
        workers = []

        def observed_open(*args, **kwargs):
            workers.append(threading.get_ident())
            return urlopen(*args, **kwargs)

        with patch("extras.AFC_utils.urlopen", side_effect=observed_open):
            io.client.update_afc_stats("first", 1)
            io.client.remove_database_entry("afc_stats", "old")
            io.client.get_file_metadata("file.gcode",
                                        lambda value: callbacks.append(("meta", value)))
            io.client.get_spool(42, lambda value: callbacks.append(("spool", value)))
            io.client.get_td1_data_async(lambda value: callbacks.append(("td1", value)))
            assert io.client._get_results(io.client.host + "/server/info") == ready
            io.client.send_lane_data({"lane": "red"})
            io.wait_idle()
        io.reactor.run_callbacks()
        assert [request[0] for request in io.requests] == [
            "/server/database/item", "/server/database/item",
            "/server/files/metadata?filename=file.gcode", "/server/spoolman/proxy",
            "/machine/td1/data", "/server/info", "/server/database/item",
        ]
        assert workers == [io.client._write_thread.ident] * 7
        assert callbacks == [
            ("meta", {"filament_change_count": 3}), ("spool", {"id": 42}),
            ("td1", {"serial": {"color": "red"}}),
        ]
        assert io.logger.messages == [("debug", "Removing old from afc_stats")]


class TestAFCMoonrakerCallbackReads:
    @pytest.mark.parametrize(
        "submit, status, response, delivered, expected_request, messages", [
        pytest.param(
            lambda client, callback: client.get_file_metadata("sub dir/file.gcode", callback),
            200, {"filament_change_count": 3}, {"filament_change_count": 3},
            ("/server/files/metadata?filename=sub%20dir/file.gcode", {}, None), [],
            id="metadata"),
        pytest.param(
            lambda client, callback: client.get_spool(42, callback),
            200, {"id": 42, "filament": {"color_hex": "ff0000"}},
            {"id": 42, "filament": {"color_hex": "ff0000"}},
            ("/server/spoolman/proxy",
             {"request_method": ["GET"], "path": ["/v1/spool/42"]}, None), [],
            id="spool"),
        pytest.param(
            lambda client, callback: client.get_spool(42, callback),
            500, None, None,
            ("/server/spoolman/proxy",
             {"request_method": ["GET"], "path": ["/v1/spool/42"]}, None),
            [("error", AFC_moonraker.ERROR_STRING), ("info", "SpoolID: 42 not found")],
            id="spool-http-error"),
        # The consumer receives the devices map, not the enclosing TD-1 response
        pytest.param(
            lambda client, callback: client.get_td1_data_async(callback),
            200, {"devices": {"serial1": {"color": "red"}}}, {"serial1": {"color": "red"}},
            ("/machine/td1/data", {}, None), [],
            id="td1"),
        pytest.param(
            lambda client, callback: client.get_td1_data_async(callback),
            200, {"other": "data"}, None,
            ("/machine/td1/data", {}, None), [],
            id="td1-missing-devices"),
        pytest.param(
            lambda client, callback: client.get_td1_data_async(callback),
            500, None, None,
            ("/machine/td1/data", {}, None), [("error", AFC_moonraker.ERROR_STRING)],
            id="td1-http-error"),
    ])
    def test_result_is_delivered_only_on_reactor_thread(self, moonraker_io, submit, status,
                                                        response, delivered,
                                                        expected_request,
                                                        messages):
        io = moonraker_io
        io.statuses[0] = status
        io.responses[0] = response
        results = []
        submit(io.client, lambda data: results.append((data, threading.get_ident())))
        io.wait_idle()
        assert results == []
        assert io.logger.messages == []
        io.reactor.run_callbacks()
        assert results == [(delivered, threading.get_ident())]
        assert io.requests == [expected_request]
        assert io.logger.messages == messages

    @pytest.mark.parametrize("submit", [
        pytest.param(lambda client, callback: client.get_file_metadata("old.gcode", callback),
                     id="metadata"),
        pytest.param(lambda client, callback: client.get_spool(42, callback), id="spool"),
        pytest.param(lambda client, callback: client.get_td1_data_async(callback), id="td1"),
    ])
    @pytest.mark.parametrize("close_during_http", [
        pytest.param(True, id="close-during-http"),
        pytest.param(False, id="close-after-delivery-scheduled"),
    ])
    def test_no_delivery_after_close(self, moonraker_io, submit, close_during_http):
        io = moonraker_io
        results = []
        if close_during_http:
            gate = io.gates[0] = threading.Event()
            submit(io.client, results.append)
            io.wait_requests(1)
            with joining(io.client):
                gate.set()
            # The helper's own closing check must not register delivery during shutdown
            assert io.reactor.callbacks.empty()
        else:
            submit(io.client, results.append)
            io.wait_idle()
            assert not io.reactor.callbacks.empty()
            io.client.join_thread()
        io.reactor.run_callbacks()
        assert results == []
        assert len(io.records) == 1


class TestAFCMoonrakerCleanupPublicationOrder:
    def test_cleanup_deletes_precede_lane_publication(self, moonraker_io):
        io = moonraker_io
        io.responses[0] = {"value": {"old1": {}, "old2": {}}}
        gate = io.gates[1] = threading.Event()
        io.client.delete_lane_data()
        io.wait_requests(2)
        io.client.send_lane_data({"lane1": {"color": "red"}})
        try:
            assert len(io.requests) == 2
            assert io.escaped == []
        finally:
            gate.set()
        io.wait_idle()
        assert io.records == [
            ("GET", "", "", None),
            ("DELETE", "lane_data", "old1", None),
            ("DELETE", "lane_data", "old2", None),
            ("POST", "", "", None),
        ]
        assert io.requests[-1][2] == {"lane1": {"color": "red"}}
        io.reactor.run_callbacks()
        assert io.logger.messages == [
            ("debug", "Removing old1 from lane_data"),
            ("debug", "Removing old2 from lane_data"),
        ]
