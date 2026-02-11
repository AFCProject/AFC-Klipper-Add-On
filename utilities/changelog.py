#!/usr/bin/env python3

import re
import os
from datetime import datetime
from collections import defaultdict


def consolidate_by_calendar_month(filename="CHANGELOG.md"):
    if not os.path.exists(filename):
        print(f"File {filename} not found.")
        return

    with open(filename, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    header = []
    entries = []
    current_entry = None

    date_pattern = re.compile(r'^##\s\[(\d{4}-\d{2}-\d{2})\]')
    category_pattern = re.compile(r'^###\s(\w+)')

    now = datetime.now()
    current_month_key = now.strftime('%Y-%m')

    processing_header = True
    for line in lines:
        date_match = date_pattern.match(line)
        if date_match:
            processing_header = False
            if current_entry:
                entries.append(current_entry)

            current_entry = {
                'date': datetime.strptime(date_match.group(1), '%Y-%m-%d'),
                'raw_date': date_match.group(1),
                'content': defaultdict(list)
            }
            current_category = "Uncategorized"
            continue

        if processing_header:
            header.append(line)
        elif current_entry:
            cat_match = category_pattern.match(line)
            if cat_match:
                current_category = cat_match.group(1)
            # Match bullet points or indented lines (sub-bullets)
            elif line.strip().startswith(('-', '*')) or (line.startswith(' ') and line.strip()):
                # Use the full line to preserve the newline character (\n)
                current_entry['content'][current_category].append(line)

    if current_entry:
        entries.append(current_entry)

    new_changelog = header[:]
    consolidated_data = defaultdict(lambda: defaultdict(list))

    for entry in entries:
        entry_month_key = entry['date'].strftime('%Y-%m')

        if entry_month_key == current_month_key:
            new_changelog.append(f"\n## [{entry['raw_date']}]\n")
            for cat, items in entry['content'].items():
                new_changelog.append(f"### {cat}\n")
                new_changelog.extend(items)  # items already contain \n
        else:
            for cat, items in entry['content'].items():
                std_cat = "Fixed" if cat.lower() in ['fixes', 'fixed'] else cat
                consolidated_data[entry_month_key][std_cat].extend(items)

    sorted_months = sorted(consolidated_data.keys(), reverse=True)
    for month in sorted_months:
        month_obj = datetime.strptime(month, '%Y-%m')
        readable_month = month_obj.strftime('%B %Y')

        new_changelog.append(f"\n## [{readable_month}]\n")
        for cat in sorted(consolidated_data[month].keys()):
            new_changelog.append(f"### {cat}\n")
            new_changelog.extend(consolidated_data[month][cat])

    output_file = "CHANGELOG_Consolidated.md"
    with open(output_file, "w", encoding='utf-8') as f:
        f.writelines(new_changelog)

    print(f"Successfully consolidated previous months into {output_file}.")


if __name__ == "__main__":
    consolidate_by_calendar_month()