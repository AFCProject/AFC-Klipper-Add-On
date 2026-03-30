#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
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
      local _macro_copied=0
      local _macro_skipped=0
      local _cfg_files=("${afc_path}/config/macros/"*.cfg)
      if [ ! -e "${_cfg_files[0]}" ]; then
        update_message+="""
No macro files found to update.
        """
      else
        for cfg in "${_cfg_files[@]}"; do
          safe_copy "$cfg" "${afc_config_dir}/macros/"
          if [ "$safe_copy_result" = "copied" ]; then
            _macro_copied=$(( _macro_copied + 1 ))
          else
            _macro_skipped=$(( _macro_skipped + 1 ))
          fi
        done
        if [ "$_macro_copied" -gt 0 ]; then
          update_message+="""
AFC Macros updated successfully (${_macro_copied} copied, ${_macro_skipped} skipped).
          """
        else
          update_message+="""
AFC Macros update skipped (all ${_macro_skipped} files kept as-is).
          """
        fi
      fi
    fi
  fi
  check_init_symlink
  link_extensions
  remove_t_macros
  remove_velocity
  if [ "$git_install" == "True" ]; then
    if [ "$test_mode" == "False" ]; then
      exclude_from_klipper_git
    fi
    update_message+="""
AFC Klipper Add-On updated successfully with version v${afc_version}.
"""
    export update_message
  else
    update_message+="""
AFC Klipper Add-On update process completed.
"""
    export update_message
  fi
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