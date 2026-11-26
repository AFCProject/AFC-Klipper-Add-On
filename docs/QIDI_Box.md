# QIDI Box

The QIDI Box is QIDI's 4-slot filament unit with a built-in dryer, sold for the Plus4, Q2 and
Max4. Its controller is a Klipper MCU on USB, so AFC drives it as a normal unit:
`[AFC_QidiBox <name>]` with one `[AFC_stepper]` lane per slot. Loads, unloads, ejects, PREP,
runout and calibration are AFC's own logic, as on a Box Turtle.

Files:

- `templates/AFC_QidiBox_1.cfg`: the unit, its lanes, hub and dryer hardware. It includes
  `mcu/QidiBox_1.cfg`.
- `config/mcu/QidiBox.cfg`: the box MCU (serial) and its pin names. The installer copies it to
  `<AFC config folder>/mcu/QidiBox_1.cfg` (normally `~/printer_data/config/AFC/mcu/`). For a
  manual install, copy it there yourself under that name, into the `mcu/` folder beside
  `AFC_QidiBox_1.cfg`.
- The installer offers it as **QIDI Box** and copies both files.

## Hardware

| Part | Pins (box MCU) | Notes |
|---|---|---|
| MCU | USB on PA11/PA12 | STM32F401xC, 8 MHz crystal |
| Slot 1-4 steppers (step/dir/enable) | PC14/PC13/!PC15, PB9/PB8/!PC0, PC12/PC11/!PD2, PC8/PB2/!PC10 | standalone drivers: no UART, current set in hardware |
| Slot 1-4 entry sensors | PA0, PB3, PA13, PA7 | one sensor per slot, before the gears |
| Shared hub (outlet) sensor | PB1 | all four slots merge into one outlet inside the box |
| Buffer tension switch | PB0 | not used by AFC |
| Slot LEDs (white, red) | PA1/PA2, PB4/PB5, PA14/PA15, PC4/PC5 | not driven by AFC |
| Dryer heater | PA3 | |
| Dryer air sensor | AHT20, I2C3 on PA8 (SCL) / PC9 (SDA), address 0x38 | temperature and humidity |
| Heater element thermistors | PC1, PC2 | NTC 100K MGB18-104F39050L32, max 130 C |
| Dryer fans | PA4, PA5 | QIDI runs them above 35 C |
| Board fan | PA6 | |
| NFC readers | CS PC6 (slots 1-2), PC7 (slots 3-4), SPI2, reset PB12 | not used by AFC |

QIDI's limits: dryer air `max_temp: 100`, `target_max_temp: 90`, element thermistors
`max_temp: 130`. QIDI's per-material table never asks for more than 65 C. Slot
`rotation_distance` is 13.4 in QIDI's Plus4 config and 13.8 in its Q2 config.

## Firmware

The files work with either box firmware. The installer asks with **QIDI Box Firmware**
(`Klipper` or `Stock`) and sets the two lines that differ; by hand, swap them as described
below. Nothing else in AFC depends on the firmware.

**Klipper converted box** (the default): the box runs the same mainline Klipper (or Kalico) MCU
firmware as the rest of the printer, as Happy Hare's QIDI Box setup and q2-offload do. Build it
with:

- STM32F401, 8 MHz crystal, USB on PA11/PA12
- bootloader offset: 32KiB with QIDI's own bootloader (as documented by Happy Hare), or 16KiB
  if the box was converted to Katapult (as q2-offload does)

It then shows up as `/dev/serial/by-id/usb-Klipper_stm32f401xc_<id>-if00`, and the dryer air
sensor is read as software I2C on PA8/PC9.

**Stock QIDI firmware**: the box keeps QIDI's MCU firmware and shows up as
`usb-Klipper_QIDI_BOX_V2_<version>_<id>-if00` (`QIDI_BOX_V1` on V1 boards). In
`mcu/QidiBox_1.cfg` use the commented stock `serial:` line instead of the converted one, and in
`[temperature_sensor QidiBox_1_dryer_air]` comment out the two `i2c_software_*` pins and
uncomment `i2c_bus: i2c3`, the hardware bus QIDI's own config uses. QIDI builds this firmware
from its own Klipper fork; if your Klipper or Kalico host refuses to talk to it (a protocol or
unknown command error at startup), convert the box.

## Modes

**Hub (stock box).** The slots merge inside the box and feed one extruder. Each slot sensor is
the lane's only sensor, so it is the lane's `load:` and the lane has no `prep:`. AFC treats
the lane as loaded as soon as the slot sensor sees filament. A new spool is then fed to the hub
sensor and parked `preload_park_dist` (80 mm) before it, like QIDI's firmware does, so the
shared hub stays free (set `load_to_hub: False` on a lane to turn that off). `dist_hub` is the
length from the slot sensor to the hub sensor, `afc_bowden_length` the length from the hub
sensor to the toolhead sensor.

**Direct (one lane per extruder).** For a toolchanger or other multi-extruder printer, with the
box's merger bypassed and each slot piped to its own extruder. In each lane:

```
prep: !QidiBox_1:TRG1          # the slot sensor
load: ^<printer side sensor>   # e.g. the tool's own inlet sensor
hub: direct
extruder: extruder1            # this lane's tool
dist_hub: 700                  # load sensor to toolhead sensor
```

and comment out `hub:` in `[AFC_QidiBox]`. Inserting a spool feeds it from the slot to the
printer side sensor (PREP); a tool load feeds `dist_hub` to that tool's toolhead sensor. PREP
and eject stop after `max_short_moves` (40) moves of `short_move_dis`, so raise
`max_short_moves` if the printer side sensor is further than about 400 mm from the box.

## Dryer

```
AFC_QIDIBOX_DRY UNIT=QidiBox_1 TEMP=55 DURATION=480 ROTATE=1
AFC_QIDIBOX_DRY_STOP UNIT=QidiBox_1
```

- `TEMP` is capped to `dryer_max_temp` (default 65 C, at most 90 C, QIDI's own cap, and never
  above the heater's `max_temp`). `DURATION` is in minutes; the heater turns off when it
  ends. Defaults come from `dryer_default_temp` (50) and `dryer_default_duration` (240).
- `ROTATE=1` turns the spools like QIDI's firmware: every `dry_rotate_interval` (300 s) each
  lane with no filament at its sensors retracts `dry_rotate_length` (50 mm) at
  `dry_rotate_speed` (80 mm/s), which turns its spool on the rollers. It holds while printing
  or while AFC is busy. Unload a lane, tip secured to the spool, to dry it with rotation.
- The unit's status has a `dryer` entry: `active`, `target`, `temperature`, `humidity`,
  `remaining` (seconds) and `rotate`.

Thermal protection stays with Klipper: `verify_heater` on the dryer heater, `max_temp: 100` on
the air sensor and `max_temp: 130` on the two element thermistors shut Klipper down when
exceeded.

Klipper's AHT driver reads at most every 5 s (`aht10_report_time`), slower than the 3 s a
Klipper heater needs between readings, so the heater reads the air sensor through a
`temperature_combined` sensor, which re-reports it every 0.3 s. A `heater_generic` with
`sensor_type: AHT10`/`AHT2X` used directly would shut the box MCU down. QIDI's PID values come
with the template; run `PID_CALIBRATE HEATER=QidiBox_1_dryer TARGET=55` once.

## Coming from other configs

- **A Box Turtle style config with FORCE_MOVE macros** (unit `[AFC_BoxTurtle]`, toolhead
  load/unload macros, a `heater_generic` on `sensor_type: AHT10` with
  `i2c_software_scl_pin: PB6`/`sda: PB7`, "PTC" `Generic 3950` sensors): use
  `templates/AFC_QidiBox_1.cfg` in direct mode instead. AFC's own `TOOL_LOAD`/`TOOL_UNLOAD`
  replace the load and unload macros (set `dist_hub` to the old bowden length from the printer
  side sensor to the toolhead sensor), `AFC_QIDIBOX_DRY` replaces the dryer macros. The AHT20
  is on PA8/PC9, not PB6/PB7, and the element sensors are NTC 100K MGB18-104F39050L32
  thermistors.
- **Happy Hare's QIDI Box profile**: the pins are the same. `unit0_heater`, `unit0_Env`,
  `unit0_heater_fan_a/b` and `unit0_board_fan` map to `QidiBox_1_dryer`,
  `QidiBox_1_dryer_air`, `QidiBox_1_dryer_fan_a/b` and `QidiBox_1_board_fan`; HH's gate
  parking distance (-80) is `preload_park_dist`, `MMU_HEATER DRY=1` is `AFC_QIDIBOX_DRY`.

## Not verified on hardware

- Sensor polarity: the pins follow Happy Hare's profile (`!` on the slot sensors, `^!` on the
  hub sensor). Flip the `!` if AFC shows a lane loaded with the slot empty.
- `dist_hub: 200` and `afc_bowden_length: 900` are placeholders; run `AFC_CALIBRATION`.
- QIDI also defines `b_endstop_pin: PA9` and `e_endstop_pin: PA10` on the box; their use is not
  documented and AFC does not use them.
- The box's buffer (PB0), slot LEDs and NFC readers are not supported yet.
- Where the slot sensor sits relative to the drive gear. Hub mode lane calibration and eject
  retract until the slot sensor clears; if the sensor is before the gear, calibration can pull
  the tip out of the gear. Measure `dist_hub` by hand (`LANE_MOVE`) if that happens.

Sources: QIDI's box configs ([Plus4 box1.cfg](https://github.com/QIDITECH/QIDI_PLUS4/blob/main/config/box1.cfg),
[Q2 box1.cfg](https://github.com/QIDITECH/QIDI_Q2/blob/main/config/box1.cfg)), Happy Hare's
QIDI Box profile ([board](https://github.com/moggieuk/Happy-Hare/blob/main/installer/boards/custom/Kconfig.qidi_box),
[unit](https://github.com/moggieuk/Happy-Hare/blob/main/installer/mmu_types/Kconfig.qidi_box))
and [q2-offload](https://github.com/cedbossneo/q2-offload).
