#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

uninstall_afc() {
  unlink_extensions
  manage_include "${printer_config_dir}/printer.cfg" "remove"
  if [ "$is_snapmaker" == "True" ]; then
    move_lite_files_back
    comment_gcode_in_fluidd
  fi
  backup_afc_config
  restart_klipper
  message="""
  AFC has been uninstalled successfully.

  Please restart your printer to complete the uninstallation process.

  We always welcome feedback on the software on our Discord channel at

  https://discord.gg/armoredturtle.
  """
  export message
}
