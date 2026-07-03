#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.


generate_emu_config() {
  local unit_name="$1"
  local num_lanes="$2"
  local board_type="${emu_board_type:-EBB}"
  local output_file="${afc_config_dir}/AFC_${unit_name}.cfg"
  local mcu_output_file="${afc_config_dir}/mcu/${unit_name}.cfg"

  mkdir -p "${afc_config_dir}/mcu"

  # Build comma-separated MCU list for board_pins
  local mcu_list=""
  for ((i=1; i<=num_lanes; i++)); do
    if [ "$i" -eq 1 ]; then
      mcu_list="${unit_name}_lane${i}"
    else
      mcu_list="${mcu_list}, ${unit_name}_lane${i}"
    fi
  done

  # Write MCU board_pins file (pin aliases differ between EBB and SLB)
  if [ "$board_type" == "SLB" ]; then
    cat > "${mcu_output_file}" <<EOF
[board_pins ${unit_name}]
mcu: ${mcu_list}
aliases:
    MOT_UART=PB6,
    MOT_STEP=PB5,
    MOT_DIR=PB4,
    MOT_EN=PB7,
    MOT_DIAG=,

    RGB_button=PA4,
    RGB_box=PA2,

    TRG=PA1,
    LOAD=PA0,

    BUFFER_ADV=PB1,
    BUFFER_TRL=PB0,

    TH=PA3,

    FAN=PA15,

    BUTTON=PB2,

    TEMP_SW_SCL=PB10,
    TEMP_SW_SCA=PB11,
    TEMP_HW_I2C=i2c2_PB10_PB11
EOF
  else
    cat > "${mcu_output_file}" <<EOF
[board_pins ${unit_name}]
mcu: ${mcu_list}
aliases:
    MOT_UART=PA15,
    MOT_STEP=PD0,
    MOT_DIR=PD1,
    MOT_EN=PD2,
    MOT_DIAG=,

    RGB=PD3,

    TRG=PB7,
    LOAD=PB5,

    BUFFER_ADV=PB8,
    BUFFER_TRL=PB9,

    TH=PA3,

    FAN=PA0,

    BUTTON=PB6,

    TEMP_SCL=PB3,
    TEMP_SCA=PB4
EOF
  fi

  # Write unit config header
  cat > "${output_file}" <<EOF
[include mcu/${unit_name}.cfg]

EOF

  # Write per-lane MCU sections
  for ((i=1; i<=num_lanes; i++)); do
    cat >> "${output_file}" <<EOF
[mcu ${unit_name}_lane${i}]
canbus_uuid: <replace with your can UUID>
#canbus_interface: can1         # Uncomment if using a CAN bus other than can0
#serial: <replace with your /dev/serial/by-id/...> and comment out canbus_uuid above

EOF
  done

  # Write AFC_EMU unit section
  cat >> "${output_file}" <<EOF
[AFC_EMU ${unit_name}]
hub: ${unit_name}_HUB
extruder: extruder
buffer: ${unit_name}_buffer
# The following speeds below are conservative, speeds have been tested up to 500mm/s, recommended
# running speeds at 400mm/s or less.
long_moves_speed: 250
long_moves_accel: 120
#long_moves_speed: 300
#long_moves_accel: 300

EOF

  # Write per-lane AFC_stepper, tmc, button, temp, fan, and led sections
  for ((i=1; i<=num_lanes; i++)); do
    if [ "$board_type" == "SLB" ]; then
      cat >> "${output_file}" <<EOF
[AFC_stepper lane${i}]
unit: ${unit_name}:${i}
step_pin: ${unit_name}_lane${i}:MOT_STEP
dir_pin: ${unit_name}_lane${i}:MOT_DIR
enable_pin: !${unit_name}_lane${i}:MOT_EN
microsteps: 4
rotation_distance: 22.7574
gear_ratio: 1:1
dist_hub: 2000
led_index: ${unit_name}_Indicator_Button_${i}:1-4
led_spool_index: ${unit_name}_Indicator_Box_${i}:1
# If using EBB board comment out the above led_index and led_spool_index and
# uncomment the indexes below. Then comment out both [AFC_led ${unit_name}_Indicator_Button_${i}]
# and [AFC_led ${unit_name}_Indicator_Box_${i}] configuration sections and uncomment
# [AFC_led ${unit_name}_Indicator_${i}].
#led_index: ${unit_name}_Indicator_${i}:1
#led_spool_index: ${unit_name}_Indicator_${i}:2
prep: ^!${unit_name}_lane${i}:TRG
load: ^${unit_name}_lane${i}:LOAD
print_current: 0.4

[tmc2209 AFC_stepper lane${i}]
uart_pin: ${unit_name}_lane${i}:MOT_UART
uart_address: 0
run_current: 0.8
sense_resistor: 0.110

[AFC_button lane${i}]
pin: ^!${unit_name}_lane${i}:BUTTON

[temperature_sensor lane${i}]
sensor_type: BME280
i2c_address: 118
# If using AHT20 sensor, comment out sensor_type and i2c_address above and uncomment
# the lines below
#sensor_type: AHT2X
#i2c_address: 56
i2c_mcu: ${unit_name}_lane${i}
i2c_software_scl_pin: ${unit_name}_lane${i}:TEMP_SW_SCL
i2c_software_sda_pin: ${unit_name}_lane${i}:TEMP_SW_SCA
# If using hardware I2C instead of software I2C, comment out software SCL/SDA pins
# and uncomment i2c_bus below
#i2c_bus: i2c2_PB10_PB11

[temperature_sensor ${unit_name}_mcu_lane${i}]
sensor_type: temperature_mcu
sensor_mcu: ${unit_name}_lane${i}

[controller_fan ${unit_name}_fan_lane${i}]
pin: ${unit_name}_lane${i}:FAN
max_power: 1
kick_start_time: 0.5
stepper: AFC_stepper lane${i}

# Based on your configuration, LED configuration must be selected from the below:

############### EBB + LED PCB ###############

#[AFC_led ${unit_name}_Indicator_${i}]
#pin: ${unit_name}_lane${i}:RGB
#chain_count: 5
#color_order: GRBW
#initial_RED: 0.0
#initial_GREEN: 0.0
#initial_BLUE: 0.0
#initial_WHITE: 0.0

############### EBB + neopixel eject buttons ###############

#[AFC_led ${unit_name}_Indicator_${i}]
#pin: ${unit_name}_lane${i}:RGB
#chain_count: 2
#color_order: GRBW
#initial_RED: 0.0
#initial_GREEN: 0.0
#initial_BLUE: 0.0
#initial_WHITE: 0.0

############### SLB + LED PCB ###############

[AFC_led ${unit_name}_Indicator_Button_${i}]
pin: ${unit_name}_lane${i}:RGB_button
chain_count: 4
color_order: GRBW
initial_RED: 0.0
initial_GREEN: 0.0
initial_BLUE: 0.0
initial_WHITE: 0.0

[AFC_led ${unit_name}_Indicator_Box_${i}]
pin: ${unit_name}_lane${i}:RGB_box
chain_count: 1
color_order: GRBW
initial_RED: 0.0
initial_GREEN: 0.0
initial_BLUE: 0.0
initial_WHITE: 0.0

EOF
    else
      cat >> "${output_file}" <<EOF
[AFC_stepper lane${i}]
unit: ${unit_name}:${i}
step_pin: ${unit_name}_lane${i}:MOT_STEP
dir_pin: ${unit_name}_lane${i}:MOT_DIR
enable_pin: !${unit_name}_lane${i}:MOT_EN
microsteps: 4
rotation_distance: 22.7574
gear_ratio: 1:1
dist_hub: 2000
led_index: ${unit_name}_Indicator_${i}:1
led_spool_index: ${unit_name}_Indicator_${i}:2
# If using SLB board comment out the above led_index and led_spool_index and
# uncomment the indexes below. Then comment out [AFC_led ${unit_name}_Indicator_${i}] configuration
# section and uncomment both [AFC_led ${unit_name}_Indicator_Button_${i}] and [AFC_led ${unit_name}_Indicator_Box_${i}]
# configuration sections.
#led_index: ${unit_name}_Indicator_Button_${i}:1-4
#led_spool_index: ${unit_name}_Indicator_Box_${i}:1
prep: ^!${unit_name}_lane${i}:TRG
load: ^${unit_name}_lane${i}:LOAD
print_current: 0.4

[tmc2209 AFC_stepper lane${i}]
uart_pin: ${unit_name}_lane${i}:MOT_UART
uart_address: 0
run_current: 0.8
sense_resistor: 0.110

[AFC_button lane${i}]
pin: ^!${unit_name}_lane${i}:BUTTON

[temperature_sensor lane${i}]
sensor_type: BME280
i2c_address: 118
# If using AHT20 sensor, comment out sensor_type and i2c_address above and uncomment
# the lines below
#sensor_type: AHT2X
#i2c_address: 56
i2c_mcu: ${unit_name}_lane${i}
i2c_software_scl_pin: ${unit_name}_lane${i}:TEMP_SCL
i2c_software_sda_pin: ${unit_name}_lane${i}:TEMP_SCA
# If using SLB board and would like to use hardware I2C, comment out software SCL/SDA pins
# and uncomment i2c_bus below
#i2c_bus: i2c2_PB10_PB11

[temperature_sensor ${unit_name}_mcu_lane${i}]
sensor_type: temperature_mcu
sensor_mcu: ${unit_name}_lane${i}

[controller_fan ${unit_name}_fan_lane${i}]
pin: ${unit_name}_lane${i}:FAN
max_power: 1
kick_start_time: 0.5
stepper: AFC_stepper lane${i}

# Based on your configuration, LED configuration must be selected from the below:

############### EBB + LED PCB ###############

#[AFC_led ${unit_name}_Indicator_${i}]
#pin: ${unit_name}_lane${i}:RGB
#chain_count: 5
#color_order: GRBW
#initial_RED: 0.0
#initial_GREEN: 0.0
#initial_BLUE: 0.0
#initial_WHITE: 0.0

############### SLB + LED PCB ###############

#[AFC_led ${unit_name}_Indicator_Button_${i}]
#pin: ${unit_name}_lane${i}:RGB_button
#chain_count: 4
#color_order: GRBW
#initial_RED: 0.0
#initial_GREEN: 0.0
#initial_BLUE: 0.0
#initial_WHITE: 0.0

#[AFC_led ${unit_name}_Indicator_Box_${i}]
#pin: ${unit_name}_lane${i}:RGB_box
#chain_count: 1
#color_order: GRBW
#initial_RED: 0.0
#initial_GREEN: 0.0
#initial_BLUE: 0.0
#initial_WHITE: 0.0

############### EBB + neopixel eject buttons ###############

[AFC_led ${unit_name}_Indicator_${i}]
pin: ${unit_name}_lane${i}:RGB
chain_count: 2
color_order: GRBW
initial_RED: 0.0
initial_GREEN: 0.0
initial_BLUE: 0.0
initial_WHITE: 0.0

EOF
    fi
  done

  # Write buffer section (uses lane1 for buffer advance/trailing pins)
  cat >> "${output_file}" <<EOF
[AFC_buffer ${unit_name}_buffer]
advance_pin: ^${unit_name}_lane1:BUFFER_ADV
trailing_pin: ^${unit_name}_lane1:BUFFER_TRL
multiplier_high: 1.15
multiplier_low: 0.90

# EMU hub defaults to virtual (no sensor). To use a physical hub sensor:
#  - Replace "virtual" with the actual sensor pin
#  - Update dist_hub in each AFC_stepper section to the PTFE length between
#    the load sensor and hub sensor
#  - Remove/comment out the \`use_dist_hub: True\` line
[AFC_hub ${unit_name}_HUB]
afc_bowden_length: 2000
hub_clear_move_dis: 5
switch_pin: virtual
use_dist_hub: True
EOF
}