#!/usr/bin/env python3
"""Read the Gitleaks pre-commit revision for use by CI."""
import pathlib
import re

config = pathlib.Path('.pre-commit-config.yaml').read_text()
blocks = re.split(r'(?m)^  - (?=repo:|hooks:)', config)
revisions = []
for block in blocks:
    if re.search(r'(?m)^[ \t]*repo: https://github\.com/gitleaks/gitleaks\s*$', block):
        revisions += re.findall(r'(?m)^[ \t]*rev: v(\d+\.\d+\.\d+)\s*$', block)
if len(revisions) != 1:
    raise SystemExit('Expected exactly one pinned Gitleaks hook version')
print(revisions[0])
