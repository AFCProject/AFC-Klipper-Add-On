#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

main_menu() {
  local message
  local choice

   while true; do
    clear
    printf "\e[49m                                                                                \e[m
    \e[49m            \e[38;5;75;48;5;81m▄\e[38;5;117;48;5;74m▄▄▄\e[38;5;117;48;5;81m▄\e[49m               \e[38;5;175;48;5;169m▄\e[38;5;175;48;5;168m▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄\e[49m         \e[38;5;11;49m▄\e[38;5;227;49m▄\e[38;5;227;48;5;220m▄▄▄▄▄▄▄▄▄▄▄\e[38;5;227;49m▄▄\e[38;5;220;49m▄\e[49m      \e[m
    \e[49m           \e[38;5;74;48;5;74m▄\e[38;5;81;48;5;117m▄\e[49;38;5;80m▀\e[49m \e[49;38;5;116m▀\e[38;5;81;48;5;117m▄\e[38;5;117;48;5;74m▄\e[49m              \e[48;5;175m \e[38;5;168;48;5;168m▄\e[49m             \e[48;5;168m \e[48;5;175m \e[49m        \e[38;5;227;48;5;220m▄\e[38;5;220;48;5;227m▄\e[49;38;5;220m▀\e[49m           \e[49;38;5;220m▀\e[38;5;220;48;5;227m▄\e[38;5;227;48;5;227m▄\e[38;5;220;49m▄\e[49m     \e[m
    \e[49m           \e[38;5;117;48;5;117m▄\e[38;5;80;48;5;74m▄\e[49m   \e[38;5;74;48;5;74m▄\e[38;5;117;48;5;117m▄\e[38;5;74;49m▄\e[49m             \e[48;5;175m \e[48;5;168m \e[49m \e[38;5;168;48;5;169m▄\e[38;5;175;48;5;175m▄\e[38;5;168;48;5;175m▄▄▄▄▄▄▄▄▄▄▄▄\e[49m        \e[48;5;227m \e[38;5;220;48;5;220m▄\e[49m  \e[38;5;227;48;5;11m▄\e[38;5;220;48;5;227m▄▄▄▄▄▄▄\e[38;5;11;48;5;227m▄\e[38;5;220;48;5;220m▄\e[49m  \e[48;5;227m \e[38;5;220;48;5;220m▄\e[49m     \e[m
    \e[49m          \e[38;5;117;48;5;74m▄\e[38;5;74;48;5;117m▄\e[49m \e[38;5;80;49m▄\e[38;5;153;48;5;74m▄\e[38;5;74;49m▄\e[49m \e[38;5;74;48;5;74m▄▄\e[49m             \e[48;5;175m \e[48;5;168m \e[49m \e[48;5;168m \e[38;5;175;48;5;175m▄\e[49m                    \e[48;5;227m \e[48;5;220m \e[49m \e[38;5;220;48;5;220m▄\e[48;5;227m \e[49m       \e[38;5;221;48;5;227m▄\e[38;5;227;48;5;11m▄\e[38;5;227;48;5;220m▄▄\e[38;5;11;48;5;227m▄\e[49;38;5;220m▀\e[49m     \e[m
    \e[49m         \e[38;5;74;49m▄\e[38;5;117;48;5;117m▄\e[49;38;5;80m▀\e[49m \e[38;5;117;48;5;74m▄\e[38;5;74;48;5;117m▄\e[38;5;117;48;5;117m▄\e[49m \e[49;38;5;74m▀\e[38;5;75;48;5;117m▄\e[38;5;74;48;5;74m▄\e[49m            \e[48;5;175m \e[48;5;168m \e[49m \e[48;5;168m \e[48;5;175m \e[49m                    \e[48;5;227m \e[48;5;220m \e[49m \e[48;5;220m \e[48;5;227m \e[49m                  \e[m
    \e[49m         \e[38;5;117;48;5;153m▄\e[38;5;80;48;5;81m▄\e[49m \e[38;5;74;49m▄\e[38;5;117;48;5;117m▄\e[38;5;116;48;5;74m▄\e[38;5;75;48;5;117m▄\e[38;5;74;48;5;74m▄\e[49m \e[38;5;74;48;5;117m▄\e[38;5;117;48;5;117m▄\e[49m            \e[48;5;175m \e[48;5;168m \e[49m \e[38;5;168;48;5;168m▄\e[38;5;175;48;5;175m▄\e[38;5;169;49m▄▄▄▄▄▄▄▄\e[49m            \e[48;5;227m \e[48;5;220m \e[49m \e[48;5;220m \e[48;5;227m \e[49m                  \e[m
    \e[49m        \e[38;5;74;48;5;74m▄\e[38;5;81;48;5;117m▄\e[49m  \e[38;5;117;48;5;153m▄\e[38;5;80;48;5;81m▄\e[49m \e[38;5;80;48;5;117m▄\e[38;5;117;48;5;117m▄\e[49m  \e[38;5;81;48;5;74m▄\e[38;5;117;48;5;74m▄\e[49m           \e[48;5;175m \e[48;5;168m \e[49m \e[49;38;5;168m▀\e[49;38;5;169m▀▀▀▀▀▀▀▀\e[38;5;175;48;5;175m▄\e[38;5;168;48;5;168m▄\e[49m           \e[48;5;227m \e[48;5;220m \e[49m \e[48;5;220m \e[48;5;227m \e[49m                  \e[m
    \e[49m       \e[38;5;80;49m▄\e[38;5;117;48;5;117m▄\e[49;38;5;74m▀\e[49m \e[38;5;74;48;5;74m▄\e[38;5;117;48;5;117m▄\e[38;5;117;48;5;74m▄▄▄\e[38;5;153;48;5;74m▄\e[38;5;74;48;5;74m▄\e[49m \e[49;38;5;74m▀\e[38;5;117;48;5;117m▄\e[38;5;74;49m▄\e[49m          \e[48;5;175m \e[48;5;168m \e[49m \e[38;5;168;49m▄\e[38;5;175;48;5;168m▄▄▄▄▄▄▄▄\e[38;5;169;48;5;175m▄\e[38;5;168;48;5;168m▄\e[49m           \e[48;5;227m \e[48;5;220m \e[49m \e[48;5;220m \e[48;5;227m \e[49m                  \e[m
    \e[49m       \e[38;5;117;48;5;74m▄\e[38;5;74;48;5;117m▄\e[49m           \e[38;5;74;48;5;81m▄\e[38;5;117;48;5;117m▄\e[49m          \e[48;5;175m \e[48;5;168m \e[49m \e[38;5;168;48;5;168m▄\e[38;5;175;48;5;175m▄\e[49;38;5;168m▀\e[49m                   \e[48;5;227m \e[48;5;220m \e[49m \e[48;5;220m \e[48;5;227m \e[49m                  \e[m
    \e[49m      \e[38;5;74;49m▄\e[38;5;117;48;5;117m▄\e[49;38;5;80m▀\e[49m \e[38;5;117;48;5;74m▄\e[38;5;74;48;5;117m▄▄▄▄▄▄▄\e[38;5;74;48;5;74m▄\e[49m \e[49;38;5;74m▀\e[38;5;75;48;5;117m▄\e[38;5;74;48;5;74m▄\e[49m         \e[48;5;175m \e[48;5;168m \e[49m \e[48;5;168m \e[48;5;175m \e[49m                    \e[48;5;227m \e[48;5;220m \e[49m \e[48;5;220m \e[48;5;227m \e[49m       \e[38;5;227;48;5;220m▄▄▄▄▄\e[38;5;220;49m▄\e[49m     \e[m
    \e[49m      \e[38;5;117;48;5;117m▄\e[38;5;80;48;5;74m▄\e[49m \e[38;5;74;49m▄\e[38;5;117;48;5;117m▄\e[49;38;5;74m▀\e[49m     \e[49;38;5;74m▀\e[38;5;75;48;5;117m▄\e[38;5;74;48;5;74m▄\e[49m \e[38;5;74;48;5;74m▄\e[38;5;117;48;5;117m▄\e[38;5;74;49m▄\e[49m        \e[48;5;175m \e[48;5;168m \e[49m \e[48;5;168m \e[48;5;175m \e[49m                    \e[48;5;227m \e[48;5;220m \e[49m \e[38;5;220;48;5;220m▄\e[38;5;11;48;5;227m▄\e[38;5;227;48;5;220m▄\e[38;5;227;49m▄▄▄▄▄\e[38;5;227;48;5;220m▄\e[38;5;227;48;5;227m▄\e[38;5;220;48;5;11m▄\e[49m \e[49;38;5;220m▀\e[48;5;227m \e[48;5;220m \e[49m     \e[m
    \e[49m     \e[38;5;81;48;5;74m▄\e[38;5;80;48;5;117m▄\e[49m \e[38;5;116;49m▄\e[38;5;74;48;5;81m▄▄\e[49m       \e[38;5;74;48;5;81m▄\e[38;5;117;48;5;117m▄\e[49m  \e[38;5;81;48;5;75m▄\e[38;5;117;48;5;74m▄\e[49m        \e[48;5;175m \e[38;5;168;48;5;168m▄\e[49m \e[38;5;168;48;5;168m▄\e[48;5;175m \e[49m                    \e[38;5;221;48;5;11m▄\e[38;5;227;48;5;220m▄\e[49m   \e[49;38;5;220m▀▀▀▀▀▀▀\e[49m   \e[38;5;220;49m▄\e[38;5;227;48;5;11m▄\e[49;38;5;220m▀\e[49m     \e[m
    \e[49m     \e[38;5;116;48;5;74m▄\e[38;5;81;48;5;74m▄\e[38;5;81;48;5;81m▄\e[38;5;81;48;5;75m▄\e[38;5;74;48;5;117m▄\e[49m         \e[38;5;153;48;5;117m▄\e[38;5;81;48;5;81m▄▄\e[38;5;81;48;5;74m▄\e[38;5;74;48;5;117m▄\e[49m        \e[38;5;169;48;5;175m▄\e[38;5;169;48;5;169m▄▄▄\e[38;5;168;48;5;175m▄\e[49m                    \e[49;38;5;220m▀\e[49;38;5;227m▀\e[38;5;220;48;5;227m▄\e[38;5;11;48;5;11m▄▄▄▄▄▄▄▄▄▄▄\e[38;5;220;48;5;11m▄\e[49;38;5;227m▀\e[49;38;5;220m▀\e[49m      \e[m
    \e[49m                                                                                \e[m
    \e[49m \e[38;5;61;48;5;61m▄\e[38;5;110;48;5;61m▄\e[38;5;146;48;5;61m▄▄\e[38;5;146;48;5;67m▄▄\e[38;5;152;48;5;67m▄▄\e[38;5;152;48;5;73m▄▄▄▄\e[38;5;152;48;5;109m▄\e[38;5;152;48;5;115m▄▄▄\e[38;5;152;48;5;114m▄\e[38;5;152;48;5;108m▄▄\e[38;5;151;48;5;108m▄▄▄\e[38;5;151;48;5;107m▄\e[38;5;151;48;5;71m▄▄▄\e[38;5;151;48;5;107m▄▄\e[38;5;187;48;5;113m▄▄\e[38;5;186;48;5;149m▄▄\e[38;5;186;48;5;148m▄\e[38;5;192;48;5;148m▄\e[38;5;192;48;5;184m▄▄\e[38;5;228;48;5;184m▄▄\e[38;5;228;48;5;11m▄▄\e[38;5;228;48;5;220m▄▄▄\e[38;5;222;48;5;220m▄▄\e[38;5;222;48;5;214m▄▄▄▄▄▄\e[38;5;222;48;5;208m▄▄▄\e[38;5;216;48;5;209m▄\e[38;5;217;48;5;209m▄\e[38;5;217;48;5;203m▄▄▄▄▄\e[38;5;217;48;5;204m▄▄\e[38;5;211;48;5;204m▄\e[38;5;212;48;5;162m▄▄▄\e[38;5;212;48;5;168m▄\e[38;5;176;48;5;168m▄▄\e[38;5;182;48;5;132m▄▄▄▄▄▄\e[38;5;182;48;5;97m▄\e[38;5;97;49m▄\e[49m \e[m
    \e[49m \e[49;38;5;61m▀▀▀▀\e[49;38;5;67m▀▀▀▀\e[49;38;5;73m▀▀▀▀\e[49;38;5;79m▀\e[49;38;5;115m▀▀▀▀\e[49;38;5;114m▀\e[49;38;5;78m▀▀▀\e[49;38;5;71m▀▀▀▀▀\e[49;38;5;107m▀\e[49;38;5;113m▀▀▀\e[49;38;5;149m▀▀\e[49;38;5;148m▀▀\e[49;38;5;184m▀▀▀▀\e[49;38;5;11m▀▀\e[49;38;5;220m▀▀▀▀▀▀\e[49;38;5;214m▀▀▀▀▀\e[49;38;5;208m▀▀▀\e[49;38;5;209m▀▀\e[49;38;5;203m▀▀▀▀▀▀\e[49;38;5;204m▀▀\e[49;38;5;198m▀\e[49;38;5;162m▀▀▀\e[49;38;5;168m▀▀▀\e[49;38;5;132m▀▀▀\e[49;38;5;133m▀▀\e[49;38;5;97m▀▀\e[49m \e[m
    \e[49m
    Github: https://github.com/AFCProject || Documentation: https://www.afcproject.dev/\n
    ";
    printf "This installation script will install/update the AFC Klipper extension to your system.\n\n"
    if [ "$files_updated_or_installed" == "True" ]; then
      printf "    Prior AFC-Klipper-Add-On installation detected: $GREEN%s$RESET\n" "$files_updated_or_installed"
    elif [ "$prior_installation" == "True" ]; then
      printf "    Prior AFC-Klipper-Add-On installation detected: $GREEN%s$RESET\n" "$prior_installation"
    elif [ "$prior_installation" == "False" ]; then
      printf "    Prior AFC-Klipper-Add-On installation detected: $RED%s$RESET\n" "$prior_installation"
    fi
    printf "\n      1. Printer Config Directory : %s \n" "$printer_config_dir"
    printf "      2. Klipper Directory        : %s \n" "$klipper_dir"
    printf "      3. Moonraker Config File    : %s \n" "$moonraker_config_file"
    printf "      4. Klipper Service Name     : %s \n" "$klipper_service"
    printf "      5. Branch                   : %s \n" "$branch"
    printf "      6. Moonraker Address        : %s    \n" "$moonraker"

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

