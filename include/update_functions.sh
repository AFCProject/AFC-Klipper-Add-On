#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

update_afc() {
  local macro update_macros confirm_update_macros
  read -p "Do you want to update the AFC provided macros? (y/n): " update_macros
  update_macros="${update_macros,,}"
  if [[ "$update_macros" == "y" ]]; then
    print_msg WARNING "Updating macros will overwrite any custom changes you have made to these macros."
    print_msg WARNING "This includes the brush, cut, kick, park, afc_macros, and poop macros."
    read -p "Please confirm you want to update these macros: (y/n): " confirm_update_macros
    confirm_update_macros="${confirm_update_macros,,}"
    if [[ "$confirm_update_macros" == "y" ]]; then
      for macro in "Brush" "Cut" "Kick" "Park" "Poop" "AFC_macros"; do
        rm -rf "${afc_config_dir}/macros/${macro}.cfg"
      done
      if cp "${afc_path}/config/macros/"*.cfg "${afc_config_dir}/macros/"; then
        update_message+="""
AFC Macros updated successfully.
        """
      else
        update_message+="""
AFC Macros update failed.
        """
      fi
    fi
  fi
  link_extensions
  remove_t_macros
  remove_velocity
  update_message+="""
AFC Klipper Add-On updated successfully with version v${afc_version}.
"""
  export update_message
  files_updated_or_installed="True"
}

remove_velocity() {
  local files config_file
  files=$(grep -rlP '^\[AFC_buffer ' "${afc_config_dir}"/*.cfg)
  for config_file in $files; do
    section=$(grep -oP '^\[AFC_buffer \K[^\]]+' "$config_file")
    if grep -qP "^\s*velocity\s*" "$config_file"; then
      crudini --del "$config_file" "AFC_buffer $section" velocity
      export update_message+="""
Removed deprecated velocity setting from $config_file.
      """
    fi
  done
}