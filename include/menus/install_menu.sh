#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

cycle_array() {
  local -n arr=$1
  local -n index=$2
  local var_name=$3
  index=$(( (index + 1) % ${#arr[@]} ))
  printf -v "$var_name" "%s" "${arr[$index]}"
  message="$4: ${!var_name}"
}

buffer_type_label() {
  # Maps the internal buffer_type token to a user-facing display label.
  case "$1" in
    "TurtleNeck2Remastered") echo "TurtleNeck 2 Remastered" ;;
    "FPS_PSF") echo "Proportional Sync-Feedback(PSF)" ;;
    *) echo "$1" ;;
  esac
}

toggle_option() {
  local var_name=$1
  local label=$2
  local current
  local new_value
  local status

  current="${!var_name}"

  if [ "$current" == "True" ]; then
    new_value="False"
    status="Disabled"
  else
    new_value="True"
    status="Enabled"
  fi

  printf -v "$var_name" "%s" "$new_value"
  message="$label $status"
}

install_menu() {
  local message choice counter board_counter motor index i
  local toggle_items toggle_labels
  counter=0
  board_counter=0
  motor=0

  toggle_items=("afc_includes" "tip_forming" "toolhead_cutter" "hub_cutter" "kick_macro" "park_macro" "poop_macro" "wipe_macro")
  toggle_labels=("Add AFC includes?" "Enable tip-forming?" "Enable toolhead cutter?" "Enable hub cutter?" "Enable kick macro?" "Enable park macro?" "Enable poop macro?" "Enable wipe macro?")


  while true; do
    clear
    print_unit_art "$installation_type"


    printf "%b▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄ \n" "$MENU_GREEN"
    printf "█%b                                    AFC Script Help      %b                            █\n" "$RESET" "$MENU_GREEN"
    printf "%b▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀%b \n" "$MENU_GREEN" "$RESET"
    printf "%b\n" "$message"
    printf "%b▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄ \n" "$MENU_GREEN"
    printf "█%b            Please review the following options to configure your system%b             █\n" "$RESET" "$MENU_GREEN"
    printf "█%b           Type a number or letter and press Enter/Return to toggle choice%b           █\n" "$RESET" "$MENU_GREEN"
    printf "%b▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀%b \n" "$MENU_GREEN" "$RESET"

    if [ "$files_updated_or_installed" == "False" ]; then
      printf "T. Installation Type: %s \n" "$installation_type"
      for i in "${!toggle_items[@]}"; do
        printf "%s. %s : %s\n" "$((i + 1))" "${toggle_labels[$i]}" "${!toggle_items[$i]}"
      done
      if [ "$installation_type" != "OpenAMS" ]; then
        printf "9. Use a toolhead sensor or ramming with a TurtleNeck buffer? : %s \n" "$toolhead_sensor"
      elif [ "$installation_type" == "OpenAMS" ]; then
        printf "9. Use a toolhead sensor or ramming with an FPS Board? : %s \n" "$toolhead_sensor"
      fi
      if [ "$toolhead_sensor" == "Sensor" ]; then
        if [ "$toolhead_sensor_pin" == "Unknown" ]; then
        printf "A. Toolhead sensor pin : ${RED}%s${RESET} \n" "$toolhead_sensor_pin"
      else
        printf "A. Toolhead sensor pin : %s \n" "$toolhead_sensor_pin"
      fi
      fi
      if [ "$installation_type" != "ViViD" ] && [ "$installation_type" != "OpenAMS" ]; then
        if [ "$toolhead_sensor" == "Ramming" ] && { [ "$buffer_type" == "Unknown" ] || [ "$buffer_type" == "None" ]; }; then
          printf "B. Buffer type : ${RED}%s${RESET} (a buffer is required with ramming) \n" "$buffer_type"
        else
          printf "B. Buffer type : %s \n" "$(buffer_type_label "$buffer_type")"
        fi
      fi
      unit_print_install_menu_options "$installation_type"
      printf "\n${BOLD_GREEN}I. Install system with current selections${RESET}\n"
    fi

    printf "M. Return to Main Menu\n"
    printf "Q. Quit\n"
    echo ""
    read -p "Enter your choice: " choice

    choice="${choice^^}"

    case $choice in
      T)
        cycle_array installation_options counter installation_type "Installation Type"
        if [ "$installation_type" == "ViViD" ]; then
          # FPS_PSF isn't supported for ViViD by default, don't leave a stale selection
          # displayed that would silently be skipped at install time.
          buffer_type="None"
        elif [ "$installation_type" == "OpenAMS" ]; then
          # OpenAMS's buffer isn't picked via B -- it follows the current
          # toolhead-sensor/ramming state (option 9) instead.
          if [ "$toolhead_sensor" == "Ramming" ]; then
            buffer_type="FPS_PSF"
          else
            buffer_type="None"
          fi
        fi
        ;;
      [1-8])
        index=$((choice - 1))
        toggle_option "${toggle_items[$index]}" "${toggle_labels[$index]}" ;;
      9)
        toolhead_sensor=$([ "$toolhead_sensor" == "Sensor" ] && echo "Ramming" || echo "Sensor")
        if [ "$toolhead_sensor" == "Sensor" ]; then
          message="Using toolhead sensor"
          # A buffer is optional with a sensor, don't leave it unselected.
          [ "$buffer_type" == "Unknown" ] && buffer_type="None"
          [ "$installation_type" == "OpenAMS" ] && buffer_type="None"
        elif [ "$installation_type" == "OpenAMS" ]; then
          message="Using ramming with an FPS board"
          buffer_type="FPS_PSF"
        else
          message="Using ramming with a TurtleNeck buffer"
          if [ "$installation_type" != "ViViD" ]; then
            # None isn't valid with ramming, force a fresh selection.
            [ "$buffer_type" == "None" ] && buffer_type="Unknown"
            [ "$buffer_type" == "Unknown" ] && message="$message. Select a buffer type with B"
          fi
        fi ;;
      A)
        read -p "Enter toolhead sensor pin (Example: nhk:gpio13): " toolhead_sensor_pin
        message="Toolhead sensor pin set to $toolhead_sensor_pin" ;;
      B)
        if [ "$installation_type" == "ViViD" ] || [ "$installation_type" == "OpenAMS" ]; then
          # Buffer type isn't a user choice for these currently.
          message="Buffer type is not selectable for $installation_type"
        else
          buffer_type=$(case "$buffer_type" in "TurtleNeck") echo "TurtleNeckV2";; "TurtleNeckV2") echo "TurtleNeck2Remastered";; "TurtleNeck2Remastered") echo "FPS_PSF";; "FPS_PSF") [ "$toolhead_sensor" == "Ramming" ] && echo "TurtleNeck" || echo "None";; "None"|*) echo "TurtleNeck";; esac)
          message="Buffer Type: $(buffer_type_label "$buffer_type")"
        fi ;;
      C)
        if [ "$installation_type" == "EMU" ]; then
          name_additional_unit
        else
          name_unit
        fi ;;
      D)
        cycle_array htlf_board_types board_counter htlf_board_type "HTLF Board Type" ;;
      E)
        cycle_array qb_board_types board_counter qb_board_type "QuattroBox Board Type" ;;
      F)
        cycle_array qb_motor_types motor qb_motor_type "QuattroBox Motor Type" ;;
      G)
        cycle_array emu_num_lanes_options emu_num_lanes_index emu_num_lanes "EMU Lane Count" ;;
      H)
        if [ "$installation_type" == "EMU" ]; then
          cycle_array emu_board_types board_counter emu_board_type "EMU Board Type"
        elif  [ "$installation_type" == "Claymore" ]; then
          cycle_array htlf2_board_types board_counter htlf2_board_type "Claymore Board Type"
        fi ;;
      I)
        if [ "$toolhead_sensor" == "Ramming" ] \
           && { [ "$buffer_type" == "Unknown" ] || [ "$buffer_type" == "None" ]; } \
           && [ "$installation_type" != "ViViD" ] && [ "$installation_type" != "OpenAMS" ]; then
          message="${RED}Ramming requires a buffer type. Select one with B${RESET}"
        else
          install_afc
        fi ;;
      M) main_menu ;;
      Q) exit_afc_install ;;
      *) echo "Invalid selection" ;;
    esac
  done
}
