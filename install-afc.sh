#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

set -e
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source include/constants.sh

# Menu functions
source include/menus/main_menu.sh
source include/menus/install_menu.sh
source include/menus/update_menu.sh
source include/menus/utilities_menu.sh
source include/menus/additional_system_menu.sh
source include/utils.sh

# Install / Update functions
source include/buffer_configurations.sh
source include/check_commands.sh
source include/colors.sh
source include/install_functions.sh
source include/uninstall.sh
source include/update_commands.sh
source include/update_functions.sh

source include/unit_functions.sh

original_args=("$@")

main() {
  ###################### Main script logic below ######################

  while getopts "a:k:s:m:n:b:p:y:th" arg; do
    case ${arg} in
    a) moonraker_address=${OPTARG} ;;
    k) klipper_dir=${OPTARG} ;;
    m) moonraker_config_file=${OPTARG} ;;
    n) moonraker_port=${OPTARG} ;;
    s) klipper_service=${OPTARG} ;;
    b) branch=${OPTARG} ;;
    p) printer_config_dir=${OPTARG} ;;
    y) klipper_venv=${OPTARG} ;;
    t) test_mode=True ;;
    h) show_help
      exit 0 ;;
    *) exit 1 ;;
    esac
  done

  moonraker="${moonraker_address}:${moonraker_port}"
  moonraker="${moonraker_address}:${moonraker_port}"
  afc_config_dir="${printer_config_dir}/AFC"
  afc_file="${afc_config_dir}/AFC.cfg"
  moonraker_config_file="${printer_config_dir}/moonraker.conf"
  afc_path="$HOME/AFC-Klipper-Add-On"


  # Make sure necessary directories exist
  echo "Ensuring we are not running as root.."
  check_root
  echo "Ensuring no conflicting software is present.."
  check_for_hh
  echo "Checking to ensure crudini and jq are present.."
  check_for_prereqs
  if [ "$test_mode" == "False" ]; then
    check_python_version
    clone_and_maybe_restart
  fi
  check_existing_install
  echo "Starting installation process.."
  sleep 2
  clear
  main_menu
}

main "$@"