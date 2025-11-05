#!/usr/bin/env python3
"""Format and sort Makefile"""

import re
from collections import OrderedDict

with open("Makefile", "r") as f:
    content = f.read()

# Extract targets with their comments and bodies
targets = OrderedDict()
lines = content.split("\n")
i = 0
while i < len(lines):
    line = lines[i]
    # Check if it's a comment before a target (# or ##)
    if (line.startswith("# ") or line.startswith("## ")) and i + 1 < len(lines):
        comment = line
        i += 1
        target_line = lines[i]
        if re.match(r"^[a-zA-Z_-]+:", target_line):
            target_name = target_line.split(":")[0]
            # Collect target body
            body_lines = [target_line]
            i += 1
            while i < len(lines) and (lines[i].startswith("\t") or lines[i] == ""):
                body_lines.append(lines[i])
                if (
                    lines[i] == ""
                    and i + 1 < len(lines)
                    and not lines[i + 1].startswith("\t")
                ):
                    break
                i += 1
            # Strip trailing empty lines
            while body_lines and body_lines[-1] == "":
                body_lines.pop()
            targets[target_name] = (comment, "\n".join(body_lines))
            continue
    # Check for target without comment
    elif re.match(r"^[a-zA-Z_-]+:", line):
        target_name = line.split(":")[0]
        body_lines = [line]
        i += 1
        while i < len(lines) and (lines[i].startswith("\t") or lines[i] == ""):
            body_lines.append(lines[i])
            if (
                lines[i] == ""
                and i + 1 < len(lines)
                and not lines[i + 1].startswith("\t")
            ):
                break
            i += 1
        # Strip trailing empty lines
        while body_lines and body_lines[-1] == "":
            body_lines.pop()
        targets[target_name] = (None, "\n".join(body_lines))
        continue
    i += 1

# Sort targets alphabetically
sorted_targets = OrderedDict(sorted(targets.items()))

# Extract .PHONY line and all target names to update it
phony_match = re.search(r"^\.PHONY:(.*)$", content, re.MULTILINE)
all_target_names = list(sorted_targets.keys())

if phony_match:
    # Combine existing .PHONY items with all target names
    existing_phony_items = phony_match.group(1).strip().split()
    all_phony_items = list(
        set(existing_phony_items + all_target_names)
    )  # Use set to avoid duplicates
    all_phony_items.sort()
    phony_line = f".PHONY: {' '.join(all_phony_items)}"
else:
    # Create a new .PHONY line with all targets
    all_target_names.sort()
    phony_line = f".PHONY: {' '.join(all_target_names)}"

# Extract DEPS section
deps_match = re.search(r"^DEPS := \\\n((?:\t.*\n)*)", content, re.MULTILINE)
if deps_match:
    deps_items = [
        line.strip().rstrip("\\").strip()
        for line in deps_match.group(1).split("\n")
        if line.strip()
    ]
    deps_items.sort()
    deps_section = "DEPS := \\\n" + " \\\n".join(f"\t{item}" for item in deps_items)
else:
    deps_section = "DEPS :="

# Rebuild Makefile
output = [phony_line, "", deps_section, ""]
for _target_name, (comment, body) in sorted_targets.items():
    if comment:
        output.append(comment)
    output.append(body)
    output.append("")

with open("Makefile", "w") as f:
    f.write("\n".join(output))

print("✓ Makefile formatted and sorted")
