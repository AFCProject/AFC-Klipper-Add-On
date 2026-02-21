#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# Colors
NC='\033[0m'
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
WHITE='\033[0;37m'
MENU_GREEN='\e[38;5;143;49m'

BOLD_RED='\033[1;31m'
BOLD_GREEN='\033[1;32m'
BOLD_YELLOW='\033[1;33m'
BOLD_BLUE='\033[1;34m'
BOLD_PURPLE='\033[1;35m'
BOLD_CYAN='\033[1;36m'
BOLD_WHITE='\033[1;37m'

# Messages
INFO="${WHITE}"
ERROR="${BOLD_RED}"
SUCCESS="${BOLD_PURPLE}"
PROMPT="${BOLD_WHITE}"
WARNING="${BOLD_YELLOW}"
RESET="${NC}"

print_msg() {
  # Function to print messages with different colors based on the message type.
  # Arguments:
  #   $1 - The type of message (INFO, WARNING, ERROR, PROMPT, or other).
  #   $2 - The message to be printed.
  # The function uses ANSI escape codes to color the messages.

  local type=$1
  shift
  case $type in
    INFO) echo -e "${INFO}$1${RESET}" ;;       # Print info message in blue.
    WARNING) echo -e "${WARNING}$1${RESET}" ;; # Print warning message in bold yellow.
    ERROR) echo -e "${ERROR}$1${RESET}" ;;     # Print error message in bold red.
    PROMPT) echo -e "${PROMPT}$1${RESET}" ;;   # Print prompt message in bold white.
    *) echo -e "$1" ;;                         # Print other messages without color.
  esac
}


print_section_delimiter() {
  print_msg SUCCESS "----------------------------------------"
}

