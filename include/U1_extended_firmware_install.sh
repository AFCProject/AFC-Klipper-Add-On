#!/usr/bin/env bash
# Automated Filament Changer Klipper-Add-On
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# This script is used for installing and performing actions on a Snapmaker U1.

set -e

# Checking for root since this script will be running as root from extended
# firmware firmware-config page
if [ $EUID -eq 0 ]; then
    HOME="/home/lava"
fi
afc_dir="${HOME}/AFC-Klipper-Add-On"

source_files() {
    source ${afc_dir}/include/constants.sh
    source ${afc_dir}/include/update_commands.sh
    source ${afc_dir}/include/install_functions.sh
}

u1_restart_klipper() {
    if [ $EUID -ne 0 ]; then
        su root -c "/etc/init.d/S60klipper restart"
    else
        /etc/init.d/S60klipper restart
    fi
}

# For Snapmaker U1 printers need to add /oem/.debug or changes in klipper folder will
# be reset on reboot/power cycle
u1_write_debug_file() {
    # Use su if passed in
    if [ $EUID -ne 0 ]; then
        echo "Writing .debug file to /oem/ directory"
        su root -c "touch /oem/.debug"
    elif [ ! -f "/oem/.debug" ]; then
        echo Adding /oem/.debug file
        touch /oem/.debug
    fi
}

check_and_move_lite_files() {
    if [ ! -f "$klipper_extra_path/AFC.py.lite" ]; then
        echo "Copying AFC.py to AFC.py.lite"
        cp $klipper_extra_path/AFC.py $klipper_extra_path/AFC.py.lite
    fi

    if [ ! -f "$klipper_extra_path/AFC_unit.py.lite" ]; then
        echo "Copying AFC_unit.py to AFC.py.lite"
        cp $klipper_extra_path/AFC_unit.py $klipper_extra_path/AFC_unit.py.lite
    fi

    if [ ! -f "$klipper_extra_path/AFC_lane.py.lite" ]; then
        echo "Copying AFC_lane.py to AFC.py.lite"
        cp $klipper_extra_path/AFC_lane.py $klipper_extra_path/AFC_lane.py.lite
    fi
}

move_lite_files_back() {
    if [ -f "$klipper_extra_path/AFC.py.lite" ]; then
        mv $klipper_extra_path/AFC.py.lite $klipper_extra_path/AFC.py
    fi
    if [ -f "$klipper_extra_path/AFC_unit.py.lite" ]; then
        mv $klipper_extra_path/AFC_unit.py.lite $klipper_extra_path/AFC_unit.py
    fi
    if [ -f "$klipper_extra_path/AFC_lane.py.lite" ]; then
        mv $klipper_extra_path/AFC_lane.py.lite $klipper_extra_path/AFC_lane.py
    fi
}

check_and_move_afc_files() {
    if [ ! -d "${printer_config_dir}/AFC" ]; then
        echo "Making AFC directories in printer_data/config directory"
        mkdir -p ${printer_config_dir}/AFC
        mkdir -p ${printer_config_dir}/AFC/macros

        echo "Copying AFC config files to printer_data/config/AFC directory"
        cp ${afc_dir}/templates/u1_macros/AFC.cfg ${printer_config_dir}/AFC/
        cp ${afc_dir}/config/AFC_Macro_Vars.cfg ${printer_config_dir}/AFC/
        cp ${afc_dir}/templates/u1_macros/Snapmaker_macros.cfg ${printer_config_dir}/AFC/macros/Snapmaker_macros.cfg
        cp ${afc_dir}/templates/AFC_Hardware_U1.cfg ${printer_config_dir}/AFC/AFC_Hardware.cfg
    fi
}

comment_gcode_in_fluidd() {
    # Passing in True comments out gcode_macro T(n) lines,
    # passing in nothing uncomments them
    if [ "$1" == "comment" ]; then
        echo "Commenting out gcode_macro T(n) in fluidd.cfg file"
        sed -i '/^\[gcode_macro T[0-9]\+\][[:space:]]*$/,/SWITCH_OF_EXTENDED_EXTRUDER INDEX/ s/^/# /' "$printer_config_dir/fluidd.cfg"
    else
        echo "Uncommenting gcode_macro T(n) lines in fluidd.cfg file"
        sed -i '/^\# \[gcode_macro T[0-9]\+\]/,/SWITCH_OF_EXTENDED_EXTRUDER INDEX/ s/^# //' "$printer_config_dir/fluidd.cfg"
    fi

}

enable() {
    source_files
    echo "Enabling AFC"
    u1_write_debug_file

    check_and_move_lite_files

    check_and_move_afc_files

    chown -R lava:lava ${printer_config_dir}/AFC

    comment_gcode_in_fluidd "comment"

    echo "Adding AFC include to printer.cfg file"
    manage_include "${printer_config_dir}/printer.cfg" "add"

    echo "Symlinking AFC files to klipper extras folder"
    link_extensions

    echo "AFC enabled. Restarting Klipper..."
    u1_restart_klipper
}

disable() {
    source_files
    echo "Disabling AFC"

    echo "Removing AFC include from printer.cfg file"
    manage_include "${printer_config_dir}/printer.cfg" "remove"

    echo "Removing AFC symlinks in klipper extras folder"
    unlink_extensions

    echo "Moving AFC lite files back"
    move_lite_files_back

    comment_gcode_in_fluidd
    
    echo "AFC disabled. Restarting Klipper..."
    u1_restart_klipper
}

while getopts "ed" opt
do
    case "${opt}" in
    e) enable ;;
    d) disable ;;
    *) exit 1 ;;
    esac
done