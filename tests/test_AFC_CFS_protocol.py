"""Unit tests for the wire-protocol part of extras/AFC_CFS.py.

The frame vectors are real traffic: the first group was captured with
interceptty from a stock printer talking to a CFS (via ityshchenko/klipper-cfs),
the rest live by tomaspakosta/k1c-cfs-klipper. Expected bytes and CRCs come
from those captures, not from the code under test.
"""
from __future__ import annotations

import struct

import pytest

from extras import AFC_CFS as P

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
