#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

set -e
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Parse -b flag early so we can switch branches before sourcing includes.
# This ensures include files are sourced from the correct branch.
_requested_branch=""
_optind_save=$OPTIND
OPTIND=1
while getopts "a:k:s:m:n:b:p:y:th" _arg; do
  case ${_arg} in
    b) _requested_branch=${OPTARG} ;;
    *) ;;
  esac
done
OPTIND=$_optind_save

if [[ -n "${_requested_branch}" && -d "${SCRIPT_DIR}/.git" ]]; then
  _current_branch="$(git -C "${SCRIPT_DIR}" rev-parse --abbrev-ref HEAD)"
  if [[ "${_current_branch}" != "${_requested_branch}" ]]; then
    echo "→ Switching to branch '${_requested_branch}' before loading…"
    git -C "${SCRIPT_DIR}" fetch --prune --quiet
    git -C "${SCRIPT_DIR}" checkout --quiet -- "${_requested_branch}"
    git -C "${SCRIPT_DIR}" pull --rebase --quiet 2>/dev/null || true
    echo "✓ Branch switched. Restarting script…"
    exec "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")" "$@"
  fi
fi

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
  afc_config_dir="${printer_config_dir}/AFC"
  afc_file="${afc_config_dir}/AFC.cfg"
  moonraker_config_file="${printer_config_dir}/moonraker.conf"
  afc_path="$HOME/AFC-Klipper-Add-On"

  # Make sure necessary directories exist
  echo "Ensuring we are not running as root (except on K1 OS)..."
  check_root
  echo "Ensuring no conflicting software is present..."
  check_for_hh
  echo "Checking to ensure crudini and jq are present..."
  check_for_prereqs
  echo "Checking installation method..."
  check_for_zip_install
  if [ "$test_mode" == "False" ]; then
    check_python_version
    if [ "$git_install" == "True" ]; then
      clone_and_maybe_restart
    fi
  fi
  check_existing_install
  echo "Starting installation process.."
  sleep 2
  clear
  main_menu
}

main "$@"