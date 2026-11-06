#!/usr/bin/env python3
"""Obtain only the pilot's SSH identity from Infisical."""

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request


def infisical_secret(request, env, path, name):
    if (path, name) not in {('/ci/pilot', 'PILOT_DEPLOY_SSH_KEY'),
                            ('/ci/pilot-recovery', 'PILOT_GITHUB_APP_KEY')}:
        raise ValueError('unqualified pilot credential')
    oidc_url = env['ACTIONS_ID_TOKEN_REQUEST_URL']
    parsed = urllib.parse.urlsplit(oidc_url)
    if (parsed.scheme != 'https' or not re.fullmatch(r'pipelines[a-z0-9-]*\.actions\.githubusercontent\.com', parsed.hostname or '')
            or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError('untrusted GitHub OIDC endpoint')
    oidc_url += ('&' if parsed.query else '?') + urllib.parse.urlencode({'audience': 'infisical'})
    jwt = request(oidc_url, env['ACTIONS_ID_TOKEN_REQUEST_TOKEN'])['value']
    login = request('https://app.infisical.com/api/v1/auth/oidc-auth/login', '',
                    {'identityId': env['INFISICAL_IDENTITY_ID'], 'jwt': jwt})
    query = urllib.parse.urlencode({'projectId': env['INFISICAL_PROJECT_ID'],
                                   'environment': 'prod', 'secretPath': path,
                                   'expandSecretReferences': 'false', 'includeImports': 'false'})
    result = request('https://app.infisical.com/api/v4/secrets/' + name + '?' + query,
                     login['accessToken'])
    secret = result['secret']
    if secret.get('secretValueHidden'):
        raise ValueError('pilot credential is unavailable')
    return secret['secretValue']


def pilot_key(request, env):
    return infisical_secret(request, env, '/ci/pilot', 'PILOT_DEPLOY_SSH_KEY')


def validate_request(operation, digest, host):
    if operation not in ('deploy', 'unlock') or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('unsupported pilot request')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', host):
        raise ValueError('invalid pilot host')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, bearer, payload=None):
    headers = {'Accept': 'application/json'}
    if bearer:
        headers['Authorization'] = 'Bearer ' + bearer
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
        body = response.read(1_000_001)
    if len(body) > 1_000_000:
        raise ValueError('identity response too large')
    return json.loads(body)


def ssh_call(operation, digest, host, host_key, key):
    validate_request(operation, digest, host)
    parts = host_key.split()
    if len(parts) != 2 or parts[0] != 'ssh-ed25519' or '\n' in host_key or '\r' in host_key:
        raise ValueError('invalid pinned host key')
    base64.b64decode(parts[1], validate=True)
    if not isinstance(key, str) or not key.startswith('-----BEGIN ' 'OPENSSH PRIVATE KEY-----'):
        raise ValueError('invalid pilot SSH credential')
    env = {'PATH': '/usr/bin:/bin', 'HOME': os.environ.get('HOME', '/tmp')}
    with tempfile.TemporaryDirectory(prefix='pilot-agent-', dir='/tmp') as tmp:
        socket = str(Path(tmp) / 'agent.sock')
        agent = subprocess.run(['/usr/bin/ssh-agent', '-a', socket, '-s'], env=env,
                               text=True, capture_output=True, check=True)
        match = re.search(r'SSH_AUTH_SOCK=([^;\n]+);', agent.stdout)
        pid = re.search(r'SSH_AGENT_PID=([0-9]+);', agent.stdout)
        if not match or match.group(1) != socket or not pid:
            raise RuntimeError('could not start SSH agent')
        env.update({'SSH_AUTH_SOCK': socket, 'SSH_AGENT_PID': pid.group(1)})
        try:
            subprocess.run(['/usr/bin/ssh-add', '-'], input=key, env=env, text=True,
                           capture_output=True, check=True, timeout=20)
            known = Path(tmp) / 'known_hosts'
            known.write_text('nexus ' + host_key + '\n')
            result = subprocess.run(['/usr/bin/ssh', '-F', '/dev/null',
                '-o', 'HostName=' + host, '-o', 'HostKeyAlias=nexus',
                '-o', 'UserKnownHostsFile=' + str(known), '-o', 'GlobalKnownHostsFile=/dev/null',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'ForwardAgent=no',
                '-o', 'ClearAllForwardings=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
                'root@nexus', operation + ' sha256:' + digest], env=env, capture_output=True,
                text=True, timeout=900)
            return result.returncode
        finally:
            subprocess.run(['/usr/bin/ssh-agent', '-k'], env=env, capture_output=True)


def main(args):
    try:
        if len(args) != 2:
            raise ValueError('expected operation and digest')
        host = os.environ['PILOT_HOST']
        validate_request(args[0], args[1], host)
        key = pilot_key(request, os.environ)
        code = ssh_call(args[0], args[1], host, os.environ['PILOT_HOST_KEY'], key)
        if code not in (0, 10, 11):
            return 1
        return code
    except (KeyError, OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print('Pilot access failed; no credentials or remote output were logged.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
