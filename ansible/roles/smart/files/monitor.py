#!/usr/bin/env python3
"""Schedule and check SMART self-tests using TrueNAS system email."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state.json'
CONFIG = ROOT / 'config.json'


def failure(data):
    ata = data.get('ata_smart_self_test_log', {})
    table = (ata.get('extended') or ata.get('standard') or {}).get('table', [])
    if table:
        entry = table[0]
        status = entry['status']
        if 3 <= status['value'] >> 4 <= 8:
            return f"{status.get('string', 'Failed')} (power-on hours {entry.get('lifetime_hours', '?')})"
    table = data.get('nvme_self_test_log', {}).get('table', [])
    if table:
        entry = table[0]
        result = entry['self_test_result']
        if result['value'] in (5, 6, 7):
            return f"{result.get('string', 'Failed')} (power-on hours {entry.get('power_on_hours', '?')})"
    return None


def notify(problems):
    from truenas_api_client import Client
    recipient = json.loads(CONFIG.read_text())['recipient']
    with Client() as client:
        client.call('mail.send', {'subject': f'TrueNAS {socket.gethostname()} SMART self-test problem',
                    'text': '\n'.join(problems), 'to': [recipient], 'queue': False, 'timeout': 30}, job=True)


def main():
    mode = sys.argv[1]
    if mode not in ('short', 'long', 'check'):
        raise ValueError('Expected short, long, or check')
    scan = subprocess.run(['smartctl', '--scan-open'], capture_output=True, text=True, check=True)
    devices = [line.split()[0] for line in scan.stdout.splitlines() if line.startswith('/dev/')]
    if not devices:
        notify(['No SMART-capable drives were detected.'])
        return
    problems = []
    for device in devices:
        args = ['smartctl', '-a', '-j', device] if mode == 'check' else ['smartctl', '-t', mode, device]
        result = subprocess.run(args, capture_output=True, text=True)
        if mode != 'check':
            if result.returncode:
                problems.append(f'{device}: could not start {mode} self-test: {result.stdout} {result.stderr}'.strip())
            continue
        try:
            if result.returncode & 7:
                raise ValueError('smartctl could not read device')
            data = json.loads(result.stdout)
            issue = failure(data)
            if issue:
                problems.append(f"{device} ({data.get('serial_number', 'unknown serial')}): {issue}")
        except (ValueError, KeyError, TypeError) as e:
            problems.append(f'{device}: could not check SMART test results: {e}')
    if mode != 'check':
        if problems:
            notify(problems)
        return
    try:
        previous = json.loads(STATE.read_text())
    except FileNotFoundError:
        previous = []
    new = sorted(set(problems) - set(previous))
    if new:
        notify(new)
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(problems))
    os.replace(temp, STATE)


if __name__ == '__main__':
    main()
