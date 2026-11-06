#!/usr/bin/env python3
"""Audit every stack's services for least-privilege settings.

Reads the compose YAML (never resolved secrets), merges each stack's includes,
and fails on findings that are not listed in EXCEPTIONS with a reason.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
STACKS = ROOT / 'docker' / 'stacks'
SOCKETS = {'/var/run/docker.sock', '/run/docker.sock'}

# Services that must start as root, whether by an explicit user or the image default.
ROOTFUL = {
    'alloy': 'reads host journals, /proc and /sys of other users',
    'alloy-host': 'host metrics need ptrace and DAC_READ_SEARCH as root',
    'kernel-exporter': 'reads other users\' /proc entries',
    'cadvisor': 'walks root-owned cgroups and image layers',
    'tailscale': 'manages host network interfaces',
    'gluetun': 'creates the WireGuard tunnel and firewall',
    'technitium': 'binds :53 on the host network',
    'nut': 'the UPS driver needs raw USB access',
    'azuracast': 'supervisord drops each service to its own user; caps are restricted',
    'plex': 'pms-docker init drops Plex Media Server to PLEX_UID',
    'syncthing': 'image has no non-root mode; s6 drops Syncthing to PUID',
}

# finding -> service -> reason; every entry was checked against the running containers.
EXCEPTIONS = {
    'root': ROOTFUL,
    'implicit-user': {
        **ROOTFUL,
        'tracearr': 'image runs as UID 1001',
        'redis-exporter': 'image runs as UID 59000',
        'peanut': 'image runs as UID 1000; Beacon data must be re-owned before moving it to 568',
    },
    'no-cap-drop': {
        'nut': 'Beacon was unreachable during the rollout; drop caps once it can be tested',
        'peanut': 'Beacon was unreachable during the rollout; drop caps once it can be tested',
    },
    'docker-socket': {
        'docker-socket-proxy': 'the read-only API gateway everything else uses',
        'hawser': 'Dockhand deploys stacks through it, so it needs the write API',
    },
}


def merge(base, extra):
    """Merge compose service overrides the way Compose does for the keys audited here."""
    out = dict(base)
    for key, value in extra.items():
        if key in ('cap_add', 'cap_drop', 'security_opt') and key in out:
            out[key] = list(dict.fromkeys([*out[key], *value]))
        elif key == 'volumes' and key in out:
            out[key] = [*out[key], *value]
        else:
            out[key] = value
    return out


def services(stack_file):
    """Return {service: merged definition} for one stack compose file."""
    doc = yaml.safe_load(stack_file.read_text()) or {}
    merged = {}
    for entry in doc.get('include', []):
        paths = entry['path'] if isinstance(entry, dict) else entry
        for rel in [paths] if isinstance(paths, str) else paths:
            data = yaml.safe_load((stack_file.parent / rel).read_text()) or {}
            for name, svc in (data.get('services') or {}).items():
                merged[name] = merge(merged.get(name, {}), svc or {})
    for name, svc in (doc.get('services') or {}).items():
        merged[name] = merge(merged.get(name, {}), svc or {})
    return merged


def uid(user):
    """Resolve a compose user to its UID string, using ${VAR:-default} defaults."""
    if user is None:
        return None
    user = re.sub(r'\$\{[A-Za-z_]+:-([^}]*)\}', r'\1', str(user))
    return user.split(':')[0]


def sources(svc):
    for volume in svc.get('volumes') or []:
        if isinstance(volume, dict):
            yield volume.get('source', '')
        else:
            yield str(volume).split(':')[0]


def findings(svc):
    found = []
    if svc.get('privileged'):
        found.append('privileged')
    if 'ALL' not in (svc.get('cap_drop') or []):
        found.append('no-cap-drop')
    if not any(str(o).replace('=', ':') == 'no-new-privileges:true' for o in svc.get('security_opt') or []):
        found.append('no-new-privileges')
    if any(source in SOCKETS for source in sources(svc)):
        found.append('docker-socket')
    user = uid(svc.get('user'))
    if user is None:
        found.append('implicit-user')
    elif user in ('0', 'root'):
        found.append('root')
    elif user == '1000':
        found.append('uid-1000')
    return found


def audit(stack_files):
    problems = []
    for stack_file in stack_files:
        stack = stack_file.parent.relative_to(STACKS)
        for name, svc in sorted(services(stack_file).items()):
            for finding in findings(svc):
                if name not in EXCEPTIONS.get(finding, {}):
                    problems.append(f'{stack}/{name}: {finding}')
    return problems


def main():
    stack_files = sorted(p for p in STACKS.glob('**/compose.yaml') if 'include' in p.read_text())
    problems = audit(stack_files)
    for problem in problems:
        print(f'✗ {problem}')
    if problems:
        return 1
    print(f'✓ {len(stack_files)} stacks meet the least-privilege rules')
    return 0


if __name__ == '__main__':
    sys.exit(main())
