"""Unit tests for extras/AFC_CFS.py (Creality CFS).

Built on the fakes and real-construction builders in tests/cfs_helpers.py.
The protocol frame vectors are real traffic: the first group was captured with
interceptty from a stock printer talking to a CFS (via ityshchenko/klipper-cfs),
the rest live by tomaspakosta/k1c-cfs-klipper. Expected bytes and CRCs come
from those captures, not from the code under test.
"""
from __future__ import annotations

import configparser
import errno
import importlib.util
import json
import os
import pathlib
import queue
import struct
import sys
import termios
import threading
from unittest.mock import MagicMock

import jinja2
import pytest

from extras.AFC_lane import AFCLaneState, AFCMoveWarning
from tests.conftest import MockConfig, MockLogger
from extras import AFC_CFS as P
from extras.AFC_CFS import (
    CFSBus,
    CFSTrace,
    CFSTransport,
    afcCFS,
    load_config_prefix,
)
from tests.cfs_helpers import (
    CommandError,
    FakeMutex,
    RecTrace,
    Recorder,
    SERIAL,
    ScriptedBox,
    connect,
    frame,
    gcmd,
    make_lane,
    make_printer,
    make_unit,
    reset_buses,
)


# ============================================================================
# Wire protocol: frames, CRC, field decoding
# ============================================================================

CAPTURED = [
    (0x01, 0x00, 0xA3, b"", "f7010300a3dd"),
    (0x02, 0x00, 0xA3, b"", "f7020300a3dd"),
    (0x04, 0x00, 0xA3, b"", "f7040300a3dd"),
    (0x01, 0xFF, 0x04, bytes([0x00, 0x01]), "f70105ff04000190"),
    (0x01, 0xFF, 0x14, b"", "f70103ff1406"),
    (0x01, 0xFF, 0x0D, bytes([0x0F, 0x01]), "f70105ff0d0f0169"),
    (0x01, 0xFF, 0x0A, b"", "f70103ff0a5c"),
    (0xFE, 0x00, 0xA1, bytes([0xFE, 0xFE]), "f7fe0500a1fefef8"),
    (0x01, 0x00, 0xA2, b"", "f7010300a2da"),
    (0x01, 0xFF, 0x07, bytes([0x01]), "f70104ff07011f"),
    (0x01, 0xFF, 0x07, bytes([0x00]), "f70104ff070018"),
    (0x01, 0xFF, 0x0F, bytes([0x01]), "f70104ff0f01b7"),
    (0x01, 0xFF, 0x11, bytes([0x01, 0x00]), "f70105ff110100e0"),
]


# A real tag written for Generic PLA-Silk, pink, read from a K1 Max box
PINK_SILK = "AB1240276A21000020E7A18E0330000001000000"


# Frame

class TestFrameOk:
    def test_ok_status(self):
        assert P.Frame(1, P.ST_OK, P.GET_BOX_STATE, b"").ok is True

    def test_error_status(self):
        assert P.Frame(1, P.ST_STATE_ERR, P.GET_BOX_STATE, b"").ok is False


class TestFrameStatusName:
    def test_names_its_status(self):
        assert P.Frame(1, P.ST_EXTRUDE_ERR8, P.EXTRUDE_PROCESS, b"").status_name() == "EXTRUDE_ERR8"


class TestFrameHex:
    @pytest.mark.parametrize("addr,status,fn,data,expected", CAPTURED)
    def test_matches_captured_bytes(self, addr, status, fn, data, expected):
        assert P.Frame(addr, status, fn, data).hex() == expected


# Module functions

class TestStatusName:
    def test_no_reply(self):
        assert P.status_name(None) == "NO_REPLY"

    def test_known_code(self):
        assert P.status_name(0x0C) == "EXTRUDE_ERR8"

    def test_unknown_code_is_hex(self):
        assert P.status_name(0x7E) == "0x7E"


class TestFnName:
    def test_known_code(self):
        assert P.fn_name(0x10) == "EXTRUDE_PROCESS"

    def test_unknown_code_is_hex(self):
        assert P.fn_name(0x5B) == "0x5B"


class TestCrc8:
    def test_empty_data(self):
        assert P.crc8(b"") == 0x00

    @pytest.mark.parametrize("addr,status,fn,data,expected", CAPTURED)
    def test_matches_captured_crc(self, addr, status, fn, data, expected):
        raw = bytes.fromhex(expected)
        # CRC covers len..data; the captured last byte is the box's own CRC
        assert P.crc8(raw[2:-1]) == raw[-1]

    def test_reference_check_value(self):
        # Standard CRC-8 (poly 0x07, init 0, no reflect, no xorout) check value
        assert P.crc8(b"123456789") == 0xF4


class TestEncodeFrame:
    @pytest.mark.parametrize("addr,status,fn,data,expected", CAPTURED)
    def test_matches_captured_traffic(self, addr, status, fn, data, expected):
        assert P.encode_frame(addr, status, fn, data).hex() == expected

    def test_bytes_over_255_are_truncated(self):
        raw = P.encode_frame(0x101, 0x1FF, 0x10A)
        assert raw[:5] == bytes([0xF7, 0x01, 0x03, 0xFF, 0x0A])

    def test_accepts_a_list_payload(self):
        assert P.encode_frame(1, 0xFF, 0x04, [0x00, 0x01]).hex() == "f70105ff04000190"


# FrameParser

class TestFrameParserInit:
    def test_starts_empty(self):
        parser = P.FrameParser()
        assert bytes(parser._buf) == b""
        assert parser.crc_errors == 0
        assert parser.discarded_bytes == 0


class TestFrameParserReset:
    def test_drops_a_partial_frame(self):
        parser = P.FrameParser()
        parser.feed(bytes.fromhex("f70105ff04"))
        parser.reset()
        assert bytes(parser._buf) == b""
        # The rest of the old frame no longer completes anything
        assert parser.feed(bytes.fromhex("000190")) == []


class TestFrameParserFeed:
    @pytest.mark.parametrize("addr,status,fn,data,expected", CAPTURED)
    def test_round_trips_captured_traffic(self, addr, status, fn, data, expected):
        parser = P.FrameParser()
        assert parser.feed(bytes.fromhex(expected)) == [P.Frame(addr, status, fn, data)]
        assert bytes(parser._buf) == b""
        assert parser.discarded_bytes == 0

    def test_two_frames_in_one_chunk(self):
        frames = P.FrameParser().feed(bytes.fromhex("f70105ff04000190f70103ff1406"))
        assert [f.fn for f in frames] == [0x04, 0x14]

    def test_split_one_byte_at_a_time(self):
        parser = P.FrameParser()
        raw = bytes.fromhex("f70105ff04000190")
        frames = []
        for i in range(len(raw)):
            frames += parser.feed(raw[i:i + 1])
        assert frames == [P.Frame(1, 0xFF, 0x04, b"\x00\x01")]

    def test_no_frame_head_discards_everything(self):
        parser = P.FrameParser()
        assert parser.feed(b"\x01\x02\x03") == []
        assert bytes(parser._buf) == b""
        assert parser.discarded_bytes == 3

    def test_garbage_before_a_frame_is_skipped(self):
        parser = P.FrameParser()
        assert parser.feed(b"\x00\x11" + bytes.fromhex("f70103ff1406")) == [
            P.Frame(1, 0xFF, 0x14, b"")]
        assert parser.discarded_bytes == 2

    def test_header_not_complete_waits(self):
        parser = P.FrameParser()
        assert parser.feed(b"\xf7\x01") == []
        assert bytes(parser._buf) == b"\xf7\x01"
        assert parser.discarded_bytes == 0

    def test_length_below_three_is_not_a_header(self):
        parser = P.FrameParser()
        good = bytes.fromhex("f70103ff1406")
        assert parser.feed(b"\xf7\x01\x02" + good) == [P.Frame(1, 0xFF, 0x14, b"")]
        # The bad 0xF7, then the 0x01 0x02 before the next head
        assert parser.discarded_bytes == 3

    def test_length_above_max_is_not_a_header(self):
        parser = P.FrameParser()
        good = bytes.fromhex("f70103ff1406")
        assert parser.feed(b"\xf7\x01\x81" + good) == [P.Frame(1, 0xFF, 0x14, b"")]
        assert parser.discarded_bytes == 3

    def test_length_at_max_is_a_header(self):
        parser = P.FrameParser()
        # Length 0x80 is accepted as a header and waits for the rest
        assert parser.feed(b"\xf7\x01\x80\xff") == []
        assert bytes(parser._buf) == b"\xf7\x01\x80\xff"

    def test_frame_not_complete_waits(self):
        parser = P.FrameParser()
        assert parser.feed(bytes.fromhex("f70105ff0400")) == []
        assert bytes(parser._buf) == bytes.fromhex("f70105ff0400")

    def test_bad_crc_is_counted_and_skipped(self):
        parser = P.FrameParser()
        bad = bytes.fromhex("f70103ff1407")          # captured frame, CRC off by one
        good = bytes.fromhex("f70103ff0a5c")
        assert parser.feed(bad + good) == [P.Frame(1, 0xFF, 0x0A, b"")]
        assert parser.crc_errors == 1
        # The bad frame's 0xF7, then its remaining 5 bytes before the next head
        assert parser.discarded_bytes == 6


# Payload helpers

class TestDecodeFloatBe:
    def test_hardware_capture(self):
        # Stage-5 telemetry captured when filament reached the toolhead sensor
        assert P.decode_float_be(bytes.fromhex("c5afa071")) == pytest.approx(-5620.055, abs=1e-3)

    def test_extra_bytes_ignored(self):
        assert P.decode_float_be(struct.pack(">f", 12.5) + b"\x99") == 12.5

    def test_too_short(self):
        assert P.decode_float_be(b"\x00\x00\x00") is None


class TestSlotBit:
    @pytest.mark.parametrize("index,bit", [(1, 0x01), (2, 0x02), (3, 0x04), (4, 0x08)])
    def test_valid_index(self, index, bit):
        assert P.slot_bit(index) == bit

    def test_below_range(self):
        with pytest.raises(ValueError) as e:
            P.slot_bit(0)
        assert str(e.value) == "CFS slot index must be 1-4, got 0"

    def test_above_range(self):
        with pytest.raises(ValueError) as e:
            P.slot_bit(5)
        assert str(e.value) == "CFS slot index must be 1-4, got 5"


class TestSlotLetter:
    def test_letters(self):
        assert [P.slot_letter(i) for i in (1, 2, 3, 4)] == ["A", "B", "C", "D"]


class TestBitsToSlots:
    def test_some_slots(self):
        assert P.bits_to_slots(0b1010) == [2, 4]

    def test_no_slots(self):
        assert P.bits_to_slots(0) == []

    def test_higher_bits_ignored(self):
        assert P.bits_to_slots(0xF1) == [1]


class TestDecodeAscii:
    def test_strips_nul_padding(self):
        assert P.decode_ascii(b"\x00113500\x00\x00") == "113500"

    def test_not_ascii(self):
        assert P.decode_ascii(b"\xc3\xa9") is None


# RFID

class TestParseRfidReply:
    def test_hardware_reply(self):
        data = (b"D:" + PINK_SILK.encode() + b";")
        assert P.parse_rfid_reply(data) == {4: PINK_SILK}

    def test_unknown_slot(self):
        assert P.parse_rfid_reply(b"B:unknown;") == {2: None}

    def test_several_slots(self):
        record = "AB1240276A21010010FFFFFF0165000001000000"
        assert P.parse_rfid_reply(f"A:unknown;C:{record};".encode()) == {1: None, 3: record}

    def test_empty_reply(self):
        assert P.parse_rfid_reply(b"") == {}

    def test_not_ascii_reply(self):
        assert P.parse_rfid_reply(b"\xffA:x;") == {}

    def test_short_record(self):
        assert P.parse_rfid_reply(b"D:short;") == {4: None}

    def test_forty_chars_but_not_alphanumeric(self):
        record = PINK_SILK[:-1] + "-"
        assert P.parse_rfid_reply(f"A:{record};".encode()) == {1: None}

    def test_part_too_short_skipped(self):
        assert P.parse_rfid_reply(b"A;B:unknown;") == {2: None}

    def test_part_without_colon_skipped(self):
        assert P.parse_rfid_reply(b"AXunknown;B:unknown;") == {2: None}

    def test_part_with_unknown_letter_skipped(self):
        assert P.parse_rfid_reply(b"E:unknown;B:unknown;") == {2: None}

    def test_whitespace_around_parts(self):
        assert P.parse_rfid_reply(b" B:unknown ; ") == {2: None}


class TestDecodeRfidRecord:
    def test_hardware_tag(self):
        assert P.decode_rfid_record(PINK_SILK) == {
            "record": PINK_SILK,
            "date": "AB124",
            "vendor": "0276",
            "batch": "A2",
            "material_code": "00002",
            "material": "PLA",
            "color": "E7A18E",
            "length_code": "0330",
            "weight": 1000,
            "serial": "000001",
            "spool_id": None,
        }

    def test_wrong_length(self):
        with pytest.raises(ValueError) as e:
            P.decode_rfid_record("too short")
        assert str(e.value) == "RFID record must be 40 characters"

    def test_unknown_material_code(self):
        record = "AB1240276A21099990123ABC0082000001000042"
        assert P.decode_rfid_record(record)["material"] is None

    def test_extra_materials_table(self):
        record = "AB1240276A21099990123ABC0082000001000042"
        assert P.decode_rfid_record(record, {"09999": "PETG"})["material"] == "PETG"

    def test_extra_table_overrides_builtin(self):
        assert P.decode_rfid_record(PINK_SILK, {"00002": "PLA-SILK"})["material"] == "PLA-SILK"

    def test_lowercase_color_is_upper_cased(self):
        record = "AB1240276A21000020abc1200330000001000000"
        assert P.decode_rfid_record(record)["color"] == "ABC120"

    def test_color_not_hex(self):
        record = "AB1240276A21000020ZZZZZZ0330000001000000"
        assert P.decode_rfid_record(record)["color"] is None

    def test_unknown_length_code(self):
        record = "AB1240276A21000020E7A18E9999000001000000"
        assert P.decode_rfid_record(record)["weight"] is None

    def test_spool_id_in_reserved(self):
        record = "AB1240276A21000020E7A18E0330000001000042"
        assert P.decode_rfid_record(record)["spool_id"] == 42

    def test_reserved_one_is_not_a_spool_id(self):
        record = "AB1240276A21000020E7A18E0330000001000001"
        assert P.decode_rfid_record(record)["spool_id"] is None

    def test_reserved_not_digits(self):
        record = "AB1240276A21000020E7A18E0330000001ABCDEF"
        assert P.decode_rfid_record(record)["spool_id"] is None


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ============================================================================
# CFSTrace, CFSTransport, CFSBus and set_lane_sensors
# ============================================================================

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

    def test_no_path_records_and_drops_nothing(self, tmp_path):
        trace = CFSTrace(None, 1024, 1, MagicMock())
        assert trace.start() is False
        trace.event("tx", fn="X")
        assert trace.written == 0 and trace.dropped == 0


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

    def test_overfull_queue_never_blocks(self, tmp_path):
        import queue
        trace = CFSTrace(str(tmp_path / "t.jsonl"), 1024 * 1024, 0, MagicMock())
        trace._queue = queue.Queue(maxsize=5)
        trace.enabled = True                                 # no writer thread: queue fills
        for _ in range(15):
            trace.event("tx")
        assert trace.dropped == 10


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

    def test_end_to_end_keeps_backups_oldest_first(self, tmp_path):
        path = tmp_path / "trace.jsonl"
        trace = CFSTrace(str(path), 2000, 2, MagicMock())
        trace.start()
        for i in range(400):
            trace.event("tx", fn="EXTRUDE_PROCESS", data="0105" * 4, i=i)
        trace.stop()
        assert os.path.exists(str(path) + ".1")
        assert not os.path.exists(str(path) + ".3")          # never more than `backups`
        files = [str(path) + ".2", str(path) + ".1", str(path)]
        files = [f for f in files if os.path.exists(f)]
        seen = [e["i"] for f in files for e in _read(f)]
        assert seen == sorted(seen)                          # backups hold the oldest events
        total = sum(os.path.getsize(f) for f in files)
        assert total < 3 * 2000 + 4000


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

    def test_end_to_end_json_lines_with_timestamps(self, tmp_path):
        path = tmp_path / "sub" / "trace.jsonl"
        trace = CFSTrace(str(path), 1024 * 1024, 1, MagicMock())
        assert trace.start()
        trace.event("tx", fn="GET_BOX_STATE", data=b"\x01".hex())
        trace.event("load_end", ok=True, seconds=12.5)
        trace.stop()
        lines = _read(path)
        assert [e["ev"] for e in lines] == ["tx", "load_end"]
        assert all("ts" in e and "t" in e for e in lines)
        assert lines[1]["seconds"] == 12.5


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


# ============================================================================
# afcCFS setup, connection, polling, PREP, lane setup and presence
# ============================================================================

def unit_with(values=None, lanes=(1,), do_connect=True, printer=None, name="CFS_1"):
    """A connected unit with real lanes and a recording trace."""
    unit = make_unit(name=name, values=values, printer=printer)
    made = [make_lane(unit, i) for i in lanes]
    if do_connect:
        connect(unit)
    unit.trace = RecTrace()
    return unit, made


def online_check(uid=None):
    """CMD_ONLINE_CHECK reply, with the box uid at data[2:14] when given."""
    return frame(P.CMD_ONLINE_CHECK, (b"\x00\x00" + uid) if uid else b"")


def msgs(unit):
    return unit.logger.messages


def tool_cmd(lane):
    """The lane's T command as the PREP line shows it: AFC DEV's map_to_string(), or
    AFC main's plain string map."""
    return lane.map_to_string() if hasattr(lane, "map_to_string") else str(lane.map)


# Construction and config

class TestAfcCFSInit:
    def test_builds_through_afc_unit(self):
        printer = make_printer()
        printer._gcode.register_mux_command = Recorder()
        unit = make_unit(printer=printer)
        assert (unit.name, unit.type) == ("CFS_1", "CFS")
        assert unit.reactor is printer.get_reactor()
        assert unit.transport is unit.bus.transport
        assert unit.bus.units == [unit]
        assert printer._event_handlers["klippy:ready"][-1] == unit._cfs_handle_ready
        assert printer._event_handlers["klippy:disconnect"][-1] == unit._cfs_handle_disconnect
        assert printer._gcode.register_mux_command.calls == [
            (("AFC_CFS_STATUS", "UNIT", "CFS_1", unit.cmd_AFC_CFS_STATUS),
             {"desc": unit.cmd_AFC_CFS_STATUS_help}),
            (("AFC_CFS_RECONNECT", "UNIT", "CFS_1", unit.cmd_AFC_CFS_RECONNECT),
             {"desc": unit.cmd_AFC_CFS_RECONNECT_help}),
            (("AFC_CFS_RAW", "UNIT", "CFS_1", unit.cmd_AFC_CFS_RAW),
             {"desc": unit.cmd_AFC_CFS_RAW_help}),
            (("AFC_CFS_TRACE", "UNIT", "CFS_1", unit.cmd_AFC_CFS_TRACE),
             {"desc": unit.cmd_AFC_CFS_TRACE_help}),
        ]

    def test_type_can_be_set(self):
        assert make_unit(values={"type": "CFS2"}).type == "CFS2"

    def test_cutter_pin_registers_a_button(self):
        printer = make_printer()
        unit = make_unit(printer=printer, values={"cutter_sensor_pin": "PA1"})
        printer._objects["buttons"].register_buttons.assert_called_once_with(
            ["PA1"], unit._cutter_callback)

    def test_no_cutter_pin_no_button(self):
        printer = make_printer()
        make_unit(printer=printer)
        assert "buttons" not in printer._objects

    def test_clashing_chain_is_a_config_error(self):
        printer = make_printer()
        make_unit("CFS_1", printer=printer)
        with pytest.raises(Exception) as e:
            make_unit("CFS_2", printer=printer)              # also box_addr 1
        assert str(e.value) == (f"[AFC_CFS CFS_2]: CFS CFS_1 and CFS_2 are both box_addr 1 on "
                                f"{SERIAL}; give each box on a chain its own box_addr (1-4)")


class TestAfcCFSLoadCfsConfig:
    def test_defaults(self):
        unit = make_unit()
        expected = {
            "serial_path": SERIAL, "baud": 230400, "box_addr": 1, "box_uid": None,
            "poll_interval": 1.0, "heartbeat_interval": 1.5, "heartbeat_interval_printing": 10.0,
            "request_timeout": 0.8, "stage_timeout": 30.0, "checkin_timeout": 5.0,
            "resync_timeout": 60.0, "offline_after": 3, "reconnect_interval": 5.0,
            "boot_wait": 20.0, "protocol_debug": False, "trace_file": None,
            "trace_max_size": 5.0, "trace_backups": 1, "trace_polls": False,
            "load_attempts": 2, "stage_settle_time": 0.3, "stage5_poll_interval": 0.4,
            "stage5_reply_timeout": 2.0, "stage5_timeout": 60.0, "stage5_stall_time": 8.0,
            "stage5_stall_distance": 2.0, "prime1_length": 10.0, "prime1_speed": 300.0,
            "prime2_length": 5.0, "prime2_speed": 600.0, "stage7_arg": 3,
            "clear_mode_after_load": False, "unload_retract_steps": [(15.0, 600.0), (45.0, 1800.0)],
            "unload_tip_wiggle": False, "unload_timeout": 30.0, "cutter_wait_time": 3.0,
            "cutter_require_trigger": True, "cutter_sensor_pin": None, "rfid_read": False,
            "rfid_insert_delay": 10.0, "rfid_spin": True, "rfid_busy_timeout": 150.0,
            "rfid_read_attempts": 3, "rfid_retry_interval": 5.0, "rfid_read_at_startup": False,
            "rfid_startup_delay": 15.0, "rfid_materials": {},
        }
        assert {k: getattr(unit, k) for k in expected} == expected

    def test_uid_with_separators_and_case(self):
        unit = make_unit(values={"box_uid": "67:91:3B:48 24:22:B0:02 36:35:34:34"})
        assert unit.box_uid == UID_A

    def test_uid_wrong_length(self):
        with pytest.raises(Exception) as e:
            make_unit(values={"box_uid": "6791"})
        assert str(e.value) == "box_uid must be 24 hex digits, got '6791'"

    def test_uid_not_hex(self):
        with pytest.raises(Exception) as e:
            make_unit(values={"box_uid": "zz913b482422b00236353434"})
        assert str(e.value) == "box_uid must be 24 hex digits, got 'zz913b482422b00236353434'"

    def test_uid_none_value(self):
        assert make_unit(values={"box_uid": None}).box_uid is None

    def test_materials_and_steps_parsed(self):
        unit = make_unit(values={"rfid_materials": "00003=PETG",
                                 "unload_retract_steps": "10:600, 10:1800"})
        assert unit.rfid_materials == {"00003": "PETG"}
        assert unit.unload_retract_steps == [(10.0, 600.0), (10.0, 1800.0)]


class TestAfcCFSInitCfsRuntime:
    def test_box_state_starts_unknown(self):
        unit = make_unit()
        state = {k: getattr(unit, k) for k in (
            "online", "version_sn", "material_mask", "connected_mask", "buffer_state",
            "box_status", "seen_uid", "cutter_source", "cutter_sensor_state",
            "cutter_trigger_count", "_consecutive_failures", "_last_reconnect_attempt",
            "_last_heartbeat", "_boot_wait_done", "_operation_depth", "_quiet_requests",
            "_poll_timer", "_offline_warned", "_rfid_timer", "_rfid_busy",
            "_rfid_startup_queued", "_traced_mask", "_traced_print_state")}
        assert state == {
            "online": False, "version_sn": None, "material_mask": None, "connected_mask": None,
            "buffer_state": None, "box_status": None, "seen_uid": None, "cutter_source": None,
            "cutter_sensor_state": False, "cutter_trigger_count": 0, "_consecutive_failures": 0,
            "_last_reconnect_attempt": 0.0, "_last_heartbeat": 0.0, "_boot_wait_done": False,
            "_operation_depth": 0, "_quiet_requests": False, "_poll_timer": None,
            "_offline_warned": False, "_rfid_timer": None, "_rfid_busy": False,
            "_rfid_startup_queued": False, "_traced_mask": None, "_traced_print_state": None}
        assert (unit._lane_commands, unit.present_last, unit.load_reports) == (set(), {}, {})
        assert (unit.lane_tags, unit._rfid_queue) == ({}, {})

    def test_resync_timeout_raised_to_the_units(self):
        assert make_unit().transport.resync_timeout == 60.0

    def test_shorter_resync_timeout_keeps_the_transports(self):
        assert make_unit(values={"resync_timeout": 10.0}).transport.resync_timeout == 15.0

    def test_no_trace_file(self):
        unit = make_unit()
        assert isinstance(unit.trace, CFSTrace)
        assert unit.trace.enabled is False
        assert unit.transport.trace is None

    def test_trace_file_records_wire_and_settings(self, tmp_path):
        path = tmp_path / "t.jsonl"
        unit = make_unit(values={"trace_file": str(path), "trace_max_size": 0.5,
                                 "trace_backups": 2})
        try:
            assert unit.trace.enabled is True
            assert unit.transport.trace is unit.trace
            assert (unit.trace.max_bytes, unit.trace.backups) == (524288, 2)
        finally:
            unit.trace.stop()
        first = path.read_text().splitlines()[0]
        assert '"ev":"trace_start"' in first and '"unit":"CFS_1"' in first

    def test_second_trace_leaves_the_wire_to_the_first(self, tmp_path):
        printer = make_printer()
        a = make_unit("CFS_1", printer=printer, values={"trace_file": str(tmp_path / "a")})
        b = make_unit("CFS_2", printer=printer, values={"box_addr": 2,
                                                       "trace_file": str(tmp_path / "b")})
        try:
            assert b.transport.trace is a.trace
        finally:
            a.trace.stop()
            b.trace.stop()

    def test_trace_takes_the_wire_from_a_stopped_one(self, tmp_path):
        printer = make_printer()
        a = make_unit("CFS_1", printer=printer, values={"trace_file": str(tmp_path / "a")})
        a.trace.stop()
        b = make_unit("CFS_2", printer=printer, values={"box_addr": 2,
                                                       "trace_file": str(tmp_path / "b")})
        try:
            assert b.transport.trace is b.trace
        finally:
            b.trace.stop()

    def test_unit_without_trace_file_leaves_the_wire_alone(self, tmp_path):
        printer = make_printer()
        a = make_unit("CFS_1", printer=printer, values={"trace_file": str(tmp_path / "a")})
        try:
            make_unit("CFS_2", printer=printer, values={"box_addr": 2})
            assert a.transport.trace is a.trace
        finally:
            a.trace.stop()


class TestAfcCFSParseMaterials:
    def test_empty(self):
        assert afcCFS._parse_materials("") == {}

    def test_none(self):
        assert afcCFS._parse_materials(None) == {}

    def test_several_with_spaces_and_blanks(self):
        assert afcCFS._parse_materials(" 00003 = PETG , ,04001=ABS") == {
            "00003": "PETG", "04001": "ABS"}

    @pytest.mark.parametrize("item", ["00003", "0003=PETG", "0000A=PETG", "00003= "])
    def test_bad_item(self, item):
        with pytest.raises(P.CONFIG_ERROR) as e:
            afcCFS._parse_materials(f"00001=PLA, {item}")
        assert str(e.value) == (f"rfid_materials: expected 'CODE=MATERIAL' with a 5-digit code, "
                                f"got '{item.strip()}'")


class TestAfcCFSParseSteps:
    def test_pairs(self):
        assert afcCFS._parse_steps("15:600, -45:1800") == [(15.0, 600.0), (45.0, 1800.0)]

    def test_blank_items_skipped(self):
        assert afcCFS._parse_steps(" , 10:90 ,") == [(10.0, 90.0)]

    @pytest.mark.parametrize("item", ["15", "15:600:1", "x:600"])
    def test_bad_item(self, item):
        with pytest.raises(P.CONFIG_ERROR) as e:
            afcCFS._parse_steps(f"10:90, {item}")
        assert str(e.value) == (f"unload_retract_steps: expected 'distance:speed' pairs, "
                                f"got '{item}'")


# Lifecycle

class TestAfcCFSHandleConnect:
    def test_sets_up_lanes_and_logo(self):
        unit, (lane,) = unit_with()
        assert lane.custom_load_cmd == "AFC_CFS_TOOL_LOAD LANE=lane1"
        assert unit.logo == '<span class=success--text>CFS Ready</span>\n'
        assert unit.logo_error == '<span class=error--text>CFS Not Ready</span>\n'

    def test_box_without_pin_uses_the_chains_cutter(self):
        printer = make_printer()
        a, _ = unit_with(name="CFS_1", printer=printer, values={"cutter_sensor_pin": "PA1"})
        b, _ = unit_with(name="CFS_2", printer=printer, values={"box_addr": 2}, lanes=(1,))
        assert b.cutter_source is a
        assert a.cutter_source is None                     # has its own pin

    def test_no_cutter_on_the_chain(self):
        unit, _ = unit_with()
        assert unit.cutter_source is None


class TestAfcCFSCfsHandleReady:
    def test_starts_polling(self):
        unit, _ = unit_with()
        unit._cfs_handle_ready()
        timer = unit.reactor.timers[-1]
        assert (timer.callback, timer.waketime) == (unit._poll, unit.reactor.NOW)
        assert unit._poll_timer is timer


class TestAfcCFSCfsHandleDisconnect:
    def test_stops_timers_port_and_trace(self):
        unit, _ = unit_with()
        unit._poll_timer = "poll"
        unit._rfid_timer = "rfid"
        unit.transport.close = Recorder()
        unit._cfs_handle_disconnect()
        assert unit.reactor.unregistered == ["poll", "rfid"]
        assert (unit._poll_timer, unit._rfid_timer) == (None, None)
        assert unit.transport.close.called
        assert unit.trace.events == [("trace_stop", {"reason": "klippy disconnect"})]
        assert unit.trace.stopped is True

    def test_no_timers_running(self):
        unit, _ = unit_with()
        unit.transport.close = Recorder()
        unit._cfs_handle_disconnect()
        assert unit.reactor.unregistered == []
        assert unit.transport.close.called


class TestAfcCFSCutterCallback:
    def test_counts_each_closing(self):
        unit, _ = unit_with()
        for state in (1, 1, 0, 1):
            unit._cutter_callback(0.0, state)
        assert unit.cutter_trigger_count == 2
        assert unit.cutter_sensor_state is True
        assert unit.trace.events == [
            ("cutter_sensor", {"triggered": True, "count": 1}),
            ("cutter_sensor", {"triggered": True, "count": 1}),
            ("cutter_sensor", {"triggered": False, "count": 1}),
            ("cutter_sensor", {"triggered": True, "count": 2}),
        ]

    def test_opening(self):
        unit, _ = unit_with()
        unit.cutter_sensor_state = True
        unit._cutter_callback(0.0, 0)
        assert (unit.cutter_sensor_state, unit.cutter_trigger_count) == (False, 0)


class TestAfcCFSTraceSettings:
    def test_settings(self):
        unit = make_unit()
        assert unit._trace_settings() == {
            "poll_interval": 1.0, "request_timeout": 0.8, "load_attempts": 2,
            "stage_settle_time": 0.3, "stage_timeout": 30.0, "checkin_timeout": 5.0,
            "resync_timeout": 60.0, "stage5_poll_interval": 0.4, "stage5_reply_timeout": 2.0,
            "stage5_timeout": 60.0, "stage5_stall_time": 8.0, "stage5_stall_distance": 2.0,
            "prime1_length": 10.0, "prime1_speed": 300.0, "prime2_length": 5.0,
            "prime2_speed": 600.0, "stage7_arg": 3, "clear_mode_after_load": False,
            "unload_retract_steps": [(15.0, 600.0), (45.0, 1800.0)],
            "unload_tip_wiggle": False, "unload_timeout": 30.0}


class TestAfcCFSHeartbeat:
    def test_answer_resets_failures(self):
        unit, _ = unit_with()
        unit._consecutive_failures = 2
        unit._quiet_requests = True
        assert unit.heartbeat() == frame(P.CMD_ONLINE_CHECK)
        assert unit._consecutive_failures == 0
        (req,) = unit.box.sent
        assert req.key == (1, P.CMD_ONLINE_CHECK, b"")
        assert req.kwargs == {"status": P.REQ_STATUS_ADDRESSING, "timeout": 0.8, "quiet": True}

    def test_missed_answer_counts(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.reply(P.CMD_ONLINE_CHECK, None)
        assert unit.heartbeat() is None
        assert (unit._consecutive_failures, unit.online) == (1, True)
        assert msgs(unit) == []

    def test_too_many_misses_go_offline(self):
        unit, _ = unit_with()
        unit.online = True
        unit._consecutive_failures = 2
        unit.box.reply(P.CMD_ONLINE_CHECK, None)
        unit.heartbeat()
        assert unit.online is False
        assert msgs(unit) == [("warning", "CFS CFS_1: offline (no reply to heartbeat)")]


class PrintStats:
    def __init__(self, state="standby", filename=None, raises=False):
        self.state = state
        self.filename = filename
        self.raises = raises

    def get_status(self, eventtime):
        if self.raises:
            raise RuntimeError("stats not ready")
        return {"state": self.state, "filename": self.filename}


class TestAfcCFSIsPrinting:
    def test_no_print_stats(self):
        unit, _ = unit_with()
        assert unit._is_printing() is False

    def test_printing(self):
        unit, _ = unit_with()
        unit.printer._objects["print_stats"] = PrintStats("printing")
        assert unit._is_printing() is True

    def test_paused_is_not_printing(self):
        unit, _ = unit_with()
        unit.printer._objects["print_stats"] = PrintStats("paused")
        assert unit._is_printing() is False

    def test_stats_error(self):
        unit, _ = unit_with()
        unit.printer._objects["print_stats"] = PrintStats(raises=True)
        assert unit._is_printing() is False


class TestAfcCFSTracePrintState:
    def test_trace_off(self):
        unit, _ = unit_with()
        unit.trace.enabled = False
        unit.printer._objects["print_stats"] = PrintStats("printing")
        unit._trace_print_state()
        assert unit._traced_print_state is None

    def test_no_print_stats(self):
        unit, _ = unit_with()
        unit._trace_print_state()
        assert unit.trace.events == []

    def test_stats_error(self):
        unit, _ = unit_with()
        unit.printer._objects["print_stats"] = PrintStats(raises=True)
        unit._trace_print_state()
        assert unit.trace.events == []

    def test_records_each_change_once(self):
        unit, _ = unit_with()
        stats = PrintStats("printing", "coin.gcode")
        unit.printer._objects["print_stats"] = stats
        unit._trace_print_state()
        unit._trace_print_state()
        stats.state = "complete"
        unit._trace_print_state()
        assert unit.trace.events == [
            ("print_state", {"state": "printing", "filename": "coin.gcode"}),
            ("print_state", {"state": "complete", "filename": "coin.gcode"}),
        ]
        assert unit._traced_print_state == "complete"


# Connection

class TestAfcCFSConnect:
    def _unit(self, values=None):
        unit, _ = unit_with(values=values)
        unit.transport.open = Recorder(result=True)
        unit.bus.discover = Recorder(result=[])
        return unit

    def test_port_wont_open(self):
        unit = self._unit()
        unit.online = True
        unit.transport.open = Recorder(result=False)
        unit.transport.last_error = "open /dev/fake_cfs failed: gone"
        assert unit.connect() is False
        assert msgs(unit) == [("warning", "CFS CFS_1: offline (open /dev/fake_cfs failed: gone)")]
        assert unit.box.sent == []

    def test_box_answers_at_its_address(self):
        unit = self._unit()
        unit.box.reply(P.GET_VERSION_SN, frame(P.GET_VERSION_SN, b"113500"))
        assert unit.connect() is True
        assert unit.online is True
        assert unit.version_sn == "113500"
        assert not unit.bus.discover.called
        assert unit.seen_uid is None                       # short reply: no uid

    def test_records_the_uid_seen(self):
        unit = self._unit()
        unit.box.reply(P.CMD_ONLINE_CHECK, online_check(UID_A))
        assert unit.connect() is True
        assert unit.seen_uid == UID_A

    def test_matching_box_uid(self):
        unit = self._unit({"box_uid": UID_A.hex()})
        unit.box.reply(P.CMD_ONLINE_CHECK, online_check(UID_A))
        assert unit.connect() is True

    def test_wrong_box_at_the_address(self):
        unit = self._unit({"box_uid": UID_A.hex()})
        unit.online = True
        unit.box.reply(P.CMD_ONLINE_CHECK, online_check(UID_B))
        assert unit.connect() is False
        reason = (f"box at address 1 is uid {UID_B.hex()}, expected {UID_A.hex()} (box_uid); "
                  f"power-cycle the CFS chain so the boxes are addressed again")
        assert msgs(unit) == [("error", f"CFS CFS_1: {reason}"),
                              ("warning", f"CFS CFS_1: offline ({reason})")]
        assert unit.online is False

    def test_discovery_addresses_the_box(self):
        unit = self._unit()
        unit.box.reply(P.CMD_ONLINE_CHECK, None, None, online_check())
        unit.bus.discover = Recorder(result=[(UID_B, 2), (UID_A, 1)])
        assert unit.connect() is True
        assert unit.bus.discover.calls == [((), {"timeout": 1.5})]
        assert msgs(unit)[:2] == [
            ("debug", "CFS CFS_1: no reply at address 1, trying discovery"),
            ("debug", f"CFS CFS_1: addressed box uid {UID_A.hex()} at 1"),
        ]

    def test_discovery_timeout_follows_request_timeout(self):
        unit = self._unit({"request_timeout": 2.5})
        unit.box.default_ok = False
        unit.connect()
        assert unit.bus.discover.calls == [((), {"timeout": 2.5})]

    def test_nothing_found(self):
        unit = self._unit()
        unit.online = True
        unit.box.default_ok = False
        assert unit.connect() is False
        assert msgs(unit) == [
            ("debug", "CFS CFS_1: no reply at address 1, trying discovery"),
            ("warning", "CFS CFS_1: offline (box did not answer discovery)"),
        ]
        assert unit.box.fns() == [P.CMD_ONLINE_CHECK, P.CMD_ONLINE_CHECK]

    def test_silent_after_addressing(self):
        unit = self._unit()
        unit.online = True
        unit.box.default_ok = False
        unit.bus.discover = Recorder(result=[(UID_A, 1)])
        assert unit.connect() is False
        assert msgs(unit)[-1] == ("warning",
                                  "CFS CFS_1: offline (box did not answer after addressing)")
        assert unit.box.fns() == [P.CMD_ONLINE_CHECK] * 4


class TestAfcCFSProbe:
    def test_answers_addressing_status(self):
        unit, _ = unit_with()
        assert unit._probe() == frame(P.CMD_ONLINE_CHECK)
        assert [r.kwargs["status"] for r in unit.box.sent] == [P.REQ_STATUS_ADDRESSING]
        assert unit.box.sent[0].kwargs["timeout"] == 0.8

    def test_answers_normal_status(self):
        unit, _ = unit_with()
        unit.box.reply(P.CMD_ONLINE_CHECK, None, frame(P.CMD_ONLINE_CHECK))
        assert unit._probe() == frame(P.CMD_ONLINE_CHECK)
        assert [r.kwargs["status"] for r in unit.box.sent] == [P.REQ_STATUS_ADDRESSING,
                                                               P.REQ_STATUS_NORMAL]

    def test_no_answer(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        assert unit._probe() is None


class TestAfcCFSSetOnline:
    def test_already_online(self):
        unit, _ = unit_with()
        unit.online = True
        assert unit._set_online() is True
        assert unit.box.sent == []

    def test_reads_version(self):
        unit, _ = unit_with()
        unit._consecutive_failures = 2
        unit.box.reply(P.GET_VERSION_SN, frame(P.GET_VERSION_SN, b"11350000232175B625DGIB"))
        assert unit._set_online() is True
        assert (unit.online, unit._consecutive_failures) == (True, 0)
        assert unit.box.sent[0].kwargs == {"timeout": 0.8}
        assert unit.trace.events == [("online", {"box_addr": 1,
                                                 "version": "11350000232175B625DGIB"})]
        assert msgs(unit) == [("debug", "CFS CFS_1: online (addr 1, version "
                                        "11350000232175B625DGIB)")]

    def test_no_version(self):
        unit, _ = unit_with()
        unit.box.reply(P.GET_VERSION_SN, None)
        unit._set_online()
        assert unit.version_sn is None
        assert msgs(unit) == [("debug", "CFS CFS_1: online (addr 1, version unknown)")]

    def test_after_an_offline_warning_it_is_shown(self):
        unit, _ = unit_with()
        unit._offline_warned = True
        unit.box.reply(P.GET_VERSION_SN, None)
        unit._set_online()
        assert msgs(unit) == [("info", "CFS CFS_1: online (addr 1, version unknown)")]
        assert unit._offline_warned is False


class TestAfcCFSSetOffline:
    def test_going_offline(self):
        unit, _ = unit_with()
        unit.online = True
        unit._set_offline("no reply")
        assert (unit.online, unit._offline_warned) == (False, True)
        assert msgs(unit) == [("warning", "CFS CFS_1: offline (no reply)")]
        assert unit.trace.events == [("offline", {"reason": "no reply"})]

    def test_already_offline(self):
        unit, _ = unit_with()
        unit._set_offline("no reply")
        assert unit._offline_warned is False
        assert msgs(unit) == []
        assert unit.trace.events == []


# Box requests

class TestAfcCFSRequest:
    def test_reply_resets_failures(self):
        unit, _ = unit_with()
        unit.online = True
        unit._consecutive_failures = 2
        assert unit.request(P.GET_BOX_STATE) == frame(P.GET_BOX_STATE)
        assert unit._consecutive_failures == 0
        (req,) = unit.box.sent
        assert req.kwargs == {"timeout": 0.8, "retries": 1, "motor": False, "quiet": False}

    def test_passes_options(self):
        unit, _ = unit_with()
        unit.online = True
        unit._quiet_requests = True
        unit.request(P.EXTRUDE_PROCESS, b"\x01\x00\x00", timeout=12.0, retries=0, motor=True)
        assert unit.box.sent[0].kwargs == {"timeout": 12.0, "retries": 0, "motor": True,
                                           "quiet": True}

    def test_reply_while_offline_brings_it_online(self):
        unit, _ = unit_with()
        unit.request(P.GET_BOX_STATE)
        assert unit.online is True
        assert unit.box.fns() == [P.GET_BOX_STATE, P.GET_VERSION_SN]

    def test_missed_reply_counts(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.default_ok = False
        assert unit.request(P.GET_BOX_STATE) is None
        assert (unit._consecutive_failures, unit.online) == (1, True)

    def test_too_many_misses_go_offline(self):
        unit, _ = unit_with()
        unit.online = True
        unit._consecutive_failures = 2
        unit.box.default_ok = False
        unit.request(P.GET_BOX_STATE)
        assert unit.online is False
        assert msgs(unit) == [("warning", "CFS CFS_1: offline (no reply to GET_BOX_STATE)")]

    def test_silence_during_a_tag_read_is_not_counted(self):
        unit, _ = unit_with()
        unit.online = True
        unit._rfid_busy = True
        unit._consecutive_failures = 2
        unit.box.default_ok = False
        unit.request(P.GET_RFID, b"\x01")
        assert (unit._consecutive_failures, unit.online) == (2, True)


class TestAfcCFSSetBoxMode:
    def test_print_mode_for_a_slot(self):
        unit, _ = unit_with()
        unit.online = True
        unit.set_box_mode(P.MODE_PRINT, 0x04)
        assert unit.box.keys() == [(1, P.SET_BOX_MODE, b"\x04\x00")]

    def test_idle_default_slot(self):
        unit, _ = unit_with()
        unit.online = True
        unit.set_box_mode(P.MODE_IDLE)
        assert unit.box.keys() == [(1, P.SET_BOX_MODE, b"\x00\x01")]


class TestAfcCFSConnectionMotor:
    def test_action(self):
        unit, _ = unit_with()
        unit.online = True
        unit.connection_motor(P.CONN_RETRUDE)
        assert unit.box.keys() == [(1, P.CTRL_CONNECTION_MOTOR_ACTION, b"\x02")]


class TestAfcCFSTightenUp:
    def test_on(self):
        unit, _ = unit_with()
        unit.online = True
        unit.tighten_up(True)
        assert unit.box.keys() == [(1, P.TIGHTEN_UP_ENABLE, b"\x01")]

    def test_off(self):
        unit, _ = unit_with()
        unit.online = True
        unit.tighten_up(False)
        assert unit.box.keys() == [(1, P.TIGHTEN_UP_ENABLE, b"\x00")]


class TestAfcCFSPreLoading:
    def test_run_is_a_motor_command(self):
        unit, _ = unit_with()
        unit.online = True
        unit.pre_loading(P.PRELOAD_RUN, 0x02)
        (req,) = unit.box.sent
        assert req.key == (1, P.SET_PRE_LOADING, b"\x02\x02")
        assert req.kwargs == {"timeout": 30.0, "retries": 1, "motor": True, "quiet": False}

    def test_tight_is_a_motor_command(self):
        unit, _ = unit_with()
        unit.online = True
        unit.pre_loading(P.PRELOAD_TIGHT)
        assert unit.box.sent[0].kwargs["motor"] is True

    def test_close_all_slots(self):
        unit, _ = unit_with()
        unit.online = True
        unit.pre_loading(P.PRELOAD_CLOSE)
        (req,) = unit.box.sent
        assert req.key == (1, P.SET_PRE_LOADING, b"\x0f\x00")
        assert req.kwargs == {"timeout": 0.8, "retries": 1, "motor": False, "quiet": False}

    def test_explicit_timeout_and_retries(self):
        unit, _ = unit_with()
        unit.online = True
        unit.pre_loading(P.PRELOAD_RUN, 0x01, timeout=150.0, retries=0)
        assert unit.box.sent[0].kwargs == {"timeout": 150.0, "retries": 0, "motor": True,
                                           "quiet": False}


class TestAfcCFSExtrudeStage:
    def test_stage_5_is_a_quick_poll(self):
        unit, _ = unit_with()
        unit.online = True
        unit.extrude_stage(0x01, 0x05)
        (req,) = unit.box.sent
        assert req.key == (1, P.EXTRUDE_PROCESS, b"\x01\x05\x00")
        assert req.kwargs == {"timeout": 2.0, "retries": 0, "motor": False, "quiet": False}

    def test_stage_5_explicit_timeout(self):
        unit, _ = unit_with()
        unit.online = True
        unit.extrude_stage(0x01, 0x05, timeout=0.5)
        assert unit.box.sent[0].kwargs["timeout"] == 0.5

    def test_other_stages_wait_for_the_motion(self):
        unit, _ = unit_with()
        unit.online = True
        unit.extrude_stage(0x08, 0x07, 0x03)
        (req,) = unit.box.sent
        assert req.key == (1, P.EXTRUDE_PROCESS, b"\x08\x07\x03")
        assert req.kwargs == {"timeout": 30.0, "retries": 0, "motor": True, "quiet": False}

    def test_other_stage_explicit_timeout(self):
        unit, _ = unit_with()
        unit.online = True
        unit.extrude_stage(0x08, 0x00, timeout=3.0)
        assert unit.box.sent[0].kwargs["timeout"] == 3.0


class TestAfcCFSRetrudeStage:
    def test_slot_stage_waits_for_the_motion(self):
        unit, _ = unit_with()
        unit.online = True
        unit.retrude_stage(0x02, 0x01)
        (req,) = unit.box.sent
        assert req.key == (1, P.RETRUDE_PROCESS, b"\x02\x01")
        assert req.kwargs == {"timeout": 30.0, "retries": 0, "motor": True, "quiet": False}

    def test_check_in(self):
        unit, _ = unit_with()
        unit.online = True
        unit.retrude_stage(0x00, 0x01)
        assert unit.box.sent[0].kwargs == {"timeout": 5.0, "retries": 0, "motor": False,
                                           "quiet": False}

    def test_explicit_timeout(self):
        unit, _ = unit_with()
        unit.online = True
        unit.retrude_stage(0x02, 0x00, timeout=1.0)
        assert unit.box.sent[0].kwargs["timeout"] == 1.0


class TestAfcCFSReadMaterialMask:
    def test_reads_slot_bits(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, frame(P.GET_FILAMENT_SENSOR_STATE, b"\xf5"))
        assert unit.read_material_mask() == 0x05
        assert unit.material_mask == 0x05
        assert unit.box.keys() == [(1, P.GET_FILAMENT_SENSOR_STATE, b"\x00")]

    def test_empty_reply(self):
        unit, _ = unit_with()
        unit.online = True
        unit.material_mask = 0x03
        assert unit.read_material_mask() is None
        assert unit.material_mask == 0x03

    def test_no_reply(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        assert unit.read_material_mask() is None


class TestAfcCFSReadConnectedMask:
    def test_reads_slot_bits(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, frame(P.GET_FILAMENT_SENSOR_STATE, b"\x12"))
        assert unit.read_connected_mask() == 0x02
        assert unit.connected_mask == 0x02
        assert unit.box.keys() == [(1, P.GET_FILAMENT_SENSOR_STATE, b"\x01")]

    def test_empty_reply(self):
        unit, _ = unit_with()
        unit.online = True
        assert unit.read_connected_mask() is None
        assert unit.connected_mask is None

    def test_no_reply(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        assert unit.read_connected_mask() is None


class TestAfcCFSReadBufferState:
    def test_reads_state(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.reply(P.GET_BUFFER_STATE, frame(P.GET_BUFFER_STATE, b"\x02"))
        assert unit.read_buffer_state() == P.BUFFER_EMPTY
        assert unit.buffer_state == P.BUFFER_EMPTY

    def test_empty_reply(self):
        unit, _ = unit_with()
        unit.online = True
        assert unit.read_buffer_state() is None
        assert unit.buffer_state is None

    def test_no_reply(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        assert unit.read_buffer_state() is None


class TestAfcCFSReadMeasuringWheel:
    def test_reads_position(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.reply(P.GET_MEASURING_WHEEL, frame(P.GET_MEASURING_WHEEL,
                                                    bytes.fromhex("c5afa071")))
        assert unit.read_measuring_wheel() == pytest.approx(-5620.055, abs=1e-3)
        assert unit.box.keys() == [(1, P.GET_MEASURING_WHEEL, b"\x01")]

    def test_no_reply(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        assert unit.read_measuring_wheel() is None


class TestAfcCFSReadBoxStatus:
    def test_reads_status(self):
        unit, _ = unit_with()
        unit.online = True
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, status=P.ST_EXTRUDE_ERR8))
        assert unit.read_box_status() == P.ST_EXTRUDE_ERR8
        assert unit.box_status == P.ST_EXTRUDE_ERR8

    def test_no_reply(self):
        unit, _ = unit_with()
        unit.box.default_ok = False
        assert unit.read_box_status() is None
        assert unit.box_status is None


class TestAfcCFSClearLatchedError:
    def test_sends_idle(self):
        unit, _ = unit_with()
        unit.online = True
        unit.clear_latched_error()
        assert unit.box.keys() == [(1, P.SET_BOX_MODE, b"\x00\x01")]


# Operations

class TestAfcCFSOperationInit:
    def test_keeps_the_unit(self):
        unit, _ = unit_with()
        assert afcCFS._Operation(unit).unit is unit


class TestAfcCFSOperationEnter:
    def test_waits_for_tag_reads_then_counts(self):
        unit, _ = unit_with()
        order = []
        unit.wait_for_tag_reads = lambda: order.append(unit._operation_depth)
        assert afcCFS._Operation(unit).__enter__() is unit
        assert order == [0]
        assert unit._operation_depth == 1


class TestAfcCFSOperationExit:
    def test_counts_down_and_lets_exceptions_through(self):
        unit, _ = unit_with()
        unit.wait_for_tag_reads = lambda: None
        with pytest.raises(RuntimeError):
            with unit.operation():
                assert unit.bus.operation_active is True
                raise RuntimeError("load failed")
        assert unit._operation_depth == 0
        assert unit.bus.operation_active is False


class TestAfcCFSOperation:
    def test_returns_a_context_for_this_unit(self):
        unit, _ = unit_with()
        op = unit.operation()
        assert isinstance(op, afcCFS._Operation)
        assert op.unit is unit


class TestAfcCFSWaitForTagReads:
    def test_nothing_running(self):
        unit, _ = unit_with()
        unit.wait_for_tag_reads()
        assert unit.reactor.pauses == []
        assert msgs(unit) == []

    def test_waits_for_another_box_on_the_chain(self):
        printer = make_printer()
        a, _ = unit_with(name="CFS_1", printer=printer)
        b, _ = unit_with(name="CFS_2", printer=printer, values={"box_addr": 2})
        b._rfid_busy = True
        a.reactor.on_pause.append(
            lambda now: setattr(b, "_rfid_busy", len(a.reactor.pauses) < 3))
        a.wait_for_tag_reads()
        assert a.reactor.pauses == pytest.approx([1000.2, 1000.4, 1000.6])
        assert msgs(a) == [("info", "CFS CFS_1: waiting for an RFID tag read to finish")]
        assert a.trace.events == [("wait_for_tag_read", {}),
                                  ("tag_read_done", {"waited_s": 0.6})]

    def test_gives_up_after_the_limit(self):
        unit, _ = unit_with()
        unit._rfid_busy = True
        unit.wait_for_tag_reads()
        # rfid_busy_timeout + resync_timeout + attempts * (2 + retry interval) + 10
        limit = 150.0 + 60.0 + 3 * (2.0 + 5.0) + 10.0
        assert msgs(unit)[-1] == ("warning", f"CFS CFS_1: tag read still running after "
                                             f"{limit:.0f}s; continuing")
        assert unit.reactor.now == pytest.approx(1000.0 + limit + 0.2, abs=0.25)


# Polling

class TestAfcCFSPoll:
    def _unit(self, values=None):
        unit, lanes = unit_with(values=values, lanes=(1, 2))
        unit.online = True
        unit._queue_startup_tag_reads = Recorder()
        unit._trace_print_state = Recorder()
        unit.box.on(P.GET_FILAMENT_SENSOR_STATE,
                    lambda req: frame(P.GET_FILAMENT_SENSOR_STATE,
                                      b"\x01" if req.data == b"\x00" else b"\x02"))
        unit.box.reply(P.GET_BUFFER_STATE, frame(P.GET_BUFFER_STATE, b"\x00"))
        return unit, lanes

    def test_skipped_while_a_box_runs_a_sequence(self):
        unit, _ = self._unit()
        unit._operation_depth = 1
        assert unit._poll(500.0) == 501.0
        assert unit.box.sent == []

    def test_skipped_while_the_line_is_busy(self):
        unit, _ = self._unit()
        unit.transport._mutex.locked = True
        assert unit._poll(500.0) == 501.0
        assert unit.box.sent == []

    def test_full_poll(self):
        unit, lanes = self._unit()
        quiet = []
        orig = unit.box.request

        def spy(addr, fn, data=b"", **kw):
            quiet.append(unit._quiet_requests)
            return orig(addr, fn, data, **kw)
        unit.transport.request = spy
        assert unit._poll(500.0) == unit.reactor.monotonic() + 1.0
        assert unit.box.keys() == [
            (1, P.CMD_ONLINE_CHECK, b""),
            (1, P.GET_FILAMENT_SENSOR_STATE, b"\x00"),
            (1, P.GET_FILAMENT_SENSOR_STATE, b"\x01"),
            (1, P.GET_BUFFER_STATE, b""),
        ]
        assert set(quiet) == {True}
        assert unit._quiet_requests is False
        assert unit._last_heartbeat == 500.0
        assert unit.present_last == {"lane1": True, "lane2": False}
        assert (lanes[0]._load_state, lanes[1]._load_state) == (False, True)
        assert unit.buffer_state == P.BUFFER_MIDDLE
        assert unit._queue_startup_tag_reads.called
        assert unit._trace_print_state.called

    def test_trace_polls_keeps_requests_traced(self):
        unit, _ = self._unit({"trace_polls": True})
        seen = []
        orig = unit.box.request

        def spy(addr, fn, data=b"", **kw):
            seen.append(unit._quiet_requests)
            return orig(addr, fn, data, **kw)
        unit.transport.request = spy
        unit._poll(500.0)
        assert set(seen) == {False}

    def test_heartbeat_not_due(self):
        unit, _ = self._unit()
        unit._last_heartbeat = 499.0
        unit._poll(500.0)
        assert P.CMD_ONLINE_CHECK not in unit.box.fns()

    def test_heartbeat_interval_while_printing(self):
        unit, _ = self._unit()
        unit.printer._objects["print_stats"] = PrintStats("printing")
        unit._last_heartbeat = 492.0                        # 8 s ago: under the 10 s
        unit._poll(500.0)
        assert P.CMD_ONLINE_CHECK not in unit.box.fns()
        unit._poll(502.0)
        assert unit.box.fns().count(P.CMD_ONLINE_CHECK) == 1

    def test_no_sensor_replies(self):
        unit, _ = self._unit()
        unit.box.on(P.GET_FILAMENT_SENSOR_STATE, lambda req: None)
        unit._poll(500.0)
        assert unit.present_last == {"lane1": None, "lane2": None}

    def test_error_is_logged_and_polling_goes_on(self):
        unit, _ = self._unit()
        unit.read_buffer_state = Recorder(raises=RuntimeError("boom"))
        assert unit._poll(500.0) == unit.reactor.monotonic() + 1.0
        level, text = msgs(unit)[-1]
        assert level == "debug"
        assert text.startswith("CFS CFS_1: poll failed\nTraceback")
        assert text.rstrip().endswith("RuntimeError: boom")
        assert unit._quiet_requests is False
        assert unit._trace_print_state.called

    def test_offline_reconnects(self):
        unit, _ = self._unit()
        unit.online = False
        calls = []
        unit.connect = lambda: (calls.append(1), setattr(unit, "online", True))
        unit._poll(500.0)
        assert calls == [1]
        assert unit._last_reconnect_attempt == 500.0
        assert P.GET_BUFFER_STATE in unit.box.fns()          # went on with the poll

    def test_offline_reconnect_fails(self):
        unit, _ = self._unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        assert unit._poll(500.0) == unit.reactor.monotonic() + 1.0
        assert unit.connect.called
        assert unit.box.sent == []

    def test_offline_reconnect_raises(self):
        unit, _ = self._unit()
        unit.online = False
        unit.connect = Recorder(raises=OSError("port gone"))
        unit._poll(500.0)
        level, text = msgs(unit)[-1]
        assert level == "debug"
        assert text.startswith("CFS CFS_1: reconnect failed\nTraceback")
        assert text.rstrip().endswith("OSError: port gone")

    def test_offline_waits_between_reconnects(self):
        unit, _ = self._unit()
        unit.online = False
        unit._last_reconnect_attempt = 497.0
        unit.connect = Recorder()
        unit._poll(500.0)
        assert not unit.connect.called


class TestAfcCFSMirrorPresence:
    def test_traces_changes_and_updates_lanes(self):
        unit, lanes = unit_with(lanes=(1, 2, 3))
        seen = []
        unit.update_lane_presence = lambda lane, present: seen.append((lane.name, present))
        unit._mirror_presence(0b0101)
        unit._mirror_presence(0b0101)
        assert seen == [("lane1", True), ("lane2", False), ("lane3", True)] * 2
        assert unit.trace.events == [("presence", {"mask": 5, "slots": "AC", "previous": None})]
        assert unit._traced_mask == 5

    def test_lane_with_a_bad_slot_is_skipped(self):
        unit, (lane1, lane2) = unit_with(lanes=(1, 2))
        lane1.index = 7
        seen = []
        unit.update_lane_presence = lambda lane, present: seen.append(lane.name)
        unit._mirror_presence(0b0011)
        assert seen == ["lane2"]


class TestAfcCFSMirrorConnections:
    def test_sets_load_sensors(self):
        unit, lanes = unit_with(lanes=(1, 2))
        unit._mirror_connections(0b0010)
        assert [lane._load_state for lane in lanes] == [False, True]


# PREP

class TestAfcCFSSystemTest:
    def _unit(self, online=True, material=0b0001, connected=0):
        unit, (lane,) = unit_with()
        unit.online = online
        unit.material_mask = material
        unit.connected_mask = connected
        unit.apply_lane_state = Recorder(result="<span>READY</span>")
        unit.lane_not_ready = Recorder()
        unit.connect = Recorder()
        unit._wait_for_box = Recorder()
        unit.afc.function.TcmdAssign.reset_mock()
        return unit, lane

    def test_online_lane_with_spool(self):
        unit, lane = self._unit()
        assert unit.system_Test(lane, 0, True, False) is True
        assert unit.apply_lane_state.calls == [((lane, True), {})]
        assert unit.present_last["lane1"] is True
        unit.afc.function.TcmdAssign.assert_called_once_with(lane)
        assert lane._afc_prep_done is True
        assert unit._boot_wait_done is True
        assert msgs(unit) == [("info", f"lane1 tool cmd: {tool_cmd(lane):3} "
                                       f"<span>READY</span>")]
        assert not unit.connect.called
        assert unit.box.sent == []

    def test_online_empty_slot(self):
        unit, lane = self._unit(material=0b0010)
        unit.system_Test(lane, 0, False, False)
        assert unit.apply_lane_state.calls == [((lane, False), {})]
        assert not unit.afc.function.TcmdAssign.called

    def test_reads_unknown_masks(self):
        unit, lane = self._unit(material=None, connected=None)
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE,
                       frame(P.GET_FILAMENT_SENSOR_STATE, b"\x01"),
                       frame(P.GET_FILAMENT_SENSOR_STATE, b"\x00"))
        unit.system_Test(lane, 0, False, False)
        assert unit.box.keys() == [(1, P.GET_FILAMENT_SENSOR_STATE, b"\x00"),
                                   (1, P.GET_FILAMENT_SENSOR_STATE, b"\x01")]
        assert (unit.material_mask, unit.connected_mask) == (1, 0)

    def test_mask_still_unknown(self):
        unit, lane = self._unit(material=None)
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, None)
        unit.system_Test(lane, 0, False, False)
        assert unit.apply_lane_state.calls == [((lane, False), {})]

    def test_offline_connects_and_waits_once(self):
        unit, lane = self._unit(online=False)
        assert unit.system_Test(lane, 0, False, False) is False
        assert unit.connect.called
        assert unit._wait_for_box.call_count == 1
        assert unit.present_last["lane1"] is None
        assert unit.lane_not_ready.calls == [((lane,), {})]
        assert msgs(unit) == [("info", f"lane1 tool cmd: {tool_cmd(lane):3} "
                                       f"<span class=error--text>CFS NOT RESPONDING</span>")]
        unit.system_Test(lane, 0, False, False)
        assert unit._wait_for_box.call_count == 1        # boot wait only once

    def test_box_comes_up_during_connect(self):
        unit, lane = self._unit(online=False)
        unit.connect = lambda: setattr(unit, "online", True)
        assert unit.system_Test(lane, 0, False, False) is True
        assert not unit._wait_for_box.called
        assert unit.apply_lane_state.called

    def test_map_as_a_plain_string(self):
        # AFCProject main keeps lane.map as a string, without map_to_string()
        unit, lane = self._unit()
        lane.map_to_string = None
        lane.map = "T0"
        lane.send_lane_data = Recorder()
        unit.system_Test(lane, 0, False, False)
        assert msgs(unit) == [("info", "lane1 tool cmd: T0  <span>READY</span>")]


class TestAfcCFSEnsurePrintMode:
    def test_offline(self):
        unit, (lane,) = unit_with()
        unit.ensure_print_mode(lane)
        assert unit.box.sent == []

    def test_no_reply(self):
        unit, (lane,) = unit_with()
        unit.online = True
        unit.box.reply(P.GET_BOX_STATE, None)
        unit.ensure_print_mode(lane)
        assert unit.box.fns() == [P.GET_BOX_STATE]

    def test_short_reply(self):
        unit, (lane,) = unit_with()
        unit.online = True
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, b"\x11\x22\x08"))
        unit.ensure_print_mode(lane)
        assert unit.box.fns() == [P.GET_BOX_STATE]

    def test_already_in_print_mode(self):
        unit, (lane,) = unit_with()
        unit.online = True
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, b"\x11\x22\x08\x02"))
        unit.ensure_print_mode(lane)
        assert unit.box.fns() == [P.GET_BOX_STATE]

    def test_restores_print_mode(self):
        unit, (_, lane3) = unit_with(lanes=(1, 3))
        unit.online = True
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, b"\x11\x22\x08\x00"))
        unit.ensure_print_mode(lane3)
        assert unit.box.keys() == [(1, P.GET_BOX_STATE, b""), (1, P.SET_BOX_MODE, b"\x04\x00")]
        assert unit.trace.events == [("print_mode_restored", {"lane": "lane3", "slot": "C",
                                                              "box_state": "11220800"})]
        assert msgs(unit) == [("debug", "CFS CFS_1: slot C is loaded in the toolhead; set print "
                                        "mode again (box state was 11220800)")]


class TestAfcCFSWaitForBox:
    def test_no_boot_wait(self):
        unit, _ = unit_with(values={"boot_wait": 0})
        unit.connect = Recorder()
        unit._wait_for_box()
        assert not unit.connect.called
        assert msgs(unit) == []

    def test_box_comes_up(self):
        unit, _ = unit_with()
        tries = []

        def connect():
            tries.append(unit.reactor.now)
            unit.online = len(tries) == 2
        unit.connect = connect
        unit._wait_for_box()
        assert tries == [1001.0, 1002.0]
        assert msgs(unit) == [("info", "CFS CFS_1: waiting up to 20s for the box to start")]

    def test_gives_up_at_the_deadline(self):
        unit, _ = unit_with(values={"boot_wait": 3.0})
        unit.connect = Recorder()
        unit._wait_for_box()
        assert unit.connect.call_count == 3
        assert unit.reactor.now == 1003.0


# Lanes

class TestAfcCFSSetupLanes:
    def test_each_lane(self):
        unit, lanes = unit_with(lanes=(1, 2), do_connect=False)
        unit.lanes = {lane.name: lane for lane in lanes}
        seen = []
        unit._setup_lane = lambda lane: seen.append(lane.name)
        unit._setup_lanes()
        assert seen == ["lane1", "lane2"]


class TestAfcCFSSetupLane:
    def _unit(self):
        unit, (lane,) = unit_with(do_connect=False)
        unit.gcode.register_mux_command = Recorder()
        return unit, lane

    def test_plain_lane_becomes_a_cfs_lane(self):
        unit, lane = self._unit()
        lane._load_state = True
        unit._setup_lane(lane)
        assert lane._load_state is False
        assert (unit.present_last, unit.load_reports) == ({"lane1": None}, {"lane1": {}})
        assert lane.custom_load_cmd == "AFC_CFS_TOOL_LOAD LANE=lane1"
        assert lane.custom_unload_cmd == "AFC_CFS_TOOL_UNLOAD LANE=lane1"
        assert [c[0][:3] for c in unit.gcode.register_mux_command.calls] == [
            ("AFC_CFS_TOOL_LOAD", "LANE", "lane1"), ("AFC_CFS_TOOL_UNLOAD", "LANE", "lane1"),
            ("AFC_CFS_EJECT", "LANE", "lane1"), ("AFC_CFS_READ_TAG", "LANE", "lane1")]
        assert [c[1]["desc"] for c in unit.gcode.register_mux_command.calls] == [
            "Load a CFS lane into the toolhead",
            "Unload a CFS lane from the toolhead back to the box",
            "Reel a CFS lane's filament back into the box (box motors only)",
            "Read a CFS lane's RFID tag and set its color/material"]

    def test_registered_commands_call_the_handlers(self):
        unit, lane = self._unit()
        handlers = {}
        for name in ("TOOL_LOAD", "TOOL_UNLOAD", "EJECT", "READ_TAG"):
            rec = Recorder()
            handlers[name] = rec
            setattr(unit, f"cmd_AFC_CFS_{name}", rec)
        unit._setup_lane(lane)
        for (args, _), name in zip(unit.gcode.register_mux_command.calls, handlers):
            args[3]("gcmd")
            assert handlers[name].calls == [((lane, "gcmd"), {})]

    def test_users_own_commands_kept(self):
        unit, lane = self._unit()
        lane.custom_load_cmd = "MY_LOAD"
        lane.custom_unload_cmd = "MY_UNLOAD"
        unit._setup_lane(lane)
        assert (lane.custom_load_cmd, lane.custom_unload_cmd) == ("MY_LOAD", "MY_UNLOAD")

    def test_commands_registered_once(self):
        unit, lane = self._unit()
        unit._setup_lane(lane)
        unit._setup_lane(lane)
        assert unit.gcode.register_mux_command.call_count == 4
        assert unit._lane_commands == {"lane1"}

    def test_prep_pin_refused(self):
        unit, lane = self._unit()
        lane.prep = "PA0"
        with pytest.raises(P.CONFIG_ERROR) as e:
            unit._setup_lane(lane)
        assert str(e.value) == ("[AFC_lane lane1]: CFS lanes take filament presence from the box; "
                                "remove prep/load pins")

    def test_load_pin_refused(self):
        unit, lane = self._unit()
        lane.load = "PA1"
        with pytest.raises(P.CONFIG_ERROR) as e:
            unit._setup_lane(lane)
        assert str(e.value).endswith("remove prep/load pins")

    @pytest.mark.parametrize("index", [0, 5])
    def test_slot_out_of_range(self, index):
        unit, lane = self._unit()
        lane.index = index
        with pytest.raises(P.CONFIG_ERROR) as e:
            unit._setup_lane(lane)
        assert str(e.value) == (f"[AFC_lane lane1]: unit must be 'CFS_1:<1-4>' (slot A-D), "
                                f"got index {index}")


class TestAfcCFSPause:
    def test_pauses(self):
        unit, _ = unit_with()
        unit._pause(0.3)
        assert unit.reactor.pauses == [1000.3]

    def test_zero_is_no_pause(self):
        unit, _ = unit_with()
        unit._pause(0)
        assert unit.reactor.pauses == []


class TestAfcCFSEMove:
    def test_speed_converted_to_mm_per_s(self):
        unit, _ = unit_with()
        unit.afc.move_e_pos = Recorder()
        unit._e_move(-15, 600, "retract 1")
        assert unit.afc.move_e_pos.calls == [((-15, 10.0, "CFS retract 1"), {"wait_tool": True})]


class TestAfcCFSToolheadSensor:
    def test_triggered(self):
        unit, (lane,) = unit_with()
        lane.extruder_obj.tool_start_state = True
        assert afcCFS._toolhead_sensor(lane) is True

    def test_clear(self):
        unit, (lane,) = unit_with()
        assert afcCFS._toolhead_sensor(lane) is False


class TestAfcCFSCutter:
    def test_own_switch(self):
        unit, _ = unit_with()
        assert unit._cutter() is unit

    def test_chains_switch(self):
        printer = make_printer()
        a, _ = unit_with(name="CFS_1", printer=printer, values={"cutter_sensor_pin": "PA1"})
        b, _ = unit_with(name="CFS_2", printer=printer, values={"box_addr": 2})
        assert b._cutter() is a


class TestAfcCFSCutterEngaged:
    def test_no_switch(self):
        unit, _ = unit_with()
        unit.cutter_sensor_state = True
        assert unit._cutter_engaged() is False

    def test_closed(self):
        unit, _ = unit_with(values={"cutter_sensor_pin": "PA1"})
        unit.cutter_sensor_state = True
        assert unit._cutter_engaged() is True

    def test_open(self):
        unit, _ = unit_with(values={"cutter_sensor_pin": "PA1"})
        assert unit._cutter_engaged() is False


class TestAfcCFSLaneTrace:
    def test_adds_lane_and_slot(self):
        unit, (_, lane) = unit_with(lanes=(1, 2))
        unit._lane_trace(lane, "load_start", toolhead_sensor=False)
        assert unit.trace.events == [("load_start", {"lane": "lane2", "slot": "B",
                                                     "toolhead_sensor": False})]


class TestAfcCFSTraceBoxState:
    def test_reads_and_records(self):
        unit, (_, lane) = unit_with(lanes=(1, 2))
        unit.online = True
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, status=P.ST_EXTRUDE_ERR8))
        lane.extruder_obj.tool_start_state = True
        unit._trace_box_state(lane, "prime1")
        assert unit.box.keys() == [(1, P.GET_BOX_STATE, b"")]
        assert unit.trace.events == [("box_state", {"lane": "lane2", "slot": "B",
                                                    "point": "prime1", "status": "EXTRUDE_ERR8",
                                                    "toolhead_sensor": True})]

    def test_no_reply(self):
        unit, (lane,) = unit_with()
        unit.online = True
        unit.box.default_ok = False
        unit._trace_box_state(lane, "stage7")
        assert unit.trace.events == [("box_state", {"lane": "lane1", "slot": "A",
                                                    "point": "stage7", "status": "NO_REPLY",
                                                    "toolhead_sensor": False})]

    def test_nothing_sent_without_a_trace(self):
        unit, (lane,) = unit_with()
        unit.online = True
        unit.trace = RecTrace(enabled=False)
        unit._trace_box_state(lane, "toolhead")
        assert unit.box.sent == []
        assert unit.trace.events == []


class TestAfcCFSLaneFail:
    def test_logs_and_hands_to_afc(self):
        unit, (lane,) = unit_with()
        unit._lane_fail(lane, "stage 5 timed out")
        assert unit.trace.events == [("fail", {"lane": "lane1", "slot": "A",
                                               "message": "stage 5 timed out",
                                               "toolhead_sensor": False})]
        assert msgs(unit) == [("error", "CFS lane1 (slot A): stage 5 timed out")]
        unit.afc.error.handle_lane_failure.assert_called_once_with(
            lane, "CFS lane1: stage 5 timed out")


# Presence

class TestAfcCFSUpdateLanePresence:
    def _unit(self, previous=False, tool_loaded=False, printing=False):
        unit, (lane,) = unit_with()
        lane._afc_prep_done = True
        unit.present_last["lane1"] = previous
        lane.tool_loaded = tool_loaded
        unit.afc.function.is_printing.return_value = printing
        unit.afc.save_vars.reset_mock()
        unit.on_filament_insert = Recorder()
        unit.on_filament_remove = Recorder()
        unit.apply_lane_state = Recorder(result="msg")
        for name in ("set_loaded", "set_unloaded", "_perform_infinite_runout",
                     "_perform_pause_runout"):
            setattr(lane, name, Recorder())
        return unit, lane

    def test_unchanged(self):
        unit, lane = self._unit(previous=True)
        lane.prep_state = "untouched"
        unit.update_lane_presence(lane, True)
        assert lane.prep_state == "untouched"
        assert not unit.afc.save_vars.called

    def test_before_prep_only_saves(self):
        unit, lane = self._unit()
        lane._afc_prep_done = False
        unit.update_lane_presence(lane, True)
        assert (lane.prep_state, lane.loaded_to_hub) == (True, True)
        assert unit.present_last["lane1"] is True
        unit.afc.save_vars.assert_called_once_with()
        assert not unit.on_filament_insert.called
        assert msgs(unit) == []

    def test_first_reading_present(self):
        unit, lane = self._unit(previous=None)
        unit.update_lane_presence(lane, True)
        assert unit.apply_lane_state.calls == [((lane, True), {})]
        assert not lane.set_unloaded.called
        assert not unit.on_filament_insert.called
        assert msgs(unit) == [("debug", "CFS lane1: box online, slot A: msg")]
        unit.afc.save_vars.assert_called_once_with()

    def test_first_reading_empty(self):
        unit, lane = self._unit(previous=None)
        unit.update_lane_presence(lane, False)
        assert lane.set_unloaded.called
        assert not unit.on_filament_remove.called

    def test_first_reading_empty_but_in_the_toolhead(self):
        unit, lane = self._unit(previous=None, tool_loaded=True)
        unit.update_lane_presence(lane, False)
        assert not lane.set_unloaded.called

    def test_insert(self):
        unit, lane = self._unit()
        unit.update_lane_presence(lane, True)
        assert lane.set_loaded.called
        assert unit.on_filament_insert.calls == [((lane,), {})]
        assert msgs(unit) == [("debug", "CFS lane1: spool detected in slot A")]
        unit.afc.save_vars.assert_called_once_with()

    @pytest.mark.parametrize("state", [AFCLaneState.LOADED, AFCLaneState.TOOLED,
                                       AFCLaneState.TOOL_LOADED])
    def test_insert_already_loaded(self, state):
        unit, lane = self._unit()
        lane.status = state
        unit.update_lane_presence(lane, True)
        assert not lane.set_loaded.called
        assert unit.on_filament_insert.called

    def test_remove_idle_lane(self):
        unit, lane = self._unit(previous=True)
        unit.update_lane_presence(lane, False)
        assert unit.on_filament_remove.calls == [((lane,), {})]
        assert lane.set_unloaded.called
        assert msgs(unit) == [("debug", "CFS lane1: slot A is empty")]
        unit.afc.save_vars.assert_called_once_with()

    def test_remove_loaded_lane_not_printing(self):
        unit, lane = self._unit(previous=True, tool_loaded=True, printing=False)
        unit.update_lane_presence(lane, False)
        assert not lane.set_unloaded.called
        assert not lane._perform_pause_runout.called
        assert unit.trace.events == []

    def test_printing_but_lane_not_loaded(self):
        unit, lane = self._unit(previous=True, tool_loaded=False, printing=True)
        unit.update_lane_presence(lane, False)
        assert lane.set_unloaded.called
        assert not lane._perform_pause_runout.called

    def test_runout_pauses(self):
        unit, lane = self._unit(previous=True, tool_loaded=True, printing=True)
        unit.update_lane_presence(lane, False)
        assert lane._perform_pause_runout.called
        assert not lane._perform_infinite_runout.called
        assert unit.trace.events == [("runout", {"lane": "lane1", "slot": "A",
                                                 "runout_lane": None})]

    def test_runout_to_the_next_spool(self):
        unit, lane = self._unit(previous=True, tool_loaded=True, printing=True)
        lane.runout_lane = "lane2"
        unit.update_lane_presence(lane, False)
        assert lane._perform_infinite_runout.called
        assert not lane._perform_pause_runout.called


# ============================================================================
# Tool load, cut, unload and eject
# ============================================================================

def ready_unit(values=None, lanes=(1,), printer=None, name="CFS_1"):
    unit = make_unit(name=name, values=values, printer=printer)
    made = [make_lane(unit, i) for i in lanes]
    connect(unit)
    unit.trace = RecTrace()
    unit.online = True
    unit.wait_for_tag_reads = Recorder()
    return unit, made


def wheel(mm):
    """Stage-5 reply payload: the measuring wheel as a big-endian float."""
    return struct.pack(">f", mm)


def sensor(lane, state=True):
    lane.extruder_obj.tool_start_state = state


# Stage 5

class TestAfcCFSStage5Feed:
    def _unit(self, values=None):
        unit, (lane,) = ready_unit(values=values)
        unit.load_reports["lane1"] = {}
        return unit, lane

    def test_sensor_already_triggered(self):
        unit, lane = self._unit()
        sensor(lane)
        assert unit._stage5_feed(lane) == (True, "toolhead sensor triggered after 0 polls")
        assert unit.box.sent == []

    def test_feeds_until_the_sensor_triggers(self):
        unit, lane = self._unit()
        positions = iter([10.0, 30.0, 50.0])
        unit.box.on(P.EXTRUDE_PROCESS,
                    lambda req: frame(P.EXTRUDE_PROCESS, wheel(next(positions))))
        unit.reactor.on_pause.append(lambda now: sensor(lane, len(unit.box.sent) >= 3))
        assert unit._stage5_feed(lane) == (True, "toolhead sensor triggered after 3 polls")
        assert unit.box.keys() == [(1, P.EXTRUDE_PROCESS, b"\x01\x05\x00")] * 3
        assert unit.reactor.pauses == pytest.approx([1000.4, 1000.8, 1001.2])
        assert unit.load_reports["lane1"] == {"stage5_polls": 3, "wheel": 50.0}
        assert msgs(unit) == []

    def test_times_out(self):
        unit, lane = self._unit({"stage5_timeout": 5})
        assert unit._stage5_feed(lane) == (
            False, "timed out after 5s without reaching the toolhead sensor")
        assert len(unit.box.sent) == 13                     # polls every 0.4 s until > 5 s

    def test_error_status_logged(self):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, frame(P.EXTRUDE_PROCESS, status=P.ST_EXTRUDE_ERR4))
        unit.reactor.on_pause.append(lambda now: sensor(lane))
        unit._stage5_feed(lane)
        assert msgs(unit) == [("debug", "CFS lane1: stage 5 status EXTRUDE_ERR4")]

    def test_update_state_status_not_logged(self):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, frame(P.EXTRUDE_PROCESS, status=P.ST_UPDATE_STATE))
        unit.reactor.on_pause.append(lambda now: sensor(lane))
        unit._stage5_feed(lane)
        assert msgs(unit) == []

    def test_missed_reply_skipped(self):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, None)
        unit.reactor.on_pause.append(lambda now: sensor(lane))
        assert unit._stage5_feed(lane) == (True, "toolhead sensor triggered after 1 polls")
        assert unit.load_reports["lane1"] == {"stage5_polls": 1, "wheel": None}

    def test_wheel_stall(self):
        unit, lane = self._unit()
        unit.box.on(P.EXTRUDE_PROCESS, lambda req: frame(P.EXTRUDE_PROCESS, wheel(42.0)))
        assert unit._stage5_feed(lane) == (False, "measuring wheel stalled at 42.0mm")
        # The stall clock starts at the first reading; it fails once > 8 s pass without movement
        assert unit.reactor.now - 1000.0 == pytest.approx(8.4)

    def test_small_creep_is_still_a_stall(self):
        unit, lane = self._unit()
        # 0.05 mm per 0.4 s poll: under stage5_stall_distance (2 mm) for longer than 8 s
        creep = iter(42.0 + 0.05 * i for i in range(1000))
        unit.box.on(P.EXTRUDE_PROCESS, lambda req: frame(P.EXTRUDE_PROCESS, wheel(next(creep))))
        ok, detail = unit._stage5_feed(lane)
        assert ok is False
        assert detail.startswith("measuring wheel stalled at ")

    def test_stall_but_sensor_just_triggered(self):
        unit, lane = self._unit()
        replied = []

        def poll(req):
            replied.append(True)
            return frame(P.EXTRUDE_PROCESS, wheel(42.0))
        unit.box.on(P.EXTRUDE_PROCESS, poll)
        unit.reactor.on_pause.append(lambda now: replied.clear())
        # The filament reaches the sensor just as the wheel looks stalled: seen only at the
        # stall check, after that poll's reply and before the next pause
        unit._toolhead_sensor = (lambda checked_lane: bool(replied)
                                 and unit.reactor.now - 1000.0 > 8.3)
        assert unit._stage5_feed(lane) == (True, "toolhead sensor triggered after 22 polls")


class TestAfcCFSBoxCleanup:
    def test_stops_the_motors(self):
        unit, _ = ready_unit()
        unit._box_cleanup()
        assert unit.box.keys() == [(1, P.TIGHTEN_UP_ENABLE, b"\x00"),
                                   (1, P.CTRL_CONNECTION_MOTOR_ACTION, b"\x00"),
                                   (1, P.SET_BOX_MODE, b"\x00\x01")]


# One load attempt

LOAD_START = [
    (1, P.SET_PRE_LOADING, b"\x0f\x00"),
    (1, P.GET_BOX_STATE, b""),
    (1, P.SET_BOX_MODE, b"\x00\x01"),
    (1, P.CTRL_CONNECTION_MOTOR_ACTION, b"\x01"),
    (1, P.TIGHTEN_UP_ENABLE, b"\x01"),
]


CLEANUP = [
    (1, P.TIGHTEN_UP_ENABLE, b"\x00"),
    (1, P.CTRL_CONNECTION_MOTOR_ACTION, b"\x00"),
    (1, P.SET_BOX_MODE, b"\x00\x01"),
]


class TestAfcCFSLoadAttempt:
    def _unit(self, values=None, reached=(True, "toolhead sensor triggered after 4 polls")):
        unit, (lane,) = ready_unit(values=values)
        unit.load_reports["lane1"] = {"stage5_polls": 4, "wheel": 87.5}

        def stage5(feeding_lane):
            sensor(lane, reached[0])
            return reached
        unit._stage5_feed = stage5
        return unit, lane

    def test_full_sequence(self):
        unit, lane = self._unit()
        assert unit._load_attempt(lane, 0) == (True, "loaded")
        state = (1, P.GET_BOX_STATE, b"")
        assert unit.box.keys() == LOAD_START + [
            (1, P.EXTRUDE_PROCESS, b"\x01\x00\x00"),
            (1, P.EXTRUDE_PROCESS, b"\x01\x04\x00"),
            state,
            state,
            (1, P.EXTRUDE_PROCESS, b"\x01\x06\x00"),
            state,
            state,
            (1, P.EXTRUDE_PROCESS, b"\x01\x07\x03"),
            state,
            (1, P.SET_BOX_MODE, b"\x01\x00"),
            (1, P.TIGHTEN_UP_ENABLE, b"\x00"),
            (1, P.CTRL_CONNECTION_MOTOR_ACTION, b"\x00"),
            state,
        ]
        # Prime 1 (10 mm at 300 mm/min) before stage 6, prime 2 (5 at 600) before stage 7
        assert unit.afc.move_e_pos.calls == [
            ((10.0, 300.0 / 60, "CFS prime 1"), {"wait_tool": True}),
            ((5.0, 600.0 / 60, "CFS prime 2"), {"wait_tool": True}),
        ]
        assert unit.load_reports["lane1"]["stage6"] == "OK"
        assert unit.load_reports["lane1"]["stage7"] == "OK"
        assert unit.trace.events == [
            ("stage5_result", {"lane": "lane1", "slot": "A", "reached": True,
                               "detail": "toolhead sensor triggered after 4 polls", "attempt": 1,
                               "polls": 4, "wheel": 87.5, "seconds": 0.0}),
        ] + [
            ("box_state", {"lane": "lane1", "slot": "A", "point": point, "status": "OK",
                           "toolhead_sensor": True})
            for point in ("toolhead", "prime1", "stage6", "prime2", "stage7")
        ] + [
            ("handoff", {"lane": "lane1", "slot": "A", "attempt": 1, "stage6": "OK",
                         "stage7": "OK", "stage7_arg": 3, "toolhead_sensor": True}),
            ("box_state", {"lane": "lane1", "slot": "A", "point": "print_mode", "status": "OK",
                           "toolhead_sensor": True}),
        ]
        assert msgs(unit) == [("debug", "CFS lane1: stage 5 toolhead sensor triggered after 4 "
                                        "polls")]

    def test_latched_error_before_load_logged(self):
        unit, lane = self._unit()
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, status=P.ST_EXTRUDE_ERR8))
        unit._load_attempt(lane, 0)
        assert msgs(unit)[0] == ("debug", "CFS lane1: box reported EXTRUDE_ERR8 before load, "
                                          "clearing")

    @pytest.mark.parametrize("status", [None, P.ST_UPDATE_STATE])
    def test_quiet_box_states_not_logged(self, status):
        unit, lane = self._unit()
        unit.box.reply(P.GET_BOX_STATE,
                       None if status is None else frame(P.GET_BOX_STATE, status=status))
        unit._load_attempt(lane, 0)
        assert not any("before load" in m for _, m in msgs(unit))

    def test_connection_motor_silent(self):
        unit, lane = self._unit()
        unit.box.reply(P.CTRL_CONNECTION_MOTOR_ACTION, None)
        assert unit._load_attempt(lane, 0) == (
            False, "box did not answer CTRL_CONNECTION_MOTOR_ACTION")
        assert unit.box.fns()[-1] == P.CTRL_CONNECTION_MOTOR_ACTION

    @pytest.mark.parametrize("stage,before", [(0x00, 0), (0x04, 1)])
    def test_stage_no_reply(self, stage, before):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, *([frame(P.EXTRUDE_PROCESS)] * before + [None]))
        assert unit._load_attempt(lane, 0) == (False, f"no reply to EXTRUDE_PROCESS stage {stage}")
        assert unit.box.keys()[-3:] == CLEANUP

    @pytest.mark.parametrize("stage,before", [(0x00, 0), (0x04, 1)])
    def test_stage_error(self, stage, before):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, *([frame(P.EXTRUDE_PROCESS)] * before
                                            + [frame(P.EXTRUDE_PROCESS, status=P.ST_STATE_ERR)]))
        assert unit._load_attempt(lane, 0) == (
            False, f"EXTRUDE_PROCESS stage {stage} returned STATE_ERR")
        assert unit.box.keys()[-3:] == CLEANUP

    def test_stage5_short_pulls_the_feed_back(self):
        unit, lane = self._unit(reached=(False, "measuring wheel stalled at 42.0mm"))
        assert unit._load_attempt(lane, 1) == (False, "measuring wheel stalled at 42.0mm")
        assert unit.box.keys()[-6:] == CLEANUP + [
            (1, P.RETRUDE_PROCESS, b"\x01\x00"),
            (1, P.RETRUDE_PROCESS, b"\x01\x01"),
            (1, P.SET_BOX_MODE, b"\x00\x01"),
        ]
        assert unit.trace.events[0][1]["attempt"] == 2
        assert not unit.afc.move_e_pos.called

    def test_no_prime_moves(self):
        unit, lane = self._unit({"prime1_length": 0, "prime2_length": 0})
        unit._load_attempt(lane, 0)
        assert not unit.afc.move_e_pos.called

    def test_stage6_and_7_silent(self):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, frame(P.EXTRUDE_PROCESS), frame(P.EXTRUDE_PROCESS),
                       None, None)
        assert unit._load_attempt(lane, 0) == (True, "loaded")
        assert (unit.load_reports["lane1"]["stage6"], unit.load_reports["lane1"]["stage7"]) == (
            "NO_REPLY", "NO_REPLY")
        assert ("warning", "CFS lane1: stage 7 returned NO_REPLY") in msgs(unit)

    def test_stage7_error_status(self):
        unit, lane = self._unit()
        unit.box.reply(P.EXTRUDE_PROCESS, frame(P.EXTRUDE_PROCESS), frame(P.EXTRUDE_PROCESS),
                       frame(P.EXTRUDE_PROCESS), frame(P.EXTRUDE_PROCESS,
                                                       status=P.ST_EXTRUDE_ERR10))
        unit._load_attempt(lane, 0)
        assert msgs(unit)[-1] == ("warning", "CFS lane1: stage 7 returned EXTRUDE_ERR10")

    def test_clear_mode_after_load(self):
        unit, lane = self._unit({"clear_mode_after_load": True})
        unit._load_attempt(lane, 0)
        assert unit.box.keys()[-2:] == [(1, P.SET_BOX_MODE, b"\x00\x01"),
                                         (1, P.GET_BOX_STATE, b"")]

    def test_sensor_lost_after_handoff(self):
        unit, lane = self._unit()
        orig = unit.set_box_mode

        def drop_sensor(mode, bits=0):
            if mode == P.MODE_PRINT:
                sensor(lane, False)
            return orig(mode, bits)
        unit.set_box_mode = drop_sensor
        assert unit._load_attempt(lane, 0) == (False, "toolhead sensor cleared after the handoff")


# The load command

class TestAfcCFSCmdToolLoad:
    def _unit(self, values=None, lanes=(1,)):
        unit, made = ready_unit(values=values, lanes=lanes)
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, frame(P.GET_FILAMENT_SENSOR_STATE, b"\x0f"))
        lane = made[0]
        lane.select_lane = Recorder()
        unit.afc._check_extruder_temp.reset_mock()
        unit._mark_tool_state = Recorder()
        unit.attempts = []

        def attempt(loading_lane, n):
            result = unit.attempt_results.pop(0)
            unit.attempts.append(n)
            if isinstance(result, BaseException):
                raise result
            if result[0]:
                sensor(loading_lane)
            return result
        unit.attempt_results = [(True, "loaded")]
        unit._load_attempt = attempt
        return unit, lane

    def test_loads(self):
        unit, lane = self._unit()
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.wait_for_tag_reads.called
        assert lane.select_lane.called
        unit.afc._check_extruder_temp.assert_called_once_with(lane)
        assert unit.attempts == [0]
        # tool_stn 60 minus the 15 mm the primes already pushed
        assert unit.afc.move_e_pos.calls == [((45.0, 10.0, "CFS tool_stn"), {"wait_tool": True})]
        assert lane._load_state is True
        assert unit._mark_tool_state.calls == [((lane,), {"loaded": True})]
        assert unit.load_reports["lane1"] == {"slot": "A", "result": "loaded"}
        assert unit.trace.events[0] == ("load_start", {"lane": "lane1", "slot": "A",
                                                       "toolhead_sensor": False})
        assert unit.trace.events[-1] == ("load_end", {"lane": "lane1", "slot": "A", "ok": True,
                                                      "detail": "loaded", "attempts": 1,
                                                      "tool_stn_push": 45.0, "seconds": 0.0})
        assert msgs(unit) == [("debug", "CFS lane1: loading slot A, try 1/2")]
        assert unit._operation_depth == 0
        assert not unit.afc.error.handle_lane_failure.called

    def test_short_tool_stn_no_push(self):
        unit, lane = self._unit()
        lane.extruder_obj.tool_stn = 12.0
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert not unit.afc.move_e_pos.called
        assert unit.trace.events[-1][1]["tool_stn_push"] == 0

    def test_parks_first(self):
        unit, lane = self._unit()
        unit.afc.park = True
        unit.afc.park_cmd = "AFC_CFS_PARK"
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        unit.afc.gcode.run_script_from_command.assert_called_once_with(
            "AFC_CFS_PARK EXTRUDER=extruder")

    def test_park_on_without_a_macro(self):
        unit, lane = self._unit()
        unit.afc.park = True
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert not unit.afc.gcode.run_script_from_command.called

    def test_park_macro_but_park_off(self):
        unit, lane = self._unit()
        unit.afc.park_cmd = "AFC_CFS_PARK"
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert not unit.afc.gcode.run_script_from_command.called

    def _failed(self, unit, lane, message):
        unit.afc.error.handle_lane_failure.assert_called_once_with(lane, f"CFS lane1: {message}")
        assert msgs(unit)[-1] == ("error", f"CFS lane1 (slot A): {message}")

    def test_offline_box_connects(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=True)
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.connect.called
        assert unit.attempts == [0]

    def test_offline_box_wont_answer(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        unit.transport.last_error = "open /dev/fake_cfs failed: gone"
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        self._failed(unit, lane, "box is offline (open /dev/fake_cfs failed: gone)")
        assert unit.attempts == []

    def test_offline_box_no_error_text(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        self._failed(unit, lane, "box is offline (no reply)")

    def test_cutter_engaged(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1"})
        unit.cutter_sensor_state = True
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        self._failed(unit, lane, "cutter sensor is engaged; reset the cutter before loading")

    def test_toolhead_already_loaded(self):
        unit, lane = self._unit()
        sensor(lane)
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        self._failed(unit, lane, "toolhead sensor already sees filament; unload the other lane "
                                 "first")

    def test_empty_slot(self):
        unit, lane = self._unit(lanes=(2,))
        unit.box._queues.clear()
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, frame(P.GET_FILAMENT_SENSOR_STATE, b"\x01"))
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        unit.afc.error.handle_lane_failure.assert_called_once_with(
            lane, "CFS lane2: no filament in slot B")
        assert unit.attempts == []

    def test_presence_unknown_still_loads(self):
        unit, lane = self._unit()
        unit.box._queues.clear()
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, None)
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.attempts == [0]

    def test_retries_then_loads(self):
        unit, lane = self._unit()
        unit.attempt_results = [(False, "measuring wheel stalled at 42.0mm"), (True, "loaded")]
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.attempts == [0, 1]
        assert ("warning", "CFS lane1: load attempt 1 failed: measuring wheel stalled at "
                           "42.0mm") in msgs(unit)
        assert ("load_attempt_failed", {"lane": "lane1", "slot": "A", "attempt": 1,
                                        "detail": "measuring wheel stalled at 42.0mm",
                                        "toolhead_sensor": False}) in unit.trace.events
        assert unit._mark_tool_state.called

    def test_stuck_in_the_toolhead_stops_retrying(self):
        unit, lane = self._unit()

        def stuck(loading_lane, n):
            unit.attempts.append(n)
            sensor(loading_lane)
            return False, "toolhead sensor cleared after the handoff"
        unit._load_attempt = stuck
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.attempts == [0]
        self._failed(unit, lane, "load failed after 1 attempt(s): toolhead sensor cleared after "
                                 "the handoff")

    def test_every_attempt_fails(self):
        unit, lane = self._unit()
        unit.attempt_results = [(False, "a"), (False, "timed out after 60s")]
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.attempts == [0, 1]
        assert unit.load_reports["lane1"]["result"] == "timed out after 60s"
        assert unit.trace.events[-2] == ("load_end", {"lane": "lane1", "slot": "A", "ok": False,
                                                      "detail": "timed out after 60s",
                                                      "attempts": 2, "seconds": 0.0})
        self._failed(unit, lane, "load failed after 2 attempt(s): timed out after 60s")
        assert not unit._mark_tool_state.called
        assert lane._load_state is False

    def test_attempt_error_cleans_up(self):
        unit, lane = self._unit({"load_attempts": 1})
        unit.attempt_results = [RuntimeError("serial gone")]
        unit.box.clear()
        unit.cmd_AFC_CFS_TOOL_LOAD(lane, gcmd())
        assert unit.box.keys()[-3:] == CLEANUP
        debug = [m for lvl, m in msgs(unit) if lvl == "debug" and m.startswith("Traceback")]
        assert len(debug) == 1 and debug[0].rstrip().endswith("RuntimeError: serial gone")
        self._failed(unit, lane, "load failed after 1 attempt(s): error: serial gone")


# Cut

class TestAfcCFSRunCutterSequence:
    def _unit(self, values=None, printer=None, name="CFS_1"):
        unit, (lane,) = ready_unit(values=values, printer=printer, name=name)
        unit.afc.tool_cut = True
        unit.afc.toolhead.wait_moves.reset_mock()
        return unit, lane

    @staticmethod
    def _cut_events(unit):
        return [e for e in unit.trace.events if e[0] == "cut"]

    def _cuts(self, unit, *switch_states):
        """Each cut moves the cutter switch through these states."""
        cutter = unit._cutter()
        plans = list(switch_states)

        def cut(cmd):
            for state in plans.pop(0) if plans else ():
                cutter._cutter_callback(0.0, state)
        unit.afc.gcode.run_script_from_command.side_effect = cut

    def test_cutting_off(self):
        unit, lane = self._unit()
        unit.afc.tool_cut = False
        assert unit._run_cutter_sequence(lane) is None
        assert not unit.afc.gcode.run_script_from_command.called

    def test_no_switch_cut_once_unverified(self):
        unit, lane = self._unit()
        assert unit._run_cutter_sequence(lane) is None
        unit.afc.gcode.run_script_from_command.assert_called_once_with("AFC_CUT EXTRUDER=extruder")
        assert lane.extruder_obj.estats.increase_cut_total.call_count == 1
        assert unit.trace.events == [("cut", {"lane": "lane1", "slot": "A", "attempt": 1,
                                              "verified": False})]
        assert not unit.afc.toolhead.wait_moves.called

    def test_switch_fired_and_released(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1"})
        self._cuts(unit, (1, 0))
        assert unit._run_cutter_sequence(lane) is None
        assert unit.afc.toolhead.wait_moves.called
        assert self._cut_events(unit) == [("cut", {"lane": "lane1", "slot": "A", "attempt": 1,
                                              "verified": True, "fired": True, "released": True,
                                              "seconds": 0.1})]
        assert msgs(unit) == []

    def test_switch_releases_while_waiting(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1"})
        self._cuts(unit, (1,))
        unit.reactor.on_pause.append(
            lambda now: unit._cutter_callback(0.0, 0) if len(unit.reactor.pauses) == 3 else None)
        assert unit._run_cutter_sequence(lane) is None
        assert self._cut_events(unit)[-1][1]["released"] is True

    def test_switch_never_released(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1", "cutter_wait_time": 0.2})
        self._cuts(unit, (1,))
        reason = "cutter sensor did not release after cutting"
        assert unit._run_cutter_sequence(lane) == reason
        # load_attempts (2) + 1 cuts, each one warned
        assert msgs(unit) == [("warning", f"CFS lane1: cut {n} not confirmed: {reason}")
                              for n in (1, 2, 3)]
        assert unit.afc.gcode.run_script_from_command.call_count == 3

    def test_switch_never_fired(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1"})
        reason = ("cutter switch never fired during the cut (blade did not reach the end of its "
                  "travel; check pin_loc_xy / cut_move_dist)")
        assert unit._run_cutter_sequence(lane) == reason
        assert msgs(unit)[0] == ("warning", f"CFS lane1: cut 1 not confirmed: {reason}")

    def test_fired_on_a_retry(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1"})
        self._cuts(unit, (), (1, 0))
        assert unit._run_cutter_sequence(lane) is None
        assert [e[1]["fired"] for e in self._cut_events(unit)] == [False, True]

    def test_trigger_not_required(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1", "cutter_require_trigger": False})
        assert unit._run_cutter_sequence(lane) is None
        assert self._cut_events(unit)[-1][1]["fired"] is False

    def test_uses_the_chains_switch(self):
        printer = make_printer()
        a, _ = self._unit({"cutter_sensor_pin": "PA1"}, printer=printer)
        b, lane = self._unit({"box_addr": 2}, printer=printer, name="CFS_2")
        b.cutter_source = a
        self._cuts(b, (1, 0))
        assert b._run_cutter_sequence(lane) is None
        assert a.cutter_trigger_count == 1


class TestAfcCFSRunPostCutMacros:
    def test_nothing_enabled(self):
        unit, (lane,) = ready_unit()
        unit._run_post_cut_macros(lane)
        assert not unit.afc.gcode.run_script_from_command.called

    def test_park(self):
        unit, (lane,) = ready_unit()
        unit.afc.park = True
        unit.afc.park_cmd = "AFC_CFS_PARK"
        unit._run_post_cut_macros(lane)
        unit.afc.gcode.run_script_from_command.assert_called_once_with(
            "AFC_CFS_PARK EXTRUDER=extruder")

    def test_park_without_macro(self):
        unit, (lane,) = ready_unit()
        unit.afc.park = True
        unit._run_post_cut_macros(lane)
        assert not unit.afc.gcode.run_script_from_command.called

    def test_afc_tip_forming(self):
        unit, (lane,) = ready_unit()
        unit.afc.form_tip = True
        unit.afc.form_tip_cmd = "AFC"
        tip = type("Tip", (), {"tip_form": Recorder()})()
        unit.printer._objects["AFC_form_tip"] = tip
        unit._run_post_cut_macros(lane)
        assert tip.tip_form.called
        assert not unit.afc.gcode.run_script_from_command.called

    def test_custom_tip_macro(self):
        unit, (lane,) = ready_unit()
        unit.afc.form_tip = True
        unit.afc.form_tip_cmd = "MY_TIP"
        unit._run_post_cut_macros(lane)
        unit.afc.gcode.run_script_from_command.assert_called_once_with("MY_TIP")

    def test_tip_forming_without_macro(self):
        unit, (lane,) = ready_unit()
        unit.afc.form_tip = True
        unit._run_post_cut_macros(lane)
        assert not unit.afc.gcode.run_script_from_command.called


# Retract to the box

RETRACT_START = [(1, P.SET_PRE_LOADING, b"\x0f\x00"), (1, P.SET_BOX_MODE, b"\x00\x01"),
                 (1, P.RETRUDE_PROCESS, b"\x01\x00")]


class TestAfcCFSRetractToBox:
    def _unit(self, values=None):
        unit, (lane,) = ready_unit(values=values)
        sensor(lane)

        def finish(req):
            if req.data == b"\x01\x01":
                unit.reactor.now += 12.4                  # the reel-back takes a while
                sensor(lane, False)
            return frame(P.RETRUDE_PROCESS)
        unit.box.on(P.RETRUDE_PROCESS, finish)
        return unit, lane

    def test_unloads(self):
        unit, lane = self._unit()
        assert unit._retract_to_box(lane) == (True, "reeled back in 12.4s")
        assert unit.box.keys() == RETRACT_START + [(1, P.RETRUDE_PROCESS, b"\x01\x01"),
                                                   (1, P.SET_BOX_MODE, b"\x00\x01")]
        assert unit.afc.move_e_pos.calls == [
            ((-15.0, 10.0, "CFS unload retract 1"), {"wait_tool": True}),
            ((-45.0, 30.0, "CFS unload retract 2"), {"wait_tool": True}),
        ]
        assert unit.trace.events == [
            ("retrude_start", {"lane": "lane1", "slot": "A", "status": "OK", "seconds": 0.0,
                               "toolhead_sensor": True}),
            ("retract_step", {"lane": "lane1", "slot": "A", "step": 1, "mm": 15.0,
                              "speed": 600.0, "toolhead_sensor": True}),
            ("retract_step", {"lane": "lane1", "slot": "A", "step": 2, "mm": 45.0,
                              "speed": 1800.0, "toolhead_sensor": True}),
            ("reel", {"lane": "lane1", "slot": "A", "done": True, "replies": ["OK", "OK"],
                      "seconds": 12.4, "toolhead_sensor": False}),
        ]
        assert msgs(unit) == []

    def test_tip_wiggle(self):
        unit, lane = self._unit({"unload_tip_wiggle": True})
        unit._retract_to_box(lane)
        wiggle = [c[0] for c in unit.afc.move_e_pos.calls[:6]]
        assert wiggle == [(0.5, 10.0, "CFS tip wiggle"), (-5, 10.0, "CFS tip wiggle"),
                          (2.5, 10.0, "CFS tip wiggle"), (-1.25, 10.0, "CFS tip wiggle"),
                          (1.75, 10.0, "CFS tip wiggle"), (1, 1.0, "CFS tip wiggle")]

    def test_start_not_answered(self):
        unit, lane = self._unit()
        unit.box._handlers.clear()
        unit.box.reply(P.RETRUDE_PROCESS, None)
        assert unit._retract_to_box(lane) == (False, "box did not answer RETRUDE start")
        assert unit.trace.events[0][1]["status"] == "NO_REPLY"
        assert not unit.afc.move_e_pos.called

    def test_start_error_carries_on(self):
        unit, lane = self._unit()
        unit.box.reply(P.RETRUDE_PROCESS, frame(P.RETRUDE_PROCESS, status=P.ST_RETRUDE_ERR2))
        assert unit._retract_to_box(lane) == (True, "reeled back in 12.4s")
        assert msgs(unit) == [("warning", "CFS lane1: RETRUDE start returned RETRUDE_ERR2")]

    def test_finish_not_confirmed(self):
        unit, lane = self._unit()
        unit.box._handlers.clear()
        unit.box.reply(P.RETRUDE_PROCESS, frame(P.RETRUDE_PROCESS),
                       frame(P.RETRUDE_PROCESS, status=P.ST_RETRUDE_ERR7))
        sensor(lane, False)
        assert unit._retract_to_box(lane) == (
            False, "box did not confirm reel-back (start OK, finish RETRUDE_ERR7)")

    def test_finish_not_answered(self):
        unit, lane = self._unit()
        unit.box._handlers.clear()
        unit.box.reply(P.RETRUDE_PROCESS, frame(P.RETRUDE_PROCESS), None)
        sensor(lane, False)
        ok, detail = unit._retract_to_box(lane)
        assert (ok, detail) == (False, "box did not confirm reel-back (start OK, finish NO_REPLY)")
        assert unit.trace.events[-1][1]["done"] is False

    def test_sensor_clears_after_a_wait(self):
        unit, lane = self._unit()
        unit.box._handlers.clear()
        unit.reactor.on_pause.append(
            lambda now: sensor(lane, False) if len(unit.reactor.pauses) >= 3 else None)
        assert unit._retract_to_box(lane)[0] is True

    def test_sensor_never_clears(self):
        unit, lane = self._unit({"unload_timeout": 2.0})
        unit.box._handlers.clear()
        assert unit._retract_to_box(lane) == (
            False, "toolhead sensor still triggered 2s after reel-back (box start OK, finish OK)")


class TestAfcCFSCmdToolUnload:
    def _unit(self, values=None):
        unit, (lane,) = ready_unit(values=values)
        lane.select_lane = Recorder()
        lane.disable_buffer = Recorder()
        unit.afc._check_extruder_temp.reset_mock()
        unit._run_cutter_sequence = Recorder(result=None)
        unit._run_post_cut_macros = Recorder()
        unit._retract_to_box = Recorder(result=(True, "reeled back in 12.4s"))
        unit._mark_tool_state = Recorder()
        return unit, lane

    def _failed(self, unit, lane, message):
        unit.afc.error.handle_lane_failure.assert_called_once_with(lane, f"CFS lane1: {message}")
        assert msgs(unit)[-1] == ("error", f"CFS lane1 (slot A): {message}")

    def test_unloads(self):
        unit, lane = self._unit()
        lane._load_state = True
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        assert unit.wait_for_tag_reads.called
        assert lane.select_lane.called and lane.disable_buffer.called
        unit.afc._check_extruder_temp.assert_called_once_with(lane)
        assert unit._run_cutter_sequence.calls == [((lane,), {})]
        assert unit._run_post_cut_macros.calls == [((lane,), {})]
        assert lane._load_state is False
        assert unit._mark_tool_state.calls == [((lane,), {"loaded": False})]
        assert unit.trace.events == [
            ("unload_start", {"lane": "lane1", "slot": "A", "toolhead_sensor": False}),
            ("unload_end", {"lane": "lane1", "slot": "A", "ok": True,
                            "detail": "reeled back in 12.4s", "seconds": 0.0}),
        ]
        assert msgs(unit) == [("debug", "CFS lane1: unload reeled back in 12.4s")]
        assert unit._operation_depth == 0

    def test_offline_box_connects(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=True)
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        assert unit._retract_to_box.called

    def test_offline_box_wont_answer(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        self._failed(unit, lane, "box is offline (no reply)")
        assert not unit._run_cutter_sequence.called

    def test_offline_box_error_text(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        unit.transport.last_error = "read error: gone"
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        self._failed(unit, lane, "box is offline (read error: gone)")

    def test_cutter_engaged_before_cutting(self):
        unit, lane = self._unit({"cutter_sensor_pin": "PA1"})
        unit.cutter_sensor_state = True
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        self._failed(unit, lane, "cutter sensor is engaged before cutting; check the cutter lever")
        assert not unit._run_cutter_sequence.called

    def test_cut_failed(self):
        unit, lane = self._unit()
        unit._run_cutter_sequence = Recorder(result="cutter sensor did not release after cutting")
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        self._failed(unit, lane, "cutter sensor did not release after cutting")
        assert not unit._retract_to_box.called

    def test_retract_failed(self):
        unit, lane = self._unit()
        lane._load_state = True
        unit._retract_to_box = Recorder(result=(False, "box did not answer RETRUDE start"))
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        self._failed(unit, lane, "unload failed: box did not answer RETRUDE start")
        assert lane._load_state is True
        assert not unit._mark_tool_state.called

    def test_retract_error(self):
        unit, lane = self._unit()
        unit._retract_to_box = Recorder(raises=RuntimeError("serial gone"))
        unit.cmd_AFC_CFS_TOOL_UNLOAD(lane, gcmd())
        assert unit.box.keys() == [(1, P.SET_BOX_MODE, b"\x00\x01")]
        debug = [m for lvl, m in msgs(unit) if lvl == "debug"]
        assert debug[0].startswith("Traceback") and debug[0].rstrip().endswith(
            "RuntimeError: serial gone")
        assert debug[1] == "CFS lane1: unload error: serial gone"
        self._failed(unit, lane, "unload failed: error: serial gone")
        assert unit._operation_depth == 0


class TestAfcCFSMarkToolState:
    def test_loaded(self):
        unit, (lane,) = ready_unit()
        unit.afc.save_vars.reset_mock()
        unit._mark_tool_state(lane, loaded=True)
        assert lane.tool_loaded is True
        unit.afc.save_vars.assert_called_once_with()

    def test_unloaded(self):
        unit, (lane,) = ready_unit()
        lane.set_tool_loaded(normal_toolchange=True)
        unit._mark_tool_state(lane, loaded=False)
        assert lane.tool_loaded is False

    def test_failure_is_a_warning(self):
        unit, (lane,) = ready_unit()
        lane.set_tool_loaded = Recorder(raises=RuntimeError("no extruder"))
        unit._mark_tool_state(lane, loaded=True)
        assert msgs(unit) == [("warning", "CFS lane1: could not record tool state: no extruder")]


class TestAfcCFSCmdEject:
    def test_reels_back(self):
        unit, (_, lane) = ready_unit(lanes=(1, 2))
        unit.reel_back = Recorder(result=(True, "reeled back"))
        cmd = gcmd(LANE="lane2")
        unit.cmd_AFC_CFS_EJECT(lane, cmd)
        assert unit.wait_for_tag_reads.called
        assert unit.reel_back.calls == [((lane,), {})]
        cmd.respond_info.assert_called_once_with("CFS lane2 (slot B) eject: reeled back")

    def test_offline_box_connects(self):
        unit, (lane,) = ready_unit()
        unit.online = False
        unit.connect = Recorder(result=True)
        unit.reel_back = Recorder(result=(True, "reeled back"))
        unit.cmd_AFC_CFS_EJECT(lane, gcmd())
        assert unit.reel_back.called

    def test_offline_box(self):
        unit, (lane,) = ready_unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        unit.reel_back = Recorder()
        with pytest.raises(CommandError) as e:
            unit.cmd_AFC_CFS_EJECT(lane, gcmd())
        assert str(e.value) == "CFS lane1: box is offline"
        assert not unit.reel_back.called

    def test_not_clean(self):
        unit, (lane,) = ready_unit()
        unit.reel_back = Recorder(result=(False, "box answered RETRUDE_ERR7"))
        cmd = gcmd()
        with pytest.raises(CommandError) as e:
            unit.cmd_AFC_CFS_EJECT(lane, cmd)
        cmd.respond_info.assert_called_once_with("CFS lane1 (slot A) eject: box answered "
                                                 "RETRUDE_ERR7")
        assert str(e.value) == "CFS lane1: eject did not finish cleanly: box answered RETRUDE_ERR7"


# ============================================================================
# RFID tag reads
# ============================================================================

def rfid_unit(values=None, lanes=(1,), printer=None, name="CFS_1"):
    unit = make_unit(name=name, values=values, printer=printer)
    made = [make_lane(unit, i) for i in lanes]
    connect(unit)
    unit.trace = RecTrace()
    unit.online = True
    for lane in made:
        lane._afc_prep_done = True
    return unit, made


def tag(**over):
    base = P.decode_rfid_record(PINK_SILK)
    base.update(over)
    return base


# The command

class TestAfcCFSCmdReadTag:
    def _unit(self, values=None, found=None):
        unit, (lane,) = rfid_unit(values=values)
        unit.read_tag = Recorder(result=found)
        unit.afc.gcode.run_script_from_command.reset_mock()
        return unit, lane

    def test_tag_reported_and_applied(self):
        unit, lane = self._unit(found=tag())
        cmd = gcmd()
        unit.cmd_AFC_CFS_READ_TAG(lane, cmd)
        assert unit.read_tag.calls == [((lane,), {"spin": True})]
        assert [c.args[0] for c in cmd.respond_info.call_args_list] == [
            f"CFS lane1 (slot A) tag: PLA, #E7A18E, 1000 g\n  record {PINK_SILK}",
            "  SET_COLOR LANE=lane1 COLOR=E7A18E",
            "  SET_MATERIAL LANE=lane1 MATERIAL=PLA",
            "  SET_WEIGHT LANE=lane1 WEIGHT=1000",
        ]
        assert [c.args[0] for c in unit.afc.gcode.run_script_from_command.call_args_list] == [
            "SET_COLOR LANE=lane1 COLOR=E7A18E", "SET_MATERIAL LANE=lane1 MATERIAL=PLA",
            "SET_WEIGHT LANE=lane1 WEIGHT=1000"]

    def test_report_only(self):
        unit, lane = self._unit(found=tag())
        cmd = gcmd(APPLY=0)
        unit.cmd_AFC_CFS_READ_TAG(lane, cmd)
        assert cmd.respond_info.call_count == 1
        assert not unit.afc.gcode.run_script_from_command.called

    def test_no_tag(self):
        unit, lane = self._unit(found=None)
        cmd = gcmd()
        unit.cmd_AFC_CFS_READ_TAG(lane, cmd)
        cmd.respond_info.assert_called_once_with("CFS lane1 (slot A): no tag found")

    def test_spin_off_by_parameter(self):
        unit, lane = self._unit()
        unit.cmd_AFC_CFS_READ_TAG(lane, gcmd(SPIN=0))
        assert unit.read_tag.calls == [((lane,), {"spin": False})]

    def test_spin_off_by_config(self):
        unit, lane = self._unit({"rfid_spin": False})
        cmd = gcmd()
        unit.cmd_AFC_CFS_READ_TAG(lane, cmd)
        assert unit.read_tag.calls == [((lane,), {"spin": False})]
        cmd.get_int.assert_any_call("SPIN", 0, minval=0, maxval=1)

    def test_offline_box_connects(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = lambda: setattr(unit, "online", True) or True
        unit.cmd_AFC_CFS_READ_TAG(lane, gcmd())
        assert unit.read_tag.called

    def test_offline_box(self):
        unit, lane = self._unit()
        unit.online = False
        unit.connect = Recorder(result=False)
        with pytest.raises(CommandError) as e:
            unit.cmd_AFC_CFS_READ_TAG(lane, gcmd())
        assert str(e.value) == "CFS lane1: box is offline"
        assert not unit.read_tag.called

    def test_blocked(self):
        unit, lane = self._unit()
        lane.tool_loaded = True
        with pytest.raises(CommandError) as e:
            unit.cmd_AFC_CFS_READ_TAG(lane, gcmd())
        assert str(e.value) == "CFS lane1: can't read the tag now (lane is loaded)"
        assert not unit.read_tag.called


# Insert and remove

class TestAfcCFSOnFilamentInsert:
    def test_afc_event_and_no_read_by_default(self):
        unit, (lane,) = rfid_unit()
        seen = Recorder()
        unit.printer.register_event_handler("afc:lane_inserted", seen)
        unit.on_filament_insert(lane)
        assert seen.calls == [((lane,), {})]
        assert unit._rfid_queue == {}

    def test_queues_a_read(self):
        unit, (lane,) = rfid_unit({"rfid_read": True, "rfid_insert_delay": 7.0})
        unit.on_filament_insert(lane)
        assert unit._rfid_queue == {"lane1": 1007.0}


class TestAfcCFSOnFilamentRemove:
    def test_forgets_the_tag_and_queued_read(self):
        unit, (lane1, lane2) = rfid_unit(lanes=(1, 2))
        unit._rfid_queue = {"lane1": 5.0, "lane2": 6.0}
        unit.lane_tags = {"lane1": tag(), "lane2": None}
        unit.on_filament_remove(lane1)
        assert (unit._rfid_queue, unit.lane_tags) == ({"lane2": 6.0}, {"lane2": None})

    def test_nothing_known(self):
        unit, (lane,) = rfid_unit()
        unit.on_filament_remove(lane)
        assert (unit._rfid_queue, unit.lane_tags) == ({}, {})


class TestAfcCFSQueueStartupTagReads:
    def _unit(self, **values):
        opts = {"rfid_read_at_startup": True, "rfid_startup_delay": 15.0}
        opts.update(values)
        unit, lanes = rfid_unit(opts, lanes=(1, 2, 3))
        unit.material_mask = 0b0101
        return unit, lanes

    def test_queues_full_slots_once(self):
        unit, _ = self._unit()
        unit._queue_startup_tag_reads()
        assert unit._rfid_queue == {"lane1": 1015.0, "lane3": 1015.0}
        assert unit._rfid_startup_queued is True
        unit._rfid_queue.clear()
        unit._queue_startup_tag_reads()
        assert unit._rfid_queue == {}

    def test_off_by_default(self):
        unit, _ = self._unit(rfid_read_at_startup=False)
        unit._queue_startup_tag_reads()
        assert (unit._rfid_queue, unit._rfid_startup_queued) == ({}, False)

    def test_waits_for_presence(self):
        unit, _ = self._unit()
        unit.material_mask = None
        unit._queue_startup_tag_reads()
        assert (unit._rfid_queue, unit._rfid_startup_queued) == ({}, False)

    def test_no_lanes(self):
        unit, _ = self._unit()
        unit.lanes = {}
        unit._queue_startup_tag_reads()
        assert unit._rfid_startup_queued is False

    def test_waits_for_prep(self):
        unit, lanes = self._unit()
        lanes[1]._afc_prep_done = False
        unit._queue_startup_tag_reads()
        assert (unit._rfid_queue, unit._rfid_startup_queued) == ({}, False)


class TestAfcCFSQueueTagRead:
    def test_first_read_starts_the_timer(self):
        unit, (lane,) = rfid_unit()
        unit.queue_tag_read(lane, 10.0)
        timer = unit.reactor.timers[-1]
        assert (timer.callback, timer.waketime) == (unit._rfid_tick, 1010.0)
        assert unit._rfid_timer is timer
        assert unit._rfid_queue == {"lane1": 1010.0}

    def test_later_reads_move_the_timer_to_the_earliest(self):
        unit, (lane1, lane2) = rfid_unit(lanes=(1, 2))
        unit.queue_tag_read(lane1, 10.0)
        unit.queue_tag_read(lane2, 4.0)
        assert unit.reactor.updated == [(unit._rfid_timer, 1004.0)]
        assert unit._rfid_queue == {"lane1": 1010.0, "lane2": 1004.0}

    def test_no_delay(self):
        unit, (lane,) = rfid_unit()
        unit.queue_tag_read(lane)
        assert unit._rfid_queue == {"lane1": 1000.0}


class TestAfcCFSJobActive:
    @pytest.mark.parametrize("state,active", [("printing", True), ("paused", True),
                                              ("standby", False), ("complete", False)])
    def test_states(self, state, active):
        unit, _ = rfid_unit()
        unit.printer._objects["print_stats"] = PrintStats(state)
        assert unit._job_active() is active

    def test_no_print_stats(self):
        unit, _ = rfid_unit()
        assert unit._job_active() is False

    def test_stats_error(self):
        unit, _ = rfid_unit()
        unit.printer._objects["print_stats"] = PrintStats(raises=True)
        assert unit._job_active() is False


class TestAfcCFSTagReadBlocker:
    def _unit(self):
        unit, lanes = rfid_unit(lanes=(1, 2))
        unit.material_mask = 0b0011
        unit.connected_mask = 0
        return unit, lanes

    def test_free(self):
        unit, (lane, _) = self._unit()
        assert unit.tag_read_blocker(lane) is None

    def test_masks_unknown(self):
        unit, (lane, _) = self._unit()
        unit.material_mask = None
        unit.connected_mask = None
        assert unit.tag_read_blocker(lane) is None

    def test_printing(self):
        unit, (lane, _) = self._unit()
        unit.printer._objects["print_stats"] = PrintStats("printing")
        assert unit.tag_read_blocker(lane) == "a print is running"

    def test_offline(self):
        unit, (lane, _) = self._unit()
        unit.online = False
        assert unit.tag_read_blocker(lane) == "box offline"

    def test_box_running_a_sequence(self):
        unit, (lane, _) = self._unit()
        unit._operation_depth = 1
        assert unit.tag_read_blocker(lane) == "box busy"

    def test_line_busy(self):
        unit, (lane, _) = self._unit()
        unit.transport._mutex.locked = True
        assert unit.tag_read_blocker(lane) == "box busy"

    def test_lane_in_the_toolhead(self):
        unit, (lane, _) = self._unit()
        lane.tool_loaded = True
        assert unit.tag_read_blocker(lane) == "lane is loaded"

    def test_lane_past_the_merge(self):
        unit, (lane, _) = self._unit()
        unit.connected_mask = 0b0001
        assert unit.tag_read_blocker(lane) == "lane is loaded"

    def test_other_slot_past_the_merge(self):
        unit, (lane, _) = self._unit()
        unit.connected_mask = 0b0010
        assert unit.tag_read_blocker(lane) == "another slot of this box is loaded"

    def test_other_lane_in_the_toolhead(self):
        unit, (lane, other) = self._unit()
        other.tool_loaded = True
        assert unit.tag_read_blocker(lane) == "another slot of this box is loaded"

    def test_empty_slot(self):
        unit, (lane, _) = self._unit()
        unit.material_mask = 0b0010
        assert unit.tag_read_blocker(lane) == "slot is empty"


class TestAfcCFSRfidTick:
    def _unit(self):
        unit, lanes = rfid_unit(lanes=(1, 2))
        unit.material_mask = 0b0011
        unit.connected_mask = 0
        unit._rfid_timer = "timer"
        unit.read_tag = Recorder(result=None)
        unit.afc.gcode.run_script = Recorder()
        return unit, lanes

    def test_nothing_queued(self):
        unit, _ = self._unit()
        assert unit._rfid_tick(1000.0) == unit.reactor.NEVER
        assert unit._rfid_timer is None

    def test_not_due_yet(self):
        unit, _ = self._unit()
        unit._rfid_queue = {"lane1": 1010.0, "lane2": 1005.0}
        assert unit._rfid_tick(1000.0) == 1005.0
        assert not unit.read_tag.called

    def test_lane_gone(self):
        unit, _ = self._unit()
        unit._rfid_queue = {"lane9": 900.0, "lane1": 1010.0}
        assert unit._rfid_tick(1000.0) == 1000.0
        assert unit._rfid_queue == {"lane1": 1010.0}

    def test_reads_and_applies_the_tag(self):
        unit, (lane, _) = self._unit()
        unit.read_tag = Recorder(result=tag())
        unit._rfid_queue = {"lane1": 990.0}
        assert unit._rfid_tick(1000.0) == unit.reactor.NEVER
        assert unit.read_tag.calls == [((lane,), {})]
        assert [c[0][0] for c in unit.afc.gcode.run_script.calls] == [
            "SET_COLOR LANE=lane1 COLOR=E7A18E", "SET_MATERIAL LANE=lane1 MATERIAL=PLA",
            "SET_WEIGHT LANE=lane1 WEIGHT=1000"]
        assert (unit._rfid_queue, unit._rfid_timer) == ({}, None)

    def test_no_tag_nothing_applied(self):
        unit, _ = self._unit()
        unit._rfid_queue = {"lane1": 990.0, "lane2": 1020.0}
        assert unit._rfid_tick(1000.0) == 1020.0
        assert not unit.afc.gcode.run_script.called
        assert unit._rfid_timer == "timer"

    @pytest.mark.parametrize("setup", ["empty", "loaded"])
    def test_dropped_when_it_cannot_happen(self, setup):
        unit, (lane, _) = self._unit()
        if setup == "empty":
            unit.material_mask = 0b0010
        else:
            lane.tool_loaded = True
        unit._rfid_queue = {"lane1": 990.0}
        unit._rfid_tick(1000.0)
        assert unit._rfid_queue == {}
        assert not unit.read_tag.called

    def test_retried_later_when_busy(self):
        unit, _ = self._unit()
        unit.printer._objects["print_stats"] = PrintStats("printing")
        unit._rfid_queue = {"lane1": 990.0}
        assert unit._rfid_tick(1000.0) == 1010.0
        assert unit._rfid_queue == {"lane1": 1010.0}

    def test_read_error_is_a_warning(self):
        unit, _ = self._unit()
        unit.read_tag = Recorder(raises=RuntimeError("serial gone"))
        unit._rfid_queue = {"lane1": 990.0}
        unit._rfid_tick(1000.0)
        level, text = msgs(unit)[-1]
        assert level == "warning"
        assert text.startswith("CFS lane1: tag read failed\nTraceback")
        assert text.rstrip().endswith("RuntimeError: serial gone")


class TestAfcCFSReadTag:
    def _unit(self, values=None):
        unit, lanes = rfid_unit(values=values, lanes=(1, 4))
        unit.wait_for_tag_reads = Recorder()
        return unit, lanes[1]

    def test_turns_the_slot_and_reads(self):
        unit, lane = self._unit()
        busy = []
        unit.box.on(P.SET_PRE_LOADING, lambda req: (busy.append(unit._rfid_busy),
                                                    frame(P.SET_PRE_LOADING))[1])
        unit.box.reply(P.GET_RFID, frame(P.GET_RFID, b"D:" + PINK_SILK.encode() + b";"))
        found = unit.read_tag(lane)
        assert found == P.decode_rfid_record(PINK_SILK)
        assert unit.box.keys() == [(1, P.SET_PRE_LOADING, b"\x08\x02"),
                                   (1, P.GET_RFID, b"\x08"),
                                   (1, P.SET_PRE_LOADING, b"\x0f\x00")]
        assert unit.box.sent[0].kwargs == {"timeout": 150.0, "retries": 0, "motor": True,
                                           "quiet": False}
        assert unit.box.sent[1].kwargs["timeout"] == 2.0
        assert busy == [True, True]
        assert unit._rfid_busy is False
        assert unit.lane_tags["lane4"] == found
        assert unit.trace.events == [("rfid", {"lane": "lane4", "slot": "D", "spin": True,
                                               "replies": [f"D:{PINK_SILK};"], "tag": found})]
        assert msgs(unit) == [("info", "CFS lane4: slot D tag: PLA, #E7A18E, 1000 g")]
        assert unit._operation_depth == 0

    def test_without_turning(self):
        unit, lane = self._unit()
        unit.box.reply(P.GET_RFID, frame(P.GET_RFID, b"D:" + PINK_SILK.encode() + b";"))
        unit.read_tag(lane, spin=False)
        assert unit.box.fns() == [P.GET_RFID]

    def test_turning_off_by_config(self):
        unit, lane = self._unit({"rfid_spin": False})
        unit.read_tag(lane)
        assert P.SET_PRE_LOADING not in unit.box.fns()

    def test_no_tag_after_every_try(self):
        unit, lane = self._unit({"rfid_read_attempts": 3, "rfid_retry_interval": 5.0})
        unit.box.reply(P.GET_RFID, frame(P.GET_RFID, b"D:unknown;"), None,
                       frame(P.GET_RFID, b"D:unknown;"))
        assert unit.read_tag(lane) is None
        assert unit.box.fns().count(P.GET_RFID) == 3
        assert unit.reactor.pauses == [1005.0, 1010.0]          # none after the last try
        assert unit.lane_tags["lane4"] is None
        assert unit.trace.events[0][1]["replies"] == ["D:unknown;", "D:unknown;"]
        assert msgs(unit) == [("debug", "CFS lane4: slot D tag: none")]

    def test_tag_on_a_retry(self):
        unit, lane = self._unit()
        unit.box.reply(P.GET_RFID, frame(P.GET_RFID, b"D:unknown;"),
                       frame(P.GET_RFID, b"D:" + PINK_SILK.encode() + b";"))
        assert unit.read_tag(lane) is not None
        assert unit.box.fns().count(P.GET_RFID) == 2

    def test_extra_material_codes_used(self):
        unit, lane = self._unit({"rfid_materials": "00002=PLA-SILK"})
        unit.box.reply(P.GET_RFID, frame(P.GET_RFID, b"D:" + PINK_SILK.encode() + b";"))
        assert unit.read_tag(lane)["material"] == "PLA-SILK"

    def test_busy_flag_cleared_on_error(self):
        unit, lane = self._unit()
        unit.box.on(P.GET_RFID, lambda req: (_ for _ in ()).throw(RuntimeError("gone")))
        with pytest.raises(RuntimeError):
            unit.read_tag(lane)
        assert unit._rfid_busy is False
        assert unit._operation_depth == 0


class TestAfcCFSTagSummary:
    def test_none(self):
        assert P.afcCFS._tag_summary(None) == "none"

    def test_full(self):
        assert P.afcCFS._tag_summary(tag(spool_id=7)) == "PLA, #E7A18E, 1000 g, Spoolman spool 7"

    def test_unknown_material_and_nothing_else(self):
        assert P.afcCFS._tag_summary(tag(material=None, color=None, weight=None)) == (
            "material code 00002")


class TestAfcCFSApplyTag:
    def _unit(self):
        unit, (lane,) = rfid_unit()
        return unit, lane, Recorder()

    def test_color_material_weight(self):
        unit, lane, run = self._unit()
        cmds = unit.apply_tag(lane, tag(), run)
        assert cmds == ["SET_COLOR LANE=lane1 COLOR=E7A18E",
                        "SET_MATERIAL LANE=lane1 MATERIAL=PLA",
                        "SET_WEIGHT LANE=lane1 WEIGHT=1000"]
        assert [c[0][0] for c in run.calls] == cmds
        assert msgs(unit) == []

    def test_spoolman_id_wins(self):
        unit, lane, run = self._unit()
        unit.afc.spoolman = "http://spoolman"
        assert unit.apply_tag(lane, tag(spool_id=7), run) == [
            "SET_SPOOL_ID LANE=lane1 SPOOL_ID=7"]

    def test_spool_id_without_spoolman(self):
        unit, lane, run = self._unit()
        assert unit.apply_tag(lane, tag(spool_id=7), run)[0] == "SET_COLOR LANE=lane1 COLOR=E7A18E"

    def test_lane_already_has_a_spool(self):
        unit, lane, run = self._unit()
        lane.spool_id = 1
        assert unit.apply_tag(lane, tag(), run) == []
        assert not run.called
        assert msgs(unit) == [("info", "CFS lane1: lane has Spoolman spool 1; tag not applied")]

    def test_unknown_material_code(self):
        unit, lane, run = self._unit()
        cmds = unit.apply_tag(lane, tag(material=None, material_code="09999"), run)
        assert cmds == ["SET_COLOR LANE=lane1 COLOR=E7A18E", "SET_WEIGHT LANE=lane1 WEIGHT=1000"]
        assert msgs(unit) == [("info", "CFS lane1: material code 09999 isn't known; add it to "
                                       "rfid_materials in [AFC_CFS CFS_1], e.g. rfid_materials: "
                                       "09999=PETG")]

    def test_nothing_usable(self):
        unit, lane, run = self._unit()
        empty = tag(material=None, material_code="", color=None, weight=None)
        assert unit.apply_tag(lane, empty, run) == []
        assert msgs(unit) == []


class TestAfcCFSApplyLaneState:
    def _unit(self):
        unit, (lane,) = rfid_unit()
        for name in ("lane_not_ready", "lane_loaded", "lane_tool_loaded",
                     "lane_tool_loaded_idle", "ensure_print_mode"):
            setattr(unit, name, Recorder())
        return unit, lane

    def test_empty_slot(self):
        unit, lane = self._unit()
        unit.connected_mask = 0
        assert unit.apply_lane_state(lane, False) == "EMPTY READY FOR SPOOL"
        assert (lane.prep_state, lane.loaded_to_hub, lane._load_state) == (False, False, False)
        assert unit.lane_not_ready.calls == [((lane,), {})]
        assert not unit.lane_loaded.called

    def test_spool_in_the_slot(self):
        unit, lane = self._unit()
        unit.connected_mask = 0b0001
        assert unit.apply_lane_state(lane, True) == "FILAMENT PRESENT"
        assert (lane.prep_state, lane._load_state) == (True, True)
        assert unit.lane_loaded.calls == [((lane,), {})]
        assert not unit.ensure_print_mode.called

    def test_current_tool(self):
        unit, lane = self._unit()
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = "lane1"
        unit.afc.current = "lane1"
        assert unit.apply_lane_state(lane, True) == "FILAMENT PRESENT in ToolHead"
        assert unit.ensure_print_mode.calls == [((lane,), {})]
        assert unit.lane_tool_loaded.called and not unit.lane_tool_loaded_idle.called

    def test_loaded_but_not_the_current_tool(self):
        unit, lane = self._unit()
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = "lane1"
        unit.afc.current = "lane5"
        assert unit.apply_lane_state(lane, True) == "FILAMENT PRESENT in ToolHead"
        assert unit.lane_tool_loaded_idle.called and not unit.lane_tool_loaded.called

    def test_extruder_holds_another_lane(self):
        unit, lane = self._unit()
        lane.tool_loaded = True
        lane.extruder_obj.lane_loaded = "lane2"
        assert unit.apply_lane_state(lane, True) == "FILAMENT PRESENT"
        assert not unit.ensure_print_mode.called

    def test_no_extruder(self):
        unit, lane = self._unit()
        lane.tool_loaded = True
        lane.extruder_obj = None
        assert unit.apply_lane_state(lane, True) == "FILAMENT PRESENT"
        assert not unit.ensure_print_mode.called


# ============================================================================
# G-code commands, calibration and config loading
# ============================================================================

def raw(unit):
    return [m for lvl, m in msgs(unit) if lvl == "raw"]


# Module import guards

def _exec_with_blocked_dependency(blocked: str) -> None:
    """Run a throw-away copy of AFC_CFS.py with one import forced to fail."""
    fresh_name = "extras.AFC_CFS_import_guard_probe"
    original = sys.modules.get(blocked)
    sys.modules[blocked] = None
    try:
        spec = importlib.util.spec_from_file_location(fresh_name, P.__file__)
        fresh = importlib.util.module_from_spec(spec)
        sys.modules[fresh_name] = fresh
        try:
            spec.loader.exec_module(fresh)
        finally:
            sys.modules.pop(fresh_name, None)
    finally:
        if original is not None:
            sys.modules[blocked] = original
        else:
            sys.modules.pop(blocked, None)


class TestModuleImportGuards:
    def test_afc_utils(self):
        with pytest.raises(P.CONFIG_ERROR) as e:
            _exec_with_blocked_dependency("extras.AFC_utils")
        assert str(e.value).startswith("Error when trying to import AFC_utils.ERROR_STR")

    @pytest.mark.parametrize("module", ["AFC_unit", "AFC_lane", "AFC_respond"])
    def test_afc_modules(self, module):
        with pytest.raises(P.CONFIG_ERROR) as e:
            _exec_with_blocked_dependency(f"extras.{module}")
        assert str(e.value).startswith(f"Error trying to import {module}, please rerun "
                                       f"install-afc.sh")


# Unit hooks AFC calls

class TestAfcCFSPrepLoad:
    def test_nothing_to_do(self):
        unit, (lane,) = ready_unit()
        assert unit.prep_load(lane) is None
        assert unit.box.sent == []


class TestAfcCFSPrepPostLoad:
    def test_nothing_to_do(self):
        unit, (lane,) = ready_unit()
        assert unit.prep_post_load(lane) is None
        assert unit.box.sent == []


class TestAfcCFSEjectLane:
    def test_done(self):
        unit, (lane,) = ready_unit()
        unit.reel_back = Recorder(result=(True, "retrude stages OK/OK, box finished"))
        unit.eject_lane(lane)
        assert msgs(unit) == [("info", "CFS CFS_1: eject lane1: retrude stages OK/OK, box "
                                       "finished")]

    def test_failed(self):
        unit, (lane,) = ready_unit()
        unit.reel_back = Recorder(result=(False, "lane is loaded in the toolhead"))
        unit.eject_lane(lane)
        assert msgs(unit) == [("error", "CFS CFS_1: eject lane1: lane is loaded in the toolhead")]


class TestAfcCFSReelBack:
    def _unit(self):
        unit, (_, lane) = ready_unit(lanes=(1, 2))
        lane._load_state = True
        return unit, lane

    def test_reels_back(self):
        unit, lane = self._unit()
        unit.reel_slot = Recorder(result=(True, ["OK", "OK"]))
        assert unit.reel_back(lane) == (
            True, "retrude stages OK/OK, box finished, toolhead sensor clear")
        assert unit.reel_slot.calls == [((0x02,), {})]
        assert lane._load_state is False
        assert unit.trace.events == [
            ("eject_start", {"lane": "lane2", "slot": "B"}),
            ("eject_end", {"lane": "lane2", "ok": True, "seconds": 0.0,
                           "detail": "retrude stages OK/OK, box finished, toolhead sensor clear"}),
        ]
        assert unit._operation_depth == 0

    def test_box_did_not_confirm(self):
        unit, lane = self._unit()
        unit.reel_slot = Recorder(result=(False, ["NO_REPLY"]))
        assert unit.reel_back(lane) == (
            False, "retrude stages NO_REPLY, box did not confirm, toolhead sensor clear")
        assert lane._load_state is True

    def test_filament_still_at_the_toolhead(self):
        unit, lane = self._unit()
        lane.extruder_obj.tool_start_state = True
        unit.reel_slot = Recorder(result=(True, ["OK", "OK"]))
        assert unit.reel_back(lane) == (
            False, "retrude stages OK/OK, box finished, toolhead sensor STILL TRIGGERED")
        assert lane._load_state is False

    def test_loaded_lane_refused(self):
        unit, lane = self._unit()
        lane.tool_loaded = True
        unit.reel_slot = Recorder()
        assert unit.reel_back(lane) == (
            False, "lane is loaded in the toolhead; use AFC_CFS_TOOL_UNLOAD")
        assert not unit.reel_slot.called
        assert unit.trace.events == []


class TestAfcCFSReelSlot:
    def test_reels_back(self):
        unit, _ = ready_unit()
        assert unit.reel_slot(0x04) == (True, ["OK", "OK"])
        assert unit.box.keys() == [(1, P.SET_PRE_LOADING, b"\x0f\x00"),
                                   (1, P.SET_BOX_MODE, b"\x00\x01"),
                                   (1, P.RETRUDE_PROCESS, b"\x04\x00"),
                                   (1, P.RETRUDE_PROCESS, b"\x04\x01"),
                                   (1, P.SET_BOX_MODE, b"\x00\x01")]
        assert unit.reactor.pauses == pytest.approx([1000.3, 1000.6])

    def test_start_not_answered(self):
        unit, _ = ready_unit()
        unit.box.reply(P.RETRUDE_PROCESS, None)
        assert unit.reel_slot(0x04) == (False, ["NO_REPLY"])
        assert unit.box.fns().count(P.RETRUDE_PROCESS) == 1
        assert unit.box.fns()[-1] == P.SET_BOX_MODE

    def test_failed_start_still_finishes(self):
        unit, _ = ready_unit()
        unit.box.reply(P.RETRUDE_PROCESS, frame(P.RETRUDE_PROCESS, status=P.ST_RETRUDE_ERR2))
        assert unit.reel_slot(0x04) == (True, ["RETRUDE_ERR2", "OK"])

    def test_finish_not_answered(self):
        unit, _ = ready_unit()
        unit.box.reply(P.RETRUDE_PROCESS, frame(P.RETRUDE_PROCESS), None)
        assert unit.reel_slot(0x04) == (False, ["OK", "NO_REPLY"])

    def test_finish_error(self):
        unit, _ = ready_unit()
        unit.box.reply(P.RETRUDE_PROCESS, frame(P.RETRUDE_PROCESS),
                       frame(P.RETRUDE_PROCESS, status=P.ST_RETRUDE_ERR7))
        assert unit.reel_slot(0x04) == (False, ["OK", "RETRUDE_ERR7"])


class TestAfcCFSMoveToHub:
    def test_not_used(self):
        unit, (lane,) = ready_unit()
        assert unit.move_to_hub(lane, 100.0, None) == (False, 0, AFCMoveWarning.ERROR)
        assert msgs(unit) == [("debug", "CFS CFS_1: move_to_hub not supported, box handles "
                                        "feeding")]


class TestAfcCFSLoadThenHome:
    def test_not_used(self):
        unit, (lane,) = ready_unit()
        assert unit.load_then_home(lane, 100.0, None, None) == (False, 0, AFCMoveWarning.ERROR)
        assert msgs(unit) == [("debug", "CFS CFS_1: load_then_home not supported, box handles "
                                        "feeding")]


NOTHING = "CFS lanes are fed by the box itself; there is nothing to calibrate."


class TestAfcCFSCalibrateLane:
    def test_nothing_to_calibrate(self):
        unit, (lane,) = ready_unit()
        assert unit.calibrate_lane(lane, 1.0) == (False, NOTHING, 0.0)


class TestAfcCFSCalibrateBowden:
    def test_nothing_to_calibrate(self):
        unit, (lane,) = ready_unit()
        assert unit.calibrate_bowden(lane, 900.0, 1.0) == (False, NOTHING, 0.0)


class TestAfcCFSCalibrateTd1:
    def test_nothing_to_calibrate(self):
        unit, (lane,) = ready_unit()
        assert unit.calibrate_td1(lane, 900.0, 1.0) == (False, NOTHING, 0.0)


PROMPT = ["// action:prompt_begin CFS_1 Calibration",
          f"// action:prompt_text {NOTHING}",
          "// action:prompt_footer_button Cancel|RESPOND TYPE=command "
          "MSG=action:prompt_end|warning",
          "// action:prompt_show"]


class TestAfcCFSCmdUnitCalibration:
    def test_prompt(self):
        unit, _ = ready_unit()
        unit.cmd_UNIT_CALIBRATION(gcmd())
        assert raw(unit) == PROMPT


class TestAfcCFSCmdUnitLaneCalibration:
    def test_prompt(self):
        unit, _ = ready_unit()
        unit.cmd_UNIT_LANE_CALIBRATION(gcmd())
        assert raw(unit) == PROMPT


class TestAfcCFSCmdUnitBowCalibration:
    def test_prompt(self):
        unit, _ = ready_unit()
        unit.cmd_UNIT_BOW_CALIBRATION(gcmd())
        assert raw(unit) == PROMPT


class TestAfcCFSCmdAfcUnitTdOneCalibration:
    def test_prompt(self):
        unit, _ = ready_unit()
        unit.cmd_AFC_UNIT_TD_ONE_CALIBRATION(gcmd())
        assert raw(unit) == PROMPT


# Status

class TestAfcCFSCmdAfcCfsStatus:
    def _unit(self, values=None, lanes=(1, 2)):
        unit, made = ready_unit(values=values, lanes=lanes)
        unit.version_sn = "11350000232175B625DGIB"
        unit.seen_uid = UID_A
        sensor_banks = {b"\x00": b"\x01", b"\x01": b"\x01"}
        unit.box.on(P.GET_FILAMENT_SENSOR_STATE,
                    lambda req: frame(P.GET_FILAMENT_SENSOR_STATE, sensor_banks[req.data]))
        unit.box.reply(P.GET_BUFFER_STATE, frame(P.GET_BUFFER_STATE, b"\x01"))
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE))
        unit.box.reply(P.GET_MEASURING_WHEEL, frame(P.GET_MEASURING_WHEEL,
                                                    bytes.fromhex("c5afa071")))
        return unit, made

    def _lines(self, cmd):
        (call,) = cmd.respond_info.call_args_list
        return call.args[0].split("\n")

    def test_full_report(self):
        unit, _ = self._unit()
        unit.lane_tags = {"lane1": P.decode_rfid_record(PINK_SILK)}
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        assert self._lines(cmd) == [
            f"CFS CFS_1 @ {SERIAL} addr 1",
            "  version: 11350000232175B625DGIB",
            f"  box uid: {UID_A.hex()}",
            "  box state: OK",
            "  filament present: A",
            "  connected slot: A",
            "  buffer: full",
            "  measuring wheel: -5620.06 mm",
            "  cutter sensor: not configured",
            "  link: tx 0 rx 0 timeouts 0 crc errors 0",
            "  trace: off (set trace_file to enable)",
            "  lane1 -> slot A: present, tag: PLA, #E7A18E, 1000 g",
            "  lane2 -> slot B: empty",
        ]
        assert unit._operation_depth == 0

    def test_unknown_values(self):
        unit, _ = self._unit(values={"box_uid": UID_B.hex()}, lanes=(1,))
        unit.version_sn = None
        unit.seen_uid = None
        unit.box._handlers.clear()
        unit.box._queues.clear()
        unit.box.default_ok = False
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        lines = self._lines(cmd)
        assert lines[1:9] == [
            "  version: unknown",
            f"  box uid: ?{f' (box_uid is {UID_B.hex()})'}",
            "  box state: NO_REPLY",
            "  filament present: ?",
            "  connected slot: ?",
            "  buffer: NO_REPLY",
            "  measuring wheel: ?",
            "  cutter sensor: not configured",
        ]
        assert lines[-1] == "  lane1 -> slot A: empty"

    def test_matching_box_uid_has_no_note(self):
        unit, _ = self._unit(values={"box_uid": UID_A.hex()})
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        assert self._lines(cmd)[2] == f"  box uid: {UID_A.hex()}"

    def test_no_slots_and_odd_buffer_value(self):
        unit, _ = self._unit()
        unit.box._handlers.clear()
        unit.box.reply(P.GET_FILAMENT_SENSOR_STATE, frame(P.GET_FILAMENT_SENSOR_STATE, b"\x00"),
                       frame(P.GET_FILAMENT_SENSOR_STATE, b"\x00"))
        unit.box._queues[P.GET_BUFFER_STATE] = [frame(P.GET_BUFFER_STATE, b"\x07")]
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        lines = self._lines(cmd)
        assert lines[4:7] == ["  filament present: none", "  connected slot: none",
                              "  buffer: 0x07"]

    @pytest.mark.parametrize("state,text", [(True, "triggered"), (False, "open")])
    def test_cutter_sensor(self, state, text):
        unit, _ = self._unit(values={"cutter_sensor_pin": "PA1"})
        unit.cutter_sensor_state = state
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        assert self._lines(cmd)[8] == f"  cutter sensor: {text}"

    def test_offline_tries_to_connect(self):
        unit, _ = self._unit()
        unit.online = False
        unit.connect = lambda: setattr(unit, "online", True)
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        assert self._lines(cmd)[0] == f"CFS CFS_1 @ {SERIAL} addr 1"

    def test_offline(self):
        unit, _ = self._unit()
        unit.online = False
        unit.connect = Recorder()
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        cmd.respond_info.assert_called_once_with("CFS CFS_1: offline (no reply)")
        assert unit.box.sent == []
        assert unit._operation_depth == 0

    def test_offline_with_error_text(self):
        unit, _ = self._unit()
        unit.online = False
        unit.connect = Recorder()
        unit.transport.last_error = "open /dev/fake_cfs failed: gone"
        cmd = gcmd()
        unit.cmd_AFC_CFS_STATUS(cmd)
        cmd.respond_info.assert_called_once_with(
            "CFS CFS_1: offline (open /dev/fake_cfs failed: gone)")


class TestAfcCFSCmdAfcCfsReconnect:
    def test_single_box_reopens_the_port(self):
        unit, _ = ready_unit()
        unit.transport.close = Recorder()
        seen = []
        unit.connect = lambda: (seen.append(unit.online), True)[1]
        cmd = gcmd()
        unit.cmd_AFC_CFS_RECONNECT(cmd)
        assert unit.transport.close.called
        assert seen == [False]
        cmd.respond_info.assert_called_once_with("CFS CFS_1: online")
        assert unit._operation_depth == 0

    def test_chain_keeps_the_port_open(self):
        printer = make_printer()
        unit, _ = ready_unit(printer=printer)
        make_unit("CFS_2", printer=printer, values={"box_addr": 2})
        unit.transport.close = Recorder()
        unit.connect = Recorder(result=True)
        unit.cmd_AFC_CFS_RECONNECT(gcmd())
        assert not unit.transport.close.called

    def test_still_offline(self):
        unit, _ = ready_unit()
        unit.transport.close = Recorder()
        unit.connect = Recorder(result=False)
        cmd = gcmd()
        unit.cmd_AFC_CFS_RECONNECT(cmd)
        cmd.respond_info.assert_called_once_with("CFS CFS_1: offline (no reply)")

    def test_still_offline_with_error_text(self):
        unit, _ = ready_unit()
        unit.transport.close = Recorder()
        unit.connect = Recorder(result=False)
        unit.transport.last_error = "read error: gone"
        cmd = gcmd()
        unit.cmd_AFC_CFS_RECONNECT(cmd)
        cmd.respond_info.assert_called_once_with("CFS CFS_1: offline (read error: gone)")


class TestAfcCFSTraceSummary:
    def test_no_trace_file(self):
        unit, _ = ready_unit()
        assert unit._trace_summary() == "off (set trace_file to enable)"

    def test_paused(self):
        unit, _ = ready_unit(values={"trace_file": "/tmp/cfs_trace.jsonl"})
        unit.trace = RecTrace(enabled=False)
        assert unit._trace_summary() == "paused (/tmp/cfs_trace.jsonl)"

    def test_stopped_by_an_error(self):
        unit, _ = ready_unit(values={"trace_file": "/tmp/cfs_trace.jsonl"})
        unit.trace = RecTrace(enabled=False)
        unit.trace.last_error = "cannot open trace file: denied"
        assert unit._trace_summary() == "paused (cannot open trace file: denied)"

    def test_recording(self):
        unit, _ = ready_unit(values={"trace_file": "/tmp/cfs_trace.jsonl"})
        unit.trace.written = 12
        assert unit._trace_summary() == "recording to /tmp/cfs_trace.jsonl (12 events)"

    def test_recording_with_drops(self):
        unit, _ = ready_unit(values={"trace_file": "/tmp/cfs_trace.jsonl"})
        unit.trace.written = 12
        unit.trace.dropped = 3
        assert unit._trace_summary() == "recording to /tmp/cfs_trace.jsonl (12 events, 3 dropped)"


class TestAfcCFSCmdAfcCfsTrace:
    def _unit(self, trace_file="/tmp/cfs_trace.jsonl"):
        values = {"trace_file": trace_file} if trace_file else None
        unit, _ = ready_unit(values=values)
        return unit

    def test_report_only(self):
        unit = self._unit()
        cmd = gcmd()
        unit.cmd_AFC_CFS_TRACE(cmd)
        assert unit.trace.events == []
        cmd.respond_info.assert_called_once_with(
            "CFS CFS_1 trace: recording to /tmp/cfs_trace.jsonl (0 events)")

    def test_resume(self):
        unit = self._unit()
        unit.trace.enabled = False
        unit.cmd_AFC_CFS_TRACE(gcmd(ENABLE=1))
        assert unit.trace.enabled is True
        assert unit.trace.events == [("trace_resume", {})]

    def test_resume_without_a_file(self):
        unit = self._unit(trace_file=None)
        with pytest.raises(CommandError) as e:
            unit.cmd_AFC_CFS_TRACE(gcmd(ENABLE=1))
        assert str(e.value) == "CFS CFS_1: set trace_file in [AFC_CFS CFS_1] first"

    def test_pause(self):
        unit = self._unit()
        cmd = gcmd(ENABLE=0)
        unit.cmd_AFC_CFS_TRACE(cmd)
        assert unit.trace.enabled is False
        assert unit.trace.events == [("trace_pause", {})]
        cmd.respond_info.assert_called_once_with("CFS CFS_1 trace: paused (/tmp/cfs_trace.jsonl)")

    def test_marker(self):
        unit = self._unit()
        unit.cmd_AFC_CFS_TRACE(gcmd(MARK="step 4 load A"))
        assert unit.trace.events == [("mark", {"text": "step 4 load A"})]


class TestAfcCFSCmdAfcCfsRaw:
    def test_reply(self):
        unit, _ = ready_unit()
        unit.box.reply(P.GET_BOX_STATE, frame(P.GET_BOX_STATE, b"\x11\x22"))
        cmd = gcmd(FN="0A")
        unit.cmd_AFC_CFS_RAW(cmd)
        (req,) = unit.box.sent
        assert req.key == (1, 0x0A, b"")
        assert req.kwargs == {"status": 0xFF, "timeout": 2.0, "retries": 0}
        # TX matches captured stock traffic for GET_BOX_STATE to box 1
        reply_hex = P.encode_frame(1, 0x00, 0x0A, b"\x11\x22").hex()
        cmd.respond_info.assert_called_once_with(
            f"TX f70103ff0a5c\nRX {reply_hex} status=OK data=1122")
        assert unit._operation_depth == 0

    def test_options_and_no_reply(self):
        unit, _ = ready_unit()
        unit.box.default_ok = False
        cmd = gcmd(FN="a2", DATA="", ADDR=2, STATUS="00", TIMEOUT=0.5)
        unit.cmd_AFC_CFS_RAW(cmd)
        (req,) = unit.box.sent
        assert req.key == (2, 0xA2, b"")
        assert req.kwargs == {"status": 0x00, "timeout": 0.5, "retries": 0}
        # Captured stock ONLINE_CHECK to box 2 is f7020300a2..; box 1's is f7010300a2da
        expected_tx = P.encode_frame(2, 0x00, 0xA2).hex()
        cmd.respond_info.assert_called_once_with(f"TX {expected_tx}\nRX (no reply)")

    def test_payload(self):
        unit, _ = ready_unit()
        cmd = gcmd(FN="04", DATA="0001")
        unit.cmd_AFC_CFS_RAW(cmd)
        assert unit.box.sent[0].key == (1, 0x04, b"\x00\x01")
        assert cmd.respond_info.call_args.args[0].startswith("TX f70105ff04000190\n")


class TestAfcCFSGetStatus:
    def test_reports_box_and_lanes(self):
        unit, (lane1, lane2) = ready_unit(lanes=(1, 2))
        unit.version_sn = "113500"
        unit.material_mask = 0b0011
        unit.connected_mask = 0b0001
        unit.buffer_state = P.BUFFER_MIDDLE
        unit.box_status = P.ST_EXTRUDE_ERR8
        unit.present_last = {"lane1": True, "lane2": None}
        unit.load_reports = {"lane1": {"slot": "A", "result": "loaded"}}
        unit.lane_tags = {"lane2": None}
        status = unit.get_status()
        assert status["lanes"] == ["lane1", "lane2"]
        assert status["cfs"] == {
            "online": True, "box_addr": 1, "version": "113500", "material_mask": 3,
            "connected_mask": 1, "buffer_state": 0, "box_status": "EXTRUDE_ERR8",
            "cutter_sensor": None, "trace": "/tmp/cfs_trace.jsonl",
            "lanes": {
                "lane1": {"slot": "A", "present": True,
                          "last_load": {"slot": "A", "result": "loaded"}, "tag": None},
                "lane2": {"slot": "B", "present": None, "last_load": {}, "tag": None},
            },
        }
        # A copy, so the report can't be changed through the status
        assert status["cfs"]["lanes"]["lane1"]["last_load"] is not unit.load_reports["lane1"]

    def test_nothing_known_yet(self):
        unit, _ = ready_unit(values={"cutter_sensor_pin": "PA1"})
        unit.trace = RecTrace(enabled=False)
        unit.cutter_sensor_state = True
        cfs = unit.get_status()["cfs"]
        assert (cfs["box_status"], cfs["cutter_sensor"], cfs["trace"]) == (None, True, None)


class TestLoadConfigPrefix:
    def test_builds_a_unit(self):
        printer = make_printer()
        printer._objects["AFC_hub CFS_1"] = object()
        config = MockConfig(name="AFC_CFS CFS_1", printer=printer,
                            values={"serial": SERIAL, "hub": "CFS_1", "extruder": "extruder"})
        unit = load_config_prefix(config)
        assert isinstance(unit, afcCFS)
        assert unit.name == "CFS_1"


# ============================================================================
# Config template macros
# ============================================================================

# Only the CFS templates: inside AFC's repo the folder also holds every other unit's
TEMPLATES = sorted((pathlib.Path(__file__).resolve().parent.parent / "templates")
                   .glob("AFC_CFS_*.cfg"))
if not TEMPLATES:  # tests run from inside the AFC checkout; find the repo copy via the symlink
    TEMPLATES = sorted(pathlib.Path(__file__).resolve().parent.parent.parent
                       .glob("templates/AFC_CFS_*.cfg"))


def _macros():
    for path in TEMPLATES:
        cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=None,
                                          comment_prefixes=("#", ";"))
        cp.read(path)
        for section in cp.sections():
            if section.startswith("gcode_macro ") and cp.has_option(section, "gcode"):
                yield pytest.param(cp.get(section, "gcode"), id="{}:{}".format(path.name, section))


class TestAfcCFSTemplates:
    @pytest.mark.parametrize("gcode", list(_macros()))
    def test_macro_compiles(self, gcode):
        # Klipper's environment: "{" "}" for expressions
        env = jinja2.Environment("{%", "%}", "{", "}")
        env.parse(gcode)

    def test_found_templates(self):
        # AFC_CFS_wipe.cfg is optional; the leave-purge macro the load needs is in AFC_CFS_1.cfg
        ids = [p.id for p in _macros()]
        assert "AFC_CFS_1.cfg:gcode_macro AFC_CFS_LEAVE_PURGE" in ids
