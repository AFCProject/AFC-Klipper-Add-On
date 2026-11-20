# Troubleshooting

## Known Issues

As with any project, there are some known issues that may arise. Known issues for the AFC-Klipper-Add-On project can 
be found [on the troubleshooting page](https://github.com/AFCProject/AFC-Klipper-Add-On/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug).

We encourage anyone who experiences an issue to check the known issues list first, and if the issue is not listed, please
open a new issue on the GitHub repository. When opening a new issue, please provide as much detail as possible,
following the issue template provided. This will help us to quickly identify and resolve the issue.


## AFC Debug Script

A debug script is available to be run that can assist the Armored Turtle support team on their Discord server. To run 
this script, simply use the following commands on your SBC:

```bash
cd ~/AFC-Klipper-Add-On/troubleshooting
./afc-debug.sh
```

This script will collect some basic information about your system to include Klipper logs, device information, CAN 
network information, and your AFC configuration. This information will be uploaded to termbin.com, and the resulting 
link can be used to share this information with the Armored Turtle support team.

## General Unreliability

### Timer Too Close (TTC) errors
AFC can require more resources than some SBCs are able to support with the stock settings. If you are encountering 
Timer Too Close (TTC) errors, the following may help resolve it:

- Operate the AFC-Lite in CAN bus mode instead of USB mode. USB bandwidth can be limited, especially when also running 
  cameras on the same bus.
- Reduce your `long_moves_speed` or `long_moves_accel` values in `AFC/AFC.cfg` from the defaults.
- Try disabling unnecessary services on the SBC, such as Crowsnest or KlipperScreen and see if that resolves the 
  problem.
- Ensure you are using a current Operating System (Debian 12+ recommended).
- Look into adjusting the `niceness` setting. More info available [here](https://canbus.esoterical.online/troubleshooting/timeout_during_homing_probing.html).
- Ensure you are using a high quality USB cable. 

If you keep getting TTC errors start by adding the following to `~/printer_data/config/AFC/AFC.cfg` file under the 
[AFC] section.

`trsync_update: True`


Another potential solution that people have found successful is to reformat the SD card on your SBC and reflash the
AFC-Lite firmware. This can help resolve issues with the filesystem that may be causing the
TTC errors. Ensure you back up your configuration files before reformatting the SD card and reinstalling all software.

### Lost communication with MCU
This usually indicates some sort of cabling or power delivery problem. First, double check that all crimps for the 
connectors for power and data are secure, even if it "looks" ok it may have a weak connection. Don't spend days 
troubleshooting problems when you can re-crimp the connectors and test in a half hour.

Powering the SBC via your MCU (e.g., on a Leviathan) may also not provide sufficient power to the device. Rule this out as a cause by running separate power to the device. If you are sharing multiple AC to DC PSUs, 
ensure that the V- wires are connected together for a common reference voltage.

## Flashing Issues or Errors

If you are having issues flashing the AFC-Lite, please ensure that you are using the correct configuration for your device.

### Error sending command: [SEND_BLOCK] to Device

The easiest way to resolve this issue is to revert to a known commit in Katapult, rebuild the katapult firmware, and then
reflash the katapult firmware to the AFC-Lite.

```shell
cd ~/katapult
git checkout e1716657
make clean
make menuconfig
make
```

Once the new katapult firmware is built, you can flash it to the AFC-Lite, and then proceed with flashing Klipper as normal.


## Mainsail

### AFC sections not showing up correctly

If you have Mainsail (>= 2.15.0) installed and the AFC sections are not showing up correctly, you may need to force 
reload your browser with one of the following commands depending on your browser / OS:

| Operating System | Browser | Key Combination  |
|------------------|---------|------------------|
| Windows          | Chrome  | ++ctrl+f5++      |
| Windows          | Firefox | ++ctrl+f5++      |
| MacOS            | Chrome  | ++cmd+shift+r++  |
| MacOS            | Firefox | ++cmd+shift+r++  |
| MacOS            | Safari  | ++cmd+option+r++ |

## Common Printing Issues

### Extruder doesn't seem to be extruding properly after using BT

We recommend ensuring you are on at least Klipper version v0.13.0 (or Kalico 2026). This is required on all MCUs to be effective.
An example of this can be seen below:

- Pre-Klipper v0.13.0
![pre-klipper-v0.13.0-extrude-issue](../assets/images/pre-update.jpg)

- Post-Klipper v0.13.0
![post-klipper-v0.13.0-extrude-issue](../assets/images/post-update.jpg)

### `TMC 'AFC_stepper lane1' reports error: GSTAT:      00000001 reset=1(Reset)`

This may be due to static building up in the system from the PTFE tubing in dry/low humidity environments. Many users
have found that running a ground wire from the stepper motor screws to a common GND pin on the AFC-Lite board, resolves
this issue. One example way to do this is below:

For each motor:

- Replace the M3x8 extruder motor mount screw with a M3x12 screw.
- Crimp a ring connector on a wire. Place a m3 washer over the now extended motor mount screw, followed by the ring
terminal, followed by an M3 hex or nyloc nut.

Join the wires from all the motors into a 5 port [WAGO 221-415](https://www.wago.com/us/wire-splicing-connectors/compact-splicing-connector/p/221-415). In the 5th port, run a wire from the WAGO to any spare
ground pin on the AFC-Lite board (e.g., the GND pin on RGB2).

### LEDs not displaying correct colors

If your LEDs are not displaying the correct color, update the following value under your AFC_led section in 
`~/printer_data/config/AFC/AFC_Turtle_(n).cfg` file.

- color_order: change to match the color order for your LEDs. Different color orders are: RGB, RGBW, GRB, GRBW

### Layer shift when using the cut macro

If you notice a layer shift occurs while using the cut macro, setting a higher stepper current while cutting has shown 
to help with this. Update and uncomment the following values in `~/printer_data/config/AFC/AFC_Macro_Vars.cfg` file.

- `variable_cut_current_stepper_x` - start with ~1.7-1.8A
- `variable_cut_current_stepper_y` - start with ~1.7-1.8A
- Only needed if cutting action is along the z - `variable_cut_current_stepper_z`

- Make sure your stepper names are updated for variables: `variable_cut_current_stepper_x`, 
- `variable_cut_current_stepper_y`, and `variable_cut_current_stepper_z`.


### "Move out of range" errors when running macros
`skew_correction` changes the coordinate system and the measures you took without a skew profile will be different from at print time. 
To solve this issue, use `variable_disable_skew_correction` but make sure you read the [configuration documentation](../configuration/AFC_Macro_Vars.cfg.md) first.

### Filament pulling past extruder during unloads

During unloads, if your filament retracts too much and goes past the lane's extruder then decrease your 
`afc_bowden_length` value in the `[AFC_hub <hub_name>]` portion of the `~/printer_data/config/AFC/AFC_Turtle_(n).cfg` 
file. 

## Creality CFS

### Checking a CFS box

`AFC_CFS_STATUS UNIT=<name>` shows a box's state: version, uid, which slots have filament, the buffer, the cutter
switch, link counters and each lane's last tag. `AFC_CFS_RECONNECT UNIT=<name>` reopens the link to a box.

### "Homing error occurred when trying to short move to hub sensor" right after an update

Restart the Klipper service. `FIRMWARE_RESTART` rereads the config but keeps the old Python code loaded.

### Loads fail at the hand-off ("toolhead sensor cleared after the handoff")

Set `prime1_speed: 35` and `prime2_speed: 10` in the `[AFC_CFS <unit_name>]` section to go back to Creality's own
speeds.

### A tagged spool reads as no tag

Turn the spool around so the tag faces the reader.

### Recording a trace for a bug report

Add `trace_file: <path>` to an `[AFC_CFS <unit_name>]` section to record everything AFC sends to and gets from the
box, with load and unload timings. `AFC_CFS_TRACE UNIT=<name> MARK="..."` adds a marker before a test. Attach the
trace when reporting a problem.

`AFC_CFS_RAW` sends a single raw frame to the box. It's for protocol debugging only: motor commands sent this way
bypass all of AFC's checks.
