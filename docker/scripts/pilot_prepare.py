#!/usr/bin/env python3
"""Identify a single pilot digest change without trusting event text as code."""

import json
import os
import re
import subprocess
import sys

from pilot_recovery import IMAGE, restore_image

PATH = 'docker/services/flaresolverr/compose.yaml'


def digests(previous, current):
    old = list(IMAGE.finditer(previous))
    new = list(IMAGE.finditer(current))
    if len(old) != 1 or len(new) != 1 or old[0]['digest'] == new[0]['digest']:
        raise ValueError('pilot change is not a single digest update')
    restore_image(previous, current, new[0]['digest'])
    return old[0]['digest'], new[0]['digest']


def main():
    try:
        with open(os.environ['GITHUB_EVENT_PATH'], encoding='utf-8') as event_file:
            event = json.load(event_file)
        before, after = event['before'], event['after']
        if (not re.fullmatch(r'[0-9a-f]{40}', before) or before == '0' * 40
                or not re.fullmatch(r'[0-9a-f]{40}', after) or after != os.environ['GITHUB_SHA']):
            raise ValueError('invalid push revisions')
        def show(revision):
            return subprocess.run(['git', 'show', revision + ':' + PATH], check=True,
                                  text=True, capture_output=True).stdout
        old, new = digests(show(before), show(after))
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            output.write(f'before={before}\nprevious={old}\ndigest={new}\n')
        return 0
    except (KeyError, ValueError, OSError, subprocess.SubprocessError):
        print('Pilot update does not match the required digest-only change.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
