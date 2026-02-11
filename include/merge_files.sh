#!/usr/bin/env bash
# Armored Turtle Automated Filament Changer
#
# Copyright (C) 2024-2026 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

merge_configs() {
  local base_file="$1"
  local new_file="$2"
  local output_file="$3"
  local section=""
  local in_section=0
  local in_comment_block=0

  temp_file=$(mktemp)

  cp "$base_file" "$temp_file"

  while IFS= read -r line || [[ -n $line ]]; do
    if [[ $line =~ ^\[(.*)\]$ ]]; then
      section="${BASH_REMATCH[1]}"
      in_section=1
      in_comment_block=0
      if ! grep -q "^\[$section\]$" "$temp_file"; then
        echo -e "\n[$section]" >>"$temp_file"
      fi
    elif [[ $line =~ ^#-- ]]; then
      in_comment_block=1
      echo "$line" >>"$temp_file"
    elif [[ $in_comment_block -eq 1 ]]; then
      echo "$line" >>"$temp_file"
      if [[ $line =~ ^#-- ]]; then
        in_comment_block=0
      fi
    elif [[ $in_section -eq 1 && $line =~ ^([^#].*): ]]; then
      key="${BASH_REMATCH[1]}"
      if ! grep -q "^\s*$key:" "$temp_file"; then
        sed -i "/^\[$section\]$/a $line" "$temp_file"
      fi
    elif [[ $line =~ ^# ]]; then
      in_section=0
    elif [[ -z $line ]]; then
      echo "" >>"$temp_file"
    fi
  done <"$new_file"
  mv "$temp_file" "$output_file"
}

function cleanup_blank_lines() {
  sed -i '/^$/N;/^\n$/D' "$1"
}
