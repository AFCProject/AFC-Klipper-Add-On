# Firmware Attribution

The firmware in this folder is modified from Anycubic's stock Kobra firmware,
[`Kobra_firmware_V2.8.2_EN_1.rar`](https://cdn.shopify.com/s/files/1/0245/5519/2380/files/Kobra_firmware_V2.8.2_EN_1.rar?v=1675913169),
distributed by Anycubic.

## Changes made

- **RFID passthrough.** Three new op commands let AFC drive the ACE's RFID
  reader directly from the printer: reader power, transmit, and receive. This
  is what `[AFC_ACE2_rfid]` uses to read, decode, and write tags itself,
  instead of relying on the ACE's own (slower) factory tag reading.
- **Speed patch.** The top speed cap for feeding and unwinding filament has
  been raised.

See `installation instructions.md` in this folder for the exact build steps,
byte offsets, and checksums for the resulting `AFC_ACE2PRO.bin`.

## `ace2-ota-update.py`

`ace2-ota-update.py` is adapted from hakimio's Anycubic ACE 2 Pro OTA updater
gist: <https://gist.github.com/hakimio/39c71fa7174e699c6470b7c79323b189>. The
gist carries no license; hakimio gave permission in the gist's comments for
the code to be reused without attribution.

Changes made here:

- Dropped the deliberate 15ms pause after every chunk (~17s over a full
  flash) to speed up flashing.
- Fixed `IAP_UPGRADE_FINISH` to report failure on a nonzero response code
  instead of always reporting success.
- Rewrote `wait_for_version` to watch for the ACE's required physical power
  cycle (it does not self-reboot after the image is committed) instead of
  just polling `GET_INFO`, which was checking the wrong thing.
- Changed an `EOFError` on the confirm prompt to abort by default instead of
  auto-confirming, with a new `--yes` flag to skip the prompt intentionally.
- One bootloader reply-matching fix is separately credited inline in the
  file to ACE2-Open (Simon-CR, `tools/ota_updater.patch`).
