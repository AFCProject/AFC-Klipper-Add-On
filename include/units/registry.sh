#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Unit type registry
# ===================
# Single source of truth for everything the installer needs to know about
# each supported unit type (BoxTurtle, NightOwl, HTLF, ...). Before this
# registry existed, adding a unit type meant touching a matching branch in
# five separate if/elif chains spread across unit_functions.sh,
# install_functions.sh and the menu files -- easy to update four of the five
# and ship a broken fifth.
#
# HOW TO ADD A NEW UNIT TYPE
# --------------------------
# 1. Pick a display name (what users see, e.g. "MyUnit") and a key: an
#    identifier-safe slug used in function names and art filenames
#    (e.g. "MyUnit"). Add both to UNIT_TYPE_ORDER / UNIT_KEY below, and a
#    default name to UNIT_ADDITIONAL_DEFAULT_NAME.
# 2. Add an ASCII art file at include/menus/art/<key>.sh defining a
#    print_unit_art_<key>() function (see include/menus/art/README.md).
#    A single `printf "placeholder art\n"` body works fine to start.
# 3. Add a block of adapter functions for the new key, following the pattern
#    of any existing block below:
#      - unit_copy_files_<key>          (required)  copies this unit's
#        template/mcu files into $afc_config_dir for a first-time install.
#      - unit_install_additional_<key>  (optional)  same, but for adding
#        this type as an *additional* unit to an existing install. Omit if
#        "add additional unit" isn't supported for this type yet.
#      - unit_buffer_target_<key>       (optional)  sets buffer_unit_name /
#        buffer_unit_section_prefix / buffer_extruder_file /
#        buffer_section_name / buffer_prebaked_header so a TurtleNeck /
#        TurtleNeckV2 / FPS_PSF buffer selection can be wired into this
#        unit's config. Omit if this type doesn't support a PSF-style buffer
#        yet (the installer will warn and skip rather than fail).
#      - unit_message_<key>             (optional)  appends any
#        type-specific follow-up instructions to $message after install.
# 4. copy_unit_files, install_additional_unit, get_unit_buffer_target,
#    build_install_message, name_additional_unit, the installation_type
#    cycle (T) and the unit art all consume the registry generically and
#    need no further edits.
# 5. Menu option *visibility* for type-specific settings (board/motor
#    selectors like "D. HTLF Board Type") is still a per-type case
#    statement in include/menus/install_menu.sh and
#    include/menus/additional_system_menu.sh, not registry-driven. If your
#    new unit type needs its own settings toggle beyond a name, add a
#    branch there the same way HTLF/QuattroBox/EMU do.

# Canonical list of supported unit types, in menu display order.
UNIT_TYPE_ORDER=(
  "BoxTurtle (4-Lane)"
  "BoxTurtle (8-Lane)"
  "NightOwl"
  "HTLF"
  "Claymore"
  "QuattroBox"
  "OpenAMS"
  "ViViD"
  "EMU"
)

# installation_type (display name) -> identifier-safe key used for function
# name suffixes and art filenames.
declare -A UNIT_KEY=(
  ["BoxTurtle (4-Lane)"]="BoxTurtle_4Lane"
  ["BoxTurtle (8-Lane)"]="BoxTurtle_8Lane"
  ["NightOwl"]="NightOwl"
  ["HTLF"]="HTLF"
  ["Claymore"]="Claymore"
  ["QuattroBox"]="QuattroBox"
  ["OpenAMS"]="OpenAMS"
  ["ViViD"]="ViViD"
  ["EMU"]="EMU"
)

# installation_type -> default unit name offered by name_additional_unit().
declare -A UNIT_ADDITIONAL_DEFAULT_NAME=(
  ["BoxTurtle (4-Lane)"]="Turtle_2"
  ["BoxTurtle (8-Lane)"]="Turtle_2"
  ["NightOwl"]="NightOwl_1"
  ["HTLF"]="HTLF_1"
  ["Claymore"]="Claymore_1"
  ["QuattroBox"]="QuattroBox_1"
  ["OpenAMS"]="AMS_1"
  ["ViViD"]="Vivid_1"
  ["EMU"]="EMU_1"
)

# Keys the "add additional unit" menu offers a buffer toggle for.
UNIT_ADDITIONAL_BUFFER_SAFE=(
  "BoxTurtle_4Lane"
  "BoxTurtle_8Lane"
  "Claymore"
  "EMU"
  "NightOwl"
  "HTLF"
  "QuattroBox"
  "OpenAMS"
)

unit_additional_buffer_supported() {
  # True (0) if $1 (a registry key, e.g. from unit_key_for_type) can safely
  # get a buffer attached via the "add additional unit" menu.
  local key="$1" k
  for k in "${UNIT_ADDITIONAL_BUFFER_SAFE[@]}"; do
    [ "$k" == "$key" ] && return 0
  done
  return 1
}

unit_additional_buffer_options() {
  case "$1" in
    "OpenAMS") printf 'None FPS_PSF' ;;
    *) printf 'None TurtleNeck TurtleNeckV2 FPS_PSF' ;;
  esac
}

unit_additional_buffer_default() {
  # Echoes the buffer_type the additional-unit menu should default a unit
  # of type $1 to "own buffer" as (see unit_additional_buffer_options).
  case "$1" in
    "OpenAMS") printf 'FPS_PSF' ;;
    *) printf 'TurtleNeck' ;;
  esac
}

unit_additional_buffer_type_valid() {
  # True (0) if $2 is one of $1's unit_additional_buffer_options.
  local installation_type="$1" buffer_type="$2" opt
  for opt in $(unit_additional_buffer_options "$installation_type"); do
    [ "$opt" == "$buffer_type" ] && return 0
  done
  return 1
}

unit_additional_buffer_type_after_transition() {
  local old_type="$1" new_type="$2" current="$3"
  if ! unit_additional_buffer_supported "$(unit_key_for_type "$new_type")"; then
    printf 'None'
    return
  fi
  if ! unit_additional_buffer_supported "$(unit_key_for_type "$old_type")" \
    || ! unit_additional_buffer_type_valid "$new_type" "$current"; then
    unit_additional_buffer_default "$new_type"
    return
  fi
  printf '%s' "$current"
}

unit_key_for_type() {
  # Echoes the registry key for a given installation_type, or nothing if
  # the type is unknown.
  printf '%s' "${UNIT_KEY[$1]:-}"
}

unit_additional_default_name() {
  # Echoes the default unit name for the "add additional unit" prompt: the
  # lowest free "<prefix>_N" for type $1, probed against what's already in
  # $afc_config_dir.
  local template prefix n probe
  template="${UNIT_ADDITIONAL_DEFAULT_NAME[$1]:-Unit_1}"
  prefix="${template%_*}"
  n=1
  while true; do
    if [ "$1" == "HTLF" ]; then
      probe="${afc_config_dir}/AFC_$(htlf_normalize_board_type "$htlf_board_type")_${prefix}_${n}.cfg"
    else
      probe="${afc_config_dir}/AFC_${prefix}_${n}.cfg"
    fi
    [ -f "$probe" ] || break
    n=$((n + 1))
  done
  printf '%s_%s' "$prefix" "$n"
}

# NOTE on these dispatchers: install-afc.sh runs under `set -e`, and each of
# these is called as a bare statement (not inside an `if`/`&&`) by its
# caller. So "no adapter registered for this type" must return 0, the same
# way an unmatched branch in the old if/elif chains fell through with
# nothing to do -- returning the failure status of a missing `declare -F`
# would abort the whole script instead of just skipping.

unit_copy_files() {
  # Copies this unit's template/mcu files for a first-time install of
  # $installation_type. No-op if the type has no adapter registered.
  local key fn
  key="$(unit_key_for_type "$1")"
  [ -n "$key" ] || return 0
  fn="unit_copy_files_${key}"
  if declare -F "$fn" >/dev/null; then
    "$fn"
  fi
}

unit_install_additional() {
  # Copies this unit's template/mcu files when adding $installation_type as
  # an *additional* unit to an existing install. No-op if unsupported for
  # this type (matches the pre-registry behavior, where types without a
  # matching elif branch were silently skipped).
  local key fn
  key="$(unit_key_for_type "$1")"
  [ -n "$key" ] || return 0
  fn="unit_install_additional_${key}"
  if declare -F "$fn" >/dev/null; then
    "$fn"
  fi
}

unit_set_buffer_target() {
  # Sets the buffer_* globals for $installation_type. Leaves them blank
  # (the "no unit target known" case) if this type doesn't support a
  # PSF-style buffer.
  local key fn
  key="$(unit_key_for_type "$1")"
  [ -n "$key" ] || return 0
  fn="unit_buffer_target_${key}"
  if declare -F "$fn" >/dev/null; then
    "$fn"
  fi
}

unit_append_message() {
  # Appends any type-specific follow-up instructions to $message.
  local key fn
  key="$(unit_key_for_type "$1")"
  [ -n "$key" ] || return 0
  fn="unit_message_${key}"
  if declare -F "$fn" >/dev/null; then
    "$fn"
  fi
}

unit_print_install_menu_options() {
  # Prints the extra option row(s) install_menu.sh shows for
  # $installation_type (board/motor/name selectors like "D. HTLF Board
  # Type"). No-op if this type has nothing beyond the common options.
  local key fn
  key="$(unit_key_for_type "$1")"
  [ -n "$key" ] || return 0
  fn="unit_install_menu_options_${key}"
  if declare -F "$fn" >/dev/null; then
    "$fn"
  fi
}

unit_print_additional_menu_row() {
  # In additional_system_menu.sh: resets boxturtle_name to this type's
  # additional-unit default (unless already renamed) and prints the "1.
  # <Type> Name: ..." row plus any board/motor rows for $installation_type.
  local key fn
  key="$(unit_key_for_type "$1")"
  [ -n "$key" ] || return 0
  fn="unit_additional_menu_row_${key}"
  if declare -F "$fn" >/dev/null; then
    "$fn"
  fi
}

# ─── BoxTurtle (4-Lane) ──────────────────────────────────────────────────────

unit_copy_files_BoxTurtle_4Lane() {
  safe_copy "${afc_path}/config/mcu/AFC_Lite.cfg" "${afc_config_dir}/mcu/AFC_Lite.cfg"
  safe_copy "${afc_path}/templates/AFC_Hardware-AFC.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
  safe_copy "${afc_path}/templates/AFC_Turtle_1.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
}

unit_install_additional_BoxTurtle_4Lane() {
  safe_copy "${afc_path}/templates/AFC_Turtle_1.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
  find "$afc_config_dir/AFC_${boxturtle_name}.cfg" -type f -exec sed -i "s/Turtle_1/$boxturtle_name/g" {} +
  safe_copy "${afc_path}/config/mcu/AFC_Lite.cfg" "${afc_config_dir}/mcu/AFC_${boxturtle_name}_mcu.cfg"
  sed -i "s/include mcu\/AFC_Lite.cfg/include mcu\/AFC_${boxturtle_name}_mcu.cfg/g" "${afc_config_dir}"/AFC_"${boxturtle_name}".cfg
}

unit_buffer_target_boxturtle_common() {
  buffer_unit_name="$boxturtle_name"
  buffer_unit_section_prefix="AFC_BoxTurtle"
  buffer_extruder_file="${afc_config_dir}/AFC_${boxturtle_name}.cfg"
  buffer_section_name="$boxturtle_name"
}
unit_buffer_target_BoxTurtle_4Lane() { unit_buffer_target_boxturtle_common; }

unit_message_boxturtle_common() {
  message+="""
- Ensure you enter either your CAN bus or serial information in the ${afc_config_dir}/AFC_${boxturtle_name}.cfg file
  """
}
unit_message_BoxTurtle_4Lane() { unit_message_boxturtle_common; }

unit_install_menu_options_boxturtle_common() {
  printf "C. BoxTurtle Name : %s \n" "$boxturtle_name"
}
unit_install_menu_options_BoxTurtle_4Lane() { unit_install_menu_options_boxturtle_common; }

unit_additional_menu_row_boxturtle_common() {
  if [ "$turtle_renamed" != "True" ]; then
    boxturtle_name="$(unit_additional_default_name "$installation_type")"
  fi
  printf "1. BoxTurtle Name: %s \n" "$boxturtle_name"
}
unit_additional_menu_row_BoxTurtle_4Lane() { unit_additional_menu_row_boxturtle_common; }

# ─── BoxTurtle (8-Lane) ──────────────────────────────────────────────────────

unit_copy_files_BoxTurtle_8Lane() {
  safe_copy "${afc_path}/config/mcu/AFC_Pro.cfg" "${afc_config_dir}/mcu/AFC_Pro.cfg"
  safe_copy "${afc_path}/templates/AFC_Hardware-AFC.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
  safe_copy "${afc_path}/templates/AFC_Pro_Turtle_1.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
}

unit_install_additional_BoxTurtle_8Lane() {
  safe_copy "${afc_path}/templates/AFC_Pro_Turtle_1.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
  find "$afc_config_dir/AFC_${boxturtle_name}.cfg" -type f -exec sed -i "s/Turtle_1/$boxturtle_name/g" {} +
  safe_copy "${afc_path}/config/mcu/AFC_Pro.cfg" "${afc_config_dir}/mcu/AFC_${boxturtle_name}_mcu.cfg"
  sed -i "s/include mcu\/AFC_Pro.cfg/include mcu\/AFC_${boxturtle_name}_mcu.cfg/g" "${afc_config_dir}"/AFC_"${boxturtle_name}".cfg
}

unit_buffer_target_BoxTurtle_8Lane() { unit_buffer_target_boxturtle_common; }

unit_message_BoxTurtle_8Lane() { unit_message_boxturtle_common; }

unit_install_menu_options_BoxTurtle_8Lane() { unit_install_menu_options_boxturtle_common; }

unit_additional_menu_row_BoxTurtle_8Lane() { unit_additional_menu_row_boxturtle_common; }

# ─── NightOwl ────────────────────────────────────────────────────────────────

unit_copy_files_NightOwl() {
  safe_copy "${afc_path}/config/mcu/ERB_2.0.cfg" "${afc_config_dir}/mcu/ERB_2.0.cfg"
  safe_copy "${afc_path}/templates/AFC_Hardware-NightOwl.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
  safe_copy "${afc_path}/templates/AFC_NightOwl_1.cfg" "${afc_config_dir}/AFC_NightOwl_1.cfg"
}

unit_install_additional_NightOwl() {
  safe_copy "${afc_path}/templates/AFC_NightOwl_1.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
  safe_copy "${afc_path}/config/mcu/ERB_2.0.cfg" "${afc_config_dir}/mcu/"
  sed -i \
    -e "s/AFC_NightOwl/AFC_NIGHTOWL_CLASS_PLACEHOLDER/g" \
    -e "s/NightOwl/$boxturtle_name/g" \
    -e "s/AFC_NIGHTOWL_CLASS_PLACEHOLDER/AFC_NightOwl/g" \
    "$afc_config_dir/AFC_${boxturtle_name}.cfg"
}

unit_buffer_target_NightOwl() {
  buffer_unit_section_prefix="AFC_NightOwl"
  if [ "$is_additional_unit" == "True" ]; then
    buffer_unit_name="$boxturtle_name"
    buffer_extruder_file="${afc_config_dir}/AFC_${boxturtle_name}.cfg"
    buffer_section_name="$boxturtle_name"
  else
    buffer_unit_name="NightOwl"
    buffer_extruder_file="${afc_config_dir}/AFC_NightOwl_1.cfg"
    buffer_section_name="NightOwl"
  fi
}

unit_message_NightOwl() {
  message+="""
- Ensure you enter either your CAN bus or serial information in the ${afc_config_dir}/AFC_NightOwl_1.cfg file
  """
}

unit_additional_menu_row_NightOwl() {
  if [ "$turtle_renamed" != "True" ]; then
    boxturtle_name="$(unit_additional_default_name "$installation_type")"
  fi
  printf "1. NightOwl Name: %s \n" "$boxturtle_name"
}

# ─── HTLF ────────────────────────────────────────────────────────────────────

htlf_normalize_board_type() {
  # HTLF's MMB_1.0/MMB_1.1 board types share a single "MMB" template; every
  # other board type's template file matches its own name. Echoes the
  # normalized value -- callers do `board_type="$(htlf_normalize_board_type "$htlf_board_type")"`.
  # Single source of truth for what used to be three independent copies of
  # this collapse (file copying, buffer targeting, the post-install message).
  local board_type="$1"
  if [ "$board_type" == "MMB_1.0" ] || [ "$board_type" == "MMB_1.1" ]; then
    board_type="MMB"
  fi
  printf '%s' "$board_type"
}

unit_copy_files_HTLF() {
  local board_type="$htlf_board_type"
  safe_copy "${afc_path}/config/mcu/HTLF_${board_type}.cfg" "${afc_config_dir}/mcu/"
  board_type="$(htlf_normalize_board_type "$board_type")"
  safe_copy "${afc_path}/templates/AFC_HTLF_1-${board_type}.cfg" "${afc_config_dir}/AFC_${board_type}_${boxturtle_name}.cfg"
  safe_copy "${afc_path}/templates/AFC_Hardware-HTLF.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
}

unit_install_additional_HTLF() {
  local board_type="$htlf_board_type"
  mkdir -p "${afc_config_dir}/mcu"
  safe_copy "${afc_path}/config/mcu/HTLF_${board_type}.cfg" "${afc_config_dir}/mcu/"
  board_type="$(htlf_normalize_board_type "$board_type")"
  safe_copy "${afc_path}/templates/AFC_HTLF_1-${board_type}.cfg" "${afc_config_dir}/AFC_${board_type}_${boxturtle_name}.cfg"
  sed -i "s/HTLF_1/$boxturtle_name/g" "${afc_config_dir}/AFC_${board_type}_${boxturtle_name}.cfg"
}

unit_buffer_target_HTLF() {
  local board_type
  board_type="$(htlf_normalize_board_type "$htlf_board_type")"
  buffer_unit_section_prefix="AFC_HTLF"
  buffer_extruder_file="${afc_config_dir}/AFC_${board_type}_${boxturtle_name}.cfg"
  if [ "$is_additional_unit" == "True" ]; then
    buffer_unit_name="$boxturtle_name"
    buffer_section_name="$boxturtle_name"
  else
    buffer_unit_name="HTLF_1"
    buffer_section_name="HTLF_1"
  fi
}

unit_message_HTLF() {
  local htlf_msg_board_type
  htlf_msg_board_type="$(htlf_normalize_board_type "$htlf_board_type")"
  message+="""
- Ensure you enter either your CAN bus or serial information in the ${afc_config_dir}/AFC_${htlf_msg_board_type}_${boxturtle_name}.cfg file.

- Ensure you modify the ${afc_config_dir}/AFC_${htlf_msg_board_type}_${boxturtle_name}.cfg file to select the proper rotation distance
  and gear ratio for your stepper motors.

- Ensure you update any necessary buffer information in the ${afc_config_dir}/AFC_${htlf_msg_board_type}_${boxturtle_name}.cfg file
  """
}

unit_install_menu_options_HTLF() {
  # HTLF's name genuinely ends up in its filename and config (see
  # copy_unit_files), unlike NightOwl/QuattroBox/OpenAMS/ViViD, so it's
  # worth showing here even though "C" isn't in this type's letter (it
  # already worked before this row existed -- name_unit() doesn't care
  # about installation_type -- this just makes it visible).
  printf "C. HTLF Name : %s \n" "$boxturtle_name"
  printf "D. HTLF Board Type : %s \n" "$htlf_board_type"
}

unit_additional_menu_row_HTLF() {
  if [ "$turtle_renamed" != "True" ]; then
    boxturtle_name="$(unit_additional_default_name "$installation_type")"
  fi
  printf "1. HTLF Name: %s \n" "$boxturtle_name"
  printf "D. HTLF Board Type : %s \n" "$htlf_board_type"
}

# ─── Claymore ────────────────────────────────────────────────────────────────

unit_copy_files_Claymore() {
  local board_type="$htlf2_board_type"
  # "Turtle_1" is the generic install-time default (set by name_unit()) --
  # if the user never customized the name, keep the sensible "Claymore_1"
  # default instead of shipping a BoxTurtle-flavored name. If they did
  # customize it, honor it instead of silently discarding it, renaming the
  # template's internal section (not just the filename) the same way
  # unit_install_additional_Claymore already does for a second unit.
  if [ "$boxturtle_name" == "Turtle_1" ]; then
    boxturtle_name="Claymore_1"
  fi
  safe_copy "${afc_path}/config/mcu/AFC_Lite_Claymore.cfg" "${afc_config_dir}/mcu/"
  safe_copy "${afc_path}/templates/AFC_Claymore_1-${board_type}.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
  if [ "$boxturtle_name" != "Claymore_1" ]; then
    sed -i "s/Claymore_1/$boxturtle_name/g" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
  fi
  safe_copy "${afc_path}/templates/AFC_Hardware-HTLF.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
}

unit_install_additional_Claymore() {
  local board_type="$htlf2_board_type"
  mkdir -p "${afc_config_dir}/mcu"
  safe_copy "${afc_path}/config/mcu/AFC_Lite_Claymore.cfg" "${afc_config_dir}/mcu/"
  safe_copy "${afc_path}/templates/AFC_Claymore_1-${board_type}.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
  sed -i "s/Claymore_1/$boxturtle_name/g" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
}

unit_buffer_target_Claymore() {
  buffer_unit_name="$boxturtle_name"
  buffer_unit_section_prefix="AFC_Claymore"
  buffer_extruder_file="${afc_config_dir}/AFC_${boxturtle_name}.cfg"
  buffer_prebaked_header="[AFC_buffer Claymore_buffer]"
  buffer_section_name="Claymore_buffer"
}

unit_message_Claymore() {
  message+="""
- Ensure you enter either your CAN bus or serial information in the ${afc_config_dir}/AFC_${boxturtle_name}.cfg file.

- Ensure you update any necessary buffer information in the ${afc_config_dir}/AFC_${boxturtle_name}.cfg file
  """
}

unit_install_menu_options_Claymore() {
  printf "C. Claymore Name : %s \n" "$boxturtle_name"
  printf "H. Claymore Board Type : %s \n" "$htlf2_board_type"
}

unit_additional_menu_row_Claymore() {
  if [ "$turtle_renamed" != "True" ]; then
    boxturtle_name="$(unit_additional_default_name "$installation_type")"
  fi
  printf "1. Claymore Name: %s \n" "$boxturtle_name"
  printf "H. Claymore Board Type : %s \n" "$htlf2_board_type"
}

# ─── QuattroBox ──────────────────────────────────────────────────────────────

unit_copy_files_QuattroBox() {
  safe_copy "${afc_path}/templates/AFC_Hardware-QuattroBox.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
  safe_copy "${afc_path}/templates/qb_macros/Eject_buttons.cfg" "${afc_config_dir}/macros/Eject_buttons.cfg"
  if [ "${qb_motor_type}" == "NEMA_14" ]; then
    safe_copy "${afc_path}/templates/AFC_QuattroBox_14.cfg" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
    if [ "${qb_board_type}" == "MMB_1.0" ]; then
      safe_copy "${afc_path}/config/mcu/MMB_1.0_QB.cfg" "${afc_config_dir}/mcu/"
      sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.0_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
    elif [ "${qb_board_type}" == "MMB_1.1" ]; then
      safe_copy "${afc_path}/config/mcu/MMB_1.1_QB.cfg" "${afc_config_dir}/mcu/"
      sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.1_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
    elif [ "${qb_board_type}" == "MMB_2.0" ]; then
      safe_copy "${afc_path}/config/mcu/MMB_2.0_QB.cfg" "${afc_config_dir}/mcu/"
      sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_2.0_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
    fi
  elif [ "${qb_motor_type}" == "NEMA_17" ]; then
    safe_copy "${afc_path}/templates/AFC_QuattroBox_17.cfg" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
    if [ "${qb_board_type}" == "MMB_1.0" ]; then
      safe_copy "${afc_path}/config/mcu/MMB_1.0_QB.cfg" "${afc_config_dir}/mcu/"
      sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.0_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
    elif [ "${qb_board_type}" == "MMB_1.1" ]; then
      safe_copy "${afc_path}/config/mcu/MMB_1.1_QB.cfg" "${afc_config_dir}/mcu/"
      sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.1_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
    elif [ "${qb_board_type}" == "MMB_2.0" ]; then
      safe_copy "${afc_path}/config/mcu/MMB_2.0_QB.cfg" "${afc_config_dir}/mcu/"
      sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_2.0_QB.cfg/g" "${afc_config_dir}/AFC_QuattroBox_1.cfg"
    fi
  fi
}

unit_install_additional_QuattroBox() {
  mkdir -p "${afc_config_dir}/macros"
  mkdir -p "${afc_config_dir}/mcu"
  safe_copy "${afc_path}/templates/qb_macros/Eject_buttons.cfg" "${afc_config_dir}/macros/Eject_buttons.cfg"
  sed -i "s/QuattroBox_1/${boxturtle_name}/g" "${afc_config_dir}/macros/Eject_buttons.cfg"
  if [ "${qb_motor_type}" == "NEMA_14" ]; then
    safe_copy "${afc_path}/templates/AFC_QuattroBox_14.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
    if [ "${qb_board_type}" == "MMB_1.0" ]; then
      sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.0_QB_${boxturtle_name}.cfg/g" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
      safe_copy "${afc_path}/config/mcu/MMB_1.0_QB.cfg" "${afc_config_dir}/mcu/MMB_1.0_QB_${boxturtle_name}.cfg"
      sed -i "s/mcu: QuattroBox_1/mcu: ${boxturtle_name}/g" "${afc_config_dir}/mcu/MMB_1.0_QB_${boxturtle_name}.cfg"
    elif [ "${qb_board_type}" == "MMB_1.1" ]; then
      sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.1_QB_${boxturtle_name}.cfg/g" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
      safe_copy "${afc_path}/config/mcu/MMB_1.1_QB.cfg" "${afc_config_dir}/mcu/MMB_1.1_QB_${boxturtle_name}.cfg"
      sed -i "s/mcu: QuattroBox_1/mcu: ${boxturtle_name}/g" "${afc_config_dir}/mcu/MMB_1.1_QB_${boxturtle_name}.cfg"
    elif [ "${qb_board_type}" == "MMB_2.0" ]; then
      sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_2.0_QB_${boxturtle_name}.cfg/g" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
      safe_copy "${afc_path}/config/mcu/MMB_2.0_QB.cfg" "${afc_config_dir}/mcu/MMB_2.0_QB_${boxturtle_name}.cfg"
      sed -i "s/mcu: QuattroBox_1/mcu: ${boxturtle_name}/g" "${afc_config_dir}/mcu/MMB_2.0_QB_${boxturtle_name}.cfg"
    fi
  elif [ "${qb_motor_type}" == "NEMA_17" ]; then
    safe_copy "${afc_path}/templates/AFC_QuattroBox_17.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
    if [ "${qb_board_type}" == "MMB_1.0" ]; then
      sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.0_QB_${boxturtle_name}.cfg/g" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
      safe_copy "${afc_path}/config/mcu/MMB_1.0_QB.cfg" "${afc_config_dir}/mcu/MMB_1.0_QB_${boxturtle_name}.cfg"
      sed -i "s/mcu: QuattroBox_1/mcu: ${boxturtle_name}/g" "${afc_config_dir}/mcu/MMB_1.0_QB_${boxturtle_name}.cfg"
    elif [ "${qb_board_type}" == "MMB_1.1" ]; then
      sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_1.1_QB_${boxturtle_name}.cfg/g" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
      safe_copy "${afc_path}/config/mcu/MMB_1.1_QB.cfg" "${afc_config_dir}/mcu/MMB_1.1_QB_${boxturtle_name}.cfg"
      sed -i "s/mcu: QuattroBox_1/mcu: ${boxturtle_name}/g" "${afc_config_dir}/mcu/MMB_1.1_QB_${boxturtle_name}.cfg"
    elif [ "${qb_board_type}" == "MMB_2.0" ]; then
      sed -i "s/include mcu\/MMB_QB.cfg/include mcu\/MMB_2.0_QB_${boxturtle_name}.cfg/g" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
      safe_copy "${afc_path}/config/mcu/MMB_2.0_QB.cfg" "${afc_config_dir}/mcu/MMB_2.0_QB_${boxturtle_name}.cfg"
      sed -i "s/mcu: QuattroBox_1/mcu: ${boxturtle_name}/g" "${afc_config_dir}/mcu/MMB_2.0_QB_${boxturtle_name}.cfg"
    fi
  fi
  find "$afc_config_dir/AFC_${boxturtle_name}.cfg" -type f -exec sed -i "s/QuattroBox_1/$boxturtle_name/g" {} +
}

unit_buffer_target_QuattroBox() {
  buffer_unit_section_prefix="AFC_QuattroBox"
  if [ "$is_additional_unit" == "True" ]; then
    buffer_unit_name="$boxturtle_name"
    buffer_extruder_file="${afc_config_dir}/AFC_${boxturtle_name}.cfg"
    buffer_prebaked_header="[AFC_buffer ${boxturtle_name}]"
    buffer_section_name="$boxturtle_name"
  else
    buffer_unit_name="QuattroBox_1"
    buffer_extruder_file="${afc_config_dir}/AFC_QuattroBox_1.cfg"
    buffer_prebaked_header="[AFC_buffer QuattroBox_1]"
    buffer_section_name="QuattroBox_1"
  fi
}

unit_message_QuattroBox() {
  message+="""
- You must update the ${afc_config_dir}/AFC_QuattroBox_1.cfg file to reference the proper buffer configuration and pins.

- Ensure you enter either your CAN bus or serial information in the ${afc_config_dir}/AFC_QuattroBox_1.cfg file
  """
}

unit_install_menu_options_QuattroBox() {
  printf "E. QuattroBox Board Type : %s \n" "$qb_board_type"
  printf "F. QuattroBox Motor Type : %s \n" "$qb_motor_type"
}

unit_additional_menu_row_QuattroBox() {
  if [ "$turtle_renamed" != "True" ]; then
    boxturtle_name="$(unit_additional_default_name "$installation_type")"
  fi
  printf "1. QuattroBox Name: %s \n" "$boxturtle_name"
  printf "E. QuattroBox Board Type : %s \n" "$qb_board_type"
  printf "F. QuattroBox Motor Type : %s \n" "$qb_motor_type"
}

# ─── OpenAMS ─────────────────────────────────────────────────────────────────

unit_copy_files_OpenAMS() {
  safe_copy "${afc_path}/templates/AFC_Hardware-AFC.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
  safe_copy "${afc_path}/templates/AFC_AMS_1.cfg" "${afc_config_dir}/AFC_AMS_1.cfg"
}

unit_install_additional_OpenAMS() {
  mkdir -p "${afc_config_dir}/macros"
  safe_copy "${afc_path}/templates/AFC_AMS_1.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
  sed -i "s/AMS_1/${boxturtle_name}/g" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
}

unit_buffer_target_OpenAMS() {
  buffer_unit_section_prefix="AFC_OpenAMS"
  if [ "$is_additional_unit" == "True" ]; then
    buffer_unit_name="$boxturtle_name"
    buffer_extruder_file="${afc_config_dir}/AFC_${boxturtle_name}.cfg"
    buffer_section_name="$boxturtle_name"
  else
    buffer_unit_name="AMS_1"
    buffer_extruder_file="${afc_config_dir}/AFC_AMS_1.cfg"
    buffer_section_name="AMS_1"
  fi
}

unit_message_OpenAMS() {
  message+="""
- Review and update the ${afc_config_dir}/AFC_AMS_1.cfg file for your AMS unit settings.

- Ensure OpenAMS is properly installed and configured per their instructions.
  """
}

unit_additional_menu_row_OpenAMS() {
  if [ "$turtle_renamed" != "True" ]; then
    boxturtle_name="$(unit_additional_default_name "$installation_type")"
  fi
  printf "1. OpenAMS Name: %s \n" "$boxturtle_name"
}

# ─── ViViD ───────────────────────────────────────────────────────────────────
# NOTE: no unit_buffer_target_ViViD adapter -- FPS_PSF buffers are not
# currently supported by default for ViViD units. Leaving buffer_unit_name
# unset triggers the "no unit target known" warning and skips applying the
# buffer, matching pre-registry behavior.

unit_copy_files_ViViD() {
  safe_copy "${afc_path}/templates/AFC_Vivid_1.cfg" "${afc_config_dir}/AFC_Vivid_1.cfg"
  safe_copy "${afc_path}/templates/AFC_Hardware-AFC.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
  safe_copy "${afc_path}/config/mcu/Vivid.cfg" "${afc_config_dir}/mcu/Vivid_1.cfg"
}

unit_install_additional_ViViD() {
  safe_copy "${afc_path}/templates/AFC_Vivid_1.cfg" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
  safe_copy "${afc_path}/config/mcu/Vivid.cfg" "${afc_config_dir}/mcu/${boxturtle_name}.cfg"
  find "$afc_config_dir/AFC_${boxturtle_name}.cfg" -type f -exec sed -i "s/Vivid_1/$boxturtle_name/g" {} +
  sed -i "s/include mcu\/Vivid_1.cfg/include mcu\/${boxturtle_name}.cfg/g" "${afc_config_dir}/AFC_${boxturtle_name}.cfg"
  sed -i "s/Vivid_1/$boxturtle_name/g" "${afc_config_dir}/mcu/${boxturtle_name}.cfg"
}

unit_message_ViViD() {
  message+="""
- Ensure you enter your serial information in the ${afc_config_dir}/AFC_Vivid_1.cfg file

- Review the ${afc_config_dir}/AFC_Vivid_1.cfg file to reference the proper buffer configuration and pins.
  """
}

unit_additional_menu_row_ViViD() {
  if [ "$turtle_renamed" != "True" ]; then
    boxturtle_name="$(unit_additional_default_name "$installation_type")"
  fi
  printf "1. ViViD Name: %s \n" "$boxturtle_name"
}

# ─── EMU ─────────────────────────────────────────────────────────────────────

unit_copy_files_EMU() {
  # "Turtle_1" is the generic install-time default (set by name_unit()) --
  # if the user never customized the name, default to "EMU_1" instead of a
  # BoxTurtle-flavored name. generate_emu_config() fully parametrizes the
  # generated config by unit name already, so a custom name just works.
  if [ "$boxturtle_name" == "Turtle_1" ]; then
    export boxturtle_name="EMU_1"
  fi
  safe_copy "${afc_path}/templates/AFC_Hardware-AFC.cfg" "${afc_config_dir}/AFC_Hardware.cfg"
  generate_emu_config "$boxturtle_name" "$emu_num_lanes"
}

unit_install_additional_EMU() {
  generate_emu_config "$boxturtle_name" "$emu_num_lanes"
}

unit_buffer_target_EMU() {
  buffer_unit_name="$boxturtle_name"
  buffer_unit_section_prefix="AFC_EMU"
  buffer_extruder_file="${afc_config_dir}/AFC_${boxturtle_name}.cfg"
  buffer_prebaked_header="[AFC_buffer ${boxturtle_name}_buffer]"
  buffer_section_name="${boxturtle_name}_buffer"
}

unit_message_EMU() {
  message+="""
- Ensure you enter either your CAN bus or serial information for each lane in the ${afc_config_dir}/AFC_${boxturtle_name}.cfg file

- The MCU board_pins configuration is at ${afc_config_dir}/mcu/${boxturtle_name}.cfg
  """
}

unit_install_menu_options_EMU() {
  printf "C. EMU Name : %s \n" "$boxturtle_name"
  printf "G. EMU Lane Count : %s \n" "$emu_num_lanes"
  printf "H. EMU Board Type : %s \n" "$emu_board_type"
}

unit_additional_menu_row_EMU() {
  if [ "$turtle_renamed" != "True" ]; then
    boxturtle_name="$(unit_additional_default_name "$installation_type")"
  fi
  printf "1. EMU Name: %s \n" "$boxturtle_name"
  printf "G. EMU Lane Count : %s \n" "$emu_num_lanes"
  printf "H. EMU Board Type : %s \n" "$emu_board_type"
}
