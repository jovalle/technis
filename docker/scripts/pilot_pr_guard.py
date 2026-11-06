#!/usr/bin/env python3
"""Keep a paused pilot from being automatically promoted."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

from pilot_gateway import verify_policy
from pilot_recovery import IMAGE, restore_image

PATH = 'docker/services/flaresolverr/compose.yaml'


def check(previous, current, policy):
    if previous == current:
        return
    old, new = list(IMAGE.finditer(previous)), list(IMAGE.finditer(current))
    if not old:
        return
    if len(old) != 1 or len(new) != 1:
        raise ValueError('pilot image must remain pinned')
    if old[0]['digest'] != new[0]['digest']:
        restore_image(previous, current, new[0]['digest'])
        verify_policy(policy)


def main():
    try:
        if os.environ.get('GITHUB_EVENT_NAME') != 'pull_request':
            return 0
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
        base = event['pull_request']['base']['sha']
        if not re.fullmatch(r'[0-9a-f]{40}', base):
            raise ValueError('invalid pull request base')
        old = subprocess.run(['git', 'show', base + ':' + PATH], check=True,
                             capture_output=True, text=True).stdout
        policy = subprocess.run(['git', 'show', base + ':renovate.json'],
                                capture_output=True, text=True)
        check(old, Path(PATH).read_text(), policy.stdout if policy.returncode == 0 else '{}')
        return 0
    except (KeyError, OSError, ValueError, TypeError, subprocess.SubprocessError):
        print('Pilot digest update is paused or includes unqualified changes.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
