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

copy_unit_files() {
  unit_copy_files "$installation_type"
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
  buffer_unit_name=""
  buffer_unit_section_prefix=""
  buffer_extruder_file=""
  buffer_section_name=""
  buffer_prebaked_header=""

  # ViViD and any other type without a unit_buffer_target_<key> adapter
  # leaves the vars above empty, which triggers the "no unit target known"
  # warning in install_afc() and skips applying a PSF-style buffer.
  unit_set_buffer_target "$installation_type"
}

apply_boxturtle_rename() {
  # BoxTurtle templates ship with the literal name "Turtle_1" baked in; if the
  # user picked a different name during install, rewrite every config file to
  # match. Other unit types handle their own renaming inline in copy_unit_files.
  #
  # This is deliberately a directory-wide rename (find over the whole
  # afc_config_dir), unlike unit_install_additional_BoxTurtle_4Lane/8Lane's
  # rename, which only touches the one new unit's file. That's not an
  # inconsistency to unify: this runs for the *first* unit, whose name can
  # legitimately appear in shared files (e.g. AFC_Hardware.cfg); an
  # additional unit's file is self-contained and never referenced elsewhere.
  if [ "$boxturtle_name" != "Turtle_1" ] && { [ "$installation_type" == "BoxTurtle (4-Lane)" ] || [ "$installation_type" == "BoxTurtle (8-Lane)" ]; }; then
    find "$afc_config_dir" -type f -exec sed -i "s/Turtle_1/$boxturtle_name/g" {} +
  fi
}

apply_unit_buffer() {
  local buffer_type="$1"
  if [ "$buffer_type" != "TurtleNeck" ] && [ "$buffer_type" != "TurtleNeckV2" ] && [ "$buffer_type" != "FPS_PSF" ]; then
    return 0
  fi
  get_unit_buffer_target
  if [ -z "$buffer_unit_name" ]; then
    print_msg WARNING "PSF buffer selected but no unit target is known for installation type '${installation_type}'; skipping."
    return 0
  fi
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
}

install_afc() {
  # This is always the first unit -- additional_system_menu.sh sets
  # is_additional_unit="True" itself
  is_additional_unit="False"
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

  apply_boxturtle_rename

  apply_unit_buffer "$buffer_type"
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
  build_install_message
}

build_install_message() {
  message="""
- AFC Configuration updated with selected options at ${afc_file}

- AFC-Klipper-Add-On python extensions installed to ${klipper_dir}/klippy/extras/
"""

unit_append_message "$installation_type"

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