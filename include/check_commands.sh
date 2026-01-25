#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

check_klipper() {
  # Function to check if the Klipper service is running.
  # Uses systemctl to list all services and checks if the Klipper service is present.
  # If the service is found, it prints a success message.
  # If the service is not found, it prints an error message and exits with status 1.

  if sudo systemctl list-units --full -all -t service --no-legend | grep -q -F "${klipper_service}.service"; then
    print_msg SUCCESS "  Klipper service found!"
  else
    print_msg ERROR "  Klipper service not found. Install Klipper first."
    exit 1
  fi
}

check_root() {
  # Function to check if the script is being run as the root user.
  # If the script is run as root, it prints an error message and exits with status 1.
  # This is to ensure the script is run by a normal user for security reasons.

  check_for_k1
  # On K1 OS, allow running as root but warn the user instead of exiting.
  if [ "$is_k1_os" == "True" ] && [ "$EUID" -eq 0 ]; then
    print_msg WARNING "K1 OS detected, skipping root user check."
    return
  fi
  if [ "$EUID" -eq 0 ]; then
    print_msg ERROR "  Do not run as root, use a normal user"
    exit 1
  fi
}

check_existing_dirs() {
  # Function to check if the required directories for Klipper and Moonraker exist.
  # If the Klipper directory is not found, it prints an error message and exits with status 1.
  # If the Moonraker directory is not found, it prints an error message and exits with status 1.
  # The user can override the default directories using '-k <klipper_dir>' and '-m <moonraker_dir>' options.

  if [ ! -d "${klipper_path}" ]; then
    print_msg ERROR "  Klipper directory not found. Use '-k <klipper_dir>' to override."
    exit 1
  fi

  if [ ! -d "${MOONRAKER_PATH}" ]; then
    print_msg ERROR "  Moonraker configuration not found. Use '-m <moonraker_dir>' to override."
    exit 1
  fi
}

check_existing_install() {
  # Function to check for a prior AFC Klipper installation.
  # It iterates through all Python files in the extras directory of the AFC_PATH.
  # For each file, it checks if a symbolic link exists in the Klipper extras directory.
  # If an existing installation is found, it sets the PRIOR_INSTALLATION variable to True and breaks the loop.

  local extension
  for extension in ${afc_path}/extras/*.py; do
    extension=$(basename "${extension}")
    if [ -L "${klipper_dir}/klippy/extras/${extension}" ]; then
      prior_installation=True
      break
    fi
  done
}

check_for_hh() {
  # Function to check if the "Happy Hare" extension is installed in Klipper extras.
  # It checks for the presence of the mmu.py file in the Klipper extras directory.
  # If the file exists, it searches for the text "Happy Hare" within the file.
  # If "Happy Hare" is found, it prints an error message and exits with status 1.
  # This is because AFC is not compatible with the "Happy Hare" extension.

  local file_path="${klipper_dir}/klippy/extras/mmu.py"
  local search_text="Happy Hare"

  if [ -f "$file_path" ]; then
    if grep -q "$search_text" "$file_path"; then
      print_msg ERROR "Happy Hare was found installed in your klipper extras. AFC is not currently compatible"
      print_msg ERROR "with Happy Hare. Please remove it, and then re-run this install-afc.sh script."
      exit 1
    fi
  fi
}

check_for_afc() {
  # Function to check if the AFC Klipper extension is already installed.
  # It checks for the presence of the AFC extension in the Klipper extras directory.
  # If the AFC extension is found, it prints an error message and exits with status 1.
  # This is to prevent the user from installing AFC multiple times.

  local file_path="${klipper_dir}/klippy/extras/AFC.py"

  if [ ! -f "$file_path" ]; then
    print_msg ERROR "AFC Klipper extension not found. Install AFC first."
    exit 1
  fi
}

check_for_mainsail() {
  # Function to check if the Mainsail interface is already installed.

  if ! grep -q "Mainsail" "${MAINSAIL_DST}/manifest.webmanifest"; then
    print_msg ERROR "  Mainsail interface not found. Exiting."
    exit 1
  fi
}

check_for_fluidd() {
  # Function to check if the Fluidd interface is already installed.

  if ! grep -q "fluidd" "$FLUIDD_DST"/release_info.json; then
    print_msg ERROR "  Fluidd interface not found. Exiting."
    exit 1
  fi
}

check_unzip() {
  # Function to check if the unzip command is available.
  # If the unzip command is not found, it prints an error message and exits with status 1.

  if ! command -v unzip &> /dev/null; then
    print_msg ERROR "  unzip command not found. Please install unzip and try again."
    exit 1
  fi
}

query_printer_status() {
  local response
  local state

  response=$(curl -s "$moonraker"/printer/objects/query?idle_timeout)
  state=$(echo "$response" | jq -r '.result.status.idle_timeout.state')

  if [ "$state" == "Ready" ]; then
    return 0
  else
    return 1
  fi
}

check_for_prereqs() {
  missing_dependencies=()
  if ! command -v jq &> /dev/null; then
    missing_dependencies+=("jq")
  fi
  if ! command -v crudini &> /dev/null; then
    missing_dependencies+=("crudini")
  fi
  if [ ${#missing_dependencies[@]} -ne 0 ]; then
    echo "Missing software prerequisites. Please run the below command and re-run this install script."
    echo "sudo apt-get install -y ${missing_dependencies[*]}"
    exit 1
  fi
}

check_python_version() {
  local PYTHON
  local VERSION
  PYTHON="${klipper_venv}"/python

  if [[ ! -x "$PYTHON" ]]; then
      echo "Python not found at $PYTHON. Please double-check your klipper configuration or specify a directory with the -y flag."
      return 1
  fi

  VERSION=$($PYTHON -c 'import sys; print(".".join(map(str, sys.version_info[:2])))' 2>/dev/null)

  if [[ $? -ne 0 ]]; then
      echo "Failed to determine Python version"
      return 1
  fi

  if [[ $(echo -e "${VERSION}\n${minimum_python_major}.${minimum_python_minor}" | sort -V | head -n1) != "${minimum_python_major}.${minimum_python_minor}" ]]; then
      echo "Your available Klipper venv Python version $VERSION is too old. The BoxTurtle AFC software requires at least Python ${minimum_python_major}.${minimum_python_minor}."
      exit 1
  fi

  echo "Python version $VERSION is OK."
  return 0
}