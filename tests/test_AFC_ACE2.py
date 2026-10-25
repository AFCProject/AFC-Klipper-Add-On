"""Unit tests for extras/AFC_ACE2.py."""

from __future__ import annotations

import configparser
import struct
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

import pytest

from extras.AFC_ACE import ACESerialError, ACETimeoutError, afcACE
from extras.AFC_ACE2 import (
    _ace2_extract_uid,
    _ace2_scan_candidates,
    _ace2_topology_key,
    _decode_status,
    _fstr,
    _fval,
    ACE2Connection,
    afcACE2,
    decode_frames,
    dump_fields,
    encode_frame,
    encode_request,
    load_config_prefix,
    method_to_v2,
    pb_bool,
    pb_decode,
    pb_decode_varint,
    pb_string,
    pb_varint,
    probe_ace2_uid,
    resolve_ace2_port,
    v2_response_to_v1,
)
import extras.AFC_ACE2 as afc_ace2_module
from tests.ace_helpers import (
    AceConfig,
    AceLogger,
    AcePrinter,
    capture_log,
    FakeAce2Connection,
    FakeSerial,
    LaneSpec,
    make_ace2_unit,
    make_ace_connection,
    make_fake_ace_connection,
    reset_ace_globals,
)


Ace2Field = Tuple[int, Union[int, str, bytes]]


class Ace2Wire:
    """
    The ACE 2 Pro wire protocol written out from its description rather than
    read from extras.AFC_ACE2: frame flags, opcodes, a base-128 varint,
    protobuf fields, and whole frames with their CRC-16 (reflected 0x8408,
    init 0xFFFF), computed byte-wise instead of the module's bit loop.
    """
    REQUEST = 0x00
    RESPONSE = 0x80
    DISCOVER_DEVICE = 0
    GET_STATUS = 6
    GET_INFO = 7
    FEED_OR_ROLLBACK = 8
    STOP_FEED_OR_ROLLBACK = 9
    UPDATE_SPEED = 10
    DRYING = 11
    SET_DRY_TEMP = 12
    GET_FILAMENT_INFO = 13
    SET_RFID_ENABLE = 14
    GET_MATERIAL_INFO = 16
    SET_MATERIAL_NAME = 18
    SET_FEED_CHECK = 19
    GET_TEMP = 64
    FILAMENT_IDENTIFY = 68
    SET_FAN = 71
    GET_SENSOR_STATE = 73
    GET_FEED_INFO = 76
    MFRC522_REG_READ = 0x50
    MFRC522_REG_WRITE = 0x51
    MFRC522_READER_POWER = 0x52

    @staticmethod
    def varint(value: int) -> bytes:
        """
        :param value: non-negative integer
        :return bytes: its base-128 varint, low group first
        """
        out = bytearray()
        while value >= 0x80:
            out.append(0x80 | (value & 0x7F))
            value >>= 7
        out.append(value)
        return bytes(out)

    @classmethod
    def message(cls, *fields: Ace2Field) -> bytes:
        """
        Encode protobuf fields in order: an int as a varint (wire type 0), a
        str (UTF-8) or bytes value length-delimited (wire type 2).

        :param fields: (field number, value) pairs
        :return bytes: the message
        """
        out = b""
        for num, value in fields:
            if isinstance(value, int):
                out += cls.varint(num << 3) + cls.varint(value)
            else:
                data = value.encode("utf-8") if isinstance(value, str) else value
                out += cls.varint((num << 3) | 2) + cls.varint(len(data)) + data
        return out

    @staticmethod
    def fixed32(num: int, value: float) -> bytes:
        """
        :param num: field number
        :param value: the float
        :return bytes: a 32-bit float field (wire type 5)
        """
        return bytes([(num << 3) | 5]) + struct.pack("<f", value)

    @staticmethod
    def fixed64(num: int, value: float) -> bytes:
        """
        :param num: field number
        :param value: the double
        :return bytes: a 64-bit double field (wire type 1)
        """
        return bytes([(num << 3) | 1]) + struct.pack("<d", value)

    @staticmethod
    def crc(data: bytes) -> int:
        """
        :param data: flags through payload of a frame
        :return int: the frame CRC (0x6F91 for b"123456789")
        """
        crc = 0xFFFF
        for byte in data:
            x = (byte ^ crc) & 0xFF
            x = (x ^ (x << 4)) & 0xFF
            crc = (((x << 8) | (crc >> 8)) ^ (x >> 4) ^ (x << 3)) & 0xFFFF
        return crc

    @classmethod
    def frame(cls, seq: int, cmd: int, payload: bytes = b"", flags: int = REQUEST) -> bytes:
        """
        :param seq: 16-bit sequence id
        :param cmd: opcode
        :param payload: protobuf payload
        :param flags: REQUEST or RESPONSE
        :return bytes: FF AA, flags, seq LE, cmd, length, payload, CRC LE, FE
        """
        inner = bytes([flags, seq & 0xFF, seq >> 8, cmd, len(payload)]) + payload
        crc = cls.crc(inner)
        return b"\xff\xaa" + inner + bytes([crc & 0xFF, crc >> 8, 0xFE])

    @classmethod
    def reply(cls, seq: int, cmd: int, payload: bytes = b"") -> bytes:
        """
        :param seq: 16-bit sequence id the unit echoes
        :param cmd: opcode
        :param payload: protobuf payload
        :return bytes: a response frame
        """
        return cls.frame(seq, cmd, payload, flags=cls.RESPONSE)


def ace2_status_slot(index: int, status: str = "empty", slot_status: str = "unknown",
                     rfid: int = 0) -> Dict[str, Any]:
    """
    One slot of a V1-shaped ACE 2 status, written out for expected values.

    :param index: 0-based slot
    :param status: "ready" (filament present) or "empty"
    :param slot_status: the slot's motor state name
    :param rfid: 2 when the filament was identified, else 0
    :return dict: the slot entry
    """
    return {"index": index, "status": status, "slot_status": slot_status, "sku": "",
            "type": "", "rfid": rfid, "brand": "", "color": [0, 0, 0]}


def ace2_link(next_id: int = 0, **kwargs: Any) -> Tuple[AcePrinter, ACE2Connection]:
    """
    A real ACE2Connection on a new printer: connected on a FakeSerial (no
    heartbeat), logging to printer.logger, its clock at 100s.

    :param next_id: next request id
    :param kwargs: other make_ace_connection keywords (serial, reconnect_enabled)
    :return tuple: (the printer, the connection)
    """
    printer = AcePrinter()
    return printer, make_ace_connection(ace2=True, printer=printer, next_id=next_id, **kwargs)


def ace2_link_answers(conn: ACE2Connection, **response: Any) -> None:
    """
    Answer each frame the connection writes as the unit would: the response,
    carrying the request's wire id, goes through the real _handle_response.

    :param conn: the connection
    :param response: the response's fields other than its id
    """
    def answer(frame: bytes) -> None:
        """
        :param frame: the request frame just written
        """
        conn._handle_response({"id": frame[3] | (frame[4] << 8), **response})

    conn._serial.on_write = answer


def ace2_jam_message(lane: str, what: str) -> str:
    """
    :param lane: the jammed lane
    :param what: the jam described in words
    :return str: the pause message for a jam on unit Ace2_1
    """
    return (f"ACE2 Ace2_1 lane {lane}: {what}. The unit's filament encoder reports the "
            "spool is not moving with the motor, likely a jam or tangle at the unit. "
            "Clear the snag, then resume. Run AFC_ACE_STUCK_SPOOL_DETECTION ENABLE=0 to "
            "disable this check.")


class TestPbVarint:
    def test_single_byte(self):
        assert pb_varint(0x7F) == b"\x7f"

    def test_multi_byte(self):
        # 300 = 0b10_0101100: low seven bits with the continuation bit, then 2.
        assert pb_varint(300) == bytes([0xAC, 0x02])
        # 0x80 is the first value that needs a second byte.
        assert pb_varint(0x80) == bytes([0x80, 0x01])

    def test_zero(self):
        assert pb_varint(0) == b"\x00"

    def test_negative_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            pb_varint(-1)

        assert str(excinfo.value) == "pb_varint cannot encode negative value -1"


class TestPbBool:
    def test_truthy_encodes_one(self):
        # Field 2, wire type 0: tag 0x10.
        assert pb_bool(2, True) == bytes([0x10, 0x01])
        assert pb_bool(2, "yes") == bytes([0x10, 0x01])

    def test_falsy_encodes_zero(self):
        assert pb_bool(2, 0) == bytes([0x10, 0x00])
        assert pb_bool(2, None) == bytes([0x10, 0x00])


class TestPbString:
    def test_pb_string_encodes_tag_len_and_bytes(self):
        # Field 2, wire type 2: tag 0x12; length 2; then the text.
        assert pb_string(2, "AB") == bytes([0x12, 0x02]) + b"AB"
        # The length counts UTF-8 bytes, not characters.
        assert pb_string(2, "hé") == bytes([0x12, 0x03, 0x68, 0xC3, 0xA9])
        # A length past 127 takes a two-byte varint.
        assert pb_string(1, "x" * 130) == bytes([0x0A, 0x82, 0x01]) + b"x" * 130

    def test_pb_string_accepts_bytes(self):
        assert pb_string(1, b"\x01\x02") == bytes([0x0A, 0x02, 0x01, 0x02])
        assert pb_string(1, bytearray(b"\x03")) == bytes([0x0A, 0x01, 0x03])

    def test_pb_string_roundtrips_through_pb_decode(self):
        assert pb_decode(pb_string(2, "hello")) == {2: [(2, b"hello")]}


class TestPbDecodeVarint:
    def test_multi_byte(self):
        assert pb_decode_varint(bytes([0xAC, 0x02]), 0) == (300, 2)
        # Decoding starts at pos and stops after the varint's last byte.
        assert pb_decode_varint(bytes([0x00, 0xAC, 0x02, 0x05]), 1) == (300, 3)

    def test_truncated_continuation_returns_partial(self):
        # The last byte still has its continuation bit: the loop runs out of
        # data and returns what it has, with pos at the end.
        assert pb_decode_varint(bytes([0x85]), 0) == (5, 1)
        assert pb_decode_varint(b"", 0) == (0, 0)


class TestPbDecode:
    def test_varint_field(self):
        assert pb_decode(bytes([0x08, 0x05])) == {1: [(0, 5)]}

    def test_double_field(self):
        assert pb_decode(Ace2Wire.fixed64(1, 2.5)) == {1: [(1, 2.5)]}

    def test_double_truncated_breaks(self):
        # Field 2 decodes; the double after it has two of its eight bytes, so
        # decoding stops and drops it.
        assert pb_decode(bytes([0x10, 0x07, 0x09, 0x00, 0x00])) == {2: [(0, 7)]}

    def test_length_delimited_field(self):
        assert pb_decode(bytes([0x12, 0x02]) + b"AB") == {2: [(2, b"AB")]}

    def test_float_field(self):
        assert pb_decode(Ace2Wire.fixed32(3, 1.5)) == {3: [(5, 1.5)]}

    def test_float_truncated_breaks(self):
        assert pb_decode(bytes([0x10, 0x07, 0x1D, 0x00])) == {2: [(0, 7)]}

    def test_unsupported_wire_type_breaks(self):
        # Wire type 3 (start group) is not supported: decoding stops there and
        # a later valid field is not reached.
        assert pb_decode(bytes([0x0B, 0x00, 0x10, 0x07])) == {}

    def test_repeated_field_kept_in_order(self):
        assert pb_decode(bytes([0x08, 0x07, 0x08, 0x08])) == {1: [(0, 7), (0, 8)]}


class TestFval:
    def test_present(self):
        assert _fval({1: [(0, 42)]}, 1) == 42
        # A repeated field reads its first value.
        assert _fval({1: [(0, 42), (0, 43)]}, 1) == 42

    def test_absent_returns_default(self):
        assert _fval({}, 1, default=99) == 99
        assert _fval({2: [(0, 5)]}, 1) == 0


class TestFstr:
    def test_utf8_decode(self):
        assert _fstr({1: [(2, b"hi")]}, 1) == "hi"

    def test_invalid_utf8_falls_back_to_hex(self):
        assert _fstr({1: [(2, b"\xff\xfe")]}, 1) == "fffe"

    def test_non_bytes_returns_default(self):
        # A varint value under the field number is not bytes: the default.
        assert _fstr({1: [(0, 5)]}, 1, default="d") == "d"
        assert _fstr({1: [(0, 5)]}, 1) == ""

    def test_absent_field_reads_empty_not_default(self):
        # An absent field reads as empty bytes, which decode to "", so the
        # default only stands in for a non-bytes value.
        assert _fstr({}, 1, default="d") == ""


class TestDumpFields:
    def test_varint_scalar(self):
        assert dump_fields({1: [(0, 5)]}) == {1: 5}
        # Fields come out in field-number order.
        assert list(dump_fields({3: [(0, 1)], 1: [(0, 2)]})) == [1, 3]

    def test_float_is_rounded(self):
        assert dump_fields({1: [(5, 1.234567)], 2: [(1, 2.718281)]}) == {1: 1.2346, 2: 2.7183}

    def test_printable_bytes_repr(self):
        assert dump_fields({1: [(2, b"AB")]}) == {1: "'AB'"}

    def test_nonprintable_bytes_hex(self):
        # Valid UTF-8 that is not printable is shown as hex.
        assert dump_fields({1: [(2, b"\x01\x02")]}) == {1: "hex:0102"}

    def test_invalid_utf8_bytes_hex(self):
        assert dump_fields({1: [(2, b"\xff\xfe")]}) == {1: "hex:fffe"}

    def test_non_bytes_wire2_falls_through_to_else(self):
        assert dump_fields({1: [(2, 123)]}) == {1: 123}

    def test_repeated_field_becomes_list(self):
        assert dump_fields({1: [(0, 1), (0, 2)]}) == {1: [1, 2]}


class TestMethodToV2:
    @staticmethod
    def _map(method: str, params: Optional[Dict[str, Any]]
             ) -> Tuple[Tuple[int, bytes], List[Tuple[str, str]]]:
        """
        :param method: V1 method name
        :param params: its params
        :return tuple: (method_to_v2's result, the "AFC_ACE2" log lines it wrote)
        """
        with capture_log("AFC_ACE2") as log:
            result = method_to_v2(method, params)
        return result, log.messages

    def test_get_info(self):
        assert self._map("get_info", None) == ((7, b""), [])

    def test_get_status(self):
        assert self._map("get_status", {}) == ((6, b""), [])

    def test_discover_device(self):
        assert self._map("discover_device", {}) == ((0, b""), [])

    def test_start_feed_assist(self):
        # slot 2, speed 15, length 0, mode 2 (assist).
        assert self._map("start_feed_assist", {"index": 2, "speed": 15}) == (
            (8, bytes([0x08, 0x02, 0x10, 0x0F, 0x18, 0x00, 0x20, 0x02])), [])

    def test_start_feed_assist_defaults(self):
        # slot 0 and speed 10 when not given.
        assert self._map("start_feed_assist", {}) == (
            (8, bytes([0x08, 0x00, 0x10, 0x0A, 0x18, 0x00, 0x20, 0x02])), [])

    def test_stop_feed_assist(self):
        assert self._map("stop_feed_assist", {"index": 3}) == ((9, bytes([0x08, 0x03])), [])

    def test_feed_filament(self):
        # slot 1, speed 60, length 40, mode 0 (feed).
        assert self._map("feed_filament", {"index": 1, "length": 40, "speed": 60}) == (
            (8, bytes([0x08, 0x01, 0x10, 0x3C, 0x18, 0x28, 0x20, 0x00])), [])

    def test_unwind_filament(self):
        # Same layout as a feed, mode 1 (rollback).
        assert self._map("unwind_filament", {"index": 1, "length": 40, "speed": 60}) == (
            (8, bytes([0x08, 0x01, 0x10, 0x3C, 0x18, 0x28, 0x20, 0x01])), [])

    def test_stop_feed_filament(self):
        assert self._map("stop_feed_filament", {"index": 2}) == ((9, bytes([0x08, 0x02])), [])

    def test_update_feeding_speed(self):
        assert self._map("update_feeding_speed", {"index": 1, "speed": 70}) == (
            (10, bytes([0x08, 0x01, 0x10, 0x46])), [])

    def test_get_filament_info(self):
        assert self._map("get_filament_info", {"index": 3}) == ((13, bytes([0x08, 0x03])), [])

    def test_drying_fan_on(self):
        # temp 55, duration 120, and any positive fan_speed sends fan on (1).
        assert self._map("drying", {"temp": 55, "duration": 120, "fan_speed": 40}) == (
            (11, bytes([0x08, 0x37, 0x10, 0x78, 0x18, 0x01])), [])

    def test_drying_fan_off(self):
        assert self._map("drying", {"temp": 55, "duration": 120, "fan_speed": 0}) == (
            (11, bytes([0x08, 0x37, 0x10, 0x78, 0x18, 0x00])), [])

    def test_drying_stop(self):
        assert self._map("drying_stop", {}) == ((11, bytes([0x08, 0x00, 0x10, 0x00])), [])

    def test_set_fan_speed_on(self):
        # speed 80, then both on/off flags set.
        assert self._map("set_fan_speed", {"speed": 80}) == (
            (71, bytes([0x08, 0x50, 0x10, 0x01, 0x18, 0x01])), [])

    def test_set_fan_speed_off(self):
        assert self._map("set_fan_speed", {"speed": 0}) == (
            (71, bytes([0x08, 0x00, 0x10, 0x00, 0x18, 0x00])), [])

    def test_set_rfid_enable_true(self):
        assert self._map("set_rfid_enable", {"index": 1, "enable": True}) == (
            (14, bytes([0x08, 0x01, 0x10, 0x01])), [])

    def test_set_rfid_enable_false(self):
        assert self._map("set_rfid_enable", {"index": 1, "enable": False}) == (
            (14, bytes([0x08, 0x01, 0x10, 0x00])), [])

    def test_set_feed_check(self):
        assert self._map("set_feed_check", {"check_length": 100, "error_length": 90}) == (
            (19, bytes([0x08, 0x64, 0x10, 0x5A])), [])

    def test_mfrc522_reg_read(self):
        # arg 0x010203 = 66051 as a varint: 0x83 0x84 0x04.
        assert self._map("mfrc522_reg_read", {"arg": 0x010203}) == (
            (0x50, bytes([0x08, 0x83, 0x84, 0x04])), [])

    def test_mfrc522_reg_read_masks_arg_to_32_bits(self):
        # -1 masks to 0xFFFFFFFF, which encodes, where -1 itself would not.
        assert self._map("mfrc522_reg_read", {"arg": -1}) == (
            (0x50, bytes([0x08, 0xFF, 0xFF, 0xFF, 0xFF, 0x0F])), [])
        assert self._map("mfrc522_reg_read", {"arg": (1 << 32) | 5}) == (
            (0x50, bytes([0x08, 0x05])), [])

    def test_mfrc522_reg_write(self):
        # arg 0x0405 = 1029 as a varint: 0x85 0x08.
        assert self._map("mfrc522_reg_write", {"arg": 0x0405}) == (
            (0x51, bytes([0x08, 0x85, 0x08])), [])

    def test_mfrc522_reader_power(self):
        # arg 0x10001 = 65537 as a varint: 0x81 0x80 0x04.
        assert self._map("mfrc522_reader_power", {"arg": 0x10001}) == (
            (0x52, bytes([0x08, 0x81, 0x80, 0x04])), [])

    def test_filament_identify(self):
        assert self._map("filament_identify", {"index": 2}) == ((68, bytes([0x08, 0x02])), [])

    def test_set_dry_temp(self):
        assert self._map("set_dry_temp", {"temp": 65}) == ((12, bytes([0x08, 0x41])), [])

    def test_get_temp(self):
        assert self._map("get_temp", {}) == ((64, b""), [])

    def test_get_feed_info(self):
        assert self._map("get_feed_info", {}) == ((76, b""), [])

    def test_raw_valid_hex(self):
        assert self._map("raw", {"cmd": 20, "hex": "0102"}) == ((20, b"\x01\x02"), [])

    def test_raw_invalid_hex_falls_back_to_empty(self):
        assert self._map("raw", {"cmd": 20, "hex": "zz"}) == ((20, b""), [])

    def test_raw_empty_hex(self):
        assert self._map("raw", {"cmd": 7}) == ((7, b""), [])
        assert self._map("raw", {"cmd": 7, "hex": None}) == ((7, b""), [])

    def test_unknown_method_falls_back_to_get_status(self):
        assert self._map("no_such_method", {}) == (
            (6, b""), [("debug", "ACE2: unknown method 'no_such_method' -> GET_STATUS")])

    def test_method_get_material_info(self):
        assert self._map("get_material_info", {"index": 3}) == ((16, bytes([0x08, 0x03])), [])

    def test_method_get_material_info_defaults_slot_zero(self):
        assert self._map("get_material_info", {}) == ((16, bytes([0x08, 0x00])), [])

    def test_method_set_material_name(self):
        # Field 1 the slot, field 2 the name string.
        assert self._map("set_material_name", {"index": 2, "name": "PLA"}) == (
            (18, bytes([0x08, 0x02, 0x12, 0x03]) + b"PLA"), [])

    def test_method_set_material_name_defaults(self):
        assert self._map("set_material_name", {}) == (
            (18, bytes([0x08, 0x00, 0x12, 0x00])), [])

    def test_method_get_sensor_state_aliases(self):
        assert self._map("get_sensor_state", {}) == ((73, b""), [])
        assert self._map("get_key_state", {}) == ((73, b""), [])

    @pytest.mark.parametrize("method,expected", [
        ("stop_feed_assist", (9, bytes([0x08, 0x00]))),
        ("feed_filament", (8, bytes([0x08, 0x00, 0x10, 0x32, 0x18, 0x00, 0x20, 0x00]))),
        ("unwind_filament", (8, bytes([0x08, 0x00, 0x10, 0x32, 0x18, 0x00, 0x20, 0x01]))),
        ("update_feeding_speed", (10, bytes([0x08, 0x00, 0x10, 0x32]))),
        ("drying", (11, bytes([0x08, 0x32, 0x10, 0x00, 0x18, 0x01]))),
        ("set_fan_speed", (71, bytes([0x08, 0x00, 0x10, 0x00, 0x18, 0x00]))),
        ("set_rfid_enable", (14, bytes([0x08, 0x00, 0x10, 0x01]))),
        ("set_feed_check", (19, bytes([0x08, 0xFE, 0x01, 0x10, 0xFE, 0x01]))),
        ("mfrc522_reg_write", (0x51, bytes([0x08, 0x00]))),
        ("set_dry_temp", (12, bytes([0x08, 0x32]))),
        ("raw", (0, b"")),
    ])
    def test_defaults_when_params_omitted(self, method, expected):
        # None params read as empty: slot 0, speed 50, drying at 50C with
        # the fan on, feed check 254/254 and RFID enabled.
        assert self._map(method, None) == (expected, [])


class TestEncodeFrame:
    def test_encode_frame_structure(self):
        # FF AA, request flag 00, seq 7 (LE), opcode 0x40, length 0, CRC (LE), FE.
        expected = bytes.fromhex("ff aa 00 07 00 40 00 cf 21 fe")

        assert encode_frame(7, 0x40, b"") == expected
        assert encode_frame(7, 0x40) == expected

    def test_encode_frame_embeds_payload(self):
        assert encode_frame(9, 20, b"\x01\x02\x03") == bytes.fromhex(
            "ff aa 00 09 00 14 03 01 02 03 df da fe")
        assert encode_frame(9, 20, b"\x01\x02\x03") == Ace2Wire.frame(9, 20, b"\x01\x02\x03")

    def test_encode_frame_masks_seq_to_16_bits(self):
        # 86480 wraps to 20944 = 0x51D0, sent low byte first.
        assert encode_frame(86480, 6, b"") == bytes.fromhex("ff aa 00 d0 51 06 00 1f 45 fe")

    def test_encode_frame_rejects_oversized_payload(self):
        with pytest.raises(ValueError) as excinfo:
            encode_frame(1, 20, b"\x00" * 101)

        assert str(excinfo.value) == "V2 payload exceeds 100 bytes for opcode 20"
        # 100 bytes is the largest payload a frame carries.
        assert encode_frame(1, 20, b"\x00" * 100) == Ace2Wire.frame(1, 20, b"\x00" * 100)


class TestEncodeRequest:
    def test_encode_request_delegates_to_encode_frame(self):
        assert encode_request(42, "get_temp", {}) == Ace2Wire.frame(42, Ace2Wire.GET_TEMP)
        assert encode_request(42, "get_info", None) == Ace2Wire.frame(42, Ace2Wire.GET_INFO)
        assert encode_request(42, "feed_filament", {"index": 1, "length": 40, "speed": 60}) == (
            Ace2Wire.frame(42, Ace2Wire.FEED_OR_ROLLBACK,
                           bytes([0x08, 0x01, 0x10, 0x3C, 0x18, 0x28, 0x20, 0x00])))

    def test_encode_request_rejects_oversized_payload(self):
        # A 200-character material name makes a 205-byte payload.
        with pytest.raises(ValueError) as excinfo:
            encode_request(1, "set_material_name", {"index": 0, "name": "x" * 200})

        assert str(excinfo.value) == "V2 payload exceeds 100 bytes for opcode 18"

    def test_encode_masks_id_to_16_bits(self):
        assert encode_request(86480, "get_status", {}) == Ace2Wire.frame(20944,
                                                                         Ace2Wire.GET_STATUS)


class TestDecodeStatus:
    IDLE_DRYER = {"status": "stop", "target_temp": 0, "duration": 0, "remain_time": 0}

    @classmethod
    def _status(cls, status: str, slots: List[Dict[str, Any]],
                **fields: Any) -> Dict[str, Any]:
        """
        :param status: overall "busy" or "ready"
        :param slots: the four slot entries
        :param fields: top-level values other than the zero defaults
        :return dict: the whole expected status
        """
        expected: Dict[str, Any] = {
            "status": status, "dryer_status": cls.IDLE_DRYER, "temp": 0, "humidity": 0,
            "enable_rfid": 0, "fan_speed": 0, "feed_assist_count": 0,
            "cont_assist_time": 0.0, "slots": slots}
        expected.update(fields)
        return expected

    def test_ace2_decode_status_indexes_and_tags_busy_slot(self):
        # One slot sent: motor state 1 (feeding), filament state 1 (unknown,
        # which still counts as present); the other three are padded.
        status = _decode_status({9: [(2, bytes([0x08, 0x01, 0x10, 0x01]))]})

        assert status == self._status("busy", [
            ace2_status_slot(0, "ready", "feeding"), ace2_status_slot(1),
            ace2_status_slot(2), ace2_status_slot(3)])
        # The slot index lets the unit tag its current action with the slot.
        assert afcACE._derive_action(status) == "feeding(slot 0)"

    def test_ace2_decode_status_pads_slots_with_index(self):
        assert _decode_status({}) == self._status("ready", [
            ace2_status_slot(0), ace2_status_slot(1), ace2_status_slot(2),
            ace2_status_slot(3)])

    @pytest.mark.parametrize("code,name,overall", [
        (0, "ready", "ready"), (1, "feeding", "busy"), (2, "rollback", "busy"),
        (3, "assisting", "ready"), (4, "rollback_assisting", "ready"),
        (5, "preloading", "busy"), (6, "upgrading", "ready"), (129, "feed_error", "ready"),
        (133, "stuck_error", "ready"), (99, "unknown", "ready"),
    ])
    def test_only_feeding_rollback_and_preloading_are_busy(self, code, name, overall):
        status = _decode_status({9: [(2, Ace2Wire.message((1, code), (2, 2)))]})

        assert status["status"] == overall
        # Filament state 2 (identified) is present and RFID-read.
        assert status["slots"][0] == ace2_status_slot(0, "ready", name, rfid=2)

    def test_unknown_filament_state_reads_empty(self):
        status = _decode_status({9: [(2, bytes([0x08, 0x00, 0x10, 0x09]))]})

        assert status["slots"][0] == ace2_status_slot(0, "empty", "ready")

    def test_first_dryer_entry_wins(self):
        first = bytes([0x08, 0x01, 0x10, 0x32, 0x18, 0x3C, 0x20, 0x1E])
        second = bytes([0x08, 0x02, 0x10, 0x46, 0x18, 0x0A, 0x20, 0x05])

        status = _decode_status({2: [(2, first), (2, second)]})

        assert status["dryer_status"] == {"status": "starting", "target_temp": 50,
                                          "duration": 60, "remain_time": 30}


class TestV2ResponseToV1:
    @staticmethod
    def _convert(cmd: int, seq: int, payload: Any) -> Tuple[Dict[str, Any],
                                                            List[Tuple[str, str]]]:
        """
        :param cmd: response opcode
        :param seq: echoed sequence id
        :param payload: response payload
        :return tuple: (the V1-shaped response, the "AFC_ACE2" log lines written)
        """
        with capture_log("AFC_ACE2") as log:
            ret = v2_response_to_v1(cmd, seq, payload)
        return ret, log.messages

    @staticmethod
    def _ok(cmd: int, seq: int, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        :param cmd: response opcode
        :param seq: echoed sequence id
        :param result: the decoded result
        :return dict: a successful V1-shaped response
        """
        return {"id": seq, "_cmd": cmd, "code": 0, "msg": "success", "result": result}

    @staticmethod
    def _bits(*on: int) -> List[bool]:
        """
        :param on: channels that are set
        :return list: the 17 sensor channels
        """
        return [channel in on for channel in range(17)]

    @staticmethod
    def _slot_sensors(insert: bool = False, empty: bool = False, buf_rst: bool = False,
                      buf_back: bool = False) -> Dict[str, bool]:
        """
        :return dict: one slot's named sensor signals
        """
        return {"insert": insert, "empty": empty, "buf_rst": buf_rst, "buf_back": buf_back}

    @staticmethod
    def _filament(**fields: Any) -> Dict[str, Any]:
        """
        :param fields: values other than the empty-tag defaults
        :return dict: a whole filament-info result
        """
        expected: Dict[str, Any] = {
            "index": 0, "version": 0, "sku": "", "type": "", "brand": "",
            "color": [0, 0, 0], "colors_rgba": [], "rfid": 0, "diameter": 0.0,
            "total_length": 0, "length": 0, "icon_type": 0,
            "extruder_temp": {}, "hotbed_temp": {}, "raw": {}}
        expected.update(fields)
        return expected

    # GET_SENSOR_STATE: four sensors per slot (insert, empty, buf_rst,
    # buf_back), then the shared buffer-feed sensor at channel 16.

    def test_decode_slot_bit_offsets(self):
        mask = (1 << 3) | (1 << 5) | (1 << 8) | (1 << 14) | (1 << 16)

        ret, logs = self._convert(73, 1, Ace2Wire.message((1, mask)))

        assert ret == self._ok(73, 1, {
            "sensor_bitmask": 82216, "sensors": self._bits(3, 5, 8, 14, 16),
            "slot_sensors": [self._slot_sensors(buf_back=True), self._slot_sensors(empty=True),
                             self._slot_sensors(insert=True), self._slot_sensors(buf_rst=True)],
            "buf_feed": True})
        assert logs == []

    def test_decode_all_clear(self):
        ret, logs = self._convert(73, 1, bytes([0x08, 0x00]))

        assert ret == self._ok(73, 1, {
            "sensor_bitmask": 0, "sensors": [False] * 17,
            "slot_sensors": [self._slot_sensors()] * 4, "buf_feed": False})
        assert logs == []

    def test_decode_keeps_raw_sensor_list(self):
        ret, logs = self._convert(73, 1, Ace2Wire.message((1, 1 << 16)))

        assert ret == self._ok(73, 1, {
            "sensor_bitmask": 65536, "sensors": self._bits(16),
            "slot_sensors": [self._slot_sensors()] * 4, "buf_feed": True})
        assert logs == []

    def test_get_sensor_state_decodes_bitmask_not_error(self):
        # 70928, a mask read from a live unit: channels 4, 8, 10, 12 and 16.
        ret, logs = self._convert(73, 9, Ace2Wire.message((1, 70928)))

        assert ret == self._ok(73, 9, {
            "sensor_bitmask": 70928, "sensors": self._bits(4, 8, 10, 12, 16),
            "slot_sensors": [self._slot_sensors(), self._slot_sensors(insert=True),
                             self._slot_sensors(insert=True, buf_rst=True),
                             self._slot_sensors(insert=True)],
            "buf_feed": True})
        assert logs == []

    def test_get_sensor_state_zero_mask_all_false(self):
        ret, logs = self._convert(73, 9, bytes([0x08, 0x00]))

        assert ret == self._ok(73, 9, {
            "sensor_bitmask": 0, "sensors": [False] * 17,
            "slot_sensors": [self._slot_sensors()] * 4, "buf_feed": False})
        assert logs == []

    def test_get_sensor_state_individual_bits(self):
        ret, logs = self._convert(73, 9, bytes([0x08, 0x11]))

        assert ret == self._ok(73, 9, {
            "sensor_bitmask": 17, "sensors": self._bits(0, 4),
            "slot_sensors": [self._slot_sensors(insert=True), self._slot_sensors(insert=True),
                             self._slot_sensors(), self._slot_sensors()],
            "buf_feed": False})
        assert logs == []

    def test_get_sensor_state_non_int_mask_reads_as_zero(self):
        # Field 1 arriving length-delimited is no bitmask: every channel off.
        ret, logs = self._convert(73, 9, bytes([0x0A, 0x01, 0xFF]))

        assert ret == self._ok(73, 9, {
            "sensor_bitmask": 0, "sensors": [False] * 17,
            "slot_sensors": [self._slot_sensors()] * 4, "buf_feed": False})
        assert logs == []

    # GET_STATUS, decoded by _decode_status.

    def test_busy_slot_identified_and_dryer(self):
        payload = (Ace2Wire.message((9, bytes([0x08, 0x01, 0x10, 0x02])),
                                    (9, bytes([0x08, 0x00, 0x10, 0x00])))
                   + Ace2Wire.message((2, Ace2Wire.message((1, 2), (2, 60), (3, 3600),
                                                           (4, 1800)))))
        payload += Ace2Wire.message((3, 25), (4, 40), (5, 3), (7, 9), (8, 12))

        ret, logs = self._convert(6, 4, payload)

        assert ret == self._ok(6, 4, {
            "status": "busy",
            "dryer_status": {"status": "keeping", "target_temp": 60, "duration": 3600,
                             "remain_time": 1800},
            "temp": 25, "humidity": 40, "enable_rfid": 1, "fan_speed": 0,
            "feed_assist_count": 9, "cont_assist_time": 12.0,
            "slots": [ace2_status_slot(0, "ready", "feeding", rfid=2),
                      ace2_status_slot(1, "empty", "ready"), ace2_status_slot(2),
                      ace2_status_slot(3)]})
        assert isinstance(ret["result"]["cont_assist_time"], float)
        assert logs == []

    def test_ready_when_no_busy_slot_and_rfid_disabled(self):
        # A ready slot holding filament of unknown type; no RFID field.
        ret, logs = self._convert(6, 4, Ace2Wire.message((9, bytes([0x08, 0x00, 0x10, 0x01]))))

        assert ret == self._ok(6, 4, {
            "status": "ready",
            "dryer_status": {"status": "stop", "target_temp": 0, "duration": 0,
                             "remain_time": 0},
            "temp": 0, "humidity": 0, "enable_rfid": 0, "fan_speed": 0,
            "feed_assist_count": 0, "cont_assist_time": 0.0,
            "slots": [ace2_status_slot(0, "ready", "ready"), ace2_status_slot(1),
                      ace2_status_slot(2), ace2_status_slot(3)]})
        assert logs == []

    def test_non_message_slot_and_dryer_entries_skipped(self):
        # Fields 9 and 2 arriving as varints are not slot or dryer messages.
        ret, logs = self._convert(6, 4, Ace2Wire.message((9, 5), (2, 5)))

        assert ret == self._ok(6, 4, {
            "status": "ready",
            "dryer_status": {"status": "stop", "target_temp": 0, "duration": 0,
                             "remain_time": 0},
            "temp": 0, "humidity": 0, "enable_rfid": 0, "fan_speed": 0,
            "feed_assist_count": 0, "cont_assist_time": 0.0,
            "slots": [ace2_status_slot(0), ace2_status_slot(1), ace2_status_slot(2),
                      ace2_status_slot(3)]})
        assert logs == []

    def test_empty_payload_short_circuits(self):
        ret, logs = self._convert(7, 3, b"")

        assert ret == {"id": 3, "_cmd": 7, "code": 0, "msg": "success", "result": {}}
        assert logs == []

    def test_decode_failure_is_logged_and_returns_default(self):
        logger = AceLogger()

        with capture_log("AFC_ACE2") as module_log:
            # Text instead of bytes: indexing yields str, which the varint
            # decoder cannot mask.
            ret = v2_response_to_v1(7, 5, "\x08\x05", logger)

        assert ret == {"id": 5, "_cmd": 7, "code": 0, "msg": "success", "result": {}}
        assert logger.messages == [
            ("debug", "ACE2 protobuf decode failure cmd=7: unsupported operand type(s) "
                      "for &: 'str' and 'int'")]
        assert module_log.messages == []

    def test_decode_failure_without_logger_uses_module_logger(self):
        ret, logs = self._convert(13, 5, "\x08")

        assert ret == {"id": 5, "_cmd": 13, "code": 0, "msg": "success", "result": {}}
        assert logs == [("debug", "ACE2 protobuf decode failure cmd=13: unsupported "
                                  "operand type(s) for &: 'str' and 'int'")]

    def test_discover_device(self):
        ret, logs = self._convert(0, 1, Ace2Wire.message((1, 11), (2, 22), (3, 33)))

        assert ret == self._ok(0, 1, {"uid1": 11, "uid2": 22, "uid3": 33})
        assert logs == []

    def test_get_info(self):
        ret, logs = self._convert(7, 1, Ace2Wire.message((1, "v1.1.31"), (2, "boot9")))

        assert ret == self._ok(7, 1, {"model": "ACE 2 Pro", "firmware": "v1.1.31",
                                      "boot_version": "boot9"})
        assert logs == []

    def test_get_info_surfaces_extra_fields(self):
        ret, logs = self._convert(7, 1, Ace2Wire.message((1, "v1"), (5, b"AB"), (3, 42)))

        assert ret == self._ok(7, 1, {"model": "ACE 2 Pro", "firmware": "v1",
                                      "boot_version": "", "field3": 42, "field5": b"AB"})
        assert list(ret["result"]) == ["model", "firmware", "boot_version", "field3", "field5"]
        assert logs == []

    def test_get_feed_info(self):
        # A varint field 1 is not a FeedInfo message and is skipped.
        payload = Ace2Wire.message((1, Ace2Wire.message((1, 100), (2, 200), (3, 300))),
                                   (1, 5), (4, 0))

        ret, logs = self._convert(76, 1, payload)

        assert ret == self._ok(76, 1, {
            "feed_info": [{"steps": 100, "length": 200, "decoder": 300}],
            "raw_fields": [1, 4]})
        assert logs == []

    def test_mfrc522_reg_read_masks_low_byte(self):
        ret, logs = self._convert(0x50, 1, Ace2Wire.message((1, 0x1FF)))

        assert ret == self._ok(0x50, 1, {"val": 0xFF})
        assert logs == []

    def test_filament_info_full(self):
        payload = Ace2Wire.message(
            (1, 2), (3, "SKU9"), (4, "PLA"), (5, Ace2Wire.message((1, 0x11223344))),
            (8, 175), (11, 330000), (6, Ace2Wire.message((1, 190), (2, 230))),
            (7, Ace2Wire.message((1, 55), (2, 65))))

        ret, logs = self._convert(13, 1, payload)

        assert ret == self._ok(13, 1, self._filament(
            index=2, sku="SKU9", type="PLA", color=[0x11, 0x22, 0x33],
            colors_rgba=[0x11223344], rfid=2,
            diameter=1.75, total_length=330000, extruder_temp={"min": 190, "max": 230},
            hotbed_temp={"min": 55, "max": 65},
            raw={1: 2, 3: "'SKU9'", 4: "'PLA'", 5: "hex:08c4e6888901", 6: "hex:08be0110e601",
                 7: "hex:08371041", 8: 175, 11: 330000}))
        assert logs == []

    def test_filament_info_every_colour_and_fields_9_and_10(self):
        # Field 5 repeats once per colour of a multi-colour spool.
        payload = Ace2Wire.message(
            (2, 0x0303), (3, "B:eSUN"), (4, "PLA Plus"),
            (5, Ace2Wire.message((1, 0xB87333FF))), (5, Ace2Wire.message((1, 0x50C878FF))),
            (9, 0x047B1F31), (10, (7 << 24) | 0xC92A81))

        result = self._convert(13, 1, payload)[0]["result"]

        assert result["colors_rgba"] == [0xB87333FF, 0x50C878FF]
        assert result["color"] == [0xB8, 0x73, 0x33]
        assert (result["length"], result["icon_type"]) == (0x047B1F31, 0x07C92A81)

    def test_filament_identify_no_type_no_color(self):
        # No type: not identified. A varint colour is skipped and an all-zero
        # temperature range reads as unset.
        payload = Ace2Wire.message((1, 0), (5, 9), (6, Ace2Wire.message((1, 0))))

        ret, logs = self._convert(68, 1, payload)

        assert ret == self._ok(68, 1, self._filament(raw={1: 0, 5: 9, 6: "hex:0800"}))
        assert logs == []

    def test_filament_info_temp_range_non_message_skipped(self):
        ret, logs = self._convert(13, 1, Ace2Wire.message((4, "PETG"), (6, 5)))

        assert ret == self._ok(13, 1, self._filament(type="PETG", rfid=2,
                                                     raw={4: "'PETG'", 6: 5}))
        assert logs == []

    def test_filament_info_temp_range_half_set_is_kept(self):
        # Only a nozzle minimum, only a bed maximum: either one keeps the range.
        payload = Ace2Wire.message((6, Ace2Wire.message((1, 190))),
                                   (7, Ace2Wire.message((2, 65))))

        ret, logs = self._convert(13, 1, payload)

        assert ret == self._ok(13, 1, self._filament(
            extruder_temp={"min": 190, "max": 0}, hotbed_temp={"min": 0, "max": 65},
            raw={6: "hex:08be01", 7: "hex:1041"}))
        assert logs == []

    def test_identify_reply_surfaces_tag_version(self):
        payload = Ace2Wire.message((1, 1), (2, 0x0102), (3, "SM0123ABCD"), (4, "PLA Basic"))

        ret, logs = self._convert(68, 5, payload)

        assert ret == self._ok(68, 5, self._filament(
            index=1, version=258, sku="SM0123ABCD", type="PLA Basic", rfid=2,
            raw={1: 1, 2: 258, 3: "'SM0123ABCD'", 4: "'PLA Basic'"}))
        assert logs == []

    def test_material_info_non_message_name_entry_skipped(self):
        ret, logs = self._convert(16, 1, Ace2Wire.message((1, 1), (2, 7)))

        assert ret == self._ok(16, 1, {"index": 1, "material_name": "", "status": 0,
                                       "raw": {1: 1, 2: 7}})
        assert logs == []

    def test_get_material_info_decode_extracts_name(self):
        # The name sits in a nested message: field 2 -> field 1.
        name = "S0395MB251230046650C3"

        ret, logs = self._convert(16, 5, Ace2Wire.message((1, 0),
                                                          (2, Ace2Wire.message((1, name)))))

        assert ret == self._ok(16, 5, {
            "index": 0, "material_name": name, "status": 0,
            "raw": {1: 0, 2: "hex:0a15" + name.encode("ascii").hex()}})
        assert logs == []

    def test_get_material_info_decode_with_status_and_slot(self):
        payload = Ace2Wire.message((1, 3), (2, Ace2Wire.message((1, "PETG"))), (3, 1))

        ret, logs = self._convert(16, 5, payload)

        assert ret == self._ok(16, 5, {"index": 3, "material_name": "PETG", "status": 1,
                                       "raw": {1: 3, 2: "hex:0a0450455447", 3: 1}})
        assert logs == []

    def test_get_material_info_first_name_entry_wins(self):
        payload = Ace2Wire.message((2, Ace2Wire.message((1, "AB"))),
                                   (2, Ace2Wire.message((1, "CD"))))

        ret, logs = self._convert(16, 5, payload)

        assert ret["result"]["material_name"] == "AB"
        assert ret["result"]["raw"] == {2: ["hex:0a024142", "hex:0a024344"]}
        assert logs == []

    def test_get_material_info_empty_name(self):
        ret, logs = self._convert(16, 5, bytes([0x08, 0x01]))

        assert ret == self._ok(16, 5, {"index": 1, "material_name": "", "status": 0,
                                       "raw": {1: 1}})
        assert logs == []

    def test_get_temp_decode_maps_all_channels_varint(self):
        payload = Ace2Wire.message((1, 21), (2, 22), (3, 23), (4, 24), (5, 25), (6, 40))

        ret, logs = self._convert(64, 3, payload)

        assert ret == self._ok(64, 3, {"box1_temp": 21, "box2_temp": 22, "ptc1_temp": 23,
                                       "ptc2_temp": 24, "env_temp": 25, "env_humidity": 40})
        assert logs == []

    def test_get_temp_decode_float_channels(self):
        payload = b"".join(Ace2Wire.fixed32(num, value) for num, value in (
            (1, 30.5), (2, 31.5), (3, 55.0), (4, 60.0), (5, 24.25), (6, 41.5)))

        ret, logs = self._convert(64, 1, payload)

        assert ret == self._ok(64, 1, {"box1_temp": 30.5, "box2_temp": 31.5, "ptc1_temp": 55.0,
                                       "ptc2_temp": 60.0, "env_temp": 24.25,
                                       "env_humidity": 41.5})
        assert logs == []

    def test_get_temp_missing_channels_default_zero(self):
        ret, logs = self._convert(64, 1, Ace2Wire.message((1, 27)))

        assert ret == self._ok(64, 1, {"box1_temp": 27, "box2_temp": 0.0, "ptc1_temp": 0.0,
                                       "ptc2_temp": 0.0, "env_temp": 0.0,
                                       "env_humidity": 0.0})
        assert logs == []

    def test_unmapped_opcode_surfaces_raw_fields(self):
        ret, logs = self._convert(20, 11, Ace2Wire.message((1, 0), (5, 99)))

        assert ret == self._ok(20, 11, {"raw_fields": {1: 0, 5: 99}})
        assert logs == []

    def test_non_error_opcode_does_not_extract_field1_as_error(self):
        # Outside the feed family field 1 is data, not a status code.
        ret, logs = self._convert(20, 11, Ace2Wire.message((1, 400)))

        assert ret == self._ok(20, 11, {"raw_fields": {1: 400}})
        assert logs == []

    @pytest.mark.parametrize("cmd", [8, 9, 10])
    def test_feed_family_opcode_still_extracts_error_code(self, cmd):
        ret, logs = self._convert(cmd, 11, bytes([0x08, 0x02]))

        assert ret == {"id": 11, "_cmd": cmd, "code": 2, "msg": "error_2",
                       "result": {"raw_fields": {1: 2}}}
        assert logs == []
        # A clean ack (field 1 = 0) is not an error.
        ok, ok_logs = self._convert(cmd, 11, bytes([0x08, 0x00]))
        assert ok == self._ok(cmd, 11, {"raw_fields": {1: 0}})
        assert ok_logs == []

    def test_feed_family_non_integer_field1_is_not_an_error(self):
        ret, logs = self._convert(8, 11, bytes([0x0A, 0x01, 0x02]))

        assert ret == self._ok(8, 11, {"raw_fields": {1: "hex:02"}})
        assert logs == []

    def test_unmapped_opcode_empty_payload_no_raw_fields(self):
        ret, logs = self._convert(20, 11, b"")

        assert ret == self._ok(20, 11, {})
        assert logs == []


class TestDecodeFrames:
    @staticmethod
    def _decode(buffer: bytearray, logger: Optional[AceLogger] = None
                ) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
        """
        :param buffer: received bytes, consumed in place
        :param logger: logger handed to decode_frames, None for the module's
        :return tuple: (the decoded responses, the "AFC_ACE2" log lines written)
        """
        with capture_log("AFC_ACE2") as log:
            results = decode_frames(buffer, logger)
        return results, log.messages

    @staticmethod
    def _temp(seq: int, box1: int) -> Dict[str, Any]:
        """
        :param seq: echoed sequence id
        :param box1: the only channel sent
        :return dict: the decoded GET_TEMP reply
        """
        return {"id": seq, "_cmd": 64, "code": 0, "msg": "success",
                "result": {"box1_temp": box1, "box2_temp": 0.0, "ptc1_temp": 0.0,
                           "ptc2_temp": 0.0, "env_temp": 0.0, "env_humidity": 0.0}}

    def test_short_buffer_returns_empty(self):
        # Nine bytes with no preamble: without the length guard the
        # no-preamble path would clear the buffer.
        buf = bytearray(b"\x11" * 9)

        assert self._decode(buf) == ([], [])
        assert buf == bytearray(b"\x11" * 9)

    def test_no_preamble_ending_in_ff_keeps_last_byte(self):
        # The trailing FF may be the first half of the next preamble.
        buf = bytearray(b"\x11" * 9 + b"\xff")

        assert self._decode(buf) == ([], [])
        assert buf == bytearray(b"\xff")

    def test_no_preamble_clears_buffer(self):
        buf = bytearray(b"\x11" * 10)

        assert self._decode(buf) == ([], [])
        assert buf == bytearray()

    def test_leading_garbage_before_preamble_is_dropped(self):
        buf = bytearray(b"\x00\x00" + Ace2Wire.reply(3, 64, bytes([0x08, 0x14])))

        assert self._decode(buf) == ([self._temp(3, 20)], [])
        assert buf == bytearray()

    def test_preamble_then_too_short_header_breaks(self):
        buf = bytearray(b"\x00" * 8 + b"\xff\xaa")

        assert self._decode(buf) == ([], [])
        assert buf == bytearray(b"\xff\xaa")

    def test_oversize_payload_len_resyncs(self):
        # A length byte of 101 cannot be a real frame: skip the preamble and
        # rescan, leaving eight bytes, too few for another frame.
        buf = bytearray(b"\xff\xaa" + bytes([0x80, 0x00, 0x00, 0x40, 101]) + b"\x00" * 3)

        assert self._decode(buf) == ([], [])
        assert buf == bytearray(bytes([0x80, 0x00, 0x00, 0x40, 101]) + b"\x00" * 3)

    def test_incomplete_frame_is_retained(self):
        # The header promises five payload bytes; three bytes follow it.
        partial = b"\xff\xaa" + bytes([0x80, 0x00, 0x00, 0x40, 0x05]) + b"\x00" * 3
        buf = bytearray(partial)

        assert self._decode(buf) == ([], [])
        assert buf == bytearray(partial)

    def test_bad_end_marker_resyncs(self):
        frame = bytearray(Ace2Wire.reply(3, 64, bytes([0x08, 0x14])))
        frame[-1] = 0x00
        good = Ace2Wire.reply(4, 64, bytes([0x08, 0x15]))
        buf = frame + bytearray(good)

        # The bad frame is skipped and the next frame still decodes.
        assert self._decode(buf) == ([self._temp(4, 21)], [])
        assert buf == bytearray()

    def test_crc_mismatch_is_dropped_and_logged(self):
        logger = AceLogger()
        # A complete frame hides in the bad frame's payload; only dropping the
        # whole bad frame (not resyncing past its preamble) keeps it hidden.
        inner = Ace2Wire.reply(9, 64, bytes([0x08, 0x16]))
        frame = bytearray(Ace2Wire.reply(3, 64, inner))
        frame[-3] ^= 0xFF
        buf = frame + bytearray(Ace2Wire.reply(4, 64, bytes([0x08, 0x15])))

        assert self._decode(buf, logger) == ([self._temp(4, 21)], [])
        assert logger.messages == [("debug", "ACE2 CRC mismatch, dropping frame")]
        assert buf == bytearray()

    def test_crc_mismatch_without_logger_uses_module_logger(self):
        frame = bytearray(Ace2Wire.reply(3, 64, bytes([0x08, 0x14])))
        frame[10] ^= 0xFF

        assert self._decode(frame) == ([], [("debug", "ACE2 CRC mismatch, dropping frame")])
        assert frame == bytearray()

    def test_request_frame_is_skipped(self):
        # A well-formed request (no response flag) is consumed, not returned.
        buf = bytearray(Ace2Wire.frame(3, 6) + Ace2Wire.reply(5, 64, bytes([0x08, 0x16])))

        assert self._decode(buf) == ([self._temp(5, 22)], [])
        assert buf == bytearray()

    def test_valid_response_frame_decoded(self):
        buf = bytearray(Ace2Wire.reply(7, 64, bytes([0x08, 0x15])))

        assert self._decode(buf) == ([self._temp(7, 21)], [])
        assert buf == bytearray()

    def test_two_frames_decoded_in_order(self):
        buf = bytearray(Ace2Wire.reply(1, 64, bytes([0x08, 0x0A]))
                        + Ace2Wire.reply(0x0102, 64, bytes([0x08, 0x14])))

        assert self._decode(buf) == ([self._temp(1, 10), self._temp(258, 20)], [])
        assert buf == bytearray()


class TestAce2ScanCandidates:
    def test_resolves_real_paths_in_sorted_glob_order(self, monkeypatch):
        # glob order is deliberately not sorted, to prove the sorted() wrapper
        # (not glob's own order) decides the result order.
        monkeypatch.setattr(afc_ace2_module.glob, "glob",
                            lambda pattern: ["/dev/by-id/2", "/dev/by-id/1"])
        monkeypatch.setattr(afc_ace2_module.os.path, "realpath",
                            lambda p: p.replace("by-id", "real"))

        assert _ace2_scan_candidates() == ["/dev/real/1", "/dev/real/2"]

    def test_realpath_failure_falls_back_to_raw_path(self, monkeypatch):
        monkeypatch.setattr(afc_ace2_module.glob, "glob", lambda pattern: ["/dev/by-id/0"])

        def boom(path: str) -> str:
            """
            :param path: the candidate path
            :return str: never; always raises
            """
            error_str = "no such path"
            raise OSError(error_str)

        monkeypatch.setattr(afc_ace2_module.os.path, "realpath", boom)

        assert _ace2_scan_candidates() == ["/dev/by-id/0"]

    def test_no_devices_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(afc_ace2_module.glob, "glob", lambda pattern: [])

        assert _ace2_scan_candidates() == []


class TestAce2TopologyKey:
    def test_matching_bypath_zero_pads_digit_runs(self, monkeypatch):
        monkeypatch.setattr(
            afc_ace2_module.glob, "glob",
            lambda pattern: ["/dev/serial/by-path/a1b22c333"])
        monkeypatch.setattr(afc_ace2_module.os.path, "realpath",
                            lambda p: "/dev/ttyACM5")

        # "1", "22" and "333" each zero-pad out to 6 digits.
        assert _ace2_topology_key("/dev/ttyACM5") == "a000001b000022c000333"

    def test_no_match_falls_back_to_zero_padded_real_tty(self, monkeypatch):
        monkeypatch.setattr(
            afc_ace2_module.glob, "glob",
            lambda pattern: ["/dev/serial/by-path/other"])
        monkeypatch.setattr(afc_ace2_module.os.path, "realpath", lambda p: "/dev/ttyACM9")

        # No by-path entry resolves to the real tty, so bypath stays empty and
        # the ternary falls back to the (zero-padded) real_tty argument.
        assert _ace2_topology_key("/dev/ttyACM7") == "/dev/ttyACM000007"

    def test_glob_exception_falls_back_to_zero_padded_real_tty(self, monkeypatch):
        def boom(pattern: str) -> List[str]:
            """
            :param pattern: the glob pattern
            :return list: never; always raises
            """
            error_str = "no /dev/serial/by-path"
            raise OSError(error_str)

        monkeypatch.setattr(afc_ace2_module.glob, "glob", boom)

        assert _ace2_topology_key("/dev/ttyACM7") == "/dev/ttyACM000007"

    def test_realpath_exception_on_one_entry_is_suppressed(self, monkeypatch):
        # The first by-path entry's realpath blows up; contextlib.suppress
        # swallows it and the loop still finds the second, matching entry.
        monkeypatch.setattr(
            afc_ace2_module.glob, "glob",
            lambda pattern: ["/dev/serial/by-path/bad", "/dev/serial/by-path/a1b22c333"])

        def realpath(path: str) -> str:
            """
            :param path: the by-path entry
            :return str: the resolved tty, or raises for the first entry
            """
            if path.endswith("bad"):
                error_str = "unreadable symlink"
                raise OSError(error_str)
            return "/dev/ttyACM5"

        monkeypatch.setattr(afc_ace2_module.os.path, "realpath", realpath)

        assert _ace2_topology_key("/dev/ttyACM5") == "a000001b000022c000333"


class TestAce2ExtractUid:
    UID = (2403054933, 129011976, 892745291)

    @classmethod
    def _discover_reply(cls, uid: Tuple[int, int, int] = UID) -> bytes:
        """
        :param uid: the unit's STM32 UID words
        :return bytes: the discover_device response frame carrying it
        """
        return Ace2Wire.reply(0, 0, Ace2Wire.message((1, uid[0]), (2, uid[1]), (3, uid[2])))

    def test_extract_uid_whole_frame(self):
        assert _ace2_extract_uid(bytearray(self._discover_reply())) == self.UID

    def test_extract_uid_tolerates_garbage_prefix(self):
        buf = bytearray(b"\x00\x11" + self._discover_reply((1, 2, 3)))

        assert _ace2_extract_uid(buf) == (1, 2, 3)

    def test_extract_uid_truncated_none(self):
        # No preamble yet, then a header cut short.
        assert _ace2_extract_uid(bytearray(b"\x00\x01")) is None
        assert _ace2_extract_uid(bytearray(self._discover_reply()[:6])) is None

    def test_extract_uid_incomplete_frame_none(self):
        # Header complete, end marker not yet arrived.
        assert _ace2_extract_uid(bytearray(self._discover_reply()[:-1])) is None

    def test_extract_uid_ignores_request_frame(self):
        frame = Ace2Wire.frame(0, 0, Ace2Wire.message((1, 9), (2, 9), (3, 9)))

        assert _ace2_extract_uid(bytearray(frame)) is None

    def test_extract_uid_ignores_other_opcode_reply(self):
        # A get_info reply is a response, but not the discover answer.
        frame = Ace2Wire.reply(0, 7, Ace2Wire.message((1, 9), (2, 9), (3, 9)))

        assert _ace2_extract_uid(bytearray(frame)) is None

    def test_extract_uid_missing_words_read_zero(self):
        frame = Ace2Wire.reply(0, 0, Ace2Wire.message((1, 5)))

        assert _ace2_extract_uid(bytearray(frame)) == (5, 0, 0)

    def test_extract_uid_unreadable_word_none(self):
        # A first word that is not a number cannot be a UID.
        frame = Ace2Wire.reply(0, 0, Ace2Wire.message((1, b"ab")))

        assert _ace2_extract_uid(bytearray(frame)) is None

    def test_extract_uid_field1_none_returns_none(self, monkeypatch):
        # _fval's own default (0) keeps field 1 from ever decoding to None in
        # practice; this proves the defensive "u1 is None" guard still returns
        # None on its own if that ever changed.
        monkeypatch.setattr(
            afc_ace2_module, "_fval",
            lambda fields, num, default=0: None if num == 1 else default)

        assert _ace2_extract_uid(bytearray(self._discover_reply())) is None


class TestProbeAce2Uid:
    class FakeSerialModule:
        """pyserial module stand-in for sys.modules['serial']: Serial(...)
        returns the given port, or raises open_error instead."""

        def __init__(self, serial: Optional[FakeSerial] = None,
                    open_error: Optional[BaseException] = None) -> None:
            """
            :param serial: the port object Serial() returns
            :param open_error: raised by Serial() instead, when set
            """
            self.serial = serial if serial is not None else FakeSerial()
            self.open_error = open_error
            self.opened: List[Tuple[str, int]] = []

        def Serial(self, port: str, baudrate: int, timeout: float,
                  write_timeout: float) -> FakeSerial:
            """
            :param port: tty path
            :param baudrate: baud rate
            :param timeout: read timeout
            :param write_timeout: write timeout
            :return FakeSerial: the opened port
            """
            self.opened.append((port, baudrate))
            if self.open_error is not None:
                raise self.open_error
            return self.serial

    class ChunkedSerial(FakeSerial):
        """A FakeSerial whose read() hands out one scripted chunk per call."""

        def __init__(self, chunks: List[bytes]) -> None:
            """
            :param chunks: bytes returned by successive read() calls
            """
            super().__init__()
            self._chunks = list(chunks)

        def read(self, size: int = 1) -> bytes:
            """
            :param size: unused; scripted chunks ignore it
            :return bytes: the next scripted chunk, b"" once exhausted
            """
            return self._chunks.pop(0) if self._chunks else b""

    class PreloadedSerial(FakeSerial):
        """A FakeSerial whose reset_input_buffer() leaves a pre-loaded .rx
        alone, as a real port's would if the reply already arrived."""

        def reset_input_buffer(self) -> None:
            """No-op: keeps .rx intact instead of clearing it."""

    class FlakySerial(FakeSerial):
        """A FakeSerial whose reset_input_buffer() and close() both raise."""

        def reset_input_buffer(self) -> None:
            """Never succeeds."""
            error_str = "reset failed"
            raise OSError(error_str)

        def close(self) -> None:
            """Never succeeds."""
            error_str = "close failed"
            raise OSError(error_str)

    class Clock:
        """Stands in for the module's time: time() reads .now, sleep() moves it."""

        def __init__(self, start: float = 1000.0) -> None:
            """
            :param start: initial clock value
            """
            self.now = start
            self.sleeps: List[float] = []

        def time(self) -> float:
            """
            :return float: the clock
            """
            return self.now

        def sleep(self, seconds: float) -> None:
            """
            :param seconds: how far to move the clock
            """
            self.sleeps.append(seconds)
            self.now += seconds

    @staticmethod
    def _discover_reply(uid: Tuple[int, int, int] = (11, 22, 33)) -> bytes:
        """
        :param uid: the unit's STM32 UID words
        :return bytes: the discover_device response frame carrying it
        """
        return Ace2Wire.reply(0, Ace2Wire.DISCOVER_DEVICE,
                              Ace2Wire.message((1, uid[0]), (2, uid[1]), (3, uid[2])))

    def test_pyserial_not_installed_returns_none(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "serial", None)

        assert probe_ace2_uid("/dev/ttyACM0", 230400) is None

    def test_open_failure_returns_none(self, monkeypatch):
        module = self.FakeSerialModule(open_error=OSError("port busy"))
        monkeypatch.setitem(sys.modules, "serial", module)

        assert probe_ace2_uid("/dev/ttyACM0", 230400) is None

        # Serial() was reached (and raised) before any ser.close() could run.
        assert module.opened == [("/dev/ttyACM0", 230400)]

    def test_success_on_first_chunk_writes_discover_and_closes(self, monkeypatch):
        # PreloadedSerial: probe_ace2_uid resets the input buffer right after
        # opening, which would wipe a plain FakeSerial's pre-loaded .rx.
        serial = self.PreloadedSerial()
        serial.rx += self._discover_reply()
        module = self.FakeSerialModule(serial=serial)
        monkeypatch.setitem(sys.modules, "serial", module)

        assert probe_ace2_uid("/dev/ttyACM0", 230400) == (11, 22, 33)

        assert serial.frames == [Ace2Wire.frame(0, Ace2Wire.DISCOVER_DEVICE)]
        assert serial.closed is True

    def test_partial_frame_then_completion_returns_uid(self, monkeypatch):
        reply = self._discover_reply((5, 6, 7))
        serial = self.ChunkedSerial([reply[:6], reply[6:]])
        module = self.FakeSerialModule(serial=serial)
        monkeypatch.setitem(sys.modules, "serial", module)

        assert probe_ace2_uid("/dev/ttyACM0", 230400) == (5, 6, 7)

    def test_no_data_times_out_and_returns_none(self, monkeypatch):
        clock = self.Clock()
        monkeypatch.setattr(afc_ace2_module, "time", clock)
        serial = FakeSerial()
        module = self.FakeSerialModule(serial=serial)
        monkeypatch.setitem(sys.modules, "serial", module)

        assert probe_ace2_uid("/dev/ttyACM0", 230400, timeout=0.05) is None

        # Three empty reads sleep 0.02s each before the clock passes deadline.
        assert clock.sleeps == [0.02, 0.02, 0.02]
        assert serial.closed is True

    def test_reset_and_close_exceptions_are_suppressed(self, monkeypatch):
        serial = self.FlakySerial()
        serial.rx += self._discover_reply((9, 8, 7))
        module = self.FakeSerialModule(serial=serial)
        monkeypatch.setitem(sys.modules, "serial", module)

        assert probe_ace2_uid("/dev/ttyACM0", 230400) == (9, 8, 7)


class TestResolveAce2Port:
    class Clock:
        """Stands in for the module's time: time() reads .now, sleep() moves it."""

        def __init__(self) -> None:
            """Start at 1000s with no sleeps."""
            self.now = 1000.0
            self.sleeps: List[float] = []

        def time(self) -> float:
            """
            :return float: the clock
            """
            return self.now

        def sleep(self, seconds: float) -> None:
            """
            :param seconds: how far to move the clock
            """
            self.sleeps.append(seconds)
            self.now += seconds

    class Bus:
        """The CH34x devices on USB: which ports exist when, and their UIDs."""

        def __init__(self, clock: "TestResolveAce2Port.Clock",
                     uid_by_port: Dict[str, Optional[Tuple[int, int, int]]],
                     topology: Optional[Dict[str, str]] = None,
                     appear_at: Optional[Dict[str, float]] = None) -> None:
            """
            :param clock: the patched clock
            :param uid_by_port: each port's discover UID, None for a non-ACE 2 device
            :param topology: each port's USB-topology sort key, the port name if absent
            :param appear_at: clock time a port enumerates at, present from the start if absent
            """
            self.clock = clock
            self.uid_by_port = uid_by_port
            self.topology = topology or {}
            self.appear_at = appear_at or {}
            self.probes: List[Tuple[str, int]] = []

        def scan(self) -> List[str]:
            """
            :return list: the ports enumerated now
            """
            return [port for port in self.uid_by_port
                    if self.appear_at.get(port, 0.0) <= self.clock.now]

        def probe(self, port: str, baud: int,
                  timeout: float = 1.5) -> Optional[Tuple[int, int, int]]:
            """
            :param port: port probed
            :param baud: baud rate
            :param timeout: probe timeout
            :return Optional[tuple]: the port's UID
            """
            self.probes.append((port, baud))
            return self.uid_by_port[port]

        def key(self, port: str) -> str:
            """
            :param port: port path
            :return str: its topology sort key
            """
            return self.topology.get(port, port)

    @pytest.fixture(autouse=True)
    def _isolated(self) -> Iterator[None]:
        """
        Start and end each test with no port claimed by another unit.

        :return Iterator[None]: the test's run
        """
        reset_ace_globals()
        yield
        reset_ace_globals()

    def _bus(self, monkeypatch: pytest.MonkeyPatch,
             uid_by_port: Dict[str, Optional[Tuple[int, int, int]]],
             **kwargs: Any) -> "TestResolveAce2Port.Bus":
        """
        Patch the CH34x scan, the discover probe, the topology key and the clock.

        :param monkeypatch: pytest's monkeypatch
        :param uid_by_port: each port's UID, None for a device that is not an ACE 2
        :param kwargs: Bus keywords (topology, appear_at)
        :return Bus: the bus, recording each probe
        """
        bus = self.Bus(self.Clock(), uid_by_port, **kwargs)
        monkeypatch.setattr(afc_ace2_module, "_ace2_scan_candidates", bus.scan)
        monkeypatch.setattr(afc_ace2_module, "probe_ace2_uid", bus.probe)
        monkeypatch.setattr(afc_ace2_module, "_ace2_topology_key", bus.key)
        monkeypatch.setattr(afc_ace2_module, "time", bus.clock)
        return bus

    def test_resolve_by_topology_cable_order(self, monkeypatch):
        bus = self._bus(monkeypatch, {"/dev/ttyACM9": (11, 0, 0), "/dev/ttyACM2": (22, 0, 0)},
                        topology={"/dev/ttyACM2": "usb-0:2.2", "/dev/ttyACM9": "usb-0:2.4"})

        # ace_index counts in cable (topology) order, not by tty number.
        assert resolve_ace2_port(1, 230400, settle=0.0) == "/dev/ttyACM2"
        assert resolve_ace2_port(2, 230400, settle=0.0) == "/dev/ttyACM9"
        assert resolve_ace2_port(3, 230400, settle=0.0) is None
        assert resolve_ace2_port(0, 230400, settle=0.0) is None
        # Each call probes each unit once and waits out the 0.5s minimum window.
        assert bus.probes == [("/dev/ttyACM9", 230400), ("/dev/ttyACM2", 230400)] * 4
        assert bus.clock.sleeps == [0.5] * 4

    def test_resolve_by_ace_uid_pin(self, monkeypatch):
        bus = self._bus(monkeypatch, {"/dev/ttyACM2": (11, 0, 0), "/dev/ttyACM9": (22, 33, 44)})

        # The pinned UID binds as soon as it answers, whatever its order.
        assert resolve_ace2_port(1, 230400, ace_uid=(22, 33, 44), settle=0.0) == "/dev/ttyACM9"
        assert bus.clock.sleeps == []
        assert resolve_ace2_port(1, 230400, ace_uid=(1, 2, 3), settle=0.0) is None
        assert bus.clock.sleeps == [0.5]

    def test_resolve_skips_claimed_but_keeps_its_topology_slot(self, monkeypatch):
        bus = self._bus(monkeypatch, {"/dev/ttyACM2": (11, 0, 0), "/dev/ttyACM9": (22, 0, 0)},
                        topology={"/dev/ttyACM2": "a", "/dev/ttyACM9": "b"})
        afc_ace2_module._ACE_CLAIMED_PORTS.add("/dev/ttyACM2")

        # The claimed port is never re-probed, but it still holds position 1
        # by topology, so the free unit stays at its real index 2 -- not
        # reassigned to index 1 just because the other unit is unavailable.
        assert resolve_ace2_port(2, 230400, settle=0.0) == "/dev/ttyACM9"
        assert bus.probes == [("/dev/ttyACM9", 230400)]

    def test_resolve_to_a_claimed_slot_returns_none(self, monkeypatch):
        """Asking for the index that maps to an already-claimed port must not
        hand back a port another unit's connection already has open."""
        bus = self._bus(monkeypatch, {"/dev/ttyACM2": (11, 0, 0), "/dev/ttyACM9": (22, 0, 0)},
                        topology={"/dev/ttyACM2": "a", "/dev/ttyACM9": "b"})
        afc_ace2_module._ACE_CLAIMED_PORTS.add("/dev/ttyACM2")

        assert resolve_ace2_port(1, 230400, settle=0.0) is None
        assert bus.probes == [("/dev/ttyACM9", 230400)]

    def test_resolve_ignores_non_ace2(self, monkeypatch):
        bus = self._bus(monkeypatch, {"/dev/ttyACM0": None, "/dev/ttyACM3": (7, 0, 0)},
                        topology={"/dev/ttyACM3": "a"})

        assert resolve_ace2_port(1, 230400, settle=0.0) == "/dev/ttyACM3"
        # A port that did not answer is probed again on the next pass.
        assert bus.probes == [("/dev/ttyACM0", 230400), ("/dev/ttyACM3", 230400),
                              ("/dev/ttyACM0", 230400)]

    def test_no_unit_found_returns_none(self, monkeypatch):
        self._bus(monkeypatch, {"/dev/ttyACM0": None})

        assert resolve_ace2_port(1, 230400, settle=0.0) is None

    def test_settle_window_rescans_for_late_units(self, monkeypatch):
        # The second unit enumerates 2s in, as after a watchdog reset.
        bus = self._bus(monkeypatch, {"/dev/ttyACM0": (1, 0, 0), "/dev/ttyACM1": (2, 0, 0)},
                        appear_at={"/dev/ttyACM1": 1002.0})

        assert resolve_ace2_port(2, 115200, settle=6.0) == "/dev/ttyACM1"
        # Passes every 0.5s until the window ends at 1006s; each unit probed once.
        assert bus.clock.sleeps == [0.5] * 12
        assert bus.probes == [("/dev/ttyACM0", 115200), ("/dev/ttyACM1", 115200)]

    def test_logs_each_discovered_unit(self, monkeypatch):
        self._bus(monkeypatch, {"/dev/ttyACM2": (11, 0, 0), "/dev/ttyACM0": None,
                                "/dev/ttyACM9": (22, 33, 44)})
        logger = AceLogger()

        assert resolve_ace2_port(1, 230400, settle=0.0, logger=logger) == "/dev/ttyACM2"
        assert logger.messages == [
            ("info", "ACE2 autodetect: /dev/ttyACM2 -> uid (11, 0, 0)"),
            ("info", "ACE2 autodetect: /dev/ttyACM9 -> uid (22, 33, 44)")]


class TestACE2ConnectionResolveConnectPort:
    def test_not_auto_returns_configured_port(self):
        # ace2_link's default serial_port ("/dev/ttyACM0") is not "auto".
        printer, conn = ace2_link()

        assert conn._resolve_connect_port() == "/dev/ttyACM0"

    def test_auto_no_cached_port_resolves_fresh_with_default_logger(self, monkeypatch):
        printer, conn = ace2_link()
        conn._auto = True
        conn._resolved_port = None
        conn._ace_index = 3
        conn._ace_uid = None
        calls = []

        def fake_resolve(ace_index, baud_rate, ace_uid=None, logger=None):
            """
            :param ace_index: unit index passed through
            :param baud_rate: baud rate passed through
            :param ace_uid: UID pin passed through
            :param logger: logger passed through
            :return str: a fixed resolved port
            """
            calls.append((ace_index, baud_rate, ace_uid, logger))
            return "/dev/ttyACM7"

        monkeypatch.setattr(afc_ace2_module, "resolve_ace2_port", fake_resolve)

        assert conn._resolve_connect_port() == "/dev/ttyACM7"
        # No logger given, so the connection's own logger is passed through.
        assert calls == [(3, conn._baud_rate, None, conn._logger)]

    def test_logger_argument_overrides_connection_logger(self, monkeypatch):
        printer, conn = ace2_link()
        conn._auto = True
        conn._resolved_port = None
        calls = []
        given_logger = AceLogger()

        def fake_resolve(ace_index, baud_rate, ace_uid=None, logger=None):
            """
            :param ace_index: unit index, unused here
            :param baud_rate: baud rate, unused here
            :param ace_uid: UID pin, unused here
            :param logger: logger passed through
            :return str: a fixed resolved port
            """
            calls.append(logger)
            return "/dev/ttyACM7"

        monkeypatch.setattr(afc_ace2_module, "resolve_ace2_port", fake_resolve)

        assert conn._resolve_connect_port(logger=given_logger) == "/dev/ttyACM7"
        assert calls == [given_logger]
        assert given_logger is not conn._logger

    def test_auto_resolve_fails_raises_without_uid_in_message(self, monkeypatch):
        printer, conn = ace2_link()
        conn._auto = True
        conn._resolved_port = None
        conn._ace_index = 2
        conn._ace_uid = None
        monkeypatch.setattr(afc_ace2_module, "resolve_ace2_port", lambda *a, **k: None)

        with pytest.raises(ACESerialError) as excinfo:
            conn._resolve_connect_port()

        assert str(excinfo.value) == (
            "ACE2 autodetect: no unit found for ace_index 2 (serial_port: auto). "
            "Is it plugged in / powered?")

    def test_auto_resolve_fails_with_uid_in_message(self, monkeypatch):
        printer, conn = ace2_link()
        conn._auto = True
        conn._resolved_port = None
        conn._ace_index = 4
        conn._ace_uid = (1, 2, 3)
        monkeypatch.setattr(afc_ace2_module, "resolve_ace2_port", lambda *a, **k: None)

        with pytest.raises(ACESerialError) as excinfo:
            conn._resolve_connect_port()

        assert str(excinfo.value) == (
            "ACE2 autodetect: no unit found for ace_index 4 / ace_uid (1, 2, 3) "
            "(serial_port: auto). Is it plugged in / powered?")

    def test_auto_resolve_succeeds_returns_port(self, monkeypatch):
        printer, conn = ace2_link()
        conn._auto = True
        conn._resolved_port = None
        monkeypatch.setattr(afc_ace2_module, "resolve_ace2_port",
                            lambda *a, **k: "/dev/ttyACM8")

        assert conn._resolve_connect_port() == "/dev/ttyACM8"
        # Binding happens later from the discover_device handshake once
        # connected (see TestACE2ConnectionPreInfoHandshake), not here.
        assert conn._bound_uid is None

    def test_cached_port_probe_exception_falls_through_to_resolve(self, monkeypatch):
        printer, conn = ace2_link()
        conn._auto = True
        conn._resolved_port = "/dev/ttyACM2"
        conn._ace_uid = None

        def boom(port, baud):
            """
            :param port: probed port, unused here
            :param baud: baud rate, unused here
            :return None: never; always raises
            """
            error_str = "probe failed"
            raise OSError(error_str)

        monkeypatch.setattr(afc_ace2_module, "probe_ace2_uid", boom)
        monkeypatch.setattr(afc_ace2_module, "resolve_ace2_port",
                            lambda *a, **k: "/dev/ttyACM9")

        assert conn._resolve_connect_port() == "/dev/ttyACM9"

    def test_cached_port_probe_returns_none_falls_through_to_resolve(self, monkeypatch):
        printer, conn = ace2_link()
        conn._auto = True
        conn._resolved_port = "/dev/ttyACM2"
        conn._ace_uid = None
        monkeypatch.setattr(afc_ace2_module, "probe_ace2_uid", lambda port, baud: None)
        monkeypatch.setattr(afc_ace2_module, "resolve_ace2_port",
                            lambda *a, **k: "/dev/ttyACM9")

        assert conn._resolve_connect_port() == "/dev/ttyACM9"

    def test_cached_port_with_bound_uid_and_no_pin_reuses_cached_port(self, monkeypatch):
        """With no ace_uid configured, the cache hit needs a uid bound from a
        previous successful connect (_bound_uid) to trust against -- simulated
        here directly, since it's normally set from the discover_device
        handshake reply once connected (see TestACE2ConnectionPreInfoHandshake)."""
        printer, conn = ace2_link()
        conn._auto = True
        conn._resolved_port = "/dev/ttyACM2"
        conn._ace_uid = None
        conn._bound_uid = (1, 2, 3)
        monkeypatch.setattr(afc_ace2_module, "probe_ace2_uid", lambda port, baud: (1, 2, 3))
        resolve_calls = []
        monkeypatch.setattr(afc_ace2_module, "resolve_ace2_port",
                            lambda *a, **k: resolve_calls.append(1))

        assert conn._resolve_connect_port() == "/dev/ttyACM2"
        # The cheap cache hit short-circuits before the full scan ever runs.
        assert resolve_calls == []
        assert conn._bound_uid == (1, 2, 3)

    def test_cached_port_with_no_pin_and_no_bound_uid_falls_through_to_resolve(self, monkeypatch):
        """Nothing to validate the cached port against yet (no ace_uid, no
        prior bind), so even a successful probe must not short-circuit the
        full scan."""
        printer, conn = ace2_link()
        conn._auto = True
        conn._resolved_port = "/dev/ttyACM2"
        conn._ace_uid = None
        assert conn._bound_uid is None
        monkeypatch.setattr(afc_ace2_module, "probe_ace2_uid", lambda port, baud: (1, 2, 3))
        monkeypatch.setattr(afc_ace2_module, "resolve_ace2_port",
                            lambda *a, **k: "/dev/ttyACM9")

        assert conn._resolve_connect_port() == "/dev/ttyACM9"

    def test_cached_port_uid_matches_pin_reuses_cached_port(self, monkeypatch):
        printer, conn = ace2_link()
        conn._auto = True
        conn._resolved_port = "/dev/ttyACM2"
        conn._ace_uid = (1, 2, 3)
        monkeypatch.setattr(afc_ace2_module, "probe_ace2_uid", lambda port, baud: (1, 2, 3))
        resolve_calls = []
        monkeypatch.setattr(afc_ace2_module, "resolve_ace2_port",
                            lambda *a, **k: resolve_calls.append(1))

        assert conn._resolve_connect_port() == "/dev/ttyACM2"
        assert resolve_calls == []
        assert conn._bound_uid == (1, 2, 3)

    def test_cached_port_uid_mismatch_falls_through_to_resolve(self, monkeypatch):
        printer, conn = ace2_link()
        conn._auto = True
        conn._resolved_port = "/dev/ttyACM2"
        conn._ace_uid = (9, 9, 9)
        monkeypatch.setattr(afc_ace2_module, "probe_ace2_uid", lambda port, baud: (1, 2, 3))
        monkeypatch.setattr(afc_ace2_module, "resolve_ace2_port",
                            lambda *a, **k: "/dev/ttyACM9")

        assert conn._resolve_connect_port() == "/dev/ttyACM9"


class TestACE2ConnectionPreInfoHandshake:
    def test_sends_discover_device(self):
        printer, conn = ace2_link()
        ace2_link_answers(conn, _cmd=0, code=0, msg="success",
                          result={"uid1": 1, "uid2": 2, "uid3": 3})

        conn._pre_info_handshake()

        assert conn._serial.frames == [Ace2Wire.frame(0, Ace2Wire.DISCOVER_DEVICE)]
        assert conn._pending == {}
        assert conn._timeout_timestamps == []
        assert printer.logger.messages == [("debug", "ACE2 TX: id=0 discover_device {}")]
        # The discover reply's uid is bound for later cache-hit checks in
        # _resolve_connect_port, at no extra serial-open cost.
        assert conn._bound_uid == (1, 2, 3)

    def test_exception_is_swallowed_and_logged(self):
        # The unit never answers: the discover times out after its 3s, and
        # that is logged rather than raised.
        printer, conn = ace2_link()

        conn._pre_info_handshake()

        assert printer.reactor.now == 103.0
        assert conn._timeout_timestamps == [103.0]
        assert printer.logger.messages == [
            ("debug", "ACE2 TX: id=0 discover_device {}"),
            ("debug", "ACE2 discover_device failed (non-fatal): ACE2 command "
                      "'discover_device' (id=0) timed out after 3.0s")]
        assert conn._bound_uid is None

    def test_reply_missing_a_uid_field_does_not_bind(self):
        """Covers the all-present guard: a reply with only some of
        uid1/uid2/uid3 must not bind a partial uid."""
        printer, conn = ace2_link()
        ace2_link_answers(conn, _cmd=0, code=0, msg="success",
                          result={"uid1": 1, "uid2": 2})

        conn._pre_info_handshake()

        assert conn._bound_uid is None

    def test_non_dict_reply_does_not_bind(self, monkeypatch):
        """Covers the isinstance guard: some other command's decode shape
        (not a dict) must not be treated as a uid-bearing reply."""
        printer, conn = ace2_link()
        monkeypatch.setattr(conn, "send_command", lambda *a, **k: None)

        conn._pre_info_handshake()

        assert conn._bound_uid is None


class TestACE2ConnectionPollExtras:
    def test_polls_temp_and_sensor_state(self):
        printer, conn = ace2_link(next_id=4)

        conn._poll_extras()

        assert conn._serial.frames == [Ace2Wire.frame(4, Ace2Wire.GET_TEMP),
                                       Ace2Wire.frame(5, Ace2Wire.GET_SENSOR_STATE)]
        assert list(conn._async_ids) == [4, 5]
        assert printer.logger.messages == [("debug", "ACE2 TX (async): id=4 get_temp"),
                                           ("debug", "ACE2 TX (async): id=5 get_sensor_state")]


class TestACE2ConnectionSendCommand:
    def test_not_connected_flag_raises(self):
        printer, conn = ace2_link()
        conn._connected = False

        with pytest.raises(ACESerialError) as excinfo:
            conn.send_command("get_status")

        assert str(excinfo.value) == "ACE2 not connected"
        # Refused before taking a request id.
        assert conn._next_request_id == 0
        assert conn._serial.frames == []
        assert printer.logger.messages == []

    def test_serial_none_raises(self):
        printer, conn = ace2_link()
        conn._serial = None

        with pytest.raises(ACESerialError) as excinfo:
            conn.send_command("get_status")

        assert str(excinfo.value) == "ACE2 not connected"
        assert conn._next_request_id == 0
        assert conn._pending == {}
        assert printer.logger.messages == []

    def test_encode_failure_raises_serial_error(self):
        printer, conn = ace2_link()

        # A 200-character name overflows the 100-byte frame payload.
        with pytest.raises(ACESerialError) as excinfo:
            conn.send_command("set_material_name", {"index": 0, "name": "x" * 200})

        assert str(excinfo.value) == ("ACE2 encode failed for 'set_material_name': "
                                      "V2 payload exceeds 100 bytes for opcode 18")
        assert conn._next_request_id == 1
        assert conn._serial.frames == []
        assert conn._pending == {}
        assert conn._pending_cmd == {}
        assert printer.logger.messages == []

    def test_write_failure_reconnects_and_raises(self):
        serial = FakeSerial(write_error=OSError("cable"))
        printer, conn = ace2_link(serial=serial, reconnect_enabled=True)

        with pytest.raises(ACESerialError) as excinfo:
            conn.send_command("get_status")

        assert str(excinfo.value) == "ACE2 write failed: cable"
        assert conn._timeout_timestamps == [100.0]
        # Reconnected at once: the port is closed and a retry timer is due in 5s.
        assert conn.connected is False
        assert conn._serial is None
        assert serial.closed is True
        assert conn._reconnect_backoff == 7.5
        assert [timer.waketime for timer in printer.reactor.timers] == [105.0]
        assert printer.logger.messages == [
            ("info", "ACE serial disconnected"),
            ("info", "ACE scheduling reconnect in 5s (next backoff: 8s)")]

    def test_write_failure_pops_only_its_own_pending(self):
        printer, conn = ace2_link(serial=FakeSerial(write_error=OSError("cable")))
        other = printer.reactor.completion()
        conn._pending[9] = other
        conn._pending_cmd[9] = Ace2Wire.GET_TEMP

        with pytest.raises(ACESerialError) as excinfo:
            conn.send_command("get_status")

        assert str(excinfo.value) == "ACE2 write failed: cable"
        # Another request in flight keeps its entries.
        assert conn._pending == {9: other}
        assert conn._pending_cmd == {9: Ace2Wire.GET_TEMP}
        assert conn._timeout_timestamps == [100.0]
        assert conn.connected is True
        assert printer.logger.messages == []

    def test_timeout_raises_and_tracks(self):
        printer, conn = ace2_link(next_id=5)

        with pytest.raises(ACETimeoutError) as excinfo:
            conn.send_command("get_status", timeout=2.5)

        assert str(excinfo.value) == "ACE2 command 'get_status' (id=5) timed out after 2.5s"
        assert printer.reactor.now == 102.5
        assert conn._timeout_timestamps == [102.5]
        assert conn._pending == {}
        assert conn._pending_cmd == {}
        assert conn._serial.frames == [Ace2Wire.frame(5, Ace2Wire.GET_STATUS)]
        assert printer.logger.messages == [("debug", "ACE2 TX: id=5 get_status {}")]

    def test_success_returns_result_and_logs_tx(self):
        printer, conn = ace2_link(next_id=5)
        ace2_link_answers(conn, _cmd=6, code=0, msg="success", result={"status": "ready"})

        assert conn.send_command("get_status") == {"status": "ready"}

        assert conn._next_request_id == 6
        assert conn._pending == {}
        assert conn._pending_cmd == {}
        assert conn._timeout_timestamps == []
        assert printer.reactor.now == 100.0
        assert printer.logger.messages == [("debug", "ACE2 TX: id=5 get_status {}")]

    def test_params_are_encoded_and_logged(self):
        printer, conn = ace2_link()
        ace2_link_answers(conn, _cmd=14, code=0, msg="success", result={})

        assert conn.send_command("set_rfid_enable", {"index": 2, "enable": False}) == {}

        assert conn._serial.frames == [Ace2Wire.frame(0, Ace2Wire.SET_RFID_ENABLE,
                                                      bytes([0x08, 0x02, 0x10, 0x00]))]
        assert printer.logger.messages == [
            ("debug", "ACE2 TX: id=0 set_rfid_enable {'index': 2, 'enable': False}")]

    def test_error_code_raises(self):
        printer, conn = ace2_link(next_id=5)
        ace2_link_answers(conn, _cmd=6, code=2, msg="error_2", result={})

        with pytest.raises(ACESerialError) as excinfo:
            conn.send_command("get_status")

        assert str(excinfo.value) == "ACE2 command 'get_status' failed: code=2, msg=error_2"
        assert conn._pending == {}
        assert conn._pending_cmd == {}
        assert printer.logger.messages == [("debug", "ACE2 TX: id=5 get_status {}")]

    def test_error_code_without_msg_reads_error(self):
        printer, conn = ace2_link()
        ace2_link_answers(conn, _cmd=6, code=5)

        with pytest.raises(ACESerialError) as excinfo:
            conn.send_command("get_status")

        assert str(excinfo.value) == "ACE2 command 'get_status' failed: code=5, msg=error"
        assert printer.logger.messages == [("debug", "ACE2 TX: id=0 get_status {}")]

    def test_dict_without_code_or_result_returned_whole(self):
        printer, conn = ace2_link()
        ace2_link_answers(conn)

        assert conn.send_command("get_status") == {"id": 0}
        assert printer.logger.messages == [("debug", "ACE2 TX: id=0 get_status {}")]

    def test_non_dict_result_returned_verbatim(self):
        printer, conn = ace2_link(next_id=5)

        def complete(frame: bytes) -> None:
            """
            :param frame: the request frame just written
            """
            conn._pending[frame[3] | (frame[4] << 8)].complete(4242)

        conn._serial.on_write = complete

        assert conn.send_command("get_status") == 4242
        assert conn._pending == {}
        assert printer.logger.messages == [("debug", "ACE2 TX: id=5 get_status {}")]

    def test_send_command_completes_after_wrap(self):
        # Past id 65535 the wire id wraps: 86480 goes out, and is echoed, as 20944.
        printer, conn = ace2_link(next_id=86480)
        ace2_link_answers(conn, code=0, result={"ok": 1})

        assert conn.send_command("get_status", timeout=1.0) == {"ok": 1}

        assert conn._serial.frames == [Ace2Wire.frame(20944, Ace2Wire.GET_STATUS)]
        assert conn._next_request_id == 86481
        assert conn._pending == {}
        assert printer.logger.messages == [("debug", "ACE2 TX: id=20944 get_status {}")]

    def test_reply_with_mismatched_opcode_is_dropped(self):
        # A stale reply landing on a reused id, for another opcode (8, a feed),
        # must not complete this get_status (opcode 6); it times out instead.
        printer, conn = ace2_link(next_id=86480)
        ace2_link_answers(conn, _cmd=8, code=0, result={"stale": 1})

        with pytest.raises(ACETimeoutError) as excinfo:
            conn.send_command("get_status", timeout=1.0)

        assert str(excinfo.value) == ("ACE2 command 'get_status' (id=20944) timed out "
                                      "after 1.0s")
        assert conn._pending == {}
        assert conn._pending_cmd == {}
        assert printer.logger.messages == [
            ("debug", "ACE dropping mismatched reply for id=20944"),
            ("debug", "ACE2 TX: id=20944 get_status {}")]

    def test_reply_with_matching_opcode_completes(self):
        printer, conn = ace2_link(next_id=86480)
        ace2_link_answers(conn, _cmd=6, code=0, result={"ok": 1})

        assert conn.send_command("get_status", timeout=1.0) == {"ok": 1}
        assert printer.logger.messages == [("debug", "ACE2 TX: id=20944 get_status {}")]

    def test_send_command_pending_keyed_by_masked_id(self):
        printer, conn = ace2_link(next_id=86480)
        seen: Dict[str, Any] = {}

        def capture(frame: bytes) -> None:
            """
            :param frame: the request frame just written
            """
            seen["pending"] = list(conn._pending)
            seen["pending_cmd"] = dict(conn._pending_cmd)

        conn._serial.on_write = capture

        with pytest.raises(ACETimeoutError) as excinfo:
            conn.send_command("get_status")

        # Keyed by the 16-bit id the unit will echo, with the opcode sent.
        assert seen == {"pending": [20944], "pending_cmd": {20944: 6}}
        assert str(excinfo.value) == ("ACE2 command 'get_status' (id=20944) timed out "
                                      "after 5.0s")
        assert conn._pending == {}
        assert conn._pending_cmd == {}
        assert printer.logger.messages == [("debug", "ACE2 TX: id=20944 get_status {}")]


class TestACE2ConnectionSendCommandAsync:
    def test_not_connected_flag_returns_early(self):
        printer, conn = ace2_link()
        conn._connected = False

        conn.send_command_async("get_status")

        assert conn._next_request_id == 0
        assert list(conn._async_ids) == []
        assert conn._serial.frames == []
        assert printer.logger.messages == []

    def test_serial_none_returns_early(self):
        printer, conn = ace2_link()
        conn._serial = None

        conn.send_command_async("get_status")

        assert conn._next_request_id == 0
        assert list(conn._async_ids) == []
        assert printer.logger.messages == []

    def test_success_writes_frame_and_tracks_id(self):
        printer, conn = ace2_link()

        conn.send_command_async("get_status")

        assert conn._serial.frames == [Ace2Wire.frame(0, Ace2Wire.GET_STATUS)]
        assert list(conn._async_ids) == [0]
        assert conn._next_request_id == 1
        assert printer.logger.messages == [("debug", "ACE2 TX (async): id=0 get_status")]

    def test_encode_failure_logged_and_swallowed(self):
        printer, conn = ace2_link()

        conn.send_command_async("set_material_name", {"index": 0, "name": "x" * 200})

        assert conn._serial.frames == []
        # The id was taken and tracked before the encode failed.
        assert list(conn._async_ids) == [0]
        assert conn._next_request_id == 1
        assert printer.logger.messages == [
            ("debug", "ACE2 async encode failed: V2 payload exceeds 100 bytes for opcode 18")]

    def test_write_failure_reconnects_and_logs(self):
        serial = FakeSerial(write_error=OSError("cable"))
        printer, conn = ace2_link(serial=serial, reconnect_enabled=True)

        conn.send_command_async("get_status")

        assert conn.connected is False
        assert serial.closed is True
        assert list(conn._async_ids) == []
        assert [timer.waketime for timer in printer.reactor.timers] == [105.0]
        # No TX line: it returns after asking for the reconnect.
        assert printer.logger.messages == [
            ("debug", "ACE2 async write failed: cable"),
            ("info", "ACE serial disconnected"),
            ("info", "ACE scheduling reconnect in 5s (next backoff: 8s)")]

    def test_async_id_tracked_masked_and_recognised(self):
        printer, conn = ace2_link(next_id=86480)

        conn.send_command_async("get_status")

        assert list(conn._async_ids) == [20944]
        assert conn._serial.frames == [Ace2Wire.frame(20944, Ace2Wire.GET_STATUS)]
        # The reply with the echoed 16-bit id is recognised as this request's,
        # not counted or logged as an unknown request.
        conn._handle_response({"id": 20944, "_cmd": 6, "code": 0, "msg": "success",
                               "result": {"status": "ready"}})
        assert list(conn._async_ids) == []
        assert conn._unsolicited_timestamps == []
        assert printer.logger.messages == [("debug", "ACE2 TX (async): id=20944 get_status")]

    def test_pre_wrap_ids_unaffected(self):
        printer, conn = ace2_link(next_id=5)

        conn.send_command_async("get_status")

        assert list(conn._async_ids) == [5]
        assert conn._serial.frames == [Ace2Wire.frame(5, Ace2Wire.GET_STATUS)]
        assert conn._next_request_id == 6
        assert printer.logger.messages == [("debug", "ACE2 TX (async): id=5 get_status")]


class TestACE2ConnectionResponseMatchesPending:
    def test_no_recorded_opcode_accepts(self):
        printer, conn = ace2_link()

        assert conn._response_matches_pending(9, {"_cmd": 6}) is True
        assert printer.logger.messages == []

    def test_non_dict_response_accepts(self):
        printer, conn = ace2_link()
        conn._pending_cmd[9] = 6

        assert conn._response_matches_pending(9, "not-a-dict") is True
        assert printer.logger.messages == []

    def test_dict_without_cmd_accepts(self):
        printer, conn = ace2_link()
        conn._pending_cmd[9] = 6

        assert conn._response_matches_pending(9, {"code": 0}) is True
        assert printer.logger.messages == []

    def test_matching_opcode_accepts(self):
        printer, conn = ace2_link()
        conn._pending_cmd[9] = 6

        assert conn._response_matches_pending(9, {"_cmd": 6}) is True
        assert printer.logger.messages == []

    def test_mismatched_opcode_rejected(self):
        printer, conn = ace2_link()
        conn._pending_cmd[9] = 6

        assert conn._response_matches_pending(9, {"_cmd": 8}) is False
        assert printer.logger.messages == []


class TestACE2ConnectionParseFrames:
    def test_complete_frame_routed_and_buffer_consumed(self):
        printer, conn = ace2_link()
        completion = printer.reactor.completion()
        conn._pending[7] = completion
        conn._pending_cmd[7] = Ace2Wire.GET_TEMP
        conn._read_buffer = Ace2Wire.reply(7, Ace2Wire.GET_TEMP, bytes([0x08, 0x15]))

        conn._parse_frames()

        expected = {"id": 7, "_cmd": 64, "code": 0, "msg": "success",
                    "result": {"box1_temp": 21, "box2_temp": 0.0, "ptc1_temp": 0.0,
                               "ptc2_temp": 0.0, "env_temp": 0.0, "env_humidity": 0.0}}
        assert completion.done is True
        assert completion.result == expected
        assert conn._read_buffer == b""
        assert printer.logger.messages == [(
            "debug", "ACE2 RX: {'id': 7, '_cmd': 64, 'code': 0, 'msg': 'success', "
                     "'result': {'box1_temp': 21, 'box2_temp': 0.0, 'ptc1_temp': 0.0, "
                     "'ptc2_temp': 0.0, 'env_temp': 0.0, 'env_humidity': 0.0}}")]

    def test_partial_frame_retained_in_buffer(self):
        printer, conn = ace2_link()
        # The header promises five payload bytes; three have arrived.
        partial = b"\xff\xaa" + bytes([0x80, 0x00, 0x00, 0x40, 0x05]) + b"\x00" * 3
        conn._read_buffer = partial

        conn._parse_frames()

        assert conn._read_buffer == partial
        assert printer.logger.messages == []


class TestACE2ConnectionEnableRfid:
    def test_enables_every_slot(self):
        printer, conn = ace2_link()

        conn.enable_rfid()

        assert conn._serial.frames == [
            Ace2Wire.frame(slot, Ace2Wire.SET_RFID_ENABLE, bytes([0x08, slot, 0x10, 0x01]))
            for slot in range(4)]
        assert list(conn._async_ids) == [0, 1, 2, 3]
        assert printer.logger.messages == [
            ("debug", "ACE2 TX (async): id=0 set_rfid_enable"),
            ("debug", "ACE2 TX (async): id=1 set_rfid_enable"),
            ("debug", "ACE2 TX (async): id=2 set_rfid_enable"),
            ("debug", "ACE2 TX (async): id=3 set_rfid_enable")]

    def test_follows_slot_count(self):
        printer, conn = ace2_link()
        conn.slot_count = 2

        conn.enable_rfid()

        assert conn._serial.frames == [
            Ace2Wire.frame(0, Ace2Wire.SET_RFID_ENABLE, bytes([0x08, 0x00, 0x10, 0x01])),
            Ace2Wire.frame(1, Ace2Wire.SET_RFID_ENABLE, bytes([0x08, 0x01, 0x10, 0x01]))]
        assert list(conn._async_ids) == [0, 1]
        assert printer.logger.messages == [
            ("debug", "ACE2 TX (async): id=0 set_rfid_enable"),
            ("debug", "ACE2 TX (async): id=1 set_rfid_enable")]


class TestACE2ConnectionDisableRfid:
    def test_disables_every_slot(self):
        printer, conn = ace2_link()

        conn.disable_rfid()

        assert conn._serial.frames == [
            Ace2Wire.frame(slot, Ace2Wire.SET_RFID_ENABLE, bytes([0x08, slot, 0x10, 0x00]))
            for slot in range(4)]
        assert list(conn._async_ids) == [0, 1, 2, 3]
        assert printer.logger.messages == [
            ("debug", "ACE2 TX (async): id=0 set_rfid_enable"),
            ("debug", "ACE2 TX (async): id=1 set_rfid_enable"),
            ("debug", "ACE2 TX (async): id=2 set_rfid_enable"),
            ("debug", "ACE2 TX (async): id=3 set_rfid_enable")]


class TestAfcACE2Init:
    def test_ace_uid_non_integer_raises_config_error(self):
        # "abc" cannot parse as int: the except ValueError branch leaves nums
        # empty, which then also fails the length-3 check below it.
        with pytest.raises(configparser.Error) as excinfo:
            make_ace2_unit(values={"ace_uid": "11,abc,33"})

        assert str(excinfo.value) == (
            "[AFC_ACE2 Ace2_1] ace_uid must be three integers 'uid1,uid2,uid3' "
            "(copy them from the discover line in AFC_ACE_serial.log); "
            "got '11,abc,33'")

    def test_ace_uid_wrong_count_raises_config_error(self):
        # Two valid integers parse fine, but still fail the length-3 check
        # without ever raising ValueError.
        with pytest.raises(configparser.Error) as excinfo:
            make_ace2_unit(values={"ace_uid": "11,22"})

        assert str(excinfo.value) == (
            "[AFC_ACE2 Ace2_1] ace_uid must be three integers 'uid1,uid2,uid3' "
            "(copy them from the discover line in AFC_ACE_serial.log); "
            "got '11,22'")


class TestLoadConfigPrefix:
    def test_returns_configured_afcace2_instance(self):
        # Reuses the first unit's already-registered hub/extruder section
        # names; load_config_prefix only constructs the instance, it does not
        # dereference them (that happens later, on handle_connect).
        unit = make_ace2_unit()
        printer = unit.printer
        cfg = {"serial_port": "/dev/ttyACM9", "hub": f"{unit.name}_hub",
              "extruder": "extruder", "ace_index": 3}
        config = AceConfig("AFC_ACE2 Ace2_2", printer, cfg)

        result = load_config_prefix(config)

        assert isinstance(result, afcACE2)
        assert result.name == "Ace2_2"
        assert result.serial_port == "/dev/ttyACM9"
        assert result.ace_index == 3


class TestAfcACE2ApplyFeedCheck:
    class RefusingLink(FakeAce2Connection):
        """A scripted link whose fire-and-forget sends raise, as a wedged port does."""
        error: Optional[BaseException] = None

        def send_command_async(self, method: str,
                               params: Optional[Dict[str, Any]] = None) -> None:
            """
            Record the send as the scripted link does, then raise .error.

            :param method: method name
            :param params: params dict
            """
            super().send_command_async(method, params)
            if self.error is not None:
                raise self.error

    def test_no_ace_returns_without_send(self):
        unit = make_ace2_unit(connection=None)

        unit._apply_feed_check()

        # Neither the info line of a send nor the warning of a failed one.
        assert unit.printer.logger.messages == []
        assert unit.printer.timeline == []

    def test_success_sends_and_logs_info(self):
        unit = make_ace2_unit(values={"feed_check_length": 150, "feed_error_length": 130})

        unit._apply_feed_check()

        assert unit._ace.async_commands == [
            ("set_feed_check", {"check_length": 150, "error_length": 130})]
        assert unit.printer.logger.messages == [
            ("info", "ACE2 Ace2_1: feed check set check_length=150 error_length=130")]

    def test_send_failure_logs_warning(self):
        printer = AcePrinter()
        link = make_fake_ace_connection(ace2=True, printer=printer, cls=self.RefusingLink)
        link.error = RuntimeError("serial down")
        unit = make_ace2_unit(printer=printer, connection=link)

        unit._apply_feed_check()

        assert link.async_commands == [
            ("set_feed_check", {"check_length": 200, "error_length": 185})]
        assert printer.logger.messages == [
            ("warning", "ACE2 Ace2_1: set_feed_check failed (non-fatal): serial down")]


class TestAfcACE2CheckStuck:
    STUCK = "stuck spool (encoder saw no movement while feeding)"

    @staticmethod
    def _unit(**kwargs: Any) -> afcACE2:
        """
        :param kwargs: make_ace2_unit keywords over the defaults
        :return afcACE2: printing, lane1 (slot 0) loaded in the tool, lane2 (slot 1) prepped
        """
        options: Dict[str, Any] = {
            "lanes": [LaneSpec("lane1", prep=True, tool_loaded=True),
                      LaneSpec("lane2", prep=True)],
            "in_print": True}
        options.update(kwargs)
        return make_ace2_unit(**options)

    @staticmethod
    def _status(*slot_states: str) -> Dict[str, Any]:
        """
        :param slot_states: each slot's slot_status
        :return dict: a decoded GET_STATUS result
        """
        return {"status": "ready",
                "slots": [{"index": index, "slot_status": state}
                          for index, state in enumerate(slot_states)]}

    def test_jam_on_active_slot_schedules_handler_once(self):
        unit = self._unit(feed_assist_active=[0])
        printer = unit.printer

        unit._check_stuck(self._status("stuck_error", "ready"))
        unit._check_stuck(self._status("stuck_error", "ready"))

        assert unit._stuck_tripped is True
        # Deferred once for both heartbeats, and nothing paused yet.
        assert len(printer.reactor.pending) == 1
        assert printer.afc.error.AFC_error.calls == []
        printer.reactor.run_callbacks()
        assert unit._ace.commands == [("get_status", {}), ("stop_feed_assist", {"index": 0})]
        assert printer.afc.error.AFC_error.calls == [
            ((ace2_jam_message("lane1", self.STUCK),), {"pause": True})]
        assert printer.logger.messages == []

    @pytest.mark.parametrize("state,what", [
        ("stuck_error", STUCK),
        ("tangled_error", "tangled spool"),
        ("assist_error", "feed-assist slip (encoder fell behind the motor)"),
        ("motor_error", "motor error"),
    ])
    def test_all_jam_states_trip(self, state, what):
        unit = self._unit()

        unit._check_stuck(self._status(state, "ready"))

        assert unit._stuck_tripped is True
        assert len(unit.printer.reactor.pending) == 1
        # The deferred handler is told the lane and the state that tripped.
        unit.printer.reactor.run_callbacks()
        assert unit.printer.afc.error.AFC_error.calls == [
            ((ace2_jam_message("lane1", what),), {"pause": True})]
        assert unit.printer.logger.messages == []

    def test_recovery_rearms_latch(self):
        unit = self._unit()

        unit._check_stuck(self._status("tangled_error"))
        assert unit._stuck_tripped is True
        unit._check_stuck(self._status("ready"))
        assert unit._stuck_tripped is False
        unit._check_stuck(self._status("stuck_error"))

        assert unit._stuck_tripped is True
        assert len(unit.printer.reactor.pending) == 2
        assert unit.printer.logger.messages == []

    @pytest.mark.parametrize("state", [
        "ready", "assisting", "feeding", "feed_error", "rollback_error", "preload_error",
        "unknown"])
    def test_healthy_slot_never_trips(self, state):
        # Feed, rollback and preload errors belong to loading, not to a jam.
        unit = self._unit()
        unit._stuck_tripped = True

        unit._check_stuck(self._status(state))

        # Re-armed for the next jam.
        assert unit._stuck_tripped is False
        assert unit.printer.reactor.pending == []
        assert unit.printer.logger.messages == []

    def test_idle_slot_error_never_trips(self):
        # Only the active lane's slot counts: a stale error on idle lane2
        # cannot pause a healthy print.
        unit = self._unit()
        unit._stuck_tripped = True

        unit._check_stuck(self._status("ready", "stuck_error"))

        assert unit._stuck_tripped is False
        assert unit.printer.reactor.pending == []
        assert unit.printer.logger.messages == []

    def test_detection_disabled_by_config(self):
        # Not printing, so reaching the print-state check would clear the latch.
        unit = self._unit(values={"stuck_spool_detection": False}, in_print=False)
        unit._stuck_tripped = True

        unit._check_stuck(self._status("stuck_error"))

        assert unit._stuck_tripped is True
        assert unit.printer.reactor.pending == []
        assert unit.printer.logger.messages == []

    def test_not_printing_resets_and_skips(self):
        unit = self._unit(in_print=False)
        unit._stuck_tripped = True

        unit._check_stuck(self._status("stuck_error"))

        assert unit._stuck_tripped is False
        assert unit.printer.reactor.pending == []
        assert unit.printer.logger.messages == []

    def test_paused_print_resets_and_skips(self):
        unit = self._unit(paused=True)
        unit._stuck_tripped = True

        unit._check_stuck(self._status("stuck_error"))

        assert unit._stuck_tripped is False
        assert unit.printer.reactor.pending == []
        assert unit.printer.logger.messages == []

    def test_no_active_lane_resets_and_skips(self):
        unit = self._unit(lanes=[LaneSpec("lane1", prep=True), LaneSpec("lane2", prep=True)])
        unit._stuck_tripped = True

        unit._check_stuck(self._status("stuck_error"))

        assert unit._stuck_tripped is False
        assert unit.printer.reactor.pending == []
        assert unit.printer.logger.messages == []

    def test_active_lane_missing_from_slot_map_skips(self):
        unit = self._unit()
        del unit._slot_map["lane1"]
        unit._stuck_tripped = True

        unit._check_stuck(self._status("stuck_error"))

        # Returned without touching the latch: no slot to read.
        assert unit._stuck_tripped is True
        assert unit.printer.reactor.pending == []
        assert unit.printer.logger.messages == []

    def test_malformed_status_is_ignored(self):
        unit = self._unit()
        unit._stuck_tripped = True

        # Each returns without reading a state, so the latch is not re-armed:
        # no slots key, slots not a list (even a tuple of healthy entries), the
        # active slot out of range, and a slot entry that is not a dict.
        for result in ({}, {"slots": "garbage"}, {"slots": ({"slot_status": "ready"},)},
                       {"slots": []}, {"slots": ["not-a-dict"]}):
            unit._check_stuck(result)
            assert unit._stuck_tripped is True

        assert unit.printer.reactor.pending == []
        assert unit.printer.logger.messages == []


class TestAfcACE2HandleEncoderJam:
    class UnreadableLink(FakeAce2Connection):
        """A scripted link whose state cannot be read, as a port torn down mid-call."""

        @property
        def connected(self) -> bool:
            """
            :return bool: never; reading it raises
            """
            error_str = "link state unreadable"
            raise OSError(error_str)

    @staticmethod
    def _unit(**kwargs: Any) -> afcACE2:
        """
        :param kwargs: make_ace2_unit keywords over the defaults
        :return afcACE2: lane1 (slot 0) and lane2 (slot 1), both assisting
        """
        options: Dict[str, Any] = {
            "lanes": [LaneSpec("lane1", prep=True, tool_loaded=True),
                      LaneSpec("lane2", prep=True)],
            "in_print": True, "feed_assist_active": [0, 1]}
        options.update(kwargs)
        return make_ace2_unit(**options)

    def test_stuck_error_stops_assist_and_pauses_via_afc(self):
        unit = self._unit()
        printer = unit.printer
        message = ace2_jam_message(
            "lane1", "stuck spool (encoder saw no movement while feeding)")

        unit._handle_encoder_jam("lane1", 0, "stuck_error")

        # Assist on the jammed slot stops before the pause.
        assert unit._feed_assist_active == {1}
        assert printer.timeline == [("send_command", ("get_status", {})),
                                    ("send_command", ("stop_feed_assist", {"index": 0})),
                                    ("AFC_error", message)]
        assert printer.afc.error.AFC_error.calls == [((message,), {"pause": True})]
        assert printer.gcode.run_script_from_command.calls == []
        assert printer.logger.messages == []

    @pytest.mark.parametrize("state,what", [
        ("tangled_error", "tangled spool"),
        ("assist_error", "feed-assist slip (encoder fell behind the motor)"),
        ("motor_error", "motor error"),
        ("weird_error", "weird_error"),
    ])
    def test_pretty_message_per_state(self, state, what):
        # An unknown state is named as the firmware gave it.
        unit = self._unit()

        unit._handle_encoder_jam("lane2", 1, state)

        assert unit._feed_assist_active == {0}
        assert unit.printer.afc.error.AFC_error.calls == [
            ((ace2_jam_message("lane2", what),), {"pause": True})]
        assert unit.printer.logger.messages == []

    def test_stop_assist_exception_is_swallowed(self):
        printer = AcePrinter()
        link = make_fake_ace_connection(ace2=True, printer=printer, cls=self.UnreadableLink)
        unit = self._unit(printer=printer, connection=link)

        unit._handle_encoder_jam("lane1", 0, "stuck_error")

        # Stopping assist raised, and the pause still went through AFC.
        assert unit._feed_assist_active == {0, 1}
        assert link.commands == []
        assert printer.afc.error.AFC_error.calls == [
            ((ace2_jam_message("lane1", "stuck spool (encoder saw no movement while "
                                        "feeding)"),), {"pause": True})]
        assert printer.gcode.run_script_from_command.calls == []
        assert printer.logger.messages == []

    def test_afc_error_failure_falls_back_to_gcode_pause(self):
        unit = self._unit()
        printer = unit.printer
        printer.afc.error.AFC_error.raises = RuntimeError("afc down")
        message = ace2_jam_message(
            "lane1", "stuck spool (encoder saw no movement while feeding)")

        unit._handle_encoder_jam("lane1", 0, "stuck_error")

        assert printer.afc.error.AFC_error.calls == [((message,), {"pause": True})]
        assert printer.logger.messages == [("error", message)]
        assert printer.gcode.run_script_from_command.calls == [(("PAUSE",), {})]
        assert unit._feed_assist_active == {1}


class TestAfcACE2MakeConnection:
    def test_builds_ace2_connection(self):
        unit = make_ace2_unit(values={"ace_index": 2, "ace_uid": "11, 22, 33"})
        printer = unit.printer

        conn = unit._make_connection(printer.reactor, "/dev/ttyACE2", printer.logger, 230400)

        assert type(conn) is ACE2Connection
        assert conn._serial_port == "/dev/ttyACE2"
        assert conn._baud_rate == 230400
        # The unit's autodetect binding travels with the connection.
        assert conn._ace_index == 2
        assert conn._ace_uid == (11, 22, 33)
        assert conn._reactor is printer.reactor
        assert conn._logger is printer.logger
        assert conn.connected is False
        assert printer.logger.messages == []


class TestAfcACE2ReaderSiblingSlot:
    def test_pairs_within_range(self):
        # Two readers: slots 0 and 1 share one, slots 2 and 3 the other.
        unit = make_ace2_unit()

        assert [unit._reader_sibling_slot(slot) for slot in range(4)] == [1, 0, 3, 2]

    def test_out_of_range_sibling_returns_none(self):
        unit = make_ace2_unit()

        assert unit._reader_sibling_slot(100) is None
        assert unit._reader_sibling_slot(4) is None
        assert unit._reader_sibling_slot(-1) is None


class TestAce2EncoderScale:
    def test_encoder_scale_constant(self):
        # The encoder reads 1.2342 per commanded mm, so at feed_error_length 185
        # it reaches 228.3: a feed_check_length of 228 fits, 229 never could.
        unit = make_ace2_unit(values={"feed_check_length": 228, "feed_error_length": 185})
        assert (unit.feed_check_length, unit.feed_error_length) == (228, 185)

        with pytest.raises(configparser.Error) as excinfo:
            make_ace2_unit(values={"feed_check_length": 229, "feed_error_length": 185})

        assert str(excinfo.value) == (
            "[AFC_ACE2 Ace2_1] feed_check_length (229) must be < feed_error_length * 1.2342 "
            "= 228, otherwise the encoder can never reach it and every feed raises "
            "FEED_ERROR. Lower feed_check_length to widen tolerance (tolerance_mm ~= "
            "feed_error_length - feed_check_length / 1.2342).")


class TestAfcACE2UsesFirmwareRfid:
    def test_v2_uses_firmware_rfid_false(self):
        # The ACE 2 reads tags host-side, so even on a live link with tag data
        # to give, the startup inventory sends no firmware get_filament_info.
        unit = make_ace2_unit(lanes=["lane1", "lane2"])
        unit._ace.set_reply("get_filament_info", {"index": 0, "sku": "PLA-1", "type": "PLA"})

        unit._sync_inventory()

        assert unit._ace.connected is True
        assert unit._ace.commands == []
        assert unit._slot_inventory == [{}, {}, {}, {}]
        assert unit.printer.logger.messages == []
