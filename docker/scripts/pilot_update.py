#!/usr/bin/env python3
"""Deploy one qualified image with health-checked recovery."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
TARGET = 'nexus/flaresolverr'
IMAGE = 'ghcr.io/flaresolverr/flaresolverr'
LOCK = 'technis-update-lock-flaresolverr'
CONTEXT = 'nexus'


class Recovered(Exception):
    pass


class RecoveryFailed(Exception):
    pass


def validate(target, image):
    if target != TARGET:
        raise ValueError(f'only {TARGET} is qualified')
    if not re.fullmatch(re.escape(IMAGE) + r'(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:[0-9a-f]{64}', image):
        raise ValueError(f'candidate must be {IMAGE}@sha256:<digest>')


def validate_endpoint(endpoint, *, local):
    if endpoint != ('unix:///var/run/docker.sock' if local else 'ssh://nexus'):
        raise ValueError('Docker context does not point to the expected endpoint')


def pilot_model():
    if CONTEXT == 'default':
        text = (ROOT / 'pilot-model.json').read_text()
    else:
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'DOCKER_CONFIG', 'SSH_AUTH_SOCK') if key in os.environ}
        text = run([str(ROOT / 'docker/scripts/stacks'), '-s', 'nexus', 'pilot-model'], env=env)
    model = json.loads(text)
    if set(model.get('services', {})) != {'flaresolverr'}:
        raise RuntimeError('pilot model must contain only flaresolverr')
    if 'TECHNIS_PILOT_SECRET' in text or '127.255.254.253' in text or '$' in text:
        raise RuntimeError('pilot service requires a secret or unresolved interpolation')
    service = model['services']['flaresolverr']
    if service.get('environment') != {'CAPTCHA_SOLVER': 'none', 'LOG_HTML': 'false',
                                      'LOG_LEVEL': 'info', 'TZ': 'America/New_York'}:
        raise RuntimeError('pilot environment changed; reassess required secrets')
    if set(service) - {'cap_drop', 'command', 'container_name', 'deploy', 'entrypoint', 'environment',
                       'healthcheck', 'hostname', 'image', 'networks', 'restart', 'security_opt', 'tmpfs', 'user'}:
        raise RuntimeError('pilot configuration changed; reassess required secrets')
    if service.get('command') is not None or service.get('entrypoint') is not None:
        raise RuntimeError('pilot command changed; reassess required secrets')
    return model


def apply_images(candidate, previous, deploy):
    try:
        deploy(candidate)
    except Exception as failure:
        try:
            deploy(previous)
        except Exception as recovery:
            raise RecoveryFailed('candidate and recovery failed') from recovery
        raise Recovered('candidate failed; previous image restored and healthy') from failure


def run(args, *, env=None, input_text=None):
    result = subprocess.run(args, env=env, input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed (exit {result.returncode})')
    return result.stdout.strip()


def docker(*args):
    return run(['docker', '--context', CONTEXT, *args])


def current():
    data = json.loads(docker('inspect', '--format', '{{json .State}}', 'flaresolverr'))
    image = docker('inspect', '--format', '{{.Image}}', 'flaresolverr')
    service = docker('inspect', '--format', '{{index .Config.Labels "com.docker.compose.service"}}', 'flaresolverr')
    if service != 'flaresolverr' or data.get('Status') != 'running' or data.get('Health', {}).get('Status') != 'healthy':
        raise RuntimeError('pilot container is not running and healthy under Compose')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise RuntimeError('cannot determine previous image ID')
    return image


def observe(expected):
    for _ in range(7):
        if current() != expected:
            raise RuntimeError('running image does not match the requested image')
        time.sleep(10)


def verify_head(expected, snapshot):
    if not re.fullmatch(r'[0-9a-f]{40}', expected) or snapshot()[0] != expected:
        raise RuntimeError('Git changed during the pilot deployment')


def deploy(image, expected, model):
    model['services']['flaresolverr']['image'] = image
    run(['docker', '--context', CONTEXT, 'compose', '--project-directory', str(ROOT / 'docker/stacks/nexus'),
         '--project-name', 'nexus', '-f', '-', 'up', '-d', '--no-deps',
         '--pull', 'never', '--wait', '--wait-timeout', '240', 'flaresolverr'], input_text=json.dumps(model))
    observe(expected)


def main():
    global CONTEXT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', help='qualified stack/service')
    parser.add_argument('image', help='candidate image pinned by digest')
    parser.add_argument('--apply', action='store_true', help='perform the deployment')
    parser.add_argument('--local', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--hold-lock', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        validate(args.target, args.image)
        print(f'{args.target}: {args.image}')
        if not args.apply:
            print('Preview only; add --apply to deploy and validate.')
            return 0
        CONTEXT = 'default' if args.local else 'nexus'
        expected_head = os.environ.get('PILOT_EXPECTED_HEAD', '') if args.local and args.hold_lock else ''
        if args.local and args.hold_lock and not re.fullmatch(r'[0-9a-f]{40}', expected_head):
            raise ValueError('missing verified Git revision')
        endpoint = run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}', CONTEXT])
        validate_endpoint(endpoint, local=args.local)
        model = pilot_model()
        previous = current()
        docker('image', 'inspect', previous, '--format', '{{.Id}}')
        docker('pull', args.image)
        candidate_id = docker('image', 'inspect', args.image, '--format', '{{.Id}}')
        docker('create', '--name', LOCK, '--entrypoint', '/bin/true', previous)
        release = False
        try:
            if current() != previous:
                raise RuntimeError('running image changed while acquiring the update lock')
            try:
                def apply(image):
                    deploy(image, previous if image == previous else candidate_id, model)
                    if expected_head and image != previous:
                        from pilot_gateway import read_url, snapshot
                        verify_head(expected_head, lambda: snapshot(read_url))
                apply_images(args.image, previous, apply)
            except Recovered:
                release = not args.hold_lock
                raise
            release = True
        finally:
            if release:
                docker('rm', LOCK)
        print('Candidate deployed and healthy.')
        return 0
    except (ValueError, RuntimeError, Recovered, RecoveryFailed) as error:
        print(f'Update stopped: {error}', file=sys.stderr)
        print(f'If present, {LOCK} blocks further attempts until recovery is verified.', file=sys.stderr)
        if isinstance(error, Recovered):
            return 10
        if isinstance(error, RecoveryFailed):
            return 11
        return 1


if __name__ == '__main__':
    sys.exit(main())
