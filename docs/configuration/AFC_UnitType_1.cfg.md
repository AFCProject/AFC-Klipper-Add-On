# AFC_UnitType_1.cfg Configuration Overview

The `AFC_UnitType_1.cfg` file is used to define the configuration for the AFC system when using a specific unit type.

This file will typically be named in the format of `AFC_Turtle_1.cfg` for example, when a BoxTurtle is installed. If 
multiple unit types are installed, the file will be named in the format of `AFC_Turtle_2.cfg`, `AFC_Turtle_3.cfg`, etc.

!!! note

    The actual naming convention may vary depending on your specific setup. The unit name can be renamed by using the 
    `install-afc.sh` script.

## [mcu mcu_name] Section

This section will define the MCU used for the specific unit.

!!! warning

    Either `canbus_uuid` or `serial` should be defined, not both. Please ensure that you are using the correct
    configuration for your setup. The `canbus_uuid` is used for CAN bus devices, while the `serial` is used for
    USB devices. Whichever option is **NOT** being used should be commented out.

``` cfg
[include mcu/<mcu_name>.cfg]
#    This will be used for any MCU aliases or definition files.
[mcu mcu_name]
canbus_uuid: 1234567890a
#    UUID of the CAN bus device. This is used to identify the device 
#    on the CAN bus.
serial: /dev/serial/by-id/<device-name>
#    Serial address of the device. This is usually in the format of
#    `/dev/serial/by-id/<device-name>` 
```

## [temperature_sensor sensor_name] Section

If your MCU has a temperature sensor built in, you can define it here. 

``` cfg
[temperature_sensor sensor_name]
sensor_type: temperature_mcu
#    Type of temperature sensor. This is usually `temperature_mcu`
sensor_mcu: mcu_name
#    Name of the MCU that the temperature sensor is connected to.
```

## [AFC_lane lane_name] Section

The following options are available in the `[AFC_lane <lane_name>]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with the stepper motor for the specific unit type.
You will typically have one of these sections for each lane in the unit.  

``` cfg
[AFC_lane <lane_name>]

unit: MCU:<lane>
#    This is the unit name of the stepper motor. This would typically be 
#    formatted as `MCU:<lane>` where `<lane>` is the lane number. The
#    MCU being referenced is relative to the `[mcu <mcu_name>]`
#    in this file. For example: `unit: Turtle_1:1` would set this as
#    the first lane for the hardware connected to the `Turtle_1` MCU.
step_pin: mcu:pin
#    Step GPIO pin (triggered high). This parameter must be provided.
dir_pin: mcu:pin
#    Direction GPIO pin (high indicates positive direction). This
#    parameter must be provided.
enable_pin: mcu:pin
#    Enable pin (default is enable high; use ! to indicate enable
#    low). If this parameter is not provided then the stepper motor
#    driver must always be enabled.
microsteps: 16
#    The number of microsteps the stepper motor driver uses. This
#    parameter must be provided.
rotation_distance: 4.65
#    Default: 4.65
#    Distance (in mm) that the axis travels with one full rotation of
#    the stepper motor (or final gear if gear_ratio is specified).
#    This parameter must be provided. With a standard BoxTurtle based
#    on BOM components, this parameter should not need to be adjusted.
gear_ratio:
#    Default: <none>
#    For a standard BoxTurtle, this parameter should not need to be
#    set. This parameter is only used if a different gear set is used.
#    The gear ratio if the stepper motor is connected to the axis via a
#    gearbox. For example, one may specify "5:1" if a 5 to 1 gearbox is
#    in use. If the axis has multiple gearboxes one may specify a comma
#    separated list of gear ratios (for example, "57:11, 2:1"). If a
#    gear_ratio is specified then rotation_distance specifies the
#    distance the axis travels for one full rotation of the final gear.
#    The default is to not use a gear ratio.
map:
#    Default: None
#    Used for custom mapping toolchange macros. This can be set to anything
#    but a normal toolchange macro is normally T(n). This variable also allows
#    a comma separated list to be passed in.
dist_hub: 60
#    Default: 60
#    Bowden distance between Box Turtle extruder and hub.
park_dist: 10
#    Default: 10
#    Currently unused.
led_index: AFC_Indicator:1
#    Default: None
#    LED index of lane in chain of lane LEDs. This should reference
#    a defined LED in the `[AFC_Indicator]` section.
#
#    Index can have multiple entries in a comma separated list and range values
#    also are allowed
#    eg. AFC_Indicator_4:1,2,3,4, 6-9, 11-14, 16-18
afc_motor_rwd: Turtle_1:MOT1_RWD
#    Reverse pin on MCU for spoolers (N20 motors).
afc_motor_fwd: Turtle_1:MOT1_FWD
#    Forward pin on MCU for spoolers (N20 motors).
afc_motor_enb: Turtle_1:MOT1_EN
#    Enable pin on MCU for spoolers (N20 motors).
rwd_speed_multiplier: 0.5
#    Default: 0.5
#    Multiplier to apply to rpm.
fwd_speed_multiplier: 0.5
#    Default: 0.5
#    Multiplier to apply to rpm.
pwm: True
#    Default: True
#    If True, the n20 motor will be controlled using PWM. 
prep: ^!Turtle_1:TRG1
#    MCU pin for prep trigger. This is the sensor attached to the
#    trigger that is used when filament is loaded.
load: ^!Turtle_1:EXT1
#    MCU pin for load trigger. This is the sensor attached to the
#    extruder body located AFTER the extruder motor.
led_fault: 1,0,0,0
#    Default: 1,0,0,0
#    LED color used when AFC flags a fault on the lane (jam, sensor
#    mismatch, failed load/unload, etc) and the lane needs attention.
#    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here 
#    overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section.
led_ready: 0,0.8,0,0
#    Default: 0,0.8,0,0
#    LED color used once filament is staged and confirmed ready behind
#    the hub. Also used for the lane's own LED after a tool unload,
#    since filament stays staged at the hub (see led_tool_unloaded
#    below for the separate toolhead LED color used at that point).
#    Overridden by the filament's color when led_use_filament_color is
#    enabled below.
#    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here 
#    overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section.
led_not_ready: 1,0,0,0
#    Default: 1,0,0,0
#    LED color used when the lane is empty, with no spool prepped
#    (prep and load sensors both false). Also reused when a lane is
#    unloaded back out of the hub but a spool is still prepped in the
#    lane (prep sensor still true).
#    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here 
#    overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section.
led_loading:
#    Default: 1,1,1,0
#    LED color used while filament is actively feeding into the lane,
#    either loading to the toolhead or loading a spool into the lane
#    itself.
#    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here 
#    overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section.
led_unloading: 1,1,1,0
#    Default: 1,1,1,0
#    LED color used while filament is backing out of the lane, either
#    unloading from the toolhead or ejecting the lane.
#    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here 
#    overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section.
led_tool_loaded: 0,0,1,0
#    Default: 0,0,1,0
#    LED color used on the lane's LED (and its toolhead status LED, if
#    `status_led_idx` is set on the extruder) while this lane is the
#    active/loaded tool. Not affected by led_use_filament_color.
#    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here 
#    overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) section.
led_spool_index:
#    Default: None
#    Led index for led in chain that illuminates spool, currently only
#    supports QuattroBox units. This should reference a defined LED in 
#    the [AFC_Indicator] section. 
#
#    Index can have multiple entries in a comma separated list and range 
#    values also are allowed.
#    eg. AFC_Indicator_4:1,2,3,4, 6-9, 11-14, 16-18
led_tool_loaded_idle: 0.4,0.4,0,0
#    Default: 0.4,0.4,0,0
#    LED color used on the lane's LED and its toolhead status LED
#    (`status_led_idx`) when this lane's tool is loaded but idle/parked,
#    e.g. waiting between tool changes on a toolchanger. Overridden by
#    the filament's color when led_use_filament_color is enabled below.
#    Format: (R,G,B,W) where 0 = off and 1 = full brightness.
#
#    Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) 
#    section.
led_tool_unloaded: 1,0,0,0
#    Default: 1,0,0,0
#    Toolhead status LED color (`status_led_idx` on the extruder) shown
#    when this lane's tool has just been unloaded from the toolhead.
#    This does not affect the lane's own LED, which instead reverts to
#    led_ready (or the filament color, if led_use_filament_color is
#    enabled).
#    Format: (R,G,B,W) where 0 = off and 1 = full brightness.
#
#    Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) 
#    section.
led_spool_illuminate: 1,1,1,0
#    Default: 1,1,1,0
#    Loading color to illuminate spool, currently only for QuattroBox units.
led_use_filament_color: False
#    Default: False
#    When True, the lane's LED (and, where noted above, the toolhead
#    status LED) uses the filament color from the spool color field (set
#    manually or synced from Spoolman) instead of the configured
#    led_ready/led_tool_loaded_idle colors. Other states (fault, loading,
#    unloading, not ready, tool loaded) always use their configured
#    color regardless of this setting.
#
#    Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) 
#    section.
long_moves_speed: 150
#    Default: 150
#    Speed in mm/s to move filament when doing long moves. 
#    Setting value here overrides values set in 
#    unit(AFC_BoxTurtle/NightOwl/etc) section
rev_long_moves_speed_factor: 1.0
#    Default: 1.0
#    Range: 0.5 to 1.2
#    Scalar factor multiplied to long_moves_speed when rewinding filament.
#    Useful when reversing is done using non-assisted systems, e.g. filamentalist
long_moves_accel: 250
#    Default: 250
#    Acceleration in mm/s squared when doing long moves. 
#    Setting value here overrides values set in 
#    unit(AFC_BoxTurtle/NightOwl/etc) section
quiet_moves_speed: 50
#    Default: 50           
#    Speed in mm/s. This is the speed cap during quiet moves.
short_moves_speed: 50
#    Default: 50
#    Speed in mm/s to move filament when doing short moves. 
#    Setting value here overrides values set in 
#    unit(AFC_BoxTurtle/NightOwl/etc) section
short_moves_accel: 300
#    Default: 300
#    Acceleration in mm/s squared when doing short moves. 
#    Setting value here overrides values set in 
#    unit(AFC_BoxTurtle/NightOwl/etc) section
short_move_dis: 10
#    Default: 10
#    Move distance in mm for failsafe moves. Setting value 
#    here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc) 
#    section
tool_max_unload_attempts: 4
#    Default: 4
#    Max number of attempts to unload filament from toolhead when using 
#    buffer as ramming sensor.
#    Setting variable here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc)
#    or AFC config sections.
max_move_dis: 99999
#    Default: 99999
#    Maximum distance to move filament. AFC breaks filament moves over 
#    this number into multiple moves. Useful to lower this number if 
#    running into timer too close errors when doing long filament moves. 
#    Setting value here overrides values set in 
#    unit(AFC_BoxTurtle/NightOwl/etc) section
n20_break_delay_time: 0.200
#    Default: 0.200
#    Time to wait between braking N20 motors(nSleep/FWD/RWD all 1) 
#    and then releasing the brake to allow coasting. Setting value 
#    here overrides values set in unit (AFC_BoxTurtle/NightOwl/etc) section.
enable_assist: True
#    Default: True
#    Enables espooler print assist. Setting value 
#    here overrides values set in unit (AFC_BoxTurtle/NightOwl/etc) section.
enable_assist_weight: 500
#    Default: 500
#    Number in grams to activate espooler print assist once spool weight is 
#    less than this number.
#    Setting value here overrides values set in unit (AFC_BoxTurtle/NightOwl/etc)
#    section.
timer_delay: 5
#    Default: 5
#    Affects espooler assist, number of seconds to wait before checking 
#    filament movement for espooler print assist. Setting value here 
#    overrides values set in unit (AFC_BoxTurtle/NightOwl/etc) section.
enable_kick_start: True
#    Default: True
#    Enables full speed espooler's for `kick_start_time` amount to
#    help spools to start moving. Setting value here overrides values
#    set in unit (AFC_BoxTurtle/NightOwl/etc) section.
kick_start_time: 0.070
#    Default: 0.070
#    Time in seconds to enable spooler at full speed to help with
#    getting the spool to spin. Setting value here overrides values set 
#    in unit (AFC_BoxTurtle/NightOwl/etc) section.
delta_movement: 150
#    Default: 150
#    Affects espooler assist, delta amount in mm to move from last assist
#    to trigger another assist move. Setting value here overrides values 
#    set in unit (AFC_BoxTurtle/NightOwl/etc) section.
spoolrate: 1.0
#    Default: 1.0
#    Scaling factor for the following variables: 
#      kick_start_time, spool_outer_diameter, delta_movement
#    Setting value here overrides values set in unit(AFC_BoxTurtle/NightOwl/etc).
#    section.
espool_rot_dist: 132.9
#    Default: 132.9
#    Rotation distance estimation of espooler wheels, setting value
#    here will override value in AFC.cfg. Setting value here
#    overrides values set in unit(AFC_BoxTurtle/NightOwl/etc).
spool_ratio: 2
#    Default: 2
#    Gear ratio for printed gearbox between N20 and spooler wheels, setting value here
#    overrides values set in unit(AFC_BoxTurtle/NightOwl/etc).
full_weight: 1000
#    Default: 1000
#    Full starting weight of filament spool (not including spool), setting value here
#    overrides values set in unit(AFC_BoxTurtle/NightOwl/etc).
load_to_hub: True
#    Default: True
#    Fast loads filament to hub when inserted, set to False to disable. This value is not
#    used in HTLF units.
#    Setting here overrides global setting in AFC.cfg.
enable_sensors_in_gui: False
#    Default: False
#    Set to True to show prep and load sensors switches as filament sensors 
#    in Mainsail/Fluidd gui, overrides value set in AFC.cfg.
sensor_to_show:
#    Default: <none>
#    Set to prep to only show prep sensor, set to load to only show load 
#    sensor. Do not add if you want both prep and load sensors to show in 
#    web GUI.
assisted_unload: False
#    Default: False
#    If True, the unload retract is assisted to prevent loose windings, 
#    especially on full spools. This can prevent loops from slipping off the 
#    spool. Setting value here overrides values set in 
#    unit(AFC_BoxTurtle/NightOwl/etc) section.
filament_diameter: 1.75
#    Default: 1.75
#    Diameter of the filament in mm.
filament_density: 1.24
#    Default: 1.24
#    Density of the filament in g/cm^3.
spool_inner_diameter: 100
#    Default: 100
#    Inner diameter of the spool in mm.
spool_outer_diameter: 200
#    Default: 200 
#    Outer diameter of the spool in mm. Affects espooler assist logic
empty_spool_weight: 190
#    Default: 190
#    Weight of the empty spool in grams. This value is pulled automatically if
#    using spoolman.
spool_weight: 1000
#    Default: 1000
#    Remaining filament weight in grams on spools.  This value is pulled automatically
#    if using spoolman.
max_motor_rpm: 465
#    Default: 465
#    Maximum motor RPM for the assist motor (N20).
hub: 
#    Default: <none>
#    Hub that this lane belongs to. Set to the name of an AFC_hub
#    section to override the hub assigned by the
#    unit (AFC_BoxTurtle/NightOwl/etc).
#
#    Two special values are also accepted:
#
#    direct       - Use when this lane has no hub and connects directly
#                   to the toolhead.
#    direct_load  - Same as direct, but AFC will automatically load
#                   filament to the toolhead when filament is inserted
#                   into the lane.
buffer:
#    Default: <none>
#    Buffer name(AFC_buffer) that belongs to this stepper, overrides 
#    buffer that is set in extruder(AFC_extruder) or 
#    unit(AFC_BoxTurtle/NightOwl/etc) sections.
extruder:
#    Default: <none>
#    Extruder name(AFC_extruder) that belongs to this stepper, 
#    overrides extruder that is set in unit(AFC_BoxTurtle/NightOwl/etc) 
#    section.
debounce_delay: 0
#    Default: 0
#    A period of time in seconds to debounce switches prior to detecting
#    runout. If switches are pressed and released during this delay,
#    the entire switch event is ignored.
#
#    This value overrides value set in AFC config section
capture_td1_when_loaded: False
#    Default: False
#    When set to True and TD-1 device is correctly configured in moonraker, 
#    when filament is loaded into a lane AFC will move filament to TD-1 device
#    to capture TD and color.
#
#    Setting value here overrides values set in unit config(AFC_BoxTurtle/NightOwl/etc).
td1_device_id: None
#    Default: None
#    Set this value to TD-1 device ID to use for a lane, this is only needed if
#    using multiple TD-1 devices.
td1_bowden_length:
#    Default: value from AFC_hub config section (if set)
#    Length in mm from the hub to the TD-1 device for this lane. AFC moves
#    filament exactly this distance when capturing TD-1 color and type data.
#
#    If not set, AFC falls back to the td1_bowden_length
#    value defined in the associated AFC_hub section. This value is not
#    used for direct hub setups.
#
#    Setting value here overrides td1_bowden_length set in AFC_hub section.
#
#    Run AFC_CALIBRATION to automatically calibrate this length,
#    option to calibrate will only show up if TD-1 has been setup
#    correctly in moonraker.
remember_spool: False
#    Default: False
#    If true, AFC will retain values (spoolID, weight, color, material) of the last spool
#    that was ejected from a lane and will reuse those values the next time the given
#    lane is loaded.
#    Overrides variable that is set in unit(AFC_BoxTurtle/NightOwl/etc) sections.
homing_overshoot: 50
#    Default: 50
#    Additional amount to add to all homing moves to guarantee the move will hit
#    endstops when homing.
#
#    Overrides variable that is set in unit(AFC_BoxTurtle/NightOwl/etc) sections.
homing_delta: 300
#    Default: 300
#    This value is only valid when homing is enabled and is used as another verifier
#    to make sure filament is within this range from commanded homing movement. If
#    homing move is not within this delta of commanded movement then AFC will try 
#    to home again to verify that filament is at correct position.
#
#    Overrides variable that is set in unit(AFC_BoxTurtle/NightOwl/etc) sections.
load_then_home: True
#    Default: True
#    When set to True and utilizing the buffer in ramming mode, AFC will first do a normal
#    move toward the toolhead where the total distance is:
#       - afc_bowden_length - load_undershoot, or
#       - dist_hub - load_undershoot (when using a direct hub).
#    After this initial move to the toolhead, AFC will then do a homing move the rest of
#    the way until the buffer advance sensor is triggered.
#
#    Overrides variable that is set in unit(AFC_BoxTurtle/NightOwl/etc) sections.
load_undershoot: 20
#    Default: 20
#    Amount to subtract from afc_bowden_length (or from dist_hub when using a direct hub)
#    when load_then_home is enabled.
#
#    Overrides variable that is set in unit(AFC_BoxTurtle/NightOwl/etc) sections.
extruder_clear_dis: 50
#    Default: 50
#    This variable affects additional move distance when ejecting filament and filament
#    has passed load sensor. AFC will move this additional amount to make sure filament
#    is no longer in extruder gears.
#    Overrides variable that is set in unit(AFC_BoxTurtle/NightOwl/etc) sections.
calibrated_lane: False
#    Default: False
#    Currently used in AFC_vivid units so AFC know if lane needs to calibrate dist_hub
#    distance when inserting filament for the first time.
selector_cal_distance: 0.0
#    Default: 0.0
#    Currently used only by AFC_vivid, AFC_HTLF and AFC_Claymore units, this value is
#    the amount to move in mm once selector is homed to specified lane. AFC will then
#    move the selector by this amount after the home to sensor has finished.
#    By modifying this value, this could allow the selector to have a better grip
#    on the filament.
custom_load_cmd:
#    Default: None
#    Custom G-code macro to run instead of AFC's built-in load sequence
#    when loading this lane to the toolhead. Leave unset to use AFC's
#    default load sequence.
custom_unload_cmd:
#    Default: None
#    Custom G-code macro to run instead of AFC's built-in unload sequence
#    when unloading this lane from the toolhead. Leave unset to use AFC's
#    default unload sequence.
```

## [AFC_stepper lane_name] Section

The following options are available in the `[AFC_stepper <lane_name>]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with the stepper motor for the specific unit type.
You will typically have one of these sections for each lane in the unit.

AFC_stepper inherits configuration options from AFC_lane configuration section, below are additional configuration values
for the AFC_stepper sections.  

``` cfg

[AFC_stepper <lane_name>]

print_current: 0.6
#    Default: 0.6
#    Current to use while printing, set to a lower current to reduce stepper 
#    heat when printing. Defaults to global_print_current, if not specified 
#    current is not changed.
default_homing_endstop: load
#    Default: load
#    Endstop to default to when running AFC_STEPPER_HOME macro.
hub_endstop: <hub_endstop_pin>
#    Default: endstop defined in AFC_hub config
#    Override default AFC_hub endstop lookup with custom hub endstop pin
#    (endstop must be defined in the AFC_hub config).
extra_homing_pins: ""
#    Default: ""
#    Extra homing pins to add to the lookup; can be used as an
#    endstop selection for the `AFC_STEPPER_HOME` macro.
```


## [tmc2209 AFC_stepper <lane_name>] Section

The following options are available in the `[tmc2209 AFC_stepper <lane_name>]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with the TMC2209 stepper motor driver for the specific unit type.

!!!note

    There should be a matching `[tmc2209 AFC_stepper <lane_name>]` section for each `[AFC_stepper <lane_name>]` 
    section in the configuration file.

``` cfg
[tmc2209 AFC_stepper <lane_name>]
uart_pin: mcu:pin
#    Example: Turtle_1:M1_UART
#    The UART pin for the TMC2209 stepper motor driver. 
uart_address: 0
#    Address of the TMC2209 driver on the UART bus.
run_current: 0.8
#    The amount of current (in amps RMS) to configure the driver to use
#    during stepper movement. This parameter must be provided.
sense_resistor: 0.110
#    The resistance (in ohms) of the driver sense resistor. This parameter
#    must be provided. Common values are 0.110 ohms for most TMC2209 drivers
#    and 0.075 ohms for TMC5160 drivers. Check your stepper driver documentation
#    or board schematic to confirm the correct value.
```
!!!warning

    Ensure that the `sense_resistor` is set appropriately. Failure to set this appropriately may result in
    overheating of the stepper motor driver and/or stepper motor. This value is typically set to 0.110 for the TMC2209
    stepper motor driver present on the AFC-Lite board for the BoxTurtle. You *MUST* change this based on your
    board if using another system / configuration.

## [AFC_hub hub_name] Section

The following options are available in the `[AFC_hub hub_name]` section of the `AFC_UnitType_1.cfg` file. These 
options control the configuration of the AFC system when interfacing with the hub for the specific unit type. Multiple
hubs may be defined in the configuration file.

``` cfg
[AFC_hub hub_name]
switch_pin: mcu:pin
#    Default: <none> (required)
#    MCU pin for the hub switch.
#
#    Alternatively, set to `virtual` to use a virtual hub sensor instead
#    of a physical switch. When set to virtual, the hub state is
#    determined by the load sensors on each attached lane — the hub is
#    considered triggered if any lane's load sensor is active.
#
#    Requires all lanes attached to this hub to have a load sensor
#    configured. AFC will raise a configuration error at startup listing
#    any lanes that are missing a load sensor.
#
#    Example (physical): switch_pin: mcu:PA5
#    Example (virtual):  switch_pin: virtual
hub_clear_move_dis: 55
#    Default: 55
#    How far to move filament so that it doesn't block the hub exit.
afc_bowden_length:  900
#    Default: 900
#    Length of the bowden tube in mm from the hub to the toolhead 
#    sensor.
#    Run AFC_CALIBRATION to automatically calibrate this length.
afc_unload_bowden_length: 900
#    Default: <afc_bowden_length>
#    Length to unload when retracting back from toolhead to hub in 
#    mm. This defaults to the value of `afc_bowden_length` if not
#    specified. With normal installations, this value should not
#    need to be changed or specified explicitly.
td1_bowden_length: <afc_bowden_length> - 50
#    Default: <afc_bowden_length> - 50
#    Length of the bowden tube in mm from the hub to TD-1 device.
#
#    Run AFC_CALIBRATION to automatically calibrate this length,
#    option to calibrate will only show up if TD-1 has been setup
#    correctly in moonraker.
use_dist_hub: False
#    Default: False
#    Set to True to use dist_hub value for PTFE length to toolhead.
#    This is useful when using sensorless hubs like in EMU Units and
#    parking filament behind load switch instead of at the hub. This
#    is needed since PTFE path per lane can differ when loading to
#    toolhead.
#
#    Caution: This is not fully implemented everywhere and has only
#    been tested with EMU units.
assisted_retract: False
#    Default: False
#    If true, retracts are assisted to prevent loose windings on the
#    spool.
move_dis: 65
#    Default: 65
#    Distance to move the filament within the hub in mm.
cut: False
#    Default: False
#    Set to true if using a hub cutter. An example of this would
#    be the `Snappy` hub cutter. 
cut_cmd: AFC
#    Default: AFC
#    Macro command to run when the cut command is sent. This should
#    be set to `AFC` if using the built in functionality. This is 
#    only required when `cut: True` is set.
cut_servo_name: cut
#    Default: cut
#    Name of servo to use for the hub cutter. This is only required
#    when `cut: True` is set. This requires a `[servo cut]` section
#    to be defined in a configuration file.
cut_dist: 50
#    Default: 50
#    How much filament (in mm) to cut when the cut command is sent.
#    This is only required when `cut: True` is set.
cut_clear: 120
#    Default: 120
#    How far the filament should retract (in mm) back from the hub.
#    This is only required when `cut: True` is set.
cut_min_length: 200
#    Default: 200
#    Minimum length of filament (in mm) to cut. This is only
#    required when `cut: True` is set.
cut_servo_pass_angle: 0 
#    Default: 0
#    Servo angle to align the bowden tube with the hole for loading
#    the toolhead. This is only required when `cut: True` is set.
cut_servo_clip_angle: 160
#    Default: 160
#    Servo angle for a completed cut movement. This is only required
#    when `cut: True` is set.
cut_servo_prep_angle: 75
#    Default: 75
#    Servo angle to prepare the filament for cutting. This should be 
#    set so that the bowden tube is NOT aligned with the hub exit. 
#    It should be clear so that filament can be pushed out of the hub
#    and the cutting mechanism can be engaged. This is only required
#    when `cut: True` is set.
cut_confirm: False
#    Default: False
#    If true, the cut command will execute twice. This is only
#    required when `cut: True` is set.
enable_sensors_in_gui: False
#    Default: False
#    Set to True to show hub sensor switch as a filament sensor in 
#    Mainsail/Fluidd gui, overrides value set in AFC.cfg.
debounce_delay: 0
#    Default: 0
#    A period of time in seconds to debounce hub switch prior to detecting
#    runout. If hub switch is pressed and released during this delay,
#    the entire switch event is ignored.
#
#    This value overrides value set in AFC config section
enable_hub_runout: True
#    Default: True
#    If enabled and hub sensor(s) detect filament not present while printing AFC will
#    pause printing. Inputting value here overrides global value in AFC.cfg file
```

## [AFC_led led_name] Section

The following options are available in the `[AFC_led led_name]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with the LED for the specific unit type. Multiple
LEDs may be defined in the configuration file.

``` cfg
[AFC_led AFC_Indicator]
pin: mcu:pin
#    MCU pin definition for LED indicator.
chain_count: 1
#    Number of LEDs in the chain.
color_order: GRB
#    Default: GRB
#    Color order of the LEDs in the chain. This is typically set to
#    a value like `GRB` or `GRBW` depending on the LED type. However,
#    if you are experiencing LED issues, ensure you validate that the 
#    proper color order is set. An incorrect value may result in 
#    sporadic behavior of the LEDs.
```

## [AFC_button lane_name] Section

The following options are available in the `[AFC_button lane_name]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when using optional hardware buttons to control various functionality
for each lane. Each lane may have its own button configuration, or you may have any combination of lanes configured.

``` cfg
[AFC_button lane1]
pin: 
#    Default: <none>
#    The pin that the switch is connected for the specified lane. 
#    This will typically look like `^!Turtle_1:SW6` or any valid
#    MCU/pin combination. Ensure that you include pull-up(^) and invert(!).
long_press_duration:
#    Default: 1.2
#    The duration in seconds that the button must be pressed to
#    trigger a long press event. This is used to differentiate between
#    a short press and a long press. 
```

More information on the configuration for this is available [here](../features.md).

## [AFC_BoxTurtle unit_name] Section

The following options are available in the `[AFC_BoxTurtle unit_name]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with the BoxTurtle unit type. This section is
typically used to define the unit name and other options that are specific to the BoxTurtle unit type.

``` cfg
[AFC_BoxTurtle Turtle_1]
hub: 
#    Default: <none>
#    Hub that this unit belongs to. Set to the name of an AFC_hub
#    section. This value can be overridden in the AFC_stepper/AFC_lane sections.
#
#    Two special values are also accepted:
#
#    direct       - Use when this lane has no hub and connects directly
#                   to the toolhead.
#    direct_load  - Same as direct, but AFC will automatically load
#                   filament to the toolhead when filament is inserted
#                   into the lane.
extruder:
#    Default: <none>
#    Extruder name(AFC_extruder) that belongs to this unit. can be
#    overridden in the [AFC_stepper] section.
buffer: 
#    Default: <none>
#    Buffer name(AFC_buffer) that belongs to this unit. can be
#    overridden in the [AFC_stepper] section.
enable_assist: True
#    Default: True
#    Enables espooler print assist, overrides setting in AFC.cfg file
#    Can be overridden in the [AFC_stepper] section.
enable_assist_weight: 500
#    Default: 500
#    Number in grams to activate espooler print assist once spool weight is 
#    less than this number.
#    Can be overridden in the [AFC_stepper] sections.
tool_max_unload_attempts: 4
#    Default: 4
#    Max number of attempts to unload filament from toolhead when using 
#    buffer as ramming sensor.
#    Setting variable here overrides value set in AFC config section. This
#    variable can be overridden in [AFC_stepper/AFC_lane] config sections.
timer_delay: 5
#    Default: 5
#    Affects espooler assist, number of seconds to wait before 
#    checking filament movement for espooler print assist
#    Can be overridden in the [AFC_stepper] section.
enable_kick_start: True
#    Default: True
#    Enables full speed espooler's for `kick_start_time` amount to
#    help spools to start moving
#    Can be overridden in the [AFC_stepper] section.
kick_start_time: 0.070
#    Default: 0.070
#    Time in seconds to enable spooler at full speed to help with
#    getting the spool to spin
#    Can be overridden in the [AFC_stepper] section.
delta_movement: 150
#    Default: 150
#    Affects espooler assist, delta amount in mm to move from last 
#    assist to trigger another assist move
#    Can be overridden in the [AFC_stepper] section.
spoolrate: 1.0
#    Default: 1.0
#    Scaling factor for the following variables: 
#      kick_start_time, spool_outer_diameter, delta_movement
#    Can be overridden in the [AFC_stepper] section.
spool_ratio: 2
#    Default: 2
#    Gear ratio for printed gearbox between N20 and spooler wheels, setting value
#    here will override value in AFC.cfg. The value also can be overridden in
#    [AFC_stepper/AFC_lane] sections.
full_weight: 1000
#    Default: 1000
#    Full starting weight of filament spool (not including spool), setting value
#    here will override value in AFC.cfg. The value also can be overridden in
#    [AFC_stepper/AFC_lane] sections.
espool_rot_dist: 132.9
#    Default: 132.9
#    Rotation distance estimation of espooler wheels, setting value
#    here will override value in AFC.cfg. The value also can be overridden in
#    [AFC_stepper/AFC_lane] sections.
led_fault: 1,0,0,0
#    Default: 1,0,0,0
#    LED color used when AFC flags a fault on the lane (jam, sensor
#    mismatch, failed load/unload, etc) and the lane needs attention.
#    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here 
#    overrides values set in AFC.cfg file.
led_ready: 1,1,0,0
#    Default: 1,1,0,0
#    LED color used once filament is staged and ready behind the hub.
#    Also used for the lane's own LED after a tool unload; see
#    led_tool_unloaded below for the separate toolhead LED color used
#    at that point. Overridden by the filament's color when
#    led_use_filament_color is enabled below.
#    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here
#    overrides values set in AFC.cfg file.
led_not_ready: 1,1,0,0
#    Default: 1,1,0,0
#    LED color used when the lane is empty (no spool prepped). Also
#    reused when a lane is unloaded back out of the hub but a spool is
#    still prepped in the lane.
#    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here
#    overrides values set in AFC.cfg file.
led_loading: 1,0,0,0
#    Default: 1,0,0,0
#    LED color used while filament is actively feeding into the lane,
#    either loading to the toolhead or loading a spool into the lane.
#    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here
#    overrides values set in AFC.cfg file.
led_unloading: 1,1,.5,0
#    Default: 1,1,.5,0
#    LED color used while filament is backing out of the lane, either
#    unloading from the toolhead or ejecting the lane.
#    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here
#    overrides values set in AFC.cfg file.
led_tool_loaded: 1,1,0,0
#    Default: 1,1,0,0
#    LED color used on the lane's LED (and its toolhead status LED, if
#    `status_led_idx` is set on the extruder) while this lane is the
#    active/loaded tool. Not affected by led_use_filament_color.
#    (R,G,B,W) 0 = off, 1 = full brightness. Setting value here
#    overrides values set in AFC.cfg file.
led_tool_loaded_idle: 0.4,0.4,0,0
#    Default: 0.4,0.4,0,0
#    LED color used on the lane's LED and its toolhead status LED
#    when this lane's tool is loaded but idle/parked, e.g. waiting
#    between tool changes on a toolchanger. Overridden by the
#    filament's color when led_use_filament_color is enabled below.
#    Format: (R,G,B,W) where 0 = off and 1 = full brightness.
#
#    Setting value here overrides values set in AFC.cfg file.
led_tool_unloaded: 1,0,0,0
#    Default: 1,0,0,0
#    Toolhead status LED color (`status_led_idx` on the extruder)
#    shown when this lane's tool has just been unloaded. Does not
#    affect the lane's own LED, which instead reverts to led_ready
#    (or the filament color, if led_use_filament_color is enabled).
#    Format: (R,G,B,W) where 0 = off and 1 = full brightness.
#
#    Setting value here overrides values set in AFC.cfg file.
led_use_filament_color: False
#    Default: False
#    When True, the lane's LED (and, where noted above, the toolhead
#    status LED) uses the filament color from the spool color field
#    (set manually or synced from Spoolman) instead of the configured
#    led_ready/led_tool_loaded_idle colors. Other states (fault,
#    loading, unloading, not ready, tool loaded) always use their
#    configured color regardless of this setting.
#
#    Setting value here overrides values set in AFC.cfg file.
long_moves_speed: 100
#    Default: 100
#    Speed in mm/s to move filament when doing long moves.
#    Setting value here overrides values set in AFC.cfg file.
rev_long_moves_speed_factor: 1.0
#    Default: 1.0
#    Range: 0.5 to 1.2
#    Scalar factor multiplied to long_moves_speed when rewinding filament.
#    Useful when reversing is done using non-assisted systems, e.g. filamentalist
long_moves_accel: 400
#    Default: 400
#    Acceleration in mm/s squared when doing long moves.
#    Setting value here overrides values set in AFC.cfg file.
short_moves_speed: 25
#    Default: 25
#    Speed in mm/s to move filament when doing short moves.
#    Setting value here overrides values set in AFC.cfg file.
short_moves_accel: 400
#    Default: 400
#    Acceleration in mm/s squared when doing short moves.
#    Setting value here overrides values set in AFC.cfg file.
short_move_dis: 400
#    Default: 400
#    Move distance in mm for failsafe moves. Setting value
#    here overrides values set in AFC.cfg file.
max_move_dis: 99999
#    Default: 99999
#    Maximum distance to move filament. AFC breaks filament moves over
#    this number into multiple moves. Useful to lower this number if
#    running into timer too close errors when doing long filament moves.
#    Setting value here overrides values set in AFC.cfg file.
n20_break_delay_time: 0.200
#    Default: 0.200
#    Time to wait between braking N20 motors(nSleep/FWD/RWD all 1)
#    and then releasing the brake to allow coasting. Setting value
#    here overrides values set in AFC.cfg file.
assisted_unload: False
#    Default: False
#    If True, the unload retract is assisted to prevent loose windings,
#    especially on full spools. This can prevent loops from slipping off the
#    spool. Setting value here overrides values set in AFC.cfg file.
unload_on_runout: False
#    Default: False
#    When True, AFC will unload lane and then pause when runout is 
#    triggered and spool to swap is not set (infinite spool). Setting
#    value here overrides values set in AFC.cfg file.
capture_td1_when_loaded: False
#    Default: False
#    When set to True and TD-1 device is correctly configured in moonraker, 
#    when filament is loaded into a lane AFC will move filament to TD-1 device
#    to capture TD and color.
#
#    Setting value here overrides values set in AFC.cfg file. This value can
#    also be overridden per AFC_lane/AFC_stepper.
td1_device_id: None
#    Default: None
#    Set this value to TD-1 device ID to use for a unit, this is only needed if
#    using multiple TD-1 devices.
#
#    If using separate TD-1 devices per lanes this value should be set in 
#    AFC_lane/AFC_stepper sections.
remember_spool: False
#    Default: False
#    If true, AFC will retain values (spoolID, weight, color, material) of the last
#    spool that was ejected from a lane and will reuse those values the next time
#    the given lane is loaded.
#
#    This variable can be overridden per AFC_lane/AFC_stepper config sections.
homing_overshoot: 50
#    Default: 50
#    Additional amount to add to all homing moves to guarantee the move will hit
#    endstops when homing.
#
#    This variable can be overridden per AFC_lane/AFC_stepper config sections.
homing_delta: 300
#    Default: 300
#    This value is only valid when homing is enabled and is used as another verifier
#    to make sure filament is within this range from commanded homing movement. If
#    homing move is not within this delta of commanded movement then AFC will try 
#    to home again to verify that filament is at correct position.
#
#    This variable can be overridden per AFC_lane/AFC_stepper config sections.
load_then_home: True
#    Default: True
#    When set to True and utilizing the buffer in ramming mode, AFC will first do a normal
#    move toward the toolhead where the total distance is:
#       - afc_bowden_length - load_undershoot, or
#       - dist_hub - load_undershoot (when using a direct hub).
#    After this initial move to the toolhead, AFC will then do a homing move the rest of
#    the way until the buffer advance sensor is triggered.
#
#    This variable can be overridden per AFC_lane/AFC_stepper config sections.
load_undershoot: 20
#    Default: 20
#    Amount to subtract from afc_bowden_length (or from dist_hub when using a direct hub)
#    when load_then_home is enabled.
#
#    This variable can be overridden per AFC_lane/AFC_stepper config sections.
extruder_clear_dis: 50
#    Default: 50
#    This variable affects additional move distance when ejecting filament and
#    filament has passed load sensor. AFC will move this additional amount to
#    make sure filament is not longer in extruder gears.
#    This variable can be overridden per AFC_lane/AFC_stepper config sections.
enable_buffer_tool_check: False
#    Default: False
#    When enabled AFC will verify that a lane is loaded to toolhead when using
#    buffer as a ramming sensor. AFC will try to expand out buffer to hit the 
#    advance sensor, if advance sensor is triggered then AFC know that lane
#    is loaded to toolhead. If this check fails AFC will notify that it could
#    not verify that lane is loaded and give instructions on how to fix.
#
#    Currently this is only intended to work with buffers that have springs
#    or rubber bands between PTFE tubes. Do not enable this if your buffer is
#    not setup this way.
```

## [AFC_NightOwl unit_name] Section

The following options are available in the `[AFC_NightOwl unit_name]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with the NightOwl unit type. This section is
typically used to define the unit name and other options that are specific to the NightOwl unit type.
 

AFC_NightOwl inherits configuration options from AFC_BoxTurtle configuration section, below are additional configuration values
for a NightOwl unit.  

``` cfg
[AFC_NightOwl NightOwl_1]
```

## [AFC_QuattroBox unit_name] Section

The following options are available in the `[AFC_QuattroBox unit_name]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with the AFC_QuattroBox unit type. This section is
typically used to define the unit name and other options that are specific to the AFC_QuattroBox unit type.  

AFC_QuattroBox inherits configuration options from AFC_BoxTurtle configuration section, below are additional configuration values
for a QuattroBox unit.  
``` cfg
[AFC_QuattroBox QuattroBox_1]
led_logo_index:
#    Default: None
#    Led index for led in chain that illuminates a logo. This should reference 
#    a defined LED in the [AFC_Indicator] section.
#
#    Index can have multiple entries in a comma separated list and range values
#    also are allowed
#    eg. AFC_Indicator_4:1,2,3,4, 6-9, 11-14, 16-18
led_logo_color: '0,0,0,0'
#    Default: '0,0,0,0'
#    Color to apply to logo. Logo also changes to led_logo_loading color when 
#    loading a lane and then once a lane is loaded the logo changes to the lanes 
#    spool color. When nothing is loaded this variable(led_logo_color) is 
#    applied to logo.
led_logo_loading: 1,0,0,0
#    Default: 1,0,0,0
#    Color to apply to logo when loading a lane, default values defaults to 
#    led_loading variable
led_spool_index:
#    Default: None
#    Led index for led in chain that illuminates spool. This should reference 
#    a defined LED in the [AFC_Indicator] section.
#    Can be overridden per lane in AFC_stepper section.
#
#    Index can have multiple entries in a comma separated list and range values
#    also are allowed
#    eg. AFC_Indicator_4:1,2,3,4, 6-9, 11-14, 16-18
led_spool_illuminate: 1,1,1,0
#    Default: 1,1,1,0
#    Loading color to illuminate spool, currently only for QuattroBox units and
#    can be overridden in AFC_stepper section.
```

## [AFC_HTLF unit_name] Section

The following options are available in the `[AFC_HTLF unit_name]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with the AFC_HTLF unit type. This section is
typically used to define the unit name and other options that are specific to the AFC_HTLF unit type. 

The following macros are specific to HTLF:  
- [AFC_HOME_UNIT](../klipper/internal/misc.md#AFC_HTLF.AFC_HTLF.cmd_AFC_HOME_UNIT)  
- [AFC_SELECT_LANE](../klipper/internal/lane.md#AFC_unit.afcUnit.cmd_AFC_SELECT_LANE)

AFC_HTLF inherits configuration options from AFC_BoxTurtle configuration section, below are additional configuration values
for a HTLF unit.  
``` cfg

[AFC_HTLF HTLF_1]
drive_stepper:
#    Name of AFC_stepper for drive motor.
selector_stepper:
#    Name of AFC_stepper for selector motor.
mm_move_per_rotation: 32
#    Default: 32
#    How many millimeters move the selector pulley one full rotation.
cam_angle:
#    Cam lobe angle thats being used on HTLF. Valid options: 30,45,60 (recommend using 60)
home_pin:
#    Pin for homing sensor.
MAX_ANGLE_MOVEMENT: 215
#    Default: 215
#    Max angle to move lobes, this is when lobe 1 is fully engaged with its lane.
enable_sensors_in_gui: True
#    Default: True
#    Set to True to show prep and load sensors switches as filament sensors 
#    in Mainsail/Fluidd gui, overrides value set in AFC.cfg.
selector_movement_speed: 50
#    Default: 50
#    Speed in mm/s to move filament when selecting lanes with selector motor.
selector_movement_accel: 50
#    Default: 50
#    Acceleration in mm/s squared when selecting lanes with selector motor.
```

## [AFC_vivid unit_name] Section

The following options are available in the `[AFC_vivid unit_name]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with the AFC_vivid unit type. This section is
typically used to define the unit name and other options that are specific to the AFC_vivid unit type. 

The following macros are specific to ViViD:  
- [AFC_SELECT_LANE](../klipper/internal/lane.md#AFC_unit.afcUnit.cmd_AFC_SELECT_LANE)  
- [AFC_RECOVER_LANE](../klipper/internal/lane.md#AFC_lane.AFCLane.cmd_AFC_RECOVER_LANE)  
- [AFC_UNSELECT_LANE](../klipper/internal/lane.md#AFC_unit.afcUnit.cmd_AFC_UNSELECT_LANE)

AFC_vivid inherits configuration options from AFC_BoxTurtle configuration section, below are additional configuration values
for a ViViD unit.  
``` cfg

[AFC_vivid Vivid_1]
drive_stepper:
#    Name of AFC_stepper for drive motor.
selector_stepper:
#    Name of AFC_stepper for selector motor.
enable_sensors_in_gui: True
#    Default: True
#    Set to True to show prep and load sensors switches as filament sensors 
#    in Mainsail/Fluidd gui, overrides value set in AFC.cfg.
selector_movement_speed: 150
#    Default: 150
#    Speed in mm/s to move filament when selecting lanes with selector motor.
selector_movement_accel: 150
#    Default: 150
#    Acceleration in mm/s squared when selecting lanes with selector motor.
max_selector_movement: 800
#    Default: 800
#    Max movement in mm to try to move selector to specified lane.
```

## [AFC_EMU unit_name] Section

The following options are available in the `[AFC_EMU unit_name]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with the EMU unit type. This section is
typically used to define the unit name and other options that are specific to the EMU unit type.
 

AFC_EMU inherits configuration options from AFC_BoxTurtle configuration section, below are additional configuration values
for a EMU unit.  

``` cfg
[AFC_EMU EMU_1]
```

### Notes about default EMU config setup
EMU default configuration is setup to run with a sensorless hub, what this means is that filament is parked behind the load sensor instead of at the hub.
Verify that your hub configuration section has `switch_pin: virtual` and `use_dist_hub: True` set. Setting switch pin as virtual means that all your load
sensors become the hub sensor, so once one load sensor is triggered then AFC will think that your hub has filament in the path to your toolhead. The `use_dist_hub`
variable is needed to tell AFC to use the `dist_hub` variable from each lane instead of `afc_bowden_length` since distance from load switch to toolhead can
differ per lane.

To switch to a hub that uses a sensor, remove `use_dist_hub: True` and set your hubs switch_pin correctly in your AFC_hub configuration section. Then make sure you
update `dist_hub` in each `AFC_stepper` section to be around what the PTFE length is between the load sensor and hub sensor. Make sure you run calibration as this
will then be calibrated to the correct distance.

## [AFC_Claymore unit_name] Section

HTLF2-Claymore logic is setup to inherit the same settings and macros as an AFC_vivid unit, please refer to the [AFC_vivid](#afc_vivid-unit_name-section) section for macros and configs that are specific to a HTLF2-Claymore unit.

## [AFC_OAMS unit_name] Section

The following options are available in the `[AFC_OAMS unit_name]` section of the `AFC_UnitType_1.cfg` file. This
section configures the OpenAMS hardware controller: the MCU it is connected to, the pressure (FPS) and Hall-effect
sensor thresholds/calibration, PTFE length, PID gains, and load/unload retry behavior. One `[AFC_OAMS <unit_name>]`
section is required per OpenAMS hardware unit and is referenced by name from a corresponding
[AFC_OpenAMS](#afc_openams-unit_name-section) section.

``` cfg
[AFC_OAMS OAMS1]
mcu: mcu_name
#    Default: mcu
#    Name of the MCU that this OpenAMS hardware unit is connected to.
fps_upper_threshold: 0.7
#    This parameter must be provided. Above this FPS (filament pressure
#    sensor) reading, the hub motor runs at full speed.
fps_lower_threshold: 0.3
#    This parameter must be provided. Below this FPS reading, the hub
#    motor stops.
fps_is_reversed: False
#    This parameter must be provided. Move the FPS slide by hand and
#    watch the buffer state in Mainsail/Fluidd. If it shows the opposite
#    of what you're doing, set this to True. Should also match the
#    `reversed` value set in the corresponding `[AFC_buffer]` section.
f1s_hes_on: 0.1, 0.1, 0.1, 0.1
#    This parameter must be provided. Comma separated list of 4 values
#    (0.0-1.0) for how full each spool bay's first-stage Hall-effect
#    sensor needs to read for that bay to count as "loaded".
f1s_hes_is_above: False
#    This parameter must be provided. If True, a bay counts as empty
#    above f1s_hes_on instead of below it.
hub_hes_on: 0.825845, 0.8075905442237854, 0.837821, 0.813116
#    This parameter must be provided. Comma separated list of 4 hub
#    Hall-effect sensor calibration values. Calibrate with
#    `OAMS_CALIBRATE_HUB_HES`.
hub_hes_is_above: True
#    This parameter must be provided. Above these hub_hes_on values,
#    that bay's hub sensor is considered ON.
ptfe_length: 2820
#    This parameter must be provided. Distance in mm the OAMS feeds
#    filament before checking the FPS slide to know when to stop
#    loading. This value is critical and must match your machine's PTFE
#    tube length. Run `OAMS_CALIBRATE_PTFE_LENGTH` to automatically
#    calibrate this value.
oams_idx: 1
#    This parameter must be provided. This unit's number (1, 2, ...),
#    used to name filament groups (oams1, oams2, ...) and to
#    distinguish multiple AMS units in klippy.log.
kp: 6.0
#    Default: 6.0
#    Proportional gain for the FPS (pressure) PID loop.
ki: 0.0
#    Default: 0.0
#    Integral gain for the FPS (pressure) PID loop.
kd: 0.0
#    Default: 0.0
#    Derivative gain for the FPS (pressure) PID loop.
fps_target: 0.5
#    Default: 0.5
#    Range: fps_lower_threshold to fps_upper_threshold
#    Target FPS (pressure) value the PID loop tries to hold.
current_kp: 0.375
#    Default: 0.375
#    Proportional gain for the rewind current-sense PID loop.
current_ki: 0.0
#    Default: 0.0
#    Integral gain for the rewind current-sense PID loop.
current_kd: 0.0
#    Default: 0.0
#    Derivative gain for the rewind current-sense PID loop.
current_target: 0.3
#    Default: 0.3
#    Range: 0.1 to 0.4
#    Target motor current (0.0-1.0) used to control spool rewind speed.
load_retry_max: 3
#    Default: 3
#    Range: 1 to 5
#    How many times to retry a failed load.
unload_retry_max: 2
#    Default: 2
#    Range: 1 to 3
#    How many times to retry a failed unload.
retry_delay: 3.0
#    Default: 3.0
#    Delay in seconds to wait between retry attempts.
auto_unload_on_failed_load: True
#    Default: True
#    If True, automatically unload back to the AMS before retrying a
#    failed load.
load_stall_grace: 3.0
#    Default: 3.0
#    Seconds to wait after a load starts before stall detection begins,
#    to allow the spool to spin up.
load_stall_dwell: 5.0
#    Default: 5.0
#    Seconds the load encoder must stop advancing (after load_stall_grace
#    has elapsed) before the load is considered stalled and aborted. Set
#    to 0 to disable stall detection.
```

## [AFC_OpenAMS unit_name] Section

The following options are available in the `[AFC_OpenAMS unit_name]` section of the `AFC_UnitType_1.cfg` file. This
section defines the AFC unit that drives an OpenAMS hardware unit (configured above in
[AFC_OAMS](#afc_oams-unit_name-section)), including stuck spool auto-recovery, clog detection, and filament
engagement verification.

AFC_OpenAMS inherits configuration options from the AFC_BoxTurtle configuration section, below are additional
configuration values specific to an AFC_OpenAMS unit.

<!--
auto_spoolman_create: False
#    Default: False
#    If True, automatically creates matching spools in Spoolman on an
#    RFID scan for this unit's lanes.
-->

``` cfg
[AFC_OpenAMS AMS_1]
oams: OAMS1
#    Default: oams1
#    Name of the corresponding [AFC_OAMS <unit_name>] section that
#    provides the hardware for this unit.
stuck_spool_auto_recovery: False
#    Default: False
#    If True, AFC will automatically attempt to recover from a stuck
#    spool during a print (unload, then reload and resume) instead of
#    pausing for manual intervention.
stuck_spool_load_grace: 8.0
#    Default: 8.0
#    Seconds after a load starts to wait before stuck-spool detection
#    begins, to allow the spool to spin up.
stuck_spool_pressure_threshold: 0.08
#    Default: 0.08
#    FPS pressure value below which the spool is considered stuck during
#    a print.
clog_sensitivity: medium
#    Default: medium
#    Valid values: off, low, medium, high
#    Sensitivity of clog detection based on FPS/encoder monitoring.
engagement_length: 20.0
#    Default: 20.0
#    Distance in mm to extrude when verifying filament engagement after
#    a load. Can be overridden per-lane.
engagement_speed: 300.0
#    Default: 300.0
#    Speed in mm/min to extrude at when verifying filament engagement
#    after a load. Can be overridden per-lane.
defer_engagement: False
#    Default: False
#    If True, defers engagement verification instead of performing it
#    immediately after load.
```

## [servo tool_cut] Section

The following options are available for all units (BoxTurtle, HTLF, and NightOwl) and allow for the configuration and 
operation of a gantry mounted servo to assist in tool cutting. 

``` cfg
[servo tool_cut]
#--=================================================================================--#
######### Tip Cut Servo ###############################################################
#--=================================================================================--#
pin: YOUR_SERVO_PIN_HERE
#    This should be the MCU/pin combination for the servo pin.
minimum_pulse_width: 0.00053
#    Adjust this value until a '_CUTTER_SERVO POS=out' extends the pin 
#    fully without a buzzing sound.
maximum_pulse_width: 0.0023
#    Adjust this value until a '_CUTTER_SERVO POS=in' retracts the pin 
#    fully without a buzzing sound.
maximum_servo_angle: 180
#   Initial angle (in degrees) to set the servo to. The default is to
#   not send any signal at startup.

```

## [temperature_oams] / [temperature_sensor sensor_name] Section (OpenAMS)

If your unit has an OpenAMS HDC1080-based temperature/humidity sensor installed, it can be defined using the
`temperature_oams` module and sensor type. This sensor reports both temperature and humidity, and can optionally
report itself to Mainsail/Fluidd as an `aht3x` sensor for GUI compatibility.

!!! note

    Both sections shown below are required. The empty `[temperature_oams]` section loads the module, while the
    `[temperature_sensor <sensor_name>]` section with `sensor_type: temperature_oams` defines the actual sensor.

``` cfg
[temperature_oams]
#    This section must be present (it can be left empty) to load the
#    temperature_oams module.

[temperature_sensor sensor_name]
sensor_type: temperature_oams
#    This parameter must be provided and set to `temperature_oams`.
i2c_mcu: mcu_name
#    Default: mcu
#    Name of the MCU that the sensor's I2C bus is connected to.
i2c_bus: i2c_bus_name
#    Name of the hardware I2C bus the sensor is connected to. Either this
#    or i2c_software_scl_pin/i2c_software_sda_pin should be provided.
i2c_software_scl_pin: mcu:pin
#    MCU pin to use for a software emulated I2C clock (SCL) line. This is
#    an alternative to i2c_bus for boards without a dedicated hardware
#    I2C bus. Both i2c_software_scl_pin and i2c_software_sda_pin must be
#    provided together.
i2c_software_sda_pin: mcu:pin
#    MCU pin to use for a software emulated I2C data (SDA) line. This is
#    an alternative to i2c_bus for boards without a dedicated hardware
#    I2C bus. Both i2c_software_scl_pin and i2c_software_sda_pin must be
#    provided together.
i2c_address: 64
#    Default: 64
#    I2C address of the HDC1080 sensor. Decimal 64 (0x40) is the default
#    HDC1080 address.
i2c_speed: 100000
#    Default: 100000
#    Speed of the I2C bus, in Hz. This is only used when i2c_bus is
#    configured for a hardware I2C bus.
min_temp: 0
#    This parameter must be provided. Minimum allowed temperature in
#    degrees Celsius. If a reading falls outside of the min_temp/max_temp
#    range, a printer shutdown will be triggered.
max_temp: 100
#    This parameter must be provided. Maximum allowed temperature in
#    degrees Celsius. If a reading falls outside of the min_temp/max_temp
#    range, a printer shutdown will be triggered.
report_time: 5
#    Default: 5
#    Minimum: 5
#    Interval in seconds between sensor readings.
temp_resolution: 14
#    Default: 14
#    Valid values: 11, 14
#    Temperature measurement resolution in bits. Higher resolution
#    increases sensor conversion time.
humidity_resolution: 14
#    Default: 14
#    Valid values: 8, 11, 14
#    Humidity measurement resolution in bits. Higher resolution
#    increases sensor conversion time.
temp_offset: 0.0
#    Default: 0.0
#    Offset in degrees Celsius applied to each temperature reading.
#    Useful to calibrate against a known reference temperature.
humidity_offset: 0.0
#    Default: 0.0
#    Offset in percent applied to each humidity reading. Useful to
#    calibrate against a known reference humidity.
heater_enabled: False
#    Default: False
#    If True, enables the HDC1080's internal heater. This can be used to
#    drive off condensation in humid environments.
simulate_supported_sensor_mainsail: True
#    Default: True
#    If True, the sensor also registers itself as an `aht3x` printer
#    object so that Mainsail/Fluidd, which do not natively recognize
#    `temperature_oams`, will display it in the GUI as a supported
#    sensor type.
```

## [AFC_ACE unit_name] Section

The following options are available in the `[AFC_ACE unit_name]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with an Anycubic ACE Pro (first generation,
"V1") unit. The ACE is not an MCU; Klipper talks to its firmware over the ACE's own USB serial port and the ACE
moves filament itself, so there are no pins or steppers in this section. Its lanes are plain `[AFC_lane]` sections.

Like other units, `[AFC_ACE]` also accepts the common unit-level options documented in the
[AFC_BoxTurtle](#afc_boxturtle-unit_name-section) section above (`hub`, `extruder`, `buffer`, etc.); only options
specific to `AFC_ACE` are listed below.

``` cfg
[AFC_ACE Ace_1]
serial_port: auto
#    Default: <none>
#    Serial port the ACE is connected on. Set to `auto` to have AFC find the
#    ACE automatically by the id its firmware reports (see `ace_index`
#    below), which keeps working when the ACE renumbers its own `ttyACM`
#    port. Otherwise set this to a fixed port, e.g. a `/dev/serial/by-id/...`
#    path (a single ACE) or a `/dev/serial/by-path/...` path (chained ACEs,
#    which all share the same by-id name).
ace_index: 1
#    Default: 1   Minimum: 1
#    Which ACE in cable order this section is (the firmware's get_info id,
#    1 = first ACE in the chain). Only used when `serial_port: auto`;
#    harmless otherwise.
mode: combined
#    Default: combined
#    Valid values: combined, direct
#
#    combined - All four lanes feed ONE toolhead through one shared hub.
#    direct   - Each lane feeds its OWN extruder, e.g. one lane per tool on
#               a toolchanger. Each lane then sets its own `hub` and
#               `extruder` (see the hub and lane sections).
max_feed_speed: 100.0
#    Default: 100.0   Minimum: 1.0
#    Ceiling in mm/s for `feed_speed` and `retract_speed`. Stock ACE
#    firmware rejects speeds above 100; raise this only on a unit flashed
#    with the AFC speed-patched firmware.
feed_speed: 100.0
#    Default: 100.0
#    Speed in mm/s to feed filament. Capped at `max_feed_speed`.
retract_speed: 100.0
#    Default: 100.0
#    Speed in mm/s to retract filament. Capped at `max_feed_speed`.
prep_ready_timeout: 90.0
#    Default: 90.0   Minimum: 1.0
#    Maximum seconds to wait for the unit's own load-to-toolhead-and-back
#    cycle to finish. Scales with PTFE/hub distance.
feed_departure_timeout: 3.0
#    Default: 3.0   Minimum: 0.5
#    How long to wait for a feed/unwind move to start moving before
#    treating it as a no-start. Slower units or long PTFE runs want a
#    longer window.
load_retry_pulse: 100.0
#    Default: 100.0   Minimum: 1.0
#    Post-feed recovery pulse length in mm, used to nudge filament onto the
#    toolhead sensor if the main feed under-shot it.
load_retry_timeout: 10.0
#    Default: 10.0   Minimum: 1.0
#    Maximum seconds to spend on the `load_retry_pulse` recovery nudge.
load_approach_length: 30.0
#    Default: 30.0   Minimum: 0.0
#    Final approach distance in mm run at `load_approach_speed` and stopped
#    on the toolhead sensor, so rollers don't slip and grind a divot. Set
#    to 0 to feed the whole way at `feed_speed` instead.
load_approach_speed: 50.0
#    Default: 50.0   Minimum: 1.0
#    Speed in mm/s used for the final `load_approach_length` approach move.
eject_buffer: 475.0
#    Default: 475.0   Minimum: 0.0
#    Extra distance in mm (beyond `dist_hub`) to retract when ejecting a
#    hub-staged lane, so filament clears the hub and pulls fully back into
#    the unit.
max_dryer_temperature: 55.0
#    Default: 55.0   Minimum: 0.0
#    Maximum temperature in Celsius `AFC_ACE_DRY` is allowed to heat to.
#    Set to 0 to hide this unit's dryer from the `afc_dryer` panel.
dry_rotate_length: 150.0
#    Default: 150.0   Minimum: 10.0   Maximum: 1000.0
#    With `AFC_ACE_DRY ROTATE=1`, mm of slow rollback per turn used to spin
#    an empty lane's spool (without feeding filament) every
#    `dry_rotate_interval` seconds while drying.
dry_rotate_speed: 15.0
#    Default: 15.0   Minimum: 5.0   Maximum: 50.0
#    Speed in mm/s used for the `dry_rotate_length` rollback move.
dry_rotate_interval: 300.0
#    Default: 300.0   Minimum: 30.0
#    Seconds between `dry_rotate_length` rollback moves while drying with
#    `ROTATE=1`.
load_to_hub: True
#    Default: True
#    When set (True/False), overrides every lane's own `load_to_hub` value
#    for this unit. Leave unset to let each lane control this individually.
use_feed_assist: True
#    Default: True
#    Whether feed assist is enabled by default for this unit's lanes.
assist_watchdog: True
#    Default: True
#    When True, AFC checks on every idle heartbeat that the lane on the
#    active tool actually has its feed assist running, in case a pickup or
#    tool-change event didn't enable it.
stuck_spool_detection: False
#    Default: False
#    When True, AFC pauses the print if the firmware's continuous feed-assist
#    run time passes `stuck_time` while printing, which usually means the
#    spool is tangled or snagged. Toggle live with
#    `AFC_ACE_STUCK_SPOOL_DETECTION`.
stuck_time: 4.0
#    Default: 4.0   Minimum: 2.0
#    Seconds of continuous feed assist with no break before
#    `stuck_spool_detection` pauses the print.
calibration_step: 50.0
#    Default: 50.0
#    Step size in mm used by `AFC_ACE_CALIBRATE`/`AFC_ACE_CALIBRATE_HUB`
#    while probing for the toolhead/hub sensor.
baud_rate: 115200
#    Default: 115200
#    Serial baud rate used to talk to the ACE.
```

## [AFC_ACE2 unit_name] Section

The following options are available in the `[AFC_ACE2 unit_name]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with an Anycubic ACE 2 Pro unit. Like the
ACE Pro, the ACE 2 is not an MCU, has no pins or steppers in this section, and uses plain `[AFC_lane]` sections.

`AFC_ACE2` inherits all configuration options from the [AFC_ACE](#afc_ace-unit_name-section) section above; only
options that are new, or whose default differs, for the ACE 2 Pro are listed below.

``` cfg
[AFC_ACE2 Ace2_1]
ace_uid:
#    Default: <none>
#    Optional per-box STM32 UID, as `uid1,uid2,uid3` (copied from the
#    "ACE2 autodetect: <port> -> uid (...)" line in AFC_ACE_serial.log,
#    without the parentheses). Only used with `serial_port: auto`; ties
#    this section to one specific box regardless of which USB port it is
#    on. When omitted, `ace_index` instead picks the unit by USB-topology
#    (cable) order.
baud_rate: 230400
#    Default: 230400
#    Serial baud rate used to talk to the ACE 2 Pro.
max_dryer_temperature: 70.0
#    Default: 70.0   Minimum: 0.0
#    Maximum temperature in Celsius `AFC_ACE_DRY` is allowed to heat to.
feed_check_length: 200
#    Default: 200   Minimum: 3   Maximum: 254
#    Encoder feed-check target in mm (ACE 2's `SET_FEED_CHECK`). At
#    `feed_error_length` mm fed, the encoder must read at least this many mm,
#    otherwise the feed is flagged as slipping. Must stay below
#    `feed_error_length * 1.2342` (the encoder's maximum possible reading),
#    or every feed will fail the check. Lower this to widen the slip
#    tolerance; 3 effectively disables the check.
feed_error_length: 185
#    Default: 185   Minimum: 3   Maximum: 254
#    Commanded feed length in mm used by the `feed_check_length` slip check.
stuck_spool_detection: True
#    Default: True
#    When True, AFC pauses the print when the ACE 2's encoder reports that
#    the active lane's spool is not turning with the motor (stuck, tangled,
#    slipping, or a motor fault). Toggle live with
#    `AFC_ACE_STUCK_SPOOL_DETECTION`.
skip_factory_autostage: True
#    Default: True
#    When True, turns off the unit's own (slower) factory insert/identify
#    cycle on every slot; AFC instead stages a fresh insert itself,
#    `stage_initial_dist` mm into the unit plus `dist_hub`.
stage_initial_dist: 330.0
#    Default: 330.0   Minimum: 0.0
#    Distance in mm AFC feeds into the unit (plus `dist_hub`) to stage a
#    fresh insert when `skip_factory_autostage` is enabled. Ignored (treated
#    as 0) when `skip_factory_autostage` is False.
eject_buffer: 475.0
#    Default: 475.0   Minimum: 0.0
#    Extra distance in mm (beyond `dist_hub`) to retract when ejecting a
#    hub-staged lane.
```

## [AFC_CFS unit_name] Section

The following options are available in the `[AFC_CFS unit_name]` section of the `AFC_UnitType_1.cfg` file. These
options control the configuration of the AFC system when interfacing with a Creality CFS box. The CFS is not an MCU;
Klipper talks to the box's own firmware over RS-485 (through the CH340 USB adapter in the CFS kit's cable) and the box
moves filament itself, so there are no pins or steppers in this section. Its lanes are plain `[AFC_lane]` sections
with `unit: <unit_name>:<1-4>` for slots A-D, left to right, and no `prep` or `load` pins; the box reports filament
presence itself.

Every box's tube goes to its own input on the CFS 4-in-1 buffer, which is AFC's hub for every box: each
`[AFC_CFS]` section uses the same `hub`, with `[AFC_hub]` set to `switch_pin: virtual` and `afc_bowden_length: 0`.
The box feeds until the toolhead sensor triggers, so there is no hub or bowden distance to calibrate; only
`tool_stn` in `[AFC_extruder]` needs measuring.

Like other units, `[AFC_CFS]` also accepts the common unit-level options documented in the
[AFC_BoxTurtle](#afc_boxturtle-unit_name-section) section above (`hub`, `extruder`, etc.); only options specific to
`AFC_CFS` are listed below.

``` cfg
[AFC_CFS CFS_1]
serial: /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
#    Default: <none>
#    Serial port of the CFS cable's USB adapter. Run `ls /dev/serial/by-id/`
#    and use the `1a86` device. Every box on a chain uses the same `serial`.
baud: 230400
#    Default: 230400
#    RS-485 speed. The box's firmware uses 230400; there is no need to change it.
box_addr: 1
#    Default: 1   Minimum: 1   Maximum: 4
#    Address of this box on the chain. Each box on a chain needs its own.
box_uid:
#    Default: <none>
#    The box's 12-byte ID, as 24 hex digits (`AFC_CFS_STATUS` shows it).
#    Required when two or more boxes share a chain; Klipper refuses the config
#    otherwise. Without it the boxes would be addressed in the order they
#    answer, which can change between power-ups and swap your lanes. A single
#    box may leave it unset.
cutter_sensor_pin:
#    Default: <none>
#    Your toolhead cutter's switch (the CFS toolhead kit has one), if it is
#    wired to a Klipper pin. When set, a load refuses to start with the
#    blade down and every cut is verified. Set it on one box only; the other
#    boxes on the chain use it.
cutter_require_trigger: True
#    Default: True
#    The cutter switch closes only near the end of the blade's travel. When
#    True, a cut must make it fire: a cut where it never fires is retried,
#    then the unload stops before retracting possibly uncut filament. When
#    False, AFC only checks that the switch released after the cut.
cutter_wait_time: 3.0
#    Default: 3.0   Minimum: 0.1
#    Seconds to wait for the cutter switch to release after a cut.
load_attempts: 2
#    Default: 2   Minimum: 1
#    Tries per load before giving up and pausing.
prime1_length: 10.0
#    Default: 10.0
#    Extruder push in mm while the box hands the filament over (before the
#    box's stage 6).
prime1_speed: 300.0
#    Default: 300.0
#    Speed of `prime1_length` in mm/min. Creality's firmware uses 35; the box
#    shows the slot red from the moment the filament reaches the toolhead
#    until this push ends, so at 35 it stays red for about 17 seconds.
prime2_length: 5.0
#    Default: 5.0
#    Second extruder push in mm, before the box's stage 7.
prime2_speed: 600.0
#    Default: 600.0
#    Speed of `prime2_length` in mm/min. Creality's firmware uses 10, which
#    adds about 30 seconds to every load. If loads fail at the hand-off
#    ("toolhead sensor cleared after the handoff"), set `prime1_speed: 35` and
#    `prime2_speed: 10` to go back to Creality's speeds.
stage5_timeout: 60.0
#    Default: 60.0   Minimum: 5.0
#    Maximum seconds for the box to feed from the spool to the toolhead sensor.
stage5_stall_time: 8.0
#    Default: 8.0   Minimum: 1.0
#    Stop the feed early if the box's measuring wheel moves less than
#    `stage5_stall_distance` mm in this many seconds.
stage5_stall_distance: 2.0
#    Default: 2.0   Minimum: 0.0
#    Measuring-wheel travel in mm that counts as moving for `stage5_stall_time`.
unload_retract_steps: 12:300
#    Default: 12:300
#    Extruder retract after the box starts pulling the filament back, as
#    comma-separated `mm:mm/min` steps. Retract only far enough to clear the
#    extruder gears: the cutter-to-gears distance plus about 2 mm (12 mm with
#    the CFS toolhead kit). The box doesn't reel during this retract and its buffer only
#    takes up slack toward the toolhead, so a longer retract makes the
#    extruder gears click. If the toolhead sensor is still triggered after
#    the reel-back, add a few mm.
unload_tip_wiggle: False
#    Default: False
#    Run Creality's tip-forming wiggle before the retract. Not needed with a
#    cutter.
unload_timeout: 30.0
#    Default: 30.0   Minimum: 1.0
#    Seconds to wait for the toolhead sensor to clear after the reel-back.
poll_interval: 1.0
#    Default: 1.0   Minimum: 0.2
#    Seconds between status polls of the box (filament presence, buffer).
heartbeat_interval: 1.5
#    Default: 1.5   Minimum: 0.5
#    Seconds between keep-alive checks, like Creality's firmware. Without
#    them the box shows its red "no printer" light.
heartbeat_interval_printing: 10.0
#    Default: 10.0   Minimum: 0.5
#    The same while printing.
request_timeout: 0.8
#    Default: 0.8   Minimum: 0.05
#    Seconds to wait for the reply to a quick command.
stage_timeout: 30.0
#    Default: 30.0   Minimum: 1.0
#    Seconds to wait for a motor stage. The box answers when the motion ends;
#    a reel-back takes about 12 seconds.
checkin_timeout: 5.0
#    Default: 5.0   Minimum: 0.2
#    Seconds to wait for the box's check-ins during an unload.
resync_timeout: 60.0
#    Default: 60.0   Minimum: 1.0
#    After a motor command times out, how long to wait for its late reply
#    before sending anything else.
offline_after_failures: 3
#    Default: 3   Minimum: 1
#    Missed replies in a row before the box is marked offline.
reconnect_interval: 5.0
#    Default: 5.0   Minimum: 0.5
#    Seconds between reconnect attempts to an offline box.
boot_wait: 20.0
#    Default: 20.0   Minimum: 0.0
#    Seconds PREP waits for a box that is still powering up.
rfid_read: False
#    Default: False
#    Read a spool's Creality-format RFID tag once when it goes into a slot,
#    and set the lane's color, material and weight with `SET_COLOR`,
#    `SET_MATERIAL` and `SET_WEIGHT`, or its Spoolman spool with
#    `SET_SPOOL_ID` if the tag carries a spool ID and AFC has Spoolman. A lane
#    that already has a Spoolman spool is left alone. Nothing is read during
#    a print or while a slot of the box is loaded to the toolhead. On a slot
#    with no tag the box searches for up to about 90 seconds and the other
#    boxes on the chain wait meanwhile, so leave this off if you mostly use
#    untagged spools.
rfid_insert_delay: 10.0
#    Default: 10.0   Minimum: 0.0
#    Seconds after a spool is detected before reading its tag, so the box
#    finishes drawing it in.
rfid_read_at_startup: False
#    Default: False
#    Also read every full slot once after Klipper starts, for spools swapped
#    while the printer was off.
rfid_startup_delay: 15.0
#    Default: 15.0   Minimum: 0.0
#    Seconds after Klipper starts before `rfid_read_at_startup` reads.
rfid_spin: True
#    Default: True
#    Turn the slot while reading, like Creality's firmware, so the tag passes
#    the reader.
rfid_read_attempts: 3
#    Default: 3   Minimum: 1
#    Tries per tag read.
rfid_retry_interval: 5.0
#    Default: 5.0   Minimum: 0.0
#    Seconds between tag read tries.
rfid_busy_timeout: 150.0
#    Default: 150.0   Minimum: 5.0
#    Maximum seconds to wait for the box to finish a tag read.
rfid_materials:
#    Default: <none>
#    Extra Creality material codes, as `<5 digits>=<MATERIAL>, ...`
#    (e.g. `00003=PETG`). Known codes: `00001` Generic PLA, `00002` Generic
#    PLA-Silk, `01001` Hyper PLA. The console names any other code a tag
#    carries.
trace_file:
#    Default: <none>
#    Record everything the unit sends to and gets from the box, with load and
#    unload timings, to this file (JSON lines). Attach it when reporting a
#    problem. Off unless set.
trace_max_size: 5.0
#    Default: 5.0   Minimum: 0.1
#    Size in MB at which the trace file rotates.
trace_backups: 1
#    Default: 1   Minimum: 0   Maximum: 10
#    Rotated trace files to keep.
trace_polls: False
#    Default: False
#    Also record the idle status polls in the trace (a much bigger file).
protocol_debug: False
#    Default: False
#    Log every frame sent to and received from the box to `AFC.log`.
```

For more boxes on the same chain, add one `[AFC_CFS]` section per box, all with the same `serial` and `hub`, each
with its own `box_addr` and `box_uid`. Box 2's lanes are `lane5`-`lane8` with `unit: CFS_2:1` to `CFS_2:4`:

``` cfg
[AFC_CFS CFS_2]
serial: /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0   # same as CFS_1
box_addr: 2
box_uid: <box 2's uid>
hub: CFS_1
extruder: extruder

[AFC_lane lane5]
unit: CFS_2:1
# ... lane6-lane8 with unit: CFS_2:2 to CFS_2:4
```
