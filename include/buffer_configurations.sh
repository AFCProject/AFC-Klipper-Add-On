#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

ensure_trailing_newline() {
  # Check if appending file has newline, if it does not add a newline before
  # appending new content
  local file_path="$1"
  if [ -s "$file_path" ] && [ -n "$(tail -c1 "$file_path")" ]; then
    echo >> "$file_path"
  fi
}

build_buffer_config() {
  # Builds the buffer config text block for the given buffer_type and sets
  # the global `buffer_config` (block text) and `buffer_name` (section name).
  # Returns 1 (and prints an error) for an unknown buffer_type.
  local buffer_type="$1"
  buffer_config=""
  buffer_name=""
  tn_advance_pin=$2
  tn_trailing_pin=$3
  local unit_name="${4:-Turtle_1}"

  case "$buffer_type" in
    "TurtleNeck")
      buffer_config=$(cat <<EOF
[AFC_buffer ${unit_name}]
advance_pin: ${tn_advance_pin}    # set advance pin
trailing_pin: ${tn_trailing_pin}  # set trailing pin
multiplier_high: 1.05   # default 1.05, factor to feed more filament
multiplier_low:  0.95   # default 0.95, factor to feed less filament
EOF
)
      buffer_name="${unit_name}"
      ;;
    "TurtleNeckV2")
      buffer_config=$(cat <<EOF
[AFC_buffer ${unit_name}]
advance_pin: !turtleneck:ADVANCE
trailing_pin: !turtleneck:TRAILING
multiplier_high: 1.05   # default 1.05, factor to feed more filament
multiplier_low:  0.95   # default 0.95, factor to feed less filament
led_index: Buffer_Indicator:1

[AFC_led Buffer_Indicator]
pin: turtleneck:RGB
chain_count: 1
color_order: GRBW
initial_RED: 0.0
initial_GREEN: 0.0
initial_BLUE: 0.0
initial_WHITE: 0.0
EOF
)
      buffer_name="${unit_name}"
      ;;
    "TurtleNeck2Remastered")
      buffer_config=$(cat <<EOF
[AFC_buffer ${unit_name}]
type: FPS_PSF
adc_pin: TN2:PA0
neutral_point: 0.5
max_tension: 0.1
max_compression: 0.9
reversed: true  #Test slide by hand and look at buffer state in mainsail/fluidd Set to true if display shows opposite of your tested state.
led_index: Buffer_Indicator:1

[AFC_led Buffer_Indicator]
pin: TN2:PB1
chain_count: 1
color_order: GRBW
initial_RED: 0.0
initial_GREEN: 0.0
initial_BLUE: 0.0
initial_WHITE: 0.0
EOF
)
      buffer_name="${unit_name}"
      ;;
    "FPS_PSF")
      buffer_config=$(cat <<EOF
[AFC_buffer ${unit_name}]
type: FPS_PSF
adc_pin: ${fps_adc_pin}
neutral_point: 0.5
max_tension: 0.1
max_compression: 0.9
reversed: false  #Test slide by hand and look at buffer state in mainsail/fluidd Set to true if display shows opposite of your tested state.
EOF
)
      buffer_name="${unit_name}"
      ;;
    *)
      echo "Invalid BUFFER_SYSTEM: $buffer_type"
      return 1
      ;;
  esac
}

append_buffer_config() {
  # Builds and appends a buffer config block to AFC_Hardware.cfg.
  # Arguments:
  #   $1-$4: same as build_buffer_config
  #   $5: target_file - file to write the buffer section into
  local buffer_type="$1"
  local target_file="$5"
  local header

  build_buffer_config "$1" "$2" "$3" "$4" || return 1
  header=$(echo "$buffer_config" | head -n 1)

  if grep -qxF "$header" "$target_file"; then
    replace_buffer_section "$target_file" "$header" "$buffer_config"
  else
    ensure_trailing_newline "$target_file"
    echo -e "\n$buffer_config" >> "$target_file"
  fi

  # Add [include mcu/TurtleNeckv2.cfg] to the target file if buffer_type is TurtleNeckV2 and not already present
  if [ "$buffer_type" == "TurtleNeckV2" ]; then
    safe_copy "${afc_path}/config/mcu/TurtleNeckv2.cfg" "$afc_config_dir/mcu/"
    if ! grep -qF "[include mcu/TurtleNeckv2.cfg]" "$target_file"; then
      ensure_trailing_newline "$target_file"
      echo -e "\n[include mcu/TurtleNeckv2.cfg]" >> "$target_file"
    fi
  fi

  # Add [include mcu/TN2.cfg] to the target file if buffer_type is TurtleNeck2Remastered and not present
  if [ "$buffer_type" == "TurtleNeck2Remastered" ]; then
    safe_copy "${afc_path}/config/mcu/TN2.cfg" "$afc_config_dir/mcu/"
    if ! grep -qF "[include mcu/TN2.cfg]" "$target_file"; then
      ensure_trailing_newline "$target_file"
      echo -e "\n[include mcu/TN2.cfg]" >> "$target_file"
    fi
  fi
}

add_buffer_to_extruder() {
  # Function to add a buffer configuration to the [AFC_extruder extruder] section in a configuration file.
  # Arguments:
  #   $1: file_path - The path to the configuration file.
  #   $2: buffer_name - The name of the buffer to be added.
  #   $3: unit_name - The unit's section name (e.g. Turtle_1, HTLF_1, AMS_1).
  #   $4: unit_section_prefix - The unit section prefix (default: AFC_BoxTurtle).
  local file_path="$1"
  local buffer_name="$2"
  local unit_name="${3:-Turtle_1}"
  local unit_section_prefix="${4:-AFC_BoxTurtle}"
  local section="[${unit_section_prefix} ${unit_name}]"
  local buffer_line="buffer: $buffer_name"

  awk -v section="$section" -v buffer="$buffer_line" '
    BEGIN { in_section = 0; done = 0 }
    # Match the start of the target section
    $0 == section {
      in_section = 1
      print $0
      next
    }
    # Replace an existing buffer: line in place instead of duplicating it
    in_section && /^buffer:[ \t]*/ {
      if (!done) { print buffer; done = 1 }
      next
    }
    # Otherwise insert the buffer line before the first blank line within
    # the target section
    in_section && /^$/ {
      if (!done) { print buffer; done = 1 }
      in_section = 0
    }
    # End section processing if a new section starts, inserting first if we
    # have not done so yet (handles a section with no trailing blank line)
    in_section && /^\[.+\]/ {
      if (!done) { print buffer; done = 1 }
      in_section = 0
    }
    # Print all lines
    { print $0 }
    # Handle the target section being the last one in the file, with no
    # trailing blank line before EOF
    END { if (in_section && !done) print buffer }
  ' "$file_path" > "$file_path.tmp" && mv "$file_path.tmp" "$file_path"
}

replace_buffer_section() {
  # Some unit templates (QuattroBox, ViViD, Claymore, EMU) already contain
  # [AFC_buffer <name>] placeholder sections in their unit config file,
  # When the user instead selects an FPS_PSF buffer, this replaces that placeholder
  # block in-place with the FPS_PSF config rather than leaving a stale,
  # conflicting switched-buffer section behind.
  #
  # Arguments:
  #   $1: file_path - The path to the configuration file containing the block.
  #   $2: old_section_header - The exact existing header line, e.g. "[AFC_buffer QuattroBox_1]".
  #   $3: new_buffer_config - The full replacement block (as built by append_buffer_config).
  local file_path="$1"
  local old_section_header="$2"
  local new_buffer_config="$3"

  if ! grep -qF "$old_section_header" "$file_path"; then
    print_msg WARNING "Could not find existing buffer section '$old_section_header' in $file_path; leaving file unchanged."
    return 1
  fi

  awk -v header="$old_section_header" -v newblock="$new_buffer_config" '
    BEGIN { in_section = 0; replaced = 0 }
    $0 == header {
      in_section = 1
      replaced = 1
      print newblock
      next
    }
    in_section && /^$/ {
      in_section = 0
      print $0
      next
    }
    in_section && /^\[.+\]/ {
      in_section = 0
    }
    in_section { next }
    { print $0 }
  ' "$file_path" > "$file_path.tmp" && mv "$file_path.tmp" "$file_path"
}

list_existing_buffers() {
  # Echoes the name of every [AFC_buffer <name>] section defined in the cfg
  # files directly under $afc_config_dir, one per line, sorted and unique.
  grep -hE '^\[AFC_buffer [^]]+\]' "$afc_config_dir"/*.cfg 2>/dev/null \
    | sed -E 's/^\[AFC_buffer ([^]]+)\].*/\1/' | sort -u
}

prompt_shared_buffer_name() {
  # Asks which existing buffer a new additional unit should share and sets
  # the global `shared_buffer_name` (empty if skipped or none exist).
  local buffers choice i=1 name
  shared_buffer_name=""
  buffers=$(list_existing_buffers)
  if [ -z "$buffers" ]; then
    print_msg WARNING "No existing buffers found in ${afc_config_dir}; set 'buffer:' manually."
    return 0
  fi
  echo ""
  echo "Existing buffers:"
  while IFS= read -r name; do
    printf "  %s. %s\n" "$i" "$name"
    i=$((i + 1))
  done <<< "$buffers"
  while true; do
    read -r -p "Enter the number or name of the buffer to share (blank to skip): " choice || choice=""
    if [ -z "$choice" ]; then
      return 0
    fi
    if [[ "$choice" =~ ^[0-9]+$ ]]; then
      name=$(sed -n "${choice}p" <<< "$buffers")
    elif grep -qxF -- "$choice" <<< "$buffers"; then
      name="$choice"
    else
      name=""
    fi
    if [ -n "$name" ]; then
      shared_buffer_name="$name"
      return 0
    fi
    print_msg WARNING "'$choice' is not one of the existing buffers."
  done
}

remove_buffer_section() {
  # Removes one [AFC_buffer <name>] section (header through its body and one
  # trailing blank line) from a config file. Commented lines are untouched.
  # Arguments:
  #   $1: file_path - The path to the configuration file containing the block.
  #   $2: section_header - The exact header line, e.g. "[AFC_buffer Claymore_2_buffer]".
  local file_path="$1"
  local section_header="$2"

  awk -v header="$section_header" '
    BEGIN { in_section = 0 }
    $0 == header { in_section = 1; next }
    in_section && /^$/ { in_section = 0; next }
    in_section && /^\[.+\]/ { in_section = 0 }
    in_section { next }
    { print $0 }
  ' "$file_path" > "$file_path.tmp" && mv "$file_path.tmp" "$file_path"
}

query_tn_pins() {
  # Function to query the user for the TurtleNeck pins.
  # Arguments:
  #   $1: buffer_name - The name of the buffer to be added.
  local buffer_name="$1"
  local unit_name="${2:-Turtle_1}"
  local input
  tn_advance_pin="^${unit_name}:TN_ADV"
  tn_trailing_pin="^${unit_name}:TN_TRL"

  print_msg INFO "\nPlease enter the pin numbers for the TurtleNeck buffer '$buffer_name':"
  print_msg INFO "(Leave blank to use the default values)"
  print_msg INFO "Ensure you use a pull-up '^' if you are using a AFC end stop pin."
  print_msg INFO "(Default: ^${unit_name}:TN_ADV)"
  print_msg INFO "(Default: ^${unit_name}:TN_TRL)"

  read -p "  Enter the advance pin (default: $tn_advance_pin): " -r input
  if [ -n "$input" ]; then
    tn_advance_pin="$input"
  fi

  read -p "  Enter the trailing pin (default: $tn_trailing_pin): " -r input
  if [ -n "$input" ]; then
    tn_trailing_pin="$input"
  fi

  print_msg INFO "Set ${buffer_name} Advance pin: $tn_advance_pin"
  print_msg INFO "Set ${buffer_name} Trailing pin: $tn_trailing_pin"
}

query_fps_pin() {
  # Function to query the user for the FPS/PSF sensor's ADC pin.
  # Arguments:
  #   $1: buffer_name - The name of the buffer to be added.
  #   $2: unit_name - Default pin-prefix suggestion, based on the unit name.
  local buffer_name="$1"
  local unit_name="${2:-Turtle_1}"
  local input
  fps_adc_pin="${3:-${unit_name}:PSF}"

  print_msg INFO "\nPlease enter the ADC pin for the FPS/PSF buffer '$buffer_name':"
  print_msg INFO "(Leave blank to use the default value)"
  print_msg INFO "Proportional Sync-Feedback (PSF) sensor is wired to."
  print_msg INFO "(Default: ${fps_adc_pin})"

  read -p "  Enter the ADC pin (default: $fps_adc_pin): " -r input
  if [ -n "$input" ]; then
    fps_adc_pin="$input"
  fi

  print_msg INFO "Set ${buffer_name} ADC pin: $fps_adc_pin"
}