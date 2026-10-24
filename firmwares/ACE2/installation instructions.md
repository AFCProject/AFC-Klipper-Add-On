# Flashing `AFC_ACE2PRO.bin` to an ACE 2 Pro

This is the AFC build of the ACE 2 Pro application firmware,
**AFCACE2PRO**, flashed as 1.0.0. It is Anycubic's stock app with two
additions:

- **AFC's speed patch** raises the feed/unwind cap from 100 to 140 mm/s.
- **AFC's register passthrough** (commands `0x50` read, `0x51` write, `0x52`
  reader power) lets AFC drive the tag reader itself, so `[AFC_ACE2_rfid]`
  reads, decodes and writes every tag format AFC knows on the printer.

The ACE's own tag reading is left as stock. AFC turns its identify off on
each connect, so the unit skips its slow factory autoload and AFC stages
each insert itself.

The passthrough is appended after the stock code, before the 8-byte IAP
trailer, which has to stay last. Against stock the image changes the three
speed bytes, the init hook at `0x0801401E` and the version field, and adds
the 396-byte passthrough at `0x080197A0`: 71,988 B, md5
`5542626deceb8a19a738e335124a3687`. It is built from the stock app (md5
`79fb22e7914bae1dc75ac91b30739c19`) with Sovoron_klipper's
`ace2_rfid/firmware/finish.py`:

```bash
python3 finish.py ace2_app.bin AFC_ACE2PRO.bin --version AFCACE2PRO --passthrough
```

The ACE 2 protocol, this updater and the firmware map the work builds on are
hakimio's: <https://gist.github.com/hakimio/4916ff69add458fdc51aeea76f21efb9>.

Flashing is done over the ACE's own serial link with
`Firmwares/ACE2/ace2-ota-update.py`, which drives the same IAP sequence the
Kobra S1 uses. **No printer, no SD card, no disassembly.**


---

## Before you start

| | |
|---|---|
| Cable | USB direct to the **ACE 2 Pro**, not to the printer |
| Dependency | `pip install pyserial` |
| Port |  `/dev/ttyCH343USB0`-style on Linux |
| Link speed | 230400 baud; the script sets this itself |
| Duration | roughly a minute; the image goes out in 64-byte chunks |

**Have the stock firmware to hand before you begin.** If a flash is
interrupted the unit stays in its IAP loader and can be re-flashed, but you
want the fallback image already downloaded rather than going looking for it
mid-recovery.

---

## Flash it

**1. Dry run first.** This talks to the unit, reads its current version and
parses the image, then exits without writing anything. If this does not work,
nothing else will:

```bash
python3 ace2-ota-update.py /dev/ttyCH343USB0 AFC_ACE2PRO.bin \
        --version 1.0.0 --dry-run
```

**2. Flash.**

```bash
python3 ace2-ota-update.py /dev/ttyCH343USB0 AFC_ACE2PRO.bin \
        --version 1.0.0
```

It prints what it is about to do and waits for confirmation:

```
  About to flash: V1.1.31  ->  1.0.0
  Image: 71988 bytes  CRC16=0x03D2
  Proceed? [y/N]
```

Answer `y`. Then leave it alone until it prints `[done] Flash complete`.

**3. Power cycle the ACE.** This is not optional and not a suggestion: the
unit commits the image but keeps running the old firmware until it is
physically power cycled. It does not reboot itself. Pull the power, wait a few
seconds, plug it back in.

---

## Checking what the unit runs

This build reports **`AFCACE2PRO`** through GET_INFO (11 characters at
most). After the power cycle, AFC's log shows it in the `ACE device info`
line, and the updater prints it when it reconnects.

| Reported | Firmware |
|---|---|
| `AFCACE2PRO` | this build |
| `V1.1.31` | stock |

The updater skips the flash when `--version` already matches what the unit
reports. If you are re-flashing the same build, add `--force`.

---

## Other options

| flag | what it does |
|---|---|
| `--dry-run` | connect, read version, parse image, exit without writing |
| `--force` | flash even when the reported version already matches |
| `--verbose` | per-chunk progress; use it when a flash fails partway |
| `--md5 HASH` | verify an archive's checksum before extracting |
| `--swu-password PASS` | password for an encrypted `.swu` |
| `--chunk-size N` | leave alone; 64 is what the IAP expects |

The script also takes a Kobra S1 `.swu` package directly and extracts the ACE
binary itself, useful for going *back* to a stock image, which is the most
likely reason you would want it.

---

## If it goes wrong

**Nothing on the port.** Check you are on the ACE's own USB socket and not the
printer's. On Linux, `ls /dev/ttyCH343USB*` or `dmesg | tail` after plugging
in; the ACE uses a CH343 USB-serial bridge, which needs a driver on some
systems.

**Flash stops partway.** Power cycle and re-run with `--force --verbose`. The
unit stays in its IAP loader, so an interrupted flash is recoverable; that is
the whole point of the IAP design.

**Flashed, power cycled, no RFID.** The firmware is only one half. AFC needs
`[AFC_ACE2_rfid]` configured for the unit; see the RFID section of
`templates/AFC_ACE2_1.cfg` and `extras/AFC_ACE2_rfid.py`.

**Back to stock.** Flash the stock image the same way, with `--force`.
Keep one archived; this is the reason to.
