
# Minimal Klipper + AFC configuration with one BoxTurtle unit and lane.
# Extends the base configuration by adding a simulated AFC_BoxTurtle unit,
# one AFC_stepper lane, and one AFC_hub — all using free STM32H723 pins
# not consumed by the base printer kinematics (PA*/PB* are already used).
#
# Free pin bank used here: PC0–PC5
#
# This file duplicates the base printer/AFC config so that klippy can load it
# as a standalone config without needing an include directive.

# ---------------------------------------------------------------------------
# MCU — path is irrelevant in dict/simulation mode
# ---------------------------------------------------------------------------
[mcu]
serial: /dev/null

# ---------------------------------------------------------------------------
# Printer kinematics (required by Klipper)
# ---------------------------------------------------------------------------
[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000
max_z_velocity: 5
max_z_accel: 100

[stepper_x]
step_pin: PA0
dir_pin: PA1
enable_pin: !PA2
microsteps: 16
rotation_distance: 40
endstop_pin: PA3
position_endstop: 0
position_max: 200
homing_speed: 50

[stepper_y]
step_pin: PA4
dir_pin: PA5
enable_pin: !PA6
microsteps: 16
rotation_distance: 40
endstop_pin: PA7
position_endstop: 0
position_max: 200
homing_speed: 50

[stepper_z]
step_pin: PB0
dir_pin: PB1
enable_pin: !PB2
microsteps: 16
rotation_distance: 8
endstop_pin: PB3
position_endstop: 0
position_max: 200

[extruder]
step_pin: PB4
dir_pin: PB5
enable_pin: !PB6
microsteps: 16
rotation_distance: 33.5
nozzle_diameter: 0.400
filament_diameter: 1.750
heater_pin: PB7
sensor_type: temperature_host
min_temp: 0
max_temp: 250
control: pid
pid_Kp: 22.2
pid_Ki: 1.08
pid_Kd: 114

[heater_bed]
heater_pin: PB8
sensor_type: temperature_host
min_temp: 0
max_temp: 130
control: pid
pid_Kp: 54.027
pid_Ki: 0.770
pid_Kd: 948.182

# save_variables is required by AFC to persist spool/lane state.
[save_variables]
filename: /tmp/afc_klippy_lane_test.cfg

# pause_resume is required by AFC_error
[pause_resume]

# ---------------------------------------------------------------------------
# AFC core
# ---------------------------------------------------------------------------

[testing]

[AFC]
VarFile: /tmp/afc_klippy_lane_test.var
long_moves_speed: 150
long_moves_accel: 250
short_moves_speed: 50
short_moves_accel: 300
short_move_dis: 10
enable_sensors_in_gui: False
load_to_hub: False
z_hop: 5
resume_speed: 120
resume_z_speed: 30
error_timeout: 36000
tool_cut: False
park: False
poop: False
kick: False
wipe: False
form_tip: False

[AFC_prep]
enable: False

[AFC_form_tip]
cooling_tube_position: 35
cooling_tube_length: 10
cooling_moves: 4

# ---------------------------------------------------------------------------
# One BoxTurtle unit with a single lane — uses PC0–PC5 (all free)
# ---------------------------------------------------------------------------

# AFC extruder wrapper (required so AFC_stepper can link to an extruder object)
[AFC_extruder extruder]
# No tool_start / tool_end sensor pins — keep config minimal.

[AFC_BoxTurtle unit_1]
hub: hub_1
extruder: extruder

[AFC_hub hub_1]
switch_pin: PC5
afc_bowden_length: 900
move_dis: 75
cut: False

[AFC_stepper lane1]
unit: unit_1:1
step_pin: PC0
dir_pin: PC1
enable_pin: !PC2
microsteps: 16
rotation_distance: 4.65
dist_hub: 155.0
park_dist: 10
prep: PC3
load: PC4
