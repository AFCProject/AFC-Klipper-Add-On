#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

update_config_value() {
  # Function to update a specific key-value pair in a configuration file.
  # Arguments:
  #   $1: file_path - The path to the configuration file.
  #   $2: key - The key whose value needs to be updated.
  #   $3: new_value - The new value to be assigned to the key.

  local file_path="$1"
  local key="$2"
  local new_value="$3"

  # Create a temporary file to store the updated content.
  local temp_file=$(mktemp)

  # Read the configuration file line by line.
  while IFS= read -r line; do
    # Check if the line contains the key and capture any comment at the end of the line.
    if [[ "$line" =~ ^[[:space:]]*$key[[:space:]]*:[[:space:]]*([^[:space:]]+)[[:space:]]*(#.*)?$ ]]; then
      local comment="${BASH_REMATCH[2]}"
      # Write the updated key-value pair along with the comment to the temporary file.
      echo "$key: $new_value ${comment}" >> "$temp_file"
    else
      # Write the original line to the temporary file if it does not contain the key.
      echo "$line" >> "$temp_file"
    fi
  done < "$file_path"

  # Replace the original configuration file with the updated temporary file.
  mv "$temp_file" "$file_path"
}

replace_varfile_path() {
  # Function to replace the VarFile path in a configuration file.
  # Arguments:
  #   $1: file_path - The path to the configuration file.
  #   $2: old_path - The old VarFile path to be replaced.
  #   $3: new_path - The new VarFile path to replace with.
  local file_path="$1"
  local old_path="VarFile: ../printer_data/config/AFC/AFC.var"
  local new_path

  new_path="VarFile: ${afc_config_dir}/AFC.var"

  if grep -qF "$old_path" "$file_path"; then
    sed -i "s|$old_path|$new_path|" "$file_path"
  fi
}

update_switch_pin() {
  # Function to update the switch pin value in the filament switch sensor section of a configuration file.
  # Arguments:
  #   $1: file_path - The path to the configuration file.
  #   $2: new_value - The new value to be assigned to the switch pin.

  local file_path="$1"
  local new_value="$2"
  local temp_file=$(mktemp)
  local in_section=false

  # Read the configuration file line by line.
  while IFS= read -r line; do
    # Check if the line indicates the start of the filament switch sensor section.
    if [[ "$line" =~ ^\[AFC_extruder\ extruder\]$ ]]; then
      in_section=true
      echo "$line" >> "$temp_file"
    # If within the section and the line contains the switch pin, update its value.
    elif $in_section && [[ "$line" =~ ^pin_tool_start: ]]; then
      echo "pin_tool_start: $new_value" >> "$temp_file"
      in_section=false
    else
      # Write the original line to the temporary file if it does not match the above conditions.
      echo "$line" >> "$temp_file"
    fi
  done < "$file_path"

  # Replace the original configuration file with the updated temporary file.
  mv "$temp_file" "$file_path"
}

manage_include() {
  # Function to manage the inclusion of AFC configuration files in a specified file.
  # Arguments:
  #   $1: file_path - The path to the file where the include statement should be added or removed.
  #   $2: action - The action to perform, either 'add' to add the include statement or 'remove' to remove it.
  #
  # The function uses the following local variables:
  #   - include_statement: The include statement to be added or removed.
  #   - save_config_line: A marker line in the file to help position the include statement.

  local file_path="$1"
  local action="$2"
  local include_statement="[include AFC/*.cfg]"
  local save_config_line="#*# <---------------------- SAVE_CONFIG ---------------------->"

  if [ "$action" == "add" ]; then
    # Add the include statement if it is not already present in the file.
    if ! grep -qF "$include_statement" "$file_path"; then
      if grep -qF "$save_config_line" "$file_path"; then
        # Insert the include statement before the save_config_line if it exists.
        sed -i "/$save_config_line/i $include_statement" "$file_path"
      else
        # Append the include statement to the end of the file if save_config_line does not exist.
        echo "$include_statement" >> "$file_path"
      fi
    fi
  elif [ "$action" == "remove" ]; then
    # Remove the include statement if it is present in the file.
    if grep -qF "$include_statement" "$file_path"; then
      grep -vF "$include_statement" "$file_path" > "${file_path}.tmp"
      mv "${file_path}.tmp" "$file_path"
    fi
  fi
}

check_and_append_prep() {
  # Function to check for the presence of a specific section in a configuration file and append it if not present.
  # Arguments:
  #   $1: file_path - The path to the configuration file.
  local file_path="$1"
  local section="[AFC_prep]"
  local content="enable: True"

  if ! grep -qF "$section" "$file_path"; then
    echo -e "\n$section\n$content" >> "$file_path"
  fi
}

remove_t_macros() {
  # Function to remove the T macros from the configuration file.
  local t_macro
  local t_macros

  t_macros=$(grep -o -E 'T[0-9]+' "${afc_config_dir}/macros/AFC_macros.cfg" || true)

  for t_macro in $t_macros; do
    crudini --del "${afc_config_dir}"/macros/AFC_macros.cfg "gcode_macro $t_macro"
  done
}

function update_moonraker_config() {
  # Function to update the Moonraker configuration with AFC-Klipper-Add-On settings.
  # Uses the global variables:
  #   - MOONRAKER_PATH: The path to the Moonraker installation.
  #   - MOONRAKER_UPDATE_CONFIG: The configuration settings to be added to Moonraker.

  local moonraker_config

  # Check if the AFC-Klipper-Add-On configuration is already present in the Moonraker config file.
  moonraker_config=$(grep -c '\[update_manager afc-software\]' "${moonraker_config_file}" || true)

  if [ "$moonraker_config" -eq 0 ]; then
    # If not present, append the configuration settings to the Moonraker config file.
    echo -e -n "\n${moonraker_update_config}" >>"${moonraker_config_file}"
    # Restart the Moonraker service to apply the new configuration.
    restart_service moonraker
  fi
}