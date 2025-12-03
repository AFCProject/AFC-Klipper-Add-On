#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.


# Path related constants
printer_config_dir="$HOME/printer_data/config"
klipper_dir="$HOME/klipper"
afc_path="$HOME/AFC-Klipper-Add-On"
afc_config_dir="${printer_config_dir}/AFC"
afc_file="$afc_config_dir/AFC.cfg"
moonraker_config_file="$printer_config_dir/moonraker.conf"
klipper_venv="$HOME/klippy-env/bin"

# Service related constants
klipper_service="klipper"
moonraker_port="7125"
moonraker_address="http://localhost"
moonraker="${moonraker_address}:${moonraker_port}"


# Git related constants
gitrepo="https://github.com/ArmoredTurtle/AFC-Klipper-Add-On.git"
branch="main"

# Misc constants
prior_installation="False"
installation_type="BoxTurtle (4-Lane)"
uninstall="False"
backup_date=$(date +%Y%m%d%H%M%S)
files_updated_or_installed="False"
test_mode="False"
installation_options=("BoxTurtle (4-Lane)" "BoxTurtle (8-Lane)" "NightOwl" "HTLF" "QuattroBox")
invalid_name="False"
minimum_python_major="3"
minimum_python_minor="8"

# AFC default configs
park_macro="True"
poop_macro="True"
afc_includes="True"
tip_forming="False"
toolhead_cutter="True"
hub_cutter="False"
kick_macro="True"
wipe_macro="True"
toolhead_sensor="Sensor"
toolhead_sensor_pin="Unknown"
buffer_type="TurtleNeck"
boxturtle_name="Turtle_1"
htlf_board_types=("ERB" "MMB_1.1" "MMB_1.0")
htlf_board_type="ERB"
qb_board_types=("MMB_1.0" "MMB_1.1" "MMB_2.0")
qb_board_type="MMB_2.0"
qb_motor_types=("NEMA_17" "NEMA_14")
qb_motor_type="NEMA_14"

# Moonraker Config
moonraker_update_config="""
[update_manager afc-software]
type: git_repo
path: ~/AFC-Klipper-Add-On
origin: $gitrepo
managed_services: $klipper_service
primary_branch: main
is_system_service: False
info_tags:
    desc=AFC Klipper Add On
"""
