#!/usr/bin/env python3
"""Validate the only command accepted by the pilot SSH identity."""

import json
import os
from pathlib import Path
import re
import sys
import subprocess
import urllib.request
import uuid

ROOT = Path('/root/technis-pilot')


def candidate(command):
    match = re.fullmatch(r'deploy sha256:([0-9a-f]{64})', command)
    if not match:
        raise ValueError('unsupported pilot command')
    return 'ghcr.io/flaresolverr/flaresolverr@sha256:' + match.group(1)


def verify_desired(image, text):
    pattern = r'^[ \t]*image:[ \t]*ghcr\.io/flaresolverr/flaresolverr(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:([0-9a-f]{64})[ \t]*$'
    digests = re.findall(pattern, text, re.MULTILINE)
    if len(digests) != 1 or image != 'ghcr.io/flaresolverr/flaresolverr@sha256:' + digests[0]:
        raise ValueError('candidate is not the current pilot image in Git')


def verify_installed_config(installed, desired):
    pattern = r'(?m)^([ \t]*image:[ \t]*ghcr\.io/flaresolverr/flaresolverr(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:)[0-9a-f]{64}([ \t]*)$'
    local, local_count = re.subn(pattern, r'\1DIGEST\2', installed)
    remote, remote_count = re.subn(pattern, r'\1DIGEST\2', desired)
    if local_count != 1 or remote_count != 1 or local != remote:
        raise ValueError('installed pilot configuration differs from Git')


def verify_policy(text):
    config = json.loads(text)
    if not isinstance(config, dict):
        raise ValueError('invalid pilot policy')
    rules = config.get('packageRules', [])
    if not isinstance(rules, list) or not rules or config.get('enabled') is not True:
        raise ValueError('pilot automerge is paused')
    rule = rules[-1]
    if (not isinstance(rule, dict) or rule.get('matchManagers') != ['docker-compose']
            or rule.get('matchFileNames') != ['docker/services/flaresolverr/compose.yaml']
            or rule.get('matchPackageNames') != ['ghcr.io/flaresolverr/flaresolverr']
            or rule.get('automerge') is not True):
        raise ValueError('pilot automerge is paused or unqualified')


def read_url(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'technis-pilot', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=10) as response:
        data = response.read(65537)
    if len(data) > 65536:
        raise ValueError('pilot Git response is unexpectedly large')
    return data.decode('utf-8')


def snapshot(read):
    def head():
        ref = json.loads(read('https://api.github.com/repos/jovalle/technis/git/ref/heads/main?nonce=' + uuid.uuid4().hex))['object']['sha']
        if not re.fullmatch(r'[0-9a-f]{40}', ref):
            raise ValueError('invalid Git branch head')
        return ref

    revision = head()
    base = 'https://raw.githubusercontent.com/jovalle/technis/' + revision + '/'
    config = read(base + 'docker/services/flaresolverr/compose.yaml')
    policy = read(base + 'renovate.json')
    if head() != revision:
        raise ValueError('Git branch changed while checking the pilot')
    return revision, config, policy


def unlock(image, docker):
    locked = docker('inspect', '--format', '{{.Image}}', 'technis-update-lock-flaresolverr')
    running = docker('inspect', '--format', '{{.Image}}', 'flaresolverr')
    state = json.loads(docker('inspect', '--format', '{{json .State}}', 'flaresolverr'))
    references = json.loads(docker('image', 'inspect', running, '--format', '{{json .RepoDigests}}')) or []
    if locked != running or state.get('Status') != 'running' or state.get('Health', {}).get('Status') != 'healthy' or image not in references:
        raise ValueError('previous image is not healthy or does not match the recovery lock')
    docker('rm', 'technis-update-lock-flaresolverr')


def trusted_tree(root):
    for parent in (root, *root.parents):
        stat = parent.lstat()
        if parent.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
            raise RuntimeError('pilot installation has unsafe ownership or permissions')
    def fail(error):
        raise error

    for path, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        for name in dirs + files:
            entry = Path(path) / name
            stat = entry.lstat()
            if entry.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
                raise RuntimeError('pilot installation has unsafe ownership or permissions')


def main():
    try:
        command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
        unlocking = command.startswith('unlock ')
        image = candidate('deploy ' + command[7:] if unlocking else command)
        if os.geteuid() != 0:
            raise RuntimeError('pilot gateway must run as root')
        trusted_tree(ROOT)
        revision, config, policy = snapshot(read_url)
        verify_desired(image, config)
        verify_installed_config((ROOT / 'docker/services/flaresolverr/compose.yaml').read_text(), config)
        if not unlocking:
            verify_policy(policy)
        if unlocking:
            def docker(*args):
                result = subprocess.run(['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', *args],
                                        capture_output=True, text=True, env={'PATH': '/usr/bin:/bin', 'HOME': '/root'})
                if result.returncode:
                    raise RuntimeError('local Docker command failed')
                return result.stdout.strip()

            unlock(image, docker)
            print('Pilot recovery lock cleared.')
            return 0
        os.execve('/usr/bin/python3', ['/usr/bin/python3', str(ROOT / 'docker/scripts/pilot_update.py'),
                  'nexus/flaresolverr', image, '--apply', '--local', '--hold-lock'],
                  {'PATH': '/usr/bin:/bin', 'HOME': '/root', 'PILOT_EXPECTED_HEAD': revision})
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(f'pilot gateway: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
