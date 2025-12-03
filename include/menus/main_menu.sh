#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

main_menu() {
  local message
  local choice

   while true; do
    clear
    printf "\e[49m             \e[38;5;143;49m▄▄\e[38;5;143;48;5;143m▄\e[49;38;5;143m▀\e[38;5;143;48;5;143m▄\e[38;5;143;49m▄▄\e[49m             \e[m  \n"
    printf "\e[49m         \e[38;5;143;49m▄▄\e[38;5;143;48;5;143m▄▄\e[49;38;5;143m▀\e[38;5;29;48;5;143m▄\e[38;5;29;49m▄\e[38;5;29;48;5;29m▄\e[38;5;29;49m▄\e[38;5;29;48;5;143m▄\e[49;38;5;143m▀\e[38;5;143;48;5;143m▄▄\e[38;5;143;49m▄▄\e[49m         \e[m\n"
    printf "\e[49m      \e[38;5;143;49m▄\e[38;5;143;48;5;143m▄▄\e[49;38;5;143m▀\e[38;5;29;48;5;143m▄\e[38;5;29;49m▄\e[38;5;29;48;5;29m▄▄\e[48;5;29m     \e[38;5;29;48;5;29m▄▄\e[38;5;29;49m▄\e[38;5;29;48;5;143m▄\e[49;38;5;143m▀\e[38;5;143;48;5;143m▄▄\e[38;5;143;49m▄\e[49m      \e[m\n"
    printf "\e[49m  \e[38;5;143;49m▄▄\e[38;5;143;48;5;143m▄▄\e[49;38;5;143m▀\e[38;5;29;48;5;143m▄\e[38;5;29;49m▄\e[38;5;29;48;5;29m▄\e[48;5;29m   \e[38;5;29;48;5;29m▄▄\e[49;38;5;29m▀▀▀\e[38;5;29;48;5;29m▄▄\e[48;5;29m   \e[38;5;29;48;5;29m▄\e[38;5;29;49m▄\e[38;5;29;48;5;143m▄\e[49;38;5;143m▀\e[38;5;143;48;5;143m▄▄\e[38;5;143;49m▄▄\e[49m  \e[m  This installation script will install/update the AFC Klipper\n"
    printf "\e[38;5;143;48;5;143m▄▄\e[49;38;5;143m▀\e[38;5;29;48;5;143m▄\e[38;5;29;49m▄\e[38;5;29;48;5;29m▄▄\e[48;5;29m   \e[38;5;29;48;5;29m▄▄\e[49;38;5;29m▀▀\e[49m     \e[49;38;5;29m▀▀\e[38;5;29;48;5;29m▄▄\e[48;5;29m   \e[38;5;29;48;5;29m▄▄\e[38;5;29;49m▄\e[38;5;29;48;5;143m▄\e[49;38;5;143m▀\e[38;5;143;48;5;143m▄▄\e[m  extension to your system.\n"
    printf "\e[48;5;143m \e[49m \e[48;5;29m     \e[38;5;29;48;5;29m▄\e[49;38;5;29m▀▀\e[49m             \e[49;38;5;29m▀▀\e[38;5;29;48;5;29m▄\e[48;5;29m     \e[49m \e[48;5;143m \e[m  Discord: https://discord.gg/armoredturtle\n"
    printf "\e[48;5;143m \e[49m \e[48;5;29m    \e[49;38;5;29m▀\e[38;5;29;49m▄▄\e[38;5;29;48;5;29m▄▄▄▄\e[38;5;29;49m▄▄\e[49m   \e[38;5;29;49m▄▄\e[38;5;29;48;5;29m▄▄▄▄\e[38;5;29;49m▄▄\e[49;38;5;29m▀\e[48;5;29m    \e[49m \e[48;5;143m \e[m  Github: https://github.com/ArmoredTurtle\n"
    printf "\e[48;5;143m \e[49m \e[48;5;29m    \e[49m \e[49;38;5;29m▀▀\e[38;5;29;48;5;29m▄\e[48;5;29m \e[38;5;29;48;5;29m▄\e[48;5;29m   \e[38;5;29;48;5;29m▄▄▄\e[48;5;29m   \e[38;5;29;48;5;29m▄\e[48;5;29m \e[38;5;29;48;5;29m▄\e[49;38;5;29m▀▀\e[49m \e[48;5;29m    \e[49m \e[48;5;143m \e[m  Documentation: https://armoredturtle.xyz/\n"
    printf "\e[48;5;143m \e[49m \e[48;5;29m    \e[49m     \e[49;38;5;29m▀▀\e[48;5;29m      \e[38;5;29;48;5;29m▄\e[49;38;5;29m▀▀\e[49m     \e[48;5;29m    \e[49m \e[48;5;143m \e[m\n"
    if [ "$files_updated_or_installed" == "True" ]; then
      printf "\e[48;5;143m \e[49m \e[48;5;29m    \e[49m        \e[48;5;29m     \e[49m        \e[48;5;29m    \e[49m \e[48;5;143m \e[m  Prior AFC-Klipper-Add-On installation detected: $GREEN%s$RESET\n" $files_updated_or_installed
    elif [ "$prior_installation" == "True" ]; then
      printf "\e[48;5;143m \e[49m \e[48;5;29m    \e[49m        \e[48;5;29m     \e[49m        \e[48;5;29m    \e[49m \e[48;5;143m \e[m  Prior AFC-Klipper-Add-On installation detected: $GREEN%s$RESET\n" $prior_installation
    elif [ "$prior_installation" == "False" ]; then
      printf "\e[48;5;143m \e[49m \e[48;5;29m    \e[49m        \e[48;5;29m     \e[49m        \e[48;5;29m    \e[49m \e[48;5;143m \e[m  Prior AFC-Klipper-Add-On installation detected: $RED%s$RESET\n" $prior_installation
    fi
    printf "\e[48;5;143m \e[49m \e[48;5;29m    \e[49m        \e[48;5;29m     \e[49m        \e[48;5;29m    \e[49m \e[48;5;143m \e[m  \n"
    printf "\e[48;5;143m \e[49m \e[48;5;29m    \e[49m        \e[48;5;29m     \e[49m        \e[48;5;29m    \e[49m \e[48;5;143m \e[m  \n"
    printf "\e[48;5;143m \e[49m \e[48;5;29m    \e[49m        \e[48;5;29m     \e[49m        \e[48;5;29m    \e[49m \e[48;5;143m \e[m  1. Printer Config Directory : %s \n" $printer_config_dir
    printf "\e[48;5;143m \e[49m \e[48;5;29m    \e[49m        \e[48;5;29m     \e[49m        \e[48;5;29m    \e[49m \e[48;5;143m \e[m  2. Klipper Directory        : %s \n" $klipper_dir
    printf "\e[38;5;143;48;5;143m▄▄\e[38;5;143;49m▄\e[38;5;143;48;5;29m▄\e[49;38;5;29m▀\e[38;5;29;48;5;29m▄\e[49m        \e[48;5;29m     \e[49m        \e[38;5;29;48;5;29m▄\e[49;38;5;29m▀\e[38;5;143;48;5;29m▄\e[38;5;143;49m▄\e[38;5;143;48;5;143m▄▄\e[m  3. Moonraker Config File    : %s \n" $moonraker_config_file
    printf "\e[49m  \e[49;38;5;143m▀▀\e[38;5;143;48;5;143m▄▄\e[38;5;143;49m▄▄\e[49m      \e[48;5;29m     \e[49m      \e[38;5;143;49m▄▄\e[38;5;143;48;5;143m▄▄\e[49;38;5;143m▀▀\e[49m  \e[m  4. Klipper Service Name     : %s \n" $klipper_service
    printf "\e[49m      \e[49;38;5;143m▀\e[38;5;143;48;5;143m▄▄\e[38;5;143;49m▄▄\e[49m   \e[48;5;29m     \e[49m   \e[38;5;143;49m▄▄\e[38;5;143;48;5;143m▄▄\e[49;38;5;143m▀\e[49m      \e[m  5. Branch                   : %s \n" $branch
    printf "\e[49m         \e[49;38;5;143m▀▀\e[38;5;143;48;5;143m▄▄\e[38;5;143;49m▄▄\e[49;38;5;29m▀\e[38;5;29;48;5;29m▄\e[49;38;5;29m▀\e[38;5;143;49m▄▄\e[38;5;143;48;5;143m▄▄\e[49;38;5;143m▀▀\e[49m \e[m          6. Moonraker Address        : %s    \n" $moonraker
    printf "\e[49m             \e[49;38;5;143m▀▀\e[38;5;143;48;5;143m▄\e[38;5;143;49m▄\e[38;5;143;48;5;143m▄\e[49;38;5;143m▀▀\e[49m             \e[m  (Select a number to change the default value)\n"
    echo ""
    printf "${MENU_GREEN}▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄ \n"
    printf "█${RESET}                                    AFC Script Help      ${MENU_GREEN}                            █\n"
    printf "${MENU_GREEN}▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀${RESET} \n"
    printf "%b\n" "$message"
    printf "${MENU_GREEN}▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄ \n"
    printf "█${RESET}                                  AFC Install Options${MENU_GREEN}                                █\n"
    printf "█%b           Type a number or letter and press Enter/Return to toggle choice%b           █\n" "$RESET" "$MENU_GREEN"
    printf "${MENU_GREEN}▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀${RESET} \n"
    echo ""
    if [ "$prior_installation" == "False" ] && [ "$files_updated_or_installed" == "False" ]; then
      printf "I. Install New System\n"
    fi
    if [ "$prior_installation" == "True" ]; then
      printf "U. Update AFC Klipper Add-On\n"
    fi
    printf "A. Install Additional System\n"
    printf "C. Utilities\n"
    printf "R. Remove AFC Klipper Add-On\n"
    echo "Q. Exit"
    echo ""
    read -p "Enter your choice: " choice

    choice="${choice^^}"
    case $choice in
      1) export message="To change the printer config directory, please re-run this script with a '-m <path>' option." ;;
      2) export message="To change the klipper directory, please re-run this script with a '-k <path>' option." ;;
      3) export message="To change the moonraker config file, please re-run this script with a '-m <path>' option." ;;
      4) export message="To change the klipper service name, please re-run this script with a '-s <name>' option." ;;
      5) export message="To change the branch, please re-run this script with a '-b <branch>' option." ;;
      6) export message="To change the moonraker address, please re-run this script with a '-a <address>' option.\n"
         message+="To change the moonraker port, please re-run this script with a '-n <moonraker port>' option." ;;
      I) install_menu ;;
      U) update_menu ;;
      R) uninstall_afc ;;
      C) utilities_menu ;;
      A) additional_system_menu ;;
      Q) exit_afc_install ;;
      *) echo "Invalid choice" ;;
    esac
  done
}

