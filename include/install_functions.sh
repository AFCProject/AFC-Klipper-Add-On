#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

check_dirs() {
  # Debugging: Check if the directory exists
  if [ ! -d "${afc_path}/include/" ]; then
    echo "Directory ${afc_path}/include/ does not exist."
    exit 1
  fi

  # Debugging: Check if there are any files in the directory
  if [ -z "$(ls -A "${afc_path}/include/")" ]; then
    echo "No files found in ${afc_path}/include/"
    exit 1
  fi
}

link_extensions() {
  # Function to link AFC extensions to Klipper.
  # Uses the global variables:
  #   - KLIPPER_DIR: The path to the Klipper installation.
  #   - AFC_PATH: The path to the AFC Klipper Add-On repository.
  local message

  if [ -d "${klipper_dir}/klippy/extras" ]; then
    for extension in "${afc_path}"/extras/*.py; do
      ln -sf "${afc_path}/extras/$(basename "${extension}")" "${klipper_dir}/klippy/extras/$(basename "${extension}")"
    done
  else
    export message="AFC Klipper extensions not installed; Klipper extras directory not found."
  fi
}

unlink_extensions() {
  # Function to unlink AFC extensions from Klipper.
  # Uses the global variables:
  #   - KLIPPER_PATH: The path to the Klipper installation.
  #   - AFC_PATH: The path to the AFC Klipper Add-On repository.
  if [ -d "${klipper_dir}/klippy/extras" ]; then
    for extension in "${afc_path}"/extras/*.py; do
      rm -f "${klipper_dir}/klippy/extras/$(basename "${extension}")"
    done
  else
    print_msg ERROR "AFC Klipper extensions not uninstalled; Klipper extras directory not found."
    exit 1
  fi
}

template_unit_files() {
  local input_file="$1"
  local output_file="$2"

  case "${installation_type}" in
    "HTLF") MCU="${htlf_board_type}" ;;
    "BoxTurtle (4-Lane)") MCU="AFC" ;;
    "NightOwl") MCU="ERB" ;;
    *) MCU="UNKNOWN" ;;  # Optional: fallback
  esac

  export INSTALL_TYPE="${installation_type}"
  export MCU

  envsubst < "${input_file}" > "${output_file}"
}

copy_unit_files() {
  case "$installation_type" in
  "BoxTurtle (4-Lane)")
    cp "${afc_path}/config/mcu/AFC_Lite.cfg" "${afc_config_dir}/mcu/AFC_Lite.cfg"
    cp "${afc_path}/templates/AFC_Hardware-AFC.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    cp "${afc_path}/templates/AFC_Turtle_1.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
    ;;

  "BoxTurtle (8-Lane)")
    cp "${afc_path}/config/mcu/AFC_Pro.cfg" "${afc_config_dir}/mcu/AFC_Pro.cfg"
    cp "${afc_path}/templates/AFC_Hardware-AFC.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    cp "${afc_path}/templates/AFC_Pro_Turtle_1.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
    ;;

  "NightOwl")
    cp "${afc_path}/config/mcu/ERB_2.0.cfg" "${afc_config_dir}/mcu/ERB_2.0.cfg"
    cp "${afc_path}/templates/AFC_Hardware-NightOwl.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    cp "${afc_path}/templates/AFC_NightOwl_1.cfg" "${afc_config_dir}/AFC_NightOwl_1.cfg"
    ;;

  "HTLF")
    [[ "$htlf_board_type" == "MMB_1.0" || "$htlf_board_type" == "MMB_1.1" ]] && htlf_board_type="MMB"
    cp "${afc_path}/config/mcu/HTLF_ERB.cfg" "${afc_path}/config/mcu/HTLF_MMB_1.0.cfg" "${afc_path}/config/mcu/HTLF_MMB_1.1.cfg" "${afc_config_dir}/mcu/"
    cp "${afc_path}/templates/AFC_HTLF_1-${htlf_board_type}.cfg" "${afc_config_dir}/AFC_${htlf_board_type}_${boxturtle_name}.cfg"
    cp "${afc_path}/templates/AFC_Hardware-HTLF.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    ;;

  "QuattroBox")
    cp "${afc_path}/templates/AFC_Hardware-QuattroBox.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    cp "${afc_path}/templates/qb_macros/Eject_buttons.cfg" "${afc_config_dir}/macros/Eject_buttons.cfg"
    if [ "${qb_motor_type}" == "NEMA_14" ]; then
      cp "${afc_path}/templates/AFC_QuattroBox_14.cfg" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      if [ "${qb_board_type}" == "MMB_1.0" ]; then
        cp "${afc_path}/config/mcu/MMB_1.0_QB.cfg" "${afc_config_dir}/mcu/"
        sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.0_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      elif [ "${qb_board_type}" == "MMB_1.1" ]; then
        cp "${afc_path}/config/mcu/MMB_1.1_QB.cfg" "${afc_config_dir}/mcu/"
        sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.1_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      elif [ "${qb_board_type}" == "MMB_2.0" ]; then
        cp "${afc_path}/config/mcu/MMB_2.0_QB.cfg" "${afc_config_dir}/mcu/"
        sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_2.0_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      fi
    elif [ "${qb_motor_type}" == "NEMA_17" ]; then
      cp "${afc_path}/templates/AFC_QuattroBox_17.cfg" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      if [ "${qb_board_type}" == "MMB_1.0" ]; then
        sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.0_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      elif [ "${qb_board_type}" == "MMB_1.1" ]; then
        sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.1_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      elif [ "${qb_board_type}" == "MMB_2.0" ]; then
        sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_2.0_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      fi
    fi
    ;;
esac
}

install_afc() {
  # Link the python extensions
  link_extensions
  copy_config
  copy_unit_files
  # Add our extensions to the klipper gitignore
  if [ "$test_mode" != "True" ]; then
    exclude_from_klipper_git
  fi
  # Include the AFC configuration files if selected
  if [ "$afc_includes" == True ]; then
    manage_include "${printer_config_dir}/printer.cfg" "add"
  fi
  # Update selected configuration values
  update_config_value "${afc_file}" "park" "${park_macro}"
  update_config_value "${afc_file}" "poop" "${poop_macro}"
  update_config_value "${afc_file}" "form_tip" "${tip_forming}"
  update_config_value "${afc_file}" "tool_cut" "${toolhead_cutter}"
  update_config_value "${afc_file}" "hub_cut" "${hub_cutter}"
  update_config_value "${afc_file}" "kick" "${kick_macro}"
  update_config_value "${afc_file}" "wipe" "${wipe_macro}"

  if [ "$toolhead_sensor" == "Sensor" ]; then
    update_switch_pin "${afc_config_dir}/AFC_Hardware.cfg" "${toolhead_sensor_pin}"
  elif [ "$toolhead_sensor" == "Ramming" ]; then
    update_switch_pin "${afc_config_dir}/AFC_Hardware.cfg" "buffer"
  fi

  # When using Boxturtle as Installation Type then insert selected buffer configuration
  # NightOwl uses Turtleneck as default for now
  if [ "$installation_type" == "BoxTurtle (4-Lane)" ] || [ "$installation_type" == "BoxTurtle (8-Lane)" ]; then
    # Make sure the unit name is correct per the user choice
    if [ "$boxturtle_name" != "Turtle_1" ]; then
      find "$afc_config_dir" -type f -exec sed -i "s/Turtle_1/$boxturtle_name/g" {} +
    fi
    if [ "$buffer_type" == "TurtleNeck" ]; then
      query_tn_pins "TN"
      append_buffer_config "TurtleNeck" "$tn_advance_pin" "$tn_trailing_pin"
      add_buffer_to_extruder "${afc_config_dir}/AFC_${boxturtle_name}.cfg" "${boxturtle_name}"
    elif [ "$buffer_type" == "TurtleNeckV2" ]; then
      append_buffer_config "TurtleNeckV2"
      add_buffer_to_extruder "${afc_config_dir}/AFC_${boxturtle_name}.cfg" "${boxturtle_name}"
    fi
  fi
  check_and_append_prep "${afc_config_dir}/AFC.cfg"
  replace_varfile_path "${afc_config_dir}/AFC.cfg"
  update_moonraker_config

  export message
  export files_updated_or_installed="True"

  # Final step should be displaying any messages and exit cleanly.
  message="""
- AFC Configuration updated with selected options at ${afc_file}

- AFC-Klipper-Add-On python extensions installed to ${klipper_dir}/klippy/extras/
"""

if [ "$installation_type" == "BoxTurtle (4-Lane)" ] || [ "$installation_type" == "BoxTurtle (8-Lane)" ]; then
  message+="""
- Ensure you enter either your CAN bus or serial information in the ${afc_config_dir}/AFC_${boxturtle_name}.cfg file
  """
elif [ "$installation_type" == "NightOwl" ]; then
  message+="""
- Ensure you enter either your CAN bus or serial information in the ${afc_config_dir}/AFC_NightOwl_1.cfg file
  """
elif [ "$installation_type" == "HTLF" ]; then
  message+="""
- Ensure you enter either your CAN bus or serial information in the ${afc_config_dir}/AFC_${htlf_board_type}_${boxturtle_name}_1.cfg file.
- Ensure you modify the ${afc_config_dir}/AFC_${htlf_board_type}_${boxturtle_name}_1.cfg file to select the proper rotation distance
  and gear ratio for your stepper motors.
- Ensure you update any necessary buffer information in the ${afc_config_dir}/AFC_Hardware.cfg file
  """
elif [ "$installation_type" == "QuattroBox" ]; then
  message+="""
- You must update the ${afc_config_dir}/AFC_Hardware.cfg file to reference the proper buffer configuration and pins.

- Ensure you enter either your CAN bus or serial information in the ${afc_config_dir}/AFC_QuattroBox_1.cfg file
  """
fi

if [ "$buffer_type" == "TurtleNeckV2" ]; then
  message+="""
- Ensure you add the correct serial information to the ${afc_config_dir}/mcu/TurtleNeckv2.cfg file
  """
fi

message+="""
You may now quit the script or return to the main menu.

If you would like to add any additional units, please restart to script to ensure the
current units are loaded correctly.
"""

}