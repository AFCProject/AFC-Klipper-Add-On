#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
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
      case $extension in
        # Excluding __init__.py from being linked into klipper folder
        *__init__.py) continue;;
        *) ln -sf "${afc_path}/extras/$(basename "${extension}")" "${klipper_dir}/klippy/extras/$(basename "${extension}")";;
      esac
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
      case $extension in
        # Excluding __init__.py files from being removed as this will make klipper dirty
        *__init__.py) continue;;
        *) rm -f "${klipper_dir}/klippy/extras/$(basename "${extension}")";;
      esac
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
    "Claymore") MCU="${htlf2_board_type}" ;;
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
  "ViViD")
    safe_copy "${afc_path}/templates/AFC_Vivid_1.cfg" "${afc_config_dir}/AFC_Vivid_1.cfg"
    safe_copy "${afc_path}/templates/AFC_Hardware-AFC.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    safe_copy "${afc_path}/config/mcu/Vivid.cfg" "${afc_config_dir}/mcu/Vivid_1.cfg"
    ;;
  "BoxTurtle (4-Lane)")
    safe_copy "${afc_path}/config/mcu/AFC_Lite.cfg" "${afc_config_dir}/mcu/AFC_Lite.cfg"
    safe_copy "${afc_path}/templates/AFC_Hardware-AFC.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    safe_copy "${afc_path}/templates/AFC_Turtle_1.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
    ;;

  "BoxTurtle (8-Lane)")
    safe_copy "${afc_path}/config/mcu/AFC_Pro.cfg" "${afc_config_dir}/mcu/AFC_Pro.cfg"
    safe_copy "${afc_path}/templates/AFC_Hardware-AFC.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    safe_copy "${afc_path}/templates/AFC_Pro_Turtle_1.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
    ;;

  "NightOwl")
    safe_copy "${afc_path}/config/mcu/ERB_2.0.cfg" "${afc_config_dir}/mcu/ERB_2.0.cfg"
    safe_copy "${afc_path}/templates/AFC_Hardware-NightOwl.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    safe_copy "${afc_path}/templates/AFC_NightOwl_1.cfg" "${afc_config_dir}/AFC_NightOwl_1.cfg"
    ;;

  "HTLF")
    local board_type="$htlf_board_type"
    safe_copy "${afc_path}/config/mcu/HTLF_${board_type}.cfg" "${afc_config_dir}/mcu/"
    [[ "$board_type" == "MMB_1.0" || "$board_type" == "MMB_1.1" ]] && board_type="MMB"
    safe_copy "${afc_path}/templates/AFC_HTLF_1-${board_type}.cfg" "${afc_config_dir}/AFC_${board_type}_${boxturtle_name}.cfg"
    safe_copy "${afc_path}/templates/AFC_Hardware-HTLF.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    ;;

  "Claymore")
    local board_type="$htlf2_board_type"
    boxturtle_name="Claymore_1"
    safe_copy "${afc_path}/config/mcu/AFC_Lite_Claymore.cfg" "${afc_config_dir}/mcu/"
    safe_copy "${afc_path}/templates/AFC_Claymore_1-${board_type}.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
    safe_copy "${afc_path}/templates/AFC_Hardware-HTLF.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    ;;

  "QuattroBox")
    safe_copy "${afc_path}/templates/AFC_Hardware-QuattroBox.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    safe_copy "${afc_path}/templates/qb_macros/Eject_buttons.cfg" "${afc_config_dir}/macros/Eject_buttons.cfg"
    if [ "${qb_motor_type}" == "NEMA_14" ]; then
      safe_copy "${afc_path}/templates/AFC_QuattroBox_14.cfg" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      if [ "${qb_board_type}" == "MMB_1.0" ]; then
        safe_copy "${afc_path}/config/mcu/MMB_1.0_QB.cfg" "${afc_config_dir}/mcu/"
        sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.0_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      elif [ "${qb_board_type}" == "MMB_1.1" ]; then
        safe_copy "${afc_path}/config/mcu/MMB_1.1_QB.cfg" "${afc_config_dir}/mcu/"
        sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.1_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      elif [ "${qb_board_type}" == "MMB_2.0" ]; then
        safe_copy "${afc_path}/config/mcu/MMB_2.0_QB.cfg" "${afc_config_dir}/mcu/"
        sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_2.0_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      fi
    elif [ "${qb_motor_type}" == "NEMA_17" ]; then
      safe_copy "${afc_path}/templates/AFC_QuattroBox_17.cfg" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      if [ "${qb_board_type}" == "MMB_1.0" ]; then
        safe_copy "${afc_path}/config/mcu/MMB_1.0_QB.cfg" "${afc_config_dir}/mcu/"
        sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.0_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      elif [ "${qb_board_type}" == "MMB_1.1" ]; then
        safe_copy "${afc_path}/config/mcu/MMB_1.1_QB.cfg" "${afc_config_dir}/mcu/"
        sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.1_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      elif [ "${qb_board_type}" == "MMB_2.0" ]; then
        safe_copy "${afc_path}/config/mcu/MMB_2.0_QB.cfg" "${afc_config_dir}/mcu/"
        sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_2.0_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
      fi
    fi
    ;;

  "OpenAMS")
    safe_copy "${afc_path}/templates/AFC_Hardware-AFC.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    safe_copy "${afc_path}/templates/AFC_AMS_1.cfg" "${afc_config_dir}/AFC_AMS_1.cfg"
    ;;

  "EMU")
    export boxturtle_name="EMU_1"
    safe_copy "${afc_path}/templates/AFC_Hardware-AFC.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
    generate_emu_config "$boxturtle_name" "$emu_num_lanes"
    ;;

esac
}



get_unit_buffer_target() {
  # Sets globals describing where a buffer selection should be applied for
  # the current $installation_type:
  #   buffer_unit_name           - the unit's section name, e.g. Turtle_1, HTLF_1, AMS_1
  #   buffer_unit_section_prefix - the unit's section prefix, e.g. AFC_BoxTurtle, AFC_HTLF
  #   buffer_extruder_file       - the config file containing that unit section
  #   buffer_section_name        - the name to give the [AFC_buffer <name>] section itself.
  #                                 For types who's template configs contains a buffer block, this MUST match
  #                                 the `buffer:` value already referenced in the unit section
  #                                 (e.g. `buffer: Vivid_1_buffer`), or that reference breaks.
  #   buffer_prebaked_header      - the exact "[AFC_buffer <name>]" header already
  #                                 present in that file's default template, or ""
  #                                 if the type has no pre-baked buffer section
  local board_type

  buffer_unit_name=""
  buffer_unit_section_prefix=""
  buffer_extruder_file=""
  buffer_section_name=""
  buffer_prebaked_header=""

  case "$installation_type" in
    "BoxTurtle (4-Lane)"|"BoxTurtle (8-Lane)")
      buffer_unit_name="$boxturtle_name"
      buffer_unit_section_prefix="AFC_BoxTurtle"
      buffer_extruder_file="${afc_config_dir}/AFC_${boxturtle_name}.cfg"
      buffer_section_name="$boxturtle_name"
      ;;
    "NightOwl")
      buffer_unit_name="NightOwl"
      buffer_unit_section_prefix="AFC_NightOwl"
      buffer_extruder_file="${afc_config_dir}/AFC_NightOwl_1.cfg"
      buffer_section_name="NightOwl"
      ;;
    "HTLF")
      board_type="$htlf_board_type"
      [[ "$board_type" == "MMB_1.0" || "$board_type" == "MMB_1.1" ]] && board_type="MMB"
      buffer_unit_name="HTLF_1"
      buffer_unit_section_prefix="AFC_HTLF"
      buffer_extruder_file="${afc_config_dir}/AFC_${board_type}_${boxturtle_name}.cfg"
      buffer_section_name="HTLF_1"
      ;;
    "Claymore")
      buffer_unit_name="Claymore_1"
      buffer_unit_section_prefix="AFC_Claymore"
      buffer_extruder_file="${afc_config_dir}/AFC_${boxturtle_name}.cfg"
      buffer_prebaked_header="[AFC_buffer Claymore_buffer]"
      buffer_section_name="Claymore_buffer"
      ;;
    "QuattroBox")
      buffer_unit_name="QuattroBox_1"
      buffer_unit_section_prefix="AFC_QuattroBox"
      buffer_extruder_file="${afc_config_dir}/AFC_QuattroBox_1.cfg"
      buffer_prebaked_header="[AFC_buffer QuattroBox_1]"
      buffer_section_name="QuattroBox_1"
      ;;
    "OpenAMS")
      buffer_unit_name="AMS_1"
      buffer_unit_section_prefix="AFC_OpenAMS"
      buffer_extruder_file="${afc_config_dir}/AFC_AMS_1.cfg"
      buffer_section_name="AMS_1"
      ;;
    # NOTE: ViViD is intentionally not included here. FPS_PSF buffers are
    # not currently supported by default for ViViD units. buffer_unit_name stays empty,
    # which triggers the "no unit target known" warning below and skips
    # applying FPS_PSF for this type.
    "EMU")
      buffer_unit_name="$boxturtle_name"
      buffer_unit_section_prefix="AFC_EMU"
      buffer_extruder_file="${afc_config_dir}/AFC_${boxturtle_name}.cfg"
      buffer_prebaked_header="[AFC_buffer ${boxturtle_name}_buffer]"
      buffer_section_name="${boxturtle_name}_buffer"
      ;;
  esac
}

install_afc() {
  # Link the python extensions
  if [ "$is_snapmaker" == "True" ]; then
    check_and_move_lite_files
    copy_snapmaker_config
    comment_gcode_in_fluidd "comment"
  elif [ "$installation_type" != "OpenAMS" ]; then
    copy_config
  else
    copy_openams_config
  fi
  link_extensions
  copy_unit_files
  # Add our extensions to the klipper gitignore
  if [ "$git_install" == "True" ]; then
    if [ "$test_mode" == "False" ]; then
      exclude_from_klipper_git
    fi
  else
    print_msg INFO "Skipping exclude from klipper git for git installations."
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
    if [ "$installation_type" != "OpenAMS" ]; then
      update_switch_pin "${afc_config_dir}/AFC_Hardware.cfg" "buffer"
    elif [ "$installation_type" == "OpenAMS" ]; then
      update_switch_pin "${afc_config_dir}/AFC_Hardware.cfg" "AMS_extruder"
    fi
  fi

  # Make sure the unit name is correct per the user choice
  if [ "$boxturtle_name" != "Turtle_1" ] && { [ "$installation_type" == "BoxTurtle (4-Lane)" ] || [ "$installation_type" == "BoxTurtle (8-Lane)" ]; }; then
    find "$afc_config_dir" -type f -exec sed -i "s/Turtle_1/$boxturtle_name/g" {} +
  fi

 
  if [ "$buffer_type" == "TurtleNeck" ] || [ "$buffer_type" == "TurtleNeckV2" ] || [ "$buffer_type" == "FPS_PSF" ]; then
    get_unit_buffer_target
    if [ -z "$buffer_unit_name" ]; then
      print_msg WARNING "PSF buffer selected but no unit target is known for installation type '${installation_type}'; skipping."
    else
      case "$buffer_type" in
        TurtleNeck)
          query_tn_pins "TN" "$buffer_unit_name"
          append_buffer_config "TurtleNeck" "$tn_advance_pin" "$tn_trailing_pin" "$buffer_section_name" "$buffer_extruder_file"
          ;;
        TurtleNeckV2)
          append_buffer_config "TurtleNeckV2" "" "" "$buffer_section_name" "$buffer_extruder_file"
          ;;
        FPS_PSF)
          if [ "$installation_type" == "EMU" ]; then
            # EMU's MCU board_pins config already defines a dedicated alias
            # for this sensor per lane.
            query_fps_pin "FPS_PSF" "$buffer_unit_name" "${buffer_unit_name}_lane1:TN"
          elif [ "$installation_type" == "OpenAMS" ]; then
            query_fps_pin "FPS_PSF" "$buffer_unit_name" "fps:PA2"
          else
            query_fps_pin "FPS_PSF" "$buffer_unit_name"
          fi
          append_buffer_config "FPS_PSF" "" "" "$buffer_section_name" "$buffer_extruder_file"
          ;;
      esac
      add_buffer_to_extruder "$buffer_extruder_file" "$buffer_section_name" "$buffer_unit_name" "$buffer_unit_section_prefix"
    fi
  fi
  check_and_append_prep "${afc_config_dir}/AFC.cfg"
  replace_varfile_path "${afc_config_dir}/AFC.cfg"
  if [ "$git_install" == "True" ] && [ "$is_snapmaker" == "False" ]; then
    update_moonraker_config
  fi

  if [ "$is_snapmaker" == "True" ]; then
    # Passing in True since su is needed to write to debug file
    u1_write_debug_file
  fi

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
  htlf_msg_board_type="$htlf_board_type"
  [[ "$htlf_msg_board_type" == "MMB_1.0" || "$htlf_msg_board_type" == "MMB_1.1" ]] && htlf_msg_board_type="MMB"
  message+="""
- Ensure you enter either your CAN bus or serial information in the ${afc_config_dir}/AFC_${htlf_msg_board_type}_${boxturtle_name}.cfg file.

- Ensure you modify the ${afc_config_dir}/AFC_${htlf_msg_board_type}_${boxturtle_name}.cfg file to select the proper rotation distance
  and gear ratio for your stepper motors.

- Ensure you update any necessary buffer information in the ${afc_config_dir}/AFC_${htlf_msg_board_type}_${boxturtle_name}.cfg file
  """
elif [ "$installation_type" == "Claymore" ]; then
  message+="""
- Ensure you enter either your CAN bus or serial information in the ${afc_config_dir}/AFC_${boxturtle_name}.cfg file.

- Ensure you update any necessary buffer information in the ${afc_config_dir}/AFC_${boxturtle_name}.cfg file
  """
elif [ "$installation_type" == "QuattroBox" ]; then
  message+="""
- You must update the ${afc_config_dir}/AFC_QuattroBox_1.cfg file to reference the proper buffer configuration and pins.

- Ensure you enter either your CAN bus or serial information in the ${afc_config_dir}/AFC_QuattroBox_1.cfg file
  """
elif [ "$installation_type" == "OpenAMS" ]; then
  message+="""
- Review and update the ${afc_config_dir}/AFC_AMS_1.cfg file for your AMS unit settings.

- Ensure OpenAMS is properly installed and configured per their instructions.
  """
elif [ "$installation_type" == "ViViD" ]; then
  message+="""
- Ensure you enter your serial information in the ${afc_config_dir}/AFC_Vivid_1.cfg file

- Review the ${afc_config_dir}/AFC_Vivid_1.cfg file to reference the proper buffer configuration and pins.
  """
elif [ "$installation_type" == "EMU" ]; then
  message+="""
- Ensure you enter either your CAN bus or serial information for each lane in the ${afc_config_dir}/AFC_${boxturtle_name}.cfg file

- The MCU board_pins configuration is at ${afc_config_dir}/mcu/EMU_${boxturtle_name}.cfg
  """
fi

if [ "$buffer_type" == "TurtleNeckV2" ]; then
  message+="""
- Ensure you add the correct serial information to the ${afc_config_dir}/mcu/TurtleNeckv2.cfg file
  """
fi

if [ "$buffer_type" == "FPS_PSF" ]; then
  message+="""
- Ensure the PSF ADC pin in your buffer configuration matches where your wiring is connected to your MCU.
  """
fi

message+="""
You may now quit the script or return to the main menu.

${RED}If you would like to add any additional units, please restart the script to ensure the
current units are loaded correctly.${NC}
"""

}