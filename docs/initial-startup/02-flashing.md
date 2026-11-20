=== "BoxTurtle"

    ## Flashing the AFC-Lite board

    The AFC-Lite MCU can be connected with either CAN bus or USB.

    If you choose to use CAN bus,
    follow [this guide from Esoterical](https://canbus.esoterical.online/toolhead_flashing/common_hardware/AFC-Lite/README.html).
    This site also has a wealth of information on how to configure CAN bus if this is your first time using it.

    If you choose to use USB, you will still need to run a 24 V+/V- cable to the AFC-Lite CAN bus port, just leave the CAN
    High/Low pins unpopulated. To flash the AFC-Lite for use with USB for data,
    follow [this guide from Esoterical](https://usb.esoterical.online/hardware_config/STM32/AFC_Lite.html).

    Further details regarding the AFC-Lite can be
    found [in the AFC-Lite manual](https://github.com/xbst/AFC-Lite/blob/master/Docs/AFC-Lite_Manual.pdf).

    After flashing and setting up connections/configurations appropriately, you should be able to either obtain the CAN bus
    UUID (if using CAN) or the device serial path (e.g., `/dev/serial/by-id/...`) (if using USB) for the AFC-Lite MCU.
    Please ensure you have these values before you proceed, as they will be required.

    --8<-- "includes/toolhead_pins.md"

=== "Other Systems"

    Please follow you MCU guide for the board that you are using in your system.

    --8<-- "includes/toolhead_pins.md"

=== "Snapmaker U1"
    Skip for Snapmaker since there is nothing to flash, installation instructions are on [Plugin Install](03-install-plugin.md) page

=== "ACE Pro 1/2"

    ACE Pro 1 does not need any firmware flashed for AFC to work.

    ## ACE Pro 2 Custom Firmware (Optional)

    AFC provides a custom firmware build for the ACE Pro 2 that:

    - Raises the feed/unwind speed cap from 100 mm/s to 140 mm/s, letting the ACE move filament faster.
    - Adds a register passthrough that lets AFC drive the ACE's RFID tag reader directly instead of relying on the
      unit's own (slower) factory tag reading/autoload.

    This is optional; if you don't want faster filament movement, you can skip this step.

    !!! note
        The RFID passthrough is not yet used by AFC. Support for reading/writing tags through it is planned for a
        future AFC release.

    The firmware file (`AFC_ACE2PRO.bin`) and the flashing script (`ace2-ota-update.py`) used below are both included
    in the `firmwares/ACE2` folder of the AFC-Klipper-Add-On repository, so this can be done directly from your
    printer's SBC once AFC-Klipper-Add-On has been cloned onto it. We suggest doing this after first cloning and
    installing AFC (see [Install the AFC Klipper Add-On](03-install-plugin.md)), rather than on a separate machine.

    ### Before you start

    | | |
    |---|---|
    | Cable | USB cable connected directly to the **ACE 2 Pro**, not to the printer |
    | Dependency | `~/klippy-env/bin/pip install pyserial` |
    | Port | found via `/dev/serial/by-id/...` (see below) |

    To find the port, run `ls -al /dev/serial/by-id/*` with the ACE 2 Pro connected. It will look something like
    `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B61016332-if00`. Use this `by-id` path rather than a
    `/dev/ttyUSBx`/`/dev/ttyCH343USBx`-style name, since those can change between reboots.

    !!! warning
        Have the stock firmware on hand before you begin. If a flash is interrupted, the unit stays in its
        bootloader and can be re-flashed, but you want the fallback image already downloaded rather than
        looking for it mid-recovery.

    !!! warning
        AFC keeps the ACE 2 Pro's serial port open while Klipper is running, which conflicts with the flashing
        script's own access to that port. Stop Klipper before the dry run and flash, and don't start it again
        until after you've power cycled the ACE.

    ### Flash it

    1. Stop Klipper so it releases the ACE 2 Pro's serial port:
        ```bash
        sudo service klipper stop
        ```
    1. From the `firmwares/ACE2` folder, do a dry run first. This talks to the unit, reads its current version, and
        parses the image without writing anything:
        ```bash
        ~/klippy-env/bin/python3 ace2-ota-update.py /dev/serial/by-id/usb-1a86_USB_Single_Serial_5B61016332-if00 AFC_ACE2PRO.bin --version 1.0.0 --dry-run
        ```
    1. If the dry run succeeds, run the same command without `--dry-run` to flash:
        ```bash
        ~/klippy-env/bin/python3 ace2-ota-update.py /dev/serial/by-id/usb-1a86_USB_Single_Serial_5B61016332-if00 AFC_ACE2PRO.bin --version 1.0.0
        ```
    1. Answer `y` when prompted, then wait until it prints `[done] Flash complete`.
    1. Power cycle the ACE. This step is required; the unit keeps running the old firmware until it is physically
        power cycled.
    1. Start Klipper back up:
        ```bash
        sudo service klipper start
        ```

    ### Checking what the unit runs

    After the power cycle, AFC's log will show the firmware version in the `ACE device info` line. The custom AFC
    build reports `AFCACE2PRO`, while stock firmware reports a version like `V1.1.31`.

    ### Reverting to stock

    Flash the stock firmware image the same way, with `--force` added to the flash command. Keep a copy of your
    stock firmware archived somewhere safe so it's ready whenever you need to revert.

=== "Creality CFS"

    The CFS does not need any firmware flashed for AFC to work; the box's own firmware is not changed. If your printer
    shipped with Creality's own CFS support, remove it before installing AFC (see the "Creality CFS" tab on the
    [Overview](01-overview.md) page), then continue on the [Plugin Install](03-install-plugin.md) page.
