#!/usr/bin/env bash
set -euo pipefail

# Armored Turtle Automated Filament Changer - Debug Script
# Copyright (C) 2024 Armored Turtle
# Licensed under GNU GPLv3
# Thanks to @Esoterical, @bolliostu, and @dormouse for contributions to the original source / inspiration for this script
# at https://github.com/Esoterical/voron_canbus/blob/main/troubleshooting/debugging/can_debug.sh


printer_log_dir="$HOME/printer_data/logs"
printer_config_dir="$HOME/printer_data/config"
klipper_dir="$HOME/klipper"
afc_path="$HOME/AFC-Klipper-Add-On"
afc_config_dir="$printer_config_dir/AFC"
afc_file="$afc_config_dir/AFC.cfg"
klipper_venv="$HOME/klippy-env/bin"
moonraker_config="$printer_config_dir/moonraker.conf"

# Global vars
temp_log=""
afc_version="Unknown"
klipper_version="Unknown"
NO_NC=false

ALL_CAN_IDS=()

log_can_interfaces() {
    local interfaces
    interfaces=$(ip -br link show | awk '{print $1}' ) # Let's get all the network interfaces in case someone named a CAN interface something weird
    if [[ -z "$interfaces" ]]; then
        echo "No CAN interfaces found."
        return
    fi
    echo "Querying CAN devices..."
    for iface in $interfaces; do
        local can_output
        if ! can_output=$("$klipper_venv"/python "$klipper_dir"/scripts/canbus_query.py "$iface" 2>&1); then
            continue
        fi
        # Split output into lines, then prepend each with the interface name and store
        ALL_CAN_IDS+=("Interface: $iface")
        while IFS= read -r line; do
            ALL_CAN_IDS+=("$line")
        done <<< "$can_output"
        ALL_CAN_IDS+=("")
    done
}



is_klipper_running_systemd() {
    if systemctl list-units --type=service | grep -q klipper; then
        systemctl is-active --quiet klipper
        return $?  # 0 if running, 3 if not running
    else
        return 1  # Service not found
    fi
}

is_klipper_running_service() {
    service klipper status > /dev/null 2>&1
    return $?  # 0 if running, non-zero if not running
}

stop_klipper() {
    if command -v systemctl > /dev/null 2>&1; then
        if is_klipper_running_systemd; then
            echo "Stopping Klipper using systemd..."
            sudo systemctl stop klipper
        else
            echo "Klipper is already stopped (systemd)."
        fi
    elif command -v service > /dev/null 2>&1; then
        if is_klipper_running_service; then
            echo "Stopping Klipper using service..."
            sudo service klipper stop
        else
            echo "Klipper is already stopped (service)."
        fi
    else
        echo "Unable to detect systemd or service management for Klipper."
        exit 1
    fi
}

start_klipper() {
    if command -v systemctl > /dev/null 2>&1; then
        echo "Starting Klipper using systemd..."
        sudo systemctl start klipper
    elif command -v service > /dev/null 2>&1; then
        echo "Starting Klipper using service..."
        sudo service klipper start
    else
        echo "Unable to detect systemd or service management for Klipper."
        exit 1
    fi
}

# Check for nc and install if missing
checknc() {
	if ! command -v nc > /dev/null 2>&1 ; then
		echo "NetCat not found, installing..."
		if ! sudo apt-get install netcat-openbsd -qq > /dev/null; then
			NO_NC=true
		fi
	fi
}

# Format section headers in the log
prepout() {
	printf "\n================================================================\n"
	printf "%s\n" "$1"
	printf "================================================================\n"
	shift
	for var in "$@"; do
		printf "%s\n" "$var"
	done
}

# Safe cat to remove null bytes
safe_cat() {
	tr -d '\0' < "$1"
}

# Null byte detector
check_null_bytes_in_file() {
	local file="$1"
	if grep -qP '\x00' "$file" 2>/dev/null; then
		echo "WARNING: Null bytes detected in $file"
	fi
}

temp_dir_creation() {
	temp_dir=$(mktemp -d)
	if [[ ! "$temp_dir" || ! -d "$temp_dir" ]]; then
		echo "Could not create temporary directory"
		exit 1
	fi
	echo "Temporary directory created at $temp_dir"
}

clean_up_temp_dir() {
	trap 'rm -rf -- "$temp_dir"' EXIT
}

extract_klipper_logs() {
	echo "Extracting Klipper logs..."
	temp_dir_creation
	cp "$printer_log_dir"/klippy.log "$temp_dir"
	cp "$printer_log_dir"/AFC.log* "$temp_dir"
	cd "$temp_dir" || exit
	"$klipper_venv"/python "$klipper_dir"/scripts/logextract.py ./klippy.log
}

create_temp_log() {
	temp_log=$(mktemp)
	echo "Using temporary log file: $temp_log"
}

clean_up_temp_log() {
	if [ -n "${temp_log:-}" ]; then
		rm -f "$temp_log"
	fi
}

trap clean_up_temp_log EXIT

get_afc_version() {
	cd "$afc_path"
	git_hash=$(git -C . rev-parse --short HEAD)
	git_commit_num=$(git -C . rev-list HEAD --count)
	afc_version="${git_commit_num}-${git_hash}"
	cd - > /dev/null
}

get_klipper_version() {
	cd "$klipper_dir"
	klipper_version=$(git describe --tags)
	cd - > /dev/null
}

# Upload a file to termbin, return a plain text string (no colors)
upload_file_to_termbin() {
	local file="$1"
	local file_name=$(basename "$file")

	# Upload only (no colors or print_msg in this function)
	local tmp_url_file=$(mktemp)
	tr -d '\0' < "$file" | nc termbin.com 9999 > "$tmp_url_file"
	local url=$(tr -d '\0' < "$tmp_url_file")
	rm -f "$tmp_url_file"

	if [[ -n "$url" ]]; then
		echo "$file_name: $url"
	else
		echo "$file_name: Upload failed"
	fi
}

# Append file contents safely to the log
append_file_to_log() {
	local section_name="$1"
	local file_path="$2"

	prepout "$section_name" >> "$temp_log"
	if [ -f "$file_path" ]; then
		check_null_bytes_in_file "$file_path" >> "$temp_log"
		safe_cat "$file_path" >> "$temp_log"
	else
		echo "File not found: $file_path" >> "$temp_log"
	fi
}

echo "This script will collect diagnostic information and upload it to Armored Turtle support."
echo "Please review the script if you have concerns about its contents."
echo "Klipper will be stopped during this process — do not run this while printing."
echo ""

read -p "Do you wish to continue? [y/n]: " yn < /dev/tty
case $yn in
	[Yy]*) ;;
	[Nn]*) exit ;;
	*) echo "Please answer y or n."; exit 1 ;;
esac

stop_klipper
checknc
create_temp_log
extract_klipper_logs

echo "Gathering system information..."

DISTRO=$(strings /etc/*-release 2>/dev/null || echo "Unknown")
KERNEL=$(uname -a)
UPTIME=$(uptime)
LSUSB=$(lsusb | tr -d '\0')
SERIAL_IDS=$(ls -l /dev/serial/by-id/ || echo "No serial devices found.")

get_afc_version
get_klipper_version
log_can_interfaces

{
	prepout "System Information" \
		"Distro: $DISTRO" \
		"Kernel: $KERNEL" \
		"Uptime: $UPTIME"

	prepout "Versions"
	printf "AFC Version: %s\n" "$afc_version"
	printf "Klipper Version: %s\n" "$klipper_version"

	prepout "lsusb output"
	echo "$LSUSB"

	prepout "Serial IDs"
	echo "$SERIAL_IDS"

	prepout "CAN Bus IDs"
  if [ ${#ALL_CAN_IDS[@]} -eq 0 ]; then
      echo "No CAN devices found."
  else
      printf "%s\n" "${ALL_CAN_IDS[@]}"
  fi
} >> "$temp_log"

# Let's get all the AFC config files
find "$afc_config_dir" -type f | while read -r file; do
    file_name=$(basename "$file")
    append_file_to_log "$file_name" "$file"
done

# Add the moonraker config if it exists
if [ -f "$moonraker_config" ]; then
    append_file_to_log "$moonraker_config" "$moonraker_config"
else
    echo "File not found: $moonraker_config" >> "$temp_log"
fi

uploaded_files=()

if [ "$NO_NC" = true ]; then
    echo "NetCat is unavailable. Creating a zip archive of logs instead."
    zipfile="$HOME/afc_debug_logs_$(date +%Y%m%d_%H%M%S).zip"
    zip -j "$zipfile" "$temp_log" "$temp_dir"/* > /dev/null
    echo "Logs have been saved to $zipfile"
    echo "Please share this file with the Armored Turtle support team."
else
  for file in "$temp_dir"/*; do
    if [[ "$file" == *config* || "$file" == *shutdown* || "$file" == *AFC* || "$file" == *klippy.log ]]; then
      file_name=$(basename "$file")

      # Inform the user in the terminal (colored)
      echo "Uploading \"$file_name\" to termbin..."

      # Log-friendly plain text URL (no colors)
      uploaded_files+=("$(upload_file_to_termbin "$file")")
    fi
  done

  if [ ${#uploaded_files[@]} -gt 0 ]; then
    {
      prepout "Uploaded Extracted Klippy Configuration and Shutdown Logs"
      for entry in "${uploaded_files[@]}"; do
        printf "%s\n" "$entry"
      done
    } >> "$temp_log"
  fi

  if nc -z -w 3 termbin.com 9999; then
    echo "Uploading logs to termbin.com..."
    tr -d '\0' < "$temp_log" | nc termbin.com 9999 > /tmp/afc_upload_url
    OUTPUTURL=$(tr -d '\0' < /tmp/afc_upload_url)
    echo "Logs are available at ${OUTPUTURL}"
    echo "Please share this URL with the Armored Turtle support team."
  else
    echo "Failed to connect to termbin.com"
  fi
fi

start_klipper
clean_up_temp_dir
clean_up_temp_log
rm -rf /tmp/afc_upload_url
