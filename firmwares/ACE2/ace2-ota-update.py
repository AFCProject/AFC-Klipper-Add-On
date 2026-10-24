#!/usr/bin/env python3
"""
Anycubic ACE 2 Pro — OTA Firmware Updater

Implements the IAP (In-Application Programming) sequence reverse-engineered from the
Kobra S1 gklib binary (v2.7.0.9):

  1. IAP_UPGRADE (cmd 2)        — announce: total size + CRC-16/Kermit + version string
  2. IAP_FIRMWARE (cmd 3)       — send firmware in 64-byte chunks, each with its flash address
  3. IAP_UPGRADE_FINISH (cmd 4) — commit the image (the ACE runs the new
     firmware only after a physical power cycle; it does not self-reboot)

Flash base address for ACE 2: 0x08024000 (IAP task, confirmed from ACE2 MCU disassembly)
Chunk size: 64 bytes (confirmed from gklib: `total = ceil(size / 64)`)

About the firmware file
-----------------------
gklib reads the firmware binary DIRECTLY with os.OpenFile + io.ReadAll — there is no
ota_local_info header parsing inside gklib. The file is expected to be a raw ARM image
for the OTA staging area (0x08024000). CRC-16/Kermit and size are computed over the
entire binary, and the version string is supplied via --version.

The .swu file from an Anycubic update package is a CPIO archive (SVR4 "newc" format).
This script auto-detects CPIO magic and extracts the ACE firmware binary from within.
gkapi (the Kobra S1 API layer) performs this extraction before handing the path to gklib.

Obtaining ACE 2 firmware:
  Option A — from a Kobra S1 OTA package (.swu file):
    Pass the .swu directly; the script extracts the ACE binary automatically.
    The relevant file inside the archive has "ace" or "filament_hub" in its name.

  Option B — capture from the printer over the network:
    Intercept HTTP(S) traffic while the Kobra S1 downloads an auto-update.
    The ACE firmware URL will appear in the printer's requests.

  Option C — SSH into the printer:
    Look in the OTA staging directory for the file gklib reads before flashing
    (path set in the printer config as fw_update_path for the filament hub).

Dependencies: pip install pyserial

Usage examples:
  # Flash a raw .bin binary:
  python ace2-ota-update.py COM3 ACE2_V1.1.31.bin --version 1.1.31

  # Extract from .swu ZIP archive and flash (with MD5 and password):
  python ace2-ota-update.py COM3 update.swu --version 1.1.31 --md5 f7968b51... --swu-password "U2FsdGVk..."

  # Dry-run: query version and parse firmware, then exit without flashing:
  python ace2-ota-update.py COM3 firmware.bin --version 1.1.31 --dry-run

  # Force flash even if version already matches:
  python ace2-ota-update.py COM3 firmware.bin --version 1.1.31 --force

  # Linux example:
  python ace2-ota-update.py /dev/ttyCH343USB0 firmware.bin --version 1.1.31
"""

import serial
import struct
import time
import sys
import os
import argparse
import hashlib
import zipfile
import tarfile
import io

# ═══════════════════════════════════════════════════════════════════
# PROTOCOL CONSTANTS
# ═══════════════════════════════════════════════════════════════════

BAUD              = 230400
PREAMBLE          = b'\xff\xaa'
END_MARKER        = 0xFE
FLAG_REQUEST      = 0x00
FLAG_RESPONSE     = 0x80

CMD_DISCOVER      = 0
CMD_ASSIGN_ID     = 1
CMD_IAP_UPGRADE   = 2
CMD_IAP_FIRMWARE  = 3
CMD_IAP_FINISH    = 4
CMD_IAP_VERSION   = 5
CMD_GET_INFO      = 7

# ACE 2 OTA staging area — confirmed from IAP task disasm (0x8013FB8): MOVT #0x802 MOVW #0x4000
# CMD3 accepts first chunk only when address == 0x08024000; CRC verified over this region.
ACE2_FLASH_BASE   = 0x08024000

# Max protobuf payload per UART frame — enforced by BuildRequest in gklib (0x60e19c)
MAX_FRAME_PAYLOAD = 100

# Firmware data bytes per chunk.
# Confirmed from gklib disasm: total_chunks = ceil(file_size / 64), each chunk is 64 bytes.
# FirmwareRequest protobuf overhead: ~8 bytes → total payload ~72 bytes, well within 100.
CHUNK_SIZE        = 64

# Timeouts (seconds) matching gklib values
T_START   = 2.0   # OtaStart timeout
T_CHUNK   = 2.0   # OtaSendChunk timeout
T_FINISH  = 5.0   # OtaFinish timeout

# ═══════════════════════════════════════════════════════════════════
# CRC
# ═══════════════════════════════════════════════════════════════════

def crc16_kermit(data: bytes) -> int:
    """CRC-16/KERMIT — poly 0x8408, init 0xFFFF."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF

# ═══════════════════════════════════════════════════════════════════
# MINIMAL PROTOBUF ENCODER
# ═══════════════════════════════════════════════════════════════════

def _varint(v: int) -> bytes:
    r = bytearray()
    while v > 0x7F:
        r.append((v & 0x7F) | 0x80)
        v >>= 7
    r.append(v & 0x7F)
    return bytes(r)

def _field_uint32(field: int, value: int) -> bytes:
    return _varint((field << 3) | 0) + _varint(value)

def _field_bytes(field: int, data: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(data)) + data

def _field_string(field: int, text: str) -> bytes:
    return _field_bytes(field, text.encode())

def encode_upgrade_request(size: int, image_crc: int, version: str) -> bytes:
    """UpgradeRequest { size=1, crc=2, version=3 } — crc is CRC-16/Kermit, stored as uint32."""
    return _field_uint32(1, size) + _field_uint32(2, image_crc) + _field_string(3, version)

def encode_firmware_request(address: int, chunk: bytes) -> bytes:
    """FirmwareRequest { address=1, firmware=2 }"""
    return _field_uint32(1, address) + _field_bytes(2, chunk)

# ═══════════════════════════════════════════════════════════════════
# PACKET FRAMING
# ═══════════════════════════════════════════════════════════════════

def build_packet(cmd: int, payload: bytes, seq: int) -> bytes:
    plen = len(payload)
    if plen > MAX_FRAME_PAYLOAD:
        raise ValueError(f"Payload {plen} bytes exceeds frame limit {MAX_FRAME_PAYLOAD}")
    inner = bytearray([FLAG_REQUEST, seq & 0xFF, (seq >> 8) & 0xFF, cmd & 0xFF, plen & 0xFF])
    inner.extend(payload)
    crc = crc16_kermit(bytes(inner))
    return bytes(PREAMBLE + inner + bytes([crc & 0xFF, (crc >> 8) & 0xFF, END_MARKER]))

def parse_response(buf: bytearray):
    """Returns (packet_dict, bytes_consumed) or (None, 0/advance)."""
    while len(buf) >= 2:
        idx = buf.find(PREAMBLE)
        if idx < 0:
            return None, max(0, len(buf) - 1)
        if idx > 0:
            return None, idx
        if len(buf) < 10:
            return None, 0
        for end in range(9, min(len(buf), 270)):
            if buf[end] != END_MARKER:
                continue
            flags, seq = buf[2], buf[3] | (buf[4] << 8)
            cmd, plen = buf[5], buf[6]
            exp = 7 + plen + 2
            if end != exp:
                continue
            inner = bytes(buf[2:7 + plen])
            crc_recv = buf[7 + plen] | (buf[8 + plen] << 8)
            if crc_recv != crc16_kermit(inner):
                return None, end + 1
            return {
                "cmd": cmd,
                "is_resp": bool(flags & 0x80),
                "seq": seq,
                "payload": bytes(buf[7:7 + plen]),
            }, end + 1
        return None, 2 if len(buf) > 270 else 0
    return None, 0

def decode_varint(data: bytes, pos: int):
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    return result, pos

def pb_decode(data: bytes) -> dict:
    fields, pos = {}, 0
    while pos < len(data):
        tag, pos = decode_varint(data, pos)
        fnum, wtype = tag >> 3, tag & 7
        if wtype == 0:
            val, pos = decode_varint(data, pos)
        elif wtype == 2:
            ln, pos = decode_varint(data, pos)
            val = data[pos:pos + ln]; pos += ln
        elif wtype == 5:
            val = struct.unpack_from('<f', data, pos)[0]; pos += 4
        elif wtype == 1:
            val = struct.unpack_from('<d', data, pos)[0]; pos += 8
        else:
            break
        fields.setdefault(fnum, []).append((wtype, val))
    return fields

def get_field(fields: dict, num: int, default=0):
    return fields.get(num, [(0, default)])[0][1]

# ═══════════════════════════════════════════════════════════════════
# TRANSPORT
# ═══════════════════════════════════════════════════════════════════

class ACE2Transport:
    def __init__(self, port: str):
        self.port = port
        self.ser = serial.Serial(port, BAUD, timeout=0.1)
        self._seq = 0

    def close(self):
        self.ser.close()

    def reopen(self, tries: int = 12) -> bool:
        """Close and reopen the serial port. Needed after an OTA reboot: the ACE
        resets its UART (and the USB bridge may briefly re-enumerate), so the old
        handle goes stale. Retries while the device re-appears, then flushes."""
        try:
            self.ser.close()
        except Exception:
            pass
        for _ in range(tries):
            try:
                self.ser = serial.Serial(self.port, BAUD, timeout=0.1)
                try:
                    self.ser.reset_input_buffer()
                    self.ser.reset_output_buffer()
                except Exception:
                    pass
                return True
            except Exception:
                time.sleep(0.5)
        return False

    def _next_seq(self) -> int:
        self._seq = (self._seq % 0xFFFF) + 1
        return self._seq

    def send_recv(self, cmd: int, payload: bytes, timeout: float) -> list:
        seq = self._next_seq()
        pkt = build_packet(cmd, payload, seq)
        self.ser.reset_input_buffer()
        self.ser.write(pkt)
        self.ser.flush()

        buf = bytearray()
        results = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ser.in_waiting:
                buf.extend(self.ser.read(self.ser.in_waiting))
                while len(buf) > 4:
                    p, n = parse_response(buf)
                    if n > 0:
                        buf = buf[n:]
                    else:
                        break
                    # The IAP bootloader answers with flags=1 (no 0x80 response
                    # bit), so filtering on is_resp made the tool deaf to it during
                    # recovery. Match on the command and accept any reply that is
                    # not an echo of our request. Fix from ACE2-Open (Simon-CR,
                    # tools/ota_updater.patch).
                    if p and p["cmd"] == cmd and (p["is_resp"] or p["payload"] != payload):
                        results.append(p)
                # The ACK for this cmd has arrived — the device is ready for the
                # next request, so stop waiting instead of burning the rest of the
                # timeout. (Without this, every send_recv blocked the FULL timeout;
                # at T_CHUNK=2s over ~1160 chunks that alone was ~38 min of dead
                # air.) The per-chunk ACK is the flow-control gate, so returning as
                # soon as it lands is both correct and safe.
                if results:
                    break
            else:
                time.sleep(0.005)
        return results

# ═══════════════════════════════════════════════════════════════════
# VERSION QUERY
# ═══════════════════════════════════════════════════════════════════

def get_ace_version(transport: ACE2Transport) -> tuple[str, str] | None:
    """Returns (version, boot_version) or None."""
    results = transport.send_recv(CMD_GET_INFO, b'', timeout=2.0)
    for r in results:
        f = pb_decode(r["payload"])
        if 1 in f:
            version = get_field(f, 1, b'').decode(errors='replace')
            boot    = get_field(f, 2, b'').decode(errors='replace')
            return version, boot
    return None

# ═══════════════════════════════════════════════════════════════════
# FIRMWARE FILE PARSING
# ═══════════════════════════════════════════════════════════════════

# Keywords that identify the ACE/filament-hub firmware file inside archives
_ACE_KEYWORDS = ('ace', 'filament_hub', 'filament-hub')

# CPIO newc (SVR4) constants — older/alternative swu format
_CPIO_MAGIC   = (b'070701', b'070702')
_CPIO_HDR_LEN = 110


def _find_in_tar(tar_bytes: bytes, mode: str) -> tuple[bytes, str] | None:
    """Extract the first ACE firmware .bin from a tar archive."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode=mode) as tf:
        for member in tf.getmembers():
            if member.isfile() and any(kw in member.name.lower() for kw in _ACE_KEYWORDS):
                f = tf.extractfile(member)
                if f:
                    return f.read(), member.name
    return None


def _extract_from_zip_swu(data: bytes, password: str | None) -> tuple[bytes, str] | None:
    """Extract ACE firmware from a ZIP-format .swu (Anycubic KS1/S1 packaging).

    Archive layout (confirmed from gkapi disassembly at 0x482360):
      swu.zip  (AES/ZipCrypto encrypted, password from cloud OTA notification)
      └── update_swu/
          └── setup.tar.gz   (or setup.tar)
              └── ACE2_*.bin
    """
    pwd_bytes = password.encode() if password else None
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # Locate the inner tarball (gkapi tries setup.tar first, then setup.tar.gz)
        tar_name = next(
            (n for n in zf.namelist() if n.endswith(('setup.tar.gz', 'setup.tar'))),
            None,
        )
        if tar_name is None:
            return None
        tar_bytes = zf.read(tar_name, pwd=pwd_bytes)
        mode = 'r:gz' if tar_name.endswith('.gz') else 'r:'
        return _find_in_tar(tar_bytes, mode)


def _extract_from_cpio_swu(data: bytes) -> tuple[bytes, str] | None:
    """Walk a CPIO newc archive and return (content, filename) for the ACE firmware entry."""
    pos = 0
    while pos + _CPIO_HDR_LEN <= len(data):
        hdr = data[pos:pos + _CPIO_HDR_LEN]
        if hdr[:6] not in _CPIO_MAGIC:
            return None
        filesize = int(hdr[54:62], 16)
        namesize = int(hdr[94:102], 16)
        pos += _CPIO_HDR_LEN
        name = data[pos:pos + namesize - 1].decode(errors='replace')
        pos = (pos + namesize + 3) & ~3  # align to 4-byte boundary
        if name == 'TRAILER!!!':
            break
        file_data = data[pos:pos + filesize]
        pos = (pos + filesize + 3) & ~3
        if any(kw in name.lower() for kw in _ACE_KEYWORDS):
            return file_data, name
    return None


class FirmwareImage:
    def __init__(self, data: bytes, version: str, image_crc: int):
        self.data      = data
        self.version   = version
        self.image_crc = image_crc  # CRC-16/Kermit — verified by the MCU after flashing


def load_firmware(path: str, version: str,
                  expected_md5: str | None = None,
                  swu_password: str | None = None) -> FirmwareImage:
    """
    Load the ACE 2 firmware image.

    gklib opens the final ARM binary with os.OpenFile + io.ReadAll, computes CRC-16/Kermit
    over the full file, and sends it in 64-byte chunks. gkapi handles archive extraction
    before gklib is involved.

    Supported input formats (auto-detected by magic bytes):
      • Raw ARM binary (.bin)        — used directly
      • ZIP archive (.swu, KS1/S1)   — unzip with optional password → setup.tar.gz → .bin
      • CPIO newc archive (.swu)      — walk archive → .bin  (older format, fallback)

    MD5 is checked over the raw input file before any extraction (matching gkapi behaviour).
    """
    with open(path, 'rb') as f:
        raw_bytes = f.read()

    # MD5 check over the raw file — same point gkapi validates before calling gklib
    if expected_md5 is not None:
        actual_md5 = hashlib.md5(raw_bytes).hexdigest()
        if actual_md5.lower() != expected_md5.lower():
            raise ValueError(
                f"MD5 mismatch!\n"
                f"  expected: {expected_md5.lower()}\n"
                f"  actual:   {actual_md5}"
            )
        print(f"[firmware] MD5 OK: {actual_md5}")

    image_data = raw_bytes

    if raw_bytes[:2] == b'PK':
        # ZIP-format .swu (Anycubic KS1/S1): ZIP → setup.tar.gz → .bin
        result = _extract_from_zip_swu(raw_bytes, swu_password)
        if result is None:
            raise ValueError(
                "File is a ZIP archive but no ACE firmware entry found inside setup.tar.gz. "
                "Check that --swu-password is correct and the archive contains an ACE binary."
            )
        image_data, source = result
        print(f"[firmware] Extracted from ZIP .swu: '{source}'  ({len(image_data)} bytes)")

    elif raw_bytes[:6] in _CPIO_MAGIC:
        # CPIO-format .swu (fallback / older format)
        result = _extract_from_cpio_swu(raw_bytes)
        if result is None:
            raise ValueError(
                "File is a CPIO archive but no ACE firmware entry found. "
                "Expected a file with 'ace' or 'filament_hub' in its name."
            )
        image_data, source = result
        print(f"[firmware] Extracted from CPIO .swu: '{source}'  ({len(image_data)} bytes)")

    image_crc = crc16_kermit(image_data)
    print(f"[firmware] {len(image_data)} bytes  CRC16/Kermit=0x{image_crc:04X}  version={version}")
    return FirmwareImage(image_data, version, image_crc)

# ═══════════════════════════════════════════════════════════════════
# IAP FLASH SEQUENCE
# ═══════════════════════════════════════════════════════════════════

def iap_upgrade(transport: ACE2Transport, fw: FirmwareImage, verbose: bool) -> bool:
    total   = len(fw.data)
    n_chunks = (total + CHUNK_SIZE - 1) // CHUNK_SIZE

    # ── Step 1: IAP_UPGRADE ─────────────────────────────────────────
    print(f"\n[step 1/3] IAP_UPGRADE  size={total}  crc16=0x{fw.image_crc:04X}  version={fw.version}")
    payload = encode_upgrade_request(total, fw.image_crc, fw.version)
    if verbose:
        print(f"  payload: {payload.hex()}")
    results = transport.send_recv(CMD_IAP_UPGRADE, payload, timeout=T_START)
    if not results:
        print("[ERROR] No response to IAP_UPGRADE — is the ACE connected and powered?")
        return False
    resp = results[0]
    code = get_field(pb_decode(resp["payload"]), 1, 0)
    if code != 0:
        print(f"[ERROR] IAP_UPGRADE rejected: code={code}")
        return False
    print("  ACE accepted upgrade announcement.")

    # ── Step 2: IAP_FIRMWARE chunks ─────────────────────────────────
    print(f"\n[step 2/3] IAP_FIRMWARE  {n_chunks} chunks × {CHUNK_SIZE} bytes  (base=0x{ACE2_FLASH_BASE:08X})")
    for i in range(n_chunks):
        offset = i * CHUNK_SIZE
        chunk  = fw.data[offset:offset + CHUNK_SIZE]
        addr   = ACE2_FLASH_BASE + offset
        payload = encode_firmware_request(addr, chunk)

        if len(payload) > MAX_FRAME_PAYLOAD:
            print(f"[ERROR] Chunk {i}: protobuf payload {len(payload)} exceeds {MAX_FRAME_PAYLOAD} bytes. Reduce CHUNK_SIZE.")
            return False

        results = transport.send_recv(CMD_IAP_FIRMWARE, payload, timeout=T_CHUNK)
        if not results:
            print(f"\n[ERROR] No response for chunk {i} (addr=0x{addr:08X})")
            return False
        code = get_field(pb_decode(results[0]["payload"]), 1, 0)
        if code != 0:
            print(f"\n[ERROR] Chunk {i} rejected at addr=0x{addr:08X}: code={code}")
            return False

        # Progress bar
        pct = (i + 1) / n_chunks * 100
        bar = int(pct / 2)
        print(f"\r  [{('█' * bar):<50s}] {pct:5.1f}%  chunk {i+1}/{n_chunks}", end='', flush=True)

    print()  # newline after progress bar
    print("  All chunks transferred.")

    # ── Step 3: IAP_UPGRADE_FINISH ───────────────────────────────────
    print(f"\n[step 3/3] IAP_UPGRADE_FINISH")
    results = transport.send_recv(CMD_IAP_FINISH, b'', timeout=T_FINISH)
    if not results:
        # No response here is normal — the ACE commits and stops answering until
        # it is power-cycled.
        print("  No response (ACE committed the image — this is normal).")
    else:
        code = get_field(pb_decode(results[0]["payload"]), 1, 0)
        print(f"  Response code: {code}")
    return True

# ═══════════════════════════════════════════════════════════════════
# POST-UPDATE VERSION VERIFY
# ═══════════════════════════════════════════════════════════════════

def _norm_ver(v: str) -> str:
    """Strip leading 'V'/'v' so '1.1.31' and 'V1.1.31' compare equal."""
    return v.lstrip('Vv')


def _poll_version(transport: ACE2Transport, expected: str, seconds: float) -> bool:
    """Reopen the port and poll GET_INFO for up to `seconds`. Success = the ACE
    answers (it's running firmware again); the reported version need not equal
    `expected` because the tool doesn't rewrite the image's baked version string."""
    transport.reopen()
    deadline = time.time() + seconds
    while time.time() < deadline:
        result = get_ace_version(transport)
        if result:
            ver, boot = result
            print(f"  ACE reports: version={ver}  boot_version={boot}")
            if _norm_ver(ver) == _norm_ver(expected):
                print("  Version matches — update confirmed.")
            else:
                print(f"  ACE is up and running (reported {ver}; the flashed image "
                      f"keeps its baked version, so it need not equal "
                      f"--version {expected}).")
            return True
        time.sleep(2.0)
        transport.reopen()
    return False


def _port_present(port: str) -> bool:
    """True if the serial device exists AND can be opened. The ACE's USB-serial
    bridge is powered by the ACE, so the device node vanishes when the ACE is off
    and reappears when it powers on."""
    if not os.path.exists(port):
        return False
    try:
        serial.Serial(port, BAUD, timeout=0.1).close()
        return True
    except Exception:
        return False


def _wait_for(port: str, present: bool, timeout: float) -> bool:
    """Wait up to `timeout`s for the port to reach the given presence state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_present(port) == present:
            return True
        time.sleep(0.3)
    return False


def wait_for_version(transport: ACE2Transport, expected: str) -> bool:
    """Confirm the new firmware runs by watching for a real power cycle.

    The ACE commits the image on IAP_FINISH but does NOT self-reboot — and it
    keeps answering with the OLD firmware until powered off, so a version query
    right after the flash is meaningless (it reports the old version and looks
    'up'). Instead, release the port and watch the device node: it disappears
    when the ACE is switched OFF and reappears when it comes back ON. Only then
    do we query the freshly-booted firmware.
    """
    port = transport.port
    try:
        transport.close()                # free the handle so the node can drop
    except Exception:
        pass

    print("\n[verify] The ACE runs the new firmware only after a power cycle.")
    print("  >>> Switch the ACE2 OFF now (wait for the prompt before turning it on). <<<")
    if not _wait_for(port, present=False, timeout=120.0):
        print("  Never saw the ACE power off (still on the USB bus). The flash is")
        print("  committed — power-cycle the ACE, then restart Klipper.")
        return True                      # flash succeeded; can't auto-confirm
    print("  ACE powered OFF detected.")
    print("  >>> Now switch the ACE2 back ON. <<<")
    if not _wait_for(port, present=True, timeout=120.0):
        print("[WARNING] ACE serial port did not reappear. Check power/cabling.")
        return False
    print("  ACE powered ON detected; waiting for it to boot...")
    time.sleep(2.0)                      # let udev settle + the app come up

    if _poll_version(transport, expected, seconds=20.0):
        return True
    print("[WARNING] ACE is back on the bus but not answering yet. Check manually.")
    return False

# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    global CHUNK_SIZE

    parser = argparse.ArgumentParser(
        description="ACE 2 Pro OTA firmware updater",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("port",     help="Serial port (e.g. COM3 or /dev/ttyCH343USB0)")
    parser.add_argument("firmware", help="Path to ACE 2 firmware (.bin) or update archive (.swu)")
    parser.add_argument("--version", metavar="X.Y.Z", required=True,
                        help="Version string to send in IAP_UPGRADE (e.g. 1.1.31)")
    parser.add_argument("--md5", metavar="HASH",
                        help="Expected MD5 hex digest of the firmware file; aborts if mismatch")
    parser.add_argument("--swu-password", metavar="PASS",
                        help="Password for encrypted ZIP .swu archive (from Anycubic OTA notification)")
    parser.add_argument("--force",  action="store_true",
                        help="Flash even if ACE already reports the target version")
    parser.add_argument("--dry-run", action="store_true",
                        help="Query version and parse firmware, then exit without flashing")
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE, metavar="N",
                        help=f"Firmware bytes per chunk (default: {CHUNK_SIZE}; max safe: ~90)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print raw payload hex for each command")
    args = parser.parse_args()

    CHUNK_SIZE = args.chunk_size

    # ── Load firmware file ──────────────────────────────────────────
    print(f"[firmware] Loading: {args.firmware}")
    fw = load_firmware(args.firmware, args.version,
                       expected_md5=args.md5, swu_password=args.swu_password)
    n_chunks = (len(fw.data) + CHUNK_SIZE - 1) // CHUNK_SIZE
    print(f"[firmware] Will send {n_chunks} chunks of {CHUNK_SIZE} bytes each")

    # Validate chunk payload size
    test_payload = encode_firmware_request(ACE2_FLASH_BASE, b'\x00' * CHUNK_SIZE)
    if len(test_payload) > MAX_FRAME_PAYLOAD:
        print(f"[ERROR] chunk-size={CHUNK_SIZE} produces {len(test_payload)}-byte protobuf payload, "
              f"exceeding {MAX_FRAME_PAYLOAD}-byte frame limit. Use a smaller --chunk-size.")
        sys.exit(1)

    # ── Connect ─────────────────────────────────────────────────────
    print(f"\n[serial] Connecting to {args.port} at {BAUD} baud...")
    transport = ACE2Transport(args.port)
    print("[serial] Connected.")

    try:
        # ── Query current version ───────────────────────────────────
        print("\n[version] Querying current ACE version...")
        current = get_ace_version(transport)
        if current:
            print(f"  Current: version={current[0]}  boot_version={current[1]}")
        else:
            print("  No response. ACE may not be connected or initialised.")

        if args.dry_run:
            print("\n[dry-run] Exiting without flashing.")
            return

        # ── Version check ───────────────────────────────────────────
        if current and _norm_ver(current[0]) == _norm_ver(fw.version) and not args.force:
            print(f"\n[skip] ACE already reports version {current[0]}. Use --force to flash anyway.")
            return

        # ── Confirm ─────────────────────────────────────────────────
        current_ver = current[0] if current else "unknown"
        print(f"\n  About to flash: {current_ver}  →  {fw.version}")
        print(f"  Image: {len(fw.data)} bytes  CRC16=0x{fw.image_crc:04X}")
        try:
            confirm = input("  Proceed? [y/N] ").strip().lower()
        except EOFError:
            confirm = 'y'
        if confirm not in ('y', 'yes'):
            print("Aborted.")
            return

        # ── Flash ───────────────────────────────────────────────────
        print()
        started = time.time()
        ok = iap_upgrade(transport, fw, args.verbose)
        elapsed = time.time() - started

        if ok:
            print(f"\n[done] Flash complete in {elapsed:.1f}s")
            wait_for_version(transport, fw.version)
        else:
            print("\n[FAILED] Firmware update did not complete successfully.")

    except KeyboardInterrupt:
        print("\n[interrupted]")
    finally:
        transport.close()
        print("[serial] Disconnected.")


if __name__ == "__main__":
    main()
