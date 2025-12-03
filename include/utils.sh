#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

pushd() {
  command pushd "$@" >/dev/null || exit
}

popd() {
  command popd >/dev/null || exit
}

function show_help() {
  echo "Usage: install-afc.sh [options]"
  echo ""
  echo "Options:"
  echo "  -a <moonraker address>      Specify the address of the Moonraker server (default: http://localhost)"
  echo "  -k <path>                   Specify the path to the Klipper directory"
  echo "  -m <moonraker config path>  Specify the path to the Moonraker config file (default: ~/printer_data/config/moonraker.conf)"
  echo "  -n <moonraker port>         Specify the port of the Moonraker server (default: 7125)"
  echo "  -s <klipper service name>   Specify the name of the Klipper service (default: klipper)"
  echo "  -p <printer config dir>     Specify the path to the printer config directory (default: ~/printer_data/config)"
  echo "  -b <branch>                 Specify the branch to use (default: main)"
  echo "  -y <klipper venv dir>       Specify the klipper python venv dir (default: ~/klippy-env/bin)"
  echo "  -h                          Display this help message"
  echo ""
  echo "Example:"
  echo " $0 [-a <moonraker address>] [-k <klipper_path>] [-s <klipper_service_name>] [-m <moonraker_config_path>] [-n <moonraker_port>] [-p <printer_config_dir>] [-p <printer_config_dir>] [-b <branch>] [-y <klipper venv dir>] [-h] "
}

function copy_config() {
  mkdir -p "${afc_config_dir}"
  cp ${afc_path}/config/AFC.cfg "${afc_config_dir}/"
  cp ${afc_path}/config/AFC_Macro_Vars.cfg "${afc_config_dir}/"
  mkdir -p "${afc_config_dir}/mcu"
  cp -R ${afc_path}/config/macros "${afc_config_dir}/"
}

get_git_version() {
  cd "$afc_path"
	git_hash=$(git -C . rev-parse --short HEAD)
	afc_py_version=$(grep "AFC_VERSION=" "${afc_path}/extras/AFC.py" | cut -d '=' -f2 | tr -d ' "')
	afc_version="${afc_py_version}-${git_hash}"
	cd - > /dev/null
}

clone_and_maybe_restart() {
  if [[ ! -d "${afc_path}/.git" ]]; then
    echo "→ Cloning ${branch} from ${gitrepo} into ${afc_path}…"
    git clone \
      --branch "${branch}" \
      --single-branch \
      --depth 1 \
      "${gitrepo}" \
      "${afc_path}"
    echo "✓ Clone complete."
  else
    echo "→ Switching to branch '${branch}'…"
    git -C "${afc_path}" checkout --quiet "${branch}"

    echo "→ Checking for uncommitted changes…"
    if ! git -C "${afc_path}" diff --quiet || \
       ! git -C "${afc_path}" diff --quiet --cached; then
      echo "❌ You have uncommitted changes in ${afc_path}."
      echo "   Please commit or stash them before running this script."
      echo ""
      echo "💡 To discard your changes and reset the branch:"
      echo "   cd \"${afc_path}\""
      echo "   git checkout ${branch}"
      echo "   git reset --hard origin/${branch}"
      echo "   git clean -fd"
      exit 1
    fi

    echo "→ Fetching updates in ${afc_path}…"
    git -C "${afc_path}" fetch --prune --quiet

    if git -C "${afc_path}" status --porcelain --branch | grep -q 'behind'; then
      echo "→ New commits detected; rebasing…"
      git -C "${afc_path}" pull --rebase --quiet
      echo "✓ Update applied. Restarting script…"
      exec "$0" "${original_args[@]}"
    else
      echo "✓ Already up to date."
    fi
  fi
}

backup_afc_config() {
  # Function to back up the existing AFC configuration.
  # Arguments:
  #   - AFC_CONFIG_PATH: The path to the AFC configuration directory.
  #   - PRINTER_CONFIG_PATH: The path to the printer configuration directory.

  if [ -d "${afc_config_dir}" ]; then
    pushd "${printer_config_dir}" || exit
    mv AFC AFC.backup."$backup_date"
    popd || exit
  fi
}

backup_afc_config_copy() {
  # Function to back up the existing AFC configuration.
  # Arguments:
  #   - AFC_CONFIG_PATH: The path to the AFC configuration directory.
  #   - PRINTER_CONFIG_PATH: The path to the printer configuration directory.

  if [ -d "${afc_config_dir}" ]; then
    pushd "${printer_config_dir}" || exit
    cp -R AFC AFC.backup."$backup_date"
    popd || exit
  fi
}

exclude_from_klipper_git() {
  local EXTRAS_DIR="${afc_path}/extras"
  local EXCLUDE_FILE="${klipper_dir}/.git/info/exclude"

  # Find all .py files in the extras directory and add them to the exclude file if they are not already present
  find "$EXTRAS_DIR" -type f -name "*.py" | while read -r file; do
    # Adjust the file path to the required format
    local relative_path="klippy/extras/$(basename "$file")"
    if ! grep -Fxq "$relative_path" "$EXCLUDE_FILE"; then
      echo "$relative_path" >> "$EXCLUDE_FILE"
    fi
  done
}

restart_service() {
  # Function to restart a given service.
  # Arguments:
  #   $1 - The name of the service to restart.

  local service_name=$1
  if command -v systemctl &> /dev/null; then
    sudo systemctl restart "${service_name}"
  else
    sudo service "${service_name}" restart
  fi
}

restart_klipper() {
  if query_printer_status; then
    restart_service klipper
  fi
}

exit_afc_install() {
  if [ "$files_updated_or_installed" == "True" ]; then
    restart_klipper
  fi
  remove_vars_tool_file
  exit 0
}

function auto_update() {
  check_and_append_prep "${afc_config_dir}/AFC.cfg"
  remove_t_macros
  # merge_configs "${AFC_CONFIG_PATH}/AFC_Hardware.cfg" "${AFC_PATH}/templates/AFC_Hardware-AFC.cfg" "${AFC_CONFIG_PATH}/AFC_Hardware-temp.cfg"
  # cleanup_blank_lines "${AFC_CONFIG_PATH}/AFC_Hardware-temp.cfg"
  # mv "${AFC_CONFIG_PATH}/AFC_Hardware-temp.cfg" "${AFC_CONFIG_PATH}/AFC_Hardware.cfg"
}


remove_vars_tool_file() {
  if [ -f "${afc_config_dir}/*.tool" ]; then
    rm "${afc_config_dir}/*.tool"
  fi
}

stop_service() {
  # Function to restart a given service.
  # Arguments:
  #   $1 - The name of the service to restart.

  local service_name=$1
  if command -v systemctl &> /dev/null; then
    sudo systemctl stop "${service_name}"
  else
    sudo service "${service_name}" stop
  fi
}

start_service() {
  # Function to restart a given service.
  # Arguments:
  #   $1 - The name of the service to restart.

  local service_name=$1
  if command -v systemctl &> /dev/null; then
    sudo systemctl start "${service_name}"
  else
    sudo service "${service_name}" start
  fi
}

del_var_file() {
  local confirm
  # Function to remove the user's AFC.var.unit file in a simple fashion.
  echo "This is generally a troubleshooting step and should not be needed during normal operation."
  read -p "Do you want to proceed? (y/n): " confirm
  confirm="${confirm,,}"
  if [[ "$confirm" != "y" ]]; then
    unit_message="Operation cancelled by user.\n"
  else
    if [ -f "${afc_config_dir}/AFC.var.unit" ]; then
      rm "${afc_config_dir}/AFC.var.unit"
      unit_message="Removed old AFC.var.unit file.\n"
    else
      unit_message="No AFC.var.unit file found to remove.\n"
    fi
  fi
  export unit_message
}